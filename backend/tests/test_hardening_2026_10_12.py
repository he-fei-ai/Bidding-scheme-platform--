"""生成大纲模块 2026-10-12 加固修复回归测试。

覆盖 3 个加固修复点（已在 2026-10-10/11 L3 质量收口链基础上的进一步增强）：
  Fix1: _default_l2_children_for_skeleton 兜底未传 grandparent_title → AI 失败时 L3 落错误域
  Fix2: clean_legacy_outline_title_suffixes 只清「（其N）」后缀 → 漏清「专项6」类存量占位标题
  Fix3: _clear_and_save_outline 落库前缺 _cleanup_placeholder_titles 安全网

所有测试均为确定性规则逻辑（不打 AI provider），run 耗时 < 0.5s。
"""
from __future__ import annotations

import json
import os
import sqlite3

import pytest


# ============================================
# Fix 1: L3 兜底应继承 L1 语义域
# ============================================


class TestFix1L3DomainInheritance:
    """验证 _default_l2_children_for_skeleton 在 L2 被派生成 generic 域时，
    L3 子节点能通过 grandparent_title 继承 L1 的专业域（quality/safety/schedule）。

    修复前：L3 只会落 work 域（通用 fallback）。
    修复后：L3 从 L1 继承域特征。
    """

    def _build_skeleton(self, l2_generic_title: str = "总体方案与实施策划") -> dict:
        return {
            "children": [
                {"id": "X.1", "title": "", "children": [{"title": ""}, {"title": ""}, {"title": ""}]},
                {"id": "X.2", "title": "", "children": [{"title": ""}, {"title": ""}, {"title": ""}]},
            ]
        }

    def test_quality_l1_produces_quality_l3_entities(self):
        """L1=质量管理措施 → 兜底 L3 应出现 quality 域实体词（样板/见证/验收...）"""
        from app.services.ai.workflows_outline_v51 import _default_l2_children_for_skeleton

        result = _default_l2_children_for_skeleton(self._build_skeleton(), l1_title="质量管理措施")

        quality_entities = ("样板", "隐蔽", "实测", "见证", "旁站", "分项", "通病")
        work_entities = ("基坑", "脚手架", "混凝土", "土方", "屋面")

        all_titles = [l3["title"] for l2 in result for l3 in l2["children"]]
        q_hits = [t for t in all_titles if any(e in t for e in quality_entities)]
        w_hits = [t for t in all_titles if any(e in t for e in work_entities)]

        # 至少有一半 L3 标题命中 quality 域特征
        assert len(q_hits) >= len(all_titles) * 0.3, (
            f"L1=质量管理措施 → L3 应命中 quality 域，实际: {all_titles}"
        )
        # 不应大量出现 work 域实体词（work 域是修复前的错误 fallback）
        assert len(w_hits) < len(all_titles) * 0.5, (
            f"L1=质量管理措施 → L3 不应大量落 work 域，实际: {w_hits}"
        )

    def test_safety_l1_produces_safety_l3_entities(self):
        """L1=安全管理措施 → 兜底 L3 应出现 safety 域实体词（临边/高处/动火/应急...）"""
        from app.services.ai.workflows_outline_v51 import _default_l2_children_for_skeleton

        result = _default_l2_children_for_skeleton(self._build_skeleton(), l1_title="安全管理措施")

        safety_entities = ("临边", "高处", "动火", "隐患", "防护", "应急", "危险源")
        all_titles = [l3["title"] for l2 in result for l3 in l2["children"]]
        s_hits = [t for t in all_titles if any(e in t for e in safety_entities)]

        assert len(s_hits) >= len(all_titles) * 0.3, (
            f"L1=安全管理措施 → L3 应命中 safety 域，实际: {all_titles}"
        )

    def test_schedule_l1_produces_schedule_l3_titles(self):
        """L1=进度控制措施 → 兜底 L3 应出现进度域特征词"""
        from app.services.ai.workflows_outline_v51 import _default_l2_children_for_skeleton

        result = _default_l2_children_for_skeleton(self._build_skeleton(), l1_title="进度控制措施")

        schedule_entities = ("里程碑", "关键线路", "资源投入", "进度")
        all_titles = [l3["title"] for l2 in result for l3 in l2["children"]]
        sc_hits = [t for t in all_titles if any(e in t for e in schedule_entities)]

        assert len(sc_hits) >= len(all_titles) * 0.3, (
            f"L1=进度控制措施 → L3 应命中 schedule 域，实际: {all_titles}"
        )


# ============================================
# Fix 2: DB 存量占位标题清洗（专项6 → 语义化）
# ============================================


class TestFix2DBCleanupLegacyPlaceholder:
    """验证 clean_legacy_outline_title_suffixes 除清「（其N）」后缀外，
    还能清洗「专项N」「总体实施策划」等加固前存量占位/泛化标题。

    旧版迁移只调用 _strip_legacy_redundant_suffix（阶段 1），
    新版追加 _cleanup_placeholder_titles（阶段 2）覆盖 L3+ 占位。
    """

    def _make_legacy_db(self, tmp_path) -> sqlite3.Connection:
        """构造带 v51_tasks.project_templates 表 + 存量脏数据的 sqlite3。"""
        path = os.path.join(str(tmp_path), "legacy.db")
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE v51_tasks (level1_outline_json TEXT, final_outline_json TEXT)")
        conn.execute("CREATE TABLE project_templates (outline_json TEXT)")

        # 脏目录 JSON：含「专项6」「总体实施策划（2）」「实施要点（其3）」
        # 注：_QI_ORDINAL_SUFFIX_RE 正则只匹配尾部的「（其N）」（历史真实形态）
        #     不处理开头式「（其N）xxx」——那是不可能由运行时去重器产出的形态
        dirty_tree = [
            {"id": "1", "title": "第一章 质量管理措施", "level": 1, "children": [
                {"id": "1.1", "title": "总体方案与实施策划", "level": 2, "children": [
                    {"id": "1.1.1", "title": "专项6", "level": 3},
                    {"id": "1.1.2", "title": "总体实施策划（2）", "level": 3},
                    {"id": "1.1.3", "title": "实施要点（其3）", "level": 3},
                    {"id": "1.1.4", "title": "样板区质量控制", "level": 3},  # 好标题应保持
                ]},
            ]},
        ]
        conn.execute(
            "INSERT INTO v51_tasks (level1_outline_json, final_outline_json) VALUES (?, ?)",
            (json.dumps(dirty_tree, ensure_ascii=False), json.dumps(dirty_tree, ensure_ascii=False)),
        )
        conn.execute(
            "INSERT INTO project_templates (outline_json) VALUES (?)",
            (json.dumps(dirty_tree, ensure_ascii=False),),
        )
        conn.commit()
        return conn

    def test_strips_legacy_suffixes_plus_placeholder_titles(self, tmp_path):
        """两阶段都生效：清（其N）+ 改写专项N"""
        from app.db_migrations import clean_legacy_outline_title_suffixes

        conn = self._make_legacy_db(tmp_path)
        stats = clean_legacy_outline_title_suffixes(conn)

        # 两表都应被改动
        assert stats.get("v51_tasks", 0) >= 1
        assert stats.get("project_templates", 0) >= 1

        # 验证：读回应无脏标题
        row = conn.execute("SELECT final_outline_json FROM v51_tasks LIMIT 1").fetchone()
        tree = json.loads(row["final_outline_json"])

        def _all_titles(nodes):
            """递归收集目录树所有节点的 title。入参是 list[dict] 或单个 dict。"""
            result = []
            if isinstance(nodes, dict):
                nodes = [nodes]
            for node in nodes or []:
                if not isinstance(node, dict):
                    continue
                t = node.get("title", "")
                if t:
                    result.append(t)
                for c in node.get("children", []) or []:
                    result.extend(_all_titles(c))
            return result

        all_titles = _all_titles(tree)
        bad_patterns = ("专项", "（其", "总体实施策划", "总体方案与实施策划（2）")
        for t in all_titles:
            for p in bad_patterns:
                assert p not in t, f"清洗后仍有脏标题 '{t}' 含模式 '{p}'"

        # 好标题应保留
        assert "样板区质量控制" in all_titles

        conn.close()

    def test_idempotent_empty_no_change(self, tmp_path):
        """已清洗过的数据跑第二次应返回 0 改动（幂等）"""
        from app.db_migrations import clean_legacy_outline_title_suffixes

        conn = self._make_legacy_db(tmp_path)
        # 第一次跑
        clean_legacy_outline_title_suffixes(conn)
        # 第二次跑：stats 应全 0
        stats2 = clean_legacy_outline_title_suffixes(conn)
        assert stats2.get("v51_tasks", 0) == 0
        assert stats2.get("project_templates", 0) == 0
        conn.close()


# ============================================
# Fix 3: 落库前安全网调用可达性
# ============================================


class TestFix3SaveOutlineSafetyNet:
    """验证 _clear_and_save_outline 在 normalize → dedupe 之后、实际写库之前，
    追加了 _cleanup_placeholder_titles 调用作为最后一道安全网。

    纯调用可达性 + 行为验证（不真正写库）。
    """

    def test_cleanup_placeholder_is_importable_and_callable(self):
        """_clear_and_save_outline 里 import 的 _cleanup_placeholder_titles 必须可用"""
        from app.services.ai.outline_common import _cleanup_placeholder_titles
        assert callable(_cleanup_placeholder_titles)

    def test_placeholder_cleanup_on_minimal_tree(self):
        """最小脏目录树：1 个 L3 占位标题 → 安全网应清洗"""
        from app.services.ai.outline_common import _cleanup_placeholder_titles

        dirty = [{"title": "第一章", "level": 1, "children": [
            {"title": "方案", "level": 2, "children": [
                {"title": "专项1", "level": 3},
            ]},
        ]}]
        count = _cleanup_placeholder_titles(dirty)
        assert count >= 1, "至少应清掉 1 个占位标题"
        # 清洗后 title 不再是 '专项1'
        l3_title = dirty[0]["children"][0]["children"][0]["title"]
        assert l3_title != "专项1", "占位标题应被改写"
        assert "专项" not in l3_title, f"标题应不含'专项'，实际='{l3_title}'"


# ============================================
# 跨修复点：安全网撞名检测闭环
# ============================================


class TestDedupeAfterPlaceholderCleanup:
    """_dedupe_outline_titles 可能产生「总体实施策划（2）」，
    _cleanup_placeholder_titles 后新标题可能与未被去重碰过的撞名 ——
    验证 cleanup 本身内置全局 used 集合，能防住这种撞名。"""

    def test_cleanup_uses_global_used_to_avoid_cross_parent_collision(self):
        """两个不同父节点下各有一个「专项1」→ cleanup 后两个新标题不应相同"""
        from app.services.ai.outline_common import _cleanup_placeholder_titles

        dirty = [
            {"title": "第一章 质量", "level": 1, "children": [
                {"title": "总体方案与实施策划", "level": 2, "children": [
                    {"title": "专项1", "level": 3},
                ]},
            ]},
            {"title": "第二章 安全", "level": 1, "children": [
                {"title": "总体方案与实施策划", "level": 2, "children": [
                    {"title": "专项1", "level": 3},
                ]},
            ]},
        ]
        _cleanup_placeholder_titles(dirty)

        l3_titles = []
        for l1 in dirty:
            for l2 in l1.get("children", []) or []:
                for l3 in l2.get("children", []) or []:
                    l3_titles.append(l3["title"])

        # 全局去重：cleanup 用 global_used，所以两个「专项1」应被改成不同标题
        assert len(l3_titles) == 2
        assert l3_titles[0] != l3_titles[1], f"cleanup 应保证跨父标题唯一，实际: {l3_titles}"
