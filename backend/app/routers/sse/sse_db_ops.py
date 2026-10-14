"""sse_handlers 拆分模块 — sse_db_ops.py"""

import asyncio
import hashlib
import json
import logging
import time
from typing import Any
from fastapi import HTTPException, Query
from starlette.responses import StreamingResponse
from ...db import get_db
from ...services.ai.outline_common import child_number
from ...services.ai.provider_factory import get_ai_config
from ...models import SectionUpdate
from ...services.ai.industry_profiles import get_industry_profile
from ...services.ai.workflows import generate_outline as _run_outline_workflow
from ...services.ai.workflows import generate_sections as _run_sections
from ...services.files import latest_extracted_text
from ...services.generation_manager import generation_manager
from ...services.projects import update_step
from ...services.redis_cache import CacheService
from ...services.sections import (

    _count_readable_words,

    _count_words,

    _invalidate_build_tree_cache,

    build_tree,

    get_subtree_leaf_ids,

    invalidate_content_caches,

    update_section,

)
from ...services.task_registry import TaskStatus, task_registry
from ...services.tender_context import extract_tender_context_async
from ...services.v51_state import (

    clear_v51_state_from_db,

    create_v51_state,

    load_v51_state_from_db,

    remove_v51_state,

    save_v51_state_to_db,

)
from ..task_control import _cancel_events, _cancel_events_lock, _cancel_tasks

from .sse_shared import logger
from .sse_utils import _parse_global_facts_items

def _save_section_to_db(

    section_id: int,

    status: str,

    content: str = "",

    project_id: int | None = None,

):

    """将章节生成结果保存到数据库



    Args:

        section_id: 章节 ID

        status: 状态 (completed/failed/cancelled)

        content: 生成的内容

        project_id: 项目 ID（PERF-SAVE-NO-SELECT 2026-09-08 新增）。

            传入后可直接失效 build_tree 缓存，省去一次 SELECT。

            若为 None 则 fallback 到原 SELECT 逻辑（保留兼容）。



    BUG #18 修复：completed 状态时同步更新 word_count，

    否则前端章节树中字数永远显示为 0。



    2026-08-23 修复：保存后立即失效 build_tree 缓存，

    确保前端 silentRefreshTree() 获取到最新的 has_content/status 数据。

    """

    with get_db() as conn:

        if status == "completed":

            # BUG-FIX-23 修复：原用 len(content.replace(...)) 粗暴去空白字符计数，

            # 与 workflows_sections.py 中 _count_words（中文字逐个+英文按词）不一致，

            # 导致 DB 存储的 word_count 与前端进度条/日志显示的字数不符。

            # BUG-2026-1008-02 修复（P1）：正文清洗——移除重复标题、Markdown
            # 标记、首行编号前缀，避免导出文档中出现双重标题。
            if content:
                try:
                    _title_row = conn.execute(
                        "SELECT title FROM sections WHERE id = ?", (section_id,)
                    ).fetchone()
                    _section_title = _title_row["title"] if _title_row else ""
                    from ...services.content_cleaning import ContentCleaner

                    content = ContentCleaner().clean(_section_title, content)
                except Exception as _e:
                    logger.debug("[SSE] 正文清洗失败（忽略，保留原内容）: %s", _e)

            word_count = _count_words(content) if content else 0

            # 易标 R1（2026-10-12）：同一条 SQL 同步持久化可读字数口径
            # （CJK 逐字 + 西文按词，剔除 Markdown/图片/代码块/URL），与
            # word_count 并存以便迁移期双口径对比。
            readable_word_count = _count_readable_words(content) if content else 0

            conn.execute(

                "UPDATE sections SET content = ?, status = 'completed', word_count = ?, readable_word_count = ?, updated_at = datetime('now', 'localtime') WHERE id = ?",

                (content, word_count, readable_word_count, section_id),

            )

        elif status == "failed":

            conn.execute(

                "UPDATE sections SET status = 'failed', updated_at = datetime('now', 'localtime') WHERE id = ?",

                (section_id,),

            )

        elif status == "cancelled":

            # 修复 BUG #22：cancelled 状态不再映射为 failed，直接存储真实状态

            # 使前端能区分"用户取消"和"生成失败"两种情况

            conn.execute(

                "UPDATE sections SET status = 'cancelled', updated_at = datetime('now', 'localtime') WHERE id = ?",

                (section_id,),

            )



        # 失效 build_tree 缓存：优先使用调用方传入的 project_id，省去一次 SELECT。

        # 仅在调用方未传时才走原 SELECT 路径（保留兼容）。

        _pid = project_id
        if _pid is not None:

            try:

                _invalidate_build_tree_cache(_pid)

            except Exception as _e:

                logger.debug("[SSE] 静默捕获异常: %s", _e)

        else:

            try:

                row = conn.execute(

                    "SELECT project_id FROM sections WHERE id = ?", (section_id,)

                ).fetchone()

                if row:

                    _pid = row["project_id"]
                    _invalidate_build_tree_cache(_pid)

            except Exception as _e:

                logger.debug("[SSE] 静默捕获异常: %s", _e)

        # BUG-FIX 缓存失效（P2）：正文写入属于旁路直写，仅失效 60s 的 build_tree
        # 内存缓存不够 —— Redis 整包章节缓存 `section_content:project:{id}`（TTL
        # 1800s）仍会回灌旧正文，进而喂给后续 L1/L2 生成。这里一并失效 Redis 与
        # 内存正文缓存，避免「改了正文但 AI/前端读到旧内容」的脏读。
        if _pid is not None:
            try:
                invalidate_content_caches(_pid, section_ids=[section_id])
            except Exception as _e:
                logger.debug("[SSE] 失效正文缓存失败（忽略）: %s", _e)

def _mark_sections_generating(section_ids: list[int]):

    """批量更新章节状态为 generating



    Args:

        section_ids: 章节 ID 列表

    """

    if not section_ids:

        return

    with get_db() as conn:

        placeholders = ",".join("?" for _ in section_ids)

        conn.execute(

            f"UPDATE sections SET status = 'generating', updated_at = datetime('now', 'localtime') WHERE id IN ({placeholders})",

            tuple(int(sid) for sid in section_ids),

        )

        # BUG-FIX 缓存失效（P2）：状态变更也是旁路直写，Redis 整包章节缓存未失效
        # 会让前端 / 后续生成读到过期的 generating 状态。按 id 反查 project_id 后失效。
        _row = conn.execute(
            "SELECT project_id FROM sections WHERE id = ?", (int(section_ids[0]),)
        ).fetchone()
        if _row:
            try:
                invalidate_content_caches(_row["project_id"], section_ids=list(section_ids))
            except Exception as _e:
                logger.debug("[SSE] 失效状态缓存失败（忽略）: %s", _e)

def _rollback_dirty_sections(leaf_ids: list[int]):

    """将 leaf_ids 中 status='generating' 的章节重置为待生成状态

    场景：任务被手动停止或 AI 服务异常导致部分章节卡在 generating 状态

    BUG-FIX 2026-09-16（中）：不再清空 content。重生成模式下
    _mark_sections_generating 只置 status、保留原 content；若中断收尾在这里
    把 content 一并清空，被打断章节的**原有正文本就永久丢失**（status 回
    pending 但旧内容没了）。回滚只需重置状态，内容由下次生成覆盖。

    """

    if not leaf_ids:

        return

    try:

        with get_db() as conn:

            placeholders = ",".join("?" for _ in leaf_ids)

            # 注意：sections 表没有 generated_words 列（历史遗留语句），

            # 且第一条 UPDATE 已把 status 从 generating 重置为 pending，

            # 第二条按 status='generating' 过滤的语句必然空匹配，故删除。

            conn.execute(

                f"UPDATE sections SET status = 'pending', updated_at = datetime('now', 'localtime') "

                f"WHERE id IN ({placeholders}) AND status = 'generating'",

                tuple(int(sid) for sid in leaf_ids),

            )

            logger.info("[正文生成] 回滚脏状态完成: %d 个章节", len(leaf_ids))

            # BUG-FIX 缓存失效（P2）：状态回滚也是旁路直写，需失效整包章节缓存。
            _row = conn.execute(
                "SELECT project_id FROM sections WHERE id = ?", (int(leaf_ids[0]),)
            ).fetchone()
            if _row:
                try:
                    invalidate_content_caches(_row["project_id"], section_ids=list(leaf_ids))
                except Exception as _e:
                    logger.debug("[SSE] 失效状态缓存失败（忽略）: %s", _e)

    except Exception as e:

        logger.warning("[正文生成] 回滚脏状态失败: %s", e)

async def _extract_project_context(project_id: int) -> dict:

    """提取项目上下文数据：摘要、评分点、行业信息、招标上下文等"""



    def _fetch():

        with get_db() as conn:

            row = conn.execute(

                # BUG-FIX 2026-09-18（P1）：补 boq_json——清单工程量此前在
                # 目录生成链路是 write-only 死字段，现随上下文一并取出供 L2 注入。
                "SELECT summary_json, scoring_points_json, scope, global_facts_json, boq_json FROM analysis WHERE project_id = ?",

                (project_id,),

            ).fetchone()

            if not row:

                raise HTTPException(status_code=400, detail="请先完成摘要和评分点识别")



            project_row = conn.execute(

                "SELECT industry, name FROM projects WHERE id = ?", (project_id,)

            ).fetchone()

            if not project_row:

                raise HTTPException(status_code=404, detail="项目不存在")



            return dict(row), dict(project_row)



    meta_result = await asyncio.to_thread(_fetch)

    row, project_row = meta_result



    summary = row["summary_json"]

    scoring_points_str = row["scoring_points_json"]

    try:

        scoring_points = json.loads(scoring_points_str) if scoring_points_str else []

    except json.JSONDecodeError as _sp_e:
        # BUG-FIX 2026-10-15（M-3 · 静默降级不可见）：坏 JSON 塌成空评分点后，
        # 目录生成照常出结果，用户完全无从得知「评分点覆盖」这一路输入是空的。
        # 行为不变（不阻塞生成），只补可排查的告警。
        logger.warning(
            "目录上下文装配: scoring_points_json 不可解析，已降级为空列表 "
            "project=%d err=%s 前50字=%r",
            project_id, _sp_e, str(scoring_points_str)[:50],
        )
        scoring_points = []



    scoring_points_list = scoring_points if isinstance(scoring_points, list) else []



    industry = project_row.get("industry", "")

    industry_profile = get_industry_profile(industry) if industry else ""



    try:

        full_text = await latest_extracted_text(project_id)

        tender_context = await extract_tender_context_async(full_text) if full_text else ""

    except Exception as _tc_e:
        # BUG-FIX 2026-10-15（M-2 · 同缺陷不同口径）：2026-09-18 给
        # `sse_utils._extract_project_context` 的同一段裸吞补了告警，本函数与
        # `sse_content` / Celery 任务里的另外三份副本一直是
        # `except Exception: tender_context = ""`。招标正文读取失败时目录仍照常
        # 生成，事后既无日志也无法判断「目录为何与招标文件脱节」。
        # 现四处统一为「不阻塞 + 必留告警」。
        logger.warning(
            "目录上下文装配: 招标正文/上下文提取失败，将不带招标上下文继续 "
            "project=%d err=%s",
            project_id, _tc_e,
        )
        tender_context = ""



    # 获取参考目录（默认28章模板）

    try:

        from ...services.ai.workflows import _get_template_list_json  # type: ignore[attr-defined]

    except ImportError:

        from ...services.ai.outline_common import _get_template_list_json



    reference_outline = _get_template_list_json()



    project_info = {

        "name": project_row.get("name", ""),

        "industry": industry,

    }



    return {

        "summary": summary,

        "scoring_points": scoring_points_str,

        "scoring_points_list": scoring_points_list,

        "industry_profile": industry_profile,

        "reference_outline": reference_outline,

        "tender_context": tender_context,

        "project_scope": row.get("scope") or "",

        "project_info": project_info,

        "global_facts_items": _parse_global_facts_items(row.get("global_facts_json")),

        "boq_json": row.get("boq_json") or "",

    }

async def _extract_project_context_with_retry(project_id: int, max_attempts: int = 4) -> dict:

    """提取项目上下文；对瞬时 SQLite I/O 错误做退避重试。



    _extract_project_context 内部各 get_db() 已自带 3 次连接重试并重置失效连接，

    但极端情况（写入高峰 + 杀毒实时扫描短暂锁定大库/WAL）下错误可能超过其重试窗口。

    该函数为纯只读查询，幂等，可安全重跑，故在调用方再补一层退避重试以提升弹性。

    """

    from ...db import _is_io_error



    last_err = None

    for attempt in range(1, max_attempts + 1):

        try:

            return await _extract_project_context(project_id)

        except Exception as e:

            last_err = e

            if not _is_io_error(e):

                raise

            if attempt < max_attempts:

                logger.warning(

                    "[目录生成] 项目上下文读取 I/O 错误，第 %d/%d 次重试: %s",

                    attempt,

                    max_attempts,

                    e,

                )

                await asyncio.sleep(0.5 * attempt)

    raise last_err

def _count_outline_nodes(outline: list) -> int:

    """递归统计目录树总节点数（含所有层级）。"""

    total = 0

    for node in outline:

        # BUG 修复：若 node 不是 dict，node.get() 会 AttributeError

        if not isinstance(node, dict):

            continue

        total += 1

        children = node.get("children", [])

        if isinstance(children, list):

            total += _count_outline_nodes(children)

    return total


def _snapshot_titles(outline: list) -> list[str]:
    """按前序遍历收集全树标题（用于消歧前后 diff 计数）。"""
    out: list[str] = []

    def _walk(nodes: list) -> None:
        if not isinstance(nodes, list):
            return
        for _n in nodes:
            if not isinstance(_n, dict):
                continue
            out.append(str(_n.get("title") or ""))
            _walk(_n.get("children"))

    _walk(outline)
    return out


def _count_title_changes(before: list[str], after: list[str]) -> int:
    """前后同位置标题发生变化的节点数（长度不一致时按较大值兜底）。"""
    n = min(len(before), len(after))
    changed = sum(1 for _i in range(n) if before[_i] != after[_i])
    return changed + abs(len(before) - len(after))


def _clear_and_save_outline(project_id: int, outline: list) -> list:

    """清除现有目录并保存新目录，返回带数据库整数 ID 的 outline"""

    from ...services.sections import invalidate_content_caches

    # ENHANCE 2026-09-15（目录生成最后一道归一化防线）：
    # workflows_outline_utils 里的归一化能力（剥编号 / 过滤空标题与 AI 错误响应 /
    # 同级标题去重 / level 按层级重算）此前只被单测引用，V5.1 主链路并未调用，
    # AI 偶尔带"3.2.1 xxx"编号前缀或重复标题时会直接入库。这里在写库前兜底一次，
    # 无论上游是 AI 还是人工确认流，落库数据都满足同一份契约。
    from ...services.ai.workflows_outline_utils import (
        normalize_outline_items,
        _dedupe_outline_titles,
    )

    outline = normalize_outline_items(outline or [])
    # BUG-FIX 目录生成：normalize_outline_items 仅做「同父去重」，无法消除不同父节点
    # 下的完全同名标题（AI 偶发在两个不同章节下各生成一份完全相同的子标题，导出后
    # 会出现重复条目）。落库前再跑一次全树去重，与 _dedupe_outline_titles 的文档意图一致。
    outline = _dedupe_outline_titles(outline)

    # 2026-10-12 加固（最后一道占位标题安全网）：无论上游是 AI 生成流、
    # 直出流还是人工编辑后回写，落库前再过一遍占位/泛化标题清洗。
    # 顺序严格：先剥编号 → 再去重 → 最后清占位。
    #   _cleanup_placeholder_titles 内部用全局 used 集合 + 父级语义派生，
    #   若先清占位后去重，改写产生的新标题可能再次撞名被 _dedupe_outline_titles
    #   追加（其N）后缀，反而引入新的脏标题。
    try:
        from ...services.ai.outline_common import _cleanup_placeholder_titles
        _ph_fixed = _cleanup_placeholder_titles(outline)
        if _ph_fixed > 0:
            logger.info(
                "_clear_and_save_outline: 落库前清洗 %d 处 L3+ 占位/泛化标题 project=%d",
                _ph_fixed, project_id,
            )
    except Exception as _ph_exc:
        # 安全网失败不阻塞落库，上游（L3 质量收口链 + db_migrations 启动清理）
        # 已有两道防线覆盖绝大多数场景
        logger.warning(
            "_clear_and_save_outline: 占位标题安全网跳过（不阻塞落库）: %s", _ph_exc
        )

    # 2026-10-12 收尾闭环（BUG-FIX P1）：cleanup 改写标题后可能与**兄弟节点**撞名，
    # 而此时 `_dedupe_outline_titles`（第 535 行）早已跑完，无人再消歧。此处补一次
    # 同级去重——顺序严格遵守「清洗 → 去重」（与 workflows_outline_v51 主链路口径一致）：
    # 反序会把裸编号后缀从标题末尾挪到中间，令 cleanup 的末尾匹配失效。
    # 此前这里只落 warning 让用户「重跑目录生成解决」，等于把可确定修复的问题
    # 推给用户；现在直接修，并把修复计数打进日志便于审计。
    _ph_fixed_after_uniquify = 0
    try:
        from ...services.ai.outline_common import _uniquify_outline_titles
        _before_titles = _snapshot_titles(outline)
        outline = _uniquify_outline_titles(outline)
        _changed = _count_title_changes(_before_titles, _snapshot_titles(outline))
        if _changed:
            _ph_fixed_after_uniquify = _changed
            logger.info(
                "_clear_and_save_outline: 清洗后同级撞名消歧 %d 处 project=%d",
                _changed, project_id,
            )
    except Exception as _uq_exc:
        logger.warning(
            "_clear_and_save_outline: 同级消歧跳过（不阻塞落库）: %s", _uq_exc
        )

    # 2026-10-15 收口补齐（B3 跨章成套复用护栏 + B4 消歧产物复检）：
    # 此前落库侧只有 cleanup→uniquify 单向，与 workflows_outline_v51 的
    # `_post_selfcheck_title_safety_net`（cleanup→uniquify→cleanup）不同构，
    # 消歧/护栏改写产物若仍泛化会直接落库。人工编辑章节与审核回灌路径
    # 完全绕开 V5.1 生成链，这里是它们唯一的防线。
    try:
        from ...config import OUTLINE_L3_MAX_PARENT_REUSE
        from ...services.ai.outline_common import (
            _cap_cross_chapter_repetition,
            _cleanup_placeholder_titles as _cleanup_after_cap,
        )

        outline, _capped = _cap_cross_chapter_repetition(
            outline, OUTLINE_L3_MAX_PARENT_REUSE
        )
        if _capped:
            logger.info(
                "_clear_and_save_outline: 跨章成套复用护栏改写 %d 个三级标题 project=%d",
                _capped, project_id,
            )
        _ph_fixed_after_uniquify += _cleanup_after_cap(outline)
    except Exception as _cap_exc:
        logger.warning(
            "_clear_and_save_outline: 跨章复用护栏跳过（不阻塞落库）: %s", _cap_exc
        )

    # 2026-10-12 收尾闭环：cleanup 内部已用 global_used 防跨父撞名，
    # 但去重/清洗改写产生的副产物可能带来新的同级撞名。这里追加轻量检测
    # 作为**兜底校验**：正常情况下应为 0，非 0 说明前面某道防线漏了，
    # 仅告警不阻塞（落库优先，避免为极端脏数据卡死目录生成）。
    try:
        _dup_count = 0
        def _check_siblings(nodes, depth=0):
            nonlocal _dup_count
            if not isinstance(nodes, list):
                return
            seen = {}
            for _n in nodes:
                if not isinstance(_n, dict):
                    continue
                _t = (_n.get("title") or "").strip()
                if not _t:
                    continue
                if _t in seen:
                    _dup_count += 1
                    logger.warning(
                        "_clear_and_save_outline: 检测到 %d 级同级撞名 '%s' project=%d "
                        "(父级路径上的兄弟节点编号 %s 与 %s)",
                        depth, _t, project_id, seen[_t], _n.get("id"),
                    )
                else:
                    seen[_t] = _n.get("id")
                _sub = _n.get("children")
                if isinstance(_sub, list):
                    _check_siblings(_sub, depth + 1)
        _check_siblings(outline)
        if _dup_count > 0:
            logger.warning(
                "_clear_and_save_outline: 落库前撞名检测发现 %d 处同级撞名 project=%d "
                "（已通过 warning 暴露，不阻塞落库；可重跑目录生成解决）",
                _dup_count, project_id,
            )
    except Exception as _chk_exc:
        logger.debug("_clear_and_save_outline: 撞名检测跳过: %s", _chk_exc)

    with get_db() as conn:

        # ENHANCE 2026-09-18（跨模块数据保全）：
        # 原实现无条件 `DELETE FROM sections` 后全量重建，用户只要重新生成一次目录
        # （哪怕只是微调一级标题），此前几十章、耗时数十分钟生成的正文就被**无声清空**，
        # 既无提示也无回滚手段（目录生成与正文生成之间缺一道数据保全）。
        # 这里在删除前按 (level, title) 快照「非空正文」，插入后精确回填：
        #   · 标题与层级都未变的章节 → 正文/状态/字数预算完整保留
        #   · 标题变化或新增的章节 → 仍按全新章节处理（内容为空）
        # 遗留 L2 修复（2026-10-04）：旧口径 dict[(level,title)] 只存单条记录，
        # 不同父节点下的同名同层旧章节撞键覆盖（一份正文静默丢失，且可能回填
        # 到错误子树）。改为 (level,title) → 候选列表 的消费式匹配：同键多候选
        # 时优先父链路径全等者，无精确匹配退回旧的宽松口径（保留标题改名容忍），
        # 每条旧正文最多被消费一次。
        _prev_content: dict = {}
        _snap_total = 0
        try:

            _rows = conn.execute(
                "SELECT id, parent_id, level, title, content, status, target_words FROM sections "
                "WHERE project_id = ?",
                (project_id,),
            ).fetchall()
            _by_id = {int(_r["id"]): _r for _r in _rows}

            def _row_path(_r) -> str:
                """沿 parent_id 链拼接绝对路径（祖先标题/…/自身标题）；
                悬空父/成环即断链，降级为已收集部分，不阻断快照。"""
                _parts = [str(_r["title"] or "")]
                _seen = {int(_r["id"])}
                _pid = _r["parent_id"]
                while _pid is not None and int(_pid) in _by_id and int(_pid) not in _seen:
                    _seen.add(int(_pid))
                    _pr = _by_id[int(_pid)]
                    _parts.append(str(_pr["title"] or ""))
                    _pid = _pr["parent_id"]
                return "/".join(reversed(_parts))

            for _r in _rows:
                if not _r["content"]:
                    continue
                _k = (int(_r["level"] or 1), str(_r["title"] or ""))
                _prev_content.setdefault(_k, []).append(
                    {
                        "content": _r["content"],
                        "status": _r["status"],
                        "target_words": _r["target_words"],
                        "path": _row_path(_r),
                    }
                )
                _snap_total += 1

            if _snap_total:
                logger.info(
                    "目录重建：快照到 %d 个已生成正文的章节，将在同名节点上按父链路径优先回填",
                    _snap_total,
                )
        except Exception as _snap_e:
            # BUG-FIX 数据保全（P0）：快照失败若继续 DELETE，用户已生成的数十章正文
            # 将被无声清空且无法回滚（目录生成与正文生成之间原本就缺一道数据保全）。
            # 改为中止本次重建并保留既有正文，让上层以错误事件告知用户重试，
            # 而不是丢失数据。
            raise RuntimeError(
                f"目录重建前快照旧正文失败，已中止重建以保留现有正文: {_snap_e!s}"
            ) from _snap_e

        # 清除现有章节

        conn.execute("DELETE FROM sections WHERE project_id = ?", (project_id,))

        _restored_count = 0



        # 递归保存章节，返回带数据库 ID 的节点

        def _save_nodes(
            nodes: list, parent_id: int | None = None, base_level: int = 1, parent_path: str = "",
            parent_number: str = "",
        ) -> list:

            nonlocal _restored_count

            result = []

            # BUG-FIX 2026-10-04（编号命名空间统一）：sort_order 此前从 0 起编，
            # 而 move_section/reorder_sections/_normalize_section_position 全部约定
            # 1-based 连续递增：目录重建后首次拖拽重排前，新旧值混用会使
            # reorder_sections 的连续性校验误报、新章节 MAX+1 与旧行撞号。
            # 统一为 1-based，与全库其余写入路径同一份契约。
            for sort_order, node in enumerate(nodes, start=1):

                title = node.get("title", "")

                # 根据父节点自动计算层级，不依赖AI返回的level字段

                # BUG-FIX-LEVEL-01：原实现 node.get("level") or (...) 优先信任外部

                # level 字段，AI/前端传入错误层级（如 L2 节点标 level=5）会直接入库；

                # 且 level=0（falsy）被静默重算，行为不一致。改为始终按父子关系计算。

                level = base_level + 1 if parent_id is not None else base_level

                children = node.get("children", [])

                section_type = node.get("section_type", "")

                scoring_point_id = node.get("scoring_point_id")

                # BUG-FIX 2026-10-09（编号命名空间统一 · 跨模块数据传递丢失）：
                # 落库后的目录树此前从不携带 number 字段，后果是三层各算一遍：
                #   1. 前端 OutlineEditorPage 靠 outlineDictToTreeNodes 自算展示编号；
                #   2. 导出/正文模块靠 get_section_path 从 DB 现算；
                #   3. 任何 API 直取 outline JSON 的消费方（如 v51-confirm 回显、
                #      前端刷新后重建树、外部对接方）拿不到编号，只能重新实现。
                # 现落库时随节点透出 path_number，与 get_section_path 同口径，
                # 作为跨模块的唯一编号事实源；title 保持纯文本，不改动。
                #
                # BUG-FIX 2026-10-13（L1 · 编号命名空间单一事实源）：拼接算法
                # 收敛到 `outline_common.child_number`（与门禁 renumber 的 id、
                # DB 版 get_section_path 同一实现）；原注释所述
                # `workflows_outline_utils._renumber_items` 早已不存在于生产
                # 代码（该引用过时），事实源现为 `outline_common.tree_path_numbers`
                # / `child_number`。一致性由
                # tests/test_outline_l3_hardening_2026_10_13.py::TestPathNumberNamespace 锁定。
                node_number = child_number(parent_number, sort_order)



                cursor = conn.execute(

                    """INSERT INTO sections (project_id, parent_id, title, level, sort_order, section_type, scoring_point_id, target_words, status, graph_suitable, suggested_chart)

                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)""",

                    (

                        project_id,

                        parent_id,

                        title,

                        level,

                        sort_order,

                        section_type,

                        scoring_point_id,

                        # BUG-FIX 2026-09-18（中）：target_words 此前只写进返回给前端的
                        # saved_node（见下方 BUG-OUTLINE-02），从未落库到 sections 列。
                        # 页面刷新后从 DB 重读该列恒为 NULL，目录阶段设定的自定义字数
                        # 预算全部丢失，_get_effective_target_words 只能回退层级默认值，
                        # 前端「字数预算」面板与实际生成预算不一致。
                        node.get("target_words"),

                        # ENHANCE 2026-10-08（跨模块数据链修复）：目录生成阶段由 AI 标注的
                        # 「是否适合配图 / 建议图表类型」此前只存在于内存树，落库时被整条
                        # 丢弃（INSERT 不含这两列），图表模块无从读取 → 跨模块数据传递丢失。
                        # 现落库并随 saved_node 透出，下游图表模块可据此直接复用目录阶段的
                        # 图表预测（PRD §2.2.1 的 chart_predictions 意图）。
                        1 if node.get("graph_suitable") else 0,

                        str(node.get("suggested_chart") or ""),

                    ),

                )

                new_id = cursor.lastrowid

                # ENHANCE 2026-09-18：按 (level, title) 回填重建前快照的正文，
                # 使「重新生成目录」不再摧毁已生成的章节内容。
                # 遗留 L2：同键多候选时优先父链路径全等，命中后从候选中消费，
                # 保证一份旧正文不会被同时回填到两个新节点。
                _node_path = f"{parent_path}/{title}" if parent_path else str(title)
                _prev = None
                _cands = _prev_content.get((level, title))
                if _cands:
                    _pick = next(
                        (i for i, _c in enumerate(_cands) if _c.get("path") == _node_path), 0
                    )
                    _prev = _cands.pop(_pick)
                    if not _cands:
                        del _prev_content[(level, title)]

                # BUG-FIX 2026-10-11（O-6）：把「回填时写入 DB 的 status」和
                # 「saved_node 里返回给前端/上层 workflow 的 status」抽成同一份变量
                # `_node_status`。旧实现：
                #   · UPDATE 分支写入 `_prev.get("status") or "completed"`（正文已回填
                #     → 视为已生成完毕）；
                #   · saved_node 却固定写 "pending"。
                # 二者不一致的后果：`_save_nodes` 返回结果里 `status="pending"`，但
                # DB 里该 section 是 "completed"。上层（sse_outline._send_outline_nodes /
                # outline_gate / 前端 SectionTreeNode 直消费 saved_node 的 SSE 事件）
                # 会把「已保留正文」的章节误判为「空白待生成」，用户点「补全生成」时
                # collectLeafIds 会把它们选进叶子集合，AI 生成时整体覆盖旧正文 ——
                # 与「正文保全」的初衷正相反。
                _node_status = "pending"
                if _prev and _prev.get("content"):
                    _node_status = _prev.get("status") or "completed"

                if _prev and _prev.get("content"):
                    # BUG-FIX 2026-09-29：回填正文时同步 word_count 列——此前只写
                    # content/status/target_words，DB 的 word_count 停留在 NULL，
                    # build_tree/list_sections 从 DB 读该列，目录重建后前端字数显示 0。
                    conn.execute(
                        "UPDATE sections SET content = ?, status = ?, word_count = ?, "
                        "readable_word_count = ?, "
                        "target_words = COALESCE(target_words, ?), "
                        "updated_at = datetime('now', 'localtime') WHERE id = ?",
                        (
                            _prev["content"],
                            _node_status,
                            _count_words(_prev["content"]),
                            _count_readable_words(_prev["content"]),
                            _prev.get("target_words"),
                            new_id,
                        ),
                    )

                    _restored_count += 1



                saved_children = _save_nodes(children, new_id, level, _node_path, f"{node_number}.") if children else []



                saved_node = {

                    "id": new_id,

                    "title": title,

                    "level": level,

                    "sort_order": sort_order,

                    # BUG-FIX 2026-10-09（跨模块数据传递）：随节点透出路式编号
                    # （"1" / "1.2" / "1.2.3"），口径同 services.sections.get_section_path，
                    # 让前端 / 导出 / 正文 / v51-confirm 回显共用同一份编号事实源。
                    "number": node_number,

                    # BUG-FIX 2026-10-11（O-6）：与上方 UPDATE 写入 DB 的 status 保持
                    # 一致（回填正文的节点为 "completed"，未回填的新章节保持 "pending"），
                    # 避免前端 / SSE 事件 / 上层 workflow 把「已保留正文」的章节误判为
                    # 待生成而被「补全生成」覆盖旧内容。
                    "status": _node_status,

                    # ENHANCE 2026-10-08（跨模块数据链修复）：把目录阶段 AI 标注的图表适配
                    # 信号随节点透出，build_tree / 前端 / 图表模块均可读取（此前被丢弃）。
                    "graph_suitable": bool(node.get("graph_suitable")),

                    "suggested_chart": str(node.get("suggested_chart") or ""),

                    # ENHANCE 2026-09-18：回填了旧正文的节点如实上报，避免前端
                    # 把「已保留正文」的章节显示成空白待生成。
                    "has_content": bool(_prev and _prev.get("content")),

                    # BUG-FIX 2026-09-29：此前用 len(content) 裸字符数，与其余链路
                    # （_count_words：中文逐字+英文按词）口径不一致，目录重建后前端
                    # 字数显示突变。统一改用 _count_words。
                    "word_count": _count_words(_prev["content"]) if (_prev and _prev.get("content")) else 0,

                    "children": saved_children,

                }

                if section_type:

                    saved_node["section_type"] = section_type

                else:

                    saved_node["section_type"] = ""

                if scoring_point_id is not None:

                    saved_node["scoring_point_id"] = scoring_point_id

                else:

                    saved_node["scoring_point_id"] = None

                # BUG-OUTLINE-02 修复：包含 target_words 字段供前端字数预算参考

                target_words = node.get("target_words")

                if target_words is not None:

                    saved_node["target_words"] = target_words

                else:

                    saved_node["target_words"] = None

                result.append(saved_node)

            return result



        saved = _save_nodes(outline)

        # ENHANCE 2026-09-18：如实汇报正文保全情况，便于排查「重新生成目录后
        # 正文是否还在」这类问题。
        if _snap_total:
            logger.info(
                "目录重建完成：旧正文快照 %d 章，实际回填 %d 章（其余章节标题已变化）",
                _snap_total,
                _restored_count,
            )

        # BUG-FIX 2026-10-04（日志不准确/容错）：落库后立即在同连接上跑一次
        # 树完整性自检（services.sections.validate_outline_integrity 同口径：
        # 悬空 parent_id/环/同父重复 sort_order）。正常链路不应触发；一旦触发
        # 说明写入过程有缺陷，必须留痕而不是静默把坏树交给下游正文/导出。
        try:
            _orphans = conn.execute(
                "SELECT COUNT(*) FROM sections s WHERE s.project_id = ? "
                "AND s.parent_id IS NOT NULL "
                "AND NOT EXISTS (SELECT 1 FROM sections p WHERE p.id = s.parent_id)",
                (project_id,),
            ).fetchone()[0]
            _dup_orders = conn.execute(
                "SELECT COUNT(*) FROM (SELECT parent_id, sort_order FROM sections "
                "WHERE project_id = ? GROUP BY parent_id, sort_order HAVING COUNT(*) > 1)",
                (project_id,),
            ).fetchone()[0]
            if _orphans or _dup_orders:
                logger.error(
                    "目录重建后完整性自检异常 project=%d：悬空父节点 %d，同父重复 sort_order %d",
                    project_id, _orphans, _dup_orders,
                )
            # ENHANCE 2026-10-08（层级完整率可观测）：结构三项之外再查
            # 「level == 实际父子深度」。_save_nodes 本就按深度写 level，正常
            # 链路恒通过；一旦触发说明写入链路被绕过/数据被旁路改过 —— 而 level
            # 漂移会让前端编号分组、DOCX outlineLvl、字数分档三处同时错，
            # 却是此前完全不可见的盲区（对齐上方注释「必须留痕」的自检意图）。
            # 必须复用**同一个 conn**：此刻写事务仍持有 db.py 的进程级写锁，
            # 另开连接（哪怕 readonly）会直接拿到 "database is locked"，
            # 自检就从「兜底留痕」退化成「每次都失败刷日志」。故用递归 CTE 在
            # 同连接上一次性算出深度，口径同 services.sections.validate_outline_levels。
            _level_mismatch = conn.execute(
                "WITH RECURSIVE dep(id, depth) AS ("
                "  SELECT id, 1 FROM sections WHERE project_id = ? AND parent_id IS NULL"
                "  UNION ALL"
                "  SELECT s.id, dep.depth + 1 FROM sections s"
                "  JOIN dep ON s.parent_id = dep.id WHERE dep.depth < 8"
                ") SELECT COUNT(*) FROM sections s JOIN dep ON s.id = dep.id"
                " WHERE COALESCE(s.level, 0) <> dep.depth",
                (project_id,),
            ).fetchone()[0]
            _max_depth = conn.execute(
                "WITH RECURSIVE dep(id, depth) AS ("
                "  SELECT id, 1 FROM sections WHERE project_id = ? AND parent_id IS NULL"
                "  UNION ALL"
                "  SELECT s.id, dep.depth + 1 FROM sections s"
                "  JOIN dep ON s.parent_id = dep.id WHERE dep.depth < 64"
                ") SELECT COALESCE(MAX(depth), 0) FROM dep",
                (project_id,),
            ).fetchone()[0]
            if _level_mismatch:
                logger.error(
                    "目录重建后层级自检异常 project=%d：%d 个节点的 level 与实际层级深度不一致"
                    "（前端编号/DOCX 大纲层级/字数分档会错位）",
                    project_id, _level_mismatch,
                )
            if _max_depth > 8:
                logger.error(
                    "目录重建后层级自检异常 project=%d：最大层级深度 %d 超出可管理上限 8"
                    "（编号命名空间只定义到 L8，导出大纲层级将失真）",
                    project_id, _max_depth,
                )
        except Exception as _chk_e:
            logger.warning("目录重建后完整性自检失败（忽略）project=%d: %s", project_id, _chk_e)

        # BUG-FIX 2026-09-29（跨模块缓存断点：目录生成→正文生成）：全量重建后除
        # build_tree（60s）外，还须失效 Redis 整包章节快照（30min）与单章内存缓存 ——
        # 正文生成入口 _fetch_project_data_async 优先读 Redis 快照，此前重建后短时间内
        # 再跑正文生成会拿到旧章节 id 的快照，生成内容写向已不存在的行（静默丢失）。
        invalidate_content_caches(project_id)

        return saved

async def _save_and_finalize_outline(

    project_id: int,

    outline: list,

    outline_queue: asyncio.Queue,

    is_fallback: bool = False,

    emit=None,

) -> bool:

    """保存目录并推送完成事件（公共函数，消除主流程和降级流程的重复代码）



    Args:

        project_id: 项目 ID

        outline: 目录数据

        outline_queue: SSE 事件队列

        is_fallback: 是否为兜底提供商降级模式

        emit: 可选的事件发送协程（如 sse_outline 的 _qput）。BUG-FIX 2026-09-18
            （P2）：本函数此前裸用 outline_queue.put，绕过了 _qput 里的
            task_registry.broadcast_event/update_progress —— 重连客户端（SSE
            断线后靠广播重建的会话）收不到 saving/完成事件，步骤条永久卡死。
            调用方传入 _qput 后事件走完整广播链；未传时退回直入队列兼容旧调用。



    Returns:

        bool: 是否成功完成

    """

    async def _emit_evt(evt: dict) -> None:

        if emit is not None:

            await emit(evt)

        else:

            await outline_queue.put(evt)

    save_msg = (

        f"兜底提供商成功，正在保存 {len(outline)} 个一级章节..."

        if is_fallback

        else f"正在保存 {len(outline)} 个一级章节..."

    )

    await _emit_evt({"status": "generating", "phase": "saving", "message": save_msg})



    _n_before_save = _count_outline_nodes(outline)

    try:

        outline = await asyncio.to_thread(_clear_and_save_outline, project_id, outline)

    except Exception as e:

        await _emit_evt({"status": "error", "message": f"保存目录失败: {e!s}"})

        return False



    _n_after_save = _count_outline_nodes(outline)

    logger.info(

        "V5.1 目录保存完成: 保存前=%d, 保存后=%d, project=%d",

        _n_before_save,

        _n_after_save,

        project_id,

    )



    # BUG 修复：目录保存成功后推进项目到步骤 4（"已生成目录"），

    # 否则项目列表/步骤条一直停留在步骤 3（"已确认评分"）。

    try:

        await asyncio.to_thread(update_step, project_id, 4)

    except Exception as e:

        logger.warning("推进项目步骤失败 project=%d: %s", project_id, e)



    # 注意：phase_callback 已发送 step 5(V3.6分析完成)、step 7(最终审核完成) 等

    # 此处只发送保存阶段的步骤，避免重复。

    # BUG 修复：V51 前端 OUTLINE_STEPS_V51 仅 8 步，旧代码固定发送 8/9 导致 step 9 越界。

    audit_step = 7

    save_step = audit_step + 1

    await _emit_evt(

        {

            "status": "generating",

            "phase": "outline_phase",

            "outline_step": audit_step,

            "message": "最终目录审核通过，准备检查目录字数范围",

        }

    )

    await _emit_evt(

        {

            "status": "generating",

            "phase": "outline_phase",

            "outline_step": save_step,

            "message": "保存目录到数据库",

        }

    )



    total_level1 = len(outline)

    done_msg = "目录生成完成（已自动降级）" if is_fallback else "目录生成完成"

    done_event = {

        "status": "completed",

        "message": done_msg,

        "outline": outline,

        "progress": {

            "level1": total_level1,

            "completed": total_level1,

            "total": total_level1,

        },

    }

    await _emit_evt(done_event)

    return True
