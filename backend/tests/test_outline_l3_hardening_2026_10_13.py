"""2026-10-13 生成大纲模块加固回归（第三轮）。

覆盖本轮修复项（均有对应 BUG-FIX 注释锚点）：
  G1  中文数字占位判定（「专项六」「地下室专项三」穿透脏标题判定）
  G2  「（其N）」残渣深度判定（「总体实施策划（其三）」漏出）
  G4  `_derive_child_title` 主循环脏标题复检（池条目回归通道）
  G6  `_generate_differentiated_l3_title` 绝对兜底 NameError + 「专项之一」占位
  G7  三级标题 AI 修复多轮化（大目录单轮上限结构性残留）
  G10 `_cleanup_placeholder_titles` 清洗序号轮转（多样性）
  G11 规则兜底 used 集合跨任务累加（重复候选）
  G14 直出路径质量收口异常包裹
  状态 自检后 edited_outline 同步（安全网结果被丢弃）
  B5  OUTLINE_NUMBERING_TEMPLATE 环境变量生效
  B11 正文编号剥离误伤数量短语（「## 3 天内完成移交」）
  B2  删除/中间插入章节后正文内嵌编号净化
  B8  删除章节后 Redis 单章缓存直删
  取消 v51 任务 DB 行 cancelled 落库

运行：cd backend && .venv\\Scripts\\python.exe -m pytest tests/test_outline_l3_hardening_2026_10_13.py -q -p no:cacheprovider
"""

import pytest

from app.services.ai.outline_common import (
    _cleanup_placeholder_titles,
    _derive_child_title,
    _is_dirty_l3_title,
    _is_dirty_l3_title_deep,
    _semantic_fallback_title,
    _uniquify_outline_titles,
)


# ===========================================================================
# G1：中文数字占位判定
# ===========================================================================
class TestChineseNumeralPlaceholders:
    def test_exact_and_embedded_chinese_numeral_are_dirty(self):
        assert _is_dirty_l3_title("专项六")
        assert _is_dirty_l3_title("专项一")
        assert _is_dirty_l3_title("专项6")  # 旧形态不回退
        assert _is_dirty_l3_title("地下室专项三")
        assert _is_dirty_l3_title("分阶段推进专项十三")

    def test_non_numeral_zhuanxiang_not_dirty(self):
        """「专项施工方案」等合法词不得误判。

        注：「专项方案」本身在 `_GENERIC_ONLY_TITLES` 泛化词表内（既定设计），
        此处用不含泛化词面的组合词验证数字约束的精确性。
        """
        assert not _is_dirty_l3_title("专项施工方案")
        assert not _is_dirty_l3_title("安全专项施工组织")

    def test_project_specific_check_rejects_chinese_numeral(self):
        """实体词白名单不得成为中文数字占位的穿透通道（G1 直接漏出路径）。"""
        from app.services.ai.workflows_outline_utils import (
            _is_l3_title_project_specific,
        )

        assert not _is_l3_title_project_specific("地下室专项六", "质量管控")
        assert not _is_l3_title_project_specific("专项六", "质量管控")

    def test_cleanup_rewrites_chinese_numeral_placeholder(self):
        tree = [{
            "title": "质量管理措施",
            "children": [{
                "title": "总体方案与实施策划",
                "children": [{"title": "专项六", "children": []}],
            }],
        }]
        fixed = _cleanup_placeholder_titles(tree)
        assert fixed == 1
        new_title = tree[0]["children"][0]["children"][0]["title"]
        assert new_title != "专项六"
        assert not _is_dirty_l3_title(new_title)


# ===========================================================================
# G2：「（其N）」残渣深度判定
# ===========================================================================
class TestQiSuffixResidueDeepCheck:
    def test_generic_stem_with_qi_suffix_is_dirty(self):
        """「总体实施策划（其三）」原文不判脏，但剥离后即泛化词 → 深度判脏。"""
        assert not _is_dirty_l3_title("总体实施策划（其三）")
        assert _is_dirty_l3_title_deep("总体实施策划（其三）")
        assert _is_dirty_l3_title_deep("总体实施策划(其三)")  # 半角

    def test_concrete_title_with_qi_suffix_preserved(self):
        """设计取舍：合法标题 + 历史残渣只剥后缀，不整体替换（保业务语义）。"""
        assert not _is_dirty_l3_title_deep("砌体成品保护（其2）")
        tree = [{
            "title": "装饰装修工程",
            "children": [{
                "title": "成品保护措施",
                "children": [{"title": "砌体成品保护（其2）", "children": []}],
            }],
        }]
        fixed = _cleanup_placeholder_titles(tree)
        assert fixed == 0, "合法标题+残渣不应被整体替换"
        # 后续统一去重会剥掉「（其2）」后缀，保留主体
        out = _uniquify_outline_titles(tree)
        assert out[0]["children"][0]["children"][0]["title"] == "砌体成品保护"

    def test_cleanup_rewrites_generic_stem_residue(self):
        tree = [{
            "title": "总体施工部署",
            "children": [{
                "title": "总体方案与实施策划",
                "children": [{"title": "总体实施策划（其三）", "children": []}],
            }],
        }]
        fixed = _cleanup_placeholder_titles(tree)
        assert fixed == 1
        new_title = tree[0]["children"][0]["children"][0]["title"]
        assert not _is_dirty_l3_title_deep(new_title)


# ===========================================================================
# G4：_derive_child_title 主循环脏标题复检
# ===========================================================================
class TestDeriveChildTitleDirtyRecheck:
    def test_mass_derivation_never_produces_dirty(self):
        """大批量派生（used 持续增长）不得产出任何脏标题。

        注意快照时机：`_semantic_fallback_title` 按设计会把返回标题登记进
        used（自洽性），因此必须在**调用前**快照 used 判重。
        """
        used: set = set()
        for i in range(300):
            before = set(used)
            t = _derive_child_title("总体方案与实施策划", i, used, level=3,
                                    grandparent_title="质量管理措施")
            assert t and t not in before, f"重复候选: {t}"
            assert not _is_dirty_l3_title(t), f"兜底产出脏标题: {t}"

    def test_generic_parent_domain_derivation(self):
        used: set = set()
        for i in range(100):
            t = _derive_child_title("工程概况", i, used, level=3)
            assert not _is_dirty_l3_title(t), f"脏标题漏出: {t}"
            used.add(t)


# ===========================================================================
# G6：_generate_differentiated_l3_title 绝对兜底
# ===========================================================================
class TestDifferentiatedFallbackNoNameError:
    @pytest.mark.asyncio
    async def test_absolute_fallback_is_safe_and_non_dirty(self, monkeypatch):
        """强制走完所有兜底层级：旧实现在此处抛 NameError（core 未定义）。"""
        import app.services.ai.outline_common as oc
        import app.services.ai.workflows_outline_v51 as v51

        l2_title = "总体方案与实施策划"
        # 让 _derive_child_title 恒返回已用候选 → 前 8 轮全跳过
        monkeypatch.setattr(
            oc, "_derive_child_title",
            lambda *a, **k: l2_title,
        )
        # 让 _semantic_fallback_title 恒返回 l2_title 本身 → 循环内全跳过，
        # 强制进入最后的绝对兜底分支
        monkeypatch.setattr(
            oc, "_semantic_fallback_title",
            lambda *a, **k: l2_title,
        )
        used = {l2_title}
        result = v51._generate_differentiated_l3_title(
            l2_title, "总体方案", 0, 0, "总体施工部署", used,
        )
        assert result, "绝对兜底不得返回空"
        assert result != l2_title
        assert not _is_dirty_l3_title(result), f"绝对兜底产出脏标题: {result}"

    @pytest.mark.asyncio
    async def test_normal_path_unchanged(self):
        import app.services.ai.workflows_outline_v51 as v51

        used: set = set()
        result = v51._generate_differentiated_l3_title(
            "质量保证措施", "质量保证", 0, 0, "质量管理", used,
        )
        assert result and not _is_dirty_l3_title(result)


# ===========================================================================
# G10：清洗序号轮转（同父多个脏标题多样性）
# ===========================================================================
class TestCleanupIdxRotation:
    def test_multiple_dirty_siblings_get_varied_titles(self):
        children = [{"title": f"专项{i}", "children": []} for i in range(1, 6)]
        tree = [{
            "title": "总体施工部署",
            "children": [{"title": "总体方案与实施策划", "children": children}],
        }]
        fixed = _cleanup_placeholder_titles(tree)
        assert fixed == 5
        titles = [c["title"] for c in tree[0]["children"][0]["children"]]
        assert len(set(titles)) == 5, f"清洗后标题重复: {titles}"
        assert all(not _is_dirty_l3_title(t) for t in titles)


# ===========================================================================
# G11：规则兜底 used 跨任务累加
# ===========================================================================
class TestRuleFallbackUsedAccumulation:
    @pytest.mark.asyncio
    async def test_provider_down_fallbacks_are_distinct(self):
        from app.services.ai.workflows_outline_utils import (
            repair_l3_generic_titles,
        )

        class _BoomProvider:
            async def chat(self, messages, **kwargs):
                raise RuntimeError("provider down")

        # 同一 L2 下 4 个泛化标题，AI 全失败 → 全走规则兜底
        tree = [{
            "title": "总体施工部署",
            "children": [{
                "title": "总体方案与实施策划",
                "children": [
                    {"title": "总体实施策划", "children": []},
                    {"title": "分阶段推进计划", "children": []},
                    {"title": "资源配置与协调保障", "children": []},
                    {"title": "过程控制与考核评价", "children": []},
                ],
            }],
        }]
        out = await repair_l3_generic_titles(
            _BoomProvider(), tree, "", "", "", "", "", max_repair=8
        )
        titles = [c["title"] for c in out[0]["children"][0]["children"]]
        assert len(set(titles)) == 4, f"规则兜底产出重复标题: {titles}"
        assert all(not _is_dirty_l3_title(t) for t in titles), titles


# ===========================================================================
# G7：具体性收口多轮修复
# ===========================================================================
class TestSpecificityCloseoutRounds:
    class _DummyProvider:
        """占位 provider：修复函数已被 monkeypatch，不会被真正调用。"""

    def _tree_with_generic(self, n: int) -> list:
        """泛化但**非脏**的三级标题（「施工准备工作」命中 _L3_GENERIC_PATTERNS，
        但不在脏词表——只有 repair_l3_generic_titles 能修它，规则清洗不动它，
        这样才能独立验证多轮修复循环本身）。"""
        return [{
            "title": "总体施工部署",
            "children": [{
                "title": "总体方案与实施策划",
                "children": [
                    {"title": "施工准备工作", "children": []} for _ in range(n)
                ],
            }],
        }]

    @pytest.mark.asyncio
    async def test_rounds_stop_when_no_progress(self, monkeypatch):
        import app.services.ai.workflows_outline_v51 as v51

        calls = []

        async def _fake_repair(provider, outline, *a, **k):
            calls.append(1)
            return outline  # 什么都不修 → 无进展

        monkeypatch.setattr(v51, "repair_l3_generic_titles", _fake_repair)
        monkeypatch.setattr(v51, "OUTLINE_L3_REPAIR_MAX_ROUNDS", 3)
        out = await v51._l3_specificity_closeout(
            self._DummyProvider(), self._tree_with_generic(2), phase_callback=None,
        )
        assert out  # 不得清空目录
        assert len(calls) == 1, f"无进展时应只跑 1 轮，实际 {len(calls)}"

    @pytest.mark.asyncio
    async def test_rounds_continue_while_progress(self, monkeypatch):
        import app.services.ai.workflows_outline_v51 as v51

        calls = []

        async def _fake_repair(provider, outline, *a, **k):
            calls.append(1)
            # 每轮修掉一个泛化标题（改写为实体词标题）
            l3s = outline[0]["children"][0]["children"]
            for node in l3s:
                if node["title"] == "施工准备工作":
                    node["title"] = "地下室施工工艺"
                    break
            return outline

        monkeypatch.setattr(v51, "repair_l3_generic_titles", _fake_repair)
        monkeypatch.setattr(v51, "OUTLINE_L3_REPAIR_MAX_ROUNDS", 3)
        out = await v51._l3_specificity_closeout(
            self._DummyProvider(), self._tree_with_generic(2), phase_callback=None,
        )
        titles = [c["title"] for c in out[0]["children"][0]["children"]]
        assert "施工准备工作" not in titles, f"两轮后仍有泛化标题: {titles}"
        assert len(calls) == 2, f"有进展时应继续到修完，实际 {len(calls)} 轮"

    @pytest.mark.asyncio
    async def test_single_round_config_backward_compatible(self, monkeypatch):
        """OUTLINE_L3_REPAIR_MAX_ROUNDS=1 退回历史单轮行为。"""
        import app.services.ai.workflows_outline_v51 as v51

        calls = []

        async def _fake_repair(provider, outline, *a, **k):
            calls.append(1)
            l3s = outline[0]["children"][0]["children"]
            for node in l3s:
                if node["title"] == "施工准备工作":
                    node["title"] = "地下室施工工艺"
                    break
            return outline

        monkeypatch.setattr(v51, "repair_l3_generic_titles", _fake_repair)
        monkeypatch.setattr(v51, "OUTLINE_L3_REPAIR_MAX_ROUNDS", 1)
        await v51._l3_specificity_closeout(
            self._DummyProvider(), self._tree_with_generic(2), phase_callback=None,
        )
        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_residual_reported_via_phase_callback(self, monkeypatch):
        import app.services.ai.workflows_outline_v51 as v51

        async def _fake_repair(provider, outline, *a, **k):
            return outline

        monkeypatch.setattr(v51, "repair_l3_generic_titles", _fake_repair)
        monkeypatch.setattr(v51, "OUTLINE_L3_REPAIR_MAX_ROUNDS", 1)
        messages = []

        async def _cb(label, msg):
            messages.append(msg)

        await v51._l3_specificity_closeout(
            self._DummyProvider(), self._tree_with_generic(2), phase_callback=_cb,
        )
        assert any("泛化" in m for m in messages), f"残留未告知用户: {messages}"


# ===========================================================================
# G14：直出路径质量收口异常包裹
# ===========================================================================
class TestDirectCloseoutExceptionGuard:
    @pytest.mark.asyncio
    async def test_cleanup_exception_does_not_drop_outline(self, monkeypatch):
        import app.services.ai.workflows_outline_v51 as v51

        def _boom(_tree):
            raise RuntimeError("tree corrupted")

        monkeypatch.setattr(v51, "_cleanup_placeholder_titles", _boom)
        tree = [{"title": "章一", "children": []}]
        out = await v51._apply_l3_quality_closeout(None, tree, "", "", "", "")
        assert out == tree, "清洗异常时必须原样返回目录，不能把已生成成果作废"


# ===========================================================================
# 状态：自检后 edited_outline 同步
# ===========================================================================
class TestEditedOutlineSyncAfterSafetyNet:
    def test_refresh_when_still_selfcheck_object(self):
        import app.services.ai.workflows_outline_v51 as v51

        class _S:
            edited_outline = None

        st = _S()
        sc_tree = [{"title": "旧树"}]
        st.edited_outline = sc_tree
        new_tree = [{"title": "新树（安全网后）"}]
        v51._sync_edited_outline_after_safety_net(st, sc_tree, new_tree)
        assert st.edited_outline is new_tree

    def test_keep_user_edit_when_object_replaced(self):
        import app.services.ai.workflows_outline_v51 as v51

        class _S:
            edited_outline = None

        st = _S()
        sc_tree = [{"title": "自检树"}]
        user_tree = [{"title": "用户编辑"}]
        st.edited_outline = user_tree  # 自检后被用户编辑替换
        v51._sync_edited_outline_after_safety_net(st, sc_tree, [{"title": "安全网"}])
        assert st.edited_outline is user_tree, "用户编辑不得被自动树覆盖"

    def test_noop_when_never_assigned(self):
        import app.services.ai.workflows_outline_v51 as v51

        class _S:
            edited_outline = None

        st = _S()
        v51._sync_edited_outline_after_safety_net(st, None, [{"title": "x"}])
        assert st.edited_outline is None
        v51._sync_edited_outline_after_safety_net(None, [{"title": "x"}], [])


# ===========================================================================
# B5：OUTLINE_NUMBERING_TEMPLATE 环境变量生效
# ===========================================================================
class TestNumberingTemplateConfig:
    def test_custom_template_is_applied(self, monkeypatch):
        import app.config as cfg
        import app.services.ai.outline_gate as gate

        monkeypatch.setattr(cfg, "OUTLINE_NUMBERING_ENABLED", True, raising=False)
        monkeypatch.setattr(
            cfg, "OUTLINE_NUMBERING_TEMPLATE",
            '{"1": "{num}、", "2": "{num:1}.{num:2}", "3": "{num:1}.{num:2}.{num:3}"}',
            raising=False,
        )
        tree = [{
            "id": "1", "title": "章一",
            "children": [{"id": "1.1", "title": "节一", "children": []}],
        }]
        out, _ = gate.renumber_outline_ids(tree)
        assert out[0]["number"] == "1、", f"自定义模板未生效: {out[0].get('number')}"
        assert out[0]["children"][0]["number"] == "1.1"

    def test_default_template_when_env_missing(self, monkeypatch):
        import app.config as cfg
        import app.services.ai.outline_gate as gate

        monkeypatch.setattr(cfg, "OUTLINE_NUMBERING_ENABLED", True, raising=False)
        monkeypatch.setattr(cfg, "OUTLINE_NUMBERING_TEMPLATE", "", raising=False)
        tree = [{"id": "1", "title": "章一", "children": []}]
        out, _ = gate.renumber_outline_ids(tree)
        assert out[0]["number"] == "第1章"

    def test_invalid_template_falls_back_to_default(self, monkeypatch):
        import app.config as cfg
        import app.services.ai.outline_gate as gate

        monkeypatch.setattr(cfg, "OUTLINE_NUMBERING_ENABLED", True, raising=False)
        monkeypatch.setattr(cfg, "OUTLINE_NUMBERING_TEMPLATE", "{不是合法JSON", raising=False)
        tree = [{"id": "1", "title": "章一", "children": []}]
        out, _ = gate.renumber_outline_ids(tree)
        assert out[0]["number"] == "第1章", "非法模板必须回退默认模板"

    def test_disabled_by_default(self, monkeypatch):
        import app.config as cfg
        import app.services.ai.outline_gate as gate

        monkeypatch.setattr(cfg, "OUTLINE_NUMBERING_ENABLED", False, raising=False)
        tree = [{"id": "1", "title": "章一", "children": []}]
        out, _ = gate.renumber_outline_ids(tree)
        assert "number" not in out[0], "默认关闭时不得写 number 字段"


# ===========================================================================
# B11：正文编号剥离误伤数量短语
# ===========================================================================
class TestStripHeadingNumberingUnitGuard:
    def test_quantity_phrases_preserved(self):
        from app.services.content_cleaning import strip_heading_numbering_from_content

        cases = [
            "## 3 天内完成移交",
            "## 5 万元投资额",
            "## 10 平方米场地",
            "## 2026年施工计划",
            "## 1.8 米净高满足要求",
        ]
        for c in cases:
            assert strip_heading_numbering_from_content(c) == c, f"数量短语被误剥: {c}"

    def test_real_headings_still_stripped(self):
        from app.services.content_cleaning import strip_heading_numbering_from_content

        assert strip_heading_numbering_from_content("## 1.1 施工部署") == "## 施工部署"
        assert strip_heading_numbering_from_content("### 2.3.1 深化设计") == "### 深化设计"
        assert strip_heading_numbering_from_content("## 3、施工部署") == "## 施工部署"
        assert strip_heading_numbering_from_content("## 一、施工准备") == "## 施工准备"
        assert strip_heading_numbering_from_content("## 第2章 工程概况") == "## 工程概况"
        assert strip_heading_numbering_from_content("## 1）施工部署") == "## 施工部署"
        assert strip_heading_numbering_from_content("## 10 施工部署") == "## 施工部署"

    def test_setext_quantity_preserved_and_heading_stripped(self):
        from app.services.content_cleaning import strip_heading_numbering_from_content

        assert strip_heading_numbering_from_content("3 天内完成移交\n===") == "3 天内完成移交\n==="
        assert strip_heading_numbering_from_content("1.1 施工部署\n===") == "施工部署\n==="

    def test_code_block_untouched(self):
        from app.services.content_cleaning import strip_heading_numbering_from_content

        src = "```\n## 1.1 施工部署\n```"
        assert strip_heading_numbering_from_content(src) == src


# ===========================================================================
# B2/B8：章节增删的正文编号净化与单章缓存直删
# ===========================================================================
def _new_project(name: str) -> int:
    from app.db import get_db

    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO projects (name, industry) VALUES (?, '通用')", (name,)
        )
        return cur.lastrowid


def _seed_section(project_id: int, title: str, sort_order: int,
                  parent_id=None, content: str = "") -> int:
    from app.db import get_db

    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO sections (project_id, parent_id, title, level, sort_order, "
            "content, status) VALUES (?, ?, ?, ?, ?, ?, 'completed')",
            (project_id, parent_id, title, 2 if parent_id else 1, sort_order,
             content),
        )
        return cur.lastrowid


class TestSectionMutationBodyNumbering:
    def test_delete_section_strips_siblings_body_numbering(self):
        from app.db import get_db
        from app.services import sections as svc

        pid = _new_project("删除-正文编号净化")
        s1 = _seed_section(pid, "章一", 1, content="## 1.1 旧编号甲\n正文")
        s2 = _seed_section(pid, "章二", 2, content="## 1.2 旧编号乙\n正文")
        s3 = _seed_section(pid, "章三", 3, content="## 1.3 旧编号丙\n正文")

        result = svc.delete_section(s2)
        assert result["success"] is True

        with get_db() as conn:
            rows = {
                r["id"]: r["content"]
                for r in conn.execute(
                    "SELECT id, content FROM sections WHERE project_id = ?", (pid,)
                ).fetchall()
            }
        assert "旧编号甲" in rows[s1] and "1.1" not in rows[s1], rows[s1]
        assert "旧编号丙" in rows[s3] and "1.3" not in rows[s3], rows[s3]

    def test_delete_section_tree_strips_siblings(self):
        from app.db import get_db
        from app.services import sections as svc

        pid = _new_project("删除树-正文编号净化")
        s1 = _seed_section(pid, "章一", 1, content="## 1.1 甲\n正文")
        victim = _seed_section(pid, "章二", 2, content="## 1.2 乙\n正文")
        _seed_section(pid, "章二之子", 1, parent_id=victim, content="## 1.2.1 乙子\n正文")
        s3 = _seed_section(pid, "章三", 3, content="## 1.3 丙\n正文")

        svc.delete_section_tree(victim)

        with get_db() as conn:
            rows = {
                r["id"]: r["content"]
                for r in conn.execute(
                    "SELECT id, content FROM sections WHERE project_id = ?", (pid,)
                ).fetchall()
            }
        assert victim not in rows
        assert "1.1" not in rows[s1], rows[s1]
        assert "1.3" not in rows[s3], rows[s3]

    def test_midlist_insert_strips_subsequent_siblings(self):
        from app.db import get_db
        from app.models import SectionCreate
        from app.services import sections as svc

        pid = _new_project("中间插入-正文编号净化")
        _seed_section(pid, "章一", 1, content="## 1.1 甲\n正文")
        _seed_section(pid, "章二", 2, content="## 1.2 乙\n正文")
        _seed_section(pid, "章三", 3, content="## 1.3 丙\n正文")

        # 显式 sort_order=2 中间插入（与章二同值）。
        # BUG-FIX 2026-10-13（B2-2）：兄弟排序键 (sort_order, created_at) 升序，
        # 新章 created_at 最新 → 排在章二之后。故只有 sort_order **严格更大** 的
        # 章三(3) 真的后移（3→4）、编号应被净化；章二(2，同值未移动) 与章一(1)
        # 必须原样保留。旧实现用 `>= ?` 会把同值未移动的章二一并剥号（过度剥离），
        # 反而制造编号不一致，已改为 `> ?`。
        svc.create_section(pid, SectionCreate(
            title="新章", level=1, sort_order=2,
        ))

        with get_db() as conn:
            rows = {
                r["title"]: r["content"]
                for r in conn.execute(
                    "SELECT title, content FROM sections WHERE project_id = ?", (pid,)
                ).fetchall()
            }
        assert rows["章一"] == "## 1.1 甲\n正文", "更小 sort_order，位次不变不应改写"
        assert rows["章二"] == "## 1.2 乙\n正文", "同值未移动的章二不应被过度剥离（B2-2）"
        assert "1.3" not in rows["章三"], f"真正后移的章三编号未净化: {rows['章三']}"
        assert "丙" in rows["章三"], "剥号必须保留标题正文"

    def test_append_insert_does_not_rewrite_siblings(self):
        """追加尾部不改变任何兄弟编号，不得无谓改写正文（性能护栏）。"""
        from app.db import get_db
        from app.models import SectionCreate
        from app.services import sections as svc

        pid = _new_project("尾部追加-不净化")
        s1 = _seed_section(pid, "章一", 1, content="## 1.1 甲\n正文")

        svc.create_section(pid, SectionCreate(title="新章", level=1))

        with get_db() as conn:
            row = conn.execute(
                "SELECT content FROM sections WHERE id = ?", (s1,)
            ).fetchone()
        assert row["content"] == "## 1.1 甲\n正文", "尾部追加不应改写既有正文"

    def test_delete_section_invalidates_single_section_cache(self, monkeypatch):
        from app.services import sections as svc

        pid = _new_project("删除-单章缓存直删")
        sid = _seed_section(pid, "章一", 1)

        deleted_keys = []
        import app.services.redis_cache as rc

        monkeypatch.setattr(
            rc.CacheService, "invalidate_section",
            classmethod(lambda cls, section_id: deleted_keys.append(section_id)),
        )
        svc.delete_section(sid)
        assert deleted_keys == [sid], f"删除后未直删单章缓存键: {deleted_keys}"


# ===========================================================================
# 端到端：AI 在 L2 阶段注入全部脏标题形态 → 真实分步全链路 → 落库
# ===========================================================================
_DIRTY_L3_TITLES = [
    "总体实施策划（2）",
    "专项六",
    "总体实施策划（其三）",
    "内容3",
    "总体实施策划",
    "地下室专项三",
    "分阶段推进专项13",
    "总体实施策划(2)",  # 半角括号
]


class _E2EProvider:
    """AI 全链路 mock：L1 返回干净章节；L2 注入脏标题；自检/修复调用直接失败
    （走规则兜底路径，与生产「AI 不可用」降级口径一致）。"""

    class _Msg:
        def __init__(self, role, content):
            self.role = role
            self.content = content

    async def chat(self, messages, **kwargs):
        raise RuntimeError("e2e mock: AI 不可用")


async def _fake_collect_e2e(provider, messages, **kwargs):
    import json as _json
    import re as _re

    user = next((m.content for m in messages if m.role == "user"), "")
    if "<json_outline>" in user:
        # L2 填充阶段：从提示词里取骨架，注入脏标题后原样返回
        m = _re.search(r"<json_outline>\n(.*?)\n</json_outline>", user, _re.DOTALL)
        skeleton = _json.loads(m.group(1))
        for i, l2 in enumerate(skeleton.get("children", [])):
            l2["title"] = l2.get("title") or "总体方案与实施策划"
            l3s = l2.get("children") or []
            for k, l3 in enumerate(l3s):
                l3["title"] = _DIRTY_L3_TITLES[(i + k) % len(_DIRTY_L3_TITLES)]
        return skeleton
    # L1 阶段
    return [
        {"title": "总体施工部署", "children": []},
        {"title": "质量管理措施", "children": []},
    ]


class TestEndToEndDirtyTitlePipeline:
    @pytest.mark.asyncio
    async def test_stepwise_pipeline_cleans_all_dirty_titles(self, monkeypatch):
        """用户报告场景复现：L2 阶段 AI 产出「总体实施策划（2）」「专项六」等
        全部脏标题形态，真实分步链路（L3→兜底→清洗→去重→具体性收口→自检→
        安全网→renumber）之后，最终目录与落库结果必须全部干净。"""
        import app.services.ai.workflows_outline_v51 as wo

        monkeypatch.setattr(wo, "collect_json_response", _fake_collect_e2e)
        outline = await wo.generate_outline_v51(
            _E2EProvider(),
            "某房建工程概述",
            "[]",
            "建筑工程",
            auto_confirm=True,
            outline_mode="stepwise",
        )
        assert outline, "分步路径未产出目录"

        # 1) 全树无脏标题（深度判定，含中文数字与（其N）残渣）
        def _walk(nodes, level=1):
            for n in nodes:
                if level >= 3:
                    t = n.get("title", "")
                    assert t and t.strip(), f"空标题: {n}"
                    assert not _is_dirty_l3_title_deep(t), f"脏标题漏出: {t}"
                for c in n.get("children") or []:
                    _walk([c], level + 1)

        _walk(outline)

        # 2) 位置式 id 与层级严格一致
        def _check_ids(nodes, prefix=""):
            for i, n in enumerate(nodes, 1):
                expect = f"{prefix}{i}" if not prefix else f"{prefix}.{i}"
                assert str(n.get("id")) == expect, f"id 错位: {n.get('id')} != {expect}"
                _check_ids(n.get("children") or [], expect)

        _check_ids(outline)

        # 3) 落库后 DB 行同样干净（落库层安全网 + 编号命名空间）
        from app.routers.sse.sse_db_ops import _clear_and_save_outline
        from app.db import get_db

        pid = _new_project("E2E-脏标题全链路")
        saved = _clear_and_save_outline(pid, outline)
        with get_db() as conn:
            rows = conn.execute(
                "SELECT title, level FROM sections WHERE project_id = ?", (pid,)
            ).fetchall()
        assert rows, "落库结果为空"
        for r in rows:
            if r["level"] >= 3:
                assert not _is_dirty_l3_title_deep(r["title"]), f"DB 脏标题: {r['title']}"
        # 落库返回树的 sort_order 1 起连续
        for n in saved:
            assert [c["sort_order"] for c in n["children"]] == list(
                range(1, len(n["children"]) + 1)
            )

    @pytest.mark.asyncio
    async def test_stepwise_pipeline_ids_consistent_after_edits(self, monkeypatch):
        """标题修改/重排后编号重算：人工编辑打乱 id 后，终态 renumber 修正。"""
        import app.services.ai.workflows_outline_v51 as wo

        monkeypatch.setattr(wo, "collect_json_response", _fake_collect_e2e)

        captured = {}

        async def _fake_review(outline_queue, v51_state, phase, outline, **kwargs):
            # 最终审核阶段：用户改标题 + 打乱 id
            if phase == "final":
                captured["before"] = [n["title"] for n in outline]
                for n in outline:
                    n["id"] = "X"
                return outline
            return outline

        monkeypatch.setattr(wo, "_wait_for_v51_review", _fake_review)
        outline = await wo.generate_outline_v51(
            _E2EProvider(),
            "某工程概述",
            "[]",
            "建筑工程",
            auto_confirm=False,
            outline_mode="stepwise",
        )
        assert outline
        for i, n in enumerate(outline, 1):
            assert str(n.get("id")) == str(i), f"编辑后编号未重算: {n.get('id')}"


# ===========================================================================
# L1：编号命名空间单一事实源（四出口一致性）
# ===========================================================================
class TestPathNumberNamespace:
    """同一结构 → 四个编号出口必须一致（BUG-FIX 2026-10-13 L1）。

    出口：
      1. `outline_gate.renumber_outline_ids` 的位置 id（内存树，门禁修正）
      2. `outline_common.tree_path_numbers`（树版批量，公共 API）
      3. `sse_db_ops._clear_and_save_outline` 透出的 path_number（落库）
      4. `sections.get_section_path`（DB 动态路径号，导出/正文消费）
    """

    def _tree(self) -> list:
        return [
            {"title": "章一", "children": [
                {"title": "节一", "children": [
                    {"title": "条一"},
                    {"title": "条二"},
                ]},
                {"title": "节二"},
            ]},
            {"title": "章二", "children": [
                {"title": "节一"},
            ]},
        ]

    def test_child_number_algorithm(self):
        from app.services.ai.outline_common import child_number

        assert child_number("", 1) == "1"
        assert child_number("1", 2) == "1.2"
        assert child_number("1.2", 3) == "1.2.3"
        assert child_number("", 12) == "12"
        # BUG-FIX 2026-10-13（L1 实证）：`_save_nodes` 递归约定父号带尾点，
        # `renumber_outline_ids` 约定不带点——统一函数对两者必须给同一输出。
        assert child_number("1.", 1) == child_number("1", 1) == "1.1"
        assert child_number("1.2.", 3) == child_number("1.2", 3) == "1.2.3"

    def test_tree_path_numbers_dfs_order(self):
        from app.services.ai.outline_common import tree_path_numbers

        assert tree_path_numbers(self._tree()) == [
            "1", "1.1", "1.1.1", "1.1.2", "1.2", "2", "2.1",
        ]
        assert tree_path_numbers([]) == []
        # 非 dict 节点容错跳过
        assert tree_path_numbers([{"title": "A"}, "oops"]) == ["1"]

    def test_renumber_ids_match_tree_path_numbers(self):
        from app.services.ai.outline_common import tree_path_numbers
        from app.services.ai.outline_gate import renumber_outline_ids

        tree = self._tree()
        expect = tree_path_numbers(tree)
        renumber_outline_ids(tree)
        actual = []

        def _walk(nodes):
            for n in nodes:
                actual.append(str(n["id"]))
                _walk(n.get("children") or [])

        _walk(tree)
        assert actual == expect, f"门禁 id 与树版编号不一致: {actual} != {expect}"

    def test_four_outlets_agree(self):
        """落库透出 path_number、DB get_section_path、门禁 id 四者一致。"""
        from app.db import get_db
        from app.routers.sse.sse_db_ops import _clear_and_save_outline
        from app.services.ai.outline_common import tree_path_numbers
        from app.services.ai.outline_gate import renumber_outline_ids
        from app.services.sections import get_section_path

        pid = _new_project("L1-编号四出口一致")
        tree = self._tree()
        # 出口 2：树版
        expect = tree_path_numbers(tree)
        # 出口 1：门禁 id（同一算法）
        renumber_outline_ids(tree)
        gate_ids = []

        def _walk(nodes):
            for n in nodes:
                gate_ids.append(str(n["id"]))
                _walk(n.get("children") or [])

        _walk(tree)
        assert gate_ids == expect

        # 出口 3：落库透出 number（DFS 顺序；字段名为 `number`，见 saved_node 构造）
        saved = _clear_and_save_outline(pid, tree)
        saved_numbers = []

        def _walk_saved(nodes):
            for n in nodes:
                saved_numbers.append(n.get("number"))
                _walk_saved(n.get("children") or [])

        _walk_saved(saved)
        assert saved_numbers == expect, (
            f"落库 path_number 与树版编号不一致: {saved_numbers} != {expect}"
        )

        # 出口 4：DB 动态路径号（按 saved 树的 DFS 顺序逐点取）
        db_numbers = []

        def _walk_db(nodes):
            for n in nodes:
                db_numbers.append(get_section_path(n["id"]))
                _walk_db(n.get("children") or [])

        _walk_db(saved)
        assert db_numbers == expect, (
            f"get_section_path 与树版编号不一致: {db_numbers} != {expect}"
        )


# ===========================================================================
# 取消 v51 任务 DB 行 cancelled 落库
# ===========================================================================
class TestCancelMarksDbRow:
    def test_cancel_marks_v51_tasks_row(self):
        from app.db import get_db
        from app.services import v51_state as vs

        pid = _new_project("取消落库")
        state = vs.create_v51_state(pid)
        try:
            assert vs.save_v51_state_to_db(state) is True
            assert vs.cancel_v51_task(pid) is True
            with get_db() as conn:
                row = conn.execute(
                    "SELECT cancelled FROM v51_tasks WHERE project_id = ?", (pid,)
                ).fetchone()
            assert row is not None and row["cancelled"] == 1, "取消后 DB 行 cancelled 未置 1"
        finally:
            vs.remove_v51_state(pid)
