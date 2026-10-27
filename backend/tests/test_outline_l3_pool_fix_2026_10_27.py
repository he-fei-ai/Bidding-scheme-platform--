"""目录生成模块：L3 池污染与 L2-L3 重叠修复（2026-10-27）。

针对《岳阳路园区科研南楼室内装修工程》docx 实测数据，修复以下缺陷：

BUG-A（6 例）：L3 标题 = 同级 L2 标题
  根因：`_L3_SUFFIX_POOL["org"]` 的 5 个条目全部是 L2 级章名
  （「组织架构与职责分工」「管理制度与考核机制」等），
  导致 L3 直接吐出 L2 章名。

BUG-D（15 例）：L3 pool 污染
  根因：`_L3_SUFFIX_POOL` 中多个域（org、cost、work）的条目
  与 `_L2_SUFFIX_POOL` 完全相同，L3 池直接吐出 L2 章名。

BUG-B（70 例）：跨 L2 三级重复
  根因：L3 pool 容量不足，跨 L2 复用同一批 L3 标题。

BUG-C（16 例）：跨 L1 二级重复
  根因：L2 pool 容量不足，跨 L1 复用同一批 L2 标题。

修复：
  1. 替换 `_L3_SUFFIX_POOL` 中与 L2 池重复的条目为动作型条目
  2. 扩展 `_L3_L2LIKE_TITLES` 拦截旧数据中残留的 L2 级 L3 标题
  3. 增大 org 域 L3 pool 容量（5→8），缓解跨 L2 重复

回归测试：验证 L3 pool 不再包含 L2 级章名，
且新增的动作型条目不会被脏判定误杀。
"""
from __future__ import annotations

from app.services.ai.outline_common import (
    _L2_SUFFIX_POOL,
    _L3_L2LIKE_TITLES,
    _L3_SUFFIX_POOL,
    _is_dirty_l3_title,
    _is_dirty_l3_title_deep,
    _cleanup_placeholder_titles,
    _uniquify_outline_titles,
    _derive_child_title,
    _detect_domain,
    _semantic_fallback_title,
)


class TestL3PoolNoL2Contamination:
    """L3 pool 不得包含 L2 级章名（BUG-A/D 根因修复）。"""

    def test_no_exact_overlap_between_l2_and_l3_pools(self):
        """每个域中，L3 pool 条目不得与 L2 pool 条目完全相同。"""
        for domain in set(_L2_SUFFIX_POOL.keys()) | set(_L3_SUFFIX_POOL.keys()):
            l2_titles = set(_L2_SUFFIX_POOL.get(domain, ()))
            l3_titles = set(_L3_SUFFIX_POOL.get(domain, ()))
            overlap = l2_titles & l3_titles
            assert not overlap, (
                f"域 {domain!r} 的 L3 pool 包含 L2 pool 条目: {overlap}。"
                f"L3 pool 条目必须为动作型，不得直接复用 L2 章名。"
            )

    def test_org_l3_pool_replaced_with_action_items(self):
        """org 域 L3 pool 已从 L2 级章名替换为动作型条目。"""
        org_l3 = _L3_SUFFIX_POOL["org"]
        # 旧值（L2 级章名）不得出现
        l2_level_titles = {
            "组织架构与职责分工",
            "管理制度与考核机制",
            "沟通协调与例会制度",
            "人才配置与梯队建设",
            "信息化管理与报表",
        }
        for title in org_l3:
            assert title not in l2_level_titles, (
                f"org 域 L3 pool 仍含 L2 级章名: {title!r}"
            )
        # 新值应为动作型条目
        assert len(org_l3) >= 8, (
            f"org 域 L3 pool 容量应为 ≥8（实际 {len(org_l3)}），"
            f"以缓解跨 L2 重复问题"
        )

    def test_work_l3_pool_replaced(self):
        """work 域 L3 pool 中与 L2 池重复的条目已替换。"""
        work_l3 = _L3_SUFFIX_POOL["work"]
        l2_level_titles = {"进度计划与资源保障", "成品保护与现场管理"}
        for title in work_l3:
            assert title not in l2_level_titles, (
                f"work 域 L3 pool 仍含 L2 级章名: {title!r}"
            )

    def test_cost_l3_pool_replaced(self):
        """cost 域 L3 pool 中与 L2 池重复的条目已替换。"""
        cost_l3 = _L3_SUFFIX_POOL["cost"]
        l2_level_titles = {"目标成本分解与控制"}
        for title in cost_l3:
            assert title not in l2_level_titles, (
                f"cost 域 L3 pool 仍含 L2 级章名: {title!r}"
            )


class TestL3L2LikeTitlesExpanded:
    """_L3_L2LIKE_TITLES 拦截集已扩展，覆盖旧 L3 pool 中的 L2 级章名。"""

    def test_old_l2_level_titles_in_l3_pool_are_intercepted(self):
        """旧 L3 pool 中的 L2 级章名应在 _L3_L2LIKE_TITLES 中。"""
        old_l2_level_in_l3_pool = {
            "组织架构与职责分工",
            "管理制度与考核机制",
            "进度计划与资源保障",
            "成品保护与现场管理",
            "目标成本分解与控制",
            "成本核算与偏差分析",
        }
        for title in old_l2_level_in_l3_pool:
            assert title in _L3_L2LIKE_TITLES, (
                f"旧 L3 pool 条目 {title!r} 应在 _L3_L2LIKE_TITLES 中，"
                f"以便拦截旧数据中残留的 L2 级 L3 标题"
            )

    def test_new_l3_pool_items_not_intercepted(self):
        """新 L3 pool 条目不应在 _L3_L2LIKE_TITLES 中（否则会被误杀）。"""
        all_l3_titles = set()
        for domain in _L3_SUFFIX_POOL:
            all_l3_titles.update(_L3_SUFFIX_POOL[domain])
        for title in all_l3_titles:
            assert title not in _L3_L2LIKE_TITLES, (
                f"新 L3 pool 条目 {title!r} 不应在 _L3_L2LIKE_TITLES 中，"
                f"否则会被脏判定误杀"
            )


class TestL2L3Differentiation:
    """L3 生成不再产出 L2 级章名（BUG-A 修复验证）。"""

    def test_derive_l3_from_org_l2_not_l2_title(self):
        """从 org 域 L2 生成 L3 时，不应产出 L2 级章名。"""
        used = set()
        l2_title = "组织架构与职责分工"
        l3 = _derive_child_title(l2_title, 0, used, level=3)
        assert l3 != l2_title, f"L3 不应等于 L2 标题: {l3!r}"
        assert l3 not in _L2_SUFFIX_POOL.get("org", ()), (
            f"L3 {l3!r} 不应是 L2 池条目"
        )

    def test_semantic_fallback_not_l2_title(self):
        """语义化兜底标题不应是 L2 级章名。"""
        used = set()
        result = _semantic_fallback_title("组织架构与职责分工", 0, used, context_title="项目管理")
        assert result not in _L2_SUFFIX_POOL.get("org", ()), (
            f"语义化兜底不应产出 L2 池条目: {result!r}"
        )
        assert result not in _L3_L2LIKE_TITLES, (
            f"语义化兜底不应产出 L2 级章名: {result!r}"
        )


class TestDirtyDetectionOfOldL2LevelL3:
    """端到端验证：完整管道不产出 L3 = L2 标题的碰撞。"""

    def _build_org_domain_tree(self):
        """构造与 docx 相同的 org 域目录树结构。"""
        return [{
            "title": "第一节 项目组织管理体系与岗位职责方案",
            "children": [
                {"title": "一、组织架构与职责分工", "children": [
                    {"title": "管理制度与考核机制", "children": []},
                    {"title": "沟通协调与例会制度", "children": []},
                    {"title": "人才配置与梯队建设", "children": []},
                    {"title": "信息化管理与报表", "children": []},
                    {"title": "项目组织人员培训", "children": []},
                    {"title": "项目组织制度落实", "children": []},
                ]},
                {"title": "二、管理制度与考核机制", "children": [
                    {"title": "组织架构与职责分工", "children": []},
                    {"title": "沟通协调与例会制度", "children": []},
                    {"title": "人才配置与梯队建设", "children": []},
                    {"title": "信息化管理与报表", "children": []},
                    {"title": "项目组织人员培训", "children": []},
                    {"title": "项目组织制度落实", "children": []},
                    {"title": "项目组织绩效考核", "children": []},
                    {"title": "技术交底实施", "children": []},
                    {"title": "专业分包例会制度", "children": []},
                    {"title": "人员培训交底", "children": []},
                ]},
            ],
        }]

    def test_l2_like_l3_titles_are_dirty(self):
        """旧数据中残留的 L2 级 L3 标题应被判为脏标题。"""
        l2_level_l3_titles = [
            "组织架构与职责分工",
            "管理制度与考核机制",
            "进度计划与资源保障",
            "成品保护与现场管理",
            "目标成本分解与控制",
        ]
        for title in l2_level_l3_titles:
            assert _is_dirty_l3_title_deep(title), (
                f"旧 L3 pool 中的 L2 级章名 {title!r} 应被判为脏标题"
            )

    def test_new_action_titles_not_dirty(self):
        """新 L3 pool 中的动作型条目不应被判为脏标题。"""
        action_titles = [
            "组织架构设置与职责明确",
            "管理制度建立与考核实施",
            "例会组织与纪要归档",
            "人员培训与技能认证",
            "信息化平台建设与数据管理",
            "绩效考核与奖惩实施",
            "岗位配置与职责落实",
            "沟通协调与问题反馈",
            "成本目标分解与动态控制",
            "进度安排与资源协调",
            "成品保护与文明施工",
        ]
        for title in action_titles:
            assert not _is_dirty_l3_title_deep(title), (
                f"新动作型 L3 条目 {title!r} 不应被判为脏标题"
            )

    def test_cleanup_removes_l2_level_l3(self):
        """清洗器应移除旧数据中残留的 L2 级 L3 标题。"""
        tree = self._build_org_domain_tree()
        # 统计修复前 L2 级 L3 数量
        l2_titles = {
            "组织架构与职责分工",
            "管理制度与考核机制",
        }
        before_count = sum(
            1
            for l1 in tree
            for l2 in l1["children"]
            for l3 in l2["children"]
            if l3["title"] in l2_titles
        )
        assert before_count > 0, "测试数据应包含 L2 级 L3 标题"

        # 运行清洗
        _cleanup_placeholder_titles(tree)
        tree = _uniquify_outline_titles(tree)

        # 验证修复后
        after_count = sum(
            1
            for l1 in tree
            for l2 in l1["children"]
            for l3 in l2["children"]
            if l3["title"] in l2_titles
        )
        assert after_count == 0, (
            f"清洗后仍有 {after_count} 个 L2 级 L3 标题残留"
        )
