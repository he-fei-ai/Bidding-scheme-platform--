import asyncio
import json
import logging
from typing import Annotated
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from ..db import get_db
from ..services.ai.provider_factory import get_ai_config, get_working_provider
from ..services.ai.workflows_analysis import analyze_boq as wbq_analyze_boq, analyze_construction_method as wbq_analyze_construction_method, analyze_work_interface as wbq_analyze_work_interface, extract_global_facts, extract_scoring_points, generate_tender_summary
from ..services.facts_service import parse_stored_items
from ..services.files import get_boq_file_text, get_construction_method_file_text, get_work_interface_file_text, latest_extracted_text
from ..services.projects import update_step
from ..services.tender_context import extract_tender_context_async
from ..security.auth import get_current_user
from .project_access import enforce_project_access
_ALLOWED_FIELD_NAMES = frozenset({'summary_json', 'scoring_points_json', 'boq_json', 'work_interface_json', 'construction_method_json', 'global_facts_json', 'scope', 'confirmed_summary', 'confirmed_scoring', 'confirmed_boq', 'confirmed_work_interface', 'confirmed_construction_method', 'confirmed_facts'})

#: analysis 表可被置位为「已确认」的列白名单（confirmed_* 布尔标记）。
#: BUG-FIX 2026-10-05（P1 安全/数据完整性）：`_sync_confirm_field` 与
#: `_sync_confirm_and_retrieve` 都把「确认标记列名」直接 f-string 插进 SQL
#: （`UPDATE analysis SET {field} = 1`）。`_sync_confirm_field` 此前**完全没有**
#: 校验，`_sync_confirm_and_retrieve` 只校验了 `field` 本体、未校验由其派生出的
#: `confirmed_col`。现有调用方传的都是字面量，暂无实际可利用路径，但这两处是
#: 「已校验字段名 → 拼进 SQL 标识符」的模板，一旦新增调用方（如把
#: `body.field` 透传进来）就是直接的 SQL 注入；而且 `field='scope'` 这类合法值
#: 会派生出 `confirmed_scope` 这个**不存在的列**，直接 500。统一收敛到
#: 「确认列白名单」校验，与 `_validate_field_name` 同一防御口径。
_ALLOWED_CONFIRMED_FIELDS = frozenset({
    'confirmed_summary', 'confirmed_scoring', 'confirmed_boq',
    'confirmed_work_interface', 'confirmed_construction_method', 'confirmed_facts',
})

def _validate_field_name(field: str) -> str:
    """校验字段名合法性，防止 SQL 注入"""
    if field not in _ALLOWED_FIELD_NAMES:
        raise ValueError(f'非法字段名: {field}')
    return field

def _validate_confirmed_field(field: str) -> str:
    """校验「确认标记列」名合法性（SQL 标识符白名单）"""
    if field not in _ALLOWED_CONFIRMED_FIELDS:
        raise ValueError(f'非法确认字段名: {field}')
    return field

#: 业务字段 → 「已确认」标记列的显式映射。未在此表中的字段一律视为
#: 「无确认标记」，不触碰任何 confirmed 列（见 _derive_confirmed_field）。
_FIELD_TO_CONFIRMED: dict[str, str] = {
    'summary_json': 'confirmed_summary',
    'scoring_points_json': 'confirmed_scoring',
    'boq_json': 'confirmed_boq',
    'work_interface_json': 'confirmed_work_interface',
    'construction_method_json': 'confirmed_construction_method',
    'global_facts_json': 'confirmed_facts',
}

def _derive_confirmed_field(field: str) -> str | None:
    """由业务字段推导其「已确认」标记列；无对应列时返回 None（不派生）。

    BUG-FIX 2026-10-05：此前用 `f"confirmed_{field.replace('_json','')}"` 盲派生，
    `scope`/`facts_policy` 等合法字段会产出库里不存在的列名并被拼进 SQL。
    现改为显式映射 + 白名单双保险：映射不到或派生值不在白名单内即视为无确认列。
    """
    derived = _FIELD_TO_CONFIRMED.get(field)
    if derived is None:
        return None
    if derived not in _ALLOWED_CONFIRMED_FIELDS:
        raise ValueError(f'确认字段映射异常: {field} -> {derived}')
    return derived

def _sync_get_analysis(project_id: int) -> dict | None:
    with get_db() as conn:
        # ENHANCE 2026-09-29（facts-ns）：补读 facts_policy。Step02 评分流程的自动事实
        # 提取以前拿不到用户所选缺值策略，永远按 fabricate 运行。
        row = conn.execute('SELECT summary_json, scoring_points_json, confirmed_summary, confirmed_scoring, boq_json, work_interface_json, construction_method_json, confirmed_boq, confirmed_work_interface, confirmed_construction_method, global_facts_json, confirmed_facts, scope, COALESCE(facts_policy, \'fabricate\') AS facts_policy FROM analysis WHERE project_id = ?', (project_id,)).fetchone()
        return dict(row) if row else None

def _sync_update_scope(project_id: int, scope: str) -> None:
    with get_db() as conn:
        existing = conn.execute('SELECT 1 FROM analysis WHERE project_id = ?', (project_id,)).fetchone()
        if existing:
            conn.execute('UPDATE analysis SET scope = ? WHERE project_id = ?', (scope, project_id))
        else:
            conn.execute('INSERT INTO analysis (project_id, scope) VALUES (?, ?)', (project_id, scope))

def _sync_update_field(project_id: int, field: str, value: str) -> None:
    field = _validate_field_name(field)
    with get_db() as conn:
        # BUG-FIX 2026-09-14（中）：analysis 行不存在时纯 UPDATE 影响 0 行，
        # 写入被静默丢弃。改为 upsert（与 _sync_insert_or_update 保持一致）。
        conn.execute(
            f'INSERT INTO analysis (project_id, {field}) VALUES (?, ?) '
            f'ON CONFLICT(project_id) DO UPDATE SET {field} = excluded.{field}',
            (project_id, value),
        )

def _sync_reset_facts_audit(project_id: int) -> None:
    """抹除历史事实审计结果（facts_audit_json / facts_audit_at）。

    BUG-FIX 2026-09-29（跨模块：提取项目→全局事实审计口径）：清除分析或重跑评分
    覆盖 global_facts_json 后，库里针对旧事实集的审计结论仍会被 GET /facts 回看成
    「本次事实的一致性结果」（与 facts 路由 POST /facts 的作废口径不一致）。
    列可能在旧库尚未迁移，先 PRAGMA 判存在再写。

    约定「事实基准 global_facts_json 变更 → confirmed_facts 回退 + facts_audit_*
    抹除」共 4 处强制点，改口径须四处同改：
      ① routers/facts.py POST /facts 手工保存
      ② routers/facts.py 重新提取端点 _persist
      ③ services/consistency_backflow.py accept_fact_proposals（审计回流采纳）
      ④ 本函数（清除分析 / 重跑评分），由 _sync_clear_analysis 调用
    """
    with get_db() as conn:
        cols = {row['name'] for row in conn.execute('PRAGMA table_info(analysis)').fetchall()}
        if 'facts_audit_json' not in cols:
            return
        if conn.execute('SELECT 1 FROM analysis WHERE project_id = ?', (project_id,)).fetchone():
            conn.execute("UPDATE analysis SET facts_audit_json = '', facts_audit_at = NULL WHERE project_id = ?", (project_id,))

def _sync_clear_analysis(project_id: int) -> None:
    with get_db() as conn:
        existing = conn.execute('SELECT 1 FROM analysis WHERE project_id = ?', (project_id,)).fetchone()
        # ENHANCE 2026-10-07：重置链路同步清空结构化提取项目字段表
        # （列可能尚未迁移的老库，做存在性守卫）
        cols = {row['name'] for row in conn.execute('PRAGMA table_info(analysis)').fetchall()}
        _ef_clear = ", extraction_fields_json = ''" if 'extraction_fields_json' in cols else ''
        if existing:
            conn.execute(f"UPDATE analysis SET\n                   summary_json = '', scoring_points_json = '[]',\n                   confirmed_summary = 0, confirmed_scoring = 0,\n                   boq_json = '[]', work_interface_json = '[]', construction_method_json = '[]',\n                   confirmed_boq = 0, confirmed_work_interface = 0, confirmed_construction_method = 0,\n                   global_facts_json = '[]', confirmed_facts = 0,\n                   scope = NULL{_ef_clear}, updated_at = datetime('now', 'localtime')\n                   WHERE project_id = ?", (project_id,))
        else:
            conn.execute("INSERT INTO analysis\n                   (project_id, summary_json, scoring_points_json,\n                    confirmed_summary, confirmed_scoring)\n                   VALUES (?, '', '[]', 0, 0)", (project_id,))
    # 事实集已清空，旧审计同步作废（新建行分支无审计可残，无需处理）
    if existing:
        _sync_reset_facts_audit(project_id)

def _sync_read_summary(project_id: int) -> tuple[bool, str]:
    """返回 (has_cached, summary_json)"""
    with get_db() as conn:
        row = conn.execute('SELECT summary_json FROM analysis WHERE project_id = ?', (project_id,)).fetchone()
        if row and row['summary_json']:
            return (True, row['summary_json'])
    return (False, '')

def _sync_read_scoring_and_summary(project_id: int) -> tuple[bool, str, str]:
    """返回 (has_cached, scoring_json, summary_json)"""
    with get_db() as conn:
        row = conn.execute('SELECT scoring_points_json, summary_json FROM analysis WHERE project_id = ?', (project_id,)).fetchone()
        if row:
            return (row['scoring_points_json'] is not None and bool(row['scoring_points_json']), row['scoring_points_json'] or '', row['summary_json'] or '')
    return (False, '', '')

def _sync_insert_or_update(project_id: int, summary: str, field: str) -> None:
    """INSERT OR REPLACE 单个字段"""
    field = _validate_field_name(field)
    # BUG-FIX 2026-10-05：派生列名同样必须过白名单。此前 `field='scope'` 这类
    # 合法值会派生出不存在的 `confirmed_scope` 列，直接 500；且该派生值未经
    # 校验就拼进 SQL 标识符。没有对应「已确认」标记的字段不触碰任何 confirmed 列。
    confirmed_col = _derive_confirmed_field(field)
    with get_db() as conn:
        if confirmed_col:
            conn.execute(f'INSERT INTO analysis (project_id, {field}) VALUES (?, ?)\n                ON CONFLICT(project_id) DO UPDATE SET {field} = excluded.{field},\n                {confirmed_col} = 0', (project_id, summary))
        else:
            conn.execute(f'INSERT INTO analysis (project_id, {field}) VALUES (?, ?)\n                ON CONFLICT(project_id) DO UPDATE SET {field} = excluded.{field}', (project_id, summary))

def _sync_read_boq(project_id: int) -> str | None:
    with get_db() as conn:
        row = conn.execute('SELECT boq_json FROM analysis WHERE project_id = ?', (project_id,)).fetchone()
        return row['boq_json'] if row and row['boq_json'] else None

def _sync_read_work_interface(project_id: int) -> str | None:
    with get_db() as conn:
        row = conn.execute('SELECT work_interface_json FROM analysis WHERE project_id = ?', (project_id,)).fetchone()
        return row['work_interface_json'] if row and row['work_interface_json'] else None

def _sync_read_construction_method(project_id: int) -> str | None:
    with get_db() as conn:
        row = conn.execute('SELECT construction_method_json FROM analysis WHERE project_id = ?', (project_id,)).fetchone()
        return row['construction_method_json'] if row and row['construction_method_json'] else None

def _sync_confirm_field(project_id: int, field: str) -> None:
    # BUG-FIX 2026-10-05：见 _ALLOWED_CONFIRMED_FIELDS 注释。此前无校验，
    # 属「未校验标识符直接进 SQL」的注入模板。
    _validate_confirmed_field(field)
    with get_db() as conn:
        conn.execute(f'UPDATE analysis SET {field} = 1 WHERE project_id = ?', (project_id,))

def _sync_select_flags(project_id: int, *fields: str) -> tuple | None:
    validated_fields = [_validate_field_name(f) for f in fields]
    cols = ', '.join(validated_fields)
    with get_db() as conn:
        row = conn.execute(f'SELECT {cols} FROM analysis WHERE project_id = ?', (project_id,)).fetchone()
        return row
logger = logging.getLogger(__name__)
# ENHANCE 2026-09-29（P1 IDOR 收口·补齐链首）：提取项目（analysis）全部端点接入
# 与 sections 同口径的属权校验：此前任意登录用户可覆写/清空他人项目的摘要、
# 评分点、全局事实与 BOQ 分析结果（下游目录/正文/审计/导出全部受影响）。
# 默认行为兼容：AUTH_ENABLED=False 时 mock admin，历史项目 created_by=NULL 放行。
router = APIRouter(prefix='/projects/{project_id}/ai', tags=['ai'], dependencies=[Depends(get_current_user)])

class SummaryConfirm(BaseModel):
    summary_json: str

class ScoringConfirm(BaseModel):
    scoring_points_json: str

@router.get('/analysis')
async def get_analysis(project_id: int, current_user: Annotated[object, Depends(get_current_user)]=None):
    """获取分析结果"""
    await enforce_project_access(project_id, current_user)
    row = await asyncio.to_thread(_sync_get_analysis, project_id)
    if not row:
        # BUG-FIX 2026-09-29（返回形态不统一）：无行分支此前缺 `global_facts_items`
        # 与 `facts_policy` 两键，而有行分支都有 —— 同一端点在「analysis 行存在与否」
        # 两种状态下形态不同，消费方（测试/集成调用）按有行形态读键会 KeyError。
        # 补齐为与有行分支一致的默认形态。
        return {'summary_json': '', 'scoring_points_json': '[]', 'confirmed_summary': False, 'confirmed_scoring': False, 'boq_json': '[]', 'work_interface_json': '[]', 'construction_method_json': '[]', 'confirmed_boq': False, 'confirmed_work_interface': False, 'confirmed_construction_method': False, 'global_facts_json': '[]', 'global_facts_items': [], 'confirmed_facts': False, 'scope': '', 'facts_policy': 'fabricate'}
    result = dict(row)
    scoring_json = result.get('scoring_points_json', '[]')
    if scoring_json and scoring_json != '[]':
        from ..services.ai.workflows_analysis import _extract_array_from_response, normalize_writing_targets
        from ..utils.json_parser import extract_json_from_text as parse_json_response
        try:
            parsed = parse_json_response(scoring_json)
            points = _extract_array_from_response(parsed)
            normalized = normalize_writing_targets(points)
            if normalized:
                result['scoring_points_json'] = json.dumps(normalized, ensure_ascii=False, indent=2)
        except Exception as _e:
            logger.debug('[silent-except] ai.py: line 251 - %s', _e)
    result['boq_json'] = row['boq_json'] if 'boq_json' in row.keys() else '[]'
    result['work_interface_json'] = row['work_interface_json'] if 'work_interface_json' in row.keys() else '[]'
    result['construction_method_json'] = row['construction_method_json'] if 'construction_method_json' in row.keys() else '[]'
    result['confirmed_boq'] = bool(row['confirmed_boq']) if 'confirmed_boq' in row.keys() else False
    result['confirmed_work_interface'] = bool(row['confirmed_work_interface']) if 'confirmed_work_interface' in row.keys() else False
    result['confirmed_construction_method'] = bool(row['confirmed_construction_method']) if 'confirmed_construction_method' in row.keys() else False
    gf_json = result.get('global_facts_json') or '[]'
    try:
        if isinstance(gf_json, str):
            gf_data = json.loads(gf_json)
            result['global_facts_items'] = gf_data if isinstance(gf_data, list) else []
        else:
            result['global_facts_items'] = gf_json if isinstance(gf_json, list) else []
    except Exception:
        result['global_facts_items'] = []
    result['confirmed_facts'] = bool(row['confirmed_facts']) if 'confirmed_facts' in row.keys() else False
    return result

class ScopeUpdate(BaseModel):
    scope: str

@router.patch('/analysis/scope')
async def save_project_scope(project_id: int, data: ScopeUpdate, current_user: Annotated[object, Depends(get_current_user)]=None):
    """保存项目承包范围描述"""
    await enforce_project_access(project_id, current_user)
    await asyncio.to_thread(_sync_update_scope, project_id, data.scope)
    return {'status': 'ok'}

@router.post('/summary')
async def generate_summary(project_id: int, current_user: Annotated[object, Depends(get_current_user)]=None):
    """生成招标摘要（真实 AI 调用，I/O 与 DB 锁分离）"""
    await enforce_project_access(project_id, current_user)
    full_text = await latest_extracted_text(project_id)
    if not full_text:
        raise HTTPException(status_code=400, detail='请先上传文件')
    tender_context = await extract_tender_context_async(full_text)
    has_cached, cached = await asyncio.to_thread(_sync_read_summary, project_id)
    if has_cached:
        if '将在后续阶段完善' not in cached and len(cached) > 50:
            return {'summary_json': cached}
    provider, used_fb = await get_working_provider(get_ai_config())
    if used_fb:
        logger.info('AI摘要生成已降级到兜底提供商: %s', used_fb)
    try:
        summary = await generate_tender_summary(provider, tender_context)
    except Exception as e:
        # BUG-FIX 2026-09-18（中）：原只捕获 RuntimeError，而 `with_timeout` 抛的是
        # **内置 TimeoutError**（OSError 子类，不是 RuntimeError 子类），provider 的
        # 网络异常（httpx.ConnectError 等）同样不在其内 → AI 超时/网络故障时异常直接
        # 冒泡，前端拿到 500 而非 502 友好提示（同型问题见 BUG-FIX 2026-09-16 对
        # workflows_analysis 的修复）。`asyncio.CancelledError` 属 BaseException，
        # 不会被误吞，取消语义不受影响。
        logger.exception('AI生成摘要失败: %s', e)
        raise HTTPException(status_code=502, detail=f'AI服务调用失败: {str(e)[:200]} | 请检查AI配置或稍后重试') from e
    await asyncio.to_thread(_sync_insert_or_update, project_id, summary, 'summary_json')
    return {'summary_json': summary}

@router.post('/scoring')
async def generate_scoring(project_id: int, current_user: Annotated[object, Depends(get_current_user)]=None):
    """识别评分点（真实 AI 调用，并行化 summary+scoring 提取阶段）"""
    await enforce_project_access(project_id, current_user)
    full_text = await latest_extracted_text(project_id)
    if not full_text:
        raise HTTPException(status_code=400, detail='请先上传文件')
    tender_context = await extract_tender_context_async(full_text)
    has_cached, cached, summary = await asyncio.to_thread(_sync_read_scoring_and_summary, project_id)
    cached_scoring_json = None
    if cached and cached != '[]' and (cached != '{}') and ('待AI解析' not in cached) and (len(cached) > 10):
        from ..services.ai.workflows_analysis import _extract_array_from_response, normalize_writing_targets
        from ..utils.json_parser import extract_json_from_text as parse_json_response
        try:
            parsed = parse_json_response(cached)
            points = _extract_array_from_response(parsed)
            normalized = normalize_writing_targets(points)
            if normalized and len(normalized) > 0:
                cached_scoring_json = json.dumps(normalized, ensure_ascii=False, indent=2)
                # BUG-FIX 2026-10-09（确认态谎报）：此处原来调 `_sync_update_field`
                # （普通 upsert，**不复位** confirmed 列），而 fresh 路径调的是
                # `_sync_insert_or_update`（会把 confirmed_* 复位为 0）。
                # 两条路径对「评分点已被改写」这一事实的处理不一致：
                # 用户点一次「识别评分点」即重写了数据，却仍被标记为「已确认」，
                # 于是「下一步：生成目录」的闸门（前端依赖 confirmed_scoring）
                # 在数据已变的情况下依然放行，目录基于**用户从未确认过的**
                # 评分点生成。
                # 改为复用 `_sync_insert_or_update`，与 fresh 路径语义一致。
                # 向后兼容：仅在评分点内容真的被重写时才复位 confirmed_scoring；
                # 内容与库中完全一致时不触碰（幂等重入不打断已确认状态）。
                if json.dumps(normalized, ensure_ascii=False, indent=2) != cached:
                    await asyncio.to_thread(
                        _sync_insert_or_update, project_id, cached_scoring_json, 'scoring_points_json'
                    )
        except Exception as _e:
            logger.debug('[silent-except] ai.py: line 370 - %s', _e)
    if cached_scoring_json:
        analysis_row = await asyncio.to_thread(_sync_get_analysis, project_id)
        # BUG-FIX 2026-09-29（读取口径不统一）：这里以前自己 `json.loads` 且只认
        # 顶层 list，与正文生成链路不同口径 —— 包装形态下前端拿到空事实列表，
        # 但正文却能读到事实，表达为「刷新后事实又消失了」。
        global_facts = parse_stored_items(analysis_row['global_facts_json']) if analysis_row else []
        return {'scoring_points_json': cached_scoring_json, 'summary_json': summary, 'global_facts': global_facts}
    need_regenerate = not summary or summary == '{}' or len(summary) < 20
    provider, used_fb = await get_working_provider(get_ai_config())
    if used_fb:
        logger.info('AI评分点识别已降级到兜底提供商: %s', used_fb)
    try:
        if need_regenerate:
            summary = await generate_tender_summary(provider, tender_context)
        scoring_points = await extract_scoring_points(provider, tender_context, summary)
    except Exception as e:
        # BUG-FIX 2026-09-18（中）：原为 `(RuntimeError, ValueError)`，捕获不到
        # `with_timeout` 抛的内置 TimeoutError 与 provider 网络异常 → AI 超时时
        # 前端拿到 500 而非 502 友好提示。放宽为 Exception（CancelledError 不受影响）。
        logger.exception('AI识别评分点失败: %s', e)
        raise HTTPException(status_code=502, detail=f'AI服务调用失败: {str(e)[:200]} | 请检查AI配置或稍后重试') from e
    if not scoring_points or len(scoring_points) == 0:
        # BUG-FIX 2026-10-09（错误分类）：`extract_scoring_points` 在第一轮失败时
        # 吞掉异常并返回 []（workflows_analysis.py:149-151），此处据此抛 500
        # 「AI 未能识别出评分点，请重试」——把一次**上游 AI 调用/网络故障**
        # 伪装成「识别结果为空」。本路由其余所有 AI 失败路径均映射为 502，
        # 此处口径不一致，前端会给出误导性的「检查招标文件内容」提示。
        # 统一为 502，并在 detail 里点明可能的两种原因。
        raise HTTPException(status_code=502, detail='AI服务调用失败或未能识别出评分点，请检查AI配置、招标文件内容或稍后重试')
    # BUG-FIX 2026-10-09（死表达式）：原为 `cached_scoring_json or json.dumps(...)`。
    # 但 `cached_scoring_json` 在此行必然为 None —— 若为真，函数早在 :299 就
    # return 了。该表达式纯属误导（读起来像会复用缓存，实际永远不会）。
    scoring_json = json.dumps(scoring_points, ensure_ascii=False, indent=2)
    # BUG-FIX 2026-10-09（数据丢失 / 重复付费）：`:312` 可能刚刚调用
    # `generate_tender_summary` 生成了新摘要（need_regenerate 为真时），
    # 但只把 scoring_points_json 落库，**摘要从未写回 DB**。
    # 于是下一次点「识别评分点」时 need_regenerate 再次为真，重复付费调用
    # AI 生成同一份摘要，且刷新页面后摘要仍是旧的「待AI解析」占位。
    # 现把新摘要一并落库（`_sync_insert_or_update` 内部会复位 confirmed_summary，
    # 与评分点重跑语义一致）。
    if need_regenerate:
        try:
            await asyncio.to_thread(
                _sync_insert_or_update,
                project_id,
                json.dumps(summary, ensure_ascii=False, indent=2),
                'summary_json',
            )
        except Exception as _e:
            # 摘要落库失败不应连带回滚已成功的评分点结果
            logger.warning('[评分] 摘要落库失败（忽略，评分点已保存）: %s', _e)
    await asyncio.to_thread(_sync_insert_or_update, project_id, scoring_json, 'scoring_points_json')
    # BUG-FIX 2026-10-11（I-9）：`extract_global_facts` 原在 try 之外，
    # :364 已把 scoring_points_json 落库；若此处抛异常（provider 502/超时/网络错误），
    # 整段 500 冒到前端，但评分点已入库 —— 前端展示的是「失败」而 DB 已有新评分点，
    # 下次点识别评分点会因缓存直接返回旧评分点、用户误以为「我上次成功了但页面崩了」。
    # 现把事实提取包裹，异常时降级为 global_facts=[] 返回，评分点已保存不受影响。
    try:
        _analysis_row = await asyncio.to_thread(_sync_get_analysis, project_id)
        global_facts = await extract_global_facts(provider, tender_context, summary, work_interface=_analysis_row['work_interface_json'] if _analysis_row else '', construction_method=_analysis_row['construction_method_json'] if _analysis_row else '', policy=(_analysis_row or {}).get('facts_policy') or 'fabricate')
        logger.info('[评分] 提取到全局事实 %d 项', len(global_facts))
        if global_facts:
            # BUG-FIX 2026-09-18（P2）：原评分流程无条件覆写 global_facts_json，
            # 用户已确认（confirmed_facts=1）并人工修订过的事实在重跑评分时被
            # 新提取结果静默覆盖，且 _sync_insert_or_update 会把 confirmed_facts
            # 标记打回。现检测到已确认即跳过覆盖，保护人工定稿成果。
            if _analysis_row and _analysis_row.get('confirmed_facts'):
                logger.info('[全局事实] 用户已确认事实(confirmed_facts=1)，跳过评分流程覆盖, project=%d', project_id)
                # 返回值同步改回 DB 中已确认事实，避免前端展示与 DB 不一致
                global_facts = parse_stored_items(_analysis_row.get('global_facts_json') or '[]')
            else:
                facts_json = json.dumps(global_facts, ensure_ascii=False, indent=2)
                await asyncio.to_thread(_sync_insert_or_update, project_id, facts_json, 'global_facts_json')
                # BUG-FIX 2026-09-29：事实被新提取结果整体覆盖，旧审计随之作废
                await asyncio.to_thread(_sync_reset_facts_audit, project_id)
                logger.info('[评分] 保存 global_facts 到 DB, 项数=%d', len(global_facts))
        else:
            logger.info('[全局事实] 评分模块提取为空，跳过保存')
    except Exception as _facts_e:
        logger.error('[评分] 提取全局事实失败（评分点已保存，不阻断返回）: %s', _facts_e)
        global_facts = []
    return {'scoring_points_json': scoring_json, 'summary_json': summary, 'global_facts': global_facts}

@router.post('/summary/confirm')
async def confirm_summary(project_id: int, data: SummaryConfirm, current_user: Annotated[object, Depends(get_current_user)]=None):
    """确认/编辑摘要"""
    await enforce_project_access(project_id, current_user)
    await asyncio.to_thread(_sync_confirm_and_retrieve, project_id, 'summary_json', data.summary_json)
    row = await asyncio.to_thread(_sync_select_flags, project_id, 'confirmed_summary', 'confirmed_scoring')
    if row and row['confirmed_summary'] and row['confirmed_scoring']:
        # PERF 2026-09-18（P2）：同步 DB 写移入线程池，避免阻塞事件循环
        await asyncio.to_thread(update_step, project_id, 3)
    return {'status': 'ok'}

def _sync_confirm_and_retrieve(project_id: int, field: str, value: str) -> None:
    field = _validate_field_name(field)
    # BUG-FIX 2026-10-05：见 _derive_confirmed_field 注释。
    confirmed_col = _derive_confirmed_field(field)
    with get_db() as conn:
        existing = conn.execute('SELECT 1 FROM analysis WHERE project_id = ?', (project_id,)).fetchone()
        if existing:
            if confirmed_col:
                conn.execute(f"UPDATE analysis SET {field} = ?, {confirmed_col} = 1, updated_at = datetime('now', 'localtime') WHERE project_id = ?", (value, project_id))
            else:
                conn.execute(f"UPDATE analysis SET {field} = ?, updated_at = datetime('now', 'localtime') WHERE project_id = ?", (value, project_id))
        else:
            if confirmed_col:
                conn.execute(f'INSERT INTO analysis (project_id, {field}, {confirmed_col}) VALUES (?, ?, 1)', (project_id, value))
            else:
                conn.execute(f'INSERT INTO analysis (project_id, {field}) VALUES (?, ?)', (project_id, value))

@router.post('/scoring/confirm')
async def confirm_scoring(project_id: int, data: ScoringConfirm, current_user: Annotated[object, Depends(get_current_user)]=None):
    """确认/编辑评分点"""
    await enforce_project_access(project_id, current_user)
    await asyncio.to_thread(_sync_confirm_and_retrieve, project_id, 'scoring_points_json', data.scoring_points_json)
    row = await asyncio.to_thread(_sync_select_flags, project_id, 'confirmed_summary', 'confirmed_scoring')
    if row and row['confirmed_summary'] and row['confirmed_scoring']:
        # PERF 2026-09-18（P2）：同步 DB 写移入线程池，避免阻塞事件循环
        await asyncio.to_thread(update_step, project_id, 3)
    return {'status': 'ok'}

@router.post('/boq')
async def analyze_boq(project_id: int, current_user: Annotated[object, Depends(get_current_user)]=None):
    """分析清单工程量（优先使用清单文件，未找到时降级使用招标文件）"""
    await enforce_project_access(project_id, current_user)
    cached = await asyncio.to_thread(_sync_read_boq, project_id)
    if cached and cached != '[]' and (len(cached) > 10):
        return {'boq_json': cached}
    boq_text = await asyncio.to_thread(get_boq_file_text, project_id)
    if boq_text and boq_text.strip():
        logger.info('[AI清单] 使用清单文件进行分析，长度: %d 字符', len(boq_text))
        analysis_text = boq_text
    else:
        full_text = await latest_extracted_text(project_id)
        if not full_text:
            raise HTTPException(status_code=400, detail='请先上传文件')
        logger.info('[AI清单] 未找到清单文件，降级使用招标文件进行分析，长度: %d 字符', len(full_text))
        tender_context = await extract_tender_context_async(full_text)
        analysis_text = tender_context
    provider, used_fb = await get_working_provider(get_ai_config())
    if used_fb:
        logger.info('AI清单工程量分析已降级到兜底提供商: %s', used_fb)
    try:
        result = await wbq_analyze_boq(provider, analysis_text)
    except Exception as e:
        # BUG-FIX 2026-09-18（中）：同 generate_summary —— 放宽异常范围，
        # 超时/网络类异常也要转成 502 友好提示而不是裸 500。
        logger.exception('AI分析清单工程量失败: %s', e)
        raise HTTPException(status_code=502, detail=f'AI服务调用失败: {str(e)[:200]} | 请检查AI配置或稍后重试') from e
    result_json = json.dumps(result, ensure_ascii=False, indent=2)
    await asyncio.to_thread(_sync_insert_or_update, project_id, result_json, 'boq_json')
    return {'boq_json': result_json}

@router.post('/work-interface')
async def analyze_work_interface(project_id: int, current_user: Annotated[object, Depends(get_current_user)]=None):
    """分析工作界面（优先使用工作界面文件，未找到时降级使用招标文件）"""
    await enforce_project_access(project_id, current_user)
    cached = await asyncio.to_thread(_sync_read_work_interface, project_id)
    if cached and cached != '[]' and (len(cached) > 10):
        return {'work_interface_json': cached}
    wi_text = await asyncio.to_thread(get_work_interface_file_text, project_id)
    if wi_text and wi_text.strip():
        logger.info('[AI界面] 使用工作界面文件进行分析，长度: %d 字符', len(wi_text))
        analysis_text = wi_text
    else:
        full_text = await latest_extracted_text(project_id)
        if not full_text:
            raise HTTPException(status_code=400, detail='请先上传文件')
        logger.info('[AI界面] 未找到工作界面文件，降级使用招标文件进行分析，长度: %d 字符', len(full_text))
        tender_context = await extract_tender_context_async(full_text)
        analysis_text = tender_context
    provider, used_fb = await get_working_provider(get_ai_config())
    if used_fb:
        logger.info('AI工作界面分析已降级到兜底提供商: %s', used_fb)
    try:
        result = await wbq_analyze_work_interface(provider, analysis_text)
    except Exception as e:
        # BUG-FIX 2026-09-18（中）：同 generate_summary —— 放宽异常范围。
        logger.exception('AI分析工作界面失败: %s', e)
        raise HTTPException(status_code=502, detail=f'AI服务调用失败: {str(e)[:200]} | 请检查AI配置或稍后重试') from e
    result_json = json.dumps(result, ensure_ascii=False, indent=2)
    await asyncio.to_thread(_sync_insert_or_update, project_id, result_json, 'work_interface_json')
    return {'work_interface_json': result_json}

@router.post('/construction-method')
async def analyze_construction_method(project_id: int, current_user: Annotated[object, Depends(get_current_user)]=None):
    """分析工程作法（优先使用工程作法文件，未找到时降级使用招标文件）"""
    await enforce_project_access(project_id, current_user)
    cached = await asyncio.to_thread(_sync_read_construction_method, project_id)
    if cached and cached != '[]' and (len(cached) > 10):
        return {'construction_method_json': cached}
    cm_text = await asyncio.to_thread(get_construction_method_file_text, project_id)
    if cm_text and cm_text.strip():
        logger.info('[AI作法] 使用工程作法文件进行分析，长度: %d 字符', len(cm_text))
        analysis_text = cm_text
    else:
        full_text = await latest_extracted_text(project_id)
        if not full_text:
            raise HTTPException(status_code=400, detail='请先上传文件')
        logger.info('[AI作法] 未找到工程作法文件，降级使用招标文件进行分析，长度: %d 字符', len(full_text))
        tender_context = await extract_tender_context_async(full_text)
        analysis_text = tender_context
    provider, used_fb = await get_working_provider(get_ai_config())
    if used_fb:
        logger.info('AI工程作法分析已降级到兜底提供商: %s', used_fb)
    try:
        result = await wbq_analyze_construction_method(provider, analysis_text)
    except Exception as e:
        # BUG-FIX 2026-09-18（中）：同 generate_summary —— 放宽异常范围。
        logger.exception('AI分析工程作法失败: %s', e)
        raise HTTPException(status_code=502, detail=f'AI服务调用失败: {str(e)[:200]} | 请检查AI配置或稍后重试') from e
    result_json = json.dumps(result, ensure_ascii=False, indent=2)
    await asyncio.to_thread(_sync_insert_or_update, project_id, result_json, 'construction_method_json')
    return {'construction_method_json': result_json}

@router.post('/boq/confirm')
async def confirm_boq(project_id: int, current_user: Annotated[object, Depends(get_current_user)]=None):
    """确认清单工程量分析"""
    await enforce_project_access(project_id, current_user)
    await asyncio.to_thread(_sync_confirm_field, project_id, 'confirmed_boq')
    return {'status': 'ok'}

@router.post('/work-interface/confirm')
async def confirm_work_interface(project_id: int, current_user: Annotated[object, Depends(get_current_user)]=None):
    """确认工作界面分析"""
    await enforce_project_access(project_id, current_user)
    await asyncio.to_thread(_sync_confirm_field, project_id, 'confirmed_work_interface')
    return {'status': 'ok'}

@router.post('/construction-method/confirm')
async def confirm_construction_method(project_id: int, current_user: Annotated[object, Depends(get_current_user)]=None):
    """确认工程作法分析"""
    await enforce_project_access(project_id, current_user)
    await asyncio.to_thread(_sync_confirm_field, project_id, 'confirmed_construction_method')
    return {'status': 'ok'}

@router.delete('/analysis')
async def clear_analysis(project_id: int, current_user: Annotated[object, Depends(get_current_user)]=None):
    """清除已生成的摘要和评分点信息"""
    await enforce_project_access(project_id, current_user)
    await asyncio.to_thread(_sync_clear_analysis, project_id)
    return {'status': 'ok'}
