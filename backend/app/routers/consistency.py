"""全文一致性审计路由

提供一致性检查、修复建议、自动修复等功能。
"""
import asyncio
import json
import logging
import threading
import time

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

#: ENHANCE 2026-10-17（第八轮·§7.2-1 通用减载）：`consistency_audit_log` 整行随
#: `/summary` 下发时，下列列**绝不**进入对外响应。当前仅 `conflicts_json`
#: （大项目可达数十 KB，前端 `AuditSummaryCards` 从不消费）。集中在此声明，使「摘要
#: 不返回大字段」成为显式契约，后续若再有类似重列，直接加进这个元组即可，避免回到
#: 此前 `SELECT *` 默认全量下发的隐患。
SUMMARY_EXCLUDED_COLUMNS: tuple[str, ...] = ("conflicts_json",)


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

    BUG-FIX 2026-10-15（P2·有界性宣称不成立）：若表里**所有**锁都处于持锁态
    （极端场景：512 个项目同时在跑一致性写操作），本函数一轮跑完一条都淘汰不掉，
    调用方 `_consistency_op_lock` 仍会照常插入新锁 —— 表就会长期超出上限。
    这不是竞态（新锁与既有锁语义互斥，不会产生两把同项目的锁），但「超过上限后
    按 LRU 淘汰最旧的」这句注释在极端情况下并不成立。现把这个退化情形显式打
    debug 日志，使「锁表实际大小」在线上可观测，而不是靠注释宣称。
    """
    for pid in list(_CONSISTENCY_OP_LOCKS_ORDER):
        if len(_CONSISTENCY_OP_LOCKS) < _CONSISTENCY_OP_LOCKS_MAX:
            return
        lock = _CONSISTENCY_OP_LOCKS.get(pid)
        if lock is not None and lock.locked():
            continue
        _CONSISTENCY_OP_LOCKS.pop(pid, None)
        _CONSISTENCY_OP_LOCKS_ORDER.remove(pid)
    if len(_CONSISTENCY_OP_LOCKS) >= _CONSISTENCY_OP_LOCKS_MAX:
        logger.debug(
            '一致性操作锁表已达上限 %d 且全部处于持锁态，本轮未淘汰任何锁',
            _CONSISTENCY_OP_LOCKS_MAX,
        )


class _LockManager:
    """`async with _LockManager(project_id)` 语法糖（避免每处手写两行）。"""

    def __init__(self, project_id: int):
        self._project_id = project_id
        # BUG-FIX 2026-10-17（P3·锁表淘汰的隐性竞态窗口）：锁对象的获取从
        # `__init__` 移到 `__aenter__`。原因是 LRU 淘汰只跳过 `locked()` 的锁，
        # 而「`_consistency_op_lock` 返回锁 → `__aenter__` 真正 acquire」之间
        # 存在一个窗口：这期间该锁既未持有、也无等待者，却已有一个请求拿着它
        # 准备进临界区。当前单事件循环模型下 `async with _LockManager(pid)` 的
        # 构造与 acquire 之间没有让出点、竞态不可达；但「淘汰安全性」此前只靠
        # 这个**隐式调度保证**成立，`_evict_idle_locks_locked` 注释里「正在等待
        # 或持有的锁不参与淘汰」对该窗口并不覆盖 —— 一旦将来有人在构造与进入
        # 之间插入 await（如加等待日志/超时抢锁），同一项目的两个并发操作就会
        # 各持一把不同的锁，临界区互斥被击穿。改为进入时**重新取字典里的规范
        # 锁**：未发生淘汰时拿到的是同一对象（dict 命中，零行为变化）；发生过
        # 淘汰时拿到的是新规范锁，互斥语义反而被保住。成本为一次 dict 查找。
        self._lock: asyncio.Lock | None = None
        # BUG-FIX 2026-10-15（P2·可能 release 未持有的锁）：`asyncio.Lock.release()`
        # 在未持有时抛 `RuntimeError: not locked`。虽然 `acquire()` 被取消时不会
        # 留下持锁状态、`__aexit__` 也不会被调用，但 `_LockManager` 是**每个写
        # 入口**都会实例化的横切设施，一旦未来有人在这里加了「等待失败即抛出」的
        # 逻辑（例如超时抢锁），`__aexit__` 就会去释放一把从未拿到的锁、把整条
        # 写路径炸成 500。用一个标记位把「是否真拿到了」钉死，成本近乎为零。
        self._acquired = False

    async def __aenter__(self):
        # 进入时取规范锁（见 __init__ 的 BUG-FIX 2026-10-17 注释）。
        # 两条路径都安全：无争用时 acquire 不让出、无淘汰窗口；有争用时该锁
        # 已 `locked()`、不参与淘汰。
        self._lock = _consistency_op_lock(self._project_id)
        await self._lock.acquire()
        self._acquired = True
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if self._acquired and self._lock is not None:
            self._lock.release()
            self._acquired = False
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
# 脚本与前端 api/index.ts），仅在 OpenAPI 中标注 deprecated/legacy。
# 前端已切到 POST。
#
# BUG-FIX 2026-10-13（第六轮·P2·OpenAPI 重复 operation ID）：此前用单个
# `api_route(methods=['GET','POST'])` 注册，FastAPI 会为同一路由生成**两个**
# operation 却共用同一个 `operation_id`（`generate_unique_id` 只取
# `route.name + path + methods[0]`）——实测 `app.openapi()` 报
#   Duplicate Operation ID run_audit_api_v1_consistency_audit__project_id__get
# 且 schema 里 GET/POST 两条目的 operationId 相同：Swagger UI 行为异常、
# 按 OpenAPI 生成客户端的代码会互相覆盖（POST 方法被 GET 顶掉）。
# 现拆成两个装饰器分别注册（`@router.post` + `@router.get`），FastAPI 为
# 每条路由取 `methods[0]` 生成唯一 ID（`..._post` / `..._get`），语义不变、
# 响应体不变，GET 显式标 deprecated 提示 legacy。
@router.post(
    '/audit/{project_id}',
    summary='运行全文一致性审计（规范入口）',
)
@router.get(
    '/audit/{project_id}',
    summary='运行全文一致性审计（legacy GET；规范入口为 POST，此入口将下线）',
    deprecated=True,
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
    _audit_start = time.monotonic()
    try:
        # BUG-FIX 2026-09-18（P2）：run_full_audit 是同步重操作（全量读正文 +
        # 事实抽取），直接在事件循环里跑会阻塞整个服务（SSE/其他请求卡死）。
        # ENHANCE 2026-10-09：与 autofix / AI 修复串行（见模块头并发说明）。
        async with _LockManager(project_id):
            result = await asyncio.to_thread(consistency_auditor.run_full_audit, project_id)
        # T5 指标埋点（2026-10-16）：审计成功/失败计数。
        from ..services.consistency_metrics import consistency_metrics, record_audit
        consistency_metrics.inc(
            'audit_runs' if rules.is_audit_success(result.status) else 'audit_failed'
        )
        # ENHANCE 2026-10-17（第八轮·T7-3 监控埋点）：记录审计耗时与检出冲突数，
        # 仅成功审计计入冲突数（失败审计无有效检出，conflicts_found=0 已兜底）。
        record_audit(
            int((time.monotonic() - _audit_start) * 1000),
            result.conflicts_found if rules.is_audit_success(result.status) else 0,
        )
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
        # BUG-FIX 2026-10-17（P3·指标完整性）：异常路径此前不计 `audit_failed`
        # —— 只有「内部降级为 failed 结果」的那条路径计数（见上方埋点）。于是
        # 路由层直接抛异常（DB 连接失败 / to_thread 崩溃等）的 500 在
        # `GET /consistency/metrics` 里完全观测不到，审计失败率被系统性低估。
        # 两条失败路径现在都计数。
        from ..services.consistency_metrics import consistency_metrics

        consistency_metrics.inc('audit_failed')
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
                # BUG-FIX 2026-10-15（P1·两条审计链路混排不可区分）：
                # `consistency_conflicts` 同时承载确定性规则冲突与 AI 语义审计发现
                # （后者 `conflict_id` 以 `AI-` 开头、`values_json` 恒为 `'[]'`），
                # 但此前对外接口不暴露这个区分。前端因此：
                #   ① 把 AI 发现的 `fact_name`（最长 100 字的自由文本 issue）当
                #      「事实项」加粗渲染，用户以为是事实名；
                #   ② 「自动修复」按钮对 AI 发现仍可点，而 `apply_autofix` 因
                #      `values=[]` 必然回 `no_change`，用户反复点、反复看到失败。
                # 现随每条冲突下发 `source_kind`（`rule` / `ai`），纯新增字段，
                # 既有字段名与取值语义一律不动。
                conflict['source_kind'] = rules.conflict_source_kind(conflict.get('conflict_id'))
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
    from ..services.consistency_metrics import record_autofix
    # BUG-FIX 2026-10-17（P3·前后端口径不一致 / 防御深度）：AI 语义发现
    # （`conflict_id` 以 `AI-` 开头，见 `consistency_rules` 冲突来源命名空间）
    # 的 `values_json` 恒为 `'[]'` —— 没有任何可替换的旧值。前端已禁用其
    # 「自动修复」按钮（2026-10-15 第六轮），但 API 层仍可调用并必然返回
    # `no_change`：脚本 / 第三方调用方 / 未来的新 UI 入口拿到的一次注定失败
    # 的写路径尝试。服务端显式拒绝并给出可读原因，与前端禁用提示同口径。
    # 规则冲突（`conflict_` 前缀）不受影响，行为逐字不变。
    if rules.is_ai_conflict_id(conflict_id):
        raise HTTPException(
            status_code=400,
            detail='AI 语义发现没有结构化取值（统一不了具体数值），无法自动修复，请人工核对',
        )
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
            # ENHANCE 2026-10-13（第六轮）：区分「什么都没改」与「改了一部分」。
            # 旧代码只要 success=False 就一律回 `no_change`，但部分章节落文成功、
            # 仅个别章节被安全闸门拦下的场景下，`no_change`（未做任何修改）与事实
            # 相反——用户看到状态是 no_change，message 却说「已修改 3 个章节」，
            # 自相矛盾。现按 changed_sections 区分：有落文 → `partial`（部分完成，
            # 冲突保持未解决待人工处理）；无落文 → `no_change`。前端对非 ok 状态
            # 统一按 warning 展示并常驻结果卡片，两种状态都不再被误读为成功。
            # ENHANCE 2026-10-17（第八轮·T7-3 监控埋点）：无正文改写的修复计入
            # `autofix_no_change_total`，便于区分「真改了」与「没动静」。
            record_autofix(no_change=True)
            if report.get('changed_sections'):
                return {'status': 'partial', **report}
            return {'status': 'no_change', **report}
        # ENHANCE 2026-10-17（第八轮·T7-3 监控埋点）：落文成功计入 `autofix_applied_total`，
        # 并以 `total_replacements` 累积实际改写处数（autofix_edits_total）。
        record_autofix(applied=True, edits=report.get('total_replacements', 0) or 0)
        return {'status': 'ok', **report}
    except Exception as e:
        logger.error('自动修复失败: %s', e, exc_info=True)
        # ENHANCE 2026-10-17（第八轮·T7-3 监控埋点）：autofix 抛异常（HTTP 500）计入
        # `autofix_failed_total`，与成功/无改动分开观测，避免失败率被低估。
        record_autofix(failed=True)
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
        # T5 指标埋点（2026-10-16）：AI 补丁被唯一性校验拒绝的累计条数。
        from ..services.consistency_metrics import consistency_metrics
        consistency_metrics.inc('ai_patch_rejected', len(report.get('rejected') or []))
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
        # T5 指标埋点（2026-10-16）：Agent 修复同样统计补丁拒绝数。
        from ..services.consistency_metrics import consistency_metrics
        consistency_metrics.inc('ai_patch_rejected', len(report.get('rejected') or []))
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

    # BUG-FIX 2026-10-17（P2·错误处理不一致）：本端点此前是模块内最后两个
    # 「无 try/except + 无 logger」的端点之一（另一个是 rejection-coverage）——
    # 2026-10-15 修过 facts_backflow_accept 的同类问题（裸 500），漏了这两个
    # 只读端点。服务层异常会穿透到 FastAPI 默认 500：响应体是一句框架级别的
    # "Internal Server Error"，日志里没有项目 ID / 参数 / 堆栈上下文，线上
    # 用户报「扫描失败」时无从倒查。与模块内其余端点统一口径。
    try:
        result = await asyncio.to_thread(propose_facts, project_id, min_occurrences=min_occurrences)
    except Exception as e:
        logger.error(
            '事实回流候选扫描失败: project=%s min_occurrences=%s: %s',
            project_id,
            min_occurrences,
            e,
            exc_info=True,
        )
        raise HTTPException(status_code=500, detail=f'候选事实扫描失败: {e!s}') from e
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
    # BUG-FIX 2026-10-15（P1·跨模块数据完整性 / 事实基准在生成中被改）：
    # 本端点通过 `accept_fact_proposals` 直接写 `analysis.global_facts_json`，
    # 与 `routers/facts.py` 的 POST /facts（手工保存）是同一条写口径 —— 但
    # 本端点此前**完全没有任何生成中防护**，而 facts.py 早就为此加过
    # `_ensure_no_ai_task_running`（见其 docstring 的 BUG-FIX 2026-09-29）：
    #   「正文生成进行中时保存事实，生成循环已把旧事实读进内存并逐章注入
    #    prompt，新事实只在后半部分章节生效 → 同一篇标书前半用旧工期、后半
    #    用新工期，一致性审计反而报矛盾」。
    # 注意这里必须用 content-generation 锁，而不是本文件其它写端点用的
    # `task_guard.reject_if_generation_running` —— 后者只查 `outline-generation`
    # （见 task_guard.live_group_lock），对正文生成完全不设防，用它等于没加。
    # 直接复用 facts 路由的守卫函数，保持两条写口径的判定完全同源。
    from .facts import _ensure_no_ai_task_running
    from ..services.consistency_metrics import consistency_metrics
    try:
        await _ensure_no_ai_task_running(project_id)
    except HTTPException:
        # T5 指标埋点（2026-10-16）：生成中守卫拦截（409）计数。
        consistency_metrics.inc('guard_interceptions')
        raise
    from ..services.consistency_backflow import accept_fact_proposals

    async with _LockManager(project_id):
        # BUG-FIX 2026-10-15（P2·可观测性不一致）：本文件其余端点都有
        # `try/except + logger.error(..., exc_info=True) + 本地化 detail`，
        # 唯独本端点把异常抛成裸 500。服务层 `accept_fact_proposals` 已
        # logger.error 后重新抛出（语义正确：不能吞掉合并失败、不产生「显示
        # 成功但没合并」的假象），路由层再补一层以保留 HTTP 侧上下文并统一 detail。
        try:
            result = await asyncio.to_thread(accept_fact_proposals, project_id, proposals)
        except HTTPException:
            # 属权/守卫类 4xx 原样上抛，不裹一层 500
            raise
        except Exception as e:
            logger.error('事实回流合并失败: %s', e, exc_info=True)
            raise HTTPException(
                status_code=500,
                detail=f'事实回流合并失败: {str(e)[:120]}',
            ) from e
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

    # BUG-FIX 2026-10-17（P2·错误处理不一致）：与 facts_backflow_proposals 同源
    # —— 本端点此前无 try/except + 无 logger，服务层异常穿透成框架级裸 500，
    # 无项目上下文可倒查。统一补齐。
    try:
        report = await asyncio.to_thread(rejection_answer_coverage, project_id)
    except Exception as e:
        logger.error(
            '废标条款应答覆盖率统计失败: project=%s: %s', project_id, e, exc_info=True
        )
        raise HTTPException(status_code=500, detail=f'覆盖率统计失败: {e!s}') from e
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
            # BUG-FIX 2026-10-09：sqlite3.Row 是惰性解引用，跨语句持有会串列，
            # 故此处所有 Row 都在语句后立即物化为普通值。
            latest = conn.execute('SELECT * FROM consistency_audit_log \
              WHERE project_id = ?\
              ORDER BY audit_time DESC LIMIT 1', (project_id,)).fetchone()
            latest = dict(latest) if latest is not None else None
            # ENHANCE 2026-10-17（第八轮·T7-1 技术债闭环 + §7.2-1 通用减载）：
            # `consistency_audit_log` 的 `conflicts_json` 列在大项目下可达数十 KB，
            # 而摘要卡片前端只消费 `audit_time` 与「是否确有审计记录」（见
            # `AuditSummaryCards.tsx`：仅读 `summary.latest_audit?.audit_time` 与存在性）。
            # 此前 `SELECT *` 把整列随响应下发，既浪费带宽也不必要。物化后立即按
            # `SUMMARY_EXCLUDED_COLUMNS` 剔除——纯删字段、不改任何既有字段名/取值语义，
            # 前端零改动、行为不变；后续若再有类似重列，加进该元组即可。
            if latest is not None:
                for _col in SUMMARY_EXCLUDED_COLUMNS:
                    latest.pop(_col, None)

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

            # BUG-FIX 2026-10-15（P1·两条审计链路混统计，卡片数字被虚高）：
            # `consistency_conflicts` 同时承载确定性规则冲突与 AI 语义审计发现。
            # AI 发现**恒为** `SEVERITY_DEFAULT`（medium）—— 见
            # `ai/workflows_expand._persist_ai_audit_findings`，因此「中等冲突」卡片
            # 会被 AI 发现一并顶高，用户据此评估风险等级被误导；而「未解决冲突」
            # 卡片与三张分级卡片之和也会永远对不上（`medium` 里混着两类东西）。
            # 现补充按来源拆分的计数（**纯新增字段**，既有字段口径不动，
            # 老调用方无需改动）：
            #   - `conflicts_by_severity_rule`：只算规则冲突（前端分级卡片优先用它）
            #   - `unresolved_rule_conflicts` / `unresolved_ai_findings`
            #   - `resolved_ai_findings`
            # 恒等式：unresolved_rule_conflicts + unresolved_ai_findings
            #         == unresolved_conflicts。
            ai_prefix = rules.AI_CONFLICT_ID_PREFIX + "%"
            _base_open = (
                'SELECT {sel} FROM consistency_conflicts '
                'WHERE project_id = ? AND COALESCE(resolved, 0) = 0'
            )
            _base_resolved = (
                'SELECT {sel} FROM consistency_conflicts '
                'WHERE project_id = ? AND COALESCE(resolved, 0) = 1'
            )
            unresolved_rule = conn.execute(
                _base_open.format(sel='COUNT(*) as cnt')
                + ' AND conflict_id NOT LIKE ?',
                (project_id, ai_prefix),
            ).fetchone()['cnt']
            unresolved_ai = conn.execute(
                _base_open.format(sel='COUNT(*) as cnt')
                + ' AND conflict_id LIKE ?',
                (project_id, ai_prefix),
            ).fetchone()['cnt']
            resolved_ai = conn.execute(
                _base_resolved.format(sel='COUNT(*) as cnt')
                + ' AND conflict_id LIKE ?',
                (project_id, ai_prefix),
            ).fetchone()['cnt']
            by_severity_rule = conn.execute(
                _base_open.format(
                    sel='severity, COUNT(*) as cnt'
                )
                + ' AND conflict_id NOT LIKE ? GROUP BY severity',
                (project_id, ai_prefix),
            ).fetchall()
            severity_counts_rule = {}
            for row in by_severity_rule:
                severity_counts_rule[rules.normalize_severity(row['severity'])] = row['cnt']

            # T2 根治（2026-10-16）：审计来源直接读 `source_kind` 列（两条写入
            # 路径已写入），剔除此前靠 `conflicts_json` 侧信道（conflict_id
            # 前缀）推断的分支。列恒存在（init_db 迁移补列），latest 为 None
            # （无任何审计记录）时回退默认 'rule'。
            audit_source = (
                (latest.get('source_kind') or rules.CONFLICT_SOURCE_RULE)
                if latest
                else rules.CONFLICT_SOURCE_RULE
            )

        return {
            'latest_audit': latest,
            'audit_source': audit_source,
            'unresolved_conflicts': unresolved,
            'resolved_conflicts': resolved,
            'conflicts_by_severity': severity_counts,
            'conflicts_by_severity_rule': severity_counts_rule,
            'unresolved_rule_conflicts': unresolved_rule,
            'unresolved_ai_findings': unresolved_ai,
            'resolved_ai_findings': resolved_ai,
        }

    summary = await asyncio.to_thread(_read_summary)
    return {'status': 'ok', 'summary': summary}


@router.get('/metrics')
async def get_consistency_metrics(user=Depends(get_current_user)):
    """一致性校核模块运行时指标快照（ENHANCE 2026-10-16 / T5）。

    返回进程内累计计数器：审计次数/失败数、AI 审计/发现/修复数、补丁拒绝数、
    生成守卫拦截数。纯内存、重启归零，用于实时观测，不作为审计结论来源
    （结论一律看 `consistency_audit_log`）。纯读、无副作用、不触碰项目数据，
    故不做 `enforce_project_access`。
    """
    from ..services.consistency_metrics import consistency_metrics
    return {'status': 'ok', 'metrics': consistency_metrics.snapshot_with_defaults()}


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
