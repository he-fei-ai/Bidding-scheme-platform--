"""认证依赖回归测试。

覆盖 FastAPI Depends 对 get_current_user 的注入行为，避免可选的无类型
request 参数退化为 None 后绕过认证。
"""
from unittest.mock import patch

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.security.auth import get_current_user


def test_get_current_user_dependency_receives_request():
    app = FastAPI()

    @app.get("/protected")
    async def protected(user=Depends(get_current_user)):
        return {"username": user.username}

    with patch("app.config.AUTH_ENABLED", True), patch("app.services.auth_service.AUTH_ENABLED", True):
        response = TestClient(app).get("/protected")

    assert response.status_code == 401
    assert response.json()["detail"] == "缺少授权令牌"
