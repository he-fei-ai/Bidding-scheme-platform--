"""Celery 异步任务配置

提供后台异步任务处理能力，用于：
1. AI 内容生成（章节正文、大纲等）
2. 文件解析和文本提取
3. 文档导出
4. 知识库处理
5. 废标检查和标书查重

设计说明（BUG-FIX 2026-09-18）：
    celery 在本项目里是**可选**依赖（requirements.txt: `celery[redis]>=5.3.0`），
    开发环境与多数单机部署并不安装。旧实现在模块导入期就 `from celery import Celery`，
    缺失时 `app.tasks` 整个包 ImportError，6 个已注册任务**连函数对象都拿不到**，
    既无法被 Celery 分发、也无法被任何代码直接调用 —— 典型的「空任务运行」。

    现在改为两段式：

    - celery 可用   → 行为不变，任务正常注册到 broker；
    - celery 不可用 → 用 `_InlineTaskApp` 兜底，`@celery_app.task(...)` 退化为
      「登记进 TASK_CATALOG + 原样返回函数」。任务因此保持为普通函数，
      可通过 `dispatch_task()` / `TASK_CATALOG` 在进程内直接执行。
"""

import asyncio
from datetime import timedelta
import importlib
import logging
import os
from typing import Any, Callable


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 可选依赖：celery 未安装时降级为「进程内直接调用」
# ---------------------------------------------------------------------------
try:  # pragma: no cover - 取决于部署环境是否安装 celery
    from celery import Celery, signals

    CELERY_AVAILABLE = True
except ImportError:  # pragma: no cover
    Celery = None  # type: ignore[assignment]
    CELERY_AVAILABLE = False

    class _Signal:
        """`signals.worker_ready.connect` 这类用法的空壳。

        既要支持 `@signals.worker_ready.connect`（无参调用返回装饰器），
        也要支持 `signals.worker_ready.connect(fn)`（直接传函数）。
        """

        def connect(self, fn: Callable[..., Any] | None = None, **_kwargs: Any):
            if fn is None:

                def _deco(func: Callable[..., Any]) -> Callable[..., Any]:
                    return func

                return _deco
            return fn

    class _Signals:
        worker_ready = _Signal()
        worker_shutdown = _Signal()

    signals = _Signals()  # type: ignore[assignment]


# 任务名 -> (任务函数, 是否 bind=True)
TASK_CATALOG: dict[str, tuple[Callable[..., Any], bool]] = {}

# 不允许在 Web 进程内同步兜底执行的任务。
# 这类任务一次运行可达 1 小时，若在没有 broker 时被「就地执行」，会长时间占住
# 请求线程/事件循环。dispatch_task 对它们的行为是：返回显式的未派发结果 + ERROR
# 日志，而不是静默什么都不做，也不是把整个进程拖死。
INLINE_BLOCKED_TASKS: frozenset[str] = frozenset({"tasks.content.generate_sections"})


class _Conf:
    """`.conf.update(...)` 的空实现（兜底模式下配置项无处落地）。"""

    def update(self, *_args: Any, **_kwargs: Any) -> None:
        return None


class _InlineTaskApp:
    """celery 缺失时的最小替身。

    只保留 `app/tasks/__init__.py` 与 `cleanup_old_results` 真正用到的接口：
    `.conf.update()` / `.task()` / `.autodiscover_tasks()`。
    """

    def __init__(self) -> None:
        self.conf = _Conf()

    def task(self, name: str | None = None, bind: bool = False, **_options: Any):
        def _deco(func: Callable[..., Any]) -> Callable[..., Any]:
            key = name or f"{func.__module__}.{func.__name__}"
            TASK_CATALOG[key] = (func, bool(bind))
            logger.debug("[celery-fallback] 登记任务 %s -> %s", key, func.__name__)
            return func

        return _deco

    def autodiscover_tasks(self, packages: Any = None, **_kwargs: Any) -> None:
        """兜底模式下没有 broker 可发现任务，改为直接 import 以填充 TASK_CATALOG。

        不 import 的话，`@celery_app.task` 从未执行 → TASK_CATALOG 为空 →
        `dispatch_task()` 永远报「未登记的任务名」，兜底形同虚设。
        """
        for pkg in list(packages or []):
            try:
                importlib.import_module(str(pkg))
            except Exception as exc:  # pragma: no cover - 兜底路径容错
                logger.warning("[celery-fallback] 自动发现 %s 失败: %s", pkg, exc)

    def send_task(self, *_args: Any, **_kwargs: Any):
        raise RuntimeError("celery 未安装，无法 send_task；请改用 dispatch_task(..., force_inline=True)")


def make_celery(app_name: str = "bid_assistant"):
    """创建 Celery 实例

    Args:
        app_name: 应用名称

    Returns:
        Celery 实例；celery 未安装时返回 `_InlineTaskApp` 兜底实例。
    """
    if not CELERY_AVAILABLE:
        logger.warning(
            "celery 未安装，异步任务将降级为进程内直调（TASK_CATALOG 仍可用）。"
            "如需真正的后台队列，请安装 celery[redis] 并启动 worker。"
        )
        return _InlineTaskApp()

    celery_app = Celery(app_name)

    # ENHANCE 2026-09-18：包装 `.task`，让**真实 Celery 模式下也填充 TASK_CATALOG**。
    # 否则 TASK_CATALOG 只有降级模式才有内容，`dispatch_task()` / `list_registered_tasks()`
    # 会随部署环境不同而行为不一致（装了 celery 反而查不到任务）。
    _orig_task = celery_app.task

    def _task(name: str | None = None, bind: bool = False, **options: Any):
        def _deco(func: Callable[..., Any]):
            TASK_CATALOG[name or f"{func.__module__}.{func.__name__}"] = (func, bool(bind))
            return _orig_task(name=name, bind=bind, **options)(func)

        return _deco

    celery_app.task = _task  # type: ignore[method-assign]

    # 配置文件来源
    celery_config = os.environ.get(
        "CELERY_CONFIG",
        "redis://localhost:6379/1",
    )

    celery_app.conf.update(
        # Broker URL (消息队列)
        broker_url=celery_config,
        # Result backend
        result_backend=celery_config.replace("/1", "/2"),
        # BUG-FIX 2026-10-16：socket 级连接超时（防 send_task 底层连接因网络
        # 黑洞长时间挂起；localhost 拒连本身很快，但跨机/防火墙场景必须给上限）。
        broker_transport_options={"socket_connect_timeout": 3, "socket_timeout": 5},
        # 序列化配置
        task_serializer="json",
        result_serializer="json",
        accept_content=["json"],
        # 时区
        timezone="Asia/Shanghai",
        enable_utc=True,
        # 任务配置
        task_track_started=True,
        task_time_limit=3600,  # 1小时硬超时
        task_soft_time_limit=3000,  # 50分钟软超时
        task_acks_late=True,  # 任务执行完再确认
        # 并发配置
        worker_concurrency=4,
        worker_max_tasks_per_child=100,  # 每个worker进程最多处理100个任务后重启
        # 定时任务配置
        beat_schedule={
            "cleanup-old-tasks": {
                "task": "app.celery_app.cleanup_old_results",
                "schedule": timedelta(hours=1),
            },
            # ENHANCE 2026-09-18：废标报告定时调度的执行器。
            # `rejection_report_schedules` 表只有 CRUD，此前没有任何代码读它并
            # 生成报告（配好的「每日报告」永远不产出）。这里每小时轮询一次到点
            # 的调度。也可用于进程内直调：
            # dispatch_task("app.celery_app.run_scheduled_reports", force_inline=True)
            "run-scheduled-reports": {
                "task": "app.celery_app.run_scheduled_reports",
                "schedule": timedelta(hours=1),
            },
            # ENHANCE 2026-10-11: repair_mermaid 步骤统计定时汇总
            # 每小时打印一次累计的 {chart_type: {step_name: count}} 并 reset，
            # 供运维洞察「哪些 chart_type 高频触发哪些修复步骤」。
            # 无数据时静默跳过（不会产生空日志）。
            "flush-repair-stats": {
                "task": "app.celery_app.flush_repair_stats_task",
                "schedule": timedelta(hours=1),
            },
            # ENHANCE 2026-10-11: repair_stats_snapshots 清理（每周一次，保留最近 720 行 = 30 天）
            "cleanup-repair-stats-snapshots": {
                "task": "app.celery_app.cleanup_repair_stats_snapshots_task",
                "schedule": timedelta(days=7),
            },
        },
        # 重试配置
        task_retry_backoff_max=600,
        task_retry_backoff=3,
        # 日志配置
        worker_log_format="[%(asctime)s: %(levelname)s/%(processName)s] %(message)s",
        worker_task_log_format="[%(asctime)s: %(levelname)s/%(task_name)s:%(task_id)s] %(message)s",
    )

    return celery_app


# 全局 Celery 实例
celery_app = make_celery()

# 延迟自动发现任务模块，避免循环导入
# celery_app.autodiscover_tasks 在 celery_app 全局变量赋值后调用
celery_app.autodiscover_tasks(
    ["app.tasks"],
    force=True,
)


@signals.worker_ready.connect
def on_worker_ready(sender, **kwargs):
    """Worker 启动时的回调"""
    logger.info("Celery Worker 启动成功: %s", sender.hostname)


@signals.worker_shutdown.connect
def on_worker_shutdown(sender, **kwargs):
    """Worker 关闭时的回调"""
    logger.info("Celery Worker 正在关闭...")


@celery_app.task(name="app.celery_app.cleanup_old_results")
def cleanup_old_results() -> dict[str, Any]:
    """清理过期的任务状态（每小时由 beat 触发一次）

    BUG-FIX 2026-09-18（高）：原实现**什么都不做** —— 只打了两条日志就
    `return {"status": "cleaned"}`，注释还把责任推给「Celery 自动清理超过
    result_expires 时间的结果」。两处都不成立：

      1. 本模块从未设置 `result_expires`（conf 里根本没有这一项）；
      2. 即便设了，它只作用于 **result backend 里的返回值**，碰不到
         `task_locks` / `background_tasks` / `generation_tasks` 三张业务表。

    后果：beat 每小时空转一次并谎报 cleaned，而心跳超时的僵尸锁、中断快照、
    过期生成任务只增不减 —— 僵尸锁会把同一项目的后续任务永久拦在门外
    （`task_lock.acquire` 撞上未回收的 active 行即报冲突）。

    现在真实执行五步清理，并返回各步的实际条数（失败的步骤单独记录，
    不因一步出错中断其余清理）。

    Returns:
        {"status": "ok"|"failed", "locks_reclaimed": int, "snapshots_interrupted": int,
         "generation_tasks_deleted": int, "parse_tasks_pruned": int,
         "parse_state_reclaimed": int, "errors": [str, ...]}
    """
    result: dict[str, Any] = {
        "status": "ok",
        "locks_reclaimed": 0,
        "snapshots_interrupted": 0,
        "generation_tasks_deleted": 0,
        "parse_tasks_pruned": 0,
        "parse_state_reclaimed": 0,
        "errors": [],
    }
    logger.info("开始清理过期的任务状态...")

    # 1) 回收心跳超时、未主动释放的任务组锁
    try:
        from .services import task_lock as _task_lock

        result["locks_reclaimed"] = int(_task_lock.reclaim_stale() or 0)
    except Exception as exc:  # pragma: no cover - 清理步骤容错
        logger.warning("回收超时任务锁失败: %s", exc, exc_info=True)
        result["errors"].append(f"task_lock: {exc}")

    # 2) 把心跳超时的「运行中」快照收敛为 interrupted（横幅可见）
    try:
        from .services import task_snapshot as _task_snapshot

        result["snapshots_interrupted"] = len(_task_snapshot.reconcile_stale() or [])
    except Exception as exc:  # pragma: no cover
        logger.warning("对账僵死任务快照失败: %s", exc, exc_info=True)
        result["errors"].append(f"task_snapshot: {exc}")

    # 3) 清理已结束超过 24 小时的生成任务及其章节进度
    #    cleanup_completed_tasks 是协程（内部 await 兼容旧调用方），Celery 任务
    #    是同步上下文，这里用 asyncio.run 跑；若调用方已在事件循环中则跳过。
    try:
        from .services.generation_manager import generation_manager

        result["generation_tasks_deleted"] = int(
            asyncio.run(generation_manager.cleanup_completed_tasks(max_hours=24)) or 0
        )
    except RuntimeError as exc:  # 已在事件循环中运行（如被测试直接 await 调用）
        logger.warning("清理过期生成任务跳过（已在事件循环中）: %s", exc)
        result["errors"].append(f"generation_manager: {exc}")
    except Exception as exc:  # pragma: no cover
        logger.warning("清理过期生成任务失败: %s", exc, exc_info=True)
        result["errors"].append(f"generation_manager: {exc}")

    # 4) 遗留修复 2026-10-06（L3）：裁剪 file_parse_tasks（每项目保留最新 N 条）。
    #    此前该表只增不减，且不在任何清理任务里。
    try:
        from .services.files import prune_parse_tasks

        result["parse_tasks_pruned"] = int(prune_parse_tasks() or 0)
    except Exception as exc:  # pragma: no cover
        logger.warning("裁剪文件解析任务失败: %s", exc, exc_info=True)
        result["errors"].append(f"file_parse_tasks: {exc}")

    # 5) GAP-FIX 2026-10-17：回收孤儿 processing（uploaded_files + file_parse_tasks）。
    #    图片上传走「broker 派发 → worker 收尾」，worker 未运行 / 消息丢失 / 被 SIGKILL
    #    都会让行永久停在 processing：prune 只淘汰终态行，前端进度条因此永久卡住。
    #    放在 prune 之后：先由 prune 淘汰终态行，再把超时的在途行收敛为 failed，
    #    下一轮 prune 才能将其一并裁掉。
    try:
        from .services.files import reclaim_stale_parse_state

        _reclaimed = reclaim_stale_parse_state() or {}
        result["parse_state_reclaimed"] = int(
            _reclaimed.get("uploaded_files_failed", 0)
            + _reclaimed.get("parse_tasks_failed", 0)
        )
    except Exception as exc:  # pragma: no cover
        logger.warning("回收孤儿解析状态失败: %s", exc, exc_info=True)
        result["errors"].append(f"reclaim_stale_parse_state: {exc}")

    if result["errors"]:
        result["status"] = "failed"
        logger.warning("任务状态清理完成（有步骤失败）: %s", result)
    else:
        logger.info("任务状态清理完成: %s", result)
    return result


@celery_app.task(name="app.celery_app.run_scheduled_reports")
def run_scheduled_reports() -> dict[str, Any]:
    """生成所有到点的废标检查报告（每小时由 beat 触发一次）

    ENHANCE 2026-09-18（高）：`rejection_report_schedules` 表此前**只有 CRUD、
    没有任何执行器** —— 路由层有「创建/启用/禁用/删除/列表」五个端点，用户能看到
    调度处于启用状态，但全库没有任何代码读取 `schedule_type` / `schedule_time`
    并真正生成报告。这是一张只写不读的表，等同于空任务：用户配好了每日报告，
    却永远收不到任何产物，也没有任何报错。

    判定逻辑与回写都放在 `services/report_schedule_runner.py`，这里只做驱动。

    Returns:
        {"status": "ok"|"failed", "scanned": int, "generated": int,
         "skipped": int, "failed": int, "details": [...]}
    """
    from .services.report_schedule_runner import run_due_schedules

    logger.info("开始执行到点的报告调度...")
    result = run_due_schedules()
    logger.info("报告调度执行完成: %s", result)
    return result


class _InlineTaskRequest:
    """bind=True 任务的 `self.request` 替身（进程内直调时无 Celery 上下文）。"""

    retries = 0
    max_retries = 0
    id: str | None = None


class _InlineTaskSelf:
    """bind=True 任务的 `self` 替身。"""

    request = _InlineTaskRequest()


@celery_app.task(name="app.celery_app.flush_repair_stats_task")
def flush_repair_stats_task() -> dict[str, Any]:
    """每小时汇总 repair_mermaid 步骤统计（ENHANCE 2026-10-11）

    委托给 image_engine.flush_repair_stats() 做实际的快照 + reset。
    无数据时 flush_repair_stats 内部静默（不产生空日志）。
    """
    try:
        from .services.ai.image_engine import flush_repair_stats as _flush
        return _flush()
    except Exception as e:
        import logging as _logging
        _logging.getLogger(__name__).warning("flush_repair_stats_task 异常: %s", e)
        return {"total": 0, "error": str(e)}


@celery_app.task(name="app.celery_app.cleanup_repair_stats_snapshots_task")
def cleanup_repair_stats_snapshots_task() -> dict[str, Any]:
    """每周清理 repair_stats_snapshots 表（保留最近 720 行 = 30 天）"""
    try:
        from .services.ai.image_engine import cleanup_repair_stats_snapshots as _cleanup
        _deleted = _cleanup()
        return {"deleted": _deleted, "kept": 720}
    except Exception as e:
        import logging as _logging
        _logging.getLogger(__name__).warning("cleanup_repair_stats_snapshots_task 异常: %s", e)
        return {"deleted": 0, "error": str(e)}


def list_registered_tasks() -> list[str]:
    """返回已在本进程登记的任务名（按名称排序）。

    用于启动自检：与 `INLINE_BLOCKED_TASKS` 一起可以快速看出「哪些任务登记了
    但无人分发」。
    """
    return sorted(TASK_CATALOG)


def dispatch_task(
    name: str,
    args: tuple[Any, ...] | None = None,
    kwargs: dict[str, Any] | None = None,
    *,
    force_inline: bool = False,
    allow_inline: bool = True,
) -> dict[str, Any]:
    """按任务名派发任务，broker 不可用时降级为进程内直调。

    BUG-FIX 2026-09-18（中）：全库此前**没有任何** `.delay()` / `apply_async()`
    / `send_task()` 调用点 —— 6 个任务全部「已注册但永不执行」。调用方（未来的
    异步入口）无处下手，只能自己去 import 内部函数，于是 Celery 层形同虚设。
    这里提供唯一正式入口：

      - celery 可用且 broker 可达 → `send_task` 入队，`dispatched_via="celery"`；
      - broker 不可达（连接失败/超时）→ 退化为进程内直调，`dispatched_via="inline"`；
      - 任务在 `INLINE_BLOCKED_TASKS` 中且无法入队 → **不静默丢弃**，返回
        `status="not_dispatched"` 并打 ERROR 日志。

    Args:
        name: 任务名，必须是 `TASK_CATALOG` 中已登记的名字
        args: 位置参数
        kwargs: 关键字参数
        force_inline: 跳过 Celery，直接在本进程同步执行（便于测试与单机部署）
        allow_inline: 是否允许 broker 不可达时就地执行

    Returns:
        统一结构的结果字典，含 `status` / `task_name` / `dispatched_via` 字段。
        就地执行成功时 `status` 取任务函数自身的返回值里的 status，
        并把原始返回值放在 `result` 中。
    """
    args = tuple(args or ())
    kwargs = dict(kwargs or {})

    entry = TASK_CATALOG.get(name)
    if entry is None:
        # 兜底模式下 autodiscover 可能因导入失败没跑成，这里补一次懒加载
        try:
            importlib.import_module("app.tasks")
        except Exception as exc:  # pragma: no cover
            logger.debug("懒加载 app.tasks 失败: %s", exc)
        entry = TASK_CATALOG.get(name)
    if entry is None:
        logger.error("未登记的任务名: %s（已登记: %s）", name, list_registered_tasks())
        return {"status": "not_dispatched", "task_name": name, "error": f"未登记的任务名: {name}"}

    func, bind = entry

    if CELERY_AVAILABLE and not force_inline:
        try:
            # BUG-FIX 2026-10-16（Web 进程 send_task 挂死·测试基线中
            # test_async_reparse_dispatches 无限挂起即此因）：celery 已安装但
            # broker（Redis）未运行时，`send_task` 内部 kombu 的 ensure_connection
            # 处于无限重试循环（默认 broker_connection_retry=True、
            # max_retries=None），把「立即返回」的请求线程永久卡死。
            # 现先做一次禁用重试的连接预检（max_retries=0，连接建立超时由
            # broker_transport_options.socket_connect_timeout 兜底），broker
            # 不可达立即抛 → 落到 except 的进程内直调兜底，请求不再挂死。
            with celery_app.connection_for_write() as _conn:
                _conn.ensure_connection(max_retries=0)
            async_result = celery_app.send_task(name, args=args, kwargs=kwargs)
            return {
                "status": "queued",
                "task_name": name,
                "dispatched_via": "celery",
                "celery_task_id": getattr(async_result, "id", None),
            }
        except Exception as exc:  # broker 不可达 / 序列化失败
            logger.warning("任务 %s 入队失败（%s），尝试进程内直调", name, exc)

    if not allow_inline:
        logger.error("任务 %s 未派发：broker 不可用且 allow_inline=False", name)
        return {
            "status": "not_dispatched",
            "task_name": name,
            "dispatched_via": "none",
            "error": "broker 不可用，且不允许就地执行",
        }

    if name in INLINE_BLOCKED_TASKS:
        logger.error(
            "任务 %s 未派发：该任务耗时过长，禁止在 Web 进程内就地执行。"
            "请安装 celery[redis] 并启动 worker 后再调用。",
            name,
        )
        return {
            "status": "not_dispatched",
            "task_name": name,
            "dispatched_via": "none",
            "error": "任务禁止就地执行（INLINE_BLOCKED_TASKS）",
        }

    logger.info("任务 %s 就地执行（无 broker 兜底）", name)
    try:
        raw = func(_InlineTaskSelf(), *args, **kwargs) if bind else func(*args, **kwargs)
    except Exception as exc:
        logger.error("任务 %s 就地执行失败: %s", name, exc, exc_info=True)
        return {"status": "failed", "task_name": name, "dispatched_via": "inline", "error": str(exc)}

    status = raw.get("status", "completed") if isinstance(raw, dict) else "completed"
    return {
        "status": status,
        "task_name": name,
        "dispatched_via": "inline",
        "result": raw,
    }


# 通用任务装饰器
def task_with_retry(max_retries: int = 3, delay: int = 1):
    """带重试机制的任务装饰器

    Args:
        max_retries: 最大重试次数
        delay: 首次重试的等待秒数（随后按指数退避，上限 600 秒）

    注（BUG-FIX 2026-09-18）：
      1. `delay` 此前是**死参数** —— 装饰器收下后从未使用，实际退避起始间隔
         被 `retry_backoff=True` 硬编码成 1 秒。现在作为首次重试间隔生效。
      2. 原实现 `base=func.__class__ if hasattr(func, '__self__') else None`
         对**绑定方法**会取到 `method` 类型当 base，而 Celery 期望 base 是
         `celery.Task` 子类，注册时会抛 TypeError；普通函数走 else 分支拿
         None，反而侥幸正常。这里一律传 None。
    """

    def decorator(func):
        return celery_app.task(
            base=None,
            name=f"{func.__module__}.{func.__name__}",
            retry_backoff=max(1, int(delay or 1)),
            retry_backoff_max=600,
            max_retries=max_retries,
        )(func)

    return decorator


if __name__ == "__main__":
    # 直接运行时启动 worker
    import sys

    command = sys.argv[1] if len(sys.argv) > 1 else "worker"

    if command == "worker":
        celery_app.worker_main(
            [
                "celery",
                "-A",
                "app.celery_app:celery_app",
                "worker",
                "--loglevel=info",
                "--concurrency=4",
                "--pool=solo",  # Windows 使用 solo 模式
            ]
        )
    elif command == "beat":
        celery_app.beat_main(
            [
                "celery",
                "-A",
                "app.celery_app:celery_app",
                "beat",
                "--loglevel=info",
            ]
        )
    elif command == "flower":
        celery_app.flower_main(
            [
                "celery",
                "-A",
                "app.celery_app:celery_app",
                "flower",
                "--port=5555",
            ]
        )
    else:
        print(f"未知命令: {command}")
        print("可用命令: worker, beat, flower")
        sys.exit(1)
