"""JWT auth system critical path tests."""

import os
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from app.services.auth_service import (
    authenticate_user,
    change_password,
    create_access_token,
    decode_access_token,
    get_current_user_dependency,
    hash_password,
    verify_password,
)

_TEST_SECRET = "unit-test-jwt-secret-fixed-value"


@pytest.fixture(autouse=True)
def _setup_test_env(monkeypatch, tmp_path):
    """Use isolated SQLite + fixed JWT secret per test."""
    db_file = tmp_path / "test_auth.sqlite3"
    monkeypatch.setattr("app.db.DATABASE_PATH", db_file)
    monkeypatch.setattr("app.services.auth_service.DATA_DIR", tmp_path)
    monkeypatch.setenv("AUTH_JWT_SECRET", _TEST_SECRET)
    monkeypatch.setenv("AUTH_ENABLED", "false")
    # Patch module-level constant directly (no reload needed)
    import app.services.auth_service as _as
    _as.AUTH_ENABLED = False
    from app.db import init_db
    init_db()
    yield


@pytest.fixture
def admin_token():
    return create_access_token(1, "admin", "admin")


@pytest.fixture
def expired_token():
    import base64, hashlib, hmac, json
    now = int(time.time())
    header = base64.urlsafe_b64encode(
        json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode()
    ).rstrip(b"=").decode()
    payload = base64.urlsafe_b64encode(
        json.dumps({
            "sub": "1", "user_id": 1, "username": "admin",
            "role": "admin", "iat": now - 7200, "exp": now - 3600,
        }, separators=(",", ":")).encode()
    ).rstrip(b"=").decode()
    signing_input = f"{header}.{payload}"
    sig = base64.urlsafe_b64encode(
        hmac.new(_TEST_SECRET.encode(), signing_input.encode(), hashlib.sha256).digest()
    ).rstrip(b"=").decode()
    return f"{header}.{payload}.{sig}"


class TestPasswordHash:
    def test_hash_and_verify(self):
        pw = "SecurePass123!"
        salt_hex, hash_hex = hash_password(pw)
        assert len(salt_hex) == 64
        assert verify_password(pw, salt_hex, hash_hex) is True

    def test_wrong_password_rejected(self):
        pw = "CorrectPass99"
        salt_hex, hash_hex = hash_password(pw)
        assert verify_password("WrongPass", salt_hex, hash_hex) is False

    def test_deterministic_with_same_salt(self):
        salt = os.urandom(32)
        pw = "SameSaltTest"
        s1, h1 = hash_password(pw, salt)
        s2, h2 = hash_password(pw, salt)
        assert s1 == s2
        assert h1 == h2


class TestToken:
    def test_create_and_decode(self, admin_token):
        payload = decode_access_token(admin_token)
        assert payload["user_id"] == 1
        assert payload["username"] == "admin"
        assert payload["role"] == "admin"
        assert payload["exp"] > payload["iat"]

    def test_expired_token_rejected(self, expired_token):
        with pytest.raises(PermissionError, match="登录已过期"):
            decode_access_token(expired_token)

    def test_tampered_token_rejected(self, admin_token):
        parts = admin_token.split(".")
        parts[2] = "bbbb" + parts[2][4:]
        with pytest.raises(PermissionError, match="无效的登录凭证"):
            decode_access_token(".".join(parts))

    def test_invalid_format_rejected(self):
        for bad in ["not-a-token", "a.b", "a.b.c.d"]:
            with pytest.raises(PermissionError, match="无效的登录凭证"):
                decode_access_token(bad)


class TestAuthEndpoints:
    def test_login_success(self):
        resp = authenticate_user("admin", "admin123")
        assert resp is not None
        assert resp["username"] == "admin"
        assert resp["role"] == "admin"

    def test_login_wrong_password(self):
        assert authenticate_user("admin", "wrongpass") is None

    def test_login_unknown_user(self):
        assert authenticate_user("nobody", "anything") is None

    def test_change_password(self):
        assert change_password(1, "admin123", "NewPass123!") is True
        assert authenticate_user("admin", "admin123") is None
        assert authenticate_user("admin", "NewPass123!") is not None
        assert change_password(1, "NewPass123!", "admin123") is True

    def test_change_password_short_new(self):
        # pydantic min_length=8 is enforced at router layer; service accepts any length
        assert change_password(1, "admin123", "1234567") is True


class TestDependency:
    def test_no_token_disabled_mode(self):
        from starlette.requests import Request
        # query_string 是 ASGI scope 的必填键（真实请求一定有）；
        # 缺失时 starlette 的 Request.query_params 会抛 KeyError。
        req = Request(
            {
                "type": "http",
                "headers": [],
                "method": "GET",
                "path": "/",
                "query_string": b"",
            }
        )
        with patch("app.services.auth_service.AUTH_ENABLED", False):
            result = get_current_user_dependency(req)
        assert result == {"id": 1, "username": "admin", "role": "admin"}

    def test_invalid_token_always_401(self):
        from starlette.requests import Request
        req = Request({
            "type": "http",
            "headers": [(b"authorization", b"Bearer bad.token.here")],
            "method": "GET",
            "path": "/",
        })
        with pytest.raises(HTTPException) as exc_info:
            get_current_user_dependency(req)
        assert exc_info.value.status_code == 401

    def test_valid_token_returns_user(self, admin_token):
        from starlette.requests import Request
        req = Request({
            "type": "http",
            "headers": [(b"authorization", f"Bearer {admin_token}".encode())],
            "method": "GET",
            "path": "/",
        })
        with patch("app.services.auth_service.AUTH_ENABLED", True):
            result = get_current_user_dependency(req)
        assert result["username"] == "admin"
        assert result["role"] == "admin"


class TestJwtAuthMiddleware:
    @pytest.fixture
    def app_with_middleware(self, admin_token):
        inner = FastAPI()

        @inner.get("/protected")
        async def protected():
            return {"ok": True}

        @inner.get("/public")
        async def public():
            return {"ok": True}

        @inner.get("/health")
        async def health():
            return {"status": "ok"}

        class JwtAuthMiddleware:
            PUBLIC_PREFIXES = (
                "/auth/login", "/auth/me", "/health",
                "/api/docs", "/api/redoc", "/api/openapi.json",
                "/api/v1/files/", "/favicon",
            )

            def __init__(self, app):
                self.app = app

            async def __call__(self, scope, receive, send):
                if scope["type"] != "http":
                    await self.app(scope, receive, send)
                    return
                path = scope["path"]
                if any(path.startswith(p) for p in self.PUBLIC_PREFIXES):
                    await self.app(scope, receive, send)
                    return
                headers = {k.decode(): v.decode() for k, v in scope.get("headers", [])}
                auth = headers.get("authorization", "")
                if not auth.startswith("Bearer "):
                    body = b'{"detail":"missing token"}'
                    await send({"type": "http.response.start", "status": 401,
                                "headers": [[b"content-type", b"application/json"]]})
                    await send({"type": "http.response.body", "body": body})
                    return
                token = auth[7:].strip()
                try:
                    payload = decode_access_token(token)
                    scope["auth_user"] = payload
                except PermissionError as exc:
                    body = str(exc).encode()
                    await send({"type": "http.response.start", "status": 401,
                                "headers": [[b"content-type", b"application/json"]]})
                    await send({"type": "http.response.body", "body": body})
                    return
                await self.app(scope, receive, send)

        inner.add_middleware(JwtAuthMiddleware)
        return inner

    def test_protected_without_token_401(self, app_with_middleware):
        client = TestClient(app_with_middleware)
        resp = client.get("/protected")
        assert resp.status_code == 401

    def test_protected_with_valid_token_200(self, app_with_middleware, admin_token):
        client = TestClient(app_with_middleware)
        resp = client.get("/protected", headers={"Authorization": f"Bearer {admin_token}"})
        assert resp.status_code == 200

    def test_public_prefix_always_allowed(self, app_with_middleware):
        client = TestClient(app_with_middleware)
        resp = client.get("/health")
        assert resp.status_code == 200
