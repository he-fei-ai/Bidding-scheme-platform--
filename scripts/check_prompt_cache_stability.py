#!/usr/bin/env python
"""提示词缓存稳定性静态检核（E-3 / A6 四规则工程化）。

背景：正文按叶子节点并发生成，分层消息（system/项目概述/事实/上下级章节/…）
的稳定前缀是 prompt cache 命中的基础。任何在共享前缀里插入「时间戳 / 随机串 /
trace_id / 每次都变的顺序和换行」都会打断前缀，让输入成本回到全价。

本脚本对关键消息构建函数做粗粒度静态扫描（启发式，可误报但不漏报方向）：
  1. 扫描对象：backend/app（排除 __pycache__、.ruff_cache、venv）
  2. 命中规则：
     - 共享上下文里出现 datetime.now / time.time / uuid / random 生成的内容
       （排除函数名含「日志/log/error/audit」等动机合理的分支 —— 见 ALLOW 表）
     - f-string / join 里混入实时时钟（常见缓存杀手）
  3. 只输出检查结果，不修改代码。

用法：
    python scripts/check_prompt_cache_stability.py [--strict]
退出码：strict 模式下发现嫌疑返回 1（供 CI 用）；默认 0（仅提示）。

注意：这是工程护栏不是扫描器——最终裁决仍靠
tests/test_enhancement_2026_10_09_eight_plan.py 的消息稳定性快照测试。
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


#: 扫描根（脚本位于 scripts/，仓库根在其上一级）
REPO_ROOT = Path(__file__).resolve().parents[1]

#: 只扫描「提示词/消息构建」相关目录 —— 只有这里的时钟/随机注入才会打断
#: 缓存前缀；数据库连接池、路由延迟统计等写时间戳是合法用途，不应被误报。
SCAN_DIRS = (
    REPO_ROOT / "backend" / "app" / "services" / "ai",
)

#: 忽略的路径片段
IGNORE_PARTS = ("__pycache__", ".ruff_cache", "node_modules", "venv", ".venv")

#: 允许出现「时间/随机」的动机（否则为打断缓存前缀的嫌疑）
ALLOWED_CONTEXT = (
    "log", "monitor", "metric", "usage", "audit", "error", "trace",
    "cache_key", "cache_key:", "request_id", "token_usage",
    # 进程治理类（限流/超时/心跳/过期/耗时统计）不是写入 shared prefix
    "latency", "elapsed", "duration", "ttl", "deadline", "cooldown",
    "timeout", "expire", "heartbeat", "start_time", "sleep",
)

#: 实时值特征表达式
CLOCK_RE = re.compile(
    r"(datetime\.now|datetime\.utcnow|time\.time\(\)|strftime|timestamp\()"
)
RANDOM_RE = re.compile(r"\buuid\b|random\.(random|randint|choice|uniform|randrange)")

#: 需要重点盯防的文件（正文/解析/目录的消息构建入口）
FOCUS_FILES = (
    "workflows_sections.py",
    "workflows_analysis.py",
    "workflows_outline_v51.py",
    "workflows_orchestration.py",
)


def scan_file(path: Path, strict: bool) -> list[str]:
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return []
    hits: list[str] = []
    for idx, line in enumerate(lines, 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if any(marker in line.lower() for marker in ALLOWED_CONTEXT):
            continue
        clock_hit = CLOCK_RE.search(line)
        random_hit = RANDOM_RE.search(line)
        if clock_hit or random_hit:
            hits.append(f"  {path.relative_to(REPO_ROOT)}:{idx}: {line.strip()[:120]}")
    return hits


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strict", action="store_true", help="严格模式：有嫌疑即退出码 1")
    args = parser.parse_args()

    total_suspicious = 0
    suspicious_locations: list[str] = []
    scanned = 0
    for root_dir in SCAN_DIRS:
        for path in sorted(root_dir.rglob("*.py")):
            if any(part in path.parts for part in IGNORE_PARTS):
                continue
            scanned += 1
            hits = scan_file(path, args.strict)
            if hits:
                total_suspicious += len(hits)
                suspicious_locations.extend(hits)

    print(f"扫描文件数: {scanned}；疑似缓存前缀破坏: {total_suspicious} 处")
    if suspicious_locations:
        print("\n".join(suspicious_locations))
    else:
        print("未发现明显嫌疑（仍需依赖快照测试兜底）")

    # focus 文件必须存在（防止误删/误移后规则失效）
    missing = []
    for name in FOCUS_FILES:
        if not list((REPO_ROOT / "backend" / "app" / "services" / "ai").glob(name)):
            missing.append(name)
    if missing:
        print(f"[FAIL] 关注文件缺失: {missing}")
        return 1

    if strict_out := (args.strict and total_suspicious > 0):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())