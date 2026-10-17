"""L3 标题修复与加固回归（2026-10-16 轮次）。

覆盖本轮三处加固，每条用例在旧实现下必然失败：

1. 技术增强「洗白」防线（`_tech_enhance_titles`）：
   - 脏输入（「总体实施策划」）不得被垫技术词前缀后逃过收口链词面判定
     （规则 6 需整段相等、泛化词干判定需词面起始命中 →「智能建造总体
     实施策划」会洗白成无法识别的泛化残留）；
   - 增强产物本身命中脏判定时放弃增强，保留原标题（G4 同口径最后防线）；
   - 干净但缺技术含量的标题仍正常增强（确保加固未误伤功能本体）。

2. `_post_process_title_quality` 早期清洗并入统一深度脏判定：
   局部 `_GENERIC_TITLE_PATTERNS` 仅 8 条锚定式正则，带前缀占位变体
   （「分阶段推进专项13」）在 L2 审核点前无人拦截 → 提前到该函数拦截，
   人工审核看到已净化目录。

3. AI 修复结果双重撞名防线（`_repair_title_conflicts`）：
   - 同级撞名：拒绝接收，避免同级去重改名波及既有合法标题；
   - 祖先撞名（L3 复述 L2）：同级去重只处理兄弟节点，父子重名没有任何
     去重防线，修复阶段新产生的复述会原样流入终态。
"""
import asyncio

from app.services.ai.outline_common import (
    _is_dirty_l3_title_deep,
    _title_core,
)
from app.services.ai.workflows_outline_utils import repair_l3_generic_titles
from app.services.ai.workflows_outline_v51 import (
    _post_process_title_quality,
    _tech_enhance_titles,
)


def _tree(l1: str, l2: str, l3_titles: list[str]) -> list[dict]:
    return [
        {
            "title": l1,
            "children": [
                {
                    "title": l2,
                    "children": [
                        {"title": t, "level": 3, "children": []}
                        for t in l3_titles
                    ],
                }
            ],
        }
    ]


# ===========================================================================
# 1. 技术增强：脏输入不洗白 / 脏产物不接收 / 干净输入仍增强
# ===========================================================================


class TestTechEnhanceDirtyGuard:
    def test_dirty_input_not_masked_by_tech_prefix(self, monkeypatch):
        """脏标题不做技术增强 —— 垫前缀会使其逃过收口链的词面判定。"""
        from app.services.ai import tech_knowledge

        # is_generic_title 可能因词表演进漏判，这里钉死为 True，
        # 确保本用例只考察脏输入防线本身。
        monkeypatch.setattr(tech_knowledge, "is_generic_title", lambda t: True)
        tree = _tree("总体施工部署", "总体方案与实施策划", ["总体实施策划"])
        out, fixes = _tech_enhance_titles(tree)
        assert fixes == 0, "脏标题被技术增强洗白（占位残留直达落库路径）"
        assert out[0]["children"][0]["children"][0]["title"] == "总体实施策划"

    def test_dirty_enhanced_output_is_rejected(self, monkeypatch):
        """增强产物命中脏判定 → 放弃增强保留原标题。"""
        from app.services.ai import tech_knowledge
        import app.services.ai.workflows_outline_v51 as v51

        monkeypatch.setattr(tech_knowledge, "is_generic_title", lambda t: True)
        monkeypatch.setattr(
            v51, "_generate_tech_enhanced_title", lambda *a, **k: "专项6"
        )
        tree = _tree("质量管理措施", "质量保证组织体系", ["样板区质量控制"])
        out, fixes = _tech_enhance_titles(tree)
        assert fixes == 0, "增强产物为占位标题仍被接收"
        assert out[0]["children"][0]["children"][0]["title"] == "样板区质量控制"

    def test_clean_generic_title_still_enhanced(self, monkeypatch):
        """加固不误伤功能本体：干净标题仍正常增强且产物非脏。"""
        from app.services.ai import tech_knowledge

        monkeypatch.setattr(tech_knowledge, "is_generic_title", lambda t: True)
        tree = _tree("质量管理措施", "质量保证组织体系", ["样板区质量控制"])
        out, fixes = _tech_enhance_titles(tree)
        new_title = out[0]["children"][0]["children"][0]["title"]
        assert fixes == 1, "干净的通用标题应被增强"
        assert new_title != "样板区质量控制"
        assert not _is_dirty_l3_title_deep(new_title), f"增强产物为脏标题: {new_title}"


# ===========================================================================
# 2. _post_process_title_quality 早期清洗并入统一深度脏判定
# ===========================================================================


class TestEarlyCleanupUnifiedDetection:
    def test_prefixed_placeholder_caught_before_l2_review(self):
        """「分阶段推进专项13」这类带前缀占位变体在 L2 审核点前即被拦截。"""
        tree = [
            {
                "title": "总体施工部署",
                "children": [
                    {
                        "title": "分阶段推进",
                        "children": [
                            {"title": "分阶段推进专项13"},
                            {"title": "样板区质量控制"},
                        ],
                    }
                ],
            }
        ]
        out, fixes = _post_process_title_quality(tree)
        l3 = out[0]["children"][0]["children"]
        assert fixes >= 1, "带前缀占位变体未被早期清洗（旧 8 条锚定正则漏检）"
        assert l3[0]["title"] != "分阶段推进专项13"
        assert not _is_dirty_l3_title_deep(l3[0]["title"]), f"早期清洗产物仍脏: {l3[0]}"
        # 具体标题与合法章节名不得误伤
        assert l3[1]["title"] == "样板区质量控制"
        assert out[0]["title"] == "总体施工部署"
        assert out[0]["children"][0]["title"] == "分阶段推进"

    def test_early_cleanup_preserves_sibling_uniqueness(self):
        """多个同父占位标题清洗后不得重名（清洗链与去重口径一致）。"""
        tree = [
            {
                "title": "总体施工部署",
                "children": [
                    {
                        "title": "分阶段推进",
                        "children": [
                            {"title": "分阶段推进专项13"},
                            {"title": "分阶段推进专项14"},
                            {"title": "样板区质量控制"},
                        ],
                    }
                ],
            }
        ]
        out, _ = _post_process_title_quality(tree)
        titles = [c["title"] for c in out[0]["children"][0]["children"]]
        cores = [_title_core(t) for t in titles]
        assert len(set(cores)) == len(cores), f"清洗后同级撞名: {titles}"
        assert not [t for t in titles if _is_dirty_l3_title_deep(t)], titles


# ===========================================================================
# 3. AI 修复结果双重撞名防线（同级 / 祖先）
# ===========================================================================


class _SiblingEchoProvider:
    """AI 复读既有同级标题的探针：强制触发同级撞名拒绝路径。"""

    async def chat(self, messages, **kwargs):
        return "地下室防水施工工艺"


class _ParentEchoProvider:
    """AI 复读父级 L2 标题的探针：强制触发「三级复述二级」拒绝路径。"""

    async def chat(self, messages, **kwargs):
        return "地下室防水施工方案"


class TestRepairResultCollisionGuard:
    def test_repair_rejects_sibling_collision(self):
        tree = _tree(
            "总体施工部署",
            "总体方案与实施策划",
            ["地下室防水施工工艺", "总体实施策划（2）", "专项6"],
        )
        out = asyncio.run(
            repair_l3_generic_titles(
                _SiblingEchoProvider(), tree, "", "", "", "", "", max_repair=10
            )
        )
        titles = [c["title"] for c in out[0]["children"][0]["children"]]
        cores = [_title_core(t) for t in titles]
        assert len(set(cores)) == len(cores), f"AI 修复结果同级撞名漏出: {titles}"
        assert not [t for t in titles if _is_dirty_l3_title_deep(t)], titles
        # 既有合法标题保持原样（不得被同级去重波及）
        assert titles[0] == "地下室防水施工工艺"

    def test_repair_rejects_ancestor_collision(self):
        """AI 返回父级 L2 标题 → 拒绝（父子重名无任何去重防线）。"""
        tree = _tree(
            "总体施工部署",
            "地下室防水施工方案",
            ["地下室防水施工方案支撑体系要点", "总体实施策划（2）"],
        )
        out = asyncio.run(
            repair_l3_generic_titles(
                _ParentEchoProvider(), tree, "", "", "", "", "", max_repair=10
            )
        )
        titles = [c["title"] for c in out[0]["children"][0]["children"]]
        assert titles[1] != "地下室防水施工方案", "修复结果复述了父级 L2 标题"
        cores = [_title_core(t) for t in titles]
        assert len(set(cores)) == len(cores), f"同级撞名漏出: {titles}"
        assert not [t for t in titles if _is_dirty_l3_title_deep(t)], titles

    def test_repair_accepts_unique_nonconflicting_result(self):
        """无撞名的高质量 AI 结果仍正常接收（确保加固未误伤主路径）。"""

        class _GoodProvider:
            async def chat(self, messages, **kwargs):
                return "高大模板支撑体系监测预警"

        tree = _tree(
            "总体施工部署",
            "总体方案与实施策划",
            ["地下室防水施工工艺", "总体实施策划（2）"],
        )
        out = asyncio.run(
            repair_l3_generic_titles(
                _GoodProvider(), tree, "", "", "", "", "", max_repair=10
            )
        )
        titles = [c["title"] for c in out[0]["children"][0]["children"]]
        assert "高大模板支撑体系监测预警" in titles, f"无撞名 AI 结果被误拒: {titles}"
        assert not [t for t in titles if _is_dirty_l3_title_deep(t)], titles
