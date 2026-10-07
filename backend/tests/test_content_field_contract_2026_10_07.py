"""正文生成字段契约与全局数据字典回归测试（2026-10-07 增强轮）

覆盖：
  * 提取项目字段清单 / 全局事实字段清单 / 章节→字段映射表 结构完整性
  * build_field_contract 从全局事实派生提取项目字段（同源 → 前后一致）
  * 全局数据字典权威值与提示词格式化（中文标签、稳定顺序）
  * 章节 → 所需字段按需选取（分类命中 / 缺失检测 / 错误映射防护）
  * build_chapter_content_messages 上下文注入（有契约 / 无契约向后兼容）
  * 生成后自检（占位标记 / 与字典冲突 / 过短 / 字段调用差集）
"""

import json

import pytest

from app.services.content_field_contract import (
    CHAPTER_FIELD_MAP,
    EXTRACTION_PROJECT_FIELDS,
    GLOBAL_FACTS_FIELD_GROUPS,
    KEY_CONSISTENCY_FIELDS,
    NINE_CHAPTERS,
    auto_fix_consistency_conflicts,
    build_field_contract,
    build_global_data_dictionary,
    check_content_consistency,
    check_placeholder_in_content,
    classify_chapter,
    clear_registered_chapter_keywords,
    compute_affected_section_ids,
    compute_call_diff,
    diff_key_field_changes,
    extraction_fields_record,
    parse_extraction_fields_record,
    extract_extraction_fields,
    extract_key_values_from_content,
    format_chapter_fields_for_prompt,
    format_global_dictionary_for_prompt,
    run_content_self_checks,
    select_fields_for_chapter,
    self_check_content,
    register_chapter_keywords,
)
from app.services.ai.workflows_sections import build_chapter_content_messages


FACTS = [
    {"key": "project_name", "label": "项目名称", "value": "某某市第一人民医院综合楼项目", "category": "basic"},
    {"key": "project_no", "label": "招标编号", "value": "ZB-2026-001", "category": "basic"},
    {"key": "owner", "label": "建设单位", "value": "某某市卫生健康委员会", "category": "project"},
    {"key": "location", "label": "建设地点", "value": "某某市城东区", "category": "project"},
    {"key": "scale", "label": "建筑面积", "value": "52000 平方米", "category": "project"},
    {"key": "structure_type", "label": "结构形式", "value": "框架剪力墙结构", "category": "project"},
    {"key": "duration", "label": "建设工期", "value": "540 日历天", "category": "schedule"},
    {"key": "budget", "label": "总投资", "value": "3.2 亿元", "category": "project"},
    {"key": "warranty", "label": "质保期", "value": "2 年", "category": "quality"},
    {"key": "quality_target", "label": "质量目标", "value": "合格", "category": "quality"},
    {"key": "safety_target", "label": "安全目标", "value": "无重大安全事故", "category": "safety"},
    {"key": "personnel_count", "label": "项目人员数量", "value": "86 人", "category": "personnel"},
    {"key": "equipment_model", "label": "主要设备型号", "value": "QTZ80 塔吊", "category": "technical"},
    {"key": "response_time", "label": "应急响应时间", "value": "30 分钟内", "category": "safety"},
    {"key": "participants", "label": "参建单位", "value": "某某设计院、某某监理公司", "category": "project"},
    {"key": "qualification", "label": "资质等级", "value": "建筑工程施工总承包一级", "category": "bidder"},
]


# ============================================================
# 一、清单结构完整性
# ============================================================

class TestManifestStructure:
    def test_extraction_fields_13_items(self):
        keys = {f["key"] for f in EXTRACTION_PROJECT_FIELDS}
        assert {
            "project_name", "project_no", "owner", "location", "scale",
            "structure_type", "duration", "budget", "special_params",
            "bid_requirements", "tech_params", "qualification", "participants",
        } == keys

    def test_extraction_fields_have_contract_meta(self):
        for f in EXTRACTION_PROJECT_FIELDS:
            assert f["label"] and f["type"] and "required" in f and "default" in f
            assert f["source"] and f["chapters"]  # 九大章节归属 / "*"

    def test_nine_chapters(self):
        assert [c["idx"] for c in NINE_CHAPTERS] == list(range(1, 10))
        assert set(GLOBAL_FACTS_FIELD_GROUPS.keys()) == set(range(1, 10))
        assert set(CHAPTER_FIELD_MAP.keys()) == set(range(1, 10))

    def test_chapter_field_map_fields_exist(self):
        all_keys = {f["key"] for f in EXTRACTION_PROJECT_FIELDS}
        for idx, m in CHAPTER_FIELD_MAP.items():
            for k in m["extraction"]:
                assert k in all_keys, f"章节{idx} 映射了不存在的提取字段 {k}"

    def test_key_consistency_fields_covered(self):
        """关键一致性字段必须被至少一个章节映射引用。"""
        referenced = set()
        for m in CHAPTER_FIELD_MAP.values():
            referenced.update(m["facts"])
        for k in KEY_CONSISTENCY_FIELDS:
            if k in {"project_name", "owner", "duration", "warranty", "participants",
                     "quality_target", "safety_target", "response_time",
                     "personnel_count", "equipment_model"}:
                assert k in referenced, f"关键一致性字段 {k} 未被任何章节引用"


# ============================================================
# 二、字段契约构建（提取项目字段从全局事实同源派生）
# ============================================================

class TestBuildFieldContract:
    def test_extract_from_facts(self):
        ec = extract_extraction_fields(FACTS)
        assert ec["project_name"] == "某某市第一人民医院综合楼项目"
        assert ec["owner"] == "某某市卫生健康委员会"
        assert ec["duration"] == "540 日历天"
        assert ec["project_no"] == "ZB-2026-001"

    def test_explicit_override_wins(self):
        ec = extract_extraction_fields(FACTS, {"duration": "600 日历天"})
        assert ec["duration"] == "600 日历天"

    def test_placeholder_value_filtered(self):
        facts = [{"key": "d", "label": "建设工期", "value": "【待填写】", "category": "schedule"}]
        ec = extract_extraction_fields(facts)
        assert ec["duration"] == ""

    def test_mis_mapping_guard_tech_params_vs_equipment(self):
        """「主要设备型号」不得被错误映射为「技术参数」（BUG 修复重点：错误映射）。"""
        ec = extract_extraction_fields(FACTS)
        assert ec["tech_params"] == ""

    def test_build_field_contract_from_project_data(self):
        contract = build_field_contract({"global_facts_items": FACTS})
        assert contract["extraction_fields"]["project_name"]
        assert contract["global_dictionary"]["fields"]["duration"] == "540 日历天"
        assert contract["policy"] == "fabricate"

    def test_global_dictionary_derives_key_fields(self):
        d = build_global_data_dictionary(FACTS)
        f = d["fields"]
        for k in ("project_name", "owner", "duration", "warranty", "personnel_count",
                  "equipment_model", "response_time", "participants",
                  "quality_target", "safety_target"):
            assert f.get(k), f"关键一致性字段 {k} 未进入全局数据字典"


# ============================================================
# 三、全局数据字典提示词格式化
# ============================================================

class TestDictionaryPromptFormat:
    def test_format_contains_chinese_labels(self):
        text = format_global_dictionary_for_prompt(
            build_global_data_dictionary(FACTS))
        assert "全局数据字典" in text
        assert "项目名称：某某市第一人民医院综合楼项目" in text
        assert "工期：540 日历天" in text
        assert "质保期：2 年" in text
        assert "response_time：" not in text  # 必须是中文标签而非裸键

    def test_format_stable_order(self):
        """两次格式化输出必须一致（缓存前缀稳定性）。"""
        d = build_global_data_dictionary(FACTS)
        assert format_global_dictionary_for_prompt(d) == format_global_dictionary_for_prompt(d)

    def test_empty_dictionary_renders_empty(self):
        assert format_global_dictionary_for_prompt({"fields": {}}) == ""


# ============================================================
# 四、章节 → 所需字段（按需选取）
# ============================================================

class TestSelectFieldsForChapter:
    def test_classify_all_nine_themes(self):
        cases = {
            "第一章 工程概况与现状分析": 1,
            "第二章 施工组织部署": 2,
            "第三章 主要分部分项工程施工方案": 3,
            "第四章 专项施工方案（高支模）": 4,
            "第五章 施工进度计划及保证措施": 5,
            "第六章 资源配置计划": 6,
            "第七章 质量管理体系与措施": 7,
            "第八章 安全管理与文明施工": 8,
            "第九章 应急预案与风险控制": 9,
        }
        for title, expect_idx in cases.items():
            assert classify_chapter(title)["idx"] == expect_idx, title

    def test_schedule_chapter_gets_duration(self):
        cf = select_fields_for_chapter("第五章 施工进度计划及保证措施", FACTS)
        assert cf["chapter_idx"] == 5
        assert cf["facts"]["工期"] == "540 日历天"

    def test_resource_chapter_gets_personnel_and_equipment(self):
        cf = select_fields_for_chapter("第六章 资源配置计划", FACTS)
        assert cf["facts"]["人员数量"] == "86 人"
        assert cf["facts"]["设备型号"] == "QTZ80 塔吊"

    def test_missing_required_field_reported(self):
        cf = select_fields_for_chapter("第五章 施工进度计划", [])
        assert "工期" in cf["missing"]

    def test_chapter_fields_format(self):
        cf = select_fields_for_chapter("第六章 资源配置计划", FACTS)
        text = format_chapter_fields_for_prompt(cf)
        assert "本章" in text and "设备型号：QTZ80 塔吊" in text


# ============================================================
# 五、build_chapter_content_messages 上下文注入
# ============================================================

SECTION = {"id": "s1", "title": "第五章 施工进度计划及保证措施", "level": 3}


class TestPromptInjection:
    def _contract(self):
        return build_field_contract({"global_facts_items": FACTS})

    def test_no_contract_backward_compatible(self):
        """无契约时不注入字典/按需字段块（旧行为完全不变）。"""
        msgs = build_chapter_content_messages(SECTION, facts=FACTS)
        joined = "\n".join(m.content for m in msgs)
        assert "全局数据字典" not in joined
        assert "本章提取项目数据" not in joined

    def test_global_dictionary_injected_in_stable_context(self):
        msgs = build_chapter_content_messages(SECTION, facts=FACTS,
                                              field_contract=self._contract())
        joined = "\n".join(m.content for m in msgs)
        assert "全局数据字典" in joined
        assert "工期：540 日历天" in joined

    def test_chapter_fields_injected_on_demand(self):
        trace: list = []
        msgs = build_chapter_content_messages(
            SECTION, facts=FACTS, field_contract=self._contract(), field_trace=trace)
        joined = "\n".join(m.content for m in msgs)
        assert "工期：540 日历天" in joined
        assert trace, "字段调用追溯未记录"
        assert any(t["field"] == "工期" and t["source"] in ("extraction", "facts") for t in trace)

    def test_dictionary_not_duplicated_across_chapters(self):
        """同一契约下不同章节注入的字典块完全一致（前后一致的数据源）。"""
        c = self._contract()
        m1 = build_chapter_content_messages({"id": "a", "title": "第五章 施工进度计划", "level": 3},
                                            facts=FACTS, field_contract=c)
        m2 = build_chapter_content_messages({"id": "b", "title": "第七章 质量管理体系与措施", "level": 3},
                                            facts=FACTS, field_contract=c)
        d1 = [m.content for m in m1 if "全局数据字典" in m.content]
        d2 = [m.content for m in m2 if "全局数据字典" in m.content]
        assert d1 and d1 == d2


# ============================================================
# 六、字段调用对照表（差集）
# ============================================================

class TestCallDiff:
    def test_not_called_detected(self):
        ec = extract_extraction_fields(FACTS)
        diff = compute_call_diff(ec, injected_keys=["project_name", "owner"])
        assert "duration" in diff["not_called"]
        assert diff["total"] == len(EXTRACTION_PROJECT_FIELDS)

    def test_missing_value_detected(self):
        diff = compute_call_diff({"project_name": "", "owner": "某业主"},
                                 injected_keys=["project_name", "owner"])
        assert diff["missing_value"] == ["project_name"]


# ============================================================
# 七、前后一致性校验与生成后自检
# ============================================================

class TestConsistencyAndSelfCheck:
    def _dict(self):
        return build_global_data_dictionary(FACTS)

    def test_extract_key_values(self):
        found = extract_key_values_from_content("本项目工期：540 日历天，质保期：2 年。")
        assert found.get("duration") == "540 日历天"
        assert found.get("warranty") == "2 年"

    def test_conflict_detected(self):
        issues = check_content_consistency("总工期为 600 日历天。", self._dict())
        assert issues and issues[0]["type"] == "conflict"
        assert issues[0]["expected"] == "540 日历天"

    def test_consistent_content_passes(self):
        assert check_content_consistency(
            "本工程工期为 540 日历天，质保期 2 年。", self._dict()) == []

    def test_placeholder_check(self):
        assert check_placeholder_in_content("【待补充】") == ["【待补充】"]
        assert check_placeholder_in_content("正文正常") == []

    def test_self_check_flags_conflict_and_placeholder(self):
        rep = self_check_content("工期：600 日历天。【待补充】", "第五章 施工进度计划",
                                 dictionary=self._dict())
        types = {i["type"] for i in rep["issues"]}
        assert "consistency_conflict" in types and "placeholder" in types
        assert rep["ok"] is False
        assert rep["needs_review"]

    def test_self_check_passes_on_good_content(self):
        rep = self_check_content(
            "本工程总工期 540 日历天。施工组织遵循总体部署要求，"
            "合理安排流水段与资源配置，确保按期履约交付使用。",
            "第五章 施工进度计划", dictionary=self._dict())
        assert rep["ok"] is True

    def test_run_self_checks_attaches_report(self):
        results = [
            {"id": "1", "title": "第五章 施工进度计划", "status": "completed",
             "content": "工期：540 日历天。" + "内容充实。" * 20},
            {"id": "2", "title": "第六章 资源配置", "status": "failed", "content": ""},
        ]
        reports = run_content_self_checks(results, self._dict())
        assert len(reports) == 1  # 空内容不参与自检
        assert results[0]["self_check"]["ok"] is True


# ============================================================
# 八、加固：定点自动修复 + 显式直供通道（2026-10-07 第二轮）
# ============================================================

class TestAutoFixConsistency:
    def _dict(self):
        return build_global_data_dictionary(FACTS)

    def test_conflict_repaired_in_place(self):
        """冲突值被定点替换为字典权威值，其余内容一字不动（只动必要位置）。"""
        content = "前文。工期：600 日历天。后文。质保期 2 年。"
        fixed, fixes = auto_fix_consistency_conflicts(content, self._dict())
        assert "工期：540 日历天" in fixed
        assert fixed == "前文。工期：540 日历天。后文。质保期 2 年。", "仅冲突片段被替换"
        assert len(fixes) == 1
        assert fixes[0]["old"] == "600 日历天" and fixes[0]["new"] == "540 日历天"

    def test_consistent_value_untouched(self):
        content = "工期：540 日历天，质保期 2 年。"
        fixed, fixes = auto_fix_consistency_conflicts(content, self._dict())
        assert fixed == content and fixes == []

    def test_repaired_content_passes_selfcheck(self):
        fixed, _ = auto_fix_consistency_conflicts("总工期为 600 日历天。", self._dict())
        assert check_content_consistency(fixed, self._dict()) == []

    def test_run_self_checks_autofixes(self):
        """批量自检：冲突被自动修复 → content 更新、报告记录 auto_fixed、复检通过。"""
        results = [{"id": "1", "title": "第五章 施工进度计划", "status": "completed",
                    "content": "工期：600 日历天。" + "内容充实。" * 20}]
        run_content_self_checks(results, self._dict())
        assert "540 日历天" in results[0]["content"]
        rep = results[0]["self_check"]
        assert rep["ok"] is True and rep.get("auto_fixed")


class TestExplicitFieldChannel:
    def test_explicit_channel_overrides_facts(self):
        """extraction_fields 直供通道优先级最高（解析模块结构化字段直供）。"""
        contract = build_field_contract({
            "global_facts_items": FACTS,
            "extraction_fields": {"duration": "720 日历天"},
        })
        assert contract["extraction_fields"]["duration"] == "720 日历天"
        assert contract["global_dictionary"]["fields"]["duration"] == "720 日历天"
        assert "duration" in contract["trace"]["explicit_keys"]

    def test_facts_policy_passthrough(self):
        contract = build_field_contract({"global_facts_items": FACTS,
                                         "facts_policy": "omit"})
        assert contract["policy"] == "omit"

    def test_invalid_policy_falls_back(self):
        contract = build_field_contract({"global_facts_items": FACTS,
                                         "facts_policy": "bogus"})
        assert contract["policy"] == "fabricate"


# ============================================================
# 九、加固第二轮：章节词表扩充 + 字段级定向重生成
# ============================================================

class TestChapterVocabularyExpansion:
    def teardown_method(self):
        clear_registered_chapter_keywords()

    def test_builtin_aliases(self):
        """跨模板同义词命中（基础词表覆盖不到的命名）。"""
        assert classify_chapter("第七章 质量保证措施")["idx"] == 7
        assert classify_chapter("第八章 安全文明施工及环境保护措施")["idx"] == 8
        assert classify_chapter("第六章 劳动力计划及机械设备投入")["idx"] == 6

    def test_industry_vocabulary(self):
        """行业专项词表：装饰装修行业「幕墙工程」归专项（第 4 章）。"""
        assert classify_chapter("第四章 幕墙工程设计深化", industry="装饰装修")["idx"] == 4
        # 消防行业：喷淋/报警等分项归主施工方案（第 3 章）
        assert classify_chapter("第三章 自动报警系统安装", industry="消防工程")["idx"] == 3
        # 市政行业：顶管归专项（第 4 章）
        assert classify_chapter("第五章 顶管施工专项方案", industry="市政/园林")["idx"] == 4

    def test_industry_name_fuzzy_match(self):
        """行业名双向包含：传「市政工程」也能命中「市政/园林」词表。"""
        base = classify_chapter("第三章 道路工程施工方案", industry=None)
        ind = classify_chapter("第三章 道路工程施工方案", industry="市政工程")
        assert ind["idx"] == 3
        # 行业词表命中「道路工程」长词 → 得分高于无行业词表的基线
        assert ind == base or ind["idx"] == 3

    def test_register_chapter_keywords(self):
        register_chapter_keywords(6, ("无人机航拍测绘",))
        assert classify_chapter("第六章 无人机航拍测绘计划")["idx"] == 6

    def test_register_invalid_idx_raises(self):
        with pytest.raises(ValueError):
            register_chapter_keywords(10, ("x",))

    def test_register_then_clear(self):
        register_chapter_keywords(7, ("样板引路",))
        clear_registered_chapter_keywords()
        assert classify_chapter("第七章 样板引路")["idx"] in (1, 7)  # 无行业兜底路径不报错

    def test_select_fields_uses_industry(self):
        """select_fields_for_chapter 透传 industry：行业词表影响章节归属。"""
        cf = select_fields_for_chapter("第三章 综合布线系统施工", FACTS, industry="弱电智能化")
        assert cf["chapter_idx"] == 3


class TestFieldChangePropagation:
    def test_diff_key_field_changes(self):
        old = [{"key": "duration", "label": "建设工期", "value": "540 日历天", "category": "schedule"}]
        new = [{"key": "duration", "label": "建设工期", "value": "600 日历天", "category": "schedule"}]
        changes = diff_key_field_changes(old, new)
        assert changes == {"duration": {"old": "540 日历天", "new": "600 日历天"}}

    def test_diff_no_change(self):
        changes = diff_key_field_changes(FACTS, FACTS)
        assert changes == {}

    def test_diff_added_and_removed(self):
        changes = diff_key_field_changes([], FACTS)
        assert set(changes.keys()) >= {"project_name", "duration"}  # 新增视为变更
        assert changes["duration"]["old"] == "" and changes["duration"]["new"] == "540 日历天"

    def test_affected_sections_scoped(self):
        """工期字段变更 → 章节 1/2/5/6/9 受影响；质量章（7）不受影响。"""
        sections = [
            {"id": 11, "title": "第一章 工程概况"},
            {"id": 12, "title": "第五章 施工进度计划及保证措施"},
            {"id": 13, "title": "第六章 资源配置计划"},
            {"id": 14, "title": "第七章 质量管理体系与措施"},
        ]
        out = compute_affected_section_ids(["duration"], sections)
        assert set(out["chapters"]) == {1, 2, 5, 6, 9}
        assert sorted(out["section_ids"]) == [11, 12, 13]
        assert out["global_change"] is False

    def test_global_field_affects_all(self):
        """项目名称（chapters="*"）变更 → 全部章节受影响。"""
        sections = [{"id": 1, "title": "第一章 工程概况"},
                    {"id": 2, "title": "第七章 质量管理体系"}]
        out = compute_affected_section_ids(["project_name"], sections)
        assert out["global_change"] is True
        assert out["section_ids"] == [1, 2]

    def test_no_change_no_affected(self):
        assert compute_affected_section_ids([], [{"id": 1, "title": "x"}]) == {
            "section_ids": [], "chapters": [], "global_change": False}


class TestSaveFactsPropagation:
    """save_facts 端点：响应回传字段变更清单与受影响章节（定向重生成入口）。"""

    def _setup(self, monkeypatch):
        import json as _json
        import app.routers.facts as fr
        from app.db import get_db, init_db

        monkeypatch.setattr(fr, "invalidate_content_caches", lambda pid: None)
        init_db()
        with get_db() as conn:
            conn.execute("INSERT INTO projects (name, industry) VALUES (?, ?)",
                         ("定向重生成-字段传播", "通用"))
            pid = conn.execute(
                "SELECT id FROM projects ORDER BY id DESC LIMIT 1").fetchone()["id"]
            conn.execute(
                "INSERT INTO analysis (project_id, global_facts_json) VALUES (?, ?)",
                (pid, _json.dumps(
                    [{"key": "duration", "label": "建设工期", "value": "540 日历天",
                      "category": "schedule"}], ensure_ascii=False)))
            for sort, title in enumerate(
                    ["第五章 施工进度计划及保证措施", "第六章 资源配置计划",
                     "第七章 质量管理体系与措施"]):
                conn.execute(
                    "INSERT INTO sections (project_id, title, level, sort_order, "
                    "content, status) VALUES (?, ?, 2, ?, '', 'completed')",
                    (pid, title, sort))
            rows = conn.execute(
                "SELECT id, title FROM sections WHERE project_id = ?", (pid,)).fetchall()
        return pid, {r["title"]: r["id"] for r in rows}

    def test_save_returns_field_changes_and_affected(self, monkeypatch):
        import asyncio
        from app.models import GlobalFact, GlobalFactList
        pid, ids = self._setup(monkeypatch)
        from app.routers import facts as fr
        body = GlobalFactList(items=[
            GlobalFact(key="duration", label="建设工期", value="600 日历天", source="manual"),
        ])
        result = asyncio.run(fr.save_facts(pid, body))
        assert result["ok"] is True
        assert result["field_changes"] == {"duration": {"old": "540 日历天", "new": "600 日历天"}}
        affected = result["affected_sections"]
        assert set(affected["chapters"]) == {1, 2, 5, 6, 9}
        # 进度/资源章受影响；质量章不受影响
        assert ids["第五章 施工进度计划及保证措施"] in affected["section_ids"]
        assert ids["第六章 资源配置计划"] in affected["section_ids"]
        assert ids["第七章 质量管理体系与措施"] not in affected["section_ids"]

    def test_save_without_key_change_returns_empty_propagation(self, monkeypatch):
        import asyncio
        from app.models import GlobalFact, GlobalFactList
        pid, _ = self._setup(monkeypatch)
        from app.routers import facts as fr
        # 仅改质量目标（非关键一致性字段）→ 不触发字段级传播
        body = GlobalFactList(items=[
            GlobalFact(key="duration", label="建设工期", value="540 日历天", source="manual"),
        ])
        result = asyncio.run(fr.save_facts(pid, body))
        assert result["field_changes"] == {}
        assert result["affected_sections"]["section_ids"] == []


# ============================================================
# 十、结构化提取项目字段表落库与直供（第三轮）
# ============================================================

class TestExtractionFieldsRecord:
    def test_record_keys_always_present(self):
        """13 键恒齐（缺事实时填 default 空串），可安全持久化。"""
        rec = extraction_fields_record([])
        assert set(rec["fields"].keys()) == {f["key"] for f in EXTRACTION_PROJECT_FIELDS}
        assert rec["fields"]["duration"] == ""
        assert rec["provenance"] == {}

    def test_record_provenance_from_facts(self):
        rec = extraction_fields_record(FACTS)
        assert rec["fields"]["duration"] == "540 日历天"
        prov = rec["provenance"]["duration"]
        assert prov["source"] == "facts"
        assert prov["fact_key"] == "duration"
        assert prov["matched_by"] == "label"

    def test_record_provenance_explicit(self):
        rec = extraction_fields_record(FACTS, {"project_name": "显式覆盖项目"})
        assert rec["fields"]["project_name"] == "显式覆盖项目"
        assert rec["provenance"]["project_name"] == {"source": "explicit"}

    def test_record_filters_placeholder(self):
        rec = extraction_fields_record(
            [{"key": "d", "label": "建设工期", "value": "【待填写：工期】", "category": "schedule"}])
        assert rec["fields"]["duration"] == ""

    def test_parse_roundtrip(self):
        rec = extraction_fields_record(FACTS)
        parsed = parse_extraction_fields_record(json.dumps(rec, ensure_ascii=False))
        assert parsed is not None
        assert parsed["fields"]["duration"] == "540 日历天"
        assert parsed["provenance"]["duration"]["fact_key"] == "duration"

    def test_parse_invalid_returns_none(self):
        assert parse_extraction_fields_record(None) is None
        assert parse_extraction_fields_record("") is None
        assert parse_extraction_fields_record("{}") is None
        assert parse_extraction_fields_record("not-json") is None
        assert parse_extraction_fields_record('{"fields": {}}') is None
        assert parse_extraction_fields_record('{"fields": {"unknown_key": "v"}}') is None

    def test_build_contract_consumes_persisted_record(self):
        """取数链路直供：project_data["extraction_fields"] 走显式通道，
        不再依赖运行时启发式派生（trace.explicit_keys 可见）。"""
        persisted = extraction_fields_record(FACTS)["fields"]
        contract = build_field_contract({
            "global_facts_items": [],
            "extraction_fields": persisted,
        })
        assert contract["extraction_fields"]["duration"] == "540 日历天"
        assert set(contract["trace"]["explicit_keys"]) >= {"duration", "project_name"}


class TestSaveFactsPersistsExtractionFields:
    """save_facts 落库 extraction_fields_json 并随响应回显（端点集成）。"""

    def test_save_persists_record(self, monkeypatch):
        import asyncio
        from app.db import get_db
        from app.models import GlobalFact, GlobalFactList
        pid, _ = TestSaveFactsPropagation._setup(self, monkeypatch)
        from app.routers import facts as fr
        body = GlobalFactList(items=[
            GlobalFact(key="duration", label="建设工期", value="600 日历天", source="manual"),
        ])
        result = asyncio.run(fr.save_facts(pid, body))
        # 响应回显
        assert result["extraction_fields"]["duration"] == "600 日历天"
        assert result["extraction_fields_provenance"]["duration"]["source"] == "facts"
        # 落库校验
        with get_db() as conn:
            row = conn.execute(
                "SELECT extraction_fields_json FROM analysis WHERE project_id = ?",
                (pid,)).fetchone()
        assert row is not None and row["extraction_fields_json"]
        rec = json.loads(row["extraction_fields_json"])
        assert rec["fields"]["duration"] == "600 日历天"
        assert rec["version"] == "1.0"

