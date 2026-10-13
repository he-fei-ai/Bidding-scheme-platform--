"""全文一致性审计路由

提供一致性检查、修复建议、自动修复等功能。
"""
import asyncio
import json
import logging
import threading

from fastapi import APIRouter, Depends, HTTPException, Query

from ..security.auth import get_current_user
from ..services import consistency_rules as rules
from ..services.consistency_auditor import consistency_auditor
from ..services.scoring_compliance import COVERAGE_WEAK
from ..services.task_guard import reject_if_generation_running
from .project_access import enforce_project_access


logger = logging.getLogger(__name__)
router = APIRouter(prefix='/consistency', tags=['consistency_audit'], dependencies=[Depends(get_current_user)])

# ENHANCE 2026-10-09（P1·并发竞态）：同一项目的「审计 / 自动修复 / AI 修复」三条
# **写路径**必须串行。原因（`consistency_auditor._save_conflicts` 的实现决定）：
#   全量审计 = `DELETE 未解决冲突` + `INSERT 本次结果`；自动修复 = `UPDATE 正文` +
#   `UPDATE 冲突 SET resolved=1`。这两组操作若交错，会出现
#     (a) 审计的 DELETE 把刚被 autofix 标记 resolved 之外的在途冲突清掉，
#         用户刚看到的冲突瞬间消失且无任何提示；
#     (b) autofix 依据的冲突行在落文前被并发审计删除 → `resolved` 标记写到 0 行，
#         正文改了、冲突却仍是未解决（或反过来：正文没改、冲突被标已解决）。
# 组锁（task_lock）在导出模块已用于跨进程互斥；此处是**进程内**排队（不返回 409，
# 而是等前者跑完），因为一致性审计是秒级操作、排队代价远低于报错重试。
_CONSISTENCY_OP_LOCKS: dict[int, asyncio.Lock] = {}
_CONSISTENCY_OP_LOCKS_GUARD = threading.Lock()

#: 锁表上限。BUG-FIX 2026-10-13（P2·无界增长）：`_CONSISTENCY_OP_LOCKS` 此前
#: 「按项目 id 建锁、永不回收」，每个访问过的项目留下一把 asyncio.Lock 常驻进程。
#: 单条约 100 字节看似无害，但这是**长驻 worker**（多 worker × 长期运行），
#: 且项目数随使用持续增长、没有自然上界——属于典型的无界单例表。
#: 回收策略：超过上限后按「空闲且未持有」淘汰最旧的。淘汰是安全的：拿不到锁的
#: 项目会即时重建，而**正在等待或持有**的锁不参与淘汰，故绝不会把两个并发
#: 操作拆到两把不同的锁上（那才是真正的竞态）。淘汰阈值取 512，远高于单实例
#: 的合理并发项目数，正常运行期不会触发。
_CONSISTENCY_OP_LOCKS_MAX = 512

#: 锁的插入序（用于 LRU 淘汰）。与 _CONSISTENCY_OP_LOCKS 同步维护。
_CONSISTENCY_OP_LOCKS_ORDER: list[int] = []


def _consistency_op_lock(project_id: int) -> asyncio.Lock:
    """取（必要时创建）该项目的操作锁；字典本身用线程锁保护。

    BUG-FIX 2026-10-13：补上无界增长的回收（见 `_CONSISTENCY_OP_LOCKS_MAX`）。
    """
    with _CONSISTENCY_OP_LOCKS_GUARD:
        lock = _CONSISTENCY_OP_LOCKS.get(project_id)
        if lock is None:
            if len(_CONSISTENCY_OP_LOCKS) >= _CONSISTENCY_OP_LOCKS_MAX:
                _evict_idle_locks_locked()
            lock = asyncio.Lock()
            _CONSISTENCY_OP_LOCKS[project_id] = lock
        # 命中即刷新 LRU 位置
        if project_id in _CONSISTENCY_OP_LOCKS_ORDER:
            _CONSISTENCY_OP_LOCKS_ORDER.remove(project_id)
        _CONSISTENCY_OP_LOCKS_ORDER.append(project_id)
        return lock


def _evict_idle_locks_locked() -> None:
    """淘汰空闲锁直到低于上限（**调用方必须已持有** `_CONSISTENCY_OP_LOCKS_GUARD`）。

    跳过 `locked()` 的锁——那把可能正被持有或有等待者，删掉会让后来的请求
    拿到一把**新**锁，从而与持锁者并发进入临界区，把锁的意义直接抵消掉。
    """
    for pid in list(_CONSISTENCY_OP_LOCKS_ORDER):
        if len(_CONSISTENCY_OP_LOCKS) < _CONSISTENCY_OP_LOCKS_MAX:
            return
        lock = _CONSISTENCY_OP_LOCKS.get(pid)
        if lock is not None and lock.locked():
            continue
        _CONSISTENCY_OP_LOCKS.pop(pid, None)
        _CONSISTENCY_OP_LOCKS_ORDER.remove(pid)


class _LockManager:
    """`async with _LockManager(project_id)` 语法糖（避免每处手写两行）。"""

    def __init__(self, project_id: int):
        self._lock = _consistency_op_lock(project_id)

    async def __aenter__(self):
        await self._lock.acquire()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self._lock.release()
        return False


#: autofix 的 `new_value` 长度上限。超过即拒：正文里一个事实值不可能这么长，
#: 超长输入只可能来自误填（把整段说明文案当值）或恶意请求。
#:
#: ENHANCE 2026-10-13：常量迁到 `consistency_rules`（审核规则命名空间），本处
#: 保留同名再导出，使「既有 import `from .consistency import AUTOFIX_VALUE_MAX_CHARS`
#: 」的调用方与测试不必改动，同时该阈值已随 `GET /consistency/rules` 下发。
AUTOFIX_VALUE_MAX_CHARS = rules.autofix_value_max_chars()


@router.get('/rules')
async def get_consistency_rules(user=Depends(get_current_user)):
    """审核规则命名空间快照（严重度取值/排序/文案配色、事实项、审计与冲突状态枚举）。

    ENHANCE 2026-10-08：此前「严重/中等/轻微」的取值、排序与配色在前端
    `utils.ts` 与后端 `consistency_auditor` 各写一套，没有任何一致性约束
    —— 后端调过阈值/文案时前端不会跟着变。本端点把 `consistency_rules`
    这份单一事实来源原样下发，前端启动时对齐一次（失败则退回静态兜底）。

    纯读、无副作用、不触碰任何项目数据，故不做 `enforce_project_access`。
    """
    return {'status': 'ok', **rules.rules_payload()}


# BUG-FIX 2026-10-13（P1 · 带写副作用的 GET）：本端点名为 `GET /consistency/audit/{id}`，
# 但它**不是读操作**——`run_full_audit` 内部会 `DELETE` 全部未解决冲突、
# 重新 `INSERT` 本次快照、并追加一行 `consistency_audit_log`
# （`consistency_auditor._save_conflicts` / `_save_audit_log`）。
# 副作用 GET 的实际代价（都不是理论问题）：
#   * 浏览器/代理的**链接预取**、页面刷新、监控探针的周期 GET，都会静默触发一次
#     全量正文重扫 + 一次 DELETE/INSERT 事务；
#   * 中间代理或 CDN 对 GET 的自动重试会把「幂等」假设坐实，重扫一次全库；
#   * 响应不可缓存，语义与 HTTP 方法约定相反。
# 现按 REST 语义补上规范动词 `POST`，**GET 保留**（不破坏任何既有调用方、
# 脚本与前端 api/index.ts），仅在文档中标注 legacy。前端已切到 POST。
@router.api_route(
    '/audit/{project_id}',
    methods=['GET', 'POST'],
    summary='运行全文一致性审计（POST 为规范入口；GET 为向后兼容保留）',
)
async def run_audit(project_id: int, user=Depends(get_current_user)):
    """运行完整一致性审计

    ⚠️ 该操作会**改写** `consistency_conflicts` 与 `consistency_audit_log`，
    属写操作。请使用 `POST`；`GET` 仅为向后兼容保留。

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
        # ENHANCE 2026-10-09：与 autofix / AI 修复串行（见模块头并发说明）。
        async with _LockManager(project_id):
            result = await asyncio.to_thread(consistency_auditor.run_full_audit, project_id)
        # BUG-FIX 2026-10-08（P1·谎报成功）：`run_full_audit` 内部把异常吞掉并
        # 降级返回 `status='failed'` + 全 0，而本行此前**无条件**回 `status='ok'`。
        # 前端收到后弹「审计完成，发现 0 处冲突」——实际审计在第一步就崩了，
        # 用户据此以为全文一致。现按 `consistency_rules.AUDIT_SUCCESS_STATUSES`
        # 判定，失败即回 `status='failed'`（HTTP 仍为 200，保持既有响应契约）
        # 并带出真实 audit_status，不再谎报成功。
        if not rules.is_audit_success(result.status):
            logger.error(
                '一致性审计未成功完成: project=%s status=%s',
                project_id,
                result.status,
            )
            return {
                'status': rules.AUDIT_STATUS_FAILED,
                'audit_status': result.status,
                'project_id': project_id,
                'total_sections': result.total_sections,
                'facts_extracted': result.facts_extracted,
                'conflicts_found': result.conflicts_found,
                'conflicts': [],
                'audit_time': result.audit_time,
                'message': '审计过程异常终止，结果不可信，请查看后端日志后重试',
            }
        return {
            'status': 'ok',
            # audit_status 与 HTTP 层 status 分开，避免同名键互相覆盖
            # （`status` 恒表示「请求是否成功」，`audit_status` 才是审计结论）。
            'audit_status': result.status,
            'project_id': project_id,
            'total_sections': result.total_sections,
            'facts_extracted': result.facts_extracted,
            'conflicts_found': result.conflicts_found,
            'conflicts': [
                {
                    'conflict_id': c.conflict_id,
                    'fact_name': c.fact_name,
                    'values': c.values,
                    'sources': c.sources,
                    'severity': c.severity,
                    'description': c.description,
                }
                for c in result.conflicts
            ],
            'audit_time': result.audit_time,
        }
    except Exception as e:
        logger.error('一致性审计失败: %s', e, exc_info=True)
        raise HTTPException(status_code=500, detail=f'审计失败: {e!s}') from e

@router.get('/conflicts/{project_id}')
async def get_conflicts(
    project_id: int,
    resolved: str = Query(
        'open',
        pattern='^(open|resolved|all)$',
        description='按解决状态过滤：open=仅未解决(默认) / resolved=仅已解决 / all=全部',
    ),
    user=Depends(get_current_user),
):
    """获取项目的冲突列表

    Args:
        project_id: 项目ID
        resolved: 状态过滤。**默认只返回未解决冲突** —— 已解决的会作为历史
            保留在库里（`_save_conflicts` 只清理 `resolved=0`），此前本接口
            无条件全量返回，前端表格把「早已修好」的历史冲突和待处理冲突
            混排在一起，用户无法分辨哪些还要动手。

    Returns:
        冲突列表
    """
    await enforce_project_access(project_id, user)
    from ..db import get_db

    # `severity` 归一化 + ORDER BY 补 ELSE：见 consistency_rules.severity_sort_sql
    order_by = rules.severity_sort_sql()
    where = 'WHERE project_id = ?'
    params: list = [project_id]
    if resolved == rules.CONFLICT_STATE_OPEN:
        where += ' AND COALESCE(resolved, 0) = 0'
    elif resolved == rules.CONFLICT_STATE_RESOLVED:
        where += ' AND COALESCE(resolved, 0) = 1'

    def _read_conflicts():
        # PERF（2026-10-05）：纯读查询改为 readonly + to_thread，
        # 不再在事件循环内抢全局写锁（旧实现 get_db() 无 readonly，
        # 生成高峰期锁争用会冻结整个 loop，前端 3s 轮询直接卡死）
        with get_db(readonly=True) as conn:
            rows = conn.execute(
                f"SELECT * FROM consistency_conflicts {where} "
                f"ORDER BY {order_by}, created_at DESC",
                tuple(params),
            ).fetchall()
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
                if not isinstance(conflict['values'], list):
                    conflict['values'] = []
                if not isinstance(conflict['sources'], list):
                    conflict['sources'] = []
                # BUG-FIX 2026-10-08：把库里的 0/1 翻译成前端可读状态名，
                # 并回传归一化后的 severity（脏值 `''` 在响应层就变回 `medium`，
                # 不再把排序错乱的成因外泄给调用方）。
                conflict['state'] = rules.conflict_state(conflict.get('resolved'))
                conflict['resolved'] = int(conflict.pop('resolved') or 0)
                conflict['severity'] = rules.normalize_severity(conflict.get('severity'))
                conflicts.append(conflict)
        return conflicts

    conflicts = await asyncio.to_thread(_read_conflicts)
    return {
        'status': 'ok',
        'count': len(conflicts),
        'filter': resolved,
        'conflicts': conflicts,
    }

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
    # BUG-FIX 2026-10-13（L2 · 旁路写正文无守卫）：autofix 是裸 SQL 改写
    # sections.content 的旁路链路。目录生成进行中落文会被收尾的
    # DELETE+INSERT 静默覆盖（冲突被标已解决但正文实际被还原）。
    # 按内容类写入口径防护：目录生成中 409，正文生成中放行（与手写编辑同性质）。
    # 放在项目操作锁**之前**：被拒请求不应占锁排队。
    reject_if_generation_running(project_id, allow_content_edit=True)
    # BUG-FIX 2026-10-09（P1·空值/超长值把正文改坏）：`new_value` 是**替换目标值**，
    # 后端此前完全不校验。前端虽然做了 `newValue.trim()` 的提示，但开放 API / 脚本
    # 调用不受此约束：
    #   - `new_value=""` → `apply_autofix` 会把所有旧值判为「待替换」并替换成空串，
    #     正文里的事实值被**删掉**（「工期 30 天」→「工期 天」），且冲突被标已解决；
    #   - 超长值（误把整段建议文案当值，历史上前端确实这么做过）会把说明文字写进正文。
    # 现显式拦截，返回 400 而不是静默落文。
    if not new_value or not new_value.strip():
        raise HTTPException(status_code=400, detail='new_value 不能为空')
    if len(new_value) > AUTOFIX_VALUE_MAX_CHARS:
        raise HTTPException(
            status_code=400,
            detail=f'new_value 过长（>{AUTOFIX_VALUE_MAX_CHARS} 字符），请只填统一后的取值',
        )
    try:
        # BUG-FIX 2026-09-18（P2）：apply_autofix 逐章节正则替换+写库+失效缓存，
        # 同步直调会阻塞事件循环（大项目正文可达数十 MB）。
        # BUG-FIX 2026-10-08（**P0·接口 100% 失败**）：`ConsistencyAuditor.apply_autofix`
        # 的第 4 参 `dry_run` 是**keyword-only**（`*, dry_run=False`），而这里把
        # `dry_run` 当第 4 个**位置参数**传了进去 —— 于是每一次调用都抛
        #     TypeError: apply_autofix() takes 4 positional arguments but 5 were given
        # 被下面的 except 吞成 HTTP 500。即 **POST /consistency/conflicts/{id}/autofix
        # 这个「把冲突真正改写进正文」的主修复接口从来就没成功执行过一次**。
        # 该缺陷此前无任何测试覆盖：既有 autofix 用例（test_enhancement_2026_09_14）
        # 都是**直接调服务方法**，绕过了路由层，故一直没暴露。
        # 修复：按签名以关键字传入。
        async with _LockManager(project_id):
            report = await asyncio.to_thread(
                consistency_auditor.apply_autofix,
                conflict_id,
                project_id,
                new_value.strip(),
                dry_run=dry_run,
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
    # BUG-FIX 2026-10-13（L2 · 旁路写正文无守卫）：AI 修复同样改写正文，
    # 与 autofix 同一口径防护（目录生成中 409，正文生成中放行）。
    reject_if_generation_running(project_id, allow_content_edit=True)
    from ..services.consistency_repair import repair_with_ai as _repair

    parsed_ids: list[int] | None = None
    if section_ids:
        try:
            parsed_ids = [int(part) for part in section_ids.split(',') if part.strip()]
        except ValueError as e:
            raise HTTPException(status_code=400, detail='section_ids 必须是逗号分隔的整数') from e

    try:
        # ENHANCE 2026-10-09：AI 修复同样会改写正文并收敛冲突状态，与审计/自动修复
        # 串行，避免与并发审计的 DELETE 交错导致冲突状态被误判。
        async with _LockManager(project_id):
            report = await _repair(project_id, dry_run=dry_run, section_ids=parsed_ids)
    except Exception as e:
        logger.error('AI 一致性修复失败: %s', e, exc_info=True)
        raise HTTPException(status_code=500, detail=f'修复失败: {e!s}') from e

    return {'status': 'ok' if report.get('success') else 'no_change', **report}


@router.post('/agent/{project_id}')
async def agent_mode_repair(
    project_id: int,
    dry_run: bool = Query(False, description='只跑 Agent 会话与预览，不写库'),
    max_turns: int = Query(8, ge=1, le=20, description='Agent 最大往返轮数（防失控烧 token）'),
    user=Depends(get_current_user),
):
    """Agent 模式全文一致性修复（E-1，对标易标 D1「Agent修复」）。

    差异点 vs 既有 `POST /consistency/repair/{project_id}`（AI 补丁路径）：

    - 本模式让 AI 以「多轮往返」方式工作（search / read / replace），长文
      不会一次塞进单请求 —— 几十万字标书不再受 batch/token 限制；
    - 安全模型与 AI 补丁路径一致：AI 只提修改意图，宿主唯一命中校验后执行，
      Agent 编造的 old_text / 越界 section_id 一律拒绝，幻觉 0 落库。

    Args:
        project_id: 项目ID
        dry_run: 预览模式
        max_turns: Agent 最大往返轮数
    """
    await enforce_project_access(project_id, user)
    # BUG-FIX 2026-10-13（L2 · 旁路写正文无守卫）：Agent 修复同样改写正文
    # （非 dry_run），与 autofix / AI 修复同一口径防护。
    # dry_run 预览不写库，无需守卫（避免预览也被 409 挡掉）。
    if not dry_run:
        reject_if_generation_running(project_id, allow_content_edit=True)
    from ..services.agent_repair import run_agent_repair

    # ENHANCE 2026-10-09：与 audit/autofix/repair 共享同一项目级写锁，避免
    # 并发写正文/冲突状态互相踩踏（见模块头 _CONSISTENCY_OP_LOCKS 说明）。
    try:
        async with _LockManager(project_id):
            report = await run_agent_repair(
                project_id, dry_run=dry_run, max_turns=max_turns
            )
    except Exception as e:
        logger.error('Agent 一致性修复失败 project=%s: %s', project_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail=f'Agent 修复失败: {e!s}') from e

    return {'status': 'ok' if report.get('success') else 'partial', **report}


@router.get('/facts/proposals/{project_id}')
async def facts_backflow_proposals(
    project_id: int,
    min_occurrences: int = Query(3, ge=1, le=50, description='候选值在正文至少出现次数'),
    user=Depends(get_current_user),
):
    """审计结果 → 全局事实 反向回流候选（E-5）。

    扫描未解决冲突，把「正文里占多数的取值」沉淀成候选事实（占位值剔除、
    与既有事实去重）。用户确认后走 `POST /facts/proposals/{id}/accept`。
    """
    await enforce_project_access(project_id, user)
    from ..services.consistency_backflow import propose_facts

    result = await asyncio.to_thread(propose_facts, project_id, min_occurrences=min_occurrences)
    return {'status': 'ok', **result}


@router.post('/facts/proposals/{project_id}/accept')
async def facts_backflow_accept(
    project_id: int,
    body: dict,
    user=Depends(get_current_user),
):
    """采纳事实回流候选，合并进全局事实清单（E-5）。

    走与手工保存一致的 upsert + 三层缓存失效；采纳后同一类冲突在下次
    审计时即为「有基准」，不再因缺失口径而复现。
    """
    await enforce_project_access(project_id, user)
    proposals = body.get('proposals') if isinstance(body, dict) else None
    if not isinstance(proposals, list):
        raise HTTPException(status_code=400, detail='body.proposals 必须是列表')
    from ..services.consistency_backflow import accept_fact_proposals

    async with _LockManager(project_id):
        result = await asyncio.to_thread(accept_fact_proposals, project_id, proposals)
    return {'status': 'ok', **result}


@router.get('/rejection-coverage/{project_id}')
async def rejection_answer_coverage_endpoint(
    project_id: int,
    user=Depends(get_current_user),
):
    """废标条款应答覆盖率（E-6）。

    基于最新一次废标检查任务，对每个检查条款与全项目正文做字符 bigram
    重合度，量化「条款要求是否被投标正文正面回应」。确定性实现、无 AI、
    结果可复现；配合废标项检查的问题点使用，避免「查出了风险却没在方案里
    补答」的断层。
    """
    await enforce_project_access(project_id, user)
    from ..services.rejection_coverage import rejection_answer_coverage

    report = await asyncio.to_thread(rejection_answer_coverage, project_id)
    return {'status': 'ok', **report}


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
            latest = conn.execute('SELECT * FROM consistency_audit_log \
              WHERE project_id = ?\
              ORDER BY audit_time DESC LIMIT 1', (project_id,)).fetchone()
            unresolved = conn.execute('SELECT COUNT(*) as cnt FROM consistency_conflicts \
               WHERE project_id = ? AND COALESCE(resolved, 0) = 0', (project_id,)).fetchone()['cnt']
            resolved = conn.execute('SELECT COUNT(*) as cnt FROM consistency_conflicts \
               WHERE project_id = ? AND COALESCE(resolved, 0) = 1', (project_id,)).fetchone()['cnt']
            # BUG-FIX 2026-10-08（统计口径不一致）：原 by_severity **未过滤
            # resolved**，把已解决的冲突也计入「严重冲突/中等冲突」卡片，而同一屏的
            # 「未解决冲突」卡片却只算未解决的 —— 两者口径不同，用户看到
            # 「未解决 3 / 严重 1 / 中等 1」却发现表格里那条中等早已修好。
            # 现按未解决口径统计，与 conflicts 表默认过滤（open）保持一致。
            by_severity = conn.execute('SELECT severity, COUNT(*) as cnt FROM consistency_conflicts \
               WHERE project_id = ? AND COALESCE(resolved, 0) = 0\
               GROUP BY severity', (project_id,)).fetchall()
            severity_counts = {}
            for row in by_severity:
                severity_counts[rules.normalize_severity(row['severity'])] = row['cnt']
        return {
            'latest_audit': dict(latest) if latest else None,
            'unresolved_conflicts': unresolved,
            'resolved_conflicts': resolved,
            'conflicts_by_severity': severity_counts,
        }

    summary = await asyncio.to_thread(_read_summary)
    return {'status': 'ok', 'summary': summary}


@router.get('/scoring-compliance/{project_id}')
async def scoring_compliance(
    project_id: int,
    # BUG-FIX 2026-10-13（P1·带写副作用的 GET 默认落库）：`persist` 此前默认为
    # True，于是「只是想看一眼」的普通 GET 每次都往 `scoring_compliance_audit`
    # 插一行历史。前端 `ScoringComplianceTab` 恰恰显式传了 `persist:false`
    # 来规避（说明这个默认值已经 inconvenient 到要专门绕开），而任何脚本 /
    # curl / 第三方调用方会无界地把该表灌大。改为默认 False：**要看结果就别写
    # 历史，要留档显式传 persist=true**。向前兼容——响应体完全不变，只有副作用
    # 收敛；依赖隐式落档的调用方需补一个查询参数。
    persist: bool = Query(False, description='是否把本次结果写入审计历史（默认不写）'),
    # BUG-FIX 2026-10-08（P1·阈值前后端/配置不一致）：此处此前硬编码
    # `Query(0.10, ...)`，而服务层 `scoring_compliance.COVERAGE_WEAK` 早已按实测
    # 标定上调到 0.25 —— 且该常量的注释里明确写着「初版取 0.10 过于宽松，实测被
    # 0.21 的反例直接逃过告警，故上调」。也就是说：生产上唯一被触达的路径用的
    # 恰恰是作者自己判定为**过松、有已知漏报**的那个值，而服务层常量因被默认值
    # 遮蔽从未生效。现改为引用常量，保持单一事实来源。
    coverage_weak: float = Query(
        COVERAGE_WEAK,
        ge=0.0,
        le=1.0,
        description='低于该覆盖率判 weak（默认取服务层标定值 COVERAGE_WEAK）',
    ),
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
        persist: 是否落库到 scoring_compliance_audit 审计历史（默认 False）
        coverage_weak: 低于该覆盖率判 weak（默认取服务层标定值 `COVERAGE_WEAK`；
            注意旧文档曾写「默认 0.10」，该硬编码已 BUG-FIX 2026-10-08 移除）

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
