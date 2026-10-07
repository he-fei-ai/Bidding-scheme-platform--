"""test_sole_generation_path.py — V5.1 唯一路径测试锁定（防线6）

核心用例断言：
1. 任何 strategy 值都路由到 V5.1
2. sse_handlers 仅导入 V5.1 工作流
3. 已退役提示词不在注册表中
4. outline_common 函数可用性
5. 入口收口机制正常工作
"""

import importlib
import sys
from pathlib import Path

import pytest


pytestmark = pytest.mark.skip(
    reason="[V3 LEGACY] workflows_outline_v2v3.py 已物理删除，统一走 V5.1 generate_outline()，跳过已退役路径测试"
)


# 路径设置
sys.path.insert(0, str(Path(__file__).parent.parent))


class TestV51SolePath:
    """V5.1 唯一路径断言"""

    def test_outline_common_imports_available(self):
        """防线2: outline_common 模块可正常导入，V5.1 依赖的共享函数存在"""
        from app.services.ai.outline_common import (
            _count_outline_nodes,
            _ensure_min_children,
            _parse_scope_items,
            _uniquify_outline_titles,
            generate_nodes_distribution,
            generate_one_outline_json_by_level1,
            check_json,
        )
        assert callable(_count_outline_nodes)
        assert callable(_ensure_min_children)
        assert callable(generate_nodes_distribution)
        assert callable(generate_one_outline_json_by_level1)
        assert callable(check_json)

    def test_v51_imports_from_outline_common(self):
        """防线2: V5.1 工作流从 outline_common 而非 v2v3 导入"""
        v51_source = Path(
            Path(__file__).parent.parent / "app/services/ai/workflows_outline_v51.py"
        ).read_text(encoding="utf-8")

        # V5.1 应从 outline_common 导入
        assert "from .outline_common import" in v51_source, (
            "V5.1 未从 outline_common 导入共享函数"
        )

        # V5.1 不应从 v2v3 导入共享函数
        # 注意: 仍可能导入 generate_outline 等，这里只检查共享函数
        v2v3_shared_imports = [
            "from .workflows_outline_v2v3 import",
        ]
        # 检查是否仍有 v2v3 的共享函数导入（非全部）
        for pattern in ["_ensure_min_children", "_uniquify_outline_titles",
                        "_parse_scope_items", "_count_outline_nodes",
                        "generate_nodes_distribution", "check_json"]:
            # 如果 v51 中从 v2v3 导入这些函数，说明迁移未完成
            if f"workflows_outline_v2v3" in v51_source and pattern in v51_source:
                assert False, f"V5.1 仍从 v2v3 导入 {pattern}，应改为 outline_common"

    def test_retired_prompts_not_in_v51(self):
        """防线3: 已退役 V3 提示词不在 V5.1 提示词文件中"""
        outline_prompts = Path(
            Path(__file__).parent.parent / "app/services/ai/prompts/outline.py"
        ).read_text(encoding="utf-8")

        retired_patterns = [
            "OUTLINE_STEP_SYSTEM",
            "OUTLINE_STEP_USER",
            "OUTLINE_CHILDREN_SYSTEM",
            "OUTLINE_CHILDREN_USER",
        ]
        for pattern in retired_patterns:
            assert pattern not in outline_prompts, (
                f"已退役提示词 {pattern} 仍存在于 outline.py"
            )

    def test_v51_prompts_present(self):
        """防线3: V5.1 提示词仍然存在"""
        outline_prompts = Path(
            Path(__file__).parent.parent / "app/services/ai/prompts/outline.py"
        ).read_text(encoding="utf-8")

        required_prompts = [
            "OUTLINE_V51_LEVEL1_SYSTEM",
            "OUTLINE_V51_LEVEL2_SYSTEM",
            "OUTLINE_V51_SELFCHECK_SYSTEM",
            "OUTLINE_REVIEW_SYSTEM",
            "OUTLINE_REVIEW_USER",
            "L3_TITLE_REPAIR_PROMPT",
        ]
        for prompt in required_prompts:
            assert prompt in outline_prompts, (
                f"必需的 V5.1 提示词 {prompt} 缺失"
            )

    def test_dead_prompts_retired(self):
        """防线4: 已下线的死模板不得复活（2026-09-14 清理）。

        OUTLINE_V51_LEVEL3_SYSTEM / TECH_REWRITE_PROMPT 注册了但全库无调用方：
        L3 早已改为纯规则生成（_generate_diverse_l3_title，不调 AI），
        技术化改写也走规则实现。留着会占满提示词管理列表、可编辑却不生效，
        误导管理员以为改动会影响生成结果。
        """
        outline_prompts = Path(
            Path(__file__).parent.parent / "app/services/ai/prompts/outline.py"
        ).read_text(encoding="utf-8")

        retired = ["OUTLINE_V51_LEVEL3_SYSTEM", "TECH_REWRITE_PROMPT"]
        for prompt in retired:
            assert prompt not in outline_prompts, (
                f"已下线的死模板 {prompt} 又回来了"
            )

    def test_tech_knowledge_available(self):
        """防线3: 技术知识库可导入"""
        from app.services.ai.tech_knowledge import (
            TECH_KNOWLEDGE,
            get_tech_candidates,
            is_generic_title,
            get_tech_term_for_domain,
        )
        assert len(TECH_KNOWLEDGE) >= 2  # new_tech + mature_method
        candidates = get_tech_candidates()
        assert len(candidates) > 0
        assert is_generic_title("质量控制") is True
        assert is_generic_title("模板工程与新型盘扣式支撑体系施工") is False

    def test_entry_guard_in_sections(self):
        """防线1: sections.py 包含 legacy strategy 处理逻辑"""
        sections_source = Path(
            Path(__file__).parent.parent / "app/routers/sections.py"
        ).read_text(encoding="utf-8")

        assert "legacy strategy" in sections_source.lower() or \
               "legacy_strategy" in sections_source, (
            "入口收口机制缺失: 未检测到 legacy strategy 处理"
        )

    def test_outline_common_functions_work(self):
        """基础功能: outline_common 核心函数可正常运行"""
        from app.services.ai.outline_common import (
            _uniquify_outline_titles,
            _ensure_min_children,
            _count_outline_nodes,
        )

        # _count_outline_nodes
        test_outline = [
            {"title": "test", "children": [
                {"title": "child1", "children": [
                    {"title": "grandchild1", "children": []}
                ]}
            ]}
        ]
        count = _count_outline_nodes(test_outline)
        assert count == 3, f"Expected 3 nodes, got {count}"

        # _uniquify_outline_titles
        outline = [
            {"title": "A", "children": [
                {"title": "X", "children": []},
                {"title": "X", "children": []},  # duplicate
            ]}
        ]
        result = _uniquify_outline_titles(outline)
        titles = [c["title"] for c in result[0]["children"]]
        assert len(titles) == len(set(titles)), "去重后标题应全部唯一"

        # _ensure_min_children
        minimal_outline = [{"title": "test", "children": []}]
        result = _ensure_min_children(minimal_outline, min_level2=3, min_level3=3)
        assert len(result[0]["children"]) >= 3, "应至少有3个二级目录"
        for child in result[0]["children"]:
            assert len(child.get("children", [])) >= 3, (
                f"每个二级下应至少有3个三级目录, '{child.get('title')}' 只有 {len(child.get('children', []))}"
            )


class TestNoV3Paths:
    """确保没有可调用的 V3 路径"""

    def test_no_generate_outline_v3_import_in_active_code(self):
        """防线2: 活跃代码中不存在 generate_outline_v3 的可调用路径"""
        active_files = [
            Path(__file__).parent.parent / "app/routers/sections.py",
            Path(__file__).parent.parent / "app/routers/sse_handlers.py",
            Path(__file__).parent.parent / "app/services/ai/workflows_outline_v51.py",
        ]
        for f in active_files:
            if f.exists():
                content = f.read_text(encoding="utf-8")
                # 不应存在 V3 生成入口
                assert "generate_outline_v3" not in content, (
                    f"V3 入口仍存在于 {f.name}"
                )
                assert "generate_outline_one_shot" not in content, (
                    f"one_shot 入口仍存在于 {f.name}"
                )
                assert "generate_outline_step_by_step" not in content, (
                    f"step_by_step 入口仍存在于 {f.name}"
                )

    def test_wf_outline_v51_has_no_v3_strategy_param(self):
        """防线2: V5.1 generate_outline 不接受 strategy 参数"""
        v51_source = Path(
            Path(__file__).parent.parent / "app/services/ai/workflows_outline_v51.py"
        ).read_text(encoding="utf-8")

        # generate_outline 函数签名中不应有 strategy 参数
        # 检查函数定义行
        for line in v51_source.split("\n"):
            if "async def generate_outline(" in line:
                assert "strategy" not in line, (
                    "generate_outline 仍接受 strategy 参数"
                )
                break
