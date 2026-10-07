"""回归测试（2026-09-17）：AI 配置模块深度探索后的缺陷修复

覆盖的缺陷与改动文件（只测行为，不测实现细节）：

A. `app/services/ai/profile_service.py`（密钥口径统一）
   A1. `get_profile` / `get_default_profile` 只在 `api_key_enc` 是 ENC: 密文时
       才生成 `api_key` —— 明文存储的 Profile 既缺 `api_key`（`POST /profiles`
       响应校验直接 500），又把明文密钥留在 `api_key_enc` 里被 GET 端点原样吐出。
   A2. `apply_profile_to_config` 只读 `api_key_enc`，而 `get_profile` 在密文分支
       已删除该键 → 「应用 Profile」实际把 ai_config.api_key 更新为空串。
   A3. `export_profiles` 用 `SELECT *` 把明文密钥导出到 JSON 文件。
   A4. `import_profiles` 读 `api_key`，v1.0 导出文件放在 `api_key_enc` → 密钥丢失。
   A5. `update_profile` 空字符串经 COALESCE 写入空值，静默清空已保存密钥；
       `json_mode` 未传时固定写 0，悄悄关闭 JSON 模式。
   A6. `delete_profile` 硬编码回退到名为 'default' 的记录，可能落到不存在的行。

B. `app/routers/ai_config.py`
   B1. `quick_setup` 把用户输入的**明文 API Key** 放进响应回传。
   B2. `quick_setup` 的 base_url 兜底过滤在已清空的列表上执行，恒为空 → 400。
   B3. `test_image_connection` 不解析脱敏占位符，已配置厂商测试必然失败。
   B4. `save_image_model_config` 解析失败时静默把占位符当作密钥落库。
   B5. `get_image_usage_stats` 分母带 `project_id IS NOT NULL` 而分子不带
       → 成功率可以超过 100%。

C. `app/services/ai/ai_config_service.py`
   C1. `cleanup_old_history` 返回 `conn.total_changes`（连接累计变更数），虚高。
   C2. `_schedule_cleanup` 重复调用会丢弃旧任务引用，产生孤儿协程。
"""

from __future__ import annotations

from datetime import datetime, timedelta
import uuid

import pytest

from app.db import get_db
from app.services.crypto import encrypt_api_key

# 本文件创建的测试 Profile 统一前缀，teardown 时按前缀清理
_PREFIX = "t17_"


def _new_name(tag: str) -> str:
    return f"{_PREFIX}{tag}_{uuid.uuid4().hex[:8]}"


@pytest.fixture
def cleanup_test_profiles():
    yield
    with get_db() as conn:
        conn.execute("DELETE FROM ai_profiles WHERE name LIKE ?", (f"{_PREFIX}%",))
        conn.commit()


# ---------------------------------------------------------------------------
# A. profile_service 密钥口径
# ---------------------------------------------------------------------------

def test_get_profile_always_exposes_api_key_and_never_api_key_enc(cleanup_test_profiles):
    """A1：明文与密文两种存储形态下，`api_key` 都必须存在、`api_key_enc` 必须消失。"""
    from app.services.ai.profile_service import create_profile, get_profile

    plain_name = _new_name("plain")
    enc_name = _new_name("enc")

    create_profile({"name": plain_name, "api_key": "sk-plain-secret"})
    create_profile({"name": enc_name, "api_key": "sk-enc-secret"})

    # 把其中一个改成 ENC: 密文，覆盖历史加密数据
    with get_db() as conn:
        conn.execute(
            "UPDATE ai_profiles SET api_key_enc = ? WHERE name = ?",
            (encrypt_api_key("sk-enc-secret"), enc_name),
        )
        conn.commit()

    for name, expect in ((plain_name, "sk-plain-secret"), (enc_name, "sk-enc-secret")):
        prof = get_profile(name, include_key=True)
        assert prof is not None
        assert "api_key_enc" not in prof, "任何情况下都不应把密钥原文字段名暴露出去"
        assert prof["api_key"] == expect


def test_get_profile_masks_key_for_http_by_default(cleanup_test_profiles):
    """A1（安全）：默认（面向 HTTP）返回脱敏占位符，不能拿到密钥原文。"""
    from app.services.ai.profile_service import create_profile, get_profile

    name = _new_name("mask")
    create_profile({"name": name, "api_key": "sk-should-not-leak"})

    prof = get_profile(name)
    assert prof["api_key"] == "***已配置***"
    assert "sk-should-not-leak" not in repr(prof)


def test_created_profile_satisfies_profile_response_model(cleanup_test_profiles):
    """A1：`POST /profiles` 的 response_model 要求 api_key 必填，缺字段会 500。"""
    from app.routers.ai_config import ProfileResponse
    from app.services.ai.profile_service import create_profile

    name = _new_name("resp")
    profile = create_profile({"name": name, "api_key": "sk-x"})

    # 不应抛出 pydantic 校验错误
    model = ProfileResponse(**profile)
    assert model.name == name
    assert model.api_key  # 非空即可（脱敏占位符）


def test_apply_profile_preserves_stored_api_key(cleanup_test_profiles):
    """A2：应用 Profile 必须把该 Profile 的密钥写进 ai_config，而不是清空。"""
    from app.services.ai.profile_service import apply_profile_to_config, create_profile

    name = _new_name("apply")
    create_profile({"name": name, "api_key": "sk-profile-key", "model": "m1"})

    snapshot = None
    try:
        with get_db(readonly=True) as c:
            row = c.execute("SELECT * FROM ai_config WHERE id = 1").fetchone()
            snapshot = dict(row) if row else None

        apply_profile_to_config(name)

        with get_db(readonly=True) as c:
            row = c.execute("SELECT api_key FROM ai_config WHERE id = 1").fetchone()
        assert row is not None
        # 存储形态无关断言（2026-10-06 S-2 更新）：断言的是「该 Profile 的密钥被
        # 正确写入 ai_config 而不是清空」，用decrypt_api_key 兼容明文/ENC: 双形态
        # （旧断言直接比对原文，等于把「明文落库」的旧行为固化进测试）。
        from app.services.crypto import decrypt_api_key, is_encrypted

        stored = row["api_key"] or ""
        assert decrypt_api_key(stored) == "sk-profile-key", "应用 Profile 把主配置密钥清空了"
        # S-2：写入形态应为 ENC: 加密（读取侧透明解密，行为不变）
        assert is_encrypted(stored), "应用 Profile 后主配置密钥应加密落库"
    finally:
        # 还原 ai_config
        with get_db() as c:
            c.execute("DELETE FROM ai_config WHERE id = 1")
            if snapshot:
                cols = ", ".join(snapshot.keys())
                marks = ", ".join("?" for _ in snapshot)
                c.execute(
                    f"INSERT INTO ai_config ({cols}) VALUES ({marks})",
                    tuple(snapshot.values()),
                )
            c.commit()


def test_export_profiles_contains_no_secret(cleanup_test_profiles):
    """A3：导出内容不得包含任何密钥原文，只标注 api_key_set。"""
    from app.services.ai.profile_service import create_profile, export_profiles

    name = _new_name("export")
    create_profile({"name": name, "api_key": "sk-export-secret"})

    data = export_profiles()
    blob = repr(data)
    assert "sk-export-secret" not in blob
    assert "api_key_enc" not in blob
    target = next(p for p in data["profiles"] if p["name"] == name)
    assert target.get("api_key_set") is True


def test_import_profiles_restores_legacy_api_key_enc(cleanup_test_profiles):
    """A4：v1.0 导出文件把密钥放在 api_key_enc，导入时必须还原成真实密钥。"""
    from app.services.ai.profile_service import get_profile, import_profiles

    name = _new_name("import")
    result = import_profiles(
        {
            "version": "1.0",
            "profiles": [
                {
                    "name": name,
                    "display_name": "导入测试",
                    "api_key_enc": "sk-legacy-import",
                    "model": "gpt-4o",
                }
            ],
        }
    )
    assert result["imported"] == 1

    prof = get_profile(name, include_key=True)
    assert prof is not None
    assert prof["api_key"] == "sk-legacy-import"


def test_import_profiles_rejects_non_list_payload():
    """A4（健壮性）：profiles 不是数组时应给出明确错误，而不是静默成功。"""
    from app.services.ai.profile_service import import_profiles

    with pytest.raises(ValueError):
        import_profiles({"profiles": "not-a-list"})


def test_update_profile_keeps_key_and_json_mode_when_not_provided(cleanup_test_profiles):
    """A5：留空 = 保持原值；未提供的开关不应被重置。"""
    from app.services.ai.profile_service import create_profile, get_profile, update_profile

    name = _new_name("update")
    create_profile({"name": name, "api_key": "sk-keep-me", "json_mode": True})

    # 只改模型，不传 api_key / json_mode
    update_profile(name, {"model": "gpt-5"})

    prof = get_profile(name, include_key=True)
    assert prof["api_key"] == "sk-keep-me", "空 api_key 不应清空已保存密钥"
    assert prof["model"] == "gpt-5"
    assert bool(prof["json_mode"]) is True, "未传 json_mode 时不应被改成关闭"

    # 显式传空串同样保持原值（前端「留空则保持原值」语义）
    update_profile(name, {"api_key": ""})
    assert get_profile(name, include_key=True)["api_key"] == "sk-keep-me"


def test_delete_default_profile_promotes_remaining(cleanup_test_profiles):
    """A6：删除默认 Profile 后，必须仍有激活的默认 Profile。"""
    from app.services.ai.profile_service import (
        create_profile,
        delete_profile,
        get_default_profile,
        set_default_profile,
    )

    a = _new_name("dflt_a")
    b = _new_name("dflt_b")
    create_profile({"name": a})
    create_profile({"name": b})
    set_default_profile(a)

    assert delete_profile(a) is True

    current = get_default_profile()
    assert current is not None, "删除默认 Profile 后没有任何默认配置了"
    assert current["name"] != a, "默认位仍指向已删除的 Profile"
    # 被顶上来的必须是仍处于激活状态的 Profile
    assert current["is_active"] == 1


def test_list_profiles_has_no_secret_field(cleanup_test_profiles):
    """A1（连带）：列表接口返回的每行都不能带密钥字段。"""
    from app.services.ai.profile_service import create_profile, list_profiles

    create_profile({"name": _new_name("list"), "api_key": "sk-list-secret"})

    for p in list_profiles():
        assert "api_key_enc" not in p
        assert "sk-list-secret" not in repr(p)


# ---------------------------------------------------------------------------
# B. routers/ai_config.py
# ---------------------------------------------------------------------------

class _FakeResp:
    def __init__(self, status_code: int = 200, text: str = ""):
        self.status_code = status_code
        self.text = text

    def json(self):
        return {"data": []}


class _FakeAsyncClient:
    """替换 httpx.AsyncClient：不发起真实网络请求。"""

    status_code = 200

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, *args, **kwargs):
        return _FakeResp(_FakeAsyncClient.status_code)

    async def get(self, *args, **kwargs):
        return _FakeResp(_FakeAsyncClient.status_code)


@pytest.fixture
def restore_ai_config():
    snapshot = None
    with get_db(readonly=True) as c:
        row = c.execute("SELECT * FROM ai_config WHERE id = 1").fetchone()
        snapshot = dict(row) if row else None
    yield
    with get_db() as c:
        c.execute("DELETE FROM ai_config WHERE id = 1")
        if snapshot:
            cols = ", ".join(snapshot.keys())
            marks = ", ".join("?" for _ in snapshot)
            c.execute(
                f"INSERT INTO ai_config ({cols}) VALUES ({marks})",
                tuple(snapshot.values()),
            )
        c.commit()


@pytest.mark.asyncio
async def test_quick_setup_does_not_return_plaintext_key(monkeypatch, restore_ai_config):
    """B1（安全 P0）：快速配置的响应里不能出现用户刚输入的 API Key。"""
    from app.routers import ai_config as ai_config_router

    _FakeAsyncClient.status_code = 200
    monkeypatch.setattr(ai_config_router.httpx, "AsyncClient", _FakeAsyncClient)

    secret = "sk-quick-setup-SECRET"
    result = await ai_config_router.quick_setup(
        ai_config_router.QuickSetupRequest(api_key=secret)
    )

    assert result.success is True
    assert secret not in repr(result.applied_config)
    assert result.applied_config["api_key_set"] is True


@pytest.mark.asyncio
async def test_quick_setup_matches_provider_by_base_url(monkeypatch, restore_ai_config):
    """B2：provider_name 不是精确展示名时，应落到 base_url 兜底匹配而不是 400。"""
    from app.routers import ai_config as ai_config_router

    _FakeAsyncClient.status_code = 401
    monkeypatch.setattr(ai_config_router.httpx, "AsyncClient", _FakeAsyncClient)

    # "deepseek" 不是展示名（展示名是 "DeepSeek"），旧实现在这里恒返回 400
    result = await ai_config_router.quick_setup(
        ai_config_router.QuickSetupRequest(api_key="sk-any", provider_name="deepseek")
    )

    assert result.tested_providers, "base_url 兜底分支应至少测试到一个提供商"
    assert any("deepseek" in (p.get("base_url") or "").lower() for p in result.tested_providers)


@pytest.mark.asyncio
async def test_quick_setup_rejects_unknown_provider(monkeypatch, restore_ai_config):
    """B2（对称场景）：真正未知的提供商仍然要 400。"""
    from fastapi import HTTPException

    from app.routers import ai_config as ai_config_router

    _FakeAsyncClient.status_code = 401
    monkeypatch.setattr(ai_config_router.httpx, "AsyncClient", _FakeAsyncClient)

    with pytest.raises(HTTPException) as exc:
        await ai_config_router.quick_setup(
            ai_config_router.QuickSetupRequest(
                api_key="sk-any", provider_name="not-a-real-provider-xyz"
            )
        )
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_test_image_connection_resolves_masked_key(monkeypatch):
    """B3：提交脱敏占位符时，应从该厂商已保存配置解析出真实密钥再去连通测试。"""
    from app.routers import ai_config as ai_config_router
    from app.services.ai.image_providers.registry import ProviderRegistry

    provider = "unit_test_provider"
    saved_key = "sk-image-real-key"

    class _FakeAdapter:
        seen_key = None
        base_url = "https://example.com/v1"

        def __init__(self, config):
            self.config = config

        async def test_connection(self, api_key=None, base_url=None):
            _FakeAdapter.seen_key = api_key
            return True

    with get_db() as conn:
        conn.execute("DELETE FROM image_provider_configs WHERE provider_name = ?", (provider,))
        conn.execute(
            "INSERT INTO image_provider_configs (provider_name, api_key, base_url, model) "
            "VALUES (?, ?, ?, ?)",
            (provider, saved_key, "https://example.com/v1", "m"),
        )
        conn.commit()

    monkeypatch.setitem(ProviderRegistry._adapters, provider, _FakeAdapter)
    try:
        result = await ai_config_router.test_image_connection(
            ai_config_router.ImageModelConfigRequest(
                provider=provider, api_key="***已配置***"
            )
        )
        assert result["status"] == "ok"
        assert _FakeAdapter.seen_key == saved_key
    finally:
        with get_db() as conn:
            conn.execute(
                "DELETE FROM image_provider_configs WHERE provider_name = ?", (provider,)
            )
            conn.commit()


@pytest.mark.asyncio
async def test_save_image_model_config_rejects_unresolvable_mask():
    """B4：占位符无法解析时必须报错，绝不能把 '***已配置***' 当作密钥落库。"""
    from fastapi import HTTPException

    from app.routers import ai_config as ai_config_router

    unknown_user = 987654321

    with pytest.raises(HTTPException) as exc:
        await ai_config_router.save_image_model_config(
            ai_config_router.ImageModelConfigRequest(api_key="***已配置***"),
            user_id=unknown_user,
        )
    assert exc.value.status_code == 400

    with get_db(readonly=True) as conn:
        row = conn.execute(
            "SELECT image_api_key_enc FROM model_configs WHERE user_id = ?", (unknown_user,)
        ).fetchone()
    assert row is None or "***" not in (row[0] or "")


@pytest.mark.asyncio
async def test_image_usage_stats_rate_never_exceeds_100():
    """B5：成功率分子分母口径必须一致，且补齐 failed_count。"""
    from app.routers import ai_config as ai_config_router

    stats = await ai_config_router.get_image_usage_stats(user_id=1)

    assert 0 <= stats["success_rate"] <= 100
    assert "failed_count" in stats
    assert stats["success_count"] + stats["failed_count"] <= stats["total"]


# ---------------------------------------------------------------------------
# C. ai_config_service
# ---------------------------------------------------------------------------

def test_cleanup_old_history_returns_real_deleted_rows():
    """C1：返回值必须是本次 DELETE 的行数，而不是连接累计变更数。"""
    from app.services.ai.ai_config_service import cleanup_old_history

    cutoff = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
    marker_a = f"{_PREFIX}old_a"
    marker_b = f"{_PREFIX}old_b"
    old_ts = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d %H:%M:%S")

    with get_db() as conn:
        for marker in (marker_a, marker_b):
            conn.execute("DELETE FROM provider_history WHERE provider_name = ?", (marker,))
            conn.execute(
                "INSERT INTO provider_history (provider_name, provider_type, base_url,"
                " success, failed, last_used_at) VALUES (?, ?, ?, ?, ?, ?)",
                (marker, "text", "https://old.example.com", 1, 0, old_ts),
            )
        conn.commit()

    try:
        with get_db(readonly=True) as conn:
            expected = conn.execute(
                "SELECT COUNT(*) FROM provider_history WHERE last_used_at < ?", (cutoff,)
            ).fetchone()[0]

        deleted = cleanup_old_history(30)

        assert expected >= 2
        assert deleted == expected, (
            "cleanup_old_history 返回值与真实删除行数不符"
            "（误用 conn.total_changes 会远大于 expected）"
        )
    finally:
        with get_db() as conn:
            conn.execute(
                "DELETE FROM provider_history WHERE provider_name IN (?, ?)",
                (marker_a, marker_b),
            )
            conn.commit()


@pytest.mark.asyncio
async def test_schedule_cleanup_is_idempotent():
    """C2：重复注册不能覆盖已有任务引用而产生孤儿协程。"""
    import app.services.ai.ai_config_service as svc

    previous = svc._cleanup_task
    try:
        svc._cleanup_task = None
        svc._schedule_cleanup()
        first = svc._cleanup_task
        svc._schedule_cleanup()
        second = svc._cleanup_task

        assert first is not None
        assert first is second, "重复注册应复用同一个后台任务"
        first.cancel()
    finally:
        svc._cleanup_task = previous


def test_shutdown_cancels_cleanup_task():
    """C2（连带）：shutdown 应把任务引用置空，便于事件循环干净退出。"""
    import app.services.ai.ai_config_service as svc

    previous = svc._cleanup_task
    try:
        svc._cleanup_task = None
        svc.shutdown()
        assert svc._cleanup_task is None
    finally:
        svc._cleanup_task = previous
