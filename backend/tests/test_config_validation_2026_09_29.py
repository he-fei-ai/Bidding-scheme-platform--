"""文本模型配置模块：保存校验增强回归测试（2026-09-29）。

覆盖 save_ai_config 的三项新增校验：
  1. temperature 钳制到 [0.0, 2.0]（非法/越界回退默认）
  2. concurrency 钳制到 [1, 64]
  3. base_url 存盘前 SSRF 校验：拒绝 file:// 等危险协议，空串允许（自定义厂商）
"""
from __future__ import annotations

import pytest

from app.db import get_db


@pytest.fixture
def restore_ai_config():
    with get_db(readonly=True) as c:
        row = c.execute("SELECT * FROM ai_config WHERE id = 1").fetchone()
        snapshot = dict(row) if row else None
    yield
    with get_db() as c:
        c.execute("DELETE FROM ai_config WHERE id = 1")
        if snapshot:
            cols = ", ".join(snapshot.keys())
            marks = ", ".join("?" for _ in snapshot)
            c.execute(f"INSERT INTO ai_config ({cols}) VALUES ({marks})", tuple(snapshot.values()))
        c.commit()


def _read_config():
    with get_db(readonly=True) as c:
        row = c.execute("SELECT * FROM ai_config WHERE id = 1").fetchone()
    return dict(row) if row else {}


@pytest.mark.asyncio
async def test_save_clamps_temperature_and_concurrency(restore_ai_config):
    from app.routers.ai_config import AIConfigData, save_ai_config

    data = AIConfigData(
        provider_name="DeepSeek",
        provider="deepseek",
        api_key="sk-test-xyz",
        model="deepseek-chat",
        base_url="https://api.deepseek.com/v1",
        temperature=999.0,  # 越界
        concurrency=0,  # 越界下界
        max_tokens=10_000_000,  # 越界
    )
    await save_ai_config(data)
    cfg = _read_config()
    assert cfg["temperature"] == pytest.approx(2.0)
    # concurrency=0 为假值，命中 `or 4` 回退默认，绝不会落库为 0；仍在合法区间
    assert 1 <= cfg["concurrency"] <= 64
    assert cfg["max_tokens"] == 65536


@pytest.mark.asyncio
async def test_save_clamps_negative_temperature(restore_ai_config):
    from app.routers.ai_config import AIConfigData, save_ai_config

    data = AIConfigData(
        provider_name="OpenAI",
        provider="openai_compatible",
        api_key="sk-test-abc",
        model="gpt-4o",
        base_url="https://api.openai.com/v1",
        temperature=-5.0,
        concurrency=100000,
    )
    await save_ai_config(data)
    cfg = _read_config()
    assert cfg["temperature"] == pytest.approx(0.0)
    assert cfg["concurrency"] == 64


@pytest.mark.asyncio
async def test_save_rejects_dangerous_scheme_base_url(restore_ai_config):
    from fastapi import HTTPException

    from app.routers.ai_config import AIConfigData, save_ai_config

    data = AIConfigData(
        provider_name="evil",
        provider="openai_compatible",
        api_key="sk-x",
        model="m",
        base_url="file:///etc/passwd",
    )
    with pytest.raises(HTTPException) as exc:
        await save_ai_config(data)
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_save_allows_empty_base_url_for_custom(restore_ai_config):
    """自定义兼容 OpenAI 接口厂商允许空 base_url（不因 SSRF 校验被拒）。"""
    from app.routers.ai_config import AIConfigData, save_ai_config

    data = AIConfigData(
        provider_name="custom",
        provider="openai_compatible",
        api_key="sk-custom",
        model="m",
        base_url="",
        temperature=0.5,
        concurrency=3,
    )
    await save_ai_config(data)
    cfg = _read_config()
    assert cfg["temperature"] == pytest.approx(0.5)
    assert cfg["concurrency"] == 3


# ---------------------------------------------------------------------------
# base_url SSRF 内网拦截（AI_ALLOW_INTERNAL_URL 开关）
# ---------------------------------------------------------------------------
def _validate(base_url):
    from app.routers.ai_config import _validate_base_url

    return _validate_base_url(base_url)


def test_validate_base_url_default_allows_internal(monkeypatch):
    """默认 AI_ALLOW_INTERNAL_URL=true（本地优先部署）：内网地址放行。"""
    monkeypatch.setattr("app.config.AI_ALLOW_INTERNAL_URL", True)
    assert _validate("http://127.0.0.1:11434/v1") is None  # Ollama 本地
    assert _validate("http://192.168.1.20:8000/v1") is None


def test_validate_base_url_blocks_internal_when_disabled(monkeypatch):
    """AI_ALLOW_INTERNAL_URL=false：环回/私有/云元数据均拦截，公网 IP 放行。"""
    monkeypatch.setattr("app.config.AI_ALLOW_INTERNAL_URL", False)
    assert _validate("http://127.0.0.1:11434/v1") is not None
    assert _validate("http://169.254.169.254/latest/meta-data") is not None
    assert _validate("http://10.0.0.5/v1") is not None
    assert _validate("http://192.168.1.20:8000/v1") is not None
    # 公网 IP 字面量（无需 DNS）应放行
    assert _validate("http://8.8.8.8/v1") is None


@pytest.mark.asyncio
async def test_save_rejects_internal_base_url_when_ssrf_enforced(restore_ai_config, monkeypatch):
    from fastapi import HTTPException

    from app.routers.ai_config import AIConfigData, save_ai_config

    monkeypatch.setattr("app.config.AI_ALLOW_INTERNAL_URL", False)
    data = AIConfigData(
        provider_name="evil",
        provider="openai_compatible",
        api_key="sk-x",
        model="m",
        base_url="http://169.254.169.254/v1",
    )
    with pytest.raises(HTTPException) as exc:
        await save_ai_config(data)
    assert exc.value.status_code == 400
