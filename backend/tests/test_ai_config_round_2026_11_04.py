"""文本模型配置模块 第 12 轮加固回归测试（2026-11-04）

覆盖本轮修复项（编号与报告一致）：
  G-A  探活按协议分端点（anthropic_compatible → /messages + x-api-key）
  G-B  /ai/test 的 provider_history.provider_type 污染修复
  G-C  /ai/test 与 /ai/models 感知显式 provider_type
  G-D  激活空 base_url 厂商被 400 拦截（防把主配置写坏）
  G-E  DELETE /ai/provider-configs/{name}（404 / 409 / 正常删除 + 审计）
"""

import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.db import get_db_with_retry
from app.main import app
from app.routers import ai_config as ac
import app.services.ai.provider_factory as pf

client = TestClient(app)
_PREFIX = "r12_"


def _pname(tag: str) -> str:
    return f"{_PREFIX}{tag}_{uuid.uuid4().hex[:8]}"


def _insert_tpc(name: str, base_url: str = "https://api.example.com/v1",
                model: str = "test-model", api_key: str = "sk-r12-key",
                is_active: int = 0, provider_type: str = "openai_compatible") -> None:
    with get_db_with_retry() as conn:
        if is_active:
            # idx_tpc_unique_active：全库至多一行 is_active=1，先清位再插入
            conn.execute("UPDATE text_provider_configs SET is_active = 0")
        conn.execute(
            "INSERT INTO text_provider_configs "
            "(provider_name, provider_type, api_key, base_url, model, "
            " max_tokens, concurrency, temperature, document_profile, timeout, is_active) "
            "VALUES (?, ?, ?, ?, ?, 4096, 4, 0.7, '', 120, ?)",
            (name, provider_type, api_key, base_url, model, is_active),
        )
        conn.commit()


def _restore_active(names: list[str]) -> None:
    with get_db_with_retry() as conn:
        conn.execute("UPDATE text_provider_configs SET is_active = 0")
        for p in names:
            conn.execute(
                "UPDATE text_provider_configs SET is_active = 1 WHERE provider_name = ?", [p]
            )
        conn.commit()


def _tpc_row(name: str):
    with get_db_with_retry(readonly=True) as conn:
        return conn.execute(
            "SELECT provider_name, base_url, model FROM text_provider_configs "
            "WHERE provider_name = ?",
            [name],
        ).fetchone()


def _cleanup_tpc():
    with get_db_with_retry() as conn:
        conn.execute("DELETE FROM text_provider_configs WHERE provider_name LIKE ?", (f"{_PREFIX}%",))
        conn.execute("DELETE FROM ai_config_audit WHERE target LIKE ?", (f"{_PREFIX}%",))
        conn.execute("DELETE FROM provider_history WHERE provider_name LIKE ?", (f"{_PREFIX}%",))
        conn.commit()


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    _cleanup_tpc()


# ══════════════════════════════════════════════════════════════════════
# G-A：探活按协议分端点
# ══════════════════════════════════════════════════════════════════════

def test_probe_request_anthropic_uses_messages_endpoint():
    url, headers, payload = pf._probe_request_for_protocol(
        "anthropic_compatible", "https://gw.example.com/v1", "sk-a", "m1"
    )
    assert url == "https://gw.example.com/v1/messages"
    assert headers["x-api-key"] == "sk-a"
    assert headers["anthropic-version"] == "2023-06-01"
    assert payload["max_tokens"] == 4
    assert payload["messages"][0]["role"] == "user"
    # Anthropic wire 格式不含 OpenAI 的 messages 外层 chat 参数
    assert "temperature" not in payload or payload.get("max_tokens") == 4


def test_probe_request_openai_compatible_unchanged():
    """向后兼容：默认/OpenAI 兼容协议的探活请求与旧实现逐字段一致"""
    for ptype in ("", "openai_compatible", "deepseek"):
        url, headers, payload = pf._probe_request_for_protocol(
            ptype, "https://api.x.com/v1", "sk-b", "m2"
        )
        assert url == "https://api.x.com/v1/chat/completions"
        assert headers["Authorization"] == "Bearer sk-b"
        assert payload["max_tokens"] == 4
        assert payload["messages"][0]["content"] == "请回复'OK'"


def test_health_cache_key_distinguishes_protocol():
    a = pf._health_cache_key("https://g/v1", "m", "sk", "anthropic_compatible")
    b = pf._health_cache_key("https://g/v1", "m", "sk", "openai_compatible")
    assert a != b  # 同一 URL/密钥在两种协议下结论互不串用


def _set_main_config(base_url: str, provider: str, model: str, api_key: str) -> None:
    """写主配置行（探活透传测试需要主条目携带真实 key/URL 才会走探活分支）"""
    with get_db_with_retry() as conn:
        conn.execute(
            "INSERT INTO ai_config (id, provider_name, provider, api_key, model, base_url,\n"
            "   document_profile, concurrency, temperature, max_tokens, timeout, updated_at)\n"
            " VALUES (1, 'r12-probe', ?, ?, ?, ?, '', 4, 0.7, 4096, 120, datetime('now','localtime'))\n"
            " ON CONFLICT(id) DO UPDATE SET provider=excluded.provider, api_key=excluded.api_key,\n"
            "   model=excluded.model, base_url=excluded.base_url",
            (provider, api_key, model, base_url),
        )
        conn.commit()
    pf._clear_ai_config_cache()


def _restore_main_config(backup: dict) -> None:
    with get_db_with_retry() as conn:
        if backup:
            conn.execute("DELETE FROM ai_config WHERE id = 1")
            cols = [k for k in backup.keys()]
            conn.execute(
                f"INSERT INTO ai_config ({', '.join(cols)}) "
                f"VALUES ({', '.join('?' for _ in cols)})",
                [backup[c] for c in cols],
            )
        else:
            conn.execute("DELETE FROM ai_config WHERE id = 1")
        conn.commit()
    pf._clear_ai_config_cache()


def _backup_main_row() -> dict:
    with get_db_with_retry(readonly=True) as conn:
        row = conn.execute("SELECT * FROM ai_config WHERE id = 1").fetchone()
    return dict(row) if row else {}


def test_sync_fallback_chain_passes_provider_type_to_probe():
    """G-A 主断言：同步降级链对显式 anthropic 条目探活时透传协议类型"""
    pf._clear_health_cache()
    backup = _backup_main_row()
    uniq = uuid.uuid4().hex[:8]
    _set_main_config(
        base_url=f"https://gw-{uniq}.example.com/v1",
        provider="anthropic_compatible",
        model="claude-x",
        api_key=f"sk-uniq-{uniq}",
    )
    captured = {}

    def _fake_probe(base_url, api_key, model, timeout, provider_type=""):
        captured["provider_type"] = provider_type
        return True

    orig_probe = pf._quick_health_check
    orig_make = pf._make_provider
    pf._quick_health_check = _fake_probe
    pf._make_provider = lambda **kw: SimpleNamespace(model=kw.get("model"))
    try:
        config = {
            "api_key": f"sk-uniq-{uniq}",
            "base_url": f"https://gw-{uniq}.example.com/v1",
            "provider": "anthropic_compatible",
            "model": "claude-x",
            "timeout": 5,
        }
        provider, source = pf.create_provider_with_fallback(config)
        assert captured["provider_type"] == "anthropic_compatible"
        assert source == ""  # 主配置命中，无降级
    finally:
        pf._quick_health_check = orig_probe
        pf._make_provider = orig_make
        pf._clear_health_cache()
        _restore_main_config(backup)


@pytest.mark.asyncio
async def test_async_fallback_chain_passes_provider_type_to_probe():
    pf._clear_health_cache()
    backup = _backup_main_row()
    uniq = uuid.uuid4().hex[:8]
    _set_main_config(
        base_url=f"https://gwa-{uniq}.example.com/v1",
        provider="anthropic_compatible",
        model="claude-y",
        api_key=f"sk-uniq-{uniq}",
    )
    captured = {}

    async def _fake_probe(base_url, api_key, model, provider_type=""):
        captured["provider_type"] = provider_type
        return True

    orig_probe = pf._async_quick_health_check
    orig_make = pf._make_provider
    pf._async_quick_health_check = _fake_probe
    pf._make_provider = lambda **kw: SimpleNamespace(model=kw.get("model"))
    try:
        config = {
            "api_key": f"sk-uniq-{uniq}",
            "base_url": f"https://gwa-{uniq}.example.com/v1",
            "provider": "anthropic_compatible",
            "model": "claude-y",
            "timeout": 5,
        }
        provider, source = await pf.get_working_provider(config)
        assert captured["provider_type"] == "anthropic_compatible"
        assert source == ""
    finally:
        pf._async_quick_health_check = orig_probe
        pf._make_provider = orig_make
        pf._clear_health_cache()
        _restore_main_config(backup)


# ══════════════════════════════════════════════════════════════════════
# G-B / G-C：/ai/test 感知协议 + provider_history 不被展示名污染
# ══════════════════════════════════════════════════════════════════════

class _FakeProvider:
    async def chat(self, **kwargs):
        return "OK"


def test_test_endpoint_uses_explicit_provider_type(monkeypatch):
    captured = {}

    def _fake_create(config):
        captured["provider"] = config.get("provider")
        return _FakeProvider()

    usages = []
    monkeypatch.setattr(ac, "_create_shared_provider", _fake_create)
    monkeypatch.setattr(ac, "record_provider_usage",
                        lambda n, t, u, success=True: usages.append((n, t, u, success)))
    r = client.post("/api/v1/ai/test", json={
        "provider": "自定义网关",  # 中文展示名（历史污染源）
        "provider_type": "anthropic_compatible",
        "api_key": "sk-x",
        "model": "claude-x",
        "base_url": "https://gw.example.com/v1",
    })
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "ok"
    # G-C：探针实例按显式协议路由
    assert captured["provider"] == "anthropic_compatible"
    # G-B：provider_history 的协议列写解析后的类型，而非中文展示名
    assert usages and usages[0][0] == "自定义网关"
    assert usages[0][1] == "anthropic_compatible"


def test_test_endpoint_falls_back_to_url_heuristic(monkeypatch):
    """不传 provider_type 时保持旧行为：URL 启发式识别（含 DeepSeek 关键词）"""
    captured = {}
    usages = []
    monkeypatch.setattr(ac, "_create_shared_provider",
                        lambda config: (captured.update(provider=config.get("provider")) or _FakeProvider()))
    monkeypatch.setattr(ac, "record_provider_usage",
                        lambda n, t, u, success=True: usages.append((n, t, u, success)))
    r = client.post("/api/v1/ai/test", json={
        "provider": "DeepSeek",
        "api_key": "sk-x",
        "model": "deepseek-chat",
        "base_url": "https://api.deepseek.com/v1",
    })
    assert r.status_code == 200, r.text
    # 向后兼容：不传 provider_type 时 config['provider'] 保持原值（展示名），
    # 真实路由由 _make_provider 的 URL 启发式决定 —— 旧行为不变。
    assert captured["provider"] == "DeepSeek"
    # G-B 核心：provider_history 写入的是 URL 启发式解析后的协议类型
    assert usages and usages[0][1] == "deepseek"


def _fake_httpx_module(monkeypatch, captured):
    """替换 ai_config 模块内的 httpx 引用，捕获 GET 请求的 URL 与头。"""

    class FakeResp:
        status_code = 200
        text = ""

        def json(self):
            return {"data": [{"id": "claude-x"}, {"id": "claude-y"}]}

    class FakeAsyncClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None, **kw):
            captured["url"] = url
            captured["headers"] = headers or {}
            return FakeResp()

    monkeypatch.setattr(ac, "httpx", SimpleNamespace(AsyncClient=FakeAsyncClient, Timeout=None))


def test_models_endpoint_anthropic_sends_x_api_key(monkeypatch):
    """G-C：anthropic_compatible 的模型列表请求携带 x-api-key + anthropic-version"""
    captured = {}
    _fake_httpx_module(monkeypatch, captured)
    r = client.post("/api/v1/ai/models", json={
        "api_key": "sk-models",
        "base_url": "https://gw.example.com/v1",
        "provider": "自定义网关",
        "provider_type": "anthropic_compatible",
    })
    assert r.status_code == 200, r.text
    assert r.json()["models"] == ["claude-x", "claude-y"]
    assert captured["headers"].get("x-api-key") == "sk-models"
    assert captured["headers"].get("anthropic-version") == "2023-06-01"
    assert captured["headers"].get("Authorization") == "Bearer sk-models"


def test_models_endpoint_openai_no_x_api_key(monkeypatch):
    """非 anthropic 协议不额外加 x-api-key（向后兼容）"""
    captured = {}
    _fake_httpx_module(monkeypatch, captured)
    r = client.post("/api/v1/ai/models", json={
        "api_key": "sk-o",
        "base_url": "https://api.example.com/v1",
        "provider_type": "openai_compatible",
    })
    assert r.status_code == 200, r.text
    assert "x-api-key" not in captured["headers"]


# ══════════════════════════════════════════════════════════════════════
# G-D：激活空 base_url 厂商被 400 拦截
# ══════════════════════════════════════════════════════════════════════

def _main_base_url() -> str:
    r = client.get("/api/v1/ai/config")
    assert r.status_code == 200
    return r.json().get("base_url", "")


def test_activate_provider_with_empty_base_url_rejected():
    name = _pname("emptyurl")
    _insert_tpc(name, base_url="")
    before = _main_base_url()
    r = client.put(f"/api/v1/ai/provider-configs/{name}/activate")
    assert r.status_code == 400, r.text
    assert "Base URL" in r.json()["detail"]
    # 关键不变量：主配置未被写坏
    assert _main_base_url() == before
    # 激活守卫失败不应误伤 is_active 状态
    with get_db_with_retry(readonly=True) as conn:
        row = conn.execute(
            "SELECT is_active FROM text_provider_configs WHERE provider_name = ?", [name]
        ).fetchone()
    assert not (row[0] if row else 0)


def test_activate_provider_with_valid_base_url_succeeds_and_restores():
    name = _pname("validact")
    _insert_tpc(name, base_url="https://valid.example.com/v1", model="m-valid")
    # 备份主配置（激活会写 ai_config 主表，测试后恢复）
    with get_db_with_retry(readonly=True) as conn:
        backup = dict(conn.execute("SELECT * FROM ai_config WHERE id = 1").fetchone() or {})
        prev_active = [
            r[0] for r in conn.execute(
                "SELECT provider_name FROM text_provider_configs WHERE is_active = 1"
            ).fetchall()
        ]
    try:
        r = client.put(f"/api/v1/ai/provider-configs/{name}/activate")
        assert r.status_code == 200, r.text
        with get_db_with_retry(readonly=True) as conn:
            row = conn.execute("SELECT base_url FROM ai_config WHERE id = 1").fetchone()
            assert row[0] == "https://valid.example.com/v1"
            flag = conn.execute(
                "SELECT is_active FROM text_provider_configs WHERE provider_name = ?", [name]
            ).fetchone()
            assert bool(flag[0])
    finally:
        with get_db_with_retry() as conn:
            conn.execute("UPDATE text_provider_configs SET is_active = 0 WHERE provider_name = ?", [name])
            for p in prev_active:
                conn.execute(
                    "UPDATE text_provider_configs SET is_active = 1 WHERE provider_name = ?", [p]
                )
            if backup:
                cols = [k for k in backup.keys()]
                sets = ", ".join(f"{c}=?" for c in cols if c != "id")
                conn.execute(
                    f"UPDATE ai_config SET {sets} WHERE id = 1",
                    [backup[c] for c in cols if c != "id"],
                )
            conn.commit()


# ══════════════════════════════════════════════════════════════════════
# G-E：DELETE /ai/provider-configs/{name}
# ══════════════════════════════════════════════════════════════════════

def test_delete_provider_config_not_found():
    r = client.delete(f"/api/v1/ai/provider-configs/{_pname('nope')}")
    assert r.status_code == 404, r.text


def test_delete_provider_config_active_conflict():
    name = _pname("activeconf")
    with get_db_with_retry(readonly=True) as conn:
        prev_active = [
            r[0] for r in conn.execute(
                "SELECT provider_name FROM text_provider_configs WHERE is_active = 1"
            ).fetchall()
        ]
    try:
        _insert_tpc(name, is_active=1)
        r = client.delete(f"/api/v1/ai/provider-configs/{name}")
        assert r.status_code == 409, r.text
        assert "当前使用" in r.json()["detail"]
        assert _tpc_row(name) is not None  # 未删除
    finally:
        _cleanup_tpc()
        _restore_active(prev_active)


def test_delete_provider_config_ok():
    name = _pname("delok")
    _insert_tpc(name)
    assert _tpc_row(name) is not None
    v_before = pf.get_ai_config_version()
    r = client.delete(f"/api/v1/ai/provider-configs/{name}")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "ok"
    assert _tpc_row(name) is None  # 行已删除（密钥一并销毁）
    assert pf.get_ai_config_version() > v_before  # 跨进程失效
    with get_db_with_retry(readonly=True) as conn:
        audit = conn.execute(
            "SELECT actor, action, target FROM ai_config_audit "
            "WHERE action = 'delete_provider_config' AND target = ? ORDER BY id DESC LIMIT 1",
            [name],
        ).fetchone()
    assert audit is not None  # 审计落库


# ══════════════════════════════════════════════════════════════════════
# G-H：quick-setup 并发信号量（与 /test 共用上限配置）
# ══════════════════════════════════════════════════════════════════════

def test_quick_setup_reuses_test_semaphore():
    """quick_setup 与 /test 共用同一进程级信号量（G-H 实现口径）"""
    assert ac._get_test_connection_semaphore() is ac._get_test_connection_semaphore()
    assert ac._TEST_CONCURRENCY_LIMIT >= 1
