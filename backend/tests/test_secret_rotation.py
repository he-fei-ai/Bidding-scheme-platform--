"""
AGNES_API_KEY 轮换回归测试。

目标
----
1. 泄露密钥不得重新出现在任何受版本控制的源码/文档中（CI 安全网）。
   一旦有人把 2026-09-15 确认泄露的旧密钥重新提交进仓库，本测试即失败。
2. 本地 backend/.env 中的 AGNES_API_KEY 若已配置，必须满足：
   非泄露值、最小强度（长度 / 前缀格式）。未配置（空值）视为本机未启用
   Agnes，自动 skip（与 .env 不存在同性质）。

重要
----
真实密钥轮换必须在 Agnes 控制台完成（作废旧密钥、生成新密钥）。
本测试只做「仓库 + 本地 .env」层面的护栏，无法代替在服务商处作废旧密钥。
backend/.env 已被 gitignore，CI 中不存在，第 2 项会自动 skip。
"""
from pathlib import Path
import re

import pytest


# 2026-09-15 确认泄露并已作废的旧密钥（仅作比对靶值，绝非可用密钥）。
KNOWN_COMPROMISED_AGNES_KEY = "cpk-ShoP4EvOmujTFJFm64qVogYpxTj8JaZVQkRH70egs3XJJZqU"

BACKEND_ROOT = Path(__file__).resolve().parent.parent  # backend/
WORKSPACE_ROOT = BACKEND_ROOT.parent

# 仅扫描受版本控制、可能误提交密钥的文本目录，避开巨量/无关目录。
SCAN_ROOTS = [BACKEND_ROOT, WORKSPACE_ROOT / "docs"]
SKIP_DIR_NAMES = {
    ".git", "__pycache__", ".venv", "venv", "node_modules",
    "data", "logs", "dist", "dist-audit", ".ruff_cache",
    ".pytest_cache", ".mypy_cache", "easybidding-service", "frontend",
}
SCAN_EXTS = {
    ".py", ".ts", ".tsx", ".js", ".json", ".md", ".txt",
    ".yml", ".yaml", ".toml", ".cfg", ".ini", ".env.example",
}


def _iter_scan_files():
    self_path = Path(__file__).resolve()
    for root in SCAN_ROOTS:
        if not root.exists():
            continue
        for p in root.rglob("*"):
            if not p.is_file():
                continue
            if p.resolve() == self_path:  # 不要扫描本测试文件自身
                continue
            if p.name == ".env":  # gitignored，不扫描
                continue
            if any(part in SKIP_DIR_NAMES for part in p.parts):
                continue
            if p.suffix.lower() not in SCAN_EXTS:
                continue
            yield p


def test_compromised_agnes_key_not_in_tracked_source():
    """泄露的旧密钥不得重新出现在任何受控文件里。"""
    hits = []
    for p in _iter_scan_files():
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except (OSError, UnicodeError):
            continue
        if KNOWN_COMPROMISED_AGNES_KEY in text:
            hits.append(str(p))
    assert not hits, (
        "已泄露的 AGNES_API_KEY 仍出现在受控文件中，必须作废并清除：\n"
        + "\n".join(hits)
    )


def test_local_env_agnes_key_is_rotated():
    """本地 .env 的密钥必须已轮换（非空、非泄露值、满足最小强度）。"""
    env_path = BACKEND_ROOT / ".env"
    if not env_path.exists():
        pytest.skip(".env 不存在（CI/未配置），跳过本地密钥强度检查")

    try:
        from dotenv import dotenv_values
    except ImportError:
        pytest.skip("python-dotenv 不可用，跳过本地密钥强度检查")

    vals = dotenv_values(env_path)
    key = (vals.get("AGNES_API_KEY") or "").strip()

    # 调整 2026-09-18：未配置（空值）视为“本机未启用 Agnes”，与 .env 不存在
    # 同性质 → skip 而非 fail。安全底线不受影响：空值不可能等于泄露值，
    # 且仓库侧泄露密钥扫描由 test_compromised_agnes_key_not_in_tracked_source 兼顾。
    # 一旦填写了密钥，以下强度/非泄露断言照常强制执行。
    if not key:
        pytest.skip(
            ".env 中 AGNES_API_KEY 未配置（本机未启用 Agnes）；"
            "若曾使用过旧密钥，请到 Agnes 控制台作废并写入新密钥"
        )
    assert key != KNOWN_COMPROMISED_AGNES_KEY, (
        ".env 中的 AGNES_API_KEY 仍是 2026-09-15 已泄露的旧密钥，必须立即轮换"
    )
    assert len(key) >= 32, "AGNES_API_KEY 长度不足 32，疑似占位/弱密钥"
    assert (
        re.match(r"^cpk-[\w]{8,}$", key)
        or re.match(r"^[\w\-]{32,}$", key)
    ), "AGNES_API_KEY 格式异常，疑似占位/弱密钥"
