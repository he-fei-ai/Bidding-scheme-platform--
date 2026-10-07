from collections import OrderedDict
import hashlib
import json
import logging
import re
import threading
import time
from typing import Any

from .prompts import get_prompt, render_prompt, sanitize_prompt_value
from .providers.base import AIMessage
from .image_styles import DEFAULT_IMAGE_STYLES, get_image_style
from .workflows import collect_json_response


# 图片生成结果缓存（基于 Prompt+Style+Size+Provider 的 MD5 哈希）
# 使用 OrderedDict 实现 LRU 淘汰，最大 100 条
# key: MD5 hexdigest, value: 图片 URL
_IMAGE_CACHE: OrderedDict[str, str] = OrderedDict()
_IMAGE_CACHE_MAX_SIZE = 100
_image_cache_lock = threading.Lock()

# 失败结果缓存（BUG 修复）：旧实现把失败结果以 None 写入 _IMAGE_CACHE，
# 但读取判断是 `if _cached_url is not None`，"键不存在"与"值为 None"无法区分，
# 失败缓存永远无法命中——每张失败配图都会重复走完整 Provider 降级链。
# 现改用独立字典记录失败时间戳，短 TTL（10 分钟）内快速失败，过期后允许重试。
_IMAGE_FAIL_CACHE: dict[str, float] = {}
_IMAGE_FAIL_TTL = 600.0


def _image_cache_set(key: str, value: str | None) -> None:
    """写入图片缓存。

    - value 为 URL：写入 LRU 成功缓存，超出最大条目数时淘汰最旧条目
    - value 为 None（生成失败）：写入短 TTL 失败缓存，避免重复失败尝试，
      同时不会永久记住瞬时故障
    """
    with _image_cache_lock:
        if value is None:
            # BUG-FIX 2026-09-16（低）：失败缓存无容量上限，写入时顺带清理
            # 过期条目，避免长期运行缓慢泄漏
            now = time.monotonic()
            if len(_IMAGE_FAIL_CACHE) >= 512:
                _expired = [k for k, ts in _IMAGE_FAIL_CACHE.items() if now - ts >= _IMAGE_FAIL_TTL]
                for k in _expired:
                    _IMAGE_FAIL_CACHE.pop(k, None)
            _IMAGE_FAIL_CACHE[key] = now
            return
        _IMAGE_FAIL_CACHE.pop(key, None)
        if key in _IMAGE_CACHE:
            _IMAGE_CACHE.move_to_end(key)
        _IMAGE_CACHE[key] = value
        while len(_IMAGE_CACHE) > _IMAGE_CACHE_MAX_SIZE:
            _IMAGE_CACHE.popitem(last=False)


def validate_mermaid(code: str) -> tuple[bool, str]:
    """基础 Mermaid 语法校验"""
    if not code or not code.strip():
        return False, "空内容"

    code = code.strip()

    # Try to detect if it's a flowchart without explicit graph prefix
    # If it contains arrows but no graph/flowchart prefix, treat as simple flowchart
    has_arrow = "-->" in code or "-- >" in code
    has_graph_prefix = code.startswith("graph") or code.startswith("flowchart")

    if has_arrow and not has_graph_prefix:
        # It looks like a simple flowchart without prefix
        # Count nodes (text segments separated by -->)
        parts = re.split(r"-->\s*", code.strip())
        node_count = len([p for p in parts if p.strip() and not p.strip().startswith(">")])

        if node_count < 2:
            return False, f"节点数不足（{node_count}<2）"

        edge_count = code.count("-->") + code.count("-- >")
        if edge_count < 1:
            return False, f"流程图连线数不足（{edge_count}<1）"

        # Fall through to normal processing with detected prefix
        code = "graph TD\n" + code

    if code.startswith("graph") or code.startswith("flowchart"):
        if "--> " not in code and "-->" not in code:
            return False, "流程图缺少连接关系"

        # For simple flowcharts detected without prefix, skip length check as they may be short
        is_simple_flowchart = code.startswith("graph TD\n") or code.startswith("flowchart TD\n")
        if not is_simple_flowchart and len(code) < 20:
            return False, "流程图代码过短"
        if "`" in code:
            return False, "流程图代码包含反引号"
        lines = code.split("\n")
        node_count = 0
        edge_count = 0
        has_semicolon = False
        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("subgraph") or stripped.startswith("end"):
                continue
            # 支持一行内多个节点：A[开始] --> B[结束]
            node_count += stripped.count("[") if "[" in stripped else 0
            node_count += stripped.count("{") if "{" in stripped else 0
            if "-->" in stripped:
                edge_count += stripped.count("-->")
            if stripped.endswith(";"):
                has_semicolon = True
        # 修复：补充统计裸标识符节点（A --> B 格式中无方括号的节点）。
        # 原实现仅统计 [ 和 { 标记的节点，导致 AI 生成的裸标识符流程图
        # （如 "graph TD\nA --> B\nB --> C"）被误判为节点数不足。
        if node_count == 0:
            _bare_nodes = set()
            for line in lines:
                s = line.strip()
                if (
                    not s
                    or s.startswith("subgraph")
                    or s.startswith("end")
                    or s.startswith("graph")
                    or s.startswith("flowchart")
                ):
                    continue
                # 只统计含箭头的行中的裸标识符，避免将首行无效类型名误计为节点
                if "-->" not in s and "-- >" not in s:
                    # 无箭头的非空行（非 directive）应视为语法错误
                    if not s.startswith("//") and not s.startswith("%%") and len(s) > 0:
                        return False, f"流程图语法错误：不支持的行 '{s[:40]}'"
                    continue
                for seg in re.split(r"\s*-->\s*", s):
                    ident = seg.strip()
                    if ident and not ident.startswith("//") and len(ident) <= 30:
                        _bare_nodes.add(ident)
            node_count = max(node_count, len(_bare_nodes))
        if node_count < 2:
            return False, f"流程图节点数不足（{node_count}<2）"
        if edge_count < 1:
            return False, f"流程图连线数不足（{edge_count}<1）"
        if "--> ... -->" in code:
            return False, "流程图存在未填充的连写箭头占位符"
        # Mermaid 原生支持方括号/花括号中的中文（如 A[开始]），无需双引号
        # 但对于包含特殊字符（逗号、括号等）的中文标签，建议使用双引号。
        # BUG 修复：① 链式箭头 A --> B --> C 是 Mermaid 原生合法语法，
        # 旧正则 -->[^-\n]*--> 会误判为"连写箭头"，触发多余的修复 AI 调用
        # 甚至丢图；仅保留字面占位符检查。② 中文与特殊字符必须是【同一个
        # 标签内】同时出现才需引号，旧实现是两个独立正则 AND，可跨标签
        # 误报；改为单括号内双 lookahead，且限制匹配不越过闭合括号。
        if re.search(
            r"\[(?=[^\]\"\n]*[\u4e00-\u9fff])(?=[^\]\"\n]*[,，、()（）])[^\]\"\n]*\]",
            code,
        ):
            return False, "流程图中文标签含特殊字符但缺少双引号"
        if re.search(
            r"\{(?=[^}\"\n]*[\u4e00-\u9fff])(?=[^}\"\n]*[,，、()（）])[^}\"\n]*\}",
            code,
        ):
            return False, "流程图菱形节点中文标签含特殊字符但缺少双引号"
        if has_semicolon:
            return False, "流程图代码包含行末分号"
        return True, "ok"

    if code.startswith("gantt"):
        lines = code.split("\n")
        has_dateformat = any("dateFormat" in l for l in lines)
        # BUG-FIX 2026-09-16（低）：删除死语句（求值结果未赋值未使用）
        has_task = False
        ids = set()
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("section "):
                pass
            elif ":" in stripped and not stripped.startswith("%") and not stripped.startswith("//"):
                parts = stripped.split(":", 1)
                task_part = parts[1].strip()
                if task_part:
                    has_task = True
                    segs = [s.strip() for s in task_part.split(",")]
                    for seg in segs:
                        if (
                            seg
                            and not seg.startswith("after")
                            and not seg.startswith("milestone")
                            and not seg.endswith("d")
                            and len(seg) <= 20
                            and " " not in seg
                        ):
                            if seg not in ("active", "done", "crit", "milestone"):
                                ids.add(seg)
        if not has_dateformat:
            return False, "甘特图缺少 dateFormat 声明"
        if not has_task:
            return False, "甘特图缺少任务定义"
        return True, "ok"

    if code.startswith("sequenceDiagram"):
        return True, "ok"

    if code.startswith("classDiagram"):
        return True, "ok"

    if code.startswith("stateDiagram") or code.startswith("stateDiagram-v2"):
        return True, "ok"

    if code.startswith("pie"):
        return True, "ok"

    if code.startswith("erDiagram"):
        return True, "ok"

    # BUG-FIX-29 修复：原 validate_mermaid 不支持 timeline 类型，
    # timeline 代码会落到兜底分支返回"不支持的 Mermaid 类型"。
    if code.startswith("timeline"):
        lines = code.split("\n")
        has_event = False
        for line in lines[1:]:
            stripped = line.strip()
            if stripped and not stripped.startswith("%") and not stripped.startswith("//"):
                if ":" in stripped or stripped.startswith("section"):
                    has_event = True
                    break
        if not has_event:
            return False, "时间线缺少事件定义"
        return True, "ok"

    return (
        False,
        f"不支持的 Mermaid 类型: {code.split(chr(10))[0] if chr(10) in code else code[:30]}",
    )


def _render_arrange_sys(
    key: str,
    *,
    section_title: str = "",
    summary: str = "",
    writing_length: str = "standard",
) -> str:
    """渲染「配图编排」系统提示词。

    BUG-FIX 2026-09-14（中）：编排类提示词模板带 __SECTION_TITLE__ / __SUMMARY__ /
    __WRITING_LENGTH__ 占位符，但调用方原先是 `get_prompt(KEY)` 不带任何 kwargs，
    模板原样发给模型（框架日志一直在报 missing variables）。
    模型因此拿不到章节标题与写作长度，无法按 short/standard/detailed 调整配图粒度。
    """
    try:
        rendered = get_prompt(
            key,
            section_title=section_title or "（未提供章节标题）",
            summary=summary or "（未提供项目概述）",
            writing_length=writing_length or "standard",
        )
    except Exception as e:  # 渲染异常不能影响配图主流程
        logger.warning("[配图编排] 提示词渲染失败 key=%s: %s", key, e)
        rendered = ""
    return rendered or get_prompt(key)


def repair_mermaid(code: str) -> str:
    """修复常见 Mermaid 语法问题

    BUG-FIX 2026-10-04（图表模块 D6）：原实现仅做围栏去除/行尾空白/空行折叠，
    不处理 AI 高频输出问题（BOM、智能引号、全角标点、缺图类型头、括号不配对、
    未闭合链接形状）。现补充机械化修复；修复后仍非法的块由调用方删除（validate）。
    """
    if not code:
        return ""

    code = code.strip()

    # 去 UTF-8 BOM（Windows 模型输出偶带）
    code = code.lstrip("\ufeff")

    code = re.sub(r"```mermaid\s*\n?", "", code)
    code = re.sub(r"\n?```", "", code)
    # 残留的内联反引号（代码块嵌套错位）：行内 `x` → x
    code = re.sub(r"`([^`\n]*)`", r"\1", code)

    # 智能引号 → ASCII（Mermaid 不识别 “ ” 作为字符串定界符）
    code = code.replace("\u201c", '"').replace("\u201d", '"')
    code = code.replace("\u2018", "'").replace("\u2019", "'")

    # 引号配对：奇数个双引号 → 删最后一个（AI 截断行尾多补的引号）
    _dq = code.count('"')
    if _dq % 2 == 1:
        _last = code.rfind('"')
        _line_start = code.rfind("\n", 0, _last) + 1
        # 仅当孤立引号所在行非注释行时删除
        if not code[_line_start:_line_start + 2].startswith("%%"):
            code = code[:_last] + code[_last + 1:]

    # 全角括号/花括号归一化（节点形状声明处）：仅对「形状括号含全角且无引号保护」
    # 的行做替换，引号内的标签文本（可能合法包含中文括号）保持不变。
    _shape_lines = []
    for _ln in code.split("\n"):
        if '"' not in _ln and ("（" in _ln or "）" in _ln or "【" in _ln or "】" in _ln or "｛" in _ln or "｝" in _ln):
            _ln = _ln.replace("（", "[").replace("）", "]") \
                .replace("【", "[").replace("】", "]") \
                .replace("｛", "{").replace("｝", "}")
        _shape_lines.append(_ln)
    code = "\n".join(_shape_lines)

    # 行末分号剔除（validate 拒绝行末半角分号；全角；同步剔除）
    code = re.sub(r"[;；][ \t]*$", "", code, flags=re.MULTILINE)

    code = re.sub(r"[ \t]+$", "", code, flags=re.MULTILINE)

    code = re.sub(r"\n{3,}", "\n\n", code)

    # 形状括号配对：[ （ 多于 ] → 行末补齐（仅处理节点声明行，跳过注释）
    _fixed_lines = []
    for _ln in code.split("\n"):
        _s = _ln.strip()
        if (
            _s
            and not _s.startswith("%%")
            and not _s.startswith("subgraph")
            and _s.count("[") > _s.count("]")
            and '-->' not in _s
            and _s.count("]") == 0
            and _s.count("[") == 1
            and _s.endswith("]") is False
        ):
            # 单节点定义行缺右括号：在末尾（剔除尾部全角后）补 ]
            _ln = _ln.rstrip() + "]"
        _fixed_lines.append(_ln)
    code = "\n".join(_fixed_lines)

    # 方括号配对：[ 多于 ] 且均为节点形状行 → 逐行补齐
    _open_sq = code.count("[") - code.count("]")
    if _open_sq > 0:
        lines_check = code.split("\n")
        _sq_fixed = []
        for _ln in lines_check:
            _s = _ln.rstrip()
            if _s.count("[") > _s.count("]") and not _s.strip().startswith("%%"):
                _s += "]" * (_s.count("[") - _s.count("]"))
            _sq_fixed.append(_s)
        code = "\n".join(_sq_fixed)

    # 缺图类型头：含箭头但首行非类型关键字 → 补 graph TD（与 validate 的
    # 「无头流程图」识别同源，确保 repair 后能进入合法分支）
    _first = code.split("\n", 1)[0].strip().lower()
    _known_heads = (
        "graph", "flowchart", "gantt", "sequencediagram", "classdiagram",
        "statediagram", "pie", "erdiagram", "timeline", "xychart", "journey",
    )
    if "-->" in code and not _first.startswith(_known_heads):
        code = "graph TD\n" + code

    # 全角冒号归一化：只剔除行尾孤立的中文冒号（标签内的中文冒号不在行尾，不受影响）
    code = re.sub(r"[ \t]*：[ \t]*$", "", code, flags=re.MULTILINE)

    lines = code.split("\n")
    cleaned = []
    for line in lines:
        if not line.strip():
            cleaned.append("")
            continue
        if "graph" in line or "flowchart" in line or "gantt" in line:
            cleaned.append(line.strip())
        else:
            cleaned.append(line.rstrip())
    code = "\n".join(cleaned).strip()

    return code


async def generate_flowchart_arrange(
    provider: Any,
    *,
    section_title: str,
    section_content: str,
    project_overview: str = "",
    scoring_standard: str = "",
    industry_profile: str = "",
    writing_length: str = "standard",
) -> dict:
    """AI 流程图编排判断 — 判断章节是否需要流程图以及类型、节点数、流向等"""
    # BUG-FIX 2026-09-14（中）：模板含 __SECTION_TITLE__/__SUMMARY__/__WRITING_LENGTH__，原先不带 kwargs 直接发送，
    # 模型收到的是字面量占位符（框架已告警 missing variables）。改为渲染后发送。
    arr_sys = _render_arrange_sys(
        "FLOWCHART_ARRANGE_SYSTEM",
        section_title=section_title,
        summary=project_overview,
        writing_length=writing_length,
    )

    user_prompt = f"""章节标题：{section_title}

章节内容：
{section_content}

项目概述：
{project_overview or "未提供项目概述"}

评分标准：
{scoring_standard or "未提供评分标准"}

行业背景：
{industry_profile or "未提供行业背景"}

请根据以上信息判断该章节是否需要生成流程图，并返回JSON格式的判断结果。"""

    messages = [
        AIMessage(role="system", content=arr_sys),
        AIMessage(role="user", content=user_prompt),
    ]

    result = await collect_json_response(provider, messages, max_retries=3)
    return result


async def chart_data_fix(
    provider: Any,
    *,
    chart_type: str,
    section_title: str,
    section_content: str,
    error_message: str = "",
    original_data: str = "",
) -> str | dict:
    """图表修复兜底 — 任意图表生成失败后调用"""
    fix_sys = get_prompt("CHART_DATA_FIX")

    user_prompt = f"""图表类型：{chart_type}

章节标题：{section_title}

章节内容：
{section_content}

错误信息：
{error_message or "未提供错误信息"}

原始数据：
{original_data or "未提供原始数据"}

请根据以上信息生成降级可用的图表数据。"""

    messages = [
        AIMessage(role="system", content=fix_sys),
        AIMessage(role="user", content=user_prompt),
    ]

    result = await collect_json_response(provider, messages, max_retries=2)
    return result


async def generate_layout_arrange(
    provider: Any,
    *,
    section_title: str,
    section_content: str,
    project_overview: str = "",
    scoring_standard: str = "",
    industry_profile: str = "",
) -> dict:
    """AI 布局图编排判断"""
    # BUG-FIX 2026-09-14（中）：模板含 __SECTION_TITLE__/__SUMMARY__，原先不带 kwargs 直接发送，
    # 模型收到的是字面量占位符（框架已告警 missing variables）。改为渲染后发送。
    arr_sys = _render_arrange_sys(
        "LAYOUT_ARRANGE_SYSTEM",
        section_title=section_title,
        summary=project_overview,
    )

    user_prompt = f"""章节标题：{section_title}

章节内容：
{section_content}

项目概述：
{project_overview or "未提供项目概述"}

评分标准：
{scoring_standard or "未提供评分标准"}

行业背景：
{industry_profile or "未提供行业背景"}

请根据以上信息判断该章节是否需要生成布局图，并返回JSON格式的判断结果。"""

    messages = [
        AIMessage(role="system", content=arr_sys),
        AIMessage(role="user", content=user_prompt),
    ]

    result = await collect_json_response(provider, messages, max_retries=3)
    return result


async def generate_layout_mermaid(
    provider: Any,
    *,
    section_title: str,
    section_content: str,
    project_overview: str = "",
    arrange_result: dict | None = None,
) -> str:
    """AI 生成布局图 Mermaid 代码"""
    if not arrange_result or not arrange_result.get("needed"):
        return ""

    gen_sys = get_prompt("LAYOUT_GENERATE_SYSTEM")

    # SECURITY-FIX 2026-10-06：section_content / project_overview 为用户内容，做元标记中和。
    safe_title = sanitize_prompt_value(section_title)
    safe_content = sanitize_prompt_value(section_content)
    safe_overview = sanitize_prompt_value(project_overview or "未提供项目概述")
    user_prompt = f"""章节标题：{safe_title}

章节内容：
{safe_content}

项目概述：
{safe_overview}

编排结果：
{json.dumps(arrange_result, ensure_ascii=False, indent=2)}

请根据以上信息生成Mermaid布局图代码。"""

    messages = [
        AIMessage(role="system", content=gen_sys),
        AIMessage(role="user", content=user_prompt),
    ]

    raw = await with_timeout(provider.chat(messages, temperature=0.7), timeout=30)
    if "```" in raw:
        m = re.search(r"```(?:mermaid)?\s*\n(.*?)\n```", raw, re.DOTALL)
        if m:
            raw = m.group(1)
    raw = repair_mermaid(raw)
    return raw.strip()


async def generate_timeline_arrange(
    provider: Any,
    *,
    section_title: str,
    section_content: str,
    project_overview: str = "",
    scoring_standard: str = "",
    industry_profile: str = "",
) -> dict:
    """AI 里程碑时间线编排判断"""
    # BUG-FIX 2026-09-14（中）：模板含 __SECTION_TITLE__/__SUMMARY__，原先不带 kwargs 直接发送，
    # 模型收到的是字面量占位符（框架已告警 missing variables）。改为渲染后发送。
    arr_sys = _render_arrange_sys(
        "TIMELINE_ARRANGE_SYSTEM",
        section_title=section_title,
        summary=project_overview,
    )

    user_prompt = f"""章节标题：{section_title}

章节内容：
{section_content}

项目概述：
{project_overview or "未提供项目概述"}

评分标准：
{scoring_standard or "未提供评分标准"}

行业背景：
{industry_profile or "未提供行业背景"}

请根据以上信息判断该章节是否需要生成里程碑时间线图，并返回JSON格式的判断结果。"""

    messages = [
        AIMessage(role="system", content=arr_sys),
        AIMessage(role="user", content=user_prompt),
    ]

    result = await collect_json_response(provider, messages, max_retries=3)
    return result


async def generate_timeline_mermaid(
    provider: Any,
    *,
    section_title: str,
    section_content: str,
    project_overview: str = "",
    arrange_result: dict | None = None,
) -> str:
    """AI 生成里程碑时间线 Mermaid 代码"""
    if not arrange_result or not arrange_result.get("needed"):
        return ""

    gen_sys = get_prompt("TIMELINE_GENERATE_SYSTEM")

    # SECURITY-FIX 2026-10-06：section_content / project_overview 为用户内容，做元标记中和。
    safe_title = sanitize_prompt_value(section_title)
    safe_content = sanitize_prompt_value(section_content)
    safe_overview = sanitize_prompt_value(project_overview or "未提供项目概述")
    user_prompt = f"""章节标题：{safe_title}

章节内容：
{safe_content}

项目概述：
{safe_overview}

编排结果：
{json.dumps(arrange_result, ensure_ascii=False, indent=2)}

请根据以上信息生成Mermaid时间线代码。"""

    messages = [
        AIMessage(role="system", content=gen_sys),
        AIMessage(role="user", content=user_prompt),
    ]

    raw = await with_timeout(provider.chat(messages, temperature=0.7), timeout=30)
    if "```" in raw:
        m = re.search(r"```(?:mermaid)?\s*\n(.*?)\n```", raw, re.DOTALL)
        if m:
            raw = m.group(1)
    raw = repair_mermaid(raw)
    return raw.strip()


async def generate_comparison_arrange(
    provider: Any,
    *,
    section_title: str,
    section_content: str,
    project_overview: str = "",
    scoring_standard: str = "",
    industry_profile: str = "",
) -> dict:
    """AI 对比图编排判断"""
    # BUG-FIX 2026-09-14（中）：模板含 __SECTION_TITLE__/__SUMMARY__，原先不带 kwargs 直接发送，
    # 模型收到的是字面量占位符（框架已告警 missing variables）。改为渲染后发送。
    arr_sys = _render_arrange_sys(
        "COMPARISON_ARRANGE_SYSTEM",
        section_title=section_title,
        summary=project_overview,
    )

    user_prompt = f"""章节标题：{section_title}

章节内容：
{section_content}

项目概述：
{project_overview or "未提供项目概述"}

评分标准：
{scoring_standard or "未提供评分标准"}

行业背景：
{industry_profile or "未提供行业背景"}

请根据以上信息判断该章节是否需要生成对比图，并返回JSON格式的判断结果。"""

    messages = [
        AIMessage(role="system", content=arr_sys),
        AIMessage(role="user", content=user_prompt),
    ]

    result = await collect_json_response(provider, messages, max_retries=3)
    return result


async def generate_comparison_mermaid(
    provider: Any,
    *,
    section_title: str,
    section_content: str,
    project_overview: str = "",
    arrange_result: dict | None = None,
) -> str:
    """AI 生成对比图 Mermaid 代码（Pie / xychart-beta）"""
    if not arrange_result or not arrange_result.get("needed"):
        return ""

    gen_sys = get_prompt("COMPARISON_GENERATE_SYSTEM")

    # SECURITY-FIX 2026-10-06：section_content / project_overview 为用户内容，做元标记中和。
    safe_title = sanitize_prompt_value(section_title)
    safe_content = sanitize_prompt_value(section_content)
    safe_overview = sanitize_prompt_value(project_overview or "未提供项目概述")
    user_prompt = f"""章节标题：{safe_title}

章节内容：
{safe_content}

项目概述：
{safe_overview}

编排结果：
{json.dumps(arrange_result, ensure_ascii=False, indent=2)}

请根据以上信息生成Mermaid对比图代码。"""

    messages = [
        AIMessage(role="system", content=gen_sys),
        AIMessage(role="user", content=user_prompt),
    ]

    raw = await with_timeout(provider.chat(messages, temperature=0.7), timeout=30)
    if "```" in raw:
        m = re.search(r"```(?:mermaid)?\s*\n(.*?)\n```", raw, re.DOTALL)
        if m:
            raw = m.group(1)
    raw = repair_mermaid(raw)
    return raw.strip()


async def generate_mermaid_flowchart(
    provider: Any,
    *,
    section_title: str,
    section_content: str,
    scoring_standard: str,
    industry_profile: str,
    project_overview: str = "",
    arrange_result: dict | None = None,
) -> str:
    """AI 生成 Mermaid 流程图"""
    logger.info(
        "[配图生成] Mermaid流程图: title=%s, content_len=%d", section_title, len(section_content)
    )
    fc_sys = get_prompt("MERMAID_FLOWCHART_SYSTEM")

    # PERF-FIX-2026-09-08: get_prompt(..., **kwargs) 直接完成渲染，消除误报 missing variables
    arrange_info = "未提供编排信息"
    if arrange_result and arrange_result.get("needed"):
        arrange_info = json.dumps(arrange_result, ensure_ascii=False, indent=2)

    # SECURITY-FIX 2026-10-06：section_content / scoring_standard / project_overview
    # 均来自招标文件解析与正文生成（用户/外部内容），注入前做元标记中和，
    # 与 workflows_analysis.py 高风险链路口径对齐。
    fc_user = get_prompt(
        "MERMAID_FLOWCHART_USER",
        section_title=section_title,
        section_content=section_content,
        scoring_standard=scoring_standard,
        industry_profile=industry_profile,
        project_overview=project_overview or "未提供项目概述",
        arrange_info=arrange_info,
        sanitize=True,
    )
    logger.info(
        "[配图生成] Mermaid流程图 提示词加载: SYSTEM=%d字, USER=%d字", len(fc_sys), len(fc_user)
    )

    messages = [
        AIMessage(role="system", content=fc_sys),
        AIMessage(role="user", content=fc_user),
    ]

    raw_content = await with_timeout(provider.chat(messages, temperature=0.7), timeout=30)

    if "```" in raw_content:
        code_match = re.search(r"```(?:mermaid)?\s*\n(.*?)\n```", raw_content, re.DOTALL)
        if code_match:
            raw_content = code_match.group(1)

    raw_content = repair_mermaid(raw_content)

    valid, msg = validate_mermaid(raw_content)
    if not valid:
        mermaid_repair = get_prompt("MERMAID_REPAIR_PROMPT")
        repair_messages = [
            AIMessage(role="system", content=mermaid_repair),
            AIMessage(
                role="user", content=f"原始 Mermaid 代码：\n{raw_content}\n\n校验问题：{msg}"
            ),
        ]
        try:
            repaired = await with_timeout(
                provider.chat(repair_messages, temperature=0.3), timeout=30
            )
            if "```" in repaired:
                code_match = re.search(r"```(?:mermaid)?\s*\n(.*?)\n```", repaired, re.DOTALL)
                if code_match:
                    repaired = code_match.group(1)
            repaired = repair_mermaid(repaired)
            if validate_mermaid(repaired)[0]:
                return repaired
        except Exception as _e:
            logger.debug("[silent-except] image_engine.py: line 628 - %s", _e)
        # BUG-FIX-30 修复：修复失败后返回空字符串而非原始无效代码，
        # 避免下游渲染器对无效代码浪费资源必然失败。
        return ""

    return raw_content


async def generate_mermaid_gantt(
    provider: Any,
    *,
    section_title: str,
    section_content: str,
    scoring_standard: str,
    industry_profile: str,
    summary: str = "",
    arrange_result: dict | None = None,
) -> str:
    """AI 生成 Mermaid 甘特图

    Args:
        arrange_result: 编排结果（含 phaseCountHint/estimatedPeak/taskDependencyHint），
            供甘特图生成时对齐阶段数与工期分配，提升图表与章节语义的一致性。
    """
    logger.info(
        "[配图生成] Mermaid甘特图: title=%s, content_len=%d", section_title, len(section_content)
    )
    gt_sys = get_prompt("MERMAID_GANTT_SYSTEM")

    # PERF-FIX-2026-09-08: get_prompt(..., **kwargs) 直接完成渲染，消除误报 missing variables
    arrange_info = "未提供编排信息"
    if arrange_result and arrange_result.get("needed"):
        arrange_info = json.dumps(arrange_result, ensure_ascii=False, indent=2)

    # SECURITY-FIX 2026-10-06：与流程图同理，注入用户内容前做元标记中和。
    gt_user = get_prompt(
        "MERMAID_GANTT_USER",
        section_title=section_title,
        section_content=section_content,
        scoring_standard=scoring_standard,
        industry_profile=industry_profile,
        summary=summary or "未提供项目概述，请根据章节内容和行业经验合理推断工期。",
        arrange_info=arrange_info,
        sanitize=True,
    )
    logger.info(
        "[配图生成] Mermaid甘特图 提示词加载: SYSTEM=%d字, USER=%d字", len(gt_sys), len(gt_user)
    )

    messages = [
        AIMessage(role="system", content=gt_sys),
        AIMessage(role="user", content=gt_user),
    ]

    raw_content = await with_timeout(provider.chat(messages, temperature=0.7), timeout=30)

    if "```" in raw_content:
        code_match = re.search(r"```(?:mermaid)?\s*\n(.*?)\n```", raw_content, re.DOTALL)
        if code_match:
            raw_content = code_match.group(1)

    raw_content = repair_mermaid(raw_content)

    valid, msg = validate_mermaid(raw_content)
    if not valid:
        mermaid_repair2 = get_prompt("MERMAID_REPAIR_PROMPT")
        repair_messages = [
            AIMessage(role="system", content=mermaid_repair2),
            AIMessage(
                role="user", content=f"原始 Mermaid 代码：\n{raw_content}\n\n校验问题：{msg}"
            ),
        ]
        try:
            repaired = await with_timeout(
                provider.chat(repair_messages, temperature=0.3), timeout=30
            )
            if "```" in repaired:
                code_match = re.search(r"```(?:mermaid)?\s*\n(.*?)\n```", repaired, re.DOTALL)
                if code_match:
                    repaired = code_match.group(1)
            repaired = repair_mermaid(repaired)
            if validate_mermaid(repaired)[0]:
                return repaired
        except Exception as _e:
            logger.debug("[silent-except] image_engine.py: line 710 - %s", _e)
        # BUG-FIX-30 修复：修复失败后返回空字符串而非原始无效代码
        return ""

    return raw_content


async def generate_illustration_prompt(
    provider: Any,
    *,
    section_title: str,
    section_content: str,
    section_path: str,
    industry_profile: str,
) -> dict:
    """AI 生成配图描述（图片生成提示词）

    Returns { "prompt": "...", "style": "...", "description": "..." }
    """
    logger.info("[配图方案] 开始: title=%s, path=%s", section_title, section_path)
    ill_sys = get_prompt("ILLUSTRATION_PLAN_SYSTEM")
    logger.info("[配图方案] 提示词加载: SYSTEM=%d字", len(ill_sys))
    ill_user = (
        f"请为以下章节规划一张配图方案：\n\n"
        f"## 章节路径\n{section_path}\n\n"
        f"## 章节标题\n{section_title}\n\n"
        f"## 章节正文（摘要）\n{section_content[:2000] if section_content else '（暂无正文）'}\n\n"
        f"## 行业背景\n{industry_profile or '通用工程'}\n\n"
        f"请严格按照系统提示词中的 JSON 格式输出。"
    )

    messages = [
        AIMessage(role="system", content=ill_sys),
        AIMessage(role="user", content=ill_user),
    ]

    result = await collect_json_response(provider, messages, max_retries=3)
    return result


async def generate_illustration_arrange(
    provider: Any,
    *,
    section_title: str,
    section_content: str,
    project_overview: str = "",
    scoring_standard: str = "",
    industry_profile: str = "",
) -> dict:
    """AI 编排判断 — 该章节是否需要配图及图像风格偏好

    返回包含 {"needed": bool, "style": string, "reason": string} 的判断结果
    """
    logger.info("[配图编排] 插图: title=%s, content_len=%d", section_title, len(section_content))
    arr_sys = get_prompt("ILLUSTRATION_ARRANGE_SYSTEM")
    logger.info("[配图编排] 插图 提示词加载: %d字", len(arr_sys))

    user_prompt = f"""章节标题：{section_title}

章节内容：
{section_content}

项目概述：
{project_overview or "未提供项目概述"}

评分标准：
{scoring_standard or "未提供评分标准"}

行业背景：
{industry_profile or "未提供行业背景"}

请根据以上信息判断该章节是否需要配图插图，如果需要，建议使用的图像风格（engineering_diagram/realistic_photo/technical_drawing/custom），并说明理由。返回JSON格式的判断结果。"""

    messages = [
        AIMessage(role="system", content=arr_sys),
        AIMessage(role="user", content=user_prompt),
    ]

    result = await collect_json_response(provider, messages, max_retries=3)
    # 确保返回必要的字段
    if result and isinstance(result, dict):
        result.setdefault("needed", False)
        result.setdefault("style", "engineering_diagram")
        result.setdefault("reason", "")
    return result


async def generate_illustration_image(
    provider: Any,
    prompt: str,
    style: str = "engineering_diagram",
    section_size: str = "16:9",
    provider_name: str = "volcengine",
    image_config: dict | None = None,
) -> str | None:
    """使用配置的图像模型提供商生成实际图片

    支持 Provider 降级链：主 Provider → qwen_image → dashscope → agnes。
    当主 Provider 调用失败时，自动尝试下一个备选 Provider。
    全部失败则返回 None，不阻塞调用方。

    内置基于 MD5 的图片生成缓存，相同 Prompt 不会重复生成。

    Args:
        provider: 文本 AI 提供商实例（仅用于兜底获取 API 配置）
        prompt: 图片生成提示词
        style: 图像风格（engineering_diagram/realistic_photo/technical_drawing/custom）
        section_size: 尺寸比例（16:9/1:1/9:16/4:3/3:4）
        provider_name: 图像提供商标识（agnes/dashscope/hunyuan 等）
        image_config: 图像模型配置（model_configs 表 image_* 字段），
                      优先于此处的 provider 实例；应包含 image_api_key_enc /
                      image_base_url / image_model / image_default_size 等

    Returns:
        生成的图片 URL，失败返回 None
    """
    from .image_providers.registry import ProviderRegistry

    # ---- 图片生成缓存（基于 MD5，含 provider_name 避免切换 provider 后误命中） ----
    _cache_key = hashlib.md5(
        f"{prompt}|{style}|{section_size}|{provider_name}".encode()
    ).hexdigest()
    with _image_cache_lock:
        _cached_url = _IMAGE_CACHE.get(_cache_key)
        if _cached_url is not None:
            # 命中缓存，将 key 移到末尾（LRU 刷新）
            _IMAGE_CACHE.move_to_end(_cache_key)
            logger.info("generate_illustration_image: 命中缓存 (key=%s)", _cache_key[:12])
            return _cached_url
        # BUG 修复：失败缓存命中检查。旧实现失败结果（None）永远无法命中，
        # 每张失败配图都会重复走完整 Provider 降级链（多次网络调用）。
        # 现在短 TTL（10 分钟）内的失败直接快速返回 None，过期后允许重试。
        _fail_ts = _IMAGE_FAIL_CACHE.get(_cache_key)
        if _fail_ts is not None:
            if time.monotonic() - _fail_ts < _IMAGE_FAIL_TTL:
                logger.info(
                    "generate_illustration_image: 命中失败缓存，跳过重试 (key=%s)",
                    _cache_key[:12],
                )
                return None
            _IMAGE_FAIL_CACHE.pop(_cache_key, None)

    def _resolve_api_key(cfg: dict) -> str:
        """从图像配置中解析 API Key（兼容明文与 ENC: 加密前缀）"""
        raw = (cfg or {}).get("image_api_key_enc") or (cfg or {}).get("api_key") or ""
        if not raw:
            return ""
        try:
            from ..crypto import decrypt_api_key, is_encrypted

            if is_encrypted(raw):
                return decrypt_api_key(raw)
        except Exception as e:
            logger.debug("图像 API Key 解密失败，按原样使用: %s", e)
        return raw

    def _resolve_base_url(cfg: dict) -> str:
        """归一化 base_url，去掉误填的 /images/generations 端点后缀"""
        url = (cfg or {}).get("image_base_url") or (cfg or {}).get("base_url") or ""
        if not url:
            return ""
        return re.sub(r"/(?:dashscope/)?images/generations$", "", url.rstrip("/"))

    # 优先使用图像专用配置
    if image_config and image_config.get("image_enabled", True):
        _fallback_api_key = getattr(provider, "api_key", "") or ""
        _cfg_api_key = _resolve_api_key(image_config) or _fallback_api_key
        _cfg_base_url = _resolve_base_url(image_config) or getattr(provider, "base_url", "") or ""
        _cfg_model = image_config.get("image_model") or getattr(provider, "model", "default-model")
        _cfg_default_size = image_config.get("image_default_size", "2K")
        _cfg_price = image_config.get("image_price_per_image", 0.0)
    else:
        # 没有图像配置时回退到 provider 实例（文本模型配置，仅作兜底）
        _cfg_api_key = getattr(provider, "api_key", "") or ""
        _cfg_base_url = getattr(provider, "base_url", "") or ""
        _cfg_model = getattr(provider, "model", "default-model")
        _cfg_default_size = "2K"
        _cfg_price = 0.0

    # 降级链定义：主 Provider → 备选1 → 备选2
    _FALLBACK_CHAIN = ["qwen_image", "dashscope", "agnes"]

    # 如果传入的 provider_name 不在降级链中，将其作为第一优先级
    _provider_queue: list[str] = []
    if provider_name and provider_name not in _FALLBACK_CHAIN:
        _provider_queue.append(provider_name)
    _provider_queue.extend(_FALLBACK_CHAIN)

    # 已尝试过的 provider（避免重复）
    _tried_providers: set[str] = set()

    # 映射 size 参数到适配器期望的像素尺寸格式（dashscope 风格：仅 size 字段）
    _size_map = {
        "16:9": "1920x1080",
        "1:1": "1024x1024",
        "9:16": "1024x1792",
        "4:3": "1024x768",
        "3:4": "768x1024",
        "2:3": "768x1024",
        "3:2": "1024x768",
        "21:9": "1920x810",
    }
    _pixel_size = _size_map.get(section_size, "1024x1024")

    # 注入风格约束（ENHANCE 2026-10-05 / P-16：文案正本见 image_styles.py）
    _final_prompt = prompt
    if style != "custom":
        _style_prefix = get_image_style(style, include_fallback=False)
        if _style_prefix:
            _final_prompt = f"{_style_prefix}\n{prompt}"

    # 尝试降级链中的每个 Provider
    _last_error: str | None = None
    for _pname in _provider_queue:
        if _pname in _tried_providers:
            continue
        _tried_providers.add(_pname)

        try:
            # --- 处理特殊 provider: qwen_image ---
            # qwen_image 不是注册的 provider ID，它使用 dashscope 适配器 + qwen-image 模型
            _actual_provider = _pname
            _model_override = None
            if _pname == "qwen_image":
                _actual_provider = "dashscope"
                _model_override = "qwen-image-3.0-pro"

            # 从图像配置构建适配器配置（优先图像 key，而非文本模型 key）
            _adapter_config = {
                "api_key": _cfg_api_key,
                "base_url": _cfg_base_url,
                "model": _model_override or _cfg_model,
                "default_size": _cfg_default_size,
                "default_ratio": section_size,
                "timeout": 60,
                "price_per_image": _cfg_price,
            }

            # 获取适配器实例（注意：get_adapter 需要传入 config）
            _adapter = ProviderRegistry.get_adapter(_actual_provider, _adapter_config)

            # 尺寸语义自适应：
            # - 适配器支持 1K/2K + ratio 风格（agnes 等）→ 传默认尺寸 + 宽高比
            # - 适配器只接受像素尺寸（dashscope 等）→ 传宽x高像素值
            _supported_sizes = getattr(_adapter, "supported_sizes", None)
            if _supported_sizes and _cfg_default_size in _supported_sizes:
                _size_arg, _ratio_arg = _cfg_default_size, section_size
            else:
                _size_arg, _ratio_arg = _pixel_size, None

            # 调用适配器生成图像
            _result = await _adapter.generate(
                prompt=_final_prompt,
                model=_adapter_config.get("model", ""),
                size=_size_arg,
                ratio=_ratio_arg,
            )

            # 提取图片 URL
            _image_url = (
                _result.get("image_url")
                or _result.get("url")
                or (
                    _result.get("data", [{}])[0].get("url")
                    if isinstance(_result.get("data"), list)
                    else None
                )
            )

            if not _image_url and isinstance(_result, dict):
                for _key in ["url", "image", "imageUrl", "picture"]:
                    if _key in _result:
                        _image_url = _result[_key]
                        break

            if _image_url:
                logger.info(
                    "generate_illustration_image: Provider '%s' 生成成功 (原始: '%s')",
                    _actual_provider,
                    _pname,
                )
                # 写入缓存
                _image_cache_set(_cache_key, _image_url)
                return _image_url

            _last_error = f"Provider '{_pname}' 返回空 URL"
            logger.warning(
                "generate_illustration_image: Provider '%s' 返回空结果，尝试下一个",
                _pname,
            )

        except Exception as e:
            _last_error = f"Provider '{_pname}' 异常: {e}"
            logger.warning(
                "generate_illustration_image: Provider '%s' 失败 (%s)，尝试下一个",
                _pname,
                e,
            )
            continue

    # 所有 Provider 均失败
    logger.error(
        "generate_illustration_image: 所有 Provider 均失败，prompt='%s...', last_error=%s",
        prompt[:80],
        _last_error,
    )
    # 缓存失败结果（避免重复失败尝试）
    _image_cache_set(_cache_key, None)
    return None


def build_illustration_url(prompt: str, image_size: str = "landscape_16_9") -> str:
    """构建配图 URL（使用 text-to-image API）"""
    import urllib.parse

    encoded_prompt = urllib.parse.quote(prompt)
    return f"https://trae-api-cn.mchost.guru/api/ide/v1/text_to_image?prompt={encoded_prompt}&image_size={image_size}"


async def generate_labor_arrange(
    provider: Any,
    *,
    section_title: str,
    section_content: str,
    project_overview: str = "",
    scoring_standard: str = "",
    industry_profile: str = "",
    writing_length: str = "standard",
) -> dict:
    """AI 劳动力图表编排判断 — 判断章节是否需要劳动力图及其阶段数、峰值等"""
    logger.info(
        "[配图编排] 劳动力图: title=%s, content_len=%d", section_title, len(section_content)
    )
    # BUG-FIX 2026-09-14（中）：模板含 __SECTION_TITLE__/__SUMMARY__/__WRITING_LENGTH__，原先不带 kwargs 直接发送，
    # 模型收到的是字面量占位符（框架已告警 missing variables）。改为渲染后发送。
    arr_sys = _render_arrange_sys(
        "LABOR_ARRANGE_SYSTEM",
        section_title=section_title,
        summary=project_overview,
        writing_length=writing_length,
    )
    logger.info("[配图编排] 劳动力图 提示词加载: %d字", len(arr_sys))

    user_prompt = f"""章节标题：{section_title}

章节内容：
{section_content}

项目概述：
{project_overview or "未提供项目概述"}

评分标准：
{scoring_standard or "未提供评分标准"}

行业背景：
{industry_profile or "未提供行业背景"}

请根据以上信息判断该章节是否需要生成劳动力图表，并返回JSON格式的判断结果。"""

    messages = [
        AIMessage(role="system", content=arr_sys),
        AIMessage(role="user", content=user_prompt),
    ]

    result = await collect_json_response(provider, messages, max_retries=3)
    # 确保返回必要的字段
    if result and isinstance(result, dict):
        result.setdefault("needed", False)
        result.setdefault("phases", [])
        result.setdefault("categories", [])
        result.setdefault("data", [])
    return result


async def generate_labor_data(
    provider: Any,
    *,
    section_title: str,
    section_content: str,
    project_overview: str = "",
    arrange_result: dict | None = None,
    writing_length: str = "standard",
    industry_profile: str = "",
) -> dict:
    """AI 生成劳动力图表数据（LaborPlan JSON）"""
    if not arrange_result or not arrange_result.get("needed"):
        return {"needed": False, "type": "labor", "phases": [], "categories": [], "data": []}

    gen_sys = get_prompt("LABOR_GENERATE_SYSTEM")

    # SECURITY-FIX 2026-10-06：section_content / project_overview 为用户内容，做元标记中和。
    safe_title = sanitize_prompt_value(section_title)
    safe_content = sanitize_prompt_value(section_content)
    safe_overview = sanitize_prompt_value(project_overview or "未提供项目概述")
    user_prompt = f"""章节标题：{safe_title}

章节内容：
{safe_content}

项目概述：
{safe_overview}

编排结果：
{json.dumps(arrange_result, ensure_ascii=False, indent=2)}

写作长度：{writing_length}

行业背景：
{industry_profile or "未提供行业背景"}

请根据以上信息生成LaborPlan格式的劳动力图表JSON数据。"""

    messages = [
        AIMessage(role="system", content=gen_sys),
        AIMessage(role="user", content=user_prompt),
    ]

    result = await collect_json_response(provider, messages, max_retries=3)
    return result


async def generate_architecture_arrange(
    provider: Any,
    *,
    section_title: str,
    section_content: str,
    project_overview: str = "",
    scoring_standard: str = "",
    industry_profile: str = "",
) -> dict:
    """AI 架构图编排判断 — 判断章节是否需要架构图，以及类型、优先级等"""
    # BUG-FIX 2026-09-14（中）：模板含 __SECTION_TITLE__/__SUMMARY__，原先不带 kwargs 直接发送，
    # 模型收到的是字面量占位符（框架已告警 missing variables）。改为渲染后发送。
    arr_sys = _render_arrange_sys(
        "ARCHITECTURE_ARRANGE_SYSTEM",
        section_title=section_title,
        summary=project_overview,
    )

    user_prompt = f"""章节标题：{section_title}

章节内容：
{section_content}

项目概述：
{project_overview or "未提供项目概述"}

评分标准：
{scoring_standard or "未提供评分标准"}

行业背景：
{industry_profile or "未提供行业背景"}

请根据以上信息判断该章节是否需要生成架构图，并返回JSON格式的判断结果。"""

    messages = [
        AIMessage(role="system", content=arr_sys),
        AIMessage(role="user", content=user_prompt),
    ]

    result = await collect_json_response(provider, messages, max_retries=3)
    return result


async def generate_architecture_tree(
    provider: Any,
    *,
    section_title: str,
    section_content: str,
    project_overview: str = "",
    arrange_result: dict | None = None,
    writing_length: str = "standard",
    industry_profile: str = "",
) -> dict:
    """AI 生成架构图递归节点树（ArchNode JSON）"""
    if not arrange_result or not arrange_result.get("needed"):
        return {"id": "root", "label": "", "children": []}

    gen_sys = get_prompt("ARCHITECTURE_GENERATE_SYSTEM")

    # SECURITY-FIX 2026-10-06：section_content / project_overview 为用户内容，做元标记中和。
    safe_title = sanitize_prompt_value(section_title)
    safe_content = sanitize_prompt_value(section_content)
    safe_overview = sanitize_prompt_value(project_overview or "未提供项目概述")
    user_prompt = f"""章节标题：{safe_title}

章节内容：
{safe_content}

项目概述：
{safe_overview}

编排结果：
{json.dumps(arrange_result, ensure_ascii=False, indent=2)}

写作长度：{writing_length}

行业背景：
{industry_profile or "未提供行业背景"}

请根据以上信息生成ArchNode格式的架构图JSON树。"""

    messages = [
        AIMessage(role="system", content=gen_sys),
        AIMessage(role="user", content=user_prompt),
    ]

    result = await collect_json_response(provider, messages, max_retries=3)
    return result


# ============================================================
# V7.0 新增：批量编排函数（单次 AI 调用替代 7 次独立编排）
# ============================================================
def _apply_chart_hints(arrangements: dict, chart_hints: dict | None) -> dict:
    """把正文中的「配图位置提示」并入批量编排结果。

    语义（CHART-MARKER 2026-09-16）：
      正文里写下的 `<<CHART:类型>>` 是**作者（AI）在写作时就已确定的配图意图**，
      优先级高于编排阶段的二次判断 —— 因此对提示命中的类型：
        1. 强制 `needed=True`（并记录 `fromHint` 便于审计）；
        2. 缺失的编排条目按默认值补齐，保证后续生成阶段能拿到完整参数；
        3. 把提示中的标题透传为 `hintTitle`，供落库时作图表标题使用。

    该函数为纯函数（不修改入参之外的状态），便于单测。
    """
    if not isinstance(arrangements, dict) or not chart_hints:
        return arrangements

    from ..content_cleaning import CHART_TYPES

    for ctype, title in (chart_hints or {}).items():
        ctype = str(ctype or "").strip().lower()
        if ctype not in CHART_TYPES:
            continue
        item = arrangements.get(ctype)
        if not isinstance(item, dict):
            item = {"type": "none", "priority": 4, "reason": ""}
            arrangements[ctype] = item
        if not item.get("needed"):
            logger.info("[批量编排] 正文配图提示强制生成: %s（原判定 needed=false）", ctype)
            item["needed"] = True
        item["fromHint"] = True
        if not item.get("priority") or item.get("priority", 0) < 4:
            item["priority"] = 4
        if title:
            item["hintTitle"] = str(title)[:24]
        if not item.get("reason"):
            item["reason"] = "正文已标注该配图位置"
    return arrangements


async def generate_all_chart_arrangements(
    provider: Any,
    *,
    section_title: str,
    section_content: str,
    project_overview: str = "",
    scoring_standard: str = "",
    industry_profile: str = "",
    chart_hints: dict | None = None,
) -> dict[str, dict]:
    """单次 AI 调用批量编排所有图表类型 — 替代 7 次独立编排调用

    返回 { chart_type: { needed, type, priority, reason, ... } }，
    其中 needed=False 的类型不纳入后续生成流程。

    Args:
        chart_hints: 正文「配图位置提示」{chart_type: title}。
            命中的类型**强制** needed=True（见 `_apply_chart_hints`），
            确保正文标注了配图位置的章节一定会产出真实图表数据。

    使用示例：
        arrangements = await generate_all_chart_arrangements(
            provider,
            section_title="外墙保温施工工艺",
            section_content="...",
            chart_hints={"gantt": "", "flowchart": ""},
        )
    """
    _time = __import__("time")
    t0 = _time.perf_counter()
    logger.info(
        "[批量编排] 开始 章节=%s 内容长度=%d 概述长度=%d 正文配图提示=%s",
        section_title,
        len(section_content),
        len(project_overview),
        sorted((chart_hints or {}).keys()),
    )

    batch_arrange_sys = """# 批量图表编排（系统提示词）— V8.0 商业级优化版
> **设计目标**：单次 AI 调用同时判断所有 7 种图表类型的适配性，
> 确保后续 v2 商业级渲染器获得高质量数据（2x 超采样 + 300 DPI 输出）。

### 角色
精通施工组织设计与标书图形化表达的资深 BIM 咨询专家，熟悉商业级图表渲染标准。

### 核心原则
1. **商业级质量优先**：只有数据充分、结构清晰的章节才配图，宁缺毋滥
2. **v2 渲染器适配**：输出数据必须适合 v2 商业级渲染器（递归节点树、多阶段矩阵、标准化坐标）
3. **无降级妥协**：数据不足时标记 needed=false，而非生成低质量占位图
4. **评分导向**：评分标准明确要求配图时，强制对应图表 needed=true
5. **无降级渲染**：渲染管线仅走 Level 1.5 (HTTP Service 2x) → Level 2 (PIL v2 2x超采样+300 DPI) 双路径，不降级使用 mmdc (~96 DPI) 或 v1 渲染器（无抗锯齿）

### 输出格式（最高优先级）
直接返回 JSON 对象，严禁在 JSON 前后附加任何解释文字或 Markdown 围栏。
```json
{
  "flowchart":    { "needed": true, "type": "construction", "priority": 5, "reason": "...", "nodeCountHint": 6, "flowHint": "td" },
  "gantt":        { "needed": false, "type": "none", "priority": 3, "reason": "...", "nodeCountHint": 0, "flowHint": "none" },
  "architecture": { "needed": false, "type": "none", "priority": 3, "reason": "...", "nodeCountHint": 0, "depthHint": 0 },
  "labor":        { "needed": false, "type": "none", "priority": 3, "reason": "...", "phases": [], "categories": [] },
  "comparison":   { "needed": false, "type": "none", "priority": 3, "reason": "..." },
  "layout":       { "needed": false, "type": "none", "priority": 3, "reason": "..." },
  "timeline":     { "needed": false, "type": "none", "priority": 3, "reason": "..." }
}
```
字段说明：
- `needed`: true/false
- `type`: 子类型或 `"none"`
- `priority`: 3/4/5
- `reason`: 判断依据（不超过60字）

### 判定原则
1. **互斥优先**：同一章节最多2种图表（优先 flowchart+gantt 或 architecture+flowchart）
2. **类型互斥**：施工工序→flowchart；工期计划→gantt；组织管理→architecture
3. **克制生成**：仅1-3步简单操作→全部 needed=false
4. **评分导向**：评分标准要求"流程图"/"进度计划"→强制对应图表 needed=true
5. **数据充分性**：架构图至少4个节点、劳动力图至少2阶段×3工种、对比图至少3组数据——不足则 needed=false

### 快速判定表
| 章节特征 | 强推荐图表 | 拒绝图表 |
|----------|-----------|----------|
| 施工工艺/工序/步骤（≥4步） | flowchart | gantt/architecture |
| 进度计划/工期/里程碑（≥5任务） | gantt | flowchart |
| 组织机构/职责/WBS（≥4节点） | architecture | flowchart |
| 劳动力配置/人员安排（≥2阶段×3工种） | labor | flowchart |
| 资源对比/方案比选（≥3组数据） | comparison | gantt |
| 平面布置/场地划分（≥3功能区） | layout | flowchart |
| 关键节点/里程碑（≥3节点） | timeline | gantt |

### 数据质量要求（v2 渲染器适配 — 2x 超采样 + 300 DPI 输出）
| 图表类型 | 最小数据量 | 数据特征要求 |
|----------|-----------|-------------|
| flowchart | 4节点+3连线 | 有明确先后顺序，含条件分支更佳；节点标签含具体施工工序名 |
| gantt | 5任务+1里程碑 | 有明确工期和依赖关系；任务名称体现具体工程内容 |
| architecture | 1根节点+3子节点 | 递归树结构，label 含具体岗位/模块名；深度≥2层 |
| labor | 2阶段×3工种 | 人数呈"先升后降"规律；数据矩阵维度完整 |
| comparison | 3组数据 | 饼图总和=100%，柱状图有明确对比；数据单位为工程常用指标 |
| layout | 3个功能区 | 坐标不重叠，有连接关系；区域名含具体功能描述 |
| timeline | 3个里程碑 | 日期有序，状态合理分配；里程碑描述含具体验收标准 |"""

    # === 正文配图位置提示（CHART-MARKER 2026-09-16）===
    # 正文写作阶段已用 `<<CHART:类型>>` 标定了配图意图与位置，这类意图优先于
    # 编排阶段的二次判断；这里显式告知模型，避免它把已标注的类型判为 needed=false
    # 导致「正文标注了配图位置、最终却没有图」。
    _hints = {str(k).lower(): v for k, v in (chart_hints or {}).items() if k}
    if _hints:
        _hint_lines = "\n".join(
            f"- {ct}：{title or '（正文已标注该配图位置，标题由你根据章节内容拟定）'}"
            for ct, title in sorted(_hints.items())
        )
        batch_arrange_sys += (
            "\n\n### 正文已标注的配图意图（最高优先级，必须 needed=true）\n"
            "下列图表类型在正文写作阶段已被作者显式标注为配图对象，"
            "**无论你的通用判定如何，这些类型的 `needed` 必须为 true**，"
            "并为其补充准确的 `type`（子类型）与 `priority`：\n"
            f"{_hint_lines}"
        )
        _hint_user_block = f"\n**正文已标注配图**：{', '.join(sorted(_hints))}\n"
    else:
        _hint_user_block = ""

    user_prompt = f"""## 章节信息
**章节标题**：{section_title}

**章节内容**：
{section_content[:2000] if section_content else "（暂无正文内容）"}
{_hint_user_block}
## 项目信息
**项目概述**：{project_overview or "未提供项目概述"}
**评分标准**：{scoring_standard or "未提供评分标准"}
**行业背景**：{industry_profile or "未提供行业背景"}

请根据以上信息，同时判断所有7种图表类型的适配性，返回JSON对象。"""

    messages = [
        AIMessage(role="system", content=batch_arrange_sys),
        AIMessage(role="user", content=user_prompt),
    ]

    result = await collect_json_response(provider, messages, max_retries=3)
    elapsed = __import__("time").perf_counter() - t0
    logger.info("[批量编排] AI 调用完成 耗时=%.2fs 原始结果类型=%s", elapsed, type(result).__name__)
    # 确保返回7个key，缺失的补默认值
    if isinstance(result, dict):
        for _t, _default in [
            (
                "flowchart",
                {
                    "needed": False,
                    "type": "none",
                    "priority": 3,
                    "reason": "",
                    "nodeCountHint": 0,
                    "flowHint": "none",
                },
            ),
            (
                "gantt",
                {
                    "needed": False,
                    "type": "none",
                    "priority": 3,
                    "reason": "",
                    "nodeCountHint": 0,
                    "flowHint": "none",
                },
            ),
            (
                "architecture",
                {
                    "needed": False,
                    "type": "none",
                    "priority": 3,
                    "reason": "",
                    "nodeCountHint": 0,
                    "depthHint": 0,
                },
            ),
            (
                "labor",
                {
                    "needed": False,
                    "type": "none",
                    "priority": 3,
                    "reason": "",
                    "phases": [],
                    "categories": [],
                },
            ),
            ("comparison", {"needed": False, "type": "none", "priority": 3, "reason": ""}),
            ("layout", {"needed": False, "type": "none", "priority": 3, "reason": ""}),
            ("timeline", {"needed": False, "type": "none", "priority": 3, "reason": ""}),
        ]:
            result.setdefault(_t, _default)
        # 正文配图提示命中类型强制 needed=true（放在补齐默认值之后，保证字段完整）
        if _hints:
            result = _apply_chart_hints(result, _hints)
        needed = [k for k, v in result.items() if isinstance(v, dict) and v.get("needed")]
        logger.info("[批量编排] 完成 耗时=%.2fs 需生成图表=%s", elapsed, needed)
    else:
        logger.warning("[批量编排] 返回非 dict 结果: %r", result)
    return result


# ============================================================
# V7.0 更新：图像生成服务（ImageGenerationService）
# ============================================================
import os as _os
import tempfile


try:
    import requests as _requests
except ImportError:
    _requests = None  # type: ignore

logger = logging.getLogger(__name__)


class ImageGenerationService:
    """V7.0 图像生成服务 - 统一接口，支持多图像模型提供商

    用于在标书正文中插入 AI 生成的配图（如施工现场示意图、设备布置图等）。

    设计特点：
        - 支持 volcengine, openai, dashscope 等多种后端
        - 自动适配不同厂商的 API 格式
        - 向后兼容旧版调用方式

    使用示例：
        gen = ImageGenerationService(provider, "volcengine")
        url = gen.generate("施工现场平面布置示意图", style="engineering_diagram")
        if url:
            download_and_embed_image(url, doc)
    """

    # 全局风格约束（确保生成的图片适合投标技术方案）
    # ENHANCE 2026-10-05（P-16）：文案正本收敛到 image_styles.DEFAULT_IMAGE_STYLES，
    # 此处保留同名类属性以兼容既有 `self.STYLE_PROMPTS[...]` 读取方式。
    STYLE_PROMPTS = DEFAULT_IMAGE_STYLES

    def __init__(self, provider: Any, provider_name: str = "volcengine"):
        """Args:
        provider: AI 提供商实例（需有 chat 接口用于生成 prompt）
        provider_name: 图像模型提供商名称（volcengine/openai 等）
        """
        self.provider = provider
        self.provider_name = provider_name
        self._client = None

    def _init_client(self):
        """初始化图像生成客户端（懒加载）"""
        if self._client is not None:
            return

        if self.provider_name == "volcengine":
            self._init_volcengine()
        elif self.provider_name == "openai":
            self._init_openai()

    def _init_volcengine(self):
        """初始化火山引擎 Seedream 客户端"""
        try:
            from volcengine.visual.VisualService import VisualService

            self._client = VisualService()
            ak = getattr(self.provider, "api_key", "") or _os.environ.get("VOLC_ACCESS_KEY", "")
            sk = getattr(self.provider, "api_secret", "") or _os.environ.get("VOLC_SECRET_KEY", "")
            if ak and sk:
                self._client.set_ak(ak)
                self._client.set_sk(sk)
            self._model = "seedream-2.0"
        except ImportError:
            logger.warning("AIMageGenerator: volcengine SDK 未安装，图像生成功能不可用")
            self._client = None

    def _init_openai(self):
        """初始化 OpenAI DALL-E 客户端"""
        try:
            import openai as _openai

            api_key = getattr(self.provider, "api_key", "") or _os.environ.get("OPENAI_API_KEY", "")
            base_url = getattr(self.provider, "base_url", "") or _os.environ.get(
                "OPENAI_BASE_URL", ""
            )
            self._client = _openai.OpenAI(api_key=api_key, base_url=base_url or None)
            self._model = "dall-e-3"
        except ImportError:
            logger.warning("AIMageGenerator: openai SDK 未安装，图像生成功能不可用")
            self._client = None

    def generate(
        self,
        prompt: str,
        style: str = "engineering_diagram",
        size: str = "1024x1024",
    ) -> str | None:
        """生成 AI 图片，返回图片 URL 或本地路径

        Args:
            prompt: 图片描述提示词
            style: 风格（engineering_diagram/realistic_photo/technical_drawing）
            size: 图片尺寸

        Returns:
            图片 URL 或本地路径，失败返回 None
        """
        # ENHANCE 2026-10-05（P-16）：走唯一正本，支持 IMAGE_STYLE_OVERRIDE 覆盖
        style_prompt = get_image_style(style)
        full_prompt = f"{style_prompt}\n{prompt}"

        self._init_client()
        if self._client is None:
            logger.warning("AIMageGenerator: 图像生成客户端未初始化")
            return None

        try:
            if self.provider_name == "volcengine":
                return self._generate_volcengine(full_prompt, size)
            elif self.provider_name == "openai":
                return self._generate_openai(full_prompt, size)
            else:
                return build_illustration_url(full_prompt)
        except Exception as e:
            logger.error("AIMageGenerator: 图像生成失败 (%s): %s", self.provider_name, e)
            return None

    def _generate_volcengine(self, prompt: str, size: str) -> str | None:
        """火山引擎 Seedream 图像生成"""
        try:
            form = {
                "req_key": self._model,
                "prompt": prompt,
                "model_version": "general_v2.0",
                "width": 1024,
                "height": 1024,
            }
            resp = self._client.cv_process(form)
            if resp and resp.get("code") == 10000:
                image_urls = resp.get("data", {}).get("image_urls", [])
                return image_urls[0] if image_urls else None
            logger.warning("AIMageGenerator: 火山引擎返回错误: %s", resp)
            return None
        except Exception as e:
            logger.error("AIMageGenerator: 火山引擎调用失败: %s", e)
            return None

    def _generate_openai(self, prompt: str, size: str) -> str | None:
        """OpenAI DALL-E 图像生成"""
        try:
            response = self._client.images.generate(
                model=self._model,
                prompt=prompt,
                size=size,
                quality="standard",
                n=1,
            )
            return response.data[0].url if response.data else None
        except Exception as e:
            logger.error("AIMageGenerator: OpenAI 调用失败: %s", e)
            return None

    async def generate_async(
        self,
        prompt: str,
        style: str = "engineering_diagram",
        size: str = "1024x1024",
    ) -> str | None:
        """异步生成 AI 图片（使用线程池避免阻塞）"""
        import asyncio

        return await asyncio.to_thread(self.generate, prompt, style, size)


def download_and_embed_image(
    url: str,
    doc,
    width_cm: float = 14.0,
    caption: str = "",
    timeout: int = 30,
) -> bool:
    """V6.0 下载图片并嵌入到 DOCX 文档

    Args:
        url: 图片 URL
        doc: python-docx Document 对象
        width_cm: 图片宽度（厘米）
        caption: 图片标题（可选，显示在图片下方）
        timeout: 下载超时（秒）

    Returns:
        bool: 是否成功嵌入
    """
    if not url or _requests is None:
        return False

    img_path = None
    # BUG-FIX 2026-09-15（高，SSRF）：拒绝拉取内网/保留地址
    from app.utils.ssrf import is_safe_http_url
    if not is_safe_http_url(url):
        logger.warning("拒绝嵌入非安全图片 URL（疑似内网地址，存在 SSRF 风险）: %s", url)
        return False

    try:
        # BUG-FIX 2026-09-16（低）：用 with 管理流式响应，异常路径（非 200 提前
        # return、写入抛异常）下连接也能确定归还连接池，不再依赖 GC
        with _requests.get(url, timeout=timeout, stream=True) as response:
            if response.status_code != 200:
                logger.warning(
                    "download_and_embed_image: HTTP %s for %s", response.status_code, url[:80]
                )
                return False

            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)
                img_path = f.name

        if _os.path.getsize(img_path) < 100:
            logger.warning("download_and_embed_image: 下载的图片过小 (%s)", img_path)
            return False

        from docx.enum.text import WD_ALIGN_PARAGRAPH
        from docx.shared import Cm, Pt, RGBColor

        paragraph = doc.add_paragraph()
        paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
        run = paragraph.add_run()
        run.add_picture(img_path, width=Cm(width_cm))

        if caption:
            caption_para = doc.add_paragraph()
            caption_para.alignment = WD_ALIGN_PARAGRAPH.CENTER
            caption_run = caption_para.add_run(caption)
            caption_run.font.size = Pt(10)
            caption_run.font.color.rgb = RGBColor(0x66, 0x66, 0x66)

        logger.info("download_and_embed_image: 成功嵌入图片 (%s)", url[:80])
        return True

    except Exception as e:
        logger.error("download_and_embed_image: 下载/嵌入失败: %s", e)
        return False
    finally:
        if img_path and _os.path.exists(img_path):
            try:
                _os.unlink(img_path)
            except Exception as _e:
                logger.debug("[silent-except] image_engine.py: line 1633 - %s", _e)


# ============================================================
# V6.0 新增：内联图表检测器（InlineChartDetector）
# ============================================================
class InlineChartDetector:
    """V6.0 内联图表检测器 - 在预编排阶段识别适合生成图表的段落

    在正文生成前，通过 AI 判断当前章节是否适合生成数据图表（柱状图、饼图、
    折线图等），从而在预编排阶段就做好图表规划。

    使用示例：
        detector = InlineChartDetector(provider)
        result = await detector.detect(
            chapter_info={"title": "资源配备计划", "description": "..."},
            facts={"project_name": "XX大厦", "total_budget": 5000},
        )
        if result["needed"]:
            chart_data = result["chart_type"]
    """

    CHART_KEYWORDS = {
        "bar": ["对比", "比较", "方案比选", "成本对比", "资源对比", "性能对比", "指标对比"],
        "pie": ["比例", "占比", "分布", "构成", "份额", "分配", "组成"],
        "line": ["趋势", "变化", "增长", "曲线", "波动", "走势", "预测", "时间序列"],
        "table": ["数据", "统计", "汇总", "清单", "明细", "台账"],
    }

    def __init__(self, provider: Any | None = None):
        """Args:
        provider: AI 提供商实例（可选；quick_detect 无需 provider，detect 需要）
        """
        self.provider = provider

    def quick_detect(self, chapter_title: str, chapter_description: str = "") -> dict:
        """快速检测（基于关键词，无需 AI 调用）

        Returns:
            {"needed": bool, "chart_type": str, "confidence": str}
        """
        text = f"{chapter_title} {chapter_description}".lower()
        result = {"needed": False, "chart_type": "", "confidence": "low"}

        for chart_type, keywords in self.CHART_KEYWORDS.items():
            for kw in keywords:
                if kw in text:
                    result["needed"] = True
                    result["chart_type"] = chart_type
                    result["confidence"] = "medium"
                    return result

        return result

    async def detect(
        self,
        chapter_info: dict,
        facts: dict,
        section_content: str = "",
    ) -> dict:
        """AI 深度检测 - 判断当前章节是否适合生成内联数据图表

        Returns:
            {"needed": bool, "chart_type": str, "data_source": str, "reason": str}
        """
        quick = self.quick_detect(
            chapter_info.get("title", ""),
            chapter_info.get("description", ""),
        )
        if not quick["needed"]:
            return {
                "needed": False,
                "chart_type": "",
                "data_source": "",
                "reason": "关键词检测未命中",
            }

        detect_sys = """你是标书编制专家，擅长判断哪些章节内容适合生成数据图表。

判断标准：
1. 包含多组对比数据（如不同方案的成本对比）→ 适合柱状图/对比图
2. 包含趋势分析（如工期-资源投入曲线）→ 适合折线图
3. 包含比例分布（如资金分配比例）→ 适合饼图
4. 包含结构化数据（如设备清单）→ 适合表格

返回 JSON：{"needed": true/false, "chart_type": "bar/pie/line/table", "data_source": "数据来源说明", "reason": "判断理由"}"""

        user_prompt = f"""章节标题：{chapter_info.get('title', '')}
章节描述：{chapter_info.get('description', '')}
项目事实：{json.dumps(facts, ensure_ascii=False, indent=2)[:2000]}

请判断该章节是否适合生成内联数据图表，返回JSON格式的判断结果。"""

        messages = [
            AIMessage(role="system", content=detect_sys),
            AIMessage(role="user", content=user_prompt),
        ]

        try:
            result = await collect_json_response(self.provider, messages, max_retries=2)
            if result and isinstance(result, dict):
                result.setdefault("needed", False)
                result.setdefault("chart_type", quick["chart_type"])
                result.setdefault("data_source", "")
                result.setdefault("reason", "")
                return result
        except Exception as e:
            logger.warning("InlineChartDetector: AI 检测失败: %s", e)

        return quick

    async def detect_all(
        self,
        chapters: list[dict],
        facts: dict,
    ) -> dict[str, dict]:
        """批量检测多个章节的图表需求

        Returns:
            {章节标题: 检测结果, ...}
        """
        results = {}
        for ch in chapters:
            title = ch.get("title", "")
            if title:
                results[title] = await self.detect(ch, facts)
        return results


# ============================================================
# Backward compatibility: AIMageGenerator is an alias for ImageGenerationService
# ============================================================
AIMageGenerator = ImageGenerationService


# ============================================================
# 图片生成缓存管理
# ============================================================


def get_image_cache_stats() -> dict:
    """获取图片生成缓存统计信息"""
    with _image_cache_lock:
        hit_count = sum(1 for v in _IMAGE_CACHE.values() if v is not None)
        miss_count = sum(1 for v in _IMAGE_CACHE.values() if v is None)
        return {
            "total": len(_IMAGE_CACHE),
            "hit": hit_count,
            "miss": miss_count,
            "keys": list(_IMAGE_CACHE.keys()),
        }


def clear_image_cache() -> None:
    """清空图片生成缓存"""
    with _image_cache_lock:
        _IMAGE_CACHE.clear()
    logger.info("generate_illustration_image: 图片生成缓存已清空")


# AI调用超时装饰器
async def with_timeout(coro, timeout: int = 30, error_msg: str = "AI调用超时"):
    """带超时的AI调用装饰器"""
    import asyncio

    try:
        return await asyncio.wait_for(coro, timeout=timeout)
    except TimeoutError:
        raise TimeoutError(f"{error_msg}（{timeout}秒）")
