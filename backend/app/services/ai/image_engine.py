import hashlib
import json
import logging
import os as _os
import re
import tempfile
import threading
import time
from typing import Any

try:  # requests 在精简部署环境可能缺失，缺失时 download_and_embed_image 直接降级
    import requests as _requests
except ImportError:  # pragma: no cover
    _requests = None

from .prompts import get_prompt, render_prompt, sanitize_prompt_value
from .providers.base import AIMessage
from .image_styles import DEFAULT_IMAGE_STYLES, get_image_style
from .workflows import collect_json_response

logger = logging.getLogger(__name__)

# ILLUSTRATION-REMOVAL 2026-11-04（遗留项3）：已移除 _IMAGE_CACHE/_IMAGE_FAIL_CACHE
# 生图结果缓存（唯一消费方 generate_illustration_image 已删）。
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

        # BUG-FIX 2026-11-02（方向偏见·短流程图误杀）：旧实现只豁免 "graph TD\n" /
        # "flowchart TD\n" 前缀，导致其他合法方向的最小流程图（如 "graph LR\nC-->D"，
        # 13 字符）被误判"流程图代码过短" → _fence_payload_to_marker_text 返回 None →
        # extract_section_charts 静默丢图。LR 是招投标流程图最常见方向，影响面大。
        # 改为按 Mermaid 全部合法方向（TD/TB/BT/RL/LR）统一豁免长度门；退化输入
        # 仍由后续 node_count<2 / edge_count<1 / 语法错误行等结构检查兜底拦截。
        is_simple_flowchart = bool(
            re.match(r"(?:graph|flowchart)\s+(?:TD|TB|BT|RL|LR)\b", code)
        )
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
        # BUG-FIX 2026-10-17（P1·菱形括号不配对漏检）：AI 截断常产出 B{判断 这类
        # 未闭合菱形节点。旧实现只数节点数不查括号配对 → 校验通过 → 渲染层拿到
        # 非法 Mermaid 报错 → 整图静默丢失。此处剔除双引号字符串后比对计数，
        # 避免误伤 A["{x"}] 这类标签内合法花括号。
        _no_quoted = re.sub(r'"[^"\n]*"', "", code)
        if _no_quoted.count("{") != _no_quoted.count("}"):
            return False, "流程图菱形节点花括号不配对（如 B{判断 缺少右括号）"
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

    # BUG-FIX 2026-10-17（P1·xychart/journey 被误杀）：xychart/xychart-beta（柱状/
    # 折线对比图）与 journey（用户旅程图）是 Mermaid 合法类型，且
    # _fence_payload_to_marker_text 已把 xychart 归入 comparison 通道、journey 归入
    # flowchart 通道，validate_output_format（BUG-FIX-PROMPT-O4）也把二者列为合法。
    # 但本函数不支持 → 围栏路径 repair 无法修复 → 整块被判「坏块」**静默删除**，
    # AI 辛苦生成的对比图/旅程图从成品文档中消失。补齐轻量结构校验。
    if code.startswith("xychart"):
        # xychart-beta 合法形式：x [类目] / y 轴声明 / bar|line|histogram 数据系列
        _xy_lines = code.split("\n")
        _has_x_axis = any(
            re.match(r"^\s*x\s+\[", l) or re.match(r"^\s*x\s", l) for l in _xy_lines
        )
        _has_series = any(
            re.match(r"^\s*(bar|line|histogram)\s*\[", l) for l in _xy_lines
        )
        if not (_has_x_axis and _has_series):
            return False, "xychart 缺少 x 轴类目或 bar/line 数据系列声明"
        return True, "ok"

    if code.startswith("journey"):
        # journey 合法形式：title / section / 「事件: 分数: 描述」
        _has_event = False
        for line in code.split("\n")[1:]:
            stripped = line.strip()
            if (
                stripped
                and not stripped.startswith(("%", "//", "title", "section"))
                and ":" in stripped
            ):
                _has_event = True
                break
        if not _has_event:
            return False, "journey 缺少事件定义"
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

    UNIFY-R2b（2026-11-04，家族约定）：编排 system 模板族（charts.py 五个
    *_ARRANGE_SYSTEM + mermaid.py FLOWCHART_ARRANGE_SYSTEM）的 ``{SUMMARY}``
    槽位承载的**语义是项目概述**（调用方统一传 project_overview 的值）。该约定
    为全家族一致设计（6/6 模板同拼法），与 user 侧模板族的
    ``{PROJECT_OVERVIEW}`` 拼法不同但各自内部一致——勿单独改动某一个，
    否则跨模板复用 kwarg 集时会踩错名（R2a 只统一了 GANTT/FLOWCHART 两个
    姊妹 USER 模板间的分裂）。
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


def _render_arrange_user(
    chart_label: str,
    *,
    section_title: str = "",
    section_content: str = "",
    project_overview: str = "",
    scoring_standard: str = "",
    industry_profile: str = "",
) -> str:
    """渲染「配图编排」**用户**提示词（原 7 处逐字重复的硬编码 f-string）。

    ENHANCE 2026-10-18（审计遗留 D2 批次 2 收编）：
      - 模板收编为注册表 key ``CHART_ARRANGE_USER``，末句图表名词抽成
        ``{CHART_LABEL}``，管理页从此可见/可覆盖/可版本化；
      - **同时修复安全口径不一致**：原先 7 处均为裸 f-string 插值
        （无 sanitize_prompt_value、无 sanitize=True），而同文件组 B 的
        3 处却做了元标记中和。现统一 ``sanitize=True``，与组 B 及
        workflows_analysis 的既有口径对齐。

    空值兜底文案（「未提供项目概述」等）由本函数上移处理，渲染结果与原实现
    逐字节一致（见 tests/test_prompts_adoption_2026_10_18.py 的等价断言）。
    """
    try:
        rendered = get_prompt(
            "CHART_ARRANGE_USER",
            CHART_LABEL=chart_label,
            section_title=section_title,
            section_content=section_content,
            project_overview=project_overview or "未提供项目概述",
            scoring_standard=scoring_standard or "未提供评分标准",
            industry_profile=industry_profile or "未提供行业背景",
            sanitize=True,
        )
    except Exception as e:  # 渲染异常不能影响配图主流程
        logger.warning("[配图编排] 用户提示词渲染失败: %s", e)
        rendered = ""
    if rendered:
        return rendered
    # 兜底：DB/注册表均不可用时的最小 fallback（保留原硬编码形态）
    return (
        f"章节标题：{section_title}\n\n章节内容：\n{section_content}\n\n项目概述：\n"
        f"{project_overview or '未提供项目概述'}\n\n评分标准：\n"
        f"{scoring_standard or '未提供评分标准'}\n\n行业背景：\n"
        f"{industry_profile or '未提供行业背景'}\n\n"
        f"请根据以上信息判断该章节是否需要生成{chart_label}，并返回JSON格式的判断结果。"
    )


def _render_mermaid_gen_user(
    chart_kind: str,
    *,
    section_title: str = "",
    section_content: str = "",
    project_overview: str = "",
    arrange_result: dict | None = None,
) -> str:
    """渲染 Mermaid 图生成的**用户**提示词（原 3 处重复的硬编码 f-string）。

    ENHANCE 2026-10-18（审计遗留 D2 批次 2 收编）：模板收编为注册表 key
    ``CHART_MERMAID_GEN_USER``。元标记中和沿用原实现（调用点已先做
    ``sanitize_prompt_value``，本函数对编排 JSON 也加 neutralize 之外的
    默认开关关闭——编排 JSON 是结构化载荷，需保持可解析，故不再中和其花括号）。
    """
    arrange_json = json.dumps(arrange_result, ensure_ascii=False, indent=2)
    try:
        rendered = get_prompt(
            "CHART_MERMAID_GEN_USER",
            CHART_KIND=chart_kind,
            safe_title=sanitize_prompt_value(section_title),
            safe_content=sanitize_prompt_value(section_content),
            safe_overview=sanitize_prompt_value(project_overview or "未提供项目概述"),
            arrange_result=arrange_json,
        )
    except Exception as e:
        logger.warning("[图表生成] 用户提示词渲染失败: %s", e)
        rendered = ""
    return rendered or (
        f"章节标题：{sanitize_prompt_value(section_title)}\n\n章节内容：\n"
        f"{sanitize_prompt_value(section_content)}\n\n项目概述：\n"
        f"{sanitize_prompt_value(project_overview or '未提供项目概述')}\n\n编排结果：\n"
        f"{arrange_json}\n\n请根据以上信息生成Mermaid{chart_kind}代码。"
    )


def _render_struct_data_gen_user(
    schema: str,
    chart_desc: str,
    *,
    section_title: str = "",
    section_content: str = "",
    project_overview: str = "",
    arrange_result: dict | None = None,
    writing_length: str = "standard",
    industry_profile: str = "",
) -> str:
    """渲染结构化图表数据生成的**用户**提示词（原 2 处重复的硬编码 f-string）。

    ENHANCE 2026-10-18（审计遗留 D2 批次 2 收编）：模板收编为注册表 key
    ``CHART_STRUCT_DATA_GEN_USER``。**同时修复安全口径不一致**：原实现对
    ``industry_profile``（行业画像，来自解析链路）裸插值未做元标记中和，
    现统一 ``sanitize=True``（对 JSON 编排载荷无副作用，单遍替换不二次扫描）。
    """
    arrange_json = json.dumps(arrange_result, ensure_ascii=False, indent=2)
    try:
        rendered = get_prompt(
            "CHART_STRUCT_DATA_GEN_USER",
            SCHEMA=schema,
            CHART_DESC=chart_desc,
            safe_title=sanitize_prompt_value(section_title),
            safe_content=sanitize_prompt_value(section_content),
            safe_overview=sanitize_prompt_value(project_overview or "未提供项目概述"),
            arrange_result=arrange_json,
            writing_length=writing_length or "standard",
            industry_profile=industry_profile or "未提供行业背景",
            sanitize=True,
        )
    except Exception as e:
        logger.warning("[图表数据生成] 用户提示词渲染失败: %s", e)
        rendered = ""
    if rendered:
        return rendered
    return (
        f"章节标题：{sanitize_prompt_value(section_title)}\n\n章节内容：\n"
        f"{sanitize_prompt_value(section_content)}\n\n项目概述：\n"
        f"{sanitize_prompt_value(project_overview or '未提供项目概述')}\n\n编排结果：\n"
        f"{arrange_json}\n\n写作长度：{writing_length}\n\n行业背景：\n"
        f"{industry_profile or '未提供行业背景'}\n\n"
        f"请根据以上信息生成{schema}格式的{chart_desc}。"
    )


def repair_mermaid(code: str, return_diff: bool = False) -> str | tuple[str, dict]:
    """修复常见 Mermaid 语法问题

    BUG-FIX 2026-10-04（图表模块 D6）：原实现仅做围栏去除/行尾空白/空行折叠，
    不处理 AI 高频输出问题（BOM、智能引号、全角标点、缺图类型头、括号不配对、
    未闭合链接形状）。现补充机械化修复；修复后仍非法的块由调用方删除（validate）。

    Args:
        code: 原始 Mermaid 代码字符串
        return_diff: True 时返回 (fixed_code, diff_dict)，diff_dict 列出哪些修复
                     步骤实际触发，便于 chart_data_fix AI 拿到更精确的 error context；
                     False（默认）时仅返回 fixed_code（向后兼容）

    Returns:
        return_diff=False → str（修复后代码）
        return_diff=True  → (str, dict)  dict 格式:
            {
                "changed": bool,      # 修复前后是否有差异
                "steps": list[str],   # 触发的修复步骤名列表
                "before": str,        # 修复前代码（截断到 500 字）
                "after": str,         # 修复后代码（截断到 500 字）
            }
    """
    _original = code or ""
    diff_steps: list[str] = []
    step_counts: dict[str, int] = {}  # ENHANCE 2026-10-11: 步骤触发次数计数

    def _track(step_name: str, before: str, after: str) -> str:
        """辅助：比较单步修复前后，记录是否触发 + 次数计数"""
        if before != after:
            diff_steps.append(step_name)
            step_counts[step_name] = step_counts.get(step_name, 0) + 1
        return after

    if not code:
        return ("", {"changed": False, "steps": [], "step_counts": {},
                     "version": "repair_mermaid_v3.2-2026-10-11",
                     "before": "", "after": ""}) if return_diff else ""

    code = code.strip()

    # 去 UTF-8 BOM（Windows 模型输出偶带）
    _before = code
    code = code.lstrip("\ufeff")
    code = _track("strip_bom", _before, code)

    _before = code
    code = re.sub(r"```mermaid\s*\n?", "", code)
    code = re.sub(r"\n?```", "", code)
    # 残留的内联反引号（代码块嵌套错位）：行内 `x` → x
    code = re.sub(r"`([^`\n]*)`", r"\1", code)
    code = _track("strip_fence_and_inline_backticks", _before, code)

    # 智能引号 → ASCII（Mermaid 不识别 " " 作为字符串定界符）
    _before = code
    code = code.replace("\u201c", '"').replace("\u201d", '"')
    code = code.replace("\u2018", "'").replace("\u2019", "'")
    code = _track("smart_quotes_to_ascii", _before, code)

    # 引号配对：奇数个双引号 → 删最后一个（AI 截断行尾多补的引号）
    _dq = code.count('"')
    if _dq % 2 == 1:
        _before = code
        _last = code.rfind('"')
        _line_start = code.rfind("\n", 0, _last) + 1
        # 仅当孤立引号所在行非注释行时删除
        if not code[_line_start:_line_start + 2].startswith("%%"):
            code = code[:_last] + code[_last + 1:]
            code = _track("pair_unmatched_quotes", _before, code)

    # 全角括号/花括号归一化
    _before = code
    _shape_lines = []
    for _ln in code.split("\n"):
        if '"' not in _ln and ("（" in _ln or "）" in _ln or "【" in _ln or "】" in _ln or "｛" in _ln or "｝" in _ln):
            _ln = _ln.replace("（", "[").replace("）", "]") \
                .replace("【", "[").replace("】", "]") \
                .replace("｛", "{").replace("｝", "}")
        _shape_lines.append(_ln)
    code = "\n".join(_shape_lines)
    code = _track("fullwidth_brackets_to_ascii", _before, code)

    # 行末分号剔除 + 空白清理
    _before = code
    code = re.sub(r"[;；][ \t]*$", "", code, flags=re.MULTILINE)
    code = re.sub(r"[ \t]+$", "", code, flags=re.MULTILINE)
    code = re.sub(r"\n{3,}", "\n\n", code)
    code = _track("strip_trailing_semicolons_and_ws", _before, code)

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
    code = _track("fix_unmatched_brackets", _before, code)

    # BUG-FIX 2026-10-17（P1·菱形节点括号不配对）：上方只补齐方括号 [ ]，
    # 菱形节点 { } 无配对修复——AI 截断产出 `A[开始] --> B{判断`（少 } ）时，
    # 修复与校验双双放行，渲染层才炸，整图静默丢失。与方括号同策略逐行补齐：
    # 仅当某行 { 多于 } 时在行尾补 }（跳过注释行），全局 { 少于 } 不动
    # （多余右括号是另一种畸形，交给 validate 拦截 + AI 修复，机械删除风险更高）。
    _before = code
    if code.count("{") > code.count("}"):
        _cu_lines = []
        for _ln in code.split("\n"):
            _s = _ln.rstrip()
            if _s.count("{") > _s.count("}") and not _s.strip().startswith("%%"):
                _s += "}" * (_s.count("{") - _s.count("}"))
            _cu_lines.append(_s)
        code = "\n".join(_cu_lines)
    code = _track("fix_unmatched_curly_braces", _before, code)

    # 缺图类型头：含箭头但首行非类型关键字 → 补 graph TD
    _first = code.split("\n", 1)[0].strip().lower()
    _known_heads = (
        "graph", "flowchart", "gantt", "sequencediagram", "classdiagram",
        "statediagram", "pie", "erdiagram", "timeline", "xychart", "journey",
    )
    if "-->" in code and not _first.startswith(_known_heads):
        _before = code
        code = "graph TD\n" + code
        code = _track("prepend_graph_td_head", _before, code)

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

    if return_diff:
        changed = code != (_original or "")
        diff = {
            "version": "repair_mermaid_v3.2-2026-10-11",
            "changed": changed,
            "steps": diff_steps,
            "step_counts": step_counts,  # ENHANCE 2026-10-11: 每个步骤触发次数
            "before": (_original or "")[:500],
            "after": code[:500],
        }
        return code, diff
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

    user_prompt = _render_arrange_user(
        "流程图",
        section_title=section_title,
        section_content=section_content,
        project_overview=project_overview,
        scoring_standard=scoring_standard,
        industry_profile=industry_profile,
    )

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
    repair_context: dict | None = None,
) -> str | dict:
    """图表修复兜底 — 任意图表生成失败后调用

    Args:
        repair_context: validate_chart_for_ai_pipeline(verbose=True) 返回的完整修复上下文。
            包含 repair_mermaid 机械化修复的 diff（version/steps/before/after）和
            结构化校验错误列表——让 AI 知道机械化修复已经尝试了什么、哪些步骤触发了。
    """
    fix_sys = get_prompt("CHART_DATA_FIX")

    # ENHANCE 2026-10-11: repair_context 注入 AI prompt（含 step_counts + parsed_structure）
    _repair_prompt = ""
    if repair_context:
        _rm = repair_context.get("repair_mermaid") or {}
        _steps = _rm.get("steps") or []
        _counts = _rm.get("step_counts") or {}
        _version = _rm.get("version") or "unknown"
        _changed = _rm.get("changed", False)
        _struct_errs = repair_context.get("structural_validation_errors") or []
        # parsed_structure：Mermaid parser 输出的 nodes/edges/tasks，让 AI 看到解析出了哪些节点连线
        _parsed = repair_context.get("parsed_structure")
        _parsed_summary = ""
        if _parsed:
            if "nodes" in _parsed and "edges" in _parsed:
                _nn = len(_parsed.get("nodes", []))
                _ne = len(_parsed.get("edges", []))
                _node_preview = _parsed.get("nodes", [])[:8]
                _edge_preview = _parsed.get("edges", [])[:5]
                _parsed_summary = (
                    f"\n  Mermaid 解析预览: nodes={_nn}, edges={_ne}\n"
                    f"    节点: {[(n.get('id'), n.get('label','')[:10]) for n in _node_preview]}\n"
                    f"    连线: {[(f.get('from'), f.get('to'), f.get('label','')) for f in _edge_preview]}"
                )
            elif "tasks" in _parsed:
                _parsed_summary = f"\n  Mermaid 解析预览: tasks={len(_parsed.get('tasks', []))}"
        _counts_str = ", ".join(f"{k}×{v}" for k, v in _counts.items()) if _counts else "无"
        # ENHANCE 2026-10-18（审计遗留 D2 批次 2 收编）：修复上下文块收编为注册表
        # key CHART_REPAIR_CONTEXT_BLOCK —— 该块此前是纯内联 f-string，运维无法在
        # 管理页调整诊断粒度/字段顺序。渲染失败时回落原硬编码形态。
        try:
            _repair_prompt = get_prompt(
                "CHART_REPAIR_CONTEXT_BLOCK",
                repair_version=_version,
                repair_triggered="是" if _changed else "否",
                repair_steps=", ".join(_steps) if _steps else "无",
                step_counts=_counts_str,
                struct_errors="; ".join(_struct_errs) if _struct_errs else "无",
                before_snippet=str(_rm.get("before", ""))[:300],
                after_snippet=str(_rm.get("after", ""))[:300],
                parsed_summary=_parsed_summary,
            )
        except Exception as e:
            logger.warning("[图表修复] 修复上下文块渲染失败: %s", e)
            _repair_prompt = (
                f"\n[机械化修复上下文 v{_version}\n"
                f"  repair_mermaid 触发: {'是' if _changed else '否'}\n"
                f"  已执行修复步骤: {', '.join(_steps) if _steps else '无'}\n"
                f"  步骤触发次数: {_counts_str}\n"
                f"  结构化校验错误: {'; '.join(_struct_errs) if _struct_errs else '无'}\n"
                f"  修复前代码片段: {str(_rm.get('before', ''))[:300]}\n"
                f"  修复后代码片段: {str(_rm.get('after', ''))[:300]}{_parsed_summary}\n\n"
            )

    # ENHANCE 2026-10-11: 高频 type mismatch 动态注入
    # 如果当前 chart_type 在 _REPAIR_TYPE_MISMATCHES 里有高频错位记录（≥3 次），
    # 在 prompt 里明确警告「请输出正确类型格式，不要输出 X 格式」
    _mismatch_warning = ""
    try:
        _high_freq = _get_high_freq_mismatches()
        _relevant = [m for m in _high_freq if m["outer_type"] == chart_type]
        if _relevant:
            _types_to_avoid = ", ".join(m["original_type"] for m in _relevant)
            # ENHANCE 2026-10-18：告警模板收编为 CHART_TYPE_MISMATCH_WARNING
            try:
                _mismatch_warning = get_prompt(
                    "CHART_TYPE_MISMATCH_WARNING",
                    chart_type=chart_type,
                    threshold=_REPAIR_MISMATCH_THRESHOLD,
                    types_to_avoid=_types_to_avoid,
                )
            except Exception:
                _mismatch_warning = (
                    f"\n⚠️ 历史上 {chart_type} 类型曾多次（≥{_REPAIR_MISMATCH_THRESHOLD}次）"
                    f"被误输出为 {_types_to_avoid} 格式，请务必输出正确的 {chart_type} 格式，不要混淆！\n"
                )
    except Exception:
        pass  # 统计模块不可用时静默跳过

    # ENHANCE 2026-10-18：user 侧整体收编为 CHART_DATA_FIX_USER
    try:
        user_prompt = get_prompt(
            "CHART_DATA_FIX_USER",
            chart_type=chart_type,
            mismatch_warning=_mismatch_warning,
            section_title=section_title,
            section_content=section_content,
            error_message=error_message or "未提供错误信息",
            repair_context=_repair_prompt,
            original_data=original_data or "未提供原始数据",
            sanitize=True,
        )
    except Exception as e:
        logger.warning("[图表修复] 用户提示词渲染失败: %s", e)
        user_prompt = ""
    if not user_prompt:
        user_prompt = (
            f"图表类型：{chart_type}\n{_mismatch_warning}\n章节标题：{section_title}\n\n"
            f"章节内容：\n{section_content}\n\n错误信息：\n{error_message or '未提供错误信息'}\n"
            f"{_repair_prompt}原始数据：\n{original_data or '未提供原始数据'}\n\n"
            f"请根据以上信息生成降级可用的图表数据。"
        )

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

    user_prompt = _render_arrange_user(
        "布局图",
        section_title=section_title,
        section_content=section_content,
        project_overview=project_overview,
        scoring_standard=scoring_standard,
        industry_profile=industry_profile,
    )

    messages = [
        AIMessage(role="system", content=arr_sys),
        AIMessage(role="user", content=user_prompt),
    ]

    result = await collect_json_response(provider, messages, max_retries=3)
    return result


def _strip_generation_fence(raw: str) -> str:
    """剥离 AI 输出中的 Markdown 代码围栏（```` ```mermaid ```` / ```` ```json ````
    / 泛化 ```` ``` ````），返回内部载荷文本。

    ENHANCE 2026-11-04（P1·JSON 契约冲突配套）：原各 generate_*_mermaid 内联的
    剥离正则 `` ```(?:mermaid)? `` 只认 mermaid 语言标签——模型遵从「纯 JSON」
    提示词却用 ```json 围栏包裹时，会残留 ``json`` 语言标签行，下游
    validate_mermaid / json.loads 双双失败。本函数同时识别 ```json / ```mermaid
    及无标签围栏；未匹配到完整围栏时退回逐行剔除围栏标记的旧行为。
    """
    text = str(raw or "")
    if "```" in text:
        m = re.search(r"```(?:mermaid|json)?\s*\n(.*?)\n?```", text, re.DOTALL)
        if m:
            return m.group(1).strip()
        # 围栏不完整（缺闭合）：逐行剔除围栏标记行
        lines = [
            ln for ln in text.split("\n")
            if not ln.strip().startswith("```")
        ]
        return "\n".join(lines).strip()
    return text.strip()


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

    # 元标记中和已下沉到 _render_mermaid_gen_user 内部统一处理。
    user_prompt = _render_mermaid_gen_user(
        "布局图",
        section_title=section_title,
        section_content=section_content,
        project_overview=project_overview,
        arrange_result=arrange_result,
    )

    messages = [
        AIMessage(role="system", content=gen_sys),
        AIMessage(role="user", content=user_prompt),
    ]

    # ENHANCE 2026-11-04（P1·JSON 契约冲突）：LAYOUT_GENERATE_SYSTEM 要求纯 JSON
    # 输出，渲染端 PIL v2 也原生消费 JSON 载荷。此前对 AI 原文一律跑 repair_mermaid
    # （引号/括号/补头机械修复会改写 JSON 语义），且 ```json 围栏剥离正则只认
    # ```mermaid → 残留 "json" 语言标签行，validate_mermaid 必败。现改为：
    # JSON 输出剥离围栏后原样返回；Mermaid 语法输出（AI 未遵从提示词时）才走
    # 修复链。JSON 合法性由 validate_chart_for_ai_pipeline 的结构化分支把关。
    raw = await with_timeout(provider.chat(messages, temperature=0.7), timeout=30)
    raw = _strip_generation_fence(raw)
    if raw.startswith("{"):
        return raw.strip()
    return repair_mermaid(raw).strip()


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

    user_prompt = _render_arrange_user(
        "里程碑时间线图",
        section_title=section_title,
        section_content=section_content,
        project_overview=project_overview,
        scoring_standard=scoring_standard,
        industry_profile=industry_profile,
    )

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

    # 元标记中和已下沉到 _render_mermaid_gen_user 内部统一处理。
    user_prompt = _render_mermaid_gen_user(
        "时间线",
        section_title=section_title,
        section_content=section_content,
        project_overview=project_overview,
        arrange_result=arrange_result,
    )

    messages = [
        AIMessage(role="system", content=gen_sys),
        AIMessage(role="user", content=user_prompt),
    ]

    # ENHANCE 2026-11-04（P1·JSON 契约冲突）：与 generate_layout_mermaid 同口径——
    # TIMELINE_GENERATE_SYSTEM 要求纯 JSON（TimelinePlan），JSON 输出剥离围栏后
    # 原样返回；Mermaid 语法输出（AI 未遵从提示词时）才走 repair_mermaid 修复链。
    raw = await with_timeout(provider.chat(messages, temperature=0.7), timeout=30)
    raw = _strip_generation_fence(raw)
    if raw.startswith("{"):
        return raw.strip()
    return repair_mermaid(raw).strip()


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

    user_prompt = _render_arrange_user(
        "对比图",
        section_title=section_title,
        section_content=section_content,
        project_overview=project_overview,
        scoring_standard=scoring_standard,
        industry_profile=industry_profile,
    )

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

    # 元标记中和已下沉到 _render_mermaid_gen_user 内部统一处理。
    user_prompt = _render_mermaid_gen_user(
        "对比图",
        section_title=section_title,
        section_content=section_content,
        project_overview=project_overview,
        arrange_result=arrange_result,
    )

    messages = [
        AIMessage(role="system", content=gen_sys),
        AIMessage(role="user", content=user_prompt),
    ]

    # ENHANCE 2026-11-04（健壮性）：COMPARISON_GENERATE_SYSTEM 要求纯净 Mermaid
    # Pie/xychart 代码，但模型偶发输出 JSON 数据（渲染端 PIL v2 原生支持）。
    # 此前 ```json 围栏剥离正则只认 ```mermaid → 残留 "json" 语言标签行。
    # 现统一围栏剥离；JSON 输出原样返回（validate_chart_for_ai_pipeline 的
    # JSON 分支与渲染端均已支持），Mermaid 输出仍走 repair_mermaid 修复链。
    raw = await with_timeout(provider.chat(messages, temperature=0.7), timeout=30)
    raw = _strip_generation_fence(raw)
    if raw.startswith("{"):
        return raw.strip()
    return repair_mermaid(raw).strip()


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
        # ENHANCE 2026-10-18（审计遗留 D2 批次 2 收编）：修复请求的 user 侧收编为
        # MERMAID_REPAIR_USER（原为两处重复的 f-string 拼接）。
        # 不加 sanitize：raw_content 是待修复的 Mermaid 源码，中和会破坏
        # 节点/连线语法（与 BUG-FIX-PROMPT-S1 同理）。
        repair_user = get_prompt(
            "MERMAID_REPAIR_USER",
            raw_content=raw_content,
            error_message=msg,
        ) or f"原始 Mermaid 代码：\n{raw_content}\n\n校验问题：{msg}"
        repair_messages = [
            AIMessage(role="system", content=mermaid_repair),
            AIMessage(role="user", content=repair_user),
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
    # UNIFY-R2a（2026-11-04）：模板概述槽位 {SUMMARY}→{PROJECT_OVERVIEW}（与
    # MERMAID_FLOWCHART_USER / chart 家族统一），此处 kwarg 同步更名，
    # 消除 `summary=project_overview` 的形参-语义错位适配。
    gt_user = get_prompt(
        "MERMAID_GANTT_USER",
        section_title=section_title,
        section_content=section_content,
        scoring_standard=scoring_standard,
        industry_profile=industry_profile,
        project_overview=summary or "未提供项目概述，请根据章节内容和行业经验合理推断工期。",
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

# ILLUSTRATION-REMOVAL 2026-11-04（遗留项3·生图功能下线）：已移除 4 个仅被
# POST /sections/{id}/generate-illustration 端点消费的生图函数——
# generate_illustration_prompt / generate_illustration_arrange /
# generate_illustration_image（含 _image_cache_* 渲染缓存）/ build_illustration_url。
# 产品口径：图表全部自动生成，不设生图功能；配图阶段对 image 类型恒跳过
# （illustration_phase.ai_image_disabled），全仓无其他调用方。
# 保留：image_styles.py 与 image_providers/（image_providers.service 自用）。
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

    user_prompt = _render_arrange_user(
        "劳动力图表",
        section_title=section_title,
        section_content=section_content,
        project_overview=project_overview,
        scoring_standard=scoring_standard,
        industry_profile=industry_profile,
    )

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
    user_prompt = _render_struct_data_gen_user(
        "LaborPlan",
        "劳动力图表JSON数据",
        section_title=section_title,
        section_content=section_content,
        project_overview=project_overview,
        arrange_result=arrange_result,
        writing_length=writing_length,
        industry_profile=industry_profile,
    )

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

    user_prompt = _render_arrange_user(
        "架构图",
        section_title=section_title,
        section_content=section_content,
        project_overview=project_overview,
        scoring_standard=scoring_standard,
        industry_profile=industry_profile,
    )

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
    user_prompt = _render_struct_data_gen_user(
        "ArchNode",
        "架构图JSON树",
        section_title=section_title,
        section_content=section_content,
        project_overview=project_overview,
        arrange_result=arrange_result,
        writing_length=writing_length,
        industry_profile=industry_profile,
    )

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
    # === 正文配图位置提示（CHART-MARKER 2026-09-16 / R1 2026-10-12）===
    # 正文写作阶段已用 `<<CHART:类型>>` 标定了配图意图与位置，这类意图优先于
    # 编排阶段的二次判断；原来用硬编码 `batch_arrange_sys += ...` 拼接，
    # R1 改为模板变量注入，管理员可通过管理页编辑时统一处理 hints 段落。
    _hints = {str(k).lower(): v for k, v in (chart_hints or {}).items() if k}
    if _hints:
        _hint_lines = "\n".join(
            f"- {ct}：{title or '（正文已标注该配图位置，标题由你根据章节内容拟定）'}"
            for ct, title in sorted(_hints.items())
        )
        _hint_block = (
            "\n\n### 正文已标注的配图意图（最高优先级，必须 needed=true）\n"
            "下列图表类型在正文写作阶段已被作者显式标注为配图对象，"
            "**无论你的通用判定如何，这些类型的 `needed` 必须为 true**，"
            "并为其补充准确的 `type`（子类型）与 `priority`：\n"
            f"{_hint_lines}"
        )
        _hint_user_block = f"\n**正文已标注配图**：{', '.join(sorted(_hints))}\n"
    else:
        _hint_block = ""
        _hint_user_block = ""
    # ENHANCE 2026-10-12: 批量编排系统提示词已注册进 prompts 注册表（_integrate.py），
    # DB 优先 + 硬编码回退的 get_prompt 已覆盖全部场景，管理员可通过管理页覆盖/重置。
    # R1: chart_hints 通过模板变量注入，不再硬编码拼接。
    # BUG-FIX 2026-11-04（P1·UnboundLocalError）：原实现先在 or 右侧引用
    # _BATCH_ARRANGE_SYS_FALLBACK（:1746），赋值却在同函数其后（:1756）——
    # 当 get_prompt 返回空（注册表/DB 故障）时触发 UnboundLocalError，
    # 被 sse_charts 宽 except 吞成「批量图表编排失败」→ 整章静默丢图，
    # fallback 永远不可达。现将 fallback 常量定义移到引用之前。
    _BATCH_ARRANGE_SYS_FALLBACK = """# 批量图表编排（系统提示词）— V8.0 商业级优化版
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

    batch_arrange_sys = (
        get_prompt("CHART_BATCH_ARRANGE_SYSTEM", chart_hints_block=_hint_block)
        or (_BATCH_ARRANGE_SYS_FALLBACK + _hint_block)
    )
    logger.info(
        "[批量编排] 开始 章节=%s 内容长度=%d 概述长度=%d 正文配图提示=%s",
        section_title,
        len(section_content),
        len(project_overview),
        sorted((chart_hints or {}).keys()),
    )

    # ENHANCE 2026-10-12: 从注册表读取用户提示词模板，支持管理页覆盖。
    # 保留 f-string 作为 DB 不可用时的 fallback。
    _user_fallback = f"""## 章节信息
**章节标题**：{section_title}

**章节内容**：
{section_content[:2000] if section_content else "（暂无正文内容）"}
{_hint_user_block}
## 项目信息
**项目概述**：{project_overview or "未提供项目概述"}
**评分标准**：{scoring_standard or "未提供评分标准"}
**行业背景**：{industry_profile or "未提供行业背景"}

请根据以上信息，同时判断所有7种图表类型的适配性，返回JSON对象。"""
    user_prompt = get_prompt(
        "CHART_BATCH_ARRANGE_USER",
        section_title=section_title or "",
        section_content=(section_content or "（暂无正文内容）")[:2000],
        hint_user_block=_hint_user_block,
        project_overview=project_overview or "未提供项目概述",
        scoring_standard=scoring_standard or "未提供评分标准",
        industry_profile=industry_profile or "未提供行业背景",
    ) or _user_fallback

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

# ILLUSTRATION-REMOVAL 2026-11-04（遗留项3·生图功能下线）：已移除 V7.0
# ImageGenerationService 图像生成服务类与 AIMageGenerator 别名、生图缓存统计
# （get_image_cache_stats/clear_image_cache）——唯一消费方为已删除的
# POST /sections/{id}/generate-illustration 端点与 generate_illustration_image。
# 生图产品口径：图表全部自动生成，不设生图功能；独立图像任务管理
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

        # ENHANCE 2026-10-18（审计遗留 D2 批次 2 收编）：此前 system 与 user
        # **两侧都是内联字面量**，是本批唯一「system 也未注册表化」的点 ——
        # 管理页完全看不到这段判定规则，运维无法调整「什么样的章节该配图」。
        # 现两侧均收编为 INLINE_CHART_DETECT_SYSTEM / INLINE_CHART_DETECT_USER，
        # 并对注入内容开启元标记中和（facts 来自解析链路，属外部内容）。
        detect_sys = get_prompt("INLINE_CHART_DETECT_SYSTEM")
        user_prompt = get_prompt(
            "INLINE_CHART_DETECT_USER",
            section_title=chapter_info.get("title", ""),
            section_desc=chapter_info.get("description", ""),
            facts_json=json.dumps(facts, ensure_ascii=False, indent=2)[:2000],
            sanitize=True,
        )
        if not detect_sys or not user_prompt:
            logger.warning("InlineChartDetector: 提示词未注册，跳过 AI 判定并回退启发式")
            return quick

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



# （routers/images.py → image_providers.service.ImageGenerationService）不受影响。
# AI调用超时装饰器
async def with_timeout(coro, timeout: int = 30, error_msg: str = "AI调用超时"):
    """带超时的AI调用装饰器"""
    import asyncio

    try:
        return await asyncio.wait_for(coro, timeout=timeout)
    except TimeoutError:
        raise TimeoutError(f"{error_msg}（{timeout}秒）")
# ============================================================
# E-7：图表数据校验统一入口 + AI 生图成本拦截
# ============================================================


def validate_chart_payload(chart_type: str, data: Any) -> tuple[bool, list]:
    """图表数据校验统一入口（E-7，chart_validators v3.1 接入）。

    此前 chart_validators.py 的 v3.1 全套校验器「零引用」——完整实现了却从未
    接入生成链路。本函数是接入的统一闸门：按图表类型分发到对应校验器，
    任何异常都不会炸穿调用方（校验失败即返回 False + 原因，由调用方按
    无降级原则丢弃劣质数据）。

    Args:
        chart_type: flowchart / gantt / architecture / timeline / labor / comparison / layout
        data: 图表数据（AI 生成的 dict）

    Returns:
        (是否通过, 错误列表前 5 条)
    """
    from .. import chart_validators as _cv

    validators = {
        "architecture": _cv.validate_architecture_tree,
        "gantt": _cv.validate_gantt_plan,
        "flowchart": _cv.validate_flowchart_structure,
        "timeline": _cv.validate_timeline_data,
        "labor": _cv.validate_labor_data,
        "comparison": _cv.validate_comparison_pie,
        "layout": _cv.validate_layout_data,
    }
    fn = validators.get(str(chart_type or "").lower())
    if fn is None:
        # 未知类型不预判：交给上层按「有标记无数据」处理，不夸大校验
        return True, []

    # 字段提取：各校验器期望输入格式不同，统一入口需要做适配。
    #   - architecture: 期望根节点 dict（有 label/children），chart_data 含 "tree" 包
    #   - labor: 期望 phases/categories/data 在顶层，格式天然对齐
    #   - flowchart/gantt 等 Mermaid 型: 期望 nodes/edges/tasks 等结构化字段，
    #     在 validate_chart_for_ai_pipeline 里已通过 Mermaid parser 注入
    try:
        data_for_validator = data
        if chart_type == "architecture" and isinstance(data, dict):
            # chart-json 格式: {"type": "architecture", "tree": {...}, ...}
            tree = data.get("tree") or data.get("structure")
            if isinstance(tree, dict):
                data_for_validator = tree
    except Exception:
        data_for_validator = data

    try:
        ok, errs = fn(data_for_validator)
        return bool(ok), (errs or [])[:5]
    except Exception as e:
        return False, [f"校验器执行异常: {e!s}"]


# ──────────────────────────────────────────────────────────────────────
# repair_mermaid 步骤统计（ENHANCE 2026-10-11）
# ──────────────────────────────────────────────────────────────────────
# 模块级 in-memory 统计。validate_chart_for_ai_pipeline(verbose=True)
# 每次调用 repair_mermaid 后累加 step_counts。celery beat 每小时触发
# flush_repair_stats() 打印汇总 + reset。
# 为什么不用 DB 表？——这个统计是运维洞察用的（哪些 chart_type 高频触发
# 哪些修复步骤），数据量不大、不需要持久化到跨重启粒度、无迁移风险。
_REPAIR_STATS: dict[str, dict[str, int]] = {}  # {chart_type: {step_name: count}}
_REPAIR_STATS_TOTAL: int = 0
# ENHANCE 2026-10-11: original_chart_type ≠ 外层 chart_type 不匹配统计
# 结构: {外层chart_type: {原始chart_type: count}}
_REPAIR_TYPE_MISMATCHES: dict[str, dict[str, int]] = {}
# ENHANCE 2026-10-11: 环形 buffer 存具体 mismatch case（section_title + 原始数据片段）
# 用于 flush 输出 top-3 典型例子，帮助定位 AI 错位模式
from collections import deque as _deque
from ...config import REPAIR_MISMATCH_THRESHOLD as _REPAIR_MISMATCH_THRESHOLD, REPAIR_MISMATCH_CASE_BUFFER as _REPAIR_MISMATCH_CASE_BUFFER
_REPAIR_MISMATCH_CASES: _deque = _deque(maxlen=_REPAIR_MISMATCH_CASE_BUFFER)
import threading as _threading
_repair_stats_lock = _threading.Lock()


def get_repair_stats() -> dict[str, Any]:
    """当前累计统计快照（供运维/API 调用）"""
    with _repair_stats_lock:
        return {
            "total_validations": _REPAIR_STATS_TOTAL,
            "per_chart_type": {
                ct: dict(sorted(st.items(), key=lambda x: -x[1]))
                for ct, st in sorted(_REPAIR_STATS.items())
            },
            "type_mismatches": {
                ct: dict(sorted(mism.items(), key=lambda x: -x[1]))
                for ct, mism in sorted(_REPAIR_TYPE_MISMATCHES.items())
            },
            # ENHANCE 2026-10-11: 高频 mismatch 规则 + 典型 case
            "high_freq_mismatches": _get_high_freq_mismatches(),
            "mismatch_case_count": len(_REPAIR_MISMATCH_CASES),
        }


def _get_high_freq_mismatches() -> list[dict]:
    """筛选高频 mismatch（≥ threshold 次），供 system prompt 动态注入 + API 输出"""
    with _repair_stats_lock:
        result = []
        for _outer, _origins in sorted(_REPAIR_TYPE_MISMATCHES.items()):
            for _orig, _cnt in sorted(_origins.items(), key=lambda x: -x[1]):
                if _cnt >= _REPAIR_MISMATCH_THRESHOLD:
                    result.append({
                        "outer_type": _outer,
                        "original_type": _orig,
                        "count": _cnt,
                        "suggestion": (
                            f"chart_data_fix prompt 里给 {_outer} 类型加约束："
                            f"'请输出该类型的正确格式，不要输出 {_orig} 格式'"
                        ),
                    })
        return result


def flush_repair_stats() -> dict[str, Any]:
    """打印汇总 + reset + DB 持久化（celery beat 每小时触发）"""
    global _REPAIR_STATS_TOTAL, _REPAIR_STATS, _REPAIR_TYPE_MISMATCHES
    with _repair_stats_lock:
        snapshot = {
            "total": _REPAIR_STATS_TOTAL,
            "stats": {ct: dict(st) for ct, st in _REPAIR_STATS.items()},
            "type_mismatches": {ct: dict(mism) for ct, mism in _REPAIR_TYPE_MISMATCHES.items()},
            "mismatch_cases": list(_REPAIR_MISMATCH_CASES),
            # ENHANCE 2026-10-11: 把高频规则也一起存入 DB
            "high_freq_mismatches": _get_high_freq_mismatches(),
        }
        _REPAIR_STATS = {}
        _REPAIR_STATS_TOTAL = 0
        _REPAIR_TYPE_MISMATCHES = {}
        _REPAIR_MISMATCH_CASES.clear()

    # ── 持久化到 DB（JSON blob 方式，INSERT 一行快照）──
    if snapshot["total"] > 0:
        try:
            import json as _json
            from ...db import get_db as _get_db
            with _get_db() as _conn:
                _conn.execute(
                    "INSERT INTO repair_stats_snapshots "
                    "(total_validations, stats_json, mismatches_json, cases_json, high_freq_json) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        snapshot["total"],
                        _json.dumps(snapshot["stats"], ensure_ascii=False),
                        _json.dumps(snapshot["type_mismatches"], ensure_ascii=False),
                        _json.dumps(snapshot["mismatch_cases"], ensure_ascii=False),
                        _json.dumps(snapshot["high_freq_mismatches"], ensure_ascii=False),
                    ),
                )
        except Exception as _dbe:
            logger.warning("repair_stats flush DB 写入失败（不影响内存 reset）: %s", _dbe)

    if snapshot["total"] > 0:
        _lines = [
            f"  {ct:12s}: {', '.join(f'{s}×{c}' for s, c in sorted(st.items(), key=lambda x: -x[1]))}"
            for ct, st in sorted(snapshot["stats"].items())
        ]
        # type mismatch 汇总
        _mism_lines = []
        _mism_total = 0
        for _outer, _origins in snapshot["type_mismatches"].items():
            for _orig, _cnt in _origins.items():
                _mism_lines.append(f"    {_outer} → {_orig}: {_cnt}")
                _mism_total += _cnt

        _extra = ""
        if _mism_total > 0:
            _extra = f"\n[类型错位] 累计 {_mism_total} 次 mismatch:" + "\n" + "\n".join(_mism_lines)
            # ENHANCE 2026-10-11: top-3 典型 case
            _cases = snapshot["mismatch_cases"][:3]
            if _cases:
                _case_lines = []
                for _c in _cases:
                    _case_lines.append(
                        f"    [{_c.get('outer')}→{_c.get('original')}] section={_c.get('section_title','')[:40]}"
                    )
                _extra += "\n[典型 case TOP-3]:" + "\n" + "\n".join(_case_lines)

        logger.warning(
            "[repair_mermaid 统计汇总] 累计校验 %d 次，按类型:\n%s%s",
            snapshot["total"], "\n".join(_lines), _extra,
        )
    return snapshot


# ──────────────────────────────────────────────────────────────────────
# DB 历史查询 / 清理 / 启动回填（ENHANCE 2026-10-11）
# ──────────────────────────────────────────────────────────────────────
def get_repair_stats_history(limit: int = 72) -> list[dict]:
    """从 DB 读取最近 N 条快照（历史趋势）

    Args:
        limit: 返回最近多少行快照。默认 72 = 3 天 × 24 小时

    Returns:
        [{id, total_validations, created_at, stats_json, mismatches_json, high_freq_json}, ...]
        已按 id DESC 排序，时间近的在前。失败返回 []（静默）。
    """
    try:
        import json as _json
        from ...db import get_db as _get_db
        with _get_db() as _conn:
            _rows = _conn.execute(
                "SELECT id, total_validations, created_at, stats_json, mismatches_json, high_freq_json "
                "FROM repair_stats_snapshots ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        _result = []
        for _r in _rows:
            _result.append({
                "id": _r["id"],
                "total_validations": _r["total_validations"],
                "created_at": _r["created_at"],
                "stats": _json.loads(_r["stats_json"] or "{}"),
                "mismatches": _json.loads(_r["mismatches_json"] or "{}"),
                "high_freq": _json.loads(_r["high_freq_json"] or "[]"),
            })
        return _result
    except Exception as e:
        logger.debug("get_repair_stats_history 失败（表不存在？）: %s", e)
        return []


def cleanup_repair_stats_snapshots(keep_rows: int = 720) -> int:
    """清理过期快照（保留最近 keep_rows 行）

    Args:
        keep_rows: 保留行数，默认 720 = 30 天 × 24 小时

    Returns:
        删除的行数。失败返回 0。
    """
    try:
        from ...db import get_db as _get_db
        with _get_db() as _conn:
            _cursor = _conn.execute(
                "DELETE FROM repair_stats_snapshots WHERE id NOT IN "
                "(SELECT id FROM repair_stats_snapshots ORDER BY id DESC LIMIT ?)",
                (keep_rows,),
            )
            _deleted = _cursor.rowcount
        if _deleted > 0:
            logger.info("repair_stats_snapshots 清理: 删除 %d 行（保留最近 %d 行）", _deleted, keep_rows)
        return _deleted
    except Exception as e:
        logger.warning("cleanup_repair_stats_snapshots 失败: %s", e)
        return 0


def restore_repair_stats_from_db(limit: int = 720) -> dict:
    """启动时从 DB 回填高频规则到内存（跨重启保留历史洞察）

    遍历最近 N 条 snapshots，把每条的 mismatches_json 聚合到
    内存的 _REPAIR_TYPE_MISMATCHES 里——这样 _get_high_freq_mismatches()
    就能在进程刚启动时（还没跑过任何校验）就有历史高频数据，
    prompt 动态注入立即生效。

    只回填 mismatches（频次统计），不回填 steps（steps 是即时性的）。
    """
    try:
        import json as _json
        from ...db import get_db as _get_db
        with _get_db() as _conn:
            _rows = _conn.execute(
                "SELECT mismatches_json FROM repair_stats_snapshots ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        _restored = 0
        with _repair_stats_lock:
            for _r in _rows:
                _mism = _json.loads(_r["mismatches_json"] or "{}")
                for _outer, _origins in _mism.items():
                    _target = _REPAIR_TYPE_MISMATCHES.setdefault(_outer, {})
                    for _orig, _cnt in _origins.items():
                        _target[_orig] = _target.get(_orig, 0) + _cnt
                        _restored += 1
        if _restored > 0:
            logger.info(
                "[repair_stats] 启动回填完成: %d 条 mismatch 记录从 DB 注入（%d 条快照）",
                _restored, len(_rows),
            )
        return {"restored": _restored, "snapshots_used": len(_rows)}
    except Exception as e:
        logger.debug("restore_repair_stats_from_db 跳过（表不存在？）: %s", e)
        return {"restored": 0, "snapshots_used": 0}


def validate_chart_for_ai_pipeline(
    chart_type: str,
    chart_data: dict,
    verbose: bool = False,
    section_title: str | None = None,
) -> tuple[bool, list[str], dict, bool, dict | None]:
    """SSE 图表管线完整校验（纯逻辑，可独立单元测试）。

    封装 sse_charts.py _validate_and_fix helper 内部的「首轮校验」逻辑——
    对 Mermaid 代码型做 repair + validate_mermaid + 结构化解析 →
    validate_chart_payload；对结构化 JSON 型直接 validate_chart_payload。

    Args:
        chart_type: flowchart / gantt / architecture / timeline / labor / comparison / layout
        chart_data: AI 生成的原始 dict（Mermaid 型含 code 字段，结构化型含 tree/phases 等）
        verbose: True 时返回 repair_context（完整修复上下文）；False（默认）时为 None

    Returns:
        (是否通过, 错误列表, chart_data, mutated标记, repair_context_or_None)
          - repair_context 结构 (仅 verbose=True + Mermaid 代码型):
              {
                "repair_mermaid": {version, changed, steps, before, after},
                "parsed_structure": dict | None,
                "structural_validation_errors": list[str],
              }
    """
    chart_type = str(chart_type or "").lower()
    errors: list[str] = []
    mutated = False
    repair_context: dict | None = None

    MERMAID_CODE_TYPES = {"flowchart", "gantt", "comparison", "layout", "timeline"}
    STRUCTURED_TYPES = {"architecture", "labor"}

    if chart_type in STRUCTURED_TYPES:
        # 结构化 JSON 型 → chart_validators 全套校验器
        try:
            ok, errs = validate_chart_payload(chart_type, chart_data)
            if not ok:
                errors = list(errs or ["校验未通过"])
        except Exception as ve:
            errors.append(f"结构化校验异常: {ve!s}")

    elif chart_type in MERMAID_CODE_TYPES:
        # Mermaid 代码型 → repair_mermaid + validate_mermaid + 结构化完整性校验
        raw = str(chart_data.get("code") or "").strip()

        # ---------- ENHANCE 2026-11-04（P1·layout/timeline JSON 契约冲突）----------
        # LAYOUT/TIMELINE_GENERATE_SYSTEM 与 CHART_DATA_FIX 速查表承诺 JSON 数据
        # 载荷（LayoutPlan/TimelinePlan/GanttPlan），且渲染端 PIL v2 原生支持 JSON
        # 载荷（mermaid_renderer V6.1 归一化）；但本管线此前一律按 Mermaid 代码
        # 校验 → JSON 载荷被 validate_mermaid 判「不支持的 Mermaid 类型」，且
        # repair_mermaid 的机械修复（引号/括号/补头）会改写 JSON 语义；AI 修复轮
        # 返回的结构化 dict（无 code 字段）又因空 code 二轮必败 → 图表整单丢失。
        # 现对 JSON 载荷走 chart_validators 结构化校验，并把结构化字段提升到
        # 载荷顶层、移除 code（与 architecture/labor 同构，build_chart_fence
        # 据此产出 chart-json 围栏，渲染/前端/导出全链路口径一致）。
        _JSON_TRIGGER_KEYS = {
            "gantt": ("tasks",),
            "layout": ("zones", "areas"),
            "timeline": ("milestones",),
            "comparison": ("items", "rows"),
            "flowchart": ("nodes",),
        }
        _json_payload: dict | None = None
        if raw.startswith("{"):
            try:
                _parsed = json.loads(raw)
                if isinstance(_parsed, dict):
                    _json_payload = _parsed
            except (json.JSONDecodeError, TypeError, ValueError):
                _json_payload = None
        elif not raw:
            # AI 修复轮直接返回结构化 dict（无 code）——chart_data 本身即载荷
            _trigger = _JSON_TRIGGER_KEYS.get(chart_type, ())
            if _trigger and any(k in chart_data for k in _trigger):
                _json_payload = chart_data

        if _json_payload is not None:
            merged = {**chart_data}
            for _k, _v in _json_payload.items():
                if _k not in ("type", "code", "title", "source", "needed"):
                    merged[_k] = _v
            merged.pop("code", None)
            if chart_type == "comparison" and "headers" in _json_payload:
                # 表格型对比图（headers+rows 多指标）：渲染端支持但无专用结构化
                # 校验器，只做最小完整性检查，不套用饼图百分比校验（会误杀）
                _rows_list = merged.get("rows")
                if isinstance(_rows_list, list) and _rows_list:
                    sok, serrs = True, []
                else:
                    sok, serrs = False, ["表格型对比图 rows 不能为空"]
            else:
                try:
                    sok, serrs = validate_chart_payload(chart_type, merged)
                except Exception as ve:
                    sok, serrs = False, [f"结构化校验异常: {ve!s}"]
            if sok:
                chart_data = merged
                mutated = True
            else:
                errors.extend(list(serrs or ["结构化校验未通过"])[:5])
            if verbose:
                repair_context = {
                    "repair_mermaid": None,
                    "parsed_structure": _json_payload,
                    "structural_validation_errors": list(serrs or [])[:5],
                    "original_chart_type": (chart_data.get("type") or chart_type),
                }
            return (len(errors) == 0, errors, chart_data, mutated, repair_context)

        repaired, _rm_diff = repair_mermaid(raw, return_diff=True)
        ok, msg = validate_mermaid(repaired)
        if not ok:
            errors.append(f"语法: {msg}")
        elif repaired != raw:
            chart_data = {**chart_data, "code": repaired}
            mutated = True

        # 结构化完整性校验
        structured: dict | None = None
        try:
            if chart_type == "flowchart":
                from .mermaid_flowchart import _parse_flowchart_structure as _pf
                structured = _pf(repaired)
            elif chart_type == "gantt":
                from .mermaid_gantt import _parse_mermaid_gantt as _pg
                tasks = _pg(repaired)
                if tasks:
                    structured = {"tasks": tasks}
            elif chart_type == "comparison":
                from .mermaid_comparison import _parse_mermaid_pie as _pp, _parse_mermaid_xychart as _px
                pie = _pp(repaired)
                if pie:
                    structured = pie
                else:
                    xy = _px(repaired)
                    if xy:
                        structured = xy
        except Exception:
            pass

        structural_errors: list[str] = []
        if structured:
            chart_data = {**chart_data, **structured}
            mutated = True
            try:
                sok, serrs = validate_chart_payload(chart_type, chart_data)
                if not sok:
                    structural_errors = list(serrs or [])
                    errors.extend(structural_errors)
            except Exception:
                pass

        # 收集 repair_context（verbose 模式）
        _orig_ct = (chart_data.get("type") or chart_type)
        if verbose:
            repair_context = {
                "repair_mermaid": _rm_diff,
                "parsed_structure": structured,
                "structural_validation_errors": structural_errors,
                "original_chart_type": _orig_ct,
            }

        # ENHANCE 2026-10-11: 模块级统计收集（chart_type × step_name → 累计次数）
        # 用于后续 celery beat 定时批量汇总
        # BUG-FIX 2026-11-04（P0·校验管线静默失效）：`_REPAIR_STATS_TOTAL += 1`
        # 对模块级全局做增强赋值却未声明 global → UnboundLocalError。此前任何
        # 触发机械修复（step_counts 非空）的图表都会让本函数抛异常，被 sse_charts
        # 的宽 except 吞成「图表校验管线不可用」→ 首轮校验整体跳过（fail-open），
        # 坏数据直通落库、修复统计恒空。补 global 声明恢复统计与校验。
        if _rm_diff and _rm_diff.get("step_counts"):
            with _repair_stats_lock:
                global _REPAIR_STATS_TOTAL
                _step_stats = _REPAIR_STATS.setdefault(chart_type, {})
                for _sn, _cnt in _rm_diff["step_counts"].items():
                    _step_stats[_sn] = _step_stats.get(_sn, 0) + _cnt
                _REPAIR_STATS_TOTAL += 1
                # type mismatch: original ≠ 外层 → 统计 + 存具体 case（环形 buffer）
                if _orig_ct != chart_type:
                    _mism_outer = _REPAIR_TYPE_MISMATCHES.setdefault(chart_type, {})
                    _mism_outer[_orig_ct] = _mism_outer.get(_orig_ct, 0) + 1
                    # 存环形 buffer（maxlen=200，先进先出）
                    _REPAIR_MISMATCH_CASES.append({
                        "outer": chart_type,
                        "original": _orig_ct,
                        "section_title": section_title or chart_data.get("title") or "",
                        "code_snippet": str(chart_data.get("code") or "")[:120],
                    })

    return (len(errors) == 0, errors, chart_data, mutated, repair_context)


def resolve_image_budget(requested: int | None, project_id: int | None = None) -> int:
    """AI 生图上限的成本拦截（E-7.4）。

    编排拍板阶段按 `max_ai_images` 全局择优，但「全局配置是否允许生图 / 项目
    生图配额」此前没有在生成路径里生效（配置存在、校验不存在 —— 图表调研
    报告项 7）。这里在编排入口做钳制：

      - 请求数 <= 0 → 0（禁用）；
      - ai_config 显式 `is_image_enabled=False` → 0；
      - ai_config 提供 `max_images_per_project` 且为有限值 → min(requested, 配额)。

    配置键不存在时**不做任何假设**（保持调用方原值），避免误伤旧配置。

    Args:
        requested: 调用方请求的 AI 生图上限（orch_max_ai_images）
        project_id: 预留（未来按项目配额钳制），当前仅透传 config

    Returns:
        钳制后的生图上限（int，>=0）
    """
    if requested is None:
        return 0
    try:
        requested = int(requested)
    except (TypeError, ValueError):
        return 0
    if requested <= 0:
        return 0
    try:
        from .provider_factory import get_ai_config

        cfg = get_ai_config() or {}
        # BUG-FIX 2026-10-09：`bool(None) is False` 恒为 True —— 若写成
        # `if bool(cfg.get("is_image_enabled")) is False`，配置未显式提供该键时
        # （旧配置/默认配置）会把请求一律钳成 0，误禁 AI 生图。只有**显式**给出
        # 该键且值为 False 时才判定为禁用。
        _flag = cfg.get("is_image_enabled")
        if _flag is not None and bool(_flag) is False:
            return 0
        limit = cfg.get("max_images_per_project")
        if isinstance(limit, (int, float)) and int(limit) > 0:
            requested = min(requested, int(limit))
    except Exception:
        pass
    return requested
