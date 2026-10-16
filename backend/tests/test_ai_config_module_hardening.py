"""
AI 配置模块加固回归测试

覆盖：
  1. PUT /ai/config 乐观锁（If-Match / ETag：缺失→400，过期→409，命中→200 并返回新 ETag/Version）
  2. API Key 轮转：保存新密钥时记录 key_rotation 审计 + 写入 key_rotated_at，/ai/config/rotation-status 到期提醒
  3. GET /ai/config/export：不带密钥的公开导出；include_secret=true 需 admin，否则 403
  4. GET /ai/config/stream：SSE 版本变更推送
  5. PUT /ai/profiles/{name} 乐观锁（409 冲突、200 带 version/etag）
"""

from __future__ import annotations

import asyncio
import json
import types
import uuid
from datetime import datetime, timedelta
from unittest import mock

import pytest
from fastapi.testclient import TestClient

from app.db import get_db_with_retry
from app.main import app
from app.routers import ai_config as ac
from app.security.auth import User
import app.services.ai.provider_factory as pf
from app.services.ai.profile_service import (
    ConflictError,
    create_profile as _create_profile,
    delete_profile as _delete_profile,
    update_profile as _update_profile,
)

client = TestClient(app)
_PREFIX = "th_"
_KEY_ROTATED_AT_STATE_KEY = "ai_config_key_rotated_at"


def _pname(tag: str) -> str:
    return f"{_PREFIX}{tag}_{uuid.uuid4().hex[:8]}"


def _current_version() -> int:
    return pf.get_ai_config_version()


def _reset_rotation_timestamp() -> None:
    with get_db_with_retry() as conn:
        conn.execute(
            "DELETE FROM ai_config_state WHERE key = ?", (_KEY_ROTATED_AT_STATE_KEY,)
        )
        conn.commit()


def _last_key_rotation_audit():
    with get_db_with_retry(readonly=True) as conn:
        rows = conn.execute(
            "SELECT actor, action, target, detail, created_at FROM ai_config_audit "
            "WHERE action = 'key_rotation' ORDER BY id DESC LIMIT 1"
        ).fetchall()
    return rows[0] if rows else None


@pytest.fixture(autouse=True)
def _cleanup_profiles():
    yield
    with get_db_with_retry() as conn:
        conn.execute("DELETE FROM ai_profiles WHERE name LIKE ?", (f"{_PREFIX}%",))
        conn.commit()


# ══════════════════════════════════════════════════════════════════════
# 1. PUT /ai/config 乐观锁（If-Match / ETag）
# ══════════════════════════════════════════════════════════════════════


def test_get_config_returns_version_and_etag():
    r = client.get("/api/v1/ai/config")
    assert r.status_code == 200, r.text
    body = r.json()
    assert isinstance(body.get("version"), int)
    assert body.get("etag") == str(body["version"])


def test_put_config_missing_if_match_returns_400():
    before = _current_version()
    r = client.put(
        "/api/v1/ai/config",
        json={"base_url": "https://api.example.com/v1", "api_key": "sk-missing-etag-key"},
    )
    assert r.status_code == 400, r.text
    assert "If-Match" in r.json()["detail"]
    assert _current_version() == before  # 未写入，版本号不变


def test_put_config_stale_if_match_returns_409():
    r = client.get("/api/v1/ai/config")
    current = r.json()["version"]
    r = client.put(
        "/api/v1/ai/config",
        json={"base_url": "https://api.example.com/v1", "api_key": "sk-stale-key"},
        headers={"If-Match": str(current + 1000)},
    )
    assert r.status_code == 409, r.text
    assert "冲突" in r.json()["detail"]


def test_put_config_ok_returns_new_etag_and_version():
    r = client.get("/api/v1/ai/config")
    current = r.json()["version"]
    payload = {
        "base_url": "https://rt.example.com/v1",
        "api_key": "sk-rt-roundtrip-key",
        "model": "rt-model",
        "temperature": 0.5,
    }
    r = client.put(
        "/api/v1/ai/config",
        json=payload,
        headers={"If-Match": str(current)},
    )
    assert r.status_code == 200, r.text
    assert r.headers["etag"] == str(current + 1)
    assert r.headers["version"] == str(current + 1)
    assert r.json()["version"] == current + 1
    # 后续 GET 可见新版本
    r = client.get("/api/v1/ai/config")
    assert r.json()["version"] == current + 1


# ══════════════════════════════════════════════════════════════════════
# 2. API Key 轮转：审计 + key_rotated_at + rotation-status
# ══════════════════════════════════════════════════════════════════════


def test_put_config_key_change_records_rotation_audit_and_timestamp():
    _reset_rotation_timestamp()
    assert pf.get_key_rotated_at() is None

    r = client.get("/api/v1/ai/config")
    current = r.json()["version"]
    r = client.put(
        "/api/v1/ai/config",
        json={"base_url": "https://rotation.example.com/v1", "api_key": "sk-rotation-new-key-01"},
        headers={"If-Match": str(current)},
    )
    assert r.status_code == 200, r.text

    assert pf.get_key_rotated_at() is not None
    audit = _last_key_rotation_audit()
    assert audit is not None
    assert audit["action"] == "key_rotation"
    assert "API key rotated" in audit["detail"]

    r = client.get("/api/v1/ai/config/rotation-status")
    assert r.status_code == 200, r.text
    st = r.json()
    assert st["api_key_set"] is True
    assert st["rotation_due"] is False  # 刚轮转过，未到期
    assert st["days_since_rotation"] == 0


def test_rotation_status_shape():
    _reset_rotation_timestamp()
    r = client.get("/api/v1/ai/config/rotation-status")
    assert r.status_code == 200, r.text
    st = r.json()
    for key in ("rotation_interval_days", "days_since_rotation", "rotation_due", "api_key_set"):
        assert key in st, st
    assert st["rotation_interval_days"] == 90
    assert st["days_since_rotation"] is None
    assert st["rotation_due"] is False


def test_rotation_due_after_interval():
    _reset_rotation_timestamp()
    old = (datetime.now() - timedelta(days=100)).isoformat()
    pf.set_key_rotated_at(old)
    try:
        r = client.get("/api/v1/ai/config/rotation-status")
        assert r.status_code == 200, r.text
        st = r.json()
        assert st["rotation_due"] is True
        assert st["days_since_rotation"] is not None
        assert st["days_since_rotation"] >= 90
    finally:
        pf.set_key_rotated_at(datetime.now().isoformat())


# ══════════════════════════════════════════════════════════════════════
# 3. GET /ai/config/export（密钥口径 + admin RBAC）
# ══════════════════════════════════════════════════════════════════════


def test_export_without_secret_public():
    r = client.get("/api/v1/ai/config/export")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["format"] == "ai-config-v1"
    assert body["keys_included"] is False
    assert "api_key" not in body["config"]
    assert isinstance(body["config_version"], int)
    assert body["config_version"] == pf.get_ai_config_version()


def test_export_with_secret_requires_admin(monkeypatch):
    monkeypatch.setattr(
        ac, "get_current_user", lambda request: User(user_id=2, username="user", role="user")
    )
    r = client.get("/api/v1/ai/config/export?include_secret=true")
    assert r.status_code == 403, r.text


def test_export_with_secret_returns_key():
    r = client.get("/api/v1/ai/config")
    current = r.json()["version"]
    r = client.put(
        "/api/v1/ai/config",
        json={"base_url": "https://export.example.com/v1", "api_key": "sk-export-secret-key"},
        headers={"If-Match": str(current)},
    )
    assert r.status_code == 200, r.text

    r = client.get("/api/v1/ai/config/export?include_secret=true")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["keys_included"] is True
    assert body["config"]["api_key"] == "sk-export-secret-key"
    assert body["config_version"] == current + 1


# ══════════════════════════════════════════════════════════════════════
# 4. GET /ai/config/stream SSE
# ══════════════════════════════════════════════════════════════════════


class _FakeRequest:
    def __init__(self, allow_first: bool = False):
        # allow_first=False：首次 is_disconnected() 即 True（客户端立即断开）
        # allow_first=True：首次 False，之后立即 True（模拟收到 1 条 event 后断开）
        self._allow_first = allow_first
        self._calls = 0

    async def is_disconnected(self):
        self._calls += 1
        if self._calls == 1:
            return not self._allow_first
        return True


@pytest.mark.asyncio
async def test_sse_stream_emits_config_changed_on_version_bump(monkeypatch):
    versions = iter([1, 2])
    monkeypatch.setattr(ac, "get_ai_config_version", lambda: next(versions))

    async def _fast_sleep(_):
        return None

    monkeypatch.setattr(ac, "asyncio", types.SimpleNamespace(sleep=_fast_sleep))

    req = _FakeRequest(allow_first=True)
    resp = await ac.stream_ai_config(req)
    assert resp.media_type == "text/event-stream"
    assert resp.headers.get("Cache-Control") == "no-cache"

    chunks = [c async for c in resp.body_iterator]
    text = "".join(chunks)
    assert "config-changed" in text
    line = [ln for ln in text.splitlines() if ln.startswith("data: ")][0]
    payload = json.loads(line[len("data: "):])
    assert payload["event"] == "config-changed"
    assert payload["version"] == 2
    assert "ts" in payload


@pytest.mark.asyncio
async def test_sse_stream_stops_on_disconnect(monkeypatch):
    monkeypatch.setattr(ac, "get_ai_config_version", lambda: 7)

    async def _fast_sleep(_):
        return None

    monkeypatch.setattr(ac, "asyncio", types.SimpleNamespace(sleep=_fast_sleep))

    req = _FakeRequest()  # 首次即断开
    resp = await ac.stream_ai_config(req)
    chunks = [c async for c in resp.body_iterator]
    assert chunks == []  # 立即断开，不产生任何 event


# ══════════════════════════════════════════════════════════════════════
# 5. PUT /ai/profiles/{name} 乐观锁（与 ai_config 共享版本）
# ══════════════════════════════════════════════════════════════════════


def test_profile_endpoint_etag_roundtrip_and_conflict_409():
    name = _pname("lock")
    _create_profile(
        {
            "name": name,
            "provider_name": "agnes",
            "provider": "openai_compatible",
            "api_key": "sk-profile-lock",
            "model": "m1",
        }
    )

    r = client.get(f"/api/v1/ai/profiles/{name}")
    assert r.status_code == 200, r.text
    v0 = r.json()["version"]
    assert r.json()["etag"] == str(v0)

    # 第一次正常保存：版本号推进
    r = client.put(
        f"/api/v1/ai/profiles/{name}",
        json={"temperature": 0.5},
        headers={"If-Match": str(v0)},
    )
    assert r.status_code == 200, r.text
    v1 = r.json()["version"]
    assert v1 == v0 + 1
    assert r.json()["etag"] == str(v1)

    # 旧 etag 再保存 → 409 冲突
    r = client.put(
        f"/api/v1/ai/profiles/{name}",
        json={"temperature": 0.9},
        headers={"If-Match": str(v0)},
    )
    assert r.status_code == 409, r.text

    # 刷新 etag 后可再次保存
    r = client.put(
        f"/api/v1/ai/profiles/{name}",
        json={"temperature": 1.1},
        headers={"If-Match": str(v1)},
    )
    assert r.status_code == 200, r.text


def test_profile_service_update_profile_conflict_error():
    name = _pname("svc")
    _create_profile(
        {
            "name": name,
            "provider_name": "agnes",
            "provider": "openai_compatible",
            "api_key": "sk-profile-svc",
            "model": "m1",
        }
    )
    current = pf.get_ai_config_version()
    _update_profile(name, {"temperature": 0.3}, if_match=current)
    with pytest.raises(ConflictError):
        _update_profile(name, {"temperature": 0.6}, if_match=current)  # 已过期


def test_profile_list_and_default_include_version():
    name = _pname("ver")
    _create_profile(
        {
            "name": name,
            "provider_name": "agnes",
            "provider": "openai_compatible",
            "api_key": "sk-profile-ver",
            "model": "mv",
        }
    )

    r = client.get("/api/v1/ai/profiles")
    assert r.status_code == 200, r.text
    mine = [p for p in r.json() if p["name"] == name]
    assert mine and "version" in mine[0] and "etag" in mine[0]

    r = client.get(f"/api/v1/ai/profiles/{name}")
    assert r.status_code == 200, r.text
    assert "version" in r.json() and "etag" in r.json()

    r = client.get("/api/v1/ai/profiles/default")
    assert r.status_code == 200, r.text
    assert "version" in r.json() and "etag" in r.json()