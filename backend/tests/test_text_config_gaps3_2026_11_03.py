"""文本模型配置模块缺口三连测（2026-11-03 第三轮）。

覆盖项（编号与代码内 2026-11-03 注释对应）：
  G-1   _make_provider 消费显式 provider_type（白名单提升：仅 URL 启发式落空时采纳）
  G-3   max_retries 贯通：create_provider / 降级链条目 / 链尾回落主配置
  D1    save_text_provider_config 的 provider_type 保留语义
        （None=保留库内原值；首存回退厂商目录默认；显式值=覆盖）
  D2    activate 时厂商显式 temperature=0 贯通主表（不再被 `or 0.7` 吞掉）
  D4    save_ai_config 的 provider_history 记录解析后的协议类型（非请求原始空串）
  D5    乱码清理：403 文案正常、两文件源码无残留乱码字符
"""
from __future__ import annotations

import re

import pytest


# ---------------------------------------------------------------------------
# fixtures / 辅助（与 gaps2 同模式：备份还原 + 清缓存）
# ---------------------------------------------------------------------------
@pytest.fixture
def restore_state():
    """备份/还原相关表，并清空 provider_factory 缓存。"""
    from app.db import get_db

    tables = (
        "ai_config",
        "text_provider_configs",
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


def _tpc_get(provider_name: str) -> dict:
    from app.db import get_db

    with get_db(readonly=True) as c:
        row = c.execute(
            "SELECT provider_type, temperature, api_key, base_url, model "
            "FROM text_provider_configs WHERE provider_name = ?",
            (provider_name,),
        ).fetchone()
    return dict(row) if row else {}


def _main_config_get() -> dict:
    from app.db import get_db

    with get_db(readonly=True) as c:
        row = c.execute(
            "SELECT provider, temperature, model, base_url FROM ai_config WHERE id = 1"
        ).fetchone()
    return dict(row) if row else {}


# ---------------------------------------------------------------------------
# G-1：_make_provider 显式 provider_type 白名单提升
# ---------------------------------------------------------------------------
class TestMakeProviderExplicitType:
    GATEWAY = "https://llm-gateway.corp.internal/v1"  # 不含任何厂商关键词

    def test_gateway_url_with_explicit_anthropic(self):
        from app.services.ai import provider_factory as pf

        p = pf._make_provider(
            api_key="sk-x", base_url=self.GATEWAY, model="claude-3-5-sonnet",
            max_tokens=1024, temperature=0.7, provider_type="anthropic_compatible",
        )
        assert type(p).__name__ == "AnthropicCompatibleProvider"

    def test_gateway_url_with_explicit_deepseek(self):
        from app.services.ai import provider_factory as pf

        p = pf._make_provider(
            api_key="sk-x", base_url=self.GATEWAY, model="deepseek-chat",
            max_tokens=1024, temperature=0.7, provider_type="deepseek",
        )
        assert type(p).__name__ == "DeepSeekProvider"

    def test_url_heuristic_wins_over_explicit_generic(self):
        """URL 已识别出具体协议（deepseek）时，显式 openai_compatible 不得反转。"""
        from app.services.ai import provider_factory as pf

        p = pf._make_provider(
            api_key="sk-x", base_url="https://api.deepseek.com/v1",
            model="deepseek-chat", max_tokens=1024, temperature=0.7,
            provider_type="openai_compatible",
        )
        assert type(p).__name__ == "DeepSeekProvider"

    def test_no_explicit_type_keeps_legacy_behavior(self):
        from app.services.ai import provider_factory as pf

        p = pf._make_provider(
            api_key="sk-x", base_url=self.GATEWAY, model="gpt-4o",
            max_tokens=1024, temperature=0.7,
        )
        assert type(p).__name__ == "OpenAICompatibleProvider"

    def test_create_provider_consumes_config_provider(self):
        """主路径：config['provider'] 显式类型必须被 create_provider 消费。"""
        from app.services.ai import provider_factory as pf

        p = pf.create_provider({
            "api_key": "sk-x", "base_url": self.GATEWAY,
            "model": "claude-3-5-sonnet", "provider": "anthropic_compatible",
        })
        assert type(p).__name__ == "AnthropicCompatibleProvider"

    def test_create_provider_max_retries_threaded(self):
        """G-3 主路径：config.max_retries 贯通到 provider 实例。"""
        from app.services.ai import provider_factory as pf

        p = pf.create_provider({
            "api_key": "sk-x", "base_url": self.GATEWAY, "model": "gpt-4o",
            "max_retries": 5,
        })
        assert p._max_retries == 5


# ---------------------------------------------------------------------------
# G-1/G-3：同步降级链（条目级 provider_type 与 max_retries 贯通）
# ---------------------------------------------------------------------------
class TestFallbackChainWiring:
    def _run_chain(self, monkeypatch, pf, entries, config, healthy):
        monkeypatch.setattr(pf, "get_provider_priority_order", lambda: entries)
        monkeypatch.setattr(pf, "_quick_health_check", lambda *a, **kw: healthy)
        return pf.create_provider_with_fallback(config)

    def test_entry_provider_type_and_max_retries(self, monkeypatch):
        from app.services.ai import provider_factory as pf

        entry = {
            "name": "网关代理", "base_url": "https://gw.example.com/v1",
            "api_key": "sk-e", "model": "claude-x", "provider": "anthropic_compatible",
            "max_retries": 7,
        }
        cfg = {"api_key": "sk-p", "base_url": "https://gw.example.com/v1",
               "model": "claude-x", "provider": "anthropic_compatible"}
        provider, _src = self._run_chain(monkeypatch, pf, [entry], cfg, healthy=True)
        assert type(provider).__name__ == "AnthropicCompatibleProvider"
        assert provider._max_retries == 7

    def test_chain_all_unhealthy_tail_uses_config(self, monkeypatch):
        """链上全灭 → 链尾回退主配置：max_retries 与显式类型同样贯通。"""
        from app.services.ai import provider_factory as pf

        cfg = {"api_key": "sk-p", "base_url": "https://gw.example.com/v1",
               "model": "deepseek-chat", "provider": "deepseek", "max_retries": 6}
        provider, src = self._run_chain(monkeypatch, pf, [], cfg, healthy=False)
        assert src == ""
        assert type(provider).__name__ == "DeepSeekProvider"
        assert provider._max_retries == 6

    def test_async_chain_source_wired(self):
        """异步链（get_working_provider）接线证据：源码含同口径透传。"""
        import inspect

        from app.services.ai import provider_factory as pf

        src = inspect.getsource(pf.get_working_provider)
        assert 'provider_type=entry.get("provider", "")' in src
        assert 'max_retries=entry.get("max_retries") or primary_config.get("max_retries", 2)' in src


# ---------------------------------------------------------------------------
# D1：provider_type 保留语义（save_text_provider_config）
# ---------------------------------------------------------------------------
class TestProviderTypePreservation:
    @pytest.mark.asyncio
    async def test_first_save_outside_catalog_defaults(self, restore_state):
        from app.routers import ai_config as r

        await r.save_text_provider_config(
            "未知网关厂商", r.TextProviderConfigData(
                provider_name="未知网关厂商", api_key="sk-d1",
                base_url="https://gw.example.com/v1", model="m",
            ),
        )
        assert _tpc_get("未知网关厂商")["provider_type"] == "openai_compatible"

    @pytest.mark.asyncio
    async def test_first_save_in_catalog_uses_catalog_type(self, restore_state):
        """目录内厂商（DeepSeek→deepseek）首存未显式提供类型时用目录默认。"""
        from app.routers import ai_config as r

        await r.save_text_provider_config(
            "DeepSeek", r.TextProviderConfigData(
                provider_name="DeepSeek", api_key="sk-d1b",
                base_url="https://api.deepseek.com/v1", model="deepseek-chat",
            ),
        )
        assert _tpc_get("DeepSeek")["provider_type"] == "deepseek"

    @pytest.mark.asyncio
    async def test_resave_without_type_preserves_existing(self, restore_state):
        """核心回归：显式保存 anthropic 后再「不带 provider_type」保存，
        旧实现会塌缩成 openai_compatible（前端不传 + 请求模型默认值）。"""
        from app.routers import ai_config as r

        await r.save_text_provider_config(
            "企业网关", r.TextProviderConfigData(
                provider_name="企业网关", api_key="sk-d1c",
                base_url="https://gw.example.com/v1", model="claude-x",
                provider_type="anthropic_compatible",
            ),
        )
        assert _tpc_get("企业网关")["provider_type"] == "anthropic_compatible"
        # 第二次保存：provider_type 未提供（None），只改 model
        await r.save_text_provider_config(
            "企业网关", r.TextProviderConfigData(
                provider_name="企业网关", api_key="sk-d1c",
                base_url="https://gw.example.com/v1", model="claude-y",
            ),
        )
        row = _tpc_get("企业网关")
        assert row["provider_type"] == "anthropic_compatible"
        assert row["model"] == "claude-y"

    @pytest.mark.asyncio
    async def test_explicit_type_overrides(self, restore_state):
        from app.routers import ai_config as r

        await r.save_text_provider_config(
            "企业网关2", r.TextProviderConfigData(
                provider_name="企业网关2", api_key="sk-d1d",
                base_url="https://gw.example.com/v1", model="m",
                provider_type="deepseek",
            ),
        )
        assert _tpc_get("企业网关2")["provider_type"] == "deepseek"
        await r.save_text_provider_config(
            "企业网关2", r.TextProviderConfigData(
                provider_name="企业网关2", api_key="sk-d1d",
                base_url="https://gw.example.com/v1", model="m",
                provider_type="openai_compatible",
            ),
        )
        assert _tpc_get("企业网关2")["provider_type"] == "openai_compatible"

    # ---- R3（2026-11-03）：写入口白名单校验与污染值回落 ------------------
    def test_invalid_provider_type_rejected_422(self):
        """显式非法协议类型在请求模型构造期即被拒（FastAPI 映射 422）。"""
        from pydantic import ValidationError

        from app.routers import ai_config as r

        with pytest.raises(ValidationError, match="provider_type 非法"):
            r.TextProviderConfigData(
                provider_name="任意厂商", provider_type="claude_official",
            )
        # 空串同样是显式非法值（保留语义只属于 None=未提供）
        with pytest.raises(ValidationError):
            r.TextProviderConfigData(provider_name="任意厂商", provider_type="")

    @pytest.mark.asyncio
    async def test_polluted_db_type_falls_back_to_catalog(self, restore_state):
        """库内历史污染值（非白名单）在「未提供」保存时视同缺失，回落目录默认。"""
        from app.db import get_db
        from app.routers import ai_config as r

        with get_db() as c:
            c.execute(
                "INSERT INTO text_provider_configs (provider_name, provider_type, "
                "api_key, base_url, model, temperature, document_profile, timeout, is_active) "
                "VALUES ('污染厂商', 'bogus_protocol', 'sk-r3', "
                "'https://api.deepseek.com/v1', 'm', 0.7, '', 120, 0) "
                "ON CONFLICT(provider_name) DO UPDATE SET provider_type='bogus_protocol'"
            )
            c.commit()
        # 「污染厂商」不在目录 → 回落 openai_compatible
        await r.save_text_provider_config(
            "污染厂商", r.TextProviderConfigData(
                provider_name="污染厂商", api_key="sk-r3",
                base_url="https://api.deepseek.com/v1", model="m2",
            ),
        )
        assert _tpc_get("污染厂商")["provider_type"] == "openai_compatible"


# ---------------------------------------------------------------------------
# D2：activate 时 temperature=0 贯通主表
# ---------------------------------------------------------------------------
class TestActivateTemperatureZero:
    @pytest.mark.asyncio
    async def test_activate_preserves_zero_temperature(self, restore_state):
        from app.db import get_db
        from app.routers import ai_config as r

        with get_db() as c:
            c.execute("DELETE FROM ai_config WHERE id = 1")
            c.execute(
                "INSERT INTO ai_config (id, provider_name, provider, api_key, model, "
                "base_url, temperature) VALUES (1, '旧', 'openai_compatible', "
                "'sk-old', 'old-m', 'https://old.example.com/v1', 0.7)"
            )
            c.execute(
                "INSERT INTO text_provider_configs (provider_name, provider_type, "
                "api_key, base_url, model, max_tokens, concurrency, temperature, "
                "document_profile, timeout, is_active) "
                "VALUES ('零温厂商', 'openai_compatible', 'sk-d2', "
                "'https://gw.example.com/v1', 'm', 4096, 4, 0.0, '', 120, 0)"
            )
            c.commit()
        await r.activate_text_provider_config("零温厂商")
        main = _main_config_get()
        assert main["temperature"] == 0.0


# ---------------------------------------------------------------------------
# D4：save_ai_config 的 provider_history 用解析后的类型
# ---------------------------------------------------------------------------
class TestRecordProviderUsageResolved:
    @pytest.mark.asyncio
    async def test_history_gets_resolved_provider(self, restore_state, monkeypatch):
        from app.routers import ai_config as r

        captured: list[tuple] = []
        monkeypatch.setattr(
            r, "record_provider_usage",
            lambda name, ptype, url, success=True: captured.append((name, ptype, url)),
        )
        await r.save_ai_config(r.AIConfigData(
            provider_name="DeepSeek", provider="",  # 留空 → 应由 URL 启发式解析
            api_key="sk-d4", model="deepseek-chat",
            base_url="https://api.deepseek.com/v1",
        ))
        assert captured, "record_provider_usage 未被调用"
        assert captured[-1][1] == "deepseek", (
            f"provider_history 应记录解析后类型 deepseek，实际 {captured[-1][1]!r}"
        )


# ---------------------------------------------------------------------------
# D5：乱码清理与 403 文案
# ---------------------------------------------------------------------------
class TestMojibakeCleanup:
    _MOJIBAKE = re.compile(r"[璁鏃鍙鈻鐨鏈鍐鍚澶]")

    def test_require_admin_message_readable(self):
        from fastapi import HTTPException

        from app.routers import ai_config as r

        class _State:
            current_user = {"id": 2, "username": "u", "role": "user"}

        class _Req:
            state = _State()
            headers: dict = {}

        with pytest.raises(HTTPException) as exc:
            r._require_admin(_Req())
        assert exc.value.status_code == 403
        assert "管理员" in exc.value.detail
        assert not self._MOJIBAKE.search(exc.value.detail)

    def test_sources_free_of_mojibake(self):
        # 扫描面固化（2026-11-03 遗留项加固）：路径以 __file__ 锚定，不再依赖
        # pytest 的工作目录（历史上 cwd 回退到仓库根曾让扫描静默扫到空目录）。
        # 口径记账：本字符集来自当年 ai_config / provider_factory 那批乱码，
        # 对 profile_service 的乱码批次不覆盖——曾评估两条扩集路线，均放弃：
        #   ①"GBK 可逆性"判据（片段 encode('gbk').decode('utf-8') 能还原连续汉字）：
        #     实测对历史乱码召回率仅 1/5（原产生成时 errors='ignore' 已丢字节，不可逆）；
        #   ②字符枚举扩集：逐字符统计发现候选 57 字里 U+5CF0、U+7845 两字在
        #     backend/app 正常源码中分别出现 71/2 次（属正常用字），误报风险大于收益。
        # 因此 profile_service 的修复点改用下方 test_conflict_messages_readable 正向断言。
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        pairs = (
            "app/services/ai/provider_factory.py",
            "app/routers/ai_config.py",
        )
        for rel in pairs:
            p = root / rel
            assert p.exists(), f"护栏扫描目标缺失: {p}"
            with p.open(encoding="utf-8") as f:
                src = f.read()
            assert not self._MOJIBAKE.search(src), f"{rel} 仍有乱码字符"

    def test_conflict_messages_readable(self):
        """正向文案护栏（2026-11-03 遗留项加固）。

        本轮实际发生过一次事故：对 `ai_config.py` 的一行 import 做 SearchReplace 时，
        IDE 编辑器缓冲区把整文件写回旧快照，将上一轮已修复的 rotation / stream
        docstring 回退成乱码。下列断言把两文件的中文文案钉死，任何后续回滚都会
        在这条测试上稳定失败（而不是等到人工发现）。
        """
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        expectations = {
            "app/services/ai/profile_service.py": (
                "If-Match 值不合法",
                "配置已被其他写入更新(etag=%s)",
                "乐观锁前置校验(If-Match / etag)",
                "配置写入版本冲突",
                "版本不一致时拒绝写入",
            ),
            "app/routers/ai_config.py": (
                "轮转到期状态",
                "前端热更新感知",
                "SSE: 配置版本变更推送",
                "provider_type 非法",
            ),
        }
        for rel, needles in expectations.items():
            p = root / rel
            with p.open(encoding="utf-8") as f:
                src = f.read()
            for needle in needles:
                assert needle in src, f"{rel} 文案回退/丢失: {needle!r}"
