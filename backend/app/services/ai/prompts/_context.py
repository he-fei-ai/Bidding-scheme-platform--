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
    # BUG-FIX-PROMPT-G5（优先级缺失 2026-10-14）：PROJECT_SCOPE（承包范围）是
    # build_context_block 的核心片段之一，却从未列入本表，之前静默回落到
    # 魔法默认值 50——低于 INDUSTRY_PROFILE/CONTEXT/BOQ_ANALYSIS/CONSTRUCTION_METHOD
    # 等所有具名片段，导致预算收紧时「界定项目边界」的承包范围反而最先被截断/丢弃。
    # 显式置于 72（行业画像之下、通用 CONTEXT/BOQ 之上），与其实质重要性对齐。
    "PROJECT_SCOPE": 72,          # 承包范围：界定项目边界，核心上下文
    "CONTEXT": 70,                # 通用上下文
    "BOQ_ANALYSIS": 70,           # 工程量清单分析
    "WORK_INTERFACE": 65,         # 工作面接口
    "CONSTRUCTION_METHOD": 65,    # 施工方法
    "OTHER_CHAPTERS": 40,         # 其它章节（参考性最弱）
}


def estimate_tokens(text: str) -> int:
    """Token 估算：tiktoken 精确计数（可用时）→ CJK 启发式回退。

    用于预算裁剪与监控指标。

    ENHANCE 2026-10-11（遗留项3）：tiktoken 已安装时使用精确编码器计数，
    消除 ±30% 启发式误差。tiktoken 不可用时无缝回退到既有启发式
    （CJK 按字符计、其余 ~4 字符 ≈ 1 token），保证环境间行为一致。

    性能：tiktoken 对 20k 字符编码 ~0.5ms（C 扩展），比启发式 translate
    (~1.4ms) 更快且精确。短文本（<200 字符）两者差异可忽略，
    但 tiktoken 精度对预算裁剪决策更可靠（避免过度截断或预算超支）。
    """
    if not text:
        return 0
    # 优先使用 tiktoken 精确计数（可用时）
    if _TIKTOKEN_ENCODER is not None:
        try:
            return len(_TIKTOKEN_ENCODER.encode(text))
        except Exception:
            pass  # 编码异常（如代理字符）→ 回退启发式
    # 启发式回退：CJK 按字符计、其余 ~4 字符 ≈ 1 token
    non_cjk_len = len(text.translate(_CJK_DELETE_TABLE))
    cjk = len(text) - non_cjk_len
    return cjk + (non_cjk_len + 3) // 4


# 统一的截断标记（组装与单段截断共用，保证口径一致且便于测试断言）
_TRUNCATION_MARKER = "\n…（内容因 Token 预算已截断）"

# CJK 统一表删除映射：覆盖 CJK 统一表意文字与扩展 A（与原正则 [一-鿿] 一致）
_CJK_DELETE_TABLE = {c: None for c in range(0x4E00, 0x9FFF + 1)}

# ENHANCE 2026-10-11（遗留项3）：tiktoken 精确编码器（可用时）。
# tiktoken 不可用时为 None，estimate_tokens 无缝回退到 CJK 启发式。
# 使用 cl100k_base 编码（GPT-4/4o 系列），对中英文混合文本计数最准确。
_TIKTOKEN_ENCODER = None
try:
    import tiktoken as _tiktoken_mod

    _TIKTOKEN_ENCODER = _tiktoken_mod.get_encoding("cl100k_base")
except Exception:
    _TIKTOKEN_ENCODER = None

# 边界片段被截断后，至少要保留的正文 token 数。
# 低于此值时「【名称】+ 截断标记」没有信息量，不如整段丢弃，把预算留给下轮。
_MIN_TRUNCATED_BODY_TOKENS = 1


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

    BUG-FIX-PROMPT-C2（预算超支）：原截断分支有两个缺陷，导致实际结果
    **必然超出 max_tokens**：
      1) ``allowed_chars = max(0, remain)`` 把 token 数直接当字符数用；
         对 CJK 为主的文本 1 token≈1 字尚可，但混入 ASCII 后会低估；
      2) 追加的截断标记 ``（内容因 Token 预算已截断）`` 本身约 14 token，
         完全没有从预算里扣除。
    实测 max_tokens=20000 时产出 20014 token。修复：截断标记计入预算，
    且按 estimate_tokens 二分收敛到真正落在预算内。
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

    for name, content in items:
        block = f"【{name}】\n{content}"
        cost = estimate_tokens(block) + (delim_cost if kept else 0)
        if cost <= budget:
            kept.append(block)
            budget -= cost
            continue

        # 超出预算：尝试对「边界片段」做部分截断以适配剩余预算
        remain = budget - (delim_cost if kept else 0)
        marker = _TRUNCATION_MARKER
        head = f"【{name}】\n"
        # BUG-FIX-PROMPT-C2：标记本身占 token，必须先从可用预算里扣除，
        # 否则产出会必然越界（实测 max_tokens=20000 → 20014）。
        head_cost = estimate_tokens(head)
        marker_cost = estimate_tokens(marker)
        # 保留边界片段的条件：剩余预算至少够「片段头 + 截断标记 + 一点正文」。
# BUG-FIX-PROMPT-C7：原用魔法数 `remain > 20`，与标记成本无关，
        # 导致要么白留空壳、要么越界。改为按实际成本判定。
        if remain >= head_cost + marker_cost + _MIN_TRUNCATED_BODY_TOKENS:
            # BUG-FIX-PROMPT-C8：原传入 ``body_budget``（= remain - head - marker，
            # 即「仅正文」的预算），而 _fit_to_token_budget 内部比对的是
            # ``head + content + marker`` 的**整块**成本 —— 头部与标记被扣了两次。
            # 结果是边界片段几乎必然「放不下」而被**静默整体丢弃**：
            # 实测 max_tokens=25 时 GLOBAL_FACTS（最高优先，声明为「数据一致性
            # 铁律」）产出空串 ''，max_tokens=30 时 SUMMARY 连截断标记都进不去。
            # 即：预算收紧后，「高优先级片段被整段丢弃」取代了「低优先级丢弃」，
            # 与 assemble_prompt_context 的优先级契约相反。
            # 修正：传入完整可用预算 remain，由 _fit_to_token_budget 对整块收敛。
            body = _fit_to_token_budget(content, remain, head, marker)
            if body:
                kept.append(f"{head}{body}{marker}")
        # 更低优先级片段不再加入
        break

    return delimiter.join(kept)


def _fit_to_token_budget(content: str, budget_tokens: int, head: str, marker: str) -> str:
    """把 content 截到「head + 截断体 + marker」不超过 budget_tokens 的最大长度。

    ``budget_tokens`` 是**整块**（head + 截断体 + marker）的预算，不是仅正文的
    预算——调用方不要预先扣除 head/marker，否则会被重复扣费（BUG-FIX-PROMPT-C8）。

    BUG-FIX-PROMPT-C2：用二分查找在字符维度收敛，避免两个极端——
      - 直接按字符数切（``content[:budget]``）：CJK 尚可，ASCII 会浪费一半预算；
      - 按 0.95 比例收缩：既慢又会过度截断。
    二分 + estimate_tokens 校验可同时兼顾两种语料，误差收敛在 1 个字符内。
    """
    if budget_tokens <= 0 or not content:
        return ""

    def _fits(n: int) -> bool:
        return estimate_tokens(head + content[:n].rstrip() + marker) <= budget_tokens

    if _fits(len(content)):
        return content.rstrip()

    lo, hi = 0, len(content)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if _fits(mid):
            lo = mid
        else:
            hi = mid - 1
    return content[:lo].rstrip() if lo > 0 else ""


def truncate_to_tokens(text: str, max_tokens: int) -> str:
    """对单段文本做 Token 软上限截断（超出则截断并附标记，宁缺毋假）。

    用于「单变量占位符」模板（如 __CONTEXT__）的逐值保护：超长招标原文、
    超长事实变量在注入前先按预算裁切，避免撑爆上下文窗口。

    BUG-FIX-PROMPT-C2：原实现 ``text[:allowed]`` 直接把 token 数当字符数，
    且未扣除标记自身 token，产出必然略超 max_tokens。现按
    ``estimate_tokens`` 收敛并扣除标记成本。

    BUG-FIX-PROMPT-C9（ASCII 预算浪费）：上版修复仍残留一个缺陷——
    初始切分 ``text[:usable]`` 把 token 数当字符索引。CJK 文本 1 token≈1
    字符尚可，但 ASCII 文本 1 token≈4 字符时初始切分只有预算的 25%，
    而 while 循环只收缩不增长 → ASCII 密集文本被严重截短、预算浪费 75%。
    现改用与 ``_fit_to_token_budget`` 一致的二分查找，对 CJK/ASCII 混排
    均能收敛到预算内最大长度。

    BUG-FIX-PROMPT-C10（小预算标记越界）：当 max_tokens 小于截断标记自身
    token 开销（~11 token）时，标记本身即超出预算。原实现返回纯标记 →
    产出必然越界（如 max_tokens=5 时返回 11 token 的标记）。现降级为
    无标记截断：返回截断后的纯文本（不附标记），保证产出恒不超预算。
    """
    if not text or estimate_tokens(text) <= max_tokens:
        return text
    # 复用 _fit_to_token_budget 的二分查找：budget 包含 marker 成本
    truncated = _fit_to_token_budget(text, max_tokens, "", _TRUNCATION_MARKER)
    if truncated:
        return truncated + _TRUNCATION_MARKER
    # C10 降级：标记自身超出预算 → 无标记截断（纯文本截到预算内）
    return _fit_to_token_budget(text, max_tokens, "", "") or ""


def split_into_token_capped_chunks(text: str, max_tokens: int) -> list[str]:
    """把长文本按行边界切分为若干段，每段 estimate_tokens 不超过 max_tokens。

    ENHANCE 2026-10-14（facts-ns P1-1，对齐易标 globalFactsTask.cjs 分段提取管线）：
    超长招标文件以前被 `truncate_to_tokens` **静默截断**——超出预算的部分直接
    丢失（尾部章节的售后/培训/质保承诺类事实系统性缺失）。现按段切分后逐段
    送 AI 提取、程序侧归并，不再丢内容。

    切分策略：
      - 优先在换行边界断开（保持段落语义完整，Markdown 标题不会被拦腰切）；
      - 单行超预算时按 token 预算二分硬切（极端长行兜底）；
      - 文本本身不超预算时原样返回单元素列表（调用方可直接判断 len>1 走分段路径，
        正常长度文件行为与从前完全一致）。
    """
    if not text:
        return []
    if estimate_tokens(text) <= max_tokens or max_tokens <= 0:
        return [text]

    chunks: list[str] = []
    buf: list[str] = []
    buf_tokens = 0

    def _flush() -> None:
        nonlocal buf, buf_tokens
        if buf:
            chunks.append("\n".join(buf))
            buf = []
            buf_tokens = 0

    for line in text.split("\n"):
        line_tokens = estimate_tokens(line) + 1  # +1 为换行符成本
        # 单行自身超预算：先冲掉已缓冲内容，再对该行按预算二分硬切
        if line_tokens > max_tokens:
            _flush()
            remaining = line
            while remaining and estimate_tokens(remaining) > max_tokens:
                cut = _fit_to_token_budget(remaining, max_tokens, "", "")
                if not cut:
                    break
                chunks.append(cut)
                remaining = remaining[len(cut):]
            if remaining:
                buf.append(remaining)
                buf_tokens = estimate_tokens(remaining) + 1
            continue
        if buf and buf_tokens + line_tokens > max_tokens:
            _flush()
        buf.append(line)
        buf_tokens += line_tokens
    _flush()
    return chunks


__all__ = [
    "DEFAULT_PRIORITIES",
    "estimate_tokens",
    "assemble_prompt_context",
    "truncate_to_tokens",
    "split_into_token_capped_chunks",
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
