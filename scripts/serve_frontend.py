#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bid Assistant 前端静态服务器 + 反向代理（Node 不可用时的替代方案）

功能：
  1. 从 frontend/dist 提供静态文件（前端打包产物）
  2. 将 /api/* 以及 /docs、/redoc、/openapi.json 等请求反向代理到后端
     （支持普通 HTTP、SSE 流式响应、WebSocket 升级）
  3. SPA 回退：对不存在的非文件、无扩展名路径返回 index.html

用法：
  python serve_frontend.py [frontend_dir] [listen_port] [backend_url]

示例：
  python serve_frontend.py ../frontend/dist 5175 http://127.0.0.1:8000

说明：
  `python -m http.server` 只能提供静态文件，无法把 /api 请求转发给后端，
  导致前端健康检查 /api/v1/health 返回 404，页面误报"后端服务不可用"。
  本脚本在静态服务之上增加了反向代理，从而无需 Node.js 也能完整使用平台。
"""

import os
import sys
import socket
import threading
import http.server
import http.client
import urllib.parse
import socketserver

# ----------------------------------------------------------------------------
# 参数解析（带默认值）
# ----------------------------------------------------------------------------
_HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_FRONTEND = os.path.normpath(os.path.join(_HERE, "..", "frontend", "dist"))
DEFAULT_PORT = int(os.getenv("FRONTEND_PORT", "5175"))
DEFAULT_BACKEND = os.getenv("BACKEND_URL", "http://127.0.0.1:8000")

FRONTEND_DIR = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_FRONTEND
LISTEN_PORT = int(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_PORT
BACKEND_URL = sys.argv[3] if len(sys.argv) > 3 else DEFAULT_BACKEND

_parsed = urllib.parse.urlparse(BACKEND_URL)
BACKEND_HOST = _parsed.hostname or "127.0.0.1"
BACKEND_PORT = _parsed.port or 8000

API_PREFIX = "/api/"
DOC_PATHS = {"/docs", "/redoc", "/openapi.json", "/api/docs", "/api/redoc", "/api/openapi.json"}
# WebSocket 升级需要保留的逐跳头之外的常规头
HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
              "te", "trailers", "transfer-encoding", "upgrade"}


def _is_proxy_path(path: str) -> bool:
    p = path.split("?", 1)[0].split("#", 1)[0]
    if p.startswith(API_PREFIX):
        return True
    if p in DOC_PATHS:
        return True
    if p.startswith("/docs") or p.startswith("/redoc") or p.startswith("/openapi.json"):
        return True
    return False


class FrontendHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=FRONTEND_DIR, **kwargs)

    # ---- 让日志带前缀，方便排查 ----
    def log_message(self, fmt, *args):
        try:
            sys.stderr.write("[serve_frontend] %s - %s\n" % (self.address_string(), fmt % args))
        except Exception:
            pass

    # ---- GET / HEAD ----
    def do_GET(self):
        if _is_proxy_path(self.path):
            return self._proxy_http()
        return self._maybe_spa_fallback(super().do_GET)

    def do_HEAD(self):
        if _is_proxy_path(self.path):
            return self._proxy_http()
        return self._maybe_spa_fallback(super().do_HEAD)

    # ---- 写操作：只有 /api 才代理，其余静态路径返回 404 ----
    def do_POST(self):
        if _is_proxy_path(self.path):
            return self._proxy_http()
        self.send_error(404)

    def do_PUT(self):
        if _is_proxy_path(self.path):
            return self._proxy_http()
        self.send_error(404)

    def do_DELETE(self):
        if _is_proxy_path(self.path):
            return self._proxy_http()
        self.send_error(404)

    def do_PATCH(self):
        if _is_proxy_path(self.path):
            return self._proxy_http()
        self.send_error(404)

    def do_OPTIONS(self):
        if _is_proxy_path(self.path):
            return self._proxy_http()
        self.send_error(404)

    # ---- SPA 回退：无扩展名且文件不存在时返回 index.html ----
    def _maybe_spa_fallback(self, original):
        path = self.path.split("?", 1)[0].split("#", 1)[0]
        ext = os.path.splitext(path)[1]
        rel = path.lstrip("/")
        file_path = os.path.join(FRONTEND_DIR, rel)
        if not ext and not os.path.isfile(file_path):
            self.path = "/index.html"
        return original()

    # ---- HTTP 反向代理（含 SSE 流式转发） ----
    def _proxy_http(self):
        if self.headers.get("Upgrade", "").lower() == "websocket":
            return self._proxy_ws()

        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length) if length else None

        fwd_headers = {}
        for k, v in self.headers.items():
            if k.lower() in HOP_BY_HOP:
                continue
            fwd_headers[k] = v
        # 修正 Host，避免后端按 Host 路由出错
        fwd_headers["Host"] = f"{BACKEND_HOST}:{BACKEND_PORT}"

        method = self.command
        target = self.path  # 已包含 query

        try:
            conn = http.client.HTTPConnection(BACKEND_HOST, BACKEND_PORT, timeout=3600)
            conn.request(method, target, body=body, headers=fwd_headers)
            resp = conn.getresponse()
        except Exception as exc:  # 后端未启动 / 连接被拒
            self.send_error(502, "Backend unreachable: %s" % exc)
            return

        # 转发状态码与响应头（去掉逐跳头与 transfer-encoding，由本机重新分块）
        self.send_response(resp.status)
        for k, v in resp.getheaders():
            if k.lower() in HOP_BY_HOP:
                continue
            self.send_header(k, v)
        self.end_headers()

        # 流式写出响应体（SSE 不会被缓冲，可实时到达前端）
        try:
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        except Exception:
            pass
        finally:
            try:
                conn.close()
            except Exception:
                pass

    # ---- WebSocket 反向代理（盲转发 TCP 字节，无需解析帧） ----
    def _proxy_ws(self):
        try:
            backend_sock = socket.create_connection((BACKEND_HOST, BACKEND_PORT), timeout=10)
        except Exception as exc:
            self.send_error(502, "Backend unreachable: %s" % exc)
            return

        # 构造转发给后端的升级请求
        lines = ["%s %s HTTP/1.1" % (self.command, self.path),
                 "Host: %s:%d" % (BACKEND_HOST, BACKEND_PORT)]
        for k, v in self.headers.items():
            if k.lower() in ("proxy-connection", "proxy-authorization"):
                continue
            lines.append("%s: %s" % (k, v))
        # 确保 Upgrade/Connection 头存在
        if not any(k.lower() == "upgrade" for k, _ in self.headers.items()):
            lines.append("Upgrade: websocket")
        if not any(k.lower() == "connection" for k, _ in self.headers.items()):
            lines.append("Connection: Upgrade")
        req = "\r\n".join(lines) + "\r\n\r\n"

        try:
            backend_sock.sendall(req.encode())
        except Exception:
            backend_sock.close()
            self.send_error(502)
            return

        # 读取后端握手响应，直到空行
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = backend_sock.recv(4096)
            if not chunk:
                backend_sock.close()
                self.send_error(502)
                return
            buf += chunk
        header_part, _, rest = buf.partition(b"\r\n\r\n")

        # 把后端握手响应原样回给客户端
        try:
            self.connection.sendall(header_part + b"\r\n\r\n")
            if rest:
                self.connection.sendall(rest)
        except Exception:
            backend_sock.close()
            return

        # 双向盲转发
        self.close_connection = True
        client_sock = self.connection
        backend_sock.setblocking(False)
        client_sock.setblocking(False)

        def _pump(src, dst):
            try:
                while True:
                    data = src.recv(65536)
                    if not data:
                        break
                    dst.sendall(data)
            except Exception:
                pass

        t1 = threading.Thread(target=_pump, args=(client_sock, backend_sock), daemon=True)
        t2 = threading.Thread(target=_pump, args=(backend_sock, client_sock), daemon=True)
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        try:
            backend_sock.close()
        except Exception:
            pass


class ThreadingServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    if not os.path.isdir(FRONTEND_DIR):
        sys.stderr.write(
            "[serve_frontend] 前端目录不存在: %s\n"
            "请先执行 `npm run build`（需 Node.js）生成 dist，或确认 frontend/dist 已存在。\n" % FRONTEND_DIR
        )
        sys.exit(1)

    server = ThreadingServer(("127.0.0.1", LISTEN_PORT), FrontendHandler)
    sys.stderr.write(
        "[serve_frontend] 前端静态服务已启动: http://127.0.0.1:%d/\n" % LISTEN_PORT
    )
    sys.stderr.write(
        "[serve_frontend] API 反向代理目标: %s\n" % BACKEND_URL
    )
    sys.stderr.write("[serve_frontend] 按 Ctrl+C 停止。\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        sys.stderr.write("\n[serve_frontend] 已停止。\n")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
