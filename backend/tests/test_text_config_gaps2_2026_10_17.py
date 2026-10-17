"""文本模型配置模块：2026-10-17 后续加固项的回归测试。

覆盖项（编号与代码内 2026-10-16/17 注释对应）：
  G-8        save_ai_config 写入响应带 Cache-Control: no-cache
  MIG-38     image_provider_configs 部分唯一索引（至多一行 is_active=1）
  IMG-409    activate_image_provider_config 唯一冲突 → 409（对齐文本侧）
  XMOD-KEY   /ai/models 跨模块复用：图像厂商密钥从 image_provider_configs 解析
  RL-APPLY   应用 Profile 后其 rate_limit_per_min 生效（不再只看 is_active）
  PROBE-TMO  _quick_health_check 的 timeout 参数真正生效（下限 1s / 上限 5s）

同时固化上一轮报告的两处勘误（见报告第 6 章）：
  - save_text_provider_config 从不修改 is_active，不会触发 idx_tpc_unique_active；
  - 审计留存早已存在（cleanup_old_config_audit + main.py 定时清理）。
"""
from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def restore_state():
    """备份/还原相关表，并清空 provider_factory 缓存。"""
    from app.db import get_db

    tables = (
        "ai_config",
        "text_provider_configs",
        "image_provider_configs",
        "ai_profiles",
        "ai_config_state",
        "ai_config_audit",
    )
    snap = {}
    with get_db(readonly=True) as c:
        for t in tables:
            snap[t] = [dict(r) for r in c.execute(f"SELECT * FROM {t}").fetchall()]
    yield
    with get_db() as c:
        for t in tables:
            c.execute(f"DELETE FROM {t}")
            for row in snap[t]:
                cols = ", ".join(row.keys())
                marks = ", ".join("?" for _ in row)
                c.execute(
                    f"INSERT INTO {t} ({cols}) VALUES ({marks})", tuple(row.values())
                )
        c.commit()
    from app.services.ai import provider_factory as pf

    pf._ai_config_cache = None
    pf._ai_config_cache_time = 0
    pf._seen_config_version = -1
    pf._clear_health_cache()


def _make_request(**state):
    """构造最小可用 request：state 可注入属性，headers 供 if-match 乐观锁读取。"""

    class _State:
        pass

    class _Req:
        state = _State()
        headers: dict = {}

    req = _Req()
    for k, v in state.items():
        setattr(req.state, k, v)
    return req


def _set_ai_config(**fields):
    """覆盖写 ai_config 主表单行。"""
    from app.db import get_db

    defaults = dict(
        provider_name="DeepSeek",
        provider="deepseek",
        api_key="",
        model="deepseek-chat",
        base_url="https://api.deepseek.com/v1",
    )
    defaults.update(fields)
    with get_db() as c:
        c.execute("DELETE FROM ai_config WHERE id = 1")
        cols = ", ".join(defaults.keys())
        marks = ", ".join("?" for _ in defaults)
        c.execute(
            f"INSERT INTO ai_config (id, {cols}) VALUES (1, {marks})",
            tuple(defaults.values()),
        )
        c.commit()


def _set_text_provider(provider_name, api_key, **kw):
    """写入一条文本厂商隔离配置。列名与真实 schema 对齐（provider_type 非 provider）。"""
    from app.db import get_db

    row = dict(
        provider_name=provider_name,
        provider_type=kw.get("provider", "openai_compatible"),
        api_key=api_key,
        model=kw.get("model", "deepseek-v3"),
        base_url=kw.get("base_url", "https://api.deepseek.com/v1"),
        max_tokens=kw.get("max_tokens", 8192),
        temperature=kw.get("temperature", 0.7),
        concurrency=kw.get("concurrency", 4),
        timeout=kw.get("timeout", 120),
        document_profile=kw.get("document_profile", ""),
        is_active=kw.get("is_active", 0),
    )
    cols = ", ".join(row.keys())
    marks = ", ".join("?" for _ in row)
    with get_db() as c:
        c.execute(
            "DELETE FROM text_provider_configs WHERE provider_name = ?", (provider_name,)
        )
        if row["is_active"]:
            c.execute(
                "UPDATE text_provider_configs SET is_active = 0 WHERE provider_name != ?",
                (provider_name,),
            )
        c.execute(
            f"INSERT INTO text_provider_configs ({cols}) VALUES ({marks})",
            tuple(row.values()),
        )
        c.commit()


def _set_image_provider(provider_name, api_key, is_active=0, **kw):
    """写入一条图像厂商配置（直写，绕过 HTTP 层校验）。"""
    from app.db import get_db

    row = dict(
        provider_name=provider_name,
        api_key=api_key,
        base_url=kw.get("base_url", "https://api.example.com/v1"),
        model=kw.get("model", "model-v1"),
        max_concurrency=kw.get("max_concurrency", 2),
        max_per_project=kw.get("max_per_project", 10),
        quality=kw.get("quality", "standard"),
        style_injection=kw.get("style_injection", ""),
        enabled=kw.get("enabled", 1),
        default_size=kw.get("default_size", "1024x1024"),
        default_ratio=kw.get("default_ratio", "1:1"),
        price_per_image=kw.get("price_per_image", 0.0),
        is_active=is_active,
        created_at=kw.get("created_at", "2026-01-01 00:00:00"),
        updated_at=kw.get("updated_at", "2026-01-01 00:00:00"),
    )
    cols = ", ".join(row.keys())
    marks = ", ".join("?" for _ in row)
    with get_db() as c:
        c.execute(
            "DELETE FROM image_provider_configs WHERE provider_name = ?", (provider_name,)
        )
        c.execute(
            f"INSERT INTO image_provider_configs ({cols}) VALUES ({marks})",
            tuple(row.values()),
        )
        c.commit()


def _set_profile(name, rate_limit_per_min=0, concurrency=4, is_active=0):
    """写入一条 AI Profile（直写，用于限流解析测试）。"""
    from app.db import get_db

    row = dict(
        name=name,
        display_name=name,
        description="",
        provider_name="DeepSeek",
        provider="deepseek",
        api_key_enc="",
        model="deepseek-chat",
        base_url="https://api.deepseek.com/v1",
        max_tokens=8192,
        temperature=0.7,
        concurrency=concurrency,
        timeout=120,
        json_mode=0,
        task_type="",
        rate_limit_per_min=rate_limit_per_min,
        token_budget_daily=0,
        is_active=is_active,
        is_default=1 if is_active else 0,
        created_at="2026-01-01 00:00:00",
        updated_at="2026-01-01 00:00:00",
    )
    cols = ", ".join(row.keys())
    marks = ", ".join("?" for _ in row)
    with get_db() as c:
        c.execute("DELETE FROM ai_profiles WHERE name = ?", (name,))
        if is_active:
            c.execute(
                "UPDATE ai_profiles SET is_active = 0, is_default = 0 WHERE name != ?",
                (name,),
            )
        c.execute(
            f"INSERT INTO ai_profiles ({cols}) VALUES ({marks})", tuple(row.values())
        )
        c.commit()



# ---------------------------------------------------------------------------
# G-8：save_ai_config 写入响应的缓存声明
# ---------------------------------------------------------------------------
class TestSaveAiConfigCacheControl:
    def test_save_response_declares_no_cache(self, restore_state):
        """配置刚变更却允许被启发式缓存，下一次 GET /ai/config 会拿到过期内容。"""
        import app.routers.ai_config as m
        from app.services.ai.provider_factory import get_ai_config_version

        req = _make_request(current_user={"id": 1, "username": "alice"})
        req.headers = {"if-match": str(get_ai_config_version())}
        resp = asyncio.run(
            m.save_ai_config(
                m.AIConfigData(
                    provider_name="CacheCtl", provider="deepseek",
                    api_key="sk-cache-1234", model="m",
                    base_url="https://api.deepseek.com/v1",
                ),
                request=req,
            )
        )
        assert resp.headers.get("cache-control") == "no-cache"

    def test_save_response_keeps_etag_and_version(self, restore_state):
        """no-cache 是追加项，不得挤掉既有 P0-CONTRACT 契约头。"""
        import app.routers.ai_config as m
        from app.services.ai.provider_factory import get_ai_config_version

        req = _make_request(current_user={"id": 1, "username": "alice"})
        req.headers = {"if-match": str(get_ai_config_version())}
        resp = asyncio.run(
            m.save_ai_config(
                m.AIConfigData(
                    provider_name="CacheCtl2", provider="deepseek",
                    api_key="sk-cache-5678", model="m",
                    base_url="https://api.deepseek.com/v1",
                ),
                request=req,
            )
        )
        ver = get_ai_config_version()
        assert resp.headers.get("etag") == str(ver)
        assert resp.headers.get("version") == str(ver)
        body = json.loads(resp.body.decode("utf-8"))
        assert body["status"] == "ok"
        assert body["version"] == ver


# ---------------------------------------------------------------------------
# MIG-38：image_provider_configs 部分唯一索引
# ---------------------------------------------------------------------------
class TestImageProviderUniqueActiveIndex:
    @staticmethod
    def _index_sql():
        from app.db import get_db

        with get_db(readonly=True) as c:
            row = c.execute(
                "SELECT sql FROM sqlite_master"
                " WHERE type = 'index' AND name = 'idx_imgpc_unique_active'"
            ).fetchone()
        return (row[0] if row else None) or ""

    def test_partial_unique_index_exists(self, restore_state):
        sql = self._index_sql()
        assert sql, "迁移 #38 未创建 idx_imgpc_unique_active"
        assert "WHERE is_active = 1" in sql
        assert "image_provider_configs" in sql

    def test_second_active_row_violates_index(self, restore_state):
        """两条 is_active=1 → UNIQUE 约束失败（部分唯一索引语义生效）。"""
        _set_image_provider("ImageA", "sk-a-1111", is_active=1)
        with pytest.raises(sqlite3.IntegrityError):
            _set_image_provider("ImageB", "sk-b-2222", is_active=1)

    def test_multiple_inactive_rows_still_allowed(self, restore_state):
        """部分索引只约束 is_active=1：未激活行可任意并存。"""
        _set_image_provider("ImageC", "sk-c-1111", is_active=0)
        _set_image_provider("ImageD", "sk-d-2222", is_active=0)
        _set_image_provider("ImageE", "sk-e-3333", is_active=0)
        from app.db import get_db

        with get_db(readonly=True) as c:
            n = c.execute("SELECT COUNT(*) FROM image_provider_configs").fetchone()[0]
        assert n == 3

    def test_migration_dedupes_preexisting_duplicates(self, restore_state):
        """存量脏数据（多行 active）在迁移时按 updated_at 收敛到最新一行。"""
        from app.db import get_db
        from app.db_migrations import ensure_enhancement_tables

        with get_db() as c:
            c.execute("DROP INDEX IF EXISTS idx_imgpc_unique_active")
            c.commit()
        _set_image_provider("ImageOld", "sk-old", is_active=1,
                            created_at="2025-01-01 00:00:00",
                            updated_at="2025-01-01 00:00:00")
        _set_image_provider("ImageNew", "sk-new", is_active=1,
                            created_at="2026-01-01 00:00:00",
                            updated_at="2026-06-01 00:00:00")

        ensure_enhancement_tables()

        with get_db(readonly=True) as c:
            active = [
                r["provider_name"]
                for r in c.execute(
                    "SELECT provider_name FROM image_provider_configs WHERE is_active = 1"
                ).fetchall()
            ]
        assert active == ["ImageNew"]
        assert self._index_sql(), "迁移重跑后索引应重建"

    def test_migration_is_idempotent(self, restore_state):
        """重复执行迁移不报错、不破坏现状。"""
        from app.db import get_db
        from app.db_migrations import ensure_enhancement_tables

        _set_image_provider("ImageOnly", "sk-only", is_active=1)
        ensure_enhancement_tables()
        ensure_enhancement_tables()
        with get_db(readonly=True) as c:
            active = [
                r["provider_name"]
                for r in c.execute(
                    "SELECT provider_name FROM image_provider_configs WHERE is_active = 1"
                ).fetchall()
            ]
        assert active == ["ImageOnly"]



# ---------------------------------------------------------------------------
# IMG-409：图像厂商激活的唯一冲突语义化
# ---------------------------------------------------------------------------
class TestImageActivateUniqueConflict:
    def test_sequential_activation_of_second_provider_succeeds(self, restore_state):
        """正常路径：串行激活 A 再激活 B，两者都成功，最终只有 B 为 active。"""
        import app.routers.ai_config as m

        _set_image_provider("ImageA", "sk-a-1111", is_active=1)
        _set_image_provider("ImageB", "sk-b-2222", is_active=0)
        req = _make_request(current_user={"id": 1, "username": "alice"})

        r1 = asyncio.run(m.activate_image_provider_config("ImageA", request=req))
        r2 = asyncio.run(m.activate_image_provider_config("ImageB", request=req))
        assert r1["status"] == "ok"
        assert r2["status"] == "ok"

        from app.db import get_db

        with get_db(readonly=True) as c:
            active = [
                r["provider_name"]
                for r in c.execute(
                    "SELECT provider_name FROM image_provider_configs WHERE is_active = 1"
                ).fetchall()
            ]
        assert active == ["ImageB"]

    def test_activate_missing_provider_returns_404(self, restore_state):
        import app.routers.ai_config as m
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc:
            asyncio.run(m.activate_image_provider_config("NoSuchProvider"))
        assert exc.value.status_code == 404

    def test_unique_integrity_error_maps_to_409(self, restore_state, monkeypatch):
        """跨进程并发激活撞唯一索引 → 409，不再冒泡成 500。"""
        import app.routers.ai_config as m
        from fastapi import HTTPException

        _set_image_provider("ImageA", "sk-a-1111", is_active=1)

        async def _boom(_fn):
            raise sqlite3.IntegrityError(
                "UNIQUE constraint failed: image_provider_configs.is_active"
            )

        monkeypatch.setattr(m.asyncio, "to_thread", _boom)
        with pytest.raises(HTTPException) as exc:
            asyncio.run(m.activate_image_provider_config("ImageA"))
        assert exc.value.status_code == 409

    def test_non_unique_integrity_error_is_reraised(self, restore_state, monkeypatch):
        """非唯一类完整性错误不得被吞成 409，仍按原样上抛。"""
        import app.routers.ai_config as m

        _set_image_provider("ImageA", "sk-a-1111", is_active=1)

        async def _boom(_fn):
            raise sqlite3.IntegrityError(
                "NOT NULL constraint failed: image_provider_configs.api_key"
            )

        monkeypatch.setattr(m.asyncio, "to_thread", _boom)
        with pytest.raises(sqlite3.IntegrityError):
            asyncio.run(m.activate_image_provider_config("ImageA"))


# ---------------------------------------------------------------------------
# XMOD-KEY：/ai/models 跨模块复用时的图像厂商密钥解析
# ---------------------------------------------------------------------------
class TestResolveProviderApiKeyImageFallback:
    def test_masked_key_falls_back_to_image_provider_configs(self, restore_state):
        """图像厂商密钥只存 image_provider_configs，占位符必须解析到它。"""
        import app.routers.ai_config as m

        _set_ai_config(provider_name="DeepSeek", api_key="sk-primary-master")
        _set_image_provider("ImageOnly", "sk-image-only-9999")
        got = m._resolve_provider_api_key("ImageOnly", m.API_KEY_MASK)
        assert got == "sk-image-only-9999"

    def test_text_provider_key_takes_precedence_over_image(self, restore_state):
        """两表同名配置时以文本表为准 —— 记录方向性，避免日后误读。"""
        import app.routers.ai_config as m

        _set_ai_config(provider_name="DeepSeek", api_key="sk-primary-master")
        _set_text_provider("Both", "sk-text-side")
        _set_image_provider("Both", "sk-image-side")
        got = m._resolve_provider_api_key("Both", m.API_KEY_MASK)
        assert got == "sk-text-side"

    def test_real_key_returned_verbatim_ignoring_tables(self, restore_state):
        """真实明文直通（quick-setup 路径），与表内容无关。"""
        import app.routers.ai_config as m

        _set_image_provider("ImageOnly", "sk-image-only-9999")
        got = m._resolve_provider_api_key("ImageOnly", "sk-user-typed")
        assert got == "sk-user-typed"

    def test_missing_in_both_falls_back_to_primary(self, restore_state):
        """两表都查不到 → 回落主配置密钥（修复前行为，向后兼容）。"""
        import app.routers.ai_config as m

        _set_ai_config(provider_name="DeepSeek", api_key="sk-primary-master")
        got = m._resolve_provider_api_key("Nobody", m.API_KEY_MASK)
        assert got == "sk-primary-master"

    def test_encrypted_image_key_is_decrypted(self, restore_state, monkeypatch):
        """图像侧存的 ENC: 密文也要正确解密。"""
        import app.routers.ai_config as m

        _set_ai_config(provider_name="DeepSeek", api_key="sk-primary-master")
        monkeypatch.setattr(m, "is_encrypted", lambda v: str(v).startswith("ENC:"))
        monkeypatch.setattr(m, "decrypt_api_key", lambda v: "sk-decrypted-777")
        _set_image_provider("ImageEnc", "ENC:ciphertext-blob")
        got = m._resolve_provider_api_key("ImageEnc", m.API_KEY_MASK)
        assert got == "sk-decrypted-777"



# ---------------------------------------------------------------------------
# RL-APPLY：应用 Profile 后其限流配额生效
# ---------------------------------------------------------------------------
class TestAppliedProfileLimitsPrecedence:
    def test_limits_prefer_applied_profile_over_default(self, restore_state):
        """应用了非默认 Profile 后，其 rate_limit_per_min 必须生效。"""
        from app.services.ai import provider_factory as pf

        _set_profile("Default", rate_limit_per_min=100, concurrency=8, is_active=1)
        _set_profile("Applied", rate_limit_per_min=7, concurrency=3, is_active=0)
        _set_ai_config(provider_name="DeepSeek", api_key="", model="deepseek-chat",
                      base_url="https://api.deepseek.com/v1",
                      current_profile_name="Applied")

        limits = pf._load_active_profile_limits()
        assert limits["rate_limit_per_min"] == 7
        assert limits["concurrency"] == 3

    def test_limits_fall_back_to_default_when_nothing_applied(self, restore_state):
        """未应用任何 Profile → 保持原有默认 Profile 语义。"""
        from app.services.ai import provider_factory as pf

        _set_profile("Default", rate_limit_per_min=100, concurrency=8, is_active=1)
        _set_ai_config(provider_name="DeepSeek", api_key="", model="deepseek-chat",
                      base_url="https://api.deepseek.com/v1", current_profile_name="")

        limits = pf._load_active_profile_limits()
        assert limits["rate_limit_per_min"] == 100

    def test_limits_fall_back_to_default_when_applied_profile_gone(
        self, restore_state
    ):
        """应用记录指向已删除的 Profile → 不空转，回落到默认 Profile。"""
        from app.services.ai import provider_factory as pf

        _set_profile("Default", rate_limit_per_min=42, concurrency=6, is_active=1)
        _set_ai_config(provider_name="DeepSeek", api_key="", model="deepseek-chat",
                      base_url="https://api.deepseek.com/v1",
                      current_profile_name="DeletedProfile")

        limits = pf._load_active_profile_limits()
        assert limits["rate_limit_per_min"] == 42

    def test_zero_limit_of_applied_profile_is_honored(self, restore_state):
        """0 = 不限速 是显式语义，不得被默认 Profile 的配额顶替。"""
        from app.services.ai import provider_factory as pf

        _set_profile("Default", rate_limit_per_min=100, concurrency=8, is_active=1)
        _set_profile("Unlimited", rate_limit_per_min=0, concurrency=0, is_active=0)
        _set_ai_config(provider_name="DeepSeek", api_key="", model="deepseek-chat",
                      base_url="https://api.deepseek.com/v1",
                      current_profile_name="Unlimited")

        limits = pf._load_active_profile_limits()
        assert limits["rate_limit_per_min"] == 0
        assert limits["concurrency"] == 0

    def test_empty_result_when_no_profiles_at_all(self, restore_state):
        """无 Profile → 空 dict（缺口即不限流，缺省行为不变）。

        注意：测试库由 init_db 预置了默认 Profile，必须显式清空才能覆盖此分支。
        """
        from app.db import get_db
        from app.services.ai import provider_factory as pf

        with get_db() as c:
            c.execute("DELETE FROM ai_profiles")
            c.commit()
        _set_ai_config(provider_name="DeepSeek", api_key="", model="deepseek-chat",
                      base_url="https://api.deepseek.com/v1", current_profile_name="")
        assert pf._load_active_profile_limits() == {}

    def test_applied_profile_limit_reaches_get_ai_config(self, restore_state):
        """端到端：应用 Profile 的限流值最终进入生成链读到的 config。"""
        from app.services.ai import provider_factory as pf

        _set_profile("Default", rate_limit_per_min=100, concurrency=8, is_active=1)
        _set_profile("Applied", rate_limit_per_min=7, concurrency=3, is_active=0)
        _set_ai_config(provider_name="DeepSeek", api_key="sk-live-1234",
                      model="deepseek-chat", base_url="https://api.deepseek.com/v1",
                      current_profile_name="Applied")

        cfg = pf.get_ai_config()
        assert cfg.get("rate_limit_per_min") == 7



# ---------------------------------------------------------------------------
# PROBE-TMO：健康探活超时真正受配置控制
# ---------------------------------------------------------------------------
class TestQuickHealthCheckTimeout:
    @staticmethod
    def _capture_timeout(monkeypatch, response_status=200):
        """替换 httpx.Client，捕获构造时的 timeout 参数。"""
        import httpx

        captured = {}

        class _Resp:
            def __init__(self, status_code):
                self.status_code = status_code

        class _Client:
            def __init__(self, timeout=None, **_kw):
                captured["timeout"] = timeout

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def post(self, *a, **kw):
                return _Resp(response_status)

        monkeypatch.setattr(httpx, "Client", _Client)
        return captured

    @staticmethod
    def _assert_timeout(captured, total, connect):
        """断言 httpx.Timeout 的四项分量。

        注意：httpx.Timeout 只与另一个 Timeout 相等、不与 float 相等，故逐分量断言。
        """
        t = captured["timeout"]
        assert t.connect == connect
        assert t.read == total
        assert t.write == total
        assert t.pool == total

    def test_configured_timeout_is_honored(self, monkeypatch):
        """厂商把 timeout 调小想快速失败，探活必须同步缩短。"""
        from app.services.ai import provider_factory as pf

        captured = self._capture_timeout(monkeypatch)
        assert pf._quick_health_check("https://x.example.com/v1", "sk-1", "m", 2) is True
        self._assert_timeout(captured, total=2, connect=2)

    def test_probe_timeout_capped_at_five_seconds(self, monkeypatch):
        """配置里填 600s 不得把整条降级链拖成十分钟。"""
        from app.services.ai import provider_factory as pf

        captured = self._capture_timeout(monkeypatch)
        pf._quick_health_check("https://x.example.com/v1", "sk-1", "m", 600)
        self._assert_timeout(captured, total=5, connect=3)

    def test_probe_timeout_default_is_five(self, monkeypatch):
        """未给 timeout（None / 0）时保持原有 5s 行为。"""
        from app.services.ai import provider_factory as pf

        for bogus in (None, 0):
            captured = self._capture_timeout(monkeypatch)
            pf._quick_health_check("https://x.example.com/v1", "sk-1", "m", bogus)
            self._assert_timeout(captured, total=5, connect=3)

    def test_probe_timeout_floored_at_one(self, monkeypatch):
        """非法负值不得变成 0（httpx 的 0 语义是关闭超时，方向反转）。"""
        from app.services.ai import provider_factory as pf

        captured = self._capture_timeout(monkeypatch)
        pf._quick_health_check("https://x.example.com/v1", "sk-1", "m", -3)
        self._assert_timeout(captured, total=1, connect=1)

    def test_429_is_not_healthy(self, monkeypatch):
        """限流响应不得被误判为健康（TD-3 语义，防回归）。"""
        from app.services.ai import provider_factory as pf

        self._capture_timeout(monkeypatch, response_status=429)
        assert pf._quick_health_check("https://x.example.com/v1", "sk-1", "m", 5) is False



# ---------------------------------------------------------------------------
# 勘误确认：上一轮报告的误判在此固化，避免被"修复"回退
# ---------------------------------------------------------------------------
class TestReportErrataGuards:
    def test_save_text_provider_config_never_writes_is_active(self, restore_state):
        """勘误 1：保存路径从不改 is_active，故不会触发 idx_tpc_unique_active。

        上一轮报告称「保存第二个厂商会抛 IntegrityError」—— 实测 INSERT 恒写
        is_active=0、UPDATE 恒保留原值，该说法不成立。此处固化，防止未来有人
        误加 is_active 写入而破坏部分唯一索引（保存端不该承担激活语义）。
        """
        import inspect

        import app.routers.ai_config as m

        src = inspect.getsource(m.save_text_provider_config)
        # INSERT 分支的 is_active 必须是字面量 0
        assert "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)" in src
        # UPDATE 分支必须保留原值，不得赋值
        assert "is_active=text_provider_configs.is_active" in src
        assert "is_active=excluded" not in src

    def test_save_text_provider_config_of_second_provider_succeeds(self, restore_state):
        """勘误 1 行为面：已激活 A 之后再保存 B，不报冲突、A 仍为激活。"""
        import app.routers.ai_config as m

        _set_ai_config(provider_name="DeepSeek", api_key="", model="m",
                       base_url="https://api.deepseek.com/v1")
        _set_text_provider("TextA", "sk-a-1111", is_active=1)
        from app.db import get_db

        def _count():
            with get_db(readonly=True) as c:
                return c.execute("SELECT COUNT(*) FROM text_provider_configs").fetchone()[0]

        before = _count()
        data = m.TextProviderConfigData(
            provider_name="TextB", api_key="sk-b-2222",
            base_url="https://api.deepseek.com/v1", model="m2",
            # 显式关闭自动激活，隔离「保存」与「激活」两条路径
            activate_after_save=False,
        )
        resp = asyncio.run(m.save_text_provider_config("TextB", data))
        assert resp["status"] == "ok"

        with get_db(readonly=True) as c:
            active = [
                r["provider_name"]
                for r in c.execute(
                    "SELECT provider_name FROM text_provider_configs"
                    " WHERE is_active = 1"
                ).fetchall()
            ]
        # 用增量而非绝对值断言：测试库可能已预置其他厂商配置
        assert _count() == before + 1
        assert active == ["TextA"]

    def test_audit_retention_cleanup_is_callable(self, restore_state):
        """勘误 2：审计留存策略早已存在（2026-10-11 起），上一轮报告称缺失有误。"""
        from app.services.ai.provider_factory import cleanup_old_config_audit

        # best-effort 语义：不抛异常、返回非负删除数
        assert cleanup_old_config_audit(retention_days=1) >= 0
        # 负值/零保留期被钳制，不抛异常
        assert cleanup_old_config_audit(retention_days=0) >= 0

    def test_cleanup_loop_is_wired_in_main(self):
        """勘误 2 续：清理函数必须在 main.py 里被定时调用，否则等于没有留存策略。"""
        import pathlib

        main_path = (
            pathlib.Path(__file__).resolve().parents[1] / "app" / "main.py"
        )
        main_src = main_path.read_text(encoding="utf-8")
        assert "CONFIG_AUDIT_RETENTION_DAYS" in main_src

        # 调用点必须紧跟 asyncio.to_thread（2026-10-14 阻塞修复，防退化）
        idx = main_src.find("cleanup_old_config_audit, retention_days")
        assert idx > 0, "main.py 缺少审计清理调用点"
        assert "to_thread" in main_src[max(0, idx - 120):idx]

