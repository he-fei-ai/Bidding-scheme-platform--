# -*- coding: utf-8 -*-
"""ENHANCE 2026-09-15 第二轮增强回归测试。

覆盖：
1. 全局事实三档缺失值策略（fabricate / omit / placeholder）
2. 一致性审计分组口径（字符装箱）与并发拆分
3. text_patch「区间占比保护」（整段重写拒绝）
4. 废标项跨分块 pending / resolved / confirmed 状态机
5. 错别字一键修正（任务内替换 + 逐条错误隔离）
"""

import json

import pytest

from app.db import get_db
from app.routers.facts import (
    _normalize_facts,
    _policy_rule_text,
    _schedule_fallback_value,
    normalize_facts_policy,
)
from app.services import rejection, text_patch
from app.services.ai.workflows_expand import (
    AUDIT_GROUP_CHAR_LIMIT,
    AUDIT_SECTION_CHAR_LIMIT,
    _split_audit_groups,
)


# ======================= 1. 全局事实缺失值策略 =======================


class TestFactsMissingValuePolicy:
    def test_policy_normalization(self):
        assert normalize_facts_policy(None) == "fabricate"
        assert normalize_facts_policy("") == "fabricate"
        assert normalize_facts_policy("Omit ") == "omit"
        assert normalize_facts_policy("placeholder") == "placeholder"
        # 非法值回退默认而不是抛错（前端自由输入不炸接口）
        assert normalize_facts_policy("hack") == "fabricate"

    def test_schedule_fallback_by_policy(self):
        assert "【待填写】" == _schedule_fallback_value("placeholder")
        # omit 是笼统承诺，不含任何编造的数字
        assert _schedule_fallback_value("omit") == "按招标文件要求及合同约定执行"

    def test_policy_rule_texts_constrain_ai(self):
        # omit 提示词必须明确「严禁编造」
        assert "严禁" in _policy_rule_text("omit")
        # placeholder 提示词必须给出【待填写】占位格式
        assert "【待填写】" in _policy_rule_text("placeholder")

    def test_normalize_facts_placeholder_marks_manual_input(self):
        items = _normalize_facts([], fallback_text="", policy="placeholder")
        schedule = [f for f in items if f.get("key") == "scheduleDuration"]
        assert schedule, "必须有工期兜底条目"
        assert schedule[0]["value"] == "【待填写】"
        assert schedule[0]["needs_manual_input"] is True

    def test_normalize_facts_omit_uses_generic_promise(self):
        items = _normalize_facts([], fallback_text="", policy="omit")
        schedule = [f for f in items if f.get("key") == "scheduleDuration"]
        assert schedule[0]["value"] == "按招标文件要求及合同约定执行"
        # omit 不逐条打「需人工填写」——笼统承诺本身是完整表达
        assert not schedule[0].get("needs_manual_input")

    def test_normalize_facts_dedup_keeps_first(self):
        items = [
            {"key": "projectName", "label": "项目名称", "value": "A"},
            {"key": "projectName", "label": "项目名称", "value": "B"},
        ]
        result = _normalize_facts(items, fallback_text="", policy="fabricate")
        names = [f for f in result if f.get("key") == "projectName"]
        assert len(names) == 1 and names[0]["value"] == "A"


# ======================= 2. 一致性审计分组口径 =======================


class TestAuditGroupSplitting:
    @staticmethod
    def _section(chars: int, idx: int) -> dict:
        return {"id": f"s{idx}", "content": "字" * chars}

    def test_groups_respect_char_budget(self):
        sections = [self._section(AUDIT_SECTION_CHAR_LIMIT, i) for i in range(10)]
        groups = _split_audit_groups(sections)
        assert len(groups) > 1
        total = 0
        for group in groups:
            group_chars = sum(
                min(len(s["content"]), AUDIT_SECTION_CHAR_LIMIT) for s in group
            )
            assert group_chars <= AUDIT_GROUP_CHAR_LIMIT
            total += len(group)
        assert total == len(sections)

    def test_oversize_section_packed_by_full_render_size(self):
        # BUG-FIX 2026-10-15：本用例原断言基于旧「截断计数」口径（超长节只按
        # 单节上限计重、可与小节同组）。ENHANCE 2026-10-15（分块全量送审）后，
        # 超长节按真实渲染字符数计重并自动独立成组，消除尾部审计盲区；
        # 与 test_enhancement_2026_09_15_facts_audit_patch.py 的新口径同步。
        big = self._section(AUDIT_GROUP_CHAR_LIMIT + 5000, 0)
        small = self._section(100, 1)
        groups = _split_audit_groups([big, small])
        assert len(groups) == 2, "超长节必须按全量送审载荷独立成组"
        assert [s["id"] for s in groups[0]] == ["s0"]
        assert [s["id"] for s in groups[1]] == ["s1"]

    def test_small_sections_pack_into_one_group(self):
        sections = [self._section(100, i) for i in range(5)]
        groups = _split_audit_groups(sections)
        assert len(groups) == 1 and len(groups[0]) == 5


# ======================= 3. text_patch 区间占比保护 =======================


class TestDisproportionatePatchGuard:
    LONG_TEXT = "原文内容。" * 120  # 远超 400 字符下限

    def test_short_text_whole_replace_allowed(self):
        rejected, detail = text_patch.is_disproportionate_patch("短文本", "短文本")
        assert not rejected
        assert detail["share"] == 0.0  # 低于 min_chars 不启用，share 不计

    def test_whole_section_rewrite_rejected(self):
        old_text = self.LONG_TEXT[: int(len(self.LONG_TEXT) * 0.95)]
        rejected, detail = text_patch.is_disproportionate_patch(self.LONG_TEXT, old_text)
        assert rejected and detail["share"] >= 0.9

    def test_normal_partial_patch_allowed(self):
        ok, _detail = text_patch.is_disproportionate_patch(self.LONG_TEXT, "原文内容。")
        assert not ok

    def test_apply_exact_patch_raises_on_disproportionate(self):
        old_text = self.LONG_TEXT[: int(len(self.LONG_TEXT) * 0.95)]
        with pytest.raises(text_patch.PatchApplyError, match="整段重写"):
            text_patch.apply_exact_patch(self.LONG_TEXT, {"old_text": old_text, "new_text": "新"})

    def test_guard_can_be_disabled(self):
        old_text = self.LONG_TEXT[: int(len(self.LONG_TEXT) * 0.95)]
        result = text_patch.apply_exact_patch(
            self.LONG_TEXT,
            {"old_text": old_text, "new_text": "新"},
            disproportionate_ratio=None,
        )
        assert result.startswith("新")

    def test_describe_patch_match_reports_guard(self):
        old_text = self.LONG_TEXT[: int(len(self.LONG_TEXT) * 0.95)]
        detail = text_patch.describe_patch_match(self.LONG_TEXT, {"old_text": old_text})
        assert detail["disproportionate"] is True


# ======================= 4. 跨分块疑似缺失状态机 =======================


class TestMissingMaterialStateMachine:
    FULL_TEXT = (
        "第一章 施工方案。本项目配备专职安全员，并按要求提供安全生产许可证扫描件。\n"
        "第二章 质量保证。附质量管理体系认证证书。"
    )

    def test_extract_keywords_from_quotes(self):
        issue = {"description": "未找到「安全生产许可证」，疑似材料缺失", "location": ""}
        keywords = rejection.extract_claim_keywords(issue)
        assert "安全生产许可证" in keywords

    def test_extract_keywords_requires_material_suffix(self):
        # 普通名词（不带材料后缀）不能作为核验关键词，否则误判率放大
        issue = {"description": "缺少相关说明和内容", "location": ""}
        assert rejection.extract_claim_keywords(issue) == []

    def test_resolved_when_material_exists_elsewhere(self):
        issue = {
            "description": "本分块未找到「安全生产许可证」",
            "location": "",
            "evidence_kind": "structural",
        }
        stats = rejection.resolve_missing_material_claims([issue], self.FULL_TEXT)
        assert issue["missing_state"] == rejection.MISSING_STATE_RESOLVED
        assert issue["missing_state_hits"]
        assert "安全生产许可证" in issue["excerpt"]
        assert stats["stats"]["resolved"] == 1

    def test_confirmed_when_nowhere_found(self):
        issue = {
            "description": "全文未见「排污许可证」",
            "location": "",
            "evidence_kind": "structural",
        }
        stats = rejection.resolve_missing_material_claims([issue], self.FULL_TEXT)
        assert issue["missing_state"] == rejection.MISSING_STATE_CONFIRMED
        assert stats["stats"]["confirmed"] == 1

    def test_pending_when_no_verifiable_keyword(self):
        issue = {"description": "疑似缺少部分佐证材料", "location": "", "evidence_kind": "structural"}
        stats = rejection.resolve_missing_material_claims([issue], self.FULL_TEXT)
        assert issue["missing_state"] == rejection.MISSING_STATE_PENDING
        assert stats["stats"]["pending"] == 1

    def test_verified_items_untouched(self):
        # 已做原文核验的条目不应被状态机改写
        issue = {
            "description": "工期矛盾「安全生产许可证」",
            "evidence_kind": "verified",
            "validated": True,
        }
        rejection.resolve_missing_material_claims([issue], self.FULL_TEXT)
        assert "missing_state" not in issue


# ======================= 5. 错别字一键修正 =======================


def _create_completed_task(text: str) -> int:
    """建一个 completed 状态、含已核验错别字条目的任务，返回任务 ID。"""
    from app.services import rejection as R

    task_id = R.create_task(
        project_id=None,
        tender_text="测试招标文件",
        bid_documents=[{"id": 1, "name": "投标A.docx", "text": text}],
        check_items=[],
        enabled_dimensions=["spelling"],
    )
    results = {
        "rejection": [],
        "logic": [],
        "spelling": [
            {
                "id": "sp-1",
                "original": "实施工程",
                "corrected": "示范工程",
                "bid_doc_id": 1,
                "severity": "warning",
                "verified": True,
            },
            {
                "id": "sp-2",
                "original": "不存在的词",
                "corrected": "x",
                "bid_doc_id": 1,
                "severity": "warning",
                "verified": True,
            },
        ],
    }
    with get_db() as conn:
        conn.execute(
            "UPDATE rejection_checks SET status = 'completed', results = ? WHERE id = ?",
            (json.dumps(results, ensure_ascii=False), task_id),
        )
    return task_id


class TestApplySpellingFixes:
    def test_apply_single_fix_updates_task_text(self):
        task_id = _create_completed_task("本项目实施工程已完成，实施工程质量合格。")
        out = rejection.apply_spelling_fixes(task_id, ["sp-1"])
        assert out["applied_count"] == 1
        assert out["applied"][0]["before"] == "实施工程"
        task = rejection.get_task(task_id)
        text = task["bid_documents"][0]["text"]
        assert "本项目示范工程已完成" in text
        # 只替换第一处命中位置（apply_range_edits 精确下标），第二处仍保留原文
        assert "实施工程质量合格" in text

    def test_unlocatable_item_skipped_not_dropped(self):
        task_id = _create_completed_task("正常文本。")
        out = rejection.apply_spelling_fixes(task_id, ["sp-1", "sp-2"])
        assert out["applied_count"] == 0
        reasons = {s["item_id"]: s["reason"] for s in out["skipped"]}
        assert "sp-1" in reasons  # 原文不含「实施工程」

    def test_running_task_rejected(self):
        from app.services import rejection as R

        task_id = _create_completed_task("本项目实施工程。")
        with get_db() as conn:
            conn.execute(
                "UPDATE rejection_checks SET status = 'running' WHERE id = ?", (task_id,)
            )
        with pytest.raises(ValueError, match="尚未结束"):
            R.apply_spelling_fixes(task_id, ["sp-1"])

    def test_unknown_task_raises(self):
        with pytest.raises(ValueError, match="任务不存在"):
            rejection.apply_spelling_fixes(999999, ["sp-1"])

    def test_empty_item_ids_raises(self):
        task_id = _create_completed_task("本项目实施工程。")
        with pytest.raises(ValueError, match="未指定"):
            rejection.apply_spelling_fixes(task_id, [])

    def test_unverified_item_rejected(self):
        task_id = _create_completed_task("本项目实施工程。")
        with get_db() as conn:
            row = conn.execute(
                "SELECT results FROM rejection_checks WHERE id = ?", (task_id,)
            ).fetchone()
            results = json.loads(row["results"])
            results["spelling"][0]["verified"] = False
            conn.execute(
                "UPDATE rejection_checks SET results = ? WHERE id = ?",
                (json.dumps(results, ensure_ascii=False), task_id),
            )
        out = rejection.apply_spelling_fixes(task_id, ["sp-1"])
        assert out["applied_count"] == 0
        assert "未通过原文核验" in out["skipped"][0]["reason"]
