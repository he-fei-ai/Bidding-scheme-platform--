"""AI 调用全局限流闸门（Rate Gate）。

## 为什么需要（生产日志实证）

`logs/backend.log` 中 2026-10-07 22:34~22:41 出现**持续 7 分钟、每分钟 70~97 次**
的 `HTTP 429`，累计 528 条；`app.services.ai.workflows_sections` 的 ERROR 占全部
ERROR 的 276/1650。失败信息统一为：

    您已达到免费用户的 API 速率限制 (AgnesAI_error, HTTP 429)

原有防护只有一处，且对限流无效：
`providers/openai_compatible.py` 的 `_GLOBAL_REQUEST_SEMAPHORE`
只限制**在飞并发数**（`AGNES_MAX_CONCURRENCY=5`），而厂商限制的是**每分钟请求数**
（RPM）与**每分钟 token 数**（TPM）。正文生成 `max_tokens` 动辄 8K~16K，
5 路并发瞬间打满 TPM，信号量对此完全无感。

而 429 的退避重试预算被复用通用 `max_retries`（provider_factory 传 2），
实际只有 **1 次重试 + 3~6s 等待**；面对持续限流必然失败，
且失败后章节级重试又以 1s/2s 退避、全并发再次冲击，形成正反馈。

## 本模块的职责

在 provider 发出**每一次** HTTP 请求之前统一节流，并按 AIMD
（加性增 Additive Increase / 乘性减 Multiplicative Decrease）自适应并发：

- **令牌桶（RPM）**：按 `base_url` 维度限制「每分钟最多 N 次请求」。
  N 取配置 `rate_limit_per_min`（0/未配置=不限流），默认 `AI_RATE_LIMIT_RPM`，
  默认 60 —— 与日志中 Agnes 免费套餐口径一致。
- **自适应并发**：初始上限取配置 `concurrency`（缺省 5）。一旦收到 429，
  立即**乘性减半**（下限 1）并进入冷却窗口（优先采信服务端 `Retry-After`）；
  连续成功达阈值则**加性恢复**（+1），恢复到初始上限为止。

即使上游限流口径与我们的估计不一致，也会在数十秒内自行收敛到可持续速率，
而不是像现在这样 7 分钟持续失败。

## 设计约束

- 纯进程内 `asyncio` 实现，**不依赖 Redis**：限流是"尽力而为"的保护，
  宁可放行也不能因缓存不可用而阻塞 AI 生成主链路。
- 令牌不足时**排队等待**而非直接拒绝，尽量不牺牲成功率；
  等待可被 `cancel_event` 打断，保证「停止生成」能立即生效。
- 许可在**每次 HTTP 请求**粒度获取/释放：退避等待期间不持有许可，
  让其它章节有机会推进，避免"一个章节在退避却把闸门堵死"。
- 状态用 `time.monotonic()` 计算，不受系统时钟调整影响。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# ── 环境变量默认值 ────────────────────────────────────────────────────────
DEFAULT_RPM = int(os.environ.get("AI_RATE_LIMIT_RPM", "60") or 60)
DEFAULT_CONCURRENCY = int(os.environ.get("AI_RATE_CONCURRENCY", "5") or 5)
MIN_CONCURRENCY = 1
DECREASE_FACTOR = 0.5          # 收到 429：并发 × 0.5
RECOVER_AFTER = 20             # 连续成功多少次后 +1 并发
BURST_FACTOR = 1.0             # 桶容量 = RPM，允许 1 秒内用完整额度
#: 并发闸门轮询间隔（秒）。仅在"并发已满需等待"时使用，
#: 兼顾"能被 cancel_event 及时唤醒"与"不空转烧 CPU"。
_CONCURRENCY_POLL = 0.25
#: 冷却/令牌等待的单次上限（秒），保证取消与配置变更能及时生效。
_MAX_SLEEP = 30.0


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _parse_retry_after(raw: str | None) -> float | None:
    """解析 `Retry-After`，支持「秒数」与「HTTP-date」两种合法格式。

    返回等待秒数；无法解析或应立即重试（<=0）时返回 None。
    """
    if not raw:
        return None
    raw = raw.strip()
    if raw.isdigit():
        return float(raw)
    try:  # HTTP-date: Wed, 21 Oct 2026 07:28:00 GMT
        from datetime import timezone
        from email.utils import parsedate_to_datetime

        target = parsedate_to_datetime(raw)
        if target is None:
            return None
        if target.tzinfo is None:
            target = target.replace(tzinfo=timezone.utc)
        return max(0.0, target.timestamp() - time.time())
    except (TypeError, ValueError, IndexError, OverflowError):
        return None


async def _interruptible_sleep(seconds: float, cancel_event: asyncio.Event | None) -> None:
    """可被 cancel_event 打断的等待。"""
    if seconds <= 0:
        if cancel_event is not None and cancel_event.is_set():
            raise asyncio.CancelledError("操作已被用户取消")
        return
    if cancel_event is None:
        await asyncio.sleep(seconds)
        return
    try:
        await asyncio.wait_for(cancel_event.wait(), timeout=seconds)
        raise asyncio.CancelledError("操作已被用户取消")
    except TimeoutError:
        pass


class _AdaptiveLimiter:
    """可即时升降限的自适应并发闸门。

    刻意不使用 `asyncio.Semaphore`：Semaphore 一旦放行许可就无法"收回"，
    无法实现「收到 429 立刻把并发从 8 砍到 4」。这里用计数器 + 广播唤醒，
    `set_limit()` 立即对后续 acquire 生效。
    """

    def __init__(self, limit: int) -> None:
        self.limit = max(MIN_CONCURRENCY, int(limit))
        self._in_flight = 0
        self._wakeup = asyncio.Event()

    @property
    def in_flight(self) -> int:
        return self._in_flight

    def _broadcast(self) -> None:
        """唤醒所有等待者，并换上新的 Event 供下一批等待。"""
        pending, self._wakeup = self._wakeup, asyncio.Event()
        pending.set()

    async def acquire(self, cancel_event: asyncio.Event | None = None) -> None:
        while True:
            if cancel_event is not None and cancel_event.is_set():
                raise asyncio.CancelledError("操作已被用户取消")
            if self._in_flight < self.limit:
                self._in_flight += 1
                return
            try:
                await asyncio.wait_for(self._wakeup.wait(), timeout=_CONCURRENCY_POLL)
            except TimeoutError:
                pass

    def release(self) -> None:
        self._in_flight = max(0, self._in_flight - 1)
        self._broadcast()

    def set_limit(self, limit: int) -> int:
        """立即调整上限，返回调整前的旧值。"""
        old = self.limit
        new = max(MIN_CONCURRENCY, int(limit))
        if new != old:
            self.limit = new
            self._broadcast()
        return old


@dataclass
class _GateState:
    """单个 base_url 的闸门状态。"""

    rpm: float
    max_concurrency: int
    initial_concurrency: int
    limiter: _AdaptiveLimiter
    capacity: float
    tokens: float = field(default=0.0)
    last_refill: float = field(default_factory=time.monotonic)
    cooldown_until: float = 0.0
    successes: int = 0
    throttled: int = 0
    last_throttled_at: float = 0.0


class _RateGate:
    """令牌桶（RPM）+ 自适应并发（AIMD）。线程/任务安全的单进程闸门。"""

    def __init__(self, enabled: bool | None = None) -> None:
        self._states: dict[str, _GateState] = {}
        self._enabled = _env_flag("AI_RATE_GATE_ENABLED", True) if enabled is None else enabled
        self._rpm_override = _env_int("AI_RATE_LIMIT_RPM", DEFAULT_RPM)
        self._conc_override = _env_int("AI_RATE_CONCURRENCY", DEFAULT_CONCURRENCY)

    # ── 状态管理 ────────────────────────────────────────────────────────
    @staticmethod
    def _normalize(base_url: str) -> str:
        return (base_url or "").strip().rstrip("/").lower()

    def _state(self, base_url: str, rpm: float | None, concurrency: int | None) -> _GateState:
        key = self._normalize(base_url)
        eff_rpm = float(rpm) if rpm and rpm > 0 else float(self._rpm_override)
        if not self._enabled:
            eff_rpm = 0.0  # 0 = 不限流
        eff_conc = int(concurrency) if concurrency and concurrency > 0 else self._conc_override

        st = self._states.get(key)
        if st is None:
            st = _GateState(
                rpm=eff_rpm,
                max_concurrency=eff_conc,
                initial_concurrency=eff_conc,
                limiter=_AdaptiveLimiter(eff_conc),
                capacity=eff_rpm * BURST_FACTOR,
            )
            st.tokens = st.capacity
            self._states[key] = st
            logger.info(
                "[限流闸门] 初始化 base_url=%s rpm=%.0f 并发上限=%d",
                key or "(default)", st.rpm, st.limiter.limit,
            )
            return st

        # 配置热更新：RPM 立即生效；并发上限跟随，但**不越过**当前已收缩到的值，
        # 避免正在退避期被外部配置立刻拉回高并发。
        if st.rpm != eff_rpm:
            st.rpm = eff_rpm
            st.capacity = eff_rpm * BURST_FACTOR
            st.tokens = min(st.tokens, st.capacity)
        if st.max_concurrency != eff_conc:
            st.max_concurrency = eff_conc
            if eff_conc > st.limiter.limit:
                st.initial_concurrency = eff_conc
        return st

    def _refill(self, st: _GateState) -> None:
        if st.rpm <= 0:
            st.tokens = st.capacity = 0.0
            st.last_refill = time.monotonic()
            return
        now = time.monotonic()
        elapsed = max(0.0, now - st.last_refill)
        st.last_refill = now
        st.tokens = min(st.capacity, st.tokens + elapsed * (st.rpm / 60.0))

    # ── 对外 API ────────────────────────────────────────────────────────
    @contextlib.asynccontextmanager
    async def slot(
        self,
        base_url: str = "",
        *,
        rpm: float | None = None,
        concurrency: int | None = None,
        cancel_event: asyncio.Event | None = None,
    ) -> AsyncIterator[_GateState | None]:
        """获取一次 AI HTTP 请求许可（令牌桶 + 自适应并发）。"""
        st = self._state(base_url, rpm, concurrency)
        if not self._enabled:
            yield None
            return

        # 1) 冷却窗口（on_rate_limited 设置）
        cooldown = st.cooldown_until - time.monotonic()
        if cooldown > 0:
            await _interruptible_sleep(min(cooldown, _MAX_SLEEP), cancel_event)

        # 2) 令牌桶（RPM）：不足则排队等攒够 1 个令牌
        while True:
            if cancel_event is not None and cancel_event.is_set():
                raise asyncio.CancelledError("操作已被用户取消")
            self._refill(st)
            if st.rpm <= 0 or st.tokens >= 1.0:
                st.tokens = max(0.0, st.tokens - 1.0)
                break
            wait = (1.0 - st.tokens) / (st.rpm / 60.0)
            await _interruptible_sleep(min(max(wait, 0.05), _MAX_SLEEP), cancel_event)

        # 3) 自适应并发
        await st.limiter.acquire(cancel_event)
        try:
            yield st
        finally:
            st.limiter.release()

    def on_success(self, base_url: str = "") -> None:
        """一次非限流成功：累加连续成功计数，达阈值则加性恢复并发。"""
        if not self._enabled:
            return
        st = self._states.get(self._normalize(base_url))
        if st is None:
            return
        st.successes += 1
        if st.successes >= RECOVER_AFTER:
            st.successes = 0
            ceiling = max(st.max_concurrency, st.initial_concurrency)
            if st.limiter.limit < ceiling:
                old = st.limiter.set_limit(min(ceiling, st.limiter.limit + 1))
                logger.info("[限流闸门] 自适应并发恢复 %d -> %d", old, st.limiter.limit)

    def on_rate_limited(
        self,
        base_url: str = "",
        retry_after: float | None = None,
    ) -> float:
        """收到 429：并发乘性减半 + 进入冷却窗口；返回建议等待秒数。"""
        st = self._states.get(self._normalize(base_url)) if self._enabled else None
        if st is None:
            return float(retry_after or 0.0)

        st.throttled += 1
        st.last_throttled_at = time.monotonic()
        st.successes = 0
        # 令牌桶清零：既然被拒，说明当前速率一定超了，立刻归零重新积累。
        st.tokens = 0.0
        st.last_refill = time.monotonic()

        current = st.limiter.limit
        if current > MIN_CONCURRENCY:
            target = max(MIN_CONCURRENCY, int(current * DECREASE_FACTOR))
            st.limiter.set_limit(target)
            logger.warning(
                "[限流闸门] 触发限流 base_url=%s 第 %d 次 → 并发 %d -> %d",
                self._normalize(base_url) or "(default)", st.throttled, current, target,
            )

        # 冷却：优先采信服务端 Retry-After，否则给一个与收缩幅度相称的窗口。
        suggested = float(retry_after or 0.0)
        if suggested <= 0:
            suggested = min(60.0, max(5.0, 2.0 * (st.max_concurrency - st.limiter.limit + 1)))
        st.cooldown_until = max(st.cooldown_until, time.monotonic() + suggested)
        logger.warning(
            "[限流闸门] 冷却 %.1fs（并发上限现为 %d）", suggested, st.limiter.limit,
        )
        return suggested

    # ── 运维/测试可观测接口 ─────────────────────────────────────────────
    def snapshot(self, base_url: str = "") -> dict:
        st = self._states.get(self._normalize(base_url))
        if st is None:
            return {}
        return {
            "rpm": st.rpm,
            "tokens": round(st.tokens, 2),
            "concurrency": st.limiter.limit,
            "in_flight": st.limiter.in_flight,
            "max_concurrency": max(st.max_concurrency, st.initial_concurrency),
            "throttled": st.throttled,
            "cooldown_remaining": round(max(0.0, st.cooldown_until - time.monotonic()), 2),
        }

    def reset(self, base_url: str = "") -> None:
        """重置闸门状态（测试与「换 Key / 换模型」后立即恢复用）。"""
        if base_url:
            self._states.pop(self._normalize(base_url), None)
        else:
            self._states.clear()


#: 进程级单例
rate_gate = _RateGate()


def get_rate_gate() -> _RateGate:
    return rate_gate


__all__ = [
    "DEFAULT_CONCURRENCY",
    "DEFAULT_RPM",
    "MIN_CONCURRENCY",
    "RECOVER_AFTER",
    "_parse_retry_after",
    "get_rate_gate",
    "rate_gate",
]