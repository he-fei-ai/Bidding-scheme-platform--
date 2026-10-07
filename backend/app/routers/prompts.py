import asyncio
import time

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from ..db import get_db
from ..security.auth import User, get_current_user
from ..services.ai.prompts import (
    _ALL_PROMPTS,
    extract_variables,
    reload_prompt_cache,
)


# 增强 2026-09-29（安全）：提示词路由此前只有 PUT/reset 要求登录，GET 列表与详情
# 完全开放——任何未认证用户都能读取平台全部内部提示词（含 AI 工作流逻辑），构成
# 信息泄露，且与本路由写接口、平台其它路由的鉴权口径不一致。前端 http 客户端已由
# 拦截器统一注入 token，测试环境 AUTH_ENABLED=False 时 get_current_user 返回
# mock admin 不拦截，故补齐读接口鉴权对现有调用方无破坏。
router = APIRouter(
    prefix="/prompts",
    tags=["prompts"],
    dependencies=[Depends(get_current_user)],
)


class PromptUpdate(BaseModel):
    content: str


# 默认内容 → 变量集合缓存：避免每次保存都重复解析硬编码模板。
# D-1 修复 2026-10-04：原注释声称"缓存"但函数体每次都重新 extract_variables，
# 缓存从未生效。注册表 _ALL_PROMPTS 的 content 在导入后固定不变，按 key 缓存安全。
_DEFAULT_VARS_CACHE: dict[str, set[str]] = {}


def _default_variables(key: str) -> set[str]:
    cached = _DEFAULT_VARS_CACHE.get(key)
    if cached is not None:
        return cached
    meta = _ALL_PROMPTS.get(key)
    result = set(extract_variables(meta["content"])) if meta else set()
    _DEFAULT_VARS_CACHE[key] = result
    return result


def _shared_refs(template: str) -> set[str]:
    """提取模板中的 {shared:NAME} 引用（用于校验引用有效性）。"""
    import re

    return set(re.findall(r"\{shared:([A-Za-z_]\w{1,})\}", template or ""))


def _validate_prompt_content(key: str, content: str) -> None:
    """保存前校验：
    1. 不得引用未注册的 {shared:NAME}（否则渲染时保留字面量，AI 收到坏块）；
    2. 不得静默删除默认模板要求的关键变量占位符（会导致对应上下文无法注入，
       AI 生成质量下降且难以排查）。允许通过 ?force=true 显式跳过（高级用户）。

    缺失变量以「默认模板变量 - 新内容变量」计算，大小写不敏感。
    """
    refs = _shared_refs(content)
    bad_refs = [r for r in refs if _shared_target(r) is None]
    if bad_refs:
        raise HTTPException(
            status_code=400,
            detail=f"提示词引用了未注册的共享规则: {sorted(bad_refs)}",
        )

    default_vars = _default_variables(key)
    new_vars = set(extract_variables(content))
    # 大小写不敏感比较
    new_vars_lower = {v.lower() for v in new_vars}
    dropped = sorted(v for v in default_vars if v.lower() not in new_vars_lower)
    if dropped:
        raise HTTPException(
            status_code=400,
            detail=(
                f"新内容删除了默认模板要求的变量占位符: {dropped}；"
                f"这会导致对应上下文无法注入 AI。如确需删除，请使用 force=true 强制保存。"
            ),
        )


def _shared_target(name: str) -> str | None:
    """按 _registry._resolve_shared 的查找顺序解析共享规则名。"""
    for cand in (name, "SHARED_" + name, name.upper(), "SHARED_" + name.upper()):
        entry = _ALL_PROMPTS.get(cand)
        if entry is not None:
            return entry["content"]
    return None


def _archive_version(
    conn,
    key: str,
    *,
    content: str,
    default_content: str,
    label: str,
    category: str,
    source: str,
    changed_by: str,
) -> None:
    """把被覆盖的旧内容归档到 prompt_versions（仅在旧内容非空时归档）。"""
    if not content:
        return
    conn.execute(
        """INSERT INTO prompt_versions
           (key, content, default_content, label, category, source, changed_by, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now', 'localtime'))""",
        (key, content, default_content or "", label or "", category or "", source, changed_by),
    )
    # 每个 key 最多保留 50 个历史版本，超出淘汰最旧
    conn.execute(
        """DELETE FROM prompt_versions WHERE key = ? AND id NOT IN (
               SELECT id FROM prompt_versions WHERE key = ? ORDER BY id DESC LIMIT 50
           )""",
        (key, key),
    )


def _current_row(conn, key: str):
    return conn.execute(
        "SELECT key, category, label, content, default_content FROM prompt_templates WHERE key = ?",
        (key,),
    ).fetchone()


@router.get("")
async def list_prompts():
    """获取所有提示词（含分类、标签、DB 内容、实际变量列表）"""
    # PERF（2026-10-05）：同步 sqlite 读取派发至工作线程（连接重试 sleep
    # 不再发生在事件循环内，与本文件其余端点同口径）
    def _read_all():
        with get_db(readonly=True) as conn:
            rows = conn.execute(
                "SELECT key, category, label, content, default_content FROM prompt_templates ORDER BY key",
            ).fetchall()
            return {row["key"]: dict(row) for row in rows}

    db_map = await asyncio.to_thread(_read_all)

    result = []
    for key in sorted(_ALL_PROMPTS.keys()):
        meta = _ALL_PROMPTS[key]
        if key in db_map:
            row = db_map[key]
            # 修正：此前 variables 恒取硬编码模板，用户 DB 编辑增删变量后前端展示的
            # 变量列表与实际内容不符。改为按实际内容解析（内容为空时回退注册表）。
            effective = row.get("content") or meta["content"]
            result.append({**row, "variables": sorted(set(extract_variables(effective)))})
        else:
            result.append(
                {
                    "key": key,
                    "category": meta["category"],
                    "label": meta["label"],
                    "content": meta["content"],
                    "default_content": meta["content"],
                    "variables": meta.get("variables", []),
                },
            )
    return result


@router.get("/{key}")
async def get_prompt_detail(key: str):
    # PERF（2026-10-05）：纯读派发至工作线程，不在事件循环内做同步 I/O
    def _read_row():
        with get_db(readonly=True) as conn:
            row = _current_row(conn, key)
            return dict(row) if row else None

    row = await asyncio.to_thread(_read_row)
    meta = _ALL_PROMPTS.get(key)
    if row:
        effective = row["content"] or (meta["content"] if meta else "")
        return {**dict(row), "variables": sorted(set(extract_variables(effective)))}
    if meta:
        return {
            "key": key,
            "category": meta["category"],
            "label": meta["label"],
            "content": meta["content"],
            "default_content": meta["content"],
            "variables": meta.get("variables", []),
        }
    raise HTTPException(status_code=404, detail="提示词不存在")


@router.put("/{key}")
async def update_prompt(
    key: str,
    data: PromptUpdate,
    force: bool = Query(False, description="跳过变量校验强制保存"),
    current: User = Depends(get_current_user),
):
    # 修复：default_content 在 key 不存在时应回退到硬编码默认值，
    # 原 SQL 用 COALESCE(...,'') 会导致新 key 的 default_content 为空字符串，
    # 前端执行"重置为默认"时得到空内容
    meta = _ALL_PROMPTS.get(key)
    fallback_default = meta["content"] if meta else ""

    if not force:
        _validate_prompt_content(key, data.content)

    changed_by = getattr(current, "username", "") or ""

    # PERF（2026-10-05）：归档+写入整段同步 I/O（含全局写锁）派发至工作线程，
    # 锁争用在 worker 阻塞，不冻结事件循环
    def _write():
        with get_db() as conn:
            old = _current_row(conn, key)
            if old:
                _archive_version(
                    conn,
                    key,
                    content=old["content"],
                    default_content=old["default_content"],
                    label=old["label"],
                    category=old["category"],
                    source="edit",
                    changed_by=changed_by,
                )
            conn.execute(
                """INSERT INTO prompt_templates (key, content, default_content, updated_at)
               VALUES (?, ?, COALESCE((SELECT default_content FROM prompt_templates WHERE key = ?), ?), datetime('now', 'localtime'))
               ON CONFLICT(key) DO UPDATE SET
                content=excluded.content,
                updated_at=datetime('now', 'localtime')""",
                (key, data.content, key, fallback_default),
            )

    await asyncio.to_thread(_write)
    reload_prompt_cache()
    return {"status": "ok"}


@router.post("/{key}/reset")
async def reset_prompt(key: str, current: User = Depends(get_current_user)):
    """重置为默认提示词（重置前归档当前自定义内容，可回滚）"""
    meta = _ALL_PROMPTS.get(key)
    if not meta:
        raise HTTPException(status_code=404, detail="提示词不存在")

    default_content = meta["content"]
    changed_by = getattr(current, "username", "") or ""

    # PERF（2026-10-05）：归档+重置写操作派发至工作线程
    def _write():
        with get_db() as conn:
            old = _current_row(conn, key)
            if old:
                _archive_version(
                    conn,
                    key,
                    content=old["content"],
                    default_content=old["default_content"],
                    label=old["label"],
                    category=old["category"],
                    source="reset",
                    changed_by=changed_by,
                )
            conn.execute(
                """INSERT INTO prompt_templates (key, content, default_content, updated_at)
               VALUES (?, ?, ?, datetime('now', 'localtime'))
               ON CONFLICT(key) DO UPDATE SET
                content=excluded.default_content,
                updated_at=datetime('now', 'localtime')""",
                (key, default_content, default_content),
            )

    await asyncio.to_thread(_write)
    reload_prompt_cache()
    return {"status": "ok"}


@router.get("/{key}/versions")
async def list_prompt_versions(key: str, limit: int = Query(50, ge=1, le=200)):
    """列出某提示词的历史版本（按时间倒序，不含 content 正文以减小体积）。"""
    # PERF（2026-10-05）：纯读派发至工作线程
    def _read_versions():
        with get_db(readonly=True) as conn:
            return [dict(r) for r in conn.execute(
                """SELECT id, key, label, category, source, changed_by, created_at,
                      length(content) AS content_length
               FROM prompt_versions WHERE key = ?
               ORDER BY id DESC LIMIT ?""",
                (key, limit),
            ).fetchall()]

    return await asyncio.to_thread(_read_versions)


@router.get("/{key}/versions/{version_id}")
async def get_prompt_version(key: str, version_id: int):
    """查看某个历史版本的完整内容。"""
    # PERF（2026-10-05）：纯读派发至工作线程
    def _read_version():
        with get_db(readonly=True) as conn:
            row = conn.execute(
                "SELECT * FROM prompt_versions WHERE id = ? AND key = ?",
                (version_id, key),
            ).fetchone()
            return dict(row) if row else None

    row = await asyncio.to_thread(_read_version)
    if not row:
        raise HTTPException(status_code=404, detail="版本不存在")
    return row


@router.post("/{key}/versions/{version_id}/rollback")
async def rollback_prompt(
    key: str,
    version_id: int,
    current: User = Depends(get_current_user),
):
    """回滚到指定历史版本：先把当前内容归档（source=rollback），再用历史版本覆盖。"""
    changed_by = getattr(current, "username", "") or ""

    # PERF（2026-10-05）：读版本+归档+覆盖的整段同步 I/O 派发至工作线程；
    # 版本不存在用哨兵返回值回主协程抛 404（语义不变）
    def _rollback():
        with get_db() as conn:
            target = conn.execute(
                "SELECT * FROM prompt_versions WHERE id = ? AND key = ?",
                (version_id, key),
            ).fetchone()
            if not target:
                return None
            target = dict(target)
            old = _current_row(conn, key)
            if old:
                _archive_version(
                    conn,
                    key,
                    content=old["content"],
                    default_content=old["default_content"],
                    label=old["label"],
                    category=old["category"],
                    source="rollback",
                    changed_by=changed_by,
                )
            conn.execute(
                """INSERT INTO prompt_templates (key, category, label, content, default_content, updated_at)
               VALUES (?, ?, ?, ?, ?, datetime('now', 'localtime'))
               ON CONFLICT(key) DO UPDATE SET
                content=excluded.content,
                category=excluded.category,
                label=excluded.label,
                updated_at=datetime('now', 'localtime')""",
                (
                    key,
                    target["category"] or "",
                    target["label"] or "",
                    target["content"],
                    target["default_content"] or "",
                ),
            )
            return target

    if await asyncio.to_thread(_rollback) is None:
        raise HTTPException(status_code=404, detail="版本不存在")
    reload_prompt_cache()
    return {"status": "ok", "restored_from": version_id}


# ──────────────────────────────────────────────────────────────────────
# 新增管理接口（2026-10-06 增强）
# ──────────────────────────────────────────────────────────────────────

@router.post("/sync-hardcode")
async def sync_hardcode_to_db(
    overwrite: bool = Query(False, description="是否覆盖DB中已修改的内容；默认仅同步DB中不存在的新提示词"),
    current: User = Depends(get_current_user),
):
    """一键同步硬编码提示词到数据库。

    用途：开发者修改了硬编码提示词后，无需手动执行 init 脚本，
    直接通过此接口将新版本同步到 DB。

    Args:
        overwrite: False（默认）仅同步 DB 中不存在的新提示词；
                   True 覆盖所有提示词的 default_content（保留用户已修改的 content）。
    """
    changed_by = getattr(current, "username", "") or ""

    def _sync():
        synced = []
        skipped = []
        with get_db() as conn:
            for key, meta in _ALL_PROMPTS.items():
                default_content = meta["content"]
                # 检查 DB 中是否存在
                existing = conn.execute(
                    "SELECT content, default_content FROM prompt_templates WHERE key = ?",
                    (key,),
                ).fetchone()

                if existing is None:
                    # DB 中不存在，插入新记录
                    conn.execute(
                        """INSERT INTO prompt_templates (key, category, label, content, default_content, updated_at)
                       VALUES (?, ?, ?, ?, ?, datetime('now', 'localtime'))""",
                        (key, meta["category"], meta["label"], default_content, default_content),
                    )
                    synced.append(key)
                elif overwrite:
                    # 覆盖 default_content（保留用户修改的 content）
                    conn.execute(
                        """UPDATE prompt_templates
                       SET default_content = ?, updated_at = datetime('now', 'localtime')
                       WHERE key = ?""",
                        (default_content, key),
                    )
                    synced.append(f"{key} (default_content)")
                else:
                    skipped.append(key)

        return {"synced": synced, "skipped": skipped, "total": len(_ALL_PROMPTS)}

    result = await asyncio.to_thread(_sync)
    reload_prompt_cache()
    return {
        "status": "ok",
        "message": f"同步完成：{len(result['synced'])} 个提示词已更新",
        **result,
    }


@router.get("/metrics")
async def get_prompt_metrics_api(
    current: User = Depends(get_current_user),
):
    """获取提示词使用监控指标。

    返回每个提示词的调用次数、缓存命中、Token 消耗等统计信息。
    """
    from ..services.ai.prompts import get_prompt_metrics

    metrics = get_prompt_metrics()

    # 按调用次数降序排列
    sorted_metrics = dict(
        sorted(metrics.items(), key=lambda x: x[1].get("calls", 0), reverse=True)
    )

    # 汇总统计
    total_calls = sum(m.get("calls", 0) for m in metrics.values())
    total_tokens = sum(m.get("total_tokens", 0) for m in metrics.values())

    return {
        "status": "ok",
        "summary": {
            "total_prompts": len(metrics),
            "total_calls": total_calls,
            "total_estimated_tokens": total_tokens,
        },
        "metrics": sorted_metrics,
    }


@router.get("/alerts")
async def get_prompt_alerts(
    current: User = Depends(get_current_user),
):
    """获取提示词监控告警。

    告警规则：
    1. DB命中率低于80%
    2. 调用量较上次查询飙升超过5倍
    """
    from ..services.ai.prompts import check_prompt_alerts

    alerts = check_prompt_alerts()

    return {
        "status": "ok",
        "alert_count": len(alerts),
        "alerts": alerts,
    }


@router.get("/metrics/history")
async def get_metrics_history_api(
    hours: int = Query(24, description="最近多少小时的数据"),
    current: User = Depends(get_current_user),
):
    """获取提示词监控历史数据（用于趋势图）。"""
    from ..services.ai.prompts import get_metrics_history

    history = get_metrics_history(hours)

    # 计算整体DB命中率
    total_calls = sum(h["total_calls"] for h in history)
    total_db_hits = sum(h["total_db_hits"] for h in history)
    db_hit_ratio = (total_db_hits / total_calls) if total_calls > 0 else 0

    return {
        "status": "ok",
        "history": history,
        "summary": {
            "total_points": len(history),
            "db_hit_ratio": db_hit_ratio,
        },
    }


@router.post("/metrics/snapshot")
async def trigger_metrics_snapshot(
    current: User = Depends(get_current_user),
):
    """手动触发一次指标快照（用于测试或手动采样）。"""
    from ..services.ai.prompts import snapshot_metrics_history

    snapshot = snapshot_metrics_history()
    return {
        "status": "ok",
        "snapshot": snapshot,
    }


@router.get("/metrics/by-category")
async def get_metrics_by_category(
    current: User = Depends(get_current_user),
):
    """按分类统计提示词调用量（用于饼图展示）。"""
    from ..services.ai.prompts import get_prompt_metrics

    metrics = get_prompt_metrics()

    # 按分类聚合
    category_stats: dict[str, dict] = {}
    for key, data in metrics.items():
        # 从注册表获取分类
        meta = _ALL_PROMPTS.get(key, {})
        category = meta.get("category", "未分类")

        if category not in category_stats:
            category_stats[category] = {
                "category": category,
                "calls": 0,
                "total_tokens": 0,
                "prompt_count": 0,
            }

        category_stats[category]["calls"] += data.get("calls", 0)
        category_stats[category]["total_tokens"] += data.get("total_tokens", 0)
        category_stats[category]["prompt_count"] += 1

    # 转换为列表并按调用量降序
    result = sorted(
        category_stats.values(),
        key=lambda x: x["calls"],
        reverse=True,
    )

    return {
        "status": "ok",
        "categories": result,
    }


@router.get("/metrics/predict")
async def predict_token_usage_api(
    hours: int = Query(24, description="预测未来多少小时"),
    current: User = Depends(get_current_user),
):
    """基于历史数据预测未来Token消耗。"""
    from ..services.ai.prompts import predict_token_usage

    prediction = predict_token_usage(hours)
    return {
        "status": "ok",
        "prediction": prediction,
    }


@router.get("/metrics/export")
async def export_metrics_report(
    format: str = Query("json", description="导出格式：json 或 csv"),
    current: User = Depends(get_current_user),
):
    """导出监控报表（JSON或CSV格式）。"""
    import io
    import csv
    from fastapi.responses import StreamingResponse
    from ..services.ai.prompts import get_prompt_metrics, get_metrics_history, check_prompt_alerts

    metrics = get_prompt_metrics()
    history = get_metrics_history(24)
    alerts = check_prompt_alerts()

    if format == "csv":
        # CSV导出
        output = io.StringIO()
        writer = csv.writer(output)

        # 写入汇总
        writer.writerow(["=== 提示词监控报表 ==="])
        writer.writerow(["导出时间", time.strftime("%Y-%m-%d %H:%M:%S")])
        writer.writerow([])

        # 写入指标
        writer.writerow(["=== 各提示词指标 ==="])
        writer.writerow(["提示词Key", "调用次数", "DB命中", "回退", "估算Token"])
        for key, data in sorted(metrics.items(), key=lambda x: x[1].get("calls", 0), reverse=True):
            writer.writerow([
                key,
                data.get("calls", 0),
                data.get("db_hits", 0),
                data.get("fallback_hits", 0),
                data.get("total_tokens", 0),
            ])

        writer.writerow([])
        writer.writerow(["=== 告警列表 ==="])
        writer.writerow(["级别", "类型", "提示词", "信息"])
        for alert in alerts:
            writer.writerow([
                alert.get("level", ""),
                alert.get("type", ""),
                alert.get("key", ""),
                alert.get("message", ""),
            ])

        output.seek(0)
        return StreamingResponse(
            iter([output.getvalue()]),
            media_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename=prompt_metrics_{int(time.time())}.csv"},
        )

    # 默认JSON导出
    return {
        "status": "ok",
        "export_time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "summary": {
            "total_prompts": len(metrics),
            "total_calls": sum(m.get("calls", 0) for m in metrics.values()),
            "total_tokens": sum(m.get("total_tokens", 0) for m in metrics.values()),
        },
        "metrics": metrics,
        "history_points": len(history),
        "alerts": alerts,
    }


# ──────────────────────────────────────────────────────────────────────
# 预算配置与成本分析
# ──────────────────────────────────────────────────────────────────────

class BudgetUpdate(BaseModel):
    amount: float


@router.get("/budget")
async def get_budget_config(
    current: User = Depends(get_current_user),
):
    """获取月度预算配置。"""
    from ..services.ai.prompts import get_monthly_budget, get_prompt_metrics
    from ..services.ai.prompts._cost import get_cost_summary

    budget = get_monthly_budget()
    metrics = get_prompt_metrics()
    cost = get_cost_summary(metrics)

    return {
        "status": "ok",
        "monthly_budget": budget,
        "current_cost": cost["total_cost"],
        "usage_ratio": round(cost["total_cost"] / budget * 100, 1) if budget > 0 else 0,
        "remaining": round(budget - cost["total_cost"], 2),
    }


@router.put("/budget")
async def update_budget_config(
    body: BudgetUpdate,
    current: User = Depends(get_current_user),
):
    """更新月度预算配置。"""
    from ..services.ai.prompts import set_monthly_budget

    set_monthly_budget(body.amount)
    return {
        "status": "ok",
        "message": f"月度预算已更新为 ¥{body.amount:.2f}",
    }


@router.get("/cost/suggestions")
async def get_cost_suggestions(
    current: User = Depends(get_current_user),
):
    """获取成本优化建议。"""
    from ..services.ai.prompts import get_prompt_metrics
    from ..services.ai.prompts._cost import generate_cost_optimization_suggestions

    metrics = get_prompt_metrics()
    suggestions = generate_cost_optimization_suggestions(metrics)

    return {
        "status": "ok",
        "suggestions": suggestions,
    }


@router.get("/budget/predict")
async def predict_budget(
    current: User = Depends(get_current_user),
):
    """预测月底是否会超预算。"""
    from ..services.ai.prompts import predict_month_end_cost

    prediction = predict_month_end_cost()
    return {
        "status": "ok",
        "prediction": prediction,
    }


@router.get("/cost/compare-models")
async def compare_cost_models(
    current: User = Depends(get_current_user),
):
    """多模型成本对比。"""
    from ..services.ai.prompts import get_prompt_metrics
    from ..services.ai.prompts._cost import compare_models

    metrics = get_prompt_metrics()
    comparison = compare_models(metrics)

    return {
        "status": "ok",
        "comparison": comparison,
    }


@router.get("/alerts/thresholds")
async def get_alert_thresholds_api(
    current: User = Depends(get_current_user),
):
    """获取告警阈值配置。"""
    from ..services.ai.prompts import get_alert_thresholds

    return {
        "status": "ok",
        "thresholds": get_alert_thresholds(),
    }


class ThresholdUpdate(BaseModel):
    db_hit_threshold: float | None = None
    spike_multiplier: float | None = None
    spike_min_baseline: int | None = None


@router.put("/alerts/thresholds")
async def update_alert_thresholds_api(
    body: ThresholdUpdate,
    current: User = Depends(get_current_user),
):
    """更新告警阈值配置。"""
    from ..services.ai.prompts import set_alert_thresholds

    set_alert_thresholds(
        db_hit_threshold=body.db_hit_threshold,
        spike_multiplier=body.spike_multiplier,
        spike_min_baseline=body.spike_min_baseline,
    )

    return {
        "status": "ok",
        "message": "告警阈值已更新",
    }


@router.get("/cost/optimization-summary")
async def get_optimization_summary(
    current: User = Depends(get_current_user),
):
    """获取成本优化效果汇总看板。"""
    from ..services.ai.prompts import get_prompt_metrics
    from ..services.ai.prompts._cost import _optimization_baselines, compare_optimization_result

    metrics = get_prompt_metrics()

    # 遍历所有已记录的优化项目
    projects = []
    total_saved = 0.0

    for name in _optimization_baselines.keys():
        result = compare_optimization_result(name, metrics)
        if result:
            projects.append(result)
            total_saved += result["improvement"]["cost_saved"]

    return {
        "status": "ok",
        "summary": {
            "total_optimization_projects": len(projects),
            "total_cost_saved": round(total_saved, 2),
        },
        "projects": projects,
    }


@router.get("/budget/suggest")
async def suggest_budget(
    current: User = Depends(get_current_user),
):
    """获取下月预算建议。"""
    from ..services.ai.prompts import suggest_next_month_budget

    suggestion = suggest_next_month_budget()
    return {
        "status": "ok",
        "suggestion": suggestion,
    }
