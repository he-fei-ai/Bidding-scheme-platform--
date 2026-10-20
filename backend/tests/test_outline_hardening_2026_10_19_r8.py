"""2026-10-19 生成大纲模块第八轮深度加固回归。

对应「报告_生成大纲模块第八轮深度加固_2026-10-19」§5 修复项：

  BUG-OUTLINE-08a（P1）：`_ai_deep_check` 只接受 dict 响应，AI 把
       `{"issues": [...]}` 省略包装直接返回问题数组时（日志实证 25 次
       「返回结构无法识别: list」），语义审结论被整体丢弃、门禁只剩
       确定性结论。修复：顶层 list 且含 dict 项时按 issues 采纳；
       空列表视为"无问题"；非空但全非 dict 仍判结构不可识别。

  BUG-OUTLINE-08b（P1）：`collect_json_response` 内层 with_timeout 硬编码
       20s(json_mode)/30s(降级)，与 provider 自身 max_tokens 自适应超时
       （>=8000 tokens→120s）冲突——慢模型生成 L1 目录（8192 tokens）被
       外层提前熔断，整个任务失败（日志实证 3 连败 project=1871130）。
       修复：超时配置化（AI_JSON_MODE_TIMEOUT/AI_CALL_TIMEOUT）+ 按
       max_tokens 自适应（_content_call_timeout）+ 降级超时错误合并
       两次事实（不再用 json_mode 超时掩盖降级失败）。

  BUG-OUTLINE-08c（P3）：`_fill_level2_with_ai` 尾部不可达分支的
       chr() 混淆字符串改回可读文案（解码后为
       "L2 fill failed with no captured exception"）。

运行：cd backend && python -m pytest tests/test_outline_hardening_2026_10_19_r8.py -q
"""

from __future__ import annotations

import json
import re
import time

import pytest

from app.config import AI_CALL_TIMEOUT, AI_JSON_MODE_TIMEOUT
from app.services.ai.providers.base import AIMessage


def _gate_outline() -> list[dict]:
    return [
        {
            "id": "1",
            "title": "项目概述",
            "level": 1,
            "children": [
                {"id": "1.1", "title": "项目背景", "level": 2, "children": [
                    {"id": "1.1.1", "title": "项目由来", "level": 3, "children": []},
                    {"id": "1.1.2", "title": "建设规模", "level": 3, "children": []},
                ]},
                {"id": "1.2", "title": "编制依据", "level": 2, "children": []},
            ],
        },
        {
            "id": "2",
            "title": "施工部署",
            "level": 1,
            "children": [
                {"id": "2.1", "title": "总体部署", "level": 2, "children": [
                    {"id": "2.1.1", "title": "部署原则", "level": 3, "children": []},
                ]},
            ],
        },
    ]


# ===========================================================================
# BUG-OUTLINE-08a：_ai_deep_check 容忍裸 issues 数组
# ===========================================================================


class TestAiDeepCheckListTolerance:
    """AI 自检返回结构容忍（日志实证 25 次裸 list 被整体丢弃）。"""

    @pytest.mark.asyncio
    async def test_bare_issues_list_accepted(self, monkeypatch):
        from app.services.ai import workflows_outline_v51 as w

        async def _fake(*_a, **_k):
            return [{"type": "generic_l3", "node_id": "1.1.1", "fix": "注入技术含量"}]

        monkeypatch.setattr(w, "_call_ai_v51", _fake)
        issues, error = await w._ai_deep_check(object(), _gate_outline())

        assert error is None, "裸 issues 数组不应判为结构错误"
        assert issues, "数组中的问题项必须被映射采纳"
        assert issues[0]["category"] == "generic_l3"
        assert issues[0]["source"] == "ai"
        assert issues[0]["severity"] and issues[0]["repair"] == "注入技术含量"

    @pytest.mark.asyncio
    async def test_empty_list_means_no_issues(self, monkeypatch):
        from app.services.ai import workflows_outline_v51 as w

        async def _fake(*_a, **_k):
            return []

        monkeypatch.setattr(w, "_call_ai_v51", _fake)
        issues, error = await w._ai_deep_check(object(), _gate_outline())

        assert error is None
        assert issues == [], "空列表 = AI 明确表示无问题"

    @pytest.mark.asyncio
    async def test_garbage_list_still_rejected(self, monkeypatch):
        """非空但全是非 dict 的跑题输出，仍判结构不可识别（不把垃圾当结论）。"""
        from app.services.ai import workflows_outline_v51 as w

        async def _fake(*_a, **_k):
            return ["这不是字典", 42, None]

        monkeypatch.setattr(w, "_call_ai_v51", _fake)
        issues, error = await w._ai_deep_check(object(), _gate_outline())

        assert issues is None
        assert error, "跑题列表必须给出错误信息（供裁决降级为仅确定性结论）"

    @pytest.mark.asyncio
    async def test_mixed_list_keeps_dict_items(self, monkeypatch):
        from app.services.ai import workflows_outline_v51 as w

        async def _fake(*_a, **_k):
            return ["噪声", {"type": "overlap", "nodes": ["1.1.1", "2.1.1"], "fix": "区分侧重点"}]

        monkeypatch.setattr(w, "_call_ai_v51", _fake)
        issues, error = await w._ai_deep_check(object(), _gate_outline())

        assert error is None
        assert len(issues) == 1
        assert issues[0]["category"] == "overlap"
        assert issues[0]["node_ids"] == ["1.1.1", "2.1.1"]

    @pytest.mark.asyncio
    async def test_dict_wrapper_still_accepted(self, monkeypatch):
        """既有 {"issues": [...]} 包装行为不变。"""
        from app.services.ai import workflows_outline_v51 as w

        async def _fake(*_a, **_k):
            return {"issues": [{"type": "mixed_lang", "target": "1.1.1", "fix": "替换为中文"}]}

        monkeypatch.setattr(w, "_call_ai_v51", _fake)
        issues, error = await w._ai_deep_check(object(), _gate_outline())

        assert error is None
        assert issues and issues[0]["category"] == "mixed_lang"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad", ["string", 42, None, 3.14])
    async def test_non_dict_non_list_rejected(self, bad, monkeypatch):
        from app.services.ai import workflows_outline_v51 as w

        async def _fake(*_a, **_k):
            return bad

        monkeypatch.setattr(w, "_call_ai_v51", _fake)
        issues, error = await w._ai_deep_check(object(), _gate_outline())

        assert issues is None
        assert error == "AI 返回结构无法识别"


# ===========================================================================
# BUG-OUTLINE-08b：AI 调用超时配置化 + 自适应
# ===========================================================================


class TestContentCallTimeout:
    """内容调用超时与 provider 自身 max_tokens 口径对齐。"""

    def test_defaults(self):
        assert AI_JSON_MODE_TIMEOUT == 20, "json_mode 探测保持快速失败（向后兼容）"
        assert AI_CALL_TIMEOUT == 60, "内容调用默认 60s（原硬编码 30s 过短）"

    def test_scaling_by_max_tokens(self):
        from app.services.ai.workflows_base import _content_call_timeout

        assert _content_call_timeout(None) == AI_CALL_TIMEOUT
        assert _content_call_timeout(0) == AI_CALL_TIMEOUT
        assert _content_call_timeout(4096) == AI_CALL_TIMEOUT
        assert _content_call_timeout(7999) == AI_CALL_TIMEOUT
        # >=8000 tokens：provider 自身超时抬到 120s，外层不得更短
        assert _content_call_timeout(8000) == 120
        assert _content_call_timeout(8192) == 120
        # >=16000 tokens：provider 自身 180s
        assert _content_call_timeout(16000) == 180
        assert _content_call_timeout(60000) == 180

    def test_bad_input_falls_back(self):
        from app.services.ai.workflows_base import _content_call_timeout

        assert _content_call_timeout("not-a-number") == AI_CALL_TIMEOUT
        assert _content_call_timeout([1, 2]) == AI_CALL_TIMEOUT

    def test_never_below_config_floor(self, monkeypatch):
        """配置调大后自适应值只能更高，不会被 provider 口径压低。"""
        from app.services.ai import workflows_base as wb

        monkeypatch.setattr(wb, "AI_CALL_TIMEOUT", 200)
        assert wb._content_call_timeout(4096) == 200
        assert wb._content_call_timeout(8192) == 200
        assert wb._content_call_timeout(20000) == 200


class _FakeProvider:
    """最小 provider 替身：始终返回合法 JSON。"""

    model = "fake-model"
    base_url = "http://fake.local/v1"

    async def chat(self, messages, **options):
        return '{"ok": true}'


class TestCollectJsonResponseTimeouts:
    """collect_json_response 实际下发的超时值走配置。"""

    @pytest.fixture(autouse=True)
    def _clean_json_mode_caches(self):
        """清理 json_mode 降级/超时缓存，避免跨用例泄漏（缓存键为
        (base_url, model) 元组，30s backoff 内会跳过 json_mode 探测）。"""
        from app.services.ai import workflows_base as wb

        key = (_FakeProvider.base_url, _FakeProvider.model)
        wb._json_mode_unsupported.pop(key, None)
        wb._json_mode_timeout_cache.pop(key, None)
        yield
        wb._json_mode_unsupported.pop(key, None)
        wb._json_mode_timeout_cache.pop(key, None)

    @pytest.mark.asyncio
    async def test_json_mode_probe_uses_config(self, monkeypatch):
        from app.services.ai import workflows_base as wb

        seen: list[tuple[int, str]] = []

        async def _fake_with_timeout(coro, timeout=30, error_msg="AI调用超时"):
            seen.append((timeout, error_msg))
            try:
                return await coro
            finally:
                pass

        monkeypatch.setattr(wb, "with_timeout", _fake_with_timeout)
        result = await wb.collect_json_response(
            _FakeProvider(),
            [AIMessage(role="user", content="hi")],
            max_tokens=8192,
        )

        assert result == {"ok": True}
        assert seen, "至少应发生一次 AI 调用"
        # 首次即 json_mode 探测：用 AI_JSON_MODE_TIMEOUT，与 max_tokens 无关
        assert seen[0][0] == AI_JSON_MODE_TIMEOUT
        assert "json_mode" in seen[0][1]

    @pytest.mark.asyncio
    async def test_content_call_uses_adaptive_timeout(self, monkeypatch):
        """跳过 json_mode（标记不支持）后，内容调用按 max_tokens 取自适应超时。"""
        from app.services.ai import workflows_base as wb

        # _provider_json_mode_key 返回 (base_url, model) 元组
        key = (_FakeProvider.base_url, _FakeProvider.model)
        seen: list[tuple[int, str]] = []

        async def _fake_with_timeout(coro, timeout=30, error_msg="AI调用超时"):
            seen.append((timeout, error_msg))
            return await coro

        monkeypatch.setattr(wb, "with_timeout", _fake_with_timeout)
        # 预置 json_mode 不支持标记（backoff 期内直接走内容调用）
        wb._json_mode_unsupported[key] = time.time()
        try:
            result = await wb.collect_json_response(
                _FakeProvider(),
                [AIMessage(role="user", content="hi")],
                max_tokens=8192,
            )
        finally:
            wb._json_mode_unsupported.pop(key, None)

        assert result == {"ok": True}
        assert seen and seen[0][0] == 120, "8192 tokens 的内容调用应为 120s"
        assert "json_mode" not in seen[0][1]

    @pytest.mark.asyncio
    async def test_degraded_timeout_merges_both_facts(self, monkeypatch):
        """json_mode 超时 + 降级也超时：错误信息必须合并两次事实。

        修复前直接 raise json_mode 那次超时，信息只提 json_mode 20s，
        掩盖降级调用也超时——运维按「json_mode 超时」排查找不到根因。
        """
        from app.services.ai import workflows_base as wb

        async def _always_timeout(coro, timeout=30, error_msg="AI调用超时"):
            coro.close()  # 避免 never-awaited 警告
            raise TimeoutError(f"{error_msg}（{timeout}秒）")

        monkeypatch.setattr(wb, "with_timeout", _always_timeout)
        with pytest.raises(TimeoutError) as exc_info:
            await wb.collect_json_response(
                _FakeProvider(),
                [AIMessage(role="user", content="hi")],
                max_tokens=4096,
            )

        msg = str(exc_info.value)
        assert "json_mode" in msg, "应保留 json_mode 超时事实"
        assert "降级" in msg, "必须点明降级非 json_mode 重试也失败"
        assert "超时" in msg, "保留「超时」字样使上层兜底提供商降级链照旧触发"

    @pytest.mark.asyncio
    async def test_degraded_non_timeout_error_surfaced(self, monkeypatch):
        """降级调用自身失败（连接错误等）：真实原因不被 json_mode 超时顶替。"""
        from app.services.ai import workflows_base as wb

        calls = {"n": 0}

        async def _fake_with_timeout(coro, timeout=30, error_msg="AI调用超时"):
            calls["n"] += 1
            coro.close()
            if calls["n"] == 1:
                raise TimeoutError(f"{error_msg}（{timeout}秒）")
            raise ConnectionError("connection reset by peer")

        monkeypatch.setattr(wb, "with_timeout", _fake_with_timeout)
        with pytest.raises(TimeoutError) as exc_info:
            await wb.collect_json_response(
                _FakeProvider(),
                [AIMessage(role="user", content="hi")],
                max_tokens=4096,
            )

        msg = str(exc_info.value)
        assert "connection reset by peer" in msg, "降级真实失败原因必须出现在错误信息里"


# ===========================================================================
# BUG-OUTLINE-08c：chr() 混淆串清理
# ===========================================================================


class TestNoObfuscatedStrings:
    def test_no_chr_chain_in_outline_workflow(self):
        from pathlib import Path

        src = (
            Path(__file__).resolve().parent.parent
            / "app/services/ai/workflows_outline_v51.py"
        ).read_text(encoding="utf-8")
        assert not re.search(r"chr\(\d+\)", src), (
            "workflows_outline_v51.py 不应再包含 ASCII 码逐字符拼接的混淆串"
            "（原 _fill_level2_with_ai 尾部不可达分支，BUG-OUTLINE-08c）"
        )

    def test_l2_fill_failure_message_readable(self):
        from pathlib import Path

        src = (
            Path(__file__).resolve().parent.parent
            / "app/services/ai/workflows_outline_v51.py"
        ).read_text(encoding="utf-8")
        assert "L2 fill failed with no captured exception" in src


# ===========================================================================
# BUG-OUTLINE-08d：L1 对 provider 级错误（超时/网络）的降级路径
# ===========================================================================


_SCORING_ITEMS_JSON = json.dumps(
    [
        {"id": "R1", "name": "施工组织设计", "weight": "30"},
        {"id": "R2", "name": "质量保证措施", "weight": "25"},
        {"id": "R3", "name": "安全文明施工", "weight": "20"},
    ],
    ensure_ascii=False,
)


class TestLevel1ProviderErrorDegradation:
    """L1 是唯一没有按章降级的环节；超时/网络错误应走评分项兜底而非杀死任务。

    生产实证：project=1871130 三次生成全部止步 L1（AI 调用超时直接穿透
    except ValueError），日志只有一行「目录生成AI调用超时(json_mode)（20秒）」。
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "err",
        [
            TimeoutError("目录生成AI调用超时(json_mode)（20秒）；降级非 json_mode 重试仍超时（120秒）"),
            RuntimeError("请求超时（120秒）: ReadTimeout"),
            RuntimeError("网络错误: Connection reset by peer"),
        ],
    )
    async def test_transient_error_falls_back_to_scoring(self, err, monkeypatch):
        from app.services.ai import workflows_outline_v51 as w

        calls = {"n": 0}

        async def _fake_call(*_a, **_k):
            calls["n"] += 1
            raise err

        monkeypatch.setattr(w, "_call_ai_v51", _fake_call)
        level1 = await w._generate_level1_v51(
            provider=object(),
            summary="某项目技术标",
            sp_text=_SCORING_ITEMS_JSON,
            reference_json="[]",
        )

        assert calls["n"] == 1, "瞬态 provider 错误不应消耗格式重试预算"
        titles = [n["title"] for n in level1]
        assert "施工组织设计" in titles and "质量保证措施" in titles
        assert all("children" in n for n in level1)

    @pytest.mark.asyncio
    async def test_fallback_emits_progress_event(self, monkeypatch):
        from app.services.ai import workflows_outline_v51 as w

        async def _fake_call(*_a, **_k):
            raise TimeoutError("目录生成AI调用超时(json_mode)（20秒）")

        monkeypatch.setattr(w, "_call_ai_v51", _fake_call)
        events: list[tuple[str, str]] = []

        async def _phase_cb(phase, message):
            events.append((phase, message))

        level1 = await w._generate_level1_v51(
            provider=object(),
            summary="某项目技术标",
            sp_text=_SCORING_ITEMS_JSON,
            reference_json="[]",
            phase_callback=_phase_cb,
        )

        assert level1, "兜底应产出目录"
        assert any("超时" in msg for _p, msg in events), (
            "降级时必须推送可见事件，不能让用户面对无进展的等待"
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "err",
        [
            RuntimeError("API Key 无效或未授权 (HTTP 401)"),
            RuntimeError("API 配额已耗尽，请检查账户余额和套餐 (HTTP 402)"),
            RuntimeError("模型不存在 (HTTP 404)"),
        ],
    )
    async def test_config_errors_reraised(self, err, monkeypatch):
        """配置类错误原样上抛：换 Key / 配兜底提供商才是正解，
        静默降级成规则目录会把「AI 没配」伪装成「生成成功」。"""
        from app.services.ai import workflows_outline_v51 as w

        async def _fake_call(*_a, **_k):
            raise err

        monkeypatch.setattr(w, "_call_ai_v51", _fake_call)
        with pytest.raises(type(err)):
            await w._generate_level1_v51(
                provider=object(),
                summary="某项目技术标",
                sp_text=_SCORING_ITEMS_JSON,
                reference_json="[]",
            )

    @pytest.mark.asyncio
    async def test_value_error_path_still_retries_then_falls_back(self, monkeypatch):
        """既有 ValueError 路径行为不变：3 次尝试后走评分项兜底。"""
        from app.services.ai import workflows_outline_v51 as w

        calls = {"n": 0}

        async def _fake_call(*_a, **_k):
            calls["n"] += 1
            raise ValueError("AI 返回格式无法识别: str")

        monkeypatch.setattr(w, "_call_ai_v51", _fake_call)
        level1 = await w._generate_level1_v51(
            provider=object(),
            summary="某项目技术标",
            sp_text=_SCORING_ITEMS_JSON,
            reference_json="[]",
        )

        assert calls["n"] == 3, "格式错误保持 3 次重试（带修正提示）"
        assert level1, "重试耗尽后仍应有评分项兜底"


# ===========================================================================
# 配置环境变量覆盖
# ===========================================================================


class TestTimeoutConfigEnvOverride:
    def test_env_override(self, monkeypatch):
        import importlib

        import app.config as cfg

        monkeypatch.setenv("AI_JSON_MODE_TIMEOUT", "12")
        monkeypatch.setenv("AI_CALL_TIMEOUT", "45")
        reloaded = importlib.reload(cfg)
        try:
            assert reloaded.AI_JSON_MODE_TIMEOUT == 12
            assert reloaded.AI_CALL_TIMEOUT == 45
        finally:
            monkeypatch.delenv("AI_JSON_MODE_TIMEOUT")
            monkeypatch.delenv("AI_CALL_TIMEOUT")
            importlib.reload(cfg)
        assert cfg.AI_JSON_MODE_TIMEOUT == 20
        assert cfg.AI_CALL_TIMEOUT == 60

    def test_floor_clamp(self, monkeypatch):
        """非法/过小值被兜底下限钳制，不会把超时打成 0 造成立即熔断。"""
        import importlib

        import app.config as cfg

        monkeypatch.setenv("AI_JSON_MODE_TIMEOUT", "0")
        monkeypatch.setenv("AI_CALL_TIMEOUT", "-5")
        reloaded = importlib.reload(cfg)
        try:
            assert reloaded.AI_JSON_MODE_TIMEOUT >= 5
            assert reloaded.AI_CALL_TIMEOUT >= 10
        finally:
            monkeypatch.delenv("AI_JSON_MODE_TIMEOUT")
            monkeypatch.delenv("AI_CALL_TIMEOUT")
            importlib.reload(cfg)
