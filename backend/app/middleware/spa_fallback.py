"""
SPA fallback middleware.

For any request that doesn't match an existing route, serve index.html
so that the client-side router can handle the path.
"""

import logging
from pathlib import Path

from starlette.types import ASGIApp, Receive, Scope, Send


logger = logging.getLogger(__name__)


class SPAIndexFallbackMiddleware:
    """
    ASGI middleware that serves index.html as a 404 fallback for SPA routes.

    Works by buffering the response from the inner app and intercepting
    404 responses for non-API, non-asset paths, replacing them with
    index.html instead.
    """

    def __init__(self, app: ASGIApp, index_html_path: Path) -> None:
        self.app = app
        self.index_html_path = index_html_path

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        method = scope.get("method", "GET")

        # Only handle GET requests for SPA navigation
        if method != "GET":
            await self.app(scope, receive, send)
            return

        # Skip API routes and asset paths — let them fall through
        if path.startswith("/api/") or path.startswith("/assets/"):
            await self.app(scope, receive, send)
            return

        # Buffer all messages from the inner app
        sent: list[dict] = []

        async def receive_wrapper() -> dict:
            return await receive()

        async def send_wrapper(message: dict) -> None:
            sent.append(message)

        await self.app(scope, receive_wrapper, send_wrapper)

        # Check if the inner app returned a 404
        if sent and sent[0].get("type") == "http.response.start":
            status = sent[0].get("status", 200)
            if status == 404:
                # Serve index.html instead
                await self._send_index_html(send)
                return

        # Not a 404 — replay the buffered messages
        for message in sent:
            await send(message)

    async def _send_index_html(self, send: Send) -> None:
        content = self.index_html_path.read_bytes()
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [
                    (b"content-type", b"text/html; charset=utf-8"),
                    (b"content-length", str(len(content)).encode()),
                ],
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": content,
            }
        )
