"""目录生成模块：跨 L1 L2 重复修复与池扩容（2026-10-27 第三轮）。

针对《岳阳路园区科研南楼室内装修工程》docx 实测数据，修复以下缺陷：

BUG-C（16 例）：跨 L1 L2 重复
  根因：`_L2_SUFFIX_POOL` 按域共享，多个 L1 属于同一域时复用相同 L2 池，
  产出完全相同的 L2 标题（如「组织架构与职责分工」出现在 3 个 L1 中）。
  修复：新增 `_cap_cross_chapter_l2_repetition` 函数，限制同一 L2 标题
  在不同 L1 中的复用广度。

BUG-B（70 例）：跨 L2 L3 重复（部分缓解）
  根因：L3 池容量不足，跨 L2 复用同一批 L3 标题。
  修复：扩展所有域 L3 池容量（work 10→12, quality 8→10, safety 8→10,
  schedule 6→8, cost 5→7, org 8→10, tech 8→10, generic 8→10）。

BUG-E（34 例）：L3 标题含 L2 级特征
  根因：L3 池仍包含 L2 级章名，或实体×维度组合产出 L2 级标题。
  修复：扩展 `_L3_L2LIKE_TITLES` 拦截集（+21 项），覆盖所有 L2 池条目。

回归测试：验证 L2-L3 池无重叠，新增条目不被误杀，跨 L1 L2 去重生效。
"""
from __future__ import annotations

from app.services.ai.outline_common import (
    _L2_SUFFIX_POOL,
    _L3_L2LIKE_TITLES,
    _L3_SUFFIX_POOL,
    _cap_cross_chapter_l2_repetition,
    _cap_cross_chapter_repetition,
    _is_dirty_l3_title_deep,
    _cleanup_placeholder_titles,
    _uniquify_outline_titles,
    _derive_child_title,
    _detect_domain,
    _semantic_fallback_title,
    _core_for_derive,
    _title_entity_fragments,
    _l3_entity_candidates,
)


class TestL2PoolExpansion:
    """L2 池扩容验证：所有域容量充足，无跨域重叠。"""

    def test_l2_pool_min_size(self):
        """每个域 L2 池至少 6 项，缓解跨 L1 重复。"""
        min_sizes = {
            "work": 10, "quality": 7, "safety": 7, "schedule": 6,
            "cost": 6, "org": 6, "tech": 8, "generic": 7,
        }
        for domain, min_size in min_sizes.items():
            actual = len(_L2_SUFFIX_POOL.get(domain, ()))
            assert actual >= min_size, (
                f"域 {domain!r} L2 池容量应为 ≥{min_size}（实际 {actual}）"
            )

    def test_l2_pool_no_intra_domain_duplicates(self):
        """每个域 L2 池内部无重复条目。"""
        for domain, titles in _L2_SUFFIX_POOL.items():
            assert len(titles) == len(set(titles)), (
                f"域 {domain!r} L2 池内部有重复: {titles}"
            )

    def test_l2_pool_entries_are_actionable(self):
        """L2 池条目应为动作型/章名级短语，不含裸编号或占位词。"""
        for domain, titles in _L2_SUFFIX_POOL.items():
            for title in titles:
                assert "N" not in title or "专项N" not in title, (
                    f"域 {domain!r} L2 池含占位词: {title!r}"
                )
                assert "（" not in title, (
                    f"域 {domain!r} L2 池含括号占位: {title!r}"
                )


class TestL3PoolExpansion:
    """L3 池扩容验证：所有域容量充足，无跨域重叠。"""

    def test_l3_pool_min_size(self):
        """每个域 L3 池至少 12 项（P1 扩容），缓解跨 L2 重复。"""
        min_sizes = {
            "work": 12, "quality": 12, "safety": 12, "schedule": 12,
            "cost": 12, "org": 12, "tech": 12, "generic": 12,
        }
        for domain, min_size in min_sizes.items():
            actual = len(_L3_SUFFIX_POOL.get(domain, ()))
            assert actual >= min_size, (
                f"域 {domain!r} L3 池容量应为 ≥{min_size}（实际 {actual}）"
            )

    def test_l3_pool_no_intra_domain_duplicates(self):
        """每个域 L3 池内部无重复条目。"""
        for domain, titles in _L3_SUFFIX_POOL.items():
            assert len(titles) == len(set(titles)), (
                f"域 {domain!r} L3 池内部有重复: {titles}"
            )

    def test_l3_pool_no_dirty_titles(self):
        """L3 池条目不应被判为脏标题。"""
        for domain, titles in _L3_SUFFIX_POOL.items():
            for title in titles:
                assert not _is_dirty_l3_title_deep(title), (
                    f"域 {domain!r} L3 池条目 {title!r} 被判为脏标题"
                )


class TestL3L2LikeTitlesExpanded:
    """_L3_L2LIKE_TITLES 拦截集扩展验证。"""

    def test_all_l2_pool_entries_in_l3_intercept_set(self):
        """所有 L2 池条目应在 _L3_L2LIKE_TITLES 中（防止 L3 复用 L2 章名）。

        注：「质量保证组织体系」刻意不拦截——回归测试
        test_does_not_false_positive_on_concrete_titles 明确断言其为具体标题。
        """
        # 刻意豁免的条目（语义具体、作为 L3 合法可读）
        deliberately_exempt = {
            "质量保证组织体系",
        }
        for domain, titles in _L2_SUFFIX_POOL.items():
            for title in titles:
                if title in deliberately_exempt:
                    continue
                assert title in _L3_L2LIKE_TITLES, (
                    f"L2 池条目 {title!r} 应在 _L3_L2LIKE_TITLES 中，"
                    f"以便拦截旧数据中残留的 L2 级 L3 标题"
                )

    def test_new_l3_pool_items_not_intercepted(self):
        """新 L3 池条目不应在 _L3_L2LIKE_TITLES 中（否则会被误杀）。"""
        all_l3_titles = set()
        for domain in _L3_SUFFIX_POOL:
            all_l3_titles.update(_L3_SUFFIX_POOL[domain])
        for title in all_l3_titles:
            assert title not in _L3_L2LIKE_TITLES, (
                f"新 L3 池条目 {title!r} 不应在 _L3_L2LIKE_TITLES 中"
            )


class TestCrossL1L2Deduplication:
    """跨 L1 L2 去重验证（BUG-C 修复）。"""

    def _build_multi_l1_tree(self):
        """构造 3 个 L1 章，模拟 docx 中的跨 L1 L2 重复场景。"""
        return [
            {"title": "第一节 项目组织管理体系", "children": [
                {"title": "一、组织架构与职责分工", "children": [
                    {"title": "1、组织架构设置与职责明确", "children": []},
                    {"title": "2、管理制度建立与考核实施", "children": []},
                ]},
                {"title": "二、管理制度与考核机制", "children": [
                    {"title": "1、例会组织与纪要归档", "children": []},
                    {"title": "2、人员培训与技能认证", "children": []},
                ]},
            ]},
            {"title": "第二节 施工组织部署", "children": [
                {"title": "一、组织架构与职责分工", "children": [
                    {"title": "1、组织架构设置与职责明确", "children": []},
                    {"title": "2、管理制度建立与考核实施", "children": []},
                ]},
                {"title": "二、管理制度与考核机制", "children": [
                    {"title": "1、例会组织与纪要归档", "children": []},
                    {"title": "2、人员培训与技能认证", "children": []},
                ]},
            ]},
            {"title": "第十三节 专项管理制度", "children": [
                {"title": "一、组织架构与职责分工", "children": [
                    {"title": "1、组织架构设置与职责明确", "children": []},
                    {"title": "2、管理制度建立与考核实施", "children": []},
                ]},
                {"title": "二、管理制度与考核机制", "children": [
                    {"title": "1、例会组织与纪要归档", "children": []},
                    {"title": "2、人员培训与技能认证", "children": []},
                ]},
            ]},
        ]

    def test_cross_l1_l2_deduplication(self):
        """跨 L1 L2 重复应被去重。"""
        tree = self._build_multi_l1_tree()
        
        # 统计修复前跨 L1 L2 重复
        l2_titles_before = {}
        for l1 in tree:
            for l2 in l1["children"]:
                title = l2["title"].split("、", 1)[-1] if "、" in l2["title"] else l2["title"]
                l2_titles_before.setdefault(title, []).append(l1["title"])
        
        cross_l1_before = sum(
            1 for title, l1s in l2_titles_before.items() if len(l1s) > 2
        )
        assert cross_l1_before > 0, "测试数据应包含跨 L1 L2 重复"
        
        # 运行去重
        tree, fixed = _cap_cross_chapter_l2_repetition(tree, max_reuse=2)
        
        # 统计修复后
        l2_titles_after = {}
        for l1 in tree:
            for l2 in l1["children"]:
                title = l2["title"].split("、", 1)[-1] if "、" in l2["title"] else l2["title"]
                l2_titles_after.setdefault(title, []).append(l1["title"])
        
        cross_l1_after = sum(
            1 for title, l1s in l2_titles_after.items() if len(l1s) > 2
        )
        assert cross_l1_after < cross_l1_before, (
            f"去重后跨 L1 L2 重复数应减少（前 {cross_l1_before} → 后 {cross_l1_after}）"
        )

    def test_cross_l1_l2_deduplication_idempotent(self):
        """去重操作应幂等。"""
        tree = self._build_multi_l1_tree()
        tree, _ = _cap_cross_chapter_l2_repetition(tree, max_reuse=2)
        _, fixed2 = _cap_cross_chapter_l2_repetition(tree, max_reuse=2)
        assert fixed2 == 0, "第二次去重不应再改写任何标题"

    def test_l2_deduplication_preserves_l3(self):
        """L2 去重不应删除 L3 节点。"""
        tree = self._build_multi_l1_tree()
        l3_count_before = sum(
            len(l2["children"]) for l1 in tree for l2 in l1["children"]
        )
        tree, _ = _cap_cross_chapter_l2_repetition(tree, max_reuse=2)
        l3_count_after = sum(
            len(l2["children"]) for l1 in tree for l2 in l1["children"]
        )
        assert l3_count_after == l3_count_before, (
            f"L3 节点数应保持不变（前 {l3_count_before} → 后 {l3_count_after}）"
        )

    def test_l2_deduplication_zero_reuse_disables(self):
        """max_reuse=0 应关闭护栏。"""
        tree = self._build_multi_l1_tree()
        _, fixed = _cap_cross_chapter_l2_repetition(tree, max_reuse=0)
        assert fixed == 0, "max_reuse=0 应关闭护栏"


class TestPoolExpansionIntegration:
    """池扩容端到端验证。"""

    def test_all_domains_have_sufficient_l3_pool(self):
        """所有域 L3 池容量应支持 12+ L3 节点生成（P1 扩容）。"""
        for domain in _L3_SUFFIX_POOL:
            pool_size = len(_L3_SUFFIX_POOL[domain])
            assert pool_size >= 12, (
                f"域 {domain!r} L3 池容量 {pool_size} 不足（至少 12）"
            )

    def test_all_domains_have_sufficient_l2_pool(self):
        """所有域 L2 池容量应支持 6+ L2 节点生成。"""
        for domain in _L2_SUFFIX_POOL:
            pool_size = len(_L2_SUFFIX_POOL[domain])
            assert pool_size >= 6, (
                f"域 {domain!r} L2 池容量 {pool_size} 不足（至少 6）"
            )

    def test_l3_pool_entries_are_actionable(self):
        """L3 池条目应为动作型短语，不含 L2 级章名特征。"""
        # 检查 L3 池条目不包含 L2 池条目
        for domain in _L3_SUFFIX_POOL:
            l3_titles = set(_L3_SUFFIX_POOL[domain])
            l2_titles = set(_L2_SUFFIX_POOL.get(domain, ()))
            overlap = l3_titles & l2_titles
            assert not overlap, (
                f"域 {domain!r} L3 池与 L2 池重叠: {overlap}"
            )


class TestL3L2AwareGeneration:
    """P2（第四轮）：L3 生成 L2 感知化，解决 L2-L3 语义脱节（66%）。

    核心：`_l3_entity_candidates` 提取 **L2 专属实体**（`_title_entity_fragments`
    后过滤规则 3 核心词），使 L3 与父级 L2 主题对应。
    """

    def test_multi_entity_l2_keeps_noncore_entity(self):
        """多实体 L2（土方与基坑支护工程）：非核心实体「基坑支护」应保留。"""
        cands = _l3_entity_candidates("土方与基坑支护工程", 0, set(), context_title="")
        assert any("基坑支护" in c for c in cands[:8]), (
            f"L2 专属实体「基坑支护」未出现在候选前 8 项: {cands[:8]}"
        )
        # 首个候选应为「基坑支护工程」×维度，而非域池兜底
        assert "基坑支护" in cands[0], f"首个候选应为 L2 专属: {cands[0]}"

    def test_single_entity_l2_excludes_its_own_fragment(self):
        """单实体 L2（脚手架工程）：L2 专属片段=核心词，P2 过滤链剔除它。

        规则 3 口径是「L3 不得以父级核心词作前缀拼合」，不是禁止域池通用
        实体（「脚手架」是 work 域池既有实体，非继承自父标题）。本测试验证
        的是 P2 的过滤链：L2 自身片段「脚手架工程」不得作为 L3 实体出现。
        """
        cands = _l3_entity_candidates("脚手架工程", 0, set(), context_title="")
        # L2 专属片段「脚手架工程」完整形式不应作为实体前缀出现
        assert not any(c.startswith("脚手架工程") for c in cands), (
            f"「脚手架工程」L2 片段未被 P2 过滤链剔除: {cands[:8]}"
        )

    def test_rule3_respected_for_l2_core(self):
        """规则 3：L3 不得以 L2 核心短语为前缀（`prefix_core=""` 既定口径）。

        验证 P2 的 l2_entities 过滤链：多实体 L2 时保留非核心片段、剔除含
        核心词的片段。域池通用实体（钢筋/脚手架）属历史设计，不在本口径内。
        """
        for parent in ("模板与支撑体系", "土方与基坑支护工程"):
            cands = _l3_entity_candidates(parent, 0, set(), context_title="")
            core = _core_for_derive(parent)
            # L2 核心短语不得作为 L3 标题前缀出现
            assert not any(c.startswith(core) for c in cands), (
                f"L3 标题违规以 L2 核心词 {core!r} 开头: "
                f"{[c for c in cands if c.startswith(core)]}"
            )

    def test_l2_entity_stage_precedes_domain_pool(self):
        """L2 专属实体阶段应排在域池之前（候选首项来自 L2，非域池兜底）。"""
        cands = _l3_entity_candidates("土方与基坑支护工程", 0, set(), context_title="")
        # 首项必然含 L2 专属实体（基坑支护），而非域池实体（地下室/主体结构等）
        first = cands[0]
        assert "基坑支护" in first, (
            f"首项应来自 L2 专属实体而非域池: {first}"
        )

    def test_l2_core_in_context_title_filtered(self):
        """context_title 含 L2 核心词时也应过滤（防御性）。"""
        cands = _l3_entity_candidates(
            "土方与基坑支护工程", 0, set(), context_title="土方与基坑支护工程"
        )
        assert not any("土方与基坑支护" in c for c in cands), (
            f"context_title 含 L2 核心词未过滤: {cands[:8]}"
        )


class TestL3ReuseCapTightened:
    """P3（第四轮）：OUTLINE_L3_MAX_PARENT_REUSE 默认收紧 3→2。

    docx 实测 65 个 L3 标题重复 2-3 次，收紧上限可进一步降低跨章重复率。
    """

    def test_default_cap_is_two(self):
        """默认上限 2：合法小范围复用（2 个相关章）不受影响，成套复制被收口。"""
        from app.config import OUTLINE_L3_MAX_PARENT_REUSE
        assert OUTLINE_L3_MAX_PARENT_REUSE == 2

    def test_cap_two_blocks_three_parent_reuse(self):
        """上限 2 时，第 3 个父级复用应被改写（不产生第 3 处同名）。"""
        tree = [
            {
                "title": f"第{i}章 悬挑花篮架与盘扣支撑体系方案",
                "level": 1,
                "children": [
                    {
                        "title": f"{j} 施工组织与部署",
                        "level": 2,
                        "children": [
                            {"title": "地下室施工工艺", "level": 3, "children": []}
                        ],
                    }
                    for j in range(1, 4)
                ],
            }
            for i in range(1, 4)
        ]
        from app.config import OUTLINE_L3_MAX_PARENT_REUSE
        capped, _fixed = _cap_cross_chapter_repetition(tree, OUTLINE_L3_MAX_PARENT_REUSE)
        # 统计「地下室施工工艺」出现次数
        l3_titles = []
        for l1 in capped:
            for l2 in l1.get("children", []):
                l3_titles.extend(c["title"] for c in l2.get("children", []))
        count = sum(1 for t in l3_titles if "地下室施工工艺" in t)
        assert count <= 2, (
            f"「地下室施工工艺」出现 {count} 次，应 ≤2（上限 2）"
        )
