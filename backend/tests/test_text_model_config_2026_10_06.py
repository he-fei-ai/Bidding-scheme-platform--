"""文本模型配置模块回归测试（2026-10-06 批次）

锁定本轮修复项，均遵循「零迁移 / 向后兼容」原则：

  S-1  db_migrations.unify_api_key_encryption：存量明文 API Key 启动时统一
       加密为 ENC:（修复旧迁移 #32 每次重启把密钥解密回明文、抵消 F-1
       加密修复的安全 P0 缺陷）；幂等可重复执行。
  S-2  profile_service.apply_profile_to_config：应用 Profile 后主配置密钥
       保持 ENC: 加密落库（此前 include_key=True 拿到明文直接写 ai_config）。
  S-3  图像配置写路径（PUT /ai/image-model、activate_image_provider_config、
       save_image_provider_config）：新密钥加密落库、同步主表保留库内原形态，
       不再解密为明文。
  B-1  fetch_model_info 元数据探测失败必须记日志（源码级自证，防回归为
       静默 except pass）。
  P-1  tasks.generate_sections 内降级链健康检查经 asyncio.to_thread 派发，
       不阻塞事件循环（源码级自证）。
"""

from __future__ import annotations

import inspect

import pytest

from app.db import get_db


# ---------------------------------------------------------------------------
# fixtures：快照/恢复，避免污染开发库
# ---------------------------------------------------------------------------
def _snapshot_rows(conn, sql: str) -> list[dict]:
    return [dict(r) for r in conn.execute(sql).fetchall()]


def _restore_rows(conn, table: str, rows: list[dict]) -> None:
    conn.execute(f"DELETE FROM {table}")
    for row in rows:
        cols = ", ".join(row.keys())
        marks = ", ".join("?" for _ in row)
        conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", tuple(row.values()))


@pytest.fixture
def restore_ai_config():
    with get_db(readonly=True) as c:
        rows = _snapshot_rows(c, "SELECT * FROM ai_config")
    yield
    with get_db() as c:
        _restore_rows(c, "ai_config", rows)
        c.commit()
    from app.services.ai.provider_factory import _clear_ai_config_cache

    _clear_ai_config_cache()


@pytest.fixture
def restore_ai_profiles():
    with get_db(readonly=True) as c:
        rows = _snapshot_rows(c, "SELECT * FROM ai_profiles")
    yield
    with get_db() as c:
        _restore_rows(c, "ai_profiles", rows)
        c.commit()


@pytest.fixture
def restore_image_tables():
    with get_db(readonly=True) as c:
        ipc = _snapshot_rows(c, "SELECT * FROM image_provider_configs")
        mc = _snapshot_rows(c, "SELECT * FROM model_configs")
    yield
    with get_db() as c:
        _restore_rows(c, "image_provider_configs", ipc)
        _restore_rows(c, "model_configs", mc)
        c.commit()


def _raw_ai_config_key() -> str:
    with get_db(readonly=True) as c:
        row = c.execute("SELECT api_key FROM ai_config WHERE id = 1").fetchone()
    return (row["api_key"] if row else "") or ""


# ---------------------------------------------------------------------------
# S-1：迁移加密收口（幂等 + 单向 + 读取透明）
# ---------------------------------------------------------------------------
class TestS1UnifyApiKeyEncryption:
    def _make_table(self, conn, table: str):
        conn.execute(
            f"CREATE TABLE IF NOT EXISTS {table} (rowid_col INTEGER PRIMARY KEY AUTOINCREMENT, api_key TEXT)"
        )

    def test_plaintext_becomes_encrypted_and_roundtrips(self):
        """明文 → ENC:，且解密回原值（读取链路透明、向后兼容）。"""
        from app.db_migrations import unify_api_key_encryption
        from app.services.crypto import decrypt_api_key, is_encrypted

        probe = (("_t_s1_probe", "api_key"),)
        with get_db() as conn:
            self._make_table(conn, "_t_s1_probe")
            conn.execute("DELETE FROM _t_s1_probe")
            conn.execute("INSERT INTO _t_s1_probe (api_key) VALUES ('sk-plain-s1-secret')")
            stats = unify_api_key_encryption(conn, columns=probe)
        try:
            assert stats["_t_s1_probe"] == 1
            with get_db(readonly=True) as c:
                stored = c.execute("SELECT api_key FROM _t_s1_probe").fetchone()[0]
            assert is_encrypted(stored)
            assert "sk-plain-s1-secret" not in stored
            assert decrypt_api_key(stored) == "sk-plain-s1-secret"
        finally:
            with get_db() as conn:
                conn.execute("DROP TABLE _t_s1_probe")
                conn.commit()

    def test_idempotent_second_pass_is_noop(self):
        """二次执行零转换（幂等），且空串/密文原样保留。"""
        from app.db_migrations import unify_api_key_encryption
        from app.services.crypto import is_encrypted

        with get_db() as conn:
            self._make_table(conn, "_t_s1_probe")
            conn.execute("DELETE FROM _t_s1_probe")
            conn.execute("INSERT INTO _t_s1_probe (api_key) VALUES ('sk-plain-s1-2')")
            conn.execute("INSERT INTO _t_s1_probe (api_key) VALUES ('')")
            probe = (("_t_s1_probe", "api_key"),)
            unify_api_key_encryption(conn, columns=probe)
            with get_db(readonly=True) as c:
                first = [r[0] for r in c.execute("SELECT api_key FROM _t_s1_probe").fetchall()]
            stats2 = unify_api_key_encryption(conn, columns=probe)
            with get_db(readonly=True) as c:
                second = [r[0] for r in c.execute("SELECT api_key FROM _t_s1_probe").fetchall()]
        try:
            assert first == second
            assert stats2["_t_s1_probe"] == 0
            assert is_encrypted(first[0]) and first[1] == ""
        finally:
            with get_db() as conn:
                conn.execute("DROP TABLE _t_s1_probe")
                conn.commit()

    def test_real_tables_plaintext_keys_get_encrypted(self):
        """真实配置表内的明文密钥经收口后全部为 ENC: 或空（端到端口径）。"""
        from app.db_migrations import unify_api_key_encryption
        from app.services.crypto import is_encrypted

        with get_db() as conn:
            unify_api_key_encryption(conn)
        with get_db(readonly=True) as c:
            for table, col in (
                ("ai_config", "api_key"),
                ("text_provider_configs", "api_key"),
                ("image_provider_configs", "api_key"),
                ("model_configs", "image_api_key_enc"),
                ("ai_profiles", "api_key_enc"),
            ):
                for (k,) in c.execute(f"SELECT {col} FROM {table}").fetchall():
                    assert k == "" or is_encrypted(k), f"{table}.{col} 存在明文密钥残留"

    def test_migration_32_no_longer_decrypts(self):
        """自证：迁移 #32 不再包含「解密为明文」路径，且调用收口函数。"""
        import app.db_migrations as m

        src = inspect.getsource(m.ensure_enhancement_tables)
        assert "_to_plain" not in src
        assert "unify_api_key_encryption(conn)" in src


# ---------------------------------------------------------------------------
# S-2：应用 Profile 后密钥加密落库
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_s2_apply_profile_keeps_key_encrypted(
    restore_ai_config, restore_ai_profiles
):
    from app.services.ai.profile_service import apply_profile_to_config, create_profile
    from app.services.crypto import decrypt_api_key, is_encrypted

    # apply 是 UPDATE WHERE id=1，先保证主配置行存在
    with get_db() as conn:
        if not conn.execute("SELECT 1 FROM ai_config WHERE id = 1").fetchone():
            conn.execute(
                "INSERT INTO ai_config (id, provider_name, provider, api_key, model,"
                " base_url, concurrency, temperature, max_tokens)"
                " VALUES (1, 's2-seed', 'openai_compatible', '', 's2-model',"
                " 'https://example.com/v1', 4, 0.7, 4096)"
            )
            conn.commit()

    create_profile(
        {
            "name": "s2-enc-profile",
            "display_name": "S2 加密回归",
            "api_key": "sk-s2-profile-secret",
            "provider_name": "DeepSeek",
            "provider": "deepseek",
            "model": "deepseek-chat",
            "base_url": "https://api.deepseek.com/v1",
        }
    )
    result = apply_profile_to_config("s2-enc-profile")
    assert result["status"] == "ok"

    stored = _raw_ai_config_key()
    assert is_encrypted(stored), f"应用 Profile 后主配置密钥应保持加密，实际: {stored[:20]!r}"
    assert "sk-s2-profile-secret" not in stored
    # 行为不变：读取链路透明解密（向后兼容）
    from app.services.ai.provider_factory import _clear_ai_config_cache, get_ai_config

    _clear_ai_config_cache()
    assert get_ai_config()["api_key"] == "sk-s2-profile-secret"
    assert decrypt_api_key(stored) == "sk-s2-profile-secret"


# ---------------------------------------------------------------------------
# S-3：图像配置写路径加密收口
# ---------------------------------------------------------------------------
def _raw_model_configs_image_key() -> str:
    with get_db(readonly=True) as c:
        row = c.execute(
            "SELECT image_api_key_enc FROM model_configs WHERE user_id = 1"
        ).fetchone()
    return (row[0] if row else "") or ""


def _raw_image_provider_key(provider_name: str) -> str:
    with get_db(readonly=True) as c:
        row = c.execute(
            "SELECT api_key FROM image_provider_configs WHERE provider_name = ?",
            [provider_name],
        ).fetchone()
    return (row[0] if row else "") or ""


@pytest.mark.asyncio
async def test_s3_activate_image_keeps_ciphertext(restore_image_tables):
    """image_provider_configs 中的 ENC: 密文同步到 model_configs 时不再解密为明文。"""
    from app.routers.ai_config import activate_image_provider_config
    from app.services.crypto import decrypt_api_key, encrypt_api_key, is_encrypted

    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO image_provider_configs
                (provider_name, api_key, base_url, model, max_concurrency,
                 max_per_project, quality, style_injection, enabled,
                 default_size, default_ratio, price_per_image, is_active)
            VALUES ('s3-img-probe', ?, 'https://example.com/v1', 's3-model', 2, 10,
                    'standard', '', 1, '1024x1024', '1:1', 0.0, 0)
            """,
            [encrypt_api_key("sk-s3-image-secret")],
        )
        conn.commit()

    result = await activate_image_provider_config("s3-img-probe")
    assert result["status"] == "ok"

    stored = _raw_model_configs_image_key()
    assert is_encrypted(stored), f"同步到主表应保留 ENC: 密文，实际: {stored[:20]!r}"
    assert "sk-s3-image-secret" not in stored
    # 行为不变：图像引擎读取侧按 is_encrypted 解密（向后兼容）
    assert decrypt_api_key(stored) == "sk-s3-image-secret"


@pytest.mark.asyncio
async def test_s3_save_image_provider_config_encrypts_fresh_key(restore_image_tables):
    """save_image_provider_config 新明文密钥加密落库（与文本厂商口径对齐）。"""
    from app.routers.ai_config import ImageProviderConfigData, save_image_provider_config
    from app.services.crypto import decrypt_api_key, is_encrypted

    await save_image_provider_config(
        "s3-img-save",
        ImageProviderConfigData(
            provider_name="s3-img-save",
            api_key="sk-s3-fresh-image",
            base_url="https://example.com/v1",
            model="s3-model",
        ),
    )
    stored = _raw_image_provider_key("s3-img-save")
    assert is_encrypted(stored)
    assert "sk-s3-fresh-image" not in stored
    assert decrypt_api_key(stored) == "sk-s3-fresh-image"


@pytest.mark.asyncio
async def test_s3_save_image_model_masked_key_preserves_ciphertext(restore_image_tables):
    """PUT /ai/image-model 占位符回传时保留库内密文，新明文加密落库。"""
    from app.routers.ai_config import (
        API_KEY_MASK,
        ImageModelConfigRequest,
        save_image_model_config,
    )
    from app.services.crypto import is_encrypted

    # 首次：明文密钥 → ENC:（直调端点需显式传 user_id，绕开 Depends 默认值）
    await save_image_model_config(
        ImageModelConfigRequest(
            provider="openai_compatible",
            model="s3-image-model",
            base_url="https://example.com/v1",
            api_key="sk-s3-main-image",
        ),
        user_id=1,
    )
    first = _raw_model_configs_image_key()
    assert is_encrypted(first)

    # 二次：占位符（前端只改模型）→ 原密文保留，不得降级为明文
    await save_image_model_config(
        ImageModelConfigRequest(
            provider="openai_compatible",
            model="s3-image-model-v2",
            base_url="https://example.com/v1",
            api_key=API_KEY_MASK,
        ),
        user_id=1,
    )
    assert _raw_model_configs_image_key() == first


# ---------------------------------------------------------------------------
# B-1 / P-1：源码级自证（项目既有 F-4 同款手法）
# ---------------------------------------------------------------------------
def test_b1_fetch_model_info_logs_probe_failure():
    """元数据探测失败必须有日志，禁止回归为静默 except pass。"""
    import app.routers.ai_config as m

    src = inspect.getsource(m.fetch_model_info)
    assert "pass" not in src.split("except Exception")[-1]
    assert "logger.warning" in src


def test_p1_fallback_health_check_off_event_loop():
    """Celery 任务内降级链创建必须经 asyncio.to_thread 派发（同步健康检查不阻塞 loop）。"""
    import app.tasks as m

    src = inspect.getsource(m)
    assert "await asyncio.to_thread(create_provider_with_fallback)" in src
    # 防自证失效：确认旧的直接调用形态已不存在
    assert "create_provider_with_fallback()" not in src
