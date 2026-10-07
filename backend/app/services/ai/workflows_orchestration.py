"""
完整预编排 + 裁定 + prompt 对接实现

设计文档：标书智能体（六）—— 超长文本生成和图文控制
实现参考：标书智能体（三）—— 同级/上级上下文（已在上一轮实现）

4 步流水线：
  Step 1: orchestrate_content() — 为每个叶子节点调用 AI，返回 section_type + plan JSON
  Step 2: _adjudicate_illustrations() — 程序侧裁定 illustration_type（ai/mermaid/none）
  Step 3: sse_content.py — 把 plan + illustration_type 合并到 leaf_dicts
  Step 4: build_chapter_content_messages() — 注入 table_decision / image_decision
"""

import json
import asyncio
import logging
import re
import time
from typing import Any
from .providers.base import AIMessage
from .prompts import get_prompt, render_prompt

logger = logging.getLogger(__name__)


# ============================================================
# priority 语义常量（ENHANCE 2026-10-05，对齐易标规格「3 有价值候选 /
# 4 推荐 / 5 强推荐」）
#
# 为什么必须统一：此前通用编排提示词 _ORCH_SYSTEM 用的是 0-3 制
# （0=不需要, 1=有价值, 2=推荐, 3=强推荐），而各图表专用提示词
# （prompts/charts.py、prompts/mermaid.py）用的是 3-5 制。裁定函数
# _adjudicate_illustrations 直接对 priority 做数值比较排序，两条路径
# 产出的分值不可比；且该分值还会被 workflows_sections 原样写进正文提示词
# （"AI配图: 计划配图（优先级 N）"），让正文模型误判强度。
# 现统一为 3-5 制，「不需要配图」改由 needed=false + priority=0 表达。
# ============================================================

#: 有效 priority 区间（易标规格）
PRIORITY_MIN = 3
PRIORITY_MAX = 5
#: 不需要配图时的哨兵值
PRIORITY_NONE = 0


def normalize_priority(value: Any, needed: bool = True) -> int:
    """把任意来源的 priority 归一化到 [PRIORITY_MIN, PRIORITY_MAX]。

    ENHANCE 2026-10-05：裁定与下游注入都依赖 priority 的可比性，但历史数据
    （sections.*_json）里存在 0-3 制的旧值。这里做**保守归一化**：

      - needed=False → 0（不参与任何排序）
      - 值缺失/0     → 3（floor，「有价值候选」）
      - 1, 2         → 3（旧制「有价值/推荐」压到 floor，不放大）
      - 3, 4, 5      → 原值
      - 6 及以上     → 5（ceiling）

    旧制的「3=强推荐」会归一化为 3（新制「有价值候选」），即**降一档**。
    这是刻意的保守选择：宁可让强推荐项在分段择优时少赢一次，也不要把它
    提升成 5 去挤占真正强推荐的配额（多生成的每张 AI 图都是真金白银）。
    """
    if not needed:
        return PRIORITY_NONE
    try:
        v = int(value)
    except (TypeError, ValueError):
        return PRIORITY_MIN
    if v <= 0:
        return PRIORITY_MIN
    return max(PRIORITY_MIN, min(PRIORITY_MAX, v))


# ============================================================
# Step 1: orchestrate_content — 完整实现（替换空壳）
# ============================================================

# 编排提示词（扩展版，同时输出 section_type + table/mermaid/image 计划）
_ORCH_SYSTEM = """你是投标技术方案正文编排专家。请根据章节上下文判断：
1. 本章节内容类型（section_type）
2. 表格是否合适（table.needed + purpose）
3. Mermaid 图是否合适（mermaid.needed + priority + reason）
4. AI 生图是否合适（image.needed + priority + reason）

核心原则：
- 克制为美：能文字表达的不配图，不要为了形式硬插
- 一章节一图：mermaid 和 image 都可以提名，但最终系统会只选一种
- priority: 3=有价值候选, 4=推荐, 5=强推荐（只需给「确实需要配图」的项赋值；
  不需要配图时用 needed=false 且 priority=0，**不要**用 priority 表达「不需要」）
- section_type: text/table/flowchart/gantt/architecture/labor/comparison（同已实现的类型系统）

输出严格 JSON 数组，每个元素包含 section_id, title, recommended_type, confidence, reason,
以及 plan: {table:{needed,purpose}, mermaid:{needed,priority,reason}, image:{needed,priority,reason}}"""


_ORCH_USER_TEMPLATE = """### 项目概述
{project_overview}

### 叶子节点列表（JSON）
{leaf_list_json}

### 评分标准（摘要）
{scoring_summary}

### 要求
- 遍历所有叶子节点
- 返回严格 JSON 数组，不要任何解释或 Markdown
- 每个节点的 plan 字段同时给出 table/mermaid/image 三种判断"""


def _render_orch_user(template: str, proj_overview: str, leaf_json: str, scoring: str) -> str:
    """渲染编排用户提示词，兼容两种占位符风格。

    BUG-FIX 2026-09-14（高）：编排只认硬编码的 _ORCH_USER_TEMPLATE（{} 风格），
    提示词管理页里注册的 CONTENT_ORCHESTRATION_USER（__OUTLINE__ 风格）从未被
    使用 → 管理员改提示词不生效。这里按模板实际使用的占位符风格分别渲染。

    UNIFY 2026-10-06：原实现对手写 `.replace` 与 `.format` 两种风格分别处理，
    且与全平台统一的 `render_prompt` 单遍渲染口径不一致（手写 `.replace`
    存在跨占位符二次替换的注入面）。现统一收编到 `render_prompt`：它同时支持
    `{var}` 与 `__VAR__`（大小写不敏感、单遍替换），一次传入两套风格所需的全部
    变量名即可兼容 v2.0（__OUTLINE__ 风格）与历史回落模板（{leaf_list_json} 风格）；
    单遍替换还顺带消除了旧 `.replace` 链路的跨变量注入隐患。
    """
    if not template:
        template = _ORCH_USER_TEMPLATE
    overview = (proj_overview or "")[:1200]
    rendered = render_prompt(
        template,
        # v2.0（__OUTLINE__ 风格）所需
        OUTLINE=leaf_json,
        SCORING_STANDARDS=(scoring or "")[:1000],
        PROJECT_OVERVIEW=overview,
        SUMMARY=overview,
        # 历史回落模板（{leaf_list_json}/{scoring_summary}/{project_overview} 风格）所需
        LEAF_LIST_JSON=leaf_json,
        SCORING_SUMMARY=(scoring or "")[:1000],
    )

    # BUG-FIX 2026-09-14（高·补）：注册的 v2.0 模板只声明了 __OUTLINE__ 与
    # __SCORING_STANDARDS__，**没有项目概述占位符**。若直接切换过去，模型在做
    # 配图类型判断时会丢失"这是医院工程还是道路工程"的关键信息（旧硬编码模板
    # 是有的）。这里在模板未引用项目概述时补一段，避免修复引入质量回退。
    if overview and "项目概述" not in rendered:
        rendered += (
            "\n\n### 项目概述\n"
            f"{overview}\n"
            "（以上概述供判断章节与项目特征的契合度，不要据此编造数据）"
        )
    return rendered


def _build_leaf_list_json(outline: list, max_items: int = 0) -> str:
    """把 outline 转成精简 JSON（只保留编排必需的字段）"""
    leaves = []
    for item in outline:
        if not isinstance(item, dict):
            continue
        leaves.append({
            "id": str(item.get("id", "")),
            "title": item.get("title", ""),
            "parent_id": str(item.get("parent_id", "")) if item.get("parent_id") else None,
            "level": item.get("level", 3),
            "description": (item.get("description") or "")[:100],
            "path": item.get("path") or [],
            "ancestors": item.get("ancestors") or [],
        })
    if max_items and len(leaves) > max_items:
        leaves = leaves[:max_items]
    return json.dumps(leaves, ensure_ascii=False)


async def _orch_one_section(provider: Any, leaf: dict, sys_prompt: str,
                             proj_overview: str, scoring: str,
                             user_template: str = "") -> dict | None:
    """单章节编排调用（失败降级为 text + 空 plan，不阻塞整体）"""
    leaf_json = json.dumps([leaf], ensure_ascii=False)
    user_prompt = _render_orch_user(user_template, proj_overview, leaf_json, scoring)
    try:
        messages = [
            AIMessage(role="system", content=sys_prompt),
            AIMessage(role="user", content=user_prompt),
        ]
        raw = await provider.chat(messages, max_tokens=800, temperature=0.2)
        text = raw if isinstance(raw, str) else raw.get("content", str(raw))
        # 提取 JSON
        m = re.search(r'\[.*\]', text, re.DOTALL) or re.search(r'\{.*\}', text, re.DOTALL)
        if not m:
            return None
        data = json.loads(m.group())
        if isinstance(data, list):
            data = data[0] if data else None
        if not isinstance(data, dict):
            return None
        return data
    except Exception as e:
        logger.debug("[编排] 单章节编排失败 sid=%s: %s", leaf.get("id"), e)
        return None


async def orchestrate_content(provider: Any, outline: Any,
                               scoring_standards: str = "",
                               orch_sys: str = "", orch_user: str = "",
                               project_overview: str = "",
                               children_map: dict = None,
                               concurrency: int = 3,
                               cancel_event=None,
                               max_ai_images: int = 6,
                               table_ratio: str = "moderate",
                               prefer_mermaid: bool = True,
                               section_per_segment: int = 10) -> list:
    """完整正文编排（标书智能体六 / 文档三核心流程）

    Args:
        provider: AI 提供商
        outline: 叶子章节列表（每个 dict 含 id/title/parent_id/level/path/ancestors）
        scoring_standards: 评分标准 JSON 字符串
        orch_sys / orch_user: 可选自定义提示词（优先使用）
        project_overview: 项目概述
        children_map: 章节树结构（同级判断用）
        concurrency: 编排并发数
        cancel_event: 取消信号

    Returns:
        list[dict]: 每个叶子节点的编排结果，含:
          - section_id, title, recommended_type, confidence, reason
          - plan.table  {needed:bool, purpose:str}
          - plan.mermaid {needed:bool, priority:int, reason:str}
          - plan.image  {needed:bool, priority:int, reason:str}
          - illustration_type: "ai" | "mermaid" | "none"  （Step 2 裁定后填充）
    """
    # 防御：旧代码可能传 JSON 字符串而非 list
    if isinstance(outline, str):
        try:
            outline = json.loads(outline)
        except json.JSONDecodeError:
            return []
    if not isinstance(outline, list):
        return []
    if not outline:
        return []

    # BUG-FIX 2026-09-14（高）：orch_sys 默认取提示词注册表（支持后台自定义），
    # orch_user 之前**完全没被使用**（恒用 _ORCH_USER_TEMPLATE），
    # 导致提示词管理页里编辑的「内容编排」提示词永远不生效。
    sys_prompt = orch_sys or (get_prompt("CONTENT_ORCHESTRATION_SYSTEM") or _ORCH_SYSTEM)
    # 注意：这里要的是**未渲染的原始模板**（占位符交给 _render_orch_user 替换），
    # 所以把占位符原样传回去，既拿到模板又不会触发框架的 missing-variables 告警。
    user_template = orch_user or (
        get_prompt(
            "CONTENT_ORCHESTRATION_USER",
            OUTLINE="__OUTLINE__",
            SCORING_STANDARDS="__SCORING_STANDARDS__",
        )
        or _ORCH_USER_TEMPLATE
    )
    results: list[dict | None] = [None] * len(outline)
    semaphore = asyncio.Semaphore(max(1, concurrency))
    children_map = children_map or {}

    # 构建同级索引（传入 AI 能更准确判断不重复配图）
    siblings_by_id: dict[str, list[dict]] = {}
    for leaf in outline:
        pid = str(leaf.get("parent_id") or "")
        if not pid:
            continue
        sibs = [
            {"id": str(s.get("id")), "title": s.get("title", "")}
            for s in outline
            if str(s.get("parent_id") or "") == pid and str(s.get("id")) != str(leaf.get("id"))
        ]
        if sibs:
            siblings_by_id[str(leaf.get("id"))] = sibs

    async def _task(idx: int, leaf: dict):
        nonlocal results
        if cancel_event and cancel_event.is_set():
            results[idx] = _fallback_plan(leaf)
            return
        async with semaphore:
            siblings = siblings_by_id.get(str(leaf.get("id")), [])
            # 在 leaf dict 里加上 siblings 让 AI 看到
            leaf_with_sibs = dict(leaf)
            if siblings:
                leaf_with_sibs["siblings"] = siblings
            orch = await _orch_one_section(
                provider, leaf_with_sibs, sys_prompt,
                project_overview, scoring_standards,
                user_template=user_template,
            )
            if orch is None:
                orch = _fallback_plan(leaf)
            results[idx] = _normalize_plan(orch, leaf)

    # BUG-FIX 2026-09-16（中 编排整体失效）：gather 没开 return_exceptions —
    # 单个章节的 `_orch_one_section` / `_normalize_plan` 抛异常时，会连同整个
    # 编排一起失败（`results` 里其余下标为 None，调用方还要额外猜测原因）。
    # 现在逐项吸收异常并降级为 `_fallback_plan`，保证「一章拖后腿」不影响全篇。
    _task_results = await asyncio.gather(
        *[asyncio.create_task(_task(i, leaf)) for i, leaf in enumerate(outline)],
        return_exceptions=True,
    )
    for _res_idx, _res in enumerate(_task_results):
        if isinstance(_res, BaseException):
            logger.warning(
                "[编排] 第 %d 个章节编排异常，降级为纯文字: %r", _res_idx, _res
            )
            results[_res_idx] = _fallback_plan(outline[_res_idx])

    results = [r for r in results if r is not None]

    # Step 2: illustration_type 裁定
    # ENHANCE 2026-10-05（P-11）：把编排参数透传到裁定层。
    # 旧实现是裸调用 `_adjudicate_illustrations(results)`，四个参数全部取
    # 签名默认值，导致 max_ai_images / table_ratio / prefer_mermaid 虽已实现
    # 却无从配置。
    results = _adjudicate_illustrations(
        results,
        max_ai_images=max_ai_images,
        table_ratio=table_ratio,
        prefer_mermaid=prefer_mermaid,
        section_per_segment=section_per_segment,
    )

    return results


def _fallback_plan(leaf: dict) -> dict:
    """AI 调用失败时的降级编排（text + 空 plan）"""
    return {
        "section_id": str(leaf.get("id", "")),
        "title": leaf.get("title", ""),
        "recommended_type": "text",
        "confidence": 0.3,
        "reason": "AI 编排失败，降级为纯文字",
        "plan": {
            "table": {"needed": False, "purpose": ""},
            "mermaid": {"needed": False, "priority": 0, "reason": ""},
            "image": {"needed": False, "priority": 0, "reason": ""},
        },
        "illustration_type": "none",
    }


def _normalize_plan(orch: dict, leaf: dict) -> dict:
    """把 AI 返回的编排 JSON 规整成统一 schema"""
    sid = str(orch.get("section_id") or leaf.get("id") or "")
    plan_raw = orch.get("plan") or {}
    tbl_raw = plan_raw.get("table") or {"needed": False, "purpose": ""}
    mm_raw = plan_raw.get("mermaid") or {"needed": False, "priority": 0, "reason": ""}
    img_raw = plan_raw.get("image") or {"needed": False, "priority": 0, "reason": ""}

    # ENHANCE 2026-10-05：priority 统一归一化到 3-5 制（见 normalize_priority）。
    # 必须在**这里**归一，而不是各调用点自行处理——否则裁定函数比较的
    # 仍是混着 0-3 / 3-5 两套语义的原始值。
    def _nblock(raw: dict) -> dict:
        out = dict(raw)
        needed = bool(out.get("needed"))
        out["needed"] = needed
        out["priority"] = normalize_priority(out.get("priority"), needed=needed)
        out.setdefault("reason", "")
        return out

    return {
        "section_id": sid,
        "title": orch.get("title") or leaf.get("title", ""),
        "recommended_type": orch.get("recommended_type", "text"),
        "confidence": orch.get("confidence", 0.5),
        "reason": orch.get("reason", ""),
        "plan": {
            "table": tbl_raw,           # table 无 priority 字段（易标规格同此）
            "mermaid": _nblock(mm_raw),
            "image": _nblock(img_raw),
        },
        "illustration_type": None,  # Step 2 填充
    }


# ============================================================
# Step 2: _adjudicate_illustrations — 程序侧裁定 illustration_type
#
# 设计文档六：AI 提名多候选，但最终程序拍板。
# 规则：
#   a) 按 section 在全文中的位置分段（每 8~12 章一段），避免头重脚轻
#   b) 每段内择优（priority 最高的 image 或 mermaid）
#   c) 遵守全局硬上限：默认最多 6 张 AI 图 / 全文（可配置）
#   d) 同章节若 mermaid 和 image 都 needed，只选 priority 高的那个
# ============================================================

def _adjudicate_illustrations(plans: list[dict],
                                max_ai_images: int = 6,
                                section_per_segment: int = 10,
                                table_ratio: str = "moderate",
                                prefer_mermaid: bool = True) -> list[dict]:
    """为每个编排结果裁定 illustration_type（ai / mermaid / none）

    ENHANCE 2026-10-05：
      - priority 统一 3-5 制，候选收集时再做一次归一化（防御从 DB 直接读入
        未归一的历史数据，见 normalize_priority）。
      - 新增 `prefer_mermaid` 开关（易标规格六.4「mermaid 优先于 image」）：
        True（默认）= 同章节两者都 needed 时 mermaid 先占位；
        False = 按 priority 高者胜出（商务标等需要照片的场景可关掉）。
      - `max_ai_images <= 0` 表示禁用 AI 生图（等价于 useAiImages=false）。
    """
    n = len(plans)
    if n == 0:
        return plans

    # BUG-FIX 2026-10-05：裁定前统一重置 illustration_type。
    # 旧实现只在「兜底」阶段给 None 的项补 "none"，于是对**已裁定过的 plans**
    # 重新调用时（例如用户改了 max_ai_images / table_ratio 后再编排一次，
    # 或从 DB 读回的 sections.*_json 直接送裁定），上一轮的 "ai"/"mermaid"
    # 会残留下来——参数收紧了，旧图却依然被保留。
    for _p in plans:
        _p["illustration_type"] = None

    # === 第一阶段：收集所有候选 ===
    mermaid_candidates = []  # [(idx, priority)]
    image_candidates = []    # [(idx, priority)]
    for idx, p in enumerate(plans):
        plan = p.get("plan", {})
        mm = plan.get("mermaid") or {}
        im = plan.get("image") or {}
        if mm.get("needed"):
            pr = normalize_priority(mm.get("priority"), needed=True)
            mermaid_candidates.append((idx, pr))
        if im.get("needed"):
            pr = normalize_priority(im.get("priority"), needed=True)
            image_candidates.append((idx, pr))

    # === 第二阶段：分段择优（避免图片集中在前面）===
    segments = max(1, n // section_per_segment + (1 if n % section_per_segment else 0))

    def _segment_of(idx: int) -> int:
        return min(segments - 1, idx // section_per_segment)

    # mermaid 裁定（不占 AI 图配额）
    if prefer_mermaid:
        for seg in range(segments):
            seg_mm = [(i, pr) for i, pr in mermaid_candidates if _segment_of(i) == seg]
            if seg_mm:
                seg_mm.sort(key=lambda x: -x[1])
                # 每段最多 1 张 mermaid
                best_idx = seg_mm[0][0]
                if plans[best_idx]["plan"]["mermaid"].get("needed"):
                    plans[best_idx]["illustration_type"] = "mermaid"

    # AI 图裁定（硬上限 max_ai_images，分段均匀分配）
    # 仅在 prefer_mermaid=True 时走这条路径：mermaid 先占位，image 在
    # 剩余空位里按段补。prefer_mermaid=False 时两类合并竞争同一配额，
    # 见下方 merged 分支——两条路径互斥，否则会双双执行、突破 max_ai_images。
    if prefer_mermaid and max_ai_images > 0:
        ai_quota_per_seg = max(1, max_ai_images // segments)
        remaining_quota = max_ai_images
        for seg in range(segments):
            if remaining_quota <= 0:
                break
            seg_im = [(i, pr) for i, pr in image_candidates
                      if _segment_of(i) == seg
                      and plans[i].get("illustration_type") is None]  # 已占位的跳过
            if not seg_im:
                continue
            seg_im.sort(key=lambda x: -x[1])
            seg_pick_count = min(ai_quota_per_seg, len(seg_im), remaining_quota)
            for pick_idx, _ in seg_im[:seg_pick_count]:
                plans[pick_idx]["illustration_type"] = "ai"
                remaining_quota -= 1

    # 关闭 prefer_mermaid 时，两类候选合并按 priority 竞争同一配额
    if not prefer_mermaid and max_ai_images > 0:
        merged = [(i, pr, "mermaid") for i, pr in mermaid_candidates] + \
                 [(i, pr, "image") for i, pr in image_candidates]
        merged.sort(key=lambda x: (-x[1], 0 if x[2] == "image" else 1))
        remaining = max_ai_images
        for idx, _pr, kind in merged:
            if plans[idx].get("illustration_type") is not None:
                continue
            if remaining <= 0:
                break
            plans[idx]["illustration_type"] = "ai" if kind == "image" else "mermaid"
            remaining -= 1

    # === 第三阶段：table 裁定 — 按 table_ratio 控制 ===
    # none / light / moderate / heavy（易标规格六.5：少量 20% / 适中 40% / 大量宽松）
    ratio_map = {"none": 0.0, "light": 0.2, "moderate": 0.4, "heavy": 0.7}
    ratio = ratio_map.get(table_ratio or "moderate", 0.4)
    max_tables = max(1, int(n * ratio)) if n else 0

    # 收集所有 table 候选（不分 illustration，table 是正文内嵌的）
    table_candidates = []
    for idx, p in enumerate(plans):
        tbl = p.get("plan", {}).get("table") or {}
        if tbl.get("needed"):
            mm_pr = normalize_priority((p.get("plan", {}).get("mermaid") or {}).get("priority"),
                                       needed=bool((p.get("plan", {}).get("mermaid") or {}).get("needed")))
            im_pr = normalize_priority((p.get("plan", {}).get("image") or {}).get("priority"),
                                       needed=bool((p.get("plan", {}).get("image") or {}).get("needed")))
            # BUG-FIX 2026-10-05（D-2）：旧实现读 tbl.get("priority")，但 table 的
            # plan 结构只有 {needed, purpose}，**从来没有 priority 字段**——该分支
            # 恒为 1，排序退化成稳定排序（原序），「有配图则表格降权」实际只靠
            # mm_pr/im_pr 生效。现改为固定的基础权重 1.0，只保留图竞争带来的降权。
            # max_pr 归一化后取值 3-5，除以 5 得到 0.6-1.0 的降权幅度：
            # 强配图章节的表格会被最先砍掉，纯文字章节的表格最先保留。
            effective = 1.0 - max(mm_pr, im_pr) * 0.2
            table_candidates.append((idx, effective))

    table_candidates.sort(key=lambda x: -x[1])
    for idx, _ in table_candidates[max_tables:]:
        # 超限的 table 降级为不需要
        plans[idx]["plan"]["table"]["needed"] = False

    # === 第四阶段：兜底 — 都不需要的 illustration_type 设为 none ===
    for p in plans:
        if p.get("illustration_type") is None:
            p["illustration_type"] = "none"

    return plans


# ============================================================
# 辅助：把 illustration_type 结果反向写回 leaf_dict
# 供正文生成时 prompt 注入使用
# ============================================================

def apply_orchestration_to_leaves(leaf_dicts: list[dict],
                                   orch_results: list[dict]) -> None:
    """把编排结果合并到 leaf_dicts（原地修改）

    对齐方式：先按 section_id，再按 title 兜底
    """
    by_id: dict[str, dict] = {}
    by_title: dict[str, dict] = {}
    for item in orch_results:
        sid = str(item.get("section_id", ""))
        if sid:
            by_id[sid] = item
        title = item.get("title", "")
        if title:
            by_title[title.strip()] = item

    for leaf in leaf_dicts:
        matched = by_id.get(str(leaf.get("id", "")))
        if matched is None:
            matched = by_title.get(str(leaf.get("title", "")).strip())
        if matched is not None:
            leaf["plan"] = matched.get("plan", {})
            leaf["illustration_type"] = matched.get("illustration_type", "none")
            # 兼容旧 section_type 字段
            st = matched.get("recommended_type")
            if st:
                leaf["section_type"] = st
