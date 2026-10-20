import asyncio
import logging
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

logger = logging.getLogger(__name__)


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

    UNIFY 2026-10-17：具体规则下沉到 ``prompts._registry.validate_prompt_content``
    （返回问题列表，便于单测与非 HTTP 场景复用），本函数只负责转成 400 响应，
    避免两处各写一份判定逻辑导致口径漂移。
    """
    from ..services.ai.prompts import validate_prompt_content

    problems = validate_prompt_content(key, content)
    if not problems:
        return
    # 保留既有文案（前端与既有测试依赖），仅对新增项给出通用描述
    detail = "；".join(problems)
    if any(p.startswith("删除了默认模板要求的变量占位符") for p in problems):
        detail += "；如确需删除，请使用 force=true 强制保存。"
    raise HTTPException(status_code=400, detail=detail)


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
async def list_prompts(include_orphans: bool = Query(False, description="是否返回DB中存在但未在代码注册的孤儿提示词")):
    """获取所有提示词（含分类、标签、DB 内容、实际变量列表）

    BUG-FIX-PROMPT-R5（孤儿记录不可见）：本接口只遍历 ``_ALL_PROMPTS``，
    DB 中存在但未注册的 key（例如历史遗留 key、或早期 PUT 未校验 key 时
    写入的垃圾行）完全不出现在响应里。运维因此无法发现/清理它们，
    实测生产库 data/bid_assistant.sqlite3 中 prompt_templates 有 84 行而
    注册表仅 60 个，其中含 ``_TEST_*`` 4 条测试污染行。

    修复：``include_orphans=true`` 时追加返回孤儿记录，并打上
    ``orphan: true`` 标记，前端可据此提示清理；默认关闭以保持既有响应形状。
    """
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
            result.append({**row, "variables": sorted(set(extract_variables(effective))), "orphan": False})
        else:
            result.append(
                {
                    "key": key,
                    "category": meta["category"],
                    "label": meta["label"],
                    "content": meta["content"],
                    "default_content": meta["content"],
                    "variables": meta.get("variables", []),
                    "orphan": False,
                },
            )

    if include_orphans:
        for key in sorted(set(db_map) - set(_ALL_PROMPTS)):
            row = db_map[key]
            effective = row.get("content") or ""
            result.append({**row, "variables": sorted(set(extract_variables(effective))), "orphan": True})

    return result


# ──────────────────────────────────────────────────────────────────────────
# 顺序约束（重要，勿调整）
#
# BUG-FIX-PROMPT-R1（路由遮蔽）：本文件原先把 `GET /prompts/{key}` 与
# `PUT /prompts/{key}` 声明在所有静态子路由（/metrics、/alerts、/budget…）之前。
# Starlette/FastAPI 按**声明顺序**匹配路由，`/prompts/{key}` 会先命中并把
# key 绑定成 "metrics" / "alerts" / "budget"，导致这些接口全部 404（实测复现）：
#   GET  /prompts/metrics   -> 404 提示词不存在
#   GET  /prompts/alerts    -> 挂起（叠加 _cache.check_prompt_alerts 死锁）
#   GET  /prompts/budget    -> 404
#   PUT  /prompts/budget    -> 422（错命中 update_prompt，要求 content 字段）
# 修复：把带路径参数的 `/{key}` 路由整体下沉到文件末尾（见 "带路径参数的路由" 一节），
# 保证静态路由优先匹配。tests/test_prompts_route_shadowing_2026_10_09.py 做回归守卫。
# ──────────────────────────────────────────────────────────────────────────


# ──────────────────────────────────────────────────────────────────────
# R7 2026-10-12：模板生命周期管理仪表盘
# ──────────────────────────────────────────────────────────────────────
@router.get("/overview")
async def get_prompts_overview():
    """提示词生命周期管理仪表盘 —— 聚合 DB / 注册表 / 漂移 / 调用指标。

    返回每个已注册 key 的：
      - key, category, label, variables 声明
      - source: db / fallback / missing（运行时内容来源）
      - drifted: DB 内容与注册表是否一致
      - calls, errors, not_found: 调用指标
      - updated_at: DB 最后更新时间

    冷启动时 metrics 为空字典，所有 calls=0。
    """
    from ..services.ai.prompts import get_prompt_metrics

    # 从注册表拿全部 key
    reg = _ALL_PROMPTS

    # R7-SECURITY: get_db() 是同步阻塞调用，async 端点必须用 to_thread 包起来；
    # 且 overview 是纯读查询，用 readonly=True 跳过全局写锁，避免阻塞其它写操作。
    def _fetch_db_rows():
        with get_db(readonly=True) as conn:
            return conn.execute(
                "SELECT key, content, category, label, updated_at FROM prompt_templates"
            ).fetchall()
    db_rows = await asyncio.to_thread(_fetch_db_rows)
    db_keys = {r["key"]: r for r in db_rows}

    # 从内存 metrics 拿调用数据（get_prompt_metrics 内部已做锁保护 + 深拷贝）
    metrics_snapshot = get_prompt_metrics()

    items = []
    for key, meta in reg.items():
        db_row = db_keys.get(key)
        content_reg = meta["content"]

        # 漂移检测（DB 存在且与注册表不一致 → drifted=True）
        drifted = False
        updated_at = None
        if db_row is not None:
            updated_at = db_row["updated_at"]
            drifted = (db_row["content"] or "") != (content_reg or "")

        # 运行时调用指标
        m = metrics_snapshot.get(key, {})
        items.append({
            "key": key,
            "category": meta.get("category", ""),
            "label": meta.get("label", ""),
            "variables": meta.get("variables", []),
            "db_present": db_row is not None,
            "drifted": drifted,
            "updated_at": updated_at,
            "calls": m.get("calls", 0),
            "db_hits": m.get("db_hits", 0),
            "fallback_hits": m.get("fallback_hits", 0),
            "total_tokens": m.get("total_tokens", 0),
        })

    # 汇总统计
    total = len(items)
    drifted_count = sum(1 for it in items if it["drifted"])
    db_present = sum(1 for it in items if it["db_present"])
    total_calls = sum(it["calls"] for it in items)

    # ENHANCE 2026-10-17（共享规则复用可观测性）：暴露 {shared:} 复用机制的
    # 实际落地情况。审计发现注册表中无任何模板引用共享规则（adoption_rate=0），
    # 即 SHARED_HEADING_RULES / SHARED_FORBIDDEN_WORDS / SHARED_OUTPUT_SPEC
    # 三条规则从未注入任何提示词——「复用与覆盖」能力形同虚设。此处量化暴露，
    # 便于持续跟踪收编进度（而非靠人工翻代码发现）。
    try:
        from ..services.ai.prompts import audit_shared_usage

        shared_audit = audit_shared_usage()
    except Exception as e:  # 审计失败不得影响主接口
        logger.warning("shared usage audit failed: %s", e)
        shared_audit = None

    return {
        "total": total,
        "db_present": db_present,
        "drifted": drifted_count,
        "total_calls": total_calls,
        "items": items,
        "shared_usage": shared_audit,
    }


# ──────────────────────────────────────────────────────────────────────
# 新增管理接口（2026-10-06 增强）
# ──────────────────────────────────────────────────────────────────────

@router.post("/sync-hardcode")
async def sync_hardcode_to_db(
    overwrite: bool = Query(False, description="是否把未被用户改过的 content 一并刷新为硬编码新版本"),
    reset_modified: bool = Query(
        False,
        description="是否强制用硬编码覆盖用户已修改的 content（危险：会丢弃自定义，清理前请先归档版本）",
    ),
    current: User = Depends(get_current_user),
):
    """一键同步硬编码提示词到数据库。

    用途：开发者修改了硬编码提示词后，无需手动执行 init 脚本，
    直接通过此接口将新版本同步到 DB。

    BUG-FIX-PROMPT-R4（同步后仍不生效）：原 ``overwrite=True`` 只更新
    ``default_content`` 而**从不刷新 content**。而 ``_cache.get_prompt`` 读的是
    ``content``，于是开发者同步后线上仍跑旧文案——正是 _cache._check_hardcode_drift
    一直在告警的那类漂移，且该接口「看起来成功了」却毫无效果。

    修复：区分「用户改过」与「未被改过」两种 content——
      - content == 旧 default_content（未被用户改过）→ 直接跟随硬编码刷新
      - content != 旧 default_content（用户改过）→ 默认保留，
        需显式 reset_modified=true 才覆盖（覆盖前按 source='sync' 归档版本）。
    """
    changed_by = getattr(current, "username", "") or ""

    def _sync():
        # BUG-FIX-PROMPT-R8（同步报告语义模糊）：原实现中 `synced` 变量在
        # overwrite=True 时**总是**追加每个 key（无论实际是新增、刷新还是保留），
        # 使 `{len(synced)} 个已处理` 的语义误导——它其实只是「本轮触碰过的 key
        # 总数」，与 `refreshed + kept_modified + (新增)` 之和相等，读者无法判断
        # 「真正同步了哪几个」。现拆分语义：
        #   - `synced`        : 仅新增（DB 中原本不存在、本轮 INSERT）
        #   - `refreshed`     : content 跟随硬编码刷新的（含用户未改过 或 reset_modified=True）
        #   - `kept_modified` : 用户改过且未 reset_modified，仅 default_content 更新
        #   - `skipped`       : overwrite=False 时未做任何动作的
        synced = []           # 仅新增
        refreshed = []        # 跟随硬编码刷新的 content
        kept_modified = []    # 保留用户自定义
        skipped = []
        with get_db() as conn:
            for key, meta in _ALL_PROMPTS.items():
                default_content = meta["content"]
                existing = conn.execute(
                    "SELECT content, default_content FROM prompt_templates WHERE key = ?",
                    (key,),
                ).fetchone()

                if existing is None:
                    conn.execute(
                        """INSERT INTO prompt_templates (key, category, label, content, default_content, updated_at)
                           VALUES (?, ?, ?, ?, ?, datetime('now', 'localtime'))""",
                        (key, meta["category"], meta["label"], default_content, default_content),
                    )
                    synced.append(key)
                    continue

                if not overwrite:
                    skipped.append(key)
                    continue

                old_content = existing["content"] or ""
                old_default = existing["default_content"] or ""
                user_modified = old_content != old_default

                if user_modified and not reset_modified:
                    # 只刷新 default_content（供「重置」使用），content 保留用户版本
                    conn.execute(
                        """UPDATE prompt_templates
                           SET default_content = ?, updated_at = datetime('now', 'localtime')
                           WHERE key = ?""",
                        (default_content, key),
                    )
                    kept_modified.append(key)
                elif user_modified and reset_modified:
                    # 危险路径：归档用户版本后再覆盖，保证可回滚
                    _archive_version(
                        conn,
                        key,
                        content=old_content,
                        default_content=old_default,
                        label=meta["label"],
                        category=meta["category"],
                        source="sync",
                        changed_by=changed_by,
                    )
                    conn.execute(
                        """UPDATE prompt_templates
                           SET content = ?, default_content = ?, category = ?, label = ?,
                               updated_at = datetime('now', 'localtime')
                           WHERE key = ?""",
                        (default_content, default_content, meta["category"], meta["label"], key),
                    )
                    refreshed.append(key)
                else:
                    # 未被用户改过：content 跟随硬编码，漂移彻底消除
                    conn.execute(
                        """UPDATE prompt_templates
                           SET content = ?, default_content = ?, category = ?, label = ?,
                               updated_at = datetime('now', 'localtime')
                           WHERE key = ?""",
                        (default_content, default_content, meta["category"], meta["label"], key),
                    )
                    refreshed.append(key)

        return {
            "synced": synced,
            "refreshed": refreshed,
            "kept_modified": kept_modified,
            "skipped": skipped,
            "total": len(_ALL_PROMPTS),
        }

    result = await asyncio.to_thread(_sync)
    reload_prompt_cache()
    # BUG-FIX-PROMPT-R8：报告语义澄清——新增 / 刷新 / 保留 / 跳过 四项之和恒等于 total
    return {
        "status": "ok",
        "message": (
            f"同步完成：新增 {len(result['synced'])} 个，"
            f"刷新 {len(result['refreshed'])} 个，"
            f"保留用户自定义 {len(result['kept_modified'])} 个，"
            f"跳过 {len(result['skipped'])} 个（共 {result['total']} 个提示词）"
        ),
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

    # BUG-FIX-PROMPT-B2：set_monthly_budget 现对非正数抛 ValueError；不拦截会变成
    # 500，而这是纯粹的用户输入错误，应返回 400。
    try:
        set_monthly_budget(body.amount)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
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

    # BUG-FIX-PROMPT-B2：越界阈值会被静默持久化，使告警规则永久失效。转 400。
    try:
        set_alert_thresholds(
            db_hit_threshold=body.db_hit_threshold,
            spike_multiplier=body.spike_multiplier,
            spike_min_baseline=body.spike_min_baseline,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

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

# ══════════════════════════════════════════════════════════════════════════
# 带路径参数的路由（必须声明在全部静态子路由之后）
#
# 原因：Starlette 按声明顺序匹配。`/prompts/{key}` 若早于 `/prompts/metrics`
# 声明，会把 "metrics"/"alerts"/"budget" 当作 key 绑定并返回 404。
# 因此本节统一置于文件末尾。详见文件头「顺序约束」注释。
# ══════════════════════════════════════════════════════════════════════════
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

    # BUG-FIX-PROMPT-R2（脏 key 写入）：原实现对未知 key 也照写不误。INSERT 只写
    # (key, content, default_content, updated_at)，category/label 落库为 NULL，
    # 于是 DB 里多出一条既不在注册表、又没有分类/标签的孤儿记录；
    # 同时 _validate_prompt_content 因 default_vars 为空集而形同虚设。
    # 另外路由遮蔽修复前，`PUT /prompts/budget` 会误命中本处理器并写入
    # key='budget' 的垃圾行。改为：仅允许更新注册表中的 key。
    if meta is None:
        raise HTTPException(
            status_code=404,
            detail=f"提示词不存在: {key}（不可新增未注册提示词，请先在代码中 _reg 注册）",
        )
    fallback_default = meta["content"]

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
            # BUG-FIX-PROMPT-R3：补写 category/label。原先仅在 key 不存在时插入，
            # 而插入的 category/label 为 NULL（见 R2）。现统一带上 meta 值，
            # 保证 list_prompts / sync-hardcode / 监控按分类聚合都有可靠字段。
            conn.execute(
                """INSERT INTO prompt_templates
                   (key, category, label, content, default_content, updated_at)
                   VALUES (?, ?, ?, ?, ?, datetime('now', 'localtime'))
                   ON CONFLICT(key) DO UPDATE SET
                    content=excluded.content,
                    category=excluded.category,
                    label=excluded.label,
                    updated_at=datetime('now', 'localtime')""",
                (key, meta["category"], meta["label"], data.content, fallback_default),
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
    force: bool = Query(False, description="跳过变量校验强制回滚"),
    current: User = Depends(get_current_user),
):
    """回滚到指定历史版本：先把当前内容归档（source=rollback），再用历史版本覆盖。

    BUG-FIX-PROMPT-R8（2026-10-17，回滚绕过内容校验）：``PUT /{key}`` 保存时会
    执行 ``_validate_prompt_content``（共享引用有效性 + 不得删除默认变量），
    但回滚——本质上是「用历史内容覆盖当前内容」的**同种写操作**——却完全不校验。
    于是历史版本中若存在无效 ``{shared:}`` 引用或缺失关键变量占位符，回滚会
    把一份 PUT 都拒绝接受的坏内容写回生产，且管理页无任何提示，表现为
    「回滚后 AI 输出突然劣化」。现与 PUT 同口径校验，并同样提供 force 逃生舱。
    """
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
            # R8：与 PUT 同口径校验（force=true 可跳过）
            if not force:
                _validate_prompt_content(key, target.get("content") or "")
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
            # BUG-FIX-PROMPT-R6（回滚清空 default_content）：ON CONFLICT 分支
            # 不更新 default_content（正确），但**首次插入**分支会写入
            # `target["default_content"] or ""`。历史版本若归档时 default_content
            # 本就为空（早期 PUT 未回退硬编码默认值所致，见 db.seed 说明），
            # 回滚后 prompt_templates.default_content 就变成空串，前端
            # 「重置为默认」将把提示词清空 —— 回滚反而制造了数据损坏。
            # 修复：空值回退到注册表硬编码默认值，绝不写入空 default_content。
            meta = _ALL_PROMPTS.get(key)
            fallback_default = meta["content"] if meta else ""
            restored_default = target.get("default_content") or fallback_default
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
                    target["category"] or (meta["category"] if meta else ""),
                    target["label"] or (meta["label"] if meta else ""),
                    target["content"],
                    restored_default,
                ),
            )
            return target

    if await asyncio.to_thread(_rollback) is None:
        raise HTTPException(status_code=404, detail="版本不存在")
    reload_prompt_cache()
    return {"status": "ok", "restored_from": version_id}
