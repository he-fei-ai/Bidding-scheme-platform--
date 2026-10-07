"""统一输出格式修复工具（2026-10-06）。

将分散在各模块中的输出校验+修复逻辑抽象为统一入口：
- 剥离 Markdown 代码围栏
- 提取 JSON 主体
- 调用 AI 修复（按需）
- 多轮降级策略

调用方只需传入原始输出和期望格式，即可获得修复后的结果。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from ._cache import get_prompt


logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────
# 1. 静态修复（无需 AI，纯文本处理）
# ──────────────────────────────────────────────────────────────────────

_FENCE_PATTERN = re.compile(r"```(?:\w*)\s*\n?(.*?)\n?```", re.DOTALL)


def strip_code_fences(text: str) -> str:
    """剥离 Markdown 代码围栏（```json ... ```）。

    如果文本中没有围栏，原样返回。
    """
    if not text:
        return text
    match = _FENCE_PATTERN.search(text)
    if match:
        return match.group(1).strip()
    return text.strip()


def extract_json_body(text: str) -> str:
    """从可能含多余文字的输出中提取 JSON 主体。

    策略：
    1. 先剥离代码围栏
    2. 找到第一个 { 或 [ 到最后一个 } 或 ] 的区间
    """
    if not text:
        return text
    cleaned = strip_code_fences(text)

    # 找到第一个 JSON 起始符
    start_obj = cleaned.find("{")
    start_arr = cleaned.find("[")
    if start_obj == -1 and start_arr == -1:
        return cleaned
    if start_obj == -1:
        start = start_arr
    elif start_arr == -1:
        start = start_obj
    else:
        start = min(start_obj, start_arr)

    # 找到最后一个 JSON 结束符
    end_obj = cleaned.rfind("}")
    end_arr = cleaned.rfind("]")
    if end_obj == -1 and end_arr == -1:
        return cleaned[start:]
    if end_obj == -1:
        end = end_arr
    elif end_arr == -1:
        end = end_obj
    else:
        end = max(end_obj, end_arr)

    return cleaned[start : end + 1]


def try_parse_json(text: str) -> tuple[Any | None, str | None]:
    """尝试解析 JSON，返回 (解析结果, 错误信息)。

    成功时错误信息为 None；失败时结果为 None，错误信息为字符串。
    """
    if not text:
        return None, "空输入"
    try:
        data = json.loads(text)
        return data, None
    except json.JSONDecodeError as e:
        return None, str(e)


# ──────────────────────────────────────────────────────────────────────
# 2. 统一输出修复入口
# ──────────────────────────────────────────────────────────────────────

async def repair_json_output(
    provider: Any,
    raw_output: str,
    *,
    error_info: str = "",
    target_schema: str = "",
    max_retries: int = 1,
) -> tuple[Any | None, bool]:
    """统一 JSON 输出修复入口。

    流程：
    1. 静态修复：剥离围栏 + 提取 JSON 主体
    2. 尝试解析：成功则直接返回
    3. AI 修复：解析失败则调用 JSON_REPAIR_PROMPT 修复

    Args:
        provider: AI provider 实例
        raw_output: AI 原始输出
        error_info: 校验失败的错误信息
        target_schema: 目标 Schema 描述
        max_retries: AI 修复最大重试次数

    Returns:
        (解析后的数据, 是否成功)
    """
    if not raw_output:
        return None, False

    # Step 1: 静态修复
    json_text = extract_json_body(raw_output)

    # Step 2: 尝试解析
    data, err = try_parse_json(json_text)
    if data is not None:
        return data, True

    # Step 3: AI 修复
    repair_prompt = get_prompt("JSON_REPAIR_PROMPT")
    if not repair_prompt:
        logger.warning("JSON_REPAIR_PROMPT 未注册，无法 AI 修复")
        return None, False

    from ..providers.base import AIMessage

    for attempt in range(max_retries):
        user_msg = f"""原始输出：
{raw_output[:2000]}

错误信息：
{error_info or err}

目标 Schema：
{target_schema or '无'}"""

        messages = [
            AIMessage(role="system", content=repair_prompt),
            AIMessage(role="user", content=user_msg),
        ]

        try:
            from .workflows_base import with_timeout
            repaired = await with_timeout(provider.chat(messages, max_tokens=4096), timeout=30)
            repaired_text = extract_json_body(repaired or "")
            data, err = try_parse_json(repaired_text)
            if data is not None:
                logger.info("JSON 修复成功（第 %d 轮）", attempt + 1)
                return data, True
        except Exception as e:
            logger.warning("JSON 修复第 %d 轮失败: %s", attempt + 1, e)

    return None, False


def validate_output_format(output: str, expected_type: str = "json") -> tuple[bool, str]:
    """校验输出格式是否符合预期。

    Args:
        output: 待校验的输出文本
        expected_type: 期望格式（json / mermaid / text）

    Returns:
        (是否通过, 错误描述)
    """
    if not output or not output.strip():
        return False, "输出为空"

    if expected_type == "json":
        json_text = extract_json_body(output)
        data, err = try_parse_json(json_text)
        if data is None:
            return False, f"JSON 解析失败: {err}"
        return True, ""

    elif expected_type == "mermaid":
        # Mermaid 基本校验：必须含 flowchart/graph/gantt 等关键字
        if not re.search(r"flowchart|graph|gantt|sequenceDiagram|classDiagram", output, re.IGNORECASE):
            return False, "未找到 Mermaid 图类型声明"
        return True, ""

    return True, ""


__all__ = [
    "strip_code_fences",
    "extract_json_body",
    "try_parse_json",
    "repair_json_output",
    "validate_output_format",
]
