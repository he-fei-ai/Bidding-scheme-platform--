"""workflows 子模块：workflows_base（由 scripts/split_workflows.py 自动生成，勿手改）"""

import asyncio
import datetime
import json
import logging
import re
import threading
import time
from typing import Any

from ...config import CONCUR_INITIAL, CONCUR_MAX, CONCUR_MIN, GLOBAL_AI_CONCURRENCY_MAX
from ...utils.json_parser import extract_json_from_text
from .heading_standard import HeadingNumberingGenerator, build_heading_spec_prompt
from .prompts import get_prompt
from .prompts._registry import _resolve_shared
from .providers.base import AIMessage


logger = logging.getLogger(__name__)


def _safe_asyncio_run(coro):
    """
    安全地运行异步协程，兼容以下三种调用场景：

    场景 A：普通线程（无 running loop）
      → 直接 asyncio.run() 执行
      适用：Celery worker 默认线程池、独立脚本

    场景 B：有 running loop 的线程（如 FastAPI/ASGI 请求处理线程）
      → 自动创建新线程执行 asyncio.run()，避免 RuntimeError
      适用：FastAPI 中间件同步调用 Celery task、同步 HTTP handler 内联调用

    场景 C：已在 asyncio.run() 上下文中调用
      → asyncio.run() 会自动拒绝并抛出 RuntimeError，
        此时调用方应改用 await 而非同步调用

    修复 BUG（Celery 嵌套 asyncio.run 事件循环冲突）：
      - 旧代码在 outline_tasks.py 中直接用 asyncio.run()，
        在场景 B 下会抛出：
          RuntimeError: asyncio.run() cannot be called from a running event loop
      - 新代码检测 running loop 存在性，自动降级到新线程
    """
    try:
        loop = asyncio.get_running_loop()  # noqa: F841
    except RuntimeError:
        # 没有 running loop（场景 A）
        return asyncio.run(coro)

    # 有 running loop（场景 B）：在新线程中执行 asyncio.run()
    result = None
    exception = None

    def _run_in_thread():
        nonlocal result, exception
        try:
            result = asyncio.run(coro)
        except Exception as e:
            exception = e

    thread = threading.Thread(target=_run_in_thread)
    thread.start()
    thread.join()

    if exception is not None:
        raise exception
    return result


async def with_timeout(coro, timeout: int = 30, error_msg: str = "AI调用超时"):
    """带超时的AI调用装饰器"""
    logger = logging.getLogger(__name__)
    start = time.monotonic()
    try:
        result = await asyncio.wait_for(coro, timeout=timeout)
        elapsed = time.monotonic() - start
        logger.info(
            "[with_timeout][%s] 完成，耗时 %.2fs / 限制 %ds",
            datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3],
            elapsed,
            timeout,
        )
        return result
    except TimeoutError:
        elapsed = time.monotonic() - start
        logger.warning(
            "[with_timeout][%s] 超时（%.2fs / %ds），任务被中止",
            datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3],
            elapsed,
            timeout,
        )
        raise TimeoutError(f"{error_msg}（{timeout}秒）")


class EmptyAIResponseError(Exception):
    """AI 返回空响应（鉴权失败 / 模型无输出 / 上下文超限截断）时抛出。

    F-P0-2（2026-10-07）修复：原先 with_timeout 后直接 `resp or ""`，
    空响应被静默落库为「已完成」正文，导致导出文档只剩大纲骨架
    （见日志 [AI摘要] 生成完成，响应长度: 0 字符）。现对空响应显式告警，
    重试后仍空则抛出本异常，由上游把章节/任务标记为 failed，而非写入空内容。
    """

    def __init__(self, message: str = "AI 返回空响应", context: str = ""):
        self.context = context
        super().__init__(f"{message} {context}".strip())


async def call_ai_text(
    provider: Any,
    messages: list,
    *,
    timeout: int = 60,
    max_tokens=None,
    require_nonempty: bool = True,
    retries: int = 1,
    context: str = "",
) -> str:
    """统一的 AI 文本调用封装：超时 + 空响应拦截 + 有限重试（F-P0-2）。

    Args:
        provider: 实现了 `chat(messages, max_tokens=...)` 的 AI 提供商
        messages: 对话消息列表（AIMessage 序列）
        timeout: 单次调用超时（秒）
        max_tokens: 透传给 provider.chat 的 max_tokens（None 表示不传）
        require_nonempty: 为 True 时，空/纯空白响应视为失败并触发重试/异常
        retries: 空响应或超时后的额外重试次数（默认 1）
        context: 调用上下文描述（如 "[AI摘要]"），用于日志与异常定位（含 ID 时便于串联）

    Returns:
        去首尾空白后的响应文本

    Raises:
        TimeoutError: 重试耗尽仍超时
        EmptyAIResponseError: retries 耗尽仍为空且 require_nonempty=True
    """
    last_err: Exception | None = None
    total = max(retries, 0) + 1
    for attempt in range(total):
        try:
            if max_tokens is not None:
                raw = await with_timeout(
                    provider.chat(messages, max_tokens=max_tokens), timeout=timeout
                )
            else:
                raw = await with_timeout(provider.chat(messages), timeout=timeout)
        except TimeoutError as e:
            last_err = e
            logger.warning(
                "[call_ai_text] 超时(attempt %d/%d) %s: %s",
                attempt + 1, total, context, e,
            )
            continue

        text = (raw or "").strip()
        if require_nonempty and not text:
            # 关键告警：与日志链路 ID（request/project/section/task）自动关联
            logger.error(
                "[call_ai_text] AI 返回空响应(attempt %d/%d) %s —— 请检查 AGNES_API_KEY 是否配置、"
                "模型是否可用、上下文是否超 tokens 上限被截断",
                attempt + 1, total, context,
            )
            last_err = EmptyAIResponseError("AI 返回空响应", context)
            continue
        return text

    if last_err is not None:
        raise last_err
    return ""


def _get_chapter_templates(chapter_title: str) -> dict[str, list[str] | dict[str, list[str]]]:
    """根据一级章节标题获取对应的二三级目录模板。

    返回结构：{"level2": [...], "level3": {"l2_title": [...], ...}}
    优先精确匹配，其次模糊匹配关键词，最后使用通用模板。
    """
    from .workflows_outline_utils import CHAPTER_TITLE_TEMPLATES

    # 1. 精确匹配
    if chapter_title in CHAPTER_TITLE_TEMPLATES:
        return CHAPTER_TITLE_TEMPLATES[chapter_title]

    # 2. 模糊匹配（包含关键词）
    for key, templates in CHAPTER_TITLE_TEMPLATES.items():
        if key in chapter_title or chapter_title in key:
            return templates

    # 3. 关键词匹配
    keywords_map = {
        "施工": ("施工方案及技术措施",),
        "安全": ("施工安全措施计划",),
        "质量": ("质量保证措施和创优计划",),
        "进度": ("施工总进度计划及保证措施",),
        "环保": ("施工环保措施计划",),
        "文明": ("文明施工措施计划",),
        "资源": ("资源配备计划",),
        "设备": ("拟投入本工程的主要施工设备表",),
        "材料": ("主要材料及劳动力计划表",),
        "分包": ("对专业分包工程的配合、协调、管理、服务方案",),
        "保修": ("成品保护和工程保修工作的管理措施和承诺",),
        "应急": ("任何可能的紧急情况的处理措施、预案以及抵抗风险的措施",),
        "配合": ("与发包人、项目管理单位、监理及设计人的配合",),
        "概述": ("项目概述",),
        "概况": ("工程概况",),
        "依据": ("编制依据",),
        "平面": ("施工现场总平面布置",),
        "工期": ("工期优化",),
        "资料": ("工程资料及竣工验收管理",),
        "验收": ("工程资料及竣工验收管理",),
        "附图": ("附图、附件",),
        "附件": ("附图、附件",),
    }
    for kw, (key,) in keywords_map.items():
        if kw in chapter_title:
            return CHAPTER_TITLE_TEMPLATES.get(key, CHAPTER_TITLE_TEMPLATES["其他"])

    # 4. 通用模板（每个二级目录有独立的三级模板）
    return {
        "level2": ["总体方案", "专项措施", "管理制度", "保障机制", "检查验收"],
        "level3": {
            "总体方案": [
                "总体施工思路",
                "施工区段划分",
                "施工顺序安排",
                "关键线路确定",
                "施工阶段划分",
            ],
            "专项措施": [
                "专项技术方案",
                "特殊工艺措施",
                "质量控制措施",
                "安全保障措施",
                "环保措施方案",
            ],
            "管理制度": [
                "管理制度体系",
                "岗位职责划分",
                "工作流程制定",
                "考核奖惩办法",
                "培训教育制度",
            ],
            "保障机制": [
                "组织保障措施",
                "技术保障措施",
                "资源保障措施",
                "资金保障措施",
                "应急保障措施",
            ],
            "检查验收": [
                "日常检查制度",
                "专项检查方案",
                "验收标准制定",
                "问题整改闭环",
                "验收资料归档",
            ],
        },
    }


def _fix_level4_heading_numbering(content: str) -> str:
    """修正内容中的标题编号（V2.1 八级统一标准）

    委托给 HeadingNumberingGenerator 集中处理：
      - 删除已有编号前缀
      - 按相对层级重新编号（与设计文档 "DOCX 导出时的标题样式映射" 一致）
      - 保证 L1-L4 用阿拉伯数字、L5-L8 一致
      - 不破坏正文段落、表格、图表标记

    保留此函数名以保持调用兼容。
    """
    if not content:
        return content

    return HeadingNumberingGenerator.normalize_content(content)


def _log_json(event_type: str, event_id: str, data: dict) -> None:
    """
    结构化 JSON 日志，用于 ELK 日志系统监控。

    Args:
        event_type: 事件类型（如 "outline_phase"、"performance"）
        event_id: 事件标识符（如 "v36_analysis_start"、"step3_merge_complete"）
        data: 附加数据字典
    """
    log_data = {
        "event_type": event_type,
        "event_id": event_id,
        "timestamp": datetime.datetime.now(datetime.UTC).isoformat(),
        **data,
    }
    logger.info("LOG_JSON: %s", json.dumps(log_data, ensure_ascii=False))


class _AnalysisCircuitBreaker:
    """
    针对 V3.6 三项分析（BOQ/工界/工法）的熔断器。

    状态机：
      CLOSED   → 正常执行，累计失败超过阈值后打开
      OPEN     → 短路，直接跳过并记录告警，等冷却期后进入 HALF_OPEN
      HALF_OPEN → 放行一次探测请求，成功则恢复 CLOSED，失败则重新 OPEN

    熔断条件：同一分析类型连续失败次数达到 threshold 时触发。
    冷却时间：熔断后等待 cooldown_seconds 秒进入 HALF_OPEN 状态。
    """

    def __init__(self, threshold: int = 2, cooldown_seconds: float = 5.0):
        self.threshold = threshold
        self.cooldown_seconds = cooldown_seconds
        self._consecutive_failures: dict[str, int] = {}
        self._last_failure_time: dict[str, float] = {}
        self._state: dict[str, str] = {}  # "closed" | "open" | "half_open"
        self._lock = asyncio.Lock()

    def _get_state(self, task: str) -> str:
        return self._state.get(task, "closed")

    def is_open(self, task: str) -> bool:
        """检查熔断器是否处于打开状态（应跳过该任务）"""
        state = self._get_state(task)
        if state != "open":
            return False
        elapsed = time.monotonic() - self._last_failure_time.get(task, 0)
        if elapsed >= self.cooldown_seconds:
            # 冷却完毕，进入 half_open 尝试恢复
            self._state[task] = "half_open"
            logger.warning("[熔断器] 任务[%s] 冷却期满，进入 HALF_OPEN 探测状态", task)
            return False
        return True

    async def record_success(self, task: str):
        """记录一次成功调用，重置失败计数，恢复 CLOSED"""
        async with self._lock:
            if self._get_state(task) == "half_open":
                logger.info("[熔断器] 任务[%s] 探测成功，恢复 CLOSED 状态", task)
            self._consecutive_failures[task] = 0
            self._state[task] = "closed"

    async def record_failure(self, task: str, error: str = ""):
        """记录一次失败调用，达到阈值后打开熔断器"""
        async with self._lock:
            count = self._consecutive_failures.get(task, 0) + 1
            self._consecutive_failures[task] = count
            self._last_failure_time[task] = time.monotonic()
            if self._get_state(task) == "half_open":
                # half_open 状态下再次失败 → 重新打开
                self._state[task] = "open"
                logger.error("[熔断器] 任务[%s] HALF_OPEN 探测失败，重新打开熔断器", task)
            elif count >= self.threshold:
                self._state[task] = "open"
                logger.error(
                    "[熔断器] 任务[%s] 连续失败 %d 次，触发熔断（阈值=%d），冷却 %.1fs 后重试",
                    task,
                    count,
                    self.threshold,
                    self.cooldown_seconds,
                )


class AIRequestMonitor:
    """AI请求监控器：跟踪当前活跃的AI请求数，提供并发限制和指数退避

    BUG-C-01 修复：添加超时机制防止请求永久阻塞
    """

    def __init__(self, max_concurrent: int = 10):
        self.max_concurrent = max_concurrent
        self.current_requests = 0
        self.lock = asyncio.Lock()
        self.cond = asyncio.Condition()

    async def acquire(self, timeout: float | None = None) -> bool:
        """获取请求许可，如果超过并发限制则等待

        Args:
            timeout: 最大等待时间（秒），None表示无限等待

        Returns:
            True表示成功获取，False表示超时
        """
        # BUG修复：原实现用 asyncio.timeout()（仅 Python 3.11+ 可用），
        # 在 Python 3.10 运行环境抛 AttributeError 导致所有 AI 调用失败。
        # 改用 wait_for + deadline 的跨版本兼容实现。
        if timeout is None:
            async with self.cond:
                while self.current_requests >= self.max_concurrent:
                    logger.debug(
                        "AI请求队列满 (%d/%d)，等待中...",
                        self.current_requests,
                        self.max_concurrent,
                    )
                    await self.cond.wait()
                self.current_requests += 1
            return True

        deadline = time.monotonic() + timeout
        async with self.cond:
            while self.current_requests >= self.max_concurrent:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    logger.warning(
                        "AI请求获取超时 (%ds)，当前并发 %d/%d",
                        timeout,
                        self.current_requests,
                        self.max_concurrent,
                    )
                    return False
                try:
                    await asyncio.wait_for(self.cond.wait(), timeout=remaining)
                except TimeoutError:
                    logger.warning(
                        "AI请求获取超时 (%ds)，当前并发 %d/%d",
                        timeout,
                        self.current_requests,
                        self.max_concurrent,
                    )
                    return False
            self.current_requests += 1
            return True

    async def release(self) -> None:
        """释放请求许可"""
        async with self.cond:
            self.current_requests = max(0, self.current_requests - 1)
            # BUG-FIX 2026-09-16（高 丢失唤醒）：原来只用 `notify()` 唤醒**一个**等待者。
            # 若被唤醒的那个恰好在 `asyncio.wait_for` 里超时（通知与超时同帧竞争），
            # 它会带着异常退出而**不**取走这张许可；其余排队者从未被通知，
            # 于是明明有空位却没人醒来 —— 表现为 AI 请求集体挂起直到整体超时。
            # 改成广播：被唤醒者各自重查 while 条件，抢不到位就回去继续等，
            # 不会漏掉任何一个可用许可。
            self.cond.notify_all()

    def get_current_count(self) -> int:
        """获取当前活跃请求数"""
        return self.current_requests


class DynamicSemaphore:
    """支持动态调整并发数的信号量

    与 asyncio.Semaphore 不同，此类允许运行时调整最大并发数，
    配合 AdaptiveConcurrencyController 实现智能并发控制。
    """

    def __init__(self, initial_value: int):
        # BUG-C-01 修复：初始化时钳制全局并发上限
        initial_value = min(initial_value, GLOBAL_AI_CONCURRENCY_MAX)
        self._max_value = initial_value
        self._current_value = initial_value
        self._waiters: list[asyncio.Future] = []
        self._lock = asyncio.Lock()

    async def acquire(self):
        async with self._lock:
            if self._current_value > 0:
                self._current_value -= 1
                return True
        # 需要等待
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._waiters.append(fut)
        try:
            await fut
            return True
        except asyncio.CancelledError:
            # BUG 修复：原取消路径两个分支逻辑颠倒——
            # ① fut 未 done（还在等待队列，从未持有 permit）时调用 release()，
            #   会凭空向池中归还一个并不存在的 permit，导致并发超发（超过 max_value）；
            # ② fut 已 done（release() 已 pop 并 set_result，permit 已转交本协程，
            #   但协程在 await 返回瞬间被取消）时反而不归还，permit 永久泄漏，
            #   后续并发任务逐渐饿死。正确逻辑：未持有仅移出队列；已持有必须归还。
            if fut.done():
                self.release()
            else:
                try:
                    self._waiters.remove(fut)
                except ValueError:
                    pass  # 已被其他路径移除
            raise

    def release(self):
        if self._waiters:
            waiter = self._waiters.pop(0)
            if not waiter.done():
                waiter.set_result(None)
        else:
            self._current_value = min(self._current_value + 1, self._max_value)

    def set_max_value(self, new_max: int):
        """动态调整最大并发数

        BUG-C-01 修复：添加全局并发上限钳制，防止动态调整时突破 GLOBAL_AI_CONCURRENCY_MAX。
        此方法可能被 AdaptiveConcurrencyController 调用，确保全局安全边界不被绕过。
        """
        # BUG-C-01: 钳制全局并发上限
        if new_max > GLOBAL_AI_CONCURRENCY_MAX:
            logger.warning(
                "[DynamicSemaphore] set_max_value(%d) 超过全局上限 %d，自动钳制为 %d",
                new_max, GLOBAL_AI_CONCURRENCY_MAX, GLOBAL_AI_CONCURRENCY_MAX,
            )
            new_max = GLOBAL_AI_CONCURRENCY_MAX

        old_max = self._max_value
        self._max_value = new_max
        # 如果新最大值更大，唤醒等待的协程
        if new_max > old_max:
            diff = new_max - old_max
            for _ in range(diff):
                if self._waiters:
                    waiter = self._waiters.pop(0)
                    if not waiter.done():
                        waiter.set_result(None)
                else:
                    self._current_value += 1
        elif new_max < old_max:
            # BUG 修复：减小时钳制剩余许可，避免超发
            self._current_value = min(self._current_value, new_max)

    @property
    def max_value(self) -> int:
        return self._max_value

    async def __aenter__(self):
        """BUG #9 修复：实现 async context manager 协议

        原实现缺少 __aenter__/__aexit__，导致 `async with sem:` 抛 AttributeError，
        所有章节都会以 `__aenter__` 作为 message 失败，199 个并发任务全挂。
        """
        await self.acquire()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self.release()
        return False


class AdaptiveConcurrencyController:
    """智能并发控制器：根据AI供应商响应时间动态调整并发数

    核心逻辑：
    1. 监控每次AI调用的响应时间
    2. 计算滑动窗口内的平均响应时间
    3. 如果平均响应时间过长，自动降低并发数
    4. 如果平均响应时间较短，适当提高并发数
    5. 防止API限流和超时

    BUG-FIX-R3: 新增 429 快速降级路径——连续 2 次限流错误时直接降为 min_concurrency，
    而不是依赖滑动窗口渐进式下降（3次触发一次调整、失败率>30%才降1）。
    """

    # BUG-FIX-R3: 连续限流阈值
    _RATE_LIMIT_FAST_DOWNGRADE_THRESHOLD = 2

    def __init__(
        self, initial_concurrency: int = 4, min_concurrency: int = 1, max_concurrency: int = 10
    ):
        self.initial_concurrency = initial_concurrency
        self.min_concurrency = min_concurrency
        self.max_concurrency = max_concurrency
        self.current_concurrency = initial_concurrency

        # 响应时间历史记录（最近50次）
        self.response_times: list[float] = []
        self.response_times_lock = asyncio.Lock()

        # 统计信息
        self.total_requests = 0
        self.failed_requests = 0
        self.timeout_count = 0
        self.rate_limit_count = 0          # BUG-FIX-R3: 连续限流计数

        # 调整阈值（秒）
        self.SLOW_THRESHOLD = 30.0  # 超过30秒视为慢响应
        self.FAST_THRESHOLD = 8.0  # 低于8秒视为快响应（原5.0，提升至8.0避免频繁震荡）
        self.WINDOW_SIZE = 20  # 滑动窗口大小

    async def record_response_time(
        self, duration: float, success: bool = True, is_rate_limit: bool = False
    ):
        """记录一次AI调用的响应时间

        Args:
            duration: 响应时间（秒）
            success: 是否成功
            is_rate_limit: 是否因限流（HTTP 429 / RateLimitError）导致失败
                           BUG-FIX-R3: 连续限流时触发快速降级
        """
        should_check = False
        async with self.response_times_lock:
            # 用元组 (duration, success) 记录
            self.response_times.append((duration, success))
            self.total_requests += 1

            if not success:
                self.failed_requests += 1

            # BUG-FIX-R3: 连续限流计数
            if is_rate_limit:
                self.rate_limit_count += 1
            elif success:
                self.rate_limit_count = 0  # 成功调用重置计数

            # 保持滑动窗口大小 - 修复：始终维护固定大小的窗口
            if len(self.response_times) > self.WINDOW_SIZE:
                self.response_times = self.response_times[-self.WINDOW_SIZE :]

            # BUG-FIX-R3: 连续限流 → 立即快速降级（不等窗口检查）
            if self.rate_limit_count >= self._RATE_LIMIT_FAST_DOWNGRADE_THRESHOLD:
                old_concurrency = self.current_concurrency
                if old_concurrency > self.min_concurrency:
                    self.current_concurrency = self.min_concurrency
                    logger.warning(
                        "[智能并发] BUG-FIX-R3: 连续 %d 次限流错误，快速降级 %d → %d",
                        self.rate_limit_count,
                        old_concurrency,
                        self.min_concurrency,
                    )
                self.rate_limit_count = 0  # 降级后重置，等后续成功再爬升

            # 每隔一定次数检查是否需要调整并发数
            # V5.0 优化：从 5 次缩短到 3 次，更快响应性能变化
            if len(self.response_times) % 3 == 0:
                should_check = True

        # V2.1 优化：调整决策放锁外执行，避免 logger 持有锁期间阻塞事件循环
        if should_check:
            await self._do_adjust_concurrency()

    async def _do_adjust_concurrency(self):
        """根据响应时间调整并发数

        V2.1 优化：此方法在 response_times_lock 释放后调用，
        只读取快照数据，不持有任何锁，logger 和状态变更均不阻塞事件循环。
        """
        if len(self.response_times) < 5:
            return

        # 读取数据快照（不持锁，避免竞争）
        recent_entries = list(self.response_times[-self.WINDOW_SIZE :])
        current_concurrency = self.current_concurrency

        # 计算指标
        recent_times = [e[0] for e in recent_entries]
        avg_time = sum(recent_times) / len(recent_times)
        max_time = max(recent_times)
        failed_in_window = sum(1 for _, success in recent_entries if not success)
        failure_rate = failed_in_window / len(recent_entries)

        logger.info(
            "[智能并发] 近期平均响应时间: %.2fs, 最大: %.2fs, 失败率: %.1f%%, 当前并发: %d",
            avg_time,
            max_time,
            failure_rate * 100,
            current_concurrency,
        )

        # 调整策略
        if avg_time > self.SLOW_THRESHOLD or failure_rate > 0.3:
            new_concurrency = max(self.min_concurrency, current_concurrency - 1)
            if new_concurrency < current_concurrency:
                self.current_concurrency = new_concurrency
                logger.warning("[智能并发] 检测到性能下降，降低并发至 %d", new_concurrency)

        elif avg_time < self.FAST_THRESHOLD and failure_rate < 0.05:
            new_concurrency = min(self.max_concurrency, current_concurrency + 2)
            if new_concurrency > current_concurrency:
                self.current_concurrency = new_concurrency
                logger.info("[智能并发] 检测到性能良好，提升并发至 %d", new_concurrency)

    def get_current_concurrency(self) -> int:
        """获取当前推荐的并发数"""
        return self.current_concurrency

    def get_stats(self) -> dict:
        """获取统计信息"""
        # 修复：从元组中提取 duration 计算平均响应时间
        times = [e[0] for e in self.response_times] if self.response_times else []
        return {
            "current_concurrency": self.current_concurrency,
            "total_requests": self.total_requests,
            "failed_requests": self.failed_requests,
            "avg_response_time": (sum(times) / len(times)) if times else 0,
        }


_adaptive_controller = AdaptiveConcurrencyController(
    initial_concurrency=CONCUR_INITIAL, min_concurrency=CONCUR_MIN, max_concurrency=CONCUR_MAX
)

_global_ai_semaphore: asyncio.Semaphore | None = None
_global_ai_semaphore_lock = threading.Lock()


def _get_global_ai_semaphore() -> asyncio.Semaphore:
    """获取/创建全局 AI 并发信号量（懒初始化，避免在模块导入时阻塞）

    BUG-FIX-05 修复：使用 threading.Lock 保护懒初始化，防止多线程竞态。
    原实现无锁保护，多线程环境下可能创建多个信号量实例，
    导致并发上限失去控制（每个信号量独立计数）。
    """
    global _global_ai_semaphore
    if _global_ai_semaphore is None:
        with _global_ai_semaphore_lock:
            if _global_ai_semaphore is None:
                _global_ai_semaphore = asyncio.Semaphore(GLOBAL_AI_CONCURRENCY_MAX)
    return _global_ai_semaphore


_FILL_TEMPLATE_PATTERN = re.compile(r"__([A-Z_][A-Z0-9_]*)__")

# 模块级单例：标题编号规范
_HEADING_SPEC_PROMPT: str | None = None


def _get_heading_spec() -> str:
    """获取标题编号规范，模块级单例缓存，避免重复生成"""
    global _HEADING_SPEC_PROMPT
    if _HEADING_SPEC_PROMPT is None:
        _HEADING_SPEC_PROMPT = build_heading_spec_prompt()
    return _HEADING_SPEC_PROMPT


def _fill_template(
    template: str,
    mapping: dict[str, str],
    labels: dict[str, str] | None = None,
    *,
    sanitize: bool = False,
    desensitize: bool = False,
) -> str:
    """填充模板：每个占位符首次出现用数据替换，后续出现用标签替换。

    设计定位（UNIFY 2026-10-06 复盘）：本函数是统一渲染器 ``render_prompt``
    之外**因语义差异而保留的必要例外**，并非未治理的技术债。差异点在于
    ``render_prompt`` 对所有同名变量做**同一值**替换，而评审类提示词要求
    「同一占位符首次出现=完整数据、后续出现=简短标签」，以避免大段 JSON
    重复注入、撑爆 token——这是有意的 token 控制设计，无法用 ``render_prompt``
    直接替代（否则需给通用渲染器增加「首现/标签」语义，污染主路径）。

    与 ``render_prompt`` 的**安全能力对齐**（消除此前的口径不一致）：
    - 共享规则：先 ``_resolve_shared`` 消解 ``{shared:NAME}``（与 render_prompt 一致）。
    - 变量值清洗：对每个「数据值」应用 ``sanitize`` / ``desensitize``，与
      ``render_prompt`` 对 **kwargs 变量值** 的处理口径完全一致（opt-in、默认关、
      向后兼容）。注入外部/用户衍生数据（评分点/目录/概述）的调用方可开启
      ``sanitize=True`` 做纵深防御提示词注入。

    解决提示词中同一占位符既用于数据插入又用于文本引用导致的数据重复问题。
    例如 OUTLINE_STEP_USER 中 __SCORING_POINTS__ 出现 4 次：首次为数据占位符，
    其余为引用说明。若全部替换为评分点 JSON，会导致提示词过长且超出 token 限制。

    对于 mapping 中没有数据但 labels 中有标签的占位符（如 SYSTEM 提示词中的
    数据引用占位符），全部替换为标签文字，避免 AI 看到字面量 __XXX__。

    优化：使用单次 re.sub+回调遍历，替代原 find+切片拼接的 O(n*m) 复杂度。
    """
    labels = labels or {}

    # 1) 共享规则解析：与 render_prompt 一致，避免 {shared:...} 残留字面量
    template = _resolve_shared(template)

    # 2) 安全能力（延迟导入，避免与 prompts 包形成导入环，同 render_prompt）
    _sanitize_fn = None
    _desensitize_fn = None
    if sanitize or desensitize:
        from .prompts._security import sanitize_prompt_value, desensitize_sensitive_info

        _sanitize_fn = sanitize_prompt_value
        _desensitize_fn = desensitize_sensitive_info

    # 构建单次替换映射表
    # 有数据的占位符：首次出现→数据（经安全清洗），后续→标签
    # 只有标签的占位符：全部→标签
    used: set[str] = set()

    def _replacer(match: re.Match) -> str:
        full = match.group(0)  # __NAME__ 完整占位符
        if full in mapping:
            if full not in used:
                used.add(full)
                val = mapping[full]  # 首次出现用数据
                if _sanitize_fn is not None:
                    val = _sanitize_fn(val)
                if _desensitize_fn is not None:
                    val = _desensitize_fn(val)
                return val
            return labels.get(full, full)  # 后续出现用标签
        if full in labels:
            return labels[full]
        return full  # 兜底：保留原占位符

    return _FILL_TEMPLATE_PATTERN.sub(_replacer, template)


def _unwrap_outline_response(data: Any) -> list[dict]:
    """将 AI 返回的目录数据归一化为列表

    处理以下格式：
    - 直接数组：[...]
    - 包装器：{"children":[...]}、{"outline":[...]}（无标题字段）
    - 单个项：{"title":"...","children":[]}（有标题字段，不当作包装器）
    - AI 错误：{"error":"..."}（过滤后返回空列表）
    """
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        # 判断是否为单个目录项（有标题类字段）
        has_title = any(
            data.get(k) for k in ("title", "name", "heading", "section", "chapter", "label")
        )
        if has_title:
            # 单个目录项，包装为列表
            return [data]
        # 无标题字段，视为包装器，尝试提取内容
        for key in ("children", "outline", "items", "data", "result", "results"):
            value = data.get(key)
            if isinstance(value, list):
                return value
        # 既无标题也无已知包装器字段，返回空（可能是 AI 错误响应）
        logger.warning("无法识别的目录响应格式: %s", json.dumps(data, ensure_ascii=False)[:300])
        return []
    return []


def _normalize_outline_list_response(data: Any, context: str = "") -> list[dict]:
    """将 AI 返回的目录响应归一化为列表（严格版，异常时抛出 ValueError）

    与 _unwrap_outline_response 的区别：
    - 要求 context 参数
    - 对 None/空数组/纯文本/错误响应 抛出 ValueError 而非返回空列表
    - 支持 chapters 键
    - 按 name 字段识别单条记录
    """
    if data is None:
        raise ValueError(f"{context} 空响应")
    if isinstance(data, list):
        if len(data) == 0:
            raise ValueError(f"{context} 空数组")
        return data
    if isinstance(data, str):
        raise ValueError(f"{context} 非 JSON 文本")
    if isinstance(data, dict):
        # 检查是否为 AI 错误响应
        if "error" in data:
            msg = data.get("error", "未知错误")
            if isinstance(msg, dict):
                msg = msg.get("message", str(msg))
            raise ValueError(f"{context} 错误信息: {msg}")
        if "message" in data and len(data) == 1:
            raise ValueError(f"{context} {data['message']}")

        # 判断是否为单条记录（有标题类字段）
        has_title = any(
            data.get(k) for k in ("title", "name", "heading", "section", "chapter", "label")
        )
        if has_title:
            return [data]

        # 尝试提取包装器中的列表
        for key in ("children", "outline", "items", "data", "result", "results", "chapters"):
            value = data.get(key)
            if isinstance(value, list):
                return value
            if value is not None and not isinstance(value, (list, dict)):
                raise ValueError(f"{context} 无法识别响应格式")

        # 检查嵌套 dict 情况（如 {'result': {'items': [...]}}）
        logger.warning("无法识别的目录响应格式: %s", json.dumps(data, ensure_ascii=False)[:300])
        raise ValueError(f"{context} 无法识别响应格式")
    raise ValueError(f"{context} 无法识别响应格式")


_JSON_MODE_BACKOFF = 600.0

# json_mode 超时缓存：短时间（30 秒）内重复超时则跳过 json_mode，直接用非 json_mode
# 避免每次调用都白等 60s 超时再降级
_JSON_MODE_TIMEOUT_BACKOFF = 30.0

_JSON_MODE_UNSUPPORTED_MAX = 20

_json_mode_unsupported: dict[tuple[str, str], float] = {}

# json_mode 超时缓存（模块级，跨调用有效）
_json_mode_timeout_cache: dict[tuple[str, str], float] = {}

# BUG-FIX-08: 并发安全锁，保护 _json_mode_unsupported 和 _json_mode_timeout_cache 的并发读写
_json_mode_cache_lock = threading.Lock()


def _trim_json_mode_unsupported() -> None:
    """当缓存超过上限时，淘汰最旧的一半条目（按时间戳排序）"""
    with _json_mode_cache_lock:
        if len(_json_mode_unsupported) >= _JSON_MODE_UNSUPPORTED_MAX:
            sorted_items = sorted(_json_mode_unsupported.items(), key=lambda x: x[1])
            for key, _ in sorted_items[: len(sorted_items) // 2]:
                _json_mode_unsupported.pop(key, None)


def _provider_json_mode_key(provider: Any) -> tuple[str, str] | None:
    """构造 provider 的 json_mode 支持性缓存键 (base_url, model)"""
    try:
        base_url = getattr(provider, "base_url", "") or ""
        model = getattr(provider, "model", "") or ""
        return (base_url, model) if base_url else None
    except Exception:
        return None


async def collect_json_response(
    provider: Any,
    messages: list,
    temperature: float = 0.7,
    schema: type | None = None,
    validator: Any = None,
    max_retries: int = 1,
    max_tokens: int | None = None,
    extra_body: dict | None = None,
) -> Any:
    """统一 JSON 生成函数

    BUG-C-01 修复：全局 AI 并发上限控制。
    在函数入口 acquire 全局信号量，出口 release，
    确保所有调用 collect_json_response 的路径（目录生成、评分点分析等）
    均受 _ai_monitor 约束，防止多项目并发时 provider 过载。

    Args:
        max_retries: 整体重试次数。
            BUG O2 修复：默认从 3 改为 1，避免与 provider 内部 max_retries 叠加
            导致 AI 不可达时总延迟达到 6+ 分钟（3*2*60s）。
            现在总延迟 = 1次(2次 chat) * 2次 provider 重试 = 4次 AI 调用 * 60s = 240s = 4分钟。
            加上 JSON 自修复链路，最坏情况约 5 分钟，由 generate-sse 的 SSE_TOTAL_TIMEOUT 兜底。
    """
    # BUG-C-01 修复：全局 AI 并发上限控制（带超时保护）
    _acquired = await _ai_monitor.acquire(timeout=60.0)  # 60秒超时
    if not _acquired:
        raise RuntimeError("全局AI并发限制超时，无法获取AI请求许可")
    try:
        json_repair_prompt = get_prompt("JSON_REPAIR_PROMPT")

        # json_mode 降级标记：记录不支持 response_format(json_object) 的 (base_url, model)。
        # 一旦探测到失败，短时间内（10 分钟）跳过 json_mode，避免每次调用都白费一次请求。
        provider_key = _provider_json_mode_key(provider)

        for attempt in range(max_retries):
            raw_content = ""
            try:
                use_json_mode = True
                # BUG-FIX-08: 使用锁保护 _json_mode_unsupported 和 _json_mode_timeout_cache 的并发读写
                with _json_mode_cache_lock:
                    if provider_key and provider_key in _json_mode_unsupported:
                        _mark_ts = _json_mode_unsupported.get(provider_key, 0.0)
                        if time.time() - _mark_ts < _JSON_MODE_BACKOFF:
                            use_json_mode = False
                        else:
                            _json_mode_unsupported.pop(provider_key, None)
                    # 🔴 关键修复：如果 json_mode 刚超时过（30秒内），直接跳过 json_mode
                    # 使用模块级缓存 _json_mode_timeout_cache（跨调用有效）
                    if provider_key and provider_key in _json_mode_timeout_cache:
                        _to_ts = _json_mode_timeout_cache.get(provider_key, 0.0)
                        if time.time() - _to_ts < _JSON_MODE_TIMEOUT_BACKOFF:
                            use_json_mode = False
                        else:
                            _json_mode_timeout_cache.pop(provider_key, None)
                try:
                    if use_json_mode:
                        raw_content = await with_timeout(
                            provider.chat(
                                messages,
                                temperature=temperature,
                                json_mode=True,
                                max_tokens=max_tokens,
                                extra_body=extra_body,
                            ),
                            timeout=20,
                            error_msg="目录生成AI调用超时(json_mode)",
                        )
                    else:
                        raw_content = await with_timeout(
                            provider.chat(
                                messages,
                                temperature=temperature,
                                max_tokens=max_tokens,
                                extra_body=extra_body,
                            ),
                            timeout=30,
                            error_msg="目录生成AI调用超时",
                        )
                except TimeoutError as timeout_err:
                    # 🔴 关键 BUG 修复：json_mode 超时后立即降级到非 json_mode
                    # 许多模型（如 AGNES/SiliconFlow）在 json_mode 下响应更慢，
                    # 但非 json_mode（让模型自由生成文本，再用正则提取 JSON）通常更快。
                    # 之前超时时直接上抛，白白浪费了一次降级机会。
                    if use_json_mode:
                        logger.warning(
                            "json_mode 调用超时 (%s)，立即降级为非 json_mode 重试",
                            str(timeout_err),
                        )
                        # 标记 json_mode 超时，30 秒内跳过 json_mode
                        if provider_key:
                            with _json_mode_cache_lock:
                                _json_mode_timeout_cache[provider_key] = time.time()
                        try:
                            raw_content = await with_timeout(
                                provider.chat(
                                    messages,
                                    temperature=temperature,
                                    max_tokens=max_tokens,
                                    extra_body=extra_body,
                                ),
                                timeout=30,
                                error_msg="目录生成降级AI调用超时",
                            )
                        except TimeoutError:
                            # 非 json_mode 也超时，彻底失败
                            raise timeout_err
                        except Exception:
                            raise timeout_err
                    else:
                        raise
                except Exception as _chat_err:
                    # 关键修复：仅对"json_mode 参数不被支持"类错误（HTTP 400 参数错误）
                    # 降级为非 json_mode 重试。429 限流/超时/网络/鉴权错误与 json_mode
                    # 无关，立即原样重试只会把请求量放大一倍（限流期间的重试风暴），
                    # 且会错误地把正常 provider 标记为"不支持 json_mode"。
                    _chat_err_str = str(_chat_err)
                    _is_param_error = isinstance(
                        _chat_err, ValueError
                    ) and (
                        "请求参数错误" in _chat_err_str
                        or "response_format" in _chat_err_str.lower()
                    )
                    if _is_param_error and use_json_mode and provider_key:
                        with _json_mode_cache_lock:
                            _json_mode_unsupported[provider_key] = time.time()
                            if len(_json_mode_unsupported) >= _JSON_MODE_UNSUPPORTED_MAX:
                                sorted_items = sorted(
                                    _json_mode_unsupported.items(), key=lambda x: x[1]
                                )
                                for key, _ in sorted_items[: len(sorted_items) // 2]:
                                    _json_mode_unsupported.pop(key, None)
                        logger.info(
                            "模型不支持 json_mode(response_format)，已记录降级标记: %s",
                            provider_key,
                        )
                        raw_content = await with_timeout(
                            provider.chat(
                                messages,
                                temperature=temperature,
                                max_tokens=max_tokens,
                                extra_body=extra_body,
                            ),
                            timeout=30,
                            error_msg="目录生成降级AI调用超时",
                        )
                    else:
                        raise

                parsed = extract_json_from_text(raw_content)
                # BUG 修复：AI 可能返回合法 JSON 但为字符串/空字符串（如 `""`），
                # extract_json_from_text 会返回 `''` 而非 None，绕过下面的 None 检查，
                # 导致 collect_json_response 返回空字符串，上游 _normalize_outline_list_response
                # 报"AI 返回非 JSON 文本"且 preview 为空（历史日志多次出现）。
                if parsed is None or isinstance(parsed, str):
                    raise ValueError("无法从响应中提取 JSON")

                # BUG 修复：AI/提供商可能返回错误对象（如 ValidationError、ErrorResponse），
                # 这类响应格式合法但内容是错误信息，不是预期的业务数据。
                # 之前未检测到此情况，错误对象直接传给上游导致"无法识别的格式"错误。
                if isinstance(parsed, dict):
                    _ERR_TYPES = {
                        "ValidationError",
                        "Error",
                        "error",
                        "ApiError",
                        "InvalidRequestError",
                        "RateLimitError",
                    }
                    if (
                        parsed.get("type") in _ERR_TYPES
                        or parsed.get("error") is not None
                        or (parsed.get("code") and parsed.get("message"))
                    ):
                        error_type = (
                            parsed.get("type")
                            or parsed.get("error_type")
                            or parsed.get("code", "Unknown")
                        )
                        error_msg = parsed.get("message") or parsed.get("error") or str(parsed)
                        # 关键：标记使用 json_mode 时发生提供商错误 → 降级
                        if use_json_mode and provider_key:
                            logger.warning(
                                "json_mode 返回错误响应 [%s]，降级为非 json_mode: %s",
                                error_type,
                                error_msg[:200],
                            )
                            with _json_mode_cache_lock:
                                _json_mode_unsupported[provider_key] = time.time()
                                if len(_json_mode_unsupported) >= _JSON_MODE_UNSUPPORTED_MAX:
                                    sorted_items = sorted(
                                        _json_mode_unsupported.items(), key=lambda x: x[1]
                                    )
                                    for key, _ in sorted_items[: len(sorted_items) // 2]:
                                        _json_mode_unsupported.pop(key, None)
                            # 用非 json_mode 重试
                            raw_content = await with_timeout(
                                provider.chat(
                                    messages,
                                    temperature=temperature,
                                    max_tokens=max_tokens,
                                    extra_body=extra_body,
                                ),
                                timeout=60,
                                error_msg="目录生成降级AI调用超时(错误响应恢复)",
                            )
                            parsed = extract_json_from_text(raw_content)
                            if parsed is None or isinstance(parsed, str):
                                raise ValueError(f"降级后仍无法提取 JSON: {error_msg}")
                            # 递归检查降级结果
                            if isinstance(parsed, dict):
                                if parsed.get("type") in _ERR_TYPES:
                                    raise RuntimeError(f"AI持续返回错误: {parsed}")
                        else:
                            raise RuntimeError(f"AI返回错误响应 [{error_type}]: {error_msg}")

                if schema:
                    parsed = schema.model_validate(parsed)
                if validator:
                    is_valid, error_msg = validator(parsed)
                    if not is_valid:
                        raise ValueError(f"业务校验失败: {error_msg}")

                return parsed

            except Exception as e:
                # 关键修复：提供商级错误（429限流/配额耗尽/超时/网络/取消）不做 JSON 自修复。
                # 此类错误与 JSON 格式无关，"修复"调用只会在限流期间把请求量再放大一倍
                # （重试风暴；历史日志大量 "JSON 修复第 1/1 次失败: 请求频率过高" 即此问题）。
                # 仅当错误是 JSON 提取/解析/业务校验失败时才走自修复链路。
                _err_str = str(e)
                _PROVIDER_ERR_MARKERS = (
                    "请求频率过高",
                    "配额已耗尽",
                    "配额不足",
                    "HTTP 429",
                    "HTTP 402",
                    "quota",
                    "超时",
                    "timeout",
                    "timed out",
                    "Timeout",
                    "Connection",
                    "Connect",
                    "连接",
                    "网络",
                    "取消",
                    "Cancelled",
                    "canceled",
                    "不可达",
                    "所有 AI",
                    "API Key 无效",
                    "HTTP 401",
                    "访问被拒绝",
                    "HTTP 403",
                    "模型不存在",
                    "HTTP 404",
                )
                if any(_m in _err_str for _m in _PROVIDER_ERR_MARKERS):
                    if attempt == max_retries - 1:
                        raise
                    logger.warning(
                        "提供商级错误（非JSON格式问题），跳过JSON修复直接重试 (%d/%d): %s",
                        attempt + 1,
                        max_retries,
                        _err_str[:200],
                    )
                    continue
                # JSON 自修复：把原始（非法/不合规）输出连同错误原因回喂给模型，
                # 要求其输出严格合法的 JSON。这是弱模型稳定产出 JSON 的关键（设计文档五）。
                # 注意：provider.chat 要求 AIMessage 对象（按属性访问 .role/.content），
                # 此前误用 dict 会静默抛 AttributeError 使修复链路整体失效。
                repair_messages = [
                    AIMessage(role="system", content=json_repair_prompt),
                    AIMessage(
                        role="user",
                        content=f"目标类型: {schema.__name__ if schema else 'JSON'}\n"
                        f"校验问题: {e}\n"
                        f"原始输出: {raw_content or '(空)'}",
                    ),
                ]
                try:
                    repaired_content = await with_timeout(
                        provider.chat(repair_messages, temperature=0.3), timeout=60
                    )
                    parsed = extract_json_from_text(repaired_content)
                    # 同样的字符串/空值防护：修复结果也必须是 dict/list
                    if parsed is not None and not isinstance(parsed, str):
                        if schema:
                            parsed = schema.model_validate(parsed)
                        if validator:
                            is_valid, error_msg = validator(parsed)
                            if is_valid:
                                return parsed
                            logger.warning(
                                "JSON 修复第 %d/%d 次结果仍未通过校验: %s",
                                attempt + 1,
                                max_retries,
                                error_msg,
                            )
                            e = ValueError(f"JSON 校验未通过: {error_msg}")
                        else:
                            return parsed
                except Exception as repair_err:
                    logger.warning(
                        "JSON 修复第 %d/%d 次失败: %s",
                        attempt + 1,
                        max_retries,
                        repair_err,
                    )
                    # 记住最后一轮的真实失败原因。
                    # BUG-FIX 2026-09-16（中）：最终抛出时用的是最外层 `e`
                    # （第一轮解析异常），而实际终止的原因往往是最后一次自修复失败，
                    # 报错文案与真相不符，排查时会被带偏。
                    e = repair_err

                if attempt == max_retries - 1:
                    raise RuntimeError(f"JSON 生成失败（{max_retries} 次重试后）: {e}") from e

        raise RuntimeError(f"JSON 生成失败（{max_retries} 次重试后）")
    finally:
        await _ai_monitor.release()


_v51_outline_semaphore: DynamicSemaphore | None = None


_ai_monitor = AIRequestMonitor(max_concurrent=GLOBAL_AI_CONCURRENCY_MAX)
