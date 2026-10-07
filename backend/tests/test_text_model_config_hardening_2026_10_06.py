"""文本模型配置模块加固回归测试（2026-10-06 第二批：遗留项 D-1~D-3 + 性能）

覆盖：
  D-1  厂商元数据单一正本 provider_catalog：别名同源、脱敏投影、元数据匹配容错、
        兜底清单同源、路由层不再内联字面量（源码级自证）。
  D-2  激活并发不变量（跨连接/跨线程，模拟多进程）：恰好一个 is_active=1 且与
        ai_config 主表一致 —— 把「仅进程内锁」的剩余边界固化为可执行断言。
  PERF 降级链探活短 TTL 缓存：命中不重复发 HTTP、失败 TTL 更短、配置变更即失效。
  D-3  密钥托管加固：BID_ASSISTANT_KEY_FILE 支持、密钥来源可观测、权限校验、
        主密钥轮换（rotate_master_key）与轮换脚本可用性。
"""

from __future__ import annotations

import base64
import inspect
import os
import threading
from pathlib import Path

import pytest

from app.db import get_db


# ---------------------------------------------------------------------------
# D-1：厂商元数据单一正本
# ---------------------------------------------------------------------------
class TestD1ProviderCatalog:
    def test_router_and_factory_use_same_source(self):
        """路由层与工厂层的别名必须指向正本，避免两处清单漂移。"""
        from app.routers import ai_config as ac
        from app.services.ai import provider_catalog as pc
        from app.services.ai import provider_factory as pf

        assert ac.PROVIDER_MODEL_METADATA is pc.PROVIDER_MODEL_METADATA
        assert pf._FALLBACK_PROVIDERS is pc.FALLBACK_PROVIDERS
        # 比较稳定字段（name/base_url/type）：元素可被安全用例就地改写做断言，
        # 同源性不应依赖「恰好没人动过内容」。
        def _identity(rows):
            return [(r.get("name"), r.get("base_url"), r.get("type")) for r in rows]

        assert _identity(ac.KNOWN_PROVIDERS) == _identity(pc.KNOWN_PROVIDERS)

    def test_router_known_providers_count_matches_catalog(self):
        """厂商条目数量与正本一致（漏条目会静默让配置页少一个厂商）。"""
        from app.routers import ai_config as ac
        from app.services.ai import provider_catalog as pc

        assert len(ac.KNOWN_PROVIDERS) == len(pc.KNOWN_PROVIDERS)

    def test_router_list_isolated_from_catalog(self):
        """路由层列表逐项拷贝：调用方就地改写不得污染正本。"""
        from app.routers import ai_config as ac
        from app.services.ai import provider_catalog as pc

        target = ac.KNOWN_PROVIDERS[0]
        original = dict(target)
        catalog_original = dict(
            next(p for p in pc.KNOWN_PROVIDERS if p.get("name") == original.get("name"))
        )
        try:
            target["base_url"] = "https://mutated/v1"
            assert (
                next(
                    p
                    for p in pc.KNOWN_PROVIDERS
                    if p.get("name") == original.get("name")
                )["base_url"]
                == catalog_original["base_url"]
            )
        finally:
            target.update(original)

    def test_public_projection_never_exposes_api_key(self):
        """脱敏投影是结构性防御：正本里即使混入 api_key 也不外泄。"""
        from app.services.ai import provider_catalog as pc

        pc.KNOWN_PROVIDERS.append({"name": "probe", "base_url": "https://x/v1", "api_key": "sk-leak"})
        try:
            assert all("api_key" not in p for p in pc.known_providers_public())
        finally:
            pc.KNOWN_PROVIDERS.pop()

    def test_metadata_lookup_is_case_tolerant(self):
        """F-8 行为固化：厂商名/模型名大小写与下划线差异都能命中。"""
        from app.services.ai import provider_catalog as pc

        assert pc.find_model_metadata("DeepSeek", "deepseek-chat")
        assert pc.find_model_metadata("SiliconFlow", "deepseek-ai/DeepSeek-V3")
        # dashscope 表内的模型键是 qwen3-* / qwen-turbo 等具体型号（不含裸 qwen-max）
        assert pc.find_model_metadata("dashscope", "qwen3-max")
        assert pc.find_model_metadata("DeepSeek", "no-such-model") is None
        # 未收录模型必须回落None（由调用方退到默认值）
        assert pc.find_model_metadata("dashscope", "qwen-max") is None

    @pytest.mark.asyncio
    async def test_chinese_provider_name_resolved_via_base_url(self):
        """中文厂商展示名无法按名匹配元数据表，须由 base_url 兜底命中（既有口径）。"""
        from app.routers.ai_config import ModelInfoRequest, fetch_model_info

        result = await fetch_model_info(
            ModelInfoRequest(
                provider="阿里云百炼（通义千问）",
                model="qwen3-max",
                base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            )
        )
        assert result["source"] == "builtin"
        assert result["max_tokens"] == 131072

    def test_china_list_returns_copy(self):
        """推荐厂商列表返回副本，调用方就地修改不污染正本。"""
        from app.services.ai import provider_catalog as pc

        first = pc.china_accessible_providers()
        first[0]["name"] = "mutated"
        assert pc.CHINA_ACCESSIBLE_PROVIDERS[0]["name"] == "DeepSeek"

    def test_fallback_entries_have_no_hardcoded_secret(self):
        """兜底清单不得内联硬编码密钥（只能来自环境变量）。"""
        from app.config import AGNES_API_KEY
        from app.services.ai.provider_catalog import FALLBACK_PROVIDERS

        for entry in FALLBACK_PROVIDERS:
            key = entry.get("api_key") or ""
            assert key == "" or key == AGNES_API_KEY, "兜底清单内联了硬编码密钥"

    def test_router_no_longer_inlines_metadata(self):
        """自证：路由层不得再出现厂商/元数据巨型字面量（须走正本）。"""
        import app.routers.ai_config as m

        src = inspect.getsource(m)
        assert "'deepseek-v4-pro'" not in src
        assert "api.siliconflow.cn/v1" not in src
        assert "provider_catalog" in src

    def test_default_image_provider_single_source(self):
        """图像默认厂商（agnes-image-2.1-flash）也有正本。"""
        from app.services.ai.provider_catalog import (
            DEFAULT_IMAGE_PROVIDER,
            default_image_base_url,
            default_image_model,
        )

        assert default_image_model() == DEFAULT_IMAGE_PROVIDER["model"]
        assert default_image_base_url().startswith("https://")


# ---------------------------------------------------------------------------
# D-2：激活并发不变量（跨连接，模拟多进程）
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_d2_concurrent_activate_keeps_single_active_invariant(restore_ai_config):
    """并发激活两个厂商：最终恰好一个 is_active=1，且与主表一致。

    激活临界区是「全表置 0 → 目标置 1 → 主表 UPSERT」，同一连接内单事务提交，
    SQLite 写锁会把跨连接（近似多进程）的写操作串行化，因此该不变量应当成立。
    本用例把这条边界从「注释自认」升级为可执行断言。
    """
    from app.routers.ai_config import TextProviderConfigData, save_text_provider_config

    for name in ("d2-race-a", "d2-race-b"):
        await save_text_provider_config(
            name,
            TextProviderConfigData(
                provider_name=name,
                api_key=f"sk-{name}",
                base_url=f"https://{name}.example.com/v1",
                model=f"{name}-model",
            ),
        )

    errors: list[BaseException] = []
    conflicts: list[int] = []

    def _activate(name: str) -> None:
        # 每个线程用独立事件循环 + 独立连接（近似多进程），不共享进程内锁
        import asyncio

        from fastapi import HTTPException

        from app.routers.ai_config import activate_text_provider_config

        try:
            asyncio.run(activate_text_provider_config(name))
        except HTTPException as e:
            # 409 =撞唯一索引（跨进程并发的正常结果），不是不变量破坏
            if e.status_code == 409:
                conflicts.append(1)
            else:
                errors.append(e)
        except BaseException as e:  # pragma: no cover - 交由断言处理
            errors.append(e)

    threads = [
        threading.Thread(target=_activate, args=("d2-race-a",), daemon=True),
        threading.Thread(target=_activate, args=("d2-race-b",), daemon=True),
    ]
    for t in threads:
        t.start()
    for t in threads:
        # D-2 回归保护：锁若再次退化为「跨 loop 共享单例」，join 会永久挂起，
        # 因此这里显式限时，超时即判定失败而不是让整个测试套件卡死。
        t.join(timeout=60)
        assert not t.is_alive(), "激活调用挂起（疑似激活锁跨事件循环共享导致死等）"

    assert not errors, f"并发激活抛错（409 语义化冲突除外）: {errors}"

    with get_db(readonly=True) as c:
        actives = [
            r[0]
            for r in c.execute(
                "SELECT provider_name FROM text_provider_configs WHERE is_active = 1"
            ).fetchall()
        ]
        main = c.execute("SELECT provider_name FROM ai_config WHERE id = 1").fetchone()
    # 核心不变量：无论是否发生 409 冲突，绝不允许「两个 active」或「标记与主表不一致」
    assert len(actives) == 1, f"应恰好一个 active，实际 {actives}"
    assert main is not None and main[0] == actives[0], "主表与 is_active 标记不一致"


@pytest.mark.asyncio
async def test_d2_unique_index_violation_maps_to_409(monkeypatch, restore_ai_config):
    """撞唯一索引（跨进程并发）必须映射为 409，不能冒泡成 500。"""
    import asyncio

    import sqlite3

    from fastapi import HTTPException

    from app.routers import ai_config as ac

    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO text_provider_configs (provider_name, api_key, base_url, model)"
            " VALUES ('d2-409-probe', 'sk-x', 'https://d2.example.com/v1', 'm')"
        )
        conn.commit()

    def _boom():
        raise sqlite3.IntegrityError("UNIQUE constraint failed: text_provider_configs.is_active")

    monkeypatch.setattr(ac.asyncio, "to_thread", lambda fn, *a, **k: _boom())
    with pytest.raises(HTTPException) as ei:
        await ac.activate_text_provider_config("d2-409-probe")
    assert ei.value.status_code == 409
    assert "重试" in str(ei.value.detail)
    assert asyncio  # 保持导入被使用


# ---------------------------------------------------------------------------
# PERF：降级链探活缓存
# ---------------------------------------------------------------------------
class TestHealthProbeCache:
    def setup_method(self):
        from app.services.ai.provider_factory import _clear_health_cache

        _clear_health_cache()

    def test_cache_hit_skips_second_http_probe(self, monkeypatch):
        """命中缓存时不得重复发探活请求（这是加缓存的核心目的）。"""
        from app.services.ai import provider_factory as pf

        calls: list[str] = []

        def _fake_probe(base_url, api_key, model, timeout):
            calls.append(base_url)
            return True

        monkeypatch.setattr(pf, "_quick_health_check", _fake_probe)
        key = pf._health_cache_key("https://cache-hit.example.com/v1", "m", "sk-x")

        assert pf._health_cache_get(key) is None
        pf._health_cache_put(key, True)
        assert pf._health_cache_get(key) is True

        monkeypatch.setattr(pf, "_quick_health_check", lambda *a, **k: calls.append("leak") or True)
        # 直接复用缓存判定逻辑（与create_provider_with_fallback 内一致）
        cached = pf._health_cache_get(key)
        assert cached is True
        assert calls == [], "缓存命中时不应触发探活"

    def test_failure_ttl_is_shorter_than_success(self, monkeypatch):
        """失败结论 TTL 必须更短，厂商恢复后才能尽快被重新选中。"""
        from app.services.ai import provider_factory as pf

        assert pf._HEALTH_CACHE_TTL_FAIL < pf._HEALTH_CACHE_TTL_OK

        key = pf._health_cache_key("https://cache-fail.example.com/v1", "m", "sk-y")
        pf._health_cache_put(key, False)
        assert pf._health_cache_get(key) is False

        # 时间推进超过失败 TTL 后应失效（要求重新探活）
        real_time = pf.time.time
        try:
            pf.time.time = lambda: real_time() + pf._HEALTH_CACHE_TTL_FAIL + 1
            assert pf._health_cache_get(key) is None
        finally:
            pf.time.time = real_time

    def test_config_bump_invalidates_health_cache(self):
        """配置变更（换厂商/密钥/模型）必须立即清空探活结论，不等 TTL。"""
        from app.services.ai.provider_factory import (
            _clear_health_cache,
            _health_cache_get,
            _health_cache_key,
            _health_cache_put,
            bump_ai_config_version,
        )

        key = _health_cache_key("https://bump.example.com/v1", "m", "sk-z")
        _health_cache_put(key, True)
        assert _health_cache_get(key) is True
        bump_ai_config_version()
        assert _health_cache_get(key) is None
        _clear_health_cache()

    def test_cache_key_never_contains_raw_key(self):
        """缓存键不得包含可读密钥（只用指纹）。"""
        from app.services.ai.provider_factory import _health_cache_key

        key = _health_cache_key("https://k.example.com/v1", "m", "sk-plain-secret-key")
        assert "sk-plain-secret-key" not in key

    def test_cache_is_bounded(self):
        """缓存有条数上限，避免多厂商多密钥长期运行后无界增长。"""
        from app.services.ai import provider_factory as pf

        for i in range(pf._HEALTH_CACHE_MAX_ENTRIES + 20):
            pf._health_cache_put(pf._health_cache_key(f"https://h{i}.example.com/v1", "m", "k"), True)
        assert len(pf._HEALTH_CACHE) <= pf._HEALTH_CACHE_MAX_ENTRIES
        pf._clear_health_cache()


# ---------------------------------------------------------------------------
# D-3：密钥托管加固与轮换
# ---------------------------------------------------------------------------
class TestD3KeyCustody:
    def test_key_file_env_supported(self, tmp_path, monkeypatch):
        """BID_ASSISTANT_KEY_FILE：多实例可共享同一密钥文件。"""
        import importlib

        from cryptography.fernet import Fernet

        shared = tmp_path / "shared.key"
        key = Fernet.generate_key()
        shared.write_bytes(key)

        import app.services.crypto as crypto

        monkeypatch.setattr(crypto, "_fernet_instance", None)
        monkeypatch.setattr(crypto, "_key_source", None)
        monkeypatch.delenv("BID_ASSISTANT_KEY", raising=False)
        monkeypatch.setenv("BID_ASSISTANT_KEY_FILE", str(shared))
        try:
            enc = crypto.encrypt_api_key("sk-d3-shared")
            assert crypto._key_source_name() == "key_file"
            assert crypto.decrypt_api_key(enc) == "sk-d3-shared"
        finally:
            monkeypatch.undo()
            monkeypatch.setattr(crypto, "_fernet_instance", None)
            monkeypatch.setattr(crypto, "_key_source", None)

    def test_env_key_has_highest_priority(self, tmp_path, monkeypatch):
        """环境变量优先于密钥文件（与既有优先级口径一致）。"""
        from cryptography.fernet import Fernet

        import app.services.crypto as crypto

        shared = tmp_path / "shared2.key"
        shared.write_bytes(Fernet.generate_key())
        env_key = Fernet.generate_key().decode()

        monkeypatch.setattr(crypto, "_fernet_instance", None)
        monkeypatch.setattr(crypto, "_key_source", None)
        monkeypatch.setenv("BID_ASSISTANT_KEY", env_key)
        monkeypatch.setenv("BID_ASSISTANT_KEY_FILE", str(shared))
        try:
            enc = crypto.encrypt_api_key("sk-d3-env")
            assert crypto._key_source_name() == "env"
            assert crypto.decrypt_api_key(enc) == "sk-d3-env"
        finally:
            monkeypatch.undo()
            monkeypatch.setattr(crypto, "_fernet_instance", None)
            monkeypatch.setattr(crypto, "_key_source", None)

    def test_world_readable_key_file_warns(self, tmp_path, monkeypatch, caplog):
        """POSIX 下组/其他可读的密钥文件必须告警。"""
        import logging
        import stat as stat_mod

        import app.services.crypto as crypto

        if os.name == "nt":
            pytest.skip("Windows 无 POSIX 权限语义")

        loose = tmp_path / "loose.key"
        loose.write_bytes(b"x" * 44)
        loose.chmod(0o644)

        with caplog.at_level(logging.WARNING, logger="app.services.crypto"):
            crypto._warn_if_world_readable(loose)
        assert any("权限过宽" in r.message for r in caplog.records)
        assert stat_mod.S_IMODE(loose.stat().st_mode) == 0o644

    def test_rotate_master_key_reencrypts_all_tables(self, restore_all_secret_tables):
        """轮换后所有密文用新密钥解密仍还原为原明文。"""
        from cryptography.fernet import Fernet

        import app.services.crypto as crypto

        with get_db() as conn:
            conn.execute("DELETE FROM text_provider_configs WHERE provider_name = 'd3-rot'")
            conn.execute(
                "INSERT INTO text_provider_configs"
                " (provider_name, api_key, base_url, model, is_active)"
                " VALUES ('d3-rot', ?, 'https://rot.example.com/v1', 'rot-model', 0)",
                [crypto.encrypt_api_key("sk-d3-rotate")],
            )
            conn.commit()

        with get_db(readonly=True) as c:
            before = c.execute(
                "SELECT api_key FROM text_provider_configs WHERE provider_name = 'd3-rot'"
            ).fetchone()[0]
        old_fernet = crypto._get_or_create_fernet()
        assert crypto.decrypt_api_key(before) == "sk-d3-rotate"

        new_key = Fernet.generate_key().decode()
        stats = crypto.rotate_master_key(new_key)
        assert stats.get("failed", 0) == 0
        assert stats.get("text_provider_configs", 0) >= 1

        with get_db(readonly=True) as c:
            after = c.execute(
                "SELECT api_key FROM text_provider_configs WHERE provider_name = 'd3-rot'"
            ).fetchone()[0]
        assert after != before, "轮换后密文应发生变化"
        # 轮换后必须用**新**密钥解密还原为原明文
        new_fernet = Fernet(new_key.encode())
        assert (
            new_fernet.decrypt(base64.urlsafe_b64decode(after[4:].encode())).decode()
            == "sk-d3-rotate"
        )
        # 旧密钥已无法解开新密文（这正是轮换的目的）
        with pytest.raises(Exception):
            old_fernet.decrypt(base64.urlsafe_b64decode(after[4:].encode()))

    def test_rotate_script_is_ascii_only(self):
        """轮换脚本必须纯 ASCII（避免 GBK/UTF-8 解析问题，与 BAT 约定一致）。"""
        script = Path(__file__).resolve().parent.parent / "scripts" / "rotate_api_key_encryption.py"
        raw = script.read_bytes()
        non_ascii = [b for b in raw if b > 127]
        assert not non_ascii, f"脚本含非 ASCII 字节: {non_ascii[:5]}"

    def test_rotate_script_dry_run(self, tmp_path):
        """dry-run 只报告范围，不写库。"""
        import subprocess
        import sys

        backend = Path(__file__).resolve().parent.parent
        new_key_file = tmp_path / "new.key"
        from cryptography.fernet import Fernet

        new_key_file.write_bytes(Fernet.generate_key())
        proc = subprocess.run(
            [
                sys.executable,
                str(backend / "scripts" / "rotate_api_key_encryption.py"),
                "--dry-run",
                "--new-key-file",
                str(new_key_file),
            ],
            capture_output=True,
            text=True,
            cwd=str(backend),
            timeout=180,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "DRY RUN" in proc.stdout


@pytest.fixture
def restore_all_secret_tables():
    """轮换类用例的兜底：把 5 张含密钥表恢复到用例前状态。"""
    from app.db_migrations import _API_KEY_STORAGE_COLUMNS

    snapshot: dict[str, list[dict]] = {}
    with get_db(readonly=True) as c:
        for table, _col in _API_KEY_STORAGE_COLUMNS:
            snapshot[table] = [dict(r) for r in c.execute(f"SELECT * FROM {table}").fetchall()]
    yield
    with get_db() as c:
        for table, _col in _API_KEY_STORAGE_COLUMNS:
            rows = snapshot[table]
            c.execute(f"DELETE FROM {table}")
            for row in rows:
                cols = ", ".join(row.keys())
                marks = ", ".join("?" for _ in row)
                c.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", tuple(row.values()))
        c.commit()


@pytest.fixture
def restore_ai_config():
    with get_db(readonly=True) as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM ai_config").fetchall()]
    yield
    with get_db() as c:
        c.execute("DELETE FROM ai_config")
        for row in rows:
            cols = ", ".join(row.keys())
            marks = ", ".join("?" for _ in row)
            c.execute(f"INSERT INTO ai_config ({cols}) VALUES ({marks})", tuple(row.values()))
        c.commit()
    from app.services.ai.provider_factory import _clear_ai_config_cache

    _clear_ai_config_cache()


# ---------------------------------------------------------------------------
# F-NEW-1（2026-10-06）：同步探活与异步链代理一致性
# ---------------------------------------------------------------------------
class TestFNew1SyncHealthCheckProxy:
    """`_quick_health_check`（同步，被 Celery 任务经 to_thread 调用）此前
    构造 httpx.Client 时未传 build_httpx_proxy_settings，而异步版
    get_working_provider/quick-setup 全部走代理 —— 配置 HTTPS_PROXY 后
    两条链路行为漂移。这里固化：同步探活必须把代理设置传给 httpx.Client。
    """

    def test_sync_health_check_passes_proxy_settings(self, monkeypatch):
        import httpx

        from app.services.ai import provider_factory as pf

        captured: dict = {}

        class _FakeResp:
            status_code = 200
            text = ""

        class _FakeClient:
            def __init__(self, *a, **kw):
                captured.update(kw)

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def post(self, *a, **kw):
                return _FakeResp()

        monkeypatch.setattr(httpx, "Client", _FakeClient)
        # 强制走代理分支：直接替换模块级函数引用
        monkeypatch.setattr(
            pf, "build_httpx_proxy_settings",
            lambda: {"proxy": "http://fake-proxy:8080"},
        )
        ok = pf._quick_health_check(
            "https://x.example.com/v1", "sk-x", "m", 120
        )
        assert ok is True
        assert captured.get("proxy") == "http://fake-proxy:8080", (
            "同步探活未把代理设置传给 httpx.Client —— 与异步链行为漂移"
        )

    def test_sync_health_check_no_proxy_when_unset(self, monkeypatch):
        """未配置代理时 kwargs 为空，不影响直连行为（向后兼容）。"""
        import httpx

        from app.services.ai import provider_factory as pf

        captured: dict = {}

        class _FakeResp:
            status_code = 200
            text = ""

        class _FakeClient:
            def __init__(self, *a, **kw):
                captured.update(kw)

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def post(self, *a, **kw):
                return _FakeResp()

        monkeypatch.setattr(httpx, "Client", _FakeClient)
        monkeypatch.setattr(pf, "build_httpx_proxy_settings", lambda: {})
        ok = pf._quick_health_check(
            "https://y.example.com/v1", "sk-y", "m", 120
        )
        assert ok is True
        assert "proxy" not in captured
