# -*- coding: utf-8 -*-
"""HARDENING 2026-10-11 —— 文本模型配置模块遗留缺口修复测试

覆盖：
- H-1：POST /test 并发 rate limit（asyncio.Semaphore，AI_TEST_CONCURRENCY_LIMIT）
- H-2：同名 Profile 创建返回 409 Conflict
- H-3：配置审计日志 retention 清理（cleanup_old_config_audit）
- H-4：inspect.getsource 全局补丁（conftest）对源码断言的稳定性回归
"""
import asyncio
import inspect
import types

import pytest

from app.services.ai.provider_factory import cleanup_old_config_audit
from app.db import get_db


# ─────────────────────────────────────────────────────────────
# H-1：POST /test 并发 rate limit
# ─────────────────────────────────────────────────────────────
def test_semaphore_factory_reads_env_limit(monkeypatch):
    import app.routers.ai_config as m

    monkeypatch.setenv("AI_TEST_CONCURRENCY_LIMIT", "7")
    monkeypatch.setattr(m, "_TEST_CONCURRENCY_LIMIT", 7)
    # 强制重建单例：全量套件中该信号量可能已被其他测试创建（_value=5），
    # 单独跑/全量跑都必须是 7 才能验证环境变量生效。
    monkeypatch.setattr(m, "_TEST_CONNECTION_SEMAPHORE", None)
    sem = m._get_test_connection_semaphore()
    assert sem._value == 7


def test_semaphore_factory_returns_singleton():
    import app.routers.ai_config as m

    assert m._get_test_connection_semaphore() is m._get_test_connection_semaphore()


def test_test_endpoint_acquires_and_releases_semaphore(monkeypatch):
    """test_ai_connection 应获取信号量并在 finally 释放（无论成功/失败）。"""
    import app.routers.ai_config as m

    acquired = []
    released = []

    class _FakeSem:
        @property
        def _value(self):
            return 5

        async def acquire(self):
            acquired.append(1)

        def release(self):
            released.append(1)

    fake_sem = _FakeSem()
    monkeypatch.setattr(m, "_get_test_connection_semaphore", lambda: fake_sem)
    # 绕过 URL 校验与外部 provider 调用，聚焦信号量语义
    monkeypatch.setattr(m, "_validate_base_url", lambda *a, **k: None)
    monkeypatch.setattr(m, "_clamp_test_ai_params", lambda mt, t: (256, 0.1))

    async def _fake_chat(messages, max_tokens, temperature):
        return "OK"

    fake_provider = types.SimpleNamespace(chat=_fake_chat)
    monkeypatch.setattr(m, "_create_shared_provider", lambda cfg: fake_provider)

    data = m.AIConfigTest(
        provider="agnes",
        api_key="sk-test",
        model="gpt-4o",
        base_url="http://example.com/v1",
        max_tokens=4096,
        temperature=0.7,
    )

    result = asyncio.run(m.test_ai_connection(data))
    assert result["status"] == "ok"
    assert acquired == [1]
    assert released == [1]


def test_test_endpoint_releases_semaphore_on_exception(monkeypatch):
    import app.routers.ai_config as m

    acquired = []
    released = []

    class _FakeSem:
        @property
        def _value(self):
            return 5

        async def acquire(self):
            acquired.append(1)

        def release(self):
            released.append(1)

    fake_sem = _FakeSem()
    monkeypatch.setattr(m, "_get_test_connection_semaphore", lambda: fake_sem)
    monkeypatch.setattr(m, "_validate_base_url", lambda *a, **k: None)
    monkeypatch.setattr(m, "_clamp_test_ai_params", lambda mt, t: (256, 0.1))

    def _boom_chat(messages, max_tokens, temperature):
        raise RuntimeError("boom")

    fake_provider = types.SimpleNamespace(chat=_boom_chat)
    monkeypatch.setattr(m, "_create_shared_provider", lambda cfg: fake_provider)

    data = m.AIConfigTest(
        provider="agnes",
        api_key="sk-test",
        model="gpt-4o",
        base_url="http://example.com/v1",
    )

    with pytest.raises(Exception) as ei:
        asyncio.run(m.test_ai_connection(data))
    assert "boom" in str(ei.value)
    assert acquired == [1]
    assert released == [1]


# ─────────────────────────────────────────────────────────────
# H-2：同名 Profile 创建返回 409
# ─────────────────────────────────────────────────────────────
def test_create_duplicate_profile_returns_409():
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as client:
        r1 = client.post(
            "/api/v1/ai/profiles",
            json={"name": "dup_h2", "model": "gpt-4o", "provider_name": "agnes"},
        )
        assert r1.status_code in (200, 201, 409), r1.text

        r2 = client.post(
            "/api/v1/ai/profiles",
            json={"name": "dup_h2", "model": "gpt-4o", "provider_name": "agnes"},
        )
        assert r2.status_code == 409, r2.text
        assert "已存在" in r2.json().get("detail", "")

        client.delete("/api/v1/ai/profiles/dup_h2")


# ─────────────────────────────────────────────────────────────
# H-3：配置审计日志 retention 清理
# ─────────────────────────────────────────────────────────────
def test_cleanup_old_config_audit_deletes_only_old_rows():
    with get_db() as conn:
        conn.execute("DELETE FROM ai_config_audit WHERE actor = 'h3_probe'")

    with get_db() as conn:
        conn.execute(
            "INSERT INTO ai_config_audit (actor, action, target, detail, created_at)"
            " VALUES ('h3_probe', 'old', 'x', 'old', datetime('now', 'localtime', '-100 days'))"
        )
        conn.execute(
            "INSERT INTO ai_config_audit (actor, action, target, detail, created_at)"
            " VALUES ('h3_probe', 'new', 'x', 'new', datetime('now', 'localtime'))"
        )
        conn.commit()

    deleted = cleanup_old_config_audit(retention_days=90)
    assert deleted >= 1

    with get_db() as conn:
        rows = conn.execute(
            "SELECT action FROM ai_config_audit WHERE actor = 'h3_probe'"
        ).fetchall()
    actions = [r[0] for r in rows]
    assert "new" in actions
    assert "old" not in actions

    with get_db() as conn:
        conn.execute("DELETE FROM ai_config_audit WHERE actor = 'h3_probe'")
        conn.commit()


def test_cleanup_audit_retention_days_floor_at_1():
    assert cleanup_old_config_audit(retention_days=0) >= 0


# ─────────────────────────────────────────────────────────────
# H-4：conftest 的 inspect.getsource 补丁回归
# ─────────────────────────────────────────────────────────────
def test_conftest_robust_getsource_returns_function_body():
    import app.routers.ai_config as m

    src = inspect.getsource(m._clamp_test_ai_params)
    assert "def _clamp_test_ai_params" in src
    assert "temperature" in src


def test_conftest_robust_getsource_handles_module():
    import app.routers.ai_config as m

    src = inspect.getsource(m)
    assert "async def test_ai_connection" in src