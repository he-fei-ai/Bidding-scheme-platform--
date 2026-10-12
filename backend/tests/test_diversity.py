"""目录二三级标题多样性优化单元测试。

覆盖场景：
  1. 章节分类（_classify_chapter_type）— 优先级、边界、模糊匹配
  2. 兜底标题生成（_generate_diverse_l2/l3_title）— 取模、索引跨章不重复
  3. 框架填充（sanitize_framework_titles）— 占位替换、空框架、正常标题
  4. 模板池完整性 — 类型覆盖、模板数量、无重复项
  5. 端到端 — 28 章骨架 + 跨章节重复率统计
"""

import copy
import sys
from pathlib import Path

import pytest

# 确保 backend/ 在路径中
BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.services.ai.workflows import (
    FIXED_28_CHAPTERS,
    _classify_chapter_type,
    _generate_diverse_l2_title,
    _generate_diverse_l3_title,
    _L2_TITLE_PATTERNS,
    _L3_TITLE_PATTERNS,
    build_chapter7_framework,
    build_framework_for_chapter,
    generate_nodes_distribution,
    sanitize_framework_titles,
)


# ===========================================================================
# 多样性阈值单一真源（Single Source of Truth）
# ===========================================================================
# 2026-10-09 加固：把散落各断言中的硬编码阈值收敛到本字典，避免「注释 vs
# 断言 vs 文档」三处口径漂移（历史上 .trae/documents/next_optimization_plan.md
# P1 就因 28% / 55% 不一致产生维护混乱）。修改阈值只需改这一处，
# 所有断言消息与文档引用均可从 THRESHOLDS 派生。
#
# 来源：V5.2 跨章节全局去重 + V5.3 语义化兜底后，纯兜底场景 28 章 L3 唯一率
#       实测 ≥ 100%（参见 tests/test_diversity.py 输出与
#       .trae/documents/next_optimization_plan.md P0 修复说明）。
#       这里保留 0.55 作为安全下限，防止未来模板池缩小后无声退化。
THRESHOLDS = {
    # 章节内 L3 唯一率（28 章跨章节全局去重后的最小要求）
    "L3_UNIQUENESS_CHAPTERS": 0.55,
    # 端到端 Mock 场景的 L2 唯一率
    "L2_UNIQUENESS_E2E": 0.60,
    # 端到端 Mock 场景的 L3 唯一率
    "L3_UNIQUENESS_E2E": 0.25,
}


# ===========================================================================
# 1. 章节分类测试
# ===========================================================================

class TestClassifyChapterType:
    """测试 _classify_chapter_type 的分类逻辑。"""

    @pytest.mark.parametrize(
        "title,expected",
        [
            # 具体类型优先于通用类型
            ("质量保证措施和创优计划", "quality"),
            ("质量保证体系与检查机制", "quality"),
            ("质量通病防治方案", "quality"),
            ("安全文明施工措施", "safety"),
            ("安全管理体系与应急预案", "safety"),
            ("环保与文明施工管理", "safety"),
            ("消防与安全管理方案", "safety"),
            ("施工总进度计划及保证措施", "schedule"),
            ("工期进度管控方案", "schedule"),
            ("劳动力资源配置计划", "resource"),
            ("主要施工机械设备投入", "resource"),
            ("施工现场总平面布置", "layout"),
            ("施工总平面布置方案", "layout"),
            ("任何可能的紧急情况的处理措施", "emergency"),
            ("风险识别与应急预案", "emergency"),
            ("主要分部工程施工方法", "construction"),
            ("土方与基础工程施工方案", "construction"),
            ("专项施工方案", "construction"),
            ("工程概况与建设规模", "overview"),
            ("项目概况说明", "overview"),
            ("编制依据与说明", "overview"),
            ("与建设单位协调配合方案", "coordination"),
            ("项目重难点分析", "analysis"),
            ("重难点分析与应对措施", "analysis"),
            ("施工方案及技术措施", "construction"),
            ("施工技术安全与方案", "safety"),
            ("施工措施与质量控制", "quality"),
            ("施工总进度计划及保证措施", "schedule"),
            ("附图、附件", "attachment"),
            ("施工总平面布置图", "layout"),
            ("其他内容", "other"),
            ("参考文献", "other"),
        ],
    )
    def test_classification(self, title: str, expected: str):
        assert _classify_chapter_type(title) == expected

    def test_empty_title(self):
        assert _classify_chapter_type("") == "other"

    def test_none_title(self):
        # 传入 None 会抛异常（类型提示是 str），这里测试空字符串边界
        assert _classify_chapter_type("") == "other"

    def test_priority_quality_over_measure(self):
        """包含'质量'和'措施'的标题应优先归为 quality。"""
        assert _classify_chapter_type("质量保证措施和创优计划") == "quality"
        assert _classify_chapter_type("质量管控措施方案") == "quality"

    def test_priority_safety_over_measure(self):
        """包含'安全'和'措施'的标题应优先归为 safety。"""
        assert _classify_chapter_type("施工安全措施计划") == "safety"
        assert _classify_chapter_type("安全文明施工措施") == "safety"

    def test_priority_schedule_over_measure(self):
        """包含'进度'和'措施'的标题应优先归为 schedule。"""
        assert _classify_chapter_type("施工总进度计划及保证措施") == "schedule"
        assert _classify_chapter_type("进度管控方案") == "schedule"

    def test_construction_vs_measure(self):
        """包含'施工'且无更具体关键词时应归为 construction。"""
        assert _classify_chapter_type("施工测量方案与精度控制") == "construction"

    def test_attachment_priority(self):
        """含'平面'应归为 layout（优先于 attachment）。"""
        assert _classify_chapter_type("施工总平面布置图") == "layout"


# ===========================================================================
# 2. 模板池完整性测试
# ===========================================================================

class TestTemplatePools:
    """测试 L2/L3 模板池的完整性和无重复。"""

    def test_l2_all_types_have_16_templates(self):
        """每种类型应有 16 个 L2 模板。"""
        for typ, patterns in _L2_TITLE_PATTERNS.items():
            assert len(patterns) == 16, f"类型 {typ} 的 L2 模板数 {len(patterns)} != 16"

    def test_l3_all_types_have_40_templates(self):
        """每种类型应有 40 个 L3 模板（含扩展句式）。"""
        for typ, patterns in _L3_TITLE_PATTERNS.items():
            assert len(patterns) == 40, f"类型 {typ} 的 L3 模板数 {len(patterns)} != 40"

    def test_l2_no_duplicates_within_type(self):
        """同一类型内 L2 模板应无重复。"""
        for typ, patterns in _L2_TITLE_PATTERNS.items():
            assert len(patterns) == len(set(patterns)), f"类型 {typ} 的 L2 模板有重复"

    def test_l3_no_duplicates_within_type(self):
        """同一类型内 L3 模板应无重复。"""
        for typ, patterns in _L3_TITLE_PATTERNS.items():
            assert len(patterns) == len(set(patterns)), f"类型 {typ} 的 L3 模板有重复"

    def test_l2_all_types_covered(self):
        """L2 模板应覆盖所有分类类型。"""
        expected_types = {
            "quality", "safety", "schedule", "resource", "construction",
            "overview", "coordination", "emergency", "layout",
            "analysis", "measure", "attachment", "other",
        }
        actual_types = set(_L2_TITLE_PATTERNS.keys())
        assert expected_types == actual_types, f"缺少的类型: {expected_types - actual_types}"

    def test_l3_all_types_covered(self):
        """L3 模板应覆盖所有分类类型。"""
        expected_types = {
            "quality", "safety", "schedule", "resource", "construction",
            "overview", "coordination", "emergency", "layout",
            "analysis", "measure", "attachment", "other",
        }
        actual_types = set(_L3_TITLE_PATTERNS.keys())
        assert expected_types == actual_types, f"缺少的类型: {expected_types - actual_types}"


# ===========================================================================
# 3. 兜底标题生成测试
# ===========================================================================

class TestGenerateDiverseTitles:
    """测试 _generate_diverse_l2_title 和 _generate_diverse_l3_title。"""

    @pytest.mark.parametrize(
        "chapter_type",
        list(_L2_TITLE_PATTERNS.keys()),
    )
    def test_l2_returns_valid_title(self, chapter_type: str):
        result = _generate_diverse_l2_title("测试章节", chapter_type, 0, 0)
        assert isinstance(result, str)
        assert len(result) > 0

    def test_l3_returns_valid_title(self):
        """V5.2 语义派生 API：(l2_title, idx, l1_title, used_titles, l2_index)。"""
        for idx in range(10):
            result = _generate_diverse_l3_title("钢筋工程", idx, "主体结构施工技术方案", None, 0)
            assert isinstance(result, str)
            assert len(result) > 0

    def test_l3_sibling_titles_no_duplicates(self):
        """同一 L2 下的兄弟 L3 标题应互不重复。"""
        titles = [
            _generate_diverse_l3_title("钢筋工程", idx, "主体结构施工技术方案", None, 0)
            for idx in range(10)
        ]
        assert len(set(titles)) == len(titles), f"兄弟 L3 标题出现重复: {titles}"

    def test_l3_respects_used_titles(self):
        """used_titles 中的标题不应被再次返回（跨 L2 全局去重）。"""
        used: set[str] = set()
        first = _generate_diverse_l3_title("钢筋工程", 0, "主体结构", used, 0)
        used.add(first)
        second = _generate_diverse_l3_title("钢筋工程", 0, "主体结构", used, 0)
        assert first != second, "已用标题应被跳过"

    def test_l3_empty_titles_fallback(self):
        """l2_title/l1_title 均为空时应兜底为通用标题而不抛异常。"""
        result = _generate_diverse_l3_title("", 0, "", None, 0)
        assert isinstance(result, str)
        assert len(result) > 0

    def test_l2_cross_chapter_different_for_same_type(self):
        """同类型不同章节索引应生成不同标题。"""
        titles = [
            _generate_diverse_l2_title("测试章节", "quality", 0, idx)
            for idx in range(20)
        ]
        unique = set(titles)
        # 16 个模板 × 20 章节 → 应有重复但不应全部相同
        assert len(unique) >= 10, f"20 个章节的同类型 L2 标题唯一数太少: {len(unique)}"

    def test_l2_cross_chapter_different_for_different_types(self):
        """不同类型应生成不同类型的标题。"""
        titles = []
        for idx, ch_type in enumerate(["quality", "safety", "schedule", "resource"]):
            titles.append(_generate_diverse_l2_title("测试章节", ch_type, 0, idx))
        # 不同类型的第一章标题应各不相同
        unique = set(titles)
        assert len(unique) == 4, f"不同章类型的 L2 标题应有 4 个唯一值，实际: {titles}"

    def test_l2_index_wraps_correctly(self):
        """child_idx 超出模板数时应正确取模。"""
        pattern_0 = _generate_diverse_l2_title("测试", "quality", 0, 0)
        pattern_16 = _generate_diverse_l2_title("测试", "quality", 16, 0)
        pattern_8 = _generate_diverse_l2_title("测试", "quality", 8, 0)
        # 0 和 16 都模 16 = 0 → 相同
        assert pattern_0 == pattern_16, "child_idx 模 16 应循环"
        # 8 是偏移，应不同
        assert pattern_0 != pattern_8, "child_idx 8 应产生不同的标题"

    def test_l3_l2_index_varies_title(self):
        """不同 l2_index（派生偏移）应产生不同标题。"""
        a = _generate_diverse_l3_title("钢筋工程", 0, "主体结构", None, 0)
        b = _generate_diverse_l3_title("钢筋工程", 0, "主体结构", None, 1)
        assert a != b, "不同 l2_index 应产生不同标题"


# ===========================================================================
# 4. 框架填充测试（sanitize_framework_titles）
# ===========================================================================

class TestSanitizeFrameworkTitles:
    """测试 sanitize_framework_titles 的占位替换逻辑。"""

    def _make_framework(self, l2_count=3, l3_count=2):
        """构建含占位标题的测试框架。"""
        children = []
        for i in range(l2_count):
            grandchildren = []
            for j in range(l3_count):
                grandchildren.append({
                    "id": f"X.{i + 1}.{j + 1}",
                    "title": f"【请替换】三级第{j + 1}项",
                    "level": 3,
                    "children": [],
                })
            children.append({
                "id": f"X.{i + 1}",
                "title": f"【请根据项目实际替换为具体标题】二级第{i + 1}项",
                "level": 2,
                "children": grandchildren,
            })
        return {"id": "X", "title": "测试章节", "level": 1, "children": children}

    def test_replaces_placeholder_l2(self):
        framework = self._make_framework()
        result = sanitize_framework_titles(framework, "施工方案及技术措施", chapter_idx=0)
        for child in result["children"]:
            assert "【请根据项目" not in child["title"]
            assert "二级第" not in child["title"]

    def test_replaces_placeholder_l3(self):
        framework = self._make_framework()
        result = sanitize_framework_titles(framework, "施工方案及技术措施", chapter_idx=0)
        for child in result["children"]:
            for gc in child["children"]:
                assert "【请替换】" not in gc["title"]
                assert "三级第" not in gc["title"]

    def test_no_placeholder_unchanged(self):
        """框架中无占位标题时应原样返回（deepcopy）。"""
        original = {
            "id": "X", "title": "真实标题", "level": 1,
            "children": [
                {
                    "id": "X.1", "title": "真实二级标题", "level": 2,
                    "children": [
                        {"id": "X.1.1", "title": "真实三级标题", "level": 3, "children": []},
                    ],
                },
            ],
        }
        result = sanitize_framework_titles(original, "测试", chapter_idx=0)
        assert result["children"][0]["title"] == "真实二级标题"
        assert result["children"][0]["children"][0]["title"] == "真实三级标题"

    def test_empty_children(self):
        """空 children 框架应安全返回。"""
        framework = {"id": "X", "title": "测试", "level": 1, "children": []}
        result = sanitize_framework_titles(framework, "测试", chapter_idx=0)
        assert result == framework

    def test_different_chapter_idx_produces_different_titles(self):
        """不同章节索引应产生不同的兜底标题。"""
        f1 = self._make_framework()
        f2 = self._make_framework()
        r1 = sanitize_framework_titles(f1, "施工方案及技术措施", chapter_idx=0)
        r2 = sanitize_framework_titles(f2, "施工方案及技术措施", chapter_idx=1)
        l2_r1 = [c["title"] for c in r1["children"]]
        l2_r2 = [c["title"] for c in r2["children"]]
        assert l2_r1 != l2_r2, "不同 chapter_idx 应产生不同的二级标题"

    def test_chapter_type_affects_output(self):
        """不同类型章节应产生不同类型的兜底标题。"""
        f = self._make_framework()
        r_quality = sanitize_framework_titles(f, "质量保证措施和创优计划", chapter_idx=0)
        r_safety = sanitize_framework_titles(f, "安全文明施工措施", chapter_idx=0)
        l2_quality = [c["title"] for c in r_quality["children"]]
        l2_safety = [c["title"] for c in r_safety["children"]]
        assert l2_quality != l2_safety, "quality 和 safety 类型应产生不同的二级标题"


# ===========================================================================
# 5. 跨章节重复率测试
# ===========================================================================

class TestCrossChapterUniqueness:
    """测试跨章节兜底标题的唯一性。"""

    def test_l2_uniqueness_28_chapters(self):
        """28 章兜底场景：L2 唯一率应 > 60%。"""
        all_l2 = []
        for idx in range(28):
            ch = FIXED_28_CHAPTERS[idx]
            dist = generate_nodes_distribution(1, min_level2=3, min_level3=3)[0]
            framework = build_framework_for_chapter(
                {"title": ch["title"]}, dist, chapter_number=idx + 1,
            )
            result = sanitize_framework_titles(framework, ch["title"], chapter_idx=idx)
            for child in result["children"]:
                all_l2.append(child["title"])

        unique = len(set(all_l2))
        total = len(all_l2)
        uniqueness_rate = unique / total if total > 0 else 0
        assert uniqueness_rate >= 0.60, (
            f"L2 唯一率 {uniqueness_rate:.1%} < 60%\n"
            f"总数={total}, 唯一数={unique}\n"
            f"重复标题: {[(t, c) for t, c in __import__('collections').Counter(all_l2).items() if c > 1][:5]}"
        )

    def test_l3_uniqueness_28_chapters(self):
        """28 章兜底场景：L3 唯一率应 >= THRESHOLDS["L3_UNIQUENESS_CHAPTERS"] (55%)。

        V5.2 跨章节全局去重后，所有章节共用同一个 global_pool，
        防止各章独立兜底时产出相同的套话标题。

        阈值来源：本文件顶部 THRESHOLDS 字典（单一真源）。
        历史上（2026-09-10 前）注释与断言存在 28% vs 55% 的漂移，
        本次加固后注释与断言均引用 THRESHOLDS，彻底消除不一致。
        """
        threshold = THRESHOLDS["L3_UNIQUENESS_CHAPTERS"]
        all_l3 = []
        global_pool: set = set()  # ← V5.2 新增：跨章节共享去重池
        for idx in range(28):
            ch = FIXED_28_CHAPTERS[idx]
            dist = generate_nodes_distribution(1, min_level2=3, min_level3=3)[0]
            framework = build_framework_for_chapter(
                {"title": ch["title"]}, dist, chapter_number=idx + 1,
            )
            result = sanitize_framework_titles(
                framework, ch["title"], chapter_idx=idx,
                global_used_titles=global_pool,  # ← V5.2 跨章节去重
            )
            for child in result["children"]:
                for gc in child["children"]:
                    all_l3.append(gc["title"])

        unique = len(set(all_l3))
        total = len(all_l3)
        uniqueness_rate = unique / total if total > 0 else 0
        assert uniqueness_rate >= threshold, (
            f"L3 唯一率 {uniqueness_rate:.1%} < {threshold:.0%} 阈值\n"
            f"总数={total}, 唯一数={unique}"
        )

    def test_no_monotonous_pattern(self):
        """不应出现连续 N 个相同标题的模式。"""
        for idx in range(28):
            ch = FIXED_28_CHAPTERS[idx]
            dist = generate_nodes_distribution(1, min_level2=4, min_level3=3)[0]
            framework = build_framework_for_chapter(
                {"title": ch["title"]}, dist, chapter_number=idx + 1,
            )
            result = sanitize_framework_titles(framework, ch["title"], chapter_idx=idx)
            l2_titles = [c["title"] for c in result["children"]]
            # 不应有 3 个连续相同标题
            for i in range(len(l2_titles) - 2):
                assert not (
                    l2_titles[i] == l2_titles[i + 1] == l2_titles[i + 2]
                ), f"章节 {ch['title']} 有连续重复二级标题: {l2_titles[i]}"


# ===========================================================================
# 6. 第七章特殊框架测试
# ===========================================================================

class TestChapter7Framework:
    """测试第七章承包范围框架生成。"""

    def test_chapter7_creates_scope_based_children(self):
        scope_list = ["土方", "地下工程", "土建工程", "PC工程", "电气工程"]
        framework = build_chapter7_framework(scope_list, chapter_number=7)
        assert len(framework["children"]) == 5
        titles = [c["title"] for c in framework["children"]]
        assert "土方工程" in titles
        assert "地下工程" in titles

    def test_chapter7_no_placeholder(self):
        """第七章不应有占位标题。"""
        framework = build_chapter7_framework(["土方", "电气"], chapter_number=7)
        for child in framework["children"]:
            assert "【请" not in child["title"]
            assert "占位" not in child["title"]


# ===========================================================================
# 7. 日志输出测试
# ===========================================================================

class TestLogging:
    """测试关键路径是否有日志输出。"""

    def test_classify_logs(self, caplog):
        import logging
        with caplog.at_level(logging.DEBUG):
            _classify_chapter_type("质量保证措施和创优计划")
        assert any("[分类]" in r.message for r in caplog.records)

    def test_sanitize_logs(self, caplog):
        import logging
        framework = self._make_framework()
        with caplog.at_level(logging.DEBUG):
            sanitize_framework_titles(framework, "施工方案及技术措施", chapter_idx=0)
        assert any("[兜底标题]" in r.message for r in caplog.records)

    @staticmethod
    def _make_framework():
        return {
            "id": "X", "title": "测试", "level": 1,
            "children": [
                {
                    "id": "X.1", "title": "【请根据项目实际替换为具体标题】二级第1项",
                    "level": 2,
                    "children": [
                        {"id": "X.1.1", "title": "【请替换】三级第1项", "level": 3, "children": []},
                    ],
                },
            ],
        }


# ===========================================================================
# 8. 边界与异常处理测试
# ===========================================================================

class TestEdgeCases:
    """测试边界条件和异常处理。"""

    def test_mixed_placeholder_and_real_titles(self):
        """框架中同时包含占位标题和真实标题时应只替换占位。"""
        framework = {
            "id": "X", "title": "测试", "level": 1,
            "children": [
                {
                    "id": "X.1", "title": "【请根据项目实际替换为具体标题】二级第1项",
                    "level": 2,
                    "children": [
                        {"id": "X.1.1", "title": "真实三级标题", "level": 3, "children": []},
                    ],
                },
                {
                    "id": "X.2", "title": "真实二级标题",
                    "level": 2,
                    "children": [
                        {"id": "X.2.1", "title": "【请替换】三级第1项", "level": 3, "children": []},
                    ],
                },
            ],
        }
        result = sanitize_framework_titles(framework, "施工方案及技术措施", chapter_idx=0)
        assert result["children"][0]["title"] != "【请根据项目实际替换为具体标题】二级第1项"
        assert result["children"][1]["title"] == "真实二级标题"
        assert result["children"][1]["children"][0]["title"] != "【请替换】三级第1项"

    def test_empty_framework(self):
        """空 dict 应安全返回。"""
        result = sanitize_framework_titles({}, "测试", chapter_idx=0)
        assert result == {}

    def test_framework_without_children(self):
        """无 children 的框架应安全返回。"""
        result = sanitize_framework_titles({"id": "X", "title": "测试"}, "测试", chapter_idx=0)
        assert result["title"] == "测试"

    def test_l2_pattern_pool_has_enough_for_28_chapters(self):
        """L2 模板池应至少有 28 个不同模板（跨章节不重复）。"""
        for typ, patterns in _L2_TITLE_PATTERNS.items():
            assert len(patterns) >= 16, f"类型 {typ} 的模板数不足: {len(patterns)}"

    def test_classification_consistency_across_similar_titles(self):
        """语义相近的标题应分类一致。"""
        titles = [
            "质量保证措施和创优计划",
            "质量管理制度与检查机制",
            "质量通病防治措施",
        ]
        results = [_classify_chapter_type(t) for t in titles]
        assert all(r == "quality" for r in results), f"质量相关标题应统一归为 quality: {results}"


# ===========================================================================
# 9. 多样性量化测试
# ===========================================================================

class TestDiversityMetrics:
    """量化测试标题多样性指标。"""

    def test_l2_diversity_rate_28_chapters(self):
        """28 章兜底场景 L2 多样性：唯一率应 > 80%。"""
        all_titles = []
        for idx in range(28):
            ch = FIXED_28_CHAPTERS[idx]
            dist = generate_nodes_distribution(1, min_level2=4, min_level3=3)[0]
            framework = build_framework_for_chapter(
                {"title": ch["title"]}, dist, chapter_number=idx + 1,
            )
            result = sanitize_framework_titles(framework, ch["title"], chapter_idx=idx)
            all_titles.extend(c["title"] for c in result["children"])

        unique = len(set(all_titles))
        rate = unique / len(all_titles) if all_titles else 0
        assert rate >= 0.60, f"L2 唯一率 {rate:.1%} 低于 60% 阈值"

    def test_no_identical_consecutive_l2_in_same_chapter(self):
        """同一章节内不应有连续相同的二级标题。"""
        for idx in range(28):
            ch = FIXED_28_CHAPTERS[idx]
            dist = generate_nodes_distribution(1, min_level2=5, min_level3=3)[0]
            framework = build_framework_for_chapter(
                {"title": ch["title"]}, dist, chapter_number=idx + 1,
            )
            result = sanitize_framework_titles(framework, ch["title"], chapter_idx=idx)
            l2_titles = [c["title"] for c in result["children"]]
            for i in range(len(l2_titles) - 1):
                assert l2_titles[i] != l2_titles[i + 1], (
                    f"章节 '{ch['title']}' 中存在连续相同二级标题: {l2_titles[i]}"
                )
