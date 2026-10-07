"""文本模型配置模块：2026-10-04 遗留问题闭环的回归测试。

覆盖项（编号与代码内 L-x 注释对应）：
  L-1  ai_provider_service 收敛为 provider_factory 门面（单一实现源）
  L-1b quick-setup 响应 applied_config.provider 与落库一致（不再取循环残留变量）
  L-2  Profile 新写入统一 ENC: 加密落库；明文历史数据读取兼容
  L-2b 降级链兜底层 is_builtin_default 按条目自身标记透传
  L-3  配置缓存跨进程失效：版本计数器 + 5 秒防抖
  L-4  GET /ai/config 回显 config_source（env/database/default）
  L-5  配置变更审计日志：写入脱敏摘要、可查询、不阻断主流程
  L-6  _resolve_api_key 不再把 AGNES_API_KEY 回退发往调用方任意指定的 URL
"""
from __future__ import annotations

import asyncio
import os

import pytest

from app.db import get_db
from app.services.crypto import decrypt_api_key, is_encrypted


@pytest.fixture
def restore_state():
    """备份/还原 ai_config、ai_config_state、ai_config_audit、provider_history，
    并保证测试前后 provider_factory 模块级缓存/版本状态干净。"""
    with get_db(readonly=True) as c:
        cfg_row = c.execute("SELECT * FROM ai_config WHERE id = 1").fetchone()
        snapshot = dict(cfg_row) if cfg_row else None
        state_rows = [dict(r) for r in c.execute("SELECT * FROM ai_config_state").fetchall()]
        audit_ids = [r["id"] for r in c.execute("SELECT id FROM ai_config_audit").fetchall()]
        hist_rows = [dict(r) for r in c.execute("SELECT * FROM provider_history").fetchall()]
    yield
    with get_db() as c:
        c.execute("DELETE FROM ai_config WHERE id = 1")
        if snapshot:
            cols = ", ".join(snapshot.keys())
            marks = ", ".join("?" for _ in snapshot)
            c.execute(f"INSERT INTO ai_config ({cols}) VALUES ({marks})", tuple(snapshot.values()))
        c.execute("DELETE FROM ai_config_state")
        for row in state_rows:
            cols = ", ".join(row.keys())
            marks = ", ".join("?" for _ in row)
            c.execute(f"INSERT INTO ai_config_state ({cols}) VALUES ({marks})", tuple(row.values()))
        if audit_ids:
            c.execute(f"DELETE FROM ai_config_audit WHERE id NOT IN ({','.join('?' * len(audit_ids))})", audit_ids)
        else:
            c.execute("DELETE FROM ai_config_audit")
        c.execute("DELETE FROM provider_history")
        for row in hist_rows:
            cols = ", ".join(row.keys())
            marks = ", ".join("?" for _ in row)
            c.execute(f"INSERT INTO provider_history ({cols}) VALUES ({marks})", tuple(row.values()))
        c.commit()
    import app.services.ai.provider_factory as pf

    pf._ai_config_cache = None
    pf._ai_config_cache_time = 0
    pf._seen_config_version = -1
    pf._version_checked_at = 0.0


@pytest.fixture
def cleanup_test_profiles():
    names = []

    def _new(suffix: str) -> str:
        name = f"zz-l2-test-{suffix}"
        names.append(name)
        return name

    yield _new
    with get_db() as c:
        for n in names:
            c.execute("DELETE FROM ai_profiles WHERE name = ?", (n,))
        c.commit()


# ──────────────────────────────────────────────────────────────────────
# L-1 门面收敛
# ──────────────────────────────────────────────────────────────────────

def test_facade_delegates_to_factory(restore_state):
    """L-1：服务门面的降级链/配置必须与 provider_factory 完全同源。"""
    import app.services.ai.provider_factory as pf
    from app.services.ai.ai_provider_service import AIProviderService

    svc = AIProviderService()
    assert svc.get_priority_chain() == pf.get_provider_priority_order()
    assert svc.get_config() == pf.get_ai_config()
    assert svc.get_history() == pf.get_provider_history()
    # 旧副本私有的实例缓存/内联实现已彻底移除（单一实现源的结构性证明）
    assert not hasattr(svc, "_cache")
    import app.services.ai.ai_provider_service as mod

    src = open(mod.__file__, encoding="utf-8").read()
    assert "def _load_env_config" not in src, "平行实现的 env 加载副本应已清除"
    assert "httpx.Client(" not in src, "平行实现的健康检查副本应已清除"


def test_facade_get_history_includes_success_rate(restore_state):
    """L-1 兼容：BUG-2 不变量（success_rate 字段）在统一实现下保持。"""
    from app.services.ai.ai_provider_service import AIProviderService, record_provider_usage

    record_provider_usage("l1-hist", "text", "https://l1-hist.example.com", success=True)
    record_provider_usage("l1-hist", "text", "https://l1-hist.example.com", success=False)
    items = AIProviderService().get_history()
    entry = next(e for e in items if e["provider_name"] == "l1-hist")
    assert "success_rate" in entry
    assert abs(entry["success_rate"] - 0.5) < 0.01


# ──────────────────────────────────────────────────────────────────────
# L-1b quick-setup 响应字段与落库一致
# ──────────────────────────────────────────────────────────────────────

def test_quick_setup_applied_provider_uses_best_type():
    """L-1b：源码层面钉扎 applied 取 best['type']（响应构造不依赖循环残留变量）。"""
    import app.routers.ai_config as mod

    src = open(mod.__file__, encoding="utf-8").read()
    assert "'provider': best['type']" in src
    assert "'provider': provider_type," not in src


# ──────────────────────────────────────────────────────────────────────
# L-2 Profile 密钥加密
# ──────────────────────────────────────────────────────────────────────

def test_create_profile_stores_encrypted_key(cleanup_test_profiles, restore_state):
    """L-2：新建 Profile 的库内 api_key_enc 必须是 ENC: 密文，且可还原。"""
    from app.services.ai.profile_service import create_profile

    name = cleanup_test_profiles("enc")
    create_profile({"name": name, "api_key": "sk-l2-secret-abcdef"})
    with get_db(readonly=True) as c:
        row = c.execute("SELECT api_key_enc FROM ai_profiles WHERE name = ?", (name,)).fetchone()
    stored = row["api_key_enc"]
    assert is_encrypted(stored), "新写入必须加密落库，不得明文入库"
    assert decrypt_api_key(stored) == "sk-l2-secret-abcdef"


def test_profile_service_layer_returns_decrypted_plaintext(cleanup_test_profiles, restore_state):
    """L-2 兼容：服务层 include_key=True 返回明文（供降级链/apply 使用，口径不变）。"""
    from app.services.ai.profile_service import create_profile, get_profile

    name = cleanup_test_profiles("plain")
    create_profile({"name": name, "api_key": "sk-l2-roundtrip-999"})
    prof = get_profile(name, include_key=True)
    assert prof["api_key"] == "sk-l2-roundtrip-999"
    # HTTP 默认口径仍是脱敏占位符
    assert get_profile(name)["api_key"] == "***已配置***"


def test_update_profile_masked_value_keeps_stored_ciphertext(cleanup_test_profiles, restore_state):
    """L-2 向后兼容：占位符 = 保持原值，库内密文原样保留、不二次加密不降级。"""
    from app.services.ai.profile_service import create_profile, update_profile

    name = cleanup_test_profiles("keep")
    create_profile({"name": name, "api_key": "sk-l2-keep-001"})
    with get_db(readonly=True) as c:
        before = c.execute("SELECT api_key_enc FROM ai_profiles WHERE name = ?", (name,)).fetchone()["api_key_enc"]
    # 模拟前端「只改模型不动密钥」的真实回传：占位符
    update_profile(name, {"api_key": "***已配置***", "model": "gpt-5"})
    with get_db(readonly=True) as c:
        after = c.execute("SELECT api_key_enc, model FROM ai_profiles WHERE name = ?", (name,)).fetchone()
    assert after["api_key_enc"] == before, "占位符保存不得把库内密文覆盖成占位符/明文"
    assert after["model"] == "gpt-5"


def test_legacy_plaintext_profile_still_readable(cleanup_test_profiles, restore_state):
    """L-2 兼容：历史明文行无需迁移即可读取（decrypt_api_key 透传）。"""
    from app.services.ai.profile_service import get_profile

    name = cleanup_test_profiles("legacy")
    with get_db() as c:
        c.execute(
            "INSERT INTO ai_profiles (name, display_name, provider_name, provider, api_key_enc, model, base_url, is_active, is_default) "
            "VALUES (?, 'legacy', 'agnes', 'openai_compatible', 'sk-legacy-plain', 'gpt-4o', '', 1, 0)",
            (name,),
        )
        c.commit()
    prof = get_profile(name, include_key=True)
    assert prof is not None
    assert prof["api_key"] == "sk-legacy-plain"


# ──────────────────────────────────────────────────────────────────────
# L-2b 兜底层 is_builtin_default 标记透传
# ──────────────────────────────────────────────────────────────────────

def test_fallback_builtin_flag_respects_per_entry_marking(restore_state, monkeypatch):
    """L-2b：第4层兜底条目不再无条件标 True —— 仅 agnes 为内置默认厂商。

    确定性前提：清空上层来源（主配置行/已配置厂商/history）、置空 Profile 层，
    并把主配置来源固定为独立 URL（否则默认回落 OpenAI URL 会把同名兜底去重），
    保证链中同名条目只能来自第4层；DB 清理由 restore_state 统一还原。
    旧实现（第4层硬写 True）与旧断言（拿上层同名条目比兜底标记）恰好
    互相掩盖，本次一并修正。
    """
    from app.services.ai.provider_factory import _FALLBACK_PROVIDERS, get_provider_priority_order
    import app.services.ai.profile_service as profile_service
    import app.services.ai.provider_factory as pf_mod

    monkeypatch.setattr(profile_service, "get_default_profile", lambda **kw: None)
    monkeypatch.setattr(pf_mod, "_load_env_config", lambda: None)
    monkeypatch.setattr(
        pf_mod, "get_ai_config",
        lambda **kw: {
            "provider": "openai_compatible", "api_key": "",
            "base_url": "https://primary.example.test/v1", "model": "test-model",
            "provider_name": "主配置(测试隔离)",
            "max_tokens": 4096, "temperature": 0.7, "concurrency": 4,
        },
    )

    with get_db() as c:
        c.execute("DELETE FROM ai_config WHERE id = 1")
        c.execute("DELETE FROM text_provider_configs")
        c.execute("DELETE FROM provider_history")
        c.commit()

    pf_mod._clear_ai_config_cache()

    fb_by_name = {f["name"]: f for f in _FALLBACK_PROVIDERS}
    chain = get_provider_priority_order()
    fallback_entries = [e for e in chain if e["name"] in fb_by_name and not e.get("is_primary")]
    # 上层已清空，全部 6 个兜底条目应原样出现在链尾
    assert len(fallback_entries) == len(fb_by_name), sorted(
        e["name"] for e in fallback_entries
    )
    for e in fallback_entries:
        expected = fb_by_name[e["name"]]["is_builtin_default"]
        assert e["is_builtin_default"] == expected, e["name"]
    # 可证伪核心：旧实现第4层无条件写 True —— 非 agnes 兜底必为 False
    non_agnes = [e for e in fallback_entries if e["name"] != "agnes"]
    assert non_agnes and all(not e["is_builtin_default"] for e in non_agnes)
    agnes = next(e for e in fallback_entries if e["name"] == "agnes")
    assert agnes["is_builtin_default"] is True


# ──────────────────────────────────────────────────────────────────────
# L-3 跨进程缓存失效
# ──────────────────────────────────────────────────────────────────────

def test_version_bump_invalidates_cache_across_processes(restore_state):
    """L-3：模拟另一进程 bump 版本 → 本进程 TTL 内缓存必须作废重建。"""
    import time

    import app.services.ai.provider_factory as pf
    from app.routers.ai_config import AIConfigData, save_ai_config

    # 进程 A：保存配置（写侧清缓存并递增版本）
    asyncio.run(save_ai_config(AIConfigData(
        provider_name="L3厂商", provider="openai_compatible", api_key="sk-l3-a",
        model="m-a", base_url="https://l3-a.example.com/v1",
    )))
    cfg1 = pf.get_ai_config()
    assert cfg1["provider_name"] == "L3厂商"

    # 模拟进程 B 写入：只动库和版本计数器，不动本进程缓存对象
    with get_db() as c:
        c.execute(
            "UPDATE ai_config SET provider_name = 'L3厂商B', model='m-b' WHERE id = 1"
        )
        c.commit()
    pf.bump_ai_config_version()
    # 还原「本进程已清缓存」的语义是不现实的 —— 正是要验证不清也能失效：
    # 直接把本进程缓存标记为「B 写入前已存在」，跳过版本初始化路径
    pf._ai_config_cache = dict(cfg1)  # 陈旧缓存（进程 A 视图）
    pf._ai_config_cache_time = time.time()
    pf._version_checked_at = 0.0  # 越过 5 秒防抖，强制本次比对
    pf._seen_config_version = pf._read_config_version() - 1  # 已知版本落后一档

    cfg2 = pf.get_ai_config()
    assert cfg2["provider_name"] == "L3厂商B", "版本号变化后 TTL 内命中必须失效重建"


def test_version_debounce_within_interval(restore_state):
    """L-3 性能约束：5 秒防抖期内重复调用不得每次都读版本表。"""
    import app.services.ai.provider_factory as pf
    from app.routers.ai_config import AIConfigData, save_ai_config

    asyncio.run(save_ai_config(AIConfigData(
        provider_name="L3deb", provider="openai_compatible", api_key="sk-l3-deb",
        model="m", base_url="https://l3-deb.example.com/v1",
    )))
    pf.get_ai_config()
    calls = []
    real_read = pf._read_config_version

    def _spy():
        calls.append(1)
        return real_read()

    pf._read_config_version = _spy
    try:
        for _ in range(20):
            pf.get_ai_config()
        assert len(calls) <= 1, f"防抖期内 20 次命中最多 1 次版本读取，实际 {len(calls)}"
    finally:
        pf._read_config_version = real_read


# ──────────────────────────────────────────────────────────────────────
# L-4 config_source 回显
# ──────────────────────────────────────────────────────────────────────

def test_get_ai_config_returns_config_source(restore_state):
    """L-4：GET /ai/config 必须回显 config_source，且 api_key 恒脱敏。"""
    from app.routers.ai_config import AIConfigData, get_ai_config, save_ai_config

    with get_db() as c:
        c.execute("DELETE FROM ai_config WHERE id = 1")
        c.commit()
    for k in ("AI_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY"):
        os.environ.pop(k, None)
    res = asyncio.run(get_ai_config())
    assert res["config_source"] == "default"

    asyncio.run(save_ai_config(AIConfigData(
        provider_name="L4厂商", provider="openai_compatible", api_key="sk-l4",
        model="m", base_url="https://l4.example.com/v1",
    )))
    res = asyncio.run(get_ai_config())
    assert res["config_source"] == "database"
    assert res["api_key"] == "***已配置***"

    os.environ["AI_API_KEY"] = "sk-l4-env"
    try:
        res = asyncio.run(get_ai_config())
        assert res["config_source"] == "env"
        # env 来源时 cfg.api_key 是环境变量明文，读接口同样必须脱敏
        assert res["api_key"] == "***已配置***"
    finally:
        os.environ.pop("AI_API_KEY", None)


# ──────────────────────────────────────────────────────────────────────
# L-5 审计日志
# ──────────────────────────────────────────────────────────────────────

def test_save_config_writes_audit_with_masked_key(restore_state):
    """L-5：保存主配置产生审计行；detail 不得含密钥原文或密文。"""
    from app.routers.ai_config import AIConfigData, get_config_audit, save_ai_config

    asyncio.run(save_ai_config(AIConfigData(
        provider_name="L5厂商", provider="openai_compatible", api_key="sk-l5-supersecret",
        model="m5", base_url="https://l5.example.com/v1",
    )))
    res = asyncio.run(get_config_audit(limit=10))
    entry = next(e for e in res["items"] if e["action"] == "save_config")
    assert entry["target"] == "L5厂商"
    assert "sk-l5-supersecret" not in entry["detail"]
    assert "ENC:" not in entry["detail"]
    assert "tail:" in entry["detail"]  # 仅后 4 位摘要
    assert entry["actor"]  # unknown 也必须有值（不留空主体）


def test_audit_failure_does_not_block_save(restore_state, monkeypatch):
    """L-5 容错：审计写入抛异常时配置保存主流程必须照常成功。"""
    import app.routers.ai_config as m
    from app.routers.ai_config import AIConfigData

    def _boom(*a, **k):
        raise RuntimeError("audit down")

    monkeypatch.setattr(m, "record_config_audit", _boom)
    res = asyncio.run(m.save_ai_config(AIConfigData(
        provider_name="L5b", provider="openai_compatible", api_key="sk-l5b",
        model="m", base_url="https://l5b.example.com/v1",
    )))
    assert res == {"status": "ok"}


# ──────────────────────────────────────────────────────────────────────
# L-6 泄露面收敛
# ──────────────────────────────────────────────────────────────────────

def test_resolve_api_key_no_agnes_fallback(restore_state):
    """L-6：库内无主配置密钥时，占位符解析返回空串 —— 绝不把 AGNES_API_KEY
    发往调用方任意指定的 base_url。"""
    from app.routers.ai_config import _resolve_api_key

    with get_db() as c:
        c.execute("DELETE FROM ai_config WHERE id = 1")
        c.commit()
    assert _resolve_api_key("***已配置***") == ""
    # 真实传入的 key 不受影响（原样透传）
    assert _resolve_api_key("sk-user-key") == "sk-user-key"
