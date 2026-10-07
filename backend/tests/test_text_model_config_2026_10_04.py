"""文本模型配置模块：2026-10-04 增强与修复的回归测试。

覆盖项（编号与 routers/ai_config.py、provider_factory.py 内 F-x 注释对应）：
  F-1  新提交明文密钥统一 ENC: 加密落库；占位符回填保持原密文（安全）
  F-2  PUT /provider-configs/{name} 校验对齐主配置（base_url SSRF / 温度 / 并发钳制）
  F-3  降级链 get_configured_providers 对 ENC: 密文解密（加密补齐后链路不断）
  F-4  create_provider_with_fallback 按厂商取自身密钥而非恒用主配置 key
  F-5  quick-setup 落库取 best 自身 type/model，不再用循环残留变量
  F-7  activate 串行化：并发激活后恰好一个 active 且与主表一致
  F-8  model-info 元数据匹配对模型名大小写容错
  F-9  get_ai_config 缓存返回拷贝，调用方改动不污染缓存
  F-13 单厂商 GET 与列表接口同构（含 updated_at）
"""
from __future__ import annotations

import asyncio

import pytest

from app.db import get_db


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
    from app.services.ai.provider_factory import _clear_ai_config_cache

    _clear_ai_config_cache()


@pytest.fixture
def restore_text_provider_configs():
    with get_db(readonly=True) as c:
        snapshot = [dict(r) for r in c.execute("SELECT * FROM text_provider_configs").fetchall()]
    yield
    with get_db() as c:
        c.execute("DELETE FROM text_provider_configs")
        for row in snapshot:
            cols = ", ".join(row.keys())
            marks = ", ".join("?" for _ in row)
            c.execute(f"INSERT INTO text_provider_configs ({cols}) VALUES ({marks})", tuple(row.values()))
        c.commit()
    from app.services.ai.provider_factory import _clear_ai_config_cache

    _clear_ai_config_cache()


def _raw_ai_config_key() -> str:
    with get_db(readonly=True) as c:
        row = c.execute("SELECT api_key FROM ai_config WHERE id = 1").fetchone()
    return (row["api_key"] if row else "") or ""


def _raw_provider_key(provider_name: str) -> str:
    with get_db(readonly=True) as c:
        row = c.execute(
            "SELECT api_key FROM text_provider_configs WHERE provider_name = ?",
            [provider_name],
        ).fetchone()
    return (row["api_key"] if row else "") or ""


# ---------------------------------------------------------------------------
# F-1：加密落库
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_save_ai_config_encrypts_fresh_key(restore_ai_config):
    from app.routers.ai_config import AIConfigData, save_ai_config
    from app.services.crypto import is_encrypted

    await save_ai_config(
        AIConfigData(
            provider_name="DeepSeek",
            provider="deepseek",
            api_key="sk-fresh-secret-123",
            model="deepseek-chat",
            base_url="https://api.deepseek.com/v1",
        )
    )
    stored = _raw_ai_config_key()
    assert is_encrypted(stored), f"新密钥应 ENC: 加密落库，实际: {stored[:20]!r}"
    assert "sk-fresh-secret-123" not in stored

    # 读取链路透明解密：功能行为不变（向后兼容）
    from app.services.ai.provider_factory import _clear_ai_config_cache, get_ai_config

    _clear_ai_config_cache()
    assert get_ai_config()["api_key"] == "sk-fresh-secret-123"


@pytest.mark.asyncio
async def test_save_ai_config_masked_key_preserves_ciphertext(restore_ai_config):
    """占位符保存不得解密/清空/二次加密，库内密文原样保留。"""
    from app.routers.ai_config import API_KEY_MASK, AIConfigData, save_ai_config

    await save_ai_config(
        AIConfigData(
            provider_name="DeepSeek",
            provider="deepseek",
            api_key="sk-keepme-1",
            model="deepseek-chat",
            base_url="https://api.deepseek.com/v1",
        )
    )
    first = _raw_ai_config_key()
    assert first.startswith("ENC:")

    await save_ai_config(
        AIConfigData(
            provider_name="DeepSeek",
            provider="deepseek",
            api_key=API_KEY_MASK,
            model="deepseek-reasoner",
            base_url="https://api.deepseek.com/v1",
        )
    )
    assert _raw_ai_config_key() == first  # 原密文原样保留


@pytest.mark.asyncio
async def test_provider_config_save_encrypts_and_activate_syncs(
    restore_ai_config, restore_text_provider_configs
):
    from app.routers.ai_config import TextProviderConfigData, save_text_provider_config
    from app.services.crypto import is_encrypted

    data = TextProviderConfigData(
        provider_name="F1测试厂商",
        provider_type="openai_compatible",
        api_key="sk-provider-secret",
        base_url="https://api.example.com/v1",
        model="m1",
    )
    await save_text_provider_config("F1测试厂商", data)
    stored = _raw_provider_key("F1测试厂商")
    assert is_encrypted(stored)

    # 带 key 保存会自动激活并同步主表：主表拿到的也应是密文（同一形态）
    with get_db(readonly=True) as c:
        row = c.execute("SELECT api_key, provider_name FROM ai_config WHERE id = 1").fetchone()
    assert row and row["api_key"] == stored and row["provider_name"] == "F1测试厂商"


# ---------------------------------------------------------------------------
# F-2：provider-configs 保存校验对齐主配置
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_provider_config_save_rejects_dangerous_scheme(
    restore_ai_config, restore_text_provider_configs
):
    from fastapi import HTTPException

    from app.routers.ai_config import TextProviderConfigData, save_text_provider_config

    with pytest.raises(HTTPException) as exc:
        await save_text_provider_config(
            "evil",
            TextProviderConfigData(provider_name="evil", base_url="file:///etc/passwd", api_key="sk-x"),
        )
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_provider_config_save_clamps_ranges(
    restore_ai_config, restore_text_provider_configs
):
    from app.routers.ai_config import TextProviderConfigData, save_text_provider_config

    await save_text_provider_config(
        "钳制厂商",
        TextProviderConfigData(
            provider_name="钳制厂商",
            api_key="sk-x",
            base_url="https://api.example.com/v1",
            model="m",
            temperature=999.0,
            concurrency=100000,
            max_tokens=10_000_000,
        ),
    )
    with get_db(readonly=True) as c:
        row = c.execute(
            "SELECT temperature, concurrency, max_tokens FROM text_provider_configs WHERE provider_name = ?",
            ["钳制厂商"],
        ).fetchone()
    assert row["temperature"] == pytest.approx(2.0)
    assert row["concurrency"] == 64
    assert row["max_tokens"] == 65536


# ---------------------------------------------------------------------------
# F-3 / F-4：降级链密钥
# ---------------------------------------------------------------------------
def test_configured_providers_decrypts_enc_key(restore_text_provider_configs):
    from app.services.ai.provider_factory import _clear_ai_config_cache, get_configured_providers
    from app.services.crypto import encrypt_api_key

    _clear_ai_config_cache()
    cipher = encrypt_api_key("sk-chain-secret")
    assert cipher.startswith("ENC:")
    with get_db() as c:
        c.execute(
            "INSERT OR REPLACE INTO text_provider_configs "
            "(provider_name, provider_type, api_key, base_url, model, is_active) "
            "VALUES ('F3厂商', 'openai_compatible', ?, 'https://f3.example.com/v1', 'm', 1)",
            (cipher,),
        )
        c.commit()
    entries = get_configured_providers()
    hit = next((e for e in entries if e["name"] == "F3厂商"), None)
    assert hit is not None
    assert hit["api_key"] == "sk-chain-secret", "降级链必须拿到解密后的可用密钥"


def test_fallback_entry_key_selection_prefers_own(restore_text_provider_configs):
    """F-4 语义校验：降级链条目自带 key 时，构造 provider 应使用条目 key（源码行为钉死）。"""
    import inspect

    from app.services.ai import provider_factory

    src = inspect.getsource(provider_factory.create_provider_with_fallback)
    assert 'entry.get("api_key") or api_key' in src


# ---------------------------------------------------------------------------
# F-5：quick-setup 落库取 best 自身 type/model
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_quick_setup_uses_best_provider_type(monkeypatch, restore_ai_config):
    from app.routers import ai_config as ai_config_router

    class FakeResp:
        def __init__(self, status_code: int):
            self.status_code = status_code
            self.text = ""

    class FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, *a, **kw):
            if "deepseek" in url:
                return FakeResp(200)  # 最快
            await asyncio.sleep(0.05)  # 其他厂商更慢且失败
            return FakeResp(500)

    monkeypatch.setattr(ai_config_router.httpx, "AsyncClient", FakeClient)

    result = await ai_config_router.quick_setup(
        ai_config_router.QuickSetupRequest(api_key="sk-f5")
    )
    assert result.success
    best = result.tested_providers[0] if result.applied_config else None
    with get_db(readonly=True) as c:
        row = c.execute(
            "SELECT provider_name, provider, model, base_url FROM ai_config WHERE id = 1"
        ).fetchone()
    assert row["provider_name"] == "DeepSeek"
    assert row["provider"] == "deepseek", (
        f"落库 provider 应为 best 自身类型，实际 {row['provider']!r}（F-5 循环残留回归）"
    )
    assert row["model"] == "deepseek-chat"
    assert best is not None


# ---------------------------------------------------------------------------
# F-7：activate 串行化，并发后状态一致
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_concurrent_activate_leaves_exactly_one_active(
    restore_ai_config, restore_text_provider_configs
):
    from app.routers.ai_config import TextProviderConfigData, save_text_provider_config

    for name in ("并发厂商A", "并发厂商B"):
        await save_text_provider_config(
            name,
            TextProviderConfigData(
                provider_name=name,
                api_key=f"sk-{name}",
                base_url="https://api.example.com/v1",
                model="m",
            ),
        )
    # 去掉 key 自动激活的干扰：直接并发打 activate（key 已在库内）
    from app.routers.ai_config import activate_text_provider_config

    await asyncio.gather(
        activate_text_provider_config("并发厂商A"),
        activate_text_provider_config("并发厂商B"),
    )
    with get_db(readonly=True) as c:
        active = c.execute(
            "SELECT provider_name FROM text_provider_configs WHERE is_active = 1"
        ).fetchall()
        main = c.execute("SELECT provider_name FROM ai_config WHERE id = 1").fetchone()
    assert len(active) == 1, f"并发激活后应恰好一个 active，实际 {len(active)} 个"
    assert main["provider_name"] == active[0]["provider_name"], "主表应与激活厂商一致"


# ---------------------------------------------------------------------------
# F-8：model-info 模型名大小写容错
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_model_info_case_insensitive_model_match():
    from app.routers.ai_config import ModelInfoRequest, fetch_model_info

    info = await fetch_model_info(
        ModelInfoRequest(
            provider="siliconflow",
            base_url="https://api.siliconflow.cn/v1",
            model="DeepSeek-AI/Deepseek-V3",  # 大小写与元数据键不一致
            api_key="",
        )
    )
    assert info["source"] == "builtin"
    assert info["max_tokens"] == 16384


# ---------------------------------------------------------------------------
# F-9：缓存返回拷贝
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_get_ai_config_returns_copy(restore_ai_config):
    from app.routers.ai_config import AIConfigData, save_ai_config
    from app.services.ai.provider_factory import _clear_ai_config_cache, get_ai_config

    await save_ai_config(
        AIConfigData(
            provider_name="F9厂商",
            api_key="sk-f9",
            model="m",
            base_url="https://api.example.com/v1",
        )
    )
    _clear_ai_config_cache()
    cfg1 = get_ai_config()
    cfg1["__pollution__"] = True
    cfg2 = get_ai_config()  # 命中缓存
    assert "__pollution__" not in cfg2, "调用方对返回 dict 的修改不得污染缓存"


# ---------------------------------------------------------------------------
# F-13：单厂商 GET 与列表同构（updated_at）
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_single_provider_get_has_updated_at(
    restore_ai_config, restore_text_provider_configs
):
    from app.routers.ai_config import (
        TextProviderConfigData,
        get_text_provider_config,
        save_text_provider_config,
    )

    await save_text_provider_config(
        "F13厂商",
        TextProviderConfigData(
            provider_name="F13厂商",
            api_key="sk-f13",
            base_url="https://api.example.com/v1",
            model="m",
        ),
    )
    single = await get_text_provider_config("F13厂商")
    assert "updated_at" in single and single["updated_at"]
    assert set(single.keys()) >= {
        "provider_name", "provider_type", "api_key", "base_url",
        "model", "max_tokens", "concurrency", "temperature",
        "document_profile", "is_active", "updated_at",
    }
