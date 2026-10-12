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

# BUG-FIX-PROMPT-O5：原用 ``\w*`` 匹配语言标识，不认连字符。
# AI 输出 ```` ```chart-json ```` 时 ``\w*`` 只匹配到 ``chart``，
# ``-json`` 残留进入捕获内容。虽被 extract_json_body 的 find('{') 兜底，
# 但内容前缀污染仍可能干扰下游解析。改为 ``[\w-]*`` 兼容连字符。
_FENCE_PATTERN = re.compile(r"```(?:[\w-]*)\s*\n?(.*?)\n?```", re.DOTALL)


def strip_code_fences(text: str) -> str:
    """剥离 Markdown 代码围栏（```json ... ```）。

    如果文本中没有围栏，原样返回（仅去首尾空白）。
    BUG-FIX-PROMPT-O1：原用 ``search`` 只取**第一个**围栏。模型输出多个围栏
    块时（常见于「先给一段说明围栏，再给 JSON 围栏」），会剥错块。改为取
    **最长的**围栏内容——JSON 修复场景下承载数据的块通常最长。
    """
    if not text:
        return text
    matches = _FENCE_PATTERN.findall(text)
    if matches:
        return max((m.strip() for m in matches), key=len)
    return text.strip()


def extract_json_body(text: str) -> str:
    """从可能含多余文字的输出中提取 JSON 主体。

    策略：
    1. 先剥离代码围栏
    2. 找到第一个 { 或 [ 到最后一个 } 或 ] 的区间

    BUG-FIX-PROMPT-O2：原实现未处理「结束符出现在起始符之前」的畸形输入
    （如 ``"] junk {"``），此时 ``cleaned[start:end+1]`` 的 end < start，
    Python 静默返回**空字符串**，把「提取失败」伪装成「内容为空」，
    上层无法区分二者并给出错误提示。改为显式识别并原样返回。
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

    # BUG-FIX-PROMPT-O2：结束符早于起始符 -> 无法构成 JSON 区间，原样返回
    # 而非静默返回空串（否则「提取失败」与「内容为空」不可区分）。
    if end < start:
        return cleaned

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
            # BUG-FIX-PROMPT-O3（相对导入层级错误）：本文件位于
            # app/services/ai/prompts/，workflows_base.py 在其**父包** app/services/ai/，
            # 故必须用 `..` 而非 `.`。原写法 `from .workflows_base import ...` 必然
            # 抛 ModuleNotFoundError: No module named 'app.services.ai.prompts.workflows_base'
            # ——因异常被下面的 `except Exception` 吞掉并记 warning，
            # 导致 repair_json_output 的 AI 修复环节**静默 100% 失效**，
            # 永远返回 (None, False)，调用方无法察觉。
            # 该缺陷此前无人发现，正是因为 repair_json_output 生产调用点为 0。
            from ..workflows_base import with_timeout
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
        # Mermaid 基本校验：必须含合法的图类型声明关键字。
        # BUG-FIX-PROMPT-O4：原正则遗漏 stateDiagram/erDiagram/pie/xychart/
        # timeline/journey，而这六类在 image_engine.validate_mermaid 中均为
        # 合法类型。AI 输出这六类时本函数误报失败 → 调用方误判输出非法。
        # 现补齐全部合法类型，与 validate_mermaid 的判定范围一致。
        if not re.search(
            r"flowchart|graph|gantt|sequenceDiagram|classDiagram"
            r"|stateDiagram|erDiagram|pie|xychart|timeline|journey",
            output,
            re.IGNORECASE,
        ):
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
