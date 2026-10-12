"""文本模型配置模块：2026-10-11 深度审计与修复的回归测试。

本轮新增缺口修复（编号与代码内 G-x 注释对应）：
  G-1  Profile 数值参数（temperature / max_tokens / concurrency / timeout /
       rate_limit_per_min / token_budget_daily）落库前统一钳制到合法区间，
       避免「只改一个字段」时把主配置写死到 temperature=-1、concurrency=0
       这类会拖垮模型的取值（与 save_ai_config 口径对齐）。
  G-2  POST /test 探针参数钳制 + 空 base_url 兜底：max_tokens=1_000_000、
       temperature=999 曾直接落 chat()，让「测试连接」在厂商侧 400/500 后
       被误判为密钥失效。
  G-3  GET /provider-configs/{name} 补 api_key_set 布尔提示（前端可据此
       直接显示「已配置/未配置」，无需再判字符串），与图像侧口径对齐。
  G-4  GET /image-provider-configs/{name} 补 updated_at + api_key_set
       （F-13 同构口径未同步到图像单查）。
  G-5  GET /config-audit 新增 action/target 过滤参数，运维可精准查询而不必
       拉全量。
  G-6  POST /config/clear-api-key 显式清空主配置 API Key，前端「重置密钥」
       场景无需再走「保存占位符 → 触发保存路径误判」的曲折路径。
"""
from __future__ import annotations

import asyncio
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
def restore_text_provider_configs():
    with get_db(readonly=True) as c:
        rows = _snapshot_rows(c, "SELECT * FROM text_provider_configs")
    yield
    with get_db() as c:
        _restore_rows(c, "text_provider_configs", rows)
        c.commit()
    from app.services.ai.provider_factory import _clear_ai_config_cache

    _clear_ai_config_cache()


@pytest.fixture
def restore_image_provider_configs():
    with get_db(readonly=True) as c:
        rows = _snapshot_rows(c, "SELECT * FROM image_provider_configs")
    yield
    with get_db() as c:
        _restore_rows(c, "image_provider_configs", rows)
        c.commit()


@pytest.fixture
def restore_ai_profiles():
    with get_db(readonly=True) as c:
        rows = _snapshot_rows(c, "SELECT * FROM ai_profiles")
    yield
    with get_db() as c:
        _restore_rows(c, "ai_profiles", rows)
        c.commit()


@pytest.fixture
def restore_audit():
    """快照审计表：本轮 G-5 会往表里塞探测行，不能污染真实审计历史。"""
    with get_db(readonly=True) as c:
        rows = _snapshot_rows(c, "SELECT * FROM ai_config_audit")
    yield
    with get_db() as c:
        _restore_rows(c, "ai_config_audit", rows)
        c.commit()


# ============================================================================
# G-1：Profile 数值参数钳制
# ============================================================================
class TestG1ProfileParamClamp:
    """Profile 数值字段落库前必须钳制到合法区间（与 save_ai_config 口径一致）。

    回归目标：此前 temperature=-1 / concurrency=0 / timeout=-100 都可原样落库，
    用户 apply_profile 后主配置被污染，模型产出乱码或直接拒绝。
    """

    def _make_profile(self, **overrides):
        from app.services.ai.profile_service import create_profile

        defaults = dict(
            name=f"g1-{overrides.get('name', 'a')}",
            api_key="",
        )
        defaults.update(overrides)
        return create_profile(defaults)

    def _cleanup(self, names):
        with get_db() as c:
            for n in names:
                c.execute("DELETE FROM ai_profiles WHERE name = ?", (n,))
            c.commit()

    def test_create_profile_clamps_temperature(self, restore_ai_profiles):
        """temperature=-5 应被钳到 0.0，temperature=99 应被钳到 2.0。"""
        from app.services.ai.profile_service import create_profile, get_profile

        name_a = "g1-temp-low"
        create_profile({"name": name_a, "temperature": -5.0})
        assert get_profile(name_a)["temperature"] == 0.0

        name_b = "g1-temp-high"
        create_profile({"name": name_b, "temperature": 99.0})
        assert get_profile(name_b)["temperature"] == 2.0

        self._cleanup([name_a, name_b])

    def test_create_profile_clamps_max_tokens_and_concurrency(self, restore_ai_profiles):
        from app.services.ai.profile_service import create_profile, get_profile

        name = "g1-mtcc"
        create_profile(
            {
                "name": name,
                "max_tokens": 10_000_000,
                "concurrency": 100_000,
                "timeout": -100,
            }
        )
        p = get_profile(name)
        assert p["max_tokens"] == 65536, f"max_tokens 应钳到 65536，实际 {p['max_tokens']}"
        assert p["concurrency"] == 64, f"concurrency 应钳到 64，实际 {p['concurrency']}"
        assert p["timeout"] == 5, f"timeout 应钳到最小值 5，实际 {p['timeout']}"
        self._cleanup([name])

    def test_create_profile_clamps_rate_and_budget(self, restore_ai_profiles):
        from app.services.ai.profile_service import create_profile, get_profile

        name = "g1-rb"
        create_profile(
            {
                "name": name,
                "rate_limit_per_min": -10,
                "token_budget_daily": -9999,
            }
        )
        p = get_profile(name)
        assert p["rate_limit_per_min"] == 0
        assert p["token_budget_daily"] == 0
        self._cleanup([name])

    def test_update_profile_only_clamps_supplied_fields(self, restore_ai_profiles):
        """未提供的字段应保留库内原值（不被默认值覆盖，保留 COALESCE 语义）。"""
        from app.services.ai.profile_service import create_profile, get_profile, update_profile

        name = "g1-upd"
        create_profile({"name": name, "temperature": 0.3, "concurrency": 8})
        # 仅改 temperature：concurrency 不应被改
        update_profile(name, {"temperature": 99.0})
        p = get_profile(name)
        assert p["temperature"] == 2.0
        assert p["concurrency"] == 8, f"未传字段不应被覆盖，实际 concurrency={p['concurrency']}"
        self._cleanup([name])

    def test_clamp_helper_handles_bad_strings(self):
        """_clamp_profile_params 对 None / 空串 / 非数字应回落默认值不抛错。"""
        from app.services.ai.profile_service import _clamp_profile_params

        out = _clamp_profile_params({"temperature": "abc", "max_tokens": None, "concurrency": ""})
        assert out["temperature"] == 0.7
        assert out["max_tokens"] == 16384
        assert out["concurrency"] == 4

    def test_create_profile_valid_values_unchanged(self, restore_ai_profiles):
        """合法值不被改写（防止过度钳制误伤正常配置）。"""
        from app.services.ai.profile_service import create_profile, get_profile

        name = "g1-normal"
        create_profile(
            {
                "name": name,
                "temperature": 0.7,
                "max_tokens": 8192,
                "concurrency": 4,
                "timeout": 120,
            }
        )
        p = get_profile(name)
        assert p["temperature"] == 0.7
        assert p["max_tokens"] == 8192
        assert p["concurrency"] == 4
        assert p["timeout"] == 120
        self._cleanup([name])


# ============================================================================
# G-2：POST /test 参数钳制与空 base_url 兜底
# ============================================================================
class TestG2TestAiConnection:
    def test_clamp_helper_clamps_extreme_values(self):
        """探针参数钳制器：越界值必须落到合法区间。"""
        from app.routers.ai_config import _clamp_test_ai_params

        m, t = _clamp_test_ai_params(1_000_000, 999.0)
        assert m == 65536
        assert t == 2.0

        m, t = _clamp_test_ai_params(10, -5.0)
        assert m == 256
        assert t == 0.0

        # 越界字符串与 None 应回落默认
        m, t = _clamp_test_ai_params("abc", None)
        assert m == 4096
        assert t == 0.7

    def test_test_ai_connection_rejects_dangerous_url(self):
        """base_url=file:// 必须被 SSRF 拦截（此前 save 路径已修，test 路径需同口径）。"""
        from fastapi import HTTPException

        from app.routers.ai_config import AIConfigTest, test_ai_connection

        with pytest.raises(HTTPException) as exc:
            asyncio.run(
                test_ai_connection(
                    AIConfigTest(
                        provider="openai_compatible",
                        api_key="sk-x",
                        model="gpt-4o",
                        base_url="file:///etc/passwd",
                    )
                )
            )
        assert exc.value.status_code == 400

    def test_test_ai_connection_rejects_empty_base_url_without_default(self, monkeypatch):
        """base_url 为空且厂商未收录 → 400（不允许把空 URL 落 chat）。"""
        import app.routers.ai_config as ac
        from fastapi import HTTPException
        from app.routers.ai_config import AIConfigTest, test_ai_connection

        # 传入未收录的厂商名，避免 catalog 命中默认 base_url
        monkeypatch.setattr(
            ac.provider_catalog,
            "find_default_base_url_for_provider",
            lambda name: "",
        )
        with pytest.raises(HTTPException) as exc:
            asyncio.run(
                test_ai_connection(
                    AIConfigTest(
                        provider="no-such-provider",
                        api_key="sk-x",
                        model="gpt-4o",
                        base_url="",
                    )
                )
            )
        assert exc.value.status_code == 400

    def test_find_default_base_url_for_provider_matches_type(self):
        """provider_catalog 新增值：能按 type（如 'deepseek'）反查 base_url。"""
        from app.services.ai.provider_catalog import find_default_base_url_for_provider

        assert find_default_base_url_for_provider("DeepSeek").startswith("https://")
        assert find_default_base_url_for_provider("deepseek").startswith("https://")
        assert find_default_base_url_for_provider("unknown-xyz") == ""
        assert find_default_base_url_for_provider("") == ""

    def test_source_clamps_params_before_chat(self):
        """自证：test_ai_connection 源码中确实在 chat 之前调用了钳制器。"""
        import app.routers.ai_config as m

        src = inspect.getsource(m.test_ai_connection)
        assert "_clamp_test_ai_params" in src
        # 钳制器必须在构造 config dict 之前调用（顺序钉死）
        idx_clamp = src.index("_clamp_test_ai_params")
        idx_config = src.index("config = {")
        assert idx_clamp < idx_config


# ============================================================================
# G-3：文本厂商单查补 api_key_set
# ============================================================================
class TestG3TextProviderConfigShape:
    def test_get_text_provider_config_includes_api_key_set_and_updated_at(
        self, restore_ai_config, restore_text_provider_configs
    ):
        from app.routers.ai_config import (
            TextProviderConfigData,
            get_text_provider_config,
            save_text_provider_config,
        )

        name = "g3-provider"
        asyncio.run(
            save_text_provider_config(
                name,
                TextProviderConfigData(
                    provider_name=name,
                    api_key="sk-g3",
                    base_url="https://g3.example.com/v1",
                    model="m",
                    activate_after_save=False,  # 不干扰主表
                ),
            )
        )
        single = asyncio.run(get_text_provider_config(name))
        # F-13 既有断言 + G-3 新增字段
        assert "updated_at" in single and single["updated_at"]
        assert single["api_key_set"] is True
        assert single["api_key"] == "***已配置***", "脱敏占位符必须原样保留"

    def test_get_text_provider_config_unknown_returns_empty_api_key_set(
        self,
        restore_text_provider_configs,
    ):
        from app.routers.ai_config import get_text_provider_config

        single = asyncio.run(get_text_provider_config("g3-nonexistent"))
        assert single["api_key_set"] is False
        assert single["api_key"] == ""
        assert "updated_at" in single


# ============================================================================
# G-4：图像厂商单查补 updated_at + api_key_set
# ============================================================================
class TestG4ImageProviderConfigShape:
    def test_get_image_provider_config_includes_updated_at(
        self, restore_image_provider_configs
    ):
        from app.routers.ai_config import get_image_provider_config, save_image_provider_config
        from app.routers.ai_config import ImageProviderConfigData

        # 用真实保存路径产生一行（避免直接 SQL 注入，也让 updated_at 走真实写入）
        name = "g4-probe"
        asyncio.run(
            save_image_provider_config(
                name,
                ImageProviderConfigData(
                    provider_name=name,
                    api_key="sk-g4-image",
                    base_url="https://g4.example.com/v1",
                    model="img-model",
                ),
            )
        )
        single = asyncio.run(get_image_provider_config(name))
        assert "updated_at" in single, "G-4：图像单查必须返回 updated_at（与列表口径同构）"
        assert single["updated_at"], "已保存的厂商 updated_at 应非空"
        assert single["api_key_set"] is True

    def test_get_image_provider_config_unknown_shape(
        self, restore_image_provider_configs
    ):
        from app.routers.ai_config import get_image_provider_config

        single = asyncio.run(get_image_provider_config("g4-nonexistent"))
        assert single["api_key_set"] is False
        assert single["api_key"] == ""
        assert single["updated_at"] == ""


# ============================================================================
# G-5：审计查询过滤
# ============================================================================
class TestG5AuditFilters:
    def _seed(self, action: str, target: str):
        with get_db() as c:
            c.execute(
                "INSERT INTO ai_config_audit (actor, action, target, detail, created_at)"
                " VALUES (?, ?, ?, ?, datetime('now', 'localtime'))",
                ("tester", action, target, "detail"),
            )
            c.commit()

    def test_filter_by_action(self, restore_audit):
        from app.routers.ai_config import get_config_audit

        self._seed("save_config", "g5-probe-a")
        self._seed("activate_provider_config", "g5-probe-b")
        self._seed("save_config", "g5-probe-c")

        # 按唯一 target 前缀 g5-probe- 限定探测行，避免测试间相互干扰
        res = asyncio.run(get_config_audit(limit=500, action="save_config", target="g5-probe-"))
        actions = [it["action"] for it in res["items"]]
        assert set(actions) == {"save_config"}, f"应仅返回 save_config，实际 {set(actions)}"
        targets = [it["target"] for it in res["items"]]
        assert all(t.startswith("g5-probe-") for t in targets)
        assert len(actions) == 2, f"应恰好 2 条 save_config 探测行，实际 {len(actions)}"

    def test_filter_by_target_prefix(self, restore_audit):
        from app.routers.ai_config import get_config_audit

        self._seed("save_config", "厂商A")
        self._seed("activate_provider_config", "厂商B")
        self._seed("save_config", "厂商B")

        res = asyncio.run(get_config_audit(limit=50, target="厂商B"))
        targets = [it["target"] for it in res["items"]]
        assert all(t.startswith("厂商B") for t in targets), f"应仅返回 target 前缀匹配：{targets}"
        assert len(targets) == 2

    def test_combined_action_and_target(self, restore_audit):
        from app.routers.ai_config import get_config_audit

        self._seed("save_config", "厂商A")
        self._seed("save_config", "厂商B")
        self._seed("activate_provider_config", "厂商A")

        res = asyncio.run(get_config_audit(limit=50, action="save_config", target="厂商A"))
        items = res["items"]
        assert len(items) == 1
        assert items[0]["action"] == "save_config"
        assert items[0]["target"].startswith("厂商A")

    def test_limit_clamp(self, restore_audit):
        from app.routers.ai_config import get_config_audit

        for i in range(5):
            self._seed("save_config", f"g5-{i}")
        res = asyncio.run(get_config_audit(limit=-1))
        assert res["total"] >= 1  # 下限保护
        res = asyncio.run(get_config_audit(limit=999999))
        assert res["total"] <= 500  # 上限保护

    def test_no_filter_returns_full_history(self, restore_audit):
        from app.routers.ai_config import get_config_audit

        self._seed("save_config", "g5-full")
        res = asyncio.run(get_config_audit(limit=50))
        # 无过滤参数时行为向后兼容（返回全部，不报错）
        assert "items" in res
        assert "total" in res


# ============================================================================
# G-6：显式清空主配置 API Key
# ============================================================================
class TestG6ClearApiKey:
    def test_clear_api_key_clears_and_bumps_version(self, restore_ai_config):
        """显式清密钥后库内应回退为空，且版本号递增（跨进程缓存失效）。"""
        from app.routers import ai_config as ac
        from app.routers.ai_config import AIConfigData, clear_ai_config_api_key, save_ai_config
        from app.services.ai import provider_factory as pf

        asyncio.run(
            save_ai_config(
                AIConfigData(
                    provider_name="g6",
                    provider="openai_compatible",
                    api_key="sk-g6-original",
                    model="m",
                    base_url="https://g6.example.com/v1",
                )
            )
        )
        # 库内应有 ENC: 密文
        with get_db(readonly=True) as c:
            row = c.execute("SELECT api_key FROM ai_config WHERE id = 1").fetchone()
        assert row and row["api_key"], "前置：save 后库内应有密文"
        v_before = pf._read_config_version()

        # 清空
        asyncio.run(clear_ai_config_api_key())

        with get_db(readonly=True) as c:
            row = c.execute("SELECT api_key, provider_name, model FROM ai_config WHERE id = 1").fetchone()
        assert row["api_key"] == "", f"清空后 api_key 应为空，实际 {row['api_key']!r}"
        # 非密钥字段保留（用户只想清密钥，不应连带清模型/URL）
        assert row["provider_name"] == "g6"
        assert row["model"] == "m"

        v_after = pf._read_config_version()
        assert v_after > v_before, "清空必须触发版本号递增"

    def test_clear_api_key_writes_audit(self, restore_ai_config, restore_audit):
        from app.routers.ai_config import clear_ai_config_api_key, get_config_audit

        asyncio.run(clear_ai_config_api_key())
        res = asyncio.run(get_config_audit(limit=20, action="clear_api_key"))
        assert res["total"] >= 1, "clear 必须写入审计行"

    def test_clear_api_key_no_mask_leakage(self, restore_ai_config):
        """清空操作不得把库内密文当明文回传（防御性断言）。"""
        import inspect

        import app.routers.ai_config as m

        src = inspect.getsource(m.clear_ai_config_api_key)
        assert "decrypt_api_key" not in src, "清空操作不得解密库内密钥"
        # 清空必须用空串而非占位符（防止把 API_KEY_MASK 字符串落库）
        assert "API_KEY_MASK" not in src, "清空操作不得写入占位符字符串"
        # UPDATE 语句必须真的把 api_key 置空
        assert "api_key = ''" in src

    def test_clear_then_save_restores_key(self, restore_ai_config):
        """清空后再保存新密钥：链路应无遗留状态。"""
        from app.routers.ai_config import AIConfigData, clear_ai_config_api_key, save_ai_config
        from app.services.crypto import is_encrypted

        asyncio.run(clear_ai_config_api_key())
        asyncio.run(
            save_ai_config(
                AIConfigData(
                    provider_name="g6b",
                    provider="openai_compatible",
                    api_key="sk-g6-b-new",
                    model="m",
                    base_url="https://g6b.example.com/v1",
                )
            )
        )
        with get_db(readonly=True) as c:
            row = c.execute("SELECT api_key FROM ai_config WHERE id = 1").fetchone()
        assert is_encrypted(row["api_key"]), "清空后再保存新密钥必须加密落库"
        assert "sk-g6-b-new" not in row["api_key"]


# ============================================================================
# 交叉一致性：新旧契约共存
# ============================================================================
class TestCrossConsistency:
    def test_config_response_still_returns_config_source(self, restore_ai_config):
        """新改动不得影响 L-4 回显 config_source 的既有契约。"""
        from app.routers.ai_config import get_ai_config

        res = asyncio.run(get_ai_config())
        assert "config_source" in res
        assert res["config_source"] in ("default", "database", "env")

    def test_config_response_still_masks_key(self, restore_ai_config):
        from app.routers.ai_config import AIConfigData, get_ai_config, save_ai_config

        asyncio.run(
            save_ai_config(
                AIConfigData(
                    provider_name="g-cons",
                    provider="openai_compatible",
                    api_key="sk-g-cons-secret",
                    model="m",
                    base_url="https://g-cons.example.com/v1",
                )
            )
        )
        res = asyncio.run(get_ai_config())
        assert res["api_key"] == "***已配置***", "读接口必须脱敏，不得泄漏明文密钥"

    def test_profile_import_export_still_works(self, restore_ai_profiles):
        """G-1 钳制不应破坏 Profile 导入/导出流程。"""
        from app.services.ai.profile_service import create_profile, export_profiles, import_profiles

        name = "g-import"
        create_profile({"name": name, "api_key": "", "temperature": 0.5})
        exported = export_profiles()
        assert exported and len(exported) >= 1
        # 导入不应因钳制器报错（新导入走 create_profile，同样会走钳制）
        result = import_profiles({"profiles": [{"name": "g-import-2", "temperature": 0.5}]})
        assert result.get("imported", 0) >= 1
        with get_db() as c:
            c.execute("DELETE FROM ai_profiles WHERE name LIKE 'g-import%'")
            c.commit()


if __name__ == "__main__":
    # 便于本地直接 python 执行
    import sys

    sys.exit(pytest.main([__file__, "-q", "-p", "no:randomly"]))
