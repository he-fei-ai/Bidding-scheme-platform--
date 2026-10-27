#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""2026-10-23 日志驱动加固（第九轮）护栏测试。

每一条断言都对应 logs/backend.log* 中的**生产实证**，不是凭空猜测：

- P0-A：目录生成 L2 失败章节直接落「默认目录」，用户拿到与项目无关的模板目录
  实证：2026-10-22 21:47:40~21:54:58 project=1871130 rid=1c5c495aa4f1
       `V5.1 L2 8/15 章节失败，已用默认目录`
       `V5.1 L2 13/15 章节失败，已用默认目录`
- P0-B：超时被误报成「操作已被用户取消」
  实证：同窗口 69 条 `[AI监控] 调用失败 [...]: 操作已被用户取消`，
       与 107 条 `[with_timeout] 超时（…），任务被中止` 一一对应；
       `AI 调用失败率过高: 42.0% (阈值: 30.0%)` 被这条错误文案推高。
- P1-A：rate_gate 限流日志同因刷屏（265 + 27 条），淹没真实异常信号。
- P1-B：ai_monitor 三种告警共用一个冷却时间戳，慢请求告警会吞掉失败率告警。
"""

from __future__ import annotations

import asyncio
import logging

import pytest


# ══════════════════════════════════════════════════════════════════════
# P0-B：CancelledError 的错误分类
# ══════════════════════════════════════════════════════════════════════
class TestTimeoutNotMisreportedAsUserCancel:
    """超时中止的 CancelledError 不得再被记成「操作已被用户取消」。"""

    @staticmethod
    def _provider():
        from app.services.ai.providers.openai_compatible import (
            OpenAICompatibleProvider,
        )

        return OpenAICompatibleProvider(
            api_key="k", base_url="https://example.invalid/v1", model="m",
            max_retries=1,
        )

    @staticmethod
    def _patch_client_raises_cancel(monkeypatch, prov):
        """让真实 `chat()` 走到 `except asyncio.CancelledError` 分支。

        模拟的是真实链路：外层 `with_timeout` 超时 → `asyncio.wait_for`
        取消在飞的 provider 调用 → provider 内部正在 await 的东西抛出
        CancelledError。这里让 `_get_client`（`async with` 的第一站）抛，
        等价于在任意 await 点被取消。
        """
        import contextlib

        @contextlib.asynccontextmanager
        async def _boom_client(self, timeout, headers):
            await asyncio.sleep(0)
            raise asyncio.CancelledError()
            yield  # pragma: no cover

        monkeypatch.setattr(type(prov), "_get_client", _boom_client, raising=True)

    def test_timeout_cancel_is_not_reported_as_user_cancel(self, monkeypatch):
        """未 set cancel_event 的取消 = 外层超时/任务取消，不能谎称用户取消。"""
        import app.services.ai_monitor as monitor_mod

        captured = {}
        monkeypatch.setattr(
            monitor_mod,
            "record_ai_call",
            lambda **kw: captured.update(kw),
        )

        prov = self._provider()
        self._patch_client_raises_cancel(monkeypatch, prov)

        with pytest.raises(asyncio.CancelledError):
            asyncio.run(prov.chat([], cancel_event=None))

        err = captured.get("error", "")
        assert "用户取消" not in err, (
            "超时导致的取消仍被归因成用户取消，运维会按错误方向排查：" + err
        )
        assert "超时" in err
        assert captured.get("success") is False

    def test_real_user_cancel_keeps_original_wording(self, monkeypatch):
        """真正的用户取消（cancel_event 已 set）必须保留原文案，向后兼容。"""
        import contextlib

        import app.services.ai_monitor as monitor_mod

        captured = {}
        monkeypatch.setattr(
            monitor_mod,
            "record_ai_call",
            lambda **kw: captured.update(kw),
        )
        prov = self._provider()

        # cancel_event 在**请求在飞期间**被 set（真实用户点「停止生成」的时序）：
        # 进入 chat 时还是 False（否则会被循环顶部的预检提前 raise，
        # 那条路径本就不做监控记录，是历史行为），在被取消的那一刻变 True。
        ev = asyncio.Event()

        @contextlib.asynccontextmanager
        async def _cancel_midflight(self, timeout, headers):
            await asyncio.sleep(0)
            ev.set()
            raise asyncio.CancelledError()
            yield  # pragma: no cover

        monkeypatch.setattr(type(prov), "_get_client", _cancel_midflight, raising=True)

        with pytest.raises(asyncio.CancelledError):
            asyncio.run(prov.chat([], cancel_event=ev))

        assert captured.get("error") == "操作已被用户取消"


# ══════════════════════════════════════════════════════════════════════
# P1-A：rate_gate 同因日志节流
# ══════════════════════════════════════════════════════════════════════
class TestRateGateLogThrottle:
    def test_should_log_throttled_basic(self, monkeypatch):
        from app.services.ai import rate_gate as rg

        rg.reset_log_throttle()
        monkeypatch.setattr(rg, "_LOG_THROTTLE_WINDOW", 60.0)
        ok1, sup1 = rg._should_log_throttled("k")
        ok2, _ = rg._should_log_throttled("k")
        ok3, _ = rg._should_log_throttled("k")
        assert (ok1, ok2, ok3) == (True, False, False)
        assert sup1 == 0

        # 把时刻回拨越过窗口 → 重新放行，并回报被抑制的条数
        import time as _t

        rg._LOG_THROTTLE_STATE["k"][0] -= 120.0
        assert _t.monotonic() > 0  # 保证 monotonic 可用
        ok4, sup4 = rg._should_log_throttled("k")
        assert ok4 is True
        assert sup4 == 2, "窗口重开时应回报此前被抑制的条数，不能静默丢日志"
        rg.reset_log_throttle()

    def test_window_zero_keeps_legacy_behavior(self, monkeypatch):
        from app.services.ai import rate_gate as rg

        monkeypatch.setattr(rg, "_LOG_THROTTLE_WINDOW", 0.0)
        for _ in range(5):
            ok, _ = rg._should_log_throttled("legacy")
            assert ok is True

    def test_on_rate_limited_log_is_throttled(self, monkeypatch, caplog):
        """连续 20 次 429 不应再打出 40 条 WARNING。"""
        from app.services.ai import rate_gate as rg

        rg.reset_log_throttle()
        monkeypatch.setattr(rg, "_LOG_THROTTLE_WINDOW", 60.0)
        gate = rg._RateGate(enabled=True)
        gate._state("https://api.example/v1", rpm=60, concurrency=8)
        with caplog.at_level(logging.WARNING, logger="app.services.ai.rate_gate"):
            for _ in range(20):
                gate.on_rate_limited("https://api.example/v1")
        n = sum(1 for r in caplog.records if r.levelno >= logging.WARNING)
        # 触发限流 + 冷却 各 1 条（首条），其余被抑制
        assert n <= 2, f"同因限流日志未被节流，仍打出 {n} 条 WARNING"
        rg.reset_log_throttle()

    def test_suppressed_count_is_reported(self, monkeypatch, caplog):
        """窗口重开后的下一条日志必须带上「已抑制 N 条」，保证可追溯。"""
        from app.services.ai import rate_gate as rg

        rg.reset_log_throttle()
        monkeypatch.setattr(rg, "_LOG_THROTTLE_WINDOW", 60.0)
        gate = rg._RateGate(enabled=True)
        gate._state("https://api.example/v1", rpm=60, concurrency=8)
        with caplog.at_level(logging.WARNING, logger="app.services.ai.rate_gate"):
            gate.on_rate_limited("https://api.example/v1")
            caplog.clear()
            for _ in range(5):
                gate.on_rate_limited("https://api.example/v1")
            # 回拨时刻越过窗口，触发"窗口重开并回报抑制条数"路径
            for _slot in rg._LOG_THROTTLE_STATE.values():
                _slot[0] -= 120.0
            gate.on_rate_limited("https://api.example/v1")
        msgs = " ".join(r.getMessage() for r in caplog.records)
        assert "已抑制" in msgs, msgs
        rg.reset_log_throttle()


# ══════════════════════════════════════════════════════════════════════
# P1-B：ai_monitor 告警按类型冷却
# ══════════════════════════════════════════════════════════════════════
class TestAiMonitorPerTypeAlertCooldown:
    @staticmethod
    def _monitor(monkeypatch):
        from app.services import ai_monitor as m
        from app.services.ai_monitor import AICallMonitor, AlertConfig

        mon = AICallMonitor(AlertConfig(window_size=50, cooldown_seconds=300.0))
        seen = []
        mon.add_alert_callback = lambda cb: seen.append(cb)  # type: ignore[attr-defined]
        return mon

    def test_slow_request_does_not_mask_failure_rate_alert(self, monkeypatch):
        """一条慢请求告警不得吞掉随后 300s 内的失败率告警。"""
        from app.services.ai_monitor import AICallMonitor, AlertConfig

        mon = AICallMonitor(AlertConfig(window_size=10, cooldown_seconds=300.0))
        alerts = []
        mon._alert_callbacks = [lambda a: alerts.extend(a)]

        # 1) 先制造一条慢请求（info 级）
        mon.record_call("p", "m", True, 200.0, 0, "", "outline")
        assert any(a["type"] == "slow_request" for a in alerts)

        # 2) 紧接着制造高失败率：旧实现会被 300s 冷却静默
        alerts.clear()
        for _ in range(10):
            mon.record_call("p", "m", False, 1.0, 0, "boom", "outline")
        assert any(a["type"] == "high_failure_rate" for a in alerts), (
            "失败率告警被慢请求告警的冷却窗口吞掉了（历史缺陷回归）"
        )

    def test_same_type_still_respects_cooldown(self):
        """同类型告警仍受冷却约束，避免刷屏。"""
        from app.services.ai_monitor import AICallMonitor, AlertConfig

        mon = AICallMonitor(AlertConfig(window_size=10, cooldown_seconds=300.0))
        alerts = []
        mon._alert_callbacks = [lambda a: alerts.extend(a)]
        for _ in range(3):
            mon.record_call("p", "m", True, 200.0, 0, "", "outline")
        n = sum(1 for a in alerts if a["type"] == "slow_request")
        assert n == 1, f"同类型慢请求告警未受冷却约束，触发了 {n} 次"

    def test_last_alert_time_by_type_populated(self):
        from app.services.ai_monitor import AICallMonitor, AlertConfig

        mon = AICallMonitor(AlertConfig(window_size=10, cooldown_seconds=300.0))
        mon._alert_callbacks = [lambda a: None]
        for _ in range(10):
            mon.record_call("p", "m", False, 1.0, 0, "boom", "outline")
        assert "high_failure_rate" in mon._last_alert_time_by_type
        assert mon._last_alert_time > 0


# ══════════════════════════════════════════════════════════════════════
# P0-A：L2 失败章节补偿重跑 + 降级可观测
# ══════════════════════════════════════════════════════════════════════
class TestL2FailedChapterCompensationRetry:
    @staticmethod
    def _level1(n=4):
        return [{"title": f"第{i+1}章主题", "level": 1} for i in range(n)]

    def test_failed_chapters_are_retried_instead_of_falling_back(self, monkeypatch):
        """首轮全部失败、补偿轮成功 → 不得落默认目录，且 stats 记救回。"""
        from app.services.ai import workflows_outline_v51 as wo

        calls = {"n": 0}

        async def _fake_fill(provider, skeleton, l1_title, idx, summary, sp_text,
                             other_outline, **kwargs):
            calls["n"] += 1
            # 前 N 次（首轮）失败，之后（补偿轮）成功
            if calls["n"] <= 4:
                raise TimeoutError("目录生成AI调用超时(json_mode)（20秒）")
            return {
                "title": l1_title,
                "level": 1,
                "children": [
                    {"title": f"{l1_title}-AI二级A", "level": 2, "children": []},
                    {"title": f"{l1_title}-AI二级B", "level": 2, "children": []},
                ],
            }

        monkeypatch.setattr(wo, "_fill_level2_with_ai", _fake_fill)
        monkeypatch.setattr(wo, "OUTLINE_L2_RETRY_FAILED_ROUNDS", 1)
        monkeypatch.setattr(wo, "OUTLINE_L2_RETRY_CONCURRENCY", 2)

        stats: dict = {}
        level1 = asyncio.run(
            wo._generate_level2_v51(
                provider=None,
                level1=self._level1(4),
                summary="摘要",
                sp_text="评分点",
                stats=stats,
            )
        )
        # 首轮 4 次 + 补偿轮 4 次
        assert calls["n"] == 8, f"补偿重跑未执行，实际调用 {calls['n']} 次"
        assert stats["l2_total"] == 4
        assert stats["l2_degraded"] is False, "全部救回后不应仍标记降级"
        assert len(stats["l2_recovered_titles"]) == 4
        assert stats["l2_fallback_titles"] == []
        # 内容必须是 AI 结果，不是默认目录
        for l1 in level1:
            titles = [c.get("title", "") for c in l1.get("children", [])]
            assert any("-AI二级" in t for t in titles), titles

    def test_remaining_failures_still_fall_back_and_report(self, monkeypatch):
        """补偿也救不回的章节，仍落默认目录，但必须写进 stats 供上层告知用户。"""
        from app.services.ai import workflows_outline_v51 as wo

        async def _always_fail(provider, skeleton, l1_title, idx, summary, sp_text,
                               other_outline, **kwargs):
            raise TimeoutError("目录生成AI调用超时（60秒）")

        monkeypatch.setattr(wo, "_fill_level2_with_ai", _always_fail)
        monkeypatch.setattr(wo, "OUTLINE_L2_RETRY_FAILED_ROUNDS", 1)
        monkeypatch.setattr(wo, "OUTLINE_L2_RETRY_CONCURRENCY", 2)

        stats: dict = {}
        level1 = asyncio.run(
            wo._generate_level2_v51(
                provider=None,
                level1=self._level1(3),
                summary="摘要",
                sp_text="评分点",
                stats=stats,
            )
        )
        assert stats["l2_degraded"] is True
        assert len(stats["l2_fallback_titles"]) == 3
        assert stats["l2_retry_rounds"] == 1
        assert abs(stats["l2_degraded_ratio"] - 1.0) < 1e-6
        # 结构完整性：兜底后仍应有 children，不能是空骨架
        for l1 in level1:
            assert l1.get("children"), "兜底后章节不应为空"

    def test_rounds_zero_keeps_legacy_behavior(self, monkeypatch):
        """OUTLINE_L2_RETRY_FAILED_ROUNDS=0 → 不补跑，保持 2026-10-23 前行为。"""
        from app.services.ai import workflows_outline_v51 as wo

        calls = {"n": 0}

        async def _always_fail(provider, skeleton, l1_title, idx, summary, sp_text,
                               other_outline, **kwargs):
            calls["n"] += 1
            raise TimeoutError("超时")

        monkeypatch.setattr(wo, "_fill_level2_with_ai", _always_fail)
        monkeypatch.setattr(wo, "OUTLINE_L2_RETRY_FAILED_ROUNDS", 0)

        stats: dict = {}
        asyncio.run(
            wo._generate_level2_v51(
                provider=None,
                level1=self._level1(3),
                summary="摘要",
                sp_text="评分点",
                stats=stats,
            )
        )
        assert calls["n"] == 3, f"关闭补偿后应只调用 3 次，实际 {calls['n']}"
        assert stats["l2_retry_rounds"] == 0

    def test_degraded_ratio_emits_error_code(self, monkeypatch, caplog):
        """兜底占比超阈值时必须打出带 error_code 的 ERROR，供监控聚合。"""
        from app.services.ai import workflows_outline_v51 as wo

        async def _always_fail(provider, skeleton, l1_title, idx, summary, sp_text,
                               other_outline, **kwargs):
            raise TimeoutError("超时")

        monkeypatch.setattr(wo, "_fill_level2_with_ai", _always_fail)
        monkeypatch.setattr(wo, "OUTLINE_L2_RETRY_FAILED_ROUNDS", 0)
        monkeypatch.setattr(wo, "OUTLINE_DEGRADED_ALERT_RATIO", 0.3)

        with caplog.at_level(logging.ERROR, logger="app.services.ai.workflows_outline_v51"):
            asyncio.run(
                wo._generate_level2_v51(
                    provider=None,
                    level1=self._level1(3),
                    summary="摘要",
                    sp_text="评分点",
                    stats={},
                )
            )
        errs = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
        assert any("OUTLINE_L2_DEGRADED" in m for m in errs), errs

    def test_progress_counter_never_exceeds_total(self, monkeypatch):
        """补偿轮不得让进度计数越界（前端进度条靠它渲染）。"""
        from app.services.ai import workflows_outline_v51 as wo

        async def _always_fail(provider, skeleton, l1_title, idx, summary, sp_text,
                               other_outline, **kwargs):
            raise TimeoutError("超时")

        monkeypatch.setattr(wo, "_fill_level2_with_ai", _always_fail)
        monkeypatch.setattr(wo, "OUTLINE_L2_RETRY_FAILED_ROUNDS", 1)

        seen = []

        async def _pcb(done, total, msg):
            seen.append((done, total))

        asyncio.run(
            wo._generate_level2_v51(
                provider=None,
                level1=self._level1(3),
                summary="摘要",
                sp_text="评分点",
                progress_callback=_pcb,
                stats={},
            )
        )
        assert seen, "未推送任何进度事件"
        for done, total in seen:
            assert done <= total, f"进度越界: {done}/{total}"


# ══════════════════════════════════════════════════════════════════════
# P1-C：L1 硬上限截断不得静默丢弃评分项
# ══════════════════════════════════════════════════════════════════════
class TestL1CapMergesOverflowScoring:
    def test_dropped_chapters_are_merged_into_last(self, monkeypatch):
        """22 章截到 15 时，被截章节的评分要点必须并入末章 description。"""
        from app.services.ai import workflows_outline_v51 as wo

        monkeypatch.setattr(wo, "OUTLINE_L1_MAX_CHAPTERS", 3)
        level1 = [
            {"title": f"章{i}", "level": 1, "description": f"要点{i}"}
            for i in range(1, 6)
        ]
        kept, capped = wo._apply_l1_chapter_cap(level1)
        assert capped is True
        assert len(kept) == 3, "章节数仍受硬上限约束"
        desc = kept[-1].get("description", "")
        assert "硬上限归并" in desc, desc
        # 被截掉的第 4、5 章的要点必须可追溯
        assert "要点4" in desc and "要点5" in desc, desc
        assert kept[-1].get("merged_from_dropped") == ["章4", "章5"]

    def test_merge_is_idempotent(self, monkeypatch):
        """重复截断不得叠加归并文案。"""
        from app.services.ai import workflows_outline_v51 as wo

        monkeypatch.setattr(wo, "OUTLINE_L1_MAX_CHAPTERS", 3)
        level1 = [
            {"title": f"章{i}", "level": 1, "description": f"要点{i}"}
            for i in range(1, 6)
        ]
        kept, _ = wo._apply_l1_chapter_cap(level1)
        first_desc = kept[-1]["description"]
        kept2, _ = wo._apply_l1_chapter_cap(kept + [
            {"title": "章X", "level": 1, "description": "要点X"}
        ])
        assert kept2[-1]["description"] == first_desc

    def test_no_cap_means_untouched(self, monkeypatch):
        from app.services.ai import workflows_outline_v51 as wo

        monkeypatch.setattr(wo, "OUTLINE_L1_MAX_CHAPTERS", 0)
        level1 = [{"title": "a", "level": 1, "description": "d"}]
        kept, capped = wo._apply_l1_chapter_cap(level1)
        assert capped is False
        assert kept is level1


# ══════════════════════════════════════════════════════════════════════
# P2：迁移幂等 DEBUG 去重
# ══════════════════════════════════════════════════════════════════════
class TestMigrationDebugDedupe:
    def test_debug_once_emits_only_first(self):
        from app import db_migrations as dm

        dm.reset_debug_once_cache()
        assert dm._debug_once("s", "k", "msg %s", "a") is None
        dm._debug_once("s", "k", "msg %s", "b")
        dm._debug_once("s", "k", "msg %s", "c")
        assert len(dm._DEDUPED_DEBUG_KEYS) == 1
        dm.reset_debug_once_cache()

    def test_different_keys_are_independent(self):
        from app import db_migrations as dm

        dm.reset_debug_once_cache()
        dm._debug_once("s", "k1", "m")
        dm._debug_once("s", "k2", "m")
        assert len(dm._DEDUPED_DEBUG_KEYS) == 2
        dm.reset_debug_once_cache()

    def test_repeated_migration_call_does_not_flood(self, caplog):
        """同一 (site,key) 连续 20 次调用，DEBUG 只应落 1 条。"""
        from app import db_migrations as dm

        dm.reset_debug_once_cache()
        with caplog.at_level(logging.DEBUG, logger="app.db_migrations"):
            for _ in range(20):
                dm._debug_once("col_exists_table", "model_configs.image_base_url",
                               "%s.%s 列已存在", "model_configs", "image_base_url")
        n = sum(1 for r in caplog.records if "列已存在" in r.getMessage())
        assert n == 1, f"去重失效，仍打出 {n} 条"
        dm.reset_debug_once_cache()


# ══════════════════════════════════════════════════════════════════════
# 配置默认值与向后兼容
# ══════════════════════════════════════════════════════════════════════
class TestNewConfigDefaults:
    def test_defaults_are_conservative(self):
        from app import config

        # 补偿默认开启 1 轮（修复目标），但可通过 0 完全退回旧行为
        assert config.OUTLINE_L2_RETRY_FAILED_ROUNDS == 1
        assert config.OUTLINE_L2_RETRY_CONCURRENCY >= 1
        # 并发上限默认仍是 0（= provider 自动），不改变既有吞吐
        assert config.OUTLINE_L2_CONCURRENCY == 0
        assert 0.0 <= config.OUTLINE_DEGRADED_ALERT_RATIO <= 1.0

    def test_new_config_names_exist(self):
        from app import config

        for name in (
            "OUTLINE_L2_RETRY_FAILED_ROUNDS",
            "OUTLINE_L2_RETRY_CONCURRENCY",
            "OUTLINE_DEGRADED_ALERT_RATIO",
        ):
            assert hasattr(config, name), name
