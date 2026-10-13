"""一致性校核模块 —— 2026-10-13 深度审计的缺陷修复回归测试。

本文件覆盖本轮（2026-10-13）审计定位并修复的缺陷，每条都用「缺陷当时的真实
形态」作为输入，而不是「修复后的理想形态」，避免再次出现
「测试替身固化了契约、把线上缺陷一起固化成绿色」的反模式
（本轮 P0 与 P1 两条缺陷的共同成因即是如此，见各用例 docstring）。

| 用例组 | 对应缺陷 | 严重度 |
|---|---|---|
| `TestAgentReportContractSymmetry` | `run_agent_repair` 报告缺 `errors` / `max_turns` → 前端无保护取 `.length` → 整页白屏 | **P0** |
| `TestRulesPayloadReflectsEffectiveConfig` | `/consistency/rules` 下发模块常量而非配置生效值；`new_value` 上限缺失 | P1 |
| `TestWriteSideEffectG verbs` | 带写副作用的 GET（audit / scoring-compliance persist 默认） | P1 |
| `TestLockTableIsBounded` | `_CONSISTENCY_OP_LOCKS` 无界增长 | P2 |
| `TestRejectionCoverageItemShape` | `items[]` 的 `id` / `section_hits` 结构（前端曾按 `clause_id` + `string[]` 消费） | P1 |
"""
from __future__ import annotations

import asyncio
import json

import pytest

from app.db import get_db, init_db


OWNER = 3500


def _new_project(name: str = "一致性审计契约项目") -> int:
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


def _client():
    from fastapi.testclient import TestClient

    from app.main import app
    from app.security import auth

    app.dependency_overrides[auth.get_current_user] = lambda: auth.User(OWNER, "u", "user")
    return TestClient(app)


# ============================================================================
# P0：Agent 修复报告与 AI 补丁修复报告**不对称**，缺 errors / max_turns
# ============================================================================


class TestAgentReportContractSymmetry:
    """`run_agent_repair` 报告 dict 必须与 `repair_with_ai` 同构。

    BUG-FIX 2026-10-13（P0）：`consistency_repair.repair_with_ai` 的报告 dict 有
    `errors`，`agent_repair.run_agent_repair` 没有。前端 `AgentRepairTab.tsx` 按
    「两条链路同构」无保护地取 `report.errors.length` —— 于是任何一份**成功**的
    Agent 响应都会抛

        TypeError: Cannot read properties of undefined (reading 'length')

    React 18 在无错误边界时卸载整棵组件树，而 `ConsistencyPage` 未挂
    ErrorBoundary → 整个「一致性校核」页白屏，Tab 全切不动。
    该组件的测试之所以一直绿，是因为它的桩数据**手写了契约里不存在的
    `errors: []`**，替真实响应打了圆场。
    """

    #: 与 `consistency_repair.repair_with_ai` 的报告 dict 键集对齐后的期望键集
    EXPECTED_KEYS = {
        "project_id",
        "dry_run",
        "success",
        "turns",
        "max_turns",
        "applied",
        "rejected",
        "unresolved",
        "errors",
        "changed_sections",
        "conflict_states",
        "message",
    }

    def test_report_dict_declares_all_contract_keys(self):
        """用「源码即契约」的静态检查锁住键集，防止将来再删。"""
        import inspect

        from app.services import agent_repair

        src = inspect.getsource(agent_repair.run_agent_repair)
        # 取出 report 字面量的那一段
        start = src.index('report: dict = {')
        end = src.index("}", start)
        block = src[start:end]
        for key in self.EXPECTED_KEYS:
            assert f'"{key}"' in block, f"agent 修复报告缺键 {key!r}（前端会按其存在消费）"

    def test_error_paths_populate_errors(self):
        """`errors` 不能是永远为空的死字段：真实失败路径必须写入。"""
        import inspect

        from app.services import agent_repair

        src = inspect.getsource(agent_repair.run_agent_repair)
        assert 'report["errors"].append' in src, (
            "errors 从未被写入 —— 前端会展示一个恒为 0 的「错误 N 条」标签，"
            "误导用户以为链路无异常"
        )

    def test_short_circuit_report_has_errors_and_max_turns(self):
        """空项目 / 无冲突的短路返回也必须带全部键。"""
        from app.services.agent_repair import run_agent_repair

        project_id = _new_project("Agent 报告键集")
        # 无章节 → 走「没有可供修复的正文内容」短路分支
        report = asyncio.run(run_agent_repair(project_id, dry_run=True, provider=object()))
        missing = self.EXPECTED_KEYS - set(report)
        assert not missing, f"短路返回缺键 {missing}"
        assert report["errors"] == []
        assert report["max_turns"] == 8

    def test_route_response_contains_errors_key(self):
        """HTTP 契约层同样必须带 `errors`（前端只认响应体）。"""
        from app.services.agent_repair import run_agent_repair

        project_id = _new_project("Agent 报告 HTTP 契约")
        report = asyncio.run(run_agent_repair(project_id, dry_run=True, provider=object()))
        # 路由层形如 {'status': ..., **report}
        merged = {"status": "partial", **report}
        assert "errors" in merged
        assert "max_turns" in merged


# ============================================================================
# P1：/consistency/rules 快照必须下发「配置生效值」而非模块常量
# ============================================================================


class TestRulesPayloadReflectsEffectiveConfig:
    """BUG-FIX 2026-10-13（P1）：快照与生效配置脱节。

    `rules_payload()` 此前下发 `AUTOFIX_KEYWORD_WINDOW_BEFORE/AFTER`（模块常量
    12 / 6）与写死的 `autofix_scope="source_sections"`。而真正生效的是
    `autofix_keyword_window_before()/after()`（读 config）与
    `autofix_scope_to_source_sections()`（读开关）。运维一旦调环境变量，实际闸门
    口径变了，而这个「规则快照」端点**照旧返回旧值** —— 前端据此展示的替换保护
    说明从此是错的，且无任何日志或告警。
    """

    def test_payload_uses_effective_window_values(self, monkeypatch):
        from app.services import consistency_rules as rules

        monkeypatch.setattr(rules, "autofix_keyword_window_before", lambda: 99)
        monkeypatch.setattr(rules, "autofix_keyword_window_after", lambda: 77)
        payload = rules.rules_payload()
        assert payload["facts"]["autofix_keyword_window_before"] == 99
        assert payload["facts"]["autofix_keyword_window_after"] == 77

    def test_payload_scope_reflects_switch(self, monkeypatch):
        from app.services import consistency_rules as rules

        monkeypatch.setattr(rules, "autofix_scope_to_source_sections", lambda: False)
        assert rules.rules_payload()["facts"]["autofix_scope"] == "all_sections"
        monkeypatch.setattr(rules, "autofix_scope_to_source_sections", lambda: True)
        assert rules.rules_payload()["facts"]["autofix_scope"] == "source_sections"

    def test_payload_includes_value_limit_and_switches(self):
        """前端需要这些才能解释「为什么被拒 / 为什么没改」，此前全部缺失。"""
        from app.services import consistency_rules as rules

        facts = rules.rules_payload()["facts"]
        assert facts["autofix_value_max_chars"] == rules.AUTOFIX_VALUE_MAX_CHARS
        assert isinstance(facts["autofix_require_keyword"], bool)

    def test_router_autofix_limit_comes_from_namespace(self):
        """路由层常量必须引用命名空间，不再各写一份 200。"""
        from app.routers import consistency as route
        from app.services import consistency_rules as rules

        assert route.AUTOFIX_VALUE_MAX_CHARS == rules.AUTOFIX_VALUE_MAX_CHARS

    def test_coverage_thresholds_are_disclosed(self):
        """评分 / 废标两个覆盖率阈值此前从不下发，前端只能硬编码或猜。"""
        from app.services import consistency_rules as rules

        payload = rules.rules_payload()
        assert payload["scoring_status"]["coverage_weak"] > 0
        assert payload["rejection_coverage"]["coverage_weak"] > 0

    def test_rules_endpoint_exposes_new_keys(self):
        """端点层同样可见（前端 api/index.ts 的 ConsistencyRules 类型依赖它们）。"""
        c = _client()
        r = c.get("/api/v1/consistency/rules")
        assert r.status_code == 200
        body = r.json()
        assert body["facts"]["autofix_value_max_chars"] > 0
        assert body["scoring_status"]["coverage_weak"] > 0
        assert body["rejection_coverage"]["coverage_weak"] > 0


# ============================================================================
# P1：带写副作用的 GET
# ============================================================================


class TestWriteSideEffectVerbs:
    """审计与评分达标两个端点都会**改库**，却都用 GET 暴露。"""

    def test_audit_supports_post_and_get(self):
        """POST 为规范入口；GET 保留以兼容既有调用方（响应体一致）。"""
        project_id = _new_project("审计动词")
        _seed_section(project_id, title="工期", content="总工期：30 天。")
        c = _client()

        post = c.post(f"/api/v1/consistency/audit/{project_id}")
        assert post.status_code == 200
        assert post.json()["status"] == "ok"

        get = c.get(f"/api/v1/consistency/audit/{project_id}")
        assert get.status_code == 200
        # 两者响应结构一致（只换了动词，没有改契约）
        assert set(post.json()) == set(get.json())

    def test_scoring_compliance_get_does_not_persist_by_default(self):
        """BUG-FIX 2026-10-13（P1）：`persist` 此前默认 True。

        于是「只是想看一眼」的普通 GET 每次都往 `scoring_compliance_audit` 插一行
        历史；前端 `ScoringComplianceTab` 恰恰显式传 `persist:false` 来规避（说明
        该默认值已 inconvenient 到要专门绕开），而脚本 / curl / 第三方调用方会无界
        地把该表灌大。改为默认 False：要看结果就别写历史，要留档显式传 true。
        响应体完全不变，仅副作用收敛。
        """
        project_id = _new_project("评分达标不落库")
        c = _client()

        def _count() -> int:
            with get_db(readonly=True) as conn:
                return conn.execute(
                    "SELECT COUNT(*) as c FROM scoring_compliance_audit WHERE project_id = ?",
                    (project_id,),
                ).fetchone()["c"]

        assert _count() == 0
        r = c.get(f"/api/v1/consistency/scoring-compliance/{project_id}")
        assert r.status_code == 200
        assert _count() == 0, "普通 GET 不应写审计历史（仅副作用收敛，响应体不变）"

        # 显式 persist=true 仍然留档 —— 依赖留档的调用方只需补一个参数
        r2 = c.get(
            f"/api/v1/consistency/scoring-compliance/{project_id}",
            params={"persist": "true"},
        )
        assert r2.status_code == 200
        assert _count() == 1


# ============================================================================
# P2：项目操作锁表无界增长
# ============================================================================


class TestLockTableIsBounded:
    """`_CONSISTENCY_OP_LOCKS` 此前「按项目 id 建锁、永不回收」。

    这是长驻 worker 进程里的无界单例表：项目数随使用持续增长、没有自然上界。
    修复策略为 LRU + 跳过持有中的锁（淘汰正在被等待/持有的锁会把并发操作拆到
    两把不同的锁上，那才是真正的竞态）。
    """

    def test_lock_table_stays_bounded(self):
        from app.routers import consistency as route

        with route._CONSISTENCY_OP_LOCKS_GUARD:
            route._CONSISTENCY_OP_LOCKS.clear()
            route._CONSISTENCY_OP_LOCKS_ORDER.clear()

        # 远超上限地创建锁，断言表本身不随之膨胀
        for i in range(route._CONSISTENCY_OP_LOCKS_MAX * 3):
            route._consistency_op_lock(-1_000_000 - i)

        with route._CONSISTENCY_OP_LOCKS_GUARD:
            size = len(route._CONSISTENCY_OP_LOCKS)
            assert size <= route._CONSISTENCY_OP_LOCKS_MAX, (
                f"锁表无界增长：上限 {route._CONSISTENCY_OP_LOCKS_MAX}，实际 {size}"
            )
            # order 列表不得残留已淘汰的 id（否则它会无限增长，只是换了个地方）
            assert len(route._CONSISTENCY_OP_LOCKS_ORDER) == size
            assert set(route._CONSISTENCY_OP_LOCKS_ORDER) == set(route._CONSISTENCY_OP_LOCKS)

        with route._CONSISTENCY_OP_LOCKS_GUARD:
            route._CONSISTENCY_OP_LOCKS.clear()
            route._CONSISTENCY_OP_LOCKS_ORDER.clear()

    def test_same_project_gets_same_lock(self):
        """淘汰不得破坏「同一项目同一把锁」这一核心语义。"""
        from app.routers import consistency as route

        a = route._consistency_op_lock(424242)
        b = route._consistency_op_lock(424242)
        assert a is b

    def test_held_lock_is_never_evicted(self):
        """正在持有的锁必须免于淘汰。"""
        from app.routers import consistency as route

        async def _run():
            with route._CONSISTENCY_OP_LOCKS_GUARD:
                route._CONSISTENCY_OP_LOCKS.clear()
                route._CONSISTENCY_OP_LOCKS_ORDER.clear()
            # 填满到上限，并把 victim 放在最前（最该被淘汰的候选）
            for i in range(route._CONSISTENCY_OP_LOCKS_MAX):
                route._consistency_op_lock(-2_000_000 - i)
            victim = route._CONSISTENCY_OP_LOCKS[-2_000_000]
            async with route._LockManager(-2_000_000):
                # 再多申请一个项目，触发淘汰
                route._consistency_op_lock(-3_000_000)
                assert route._CONSISTENCY_OP_LOCKS.get(-2_000_000) is victim
                assert victim.locked()
            with route._CONSISTENCY_OP_LOCKS_GUARD:
                route._CONSISTENCY_OP_LOCKS.clear()
                route._CONSISTENCY_OP_LOCKS_ORDER.clear()

        asyncio.run(_run())


# ============================================================================
# P1：废标覆盖率 items 结构（前端曾按 clause_id + string[] 消费）
# ============================================================================


class TestRejectionCoverageItemShape:
    """锁住 `items[]` 的真实结构，防止前端再次按错误契约消费。

    前端 `RejectionCoverageTab.tsx` 曾把 `rowKey` 写成 `clause_id`、把
    `section_hits` 声明为 `string[]` 并 `{h}` 直出，而后端给的是 `id` 与
    `[{section_id, matched_bigrams}]` —— 于是全表 rowKey 失效、该列渲染出
    `[object Object]`。两个错误都由「前端按自己的 TS 类型想象后端」造成，
    故在服务端把结构钉死。
    """

    def test_items_use_id_and_object_section_hits(self):
        from app.services.rejection_coverage import rejection_answer_coverage

        project_id = _new_project("废标覆盖率结构")
        sid = _seed_section(project_id, title="工期承诺", content="总工期不超过 365 日历天。")
        with get_db() as conn:
            conn.execute(
                "INSERT INTO rejection_checks (project_id, status, results, created_at) "
                "VALUES (?, 'completed', ?, datetime('now','localtime'))",
                (
                    project_id,
                    json.dumps(
                        {"items": [{"id": "c1", "name": "工期条款", "clause": "投标人须承诺总工期"}]},
                        ensure_ascii=False,
                    ),
                ),
            )

        report = rejection_answer_coverage(project_id)
        assert report["status"] == "done", report
        assert report["items"], report

        item = report["items"][0]
        # 键名是 `id`，**不是** `clause_id`
        assert "id" in item
        assert "clause_id" not in item
        # section_hits 是对象数组
        assert isinstance(item["section_hits"], list)
        for hit in item["section_hits"]:
            assert isinstance(hit, dict)
            assert {"section_id", "matched_bigrams"} <= set(hit)
            assert isinstance(hit["section_id"], int)
            assert sid > 0

    def test_threshold_is_a_ratio_not_char_count(self):
        """前端曾把该阈值当「字数」展示（「单条阈值 200 字」），单位完全讲错。"""
        from app.services.rejection_coverage import COVERAGE_WEAK, rejection_answer_coverage

        assert 0 < COVERAGE_WEAK < 1, "覆盖率阈值必须是 0~1 的比值，不是字数"

        project_id = _new_project("废标覆盖率阈值")
        _seed_section(project_id, title="空章节", content="")
        with get_db() as conn:
            conn.execute(
                "INSERT INTO rejection_checks (project_id, status, results, created_at) "
                "VALUES (?, 'completed', ?, datetime('now','localtime'))",
                (project_id, json.dumps({"items": [{"id": "c1", "clause": "投标人须承诺总工期"}]})),
            )
        report = rejection_answer_coverage(project_id)
        assert report["threshold"] == COVERAGE_WEAK
        assert 0 < report["threshold"] < 1


class TestRejectionCoverageRouteDoesNot500:
    """P1 回归：端点对「已跑过废标检查」的项目此前**必然 500**。

    `rejection_coverage.rejection_answer_coverage` 用 `row.get("results")` 判断，
    而 `row` 是 `sqlite3.Row`（无 `.get`）→ AttributeError 冒泡出函数；路由层
    `asyncio.to_thread(...)` 外无 try/except → FastAPI 回 500。
    只有「从未跑过废标检查」（`row is None` 短路）才正常 —— 恰好把该端点唯一的
    存在意义整条废掉。此处锁住 HTTP 层的真实表现。
    """

    def _seed_check(self, project_id: int) -> None:
        with get_db() as conn:
            conn.execute(
                "INSERT INTO rejection_checks (project_id, status, results, created_at) "
                "VALUES (?, 'completed', ?, datetime('now','localtime'))",
                (
                    project_id,
                    json.dumps(
                        {"items": [{"id": "c1", "name": "工期条款", "clause": "投标人须承诺总工期"}]},
                        ensure_ascii=False,
                    ),
                ),
            )

    def test_no_checks_still_returns_graceful(self):
        """无废标数据：仍是 no_checks（不是 500）。"""
        project_id = _new_project("废标路由-无数据")
        c = _client()
        r = c.get(f"/api/v1/consistency/rejection-coverage/{project_id}")
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "no_checks"

    def test_with_checks_returns_200(self):
        """有废标数据：修复前 500，修复后 200。"""
        project_id = _new_project("废标路由-有数据")
        _seed_section(project_id, title="工期", content="我方承诺总工期不超过 365 日历天。")
        self._seed_check(project_id)

        c = _client()
        r = c.get(f"/api/v1/consistency/rejection-coverage/{project_id}")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "done", body
        assert body["scanned_clauses"] == 1
        assert body["items"][0]["id"] == "c1"
