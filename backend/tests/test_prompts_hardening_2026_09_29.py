"""提示词模块增强回归测试（2026-09-29）。

覆盖三项能力：
  1. 读接口鉴权（GET /prompts、GET /prompts/{key} 在 AUTH_ENABLED=True 下需登录）
  2. 保存前变量校验（删除默认模板要求的关键占位符 → 400，force=true 可跳过）
  3. 版本控制与回滚（编辑/重置归档旧内容，可查询并回滚到历史版本）
"""
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.db import get_db
from app.routers import prompts as prompts_router
from app.security.auth import User, get_current_user
from app.services.ai.prompts import _ALL_PROMPTS, extract_variables


def _mock_user() -> User:
    return User(user_id=1, username="tester", role="admin")


@pytest.fixture()
def client():
    """CRUD/版本用例：直接覆盖鉴权依赖，与 AUTH_ENABLED 全局开关彻底解耦。"""
    app = FastAPI()
    app.include_router(prompts_router.router)
    app.dependency_overrides[get_current_user] = _mock_user
    return TestClient(app)


def _key_with_variables() -> str:
    """从注册表中挑选一个默认模板含变量的 key（保证校验用例稳定）。"""
    for key, meta in sorted(_ALL_PROMPTS.items()):
        if extract_variables(meta["content"]):
            return key
    raise AssertionError("注册表中找不到含变量的提示词")


def _strip_placeholders(text: str) -> str:
    import re

    return re.sub(r"\{[A-Za-z_]\w{1,}\}|__[A-Z_][A-Z0-9_]*__", "", text)


# ──────────────────────────────────────────────────────────────────────
# 1. 读接口鉴权
# ──────────────────────────────────────────────────────────────────────
def test_list_prompts_requires_auth_when_enabled():
    app = FastAPI()
    app.include_router(prompts_router.router)
    authed = TestClient(app)
    with patch("app.config.AUTH_ENABLED", True), patch(
        "app.services.auth_service.AUTH_ENABLED", True
    ):
        assert authed.get("/prompts").status_code == 401
        assert authed.get("/prompts/ANY_KEY").status_code == 401


def test_list_prompts_ok_when_auth_disabled(client):
    resp = client.get("/prompts")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list) and data
    # 每条含 variables 字段
    assert all("variables" in item for item in data)


# ──────────────────────────────────────────────────────────────────────
# 2. 保存前变量校验
# ──────────────────────────────────────────────────────────────────────
def test_update_preserving_variables_succeeds(client):
    key = _key_with_variables()
    default = _ALL_PROMPTS[key]["content"]
    resp = client.put(f"/prompts/{key}", json={"content": default})
    assert resp.status_code == 200


def test_update_dropping_variable_rejected(client):
    key = _key_with_variables()
    broken = _strip_placeholders(_ALL_PROMPTS[key]["content"])
    resp = client.put(f"/prompts/{key}", json={"content": broken})
    assert resp.status_code == 400
    assert "变量" in resp.json()["detail"]


def test_update_force_overrides_validation(client):
    key = _key_with_variables()
    broken = _strip_placeholders(_ALL_PROMPTS[key]["content"])
    resp = client.put(f"/prompts/{key}?force=true", json={"content": broken})
    assert resp.status_code == 200


def test_update_rejects_unknown_shared_ref(client):
    key = _key_with_variables()
    default = _ALL_PROMPTS[key]["content"]
    resp = client.put(
        f"/prompts/{key}", json={"content": default + "\n{shared:NOT_A_REAL_SHARED_RULE_XYZ}"}
    )
    assert resp.status_code == 400
    assert "共享规则" in resp.json()["detail"]


# ──────────────────────────────────────────────────────────────────────
# 3. 版本控制与回滚
# ──────────────────────────────────────────────────────────────────────
def test_edit_archives_previous_version_and_rollback(client):
    key = _key_with_variables()
    default = _ALL_PROMPTS[key]["content"]
    edited = default + "\n【自定义编辑标记 ABC123】"

    # 首次编辑：把旧的（DB 现有内容，可能为空）归档，写入 edited
    assert client.put(f"/prompts/{key}", json={"content": edited}).status_code == 200
    # 二次编辑：把 edited 归档
    edited2 = default + "\n【第二次编辑 DEF456】"
    assert client.put(f"/prompts/{key}", json={"content": edited2}).status_code == 200

    with get_db() as conn:
        cur = conn.execute(
            "SELECT content FROM prompt_templates WHERE key = ?", (key,)
        ).fetchone()
    assert "DEF456" in cur["content"]

    # 版本列表应包含被归档的 edited（含 ABC123）
    versions = client.get(f"/prompts/{key}/versions").json()
    assert versions
    archived_ids = [v["id"] for v in versions]

    # 找到归档了 ABC123 的那个版本
    target_id = None
    for vid in archived_ids:
        detail = client.get(f"/prompts/{key}/versions/{vid}").json()
        if "ABC123" in detail["content"]:
            target_id = vid
            break
    assert target_id is not None, "未能归档被覆盖的编辑内容"

    # 回滚到含 ABC123 的版本
    rb = client.post(f"/prompts/{key}/versions/{target_id}/rollback")
    assert rb.status_code == 200
    with get_db() as conn:
        after = conn.execute(
            "SELECT content FROM prompt_templates WHERE key = ?", (key,)
        ).fetchone()
    assert "ABC123" in after["content"]
    # 回滚前的 DEF456 也被归档（可再次回滚，非破坏性）
    versions_after = client.get(f"/prompts/{key}/versions").json()
    assert len(versions_after) >= 3


def test_reset_archives_custom_content(client):
    key = _key_with_variables()
    default = _ALL_PROMPTS[key]["content"]
    edited = default + "\n【重置前自定义 GHI789】"
    assert client.put(f"/prompts/{key}", json={"content": edited}).status_code == 200
    assert client.post(f"/prompts/{key}/reset").status_code == 200

    # reset 后当前内容回到默认（不含自定义标记）
    with get_db() as conn:
        cur = conn.execute(
            "SELECT content FROM prompt_templates WHERE key = ?", (key,)
        ).fetchone()
    assert "GHI789" not in cur["content"]

    # 但自定义内容被归档，可查询到
    versions = client.get(f"/prompts/{key}/versions").json()
    found = False
    for v in versions:
        if "GHI789" in client.get(f"/prompts/{key}/versions/{v['id']}").json()["content"]:
            found = True
            break
    assert found, "重置应归档被清除的自定义内容"


def test_get_detail_reflects_edited_variables(client):
    key = _key_with_variables()
    resp = client.get(f"/prompts/{key}")
    assert resp.status_code == 200
    assert "variables" in resp.json()
