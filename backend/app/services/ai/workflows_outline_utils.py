"""workflows 子模块：workflows_outline_utils（由 scripts/split_workflows.py 自动生成，勿手改）"""

import asyncio
from collections import OrderedDict
import copy
import json
import logging
import os
import re
from typing import Any

from .prompts import get_prompt, render_prompt
from .providers.base import AIMessage


# BUG 修复（循环导入）：_L3_GENERIC_PATTERNS 和 _L3_PROJECT_SPECIFIC_KEYWORDS
# 原定义在已退役的 v2v3 模块中，曾因循环导入导致
# L3 具体性校验静默降级。现已直接定义在本模块。L3 具体性校验
# 一直降级为仅长度检查。现将常量定义移到本模块（唯一使用方），
# v2v3 中已删除原定义。
_L3_GENERIC_PATTERNS: tuple[str, ...] = (
    # 固定通用占位词（必须检测）
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
    "日常管理",
    "制度管理",
    "安全保障",
    "组织保障",
    "技术保障",
    "资源保障",
    "应对措施",
    "总体思路",
    "基本内容",
    "核心内容",
    "工作要点",
    "主要措施",
    "关键内容",
    "实施内容",
    "整体方案",
    "总体规划",
    "总体部署",
    "总体安排",
    "总体要求",
    "通用方案",
    "通用技术",
    "通用措施",
    "通用管理",
    "通用方法",
    "通用手段",
    "通用方式",
    "通用流程",
    "通用步骤",
    "通用规定",
    "通用规范",
    "通用标准",
    "通用条件",
    "通用原则",
    "通用制度",
    "通用规则",
    "通用准则",
    "通用条款",
    "通用条目",
    "通用事项",
    "通用内容",
    "通用部分",
    "通用环节",
    "通用阶段",
    "通用过程",
    "通用程序",
    "常规做法",
    "标准做法",
    "常规要求",
    "技术要点",
    "工作内容",
    "基本思路",
    # 纯项目级通用标题（不含具体工程信息）
    "项目概述",
    "项目概况",
    "项目简介",
    "项目背景",
    "项目意义",
    "项目定位",
    "项目管理",
    "项目组织",
    "项目协调",
    "项目沟通",
    "项目风险",
    "项目进度",
    "项目质量",
    "项目安全",
    "项目成本",
    "项目资源",
    "项目材料",
    "项目设备",
    "项目人员",
    "项目配置",
    "项目计划",
    "项目安排",
)

_L3_PROJECT_SPECIFIC_KEYWORDS: tuple[str, ...] = (
    # 工程部位
    "外墙",
    "屋面",
    "地下室",
    "基坑",
    "基础",
    "主体",
    "结构",
    "装饰",
    "楼面",
    "墙面",
    "地面",
    "门窗",
    "防水",
    "保温",
    "幕墙",
    "砌体",
    # 材料/构件
    "混凝土",
    "钢筋",
    "模板",
    "砌块",
    "钢材",
    "防水卷材",
    "保温板",
    "钢结构",
    "预制",
    "PC构件",
    "涂料",
    "石材",
    # 工艺/工法
    "浇筑",
    "绑扎",
    "焊接",
    "吊装",
    "砌筑",
    "抹灰",
    "涂装",
    "铺设",
    "开挖",
    "支护",
    "降水",
    "监测",
    "加固",
    "预应力",
    "拼装",
    # 安全/管理专项
    "临边",
    "洞口",
    "高空",
    "触电",
    "火灾",
    "坍塌",
    "中毒",
    "坠落",
    # 设备/机械
    "塔吊",
    "电梯",
    "泵车",
    "挖掘机",
    "起重机",
    "脚手架",
    # BIM/技术
    "BIM",
    "碰撞",
    "深化",
)




logger = logging.getLogger(__name__)


from .workflows_base import with_timeout


_CHINESE_TO_ARABIC = {
    "一": 1,
    "二": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
    "十一": 11,
    "十二": 12,
    "十三": 13,
    "十四": 14,
    "十五": 15,
    "十六": 16,
    "十七": 17,
    "十八": 18,
    "十九": 19,
    "二十": 20,
    "二十一": 21,
    "二十二": 22,
    "二十三": 23,
    "二十四": 24,
    "二十五": 25,
    "二十六": 26,
    "二十七": 27,
    "二十八": 28,
    "二十九": 29,
    "三十": 30,
}


def _flatten_outline_tree(
    nodes: list[dict], parent_id: int | None = None, level: int = 1
) -> list[dict]:
    """将嵌套目录树展平为扁平的列表，用于存入数据库

    遗留修复（层级完整率）：`level` 原为 `node.get("level", 1)`，盲信 AI/前端
    传入的层级字段——L2 节点被标 level=5 会直接入库，且缺省值恒为 1（无论实际
    处在第几层），与落库主链路 `_save_nodes`「按父子关系计算层级」的口径不一致。
    改为按递归深度计算，与 `_save_nodes` 同一份契约；新增的 `level` 参数带默认值，
    既有调用方无需改动（向后兼容）。
    """
    flat = []
    _next_id = [0]  # 使用列表以便在嵌套递归中可变引用
    for i, node in enumerate(nodes):
        # BUG 修复：若 node 不是 dict，node.get() 会 AttributeError
        if not isinstance(node, dict):
            continue
        _next_id[0] += 1
        temp_id = _next_id[0]
        item = {
            "_temp_id": temp_id,
            "parent_id": parent_id,
            "title": node.get("title", ""),
            # 层级由实际递归深度决定，不采信节点自带 level（口径同 _save_nodes）
            "level": level,
            "sort_order": i + 1,
            "section_type": "text",
            "scoring_point_id": node.get("scoring_point_id"),
        }
        flat.append(item)
        # BUG-FIX 2026-10-08（目录树错误 · children 非 list 静默丢子树）：
        # 原实现只判 `if children:`。AI / 直出 JSON 偶尔把 children 序列化成
        # **对象**（`{"0": {...}, "1": {...}}`，部分模型对纯数组会这么吐），
        # 此时 dict 为真值 → 递归进去迭代出的是 **字符串 key**，
        # 被上面的 `isinstance(node, dict)` 全量 continue 掉，
        # 结果该节点**整棵子树无声消失**（不报错、无日志），落库后目录缺章。
        # 改为严格要求 list；非 list 且非空时告警留痕，便于事后追责。
        children = node.get("children")
        if children and not isinstance(children, list):
            logger.warning(
                "目录节点 children 类型非法（期望 list，实际 %s），已忽略其子树: %s",
                type(children).__name__,
                item.get("title", ""),
            )
        if isinstance(children, list) and children:
            flat.extend(_flatten_outline_tree(children, temp_id, level + 1))
    return flat


def _renumber_items(items: list[dict], parent_number: str = "") -> list[dict]:
    """为目录项附加 number 字段（用于前端展示和导出），**绝不修改纯文本 title**

    关键设计（依据 V2.1/V3.0 文档）：
      - title 保持纯文本（与 28 章模板格式完全一致），不附加"1."、"1.1."等数字前缀
      - 仅在 number 字段中保存路径式编号（如 "1"、"1.2"、"1.2.3"），供前端按需展示
      - 前端展示时由展示层根据层级 + number 渲染"第一章/1.1/a"等格式
      - DOCX 导出时由导出器统一根据层级 + 路径重新生成 V2.1 标准编号
    """
    result = []
    for i, item in enumerate(items):
        # BUG 修复：若 item 不是 dict，dict(item) 会 TypeError
        if not isinstance(item, dict):
            continue
        num = f"{parent_number}{i + 1}" if parent_number else str(i + 1)
        new_item = dict(item)  # 不修改原对象
        new_item["number"] = num
        # 保持 title 纯文本，不附加"1."、"1.1."、"1.2.3."等数字前缀
        # BUG-FIX 2026-10-08（编号冲突/目录树错误）：同 _flatten_outline_tree，
        # children 为 dict（真值）时原实现递归后被 isinstance 守卫全量跳过，
        # 子树连同其编号一起消失；现严格要求 list 并对异常形态留痕。
        children = new_item.get("children")
        if children and not isinstance(children, list):
            logger.warning(
                "目录节点 children 类型非法（期望 list，实际 %s），其子树编号已跳过: %s",
                type(children).__name__,
                new_item.get("title", ""),
            )
        if isinstance(children, list) and children:
            new_item["children"] = _renumber_items(children, f"{num}.")
        result.append(new_item)
    return result


def normalize_outline_items(items: list[dict], default_level: int = 1) -> list[dict]:
    """归一化目录项字段：AI 可能返回 name/heading 等字段名，统一为 title

    同时过滤掉 AI 错误响应（含 error/message 字段且无有效标题的字典）
    以及空标题节点（一级、二级、三级）
    确保每个节点都有正确的 level 字段
    V3.7 修复：添加同级别标题去重，避免同一层级出现重复标题
    V4.0 增强：剥离标题中 AI 误带的数字/编号前缀（编号由系统自动生成，
    标题中严禁出现数字前缀），并在剥离后重新去重，避免"1、xxx"与"xxx"并存
    BUG-O-03 修复：将递归实现改为迭代（栈式 DFS 遍历），彻底消除 RecursionError 风险
    """
    result: list[dict] = []

    # BUG-FIX 2026-10-08（空数据容错）：落库链路会传 `outline or []`，但直出/
    # 直调路径可能传 None。原实现 `len(current_items)` 对 None 抛
    # TypeError → 整个目录归一化 500。现统一降级为空结果。
    if items is None:
        return result
    if not isinstance(items, list):
        if isinstance(items, dict):
            items = [items]
        else:
            logger.warning(
                "归一化：入参类型非法（期望 list，实际 %s），已按空目录处理", type(items).__name__
            )
            return result

    # 使用显式栈实现 DFS 遍历：每个栈帧包含 (items, level, parent_list, index)
    # index 跟踪当前处理位置，确保先处理子节点再处理兄弟节点（与递归一致）
    # 完全避免 Python 递归深度限制（默认 1000），支持任意深度的目录树
    stack: list[tuple[list[dict], int, list[dict], int]] = [(items, default_level, result, 0)]

    while stack:
        current_items, level, parent_list, idx = stack[-1]

        if idx >= len(current_items):
            # 当前层级所有项已处理完毕，弹出栈
            stack.pop()
            continue

        # 更新当前栈帧的索引（指向下一个待处理项）
        stack[-1] = (current_items, level, parent_list, idx + 1)
        item = current_items[idx]

        if not isinstance(item, dict):
            continue

        # 过滤 AI 错误响应
        if "error" in item and not (item.get("title") or item.get("name") or item.get("heading")):
            logger.warning("过滤 AI 错误响应: %s", item.get("error", ""))
            continue

        # 归一化标题字段名
        normalized = dict(item)
        if not normalized.get("title"):
            for alt in ("name", "heading", "section", "chapter", "label"):
                if normalized.get(alt):
                    normalized["title"] = normalized[alt]
                    break

        # 过滤空标题节点
        title = normalized.get("title", "").strip()
        if not title:
            logger.warning("跳过空标题节点: %s", normalized)
            continue

        # V4.0 增强：剥离标题中误带的编号前缀
        title = _strip_title_number_prefix(title)
        if not title:
            logger.warning("剥离编号前缀后标题为空，跳过节点: %s", normalized)
            continue
        normalized["title"] = title

        # 始终使用迭代计算的 level，避免 AI 返回错误的 level 值
        normalized["level"] = level

        # V3.7 同级别标题去重：基于父列表已有标题
        # 收集当前 parent_list 中已有的标题用于去重
        seen_titles_in_parent = {n.get("title", "") for n in parent_list if isinstance(n, dict)}
        if title in seen_titles_in_parent:
            logger.warning("V3.7 去重: 跳过重复标题「%s」（level=%d）", title, level)
            continue

        # 将当前节点添加到父列表
        parent_list.append(normalized)

        # 如果有子节点，压入栈中先处理子节点（DFS：深度优先）
        children = normalized.get("children")
        # BUG-FIX 2026-10-08（目录树错误 · 归一化收口）：原实现对非 list 的
        # children 只是「不进 if」，却把原值原样留在 normalized["children"] 里
        # 继续向下游传。于是 dict 形态的 children 会一路穿过本函数 →
        # sse_db_ops._save_nodes 的 `for sort_order, node in enumerate(nodes)`
        # 迭代出的是字符串 key → node.get(...) 抛 AttributeError → 整条目录
        # 生成落库链路 500（而不是像 _flatten_outline_tree 那样静默丢子树）。
        # 归一化的职责就是消灭形态差异：非 list 一律收敛为 [] 并留痕。
        if "children" in normalized and not isinstance(children, list):
            if children:
                logger.warning(
                    "归一化：节点 children 类型非法（期望 list，实际 %s），已置空: %s",
                    type(children).__name__,
                    normalized.get("title", ""),
                )
            normalized["children"] = []
            children = []
        if isinstance(children, list) and children:
            # 创建新的子列表用于存储处理后的子节点
            child_result: list[dict] = []
            normalized["children"] = child_result
            # 子节点级别+1，压入栈等待处理（先于同级兄弟节点）
            stack.append((children, level + 1, child_result, 0))

    return result


_TITLE_NUMBER_PREFIX_PATTERNS = [
    # FIX-2026-10-06：中文字符集与 heading_standard.HEADING_REGEX_PATTERNS 对齐（补"零"）
    re.compile(r"^第[一二三四五六七八九十百千万零\d]+[章节篇部][、，]?\s*"),

    # BUG-O-04 修复：多级编号允许空格结尾（之前只匹配顿号结尾）
    # 1.1.1 施工方案 或 1.1.1、施工方案
    re.compile(r"^\d+(\.\d+){2,}\s*[、．.\s]?\s*"),
    # 1.1 施工方案 或 1.1、施工方案
    re.compile(r"^\d+\.\d+\s*[、．.\s]?\s*"),

    re.compile(r"^\d+\s*[、．.]\s*"),
    re.compile(r"^[（(]\s*[一二三四五六七八九十百千零\d]+\s*[)）]\s*"),
    re.compile(r"^\d+\s*[)）]\s*"),
    re.compile(r"^[（(]\s*\d+\s*[)）]\s*"),
    re.compile(r"^[a-z]\s*[、．.]\s*"),
    re.compile(r"^[一二三四五六七八九十百千零]+\s*[)）]\s*[、．.]\s*"),

    # BUG-O-04 修复：中文编号添加负面先行断言
    # 只有顿号后不是另一个中文编号开头时才剥离，避免误剥「一、二、三级控制」等正文
    re.compile(r"^[一二三四五六七八九十百千零]+[、．.]\s*(?![一二三四五六七八九十百千零]+[、，])"),
]


def _strip_title_number_prefix(title: str) -> str:
    """剥离标题文本开头已有的编号前缀（第一章、1、1.1、一、1）等），保留纯文本标题

    BUG-O-04 修复：
    1. 多级编号正则允许空格结尾（1.1 施工方案），之前只匹配顿号结尾
    2. 中文编号添加负面先行断言，避免误剥「一、二、三级控制」等正文内容
    """
    if not title:
        return title
    text = title.strip()
    for pat in _TITLE_NUMBER_PREFIX_PATTERNS:
        text = pat.sub("", text).strip()
    return text.strip()


def _dedupe_outline_titles(outline: list[dict]) -> list[dict]:
    """全树标题去重（V3.8 修复）：确保整个目录树中不存在完全相同的标题。

    normalize_outline_items 仅做「同父去重」，无法消除不同父节点下的重复标题，
    例如两个三级节点下各有一个「多栋楼同时施工平面协调」四级节点，
    导出后会出现「1、多栋楼同时施工平面协调」「1、多栋楼同时施工平面协调」的重复条目。

    本函数维护全局已见标题集合（归一化后），重复的后续节点直接丢弃。
    建议在 normalize_outline_items 之后、_ensure_min_children 之前调用，
    若因此导致某父节点子节点数不足，_ensure_min_children 会用模板标题兜底补全。

    Args:
        outline: 目录树（列表）
    Returns:
        去重后的目录树
    """
    seen: set[str] = set()

    def _walk(nodes: list[dict]) -> list[dict]:
        result = []
        for node in nodes:
            if not isinstance(node, dict):
                continue
            title = node.get("title", "")
            key = _normalize_title_core(title)
            if key and key in seen:
                logger.warning(
                    "V3.8 全树去重: 跳过重复标题「%s」（level=%s）", title, node.get("level", "")
                )
                continue
            if key:
                seen.add(key)
            children = node.get("children")
            if isinstance(children, list):
                node["children"] = _walk(children)
            result.append(node)
        return result

    return _walk(outline)


def validate_outline_business(outline: list[dict]) -> tuple[bool, str]:
    """业务校验目录（三层结构、标题非空、编号正确性）

    V4.0 增强：
    1. 全局重复标题检测（跨章节、跨层级，剥离编号前缀后比较）
    2. 标题数字前缀残留检测（编号由系统自动生成，标题中严禁出现数字前缀）
    3. 最小节点数校验：每个一级 ≥2 个二级，每个二级 ≥2 个三级

    BUG-OUTLINE-VALIDATE 修复：原代码硬依赖节点有 level 字段
    （item.get("level", 0) → 默认 0 → 0 != 1 → 拒绝所有合法目录）。
    改为：
    - 根节点天然 level=1（不再硬性要求显式 level 字段）
    - 子节点 level 由树深度自动推导（父级+1）
    - 若节点有显式 level 字段，则额外校验其与推导值一致
    - 兼容显式 level 场景（测试用例、normalize 后）
      和隐式 level 场景（AI 原始返回、调用链路中的早期阶段）
    """
    if not isinstance(outline, list) or len(outline) == 0:
        return False, "目录为空"

    seen_titles: set[str] = set()  # V4.0 全局标题去重

    def _check_node(node: dict, path: str) -> str | None:
        """校验单个节点，返回错误信息或 None"""
        title = str(node.get("title", "") or "").strip()
        if not title:
            return f"{path} 标题为空"
        # V4.0 数字前缀残留检测
        stripped = _strip_title_number_prefix(title)
        if stripped != title:
            return f"{path} 标题含编号前缀「{title}」，编号应由系统自动生成"
        # V4.0 全局重复标题检测（剥离编号后比较，避免误报）
        core = _normalize_title_core(stripped)
        if core in seen_titles:
            return f"{path} 标题与目录中其他章节重复: {title}"
        seen_titles.add(core)
        return None

    def _consistency_check(node: dict, derived_level: int, path: str) -> str | None:
        """若节点带显式 level 字段，校验其与推导的树深度一致"""
        if "level" not in node:
            return None
        explicit = node["level"]
        if explicit is not None and explicit != derived_level:
            return f"{path} 显式 level={explicit} 与树深度推导 level={derived_level} 不一致"
        return None

    for i, item in enumerate(outline):
        # BUG 修复：若 item 不是 dict，item.get() 会 AttributeError
        if not isinstance(item, dict):
            continue
        title = item.get("title", "")
        if not title or not str(title).strip():
            return False, f"第 {i + 1} 项标题为空"

        # 根节点天然 level=1，若有显式 level 则做一致性校验
        err = _consistency_check(item, 1, f"第 {i + 1} 项")
        if err:
            logger.warning("[目录校验] %s（非致命，继续校验）", err)

        # V4.0 一级标题校验（前缀/全局重复）
        err = _check_node(item, f"第 {i + 1} 项")
        if err:
            return False, err

        children = item.get("children", [])
        if not isinstance(children, list):
            children = []

        # V4.0 最小节点数：每个一级 ≥2 个二级
        if len(children) < 2:
            return False, f"第 {i + 1} 项二级目录不足 2 个（实际 {len(children)} 个）"

        for j, child in enumerate(children):
            # BUG 修复：若 child 不是 dict，跳过避免 AttributeError
            if not isinstance(child, dict):
                continue

            # 二级节点：树深度推导 level=2
            err = _consistency_check(child, 2, f"第 {i + 1}.{j + 1} 项")
            if err:
                logger.warning("[目录校验] %s（非致命，继续校验）", err)

            # V4.0 二级标题校验
            err = _check_node(child, f"第 {i + 1}.{j + 1} 项")
            if err:
                return False, err

            grandchildren = child.get("children", [])
            if not isinstance(grandchildren, list):
                grandchildren = []

            # V4.0 最小节点数：每个二级 ≥2 个三级
            if len(grandchildren) < 2:
                return False, (
                    f"第 {i + 1}.{j + 1} 项三级目录不足 2 个"
                    f"（实际 {len(grandchildren)} 个）"
                )

            for k, gc in enumerate(grandchildren):
                # BUG 修复：若 gc 不是 dict，跳过
                if not isinstance(gc, dict):
                    continue

                # 三级节点：树深度推导 level=3
                err = _consistency_check(gc, 3, f"第 {i + 1}.{j + 1}.{k + 1} 项")
                if err:
                    logger.warning("[目录校验] %s（非致命，继续校验）", err)

                # V4.0 三级标题校验
                err = _check_node(gc, f"第 {i + 1}.{j + 1}.{k + 1} 项")
                if err:
                    return False, err

    return True, "ok"


def _normalize_title_core(title: str) -> str:
    t = title.strip().lower()
    t = re.sub(r"[\s\u3000]+", "", t)
    t = re.sub(r"[^\w\u4e00-\u9fff]", "", t)
    return t


def _title_similarity(a: str, b: str) -> float:
    na = _normalize_title_core(a)
    nb = _normalize_title_core(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    set_a = {na[i : i + 2] for i in range(len(na) - 1)} if len(na) > 1 else {na}
    set_b = {nb[i : i + 2] for i in range(len(nb) - 1)} if len(nb) > 1 else {nb}
    if not set_a and not set_b:
        return 1.0
    if not set_a or not set_b:
        return 0.0
    intersection = len(set_a & set_b)
    union = len(set_a | set_b)
    return intersection / union if union > 0 else 0.0


def _is_title_duplicate(a: str, b: str) -> tuple[bool, float]:
    """判断两个标题是否为重复（V3.8 强化版，多级判定）。

    仅用大字符二元组 Jaccard 相似度（原 _title_similarity）无法识别短标题近义重复，
    例如「项目概述」vs「项目概况」相似度仅 0.5，却会在目录中形成「第一章、项目概述」
    与「第X章、项目概况」的高度重复条目。本函数采用四级判定：
      1. 归一化后完全相等 → 重复
      2. 一方是另一方的子串（如「项目概述」⊂「项目概述与总体部署」）→ 重复
      3. 短标题共享前 3 字前缀（如「项目概述」vs「项目概况」共享「项目概」）→ 重复
      4. 大字符二元组 Jaccard 相似度 ≥ 0.7 → 重复

    Args:
        a: 标题 A
        b: 标题 B
    Returns:
        (是否重复, 判定依据分值)
    """
    na = _normalize_title_core(a)
    nb = _normalize_title_core(b)
    if not na or not nb:
        return False, 0.0
    if na == nb:
        return True, 1.0
    # 包含关系：一个标题是另一个的子串
    if na in nb or nb in na:
        return True, 0.9
    # 短标题前缀判定（如「项目概述」vs「项目概况」）
    if len(na) >= 4 and len(nb) >= 4 and na[:3] == nb[:3]:
        return True, 0.85
    # 相似度判定
    sim = _title_similarity(a, b)
    return sim >= 0.7, sim


def semantic_relevance_check(
    outline: list[dict],
    threshold: float = 0.3,
) -> list[dict]:
    """检查目录中三级标题是否与父级二级标题语义相关。

    使用基于字符二元组 Jaccard 相似度的轻量语义检查，
    无需外部嵌入模型。若三级标题与父二级标题的相似度低于阈值，
    则发出警告。

    V4.0 增强说明：
    - 轻量实现：基于 _title_similarity（大字符二元组 Jaccard），
      适合无外部嵌入模型的场景。
    - 若后续接入嵌入模型（如 text2vec），可替换为向量余弦相似度。

    Args:
        outline: 完整目录树
        threshold: 相似度阈值，低于此值视为可能不相关（默认0.3）

    Returns:
        list[dict]: 语义警告列表，每项包含 {path, level3_title, level2_title, similarity}
    """
    warnings = []
    for i, chapter in enumerate(outline):
        # BUG 修复：若 chapter 不是 dict，跳过
        if not isinstance(chapter, dict):
            continue
        chapter_title = chapter.get("title", "")
        for j, l2 in enumerate(chapter.get("children", [])):
            # BUG 修复：若 l2 不是 dict，跳过
            if not isinstance(l2, dict):
                continue
            l2_title = l2.get("title", "")
            for k, l3 in enumerate(l2.get("children", [])):
                # BUG 修复：若 l3 不是 dict，跳过
                if not isinstance(l3, dict):
                    continue
                l3_title = l3.get("title", "")
                if not l2_title or not l3_title:
                    continue
                sim = _title_similarity(l2_title, l3_title)
                if sim < threshold:
                    warnings.append(
                        {
                            "path": f"第{i+1}章.{j+1}级.{k+1}级",
                            "chapter_title": chapter_title,
                            "level2_title": l2_title,
                            "level3_title": l3_title,
                            "similarity": round(sim, 4),
                            "suggestion": "三级标题与二级主题相关性较低，建议调整标题使其更贴合二级主题",
                        }
                    )
    return warnings


def validate_outline_with_semantic(
    outline: list[dict],
    semantic_threshold: float = 0.3,
) -> tuple[bool, str, list[dict]]:
    """增强版校验：执行结构校验 + 语义相关性检查。

    先调用 validate_outline_business 进行基础结构校验，
    再执行语义相关性检查，返回综合结果。

    Args:
        outline: 完整目录树
        semantic_threshold: 语义相似度阈值

    Returns:
        (passed, message, semantic_warnings)
    """
    # 1. 基础结构校验
    passed, msg = validate_outline_business(outline)

    # 2. 语义相关性检查（非致命，仅警告）
    semantic_warnings = semantic_relevance_check(outline, threshold=semantic_threshold)

    if semantic_warnings:
        logger.warning(
            "语义相关性检查发现 %d 处可能不相关的三级标题",
            len(semantic_warnings),
        )
        # 对于严重语义问题（相似度为0），标记为校验失败
        zero_sim = [w for w in semantic_warnings if w["similarity"] == 0.0]
        if zero_sim and passed:
            msg = f"语义校验: {len(zero_sim)} 个三级标题与二级完全无关"
            passed = False

    return passed, msg, semantic_warnings


def _is_l3_title_project_specific(title: str, l2_title: str = "") -> bool:
    """判断三级标题是否结合了项目具体内容。"""
    if not title or len(title) < 5:
        return False

    # 检查是否含项目特异性关键词
    for kw in _L3_PROJECT_SPECIFIC_KEYWORDS:
        if kw in title:
            return True

    # 检查是否含通用占位词
    for pat in _L3_GENERIC_PATTERNS:
        if pat in title:
            return False

    # 长度足够且不含通用词 → 视为具体
    return len(title) >= 7


def validate_l3_specificity(outline: list[dict]) -> tuple[bool, list[dict]]:
    """校验三级目录标题的项目针对性。

    返回:
        (passed, warnings) — warnings 为 [{"path": "...", "title": "...", "reason": "..."}]
    """
    warnings: list[dict] = []

    def _check_l3(node: dict, path: str, l2_title: str = ""):
        # BUG 修复：若 node 不是 dict，node.get() 会 AttributeError
        if not isinstance(node, dict):
            return
        title = str(node.get("title", "") or "").strip()
        level = node.get("level", 0)
        if level != 3:
            return
        if not _is_l3_title_project_specific(title, l2_title):
            reason = "含通用占位词，缺乏项目针对性"
            for pat in _L3_GENERIC_PATTERNS:
                if pat in title:
                    reason = f"含通用占位词「{pat}」"
                    break
            if len(title) < 5:
                reason = f"标题过短（{len(title)}字），缺乏信息量"
            warnings.append(
                {
                    "path": path,
                    "title": title,
                    "reason": reason,
                }
            )
        # 递归检查子节点
        for child in node.get("children", []):
            # BUG 修复：若 child 不是 dict，跳过避免 AttributeError
            if isinstance(child, dict):
                _check_l3(child, f"{path}.{child.get('level', 0)}", title)

    for i, item in enumerate(outline, 1):
        # BUG 修复：若 item 不是 dict，跳过
        if not isinstance(item, dict):
            continue
        for j, child in enumerate(item.get("children", []), 1):
            # BUG 修复：若 child 不是 dict，跳过
            if not isinstance(child, dict):
                continue
            l2_title = child.get("title", "")
            for k, gc in enumerate(child.get("children", []), 1):
                _check_l3(gc, f"{i}.{j}.{k}", l2_title)

    return len(warnings) == 0, warnings


async def repair_l3_generic_titles(
    provider: Any,
    outline: list[dict],
    summary: str,
    tender_context: str,
    boq_json: str,
    work_interface_json: str,
    construction_method_json: str,
    project_scope: str = "",
) -> list[dict]:
    """修复含通用占位词的三级目录标题。

    策略：对每个含通用词的L3标题，收集其L1/L2上下文+项目信息，
    调用AI重新生成一个具体的三级标题。
    """

    repaired = copy.deepcopy(outline)
    _, warnings = validate_l3_specificity(repaired)
    if not warnings:
        return repaired

    logger.info("[L3修复] 检测到 %d 个通用三级标题，开始修复", len(warnings))

    # 构建项目上下文
    project_context = f"【项目概述】\n{summary[:3000]}\n"
    if tender_context:
        project_context += f"【招标文件关键信息】\n{tender_context[:5000]}\n"
    if boq_json:
        project_context += f"【清单工程量】\n{boq_json[:2000]}\n"
    if construction_method_json:
        project_context += f"【工程作法】\n{construction_method_json[:2000]}\n"
    if project_scope:
        project_context += f"【承包范围】\n{project_scope[:1000]}\n"

    # BUG-FIX-PROMPT-B2 2026-10-04：取**未渲染的模板**（命中 DB 覆盖），变量注入
    # 交由统一渲染函数 render_prompt（下方 _repair_one 内逐任务完成）。旧实现用
    # `get_prompt(key)` 不带 kwargs，会每次都触发缺失变量告警噪声，且只靠 raw
    # `.replace("__UPPER__", ...)` 注入，与全平台 render_prompt 口径不一致。
    repair_template = get_prompt("L3_TITLE_REPAIR_PROMPT")

    # 预构建所有待修复任务（并行执行）
    repair_tasks: list[tuple[int, int, int, str, str, str]] = []
    for w in warnings[:10]:  # 最多修复10个，避免过多AI调用
        path = w["path"]
        parts = path.split(".")
        if len(parts) != 3:
            continue
        try:
            i, j, k = int(parts[0]) - 1, int(parts[1]) - 1, int(parts[2]) - 1
            l1_title = repaired[i].get("title", "")
            l2_title = (
                repaired[i].get("children", [{}])[j].get("title", "")
                if j < len(repaired[i].get("children", []))
                else ""
            )
            l3_title = (
                repaired[i].get("children", [{}])[j].get("children", [{}])[k].get("title", "")
                if k < len(repaired[i].get("children", [{}])[j].get("children", []))
                else ""
            )
            if not l3_title:
                continue
            repair_tasks.append((i, j, k, l1_title, l2_title, l3_title))
        except (IndexError, ValueError):
            continue

    async def _repair_one(
        i: int, j: int, k: int, l1_title: str, l2_title: str, l3_title: str
    ) -> None:
        """并行修复单个L3标题"""
        repair_msg = render_prompt(
            repair_template,
            L1_TITLE=l1_title,
            L2_TITLE=l2_title,
            L3_TITLE=l3_title,
            PROJECT_CONTEXT=project_context,
        )
        messages = [
            AIMessage(role="system", content=repair_msg),
            AIMessage(
                role="user", content=f'请将以下三级标题替换为结合本项目具体内容的标题："{l3_title}"'
            ),
        ]
        try:
            new_title = await with_timeout(
                provider.chat(messages, temperature=0.3),
                timeout=30,
                error_msg="三级标题修复AI调用超时",
            )
            # 清理可能的markdown围栏
            new_title = new_title.strip().strip("`").strip("json").strip()
            if new_title and len(new_title) >= 5 and new_title != l3_title:
                repaired[i]["children"][j]["children"][k]["title"] = new_title
                logger.info("[L3修复] %s → %s", l3_title[:30], new_title[:30])
        except Exception as e:
            logger.warning("[L3修复] AI调用失败 (%s): %s", l3_title[:20], e)

    if repair_tasks:
        await asyncio.gather(*[_repair_one(*t) for t in repair_tasks], return_exceptions=True)

    return repaired


# ============================================================

# 从外部 JSON 文件加载章节标题模板，避免 ~1350 行内联数据
_templates_path = os.path.join(os.path.dirname(__file__), "prompts", "chapter_title_templates.json")
try:
    with open(_templates_path, encoding="utf-8") as _f:
        _raw_templates = json.load(_f)
except (FileNotFoundError, json.JSONDecodeError) as _e:
    logging.getLogger(__name__).warning("加载章节标题模板文件失败: %s，使用空字典", _e)
    _raw_templates = {}

# 显式转换为 OrderedDict，确保模板迭代顺序与定义顺序一致
CHAPTER_TITLE_TEMPLATES: dict[str, dict[str, list[str] | dict[str, list[str]]]] = OrderedDict(
    (
        k,
        {
            "level2": v["level2"],
            "level3": OrderedDict(v["level3"])
            if isinstance(v.get("level3"), dict)
            else v.get("level3", {}),
        },
    )
    for k, v in _raw_templates.items()
)


_L2_TITLE_PATTERNS: dict[str, list[str]] = {
    "quality": [
        "质量目标与创优规划",
        "质量保证体系与组织机构",
        "施工质量过程控制",
        "质量通病防治措施",
        "隐蔽工程质量验收",
        "成品保护与交付维护",
        "质量检验与试验管理",
        "质量回访与保修承诺",
        "质量文档与档案管理",
        "质量奖惩与考核制度",
        "材料设备质量管控",
        "分项工程验收标准",
        "质量例会与沟通机制",
        "质量监督与整改闭环",
        "质量风险预警管理",
        "质量持续改进机制",
    ],
    "safety": [
        "安全目标与管理体系",
        "安全生产责任制与组织架构",
        "施工现场安全管控措施",
        "特种作业与危险源管理",
        "临时用电安全管理",
        "机械设备安全防护",
        "高处作业与临边防护",
        "消防安全与应急管理",
        "安全培训与交底制度",
        "安全检查与隐患排查",
        "文明施工与环境保护",
        "职业病防治与健康管理",
        "安全教育与宣传机制",
        "安全事故报告与处理",
        "安全防护用品管理",
        "安全投入与经费保障",
    ],
    "schedule": [
        "施工总进度计划编制",
        "关键线路分析与里程碑节点",
        "进度保障措施与资源配置",
        "进度动态监控与调整机制",
        "工期延误应急预案",
        "进度协调与会议制度",
        "交叉作业进度统筹",
        "进度报表与报告体系",
        "进度预警与纠偏措施",
        "节点工期考核方案",
        "赶工措施与资源增配",
        "进度优化与网络计划",
        "总进度计划与分项计划",
        "进度资金保障机制",
        "进度风险识别与防控",
        "进度总结与经验沉淀",
    ],
    "resource": [
        "劳动力配置与进场计划",
        "施工机械设备投入计划",
        "主要材料采购与供应方案",
        "试验检测设备配置",
        "资金使用与现金流计划",
        "资源动态调配机制",
        "材料进场检验方案",
        "设备维护保养计划",
        "劳动力培训与安全教育",
        "资源需求预测与调整",
        "外包资源管理方案",
        "仓储与物流管理",
        "资源平衡与优化配置",
        "主要材料品牌与规格",
        "机械设备进退场计划",
        "资源成本管控措施",
    ],
    "construction": [
        "施工测量方案与精度控制",
        "土方开挖与基坑支护施工",
        "主体结构施工方法",
        "装饰装修工程施工工艺",
        "机电安装施工方案",
        "脚手架与临时设施方案",
        "屋面防水施工方法",
        "砌体工程施工方案",
        "钢结构安装工艺",
        "地面工程施工方案",
        "门窗安装施工工艺",
        "防水与保温施工方法",
        "施工降排水方案",
        "模板工程专项方案",
        "钢筋工程专项方案",
        "混凝土工程专项方案",
    ],
    "overview": [
        "工程概况与建设规模",
        "项目建设地点与周边环境",
        "工程承包范围与合同工期",
        "项目质量目标与安全目标",
        "施工条件与现场环境",
        "项目组织架构与人员配置",
        "项目特点与重难点分析",
        "主要技术参数与指标",
        "主要工程量汇总",
        "项目主要材料设备概况",
        "项目设计依据与技术标准",
        "项目主要分部分项工程",
        "项目工期目标与节点安排",
        "项目主要风险点识别",
        "项目组织协调原则",
        "项目实施总体思路",
    ],
    "layout": [
        "施工总平面布置原则与依据",
        "施工总平面布置方案",
        "临时道路与交通组织",
        "材料堆场与加工区布置",
        "临时办公与生活设施",
        "临时水电设施布置",
        "消防布置与安全通道",
        "施工分区与围挡方案",
        "排水系统与环保措施",
        "垂直运输设备布置",
        "现场标牌与CI形象布置",
        "季节性施工平面调整",
        "临建拆除与场地恢复",
        "总平面动态管理方案",
        "施工总平面布置图",
        "平面布置说明与依据",
    ],
    "emergency": [
        "应急组织体系与职责分工",
        "风险识别与预警机制",
        "应急响应分级与处置流程",
        "应急资源与物资保障",
        "突发事件专项处置方案",
        "应急演练计划与评估",
        "灾后恢复与总结改进",
        "应急信息报告与舆论应对",
        "应急培训与教育计划",
        "应急联动与外部协调",
        "自然灾害应急处置",
        "安全事故应急处理",
        "公共卫生事件应急响应",
        "应急资金与后勤保障",
        "应急预案管理体系",
        "应急总结与持续改进",
    ],
    "coordination": [
        "组织协调组织架构与职责",
        "沟通协调机制与流程设计",
        "与建设单位配合方案",
        "与监理单位协调措施",
        "与设计单位技术对接方案",
        "专业分包协调管理",
        "外部关系协调与公共关系",
        "联席会议制度与决策流程",
        "信息传递与报告机制",
        "冲突处理与争议解决程序",
        "与政府主管部门协调",
        "社区关系与居民沟通",
        "协调会议管理制度",
        "协调档案与记录管理",
        "协调效果评估与改进",
        "跨专业协同工作机制",
    ],
    "analysis": [
        "项目主要特点与优势分析",
        "施工重难点识别与对策",
        "技术难点分析与解决方案",
        "管理难点分析与应对策略",
        "进度难点分析与保障措施",
        "安全难点分析与防控方案",
        "质量难点分析与控制措施",
        "外部环境风险分析",
        "资源供应风险分析",
        "组织协调难点分析",
        "季节性施工难点分析",
        "交叉作业难点分析",
        "项目风险总体评估",
        "难点应对效果预测",
        "重难点动态跟踪机制",
        "分析结论与建议汇总",
    ],
    "measure": [
        "施工总体部署与规划",
        "主要施工方案与技术措施",
        "专项施工方案",
        "施工关键工序控制",
        "施工资源配置方案",
        "施工节点进度计划",
        "季节性施工措施",
        "成品保护措施",
        "施工技术方案优化",
        "施工质量控制要点",
        "施工安全管理措施",
        "施工进度保障措施",
        "施工成本控制措施",
        "施工环保保障措施",
        "施工技术管理措施",
        "施工协调管理措施",
    ],
    "attachment": [
        "施工总平面布置图",
        "施工进度计划网络图",
        "项目管理组织架构图",
        "专项施工方法流程图",
        "主要机械设备配置表",
        "主要材料进场计划表",
        "劳动力动态曲线图",
        "质量检验试验计划表",
        "安全文明施工检查表",
        "进度计划甘特图",
        "施工总进度计划横道图",
        "项目管理流程图",
        "主要分部分项工程进度计划",
        "材料设备采购计划表",
        "安全应急预案流程图",
        "项目组织机构职责表",
    ],
    "other": [
        "基本组织方案",
        "主要实施措施",
        "质量控制要点",
        "安全管理办法",
        "进度管控方案",
        "资源配置计划",
        "技术保障措施",
        "协调管理方案",
        "现场管理制度",
        "档案资料管理",
        "成本管控措施",
        "环境保护措施",
        "综合管理方案",
        "技术质量控制",
        "进度安全保障",
        "资源协调管理",
    ],
}


_L3_TITLE_PATTERNS: dict[str, list[str]] = {
    "quality": [
        "质量管理组织架构搭建",
        "质量方针目标与指标分解",
        "质量管理责任制与签字制度",
        "质量管理制度文件汇编",
        "材料进场质量检验流程",
        "工序交接质量检查制度",
        "隐蔽工程验收程序",
        "分项分部工程质量评定",
        "质量通病防治技术方案",
        "质量缺陷处理与返工程序",
        "质量检验试验计划",
        "质量数据分析与改进",
        "质量奖惩考核实施细则",
        "质量例会与沟通机制",
        "质量回访保修制度",
        "质量持续改进流程",
        "质量策划与事前控制措施",
        "过程质量控制要点",
        "质量资料归档与验收准备",
        "质量通病专项治理方案",
        "质量控制节点与停检点设置",
        "质量追溯与终身责任制",
        "质量缺陷预防与纠偏机制",
        "质量评审与持续改进循环",
        "首件鉴定与样板引路制度",
        "质量数据信息化管理",
        "质量目标分解与考核体系",
        "质量检验批划分方案",
        "质量通病防治交底卡",
        "质量事故分析与整改闭环",
        "质量改进PDCA循环机制",
        "材料复试与见证取样管理",
        "质量目标分解与考核指标",
        "质量检验批划分与验收标准",
        "质量事故责任追溯机制",
        "质量数据可视化分析报告",
        "质量审核内审流程与整改",
        "质量回访保修服务方案",
        "质量创优策划与申报流程",
        "质量追溯码与数字化台账",
    ],
    "safety": [
        "安全生产领导小组设立",
        "各级人员安全生产责任书",
        "安全管理组织网络搭建",
        "安全生产管理制度汇编",
        "危险源辨识与风险分级",
        "专项安全施工方案编制",
        "安全教育培训与交底",
        "安全检查与隐患排查制度",
        "安全事故报告与处理流程",
        "安全防护用品管理",
        "特种作业人员管理",
        "安全经费投入与使用",
        "安全例会与协调机制",
        "安全应急预案演练",
        "安全奖惩考核制度",
        "安全文明施工创建方案",
        "安全技术创新与工艺改进",
        "安全风险分级管控措施",
        "安全隐患排查治理台账",
        "安全应急演练评估与改进",
        "安全文明施工标准化建设",
        "特种作业持证上岗核查",
        "安全费用专款专用审计",
        "安全风险动态预警机制",
        "安全技术交底签字存档",
        "交叉作业安全协调机制",
        "安全警示标识与防护设施",
        "安全事故责任追究制度",
        "安全教育培训效果评估",
        "安全隐患闭环整改流程",
        "安全例会会议纪要归档",
        "安全应急预案动态更新",
        "安全风险分级管控清单",
        "安全文明施工检查结果通报",
        "安全费用投入统计分析",
        "安全应急预案响应时间考核",
        "安全培训效果跟踪机制",
        "安全隐患排查标准台账",
        "安全责任考核奖惩兑现",
        "安全标准化示范工地创建",
    ],
    "schedule": [
        "施工总进度计划编制方法",
        "关键线路识别与工期目标",
        "进度计划分级管理体系",
        "进度资源配置与平衡",
        "进度偏差分析与纠偏措施",
        "进度协调会议与报告制度",
        "交叉作业进度统筹方案",
        "进度预警与应急响应",
        "赶工措施与资源增配",
        "进度计划优化调整",
        "节点工期考核与奖惩",
        "进度数据收集与分析",
        "进度风险识别与防控",
        "进度总结与经验归档",
        "总进度计划与施工网络图",
        "进度计划动态调整机制",
        "进度计划滚动修订制度",
        "工期延误责任划分机制",
        "多专业交叉进度协调表",
        "进度偏差根本原因分析",
        "进度资源动态调配方案",
        "进度考核与激励机制",
        "进度风险预留与缓冲安排",
        "进度数据可视化看板建设",
        "进度计划里程碑管理",
        "进度协调联席会议制度",
        "进度偏差预警与纠偏流程",
        "进度总结与复盘机制",
        "进度计划版本控制管理",
        "进度数据分析报表体系",
        "进度风险应急预案编制",
        "进度考核指标分解落地",
        "进度计划里程碑分解图",
        "进度协调联席会议制度汇编",
        "进度偏差预警响应流程图",
        "进度数据可视化看板建设方案",
        "进度计划三级预警机制",
        "进度风险应急赶工预案",
        "进度计划数字化协同平台",
        "进度偏差根因追溯报告",
    ],
    "resource": [
        "劳动力需求预测与配置",
        "各阶段用工计划与进场安排",
        "施工机械设备选型与配置",
        "主要材料采购与供应计划",
        "试验检测设备配置方案",
        "资金使用计划与现金流管理",
        "材料进场检验与保管",
        "设备维护保养计划",
        "劳动力培训与安全教育",
        "资源动态调配与平衡",
        "外包资源管理方案",
        "材料仓储与物流管理",
        "资源成本分析与控制",
        "主要设备进退场计划",
        "资源需求预测调整机制",
        "资源总结与经验归档",
        "资源需求弹性预测模型",
        "供应商资质审查与评价",
        "材料损耗率控制与节约措施",
        "大型设备租赁比选方案",
        "资源调配应急预案",
        "资源成本动态监控机制",
        "劳动力技能矩阵与培训体系",
        "资源利用率评估指标",
        "资源需求预测偏差分析",
        "供应商备选库建设方案",
        "材料采购价格监测机制",
        "设备维护保养周期表",
        "劳动力技能等级考核标准",
        "资源成本偏差纠偏措施",
        "资源需求预测方法总结",
        "供应商评价打分表模板",
        "材料损耗率指标分解表",
        "设备租赁比选决策流程",
        "资源需求预测偏差分析报告",
        "供应商备选库动态更新机制",
        "劳动力技能等级考核评估报告",
        "资源成本偏差预警与应对方案",
        "资源需求预测偏差修正方案",
        "供应商动态绩效评价机制",
    ],
    "construction": [
        "施工测量定位与精度控制",
        "土方开挖与基坑支护工艺",
        "基础工程施工方法",
        "主体结构施工技术方案",
        "钢筋工程专项工艺",
        "模板工程专项方案",
        "混凝土工程专项工艺",
        "砌体工程施工方法",
        "钢结构安装工艺",
        "屋面防水施工方法",
        "外墙保温施工要点",
        "装饰装修工程施工工艺",
        "机电安装施工方案",
        "脚手架搭设与拆除方案",
        "施工降排水方案",
        "季节性施工措施",
        "预制构件吊装与连接工艺",
        "大体积混凝土温控方案",
        "深基坑监测与信息化施工",
        "高空作业安全技术措施",
        "施工临时设施搭设规范",
        "特种工序质量检验标准",
        "新材料新工艺应用方案",
        "复杂节点施工方案优化",
        "预应力张拉施工工艺",
        "焊接工艺评定与检测",
        "防水工程细部构造做法",
        "幕墙施工技术方案",
        "桩基施工专项方案",
        "地下室防水施工工艺",
        "高空吊装安全验算",
        "大体积混凝土测温方案",
        "测量定位复测与验线",
        "施工缝与后浇带处理工艺",
        "预制构件吊装专项方案",
        "深基坑监测自动化系统",
        "高空作业安全应急预案",
        "特种工序质量追溯制度",
        "施工测量复核与精度验算",
        "地下管线保护专项方案",
    ],
    "overview": [
        "工程名称与建设地点",
        "项目规模与结构类型",
        "承包范围与合同工期",
        "质量标准与安全目标",
        "施工条件与现场环境",
        "主要工程量概况",
        "项目组织架构与人员",
        "项目主要技术特点",
        "项目建设背景与意义",
        "项目设计依据与标准",
        "项目主要分部分项工程",
        "项目重难点初步识别",
        "项目投资与资金来源",
        "项目合同结构与分包",
        "项目实施总体思路",
        "项目主要风险点概述",
        "项目地理位置与交通条件",
        "周边环境和制约因素分析",
        "项目组织架构与职责分工",
        "项目管理体系与运行机制",
        "项目投资构成与资金筹措",
        "项目主要技术参数清单",
        "项目相关方职责与协作机制",
        "项目实施总体路线图",
        "项目审批手续与合规性审查",
        "项目建设条件踏勘报告",
        "项目主要技术经济指标",
        "项目组织架构人员编制表",
        "项目管理体系框架图",
        "项目实施阶段划分与里程碑",
        "项目相关方沟通计划",
        "项目风险矩阵与应对策略",
        "项目审批流程与进度计划",
        "项目建设条件踏勘记录表",
        "项目主要技术参数清单模板",
        "项目组织架构人员编制方案",
        "项目投资估算与经济评价",
        "项目主要工程量计算书",
        "项目参建单位职责矩阵",
        "项目全周期管理流程图",
    ],
    "coordination": [
        "协调组织体系与职责分工",
        "沟通协调流程与机制",
        "例会制度与决策程序",
        "信息传递与报告渠道",
        "冲突处理与争议解决",
        "外包协调管理方案",
        "政府主管部门协调",
        "社区与公众关系管理",
        "协调会议记录与档案管理",
        "协调效果评估机制",
        "跨专业协同工作方案",
        "外部接口管理方案",
        "协调风险预警机制",
        "协调经验总结与改进",
        "协调制度文件汇编",
        "协调培训计划",
        "多方协调联席会议制度",
        "协调问题升级处理机制",
        "协调信息数字化管理平台",
        "协调效果量化评估指标",
        "外部关系维护与公共关系",
        "协调风险预案与快速响应",
        "协调成果文档标准化模板",
        "协调培训与能力提升计划",
        "协调问题跟踪闭环管理",
        "跨部门协调责任矩阵",
        "外部关系维护年度计划",
        "协调会议纪要标准化模板",
        "协调效果满意度调查机制",
        "协调问题升级处理流程图",
        "协调信息管理系统建设方案",
        "协调培训课件与考核体系",
        "协调问题跟踪闭环管理制度",
        "跨部门协调责任矩阵模板",
        "外部关系维护年度计划方案",
        "协调会议纪要标准化模板汇编",
        "协调机制运转效能审计",
        "多方联席会议议事规则",
        "外部关系公共关系手册",
        "协调问题升级决策流程",
    ],
    "emergency": [
        "应急组织架构与职责",
        "风险识别与预警标准",
        "应急响应分级与流程",
        "应急处置技术方案",
        "应急资源与物资保障",
        "应急演练计划与评估",
        "灾后恢复与总结改进",
        "信息报告与舆论应对",
        "应急培训与教育计划",
        "应急联动与外部协调",
        "自然灾害应急处置",
        "安全事故应急处理",
        "公共卫生事件应急响应",
        "应急资金与后勤保障",
        "应急预案管理体系",
        "应急总结与持续改进",
        "应急指挥体系与决策机制",
        "预警信息发布与响应触发",
        "应急资源储备与调配方案",
        "灾后恢复与业务连续性计划",
        "应急演练效果评估与改进",
        "应急联动外部机构协作机制",
        "应急物资动态盘点与更新",
        "应急响应时间指标与考核",
        "应急值班与报告制度",
        "应急指挥平台运维保障",
        "应急资源跨区域调配预案",
        "应急培训效果评估标准",
        "应急响应时效考核机制",
        "应急演练脚本与评估表",
        "应急物资储备清单与盘点表",
        "应急指挥决策流程图",
        "应急值班与报告制度汇编",
        "应急指挥平台运维保障方案",
        "应急资源跨区域调配预案汇编",
        "应急响应时效考核指标体系",
        "应急资源动态盘点机制",
        "应急联动协议签订方案",
        "应急演练脚本编写规范",
        "应急响应时效考核细则",
    ],
    "layout": [
        "施工总平面布置原则",
        "分区与功能划分方案",
        "临时道路与交通组织",
        "材料堆场与加工区布置",
        "临时办公与生活设施",
        "临时水电设施布置",
        "消防布置与安全通道",
        "排水系统与环保措施",
        "垂直运输设备布置",
        "施工分区与围挡方案",
        "现场标牌与CI形象",
        "季节性平面调整方案",
        "临建拆除与场地恢复",
        "总平面动态管理",
        "施工总平面布置图绘制",
        "平面布置说明与依据",
        "平面布置动态优化机制",
        "临时设施标准化建造规范",
        "施工通道与物流组织优化",
        "临建容量规划与分期安排",
        "平面布置BIM可视化方案",
        "场地复用与二次转运规划",
        "季节性平面调整应急预案",
        "总平面管理责任分区划分",
        "施工总平面分层布置方案",
        "临建设施抗震与消防验算",
        "总平面交通流线优化设计",
        "施工分区与封闭管理方案",
        "临建设施标准化图集",
        "总平面动态调整审批流程",
        "施工道路排水与沉降监测",
        "总平面布置效果评估指标",
        "施工总平面分层布置方案汇编",
        "临建设施抗震消防验算报告",
        "总平面交通流线优化设计方案",
        "施工分区封闭管理实施方案",
        "总平面动态调整审批表",
        "临建设施抗震验算报告",
        "施工道路沉降监测方案",
        "平面布置BIM仿真验证",
    ],
    "analysis": [
        "项目特点与优势分析",
        "施工重难点识别与对策",
        "技术难点分析与方案",
        "管理难点分析与措施",
        "进度难点分析与保障",
        "安全难点分析与防控",
        "质量难点分析与控制",
        "外部环境风险分析",
        "资源供应风险分析",
        "组织协调难点分析",
        "季节性施工难点分析",
        "交叉作业难点分析",
        "项目风险总体评估",
        "难点应对效果预测",
        "重难点动态跟踪机制",
        "分析结论与建议汇总",
        "难点识别方法论与评分模型",
        "重难点分级分类管控策略",
        "技术难点专项论证方案",
        "管理难点组织优化建议",
        "难点应对资源保障分析",
        "重难点动态监测与预警机制",
        "难点应对效果量化评估",
        "难点分析与解决方案库建设",
        "难点识别与分级标准",
        "重难点管控责任矩阵",
        "技术难点专家论证流程",
        "管理难点专项整改方案",
        "难点应对资源平衡分析",
        "重难点动态监测报告模板",
        "难点应对效果评估指标",
        "难点知识库建设方案",
        "难点识别与分级标准汇编",
        "重难点管控责任矩阵模板",
        "技术难点专家论证流程标准化",
        "管理难点专项整改方案汇编",
        "重难点识别评分模型说明",
        "难点应对方案库建设方案",
        "技术难点专家论证纪要",
        "难点知识库建设与维护方案",
    ],
    "measure": [
        "施工总体部署与规划",
        "主要施工方案与技术措施",
        "专项施工方案编制",
        "施工关键工序控制要点",
        "施工资源配置方案",
        "施工节点进度计划",
        "季节性施工保障措施",
        "成品保护具体措施",
        "施工技术方案优化",
        "施工质量控制要点",
        "施工安全管理措施",
        "施工进度保障措施",
        "施工成本控制措施",
        "施工环保保障措施",
        "施工技术管理措施",
        "施工协调管理措施",
        "施工总体部署动态调整方案",
        "重大施工方案专家论证机制",
        "专项施工方案编制深度标准",
        "施工关键工序质量控制卡",
        "施工资源配置弹性调整方案",
        "施工节点里程碑考核办法",
        "季节性施工专项保障方案",
        "成品保护分级管理制度",
        "施工组织设计动态修订流程",
        "重大施工方案专家库建设",
        "专项施工方案编制审查流程",
        "关键工序质量控制卡模板",
        "资源配置弹性调整审批流程",
        "里程碑节点考核奖惩办法",
        "季节性施工专项技术交底",
        "成品保护分级管理制度汇编",
        "施工组织设计动态修订制度",
        "重大施工方案专家库管理办法",
        "专项施工方案编制审查标准",
        "关键工序质量控制卡实施细则",
        "施工组织设计动态修订手册",
        "专项施工方案编制审查指南",
        "里程碑节点考核奖惩办法汇编",
        "施工组织设计动态修订流程标准化",
    ],
    "attachment": [
        "施工总平面布置图",
        "施工进度计划网络图",
        "项目管理组织架构图",
        "专项施工方法流程图",
        "主要机械设备配置表",
        "主要材料进场计划表",
        "劳动力动态曲线图",
        "质量检验试验计划表",
        "安全文明施工检查表",
        "进度计划甘特图",
        "施工总进度计划横道图",
        "项目管理流程图",
        "主要分部分项工程进度计划",
        "材料设备采购计划表",
        "安全应急预案流程图",
        "项目组织机构职责表",
        "施工总进度计划双代号网络图",
        "主要分部分项工程流水段划分图",
        "项目管理岗位职责矩阵表",
        "专项施工方案审批流程图",
        "机械设备维修保养记录表",
        "材料进场验收合格台账",
        "劳动力动态分布热力图",
        "质量检验批次计划汇总表",
        "施工进度计划WBS分解图",
        "安全管理组织机构网络图",
        "质量保证措施落实责任表",
        "安全文明施工达标评价表",
        "主要分部分项工程质量控制表",
        "施工进度偏差分析报告表",
        "材料设备进场验收登记表",
        "劳动力技能等级分布表",
        "施工进度计划里程碑分解图",
        "安全管理组织机构职责网络图",
        "质量保证措施落实责任清单",
        "安全文明施工达标评价细则",
        "施工总进度计划里程碑分解图",
        "质量保证措施落实责任清单模板",
        "安全文明施工达标评价细则汇编",
        "施工进度偏差分析报表体系",
    ],
    "other": [
        "基本组织方案",
        "主要实施措施",
        "质量控制要点",
        "安全管理办法",
        "进度管控方案",
        "资源配置计划",
        "技术保障措施",
        "协调管理方案",
        "现场管理制度",
        "档案资料管理",
        "成本管控措施",
        "环境保护措施",
        "综合管理方案",
        "技术质量控制",
        "进度安全保障",
        "资源协调管理",
        "基础保障体系建设方案",
        "实施过程风险防控措施",
        "质量控制关键节点清单",
        "安全管理红线与底线要求",
        "进度管控节点考核办法",
        "资源统筹调配实施方案",
        "技术保障措施清单",
        "协调管理机制运转方案",
        "基础保障体系组织架构图",
        "风险防控措施责任矩阵",
        "质量控制关键节点检查表",
        "安全管理红线条款清单",
        "进度管控节点考核细则",
        "资源统筹调配决策流程",
        "技术保障措施执行清单",
        "协调管理机制运转流程图",
        "基础保障体系组织架构图模板",
        "风险防控措施责任矩阵模板",
        "质量控制关键节点检查表汇编",
        "安全管理红线条款清单模板",
        "综合保障体系建设方案",
        "风险识别与评估方法论",
        "质量控制闭环管理机制",
        "安全管理标准化建设方案",
    ],
}
