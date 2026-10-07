"""
Mermaid HTTP Service Client

提供 3 种渲染后端：
  1. Docker mermaid-service（本地容器）
  2. Railway Cloud（远程托管）
  3. mmdc CLI（本地 Node.js）

自动检测服务可用性，渲染失败时抛出明确异常。
"""

from __future__ import annotations

import asyncio
from enum import Enum
from io import BytesIO
import json
import logging
import os
import subprocess
import sys
import tempfile
import threading
import time

import httpx

from ...utils.proxy_config import build_httpx_proxy_settings


logger = logging.getLogger(__name__)

# BUG-FIX 2026-09-18（中）：fire-and-forget 的 asyncio 任务必须被强引用。
# CPython 的已知陷阱 —— `asyncio.create_task()` 返回的句柄若没有活跃强引用，
# 事件循环只持有**弱引用**，任务可能在协程执行到一半时被垃圾回收掉，
# 表现为"关闭旧 AsyncClient 的任务时灵时不灵"，且没有任何报错。
# 这里统一登记，任务结束时自动 discard（与 task_registry / main.py 同一套做法）。
_PENDING_CLOSE_TASKS: set = set()


class MermaidRenderBackend(Enum):
    """支持的渲染后端"""

    DOCKER = "docker"
    RAILWAY = "railway"
    MMDC = "mmdc"


class MermaidRenderError(Exception):
    """Mermaid 渲染失败的明确异常"""

    def __init__(self, message: str, backend: str = "", code: str = ""):
        self.message = message
        self.backend = backend
        self.code = code
        super().__init__(f"[{backend}] {message}")


def _kill_process_tree(proc: subprocess.Popen) -> None:
    """递归终止进程及其全部子进程（mmdc → puppeteer → Chrome）

    BUG-FIX-36 新增：proc.kill() 在 Windows 上只终止主进程，mmdc（Node.js）
    派生的 puppeteer 控制的 Chrome/Chromium 孙进程不会被回收，造成僵尸
    浏览器进程持续占用内存。Windows 用 taskkill /T /F 递归强杀进程树。
    """
    try:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                capture_output=True,
                timeout=5,
            )
        else:
            import signal
            import os as _os
            try:
                _os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, AttributeError):
                proc.kill()
    except Exception:
        try:
            proc.kill()
        except Exception as _e:
            logger.debug("[silent-except] mermaid_service.py: line 76 - %s", _e)


class MermaidServiceClient:
    """Mermaid HTTP 服务客户端

    支持 3 种渲染后端，自动检测可用性并降级。
    优化 V7.5：
      - 后端可用性缓存 TTL（60 秒自动失效）
      - 同步渲染方法（兼容同步调用场景）
      - 单例复用强化（通过 get_mermaid_service_client 获取）

    使用示例:
        client = MermaidServiceClient()
        png_bytes = await client.render_mermaid_via_http(
            code="graph TD; A-->B",
            theme="neutral",
            scale=2,
        )
        # 同步调用
        png_bytes = client.render_mermaid_sync(
            code="graph TD; A-->B",
            theme="neutral",
            scale=2,
        )
    """

    # 后端可用性缓存 TTL（秒）
    _BACKEND_CACHE_TTL: float = 60.0

    def __init__(
        self,
        docker_url: str = "",
        railway_url: str = "",
        mmdc_path: str = "",
        timeout: int = 30,
    ):
        """
        Args:
            docker_url: Docker mermaid-service 地址，默认 http://localhost:8080
            railway_url: Railway Cloud 地址
            mmdc_path: mmdc CLI 路径，默认自动检测
            timeout: HTTP 请求超时（秒）
        """
        self._docker_url = (
            docker_url or os.environ.get("MERMAID_SERVICE_URL", "http://localhost:8080")
        ).rstrip("/")
        self._railway_url = (railway_url or os.environ.get("MERMAID_RAILWAY_URL", "")).rstrip("/")
        self._mmdc_path = mmdc_path or ""
        self._timeout = timeout
        self._http_client: httpx.AsyncClient | None = None
        self._client_loop: asyncio.AbstractEventLoop | None = None
        # 按事件循环缓存客户端：{id(loop): (loop, client, last_used)}
        # 注意：元组结构为 (loop, client, last_used)，修复此前 (now, client)
        # 与读取端 (loop, client) 不一致导致的 AttributeError
        self._clients_by_loop: dict[
            int, tuple[asyncio.AbstractEventLoop, httpx.AsyncClient, float]
        ] = {}
        self._clients_lock = threading.Lock()

        # 内部状态缓存（带时间戳）
        self._backend_available: dict[MermaidRenderBackend, bool | None] = {
            MermaidRenderBackend.DOCKER: None,
            MermaidRenderBackend.RAILWAY: None,
            MermaidRenderBackend.MMDC: None,
        }
        self._backend_cache_time: dict[MermaidRenderBackend, float] = {
            MermaidRenderBackend.DOCKER: 0.0,
            MermaidRenderBackend.RAILWAY: 0.0,
            MermaidRenderBackend.MMDC: 0.0,
        }

    # ---------------------------------------------------------------
    # 客户端生命周期
    # ---------------------------------------------------------------
    # 跨事件循环修复：
    # 本客户端为全局单例，但渲染协程可能经 _safe_asyncio_run/asyncio.run()
    # 在不同线程、不同（一次性）事件循环中执行。httpx.AsyncClient 绑定
    # 创建它的循环，跨循环复用会触发 "Event loop is closed" /
    # "attached to a different loop" 异常，导致并行图表渲染全部失败。
    # 因此按事件循环维度缓存客户端，并在循环关闭后自动清理。
    # BUG-H-03 修复：添加空闲超时清理，避免长期持有客户端导致内存泄漏
    _CLIENT_IDLE_TIMEOUT = 300.0  # 5 分钟无使用后自动关闭客户端

    async def _get_client(self) -> httpx.AsyncClient:
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:  # 理论上不会发生（仅在协程内调用）
            current_loop = None

        now = time.time()
        if current_loop is not None:
            key = id(current_loop)
            with self._clients_lock:
                entry = self._clients_by_loop.get(key)
                if entry is not None and entry[0] is current_loop:
                    _, client, last_used = entry
                    # BUG-H-03 修复：检查空闲超时，过期则关闭并丢弃
                    elapsed = now - last_used
                    if elapsed > self._CLIENT_IDLE_TIMEOUT:
                        try:
                            await client.aclose()
                        except Exception as _e:
                            logger.debug("[silent-except] mermaid_service.py: line 179 - %s", _e)
                        self._clients_by_loop.pop(key, None)
                    else:
                        self._clients_by_loop[key] = (current_loop, client, now)  # 更新使用时间
                        self._http_client = client
                        self._client_loop = current_loop
                        return client

                # 顺带清理已关闭循环的过期客户端，防止泄漏
                # BUG-FIX-37 修复：原只 pop 不 aclose，一次性循环（每图一个
                # asyncio.run）的 AsyncClient 连接池/socket 依赖 GC 兜底，
                # 批量渲染时产生大量 "Unclosed AsyncClient" 警告。改为
                # 后台任务尝试 aclose（client 绑定旧 loop，失败则静默放行 GC）。
                for k in list(self._clients_by_loop.keys()):
                    old_loop, old_client, _old_ts = self._clients_by_loop[k]
                    if old_loop.is_closed():
                        self._clients_by_loop.pop(k, None)
                        if old_client is not None:
                            async def _aclose(c=old_client):
                                try:
                                    await c.aclose()
                                except Exception as _e:
                                    logger.debug("[silent-except] mermaid_service.py: line 201 - %s", _e)
                            try:
                                _close_task = asyncio.get_running_loop().create_task(_aclose())
                                # 强引用登记，防止被 GC 中途回收（见模块顶部说明）
                                _PENDING_CLOSE_TASKS.add(_close_task)
                                _close_task.add_done_callback(_PENDING_CLOSE_TASKS.discard)
                            except RuntimeError:
                                pass

                client = httpx.AsyncClient(
                    timeout=httpx.Timeout(self._timeout),
                    follow_redirects=True,
                    **build_httpx_proxy_settings(),
                )
                self._clients_by_loop[key] = (current_loop, client, now)
                self._http_client = client
                self._client_loop = current_loop
                return client

        # 无 running loop 的兜底路径（保持旧行为）
        if self._http_client is None or (
            self._client_loop is not None and self._client_loop.is_closed()
        ):
            self._http_client = httpx.AsyncClient(
                timeout=httpx.Timeout(self._timeout),
                follow_redirects=True,
                **build_httpx_proxy_settings(),
            )
            self._client_loop = current_loop
        return self._http_client

    async def close(self):
        with self._clients_lock:
            clients = [c for _, c, _ in self._clients_by_loop.values()]
            self._clients_by_loop.clear()
            legacy = self._http_client
            self._http_client = None
            self._client_loop = None
        # 只在客户端绑定的循环已关闭时跳过 aclose（避免跨循环 await 异常）
        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None
        for client in clients:
            try:
                await client.aclose()
            except Exception as _e:
                logger.debug("[silent-except] mermaid_service.py: line 245 - %s", _e)
        if legacy is not None and legacy not in clients:
            try:
                await legacy.aclose()
            except Exception as _e:
                logger.debug("[silent-except] mermaid_service.py: line 250 - %s", _e)
        _ = running_loop  # 保留引用供未来调试

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.close()

    # ---------------------------------------------------------------
    # 服务可用性检测
    # ---------------------------------------------------------------

    def _is_cache_valid(self, backend: MermaidRenderBackend) -> bool:
        """检查后端可用性缓存是否仍在 TTL 有效期内"""
        if self._backend_available[backend] is None:
            return False
        elapsed = __import__("time").time() - self._backend_cache_time[backend]
        return elapsed < self._BACKEND_CACHE_TTL

    async def check_docker_available(self) -> bool:
        """检测 Docker mermaid-service 是否可达（带 TTL 缓存）"""
        if self._is_cache_valid(MermaidRenderBackend.DOCKER):
            return self._backend_available[MermaidRenderBackend.DOCKER]

        if not self._docker_url:
            self._backend_available[MermaidRenderBackend.DOCKER] = False
            self._backend_cache_time[MermaidRenderBackend.DOCKER] = __import__("time").time()
            return False

        try:
            client = await self._get_client()
            resp = await client.get(f"{self._docker_url}/health", timeout=5)
            available = resp.status_code == 200
            self._backend_available[MermaidRenderBackend.DOCKER] = available
            self._backend_cache_time[MermaidRenderBackend.DOCKER] = __import__("time").time()
            if available:
                logger.info(
                    "MermaidServiceClient: Docker mermaid-service 可用 (%s)", self._docker_url
                )
            else:
                logger.info(
                    "MermaidServiceClient: Docker mermaid-service 不可用 (HTTP %s)",
                    resp.status_code,
                )
            return available
        except Exception as e:
            self._backend_available[MermaidRenderBackend.DOCKER] = False
            self._backend_cache_time[MermaidRenderBackend.DOCKER] = __import__("time").time()
            logger.debug("MermaidServiceClient: Docker mermaid-service 检测失败: %s", e)
            return False

    async def check_railway_available(self) -> bool:
        """检测 Railway Cloud 服务是否可达（带 TTL 缓存）"""
        if self._is_cache_valid(MermaidRenderBackend.RAILWAY):
            return self._backend_available[MermaidRenderBackend.RAILWAY]

        if not self._railway_url:
            self._backend_available[MermaidRenderBackend.RAILWAY] = False
            self._backend_cache_time[MermaidRenderBackend.RAILWAY] = __import__("time").time()
            return False

        try:
            client = await self._get_client()
            resp = await client.get(f"{self._railway_url}/health", timeout=5)
            available = resp.status_code == 200
            self._backend_available[MermaidRenderBackend.RAILWAY] = available
            self._backend_cache_time[MermaidRenderBackend.RAILWAY] = __import__("time").time()
            if available:
                logger.info("MermaidServiceClient: Railway Cloud 可用 (%s)", self._railway_url)
            return available
        except Exception as e:
            self._backend_available[MermaidRenderBackend.RAILWAY] = False
            self._backend_cache_time[MermaidRenderBackend.RAILWAY] = __import__("time").time()
            logger.debug("MermaidServiceClient: Railway Cloud 检测失败: %s", e)
            return False

    def check_mmdc_available(self) -> bool:
        """检测本地 mmdc CLI 是否可用（带 TTL 缓存）"""
        if self._is_cache_valid(MermaidRenderBackend.MMDC):
            return self._backend_available[MermaidRenderBackend.MMDC]

        mmdc = self._mmdc_path or "mmdc"
        try:
            result = subprocess.run(
                [mmdc, "--version"],
                capture_output=True,
                text=True,
                timeout=10,
                shell=sys.platform == "win32",
            )
            available = result.returncode == 0
            self._backend_available[MermaidRenderBackend.MMDC] = available
            self._backend_cache_time[MermaidRenderBackend.MMDC] = __import__("time").time()
            if available:
                logger.info("MermaidServiceClient: mmdc CLI 可用 (%s)", mmdc)
            return available
        except Exception as e:
            self._backend_available[MermaidRenderBackend.MMDC] = False
            self._backend_cache_time[MermaidRenderBackend.MMDC] = __import__("time").time()
            logger.debug("MermaidServiceClient: mmdc CLI 检测失败: %s", e)
            return False

    async def get_available_backends(self) -> list[MermaidRenderBackend]:
        """返回所有可用后端的列表"""
        backends: list[MermaidRenderBackend] = []
        if await self.check_docker_available():
            backends.append(MermaidRenderBackend.DOCKER)
        if await self.check_railway_available():
            backends.append(MermaidRenderBackend.RAILWAY)
        if await asyncio.to_thread(self.check_mmdc_available):
            backends.append(MermaidRenderBackend.MMDC)
        return backends

    # ---------------------------------------------------------------
    # 核心渲染方法
    # ---------------------------------------------------------------

    async def render_mermaid_via_http(
        self,
        code: str,
        theme: str = "neutral",
        scale: float = 2,
    ) -> bytes:
        """通过 HTTP 服务渲染 Mermaid 代码为 PNG 字节流

        自动尝试可用后端（Docker → Railway → mmdc），全部失败时抛出异常。

        Args:
            code: Mermaid 代码
            theme: 主题（default/neutral/dark/forest）
            scale: 缩放倍数（2 表示 2x 高清，支持 2.5 等浮点值）

        Returns:
            PNG 图片字节流

        Raises:
            MermaidRenderError: 所有后端渲染均失败
        """
        if not code or not code.strip():
            raise MermaidRenderError("Mermaid 代码为空", backend="all")

        last_error: str | None = None

        # 后端 1: Docker mermaid-service
        if await self.check_docker_available():
            try:
                return await self._render_via_docker(code, theme, scale)
            except Exception as e:
                last_error = f"Docker 渲染失败: {e}"
                logger.warning("MermaidServiceClient: %s", last_error)

        # 后端 2: Railway Cloud
        if await self.check_railway_available():
            try:
                return await self._render_via_railway(code, theme, scale)
            except Exception as e:
                last_error = f"Railway 渲染失败: {e}"
                logger.warning("MermaidServiceClient: %s", last_error)

        # 后端 3: mmdc CLI
        # BUG-FIX-35 修复：check_mmdc_available 内部用 subprocess.run 同步检测
        # （Windows shell 启动 + 最长 10s 超时），在 async 上下文直接调用会阻塞
        # 事件循环。用 asyncio.to_thread 包装到线程池执行。
        if await asyncio.to_thread(self.check_mmdc_available):
            try:
                return await asyncio.to_thread(self._render_via_mmdc, code)
            except Exception as e:
                last_error = f"mmdc 渲染失败: {e}"
                logger.warning("MermaidServiceClient: %s", last_error)

        # 全部失败
        raise MermaidRenderError(
            message=f"所有渲染后端均不可用: {last_error or '无可用后端'}",
            backend="all",
            code=code[:200],
        )

    # ---------------------------------------------------------------
    # Docker 后端渲染
    # ---------------------------------------------------------------

    async def _render_via_docker(self, code: str, theme: str, scale: float) -> bytes:
        """通过 Docker mermaid-service 渲染"""
        client = await self._get_client()
        payload = {
            "code": code,
            "type": "png",
            "theme": theme,
            "scale": scale,
            "backgroundColor": "white",
        }
        try:
            resp = await client.post(
                f"{self._docker_url}/render",
                json=payload,
                timeout=self._timeout,
            )
            if resp.status_code != 200:
                raise MermaidRenderError(
                    f"Docker 服务返回 HTTP {resp.status_code}: {resp.text[:200]}",
                    backend="docker",
                )
            content_type = resp.headers.get("content-type", "")
            if "image" in content_type or "png" in content_type:
                return resp.content
            # 可能返回 JSON 包裹的 base64
            data = resp.json()
            raw = data.get("image") or data.get("data") or data.get("png") or ""
            if raw:
                import base64

                return base64.b64decode(raw)
            raise MermaidRenderError(
                "Docker 服务返回格式异常，无法解析图片",
                backend="docker",
            )
        except httpx.TimeoutException:
            raise MermaidRenderError(
                f"Docker 渲染超时（{self._timeout}秒）",
                backend="docker",
            )
        except httpx.RequestError as e:
            raise MermaidRenderError(
                f"Docker 请求失败: {e}",
                backend="docker",
            ) from e

    # ---------------------------------------------------------------
    # Railway 后端渲染
    # ---------------------------------------------------------------

    async def _render_via_railway(self, code: str, theme: str, scale: float) -> bytes:
        """通过 Railway Cloud 服务渲染"""
        client = await self._get_client()
        payload = {
            "code": code,
            "type": "png",
            "theme": theme,
            "scale": scale,
            "backgroundColor": "white",
        }
        try:
            resp = await client.post(
                f"{self._railway_url}/render",
                json=payload,
                timeout=self._timeout,
            )
            if resp.status_code != 200:
                raise MermaidRenderError(
                    f"Railway 服务返回 HTTP {resp.status_code}: {resp.text[:200]}",
                    backend="railway",
                )
            content_type = resp.headers.get("content-type", "")
            if "image" in content_type or "png" in content_type:
                return resp.content
            data = resp.json()
            raw = data.get("image") or data.get("data") or data.get("png") or ""
            if raw:
                import base64

                return base64.b64decode(raw)
            raise MermaidRenderError(
                "Railway 服务返回格式异常，无法解析图片",
                backend="railway",
            )
        except httpx.TimeoutException:
            raise MermaidRenderError(
                f"Railway 渲染超时（{self._timeout}秒）",
                backend="railway",
            )
        except httpx.RequestError as e:
            raise MermaidRenderError(
                f"Railway 请求失败: {e}",
                backend="railway",
            ) from e

    # ---------------------------------------------------------------
    # mmdc CLI 后端渲染
    # ---------------------------------------------------------------

    def _render_via_mmdc(self, code: str) -> bytes:
        """通过 mmdc CLI 渲染（同步包装）

        BUG-H-01 修复：使用 Popen 替代 subprocess.run，超时时显式 kill 整个进程树，
        避免 mmdc（Node.js）子进程在超时后成为孤儿进程持续占用资源。
        """
        mmdc = self._mmdc_path or "mmdc"
        mmd_path = output_path = cfg_path = None
        proc = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                suffix=".mmd",
                delete=False,
                encoding="utf-8",
            ) as f:
                f.write(code)
                mmd_path = f.name

            output_path = mmd_path + ".png"
            cfg_path = mmd_path + ".json"

            config = {
                "theme": "default",
                "themeVariables": {
                    "fontFamily": "Microsoft YaHei, SimHei, Noto Sans CJK SC, sans-serif",
                },
            }
            with open(cfg_path, "w", encoding="utf-8") as f:
                json.dump(config, f)

            cmd = [mmdc, "-i", mmd_path, "-o", output_path, "-c", cfg_path, "-b", "white"]
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0,
            )
            try:
                stdout, stderr = proc.communicate(timeout=60)
            except subprocess.TimeoutExpired:
                # BUG-FIX-36 修复：原 proc.kill() 在 Windows 上只终止 mmdc 主进程，
                # mmdc（Node.js）派生的 puppeteer/Chrome 孙进程不会被回收，造成
                # 僵尸浏览器进程泄漏。改用 taskkill /T /F 递归终止整个进程树。
                _kill_process_tree(proc)
                try:
                    proc.communicate(timeout=5)
                except Exception as _e:
                    logger.debug("[silent-except] mermaid_service.py: line 579 - %s", _e)
                raise MermaidRenderError("mmdc 渲染超时（60秒）", backend="mmdc")

            if proc.returncode != 0:
                raise MermaidRenderError(
                    f"mmdc 返回非零退出码: {stderr.decode('utf-8', errors='replace').strip()[:200]}",
                    backend="mmdc",
                )

            if not os.path.exists(output_path):
                raise MermaidRenderError("mmdc 未生成输出文件", backend="mmdc")

            with open(output_path, "rb") as f:
                return f.read()

        except MermaidRenderError:
            raise
        except Exception as e:
            raise MermaidRenderError(f"mmdc 渲染异常: {e}", backend="mmdc") from e
        finally:
            if proc is not None and proc.poll() is None:
                # BUG-FIX-36 修复：finally 中的二次 communicate 原无 try/except 保护，
                # 若再次超时抛 TimeoutExpired 会覆盖 try 块中的原始异常。
                _kill_process_tree(proc)
                try:
                    proc.communicate(timeout=5)
                except Exception as _e:
                    logger.debug("[silent-except] mermaid_service.py: line 606 - %s", _e)
            for p in [mmd_path, output_path, cfg_path]:
                try:
                    if p and os.path.exists(p):
                        os.unlink(p)
                except Exception as _e:
                    logger.debug("[silent-except] mermaid_service.py: line 612 - %s", _e)

    # ---------------------------------------------------------------
    # 便捷方法
    # ---------------------------------------------------------------

    async def render_mermaid_to_bytesio(
        self,
        code: str,
        theme: str = "neutral",
        scale: float = 2,
    ) -> BytesIO:
        """渲染为 BytesIO 对象"""
        data = await self.render_mermaid_via_http(code, theme, scale)
        return BytesIO(data)

    # ---------------------------------------------------------------
    # 同步渲染方法（兼容同步调用场景）
    # ---------------------------------------------------------------

    def render_mermaid_sync(
        self,
        code: str,
        theme: str = "neutral",
        scale: float = 2,
    ) -> bytes:
        """同步渲染 Mermaid 代码为 PNG 字节流

        使用 _safe_asyncio_run 统一处理 running loop 场景，
        兼容 Celery worker、FastAPI 同步 handler 等同步调用上下文。

        Args:
            code: Mermaid 代码
            theme: 主题（default/neutral/dark/forest）
            scale: 缩放倍数（2 表示 2x 高清）

        Returns:
            PNG 图片字节流

        Raises:
            MermaidRenderError: 所有后端渲染均失败
        """
        from .workflows import _safe_asyncio_run

        return _safe_asyncio_run(self.render_mermaid_via_http(code, theme, scale))

    def render_mermaid_to_bytesio_sync(
        self,
        code: str,
        theme: str = "neutral",
        scale: float = 2,
    ) -> BytesIO:
        """同步渲染为 BytesIO 对象"""
        data = self.render_mermaid_sync(code, theme, scale)
        return BytesIO(data)


# ---------------------------------------------------------------
# 模块级单例工厂
# ---------------------------------------------------------------

_client_instance: MermaidServiceClient | None = None


def get_mermaid_service_client(
    docker_url: str = "",
    railway_url: str = "",
    mmdc_path: str = "",
    timeout: int = 30,
) -> MermaidServiceClient:
    """获取全局 MermaidServiceClient 单例

    优化 V7.5：首次调用后锁定配置，后续调用忽略参数（单例不变）。
    如需重新配置，先调用 close_mermaid_service_client() 再调用此函数。
    """
    global _client_instance
    if _client_instance is None:
        _client_instance = MermaidServiceClient(
            docker_url=docker_url,
            railway_url=railway_url,
            mmdc_path=mmdc_path,
            timeout=timeout,
        )
    return _client_instance


async def close_mermaid_service_client():
    """关闭全局客户端"""
    global _client_instance
    if _client_instance:
        await _client_instance.close()
        _client_instance = None


# AI调用超时装饰器
async def with_timeout(coro, timeout: int = 30, error_msg: str = "AI调用超时"):
    """带超时的AI调用装饰器"""
    import asyncio

    try:
        return await asyncio.wait_for(coro, timeout=timeout)
    except TimeoutError:
        raise TimeoutError(f"{error_msg}（{timeout}秒）")
