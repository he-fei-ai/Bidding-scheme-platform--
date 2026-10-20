# -*- coding: utf-8 -*-
"""test_bugfix_2026_10_20_heuristic_fit_perf.py — 无 tiktoken 环境裁剪加速（PERF）

背景（2026-10-20 full-suite 实录）：
    本部署环境未安装 tiktoken，`estimate_tokens` 走启发式（CJK 1 字符 = 1 token、
    其余 4 字符 ≈ 1 token）。`_fit_to_token_budget` 在该模式下每次 `_fits(n)` 都对
    `content[:n]` 做一次全量 `str.translate` + 字符串拼接，二分约 18 次迭代，
    24 万字符实测 ~0.35s——恰好压在 perf 测试 0.4s 断言线上，full-suite 负载下
    偶发越线（`test_performance_on_long_text` 失败即此因，单跑 0.32~0.44s 抖动）。

修复（PERF 2026-10-20）：
    `_heuristic_fast_fits` 把 `_fits` 升级为 O(1) 版——启发式计数可按段**精确
    分解**（拼接不改变单字符分类，按总量一次取整），不是近似。仅当无 tiktoken
    且正文 ≥ 4096 字符时启用；tiktoken 可用时走原 token 维度快速路径，行为不变。

本文件锁定：
    1. 快速版 _fits 与原实现**逐位等价**（随机语料 × 全量 n 采样）；
    2. 端到端 `_fit_to_token_budget` 输出与原实现逐字节一致；
    3. 长文本性能有数量级余量（不再压线）；
    4. 短文本（<4096）不走快速路径，行为与历史逐字一致。
"""

import random
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.ai.prompts._context import (  # noqa: E402
    _CJK_DELETE_TABLE,
    _fit_to_token_budget,
    _heuristic_fast_fits,
    estimate_tokens,
)


# 原始实现的 _fits 口径（等价性基准，逐字复制自已提交版本）
def _reference_fits(content: str, n: int, head: str, marker: str, budget: int) -> bool:
    return estimate_tokens(head + content[:n].rstrip() + marker) <= budget


def _reference_fit(content: str, budget: int, head: str, marker: str) -> str:
    """原始 _fit_to_token_budget 的回退实现（无 tiktoken 时的字符二分）。"""
    if budget <= 0 or not content:
        return ""
    if not head and not marker and estimate_tokens(content) <= budget:
        return content.rstrip()
    lo, hi = 0, len(content)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if _reference_fits(content, mid, head, marker, budget):
            lo = mid
        else:
            hi = mid - 1
    return content[:lo].rstrip() if lo > 0 else ""


def _random_content(rng: random.Random, length: int) -> str:
    """混合语料：CJK / ASCII / 数字 / 空白（含行尾空白，压测 rstrip 口径）。"""
    pool = (
        "中文测试内容工程技术方案招标文件编制依据一二三四五六七八九十"
        "abc123XYZ .,;:!?-\n\t\r "
        "　  行尾空白用例　"
    )
    return "".join(rng.choice(pool) for _ in range(length))


class TestFastFitsEquivalence:
    def test_equivalent_on_random_corpora(self):
        """随机语料 × 全量 n 采样：快速版与原 _fits 逐位一致。"""
        rng = random.Random(20261020)
        checked = 0
        for _ in range(12):
            length = rng.randint(5000, 12000)
            content = _random_content(rng, length)
            head = rng.choice(["", "【项目概述】", "头部Head123"])
            marker = rng.choice(["", "标", "\n…（内容因 Token 预算已截断）"])
            budget = rng.randint(200, 6000)
            fast = _heuristic_fast_fits(content, head, marker, budget)
            # 采样 n：边界 + 均匀 + 随机，覆盖 rstrip 与块边界
            ns = {0, 1, len(content), len(content) - 1}
            ns.update(range(0, min(len(content), 4096) + 2, 97))
            ns.update(rng.randint(0, len(content)) for _ in range(300))
            for n in ns:
                if n < 0 or n > len(content):
                    continue
                assert fast(n) == _reference_fits(content, n, head, marker, budget), (
                    f"快速版 _fits 不等价: n={n} head={head!r} marker={marker!r} budget={budget}"
                )
                checked += 1
        assert checked > 3000

    def test_equivalent_with_trailing_unicode_whitespace(self):
        """行尾含 U+3000 等 Unicode 空白时，rstrip 口径仍逐位一致。"""
        content = "中文内容" * 800 + "　　" + "abc" * 100 + "\u2028\u3000"
        for budget in (100, 2000, 8000):
            fast = _heuristic_fast_fits(content, "", "标", budget)
            for n in (len(content), len(content) - 1, len(content) - 4, 100, 3200):
                assert fast(n) == _reference_fits(content, n, "", "标", budget)

    def test_equivalent_empty_head_marker(self):
        """head/marker 为空（最常见调用形态）同样逐位一致。"""
        rng = random.Random(7)
        content = _random_content(rng, 9000)
        fast = _heuristic_fast_fits(content, "", "", 3000)
        for n in range(0, len(content) + 1, 61):
            assert fast(n) == _reference_fits(content, n, "", "", 3000)


class TestEndToEndIdentical:
    def test_output_byte_identical_to_reference(self):
        """端到端：快速路径启用后输出与原始实现逐字节一致。"""
        rng = random.Random(42)
        for _ in range(10):
            content = _random_content(rng, rng.randint(5000, 15000))
            head = rng.choice(["", "【概述】", "Head-abc"])
            marker = rng.choice(["", "标", "\n…（内容因 Token 预算已截断）"])
            budget = rng.randint(300, 9000)
            fast_out = _fit_to_token_budget(content, budget, head, marker)
            ref_out = _reference_fit(content, budget, head, marker)
            assert fast_out == ref_out, (
                f"端到端输出不一致: budget={budget} head={head!r} marker={marker!r}"
            )
            # 不超预算保证（与原实现同一契约）
            assert estimate_tokens(head + fast_out + marker) <= budget or fast_out == ""

    def test_short_content_unchanged(self):
        """"<4096 字符不走快速路径：输出与原始实现一致（行为零变化）。"""
        rng = random.Random(99)
        for _ in range(20):
            content = _random_content(rng, rng.randint(1, 4000))
            budget = rng.randint(10, 3000)
            assert _fit_to_token_budget(content, budget, "头", "尾") == _reference_fit(
                content, budget, "头", "尾"
            )


class TestPerformance:
    def test_long_text_has_magnitude_margin(self):
        """24 万字符裁剪应有数量级余量（原 ~0.35s 压线 0.4s 断言；现应 ~0.06s）。"""
        text = "中文测试内容abc123" * 20000
        # 预热（建表/导入的一次性成本不计入断言）
        _fit_to_token_budget(text, 100000, "", "标")
        best = min(
            (
                (lambda t0: (
                    _fit_to_token_budget(text, 100000, "", "标"),
                    time.perf_counter() - t0,
                )[1])(time.perf_counter())
            )
            for _ in range(3)
        )
        assert best < 0.4, f"长文本裁剪应远低于 0.4s 断言线，实测 {best:.3f}s"
        # 数量级验证：至少比原始回退实现的量级快 3 倍以上
        assert best < 0.12, f"期望 ~0.06s 量级，实测 {best:.3f}s（优化可能未生效）"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
