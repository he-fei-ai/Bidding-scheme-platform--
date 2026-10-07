"""章节编写模块 · AI 限流闸门（rate_gate）与 429 退避回归测试。

BUG-FIX 2026-10-07：修复 `logs/backend.log` 中 2026-10-07 22:34~22:41
连续 7 分钟、每分钟 70~97 次、累计 528 次 HTTP 429 导致章节生成全军覆没的问题。

覆盖：
  1. 令牌桶按 RPM 节流（并验证不限流配置可关闭）
  2. AIMD：429 乘性减半并发 + 冷却窗口；连续成功后加性恢复
  3. 自适应并发闸门可即时升降限（Semaphore 做不到）
  4. `Retry-After` 双格式解析（秒数 / HTTP-date）
  5. provider 层 429 重试预算不再被通用 max_retries 截断
  6. cancel_event 能立即打断闸门等待（"停止生成"必须即时生效）
  7. `_is_rate_limit_error` 能识别日志中的真实中文错误文本
"""

import asyncio
import time

import pytest

from app.services.ai.rate_gate import (
    MIN_CONCURRENCY,
    RECOVER_AFTER,
    _AdaptiveLimiter,
    _parse_retry_after,
    _RateGate,
)


# ─────────────────────────── 1. 令牌桶 RPM ───────────────────────────
class TestTokenBucket:
    @pytest.mark.asyncio
    async def test_rpm_throttles_request_rate(self):
        """rpm=60 时，第 61 次请求必须等待（桶容量=60，允许 1 秒内用完整额度）。"""
        gate = _RateGate()
        url = "https://api.test/v1"
        # 先把桶抽干：60 次
        for _ in range(60):
            async with gate.slot(url, rpm=60, concurrency=10):
                pass
        snap = gate.snapshot(url)
        assert snap["tokens"] < 1.0, "令牌桶应已被抽干"

        t0 = time.monotonic()
        async with gate.slot(url, rpm=60, concurrency=10):
            pass
        elapsed = time.monotonic() - t0
        # 60 RPM = 每秒 1 个令牌，补 1 个令牌需 ~1s。留足 CI 抖动余量。
        assert elapsed >= 0.5, f"第 61 次请求应被限流，实际耗时 {elapsed:.3f}s"

    @pytest.mark.asyncio
    async def test_rpm_zero_falls_back_to_safe_default(self):
        """rpm=0/None 语义是"未配置"→ 回落到AI_RATE_LIMIT_RPM 默认值，而非放开不限流。

        这是刻意的安全取向：限流保护宁可偏紧，也不能因为配置缺失就让
        章节生成以 8 路并发无限冲击厂商配额（即 2026-10-07 的故障形态）。
        """
        gate = _RateGate()
        url = "https://api.nolimit/v1"
        async with gate.slot(url, rpm=0, concurrency=10):
            pass
        assert gate.snapshot(url)["rpm"] > 0, "未配置 rpm 时应回落到默认限额，而非不限流"

    @pytest.mark.asyncio
    async def test_large_rpm_does_not_throttle_short_burst(self):
        gate = _RateGate()
        url = "https://api.fast/v1"
        t0 = time.monotonic()
        for _ in range(30):
            async with gate.slot(url, rpm=6000, concurrency=10):
                pass
        assert time.monotonic() - t0 < 1.0, "高 RPM 下短突发不应产生等待"

    @pytest.mark.asyncio
    async def test_gate_disabled_passes_through(self):
        gate = _RateGate(enabled=False)
        async with gate.slot("https://api.off/v1", rpm=1, concurrency=1) as st:
            assert st is None, "闸门关闭时不返回状态对象"

    @pytest.mark.asyncio
    async def test_reset_clears_state(self):
        gate = _RateGate()
        url = "https://api.reset/v1"
        async with gate.slot(url, rpm=600, concurrency=4):
            pass
        gate.on_rate_limited(url)
        assert gate.snapshot(url)["throttled"] == 1
        gate.reset(url)
        assert gate.snapshot(url) == {}, "reset 后应清空该base_url 的全部状态"

    @pytest.mark.asyncio
    async def test_throttle_event_on_unknown_base_url_is_noop(self):
        """未建立状态就收到 429（理论不可能）不应抛错——闸门必须是纯保护。"""
        gate = _RateGate()
        assert gate.on_rate_limited("https://never-seen/v1", 5.0) == 5.0


# ─────────────────── 2. AIMD 自适应并发（429 收缩 / 成功恢复）───────────────
class TestAdaptiveConcurrency:
    @pytest.mark.asyncio
    async def test_rate_limit_halves_concurrency(self):
        gate = _RateGate()
        url = "https://api.aimd/v1"
        async with gate.slot(url, rpm=600, concurrency=8):
            pass
        assert gate.snapshot(url)["concurrency"] == 8

        gate.on_rate_limited(url)
        assert gate.snapshot(url)["concurrency"] == 4, "429 后应乘性减半"

        gate.on_rate_limited(url)
        assert gate.snapshot(url)["concurrency"] == 2

        gate.on_rate_limited(url)
        assert gate.snapshot(url)["concurrency"] == 1

    @pytest.mark.asyncio
    async def test_concurrency_never_below_one(self):
        gate = _RateGate()
        url = "https://aimd.floor/v1"
        async with gate.slot(url, rpm=600, concurrency=2):
            pass
        for _ in range(10):
            gate.on_rate_limited(url)
        assert gate.snapshot(url)["concurrency"] == MIN_CONCURRENCY

    @pytest.mark.asyncio
    async def test_recovers_additively_after_sustained_success(self):
        gate = _RateGate()
        url = "https://aimd.recover/v1"
        async with gate.slot(url, rpm=600, concurrency=8):
            pass
        gate.on_rate_limited(url)  # 8 -> 4
        assert gate.snapshot(url)["concurrency"] == 4

        for _ in range(RECOVER_AFTER):
            gate.on_success(url)
        assert gate.snapshot(url)["concurrency"] == 5, "连续成功达阈值后应 +1"

    @pytest.mark.asyncio
    async def test_recovery_never_exceeds_ceiling(self):
        gate = _RateGate()
        url = "https://aimd.ceiling/v1"
        async with gate.slot(url, rpm=600, concurrency=4):
            pass
        for _ in range(RECOVER_AFTER * 5):
            gate.on_success(url)
        assert gate.snapshot(url)["concurrency"] <= 4

    @pytest.mark.asyncio
    async def test_cooldown_blocks_immediate_reentry(self):
        """收到 429 后应进入冷却窗口，且窗口随 Retry-After 采信。"""
        gate = _RateGate()
        url = "https://aimd.cooldown/v1"
        async with gate.slot(url, rpm=600, concurrency=8):
            pass
        suggested = gate.on_rate_limited(url, retry_after=2.0)
        assert suggested == 2.0
        assert gate.snapshot(url)["cooldown_remaining"] > 1.0

    @pytest.mark.asyncio
    async def test_retry_after_wins_over_local_backoff(self):
        """服务端 Retry-After=30 时，等待建议必须 >= 30s，不能退回本地 3s 退避。"""
        gate = _RateGate()
        url = "https://aimd.ra/v1"
        async with gate.slot(url, rpm=600, concurrency=8):
            pass
        assert gate.on_rate_limited(url, retry_after=30.0) >= 30.0


class TestAdaptiveLimiter:
    @pytest.mark.asyncio
    async def test_shrink_takes_effect_immediately(self):
        """Semaphore 无法收回已放行的许可；本实现必须让收缩立刻对后续 acquire 生效。

        语义：在飞请求数需自然退出到**低于**新上限后，才放行新的请求。
        """
        lim = _AdaptiveLimiter(4)
        for _ in range(4):
            await lim.acquire()
        assert lim.in_flight == 4

        old = lim.set_limit(1)  # 立即收缩，无需等待在飞请求结束
        assert old == 4 and lim.limit == 1

        # 在飞仍是 4（远高于新上限 1）时，新请求必须被挡住
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(lim.acquire(), timeout=0.5)
        assert lim.in_flight == 4, "被挡住时不得增加在飞计数"

        # 存量退出到 0 之后，新请求立即放行 —— 这正是 Semaphore 做不到的"收缩"
        for _ in range(4):
            lim.release()
        assert lim.in_flight == 0
        await asyncio.wait_for(lim.acquire(), timeout=1.0)
        assert lim.in_flight == 1

    @pytest.mark.asyncio
    async def test_shrink_to_same_inflight_still_blocks(self):
        """收缩到恰好等于在飞数时，新请求仍需等待（不得"插队"）。"""
        lim = _AdaptiveLimiter(4)
        for _ in range(4):
            await lim.acquire()
        lim.set_limit(4)
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(lim.acquire(), timeout=0.5)
        lim.release()
        await asyncio.wait_for(lim.acquire(), timeout=1.0)

    @pytest.mark.asyncio
    async def test_grow_allows_more_concurrent_holders(self):
        lim = _AdaptiveLimiter(1)
        await lim.acquire()
        lim.set_limit(3)
        await lim.acquire()
        await lim.acquire()
        assert lim.in_flight == 3

    @pytest.mark.asyncio
    async def test_release_never_goes_negative(self):
        lim = _AdaptiveLimiter(2)
        lim.release()
        lim.release()
        assert lim.in_flight == 0

    @pytest.mark.asyncio
    async def test_cancel_event_interrupts_wait(self):
        """"停止生成"必须在等待闸门时立即生效，而不是等退避结束。"""
        lim = _AdaptiveLimiter(1)
        await lim.acquire()  # 占满
        cancel = asyncio.Event()
        task = asyncio.create_task(lim.acquire(cancel))
        await asyncio.sleep(0.1)
        cancel.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2.0)


class TestSlotCancellation:
    @pytest.mark.asyncio
    async def test_slot_respects_cancel_event(self):
        gate = _RateGate()
        url = "https://cancel.slot/v1"
        async with gate.slot(url, rpm=6000, concurrency=1):
            pass
        cancel = asyncio.Event()
        cancel.set()
        with pytest.raises(asyncio.CancelledError):
            async with gate.slot(url, rpm=6000, concurrency=1, cancel_event=cancel):
                pass


# ─────────────────────────── 4. Retry-After 解析 ───────────────────────────
class TestParseRetryAfter:
    def test_seconds_format(self):
        assert _parse_retry_after("30") == 30.0

    def test_missing_or_invalid(self):
        assert _parse_retry_after(None) is None
        assert _parse_retry_after("") is None
        assert _parse_retry_after("soon") is None

    def test_http_date_future(self):
        from email.utils import format_datetime
        from datetime import datetime, timedelta, timezone

        target = datetime.now(timezone.utc) + timedelta(seconds=45)
        parsed = _parse_retry_after(format_datetime(target, usegmt=True))
        assert parsed is not None
        assert 40 <= parsed <= 50

    def test_http_date_past_returns_zero(self):
        from email.utils import format_datetime
        from datetime import datetime, timedelta, timezone

        target = datetime.now(timezone.utc) - timedelta(seconds=10)
        assert _parse_retry_after(format_datetime(target, usegmt=True)) == 0.0


# ─────────────────── 5. provider 层 429 重试预算 ───────────────────
class TestProviderRateLimitRetry:
    """验证 429 不再被通用 max_retries(=2) 截断为「1 次重试」。"""

    @pytest.mark.asyncio
    async def test_429_retries_exceed_generic_budget(self, monkeypatch):
        from app.services.ai.providers import openai_compatible as oc

        gate = _RateGate(enabled=False)
        monkeypatch.setattr(oc, "_rate_gate", gate)
        monkeypatch.setattr(oc, "_RATE_LIMIT_BASE_WAIT", 0.01)
        monkeypatch.setattr(oc, "_RATE_LIMIT_MAX_WAIT", 0.05)
        # 闸门置为 disabled 以隔离"退避时长计算"与"闸门冷却时长"：
        # 本用例只验证 429 重试**次数**不再被通用 max_retries(=2) 截断，
        # 冷却窗口的收敛行为由 TestAdaptiveConcurrency 单独覆盖。

        attempts = {"n": 0}

        class _Resp:
            status_code = 429
            text = '{"error":{"message":"您已达到免费用户的 API 速率限制。"}}'
            headers: dict = {}

            def json(self):
                return {}

        class _Client:
            async def post(self, *a, **kw):
                attempts["n"] += 1
                return _Resp()

        provider = oc.OpenAICompatibleProvider(
            api_key="k", base_url="https://api.429test/v1", max_retries=2,
        )

        @oc.asynccontextmanager
        async def _fake_client(self, timeout, headers):
            yield _Client()

        monkeypatch.setattr(oc.OpenAICompatibleProvider, "_get_client", _fake_client)

        with pytest.raises(RuntimeError) as exc:
            await provider.chat([oc.AIMessage(role="user", content="hi")])

        msg = str(exc.value)
        assert "429" in msg
        # 修复前：max_retries=2 → 仅 2 次尝试；修复后应 > 3 次。
        assert attempts["n"] >= 4, f"429 重试次数应远超修复前的 2 次，实际 {attempts['n']}"
        assert "已重试" in msg, "错误信息应说明限流重试次数"

    @pytest.mark.asyncio
    async def test_quota_exhausted_fails_fast(self, monkeypatch):
        """真正的配额耗尽不应重试（重试只会浪费 6 次预算）。"""
        from app.services.ai.providers import openai_compatible as oc

        attempts = {"n": 0}

        class _Resp:
            status_code = 429
            text = '{"error":{"code":"insufficient_quota"}}'
            headers: dict = {}

            def json(self):
                return {}

        class _Client:
            async def post(self, *a, **kw):
                attempts["n"] += 1
                return _Resp()

        provider = oc.OpenAICompatibleProvider(
            api_key="k", base_url="https://api.quota/v1", max_retries=2,
        )

        @oc.asynccontextmanager
        async def _fake_client(self, timeout, headers):
            yield _Client()

        monkeypatch.setattr(oc.OpenAICompatibleProvider, "_get_client", _fake_client)

        with pytest.raises(RuntimeError, match="配额已耗尽"):
            await provider.chat([oc.AIMessage(role="user", content="hi")])
        assert attempts["n"] == 1, "配额耗尽应首次即失败，不做无谓重试"


# ────────────── 6. 限流错误识别（章节级重试策略分流依据）──────────────
class TestIsRateLimitError:
    def test_matches_real_log_message(self):
        from app.services.ai.workflows_sections import _is_rate_limit_error

        # 取自 logs/backend.log 2026-10-07 22:39:37 的真实错误
        real = (
            '请求频率过高，重试失败 (HTTP 429): {"error":{"code":"","message":'
            '"您已达到免费用户的 API 速率限制。升级 Token Plan 即可解锁更高限额，'
            '继续不间断使用 API。 (request id: 202610071439388985973pp2bInJh)",'
            '"type":"AgnesAI_error"}}'
        )
        assert _is_rate_limit_error(real) is True

    def test_matches_english_variants(self):
        from app.services.ai.workflows_sections import _is_rate_limit_error

        assert _is_rate_limit_error("Rate limit exceeded") is True
        assert _is_rate_limit_error("429 Too Many Requests") is True

    def test_rejects_unrelated_errors(self):
        from app.services.ai.workflows_sections import _is_rate_limit_error

        assert _is_rate_limit_error("请求超时（120秒）") is False
        assert _is_rate_limit_error("API Key 无效或未授权 (HTTP 401)") is False
        assert _is_rate_limit_error("章节正文为空") is False
        assert _is_rate_limit_error(None) is False