"""字数统计路由

提供 2 个 API 端点：
- GET /word-count/overview/{project_id}   项目总体字数概览
- GET /word-count/sections/{project_id}   章节字数明细（含目标字数对比）

（章节目标字数的设置沿用已有接口：
  PATCH /projects/{projectId}/sections/{sectionId}/target-words）
"""
import asyncio
import logging
from fastapi import APIRouter, Depends, HTTPException
from ..security.auth import get_current_user
from ..services.sections import list_sections
from .project_access import enforce_project_access
logger = logging.getLogger(__name__)
router = APIRouter(prefix='/word-count', tags=['word_count'], dependencies=[Depends(get_current_user)])

async def _load_sections(project_id: int):
    """在**线程池**中读取章节列表（PERF 2026-10-06 修复）。

    BUG（本模块唯一残留的事件循环阻塞点）：本文件两个端点此前在 `async def`
    里**同步直调** `list_sections(project_id)`。该函数并非廉价读：
      ① `get_db()` 默认取**全局写锁**连接 —— 与生成/导出事务争用；
      ② 对**每个章节的完整正文**执行 `_count_words`，大项目单次是
         数十 MB 级文本扫描。
    于是一个纯预检 GET 请求会把整个事件循环卡住数秒，SSE 进度流与其他并发
    请求一并饿死（前端表现为「字数预检转圈时全站无响应」）。
    本项目其余只读端点已于 PERF 2026-10-05 统一改为
    `readonly + asyncio.to_thread`（见 routers/consistency.py、routers/export.py），
    本模块当时被漏掉，本轮补齐，口径一致。
    """
    return await asyncio.to_thread(list_sections, project_id, readonly=True)

@router.get('/overview/{project_id}')
async def overview(project_id: int, user=Depends(get_current_user)):
    """项目总体字数概览

    统计项目所有章节的总字数、目标字数、达标情况，
    按章节层级与生成状态分组汇总。
    """
    try:
        # ENHANCE 2026-09-29（P1 IDOR 收口）：审核/导出四路由统一接入项目属权校验
        await enforce_project_access(project_id, user)
        sections = await _load_sections(project_id)
    except HTTPException:
        raise
    except Exception as e:
        logger.error('加载章节失败: %s', e, exc_info=True)
        raise HTTPException(status_code=500, detail=f'加载章节失败: {str(e)[:200]}') from e
    total_words = sum((s.word_count for s in sections))
    total_target = sum((s.target_words or 0 for s in sections))
    completed = [s for s in sections if s.word_count > 0]
    on_target = [s for s in sections if s.target_words and s.word_count >= s.target_words]
    below_target = [s for s in sections if s.target_words and s.word_count > 0 and (s.word_count < s.target_words)]
    empty = [s for s in sections if s.word_count == 0]
    return {'status': 'ok', 'project_id': project_id, 'total_sections': len(sections), 'total_words': total_words, 'total_target': total_target, 'target_coverage': round(total_words / total_target * 100, 1) if total_target else 0, 'completed_sections': len(completed), 'on_target_sections': len(on_target), 'below_target_sections': len(below_target), 'empty_sections': len(empty), 'stats': {'per_level': _group_by_level(sections), 'per_status': _group_by_status(sections)}}

@router.get('/sections/{project_id}')
async def section_stats(project_id: int, user=Depends(get_current_user)):
    """章节字数明细

    返回每章节的字数与目标字数，供前端表格展示与排序。
    """
    try:
        await enforce_project_access(project_id, user)
        sections = await _load_sections(project_id)
    except HTTPException:
        raise
    except Exception as e:
        logger.error('加载章节失败: %s', e, exc_info=True)
        raise HTTPException(status_code=500, detail=f'加载章节失败: {str(e)[:200]}') from e
    items = [{'id': s.id, 'title': s.title, 'level': s.level, 'parent_id': s.parent_id, 'section_type': s.section_type, 'status': s.status, 'word_count': s.word_count, 'target_words': s.target_words, 'word_count_text': _human_words(s.word_count), 'progress': _progress(s.word_count, s.target_words)} for s in sections]
    # ENHANCE 2026-10-04（排序口径修正）：旧写法以 `(target-words if target else 0)`
    # 作主键、整体 reverse=True —— 未设目标的章节主键恒为 0，会与「刚好达标」
    # (缺口=0) 的章节交错混排。现改为分层：缺字章节(按缺口从大到小) → 已达标章节 →
    # 无目标章节，层内沿用 list_sections 的原始层级顺序（sort 稳定）。
    items.sort(key=_section_sort_key)
    return {'status': 'ok', 'project_id': project_id, 'total': len(items), 'items': items}

def _section_sort_key(item: dict) -> tuple[int, int]:
    """字数明细排序键：(优先级层, -缺口)。层 0=有目标且缺字，1=有目标已达标，2=无目标。

    层内以「缺口从大到小」排序（-deficit 升序即 deficit 降序）；已达标/无目标层
    deficit 不参与排序（统一取 0），由稳定排序保留原始层级顺序。
    """
    target = item.get('target_words') or 0
    word_count = item.get('word_count') or 0
    deficit = target - word_count
    if target and deficit > 0:
        return (0, -deficit)
    if target:
        return (1, 0)
    return (2, 0)

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

def _progress(current: int, target: int | None) -> int:
    """计算目标达成百分比（0-100）"""
    if not target:
        return 100 if current > 0 else 0
    return min(100, round(current / target * 100))
