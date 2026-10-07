"""Token 预算化、优先级有序的提示词上下文组装（2026-10-06）。

背景：历史实现中「解析提取 / 全局事实 / 目录树 / 图表指令」等上下文由各个
workflow 调用点自行 .replace 注入，存在「上下文组装分散、变量命名不统一、
超大上下文无上限导致 token 暴涨/截断不可控」等问题（见提示词模块审计：
上下文组装分散、缺少 Token 上限控制）。

本模块提供统一入口 `assemble_prompt_context`：
- 统一变量命名与片段标签；
- 按优先级排序组装（高优先级先放入预算内）；
- 超过 token 预算时，优先保留高优先级片段，对「边界片段」做部分截断，
  更低优先级片段整体丢弃（宁缺毋假，不把被截断的半成品塞给模型）。
"""

from __future__ import annotations

import re

# 默认优先级：数值越大越优先保留。覆盖提示词模块常见上下文片段。
DEFAULT_PRIORITIES: dict[str, int] = {
    "GLOBAL_FACTS": 100,          # 全局事实：数据一致性铁律，最高优先
    "SCORING_STANDARDS": 95,      # 评分标准
    "SCORING_POINTS": 95,         # 评分点
    "SUMMARY": 90,                # 项目概述/解析摘要
    "OUTLINE": 85,                # 目录树
    "CHART_INSTRUCTIONS": 80,     # 图表编排决策/指令
    "INDUSTRY_PROFILE": 75,       # 行业画像
    "CONTEXT": 70,                # 通用上下文
    "BOQ_ANALYSIS": 70,           # 工程量清单分析
    "WORK_INTERFACE": 65,         # 工作面接口
    "CONSTRUCTION_METHOD": 65,    # 施工方法
    "OTHER_CHAPTERS": 40,         # 其它章节（参考性最弱）
}


def estimate_tokens(text: str) -> int:
    """粗略 token 估算：CJK 按字符计、其余按 ~4 字符 ≈ 1 token。

    仅用于预算裁剪，非精确计费；误差在 ±30% 内对截断决策足够。
    """
    if not text:
        return 0
    cjk = len(re.findall(r"[一-鿿]", text))
    other = len(text) - cjk
    return cjk + (other + 3) // 4


def assemble_prompt_context(
    parts: dict[str, str],
    *,
    max_tokens: int | None = None,
    priorities: dict[str, int] | None = None,
    delimiter: str = "\n\n",
) -> str:
    """按优先级组装上下文片段，支持 token 预算上限。

    Args:
        parts: {片段名: 内容}；空内容自动跳过。
        max_tokens: token 预算上限。为 None 时不限（全部拼接）。
        priorities: 覆盖默认优先级表。
        delimiter: 片段间连接符。

    Returns:
        组装后的上下文字符串。超出预算时高优先级片段优先保留，
        边界片段做部分截断，更低优先级整体丢弃。
    """
    prio = priorities or DEFAULT_PRIORITIES

    items = [
        (name, content)
        for name, content in parts.items()
        if content and content.strip()
    ]
    if not items:
        return ""

    # 高优先级在前，保证其在预算内被优先保留
    items.sort(key=lambda it: prio.get(it[0], 50), reverse=True)

    if max_tokens is None:
        return delimiter.join(f"【{name}】\n{content}" for name, content in items)

    delim_cost = estimate_tokens(delimiter) if items else 0
    budget = max_tokens
    kept: list[str] = []

    for idx, (name, content) in enumerate(items):
        block = f"【{name}】\n{content}"
        cost = estimate_tokens(block) + (delim_cost if kept else 0)
        if cost <= budget:
            kept.append(block)
            budget -= cost
            continue

        # 超出预算：尝试对「边界片段」做部分截断以适配剩余预算
        remain = budget - (delim_cost if kept else 0)
        if remain > 20:  # 至少留一点意义
            allowed_chars = max(0, remain)  # 粗略：CJK 主导时 1 token ≈ 1 字
            truncated = content[:allowed_chars].rstrip()
            if truncated:
                kept.append(f"【{name}】\n{truncated}（内容因 Token 预算已截断）")
        # 更低优先级片段不再加入
        break

    return delimiter.join(kept)


def truncate_to_tokens(text: str, max_tokens: int) -> str:
    """对单段文本做 Token 软上限截断（超出则截断并附标记，宁缺毋假）。

    用于「单变量占位符」模板（如 __CONTEXT__）的逐值保护：超长招标原文、
    超长事实变量在注入前先按预算裁切，避免撑爆上下文窗口。
    """
    if not text or estimate_tokens(text) <= max_tokens:
        return text
    allowed = max(0, max_tokens)
    truncated = text[:allowed].rstrip()
    return truncated + "\n…（内容因 Token 预算已截断）"


__all__ = [
    "DEFAULT_PRIORITIES",
    "estimate_tokens",
    "assemble_prompt_context",
    "truncate_to_tokens",
    "build_chapter_context",
]


def build_chapter_context(
    *,
    summary: str = "",
    global_facts: str = "",
    scoring_points: str = "",
    outline: str = "",
    industry_profile: str = "",
    project_scope: str = "",
    construction_method: str = "",
    boq_analysis: str = "",
    work_interface: str = "",
    chart_instructions: str = "",
    other_chapters: str = "",
    max_tokens: int = 8000,
) -> str:
    """正文生成场景的统一上下文组装高层接口。

    自动按优先级排序、Token 预算裁剪，输出标准化的上下文块。
    替代各 workflow 中手动拼接 f-string 的分散实现。

    Args:
        summary: 项目概述/解析摘要
        global_facts: 全局事实变量
        scoring_points: 评分点/评分标准
        outline: 目录树
        industry_profile: 行业画像
        project_scope: 承包范围
        construction_method: 施工方法/工程作法
        boq_analysis: 工程量清单分析
        work_interface: 工作面接口
        chart_instructions: 图表编排决策/指令
        other_chapters: 其它章节（同级/上级）
        max_tokens: 上下文总 Token 预算上限

    Returns:
        组装后的上下文字符串，各片段带标准化标签。
    """
    parts = {
        "GLOBAL_FACTS": global_facts,
        "SCORING_POINTS": scoring_points,
        "SUMMARY": summary,
        "OUTLINE": outline,
        "CHART_INSTRUCTIONS": chart_instructions,
        "INDUSTRY_PROFILE": industry_profile,
        "PROJECT_SCOPE": project_scope,
        "BOQ_ANALYSIS": boq_analysis,
        "CONSTRUCTION_METHOD": construction_method,
        "WORK_INTERFACE": work_interface,
        "OTHER_CHAPTERS": other_chapters,
    }
    return assemble_prompt_context(parts, max_tokens=max_tokens)
