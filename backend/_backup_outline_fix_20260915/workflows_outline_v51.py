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
import random
import re
import time
from typing import Any

from ...utils.json_parser import safe_json_parse
from . import outline_gate
from .outline_common import (
    _ensure_min_children,
    _parse_scope_items,
    _uniquify_outline_titles,
    check_json,
    generate_nodes_distribution,
    generate_one_outline_json_by_level1,
)
from .prompts import get_prompt
from .providers.base import AIMessage

# 从其他子模块导入工具函数
from .workflows_base import (
    DynamicSemaphore,
    collect_json_response,
)
from .workflows_sections import _format_facts_list


logger = logging.getLogger(__name__)

# V5.1 全局并发信号量已移除，改为任务级信号量避免并发竞争
# 每个 generate_outline_v51 任务会创建自己的信号量实例


# ========== 参考模板（可根据项目特点增删章节） ==========

REFERENCE_TEMPLATE_V51 = [
    {"id": "ref_1", "title": "项目概述"},
    {"id": "ref_2", "title": "项目重难点分析"},
    {"id": "ref_3", "title": "工程概况"},
    {"id": "ref_4", "title": "编制依据"},
    {"id": "ref_5", "title": "施工方案及技术措施"},
    {"id": "ref_6", "title": "施工现场总平面布置"},
    {"id": "ref_7", "title": "主要分部工程施工方法"},
    {"id": "ref_8", "title": "装配式混凝土结构（PC）工程专项方案"},
    {"id": "ref_9", "title": "质量保证措施和创优计划"},
    {"id": "ref_10", "title": "施工安全措施计划"},
    {"id": "ref_11", "title": "施工环保措施计划"},
    {"id": "ref_12", "title": "施工总进度计划及保证措施"},
    {"id": "ref_13", "title": "资源配备计划"},
    {"id": "ref_14", "title": "拟投入本工程的主要施工设备表"},
    {"id": "ref_15", "title": "主要材料及劳动力计划表"},
    {"id": "ref_16", "title": "特殊气候季节性施工方案"},
    {"id": "ref_17", "title": "成品保护和工程保修工作的管理措施和承诺"},
    {"id": "ref_18", "title": "危大工程及专项施工方案、紧急情况预案与风险控制措施"},
    {"id": "ref_19", "title": "对专业分包工程的配合、协调、管理、服务方案"},
    {"id": "ref_20", "title": "与各参建方的配合协调"},
    {"id": "ref_21", "title": "工期优化"},
    {"id": "ref_22", "title": "工程资料及竣工验收管理"},
    {"id": "ref_23", "title": "附图、附件"},
]


# ========== AI 调用统一入口 ==========


async def _call_ai_v51(
    provider: Any,
    system_prompt: str,
    user_prompt: str,
    max_tokens: int = 2000,
    max_retries: int = 1,
    project_info: dict | None = None,
) -> Any:
    """V5.1 统一 AI 调用入口。

    重试逻辑由 collect_json_response 内部处理（含 JSON 自修复链路），
    这里不再包一层循环，避免嵌套重试。
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
        temperature=0.1,
        max_retries=max_retries,
        max_tokens=max_tokens,
        extra_body=extra_body,
    )
    return result


# ========== L1 一级目录生成 ==========


async def _generate_level1_v51(
    provider: Any,
    summary: str,
    sp_text: str,
    reference_json: str,
    global_facts: list | None = None,
    phase_callback: Callable | None = None,
    project_info: dict | None = None,
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
        sp_parsed = json.loads(sp_text) if sp_text else []
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
    # 设计文档："直接返回json，不要任何额外说明或格式标记"
    base_user_prompt = (
        f"<overview>\n{(summary or '')[:1500]}\n</overview>\n"
        f"<requirements>\n{sp_text}\n</requirements>\n"
        f"<reference>\n{reference_json[:3000]}\n</reference>\n\n"
        f"请严格按评分要求生成一级目录。\n"
        f"评分项总数: {_score_item_count}\n"
        f"要求:\n"
        f"1. 每个评分项对应一个一级章节，数量与评分项严格相等\n"
        f"2. 章节标题在评分项文字基础上进行专业化调整\n"
        f"3. 按项目逻辑排序（总纲→组织→方案→管理→收尾）\n"
        f"4. 直接返回 JSON 数组，不要任何额外说明\n"
    )

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
    last_error = None
    last_raw = None
    for attempt in range(3):
        try:
            raw_result = await _call_ai_v51(
                provider,
                sys_prompt,
                base_user_prompt,
                max_tokens=8192,
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
                base_user_prompt += correction
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


def _assign_ids(nodes: list[dict]) -> list[dict]:
    """为节点分配连续 ID（从 1 开始）

    防御性修复：如果 nodes 中混入字符串（上游 _normalize_to_list 漏网），
    自动转换为 dict 格式，避免 str item assignment 错误。

    BUG 修复：正确处理 children 中的非 dict/非 string 元素，统一转换为 dict。
    """
    _normalized = []
    for node in nodes:
        if isinstance(node, str):
            if node.strip():
                _normalized.append({"title": node.strip(), "children": []})
        elif isinstance(node, dict):
            _normalized.append(node)
        else:
            _normalized.append({"title": str(node), "children": []})
    nodes = _normalized

    for i, node in enumerate(nodes):
        node["id"] = str(i + 1)
        # 递归处理 children
        if "children" in node and isinstance(node["children"], list):
            for j, child in enumerate(node["children"]):
                # BUG 修复：正确处理各种类型的 child
                if isinstance(child, str):
                    child = {"title": child, "children": []}
                    node["children"][j] = child
                elif not isinstance(child, dict):
                    # 非 dict 非 str 的元素（如 int、None 等），转为 dict
                    child = {"title": str(child) if child is not None else "", "children": []}
                    node["children"][j] = child
                # 现在 child 一定是 dict
                child["id"] = f"{i + 1}.{j + 1}"
                if "children" in child and isinstance(child["children"], list):
                    for k, leaf in enumerate(child["children"]):
                        # BUG 修复：正确处理各种类型的 leaf
                        if isinstance(leaf, str):
                            leaf = {"title": leaf, "children": []}
                            child["children"][k] = leaf
                        elif not isinstance(leaf, dict):
                            leaf = {"title": str(leaf) if leaf is not None else "", "children": []}
                            child["children"][k] = leaf
                        # 现在 leaf 一定是 dict
                        leaf["id"] = f"{i + 1}.{j + 1}.{k + 1}"
    return nodes


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
        return _derive_child_title(source, index, used, level=3)

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

    _tech_verbs = ("施工", "管理", "控制", "检查", "组织", "实施", "协调")
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


def _validate_level1_v51(level1: list[dict]) -> list[dict]:
    """校验一级目录（V51 严格版），通过后返回原列表

    校验项：
    - 章节数 28-32
    - 无重复标题
    - ID 连续（从1开始递增）
    - 每项有 title 字段
    """
    if not level1:
        raise ValueError("一级目录为空")

    # 检查章节数
    if len(level1) < 28:
        raise ValueError(f"一级目录长度不足（{len(level1)} < 28）")
    if len(level1) > 32:
        raise ValueError(f"一级目录长度过长（{len(level1)} > 32）")

    # 检查标题和 ID
    titles = []
    for i, node in enumerate(level1):
        if not isinstance(node, dict):
            raise ValueError(f"第 {i} 项不是 dict 类型")
        title = node.get("title")
        if title is None:
            raise KeyError(f"第 {i} 项缺少 title 字段")
        titles.append(title)

        expected_id = i + 1
        node_id = node.get("id")
        if node_id is not None and node_id != expected_id:
            raise ValueError(f"id不连续: 第{i}项期望id={expected_id}，实际id={node_id}")

    # 检查重复标题
    if len(set(titles)) != len(titles):
        seen = set()
        duplicates = [t for t in titles if t in seen or seen.add(t)]
        raise ValueError(f"一级目录存在重复标题: {list(set(duplicates))[:5]}")

    return level1


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
) -> dict:
    """用 AI 填充预构建骨架中的二三级标题。

    设计文档思路：
    1. 传给 AI 的是预构建 JSON 骨架（title 留空）
    2. AI 只在原结构上补全标题和描述，禁止修改结构
    3. 传入其他章节标题供参考，避免内容重叠
    4. 校验 AI 输出结构与骨架一致

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

    user_prompt = (
        f"<overview>\n{(summary or '')[:1200]}\n</overview>\n"
        f"<requirements>\n{sp_text[:800]}\n</requirements>\n"
        f"{facts_block}"
        f"<other_outline>\n{other_outline}\n</other_outline>\n"
        f"{scope_block}"
        f"<chapter_title>\n{level1_title}\n</chapter_title>\n"
        f"<json_outline>\n{skeleton_json}\n</json_outline>\n\n"
        f"请在原 JSON 骨架上补全 title 和 description。直接返回填充后的 JSON，不要额外说明。\n"
    )

    # 最多 3 次重试
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
                max_tokens=4096,
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
                user_prompt += correction
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
) -> list[dict]:
    """L2：并发生成二级目录（预构建骨架 + AI 填充模式）。

    设计文档流程：
    1. 用 generate_nodes_distribution 预构建每个一级章节的 JSON 骨架
    2. 并发生成：每个一级章节独立调用 AI 填充
    3. 传入其他章节标题避免重复
    4. 结构校验 + 3 次重试
    5. 单节点失败用默认目录填充，不中止整体流程

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
        # 三级默认标题从所属 L2 派生
        used_l3: set[str] = set()
        for j, leaf in enumerate(child.get("children", [])):
            _l3_title = _derive_child_title(_l2_title, j, used_l3, level=3)
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
            _l3_title = _derive_child_title(_l2_title, j, used_l3, level=3)
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
    """修正所有节点的 ID（按层级位置分配）。

    BUG-FIX-82: 添加类型检查，防止非 dict 元素导致 AttributeError
    """
    for i, node in enumerate(level1):
        if not isinstance(node, dict):
            continue
        node["id"] = str(i + 1)
        for j, child in enumerate(node.get("children", [])):
            if not isinstance(child, dict):
                continue
            child["id"] = f"{i + 1}.{j + 1}"
            for k, leaf in enumerate(child.get("children", [])):
                if not isinstance(leaf, dict):
                    continue
                leaf["id"] = f"{i + 1}.{j + 1}.{k + 1}"
    return level1


def _fix_ids_v51(level1: list[dict]) -> list[dict]:
    """修正整个目录树的 ID 编号（与 _fix_all_ids 兼容的测试专用接口）

    递归分配 ID：
    - L1: 1, 2, 3, ...
    - L2: 1.1, 1.2, ...
    - L3: 1.1.1, 1.1.2, ...
    - 处理缺少 children 键、空 children 等边界情况
    """
    for i, node in enumerate(level1):
        node["id"] = str(i + 1)  # BUG-FIX-83: 统一使用字符串 ID，与 _fix_all_ids 保持一致
        children = node.get("children")
        if children:
            for j, child in enumerate(children):
                child["id"] = f"{i + 1}.{j + 1}"
                grandchildren = child.get("children")
                if grandchildren:
                    for k, leaf in enumerate(grandchildren):
                        leaf["id"] = f"{i + 1}.{j + 1}.{k + 1}"
    return level1


# ========== L3 三级目录生成（流水线模式） ==========

# 场景模板映射：根据二级目录关键词匹配三级标题模板
# 每个模板是一组自然的、专业的三级标题候选
# 模板数量充足（≥20个），避免 idx 取模循环重复
_L3_SCENE_TEMPLATES: dict[str, list[str]] = {
    # 质量管理相关
    "quality_system": [
        "质量方针与目标",
        "质量管理组织",
        "质量管理制度",
        "质量保证措施",
        "质量控制流程",
        "质量检验标准",
        "质量问题处理",
        "质量改进机制",
        "质量记录管理",
        "质量策划与部署",
        "质量责任体系",
        "质量风险管控",
        "质量数据统计",
        "质量考核评价",
        "质量持续改进",
        "质量通病防治",
        "质量验收程序",
        "质量追溯机制",
        "质量信息管理",
        "质量创新应用",
    ],
    # 安全管理相关
    "safety_system": [
        "安全责任体系",
        "安全教育培训",
        "安全检查制度",
        "危险源辨识",
        "安全防护措施",
        "应急预案制定",
        "安全事故处理",
        "安全投入保障",
        "安全绩效评估",
        "安全组织架构",
        "安全规章制度",
        "安全风险分级",
        "安全技术交底",
        "安全设施管理",
        "安全行为规范",
        "安全台账记录",
        "安全应急演练",
        "安全隐患排查",
        "安全事故报告",
        "安全改进提升",
    ],
    # 进度管理相关
    "progress": [
        "进度计划编制",
        "进度节点控制",
        "进度动态跟踪",
        "进度偏差分析",
        "进度调整方案",
        "关键路径管理",
        "资源平衡调配",
        "里程碑验收标准",
        "进度报告制度",
        "进度分级管理",
        "进度预警机制",
        "进度考核奖惩",
        "进度优化方法",
        "进度网络分析",
        "进度资源配置",
        "进度风险预案",
        "进度协调沟通",
        "进度总结归档",
        "进度信息化管理",
        "进度持续改进",
    ],
    # 技术方案相关
    "technical": [
        "施工工艺选择",
        "技术参数确定",
        "操作要点说明",
        "质量控制指标",
        "常见问题预防",
        "技术交底内容",
        "试验检测方法",
        "工艺改进措施",
        "技术创新应用",
        "技术方案比选",
        "技术要求规范",
        "技术标准执行",
        "技术难点攻关",
        "技术路线规划",
        "技术验证方案",
        "技术资料整理",
        "技术总结沉淀",
        "技术专利申报",
        "技术交流机制",
        "技术成果转化",
    ],
    # 资源管理相关
    "resource": [
        "资源需求计划",
        "资源采购管理",
        "资源进场验收",
        "资源存储保管",
        "资源调配使用",
        "资源消耗控制",
        "资源盘点清查",
        "资源利用率",
        "资源节约措施",
        "资源分类编码",
        "资源供应商管理",
        "资源价格监测",
        "资源合同签订",
        "资源物流配送",
        "资源质量检验",
        "资源动态监控",
        "资源成本核算",
        "资源效率提升",
        "资源替代方案",
        "资源循环利用",
    ],
    # 项目管理相关
    "project": [
        "项目组织架构",
        "项目管理制度",
        "项目沟通机制",
        "项目文档管理",
        "项目里程碑",
        "项目风险管控",
        "项目验收标准",
        "项目总结评价",
        "项目移交清单",
        "项目范围管理",
        "项目成本控制",
        "项目质量管理",
        "项目时间管理",
        "项目人力资源",
        "项目采购管理",
        "项目集成管理",
        "项目干系人管理",
        "项目风险管理",
        "项目合同管理",
        "项目信息管理",
    ],
    # 通用管理
    "general_management": [
        "制度建设",
        "流程规范",
        "职责分工",
        "考核指标",
        "监督机制",
        "改进措施",
        "执行标准",
        "操作指南",
        "评估体系",
        "组织架构",
        "人员配置",
        "岗位职责",
        "工作流程",
        "管理办法",
        "实施细则",
        "指导意见",
        "实施计划",
        "推进方案",
        "保障措施",
        "长效机制",
    ],
    # 施工工艺相关
    "construction_method": [
        "施工测量放线",
        "土方开挖回填",
        "基坑支护降水",
        "钢筋加工安装",
        "模板支设拆除",
        "混凝土浇筑养护",
        "砌体工程施工",
        "防水工程施工",
        "保温隔热施工",
        "装饰装修施工",
        "机电安装施工",
        "脚手架搭拆",
        "施工通道布置",
        "材料垂直运输",
        "施工面清理",
        "季节性施工",
        "夜间施工管理",
        "交叉作业协调",
        "施工工艺改进",
        "新型工艺应用",
    ],
    # 成本管理
    "cost_management": [
        "成本预算编制",
        "成本核算方法",
        "成本控制措施",
        "成本分析报告",
        "成本优化方案",
        "成本考核制度",
        "成本预警机制",
        "成本台账管理",
        "成本审批流程",
        "成本节约措施",
        "成本动态监控",
        "成本偏差分析",
        "成本定额管理",
        "成本报价策略",
        "成本风险防控",
        "成本调整机制",
        "成本复盘总结",
        "成本信息化管理",
        "成本经验积累",
        "成本持续优化",
    ],
    # 合同与法务
    "contract_legal": [
        "合同条款审查",
        "合同签订流程",
        "合同履约管理",
        "变更索赔处理",
        "合同争议解决",
        "保险手续办理",
        "法律风险防控",
        "合规管理制度",
        "知识产权保护",
        "保密协议签订",
        "担保手续办理",
        "税务筹划安排",
        "审计配合工作",
        "法律文件归档",
        "法务咨询机制",
        "纠纷处理预案",
        "仲裁诉讼准备",
        "法律培训教育",
        "合同履约评估",
        "法务风险管理",
    ],
    # 信息化与数字化
    "digitalization": [
        "信息系统规划",
        "数字化平台建设",
        "数据采集标准",
        "数据分析方法",
        "可视化看板搭建",
        "业务流程再造",
        "移动应用开发",
        "云平台部署",
        "系统集成对接",
        "数据安全保障",
        "系统运维管理",
        "信息化培训",
        "数字化转型规划",
        "智慧工地建设",
        "BIM技术应用",
        "物联网设备部署",
        "大数据分析应用",
        "AI智能辅助",
        "数字化成果展示",
        "信息化持续运营",
    ],
    # 施工方案专项（承包范围分部工程对应）
    "construction_scheme": [
        "施工测量放线与精度控制",
        "土方开挖分层开挖工艺",
        "基坑支护结构设计与施工",
        "地基处理与基础浇筑",
        "钢筋工程绑扎与连接工艺",
        "模板工程支设与拆除",
        "混凝土浇筑与养护工艺",
        "砌体工程施工与验收",
        "钢结构吊装与节点连接",
        "屋面防水卷材施工",
        "外墙保温板粘贴施工",
        "装饰装修面层施工工艺",
        "机电管线综合布置",
        "脚手架搭设与拆除方案",
        "施工降排水与降水方案",
        "季节性施工技术措施",
        "预制构件吊装与灌浆工艺",
        "大体积混凝土温控措施",
        "深基坑监测与预警系统",
        "高空作业安全防护技术",
        "施工临时设施搭设规范",
        "特种工序质量检验标准",
        "新材料新工艺应用方案",
        "复杂节点施工工艺优化",
        "预应力张拉与灌浆工艺",
        "焊接工艺评定与无损检测",
        "防水工程细部节点做法",
        "幕墙龙骨安装与面板施工",
        "灌注桩施工与检测工艺",
        "地下室防水施工与试验",
        "施工缝与后浇带处理",
        "构件深化设计与BIM建模",
        "绿色施工与环境保护措施",
        "智慧工地数字化管控",
    ],
}

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


def _extract_l2_domain_keywords(l2_title: str) -> list[str]:
    """从二级目录标题提取领域关键词，用于注入三级标题使其具体化。

    提取的关键词代表 L2 的专业领域/分部工程，将用于丰富 L3 标题，
    使不同 L2 下的 L3 标题具有明确的上下文差异。

    例如：L2="混凝土工程施工方案" → ["混凝土"]
         L2="基坑支护与降水方案" → ["基坑", "降水"]
         L2="砌筑工程施工工艺" → ["砌筑"]
    """
    # 分部工程关键词（按优先级排序）
    _DOMAIN_KEYWORDS = [
        "混凝土",
        "钢筋",
        "模板",
        "砌筑",
        "砌",
        "砌体",
        "防水",
        "保温",
        "装饰",
        "抹灰",
        "涂料",
        "幕墙",
        "门窗",
        "地面",
        "墙面",
        "顶棚",
        "吊顶",
        "土方",
        "基坑",
        "降水",
        "桩基",
        "基础",
        "主体",
        "屋面",
        "外墙",
        "内墙",
        "机电",
        "电气",
        "给排水",
        "暖通",
        "消防",
        "智能化",
        "弱电",
        "园林",
        "绿化",
        "道路",
        "桥梁",
        "隧道",
        "管廊",
        "钢结构",
        "木结构",
        "装配式",
        "预制",
        "脚手架",
        "模板支撑",
        "测量",
        "放线",
        "试验",
        "检测",
        "安全",
        "质量",
        "进度",
        "成本",
        "材料",
        "资源",
        "绿色",
        "环保",
        "节能",
        "低碳",
        "智慧",
        "数字",
        "BIM",
    ]

    found = []
    for kw in _DOMAIN_KEYWORDS:
        if kw in l2_title.strip():
            found.append(kw)
            # 最多取 2 个关键词，避免标题过长
            if len(found) >= 2:
                break

    # 如果没找到分部工程关键词，尝试从标题前半段提取有意义的词
    if not found and l2_title:
        # 取标题核心（去除通用后缀）
        core = _strip_l2_suffix(l2_title)
        # 尝试从核心中提取 2 字以上的实体词
        if len(core) >= 2:
            # 移除通用动词/管理词
            stop_words = {
                "施工",
                "方案",
                "工艺",
                "方法",
                "工程",
                "实施",
                "组织",
                "部署",
                "管理",
                "技术",
                "计划",
                "进度",
                "质量",
                "安全",
            }
            for word in stop_words:
                core = core.replace(word, "")
            if len(core) >= 2:
                found.append(core[:4] if len(core) >= 4 else core)

    return found


def _enrich_l3_title_with_l2(candidate: str, l2_title: str, idx: int, l2_index: int) -> str:
    """将候选三级标题与 L2 领域关键词融合，使其具体化。

    这是解决目录重名/缺乏多样性的核心函数：
    - 不同 L2 章节的 L3 标题即使使用相同模板，也会因为注入了 L2 的领域关键词而不同
    - 例如：模板"质量控制流程" + L2="混凝土工程施工方案" → "混凝土质量控制流程"
    - 例如：模板"安全操作规范" + L2="基坑支护方案" → "基坑安全操作规范"

    Args:
        candidate: 候选的三级标题（来自模板或生成策略）
        l2_title: 父级二级目录标题
        idx: 三级序号（用于轮转注入位置）
        l2_index: 二级序号（用于多样化注入方式）
    """
    domain_keywords = _extract_l2_domain_keywords(l2_title)

    if not domain_keywords:
        return candidate

    # 选取注入的领域关键词
    keyword = domain_keywords[0]

    # 避免注入重复（关键词已在标题中则跳过）
    if keyword in candidate:
        # 尝试第二关键词
        if len(domain_keywords) > 1 and domain_keywords[1] not in candidate:
            keyword = domain_keywords[1]
        else:
            return candidate

    # 注入策略（根据 idx + l2_index 轮转，增加多样性）
    strategy = (idx + l2_index) % 5

    if strategy == 0:
        # 前缀注入："混凝土质量控制流程"
        return f"{keyword}{candidate}"

    elif strategy == 1:
        # 中间注入：通过在核心词后插入领域
        # 找到 candidate 中的第一个语义核心词
        _INJECT_CORE = [
            "质量",
            "安全",
            "进度",
            "成本",
            "技术",
            "施工",
            "管理",
            "控制",
            "体系",
            "机制",
            "流程",
            "标准",
            "规范",
            "组织",
            "制度",
            "措施",
        ]
        for core in _INJECT_CORE:
            if core in candidate:
                pos = candidate.index(core) + len(core)
                # 避免重复
                if pos < len(candidate) and candidate[pos : pos + len(keyword)] != keyword:
                    return f"{candidate[:pos]}{keyword}{candidate[pos:]}"
                break
        # 回退到前缀
        return f"{keyword}{candidate}"

    elif strategy == 2:
        # 介词连接："混凝土工程质量控制流程"
        return f"{keyword}工程{candidate}"

    elif strategy == 3:
        # 叠词模式：适合专业标题
        # 检查 candidate 是否以通用词开头
        _GENERIC_STARTS = [
            "施工",
            "质量",
            "安全",
            "进度",
            "成本",
            "技术",
            "管理",
            "控制",
            "体系",
            "机制",
            "流程",
            "标准",
        ]
        for gs in _GENERIC_STARTS:
            if candidate.startswith(gs):
                return f"{keyword}{gs}{candidate[len(gs):]}"
        return f"{keyword}{candidate}"

    else:  # strategy == 4
        # 复合注入：使用 L2 的核心词 + 候选
        l2_core = _strip_l2_suffix(l2_title)
        if l2_core and len(l2_core) >= 2 and l2_core not in candidate:
            return f"{l2_core}{candidate}"
        return f"{keyword}{candidate}"


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
    from .outline_common import _derive_child_title, _extract_title_core

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
            parent, idx + l2_index * 5 + offset * 3, used_titles, level=3
        )
        if candidate not in used_titles:
            return candidate

    # Step 2（兜底）：L2 核心 + 序号，确保唯一且仍保留 L2 关联
    core = _extract_title_core(l2_title) or (_extract_title_core(l1_title) if l1_title else "")
    core = core or "专项"
    n = idx + 1
    while True:
        candidate = f"{core}专项{n}" if n > 1 else f"{core}实施要点"
        if candidate not in used_titles:
            return candidate
        n += 1


def _generate_generic_l3_title(
    l2_title: str, idx: int, l1_title: str = "", l2_index: int = 0
) -> str:
    """通用三级标题生成策略，确保语义通顺、无重复词、多样化。

    优化：
    - 结合 l1_title 关键词作为上下文补充
    - l2_index 用于模式轮转，避免相邻 L2 生成相同标题
    - 12 种模式 × 10+ 后缀 = 120+ 组合
    """
    # 提取核心词（移除后缀后）
    core = _strip_l2_suffix(l2_title)
    if not core or len(core) < 2:
        core = l2_title

    # 从 core 中提取关键词（用于避免重复）
    core_words = set()
    for word in [
        "体系",
        "组织",
        "制度",
        "流程",
        "机制",
        "标准",
        "规范",
        "管理",
        "质量",
        "安全",
        "技术",
        "施工",
        "方案",
        "措施",
        "计划",
        "进度",
        "资源",
        "项目",
        "责任制",
        "实施",
        "操作",
        "控制",
        "检查",
        "评估",
        "要点",
        "要求",
        "准则",
        "方法",
    ]:
        if word in core:
            core_words.add(word)

    # 从 l1_title 提取上下文关键词
    l1_keywords = set()
    for word in [
        "施工",
        "部署",
        "方案",
        "管理",
        "组织",
        "规划",
        "实施",
        "技术",
        "安全",
        "质量",
        "进度",
        "资源",
        "成本",
        "合同",
    ]:
        if word in l1_title:
            l1_keywords.add(word)

    # 12 种生成模式，每种 10+ 选项
    patterns = [
        # 模式1: 核心 + 实施类后缀
        {
            "type": "core_suffix",
            "suffixes": [
                "建设",
                "完善",
                "规范",
                "落实",
                "执行",
                "实施",
                "推进",
                "深化",
                "加强",
                "强化",
                "提升",
                "优化",
            ],
        },
        # 模式2: 核心 + 流程类后缀
        {
            "type": "core_suffix",
            "suffixes": [
                "流程",
                "步骤",
                "方法",
                "措施",
                "方案",
                "细则",
                "规划",
                "部署",
                "安排",
                "设计",
                "体系",
                "模式",
            ],
        },
        # 模式3: 核心 + 管控类后缀
        {
            "type": "core_suffix",
            "suffixes": [
                "控制",
                "监督",
                "检查",
                "评估",
                "考核",
                "评审",
                "验收",
                "监控",
                "核查",
                "审查",
                "监管",
                "督导",
            ],
        },
        # 模式4: 动作 + 核心
        {
            "type": "action_core",
            "prefixes": [
                "建立",
                "完善",
                "强化",
                "规范",
                "明确",
                "落实",
                "推进",
                "深化",
                "加强",
                "优化",
                "构建",
                "健全",
            ],
        },
        # 模式5: 核心 + 修饰词 + 要点
        {
            "type": "core_keypoints",
            "modifiers": [
                "关键",
                "核心",
                "重要",
                "主要",
                "重点",
                "基本",
                "基础",
                "首要",
                "核心",
                "关键",
                "重要",
                "主要",
            ],
        },
        # 模式6: 核心 + 标准/要求
        {
            "type": "core_standard",
            "suffixes": [
                "标准",
                "要求",
                "规范",
                "指标",
                "条件",
                "参数",
                "尺度",
                "准则",
                "规定",
                "规程",
                "细则",
                "要求",
            ],
        },
        # 模式7: 核心 + 机制/体系
        {
            "type": "core_mechanism",
            "suffixes": [
                "机制",
                "体系",
                "系统",
                "模式",
                "方式",
                "手段",
                "形式",
                "结构",
                "框架",
                "体系",
                "机制",
                "网络",
            ],
        },
        # 模式8: 核心 + 组织/管理
        {
            "type": "core_org",
            "suffixes": [
                "组织",
                "管理",
                "协调",
                "调度",
                "指挥",
                "统筹",
                "安排",
                "布置",
                "部署",
                "分工",
                "管控",
                "督导",
            ],
        },
        # 模式9: 核心 + 专项/专题
        {
            "type": "core_suffix",
            "suffixes": [
                "专项方案",
                "实施细则",
                "操作指南",
                "作业指导",
                "技术交底",
                "培训手册",
                "检查清单",
                "验收标准",
                "预案",
                "总结",
            ],
        },
        # 模式10: 核心 + 分级/分类
        {
            "type": "core_classified",
            "prefixes": [
                "分级",
                "分类",
                "分层",
                "分区域",
                "分阶段",
                "分专业",
                "分模块",
                "分项",
                "分片",
                "分单元",
            ],
        },
        # 模式11: 核心 + 动态/持续
        {
            "type": "core_suffix",
            "suffixes": [
                "动态管理",
                "持续改进",
                "实时监控",
                "定期评估",
                "跟踪落实",
                "反馈调整",
                "循环优化",
                "迭代升级",
                "周期性检查",
                "常态化运行",
            ],
        },
        # 模式12: 核心 + 信息化/数字化
        {
            "type": "core_suffix",
            "suffixes": [
                "信息化管理",
                "数字化监控",
                "智能化管控",
                "在线化管理",
                "可视化分析",
                "数据化决策",
                "自动化执行",
                "标准化流程",
                "集成化管理",
                "协同化运作",
            ],
        },
    ]

    # 用 l2_index 轮转模式，避免相邻 L2 用相同模式
    pattern = patterns[(idx + l2_index * 3) % len(patterns)]

    if pattern["type"] == "core_suffix":
        suffixes = pattern["suffixes"]
        valid_suffixes = [s for s in suffixes if not any(w in s for w in core_words)]
        if not valid_suffixes:
            valid_suffixes = suffixes
        suffix = valid_suffixes[idx % len(valid_suffixes)]
        return f"{core}{suffix}"

    elif pattern["type"] == "action_core":
        prefixes = pattern["prefixes"]
        valid_prefixes = [p for p in prefixes if not any(w in p for w in core_words)]
        if not valid_prefixes:
            valid_prefixes = prefixes
        prefix = valid_prefixes[idx % len(valid_prefixes)]
        return f"{prefix}{core}"

    elif pattern["type"] == "core_keypoints":
        modifiers = pattern["modifiers"]
        modifier = modifiers[idx % len(modifiers)]
        full = f"{core}{modifier}要点"
        # 检查重复词
        for word in core_words:
            if word * 2 in full:
                suffixes = ["建设", "完善", "流程", "措施", "方法", "体系", "机制"]
                valid_suffixes = [s for s in suffixes if s not in core_words]
                suffix = (
                    valid_suffixes[idx % len(valid_suffixes)] if valid_suffixes else suffixes[0]
                )
                return f"{core}{suffix}"
        if f"{modifier}要点" in core:
            suffixes = ["建设", "完善", "流程", "措施", "方法"]
            valid_suffixes = [s for s in suffixes if s not in core_words]
            suffix = valid_suffixes[idx % len(valid_suffixes)] if valid_suffixes else suffixes[0]
            return f"{core}{suffix}"
        return full

    elif pattern["type"] == "core_classified":
        prefixes = pattern["prefixes"]
        prefix = prefixes[idx % len(prefixes)]
        return f"{prefix}{core}"

    elif pattern["type"] == "core_mechanism":
        suffixes = pattern["suffixes"]
        valid_suffixes = [s for s in suffixes if not any(w in s for w in core_words)]
        if not valid_suffixes:
            valid_suffixes = suffixes
        suffix = valid_suffixes[idx % len(valid_suffixes)]
        full = f"{core}{suffix}"
        for word in core_words:
            if word * 2 in full:
                suffix = suffixes[(idx + 1) % len(suffixes)]
                full = f"{core}{suffix}"
                break
        return full

    elif pattern["type"] == "core_org":
        suffixes = pattern["suffixes"]
        valid_suffixes = [s for s in suffixes if not any(w in s for w in core_words)]
        if not valid_suffixes:
            valid_suffixes = suffixes
        suffix = valid_suffixes[idx % len(valid_suffixes)]
        return f"{core}{suffix}"

    else:  # core_standard
        suffixes = pattern["suffixes"]
        valid_suffixes = [s for s in suffixes if not any(w in s for w in core_words)]
        if not valid_suffixes:
            valid_suffixes = suffixes
        suffix = valid_suffixes[idx % len(valid_suffixes)]
        return f"{core}{suffix}"


def _generate_cross_context_l3_title(l1_title: str, l2_title: str, idx: int, l2_index: int) -> str:
    """结合一级和二级标题的交叉上下文生成创新三级标题。

    这是 Step 4 的创新生成策略，通过交叉组合 L1 和 L2 的核心词，
    产生与纯 L2 衍生完全不同的标题，有效避免重复。
    """
    # 提取 L1 核心词
    _strip_l2_suffix(l1_title) if l1_title else ""
    # 提取 L2 核心词
    l2_core = _strip_l2_suffix(l2_title)

    # 从 L1 中提取有区分度的关键词
    l1_distinct_words = set()
    for word in [
        "施工",
        "部署",
        "方案",
        "规划",
        "组织",
        "实施",
        "技术",
        "安全",
        "质量",
        "进度",
        "资源",
        "成本",
        "合同",
        "信息化",
        "数字化",
        "BIM",
        "环保",
        "节能",
        "绿色",
    ]:
        if word in l1_title and word not in l2_title:
            l1_distinct_words.add(word)

    # 交叉组合模式
    cross_patterns = [
        # L1 关键词 + L2 核心 + 后缀
        lambda: f"{_pick_word(l1_distinct_words, idx, l2_index)}{l2_core}专项",
        # L2 核心 + 特色 + 后缀
        lambda: f"{l2_core}特色化建设",
        # L1 关键词 + 专项 + L2 核心
        lambda: f"{_pick_word(l1_distinct_words, idx, l2_index)}专项{l2_core}",
        # L2 核心 + 提升类
        lambda: f"{l2_core}效能提升",
        # L2 核心 + 创新类
        lambda: f"{l2_core}创新实践",
        # L2 核心 + 精细化
        lambda: f"{l2_core}精细化管理",
        # L2 核心 + 标准化
        lambda: f"{l2_core}标准化建设",
        # L2 核心 + 一体化
        lambda: f"{l2_core}一体化推进",
        # L2 核心 + 全过程
        lambda: f"{l2_core}全过程管控",
        # L2 核心 + 全方位
        lambda: f"{l2_core}全方位实施",
    ]

    pattern_idx = (idx + l2_index * 5) % len(cross_patterns)
    return cross_patterns[pattern_idx]()


def _pick_word(word_set: set, idx: int, offset: int) -> str:
    """从集合中选取一个词，如果集合为空则返回默认词。"""
    words = list(word_set)
    if not words:
        defaults = ["综合", "专项", "重点", "核心", "关键", "重要", "主要", "基础"]
        return defaults[(idx + offset) % len(defaults)]
    return words[(idx + offset) % len(words)]


def _strip_l2_suffix(title: str) -> str:
    """移除 L2 标题末尾的常见后缀词，提取核心内容。

    采用多策略提取：
    1. 优先按连接词（的/与/及/和）分割，取最长且有意义的段作为核心
    2. 否则移除末尾的通用动词/形容词后缀
    3. 保护重要名词如"体系""系统""组织"等
    4. 确保核心词至少 4 个字符，信息量充足
    """
    # 保护重要名词列表
    protected_nouns = [
        "体系",
        "系统",
        "组织",
        "机构",
        "队伍",
        "班子",
        "责任制",
        "制度",
        "机制",
        "模式",
        "规划",
        "计划",
        "方案",
        "措施",
        "设计",
        "部署",
        "流程",
        "标准",
        "规范",
        "制度",
        "机制",
        "模式",
    ]

    # 策略1: 按连接词分割（取最长且有意义的段）
    connectors = ["的", "与", "及", "和", "对", "关于", "中", "内"]
    for conn in connectors:
        if conn in title:
            parts = [p.strip() for p in title.split(conn) if p.strip()]
            valid_parts = [p for p in parts if len(p) >= 2]
            if valid_parts:
                best = max(valid_parts, key=len)
                if len(best) >= 3:
                    # 如果分割后的核心词太短（<4字），回退到策略2
                    if len(best) >= 4:
                        return best
                    # 保留更多上下文信息
                    return title  # 返回原标题，不做分割

    # 策略2: 移除末尾的通用动词/形容词后缀
    verb_suffixes = sorted(
        [
            "施工方案",
            "施工方法",
            "施工工艺",
            "专项方案",
            "实施方案",
            "设计方案",
            "保障方案",
            "优化方案",
            "方案",
            "措施",
            "计划",
            "设计",
            "分析",
            "研究",
            "建设",
            "实施",
            "管理",
            "规划",
            "部署",
            "安排",
            "流程",
            "标准",
            "要求",
            "内容",
            "情况",
            "方法",
            "工艺",
            "技术",
            "配置",
            "剖析",
            "概论",
            "概览",
            "介绍",
            "说明",
            "控制",
            "保证",
            "监督",
            "检查",
        ],
        key=len,
        reverse=True,
    )

    result = title
    for sfx in verb_suffixes:
        if result.endswith(sfx) and len(result) - len(sfx) >= 4:
            remaining = result[: -len(sfx)]
            # 检查剥离后是否以保护名词结尾
            is_protected = False
            for pn in protected_nouns:
                if remaining.endswith(pn):
                    is_protected = True
                    break
            if not is_protected:
                result = remaining
                break

    # 确保核心词长度充足（至少4字），否则返回原标题
    if len(result) < 4:
        return title

    return result.strip()


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
    from .outline_common import _derive_child_title, _extract_title_core

    parent = l2_title.strip() if l2_title and l2_title.strip() else (l1_title or "工程实施")

    # 尝试不同的后缀维度组合，直到找到未使用且与 L2 全文不相同的标题
    for offset in range(8):
        candidate = _derive_child_title(
            parent, l3_idx + l2_idx * 5 + offset * 3, used_titles, level=3
        )
        if not candidate or candidate in used_titles:
            continue
        if candidate == l2_title:
            continue  # 完全相同则跳过，继续取其他维度后缀
        return candidate

    # 极端情况：所有派生组合都已使用，以 L2 核心 + 序号保证唯一
    core = _extract_title_core(l2_title) or l2_title or "专项"
    n = l3_idx + 1
    while True:
        candidate = f"{core}专项{n}" if n > 1 else f"{core}实施要点"
        if candidate not in used_titles and candidate != l2_title:
            return candidate
        n += 1


def _build_differentiated_candidates(
    l2_title: str,
    l2_core: str,
    l3_idx: int,
    l2_idx: int,
    l1_title: str,
) -> list[str]:
    """构建一组差异化的 L3 候选标题。

    关键原则：候选标题严禁复用 L2 核心词干。
    策略：跨场景模板 + 施工工艺模板 + 前缀/后缀组合。
    """
    candidates: list[str] = []
    core_chars = set(l2_core) if l2_core else set()
    core_len = len(core_chars)

    # 提取 L1 中的区分度关键词（排除已出现在 L2 中的）
    l1_distinct: list[str] = []
    for kw in [
        "施工",
        "部署",
        "方案",
        "规划",
        "组织",
        "实施",
        "技术",
        "安全",
        "质量",
        "进度",
        "资源",
        "成本",
        "合同",
        "信息化",
        "数字化",
        "BIM",
        "环保",
        "节能",
        "绿色",
        "装饰",
        "机电",
        "砌筑",
        "防水",
        "保温",
        "幕墙",
        "智能化",
        "消防",
    ]:
        if kw in l1_title and kw not in l2_title:
            l1_distinct.append(kw)

    # 1. 跨场景模板：优先从与 L2 不匹配的场景中选取
    #    这确保候选标题与 L2 核心天然不重叠
    scene_match = _match_scene_for_diff(l2_title)
    for scene_name, templates in _L3_SCENE_TEMPLATES.items():
        if scene_name == scene_match:
            continue  # 跳过匹配场景（其模板可能与 L2 核心重叠）
        for template in templates:
            cand_chars = set(template)
            overlap = core_chars & cand_chars
            # 允许最多 50% 字符重叠用于候选池（后续严格筛选）
            if core_len == 0 or len(overlap) <= core_len * 0.5:
                candidates.append(template)

    # 2. 若跨场景模板不足，再从匹配场景中选取重叠最少的
    if scene_match and scene_match in _L3_SCENE_TEMPLATES:
        for template in _L3_SCENE_TEMPLATES[scene_match]:
            cand_chars = set(template)
            overlap = core_chars & cand_chars
            # 仅保留重叠 <= 25% 的
            if core_len == 0 or len(overlap) <= core_len * 0.25:
                candidates.append(template)

    # 3. 从 _L2_TITLE_PATTERNS 中选取（跨类别确保差异化）
    #    场景名与模板类别名映射
    _SCENE_TO_TEMPLATE_CAT = {
        "quality_system": "quality",
        "safety_system": "safety",
        "progress": "schedule",
        "technical": "construction",
        "resource": "resource",
        "project": "coordination",
        "construction_method": "construction",
        "cost_management": "analysis",
        "contract_legal": "analysis",
        "digitalization": "analysis",
        "emergency": "emergency",
    }
    matched_cat = _SCENE_TO_TEMPLATE_CAT.get(scene_match, "") if scene_match else ""
    from .workflows_outline_utils import _L2_TITLE_PATTERNS

    for cat_name, templates in _L2_TITLE_PATTERNS.items():
        if cat_name == matched_cat:
            continue  # 跳过匹配类别
        for template in templates:
            cand_chars = set(template)
            overlap = core_chars & cand_chars
            if core_len == 0 or len(overlap) <= core_len * 0.5:
                candidates.append(template)

    # 4. 前缀 + 后缀组合（不含 L2 核心）
    prefixes = _L3_DIFF_SPECIFIC_PREFIXES
    suffixes = _L3_DIFF_SPECIFIC_SUFFIXES
    for i in range(min(8, len(prefixes))):
        p = prefixes[(l3_idx + l2_idx + i) % len(prefixes)]
        s = suffixes[(l3_idx + i * 2) % len(suffixes)]
        candidate = f"{p}{s}"
        cand_chars = set(candidate)
        overlap = core_chars & cand_chars
        if core_len == 0 or len(overlap) <= core_len * 0.3:
            candidates.append(candidate)

    # 5. L1 区分词 + 前缀 + 后缀
    for i, l1kw in enumerate(l1_distinct[:3]):
        p = prefixes[(l3_idx + l2_idx + i + 3) % len(prefixes)]
        s = suffixes[(l3_idx + i) % len(suffixes)]
        candidate = f"{l1kw}{p}{s}"
        cand_chars = set(candidate)
        overlap = core_chars & cand_chars
        if core_len == 0 or len(overlap) <= core_len * 0.3:
            candidates.append(candidate)

    # 6. 通用施工后缀变体
    variant_suffixes = [
        "专项施工方案",
        "核心工艺要点",
        "关键工序操作",
        "重点管控措施",
        "细化实施细则",
        "具体操作规范",
        "分步实施流程",
        "分项管控标准",
    ]
    for i, vs in enumerate(variant_suffixes):
        candidate = prefixes[(l3_idx + i) % len(prefixes)] + vs
        cand_chars = set(candidate)
        overlap = core_chars & cand_chars
        if core_len == 0 or len(overlap) <= core_len * 0.3:
            candidates.append(candidate)

    return candidates


def _match_scene_for_diff(l2_title: str) -> str | None:
    """根据 L2 标题匹配场景模板。"""
    for keywords, scene_name in _L2_KEYWORD_TO_SCENE:
        for kw in keywords:
            if kw in l2_title:
                return scene_name
    return None


async def _generate_level3_v51(
    provider: Any,
    level1: list[dict],
    summary: str,
    sp_text: str,
    phase_callback: Callable | None = None,
    l2_done_queue: asyncio.Queue | None = None,
    project_info: dict | None = None,
) -> list[dict]:
    """L3：流水线生成三级目录。

    ⚠️ 说明（2026-09-14 排查）：本函数**完全不调用 AI**，`provider` 参数保留仅为
    接口兼容。三级标题为空时由 `_generate_diverse_l3_title()` 按规则兜底生成，
    因此 L3 阶段**不存在可注入全局事实的提示词入口**——要注入就必须把 L3 重新
    改成 AI 驱动，代价是当前"零耗时、零成本"的 L3 会变成 N 次 AI 调用。
    事实约束目前覆盖 L1（_generate_level1_v51）与 L2（_fill_level2_with_ai），
    如需 L3 也受约束，应改走"事实校验/后处理"而非"提示词注入"。

    在 L2 阶段已经预构建了包含三级节点的骨架，此处 AI 仅需填充三级标题。
    流水线模式：L2 完成一个章节，立即开始该章节的 L3 生成。
    三级默认标题根据二级目录名称多样化生成。

    BUG 修复：添加完整的类型检查，防止非 dict 元素导致 AttributeError。
    """
    if phase_callback:
        await phase_callback("V5.1三级生成", "流水线生成三级目录")

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

    # L2/L3 差异化后处理：检测并修正与父级 L2 标题类同的 L3 标题
    diff_fixed = _ensure_l2_l3_differentiation(level1)
    if diff_fixed > 0:
        logger.info("V5.1 L2/L3 差异化修正: 修正 %d 个三级标题", diff_fixed)
        l3_filled += diff_fixed

    # 进度回调
    if phase_callback:
        await phase_callback(
            "V5.1三级生成", f"三级目录生成完成 {l3_filled}/{l3_total}（差异化修正 {diff_fixed}）"
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
) -> list[dict]:
    """V5.1 自检：本地修复 + 目录门禁（确定性事实 + AI 语义审）。

    ENHANCE 2026-09-14：自检阶段接入目录门禁。门禁只产出结论不做阻断，
    结论写入 `v51_state.gate_report` 并通过 phase_callback 上报给前端。
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

    # 2.2 AI 语义审（失败只降级，不阻断）
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

    # 检查标题唯一性
    all_titles: list[tuple[str, str]] = []  # (title, path)
    for l1 in level1:
        if not isinstance(l1, dict):
            continue
        l1_id = l1.get("id", "")
        l1_children = l1.get("children", [])
        all_titles.append((l1.get("title", ""), l1_id))
        if not isinstance(l1_children, list):
            continue
        for l2 in l1_children:
            if not isinstance(l2, dict):
                continue
            l2_id = l2.get("id", "")
            l2_children = l2.get("children", [])
            all_titles.append((l2.get("title", ""), f"{l1_id}/{l2_id}"))
            if not isinstance(l2_children, list):
                continue
            for l3 in l2_children:
                if not isinstance(l3, dict):
                    continue
                all_titles.append((l3.get("title", ""), f"{l1_id}/{l2_id}/{l3.get('id', '')}"))

    seen: dict[str, str] = {}
    for title, path in all_titles:
        t = title.strip()
        if not t:
            issues.append(f"空标题: {path}")
            continue
        if t in seen:
            issues.append(f"重复标题 '{t}': {seen[t]} 和 {path}")
        seen[t] = path

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
        """收集目录树中所有现有标题，用于检测后缀冲突。"""
        titles: set[str] = set()
        for l1 in level1:
            if not isinstance(l1, dict):
                continue
            titles.add(l1.get("title", ""))
            for l2 in l1.get("children", []) or []:
                if not isinstance(l2, dict):
                    continue
                titles.add(l2.get("title", ""))
                for l3 in l2.get("children", []) or []:
                    if not isinstance(l3, dict):
                        continue
                    titles.add(l3.get("title", ""))
        return titles

    _CN_NUMS = ["二", "三", "四", "五", "六", "七", "八", "九", "十"]

    def _next_unique_title(base: str, existing: set[str]) -> str:
        """生成下一个不冲突的带后缀标题：base（二）、base（三）..."""
        for num in _CN_NUMS:
            candidate = f"{base}（{num}）"
            if candidate not in existing:
                existing.add(candidate)
                return candidate
        # 超过10个重复——用数字后缀
        for i in range(11, 100):
            candidate = f"{base}（{i}）"
            if candidate not in existing:
                existing.add(candidate)
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

        # 修复空标题 (格式: "空标题: 1/2/3")
        if "空标题" in issue:
            # BUG 修复: 正确匹配 "空标题: <path>" 格式
            path_match = re.search(r"空标题:\s*(.+)", issue)
            if path_match:
                path = path_match.group(1).strip()
                parts = path.split("/")
                try:
                    # BUG-FIX-12: 使用 _path_to_index 替代 int() 解析点号ID格式
                    # 原实现 int("1.1") 抛 ValueError，导致 L2/L3 空标题修复完全失效
                    if len(parts) == 1:
                        idx = _path_to_index(parts[0])
                        if 0 <= idx < len(level1) and isinstance(level1[idx], dict):
                            level1[idx]["title"] = f"章节{idx + 1}"
                    elif len(parts) == 2:
                        i, j = _path_to_index(parts[0]), _path_to_index(parts[1])
                        if 0 <= i < len(level1) and isinstance(level1[i], dict):
                            children = level1[i].get("children", [])
                            if (
                                isinstance(children, list)
                                and 0 <= j < len(children)
                                and isinstance(children[j], dict)
                            ):
                                children[j]["title"] = f"子章节{j + 1}"
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
                                    l2_children[k]["title"] = f"内容{k + 1}"
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
                                _l3_title = _derive_child_title(_l2_title, _j, used_l3, level=3)
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
                            for _j in range(3):
                                _l3_title = _derive_child_title(
                                    l2_title or l1_title, _j, used_l3, level=3
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
            max_tokens=5000,
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
) -> list[dict] | None:
    """等待人工审核（支持 auto_confirm 跳过）。

    Args:
        outline_queue: SSE 事件队列
        v51_state: V5.1 任务状态（含 resume_event）
        phase: 当前阶段（level1/level2/level3/final）
        outline: 待审核的目录数据
        auto_confirm: 是否自动确认（跳过人工审核）
        timeout: 审核超时秒数，默认 1 小时
    """
    if auto_confirm:
        # 自动确认：发送事件后直接返回当前目录
        logger.info("V5.1 %s 自动确认（auto_confirm=True）", phase)
        await outline_queue.put(
            {
                "status": "review_auto_confirm",
                "phase": phase,
                "outline": outline,
                "message": f"{phase} 自动确认通过",
            }
        )
        return outline

    # 发送审核事件
    await outline_queue.put(
        {
            "status": "review_needed",
            "phase": phase,
            "outline": outline,
            "message": f"请审核 {phase} 目录，确认后继续生成",
        }
    )

    # 等待审核结果（使用 resume_event）
    try:
        # BUG-FIX-76: 审核前清除上次残留的 set 状态，否则第二次 wait() 会立即返回
        v51_state.resume_event.clear()
        # BUG-FIX-EDITED-STALE: 审核前清空上一阶段的 edited_outline，
        # 防止用户在本阶段"只确认不编辑"时，旧阶段的编辑结果覆盖本阶段新生成的目录
        v51_state.edited_outline = None
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
) -> list[dict]:
    """V5.1 目录生成——基于设计文档的预构建骨架模式。

    工作流（严格遵循《标书智能体（二）》设计文档）：
    1. L1：AI 根据评分要求生成一级目录标题（rating_item → new_title）
    2. 人工审核：一级目录（可跳过）
    3. L2：代码预构建 JSON 骨架 → AI 并发填充二三级标题
    4. 人工审核：二级目录（可跳过）
    5. L3：流水线模式检查三级节点完整性
    6. AI 自检 + 最终审核

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
    """
    _t_start = time.monotonic()
    _t_phase = time.monotonic()
    project_info = project_info or {}

    logger.info("[V5.1] 开始: summary_len=%d, scoring_len=%d", len(summary), len(scoring_points))

    # 解析评分点
    try:
        sp_parsed = json.loads(scoring_points) if scoring_points else []
    except Exception as _sp_e:
        # P2 修复：原静默置空继续生成，用户无任何感知。评分点解析失败意味着
        # L1 生成将缺少评分覆盖校验，目录可能与招标要求严重脱节。
        logger.error("V5.1 评分点 JSON 解析失败，将以空评分继续: %s", _sp_e)
        sp_parsed = []
    # BUG-FIX-SP-TRUNCATE：原实现 json.dumps 后硬截断 [:5000]，超长评分点会被
    # 截成非法 JSON，下游 _generate_level1_v51 的 json.loads 解析失败导致
    # score_item_count=0，评分项数量覆盖校验被整体跳过。
    # 改为按条目智能截断，保证输出始终是合法 JSON。
    sp_text = _truncate_scoring_json(sp_parsed, max_chars=5000) if sp_parsed else ""

    # 构建参考模板
    reference_json = json.dumps(REFERENCE_TEMPLATE_V51, ensure_ascii=False)

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
            outline_queue, v51_state, "level1", level1, auto_confirm=auto_confirm
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
            outline_queue, v51_state, "level2", level1, auto_confirm=auto_confirm
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

    # ===== AI 自检 =====
    # BUG-FIX-48: 自检前取消检查
    if v51_state and getattr(v51_state, "cancelled", False):
        logger.info("V5.1 用户已取消（自检前），中止生成")
        return []

    _t_phase = time.monotonic()
    level1 = await _self_check_v51(
        provider,
        level1,
        _t_start,
        phase_callback=phase_callback,
        v51_state=v51_state,
        project_info=project_info,
        scoring_points=scoring_points,
        word_budget_text="\n".join(
            part for part in (scoring_points, tender_context, project_scope) if part
        ),
    )
    logger.info(
        "[V5.1-PHASE] AI 自检阶段耗时: %.1fs", time.monotonic() - _t_phase
    )

    # ===== 最终审核 =====
    if v51_state and outline_queue is not None:
        # BUG-FIX-PHASE-01：更新阶段状态
        v51_state.current_phase = "final"
        outline_for_review = v51_state.edited_outline or level1
        edited = await _wait_for_v51_review(
            outline_queue, v51_state, "final", outline_for_review, auto_confirm=auto_confirm
        )
        if edited is None:
            logger.info("V5.1 最终审核未通过，中止")
            return []
        level1 = edited

    # ===== 返回最终结果 =====
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

