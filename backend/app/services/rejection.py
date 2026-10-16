"""废标检查服务层

实现：
1. 滚动分段策略（长文档处理）
2. 三类并行检查：废标项 / 错别字 / 逻辑谬误
3. 多投标文件并发
4. 报告导出（DOCX）
"""

import asyncio
from datetime import UTC, datetime
import json
import logging
import os
import re
from typing import Any

from ..config import REJECTION_REPORTS_DIR
from ..db import get_db
from . import check_report_excel as _excel_report
from .ai.prompts import get_prompt, sanitize_prompt_value
from .ai.provider_factory import create_provider, get_ai_config
from .ai.providers.base import AIMessage
from .ai.workflows import collect_json_response, with_timeout


logger = logging.getLogger(__name__)


# ========== 滚动分段策略 ==========

CHUNK_SIZE = 20000  # 每段字符数
CHUNK_OVERLAP = 2000  # 重叠字符数


def split_into_chunks(
    text: str,
    chunk_size: int = CHUNK_SIZE,
    overlap: int = CHUNK_OVERLAP,
) -> list[str]:
    """将文本按固定大小切分，相邻片段重叠。

    HARDEN 2026-10-15（P1 · 死循环防护）：
      当 ``overlap >= chunk_size`` 时，步进 ``step = chunk_size - overlap``
      会 ≤ 0，导致 ``start`` 不前进甚至倒退 → **无限循环**。此前仅有
      `test_overlap_larger_than_chunk_size_edge` 暴露了分块数量爆炸
      (<100)，但极端参数（overlap=chunk_size）仍会真正挂死。
      现将 overlap 钳制到 ``[0, chunk_size // 2]`` 安全区间：
        - overlap < 0 → 视为 0（无重叠，纯顺序切分）
        - overlap >= chunk_size → 降至 chunk_size // 2（保留合理重叠，
          既不丢内容也不死循环）
    """
    if not text:
        return []
    # 钳制 overlap 到安全区间
    if overlap < 0:
        overlap = 0
    elif overlap >= chunk_size:
        overlap = chunk_size // 2
    if len(text) <= chunk_size:
        return [text]
    chunks: list[str] = []
    start = 0
    step = chunk_size - overlap  # 保证 step > 0
    while start < len(text):
        end = start + chunk_size
        chunks.append(text[start:end])
        if end >= len(text):
            break
        start += step  # 重叠
    return chunks


def split_paragraphs(text: str) -> list[str]:
    """按段落切分文本（用于错别字检查）"""
    if not text:
        return []
    paragraphs = re.split(r"\n\s*\n|\n", text)
    return [p.strip() for p in paragraphs if p.strip()]


# ========== 任务管理 ==========


def create_task(
    project_id: int | None,
    tender_text: str,
    bid_documents: list[dict],
    check_items: list[dict],
    enabled_dimensions: list[str],
) -> int:
    """创建废标检查任务"""
    with get_db() as conn:
        cursor = conn.execute(
            """INSERT INTO rejection_checks
               (project_id, tender_text, bid_documents, check_items, enabled_dimensions, status, progress)
               VALUES (?, ?, ?, ?, ?, 'pending', 0)""",
            (
                project_id,
                tender_text,
                json.dumps(bid_documents, ensure_ascii=False),
                json.dumps(check_items, ensure_ascii=False),
                json.dumps(enabled_dimensions, ensure_ascii=False),
            ),
        )
        return cursor.lastrowid


def update_task_progress(task_id: int, progress: int, message: str = ""):
    """更新任务进度"""
    with get_db() as conn:
        conn.execute(
            """UPDATE rejection_checks
               SET progress = ?, progress_message = ?, updated_at = datetime('now', 'localtime')
               WHERE id = ?""",
            (progress, message, task_id),
        )


def update_task_status(task_id: int, status: str, results: dict | None = None, error: str = ""):
    """更新任务状态和结果"""
    with get_db() as conn:
        if results is not None:
            conn.execute(
                """UPDATE rejection_checks
                   SET status = ?, results = ?, error = ?, updated_at = datetime('now', 'localtime')
                   WHERE id = ?""",
                (status, json.dumps(results, ensure_ascii=False), error, task_id),
            )
        else:
            conn.execute(
                """UPDATE rejection_checks
                   SET status = ?, error = ?, updated_at = datetime('now', 'localtime')
                   WHERE id = ?""",
                (status, error, task_id),
            )


def get_task(task_id: int) -> dict | None:
    """获取任务详情"""
    with get_db() as conn:
        row = conn.execute("SELECT * FROM rejection_checks WHERE id = ?", (task_id,)).fetchone()
        if not row:
            return None
        return _row_to_dict(row)


def list_tasks(project_id: int | None = None, limit: int = 20) -> list[dict]:
    """列出任务"""
    with get_db() as conn:
        if project_id:
            rows = conn.execute(
                "SELECT * FROM rejection_checks WHERE project_id = ? ORDER BY id DESC LIMIT ?",
                (project_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM rejection_checks ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [_row_to_dict(r) for r in rows]


def delete_tasks(task_ids: list[int]) -> dict:
    """批量删除任务（硬删除 — DELETE FROM）。

    BUG-FIX 2026-10-15（D1）：docstring 此前写「软删除，标记为deleted」，
    但实现是 ``DELETE FROM rejection_checks``（硬删除），且表中无 ``deleted``
    状态列。修正 docstring 以匹配真实行为。

    设计约束：只允许删除未完成的任务（pending / running），已完成的检查
    结果不可删（防止误删审计结论）。批量中若混有已完成任务则整批拒绝。
    """
    with get_db() as conn:
        if not task_ids:
            return {"success": 0, "failed": 0, "message": "没有指定任务"}

        # 检查任务是否已完成（防止删除已完成的任务）
        # BUG-FIX-56: 修正SQL占位符生成，原 ",".join("?",) * len(task_ids) 生成 "??" 而非 "?,?"
        placeholders = ",".join(["?"] * len(task_ids))
        cursor = conn.execute(
            """SELECT id, status FROM rejection_checks 
               WHERE id IN ({}) AND status NOT IN ('completed', 'failed')""".format(
                placeholders
            ),
            task_ids,
        )
        incomplete_tasks = [r["id"] for r in cursor.fetchall()]

        if len(incomplete_tasks) != len(task_ids):
            return {"success": 0, "failed": len(task_ids), "message": "只能删除未完成的任务"}

        # 执行删除
        count = conn.execute(
            "DELETE FROM rejection_checks WHERE id IN ({})".format(placeholders),
            task_ids,
        ).rowcount

        return {"success": count, "failed": len(task_ids) - count}


def mark_all_false_positive(task_id: int, item_ids: list[str]) -> list[str]:
    """将指定任务中的所有项目标记为误报（一次性标记）"""
    # BUG-FIX 2026-10-15（D6 · P1 TOCTOU 竞态）：原实现先 get_task()（开独立连接读
    # false_positives），再在另一连接里 UPDATE。两个连接之间若有并发
    # mark_false_positive 调用，后者的修改会被前者基于旧值的覆盖抹掉。
    # 现将读-改-写收进同一事务：在 UPDATE 的同一连接内先 SELECT 当前值。
    with get_db() as conn:
        row = conn.execute(
            "SELECT false_positives FROM rejection_checks WHERE id = ?",
            (task_id,),
        ).fetchone()
        if not row:
            raise ValueError("任务不存在")

        fps = json.loads(row["false_positives"] or "[]")
        fps_set = set(fps)
        for item_id in item_ids:
            fps_set.add(item_id)

        conn.execute(
            "UPDATE rejection_checks SET false_positives = ?, updated_at = datetime('now', 'localtime') WHERE id = ?",
            (json.dumps(list(fps_set), ensure_ascii=False), task_id),
        )

    return list(fps_set)


def get_tasks_by_project(project_id: int, limit: int = 100) -> list[dict]:
    """获取项目的废标检查任务列表"""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM rejection_checks WHERE project_id = ? ORDER BY id DESC LIMIT ?",
            (project_id, limit),
        ).fetchall()
        return [_row_to_dict(r) for r in rows]


def mark_false_positive(task_id: int, item_id: str, is_false_positive: bool = True) -> list[str]:
    """标记/取消标记误报"""
    with get_db() as conn:
        row = conn.execute(
            "SELECT false_positives FROM rejection_checks WHERE id = ?",
            (task_id,),
        ).fetchone()
        if not row:
            raise ValueError("任务不存在")

        fps = json.loads(row["false_positives"] or "[]")
        if is_false_positive:
            if item_id not in fps:
                fps.append(item_id)
        else:
            if item_id in fps:
                fps.remove(item_id)

        conn.execute(
            "UPDATE rejection_checks SET false_positives = ?, updated_at = datetime('now', 'localtime') WHERE id = ?",
            (json.dumps(fps, ensure_ascii=False), task_id),
        )
        return fps


# ========== 报告调度管理 ==========


def create_report_schedule(
    task_id: int, schedule_type: str, schedule_time: str | None = None, enabled: bool = True
) -> int:
    """创建报告定时调度任务

    BUG-FIX 2026-09-18（中）：`enabled` 形参此前**完全没被使用** —— INSERT 语句
    里根本没有 enabled 列，取值全靠表定义 `enabled INTEGER NOT NULL DEFAULT 1`。
    后果：调用方传 `enabled=False` 建出来的调度**依然处于启用状态**，
    「先建好、稍后再启用」的用法彻底失效，等同于把启用状态硬编码成 True。
    现按传入值写入。

    注（BUG-FIX 2026-09-18）：本表此前**没有任何执行器** —— 只有 CRUD，
    全库无读取 schedule_type/schedule_time 并触发报告生成的代码，定时报告
    只落库不执行（用户配好了每日报告却永远收不到产物，也没有任何报错）。
    执行器现已补在 `services/report_schedule_runner.py`：由 beat 任务
    `app.celery_app.run_scheduled_reports` 每小时驱动，生成结果回写本表的
    `last_run_at` / `run_count` / `last_error` 三列（用 `run_count` 判
    「本周期是否已生成」，不依赖进程内状态，多 worker 也不会重复生成）。
    """
    with get_db() as conn:
        cursor = conn.execute(
            """INSERT INTO rejection_report_schedules 
               (task_id, schedule_type, schedule_time, enabled, created_at, updated_at)
               VALUES (?, ?, ?, ?, datetime('now', 'localtime'), datetime('now', 'localtime'))""",
            (task_id, schedule_type, schedule_time, 1 if enabled else 0),
        )
        return cursor.lastrowid


def update_report_schedule_status(schedule_id: int, enabled: bool) -> bool:
    """更新定时调度任务的启用状态"""
    with get_db() as conn:
        cursor = conn.execute(
            "UPDATE rejection_report_schedules SET enabled = ?, updated_at = datetime('now', 'localtime') WHERE id = ?",
            (enabled, schedule_id),
        )
        return cursor.rowcount > 0


def delete_report_schedule(schedule_id: int) -> bool:
    """删除定时调度任务"""
    with get_db() as conn:
        cursor = conn.execute("DELETE FROM rejection_report_schedules WHERE id = ?", (schedule_id,))
        return cursor.rowcount > 0


def get_schedule_task_id(schedule_id: int) -> int | None:
    """反查调度所属任务 ID（路由层据此做项目属权校验）。"""
    with get_db() as conn:
        row = conn.execute(
            "SELECT task_id FROM rejection_report_schedules WHERE id = ?",
            (schedule_id,),
        ).fetchone()
        return row["task_id"] if row else None


def get_report_schedules_by_task(task_id: int) -> list[dict]:
    """获取某任务的定时调度列表"""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM rejection_report_schedules WHERE task_id = ? ORDER BY id DESC",
            (task_id,),
        ).fetchall()
        return [_row_to_dict_schedule(r) for r in rows]


def get_all_report_schedules(limit: int = 100) -> list[dict]:
    """获取所有定时调度任务"""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM rejection_report_schedules ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [_row_to_dict_schedule(r) for r in rows]


def _row_to_dict_schedule(row) -> dict:
    d = dict(row)
    return d


def _row_to_dict(row) -> dict:
    d = dict(row)
    for k in ("bid_documents", "check_items", "enabled_dimensions", "results", "false_positives"):
        if d.get(k):
            try:
                d[k] = json.loads(d[k])
            except json.JSONDecodeError:
                d[k] = [] if k != "results" else {}
        else:
            d[k] = [] if k != "results" else {}
    return d


# ========== 废标条款提取 ==========


async def extract_rejection_items(provider: Any, tender_text: str) -> list[dict]:
    """从招标文件中提取废标条款（滚动分段）"""
    if not tender_text or not tender_text.strip():
        return _default_rejection_items()
    logger.info("[废标检查] 提取开始: tender_len=%d", len(tender_text))
    sys_prompt = get_prompt("REJECTION_EXTRACT_SYSTEM")
    logger.info("[废标检查] 提取 提示词加载: %d字", len(sys_prompt))
    chunks = split_into_chunks(tender_text)
    all_items: list[dict] = []

    for idx, chunk in enumerate(chunks):
        # SECURITY-FIX 2026-10-06：chunk 为招标文件原文（用户/外部内容），
        # 手动拼入 user_prompt 前做元标记中和，防御提示词注入。
        safe_chunk = sanitize_prompt_value(chunk)
        user_prompt = (
            f"以下是招标文件第 {idx + 1}/{len(chunks)} 段：\n\n{safe_chunk}\n\n请提取本段中的废标条款。"
        )
        messages = [
            AIMessage(role="system", content=sys_prompt),
            AIMessage(role="user", content=user_prompt),
        ]
        try:
            result = await collect_json_response(provider, messages, max_retries=2)
            if isinstance(result, list):
                all_items.extend(result)
        except Exception:
            logger.warning("废标项AI提取失败", exc_info=True)
            continue

    # 去重（按 description 相似度）
    deduped = _dedup_items(all_items)

    # 全局合稿：若条目较多，再调用 AI 合并
    if len(deduped) > 30:
        merged = await _merge_rejection_items(provider, deduped)
        # 防御：若模型返回 dict 而非 list，保持原列表，避免下游 enumerate 在 dict 上崩溃
        if isinstance(merged, list):
            deduped = merged

    if not deduped:
        return _default_rejection_items()

    # 确保每条有 id
    for i, item in enumerate(deduped, 1):
        if not item.get("id"):
            item["id"] = f"r{i:03d}"
        item.setdefault("category", "废标")
        item.setdefault("source", "")

    return deduped


def _default_rejection_items() -> list[dict]:
    """默认废标条款（兜底）"""
    return [
        {
            "id": "r001",
            "description": "投标文件未按招标文件要求加盖单位公章和法定代表人签字",
            "source": "通用",
            "category": "废标",
        },
        {
            "id": "r002",
            "description": "投标文件未按招标文件规定的格式、内容填写",
            "source": "通用",
            "category": "废标",
        },
        {
            "id": "r003",
            "description": "投标报价超过招标控制价（最高限价）",
            "source": "通用",
            "category": "废标",
        },
        {"id": "r004", "description": "工期超过招标文件要求", "source": "通用", "category": "废标"},
        {
            "id": "r005",
            "description": "质量标准低于招标文件要求",
            "source": "通用",
            "category": "废标",
        },
    ]


def _dedup_items(items: list[dict]) -> list[dict]:
    """简单去重：基于 description 字符串相似度"""
    if not items:
        return []
    result = []
    seen_descs = []
    for item in items:
        desc = item.get("description", "")
        if not desc:
            continue
        is_dup = False
        for seen in seen_descs:
            if _similarity(desc, seen) > 0.8:
                is_dup = True
                break
        if not is_dup:
            result.append(item)
            seen_descs.append(desc)
    return result


def _similarity(a: str, b: str) -> float:
    """改进的文本相似度：融合序列匹配与字符集，更适合中文"""
    if not a or not b:
        return 0
    a, b = a.strip(), b.strip()
    if a == b:
        return 1.0
    if a in b or b in a:
        return len(min(a, b, key=len)) / len(max(a, b, key=len))
    # 字符集 Jaccard（轻量兜底）
    set_a, set_b = set(a), set(b)
    jaccard = len(set_a & set_b) / len(set_a | set_b) if set_a | set_b else 0

    # 序列相似度：滑动窗口 bigram 重叠
    def _bigrams(s: str) -> set:
        return {s[i : i + 2] for i in range(len(s) - 1) if len(s) >= 2}

    a_big, b_big = _bigrams(a), _bigrams(b)
    if not a_big or not b_big:
        return jaccard
    bigram_overlap = len(a_big & b_big) / max(len(a_big), len(b_big))
    return bigram_overlap * 0.7 + jaccard * 0.3


async def _merge_rejection_items(provider: Any, items: list[dict]) -> list[dict]:
    """调用 AI 合并去重"""
    sys_prompt = get_prompt("REJECTION_MERGE_SYSTEM")
    user_prompt = f"请合并去重以下废标条款：\n\n{json.dumps(items, ensure_ascii=False, indent=2)}"
    messages = [
        AIMessage(role="system", content=sys_prompt),
        AIMessage(role="user", content=user_prompt),
    ]
    try:
        return await collect_json_response(provider, messages, max_retries=2)
    except Exception:
        return items


# ========== 三类检查 ==========


# ========== 增强 4.3: 假阳性学习 ==========


def get_false_positive_patterns(project_id: int | None = None, limit: int = 20) -> list[dict]:
    """获取已知假阳性模式，用于优化后续检查

    从历史任务中提取被标记为假阳性的条目，汇总为模式列表。

    Args:
        project_id: 可选，按项目筛选
        limit: 最大返回条数

    Returns:
        list[dict]: [{"description": "...", "pattern": "..."}, ...]
    """
    from ..db import get_db  # BUG-FIX-55: 修正相对导入路径，services/ 下无 db.py，需从上级 app/ 导入

    patterns = []
    with get_db() as conn:
        if project_id:
            rows = conn.execute(
                """SELECT false_positives, check_items, results
                   FROM rejection_checks
                   WHERE project_id = ? AND false_positives != '[]'
                   ORDER BY id DESC LIMIT ?""",
                (project_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT false_positives, check_items, results
                   FROM rejection_checks
                   WHERE false_positives != '[]'
                   ORDER BY id DESC LIMIT ?""",
                (limit,),
            ).fetchall()

        for row in rows:
            fps = json.loads(row["false_positives"] or "[]")
            if not fps:
                continue
            results = json.loads(row["results"] or "{}")
            # 从结果中提取假阳性条目的描述
            for dim in ("rejection", "spelling", "logic"):
                items = results.get(dim, [])
                for item in items:
                    if item.get("id") in fps:
                        patterns.append(
                            {
                                "description": item.get("description", ""),
                                "quote": item.get("quote", "") or item.get("original", ""),
                                "category": item.get("category", dim),
                            }
                        )

    # 去重（基于描述文本相似度）
    if patterns:
        unique = [patterns[0]]
        for p in patterns[1:]:
            desc = p.get("description", "")
            if desc and not any(_similarity(desc, u.get("description", "")) > 0.8 for u in unique):
                unique.append(p)
        # 限制数量
        return unique[:limit]
    return []


# ===== ENHANCE 2026-09-14：废标项三轮流程（移植自易标 rejectionCheckTask.cjs） =====
#
# 原实现是"单轮直出 JSON"：把检查项清单 + 投标文件分段一起塞给 AI，要求直接吐 JSON。
# 两个问题：
#   1) 模型要同时做"推理"和"严格格式化"，格式压力挤占判断质量；
#   2) 没有范围收敛——"未加盖公章""未密封""未现场递交"这类**纸质/线下事项**
#      电子文件根本无从判断，AI 却会稳定地报出来，形成大量误报；
#      同理，解析后的 Markdown 里图片/扫描件正文本就不可见，AI 会大量误报"材料缺失"。
#
# 三轮流程把"推理"和"格式化"拆开，并在第一轮先收敛范围：
#   第一轮 范围分析（自由文本，不要求 JSON）→ 排除不可判断项、给出重点章节
#   第二轮 逐项检查（结构化 Markdown 初稿，不要求 JSON）→ 逐段产出初步风险
#   第三轮 定稿（严格 JSON）→ 去重合并、删除不合格条目
#
# 任一轮失败/返回空 → 返回 None，调用方降级到单轮检查（原实现原样保留），保证不倒退。

REJECTION_THREE_ROUND_ENV = "REJECTION_THREE_ROUND"
_DRAFT_MAX_CHARS = 20000  # 第二轮初稿送入第三轮前的上限
_ANALYSIS_TIMEOUT = 90  # 第一轮超时（只喂目录线索，输入很小）
_DRAFT_TIMEOUT = 180  # 第二轮每段超时（单段约 2 万字）
_FINAL_TIMEOUT = 180  # 第三轮超时


def _three_round_enabled() -> bool:
    """三轮流程开关：环境变量 REJECTION_THREE_ROUND 设为 0/false/off/no 可回退单轮。"""
    raw = os.getenv(REJECTION_THREE_ROUND_ENV, "1").strip().lower()
    return raw not in {"0", "false", "off", "no"}


def _assign_item_codes(check_items: list[dict]) -> list[dict]:
    """确保每个检查项都有唯一、稳定的 id，供 AI 回填与宿主校验。

    用户自建的检查项可能没有 id，也可能出现重复 id；重复 id 会让第三轮的
    `check_item_id` 校验形同虚设（AI 填哪个都"命中"），因此这里统一去重。

    实现必须**两遍扫描**：先登记所有"已有且不重复"的 id 作为保留集合，再给缺 id /
    重复 id 的项分配自增编号。否则给前面无 id 项生成的自增编号会抢走后面某一项
    本来合法的 id（例如第 1 项无 id 被编成 r001，第 2 项原本就叫 r001 就被迫改名），
    导致 AI 看到的编号与用户在前端看到的检查项编号对不上。
    """
    items = [dict(item or {}) for item in (check_items or [])]

    reserved: set[str] = set()
    for entry in items:
        item_id = str(entry.get("id") or "").strip()
        if item_id and item_id not in reserved:
            reserved.add(item_id)

    result: list[dict] = []
    used: set[str] = set()
    counter = 0
    for entry in items:
        item_id = str(entry.get("id") or "").strip()
        if not item_id or item_id in used:
            counter += 1
            item_id = f"r{counter:03d}"
            while item_id in used or item_id in reserved:
                counter += 1
                item_id = f"r{counter:03d}"
        entry["id"] = item_id
        used.add(item_id)
        result.append(entry)
    return result


def _extract_outline_hint(text: str, max_lines: int = 150, fallback_chars: int = 3000) -> str:
    """抽取投标文件的目录/章节结构线索。

    第一轮范围分析只需要知道"文档里有哪些章节"，不需要全文——只喂结构可以把
    第一轮的输入从数万字压到几千字，成本与延迟都显著下降，而判断质量不受影响
    （范围分析依赖的是结构信息，不是正文细节）。

    结构线索不明显时（例如解析结果没有编号标题）退回文首片段兜底。
    """
    if not text:
        return ""
    try:
        from .duplicate import extract_toc  # 复用既有目录抽取，避免重复实现 6 组正则

        titles = extract_toc(text)
    except Exception:
        logger.debug("目录线索抽取失败，回退文首片段", exc_info=True)
        titles = []
    if len(titles) >= 5:
        # 去重但保持顺序
        seen: set[str] = set()
        unique: list[str] = []
        for title in titles:
            if title not in seen:
                seen.add(title)
                unique.append(title)
        return "\n".join(unique[:max_lines])
    return text[:fallback_chars]


_SEVERITY_ALIASES = {
    "critical": "critical",
    "high": "critical",
    "fatal": "critical",
    "确定废标": "critical",
    "warning": "warning",
    "warn": "warning",
    "medium": "warning",
    "可能废标": "warning",
    "info": "info",
    "low": "info",
    "note": "info",
    "提示": "info",
}


def _normalize_severity(value: Any) -> str:
    """归一化风险级别到前端约定值 critical / warning / info。

    第三轮提示词已要求输出这三个值，但模型仍可能给出 high/medium/low
    （易标用的是这套），归一化可避免前端 Tag 取不到颜色配置而显示空白。
    """
    return _SEVERITY_ALIASES.get(str(value or "").strip().lower(), "warning")


def verify_rejection_evidence(issue: dict, original_text: str) -> dict:
    """核验废标项证据摘录能否在投标文件原文中定位。

    ENHANCE 2026-09-14：补上易标**没有**的原文定位核验。易标的错别字检查有
    `findVerifiedTypoPosition` 做 offset 校验，废标项检查却只有字段非空校验，
    导致 `bidEvidence` 只能是 AI 自述、人工复核无从下手。

    与错别字检查的关键差异：废标项存在"材料缺失"类风险，本就**没有**可摘录的原文，
    因此定位失败不丢弃，只置 `validated=False` 提示人工重点复核。

    返回补充了 validated / evidence_kind / position / excerpt / line_hint 的副本。
    """
    issue = dict(issue or {})
    quote = str(
        issue.get("quote") or issue.get("bidEvidence") or issue.get("evidence") or ""
    ).strip()
    if not quote:
        # 无原文摘录：多为"材料缺失"类风险，属正常，不判为核验失败
        issue.setdefault("validated", None)
        issue["evidence_kind"] = "structural"
        return issue

    position = locate_text_span(quote, original_text)
    issue["evidence_kind"] = "quote"
    if position < 0:
        issue["validated"] = False
        return issue

    issue["validated"] = True
    issue["position"] = position
    # 摘录只取证据开头 + 上下文，避免把整段 quote 原样复制一遍
    issue["excerpt"] = build_verified_excerpt(original_text, position, min(len(quote), 30))
    issue["line_hint"] = build_line_hint(original_text, position)
    return issue


# ===== ENHANCE 2026-09-15：跨分块「疑似缺失」状态机 =====
# 移植自易标 `rejectionCheckTask.cjs` 滚动 patch 的 pending / resolved 语义，
# 但落点不同：易标有可跨块累积的 patch 载体（submittedEvidence / pendingRisks /
# resolvedRisks），本平台的分块流程是「各块独立检查 + 最后一次定稿」，没有载体，
# 因此把状态机落在**宿主侧、全文维度**上。

MISSING_STATE_PENDING = "pending"
MISSING_STATE_RESOLVED = "resolved"
MISSING_STATE_CONFIRMED = "confirmed"

#: 材料/文件类名词后缀 —— 命中才认为抽到的是"本应在文档里出现的东西"
_MATERIAL_SUFFIXES = (
    "证", "证书", "执照", "书", "表", "单", "报告", "记录", "计划", "方案", "许可",
    "资质", "文件", "材料", "清单", "附件", "协议", "合同", "说明书", "图纸",
    "封面", "目录", "扫描件", "承诺函", "保函", "授权", "设计", "措施", "制度",
)
#: 抽取关键词时剔除的通用词 —— 它们在全文里必然出现，不能当作"该材料已提交"的证据
_MATERIAL_STOPWORDS = {
    "投标文件", "招标文件", "本项目", "投标人", "本工程", "以下", "上述", "相关",
    "有关", "内容", "部分", "情况", "要求", "说明", "工作", "问题", "项目", "材料",
    "证明", "文件", "附件", "文本", "章节", "段落", "全文",
}
#: 被引号包裹的片段最可能是 AI 明确指认的材料名
_MATERIAL_QUOTE_RE = re.compile(r"[「『“\”']([^」』”\”']{2,40})[」』”\”']")
_MATERIAL_TOKEN_RE = re.compile(r"[\u4e00-\u9fffA-Za-z0-9]{3,30}")
#: 普通分词候选里出现这些标记，说明该片段是「问题描述」而不是「材料名称」——
#: 例如「疑似缺少部分佐证材料」以"材料"结尾会被后缀规则误判，必须靠标记拦截
_MATERIAL_CLAIM_MARKERS = (
    "疑似", "缺少", "缺失", "未见", "未找到", "没有", "未提供",
    "不足", "可能", "大概", "应提供", "需提供", "部分", "相关", "上述", "如下",
)


def extract_claim_keywords(issue: dict, limit: int = 6) -> list[str]:
    """从「疑似缺失」条目中抽取「本应出现在文档里」的关键词。

    两条来源（按可靠性排序）：
      1. 引号包裹的片段（AI 常写「未找到『安全生产许可证』」）；
      2. 按标点切分 description / location 后，以材料类后缀结尾的片段。
    抽不出关键词时返回空列表 —— 调用方据此**不做任何排除**（宁可不判，不可误杀）。
    """
    issue = issue or {}
    candidates: list[str] = []
    for field in ("description", "location", "suggestion"):
        text = str(issue.get(field) or "")
        if not text:
            continue
        for match in _MATERIAL_QUOTE_RE.finditer(text):
            inner = match.group(1).strip("，。；、,.;: ")
            if len(inner) >= 2:
                candidates.append(inner)
        # 分词候选必须先过「描述性片段」过滤：像"疑似缺少部分佐证材料"这种
        # 以材料后缀结尾的问题描述，不能被当成材料名拿去全文检索
        candidates.extend(
            token
            for token in _MATERIAL_TOKEN_RE.findall(text)
            if not any(marker in token for marker in _MATERIAL_CLAIM_MARKERS)
        )

    keywords: list[str] = []
    for token in candidates:
        token = token.strip()
        if len(token) < 3 or token in _MATERIAL_STOPWORDS:
            continue
        # 必须像"材料/文件/制度名"，否则普通名词会把误判概率放大
        if not any(token.endswith(suffix) for suffix in _MATERIAL_SUFFIXES):
            continue
        if any(token in existing or existing in token for existing in keywords):
            continue
        keywords.append(token)
        if len(keywords) >= limit:
            break
    return keywords


def resolve_missing_material_claims(issues: list[dict], full_text: str) -> dict:
    """对「疑似缺失」类条目做**跨全文**复核，回填 `missing_state`。

    要解决的问题：废标项检查是分块进行的（`CHUNK_SIZE = 20000`）。某一块里没看到
    「安全生产许可证」，模型就会按"材料缺失"上报 —— 而该材料完全可能出现在文档的
    其它分块里。这类跨块误报在长文档中占比很高，且人工逐条核查成本极贵。

    状态判定：
      - 条目声称的材料在**整份**投标文件中存在结构性线索 → `resolved`
        （保留条目但标记为"疑似分块误判、已排除"，并补上命中的行号与摘录）
      - 全文都找不到 → `confirmed`（缺失判断成立，维持原风险等级）
      - 抽不出可核验的关键词 → `pending`（交人工，**不做排除**）

    Returns:
        {"stats": {"pending": n, "resolved": n, "confirmed": n}}
    """
    text = str(full_text or "")
    compact_text, positions = _build_compact_index_map(text)
    stats = {MISSING_STATE_PENDING: 0, MISSING_STATE_RESOLVED: 0, MISSING_STATE_CONFIRMED: 0}

    for issue in issues or []:
        if not isinstance(issue, dict):
            continue
        # 只有"没有原文摘录"的条目属于疑似缺失类；有摘录的已由证据核验处理，
        # 这里重复参与只会把已验证通过的条目又改一遍。
        if issue.get("evidence_kind") != "structural":
            continue

        keywords = extract_claim_keywords(issue)
        if not keywords:
            issue["missing_state"] = MISSING_STATE_PENDING
            issue["missing_state_reason"] = "未能抽出可核验的材料关键词，保持待人工确认"
            stats[MISSING_STATE_PENDING] += 1
            continue

        hits: list[dict] = []
        for keyword in keywords:
            compact_keyword = re.sub(r"\s+", "", keyword)
            if not compact_keyword:
                continue
            index = compact_text.find(compact_keyword)
            if index >= 0:
                hits.append({"keyword": keyword, "position": positions[index]})

        if hits:
            first = hits[0]
            issue["missing_state"] = MISSING_STATE_RESOLVED
            issue["missing_state_hits"] = hits[:5]
            issue["missing_state_reason"] = (
                "全文其它位置存在该材料的结构性线索（"
                + "、".join(hit["keyword"] for hit in hits[:3])
                + "），疑似分块误判，已标记排除，建议人工确认"
            )
            issue["line_hint"] = build_line_hint(text, first["position"])
            issue["excerpt"] = build_verified_excerpt(
                text, first["position"], len(first["keyword"])
            )
            stats[MISSING_STATE_RESOLVED] += 1
        else:
            issue["missing_state"] = MISSING_STATE_CONFIRMED
            issue["missing_state_reason"] = "全文均未找到该材料的结构性线索，缺失判断成立"
            stats[MISSING_STATE_CONFIRMED] += 1

    return {"stats": stats}


def _coerce_issue_list(result: Any) -> list[dict]:
    """把 AI 返回归一化为 issue 列表。

    第三轮提示词要求返回数组，但模型常包一层 `{"findings": [...]}`（易标用的就是这个
    包装格式），因此两种都要接受；返回 None 视为空列表。
    """
    if isinstance(result, list):
        return [item for item in result if isinstance(item, dict)]
    if isinstance(result, dict):
        for key in ("findings", "issues", "items", "results", "data"):
            value = result.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        # 单条记录（带标题类字段）也接受
        if any(result.get(k) for k in ("description", "title", "check_item_id")):
            return [result]
    return []


def _dedup_final_issues(issues: list[dict], threshold: float = 0.85) -> list[dict]:
    """机械去重兜底：AI 已在第三轮做过语义去重，这里只处理漏网的重复。

    判据：同一检查项编号 + 描述相似度 ≥ threshold 视为同一条。
    合并时优先保留证据已核验的那条，并补齐缺失的建议字段。
    """
    if not issues:
        return []
    kept: list[dict] = []
    for issue in issues:
        desc = str(issue.get("description") or issue.get("title") or "").strip()
        code = str(issue.get("check_item_id") or "").strip()
        merged = False
        for index, existing in enumerate(kept):
            existing_code = str(existing.get("check_item_id") or "").strip()
            # 两侧都有编号且不一致 → 不同条款，不合并
            if code and existing_code and code != existing_code:
                continue
            existing_desc = str(existing.get("description") or existing.get("title") or "")
            if desc and _similarity(desc, existing_desc) >= threshold:
                target, source = existing, issue
                if bool(issue.get("validated")) and not bool(existing.get("validated")):
                    target, source = issue, existing
                    kept[index] = target
                if not target.get("suggestion") and source.get("suggestion"):
                    target["suggestion"] = source["suggestion"]
                target["merged_count"] = int(target.get("merged_count", 1)) + int(
                    source.get("merged_count", 1)
                )
                merged = True
                break
        if not merged:
            issue["merged_count"] = int(issue.get("merged_count", 1))
            kept.append(issue)
    return kept


def _finalize_rejection_issues(
    issues: list[dict],
    bid_doc: dict,
    coded_items: list[dict],
    text: str,
) -> list[dict]:
    """废标项结果统一收尾：补全字段 → 编号校验 → 证据核验 → 去重。

    单轮降级路径与三轮流程共用这一套收尾，保证两条路径产出的结构完全一致。
    """
    valid_ids = {str(item.get("id") or "").strip() for item in coded_items}
    prepared: list[dict] = []
    for index, issue in enumerate(issues or [], 1):
        if not isinstance(issue, dict):
            continue
        entry = dict(issue)
        entry.setdefault("id", f"issue-{index:03d}")
        entry["bid_doc_id"] = bid_doc.get("id")
        entry["bid_doc_name"] = bid_doc.get("name", "")
        entry.setdefault("category", "废标")
        entry["severity"] = _normalize_severity(entry.get("severity"))
        code = str(entry.get("check_item_id") or "").strip()
        entry["code_matched"] = bool(code) and code in valid_ids
        prepared.append(verify_rejection_evidence(entry, text))

    # ENHANCE 2026-09-15：跨分块「疑似缺失」复核。
    # 分块检查时本块看不到的材料会被报成缺失，这里用**全文**结构性线索做
    # pending → resolved / confirmed 判定，压制跨块误报。
    missing = resolve_missing_material_claims(prepared, text)
    logger.info(
        "[废标检查] 疑似缺失跨块复核: resolved=%d, confirmed=%d, pending=%d",
        missing["stats"][MISSING_STATE_RESOLVED],
        missing["stats"][MISSING_STATE_CONFIRMED],
        missing["stats"][MISSING_STATE_PENDING],
    )
    return _dedup_final_issues(prepared)


async def _run_text_round(provider: Any, prompt_key: str, user_prompt: str, timeout: int) -> str:
    """执行一轮"纯文本"AI 调用（不要求 JSON），失败返回空串。

    走 provider.chat 而非 collect_json_response：后者的作用是"拿到 JSON"，
    对第一轮的散文式分析结论与第二轮的 Markdown 初稿都不适用（会白白触发
    JSON 修复链路与重试）。
    """
    messages = [
        AIMessage(role="system", content=get_prompt(prompt_key)),
        AIMessage(role="user", content=user_prompt),
    ]
    try:
        raw = await with_timeout(
            provider.chat(messages, temperature=0.3), timeout=timeout, error_msg=f"{prompt_key} 调用超时"
        )
    except Exception:
        logger.warning("[废标检查] 文本轮调用失败: %s", prompt_key, exc_info=True)
        return ""
    if isinstance(raw, str):
        return raw.strip()
    if isinstance(raw, dict):
        return str(raw.get("content") or "").strip()
    return ""


async def _run_rejection_single_round(
    provider: Any,
    bid_doc: dict,
    coded_items: list[dict],
    fp_instruction: str,
    chunks: list[str],
) -> list[dict]:
    """单轮检查（原实现原样保留，作为三轮流程的降级路径）。"""
    sys_prompt = get_prompt("REJECTION_CHECK_SYSTEM")
    all_issues: list[dict] = []
    for idx, chunk in enumerate(chunks):
        # SECURITY-FIX 2026-10-06：chunk 为投标文件原文（用户/外部内容），
        # 手动拼入 user_prompt 前做元标记中和。
        safe_chunk = sanitize_prompt_value(chunk)
        user_prompt = f"""{fp_instruction}
检查项清单：
{json.dumps(coded_items, ensure_ascii=False, indent=2)}

投标文件（第 {idx + 1}/{len(chunks)} 段）：
{safe_chunk}

请逐条对照检查项审查本段内容。"""
        messages = [
            AIMessage(role="system", content=sys_prompt),
            AIMessage(role="user", content=user_prompt),
        ]
        try:
            result = await collect_json_response(provider, messages, max_retries=2)
            all_issues.extend(_coerce_issue_list(result))
        except Exception:
            logger.warning("废标项智能提取失败", exc_info=True)
            continue
    return all_issues


async def _run_rejection_three_rounds(
    provider: Any,
    bid_doc: dict,
    coded_items: list[dict],
    fp_instruction: str,
    chunks: list[str],
    text: str,
    progress_callback: Any = None,
) -> list[dict] | None:
    """三轮流程：范围分析 → 逐项检查初稿 → 去重定稿。

    返回 None 表示应当降级到单轮检查；返回列表（含空列表）表示三轮流程正常完成。
    """
    items_json = json.dumps(coded_items, ensure_ascii=False, indent=2)

    def _report(message: str) -> None:
        if not progress_callback:
            return
        try:
            progress_callback(message)
        except Exception:
            logger.debug("[废标检查] 进度回调失败", exc_info=True)

    # ---- 第一轮：范围分析 ----
    _report("第一轮：分析检查范围...")
    outline = _extract_outline_hint(text)
    analysis_prompt = f"""{fp_instruction}
检查项清单：
{items_json}

投标文件的目录与章节结构线索：
{outline}

请分析本次可纳入电子文件检查的范围。"""
    analysis = await _run_text_round(
        provider, "REJECTION_ANALYSIS_SYSTEM", analysis_prompt, _ANALYSIS_TIMEOUT
    )
    if not analysis:
        logger.warning("[废标检查] 第一轮范围分析为空，降级单轮")
        return None
    logger.info("[废标检查] 第一轮范围分析完成: %d 字", len(analysis))

    # ---- 第二轮：逐项检查初稿（Markdown，按段）----
    draft_parts: list[str] = []
    total_chunks = len(chunks)
    for idx, chunk in enumerate(chunks):
        _report(f"第二轮：逐项检查 {idx + 1}/{total_chunks}...")
        draft_prompt = f"""{fp_instruction}
检查项清单：
{items_json}

第一轮范围分析结论：
{analysis}

投标文件（第 {idx + 1}/{total_chunks} 段）：
{chunk}

请基于上述范围，逐项检查本段内容，按约定 Markdown 格式输出初步风险清单。"""
        part = await _run_text_round(
            provider, "REJECTION_DRAFT_SYSTEM", draft_prompt, _DRAFT_TIMEOUT
        )
        if part:
            draft_parts.append(part)
    if not draft_parts:
        logger.warning("[废标检查] 第二轮初稿全部为空，降级单轮")
        return None

    draft = "\n\n".join(draft_parts)
    if len(draft) > _DRAFT_MAX_CHARS:
        logger.warning("[废标检查] 初稿过长(%d字)，截断至 %d 字", len(draft), _DRAFT_MAX_CHARS)
        draft = draft[:_DRAFT_MAX_CHARS] + "\n...(初稿过长已截断)"

    # ---- 第三轮：去重定稿（严格 JSON）----
    _report("第三轮：去重定稿...")
    final_prompt = f"""{fp_instruction}
检查项清单：
{items_json}

第一轮范围分析结论：
{analysis}

第二轮初步风险清单：
{draft}

请按定稿规则去重合并、删除不合格条目，并输出最终 JSON 数组。"""
    messages = [
        AIMessage(role="system", content=get_prompt("REJECTION_FINAL_SYSTEM")),
        AIMessage(role="user", content=final_prompt),
    ]
    try:
        result = await collect_json_response(provider, messages, max_retries=2)
    except Exception:
        logger.warning("[废标检查] 第三轮定稿失败，降级单轮", exc_info=True)
        return None
    issues = _coerce_issue_list(result)
    logger.info("[废标检查] 三轮流程完成: findings=%d", len(issues))
    return issues


async def check_rejection_dimension(
    provider: Any,
    _tender_text: str,
    bid_doc: dict,
    check_items: list[dict],
    false_positive_patterns: list[dict] | None = None,
    progress_callback: Any = None,
) -> list[dict]:
    """废标项检查：对照检查项审查投标文件。

    ENHANCE 2026-09-14：默认走三轮流程（范围分析 → 逐项检查 → 定稿），
    任一轮失败自动降级到单轮检查；两条路径最终都经过同一套收尾
    （编号校验 + 证据原文核验 + 去重）。
    """
    logger.info(
        "[废标检查] 审查开始: doc_id=%s, doc_name=%s, items=%d",
        bid_doc.get("id"),
        bid_doc.get("name"),
        len(check_items),
    )
    coded_items = _assign_item_codes(check_items)
    text = bid_doc.get("text", "")
    chunks = split_into_chunks(text)

    # 增强 4.3: 注入假阳性模式作为负面示例
    fp_instruction = ""
    if false_positive_patterns:
        fp_instruction = f"""
已知以下条目曾被误判为废标问题（假阳性），请避免重复报告类似内容：
{json.dumps([p.get("description", "") for p in false_positive_patterns[:5]], ensure_ascii=False)}
"""

    issues: list[dict] | None = None
    if _three_round_enabled():
        try:
            issues = await _run_rejection_three_rounds(
                provider, bid_doc, coded_items, fp_instruction, chunks, text, progress_callback
            )
        except Exception:
            logger.warning("[废标检查] 三轮流程异常，降级单轮", exc_info=True)
            issues = None
    else:
        logger.info("[废标检查] 三轮流程已关闭（%s），使用单轮检查", REJECTION_THREE_ROUND_ENV)

    if issues is None:
        issues = await _run_rejection_single_round(
            provider, bid_doc, coded_items, fp_instruction, chunks
        )

    # 合并去重（AI 侧）：条目过多时再调一次 AI 合稿
    if len(issues) > 20:
        merged = await _merge_rejection_items(provider, issues)
        if isinstance(merged, list):
            issues = _coerce_issue_list(merged)

    finalized = _finalize_rejection_issues(issues, bid_doc, coded_items, text)
    verified = sum(1 for i in finalized if i.get("validated") is True)
    unverified = sum(1 for i in finalized if i.get("validated") is False)
    logger.info(
        "[废标检查] 审查完成: doc=%s, 条目=%d, 证据已核验=%d, 未能核验=%d",
        bid_doc.get("name"),
        len(finalized),
        verified,
        unverified,
    )
    return finalized


def _build_compact_index_map(text: str) -> tuple[str, list[int]]:
    """构建「去空白」文本及其到原文的索引映射。

    返回 (compact_text, positions)，其中 positions[i] 是 compact_text[i] 在原文中的下标。
    用于在 AI 复述的片段与原文存在空白/换行差异时仍能定位。
    """
    compact_chars: list[str] = []
    positions: list[int] = []
    for index, char in enumerate(text):
        if char.isspace():
            continue
        compact_chars.append(char)
        positions.append(index)
    return "".join(compact_chars), positions


def locate_text_span(needle: str, haystack: str) -> int:
    """在 haystack 中定位 needle 的起始下标；定位失败返回 -1。

    三级匹配（移植自易标 `findVerifiedTypoPosition` 并做了容错增强）：
      1. 精确子串匹配（最快，覆盖绝大多数情况）
      2. 去空白后匹配（AI 常把原文的换行/空格复述掉或补上）
      3. 忽略引号/全角半角标点差异后匹配（AI 常把 " 引号改成 ""）
    """
    needle = str(needle or "")
    haystack = str(haystack or "")
    if not needle or not haystack:
        return -1

    direct = haystack.find(needle)
    if direct >= 0:
        return direct

    # 第 2 级：去空白匹配
    compact_needle = re.sub(r"\s+", "", needle)
    if compact_needle:
        compact_haystack, positions = _build_compact_index_map(haystack)
        idx = compact_haystack.find(compact_needle)
        if idx >= 0:
            return positions[idx]

    # 第 3 级：忽略引号与常见标点差异后匹配
    def _strip_punct(value: str) -> str:
        return re.sub(r"[\s\"'""''`「」『』]+", "", value)

    stripped_needle = _strip_punct(needle)
    if stripped_needle:
        # 逐字符构建「无标点」映射（保留每个保留下来的字符在原文的下标）
        kept: list[str] = []
        kept_positions: list[int] = []
        for index, char in enumerate(haystack):
            if re.fullmatch(r"[\s\"'""''`「」『』]+", char):
                continue
            kept.append(char)
            kept_positions.append(index)
        kept_text = "".join(kept)
        idx = kept_text.find(stripped_needle)
        if idx >= 0:
            return kept_positions[idx]

    return -1


def build_verified_excerpt(text: str, position: int, span: int, context_chars: int = 8) -> str:
    """截取命中位置前后各 context_chars 个字符的摘录（去掉换行，便于报告展示）。

    对应易标 `createVerifiedTypoExcerpt`：±8 字上下文足以让人工复核时一眼定位。
    """
    if position < 0 or not text:
        return ""
    start = max(0, position - context_chars)
    end = min(len(text), position + max(span, 1) + context_chars)
    excerpt = text[start:end].replace("\r", " ").replace("\n", " ")
    return re.sub(r"\s{2,}", " ", excerpt).strip()


def build_line_hint(text: str, position: int) -> str:
    """根据字符偏移给出「第 N 行」提示（对应易标 `createLineLocationHint`）"""
    if position < 0 or not text:
        return ""
    line_no = text.count("\n", 0, position) + 1
    return f"第 {line_no} 行"


def verify_spelling_error(err: dict, original_text: str) -> dict:
    """校验错别字条目的原文可定位性，并补齐 position / excerpt / line_hint。

    ENHANCE 2026-09-14：移植自易标 `rejectionCheckTask.cjs::findVerifiedTypoPosition`。

    原实现只做 `original in text` 的布尔判断并写个 validated 标记，问题在于：
      - AI 幻觉出的错别字（原文根本没有）会照样进入报告，人工复核时逐条找不到，
        最终导致整份错别字检查结果不被信任（"狼来了"效应）；
      - 没有位置信息，长文档里靠"第3段第2句"这种 AI 自述定位基本靠猜。

    现在：定位不到原文的条目会被显式标记 `verified=False` + `discard_reason`，
    由调用方决定丢弃；定位到的条目补上字符偏移、±8 字摘录和行号。
    """
    err = dict(err or {})
    needle = str(err.get("original") or err.get("wrong") or err.get("wrongText") or "").strip()
    if not needle:
        err["verified"] = False
        err["discard_reason"] = "missing_original"
        return err

    position = locate_text_span(needle, original_text)
    if position < 0:
        # 回退：AI 常把出错片段写在 quote 里，先在 quote 内定位，再把 quote 落到原文。
        #
        # NOTE 2026-09-14：这一分支当前**不可达**，保留为防御性代码。
        # 论证：`locate_text_span` 的三级匹配（精确 / 去空白 / 去引号）对 needle 都是单调的——
        # 若 needle 是 quote 的子串，则第 k 级变换后的 needle 必然是第 k 级变换后的 quote 的子串；
        # 又因 quote 能在 original_text 中命中某一级 j，故 needle 必能在 original_text 命中第 j 级，
        # 与"position < 0"矛盾。若日后新增非单调的匹配级（如模糊编辑距离），此分支才会生效。
        quote = str(err.get("quote") or "").strip()
        if quote:
            inner = locate_text_span(needle, quote)
            if inner >= 0:
                quote_position = locate_text_span(quote, original_text)
                if quote_position >= 0:
                    position = quote_position + inner
    if position < 0:
        err["verified"] = False
        err["discard_reason"] = "not_found_in_source"
        return err

    err["verified"] = True
    err["position"] = position
    err["excerpt"] = build_verified_excerpt(original_text, position, len(needle))
    err["line_hint"] = build_line_hint(original_text, position)
    return err


async def check_spelling_dimension(
    provider: Any,
    bid_doc: dict,
    false_positive_patterns: list[dict] | None = None,
) -> list[dict]:
    """错别字检查：分段 AI 识别 + 原文定位校验

    ENHANCE 2026-09-14：新增定位校验与去重。校验不通过的条目（AI 幻觉）被丢弃，
    丢弃数量写入日志，便于评估模型质量；同一位置同一错别字只保留一条。
    """
    sys_prompt = get_prompt("SPELLING_CHECK_SYSTEM")
    text = bid_doc.get("text", "")
    paragraphs = split_paragraphs(text)
    all_errors: list[dict] = []
    discarded_count = 0
    seen_keys: set[tuple[str, int]] = set()

    # 增强 4.3: 注入假阳性模式
    fp_instruction = ""
    if false_positive_patterns:
        fp_instruction = f"""
已知以下条目曾被误判为错别字（假阳性），请避免重复报告类似内容：
{json.dumps([p.get("description", "") for p in false_positive_patterns[:5]], ensure_ascii=False)}
"""

    # 合并段落成块，避免过多调用
    batch_size = 10
    for i in range(0, len(paragraphs), batch_size):
        batch = paragraphs[i : i + batch_size]
        batch_text = "\n\n".join(f"[段落 {i + j + 1}] {p}" for j, p in enumerate(batch))
        # SECURITY-FIX 2026-10-06：batch_text 为投标文件原文，做元标记中和。
        safe_batch = sanitize_prompt_value(batch_text)
        user_prompt = (
            f"{fp_instruction}"
            f"文档名：{bid_doc.get('name', '')}\n\n请检查以下文本中的错别字：\n\n{safe_batch}"
        )
        messages = [
            AIMessage(role="system", content=sys_prompt),
            AIMessage(role="user", content=user_prompt),
        ]
        try:
            # BUG-FIX 2026-09-18（高）：原实现 `result = await collect_json_response(...)`
            # 后只接受裸 list（`if isinstance(result, list)`）。模型经常返回
            # `{"findings": [...]}` / `{"errors": [...]}` 等包装格式，此时整批结果被
            # 丢弃 → 错别字维度静默返回 0 条 → 界面显示「未发现错别字」，误报通过。
            # 复用废标维度已在使用的 _coerce_issue_list（兼容 5 种包装格式）。
            result = _coerce_issue_list(
                await collect_json_response(provider, messages, max_retries=2)
            )
            if isinstance(result, list):
                for err in result:
                    err.setdefault("bid_doc_id", bid_doc.get("id"))
                    err.setdefault("bid_doc_name", bid_doc.get("name", ""))
                    err.setdefault("category", "错别字")
                    verified = verify_spelling_error(err, batch_text)
                    if not verified.get("verified"):
                        discarded_count += 1
                        logger.debug(
                            "[废标检查] 丢弃无法定位的错别字条目: original=%r reason=%s",
                            err.get("original"),
                            verified.get("discard_reason"),
                        )
                        continue
                    dedupe_key = (
                        str(verified.get("original", "")),
                        int(verified.get("position", -1)),
                    )
                    if dedupe_key in seen_keys:
                        continue
                    seen_keys.add(dedupe_key)
                    all_errors.append(verified)
        except Exception:
            logger.warning("错别字检查AI提取失败", exc_info=True)
            continue

    # BUG-FIX 2026-09-15：`verify_spelling_error` 是在**批次文本**上定位的，
    # 记录下来的 position/excerpt/line_hint 都是批次内偏移 ——
    #   ① 报告里给出的"第 N 行"会指向错误的位置；
    #   ② 一键修正没法拿它当锚点（下标在整份文档里无意义）。
    # 这里统一按整份文档重新锚定一次；定位失败的条目**保留原批次信息**（不丢弃，
    # 因为它在批次里确实可核验，只是整文定位不到，可能被清洗过）。
    reanchored = 0
    anchored: list[dict] = []
    anchored_keys: set[tuple[str, int]] = set()
    for err in all_errors:
        needle = str(err.get("original") or "").strip()
        position = locate_text_span(needle, text) if needle else -1
        if position >= 0:
            err["position"] = position
            err["excerpt"] = build_verified_excerpt(text, position, len(needle))
            err["line_hint"] = build_line_hint(text, position)
            err["position_scope"] = "document"
            reanchored += 1
        else:
            err["position_scope"] = "batch"
        key = (str(err.get("original") or ""), int(err.get("position", -1)))
        if key in anchored_keys:
            continue
        anchored_keys.add(key)
        anchored.append(err)

    if discarded_count or reanchored:
        logger.info(
            "[废标检查] 错别字检查完成: 保留 %d 条（其中 %d 条已按整文重新锚定位置），"
            "丢弃 %d 条无法定位的疑似幻觉条目",
            len(anchored),
            reanchored,
            discarded_count,
        )
    return anchored


async def check_logic_dimension(
    provider: Any,
    bid_doc: dict,
    false_positive_patterns: list[dict] | None = None,
) -> list[dict]:
    """逻辑谬误检查：全文综合分析"""
    logger.info(
        "[废标检查] 逻辑检查开始: doc_id=%s, doc_name=%s, text_len=%d",
        bid_doc.get("id"),
        bid_doc.get("name"),
        len(bid_doc.get("text", "")),
    )
    sys_prompt = get_prompt("LOGIC_CHECK_SYSTEM")
    logger.info("[废标检查] 逻辑检查 提示词加载: %d字", len(sys_prompt))
    text = bid_doc.get("text", "")

    # 增强 4.3: 注入假阳性模式
    fp_instruction = ""
    if false_positive_patterns:
        fp_instruction = f"""
已知以下条目曾被误判为逻辑谬误（假阳性），请避免重复报告类似内容：
{json.dumps([p.get("description", "") for p in false_positive_patterns[:5]], ensure_ascii=False)}
"""

    # BUG-FIX 2026-10-15（D3）：原实现把全文截断到 30000 字，超长投标
    # 文件的尾部逻辑矛盾完全漏检。现按与废标维度同口径的滚动分段策略
    # 逐段检查，合并所有发现。
    chunks = split_into_chunks(text, chunk_size=30000, overlap=2000)
    all_logic_issues: list[dict] = []
    total_chunks = len(chunks)
    for idx, chunk in enumerate(chunks):
        # SECURITY-FIX 2026-10-06：chunk 为投标文件原文，做元标记中和。
        safe_chunk = sanitize_prompt_value(chunk)
        user_prompt = (
            f"{fp_instruction}文档名：{bid_doc.get('name', '')}\n\n"
            f"投标文件（第 {idx + 1}/{total_chunks} 段）：\n{safe_chunk}\n\n"
            f"请检查本段内容中的逻辑谬误。"
        )
        messages = [
            AIMessage(role="system", content=sys_prompt),
            AIMessage(role="user", content=user_prompt),
        ]
        try:
            # BUG-FIX 2026-09-18（高）：同错别字维度，原实现只接受裸 list。
            # 模型返回 {"findings": [...]} 等包装时整批丢弃 → 逻辑一致性维度静默 0 条。
            result = _coerce_issue_list(
                await collect_json_response(provider, messages, max_retries=2)
            )
            if isinstance(result, list):
                all_logic_issues.extend(result)
        except Exception:
            logger.warning("逻辑谬误AI检查失败 (段 %d/%d)", idx + 1, total_chunks, exc_info=True)
            continue

    for issue in all_logic_issues:
        issue.setdefault("bid_doc_id", bid_doc.get("id"))
        issue.setdefault("bid_doc_name", bid_doc.get("name", ""))
        issue.setdefault("category", "逻辑")
    return all_logic_issues


# ========== 主检查流程 ==========


async def run_rejection_check(task_id: int, dimension: str | None = None):
    """执行废标检查任务（后台运行）

    Args:
        task_id: 任务ID
        dimension: 可选，仅重试指定维度（"rejection"/"spelling"/"logic"）
    """
    task = get_task(task_id)
    if not task:
        return

    try:
        ai_config = get_ai_config()
        provider = create_provider(ai_config)
    except Exception as e:
        update_task_status(task_id, "failed", error=f"AI 配置异常: {e}")
        return

    update_task_status(task_id, "running")
    update_task_progress(task_id, 5, "正在初始化检查...")

    # 增强 4.3: 加载假阳性模式，避免重复误报
    false_positive_patterns = get_false_positive_patterns(
        project_id=task.get("project_id"), limit=10
    )
    if false_positive_patterns:
        logger.info("[废标检查] 加载 %s 条假阳性模式用于优化检查", len(false_positive_patterns))

    enabled = task["enabled_dimensions"]
    # 如果指定了维度且该维度已启用，则只检查该维度
    if dimension:
        if dimension in enabled:
            enabled = [dimension]
        else:
            update_task_status(task_id, "failed", error=f"维度 {dimension} 未启用")
            return
    bid_docs = task["bid_documents"]
    check_items = task["check_items"]
    tender_text = task["tender_text"]

    results = {"rejection": [], "spelling": [], "logic": []}
    # BUG-FIX 2026-09-18（高）：单维度重试（retry-dimension 传入 dimension）时，
    # `enabled` 被收窄为 [dimension]，但 `results` 仍是三个维度全空的新字典，
    # 而函数结尾用 `update_task_status(..., results=results)` **整体覆盖**旧结果。
    # 后果：用户重试「错别字」后，此前的废标条款、逻辑一致性结论被空列表抹掉，
    # 界面上看起来像「这两项检查无问题」——检查结论被静默删除。
    # 修复：重试场景下先继承上一次的旧结果，只让本次真正执行的维度被覆盖。
    if dimension:
        _prev_results = task.get("results") or {}
        if isinstance(_prev_results, dict):
            for _dim_key in ("rejection", "spelling", "logic"):
                _prev_val = _prev_results.get(_dim_key)
                if isinstance(_prev_val, list):
                    results[_dim_key] = _prev_val
        logger.info(
            "[废标检查] 单维度重试 mode=dimension=%s，已继承其余维度历史结果",
            dimension,
        )
    total_docs = len(bid_docs)
    # BUG-FIX-73: 并发数配置校验，防止非数值/负数导致 Semaphore 异常
    _raw_concurrency = ai_config.get("concurrency", 4)
    try:
        _concurrency = int(_raw_concurrency)
        if _concurrency < 1:
            _concurrency = 4
    except (TypeError, ValueError):
        _concurrency = 4
    semaphore = asyncio.Semaphore(_concurrency)

    # Track per-dimension progress for detailed status reporting
    dim_progress: dict[str, float] = {dim: 0.0 for dim in enabled}

    # BUG-FIX 2026-09-18（中）：维度级异常此前只被包装成一条 `severity="info"` 的
    # 伪 issue，任务最终仍以 `completed` 收尾。当 AI 整体不可用（密钥失效、超时、
    # 配额耗尽）时，三个维度全挂，用户看到的却是「检查完成」加一条不起眼的 info，
    # 极易误判为「标书没问题」。这里统计各维度失败文档数，用于收尾给出真实状态。
    dim_failures: dict[str, int] = {"rejection": 0, "spelling": 0, "logic": 0}

    async def _check_one_doc(doc_idx: int, doc: dict):
        async with semaphore:
            doc_results = {"rejection": [], "spelling": [], "logic": []}

            # ENHANCE 2026-09-14：三轮流程会显著拉长废标项检查耗时，期间上报轮次文案，
            # 避免前端看起来"卡住"。整体进度仍由文档完成度驱动，轮次不参与百分比计算。
            doc_progress_holder = [10 + int(doc_idx / max(total_docs, 1) * 85)]

            def _rejection_progress(message: str) -> None:
                update_task_progress(
                    task_id,
                    doc_progress_holder[0],
                    f"{doc.get('name', '')} · {message}",
                )

            async def _wrap_rejection():
                if "rejection" in enabled:
                    try:
                        return await check_rejection_dimension(
                            provider,
                            tender_text,
                            doc,
                            check_items,
                            false_positive_patterns=false_positive_patterns,
                            progress_callback=_rejection_progress,
                        )
                    except Exception as e:
                        dim_failures["rejection"] += 1
                        logger.warning(
                            "[废标检查] 废标维度失败 doc=%s: %s", doc.get("name", ""), e
                        )
                        return [
                            {
                                "id": f"err-r-{doc_idx}",
                                "description": f"废标项检查失败: {e}",
                                "severity": "info",
                                "category": "废标",
                                "bid_doc_name": doc.get("name", ""),
                            },
                        ]
                return []

            async def _wrap_spelling():
                if "spelling" in enabled:
                    try:
                        return await check_spelling_dimension(
                            provider,
                            doc,
                            false_positive_patterns=false_positive_patterns,
                        )
                    except Exception as e:
                        dim_failures["spelling"] += 1
                        logger.warning(
                            "[废标检查] 错别字维度失败 doc=%s: %s", doc.get("name", ""), e
                        )
                        return [
                            {
                                "id": f"err-s-{doc_idx}",
                                "description": f"错别字检查失败: {e}",
                                "severity": "info",
                                "category": "错别字",
                                "bid_doc_name": doc.get("name", ""),
                            },
                        ]
                return []

            async def _wrap_logic():
                if "logic" in enabled:
                    try:
                        return await check_logic_dimension(
                            provider,
                            doc,
                            false_positive_patterns=false_positive_patterns,
                        )
                    except Exception as e:
                        dim_failures["logic"] += 1
                        logger.warning(
                            "[废标检查] 逻辑维度失败 doc=%s: %s", doc.get("name", ""), e
                        )
                        return [
                            {
                                "id": f"err-l-{doc_idx}",
                                "description": f"逻辑检查失败: {e}",
                                "severity": "info",
                                "category": "逻辑",
                                "bid_doc_name": doc.get("name", ""),
                            },
                        ]
                return []

            r, s, logic_result = await asyncio.gather(
                _wrap_rejection(), _wrap_spelling(), _wrap_logic()
            )
            doc_results["rejection"] = r
            doc_results["spelling"] = s
            doc_results["logic"] = logic_result

            # 合并到全局
            results["rejection"].extend(r)
            results["spelling"].extend(s)
            results["logic"].extend(logic_result)

            # 更新每个维度的进度（按文档完成度）
            for dim in enabled:
                dim_progress[dim] += 1 / total_docs

            # 计算整体进度 (10% - 95%)
            progress = 10 + int((doc_idx + 1) / total_docs * 85)

            # 构建各维度进度详情（显示中文标签）
            dim_names: dict[str, str] = {
                "rejection": "废标条款",
                "spelling": "错别字",
                "logic": "逻辑一致性",
            }
            dim_details = ", ".join(
                f"{dim_names.get(dim, dim)}: {int(dim_progress[dim] * 100)}%" for dim in enabled
            )
            update_task_progress(
                task_id,
                progress,
                f"已完成文档 {doc_idx + 1}/{total_docs}: {doc.get('name', '')} [{dim_details}]",
            )

    # 并发执行所有文档的检查
    await asyncio.gather(*[_check_one_doc(i, doc) for i, doc in enumerate(bid_docs)])

    update_task_progress(task_id, 95, "正在生成报告...")

    # BUG-FIX 2026-09-18（中）：按维度失败情况给出**真实**任务终态。
    # 原实现无论维度是否全部失败都写 `completed`，AI 全挂时用户看到「检查完成」，
    # 误以为标书通过了废标/错别字/逻辑检查。
    _fully_failed_dims = [
        d for d in enabled if total_docs > 0 and dim_failures.get(d, 0) >= total_docs
    ]
    if enabled and len(_fully_failed_dims) == len(enabled):
        _final_status = "failed"
        _final_error = (
            "所有检查维度均失败（AI 服务可能不可用或密钥失效），"
            "结论不可用，请检查 AI 配置后重试"
        )
        logger.error("[废标检查] 全部维度失败，任务标记为 failed: task=%s", task_id)
    elif _fully_failed_dims:
        _final_status = "completed"
        _final_error = "部分维度检查失败，结论不完整: " + "、".join(_fully_failed_dims)
        logger.warning(
            "[废标检查] 部分维度失败 %s，任务仍完成但标注错误: task=%s",
            _fully_failed_dims,
            task_id,
        )
    else:
        _final_status = "completed"
        _final_error = ""

    # 保存结果
    update_task_status(task_id, _final_status, results=results, error=_final_error)
    update_task_progress(task_id, 100, "检查完成" if _final_status == "completed" else "检查失败")


# ========== 报告导出 ==========


def export_report(task_id: int) -> str:
    """导出检查报告（DOCX）"""
    task = get_task(task_id)
    if not task:
        raise ValueError("任务不存在")

    REJECTION_REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    try:
        from docx import Document
        from docx.enum.text import WD_ALIGN_PARAGRAPH
    except ImportError:
        raise ImportError("需要安装 python-docx")

    doc = Document()

    # 标题
    title = doc.add_heading("废标检查报告", level=0)
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER

    doc.add_paragraph(f"生成时间：{datetime.now(UTC).strftime('%Y-%m-%d %H:%M:%S')}")
    doc.add_paragraph(f"任务 ID：{task_id}")
    if task.get("project_id"):
        doc.add_paragraph(f"项目 ID：{task['project_id']}")

    # 概要
    doc.add_heading("检查概要", level=1)
    results = task.get("results", {})
    # BUG-FIX 2026-10-15（D2）：概要计数此前含误报条目，但下方详情区
    # 按 false_positives 过滤后只展示非误报项 → 概要说「5 项」、详情只列
    # 3 项，用户困惑。现按过滤后口径计数，并在有误报时追加一行说明。
    fps = set(task.get("false_positives", []))
    dimension_titles = {
        "rejection": "废标项问题",
        "spelling": "错别字问题",
        "logic": "逻辑谬误",
    }
    for dim, name in dimension_titles.items():
        issues = results.get(dim, [])
        visible = [i for i in issues if i.get("id") not in fps]
        p = doc.add_paragraph()
        p.add_run(f"{name}：").bold = True
        p.add_run(f"{len(visible)} 项")
        hidden = len(issues) - len(visible)
        if hidden:
            p.add_run(f"（另有 {hidden} 项已标记为误报，不在下方展示）")

    # ENHANCE 2026-09-14：废标项证据核验概览（区分"已定位"与"待人工核验"）
    rejection_items = results.get("rejection", [])
    if rejection_items:
        _verified = sum(1 for i in rejection_items if i.get("validated") is True)
        _unverified = sum(1 for i in rejection_items if i.get("validated") is False)
        _structural = sum(1 for i in rejection_items if i.get("validated") is None)
        p = doc.add_paragraph()
        p.add_run("废标项证据核验：").bold = True
        p.add_run(
            f"已在原文定位 {_verified} 项，"
            f"未能核验 {_unverified} 项，"
            f"结构性线索（材料缺失类，无原文摘录）{_structural} 项"
        )

    # ENHANCE 2026-09-15：跨分块「疑似缺失」复核统计
    _missing_resolved = sum(
        1 for i in rejection_items if isinstance(i, dict) and i.get("missing_state") == MISSING_STATE_RESOLVED
    )
    _missing_confirmed = sum(
        1 for i in rejection_items if isinstance(i, dict) and i.get("missing_state") == MISSING_STATE_CONFIRMED
    )
    _missing_pending = sum(
        1 for i in rejection_items if isinstance(i, dict) and i.get("missing_state") == MISSING_STATE_PENDING
    )
    if _missing_resolved or _missing_confirmed or _missing_pending:
        p = doc.add_paragraph()
        p.add_run("疑似缺失跨块复核：").bold = True
        p.add_run(
            f"全文复核后排除 {_missing_resolved} 项（该材料在其它分块中存在线索），"
            f"确认缺失 {_missing_confirmed} 项，待人工确认 {_missing_pending} 项"
        )

    # 各类问题详情（fps / dimension_titles 已在概要区定义，此处直接复用）
    for dim, title_text in dimension_titles.items():
        issues = results.get(dim, [])
        visible_issues = [i for i in issues if i.get("id") not in fps]
        if not visible_issues:
            continue

        doc.add_heading(title_text, level=1)
        for i, issue in enumerate(visible_issues, 1):
            severity = issue.get("severity", "warning")
            sev_text = {"critical": "严重", "warning": "警告", "info": "提示"}.get(severity, "警告")
            p = doc.add_paragraph()
            p.add_run(f"{i}. [{sev_text}] ").bold = True
            p.add_run(issue.get("description", "无描述"))

            if issue.get("bid_doc_name"):
                doc.add_paragraph(f"   文档：{issue['bid_doc_name']}")

            if issue.get("quote"):
                p = doc.add_paragraph()
                p.add_run("   原文引用：").bold = True
                p.add_run(issue["quote"])

            if issue.get("quote_a"):
                p = doc.add_paragraph()
                p.add_run("   矛盾点 A：").bold = True
                p.add_run(issue["quote_a"])
            if issue.get("quote_b"):
                p = doc.add_paragraph()
                p.add_run("   矛盾点 B：").bold = True
                p.add_run(issue["quote_b"])

            if issue.get("location"):
                doc.add_paragraph(f"   位置：{issue['location']}")

            # ENHANCE 2026-09-14：带上证据核验结论，人工复核时可直接跳到行
            if issue.get("validated") is True and issue.get("line_hint"):
                doc.add_paragraph(
                    f"   证据定位：{issue['line_hint']}（摘录已在投标文件原文中核验通过）"
                )
            elif issue.get("validated") is False:
                doc.add_paragraph(
                    "   证据定位：未能核验（摘录与原文不一致，可能是模型改写或误报，建议人工重点复核）"
                )

            # ENHANCE 2026-09-15：疑似缺失的跨块复核结论
            if issue.get("missing_state_reason"):
                doc.add_paragraph(f"   缺失复核：{issue['missing_state_reason']}")

            if issue.get("suggestion"):
                p = doc.add_paragraph()
                p.add_run("   修改建议：").bold = True
                p.add_run(issue["suggestion"])

            doc.add_paragraph("")  # 空行

    # 保存
    filename = f"rejection_report_{task_id}_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}.docx"
    file_path = REJECTION_REPORTS_DIR / filename
    doc.save(str(file_path))

    return str(file_path)


def export_report_excel(task_id: int) -> str:
    """导出检查报告（Excel 多 Sheet）。

    ENHANCE 2026-09-15：移植自易标 `checkResultExportService.cjs` 的
    「检查概览 / 废标项 / 错别字 / 逻辑问题」四 Sheet 结构。

    与 Word 报告的分工：Word 用于阅读归档，Excel 用于**复核与流转**
    （可按风险等级筛选、按文件排序、把问题清单直接贴进整改台账）。
    """
    task = get_task(task_id)
    if not task:
        raise ValueError("任务不存在")

    sheets = _excel_report.build_rejection_workbook(task)
    filename = f"rejection_report_{task_id}_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}.xlsx"
    return str(_excel_report.write_workbook(sheets, REJECTION_REPORTS_DIR / filename))


# ========== 错别字一键修正（ENHANCE 2026-09-15）==========
#
# 参考易标 `rejectionCheckTask.cjs`：错别字检查除了给出 wrongText/correctText，
# 还会用 `findVerifiedTypoPosition` 把位置核验出来，**目的就是支持一键替换**。
# 本平台此前只做到"检测 + 导出"，人工还得回原文手改 —— 位置信息算是白算了。
#
# 设计取舍：
#   - 只改**已核验**（position 与原文逐字相符）的条目，未核验的一律拒绝；
#   - 替换走 `text_patch.apply_range_edits`（下标定位 + 整批校验），不靠字符串
#     `str.replace`，避免"同一错字多次出现改错位置"；
#   - 默认只更新**任务内的投标文件文本**（复查/重导出立即生效），
#     写回项目正文需显式 `writeback_project=True`（投标文本由项目正文拼装而来）。


def _find_spelling_item(results: dict, item_id: str) -> dict | None:
    for item in results.get("spelling") or []:
        if isinstance(item, dict) and str(item.get("id")) == str(item_id):
            return item
    return None


def _resolve_spelling_span(text: str, item: dict, original: str) -> int:
    """定位待替换片段：优先用已核验的 position，否则重新定位；失败返回 -1。"""
    position = item.get("position")
    if isinstance(position, int) and 0 <= position <= len(text) - len(original):
        if text[position : position + len(original)] == original:
            return position
    return locate_text_span(original, text)


def _map_task_text_offset_to_section(project_id: int, offset: int) -> tuple[int, int] | None:
    """把「任务投标文本」的字符偏移映射回项目小节 `(section_id, 小节内偏移)`。

    `import_from_project` 用 `f"## {title}\\n\\n{content}"` 逐节拼接、`"\\n\\n".join`，
    且与这里都走 `list_sections` 的同一排序，因此可以按同样方式重建偏移表做精确映射。
    映射不上时返回 None —— 调用方跳过写回，不做猜测。
    """
    from .sections import list_sections

    cursor = 0
    for section in list_sections(project_id):
        content = section.content or ""
        if not content:
            continue
        prefix = f"## {section.title}\n\n"
        part_start = cursor
        part_end = part_start + len(prefix) + len(content)
        if part_start <= offset < part_end:
            inner = offset - part_start - len(prefix)
            return (section.id, inner) if 0 <= inner < len(content) else None
        cursor = part_end + 2  # "\n\n".join 的分隔符
    return None


def apply_spelling_fixes(
    task_id: int,
    item_ids: list[str],
    *,
    writeback_project: bool = False,
) -> dict:
    """一键应用错别字修正，返回逐条结果（单条失败不影响其余条）。

    Args:
        task_id: 废标检查任务 ID
        item_ids: 要修正的错别字条目 ID 列表
        writeback_project: 是否同时把修正写回项目正文小节（需文本布局匹配）

    Returns:
        {"ok": True, "applied": [...], "skipped": [...], "task_id": ...}
    """
    from ..models import SectionUpdate

    from . import text_patch
    from .sections import get_section, update_section

    task = get_task(task_id)
    if not task:
        raise ValueError("任务不存在")
    if not item_ids:
        raise ValueError("未指定要修正的错别字条目")
    # 检查未结束时结果还会被整体覆盖写回，此时改文本会被冲掉（且可能改到半成品）
    if task.get("status") in ("running", "pending"):
        raise ValueError("检查任务尚未结束，请等检查完成后再修正错别字")

    results = task.get("results") or {}
    documents = task.get("bid_documents") or []
    if not documents:
        raise ValueError("该任务没有可修正的投标文件文本")

    applied: list[dict] = []
    skipped: list[dict] = []
    audit_log = list(results.get("spelling_fixes") or [])
    documents_changed = False

    for item_id in item_ids:
        item = _find_spelling_item(results, item_id)
        if item is None:
            skipped.append({"item_id": item_id, "reason": "任务结果中找不到该错别字条目"})
            continue

        original = str(item.get("original") or item.get("wrongText") or "").strip()
        corrected = str(item.get("corrected") or item.get("correctText") or "").strip()
        if not original or not corrected:
            skipped.append({"item_id": item_id, "reason": "条目缺少原文或改正文本"})
            continue
        if original == corrected:
            skipped.append({"item_id": item_id, "reason": "原文与改正文本相同，无需替换"})
            continue
        # 只动已核验过的条目：未核验说明位置不可信，盲改风险高于收益
        if item.get("verified") is False:
            skipped.append({"item_id": item_id, "reason": "该条目未通过原文核验，请人工确认后再改"})
            continue

        doc_index = next(
            (
                i
                for i, doc in enumerate(documents)
                if isinstance(doc, dict) and str(doc.get("id")) == str(item.get("bid_doc_id"))
            ),
            0,
        )
        document = documents[doc_index] if isinstance(documents[doc_index], dict) else None
        if document is None:
            skipped.append({"item_id": item_id, "reason": "投标文件文本缺失"})
            continue

        text = str(document.get("text") or "")
        position = _resolve_spelling_span(text, item, original)
        if position < 0:
            skipped.append(
                {"item_id": item_id, "reason": "未能在投标文件原文中定位该错别字，可能已被修订或属误报"},
            )
            continue

        edit_result = text_patch.apply_range_edits(
            text,
            [{"start": position, "end": position + len(original), "new_text": corrected}],
        )
        if edit_result.get("errors") or not edit_result.get("changed"):
            skipped.append(
                {"item_id": item_id, "reason": (edit_result.get("errors") or ["替换未产生变化"])[0]},
            )
            continue

        document["text"] = edit_result["content"]
        documents_changed = True

        item["applied"] = True
        item["after_excerpt"] = build_verified_excerpt(
            edit_result["content"], position, len(corrected)
        )
        # 替换后位置仍有效（等长或长度变化都按新文本重新核验），避免二次替换错位
        item["position"] = position
        item["original"] = corrected

        section_updated: int | None = None
        if writeback_project and task.get("project_id"):
            mapped = _map_task_text_offset_to_section(task["project_id"], position)
            if mapped:
                section_id, inner = mapped
                section = get_section(section_id)
                section_content = (section.content if section else "") or ""
                if section_content[inner : inner + len(original)] == original:
                    update_section(
                        section_id,
                        SectionUpdate(
                            content=(
                                section_content[:inner]
                                + corrected
                                + section_content[inner + len(original) :]
                            )
                        ),
                    )
                    section_updated = section_id

        audit_log.append(
            {
                "item_id": item_id,
                "bid_doc_name": document.get("name", ""),
                "before": original,
                "after": corrected,
                "position": position,
                "section_id": section_updated,
                "applied_at": datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S"),
            }
        )
        applied.append(
            {
                "item_id": item_id,
                "before": original,
                "after": corrected,
                "line_hint": item.get("line_hint", ""),
                "excerpt": item.get("after_excerpt", ""),
                "section_id": section_updated,
            }
        )

    if applied:
        results["spelling_fixes"] = audit_log
        with get_db() as conn:
            conn.execute(
                "UPDATE rejection_checks SET bid_documents = ?, results = ?, "
                "updated_at = datetime('now', 'localtime') WHERE id = ?",
                (
                    json.dumps(documents, ensure_ascii=False),
                    json.dumps(results, ensure_ascii=False),
                    task_id,
                ),
            )

    logger.info(
        "[废标检查] 错别字一键修正: 成功 %d 条, 跳过 %d 条, 文本变更=%s",
        len(applied),
        len(skipped),
        documents_changed,
    )
    return {
        "ok": True,
        "task_id": task_id,
        "applied": applied,
        "skipped": skipped,
        "applied_count": len(applied),
        "skipped_count": len(skipped),
    }


# ========== 项目导入辅助 ==========


async def import_from_project(project_id: int) -> dict:
    """从项目导入招标原文和投标方案正文"""
    from .files import latest_extracted_text
    from .sections import list_sections

    tender_text = await latest_extracted_text(project_id)

    # 投标方案正文：合并所有 sections 的 content
    sections = list_sections(project_id)
    bid_text_parts = []
    for s in sections:
        if s.content:
            bid_text_parts.append(f"## {s.title}\n\n{s.content}")

    return {
        "tender_text": tender_text,
        "bid_text": "\n\n".join(bid_text_parts),
        "project_id": project_id,
    }
