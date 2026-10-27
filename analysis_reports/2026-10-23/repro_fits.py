#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""定位 _heuristic_fast_fits 与参考实现不等价的最小反例。"""
import random
import sys
from pathlib import Path

sys.path.insert(0, r"j:/编程/招投标方案平台/backend")

from app.services.ai.prompts._context import (  # noqa: E402
    _cjk_noncjk,
    _fit_to_token_budget,
    _heuristic_fast_fits,
    estimate_tokens,
)


def _reference_fits(content, n, head, marker, budget):
    return estimate_tokens(head + content[:n].rstrip() + marker) <= budget


def _random_content(rng, length):
    pool = (
        "中文测试内容工程技术方案招标文件编制依据一二三四五六七八九十"
        "abc123XYZ .,;:!?-\n\t\r "
        "　  行尾空白用例　"
    )
    return "".join(rng.choice(pool) for _ in range(length))


rng = random.Random(20261020)
found = 0
for _ in range(12):
    length = rng.randint(5000, 12000)
    content = _random_content(rng, length)
    head = rng.choice(["", "【项目概述】", "头部Head123"])
    marker = rng.choice(["", "标", "\n…（内容因 Token 预算已截断）"])
    budget = rng.randint(200, 6000)
    fast = _heuristic_fast_fits(content, head, marker, budget)
    ns = {0, 1, len(content), len(content) - 1}
    ns.update(range(0, min(len(content), 4096) + 2, 97))
    ns.update(rng.randint(0, len(content)) for _ in range(300))
    for n in sorted(ns):
        if n < 0 or n > len(content):
            continue
        f = fast(n)
        r = _reference_fits(content, n, head, marker, budget)
        if f != r:
            found += 1
            if found <= 6:
                print("MISMATCH n=%d head=%r marker=%r budget=%d fast=%s ref=%s"
                      % (n, head, marker, budget, f, r))
                body = content[:n].rstrip()
                print("   len(content[:n])=%d len(rstrip)=%d"
                      % (len(content[:n]), len(body)))
                print("   estimate_tokens(ref concat)=%d"
                      % estimate_tokens(head + body + marker))
                hc, hn = _cjk_noncjk(head)
                mc, mn = _cjk_noncjk(marker)
                bc, bn = _cjk_noncjk(body)
                print("   seg: head=(%d,%d) body=(%d,%d) marker=(%d,%d)" % (hc, hn, bc, bn, mc, mn))
                print("   seg-formula=%d  concat-formula=%d"
                      % (hc + bc + mc + ((hn + bn + mn + 3) // 4),
                         estimate_tokens(head + body + marker)))
                print("   tail=%r" % content[max(0, n - 8):n])
print("total mismatches:", found)
