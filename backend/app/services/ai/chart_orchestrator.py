"""图表智能编排服务（P2 前端智能编排）

职责：
  1. AI 智能判断章节是否需要图表
  2. AI 生成 Mermaid/JSON 载荷
  3. AI 推荐图表插入位置
"""

from __future__ import annotations

import concurrent.futures
import json
import logging
import re
import threading
from typing import Any

from ..export_chart_pipeline import (
    _CHART_TYPE_MAP,
    _CHART_FALLBACK_KEYWORDS,
    _infer_chart_type_from_text,
)

logger = logging.getLogger(__name__)


# ============================================================
# 核心编排函数
# ============================================================


def analyze_chart_needs(
    section_title: str,
    content: str,
    project_summary: str = "",
    ai_client: Any = None,
) -> dict[str, Any]:
    """AI 智能判断章节是否需要图表。"""
    result: dict[str, Any] = {
        "needed": False,
        "chart_type": "none",
        "confidence": 0.0,
        "reason": "",
        "title_hint": "",
        "insert_after": "",
        "source": "local",
    }

    if not section_title and not content:
        return result

    # 本地推断（快速路径）
    local_result = _local_chart_inference(section_title, content)
    result.update(local_result)

    # AI 增强（可选，失败时回退本地）
    if ai_client is not None:
        try:
            ai_result = _ai_chart_analysis(
                section_title, content, project_summary, ai_client
            )
            if ai_result.get("needed") and ai_result.get("confidence", 0) > 0.7:
                result.update(ai_result)
                result["source"] = "ai"
        except Exception as e:
            logger.debug("AI 图表分析失败，回退本地推断: %s", e)

    return result


def generate_chart_payload(
    section_title: str,
    content: str,
    chart_type: str,
    title_hint: str = "",
    ai_client: Any = None,
) -> dict | None:
    """AI 智能生成图表载荷。

    ENHANCE 2026-10-27（需求 3·AI 优先）：提供 ``ai_client`` 时**优先 AI 生成**——
    AI 载荷基于章节真实内容（真实工序/任务/人数），本地模板只是兜底。旧实现
    本地模板先行且对全部 7 类恒成功，AI 载荷生成是**死代码**，与「AI 自动生成
    Mermaid/JSON 载荷」需求相悖。无 ``ai_client`` 时行为与旧版完全一致（本地
    模板快速路径），零回归。
    """
    if not chart_type or chart_type == "none":
        return None

    # AI 生成（优先路径：载荷反映章节真实内容）
    ai_payload: dict | None = None
    if ai_client is not None:
        try:
            ai_payload = _ai_payload_generation(
                section_title, content, chart_type, title_hint, ai_client
            )
        except Exception as e:
            logger.debug("AI 载荷生成失败，回退本地模板: %s", e)

    if ai_payload:
        # A3 建议 1（显式披露）：打来源标记，前端据 source="local" 打
        # 「模板数据」标签引导用户核定，导出链路据此决定是否挂披露括注。
        ai_payload["source"] = "ai"
        return ai_payload

    # 本地生成（快速路径 / AI 不可用或失败时的兜底）
    local_payload = _local_payload_generation(section_title, content, chart_type, title_hint)
    if local_payload:
        local_payload["source"] = "local"
    return local_payload


#: 图引导语框架词（需求 4·图文连贯）：AI 正文为图预留的铺垫句
#: （「下图为施工流程：」「如下图所示」「见图 3-2」），图表围栏应紧随其后。
_LEADIN_HINT_RE = re.compile(r"如?下[面一]?图(?:所示)?|如图\s*[\d\-]*\s*所示|见下?图(?:所示)?")

#: 结构化行：不可作插入锚点（小节标题 / 列表项 / 表格行 / 围栏 / 引用 / 图表标记）。
#: 图插在标题行后会与下文断裂；插在列表/表格行后会打断列表结构（需求 11·列表样式）。
_STRUCTURED_LINE_RE = re.compile(
    r"^(?:#{1,6}\s|[-*+]\s|\d+[\.、)]\s|\||```|~~~|>|<<|\[\[)"
)

#: 数据型图表集合（A3 建议 2·空态优于错态）：载荷含具体数值（工期/人数/
#: 占比/日期），正文零数字时本地模板数字纯属虚构，危害最大。
_DATA_DRIVEN_CHART_TYPES = frozenset(
    {"gantt", "timeline", "labor", "comparison", "layout"}
)

#: 「数字+单位」线索：正文提供了可提取的真实数据（工期/人数/金额/比例/日期）。
#: 覆盖「90 天」「30 人」「25%」「180 工日」「30 个工日」「2026-03-01」「2026年3月」等形态。
_NUMERIC_CLUE_RE = re.compile(
    r"\d+(?:\.\d+)?\s*个?\s*(?:天|日|周|星期|月|季度|年|人|台|套|次|%|％|万|亿|元|米|㎡|m²|km|千米|小时|工日|工天)"
    r"|\d{4}\s*[-/年]\s*\d{1,2}"
)


def content_has_extractable_data(content: str, chart_type: str) -> bool:
    """正文是否含可提取的真实数据线索（A3 建议 2·空态优于错态）。

    本地模板兜底（``_local_payload_generation``）产出的固定示例数字
    （劳动力 3/5/2、里程碑 2025-01-01 等）与章节正文无关联，直接进入
    投标文件存在「图文矛盾」风险。判定口径（``chart_type`` 分级）：

      - 数据型（gantt/timeline/labor/comparison/layout）：正文需含
        「数字+单位」线索（工期/人数/金额/日期），零数字 = 模板数字
        纯虚构，宁缺毋假；
      - 结构型（flowchart/architecture）：正文需含该类型关键词命中
        （正文提供工序名/部门名锚定主题），正文零命中 = 模板节点
        纯虚构。

    正文为空不在判定范围（返回 False 语义上的「无线索」，但编排层对
    空正文有独立的降级落库契约 ``content_empty_fallback_chart_field``，
    由前端「模板数据」披露标签兜底，不在此收紧）。
    """
    if not content or not content.strip():
        return False
    if (chart_type or "").strip().lower() in _DATA_DRIVEN_CHART_TYPES:
        return bool(_NUMERIC_CLUE_RE.search(content))
    keywords = _CHART_FALLBACK_KEYWORDS.get(chart_type, [])
    return any(kw in content for kw in keywords)


def suggest_insertion_position(
    content: str,
    chart_type: str,
    section_title: str = "",
) -> int:
    """AI 推荐图表插入位置（返回第 N 个非空段索引，与 insert_fence_at_paragraph 同口径）。

    ENHANCE 2026-11-03（需求 4·位置插入优化）：旧实现纯关键词打分且不感知
    段落结构，存在三个缺陷：
      1. 图可插在**小节标题行 / 列表项 / 表格行之后**——图文断裂、打断列表结构；
      2. ``section_title`` 参数从未参与打分（死参数）——章节标题常含图表类型
         关键词（「施工工艺流程」），标题自身是强锚点信号；
      3. 正文已有图引导语段（「下图为…：」）时不优先——引导语是正文为图
         预留的铺垫，图应紧随其后而非插到别处。

    现按三级策略（向后兼容：返回值语义不变、关键词打分保留）：
      ① 引导语段优先：命中 _LEADIN_HINT_RE 的段落直接作锚点（图随引导语）；
      ② 关键词打分 + 结构过滤：跳过结构化行，**纯正文信号**打分；
      ③ 兜底：正文无任何命中时——``section_title`` 含类型关键词 → 首段
         （标题引入段后）作锚点；否则保持旧口径 min(1, len-1)；
         命中结构化行则前移到最近可锚点段。

    FIX 2026-11-03：首版把标题加权放进 ② 打分（首段 0+1=1 分），与正文单
    关键词命中段（1 分）打平且严格大于不成立 → 首段占先，破坏了 2026-10-17
    契约（图应插在含「岗位」的第二段后，全量回归实测暴露）。正文关键词命中
    段是「图文连贯」的第一信号，标题只是**无正文信号时**的弱兜底——故把
    加权从打分阶段移到兜底阶段。
    """
    if not content:
        return -1

    paragraphs = [p.strip() for p in content.split("\n") if p.strip()]
    if not paragraphs:
        return -1

    keywords = _CHART_FALLBACK_KEYWORDS.get(chart_type, [])
    _title_bonus = 1 if section_title and any(kw in section_title for kw in keywords) else 0

    # ① 引导语段优先：图紧随「下图为…：」铺垫句（图文逻辑连贯的最强信号）
    for i, para in enumerate(paragraphs):
        if _LEADIN_HINT_RE.search(para) and not _STRUCTURED_LINE_RE.match(para):
            return i

    # ② 关键词打分 + 结构化行过滤（纯正文信号，标题不参与——避免压过真实命中段）
    best_index = -1
    best_score = 0
    for i, para in enumerate(paragraphs):
        if _STRUCTURED_LINE_RE.match(para):
            continue  # 标题/列表/表格/围栏行不作锚点（防图文断裂）
        score = sum(1 for kw in keywords if kw in para)
        if score > best_score:
            best_score = score
            best_index = i

    # ③ 兜底：正文无命中 → 标题含类型关键词时首段作锚点（标题引入段后）；
    #    否则旧口径 min(1, len-1)；命中结构化行时前移到最近可锚点段
    if best_index < 0:
        if _title_bonus and not _STRUCTURED_LINE_RE.match(paragraphs[0]):
            return 0
        best_index = min(1, len(paragraphs) - 1)
        while best_index > 0 and _STRUCTURED_LINE_RE.match(paragraphs[best_index]):
            best_index -= 1

    return best_index


# ============================================================
# 本地推断与生成
# ============================================================


def _local_chart_inference(section_title: str, content: str) -> dict[str, Any]:
    """本地图表需求推断（无 AI 依赖，基于关键词匹配）。"""
    combined = f"{section_title} {content}"[:500]
    chart_type = _infer_chart_type_from_text(combined)

    keywords = _CHART_FALLBACK_KEYWORDS.get(chart_type, [])
    score = sum(1 for kw in keywords if kw in combined)
    confidence = min(0.9, 0.5 + score * 0.1) if score > 0 else 0.0

    needed = score >= 2

    return {
        "needed": needed,
        "chart_type": chart_type if needed else "none",
        "confidence": confidence,
        "reason": f"关键词命中 {score} 个" if needed else "关键词不足",
        "title_hint": section_title[:20] if section_title else "",
        "insert_after": _paragraphs_to_hint(content, chart_type),
        "source": "local",
    }


def _local_payload_generation(
    section_title: str, content: str, chart_type: str, title_hint: str = ""
) -> dict | None:
    """本地图表载荷生成（基于模板+关键词提取）。"""
    title = title_hint or section_title or _CHART_TYPE_MAP.get(chart_type, ("", "图表"))[1]

    # 结构化 JSON 载荷（优先本地生成）
    if chart_type == "architecture":
        return _generate_local_architecture(title)
    elif chart_type == "labor":
        return _generate_local_labor(title)
    elif chart_type == "comparison":
        return _generate_local_comparison(title)
    elif chart_type == "layout":
        return _generate_local_layout(title)
    elif chart_type == "timeline":
        return _generate_local_timeline(title)

    # Mermaid 类型（flowchart/gantt）走本地模板
    from ..export_chart_pipeline import _generate_local_mermaid_chart
    mermaid = _generate_local_mermaid_chart(chart_type, title, content[:200])
    if mermaid:
        return mermaid

    return None


def _generate_local_architecture(title: str) -> dict:
    """本地生成组织架构树。"""
    return {
        "type": "architecture",
        "title": title,
        "tree": {
            "label": "项目组织",
            "children": [
                {"label": "项目经理部"},
                {"label": "技术负责人"},
                {"label": "安全员"},
                {"label": "质检员"},
            ],
        },
    }


def _generate_local_labor(title: str) -> dict:
    """本地生成劳动力配置矩阵。"""
    return {
        "type": "labor",
        "title": title,
        "phases": ["准备", "施工", "验收"],
        "categories": ["管理", "技术", "劳务"],
        "data": [
            [3, 2, 5],
            [5, 10, 30],
            [2, 3, 5],
        ],
    }


def _generate_local_comparison(title: str) -> dict:
    """本地生成对比图数据。"""
    return {
        "type": "comparison",
        "title": title,
        "rows": [
            {"label": "方案一", "value": 40},
            {"label": "方案二", "value": 35},
            {"label": "方案三", "value": 25},
        ],
    }


def _generate_local_layout(title: str) -> dict:
    """本地生成总平面布置图。"""
    return {
        "type": "layout",
        "title": title,
        "zones": [
            {"name": "办公区", "x": 0, "y": 0, "w": 30, "h": 40},
            {"name": "生产区", "x": 30, "y": 0, "w": 50, "h": 60},
            {"name": "材料堆场", "x": 0, "y": 40, "w": 30, "h": 60},
        ],
    }


def _generate_local_timeline(title: str) -> dict:
    """本地生成时间线。"""
    return {
        "type": "timeline",
        "title": title,
        "milestones": [
            {"date": "2025-01-01", "title": "开工"},
            {"date": "2025-03-01", "title": "基础完成"},
            {"date": "2025-06-01", "title": "主体封顶"},
            {"date": "2025-09-01", "title": "竣工验收"},
        ],
    }


def _paragraphs_to_hint(content: str, chart_type: str) -> str:
    """提取建议插入位置的段落提示。"""
    if not content:
        return ""
    paragraphs = [p.strip() for p in content.split("\n") if p.strip()]
    if not paragraphs:
        return ""
    keywords = _CHART_FALLBACK_KEYWORDS.get(chart_type, [])
    for para in paragraphs:
        if any(kw in para for kw in keywords):
            return para[:40]
    return paragraphs[0][:40]


# ============================================================
# AI 增强函数
# ============================================================

# 标书智能体（4）——提示词顺序优化（缓存命中）：图表分析/载荷生成的稳定规则
# （图表类型标准 + 输出契约 + Mermaid/JSON 语法约束）抽成独立 system 消息，
# 跨调用完全一致 → 稳定前缀可命中服务商缓存；项目信息（稳定）与当前章节
# 任务（易变）拆成独立 user 消息，任务差异放最后。
_CHART_ANALYSIS_SYSTEM = """你是标书图表编排专家，负责判断章节是否需要配图并选择图表类型。

【图表类型标准】
architecture=组织架构/体系图；flowchart=工艺流程/施工流程；gantt=进度计划/工期安排；
comparison=方案比选；layout=总平面/现场布置；labor=劳动力配置；timeline=里程碑时间线；
none=不需要配图（纯文字章节）。

【输出契约】
只返回 JSON 对象，不要任何解释、Markdown 代码围栏：
{"needed":true/false,"chart_type":"上述类型之一","confidence":0-1,"reason":"原因",
 "title_hint":"标题","insert_after":"段落提示"}

【判断规则】
1. needed 仅当图表能显著提升表达时取 true；confidence 表示确信程度（0-1）。
2. chart_type 必须与章节内容特征匹配，不得随意指定。
3. reason 不超过 100 字；title_hint 不超过 30 字。"""

_CHART_PAYLOAD_SYSTEM = """你是标书图表载荷生成专家，负责把章节内容转成图表源码或结构化数据。

【输出契约】
- flowchart/gantt：返回纯 Mermaid 代码（无围栏、无解释）。
- 其他类型：返回纯 JSON 载荷对象（含 type/title 与对应数据字段），无解释文字。

【Mermaid 语法约束】
1. 节点文本使用引号包裹，禁用未转义的特殊字符（括号、引号、分号）。
2. 不使用 mermaid 主题指令、%%init%% 或 HTML 标签。
3. 流程图方向统一 TD；甘特图 dateFormat 统一 YYYY-MM-DD。

【数据约束】
只使用章节内容中真实存在的信息，禁止虚构数据；标题用 title_hint 或章节标题。"""


def _resolve_chart_provider(ai_client: Any) -> Any:
    """把调用方传入的 ai_client 解析为平台 AI provider。

    BUG-FIX 2026-10-23（标书智能体（4）引入时发现）：routers/charts_orchestration.py
    传入的是 ``get_ai_config()`` 返回的**配置 dict**，而旧实现直接调用
    ``ai_client.chat_completion(...)``（OpenAI SDK 形态）——dict 没有该方法，
    AttributeError 被 analyze_chart_needs 的 except 静默吞掉 →「AI 图表分析」
    在生产环境从未真正执行过（永远走本地推断），且平台未安装 openai SDK、
    全仓库无第二处 chat_completion 调用（死代码实证）。

    现统一收敛到平台 provider（provider.chat + AIMessage）：
      - 传入对象已有 .chat → 直接使用（平台 provider / 测试 fake）；
      - dict / None → 经 provider_factory 创建（DB 配置 → 降级链）；
      - 创建失败 → None（调用方按本地推断继续，不阻塞）。
    """
    if ai_client is None:
        return None
    if hasattr(ai_client, "chat"):
        return ai_client
    try:
        from .provider_factory import create_provider_with_fallback

        provider, _name = create_provider_with_fallback(
            ai_client if isinstance(ai_client, dict) else None
        )
        return provider
    except Exception as e:
        # ENHANCE 2026-11-04（P1·AI 增强弱失效不可观测）：原 logger.debug——
        # AI 配置错误时永久静默走本地推断（2026-10-23 生产事故根因之一），
        # 运维无法感知。升级为 warning 并带固定 tag 便于日志检索；
        # 行为不变（仍回退本地推断，不阻塞）。
        logger.warning(
            "[图表AI] provider 创建失败，本章 AI 增强分析降级为本地推断: %s", e
        )
        return None


def _chart_response_text(response: Any) -> str:
    """provider.chat 返回值 → 纯文本（兼容 str / dict / 对象三种形态）。"""
    if response is None:
        return ""
    if isinstance(response, str):
        return response
    if isinstance(response, dict):
        return str(response.get("content", "") or "")
    return str(getattr(response, "content", "") or "")


#: 模块级桥接线程池（F5 性能修复 2026-11-03·需求 10）：旧实现每次 AI 调用
#: 新建 ``ThreadPoolExecutor(max_workers=1)``，线程创建/销毁开销随调用次数
#: 线性增长（批量编排多章节时尤甚）。改为模块级复用（并发上限 4，与 SSE 侧
#: _chart_worker 动态 2-4 口径一致）；进程退出由 concurrent.futures 的
#: atexit 钩子正常回收，空闲线程不阻塞退出。
#: R3 加固 2026-11-03：惰性初始化双重检查加锁——并发首调可能各自建池泄漏
#: 一个（旧实现每次新建无此问题，模块级复用引入后必须防竞态）。
_CHAT_BRIDGE_POOL: concurrent.futures.ThreadPoolExecutor | None = None
_CHAT_BRIDGE_POOL_LOCK = threading.Lock()


def _get_chat_bridge_pool() -> concurrent.futures.ThreadPoolExecutor:
    global _CHAT_BRIDGE_POOL
    if _CHAT_BRIDGE_POOL is None:  # fast path（池建好后无锁零竞争）
        with _CHAT_BRIDGE_POOL_LOCK:
            if _CHAT_BRIDGE_POOL is None:
                _CHAT_BRIDGE_POOL = concurrent.futures.ThreadPoolExecutor(
                    max_workers=4, thread_name_prefix="chart-ai-bridge"
                )
    return _CHAT_BRIDGE_POOL


def _run_chat_sync(provider: Any, messages: list, **kwargs: Any) -> Any:
    """在同步函数里驱动异步 provider.chat。

    图表编排链路（analyze_chart_needs / generate_chart_payload /
    batch_analyze_sections ...）是同步函数，且被 async 路由在**运行中的事件
    循环内**调用（routers/charts_orchestration.py），不能直接 asyncio.run。
    策略：当前线程无运行循环 → asyncio.run；有运行循环 → 丢到**模块级复用**
    线程池跑独立 loop（provider 的 httpx 客户端惰性创建，跨 loop 顺序使用；
    图表调用失败有本地推断兜底，风险可控）。
    """
    import asyncio

    coro = provider.chat(messages, **kwargs)
    if not asyncio.iscoroutine(coro):
        return coro  # 同步 provider（测试 fake 等）直接返回
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    return _get_chat_bridge_pool().submit(asyncio.run, coro).result()


def _record_chart_cache(provider: Any, task_type: str, section_id: str = "") -> None:
    """图表链路缓存用量记账（标书智能体（4），fail-soft）。"""
    try:
        from .cache_metrics import record_provider_cache_usage

        record_provider_cache_usage(provider, task_type=task_type, section_id=section_id)
    except Exception:  # pragma: no cover - 防御性
        pass


def _ai_chart_analysis(
    section_title: str, content: str, project_summary: str, ai_client: Any
) -> dict[str, Any]:
    """AI 图表需求分析（调用 LLM）。

    标书智能体（4）：system 只放稳定规则（图表类型标准+输出契约），项目信息
    （同项目稳定）与当前章节任务（易变）拆成独立 user 消息、任务放最后 →
    同项目多章节分析时稳定前缀命中缓存。
    """
    provider = _resolve_chart_provider(ai_client)
    if provider is None:
        raise RuntimeError("图表 AI provider 不可用")

    from .prompt_order import build_cache_friendly_messages

    try:
        from app.config import CHART_PROMPT_LAYERS_ENABLED as _LAYERS_ON
    except Exception:  # pragma: no cover - 配置读取兜底
        _LAYERS_ON = True
    task_block = (
        f"标题：{section_title}\n"
        f"内容：{(content or '')[:300]}\n"
        f"项目：{(project_summary or '')[:200]}"
    )
    if _LAYERS_ON:
        _stable = (
            [f"项目信息：{(project_summary or '')[:200]}"]
            if (project_summary or "").strip() else []
        )
        messages = build_cache_friendly_messages(
            _CHART_ANALYSIS_SYSTEM, _stable, task_block
        )
    else:
        messages = [
            {"role": "system", "content": _CHART_ANALYSIS_SYSTEM},
            {"role": "user", "content": task_block},
        ]

    response = _run_chat_sync(provider, messages, temperature=0.3, max_tokens=500)
    _record_chart_cache(provider, "chart_analysis", section_title)
    text = _chart_response_text(response)

    try:
        text = text.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text)
            text = re.sub(r"\s*```$", "", text)
        result = json.loads(text)
        result["needed"] = bool(result.get("needed", False))
        result["confidence"] = float(result.get("confidence", 0))
        result["chart_type"] = str(result.get("chart_type", "none")).lower()
        result["reason"] = str(result.get("reason", ""))[:100]
        result["title_hint"] = str(result.get("title_hint", ""))[:30]
        result["insert_after"] = str(result.get("insert_after", ""))[:100]
        return result
    except (json.JSONDecodeError, KeyError, IndexError, ValueError) as e:
        logger.debug("AI 响应解析失败: %s", e)
        return {"needed": False, "confidence": 0.0}


def _ai_payload_generation(
    section_title: str, content: str, chart_type: str, title_hint: str, ai_client: Any
) -> dict | None:
    """AI 图表载荷生成（调用 LLM）。

    标书智能体（4）：system 放稳定规则（Mermaid/JSON 语法约束+数据约束），
    当前图表类型与章节内容作为任务差异放最后一条 user 消息。
    """
    provider = _resolve_chart_provider(ai_client)
    if provider is None:
        raise RuntimeError("图表 AI provider 不可用")

    from .prompt_order import build_cache_friendly_messages

    try:
        from app.config import CHART_PROMPT_LAYERS_ENABLED as _LAYERS_ON
    except Exception:  # pragma: no cover - 配置读取兜底
        _LAYERS_ON = True
    task_block = (
        f"生成{chart_type}类型图表载荷，标题：{title_hint or section_title}\n"
        f"内容：{(content or '')[:500]}\n"
        "仅返回纯载荷（Mermaid代码或JSON），无解释文字。"
    )
    if _LAYERS_ON:
        messages = build_cache_friendly_messages(_CHART_PAYLOAD_SYSTEM, [], task_block)
    else:
        messages = [
            {"role": "system", "content": _CHART_PAYLOAD_SYSTEM},
            {"role": "user", "content": task_block},
        ]

    response = _run_chat_sync(provider, messages, temperature=0.2, max_tokens=1000)
    _record_chart_cache(provider, "chart_payload", section_title)
    text = _chart_response_text(response)

    try:
        text = text.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:mermaid|json)?\s*", "", text)
            text = re.sub(r"\s*```$", "", text)

        if chart_type in ("flowchart", "gantt"):
            if text and len(text) > 10:
                return {"type": chart_type, "code": text, "title": title_hint or section_title}
            return None

        result = json.loads(text)
        if isinstance(result, dict) and "type" in result:
            return result
        return None
    except (json.JSONDecodeError, KeyError, IndexError, ValueError) as e:
        logger.debug("AI 载荷生成失败: %s", e)
        return None


# ============================================================
# 批量智能编排接口
# ============================================================


def batch_analyze_sections(
    sections: list[dict[str, str]],
    project_summary: str = "",
    ai_client: Any = None,
) -> list[dict[str, Any]]:
    """批量智能分析章节图表需求。"""
    results = []
    for i, section in enumerate(sections):
        title = section.get("title", "")
        content = section.get("content", "")
        section_id = section.get("id", f"section_{i}")

        result = analyze_chart_needs(
            section_title=title,
            content=content,
            project_summary=project_summary,
            ai_client=ai_client,
        )
        result["section_id"] = section_id
        results.append(result)

    return results


def generate_all_chart_payloads(
    sections: list[dict[str, Any]],
    ai_client: Any = None,
) -> list[dict[str, Any]]:
    """为所有需要图表的章节生成载荷。"""
    payloads = []
    for section in sections:
        if not section.get("needed", False):
            continue

        chart_type = section.get("chart_type", "none")
        section_id = section.get("section_id", "") or section.get("id", "")
        title_hint = section.get("title_hint", "")
        content = section.get("content", "")

        payload = generate_chart_payload(
            section_title=section.get("title", ""),
            content=content,
            chart_type=chart_type,
            title_hint=title_hint,
            ai_client=ai_client,
        )

        if payload:
            payloads.append({
                "section_id": section_id,
                "chart_type": chart_type,
                "payload": payload,
            })

    return payloads


# ============================================================
# 导出接口
# ============================================================

__all__ = [
    "analyze_chart_needs",
    "generate_chart_payload",
    "suggest_insertion_position",
    "content_has_extractable_data",
    "batch_analyze_sections",
    "generate_all_chart_payloads",
    "_generate_local_architecture",
    "_generate_local_labor",
    "_generate_local_comparison",
    "_generate_local_layout",
    "_generate_local_timeline",
]