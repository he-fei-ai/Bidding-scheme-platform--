"""一致性校核模块 —— 第七轮（2026-10-17）回归测试。

聚焦本轮修复的缺陷，每条一个独立用例，钉死行为不变量：

| 用例组 | 缺陷 | 现象 / 修复 |
|---|---|---|
| A | `_LockManager` 构造时取锁，锁表 LRU 淘汰存在「构造后、acquire 前」窗口 | 该窗口内锁既未持有也无等待者却已被请求持有引用，淘汰后同一项目再建新锁 → 两把锁对象 → 临界区互斥被击穿。改为 `__aenter__` 重新取字典规范锁 |
| B | `apply_autofix` 路由对 AI 语义发现（`AI-` 前缀）无服务端守卫 | 前端禁用按钮但 API 仍可调用，values=[] 必然 no_change；服务端显式 400 + 可读原因 |
| C | `facts_backflow_proposals` / `rejection-coverage` 端点无 try/except + 无 logger | 服务层异常穿透成框架级裸 500，无项目上下文可倒查；补齐本地化 detail + 错误日志 |
| D | `run_audit` 异常路径不计 `audit_failed` 指标 | 500 失败率在 /consistency/metrics 观测不到；两条失败路径都计数 |
| E | `propose_facts` 纯读却用写连接 | 生成高峰期与正文写库争全局写锁；改 readonly 连接 |

原则：只测可观察行为（HTTP 响应、锁表状态、指标快照、连接参数），不依赖真实 AI/网络。
"""
from __future__ import annotations

import asyncio

import pytest

from app.db import get_db, init_db
from app.security.auth import User


OWNER = 3500
STRANGER_ID = 3501
API = "/api/v1"


def _new_project(name: str) -> int:
    init_db()
    with get_db() as conn:
        conn.execute(
            "INSERT INTO projects (name, industry, created_by) VALUES (?, ?, ?)",
            (name, "通用", OWNER),
        )
        return conn.execute("SELECT id FROM projects ORDER BY id DESC LIMIT 1").fetchone()["id"]


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


def _seed_conflict(project_id: int, *, conflict_id: str) -> None:
    with get_db() as conn:
        conn.execute(
            "INSERT INTO consistency_conflicts "
            "(project_id, conflict_id, fact_name, values_json, sources_json, "
            " severity, description, resolved, created_at) "
            "VALUES (?, ?, '工期', '[\"450\",\"500\"]', '[\"section_1\"]', 'high', "
            "'手工构造', 0, '2026-10-17 10:00:00')",
            (project_id, conflict_id),
        )


# ============================================================================
# A. 锁表淘汰竞态窗口（_LockManager 进入时取规范锁）
# ============================================================================


class TestLockManagerRefetchesCanonicalLock:
    """BUG-FIX 2026-10-17：锁在「构造后、acquire 前」被淘汰时不得互斥失效。

    旧实现在 `__init__` 取锁：锁表打到上限后，一把刚创建、尚未 acquire 的锁
    既不在持有、也无等待者，会被 LRU 淘汰掉；随后同一项目的下一个请求拿到
    一把**新**锁 —— 两个并发操作各持不同的锁对象，项目级临界区互斥被击穿
    （`_save_conflicts` 的 DELETE+INSERT 与 autofix 的 UPDATE 交错，冲突状态
    被误判）。现改为 `__aenter__` 重新取字典里的规范锁。
    """

    def test_lock_manager_acquires_canonical_lock_after_eviction(self):
        from app.routers import consistency as route

        async def _run():
            with route._CONSISTENCY_OP_LOCKS_GUARD:
                route._CONSISTENCY_OP_LOCKS.clear()
                route._CONSISTENCY_OP_LOCKS_ORDER.clear()
            try:
                pid = -4_000_001
                # 模拟「构造时拿到的锁」——随后即被淘汰
                stale = route._consistency_op_lock(pid)
                # 填满锁表迫使淘汰：stale 未持锁，是最旧的淘汰候选
                for i in range(route._CONSISTENCY_OP_LOCKS_MAX):
                    route._consistency_op_lock(-4_100_000 - i)
                assert route._CONSISTENCY_OP_LOCKS.get(pid) is None, (
                    "前置条件：构造后的锁应已被 LRU 淘汰"
                )
                lm = route._LockManager(pid)
                async with lm:
                    canonical = route._CONSISTENCY_OP_LOCKS[pid]
                    # 核心不变量：acquire 的是字典里的规范锁，不是过期的 stale
                    assert lm._lock is canonical
                    assert canonical is not stale
                    assert canonical.locked()
                    # 同一项目的第二个管理器必须排队在同一把规范锁上
                    lm2 = route._LockManager(pid)
                    assert lm2._lock is None or lm2._lock is canonical
                    assert canonical.locked()
                assert not canonical.locked()
            finally:
                with route._CONSISTENCY_OP_LOCKS_GUARD:
                    route._CONSISTENCY_OP_LOCKS.clear()
                    route._CONSISTENCY_OP_LOCKS_ORDER.clear()

        asyncio.run(_run())

    def test_same_project_still_shares_one_lock(self):
        """无淘汰发生时行为不变：同一项目反复拿到同一把锁。"""
        from app.routers import consistency as route

        a = route._consistency_op_lock(424242)
        b = route._consistency_op_lock(424242)
        assert a is b


# ============================================================================
# B. autofix 对 AI 语义发现的服务端守卫
# ============================================================================


class TestAutofixRejectsAiFindings:
    """BUG-FIX 2026-10-17：AI 发现（values 恒空）在 API 层显式 400。

    前端 2026-10-15 禁用了 AI 发现行的「自动修复」按钮，但服务端仍可被
    脚本 / 第三方 / 未来 UI 入口调用并必然返回 no_change。现路由层按
    `conslict_id` 前缀（`rules.is_ai_conflict_id`）显式拒绝。
    """

    def test_autofix_ai_conflict_400(self):
        pid = _new_project("R7-autofix-AI-400")
        _seed_conflict(pid, conflict_id="AI-1-deadbeefcafe")
        resp = _client().post(
            f"{API}/consistency/conflicts/AI-1-deadbeefcafe/autofix",
            params={"project_id": pid, "new_value": "450"},
        )
        assert resp.status_code == 400, resp.text
        assert "AI 语义发现" in resp.json()["detail"]

    def test_autofix_rule_conflict_not_blocked(self):
        """规则冲突不受守卫影响：不存在的冲突仍走原有 no_change 报告路径。"""
        pid = _new_project("R7-autofix-rule-control")
        resp = _client().post(
            f"{API}/consistency/conflicts/conflict_missing_1/autofix",
            params={"project_id": pid, "new_value": "450"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "no_change"
        assert body["success"] is False


# ============================================================================
# C. 只读端点的错误处理（本地化 500 + 日志上下文）
# ============================================================================


class TestReadEndpointsLocalized500:
    """BUG-FIX 2026-10-17：模块内最后两个裸 500 端点补齐统一口径。"""

    def test_facts_proposals_localized_500(self, monkeypatch):
        from app.services import consistency_backflow as backflow

        pid = _new_project("R7-proposals-500")

        def _boom(*_a, **_k):
            raise RuntimeError("模拟扫描崩溃")

        monkeypatch.setattr(backflow, "propose_facts", _boom)
        resp = _client().get(f"{API}/consistency/facts/proposals/{pid}")
        assert resp.status_code == 500, resp.text
        detail = resp.json()["detail"]
        assert "候选事实扫描失败" in detail
        assert "模拟扫描崩溃" in detail

    def test_rejection_coverage_localized_500(self, monkeypatch):
        from app.services import rejection_coverage as rc

        pid = _new_project("R7-rejection-500")

        def _boom(*_a, **_k):
            raise RuntimeError("模拟覆盖率崩溃")

        monkeypatch.setattr(rc, "rejection_answer_coverage", _boom)
        resp = _client().get(f"{API}/consistency/rejection-coverage/{pid}")
        assert resp.status_code == 500, resp.text
        detail = resp.json()["detail"]
        assert "覆盖率统计失败" in detail
        assert "模拟覆盖率崩溃" in detail


# ============================================================================
# D. 审计异常路径计入 audit_failed 指标
# ============================================================================


class TestAuditExceptionMetric:
    """BUG-FIX 2026-10-17：路由层直接抛异常的 500 也计 audit_failed。

    此前只有「内部降级为 failed 结果」那条路径计数，异常路径的失败率在
    `GET /consistency/metrics` 里观测不到。
    """

    def test_audit_exception_increments_failed_metric(self, monkeypatch):
        from app.routers import consistency as route
        from app.services.consistency_metrics import consistency_metrics

        consistency_metrics.reset()
        pid = _new_project("R7-audit-metric")

        def _boom(_project_id):
            raise RuntimeError("模拟审计崩溃")

        monkeypatch.setattr(route.consistency_auditor, "run_full_audit", _boom)
        resp = _client().post(f"{API}/consistency/audit/{pid}")
        assert resp.status_code == 500, resp.text
        snap = consistency_metrics.snapshot()
        assert snap.get("audit_failed") == 1, snap
        # 成功计数不得被异常路径误增
        assert snap.get("audit_runs", 0) == 0, snap

    def test_audit_degraded_result_still_counts(self, monkeypatch):
        """回归 2026-10-16 行为：降级 failed 结果路径的计数保持不変。"""
        from app.routers import consistency as route
        from app.services.consistency_auditor import AuditResult
        from app.services.consistency_metrics import consistency_metrics

        consistency_metrics.reset()
        pid = _new_project("R7-audit-degraded")

        def _degraded(_project_id):
            return AuditResult(project_id=pid, total_sections=0, facts_extracted=0,
                               conflicts_found=0, status="failed")

        monkeypatch.setattr(route.consistency_auditor, "run_full_audit", _degraded)
        resp = _client().post(f"{API}/consistency/audit/{pid}")
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "failed"
        snap = consistency_metrics.snapshot()
        assert snap.get("audit_failed") == 1, snap


# ============================================================================
# E. propose_facts 纯读走 readonly 连接
# ============================================================================


class TestProposeFactsReadonlyConnections:
    """BUG-FIX 2026-10-17：纯读路径不再抢全局写锁。

    `propose_facts`（含 `_existing_fact_markers`）只做 SELECT，此前两处都按
    写连接取锁 —— 生成高峰期事实回流 Tab 每次扫描都与正文写库争锁。
    """

    def test_propose_facts_uses_readonly_only(self, monkeypatch):
        from app.db import get_db as real_get_db
        from app.services import consistency_backflow as backflow

        pid = _new_project("R7-propose-readonly")
        recorded: list[bool] = []

        def _spy(readonly: bool = False):
            recorded.append(bool(readonly))
            return real_get_db(readonly=readonly)

        monkeypatch.setattr(backflow, "get_db", _spy)
        result = backflow.propose_facts(pid, min_occurrences=3)
        # 函数本体一次 + _existing_fact_markers 一次，均为只读
        assert recorded == [True, True], recorded
        assert result["proposals"] == []
