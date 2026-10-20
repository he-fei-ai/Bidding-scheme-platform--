"""2026-10-17 生成大纲模块第六轮加固回归（编号前缀 / 专项家族 / 序号占位扩展）。

症状来源（用户复报）：「三级标题大量是占位式泛化名称（如"专项6"
"总体实施策划（2）""总体实施策划（3）"），且存在编号冲突后缀（3）、（2）等」。

排查结论：2026-10-09~15 五轮修复已拦住「专项+紧邻数字」「编号后缀+泛化主体」
两个主干家族，但对抗探针（_probe_* 系列，单标题穿透 + 全链路 E2E 实证）
发现**同一症状仍有四副漏判面孔**，其中「编号前缀」是用户症状的直接镜像：

  BUG-OUTLINE-2026-10-17-01a 编号前缀形态：「（2）总体实施策划」
      「(3)总体实施策划」「（二）质量保证措施」「2、总体实施策划」
      「2. 总体实施策划」——编号在标题**开头**。`_ORDINAL_SUFFIX_RE` 只锚定
      结尾，`_PLACEHOLDER_TITLE_RE` 要求整标题恰为「专项N」→ 此前缀形态
      完整穿过清洗链/去重/自检/安全网/落库（探针实证 3 处存活）。
  BUG-OUTLINE-2026-10-17-01b 专项家族扩展：「专项（6）」「第六专项」
      「专项措施6」——括号式/倒序/泛化词+尾编号，旧正则只查「专项+紧邻数字」。
  BUG-OUTLINE-2026-10-17-01c 序号占位家族扩展：「项目6」「第六项」「第6项」
      「内容三」——中文数字、「项目」结构词、整标题即序号三种漏判形态。
  BUG-OUTLINE-2026-10-17-01d 清洗器碎片主体：「专项（6）」旧实现剥后缀得
      「专项」直接保留——把一种占位垃圾换成另一种。新增碎片主体护栏
      （过短主体/结构词表成员 → 整体语义化重写）。

修复口径（与后缀侧严格对称）：
  - `_is_dirty_l3_title` 规则 9/10/11 纳入新形态；
  - `_cleanup_placeholder_titles` 先剥**前缀**保主体，主体仍泛化/碎片才重写；
  - `_disambiguate_title` 入口对称剥离前缀，避免编号被挪到标题中间；
  - DB 落库层（`_clear_and_save_outline` 的归一化链）与存量迁移
    （`db_migrations` 遗留标题清理）复用同一函数，自动继承修复。

运行：cd backend && python -m pytest tests/test_outline_l3_prefix_placeholder_2026_10_17.py -q -p no:cacheprovider
"""

import re

import pytest

from app.services.ai.outline_common import (
    _cleanup_placeholder_titles,
    _disambiguate_title,
    _is_dirty_l3_title,
    _is_dirty_l3_title_deep,
    _strip_ordinal_prefix,
    _uniquify_outline_titles,
)

# ===========================================================================
# 1. 脏标题判定：新增形态必须全部识别
# ===========================================================================

#: BUG-OUTLINE-2026-10-17-01a：编号前缀形态（用户症状的镜像面孔）
PREFIX_FORMS = [
    "（2）总体实施策划",
    "（3）总体实施策划",
    "(2)总体实施策划",
    "(3)总体实施策划",
    "（二）质量保证措施",
    "（第2项）总体实施策划",
    "2、总体实施策划",
    "二、总体实施策划",
    "2. 总体实施策划",
    "12. 总体实施策划",
    "（2）（3）总体实施策划",  # 堆叠前缀
]

#: BUG-OUTLINE-2026-10-17-01b：专项家族扩展
ZHUANXIANG_FORMS = [
    "专项（6）",
    "专项(6)",
    "第六专项",
    "第6专项",
    "6专项",
    "专项措施6",
    "专项方案6",
    "专项施工3",
]

#: BUG-OUTLINE-2026-10-17-01c：纯序号占位家族扩展
ORDINAL_FORMS = [
    "项目6",
    "项目六",
    "内容三",
    "第六项",
    "第6项",
    "第6条",
    "6项",
]

#: 合法标题：不得误判（含编号前缀的合法近邻）
LEGAL_TITLES = [
    # 既有守护集（test_l3_title_quality_2026_10_10 断言集）
    "样板区质量控制",
    "关键线路进度计划",
    "地下室防水施工工艺",
    "总平面布置",
    "脚手架专项施工方案",
    "质量保证组织体系",
    "深基坑（超5m）支护方案",
    "混凝土(28天)配合比",
    "工序交接流程5",
    # 编号前缀的合法近邻
    "第一章 总体概述",
    "第三章施工部署",
    "第一编 技术方案",
    "1.5 目标分解",
    "（GB50204）验收规范",
    "2026年度施工组织计划",
    "专项施工方案编审批制度",
    "装饰装修专项施工方案",
    # 短但具体的工程实体标题
    "成品保护",
    "防水工程",
    "样板引路",
]


class TestDirtyDetectorNewForms:
    """四副漏判面孔必须全部纳入脏标题判定。"""

    @pytest.mark.parametrize("title", PREFIX_FORMS)
    def test_ordinal_prefix_forms_are_dirty(self, title):
        assert _is_dirty_l3_title(title), f"编号前缀形态漏判: {title!r}"
        assert _is_dirty_l3_title_deep(title), f"深度判定漏判: {title!r}"

    @pytest.mark.parametrize("title", ZHUANXIANG_FORMS)
    def test_zhuanxiang_family_forms_are_dirty(self, title):
        assert _is_dirty_l3_title(title), f"专项家族变体漏判: {title!r}"

    @pytest.mark.parametrize("title", ORDINAL_FORMS)
    def test_ordinal_only_forms_are_dirty(self, title):
        assert _is_dirty_l3_title(title), f"纯序号占位漏判: {title!r}"

    @pytest.mark.parametrize("title", LEGAL_TITLES)
    def test_legal_titles_not_dirty(self, title):
        assert not _is_dirty_l3_title(title), f"合法标题被误判: {title!r}"
        assert not _is_dirty_l3_title_deep(title), f"合法标题被深度误判: {title!r}"


class TestStripOrdinalPrefix:
    """`_strip_ordinal_prefix` 单元行为：只剥编号、不动主体、循环堆叠。"""

    def test_bracket_prefix(self):
        assert _strip_ordinal_prefix("（2）总体实施策划") == "总体实施策划"
        assert _strip_ordinal_prefix("(3)质量保证措施") == "质量保证措施"
        assert _strip_ordinal_prefix("（二）质量保证措施") == "质量保证措施"
        assert _strip_ordinal_prefix("（第2项）总体实施策划") == "总体实施策划"

    def test_list_prefix(self):
        assert _strip_ordinal_prefix("2、总体实施策划") == "总体实施策划"
        assert _strip_ordinal_prefix("二、工程概况") == "工程概况"
        assert _strip_ordinal_prefix("2. 总体实施策划") == "总体实施策划"

    def test_stacked_prefix(self):
        assert _strip_ordinal_prefix("（2）（3）总体实施策划") == "总体实施策划"

    def test_pure_ordinal_title_becomes_empty(self):
        assert _strip_ordinal_prefix("（2）") == ""

    def test_no_hit_returns_original(self):
        for t in ("总体实施策划", "第一章 总体概述", "1.5 目标分解", "深基坑（超5m）支护方案"):
            assert _strip_ordinal_prefix(t) == t


# ===========================================================================
# 2. 清洗链：前缀形态的处置（剥前缀保主体 / 碎片护栏）
# ===========================================================================


def _tree(l3_titles, l2_title="项目总体理解", l1_title="第一章 总体概述"):
    return [
        {
            "title": l1_title,
            "level": 1,
            "children": [
                {
                    "title": l2_title,
                    "level": 2,
                    "children": [{"title": t, "level": 3, "children": []} for t in l3_titles],
                }
            ],
        }
    ]


def _l3_of(tree):
    return [c["title"] for c in tree[0]["children"][0]["children"]]


class TestCleanupPrefixHandling:
    def test_prefix_with_generic_body_fully_rewritten(self):
        """前缀 + 泛化主体 → 整体语义化重写（与后缀侧同口径）。"""
        tree = _tree(["（2）总体实施策划"])
        assert _cleanup_placeholder_titles(tree) == 1
        t = _l3_of(tree)[0]
        assert t != "（2）总体实施策划"
        assert not _is_dirty_l3_title_deep(t), t
        assert "总体实施策划" not in t, f"泛化主体必须被替换: {t}"

    def test_prefix_with_concrete_body_stripped_and_kept(self):
        """前缀 + 具体主体 → 剥前缀保留主体（对称 B1「剥后缀保主体」）。"""
        tree = _tree(["（2）混凝土施工工艺"])
        assert _cleanup_placeholder_titles(tree) == 1
        assert _l3_of(tree) == ["混凝土施工工艺"]

    def test_fragment_body_fully_rewritten(self):
        """BUG-01d：碎片主体（「专项」）不得保留，整体重写。"""
        tree = _tree(["专项（6）"])
        assert _cleanup_placeholder_titles(tree) == 1
        t = _l3_of(tree)[0]
        assert t not in ("专项", "专项（6）"), f"碎片主体被保留: {t}"
        assert not _is_dirty_l3_title_deep(t), t

    def test_cleanup_is_idempotent(self):
        tree = _tree(["（2）总体实施策划", "专项（6）", "第六专项"])
        _cleanup_placeholder_titles(tree)
        snap = _l3_of(tree)
        assert _cleanup_placeholder_titles(tree) == 0, "幂等：二次清洗不应有改动"
        assert _l3_of(tree) == snap

    def test_full_chain_produces_clean_tree(self):
        """生产链顺序「清洗 → 去重」之后整树干净。"""
        tree = _tree([
            "（2）总体实施策划", "(3)总体实施策划", "（二）质量保证措施",
            "专项（6）", "第六专项", "专项措施6", "项目6", "第6项",
            "2、总体实施策划",
        ])
        _cleanup_placeholder_titles(tree)
        _uniquify_outline_titles(tree)
        titles = _l3_of(tree)
        for t in titles:
            assert not _is_dirty_l3_title_deep(t), f"清洗链残留脏标题: {t}"
        assert len(set(titles)) == len(titles), f"同级重复: {titles}"


# ===========================================================================
# 3. 消歧入口：前缀对称剥离
# ===========================================================================


class TestDisambiguateStripsPrefix:
    def test_prefix_stripped_before_disambiguation(self):
        """输入带前缀 → 产物不得把编号挪到标题中间。"""
        out = _disambiguate_title("（2）总体实施策划", set())
        assert not out.startswith("（"), f"前缀残留: {out}"
        assert "（2）" not in out, f"编号被挪进标题: {out}"

    def test_prefix_stripped_avoids_middle_residue(self):
        """前缀 + 具体主体：产物是主体的消歧形态，不含前导编号。"""
        out = _disambiguate_title("2、混凝土施工工艺", {"混凝土施工工艺"})
        assert "2、" not in out and not out.startswith("2"), out
        assert not _is_dirty_l3_title_deep(out), out


# ===========================================================================
# 4. 全链路 E2E：AI 注入前缀形态 → 分步主链 → 落库
# ===========================================================================

_DIRTY_L3 = [
    "（2）总体实施策划",
    "专项（6）",
    "第六专项",
    "2、总体实施策划",
    "项目6",
    "总体实施策划（3）",  # 后缀形态（回归守护）
    "专项6",              # 原始形态（回归守护）
]


class _E2EProvider:
    class _Msg:
        def __init__(self, role, content):
            self.role = role
            self.content = content

    async def chat(self, messages, **kwargs):
        raise RuntimeError("e2e mock: AI 不可用")


async def _fake_collect_e2e(provider, messages, **kwargs):
    import json as _json

    user = next((m.content for m in messages if getattr(m, "role", "") == "user"), "")
    if "<json_outline>" in user:
        m = re.search(r"<json_outline>\n(.*?)\n</json_outline>", user, re.DOTALL)
        skeleton = _json.loads(m.group(1))
        for i, l2 in enumerate(skeleton.get("children", [])):
            l2["title"] = l2.get("title") or "总体方案与实施策划"
            for k, l3 in enumerate(l2.get("children") or []):
                l3["title"] = _DIRTY_L3[(i + k) % len(_DIRTY_L3)]
        return skeleton
    return [
        {"title": "总体施工部署", "children": []},
        {"title": "工程质量保障", "children": []},
    ]


class TestEndToEndPrefixPipeline:
    @pytest.mark.asyncio
    async def test_stepwise_pipeline_cleans_prefix_forms(self, monkeypatch):
        """AI 在 L2 阶段注入编号前缀/专项家族/序号占位全部新形态，
        真实分步全链路之后最终目录必须全部干净。"""
        import app.services.ai.workflows_outline_v51 as wo

        monkeypatch.setattr(wo, "collect_json_response", _fake_collect_e2e)
        outline = await wo.generate_outline_v51(
            _E2EProvider(), "某房建工程概述", "[]", "建筑工程",
            auto_confirm=True, outline_mode="stepwise",
        )
        assert outline, "分步路径未产出目录"

        def _walk(nodes, level=1):
            for n in nodes:
                if level >= 3:
                    t = n.get("title", "")
                    assert t and t.strip(), f"空标题: {n}"
                    assert not _is_dirty_l3_title_deep(t), f"脏标题漏出: {t}"
                for c in n.get("children") or []:
                    _walk([c], level + 1)

        _walk(outline)

        # 位置式 id 与层级严格一致
        def _check_ids(nodes, prefix=""):
            for i, n in enumerate(nodes, 1):
                expect = f"{prefix}{i}" if not prefix else f"{prefix}.{i}"
                assert str(n.get("id")) == expect, f"id 错位: {n.get('id')} != {expect}"
                _check_ids(n.get("children") or [], expect)

        _check_ids(outline)

    @pytest.mark.asyncio
    async def test_db_save_layer_cleans_prefix_forms(self):
        """落库层归一化链（cleanup → uniquify）同样必须拦住前缀形态。"""
        from app.routers.sse.sse_db_ops import _clear_and_save_outline
        from app.db import get_db

        tree = _tree(
            ["（2）总体实施策划", "专项（6）", "第六专项", "项目6"],
            l2_title="项目总体理解", l1_title="第一章 总体概述",
        )
        pid = _new_project("E2E-前缀形态落库")
        _clear_and_save_outline(pid, tree)
        with get_db() as conn:
            rows = conn.execute(
                "SELECT title, level FROM sections WHERE project_id = ?", (pid,)
            ).fetchall()
        assert rows, "落库结果为空"
        for r in rows:
            if r["level"] >= 3:
                assert not _is_dirty_l3_title_deep(r["title"]), f"DB 脏标题: {r['title']}"


def _new_project(name: str) -> int:
    from app.db import get_db

    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO projects (name, industry) VALUES (?, '通用')", (name,)
        )
        return cur.lastrowid
