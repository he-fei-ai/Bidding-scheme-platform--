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

# 上下文片段名别名表（UNIFY 2026-10-17：统一变量定义与命名）。
#
# 背景（缺口 G-NAMING）：各 workflow 组装上下文时对同一语义使用了不同片段名
# ——「评分标准」有 SCORING_STANDARDS / SCORING_POINTS / SCORING 三种写法，
# 「全局事实」有 GLOBAL_FACTS / FACTS / GLOBAL_FACT，「目录树」有 OUTLINE /
# OUTLINE_TREE / TOC。后果：
#   1) 片段名未在 DEFAULT_PRIORITIES 中时静默回落魔法默认值 50（曾导致
#      PROJECT_SCOPE 被优先截断，见 BUG-FIX-PROMPT-G5）；
#   2) 同名不同义/异名同义使 Token 预算行为无法预测、监控无法聚合。
# 现统一收敛到 DEFAULT_PRIORITIES 中的规范名，非规范名自动映射。
PART_ALIASES: dict[str, str] = {
    # 全局事实
    "FACTS": "GLOBAL_FACTS",
    "GLOBAL_FACT": "GLOBAL_FACTS",
    "GLOBALFACTS": "GLOBAL_FACTS",
    # 评分
    "SCORING": "SCORING_POINTS",
    "SCORING_STANDARD": "SCORING_STANDARDS",
    "SCORE_POINTS": "SCORING_POINTS",
    # 目录树
    "OUTLINE_TREE": "OUTLINE",
    "TOC": "OUTLINE",
    "CATALOG": "OUTLINE",
    # 项目概述 / 解析摘要
    "PROJECT_SUMMARY": "SUMMARY",
    "OVERVIEW": "SUMMARY",
    "PROJECT_OVERVIEW": "SUMMARY",
    # 图表
    "CHART_INSTRUCTION": "CHART_INSTRUCTIONS",
    "CHART_HINTS": "CHART_INSTRUCTIONS",
    # 行业
    "INDUSTRY": "INDUSTRY_PROFILE",
    # 承包范围
    "SCOPE": "PROJECT_SCOPE",
    # 工程量清单
    "BOQ": "BOQ_ANALYSIS",
    # 施工方法
    "METHOD": "CONSTRUCTION_METHOD",
    # 工作面接口
    "INTERFACE": "WORK_INTERFACE",
    # 其它章节
    "OTHER": "OTHER_CHAPTERS",
    "SIBLING_CHAPTERS": "OTHER_CHAPTERS",
}


def normalize_part_name(name: str) -> str:
    """把上下文片段名收敛到 DEFAULT_PRIORITIES 中的规范名。

    未知片段名原样返回（保持向后兼容，优先级回落到默认值 50）。
    """
    if not name:
        return name
    upper = name.strip().upper()
    return PART_ALIASES.get(upper, upper)


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

# PERF 2026-10-17：token 维度快速裁剪的调参。
# 跨边界 BPE 合并的安全余量：head|正文、正文|marker 两处，各最多合并出 1 token，
# 故取 2 即可覆盖；多留会白白浪费预算（实测余量 4 时每次少用 4~5 token）。
_MERGE_SAFETY_TOKENS = 2
# 回退步长（字符）/ 最大回退次数：把最坏情况下的额外全量编码次数控制在个位数
_REFINE_STEP = 64
_MAX_SHRINK_STEPS = 6
# 前进步长（字符）/ 最大前进次数：把安全余量吃回来，同时限制额外编码次数
_GROW_STEP = 8
_MAX_GROW_STEPS = 3


def _token_prefix_char_len(content: str, token_budget: int) -> int | None:
    """返回「前 token_budget 个 token 对应的字符数」；无法快速计算时返回 None。

    仅在 tiktoken 可用且确实需要截断时走这条快路径；否则返回 None 由调用方
    回落到字符维度二分（行为与修复前一致）。
    """
    if _TIKTOKEN_ENCODER is None or token_budget <= 0 or not content:
        return None
    try:
        ids = _TIKTOKEN_ENCODER.encode(content)
        if len(ids) <= token_budget:
            return len(content)
        # decode 前 k 个 token，其字符长度即所求切点（decode 与 encode 互逆，
        # 故 content.startswith(decode(ids[:k])) 成立，切点必然落在 token 边界）
        return len(_TIKTOKEN_ENCODER.decode(ids[:token_budget]))
    except Exception:
        return None


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

    # UNIFY 2026-10-17：片段名先归一化（别名 → 规范名），保证「异名同义」
    # 命中同一优先级，避免回落魔法默认 50。同名冲突时按原顺序后者覆盖前者
    # （与 dict 语义一致），并在下方按优先级稳定排序。
    items = [
        (normalize_part_name(name), content)
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
                # 边界片段已把剩余预算填满（或接近填满），更低优先级不再加入。
                break
            # 截断后无有效正文（极端小预算）→ 视为该片段不可容纳，落到下面继续。

        # BUG-FIX-PROMPT-C11（2026-10-17，预算浪费 + 低优先级误杀）：
        # 原实现在此**无条件 break**——只要遇到第一个放不下的片段，其后所有
        # 更低优先级片段统统丢弃，即便它们很短、完全装得进剩余预算。
        # 实测复现（max_tokens=40，HIGH 占 27 token，MID 超长，LOW 仅 8 token）：
        #     remain=12 < head(5)+marker(15)+1=21 → MID 无法容纳
        #     原实现直接 break → 产出 27 token，13 token 预算(32%)被浪费，
        #     且 LOW（8 token，装得下）被误杀。
        # 该行为与「按优先级最大化利用预算」的设计目标相悖：优先级只应决定
        # **谁先被满足**，不应导致预算闲置。改为跳过该片段、继续尝试后续
        # 更低优先级片段；已截断并加入的边界片段仍走上面的 break（预算已满）。
        continue

    return delimiter.join(kept)


#: PERF 2026-10-20：无 tiktoken 环境下，启发式计数的前缀统计块大小（字符）。
#: 建表成本 ≈ 全文 translate（C 速度）；查询成本 ≤ 一个块的 translate。
_HEURISTIC_PREFIX_CHUNK = 2048

#: 仅对达到该长度的正文构建前缀统计表；短文本一次 translate 比建表更便宜，
#: 且与历史逐字一致（零行为变化）。
_HEURISTIC_FAST_PATH_MIN_CHARS = 4096


def _cjk_noncjk(text: str) -> tuple[int, int]:
    """文本的 (CJK 字符数, 非 CJK 字符数)（与 estimate_tokens 启发式同口径）。"""
    non_cjk = len(text.translate(_CJK_DELETE_TABLE))
    return len(text) - non_cjk, non_cjk


def _build_cjk_prefix_heads(content: str) -> list[int]:
    """按块预计算「截至该块末尾的 CJK 字符数」累计表。

    配合 `_heuristic_fast_fits` 把二分查找的每次全量 translate 降为 O(1)。
    注意：token 合并取整必须按「总 CJK + (总非 CJK + 3)//4」一次性计算
    （见 _heuristic_fast_fits 的等价性说明），按段各自取整再相加会因小数
    累积产生 1 token 偏差。
    """
    heads = [0]
    for i in range(0, len(content), _HEURISTIC_PREFIX_CHUNK):
        chunk = content[i : i + _HEURISTIC_PREFIX_CHUNK]
        heads.append(heads[-1] + (len(chunk) - len(chunk.translate(_CJK_DELETE_TABLE))))
    return heads


def _heuristic_fast_fits(content: str, head: str, marker: str, budget_tokens: int):
    """构造与 ``estimate_tokens(head + content[:n].rstrip() + marker)`` **逐位等价**
    的 O(1) 版 _fits（仅无 tiktoken 环境启用）。

    等价性依据：estimate_tokens 启发式 = ``cjk + (non_cjk + 3) // 4``，其中
    cjk / non_cjk 是**拼接串**的字符分类计数；字符串拼接不改变单字符分类，
    故 cjk(concat) = Σcjk(段)、non_cjk(concat) = Σnon_cjk(段)，按总量一次
    取整即得与原实现完全相同的结果——是精确等价，不是近似。

    性能依据：原 _fits 每次对 content[:n] 做一次全量 translate + 字符串拼接，
    二分约 18 次迭代，在 24 万字符正文上实测整函数 ~0.35s（full-suite 负载下
    会越过 perf 测试的 0.4s 断言线）；本版建表一次（~40ms）+ 每次查询
    O(块大小)（~1ms），实测 ~0.06s。`truncate_to_tokens` 在长文档上被逐段
    反复调用，收益同享。

    Args:
        content: 待裁剪正文（调用方保证非空且长度 ≥ _HEURISTIC_FAST_PATH_MIN_CHARS）
        head / marker: 预算整块的固定头尾（其 token 成本只算一次）
        budget_tokens: 整块 token 预算

    Returns:
        ``fits(n: int) -> bool``：content[:n]（rstrip 后）配 head/marker 是否不超预算
    """
    heads = _build_cjk_prefix_heads(content)
    head_cjk, head_non = _cjk_noncjk(head)
    marker_cjk, marker_non = _cjk_noncjk(marker)
    chunk = _HEURISTIC_PREFIX_CHUNK

    def _fits(n: int) -> bool:
        # 与 content[:n].rstrip() 等价的后缀空白剥离：str.isspace() 与
        # str.rstrip() 使用同一 Unicode 空白定义（含 U+3000 等），逐字符等价。
        while n > 0 and content[n - 1].isspace():
            n -= 1
        if n <= 0:
            body_cjk, body_non = 0, 0
        else:
            base = (n // chunk) * chunk
            body_cjk = heads[n // chunk] + (
                (n - base) - len(content[base:n].translate(_CJK_DELETE_TABLE))
            )
            body_non = n - body_cjk
        total_cjk = head_cjk + body_cjk + marker_cjk
        total_non = head_non + body_non + marker_non
        return total_cjk + (total_non + 3) // 4 <= budget_tokens

    return _fits


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
    # 空 head/marker 且整段放得下：直接返回，避免进入二分。
    if not head and not marker and estimate_tokens(content) <= budget_tokens:
        return content.rstrip()

    def _fits(n: int) -> bool:
        return estimate_tokens(head + content[:n].rstrip() + marker) <= budget_tokens

    # PERF 2026-10-17（O(log n) 次全量编码 → 1 次编码 + 1 次解码）：
    #
    # 原实现在**字符维度**二分，每次 _fits(n) 都要对 content[:n] 重新编码。
    # 对 20 万字符的招标原文（facts 分段提取 / 上下文裁剪的真实体量）需 ~18
    # 次迭代 × 每次编码 57ms → 单次裁剪 ~1s；而 truncate_to_tokens 在长文档
    # 上会被逐段反复调用，累计开销可观（实测 200k 字符：0.98s → 0.19s）。
    #
    # 改为在 **token 维度**直接求解：把 content 全量编码一次得到 ids，预算扣除
    # head/marker 后即得到可容纳的 token 数 k（纯算术），decode(ids[:k]) 的
    # 字符长度即所求切点。随后用真实 _fits 校验一次并按步长回退，保证产出
    # **严格不超预算**（与旧实现同一保证）。
    enc = _TIKTOKEN_ENCODER
    ids = None
    if enc is not None:
        try:
            ids = enc.encode(content)
        except Exception:
            ids = None

    # PERF 2026-10-20（无 tiktoken 环境的二分查找加速）：tiktoken 不可用时
    # （本部署环境默认即如此，estimate_tokens 走启发式），原 _fits 每次都对
    # content[:n] 做全量 translate + 拼接，二分约 18 次在 24 万字符上 ~0.35s。
    # 启发式计数可按段精确分解（见 _heuristic_fast_fits 的等价性证明），此处
    # 把 _fits 升级为 O(1) 版。tiktoken 可用时 ids 非 None，走上方的 token
    # 维度快速路径，_fits 保持真实编码校验口径，不受本分支影响。
    if ids is None and len(content) >= _HEURISTIC_FAST_PATH_MIN_CHARS:
        _fits = _heuristic_fast_fits(content, head, marker, budget_tokens)

    if ids is not None:
        fixed = estimate_tokens(head + marker)
        if len(ids) + fixed <= budget_tokens:
            return content.rstrip()
        k = budget_tokens - fixed - _MERGE_SAFETY_TOKENS
        if k <= 0:
            return ""
        n = min(len(enc.decode(ids[:k])), len(content))
        # 校验 + 有限回退（BPE 跨边界合并时快速解可能略超预算）
        for _ in range(_MAX_SHRINK_STEPS):
            if _fits(n):
                break
            n -= _REFINE_STEP
            if n <= 0:
                return ""
        else:
            n = -1  # 回退次数用尽仍未满足 → 兜底二分
        if n >= 0:
            # 前进：安全余量通常不满，用小块步长吃回剩余预算（每步一次编码，故限次）
            grow = 0
            while grow < _MAX_GROW_STEPS and n + _GROW_STEP <= len(content) and _fits(n + _GROW_STEP):
                n += _GROW_STEP
                grow += 1
            return content[:n].rstrip() if n > 0 else ""
        # 回退仍不满足：落到字符二分兜底（极端语料，保证不返回超预算内容）
        lo, hi = 0, max(0, n)
    else:
        lo, hi = 0, len(content)

    while lo < hi:
        mid = (lo + hi + 1) // 2
        if _fits(mid):
            lo = mid
        else:
            hi = mid - 1
    return content[:lo].rstrip() if lo > 0 else ""

    # 以下分支不可达（上方已 return），仅为静态检查与可读性保留兜底。
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
    # BUG-FIX-PROMPT-C12（2026-10-17，None/非正整数预算崩溃）：原实现直接把
    # ``max_tokens`` 与 int 比较。调用方从配置/DB 读到的预算可能为 None
    # （未配置即不限）或 0/负数，实测 ``truncate_to_tokens(text, None)`` 抛
    # ``TypeError: '<=' not supported between 'int' and 'NoneType'``，
    # 在正文生成链路里表现为整个章节生成失败。现统一口径：
    #   - None  → 等同「不限」，原样返回（与 assemble_prompt_context 一致）；
    #   - <= 0  → 空串（无预算即无内容，语义明确且不抛异常）。
    if not text:
        return text
    if max_tokens is None:
        return text
    if max_tokens <= 0:
        return ""
    if estimate_tokens(text) <= max_tokens:
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
    "PART_ALIASES",
    "normalize_part_name",
    "estimate_tokens",
    "assemble_prompt_context",
    "truncate_to_tokens",
    "split_into_token_capped_chunks",
    "build_chapter_context",
]


def _default_context_max_tokens() -> int | None:
    """上下文组装的默认 Token 预算（0/None 表示不限，保持既有行为）。"""
    try:
        from .... import config

        value = int(getattr(config, "PROMPT_CONTEXT_MAX_TOKENS", 0) or 0)
    except Exception:
        return None
    return value if value > 0 else None


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
    max_tokens: int | None = None,
) -> str:
    """正文生成场景的统一上下文组装高层接口。

    ENHANCE 2026-10-17：``max_tokens`` 改为可选，未指定时读取
    ``config.PROMPT_CONTEXT_MAX_TOKENS``；该项为 0（默认）时表示不限，
    与修复前「默认 8000」不同——旧默认值会在无人察觉的情况下截断超长
    上下文，现交由运维显式配置。需要固定 8000 的调用方显式传参即可。

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
    return assemble_prompt_context(
        parts, max_tokens=max_tokens if max_tokens is not None else _default_context_max_tokens()
    )
