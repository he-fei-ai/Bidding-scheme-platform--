"""服务端出站请求（SSRF）防护工具。

问题背景
--------
多处逻辑会在「服务端」主动发起 HTTP(S) 请求去拉取外部资源（例如把图片 URL
下载后嵌入 DOCX）。若被拉取的 URL 来自用户可控内容（富文本中的 <img>、导入文档的
外链图片、或用户自行配置的 AI 图片提供商返回的 URL），攻击者可利用本服务作为跳板
访问内网/元数据地址（如 http://169.254.169.254、http://localhost:6379），造成
SSRF（服务端请求伪造）。

修复策略
--------
`is_safe_http_url(url)` 在真正发请求之前做「出站白名单」校验：
  1. 仅允许 http / https 协议（拦掉 file://、ftp://、gopher:// 等）；
  2. 解析主机名，拒绝任何落到以下范围的地址：
       - 环回（127.0.0.0/8、::1）
       - 链路本地（169.254.0.0/16、fe80::/10）——含云元数据 169.254.169.254
       - 私有网段（10/8、172.16/12、192.168/16、fc00::/7）
       - CGNAT（100.64.0.0/10）、广播/多播/保留/未指定地址
  3. 解析失败直接拒绝（fail-closed）。

已知局限（需在调用侧继续加固）：
  本函数采用「先解析校验、再交给 httpx 连接」的经典做法，存在极小的 DNS 重绑定
  （TOCTOU）窗口。若未来要求更高，应改用「IP 钉扎」的自定义 httpx transport
  （解析后固定连接该 IP，并禁用跨主机重定向）。当前实现已能拦截绝大多数 SSRF 场景。
"""
from __future__ import annotations

import ipaddress
import logging
import socket
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# 必须禁止服务端访问的地址范围
_BLOCKED_NETWORKS: tuple[ipaddress._BaseNetwork, ...] = (
    ipaddress.ip_network("0.0.0.0/8"),          # 本网络（非具体主机）
    ipaddress.ip_network("10.0.0.0/8"),         # RFC1918 私有 A
    ipaddress.ip_network("127.0.0.0/8"),        # 环回
    ipaddress.ip_network("169.254.0.0/16"),     # 链路本地（云元数据 169.254.169.254）
    ipaddress.ip_network("172.16.0.0/12"),      # RFC1918 私有 B
    ipaddress.ip_network("192.168.0.0/16"),     # RFC1918 私有 C
    ipaddress.ip_network("100.64.0.0/10"),      # CGNAT
    ipaddress.ip_network("::1/128"),            # IPv6 环回
    ipaddress.ip_network("fc00::/7"),           # IPv6 唯一本地地址（ULA）
    ipaddress.ip_network("fe80::/10"),          # IPv6 链路本地
    ipaddress.ip_network("64:ff9b::/96"),       # IPv4/IPv6 转换（ well-known prefix）
)


def _is_blocked_ip(ip: str) -> bool:
    """判断一个 IP 字面量是否属于禁止访问范围。无法解析一律视为禁止。"""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True

    # 处理 IPv4 映射的 IPv6 地址（如 ::ffff:127.0.0.1）
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped

    for net in _BLOCKED_NETWORKS:
        if addr.version == net.version and addr in net:
            return True

    # 多播 / 保留 / 未指定地址同样禁止
    if addr.is_multicast or addr.is_reserved or addr.is_unspecified:
        return True
    return False


def is_safe_http_url(url: str, *, allow_private: bool = False) -> bool:
    """判断一个 URL 是否允许服务端发起出站请求。

    Args:
        url: 待校验的 URL。
        allow_private: 是否允许内网地址（默认 False，即禁止）。仅供受信任的
            内部场景使用，常规外部资源下载务必保持默认。

    Returns:
        True 表示可安全请求；False 表示应直接拒绝（fail-closed）。
    """
    if not url or not isinstance(url, str):
        return False

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return False

    host = (parsed.hostname or "").strip().lower()
    if not host:
        return False

    # 解析主机名到所有 IP（同时覆盖 IP 字面量与域名）
    try:
        infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, UnicodeError, ValueError, OSError) as exc:
        logger.warning("SSRF 校验：主机名解析失败，拒绝请求 %s: %s", host, exc)
        return False

    for info in infos:
        ip = info[4][0]
        # 去掉 IPv6 的 scope id（如 %eth0）
        if "%" in ip:
            ip = ip.split("%", 1)[0]
        if allow_private:
            continue
        if _is_blocked_ip(ip):
            logger.warning("SSRF 校验：拒绝内网/保留地址 %s -> %s", host, ip)
            return False
    return True
