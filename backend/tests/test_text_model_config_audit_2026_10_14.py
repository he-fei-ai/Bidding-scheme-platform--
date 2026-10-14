"""文本模型配置模块 —— 2026-10-14 深度审计修复回归测试

本文件钉住本轮审计定位并修复的缺陷，每条用例都对应一个可复现的失败路径：

  P0-SEC-1  同步降级链把**主配置密钥**发往无自有密钥的第三方厂商 URL（凭据外泄 + 5s×N 等待）
  P0-CONTRACT  /ai/config-audit 返回 {items,total}，前端封装按数组消费（审计页崩溃）
  P1-META   find_model_metadata 无法匹配中文展示名 → /ai/model-info 恒回落默认值
  P1-ID      厂商缺稳定机器 id → 前端元数据表 11 项仅 2 项命中，base_url/model 回填静默失效
  P1-CFG     timeout 是「死列」：表里有、provider 读，但请求模型/SQL/UI 全缺
  P1-DOC     document_profile 被 sections.py 消费，却无 UI 且每次保存被清空
  P1-CFG2    降级链丢弃 rate_limit_per_min / concurrency（与 create_provider 行为不一致）
  P1-PERF    异步降级链无探活缓存；读端点在事件循环内做同步 SQLite I/O
"""

from __future__ import annotations

import asyncio
import inspect
from unittest import mock

import pytest

from app.db import get_db_with_retry
from app.routers import ai_config as ac
from app.services.ai import provider_catalog as pc
from app.services.ai import provider_factory as pf
import app.services.ai.profile_service as profile_service


# ══════════════════════════════════════════════════════════════════════
# P0-SEC-1：同步降级链的凭据外泄
# ══════════════════════════════════════════════════════════════════════
_PRIMARY_CFG = {
    "provider_name": "agnes",
    "provider": "openai_compatible",
    "api_key": "PRIMARY-SECRET-KEY",
    "model": "primary-model",
    "base_url": "https://api.agnes-ai.cn/v1",
    "timeout": 120,
}


class _Resp401:
    status_code = 401
    headers: dict = {}


class _SyncClientProbe:
    """记录同步探活实际发出的 URL + Authorization 头。"""

    def __init__(self, sent: list):
        self.sent = sent

    def __call__(self, *a, **k):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def post(self, url, headers=None, json=None):
        self.sent.append((url, (headers or {}).get("Authorization", "")))
        return _Resp401()


class _AsyncResp401:
    status_code = 401
    headers: dict = {}


class _AsyncClientProbe:
    def __init__(self, sent: list):
        self.sent = sent

    def __call__(self, *a, **k):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, headers=None, json=None):
        self.sent.append((url, (headers or {}).get("Authorization", "")))
        return _AsyncResp401()


def _neutral_deps():
    """把降级链的其它层清空，让断言只聚焦「主配置 + 内置兜底」。"""
    return (
        mock.patch.object(pf, "get_ai_config", lambda: dict(_PRIMARY_CFG)),
        mock.patch.object(pf, "get_configured_providers", lambda: []),
        mock.patch.object(pf, "get_history_providers", lambda: []),
        mock.patch.object(profile_service, "get_default_profile", lambda include_key=False: None),
    )


class TestP0SyncFallbackKeyLeak:
    def test_sync_chain_never_sends_primary_key_to_third_party_urls(self):
        """同步降级链不得把主配置密钥发往第三方厂商 URL。

        修复前：`key = entry.get("api_key") or api_key` 会让无自有密钥的兜底条目
        （DeepSeek / SiliconFlow / 通义 / 智谱 / OpenAI）继承主配置密钥，
        实测一次调用会向 5 个无关端点各发一次 `Authorization: Bearer <主密钥>`。
        """
        import httpx

        sent: list = []
        pf._clear_health_cache()
        p1, p2, p3, p4 = _neutral_deps()
        with mock.patch.object(httpx, "Client", _SyncClientProbe(sent)), p1, p2, p3, p4:
            pf.create_provider_with_fallback(dict(_PRIMARY_CFG))

        leaked = [u for u, auth in sent if auth == "Bearer PRIMARY-SECRET-KEY"]
        # 主配置自身必须被探活（否则等于完全跳过健康检查）
        assert any("api.agnes-ai.cn" in u for u in leaked), "主配置自身应被探活"
        # 除主配置 URL 外，不得有任何第三方收到该密钥
        for url in leaked:
            assert "agnes-ai.cn" in url, f"主配置密钥被发往第三方 URL: {url}"
        # 第三方 URL 也不应被无谓探活（必然 401 + 每次 5s 超时）
        third_party = [u for u, _ in sent if "agnes-ai.cn" not in u]
        assert third_party == [], f"仍对无密钥的第三方厂商发起探活: {third_party}"

    def test_sync_and_async_chains_agree_on_key_selection(self):
        """两条降级链的「用哪把密钥」口径必须一致（TD-3 声称已对齐，实则未对齐）。"""
        import httpx

        sync_sent: list = []
        pf._clear_health_cache()
        p1, p2, p3, p4 = _neutral_deps()
        with mock.patch.object(httpx, "Client", _SyncClientProbe(sync_sent)), p1, p2, p3, p4:
            pf.create_provider_with_fallback(dict(_PRIMARY_CFG))

        async_sent: list = []
        pf._clear_health_cache()
        q1, q2, q3, q4 = _neutral_deps()
        with mock.patch.object(httpx, "AsyncClient", _AsyncClientProbe(async_sent)), q1, q2, q3, q4:
            asyncio.run(pf.get_working_provider(dict(_PRIMARY_CFG)))
        pf._clear_health_cache()

        assert sync_sent, "同步链未发出任何探活"
        assert {u for u, _ in sync_sent} == {u for u, _ in async_sent}


# ══════════════════════════════════════════════════════════════════════
# P1-CFG2：降级链不得丢弃 rate_limit_per_min / concurrency
# ══════════════════════════════════════════════════════════════════════
class TestFallbackForwardsThrottleConfig:
    """降级链此前把 rate_limit_per_min / concurrency / 条目级参数全部丢弃，
    与 create_provider 的语义不一致 —— 同一份配置在不同调用路径上行为不同。"""

    def test_sync_fallback_forwards_rate_limit_and_concurrency(self):
        cfg = {**_PRIMARY_CFG, "rate_limit_per_min": 42, "concurrency": 3}
        with mock.patch.object(pf, "_health_cache_get", lambda k: True):
            prov, _ = pf.create_provider_with_fallback(cfg)
        assert prov._rate_limit_per_min == 42
        assert prov._concurrency == 3

    def test_async_fallback_forwards_rate_limit_and_concurrency(self):
        cfg = {**_PRIMARY_CFG, "rate_limit_per_min": 42, "concurrency": 3}
        q1, q2, q3, q4 = _neutral_deps()
        with mock.patch.object(pf, "_health_cache_get", lambda k: True), q1, q2, q3, q4:
            prov, _ = asyncio.run(pf.get_working_provider(cfg))
        assert prov._rate_limit_per_min == 42
        assert prov._concurrency == 3

    def test_entry_level_values_take_precedence(self):
        """厂商条目自带并发/超时时优先于主配置（厂商隔离语义）。"""
        chain = [{
            "name": "x", "provider": "openai_compatible",
            "base_url": "https://x.example/v1", "api_key": "k-x",
            "model": "m-x", "max_tokens": 2048, "temperature": 0.1,
            "concurrency": 2, "timeout": 45, "is_primary": True,
        }]
        with mock.patch.object(pf, "get_provider_priority_order", lambda: chain), \
             mock.patch.object(pf, "_health_cache_get", lambda k: True):
            prov, _ = pf.create_provider_with_fallback({**_PRIMARY_CFG, "concurrency": 9, "timeout": 300})
        assert prov._concurrency == 2
        assert prov.timeout == 45
        assert prov.max_tokens == 2048
        assert prov.temperature == 0.1

    def test_create_provider_semantics_match_fallback(self):
        """create_provider 与降级链对同一份配置的 max_tokens/concurrency/timeout 语义一致。"""
        cfg = {**_PRIMARY_CFG, "max_tokens": 5555, "concurrency": 3, "timeout": 77}
        direct = pf.create_provider(cfg)
        chain = [{
            "name": "agnes", "provider": "openai_compatible",
            "base_url": cfg["base_url"], "api_key": cfg["api_key"], "model": cfg["model"],
            "is_primary": True, "is_builtin_default": True,
        }]
        with mock.patch.object(pf, "get_provider_priority_order", lambda: chain), \
             mock.patch.object(pf, "_health_cache_get", lambda k: True):
            via_fb, _ = pf.create_provider_with_fallback(cfg)
        assert direct.max_tokens == via_fb.max_tokens == 5555
        assert direct._concurrency == via_fb._concurrency == 3
        assert direct.timeout == via_fb.timeout == 77
        assert direct._rate_limit_per_min == via_fb._rate_limit_per_min == 0


# ══════════════════════════════════════════════════════════════════════
# P1-META / P1-ID：厂商稳定 id 与模型元数据解析
# ══════════════════════════════════════════════════════════════════════
class TestProviderIdAndModelMetadata:
    def test_every_known_provider_has_unique_stable_id(self):
        ids = [p.get("id") for p in pc.KNOWN_PROVIDERS]
        assert all(ids), "存在缺少稳定 id 的厂商条目"
        assert len(set(ids)) == len(ids), f"厂商 id 存在重复: {ids}"

    @pytest.mark.parametrize(
        "provider_name, model, expect_hit",
        [
            ("智谱AI (GLM)", "glm-4-flash", True),
            ("腾讯混元", "hunyuan-turbo", True),
            ("阿里云百炼（通义千问）", "qwen-turbo", True),
            ("字节跳动豆包", "Doubao-Seed-2.0-Mini", True),
            ("百度千帆", "ernie-4.5-turbo-128k", True),
            ("Moonshot (Kimi)", "kimi-k2", True),
            ("DeepSeek", "deepseek-chat", True),
        ],
    )
    def test_model_metadata_resolves_chinese_display_names(self, provider_name, model, expect_hit):
        """P1-META：中文展示名此前一律匹配失败 → /ai/model-info 恒返回默认值。"""
        assert bool(pc.find_model_metadata(provider_name, model)) is expect_hit

    @pytest.mark.parametrize("provider_id", ["zhipu", "hunyuan", "dashscope", "volcengine", "deepseek"])
    def test_model_metadata_resolves_by_stable_id(self, provider_id):
        table = pc.resolve_provider_meta_key(provider_id)
        assert table in pc.PROVIDER_MODEL_METADATA

    def test_empty_provider_does_not_match_first_table(self):
        """`"" in "deepseek"` 恒真 → 空厂商名会被错配到第一张表。"""
        assert pc.resolve_provider_meta_key("") is None
        assert pc.resolve_provider_meta_key("   ") is None
        assert pc.find_model_metadata("", "gpt-4o") is None

    def test_provider_id_roundtrip(self):
        for p in pc.KNOWN_PROVIDERS:
            assert pc.provider_id_of(p["name"]) == p["id"]
            assert pc.find_known_provider_by_id(p["id"].upper()) is not None

    @pytest.mark.asyncio
    async def test_model_info_endpoint_uses_provider_id(self):
        """前端传 provider_id 时，即使 provider 名是中文也应命中 builtin。"""
        r = await ac.fetch_model_info(ac.ModelInfoRequest(
            provider_id="zhipu",
            provider="智谱AI (GLM)",
            model="glm-4-flash",
            base_url="https://open.bigmodel.cn/api/paas/v4",
        ))
        assert r["source"] == "builtin"
        assert r["max_tokens"] == 8192

    def test_public_projection_exposes_id(self):
        for item in pc.known_providers_public():
            assert "api_key" not in item
            assert item.get("id")


# ══════════════════════════════════════════════════════════════════════
# P1-CFG：timeout 从「死列」变为可配置项
# ══════════════════════════════════════════════════════════════════════
class TestTimeoutIsConfigurable:
    @pytest.mark.asyncio
    async def test_save_ai_config_persists_timeout(self):
        await ac.save_ai_config(ac.AIConfigData(
            provider_name="t-timeout", base_url="https://api.example.com/v1",
            model="m", api_key="sk-plain-timeout", timeout=300,
        ))
        with get_db_with_retry(readonly=True) as conn:
            row = conn.execute("SELECT timeout FROM ai_config WHERE id = 1").fetchone()
        assert row["timeout"] == 300

    @pytest.mark.asyncio
    @pytest.mark.parametrize("raw, expect", [(1, 5), (99999, 600), (0, 120), (60, 60)])
    async def test_timeout_is_clamped(self, raw, expect):
        await ac.save_ai_config(ac.AIConfigData(
            provider_name="t-clamp", base_url="https://api.example.com/v1",
            model="m", api_key="sk-clamp", timeout=raw,
        ))
        with get_db_with_retry(readonly=True) as conn:
            row = conn.execute("SELECT timeout FROM ai_config WHERE id = 1").fetchone()
        assert row["timeout"] == expect

    def test_ai_config_data_declares_timeout(self):
        """回归钉死：请求模型必须含 timeout（此前字段缺失 → SQL 不写 → 死列）。"""
        assert "timeout" in ac.AIConfigData.model_fields

    @pytest.mark.asyncio
    async def test_provider_config_roundtrip_timeout(self):
        await ac.save_text_provider_config("T-Timeout-Vendor", ac.TextProviderConfigData(
            provider_name="T-Timeout-Vendor", base_url="https://v.example/v1",
            model="m", api_key="sk-vendor-timeout", timeout=222,
            activate_after_save=False,
        ))
        got = await ac.get_text_provider_config("T-Timeout-Vendor")
        assert got["timeout"] == 222

        with get_db_with_retry(readonly=True) as conn:
            row = conn.execute(
                "SELECT timeout FROM text_provider_configs WHERE provider_name = ?",
                ["T-Timeout-Vendor"],
            ).fetchone()
        assert row["timeout"] == 222

    @pytest.mark.asyncio
    async def test_activate_syncs_timeout_to_primary_config(self):
        """激活厂商时其超时必须同步到主配置（否则该厂商的调参不生效）。"""
        await ac.save_text_provider_config("T-Act-Timeout", ac.TextProviderConfigData(
            provider_name="T-Act-Timeout", base_url="https://act.example/v1",
            model="m-activate", api_key="sk-act-timeout", timeout=180,
            activate_after_save=False,
        ))
        await ac.activate_text_provider_config("T-Act-Timeout")
        with get_db_with_retry(readonly=True) as conn:
            row = conn.execute("SELECT timeout, model FROM ai_config WHERE id = 1").fetchone()
        assert row["timeout"] == 180
        assert row["model"] == "m-activate"

    def test_text_provider_configs_table_has_timeout_column(self):
        with get_db_with_retry(readonly=True) as conn:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(text_provider_configs)")}
        assert "timeout" in cols


# ══════════════════════════════════════════════════════════════════════
# P1-DOC：document_profile 不得被静默清空
# ══════════════════════════════════════════════════════════════════════
class TestDocumentProfilePreserved:
    @pytest.mark.asyncio
    async def test_partial_save_keeps_document_profile_on_primary_config(self):
        await ac.save_ai_config(ac.AIConfigData(
            provider_name="dp", base_url="https://dp.example/v1", model="m",
            api_key="sk-dp", document_profile="市政公用工程总承包",
        ))
        # 只改温度，不带 document_profile（None）
        await ac.save_ai_config(ac.AIConfigData(
            provider_name="dp", base_url="https://dp.example/v1", model="m",
            api_key="***已配置***", temperature=0.3,
        ))
        with get_db_with_retry(readonly=True) as conn:
            row = conn.execute("SELECT document_profile, temperature FROM ai_config WHERE id = 1").fetchone()
        assert row["document_profile"] == "市政公用工程总承包"
        assert row["temperature"] == 0.3

    @pytest.mark.asyncio
    async def test_explicit_empty_string_clears_document_profile(self):
        await ac.save_ai_config(ac.AIConfigData(
            provider_name="dp2", base_url="https://dp2.example/v1", model="m",
            api_key="sk-dp2", document_profile="旧画像",
        ))
        await ac.save_ai_config(ac.AIConfigData(
            provider_name="dp2", base_url="https://dp2.example/v1", model="m",
            api_key="***已配置***", document_profile="",
        ))
        with get_db_with_retry(readonly=True) as conn:
            row = conn.execute("SELECT document_profile FROM ai_config WHERE id = 1").fetchone()
        assert row["document_profile"] == ""

    @pytest.mark.asyncio
    async def test_partial_vendor_save_keeps_document_profile(self):
        await ac.save_text_provider_config("T-Doc", ac.TextProviderConfigData(
            provider_name="T-Doc", base_url="https://d.example/v1", model="m",
            api_key="sk-doc", document_profile="智慧城市信息化",
            activate_after_save=False,
        ))
        await ac.save_text_provider_config("T-Doc", ac.TextProviderConfigData(
            provider_name="T-Doc", base_url="https://d.example/v1", model="m2",
            api_key="***已配置***", temperature=0.9,
            activate_after_save=False,
        ))
        got = await ac.get_text_provider_config("T-Doc")
        assert got["document_profile"] == "智慧城市信息化"
        assert got["model"] == "m2"

    @pytest.mark.asyncio
    async def test_activate_carries_document_profile_to_primary_config(self):
        await ac.save_text_provider_config("T-Doc-Act", ac.TextProviderConfigData(
            provider_name="T-Doc-Act", base_url="https://da.example/v1", model="m",
            api_key="sk-doc-act", document_profile="轨道交通",
            activate_after_save=False,
        ))
        await ac.activate_text_provider_config("T-Doc-Act")
        pf._clear_ai_config_cache()
        assert pf.get_ai_config().get("document_profile") == "轨道交通"


# ══════════════════════════════════════════════════════════════════════
# P0-CONTRACT + P1-ID：读接口响应形状
# ══════════════════════════════════════════════════════════════════════
class TestReadEndpointShapes:
    @pytest.mark.asyncio
    async def test_config_audit_returns_items_object(self):
        """契约：{items, total}（前端封装按 items 归一为数组）。"""
        res = await ac.get_config_audit(limit=10)
        assert isinstance(res, dict)
        assert isinstance(res["items"], list)
        assert isinstance(res["total"], int)

    @pytest.mark.asyncio
    async def test_provider_config_list_exposes_provider_id(self):
        res = await ac.list_text_provider_configs()
        by_name = {c["provider_name"]: c for c in res["configs"]}
        zhipu = by_name["智谱AI (GLM)"]
        assert zhipu["provider_id"] == "zhipu"
        assert "api_key_set" in zhipu
        assert "timeout" in zhipu
        # 未配置厂商也应带 id（前端要靠它查本地兜底表）
        for name in ("阿里云百炼（通义千问）", "腾讯混元", "字节跳动豆包"):
            assert by_name[name]["provider_id"] != ""

    @pytest.mark.asyncio
    async def test_provider_config_list_uses_catalog_default_model(self):
        """默认模型取自厂商目录正本（provider_catalog.DEFAULT_TEXT_PROVIDER）。

        注意比对对象用**目录正本**而非 `app.config.AGNES_MODEL`：全量套件里存在
        reload config 模块的用例，重载后两者会脱钩，而运行时真正生效的永远是
        正本（provider_catalog 在 import 时绑定）。
        """
        res = await ac.list_text_provider_configs()
        agnes = next(c for c in res["configs"] if c["provider_name"] == "agnes")
        assert agnes["model"] == pc.DEFAULT_TEXT_PROVIDER["model"]
        assert agnes["provider_id"] == "agnes"
        # 目录正本与 config 默认值同源（除非 env 覆盖）
        assert pc.DEFAULT_TEXT_PROVIDER["base_url"]

    @pytest.mark.asyncio
    async def test_provider_view_never_exposes_raw_key(self):
        await ac.save_text_provider_config("T-Mask", ac.TextProviderConfigData(
            provider_name="T-Mask", base_url="https://m.example/v1", model="m",
            api_key="sk-super-secret-value", activate_after_save=False,
        ))
        res = await ac.list_text_provider_configs()
        entry = next(c for c in res["configs"] if c["provider_name"] == "T-Mask")
        assert entry["api_key"] == ac.API_KEY_MASK
        assert entry["api_key_set"] is True
        assert "sk-super-secret-value" not in str(res)


# ══════════════════════════════════════════════════════════════════════
# P1-PERF：事件循环内不得直调阻塞 SQLite
# ══════════════════════════════════════════════════════════════════════
class TestReadEndpointsOffloadBlockingIO:
    @pytest.mark.parametrize(
        "func",
        [
            ac.get_config_audit,
            ac.list_text_provider_configs,
            ac.get_text_provider_config,
            ac.get_provider_history,
            ac.check_ai_health,
            ac.get_ai_config,
        ],
    )
    def test_no_direct_db_entry_in_async_body(self, func):
        """合规写法：DB 访问包在嵌套同步闭包里，再 await asyncio.to_thread(闭包)。

        实现说明：直接 `ast.parse(inspect.getsource(fn))` 不可靠 —— 本仓库的
        robust_getsource 会把「下一个函数的装饰器」一并带进片段，导致整串解析失败。
        故改为读**整个模块文件**再按函数名定位节点（与 test_async_blocking_gate 同法）。
        """
        import ast
        from pathlib import Path

        mod_path = Path(inspect.getfile(func))
        tree = ast.parse(mod_path.read_text(encoding="utf-8"))
        fn = next(
            (
                n for n in ast.walk(tree)
                if isinstance(n, ast.AsyncFunctionDef) and n.name == func.__name__
            ),
            None,
        )
        assert fn is not None, f"未在 {mod_path} 中定位到 async def {func.__name__}"

        def _own_calls(node):
            out = []
            nested = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)
            for stmt in node.body:
                if isinstance(stmt, nested):
                    continue
                stack = [stmt]
                while stack:
                    cur = stack.pop()
                    for child in ast.iter_child_nodes(cur):
                        if isinstance(child, nested):
                            continue
                        if isinstance(child, ast.Call):
                            out.append(child)
                        stack.append(child)
            return out

        for call in _own_calls(fn):
            f = call.func
            name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
            assert name not in ("get_db", "get_db_with_retry"), (
                f"{func.__name__} 在事件循环内直调 {name}"
            )


# ══════════════════════════════════════════════════════════════════════
# 边界与异常输入
# ══════════════════════════════════════════════════════════════════════
class TestBoundaryAndMalformedInput:
    @pytest.mark.asyncio
    async def test_unicode_and_special_chars_in_provider_name(self):
        """中文/括号/斜杠厂商名不得导致 SQL 注入或 500。"""
        name = "厂商 A/B（C）'测试\""
        res = await ac.save_text_provider_config(name, ac.TextProviderConfigData(
            provider_name=name, base_url="https://special.example/v1", model="m",
            api_key="sk-special", activate_after_save=False,
        ))
        assert res["provider_name"] == name
        got = await ac.get_text_provider_config(name)
        assert got["provider_name"] == name

    @pytest.mark.asyncio
    async def test_provider_name_mismatch_rejected(self):
        with pytest.raises(Exception) as ei:
            await ac.save_text_provider_config("A", ac.TextProviderConfigData(
                provider_name="B", base_url="https://x/v1", model="m", api_key="k",
                activate_after_save=False,
            ))
        assert "provider_name 不匹配" in str(ei.value)

    @pytest.mark.asyncio
    async def test_activate_unknown_provider_404(self):
        with pytest.raises(Exception) as ei:
            await ac.activate_text_provider_config("不存在的厂商-XYZ")
        assert getattr(ei.value, "status_code", None) == 404

    @pytest.mark.asyncio
    async def test_empty_document_profile_and_zero_temperature_roundtrip(self):
        """temperature=0 是合法值（确定性输出），不得被 `or 0.7` 吞掉。"""
        await ac.save_text_provider_config("T-Edge", ac.TextProviderConfigData(
            provider_name="T-Edge", base_url="https://e.example/v1", model="m",
            api_key="sk-edge", temperature=0.0, document_profile="",
            activate_after_save=False,
        ))
        got = await ac.get_text_provider_config("T-Edge")
        assert got["temperature"] == 0.0
        assert got["document_profile"] == ""

    @pytest.mark.asyncio
    async def test_unknown_provider_get_returns_placeholder(self):
        got = await ac.get_text_provider_config("完全未收录的厂商")
        assert got["api_key"] == ""
        assert got["api_key_set"] is False
        assert got["provider_id"] == ""
        assert got["timeout"] == 120

    def test_resolve_meta_key_is_stable_for_all_known_providers(self):
        """所有收录厂商按 id / 展示名解析必须不抛异常，且结果要么是合法表键要么 None。"""
        valid = set(pc.PROVIDER_MODEL_METADATA)
        for p in pc.KNOWN_PROVIDERS:
            by_id = pc.resolve_provider_meta_key(p["id"])
            by_name = pc.resolve_provider_meta_key(p["name"])
            assert by_id is None or by_id in valid, f"{p['id']} 解析出非法表键 {by_id}"
            assert by_name is None or by_name in valid, f"{p['name']} 解析出非法表键 {by_name}"
            # 有专属元数据表的厂商，按 id 与按名必须解析到同一张表
            if by_id is not None and by_name is not None:
                assert by_id == by_name, f"{p['name']} 按名/按 id 解析不一致: {by_name} vs {by_id}"
        # 映射表登记的 id 必须都能解析出合法表键
        for pid, key in pc._PROVIDER_ID_TO_META_KEY.items():
            assert pc.resolve_provider_meta_key(pid) == key