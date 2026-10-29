"""敏感信息集中脱敏（ENHANCE 2026-10-29）。

统一入口：`redact(value)` / `redact_text(text)`。

脱敏规则集中配置于此（REDACT_RULES），覆盖：
- API Key / 密钥（sk-****abcd 形式保留前 3 + 后 4）
- Token / Bearer / JWT
- 密码 / passwd / secret
- 身份证号（保留前 3 后 4）
- 手机号（138****5678 形式）
- 银行卡号

设计约束：
- 只依赖标准库；函数对异常宽容（脱敏本身绝不抛异常）。
- 与 `logging_format.desensitize` 兼容：命中敏感键名的 dict 值整体掩码，
  并额外对值内容做正则脱敏（覆盖身份证/手机号出现在自由文本的场景）。
"""
from __future__ import annotations

import re
from typing import Any

#: 敏感键名（小写子串匹配，来自既有 logging_format）
SENSITIVE_KEY_HINTS: tuple[str, ...] = (
    "api_key", "apikey", "password", "passwd", "token", "secret",
    "authorization", "access_key", "private_key", "credential",
)

#: 键名命中时的整体掩码
KEY_MASK = "***已配置***"

#: 通用掩码（值内容）
VALUE_MASK = "***"


# ── 正则脱敏规则：顺序执行，先长后短，避免子串先被掩掉 ──
def _api_key(m: re.Match) -> str:
    s = m.group(0)
    if len(s) > 8:
        return f"{s[:3]}{'*' * 4}{s[-4:]}"
    return f"{s[:1]}{'*' * max(2, len(s) - 2)}{s[-1:]}"


def _phone(m: re.Match) -> str:
    s = m.group(0)
    digits = re.sub(r"\D", "", s)
    if len(digits) == 11:
        return f"{digits[:3]}****{digits[-4:]}"
    return VALUE_MASK


def _idcard(m: re.Match) -> str:
    s = m.group(0)
    if len(s) >= 10:
        return f"{s[:3]}{'*' * (len(s) - 7)}{s[-4:]}"
    return VALUE_MASK


REDACT_RULES: tuple[tuple[re.Pattern, Any], ...] = (
    # API Key：sk-... 或 以 sk 开头、含连字符的长串
    (re.compile(r"\bsk-[A-Za-z0-9_-]{6,}\b"), _api_key),
    # JWT（三段 base64）
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"), VALUE_MASK),
    # Bearer token
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._-]{8,}\b"), lambda m: f"Bearer {VALUE_MASK}"),
    # 身份证号（18 位含 X）
    (re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)"), _idcard),
    # 手机号（1 开头 11 位）
    (re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"), _phone),
    # 银行卡（13-19 位连续数字）
    (re.compile(r"(?<!\d)\d{13,19}(?!\d)"), VALUE_MASK),
    # 长 hex 密钥（32/64 位）
    (re.compile(r"\b[a-fA-F0-9]{32}\b"), VALUE_MASK),
    (re.compile(r"\b[a-fA-F0-9]{64}\b"), VALUE_MASK),
)


def is_sensitive_key(key: str) -> bool:
    """键名是否命中敏感特征。"""
    k = str(key or "").lower()
    return any(hint in k for hint in SENSITIVE_KEY_HINTS)


def redact_text(text: Any) -> str:
    """对任意文本做正则脱敏（自由文本中的手机号/身份证/密钥等）。"""
    try:
        s = str(text)
        for pattern, repl in REDACT_RULES:
            s = pattern.sub(repl, s)
        return s
    except Exception:
        return VALUE_MASK


def redact(value: Any, _depth: int = 0) -> Any:
    """递归脱敏：dict 敏感键掩码 + 值内容正则脱敏；list/tuple 递归；标量 redact_text。

    深度 > 8 时截断，防御超深嵌套 / 循环引用。
    """
    if _depth > 8:
        return "<deep>"
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            if is_sensitive_key(k):
                out[str(k)] = KEY_MASK
            else:
                out[str(k)] = redact(v, _depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [redact(v, _depth + 1) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        if isinstance(value, str) and value:
            return redact_text(value)
        return value
    try:
        return redact_text(repr(value))
    except Exception:
        return VALUE_MASK


# 兼容别名（供既有 import 使用）
desensitize = redact
