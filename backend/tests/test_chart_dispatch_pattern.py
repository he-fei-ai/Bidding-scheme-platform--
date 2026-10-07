"""
重构带教的可运行样本：用"注册表 + 分发"消灭 sections.py 里 6 类图表的重复端点。

本测试完全自包含（不依赖 DB / AI provider），只演示并验证 DRY 模式本身：
  1. 每种图表类型都必须同时登记 arrange 与 generate（完整性契约）
  2. 未知图表类型必须 fail-fast（立即报错，而非静默走默认）
  3. 分发器按类型返回正确的处理函数

对应教学文档：仓库根目录 `重构带教_sections.md`
运行：cd backend && pytest tests/test_chart_dispatch_pattern.py -q
"""

from typing import Callable

import pytest


# ---------- 1. 被测的"分发表"模式（与生产中 CHART_REGISTRY 同构） ----------
class _FakeProvider:
    """模拟 AI provider，仅用于验证分发逻辑，不发起真实请求。"""

    async def arrange(self, *, section_title: str, **kwargs) -> dict:
        return {"needed": True, "title": section_title}

    async def generate(self, *, section_title: str, **kwargs) -> str:
        return f"mermaid://{section_title}"


# 用普通函数模拟 image_engine 里的各个生成器，签名一致便于统一调用
async def fake_arrange(provider: _FakeProvider, *, section_title: str, **_kw) -> dict:
    return await provider.arrange(section_title=section_title)


async def fake_generate(provider: _FakeProvider, *, section_title: str, **_kw) -> str:
    return await provider.generate(section_title=section_title)


# 注册表：图表类型 -> {arrange, generate, field}
CHART_REGISTRY: dict[str, dict] = {
    "flowchart": {"arrange": fake_arrange, "generate": fake_generate, "field": "flowchart_json"},
    "architecture": {"arrange": fake_arrange, "generate": fake_generate, "field": "architecture_json"},
    "labor": {"arrange": fake_arrange, "generate": fake_generate, "field": "labor_json"},
    "comparison": {"arrange": fake_arrange, "generate": fake_generate, "field": "flowchart_json"},
}

VALID_TYPES = set(CHART_REGISTRY.keys())

# 生产环境会抛 HTTPException；测试里用等价异常便于断言
DISPATCH_ERROR = ValueError


def get_chart_spec(chart_type: str) -> dict:
    """未知类型立即失败（fail-fast），对应生产中 raise HTTPException(400)。"""
    spec = CHART_REGISTRY.get(chart_type)
    if spec is None:
        raise DISPATCH_ERROR(f"不支持的图表类型: {chart_type}")
    return spec


# ---------- 2. 测试用例 ----------

def test_registry_complete_contract():
    """完整性契约：每种类型都必须同时有 arrange 与 generate，且为可调用对象。"""
    required_keys = ("arrange", "generate", "field")
    for chart_type, spec in CHART_REGISTRY.items():
        for key in required_keys:
            assert key in spec, f"{chart_type} 缺少字段 {key}"
        assert isinstance(spec["arrange"], Callable), f"{chart_type}.arrange 非可调用"
        assert isinstance(spec["generate"], Callable), f"{chart_type}.generate 非可调用"
        assert isinstance(spec["field"], str) and spec["field"], f"{chart_type}.field 非法"


def test_known_types_resolve():
    """已知类型能正确解析出各自的落地字段。"""
    assert get_chart_spec("flowchart")["field"] == "flowchart_json"
    assert get_chart_spec("architecture")["field"] == "architecture_json"
    assert get_chart_spec("labor")["field"] == "labor_json"


def test_unknown_type_fails_fast():
    """未知/拼写错误的类型必须立即报错，而不是静默走默认分支。"""
    with pytest.raises(DISPATCH_ERROR):
        get_chart_spec("flowchar")  # 少个 t
    with pytest.raises(DISPATCH_ERROR):
        get_chart_spec("gantt")  # 未在注册表（演示：新增类型必须显式登记）


@pytest.mark.asyncio
async def test_dispatch_calls_correct_handler():
    """分发器按类型调用对应 handler，返回符合预期的结构。"""
    provider = _FakeProvider()

    spec = get_chart_spec("flowchart")
    arrange_result = await spec["arrange"](provider, section_title="施工方案")
    assert arrange_result["needed"] is True

    gen_result = await spec["generate"](provider, section_title="施工方案")
    assert gen_result.startswith("mermaid://")
