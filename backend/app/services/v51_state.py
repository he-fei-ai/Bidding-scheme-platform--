"""V5.1 目录生成任务状态管理

管理 V5.1 多阶段目录生成的状态，支持：
- 分阶段暂停/恢复（Level 1 → 审核 → Level 2 → 审核 → Level 3 → 自检）
- 审核结果保存（编辑后的目录树）
- 取消生成
- 断点续生（状态持久化到数据库）
"""

import asyncio
from datetime import datetime, timedelta
import json
import logging
import threading
from typing import Any


logger = logging.getLogger(__name__)


# BUG-FIX 2026-10-11（O-7）：V5.1 分阶段审核窗口的 max_depth 白名单。
# `resume_v51_task` 此前无条件接受 `edited_outline`，一旦用户在 L1 审核窗口
# 提交了 L3 完整树（例如从浏览器工具改 payload，或前端刷新后缓存了旧 outline），
# 工作流后续 `_generate_level2_v51` → `_build_level2_skeleton`
# （`workflows_outline_v51.py:2107`）会**按 L1 标题完全重建 L2+L3 骨架**，
# 用户精心编辑的 children 被静默丢弃，且没有任何错误提示。
#
# 该表定义每个 phase 下允许的 outline 最大深度区间 `[min, max]`：
#   - level1：等用户在"章"级别审核，允许空 tree（仅返回 outline 原样）或
#     depth<=1 的一级目录。depth>=2 视为「跳审核」——拒绝，用户如需跳过
#     应显式调 auto_confirm 或直接提交 L3 完整树并切到对应 phase。
#   - level2：等用户在"章+节"级别审核，允许 depth<=2。
#   - level3：等用户在"章+节+条"级别审核，允许 depth<=3。
#   - final：最终审核（L3 已生成完毕），允许完整树（无上限，走 >= max 兜底）。
# 未知 phase 走宽松兜底（>= 任意深度都放行），避免误伤历史持久化状态。
_V51_PHASE_ALLOWED_DEPTH: dict[str, tuple[int, int]] = {
    "level1": (1, 1),
    "level2": (1, 2),
    "level3": (1, 3),
    "final": (0, 10**9),  # 无上限（含空 tree）
}
_V51_DEPTH_UNLIMITED = 10**9


def _outline_max_depth(outline: Any) -> int:
    """计算 outline 树的最大深度（空/非 list → 0）。

    - `[{"title": "x"}]` → 1
    - `[{"title": "x", "children": [{"title": "y"}]}]` → 2
    - `[{"title": "x", "children": [{"title": "y", "children": []}]}]` → 2
      （空 children 不计入深度）
    """
    if not isinstance(outline, list):
        return 0
    max_d = 0
    for node in outline:
        if not isinstance(node, dict):
            continue
        sub = node.get("children")
        # 空 children / None 视为叶子；只有子节点列表非空时才递归
        if isinstance(sub, list) and sub:
            child_depth = _outline_max_depth(sub)
        else:
            child_depth = 0
        max_d = max(max_d, 1 + child_depth)
    return max_d


def _phase_allows_depth(phase: str, depth: int) -> bool:
    """判断 phase 下 depth 是否被允许。未知 phase 兜底放行。"""
    rng = _V51_PHASE_ALLOWED_DEPTH.get(phase)
    if rng is None:
        return True
    lo, hi = rng
    return lo <= depth <= hi


class V51TaskState:
    """单个项目的 V5.1 生成任务状态

    V5.1 乐观锁（BUG-O-02 修复）：
    - version 字段在每次 resume_v51_task 时递增
    - v51_confirm_route 在调用前记录 version，调用后检查 version 是否变化
    - 若 version 变化说明有其他并发的确认操作覆盖了当前编辑，需拒绝写入
    - 多标签页并发操作同一项目的 V5.1 任务时，后提交的会收到冲突错误
    """

    def __init__(self, project_id: int):
        self.project_id = project_id
        self.resume_event = asyncio.Event()
        self.current_phase: str = "level1"  # level1, level2, level3, final
        self.cancelled: bool = False
        self.edited_outline: list[dict] | None = None  # 用户审核编辑后的目录
        self.level1_outline: list[dict] | None = None  # 原始一级目录
        self.full_outline: list[dict] | None = None  # 完整目录树
        self.provider: Any = None
        self.summary: str = ""
        self.scoring_points: str = ""
        self.industry_profile: str = ""
        self.tender_context: str = ""
        self.project_scope: str = ""
        self.project_info: dict | None = None
        self.progress_callback: Any = None
        self.phase_callback: Any = None
        # ENHANCE 2026-09-14：目录门禁裁决（`outline_gate.build_verdict` 的产物）。
        # 只存内存不落库：门禁结论用于本次生成期间向用户提示与前端展示，
        # 落库需要加列 + 迁移，收益不足。
        self.gate_report: dict | None = None
        # BUG-FIX 2026-09-15（高）：标记该项目是否有**正在运行**的 V5.1 生成任务。
        # 供 load_v51_state_from_db 判断「能否用 DB 行重建覆盖内存状态」——
        # 运行中的工作流持有的是旧对象，覆盖会让 cancel/resume 打到空处。
        self.active_run: bool = False
        # BUG-O-02 修复：乐观锁版本号，每次 resume_v51_task 时递增
        self._confirm_version: int = 0
        # BUG-FIX 2026-10-11（O-7）：最近一次 resume_v51_task 被拒的原因，
        # 供路由层区分「乐观锁冲突（409）」和「层级不匹配（400）」。
        # 值域：None（未拒或最新一次成功）| "version_conflict" | "depth_mismatch"
        #      | "invalid_outline" | "no_state"
        self._last_reject_reason: str | None = None


# 全局 V5.1 任务状态字典（project_id -> V51TaskState）
# SSE-8/BUG-O-02 修复：添加 threading.Lock 保护，防止跨线程并发访问导致竞态条件
_v51_states: dict[int, V51TaskState] = {}
_v51_states_lock = threading.Lock()

# 进程内一次性标记：避免每次 load_v51_state_from_db 都扫 v51_tasks 全表做过期清理
_v51_expired_cleanup_done = False
_v51_expired_cleanup_lock = threading.Lock()


def get_v51_state(project_id: int) -> V51TaskState | None:
    """获取项目 V5.1 状态"""
    with _v51_states_lock:
        return _v51_states.get(project_id)


def get_v51_confirm_version(project_id: int) -> int | None:
    """获取项目 V5.1 确认版本号（乐观锁用）

    BUG-O-02 修复：供 v51_confirm_route 在调用 resume_v51_task 前记录版本号，
    用于检测并发修改。

    Returns:
        int | None: 当前版本号，状态不存在时返回 None
    """
    with _v51_states_lock:
        state = _v51_states.get(project_id)
    if state is None:
        return None
    return state._confirm_version


def get_v51_last_reject_reason(project_id: int) -> str | None:
    """获取最近一次 resume_v51_task 被拒的原因。

    BUG-FIX 2026-10-11（O-7）：供 v51_confirm_route 区分不同拒绝原因，
    以返回精准的 HTTP 状态码：
      - "version_conflict" → 409 Conflict（并发冲突）
      - "depth_mismatch"   → 400 Bad Request（提交的 outline 深度不符合当前 phase）
      - "invalid_outline"  → 400 Bad Request（outline 格式非法）
      - "no_state"         → 404 Not Found
      - None               → 未拒，或最新一次 resume 成功

    Returns:
        str | None: 拒绝原因码，状态不存在时返回 "no_state"，未拒/成功时返回 None
    """
    with _v51_states_lock:
        state = _v51_states.get(project_id)
    if state is None:
        return "no_state"
    return state._last_reject_reason


def get_v51_phase(project_id: int) -> str | None:
    """获取项目 V5.1 当前 phase（level1/level2/level3/final）。

    BUG-FIX 2026-10-11（O-7）：供路由层向客户端透出「实际允许的 outline 深度」，
    便于用户端在返回 400 时展示"当前处于 L1 审核窗口，请只提交章级目录"之类的
    可操作提示。

    Returns:
        str | None: 当前 phase；状态不存在时返回 None
    """
    with _v51_states_lock:
        state = _v51_states.get(project_id)
    return state.current_phase if state else None


def create_v51_state(project_id: int) -> V51TaskState:
    """创建项目 V5.1 状态"""
    state = V51TaskState(project_id)
    with _v51_states_lock:
        _v51_states[project_id] = state
    logger.info("V5.1 状态已创建: project=%d", project_id)
    return state


def remove_v51_state(project_id: int):
    """移除项目 V5.1 状态"""
    with _v51_states_lock:
        _v51_states.pop(project_id, None)
    logger.info("V5.1 状态已移除: project=%d", project_id)


def resume_v51_task(
    project_id: int, edited_outline: list[dict] | None = None, expected_version: int | None = None
) -> bool:
    """恢复 V5.1 生成任务

    BUG-O-02 修复：乐观锁版本检查
    - 传入 expected_version 时，检查当前 version 是否匹配
    - 不匹配说明有其他并发操作已修改了状态，返回 False

    BUG-FIX 2026-10-11（O-7）：新增层级深度校验
    - `state.current_phase` 决定本阶段允许收到的 outline 最大深度（见
      `_V51_PHASE_ALLOWED_DEPTH`）。若提交 outline 的深度超出允许区间，
      拒绝写入 `state.edited_outline`（避免下游 `_build_level2_skeleton`
      静默丢弃用户目录），返回 False 并设置 `_last_reject_reason`。
    - 校验通过/跳过时清空 `_last_reject_reason`，让下一次 resume 的
      返回值不受上次残留影响。

    Args:
        project_id: 项目 ID
        edited_outline: 用户审核编辑后的目录树（可选）
        expected_version: 期望的版本号（乐观锁检查），为 None 时不检查

    Returns:
        bool: 是否成功恢复；版本冲突、层级不匹配、outline 格式非法或状态不存在
              均返回 False，调用方通过 get_v51_last_reject_reason 区分原因。
    """
    with _v51_states_lock:
        state = _v51_states.get(project_id)
        if not state:
            logger.warning("V5.1 恢复失败: 项目 %d 无进行中的任务", project_id)
            return False

        # 乐观锁检查：并发版本冲突检测
        if expected_version is not None and state._confirm_version != expected_version:
            logger.warning(
                "V5.1 乐观锁冲突: project=%d, expected_version=%d, actual_version=%d - "
                "可能有多标签页并发操作同一项目",
                project_id,
                expected_version,
                state._confirm_version,
            )
            state._last_reject_reason = "version_conflict"
            return False

        # BUG-FIX 2026-10-11（O-7）：层级深度校验
        if edited_outline is not None:
            if not isinstance(edited_outline, list):
                logger.warning(
                    "V5.1 恢复拒绝：edited_outline 不是 list（type=%s），"
                    "phase=%s, project=%d",
                    type(edited_outline).__name__,
                    state.current_phase,
                    project_id,
                )
                state._last_reject_reason = "invalid_outline"
                return False
            actual_depth = _outline_max_depth(edited_outline)
            if not _phase_allows_depth(state.current_phase, actual_depth):
                rng = _V51_PHASE_ALLOWED_DEPTH[state.current_phase]
                logger.warning(
                    "V5.1 恢复拒绝：outline 深度=%d 超出 phase=%s 允许范围 [%d,%d] - "
                    "项目 %d。为避免下游重建骨架静默丢弃用户目录，已拒绝写入。",
                    actual_depth,
                    state.current_phase,
                    rng[0],
                    rng[1],
                    project_id,
                )
                state._last_reject_reason = "depth_mismatch"
                return False

            state.edited_outline = edited_outline

        # 每次确认递增版本号
        state._confirm_version += 1
        state._last_reject_reason = None  # 本次成功，清空残留

        logger.info(
            "V5.1 任务恢复: project=%d, phase=%s, version=%d",
            project_id,
            state.current_phase,
            state._confirm_version,
        )
    state.resume_event.set()
    return True


def cancel_v51_task(project_id: int) -> bool:
    """取消 V5.1 生成任务"""
    with _v51_states_lock:
        state = _v51_states.get(project_id)
        if not state:
            return False

        state.cancelled = True
    state.resume_event.set()
    # BUG-FIX 2026-10-13（取消后 DB 行状态残留）：`mark_v51_cancelled` 此前
    # 无任何调用方（死代码）——内存态 cancelled=True，但 v51_tasks 行的
    # cancelled 列仍为 0，断点续生/恢复界面拿到的是「未取消」的旧行，
    # 只能靠 2 小时 expired_at 兜底清理。取消是终态，在此统一落库。
    mark_v51_cancelled(project_id)
    logger.info("V5.1 任务已取消: project=%d", project_id)
    return True


def set_v51_active_run(project_id: int, active: bool) -> bool:
    """标记/解除「该项目有 V5.1 任务在运行」。

    BUG-FIX 2026-09-15（高）：`load_v51_state_from_db` 原本在结尾无条件执行
    `_v51_states[project_id] = state`。若该项目正在生成，工作流持有的是**旧**
    的 `V51TaskState` 对象，而 `cancel_v51_task` / `resume_v51_task` 都是
    「先 get_v51_state() 再改字段」—— 覆盖之后：

    - `cancel_v51_task` 把新对象的 `cancelled` 置 True，工作流检查的是旧对象
      → **「停止生成」静默失效**（用户以为停了，AI 还在烧）；
    - `resume_v51_task` 把 `resume_event` 设在新对象上，工作流等的是旧对象
      → 审核永远等不到（走 `auto_confirm=False` 的路径时）。

    Returns:
        bool: 是否找到并修改了状态对象（无状态时返回 False，调用方无需分支）。
    """
    with _v51_states_lock:
        state = _v51_states.get(project_id)
        if state is None:
            return False
        state.active_run = bool(active)
    return True


# ===== 数据库持久化方法 =====


def save_v51_state_to_db(state: V51TaskState) -> bool:
    """将 V5.1 状态保存到数据库（用于断点续生）

    Args:
        state: V5.1 任务状态对象

    Returns:
        bool: 是否保存成功
    """
    try:
        from ..db import get_db

        with get_db() as conn:
            # 序列化各字段
            level1_json = json.dumps(state.level1_outline or [], ensure_ascii=False)
            full_json = json.dumps(state.full_outline or [], ensure_ascii=False)
            edited_json = json.dumps(state.edited_outline or [], ensure_ascii=False)
            project_info_json = json.dumps(state.project_info or {}, ensure_ascii=False)

            # 计算过期时间（2小时后过期）
            expired_at = (datetime.now() + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")

            conn.execute(
                """
                INSERT INTO v51_tasks
                    (project_id, phase, current_phase, level1_outline_json,
                     level2_outline_json, final_outline_json, edited_outline_json,
                     cancelled, created_at, updated_at, expired_at,
                     summary, scoring_points, industry_profile, tender_context,
                     project_scope, project_info)
                VALUES (?, ?, ?, ?, NULL, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(project_id) DO UPDATE SET
                    phase = excluded.phase,
                    current_phase = excluded.current_phase,
                    level1_outline_json = excluded.level1_outline_json,
                    final_outline_json = excluded.final_outline_json,
                    edited_outline_json = excluded.edited_outline_json,
                    updated_at = excluded.updated_at,
                    expired_at = excluded.expired_at,
                    summary = excluded.summary,
                    scoring_points = excluded.scoring_points,
                    industry_profile = excluded.industry_profile,
                    tender_context = excluded.tender_context,
                    project_scope = excluded.project_scope,
                    project_info = excluded.project_info
            """,
                (
                    state.project_id,
                    state.current_phase,  # 使用状态对象的实际 phase
                    state.current_phase,
                    level1_json,
                    full_json,
                    edited_json,
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),  # created_at
                    # BUG 修复：此前此处缺失 updated_at 的值，导致从第 7 个占位符起
                    # 全部参数错位一列（updated_at←expired_at、expired_at←summary、
                    # summary←scoring_points … project_scope←project_info），
                    # 断点续生恢复时所有上下文字段取到的是相邻列的值。
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),  # updated_at
                    expired_at,
                    state.summary,
                    state.scoring_points,
                    state.industry_profile,
                    state.tender_context,
                    state.project_scope,
                    project_info_json,
                ),
            )
            logger.info(
                "V5.1 状态已保存到数据库: project=%d, phase=%s",
                state.project_id,
                state.current_phase,
            )
            return True
    except Exception as e:
        logger.error("保存 V5.1 状态到数据库失败: %s", e)
        return False


def load_v51_state_from_db(project_id: int) -> V51TaskState | None:
    """从数据库加载 V5.1 状态

    Args:
        project_id: 项目 ID

    Returns:
        V51TaskState | None: 加载的状态，如果不存在或已过期则返回 None
    """
    # BUG-FIX 2026-09-15（高）：该项目已有**正在运行**的 V5.1 任务时，
    # 直接返回内存里那份，**不要**用 DB 行重建覆盖。
    # 内存那份一定比 DB 行更新更全（DB 只在 finally 写一次），而且运行中的
    # 工作流持有它的引用 —— 覆盖会让 cancel/resume 打到空处（详见
    # set_v51_active_run 的注释）。
    with _v51_states_lock:
        _active = _v51_states.get(project_id)
    if _active is not None and _active.active_run:
        logger.info(
            "V5.1 任务正在运行，跳过 DB 重载以免覆盖活跃状态: project=%d", project_id
        )
        return _active

    # ---- 一次性扫全表清理所有过期脏行 ----
    # 原缺陷：SQL 只查 WHERE project_id = ?，然后在命中行上做过期判断。
    # 其他 project_id 的过期脏行永远不会被触发清理，长期堆积导致误判 pending。
    # 修复：进程启动后第一次进入此处时扫一次全表，标记所有 expired_at < now 的行为 cancelled=1。
    global _v51_expired_cleanup_done
    if not _v51_expired_cleanup_done:
        with _v51_expired_cleanup_lock:
            if not _v51_expired_cleanup_done:
                try:
                    from ..db import get_db
                    with get_db() as conn:
                        n = conn.execute(
                            "UPDATE v51_tasks SET cancelled = 1 "
                            "WHERE cancelled = 0 AND expired_at IS NOT NULL "
                            "AND datetime(expired_at) < datetime('now')"
                        ).rowcount
                        # BUG-FIX-EXPIRE-02：仅在清理成功后置位标记，
                        # 失败时下次进入重试（原实现 finally 无条件置位，失败后本进程内永不重试）
                        _v51_expired_cleanup_done = True
                        if n:
                            logger.warning(
                                "V5.1 过期脏行清理: %d 行 (进程级一次性)", n
                            )
                except Exception as _e:
                    logger.debug("V5.1 过期清理旁路跳过: %s", _e)

    try:
        from ..db import get_db

        with get_db() as conn:
            row = conn.execute(
                "SELECT * FROM v51_tasks WHERE project_id = ? AND cancelled = 0",
                (project_id,),
            ).fetchone()

            if not row:
                return None

            # 检查是否过期
            expired_at = row["expired_at"]
            if expired_at:
                try:
                    exp_dt = datetime.strptime(expired_at, "%Y-%m-%d %H:%M:%S")
                    if datetime.now() > exp_dt:
                        logger.info("V5.1 任务已过期: project=%d", project_id)
                        # 标记为过期
                        conn.execute(
                            "UPDATE v51_tasks SET cancelled = 1 WHERE project_id = ?",
                            (project_id,),
                        )
                        return None
                except ValueError:
                    # BUG-FIX-EXPIRE-01：过期时间损坏时不能当作未过期无限续命，
                    # 直接标记该任务过期，避免脏数据永久占用恢复槽位
                    logger.warning(
                        "V5.1 过期时间格式非法，按过期处理: project=%d, expired_at=%r",
                        project_id,
                        expired_at,
                    )
                    conn.execute(
                        "UPDATE v51_tasks SET cancelled = 1 WHERE project_id = ?",
                        (project_id,),
                    )
                    return None

            state = V51TaskState(project_id)
            state.current_phase = row["current_phase"] or "level1"
            state.cancelled = bool(row["cancelled"])

            # 反序列化 JSON 字段
            level1 = row["level1_outline_json"]
            if level1:
                try:
                    state.level1_outline = json.loads(level1)
                except (json.JSONDecodeError, TypeError):
                    state.level1_outline = []

            final = row["final_outline_json"]
            if final:
                try:
                    state.full_outline = json.loads(final)
                except (json.JSONDecodeError, TypeError):
                    state.full_outline = None

            edited = row["edited_outline_json"]
            if edited:
                try:
                    state.edited_outline = json.loads(edited)
                except (json.JSONDecodeError, TypeError):
                    state.edited_outline = None

            # 恢复上下文信息
            # BUG 修复：sqlite3.Row 没有 .get() 方法（db.py 设置 row_factory=sqlite3.Row），
            # 旧代码 row.get("summary") 抛 AttributeError 被 except 捕获后返回 None，
            # 导致 load_v51_state_from_db 永远失败、断点续生完全失效。
            # 改为先转 dict 再取值。
            row_dict = dict(row) if not isinstance(row, dict) else row
            state.summary = row_dict.get("summary") or ""
            state.scoring_points = row_dict.get("scoring_points") or ""
            state.industry_profile = row_dict.get("industry_profile") or ""
            state.tender_context = row_dict.get("tender_context") or ""
            state.project_scope = row_dict.get("project_scope") or ""

            project_info = row_dict.get("project_info")
            if project_info:
                try:
                    state.project_info = json.loads(project_info)
                except (json.JSONDecodeError, TypeError):
                    state.project_info = None

            # 加入内存状态字典
            with _v51_states_lock:
                _v51_states[project_id] = state
            logger.info(
                "V5.1 状态已从数据库加载: project=%d, phase=%s", project_id, state.current_phase
            )
            return state
    except Exception as e:
        logger.error("从数据库加载 V5.1 状态失败: %s", e)
        return None


def clear_v51_state_from_db(project_id: int):
    """从数据库清除 V5.1 状态（任务完成后调用）

    Args:
        project_id: 项目 ID
    """
    try:
        from ..db import get_db

        with get_db() as conn:
            conn.execute(
                "DELETE FROM v51_tasks WHERE project_id = ?",
                (project_id,),
            )
            logger.info("V5.1 状态已从数据库清除: project=%d", project_id)
    except Exception as e:
        logger.warning("清除 V5.1 数据库状态失败: %s", e)


def mark_v51_cancelled(project_id: int):
    """标记 V5.1 任务为已取消（用于取消场景）"""
    try:
        from ..db import get_db

        with get_db() as conn:
            conn.execute(
                "UPDATE v51_tasks SET cancelled = 1 WHERE project_id = ?",
                (project_id,),
            )
            logger.info("V5.1 任务已标记为取消: project=%d", project_id)
    except Exception as e:
        logger.warning("标记 V5.1 取消失败: %s", e)
