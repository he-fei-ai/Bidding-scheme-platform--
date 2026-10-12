"""全局事实模块 —— 组件级交互测试（ENHANCE 2026-09-29）。

覆盖「事实命名空间正本」与它的上下游：
  A. 命名空间一致性 —— routers/facts、workflows_analysis、sse_utils 三处历史副本
     必须指向同一实现（分类表、中文 label、占位判定、策略工具）。
  B. 占位契约 —— 【待填写】/【待填写：xxx】两形态在打标、计数、生成过滤同一口径。
  C. 存储读写 —— analysis.global_facts_json 的扁平/包装/坏数据三态，
     以及写入方（facts.py）与读取方（正文生成 / 一致性修复）互认。
  D. 去重与归一化 —— 占位压住真实值的补强、同义排版的判等。
  E. 路由与接口（Tab 级）—— GET/POST/generate 的返回体字段、并发生成防护、
     审计缓存失效。
  F. 一致性审计端点 —— 占位跳过、多组合并取最严、单组失败降级 partial、结果落库。
  G. 与 consistency_auditor 的数据链 —— 全局事实作为审计基线、修复建议以基准为准。
  H. Step02 自动提取链路的策略透传。

设计原则：只测行为不测实现；不依赖真实 AI 与外部网络（用可编程假 provider）。
"""

from __future__ import annotations

import asyncio
import json
import re

import pytest

from app.db import get_db, init_db


# ==================== 公共工具 ====================


def _new_project(name: str) -> int:
    init_db()
    with get_db() as conn:
        conn.execute("INSERT INTO projects (name, industry) VALUES (?, ?)", (name, "通用"))
        return conn.execute("SELECT id FROM projects ORDER BY id DESC LIMIT 1").fetchone()["id"]


def _seed_analysis(project_id: int, items: list, *, policy: str = "fabricate", **extra) -> None:
    """写入 analysis 行；extra 用于补 facts_audit_json 等审计列。"""
    cols = ["project_id", "global_facts_json", "facts_policy"]
    values = [project_id, json.dumps(items, ensure_ascii=False), policy]
    for key, val in extra.items():
        cols.append(key)
        values.append(val)
    placeholders = ", ".join("?" for _ in cols)
    sql = f"INSERT INTO analysis ({', '.join(cols)}) VALUES ({placeholders}) "
    sql += "ON CONFLICT(project_id) DO UPDATE SET " + ", ".join(
        f"{c} = excluded.{c}" for c in cols[1:]
    )
    with get_db() as conn:
        conn.execute(sql, tuple(values))


def _seed_section(project_id: int, title: str, content: str, sort_order: int = 0) -> int:
    with get_db() as conn:
        conn.execute(
            "INSERT INTO sections (project_id, title, level, sort_order, content, status) "
            "VALUES (?, ?, 1, ?, ?, 'completed')",
            (project_id, title, sort_order, content),
        )
        return conn.execute("SELECT id FROM sections ORDER BY id DESC LIMIT 1").fetchone()["id"]


# ============================================================
# A. 命名空间一致性（正本合一，副本不再漂移）
# ============================================================


class TestFactsNamespaceSingleSource:
    def test_router_aliases_are_the_same_objects(self):
        """routers/facts 的旧名字必须是服务层正本本身（而不是第二份实现）。"""
        from app.routers import facts as facts_router
        from app.services import facts_service

        assert facts_router._normalize_facts is not facts_service.normalize_fact_items
        # 上面是 wrapper（为保留 docstring），但底层必须是同一实现
        assert facts_router._DEFAULT_FACTS_POLICY is facts_service.DEFAULT_FACTS_POLICY
        assert facts_router._DEFAULT_FACT_CATEGORIES is facts_service.FACT_CATEGORIES
        assert facts_router._policy_rule_text is facts_service.policy_rule_text
        assert facts_router._schedule_fallback_value is facts_service.schedule_fallback_value
        assert facts_router._coerce_fact_audit_list is facts_service.coerce_audit_list
        assert facts_router._dict_to_fact_items is facts_service.dict_to_fact_items

    def test_workflows_analysis_shares_the_canonical_impl(self):
        """Step02 自动提取链路不得再持有第二份分类/规范化实现。"""
        from app.services import facts_service
        from app.services.ai import workflows_analysis as wa

        assert wa.dict_to_fact_items is facts_service.dict_to_fact_items
        assert wa.normalize_fact_items is facts_service.normalize_fact_items
        assert wa.fact_extract_system_prompt is facts_service.fact_extract_system_prompt
        assert wa.DEFAULT_FACTS_POLICY == facts_service.DEFAULT_FACTS_POLICY

    def test_sse_reader_shares_the_canonical_parser(self):
        """正文生成读取事实与路由 GET 必须同一口径（否则前端有事实、正文没注入）。"""
        from app.routers.sse import sse_utils
        from app.services import facts_service

        assert sse_utils.parse_stored_items is facts_service.parse_stored_items
        assert sse_utils.is_placeholder_value is facts_service.is_placeholder_value
        assert sse_utils._PLACEHOLDER_VALUE_PREFIX == facts_service.PLACEHOLDER_VALUE_PREFIX

    def test_categories_cover_implementation_with_chinese_labels(self):
        """分类命名空间：10 项、含 implementation、中文标签非空（旧表缺它显示英文）。"""
        from app.services.facts_service import FACT_CATEGORIES, FACT_CATEGORY_LABELS

        keys = [k for k, _ in FACT_CATEGORIES]
        assert len(keys) == len(set(keys)), "分类键不得重复"
        assert "implementation" in keys
        assert all(label and label != key for key, label in FACT_CATEGORIES)
        assert FACT_CATEGORY_LABELS["implementation"] == "实施做法"

    def test_normalize_category_accepts_chinese_and_aliases(self):
        from app.services.facts_service import normalize_category

        assert normalize_category("schedule") == "schedule"
        assert normalize_category("工期") == "schedule"
        assert normalize_category(" QUALITY ") == "quality"
        # 未知值回退 basic，而不是把脏值写进库把前端分组打散
        assert normalize_category("乱七八糟") == "basic"
        assert normalize_category(None) == "basic"

    def test_dict_to_fact_items_uses_chinese_labels(self):
        """BUG 回归：旧副本 label 直接用英文 key，前端展示成 projectName 一串。"""
        from app.services.facts_service import dict_to_fact_items

        items = dict_to_fact_items({"projectName": "示范工程", "scheduleDuration": "450 日历天"})
        by_key = {i["key"]: i for i in items}
        assert by_key["projectName"]["label"] == "项目名称"
        assert by_key["scheduleDuration"]["label"] == "建设工期"
        # 中文 label 参与分类推断：英文 key + 中文标签也能正确归类
        assert by_key["scheduleDuration"]["category"] == "schedule"
        assert all(i["source"] == "ai" for i in items)

    def test_dict_to_fact_items_skips_empty_values(self):
        from app.services.facts_service import dict_to_fact_items

        items = dict_to_fact_items({"a": None, "b": "   ", "c": "值", "d": 0})
        assert [i["key"] for i in items] == ["c", "d"], "None/空串丢弃，0 属合法取值"


# ============================================================
# B. 占位契约（两形态 × 打标 / 计数 / 过滤）
# ============================================================


class TestPlaceholderContract:
    def test_both_placeholder_forms_recognized(self):
        from app.services.facts_service import is_placeholder_value

        assert is_placeholder_value("【待填写】") is True
        assert is_placeholder_value("【待填写：项目经理姓名】") is True
        # 句中内嵌占位也必须识别，否则会漏进正文 prompt
        assert is_placeholder_value("工期为【待填写】天") is True
        assert is_placeholder_value("450 日历天") is False
        assert is_placeholder_value("") is False
        assert is_placeholder_value(None) is False

    def test_mark_and_count_use_the_same_ruler(self):
        from app.services.facts_service import count_placeholders, mark_placeholders

        items = [
            {"key": "a", "value": "【待填写】"},
            {"key": "b", "value": "【待填写：姓名】"},
            {"key": "c", "value": "真实值"},
        ]
        assert count_placeholders(items) == 2
        assert mark_placeholders(items) == 2
        assert items[0]["needs_manual_input"] is True
        assert items[2].get("needs_manual_input") is None

    def test_normalize_items_placeholder_policy_marks_fallback(self):
        """placeholder 档的工期兜底必须自带 needs_manual_input，供前端出角标。"""
        from app.services.facts_service import normalize_fact_items

        items = normalize_fact_items([], fallback_text="", policy="placeholder")
        schedule = next(i for i in items if i["key"] == "scheduleDuration")
        assert schedule["value"] == "【待填写】"
        assert schedule["needs_manual_input"] is True

    def test_global_fact_model_keeps_needs_manual_input(self):
        """前后端字段契约：GlobalFact 必须带该字段，否则 model_dump 重生 JSON 时被丢弃。"""
        from app.models import GlobalFact

        fact = GlobalFact(key="k", value="【待填写】", needs_manual_input=True)
        assert fact.model_dump()["needs_manual_input"] is True
        # 默认值为 False，旧数据（不含该键）仍可正常校验
        assert GlobalFact(key="k", value="v").needs_manual_input is False


# ============================================================
# C. 存储读写口径（写入方 ↔ 读取方）
# ============================================================


class TestStoredFactsContract:
    def test_parse_flat_list(self):
        from app.services.facts_service import parse_stored_items

        raw = json.dumps([{"key": "a", "value": "1"}], ensure_ascii=False)
        assert parse_stored_items(raw) == [{"key": "a", "value": "1"}]

    def test_parse_wrapped_items_form(self):
        """BUG 回归：{"items": [...]} 包装曾被静默忽略 → 正文不带事实生成。"""
        from app.services.facts_service import parse_stored_items

        assert parse_stored_items({"items": [{"key": "a"}]}) == [{"key": "a"}]
        assert parse_stored_items(json.dumps({"items": [{"key": "b"}]})) == [{"key": "b"}]

    def test_parse_bad_payloads_degrade_to_empty(self):
        from app.services.facts_service import parse_stored_items

        for bad in (None, "", "not json", b"\xff\xfe", 123, json.dumps("纯字符串"), {"foo": 1}):
            assert parse_stored_items(bad) == [], f"{bad!r} 应降级为空列表而不是抛错"

    def test_facts_for_generation_drops_placeholders(self):
        from app.services.facts_service import facts_for_generation

        raw = json.dumps(
            [
                {"key": "k1", "value": "180天"},
                {"key": "k2", "value": "【待填写】"},
                {"key": "k3", "value": "【待填写：技术负责人姓名】"},
            ],
            ensure_ascii=False,
        )
        assert [f["key"] for f in facts_for_generation(raw)] == ["k1"]

    def test_sse_reader_matches_service_filter(self):
        """SSE 读取器与服务层入口对同一份 JSON 必须给出完全相同的结果集。"""
        from app.routers.sse.sse_utils import _parse_global_facts_items
        from app.services.facts_service import facts_for_generation

        raw = json.dumps(
            {
                "items": [
                    {"key": "k1", "value": "180天"},
                    {"key": "k2", "value": "【待填写：姓名】"},
                ]
            },
            ensure_ascii=False,
        )
        assert _parse_global_facts_items(raw) == facts_for_generation(raw)

    def test_dump_items_keeps_chinese_readable(self):
        from app.services.facts_service import dump_items, parse_stored_items

        text = dump_items([{"key": "工期", "value": "450 日历天"}])
        assert "日历天" in text and "\\u" not in text
        assert parse_stored_items(text)[0]["value"] == "450 日历天"

    def test_repair_reader_shares_baseline(self):
        """一致性修复链路也必须剔除占位，否则把【待填写】当事实写回正文。"""
        from app.services import consistency_repair as cr
        from app.services.facts_service import facts_for_generation

        assert cr.facts_for_generation is facts_for_generation


# ============================================================
# D. 去重与值归一化
# ============================================================


class TestDedupAndNormalization:
    def test_dedup_keeps_first_but_real_value_wins_over_placeholder(self):
        """占位在前、真值在后时必须补强，否则 AI 输出顺序不稳会随机丢事实。"""
        from app.services.facts_service import dedup_facts

        merged = dedup_facts(
            [
                {"key": "scheduleDuration", "value": "【待填写】", "category": "schedule"},
                {"key": "scheduleDuration", "value": "450 日历天", "category": "schedule"},
            ]
        )
        assert len(merged) == 1
        assert merged[0]["value"] == "450 日历天"
        assert not merged[0].get("needs_manual_input")

    def test_dedup_drops_empty_keys(self):
        from app.services.facts_service import dedup_facts

        assert dedup_facts([{"value": "无主值"}, "非字典", {"key": "k", "value": "v"}]) == [
            {"key": "k", "value": "v"}
        ]

    def test_normalize_value_ignores_typography_only(self):
        from app.services.facts_service import normalize_fact_value

        assert normalize_fact_value("450日历天") == normalize_fact_value("450 日历天")
        assert normalize_fact_value("450天") == normalize_fact_value("４５０天"), "全角转半角"
        assert normalize_fact_value("零死亡事故。") == normalize_fact_value("零死亡 事故")
        assert normalize_fact_value("") == ""

    def test_values_conflict_only_when_truly_different(self):
        from app.services.facts_service import values_conflict

        assert values_conflict(["450 天", "450天"]) is False
        assert values_conflict(["450 天", "500 天"]) is True
        assert values_conflict(["零死亡事故", "无重伤事故"]) is True
        assert values_conflict([]) is False

    def test_resolve_category_keeps_explicit_and_infers_missing(self):
        """分类缺失不得塌成 basic：basic 会掉出前端分组与审计基准匹配。"""
        from app.services.facts_service import resolve_category

        # 显式合法值（含中文别名）保持原义，不被推断覆盖
        assert resolve_category("schedule", "anyKey", "任意") == "schedule"
        assert resolve_category("工期", "anyKey", "任意") == "schedule"
        assert resolve_category("basic", "projectName", "项目名称") == "basic"
        # 空值/未知值按 key + label 兜底
        assert resolve_category("", "scheduleDuration", "建设工期") == "schedule"
        assert resolve_category(None, "projectManager", "项目经理") == "personnel"
        assert resolve_category("乱七八糟", "safetyTarget", "安全目标") == "safety"
        assert resolve_category("", "unknownKey", "未知事实") == "basic"

    def test_normalize_items_recovers_category(self):
        """AI 只给 key/value 时，落库前必须把分类补上（同一口径走正本）。"""
        from app.services.facts_service import normalize_fact_items

        items = normalize_fact_items([{"key": "qualityTarget", "label": "质量目标", "value": "合格"}])
        assert items[0]["category"] == "quality"


# ============================================================
# E. 路由与接口（Tab 级：GET / POST / generate）
# ============================================================


class TestFactsRouteContracts:
    def test_get_facts_ships_categories_and_last_audit(self):
        from app.routers import facts as facts_router
        from app.services.facts_service import FACT_CATEGORY_LABELS

        pid = _new_project("事实接口-GET")
        _seed_analysis(pid, [{"key": "k1", "value": "180天", "category": "schedule"}])
        with get_db() as conn:
            conn.execute(
                "UPDATE analysis SET facts_audit_json = ?, facts_audit_at = ? WHERE project_id = ?",
                (json.dumps({"items": [{"fact_key": "k1", "status": "missing"}]}, ensure_ascii=False),
                 "2026-09-29 10:00:00", pid),
            )

        result = asyncio.run(facts_router.get_facts(pid))
        assert result["categories"] == FACT_CATEGORY_LABELS
        assert result["placeholder_count"] == 0
        # 旧实现审计只在前端内存里，刷新即丢 → 现在从库里回看
        assert result["last_audit"]["items"][0]["status"] == "missing"
        assert result["audited_at"] == "2026-09-29 10:00:00"

    def test_get_facts_without_analysis_row_is_three_keys(self):
        """向后兼容：无 analysis 行时返回值严格保持 3 键（旧前端依赖该形状）。"""
        from app.routers import facts as facts_router

        pid = _new_project("事实接口-无行")
        assert asyncio.run(facts_router.get_facts(pid)) == {
            "items": [],
            "confirmed": False,
            "missing_value_policy": "fabricate",
        }

    def test_post_facts_recomputes_manual_input_flag_from_value(self):
        """以 value 为准：填成真值就清角标；仍是占位则保留标记。"""
        from app.models import GlobalFact, GlobalFactList
        from app.routers import facts as facts_router

        pid = _new_project("事实接口-POST")
        body = GlobalFactList(
            items=[
                GlobalFact(key="pm", label="项目经理", value="张工", needs_manual_input=True),
                GlobalFact(key="chief", label="技术负责人", value="【待填写】"),
            ]
        )
        saved = asyncio.run(facts_router.save_facts(pid, body))
        assert saved["ok"] is True
        assert saved["fact_conflicts"] == []

        with get_db() as conn:
            row = conn.execute(
                "SELECT global_facts_json FROM analysis WHERE project_id = ?", (pid,)
            ).fetchone()
        stored = json.loads(row["global_facts_json"])
        by_key = {i["key"]: i for i in stored}
        assert "needs_manual_input" not in by_key["pm"], "真值上的旧角标必须清掉"
        assert by_key["chief"]["needs_manual_input"] is True

    def test_post_facts_recovers_category_when_client_omits_it(self):
        """前后端契约：不传 category 时不得写成 basic（否则审计基准与分组双双丢）。"""
        from app.models import GlobalFact, GlobalFactList
        from app.routers import facts as facts_router

        pid = _new_project("事实接口-分类兜底")
        body = GlobalFactList(
            items=[GlobalFact(key="scheduleDuration", label="建设工期", value="工期：450天")]
        )
        saved = asyncio.run(facts_router.save_facts(pid, body))
        assert saved["ok"] is True
        with get_db() as conn:
            row = conn.execute(
                "SELECT global_facts_json FROM analysis WHERE project_id = ?", (pid,)
            ).fetchone()
        stored = json.loads(row["global_facts_json"])
        assert stored[0]["category"] == "schedule"

        from app.services.consistency_auditor import ConsistencyAuditor

        _seed_section(pid, "第一章", "本章工期：500天。", 0)
        conflicts = [c for c in ConsistencyAuditor().detect_conflicts(pid) if c.fact_name == "工期"]
        assert conflicts and conflicts[0].values[0] == "450", "保存后的事实仍须参入审计基准"

    def test_post_facts_invalidates_stored_audit(self):
        """缓存未失效回归：全量覆写事实后，旧审计结果不得再被 GET 回看。"""
        from app.models import GlobalFact, GlobalFactList
        from app.routers import facts as facts_router

        pid = _new_project("事实接口-审计失效")
        _seed_analysis(pid, [{"key": "k1", "value": "旧值", "category": "schedule"}])
        with get_db() as conn:
            conn.execute(
                "UPDATE analysis SET facts_audit_json = ?, facts_audit_at = ? WHERE project_id = ?",
                ('{"items": []}', "2026-09-29 09:00:00", pid),
            )

        asyncio.run(facts_router.save_facts(pid, GlobalFactList(items=[GlobalFact(key="k1", value="新值")])))
        with get_db() as conn:
            row = conn.execute(
                "SELECT facts_audit_json, facts_audit_at FROM analysis WHERE project_id = ?", (pid,)
            ).fetchone()
        assert row["facts_audit_json"] == ""
        assert row["facts_audit_at"] is None
        assert asyncio.run(facts_router.get_facts(pid))["last_audit"] is None

    def test_generate_invalidates_stored_audit_too(self, monkeypatch):
        from app.models import FactsGenerateRequest
        from app.routers import facts as facts_router

        pid = _new_project("事实接口-提取失效")
        _seed_analysis(pid, [{"key": "k1", "value": "旧值"}])
        with get_db() as conn:
            conn.execute(
                "UPDATE analysis SET facts_audit_json = ?, facts_audit_at = ? WHERE project_id = ?",
                ('{"items": []}', "2026-09-29 09:00:00", pid),
            )
        _patch_facts_ai(monkeypatch, [{"key": "projectName", "label": "项目名称", "value": "新工程"}])

        asyncio.run(facts_router.generate_facts(pid, FactsGenerateRequest()))
        with get_db() as conn:
            row = conn.execute("SELECT facts_audit_json FROM analysis WHERE project_id = ?", (pid,)).fetchone()
        assert row["facts_audit_json"] == ""

    @pytest.mark.parametrize("endpoint", ["save_facts", "generate_facts"])
    def test_write_endpoints_reject_while_generating(self, monkeypatch, endpoint):
        """并发竞态防护：正文生成进行中改事实 → 409，与 sections 路由同口径。"""
        from fastapi import HTTPException

        from app.models import FactsGenerateRequest, GlobalFact, GlobalFactList
        from app.routers import facts as facts_router
        from app.services import task_lock

        pid = _new_project(f"事实接口-生成中-{endpoint}")
        lock = task_lock.acquire("content-generation", project_id=pid)
        try:
            if endpoint == "save_facts":
                call = facts_router.save_facts(pid, GlobalFactList(items=[GlobalFact(key="k", value="v")]))
            else:
                _patch_facts_ai(monkeypatch, [{"key": "projectName", "label": "项目名称", "value": "X"}])
                call = facts_router.generate_facts(pid, FactsGenerateRequest())
            with pytest.raises(HTTPException) as exc_info:
                asyncio.run(call)
            assert exc_info.value.status_code == 409
            assert "正文生成" in exc_info.value.detail, "提示需带占用任务中文名，便于用户等待"
        finally:
            task_lock.release(lock["lock_key"], lock["owner_id"])

    def test_lock_from_another_project_does_not_block(self):
        from app.models import GlobalFact, GlobalFactList
        from app.routers import facts as facts_router
        from app.services import task_lock

        pid = _new_project("事实接口-他项目锁")
        other = _new_project("事实接口-他项目锁B")
        lock = task_lock.acquire("content-generation", project_id=other)
        try:
            saved = asyncio.run(
                facts_router.save_facts(pid, GlobalFactList(items=[GlobalFact(key="k", value="v")]))
            )
            assert saved["ok"] is True
        finally:
            task_lock.release(lock["lock_key"], lock["owner_id"])


def _patch_facts_ai(monkeypatch, ai_payload):
    """把 /facts/generate 依赖的 AI 与文件读取替换成假实现，并记录 system 提示词。"""
    from app.routers import facts as facts_router
    from app.services.ai import provider_factory, workflows

    monkeypatch.setattr(
        facts_router, "get_facts_file_text", lambda _pid: "招标文件正文：工期要求 450 日历天。"
    )
    monkeypatch.setattr(provider_factory, "get_ai_config", lambda: object())
    monkeypatch.setattr(provider_factory, "create_provider", lambda _cfg: object())

    captured: dict = {}

    async def _fake_collect(_provider, messages, **_kwargs):
        captured["system"] = str(getattr(messages[0], "content", ""))
        captured["user"] = str(getattr(messages[1], "content", ""))
        return ai_payload

    monkeypatch.setattr(workflows, "collect_json_response", _fake_collect)
    return captured


def _patch_audit_ai(monkeypatch, responder, *, group_char_limit: int | None = None):
    """把 `/facts/audit` 的 AI 与装箱预算换成可编程假实现。

    Args:
        responder: 收到批号（从 1 起），返回 AI 结果或抛异常（模拟该组失败）。
        group_char_limit: 强制每组只放一章，用于造「多组并发审计」场景。

    Returns:
        调用记录列表，每项 {"index": 批号, "user": 送审 prompt}。
    """
    from app.services.ai import provider_factory, workflows
    from app.services.ai import workflows_expand as wf

    monkeypatch.setattr(provider_factory, "get_ai_config", lambda: object())
    monkeypatch.setattr(provider_factory, "create_provider", lambda _cfg: object())
    # 审计端点在函数体内 import 这些名字，因此运行时读到的是此处打补丁后的值
    monkeypatch.setattr(wf, "AUDIT_SECTION_CHAR_LIMIT", 5000)
    if group_char_limit is not None:
        monkeypatch.setattr(wf, "AUDIT_GROUP_CHAR_LIMIT", group_char_limit)

    calls: list[dict] = []

    async def _fake_collect(_provider, messages, **_kwargs):
        user = str(getattr(messages[1], "content", ""))
        matched = re.search(r"第 (\d+) 批", user)
        index = int(matched.group(1)) if matched else 1
        calls.append({"index": index, "user": user})
        return responder(index)

    monkeypatch.setattr(workflows, "collect_json_response", _fake_collect)
    return calls


# ============================================================
# F. 一致性审计端点（多组装箱 / 降级 / 落库）
# ============================================================


class TestFactsAuditEndpoint:
    def _seed(self, name: str, items: list, contents: list[str]) -> int:
        pid = _new_project(name)
        _seed_analysis(pid, items)
        for i, content in enumerate(contents):
            _seed_section(pid, f"第{i + 1}章", content, sort_order=i)
        return pid

    def test_placeholder_facts_never_reach_the_prompt(self, monkeypatch):
        """占位不得送审：否则「正文里没有【待填写】」被报成 missing 假阳。"""
        from app.routers import facts as facts_router

        pid = self._seed(
            "事实审计-占位跳过",
            [
                {"key": "k1", "label": "工期", "value": "450天", "category": "schedule"},
                {"key": "k2", "label": "项目经理", "value": "【待填写】", "category": "personnel"},
                {"key": "k3", "label": "质量标准", "value": "合格", "category": "quality"},
            ],
            ["工期 450 天，质量合格。"],
        )
        calls = _patch_audit_ai(
            monkeypatch,
            lambda idx: [{"fact_key": "k1", "status": "consistent"}],
        )

        result = asyncio.run(facts_router.audit_facts_consistency(pid))
        assert result["total_facts"] == 2
        assert result["skipped_placeholder"] == 1
        assert "【待填写】" not in calls[0]["user"]
        assert "项目经理" not in calls[0]["user"]

    def test_audit_covers_all_sections_not_first_five(self, monkeypatch):
        """覆盖率回归：旧实现只取前 5 章 × 每章 500 字，十万字标书查不到 3%。"""
        from app.routers import facts as facts_router

        pid = self._seed(
            "事实审计-全覆盖",
            [{"key": "k1", "label": "工期", "value": "450天", "category": "schedule"}],
            [f"第 {i} 章正文，工期 450 天。" * 4 for i in range(8)],
        )
        calls = _patch_audit_ai(
            monkeypatch,
            lambda idx: [{"fact_key": "k1", "status": "consistent"}],
            group_char_limit=10,
        )

        result = asyncio.run(facts_router.audit_facts_consistency(pid))
        assert result["total_sections"] == 8
        assert result["audit_groups"] == 8, "每章一组，不得静默丢弃后面的章节"
        assert result["audited_sections"] == 8
        assert sorted(c["index"] for c in calls) == list(range(1, 9))

    def test_multi_group_merge_keeps_worst_status(self, monkeypatch):
        """同一事实多组上报异结论时取最严（inconsistent > missing > consistent）。"""
        from app.routers import facts as facts_router

        pid = self._seed(
            "事实审计-取最严",
            [{"key": "k1", "label": "工期", "value": "450天", "category": "schedule"}],
            ["工期 450 天。", "工期 450 天，另有表述冲突。"],
        )
        statuses = {1: "consistent", 2: "missing"}
        _patch_audit_ai(
            monkeypatch,
            lambda idx: [{"fact_key": "k1", "status": statuses[idx], "detail": f"批{idx}"}],
            group_char_limit=10,
        )

        items = asyncio.run(facts_router.audit_facts_consistency(pid))["items"]
        assert len(items) == 1, "同一 fact_key 不得在报告里重复出现多次"
        assert items[0]["status"] == "missing"

    def test_same_status_details_are_merged(self, monkeypatch):
        """同状态的多组定位信息拼接保留，否则用户只看到其中一处的章节号。"""
        from app.routers import facts as facts_router

        pid = self._seed(
            "事实审计-拼接定位",
            [{"key": "k1", "label": "工期", "value": "450天", "category": "schedule"}],
            ["工期 450 天。", "工期 500 天。"],
        )
        _patch_audit_ai(
            monkeypatch,
            lambda idx: [{"fact_key": "k1", "status": "inconsistent", "detail": f"第{idx}批发现矛盾"}],
            group_char_limit=10,
        )

        items = asyncio.run(facts_router.audit_facts_consistency(pid))["items"]
        assert "第1批发现矛盾" in items[0]["detail"]
        assert "第2批发现矛盾" in items[0]["detail"]

    def test_partial_group_failure_is_reported(self, monkeypatch):
        """单组异常不整体退回：旧实现 except 后回 []，前端绿字「检查通过」。"""
        from app.routers import facts as facts_router

        pid = self._seed(
            "事实审计-部分失败",
            [{"key": "k1", "label": "工期", "value": "450天", "category": "schedule"}],
            ["工期 450 天。", "工期 450 天。"],
        )

        def _responder(idx: int):
            if idx == 1:
                raise TimeoutError("AI 超时")
            return [{"fact_key": "k1", "status": "inconsistent", "detail": "第2章写 500 天"}]

        _patch_audit_ai(monkeypatch, _responder, group_char_limit=10)
        result = asyncio.run(facts_router.audit_facts_consistency(pid))
        assert result["audit_status"] == "partial"
        assert result["failed_groups"] == 1
        assert result["items"], "其余组结果应照常合入，不因一组失败丢全部"

    def test_total_failure_marks_failed_not_ok(self, monkeypatch):
        from app.routers import facts as facts_router

        pid = self._seed(
            "事实审计-全部失败",
            [{"key": "k1", "label": "工期", "value": "450天", "category": "schedule"}],
            ["工期 450 天。", "工期 450 天。"],
        )

        def _responder(_idx: int):
            raise TimeoutError("AI 不可用")

        _patch_audit_ai(monkeypatch, _responder, group_char_limit=10)
        result = asyncio.run(facts_router.audit_facts_consistency(pid))
        assert result["audit_status"] == "failed"
        assert result["failed_groups"] == result["audit_groups"] == 2
        assert result["items"] == []

    def test_success_is_ok_and_persisted_for_reload(self, monkeypatch):
        """审计结果落库：刷新页面后 GET /facts 能回看历史（旧版审计只在内存里）。"""
        from app.routers import facts as facts_router

        pid = self._seed(
            "事实审计-落库回看",
            [{"key": "k1", "label": "工期", "value": "450天", "category": "schedule"}],
            ["工期 450 天。"],
        )
        _patch_audit_ai(
            monkeypatch,
            lambda idx: [{"fact_key": "k1", "status": "inconsistent", "detail": "写法不一致"}],
        )

        result = asyncio.run(facts_router.audit_facts_consistency(pid))
        assert result["audit_status"] == "ok"
        assert result["truncated"] is False

        refreshed = asyncio.run(facts_router.get_facts(pid))
        assert refreshed["last_audit"]["status"] == "ok"
        assert refreshed["last_audit"]["items"][0]["status"] == "inconsistent"
        # 与 HTTP 返回体同一字段集，否则回看的报告缺诊断信息
        assert refreshed["last_audit"]["audit_groups"] == result["audit_groups"]
        assert refreshed["audited_at"]

    def test_wrapped_ai_payload_is_unwrapped(self, monkeypatch):
        """模型用 {"items": [...]} 包装时不得整体丢弃（旧版返回空数组 → 假通过）。"""
        from app.routers import facts as facts_router

        pid = self._seed(
            "事实审计-包装解包",
            [{"key": "k1", "label": "工期", "value": "450天", "category": "schedule"}],
            ["工期 450 天。"],
        )
        _patch_audit_ai(
            monkeypatch,
            lambda idx: {"items": [{"fact_key": "k1", "status": "missing", "detail": "未提及"}]},
        )

        items = asyncio.run(facts_router.audit_facts_consistency(pid))["items"]
        assert items[0]["status"] == "missing"

    @pytest.mark.parametrize(
        ("case", "expect_detail"),
        [
            ("no_analysis", "未找到事实变量"),
            ("all_placeholder", "事实变量为空"),
            ("no_sections", "没有已完成的正文"),
        ],
    )
    def test_audit_rejects_unusable_inputs(self, monkeypatch, case, expect_detail):
        """错误处理：三类不可审计场景给明确 400，而不是静默返回「全部一致」。"""
        from fastapi import HTTPException

        from app.routers import facts as facts_router

        real = [{"key": "k1", "label": "工期", "value": "450天", "category": "schedule"}]
        if case == "no_analysis":
            pid = _new_project("事实审计-无分析")
            _seed_section(pid, "第一章", "工期 450 天。")
        elif case == "all_placeholder":
            pid = self._seed(
                "事实审计-全占位",
                [{"key": "k1", "label": "工期", "value": "【待填写】", "category": "schedule"}],
                ["正文若干。"],
            )
        else:
            pid = self._seed("事实审计-无正文", real, [])
        _patch_audit_ai(monkeypatch, lambda idx: [])

        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(facts_router.audit_facts_consistency(pid))
        assert exc_info.value.status_code == 400
        assert expect_detail in exc_info.value.detail


# ============================================================
# G. 与 consistency_auditor 的数据链（全局事实作为审计基准）
# ============================================================


class TestConsistencyAuditorFactsBaseline:
    def _audit(self, pid: int):
        from app.services.consistency_auditor import ConsistencyAuditor

        return ConsistencyAuditor().detect_conflicts(pid)

    def _seed_project(self, name: str, facts: list, contents: list[str]) -> int:
        pid = _new_project(name)
        _seed_analysis(pid, facts)
        for i, content in enumerate(contents):
            _seed_section(pid, f"第{i + 1}章", content, sort_order=i)
        return pid

    def test_baseline_takes_part_in_conflict_detection(self):
        """双轨制回归：审计器此前不读 global_facts，正文写错时只拿两个错值互比。"""
        pid = self._seed_project(
            "审计器-基准入局",
            [{"key": "scheduleDuration", "label": "建设工期", "value": "工期：450天", "category": "schedule"}],
            ["本章工期：500天。", "本章工期：500天。"],
        )
        conflicts = [c for c in self._audit(pid) if c.fact_name == "工期"]
        assert conflicts, "正文与已确认基准不一致时必须报冲突"
        assert conflicts[0].values[0] == "450", "基准值排在最前，供修复建议优先取用"
        assert conflicts[0].sources[0].startswith("global_facts")

    def test_typography_variants_do_not_create_false_conflict(self):
        """同义排版不得判冲突：旧版 `len(set(原文))>1` 会报一堆假阳性。"""
        pid = self._seed_project(
            "审计器-排版不误报",
            [{"key": "scheduleDuration", "label": "建设工期", "value": "工期：450天", "category": "schedule"}],
            ["本工程工期 450 天。", "工期：450天。"],
        )
        assert [c for c in self._audit(pid) if c.fact_name == "工期"] == []

    def test_safety_typography_variants_do_not_conflict(self):
        """归一化判等回归：抽取后的原文不同（空格/全角/句号）但语义同一。

        上例的工期抽取值本来就是裸数字，拿原文去重也不会误报；本例抽的是
        中文短语，只有按 `normalize_fact_value` 去重才能剔除假阳性。
        """
        pid = self._seed_project(
            "审计器-安全排版不误报",
            [
                {
                    "key": "safetyTarget",
                    "label": "安全目标",
                    "value": "安全生产目标：零死亡事故",
                    "category": "safety",
                }
            ],
            ["安全生产目标：零死亡 事故。", "安全生产目标：零死亡事故", "安全生产目标：零死亡事故。"],
        )
        assert [c for c in self._audit(pid) if c.fact_name == "安全标准"] == []

    def test_safety_sentence_without_label_is_not_a_conflict(self):
        """正则过宽回归：正文出现「安全」两字不应抓走整行当作安全标准。"""
        from app.services.consistency_auditor import ConsistencyAuditor

        auditor = ConsistencyAuditor()
        prose = "安全是施工的重中之重，必须坚持安全第一 预防为主，落实安全责任制。"
        assert [f for f in auditor.extract_facts_from_text(prose) if f.name == "安全标准"] == []

        pid = self._seed_project(
            "审计器-安全不误报",
            [{"key": "safetyTarget", "label": "安全目标", "value": "安全目标：零死亡事故", "category": "safety"}],
            [prose, prose],
        )
        assert [c for c in self._audit(pid) if c.fact_name == "安全标准"] == []

    def test_explicit_safety_labels_still_conflict(self):
        """收窄后仍要能抓到真冲突：带显式标签与冒号的写法。"""
        pid = self._seed_project(
            "审计器-安全真冲突",
            [{"key": "safetyTarget", "label": "安全目标", "value": "安全生产目标：零死亡", "category": "safety"}],
            ["安全生产目标：零死亡。", "安全生产目标：无重伤事故。"],
        )
        conflicts = [c for c in self._audit(pid) if c.fact_name == "安全标准"]
        assert conflicts
        assert set(conflicts[0].values) == {"零死亡", "无重伤事故"}

    def test_only_comparable_categories_are_injected(self):
        """基线只纳入可与正则抽取直接比较的三类，人员类口径不同不注入。

        BUG-FIX 2026-10-04：人员事实按岗位分桶（项目经理/技术人员…各自独立），
        故两章「项目经理：5人 / 8人」的同岗位冲突落在 fact_name「项目经理数量」。
        本例仍验证 personnel 基线（10人）不得冒充正则抽取值。
        """
        pid = self._seed_project(
            "审计器-基线范畴",
            [{"key": "pmCount", "label": "项目经理人数", "value": "项目经理：10人", "category": "personnel"}],
            ["项目经理：5人。", "项目经理：8人。"],
        )
        conflicts = [c for c in self._audit(pid) if c.fact_name == "项目经理数量"]
        assert conflicts, "同岗位（项目经理）不同人数应报冲突"
        assert "10" not in conflicts[0].values, "personnel 基线不得冒充正则抽取值"

    def test_fix_suggestion_prefers_baseline_over_frequency(self):
        """修复建议以基准为准：旧版按频率投票，会把错值因章节多而“统一”为错值。"""
        from app.services.consistency_auditor import ConsistencyAuditor

        pid = self._seed_project(
            "审计器-建议取基准",
            [{"key": "scheduleDuration", "label": "建设工期", "value": "工期：450天", "category": "schedule"}],
            ["工期：500天。", "工期：500天。", "工期：500天。"],
        )
        auditor = ConsistencyAuditor()
        conflicts = [c for c in auditor.detect_conflicts(pid) if c.fact_name == "工期"]
        assert conflicts

        suggestion = auditor.suggest_fixes(conflicts[0].conflict_id, pid)
        assert suggestion is not None
        assert "统一为：450" in suggestion, "基准值 450 应胜过正文里票数更高的错值 500"
        assert "已确认的全局事实基准" in suggestion
        assert "500" in suggestion and "待修正取值" in suggestion


# ============================================================
# H. Step02 自动提取链路的策略透传
# ============================================================


class TestExtractGlobalFactsPolicyPropagation:
    def _run(self, monkeypatch, policy_kwargs):
        from app.services.ai import workflows_analysis as wa

        captured: dict = {}

        async def _fake_collect(_provider, messages, **_kwargs):
            captured["system"] = str(getattr(messages[0], "content", ""))
            captured["user"] = str(getattr(messages[1], "content", ""))
            # 缓存优化（文档四）后消息为 4 条：通用 system / 上下文块 /
            # 策略专属指令 / 补充来源。保留整个列表供断言按角色定位。
            captured["messages"] = [
                (str(getattr(m, "role", "")), str(getattr(m, "content", "")))
                for m in messages
            ]
            return [{"key": "projectName", "label": "项目名称", "value": "示范工程"}]

        monkeypatch.setattr(wa, "collect_json_response", _fake_collect)
        provider = object()
        result = asyncio.run(
            wa.extract_global_facts(provider, "招标文件正文若干", "某市政道路改造", **policy_kwargs)
        )
        return captured, result

    @pytest.mark.parametrize("policy", ["fabricate", "omit", "placeholder"])
    def test_system_prompt_follows_policy(self, monkeypatch, policy):
        """提示词正本共用：Step02 不得再写死「可模拟生成」一档。

        ENHANCE 2026-10-09（文档四·缓存前缀）：system 改为所有解析任务共享的
        通用前缀，策略专属提示词下移到任务消息。断言相应调整为「策略提示词
        必须出现在发给模型的消息中」——策略传播这一核心契约不变。
        """
        from app.services import facts_service
        from app.services.ai import workflows_analysis as wa

        captured, _result = self._run(monkeypatch, {"policy": policy})
        policy_prompt = facts_service.fact_extract_system_prompt(policy)
        bodies = [c for _role, c in captured["messages"]]

        assert captured["system"] == wa.ANALYSIS_SYSTEM, "system 应为解析类共享前缀"
        assert policy_prompt in bodies, "策略专属提示词必须发给模型（策略传播不得丢）"
        if policy == "omit":
            assert "一致性优先于具体性" in policy_prompt
        elif policy == "placeholder":
            assert "占位要集中" in policy_prompt
        else:
            assert "一致性优先于具体性" not in policy_prompt

    def test_missing_policy_argument_keeps_legacy_behaviour(self, monkeypatch):
        """向后兼容：旧调用方不传 policy 时行为与 fabricate 完全一致。"""
        from app.services import facts_service

        captured, _result = self._run(monkeypatch, {})
        bodies = [c for _role, c in captured["messages"]]
        assert facts_service.fact_extract_system_prompt("fabricate") in bodies

    @pytest.mark.parametrize(
        ("policy", "expect_value", "expect_flag"),
        [
            ("fabricate", "满足招标文件工期要求，按合同约定执行", False),
            ("omit", "按招标文件要求及合同约定执行", False),
            ("placeholder", "【待填写】", True),
        ],
    )
    def test_schedule_fallback_follows_policy(self, monkeypatch, policy, expect_value, expect_flag):
        """工期兜底缺值按策略生成，不得把用户选的 omit/placeholder 覆盖回 fabricate。"""
        _captured, result = self._run(monkeypatch, {"policy": policy})
        schedule = next(i for i in result if i["key"] == "scheduleDuration")
        assert schedule["value"] == expect_value
        assert schedule["source"] == "default"
        assert bool(schedule.get("needs_manual_input")) is expect_flag


# ============================================================
# I. 人工编辑与确认链路（ENHANCE 2026-10-04）
#   —— 同 key 重复拦截、空基准确认防御、旧库缺列审计落库降级、
#   —— 无基准时修复建议的目标值口径。
# ============================================================


class TestFactsManualEditingContracts:
    def test_post_facts_rejects_duplicate_keys(self):
        """同一 key 两行不同值：提取链路会 dedup 合并，手工保存必须显式拒绝，
        否则两条互斥事实同时入库并被逐章注入 prompt。"""
        from fastapi import HTTPException

        from app.models import GlobalFact, GlobalFactList
        from app.routers import facts as facts_router

        pid = _new_project("事实接口-同key重复")
        body = GlobalFactList(
            items=[
                GlobalFact(key="scheduleDuration", label="建设工期", value="450天"),
                GlobalFact(key="scheduleDuration", label="建设工期", value="500天"),
            ]
        )
        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(facts_router.save_facts(pid, body))
        assert exc_info.value.status_code == 400
        assert "scheduleDuration" in exc_info.value.detail
        # 拒绝必须是「写前校验」：库里不得残留半批数据
        from app.services.facts_service import parse_stored_items

        with get_db() as conn:
            row = conn.execute(
                "SELECT global_facts_json FROM analysis WHERE project_id = ?", (pid,)
            ).fetchone()
        assert row is None or parse_stored_items(row["global_facts_json"]) == [], "400 分支不得落库"

    def test_post_facts_allows_distinct_manual_rows(self):
        """边界：人工补录行只要 key 唯一即可保存，不得被重复校验误杀。"""
        from app.models import GlobalFact, GlobalFactList
        from app.routers import facts as facts_router

        pid = _new_project("事实接口-人工补录")
        body = GlobalFactList(
            items=[
                GlobalFact(key="remarkA", label="备注A", value="甲", source="manual"),
                GlobalFact(key="remarkB", label="备注B", value="乙", source="manual"),
            ]
        )
        saved = asyncio.run(facts_router.save_facts(pid, body))
        assert saved["ok"] is True

    def test_post_facts_rejects_missing_key(self):
        """空标识写入侧显式拒绝：无 key 的行会在「重新提取」时被
        dedup_facts 静默丢弃，两条链路行为分叉比拒绝更槽。"""
        from fastapi import HTTPException

        from app.models import GlobalFact, GlobalFactList
        from app.routers import facts as facts_router

        pid = _new_project("事实接口-空标识")
        body = GlobalFactList(items=[GlobalFact(key="  ", label="备注", value="甲")])
        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(facts_router.save_facts(pid, body))
        assert exc_info.value.status_code == 400
        assert "标识" in exc_info.value.detail

    def test_confirm_rejects_empty_fact_set(self):
        """空事实集不得被置为「已确认」：下游（评分流程保护/审计基准）
        都以 confirmed_facts 为开关，空基准会锁住整条事实链。"""
        from fastapi import HTTPException

        from app.routers import facts as facts_router

        pid = _new_project("事实确认-空集")
        _seed_analysis(pid, [])
        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(facts_router.confirm_facts(pid))
        assert exc_info.value.status_code == 400
        assert "没有可确认" in exc_info.value.detail
        with get_db() as conn:
            confirmed = conn.execute(
                "SELECT confirmed_facts FROM analysis WHERE project_id = ?", (pid,)
            ).fetchone()["confirmed_facts"]
        assert not confirmed

    def test_confirm_rejects_all_placeholder_set_but_accepts_partial(self):
        """全占位拒绝；含占位仍允许确认（placeholder 策略下补值前确认是现有
        UI 流程，行为向后兼容，提醒责任在 GET /facts 的 placeholder_count）。"""
        from fastapi import HTTPException

        from app.routers import facts as facts_router

        pid_all = _new_project("事实确认-全占位")
        _seed_analysis(
            pid_all,
            [{"key": "k1", "label": "工期", "value": "【待填写】", "category": "schedule"}],
        )
        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(facts_router.confirm_facts(pid_all))
        assert exc_info.value.status_code == 400

        pid_mix = _new_project("事实确认-含占位")
        _seed_analysis(
            pid_mix,
            [
                {"key": "k1", "label": "工期", "value": "450天", "category": "schedule"},
                {"key": "k2", "label": "项目经理", "value": "【待填写】", "category": "personnel"},
            ],
        )
        assert asyncio.run(facts_router.confirm_facts(pid_mix)) == {"ok": True}
        with get_db() as conn:
            row = conn.execute(
                "SELECT confirmed_facts FROM analysis WHERE project_id = ?", (pid_mix,)
            ).fetchone()
        assert bool(row["confirmed_facts"])

    def test_audit_persist_degrades_when_columns_missing(self):
        """旧库未跑 facts_audit_* 迁移：审计仍返回结果，只是不落库，不抛错。

        GET /facts 早就按列名集合降级，写入侧此前是裸 INSERT；本例钉住
        「读写两侧的缺列容忍对称」这一修复。
        """
        from app.routers import facts as facts_router

        pid = _new_project("事实审计-缺列降级")
        with get_db() as conn:
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(analysis)").fetchall()}
        if {"facts_audit_json", "facts_audit_at"} <= cols:
            # 只验证「无列时不落库不报错」这一个分支；临时造一张缺列同构表。
            with get_db() as conn:
                conn.execute("ALTER TABLE analysis RENAME TO analysis_with_audit")
                conn.execute(
                    "CREATE TABLE analysis (project_id INTEGER PRIMARY KEY, "
                    "global_facts_json TEXT DEFAULT '[]', confirmed_facts INTEGER DEFAULT 0, "
                    "facts_policy TEXT DEFAULT 'fabricate')"
                )
            try:
                assert facts_router._persist_audit(pid, {"status": "ok"}) is False
            finally:
                with get_db() as conn:
                    conn.execute("DROP TABLE analysis")
                    conn.execute("ALTER TABLE analysis_with_audit RENAME TO analysis")

    def test_auditor_suggestion_prefers_summary_when_no_baseline(self):
        """无基准时：摘要口径（招标资料）优先于纯频率投票。

        正文错值散布多章时，频率投票会把错值投成目标值；摘要非空时
        必须拿摘要值，摘要为空才回退频率（兼容旧行为）。
        """
        from app.services.consistency_auditor import ConsistencyAuditor

        pid = _new_project("审计器-无基准优先摘要")
        with get_db() as conn:
            conn.execute("UPDATE projects SET notes = ? WHERE id = ?", ("工期：300天", pid))
        _seed_analysis(pid, [])
        for i, content in enumerate(["本章工期：450天。", "本章工期：450天。", "本章工期：300天。"]):
            _seed_section(pid, f"第{i + 1}章", content, sort_order=i)

        auditor = ConsistencyAuditor()
        conflicts = [c for c in auditor.detect_conflicts(pid) if c.fact_name == "工期"]
        assert conflicts
        suggestion = auditor.suggest_fixes(conflicts[0].conflict_id, pid)
        assert suggestion is not None
        assert "统一为：300" in suggestion, "摘要口径 300 应压过正文票数更高的 450"


# ============================================================
# J. 增强项测试（ENHANCE 2026-10-06）
#   —— 保存/重新提取后正文缓存失效（设计文档 8.1）、事实集内部互斥矛盾检测、
#   —— 基线事实每章必注入、超长/特殊字符往返、Tab 级（HTTP）端到端交互。
# ============================================================


class TestFactsCacheInvalidationOnChange:
    """设计文档 8.1「保存后自动清空旧正文生成缓存」契约的回归钉。

    事实基准全量覆写后若不失效正文缓存，用户改完事实重生成仍命中旧章节，
    造成「改了事实但正文前后仍不一致」的隐性回归。
    """

    def test_save_facts_invalidates_content_cache(self, monkeypatch):
        import app.routers.facts as fr
        from app.models import FactsGenerateRequest, GlobalFact, GlobalFactList

        calls: list[int] = []
        monkeypatch.setattr(fr, "invalidate_content_caches", lambda pid: calls.append(pid))

        pid = _new_project("事实-缓存失效-保存")
        body = GlobalFactList(
            items=[GlobalFact(key="k1", label="L1", value="v1", source="manual")]
        )
        saved = asyncio.run(fr.save_facts(pid, body))
        assert saved["ok"] is True
        assert calls == [pid], "保存必须触发正文缓存失效"

    def test_generate_facts_invalidates_content_cache(self, monkeypatch):
        import app.routers.facts as fr
        from app.models import FactsGenerateRequest
        from app.services.ai import provider_factory, workflows

        calls: list[int] = []
        monkeypatch.setattr(fr, "invalidate_content_caches", lambda pid: calls.append(pid))
        monkeypatch.setattr(fr, "get_facts_file_text", lambda _pid: "工期要求 450 日历天。")
        monkeypatch.setattr(provider_factory, "get_ai_config", lambda: object())
        monkeypatch.setattr(provider_factory, "create_provider", lambda _cfg: object())

        async def _fake(_provider, _messages, **_kwargs):
            return {"projectName": "示范工程"}

        monkeypatch.setattr(workflows, "collect_json_response", _fake)

        pid = _new_project("事实-缓存失效-提取")
        asyncio.run(
            fr.generate_facts(pid, FactsGenerateRequest(missing_value_policy="fabricate"))
        )
        assert calls == [pid], "重新提取必须触发正文缓存失效"


class TestFactsInternalConflictDetection:
    """事实集内部互斥取值矛盾检测（模块此前只做跨章节审计，从不在自身查矛盾）。"""

    def test_group_fact_conflicts_same_label_diff_value(self):
        from app.services.facts_service import group_fact_conflicts

        items = [
            {"key": "projectManager", "label": "项目经理", "value": "张伟", "category": "personnel"},
            {"key": "pm2", "label": "项目经理", "value": "李强", "category": "personnel"},
        ]
        conflicts = group_fact_conflicts(items)
        assert len(conflicts) == 1
        assert conflicts[0]["label"] == "项目经理"
        assert {v["value"] for v in conflicts[0]["values"]} == {"张伟", "李强"}

    def test_group_fact_conflicts_ignores_placeholder(self):
        from app.services.facts_service import group_fact_conflicts

        items = [
            {"key": "projectManager", "label": "项目经理", "value": "张伟"},
            {"key": "pm2", "label": "项目经理", "value": "【待填写：项目经理姓名】"},
        ]
        # 占位值不得与真实值互比，否则人工补录流程会无端报矛盾
        assert group_fact_conflicts(items) == []

    def test_group_fact_conflicts_typography_not_false_positive(self):
        from app.services.facts_service import group_fact_conflicts

        items = [
            {"key": "sd", "label": "建设工期", "value": "450 日历天"},
            {"key": "sd2", "label": "建设工期", "value": "450天"},
        ]
        # 同义排版（空格差异）归一化后应判为同一值，不报矛盾
        assert group_fact_conflicts(items) == []

    def test_get_facts_returns_internal_conflicts(self, monkeypatch):
        import app.routers.facts as fr
        from app.models import GlobalFact, GlobalFactList

        monkeypatch.setattr(fr, "invalidate_content_caches", lambda pid: None)

        pid = _new_project("事实-内部矛盾")
        body = GlobalFactList(
            items=[
                GlobalFact(key="projectManager", label="项目经理", value="张伟", source="ai"),
                GlobalFact(key="pm2", label="项目经理", value="李强", source="manual"),
            ]
        )
        asyncio.run(fr.save_facts(pid, body))
        result = asyncio.run(fr.get_facts(pid, None))
        assert len(result["fact_conflicts"]) == 1
        assert result["fact_conflicts"][0]["label"] == "项目经理"

    def test_generate_surfaces_fact_conflicts_key(self, monkeypatch):
        import app.routers.facts as fr
        from app.models import FactsGenerateRequest
        from app.services.ai import provider_factory, workflows

        monkeypatch.setattr(fr, "invalidate_content_caches", lambda pid: None)
        monkeypatch.setattr(fr, "get_facts_file_text", lambda _pid: "工期要求 450 日历天。")
        monkeypatch.setattr(provider_factory, "get_ai_config", lambda: object())
        monkeypatch.setattr(provider_factory, "create_provider", lambda _cfg: object())

        async def _fake(_provider, _messages, **_kwargs):
            return {"projectName": "示范工程"}

        monkeypatch.setattr(workflows, "collect_json_response", _fake)

        pid = _new_project("事实-提取矛盾键")
        result = asyncio.run(
            fr.generate_facts(pid, FactsGenerateRequest(missing_value_policy="fabricate"))
        )
        # 契约：提取响应也必须回传 fact_conflicts（便于前端即时提示）
        assert "fact_conflicts" in result
        assert result["fact_conflicts"] == []


class TestFactsBaselineInjection:
    """基线事实（项目名称/建设地点/总工期）是每章都要用的唯一事实源，
    必须无论章节标题是否命中都被注入（select_facts_for_section 契约）。"""

    def test_core_baselines_injected_for_unrelated_section(self):
        from app.services.facts_service import select_facts_for_section

        facts = [
            {"key": "projectName", "label": "项目名称", "value": "X市政", "category": "basic"},
            {"key": "constructionLocation", "label": "建设地点", "value": "城东", "category": "basic"},
            {"key": "scheduleDuration", "label": "建设工期", "value": "450天", "category": "schedule"},
            {"key": "craneModel", "label": "塔吊型号", "value": "QTZ63", "category": "technical"},
        ]
        picked = select_facts_for_section(facts, "质量管理体系", "")
        keys = [f["key"] for f in picked]
        # 与本章无关的弱相关技术事实可排后，但三条基线必须命中
        assert {"projectName", "constructionLocation", "scheduleDuration"} <= set(keys)

    def test_baselines_come_before_weak_technical_match(self):
        from app.services.facts_service import select_facts_for_section

        facts = [
            {"key": "projectName", "label": "项目名称", "value": "X市政", "category": "basic"},
            {"key": "scheduleDuration", "label": "建设工期", "value": "450天", "category": "schedule"},
            {"key": "craneModel", "label": "塔吊型号", "value": "QTZ63", "category": "technical"},
        ]
        picked = select_facts_for_section(facts, "混凝土施工方案", "")
        idx = {f["key"]: i for i, f in enumerate(picked)}
        # 基线排在被零散命中的技术事实之前（避免早期章节被弱相关事实占满预算）
        assert idx["projectName"] < idx["craneModel"]
        assert idx["scheduleDuration"] < idx["craneModel"]


class TestFactsEdgeValueHandling:
    """空数据/超长内容/特殊字符的健壮性：不得截断、转义破坏或误打占位标。"""

    def test_super_long_and_special_chars_roundtrip(self, monkeypatch):
        import app.routers.facts as fr
        from app.models import GlobalFact, GlobalFactList

        monkeypatch.setattr(fr, "invalidate_content_caches", lambda pid: None)

        pid = _new_project("事实-超长特殊字符")
        long_value = (
            "特殊符号测试①℃±×÷√【】『』\n\t\r" + '"' + "'" + "x" * 5000
        )
        body = GlobalFactList(
            items=[
                GlobalFact(key="specialNote", label="特殊说明", value=long_value, source="manual")
            ]
        )
        saved = asyncio.run(fr.save_facts(pid, body))
        # ENHANCE 2026-10-06：保存响应新增 fact_conflicts 字段（无冲突时为空列表）
        assert saved["ok"] is True
        assert saved["fact_conflicts"] == []
        result = asyncio.run(fr.get_facts(pid, None))
        stored = result["items"][0]["value"]
        assert stored == long_value, "超长/特殊字符不得被截断或转义破坏"
        assert result["placeholder_count"] == 0

    def test_empty_items_yields_empty_fact_conflicts(self):
        import app.routers.facts as fr

        pid = _new_project("事实-空集")
        _seed_analysis(pid, [])
        result = asyncio.run(fr.get_facts(pid, None))
        assert result["items"] == []
        assert result["fact_conflicts"] == []


class TestFactsTabLevelHttpFlow:
    """Tab 级（HTTP 端到端）交互：模拟前端的「新增→保存→确认」链路。

    覆盖：空 key 新增行必须被 400 拒绝（前端「新增事实」默认 key 为空）、
    补全 key 后可保存、确认后开关置位、整链数据自洽。
    """

    def _client(self):
        from app.main import app
        from fastapi.testclient import TestClient

        return TestClient(app)

    def test_add_empty_key_rejected_then_filled_accepted(self):
        API = "/api/v1"
        client = self._client()
        r = client.post(f"{API}/projects", json={"name": "事实Tab流", "industry": "市政工程"})
        assert r.status_code == 201, r.text
        pid = r.json()["id"]

        # ① 模拟「新增事实」产生空 key 行直接保存 → 400（不靠用户碰运气发现丢行）
        bad = client.post(
            f"{API}/projects/{pid}/facts",
            json={"items": [{"key": "", "label": "项目经理", "value": "张伟", "source": "manual"}]},
        )
        assert bad.status_code == 400
        assert "标识" in bad.json()["detail"]

        # ② 补全 key 后保存 → 200
        ok = client.post(
            f"{API}/projects/{pid}/facts",
            json={"items": [{"key": "projectManager", "label": "项目经理", "value": "张伟", "source": "manual"}]},
        )
        assert ok.status_code == 200, ok.text

        # ③ 确认 → 200，且 GET 回读 confirmed=True
        cf = client.post(f"{API}/projects/{pid}/facts/confirm", json={})
        assert cf.status_code == 200, cf.text
        got = client.get(f"{API}/projects/{pid}/facts").json()
        assert got["confirmed"] is True
        assert got["items"][0]["key"] == "projectManager"

    def test_generate_then_edit_then_confirm_http(self, monkeypatch):
        API = "/api/v1"
        import app.routers.facts as fr
        from app.services.ai import provider_factory, workflows

        monkeypatch.setattr(fr, "invalidate_content_caches", lambda pid: None)
        monkeypatch.setattr(fr, "get_facts_file_text", lambda _pid: "工期要求 450 日历天。")
        monkeypatch.setattr(provider_factory, "get_ai_config", lambda: object())
        monkeypatch.setattr(provider_factory, "create_provider", lambda _cfg: object())

        async def _fake(_provider, _messages, **_kwargs):
            return {"projectName": "示范工程"}

        monkeypatch.setattr(workflows, "collect_json_response", _fake)

        client = self._client()
        r = client.post(f"{API}/projects", json={"name": "事实Tab提取", "industry": "市政工程"})
        pid = r.json()["id"]

        gen = client.post(
            f"{API}/projects/{pid}/facts/generate",
            json={"missing_value_policy": "fabricate"},
        )
        assert gen.status_code == 200, gen.text
        body = gen.json()
        assert "fact_conflicts" in body
        keys = {i["key"] for i in body["items"]}
        assert {"projectName", "scheduleDuration"} <= keys

        # 编辑：把项目名改掉再保存
        edited = [{"key": "projectName", "label": "项目名称", "value": "改后工程", "source": "manual"}]
        sv = client.post(f"{API}/projects/{pid}/facts", json={"items": edited})
        assert sv.status_code == 200
        got = client.get(f"{API}/projects/{pid}/facts").json()
        assert got["items"][0]["value"] == "改后工程"
        assert got["confirmed"] is False

