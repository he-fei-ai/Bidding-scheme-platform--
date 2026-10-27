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
# 2026-10-20 易标机制引入（规格二.5）：参考条目截断长度配置化，
# 默认值 = 历史硬编码值（60/120），不改变现有行为。
from app.config import KNOWLEDGE_CATALOG_SUMMARY_MAX, KNOWLEDGE_CATALOG_TITLE_MAX

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

#: 编排输出契约（程序强制，最高优先级）。
#:
#: 为什么必须有它：编排 system 提示词**允许管理员自定义**（DB 注册表优先），
#: 历史版本 / 管理员改写的提示词只输出 {section_id, recommended_type, confidence,
#: reason, type_sub}，**根本没有 plan 字段** → `_normalize_plan` 拿到空 plan →
#: table/mermaid/image 判定全部为空 → 预编排机制静默失效（外部文档所称
#: 「配图不是正文写完后再加，而是正文生成前先规划好」完全落空）。
#:
#: 外部引用软件的标准是「AI 负责提名、程序负责拍板」，提名结构必须由**程序**
#: 兜底保证，不能指望提示词自觉。因此这里把契约作为**常量块**追加到 system
#: 提示词末尾：常量 → 同项目所有叶子共享同一 system 前缀 → 不破坏 prompt cache。
_ORCH_OUTPUT_CONTRACT = """

【输出契约（程序强制，最高优先级）】
无论上文要求何种输出格式，最终**只能返回一个 JSON 对象**，且必须包含 plan 字段：
{
  "section_id": "与输入一致的章节ID",
  "title": "章节标题",
  "recommended_type": "text|table|flowchart|gantt|architecture|labor|comparison",
  "confidence": 0.0~1.0,
  "reason": "判断理由（不超过40字）",
  "plan": {
    "table":   {"needed": true或false, "purpose": "表格在本节要表达什么"},
    "mermaid": {"needed": true或false, "title": "图标题", "code": "合法 Mermaid 代码（不含代码围栏）", "priority": 3或4或5, "reason": "理由"},
    "chart":   {"needed": true或false, "type": "gantt|architecture|labor|comparison|layout|timeline", "title": "图标题", "data": {"结构化数据": "含真实字段与数值"}, "priority": 3或4或5, "reason": "理由"},
    "image":   {"needed": true或false, "style": "engineering_diagram或realistic_photo", "title": "图标题", "prompt": "中文生图提示词", "priority": 3或4或5, "reason": "理由"},
    "knowledge": {"item_ids": ["只能来自『可用知识素材清单』的ID"]},
    "facts": {"titles": ["只能来自『全局事实标题清单』的标题"]}
  }
}
契约规则：
1. 只输出 JSON，不要输出解释、总结或 Markdown 代码围栏；
2. priority 含义：3=有价值候选、4=推荐、5=强推荐；**只有 priority≥3 时才允许把 needed 设为 true**；不需要时 needed=false 且 priority=0；
3. 克制：能文字表达的不配图，不要为了形式硬插；同一章节 mermaid/chart/image 最多提名两类，最终由程序拍板只保留一种；
4. mermaid 只适合简单抽象的文本节点关系图（少量节点的流程/层级/时间线/职责关系）；code 必须是可直接渲染的合法 Mermaid：优先 flowchart TD、节点 ID 只用 ASCII 字母数字下划线、中文标签写成 A["中文标签"]、不用分号、不用多节点连接简写、不加代码围栏；
5. chart 只用于有明确数据支撑的结构化图表（进度任务依赖→gantt、组织管理层级→architecture、工种人数→labor、量化对比→comparison、平面布置→layout、时间节点→timeline）；data 必须给出真实字段与数值，不得编造；
6. image 只在有具象对象（设备、现场、空间关系、实物示意）时提名，prompt 用中文描述画面；
7. knowledge.item_ids 与 facts.titles 只能从给定清单中选择，不得编造；没有相关条目给空数组；
8. 不要要求用满任何数量上限，没有合适对象时不要硬插。
"""


def _build_orch_system_prompt(base_prompt: str) -> str:
    """给编排 system 提示词追加**输出契约**（程序强制提名结构）。

    契约为常量块 → 同项目所有叶子共享同一 system 前缀 → prompt cache 不受影响。
    """
    contract = _ORCH_OUTPUT_CONTRACT
    try:
        from app.config import ORCHESTRATION_CHART_ENABLED as _CHART_ON
    except Exception:  # pragma: no cover - 配置读取兜底
        _CHART_ON = True
    if not _CHART_ON:
        # 关闭数据型图表提名时，契约里同步去掉 chart 项（避免模型提名后被程序丢弃）
        contract = contract.replace(
            '    "chart":   {"needed": true或false, "type": "gantt|architecture|labor|comparison|layout|timeline", '
            '"title": "图标题", "data": {"结构化数据": "含真实字段与数值"}, "priority": 3或4或5, "reason": "理由"},\n',
            "",
        )
    base = base_prompt or ""
    if "【输出契约（程序强制，最高优先级）】" in base:
        return base
    return base.rstrip() + contract


# 编排提示词（扩展版，同时输出 section_type + table/mermaid/image 计划）
_ORCH_SYSTEM = """你是投标技术方案正文编排专家。请根据章节上下文判断：
1. 本章节内容类型（section_type）
2. 表格是否合适（table.needed + purpose）
3. Mermaid 图是否合适（mermaid.needed + priority + reason）
4. AI 生图是否合适（image.needed + priority + reason）
5. 本章节应参考哪些知识库素材（knowledge.item_ids，从用户提供的素材清单中选择）
6. 本章节需要哪些全局事实（facts.titles，只从用户提供的「全局事实标题清单」中选标题，
   不要输出任何具体事实内容）

核心原则：
- 克制为美：能文字表达的不配图，不要为了形式硬插
- 一章节一图：mermaid 和 image 都可以提名，但最终系统会只选一种
- priority: 3=有价值候选, 4=推荐, 5=强推荐（只需给「确实需要配图」的项赋值；
  不需要配图时用 needed=false 且 priority=0，**不要**用 priority 表达「不需要」）
- section_type: text/table/flowchart/gantt/architecture/labor/comparison（同已实现的类型系统）
- knowledge.item_ids: 只能从「可用知识素材清单」给出的 ID 中选择，只选与本章节
  内容真正相关的条目（通常 0~3 条），没有相关素材就给空数组，不要硬凑；
  素材 ID 必须原样引用，禁止编造清单中不存在的 ID
- facts.titles: 只能从「全局事实标题清单」中选择标题（通常 0~8 条，涉及工期/人员/
  质保/服务承诺的章节应选上对应事实），未提供清单或无需要时给空数组；
  编排判断必须结合事实标题避免时间、地点、人员、设备、标准或服务承诺前后不一致的表达

输出严格 JSON 数组，每个元素包含 section_id, title, recommended_type, confidence, reason,
以及 plan: {table:{needed,purpose}, mermaid:{needed,priority,reason}, image:{needed,priority,reason},
knowledge:{item_ids:[...]}, facts:{titles:[...]}}"""


_ORCH_USER_TEMPLATE = """### 项目概述
{project_overview}

### 叶子节点列表（JSON）
{leaf_list_json}

### 评分标准（摘要）
{scoring_summary}

### 可用知识素材清单（ID: 标题 — 摘要）
{knowledge_catalog}

### 要求
- 遍历所有叶子节点
- 返回严格 JSON 数组，不要任何解释或 Markdown
- 每个节点的 plan 字段同时给出 table/mermaid/image 三种判断
- knowledge.item_ids 只从「可用知识素材清单」中选择，无相关素材给空数组"""


def _render_orch_user(template: str, proj_overview: str, leaf_json: str, scoring: str,
                      knowledge_catalog: str = "",
                      fact_titles_text: str = "",
                      leaf_marker: str = "") -> str:
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

    ENHANCE 2026-10-09（E-2）：新增 knowledge_catalog（可用知识素材清单文本）。
    ENHANCE 2026-10-14（facts-ns P0）：新增 fact_titles_text（全局事实标题清单，
    对齐易标 globalFactTitlesText：编排只选标题，不输出事实内容）。
    """
    if not template:
        template = _ORCH_USER_TEMPLATE
    overview = (proj_overview or "")[:1200]
    # 标书智能体（4）：leaf_marker 非空时叶子列表渲染为哨兵串（供缓存友好拆分），
    # 拆分后哨兵即被真实数据替换，不会发给模型。
    _leaf_value = leaf_marker or leaf_json
    rendered = render_prompt(
        template,
        # v2.0（__OUTLINE__ 风格）所需
        OUTLINE=_leaf_value,
        SCORING_STANDARDS=(scoring or "")[:1000],
        PROJECT_OVERVIEW=overview,
        SUMMARY=overview,
        # 历史回落模板（{leaf_list_json}/{scoring_summary}/{project_overview} 风格）所需
        LEAF_LIST_JSON=_leaf_value,
        SCORING_SUMMARY=(scoring or "")[:1000],
        # E-2：知识素材清单（两种占位符风格统一供给）
        KNOWLEDGE_CATALOG=knowledge_catalog or "（无可用知识素材）",
        # P0（facts-ns）：全局事实标题清单（未提供时给空，模板无此占位符也不报错）
        FACTS_TITLES=fact_titles_text,
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
    # E-2：自定义模板没有知识素材占位符时同样补一段，保证 knowledge 通道
    # 在管理员自定义提示词的场景下不静默失效。
    if knowledge_catalog and "知识素材" not in rendered:
        rendered += (
            "\n\n### 可用知识素材清单（ID: 标题 — 摘要）\n"
            f"{knowledge_catalog}\n"
            "（knowledge.item_ids 只能从上述清单中选择，无相关素材给空数组）"
        )
    # P0（facts-ns 2026-10-14）：同 E-2 思路——自定义模板没有事实标题占位符时
    # 补一段，保证 facts.titles 通道在管理员自定义提示词场景下不静默失效。
    if fact_titles_text and "全局事实标题" not in rendered:
        rendered += (
            "\n\n### 全局事实标题清单（编排时只能选择标题，不要输出具体事实内容）\n"
            f"{fact_titles_text}\n"
            "（plan.facts.titles 只能从上述标题中选择，只选本章节确实需要的，"
            "通常 0~8 条；无需要给空数组）"
        )
    return rendered


#: 编排模板拆分的易变占位符哨兵（标书智能体（4）缓存友好拆分用）。
#: 只在渲染-拆分过程中存在，拆分后即被真实叶子列表替换，不会发给模型。
_LEAF_LIST_MARKER = "@@LEAF_LIST_MARKER@@"


def _build_orch_user_messages(template: str, proj_overview: str, leaf_json: str,
                              scoring: str, knowledge_catalog: str = "",
                              fact_titles_text: str = "") -> list[str]:
    """把编排用户提示词拆成「稳定上下文前置、任务差异后置」的多条 user 消息。

    标书智能体（4）——提示词顺序优化（缓存命中，冲突时以外部引用标准为准）：
    ``_orch_one_section`` 每个叶子调用一次（几十次/项目），旧实现把项目概述 /
    评分标准 / 素材清单 / 判断规则全部拼进单条 user，且 v2.0 模板里**易变的
    叶子列表排在模板最前面**（「输入数据」段在「判断规则/输出格式」之前）
    → 每次调用前缀在消息起始处就漂移，稳定规则完全无法命中缓存。

    改法：以哨兵占位符渲染模板（复用 _render_orch_user 的全部补段逻辑），
    再按哨兵切分为：
      [稳定前段]（叶子列表之前的模板文本，如无则为空）
      [稳定后段]（判断规则/输出格式/自检/项目概述补段/素材清单/事实标题）
      [任务差异]（当前叶子的 JSON + 待编排说明，放最后）
    同一项目下所有叶子共享前两条（逐字节一致），只有最后一条变化。
    """
    from .prompt_order import split_rendered_template

    rendered = _render_orch_user(
        template, proj_overview, "", scoring,
        knowledge_catalog=knowledge_catalog,
        fact_titles_text=fact_titles_text,
        leaf_marker=_LEAF_LIST_MARKER,
    )
    before, after = split_rendered_template(rendered, _LEAF_LIST_MARKER)
    blocks = [b for b in (before.strip(), after.strip()) if b]
    blocks.append(
        "### 待编排章节（JSON）\n"
        f"{leaf_json}\n\n"
        "请针对该章节输出编排决策 JSON（判断规则与输出格式同上文）。"
    )
    return blocks


def _build_knowledge_catalog_text(catalog: list | None) -> str:
    """把知识素材目录渲染成编排提示词用的稳定文本（E-2）。

    稳定性要求：按 id 升序、逐行固定格式 —— 同一项目同一批素材多次请求的
    前缀必须完全一致，否则编排请求的 prompt cache 前缀会被打破。
    """
    items = []
    for it in catalog or []:
        if not isinstance(it, dict):
            continue
        try:
            iid = int(it.get("id"))
        except (TypeError, ValueError):
            continue
        # 2026-10-20 截断长度配置化（原硬编码 [:60]/[:120]，默认值不变）
        title = str(it.get("title") or "").strip()[:KNOWLEDGE_CATALOG_TITLE_MAX]
        summary = str(it.get("summary") or "").strip()[:KNOWLEDGE_CATALOG_SUMMARY_MAX]
        items.append((iid, title, summary))
    if not items:
        return ""
    items.sort(key=lambda x: x[0])
    return "\n".join(f"K{iid}: {title} — {summary}" for iid, title, summary in items)


def _extract_knowledge_ids(plan_raw: dict, allowed_ids: set | None) -> list[int]:
    """从编排 plan 里提取 knowledge.item_ids，过滤到白名单（E-2）。

    AI 编造的 ID 一律丢弃（与补丁 section_id 越界丢弃同一安全思路），
    去重保序，上限 3 条防止单节素材注入失控。
    """
    kn = plan_raw.get("knowledge") or {}
    raw_ids = kn.get("item_ids") if isinstance(kn, dict) else None
    if not isinstance(raw_ids, list):
        return []
    out: list[int] = []
    for rid in raw_ids:
        try:
            iid = int(rid)
        except (TypeError, ValueError):
            continue
        if allowed_ids is not None and iid not in allowed_ids:
            continue
        if iid not in out:
            out.append(iid)
        if len(out) >= 3:
            break
    return out


def _build_fact_titles_text(titles: list | None) -> str:
    """把事实标题清单渲染成编排提示词用的稳定文本（P0，facts-ns 2026-10-14）。

    稳定性要求与知识素材目录同理：同一项目同一批事实多次请求的文本必须
    完全一致，不破坏编排请求的 prompt cache 前缀。
    """
    if not titles:
        return ""
    return "\n".join(f"- {str(t).strip()}" for t in titles if str(t).strip())


def _extract_fact_titles(plan_raw: dict, max_items: int = 12) -> list[str]:
    """从编排 plan 里提取 facts.titles（P0，facts-ns 2026-10-14）。

    容错两种形态：`facts: {titles: [...]}`（标准）与 `facts: [...]`（AI 简写）。
    去重保序、限长；标题是否真实存在由生成侧 `resolve_facts_by_titles` 统一
    白名单过滤（事实集本身即白名单，清单外标题一律丢弃）。
    """
    facts_raw = plan_raw.get("facts")
    if isinstance(facts_raw, dict):
        titles = facts_raw.get("titles")
    elif isinstance(facts_raw, list):
        titles = facts_raw
    else:
        return []
    if not isinstance(titles, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for t in titles:
        s = str(t or "").strip()
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s)
        if len(out) >= max_items:
            break
    return out


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
                             user_template: str = "",
                             knowledge_catalog: str = "",
                             fact_titles_text: str = "") -> dict | None:
    """单章节编排调用（失败降级为 text + 空 plan，不阻塞整体）"""
    try:
        # 标书智能体（4）——提示词顺序优化（缓存命中）：编排是每叶子一次的
        # 高频调用，项目概述/评分标准/素材清单/判断规则对所有叶子一致，
        # 拆成独立 user 消息前置，只有「当前叶子 JSON」放最后一条 →
        # 同项目所有叶子共享稳定前缀，命中服务商 Prompt Cache。
        # 开关 ORCHESTRATION_PROMPT_LAYERS_ENABLED（默认 true）；false 时
        # 保持旧的单条 user 行为（内容一致，仅消息条数不同）。
        try:
            from app.config import ORCHESTRATION_PROMPT_LAYERS_ENABLED as _LAYERS_ON
        except Exception:  # pragma: no cover - 配置读取兜底
            _LAYERS_ON = True
        # 标书智能体（6）：system 提示词追加**输出契约**（程序强制 plan 结构），
        # 并按开关放开 token 预算（要 mermaid.code / chart.data 时 800 不够用）。
        sys_prompt = _build_orch_system_prompt(sys_prompt)
        try:
            from app.config import ORCHESTRATION_PLAN_PAYLOAD_ENABLED as _PAYLOAD_ON
        except Exception:  # pragma: no cover - 配置读取兜底
            _PAYLOAD_ON = True
        _max_tokens = 1600 if _PAYLOAD_ON else 800
        if _LAYERS_ON:
            from .prompt_order import build_cache_friendly_messages

            user_blocks = _build_orch_user_messages(
                user_template, proj_overview, json.dumps([leaf], ensure_ascii=False),
                scoring,
                knowledge_catalog=knowledge_catalog,
                fact_titles_text=fact_titles_text,
            )
            messages = build_cache_friendly_messages(sys_prompt, user_blocks, "")
        else:
            user_prompt = _render_orch_user(
                user_template, proj_overview, json.dumps([leaf], ensure_ascii=False),
                scoring,
                knowledge_catalog=knowledge_catalog,
                fact_titles_text=fact_titles_text,
            )
            messages = [
                AIMessage(role="system", content=sys_prompt),
                AIMessage(role="user", content=user_prompt),
            ]
        raw = await provider.chat(messages, max_tokens=_max_tokens, temperature=0.2)
        # 标书智能体（4）缓存可观测性：按任务类型记账（fail-soft）。
        try:
            from .cache_metrics import record_provider_cache_usage

            record_provider_cache_usage(
                provider, task_type="orchestration",
                section_id=str(leaf.get("id", "")),
            )
        except Exception:  # pragma: no cover - 防御性
            pass
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
                               section_per_segment: int = 10,
                               knowledge_catalog: list | None = None,
                               fact_titles: list | None = None) -> list:
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
        knowledge_catalog: 可选知识素材目录（E-2），
            每项 {id:int, title:str, summary:str}；非空时编排提示词附带素材清单，
            AI 从中挑选 knowledge.item_ids，程序侧白名单过滤后写回 plan.knowledge
        fact_titles: 可选全局事实标题清单（P0，facts-ns 2026-10-14，对齐易标
            globalFactTitlesText）；非空时编排提示词附带标题清单，AI 输出
            plan.facts.titles，生成侧按标题取完整事实注入（编排只看标题、
            不输出事实内容）；缺省/空列表时 facts.titles 恒为空，行为与今天一致

    Returns:
        list[dict]: 每个叶子节点的编排结果，含:
          - section_id, title, recommended_type, confidence, reason
          - plan.table  {needed:bool, purpose:str}
          - plan.mermaid {needed:bool, priority:int, reason:str}
          - plan.image  {needed:bool, priority:int, reason:str}
          - plan.knowledge {item_ids:[int]} （E-2；无知识通道时为空列表）
          - plan.facts {titles:[str]} （P0，facts-ns；无事实通道时为空列表）
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
    # ENHANCE 2026-10-12：在 V2.0 (CONTENT_ORCHESTRATION_SYSTEM) 之后追加 legacy
    # 路径 (CONTENT_ORCHESTRATION_LEGACY_SYSTEM)，使存量提示词也可通过注册表覆盖。
    sys_prompt = orch_sys or (
        get_prompt("CONTENT_ORCHESTRATION_SYSTEM")
        or get_prompt("CONTENT_ORCHESTRATION_LEGACY_SYSTEM")
        or _ORCH_SYSTEM
    )
    # 注意：这里要的是**未渲染的原始模板**（占位符交给 _render_orch_user 替换），
    # 所以把占位符原样传回去，既拿到模板又不会触发框架的 missing-variables 告警。
    # ENHANCE 2026-10-12：追加 legacy user 模板路径。
    user_template = orch_user or (
        get_prompt(
            "CONTENT_ORCHESTRATION_USER",
            OUTLINE="__OUTLINE__",
            SCORING_STANDARDS="__SCORING_STANDARDS__",
        )
        or get_prompt("CONTENT_ORCHESTRATION_LEGACY_USER")
        or _ORCH_USER_TEMPLATE
    )
    results: list[dict | None] = [None] * len(outline)
    semaphore = asyncio.Semaphore(max(1, concurrency))
    children_map = children_map or {}

    # E-2：知识素材通道 —— 目录文本（稳定格式）+ 白名单（AI 编造 ID 一律丢弃）
    knowledge_catalog_text = _build_knowledge_catalog_text(knowledge_catalog)
    allowed_knowledge_ids: set | None = None
    if knowledge_catalog:
        allowed_knowledge_ids = set()
        for it in knowledge_catalog:
            if isinstance(it, dict):
                try:
                    allowed_knowledge_ids.add(int(it.get("id")))
                except (TypeError, ValueError):
                    continue

    # P0（facts-ns 2026-10-14）：事实标题清单通道（只看标题选事实，同易标）
    fact_titles_text = _build_fact_titles_text(fact_titles)

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
                knowledge_catalog=knowledge_catalog_text,
                fact_titles_text=fact_titles_text,
            )
            if orch is None:
                orch = _fallback_plan(leaf)
            results[idx] = _normalize_plan(orch, leaf,
                                           allowed_knowledge_ids=allowed_knowledge_ids)

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
            "mermaid": {"needed": False, "priority": 0, "reason": "", "title": "", "code": ""},
            "image": {"needed": False, "priority": 0, "reason": "", "style": "", "title": "", "prompt": ""},
            # 标书智能体（6）：数据型图表提名块（外部文档 chart.needed/type/title/data）
            "chart": {"needed": False, "priority": 0, "reason": "", "type": "", "title": "", "data": {}},
            # E-2：降级路径 knowledge 置空（不阻塞，正文生成按无素材处理）
            "knowledge": {"item_ids": []},
            # P0（facts-ns）：降级路径 facts 同样置空，正文生成回退确定性打分筛选
            "facts": {"titles": []},
        },
        "illustration_type": "none",
    }


#: 编排允许提名的数据型图表类型白名单（与导出渲染链路 _CHART_TYPE_MAP 同源）。
#: 取不到时回落为内置集合（避免因导入失败导致 chart 提名全丢）。
CHART_TYPE_WHITELIST: tuple[str, ...] = (
    "flowchart", "gantt", "architecture", "labor",
    "comparison", "layout", "timeline",
)


def _chart_type_whitelist() -> tuple[str, ...]:
    """返回可消费的数据型图表类型白名单。"""
    try:
        from ..export_chart_pipeline import _CHART_TYPE_MAP  # 服务层同包导入

        types = tuple(str(k).lower() for k in (_CHART_TYPE_MAP or {}).keys())
        if types:
            return types
    except Exception:  # pragma: no cover - 导入兜底
        pass
    return CHART_TYPE_WHITELIST


def _strip_code_fence(text: Any) -> str:
    """去掉 AI 可能带回来的 Markdown 代码围栏（```mermaid ... ```）。

    外部文档明确要求「Mermaid 代码不含代码围栏」；模型偶尔仍会带上围栏，
    这里做**幂等**清洗，避免围栏进入正文围栏块后被二次包裹。
    """
    s = str(text or "").strip()
    if not s:
        return ""
    if "```" not in s:
        return s
    lines = s.split("\n")
    # 去掉首尾围栏行（允许 ```mermaid / ``` 两种形态）
    while lines and lines[0].strip().startswith("```"):
        lines.pop(0)
    while lines and lines[-1].strip().startswith("```"):
        lines.pop()
    return "\n".join(lines).strip()


def _sanitize_title(value: Any, limit: int = 60) -> str:
    """图标题清洗：单行化 + 限长。"""
    s = " ".join(str(value or "").split())
    return s[:limit]


def _sanitize_mermaid_code(value: Any, limit: int = 4000) -> str:
    """Mermaid 代码清洗：去围栏 + 限长 + 去空行尾空格。"""
    s = _strip_code_fence(value)
    if not s:
        return ""
    lines = [ln.rstrip() for ln in s.split("\n")]
    return "\n".join(lines).strip()[:limit]


def _sanitize_chart_data(value: Any, limit: int = 4000) -> dict:
    """数据型图表载荷清洗：只接受 dict，限长（防止模型塞超长文本）。"""
    if not isinstance(value, dict):
        return {}
    try:
        blob = json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return {}
    if len(blob) > limit:
        return {}
    return value


def _normalize_plan(orch: dict, leaf: dict,
                    allowed_knowledge_ids: set | None = None) -> dict:
    """把 AI 返回的编排 JSON 规整成统一 schema

    标书智能体（6）对齐：plan 结构与外部文档一致——
      plan.table   {needed, purpose}
      plan.mermaid {needed, title, code, priority, reason}
      plan.chart   {needed, type, title, data, priority, reason}
      plan.image   {needed, style, title, prompt, priority, reason}
      plan.knowledge {item_ids}
      plan.facts     {titles}
    并由**程序侧**强制「priority ≥ 3 才允许 needed=true」。
    """
    sid = str(orch.get("section_id") or leaf.get("id") or "")
    plan_raw = orch.get("plan") or {}
    if not isinstance(plan_raw, dict):
        plan_raw = {}
    tbl_raw = plan_raw.get("table") or {"needed": False, "purpose": ""}
    mm_raw = plan_raw.get("mermaid") or {"needed": False, "priority": 0, "reason": ""}
    img_raw = plan_raw.get("image") or {"needed": False, "priority": 0, "reason": ""}
    chart_raw = plan_raw.get("chart") or {"needed": False, "priority": 0, "reason": ""}

    # ENHANCE 2026-10-05：priority 统一归一化到 3-5 制（见 normalize_priority）。
    # 必须在**这里**归一，而不是各调用点自行处理——否则裁定函数比较的
    # 仍是混着 0-3 / 3-5 两套语义的原始值。
    def _nblock(raw: dict) -> dict:
        out = dict(raw) if isinstance(raw, dict) else {}
        needed = bool(out.get("needed"))
        # 标书智能体（6）程序侧强制外部规则：
        # 「只有 priority 达到 3 才将 needed 设为 true」——AI 若给出
        # needed=true 但 priority 低于 3（甚至 0），提名视为不达标，丢弃。
        raw_pr = out.get("priority")
        try:
            raw_pr_int = int(raw_pr)
        except (TypeError, ValueError):
            raw_pr_int = None
        if needed and raw_pr_int is not None and raw_pr_int < PRIORITY_MIN:
            needed = False
            out["reason"] = str(out.get("reason") or "")
        out["needed"] = needed
        out["priority"] = normalize_priority(out.get("priority"), needed=needed)
        out.setdefault("reason", "")
        return out

    mm_block = _nblock(mm_raw)
    mm_block["title"] = _sanitize_title(mm_raw.get("title") if isinstance(mm_raw, dict) else "")
    mm_block["code"] = _sanitize_mermaid_code(mm_raw.get("code") if isinstance(mm_raw, dict) else "")

    img_block = _nblock(img_raw)
    _style = str((img_raw.get("style") if isinstance(img_raw, dict) else "") or "").strip().lower()
    img_block["style"] = _style if _style in ("engineering_diagram", "realistic_photo") else ""
    img_block["title"] = _sanitize_title(img_raw.get("title") if isinstance(img_raw, dict) else "")
    img_block["prompt"] = str((img_raw.get("prompt") if isinstance(img_raw, dict) else "") or "").strip()[:600]

    chart_block = _nblock(chart_raw)
    _ctype = str((chart_raw.get("type") if isinstance(chart_raw, dict) else "") or "").strip().lower()
    chart_block["type"] = _ctype if _ctype in _chart_type_whitelist() else ""
    chart_block["title"] = _sanitize_title(chart_raw.get("title") if isinstance(chart_raw, dict) else "")
    chart_block["data"] = _sanitize_chart_data(chart_raw.get("data") if isinstance(chart_raw, dict) else None)
    # 类型非法或数据缺失 → 提名不成立（数据型图表没有真实数据一律不画）
    if chart_block.get("needed") and (not chart_block["type"] or not chart_block["data"]):
        chart_block["needed"] = False
        chart_block["priority"] = PRIORITY_NONE

    return {
        "section_id": sid,
        "title": orch.get("title") or leaf.get("title", ""),
        "recommended_type": orch.get("recommended_type", "text"),
        "confidence": orch.get("confidence", 0.5),
        "reason": orch.get("reason", ""),
        "plan": {
            "table": tbl_raw,           # table 无 priority 字段（易标规格同此）
            "mermaid": mm_block,
            "chart": chart_block,
            "image": img_block,
            # E-2：knowledge.item_ids 白名单过滤后写回；无通道时为空列表
            "knowledge": {"item_ids": _extract_knowledge_ids(plan_raw, allowed_knowledge_ids)},
            # P0（facts-ns 2026-10-14）：facts.titles 去重限长后写回；标题存在性由
            # 生成侧 resolve_facts_by_titles 统一白名单过滤（事实集即白名单）
            "facts": {"titles": _extract_fact_titles(plan_raw)},
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
    # 2026-10-20 易标机制引入（规格三.2）：同类图按归一化 reason 去重。
    # 对齐易标 resolveIllustrationPlan 的「标题归一化去重」：同一类型的多个
    # 候选若配图意图相同（reason 前 16 字归一化后相等），只保留 priority 高的
    # 一个，避免同一张「施工流程图」类意图在不同小节重复裁定落图。
    mermaid_candidates = []  # [(idx, priority)]
    image_candidates = []    # [(idx, priority)]
    chart_candidates = []    # [(idx, priority)] —— 标书智能体（6）：数据型图表
    _mm_seen: dict[str, tuple[int, int]] = {}  # key -> (priority, idx)
    _im_seen: dict[str, tuple[int, int]] = {}
    _ct_seen: dict[str, tuple[int, int]] = {}

    def _dedup_key(obj: dict) -> str:
        raw = str(obj.get("reason") or obj.get("purpose") or "").strip()
        if not raw:
            return ""  # 无意图描述 → 不参与去重（各算各的）
        return re.sub(r"[\s，。、；：;:,.!！?？\-—_/（）()【】\[\]「””]+", "", raw)[:16]

    def _collect(cands: list, seen: dict, idx: int, pr: int, key: str) -> None:
        if key:
            if key in seen:
                old_pr, old_idx = seen[key]
                if pr > old_pr:  # 新候选优先：旧候选出局，换人登记
                    cands[:] = [(i, p) for i, p in cands if i != old_idx]
                    cands.append((idx, pr))
                    seen[key] = (pr, idx)
                return  # priority 不高于已登记者 → 丢弃
            seen[key] = (pr, idx)
        cands.append((idx, pr))

    for idx, p in enumerate(plans):
        plan = p.get("plan", {})
        mm = plan.get("mermaid") or {}
        im = plan.get("image") or {}
        ct = plan.get("chart") or {}
        if mm.get("needed"):
            pr = normalize_priority(mm.get("priority"), needed=True)
            _collect(mermaid_candidates, _mm_seen, idx, pr, _dedup_key(mm))
        if im.get("needed"):
            pr = normalize_priority(im.get("priority"), needed=True)
            _collect(image_candidates, _im_seen, idx, pr, _dedup_key(im))
        if ct.get("needed"):
            pr = normalize_priority(ct.get("priority"), needed=True)
            _collect(chart_candidates, _ct_seen, idx, pr, _dedup_key(ct))

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

    # 标书智能体（6）：数据型图表（chart）裁定 —— 每段最多 1 张，不占 AI 图配额
    # （外部文档：图表类型最终只能一个 mermaid/chart/image/none）。
    # 已占位（mermaid 抢占）的章节跳过 → 同一章节只会落一种图。
    if prefer_mermaid:
        for seg in range(segments):
            seg_ct = [(i, pr) for i, pr in chart_candidates
                      if _segment_of(i) == seg
                      and plans[i].get("illustration_type") is None]
            if not seg_ct:
                continue
            seg_ct.sort(key=lambda x: -x[1])
            best_idx = seg_ct[0][0]
            if plans[best_idx]["plan"].get("chart", {}).get("needed"):
                plans[best_idx]["illustration_type"] = "chart"

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

    # 关闭 prefer_mermaid 时，各类候选合并按 priority 竞争同一配额
    if not prefer_mermaid and max_ai_images > 0:
        merged = ([(i, pr, "mermaid") for i, pr in mermaid_candidates]
                  + [(i, pr, "image") for i, pr in image_candidates]
                  + [(i, pr, "chart") for i, pr in chart_candidates])
        merged.sort(key=lambda x: (-x[1], 0 if x[2] == "image" else 1))
        remaining = max_ai_images
        for idx, _pr, kind in merged:
            if plans[idx].get("illustration_type") is not None:
                continue
            if remaining <= 0:
                break
            plans[idx]["illustration_type"] = {
                "image": "ai", "mermaid": "mermaid", "chart": "chart",
            }.get(kind, kind)
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
            # 标书智能体（6）：数据型图表提名同样参与「有配图则表格降权」
            ct_pr = normalize_priority((p.get("plan", {}).get("chart") or {}).get("priority"),
                                       needed=bool((p.get("plan", {}).get("chart") or {}).get("needed")))
            # BUG-FIX 2026-10-05（D-2）：旧实现读 tbl.get("priority")，但 table 的
            # plan 结构只有 {needed, purpose}，**从来没有 priority 字段**——该分支
            # 恒为 1，排序退化成稳定排序（原序），「有配图则表格降权」实际只靠
            # mm_pr/im_pr 生效。现改为固定的基础权重 1.0，只保留图竞争带来的降权。
            # max_pr 归一化后取值 3-5，除以 5 得到 0.6-1.0 的降权幅度：
            # 强配图章节的表格会被最先砍掉，纯文字章节的表格最先保留。
            effective = 1.0 - max(mm_pr, im_pr, ct_pr) * 0.2
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



# ============================================================
# E-4：全篇叙事骨架（标书智能体六「超长文本」机制 / 差距报告 P-12）
#
# 为什么需要：正文按叶子节点独立并发生成，跨章连贯仅靠父子/同级章节列表，
# 几十万字时各章「各自成文、缺少全篇叙事推进」。骨架在编排阶段一次性生成，
# 给每个 L1/L2 章节一段 1~2 句的篇章定位 + 承上启下约定；正文生成时按
# 章节前缀匹配注入相关骨架段。
#
# 缓存友好：骨架全文同一项目只生成一次、内容稳定，注入消息对所有叶子共享
# 同一前缀（只截取相关行），不会打破 [0]+[1] 的稳定缓存区间。
# ============================================================

_SKELETON_SYSTEM = """你是投标技术方案的全篇叙事骨架规划师。请为目录中的每个一级/二级章节各写 1~2 句「篇章骨架」，说明该章在整个方案中的定位、要回答什么问题、与前后章节如何承接。

硬性要求：
1. 骨架只约束行文衔接与章节定位，禁止出现任何具体数据（工期、人数、价格、设备型号等由全局事实统一口径，骨架一律不写）
2. 语言朴实具体，不写「本章节将详细介绍」这类空话，直接说明该章要论证什么、怎么衔接
3. 只输出严格 JSON：{"skeleton": {"<section_id>": "<1~2 句骨架>", ...}}
4. skeleton 的键必须与输入清单中的 section_id 一一对应，一个都不能少，也不要多造
5. 除 JSON 外不要输出任何解释或 Markdown"""


def _collect_skeleton_nodes(outline: list) -> dict[str, str]:
    """从叶子列表推导需要骨架的 L1/L2 节点（id → title）。

    叶子本身不进骨架（它们有自己的父子/同级上下文）；只取祖先链上的
    L1/L2 节点。ancestors 里每项可能是 {"id","title"} 或纯标题字符串。
    """
    nodes: dict[str, str] = {}
    for leaf in outline or []:
        if not isinstance(leaf, dict):
            continue
        for anc in leaf.get("ancestors") or []:
            if isinstance(anc, dict):
                sid = str(anc.get("id") or "").strip()
                if sid and sid.count(".") <= 1:
                    nodes.setdefault(sid, str(anc.get("title") or ""))
            # 纯字符串形态没有 id，跳过（骨架键必须可定位章节）
        # 兜底：ancestors 缺失时按 id 前缀推导（"1.2.3" → "1"、"1.2"）
        sid = str(leaf.get("id") or "").strip()
        if sid and not nodes:
            parts = sid.split(".")
            if len(parts) >= 2:
                nodes.setdefault(parts[0], "")
            if len(parts) >= 3:
                nodes.setdefault(".".join(parts[:2]), "")
    return {k: v for k, v in nodes.items() if k}


async def generate_full_skeleton(provider: Any, outline: list,
                                 project_overview: str = "",
                                 cancel_event=None,
                                 max_tokens: int = 1600) -> dict[str, str]:
    """一次性生成全篇叙事骨架（E-4）。

    Args:
        provider: AI 提供商
        outline: 叶子章节列表（同 orchestrate_content 的 outline 形态）
        project_overview: 项目概述（帮助骨架贴合项目特征）

    Returns:
        {section_id: 骨架文本}；失败返回 {}（调用方按无骨架继续，不阻塞生成）。
    """
    if isinstance(outline, str):
        try:
            outline = json.loads(outline)
        except json.JSONDecodeError:
            return {}
    if not isinstance(outline, list) or not outline:
        return {}

    nodes = _collect_skeleton_nodes(outline)
    if not nodes:
        return {}

    listing = "\n".join(
        f"{sid}\t{title}" for sid, title in sorted(nodes.items())
    )
    # ENHANCE 2026-10-18（审计遗留 D2 批次 2 收编）：user 侧收编为
    # CONTENT_SKELETON_USER（system 侧早已于 BUG-FIX-PROMPT-E3 收编）。
    # 项目概述来自解析链路（外部内容），注入前做元标记中和。
    # 标书智能体（4）——提示词顺序优化（缓存命中）：node_listing 是唯一随
    # 目录变化的部分，用哨兵占位符渲染模板后按哨兵拆分——稳定段（角色/
    # 规则/输出格式/项目概述）前置为独立 user 消息，章节清单与任务说明放
    # 最后一条；目录重建后再次生成骨架时稳定前缀可命中缓存。
    _NODE_MARKER = "@@NODE_LISTING_MARKER@@"
    from .prompt_order import (
        build_cache_friendly_messages,
        split_rendered_template,
    )

    _skel_template = get_prompt(
        "CONTENT_SKELETON_USER",
        project_overview=(project_overview or "")[:800],
        node_listing=_NODE_MARKER,
        sanitize=True,
    )
    if not _skel_template:
        _skel_template = (
            "### 项目概述\n@@PROJECT_OVERVIEW@@\n\n"
            "### 需要写骨架的章节（section_id<TAB>标题）\n"
            f"{_NODE_MARKER}\n\n"
            "请为以上每个 section_id 输出一篇 1~2 句的篇章骨架，返回严格 JSON。"
        ).replace("@@PROJECT_OVERVIEW@@", (project_overview or "")[:800])
    _before, _after = split_rendered_template(_skel_template, _NODE_MARKER)
    _skel_blocks = [b.strip() for b in (_before, _after) if b.strip()]
    _skel_blocks.append(
        "### 需要写骨架的章节（section_id<TAB>标题）\n"
        f"{listing}\n\n"
        "请为以上每个 section_id 输出一篇 1~2 句的篇章骨架，返回严格 JSON。"
    )
    try:
        # BUG-FIX-PROMPT-E3（骨架提示词未注册）：原直接引用模块级 _SKELETON_SYSTEM
        # 硬编码，管理页无法查看/编辑/覆盖，漂移检测也无法比对。现统一走
        # get_prompt()（DB 优先 → 注册表 → 硬编码回退），保留 _SKELETON_SYSTEM
        # 作为最小 fallback，行为向后兼容。
        _skel_sys = get_prompt("CONTENT_SKELETON_SYSTEM") or _SKELETON_SYSTEM
        try:
            from app.config import ORCHESTRATION_PROMPT_LAYERS_ENABLED as _LAYERS_ON
        except Exception:  # pragma: no cover - 配置读取兜底
            _LAYERS_ON = True
        if _LAYERS_ON:
            messages = build_cache_friendly_messages(_skel_sys, _skel_blocks, "")
        else:
            messages = [
                AIMessage(role="system", content=_skel_sys),
                AIMessage(role="user", content="\n\n".join(_skel_blocks)),
            ]
        chat_kwargs: dict[str, Any] = {"max_tokens": max_tokens, "temperature": 0.2}
        if cancel_event is not None:
            chat_kwargs["cancel_event"] = cancel_event
        raw = await provider.chat(messages, **chat_kwargs)
        # 标书智能体（4）缓存可观测性：按任务类型记账（fail-soft）。
        try:
            from .cache_metrics import record_provider_cache_usage

            record_provider_cache_usage(provider, task_type="full_skeleton")
        except Exception:  # pragma: no cover - 防御性
            pass
        text = raw if isinstance(raw, str) else (raw or {}).get("content", str(raw))
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if not m:
            return {}
        data = json.loads(m.group())
        skeleton = data.get("skeleton") if isinstance(data, dict) else None
        if not isinstance(skeleton, dict):
            return {}
        # 只保留已知节点、裁剪长度；保持与输入一致的稳定顺序
        out: dict[str, str] = {}
        for sid in sorted(nodes.keys()):
            txt = str(skeleton.get(sid) or "").strip()
            if txt:
                out[sid] = txt[:160]
        return out
    except Exception as e:
        logger.warning("[全篇骨架] 生成失败（按无骨架继续）: %s", e)
        return {}


def select_skeleton_for_section(skeleton: dict | None, section_id: str,
                                max_chars: int = 500) -> str:
    """按章节 id 前缀匹配取出相关骨架段（E-4，供正文消息构建调用）。

    章节属「1.2.3」时命中骨架键「1」「1.2」（祖先链），键顺序按骨架字典
    原序（L1 → L2），保证同项目内注入文本稳定。超长时整体截断。
    """
    if not isinstance(skeleton, dict) or not skeleton:
        return ""
    sid = str(section_id or "").strip()
    if not sid:
        return ""
    lines = []
    for key, txt in skeleton.items():
        k = str(key).strip()
        if sid == k or sid.startswith(k + "."):
            lines.append(f"[{k}] {txt}")
    if not lines:
        return ""
    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[:max_chars] + "…"
    return text