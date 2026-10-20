"""生成任务管理器

管理正文生成的暂停、恢复和进度追踪。
支持精确的断点续生功能。
"""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
import functools
import hashlib
import logging
import time
import uuid

from ..db import get_db_with_retry
from . import task_lock


logger = logging.getLogger(__name__)


def _task_group_lock_still_ours(lock_key: str, owner_id: str, project_id: int) -> bool:
    """判断「任务组锁」是否仍由本任务持有（同步执行，调用方需在线程池里跑）。

    FIX 2026-10-08（遗留8）：暂停 >30min（超过 `DEFAULT_TTL_SECONDS`）后恢复，
    锁可能已被 `reclaim_stale` 回收或被他进程抢走。此时继续复用旧任务 = 放行
    第二个并发写循环。判定：锁 live **且** owner 仍是自己。
    """
    if not lock_key or not owner_id:
        return False
    try:
        if not task_lock.is_lock_live(lock_key):
            return False
        active = task_lock.get_active_lock("content-generation", project_id=project_id)
    except Exception as e:  # 查询失败：交由调用方按保守策略处理
        logger.warning("[generation_manager] 任务组锁状态查询失败 key=%s: %s", lock_key, e)
        raise
    if not active:
        return False
    return (active.get("owner_id") or "") == owner_id


@dataclass
class SectionProgress:
    """单章节进度"""

    section_id: int
    title: str
    status: str = "pending"  # pending/processing/completed/failed/cancelled/needs_rewrite
    progress: int = 0  # 0-100
    retry_count: int = 0
    quality_score: float | None = None
    quality_grade: str | None = None
    content_hash: str | None = None
    last_error: str | None = None
    # BUG-FIX 2026-09-14：章节是否已被计入 task.completed_count。
    # 生成链路会先用 status="completed"（不带 content）回调一次，
    # 再用带 content 的方式回调一次；原实现依赖
    # `prev_status != "completed"` 去重，导致第二次才是"真正完成"的回调
    # 被漏计，completed_count 恒为 0、任务永远不进入 completed 状态。
    counted: bool = False
    # BUG-FIX 2026-09-16：失败/取消同样需要幂等计数（详见 update_section_progress）。
    failed_counted: bool = False
    cancelled_counted: bool = False


@dataclass
class GenerationTask:
    """生成任务"""

    generation_id: str
    project_id: int
    status: str = "running"  # running/paused/completed/cancelled/failed
    outline_version: str = ""
    total_sections: int = 0
    completed_count: int = 0
    failed_count: int = 0
    cancelled_count: int = 0
    started_at: str = ""
    paused_at: str | None = None
    resumed_at: str | None = None
    completed_at: str | None = None
    error_message: str = ""
    section_states: dict[int, SectionProgress] = field(default_factory=dict)
    writing_length: str = "standard"
    concurrency: int = 4
    # ENHANCE 2026-09-15：任务组锁的持有凭据。
    # 刻意**不持久化**到 generation_tasks：锁自带 TTL，进程重启后由
    # task_lock.reclaim_stale() 按时间回收即可；把凭据存进 DB 反而会引出
    # 「新进程把旧凭据当有效凭据、去释放别人的锁」这类问题。
    lock_key: str = ""
    lock_owner: str = ""
    last_heartbeat_at: float = 0.0
    # ENHANCE 2026-09-16（断点续生）：是否仍有「活的 SSE 循环」在驱动本任务。
    # True  = 有 run_generation 在跑（含客户端断开后的后台续跑）；
    # False = 已被 pause_task 暂停（任务与组锁都保留，等待 /generate-content 续跑）。
    # 关键用途：create_task 据此区分「真并发」（拒绝）与「恢复续跑」（复用）。
    attached: bool = True


class GenerationManager:
    """生成任务管理器

    职责：
    1. 管理所有活跃生成任务的内存状态
    2. 持久化进度到数据库
    3. 提供暂停/恢复接口
    4. 处理任务生命周期
    """

    #: 锁续约节流间隔（秒）。正文生成可能上千章节，逐章节写锁表没必要；
    #: 远小于 task_lock.DEFAULT_TTL_SECONDS（1800s），留足容错余量。
    HEARTBEAT_INTERVAL_SECONDS = 60

    def __init__(self):
        self._tasks: dict[int, GenerationTask] = {}
        self._lock = asyncio.Lock()
        # 持久化串行锁：同一项目内 _persist_task 的 INSERT OR REPLACE 必须按
        # 调用顺序串行执行。否则并发章节完成时，线程池中先读到旧快照的线程
        # 后完成写入，会用旧 completed_count 覆盖新值（竞态，导致进度回退）。
        self._persist_locks: dict[int, asyncio.Lock] = {}

    async def create_task(self, project_id: int, outline_version: str, config: dict) -> str:
        """创建新的生成任务

        Args:
            project_id: 项目ID
            outline_version: 目录版本哈希
            config: 生成配置

        Returns:
            generation_id (UUID字符串)
        """
        generation_id = str(uuid.uuid4())
        loop = asyncio.get_running_loop()

        # ENHANCE 2026-09-16（P0 断点续生）：本项目若存在「已暂停且无活循环」的任务，
        # 本次调用即「恢复续跑」——复用它的 generation_id 与组锁凭据，不重新抢锁。
        #
        # 背景（原缺陷）：pause_task 把任务标记为 paused 但**仍持有** content-generation
        # 组锁（resume_task 靠同一 owner 续约，见下方注释）。前端「恢复」的流程是
        #   resumeGeneration() → startGeneration() → /generate-content → create_task()
        # 旧实现无条件 `task_lock.acquire(...)` 且 owner 是新生成的 uuid → 必然
        # TaskLockConflict → SSE 立刻回 error。也就是说「暂停后恢复」100% 失败，
        # 章节状态停在 generating，用户只能停止后重跑（丢失已生成进度）。
        #
        # 复用而非新建的另一个好处：generation_tasks 里仍是同一行 generation_id，
        # 断点续生的进度记录（section_generation_progress）不会断链。
        reuse_task: GenerationTask | None = None
        async with self._lock:
            existing = self._tasks.get(project_id)
            # BUG-FIX 2026-10-08（高）：原条件只有 `not existing.attached`，不看 status。
            # attached 是「是否有活循环在驱动」的标志，而 resume_task() 会把 status 置回
            # running 却**故意不置 attached**（置 True 会让本函数走抢锁分支、用新 owner
            # 与自己持有的锁冲突 → 恢复 100% 失败）。于是内存里存在一个
            # 「status=running 且 attached=False」的中间态：此刻任何一次 /generate-content
            # 都会被判为「续跑」而静默复用同一 generation_id，抢锁被跳过。
            # 后果：并发两个生成循环同写一批 sections；或一个已终态但尚未 finalize 的任务
            # 被当作可续跑任务复用，section_states / 锁凭据全部错位。
            # 现在只允许「paused」或「已恢复待挂载（running 且未挂载）」两种状态复用，
            # 其余（completed/failed/cancelled）先清出内存再走正常抢锁路径。
            if existing is not None and not existing.attached:
                # FIX 2026-10-08（遗留8）：暂停 >30min 后恢复必须校验任务组锁存活性。
                # 原实现校验了 status/attached 却**不校验锁**——锁被 reclaim_stale 回收
                # 或被他进程抢走时，这里仍照旧复用，恢复出的新循环会与其它持锁者
                # 并发 UPDATE 同一批 sections（正是任务组锁要防的场景）。
                # 校验方式：锁仍 live + owner 仍是本任务；查询失败时保守放行（不改既有行为）。
                _lock_valid = True
                if existing.lock_key:
                    try:
                        _lock_valid = await loop.run_in_executor(
                            None,
                            functools.partial(
                                _task_group_lock_still_ours,
                                existing.lock_key, existing.lock_owner, project_id,
                            ),
                        )
                    except Exception as _lk_e:
                        logger.warning(
                            "[generation_manager] 任务组锁存活性查询异常，按既有行为放行: "
                            "project=%d: %s", project_id, _lk_e,
                        )
                        _lock_valid = True
                if not _lock_valid:
                    logger.warning(
                        "[generation_manager] 任务组锁已失效或易主，放弃复用并按新任务处理: "
                        "project=%d, generation=%s, lock_key=%s",
                        project_id, existing.generation_id, existing.lock_key,
                    )
                    self._tasks.pop(project_id, None)
                elif existing.status in ("paused", "running"):
                    reuse_task = GenerationTask(
                        generation_id=existing.generation_id,
                        project_id=project_id,
                        outline_version=outline_version or existing.outline_version,
                        started_at=existing.started_at,
                        resumed_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        writing_length=config.get("writing_length", existing.writing_length),
                        concurrency=int(config.get("concurrency", existing.concurrency) or existing.concurrency),
                        # 锁凭据必须原样继承：锁在 DB 里仍挂在这个 owner 名下
                        lock_key=existing.lock_key,
                        lock_owner=existing.lock_owner,
                        last_heartbeat_at=time.time(),
                    )
                    self._tasks[project_id] = reuse_task
                    generation_id = existing.generation_id
                    logger.info(
                        "[generation_manager] 复用暂停任务续跑: project=%d, generation=%s",
                        project_id,
                        generation_id,
                    )
                else:
                    logger.warning(
                        "[generation_manager] 清理已终态的僵尸任务后重新创建: "
                        "project=%d, generation=%s, status=%s",
                        project_id,
                        existing.generation_id,
                        existing.status,
                    )
                    self._tasks.pop(project_id, None)

        if reuse_task is not None:
            await loop.run_in_executor(None, self._persist_task, reuse_task)
            return generation_id

        # ENHANCE 2026-09-15（高）：任务组互斥。
        # 原实现没有任何并发保护：重复调用 create_task 会静默覆盖
        # self._tasks[project_id] —— 旧生成循环仍在跑，但它的进度更新会
        # 被记到新 generation_id 上；同时 generation_tasks 里会插入第二行
        # running 记录，两个循环并发写同一批 sections。
        # 后果：进度数字错乱、AI 成本翻倍、正文互相覆盖。
        # 现在：先抢锁，冲突直接抛 TaskLockConflict（内存/DB 零改动）；
        # 抢到锁后若创建过程出错，回滚释放锁，避免占着锁不放。
        lock_record = await loop.run_in_executor(
            None,
            functools.partial(
                task_lock.acquire,
                "content-generation",
                project_id=project_id,
                note=f"generation_id={generation_id} outline_version={outline_version}",
            ),
        )
        try:
            task = GenerationTask(
                generation_id=generation_id,
                project_id=project_id,
                outline_version=outline_version,
                started_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                writing_length=config.get("writing_length", "standard"),
                concurrency=config.get("concurrency", 4),
                lock_key=lock_record.get("lock_key", ""),
                lock_owner=lock_record.get("owner_id", ""),
                last_heartbeat_at=time.time(),
            )

            async with self._lock:
                self._tasks[project_id] = task
        except BaseException:
            # BUG-FIX（P1 锁泄漏）：`asyncio.CancelledError` 自 Python 3.8 起继承
            # BaseException，`except Exception` 捕获不到它。用户在建流过程中关闭
            # 页面 → Starlette 取消协程 → 锁不释放 → 该项目 30 分钟（TTL）内
            # 所有正文生成/兜底扩写都被 TASK_LOCK_CONFLICT 拒绝。
            try:
                await loop.run_in_executor(
                    None,
                    functools.partial(
                        task_lock.release,
                        lock_record.get("lock_key", ""),
                        lock_record.get("owner_id", ""),
                    ),
                )
            except Exception as _e:  # 释放失败不能掩盖原始异常
                logger.warning(
                    "创建任务失败后释放组锁异常 project=%s: %s", project_id, _e
                )
            raise
        # 同步内存操作完成，立即释放锁；持久化在锁外异步执行
        # 注意：传方法本身而非调用结果，run_in_executor 会在 Executor 线程中调用

        logger.info("创建生成任务: project=%d, generation=%s", project_id, generation_id)
        # 锁外异步持久化，不阻塞事件循环
        loop = asyncio.get_running_loop()
        # BUG-FIX 2026-09-16（中）：return_exceptions=True 会把持久化异常静默收走，
        # DB 写失败（连接池耗尽/磁盘错误）时任务记录丢失且无从排查。至少留痕。
        _persist_results = await asyncio.gather(
            loop.run_in_executor(None, self._persist_task, task),
            loop.run_in_executor(
                None,
                self._persist_section_progress,
                task.generation_id,
                SectionProgress(section_id=-1, title="_init_"),
            ),
            return_exceptions=True,
        )
        for _r in _persist_results:
            if isinstance(_r, BaseException):
                logger.warning(
                    "[generation_manager] 创建任务持久化失败 generation=%s: %r",
                    generation_id,
                    _r,
                )
        return generation_id

    async def is_paused(self, project_id: int) -> bool:
        """本项目是否存在已暂停（保留断点续生现场）的任务。

        ENHANCE 2026-09-16：供 SSE 收尾逻辑判定「这次结束是暂停还是真停止」。
        只读内存快照，不触发任何持久化，可安全地在 finally 中调用。
        """
        async with self._lock:
            task = self._tasks.get(project_id)
            return bool(task is not None and task.status == "paused")

    async def pause_task(self, project_id: int) -> bool:
        """暂停生成任务

        Args:
            project_id: 项目ID

        Returns:
            是否成功暂停
        """
        task = None
        async with self._lock:
            task = self._tasks.get(project_id)
            if not task or task.status != "running":
                return False
            task.status = "paused"
            task.paused_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            # ENHANCE 2026-09-16：标记「无活循环」。SSE 侧据此跳过 finalize_task
            # （否则会把暂停任务从内存删除并释放组锁 → resume 找不到任务、恢复恒失败），
            # create_task 也据此把下次调用识别为「续跑」而非「并发冲突」。
            task.attached = False

        logger.info("暂停生成任务: project=%d, generation=%s", project_id, task.generation_id)
        # 持久化在锁外执行
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._persist_task, task)
        return True

    async def resume_task(self, project_id: int) -> str | None:
        """恢复生成任务

        Args:
            project_id: 项目ID

        Returns:
            generation_id 或 None（如果恢复失败）
        """
        task = None
        async with self._lock:
            task = self._tasks.get(project_id)
            if not task or task.status != "paused":
                return None

        # BUG-FIX（P1 并发写冲突）：暂停期间心跳停止，锁可能在 30 分钟 TTL 后过期，
        # 期间别的用户/任务可以合法抢到同一把 content-generation 锁。旧实现恢复时
        # 不校验也不重获锁，于是「恢复的生成循环」与「新起的生成循环」会并发
        # UPDATE 同一批 sections。这里用相同 owner 重新获取（等价续约）：
        # 拿不到说明锁已易主，直接拒绝恢复。
        if task.lock_key and task.lock_owner:
            loop = asyncio.get_running_loop()
            try:
                await loop.run_in_executor(
                    None,
                    functools.partial(
                        task_lock.acquire,
                        "content-generation",
                        project_id=project_id,
                        owner_id=task.lock_owner,
                        allow_same_owner=True,
                        note=f"resume generation_id={task.generation_id}",
                    ),
                )
            except Exception as _e:
                logger.warning(
                    "恢复生成任务失败：任务组锁已被其他任务持有 project=%d: %s",
                    project_id,
                    _e,
                )
                return None

        async with self._lock:
            task = self._tasks.get(project_id)
            if not task or task.status != "paused":
                return None
            task.status = "running"
            task.resumed_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            # BUG-FIX 2026-10-08（高·不变式说明）：这里**刻意不置 attached=True**。
            # attached=True 表示「已有活 SSE 循环在驱动」，但此刻还没有——前端拿到本接口
            # 的 generation_id 之后才会去调 /generate-content 建流。若在此置 True，
            # create_task 会走「抢 content-generation 组锁」分支，而锁此刻仍由本任务的
            # lock_owner 持有且 owner_id 是新 uuid → 必然 TaskLockConflict，
            # 「暂停→恢复」将 100% 失败（这正是 2026-09-16 修复的那个坑）。
            # 因此保持 attached=False，由 create_task 的复用分支在**锁内原子地**
            # 翻成 running + attached=True（复用本身是互斥的：第二个并发请求会看到
            # attached=True 从而走抢锁分支并被正确拒绝）。
            task.attached = False

        logger.info("恢复生成任务: project=%d, generation=%s", project_id, task.generation_id)
        # 持久化在锁外执行
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._persist_task, task)
        return task.generation_id

    async def update_section_progress(
        self,
        project_id: int,
        section_id: int,
        status: str,
        progress: int = 0,
        content: str | None = None,
        quality_score: float | None = None,
        error: str | None = None,
    ):
        """更新章节进度并持久化

        优化：所有内存操作在锁内完成，持久化在锁外异步执行，
        避免阻塞事件循环和其他并发任务。
        """
        generation_id = None
        state: SectionProgress | None = None

        async with self._lock:
            task = self._tasks.get(project_id)
            if not task:
                return
            generation_id = task.generation_id

            if section_id not in task.section_states:
                task.section_states[section_id] = SectionProgress(
                    section_id=section_id,
                    title=f"section_{section_id}",
                )

            state = task.section_states[section_id]
            prev_status = state.status
            state.status = status
            state.progress = progress

            # 状态从 completed 回退（重新生成 / 重写）时撤销已计数值，避免重复计数
            if state.counted and status != "completed":
                state.counted = False
                task.completed_count = max(0, task.completed_count - 1)

            # BUG-FIX 2026-09-16（P1 计数失真）：失败/取消此前用 `prev_status != X`
            # 去重，而 SSE 重试时会在再次失败前先回调 `status="generating"`
            # （sse_content.py 的进度回调），prev_status 因此不再是 failed ——
            # 同一章节重试 N 次就给 failed_count 加 N 次。计数一超额，
            # 下面的 `total >= total_sections` 会把还在生成（配图/扩写未收尾）的任务
            # 提前写成终态，而 pause_task 要求 status == "running"，此后暂停全部静默失败。
            # 与 completed 对齐，改用显式旗标：进终态计一次，离开终态撤销。
            if state.failed_counted and status != "failed":
                state.failed_counted = False
                task.failed_count = max(0, task.failed_count - 1)
            if state.cancelled_counted and status != "cancelled":
                state.cancelled_counted = False
                task.cancelled_count = max(0, task.cancelled_count - 1)

            if quality_score is not None:
                state.quality_score = quality_score
                state.quality_grade = self._score_to_grade(quality_score)

            if content:
                state.content_hash = hashlib.md5(content.encode()).hexdigest()
                # BUG-FIX 2026-09-14（高）：改用显式 counted 标志去重。
                # 原条件 `prev_status != "completed"` 在「先回调 completed(无content)、
                # 再回调 completed(带content)」的顺序下恒为假，completed_count 永远为 0。
                if status == "completed" and not state.counted:
                    state.counted = True
                    task.completed_count += 1
            elif error:
                state.last_error = error
                if status == "failed" and not state.failed_counted:
                    state.failed_counted = True
                    task.failed_count += 1

            if status == "cancelled" and not state.cancelled_counted:
                state.cancelled_counted = True
                task.cancelled_count += 1

            total = task.completed_count + task.failed_count + task.cancelled_count
            if total >= task.total_sections and task.total_sections > 0:
                task.status = "completed" if task.failed_count == 0 else "failed"
                task.completed_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            # 持久化快照在锁外执行，不阻塞事件循环
            # 注意：传方法本身，run_in_executor 会在 Executor 线程中调用

        # 锁外异步持久化（不持有业务锁，不阻塞事件循环）
        # 性能修复：同一项目内的 _persist_task 必须串行执行——
        # INSERT OR REPLACE 整行写入，若并发执行，先读到旧计数的线程
        # 后完成写入，会用旧 completed_count 覆盖新值（进度回退竞态）。
        # 串行化后写入顺序与计数递增顺序一致，DB 最终值=内存最终值。
        # ENHANCE 2026-09-15：章节进度更新是天然的续约点。
        # 内部按 HEARTBEAT_INTERVAL_SECONDS 节流，避免上千个章节各写一次锁表。
        await self._maybe_heartbeat(project_id, task)

        persist_lock = self._persist_locks.setdefault(project_id, asyncio.Lock())
        async with persist_lock:
            loop = asyncio.get_running_loop()
            _progress_results = await asyncio.gather(
                loop.run_in_executor(None, self._persist_task, task),
                loop.run_in_executor(None, self._persist_section_progress, generation_id, state),
                return_exceptions=True,
            )
            for _r in _progress_results:
                if isinstance(_r, BaseException):
                    logger.warning(
                        "[generation_manager] 章节进度持久化失败 project=%d section=%s: %r",
                        project_id,
                        section_id,
                        _r,
                    )

    async def _maybe_heartbeat(self, project_id: int, task: GenerationTask) -> bool:
        """为任务组锁续约（按间隔节流）。

        返回 False 表示锁已不属于本任务（被超时回收后由别的任务抢走）。
        这种情况**只告警不中断**：正文已经生成到一半，中断只会留下更破碎的
        状态；但日志里必须留痕，否则事后无法判断正文是否被并发写过。
        """
        if not task.lock_key or not task.lock_owner:
            return True
        now_ts = time.time()
        if now_ts - task.last_heartbeat_at < self.HEARTBEAT_INTERVAL_SECONDS:
            return True
        task.last_heartbeat_at = now_ts
        loop = asyncio.get_running_loop()
        ok = await loop.run_in_executor(
            None,
            functools.partial(task_lock.heartbeat, task.lock_key, task.lock_owner),
        )
        if not ok:
            logger.warning(
                "[generation_manager] 任务组锁续约失败，本次生成已失去互斥保护："
                "project=%d generation=%s lock_key=%s",
                project_id,
                task.generation_id,
                task.lock_key,
            )
        return bool(ok)

    async def get_project_progress(self, project_id: int) -> dict:
        """获取项目的生成进度

        BUG #4 修复：自动修复状态不一致
        原实现直接返回 DB 中的状态，但服务重启/异常中断后 DB 中可能残留
        "running" 状态的 task，而实际已无活跃任务，造成前端显示混乱。
        修复：检查内存中是否有活跃任务，若 DB 标记 running 但内存无，
        自动将 DB 状态改为 failed/stopped，并同步更新 sections 状态。

        Args:
            project_id: 项目ID

        Returns:
            进度信息字典
        """
        with get_db_with_retry() as conn:
            # 查询最新的活跃生成任务
            task = conn.execute(
                """SELECT * FROM generation_tasks
                   WHERE project_id = ? AND status IN ('running', 'paused')
                   ORDER BY started_at DESC LIMIT 1""",
                (project_id,),
            ).fetchone()

            if not task:
                return {"has_active_task": False}

            # BUG #4 修复：检查内存中是否有对应 task，若无则视为异常残留
            async with self._lock:
                in_memory_task = self._tasks.get(project_id)

            # ENHANCE 2026-09-15（高）：判定「残留任务」之前必须先看任务组锁。
            # 原判据是「DB 有 running/paused 行，但**本进程**内存里没有对应实例」，
            # 这条判据在多进程部署下会**误杀另一个进程正在跑的任务**：
            # Celery worker / 另一个 uvicorn worker 正在生成时，API 进程的内存里
            # 当然没有该实例 → 于是把任务改成 failed，并把该项目的 sections
            # 全部 generating 改成 failed，正在生成的正文被当场判死。
            # 现在：锁仍活跃且未过期 → 说明确有进程持有它 → 原样返回，不做任何修正。
            # 注意用 read_lock_state（复用当前连接），不要用 get_active_lock：
            # 后者自己开 get_db()，退出时会 commit，会把外层事务提前提交。
            lock_state = task_lock.read_lock_state(conn, "content-generation", project_id)
            owned_elsewhere = bool(lock_state["held"] and lock_state["live"])
            if owned_elsewhere:
                logger.info(
                    "[generation-status] 项目 %d 的任务 %s 由其他进程持有任务组锁（owner=%s），"
                    "本进程不做残留修正",
                    project_id,
                    task["generation_id"],
                    (lock_state["holder"] or {}).get("owner_id", ""),
                )

            # DB 显示 running/paused，但内存中没有此任务的实例，且锁也没被别处持有
            # 场景：服务重启 / 进程崩溃 / 异常中断
            if not owned_elsewhere and (
                in_memory_task is None or in_memory_task.generation_id != task["generation_id"]
            ):
                logger.warning(
                    "[generation-status] 检测到状态不一致：项目 %d 的任务 %s 在 DB 中为 %s，但内存中无对应实例，自动标记为失败",
                    project_id,
                    task["generation_id"],
                    task["status"],
                )
                # 修正 DB 中残留的 task 状态
                conn.execute("BEGIN")
                try:
                    conn.execute(
                        """UPDATE generation_tasks
                           SET status = 'failed',
                               completed_at = datetime('now', 'localtime'),
                               error_message = '服务异常中断，任务状态已自动修正'
                           WHERE generation_id = ?""",
                        (task["generation_id"],),
                    )
# 将 progress 表中仍在「生成中」的章节标记为 failed。
                    # BUG-FIX 2026-10-08（P0 死代码）：原条件是 status = 'processing'，
                    # 但本表的 status 由 update_section_progress() 写入，取值是 SSE 的
                    # 章节状态串 —— 'generating' / 'completed' / 'failed' / 'cancelled'，
                    # **'processing' 从来不会被写入**（只有 uploaded_files/tasks 用这个词）。
                    # 于是这条 UPDATE 恒匹配 0 行：进程被 OOM/kill 后 section_generation_progress
                    # 里的行永远停在 'generating'，而 get_pending_sections 的
                    # 「status IN ('pending','failed','cancelled')」又把它们排除 →
                    # 这些章节既不是「在飞」也不是「待生成」，断点续跑会静默跳过它们，
                    # 且 total_sections 少算，任务可能被提前判为 completed。
                    # 现同时兼容新旧两种字面量（老版本可能已落库 'processing'）。
                    conn.execute(
                        """UPDATE section_generation_progress
                           SET status = 'failed', last_error = '服务异常中断'
                           WHERE generation_id = ? AND status IN ('generating', 'processing')""",
                        (task["generation_id"],),
                    )
                    # 将 sections 表中所有 generating 状态章节标记为 failed
                    conn.execute(
                        """UPDATE sections
                           SET status = 'failed', updated_at = datetime('now', 'localtime')
                           WHERE project_id = ? AND status = 'generating'""",
                        (project_id,),
                    )
                    conn.execute("COMMIT")
                except Exception:
                    conn.execute("ROLLBACK")
                    raise
                # BUG #8 修复：任务已被自动标记为失败，不再返回为"活跃任务"
                # 原实现仍返回 has_active_task=True，导致前端误以为还在生成中
                # 应返回 has_active_task=False（任务已结束，仅返回历史状态供查询）
                return {
                    "has_active_task": False,
                    "generation_id": task["generation_id"],
                    "task_status": "failed",
                    "total_sections": task["total_sections"],
                    "completed_count": task["completed_count"],
                    "failed_count": task["failed_count"],
                    "cancelled_count": task["cancelled_count"],
                    "started_at": task["started_at"],
                    "paused_at": task["paused_at"],
                    "auto_corrected": True,
                    "owned_elsewhere": False,
                    "message": "检测到残留任务，已自动标记为失败",
                }

            # 查询章节进度
            sections = conn.execute(
                """SELECT * FROM section_generation_progress
                   WHERE generation_id = ? ORDER BY section_id""",
                (task["generation_id"],),
            ).fetchall()

            return {
                "has_active_task": True,
                "generation_id": task["generation_id"],
                "task_status": task["status"],
                "total_sections": task["total_sections"],
                "completed_count": task["completed_count"],
                "failed_count": task["failed_count"],
                "cancelled_count": task["cancelled_count"],
                "started_at": task["started_at"],
                "paused_at": task["paused_at"],
                "sections": [dict(s) for s in sections],
                "owned_elsewhere": owned_elsewhere,
            }

    async def get_pending_sections(self, project_id: int) -> list[dict]:
        """获取需要恢复的章节列表（限定于最新活跃 generation）

        BUG 修复：原查询缺少 generation_id 过滤，会返回所有 generation 的
        待恢复章节。修复：先获取最新活跃 generation_id，再用该 ID 过滤
        section_generation_progress 表，确保只返回当前 generation 的章节。
        """
        # ENHANCE 2026-09-16（并发）：本函数全是 SELECT，走 readonly 不抢全局写锁。
        # 前端轮询 /pending-sections 时会打到这里，旧实现每一次轮询都要
        # 排队等正文生成的写事务（写锁持有期间最长可超时 8s 直接报错）。
        with get_db_with_retry(readonly=True) as conn:
            latest_task = conn.execute(
                """SELECT generation_id FROM generation_tasks
                   WHERE project_id = ? AND status IN ('running', 'paused')
                   ORDER BY started_at DESC LIMIT 1""",
                (project_id,),
            ).fetchone()
            generation_id = latest_task["generation_id"] if latest_task else None

            if generation_id:
                pending = conn.execute(
                    """SELECT s.id, s.parent_id, s.sort_order, s.title, s.level,
                              s.section_type, s.scoring_point_id,
                              s.content, s.target_words, s.status,
                              p.progress, p.retry_count, p.last_error
                        FROM sections s
                        LEFT JOIN section_generation_progress p
                                ON s.id = p.section_id AND p.generation_id = ?
                        WHERE s.project_id = ?
                          AND (p.status IS NULL OR p.status IN ('pending', 'failed', 'cancelled'))
                        ORDER BY s.id""",
                    (generation_id, project_id),
                ).fetchall()
            else:
                pending = conn.execute(
                    """SELECT s.id, s.parent_id, s.sort_order, s.title, s.level,
                              s.section_type, s.scoring_point_id,
                              s.content, s.target_words, s.status,
                              NULL AS progress, NULL AS retry_count, NULL AS last_error
                        FROM sections s
                        WHERE s.project_id = ? AND s.status != 'completed'
                        ORDER BY s.id""",
                    (project_id,),
                ).fetchall()

            pending = [dict(row) for row in pending]

            # 遗留加固（2026-10-19）：待恢复章节按目录树深度优先前序返回。
            # 旧口径 `ORDER BY s.sort_order` 是**跨父扁平**排序，两个 L1 子树的
            # 兄弟会被交错（1.1, 2.1, 1.2, 2.2），且同父 sort_order 重复时次序
            # 不确定 —— 而本结果被 /pending-sections **分页**下发给前端，
            # 排序不稳定会让翻页重复/漏项。统一收敛到唯一权威口径。
            try:
                from .ai.ordered_generation import order_tree_dfs
                pending = order_tree_dfs(pending)
            except ImportError:  # pragma: no cover - 防御性兜底（等价旧口径）
                pending.sort(key=lambda r: (r.get("sort_order") or 0, r.get("id") or 0))

            return pending

    def _persist_task(self, task: GenerationTask):
        """持久化任务到数据库"""
        with get_db_with_retry() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO generation_tasks
                   (generation_id, project_id, status, outline_version,
                    started_at, paused_at, resumed_at, completed_at,
                    total_sections, completed_count, failed_count, cancelled_count,
                    writing_length, concurrency, error_message,
                    lock_key, lock_owner)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    task.generation_id,
                    task.project_id,
                    task.status,
                    task.outline_version,
                    task.started_at,
                    task.paused_at,
                    task.resumed_at,
                    task.completed_at,
                    task.total_sections,
                    task.completed_count,
                    task.failed_count,
                    task.cancelled_count,
                    task.writing_length,
                    task.concurrency,
                    task.error_message,
                    task.lock_key,
                    task.lock_owner,
                ),
            )

    @staticmethod
    def _load_persisted_lock(project_id: int) -> tuple[str, str]:
        """读取该项目仍登记在库的任务组锁凭据（多 worker 下跨进程释放用）。"""
        try:
            with get_db_with_retry(readonly=True) as conn:
                row = conn.execute(
                    """SELECT lock_key, lock_owner FROM generation_tasks
                       WHERE project_id = ? AND lock_key != '' AND lock_owner != ''
                         AND status IN ('running', 'paused')
                       ORDER BY rowid DESC LIMIT 1""",
                    (project_id,),
                ).fetchone()
            if row:
                return row["lock_key"] or "", row["lock_owner"] or ""
        except Exception as _e:
            logger.debug("读取已持久化锁凭据失败 project=%s: %s", project_id, _e)
        return "", ""

    def _persist_section_progress(self, generation_id: str, state: SectionProgress):
        """持久化章节进度到数据库"""
        with get_db_with_retry() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO section_generation_progress
                   (generation_id, section_id, status, progress, retry_count,
                    quality_score, quality_grade, content_hash, last_error)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    generation_id,
                    state.section_id,
                    state.status,
                    state.progress,
                    state.retry_count,
                    state.quality_score,
                    state.quality_grade,
                    state.content_hash,
                    state.last_error,
                ),
            )

    @staticmethod
    def _score_to_grade(score: float) -> str:
        """将分数转换为等级"""
        if score >= 90:
            return "excellent"
        if score >= 70:
            return "good"
        if score >= 50:
            return "fair"
        return "poor"

    async def cleanup_completed_tasks(self, max_hours: int = 24) -> int:
        """清理已完成的旧任务，返回被删除的生成任务条数。

        BUG-FIX 2026-09-18（中）：原实现**不返回任何结果** —— 调用方（Celery
        `cleanup_old_results`、`enhancement_orchestrator`）无从判断清理是否真的
        生效，只能盲打一条日志，属于「任务运行无任何结果」。同时它只删
        `generation_tasks` 主表，不删 `section_generation_progress` 明细：
        明细以 generation_id 关联主表，主表行一删它们就成了永久孤儿行，只增不减。
        现一并清理并回传真实条数。

        Args:
            max_hours: 保留最近N小时的任务

        Returns:
            删除的 generation_tasks 行数
        """
        from datetime import timedelta

        cutoff_time = (datetime.now() - timedelta(hours=max_hours)).strftime("%Y-%m-%d %H:%M:%S")

        deleted = 0
        with get_db_with_retry() as conn:
            cursor = conn.execute(
                """DELETE FROM generation_tasks
                   WHERE status IN ('completed', 'failed', 'cancelled')
                     AND completed_at < ?""",
                (cutoff_time,),
            )
            deleted = cursor.rowcount or 0
            # 清理主表行已删除后残留的孤儿进度明细
            conn.execute(
                """DELETE FROM section_generation_progress
                   WHERE generation_id NOT IN (SELECT generation_id FROM generation_tasks)"""
            )

        logger.info("清理过期生成任务（%d小时前），删除 %d 条", max_hours, deleted)
        return deleted

    async def finalize_task(
        self, project_id: int, task_status: str = "completed", error_message: str = ""
    ):
        """BUG #11 修复：结束任务时从内存中清理任务对象

        优化：持久化在锁外异步执行，避免阻塞事件循环。

        场景：SSE 流结束（正常完成 / 客户端断开 / 异常中断）后，必须从
        generation_manager._tasks 中移除 task，否则 has_active_task 永远
        返回 True，前端进度面板会一直显示「正在生成」。

        BUG-C-02 增强：同时清理 DB 中 sections 表残留 generating 状态
        的章节，将其回滚为 failed，确保断点续传时不会读到脏状态。

        行为：
        - 标记 task 状态为完成态（completed/failed/cancelled）
        - 设置 completed_at 时间戳
        - 持久化到数据库
        - 从 _tasks 内存字典中移除该任务
        - 清理 sections 表中的 generating 状态残留
        """
        task = None
        released_lock_key = ""
        released_lock_owner = ""
        # BUG-FIX 2026-09-16（高 锁争抢下的长阻塞）：文件的自述（下方 782 行）
        # 明确要求「DB 写入必须在 `self._lock` 之外」，但 early-return 分支却在锁内
        # `await self.rollback_dirty_sections(...)` —— SQLite 繁忙时，这一分支会把
        # update_section_progress / pause_task / is_paused 的调用者全部堵住
        # （它们在等同一把 asyncio 锁）。改为在锁外汇票后再执行 DB 回滚。
        no_task_in_memory = False
        async with self._lock:
            task = self._tasks.get(project_id)
            if not task:
                no_task_in_memory = True

        if no_task_in_memory:
            # BUG-C-02: 内存中无任务但 DB 可能有残留 generating 章节，
            # 仍需回滚清理，避免下次断点续传读到脏状态
            await self.rollback_dirty_sections(project_id)
            # BUG-FIX（P1 锁泄漏）：多 worker 部署下，「停止生成」的 HTTP 请求
            # 可能落到没有该内存任务的进程上，旧实现在此直接 return，锁要等
            # 30 分钟 TTL 才回收。现在从 generation_tasks 里取回锁凭据，
            # release 内部会校验 owner，不会误释放别人的锁。
            loop = asyncio.get_running_loop()
            key, owner = await loop.run_in_executor(
                None, self._load_persisted_lock, project_id
            )
            if key and owner:
                await loop.run_in_executor(
                    None, functools.partial(task_lock.release, key, owner)
                )
                logger.warning(
                    "[generation_manager] 跨进程兜底释放组锁 project=%d key=%s",
                    project_id,
                    key,
                )
            return False

        loop = asyncio.get_running_loop()
        async with self._lock:
            task = self._tasks.get(project_id)
            if not task:
                # 极窄竞态：上面的检查后、本次拿锁前任务已被别的协程 finalize
                return False

            task.status = task_status
            task.completed_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            if error_message:
                task.error_message = error_message

            # 从内存中移除（先把锁凭据抄出来，对象随后就没了）
            released_lock_key = task.lock_key
            released_lock_owner = task.lock_owner
            del self._tasks[project_id]

            # 持久化快照在锁外执行

        # 锁外异步持久化 + 回滚 sections（必须在锁外，避免 DB 锁 + asyncio 锁嵌套）
        await self.rollback_dirty_sections(project_id)

        # 串行锁持久化，避免与并发章节进度写入竞态交错
        persist_lock = self._persist_locks.setdefault(project_id, asyncio.Lock())
        async with persist_lock:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self._persist_task, task)
        # BUG-FIX（P2 内存泄漏）：`_persist_locks` 只增不减，每个触碰过的项目永久
        # 留一个 asyncio.Lock（且跨事件循环复用会报 "attached to a different loop"）。
        # BUG-FIX 2026-09-16（中）：不能无条件 pop——并发 update_section_progress
        # 可能已通过 setdefault 拿到旧锁引用并正在等锁；pop 后新写入会新建 Lock，
        # 与旧锁等待者并行放行 → 旧快照覆盖新计数（进度回退竞态）。
        # 只在锁空闲（无人持有/等待）时回收；仍有等待者时保留孤儿锁，
        # 它们彼此仍串行，且 finalize 之后 task 已从 _tasks 删除、不会再有新 setdefault。
        if not persist_lock.locked():
            self._persist_locks.pop(project_id, None)

        # ENHANCE 2026-09-15：任务结束必须释放任务组锁，否则同项目的下一个任务
        # 会被自己残留的锁一直挡到 TTL 超时（默认 30 分钟）。
        # release 内部校验 owner，所以即使锁已被超时回收后由别的任务抢走，
        # 这里也不会误释放别人的锁（ABA）。
        if released_lock_key:
            await loop.run_in_executor(
                None,
                functools.partial(
                    task_lock.release, released_lock_key, released_lock_owner
                ),
            )

        logger.info(
            "[generation_manager] 任务已结束: project=%d, generation=%s, status=%s",
            project_id,
            task.generation_id,
            task_status,
        )
        return True

    async def rollback_dirty_sections(self, project_id: int, generation_id: str | None = None):
        """BUG-C-02 修复：回滚 sections 表中脏状态（generating → failed）

        场景：任务取消 / 客户端断开 / 进程重启后，sections 表中可能残留
        generating 状态的章节。这些章节永远不会被 update，也阻止断点续传
        正确识别哪些章节需要重试。

        清理动作（单事务）：
        1. sections 表: status='generating' → 'failed' + error_message
        2. section_generation_progress 表: status='generating'（兼容旧 'processing'）→ 'failed'

        Args:
            project_id: 项目 ID
            generation_id: 可选，指定要清理的 generation；None 时清理所有 running
        """
        # BUG-FIX 2026-09-15：删掉遗留死导入 `import asyncio as _asyncio` —— 它从未被使用
        # （本函数末尾用的是模块级的 asyncio.get_running_loop()）。

        def _do_rollback():
            try:
                with get_db_with_retry() as conn:
                    conn.execute("BEGIN")
                    try:
                        # BUG-FIX 2026-09-15（高）：原 SQL 里写了
                        # `error_message = COALESCE(error_message, '任务异常中断，已自动回滚')`，
                        # 但 `sections` 表**根本没有 error_message 列**（见 db.py 的
                        # sections 建表语句）。于是这条 UPDATE 必然抛
                        # no such column: error_message，被外层 except Exception
                        # 吞成一条 warning 日志，整个事务 ROLLBACK ——
                        # 后果：sections 的 generating 残留、section_generation_progress
                        # 的 processing 残留**从来没被清理过**，断点续传会一直把
                        # 这些章节当成「正在生成」而跳过。
                        # 修法：去掉对不存在列的写入。章节级的错误信息本身由
                        # section_generation_progress.last_error 承载，sections 无需冗余一份。
                        cursor1 = conn.execute(
                            """UPDATE sections
                               SET status = 'failed',
                                   updated_at = datetime('now', 'localtime')
                               WHERE project_id = ? AND status = 'generating'""",
                            (project_id,),
                        )
                        sections_affected = cursor1.rowcount

# 清理 generation_progress 表中的「生成中」残留。
                        # BUG-FIX 2026-10-08（P0）：见 get_project_progress 同名修复的说明 ——
                        # 该表 status 由 SSE 章节状态串写入（'generating'），不是 'processing'，
                        # 原 WHERE 恒匹配 0 行，回滚形同虚设。同时兼容旧字面量。
                        if generation_id:
                            cursor2 = conn.execute(
                                """UPDATE section_generation_progress
                                   SET status = 'failed',
                                       last_error = '任务异常中断，已自动回滚'
                                   WHERE generation_id = ?
                                     AND status IN ('generating', 'processing')""",
                                (generation_id,),
                            )
                        else:
                            cursor2 = conn.execute(
                                """UPDATE section_generation_progress
                                   SET status = 'failed',
                                       last_error = '任务异常中断，已自动回滚'
                                   WHERE generation_id IN (
                                       SELECT generation_id FROM generation_tasks
                                       WHERE project_id = ? AND status IN ('running', 'paused')
                                   ) AND status IN ('generating', 'processing')""",
                                (project_id,),
                            )
                        progress_affected = cursor2.rowcount

                        conn.execute("COMMIT")

                        if sections_affected > 0 or progress_affected > 0:
                            logger.info(
                                "[generation_manager] BUG-C-02 回滚脏状态: project=%d, "
                                "sections=%d, progress=%d",
                                project_id, sections_affected, progress_affected,
                            )
                    except Exception:
                        conn.execute("ROLLBACK")
                        raise
            except Exception as e:
                logger.warning("[generation_manager] BUG-C-02 回滚脏状态失败: %s", e)

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, _do_rollback)


# 全局实例
generation_manager = GenerationManager()
