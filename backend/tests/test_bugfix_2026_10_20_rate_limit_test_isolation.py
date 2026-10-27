# -*- coding: utf-8 -*-
"""test_bugfix_2026_10_20_rate_limit_test_isolation.py — HTTP 限速器测试隔离（BUG-FIX）

背景（2026-10-20 全量套件实录）：
    TestClient 的所有请求共享标识符 `ip_testclient`（无认证注入、无 API Key、
    client.host 固定），全量套件单进程数千个 HTTP 请求远超 100 次/60s 滑动窗口，
    后运行的测试文件全部 429。`test_outline_hardening_2026_10_08.py` 的 8 个用例
    因此失败（`_mk_section` 断言 201 实得 429），单文件运行却全过——被早期误判为
    "跨文件 DB 污染"。

修复：`app.routers.rate_limiter.rate_limit_dependency` 在 `PYTEST_RUNNING=1`
（conftest 在任何 app 导入前设置）时直接放行；生产环境无该标记，行为不变。

本文件锁定：
    1. 测试环境下 bypass 判定为真、依赖直接放行（不消耗限速器配额）；
    2. bypass 关闭后限速逻辑仍然生效（第 N+1 次请求抛 429）；
    3. 生产环境（无 PYTEST_RUNNING）判定为假——防止误把生产也放过；
    4. 限速器服务层算法不受本次改动影响（allow 仍按 limit 拒绝）。
"""

import asyncio
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.routers import rate_limiter as rl  # noqa: E402


def _fake_request(host: str):
    """构造最小可用的 Starlette Request（覆盖依赖取标识符所需的字段）。"""
    from starlette.requests import Request

    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/v1/test",
        "raw_path": b"/api/v1/test",
        "root_path": "",
        "query_string": b"",
        "headers": [],
        "client": (host, 12345),
        "server": ("testserver", 80),
    }
    return Request(scope)


class TestBypassDecision:
    def test_bypassed_under_pytest_running(self, monkeypatch):
        """conftest 设置的 PYTEST_RUNNING=1 下判定为真（测试环境放行）。"""
        monkeypatch.setenv("PYTEST_RUNNING", "1")
        assert rl._rate_limit_bypassed_in_tests() is True

    def test_not_bypassed_without_marker(self, monkeypatch):
        """无 PYTEST_RUNNING（生产环境）判定为假——不得把生产限流也跳过。"""
        monkeypatch.delenv("PYTEST_RUNNING", raising=False)
        assert rl._rate_limit_bypassed_in_tests() is False


class TestDependencyBehavior:
    def test_passthrough_does_not_consume_quota(self, monkeypatch):
        """测试环境下依赖直接放行：连打 50 次不抛错，且限速器计数为零。"""
        monkeypatch.setenv("PYTEST_RUNNING", "1")
        host = "10.99.0.1"
        request = _fake_request(host)

        # Redis 后端的计数跨进程保留（key 过期时间 = window+1 秒），
        # 上一个用例/上一次套件跑留下的条目会让「空桶」前提不成立 → 先重置。
        limiter = rl.get_rate_limiter()
        asyncio.run(limiter.reset(f"ip_{host}"))

        async def _run():
            for _ in range(50):
                await rl.rate_limit_dependency(request)

        asyncio.run(_run())

        # 配额未被消耗（否则说明放行路径仍在计数，仍会撞墙）
        allowed, remaining = asyncio.run(
            limiter.allow(f"ip_{host}", limit=100, window=60)
        )
        assert allowed is True
        assert remaining >= 99

    def test_enforces_limit_when_bypass_disabled(self, monkeypatch):
        """bypass 关闭（模拟生产）时限速照常生效：第 3 次请求抛 429。"""
        monkeypatch.setattr(rl, "_rate_limit_bypassed_in_tests", lambda: False)
        host = "10.99.0.2"  # 独立标识符，避免与其它用例共享计数
        request = _fake_request(host)

        # 同上：Redis 计数跨进程保留，先清空该标识符的历史条目。
        asyncio.run(rl.get_rate_limiter().reset(f"ip_{host}"))

        async def _run():
            # limit=2：前 2 次放行
            await rl.rate_limit_dependency(request, limit=2, window=60)
            await rl.rate_limit_dependency(request, limit=2, window=60)
            with pytest.raises(HTTPException) as exc:
                await rl.rate_limit_dependency(request, limit=2, window=60)
            assert exc.value.status_code == 429

        asyncio.run(_run())

    def test_identifier_falls_back_to_ip(self):
        """标识符口径不变：无用户/API Key 时回落 ip_{host}（与修复前一致）。"""
        request = _fake_request("10.99.0.3")
        assert rl.get_identifier_from_request(request) == "ip_10.99.0.3"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
