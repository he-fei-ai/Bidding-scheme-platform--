# -*- coding: utf-8 -*-
"""图表模块遗留问题加固测试（2026-10-27 第二批）

覆盖：
  A. Mermaid 服务健康监控（熔断器 / TTL 缓存 / 延迟统计 / 健康快照 / 运维端点）
  B. 提示词注册表完整性（7 类图表提示词存在且非空，防改写导致格式漂移）
  C. 偶发大纲测试隔离性确认（纯函数确定性，双跑一致——由运行命令保证，
     此处补充注册表/熔断器等无外部状态依赖的纯逻辑回归）
"""

import asyncio
import time
import types

import pytest

from app.services.ai.mermaid_service import (
    MermaidRenderBackend,
    MermaidServiceClient,
    get_mermaid_service_client,
)


def _fresh_client() -> MermaidServiceClient:
    return MermaidServiceClient(docker_url="http://127.0.0.1:59999")


# ============================================================
# A. Mermaid 服务健康监控
# ============================================================


class TestMermaidHealthMonitor:
    def test_ttl_cache_avoids_probe(self, monkeypatch):
        """TTL 内第二次检查直接读缓存，不发起 HTTP 探测。"""
        c = _fresh_client()
        now = time.time()
        c._backend_available[MermaidRenderBackend.DOCKER] = True
        c._backend_cache_time[MermaidRenderBackend.DOCKER] = now

        def _no_client(*a, **k):
            raise AssertionError("TTL 内不得发起探测")

        monkeypatch.setattr(c, "_get_client", _no_client)
        assert asyncio.run(c.check_docker_available()) is True

    def test_circuit_breaker_blocks_probe_after_failures(self, monkeypatch):
        """连续失败达阈值 → 冷却期内不探测、直接判不可用。"""
        c = _fresh_client()
        now = time.time()
        for _ in range(c._BACKEND_FAIL_THRESHOLD):
            c._mark_probe(MermaidRenderBackend.DOCKER, False, now - 1, error="conn refused")

        def _no_client(*a, **k):
            raise AssertionError("熔断冷却期内不得发起探测")

        monkeypatch.setattr(c, "_get_client", _no_client)
        assert asyncio.run(c.check_docker_available()) is False

    def test_probe_allowed_after_cooldown(self, monkeypatch):
        """冷却期结束 → 放行探测；恢复成功复位连续失败计数。"""
        c = _fresh_client()
        now = time.time()
        for _ in range(c._BACKEND_FAIL_THRESHOLD):
            c._mark_probe(MermaidRenderBackend.DOCKER, False, now - c._BACKEND_FAIL_COOLDOWN - 1)

        class _Resp:
            status_code = 200

        class _Client:
            async def get(self, *a, **k):
                return _Resp()

        async def _fake_client():
            return _Client()

        monkeypatch.setattr(c, "_get_client", _fake_client)
        assert asyncio.run(c.check_docker_available()) is True
        h = c._backend_health[MermaidRenderBackend.DOCKER]
        assert h["consecutive_failures"] == 0
        assert h["latency_ms"] is not None

    def test_failure_records_latency_and_error(self):
        """失败探测记录延迟与错误信息（供健康快照/排障）。"""
        c = _fresh_client()
        c._mark_probe(MermaidRenderBackend.DOCKER, False, time.time(), 123.4, error="HTTP 503")
        h = c._backend_health[MermaidRenderBackend.DOCKER]
        assert h["consecutive_failures"] == 1
        assert h["latency_ms"] == 123.4
        assert "503" in h["last_error"]
        snap = c.get_health_snapshot()
        d = snap["backends"]["docker"]
        assert d["available"] is False
        assert d["circuit_open"] is False  # 1 次 < 阈值 2
        assert d["latency_ms"] == 123.4

    def test_circuit_open_in_snapshot(self):
        """熔断状态在快照中可见（运维端点数据源）。"""
        c = _fresh_client()
        for _ in range(c._BACKEND_FAIL_THRESHOLD):
            c._mark_probe(MermaidRenderBackend.MMDC, False, time.time(), error="exit 1")
        snap = c.get_health_snapshot()
        assert snap["backends"]["mmdc"]["circuit_open"] is True
        assert snap["fail_threshold"] == c._BACKEND_FAIL_THRESHOLD
        assert "urls" in snap

    def test_mmdc_health_via_sync_check(self, monkeypatch):
        """mmdc 后端：TTL 缓存命中走缓存；熔断同样生效。"""
        c = _fresh_client()
        c._backend_available[MermaidRenderBackend.MMDC] = False
        c._backend_cache_time[MermaidRenderBackend.MMDC] = time.time()
        import subprocess as _sp

        def _no_run(*a, **k):
            raise AssertionError("TTL 内不得重复探测 mmdc")

        monkeypatch.setattr(_sp, "run", _no_run)
        assert c.check_mmdc_available() is False

    def test_singleton_client_available(self):
        """全局单例可获取且具备快照接口（向后兼容）。"""
        c = get_mermaid_service_client()
        assert isinstance(c, MermaidServiceClient)
        snap = c.get_health_snapshot()
        assert set(snap["backends"].keys()) == {"docker", "railway", "mmdc"}


class TestHealthEndpoint:
    def test_charts_health_endpoint(self, monkeypatch):
        """GET /api/v1/charts/health 返回健康快照（200，零探测开销）。"""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from app.routers.charts import router

        c = _fresh_client()
        c._mark_probe(MermaidRenderBackend.DOCKER, True, time.time(), 42.0)
        monkeypatch.setattr(
            "app.services.ai.mermaid_service.get_mermaid_service_client", lambda: c
        )
        app = FastAPI()
        app.include_router(router)
        tc = TestClient(app)
        r = tc.get("/api/v1/charts/health")
        assert r.status_code == 200
        body = r.json()
        assert "docker" in body["backends"]
        assert body["backends"]["docker"]["latency_ms"] == 42.0
        assert body["urls"]["docker"].startswith("http")


# ============================================================
# B. 提示词注册表完整性（防提示词改写导致输出格式漂移）
# ============================================================


class TestChartPromptRegistryIntegrity:
    EXPECTED = {"flowchart", "gantt", "architecture", "labor", "comparison", "layout", "timeline"}

    def _resolve(self, entry: dict, key: str):
        import importlib

        name = entry.get(key)
        if not name:
            return None
        mod_name = "app.services.ai.prompts." + entry["source"].split("/")[-1].removesuffix(".py")
        mod = importlib.import_module(mod_name)
        return getattr(mod, name, None)

    def test_registry_complete(self):
        from app.services.ai.prompts.charts import CHART_REGISTRY

        assert set(CHART_REGISTRY.keys()) == self.EXPECTED
        for ct, entry in CHART_REGISTRY.items():
            assert entry.get("label"), f"{ct} 缺 label"
            assert entry.get("renderer"), f"{ct} 缺 renderer"

    @pytest.mark.parametrize("ct", ["flowchart", "gantt", "architecture", "labor", "comparison", "layout", "timeline"])
    def test_generate_prompts_exist_and_nonempty(self, ct):
        from app.services.ai.prompts.charts import CHART_REGISTRY

        entry = CHART_REGISTRY[ct]
        assert self._resolve(entry, "generate_prompt") is not None, (
            f"{ct}.generate_prompt 引用的常量不存在: {entry.get('generate_prompt')}"
        )
        # generate_user_prompt 仅 Mermaid 语法类注册（数据图类型显式置 None，
        # user 消息由模板函数构造）
        if entry.get("generate_user_prompt"):
            prompt = self._resolve(entry, "generate_user_prompt")
            assert prompt is not None, (
                f"{ct}.generate_user_prompt 引用的常量不存在: {entry.get('generate_user_prompt')}"
            )
            assert isinstance(prompt, str) and len(prompt) >= 80, f"{ct}.generate_user_prompt 内容过短"

    @pytest.mark.parametrize("ct", ["flowchart", "gantt"])
    def test_mermaid_prompts_mention_syntax(self, ct):
        """Mermaid 语法类提示词必须申明 mermaid 语法（防漂移为纯文字输出）。"""
        from app.services.ai.prompts.charts import CHART_REGISTRY

        prompt = self._resolve(CHART_REGISTRY[ct], "generate_prompt")
        assert "mermaid" in prompt.lower(), f"{ct} 生成提示词未申明 mermaid 语法"
