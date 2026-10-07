"""全文一致性审计路由

提供一致性检查、修复建议、自动修复等功能。
"""
import asyncio
import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Query

from ..security.auth import get_current_user
from ..services.consistency_auditor import consistency_auditor
from .project_access import enforce_project_access


logger = logging.getLogger(__name__)
router = APIRouter(prefix='/consistency', tags=['consistency_audit'], dependencies=[Depends(get_current_user)])

@router.get('/audit/{project_id}')
async def run_audit(project_id: int, user=Depends(get_current_user)):
    """运行完整一致性审计

    Args:
        project_id: 项目ID

    Returns:
        审计结果
    """
    # ENHANCE 2026-09-29（P1 IDOR 收口）：审核/导出四路由统一接入项目属权校验，
    # 与 sections/facts/content_version 同口径；autofix 能改写正文，更不能裸奔。
    await enforce_project_access(project_id, user)
    try:
        # BUG-FIX 2026-09-18（P2）：run_full_audit 是同步重操作（全量读正文 +
        # 事实抽取），直接在事件循环里跑会阻塞整个服务（SSE/其他请求卡死）。
        result = await asyncio.to_thread(consistency_auditor.run_full_audit, project_id)
        return {'status': 'ok', 'project_id': project_id, 'total_sections': result.total_sections, 'facts_extracted': result.facts_extracted, 'conflicts_found': result.conflicts_found, 'conflicts': [{'conflict_id': c.conflict_id, 'fact_name': c.fact_name, 'values': c.values, 'sources': c.sources, 'severity': c.severity, 'description': c.description} for c in result.conflicts], 'audit_time': result.audit_time}
    except Exception as e:
        logger.error('一致性审计失败: %s', e, exc_info=True)
        raise HTTPException(status_code=500, detail=f'审计失败: {e!s}') from e

@router.get('/conflicts/{project_id}')
async def get_conflicts(project_id: int, user=Depends(get_current_user)):
    """获取项目的冲突列表

    Args:
        project_id: 项目ID

    Returns:
        冲突列表
    """
    await enforce_project_access(project_id, user)
    from ..db import get_db

    def _read_conflicts():
        # PERF（2026-10-05）：纯读查询改为 readonly + to_thread，
        # 不再在事件循环内抢全局写锁（旧实现 get_db() 无 readonly，
        # 生成高峰期锁争用会冻结整个 loop，前端 3s 轮询直接卡死）
        with get_db(readonly=True) as conn:
            rows = conn.execute("SELECT * FROM consistency_conflicts \n               WHERE project_id = ?\n               ORDER BY \n                   CASE severity \n                       WHEN 'high' THEN 1 \n                       WHEN 'medium' THEN 2 \n                       WHEN 'low' THEN 3 \n                   END,\n                   created_at DESC", (project_id,)).fetchall()
            conflicts = []
            for row in rows:
                conflict = dict(row)
                # BUG-FIX 2026-09-18（P2）：原 `except (json.JSONDecodeError, Exception)`
                # 等价于裸吞一切异常（Exception 是 JSONDecodeError 的父类超集），
                # 列缺失/类型错误全被压成 warning，前端只看到空 values 无从察觉
                # 数据损坏。收窄为解析类异常，其余照常上报。
                try:
                    conflict['values'] = json.loads(conflict.get('values_json') or '[]')
                    conflict['sources'] = json.loads(conflict.get('sources_json') or '[]')
                except (json.JSONDecodeError, TypeError):
                    logger.warning(f"解析冲突数据失败: {conflict.get('id', 'unknown')}")
                    conflict['values'] = []
                    conflict['sources'] = []
                conflicts.append(conflict)
        return conflicts

    conflicts = await asyncio.to_thread(_read_conflicts)
    return {'status': 'ok', 'count': len(conflicts), 'conflicts': conflicts}

@router.get('/conflicts/{conflict_id}/fix-suggestion')
async def get_fix_suggestion(conflict_id: str, project_id: int, user=Depends(get_current_user)):
    """获取冲突的修复建议

    Args:
        conflict_id: 冲突ID
        project_id: 项目ID

    Returns:
        修复建议
    """
    await enforce_project_access(project_id, user)
    try:
        # P2：suggest_fixes 含 DB 读与正文扫描，移出事件循环
        suggestion = await asyncio.to_thread(
            consistency_auditor.suggest_fixes, conflict_id, project_id
        )
        if suggestion:
            # BUG-FIX 2026-10-06（P1）：同时下发结构化纯目标值。此前前端把
            # `suggestion` 整段文案（“建议将以下内容统一为：450（已确认…）”）塞进
            # new_value 提交 autofix，会把正文旧值替换成说明文字。前端「采纳建议」
            # 按钮现改填 target_value；suggestion 仅作人类可读展示，字段向后兼容。
            target_value = await asyncio.to_thread(
                consistency_auditor.suggest_fix_target_value, conflict_id, project_id
            )
            return {'status': 'ok', 'suggestion': suggestion, 'target_value': target_value}
        else:
            raise HTTPException(status_code=404, detail='未找到修复建议')
    except HTTPException:
        raise
    except Exception as e:
        logger.error('获取修复建议失败: %s', e)
        raise HTTPException(status_code=500, detail=f'获取建议失败: {e!s}') from e

@router.post('/conflicts/{conflict_id}/autofix')
async def apply_autofix(
    conflict_id: str,
    project_id: int,
    new_value: str = Query(..., description='新的统一值'),
    dry_run: bool = Query(False, description='只预览要改什么，不写库'),
    user=Depends(get_current_user),
):
    """应用自动修复：把冲突值真正改写进正文。

    BUG-FIX 2026-09-14（高）：本接口原先只标记冲突为已解决、**从未改写正文**，
    却回「冲突已修复」——用户以为改了，实际没改，问题还被隐藏。
    现在返回真实报告，且**只有确实落文成功才标为已解决**。

    Args:
        conflict_id: 冲突ID
        project_id: 项目ID
        new_value: 统一后的值
        dry_run: 预览模式（前端「预览」按钮用）

    Returns:
        修复报告（changed_sections / total_replacements / sections / skipped / message）
    """
    await enforce_project_access(project_id, user)
    try:
        # BUG-FIX 2026-09-18（P2）：apply_autofix 逐章节正则替换+写库+失效缓存，
        # 同步直调会阻塞事件循环（大项目正文可达数十 MB）。
        report = await asyncio.to_thread(
            consistency_auditor.apply_autofix,
            conflict_id, project_id, new_value, dry_run,
        )
        if not report.get('success'):
            # 未做任何修改（未定位到待替换值 / 有章节无法安全替换）→ 明确告知，不谎报成功
            return {'status': 'no_change', **report}
        return {'status': 'ok', **report}
    except Exception as e:
        logger.error('自动修复失败: %s', e, exc_info=True)
        raise HTTPException(status_code=500, detail=f'修复失败: {e!s}') from e

@router.post('/repair/{project_id}')
async def repair_with_ai(
    project_id: int,
    dry_run: bool = Query(False, description='只算补丁与预览，不写库'),
    section_ids: str | None = Query(None, description='只修指定小节，逗号分隔；留空修全部'),
    user=Depends(get_current_user),
):
    """AI 补丁修复：针对语义级事实冲突产出局部补丁并落文。

    ENHANCE 2026-09-14：移植自易标 `contentGenerationTask.cjs` 的 auditing 阶段。
    与 `/conflicts/{id}/autofix`（确定性取值统一）互补：
    本接口处理「同一事实用不同措辞表达」的冲突，确定性替换无能为力。

    AI 只产出 `{old_text, new_text}` 局部补丁，**正文由宿主改写**；
    编造的 `old_text` 会被唯一性校验拒绝，幻觉不会污染正文。

    Args:
        project_id: 项目ID
        dry_run: 预览模式
        section_ids: 逗号分隔的小节 ID 列表

    Returns:
        修复报告（patches / applied / rejected / sections / unresolved / conflict_states）
    """
    await enforce_project_access(project_id, user)
    from ..services.consistency_repair import repair_with_ai as _repair

    parsed_ids: list[int] | None = None
    if section_ids:
        try:
            parsed_ids = [int(part) for part in section_ids.split(',') if part.strip()]
        except ValueError as e:
            raise HTTPException(status_code=400, detail='section_ids 必须是逗号分隔的整数') from e

    try:
        report = await _repair(project_id, dry_run=dry_run, section_ids=parsed_ids)
    except Exception as e:
        logger.error('AI 一致性修复失败: %s', e, exc_info=True)
        raise HTTPException(status_code=500, detail=f'修复失败: {e!s}') from e

    return {'status': 'ok' if report.get('success') else 'no_change', **report}


@router.get('/summary/{project_id}')
async def get_audit_summary(project_id: int, user=Depends(get_current_user)):
    """获取审计摘要

    Args:
        project_id: 项目ID

    Returns:
        审计摘要信息
    """
    await enforce_project_access(project_id, user)
    from ..db import get_db

    def _read_summary():
        # PERF（2026-10-05）：纯读改 readonly + to_thread，不在 loop 内抢写锁
        with get_db(readonly=True) as conn:
            latest = conn.execute('SELECT * FROM consistency_audit_log \n               WHERE project_id = ?\n               ORDER BY audit_time DESC LIMIT 1', (project_id,)).fetchone()
            unresolved = conn.execute('SELECT COUNT(*) as cnt FROM consistency_conflicts \n               WHERE project_id = ? AND resolved = 0', (project_id,)).fetchone()['cnt']
            by_severity = conn.execute('SELECT severity, COUNT(*) as cnt FROM consistency_conflicts \n               WHERE project_id = ?\n               GROUP BY severity', (project_id,)).fetchall()
            severity_counts = {}
            for row in by_severity:
                severity_counts[row['severity']] = row['cnt']
        return {'latest_audit': dict(latest) if latest else None, 'unresolved_conflicts': unresolved, 'conflicts_by_severity': severity_counts}

    summary = await asyncio.to_thread(_read_summary)
    return {'status': 'ok', 'summary': summary}


@router.get('/scoring-compliance/{project_id}')
async def scoring_compliance(
    project_id: int,
    persist: bool = Query(True, description='是否把本次结果写入审计历史'),
    coverage_weak: float = Query(0.10, ge=0.0, le=1.0, description='低于该覆盖率判 weak'),
    user=Depends(get_current_user),
):
    """评分标准达标对齐审计（ENHANCE 2026-10-06 新增）

    与既有 `uncovered_scoring_points`（导出预检）的分工：

    - 导出预检：判**存在性** —— 评分点有没有被章节挂载（有没有写到这一章）
    - 本端点：判**达标性** —— 挂载了的那一章正文里，评分标准要求的内容
      到底写没写（有没有真正答到点上）

    举例：评分点「施工组织设计」要求「须含施工工艺流程图并附 BIM 模型」，
    章节正文只写「本文介绍施工工艺」时，预检判为已覆盖，本端点判为 weak。

    确定性实现（字符 n-gram 重合度），不调 AI、无超时、结果可复现；
    返回每个评分点的原始 coverage 数字，便于按更严标准自行解读。

    Args:
        project_id: 项目ID
        persist: 是否落库到 scoring_compliance_audit 审计历史
        coverage_weak: 低于该覆盖率判 weak（默认 0.10，刻意保守以零假阳性优先）

    Returns:
        {status, project_id, audit_time, total_points, unmapped, empty, weak,
         covered, gaps:[{scoring_point_id,name,weight,standard,status,coverage,
         section_ids,section_titles,content_chars,detail}], warnings}
    """
    await enforce_project_access(project_id, user)
    from ..services.scoring_compliance import (
        audit_scoring_compliance,
        save_report,
    )

    try:
        report = await asyncio.to_thread(
            audit_scoring_compliance, project_id, coverage_weak=coverage_weak
        )
        if persist:
            await asyncio.to_thread(save_report, report)
        return {'status': 'ok', **report.to_dict()}
    except Exception as e:
        logger.error('评分标准达标对齐失败: %s', e, exc_info=True)
        raise HTTPException(status_code=500, detail=f'达标对齐审计失败: {e!s}') from e
