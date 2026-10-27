"""直出/分步方案选择增加「模型最大输出 token」维度回归测试（2026-10-23）。

对齐 yibiao-simple 文档《AI 标书提纲生成工作流程》方案选择依据：除评分点规模外，
还应考量「模型最大输出 token」预算。弱模型即便评分点规模短，一次性输出长 JSON
也易被截断，应强制走分步路径。
"""

import json

from app.services.ai import workflows_outline_v51 as v51


def _short_scoring() -> str:
    return json.dumps(
        [{"item": f"评分{i}", "score": 10} for i in range(3)], ensure_ascii=False
    )


def _long_scoring() -> str:
    return json.dumps(
        [{"item": f"评分项目条目内容{i}" * 10, "score": 10} for i in range(20)],
        ensure_ascii=False,
    )


def test_token_budget_below_threshold_forces_stepwise():
    """短标书 + 模型输出预算低于阈值 → 强制分步（即便规模判定为短）。"""
    assert v51.should_use_direct_outline(_short_scoring(), "auto", model_max_output_tokens=2048) is False
    # 预算充足 → 仍走直出
    assert v51.should_use_direct_outline(_short_scoring(), "auto", model_max_output_tokens=8192) is True


def test_legacy_call_without_token_preserves_short_behavior():
    """未传 token（旧调用口径）时行为不变：短→直出，长→分步。"""
    assert v51.should_use_direct_outline(_short_scoring(), "auto") is True
    assert v51.should_use_direct_outline(_long_scoring(), "auto") is False


def test_explicit_direct_mode_overrides_token_gate():
    """显式 direct 模式覆盖长度与 token 判别（与既有语义一致）。"""
    assert v51.should_use_direct_outline(_long_scoring(), "direct", model_max_output_tokens=1024) is True
    assert v51.should_use_direct_outline(_short_scoring(), "stepwise", model_max_output_tokens=1024) is False


def test_provider_max_output_tokens_helper_precedence(monkeypatch):
    """_provider_max_output_tokens：provider 属性 > 运维声明 > None。"""

    class _P:
        max_output_tokens = 2048

    assert v51._provider_max_output_tokens(_P()) == 2048

    # provider 无属性，回退运维声明
    class _P2:
        pass

    monkeypatch.setattr(v51, "OUTLINE_DIRECT_MODEL_MAX_OUTPUT_TOKENS", 8192)
    assert v51._provider_max_output_tokens(_P2()) == 8192

    # 均未声明 → None（沿用旧规模代理口径）
    monkeypatch.setattr(v51, "OUTLINE_DIRECT_MODEL_MAX_OUTPUT_TOKENS", 0)
    assert v51._provider_max_output_tokens(None) is None
    assert v51._provider_max_output_tokens(_P2()) is None
