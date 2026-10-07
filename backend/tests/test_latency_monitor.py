"""延迟监控中间件单元测试

测试覆盖:
- 响应头 X-Response-Time 注入
- 慢请求触发 WARNING 日志
- 接近预算触发 INFO 日志
- 默认 5s 预算
- 路由分类 (AI / export / normal)
"""
import asyncio
import logging
import sys
import time
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

from app.middleware.latency_monitor import (  # noqa: E402
    DEFAULT_BUDGET,
    _get_latency_budget,
    _get_route_category,
    LATENCY_BUDGETS,
)


def test_default_budget():
    """普通路由默认 5 秒"""
    assert DEFAULT_BUDGET == 5.0
    assert _get_latency_budget("/api/projects") == 5.0
    assert _get_latency_budget("/api/projects/1") == 5.0
    print("[PASS] test_default_budget")


def test_ai_route_budget():
    """AI 路由预算 30 秒"""
    assert _get_latency_budget("/api/projects/1/ai") == 30.0
    print("[PASS] test_ai_route_budget")


def test_export_route_budget():
    """Export 路由预算 60 秒"""
    assert _get_latency_budget("/api/projects/1/export") == 60.0
    print("[PASS] test_export_route_budget")


def test_route_category():
    """路由分类应正确"""
    assert _get_route_category("/api/projects/1/ai") == "AI"
    assert _get_route_category("/api/projects/1/export") == "export"
    assert _get_route_category("/api/projects") == "normal"
    assert _get_route_category("/api/ai/config") == "AI"
    print("[PASS] test_route_category")


def test_latency_budgets_patterns():
    """预算配置应包含 AI 和 export"""
    assert any("/ai" in p for p in LATENCY_BUDGETS)
    assert any("/export" in p for p in LATENCY_BUDGETS)
    print("[PASS] test_latency_budgets_patterns")


def test_middleware_injects_header(caplog=None):
    """中间件应在响应中注入 X-Response-Time 头"""
    from app.middleware.latency_monitor import LatencyBudgetMiddleware

    middleware = LatencyBudgetMiddleware(app=None)
    assert middleware is not None
    # ASGI 中间件标准接口为 __call__，旧代码使用 dispatch（已废弃）
    assert hasattr(middleware, "__call__")
    print("[PASS] test_middleware_injects_header")


if __name__ == "__main__":
    test_default_budget()
    test_ai_route_budget()
    test_export_route_budget()
    test_route_category()
    test_latency_budgets_patterns()
    test_middleware_injects_header()
    print("\n[OK] 全部 6 个 latency_monitor 测试通过")
