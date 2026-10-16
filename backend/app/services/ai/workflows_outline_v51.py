"""workflows 子模块：workflows_outline_v51

基于《标书智能体（二）——生成标书提纲代码+提示词》设计文档重写。

核心设计（严格遵循设计文档）：
  1. L1 生成：评分要求 → 一级目录标题映射（rating_item → new_title）
  2. L2/L3 生成：代码预构建 JSON 骨架 → AI 仅填充 title/description → 结构校验
  3. 跨章节去重：传递其他章节标题供 AI 参考，避免内容重叠
  4. 结构校验：check_json() 严格验证 AI 输出与预构建骨架一致
  5. 并发控制：DynamicSemaphore 控制 L2/L3 并发数
  6. 容错机制：单节点失败默认填充，不中止整体流程
"""

import asyncio
from collections.abc import Callable
import copy
import json
import logging
import os
import random
import re
import time
from typing import Any

from ...utils.json_parser import extract_json_from_text, safe_json_parse


def _parse_scoring_points_json(raw: str | None) -> tuple[list | dict | None, str]:
    """容错解析评分点 JSON，返回 (解析结果, 解析方式)。

    遗留修复（评分点解析失败不再彻底丢信息）：原先两处都用裸 `json.loads`，
    只要评分点字符串尾部多一个逗号、被 AI 加了说明文字或包裹了 ```json 代码
    围栏，就整体解析失败 → `score_item_count=0` / 空评分继续生成，目录与招标
    要求严重脱节。这里按三级降级：
      1. 纯 json.loads（与历史行为一致）
      2. extract_json_from_text：剥离代码围栏 / 定位内嵌 JSON / 本地修复近似 JSON
      3. 确实无法解析 → None，调用方按空评分优雅降级（保持既有不阻断行为）

    Returns:
        (parsed, mode)，mode ∈ {"json", "salvage", "failed"}
    """
    if not raw or not str(raw).strip():
        return None, "empty"
    try:
        return json.loads(raw), "json"
    except (json.JSONDecodeError, TypeError, ValueError):
        pass
    salvaged = None
    try:
        salvaged = extract_json_from_text(str(raw))
    except Exception as _e:  # 解析工具自身异常不得阻断生成
        logger.debug("V5.1 评分点救援解析异常: %s", _e)
    if salvaged is not None:
        logger.warning("V5.1 评分点非标准 JSON，已救援解析成功（降级 salvage）")
        return salvaged, "salvage"
    return None, "failed"
from ...utils.scoring_points import (
    SCORING_PLACEHOLDER_IDS,
    is_scoring_placeholder,
    strip_scoring_placeholders,
)
from . import outline_gate
from .outline_common import (
    FIXED_28_CHAPTERS,
    _cap_cross_chapter_repetition,
    _cleanup_placeholder_titles,
    _cn_ordinal,
    _disambiguate_title,
    _ensure_min_children,
    _is_dirty_l3_title_deep,
    _parse_scope_items,
    _title_core,
    _uniquify_outline_titles,
    check_json,
    generate_nodes_distribution,
    generate_one_outline_json_by_level1,
)
# 2026-10-10：接入此前长期无生产调用方的 L3 具体性校验 / AI 修复（死代码激活）
from .workflows_outline_utils import (  # noqa: E402
    repair_l3_generic_titles,
    validate_l3_specificity,
)
from .prompts import get_prompt, render_prompt, sanitize_prompt_value
from .providers.base import AIMessage

# 从其他子模块导入工具函数
from .workflows_base import (
    DynamicSemaphore,
    collect_json_response,
)
from .workflows_sections import _format_facts_list
from app.config import (
    OUTLINE_L3_MAX_PARENT_REUSE,
    OUTLINE_L3_QUALITY_ENHANCE_ENABLED,
    OUTLINE_L3_REPAIR_MAX,
    OUTLINE_L3_REPAIR_MAX_ROUNDS,
    OUTLINE_MAX_TOKENS_DIRECT,
    OUTLINE_MAX_TOKENS_L1,
    OUTLINE_MAX_TOKENS_L2,
    OUTLINE_MAX_TOKENS_SELFCHECK,
)


logger = logging.getLogger(__name__)

# V5.1 全局并发信号量已移除，改为任务级信号量避免并发竞争
# 每个 generate_outline_v51 任务会创建自己的信号量实例

# ============================================================
# ENHANCE 2026-10-05（P-4）：短标书「直出」目录路径
#
# 易标规格二.2 明确区分两条路径：
#   - 短标书 + 强模型 → 直接输出 JSON 三级目录（一次调用）
#   - 长标书 + 普通模型 → 分步生成（一级 → 遍历二三级）
# 平台此前只有分步路径（V5.1），短标书场景白白多花 3 轮 AI 调用，
# 且多次调用之间口径容易漂移。本段实现直出路径与自动判别。
# ============================================================

from ...config import (  # noqa: E402
    OUTLINE_DIRECT_ENABLED,
    OUTLINE_DIRECT_MAX_LEVEL1,
    OUTLINE_DIRECT_MAX_SCORING_CHARS,
    OUTLINE_DIRECT_MAX_SCORING_ITEMS,
)


def should_use_direct_outline(
    scoring_points: str,
    mode: str = "auto",
) -> bool:
    """判断是否走「短标书 + 强模型」直出路径。

    Args:
        scoring_points: 评分点 JSON 文本
        mode: "auto" 自动判别 / "direct" 强制直出 / "stepwise" 强制分步

    Returns:
        bool: True = 走直出路径。

    自动判别口径（对齐易标规格二.2「超出 AI 输出长度」的分界）：
      - 评分点文本长度 <= OUTLINE_DIRECT_MAX_SCORING_CHARS
      - 评分条目数 <= OUTLINE_DIRECT_MAX_SCORING_ITEMS
    两条都满足才判定为短标书。长标书走分步是规格明确的要求——
    「几十万字标书提纲很长，普通模型输出仅 1500 字左右」，硬压进一次
    调用必然被截断，产出残缺 JSON。
    """
    if mode == "stepwise":
        return False
    if mode == "direct":
        return True
    if not OUTLINE_DIRECT_ENABLED:
        return False

    text = (scoring_points or "").strip()
    if not text:
        return False
    if len(text) > OUTLINE_DIRECT_MAX_SCORING_CHARS:
        return False

    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            n_items = len(parsed)
        elif isinstance(parsed, dict):
            # 部分链路把评分点包在 {"items": [...]} 里
            for key in ("items", "scoring_points", "scoringPoints", "data"):
                if isinstance(parsed.get(key), list):
                    n_items = len(parsed[key])
                    break
            else:
                n_items = 1
        else:
            n_items = 1
    except Exception:
        # 非法 JSON 无法统计条目数，退回按长度判断（长度已判定为短）
        n_items = 0

    return n_items <= OUTLINE_DIRECT_MAX_SCORING_ITEMS


async def generate_outline_direct(
    provider: Any,
    summary: str,
    scoring_points: str,
    industry_profile: str,
    project_scope: str = "",
    reference_outline: str = "",
) -> list[dict]:
    """短标书直出路径：一次 AI 调用输出完整三级目录 JSON。

    对齐易标规格二.2：
      - JSON 结构：outline → id / title / description / children
      - 一级目录与技术评分要求一致
      - 共三级目录，返回标准 JSON

    与分步路径的差异：不做「代码预构建骨架 + AI 只填空」的强约束，
    直接让模型自由生成三级结构，依赖强模型的一次性输出能力。

    失败处理：AI 调用失败或 JSON 解析失败 → 返回空列表，由调用方回退分步路径。

    Returns:
        list[dict]: 一级目录列表（含嵌套 children），失败返回 []。
    """
    from .prompts.outline import OUTLINE_DIRECT_SYSTEM, OUTLINE_DIRECT_USER

    scoring_text = scoring_points or "（未提供评分要求，请依据项目概述生成通用技术标目录）"

    # BUG-FIX-PROMPT-B1 2026-10-04：此前直接 `from .prompts.outline import` 取硬编码
    # 常量并用 str.replace 注入变量，导致提示词管理页对 OUTLINE_DIRECT_SYSTEM /
    # OUTLINE_DIRECT_USER 的 DB 编辑**完全不生效**（绕过 get_prompt 的「DB 优先」缓存），
    # 与全平台其它提示词口径不一致；且 raw replace 只认 `__UPPER__` 形式，用户改用
    # {var} 或不同大小写即导致占位符泄漏。改为走 get_prompt(...)：命中 DB 覆盖即生效，
    # 变量交由统一渲染函数 render_prompt 注入（get_prompt 内部即调用 render_prompt）。
    system_prompt = get_prompt("OUTLINE_DIRECT_SYSTEM") or OUTLINE_DIRECT_SYSTEM
    # SECURITY-FIX 2026-10-06：summary / scoring_points / project_scope 均来自
    # 招标文件解析结果（用户/外部内容），注入前须做元标记中和（sanitize），
    # 与 workflows_analysis.py 高风险链路口径对齐，防御提示词注入。
    user_prompt = get_prompt(
        "OUTLINE_DIRECT_USER",
        SUMMARY=(summary or "").strip() or "（未提供）",
        SCORING_POINTS=scoring_text,
        INDUSTRY_PROFILE=(industry_profile or "").strip() or "通用建设工程",
        PROJECT_SCOPE=(project_scope or "").strip() or "（未提供）",
        sanitize=True,
    )
    if not user_prompt:
        # 兜底（get_prompt 正常不会返回空，仅防御缓存异常）：直接渲染硬编码常量
        user_prompt = render_prompt(
            OUTLINE_DIRECT_USER,
            SUMMARY=(summary or "").strip() or "（未提供）",
            SCORING_POINTS=scoring_text,
            INDUSTRY_PROFILE=(industry_profile or "").strip() or "通用建设工程",
            PROJECT_SCOPE=(project_scope or "").strip() or "（未提供）",
            sanitize=True,
        )

    messages = [
        AIMessage(role="system", content=system_prompt),
        AIMessage(role="user", content=user_prompt),
    ]

    raw = await collect_json_response(
        provider,
        messages,
        max_retries=2,
        max_tokens=OUTLINE_MAX_TOKENS_DIRECT,
    )
    if not raw:
        logger.warning("[目录直出] AI 未返回有效内容，回退分步路径")
        return []

    data = safe_json_parse(raw) if isinstance(raw, str) else raw
    payload = _unwrap_outline_payload(data)
    if not payload:
        logger.warning("[目录直出] JSON 解析失败或为空，回退分步路径")
        return []

    # BUG-FIX 2026-10-09（超长内容崩溃 · 浪费 AI 调用）：规模护栏必须前移到
    # _normalize_direct_outline **之前** —— 后者在给每个一级标题补「第X章」前缀时
    # 调用 _int_to_chinese_num，旧实现在 n >= 100 时抛 IndexError，异常被
    # generate_outline_v51 的 try/except 吞掉后静默回退分步路径，一次完整 AI 调用
    # 被白白浪费，日志里只剩一行 generic warning 无法定位。这里先按原始条目数做
    # O(1) 判定；下方 normalize 之后再留一道（覆盖 normalize 过滤条目后的计数）。
    if len(payload) > OUTLINE_DIRECT_MAX_LEVEL1:
        logger.warning(
            "[目录直出] 一级章数 %d 超过上限 %d，回退分步路径",
            len(payload), OUTLINE_DIRECT_MAX_LEVEL1,
        )
        return []

    # 规整结构：补齐 children / description / graph_suitable 等字段
    outline = _normalize_direct_outline(payload)

    # 规模护栏：一级章数超过上限说明模型过度展开，回退分步路径更稳妥
    if len(outline) > OUTLINE_DIRECT_MAX_LEVEL1:
        logger.warning(
            "[目录直出] 一级章数 %d 超过上限 %d，回退分步路径",
            len(outline), OUTLINE_DIRECT_MAX_LEVEL1,
        )
        return []

    # 复用分步路径的后处理链，保证两条路径的输出质量口径一致
    outline = _ensure_min_children(outline, min_level2=3, min_level3=3)
    outline, _fixes = _post_process_title_quality(outline)
    outline, _tech = _tech_enhance_titles(outline)

    total = sum(
        1 + sum(1 + len(l3) for l3 in l2.get("children", []))
        for l1 in outline for l2 in l1.get("children", [])
    )
    logger.info("[目录直出] 完成: %d 章, %d 节点（标题修正 %d，技术增强 %d）",
                len(outline), total, _fixes, _tech)
    return outline


def _unwrap_outline_payload(data: Any) -> list | None:
    """把 AI 直出响应解包成顶层数组（兼容 {outline/children/data/items: [...]}）。

    只解包、不补编号：调用方可先据此做规模护栏，避免在编号阶段才暴露异常。
    返回 None 表示不是数组形态（调用方据此回退分步路径）。
    """
    if isinstance(data, dict):
        for key in ("outline", "children", "data", "items"):
            if isinstance(data.get(key), list):
                data = data[key]
                break
    return data if isinstance(data, list) else None


def _normalize_direct_outline(data: list) -> list[dict]:
    """把直出 JSON 规整成平台统一的一级目录结构。

    兼容模型输出的几种常见变体：
      - 顶层是数组（标准）
      - 顶层是 {"outline": [...]} / {"children": [...]} 包一层
      - 节点缺 children / description 字段
    """
    payload = _unwrap_outline_payload(data)
    if payload is None:
        return []

    outline: list[dict] = []
    for idx, node in enumerate(payload):
        if not isinstance(node, dict):
            continue
        title = str(node.get("title") or node.get("name") or "").strip()
        if not title:
            continue
        item = {
            "title": title,
            "description": str(node.get("description") or node.get("content") or "").strip(),
            "graph_suitable": bool(node.get("graph_suitable", False)),
            "suggested_chart": str(node.get("suggested_chart") or node.get("chart") or ""),
            "children": [],
        }
        # 一级标题缺编号时补上（对齐审核口径「一级标题含第X章编号」）
        if not re.match(r"第[一二三四五六七八九十0-9]+章", item["title"]):
            item["title"] = f"第{_int_to_chinese_num(idx + 1)}章 {item['title']}"

        for sub in node.get("children") or []:
            if not isinstance(sub, dict):
                continue
            sub_title = str(sub.get("title") or sub.get("name") or "").strip()
            if not sub_title:
                continue
            l2 = {
                "title": sub_title,
                "description": str(sub.get("description") or sub.get("content") or "").strip(),
                "children": [],
            }
            for l3 in sub.get("children") or []:
                if not isinstance(l3, dict):
                    continue
                l3_title = str(l3.get("title") or l3.get("name") or "").strip()
                if not l3_title:
                    continue
                l2["children"].append({
                    "title": l3_title,
                    "description": str(l3.get("description") or "").strip(),
                    "children": [],
                })
            item["children"].append(l2)
        outline.append(item)
    return outline


_CN_NUMS = "零一二三四五六七八九"


def _int_to_chinese_num(n: Any) -> str:
    """0-999 转中文数字（用于补「第X章」编号）。

    BUG-FIX 2026-10-09（超长内容崩溃）：原实现 `tens, ones = divmod(n, 10)` 后
    直接 `_CN_NUMS[tens]`，当 n >= 100 时 tens >= 10 越界抛 IndexError。
    触发场景：`_normalize_direct_outline` 在**规模上限校验之前**就给每个一级标题
    补「第X章」前缀，而直出模式只限制评分点字符数（OUTLINE_DIRECT_MAX_SCORING_CHARS）
    与条目数，不限制 AI 输出的一级章数。模型一旦吐出 >= 100 个一级章节，整次直出
    因 IndexError 中断，异常被 `generate_outline_v51` 的 try/except 吞掉后静默回退
    分步路径 —— 白白浪费一次完整 AI 调用，且日志只有一行 generic warning。
    现支持到 999（百位）；n <= 0 或非数字返回空串（调用方据此跳过补编号，
    不再产出「第零章」这类脏标题）。
    """
    try:
        n = int(n)
    except (TypeError, ValueError):
        return ""
    if n <= 0:
        return ""
    if n >= 100:
        hundreds, rest = divmod(n, 100)
        _h = _CN_NUMS[hundreds % 10] + "百"
        if rest == 0:
            return _h
        if rest < 10:
            # 101 → 「一百零一」（口语「一百一」不用于正式章节编号）
            return _h + "零" + _CN_NUMS[rest]
        return _h + _int_to_chinese_num(rest)
    if n < 10:
        return _CN_NUMS[n]
    if n == 10:
        return "十"
    if n < 20:
        return "十" + _CN_NUMS[n % 10]
    tens, ones = divmod(n, 10)
    s = _CN_NUMS[tens] + "十"
    return s + (_CN_NUMS[ones] if ones else "")


# ========== 参考模板（可根据项目特点增删章节） ==========
#
# BUG-FIX 2026-09-15（中）：本软件有三份互不同步的「固定参考目录」——
#   - `outline_sort_optimizer.CORE_28_CHAPTERS`（28 章，字段 {id,title,zone}）
#   - `outline_common.FIXED_28_CHAPTERS`（28 章，字段 {id,title,fixed,dynamic,conditional}；
#     `_get_template_list_json()` 的输出，路由层读出来经 `reference_outline` 传进本工作流）
#   - 本列表（23 章）
# 三者差异（2026-09-15 实测，护栏见 tests/test_bugfix_2026_09_15_outline_module.py）：
#   - CORE 与 FIXED 的 id 序列相同，仅 id=23 标题不同：FIXED 为「与发包人、项目管理单位、
#     监理及设计人的配合」，CORE 多了「（包括投资监理）」。该差异会让排序器的
#     `CORE_ZONE_MAP` 查不到 FIXED 的 id=23 标题，只能回退关键词推断分区。
#   - 本列表相对 FIXED **缺 6 章**（文明施工措施计划、拟配备本工程的试验和检测仪器设备表、
#     任何可能的紧急情况的处理措施预案以及抵抗风险的措施、与发包人（项目管理单位、
#     监理及设计人）的配合、对总包管理及专业分包配合方案／与各参建方配合、其他），
#     并把「对总包管理及专业分包配合方案／与各参建方配合」改写为「与各参建方的配合协调」，
#     净少 5 项 = 23 章。**本列表是有意精简的兜底模板**，不是 FIXED_28 的缺陷。
# 本工作流此前**直接忽略**调用方传入的 `reference_outline`，永远用这份 23 章列表，
# 于是「改参考目录」只改一处、另一处不动，两套骨架长期漂移且无人察觉。
# 现由 `_resolve_reference_json()` 让调用方传入的参考目录优先生效，本列表仅作兜底。
# 增强 2026-10-08（反漂移）：23 章精简兜底模板由 FIXED_28_CHAPTERS 过滤派生，
# 标题与 28 章主源单一来源，新增/重命名章节时自动同步，杜绝第三份手工列表漂移。
# 剔除：文明施工(11)、检测仪器(16)、紧急情况处理(21)、与发包人配合(23)、其他(28)。
# 唯一保留的刻意改写：id24 原标题较冗长，兜底模板以「与各参建方的配合协调」措辞呈现
# （与原硬编码模板完全一致，仅在此显式登记，便于后续溯源）。
_REFERENCE_KEEP_IDS = {
    "1", "2", "3", "4", "5", "6", "7", "8", "9", "10",
    "12", "13", "14", "15", "17", "18", "19", "20", "22", "24", "25", "26", "27",
}
_REFERENCE_TITLE_REWRITE = {
    "对总包管理及专业分包配合方案／与各参建方配合": "与各参建方的配合协调",
}
REFERENCE_TEMPLATE_V51 = [
    {"id": f"ref_{i}", "title": _REFERENCE_TITLE_REWRITE.get(c["title"], c["title"])}
    for i, c in enumerate(
        (ch for ch in FIXED_28_CHAPTERS if ch["id"] in _REFERENCE_KEEP_IDS), start=1
    )
]


def _resolve_reference_json(reference_outline: Any) -> str:
    """把调用方传入的参考目录解析为提示词可用的 JSON 字符串。

    BUG-FIX 2026-09-15（中）：`generate_outline_v51(reference_outline=...)` 这个形参
    此前从未被读取过 —— 路由层从 `outline_common._get_template_list_json()` 取出现成
    的模板 JSON 一路传进来，却被静默丢弃，实际用的是硬编码的 `REFERENCE_TEMPLATE_V51`。
    这里让它真正生效。

    接受 JSON 字符串（路由层产物）、已解析的 list，或空值；非法/空值一律回退内置
    模板，保证 `<reference>` 段永远有内容、且永远是合法 JSON。

    Returns:
        str: 形如 `[{"id": "1", "title": "项目概述"}, ...]` 的合法 JSON 字符串。
    """
    candidate: Any = None
    if isinstance(reference_outline, str) and reference_outline.strip():
        try:
            candidate = json.loads(reference_outline)
        except (ValueError, TypeError) as _ref_e:
            logger.warning("V5.1 参考目录不是合法 JSON，回退内置模板: %s", _ref_e)
    elif isinstance(reference_outline, list):
        candidate = reference_outline

    if isinstance(candidate, list) and candidate:
        # 只保留 id/title：调用方可能带上 zone 等额外字段，白占提示词预算
        slim = [
            {"id": str(ch.get("id", "")), "title": str(ch.get("title", "")).strip()}
            for ch in candidate
            if isinstance(ch, dict) and str(ch.get("title", "")).strip()
        ]
        if slim:
            return json.dumps(slim, ensure_ascii=False)

    return json.dumps(REFERENCE_TEMPLATE_V51, ensure_ascii=False)


# ========== AI 调用统一入口 ==========


def _resolve_outline_temperature() -> float:
    """目录生成链路的 temperature（遗留修复：去掉无解释的硬编码）。

    目录生成刻意使用**低于**全局 `ai_config.temperature`（默认 0.7）的温度：
    L1/L2 需要稳定的章节切分与编号结构，高温会引入不必要的随机性。

    默认行为与修复前完全一致（0.1）；需要按环境调优时设置环境变量
    `OUTLINE_AI_TEMPERATURE` 即可，解析失败一律回退 0.1（向后兼容、不会因
    配置写错而阻断生成）。
    """
    raw = os.getenv("OUTLINE_AI_TEMPERATURE", "0.1")
    try:
        return float(raw)
    except (TypeError, ValueError):
        logger.warning("OUTLINE_AI_TEMPERATURE=%r 非法，回退默认 0.1", raw)
        return 0.1


async def _call_ai_v51(
    provider: Any,
    system_prompt: str,
    user_prompt: str,
    max_tokens: int = 2000,
    max_retries: int = 1,
    project_info: dict | None = None,
    temperature: float | None = None,
) -> Any:
    """V5.1 统一 AI 调用入口。

    重试逻辑由 collect_json_response 内部处理（含 JSON 自修复链路），
    这里不再包一层循环，避免嵌套重试。

    Args:
        temperature: 显式覆盖温度；None 时取 `_resolve_outline_temperature()`。
    """
    messages = [
        AIMessage(role="system", content=system_prompt),
        AIMessage(role="user", content=user_prompt),
    ]
    extra_body = None
    if project_info:
        extra_body = {
            "projectName": project_info.get("name", ""),
            "projectType": project_info.get("industry", ""),
        }
    result = await collect_json_response(
        provider,
        messages,
        temperature=_resolve_outline_temperature() if temperature is None else temperature,
        max_retries=max_retries,
        max_tokens=max_tokens,
        extra_body=extra_body,
    )
    return result


# ========== L1 一级目录生成 ==========


def _format_tender_context_block(
    tender_context: str,
    industry_profile: str = "",
    max_chars: int = 3000,
) -> str:
    """构造「招标文件上下文」提示块。

    BUG-FIX 2026-09-18（高）：`tender_context` 与 `industry_profile` 此前是
    `generate_outline_v51` 的**死参数** —— 调用方（routers/sse/sse_outline.py）
    已通过 `extract_tender_context_async` 花费解析成本提取出招标文件锚定片段并传入，
    但函数体从未引用，目录生成实际只依据 summary + 评分点 + 全局事实。后果：
    招标文件中的工期、技术标准、强制条款、特殊约定完全不参与目录构建，
    「招标文件驱动目录」这一核心能力形同虚设。

    本函数把两者格式化为受限长度的提示块，供 L1 / L2 注入。

    BUG-FIX-PROMPT-C6（截断≠防御）：原实现只做长度截断，**不做任何安全处理**。
    但 tender_context 是从招标文件抽取的原文（用户/外部内容），industry_profile
    是 LLM 生成内容，二者都可能携带提示词元标记或 PII。本模块其它注入点
    （workflows_analysis / workflows_outline_utils）均已统一施加
    sanitize_prompt_value，本函数独缺造成口径不一致。L2 阶段该函数按章节
    并发调用，暴露面被进一步放大。修复：对两段内容统一做元标记中和。
    注意：sanitize_prompt_value 不会破坏 Mermaid/JSON 结构
    （见 _security.BUG-FIX-PROMPT-S1）。
    """
    parts: list[str] = []
    ctx = (tender_context or "").strip()
    if ctx:
        if len(ctx) > max_chars:
            ctx = ctx[:max_chars] + "\n…（招标文件片段已截断）"
        parts.append(f"<tender_context>\n{sanitize_prompt_value(ctx)}\n</tender_context>\n")
    ind = (industry_profile or "").strip()
    if ind:
        if len(ind) > 800:
            ind = ind[:800] + "\n…（行业背景已截断）"
        parts.append(f"<industry_profile>\n{sanitize_prompt_value(ind)}\n</industry_profile>\n")
    if not parts:
        return ""
    parts.append(
        "（以上为招标文件原文与行业背景：章节设置须覆盖其中的强制要求、"
        "技术条款与特殊约定，不得遗漏；但不得据此改变一级章节数量）\n"
    )
    return "".join(parts)


# ===== 评分项占位过滤（BUG-FIX 2026-09-18）=====
# `normalize_writing_targets`（workflows_analysis.py）会在「评分项权重之和偏离 100
# 超过 1 个百分点」时，往评分项列表**首位插入**一条系统提示项：
#   {"id": "__weight_warning__",
#    "name": "[权重异常] 评分项权重之和为 X，请检查是否遗漏评分项"}
# 它是给用户看的提醒，**不是评分项**。`outline_gate._extract_scoring_titles` 已显式
# 过滤（outline_gate.py:439），但本模块的两处消费点漏了：
#   1. `_score_item_count = len(sp_parsed)` 把伪项计入 → 提示词告知 AI
#      「评分项总数: N+1」，且要求「每个评分项对应一个一级章节，数量与评分项严格相等」
#      → AI 会**真的多生成一个章节**去凑数，标题甚至可能直接照抄「[权重异常]…」；
#   2. `_build_l1_from_scoring_items` 兜底路径把伪项当评分项，产出同样的伪章节标题。
# 而「评分表只列技术标分数、权重合计不足 100」是极常见的真实情况 → 高频触发。
# ENHANCE 2026-09-18：判据与过滤实现统一收敛到 app.utils.scoring_points。
# 修复前本模块、outline_gate、workflows_sections、workflows_analysis 各写一份
# `__weight_warning__` 字符串字面量，新增伪项类型需改四处、漏一处即复现缺陷
# （多凑一个 "[权重异常]…" 的一级章节 / 预检恒报 1 个评分点未覆盖）。
# 这里保留旧名称做转发，既有调用方与回归用例无需改动。
_SCORE_PLACEHOLDER_IDS = SCORING_PLACEHOLDER_IDS  # 旧名称别名，保持向后兼容


def _is_scoring_placeholder(item: Any) -> bool:
    """判断评分项条目是否为系统插入的提示占位（非真实评分项）。

    转发 app.utils.scoring_points.is_scoring_placeholder（单一事实来源）。
    """
    return is_scoring_placeholder(item)


def _strip_scoring_placeholders(sp_parsed: Any) -> Any:
    """剔除评分项中的系统提示占位项。

    list 形态直接过滤；dict 形态（`{"items": [...]}`）对内部列表过滤；
    其它类型原样返回。转发 app.utils.scoring_points.strip_scoring_placeholders。
    """
    return strip_scoring_placeholders(sp_parsed)


async def _generate_level1_v51(
    provider: Any,
    summary: str,
    sp_text: str,
    reference_json: str,
    global_facts: list | None = None,
    phase_callback: Callable | None = None,
    project_info: dict | None = None,
    tender_context: str = "",
    industry_profile: str = "",
) -> list[dict]:
    """L1：根据评分要求生成一级目录标题。

    设计文档思路（标书智能体(二)第2节）：
    1. AI 接收评分要求列表，为每个评分项生成一级章节标题（rating_item → new_title）
    2. 一级标题在评分项文字基础上进行专业化调整，不能完全照搬
    3. 章节数量与评分项数量严格对应，评分要点全覆盖
    4. 输出必须是 JSON 数组，每项含 id、title、description

    Returns:
        list[dict]: 一级目录列表，每个元素 {"id": str, "title": str, "description": str, "children": []}
    """
    if phase_callback:
        await phase_callback("V5.1一级生成", "根据评分要求生成一级目录标题")

    sys_prompt = get_prompt("OUTLINE_V51_LEVEL1_SYSTEM")

    # 解析评分项数量用于覆盖校验
    _score_item_count = 0
    _sp_parsed_cache = None
    try:
        sp_parsed, _sp_mode = _parse_scoring_points_json(sp_text)
        if sp_parsed is None:
            sp_parsed = []
            if _sp_mode == "failed":
                logger.error("V5.1 L1 评分点解析失败，score_item_count 将为 0")
        # BUG-FIX 2026-09-18（高）：剔除 `__weight_warning__` 系统提示项。
        # 否则评分项数量被多算 1，提示词里的「评分项总数」与「数量严格相等」
        # 约束会把 AI 带偏，多生成一个凑数章节。
        sp_parsed = _strip_scoring_placeholders(sp_parsed)
        _sp_parsed_cache = sp_parsed
        if isinstance(sp_parsed, list):
            _score_item_count = len(sp_parsed)
        elif isinstance(sp_parsed, dict):
            _score_item_count = len(sp_parsed.get("items", sp_parsed.get("scoring_items", [])))
    except Exception as _sp_e:
        # P2 修复：原静默置0，L1 生成将不匹配任何评分项数量
        logger.warning("V5.1 L1 评分点解析失败，score_item_count=0: %s", _sp_e)
        _score_item_count = 0

    # 构建用户提示（严格遵循设计文档的 overview + requirements 结构）
    # 设计文档：“直接返回json，不要任何额外说明或格式标记”
    # BUG-FIX 2026-09-18（P1-低）：注入 <requirements> 的是原始 sp_text，仍含
    # __weight_warning__（[权重异常]…）系统伪项——上方计数已剔除但正文没剔，
    # AI 会把伪项当真实评分项处心对待。用清洗后的对象重新序列化注入。
    _requirements_text = sp_text or ''
    if _sp_parsed_cache is not None:
        try:
            _requirements_text = json.dumps(_sp_parsed_cache, ensure_ascii=False)
        except (TypeError, ValueError) as _ser_e:
            logger.warning('V5.1 L1 清洗后评分点序列化失败，回退原文注入: %s', _ser_e)
    # SECURITY-FIX 2026-10-06：summary / scoring_points / tender_context 均来自
    # 招标文件解析（用户/外部内容），手动拼入 user_prompt 前做元标记中和，
    # 防御提示词注入。
    safe_summary = sanitize_prompt_value((summary or '')[:1500])
    safe_requirements = sanitize_prompt_value(_requirements_text)
    safe_reference = sanitize_prompt_value(reference_json[:3000])
    base_user_prompt = (
        f"<overview>\n{safe_summary}\n</overview>\n"
        f"<requirements>\n{safe_requirements}\n</requirements>\n"
        f"<reference>\n{safe_reference}\n</reference>\n\n"
        f"请严格按评分要求生成一级目录。\n"
        f"评分项总数: {_score_item_count}\n"
        f"要求:\n"
        f"1. 每个评分项对应一个一级章节，数量与评分项严格相等\n"
        f"2. 章节标题在评分项文字基础上进行专业化调整\n"
        f"3. 按项目逻辑排序（总纲→组织→方案→管理→收尾）\n"
        f"4. <reference> 仅作章节体系与命名风格参考，不得据此改变章节数量\n"
        f"5. 若提供了 <tender_context>，其中的强制要求、技术条款、特殊约定必须能在章节体系中找到落点\n"
        f"6. 直接返回 JSON 数组，不要任何额外说明\n"
    )

    # BUG-FIX 2026-09-18（高）：注入招标文件上下文与行业背景（此前为死参数）。
    # 放在 <reference> 之后、全局事实之前，便于模型把「招标强制要求」与
    # 「章节体系参考」区分开。
    _tender_block = _format_tender_context_block(tender_context, industry_profile)
    if _tender_block:
        base_user_prompt += "\n" + _tender_block

    # 注入全局事实变量
    facts_text = _format_facts_list(global_facts) if isinstance(global_facts, list) else ""
    if facts_text:
        # BUG-FIX 2026-09-14（中）：截断时无标记，模型会把半截数值当成完整事实
        # （"450 日历天" → "450 日"）。补上与正文侧一致的截断提示。
        if len(facts_text) > 2000:
            facts_text = facts_text[:2000] + "\n…（事实变量已截断，以事实变量页为准）"
        facts_block = (
            "\n\n【全局事实变量】（目录章节涉及相关内容时须与之保持一致）：\n"
            f"{facts_text}\n"
        )
        base_user_prompt += facts_block

    # 最多 3 次重试（首次 + 2 次重试）
    # BUG-FIX 2026-09-18（P2）：快照基础 prompt。旧实现每次失败都往
    # base_user_prompt 上追加 correction，多次重试后旧错误信息/旧输出预览
    # 会累积污染提示（误导模型朝已失效的修正方向走）。改为每次重试以
    # 「基础 + 最新修正段」重组。
    _l1_prompt_base = base_user_prompt
    last_error = None
    last_raw = None
    for attempt in range(3):
        try:
            raw_result = await _call_ai_v51(
                provider,
                sys_prompt,
                base_user_prompt,
                max_tokens=OUTLINE_MAX_TOKENS_L1,
                max_retries=1,
                project_info=project_info,
            )
            last_raw = raw_result
            # 规范化为 list[dict]
            level1 = _normalize_to_list(raw_result, "一级目录")
            # 修正 ID（按数组位置分配）
            level1 = _assign_ids(level1)
            # 校验
            _validate_level1(level1)
            # 评分项覆盖校验
            if _score_item_count > 0:
                _check_scoring_coverage(level1, _score_item_count)
            # 确保每个节点有 children 字段
            for node in level1:
                if "children" not in node:
                    node["children"] = []
            logger.info("V5.1 L1 生成成功: %d 章 (评分项=%d)", len(level1), _score_item_count)
            # BUG-OUTLINE-PROGRESS-01 修复：L1 成功后推一条完成事件，
            # 避免前端在 AI 25s 调用期间只能看到心跳造成"卡顿"假象
            if phase_callback:
                await phase_callback(
                    "V5.1一级校验",
                    f"一级目录生成完成（{len(level1)} 章），正在进行校验...",
                )
            return level1
        except ValueError as e:
            last_error = e
            last_tb = e.__traceback__
            if attempt < 2:
                # 记录原始返回用于调试
                raw_preview = ""
                if last_raw is not None:
                    raw_preview = str(last_raw)[:300]
                # BUG-OUTLINE-PROGRESS-04 修复：L1 重试时推事件，
                # 避免 60s 超时降级后用户看不到任何进展
                if phase_callback:
                    await phase_callback(
                        "V5.1一级生成",
                        f"一级目录第 {attempt + 2}/3 次尝试（上次返回格式有误，正在修正）...",
                    )
                correction = (
                    f"\n\n【必须修正，严格遵守以下要求】\n"
                    f"错误：{e!s}\n"
                    f"上次输出预览：{raw_preview}\n"
                    f"评分项数量: {_score_item_count}\n"
                    f"请生成 JSON 数组格式，以 [ 开头、以 ] 结尾，每个元素含 id、title、description 字段。\n"
                    f"章节数量必须与评分项数量严格对应。\n"
                    f'示例：[{{"id":"1","title":"项目概述","description":"项目基本信息、规模、工期等"}},...]\n'
                    f'禁止返回 {{"result": ...}} 等包装格式！\n'
                )
                base_user_prompt = _l1_prompt_base + correction
                logger.warning(
                    "V5.1 L1 校验失败，第 %d/3 次: %s | raw=%s", attempt + 1, e, raw_preview
                )
                continue
            # 最后一次失败，尝试评分项兜底方案
            raw_info = ""
            if last_raw is not None:
                raw_info = f" | AI原始返回: {str(last_raw)[:500]}"

            # BUG-OUTLINE-12 修复：AI 多次返回单对象/少量章节时，
            # 使用评分项直接构建目录作为兜底方案，确保章节数量匹配
            if _score_item_count > 0 and _sp_parsed_cache is not None:
                try:
                    fallback_level1 = _build_l1_from_scoring_items(
                        _sp_parsed_cache, _score_item_count
                    )
                    if fallback_level1 and len(fallback_level1) >= max(1, _score_item_count - 1):
                        logger.warning(
                            "V5.1 L1 AI 生成失败，使用评分项兜底方案: %d 章 (期望=%d)",
                            len(fallback_level1),
                            _score_item_count,
                        )
                        # 修正 ID 并确保 children 字段
                        fallback_level1 = _assign_ids(fallback_level1)
                        for node in fallback_level1:
                            if "children" not in node:
                                node["children"] = []
                        return fallback_level1
                except Exception as fallback_e:
                    logger.warning("V5.1 L1 兜底方案异常: %s", fallback_e)

            raise ValueError(f"{e}{raw_info}") from e
    if last_error is not None:
        raise last_error.with_traceback(last_tb)
    raise RuntimeError('V5.1 L1 generation failed with no captured exception')


def _normalize_to_list(data: Any, context: str, _depth: int = 0) -> list[dict]:
    """将 AI 返回的各种格式规范化为 list[dict]。

    支持格式：
    - 直接数组: [...]
    - 包装对象: {"outline": [...]}、{"chapters": [...]} 等
    - 嵌套包装: {"result": "[...]"} (字符串形式的 JSON)
    - 单条记录: {"title": "..."} → 包装为 [{...}]
    - 任意 dict 中包含 JSON 字符串值的场景
    """
    # BUG-FIX 2026-09-14（中）：AI 可能返回层层嵌套的 JSON 字符串
    # （如 "{\"result\":\"[\\\"[\\\\[...]]\\\"]\"}")，原实现无递归深度上限，
    # 深嵌套时会 RecursionError —— 而 RecursionError 不是 ValueError，
    # 会直接穿透 _generate_level1_v51 的 3 次重试炸掉整个 SSE 目录任务。
    # 修复：显式深度上限，超限按 ValueError 抛出（可重试 / 可降级）。
    if _depth > 12:
        raise ValueError(f"{context}：AI 返回结构嵌套过深（>{_depth} 层），拒绝继续解析")
    _depth += 1
    if data is None:
        raise ValueError(f"{context}：AI 返回空响应")

    if isinstance(data, list):
        if not data:
            raise ValueError(f"{context}：AI 返回空数组")
        result = []
        for item in data:
            if isinstance(item, dict):
                result.append(item)
            elif isinstance(item, str) and item.strip():
                result.append({"title": item.strip(), "children": []})
        if not result:
            raise ValueError(f"{context}：解析后无有效条目")
        return result

    if isinstance(data, str):
        preview = data[:200].replace("\n", " ")
        if not preview.strip():
            raise ValueError(f"{context}：AI 返回空内容")
        # 尝试 JSON 解析
        parsed = safe_json_parse(data)
        if parsed is not None:
            return _normalize_to_list(parsed, context, _depth)
        # 尝试从文本中提取 JSON 数组
        arr_match = re.search(r"\[[\s\S]*\]", data)
        if arr_match:
            arr_str = arr_match.group(0)
            arr_parsed = safe_json_parse(arr_str)
            if isinstance(arr_parsed, list):
                return _normalize_to_list(arr_parsed, context, _depth)
        raise ValueError(f"{context}：AI 返回非 JSON 文本: {preview}")

    if isinstance(data, dict):
        # 优先级 1：常见包装键 → 取 list 并规范化每个元素
        LIST_KEYS = (
            "outline",
            "chapters",
            "items",
            "data",
            "result",
            "results",
            "children",
            "response",
            "sections",
            "list",
            "output",
            "outlines",
            "chapters_list",
            "node_list",
        )
        for key in LIST_KEYS:
            value = data.get(key)
            if isinstance(value, list):
                # 规范化每个元素（字符串 → dict，确保都是 dict）
                normalized = []
                for item in value:
                    if isinstance(item, dict):
                        normalized.append(item)
                    elif isinstance(item, str) and item.strip():
                        normalized.append({"title": item.strip(), "children": []})
                if normalized:
                    return normalized
            if isinstance(value, str) and value.strip():
                # 字符串形式的 JSON，尝试解析
                parsed = safe_json_parse(value)
                if isinstance(parsed, list):
                    # 规范化每个元素
                    normalized = []
                    for item in parsed:
                        if isinstance(item, dict):
                            normalized.append(item)
                        elif isinstance(item, str) and item.strip():
                            normalized.append({"title": item.strip(), "children": []})
                    if normalized:
                        return normalized
                # 递归处理嵌套 dict
                if isinstance(parsed, dict):
                    try:
                        return _normalize_to_list(parsed, context, _depth)
                    except ValueError:
                        pass

        # 优先级 2：检查是否为单条记录（含 title/name/heading/id 等字段）
        if any(data.get(k) for k in ("title", "name", "heading", "chapter_title")):
            return [data]

        # 优先级 3：检查是否包含嵌套 dict/list（递归查找列表）
        for key, value in data.items():
            if isinstance(value, dict):
                try:
                    return _normalize_to_list(value, f"{context}>({key})", _depth)
                except ValueError:
                    continue
            if isinstance(value, list) and len(value) > 0:
                # 可能是其他意外的列表字段 — 规范化每个元素
                normalized = []
                for item in value:
                    if isinstance(item, dict):
                        normalized.append(item)
                    elif isinstance(item, str) and item.strip():
                        normalized.append({"title": item.strip(), "children": []})
                if normalized:
                    return normalized

        # 优先级 4：尝试将整个 dict 的所有字符串值拼接后再解析
        all_strings = []
        for v in data.values():
            if isinstance(v, str):
                all_strings.append(v)
        if all_strings:
            combined = " ".join(all_strings)
            arr_match = re.search(r"\[[\s\S]*\]", combined)
            if arr_match:
                arr_str = arr_match.group(0)
                arr_parsed = safe_json_parse(arr_str)
                if isinstance(arr_parsed, list):
                    return _normalize_to_list(arr_parsed, context, _depth)

        # 最后兜底：如果 dict 有 "id" 字段，当作单条记录
        if "id" in data and ("title" in data or "name" in data or "heading" in data):
            return [data]

        raise ValueError(
            f"{context}：AI 返回无法识别的格式（dict，无列表字段）: {list(data.keys())}, "
            f"值类型: {[(k, type(v).__name__) for k, v in list(data.items())[:5]]}"
        )

    raise ValueError(f"{context}：AI 返回类型异常: {type(data).__name__}")


def _coerce_node_to_dict(node: Any) -> dict:
    """将任意节点（可能混入了 str / int / None / 其他类型）规整为 dict。

    防御性修复：上游 _normalize_to_list 偶尔漏网时，children 里会出现字符串或
    非 dict 元素，直接 node["id"] = ... 会 AttributeError。统一在此兜底。
    """
    if isinstance(node, dict):
        return node
    if isinstance(node, str):
        return {"title": node.strip(), "children": []} if node.strip() else {"title": "", "children": []}
    return {"title": str(node) if node is not None else "", "children": []}


def _assign_ids(nodes: list[dict]) -> list[dict]:
    """为节点分配连续 ID（从 1 开始），支持任意层级深度。

    防御性修复：若 nodes 中混入字符串（上游 _normalize_to_list 漏网），
    自动转换为 dict 格式，避免 str item assignment 错误。

    ENHANCE（编号命名空间统一）：改为真递归，原先硬编码只处理 L1/L2/L3 三层，
    而 outline_gate 允许 ≤6 层。AI 产出 4 层以上结构时，深层节点永远拿不到 id，
    每次自检触发 invalid-id → renumber_outline_ids 来回抖动。现与
    renumber_outline_ids 对齐，递归到任意深度。
    """

    def _walk(level_nodes: list, prefix: str) -> list:
        _out = []
        for idx, raw in enumerate(level_nodes, 1):
            node = _coerce_node_to_dict(raw)
            new_id = f"{prefix}{idx}" if not prefix else f"{prefix}.{idx}"
            node["id"] = new_id
            children = node.get("children")
            if isinstance(children, list) and children:
                node["children"] = _walk(children, new_id)
            elif children is None:
                node["children"] = []
            _out.append(node)
        return _out

    return _walk(nodes, "")


def _check_scoring_coverage(level1: list[dict], expected_count: int) -> None:
    """校验一级目录数量与评分项数量的覆盖关系。

    设计文档要求："一级标题数量要和'评分要求'一一对应"
    允许 ±1 的灵活性（AI 合并/拆分相近项时），但差异过大需警告。
    """
    actual = len(level1)
    if actual == 0:
        raise ValueError("一级目录为空，无法覆盖评分项")
    # 严格匹配：数量必须与评分项相等（±1 容错）
    if abs(actual - expected_count) > 1:
        raise ValueError(
            f"一级目录数量({actual})与评分项数量({expected_count})差异过大，"
            f"要求每个评分项对应一个章节。请调整数量至 {expected_count} 左右。"
        )
    if actual != expected_count:
        logger.warning(
            "一级目录数量(%d)与评分项数量(%d)不完全匹配（±1容错），" "章节标题应覆盖所有评分要点",
            actual,
            expected_count,
        )


def _build_l1_from_scoring_items(sp_parsed: Any, expected_count: int) -> list[dict]:
    """从评分项直接构建一级目录（AI 兜底方案）

    当 AI 无法生成足够的章节时，直接从评分项列表构建目录，
    确保章节数量与评分项数量匹配。

    Args:
        sp_parsed: 解析后的评分项数据（list 或 dict）
        expected_count: 期望的章节数量

    Returns:
        list[dict]: 一级目录列表
    """
    # 提取评分项列表
    items = []
    if isinstance(sp_parsed, list):
        items = sp_parsed
    elif isinstance(sp_parsed, dict):
        items = sp_parsed.get("items", sp_parsed.get("scoring_items", []))
        if isinstance(items, dict):
            items = [items]
    elif isinstance(sp_parsed, str):
        # 尝试解析
        try:
            parsed = json.loads(sp_parsed)
            return _build_l1_from_scoring_items(parsed, expected_count)
        except Exception as _e:
            logger.debug("[silent-except] workflows_outline_v51.py: line 519 - %s", _e)

    if not items:
        return []

    # BUG-FIX 2026-09-18（高）：兜底路径同样剔除系统提示占位项，
    # 否则会生成一个标题为「[权重异常] 评分项权重之和为 90…」的伪一级章节。
    items = [it for it in items if not _is_scoring_placeholder(it)]
    if not items:
        return []

    # 从每个评分项提取标题和描述
    level1 = []
    for i, item in enumerate(items[:expected_count]):
        if isinstance(item, dict):
            # 提取标题：尝试多个可能的字段名
            title = (
                item.get("title")
                or item.get("name")
                or item.get("item")
                or item.get("text")
                or item.get("chapter")
                or item.get("heading")
                or ""
            )
            # 提取描述
            desc = (
                item.get("description")
                or item.get("desc")
                or item.get("content")
                or item.get("summary")
                or ""
            )
            # 清理标题
            if isinstance(title, str):
                title = title.strip()
                # 移除常见编号前缀：1.1、1.1.1、（1）、1) 等
                title = re.sub(r"^[\d]+[.\s、)）]+", "", title)
                title = title.strip()
            level1.append(
                {
                    "id": str(i + 1),
                    "title": title or f"章节{i + 1}",
                    "description": desc or title or "",
                    "children": [],
                }
            )
        elif isinstance(item, str):
            title = item.strip()
            title = re.sub(r"^[\d]+[.\s、)）]+", "", title)
            level1.append(
                {
                    "id": str(i + 1),
                    "title": title or f"章节{i + 1}",
                    "description": title,
                    "children": [],
                }
            )

    return level1


# ===== 标题质量后处理 =====

# 设计文档禁用词（三级标题严禁使用）
_L3_BANNED_WORDS = frozenset(
    [
        "基本要求",
        "实施要点",
        "主要内容",
        "关键措施",
        "施工准备",
        "施工工艺",
        "质量控制",
        "通用要求",
        "基本方案",
        "实施流程",
        "质量检查",
        "验收标准",
        "管理措施",
        "安全保障",
        "进度计划",
        "资源配置",
        "其他",
        "控制措施",
        "保障措施",
        "管理制度",
        "检查制度",
        "施工方法",
        "施工要点",
    ]
)

# 通用化/占位标题模式
_GENERIC_TITLE_PATTERNS = [
    r"^专项方案（第\d+项）$",
    r"^实施要点（第\d+项）$",
    r"^专项措施（第\d+项）$",
    r"^质量控制$",
    r"^安全保障$",
    r"^进度保障$",
    r"^资源配置$",
    r"^其他$",
]


def _post_process_title_quality(level1: list[dict]) -> tuple[list[dict], int]:
    """标题质量后处理：修正通用化、低质量、占位标题。

    在 L2 生成完成后、人工审核前执行，扫描所有二三级标题：
    1. 替换通用化/占位标题（如"专项方案（第N项）"）
    2. 替换仅包含禁用词的三级标题
    3. 修正过短/过长的标题
    4. 返回修正后的目录树和修正数量

    Returns:
        tuple[list[dict], int]: (修正后的目录树, 修正标题数量)
    """
    import re as _re

    fix_count = 0

    def _is_generic_title(title: str) -> bool:
        """检查是否为通用化/占位标题"""
        if not title:
            return True
        stripped = title.strip()
        for pat in _GENERIC_TITLE_PATTERNS:
            if _re.match(pat, stripped):
                return True
        # 检查是否以"第N项"结尾（常见占位格式）
        if _re.search(r"（第\d+项）$", stripped):
            return True
        return False

    def _is_banned_l3_title(title: str) -> bool:
        """检查三级标题是否仅为禁用词或主要由禁用词构成。

        判定标准：标题与任一禁用词高度相似（编辑距离≤1或标题长度≤禁用词长度+1）。
        避免将"施工工艺流程"等质量标题误判为"施工工艺"禁用词。
        """
        if not title:
            return True
        stripped = title.strip()
        # 精确匹配禁用词
        if stripped in _L3_BANNED_WORDS:
            return True
        # 模糊匹配：禁用词必须是标题的核心且标题未显著扩展
        for banned in _L3_BANNED_WORDS:
            if banned == stripped:
                return True
            # 标题以禁用词为核心且仅附加1个字（如"质量控制措施"→"质量控制"+"措施"）
            if banned in stripped and len(stripped) <= len(banned) + 1:
                # 额外验证：标题中禁用词占比超过70%
                if len(banned) / len(stripped) >= 0.7:
                    return True
        return False

    def _generate_quality_l2_title(
        parent_title: str, index: int, sibling_count: int, used_titles: set[str] | None = None
    ) -> str:
        """为通用化 L2 标题生成质量替代标题。V5.2：从 L1 标题语义派生，不落固定模板池。"""
        from .outline_common import _derive_child_title

        used = used_titles if used_titles is not None else set()
        candidate = _derive_child_title(parent_title or "工程实施", index, used, level=2)
        return candidate

    def _generate_quality_l3_title(
        parent_title: str, grandparent_title: str, index: int, used_titles: set[str] | None = None
    ) -> str:
        """为通用化 L3 标题生成质量替代标题。V5.2：从所属 L2 标题语义派生，保证三级承接二级。"""
        from .outline_common import _derive_child_title

        # 优先从 L2 语义派生；L2 也无效时退回 L1
        source = (
            parent_title.strip()
            if parent_title and parent_title.strip()
            else (grandparent_title or "工程实施")
        )
        used = used_titles if used_titles is not None else set()
        return _derive_child_title(
            source, index, used, level=3, grandparent_title=grandparent_title
        )

    # 遍历所有节点
    for l1_idx, l1_node in enumerate(level1):
        if not isinstance(l1_node, dict):
            continue
        l1_title = l1_node.get("title", "")
        children = l1_node.get("children", [])
        if not isinstance(children, list):
            continue

        # 统计 L2 兄弟数量
        l2_count = len(children)

        # V5.2：L1 下已用 L2 标题集合（用于修正时不重名）
        used_l2_titles: set[str] = set()
        for _l2n in children:
            if isinstance(_l2n, dict) and _l2n.get("title"):
                used_l2_titles.add(_l2n["title"].strip())

        for l2_idx, l2_node in enumerate(children):
            if not isinstance(l2_node, dict):
                continue
            l2_title = l2_node.get("title", "").strip()

            # 检查 L2 标题质量
            if not l2_title or _is_generic_title(l2_title):
                new_l2 = _generate_quality_l2_title(l1_title, l2_idx, l2_count, used_l2_titles)
                logger.info(
                    "标题质量修正: L2[%d.%d] '%s' -> '%s'", l1_idx + 1, l2_idx + 1, l2_title, new_l2
                )
                used_l2_titles.discard(l2_title)
                used_l2_titles.add(new_l2)
                l2_node["title"] = new_l2
                l2_title = new_l2
                fix_count += 1

            # 检查 L3 标题质量
            l3_children = l2_node.get("children", [])
            if not isinstance(l3_children, list):
                continue
            # V5.2：L2 下已用 L3 标题集合（用于修正时不重名）
            used_l3_titles: set[str] = set()
            for _l3n in l3_children:
                if isinstance(_l3n, dict) and _l3n.get("title"):
                    used_l3_titles.add(_l3n["title"].strip())
            for l3_idx, l3_node in enumerate(l3_children):
                if not isinstance(l3_node, dict):
                    continue
                l3_title = l3_node.get("title", "").strip()
                if not l3_title or _is_generic_title(l3_title) or _is_banned_l3_title(l3_title):
                    new_l3 = _generate_quality_l3_title(l2_title, l1_title, l3_idx, used_l3_titles)
                    logger.info(
                        "标题质量修正: L3[%d.%d.%d] '%s' -> '%s'",
                        l1_idx + 1,
                        l2_idx + 1,
                        l3_idx + 1,
                        l3_title,
                        new_l3,
                    )
                    used_l3_titles.discard(l3_title)
                    used_l3_titles.add(new_l3)
                    l3_node["title"] = new_l3
                    fix_count += 1

    if fix_count > 0:
        logger.info("V5.1 标题质量后处理: 共修正 %d 个低质量标题", fix_count)

    return level1, fix_count


# ===== 技术增强后处理 =====


def _tech_enhance_titles(level1: list[dict]) -> tuple[list[dict], int]:
    """三级标题技术增强后处理：将通用标题改写为含技术含量的专业标题。

    在 L3 生成完成后、AI 自检前执行，扫描所有三级标题：
    1. 识别通用化三级标题（不含技术关键词）
    2. 基于一级/二级标题的领域匹配技术词条
    3. 生成技术增强标题（本地模板生成，零 AI 调用开销）
    4. 返回增强后的目录树和增强数量

    Returns:
        tuple[list[dict], int]: (增强后的目录树, 增强标题数量)
    """
    from .tech_knowledge import get_fallback_tech, get_tech_term_for_domain, is_generic_title

    # 已含技术关键词的三级标题特征
    _tech_indicators = (
        "BIM",
        "智能",
        "自动化",
        "实时",
        "无损",
        "高性能",
        "装配式",
        "数字孪生",
        "无人机",
        "盘扣",
        "直螺纹",
        "逆作",
        "TRD",
        "CSM",
        "MJS",
        "抗浮",
        "智慧",
        "物联网",
        "AI",
        "HRB",
        "抗震",
    )

    fix_count = 0
    for l1_idx, l1_node in enumerate(level1):
        l1_title = l1_node.get("title", "")
        l2_children = l1_node.get("children", [])
        if not isinstance(l2_children, list):
            continue

        for l2_idx, l2_node in enumerate(l2_children):
            l2_title = l2_node.get("title", "")
            l3_children = l2_node.get("children", [])
            if not isinstance(l3_children, list):
                continue

            for l3_idx, l3_node in enumerate(l3_children):
                if not isinstance(l3_node, dict):
                    continue
                l3_title = l3_node.get("title", "").strip()
                if not l3_title:
                    continue

                # 检查是否已经含有技术特征
                already_tech = any(ind in l3_title for ind in _tech_indicators)
                if already_tech:
                    continue  # 已有技术含量，跳过

                # 检查是否为通用标题
                if not is_generic_title(l3_title):
                    continue

                # 基于 L1/L2 匹配技术词条，找不到则用兜底匹配
                domain = f"{l1_title} {l2_title}"
                tech_term = get_tech_term_for_domain(domain)
                if not tech_term:
                    tech_term = get_fallback_tech(l3_title)

                # 生成技术增强标题
                new_title = _generate_tech_enhanced_title(l3_title, l2_title, tech_term)
                if new_title and new_title != l3_title:
                    logger.info(
                        "技术增强: L3[%d.%d.%d] '%s' -> '%s' (技术词条: %s)",
                        l1_idx + 1,
                        l2_idx + 1,
                        l3_idx + 1,
                        l3_title,
                        new_title,
                        tech_term,
                    )
                    l3_node["title"] = new_title
                    fix_count += 1

    if fix_count > 0:
        logger.info("V5.1 技术增强: 共增强 %d 个三级标题", fix_count)

    return level1, fix_count


def _generate_tech_enhanced_title(l3_title: str, l2_title: str, tech_term: str) -> str:
    """基于技术词条生成增强标题（本地模板，零 AI 开销）。"""
    if not tech_term:
        return l3_title

    # 提取标题核心动作
    import re

    # 匹配常见技术名词模式
    patterns = [
        # "XX施工" -> 在前面加技术
        (r"^(.+?)施工$", lambda m: f"{tech_term}{m.group(1)}施工"),
        # "XX管理" -> 改为智能管理
        (r"^(.+?)管理$", lambda m: f"{tech_term}{m.group(1)}管理"),
        # "XX控制" -> 改为技术化控制
        (r"^(.+?)控制$", lambda m: f"{tech_term}{m.group(1)}控制"),
        # "XX检查" -> 改为技术化检查
        (r"^(.+?)检查$", lambda m: f"{tech_term}{m.group(1)}检查"),
        # "XX组织" -> 改为技术化组织
        (r"^(.+?)组织$", lambda m: f"{tech_term}{m.group(1)}组织"),
        # "XX实施" -> 改为技术化实施
        (r"^(.+?)实施$", lambda m: f"{tech_term}{m.group(1)}实施"),
    ]

    for pattern, replacer in patterns:
        m = re.match(pattern, l3_title)
        if m:
            return replacer(m)

    # 默认：在标题前注入技术词条
    return f"{tech_term}{l3_title}"


def _validate_level1(level1: list[dict]) -> None:
    """校验一级目录：数量、标题有效性、无重复"""
    if not level1:
        raise ValueError("一级目录为空")

    # 检查标题
    titles = []
    for i, node in enumerate(level1):
        if not isinstance(node, dict):
            raise ValueError(f"第 {i} 项不是 dict 类型")
        title = node.get("title", "").strip()
        if not title:
            raise ValueError(f"第 {i} 项标题为空")
        if len(title) < 2 or len(title) > 60:
            raise ValueError(f"第 {i} 项标题长度异常（{len(title)}字）：{title}")
        titles.append(title)

    # 检查重复
    if len(set(titles)) != len(titles):
        # 找出重复的标题
        seen = set()
        duplicates = [t for t in titles if t in seen or seen.add(t)]
        raise ValueError(f"一级目录存在重复标题: {list(set(duplicates))[:5]}")


# BUG-FIX 2026-09-18（P2）：此处曾有 _validate_level1_v51（与上方在用校验几乎
# 同体、全仓无任何调用，仅 workflows.py 一处 re-export），属死代码已删除；
# 若未来需要严格版 L1 校验，请直接复用上方已在主流程生效的校验函数。


# ========== L2 二级目录生成（预构建骨架 + AI 填充） ==========


def _build_other_outline(level1: list[dict], current_idx: int) -> str:
    """构建"其他章节标题"文本，供 AI 参考避免重复。"""
    lines = []
    for j, node in enumerate(level1):
        if j == current_idx:
            continue
        title = node.get("title", "")
        if title:
            lines.append(f"{j + 1}. {title}")
    return "\n".join(lines)


def _build_level2_skeleton(
    level1_title: str,
    level1_index: int,
    nodes_distribution: list[dict],
    project_scope: str = "",
) -> dict:
    """预构建一级章节的 JSON 骨架（含二三级节点结构，标题留空）。

    对应设计文档：generate_one_outline_json_by_level1()

    特殊处理：当一级标题为"施工方案及技术措施"时，
    基于承包范围（project_scope）构建专项方案二级目录框架。
    """
    # 检测是否为施工方案章节
    _construction_keywords = ("施工方案",)
    is_construction_scheme = any(kw in level1_title for kw in _construction_keywords)

    if is_construction_scheme and project_scope:
        # 基于承包范围构建专项方案框架
        scope_items = _parse_scope_items(project_scope)
        return _build_construction_scheme_l2_framework(
            level1_title, level1_index, nodes_distribution, scope_items, project_scope
        )

    return generate_one_outline_json_by_level1(level1_title, level1_index, nodes_distribution)


def _build_construction_scheme_l2_framework(
    level1_title: str,
    level1_index: int,
    nodes_distribution: list[dict],
    scope_items: list[str],
    project_scope: str = "",
) -> dict:
    """为"施工方案及技术措施"章节构建基于承包范围的二三级目录框架。

    核心设计：
    1. 二级目录（专项方案）引用承包范围中的分部工程
    2. 结合工程概况补充通用施工管理类二级目录
    3. 三级目录按每个专项方案独立生成

    Args:
        level1_title: 一级目录标题
        level1_index: 一级目录索引（1-based）
        nodes_distribution: 节点分布
        scope_items: 承包范围分部工程列表
        project_scope: 原始承包范围字符串

    Returns:
        dict: 带二级节点的 JSON 框架（标题留空待 AI 填充）
    """

    # 边界检查：确保 level1_index 在 nodes_distribution 范围内
    if level1_index < 1 or level1_index > len(nodes_distribution):
        logger.warning(
            "_build_construction_scheme_l2_framework: level1_index %d 超出范围 (1~%d)，使用默认分布",
            level1_index,
            len(nodes_distribution),
        )
        total_l2 = 5  # 默认5个二级节点
        leaf_distribution = [3] * total_l2  # 每个二级下默认3个三级
    else:
        dist = nodes_distribution[level1_index - 1]
        total_l2 = dist["level2_count"]
        leaf_distribution = dist["leaf_per_level2"]

    # 确保 leaf_distribution 至少有 total_l2 个元素
    if len(leaf_distribution) < total_l2:
        leaf_distribution = leaf_distribution + [3] * (total_l2 - len(leaf_distribution))

    # 施工方案章节的固定二级项（用于补充承包范围不足的情况）
    _fixed_construction_l2 = [
        ("施工区段划分与流水组织", "施工组织类"),
        ("施工总平面布置与管理", "施工组织类"),
        ("施工进度计划与保障措施", "进度管理类"),
        ("资源配置与供应保障", "资源管理类"),
        ("质量保证措施与管控体系", "质量管理类"),
        ("安全文明施工与职业健康", "安全管理类"),
        ("季节性施工措施", "施工措施类"),
        ("绿色施工与环境保护", "环保管理类"),
    ]

    # 构建二级节点：承包范围项 + 补充项
    children = []

    # 1. 承包范围项（每个分部工程作为一个专项方案）
    _std_names = {
        "主体结构",
        "砌体",
        "装饰装修",
        "机电安装",
        "给排水",
        "采暖",
        "通风",
        "空调",
        "电气",
        "智能建筑",
        "电梯",
        "节能",
        "屋面",
        "土方",
        "基础",
        "桩基",
        "防水",
        "保温",
        "幕墙",
        "消防",
    }

    scope_count = min(len(scope_items), max(2, total_l2 - 2))
    for i in range(scope_count):
        item = scope_items[i]
        # 标题处理：承包范围术语原样保留
        if item.endswith("工程") or item in _std_names or "结构" in item or "装修" in item:
            l2_title = f"{item}专项施工方案"
        else:
            l2_title = f"{item}专项施工方案"

        leaf_count = (
            leaf_distribution[len(children)] if len(children) < len(leaf_distribution) else 3
        )
        level2_node = {
            "id": f"{level1_index}.{len(children) + 1}",
            "title": "",  # 由 AI 填充具体标题
            "description": f"基于承包范围的{l2_title}",
            "level": 2,
            "children": [],
        }
        for k in range(leaf_count):
            level2_node["children"].append(
                {
                    "id": f"{level1_index}.{len(children) + 1}.{k + 1}",
                    "title": "",  # 由 AI 填充
                    "description": "",
                    "level": 3,
                    "children": [],
                }
            )
        children.append(level2_node)

    # 2. 补充通用施工管理类二级项
    remaining = total_l2 - len(children)
    if remaining > 0:
        _fixed_pool = _fixed_construction_l2[:]
        random.shuffle(_fixed_pool)
        for i in range(min(remaining, len(_fixed_pool))):
            fixed_title, _cat = _fixed_pool[i]
            leaf_count = (
                leaf_distribution[len(children)] if len(children) < len(leaf_distribution) else 3
            )
            level2_node = {
                "id": f"{level1_index}.{len(children) + 1}",
                "title": "",  # 由 AI 填充
                "description": fixed_title,
                "level": 2,
                "children": [],
            }
            for k in range(leaf_count):
                level2_node["children"].append(
                    {
                        "id": f"{level1_index}.{len(children) + 1}.{k + 1}",
                        "title": "",  # 由 AI 填充
                        "description": "",
                        "level": 3,
                        "children": [],
                    }
                )
            children.append(level2_node)

    # 构建一级节点
    level1_node = {
        "id": str(level1_index),
        "title": level1_title,
        "description": f"施工方案及技术措施（承包范围：{project_scope[:100]}）",
        "level": 1,
        "children": children,
    }
    return level1_node


async def _fill_level2_with_ai(
    provider: Any,
    skeleton: dict,
    level1_title: str,
    level1_index: int,
    summary: str,
    sp_text: str,
    other_outline: str,
    project_info: dict | None = None,
    project_scope: str = "",
    global_facts: list | None = None,
    tender_context: str = "",
    boq_text: str = "",
    review_suggestions: list | None = None,
) -> dict:
    """用 AI 填充预构建骨架中的二三级标题。

    设计文档思路：
    1. 传给 AI 的是预构建 JSON 骨架（title 留空）
    2. AI 只在原结构上补全标题和描述，禁止修改结构
    3. 传入其他章节标题供参考，避免内容重叠
    4. 校验 AI 输出结构与骨架一致

    ENHANCE 2026-10-09（文档五·审核回灌）：新增 `review_suggestions`。
    目录门禁/AI 语义审产出的修改建议回灌到重生成提示词，让模型「按建议改」
    而不是重新抽卡——弱模型改一个接近正确的结果，成功率远高于重新发挥。

    特殊处理：当 level1_title 为"施工方案及技术措施"时，
    传入承包范围和工程概况上下文，指导 AI 生成针对性专项方案标题。

    Returns:
        dict: 填充后的完整节点
    """
    sys_prompt = get_prompt("OUTLINE_V51_LEVEL2_SYSTEM")

    # 将骨架转为 JSON 字符串传给 AI
    skeleton_json = json.dumps(skeleton, ensure_ascii=False)

    # 检测是否为施工方案章节
    _is_construction = "施工方案" in level1_title and project_scope
    _is_general_construction = "施工方案" in level1_title and not project_scope

    # 构建用户提示（严格遵循设计文档结构）
    # 设计文档 L2 UserPrompt: overview + requirements + other_outline + skeleton
    scope_block = ""
    if _is_construction:
        scope_items = _parse_scope_items(project_scope)
        scope_list_str = "、".join(scope_items)
        scope_block = (
            f"<contracting_scope>\n承包范围：{project_scope[:300]}\n"
            f"分部工程：{scope_list_str}\n"
            f"要求：二级目录必须基于分部工程生成专项施工方案，三级目录紧扣施工工艺\n"
            f"</contracting_scope>\n"
        )
    elif _is_general_construction:
        scope_block = (
            "<construction_context>\n章节为施工方案及技术措施，"
            "需生成覆盖土方、基础、主体结构、装饰装修、机电安装等分部工程的专项施工方案\n"
            "</construction_context>\n"
        )

    # BUG-FIX 2026-09-14（中·质量）：L1 一直注入全局事实，但 L2 从来没有
    # （_generate_level2_v51 / _fill_level2_with_ai 签名里都没有 global_facts）。
    # 结果二三级标题里的具体数值（工期、人数、面积、型号、标准编号）完全不受
    # 事实约束，与 L1、与正文自相矛盾。这里补上与 L1 一致的事实块。
    facts_block = ""
    if global_facts:
        _l2_facts_text = _format_facts_list(global_facts)
        if _l2_facts_text:
            if len(_l2_facts_text) > 2000:
                _l2_facts_text = _l2_facts_text[:2000] + "\n…（事实变量已截断）"
            facts_block = (
                f"<global_facts>\n{_l2_facts_text}\n</global_facts>\n"
                "（二级/三级标题若涉及上述事实，措辞须与之保持一致，禁止改写数值）\n"
            )

    # BUG-FIX 2026-09-18（高）：L2 同样注入招标文件上下文。二三级标题是
    # 「专项方案 / 施工工艺」的落点，若拿不到招标文件里的工程内容与技术要求，
    # 生成出的专项方案标题会与实际工程脱节。长度上限比 L1 更紧（1500 字符），
    # 因为 L2 是并发的 N 次调用，上下文开销会被放大 N 倍。
    tender_block = ""
    if tender_context:
        tender_block = _format_tender_context_block(tender_context, "", max_chars=1500)
        if tender_block:
            tender_block = tender_block + "（二三级标题须覆盖上述要求中的具体工艺/工序）\n"

    # BUG-FIX 2026-09-18（P1）：boq_json（清单工程量）此前是 write-only 死字段——
    # 分析模块产出后目录生成完全不消费。接线到 L2 提示词：二级/三级标题是
    # 分部分项工程的落点，拿不到清单条目会导致目录与实际工程量脱节。
    boq_block = ""
    _boq_compact = (boq_text or "").strip()
    if _boq_compact and _boq_compact not in ("[]", "{}", "''"):
        # 只取条目名称级信息，控制 token（L2 是 N 路并发，开销按 N 放大）
        boq_block = (
            f"<boq_summary>\n{_boq_compact[:1200]}\n</boq_summary>\n"
            "（二级/三级标题涉及的分部分项工程应在上述清单项目中能找到对应，不得凭空发明清单外的大项）\n"
        )

    # ENHANCE 2026-10-09（文档五·审核回灌）：把上一版目录的质量审核建议注入，
    # 让本轮填充「按建议修正」而非重新抽卡。建议为空时该块为空串，
    # 首轮生成行为与改造前完全一致（不引入回归）。
    suggestions_block = ""
    if review_suggestions:
        _sg_lines = "\n".join(
            f"- {str(s).strip()}" for s in review_suggestions if s and str(s).strip()
        )
        if _sg_lines:
            suggestions_block = (
                f"<review_suggestions>\n{_sg_lines}\n</review_suggestions>\n"
                "（上一版目录经质量审核后提出上述修改建议，本次填充必须逐条落实；"
                "与建议无关的章节保持原有写法，不要整体重写）\n"
            )

    # SECURITY-FIX 2026-10-06：summary / sp_text / boq_text / tender_context 等
    # 均来自招标文件解析（用户/外部内容），手动拼入 user_prompt 前做元标记中和。
    safe_summary = sanitize_prompt_value((summary or '')[:1200])
    safe_sp = sanitize_prompt_value(sp_text[:800])
    safe_other = sanitize_prompt_value(other_outline)
    safe_title = sanitize_prompt_value(level1_title)
    safe_skeleton = sanitize_prompt_value(skeleton_json)
    user_prompt = (
        f"<overview>\n{safe_summary}\n</overview>\n"
        f"<requirements>\n{safe_sp}\n</requirements>\n"
        f"{facts_block}"
        f"{tender_block}"
        f"{boq_block}"
        f"<other_outline>\n{safe_other}\n</other_outline>\n"
        f"{scope_block}"
        f"{suggestions_block}"
        f"<chapter_title>\n{safe_title}\n</chapter_title>\n"
        f"<json_outline>\n{safe_skeleton}\n</json_outline>\n\n"
        f"请在原 JSON 骨架上补全 title 和 description。直接返回填充后的 JSON，不要额外说明。\n"
    )

    # 最多 3 次重试
    # BUG-FIX 2026-09-18（P2）：同 L1，快照基础 prompt，重试时以「基础 + 最新
    # 修正段」重组，避免 correction 逐次累加污染提示。
    _l2_prompt_base = user_prompt
    last_error = None
    last_tb = None  # BUG-FIX: 存 traceback
    for attempt in range(3):
        try:
            if attempt > 0:
                logger.info(
                    "_fill_level2_with_ai: 第 %d 次重试, level1='%s', idx=%d",
                    attempt + 1,
                    level1_title,
                    level1_index,
                )
            result = await _call_ai_v51(
                provider,
                sys_prompt,
                user_prompt,
                max_tokens=OUTLINE_MAX_TOKENS_L2,
                max_retries=1,
                project_info=project_info,
            )
            # 解析 AI 返回
            if isinstance(result, str):
                result = safe_json_parse(result)
            if result is None:
                raise ValueError("AI 返回空响应")

            # 解析 AI 返回的 children 数据
            merged_children = None

            if isinstance(result, dict) and "children" in result:
                # AI 返回 {children: [...]} 或 {id, title, children: [...]}
                merged_children = result.get("children", [])
            elif isinstance(result, list):
                # AI 直接返回 children 数组
                merged_children = result
            elif isinstance(result, dict) and "id" in result and "children" in result:
                # AI 返回完整的一级节点结构
                merged_children = result.get("children", [])
            elif isinstance(result, dict):
                # AI 返回未识别的 dict 格式，尝试提取列表
                for key in ("children", "items", "chapters", "outline"):
                    if key in result and isinstance(result[key], list):
                        merged_children = result[key]
                        break

            if merged_children is None:
                raise ValueError(
                    f"AI 返回格式无法识别: {type(result).__name__}, keys={list(result.keys()) if isinstance(result, dict) else 'N/A'}"
                )

            # 预校验：验证 AI 返回的 children 结构与骨架兼容
            skeleton_children = skeleton.get("children", [])
            if merged_children and skeleton_children:
                # 用骨架的第一个子节点作为结构模板
                template_child = skeleton_children[0]
                valid, err = check_json(
                    json.dumps(merged_children, ensure_ascii=False),
                    [template_child],  # 列表模板
                )
                if not valid:
                    logger.warning("AI 返回 children 结构与骨架不兼容: %s", err)
                    # 不直接失败，让合并逻辑处理不兼容情况

            # 将填充的 children 合并到骨架中
            # BUG-FIX 2026-09-14（高）：原实现在 merge **之后**才取 skeleton 当 schema，
            # 等于拿填充结果自己校验自己 → check_json 恒为 True，AI 增删节点、
            # 篡改 id/level 等结构性破坏 100% 漏检；且 `raise ... from err` 里
            # err 是 str，一旦真触发会抛 TypeError（不在 except ValueError 内）
            # 直接穿透 3 次重试炸掉整个目录生成任务。
            # 修复：merge 前深拷贝骨架作为 schema；异常链改为显式拼接错误信息。
            expected_schema = copy.deepcopy(skeleton)
            skeleton["children"] = _merge_filled_children(skeleton_children, merged_children)

            # 结构校验：验证填充后结构与骨架一致
            valid, err = check_json(
                json.dumps(skeleton, ensure_ascii=False),
                expected_schema,
            )
            if not valid:
                raise ValueError(f"结构校验失败: {err}")

            # 内容校验：检查标题是否被填充
            _check_titles_filled(skeleton, level1_title)

            logger.info(
                "_fill_level2_with_ai 成功: level1='%s', idx=%d, 二级=%d, 三级=%d, attempt=%d",
                level1_title,
                level1_index,
                len(skeleton.get("children", [])),
                sum(len(c.get("children", [])) for c in skeleton.get("children", [])),
                attempt + 1,
            )
            return skeleton

        except ValueError as e:
            last_error = e
            last_tb = e.__traceback__
            if attempt < 2:
                correction = (
                    f"\n\n【上次返回有误，请修正】\n"
                    f"错误：{e!s}\n"
                    f"请在原 JSON 骨架上补全 title 和 description，禁止修改结构。\n"
                )
                user_prompt = _l2_prompt_base + correction
                logger.warning("L2 填充失败 %s，第 %d/3 次: %s", level1_title, attempt + 1, e)
                continue
            raise
    if last_error is not None:
        raise last_error.with_traceback(last_tb)
    raise RuntimeError(chr(34)+chr(76)+chr(50)+chr(32)+chr(102)+chr(105)+chr(108)+chr(108)+chr(32)+chr(102)+chr(97)+chr(105)+chr(108)+chr(101)+chr(100)+chr(32)+chr(119)+chr(105)+chr(116)+chr(104)+chr(32)+chr(110)+chr(111)+chr(32)+chr(99)+chr(97)+chr(112)+chr(116)+chr(117)+chr(114)+chr(101)+chr(100)+chr(32)+chr(101)+chr(120)+chr(99)+chr(101)+chr(112)+chr(116)+chr(105)+chr(111)+chr(110)+chr(34))


def _merge_filled_children(
    skeleton_children: list[dict],
    ai_filled: Any,
) -> list[dict]:
    """将 AI 填充的标题合并回骨架。

    AI 可能返回多种格式，此函数负责：
    1. 找到对应的骨架节点
    2. 用 AI 返回的 title/description 填充
    3. 保留骨架的 id 和层级结构

    增强：
    - 处理 AI 返回 dict 而非 list 的情况
    - 处理空骨架或空 AI 返回
    - 容错：跳过非 dict 类型的异常节点
    - BUG 修复：AI 返回节点数量不足时，对剩余骨架节点标记警告
    - BUG 修复：AI 返回节点数量过多时，记录多余节点数量
    """
    if not skeleton_children:
        return skeleton_children

    # 容错：处理 AI 返回非 list 的情况
    if not isinstance(ai_filled, list):
        if isinstance(ai_filled, dict) and "children" in ai_filled:
            ai_filled = ai_filled.get("children", [])
        else:
            return skeleton_children

    if not ai_filled:
        return skeleton_children

    # 建立 AI 返回的 title 映射（按顺序对应骨架节点）
    skipped_count = 0
    unfilled_count = 0
    extra_count = 0

    # BUG 修复：检查 AI 返回数量是否超出骨架数量
    if len(ai_filled) > len(skeleton_children):
        extra_count = len(ai_filled) - len(skeleton_children)
        logger.warning(
            "_merge_filled_children: AI 返回 %d 个节点，骨架仅 %d 个，多余 %d 个将被忽略",
            len(ai_filled),
            len(skeleton_children),
            extra_count,
        )

    for i, skeleton_node in enumerate(skeleton_children):
        if i >= len(ai_filled):
            # BUG 修复：AI 返回节点不足，剩余骨架节点保留空标题但计数
            if not skeleton_node.get("title", "").strip():
                unfilled_count += 1
            continue

        ai_node = ai_filled[i]
        if not isinstance(ai_node, dict):
            # 跳过非 dict 类型的异常节点
            skipped_count += 1
            continue

        # 填充二级标题
        ai_title = ai_node.get("title", "").strip()
        if ai_title:
            skeleton_node["title"] = ai_title
        ai_desc = ai_node.get("description", "").strip()
        if ai_desc:
            skeleton_node["description"] = ai_desc

        # 填充三级标题
        ai_children = ai_node.get("children", [])
        skeleton_children_nodes = skeleton_node.get("children", [])
        if isinstance(ai_children, list):
            for j, leaf_node in enumerate(skeleton_children_nodes):
                if j >= len(ai_children):
                    # AI 返回的三级节点不足，剩余骨架三级节点保留空标题
                    if not isinstance(leaf_node, dict) or not leaf_node.get("title", "").strip():
                        unfilled_count += 1
                    break
                ai_leaf = ai_children[j]
                if not isinstance(ai_leaf, dict):
                    continue
                leaf_title = ai_leaf.get("title", "").strip()
                if leaf_title:
                    leaf_node["title"] = leaf_title
                leaf_desc = ai_leaf.get("description", "").strip()
                if leaf_desc:
                    leaf_node["description"] = leaf_desc

    if skipped_count > 0:
        logger.warning("_merge_filled_children: 跳过 %d 个非dict类型的AI返回节点", skipped_count)
    if unfilled_count > 0:
        logger.warning(
            "_merge_filled_children: %d 个骨架节点标题仍为空（AI未填充）", unfilled_count
        )
    if extra_count > 0:
        logger.warning(
            "_merge_filled_children: %d 个AI返回节点被忽略（骨架无对应位置）", extra_count
        )

    return skeleton_children


def _check_titles_filled(node: dict, parent_title: str) -> None:
    """检查节点标题是否已被 AI 填充（不再是空字符串）。"""
    if not isinstance(node, dict):
        return
    title = node.get("title", "")
    level = node.get("level", 2)
    if level == 2 and not title:
        raise ValueError(f"二级目录标题为空: {parent_title}")
    children = node.get("children", [])
    if isinstance(children, list):
        for child in children:
            child_title = child.get("title", "")
            child_level = child.get("level", 3)
            if child_level == 3 and not child_title:
                raise ValueError(f"三级目录标题为空: {parent_title}/{title}")
            _check_titles_filled(child, title)


async def _generate_level2_v51(
    provider: Any,
    level1: list[dict],
    summary: str,
    sp_text: str,
    progress_callback: Callable | None = None,
    phase_callback: Callable | None = None,
    l2_done_queue: asyncio.Queue | None = None,
    project_info: dict | None = None,
    project_scope: str = "",
    task_semaphore: DynamicSemaphore | None = None,
    global_facts: list | None = None,
    tender_context: str = "",
    boq_text: str = "",
    review_suggestions: list | None = None,
) -> list[dict]:
    """L2：并发生成二级目录（预构建骨架 + AI 填充模式）。

    设计文档流程：
    1. 用 generate_nodes_distribution 预构建每个一级章节的 JSON 骨架
    2. 并发生成：每个一级章节独立调用 AI 填充
    3. 传入其他章节标题避免重复
    4. 结构校验 + 3 次重试
    5. 单节点失败用默认目录填充，不中止整体流程

    ENHANCE 2026-10-09（文档五·审核回灌）：`review_suggestions` 透传给
    `_fill_level2_with_ai`，用于「审核不通过 → 带建议重生成」的第二轮。

    特殊处理：当一级标题为"施工方案及技术措施"时，
    基于承包范围（project_scope）构建专项方案二级目录框架。

    BUG-OUTLINE-09 修复：使用任务级信号量（task_semaphore）替代全局变量，
    避免多任务并发时信号量配置互相干扰。
    """
    if phase_callback:
        await phase_callback("V5.1二级生成", "预构建骨架并并发填充二级目录")

    total = len(level1)
    logger.info(
        "V5.1 L2 开始生成: 一级章节数=%d, project_scope=%s",
        total,
        project_scope[:100] if project_scope else "none",
    )

    # 生成节点分布（每个一级下的二级数量和三级数量）
    nodes_dist = generate_nodes_distribution(total, min_level2=3, min_level3=5)

    # 初始化并发信号量（任务级，避免多任务竞争）
    # BUG-OUTLINE-09 修复：优先使用任务级信号量，否则创建新的
    if task_semaphore is not None:
        sem = task_semaphore
    else:
        provider_base_url = getattr(provider, "base_url", "")
        max_concurrent = 16 if "anthropic" in provider_base_url else 32
        sem = DynamicSemaphore(max_concurrent)

    # 收集骨架（预构建，不需要 AI）
    skeletons: list[dict] = []
    for idx, l1_node in enumerate(level1):
        l1_title = l1_node.get("title", f"第{idx + 1}章")
        skeleton = _build_level2_skeleton(
            l1_title, idx + 1, nodes_dist, project_scope=project_scope
        )
        skeletons.append(skeleton)

    # 并发生成每个一级章节的二级+三级目录
    results: list[dict | None] = [None] * total
    _l2_completed = {"n": 0}  # 共享计数器（dict 是可变对象，闭包能修改）

    async def _fill_one(idx: int) -> dict:
        l1_node = level1[idx]
        l1_title = l1_node.get("title", f"第{idx + 1}章")
        skeleton = skeletons[idx]
        other_outline = _build_other_outline(level1, idx)

        # BUG-OUTLINE-PROGRESS-03 修复：子任务开始时立即推一条 "正在生成" 事件，
        # 让前端在 AI 60s 调用期间也能看到实时进展，而不是等 AI 全跑完才看到
        if progress_callback is not None:
            try:
                await progress_callback(
                    _l2_completed["n"], total, f"正在生成：{l1_title}..."
                )
            except Exception as _e:
                logger.debug("[silent-except] workflows_outline_v51.py: line 1522 - %s", _e)

        async with sem:
            try:
                _t0 = time.monotonic()
                filled = await _fill_level2_with_ai(
                    provider,
                    skeleton,
                    l1_title,
                    idx + 1,
                    summary,
                    sp_text,
                    other_outline,
                    project_info=project_info,
                    project_scope=project_scope,
                    global_facts=global_facts,
                    tender_context=tender_context,
                    boq_text=boq_text,
                    review_suggestions=review_suggestions,
                )
                results[idx] = filled
                if l2_done_queue is not None:
                    await l2_done_queue.put((idx, filled))
                # BUG-OUTLINE-PROGRESS-02 修复：并发子任务成功完成后，
                # 在子任务内部立即调 progress_callback 推事件到 SSE 队列
                _l2_completed["n"] += 1
                logger.info(
                    "L2[%d/%d] 完成: %s (耗时 %.1fs)",
                    _l2_completed["n"], total, l1_title, time.monotonic() - _t0,
                )
                if progress_callback is not None:
                    try:
                        await progress_callback(_l2_completed["n"], total, l1_title)
                    except Exception as _cb_err:
                        logger.debug("L2 progress_callback(ok) 异常: %s", _cb_err)
                return filled
            except Exception as e:
                logger.error(
                    "L2[%d/%d] 失败 idx=%d %s: %s (耗时 %.1fs)",
                    _l2_completed["n"] + 1, total, idx, l1_title, e, time.monotonic() - _t0,
                )
                _l2_completed["n"] += 1
                if progress_callback is not None:
                    try:
                        await progress_callback(_l2_completed["n"], total, f"{l1_title}(失败)")
                    except Exception as _cb_err:
                        logger.debug("L2 progress_callback(err) 异常: %s", _cb_err)
                raise

    # 并发执行：每个 _fill_one 已在完成时自己推 progress_callback
    tasks = [asyncio.create_task(_fill_one(i)) for i in range(total)]
    gather_results = await asyncio.gather(*tasks, return_exceptions=True)

    # 处理结果（gather_results 里是返回值 dict 或 Exception）
    l2_failed: list[str] = []
    for idx, result in enumerate(gather_results):
        if isinstance(result, Exception):
            l1_title = level1[idx].get("title", f"第{idx + 1}章")
            l2_failed.append(l1_title)
            logger.warning("V5.1 L2 章节失败 %s，使用默认目录", l1_title)
            # 默认填充（携带父一级章节标题前缀，避免默认标题跨章节重名）
            level1[idx]["children"] = _default_l2_children_for_skeleton(skeletons[idx], l1_title)
            if l2_done_queue is not None:
                await l2_done_queue.put((idx, level1[idx]))
        else:
            if results[idx] is not None:
                level1[idx] = results[idx]

    # 发送哨兵通知 L3 消费者
    if l2_done_queue is not None:
        await l2_done_queue.put(None)

    if l2_failed:
        logger.warning("V5.1 L2 %d/%d 章节失败，已用默认目录: %s", len(l2_failed), total, l2_failed)

    # 汇总日志
    total_l2_count = sum(len(l1.get("children", [])) for l1 in level1)
    total_l3_count = sum(
        len(l2.get("children", [])) for l1 in level1 for l2 in l1.get("children", [])
    )
    logger.info(
        "V5.1 L2 生成完成: 一级=%d, 二级=%d, 三级=%d, 失败=%d",
        total,
        total_l2_count,
        total_l3_count,
        len(l2_failed),
    )

    # 修正所有 ID
    level1 = _fix_all_ids(level1)
    return level1


def _default_l2_children_for_skeleton(skeleton: dict, l1_title: str = "") -> list[dict]:
    """基于骨架结构生成默认填充（保持结构不变，仅填充默认标题）。

    兜底原则（V5.2）：不使用固定模板/占位标题，所有兜底标题一律
    「从上一级目录语义派生」——L2 从 L1 章节标题派生、L3 从所属 L2 派生，
    保证：
    - 兜底 L2 仍与一级章节主题对应（如 L1="质量保证措施和创优计划"
      → L2="质量总体方案与实施策划"/"质量控制与验收要求"…）
    - 兜底 L3 与所属 L2 语义关联，不再出现"实施要点（第N项）"这类占位
    - 不同章节退化到兜底时，因父级标题不同而天然不重名
    """
    from .outline_common import _derive_child_title

    _parent = l1_title.strip() if l1_title and l1_title.strip() else "工程实施"
    children = skeleton.get("children", [])
    defaults = []
    used_l2: set[str] = set()
    for i, child in enumerate(children):
        n = len(defaults)
        base = _derive_child_title(_parent, n, used_l2, level=2)
        used_l2.add(base)
        _l2_title = base
        l2_default = {
            "id": child.get("id", f"X.{i + 1}"),
            "title": _l2_title,
            "description": "",
            "level": 2,
            "children": [],
        }
        # 三级默认标题从所属 L2 派生（同时把 L1 作为 grandparent_title 传入，
        # 使通用型 L2 标题的 L3 兜底能继承 L1 的专业域，如 L1=质量管理措施 → quality 域而非 work 域）
        used_l3: set[str] = set()
        for j, leaf in enumerate(child.get("children", [])):
            _l3_title = _derive_child_title(_l2_title, j, used_l3, level=3, grandparent_title=_parent)
            used_l3.add(_l3_title)
            l2_default["children"].append(
                {
                    "id": leaf.get("id", f"X.{i + 1}.{j + 1}"),
                    "title": _l3_title,
                    "description": "",
                    "level": 3,
                    "children": [],
                }
            )
        defaults.append(l2_default)
    # 确保至少 3 个二级
    while len(defaults) < 3:
        idx = len(defaults)
        _l2_title = _derive_child_title(_parent, idx, used_l2, level=2)
        used_l2.add(_l2_title)
        l2_default = {
            "id": f"X.{idx + 1}",
            "title": _l2_title,
            "description": "",
            "level": 2,
            "children": [],
        }
        used_l3: set[str] = set()
        for j in range(3):
            _l3_title = _derive_child_title(_l2_title, j, used_l3, level=3, grandparent_title=_parent)
            used_l3.add(_l3_title)
            l2_default["children"].append(
                {
                    "id": f"X.{idx + 1}.{j + 1}",
                    "title": _l3_title,
                    "description": "",
                    "level": 3,
                    "children": [],
                }
            )
        defaults.append(l2_default)
    return defaults


def _fix_all_ids(level1: list[dict]) -> list[dict]:
    """修正所有节点的 ID（按层级位置分配），支持任意层级深度。

    BUG-FIX-82: 添加类型检查，防止非 dict 元素导致 AttributeError。
    ENHANCE（编号命名空间统一）：改为真递归，原硬编码仅 L1/L2/L3 三层，与
    renumber_outline_ids（递归任意深）口径不一致；现统一为任意深度。
    """
    def _walk(nodes: list, prefix: str) -> None:
        for idx, node in enumerate(nodes, 1):
            if not isinstance(node, dict):
                continue
            new_id = f"{prefix}{idx}" if not prefix else f"{prefix}.{idx}"
            node["id"] = new_id
            _walk(node.get("children", []) or [], new_id)

    _walk(level1, "")
    return level1


def _fix_ids_v51(level1: list[dict]) -> list[dict]:
    """修正整个目录树的 ID 编号（与 _fix_all_ids 兼容的测试专用接口）

    递归分配 ID，支持任意层级深度：
    - L1: 1, 2, 3, ...
    - L2: 1.1, 1.2, ...
    - L3: 1.1.1, 1.1.2, ...
    - 处理缺少 children 键、空 children、非 dict 子节点等边界情况
    """
    def _walk(nodes: list, prefix: str) -> None:
        for idx, node in enumerate(nodes, 1):
            if not isinstance(node, dict):
                # BUG-FIX-83：与原 _fix_all_ids 对齐，非 dict 子节点跳过而非 AttributeError
                continue
            new_id = f"{prefix}{idx}" if not prefix else f"{prefix}.{idx}"
            node["id"] = new_id  # BUG-FIX-83: 统一使用字符串 ID，与 _fix_all_ids 保持一致
            children = node.get("children")
            if children:
                _walk(children, new_id)

    _walk(level1, "")
    return level1


# ========== L3 三级目录生成（流水线模式） ==========

# 关键词到场景模板的映射
_L2_KEYWORD_TO_SCENE: list[tuple[list[str], str]] = [
    # 质量管理
    (["质量", "质保", "创优", "验收"], "quality_system"),
    # 安全管理
    (["安全", "安保", "消防", "应急", "防护"], "safety_system"),
    # 进度/工期
    (["进度", "工期", "计划", "节点", "里程碑"], "progress"),
    # 施工方案专项（承包范围分部工程）
    (["专项施工方案", "施工方案", "专项方案", "施工组织"], "construction_scheme"),
    # 技术/工艺/方案
    (["技术", "工艺", "施工方法", "专项"], "technical"),
    # 资源/材料/设备
    (["资源", "材料", "设备", "劳动力", "资金", "采购", "供应"], "resource"),
    # 项目管理
    (["项目", "组织", "协调", "沟通", "部署"], "project"),
    # 施工工艺（具体分部工程）
    (
        [
            "施工",
            "土方",
            "钢筋",
            "混凝土",
            "模板",
            "砌体",
            "防水",
            "装饰",
            "安装",
            "脚手架",
            "桩基",
            "基础",
            "主体结构",
            "机电",
        ],
        "construction_method",
    ),
    # 成本管理
    (["成本", "造价", "预算", "费用", "投资"], "cost_management"),
    # 合同与法务
    (["合同", "法规", "法律", "保险", "索赔", "变更"], "contract_legal"),
    # 信息化
    (["信息", "数字化", "智能", "BIM", "系统", "平台", "软件"], "digitalization"),
    # 通用管理（兜底）
    (["管理", "制度", "体系", "机制", "组织", "流程"], "general_management"),
]


def _generate_diverse_l3_title(
    l2_title: str,
    idx: int,
    l1_title: str = "",
    used_titles: set[str] | None = None,
    l2_index: int = 0,
) -> str:
    """根据二级目录名称生成多样化的三级目录标题。

    V5.2 原则（不采用模板池兜底）：
    1. 三级标题一律「从所属二级目录语义派生」——L3 核心词取自 L2，
       后缀用专业执行维度（工艺/质量/安全/进度…），保证三级与二级主题对应
    2. 不使用 CHAPTER_TITLE_TEMPLATES / 场景模板池等静态模板
       —— 模板池空间小且与任意 L2 无关，是多章节重名、三级脱节二级的根源
    3. used_titles 跨 L2 全局去重：不同 L2 核心词不同 → 派生标题天然不重名

    Args:
        l2_title: 二级目录标题
        idx: 当前三级节点在同级中的序号
        l1_title: 一级目录标题（用于上下文兜底）
        used_titles: 已使用的标题集合（跨 L2 共享，用于去重）
        l2_index: 当前二级目录在同级中的序号（用于派生偏移）
    """
    from .outline_common import (
        _derive_child_title,
        _extract_title_core,
        _semantic_fallback_title,
    )

    used_titles = used_titles or set()

    # Step 1（主路径）：从 L2 语义派生（结合上一级目录对应性）
    parent = l2_title.strip() if l2_title and l2_title.strip() else ""
    if not parent and l1_title:
        parent = l1_title.strip()
    if not parent:
        parent = "工程实施"

    # 多次尝试取不同后缀组合，迫使兄弟 L3 多样化
    for offset in range(5):
        candidate = _derive_child_title(
            parent,
            idx + l2_index * 5 + offset * 3,
            used_titles,
            level=3,
            grandparent_title=l1_title,
        )
        if candidate not in used_titles:
            return candidate

    # Step 2（兜底）：语义化兜底，杜绝「专项N」/「（第N项）」占位
    # 2026-10-10 增强：传入 L1 标题作为上下文，L2 为通用型时仍可继承所在
    # 一级章的专业领域（L1=质量管理措施 → quality 域），产出具体三级标题。
    return _semantic_fallback_title(
        l2_title or l1_title or "", idx, used_titles, context_title=l1_title
    )


# ========== L2/L3 差异化后处理 ==========

# 三级标题差异化修饰词池（确保与 L2 核心词区分）
_L3_DIFF_SPECIFIC_PREFIXES: list[str] = [
    # 部位类前缀（使 L3 指向具体工程部位，而非抽象概念）
    "基坑",
    "主体",
    "屋面",
    "外墙",
    "内墙",
    "地面",
    "顶棚",
    "楼梯间",
    "地下室",
    "首层",
    "标准层",
    "顶层",
    "檐口",
    "女儿墙",
    "管道井",
    "机电",
    "幕墙",
    "装饰",
    "防水",
    "保温",
    "砌筑",
    "抹灰",
    # 工艺类前缀（使 L3 指向具体施工动作）
    "隐蔽",
    "浇筑",
    "绑扎",
    "支设",
    "拆除",
    "铺贴",
    "喷涂",
    "涂刷",
    "安装",
    "调试",
    "检测",
    "试验",
    "试压",
    "清洁",
    "养护",
    # 管控类前缀（使 L3 指向具体管控动作）
    "进场",
    "验收",
    "交底",
    "巡检",
    "监测",
    "计量",
    "结算",
    "归档",
]

# 三级标题差异化后缀池（与前缀配合使用）
_L3_DIFF_SPECIFIC_SUFFIXES: list[str] = [
    "施工工艺",
    "安装工艺",
    "工艺流程",
    "操作要点",
    "验收标准",
    "质量控制",
    "安全操作",
    "技术要求",
    "检验方法",
    "检测标准",
    "专项方案",
    "实施细则",
    "作业指导",
    "技术交底",
    "操作规范",
    "管控要点",
    "控制指标",
    "评价标准",
    "实施方法",
    "执行要点",
]


def _ensure_l2_l3_differentiation(level1: list[dict]) -> int:
    """确保三级标题与父级二级标题不存在【完全重复/近似复述】。

    核心逻辑（V5.2 调整）：
    1. 旧逻辑以相似度 0.45 为阈值，会把「承接 L2 语义的合格三级标题」
       （如 L2="模板工程施工方案" → L3="模板工程支撑体系施工要点"）误判为
       "与二级类同"，然后替换成与 L2 无关的场景模板标题——这正是
       "三级标题不关联二级内容"的根源之一。
    2. 现在只拦截【三级复述二级】的情形（L3 完整包含 L2 或与之几乎相同，
       相似度 >= 0.85），替换时仍从 L2 语义派生，保证三级承接二级。
    3. 使用全局 used_titles 集合（跨所有 L1+L2 章节），防止替换后产生新的重复

    BUG 修复：添加完整的类型检查，防止非 dict 元素导致 AttributeError。

    Returns:
        int: 被修正的 L3 标题数量
    """
    fixed_count = 0

    # 全局 used_titles 集合，跨所有章节去重
    global_used: set[str] = set()

    # 先收集所有已有 L3 标题
    for l1 in level1:
        # BUG 修复: 检查 l1 是否为 dict
        if not isinstance(l1, dict):
            continue
        l1_children = l1.get("children", [])
        if not isinstance(l1_children, list):
            continue
        for l2 in l1_children:
            # BUG 修复: 检查 l2 是否为 dict
            if not isinstance(l2, dict):
                continue
            l2_children = l2.get("children", [])
            if not isinstance(l2_children, list):
                continue
            for l3 in l2_children:
                # BUG 修复: 检查 l3 是否为 dict
                if not isinstance(l3, dict):
                    continue
                t = l3.get("title", "").strip()
                if t:
                    global_used.add(t)

    for l1 in level1:
        # BUG 修复: 检查 l1 是否为 dict
        if not isinstance(l1, dict):
            continue
        l1_title = l1.get("title", "")
        l1_children = l1.get("children", [])
        if not isinstance(l1_children, list):
            continue

        for l2_idx, l2 in enumerate(l1_children):
            # BUG 修复: 检查 l2 是否为 dict
            if not isinstance(l2, dict):
                continue
            l2_title = l2.get("title", "").strip()
            l2_core = _extract_l2_core_for_diff(l2_title)
            l2_children = l2.get("children", [])
            if not isinstance(l2_children, list):
                continue

            for l3_idx, l3 in enumerate(l2_children):
                # BUG 修复: 检查 l3 是否为 dict
                if not isinstance(l3, dict):
                    continue
                l3_title = l3.get("title", "").strip()
                if not l3_title or not l2_title:
                    continue

                # 仅拦截「三级复述二级」：完整包含 L2 标题，或与 L2 几乎相同
                is_repetition = (
                    l3_title == l2_title
                    or (len(l2_title) >= 2 and l2_title in l3_title)
                    or (len(l3_title) >= 2 and l3_title in l2_title)
                )
                if not is_repetition:
                    similarity = _compute_l2_l3_similarity(l2_core, l3_title, l2_full=l2_title)
                    if similarity < 0.85:
                        # 三级是二级的合理承接展开，差异度足够，无需修改
                        continue

                # 需要替换：从 L2 语义派生差异化 L3（注入 L2 领域关键词）
                new_title = _generate_differentiated_l3_title(
                    l2_title, l2_core, l3_idx, l2_idx, l1_title, global_used
                )
                if new_title and new_title != l3_title:
                    old_title = l3["title"]
                    l3["title"] = new_title
                    global_used.discard(old_title)
                    global_used.add(new_title)
                    fixed_count += 1
                    logger.info(
                        "L2/L3 差异化修正: '%s' → '%s' (相似度=%.2f)",
                        l3_title,
                        new_title,
                        similarity if not is_repetition else 1.0,
                    )

    return fixed_count


def _extract_l2_core_for_diff(l2_title: str) -> str:
    """提取 L2 标题的核心内容（用于与 L3 做相似度比对）。

    移除通用后缀词，保留语义核心。
    """
    # 移除末尾的通用修饰词
    common_suffixes = sorted(
        [
            "体系",
            "机制",
            "系统",
            "模式",
            "架构",
            "组织",
            "制度",
            "措施",
            "方案",
            "方法",
            "流程",
            "标准",
            "规范",
            "要求",
            "控制",
            "管理",
            "保障",
            "检查",
            "监督",
            "评估",
            "考核",
            "规划",
            "计划",
            "部署",
            "安排",
            "设计",
            "研究",
            "分析",
            "建设",
            "实施",
            "推进",
            "深化",
            "加强",
            "强化",
            "提升",
            "优化",
            "完善",
            "健全",
            "建立",
            "落实",
            "执行",
        ],
        key=len,
        reverse=True,
    )

    core = l2_title
    for sfx in common_suffixes:
        if core.endswith(sfx) and len(core) - len(sfx) >= 2:
            core = core[: -len(sfx)]
            break

    # 若核心太短，使用原标题
    if len(core) < 2:
        return l2_title

    return core


def _compute_l2_l3_similarity(l2_core: str, l3_title: str, l2_full: str = "") -> float:
    """计算 L2 核心与 L3 标题的相似度（0.0 ~ 1.0）。

    综合考量：字符级 Jaccard + 子串包含度 + 前缀匹配度 + 公共双字词 + 首二字连续匹配。
    同时比对 L2 完整标题（若提供），捕获核心提取后丢失的相似度。
    """
    if not l2_core or not l3_title:
        return 0.0

    # 分别对 core 和 full 计算相似度，取最大值
    def _sim(l2_src: str) -> float:
        if not l2_src:
            return 0.0

        # 1. 字符集合 Jaccard
        l3_set = set(l3_title)
        l2_set = set(l2_src)
        intersection = l3_set & l2_set
        union = l3_set | l2_set
        jaccard = len(intersection) / len(union) if union else 0.0

        # 2. 子串包含度：L2 有多少字符出现在 L3 中
        chars_in_l3 = sum(1 for c in l2_src if c in l3_title)
        containment = chars_in_l3 / len(l2_src) if l2_src else 0.0

        # 前缀匹配度（强信号）
        prefix_match = 0.0
        if l3_title.startswith(l2_src):
            prefix_match = 1.0
        elif len(l2_src) >= 2 and l3_title.startswith(l2_src[:2]):
            prefix_match = 0.95  # L3 以 L2 核心的前 2 个字开头
        elif len(l2_src) >= 3 and l3_title.startswith(l2_src[:3]):
            prefix_match = 0.9

        # 4. 公共双字词和三字词
        bigram_overlap = 0.0
        l2_bigrams = {l2_src[i : i + 2] for i in range(len(l2_src) - 1)}
        l3_bigrams = {l3_title[i : i + 2] for i in range(len(l3_title) - 1)}
        common_bigrams = l2_bigrams & l3_bigrams
        if common_bigrams:
            bigram_overlap = len(common_bigrams) / max(len(l2_bigrams), 1)

        trigram_overlap = 0.0
        if len(l2_src) >= 3:
            l2_trigrams = {l2_src[i : i + 3] for i in range(len(l2_src) - 2)}
            l3_trigrams = {l3_title[i : i + 3] for i in range(len(l3_title) - 2)}
            common_trigrams = l2_trigrams & l3_trigrams
            if common_trigrams:
                trigram_overlap = len(common_trigrams) / max(len(l2_trigrams), 1)

        # 5. 首二字连续匹配
        first_two_match = 0.0
        if len(l2_src) >= 2 and l2_src[:2] in l3_title:
            first_two_match = 0.95 if l3_title.startswith(l2_src[:2]) else 0.5

        # 6. 最长公共子串（连续匹配，强信号）
        # BUG-FIX-18: 使用动态规划 O(n²) 替代暴力 O(n³) 算法
        # 原实现三重嵌套（start + end + substr in l3_title），500节点×30字符 = 1350万次操作
        m, n_len = len(l2_src), len(l3_title)
        if m > 0 and n_len > 0:
            # 使用一维DP数组滚动优化空间
            dp = [0] * (n_len + 1)
            lcs_len = 0
            for i in range(1, m + 1):
                prev = 0
                for j in range(1, n_len + 1):
                    temp = dp[j]
                    if l2_src[i - 1] == l3_title[j - 1]:
                        dp[j] = prev + 1
                        if dp[j] > lcs_len:
                            lcs_len = dp[j]
                    else:
                        dp[j] = 0
                    prev = temp
        else:
            lcs_len = 0
        # 3+ 连续字符匹配视为强信号，4+ 视为极强信号
        if lcs_len >= 4:
            lcs_score = 0.95
        elif lcs_len >= 3:
            lcs_score = 0.75
        else:
            lcs_score = 0.0

        # 加权求和
        return (
            0.05 * jaccard
            + 0.10 * containment
            + 0.25 * prefix_match
            + 0.08 * bigram_overlap
            + 0.08 * trigram_overlap
            + 0.15 * first_two_match
            + 0.29 * lcs_score  # 最长公共子串（核心信号）
        )

    # 对 core 和 full 分别计算，取最大
    core_sim = _sim(l2_core)
    if l2_full and l2_full != l2_core:
        full_sim = _sim(l2_full)
        return max(core_sim, full_sim)
    return core_sim


def _generate_differentiated_l3_title(
    l2_title: str,
    l2_core: str,
    l3_idx: int,
    l2_idx: int,
    l1_title: str,
    used_titles: set[str],
) -> str:
    """为与 L2 类同的 L3 标题生成差异化替换标题。

    V5.2 原则：不采用模板池兜底。替换标题一律「从所属 L2 语义派生」，
    通过不同的专业执行维度后缀（工艺/质量/验收/安全/进度…）实现与
    原 L3 的差异，同时保持与 L2 的承接关系——不能像旧逻辑那样退化为
    「与 L2 完全无关的场景模板」。

    Args:
        l2_title: 父级二级标题
        l2_core: 二级标题核心词
        l3_idx / l2_idx: 序号（派生偏移）
        l1_title: 一级标题（上下文兜底）
        used_titles: 全局已用标题（跨 L2 去重）
    """
    from .outline_common import _derive_child_title

    parent = l2_title.strip() if l2_title and l2_title.strip() else (l1_title or "工程实施")

    # 尝试不同的后缀维度组合，直到找到未使用且与 L2 全文不相同的标题
    # 2026-10-12 加固：追加 grandparent_title=l1_title —— 当 L2 是通用型
    # 标题（"总体方案与实施策划" dom=generic）时，L3 兜底能继承 L1 的
    # 专业域（quality/safety/schedule），避免全部落到 work 混合域。
    for offset in range(8):
        candidate = _derive_child_title(
            parent, l3_idx + l2_idx * 5 + offset * 3, used_titles, level=3,
            grandparent_title=l1_title,
        )
        if not candidate or candidate in used_titles:
            continue
        if candidate == l2_title:
            continue  # 完全相同则跳过，继续取其他维度后缀
        return candidate

    # 2026-10-11 修复：旧兜底 `f"{core}专项{n}"` 产出「总体实施策划专项6」这类标题，
    # 命中 `_PLACEHOLDER_ANYWHERE_RE`（专项 + 数字）被判为脏标题，是用户报告的
    # 「专项6」类占位名称的直接来源之一；`f"{core}实施要点"` 虽不直接判脏，
    # 但「实施要点」属纯泛化词，仍是无信息量标题。改用 outline_common 的语义化
    # 兜底：从所属 L2 语义派生、保证非脏且唯一，与 V5.2「不采用模板池兜底」一致。
    # 注意 `_semantic_fallback_title` 会把结果登记进 used，故无需再判 in used。
    from .outline_common import _semantic_fallback_title
    for i in range(1, 12):
        candidate = _semantic_fallback_title(
            parent, l3_idx + i, used_titles, context_title=l1_title or ""
        )
        if candidate and candidate != l2_title:
            return candidate
    # BUG-FIX 2026-10-13（G6）：旧兜底 `return f"{core}专项之一"` 有两个缺陷——
    #   1) `core` 是**未定义局部变量**（形参名是 l2_core），一旦触发即 NameError，
    #      异常被上层 `_ensure_l2_l3_differentiation` 的 try/except 吞掉，
    #      修正静默失败；
    #   2) 「专项之一」虽用中文序数规避了「专项+数字」的脏判定，但本质仍是
    #      占位式命名，与用户报告的「专项6」同族。
    # 改为与 `_semantic_fallback_title` 相同的「永不失败、永不脏」保证：
    # 换序号多试几次，最后用 `_disambiguate_title` 收口（中文序数消歧、
    # 无括号裸编号，必然收敛且非脏）。
    from .outline_common import _disambiguate_title
    for i in range(12, 40):
        candidate = _semantic_fallback_title(
            parent, l3_idx + i, used_titles, context_title=l1_title or ""
        )
        if candidate and candidate != l2_title:
            return candidate
    return _disambiguate_title(l2_title, used_titles, l1_title or "")


async def _generate_level3_v51(
    provider: Any,
    level1: list[dict],
    summary: str,
    sp_text: str,
    phase_callback: Callable | None = None,
    l2_done_queue: asyncio.Queue | None = None,
    project_info: dict | None = None,
) -> list[dict]:
    """L3：补全三级目录（全量串行后处理，**不是**流水线）。

    ⚠️ 说明（2026-09-14 排查，2026-09-15 修正描述）：本函数**完全不调用 AI**。
    `provider` / `summary` / `sp_text` / `l2_done_queue` 四个形参目前都只为接口
    兼容保留，函数体内一概未使用。三级标题非空则原样保留，为空才由
    `_generate_diverse_l3_title()` 按规则兜底生成。因此 L3 阶段**不存在可注入
    全局事实的提示词入口**——要注入就必须把 L3 改成 AI 驱动，代价是当前
    "零耗时、零成本"的 L3 会变成 N 次 AI 调用。事实约束目前覆盖 L1
    （_generate_level1_v51）与 L2（_fill_level2_with_ai），如需 L3 也受约束，
    应改走"事实校验/后处理"而非"提示词注入"。

    原 docstring 自称"流水线模式：L2 完成一个章节，立即开始该章节的 L3 生成"，
    与实现不符：这里是在 L2 全部结束后对整棵目录做一次串行遍历，`l2_done_queue`
    从未被消费。若确实需要流水线语义，应重写本函数而非只改描述。

    三级默认标题根据二级目录名称多样化生成。

    BUG 修复：添加完整的类型检查，防止非 dict 元素导致 AttributeError。
    """
    if phase_callback:
        await phase_callback("V5.1三级生成", "检查并补全三级目录（串行后处理）")

    # 收集所有 L3 节点做最终检查并多样化填充
    l3_total = 0
    l3_filled = 0
    l3_empty = 0

    # BUG 修复: 统计 L3 节点总数时添加类型检查
    total_l3_nodes = 0
    for l1 in level1:
        if not isinstance(l1, dict):
            continue
        l1_children = l1.get("children", [])
        if not isinstance(l1_children, list):
            continue
        for l2 in l1_children:
            if not isinstance(l2, dict):
                continue
            l2_children = l2.get("children", [])
            if isinstance(l2_children, list):
                total_l3_nodes += len(l2_children)
    logger.info("V5.1 L3 开始生成: 预计三级节点=%d", total_l3_nodes)

    # 全局已用标题集合：跨所有一级+二级章节去重
    used_titles_global: set[str] = set()

    for l1 in level1:
        # BUG 修复: 检查 l1 是否为 dict
        if not isinstance(l1, dict):
            continue
        l1_title = l1.get("title", "")
        l1_children = l1.get("children", [])
        # BUG 修复: 检查 children 是否为 list
        if not isinstance(l1_children, list):
            continue

        for l2_idx, l2 in enumerate(l1_children):
            # BUG 修复: 检查 l2 是否为 dict
            if not isinstance(l2, dict):
                continue
            l2_title = l2.get("title", "")
            l2_children = l2.get("children", [])
            # BUG 修复: 检查 l2 的 children 是否为 list
            if not isinstance(l2_children, list):
                continue

            for l3_idx, l3 in enumerate(l2_children):
                # BUG 修复: 检查 l3 是否为 dict
                if not isinstance(l3, dict):
                    continue
                l3_total += 1
                title = l3.get("title", "").strip()
                if title:
                    l3_filled += 1
                    used_titles_global.add(title)
                else:
                    l3_empty += 1
                    # 根据二级目录名称生成多样化标题（使用全局已用标题集合跨章节去重）
                    new_title = _generate_diverse_l3_title(
                        l2_title,
                        l3_idx,
                        l1_title,
                        used_titles=used_titles_global,
                        l2_index=l2_idx,
                    )
                    l3["title"] = new_title
                    l3["level"] = 3
                    used_titles_global.add(new_title)
                    if "children" not in l3:
                        l3["children"] = []

    logger.info(
        "V5.1 L3 检查完成: 共 %d 个三级节点，已填充 %d，默认填充 %d", l3_total, l3_filled, l3_empty
    )

    # 2026-10-11 修复（O-8）：旧实现把「差异化修正」「占位清洗」的修正数累加进
    # l3_filled，导致日志输出「已填充 X > Y（共 Y 个）」的语义漂移。
    # 现分离 l3_repaired 计数器，l3_filled 仅记录原始填充数。
    l3_repaired = 0

    # L2/L3 差异化后处理：检测并修正与父级 L2 标题类同的 L3 标题
    diff_fixed = _ensure_l2_l3_differentiation(level1)
    if diff_fixed > 0:
        logger.info("V5.1 L2/L3 差异化修正: 修正 %d 个三级标题", diff_fixed)
        l3_repaired += diff_fixed

    # 2026-10-11 修复（O-10）：旧实现在此处调用 _cleanup_placeholder_titles + _uniquify_outline_titles，
    # 但 generate_outline_v51 在 _ensure_min_children / _tech_enhance_titles 之后又会调用一次。
    # 由于 _ensure_min_children 和 _tech_enhance_titles 可能引入新的占位标题或修改标题，
    # 外部的清洗+去重调用是必需的，而此处的内部调用是冗余的（会被外部调用覆盖）。
    # 现移除内部调用，统一由 generate_outline_v51 在全部后处理完成后执行一次清洗+去重。
    # 进度回调中的 l3_repaired 仅统计 _ensure_l2_l3_differentiation 的修正数（占位清洗和去重
    # 的结果由外部调用处理，不在此处报告）。

    # 进度回调
    if phase_callback:
        await phase_callback(
            "V5.1三级生成",
            f"三级目录生成完成 {l3_filled}/{l3_total}（差异化修正 {diff_fixed}，占位清洗和去重由后续统一处理）"
        )

    return level1


# ========== 自检 ==========


async def _self_check_v51(
    provider: Any,
    level1: list[dict],
    _t_start: float,
    phase_callback: Callable | None = None,
    v51_state: Any = None,
    project_info: dict | None = None,
    scoring_points: str = "",
    expected_leaf_count: int | None = None,
    word_budget_text: str = "",
    verdict_out: dict | None = None,
) -> list[dict]:
    """V5.1 自检：本地修复 + 目录门禁（确定性事实 + AI 语义审）。

    ENHANCE 2026-09-14：自检阶段接入目录门禁。门禁只产出结论不做阻断，
    结论写入 `v51_state.gate_report` 并通过 phase_callback 上报给前端。

    ENHANCE 2026-10-09（文档五·审核回灌）：新增可选 `verdict_out`——调用方
    传入一个 dict 即可拿到裁决副本，用于「审核不通过 → 带建议重生成」的判定。
    不传时行为与改造前完全一致（v51_state 可能为 None，裁决仍需可取）。
    """
    if phase_callback:
        await phase_callback("V5.1自检", "AI自检目录质量")

    # 1. 快速本地检查（不调用 AI）
    # BUG-FIX-43 修复：原实现 check→fix 只执行一次，修复后从不重新校验。
    # 问题链：3+ 个同标题时，_auto_fix_issues 只给第2个加"（二）"后缀，
    # 第3个仍与第1个重复；且多个不同重复对都加"（二）"会创建新重复。
    # 现改为 check→fix→re-check 循环，最多 3 轮，仍残留则记 error 日志。
    max_fix_rounds = 3
    total_fixed = 0
    for round_idx in range(max_fix_rounds):
        issues = _quick_local_check(level1)
        if not issues:
            break
        logger.warning("V5.1 本地检查第 %d 轮发现 %d 个问题: %s", round_idx + 1, len(issues), issues[:3])
        level1 = _auto_fix_issues(level1, issues)
        total_fixed += len(issues)
    else:
        # 循环耗尽仍有有问题——记录未修复问题
        remaining = _quick_local_check(level1)
        if remaining:
            logger.error(
                "V5.1 本地检查经 %d 轮修复后仍残留 %d 个问题: %s",
                max_fix_rounds, len(remaining), remaining[:5],
            )
    if phase_callback:
        await phase_callback(
            "V5.1自检",
            f"本地检查完成（{'发现并修复' if total_fixed else '无'} {total_fixed} 项问题）",
        )

    # 2. 目录门禁：宿主算事实 → 机械可修的先修 → AI 语义审 → 合并裁决
    auto_fixed: list[str] = []
    scoring_titles = outline_gate.parse_scoring_item_titles(scoring_points)

    # 叶子数目标：显式传入优先；否则从招标要求/评分要点里解析字数预算再推导。
    # ENHANCE 2026-09-14：此前 `expected_leaf_count` 声明了却无任何调用方赋值，
    # 导致 leaf-count 检查实际从不生效（`derive_target_leaf_count` 只被测试调用过）。
    if expected_leaf_count is None and word_budget_text:
        budget = outline_gate.parse_word_budget(word_budget_text)
        expected_leaf_count = outline_gate.derive_target_leaf_count(**budget)
        if expected_leaf_count:
            logger.info(
                "V5.1 门禁：由字数预算 %s 推导目标叶子数 %d", budget, expected_leaf_count
            )

    def _facts_and_issues() -> tuple[dict, list[dict]]:
        _facts = outline_gate.compute_outline_facts(
            level1,
            scoring_titles=scoring_titles or None,
            expected_leaf_count=expected_leaf_count,
        )
        return _facts, outline_gate.collect_deterministic_issues(_facts)

    facts, det_issues = _facts_and_issues()

    # 2.1 编号类问题可机械修复：按位置重排后重新取事实。
    # 实测 `_ensure_min_children` 补全的新节点不带 id（且它在 id 分配之后运行），
    # 而最终审核会把这份目录推给前端编辑——缺 id 会让前端按 id 编辑时改错节点。
    if any(i.get("category") == "invalid-id" for i in det_issues):
        level1, _id_changed = outline_gate.renumber_outline_ids(level1)
        auto_fixed.append(f"编号重排（{_id_changed} 个节点）")
        logger.warning("V5.1 门禁：编号缺失或非法，已按位置重排 %d 个节点", _id_changed)
        facts, det_issues = _facts_and_issues()

    # 2.2 工程逻辑顺序可机械修复（ENHANCE 2026-09-15）：与编号重排同理，
    # 属宿主能确定性判定的机械修复。改完重新取事实，并记入 auto_fixed ——
    # 不做静默改动，用户能在进度文案里看到「已自动修复：工程逻辑顺序重排」。
    # 招标文件若指定了目录顺序，把 `outline_gate.APPLY_ZONE_REORDER` 置 False，
    # 即可退回"只提示不改"（门禁仍会在裁决里报 zone-order）。
    if outline_gate.APPLY_ZONE_REORDER and facts.get("zone_order_violations"):
        level1, _zone_moves = outline_gate.reorder_by_zone(level1)
        if _zone_moves:
            auto_fixed.append(f"工程逻辑顺序重排（{len(_zone_moves)} 个章节）")
            logger.warning(
                "V5.1 门禁：一级章节工程逻辑顺序倒置，已机械重排 %d 个章节：%s",
                len(_zone_moves),
                "; ".join(f"「{m['title']}」{m['from']}→{m['to']}" for m in _zone_moves[:5]),
            )
            facts, det_issues = _facts_and_issues()

    # 2.3 AI 语义审（失败只降级，不阻断）
    ai_issues: list[dict] | None = None
    ai_error: str | None = None
    ai_skipped = False
    if outline_gate.should_run_ai_review(facts, det_issues):
        if phase_callback:
            await phase_callback("V5.1自检", f"AI 语义审查（{facts['node_count']} 节点）...")
        ai_issues, ai_error = await _ai_deep_check(
            provider, level1, project_info, facts=facts, deterministic_issues=det_issues
        )
    else:
        ai_skipped = True
        logger.info(
            "V5.1 门禁：无确定性问题且节点数 %d 未超阈值 %d，跳过 AI 语义审查",
            facts["node_count"],
            outline_gate.OUTLINE_GATE_AI_NODE_THRESHOLD,
        )

    verdict = outline_gate.build_verdict(facts, det_issues, ai_issues, ai_error, ai_skipped)
    verdict["auto_fixed"] = auto_fixed
    if verdict_out is not None:
        try:
            verdict_out.update(verdict)
        except Exception:  # 裁决回写失败不能影响主流程
            logger.debug("V5.1 门禁裁决回写 verdict_out 失败", exc_info=True)
    logger.info(
        "V5.1 门禁裁决: status=%s, 问题=%d（error %d, 需用户确认 %d）%s%s",
        verdict["status"],
        verdict["issue_count"],
        verdict["error_count"],
        verdict["needs_user_confirmation"],
        f", 摘要={verdict['summary']}" if verdict["issue_count"] else "",
        f", AI审查异常={ai_error}" if ai_error else "",
    )
    if v51_state is not None:
        try:
            v51_state.gate_report = verdict
        except Exception:  # 状态对象可能是只读替身，门禁结论不能因此丢失
            logger.debug("V5.1 门禁结论写入 v51_state 失败", exc_info=True)

    if phase_callback:
        if verdict["issue_count"]:
            _tail = "（已自动修复：" + "、".join(auto_fixed) + "）" if auto_fixed else ""
            await phase_callback(
                "V5.1自检",
                f"门禁检查完成：{verdict['issue_count']} 项待关注"
                f"（{verdict['summary']}）{_tail}",
            )
        else:
            await phase_callback("V5.1自检", "门禁检查完成：未发现问题")

    # 保存到状态
    if v51_state:
        v51_state.edited_outline = level1

    return level1


def _quick_local_check(level1: list[dict]) -> list[str]:
    """快速本地检查（不调用 AI）。

    BUG 修复：添加完整的类型检查，防止非 dict 元素导致 AttributeError。
    """
    issues: list[str] = []

    # 检查标题唯一性（同级作用域）
    # 2026-10-11 修正：旧实现用全局 seen 集合，会把不同章节下**合法**的同名
    # 条目判为「重复标题」，随后 `_auto_fix_issues` 给它追加「（其N）」式后缀 ——
    # 这正是目录里出现「砌体成品保护（其2）」「机电管线材料管理实施细则（其2）」
    # 的来源。标书目录中同名条目分属不同章节是正常形态，只应报告**同级**重名。
    def _report(
        group: dict[str, str], title: str, path: str, level: int = 1
    ) -> None:
        t = title.strip()
        if not t:
            issues.append(f"空标题: {path}")
            return
        # BUG-FIX 2026-10-15（D8 · 脏标题漏检）：旧实现只查空标题与同级重名。
        # 「总体实施策划（2）」这类占位/泛化 L3 标题既非空、又常为同级唯一，
        # 于是 `_auto_fix_issues` 永远收不到它的修复指令，脏标题原样落库。
        # 现对三级及以下用 `_is_dirty_l3_title_deep` 复检，单独报「占位标题」。
        if level >= 3 and _is_dirty_l3_title_deep(t):
            issues.append(f"占位标题: {path}")
        if t in group:
            issues.append(f"重复标题 '{t}': {group[t]} 和 {path}")
        group[t] = path

    l1_seen: dict[str, str] = {}
    for l1 in level1:
        if not isinstance(l1, dict):
            continue
        l1_id = l1.get("id", "")
        l1_children = l1.get("children", [])
        _report(l1_seen, l1.get("title", ""), l1_id)
        if not isinstance(l1_children, list):
            continue
        l2_seen: dict[str, str] = {}
        for l2 in l1_children:
            if not isinstance(l2, dict):
                continue
            l2_id = l2.get("id", "")
            l2_children = l2.get("children", [])
            _report(l2_seen, l2.get("title", ""), f"{l1_id}/{l2_id}")
            if not isinstance(l2_children, list):
                continue
            l3_seen: dict[str, str] = {}
            for l3 in l2_children:
                if not isinstance(l3, dict):
                    continue
                _report(
                    l3_seen,
                    l3.get("title", ""),
                    f"{l1_id}/{l2_id}/{l3.get('id', '')}",
                    level=3,
                )

    # 检查层级结构
    for l1 in level1:
        if not isinstance(l1, dict):
            continue
        l1_title = l1.get("title", "")
        l1_children = l1.get("children", [])
        if not isinstance(l1_children, list) or len(l1_children) == 0:
            issues.append(f"一级目录 '{l1_title}' 无二级子目录")
            continue
        for l2 in l1_children:
            if not isinstance(l2, dict):
                continue
            l2_title = l2.get("title", "")
            l2_children = l2.get("children", [])
            if not isinstance(l2_children, list) or len(l2_children) == 0:
                issues.append(f"二级目录 '{l1_title}/{l2_title}' 无三级子目录")

    return issues


def _auto_fix_issues(level1: list[dict], issues: list[str]) -> list[dict]:
    """自动修复本地检查发现的问题。

    BUG 修复：修正 issue 消息解析正则，确保正确提取路径和标题。
    新增：修复重复标题问题（重新命名重复标题）。
    BUG 修复：路径组件可能是点号 ID（如 "2.1"），需正确解析。
    BUG-FIX-43 修复：原始终用"（二）"后缀，3+个同标题重复时第2、3个都
    被改成"xxx（二）"产生新重复。改为递增后缀，跳过已存在标题。
    """

    def _path_to_index(path_part: str) -> int:
        """将路径组件转换为 0-based 索引。

        支持：
        - 纯整数："2" → 1（L1 的 0-based 索引）
        - 点号 ID："2.1" → 取最后一段 "1" → 0（L2 在父节点中的索引）
        - 多级 ID："2.1.3" → 取最后一段 "3" → 2（L3 在父节点中的索引）
        """
        path_part = path_part.strip()
        if "." in path_part:
            path_part = path_part.split(".")[-1]
        return int(path_part) - 1

    def _collect_all_titles(level1: list[dict]) -> set[str]:
        """收集目录树中所有现有标题的归一化 core，用于检测后缀冲突。"""
        titles: set[str] = set()
        for l1 in level1:
            if not isinstance(l1, dict):
                continue
            titles.add(_title_core(l1.get("title", "")))
            for l2 in l1.get("children", []) or []:
                if not isinstance(l2, dict):
                    continue
                titles.add(_title_core(l2.get("title", "")))
                for l3 in l2.get("children", []) or []:
                    if not isinstance(l3, dict):
                        continue
                    titles.add(_title_core(l3.get("title", "")))
        return titles

    def _next_unique_title(base: str, existing: set[str]) -> str:
        """生成下一个不冲突的标题。

        2026-10-11 修正：`_disambiguate_title` 已按「专业维度词 → 父级差异词
        → 中文序数（之N）」三级递进保证结果不冲突，**不再回落裸编号**。
        旧的 `（二）/（2）` 分支会产出与「（其2）」同类的冗余后缀污染目录，
        故整体删除，仅保留一个理论上不可达的防御性兜底。
        """
        candidate = _disambiguate_title(base, existing)
        if candidate != base:
            existing.add(_title_core(candidate))
            return candidate
        for i in range(2, 200):
            candidate = f"{base}之{_cn_ordinal(i)}"
            if _title_core(candidate) not in existing:
                existing.add(_title_core(candidate))
                return candidate
        return base  # 兜底

    all_existing_titles = _collect_all_titles(level1)

    for issue in issues:
        # 修复重复标题 (格式: "重复标题 'xxx': path1 和 path2")
        if "重复标题" in issue:
            # BUG-OUTLINE-07 修复: 解析重复标题问题，给重复标题添加后缀区分
            title_match = re.search(r"重复标题\s+'(.+?)':\s*(.+?)\s*和\s*(.+)", issue)
            if title_match:
                dup_title = title_match.group(1)
                # path1 是第一个出现的（seen），path2 是重复的——只改 path2
                # BUG-FIX-87: 原 L3744 有死代码 title_match.group(2).strip() 未赋值，清理之
                path2 = title_match.group(3).strip()
                # 给第二个出现的标题添加递增序号后缀
                try:
                    parts2 = path2.split("/")
                    if len(parts2) == 1:
                        idx = _path_to_index(parts2[0])
                        if 0 <= idx < len(level1) and isinstance(level1[idx], dict):
                            orig = level1[idx].get("title", dup_title)
                            new_title = _next_unique_title(orig, all_existing_titles)
                            level1[idx]["title"] = new_title
                    elif len(parts2) == 2:
                        i = _path_to_index(parts2[0])
                        j = _path_to_index(parts2[1])
                        if 0 <= i < len(level1) and isinstance(level1[i], dict):
                            children = level1[i].get("children", [])
                            if (
                                isinstance(children, list)
                                and 0 <= j < len(children)
                                and isinstance(children[j], dict)
                            ):
                                orig = children[j].get("title", dup_title)
                                new_title = _next_unique_title(orig, all_existing_titles)
                                children[j]["title"] = new_title
                    elif len(parts2) == 3:
                        i = _path_to_index(parts2[0])
                        j = _path_to_index(parts2[1])
                        k = _path_to_index(parts2[2])
                        if 0 <= i < len(level1) and isinstance(level1[i], dict):
                            children = level1[i].get("children", [])
                            if (
                                isinstance(children, list)
                                and 0 <= j < len(children)
                                and isinstance(children[j], dict)
                            ):
                                l2_children = children[j].get("children", [])
                                if (
                                    isinstance(l2_children, list)
                                    and 0 <= k < len(l2_children)
                                    and isinstance(l2_children[k], dict)
                                ):
                                    orig = l2_children[k].get("title", dup_title)
                                    new_title = _next_unique_title(orig, all_existing_titles)
                                    l2_children[k]["title"] = new_title
                except (ValueError, IndexError):
                    pass

        # 修复空标题 / 占位泛化标题
        # (格式: "空标题: 1/2/3" 或 "占位标题: 1/2/3" —— 后者由 2026-10-15 D8 新增)
        if "空标题" in issue or "占位标题" in issue:
            # BUG 修复: 正确匹配 "空标题: <path>" 格式
            path_match = re.search(r"(?:空标题|占位标题):\s*(.+)", issue)
            if path_match:
                path = path_match.group(1).strip()
                parts = path.split("/")
                try:
                    # BUG-FIX-12: 使用 _path_to_index 替代 int() 解析点号ID格式
                    # 原实现 int("1.1") 抛 ValueError，导致 L2/L3 空标题修复完全失效
                    # BUG-FIX 2026-10-12（P1）：三个分支原分别写死
                    # `章节{i+1}` / `子章节{j+1}` / `内容{k+1}` —— 零语义的纯序号
                    # 占位，与「专项6」同属用户投诉的占位泛化标题。统一改为按父级
                    # 语义派生（与同函数「无二级/无三级子目录」分支同一套规则）。
                    # 用 `_semantic_fallback_title`（工程实体 × 专业维度）而非
                    # `_derive_child_title`：L1 无父级可用，后者对 level=1 没有
                    # 对应的语义池分支。
                    from .outline_common import _semantic_fallback_title

                    def _sibling_titles(nodes: list) -> set[str]:
                        return {
                            str(_s.get("title") or "").strip()
                            for _s in (nodes or [])
                            if isinstance(_s, dict) and str(_s.get("title") or "").strip()
                        }

                    if len(parts) == 1:
                        idx = _path_to_index(parts[0])
                        if 0 <= idx < len(level1) and isinstance(level1[idx], dict):
                            level1[idx]["title"] = _semantic_fallback_title(
                                "总体策划", idx, _sibling_titles(level1)
                            )
                    elif len(parts) == 2:
                        i, j = _path_to_index(parts[0]), _path_to_index(parts[1])
                        if 0 <= i < len(level1) and isinstance(level1[i], dict):
                            children = level1[i].get("children", [])
                            if (
                                isinstance(children, list)
                                and 0 <= j < len(children)
                                and isinstance(children[j], dict)
                            ):
                                children[j]["title"] = _semantic_fallback_title(
                                    level1[i].get("title", ""),
                                    j,
                                    _sibling_titles(children),
                                )
                    elif len(parts) == 3:
                        i, j, k = (
                            _path_to_index(parts[0]),
                            _path_to_index(parts[1]),
                            _path_to_index(parts[2]),
                        )
                        if 0 <= i < len(level1) and isinstance(level1[i], dict):
                            children = level1[i].get("children", [])
                            if (
                                isinstance(children, list)
                                and 0 <= j < len(children)
                                and isinstance(children[j], dict)
                            ):
                                l2_children = children[j].get("children", [])
                                if (
                                    isinstance(l2_children, list)
                                    and 0 <= k < len(l2_children)
                                    and isinstance(l2_children[k], dict)
                                ):
                                    # BUG-FIX 2026-10-12（P1）：原写死
                                    # `f"内容{k + 1}"` —— 零语义的纯序号占位，
                                    # 与「专项6」同属用户投诉的占位泛化标题。
                                    # 改为按父级语义派生（与同函数「无二级/无三级
                                    # 子目录」分支已用的 `_derive_child_title`
                                    # 同一套规则），既无占位又不与兄弟撞名。
                                    from .outline_common import _derive_child_title

                                    _used_l3: set[str] = {
                                        str(_s.get("title") or "").strip()
                                        for _s in l2_children
                                        if isinstance(_s, dict)
                                    }
                                    _used_l3.discard("")
                                    l2_children[k]["title"] = _derive_child_title(
                                        children[j].get("title", ""),
                                        k,
                                        _used_l3,
                                        level=3,
                                        grandparent_title=level1[i].get("title", ""),
                                    )
                except (ValueError, IndexError):
                    pass

        # 修复空子目录 (格式: "一级目录 'xxx' 无二级子目录")
        if "无二级子目录" in issue:
            # BUG 修复: 正确匹配 "一级目录 'xxx' 无二级" 格式
            title_match = re.search(r"'(.+?)'\s*无二级", issue)
            if title_match:
                missing_title = title_match.group(1)
                from .outline_common import _derive_child_title

                for l1 in level1:
                    if not isinstance(l1, dict):
                        continue
                    if l1.get("title") == missing_title and not l1.get("children"):
                        l1_id = l1.get("id", "X")
                        used_l2: set[str] = set()
                        _l1_title = l1.get("title", "工程实施")
                        l1["children"] = []
                        for _i in range(3):
                            _l2_title = _derive_child_title(_l1_title, _i, used_l2, level=2)
                            used_l2.add(_l2_title)
                            used_l3: set[str] = set()
                            _l3_children = []
                            for _j in range(3):
                                # 2026-10-12 加固：追加 grandparent_title=_l1_title，
                                # 同 Fix A——空 L2 被派生成 generic 时 L3 能继承 L1 域
                                _l3_title = _derive_child_title(
                                    _l2_title, _j, used_l3, level=3, grandparent_title=_l1_title
                                )
                                used_l3.add(_l3_title)
                                _l3_children.append(
                                    {
                                        "title": _l3_title,
                                        "id": f"{l1_id}.{_i + 1}.{_j + 1}",
                                        "level": 3,
                                        "children": [],
                                    }
                                )
                            l1["children"].append(
                                {
                                    "title": _l2_title,
                                    "id": f"{l1_id}.{_i + 1}",
                                    "level": 2,
                                    "children": _l3_children,
                                }
                            )

        # 修复无三级子目录 (格式: "二级目录 'xxx/yyy' 无三级子目录")
        if "无三级子目录" in issue:
            # BUG 修复: 正确匹配 "二级目录 'xxx/yyy' 无三级" 格式
            path_match = re.search(r"'(.+?)'\s*无三级", issue)
            if path_match:
                missing_path = path_match.group(1)
                from .outline_common import _derive_child_title

                # 查找对应的二级节点
                for l1 in level1:
                    if not isinstance(l1, dict):
                        continue
                    l1_children = l1.get("children", [])
                    if not isinstance(l1_children, list):
                        continue
                    for l2 in l1_children:
                        if not isinstance(l2, dict):
                            continue
                        l2_children = l2.get("children", [])
                        l2_title = l2.get("title", "")
                        l1_title = l1.get("title", "")
                        # 检查路径是否匹配
                        if f"{l1_title}/{l2_title}" in missing_path and (
                            not isinstance(l2_children, list) or len(l2_children) == 0
                        ):
                            parent_id = l2.get("id", "X")
                            used_l3: set[str] = set()
                            l2["children"] = []
                            # 2026-10-12 加固：追加 grandparent_title=l1_title，
                            # 同 L4623——二级被派生成 generic 时 L3 能继承 L1 域
                            for _j in range(3):
                                _l3_title = _derive_child_title(
                                    l2_title or l1_title, _j, used_l3, level=3,
                                    grandparent_title=l1_title,
                                )
                                used_l3.add(_l3_title)
                                l2["children"].append(
                                    {
                                        "title": _l3_title,
                                        "id": f"{parent_id}.{_j + 1}",
                                        "level": 3,
                                        "children": [],
                                    }
                                )

    # 最终补全：确保每个节点有 children 字段
    for l1 in level1:
        if not isinstance(l1, dict):
            continue
        if "children" not in l1:
            l1["children"] = []
        l1_children = l1.get("children", [])
        if not isinstance(l1_children, list):
            l1["children"] = []
            l1_children = []
        for l2 in l1_children:
            if not isinstance(l2, dict):
                continue
            if "children" not in l2:
                l2["children"] = []
            if "level" not in l2:
                l2["level"] = 2
            l2_children = l2.get("children", [])
            if not isinstance(l2_children, list):
                l2["children"] = []
                l2_children = []
            for l3 in l2_children:
                if not isinstance(l3, dict):
                    continue
                if "children" not in l3:
                    l3["children"] = []
                if "level" not in l3:
                    l3["level"] = 3

    return level1


def _truncate_outline_for_check(level1: list[dict], max_chars: int = 40000) -> str:
    """按**章节边界**截断目录 JSON，保证输出始终是合法 JSON。

    BUG-FIX 2026-09-14（高）：原实现 `json.dumps(level1)[:40000]` 是字符级硬切，
    30 章 × 8×5 的目录实测约 90KB，截断后是不闭合的非法 JSON，
    AI 只能靠猜 → 自检结论基于残缺输入，且极易返回垃圾结构。
    修复：先整章丢弃（保留章节完整性），仍超限再逐章丢弃三级节点。
    """
    payload = json.dumps(level1, ensure_ascii=False)
    if len(payload) <= max_chars:
        return payload

    # 第一轮：整章保留（前缀式），丢弃尾部章节
    kept: list[dict] = []
    for ch in level1:
        trial = kept + [ch]
        if len(json.dumps(trial, ensure_ascii=False)) > max_chars and kept:
            break
        kept.append(ch)
    payload = json.dumps(kept, ensure_ascii=False)
    if len(payload) <= max_chars:
        logger.warning(
            "V5.1 自检：目录过大，仅送检前 %d/%d 个一级章节（结构化截断）",
            len(kept), len(level1),
        )
        return payload

    # 第二轮：逐章削减三级节点，保证 JSON 合法
    for ch in kept:
        for l2 in ch.get("children", []) or []:
            l3 = l2.get("children", []) or []
            while l3 and len(json.dumps(kept, ensure_ascii=False)) > max_chars:
                l3.pop()
    logger.warning("V5.1 自检：目录过大，已削减三级节点后送检")
    return json.dumps(kept, ensure_ascii=False)


async def _ai_deep_check(
    provider: Any,
    level1: list[dict],
    project_info: dict | None = None,
    *,
    facts: dict | None = None,
    deterministic_issues: list[dict] | None = None,
) -> tuple[list[dict] | None, str | None]:
    """AI 深度检查目录质量（目录门禁的语义审层）。

    ENHANCE 2026-09-14：改为门禁体系的一环。
      - 宿主先算好确定性事实（`outline_gate.compute_outline_facts`）并拼进 user prompt，
        明确要求 AI「直接采用宿主统计、不要重算」——这是参考实现的关键设计，
        既省 token 也避免 AI 把节点数/重复项数错。
      - AI 返回的 `issues` 经 `outline_gate.map_ai_issues` 映射到统一 schema，
        补上严重度与 `confirmation_required`，与确定性结论合并成同一份裁决。

    返回 `(已映射的 issues, 错误信息)`：
      - AI 正常但没发现问题 → `([], None)`
      - AI 调用/解析失败     → `(None, 错误信息)`，由调用方降级为"仅确定性结论"，
        **不阻断目录生成**（参考实现这里会直接让任务失败，本软件刻意不这么做）。

    注：原实现会接受 AI 返回的 `fixed_outline` 并整体替换目录，但提示词从未要求该字段，
    且静默接受 AI 重写的整棵目录有丢节点风险；现改为只收集问题、由前端/用户决定是否修改。
    """
    sys_prompt = get_prompt("OUTLINE_V51_SELFCHECK_SYSTEM")
    if facts is None:
        facts = outline_gate.compute_outline_facts(level1)
    user_prompt = outline_gate.build_gate_user_prompt(facts, _truncate_outline_for_check(level1))

    try:
        result = await _call_ai_v51(
            provider,
            sys_prompt,
            user_prompt,
            max_tokens=OUTLINE_MAX_TOKENS_SELFCHECK,
            max_retries=1,
            project_info=project_info,
        )
    except Exception as e:
        logger.warning("V5.1 AI 深度检查失败: %s", e)
        return None, str(e)

    if not isinstance(result, dict):
        logger.warning("V5.1 AI 深度检查返回结构无法识别: %s", type(result).__name__)
        return None, "AI 返回结构无法识别"

    issues = outline_gate.map_ai_issues(result.get("issues"))
    if issues:
        logger.warning(
            "V5.1 AI 语义审发现 %d 项问题: %s", len(issues), outline_gate.summarize_issues(issues)
        )
        for it in issues[:20]:
            logger.info(
                "V5.1 语义审问题: [%s] %s → %s",
                it.get("category"),
                str(it.get("problem"))[:120],
                str(it.get("repair"))[:120],
            )
    return issues, None


# ========== 人工审核 ==========


async def _wait_for_v51_review(
    outline_queue: Any,
    v51_state: Any,
    phase: str,
    outline: list[dict],
    auto_confirm: bool = False,
    timeout: int = 3600,
    event_emitter: Any = None,
) -> list[dict] | None:
    """等待人工审核（支持 auto_confirm 跳过）。

    Args:
        outline_queue: SSE 事件队列
        v51_state: V5.1 任务状态（含 resume_event）
        phase: 当前阶段（level1/level2/level3/final）
        outline: 待审核的目录数据
        auto_confirm: 是否自动确认（跳过人工审核）
        timeout: 审核超时秒数，默认 1 小时
        event_emitter: 可选的异步事件发送回调（如 sse_outline 的 _qput）。
            BUG-FIX 2026-10-04（遗留 L1）：此前裸用 outline_queue.put 绕过
            task_registry.broadcast_event —— SSE 断线重连的客户端靠广播重建
            会话，收不到 review_needed/review_auto_confirm，分步模式下步骤条
            永久卡在审核步。与 _save_and_finalize_outline 的 emit 同一套路；
            未传时退回直入队列，兼容旧调用与单测。
    """

    async def _emit_evt(evt: dict) -> None:
        if event_emitter is not None:
            await event_emitter(evt)
        else:
            await outline_queue.put(evt)

    if auto_confirm:
        # 自动确认：发送事件后直接返回当前目录
        logger.info("V5.1 %s 自动确认（auto_confirm=True）", phase)
        await _emit_evt(
            {
                "status": "review_auto_confirm",
                "phase": phase,
                "outline": outline,
                "message": f"{phase} 自动确认通过",
            }
        )
        return outline

    # BUG-FIX 2026-10-04（分步生成状态竞态）：此前先 put review_needed 再 clear
    # resume_event/edited_outline：若用户在「事件已发出、尚未 clear」的窗口内
    # 极快确认（v51-confirm 打到的 resume 先于 clear 执行），确认信号与编辑结果
    # 会被 clear 抹掉，工作流挂到超时——审核中断。改为先清残留再发事件，
    # clear 之后到达的 resume 不可能被覆盖。
    # BUG-FIX-76: 审核前清除上次残留的 set 状态，否则第二次 wait() 会立即返回
    v51_state.resume_event.clear()
    # BUG-FIX-EDITED-STALE: 审核前清空上一阶段的 edited_outline，
    # 防止用户在本阶段"只确认不编辑"时，旧阶段的编辑结果覆盖本阶段新生成的目录
    v51_state.edited_outline = None
    # 已取消则不再发出审核请求，直接中止
    if getattr(v51_state, "cancelled", False):
        return None

    # 发送审核事件
    await _emit_evt(
        {
            "status": "review_needed",
            "phase": phase,
            "outline": outline,
            "message": f"请审核 {phase} 目录，确认后继续生成",
        }
    )

    # 等待审核结果（使用 resume_event）
    try:
        review_result = await asyncio.wait_for(
            v51_state.resume_event.wait(),
            timeout=timeout,
        )
        # BUG-FIX-11: 检查 cancelled 标志，用户拒绝审核时应中止生成
        # 原实现未检查 cancelled，导致拒绝操作被当作通过处理
        if review_result and not getattr(v51_state, "cancelled", False):
            return v51_state.edited_outline or outline
        return None
    except TimeoutError:
        logger.warning("V5.1 %s 审核超时", phase)
        return None


# ========== 主入口 ==========


def _truncate_scoring_json(sp_parsed: Any, max_chars: int = 5000) -> str:
    """将评分点数据序列化为 JSON，超长时按条目智能截断（保证输出始终为合法 JSON）。

    Args:
        sp_parsed: 已解析的评分点数据（list 或 dict）
        max_chars: 最大字符数

    Returns:
        str: 合法 JSON 字符串（长度 <= max_chars，除非单条数据本身超长）
    """
    text = json.dumps(sp_parsed, ensure_ascii=False)
    if len(text) <= max_chars:
        return text

    # 定位需要截断的条目列表
    list_key: str | None = None
    if isinstance(sp_parsed, list):
        items: list | None = sp_parsed
    elif isinstance(sp_parsed, dict):
        items = None
        for key in ("items", "scoring_items", "scoring_points", "data"):
            if isinstance(sp_parsed.get(key), list):
                items = sp_parsed[key]
                list_key = key
                break
    else:
        items = None

    if items:
        # 二分查找最多可保留的条目数
        lo, hi = 1, len(items)
        best = 0
        while lo <= hi:
            mid = (lo + hi) // 2
            if list_key is None:
                candidate: Any = items[:mid]
            else:
                candidate = dict(sp_parsed)
                candidate[list_key] = items[:mid]
            if len(json.dumps(candidate, ensure_ascii=False)) <= max_chars:
                best = mid
                lo = mid + 1
            else:
                hi = mid - 1
        if best > 0:
            logger.warning(
                "V5.1 评分点超长，按条目截断: %d -> %d 条", len(items), best
            )
            if list_key is None:
                return json.dumps(items[:best], ensure_ascii=False)
            truncated = dict(sp_parsed)
            truncated[list_key] = items[:best]
            return json.dumps(truncated, ensure_ascii=False)

    # 单条即超长的极端情况：返回空（宁可缺评分也不喂非法 JSON）
    logger.warning("V5.1 评分点单条超长且无法按条目截断，置空处理")
    return ""


def _collect_gate_suggestions(verdict: dict, max_items: int = 8) -> list[str]:
    """从目录门禁裁决中提取「可执行的重生成建议」（文档五：审核建议回灌）。

    只取 error/warning 级别且带具体问题或修复意见的条目——纯提示性
    （advisory / info）条目回灌进提示词只会稀释注意力。
    优先使用 `repair`（可执行的修改意见），缺失时退回 `problem`（问题描述）；
    若问题定位到具体章节，把章节号一并带上，便于模型定向修正。

    Args:
        verdict: `outline_gate.build_verdict` 的裁决结果
        max_items: 最多回灌的条目数（防止提示词被建议淹没）

    Returns:
        建议文本列表；无可执行建议时为空列表（调用方据此跳过重生成）。
    """
    if not isinstance(verdict, dict):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for issue in verdict.get("issues") or []:
        if not isinstance(issue, dict):
            continue
        severity = str(issue.get("severity") or "")
        if severity not in ("error", "warning"):
            continue
        text = str(issue.get("repair") or issue.get("problem") or "").strip()
        if not text:
            continue
        node_ids = issue.get("node_ids") or []
        if node_ids:
            text = f"（章节 {', '.join(str(n) for n in node_ids[:3])}）{text}"
        if text in seen:
            continue
        seen.add(text)
        out.append(text)
        if len(out) >= max_items:
            break
    return out


def _sync_edited_outline_after_safety_net(
    v51_state: Any, sc_assigned: Any, level1: list[dict]
) -> None:
    """自检后标题安全网执行完毕，把 `v51_state.edited_outline` 刷新为新树。

    BUG-FIX 2026-10-13（分步生成状态错乱 · 安全网结果被丢弃）：
    `_self_check_v51` 收尾时把 `edited_outline` 置为自检后的树，而自检
    （`_auto_fix_issues` 重命名 / 门禁重排）可能引入新的占位/重复标题，
    需要安全网再兜一次。最终审核取 `edited_outline or level1`，若不同步，
    auto_confirm 模式下安全网的清洗结果会被整体丢弃。

    同步条件（全部满足才刷新）：
      1. `v51_state` 非 None；
      2. `sc_assigned` 非 None（自检确实写入过 edited_outline）；
      3. 当前 `edited_outline` 仍是 `sc_assigned` 同一对象——即自检写入后
         没有用户编辑/回灌采纳把它替换掉。对象被替换时尊重更新的值。
    安全网是同步执行（无 await），调用前后对象不可能被并发替换，
    因此该 identity 判定是精确的。
    """
    if v51_state is None or sc_assigned is None:
        return
    if getattr(v51_state, "edited_outline", None) is not sc_assigned:
        return
    try:
        v51_state.edited_outline = level1
    except Exception:
        logger.debug("V5.1 安全网后 edited_outline 同步失败", exc_info=True)


def _post_selfcheck_title_safety_net(outline: list[dict]) -> list[dict]:
    """AI 自检之后的标题兜底：清洗占位/泛化标题 + 同级去重。

    BUG-FIX 2026-10-12（P1 · 目录脏标题漏出）：`_self_check_v51` 会**改标题**——
      1) `_quick_local_check` 检出空标题/同级重名后交给 `_auto_fix_issues` 重命名，
         而它的兜底文案是「子章节N」「内容N」这类无语义占位；
      2) `outline_gate.reorder_by_zone` 会整章搬移节点，父子关系变化后
         同级重名可能新出现。
    但三级标题质量收口链（清洗 → 去重 → 具体性校验 → AI 修复）此前整体跑在
    `_self_check_v51` **之前**，自检引入的新问题没有任何一道防线再兜一次，
    占位标题与重复标题直接进入最终审核与落库。这正是用户报告的
    「三级标题大量是占位式泛化名称（专项6/总体实施策划（2））」能绕过全部
    规则清洗直抵界面的结构性原因。

    此处补上自检后的最后一道规则兜底。刻意**不再调 AI**：自检已在其后执行过
    AI 语义审查，且再插一轮 AI 调用会显著拉长生成时长；AI 精化仍由
    `_l3_specificity_closeout` 在生成中段承担。

    只做规则操作（幂等、纯本地、不抛异常），任何异常都原样返回入参目录，
    绝不阻断生成。
    """
    if not isinstance(outline, list):
        return outline
    try:
        _fixed = _cleanup_placeholder_titles(outline)
        outline = _uniquify_outline_titles(outline)
        # 清洗可能与既有兄弟节点撞名，再收一次（幂等）
        _fixed2 = _cleanup_placeholder_titles(outline)
        # 2026-10-15 新增（B3）：跨章成套复用护栏。同级去重政策（2026-10-11）
        # 使跨父同名不再被消歧，导致同一批泛化标题被成套复制到几十章；
        # 护栏按复用广度上限改写超出的节点，改写后再收一次占位（幂等）。
        outline, _capped = _cap_cross_chapter_repetition(
            outline, OUTLINE_L3_MAX_PARENT_REUSE
        )
        if _capped:
            outline = _uniquify_outline_titles(outline)
            _fixed2 += _cleanup_placeholder_titles(outline)
        if _fixed or _fixed2 or _capped:
            logger.info(
                "V5.1 自检后标题兜底: 清洗 %d 个占位/泛化三级标题，跨章成套复用改写 %d 个",
                _fixed + _fixed2,
                _capped,
            )
        return outline
    except Exception as _e:
        logger.warning("V5.1 自检后标题兜底异常（已忽略）: %s", _e)
        return outline


async def _l3_specificity_closeout(
    provider: Any,
    outline: list[dict],
    *,
    summary: str = "",
    tender_context: str = "",
    boq_text: str = "",
    project_scope: str = "",
    phase_callback: Callable | None = None,
    phase_label: str = "V5.1标题质量",
) -> list[dict]:
    """三级标题「具体性校验 → AI 修复 → 二次收口」。

    与 `_apply_l3_quality_closeout` 的分工：后者是**完整收口**（清洗 → 去重 →
    本函数），供直出路径在生成末尾整体调用；本函数只做**具体性**那一段，供
    分步路径在「清洗 → 去重」之后、以及审核回灌重生成路径单独复用。

    抽出独立函数的原因（BUG-FIX 2026-10-12）：分步主路径与审核回灌路径此前各自
    内联了一份逻辑完全相同、但极易漂移的代码块（回灌那份还漏掉了 AI 修复环节），
    两处口径不一致。统一后新增调用点只需一行。

    ⚠️ 接线契约：本函数不得返回 int（历史上 `_cleanup_placeholder_titles` 的返回值
    被误赋给目录树变量，导致目录树退化为 int 并在下游抛
    `TypeError: 'int' object is not iterable`，让整条审核回灌路径静默失效）。
    """
    try:
        _passed, _warnings = validate_l3_specificity(outline)
        if not _passed and provider is not None:
            if phase_callback:
                await phase_callback(
                    phase_label,
                    f"检测到 {len(_warnings)} 个缺乏项目针对性的三级标题，正在修复...",
                )
            # BUG-FIX 2026-10-13（G7 · 大目录修复上限结构性残留）：
            # 旧实现单轮只修 `OUTLINE_L3_REPAIR_MAX`（默认 24）条，二次校验
            # 的结果仅写日志、无任何补救。300 个泛化标题的目录只修 24 个，
            # 276 个原样进入最终审核与落库。现改为多轮修复：
            #   · 每轮最多修 OUTLINE_L3_REPAIR_MAX 条（并发/负载口径不变）；
            #   · 轮数上限 OUTLINE_L3_REPAIR_MAX_ROUNDS（默认 3）；
            #   · 任一轮修复后问题数不降（无进展）即提前收尾，避免空烧 AI；
            #   · 仍有残留时显式 warning + phase_callback 告知，不再静默。
            _max_rounds = max(1, OUTLINE_L3_REPAIR_MAX_ROUNDS)
            _prev_count = len(_warnings)
            for _round in range(_max_rounds):
                outline = await repair_l3_generic_titles(
                    provider,
                    outline,
                    summary or "",
                    tender_context or "",
                    boq_text or "",
                    "",
                    "",
                    project_scope or "",
                )
                # 修复后再次收口（与分步路径一致）：AI 修复结果可能引入新的占位/重复。
                # ⚠️ _cleanup_placeholder_titles 返回清洗计数（int），不可赋给 outline。
                _cleanup_placeholder_titles(outline)
                outline = _uniquify_outline_titles(outline)
                _passed2, _warnings2 = validate_l3_specificity(outline)
                logger.info(
                    "%s: 三级标题具体性校验 第%d轮 %d → %d 个问题（%s）",
                    phase_label,
                    _round + 1,
                    _prev_count,
                    len(_warnings2),
                    "通过" if _passed2 else "仍有残留",
                )
                if _passed2:
                    break
                if len(_warnings2) >= _prev_count:
                    # 无进展：继续跑只会重复同样的失败，及时收尾
                    break
                _prev_count = len(_warnings2)
            if not _passed2:
                logger.warning(
                    "%s: 三级标题具体性校验经 %d 轮修复后仍有 %d 个泛化标题残留"
                    "（每轮上限 %d 条）。样例: %s",
                    phase_label,
                    _max_rounds,
                    len(_warnings2),
                    OUTLINE_L3_REPAIR_MAX,
                    "、".join(w["title"] for w in _warnings2[:3]),
                )
                if phase_callback:
                    await phase_callback(
                        phase_label,
                        f"仍有 {len(_warnings2)} 个三级标题为泛化名称（已达修复上限，可在目录编辑中手动调整）",
                    )
        elif not _passed:
            # 无 AI provider（离线/未配置密钥）时，validate 发现的问题只能停留在
            # 规则兜底标题。显式暴露，避免用户无法区分「已精化」与「规则兜底」。
            logger.warning(
                "%s: %d 个三级标题由规则兜底生成（未配置 AI provider）。样例: %s",
                phase_label,
                len(_warnings),
                "、".join(w["title"] for w in _warnings[:3]),
            )
            if phase_callback:
                await phase_callback(
                    phase_label,
                    f"{len(_warnings)} 个三级标题为规则兜底生成，配置 AI 后可自动精化",
                )
    except Exception as _e:
        # 具体性收口属增强环节，任何异常都不得阻断目录生成
        logger.warning("%s 异常（已忽略，保留当前目录）: %s", phase_label, _e)
    return outline


async def _apply_l3_quality_closeout(
    provider: Any,
    outline: list[dict],
    summary: str = "",
    tender_context: str = "",
    boq_text: str = "",
    project_scope: str = "",
    phase_callback: Callable | None = None,
) -> list[dict]:
    """直出路径的 L3 标题质量统一治理（与分步路径收口逻辑完全一致）。

    BUG-2026-1009-01/02/03 修复覆盖直出路径：短标书直出模式（generate_outline_direct）
    此前在 `generate_outline_v51` 中直接 return，绕过分步路径的「占位/泛化标题清洗 →
    全局去重 → 具体性校验 → AI 修复」质量防线，导致直出产物仍可残留「专项6」
    「总体实施策划（2）」这类脏标题。本函数按同一套口径收口，保证两条生成路径
    产出目录的三级标题质量一致。

    ⚠️ 接线契约：测试 test_l3_title_quality_2026_10_10.py 的 `_block()` 依赖
    `src.index` 定位分步路径的质量收口块，因此本函数的文档字符串**不得**包含与
    分步块注释完全相同的定位锚子串（现措辞已规避）。

    Args:
        provider: AI 提供商（None 时仅走规则清洗/去重，不做 AI 精化）
        outline: 目录树（直出产物）
        summary: 项目概述
        tender_context: 招标文件全文
        boq_text: 工程量清单文本
        project_scope: 承包范围
        phase_callback: 阶段回调

    Returns:
        质量治理后的目录树
    """
    if not OUTLINE_L3_QUALITY_ENHANCE_ENABLED:
        return outline
    _t_phase = time.monotonic()
    # BUG-FIX 2026-10-13（G14）：旧实现 :5202-5203 两步无异常包裹，而
    # `_cleanup_placeholder_titles` 只在被 `generate_outline_v51` 的
    # `generate_outline_direct` try 块之外调用——一旦清洗抛异常（如树里混入
    # 非 dict 节点的边界数据），异常会冒泡到直出分支的 except，把**已成功
    # 生成的目录**当作直出失败丢弃并回退分步路径（整段直出成果作废）。
    # 收口整体属增强环节，与 `_l3_specificity_closeout` 保持同一容错口径：
    # 任何异常都原样返回入参目录，绝不阻断生成。
    try:
        # 顺序必须是「清洗 → 去重」：先清裸编号后缀再判重，避免去重把
        # 「总体实施策划（2）」的裸编号挪到标题中间导致清洗失效（与分步路径一致）。
        _cleaned = _cleanup_placeholder_titles(outline)
        outline = _uniquify_outline_titles(outline)
        # 2026-10-15 新增（B3）：跨章成套复用护栏（与分步路径同一实现、同一上限）
        outline, _capped_direct = _cap_cross_chapter_repetition(
            outline, OUTLINE_L3_MAX_PARENT_REUSE
        )
        if _capped_direct:
            outline = _uniquify_outline_titles(outline)
            _cleaned += _cleanup_placeholder_titles(outline)
        if _cleaned:
            logger.info(
                "V5.1直出路径 L3质量收口: 清洗 %d 个占位/泛化三级标题", _cleaned
            )
    except Exception as _e:
        logger.warning(
            "V5.1直出路径 L3质量收口异常（已忽略，保留原目录）: %s", _e
        )
    outline = await _l3_specificity_closeout(
        provider,
        outline,
        summary=summary or "",
        tender_context=tender_context or "",
        boq_text=boq_text or "",
        project_scope=project_scope or "",
        phase_callback=phase_callback,
        phase_label="V5.1直出路径 L3质量收口",
    )
    logger.info(
        "[V5.1-PHASE] 直出路径 L3质量收口耗时: %.1fs", time.monotonic() - _t_phase
    )
    return outline


async def generate_outline_v51(
    provider: Any,
    summary: str,
    scoring_points: str,
    industry_profile: str,
    tender_context: str = "",
    project_scope: str = "",
    project_info: dict | None = None,
    global_facts: list | None = None,
    progress_callback: Callable | None = None,
    phase_callback: Callable | None = None,
    v51_state: Any = None,
    outline_queue: asyncio.Queue | None = None,
    auto_confirm: bool = False,
    reference_outline: str = "",
    boq_text: str = "",
    outline_mode: str = "auto",
    event_emitter: Callable | None = None,
) -> list[dict]:
    """V5.1 目录生成——基于设计文档的预构建骨架模式。

    工作流（严格遵循《标书智能体（二）》设计文档）：
    1. L1：AI 根据评分要求生成一级目录标题（rating_item → new_title）
    2. 人工审核：一级目录（可跳过）
    3. L2：代码预构建 JSON 骨架 → AI 并发填充二三级标题
    4. 人工审核：二级目录（可跳过）
    5. L3：全量串行后处理，检查三级节点完整性（非流水线，整树一次性遍历）
    6. AI 自检 + 最终审核

    ENHANCE 2026-10-05（P-4）：新增 `outline_mode` 参数，支持「短标书 +
    强模型」直出路径（易标规格二.2）。`outline_mode="auto"` 时按评分规模
    自动判别；`"direct"` 强制直出；`"stepwise"` 强制分步。
    直出路径失败（AI 无返回 / JSON 非法 / 规模超限）自动回退分步路径。

    Args:
        provider: AI 提供商
        summary: 项目概述
        scoring_points: 评分点 JSON
        industry_profile: 行业背景
        tender_context: 招标文件全文
        project_scope: 承包范围
        project_info: 项目信息
        global_facts: 全局事实变量
        progress_callback: 进度回调
        phase_callback: 阶段回调
        v51_state: V5.1 状态（支持暂停/恢复）
        outline_queue: SSE 事件队列
        auto_confirm: 是否自动确认审核（True=全自动）
        outline_mode: "auto" 自动判别 / "direct" 强制直出 / "stepwise" 强制分步
    """
    _t_start = time.monotonic()
    _t_phase = time.monotonic()
    project_info = project_info or {}

    logger.info("[V5.1] 开始: summary_len=%d, scoring_len=%d", len(summary), len(scoring_points))

    # ===== ENHANCE 2026-10-05（P-4）：短标书直出路径分支 =====
    if should_use_direct_outline(scoring_points, outline_mode):
        if phase_callback:
            await phase_callback("目录生成", "检测到短标书，启用一次性直出模式...")
        logger.info(
            "[V5.1] outline_mode=%s → 走短标书直出路径", outline_mode
        )
        try:
            direct = await generate_outline_direct(
                provider,
                summary,
                scoring_points,
                industry_profile,
                project_scope=project_scope,
                reference_outline=reference_outline,
            )
        except Exception as e:
            direct = []
            logger.warning("[V5.1] 直出路径异常，回退分步路径: %s", e)
        if direct:
            # 直出路径三级标题质量收口（与分步路径一致，BUG-2026-1009-01/02/03 修复）：
            # 分步路径的「占位/泛化标题清洗 → 全局去重 → 具体性校验 → AI 修复」质量
            # 防线在下方仅对 level1 生效；直出路径此前在此时直接返回，短标书产物仍可能
            # 带「专项6」「总体实施策划（2）」这类脏标题。此处先收口再进入最终审核，
            # 用户审核与落库拿到的都是收口后的目录，两条生成路径质量口径一致。
            direct = await _apply_l3_quality_closeout(
                provider,
                direct,
                summary,
                tender_context,
                boq_text,
                project_scope,
                phase_callback=phase_callback,
            )
            # 直出路径不做人工审核点（无 L1/L2 中间态可审），但保留最终审核入口
            if v51_state and outline_queue is not None:
                if getattr(v51_state, "cancelled", False):
                    return []
                if v51_state.edited_outline is None:
                    v51_state.edited_outline = copy.deepcopy(direct)
                v51_state.current_phase = "final"
                edited = await _wait_for_v51_review(
                    outline_queue, v51_state, "final", direct,
                    auto_confirm=auto_confirm, event_emitter=event_emitter,
                )
                if edited is None:
                    logger.info("[V5.1] 直出目录最终审核未通过，中止")
                    return []
                direct = edited

        if direct:
            # 编号安全网（修复：直出路径与分步路径产物结构不一致 / 编辑后编号未重算）：
            # 直出路径不经过门禁 rennumber，且最终审核可能有人工编辑/重排，落库前按当前
            # 真实树结构重排位置式 id（1 / 1.1 / 1.1.1）。renumber_outline_ids 仅改 id，
            # 不动 title/children，幂等且安全，确保 id 始终与层级、顺序严格一致。
            direct, _id_changes = outline_gate.renumber_outline_ids(direct)
            if _id_changes:
                logger.info("[V5.1] 直出路径编号重排: 修正 %d 个 id", _id_changes)
            logger.info(
                "[V5.1] 直出路径完成: %d 章, 耗时 %.1fs",
                len(direct), time.monotonic() - _t_start,
            )
            # BUG-FIX 2026-10-08（P1 直出失败不回退）：return 原先位于 if 块外，
            # 直出异常（direct=[]）时直接返回空目录，"回退分步路径"永远不执行
            # （下一行回退日志是不可达死代码）。生产表现：短标书直出遇 AI 超时/
            # JSON 失败时目录生成为空，而不是按设计回退到分步生成。修复后仅
            # 直出成功才返回，空结果落到下方分步路径。
            return direct
        logger.warning("[V5.1] 直出路径未产出结果，回退分步路径")

    # 解析评分点
    # 遗留修复：改用容错解析（json → 代码围栏/内嵌 JSON 救援 → 空评分降级）。
    # 纯 json.loads 对「尾部多余逗号 / AI 附了说明文字 / ```json 围栏」一律失败，
    # 会把评分点整批丢掉；救援解析能保住绝大部分评分信息，只有真正无法提取时
    # 才降级为空评分继续生成（保持不阻断）。
    sp_parsed, _sp_mode = _parse_scoring_points_json(scoring_points)
    if sp_parsed is None:
        sp_parsed = []
        if _sp_mode == "failed":
            # 保留既有降级语义，但明确记录原因，便于排查「目录与招标要求脱节」
            logger.error(
                "V5.1 评分点 JSON 解析失败（已尝试救援解析），将以空评分继续: %s",
                (scoring_points or "")[:200],
            )
    # BUG-FIX 2026-09-18（高）：最上游清洗一次 —— `sp_text`（注入 L1 提示词
    # `<requirements>` 的那段 JSON）以及 L1/L2/AI 自检拿到的评分点全部源自这里，
    # 在此剔除系统提示占位项可一次覆盖所有下游消费点。
    sp_parsed = _strip_scoring_placeholders(sp_parsed)
    # BUG-FIX-SP-TRUNCATE：原实现 json.dumps 后硬截断 [:5000]，超长评分点会被
    # 截成非法 JSON，下游 _generate_level1_v51 的 json.loads 解析失败导致
    # score_item_count=0，评分项数量覆盖校验被整体跳过。
    # 改为按条目智能截断，保证输出始终是合法 JSON。
    sp_text = _truncate_scoring_json(sp_parsed, max_chars=5000) if sp_parsed else ""

    # 构建参考模板：调用方传入的参考目录优先，缺省才回退内置模板。
    reference_json = _resolve_reference_json(reference_outline)

    # ===== L1：生成一级目录 =====
    _t_phase = time.monotonic()
    level1 = await _generate_level1_v51(
        provider,
        summary,
        sp_text,
        reference_json,
        global_facts=global_facts,
        phase_callback=phase_callback,
        project_info=project_info,
        tender_context=tender_context,
        industry_profile=industry_profile,
    )
    logger.info(
        "V5.1 L1 完成: %d 章, 耗时 %.1fs", len(level1), time.monotonic() - _t_start
    )
    logger.info(
        "[V5.1-PHASE] L1 生成阶段耗时: %.1fs", time.monotonic() - _t_phase
    )

    # ===== 人工审核：L1 =====
    if v51_state and outline_queue is not None:
        edited = await _wait_for_v51_review(
            outline_queue, v51_state, "level1", level1,
            auto_confirm=auto_confirm, event_emitter=event_emitter,
        )
        if edited is None:
            logger.info("V5.1 L1 审核未通过，中止")
            return []
        level1 = edited

    # ===== L2：预构建骨架 + AI 并发填充 =====
    # BUG-FIX-48 修复：原取消机制仅在审核点检查 cancelled 标志，L1/L2/L3
    # 生成阶段无法中途取消。在关键转换点添加取消检查，用户取消后立即中止。
    if v51_state and getattr(v51_state, "cancelled", False):
        logger.info("V5.1 用户已取消（L2前），中止生成")
        return []

    # BUG-FIX-PHASE-01：更新阶段状态（此前 current_phase 恒为 level1，
    # 断点续生持久化的阶段进度失真）
    if v51_state:
        v51_state.current_phase = "level2"

    _t_phase = time.monotonic()
    l2_done_queue: asyncio.Queue = asyncio.Queue()

    # BUG-OUTLINE-09 修复：创建任务级信号量，避免全局变量竞争
    provider_base_url = getattr(provider, "base_url", "")
    max_concurrent = 16 if "anthropic" in provider_base_url else 32
    task_sem = DynamicSemaphore(max_concurrent)

    level1 = await _generate_level2_v51(
        provider,
        level1,
        summary,
        sp_text,
        progress_callback=progress_callback,
        phase_callback=phase_callback,
        l2_done_queue=l2_done_queue,
        project_info=project_info,
        project_scope=project_scope,
        task_semaphore=task_sem,
        global_facts=global_facts,
        tender_context=tender_context,
        boq_text=boq_text,
    )
    _n_l2 = sum(
        1 + sum(1 + sum(1 for _ in l2.get("children", [])) for l2 in l1.get("children", []))
        for l1 in level1
    )
    logger.info(
        "V5.1 L2 完成: %d 章, 总节点 %d, 耗时 %.1fs",
        len(level1),
        _n_l2,
        time.monotonic() - _t_start,
    )
    logger.info(
        "[V5.1-PHASE] L2 并发填充阶段耗时: %.1fs", time.monotonic() - _t_phase
    )

    # ===== 标题质量后处理：修正通用化/低质量标题 =====
    if phase_callback:
        await phase_callback("V5.1标题质量", "正在修正标题质量...")
    level1, _title_fixes = _post_process_title_quality(level1)
    if _title_fixes > 0:
        logger.info("V5.1 标题质量修正: 自动修正 %d 个低质量标题", _title_fixes)
    if phase_callback:
        await phase_callback("V5.1标题质量", f"标题质量修正完成（{_title_fixes} 处修正）")

    # ===== 人工审核：L2 =====
    if v51_state and outline_queue is not None:
        edited = await _wait_for_v51_review(
            outline_queue, v51_state, "level2", level1,
            auto_confirm=auto_confirm, event_emitter=event_emitter,
        )
        if edited is None:
            logger.info("V5.1 L2 审核未通过，中止")
            return []
        level1 = edited

    # ===== L3：检查三级节点完整性 =====
    # BUG-FIX-48: L3前取消检查
    if v51_state and getattr(v51_state, "cancelled", False):
        logger.info("V5.1 用户已取消（L3前），中止生成")
        return []

    # BUG-FIX-PHASE-01：更新阶段状态
    if v51_state:
        v51_state.current_phase = "level3"

    _t_phase = time.monotonic()
    level1 = await _generate_level3_v51(
        provider,
        level1,
        summary,
        sp_text,
        phase_callback=phase_callback,
        l2_done_queue=l2_done_queue,
        project_info=project_info,
    )
    logger.info(
        "[V5.1-PHASE] L3 三级目录阶段耗时: %.1fs", time.monotonic() - _t_phase
    )

    # ===== 兜底补全：确保每个节点结构完整 =====
    level1 = _ensure_min_children(level1, min_level2=3, min_level3=5)

    # ===== 技术增强后处理：将通用三级标题注入技术含量 =====
    _t_phase = time.monotonic()
    if phase_callback:
        await phase_callback("V5.1技术增强", "正在增强三级标题技术含量...")
    level1, _tech_fixes = _tech_enhance_titles(level1)
    if _tech_fixes > 0:
        logger.info("V5.1 技术增强: 自动增强 %d 个通用三级标题的技术含量", _tech_fixes)
    logger.info(
        "[V5.1-PHASE] 标题质量+技术增强耗时: %.1fs", time.monotonic() - _t_phase
    )
    if phase_callback:
        await phase_callback("V5.1技术增强", f"技术增强完成（{_tech_fixes} 处增强）")

    # ===== L3 标题质量收口：占位/泛化标题识别 + 具体性校验 + AI 修复 =====
    # 2026-10-10 修复：`validate_l3_specificity` / `repair_l3_generic_titles`
    # 此前在代码库中长期存在但**没有任何生产调用方**（死代码）——
    # 既有的规则链路无法识别「总体实施策划（2）」「专项6」这类占位泛化标题，
    # 导致三级标题大量退化为无信息量的占位名称。本段将其接入主链路。
    if OUTLINE_L3_QUALITY_ENHANCE_ENABLED:
        _t_phase = time.monotonic()
        # 2026-10-11 修复：顺序必须是「清洗 → 去重」，与 4169-4178 的 L3 生成收口一致。
        # 旧顺序为「去重 → 清洗」，存在缺陷：_uniquify_outline_titles 对带裸编号后缀的
        # 脏标题（如「总体实施策划（2）」）做归一化时已把后缀剥离，判定与同名的
        # 「总体实施策划」碰撞，于是给原标题追加维度词 →「总体实施策划（2）专项方案」，
        # 裸编号从「末尾」移到了「中间」。随后 _cleanup_placeholder_titles 依赖
        # 末尾后缀匹配，再也识别不出它 → 脏标题从质量收口环节静默漏出。
        # 先清洗（把脏标题改写成语义化标题）再去重（解决残留碰撞），两个缺陷同时消除。
        _cleaned_l3 = _cleanup_placeholder_titles(level1)
        level1 = _uniquify_outline_titles(level1)
        # 2026-10-15 新增（B3）：跨章成套复用护栏 —— AI 逐章按同一套骨架提示词
        # 填充时，同一批泛化三级标题会被成套复制到几十个章（生产库实测单标题
        # 25 个父级、跨章重复率 97%）。同级去重不拦这类形态，此处按复用广度收口。
        level1, _capped_l3 = _cap_cross_chapter_repetition(
            level1, OUTLINE_L3_MAX_PARENT_REUSE
        )
        if _capped_l3:
            level1 = _uniquify_outline_titles(level1)
            _cleaned_l3 += _cleanup_placeholder_titles(level1)
        if _cleaned_l3:
            logger.info("V5.1 L3质量收口: 清洗 %d 个占位/泛化三级标题", _cleaned_l3)
        # BUG-FIX 2026-10-12：与审核回灌路径共用同一实现，消除两处内联副本的漂移
        # （回灌那份此前整体缺失「具体性校验 → AI 修复」环节）。
        level1 = await _l3_specificity_closeout(
            provider,
            level1,
            summary=summary or "",
            tender_context=tender_context or "",
            boq_text=boq_text or "",
            project_scope=project_scope or "",
            phase_callback=phase_callback,
            phase_label="V5.1标题质量",
        )
        logger.info(
            "[V5.1-PHASE] L3标题质量收口耗时: %.1fs", time.monotonic() - _t_phase
        )

    # ===== AI 自检 =====
    # BUG-FIX-48: 自检前取消检查
    if v51_state and getattr(v51_state, "cancelled", False):
        logger.info("V5.1 用户已取消（自检前），中止生成")
        return []

    # 字数预算只从评分要点与承包范围解析：招标文件全文（tender_context）动辄
    # 数万字，其中"每页 N 字""限 N 字"这类排版要求会被 _WORD_TOTAL_RE 命中，
    # 把目标叶子数推导到个位数。护栏见 outline_gate.parse_word_budget。
    # 提出为变量：首轮自检与「审核回灌」重生成的自检共用同一预算口径。
    _word_budget_text = "\n".join(
        part for part in (scoring_points, project_scope) if part
    )

    _t_phase = time.monotonic()
    _gate_verdict: dict = {}
    level1 = await _self_check_v51(
        provider,
        level1,
        _t_start,
        phase_callback=phase_callback,
        v51_state=v51_state,
        project_info=project_info,
        scoring_points=scoring_points,
        verdict_out=_gate_verdict,
        # 字数预算只从评分要点与承包范围解析：招标文件全文（tender_context）动辄
        # 数万字，其中"每页 N 字""限 N 字"这类排版要求会被 _WORD_TOTAL_RE 命中，
        # 把目标叶子数推导到个位数。护栏见 outline_gate.parse_word_budget。
        word_budget_text=_word_budget_text,
    )
    logger.info(
        "[V5.1-PHASE] AI 自检阶段耗时: %.1fs", time.monotonic() - _t_phase
    )
    # BUG-FIX 2026-10-13（分步生成状态错乱 · 安全网结果被丢弃）：
    # `_self_check_v51` 收尾时把 `v51_state.edited_outline` 置为自检后的树
    # （见该函数结尾；test_self_check_v51_persists_edited_outline 锁定该语义），
    # 而自检（_auto_fix_issues 重命名/门禁重排）可能引入新的占位/重复标题，
    # 需要下面安全网再兜一次。但最终审核取的是
    # `outline_for_review = v51_state.edited_outline or level1`：
    #   · auto_confirm 模式下 `_wait_for_v51_review` 原样返回该树 → 安全网的
    #     清洗/去重结果被整体丢弃，占位标题重新进入最终审核与落库前的展示；
    #   · 交互模式下用户收到的 review_needed 事件同样是未过安全网的旧树。
    # 修复：先快照自检写入的对象；安全网是同步执行（无 await，期间不可能被
    # 用户编辑替换），其后若 edited_outline 仍是该对象，刷新为安全网后的
    # 新树；若已被替换（用户在自检期间提交了编辑），尊重用户的值不覆盖。
    _sc_assigned = getattr(v51_state, "edited_outline", None) if v51_state is not None else None
    level1 = _post_selfcheck_title_safety_net(level1)
    _sync_edited_outline_after_safety_net(v51_state, _sc_assigned, level1)

    # ===== ENHANCE 2026-10-09（文档五）：审核不通过 → 带建议重生成一次 =====
    # 文档五的闭环是「生成 → 审核 → 审核建议回灌 → 二次生成」，而不是把有问题的
    # 目录直接交给用户。这里只在**全自动**（auto_confirm）下启用：交互模式下
    # L1/L2 已经过用户审核确认，自动重生成会覆盖用户已确认的内容。
    # 严格限制：最多 1 轮、建议非空才跑、新结果问题数更少才采纳（防止越改越差）。
    if auto_confirm and _gate_verdict:
        _suggestions = _collect_gate_suggestions(_gate_verdict)
        if _suggestions and not (v51_state and getattr(v51_state, "cancelled", False)):
            logger.info(
                "V5.1 审核回灌：门禁发现 %d 项问题（%d 条建议），带建议重生成二三级目录",
                _gate_verdict.get("issue_count", 0), len(_suggestions),
            )
            if phase_callback:
                await phase_callback("V5.1审核回灌", "按审核建议重新生成目录…")
            _t_phase = time.monotonic()
            try:
                _retry_queue: asyncio.Queue = asyncio.Queue()
                _retry_tree = await _generate_level2_v51(
                    provider,
                    level1,
                    summary,
                    sp_text,
                    progress_callback=progress_callback,
                    phase_callback=phase_callback,
                    l2_done_queue=_retry_queue,
                    project_info=project_info,
                    project_scope=project_scope,
                    task_semaphore=task_sem,
                    global_facts=global_facts,
                    tender_context=tender_context,
                    boq_text=boq_text,
                    review_suggestions=_suggestions,
                )
                _retry_tree, _ = _post_process_title_quality(_retry_tree)
                _retry_tree = await _generate_level3_v51(
                    provider,
                    _retry_tree,
                    summary,
                    sp_text,
                    phase_callback=phase_callback,
                    l2_done_queue=_retry_queue,
                    project_info=project_info,
                )
                _retry_tree = _ensure_min_children(_retry_tree, min_level2=3, min_level3=5)
                _retry_tree, _ = _tech_enhance_titles(_retry_tree)
                # 2026-10-11 修复（O-10）：与主路径一致，_ensure_min_children / _tech_enhance_titles
                # 之后必须执行清洗+去重，避免引入的占位标题和重复标题遗留到最终输出。
                #
                # BUG-FIX 2026-10-12（P0）：原写作 `_retry_tree = _cleanup_placeholder_titles(...)`，
                # 而该函数返回的是**清洗计数 int**（不是目录树）——`_retry_tree` 立刻退化为
                # int，随后的 `_uniquify_outline_titles(int)` 静默 no-op（内部 isinstance
                # 守卫），再传给 `_self_check_v51(int, ...)` 抛
                # `TypeError: 'int' object is not iterable`，被下方 `except Exception` 吞掉
                # 只留一条 warning。净效果：**「审核不通过 → 带建议重生成」整条路径 100% 失效**，
                # 每次都走到 "重生成失败，保留原目录"。同类缺陷在主路径已于 2026-10-11 修复
                # （见本文件 L5437-5439 注释），本处当时漏改。改为分离返回值与目录树。
                _retry_ph_fixed = _cleanup_placeholder_titles(_retry_tree)
                if _retry_ph_fixed:
                    logger.info(
                        "V5.1 审核回灌：清洗 %d 个占位/泛化三级标题", _retry_ph_fixed
                    )
                _retry_tree = _uniquify_outline_titles(_retry_tree)
                # BUG-FIX 2026-10-12（P1）：重生成路径此前只有规则清洗，缺少主路径的
                # 「具体性校验 → AI 修复 → 二次收口」，与主路径质量口径不一致
                # （门禁重排后新引入的泛化标题不会被 AI 精化）。
                if OUTLINE_L3_QUALITY_ENHANCE_ENABLED:
                    _retry_tree = await _l3_specificity_closeout(
                        provider,
                        _retry_tree,
                        summary=summary or "",
                        tender_context=tender_context or "",
                        boq_text=boq_text or "",
                        project_scope=project_scope or "",
                        phase_callback=phase_callback,
                        phase_label="V5.1审核回灌",
                    )
                _retry_verdict: dict = {}
                _retry_tree = await _self_check_v51(
                    provider,
                    _retry_tree,
                    _t_start,
                    phase_callback=phase_callback,
                    v51_state=None,  # 先不落状态：采纳判定后再统一写入
                    project_info=project_info,
                    scoring_points=scoring_points,
                    word_budget_text=_word_budget_text,
                    verdict_out=_retry_verdict,
                )
                _retry_tree = _post_selfcheck_title_safety_net(_retry_tree)
                _before = int(_gate_verdict.get("issue_count") or 0)
                _after = int(_retry_verdict.get("issue_count") or 0)
                if _after < _before:
                    logger.info(
                        "V5.1 审核回灌：采纳重生成结果（问题 %d → %d），耗时 %.1fs",
                        _before, _after, time.monotonic() - _t_phase,
                    )
                    level1 = _retry_tree
                    _gate_verdict.update(_retry_verdict)
                    if v51_state is not None:
                        try:
                            v51_state.gate_report = _gate_verdict
                            v51_state.edited_outline = level1
                        except Exception:
                            logger.debug("V5.1 审核回灌：状态回写失败", exc_info=True)
                    if phase_callback:
                        await phase_callback(
                            "V5.1审核回灌", f"已按建议重生成（问题 {_before} → {_after}）"
                        )
                else:
                    logger.warning(
                        "V5.1 审核回灌：重生成未改善（问题 %d → %d），保留原目录",
                        _before, _after,
                    )
                    if phase_callback:
                        await phase_callback(
                            "V5.1审核回灌", f"重生成未改善（问题仍为 {_before}），保留原目录"
                        )
            except Exception as e:
                # 重生成只是一条优化路径，任何失败都保留首轮结果，不影响主流程
                logger.warning("V5.1 审核回灌重生成失败，保留原目录: %s", e)

    # ===== 最终审核 =====
    if v51_state and outline_queue is not None:
        # BUG-FIX-PHASE-01：更新阶段状态
        v51_state.current_phase = "final"
        outline_for_review = v51_state.edited_outline or level1
        edited = await _wait_for_v51_review(
            outline_queue, v51_state, "final", outline_for_review,
            auto_confirm=auto_confirm, event_emitter=event_emitter,
        )
        if edited is None:
            logger.info("V5.1 最终审核未通过，中止")
            return []
        level1 = edited

    # ===== 返回最终结果 =====
    # 编号安全网（修复：最终审核的人工编辑/重排发生在门禁 rennumber 之后，此处按最终
    # 真实结构重排位置式 id，确保 id 与层级、顺序严格一致；同时统一直出/分步两条路径
    # 的产物契约）。renumber_outline_ids 仅改 id，不动 title/children，幂等且安全。
    level1, _id_changes = outline_gate.renumber_outline_ids(level1)
    if _id_changes:
        logger.info("[V5.1] 终态编号重排: 修正 %d 个 id（含人工编辑后重排）", _id_changes)

    _total_nodes = sum(
        1 + sum(1 + sum(1 for _ in l2.get("children", [])) for l2 in l1.get("children", []))
        for l1 in level1
    )
    logger.info(
        "[V5.1] 完成: 耗时 %.1fs, 总节点 %d",
        time.monotonic() - _t_start,
        _total_nodes,
    )
    return level1

