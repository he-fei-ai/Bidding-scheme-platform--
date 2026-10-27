"""json_issues.py — 校验问题格式化（《标书智能体（5）》§五）。

外部标准核心认知：**修复能不能成功，很大程度取决于错误信息是否具体**。
模型少了 `description` 字段时，修复助手拿到的不能是一句「格式不对」，
而必须是 `outline.0.children.1.description: Field required` 这类可定位信息。

本模块把三类错误统一格式化为「可读问题列表」：
  1. `json.JSONDecodeError` → 「JSON 语法错误：第 N 行第 M 列附近 xxx」；
  2. pydantic `ValidationError` → 逐条 `loc.path: msg`（loc 用点号拼接）；
  3. 其它异常（含业务校验 ValueError，其消息本身已带路径）→ 原样字符串。

纯函数、无 IO、无第三方硬依赖（pydantic 为可选导入，缺失时退化为 str），
可被 collect_json_response 的 AI 修复消息与 json_repair 修复循环共用。
"""

from __future__ import annotations

import json
from typing import Any


def format_json_issues(error: BaseException) -> list[str]:
    """把 JSON 解析/校验异常格式化为可读问题列表（至少一条）。

    Args:
        error: 捕获到的异常对象。

    Returns:
        问题描述列表；空输入场景返回单条兜底描述，绝不为空（修复提示词
        依赖「校验问题」段有内容才能定向修复）。
    """
    # 1. JSON 语法错误：带行号列号（外部标准明确要求）。
    if isinstance(error, json.JSONDecodeError):
        return [
            f"JSON 语法错误：第 {error.lineno} 行第 {error.colno} 列附近 {error.msg}。"
        ]

    # 2. Pydantic 校验错误：loc 路径点号拼接（outline.0.children.1.description）。
    try:
        from pydantic import ValidationError as _PydanticValidationError
    except Exception:  # pragma: no cover - pydantic 缺失的极端环境
        _PydanticValidationError = None  # type: ignore[assignment]
    if _PydanticValidationError is not None and isinstance(
        error, _PydanticValidationError
    ):
        issues: list[str] = []
        try:
            for item in error.errors():
                loc = item.get("loc") or ()
                path = ".".join(str(part) for part in loc) or "root"
                msg = item.get("msg", "字段校验失败")
                issues.append(f"{path}: {msg}")
        except Exception:  # pragma: no cover - errors() 结构异常的兜底
            issues = []
        return issues or [str(error)]

    # 3. 其它（业务校验 ValueError 的消息已含「第 x.y 项 …」路径信息）。
    text = str(error).strip()
    return [text] if text else [f"{type(error).__name__}（无详细错误信息）"]


def format_issues_text(error: BaseException) -> str:
    """把异常格式化为修复消息用的多行文本（每条一行，带「- 」前缀）。

    供 collect_json_response 的修复 user 消息与 json_repair 修复循环共用，
    保证两条修复链路拿到的事实描述逐字节一致。
    """
    return "\n".join(f"- {issue}" for issue in format_json_issues(error))


def format_issues_from_list(issues: list[Any] | None) -> str:
    """把既有的问题字符串列表格式化为修复消息用的多行文本。

    供调用方已有结构化问题列表（如 json_repair.validate_outline_json 的
    返回值）时使用；空/None 项一律剔除，空列表返回空串（调用方据此省略
    「校验问题」段）。
    """
    cleaned = [
        text.strip()
        for text in (str(item).strip() for item in (issues or []) if item is not None)
        if text
    ]
    return "\n".join(f"- {item}" for item in cleaned)
