"""2026-09-15 全面安全审查回归测试。

覆盖本轮修复的两类高危问题：
  A. SSRF 防护（app.utils.ssrf.is_safe_http_url）：服务端出站图片下载必须拒绝
     内网/保留地址，防止服务端请求伪造。
  B. 认证生产加固（app.config）：ENVIRONMENT=production 且 AUTH_ENABLED=false
     时必须拒绝启动。
"""
import importlib
import socket

import pytest

from app.utils import ssrf


# ===================== A. SSRF 防护 =====================

def test_blocks_private_ip_literals():
    """内网/保留地址字面量一律拒绝。"""
    for url in (
        "http://127.0.0.1/x.png",
        "https://127.0.0.1:9000/a",
        "http://169.254.169.254/latest/meta-data/",  # 云元数据
        "http://10.0.0.5/img.png",
        "http://192.168.1.10/x",
        "http://172.16.0.1/x",
        "http://172.31.255.255/x",
        "http://[::1]/x.png",
        "http://0.0.0.0/x",
    ):
        assert ssrf.is_safe_http_url(url) is False, url


def test_allows_public_ip_literals():
    """公网地址字面量放行。"""
    for url in (
        "http://8.8.8.8/x.png",
        "https://1.1.1.1/a",
        "http://93.184.216.34/x",  # example.com 历史 IP
    ):
        assert ssrf.is_safe_http_url(url) is True, url


def test_rejects_non_http_schemes():
    """仅允许 http/https，拦掉 file/ftp/javascript/gopher 等。"""
    for url in (
        "file:///etc/passwd",
        "ftp://example.com/x",
        "javascript:alert(1)",
        "gopher://127.0.0.1:6379/_",
        "data:text/html,<script>alert(1)</script>",
        "http:///no-host",
        "",
        None,
    ):
        assert ssrf.is_safe_http_url(url) is False, repr(url)


def test_rejects_unresolvable_host(monkeypatch):
    """DNS 解析失败应 fail-closed（拒绝）。"""
    def _boom(*a, **k):
        raise socket.gaierror("no such host")

    monkeypatch.setattr(socket, "getaddrinfo", _boom)
    assert ssrf.is_safe_http_url("http://nonexistent.invalid/x.png") is False


def test_resolves_hostname_and_blocks_internal(monkeypatch):
    """域名解析到内网 IP 时拒绝（覆盖域名型 SSRF）。"""
    def _resolve_internal(host, *a, **k):
        # 任何主机名都解析到 127.0.0.1
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", _resolve_internal)
    assert ssrf.is_safe_http_url("http://internal-service/x.png") is False


def test_resolves_hostname_and_allows_public(monkeypatch):
    """域名解析到公网 IP 时放行。"""
    def _resolve_public(host, *a, **k):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", _resolve_public)
    assert ssrf.is_safe_http_url("http://cdn.example.com/logo.png") is True


# ===================== B. 认证生产加固 =====================

def test_production_requires_auth(monkeypatch):
    """ENVIRONMENT=production 且 AUTH_ENABLED=false 必须拒绝启动。"""
    import app.config as cfg

    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("AUTH_ENABLED", "false")
    try:
        with pytest.raises(RuntimeError):
            importlib.reload(cfg)
    finally:
        # 复原默认开发环境，避免污染其他用例
        monkeypatch.setenv("ENVIRONMENT", "development")
        monkeypatch.setenv("AUTH_ENABLED", "false")
        importlib.reload(cfg)


def test_production_with_auth_ok(monkeypatch):
    """ENVIRONMENT=production 且 AUTH_ENABLED=true 不应拒绝启动。"""
    import app.config as cfg

    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("AUTH_ENABLED", "true")
    try:
        importlib.reload(cfg)  # 不应抛异常
    finally:
        monkeypatch.setenv("ENVIRONMENT", "development")
        monkeypatch.setenv("AUTH_ENABLED", "false")
        importlib.reload(cfg)
