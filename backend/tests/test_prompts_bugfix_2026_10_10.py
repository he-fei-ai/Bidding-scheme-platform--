"""提示词模块第三轮缺陷回归。

覆盖本轮定位并修复的缺陷，每项均对应可复现的失败场景：

P1 正确性 / 优先级契约
  - PROMPT-C8  assemble_prompt_context 把 head/marker 预算扣了两次，
    边界片段必然「放不下」而被**静默整体丢弃**；实测 GLOBAL_FACTS
    （DEFAULT_PRIORITIES 中声明的最高优先「数据一致性铁律」）产出空串。
    与「低优先级先丢」的设计契约相反。

P1 监控可用性
  - PROMPT-B2  月度预算为 0 时 check_prompt_alerts / suggest_next_month_budget
    抛 ZeroDivisionError（监控看板与预算建议 500）。
  - PROMPT-B2b set_monthly_budget / set_alert_thresholds 无入参校验，
    负数/越界值被静默写入并持久化，使告警规则永久失效。

P2 部署健壮性
  - PROMPT-B4  监控配置/报表路径为 CWD 相对路径，随启动目录漂移，
    导致「改了预算重启后又变回默认值」。
"""

from __future__ import annotations

import json
import os

import pytest


# ══════════════════════════════════════════════════════════════════════════
# PROMPT-C8  上下文组装：边界片段被静默丢弃
# ══════════════════════════════════════════════════════════════════════════

class TestContextAssemblyBudgetAccounting:
    def test_top_priority_fragment_never_dropped(self):
        """回归 PROMPT-C8：修复前 max_tokens=25 时 GLOBAL_FACTS 产出空串。

        根因：调用方先扣掉 head_cost/marker_cost 得到 body_budget，而
        _fit_to_token_budget 内部比对的是 head+content+marker 的**整块**成本，
        头部与标记被扣两次 -> 任何边界片段都「放不下」-> 返回 "" -> 整段丢弃。
        """
        from app.services.ai.prompts import assemble_prompt_context

        parts = {
            "GLOBAL_FACTS": "高优先级事实内容" * 3,
            "SUMMARY": "中优先级概述内容" * 3,
            "OTHER_CHAPTERS": "低优先级其它章节内容" * 3,
        }
        out = assemble_prompt_context(parts, max_tokens=25)

        assert out, "最高优先级片段 GLOBAL_FACTS 被整体丢弃"
        assert "【GLOBAL_FACTS】" in out
        # 低优先级片段仍应被整体丢弃（优先级契约不变）
        assert "【OTHER_CHAPTERS】" not in out

    def test_boundary_fragment_truncated_with_marker(self):
        """回归 PROMPT-C8：边界片段应「部分截断」而非「整段丢弃」。"""
        from app.services.ai.prompts import assemble_prompt_context

        parts = {"GLOBAL_FACTS": "事实A", "SUMMARY": "概述" * 50}
        out = assemble_prompt_context(parts, max_tokens=30)

        assert "【GLOBAL_FACTS】" in out
        assert "【SUMMARY】" in out, "边界片段被整段丢弃而非截断"
        assert "因 Token 预算已截断" in out

    def test_truncated_boundary_still_never_exceeds_budget(self):
        """修复不得牺牲预算硬上限：截断后仍须落在 max_tokens 内。"""
        from app.services.ai.prompts import assemble_prompt_context, estimate_tokens

        for parts, cap in [
            ({"GLOBAL_FACTS": "高优先级事实内容" * 3, "SUMMARY": "概述" * 200}, 25),
            ({"GLOBAL_FACTS": "事实A", "SUMMARY": "概述" * 50}, 30),
            ({"GLOBAL_FACTS": "招标全文" * 60000, "SUMMARY": "摘要" * 50}, 20000),
            ({"GLOBAL_FACTS": "abcdefghij " * 20000, "SUMMARY": "x" * 100}, 5000),
        ]:
            out = assemble_prompt_context(parts, max_tokens=cap)
            assert estimate_tokens(out) <= cap, (
                f"超预算: {estimate_tokens(out)} > {cap}"
            )

    def test_head_and_marker_charged_exactly_once(self):
        """口径断言：head/marker 只扣一次，产出应贴近预算上限而非显著缩水。

        修复前预算被重复扣费，可用正文预算恒为负 -> 产出恒为空；
        修复后应把剩余预算用满（留出 head+marker 的空间）。
        """
        from app.services.ai.prompts import assemble_prompt_context, estimate_tokens

        cap = 200
        out = assemble_prompt_context({"GLOBAL_FACTS": "招标全文" * 5000}, max_tokens=cap)
        used = estimate_tokens(out)
        assert out
        # 允许少量收敛误差，但不应远低于预算
        assert used > cap * 0.9, f"预算利用率过低（{used}/{cap}），可能重复扣费"


# ══════════════════════════════════════════════════════════════════════════
# PROMPT-B2  月度预算为 0 -> 除零崩溃
# ══════════════════════════════════════════════════════════════════════════

class TestBudgetZeroDivision:
    @pytest.fixture(autouse=True)
    def _restore_budget(self):
        from app.services.ai.prompts import _cache

        old = _cache.MONTHLY_BUDGET_CNY
        _cache.reset_prompt_metrics()
        yield
        _cache.MONTHLY_BUDGET_CNY = old
        _cache.reset_prompt_metrics()

    def test_check_alerts_survives_zero_budget(self, monkeypatch):
        """回归 PROMPT-B2：修复前 ZeroDivisionError（/prompts/alerts 500）。"""
        from app.services.ai.prompts import _cache

        monkeypatch.setattr(_cache, "MONTHLY_BUDGET_CNY", 0.0)
        with _cache._metrics_lock:
            m = _cache._new_metrics_entry()
            m.update({"calls": 10, "total_tokens": 1_000_000})
            _cache._prompt_metrics["X"] = m

        alerts = _cache.check_prompt_alerts()  # 不应抛异常
        assert isinstance(alerts, list)
        budget_alerts = [a for a in alerts if a["type"] == "budget_exceeded"]
        assert budget_alerts, "成本超预算时应产生告警"

    def test_suggest_budget_survives_zero_budget(self, monkeypatch):
        """回归 PROMPT-B2：修复前 ZeroDivisionError（/budget/suggest 500）。"""
        from app.services.ai.prompts import _cache

        monkeypatch.setattr(_cache, "MONTHLY_BUDGET_CNY", 0.0)
        result = _cache.suggest_next_month_budget()
        assert result["current_budget"] == 0.0
        assert result["increase_ratio"] is None


# ══════════════════════════════════════════════════════════════════════════
# PROMPT-B2b  配置入参校验
# ══════════════════════════════════════════════════════════════════════════

class TestConfigValidation:
    @pytest.fixture(autouse=True)
    def _restore(self, monkeypatch):
        from app.services.ai.prompts import _cache

        old = (
            _cache.MONTHLY_BUDGET_CNY,
            _cache.DB_HIT_RATIO_WARN_THRESHOLD,
            _cache.CALL_COUNT_SPIKE_MULTIPLIER,
            _cache.CALL_COUNT_SPIKE_MIN_BASELINE,
        )
        yield
        (
            _cache.MONTHLY_BUDGET_CNY,
            _cache.DB_HIT_RATIO_WARN_THRESHOLD,
            _cache.CALL_COUNT_SPIKE_MULTIPLIER,
            _cache.CALL_COUNT_SPIKE_MIN_BASELINE,
        ) = old

    @pytest.mark.parametrize("bad", [0, 0.0, -1, -50.0, float("nan"), float("inf"), "abc", None])
    def test_reject_invalid_budget(self, bad, monkeypatch):
        """回归 PROMPT-B2b：非法预算原会被静默写入并持久化。"""
        from app.services.ai.prompts import _cache

        monkeypatch.setattr(_cache, "_save_config_to_file", lambda: None)
        with pytest.raises(ValueError):
            _cache.set_monthly_budget(bad)

    def test_accept_valid_budget(self, monkeypatch):
        from app.services.ai.prompts import _cache

        monkeypatch.setattr(_cache, "_save_config_to_file", lambda: None)
        _cache.set_monthly_budget(88.5)
        assert _cache.get_monthly_budget() == 88.5

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"db_hit_threshold": 5.0},
            {"db_hit_threshold": -0.1},
            {"spike_multiplier": 0},
            {"spike_multiplier": 0.5},
            {"spike_min_baseline": -1},
        ],
    )
    def test_reject_invalid_thresholds(self, kwargs, monkeypatch):
        """回归 PROMPT-B2b：越界阈值会被持久化，使告警规则永久失效。"""
        from app.services.ai.prompts import _cache

        monkeypatch.setattr(_cache, "_save_config_to_file", lambda: None)
        with pytest.raises(ValueError):
            _cache.set_alert_thresholds(**kwargs)

    def test_accept_valid_thresholds(self, monkeypatch):
        from app.services.ai.prompts import _cache

        monkeypatch.setattr(_cache, "_save_config_to_file", lambda: None)
        _cache.set_alert_thresholds(
            db_hit_threshold=0.5, spike_multiplier=3.0, spike_min_baseline=5
        )
        got = _cache.get_alert_thresholds()
        assert got["db_hit_threshold"] == 0.5
        assert got["spike_multiplier"] == 3.0
        assert got["spike_min_baseline"] == 5

    def test_router_returns_400_for_invalid_budget(self):
        """API 层：用户输入错误应返回 400，而非 500。"""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from app.routers import prompts

        app = FastAPI()
        app.include_router(prompts.router)
        client = TestClient(app, raise_server_exceptions=False)

        for amount in (0, -1):
            resp = client.put("/prompts/budget", json={"amount": amount})
            assert resp.status_code == 400, f"amount={amount} -> {resp.status_code} {resp.text[:200]}"

    def test_router_returns_400_for_invalid_threshold(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from app.routers import prompts

        app = FastAPI()
        app.include_router(prompts.router)
        client = TestClient(app, raise_server_exceptions=False)

        resp = client.put("/prompts/alerts/thresholds", json={"spike_multiplier": 0})
        assert resp.status_code == 400, resp.text[:200]


# ══════════════════════════════════════════════════════════════════════════
# PROMPT-B4  配置/报表路径随 CWD 漂移
# ══════════════════════════════════════════════════════════════════════════

class TestConfigPathStability:
    def test_config_file_is_absolute(self):
        """回归 PROMPT-B4：原为相对 CWD 的 "logs/..."。"""
        from app.services.ai.prompts import _cache

        assert os.path.isabs(_cache._CONFIG_FILE), _cache._CONFIG_FILE

    def test_config_path_independent_of_cwd(self, tmp_path, monkeypatch):
        """核心断言：从两个不同 CWD 解析出的配置路径必须一致。"""
        from app.services.ai.prompts import _cache

        resolved = []
        for cwd in (tmp_path, tmp_path / "sub"):
            cwd.mkdir(parents=True, exist_ok=True)
            monkeypatch.chdir(cwd)
            resolved.append(_cache._resolve_config_path())
        assert resolved[0] == resolved[1]

    def test_saved_config_survives_cwd_change(self, tmp_path, monkeypatch):
        """改了预算后切换工作目录，配置仍应可被读回（不丢配置）。"""
        from app.services.ai.prompts import _cache

        cfg = tmp_path / "cfg.json"
        monkeypatch.setattr(_cache, "_CONFIG_FILE", str(cfg))

        _cache.set_monthly_budget(123.0)
        assert cfg.exists()

        _cache.MONTHLY_BUDGET_CNY = 100.0
        _cache._load_saved_config()
        assert _cache.get_monthly_budget() == 123.0

    def test_report_dir_is_anchored(self, tmp_path, monkeypatch):
        """回归 PROMPT-B4：报表默认输出目录不应随 CWD 变化。"""
        from app.services.ai.prompts import _cache

        seen = []

        def _fake_history(hours=24):
            return []

        monkeypatch.chdir(tmp_path)
        out = _cache.generate_monthly_report()
        seen.append(out)
        assert os.path.isabs(out), out
        assert out.startswith(str(_cache._BACKEND_ROOT)), out

    def test_load_saved_config_rejects_nonpositive_budget(self, tmp_path, monkeypatch):
        """历史上已写坏的配置（预算 0）不应再把内存带进除零状态。"""
        from app.services.ai.prompts import _cache

        cfg = tmp_path / "bad.json"
        cfg.write_text(json.dumps({"monthly_budget": 0}), encoding="utf-8")
        monkeypatch.setattr(_cache, "_CONFIG_FILE", str(cfg))
        monkeypatch.setattr(_cache, "MONTHLY_BUDGET_CNY", 42.0)

        _cache._load_saved_config()
        assert _cache.get_monthly_budget() > 0

    def test_legacy_cwd_relative_config_is_still_read(self, tmp_path, monkeypatch):
        """向后兼容：新路径不存在而旧 CWD 相对路径存在时，沿用旧配置。"""
        from app.services.ai.prompts import _cache

        monkeypatch.chdir(tmp_path)
        (tmp_path / "logs").mkdir()
        legacy = tmp_path / "logs" / "prompt_monitoring_config.json"
        legacy.write_text(json.dumps({"monthly_budget": 77.0}), encoding="utf-8")
        monkeypatch.setattr(
            _cache, "_CONFIG_FILE", str(tmp_path / "new" / "prompt_monitoring_config.json")
        )

        _cache.MONTHLY_BUDGET_CNY = 100.0
        _cache._load_saved_config()
        assert _cache.get_monthly_budget() == 77.0


# ══════════════════════════════════════════════════════════════════════════
# PROMPT-D1  解析提取降级返回空串 -> 提示词数据链静默为空
# ══════════════════════════════════════════════════════════════════════════

class TestExtractionDegradationNotSilentEmpty:
    """守卫命中时不得返回「成功但空正文」，否则整条提示词上下文为空。

    提示词模块的所有上下文（GLOBAL_FACTS / SUMMARY / SCORING_POINTS / OUTLINE）
    都源自解析提取结果。主文件被判为「成功但空」时，is_extraction_notice('') 为
    False -> status='success'，全流程无感地产出基于空上下文的方案。
    """

    def test_doc_outside_projects_dir_returns_notice_not_empty(self, tmp_path, monkeypatch):
        import shutil
        import sys

        from app.services import files as files_mod

        # Git for Windows 自带 antiword，故该命令行分支在本机必然可达；
        # 守卫命中时应跳过并落到降级提示，而非提前 return ""。
        if not shutil.which("antiword"):
            pytest.skip("antiword 未安装，无法覆盖该分支")

        p = tmp_path / "x.doc"
        p.write_bytes(b"\xd0\xcf\x11\xe0fake")
        monkeypatch.setitem(sys.modules, "olefile", None)

        out = files_mod._extract_doc(str(p))
        assert out != "", "守卫命中却返回空串（会被判为解析成功但正文为空）"
        assert out in files_mod.EXTRACTION_NOTICES, out

    def test_empty_result_is_not_treated_as_success(self):
        """空串不得被判为「已解析」，否则下游拿不到任何上下文。"""
        from app.services import files as files_mod

        assert files_mod.is_extraction_notice("") is False  # 现状：空串确实不是提示串
        # 因此生产代码必须避免在守卫分支返回 ""；上面的用例即为此护栏。
        assert files_mod.is_extraction_notice(files_mod.EXTRACT_DOC_NOTICE) is True