"""一致性校核模块 —— 2026-10-08 缺陷修复回归测试。

覆盖本轮审计中**逐条实测确认**的缺陷，每条一个独立用例，钉死行为不变量：

| 用例组 | 缺陷 | 现象 |
|---|---|---|
| A | P0 `consistency_audit_content` 未从 `workflows` facade re-export | 正文生成勾选「全文一致性审计」后**一次都没跑过**，且只有一条 warning |
| B | `GET /consistency/audit/{id}` 无条件回 `status='ok'` | 审计内部崩溃被降级成「发现 0 处冲突」的**谎报成功** |
| C | `scoring-compliance` 路由硬编码 `coverage_weak=0.10` | 服务层已标定 0.25，生产入口却用被判定「过松、有已知漏报」的旧值 |
| D | `GET /conflicts` 不区分已解决/未解决 | 早已修好的历史冲突与待处理冲突混排，用户无法分辨 |
| E | `ORDER BY CASE severity … END` 漏 `ELSE` | `severity=''` 的行在 SQLite 里求值 NULL 并**排到最前**，顶在 high 之前 |
| F | `summary.by_severity` 未过滤 `resolved` | 「严重/中等冲突」卡片把已解决的也算进去，与「未解决冲突」口径打架 |
| G | `refresh_conflict_states` 用裸子串定位 | 冲突值「30」在正文写作「300天」时被判「仍在正文」→ 永久 still_open |
| H | `consistency_rules` 命名空间单测 | 严重度归一 / 排序 / 事实名 / 状态判定的单一事实来源 |

原则：只测可观察行为（HTTP 响应体、DB 落库结果、跨模块 import 结果），
不测内部实现细节；不依赖真实 AI/网络（AI 路径以纯函数 `refresh_conflict_states` 验证）。
"""
from __future__ import annotations

import asyncio
import inspect

import pytest

from app.db import get_db, init_db
from app.security.auth import User


OWNER = 3400
STRANGER_ID = 3401


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


def _seed_section(project_id: int, *, title: str, content: str, sort_order: int = 0) -> int:
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO sections (project_id, title, level, sort_order, content, status, section_type) "
            "VALUES (?, ?, 1, ?, ?, 'completed', 'normal')",
            (project_id, title, sort_order, content),
        )
        return cur.lastrowid


def _seed_conflict(
    project_id: int,
    *,
    conflict_id: str,
    severity: str,
    resolved: int = 0,
    values: str = '["450","500"]',
    fact_name: str = "工期",
    created_at: str = "2026-10-08 10:00:00",
) -> None:
    """直接插一行冲突，用于构造已解决/脏严重度等 auditor 正常路径造不出的状态。"""
    with get_db() as conn:
        conn.execute(
            "INSERT INTO consistency_conflicts "
            "(project_id, conflict_id, fact_name, values_json, sources_json, "
            " severity, description, resolved, created_at) "
            "VALUES (?, ?, ?, ?, '[\"section_1\"]', ?, '手工构造', ?, ?)",
            (project_id, conflict_id, fact_name, values, severity, resolved, created_at),
        )


def _client(user_id: int = OWNER):
    from fastapi.testclient import TestClient

    from app.main import app
    from app.security.auth import get_current_user

    app.dependency_overrides[get_current_user] = lambda: User(user_id, "u", "user")
    return TestClient(app)


@pytest.fixture(autouse=True)
def _clean_overrides():
    yield
    try:
        from app.main import app

        app.dependency_overrides.clear()
    except Exception:
        pass


# ============================================================================
# A. P0 —— 后生成一致性审计整条链路失效（facade 漏 re-export）
# ============================================================================


class TestPostGenerationAuditLink:
    def test_consistency_audit_content_importable_from_workflows_facade(self):
        """BUG-A（P0·链路断裂）回归。

        `routers/sse/sse_content.py` 是 `from ...services.ai.workflows import
        consistency_audit_content`（走 facade）。facade 曾只 re-export
        `estimate_total_words_from_probe`，漏了本符号 → 每次带
        `use_consistency_audit=true` 生成正文必然 ImportError，又被外层裸
        `except Exception` 吞成一条 warning，**用户全程无感**。

        已实测复现过：
            ImportError: cannot import name 'consistency_audit_content'
            from 'app.services.ai.workflows'
        """
        from app.services.ai import workflows

        assert hasattr(workflows, "consistency_audit_content"), (
            "workflows facade 缺少 consistency_audit_content re-export —— "
            "sse_content 的后生成一致性审计分支会 ImportError 并被静默吞掉"
        )
        assert callable(workflows.consistency_audit_content)

    def test_facade_export_signature_matches_sse_call_site(self):
        """re-export 的函数签名必须与 sse_content 的调用方式兼容（关键字 cancel_event）。"""
        from app.services.ai.workflows import consistency_audit_content

        params = inspect.signature(consistency_audit_content).parameters
        # sse_content 以 (project_id, provider, results, sections_by_id, summary,
        # tender_context, cancel_event=...) 七个参数调用（G1/G2 修复补了 project_id）
        for name in (
            "project_id",
            "provider",
            "results",
            "sections_by_id",
            "summary",
            "tender_context",
        ):
            assert name in params, f"签名缺少位置参数 {name}"
        assert "cancel_event" in params, "签名缺少 cancel_event（sse_content 以关键字传入）"

    def test_sse_content_import_target_resolves(self):
        """直接执行 sse_content 里的那句 import 语句，确认真实调用链可用。

        比只断言 facade 有属性更强：它会真正走一遍 `workflows` 包初始化。
        """
        from app.services.ai.workflows import consistency_audit_content as fn

        assert fn is not None

    def test_workflows_package_importable(self):
        """facade 整体仍可导入（re-export 不得引入循环依赖）。"""
        import importlib

        module = importlib.import_module("app.services.ai.workflows")
        for name in (
            "generate_outline",
            "generate_sections",
            "orchestrate_content",
            "collect_json_response",
            "estimate_total_words_from_probe",
            "consistency_audit_content",
        ):
            assert hasattr(module, name), f"facade 缺少 {name}"


# ============================================================================
# B. 审计失败被谎报为成功
# ============================================================================


class TestAuditFailureIsNotReportedAsSuccess:
    API = "/api/v1"

    def test_successful_audit_reports_ok_with_audit_status(self):
        pid = _new_project("A2-审计成功")
        _seed_section(pid, title="概述", content="总工期：30天。")
        _seed_section(pid, title="进度", content="计划工期：45天完成。", sort_order=1)
        client = _client(OWNER)
        r = client.get(f"{self.API}/consistency/audit/{pid}")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "ok"
        # audit_status 与 HTTP status 分开，避免同名键覆盖
        assert body["audit_status"] in {"completed", "completed_no_conflicts"}
        assert body["facts_extracted"] >= 2
        assert any(c["fact_name"] == "工期" for c in body["conflicts"])

    def test_degraded_audit_is_not_reported_as_ok(self, monkeypatch):
        """BUG-B（P1·谎报成功）回归。

        `run_full_audit` 把异常吞掉并降级返回 `status='failed'` + 全 0；路由层
        此前无条件回 `status='ok'`，前端随即弹「审计完成，发现 0 处冲突」——
        实际审计在第一步就崩了，用户据此以为全文一致。
        """
        from app.services.consistency_auditor import AuditResult
        from app.services import consistency_auditor as auditor_mod

        pid = _new_project("A2-审计降级")
        _seed_section(pid, title="概述", content="总工期：30天。")

        def _boom(project_id):
            return AuditResult(
                project_id=project_id,
                total_sections=0,
                facts_extracted=0,
                conflicts_found=0,
                audit_time="2026-10-08 10:00:00",
                status="failed",
            )

        monkeypatch.setattr(auditor_mod.consistency_auditor, "run_full_audit", _boom)
        client = _client(OWNER)
        r = client.get(f"{self.API}/consistency/audit/{pid}")
        assert r.status_code == 200, r.text
        body = r.json()
        # 关键：绝不能是 ok
        assert body["status"] != "ok", "审计失败被谎报为成功"
        assert body["status"] == "failed"
        assert body["audit_status"] == "failed"
        assert body["conflicts"] == []
        assert "不可信" in body["message"]

    def test_is_audit_success_rejects_failed_status(self):
        from app.services import consistency_rules as rules

        assert rules.is_audit_success("completed") is True
        assert rules.is_audit_success("completed_no_conflicts") is True
        assert rules.is_audit_success("failed") is False
        assert rules.is_audit_success("") is False
        assert rules.is_audit_success(None) is False


# ============================================================================
# C. 评分达标阈值：路由默认值必须等于服务层标定值
# ============================================================================


class TestScoringComplianceThreshold:
    API = "/api/v1"

    def test_route_default_threshold_equals_service_constant(self):
        """BUG-C（P1·配置不一致）回归。

        路由曾硬编码 `Query(0.10)`，而 `scoring_compliance.COVERAGE_WEAK` 已按实测
        标定到 0.25，且该常量注释明确写「初版取 0.10 过于宽松，实测被 0.21 的
        反例直接逃过告警，故上调」—— 也就是说生产唯一入口用的正是被判定过松的值。
        """
        from app.services.scoring_compliance import COVERAGE_WEAK

        from app.routers import consistency as consistency_router

        param = inspect.signature(consistency_router.scoring_compliance).parameters
        default = param["coverage_weak"].default
        assert COVERAGE_WEAK == 0.25, "标定常量本身被改动，请同步更新本断言与注释"
        assert default.default == COVERAGE_WEAK, (
            f"路由默认阈值 {default.default} 与服务层标定值 {COVERAGE_WEAK} 不一致"
        )

    def test_weak_point_is_caught_over_http_with_default_threshold(self):
        """覆盖率 0.21 的反例在默认阈值(0.25)下必须被判 weak —— 旧默认值 0.10 会漏报。"""
        from app.services.scoring_compliance import COVERAGE_WEAK

        pid = _new_project("C-评分达标阈值")
        with get_db() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO analysis (project_id, scoring_points_json) VALUES (?, ?)",
                (
                    pid,
                    '[{"id":"R1","name":"施工组织设计","weight":"10",'
                    '"standard":"须包含施工工艺流程图并附 BIM 模型"}]',
                ),
            )
        # 正文只泛泛而谈，覆盖率落在 0.10 与 0.25 之间
        _seed_section(
            pid,
            title="施工组织设计",
            content="本文介绍施工工艺，并做一些说明性介绍，以充实本章篇幅内容。" * 3,
            sort_order=0,
        )
        with get_db() as conn:
            conn.execute(
                "UPDATE sections SET scoring_point_id = 'R1' WHERE project_id = ?", (pid,)
            )

        client = _client(OWNER)
        r = client.get(f"{self.API}/consistency/scoring-compliance/{pid}?persist=false")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "ok"
        assert body["weak"] >= 1, (
            f"覆盖率 {body['gaps'][0]['coverage']:.2%} 的弱响应未被 weak 捕获"
            f"（阈值 {COVERAGE_WEAK:.0%}）—— 默认阈值回退到了过松的旧值"
        )
        gap = next(g for g in body["gaps"] if g["status"] == "weak")
        assert 0 < gap["coverage"] < COVERAGE_WEAK

    def test_explicit_override_still_supported(self):
        """显式传参仍可覆盖（向后兼容：调用方可按更严标准自行解读）。"""
        pid = _new_project("C-评分阈值覆盖")
        with get_db() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO analysis (project_id, scoring_points_json) VALUES (?, ?)",
                (
                    pid,
                    '[{"id":"R1","name":"施工组织设计","weight":"10",'
                    '"standard":"须包含施工工艺流程图并附 BIM 模型"}]',
                ),
            )
        _seed_section(pid, title="施工组织设计", content="介绍施工工艺。" * 40)
        with get_db() as conn:
            conn.execute(
                "UPDATE sections SET scoring_point_id = 'R1' WHERE project_id = ?", (pid,)
            )
        client = _client(OWNER)
        r = client.get(
            f"{self.API}/consistency/scoring-compliance/{pid}?persist=false&coverage_weak=0.99"
        )
        assert r.status_code == 200, r.text
        assert r.json()["weak"] >= 1


# ============================================================================
# D. 已解决 / 未解决冲突必须分离
# ============================================================================


class TestConflictStateSeparation:
    API = "/api/v1"

    def _seed_mixed(self, name: str) -> int:
        pid = _new_project(name)
        _seed_section(pid, title="概述", content="工期：30天。")
        _seed_conflict(pid, conflict_id=f"{pid}_open_high", severity="high", resolved=0)
        _seed_conflict(pid, conflict_id=f"{pid}_done_med", severity="medium", resolved=1)
        return pid

    def test_default_filter_returns_only_unresolved(self):
        """BUG-D（P1·历史冲突混入）回归。

        `_save_conflicts` 只清理 `resolved=0`、已解决的作为历史保留；此前本接口
        无条件全量返回，前端表格把早已修好的冲突和待处理冲突混排。
        """
        pid = self._seed_mixed("D-默认只回未解决")
        client = _client(OWNER)
        body = client.get(f"{self.API}/consistency/conflicts/{pid}").json()
        assert body["filter"] == "open"
        assert body["count"] == 1
        assert [c["conflict_id"] for c in body["conflicts"]] == [f"{pid}_open_high"]
        assert body["conflicts"][0]["state"] == "open"
        assert body["conflicts"][0]["resolved"] == 0

    def test_resolved_filter_returns_only_resolved(self):
        pid = self._seed_mixed("D-只看已解决")
        client = _client(OWNER)
        body = client.get(f"{self.API}/consistency/conflicts/{pid}?resolved=resolved").json()
        assert body["filter"] == "resolved"
        assert body["count"] == 1
        assert body["conflicts"][0]["conflict_id"] == f"{pid}_done_med"
        assert body["conflicts"][0]["state"] == "resolved"
        assert body["conflicts"][0]["resolved"] == 1

    def test_all_filter_returns_both(self):
        pid = self._seed_mixed("D-全部")
        client = _client(OWNER)
        body = client.get(f"{self.API}/consistency/conflicts/{pid}?resolved=all").json()
        assert body["filter"] == "all"
        assert body["count"] == 2

    def test_invalid_filter_rejected(self):
        pid = self._seed_mixed("D-非法过滤")
        client = _client(OWNER)
        r = client.get(f"{self.API}/consistency/conflicts/{pid}?resolved=bogus")
        assert r.status_code == 422

    def test_corrupt_json_does_not_break_payload_shape(self):
        """values/sources 损坏时仍须回 list，避免前端 .map 崩溃。"""
        pid = _new_project("D-脏JSON")
        with get_db() as conn:
            conn.execute(
                "INSERT INTO consistency_conflicts "
                "(project_id, conflict_id, fact_name, values_json, sources_json, "
                " severity, description, resolved, created_at) "
                "VALUES (?, ?, '工期', '{not json', 'also bad', 'high', 'x', 0, ?)",
                (pid, f"{pid}_bad", "2026-10-08 10:00:00"),
            )
        client = _client(OWNER)
        body = client.get(f"{self.API}/consistency/conflicts/{pid}").json()
        assert body["count"] == 1
        assert body["conflicts"][0]["values"] == []
        assert body["conflicts"][0]["sources"] == []

    def test_legacy_nonlist_values_normalized(self):
        """values_json 合法但不是数组（如字典）时归一为 []。"""
        pid = _new_project("D-非数组JSON")
        with get_db() as conn:
            conn.execute(
                "INSERT INTO consistency_conflicts "
                "(project_id, conflict_id, fact_name, values_json, sources_json, "
                " severity, description, resolved, created_at) "
                "VALUES (?, ?, '工期', '{\"a\":1}', '{}', 'high', 'x', 0, ?)",
                (pid, f"{pid}_dict", "2026-10-08 10:00:00"),
            )
        client = _client(OWNER)
        body = client.get(f"{self.API}/consistency/conflicts/{pid}").json()
        assert body["conflicts"][0]["values"] == []


# ============================================================================
# E. 严重度排序必须给「未知」兜底位（ORDER BY 缺 ELSE）
# ============================================================================


class TestSeverityOrdering:
    API = "/api/v1"

    def test_unknown_severity_sorts_last_not_first(self):
        """BUG-E（P1·排序错乱）回归。

        原 SQL `ORDER BY CASE severity WHEN 'high' THEN 1 … END` **漏写 ELSE**：
        SQLite 对未命中 CASE 的行求值为 NULL，而 NULL 在 ASC 排序里排**最前**，
        于是 `severity=''` 的脏数据行被顶到 `high` 行之前，冲突列表顺序错乱。
        """
        pid = _new_project("E-排序")
        _seed_section(pid, title="概述", content="工期：30天。")
        _seed_conflict(pid, conflict_id=f"{pid}_empty", severity="", resolved=0)
        _seed_conflict(pid, conflict_id=f"{pid}_high", severity="high", resolved=0)
        _seed_conflict(pid, conflict_id=f"{pid}_low", severity="low", resolved=0)

        client = _client(OWNER)
        body = client.get(f"{self.API}/consistency/conflicts/{pid}").json()
        order = [c["conflict_id"] for c in body["conflicts"]]
        assert order[0] == f"{pid}_high", f"high 未排在首位，实际顺序 {order}"
        assert order[1] == f"{pid}_low"
        # 脏严重度行必须排最后，且响应里已被归一化为 medium
        assert order[-1] == f"{pid}_empty"
        assert body["conflicts"][-1]["severity"] == "medium"

    def test_sort_sql_contains_else_branch(self):
        from app.services import consistency_rules as rules

        sql = rules.severity_sort_sql()
        assert "ELSE" in sql, "ORDER BY 片段缺少 ELSE —— NULL 会排到最前"
        assert sql.index("ELSE") > sql.index("WHEN 'high'")
        assert f"ELSE {rules.SEVERITY_UNKNOWN_RANK}" in sql

    def test_auditor_normalizes_severity_before_write(self):
        """auditor 写库前归一化，从源头杜绝 `severity=''`。"""
        from app.services import consistency_rules as rules
        from app.services.consistency_auditor import ConsistencyAuditor

        auditor = ConsistencyAuditor()
        pid = _new_project("E-写库归一化")
        from app.services.consistency_auditor import Conflict

        auditor._save_conflicts(
            pid,
            [
                Conflict(
                    conflict_id=f"{pid}_dirty",
                    fact_name="工期",
                    values=["30", "45"],
                    sources=["s1"],
                    severity="",  # 脏值
                    description="d",
                )
            ],
        )
        with get_db(readonly=True) as conn:
            row = conn.execute(
                "SELECT severity FROM consistency_conflicts WHERE conflict_id = ?",
                (f"{pid}_dirty",),
            ).fetchone()
        assert row["severity"] == rules.SEVERITY_DEFAULT


# ============================================================================
# F. 摘要卡片统计口径必须与冲突列表一致
# ============================================================================


class TestSummaryStatsConsistency:
    API = "/api/v1"

    def test_severity_cards_exclude_resolved_conflicts(self):
        """BUG-F（P1·统计口径打架）回归。

        原 `by_severity` 未过滤 `resolved`，把已解决的也计入「严重/中等冲突」
        卡片，而同一屏「未解决冲突」只算未解决的 —— 用户看到
        「未解决 3 / 严重 1 / 中等 1」，点进表格却发现那条中等早已修好。
        """
        pid = _new_project("F-统计口径")
        _seed_section(pid, title="概述", content="工期：30天。")
        _seed_conflict(pid, conflict_id=f"{pid}_open_high", severity="high", resolved=0)
        _seed_conflict(pid, conflict_id=f"{pid}_open_med", severity="medium", resolved=0)
        _seed_conflict(pid, conflict_id=f"{pid}_done_low", severity="low", resolved=1)

        client = _client(OWNER)
        summary = client.get(f"{self.API}/consistency/summary/{pid}").json()["summary"]
        assert summary["unresolved_conflicts"] == 2
        assert summary["resolved_conflicts"] == 1
        # 已解决的 low 不得进入 severity 卡片
        assert summary["conflicts_by_severity"] == {"high": 1, "medium": 1}
        assert "low" not in summary["conflicts_by_severity"]

    def test_summary_counts_match_conflict_list_length(self):
        """不变量：摘要的未解决数 == 冲突列表默认（open）返回的条数。"""
        pid = _new_project("F-口径一致")
        _seed_section(pid, title="概述", content="工期：30天。")
        _seed_conflict(pid, conflict_id=f"{pid}_a", severity="high", resolved=0)
        _seed_conflict(pid, conflict_id=f"{pid}_b", severity="medium", resolved=0)
        _seed_conflict(pid, conflict_id=f"{pid}_c", severity="low", resolved=1)

        client = _client(OWNER)
        summary = client.get(f"{self.API}/consistency/summary/{pid}").json()["summary"]
        conflicts = client.get(f"{self.API}/consistency/conflicts/{pid}").json()
        assert summary["unresolved_conflicts"] == conflicts["count"]
        by_sev = summary["conflicts_by_severity"]
        assert sum(by_sev.values()) == conflicts["count"]

    def test_empty_project_returns_zeroed_summary(self):
        pid = _new_project("F-空项目")
        client = _client(OWNER)
        summary = client.get(f"{self.API}/consistency/summary/{pid}").json()["summary"]
        assert summary["latest_audit"] is None
        assert summary["unresolved_conflicts"] == 0
        assert summary["resolved_conflicts"] == 0
        assert summary["conflicts_by_severity"] == {}


# ============================================================================
# G. AI 修复后的冲突状态收敛必须带数字边界保护
# ============================================================================


class TestConflictStateRefreshBoundary:
    def test_numeric_substring_does_not_count_as_present(self):
        """BUG-G（P1·谎报仍待处理）回归。

        `refresh_conflict_states` 曾用裸子串 `str(value) in content`。当冲突取值
        恰好互为子串（本例「30」与「300」是极常见的工期口径错写）而正文只写了
        「300天」时：

          - 裸子串：两个取值都判定「仍在正文」→ distinct present = 2 → 永久 still_open
          - 边界保护：只有「300」真在正文 → distinct present = 1 → 应判已解决

        确定性修复路径（`consistency_auditor.apply_autofix` →
        `text_patch.replace_values`）走的是后者，两条路径口径必须一致，否则 AI
        修完仍提示「仍待处理」，用户反复重试。
        """
        from app.services.consistency_repair import refresh_conflict_states

        pid = _new_project("G-边界保护")
        _seed_conflict(
            pid,
            conflict_id=f"{pid}_num",
            severity="high",
            resolved=0,
            values='["30","300"]',
        )
        with get_db() as conn:
            # 正文只含 300，取值 30 已不在正文（它只是 300 的子串）
            states = refresh_conflict_states(conn, pid, {1: "本项目工期300天。"})
        assert states["still_open"] == 0, (
            "取值 30 只是 300 的子串、正文中并不存在，不应被判为仍待处理 —— "
            "说明仍在用裸子串匹配"
        )
        assert states["resolved"] == 1
        with get_db(readonly=True) as conn:
            row = conn.execute(
                "SELECT resolved FROM consistency_conflicts WHERE conflict_id = ?",
                (f"{pid}_num",),
            ).fetchone()
        assert row["resolved"] == 1

    def test_substring_only_pair_never_produces_false_open(self):
        """更一般的形式：任意一组互为子串的取值，正文只含最大者时应收敛为已解决。"""
        from app.services.consistency_repair import refresh_conflict_states

        pid = _new_project("G-子串组收敛")
        _seed_conflict(
            pid,
            conflict_id=f"{pid}_num",
            severity="high",
            resolved=0,
            values='["30","300","3000"]',
        )
        with get_db() as conn:
            states = refresh_conflict_states(conn, pid, {1: "工期3000天。"})
        assert states["still_open"] == 0
        assert states["resolved"] == 1

    def test_genuinely_present_multiple_values_stay_open(self):
        """反向不变量：正文确实同时存在两个取值时，必须保持未解决（不谎报已解决）。"""
        from app.services.consistency_repair import refresh_conflict_states

        pid = _new_project("G-真冲突保持未解决")
        _seed_conflict(
            pid, conflict_id=f"{pid}_num", severity="high", resolved=0, values='["30","450"]'
        )
        with get_db() as conn:
            states = refresh_conflict_states(conn, pid, {1: "工期30天，另一章写450天。"})
        assert states["still_open"] == 1
        assert states["resolved"] == 0

    def test_single_remaining_value_resolves(self):
        from app.services.consistency_repair import refresh_conflict_states

        pid = _new_project("G-统一后收敛")
        _seed_conflict(
            pid, conflict_id=f"{pid}_num", severity="high", resolved=0, values='["30","450"]'
        )
        with get_db() as conn:
            states = refresh_conflict_states(conn, pid, {1: "本项目工期450天，全文统一。"})
        assert states["resolved"] == 1
        assert states["still_open"] == 0


# ============================================================================
# H. 规则命名空间单测
# ============================================================================


class TestConsistencyRulesNamespace:
    def test_severity_order_and_rank(self):
        from app.services import consistency_rules as rules

        assert rules.SEVERITY_ORDER == ("high", "medium", "low")
        assert rules.SEVERITY_RANK == {"high": 1, "medium": 2, "low": 3}
        assert rules.SEVERITY_UNKNOWN_RANK == 4

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("high", "high"),
            ("HIGH", "high"),
            (" Medium ", "medium"),
            ("low", "low"),
            ("", "medium"),
            (None, "medium"),
            ("critical", "medium"),
            (123, "medium"),
        ],
    )
    def test_normalize_severity(self, raw, expected):
        from app.services import consistency_rules as rules

        assert rules.normalize_severity(raw) == expected

    @pytest.mark.parametrize(
        "raw,expected",
        [("high", 1), ("medium", 2), ("low", 3), ("", 4), ("weird", 4), (None, 4)],
    )
    def test_severity_rank_unknown_goes_last(self, raw, expected):
        from app.services import consistency_rules as rules

        assert rules.severity_rank(raw) == expected

    def test_critical_fact_names(self):
        from app.services import consistency_rules as rules

        assert rules.is_critical_fact("工期") is True
        assert rules.is_critical_fact("金额") is True
        assert rules.is_critical_fact("资质等级") is True
        assert rules.is_critical_fact("质量标准") is False
        assert rules.is_critical_fact("") is False
        assert rules.is_critical_fact(None) is False

    def test_auditor_severity_matches_rules_namespace(self):
        """auditor 的严重度判定必须与命名空间常量一致（不得再自带一份字面量）。"""
        from app.services import consistency_rules as rules
        from app.services.consistency_auditor import ConsistencyAuditor

        auditor = ConsistencyAuditor()
        assert auditor._assess_severity("工期", ["30", "45"]) == rules.SEVERITY_HIGH
        # 非关键事实 + 恰好 2 种取值 → medium（阈值来自常量）
        assert auditor._assess_severity("质量标准", ["合格", "优良"]) == rules.SEVERITY_MEDIUM
        # 非关键事实 + 3 种取值 → high
        assert auditor._assess_severity("质量标准", ["合格", "优良", "优秀"]) == rules.SEVERITY_HIGH

    def test_personnel_fact_name_splits_by_role(self):
        from app.services import consistency_rules as rules

        assert rules.personnel_fact_name("项目经理") == "项目经理数量"
        assert rules.personnel_fact_name("技术人员") == "技术人员数量"
        assert rules.personnel_fact_name("人员") == "人员数量"
        assert rules.personnel_fact_name("") == "人员数量"
        assert rules.personnel_fact_name(None) == "人员数量"

    def test_auditor_personnel_extraction_uses_rules_namespace(self):
        """抽取产出的岗位事实名必须与 rules.personnel_fact_name 一致。"""
        from app.services import consistency_rules as rules
        from app.services.consistency_auditor import ConsistencyAuditor

        auditor = ConsistencyAuditor()
        facts = auditor.extract_facts_from_text("项目经理：1人；技术人员：20人；管理人员：5人。")
        names = {f.name for f in facts}
        for role in ("项目经理", "技术人员", "管理人员"):
            assert rules.personnel_fact_name(role) in names

    def test_conflict_state_mapping(self):
        from app.services import consistency_rules as rules

        assert rules.conflict_state(0) == "open"
        assert rules.conflict_state(1) == "resolved"
        assert rules.conflict_state(None) == "open"
        assert rules.conflict_state("x") == "open"

    def test_scoring_status_meta_unknown_is_safe(self):
        from app.services import consistency_rules as rules

        assert rules.scoring_status_meta("weak")["color"] == "gold"
        assert rules.scoring_status_meta("nope")["color"] == "default"
        assert rules.scoring_status_meta(None)["label"] == "未知"

    def test_rules_payload_shape(self):
        from app.services import consistency_rules as rules

        payload = rules.rules_payload()
        # ENHANCE 2026-10-13：原断言是 `set(payload) == {…}` **等值**比较，把顶层键集
        # 焊死成不可扩展——正因如此，2026-10-09 加「自动修复防护口径」时只能把
        # window/scope 硬塞进语义上并不合适的 `facts` 块（那里本应只有事实名），
        # 同一个隐性耦合逼出了下一轮的继续打补丁：本次要下发的废标覆盖率阈值同理。
        # 改为「必备键 ⊆ 顶层键」+「每个值都是 dict」：既保住契约不被悄悄删，
        # 又允许命名空间按需新增子块。
        required = {
            "severity",
            "facts",
            "audit_status",
            "conflict_state",
            "scoring_status",
        }
        assert required <= set(payload), f"规则快照缺必备顶层块: {required - set(payload)}"
        assert all(isinstance(v, dict) for v in payload.values())
        assert payload["severity"]["order"] == ["high", "medium", "low"]
        assert payload["severity"]["meta"]["high"]["label"] == "严重"
        assert payload["conflict_state"] == {"open": "open", "resolved": "resolved"}

    def test_rules_endpoint_returns_snapshot(self):
        """规则端点：纯读、无需项目属权（不触碰任何项目数据）。"""
        from fastapi.testclient import TestClient

        from app.main import app

        with TestClient(app) as client:
            r = client.get("/api/v1/consistency/rules")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "ok"
        assert body["severity"]["order"] == ["high", "medium", "low"]
        assert body["severity"]["meta"]["medium"]["label"] == "中等"


# ============================================================================
# J. P0 —— autofix 接口 100% 失败（路由把 keyword-only 参数当位置参数传）
# ============================================================================


class TestAutofixRouteIsCallable:
    """BUG-J（P0·主修复接口从未成功执行）回归。

    `ConsistencyAuditor.apply_autofix(self, conflict_id, project_id, new_value, *, dry_run=False)`
    的 `dry_run` 是 keyword-only，而路由层 `asyncio.to_thread(...)` 把它当第 4 个
    **位置参数**传入，于是每一次 HTTP 调用都抛
        TypeError: apply_autofix() takes 4 positional arguments but 5 were given
    被 except 吞成 HTTP 500 —— **POST /consistency/conflicts/{id}/autofix 这个
    「把冲突真正改写进正文」的主修复接口从来就没成功执行过一次**。

    此前无任何测试覆盖该路径：既有 autofix 用例（test_enhancement_2026_09_14）
    都是**直接调服务方法**，绕过路由层，故缺陷长期潜伏。
    本组用例由 2026-10-08 的端到端链路测试
    （tests/test_consistency_e2e_chain_2026_10_08.py）首次暴露。
    """
    API = "/api/v1"

    def test_route_passes_dry_run_as_keyword(self):
        """钉住服务签名：dry_run 必须是 keyword-only，否则路由按位置传就 TypeError。"""
        from app.services.consistency_auditor import ConsistencyAuditor

        sig = inspect.signature(ConsistencyAuditor.apply_autofix)
        dry = sig.parameters["dry_run"]
        assert dry.kind is inspect.Parameter.KEYWORD_ONLY, (
            "apply_autofix 的 dry_run 必须是 keyword-only —— "
            "路由层若按位置传参会 TypeError"
        )
        positional = [
            p.name
            for p in sig.parameters.values()
            if p.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
        ]
        assert positional == ["self", "conflict_id", "project_id", "new_value"]

    def test_autofix_http_call_does_not_500(self):
        """端到端验证：autofix 走 HTTP 不再返回 500，且真能落文。"""
        pid = _new_project("J-autofix 可调用")
        _seed_section(pid, title="概述", content="总工期：30天。")
        _seed_section(pid, title="进度", content="计划工期：45天完成。", sort_order=1)
        client = _client(OWNER)
        assert client.get(f"{self.API}/consistency/audit/{pid}").status_code == 200
        listing = client.get(f"{self.API}/consistency/conflicts/{pid}").json()
        assert listing["count"] >= 1
        cid = listing["conflicts"][0]["conflict_id"]

        r = client.post(
            f"{self.API}/consistency/conflicts/{cid}/autofix",
            params={"project_id": pid, "new_value": "30", "dry_run": True},
        )
        assert r.status_code == 200, f"autofix 返回 {r.status_code}: {r.text}"
        body = r.json()
        assert body["dry_run"] is True
        assert body["total_replacements"] >= 1

        # 真正落文路径同样不能 500
        r = client.post(
            f"{self.API}/consistency/conflicts/{cid}/autofix",
            params={"project_id": pid, "new_value": "30", "dry_run": False},
        )
        assert r.status_code == 200, f"autofix 返回 {r.status_code}: {r.text}"
        assert r.json()["status"] == "ok"
        with get_db(readonly=True) as conn:
            contents = [
                row["content"] or ""
                for row in conn.execute(
                    "SELECT content FROM sections WHERE project_id = ?", (pid,)
                ).fetchall()
            ]
        assert not any("45天" in c for c in contents), "autofix 未真正改写正文"

    def test_autofix_missing_value_returns_422_not_500(self):
        """缺 new_value 应是 422（参数校验），而不是穿透到服务层变 500。"""
        pid = _new_project("J-autofix 参数校验")
        _seed_section(pid, title="概述", content="总工期：30天。")
        _seed_section(pid, title="进度", content="计划工期：45天完成。", sort_order=1)
        client = _client(OWNER)
        client.get(f"{self.API}/consistency/audit/{pid}")
        listing = client.get(f"{self.API}/consistency/conflicts/{pid}").json()
        cid = listing["conflicts"][0]["conflict_id"]
        r = client.post(
            f"{self.API}/consistency/conflicts/{cid}/autofix",
            params={"project_id": pid},  # 故意不给 new_value
        )
        assert r.status_code == 422


# ============================================================================
# I. 规则端点与 IDOR 边界
# ============================================================================


class TestRulesEndpointAccess:
    API = "/api/v1"

    def test_other_consistency_routes_still_enforce_idor(self):
        """新增 /rules 端点不得削弱既有路由的属权校验。"""
        pid = _new_project("I-IDOR")
        _seed_section(pid, title="概述", content="工期：30天。")
        client = _client(STRANGER_ID)
        for path in (
            f"{self.API}/consistency/audit/{pid}",
            f"{self.API}/consistency/conflicts/{pid}",
            f"{self.API}/consistency/summary/{pid}",
            f"{self.API}/consistency/scoring-compliance/{pid}",
        ):
            r = client.get(path)
            assert r.status_code == 403, f"{path} 未拦截越权访问：{r.status_code}"

    def test_dead_code_count_extracted_facts_removed(self):
        """DEAD-CODE 回归：`count_extracted_facts` 已移除，不得复活。

        它自述供 `run_full_audit` 使用，实际早已改为直接对 `_collect_fact_instances`
        的返回值求和；保留它等于多一条「调它就重扫全部章节正文」的重复路径。
        """
        from app.services.consistency_auditor import ConsistencyAuditor

        assert not hasattr(ConsistencyAuditor, "count_extracted_facts")

    def test_run_audit_is_exported_through_coroutine(self):
        """路由协程可直接 await（供无 HTTP 场景复用，如 Celery 预检任务）。"""
        from app.routers.consistency import run_audit

        assert inspect.iscoroutinefunction(run_audit)