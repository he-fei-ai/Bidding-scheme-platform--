"""§6.1 P2：模板段落 AI 语义映射单元测试。

覆盖 TEMPLATE_AI_SECTION_MAPPING_ENABLED 开关的开/关两态，
以及 AI 映射失败回落、白名单校验、视角反转、空输入等边界。

对齐 template_coverage_audit 的测试风格：monkeypatch 环境变量控制开关，
不启真实 AI 调用（全部通过 monkeypatch collect_json_response 模拟返回）。
"""

from __future__ import annotations

import pytest

from app.services.template_expansion import (
    TemplateExpansionService,
    TemplateSection,
    _ai_section_mapping_enabled,
    _ai_section_mapping_timeout,
    _ai_section_mapping_topk,
)


# ============================================================================
# 构造测试数据
# ============================================================================


def _make_section(sid: str, title_path: list[str], chars: int = 100) -> TemplateSection:
    """构造一个 TemplateSection 测试夹具。"""
    return TemplateSection(
        id=sid,
        title_path=list(title_path),
        content=f"内容 {sid}",
        chars=chars,
        hash=f"h_{sid}",
    )


def _make_node(node_id: str, title: str, is_leaf: bool = True) -> dict:
    """构造一个目录节点测试夹具。"""
    return {
        "id": node_id,
        "title": title,
        "is_leaf": is_leaf,
        "level": 3,
    }


@pytest.fixture
def service() -> TemplateExpansionService:
    return TemplateExpansionService()


# ============================================================================
# 开关读取工具函数
# ============================================================================


class TestSwitchReaders:
    """_ai_section_mapping_enabled / _timeout / _topk 环境读取。"""

    def test_enabled_default_false(self, monkeypatch):
        monkeypatch.delenv("TEMPLATE_AI_SECTION_MAPPING_ENABLED", raising=False)
        assert _ai_section_mapping_enabled() is False

    @pytest.mark.parametrize("raw", ["1", "true", "True", "TRUE", "yes", "on"])
    def test_enabled_truthy_values(self, monkeypatch, raw):
        monkeypatch.setenv("TEMPLATE_AI_SECTION_MAPPING_ENABLED", raw)
        assert _ai_section_mapping_enabled() is True

    @pytest.mark.parametrize("raw", ["0", "false", "no", "off", "", "unknown"])
    def test_enabled_falsy_values(self, monkeypatch, raw):
        monkeypatch.setenv("TEMPLATE_AI_SECTION_MAPPING_ENABLED", raw)
        assert _ai_section_mapping_enabled() is False

    def test_timeout_default_60(self, monkeypatch):
        monkeypatch.delenv("TEMPLATE_AI_SECTION_MAPPING_TIMEOUT", raising=False)
        assert _ai_section_mapping_timeout() == 60

    def test_timeout_clamped_to_range(self, monkeypatch):
        monkeypatch.setenv("TEMPLATE_AI_SECTION_MAPPING_TIMEOUT", "1")
        assert _ai_section_mapping_timeout() == 5  # 下限 5
        monkeypatch.setenv("TEMPLATE_AI_SECTION_MAPPING_TIMEOUT", "999")
        assert _ai_section_mapping_timeout() == 300  # 上限 300

    def test_timeout_invalid_fallback(self, monkeypatch):
        monkeypatch.setenv("TEMPLATE_AI_SECTION_MAPPING_TIMEOUT", "abc")
        assert _ai_section_mapping_timeout() == 60

    def test_topk_default_3(self, monkeypatch):
        monkeypatch.delenv("TEMPLATE_AI_SECTION_MAPPING_TOPK", raising=False)
        assert _ai_section_mapping_topk() == 3

    def test_topk_clamped_to_range(self, monkeypatch):
        monkeypatch.setenv("TEMPLATE_AI_SECTION_MAPPING_TOPK", "0")
        assert _ai_section_mapping_topk() == 1  # 下限 1
        monkeypatch.setenv("TEMPLATE_AI_SECTION_MAPPING_TOPK", "99")
        assert _ai_section_mapping_topk() == 10  # 上限 10


# ============================================================================
# 开关关闭（默认行为）：完全走确定性匹配
# ============================================================================


class TestSwitchOff:
    """开关关（默认）时，map_sections_to_nodes 完全走 _simple_title_match。"""

    @pytest.fixture(autouse=True)
    def switch_off(self, monkeypatch):
        monkeypatch.delenv("TEMPLATE_AI_SECTION_MAPPING_ENABLED", raising=False)

    def test_returns_empty_for_empty_input(self, service):
        assert service.map_sections_to_nodes([], []) == []
        assert service.map_sections_to_nodes([_make_section("P001", ["项目"])], []) == []
        assert service.map_sections_to_nodes([], [_make_node("1", "节点")]) == []

    def test_deterministic_match_no_ai_call(self, service, monkeypatch):
        """开关关时，绝不触发 AI 调用。"""
        sections = [_make_section("P001", ["项目概况"])]
        nodes = [_make_node("1", "项目概况", is_leaf=True)]

        ai_called = {"count": 0}

        def _fail_ai(*args, **kwargs):
            ai_called["count"] += 1
            raise AssertionError("开关关时不应调用 AI")

        # 替换 collect_json_response，若被调用会抛异常
        import app.services.template_expansion as mod
        monkeypatch.setattr(mod, "collect_json_response", _fail_ai)

        result = service.map_sections_to_nodes(sections, nodes)

        # 标题包含匹配 → 至少 1 条
        assert isinstance(result, list)
        assert any("项目概况" in m.get("node_title", "") for m in result)
        assert ai_called["count"] == 0

    def test_no_ai_marker_field(self, service):
        """开关关时，结果中不应出现 ai_matched 字段。"""
        sections = [_make_section("P001", ["工程"])]
        nodes = [_make_node("1", "工程概况", is_leaf=True)]

        result = service.map_sections_to_nodes(sections, nodes)
        for m in result:
            assert "ai_matched" not in m


# ============================================================================
# 开关开启：AI 映射成功、失败回落、白名单校验、视角反转
# ============================================================================


class TestSwitchOn:
    """开关开时，走 AI 语义映射 + 确定性回退。"""

    @pytest.fixture(autouse=True)
    def switch_on(self, monkeypatch):
        monkeypatch.setenv("TEMPLATE_AI_SECTION_MAPPING_ENABLED", "true")

    def _patch_ai(self, monkeypatch, result):
        """替换 collect_json_response（async）+ get_ai_config + create_provider，返回给定 result。"""
        import app.services.template_expansion as mod

        captured = {"called": 0, "messages": None}

        async def _fake_collect(provider, messages, **kwargs):
            captured["called"] += 1
            captured["messages"] = messages
            return result

        monkeypatch.setattr(mod, "collect_json_response", _fake_collect)
        monkeypatch.setattr(mod, "get_ai_config", lambda: {"api_key": "test"})
        monkeypatch.setattr(mod, "create_provider", lambda cfg: object())
        return captured

    def test_ai_success_returns_ai_matched(self, service, monkeypatch):
        """AI 成功返回时，结果带 ai_matched=True。"""
        captured = self._patch_ai(
            monkeypatch,
            {"mappings": [{"paragraph_id": "P001", "node_ids": ["1.1", "1.2"]}]},
        )

        sections = [
            _make_section("P001", ["工程概况"], chars=200),
        ]
        nodes = [
            _make_node("1.1", "工程概况", is_leaf=True),
            _make_node("1.2", "项目背景", is_leaf=True),
            _make_node("2.1", "施工计划", is_leaf=True),
        ]

        result = service.map_sections_to_nodes(sections, nodes)

        assert captured["called"] == 1
        assert len(result) == 2  # 两个节点各一条
        for m in result:
            assert m.get("ai_matched") is True
            assert m.get("source_ids") == ["P001"]

    def test_ai_empty_falls_back_to_deterministic(self, service, monkeypatch):
        """AI 返回空映射时，回落到 _simple_title_match。"""
        self._patch_ai(monkeypatch, {"mappings": []})

        sections = [_make_section("P001", ["工程"])]
        nodes = [_make_node("1", "工程概况", is_leaf=True)]

        result = service.map_sections_to_nodes(sections, nodes)

        # 回落到确定性匹配（标题包含"工程"→ 命中）
        assert isinstance(result, list)
        # 回落结果不应带 ai_matched
        for m in result:
            assert "ai_matched" not in m

    def test_ai_exception_falls_back_to_deterministic(self, service, monkeypatch):
        """AI 抛异常时，回落到 _simple_title_match。"""
        import app.services.template_expansion as mod

        def _raise_ai(*args, **kwargs):
            raise RuntimeError("AI 服务不可达")

        monkeypatch.setattr(mod, "collect_json_response", _raise_ai)

        sections = [_make_section("P001", ["施工"])]
        nodes = [_make_node("1", "施工方案", is_leaf=True)]

        result = service.map_sections_to_nodes(sections, nodes)

        # 回落成功，至少有一条确定性结果
        assert isinstance(result, list)
        assert len(result) >= 1
        for m in result:
            assert "ai_matched" not in m

    def test_ai_invalid_paragraph_id_dropped(self, service, monkeypatch):
        """AI 返回清单外的段落 id → 该条被丢弃。"""
        self._patch_ai(
            monkeypatch,
            {"mappings": [
                {"paragraph_id": "P999", "node_ids": ["1.1"]},  # 非法段落 id
                {"paragraph_id": "P001", "node_ids": ["1.1"]},   # 合法
            ]},
        )

        sections = [_make_section("P001", ["工程"])]
        nodes = [_make_node("1.1", "工程概况", is_leaf=True)]

        result = service.map_sections_to_nodes(sections, nodes)

        assert len(result) == 1
        assert result[0]["node_id"] == "1.1"
        assert result[0]["source_ids"] == ["P001"]

    def test_ai_invalid_node_id_dropped(self, service, monkeypatch):
        """AI 返回清单外的节点 id → 该 node_id 被丢弃。"""
        self._patch_ai(
            monkeypatch,
            {"mappings": [
                {"paragraph_id": "P001", "node_ids": ["1.1", "999"]},  # 999 非法
            ]},
        )

        sections = [_make_section("P001", ["工程"])]
        nodes = [_make_node("1.1", "工程概况", is_leaf=True)]

        result = service.map_sections_to_nodes(sections, nodes)

        assert len(result) == 1
        assert result[0]["node_id"] == "1.1"
        # 因为 999 被丢弃，只剩一个节点
        assert result[0]["source_ids"] == ["P001"]

    def test_ai_topk_limit_respected(self, service, monkeypatch):
        """AI 返回超过 topk 的候选 → 截取前 topk 个。"""
        monkeypatch.setenv("TEMPLATE_AI_SECTION_MAPPING_TOPK", "2")
        self._patch_ai(
            monkeypatch,
            {"mappings": [
                {"paragraph_id": "P001", "node_ids": ["1.1", "1.2", "1.3", "1.4"]},
            ]},
        )

        sections = [_make_section("P001", ["工程"])]
        nodes = [
            _make_node("1.1", "节点1", is_leaf=True),
            _make_node("1.2", "节点2", is_leaf=True),
            _make_node("1.3", "节点3", is_leaf=True),
            _make_node("1.4", "节点4", is_leaf=True),
        ]

        result = service.map_sections_to_nodes(sections, nodes)

        # topk=2，只应出现 2 个节点
        assert len(result) == 2
        node_ids = sorted(m["node_id"] for m in result)
        assert node_ids == ["1.1", "1.2"]

    def test_ai_multi_paragraph_merge(self, service, monkeypatch):
        """多个段落的映射按节点视角合并。"""
        self._patch_ai(
            monkeypatch,
            {"mappings": [
                {"paragraph_id": "P001", "node_ids": ["1.1"]},
                {"paragraph_id": "P002", "node_ids": ["1.1", "2.1"]},  # P002 也指向 1.1
            ]},
        )

        sections = [
            _make_section("P001", ["工程概况"], chars=100),
            _make_section("P002", ["项目背景"], chars=200),
        ]
        nodes = [
            _make_node("1.1", "工程概况", is_leaf=True),
            _make_node("2.1", "项目背景", is_leaf=True),
        ]

        result = service.map_sections_to_nodes(sections, nodes)

        # 节点 1.1 应包含 P001 + P002 两个段落
        node_1_1 = next(m for m in result if m["node_id"] == "1.1")
        assert sorted(node_1_1["source_ids"]) == ["P001", "P002"]
        # 节点 2.1 只有 P002
        node_2_1 = next(m for m in result if m["node_id"] == "2.1")
        assert node_2_1["source_ids"] == ["P002"]

    def test_ai_duplicate_paragraph_first_wins(self, service, monkeypatch):
        """同一 paragraph_id 重复出现 → 取首条。"""
        self._patch_ai(
            monkeypatch,
            {"mappings": [
                {"paragraph_id": "P001", "node_ids": ["1.1"]},
                {"paragraph_id": "P001", "node_ids": ["2.1"]},  # 重复，丢弃
            ]},
        )

        sections = [_make_section("P001", ["工程"])]
        nodes = [
            _make_node("1.1", "工程概况", is_leaf=True),
            _make_node("2.1", "项目背景", is_leaf=True),
        ]

        result = service.map_sections_to_nodes(sections, nodes)

        # 只应有一个节点命中
        assert len(result) == 1
        assert result[0]["node_id"] == "1.1"

    def test_ai_all_invalid_falls_back(self, service, monkeypatch):
        """AI 全部返回非法条目 → 回落到确定性匹配。"""
        self._patch_ai(
            monkeypatch,
            {"mappings": [
                {"paragraph_id": "P999", "node_ids": ["1.1"]},  # 段落 id 非法
            ]},
        )

        sections = [_make_section("P001", ["施工"])]
        nodes = [_make_node("1", "施工方案", is_leaf=True)]

        result = service.map_sections_to_nodes(sections, nodes)

        # 回落到确定性匹配
        for m in result:
            assert "ai_matched" not in m

    def test_ai_result_not_dict_falls_back(self, service, monkeypatch):
        """AI 返回非 dict 结构 → 回落到确定性匹配。"""
        self._patch_ai(monkeypatch, None)

        sections = [_make_section("P001", ["施工"])]
        nodes = [_make_node("1", "施工方案", is_leaf=True)]

        result = service.map_sections_to_nodes(sections, nodes)

        # 回落
        for m in result:
            assert "ai_matched" not in m

    def test_prompt_contains_role_and_rules(self, service):
        """提示词包含关键角色和规则声明。"""
        prompt = service._get_section_mapping_prompt()
        assert "标书结构分析专家" in prompt
        assert "语义相近" in prompt
        assert "禁止编造" in prompt
        assert "mappings" in prompt


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
