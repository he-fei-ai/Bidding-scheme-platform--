"""【一致性校核模块】—— 2026-10-10 深度审计修复回归测试。

原则：每条用例对应一个**实测确认**（探针 `probe_audit_2026_10_10.py` 复现）的缺陷；
只断言可观察行为（抽取结果 / 闸门判定 / 落库正文 / 冲突状态），不依赖真实 AI 与网络。

覆盖缺陷：

  | F-1 | `fact_has_keyword_nearby` 的非对称窗口（12/6）是**死代码** ——
        参数默认 `window=AUTOFIX_KEYWORD_WINDOW`(40, 真值) 遮蔽了
        `AUTOFIX_KEYWORD_WINDOW_BEFORE/AFTER` 与对应配置项，
        「总工期 30 天，钢筋进场 30 批」的第二个 30 仍被当成工期取值   |
  | F-2 | 裸「人员数量」事实名**无标签词** → 人数类自动修复零闸门，
        「钢筋直径 20 mm」会被当「共 20 人」改写                        |
  | F-3 | 工期正则只认 `数字+天` → 「450 日历天 / 450 个日历天 / 工期为450日历天」
        全部漏抽（关键事实整类静默缺席）                                |
  | F-4 | 资质等级正则要求以甲/乙/丙开头 → 「资质等级：一级」「企业资质：施工
        总承包一级」全部漏抽                                            |
  | F-5 | `extract_facts_from_text(None)` 抛 TypeError → 整轮审计降级 failed |
  | F-1 | `fact_has_keyword_nearby` 的非对称窗口（12/6）是**死代码** ——
        参数默认 `window=AUTOFIX_KEYWORD_WINDOW`(40, 真值) 遮蔽了
        `AUTOFIX_KEYWORD_WINDOW_BEFORE/AFTER` 与对应配置项，
        「总工期 30 天，钢筋进场 30 批」的第二个 30 仍被当成工期取值   |
  | F-2 | 裸「人员数量」事实名**无标签词** → 人数类自动修复零闸门，
        「钢筋直径 20 mm」会被当「共 20 人」改写                        |
  | F-3 | 工期正则只认 `数字+天` → 「450 日历天 / 450 个日历天 / 工期为450日历天」
        全部漏抽；`(?:为|是|约)?` 只吃一个字，「项目周期约为 180 日历天」也漏 |
  | F-4 | 资质等级正则要求以甲/乙/丙开头 → 「资质等级：一级」漏抽；
        资格类型词表缺「承包」，「企业资质等级：专业承包二级」也漏抽      |
  | F-5 | `extract_facts_from_text(None)` 抛 TypeError → 整轮审计降级 failed |
  | F-6 | `assess_severity` 的 `low` 档口径为「恰好 2 种表述 + 单章节」，
        本文件用用例把该契约钉死，防止回退为 `<= 2`（单值误判 low）      |
  | F-7 | 裸「人员数量」标签词「人员」是「技术人员/管理人员」等的**子串** →
        人员数量 20→30 时「技术人员 20 人」被一起串改成「技术人员 30 人」 |
  | F-8 | 就近校验把**正确拦截**计入 skipped → `success=False` 且冲突永不关闭，
        用户反复点「采纳修复」反复失败，而正文其实已改对                 |

F-7 / F-8 是在真实审计链路的端到端验证中暴露的（见 `_e2e_verify.py` 场景）。

运行：`cd backend && python -m pytest tests/test_consistency_fixes_2026_10_10.py -q`
"""
from __future__ import annotations

import asyncio
import json
import re

import pytest

from app.db import get_db
from app.services import consistency_repair as repair
from app.services import consistency_rules as rules
from app.services import text_patch
from app.services.consistency_auditor import ConsistencyAuditor


@pytest.fixture(scope="module")
def auditor() -> ConsistencyAuditor:
    return ConsistencyAuditor()


def _extract(auditor: ConsistencyAuditor, text: str) -> list[tuple[str, str]]:
    return [(f.name, f.value) for f in auditor.extract_facts_from_text(text)]


# ============================================================================
# F-1 非对称窗口必须真正生效
# ============================================================================


class TestNonSymmetricWindowIsActive:
    """F-1：前向 12 / 后向 6 的口径此前从未生效（默认参数遮蔽）。"""

    def test_default_windows_are_12_and_6(self):
        assert rules.autofix_keyword_window_before() == 12
        assert rules.autofix_keyword_window_after() == 6

    def test_second_unrelated_number_is_blocked(self):
        """「总工期 30 天，钢筋进场 30 批」——第二个 30 距「工期」14 字，
        超过前向窗口 12，必须被拦；修复前（窗口实际为 40）会放行。"""
        text = "本工程总工期 30 天，其中钢筋进场 30 批。"
        positions = text_patch.find_value_occurrences(text, "30")
        assert len(positions) == 2, positions
        first, second = positions
        assert rules.fact_has_keyword_nearby(text, first, first + 2, "工期") is True
        assert (
            rules.fact_has_keyword_nearby(text, second, second + 2, "工期") is False
        ), "第二个「30」距离标签词 14 字，已超出前向窗口 12，不得放行"

    def test_explicit_window_keeps_symmetric_override_contract(self):
        """显式传 window 仍是「前后同一半径」的对称覆盖（既有调用契约）。"""
        text = "本工程总工期 30 天，其中钢筋进场 30 批。"
        positions = text_patch.find_value_occurrences(text, "30")
        second = positions[1]
        # 40 半径下「工期」仍在窗口内 → 放行（这正是旧的（错误）行为）
        assert (
            rules.fact_has_keyword_nearby(text, second, second + 2, "工期", window=40)
            is True
        )

    def test_keyword_beyond_before_window_is_rejected(self):
        """标签词与数值隔 20+ 字时不放行（前向窗口上界）。"""
        text = "本工程的建设工期为 30 天，其余内容较长较长较长较长较长。钢筋进场 30 批。"
        positions = text_patch.find_value_occurrences(text, "30")
        assert len(positions) == 2
        second = positions[1]
        assert rules.fact_has_keyword_nearby(text, second, second + 2, "工期") is False

    def test_normal_duration_case_still_passes(self):
        """回归保护：正常用例「承诺工期 45 天完成」不得被新口径误拦。"""
        text = "承诺工期 45 天完成。"
        pos = text_patch.find_value_occurrences(text, "45")[0]
        assert rules.fact_has_keyword_nearby(text, pos, pos + 2, "工期") is True


# ============================================================================
# F-2 人员数量类事实的闸门
# ============================================================================


class TestPersonnelFactGate:
    """F-2：人数类自动修复此前完全无闸门，无关数字会被改写。"""

    def test_bare_personnel_fact_has_keywords(self):
        """裸「人员数量」必须有标签词（修复前返回空集 → 闸门对其失效）。"""
        assert len(rules.personnel_fact_keywords("人员数量")) > 0

    def test_role_variant_has_keywords(self):
        assert "项目经理" in rules.personnel_fact_keywords("项目经理数量")
        assert "人" in rules.personnel_fact_keywords("技术人员数量")

    def test_personnel_fact_name_detection(self):
        for name in ("人员数量", "项目经理数量", "技术人员数量", "后勤人员数量"):
            assert rules.is_personnel_fact_name(name) is True, name
        for name in ("设备数量", "工期", "金额", "术语", ""):
            assert rules.is_personnel_fact_name(name) is False, name

    @pytest.mark.parametrize(
        "text,needle,expected",
        [
            ("本项目共 20 人参与施工。", "20", True),
            ("后勤人员：30 人。", "30", True),
            ("共 20 名施工人员到场。", "20", True),
            ("钢筋直径 20 mm。", "20", False),
            ("构件编号 20 号。", "20", False),
            ("本项目划分为 1 个标段。", "1", False),
        ],
    )
    def test_structural_check_accepts_only_headcount(self, text, needle, expected):
        pos = text_patch.find_value_occurrences(text, needle)[0]
        assert (
            rules.fact_has_keyword_nearby(text, pos, pos + len(needle), "人员数量")
            is expected
        )

    def test_role_variant_uses_structural_check(self):
        """岗位变体事实名同样走结构校验：合法放行，无关数字被拦。"""
        text = "项目经理 1 人、钢筋 1 mm。"
        positions = text_patch.find_value_occurrences(text, "1")
        got = [
            rules.fact_has_keyword_nearby(text, p, p + 1, "项目经理数量") for p in positions
        ]
        assert got == [True, False], got

    def test_non_personnel_fact_not_affected(self):
        """「设备数量」这类同名后缀的非人员事实不得被套人数结构校验。"""
        assert rules.is_personnel_fact_name("设备数量") is False
        text = "设备数量 5 台。"
        pos = text_patch.find_value_occurrences(text, "5")[0]
        assert rules.fact_has_keyword_nearby(text, pos, pos + 1, "设备数量") is True


class TestPersonnelAutofixEndToEnd:
    """F-2 端到端：真实章节里「钢筋直径 20 mm」不得被当人数改写。"""

    def test_unrelated_number_in_same_section_not_rewritten(self):
        pid, cid = 991001, "cf_991001"
        section_id = 9910011
        content = "本项目共 20 人参与施工，其中钢筋直径 20 mm，构件编号 20 号。"
        with get_db() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO projects (id, name) VALUES (?, ?)",
                (pid, "P991001"),
            )
            conn.execute(
                "INSERT OR REPLACE INTO sections "
                "(id, project_id, title, content, status, sort_order) "
                "VALUES (?, ?, ?, ?, 'completed', 1)",
                (section_id, pid, "资源配置", content),
            )
            conn.execute(
                "INSERT OR REPLACE INTO consistency_conflicts "
                "(project_id, conflict_id, fact_name, values_json, sources_json, "
                "severity, description, created_at, resolved) "
                "VALUES (?, ?, ?, ?, ?, 'medium', '', '2026-10-10', 0)",
                (pid, cid, "人员数量", json.dumps(["20", "30"], ensure_ascii=False),
                 json.dumps([f"section_{section_id} (资源配置)"], ensure_ascii=False)),
            )

        report = ConsistencyAuditor().apply_autofix(cid, pid, "30")

        with get_db() as conn:
            new_content = conn.execute(
                "SELECT content FROM sections WHERE id = ?", (section_id,)
            ).fetchone()["content"]
            resolved = conn.execute(
                "SELECT resolved FROM consistency_conflicts WHERE conflict_id = ?",
                (cid,),
            ).fetchone()["resolved"]

        # 只有「共 20 人」那一处应被改写
        assert "共 30 人参与施工" in new_content, new_content
        assert "钢筋直径 20 mm" in new_content, f"钢筋直径被误改: {new_content}"
        assert "构件编号 20 号" in new_content, f"构件编号被误改: {new_content}"
        assert report["total_replacements"] == 1, report
        assert report["success"] is True
        assert resolved == 1


# ============================================================================
# F-3 工期抽取：日历天 / 个日历天 / 为是约
# ============================================================================


class TestDurationExtraction:
    @pytest.mark.parametrize(
        "text,value",
        [
            ("建设工期：450 日历天", "450"),
            ("总工期 450 个日历天", "450"),
            ("合同工期为450日历天", "450"),
            ("总工期450个自然日", "450"),
            ("工期 90 工作日", "90"),
            ("总工期：450天", "450"),
            ("工期 450 天", "450"),
            ("项目周期约为 180 日历天", "180"),
        ],
    )
    def test_calendar_day_forms_are_extracted(self, auditor, text, value):
        assert ("工期", value) in _extract(auditor, text), text

    def test_unrelated_x_days_is_not_extracted(self, auditor):
        """无关叙述「养护 7 天」不得被误判为工期。"""
        assert not [
            (n, v) for n, v in _extract(auditor, "混凝土养护 7 天。") if n == "工期"
        ]

    def test_regression_plain_days_still_work(self, auditor):
        assert [v for n, v in _extract(auditor, "工期 30 天，工期 45 天") if n == "工期"] == [
            "30",
            "45",
        ]


# ============================================================================
# F-4 资质等级抽取
# ============================================================================


class TestQualificationExtraction:
    @pytest.mark.parametrize(
        "text,value",
        [
            ("资质等级：一级", "一级"),
            ("资质等级：一级建筑业企业", "一级"),
            ("企业资质：施工总承包一级", "一级"),
            ("资质：甲级", "甲级"),
            ("资质等级：乙级", "乙级"),
            ("资质等级：施工总承包三级", "三级"),
            ("企业资质等级：专业承包二级", "二级"),
        ],
    )
    def test_grade_forms_are_extracted(self, auditor, text, value):
        assert ("资质等级", value) in _extract(auditor, text), text

    def test_regression_yiji_still_works(self, auditor):
        assert ("资质等级", "甲级") in _extract(auditor, "资质：甲级")


# ============================================================================
# F-5 非字符串输入防护
# ============================================================================


class TestExtractionRobustness:
    def test_none_input_does_not_raise(self, auditor):
        assert auditor.extract_facts_from_text(None) == []

    def test_non_string_input_does_not_raise(self, auditor):
        assert auditor.extract_facts_from_text(0) == []

    def test_empty_input(self, auditor):
        assert auditor.extract_facts_from_text("") == []

    def test_very_long_input(self, auditor):
        assert isinstance(auditor.extract_facts_from_text("工期 30 天。" * 20000), list)


# ============================================================================
# F-6 low 档边界
# ============================================================================


class TestLowSeverityBoundary:
    def test_single_value_is_not_low(self):
        """文档口径是「恰好 2 种表述」；单值不是冲突，不得判 low。"""
        assert rules.assess_severity("质量标准", ["合格"], section_count=1) != (
            rules.SEVERITY_LOW
        )

    @pytest.mark.parametrize("sections", [2, 3, 10])
    def test_two_values_multi_section_is_medium(self, sections):
        assert rules.assess_severity(
            "质量标准", ["合格", "优良"], section_count=sections
        ) == rules.SEVERITY_MEDIUM

    def test_two_values_single_section_is_low(self):
        assert rules.assess_severity(
            "质量标准", ["合格", "优良"], section_count=1
        ) == rules.SEVERITY_LOW

    def test_three_values_is_high(self):
        assert rules.assess_severity(
            "质量标准", ["合格", "优良", "优秀"], section_count=1
        ) == rules.SEVERITY_HIGH

    def test_critical_fact_is_high_regardless(self):
        assert rules.assess_severity("工期", ["30", "45"], section_count=1) == (
            rules.SEVERITY_HIGH
        )


# ============================================================================
# 回归保护：既有口径不得被本轮修改破坏
# ============================================================================


class TestExistingContractsKept:
    def test_amount_unit_normalization(self):
        assert rules.normalize_amount_to_yuan("1200万元") == "12000000"
        assert rules.normalize_amount_to_yuan("1,200万元") == "12000000"
        assert rules.normalize_amount_to_yuan("1.5亿元") == "150000000"
        assert rules.normalize_amount_to_yuan("待定") is None
        assert rules.normalize_amount_to_yuan("100 万美元") is None

    def test_amount_conflict_detection(self, auditor):
        assert len(auditor._distinct_values(["1200万元", "12000000元"], fact_name="金额")) == 1
        assert len(auditor._distinct_values(["1200万元", "1500万元"], fact_name="金额")) == 2

    def test_normalization_is_conservative(self, auditor):
        """归一化刻意只消除排版差异、不做语义推断（既有契约）。"""
        assert len(
            auditor._distinct_values(["450日历天", "450 日历天"], fact_name="工期")
        ) == 1

    def test_role_bucketing_still_splits_personnel(self, auditor):
        """BUG 回归：不同岗位人数不合并成一条「人员数量」冲突。"""
        facts = _extract(auditor, "配备项目经理1人、技术人员20人、管理人员5人。")
        names = {n for n, _ in facts}
        assert "项目经理数量" in names and "技术人员数量" in names
        assert len(names) == 3, names

    def test_safety_standard_requires_explicit_label(self, auditor):
        """无显式标签的「安全」叙述不得误抽为安全标准。"""
        assert not [
            (n, v)
            for n, v in _extract(
                auditor, "我们高度重视安全生产，制定了详细的安全管理制度并严格执行。"
            )
            if n == "安全标准"
        ]

    def test_severity_normalization_and_sql(self):
        assert rules.normalize_severity("") == rules.SEVERITY_MEDIUM
        assert rules.normalize_severity(None) == rules.SEVERITY_MEDIUM
        assert rules.normalize_severity("critical") == rules.SEVERITY_MEDIUM
        sql = rules.severity_sort_sql()
        assert "ELSE" in sql
        assert "WHEN 'low' THEN 3" in sql

    def test_digit_boundary_protection(self):
        text = "总工期 30 天，300 天为最长，3.30 版本，第30号。"
        positions = text_patch.find_value_occurrences(text, "30")
        assert all(text[p : p + 2] == "30" for p in positions)
        result = text_patch.replace_values(text, {"30": "45"})
        assert "300" in result["content"] and "3.30" in result["content"]

    def test_extract_source_section_ids(self):
        assert rules.extract_source_section_ids(
            ["section_12 (施工组织方案)", "summary (摘要)", "section_3 (进度)"]
        ) == [12, 3]


# ============================================================================
# F-7 跨岗位串改防护（端到端审计时发现的真实缺陷）
# ============================================================================


class TestCrossRolePersonnelGuard:
    """F-7：裸「人员数量」的标签词「人员」是「技术人员/管理人员」等的子串，
    导致把人员数量 20 改成 30 时「技术人员 20 人」被一起改写成
    「技术人员 30 人」——不同岗位编制被静默串改。"""

    @pytest.mark.parametrize(
        "text,expected",
        [
            ("本项目人员 20 人。", True),
            ("本项目组织人员 20 人。", True),
            ("后勤人员：30 人。", True),
            ("技术人员 20 人", False),
            ("技术人员约 20 人", False),
            ("管理人员 5 人", False),
            ("施工人员 30 人", False),
            ("技术负责人 2 人", False),
            ("项目经理 1 人", False),
            ("钢筋直径 20 mm。", False),
        ],
    )
    def test_bare_personnel_rejects_role_values(self, text, expected):
        m = re.search(r"\d+", text)
        pos = text_patch.find_value_occurrences(text, m.group())[0]
        assert (
            rules.fact_has_keyword_nearby(text, pos, pos + len(m.group()), rules.FACT_PERSONNEL)
            is expected
        ), text

    @pytest.mark.parametrize(
        "fact_name,text,expected",
        [
            # 岗位变体只放行自己的取值
            ("技术人员数量", "技术人员 20 人", True),
            ("技术人员数量", "钢筋直径 20 mm", False),
            ("施工人员数量", "本项目施工人员 30 人", True),
            ("管理人员数量", "管理人员 5 人", True),
            # 岗位变体不得反向影响别的岗位
            ("技术人员数量", "管理人员 5 人", False),
        ],
    )
    def test_role_variant_unaffected_by_guard(self, fact_name, text, expected):
        m = re.search(r"\d+", text)
        pos = text_patch.find_value_occurrences(text, m.group())[0]
        assert (
            rules.fact_has_keyword_nearby(text, pos, pos + len(m.group()), fact_name)
            is expected
        ), (fact_name, text)

    def test_guard_lookback_is_bounded(self):
        """岗位词回看半径有上界：远处另一句岗位描述不得抢占当前取值归属。"""
        text = "前期由技术人员 20 人承担勘察。正式施工阶段本项目人员 30 人。"
        pos = text_patch.find_value_occurrences(text, "30")[0]
        assert (
            rules.fact_has_keyword_nearby(text, pos, pos + 2, rules.FACT_PERSONNEL)
            is True
        ), "「本项目人员 30 人」应被放行，不受远处「技术人员」影响"


# ============================================================================
# F-8 就近校验的「正确拦截」不得被报成「修复失败」
# ============================================================================


class TestFilteredDoesNotBlockResolution:
    """F-8：整章取值全被就近校验拦下时（这些取值本就不属于该事实），
    旧实现把它计入 skipped → `success=False` 且冲突永不关闭。
    用户会反复点「采纳修复」反复失败，而正文其实已改对。"""

    def _setup(self, pid: int) -> tuple[int, str, int, int]:
        cid = "cf_992001"
        legit_id, other_id = 9920011, 9920012
        with get_db() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO projects (id, name) VALUES (?, ?)",
                (pid, "P992001"),
            )
            conn.execute(
                "INSERT OR REPLACE INTO sections "
                "(id, project_id, title, content, status, sort_order) "
                "VALUES (?, ?, ?, ?, 'completed', 1)",
                (legit_id, pid, "劳动力计划", "本项目组织人员 20 人。"),
            )
            conn.execute(
                "INSERT OR REPLACE INTO sections "
                "(id, project_id, title, content, status, sort_order) "
                "VALUES (?, ?, ?, ?, 'completed', 2)",
                (other_id, pid, "资源配置", "本项目人员 30 人。\n技术人员 20 人。\n"),
            )
            conn.execute(
                "INSERT OR REPLACE INTO consistency_conflicts "
                "(project_id, conflict_id, fact_name, values_json, sources_json, "
                "severity, description, created_at, resolved) "
                "VALUES (?, ?, ?, ?, ?, 'medium', '', '2026-10-10', 0)",
                (pid, cid, rules.FACT_PERSONNEL, json.dumps(["20", "30"], ensure_ascii=False),
                 json.dumps(
                     [f"section_{legit_id} (劳动力计划)", f"section_{other_id} (资源配置)"],
                     ensure_ascii=False,
                 )),
            )
        return pid, cid, legit_id, other_id

    def test_gate_rejection_is_not_a_failure(self):
        pid, cid, legit_id, other_id = self._setup(992001)
        report = ConsistencyAuditor().apply_autofix(cid, pid, "30")

        assert report["success"] is True, report["message"]
        assert report["total_replacements"] == 1, report
        # 正确拦截计入 filtered，而不是 skipped
        assert report["skipped"] == [], report
        assert [f["section_id"] for f in report["filtered"]] == [other_id], report
        assert report["message"], report

        with get_db() as conn:
            resolved = conn.execute(
                "SELECT resolved, resolved_value FROM consistency_conflicts "
                "WHERE conflict_id = ?",
                (cid,),
            ).fetchone()
            legit = conn.execute(
                "SELECT content FROM sections WHERE id = ?", (legit_id,)
            ).fetchone()["content"]
            other = conn.execute(
                "SELECT content FROM sections WHERE id = ?", (other_id,)
            ).fetchone()["content"]

        assert resolved["resolved"] == 1, "闸门正确拦截时冲突仍应关闭"
        assert resolved["resolved_value"] == "30"
        assert legit == "本项目组织人员 30 人。", legit
        # 资源配置章节一字未动（技术人员 20 人 / 钢筋类数字均未被串改）
        assert "技术人员 20 人" in other and "本项目人员 30 人" in other, other

    def test_real_error_still_blocks_resolution(self):
        """回归保护：真正无法替换（整章无待统一值之外的取值）仍算 skipped。"""
        pid, cid, legit_id, other_id = self._setup(992002)
        # 把来源章节里唯一含旧值的章节删掉 → 无章节可改
        with get_db() as conn:
            conn.execute("DELETE FROM sections WHERE id = ?", (legit_id,))
            conn.execute("DELETE FROM sections WHERE id = ?", (other_id,))

        report = ConsistencyAuditor().apply_autofix(cid, pid, "30")
        assert report["success"] is False, report
        assert report["total_replacements"] == 0, report
        assert "未在正文中定位到待统一的值" in report["message"], report

        with get_db() as conn:
            resolved = conn.execute(
                "SELECT resolved FROM consistency_conflicts WHERE conflict_id = ?",
                (cid,),
            ).fetchone()["resolved"]
        assert resolved == 0, "真正无法替换时冲突必须保持未解决"


# ============================================================================
# F-9 收敛判据的扫描宇宙必须覆盖摘要与已确认事实基准
# ============================================================================


class TestRefreshScopeCoversSummaryAndBaseline:
    """F-9：`refresh_conflict_states` 只扫章节正文 → 旧取值只存在于
    摘要/事实基准的冲突被误标「已解决」。

    判据是「全文还能找到 ≥2 个取值 → 仍算冲突」，而审计器的事实来源宇宙是
    基准 + 摘要 + 章节正文（`ConsistencyAuditor._collect_fact_instances`）。
    只扫 sections 时，正文一旦统一就只剩 1 个取值，冲突被静默洗白 ——
    与 2026-10-09 修的「局部修复洗白其它小节」同源，只是发生在摘要/基准维度。
    """

    def _seed(
        self,
        pid: int,
        *,
        values: list[str],
        notes: str = "",
        bidder_name: str = "",
        global_facts: list[dict] | None = None,
        sections: list[str] | None = None,
        fact_name: str = "工期",
        severity: str = "high",
    ) -> str:
        with get_db() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO projects (id, name, notes, bidder_name, industry) "
                "VALUES (?, ?, ?, ?, ?)",
                (pid, f"P{pid}", notes, bidder_name, "通用"),
            )
            for i, content in enumerate(sections or []):
                conn.execute(
                    "INSERT OR REPLACE INTO sections "
                    "(id, project_id, title, content, status, sort_order) "
                    "VALUES (?, ?, ?, ?, 'completed', ?)",
                    (pid * 10 + i, pid, f"S{i}", content, i),
                )
            conn.execute(
                "INSERT OR REPLACE INTO analysis (project_id, global_facts_json) VALUES (?, ?)",
                (pid, json.dumps({"items": global_facts or []}, ensure_ascii=False)),
            )
            cid = f"cf_{pid}"
            conn.execute(
                "INSERT OR REPLACE INTO consistency_conflicts "
                "(project_id, conflict_id, fact_name, values_json, sources_json, "
                "severity, description, created_at, resolved) "
                "VALUES (?, ?, ?, ?, ?, ?, '', '2026-10-11', 0)",
                (pid, cid, fact_name, json.dumps(values, ensure_ascii=False), "[]", severity),
            )
        return cid

    def _refresh(self, pid: int, *, with_extra: bool) -> dict:
        with get_db() as conn:
            scope = repair._load_all_project_content(conn, pid)
            return repair.refresh_conflict_states(
                conn,
                pid,
                scope,
                extra_texts=(
                    repair._load_project_summary_and_baseline(conn, pid)
                    if with_extra
                    else None
                ),
            )

    def test_value_only_in_summary_keeps_conflict_open(self):
        """旧值只在摘要里：正文统一后仍不得标记已解决。"""
        pid = 993001
        self._seed(
            pid,
            values=["45", "450"],
            notes="建设工期：450 天。",
            sections=["本项目建设工期：45 天。"],
        )
        states = self._refresh(pid, with_extra=True)
        assert states["resolved"] == 0, states
        assert states["still_open"] == 1, states

        # 回归对照：不传 extra_texts 会复现旧缺陷（误标已解决）
        assert self._refresh(pid, with_extra=False)["resolved"] == 1

    def test_value_only_in_baseline_keeps_conflict_open(self):
        """旧值只在已确认事实基准里：同样不得标记已解决。"""
        pid = 993002
        self._seed(
            pid,
            values=["45", "450"],
            global_facts=[
                {"category": "schedule", "label": "建设工期", "value": "建设工期45天"},
            ],
            sections=["本项目建设工期：450 天。"],
        )
        states = self._refresh(pid, with_extra=True)
        assert states["resolved"] == 0, states
        assert states["still_open"] == 1, states

    def test_all_sources_unified_resolves(self):
        """摘要 + 基准 + 正文三方一致时才允许关闭，且写回唯一取值。"""
        pid = 993003
        self._seed(
            pid,
            values=["45", "450"],
            notes="建设工期：45 天。",
            global_facts=[
                {"category": "schedule", "label": "建设工期", "value": "建设工期45天"},
            ],
            sections=["本项目建设工期：45 天。"],
        )
        states = self._refresh(pid, with_extra=True)
        assert states["resolved"] == 1, states
        assert states["still_open"] == 0, states

        with get_db() as conn:
            row = conn.execute(
                "SELECT resolved, resolved_value FROM consistency_conflicts WHERE project_id = ?",
                (pid,),
            ).fetchone()
        assert row["resolved"] == 1
        assert row["resolved_value"] == "45"

    def test_sections_only_scope_is_backward_compatible(self):
        """不传 extra_texts 时行为与修复前完全一致（只扫章节正文）。"""
        pid = 993004
        self._seed(pid, values=["30", "300"], sections=["工期 300 天", "工期 30 天"])
        states = self._refresh(pid, with_extra=True)
        assert states["still_open"] == 1, states


class TestLoadSummaryAndBaseline:
    """F-9 数据来源：摘要三段拼接、基准只收已映射类别。"""

    def test_summary_text_shape(self):
        pid = 993005
        with get_db() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO projects "
                "(id, name, notes, bidder_name, industry) VALUES (?, ?, ?, ?, ?)",
                (pid, "P993005", "建设工期：450 天。", "中建一局", "房建"),
            )
        with get_db() as conn:
            texts = repair._load_project_summary_and_baseline(conn, pid)

        assert "summary" in texts
        assert "450" in texts["summary"]
        assert "中建一局" in texts["summary"]
        assert "房建" in texts["summary"]

    def test_baseline_only_maps_mapped_categories(self):
        """未映射类别（如付款）不得进入扫描宇宙，否则会误压工期冲突。"""
        pid = 993006
        with get_db() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO projects (id, name) VALUES (?, ?)",
                (pid, "P993006"),
            )
            conn.execute(
                "INSERT OR REPLACE INTO analysis (project_id, global_facts_json) VALUES (?, ?)",
                (
                    pid,
                    json.dumps(
                        {
                            "items": [
                                {
                                    "category": "payment",
                                    "label": "预付款期限",
                                    "value": "预付款期限45天",
                                },
                                {
                                    "category": "schedule",
                                    "label": "建设工期",
                                    "value": "建设工期45天",
                                },
                            ]
                        },
                        ensure_ascii=False,
                    ),
                ),
            )
        with get_db() as conn:
            texts = repair._load_project_summary_and_baseline(conn, pid)

        assert "global_facts" in texts
        # payment 被过滤，只剩 schedule 一行（且只出现一次 45）
        assert texts["global_facts"].count("45") == 1
        assert "预付款" not in texts["global_facts"]

    def test_placeholder_and_unmapped_values_are_dropped(self):
        """占位值与未映射类别一律不进扫描宇宙。"""
        pid = 993007
        with get_db() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO projects (id, name) VALUES (?, ?)",
                (pid, "P993007"),
            )
            conn.execute(
                "INSERT OR REPLACE INTO analysis (project_id, global_facts_json) VALUES (?, ?)",
                (
                    pid,
                    json.dumps(
                        {
                            "items": [
                                {"category": "schedule", "label": "建设工期", "value": "【待填写】建设工期"},
                                {"category": "payment", "label": "预付款", "value": "预付款45天"},
                            ]
                        },
                        ensure_ascii=False,
                    ),
                ),
            )
        with get_db() as conn:
            texts = repair._load_project_summary_and_baseline(conn, pid)

        assert "global_facts" not in texts, texts

    def test_missing_analysis_row_is_safe(self):
        """项目没有 analysis 行时不得报错，返回空扫描宇宙。"""
        pid = 993008
        with get_db() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO projects (id, name) VALUES (?, ?)",
                (pid, "P993008"),
            )
            texts = repair._load_project_summary_and_baseline(conn, pid)

        assert texts == {}, texts


# ============================================================================
# F-10 总括式人数「共 N 人」抽取（检测/修复口径对齐）
# ============================================================================


class TestBareTotalHeadcountExtraction:
    """F-10：「本项目共 20 人参与施工」整类漏抽。

    原式只有「岗位词 + 数字 + 人」一种写法，总括式（总括词在数字前、无岗位词）
    全部漏抽；而自动修复侧的结构性闸门（取值后紧跟「人」即放行）却**认**这种
    写法 —— 检测看不见、修复却在改，两侧口径不一致。
    """

    @pytest.mark.parametrize(
        "text,expected",
        [
            ("本项目共 20 人参与施工。", [("人员数量", "20")]),
            ("合计 30 人到岗。", [("人员数量", "30")]),
            ("共计投入 15 人负责现场。", [("人员数量", "15")]),
            ("总计 8 人组成项目管理机构。", [("人员数量", "8")]),
            ("总共 12 人参与投标。", [("人员数量", "12")]),
        ],
    )
    def test_total_form_is_extracted(self, auditor, text, expected):
        assert _extract(auditor, text) == expected, text

    @pytest.mark.parametrize(
        "text",
        [
            "本项目共投入 45 人天。",
            "累计完成 30 人次。",
            "共计 12 人月。",
            "本项目共计投入 100 工时。",
            "共 3 人受伤。",
            "共 2 人死亡。",
            "共 4 人失踪。",
            "共 2 人被困。",
            "共 1 人遇难。",
            "本项目共发生 3 起事故。",
            "累计 5 人受伤。",
        ],
    )
    def test_unit_and_casualty_words_are_excluded(self, auditor, text):
        """单位词与事故伤亡复合词不得被当编制人数。"""
        assert _extract(auditor, text) == [], text

    @pytest.mark.parametrize(
        "text",
        [
            "共安排 5 人值守夜间施工。",
            "共安排 8 人负责材料验收。",
            "共计安排 10 人轮流值班。",
            "共参与 12 人到场施工。",
            "总计参与 6 人专家评审。",
        ],
    )
    def test_subset_scope_verbs_are_not_extracted(self, auditor, text):
        """R-1：排班/参与子集动词（安排/参与）不得把子集人数抽进通用「人员数量」桶。

        通用桶不按范围区分，同篇内「共投入 30 人施工」+「共安排 5 人值守」会互判成
        30 vs 5 的假冲突——与 2026-10-04 修的「不同岗位归一桶」同类结构性误报。
        注意动词组是可选的，总括词后无动词的写法（见 test_total_form_is_extracted）
        不受收窄影响。
        """
        assert _extract(auditor, text) == [], text

    def test_total_form_isolated_from_role_prefixes(self, auditor):
        """总括式与岗位式各自归桶：不得互相串改，也不得合并成一条。"""
        text = "共 20 人参与，其中技术人员 20 人。"
        assert _extract(auditor, text) == [
            ("人员数量", "20"),
            ("技术人员数量", "20"),
        ]

    def test_role_form_unaffected_by_total_branch(self, auditor):
        """回归保护：岗位式写法的抽取结果不得被新增分支改变。"""
        assert _extract(auditor, "技术人员 20 人、管理人员 5 人。") == [
            ("技术人员数量", "20"),
            ("管理人员数量", "5"),
        ]
        assert _extract(auditor, "本项目组织人员 20 人。") == [("人员数量", "20")]


# ============================================================================
# F-9 端到端：AI 补丁落文后的收敛判定
# ============================================================================


class TestRepairRefreshScopeEndToEnd:
    """F-9 端到端：`repair_with_ai` 落文后的 `conflict_states` 必须扫全项目
    事实来源（正文 + 摘要 + 已确认事实基准）。

    只 mock `generate_patches`（无需真实 AI / 网络）：补丁定位、落库、缓存失效、
    冲突收敛全走真实代码路径。
    """

    def test_summary_only_old_value_not_falsely_resolved(self, monkeypatch):
        pid, sid = 993100, 9931000
        with get_db() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO projects (id, name, notes) VALUES (?, ?, ?)",
                (pid, "P993100", "建设工期：450 日历天。"),
            )
            conn.execute(
                "INSERT OR REPLACE INTO sections "
                "(id, project_id, title, content, status, sort_order) "
                "VALUES (?, ?, ?, ?, 'completed', 1)",
                (sid, pid, "工期说明", "本项目建设工期：45 天。"),
            )
            conn.execute(
                "INSERT OR REPLACE INTO consistency_conflicts "
                "(project_id, conflict_id, fact_name, values_json, sources_json, "
                "severity, description, created_at, resolved) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, '2026-10-11', 0)",
                (pid, f"cf_{pid}", "工期", json.dumps(["45", "450"]), "[]", "high", ""),
            )

        async def fake_generate_patches(provider, *, sections, conflicts_text, facts_text):
            return (
                [
                    {
                        "section_id": str(sid),
                        "old_text": "本项目建设工期：45 天。",
                        "new_text": "本项目建设工期：45 天，进度可控。",
                        "reason": "补齐表述",
                    }
                ],
                [],
                None,
            )

        monkeypatch.setattr(repair, "generate_patches", fake_generate_patches)
        report = asyncio.run(repair.repair_with_ai(pid, provider=object()))

        assert report["success"] is True, report
        assert len(report["applied"]) == 1, report
        states = report["conflict_states"] or {}
        # 正文已统一，但摘要仍写「450 日历天」→ 不得判为已解决
        assert states["resolved"] == 0, (report, states)
        assert states["still_open"] == 1, states

        with get_db() as conn:
            conflict = conn.execute(
                "SELECT resolved FROM consistency_conflicts WHERE conflict_id = ?",
                (f"cf_{pid}",),
            ).fetchone()["resolved"]
            content = conn.execute(
                "SELECT content FROM sections WHERE id = ?", (sid,)
            ).fetchone()["content"]

        assert conflict == 0, "摘要里的旧取值仍在，冲突必须保持未解决"
        assert content == "本项目建设工期：45 天，进度可控。", content

    def test_all_sources_unified_resolves_after_repair(self, monkeypatch):
        """回归保护：正文与摘要/基准三方一致时仍允许关闭。"""
        pid, sid = 993101, 9931010
        with get_db() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO projects (id, name, notes) VALUES (?, ?, ?)",
                (pid, "P993101", "建设工期：45 日历天。"),
            )
            conn.execute(
                "INSERT OR REPLACE INTO sections "
                "(id, project_id, title, content, status, sort_order) "
                "VALUES (?, ?, ?, ?, 'completed', 1)",
                (sid, pid, "工期说明", "本项目建设工期：450 天。"),
            )
            conn.execute(
                "INSERT OR REPLACE INTO consistency_conflicts "
                "(project_id, conflict_id, fact_name, values_json, sources_json, "
                "severity, description, created_at, resolved) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, '2026-10-11', 0)",
                (pid, f"cf_{pid}", "工期", json.dumps(["45", "450"]), "[]", "high", ""),
            )

        async def fake_generate_patches(provider, *, sections, conflicts_text, facts_text):
            return (
                [
                    {
                        "section_id": str(sid),
                        "old_text": "本项目建设工期：450 天。",
                        "new_text": "本项目建设工期：45 天。",
                        "reason": "对齐摘要工期",
                    }
                ],
                [],
                None,
            )

        monkeypatch.setattr(repair, "generate_patches", fake_generate_patches)
        report = asyncio.run(repair.repair_with_ai(pid, provider=object()))

        assert report["success"] is True, report
        states = report["conflict_states"] or {}
        assert states["resolved"] == 1, (report, states)
        assert states["still_open"] == 0, states

        with get_db() as conn:
            row = conn.execute(
                "SELECT resolved, resolved_value FROM consistency_conflicts WHERE conflict_id = ?",
                (f"cf_{pid}",),
            ).fetchone()
        assert row["resolved"] == 1
        assert row["resolved_value"] == "45"


# ============================================================================
# R-5 accept_fact_proposals 合并必须在同一个事务临界区内
# ============================================================================


class TestAcceptFactProposalsAtomicMerge:
    """R-5：「读快照 + 去重合并 + 写回」必须是同一个 get_db() 块。

    旧实现把读和写拆在两个独立事务里。db.get_db 的全局写锁（threading.RLock）
    只串行化**单个事务块**、不覆盖块与块之间的间隙，于是并发两次 accept 可以在
    「A 读到空快照 → B 也读到空快照 → A 写 {a} → B 写 {b}」的交错下丢掉 A 刚合并
    的那条事实（lost update）。路由层 `_LockManager(project_id)` 只挡住 HTTP 入口，
    服务层被第二个调用方绕过路由调用时窗口立即重现。

    判据（确定性、不依赖线程调度）：monkeypatch `dump_items` 让 A 在合并临界区内
    暂停。临界区若原子，A 此时必然持有 DB 全局写锁，另一线程直接 get_db() 读快照
    会被阻塞；旧实现下 A 在 dump_items 时尚未持锁，读线程会立刻完成。
    """

    def test_merge_holds_db_write_lock_across_snapshot_and_write(self, monkeypatch):
        import threading

        from app.services import consistency_backflow as backflow
        from app.services.consistency_backflow import accept_fact_proposals

        pid = 993200
        with get_db() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO projects (id, name, notes) VALUES (?, ?, ?)",
                (pid, "P993200", ""),
            )
            conn.execute(
                "INSERT OR REPLACE INTO analysis (project_id, global_facts_json) "
                "VALUES (?, ?)",
                (
                    pid,
                    json.dumps(
                        [{"key": "base", "label": "工期", "value": "450"}],
                        ensure_ascii=False,
                    ),
                ),
            )

        real_dump_items = backflow.dump_items
        entered = threading.Event()
        release = threading.Event()

        def paused_dump(items):
            entered.set()  # A 已进入合并临界区
            assert release.wait(5), "临界区未在测试内释放"
            return real_dump_items(items)

        monkeypatch.setattr(backflow, "dump_items", paused_dump)

        a = threading.Thread(
            target=accept_fact_proposals,
            args=(pid, [{"fact_name": "质量标准", "value": "合格", "category": "quality"}]),
        )
        a.start()
        assert entered.wait(5), "accept 未进入合并临界区"

        reader_done = threading.Event()

        def reader():
            with get_db() as conn:
                conn.execute(
                    "SELECT global_facts_json FROM analysis WHERE project_id = ?", (pid,)
                ).fetchone()
            reader_done.set()

        reader = threading.Thread(target=reader)
        try:
            reader.start()
            assert not reader_done.wait(0.5), (
                "合并临界区未持有 DB 写锁：并发读快照未被阻塞（lost update 窗口重现）"
            )
        finally:
            # 无论断言成败都放行 A，避免后台线程卡在 release.wait 上刷 traceback
            release.set()
        a.join(5)
        assert not a.is_alive(), "accept 线程未正常结束"
        assert reader_done.wait(5), "读线程在 accept 释放锁后仍未完成"

        # 合并结果必须同时含既有事实与新采纳事实（未被自己的旧快照覆写）
        with get_db() as conn:
            row = conn.execute(
                "SELECT global_facts_json FROM analysis WHERE project_id = ?", (pid,)
            ).fetchone()
        items = json.loads(row["global_facts_json"]) if row else []
        labels = {it.get("label") for it in items}
        assert {"工期", "质量标准"} <= labels, row


# ============================================================================
# R-8 / R-14 merge_fact_proposals 纯函数契约
# ============================================================================


class TestMergeFactProposalsPure:
    """R-8：合并逻辑已提纯为纯函数，可在不动数据库的情况下钉死契约。

    `accept_fact_proposals` 的临界区 = 一次 SELECT + 本函数 + 一次 INSERT，
    因此「函数体不做任何 I/O」是硬约定：锁内引入查库/调 AI/网络调用会把持锁
    时长拖向 `db._db_write_lock_timeout`（当前 8.0s）的超时边界，争用时把后续
    请求打成 `DatabaseWriteLockTimeout`。本组用例把合并规则逐条钉住，也防止
    有人往临界区里偷偷塞回外部调用。
    """

    def test_no_db_access_and_no_inplace_mutation(self):
        """纯函数：不查库、不就地修改入参（只返回浅拷贝后的新列表）。"""
        from app.services import consistency_backflow as backflow
        from app.services.consistency_backflow import merge_fact_proposals

        existing = [{"key": "base", "label": "工期", "value": "450"}]
        snapshot = [dict(it) for it in existing]
        calls = []
        orig_get_db = backflow.get_db

        def boom(*args, **kwargs):
            calls.append((args, kwargs))
            raise AssertionError("merge_fact_proposals 不得访问数据库")

        backflow.get_db = boom
        try:
            items, added = merge_fact_proposals(
                existing,
                [
                    {
                        "fact_name": "质量标准",
                        "value": "合格",
                        "category": "quality",
                        "conflict_id": "c1",
                    }
                ],
            )
        finally:
            backflow.get_db = orig_get_db

        assert calls == [], "纯函数出现数据库访问"
        assert added == 1
        assert existing == snapshot, "入参被就地修改"
        assert items is not existing
        assert items[0] == snapshot[0]

    def test_skips_invalid_proposals(self):
        """非 dict / 空 value / 空 fact_name / 占位值一律跳过，且不改既有清单。"""
        from app.services.consistency_backflow import merge_fact_proposals

        existing = [{"key": "base", "label": "工期", "value": "450"}]
        items, added = merge_fact_proposals(
            existing,
            [
                "not-a-dict",
                {"fact_name": "质量标准", "value": "  "},
                {"fact_name": "  ", "value": "合格"},
                {"fact_name": "工期", "value": "【待填写】"},
                {"fact_name": "工期", "value": "【待填写：请在投标前确认】"},
            ],
        )
        assert added == 0
        assert items == existing

    def test_category_defaults_to_infer_category(self):
        """category 缺省时按事实名推导；显式给出则原样保留。"""
        from app.services.consistency_backflow import merge_fact_proposals

        items, added = merge_fact_proposals(
            [],
            [
                {"fact_name": "工期", "value": "450 日历天", "conflict_id": "c1"},
                {"fact_name": "其他事项", "value": "详见响应", "category": "quality", "conflict_id": "c2"},
            ],
        )
        assert added == 2
        assert [it["category"] for it in items] == ["schedule", "quality"]

    def test_key_prefers_suggested_key_else_conflict_id(self):
        """key 优先 suggested_key；缺省时退回 audit_<conflict_id>。"""
        from app.services.consistency_backflow import merge_fact_proposals

        items, added = merge_fact_proposals(
            [],
            [
                {"fact_name": "工期", "value": "450 日历天", "suggested_key": "myKey", "conflict_id": "c1"},
                {"fact_name": "安全标准", "value": "无伤亡", "conflict_id": "c2"},
            ],
        )
        assert added == 2
        assert [it["key"] for it in items] == ["myKey", "audit_c2"]
        assert [it["label"] for it in items] == ["工期", "安全标准"]
        assert [it["source"] for it in items] == ["audit_backflow", "audit_backflow"]

    def test_dedup_by_key_is_idempotent(self):
        """同 key 跳过：同一冲突重复采纳只留一条（幂等）。"""
        from app.services.consistency_backflow import merge_fact_proposals

        items, added = merge_fact_proposals(
            [],
            [
                {"fact_name": "工期", "value": "450", "conflict_id": "c1"},
                {"fact_name": "工期", "value": "460", "conflict_id": "c1"},
            ],
        )
        assert added == 1
        assert len(items) == 1

    def test_existing_same_key_blocks_new(self):
        """既有事实的同 key（或无 key 时的同 label）会拦下新候选。"""
        from app.services.consistency_backflow import merge_fact_proposals

        items, added = merge_fact_proposals(
            [{"key": "audit_c9", "label": "工期", "value": "400"}],
            [{"fact_name": "工期", "value": "450", "conflict_id": "c9"}],
        )
        assert added == 0
        assert items == [{"key": "audit_c9", "label": "工期", "value": "400"}]

    def test_order_is_stable(self):
        """顺序稳定：既有项在前，新增项按 proposals 原顺序追加。"""
        from app.services.consistency_backflow import merge_fact_proposals

        existing = [
            {"key": "b1", "label": "工期", "value": "450"},
            {"key": "b2", "label": "质量标准", "value": "合格"},
        ]
        items, added = merge_fact_proposals(
            existing,
            [
                {"fact_name": "安全标准", "value": "零事故", "conflict_id": "c1"},
                {"fact_name": "质保期", "value": "24 个月", "conflict_id": "c2"},
            ],
        )
        assert added == 2
        assert [it["key"] for it in items] == ["b1", "b2", "audit_c1", "audit_c2"]

    def test_same_label_different_value_both_kept(self):
        """R-14 已知限制：同 label 不同 value 不会被 dedup 拦下，两条都留。

        R-14 已知限制：同 label 不同 value 不会被 dedup 拦下，两条都留。

        `dedup_key` 只比较 key（无 key 时退化为 label）、不含 value；而候选的
        key 恒为 `audit_<conflict_id>`，故用户跨轮确认「同一事实名的两个不同
        取值」时会落下两条同 label 的基准事实。下游
        `consistency_auditor.FACT_BASELINE_CATEGORIES` 按 category 收基准、并把
        同名下**所有**值都收进 `all_facts` 互比（没有「首条胜出」），于是用户
        自己确认的两条会互相判成冲突，且该冲突来自 global_facts 本身、无法靠
        改正文消解。判定不改语义（label 去重会静默丢弃用户明确确认的候选），
        此处把该行为钉死以免被无意「修好」。
        """
        from app.services.consistency_backflow import merge_fact_proposals

        items, added = merge_fact_proposals(
            [],
            [
                {"fact_name": "工期", "value": "450 日历天", "conflict_id": "c1"},
                {"fact_name": "工期", "value": "460 日历天", "conflict_id": "c2"},
            ],
        )
        assert added == 2
        assert [(it["label"], it["value"]) for it in items] == [
            ("工期", "450 日历天"),
            ("工期", "460 日历天"),
        ]

    def test_empty_inputs(self):
        """空入参：既有清单为空仍正常合并；候选为空时不新增也不复制。"""
        from app.services.consistency_backflow import merge_fact_proposals

        assert merge_fact_proposals([], []) == ([], 0)
        items, added = merge_fact_proposals(
            [],
            [{"fact_name": "工期", "value": "450", "conflict_id": "c1"}],
        )
        assert (added, len(items)) == (1, 1)
        items2, added2 = merge_fact_proposals(
            [{"key": "b1", "label": "工期", "value": "450"}], []
        )
        assert added2 == 0
        assert items2 == [{"key": "b1", "label": "工期", "value": "450"}]

