"""文本模型配置独立端到端验证（2026-11-04 收尾 P2）。

全部使用**假 Provider / mock**，不依赖真实 API Key，也不会发起任何外网请求：

1. 场景路由（多模型选择 / 降级链）
   - 主配置健康检查失败 → 选中备选条目，并使用**该条目自己的** api_key/model
     （防止把主厂商密钥发到第三方端点）；
   - 无自身密钥且非内置默认的条目被跳过；
   - 健康检查结论进入 15s 失败 / 60s 成功短缓存，冷却期满后重新探活并按
     最新结果重选（熔断窗口的恢复语义）。
2. 分析熔断器状态机：CLOSED → OPEN → 冷却 → HALF_OPEN → 成功恢复 CLOSED /
   探测失败重新 OPEN。
3. 配置审计与版本回滚（HTTP）：
   - 保存 → 审计（api_key 全程脱敏，不落明文）；
   - Profile 快照 `apply` 回到历史配置（版本回滚）；
   - 过期 If-Match → 409（拒绝静默覆盖旧版本）。
"""

from __future__ import annotations

import asyncio
import time
import uuid

import pytest
from fastapi.testclient import TestClient

from app.services.ai import provider_factory as pf


# ──────────────────────────────────────────────────────────────────────
# 1. 场景路由 / 降级链（假 Provider）
# ──────────────────────────────────────────────────────────────────────


class _FakeProvider:
    def __init__(self, **kwargs):
        self.kw = kwargs


def _chain_entry(name: str, url: str, key: str, model: str, *, primary: bool = False):
    return {
        "name": name,
        "provider": "openai_compatible",
        "base_url": url,
        "api_key": key,
        "model": model,
        "success_rate": 1.0,
        "is_primary": primary,
        "is_builtin_default": False,
    }


@pytest.fixture()
def routing(monkeypatch):
    """装配降级链 + 假健康检查 + 假 Provider，并清理健康缓存。"""
    pf._clear_health_cache()
    state = {"checks": [], "health": {}, "built": []}

    def fake_health(url, api_key, model, timeout, provider_type=""):
        state["checks"].append((url, api_key, model))
        return state["health"][url]

    def fake_make(**kwargs):
        state["built"].append(kwargs)
        return _FakeProvider(**kwargs)

    monkeypatch.setattr(pf, "_quick_health_check", fake_health)
    monkeypatch.setattr(pf, "_make_provider", fake_make)
    yield state
    pf._clear_health_cache()


def test_primary_unhealthy_falls_back_with_own_key_model(routing, monkeypatch):
    url_a = "https://primary.example.com/v1"
    url_b = "https://backup.example.com/v1"
    url_no_key = "https://nokey.example.com/v1"
    chain = [
        _chain_entry("主配置", url_a, "k-primary", "model-a", primary=True),
        _chain_entry("备选B", url_b, "k-backup", "model-b"),
        _chain_entry("无密钥C", url_no_key, "", "model-c"),
    ]
    monkeypatch.setattr(pf, "get_provider_priority_order", lambda: chain)
    routing["health"] = {url_a: False, url_b: True}

    config = {"api_key": "k-primary", "base_url": url_a, "model": "model-a", "timeout": 5}
    provider, source = pf.create_provider_with_fallback(config=config)

    # 选中的是备选 B，且用的是 B 自己的密钥/模型（P0-SEC-1 口径）
    assert source == "备选B"
    assert provider.kw["api_key"] == "k-backup"
    assert provider.kw["model"] == "model-b"
    assert provider.kw["base_url"] == url_b
    # 无密钥且非内置默认的条目不会被探活（避免猜测主密钥）
    assert [c[0] for c in routing["checks"]] == [url_a, url_b]
    assert url_no_key not in [c[0] for c in routing["checks"]]


def test_health_cache_cools_down_then_reprobes_and_recovers(routing, monkeypatch):
    url_a = "https://primary.example.com/v1"
    url_b = "https://backup.example.com/v1"
    chain = [
        _chain_entry("主配置", url_a, "k-primary", "model-a", primary=True),
        _chain_entry("备选B", url_b, "k-backup", "model-b"),
    ]
    monkeypatch.setattr(pf, "get_provider_priority_order", lambda: chain)
    routing["health"] = {url_a: False, url_b: True}
    config = {"api_key": "k-primary", "base_url": url_a, "model": "model-a", "timeout": 5}

    # 第 1、2 次：A 失败结论进入 15s 缓存，不再重复探活（熔断窗口内短路）
    first, _ = pf.create_provider_with_fallback(config=config)
    checks_after_first = len(routing["checks"])
    second, _ = pf.create_provider_with_fallback(config=config)
    assert len(routing["checks"]) == checks_after_first
    assert second.kw["base_url"] == url_b

    # 模拟冷却期满（把缓存时间戳回拨 20s），且 A 已恢复、B 已故障
    key_a = pf._health_cache_key(url_a, "model-a", "k-primary", "openai_compatible")
    key_b = pf._health_cache_key(url_b, "model-b", "k-backup", "openai_compatible")
    old = time.time() - 20
    with pf._HEALTH_CACHE_LOCK:
        pf._HEALTH_CACHE[key_a] = (old, True)
        pf._HEALTH_CACHE[key_b] = (old, False)
    routing["health"] = {url_a: True, url_b: False}

    recovered, source = pf.create_provider_with_fallback(config=config)
    # 冷却后重新探活，主配置恢复即被重新选中（source 为空 = 主配置）
    assert source == ""
    assert recovered.kw["base_url"] == url_a
    assert recovered.kw["api_key"] == "k-primary"


# ──────────────────────────────────────────────────────────────────────
# 2. 分析熔断器：熔断 + 冷却 + 半开恢复
# ──────────────────────────────────────────────────────────────────────


def test_analysis_circuit_breaker_open_half_open_recover():
    from app.services.ai.workflows_base import _AnalysisCircuitBreaker

    breaker = _AnalysisCircuitBreaker(threshold=2, cooldown_seconds=5.0)
    task = "boq"

    assert breaker.is_open(task) is False
    asyncio.run(breaker.record_failure(task))
    assert breaker.is_open(task) is False          # 未达阈值
    asyncio.run(breaker.record_failure(task))
    assert breaker.is_open(task) is True           # 达到阈值 -> OPEN

    # 冷却期内持续短路（把最近失败时间回拨 4s，仍在 5s 冷却窗口内）
    breaker._last_failure_time[task] -= 4.0
    assert breaker.is_open(task) is True

    # 冷却期满 -> HALF_OPEN，放行一次探测
    breaker._last_failure_time[task] -= 2.0
    assert breaker.is_open(task) is False
    assert breaker._get_state(task) == "half_open"

    # 探测成功 -> 恢复 CLOSED
    asyncio.run(breaker.record_success(task))
    assert breaker._get_state(task) == "closed"
    assert breaker.is_open(task) is False


def test_analysis_circuit_breaker_half_open_failure_reopens():
    from app.services.ai.workflows_base import _AnalysisCircuitBreaker

    breaker = _AnalysisCircuitBreaker(threshold=1, cooldown_seconds=3.0)
    task = "work_section"

    asyncio.run(breaker.record_failure(task))
    assert breaker.is_open(task) is True
    breaker._last_failure_time[task] -= 4.0
    assert breaker.is_open(task) is False
    assert breaker._get_state(task) == "half_open"

    asyncio.run(breaker.record_failure(task))
    assert breaker._get_state(task) == "open"
    assert breaker.is_open(task) is True


# ──────────────────────────────────────────────────────────────────────
# 3. 配置审计 + 版本回滚（HTTP，无真实 API Key）
# ──────────────────────────────────────────────────────────────────────


@pytest.fixture()
def client():
    from app.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def _restore_ai_config():
    """HTTP 用例会改 ai_config 主配置，用例结束恢复原行，避免污染全量测试。"""
    from app.db import get_db

    with get_db() as conn:
        row = conn.execute("SELECT * FROM ai_config WHERE id = 1").fetchone()
        original = dict(row) if row is not None else None
    yield
    with get_db() as conn:
        if original is not None:
            cols = list(original.keys())
            placeholders = ",".join("?" for _ in cols)
            updatable = [c for c in cols if c != "id"]
            conn.execute(
                f"INSERT INTO ai_config ({','.join(cols)}) VALUES ({placeholders})"
                f" ON CONFLICT(id) DO UPDATE SET "
                + ",".join(f"{c}=excluded.{c}" for c in updatable),
                [original[c] for c in cols],
            )
        else:
            conn.execute("DELETE FROM ai_config WHERE id = 1")
    pf._clear_ai_config_cache()
    pf._clear_health_cache()


def _save_config(client, *, model: str, key: str, etag: str):
    return client.put(
        "/api/v1/ai/config",
        headers={"If-Match": etag},
        json={
            "provider_name": "mock-vendor",
            "provider": "openai_compatible",
            "api_key": key,
            "model": model,
            "base_url": "https://mock.example.com/v1",
            "concurrency": 4,
            "temperature": 0.4,
            "max_tokens": 4096,
            "timeout": 60,
        },
    )


def test_config_audit_and_profile_rollback_e2e(client):
    snapshot = f"rollback-{uuid.uuid4().hex[:8]}"
    secret_a = f"sk-mock-a-{uuid.uuid4().hex[:12]}"
    secret_b = f"sk-mock-b-{uuid.uuid4().hex[:12]}"

    # 1) 保存配置 A（真实格式假密钥，仅落库/加密，不发起网络请求）
    etag = client.get("/api/v1/ai/config").json()["etag"]
    resp = _save_config(client, model="mock-model-a", key=secret_a, etag=etag)
    assert resp.status_code == 200, resp.text
    assert client.get("/api/v1/ai/config").json()["model"] == "mock-model-a"

    # 2) 存一份 Profile 快照（作为可回滚的版本）
    created = client.post(
        "/api/v1/ai/profiles",
        json={
            "name": snapshot,
            "display_name": "回滚快照",
            "provider_name": "mock-vendor",
            "provider": "openai_compatible",
            "api_key": secret_a,
            "model": "mock-model-a",
            "base_url": "https://mock.example.com/v1",
            "max_tokens": 4096,
            "temperature": 0.4,
            "concurrency": 4,
            "timeout": 60,
        },
    )
    assert created.status_code == 200, created.text

    # 3) 改成配置 B
    etag = client.get("/api/v1/ai/config").json()["etag"]
    resp = _save_config(client, model="mock-model-b", key=secret_b, etag=etag)
    assert resp.status_code == 200, resp.text
    assert client.get("/api/v1/ai/config").json()["model"] == "mock-model-b"

    # 4) 应用 Profile = 版本回滚，主配置回到 A
    applied = client.post(f"/api/v1/ai/profiles/{snapshot}/apply")
    assert applied.status_code == 200, applied.text
    cfg = client.get("/api/v1/ai/config").json()
    assert cfg["model"] == "mock-model-a"
    assert cfg["base_url"] == "https://mock.example.com/v1"

    # 5) 审计：动作齐全，且 api_key 全程脱敏（库里/审计里都不出现明文）
    audit = client.get("/api/v1/ai/config-audit?limit=50").json()["items"]
    actions = {row.get("action") for row in audit}
    assert "update_config" in actions or "apply_profile" in actions
    assert any(row.get("action") == "apply_profile" for row in audit)
    blob = client.get("/api/v1/ai/config-audit?limit=50").text
    assert secret_a not in blob and secret_b not in blob
    assert "sk-mock" not in blob

    # 清理：删除快照，避免污染其它用例
    client.delete(f"/api/v1/ai/profiles/{snapshot}")


def test_stale_etag_is_rejected_with_409(client):
    etag = client.get("/api/v1/ai/config").json()["etag"]
    stale = str(int(etag) - 1)
    resp = _save_config(client, model="mock-model-x", key=f"sk-mock-{uuid.uuid4().hex[:10]}", etag=stale)
    assert resp.status_code == 409, resp.text
    # 拒绝覆盖：模型仍是上一次提交的值
    assert client.get("/api/v1/ai/config").json()["model"] != "mock-model-x"
