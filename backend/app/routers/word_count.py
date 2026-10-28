"""字数统计路由

提供两组 API：

A. 原有 4 个「档位（tier）」端点（2026-10-11 修复，保留不动）：
   - GET /word-count/overview/{project_id}   项目总体字数概览（含各档位达标率）
   - GET /word-count/sections/{project_id}   章节字数明细（含档位与达标状态）
   - GET /word-count/tiers                   读取档位配置
   - PUT /word-count/tiers/{tier_key}        修改档位配置（管理员）

B. 易标（OpenBidKit）字数控制端点（ENHANCE 2026-10-12，新增）：
   - GET  /word-count/word-control-settings/{project_id}   读取字数控制配置
   - PUT  /word-count/word-control-settings/{project_id}   写入字数控制配置
    - POST /word-count/word-control-derived                 即改即算派生指标；body 带 project_id 时附带当前目录叶子数与可行性   - POST /word-count/word-control-snapshot/{project_id}   固化当前配置为快照
   - GET  /word-count/audit/{project_id}                   审计（返回结构化警告）

（章节目标字数的设置沿用已有接口：
  PATCH /projects/{projectId}/sections/{sectionId}/target-words
  PATCH /projects/{projectId}/sections/{sectionId}/word-count-tier）
"""
import asyncio
import json
import logging
from collections import defaultdict
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from ..security.auth import get_current_user
from ..db import get_db
from ..services.sections import list_sections
from ..services.ai.writing_length_config import (
    get_l3_own_range,
    get_l3_own_target,
    get_l3_own_tolerance,
    get_tier_config,
    get_default_tier_config,
    list_tier_configs,
    DEFAULT_WORD_COUNT_TIER,
)
from ..utils import count_readable_words
from .project_access import enforce_project_access

logger = logging.getLogger(__name__)
router = APIRouter(prefix='/word-count', tags=['word_count'], dependencies=[Depends(get_current_user)])

async def _load_sections(project_id: int):
    """在**线程池**中读取章节列表（PERF 2026-10-06 修复）。"""
    return await asyncio.to_thread(list_sections, project_id, readonly=True)


def _load_project_writing_length(project_id: int) -> str | None:
    """读取项目最近一次正文生成任务所用的全局档位。

    BUG-FIX 2026-10-09（跨模块数据链 / 生成-校验口径一致）：
    `generation_tasks.writing_length`（db_migrations.py:184）记录了正文生成时
    用户选择的档位，SSE 侧透传给 `_get_effective_target_words(writing_length=…)`，
    但本模块此前完全没读它 —— 对 L4+ 章节一律回落到 standard(1200)，
    导致「生成时按 detailed(2000) 写、校验时按 1200 判定」的系统性误报。

    取不到（无任务 / 表缺失 / DB 异常）时返回 None，调用方回落到默认档位，
    与修复前行为一致（向后兼容）。
    """
    try:
        with get_db(readonly=True) as conn:
            row = conn.execute(
                "SELECT writing_length FROM generation_tasks "
                "WHERE project_id = ? ORDER BY id DESC LIMIT 1",
                (project_id,),
            ).fetchone()
        if row and row["writing_length"]:
            return str(row["writing_length"])
    except Exception as e:
        logger.debug("读取项目 writing_length 失败，回落默认档位: %s", e)
    return None


def _build_children_ids(sections) -> set[int]:
    """构建「拥有下级章节」的父节点 id 集合（D1 修复 2026-10-20）。

    生成侧 ``build_chapter_content_messages`` 显式传 ``is_l3_own``，含下级的 L3
    走四档「子树总字数」；而校验侧此前仅凭 ``level==3/target_words/tier`` 判定，
    把含下级的 L3 误判为「L3 自身正文」240~360，与生成/面板/SSE 口径分叉。
    现按 ``parent_id`` 关系重建下级集合，供 ``_is_l3_own_section`` 与权威解析器
    使用，让四处对同一 L3 的 ``is_l3_own`` 判定逐值一致。
    """
    ids: set[int] = set()
    for s in sections:
        pid = getattr(s, "parent_id", None)
        if pid is not None:
            ids.add(pid)
    return ids


def _section_actual_words(s) -> int:
    """章节实际字数（易标 R1 可读口径，D2 修复 2026-10-20）。

    与生成链 / R9R10 / R12 警告共用同一事实源 ``count_readable_words``，消除
    「校验页用原始 count_words、强控链用可读字数」的膨胀（探针实测含表格/
    图片/URL 的正文两口径差约 40%）。``Section`` 模型未透出 ``readable_word_count``
    列（list_sections 用 SELECT * 但 pydantic 忽略未声明字段），故优先取该列（
    若上游透出）、否则按正文现算。无正文时（生产环境 word_count 由 content 派生，
    必为 0）兼容回落 ``word_count``，以兼容仅设 word_count 的旧单元测试桩。
    """
    rwc = getattr(s, "readable_word_count", None)
    if rwc is not None:
        return int(rwc)
    content = getattr(s, "content", "") or ""
    if content:
        return count_readable_words(content)
    return int(getattr(s, "word_count", 0) or 0)


def _prepare_wc_context(project_id: int, sections) -> tuple[dict, dict]:
    """线程池中预取校验上下文：``(可读字数表, 强控覆盖表)``（D2/D3 修复 2026-10-20）。

    覆盖表复用装配层唯一入口 ``word_control_runtime.build_section_override``，保证
    校验页展示的区间与生成链 R5 完全同源（含「配置失配回落 S」的 GUARD）；
    非强控 / 无快照 / 异常 → 空 dict（校验页回落档位口径，与修复前行为一致）。
    """
    readable_map: dict = {}
    for s in sections:
        readable_map[getattr(s, "id", None)] = _section_actual_words(s)
    override: dict = {}
    try:
        from ..services import word_control_runtime as wcr

        state = wcr.load_project_word_control(project_id)
        override = wcr.build_section_override(state, sections) or {}
    except Exception as e:
        logger.debug("[字数控制] 校验页读取强控覆盖失败（回落档位口径）: %s", e)
    return readable_map, override


def _tolerance_for_section(s, children_ids: set[int] | None = None) -> float:
    """解析章节的容差比例（BUG-FIX 2026-10-09：消除硬编码 ±0.10）。

    此前 `_target_bounds` 直接写死 0.10，而 `word_count_tiers.tolerance_ratio`
    是管理员可编辑列（`PUT /word-count/tiers/{key}`），正文生成侧
    （workflows_sections.py:958）读的正是该列。于是管理员调过容差后，
    生成按新容差扩写、字数校验页却仍按 ±10% 判定，两端口径分叉。

    现按与生成侧相同的优先级取容差：L3 自身正文 → L3_OWN_TOLERANCE_RATIO；
    其余 → 章节档位的 tolerance_ratio；无档位时回落 0.10（保持兼容）。
    """
    if _is_l3_own_section(s, children_ids):
        return get_l3_own_tolerance()
    tier_key = getattr(s, 'word_count_tier', None)
    if tier_key:
        cfg = get_tier_config(tier_key)
        if cfg and cfg.get('tolerance_ratio') is not None:
            try:
                return float(cfg['tolerance_ratio'])
            except (TypeError, ValueError):
                pass
    return 0.10


def _tolerance_for_section_via_resolver(s) -> float:
    """CONVERGE 2026-10-12：容差的权威读取路径（转发唯一权威口径）。

    ``_tolerance_for_section`` 保留为兼容入口（仍有测试与外部调用引用其符号），
    新代码请直接用本函数或 ``_resolve_word_target_for_section``。
    """
    resolved = _resolve_word_target_for_section(s)
    return float(resolved.tolerance) if resolved else 0.10


@router.get('/overview/{project_id}')
async def overview(project_id: int, user=Depends(get_current_user)):
    """项目总体字数概览（2026-10-08：含三级目录达标率与各档位统计）"""
    try:
        await enforce_project_access(project_id, user)
        sections = await _load_sections(project_id)
        writing_length = await asyncio.to_thread(_load_project_writing_length, project_id)
    except HTTPException:
        raise
    except Exception as e:
        logger.error('加载章节失败: %s', e, exc_info=True)
        raise HTTPException(status_code=500, detail=f'加载章节失败: {str(e)[:200]}') from e

    children_ids = _build_children_ids(sections)
    readable_map, override = await asyncio.to_thread(_prepare_wc_context, project_id, sections)
    _kw = {"writing_length": writing_length, "children_ids": children_ids, "override": override}

    # BUG-FIX（2026-10-28 跨模块深度探索 F8）：total_words / per_level / per_status
    # 此前用 raw 口径（count_words），而 on_target 判定用 R1 可读口径——同一节在
    # 统计卡片（raw）与明细表/达标判定（readable）会互相矛盾（含表格/图片/URL 的
    # 正文两口径差约 40%）。D2 修复（2026-10-20）已让「面板/校验/SSE」共用可读
    # 口径，此处补齐统计口径，消除展示矛盾。
    total_words = sum((readable_map.get(s.id, s.word_count or 0) for s in sections))
    total_target = sum((_resolve_target_for_section(s, writing_length, children_ids=children_ids, override=override) or 0 for s in sections))
    completed = [s for s in sections if (readable_map.get(s.id, 0) or 0) > 0]
    on_target = [s for s in sections if _is_within_tolerance(s, actual=readable_map.get(s.id), **_kw)]
    below_target = [s for s in sections if _is_below_target(s, actual=readable_map.get(s.id), **_kw)]
    exceeded_target = [s for s in sections if _is_above_target(s, actual=readable_map.get(s.id), **_kw)]
    empty = [s for s in sections if (readable_map.get(s.id, 0) or 0) == 0]

    level3_sections = [s for s in sections if (s.level or 3) == 3]
    level3_on_target = [s for s in level3_sections if _is_within_tolerance(s, actual=readable_map.get(s.id), **_kw)]
    level3_below = [s for s in level3_sections if _is_below_target(s, actual=readable_map.get(s.id), **_kw)]
    level3_exceeded = [s for s in level3_sections if _is_above_target(s, actual=readable_map.get(s.id), **_kw)]
    per_tier_stats = _compute_tier_stats(sections, writing_length, children_ids, override, readable_map)

    return {'status': 'ok', 'project_id': project_id, 'total_sections': len(sections), 'total_words': total_words, 'total_target': total_target, 'target_coverage': round(total_words / total_target * 100, 1) if total_target else 0, 'completed_sections': len(completed), 'on_target_sections': len(on_target), 'below_target_sections': len(below_target), 'exceeded_target_sections': len(exceeded_target), 'empty_sections': len(empty), 'stats': {'per_level': _group_by_level(sections, readable_map), 'per_status': _group_by_status(sections, readable_map), 'level3_stats': {'count': len(level3_sections), 'on_target': len(level3_on_target), 'below_target': len(level3_below), 'exceeded_target': len(level3_exceeded), 'rate': round(len(level3_on_target) / len(level3_sections) * 100, 1) if level3_sections else 0}, 'per_tier_stats': per_tier_stats}}

@router.get('/sections/{project_id}')
async def section_stats(project_id: int, user=Depends(get_current_user)):
    """章节字数明细（2026-10-08：含档位与达标状态）"""
    try:
        await enforce_project_access(project_id, user)
        sections = await _load_sections(project_id)
        writing_length = await asyncio.to_thread(_load_project_writing_length, project_id)
    except HTTPException:
        raise
    except Exception as e:
        logger.error('加载章节失败: %s', e, exc_info=True)
        raise HTTPException(status_code=500, detail=f'加载章节失败: {str(e)[:200]}') from e

    children_ids = _build_children_ids(sections)
    readable_map, override = await asyncio.to_thread(_prepare_wc_context, project_id, sections)
    items = []
    for s in sections:
        effective_target = _resolve_target_for_section(s, writing_length, children_ids=children_ids, override=override)
        tier_key = getattr(s, 'word_count_tier', None)
        tier_cfg = get_tier_config(tier_key) if tier_key else get_default_tier_config()
        tier_label = tier_cfg.get('label') if tier_cfg else None
        tier_target = int(tier_cfg.get('target_words', 0)) if tier_cfg else 0
        if effective_target and s.word_count > 0:
            on_target_val = _is_within_tolerance(
                s, actual=readable_map.get(s.id),
                writing_length=writing_length, children_ids=children_ids, override=override,
            )
        elif s.word_count == 0:
            on_target_val = 'unknown'
        else:
            on_target_val = False

        # D2 修复 2026-10-20：面板展示字数/进度与 on_target 判定共用 R1 可读口径，
        # 保证「面板/校验/SSE」对同一节的可读字数逐值相等（避免展示 raw、判定 readable 互矛）。
        _a = readable_map.get(s.id)
        if _a is None:
            _a = _section_actual_words(s)
        items.append({
            'id': s.id, 'title': s.title, 'level': s.level, 'parent_id': s.parent_id,
            'section_type': s.section_type, 'status': s.status, 'word_count': _a,
            'target_words': effective_target, 'word_count_text': _human_words(_a),
            'progress': _progress(_a, effective_target),
            'word_count_tier': tier_key, 'tier_label': tier_label, 'tier_target': tier_target,
            'on_target': on_target_val,
        })

    items.sort(key=_section_sort_key)
    return {'status': 'ok', 'project_id': project_id, 'total': len(items), 'items': items}


def _target_bounds(s, *, writing_length: str | None = None,
                   children_ids: set[int] | None = None,
                   override: dict | None = None) -> tuple[int, int]:
    """解析章节达标区间 [lo, hi]（CONVERGE 2026-10-12：转发唯一权威口径）。

    - 体系合并 2026-10-20：强控项目不再在本函数内写独立分支，而是把
      ``build_section_override`` 产物作为 ``strict_override`` 透传给统一解析器（
      命中即短路返回 R5 区间 ``[ceil(0.8S), floor(1.2S)]``）；与生成链 / R9R10 /
      SSE / R12 完全同源，彻底消除「双体系平行实现」；
    - D1 修复 2026-10-20：``children_ids`` 让含下级的 L3 走四档子树总字数；
    - 其余（L3 自身正文 300±20%、L4+ 叶子档位）：target × [1-tol, 1+tol]。
    """
    resolved = _resolve_word_target_for_section(
        s, writing_length, children_ids=children_ids, override=override,
    )
    if not resolved:
        return (0, 0)
    return (int(resolved.lo), int(resolved.hi))


def _is_below_target(s, *, actual: int | None = None,
                     writing_length: str | None = None,
                     children_ids: set[int] | None = None,
                     override: dict | None = None) -> bool:
    """字数已生成且低于达标区间下界（无目标或空正文均不算）。

    D2 修复 2026-10-20：实际字数按易标 R1 可读口径（``actual`` 或 ``_section_actual_words``），
    与 ``_target_bounds`` / 强控链共用同一计量。
    """
    lo, hi = _target_bounds(s, writing_length=writing_length, children_ids=children_ids, override=override)
    a = actual if actual is not None else _section_actual_words(s)
    return bool(lo) and a > 0 and a < lo


def _is_above_target(s, *, actual: int | None = None,
                     writing_length: str | None = None,
                     children_ids: set[int] | None = None,
                     override: dict | None = None) -> bool:
    """字数已生成且高于达标区间上界（无目标或空正文均不算）。D2：R1 可读口径。"""
    lo, hi = _target_bounds(s, writing_length=writing_length, children_ids=children_ids, override=override)
    a = actual if actual is not None else _section_actual_words(s)
    return bool(hi) and a > 0 and a > hi


def _is_l3_own_section(s, children_ids: set[int] | None = None) -> bool:
    """判断章节是否属于 L3 自身正文（无下级目录章节、无档位覆盖）。

    D1 修复 2026-10-20：新增 ``children_ids``——含下级的 L3 不是「自身正文」，
    应走四档「子树总字数」口径。``children_ids`` 为 None（调用方无子节点信息，
    如单元测试直接构造单节）时保持既有多数派口径（视为 L3 自身正文）。
    """
    if (s.level or 3) != 3:
        return False
    if (s.target_words or 0) > 0:
        return False
    if getattr(s, 'word_count_tier', None) not in (None, ''):
        return False
    if children_ids is not None and getattr(s, "id", None) in children_ids:
        return False
    return True


def _resolve_target_for_section(s, writing_length: str | None = None, *,
                                children_ids: set[int] | None = None,
                                override: dict | None = None):
    """解析章节有效目标字数（CONVERGE 2026-10-12：转发唯一权威口径）。

    权威实现见 ``services/word_target_resolver.py``。本函数保留原签名与
    返回语义（``int | None``，L1/L2 → None），口径改为转发。体系合并
    2026-10-20：``override`` 透传至统一解析器（强控命中时 target=S）。

    优先级：强控覆盖（体系 B）> 单节点 target_words > 单节点 word_count_tier
      > L3 自身正文 300±20% > 全局 writing_length > 默认档位（standard）。
    """
    resolved = _resolve_word_target_for_section(
        s, writing_length, children_ids=children_ids, override=override,
    )
    return resolved.target if resolved else None


def _resolve_word_target_for_section(s, writing_length: str | None = None, *,
                                     children_ids: set[int] | None = None,
                                     override: dict | None = None):
    """返回完整口径对象（target / lo / hi / tolerance / source）。

    CONVERGE 2026-10-12：``_target_bounds`` 与 ``_tolerance_for_section`` 原本
    各自再解析一遍（且边界条件不同），现统一取权威对象的 lo/hi/tolerance。

    体系合并 2026-10-20：把强控覆盖表 ``override``（``build_section_override`` 产物）
    按章节 id 取本节条目，作为 ``strict_override`` 透传给统一解析器（命中即短路）。
    """
    from ..services.word_target_resolver import (
        resolve_section_word_target_from_mapping,
    )

    # D1 修复 2026-10-20：对 L3 显式传 is_l3_own（含下级的 L3 → False → 四档子树），
    # 与生成侧 build_chapter_content_messages 对称；非 L3 传 None 交由解析器层级判定。
    is_l3_own = _is_l3_own_section(s, children_ids) if (s.level or 3) == 3 else None
    sid = getattr(s, "id", None)
    ov = override.get(sid) if (override and sid is not None) else None
    return resolve_section_word_target_from_mapping(
        s, writing_length=writing_length, is_l3_own=is_l3_own, strict_override=ov,
    )


def _is_within_tolerance(s, *, actual: int | None = None,
                         writing_length: str | None = None,
                         children_ids: set[int] | None = None,
                         override: dict | None = None) -> bool:
    """判断章节字数是否在达标区间内。

    L3 自身正文：300 ± 20%（默认 240~360，随 L3_OWN_* 配置变化）；
    L3 非叶子（四档子树总字数）与其他层级：±10%。

    D2 修复 2026-10-20：实际字数按 R1 可读口径；D1/D3：区间随 children_ids/override。
    """
    lo, hi = _target_bounds(s, writing_length=writing_length, children_ids=children_ids, override=override)
    a = actual if actual is not None else _section_actual_words(s)
    if not lo or not hi or a == 0:
        return False
    return lo <= a <= hi


def _compute_tier_stats(sections, writing_length: str | None = None,
                        children_ids: set[int] | None = None,
                        override: dict | None = None,
                        readable_map: dict | None = None) -> dict:
    """按档位分组统计达标率。

    L3 自身正文（300 ± 20%，240~360）：归入 l3_own；
    L3 非叶子（四档子树总字数）与其他层级：按各自档位 ±10%。
    D1/D2/D3 修复 2026-10-20：区间随 children_ids/override，计量统一用 R1 可读字数。
    """
    stats = defaultdict(lambda: {'count': 0, 'on_target': 0, 'below': 0, 'exceeded': 0})
    default_cfg = get_default_tier_config()
    default_key = default_cfg.get('key', 'standard')
    for s in sections:
        if (s.level or 3) < 3:
            continue
        tier_key = getattr(s, 'word_count_tier', None) or default_key
        target = _resolve_target_for_section(s, writing_length, children_ids=children_ids, override=override)
        if not target:
            continue
        entry = stats[tier_key]
        entry['count'] += 1
        a = readable_map.get(s.id) if readable_map is not None else _section_actual_words(s)
        if a == 0:
            continue
        lo, hi = _target_bounds(s, writing_length=writing_length, children_ids=children_ids, override=override)
        if a < lo:
            entry['below'] += 1
        elif a > hi:
            entry['exceeded'] += 1
        else:
            entry['on_target'] += 1
    for key, entry in stats.items():
        entry['rate'] = round(entry['on_target'] / entry['count'] * 100, 1) if entry['count'] else 0
    return dict(stats)

def _section_sort_key(item: dict):
    """字数明细排序键：(优先级层, level, -缺口)。

    层 0=有目标且缺字（需扩写），1=有目标已达标，2=无目标（L1/L2 父节点）。
    """
    target = item.get('target_words') or 0
    word_count = item.get('word_count') or 0
    level = item.get('level', 3) or 3
    deficit = target - word_count
    if target and deficit > 0:
        return (0, level, -deficit)
    if target:
        return (1, level, 0)
    return (2, level, 0)

def _group_by_level(sections, readable_map: dict | None = None) -> dict[str, int]:
    """按章节层级分组统计字数。

    BUG-FIX（2026-10-28 跨模块深度探索 F8）：默认 raw 口径此前与 on_target 的
    readable 判定矛盾（含表格/图片/URL 的正文两口径差约 40%）。传入 readable_map
    时改用 R1 可读口径，与校验页/SSE 逐值相等，消除统计卡片展示矛盾。
    """
    groups: dict[str, int] = {}
    for s in sections:
        key = f'level_{s.level}'
        val = readable_map.get(s.id, s.word_count) if readable_map is not None else s.word_count
        groups[key] = groups.get(key, 0) + (val or 0)
    return groups

def _group_by_status(sections, readable_map: dict | None = None) -> dict[str, int]:
    """按生成状态分组统计字数。

    BUG-FIX（2026-10-28 F8）：同上，readable_map 可用时改用 R1 可读口径。
    """
    groups: dict[str, int] = {}
    for s in sections:
        key = s.status or 'unknown'
        val = readable_map.get(s.id, s.word_count) if readable_map is not None else s.word_count
        groups[key] = groups.get(key, 0) + (val or 0)
    return groups

def _human_words(n: int) -> str:
    """将字数格式化为人类可读文本（万/千）"""
    if n >= 10000:
        return f'{n / 10000:.1f}万字'
    if n >= 1000:
        return f'{n / 1000:.1f}千字'
    return f'{n}字'

def _progress(current: int, target) -> int:
    """计算目标达成百分比（0-100）"""
    if not target:
        return 100 if current > 0 else 0
    return min(100, round(current / target * 100))


# ========== 档位配置端点（ENHANCE 2026-10-08）==========

@router.get('/tiers')
async def list_tiers(user=Depends(get_current_user)):
    """读取所有档位配置（简短/标准/详细/超长）。"""
    tiers = list_tier_configs()
    return {'status': 'ok', 'tiers': tiers, 'default_key': DEFAULT_WORD_COUNT_TIER}


# =============================================================================
# 2026-10-11：字数偏差审计端点（新增）
# -----------------------------------------------------------------------------
# 需求：字数偏差可控可追溯。本端点返回每个 L3 章节的目标字数、实际字数、
# 偏差值、偏差率，以及分类统计（达标/偏低/偏高/极端）。
# 与 overview 端点区别：overview 返回项目级汇总；本端点返回章节级明细 + 分布。
# =============================================================================


@router.get('/audit/{project_id}')
async def word_count_audit(project_id: int, user=Depends(get_current_user)):
    """字数偏差审计报告（章节级明细 + 分布统计）。

    2026-10-11 新增：配合字数偏差控制修复，提供可追溯的偏差数据。
    审核与预检模块可读取本端点用于字数审计。
    """
    try:
        await enforce_project_access(project_id, user)
        sections = await _load_sections(project_id)
        writing_length = await asyncio.to_thread(_load_project_writing_length, project_id)
    except HTTPException:
        raise
    except Exception as e:
        logger.error('字数审计加载失败: %s', e, exc_info=True)
        raise HTTPException(status_code=500, detail=f'加载章节失败: {str(e)[:200]}') from e

    # BUG-FIX（2026-10-28 跨模块深度探索 F3）：audit 端点此前与 overview/sections
    # 口径不一致——它调用 `_resolve_target_for_section(s, writing_length)` 与
    # `_target_bounds(s)` 时**不传** `children_ids` 与 `override`（D1/D3 修复 2026-10-20
    # 之后 overview/sections 已传），导致「含下级的 L3」仍按 L3 自身 300±20% 判定、
    # 「强控项目」仍按档位口径判定——同一章节在 audit 与校验页得到矛盾结论。
    # 修复：与 overview 端点同源预取 `children_ids` + `override`（强控覆盖表），
    # 一并透传，使审核/预检与校验页结论一致。
    children_ids = _build_children_ids(sections)
    _readable_map, _override = await asyncio.to_thread(_prepare_wc_context, project_id, sections)
    _wc_kw = {"writing_length": writing_length, "children_ids": children_ids, "override": _override}

    items = []
    total_target = 0
    total_actual = 0
    total_readable = 0
    deviation_sum = 0.0
    deviation_sq_sum = 0.0
    category_counts = {'on_target': 0, 'below_target': 0, 'above_target': 0, 'extreme': 0, 'empty': 0}
    tier_breakdown = {}

    for s in sections:
        level = s.level or 3
        if level < 3:
            # L1/L2 不参与字数控制
            continue
        target = _resolve_target_for_section(s, **_wc_kw)
        if not target:
            continue
        actual = s.word_count or 0
        # 易标 R1 口径（2026-10-12 新增）：判定偏差以**可读字数**为准。
        # count_words 会把 Markdown 标记、代码块、URL、HTML 全计入，
        # 导致 AI 生成的技术文档系统性误报「偏多」。原始值仍保留在
        # actual_words 字段里，便于迁移期对比。
        readable = await asyncio.to_thread(count_readable_words, s.content or '')
        deviation = readable - target
        deviation_rate = round(deviation / target, 4) if target else 0
        lo, hi = _target_bounds(s, **_wc_kw)
        if readable == 0:
            category = 'empty'
        elif lo <= readable <= hi:
            category = 'on_target'
        elif readable < lo:
            category = 'below_target'
        else:
            category = 'above_target'
        if abs(deviation_rate) > 0.5:
            category_counts['extreme'] += 1
        category_counts[category] += 1
        tier_key = getattr(s, 'word_count_tier', None) or 'default'
        tier_breakdown.setdefault(tier_key, {
            'count': 0, 'on_target': 0, 'below': 0, 'above': 0,
            'avg_deviation_rate': 0.0,
        })
        _tb = tier_breakdown[tier_key]
        _tb['count'] += 1
        if category == 'on_target':
            _tb['on_target'] += 1
        elif category == 'below_target':
            _tb['below'] += 1
        else:
            _tb['above'] += 1
        _tb['avg_deviation_rate'] += deviation_rate

        items.append({
            'id': s.id,
            'title': s.title,
            'level': level,
            'target_words': target,
            'actual_words': actual,
            'readable_words': readable,
            'deviation': deviation,
            'deviation_rate': deviation_rate,
            'category': category,
            'lo': lo,
            'hi': hi,
            'word_count_tier': getattr(s, 'word_count_tier', None),
        })
        total_readable += readable

    total_target = sum(i['target_words'] for i in items)
    total_actual = sum(i['actual_words'] for i in items)
    if items:
        deviation_sum = sum(i['deviation_rate'] for i in items)
        deviation_sq_sum = sum((i['deviation_rate']) ** 2 for i in items)
        avg_deviation_rate = round(deviation_sum / len(items), 4)
        std_deviation_rate = round((deviation_sq_sum / len(items)) ** 0.5, 4)
    else:
        avg_deviation_rate = 0.0
        std_deviation_rate = 0.0
    # 计算 tier_breakdown 的平均偏差率
    for _k, _v in tier_breakdown.items():
        if _v['count']:
            _v['avg_deviation_rate'] = round(_v['avg_deviation_rate'] / _v['count'], 4)

    return {
        'status': 'ok',
        'project_id': project_id,
        'total_l3_sections': len(items),
        'total_target': total_target,
        'total_actual': total_actual,
        'target_coverage': round(total_actual / total_target * 100, 1) if total_target else 0,
        # 易标 R1 口径的目标覆盖率（判定以可读字数为准）
        'readable_coverage': round(total_readable / total_target * 100, 1) if total_target else 0,
        'avg_deviation_rate': avg_deviation_rate,
        'std_deviation_rate': std_deviation_rate,
        'category_counts': category_counts,
        'on_target_rate': round(category_counts['on_target'] / len(items) * 100, 1) if items else 0,
        'tier_breakdown': tier_breakdown,
        'items': items,
        # 易标 R12 失败兜底：结构化警告（None 表示一切在区间内）。
        # 前端据此弹「正文已生成，但字数仍超出配置区间」同款确认弹窗。
        'readable_words_total': total_readable,
        'word_control_warning': await asyncio.to_thread(
            _build_warning_for_project, project_id, total_readable,
        ),
    }


# =============================================================================
# 2026-10-12：易标（OpenBidKit）字数控制端点（新增）
# -----------------------------------------------------------------------------
# 移植自《报告_易标文字生成数量控制规则与标准_2026-10-12》：
#   R2 万字输入单位 / R5 强控 ±20% 小节区间 / R6 目标小节数反推与可行性 /
#   R8 初稿目标倒推 / R12 失败兜底警告 / R14 配置快照锚定。
#
# 与已有「档位（tier）」体系**并列、互不覆盖**：
#   word_count_tiers          —— 全局档位字典（简短/标准/详细/超长），
#                               sections.word_count_tier 引用，管理员维护；
#   word_control_settings     —— 本项目一次正文任务的**绝对字数目标**
#                               （最少/最多/每节 + 强控）+ 生效快照，
#                               由项目作者在生成前填写。
#
# 本文件只做「参数归一 → 调纯函数 → 序列化返回」，全部公式口径集中在
# app/services/word_control.py（185 条测试覆盖），禁止在此重复实现，
# 否则就会复现本平台历史上「生成侧/校验侧 tolerance_ratio 分叉」的故障。
# =============================================================================


class WordControlSettingsInput(BaseModel):
    """字数控制配置入参（易标 WordControlOptions 的 wire 形态）。

    单位均为「字」（中文按字计、西文按词计，见 R1 口径）。0 表示该维度不控制。

    ``project_id`` 仅用于 ``POST /word-control-derived`` 的可选上下文
    （附带当前目录叶子数与可行性判定），不参与落库。
    """
    minimum_words: int = 0
    maximum_words: int = 0
    section_words: int = 0
    strict_section_words: bool = False
    project_id: Optional[int] = None

    def to_word_control_options(self):
        """转成纯函数侧的 WordControlOptions（丢弃 project_id）。"""
        from ..services import word_control as wc

        return wc.WordControlOptions(
            self.minimum_words, self.maximum_words, self.section_words,
            self.strict_section_words,
        )

    def settings_dict(self) -> dict:
        """落库/回传用字段（不含 project_id）。"""
        return {
            "minimum_words": self.minimum_words,
            "maximum_words": self.maximum_words,
            "section_words": self.section_words,
            "strict_section_words": self.strict_section_words,
        }


def _now_local() -> str:
    from datetime import datetime

    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _wc_table_ready(conn) -> bool:
    """判断 word_control_settings 表是否已建（迁移未跑时容错）。"""
    return conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name='word_control_settings'"
    ).fetchone() is not None


def _load_word_control_row(project_id: int) -> dict | None:
    """读取项目的字数控制配置行；表不存在或无行时返回 None。"""
    with get_db(readonly=True) as conn:
        if not _wc_table_ready(conn):
            return None
        row = conn.execute(
            "SELECT * FROM word_control_settings WHERE project_id = ?",
            (project_id,),
        ).fetchone()
    return dict(row) if row else None


def _row_to_input(row: dict | None) -> WordControlSettingsInput:
    if not row:
        return WordControlSettingsInput()
    return WordControlSettingsInput(
        minimum_words=int(row.get("minimum_words") or 0),
        maximum_words=int(row.get("maximum_words") or 0),
        section_words=int(row.get("section_words") or 0),
        strict_section_words=bool(row.get("strict_section_words")),
    )


def _options_payload(inp: WordControlSettingsInput) -> dict:
    """纯函数聚合配置校验 + 派生指标，供前端实时预览。

    前端提交前用它做「即改即算」：页数预估、小节 ±20% 区间、
    目标小节数区间、初稿目标字数、可扩缩容量。
    """
    from ..services import word_control as wc

    opts = wc.WordControlOptions(
        inp.minimum_words, inp.maximum_words, inp.section_words,
        inp.strict_section_words,
    )
    s_lo, s_hi = wc.derive_strict_bounds(wc.effective_section_words(opts))
    min_leaf, max_leaf = wc.leaf_count_range(opts)
    target_total = inp.maximum_words or inp.minimum_words
    target_leaf = wc.derive_target_leaf_count(opts)
    # R7：未设置总字数目标时 outline_review_bounds 返回 None（回归 2026-10-12：
    # 未配置项目 GET 该接口曾因直接解包 None 而 500）
    review_bounds = wc.outline_review_bounds(target_leaf)
    return {
        "valid": True,
        "errors": [],
        "section_bounds": [s_lo, s_hi],
        "leaf_count_range": [min_leaf, max_leaf],
        "target_leaf_count": target_leaf,
        "outline_review_bounds": (
            [review_bounds[0], review_bounds[1]] if review_bounds is not None else None
        ),
        "generation_word_target": (
            wc.compute_generation_word_target(
                inp.section_words, inp.maximum_words, target_leaf,
                inp.strict_section_words,
            )
            if target_leaf else None
        ),
        "estimated_pages": wc.estimate_pages(target_total) if target_total else None,
        "word_capacity": wc.check_leaf_count_feasibility(opts, required_leaf_count=0),
    }


@router.get('/word-control-settings/{project_id}')
async def get_word_control_settings(project_id: int, user=Depends(get_current_user)):
    """读取字数控制配置。

    同时回传生效快照（R14）与实时派生指标，前端一字段不缺即可渲染
    配置面板：页数预估 / 小节区间 / 目标小节数 / 初稿目标 / 可扩缩容量。
    """
    await enforce_project_access(project_id, user)
    row = await asyncio.to_thread(_load_word_control_row, project_id)
    inp = _row_to_input(row)

    snapshot = None
    if row and row.get("outline_word_control_snapshot_json"):
        try:
            snapshot = json.loads(row["outline_word_control_snapshot_json"])
        except (TypeError, ValueError):
            snapshot = None

    return {
        "status": "ok",
        "project_id": project_id,
        "settings": inp.settings_dict(),
        "snapshot": snapshot,
        "snapshot_at": row.get("snapshot_at") if row else None,
        "last_warning": row.get("word_control_warning") if row else None,
        "derived": await asyncio.to_thread(_options_payload, inp),
    }


@router.put('/word-control-settings/{project_id}')
async def put_word_control_settings(
    project_id: int,
    body: WordControlSettingsInput,
    user=Depends(get_current_user),
):
    """写入字数控制配置；配置不合理时 422 拒绝保存。

    易标把保存校验放在「设置面板提交」这一步（validateWordControlOptions），
    而不是拖到生成任务才报错 —— 用户在填写阶段就能看到
    「最多字数不能小于最少字数」「区间不存在」这类即时反馈。
    本端点保留同一道闸：不合理的配置不会落库，更不会进快照。
    """
    await enforce_project_access(project_id, user)

    from ..services import word_control as wc

    opts = wc.WordControlOptions(
        body.minimum_words, body.maximum_words, body.section_words,
        body.strict_section_words,
    )
    errors = wc.validate_word_control_options(opts)
    if errors:
        raise HTTPException(status_code=422, detail={"valid": False, "errors": errors})

    # 写入配置后，快照视为失效（需用户重新点「固化」才生效）
    # GUARD 2026-10-12：写事务走线程池——async def 内裸调 get_db() 会阻塞
    # 事件循环（SSE 流被 fsync 卡住），与 update_tier 的既有处理保持一致。
    await asyncio.to_thread(
        _persist_word_control_settings,
        project_id,
        body.minimum_words,
        body.maximum_words,
        body.section_words,
        1 if body.strict_section_words else 0,
        None,
    )

    return {
        "status": "ok",
        "project_id": project_id,
        "settings": body.settings_dict(),
        "snapshot_invalidated": True,
        "derived": _options_payload(body),
    }


def _persist_word_control_settings(
    project_id: int,
    minimum_words: int,
    maximum_words: int,
    section_words: int,
    strict_flag: int,
    snapshot_json: str | None,
) -> None:
    """把「配置 + 快照」写进同一行（同步函数，必须在 to_thread 中调用）。

    快照与配置同表同行：``INSERT OR REPLACE`` 保证二者要么同时更新、要么都不变，
    不会出现「新配置 + 旧快照」的脱节。``snapshot_json=None`` 时连带清空快照
    （改配置即让快照失效，需用户重新固化）。
    """
    now = _now_local()
    with get_db() as conn:
        if not _wc_table_ready(conn):
            return
        conn.execute(
            """
            INSERT OR REPLACE INTO word_control_settings (
                project_id, minimum_words, maximum_words, section_words,
                strict_section_words, outline_word_control_snapshot_json,
                snapshot_at, word_control_warning, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
            """,
            (
                project_id, minimum_words, maximum_words, section_words,
                strict_flag, snapshot_json,
                now if snapshot_json else None,
                now, now,
            ),
        )


def _build_warning_for_project(project_id: int, total_readable: int) -> dict | None:
    """按项目当前配置计算易标 R12 收尾警告（无配置时返回 None）。"""
    from ..services import word_control as wc

    row = _load_word_control_row(project_id)
    inp = _row_to_input(row)
    opts = wc.WordControlOptions(
        inp.minimum_words, inp.maximum_words, inp.section_words,
        inp.strict_section_words,
    )
    if not (opts.minimum_words or opts.maximum_words):
        # 未配置全文区间 → 没有可判定的期望，不弹窗
        return None
    sections = [s for s in (list_sections(project_id, readonly=True) or [])]
    return wc.build_word_control_warning(
        single_section=False,
        total_words=total_readable,
        options=opts,
        sections=sections,
    )


@router.post('/word-control-derived')
async def word_control_derived(
    body: WordControlSettingsInput = WordControlSettingsInput(),
    user=Depends(get_current_user),
):
    """字数控制派生指标（易标 R2/R5/R6/R7/R8 一览）。

    前端「字数控制」面板的即改即算就调它：配置变更**不必落库**，
    直接传预览参数即可拿到页数预估、小节 ±20% 区间、目标小节数区间、
    初稿目标字数、可扩缩容量、叶子数可行性。
    传 project_id 时会附带当前目录叶子数与其可行性判定。
    """
    from ..services import word_control as wc

    payload = _options_payload(body)
    payload["status"] = "ok"
    payload["body"] = body.model_dump()

    if body.project_id:
        try:
            await enforce_project_access(body.project_id, user)
        except HTTPException:
            raise
        sections = await asyncio.to_thread(
            lambda: list_sections(body.project_id, readonly=True)
        )
        sections = [s for s in (sections or [])]
        opts = wc.WordControlOptions(
            body.minimum_words, body.maximum_words, body.section_words,
            body.strict_section_words,
        )
        payload["project_id"] = body.project_id
        payload["current_leaf_count"] = len(sections)
        payload["leaf_outside_range"] = wc.is_outline_leaf_count_outside_range(
            opts, len(sections)
        )
        target_leaf = wc.derive_target_leaf_count(opts)
        payload["leaf_count_warning"] = wc.build_leaf_count_warning(
            expected_leaf_count=target_leaf or 0,
            actual_leaf_count=len(sections),
        ) if target_leaf else None
        payload["word_capacity"] = wc.check_leaf_count_feasibility(
            opts, required_leaf_count=len(sections)
        )
    return payload


@router.post('/word-control-snapshot/{project_id}')
async def create_word_control_snapshot(project_id: int, user=Depends(get_current_user)):
    """把当前配置固化为生效快照（易标 R14）。

    易标的关键设计是「快照配对」：用户在目录阶段设置的字数配置，必须在
    目录生成成功那一刻固化为 outlineWordControlSnapshot，正文生成只认快照，
    从而杜绝「用户手工改了目录但控制链仍按旧配置跑」的静默脱节。

    这里把该闸门显式暴露为一个独立端点，让前端把「固化快照」做成
    生成前的必经步骤，而不是隐式副作用。
    """
    await enforce_project_access(project_id, user)
    row = await asyncio.to_thread(_load_word_control_row, project_id)
    inp = _row_to_input(row)

    from ..services import word_control as wc

    opts = wc.WordControlOptions(
        inp.minimum_words, inp.maximum_words, inp.section_words,
        inp.strict_section_words,
    )
    errors = wc.validate_word_control_options(opts)
    if errors:
        raise HTTPException(status_code=422, detail={"valid": False, "errors": errors})

    sections = [s for s in (await asyncio.to_thread(
        lambda: list_sections(project_id, readonly=True)
    )) or []]
    snapshot = {
        "minimum_words": inp.minimum_words,
        "maximum_words": inp.maximum_words,
        "section_words": inp.section_words,
        "strict_section_words": inp.strict_section_words,
        "snapshot_at": _now_local(),
        "section_count": len(sections),
    }
    # GUARD 2026-10-12：同 put_word_control_settings，写事务走线程池。
    await asyncio.to_thread(
        _persist_word_control_settings,
        project_id,
        inp.minimum_words,
        inp.maximum_words,
        inp.section_words,
        1 if inp.strict_section_words else 0,
        json.dumps(snapshot, ensure_ascii=False),
    )

    return {
        "status": "ok",
        "project_id": project_id,
        "snapshot": snapshot,
        "valid": True,
        "has_valid_snapshot": wc.has_valid_snapshot(snapshot),
        "snapshot_bounds": wc.derive_snapshot_bounds(snapshot),
    }


class TierUpdateRequest(BaseModel):
    """档位配置修改请求体。"""
    target_words: Optional[int] = None
    tolerance_ratio: Optional[float] = None
    is_default: Optional[bool] = None
    label: Optional[str] = None
    enabled: Optional[bool] = None


@router.put('/tiers/{tier_key}')
async def update_tier(tier_key: str, body: TierUpdateRequest, user=Depends(get_current_user)):
    """修改档位配置（管理员操作）。

    仅允许修改 target_words / tolerance_ratio / is_default / label / enabled。
    若设置 is_default=True，需先将其他档位的 is_default 置为 0（保证唯一默认）。
    """
    valid_keys = {'brief', 'standard', 'detailed', 'long'}
    if tier_key not in valid_keys:
        raise HTTPException(status_code=400, detail=f'无效的档位 key: {tier_key}')

    updates = []
    params = []

    if body.target_words is not None:
        if body.target_words < 100 or body.target_words > 5000:
            raise HTTPException(status_code=400, detail='target_words 必须在 100-5000 范围内')
        updates.append('target_words = ?')
        params.append(int(body.target_words))
    if body.tolerance_ratio is not None:
        if not 0 <= body.tolerance_ratio <= 0.5:
            raise HTTPException(status_code=400, detail='tolerance_ratio 必须在 0-0.5 范围内')
        updates.append('tolerance_ratio = ?')
        params.append(float(body.tolerance_ratio))
    if body.is_default is not None:
        # BUG-FIX 2026-10-09（数据破坏）：原实现只要 `body.is_default is not None`
        # 就先全表 `UPDATE ... SET is_default = 0`，再写回本行的值。
        # 于是把某个**非默认**档位的 is_default 显式改成 False（一次完全合理的
        # 「取消勾选」操作）会先把全表默认清空，而本行本来就是 0、没补上默认值
        # —— 项目从此没有任何默认档位；`get_default_tier_config()` 只能回落到
        # `WRITING_LENGTH_TIER_CONFIG['standard']` 常量，管理员此前的所有配置
        # 被静默忽略。
        # 修正：仅在「把本档位设为默认」时才清空其他行；显式取消勾选只更新本行。
        # 行为向后兼容：is_default=True 的旧行为完全不变。
        if body.is_default:
            # BUG-FIX 2026-10-08（阻塞护栏）：async def 内直接 get_db() 会阻塞事件循环
            # （test_async_blocking_gate / test_enhancement_2026-10-05_event_loop_blocking）。
            # 写操作包进线程池执行。
            def _clear_default():
                with get_db() as conn:
                    conn.execute('UPDATE word_count_tiers SET is_default = 0')

            await asyncio.to_thread(_clear_default)
        updates.append('is_default = ?')
        params.append(1 if body.is_default else 0)
    if body.label is not None:
        updates.append('label = ?')
        params.append(body.label)
    if body.enabled is not None:
        updates.append('enabled = ?')
        params.append(1 if body.enabled else 0)

    if not updates:
        return {'status': 'ok', 'message': '无变更', 'tier': get_tier_config(tier_key)}

    updates.append("updated_at = datetime('now', 'localtime')")
    params.append(tier_key)

    def _apply_update():
        with get_db() as conn:
            cur = conn.execute(f'UPDATE word_count_tiers SET {", ".join(updates)} WHERE key = ?', tuple(params))
            return cur.rowcount

    # BUG-FIX 2026-10-09：原实现忽略 rowcount，行不存在时（表被清空 / key 拼错）
    # 仍返回 `{'status':'ok', 'tier': get_tier_config(...)}`，而 get_tier_config 会
    # 静默回落到内置常量 —— 调用方收到「成功」但拿到的是从未写入库的默认值，
    # 排查成本极高。现按 rowcount 判定，未命中返回 404。
    rowcount = await asyncio.to_thread(_apply_update)
    if not rowcount:
        raise HTTPException(status_code=404, detail=f'档位不存在: {tier_key}')

    updated = get_tier_config(tier_key)
    return {'status': 'ok', 'tier': updated}
