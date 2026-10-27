"""渲染后端实现：本地 mmdc（首选）+ 远程 mermaid.ink（默认关闭）。

优先级：本地 mmdc > 远程。本地后端在私网内完成渲染，**不外传项目数据**；
远程后端会把图表源码发给第三方服务，因此默认关闭（见 config.REMOTE_ENABLED）。
"""
from __future__ import annotations

import base64
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time

import httpx

from . import config as C

logger = logging.getLogger("mermaid_service.backends")


# ── 自动探测工具 ──────────────────────────────────────────────────────

#: Windows 常见 Chromium 内核浏览器位置（Edge 优先：Win10/11 必装）
_WIN_BROWSERS = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files\Chromium\Application\chrome.exe",
)

_MAC_BROWSERS = (
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
)

_LINUX_BROWSERS = (
    "/usr/bin/microsoft-edge",
    "/usr/bin/microsoft-edge-stable",
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
)

#: npm 全局 bin 常见位置（mmdc 装在这里但不在 PATH 时也能找到）
_NPM_GLOBAL_BINS = (
    os.path.expandvars(r"%APPDATA%\npm"),
    os.path.expanduser("~/.npm-global/bin"),
    "/usr/local/bin",
    "/usr/bin",
)


def _platform_browsers() -> tuple[str, ...]:
    if sys.platform == "win32":
        return _WIN_BROWSERS
    if sys.platform == "darwin":
        return _MAC_BROWSERS
    return _LINUX_BROWSERS


def detect_browser() -> str:
    """探测本地 Chromium 内核浏览器；找不到返回空串。"""
    if C.BROWSER_PATH:
        return C.BROWSER_PATH if os.path.isfile(C.BROWSER_PATH) else ""
    for p in _platform_browsers():
        try:
            if os.path.isfile(p):
                return p
        except OSError:
            continue
    # PATH 上的 chrome/edge/chromium
    for name in ("msedge", "microsoft-edge", "chrome", "google-chrome", "chromium"):
        found = shutil.which(name)
        if found:
            return found
    return ""


def detect_mmdc() -> str:
    """探测 mmdc 可执行文件；找不到返回空串。"""
    if C.MMDC_PATH:
        return C.MMDC_PATH if os.path.isfile(C.MMDC_PATH) else ""
    names = ("mmdc", "mmdc.cmd", "mmdc.ps1") if sys.platform == "win32" else ("mmdc",)
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    for d in _NPM_GLOBAL_BINS:
        if not d or not os.path.isdir(d):
            continue
        for name in names:
            cand = os.path.join(d, name)
            if os.path.isfile(cand):
                return cand
    return ""


def _kill_process_tree(proc: subprocess.Popen) -> None:
    """递归终止进程树（mmdc → node → chrome）。Windows 用 taskkill /T /F。"""
    try:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                capture_output=True, timeout=5,
            )
        else:
            import signal
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


# ── 后端：本地 mmdc ───────────────────────────────────────────────────


class LocalMmdcBackend:
    """本地 mermaid-cli（mmdc）后端。

    复用机器上已有的 Chromium 内核浏览器（Edge/Chrome），无需额外下载
    ~170MB 的 puppeteer Chromium —— 这是本服务能在现有 Windows 机器上
    零成本落地的前提。
    """

    name = "mmdc"

    def __init__(self) -> None:
        self._mmdc = detect_mmdc()
        self._browser = detect_browser()
        self._available: bool | None = None
        self._checked_at = 0.0
        self._ttl = 300.0  # 可用性缓存 5 分钟

    @property
    def detail(self) -> dict:
        return {
            "name": self.name,
            "available": self.available(),
            "mmdc": self._mmdc or "",
            "browser": self._browser or "",
            "note": "" if (self._mmdc and self._browser) else (
                "缺少 mmdc 或本地浏览器；执行 install_mmdc.ps1 安装"
            ),
        }

    def available(self) -> bool:
        now = time.monotonic()
        if self._available is not None and now - self._checked_at < self._ttl:
            return self._available
        self._available = bool(self._mmdc) and bool(self._browser)
        self._checked_at = now
        return self._available

    def invalidate(self) -> None:
        """安装/卸载 mmdc 后调用，强制重新探测。"""
        self._mmdc = detect_mmdc()
        self._browser = detect_browser()
        self._available = None

    def render(self, code: str, theme: str, scale: float, background: str) -> bytes:
        if not self.available():
            raise RuntimeError(
                f"本地 mmdc 后端不可用（mmdc={self._mmdc or '未找到'}, "
                f"browser={self._browser or '未找到'}）"
            )

        workdir = tempfile.mkdtemp(prefix="mmd_render_")
        mmd_path = out_path = cfg_path = pcfg_path = None
        proc = None
        try:
            mmd_path = os.path.join(workdir, "diagram.mmd")
            out_path = os.path.join(workdir, "diagram.png")
            with open(mmd_path, "w", encoding="utf-8") as f:
                f.write(code)

            # mermaid 配置：中文字体优先，避免豆腐块
            cfg = {
                "theme": theme if theme in ("default", "neutral", "dark", "forest") else "neutral",
                "themeVariables": {
                    "fontFamily": "Microsoft YaHei, SimHei, Noto Sans CJK SC, PingFang SC, sans-serif",
                },
            }
            cfg_path = os.path.join(workdir, "mermaid.json")
            with open(cfg_path, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False)

            # puppeteer 配置：指向本地浏览器，禁沙箱（Windows 服务场景必需）
            if C.PUPPETEER_CONFIG and os.path.isfile(C.PUPPETEER_CONFIG):
                pcfg_path = C.PUPPETEER_CONFIG
            else:
                pcfg_path = os.path.join(workdir, "puppeteer.json")
                with open(pcfg_path, "w", encoding="utf-8") as f:
                    json.dump(
                        {"executablePath": self._browser, "args": ["--no-sandbox", "--disable-dev-shm-usage"]},
                        f, ensure_ascii=False,
                    )

            cmd = [
                self._mmdc,
                "-i", mmd_path,
                "-o", out_path,
                "-c", cfg_path,
                "-p", pcfg_path,
                "-b", background or "white",
                "-t", theme if theme in ("default", "neutral", "dark", "forest") else "neutral",
                "-s", str(scale),
                "--quiet",
            ]
            # Windows 上 npm bin 是 .cmd，Popen 需要 shell=True 才能执行
            use_shell = sys.platform == "win32" and self._mmdc.lower().endswith((".cmd", ".bat"))
            proc = subprocess.Popen(
                cmd if not use_shell else " ".join(f'"{c}"' for c in cmd),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=use_shell,
                creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if sys.platform == "win32" else 0,
            )
            try:
                _, stderr = proc.communicate(timeout=C.RENDER_TIMEOUT)
            except subprocess.TimeoutExpired:
                _kill_process_tree(proc)
                try:
                    proc.communicate(timeout=5)
                except Exception:
                    pass
                raise RuntimeError(f"mmdc 渲染超时（{C.RENDER_TIMEOUT}s）") from None

            if proc.returncode != 0:
                err = (stderr or b"").decode("utf-8", errors="replace").strip()[:300]
                raise RuntimeError(f"mmdc 退出码 {proc.returncode}: {err}")

            if not os.path.isfile(out_path):
                raise RuntimeError("mmdc 未生成输出文件")
            with open(out_path, "rb") as f:
                data = f.read()
            if not data:
                raise RuntimeError("mmdc 输出为空文件")
            return data
        finally:
            if proc is not None and proc.poll() is None:
                _kill_process_tree(proc)
            # 自建的临时配置可以删；用户显式提供的 PUPPETEER_CONFIG 不能动
            for p in (mmd_path, out_path, cfg_path):
                try:
                    if p and os.path.exists(p) and p != C.PUPPETEER_CONFIG:
                        os.unlink(p)
                except OSError:
                    pass
            try:
                shutil.rmtree(workdir, ignore_errors=True)
            except Exception:
                pass


# ── 后端：远程 mermaid.ink 兼容端点（默认关闭）────────────────────────


class RemoteBackend:
    """mermaid.ink 兼容远程后端。

    ⚠️ 会把图表源码（含项目内容）发送到第三方。默认关闭，
    仅在 `MERMAID_ALLOW_REMOTE=true` 且本地后端不可用时兜底。
    """

    name = "remote"

    def __init__(self) -> None:
        self._client: httpx.Client | None = None

    @property
    def detail(self) -> dict:
        return {
            "name": self.name,
            "available": C.REMOTE_ENABLED,
            "url": C.REMOTE_URL,
            "note": "" if C.REMOTE_ENABLED else "默认关闭（隐私：不外传项目数据）；设 MERMAID_ALLOW_REMOTE=true 启用",
        }

    def available(self) -> bool:
        return C.REMOTE_ENABLED

    def _get_client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                timeout=httpx.Timeout(C.REMOTE_TIMEOUT),
                follow_redirects=True,
            )
        return self._client

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None

    def render(self, code: str, theme: str, scale: float, background: str) -> bytes:
        if not C.REMOTE_ENABLED:
            raise RuntimeError("远程后端未启用（MERMAID_ALLOW_REMOTE=false）")
        # mermaid.ink：GET /img/<urlsafe-base64>?type=png&theme=..&scale=..&bgColor=..
        b64 = base64.urlsafe_b64encode(code.encode("utf-8")).decode("ascii").rstrip("=")
        params = {
            "type": "png",
            "theme": theme if theme in ("default", "neutral", "dark", "forest") else "neutral",
            "scale": str(scale),
            "bgColor": background or "white",
        }
        resp = self._get_client().get(f"{C.REMOTE_URL}/img/{b64}", params=params)
        if resp.status_code != 200:
            raise RuntimeError(f"远程服务 HTTP {resp.status_code}: {resp.text[:200]}")
        ctype = resp.headers.get("content-type", "")
        if "image" in ctype or "png" in ctype:
            return resp.content
        data = resp.json()
        raw = data.get("image") or data.get("data") or data.get("png") or ""
        if raw:
            return base64.b64decode(raw)
        raise RuntimeError("远程服务返回格式异常，无法解析图片")


# ── 后端注册表 ────────────────────────────────────────────────────────

_mmdc_backend = LocalMmdcBackend()
_remote_backend = RemoteBackend()

#: 优先级顺序（本地优先）
_BACKENDS = (_mmdc_backend, _remote_backend)


def backends() -> tuple:
    return _BACKENDS


def active_backends() -> list:
    return [b for b in _BACKENDS if b.available()]


def rescan() -> None:
    """重新探测后端可用性（安装 mmdc 后无需重启服务即可生效）。"""
    _mmdc_backend.invalidate()
    logger.info(
        "[mermaid-service] 重新探测后端: mmdc=%s browser=%s remote=%s",
        _mmdc_backend.detail.get("mmdc") or "-",
        _mmdc_backend.detail.get("browser") or "-",
        _remote_backend.available(),
    )


def close_all() -> None:
    _remote_backend.close()
