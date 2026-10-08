"""审核与预检模块 —— 组件级交互测试（ENHANCE 2026-10-04）。

聚焦此前只被鉴权用例覆盖、缺少**计算逻辑**验证的两条预检/审计链：

1. 字数统计预检（word_count 路由 overview / section_stats）
   —— 聚合口径：总字数、目标覆盖率、达标/缺字/空章节分组、逐层级/逐状态分组、
      进度百分比与人类可读文本。此前仅有 IDOR 鉴权测试，无任何功能行为断言。

2. 全文一致性审计 facts_extracted（consistency_auditor.run_full_audit）
   —— BUG 回归：旧实现从历史审计日志**累加** facts_extracted，而首行日志写入的
      正是累加值，导致真实抽取数从未传出、前端「提取 N 个事实」恒显示 0。
      现改为「本次全量抽取的真实事实实例数」，并要求重复审计**不累加**（幂等）。

设计原则：直接调用路由协程 / 服务方法并显式传入身份（不依赖真实 HTTP/AI/网络），
只测行为不测实现；断言关键返回字段与不变量。
"""
from __future__ import annotations

import asyncio

import pytest

from app.db import get_db, init_db
from app.security.auth import User


OWNER = 3100
STRANGER_ID = 3200


def _run(coro):
    return asyncio.run(coro)


def _new_project(name: str) -> int:
    init_db()
    with get_db() as conn:
        conn.execute(
            "INSERT INTO projects (name, industry, created_by) VALUES (?, ?, ?)",
            (name, "通用", OWNER),
        )
        return conn.execute("SELECT id FROM projects ORDER BY id DESC LIMIT 1").fetchone()["id"]


def _seed_section(
    project_id: int,
    *,
    title: str,
    level: int,
    sort_order: int,
    content: str,
    status: str = "completed",
    target_words: int | None = None,
    section_type: str = "normal",
) -> int:
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO sections "
            "(project_id, title, level, sort_order, content, status, target_words, section_type) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (project_id, title, level, sort_order, content, status, target_words, section_type),
        )
        return cur.lastrowid


# ============================================================
# 1. 字数统计预检：overview 聚合口径
# ============================================================


class TestWordCountOverviewComputation:
    def test_empty_project_returns_zeroes(self):
        from app.routers.word_count import overview

        pid = _new_project("字数-空项目")
        result = _run(overview(pid, user=User(OWNER, "o", "user")))
        assert result["status"] == "ok"
        assert result["total_sections"] == 0
        assert result["total_words"] == 0
        assert result["total_target"] == 0
        # 无目标时覆盖率安全回 0，不得因除零抛异常
        assert result["target_coverage"] == 0

    def test_aggregation_coverage_and_grouping(self):
        from app.routers.word_count import overview

        pid = _new_project("字数-聚合")
        # 2026-10-08 契约更新：字数控制下沉到 L3——目标字数仅对 L3+ 生效。
        # 章节1：有正文 + 达标目标（10/10 在 ±10% 容差内）
        _seed_section(pid, title="第一章", level=3, sort_order=0,
                      content="一二三四五六七八九十", status="completed", target_words=10)
        # 章节2：有正文 + 未达标（缺字）
        _seed_section(pid, title="第二章", level=3, sort_order=1,
                      content="一二三", status="completed", target_words=100)
        # 章节3：空正文 → empty
        _seed_section(pid, title="第三章", level=1, sort_order=2,
                      content="", status="pending")

        result = _run(overview(pid, user=User(OWNER, "o", "user")))
        assert result["total_sections"] == 3
        assert result["empty_sections"] == 1
        assert result["on_target_sections"] == 1
        assert result["below_target_sections"] == 1
        # 总目标 = 10 + 100 + 0（L3 才有目标，2026-10-08 契约）
        assert result["total_target"] == 110
        # 总字数 = 第一章(10) + 第二章(3) = 13（count_words 中文逐字计数）
        assert result["total_words"] == 13
        assert result["completed_sections"] == 2
        assert isinstance(result["stats"]["per_level"], dict)
        assert isinstance(result["stats"]["per_status"], dict)
        # 分组键存在性
        assert "level_3" in result["stats"]["per_level"]
        assert "completed" in result["stats"]["per_status"]

    def test_coverage_math_matches_words_and_target(self):
        from app.routers.word_count import overview

        pid = _new_project("字数-覆盖率自洽")
        _seed_section(pid, title="A", level=3, sort_order=0,
                      content="一二三", status="completed", target_words=6)
        result = _run(overview(pid, user=User(OWNER, "o", "user")))
        total_words = result["total_words"]
        total_target = result["total_target"]
        assert total_target == 6
        assert total_words == 3  # count_words 中文逐字计数
        assert result["target_coverage"] == round(total_words / total_target * 100, 1)


# ============================================================
# 2. 字数统计预检：section_stats 明细与进度
# ============================================================


class TestWordCountSectionStats:
    def test_items_fields_and_progress(self):
        from app.routers.word_count import section_stats

        pid = _new_project("字数-明细")
        _seed_section(pid, title="达标章", level=3, sort_order=0,
                      content="一二三四", status="completed", target_words=4)
        _seed_section(pid, title="缺字章", level=3, sort_order=1,
                      content="一二", status="completed", target_words=10)

        result = _run(section_stats(pid, user=User(OWNER, "o", "user")))
        assert result["status"] == "ok"
        assert result["total"] == 2
        items = {it["title"]: it for it in result["items"]}
        # 达标：progress=100（min(100, 4/4*100)）
        assert items["达标章"]["progress"] == 100
        assert items["达标章"]["word_count"] == 4
        # 缺字：progress=20
        assert items["缺字章"]["progress"] == 20
        assert items["缺字章"]["word_count"] == 2
        # 人类可读文本字段存在
        assert isinstance(items["缺字章"]["word_count_text"], str)

    def test_no_target_section_progress(self):
        from app.routers.word_count import section_stats

        pid = _new_project("字数-无目标")
        _seed_section(pid, title="无目标章", level=1, sort_order=0,
                      content="一二三", status="completed", target_words=None)
        result = _run(section_stats(pid, user=User(OWNER, "o", "user")))
        item = result["items"][0]
        # 无目标且有正文 → 视为达成 100
        assert item["progress"] == 100

    def test_empty_content_no_target_progress_zero(self):
        from app.routers.word_count import section_stats

        pid = _new_project("字数-空无目标")
        _seed_section(pid, title="空章", level=1, sort_order=0,
                      content="", status="pending", target_words=None)
        result = _run(section_stats(pid, user=User(OWNER, "o", "user")))
        item = result["items"][0]
        # 无目标且无正文 → 0
        assert item["progress"] == 0

    def test_sort_tiering_deficit_then_met_then_no_target(self):
        """排序分层：缺字(按缺口大→小) → 已达标 → 无目标，无目标不再与达标交错。"""
        from app.routers.word_count import section_stats

        pid = _new_project("字数-排序分层")
        # 2026-10-08 契约：目标字数仅对 L3+ 生效——「无目标」用 L1 父节点表达，
        # 三个 L3 章节分别覆盖 已达标 / 小缺口 / 大缺口。
        _seed_section(pid, title="无目标", level=1, sort_order=0,
                      content="一二三", status="completed", target_words=None)
        _seed_section(pid, title="已达标", level=3, sort_order=1,
                      content="一二三", status="completed", target_words=2)
        _seed_section(pid, title="小缺口", level=3, sort_order=2,
                      content="一", status="completed", target_words=3)   # 缺2
        _seed_section(pid, title="大缺口", level=3, sort_order=3,
                      content="一", status="completed", target_words=10)  # 缺9

        result = _run(section_stats(pid, user=User(OWNER, "o", "user")))
        order = [it["title"] for it in result["items"]]
        assert order == ["大缺口", "小缺口", "已达标", "无目标"], order


# ============================================================
# 3. 一致性审计 facts_extracted BUG 回归（本次真实抽取数、重复审计不累加）
# ============================================================


class TestConsistencyFactsExtracted:
    def _seed_conflict_project(self, name: str) -> int:
        pid = _new_project(name)
        # 两条工期取值冲突：30 vs 45 → 触发一条「工期」冲突
        _seed_section(pid, title="概述", level=1, sort_order=0,
                      content="本项目总工期：30天，质量合格。", status="completed")
        _seed_section(pid, title="进度", level=1, sort_order=1,
                      content="计划工期：45天完成全部施工。", status="completed")
        return pid

    def test_facts_extracted_reflects_real_extraction(self):
        from app.services.consistency_auditor import ConsistencyAuditor

        pid = self._seed_conflict_project("一致性-抽取数真实")
        result = ConsistencyAuditor().run_full_audit(pid)
        assert result.status != "failed"
        # 本次至少抽取到两条工期事实实例（30、45），绝非旧的恒 0
        assert result.facts_extracted >= 2, "facts_extracted 应为本次真实抽取数，不得恒为 0"
        # 确实检出工期冲突
        assert any(c.fact_name == "工期" for c in result.conflicts)

    def test_reaudit_is_idempotent_not_accumulating(self):
        """重复审计：facts_extracted 稳定，不因历史日志累加而膨胀（旧 BUG 的直接回归）。"""
        from app.services.consistency_auditor import ConsistencyAuditor

        pid = self._seed_conflict_project("一致性-重复审计幂等")
        auditor = ConsistencyAuditor()
        first = auditor.run_full_audit(pid).facts_extracted
        second = auditor.run_full_audit(pid).facts_extracted
        third = auditor.run_full_audit(pid).facts_extracted
        assert first == second == third, "同一数据集重复审计，抽取数必须一致（不累加）"

    def test_audit_log_latest_matches_real_count(self):
        from app.services.consistency_auditor import ConsistencyAuditor

        pid = self._seed_conflict_project("一致性-日志落库")
        real = ConsistencyAuditor().run_full_audit(pid).facts_extracted
        with get_db() as conn:
            latest = conn.execute(
                "SELECT facts_extracted FROM consistency_audit_log "
                "WHERE project_id = ? ORDER BY audit_time DESC LIMIT 1",
                (pid,),
            ).fetchone()
        assert latest is not None
        assert int(latest["facts_extracted"]) == real

    def test_detect_conflicts_shared_path_equivalent(self):
        """detect_conflicts 复用预抽取 all_facts 与自行抽取结果一致（重构无行为漂移）。"""
        from app.services.consistency_auditor import ConsistencyAuditor

        pid = self._seed_conflict_project("一致性-共享抽取路径")
        auditor = ConsistencyAuditor()
        via_collect = auditor.detect_conflicts(pid)
        via_precomputed = auditor.detect_conflicts(pid, all_facts=auditor._collect_fact_instances(pid))
        assert {c.fact_name for c in via_collect} == {c.fact_name for c in via_precomputed}
        assert any(c.fact_name == "工期" for c in via_precomputed)


# ============================================================
# 4. 人员事实按岗位分桶（消除「不同岗位人数被误判为冲突」的假阳性）
# ============================================================


class TestPersonnelRoleBucketing:
    """BUG 回归：旧正则把项目经理/技术人员/管理人员合并为同一「人员数量」，
    于是一句「项目经理1人、技术人员20人、管理人员5人」被误报为冲突。
    现按岗位分桶，只有同岗位数量不一致才判冲突。"""

    def _multi_role(self, name: str) -> list:
        from app.services.consistency_auditor import ConsistencyAuditor

        pid = _new_project(name)
        _seed_section(pid, title="人员配备", level=1, sort_order=0,
                      content="本项目配备项目经理1人、技术人员20人、管理人员5人。", status="completed")
        _seed_section(pid, title="团队", level=1, sort_order=1,
                      content="项目经理1人，技术人员20人，管理人员5人。", status="completed")
        return ConsistencyAuditor().detect_conflicts(pid)

    def test_distinct_roles_do_not_create_false_conflict(self):
        conflicts = self._multi_role("人员-多岗位不误报")
        personnel = [c for c in conflicts if c.fact_name.endswith("数量") and c.fact_name != "金额"]
        # 各岗位两章取值一致 → 不应有任何人员类冲突
        assert personnel == [], f"不同岗位各自一致却误报: {[(c.fact_name, c.values) for c in personnel]}"

    def test_same_role_conflict_still_detected(self):
        from app.services.consistency_auditor import ConsistencyAuditor

        pid = _new_project("人员-同岗位真冲突")
        _seed_section(pid, title="A", level=1, sort_order=0,
                      content="项目经理：5人。", status="completed")
        _seed_section(pid, title="B", level=1, sort_order=1,
                      content="项目经理：8人。", status="completed")
        conflicts = [c for c in ConsistencyAuditor().detect_conflicts(pid) if c.fact_name == "项目经理数量"]
        assert conflicts, "同岗位不同人数必须仍被检出"
        assert set(conflicts[0].values) == {"5", "8"}

    def test_generic_personnel_stays_bundled(self):
        """未列举的岗位（后勤人员）仍归入通用「人员数量」，保留跨章节比对能力。"""
        from app.services.consistency_auditor import ConsistencyAuditor

        pid = _new_project("人员-通用归集")
        _seed_section(pid, title="A", level=1, sort_order=0,
                      content="后勤人员：30人。", status="completed")
        _seed_section(pid, title="B", level=1, sort_order=1,
                      content="后勤人员：40人。", status="completed")
        conflicts = [c for c in ConsistencyAuditor().detect_conflicts(pid) if c.fact_name == "人员数量"]
        assert conflicts, "裸「人员」岗位应归入通用人员数量并可跨章比对"
        assert set(conflicts[0].values) == {"30", "40"}

    def test_extract_names_are_role_specific(self):
        from app.services.consistency_auditor import ConsistencyAuditor

        facts = ConsistencyAuditor().extract_facts_from_text(
            "配备项目经理1人、技术人员20人、管理人员5人。"
        )
        names = sorted({f.name for f in facts})
        assert names == ["项目经理数量", "人员数量", "技术人员数量", "管理人员数量"] or \
            set(names) == {"项目经理数量", "技术人员数量", "管理人员数量"}, names


# ============================================================
# 5. 一致性路由 run_audit 端点回传真实 facts_extracted（前后端契约）
# ============================================================


class TestConsistencyRouteFactsField:
    def test_run_audit_endpoint_returns_nonzero_facts(self):
        from app.routers.consistency import run_audit

        pid = _new_project("一致性-路由抽取数")
        _seed_section(pid, title="S1", level=1, sort_order=0,
                      content="工期：60天。", status="completed")
        _seed_section(pid, title="S2", level=1, sort_order=1,
                      content="工期：90天。", status="completed")
        result = _run(run_audit(pid, user=User(OWNER, "o", "user")))
        assert result["status"] == "ok"
        assert result["facts_extracted"] >= 2, "路由层须把真实抽取数透传给前端"


# ============================================================
# 6. HTTP 层：审核/预检路由挂载与调用链路（TestClient）
#    —— 验证路由确实挂在 /api/v1 下、鉴权链贯通、响应契约与前端一致（无 AI 依赖）。
# ============================================================


class TestAuditPrecheckHttpChain:
    API = "/api/v1"

    def _client(self, user_id: int):
        from fastapi.testclient import TestClient

        from app.main import app
        from app.security.auth import get_current_user

        app.dependency_overrides[get_current_user] = lambda: User(user_id, "u", "user")
        return TestClient(app)

    def _seed_audit_project(self, name: str) -> int:
        pid = _new_project(name)
        _seed_section(pid, title="概述", level=1, sort_order=0,
                      content="总工期：30天。质目标：安全生产目标：零死亡。", status="completed")
        _seed_section(pid, title="进度", level=1, sort_order=1,
                      content="计划工期：45天完成。安全生产目标：无重伤事故。", status="completed")
        return pid

    def test_consistency_routes_mounted_and_return_contract(self):
        pid = self._seed_audit_project("HTTP-一致性链路")
        client = self._client(OWNER)
        try:
            r_audit = client.get(f"{self.API}/consistency/audit/{pid}")
            assert r_audit.status_code == 200, r_audit.text
            body = r_audit.json()
            assert body["status"] == "ok"
            # 真实抽取数透传（非恒 0）
            assert body["facts_extracted"] >= 2
            # 冲突列表含工期冲突
            assert any(c["fact_name"] == "工期" for c in body["conflicts"])

            r_summary = client.get(f"{self.API}/consistency/summary/{pid}")
            assert r_summary.status_code == 200
            assert r_summary.json()["status"] == "ok"

            r_conf = client.get(f"{self.API}/consistency/conflicts/{pid}")
            assert r_conf.status_code == 200
            assert "conflicts" in r_conf.json()
        finally:
            client.app.dependency_overrides.clear()

    def test_word_count_routes_mounted_and_return_contract(self):
        pid = self._seed_audit_project("HTTP-字数链路")
        client = self._client(OWNER)
        try:
            r_ov = client.get(f"{self.API}/word-count/overview/{pid}")
            assert r_ov.status_code == 200, r_ov.text
            assert r_ov.json()["status"] == "ok"

            r_sec = client.get(f"{self.API}/word-count/sections/{pid}")
            assert r_sec.status_code == 200
            assert r_sec.json()["total"] == 2
        finally:
            client.app.dependency_overrides.clear()

    def test_foreign_user_gets_403_on_consistency_audit(self):
        """跨项目越权：HTTP 层必须 403（锁定 IDOR 守卫已接入路由）。"""
        pid = self._seed_audit_project("HTTP-越权拦截")
        client = self._client(STRANGER_ID)
        try:
            r = client.get(f"{self.API}/consistency/audit/{pid}")
            assert r.status_code == 403, r.text
        finally:
            client.app.dependency_overrides.clear()
