"""方案三（2026-10-09）L3 自身正文 300 字 ±20% 字数口径专项测试。

需求原文：三级目录（L3）级目录下（除下级目录章节外）需生成 300 字左右的
正文文字，文字允许上下浮动 20%。

双口径化解：
  - L3 自身正文：300 ± 20%（240~360）——「除下级目录章节外」的正文部分。
  - 四档 800/1200/2000/2500：语义为「L3 子树总字数（含下级目录章节）」，
    仅当 L3 存在 L4+ 下级目录章节时参与校验。

覆盖：
  1. 配置层 writing_length_config：目标/容差/区间/达标判断、环境变量覆盖、
     四档语义保留、L1/L2 不参与控制。
  2. 生成层 sse_utils：L3 目标取 300；叶子字典含 is_l3_own / child_chapters。
  3. 提示词层 workflows_sections：L3 自身 300±20% 红线、L3 非叶子四档子树
     口径、child_chapters 交叉引用与「禁止复述」红线。
  4. 统计层 routers/word_count.py：目标解析 / 达标区间 / 达标与超欠判定 /
     档位分组统计。
  5. 导出预检 export.check_document：L3 空正文纳入检查、240~360 区间判罚、
     四档 L3 不在此判罚。
  6. 目录树 build_tree：cumulative_word_count 累计统计（L1/L2 仅作统计展示）。

运行：cd backend && pytest tests/test_l3_own_word_count_2026_10_09.py -v
"""
from __future__ import annotations

import importlib
import uuid

import pytest

from app.db import get_db
from app.routers import word_count as wc_router
from app.routers.sse import sse_utils
from app.services import export as export_svc
from app.services.ai import writing_length_config as wlc
from app.services.ai.workflows_sections import build_chapter_content_messages
from app.services.sections import _invalidate_build_tree_cache, build_tree
from app.utils import count_words


# ===========================================================================
# 工具
# ===========================================================================


def _cn(n: int) -> str:
    """生成恰好 n 个中文字符的正文（count_words 对中文逐字计数）。"""
    return "施" * n


def _make_section_row(
    *,
    sid: int = 0,
    pid: int = 0,
    title: str,
    level: int,
    sort_order: int = 1,
    parent_id=None,
    content: str = "",
    word_count_tier: str | None = None,
    target_words: int | None = None,
    status: str = "completed",
    ref: str | None = None,
    parent_ref: str | None = None,
) -> dict:
    """构造章节行。

    - sid / parent_id：仅用于 sse_utils 的纯内存 dict 场景（id 可自由指定）；
    - ref / parent_ref：用于入库场景（SQLite 自增 id 无法预知，按插入顺序解析）。
    """
    return {
        "id": sid,
        "project_id": pid,
        "parent_id": parent_id,
        "title": title,
        "level": level,
        "sort_order": sort_order,
        "section_type": "text",
        "scoring_point_id": None,
        "target_words": target_words,
        "word_count_tier": word_count_tier,
        "content": content,
        "status": status,
        "description": "",
        "word_count": count_words(content),
        "ref": ref,
        "parent_ref": parent_ref,
    }


@pytest.fixture
def project_id():
    """每个用例独立项目，避免相互污染。"""
    name = f"L3口径-{uuid.uuid4().hex[:10]}"
    with get_db() as conn:
        cur = conn.execute("INSERT INTO projects (name, industry) VALUES (?, '通用')", (name,))
        return int(cur.lastrowid)


def _insert(pid: int, rows: list[dict]) -> dict:
    """按顺序插入章节，返回 {ref: 真实自增 id}。"""
    ref_to_id: dict[str, int] = {}
    with get_db() as conn:
        for r in rows:
            parent_id = (
                ref_to_id.get(r["parent_ref"]) if r.get("parent_ref") else r.get("parent_id")
            )
            cur = conn.execute(
                "INSERT INTO sections (project_id, parent_id, title, level, sort_order,"
                " section_type, status, content, target_words, word_count_tier, word_count)"
                " VALUES (?, ?, ?, ?, ?, 'text', ?, ?, ?, ?, ?)",
                (
                    pid, parent_id, r["title"], r["level"], r["sort_order"],
                    r["status"], r["content"], r["target_words"],
                    r["word_count_tier"], r["word_count"],
                ),
            )
            if r.get("ref"):
                ref_to_id[r["ref"]] = int(cur.lastrowid)
    return ref_to_id


# ===========================================================================
# 1. 配置层
# ===========================================================================


class TestWritingLengthConfigL3Own:
    """writing_length_config：L3 自身正文 300 ± 20%。"""

    def test_l3_own_target_and_tolerance(self):
        assert wlc.get_l3_own_target() == 300
        assert abs(wlc.get_l3_own_tolerance() - 0.20) < 1e-9

    def test_l3_own_range_default(self):
        assert wlc.get_l3_own_range() == (240, 360)

    @pytest.mark.parametrize(
        "wc,expected",
        [(240, True), (300, True), (360, True), (239, False), (361, False), (0, False), (-5, False)],
    )
    def test_l3_own_within_boundary(self, wc, expected):
        assert wlc.l3_own_within(wc) is expected

    def test_l3_own_env_override(self):
        """环境变量 L3_OWN_TARGET_WORDS / L3_OWN_TOLERANCE_RATIO 可覆盖默认值。"""
        import os

        keys = ("L3_OWN_TARGET_WORDS", "L3_OWN_TOLERANCE_RATIO")
        old = {k: os.environ.get(k) for k in keys}
        os.environ["L3_OWN_TARGET_WORDS"] = "500"
        os.environ["L3_OWN_TOLERANCE_RATIO"] = "0.10"
        try:
            importlib.reload(wlc)
            assert wlc.get_l3_own_target() == 500
            assert abs(wlc.get_l3_own_tolerance() - 0.10) < 1e-9
            assert wlc.get_l3_own_range() == (450, 550)
            assert wlc.l3_own_within(460) and not wlc.l3_own_within(449)
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            importlib.reload(wlc)
            assert wlc.get_l3_own_target() == 300

    def test_four_tiers_semantics_preserved(self):
        """四档保留为「L3 子树总字数」口径，数值不变。"""
        assert wlc.level3_target("brief") == 800
        assert wlc.level3_target("standard") == 1200
        assert wlc.level3_target("detailed") == 2000
        assert wlc.level3_target("long") == 2500

    def test_l1_l2_not_controlled(self):
        """L1/L2 是父节点，不参与字数控制（返回 0）。"""
        assert wlc.level2_target("standard") == 0
        assert wlc.level2_target("detailed") == 0


# ===========================================================================
# 2. 生成层 sse_utils
# ===========================================================================


class TestSseEffectiveTargetL3Own:
    """_get_effective_target_words：L3 取 300，L1/L2 不控制。"""

    @staticmethod
    def _sec(level, **kw):
        base = {"level": level, "target_words": None, "word_count_tier": None}
        base.update(kw)
        return base

    def test_l3_default_is_300(self):
        assert sse_utils._get_effective_target_words(self._sec(3), writing_length="standard") == 300

    @pytest.mark.parametrize("mode", ["brief", "standard", "detailed", "long"])
    def test_l3_ignores_four_tiers(self, mode):
        """方案三：无下级的 L3 以 300 为唯一口径，四档不生效。"""
        assert sse_utils._get_effective_target_words(self._sec(3), writing_length=mode) == 300

    def test_l3_custom_target_words_wins(self):
        assert sse_utils._get_effective_target_words(
            self._sec(3, target_words=999), writing_length="standard"
        ) == 999

    def test_l1_l2_return_none(self):
        assert sse_utils._get_effective_target_words(self._sec(1)) is None
        assert sse_utils._get_effective_target_words(self._sec(2)) is None

    def test_l4_follows_tier(self):
        """L4+ 深层叶子仍跟随档位（与 L3 自身口径无关）。"""
        assert sse_utils._get_effective_target_words(
            self._sec(4), writing_length="detailed"
        ) == 2000


class TestSseLeafDictsL3Own:
    """_build_leaf_dicts：is_l3_own / child_chapters 字段。"""

    @staticmethod
    def _dicts(rows: list[dict]) -> tuple[dict, dict]:
        by_id = {r["id"]: dict(r) for r in rows}
        cmap: dict = {}
        for r in rows:
            if r["parent_id"] is not None:
                cmap.setdefault(r["parent_id"], []).append(dict(r))
        return by_id, cmap

    def test_l3_leaf_flags(self):
        rows = [
            _make_section_row(sid=1, pid=900, title="第一章", level=1, status="pending"),
            _make_section_row(sid=2, pid=900, title="1.1", level=2, parent_id=1, status="pending"),
            _make_section_row(
                sid=3, pid=900, title="1.1.1 施工部署", level=3, parent_id=2, status="pending"
            ),
        ]
        by_id, cmap = self._dicts(rows)
        leaves = sse_utils._build_leaf_dicts(
            900, by_id, cmap, regenerate_ids="3", writing_length="standard"
        )
        assert len(leaves) == 1
        leaf = leaves[0]
        assert leaf["is_l3_own"] is True
        assert leaf["child_chapters"] == []
        assert leaf["target_words"] == 300

    def test_l4_leaf_flags(self):
        rows = [
            _make_section_row(sid=1, pid=901, title="第一章", level=1, status="pending"),
            _make_section_row(sid=2, pid=901, title="1.1", level=2, parent_id=1, status="pending"),
            _make_section_row(
                sid=3, pid=901, title="1.1.1 质量管理", level=3, parent_id=2,
                status="pending", word_count_tier="detailed",
            ),
            _make_section_row(
                sid=4, pid=901, title="1.1.1.1 质量组织", level=4, parent_id=3, status="pending"
            ),
        ]
        by_id, cmap = self._dicts(rows)
        leaves = sse_utils._build_leaf_dicts(
            901, by_id, cmap, regenerate_ids="4", writing_length="detailed"
        )
        assert len(leaves) == 1
        leaf = leaves[0]
        assert leaf["level"] == 4
        assert leaf["is_l3_own"] is False
        assert leaf["target_words"] == 2000  # L4 跟随 detailed 档位
        assert leaf["child_chapters"] == []


def _prompt_text(chapter_info: dict, **kw) -> str:
    msgs = build_chapter_content_messages(
        chapter_info, writing_length=kw.pop("writing_length", "standard"), **kw
    )
    return "\n".join(m.content for m in msgs if m.role == "user")


# ===========================================================================
# 3. 提示词层
# ===========================================================================


class TestChapterPromptL3Own:
    """build_chapter_content_messages：字数红线与交叉引用注入。"""

    def test_l3_own_uses_300_range(self):
        info = {"id": 3, "title": "1.1.1 施工部署", "level": 3, "is_l3_own": True,
                "word_count_tier": None, "child_chapters": []}
        text = _prompt_text(info)
        assert "240~360" in text, "L3 自身正文必须下发 240~360 区间"

    def test_l3_own_forbids_repetition(self):
        info = {"id": 3, "title": "1.1.1 施工部署", "level": 3, "is_l3_own": True,
                "word_count_tier": None, "child_chapters": []}
        text = _prompt_text(info)
        assert "严禁复述" in text, "L3 必须带「严禁复述下级目录章节」红线"

    def test_child_chapters_cross_reference_injected(self):
        info = {"id": 3, "title": "1.1.1 质量管理", "level": 3, "is_l3_own": True,
                "word_count_tier": "detailed",
                "child_chapters": [
                    {"id": "4", "title": "1.1.1.1 质量组织", "level": 4,
                     "description": "质量组织体系与岗位职责"},
                ]}
        text = _prompt_text(info)
        assert "1.1.1.1 质量组织" in text, "child_chapters 交叉引用必须下发"
        assert "复述" in text, "交叉引用区必须带禁止复述红线"

    def test_l3_non_leaf_uses_tree_total(self):
        """L3 非叶子（存在 L4+）走四档子树总字数口径。"""
        info = {"id": 3, "title": "1.1.1 质量管理", "level": 3, "is_l3_own": False,
                "word_count_tier": "detailed", "child_chapters": []}
        text = _prompt_text(info)
        assert "2000" in text
        assert "子树总字数" in text

    def test_l1_has_no_word_instruction(self):
        info = {"id": 1, "title": "第一章 工程概况", "level": 1, "is_l3_own": False,
                "word_count_tier": None, "child_chapters": []}
        text = _prompt_text(info)
        assert "目标字数" not in text



# ===========================================================================
# 4. 统计层 routers/word_count.py
# ===========================================================================


def _sec_obj(level=3, word_count=300, tier=None, target_words=None):
    class _S:
        pass

    o = _S()
    o.level = level
    o.word_count = word_count
    o.target_words = target_words
    o.word_count_tier = tier
    return o


class TestWordCountRouterL3Own:
    """routers/word_count.py 的目标解析与达标判定口径。"""

    def test_resolve_target_l3_own_is_300(self):
        assert wc_router._resolve_target_for_section(_sec_obj(3)) == 300

    def test_resolve_target_l1_l2_none(self):
        assert wc_router._resolve_target_for_section(_sec_obj(1)) is None
        assert wc_router._resolve_target_for_section(_sec_obj(2)) is None

    def test_resolve_target_custom_wins(self):
        assert wc_router._resolve_target_for_section(_sec_obj(3, target_words=888)) == 888

    def test_resolve_target_tier_wins_for_l4(self):
        assert wc_router._resolve_target_for_section(_sec_obj(4, tier="detailed")) == 2000

    def test_target_bounds_l3_own(self):
        assert wc_router._target_bounds(_sec_obj(3, word_count=300)) == (240, 360)

    def test_target_bounds_l4_tier(self):
        assert wc_router._target_bounds(_sec_obj(4, tier="detailed")) == (1800, 2200)

    def test_target_bounds_l1_empty(self):
        assert wc_router._target_bounds(_sec_obj(1)) == (0, 0)

    @pytest.mark.parametrize(
        "wc,expected", [(240, True), (300, True), (360, True), (239, False), (361, False), (0, False)]
    )
    def test_is_within_tolerance_l3_own(self, wc, expected):
        assert wc_router._is_within_tolerance(_sec_obj(3, word_count=wc)) is expected

    @pytest.mark.parametrize("wc,expected", [(1200, True), (1080, True), (1000, False), (1400, False)])
    def test_is_within_tolerance_l4_tier(self, wc, expected):
        assert wc_router._is_within_tolerance(_sec_obj(4, word_count=wc, tier="standard")) is expected

    def test_below_above_target(self):
        assert wc_router._is_below_target(_sec_obj(3, word_count=200))
        assert wc_router._is_above_target(_sec_obj(3, word_count=400))
        assert not wc_router._is_below_target(_sec_obj(3, word_count=300))
        assert not wc_router._is_above_target(_sec_obj(3, word_count=300))
        # 无目标（L1/L2）不参与超欠判定
        assert not wc_router._is_above_target(_sec_obj(1, word_count=99999))

    def test_340_words_is_on_target_not_exceeded(self):
        """回归：340 字在 240~360 内，不能被当作「超字数」统计。"""
        s = _sec_obj(3, word_count=340)
        assert wc_router._is_within_tolerance(s)
        assert not wc_router._is_above_target(s)

    def test_compute_tier_stats_l3_own_grouped(self):
        sections = [
            _sec_obj(3, word_count=300),                    # 达标
            _sec_obj(3, word_count=200),                    # 偏少
            _sec_obj(3, word_count=400),                    # 偏多
            _sec_obj(4, word_count=2000, tier="detailed"),  # 四档达标
        ]
        stats = wc_router._compute_tier_stats(sections)
        std = stats.get("standard", {})
        assert std["count"] == 3
        assert std["on_target"] == 1
        assert std["below"] == 1
        assert std["exceeded"] == 1
        det = stats.get("detailed", {})
        assert det["count"] == 1 and det["on_target"] == 1

    def test_compute_tier_stats_empty_section_not_classified(self):
        stats = wc_router._compute_tier_stats([_sec_obj(3, word_count=0)])
        std = stats.get("standard", {})
        assert std["count"] == 1
        assert std["on_target"] + std["below"] + std["exceeded"] == 0



# ===========================================================================
# 5. 导出预检 export.check_document
# ===========================================================================


class TestCheckDocumentL3Own:
    """check_document：L3 空正文纳入检查 + 240~360 区间判罚。"""

    @staticmethod
    def _run(pid: int, rows: list[dict]):
        _insert(pid, rows)
        return export_svc.check_document(pid)

    def _base_rows(self, pid: int, level3_content: str = _cn(300), **kw) -> list[dict]:
        return [
            _make_section_row(pid=pid, title="第一章", level=1, ref="l1"),
            _make_section_row(pid=pid, title="1.1", level=2, parent_ref="l1", ref="l2"),
            _make_section_row(
                pid=pid, title="1.1.1 施工部署", level=3, parent_ref="l2", ref="l3",
                content=level3_content, **kw,
            ),
        ]

    def test_l3_leaf_empty_body_flagged(self, project_id):
        rows = self._base_rows(project_id)
        rows.append(_make_section_row(
            pid=project_id, title="1.1.2 资源配置", level=3, parent_ref="l2", ref="l3b",
            content="",
        ))
        result = self._run(project_id, rows)
        empty_titles = {e.title for e in result.empty_sections}
        assert "1.1.2 资源配置" in empty_titles, "L3 空正文必须纳入空正文检查"
        assert "1.1.1 施工部署" not in empty_titles

    def test_l3_within_range_no_range_warning(self, project_id):
        result = self._run(project_id, self._base_rows(project_id))
        assert not any("三级目录自身正文" in w for w in result.warnings)

    @pytest.mark.parametrize("n", [240, 300, 360])
    def test_l3_boundary_words_pass(self, project_id, n):
        result = self._run(project_id, self._base_rows(project_id, _cn(n)))
        assert not any("三级目录自身正文" in w for w in result.warnings), (
            f"{n} 字应落在 240~360 区间内"
        )

    @pytest.mark.parametrize("n", [200, 239, 361, 400])
    def test_l3_out_of_range_warns(self, project_id, n):
        result = self._run(project_id, self._base_rows(project_id, _cn(n)))
        assert any("三级目录自身正文" in w for w in result.warnings), (
            f"{n} 字应触发 240~360 区间告警：{result.warnings}"
        )

    def test_l3_range_warning_lists_section_title(self, project_id):
        result = self._run(project_id, self._base_rows(project_id, _cn(200)))
        matched = [w for w in result.warnings if "三级目录自身正文" in w]
        assert matched and "1.1.1 施工部署" in matched[0]

    def test_l3_with_children_not_range_penalized(self, project_id):
        """L3 有下级（L4+）时按四档子树总字数口径，不走 240~360 判罚。"""
        rows = [
            _make_section_row(pid=project_id, title="第一章", level=1, ref="l1"),
            _make_section_row(pid=project_id, title="1.1", level=2, parent_ref="l1", ref="l2"),
            _make_section_row(
                pid=project_id, title="1.1.1 质量管理", level=3, parent_ref="l2", ref="l3",
                content=_cn(150), word_count_tier="detailed",
            ),
            _make_section_row(
                pid=project_id, title="1.1.1.1 质量组织", level=4, parent_ref="l3", ref="l4",
                content=_cn(1800),
            ),
        ]
        result = self._run(project_id, rows)
        assert not any("三级目录自身正文" in w for w in result.warnings), (
            f"L3 非叶子不应按 240~360 判罚：{result.warnings}"
        )

    def test_exported_word_count_matches_l3_body(self, project_id):
        """导出前后一致：check_document 统计的 total_chars 等于 L3 正文长度。"""
        body = _cn(300)
        result = self._run(project_id, self._base_rows(project_id, body))
        assert result.total_chars == len(body)


# ===========================================================================
# 6. 目录树累计字数
# ===========================================================================


class TestBuildTreeCumulative:
    """build_tree：cumulative_word_count 累计统计（L1/L2 仅作统计展示）。"""

    def test_cumulative_word_count(self, project_id):
        ids = _insert(project_id, [
            _make_section_row(pid=project_id, title="第一章", level=1, ref="l1"),
            _make_section_row(pid=project_id, title="1.1", level=2, parent_ref="l1",
                              sort_order=1, ref="l2a"),
            _make_section_row(pid=project_id, title="1.1.1 施工部署", level=3, parent_ref="l2a",
                              content=_cn(300), sort_order=1, ref="l3a"),
            _make_section_row(pid=project_id, title="1.1.2 资源配置", level=3, parent_ref="l2a",
                              content=_cn(250), sort_order=2, ref="l3b"),
            _make_section_row(pid=project_id, title="1.2", level=2, parent_ref="l1",
                              sort_order=2, ref="l2b"),
            _make_section_row(pid=project_id, title="1.2.1 安全保证", level=3, parent_ref="l2b",
                              content=_cn(310), sort_order=1, ref="l3c"),
        ])
        _invalidate_build_tree_cache(project_id)
        tree = build_tree(project_id)

        assert len(tree) == 1
        l1 = tree[0]
        assert l1.cumulative_word_count == 300 + 250 + 310

        l2a = [c for c in l1.children if c.id == ids["l2a"]][0]
        assert l2a.cumulative_word_count == 550
        by_id = {c.id: c for c in l2a.children}
        assert by_id[ids["l3a"]].cumulative_word_count == 300
        assert by_id[ids["l3b"]].cumulative_word_count == 250

    def test_leaf_cumulative_equals_own(self, project_id):
        _insert(project_id, [
            _make_section_row(pid=project_id, title="第一章", level=1, ref="l1"),
            _make_section_row(pid=project_id, title="1.1", level=2, parent_ref="l1", ref="l2"),
            _make_section_row(pid=project_id, title="1.1.1 施工部署", level=3, parent_ref="l2",
                              content=_cn(300), ref="l3"),
        ])
        _invalidate_build_tree_cache(project_id)
        tree = build_tree(project_id)
        leaf = tree[0].children[0].children[0]
        assert leaf.cumulative_word_count == leaf.word_count == 300

