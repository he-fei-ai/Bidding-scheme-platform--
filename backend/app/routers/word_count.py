"""字数统计路由

提供 4 个 API 端点：
- GET /word-count/overview/{project_id}   项目总体字数概览（含各档位达标率）
- GET /word-count/sections/{project_id}   章节字数明细（含档位与达标状态）
- GET /word-count/tiers                   读取档位配置
- PUT /word-count/tiers/{tier_key}        修改档位配置（管理员）

（章节目标字数的设置沿用已有接口：
  PATCH /projects/{projectId}/sections/{sectionId}/target-words
  PATCH /projects/{projectId}/sections/{sectionId}/word-count-tier）
"""
import asyncio
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
from .project_access import enforce_project_access

logger = logging.getLogger(__name__)
router = APIRouter(prefix='/word-count', tags=['word_count'], dependencies=[Depends(get_current_user)])

async def _load_sections(project_id: int):
    """在**线程池**中读取章节列表（PERF 2026-10-06 修复）。"""
    return await asyncio.to_thread(list_sections, project_id, readonly=True)


@router.get('/overview/{project_id}')
async def overview(project_id: int, user=Depends(get_current_user)):
    """项目总体字数概览（2026-10-08：含三级目录达标率与各档位统计）"""
    try:
        await enforce_project_access(project_id, user)
        sections = await _load_sections(project_id)
    except HTTPException:
        raise
    except Exception as e:
        logger.error('加载章节失败: %s', e, exc_info=True)
        raise HTTPException(status_code=500, detail=f'加载章节失败: {str(e)[:200]}') from e

    total_words = sum((s.word_count for s in sections))
    total_target = sum((_resolve_target_for_section(s) or 0 for s in sections))
    completed = [s for s in sections if s.word_count > 0]
    on_target = [s for s in sections if _is_within_tolerance(s)]
    below_target = [s for s in sections if _is_below_target(s)]
    exceeded_target = [s for s in sections if _is_above_target(s)]
    empty = [s for s in sections if s.word_count == 0]

    level3_sections = [s for s in sections if (s.level or 3) == 3]
    level3_on_target = [s for s in level3_sections if _is_within_tolerance(s)]
    level3_below = [s for s in level3_sections if _is_below_target(s)]
    level3_exceeded = [s for s in level3_sections if _is_above_target(s)]
    per_tier_stats = _compute_tier_stats(sections)

    return {'status': 'ok', 'project_id': project_id, 'total_sections': len(sections), 'total_words': total_words, 'total_target': total_target, 'target_coverage': round(total_words / total_target * 100, 1) if total_target else 0, 'completed_sections': len(completed), 'on_target_sections': len(on_target), 'below_target_sections': len(below_target), 'exceeded_target_sections': len(exceeded_target), 'empty_sections': len(empty), 'stats': {'per_level': _group_by_level(sections), 'per_status': _group_by_status(sections), 'level3_stats': {'count': len(level3_sections), 'on_target': len(level3_on_target), 'below_target': len(level3_below), 'exceeded_target': len(level3_exceeded), 'rate': round(len(level3_on_target) / len(level3_sections) * 100, 1) if level3_sections else 0}, 'per_tier_stats': per_tier_stats}}

@router.get('/sections/{project_id}')
async def section_stats(project_id: int, user=Depends(get_current_user)):
    """章节字数明细（2026-10-08：含档位与达标状态）"""
    try:
        await enforce_project_access(project_id, user)
        sections = await _load_sections(project_id)
    except HTTPException:
        raise
    except Exception as e:
        logger.error('加载章节失败: %s', e, exc_info=True)
        raise HTTPException(status_code=500, detail=f'加载章节失败: {str(e)[:200]}') from e

    items = []
    for s in sections:
        effective_target = _resolve_target_for_section(s)
        tier_key = getattr(s, 'word_count_tier', None)
        tier_cfg = get_tier_config(tier_key) if tier_key else get_default_tier_config()
        tier_label = tier_cfg.get('label') if tier_cfg else None
        tier_target = int(tier_cfg.get('target_words', 0)) if tier_cfg else 0
        if effective_target and s.word_count > 0:
            on_target_val = _is_within_tolerance(s)
        elif s.word_count == 0:
            on_target_val = 'unknown'
        else:
            on_target_val = False

        items.append({
            'id': s.id, 'title': s.title, 'level': s.level, 'parent_id': s.parent_id,
            'section_type': s.section_type, 'status': s.status, 'word_count': s.word_count,
            'target_words': effective_target, 'word_count_text': _human_words(s.word_count),
            'progress': _progress(s.word_count, effective_target),
            'word_count_tier': tier_key, 'tier_label': tier_label, 'tier_target': tier_target,
            'on_target': on_target_val,
        })

    items.sort(key=_section_sort_key)
    return {'status': 'ok', 'project_id': project_id, 'total': len(items), 'items': items}


def _target_bounds(s) -> tuple[int, int]:
    """解析章节达标区间 [lo, hi]（与 sse_utils / check_document 同口径）。

    - L3 自身正文（无档位覆盖）：300 ± 20%（默认 240~360），随
      L3_OWN_TARGET_WORDS / L3_OWN_TOLERANCE_RATIO 配置变化。
    - 其他（L3 非叶子四档子树总字数、L4+ 叶子）：target × [0.9, 1.1]。
    """
    target = _resolve_target_for_section(s)
    if not target:
        return (0, 0)
    if target == get_l3_own_target() and _is_l3_own_section(s):
        return get_l3_own_range()
    return (int(target * (1 - 0.10)), int(target * (1 + 0.10)))


def _is_below_target(s) -> bool:
    """字数已生成且低于达标区间下界（无目标或空正文均不算）。"""
    lo, hi = _target_bounds(s)
    return bool(lo) and s.word_count > 0 and s.word_count < lo


def _is_above_target(s) -> bool:
    """字数已生成且高于达标区间上界（无目标或空正文均不算）。"""
    lo, hi = _target_bounds(s)
    return bool(hi) and s.word_count > 0 and s.word_count > hi


def _is_l3_own_section(s) -> bool:
    """判断章节是否属于 L3 自身正文（无下级目录章节、无档位覆盖）。"""
    if (s.level or 3) != 3:
        return False
    if (s.target_words or 0) > 0:
        return False
    return getattr(s, 'word_count_tier', None) in (None, '')


def _resolve_target_for_section(s):
    """解析章节有效目标字数（与 sse_utils._get_effective_target_words 同口径）。"""
    level = s.level or 3
    if level < 3:
        return None
    tw = s.target_words
    if isinstance(tw, int) and tw > 0:
        return tw
    tier_key = getattr(s, 'word_count_tier', None)
    if tier_key:
        cfg = get_tier_config(tier_key)
        if cfg:
            return int(cfg['target_words'])
    if level == 3:
        # L3 自身正文：300 ± 20%（240~360）为唯一口径；仅在存在 L4+ 下级时
        # 四档「子树总字数」口径才生效（SSE 侧已由 word_count_tier 兜住）。
        return get_l3_own_target()
    default_cfg = get_default_tier_config()
    return int(default_cfg.get('target_words', 1200))


def _is_within_tolerance(s) -> bool:
    """判断章节字数是否在达标区间内。

    L3 自身正文：300 ± 20%（默认 240~360，随 L3_OWN_* 配置变化）；
    L3 非叶子（四档子树总字数）与其他层级：±10%。
    """
    lo, hi = _target_bounds(s)
    if not lo or not hi or s.word_count == 0:
        return False
    return lo <= s.word_count <= hi


def _compute_tier_stats(sections) -> dict:
    """按档位分组统计达标率。

    L3 自身正文（300 ± 20%，240~360）：归入 l3_own；
    L3 非叶子（四档子树总字数）与其他层级：按各自档位 ±10%。
    """
    stats = defaultdict(lambda: {'count': 0, 'on_target': 0, 'below': 0, 'exceeded': 0})
    default_cfg = get_default_tier_config()
    default_key = default_cfg.get('key', 'standard')
    for s in sections:
        if (s.level or 3) < 3:
            continue
        tier_key = getattr(s, 'word_count_tier', None) or default_key
        target = _resolve_target_for_section(s)
        if not target:
            continue
        entry = stats[tier_key]
        entry['count'] += 1
        if s.word_count == 0:
            continue
        lo, hi = _target_bounds(s)
        if s.word_count < lo:
            entry['below'] += 1
        elif s.word_count > hi:
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

def _group_by_level(sections) -> dict[str, int]:
    """按章节层级分组统计字数"""
    groups: dict[str, int] = {}
    for s in sections:
        key = f'level_{s.level}'
        groups[key] = groups.get(key, 0) + s.word_count
    return groups

def _group_by_status(sections) -> dict[str, int]:
    """按生成状态分组统计字数"""
    groups: dict[str, int] = {}
    for s in sections:
        key = s.status or 'unknown'
        groups[key] = groups.get(key, 0) + s.word_count
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
            conn.execute(f'UPDATE word_count_tiers SET {", ".join(updates)} WHERE key = ?', tuple(params))

    await asyncio.to_thread(_apply_update)

    updated = get_tier_config(tier_key)
    return {'status': 'ok', 'tier': updated}
