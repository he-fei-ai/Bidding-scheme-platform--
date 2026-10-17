"""文本模型配置模块：2026-10-16 深审缺口闭环的回归测试。

覆盖项（编号与代码内 G-x / BUG-x 注释对应）：
  G-1   _resolve_provider_api_key：占位符密钥按厂商隔离解析（新增函数）
  G-1b  /ai/models、/ai/test 走厂商隔离密钥（端点接线）
  G-2   _actor_of 审计主体修复：优先读 request.state.current_user dict
  G-3   get_ai_config 回落主表为空时同步 timeout / temperature
  G-4   get_provider_priority_order Profile 层携带生成参数
  G-5   list_image_provider_configs 同步读移出事件循环
  BUG-1 save_ai_config 返回契约（JSONResponse + etag）—— 遗留测试漂移修复
"""
from __future__ import annotations

import asyncio
import inspect
import json

import pytest


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def restore_text_state():
    """备份/还原 ai_config、text_provider_configs、ai_profiles、ai_config_state，
    并清空 provider_factory 缓存与降级链探活缓存。"""
    from app.db import get_db

    snap = {}
    with get_db(readonly=True) as c:
        row = c.execute("SELECT * FROM ai_config WHERE id = 1").fetchone()
        snap["ai_config"] = dict(row) if row else None
        snap["text_provider_configs"] = [
            dict(r) for r in c.execute("SELECT * FROM text_provider_configs").fetchall()
        ]
        snap["ai_profiles"] = [
            dict(r) for r in c.execute("SELECT * FROM ai_profiles").fetchall()
        ]
        snap["ai_config_state"] = [
            dict(r) for r in c.execute("SELECT * FROM ai_config_state").fetchall()
        ]
    yield
    with get_db() as c:
        for table in ("ai_config_state", "text_provider_configs", "ai_profiles", "ai_config"):
            c.execute(f"DELETE FROM {table}")
        if snap["ai_config"]:
            row = snap["ai_config"]
            cols = ", ".join(row.keys())
            marks = ", ".join("?" for _ in row)
            c.execute(f"INSERT INTO ai_config ({cols}) VALUES ({marks})", tuple(row.values()))
        for table in ("ai_config_state", "text_provider_configs", "ai_profiles"):
            for row in snap[table]:
                cols = ", ".join(row.keys())
                marks = ", ".join("?" for _ in row)
                c.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", tuple(row.values()))
        c.commit()
    from app.services.ai import provider_factory as pf

    pf._ai_config_cache = None
    pf._ai_config_cache_time = 0
    pf._seen_config_version = -1
    pf._clear_health_cache()
    # 复位默认 Profile 关联的主表字段（create_profile(is_default=True) 会写入）
    if snap["ai_config"]:
        with get_db() as c:
            c.execute(
                "UPDATE ai_config SET current_profile_name = ? WHERE id = 1",
                (snap["ai_config"].get("current_profile_name", ""),),
            )
            c.commit()


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


def _set_text_provider(provider_name, api_key, timeout=120, temperature=0.7, **kw):
    """写入一条文本厂商隔离配置（api_key 支持 ENC: 密文）。

    列名与 db_migrations.py 的 text_provider_configs 实际 schema 对齐：
    注意是 provider_type（非 provider），且尚无 rate_limit_per_min 列。
    """
    from app.db import get_db

    row = dict(
        provider_name=provider_name,
        provider_type=kw.get("provider", "openai_compatible"),
        api_key=api_key,
        model=kw.get("model", "deepseek-v3"),
        base_url=kw.get("base_url", "https://api.deepseek.com/v1"),
        max_tokens=kw.get("max_tokens", 8192),
        temperature=temperature,
        concurrency=kw.get("concurrency", 4),
        timeout=timeout,
        document_profile=kw.get("document_profile", ""),
        is_active=kw.get("is_active", 0),
    )
    cols = ", ".join(row.keys())
    marks = ", ".join("?" for _ in row)
    with get_db() as c:
        c.execute(
            "DELETE FROM text_provider_configs WHERE provider_name = ?", (provider_name,)
        )
        # idx_tpc_unique_active 部分唯一索引：全表最多一行 is_active=1
        if row["is_active"]:
            c.execute(
                "UPDATE text_provider_configs SET is_active = 0 "
                "WHERE provider_name != ?",
                (provider_name,),
            )
        c.execute(f"INSERT INTO text_provider_configs ({cols}) VALUES ({marks})",
                  tuple(row.values()))
        c.commit()
# ---------------------------------------------------------------------------
# G-1：_resolve_provider_api_key 语义
# ---------------------------------------------------------------------------
class TestResolveProviderApiKey:
    def test_masked_placeholder_resolves_provider_specific_key(self, restore_text_state):
        """占位符 + 已配置厂商 → 该厂商自己的密钥，绝不泄漏主配置密钥。"""
        from app.routers.ai_config import API_KEY_MASK, _resolve_provider_api_key
        from app.services.crypto import encrypt_api_key

        _set_text_provider("dashscope", encrypt_api_key("sk-dash-real"))
        _set_text_provider("zhipu", encrypt_api_key("sk-zhipu-real"))
        _set_ai_config(api_key=encrypt_api_key("sk-primary-master"))

        got = _resolve_provider_api_key("dashscope", API_KEY_MASK)
        assert got == "sk-dash-real"
        assert got != "sk-primary-master"

    def test_masked_placeholder_falls_back_to_primary_when_unconfigured(self, restore_text_state):
        """占位符 + 未配置厂商 → 回落主配置（向后兼容）。"""
        from app.routers.ai_config import API_KEY_MASK, _resolve_provider_api_key
        from app.services.crypto import encrypt_api_key

        _set_text_provider("zhipu", encrypt_api_key("sk-zhipu-real"))
        _set_ai_config(api_key=encrypt_api_key("sk-primary-master"))

        assert (
            _resolve_provider_api_key("dashscope", API_KEY_MASK) == "sk-primary-master"
        )

    def test_masked_placeholder_empty_when_neither_configured(self, restore_text_state):
        """占位符 + 两边都无配置 → 返回空串（不发必然 401 的请求）。"""
        from app.routers.ai_config import API_KEY_MASK, _resolve_provider_api_key

        _set_ai_config(api_key="")
        assert _resolve_provider_api_key("dashscope", API_KEY_MASK) == ""

    def test_real_key_returned_verbatim_without_decrypt(self, restore_text_state, monkeypatch):
        """真实新填密钥直接透传（不得解密/不得二次加密/不得覆盖）。"""
        from app.routers.ai_config import _resolve_provider_api_key

        _set_text_provider("zhipu", "should-not-be-used")
        _set_ai_config(api_key="should-not-be-used-2")

        def _boom(*a, **k):
            raise AssertionError("真实密钥不应触发任何解密调用")

        monkeypatch.setattr("app.services.crypto.decrypt_api_key", _boom)
        assert (
            _resolve_provider_api_key("zhipu", "sk-user-typed-fresh")
            == "sk-user-typed-fresh"
        )

    def test_empty_or_none_input_returns_empty(self, restore_text_state):
        """空串/None 不触发回落：保持确定性的「未提供密钥」语义。"""
        from app.routers.ai_config import _resolve_provider_api_key

        _set_text_provider("zhipu", "should-not-be-used")
        _set_ai_config(api_key="should-not-be-used-2")
        assert _resolve_provider_api_key("zhipu", "") == ""
        assert _resolve_provider_api_key("zhipu", None) is None

    def test_encrypted_input_is_decrypted(self, restore_text_state):
        """ENC: 密文输入返回解密后的明文（供真实调用使用）。"""
        from app.routers.ai_config import _resolve_provider_api_key
        from app.services.crypto import encrypt_api_key

        _set_text_provider("zhipu", encrypt_api_key("sk-cipher-real"))
        assert (
            _resolve_provider_api_key("zhipu", encrypt_api_key("sk-cipher-real"))
            == "sk-cipher-real"
        )

# ---------------------------------------------------------------------------
# G-1b：/ai/models 与 /ai/test 走厂商隔离密钥
# ---------------------------------------------------------------------------
class TestEndpointKeyWiring:
    def test_model_fetch_request_accepts_provider_field(self):
        """请求模型新增可选 provider 字段，默认空串保证旧客户端兼容。"""
        from app.routers.ai_config import ModelFetchRequest

        assert ModelFetchRequest(api_key="k", base_url="https://x/v1").provider == ""
        assert (
            ModelFetchRequest(api_key="k", base_url="https://x/v1", provider="zhipu").provider
            == "zhipu"
        )

    def test_fetch_available_models_uses_provider_key(self, restore_text_state):
        """/ai/models 必须把该厂商自己的密钥放进 Authorization，而非主配置密钥。"""
        import httpx

        import app.routers.ai_config as m
        from app.routers.ai_config import API_KEY_MASK
        from app.services.crypto import encrypt_api_key

        _set_text_provider(
            "zhipu", encrypt_api_key("sk-zhipu-real"),
            base_url="https://open.bigmodel.cn/api/paas/v4",
        )
        _set_ai_config(api_key=encrypt_api_key("sk-primary-master"))

        captured = {}

        class _FakeResp:
            status_code = 200

            def json(self):
                return {"data": [{"id": "glm-4"}]}

        class _FakeClient:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, url, headers=None):
                captured["url"] = url
                captured["headers"] = headers
                return _FakeResp()

        orig = httpx.AsyncClient
        httpx.AsyncClient = _FakeClient
        try:
            data = asyncio.run(m.fetch_available_models(m.ModelFetchRequest(
                api_key=API_KEY_MASK, base_url="https://open.bigmodel.cn/api/paas/v4",
                provider="zhipu",
            )))
        finally:
            httpx.AsyncClient = orig

        assert data["models"] == ["glm-4"]
        assert captured["headers"]["Authorization"] == "Bearer sk-zhipu-real"
        assert "sk-primary-master" not in captured["headers"]["Authorization"]

    def test_fetch_available_models_without_provider_keeps_primary_fallback(
        self, restore_text_state
    ):
        """未传 provider 的旧客户端：仍回落主配置密钥（向后兼容，行为不突变）。"""
        import httpx

        import app.routers.ai_config as m
        from app.routers.ai_config import API_KEY_MASK
        from app.services.crypto import encrypt_api_key

        _set_ai_config(api_key=encrypt_api_key("sk-primary-master"))

        captured = {}

        class _FakeResp:
            status_code = 200

            def json(self):
                return {"data": [{"id": "m1"}]}

        class _FakeClient:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, url, headers=None):
                captured["headers"] = headers
                return _FakeResp()

        orig = httpx.AsyncClient
        httpx.AsyncClient = _FakeClient
        try:
            asyncio.run(m.fetch_available_models(m.ModelFetchRequest(
                api_key=API_KEY_MASK, base_url="https://api.x.com/v1")))
        finally:
            httpx.AsyncClient = orig

        assert captured["headers"]["Authorization"] == "Bearer sk-primary-master"

    def test_test_ai_connection_uses_provider_key(self, restore_text_state, monkeypatch):
        """/ai/test 传 provider 时，构造 provider 的 config 里必须是厂商隔离密钥。"""
        import app.routers.ai_config as m
        from app.routers.ai_config import AIConfigTest, API_KEY_MASK, test_ai_connection
        from app.services.crypto import encrypt_api_key

        _set_text_provider("zhipu", encrypt_api_key("sk-zhipu-real"))
        _set_ai_config(api_key=encrypt_api_key("sk-primary-master"))

        captured = {}

        class _FakeProvider:
            async def chat(self, messages=None, max_tokens=0, temperature=0.0):
                return "OK"

        def _capture(config):
            captured["config"] = config
            return _FakeProvider()

        monkeypatch.setattr(m, "_create_shared_provider", _capture)
        res = asyncio.run(m.test_ai_connection(AIConfigTest(
            provider="zhipu", api_key=API_KEY_MASK, model="glm-4",
            base_url="https://open.bigmodel.cn/api/paas/v4",
        )))
        assert res["status"] == "ok"
        assert captured["config"]["api_key"] == "sk-zhipu-real"
        assert captured["config"]["api_key"] != "sk-primary-master"

# ---------------------------------------------------------------------------
# G-2：_actor_of 审计主体
# ---------------------------------------------------------------------------
class TestActorResolution:
    @staticmethod
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

    def test_actor_of_reads_current_user_dict(self):
        """JwtAuthMiddleware 注入的 request.state.current_user dict 必须被识别。"""
        import app.routers.ai_config as m

        req = self._make_request(current_user={"id": 7, "username": "alice"})
        assert m._actor_of(req) == "alice"

    def test_actor_of_falls_back_to_user_object(self):
        """兼容旧 request.state.user 对象形态。"""
        import app.routers.ai_config as m

        class _User:
            username = "bob"

        assert m._actor_of(self._make_request(user=_User())) == "bob"

    def test_actor_of_prefers_current_user_over_stale_user(self):
        """两者并存时以中间件写入的 current_user 为准。"""
        import app.routers.ai_config as m

        class _User:
            username = "stale"

        req = self._make_request(user=_User(), current_user={"username": "fresh"})
        assert m._actor_of(req) == "fresh"

    def test_actor_of_returns_unknown_without_request(self):
        """无 request（直接调用函数/定时任务）不得抛异常。"""
        import app.routers.ai_config as m

        assert m._actor_of(None) == "unknown"

    def test_actor_of_returns_unknown_when_no_user_attrs(self):
        """request 存在但未注入任何用户信息 → 落 'unknown'，不留空主体。"""
        import app.routers.ai_config as m

        assert m._actor_of(self._make_request()) == "unknown"
        assert m._actor_of(self._make_request(current_user={})) == "unknown"

    def test_actor_of_ignores_masked_or_empty_username(self):
        """current_user 里 username 为空 → 仍落 'unknown'，不写入空主体。"""
        import app.routers.ai_config as m

        req = self._make_request(current_user={"id": 1, "username": ""})
        assert m._actor_of(req) == "unknown"

    def test_save_config_records_real_actor(self, restore_text_state):
        """端到端：带 current_user 的请求保存主配置后，审计行 actor 为真实用户名。

        乐观锁口径（P0-CONTRACT）：HTTP 层写入必须携带 If-Match etag。
        """
        import app.routers.ai_config as m
        from app.services.ai.provider_factory import get_ai_config_version

        req = self._make_request(current_user={"id": 3, "username": "charlie"})
        req.headers = {"if-match": str(get_ai_config_version())}
        asyncio.run(m.save_ai_config(m.AIConfigData(
            provider_name="AuditMe", provider="deepseek",
            api_key="sk-audit-1234", model="m",
            base_url="https://api.deepseek.com/v1",
        ), request=req))

        res = asyncio.run(m.get_config_audit(limit=10))
        entry = next(e for e in res["items"] if e["action"] == "save_config")
        assert entry["actor"] == "charlie"
        assert entry["target"] == "AuditMe"

    def test_save_config_without_request_still_auditable(self, restore_text_state):
        """无 request 时审计行 actor='unknown'（不留空），且保存本身成功。"""
        import app.routers.ai_config as m

        res = asyncio.run(m.save_ai_config(m.AIConfigData(
            provider_name="AuditAnon", provider="deepseek",
            api_key="sk-anon-1234", model="m",
            base_url="https://api.deepseek.com/v1",
        )))
        assert json.loads(res.body.decode("utf-8"))["status"] == "ok"
        items = asyncio.run(m.get_config_audit(limit=10))["items"]
        entry = next(e for e in items if e["action"] == "save_config")
        assert entry["actor"] == "unknown"
# ---------------------------------------------------------------------------
# G-3：get_ai_config 回落主表为空时同步 timeout / temperature
# ---------------------------------------------------------------------------
class TestGetAiConfigFallbackCarriesParams:
    def test_fallback_carries_timeout_and_temperature(self, restore_text_state, monkeypatch):
        """主配置 api_key 为空 + 激活厂商单独调了 timeout/temperature → 回落必须带上。"""
        import app.services.ai.provider_factory as pf

        monkeypatch.setattr(pf, "_load_env_config", lambda: {})
        monkeypatch.setattr(
            pf, "_AI_CONFIG_DEFAULTS",
            {**pf._AI_CONFIG_DEFAULTS, "timeout": 120, "temperature": 0.7},
        )

        _set_ai_config(api_key="", timeout=120, temperature=0.7)
        _set_text_provider(
            "zhipu", "ENC:irrelevant", timeout=333, temperature=1.23,
            base_url="https://open.bigmodel.cn/api/paas/v4", is_active=1,
        )

        pf._clear_ai_config_cache()
        config = pf.get_ai_config()

        assert config["timeout"] == 333, f"回落未同步 timeout: {config['timeout']}"
        assert abs(float(config["temperature"]) - 1.23) < 1e-9, \
            f"回落未同步 temperature: {config['temperature']}"
        # 原有字段同步语义不变
        assert config["provider_name"] == "zhipu"
        assert config["base_url"] == "https://open.bigmodel.cn/api/paas/v4"

    def test_fallback_does_not_touch_primary_when_key_present(
        self, restore_text_state, monkeypatch
    ):
        """主配置有 api_key 时走主配置，不被厂商配置覆盖（避免回归优先级语义）。"""
        import app.services.ai.provider_factory as pf

        monkeypatch.setattr(pf, "_load_env_config", lambda: {})

        _set_ai_config(api_key="sk-primary-master", timeout=444, temperature=0.11)
        _set_text_provider("zhipu", "sk-zhipu", timeout=333, temperature=1.23, is_active=1)

        pf._clear_ai_config_cache()
        config = pf.get_ai_config()

        assert config["api_key"] == "sk-primary-master"
        assert config["timeout"] == 444
        assert abs(float(config["temperature"]) - 0.11) < 1e-9


# ---------------------------------------------------------------------------
# G-4：降级链 Profile 层携带生成参数
# ---------------------------------------------------------------------------
class TestPriorityChainProfileCarriesParams:
    def test_profile_layer_carries_generation_params(self, restore_text_state, monkeypatch):
        """降级到 Profile 层时不得丢掉 max_tokens/temperature/concurrency/timeout/rate_limit。"""
        import app.services.ai.profile_service as ps
        import app.services.ai.provider_factory as pf

        monkeypatch.setattr(pf, "_load_env_config", lambda: {})

        _set_ai_config(
            provider_name="Primary", provider="deepseek", api_key="sk-primary-key",
            base_url="https://api.deepseek.com/v1", model="deepseek-chat",
        )
        created = ps.create_profile({
            "name": "outline_profile",
            "provider_name": "Outline",
            "provider": "deepseek",
            "api_key": "sk-profile-key",
            "model": "deepseek-chat",
            "base_url": "https://profile.example.com/v1",  # 唯一 URL，确保进入降级链
            "max_tokens": 8192,
            "temperature": 0.31,
            "concurrency": 3,
            "timeout": 333,
            "rate_limit_per_min": 7,
            "is_default": True,
        })
        assert created, "Profile 创建失败"

        pf._clear_ai_config_cache()
        chain = pf.get_provider_priority_order()

        entry = next(e for e in chain if e["base_url"] == "https://profile.example.com/v1")
        assert entry["provider"] == "deepseek"
        # 修复前这些字段为 0 / 0.7（缺失），Provider 构造器会全部回落内置默认
        assert entry["max_tokens"] == 8192, f"max_tokens 丢失: {entry.get('max_tokens')}"
        assert abs(float(entry["temperature"]) - 0.31) < 1e-9, \
            f"temperature 丢失: {entry.get('temperature')}"
        assert entry["concurrency"] == 3, f"concurrency 丢失: {entry.get('concurrency')}"
        assert entry["timeout"] == 333, f"timeout 丢失: {entry.get('timeout')}"
        assert entry["rate_limit_per_min"] == 7, \
            f"rate_limit_per_min 丢失: {entry.get('rate_limit_per_min')}"

    def test_profile_layer_still_has_identity_fields(self, restore_text_state, monkeypatch):
        """修复后原有身份字段语义不变（真实密钥、is_builtin_default=False）。"""
        import app.services.ai.profile_service as ps
        import app.services.ai.provider_factory as pf

        monkeypatch.setattr(pf, "_load_env_config", lambda: {})
        _set_ai_config(
            provider_name="Primary", provider="deepseek", api_key="sk-primary-key",
            base_url="https://api.deepseek.com/v1", model="deepseek-chat",
        )
        ps.create_profile({
            "name": "ident_profile",
            "api_key": "sk-ident-123",
            "base_url": "https://ident.example.com/v1",
            "is_default": True,
        })
        pf._clear_ai_config_cache()
        chain = pf.get_provider_priority_order()
        entry = next(e for e in chain if e["base_url"] == "https://ident.example.com/v1")
        assert entry["api_key"] == "sk-ident-123"
        assert entry["is_builtin_default"] is False
        # 未显式设置的参数保持「回落主配置」的 0 语义
        assert entry["concurrency"] == 4  # _clamp_profile_params 回填默认值

    def test_profile_layer_absent_when_no_default_profile(
        self, restore_text_state, monkeypatch
    ):
        """无默认 Profile 时降级链不得出现空条目。"""
        import app.services.ai.provider_factory as pf

        monkeypatch.setattr(pf, "_load_env_config", lambda: {})
        _set_ai_config(api_key="sk-primary-key", base_url="https://api.deepseek.com/v1")
        pf._clear_ai_config_cache()
        chain = pf.get_provider_priority_order()
        assert not any(e["base_url"] == "https://profile.example.com/v1" for e in chain)


# ---------------------------------------------------------------------------
# G-5：list_image_provider_configs 同步读移出事件循环
# ---------------------------------------------------------------------------
class TestImageProviderConfigsReadOffEventLoop:
    def test_db_read_dispatched_to_thread(self):
        """源码断言：DB 读包在 to_thread 中（对齐 /ai/provider-configs 口径）。"""
        import app.routers.ai_config as m

        src = inspect.getsource(m.list_image_provider_configs)
        assert "asyncio.to_thread" in src, "DB 读未派发 to_thread，仍阻塞事件循环"
        assert "def _read_image_provider_rows" in src

    def test_source_shape_matches_text_config_list(self):
        """两个列表接口同为『内部函数持锁 + to_thread 派发』形态，不得再出现分叉。"""
        import app.routers.ai_config as m

        assert "asyncio.to_thread" in inspect.getsource(m.list_text_provider_configs)
        assert "asyncio.to_thread" in inspect.getsource(m.list_image_provider_configs)

    def test_source_shape_matches_single_image_get(self):
        """单厂商读接口同样应移出事件循环（同口径核查）。"""
        import app.routers.ai_config as m

        src = inspect.getsource(m.get_image_provider_config)
        assert "asyncio.to_thread" in src, "单厂商读取仍在事件循环内抢全局写锁"

    def test_list_still_returns_masked_configs(self, restore_text_state):
        """重构后接口行为不变：密钥脱敏、未配置厂商补默认行。"""
        import app.routers.ai_config as m

        res = asyncio.run(m.list_image_provider_configs())
        configs = res["configs"]
        assert isinstance(configs, list) and configs
        for item in configs:
            key = item.get("api_key", "")
            assert key == "" or key == m.API_KEY_MASK, f"密钥泄露: {key[:20]!r}"
        names = {c["provider_name"] for c in configs}
        assert "agnes" in names
        # 字段口径与修复前一致
        assert {"provider_name", "api_key", "base_url", "model", "enabled",
                "is_active", "updated_at", "price_per_image"} <= set(configs[0].keys())


# ---------------------------------------------------------------------------
# BUG-1：save_ai_config 返回契约（遗留测试漂移）
# ---------------------------------------------------------------------------
class TestSaveAiConfigContract:
    def test_returns_jsonresponse_with_etag(self, restore_text_state):
        """P0-CONTRACT 2026-10-14：返回 JSONResponse，携带 etag/version 与 status 体。"""
        from starlette.responses import JSONResponse

        import app.routers.ai_config as m

        res = asyncio.run(m.save_ai_config(m.AIConfigData(
            provider_name="ContractCheck", provider="deepseek",
            api_key="sk-contract", model="m",
            base_url="https://api.deepseek.com/v1",
        )))
        assert isinstance(res, JSONResponse)
        body = json.loads(res.body.decode("utf-8"))
        assert body["status"] == "ok"
        assert int(body["version"]) > 0
        # 响应头携带版本号，供客户端下一轮 If-Match 乐观锁使用
        assert int(res.headers["etag"]) == body["version"]
        assert int(res.headers["version"]) == body["version"]
        # 配置是易失态资源，不得让 CDN/浏览器缓存
        assert "max-age=" not in res.headers.get("cache-control", "")

    def test_audit_failure_still_does_not_block_save(self, restore_text_state, monkeypatch):
        """L-5 容错（遗留用例按新契约重述）：审计抛错不得阻断保存主流程。"""
        import app.routers.ai_config as m

        def _boom(*a, **k):
            raise RuntimeError("audit down")

        monkeypatch.setattr(m, "record_config_audit", _boom)
        res = asyncio.run(m.save_ai_config(m.AIConfigData(
            provider_name="L5bContract", provider="deepseek",
            api_key="sk-l5b", model="m",
            base_url="https://api.deepseek.com/v1",
        )))
        assert json.loads(res.body.decode("utf-8"))["status"] == "ok"

    def test_http_layer_requires_if_match(self, restore_text_state):
        """回归护栏：HTTP 层写入缺 If-Match → 400（防他人绕过乐观锁）。"""
        from fastapi import HTTPException

        import app.routers.ai_config as m

        req = TestActorResolution._make_request(
            current_user={"username": "dave"}
        )
        with pytest.raises(HTTPException) as ei:
            asyncio.run(m.save_ai_config(m.AIConfigData(
                provider_name="NoMatch", provider="deepseek",
                api_key="sk-nomatch", model="m",
                base_url="https://api.deepseek.com/v1",
            ), request=req))
        assert ei.value.status_code == 400

