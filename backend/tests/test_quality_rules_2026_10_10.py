# -*- coding: utf-8 -*-
"""内容质量规则验证 —— 《装饰装修专项施工方案》文档质量分析报告·修复 15~24（2026-10-10）。"""

from types import SimpleNamespace

from app.services import quality_rules as qr


def _sec(title: str, content: str = "", *, id: int | None = None, parent_id=None):
    """轻量章节桩（规则只读 title/content/id/parent_id）。"""
    return SimpleNamespace(
        id=id, title=title, content=content, parent_id=parent_id, level=1
    )


# ============================================================================
# 修复 16：方案类型模板校验
# ============================================================================


class TestSchemeTypeValidation:
    def test_infer_from_project_name(self):
        assert qr.infer_scheme_type("装饰装修专项施工方案") == "装饰装修"
        assert qr.infer_scheme_type("脚手架专项施工方案") == "脚手架"
        assert qr.infer_scheme_type("某某道路工程") is None

    def test_missing_required_section_flags_cmp01(self):
        sections = [
            _sec("装饰装修工程概况", "本工程为幼儿园整体维修……"),
            _sec("装饰装修施工工艺", "工艺内容……"),
            _sec("装饰装修安全保证措施", "安全内容……"),
            _sec("装饰装修验收要求", "验收内容……"),
            _sec("装饰装修应急处置", "应急内容……"),
        ]
        # 缺「编制依据」「施工进度」→ 2 条 CMP-01
        issues = qr.validate_scheme_sections(sections)
        cmp01 = [i for i in issues if i["rule_id"] == "CMP-01"]
        assert len(cmp01) == 2, issues
        assert all(i["severity"] == "high" for i in cmp01)

    def test_forbidden_section_flags_cmp02(self):
        """报告内容问题 11：装饰装修方案混入 PC 吊装章节。"""
        sections = [
            _sec("装饰装修工程概况", "……"),
            _sec("编制依据", "……"),
            _sec("施工进度计划", "……"),
            _sec("装饰装修施工工艺", "……"),
            _sec("安全保证措施", "……"),
            _sec("验收要求", "……"),
            _sec("应急处置措施", "……"),
            _sec("PC工程吊装专项施工方案", "……"),
        ]
        issues = qr.validate_scheme_sections(sections, project_name="装饰装修专项方案")
        cmp02 = [i for i in issues if i["rule_id"] == "CMP-02"]
        assert len(cmp02) == 1
        assert cmp02[0]["severity"] == "high"
        assert "PC工程吊装" in cmp02[0]["message"]

    def test_unknown_scheme_type_skips_checks(self):
        sections = [_sec("第一章 工程概况", "……"), _sec("第二章 施工", "……")]
        assert qr.validate_scheme_sections(sections) == []

    def test_complete_decor_scheme_passes(self):
        sections = [
            _sec("装饰装修工程概况", "……"),
            _sec("编制依据", "……"),
            _sec("施工进度计划", "……"),
            _sec("装饰装修施工工艺", "……"),
            _sec("安全保证措施", "……"),
            _sec("验收要求", "……"),
            _sec("应急处置措施", "……"),
        ]
        issues = qr.validate_scheme_sections(sections, project_name="装饰装修专项方案")
        assert issues == [], issues


# ============================================================================
# 修复 17：工期一致性
# ============================================================================


class TestScheduleConsistency:
    def test_report_case_stage_sum_mismatch(self):
        """报告内容问题 5：总工期 195，阶段 15+30+120+18=183。"""
        sections = [
            _sec("工程概况", "本工程总工期约195日历天，自2026.05.08至2026.11.18。"),
            _sec(
                "施工进度计划",
                "各阶段工期安排：准备阶段15天+拆除阶段30天+主体施工120天+收尾18天=183天。",
            ),
        ]
        issues = qr.validate_schedule_consistency(sections)
        assert len(issues) == 1, issues
        assert issues[0]["rule_id"] == "CON-01"
        assert issues[0]["severity"] == "high"
        assert "183" in issues[0]["message"] and "195" in issues[0]["message"]

    def test_matching_stage_sum_passes(self):
        sections = [
            _sec("工程概况", "总工期195日历天。"),
            _sec("施工进度计划", "各阶段工期：150+45=195天。"),
        ]
        assert qr.validate_schedule_consistency(sections) == []

    def test_no_total_duration_passes(self):
        sections = [_sec("施工进度计划", "阶段工期：10+20=30天。")]
        assert qr.validate_schedule_consistency(sections) == []

    def test_non_schedule_plus_expression_ignored(self):
        """没有「阶段/工期」上下文的加和表达式不判罚。"""
        sections = [
            _sec("工程概况", "总工期195日历天。"),
            _sec("材料配合比", "水泥1+砂2=3份比例说明。"),
        ]
        assert qr.validate_schedule_consistency(sections) == []


# ============================================================================
# 修复 19/18：规范引用与单位
# ============================================================================


class TestStandardReferences:
    def test_report_case_wrong_standard_name(self):
        """报告内容问题 7：GB/T23997 被引成《建筑外墙用氟碳漆》。"""
        sections = [
            _sec(
                "材料要求",
                "外墙涂装执行《建筑外墙用氟碳漆》（GB/T23997-2009）的规定。",
            )
        ]
        issues = qr.validate_standard_references(sections)
        std02 = [i for i in issues if i["rule_id"] == "STD-02"]
        assert len(std02) == 1, issues
        assert "室内装饰装修用水性木器涂料" in std02[0]["message"]

    def test_report_case_gb33372_name_mismatch(self):
        sections = [
            _sec("编制依据", "依据《地面材料中有害物质限量》（GB 33372-2020）执行。")
        ]
        issues = qr.validate_standard_references(sections)
        std02 = [i for i in issues if i["rule_id"] == "STD-02"]
        assert len(std02) == 1, issues
        assert "胶粘剂中有害物质限量" in std02[0]["message"]

    def test_correct_reference_passes(self):
        sections = [
            _sec(
                "编制依据",
                "《民用建筑工程室内环境污染控制标准》（GB50325-2020）；"
                "《建筑地面工程施工质量验收规范》（GB50209-2010）。",
            )
        ]
        assert qr.validate_standard_references(sections) == []

    def test_unknown_code_reported_once_as_low(self):
        sections = [
            _sec("编制依据", "依据《某未知标准》（GB99999-2020）执行。"),
            _sec("施工工艺", "同上《某未知标准》（GB99999-2020）要求。"),
        ]
        issues = qr.validate_standard_references(sections)
        std03 = [i for i in issues if i["rule_id"] == "STD-03"]
        assert len(std03) == 1, issues
        assert std03[0]["severity"] == "low"


class TestUnitValidation:
    def test_report_case_missing_unit(self):
        """报告内容问题 6：`甲醛（HCHO）≤ 0.07` 单位缺失。"""
        sections = [_sec("材料控制", "室内空气检测限值：甲醛（HCHO）≤ 0.07。")]
        issues = qr.validate_limit_units(sections)
        std01 = [i for i in issues if i["rule_id"] == "STD-01"]
        assert len(std01) == 1, issues
        assert "mg/m³" in std01[0]["message"]

    def test_with_unit_passes(self):
        sections = [
            _sec(
                "材料控制",
                "人造板甲醛释放量≤0.05mg/m³（E0级）；甲醛 HCHO mg/m³ ≤0.07。",
            )
        ]
        assert qr.validate_limit_units(sections) == []

    def test_report_case_ammonium_radon_confusion(self):
        """报告内容问题 6：「氨 222Rn」应为「氡 222Rn」。"""
        sections = [_sec("材料控制", "检测项目：氨 222Rn 浓度限值。")]
        issues = qr.validate_limit_units(sections)
        std04 = [i for i in issues if i["rule_id"] == "STD-04"]
        assert len(std04) == 1, issues
        assert std04[0]["severity"] == "high"


# ============================================================================
# 修复 20/21：空话套话与术语
# ============================================================================


class TestFillerAndTerminology:
    def test_report_case_filler_detected(self):
        """报告内容问题 8：`确保在有限工期内高质量完成各项施工任务`。"""
        sections = [
            _sec(
                "工程概况",
                "确保在有限工期内高质量完成各项施工任务。确保装饰工程施工受控。",
            )
        ]
        issues = qr.detect_filler_content(sections)
        qlts = [i for i in issues if i["rule_id"] == "QLT-01"]
        assert qlts, issues
        assert all(i["severity"] == "low" for i in qlts)
        # 单章节每种套话最多 2 条（防刷屏）
        assert len(qlts) <= 2

    def test_substantive_content_not_flagged(self):
        sections = [_sec("工艺", "抹灰层厚度控制在8~12mm，分两遍成活，养护7天。")]
        assert qr.detect_filler_content(sections) == []

    def test_terminology_contracting_party(self):
        """报告内容问题 9：发包人/甲方 → 建设单位。"""
        sections = [_sec("合同管理", "发包人提供图纸，甲方负责验收。")]
        issues = qr.detect_terminology_issues(sections)
        qlt02 = [i for i in issues if i["rule_id"] == "QLT-02"]
        assert any("建设单位" in i["message"] for i in qlt02), issues

    def test_terminology_compound_word_excluded(self):
        """「产业园区」的「园区」不得计入幼儿园项目的术语混用。"""
        sections = [_sec("总平面布置", "本工程毗邻产业园区与科技园区。")]
        issues = qr.detect_terminology_issues(
            sections, project_name="幼儿园整体维修工程"
        )
        qlt02 = [i for i in issues if i["rule_id"] == "QLT-02"]
        assert not any("幼儿园" in i["message"] for i in qlt02), issues

    def test_child_terms_gated_by_context(self):
        """非幼儿园项目不启用「儿童/小朋友」术语规则。"""
        sections = [_sec("安全", "防止儿童进入施工区。")]
        issues = qr.detect_terminology_issues(sections, project_name="办公楼改造")
        assert not any("儿童" in i["message"] for i in issues)


# ============================================================================
# 修复 22/12/24：章节重叠、空章节、编号去重
# ============================================================================


class TestOverlapEmptyNumbering:
    def test_identical_long_sections_overlap(self):
        body = "本工程采用穿插施工组织方式，主体与装饰流水作业。" * 20
        sections = [_sec("第五章 安全保证", body), _sec("第八章 应急处置", body)]
        issues = qr.detect_content_overlap(sections)
        dup = [i for i in issues if i["rule_id"] == "DUP-01"]
        assert len(dup) == 1, issues
        assert dup[0]["severity"] == "medium"

    def test_different_sections_pass(self):
        sections = [
            _sec("第五章 安全保证", "脚手架验收合格后方可使用。" * 20),
            _sec("第七章 验收要求", "室内环境检测须由第三方机构完成。" * 20),
        ]
        assert qr.detect_content_overlap(sections) == []

    def test_empty_section_flagged(self):
        sections = [_sec("3.5 信息台账与资料归档", ""), _sec("正常章节", "正文" * 40)]
        issues = qr.detect_empty_sections(sections)
        dlv = [i for i in issues if i["rule_id"] == "DLV-01"]
        assert len(dlv) == 1
        assert "信息台账" in dlv[0]["message"]

    def test_duplicate_sibling_numbering(self):
        """报告内容问题 3：同级两个「第二节」。"""
        sections = [
            _sec("第一节、项目概述与重难点分析", "……", parent_id=None),
            _sec("第二节、总体施工方案", "……", parent_id=None),
            _sec("第二节、专业分包配合协调管理方案", "……", parent_id=None),
        ]
        issues = qr.detect_duplicate_numbering(sections)
        cmp08 = [i for i in issues if i["rule_id"] == "CMP-08"]
        assert len(cmp08) == 1, issues
        assert "第二节" in cmp08[0]["message"]

    def test_unnumbered_titles_not_flagged(self):
        sections = [
            _sec("工程概况", "……", parent_id=None),
            _sec("工程概况及重难点", "……", parent_id=None),
        ]
        assert qr.detect_duplicate_numbering(sections) == []


# ============================================================================
# 编排与告警聚合
# ============================================================================


class TestOrchestrator:
    def test_run_quality_checks_sorted_by_severity(self):
        sections = [
            _sec("装饰装修工程概况", "……"),
            _sec("PC工程吊装专项方案", "……"),
            _sec("工艺", "确保按期完成任务。"),
        ]
        issues = qr.run_quality_checks(sections, project_name="装饰装修专项施工方案")
        assert issues, "至少应命中方案类型相关规则"
        ranks = [qr.rules.severity_rank(i["severity"]) for i in issues]
        assert ranks == sorted(ranks), f"应按严重度排序: {ranks}"

    def test_run_quality_checks_empty_input(self):
        assert qr.run_quality_checks([]) == []

    def test_summarize_excludes_dlv01(self):
        """DLV-01 已有等价预检告警，不得重复进 warnings。"""
        issues = [
            qr.make_issue("DLV-01", "high", "空章节", "章节「X」缺少正文"),
            qr.make_issue("CON-01", "high", "工期不一致", "和为183≠195"),
        ]
        summaries = qr.summarize_quality_issues(issues)
        assert len(summaries) == 1
        assert "CON-01" in summaries[0]
        assert "[内容质量]" in summaries[0]

    def test_summarize_groups_by_rule(self):
        issues = [
            qr.make_issue("QLT-01", "low", "空话套话", f"第{i}处确保……")
            for i in range(5)
        ]
        summaries = qr.summarize_quality_issues(issues)
        assert len(summaries) == 1
        assert "共 5 处" in summaries[0]


# ============================================================================
# check_document 集成
# ============================================================================


class TestCheckDocumentIntegration:
    def _new_project(self, name: str) -> int:
        from app.db import get_db, init_db

        init_db()
        with get_db() as conn:
            conn.execute(
                "INSERT INTO projects (name, industry) VALUES (?, ?)", (name, "通用")
            )
            return conn.execute(
                "SELECT id FROM projects ORDER BY id DESC LIMIT 1"
            ).fetchone()["id"]

    def _mk_section(self, pid: int, title: str, content: str, level: int = 2) -> None:
        from app.db import get_db

        with get_db() as conn:
            conn.execute(
                "INSERT INTO sections (project_id, parent_id, title, level, sort_order, "
                "section_type, scoring_point_id, content) VALUES (?,?,?,?,?,?,?,?)",
                (pid, None, title, level, 0, "normal", None, content),
            )

    def test_quality_issues_populated_and_warnings_aggregated(self):
        from app.services.export import check_document

        pid = self._new_project("QR-装饰装修专项施工方案")
        self._mk_section(
            pid,
            "编制依据",
            "依据《建筑外墙用氟碳漆》（GB/T23997-2009）执行；发包人提供图纸。",
        )
        self._mk_section(pid, "工艺标准", "室内空气限值：甲醛（HCHO）≤ 0.07。")

        r = check_document(pid)
        rule_ids = {i["rule_id"] for i in r.quality_issues}
        assert "STD-02" in rule_ids, r.quality_issues
        quality_warnings = [w for w in r.warnings if w.startswith("[内容质量]")]
        assert quality_warnings, r.warnings
        assert any("STD-02" in w for w in quality_warnings), r.warnings
        # 内容质量规则只出告警，不产生 errors，passed 不受影响
        assert all(not w.startswith("[内容质量]") for w in r.errors)
        assert r.passed is True, r.errors

    def test_clean_doc_has_no_quality_warnings(self):
        """无内容质量问题时无 [内容质量] 告警（向后兼容）。"""
        from app.services.export import check_document

        pid = self._new_project("QR-干净文档")
        self._mk_section(pid, "工艺标准", "抹灰层厚度8~12mm，养护7天后验收。")
        r = check_document(pid)
        assert isinstance(r.quality_issues, list)
        assert not any(w.startswith("[内容质量]") for w in r.warnings), r.warnings


# ============================================================================
# 修复 25：标题-正文错位 / 正文截断 / 项目类型禁止章节扩充（2026-10-26 报告）
# ============================================================================


class TestTitleContentMismatch:
    _GENERIC = (
        "本工程位于岳阳路园区科研南楼，建筑面积7974.70平方米，工期180日历天，"
        "质量目标为合格。编制依据包括招标文件、施工图纸及现行国家规范。"
    ) * 3

    def test_mismatched_section_flags_cmp09(self):
        sections = [
            _sec("消防管线支吊架固定", self._GENERIC, id=1),
            _sec("给排水管道耐压耐腐蚀指标", self._GENERIC, id=2),
        ]
        issues = qr.detect_title_content_mismatch(sections)
        assert {i["section_id"] for i in issues} == {1, 2}, issues
        assert all(i["rule_id"] == "CMP-09" for i in issues)
        assert all(i["severity"] == "high" for i in issues)

    def test_matched_section_not_flagged(self):
        sections = [
            _sec(
                "劳动力考核评价",
                "项目部建立劳动力考核评价机制，每月对班组进行考核评价，"
                "考核结果与工资挂钩。具体做法包括考勤记录、工序质量评分、"
                "安全行为评分，考核评价结果纳入月度评比。" * 2,
                id=3,
            ),
        ]
        assert qr.detect_title_content_mismatch(sections) == []

    def test_short_title_or_content_skipped(self):
        sections = [_sec("要点", self._GENERIC), _sec("消防管线支吊架固定", "内容过短")]
        assert qr.detect_title_content_mismatch(sections) == []


class TestTruncatedSections:
    def test_trailing_comma_flags_dlv02(self):
        content = (
            "本工程设置废料暂存点位于场地北侧，采用封闭式围挡并设置喷淋降尘设施，"
            "清运路线沿北侧临时通道经东门出场，避开办公区与教学区。"
            "清运车辆加盖篷布，出场前冲洗轮胎，"
        )
        issues = qr.detect_truncated_sections([_sec("废料暂存点与清运路线", content)])
        assert [i["rule_id"] for i in issues] == ["DLV-02"]

    def test_complete_sentence_not_flagged(self):
        content = (
            "本工程设置废料暂存点位于场地北侧，采用封闭式围挡并设置喷淋降尘设施，"
            "清运路线沿北侧临时通道经东门出场，避开办公区与教学区。"
        )
        assert qr.detect_truncated_sections([_sec("废料暂存点", content)]) == []


class TestDecorationForbiddenSections:
    def test_structural_sections_flagged_for_decoration(self):
        sections = [
            _sec("基坑排水系统设置"),
            _sec("脚手架连墙件加固"),
            _sec("塔吊防风锚固措施"),
            _sec("地下室施工工艺"),
            _sec("主体结构质量控制"),
            _sec("外墙技术交底"),
            _sec("混凝土养护保湿措施"),
        ]
        issues = qr.validate_scheme_sections(
            sections, project_name="岳阳路园区科研南楼室内装修工程"
        )
        cmp02 = [i for i in issues if i["rule_id"] == "CMP-02"]
        flagged = {i["section_title"] for i in cmp02}
        assert flagged == {s.title for s in sections}, cmp02


class TestDuplicateTitles:
    def test_same_level_duplicate_title_flagged(self):
        sections = [
            _sec("施工工艺流程与要点", "", id=1),
            _sec("1、施工工艺流程与要点", "", id=2),
            _sec("施工工艺流程与要点", "", id=3),
        ]
        sections[0].level = sections[1].level = sections[2].level = 3
        issues = qr.detect_duplicate_titles(sections)
        assert len(issues) == 1, issues
        assert issues[0]["rule_id"] == "CMP-10"
        assert issues[0]["severity"] == "medium"

    def test_same_title_different_level_not_flagged(self):
        a = _sec("工程概况", "", id=1)
        b = _sec("工程概况", "", id=2)
        a.level, b.level = 2, 3
        assert qr.detect_duplicate_titles([a, b]) == []


# ============================================================================
# 修复 26：图表一致性 / 编号残留 / 必需交付物（2026-10-26 报告 F/G/H）
# ============================================================================


class TestFigureWithoutPayload:
    def test_caption_without_chart_flags_cht01(self):
        """报告 F-5/E-6：正文只有「图X-Y」图题，没有图对象。"""
        content = (
            "图1-1 装饰装修独立区段\n"
            "本节按施工区段组织流水作业，配置独立班组与独立材料堆放区。\n\n"
        ) * 3
        issues = qr.detect_figure_without_payload([_sec("装饰装修独立区段", content, id=7)])
        assert [i["rule_id"] for i in issues] == ["CHT-01"], issues
        assert issues[0]["severity"] == "high"

    def test_caption_with_mermaid_fence_not_flagged(self):
        content = "图1-1 施工流程\n\n```mermaid\nflowchart LR\n A-->B\n```\n"
        assert qr.detect_figure_without_payload([_sec("施工流程", content)]) == []

    def test_caption_with_payload_not_flagged(self):
        s = _sec("区段划分", "图1-1 区段划分\n说明文字。", id=1)
        s.layout_json = '{"title": "区段划分", "nodes": []}'
        assert qr.detect_figure_without_payload([s]) == []


class TestDuplicateFigureTitles:
    def _mk(self, sid: int, title: str, fig_title: str, parent_id: int = 1):
        s = _sec(title, "", id=sid, parent_id=parent_id)
        s.layout_json = '{"title": "%s", "nodes": []}' % fig_title
        return s

    def test_same_chapter_duplicate_caption_flags_cht02(self):
        """报告 F-2/B-4：图7-2/7-3/7-4 共用同一图题。"""
        sections = [
            _sec("第七章", "", id=1),
            self._mk(2, "环保材料 A", "环保与速干材料交叉施工协调"),
            self._mk(3, "环保材料 B", "图7-2 环保与速干材料交叉施工协调"),
            self._mk(4, "环保材料 C", "环保与速干材料交叉施工协调"),
        ]
        issues = qr.detect_duplicate_figure_titles(sections)
        assert len(issues) == 1, issues
        assert issues[0]["rule_id"] == "CHT-02"
        assert issues[0]["severity"] == "medium"

    def test_different_chapter_same_title_not_flagged(self):
        """跨章同名图是合理的（每章都可能有一张进度横道图）。"""
        sections = [
            _sec("第一章", "", id=1),
            _sec("第七章", "", id=2),
            self._mk(3, "进度 A", "施工进度横道图", parent_id=1),
            self._mk(4, "进度 B", "施工进度横道图", parent_id=2),
        ]
        assert qr.detect_duplicate_figure_titles(sections) == []


class TestContentNumberingStyle:
    def test_dotted_numbering_flags_cmp11(self):
        """报告 G-1/G-4：正文残留「1.2.1.1 围挡设置标准」。"""
        content = "1.2.1.1 围挡设置标准\n围挡采用2.2m高彩钢板，底部砌筑300mm高砖基础。"
        issues = qr.detect_content_numbering_style([_sec("施工隔离围挡设置", content)])
        assert [i["rule_id"] for i in issues] == ["CMP-11"], issues
        assert issues[0]["severity"] == "medium"

    def test_single_level_number_not_flagged(self):
        content = "5.1米的净高满足要求，厚度5~7mm，验收合格率100%。"
        assert qr.detect_content_numbering_style([_sec("净高控制", content)]) == []


class TestRequiredDeliverables:
    def _decoration_sections(self, content: str = ""):
        return [
            _sec("装饰装修工程概况", "本项目为既有科研楼室内装修，工期180日历天。"),
            _sec("编制依据", "依据现行国家标准与招标文件。"),
            _sec("施工进度计划", "总工期180日历天，按区段流水组织。"),
            _sec("装饰装修施工工艺", content),
            _sec("安全保证措施", "建立安全生产责任制。"),
            _sec("验收要求", "按 GB50210 验收。"),
            _sec("应急处置", "建立应急组织。"),
        ]

    def test_missing_tables_flags_dlv03(self):
        """报告 H-9：材料进场检验/隐蔽验收/消防联动调试记录表缺失。"""
        issues = qr.detect_missing_required_tables(
            self._decoration_sections(), "装饰装修专项施工方案"
        )
        assert len([i for i in issues if i["rule_id"] == "DLV-03"]) == 3, issues

    def test_present_tables_not_flagged(self):
        content = "材料进场检验记录表、隐蔽工程验收记录表、消防联动调试记录表均已建立。"
        issues = qr.detect_missing_required_tables(
            self._decoration_sections(content), "装饰装修专项施工方案"
        )
        assert issues == []

    def test_missing_key_charts_flags_dlv04(self):
        """报告 H-10：总平面布置图/进度横道图/劳动力曲线图缺失。"""
        issues = qr.detect_missing_key_charts(
            self._decoration_sections(), "装饰装修专项施工方案"
        )
        assert len([i for i in issues if i["rule_id"] == "DLV-04"]) == 3, issues
        assert all(i["severity"] == "high" for i in issues)

    def test_text_only_mention_still_flags(self):
        """报告 F-4：只有「施工总平面布置图」文字描述、无图，不算覆盖。"""
        content = "施工总平面布置图如下：北侧为物料主入口，南侧为加工区。"
        issues = qr.detect_missing_key_charts(
            self._decoration_sections(content), "装饰装修专项施工方案"
        )
        names = " ".join(i["message"] for i in issues)
        assert "施工总平面布置图" in names, issues

    def test_chart_payloads_cover(self):
        sections = self._decoration_sections()
        sections[3].layout_json = '{"title": "总平面布置", "nodes": []}'
        sections[3].gantt_json = '{"title": "进度横道图", "tasks": []}'
        sections[3].labor_json = '{"title": "劳动力曲线", "points": []}'
        assert qr.detect_missing_key_charts(sections, "装饰装修专项施工方案") == []


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))



