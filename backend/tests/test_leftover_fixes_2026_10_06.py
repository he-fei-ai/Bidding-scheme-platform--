"""遗留项闭环测试（ENHANCE 2026-10-06 第二轮）：安全门禁 + 静态门禁自证 + 评审链阻塞修复。

覆盖本轮处置的三个遗留项：
1. **安全 P1 越权**：`GET /rejection/all-schedules` 原先只挂 `get_current_user`，
   任何登录用户可列出**全部项目**的废标报告调度 → 补管理员角色门禁。
2. **静态门禁**（`test_async_blocking_gate.py` 的行为侧证据）：这里补一条
   「评审链端点不再阻塞事件循环」的行为断言，与门禁的静态断言互为印证。
3. **评审链（sections.py）**：`_prepare_review` / `list_sections_route` 等端点
   原在 async def 内直调 `get_db()` 与 `build_tree`/`list_sections`。
"""
from __future__ import annotations

import asyncio
import time

import pytest

from app.db import get_db, init_db
from app.security.auth import User


OWNER = 5100
STRANGER = 5200
ADMIN = 5300

_BLOCK_SECONDS = 0.25
_TICK = 0.005
_MIN_TICKS = 5


def _new_project(name: str, owner: int = OWNER) -> int:
    init_db()
    with get_db() as conn:
        conn.execute(
            "INSERT INTO projects (name, industry, created_by) VALUES (?, ?, ?)",
            (name, "通用", owner),
        )
        return conn.execute("SELECT id FROM projects ORDER BY id DESC LIMIT 1").fetchone()["id"]


def _seed_section(project_id: int, *, title: str, content: str, sort_order: int = 0) -> int:
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO sections (project_id, title, level, sort_order, content, status) "
            "VALUES (?, ?, 1, ?, ?, 'completed')",
            (project_id, title, sort_order, content),
        )
        return int(cur.lastrowid)


async def _loop_ticks_while(awaitable) -> int:
    ticks = 0
    stop = False

    async def _hb():
        nonlocal ticks
        while not stop:
            ticks += 1
            await asyncio.sleep(_TICK)

    hb = asyncio.create_task(_hb())
    await asyncio.sleep(0)
    try:
        await awaitable
    finally:
        stop = True
        await hb
    return ticks


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def stub_ai_provider(monkeypatch):
    """`_prepare_review` 首步就 get_ai_config + create_provider，无密钥时先抛 500。

    这两个符号是**函数内局部导入**（`from ..services.ai.provider_factory import ...`），
    因此必须打在源模块上才能被 `_prepare_review` 看到。
    """
    from app.services.ai import provider_factory

    monkeypatch.setattr(
        provider_factory, "get_ai_config",
        lambda: {"provider": "openai_compatible", "api_key": "stub-key", "base_url": "http://x", "model": "m"},
    )
    monkeypatch.setattr(provider_factory, "create_provider", lambda cfg: object())
    return object()


# ==========================================================================
# 一、安全 P1：/rejection/all-schedules 越权
# ==========================================================================
class TestAllSchedulesAdminGuard:
    """跨项目调度列表必须仅管理员可见。"""

    def test_non_admin_is_forbidden(self):
        """普通登录用户 → 403（这是修复前的主要漏洞：任何人都能列出全项目调度）。"""
        from fastapi import HTTPException

        from app.routers.rejection import list_all_schedules

        with pytest.raises(HTTPException) as ei:
            _run(list_all_schedules(limit=10, user=User(STRANGER, "u", "user")))
        assert ei.value.status_code == 403
        assert "管理员" in str(ei.value.detail)

    def test_missing_user_is_forbidden(self):
        """user 为 None（依赖未注入）时也应拒绝，不得因 None 而漏过。"""
        from fastapi import HTTPException

        from app.routers.rejection import list_all_schedules

        with pytest.raises(HTTPException) as ei:
            _run(list_all_schedules(limit=10, user=None))
        assert ei.value.status_code == 403

    def test_admin_is_allowed(self, monkeypatch):
        """管理员放行，且仍走线程池（不阻塞 loop）。"""
        from app.routers import rejection as rj

        def _slow(limit=100):
            time.sleep(_BLOCK_SECONDS)
            return [{"schedule_id": 1}]

        monkeypatch.setattr(rj.rejection, "get_all_report_schedules", _slow)
        ticks = _run(_loop_ticks_while(
            rj.list_all_schedules(limit=10, user=User(ADMIN, "a", "admin"))
        ))
        assert ticks >= _MIN_TICKS, f"管理员列表端点阻塞了 loop（心跳 {ticks} 次）"

    def test_admin_gets_real_payload(self, monkeypatch):
        """管理员能真正拿到数据（门禁没有把功能锁死）。"""
        from app.routers import rejection as rj

        monkeypatch.setattr(
            rj.rejection, "get_all_report_schedules", lambda limit=100: [{"id": i} for i in range(limit)]
        )
        result = _run(rj.list_all_schedules(limit=3, user=User(ADMIN, "a", "admin")))
        assert result["total"] == 3
        assert len(result["items"]) == 3


class TestAllSchedulesHttpChain:
    """HTTP 层验证：门禁在真实请求链路上生效（防止只在直接调用时生效）。"""

    def _client(self):
        from fastapi.testclient import TestClient

        from app.main import app
        from app.security.auth import get_current_user

        app.dependency_overrides[get_current_user] = lambda: User(STRANGER, "u", "user")
        return TestClient(app)

    def test_http_403_for_normal_user(self):
        client = self._client()
        try:
            r = client.get("/api/v1/rejection/all-schedules")
            assert r.status_code == 403, r.text
        finally:
            client.app.dependency_overrides.clear()


# ==========================================================================
# 二、评审链（sections.py）阻塞修复
# ==========================================================================
class TestReviewChainOffEventLoop:
    def test_sections_tree_route_yields_control(self, monkeypatch):
        """GET /sections（目录树）不得阻塞 loop。"""
        from app.routers import sections as sec

        def _slow_tree(project_id):
            time.sleep(_BLOCK_SECONDS)
            return []

        monkeypatch.setattr(sec, "build_tree", _slow_tree)
        pid = _new_project("评审-目录树不阻塞")
        ticks = _run(_loop_ticks_while(
            sec.list_sections_route(pid, current_user=User(OWNER, "o", "user"))
        ))
        assert ticks >= _MIN_TICKS, f"目录树端点阻塞了 loop（心跳 {ticks} 次）"

    def test_sections_flat_route_yields_control_and_uses_readonly(self, monkeypatch):
        """GET /sections/flat（逐章正文统计）不得阻塞 loop，且须走只读连接。"""
        from app.routers import sections as sec

        seen: list[bool] = []

        def _slow_list(project_id, *, readonly=False):
            seen.append(bool(readonly))
            time.sleep(_BLOCK_SECONDS)
            return []

        monkeypatch.setattr(sec, "list_sections", _slow_list)
        pid = _new_project("评审-flat-不阻塞")
        ticks = _run(_loop_ticks_while(
            sec.list_sections_flat_route(pid, current_user=User(OWNER, "o", "user"))
        ))
        assert ticks >= _MIN_TICKS, f"扁平章节端点阻塞了 loop（心跳 {ticks} 次）"
        assert seen == [True], f"须传 readonly=True，实际 {seen}"

    def test_prepare_review_db_access_off_loop(self, monkeypatch, stub_ai_provider):
        """_prepare_review 的查评分点必须移出事件循环（原本直调写锁 get_db）。"""
        from app.routers import sections as sec

        def _fake_get_ai_config():
            return {"api_key": "x"}

        def _fake_create_provider(cfg):
            return object()

        real_get_db = sec.get_db
        holder = {}

        def _tracking_get_db(*, readonly=False):
            holder.setdefault("readonly", []).append(bool(readonly))
            return real_get_db(readonly=readonly)

        monkeypatch.setattr(sec, "get_db", _tracking_get_db)
        monkeypatch.setattr(sec, "build_tree", lambda pid: [])

        pid = _new_project("评审-评分点不阻塞")
        # 未做评分点识别 → 应 400（证明查询真的执行了）
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as ei:
            _run(sec._prepare_review(pid))
        assert ei.value.status_code == 400
        assert holder["readonly"] == [True], (
            f"评分点查询须走只读连接，实际 readonly={holder['readonly']}"
        )

    def test_review_routes_still_registered(self):
        from app.main import app

        paths = app.openapi()["paths"]
        for p in (
            "/api/v1/projects/{project_id}/sections/review",
            "/api/v1/projects/{project_id}/sections/review-with-autofix",
            "/api/v1/projects/{project_id}/sections",
            "/api/v1/projects/{project_id}/sections/flat",
            "/api/v1/rejection/all-schedules",
        ):
            assert p in paths, f"路由未挂载: {p}"


class TestPrepareReviewErrorPathsPreserved:
    """_prepare_review 重构后三条错误分支与文案保持原样。"""

    def test_missing_scoring_points_400(self, monkeypatch, stub_ai_provider):
        from fastapi import HTTPException

        from app.routers import sections as sec

        monkeypatch.setattr(sec, "build_tree", lambda pid: [])
        pid = _new_project("评审-无评分点")
        with pytest.raises(HTTPException) as ei:
            _run(sec._prepare_review(pid))
        assert ei.value.status_code == 400
        assert "评分点" in str(ei.value.detail)

    def test_returns_scoring_points_and_outline(self, monkeypatch, stub_ai_provider):
        """正常路径：返回 (provider, scoring_points, outline) 三元组。"""
        from app.routers import sections as sec

        pid = _new_project("评审-正常路径")
        with get_db() as conn:
            conn.execute(
                "INSERT INTO analysis (project_id, scoring_points_json) VALUES (?, ?)",
                (pid, '[{"name": "技术方案"}]'),
            )
        _seed_section(pid, title="概述", content="正文内容")
        monkeypatch.setattr(sec, "build_tree", lambda _pid: [])
        provider, scoring_points, outline = _run(sec._prepare_review(pid))
        assert provider is not None
        assert "技术方案" in scoring_points
        assert isinstance(outline, str) and outline == "[]"
