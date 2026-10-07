import logging
import time

from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send


logger = logging.getLogger(__name__)

LATENCY_BUDGETS = {
    "/api/v1/projects/{project_id}/ai": 30.0,
    "/api/v1/projects/{project_id}/sections/generate-content": 600.0,
    "/api/v1/projects/{project_id}/sections/generate-sse": 300.0,
    "/api/v1/projects/{project_id}/export": 60.0,
}

# Add compatibility paths without /v1 for testing and older setups
LATENCY_BUDGETS_COMPAT = {
    "/api/projects/{project_id}/ai": 30.0,
    "/api/projects/{project_id}/sections/generate-content": 600.0,
    "/api/projects/{project_id}/sections/generate-sse": 300.0,
    "/api/projects/{project_id}/export": 60.0,
}

DEFAULT_BUDGET = 5.0


def _get_route_category(route_path: str) -> str:
    if "/ai" in route_path:
        return "AI"
    if "/export" in route_path:
        return "export"
    return "normal"


def _match_pattern(path_parts: list[str], pattern_parts: list[str]) -> bool:
    """Check if path matches pattern, treating {param} as wildcard."""
    if len(path_parts) < len(pattern_parts):
        return False
    for i, part in enumerate(pattern_parts):
        if part.startswith("{") and part.endswith("}"):
            continue
        if part != path_parts[i]:
            return False
    return True


def _get_latency_budget(route_path: str) -> float:
    """Get latency budget for a given route path, supporting both /api/v1/ and /api/ prefixes."""
    for pattern, budget in LATENCY_BUDGETS.items():
        pattern_parts = pattern.strip("/").split("/")
        path_parts = route_path.strip("/").split("/")
        if _match_pattern(path_parts, pattern_parts):
            return budget

    for pattern, budget in LATENCY_BUDGETS_COMPAT.items():
        pattern_parts = pattern.strip("/").split("/")
        path_parts = route_path.strip("/").split("/")
        if _match_pattern(path_parts, pattern_parts):
            return budget

    return DEFAULT_BUDGET


class LatencyBudgetMiddleware:
    """ASGI middleware that tracks latency without buffering streaming responses.

    Uses a pass-through wrapper that only intercepts the final body message
    (more_body=False) to inject the X-Response-Time header. This preserves
    true streaming for SSE endpoints while still measuring latency.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive)
        start_time = time.perf_counter()
        response_started = False

        async def send_wrapper(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            if not response_started:
                # Response headers were never sent — inject timing before error response
                pass
            raise
        finally:
            process_time = time.perf_counter() - start_time
            process_time_ms = process_time * 1000

            route_path = request.url.path
            category = _get_route_category(route_path)
            budget = _get_latency_budget(route_path)

            if process_time > budget:
                logger.warning(
                    "延迟预算超限 | 路由: %s | 分类: %s | 耗时: %.2fms (%.2fs) | 预算: %.2fs | 状态码: %d",
                    route_path,
                    category,
                    process_time_ms,
                    process_time,
                    budget,
                    200 if response_started else 500,
                )
            elif process_time > budget * 0.8:
                logger.info(
                    "接近延迟预算 | 路由: %s | 分类: %s | 耗时: %.2fms (%.2fs) | 预算: %.2fs | 状态码: %d",
                    route_path,
                    category,
                    process_time_ms,
                    process_time,
                    budget,
                    200 if response_started else 500,
                )
