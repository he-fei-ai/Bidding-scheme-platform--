"""解析模块回归测试（2026-09-16）

覆盖本次修复的两类缺陷：
- F1 (json_parser): 组合修复缺失 —— 同一段 JSON 同时含非法反斜杠转义（如 Markdown 转义）、裸换行
      与尾随逗号/注释时，单独做任一种修复都无法解析，必须「转义修复 + 宽松语法清理」
      组合才能消化，否则会退化到昂贵的 AI 修复重试。
- F2 (ocr_parser): 并发控制死代码；MinerU 新版把 md 写到子目录时只查一层会漏掉结果。

所有用例不依赖真实 MinerU/外部网络，仅单元级验证解析逻辑。
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.utils.json_parser import extract_json_from_text
from app.services.ocr_parser import MinerUParser


# ==================== F1: JSON 组合修复 ====================


# (原始文本, 期望解析结果)
COMBINED_CASES = [
    # 代码围栏内同时含 Markdown 转义 \. 与尾随逗号
    (
        '```json\n[{"name":"1\\. 步骤", "v":1,},]\n```',
        [{"name": "1. 步骤", "v": 1}],
    ),
    # 裸换行 + 尾随逗号共存
    (
        '{"a": "第一行\n第二行", "b": 2,}',
        {"a": "第一行\n第二行", "b": 2},
    ),
    # 行注释 + 非法转义 \d 共存
    (
        '{"re": "\\d+", "x": 1 // 备注\n}',
        {"re": "\\d+", "x": 1},
    ),
    # 块注释 + 非法转义 \. 共存
    (
        '{"t": "1\\. 项" /* 备注 */, "ok": true}',
        {"t": "1. 项", "ok": True},
    ),
]


@pytest.mark.parametrize(("raw", "expected"), COMBINED_CASES)
def test_combined_escape_and_loose_repair(raw, expected):
    """同时含转义瑕疵与尾随逗号/注释的 JSON 必须能解析（组合修复）"""
    assert extract_json_from_text(raw) == expected, f"组合修复失败: {raw!r}"


def test_combined_repair_does_not_alter_valid_json():
    """合法 JSON 仍应原样解析，组合修复不应改变结果"""
    assert extract_json_from_text('{"a": 1, "b": [1,2,3]}') == {"a": 1, "b": [1, 2, 3]}
    assert extract_json_from_text('[{"k":"v"}]') == [{"k": "v"}]
    assert extract_json_from_text('前缀 {"a": 1} 后缀') == {"a": 1}


# ==================== F2: OCR 解析器 ====================


def test_ocr_concurrency_semaphore_is_real():
    """MinerU 实例必须存在线程信号量（不再只是死代码的 asyncio.Semaphore）"""
    parser = MinerUParser(max_concurrent=2, cache_max_size=5)
    assert isinstance(parser._thread_semaphore, threading.Semaphore)
    assert parser._thread_semaphore._value == 2  # 初始许可数 = max_concurrent


def test_ocr_find_output_markdown_recursive(tmp_path):
    """MinerU 新版把 md 写到子目录时也应被找到，且优先返回 <stem>.md"""
    parser = MinerUParser(cache_max_size=5)
    # 嵌套布局：output/<stem>/<stem>.md
    nested = tmp_path / "doc1" / "doc1.md"
    nested.parent.mkdir(parents=True, exist_ok=True)
    nested.write_text("# 嵌套结果", encoding="utf-8")
    # 另有一个无关 md
    other = tmp_path / "other.md"
    other.write_text("# 其它", encoding="utf-8")

    found = parser._find_output_markdown(tmp_path, "doc1")
    assert found, "递归查找未找到子目录内的 md"
    assert found[0] == nested, "应优先返回与 stem 同名的 md"


def test_ocr_cache_thread_safe_under_concurrent_miss(tmp_path):
    """并发读缓存不抛异常（多线程读写保护）"""
    parser = MinerUParser(cache_max_size=5)

    def worker(i: int):
        # 触发一次写入型 miss（不存在的文件 → FileNotFoundError 之前会先写缓存吗？
        # 不会：缓存只在成功解析后写入，这里仅验证 get/set 路径在并发下不崩溃）
        parser._set_to_cache(f"k{i}", f"text-{i}", len(f"text-{i}"))
        got = parser._get_from_cache(f"k{i}")
        assert got is not None and got["text"] == f"text-{i}"

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


def test_ocr_parse_pdf_uses_thread_semaphore(tmp_path):
    """parse_pdf 在真正执行 subprocess 前会 acquire 线程信号量（并发控制生效）"""
    fake_pdf = tmp_path / "scan.pdf"
    fake_pdf.write_bytes(b"%PDF-1.4 fake")
    out_md = tmp_path / "scan.md"
    out_md.write_text("# 扫描结果", encoding="utf-8")

    parser = MinerUParser(max_concurrent=1, cache_max_size=5)
    acquired_states: list[bool] = []

    real_acquire = parser._thread_semaphore.acquire
    real_release = parser._thread_semaphore.release

    def traced_acquire(timeout=None):
        ok = real_acquire(timeout=timeout)
        acquired_states.append(ok)
        return ok

    def traced_release():
        return real_release()

    with patch.object(parser._thread_semaphore, "acquire", traced_acquire), \
         patch.object(parser._thread_semaphore, "release", traced_release), \
         patch("app.services.ocr_parser.subprocess.run") as mock_run, \
         patch.object(parser, "_clear_old_output"):
        # 让 subprocess.run 返回成功，并在输出目录写出 md
        mock_run.return_value = MagicMock(stdout="ok", stderr="")
        # _run_mineru 直接读 output_path/<stem>.md，需要用临时输出目录
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        (out_dir / "scan.md").write_text("# 扫描结果", encoding="utf-8")

        text = parser.parse_pdf(str(fake_pdf), output_dir=str(out_dir), skip_cache=True)

    assert text == "# 扫描结果"
    assert any(acquired_states), "parse_pdf 未 acquire 线程信号量（并发控制未生效）"


def test_ocr_async_parse_pdf_runs_in_thread(tmp_path):
    """新增的 async_parse_pdf 应能在事件循环中调用并委托同步解析"""
    fake_pdf = tmp_path / "scan.pdf"
    fake_pdf.write_bytes(b"%PDF-1.4 fake")
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    (out_dir / "scan.md").write_text("# 异步结果", encoding="utf-8")

    parser = MinerUParser(cache_max_size=5)
    with patch("app.services.ocr_parser.subprocess.run") as mock_run, \
         patch.object(parser, "_clear_old_output"):
        mock_run.return_value = MagicMock(stdout="ok", stderr="")
        text = asyncio.run(
            parser.async_parse_pdf(str(fake_pdf), output_dir=str(out_dir), skip_cache=True)
        )
    assert text == "# 异步结果"
