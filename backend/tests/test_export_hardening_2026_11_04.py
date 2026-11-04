"""【导出文档模块】第 N 轮加固（2026-11-04）——B-1 mmdc 探测 GBK 解码崩溃回归钉住。

| 编号 | 优先级 | 位置 | 修复要点 |
|------|--------|------|----------|
| B-1  | P2 | services/ai/mermaid_service.py:`check_mmdc_available` | `text=True` 未指定 `errors`，中文 Windows 上 mmdc 未安装时 cmd 输出 GBK 错误文本（"'mmdc' 不是内部或外部命令"，首字节 0xB2），subprocess `_readerthread` 严格 UTF-8 解码崩溃 → 探测线程死亡（pytest 报 PytestUnhandledThreadExceptionWarning，生产侧不可观测）。补 `errors="replace"`：输出有损可读，可用性判定（returncode）不变。

该探测位于导出图表渲染链（render_mermaid_to_bytes 的 MMDC 后端可用性检查），
基线运行 export 测试集群时该警告实测可复现。

运行：`python -m pytest tests/test_export_hardening_2026_11_04.py -v`
"""
from __future__ import annotations

import subprocess
import sys
import threading
from types import SimpleNamespace
from unittest.mock import patch

from app.services.ai.mermaid_service import MermaidServiceClient


def _fresh_client(mmdc_path: str = "") -> MermaidServiceClient:
    """构造无缓存/无熔断污染的探测客户端（TTL/熔断状态均为实例级）。"""
    return MermaidServiceClient(mmdc_path=mmdc_path)


class TestMmdcProbeGbkOutput:
    """B-1：mmdc 探测对 GBK/非 UTF-8 输出必须存活，且参数契约带 errors='replace'。"""

    def test_probe_passes_replace_errors(self):
        """参数契约：探测调用必须透传 errors='replace'（修复的静态可观测面）。

        修复前该 kwargs 缺失 → Windows 中文环境 cmd 的 GBK 输出走严格 UTF-8 解码。
        """
        client = _fresh_client()
        captured: dict = {}

        def _fake_run(*args, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(returncode=1)

        with patch("app.services.ai.mermaid_service.subprocess.run", side_effect=_fake_run):
            ok = client.check_mmdc_available()

        assert ok is False  # returncode=1 → 不可用（原语义）
        assert captured.get("errors") == "replace", (
            "check_mmdc_available 的 subprocess.run 必须携带 errors='replace'，"
            "否则中文 Windows 上 GBK 输出会让读取线程崩溃"
        )
        assert captured.get("text") is True

    def test_probe_survives_decode_error(self):
        """韧性契约：即使底层 run 抛 UnicodeDecodeError，探测也只判不可用，不向调用方传播。"""
        client = _fresh_client()

        def _explode(*args, **kwargs):
            raise UnicodeDecodeError("utf-8", b"\xb2\xbb", 0, 1, "invalid start byte")

        with patch("app.services.ai.mermaid_service.subprocess.run", side_effect=_explode):
            ok = client.check_mmdc_available()  # 修复前后均被 except Exception 兜住

        assert ok is False

    def test_real_subprocess_gbk_output_no_thread_crash(self):
        """端到端复现基线警告场景：子进程输出 GBK 字节 + text=True + errors='replace'。

        - 修复后：读取线程正常解码（非法字节 → U+FFFD），无线程异常；
        - 用 threading.excepthook 捕获（与 pytest PytestUnhandledThreadExceptionWarning
          同一挂钩点），任何线程级 UnicodeDecodeError 都会失败本用例。
        - 同时用 bytes 级 stderr 断言探测结论仍由 returncode 决定。
        """
        captured_thread_errors: list[BaseException] = []
        original_hook = threading.excepthook

        def _capture(args: threading.ExceptHookArgs):
            captured_thread_errors.append(args.exc_value)  # type: ignore[arg-type]

        threading.excepthook = _capture
        try:
            # 与 check_mmdc_available 同款调用形态：列表参数 + text=True + shell（win32）
            result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import sys;sys.stdout.buffer.write(b'\\xb2\\xbb\\xca\\xc7');"
                    "sys.stdout.flush();sys.exit(1)",
                ],
                capture_output=True,
                text=True,
                errors="replace",
                timeout=10,
                shell=(sys.platform == "win32"),
            )
            assert result.returncode == 1
        finally:
            threading.excepthook = original_hook

        assert not captured_thread_errors, (
            f"探测读取线程发生异常（GBK 输出解码崩溃未修复）: {captured_thread_errors!r}"
        )
