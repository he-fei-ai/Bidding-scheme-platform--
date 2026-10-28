"""
Placeholder for workflows_sections.py - restored from .pyc analysis.
This module handles content generation (正文生成) for outline sections.

Source was lost; this stub provides all required exports and basic logic.
The full implementation will be restored incrementally.
"""
import asyncio
import json
import logging
import re
import time
from typing import Any, Callable

# BUG-FIX 2026-10-12（P0 字数口径）：
# 原实现两级导入均失败——`app.services.utils` 模块不存在（backend/app/services/
# 下无 utils.py），`from ..utils import count_words` 从 app.services.ai 上溯到
# app.services 后再进 utils，仍是错路径。两级 ImportError 后落入本地兜底
# `len(text.replace(" ", "").replace("\n", ""))`——把中英文一律按「非空白字符
# 计数」，中文段落字数被系统性放大 ~14%（如 70 字→80 字）。三条连锁问题：
#   ① progress_callback 上报的 word_count 虚高，SSE 事件字数 ≠ DB 字数；
#   ② condense_overlong_content 入口判定用同一虚高口径，L3 目标 300±20%
#      对应 hi=360，边界章节（真实 346 字）被误判为超标（虚高 394>360），
#      触发不必要的 AI 压缩；
#   ③ 与 sse_content 落库使用的 `sections._count_words`（源自 app.utils 权威
#      实现：中文逐字 + 英文按词的正则分词）分叉，用户刷新后看到字数变化。
# 修复：三级相对导入 `from ...utils import count_words` 指向 app.utils；
# 兜底分支同构复用相同正则口径，防止未来 app.utils 重构再次静默退化。
try:
    from ...utils import count_words, normalize_newlines  # 权威口径 + CRLF 归一
except ImportError:  # pragma: no cover - 兜底路径，与权威实现语义等价
    import re as _re_count

    _COUNT_WORDS_FALLBACK_RE = _re_count.compile(r"[\u4e00-\u9fff]|[a-zA-Z0-9]+")

    def count_words(text: str) -> int:
        """字数统计兜底实现（与 app.utils.count_words 口径一致）。"""
        if not text:
            return 0
        return len(_COUNT_WORDS_FALLBACK_RE.findall(text))

    def normalize_newlines(text: str) -> str:
        """CRLF / CR → LF 兜底实现（与 app.utils.normalize_newlines 等价）。"""
        if not text:
            return ""
        if "\r" not in text:
            return text
        return text.replace("\r\n", "\n").replace("\r", "\n")
from ...utils.scoring_points import is_scoring_placeholder
from .cache_metrics import record_prompt_cache, record_store
from .heading_standard import HeadingNumberingGenerator
from .writing_length_config import (
    WRITING_LENGTH_RANGE,
    WRITING_LENGTH_LABEL,
    normalize_writing_length,
    level_target,
    get_tier_config,
    get_default_tier_config,
)
from .providers.base import AIMessage
from .workflows_base import EmptyAIResponseError
try:
    from .prompts._cache import get_prompt
    from .prompts._security import sanitize_prompt_value
except ImportError:
    try:
        from app.services.ai.prompts._cache import get_prompt
        from app.services.ai.prompts._security import sanitize_prompt_value
    except ImportError:
        get_prompt = None
        sanitize_prompt_value = None

# ENHANCE 2026-10-07：正文生成字段契约与全局数据字典（确保提取项目 / 全局事实
# 被有效、完整、一致调用）。集中管理字段清单、章节→字段映射、全局数据字典、
# 按需注入与生成后自检，所有逻辑在 app/services/content_field_contract.py。
try:
    from ...services.content_field_contract import (
        format_global_dictionary_for_prompt,
        format_chapter_fields_for_prompt,
        select_fields_for_chapter,
    )
except ImportError:  # pragma: no cover - 兜底路径
    format_global_dictionary_for_prompt = None
    format_chapter_fields_for_prompt = None
    select_fields_for_chapter = None
# 2026-10-18 ENHANCE: ordered batch generation utilities
try:
    from .ordered_generation import (
        build_generation_batches,
        extract_section_summary,
        inject_previous_section_context,
        verify_generation_order,
    )
except ImportError:  # pragma: no cover
    build_generation_batches = None
    extract_section_summary = None
    inject_previous_section_context = None
    verify_generation_order = None

# BUG-FIX 2026-10-15（P1·图表围栏 mask 口径分叉）：
# 本模块的压缩前置保护（_mask_chart_fences_for_compression）原先自带一份手写
# 围栏正则，与 content_cleaning 的单一权威状态机分叉——只认 3 个反引号、不认
# 波浪线、且按子串而非围栏标记行匹配。现直接复用 content_cleaning 的围栏判定
# 口径（CHART_FENCE_OPEN_PATTERN / _classify_fence_line / _fence_line_is_new_open），
# 避免第 4 份分叉实现。
try:
    from ...services import content_cleaning
except ImportError:  # pragma: no cover - 兜底路径
    from app.services import content_cleaning  # type: ignore  # noqa: PLC0415


logger = logging.getLogger(__name__)


def _resolve_section_system_prompt(planned: bool = False) -> str:
    """解析章节正文 system 提示词（唯一正本：prompts/content.py）。

    FIX 2026-10-08（遗留9）：此处原先存在一份「兜底提示词」，其配图口径是
    `<<CHART:类型>>` 文本标记（并要求「不要输出 Mermaid 代码块」），与注册正本
    `CONTENT_SECTION_SYSTEM` 的 **mermaid/chart-json 围栏**口径完全相反。
    一旦提示词注册表不可用（模块导入失败/提示词未注册），生成会静默切到错误口径，
    AI 输出的配图形态与 `content_cleaning` 的解析口径错配 → 图表被判坏块删除。

    与其静默降级到矛盾文案，不如明确失败：注册正本缺失属于严重配置缺陷，
    必须被听见。

    标书智能体（6）（2026-10-23）：`planned=True` 时改用
    `CONTENT_SECTION_SYSTEM_PLANNED`——**正文与配图严格分离**口径：
    正文只写文字/列表/表格，严禁输出任何图表代码块，配图由系统在全部正文
    生成完成后统一执行（失败隔离，不影响正文）。
    """
    if get_prompt is not None:
        keys = (
            ("CONTENT_SECTION_SYSTEM_PLANNED", "CONTENT_SECTION_SYSTEM")
            if planned
            else ("CONTENT_SECTION_SYSTEM",)
        )
        for key in keys:
            try:
                _prompt = get_prompt(key)
            except Exception as _e:  # 注册表损坏：与缺失同等对待
                logger.error("[提示词] %s 读取失败: %s", key, _e)
                _prompt = None
            if _prompt:
                return _prompt
    raise RuntimeError(
        "CONTENT_SECTION_SYSTEM 提示词不可用（prompts 注册表未加载或提示词为空）。"
        "已拒绝使用历史兜底文案（其配图口径与注册正本相反）。"
        "请检查 app/services/ai/prompts/content.py 是否可正常导入并已注册。"
    )


class SectionContentCache:
    """章节内容缓存 - 内存 LRU 缓存

    BUG-FIX 2026-09-16：原实现 `get_stats()` 恒返回 hits=0 / misses=0 / hit_rate="-"，
    而 `/monitor` 与正文生成的 `cache_stats` SSE 事件都会把它下发给前端 ——
    诊断面板里的「缓存命中率」永远是无意义的 0，无法判断"生成变快"是缓存生效
    还是 AI 变快。现在真实维护命中/未命中计数（过期条目计入 misses）。

    Risk-P3c NOTE 2026-10-11：本缓存是**进程内单例**（模块级 `section_content_cache`）。
    在 uvicorn 多 worker 部署时，每个 worker 有独立缓存 → worker A 写入的缓存
    worker B 无法命中，且 invalidate_by_ids 只清本进程。项目当前单 worker 模式
    下无影响；若后期横向扩容，需引入 Redis 共享缓存层替换本类。
    """

    def __init__(self, max_size: int = 100, ttl: int = 3600):
        self._cache: dict = {}
        self._max_size = max_size
        self._ttl = ttl
        self._lock = asyncio.Lock()
        self._hits = 0
        self._misses = 0

    async def get(self, key: str) -> str | None:
        async with self._lock:
            if key in self._cache:
                value, ts = self._cache[key]
                if time.time() - ts < self._ttl:
                    self._hits += 1
                    return value
                del self._cache[key]
            self._misses += 1
        return None

    async def set(self, key: str, value: str) -> None:
        async with self._lock:
            if len(self._cache) >= self._max_size:
                oldest = min(self._cache, key=lambda k: self._cache[k][1])
                del self._cache[oldest]
            self._cache[key] = (value, time.time())

    def get_stats(self) -> dict:
        """返回缓存统计（同步快照，避免锁嵌套；计数由异步 get/set 维护）"""
        total = self._hits + self._misses
        return {
            "cache_size": len(self._cache),
            "max_size": self._max_size,
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": (f"{self._hits / total:.1%}" if total else "-"),
        }

    async def invalidate_by_title(self, project_id: int | None, titles: list | None = None) -> int:
        """按 project_id + title 失效缓存条目

        调用方在"重生成某些章节"前调用：删除 cache_key 为
        ``section_content:<project_id>:<section_id>`` 的条目。
        titles 是可选的标题匹配列表（若传 None → 清空该项目全部）。
        返回被删除的条目数。

        关键修复（PERF-CACHE-ISOLATE 2026-09-08）：原实现完全忽略 project_id 参数，
        只用 title 做子串匹配，导致：
          1. 多项目并发时跨项目错误命中
          2. invalidate 误删其它项目的缓存
        新版严格按 project_id 前缀隔离。
        """
        async with self._lock:
            prefix = (
                f"section_content:{project_id}:"
                if project_id is not None
                else "section_content:"
            )
            if not titles:
                if project_id is None:
                    count = len(self._cache)
                    self._cache.clear()
                    return count
                # 仅清空该项目缓存
                keys_to_del = [k for k in self._cache if k.startswith(prefix)]
                for k in keys_to_del:
                    del self._cache[k]
                return len(keys_to_del)
            targets = {t.strip() for t in titles if t}
            keys_to_del = [
                k for k in self._cache
                if k.startswith(prefix) and any(t in k for t in targets)
            ]
            for k in keys_to_del:
                del self._cache[k]
            return len(keys_to_del)

    async def invalidate_by_ids(self, project_id: int | None, ids: list | None = None) -> int:
        """按 project_id + section_id 失效缓存条目

        BUG-FIX 2026-09-14（中）：缓存键形如 ``section_content:<pid>:<section_id>``，
        而 ``invalidate_by_title`` 是拿**标题**做子串匹配，与 id 型键永远匹配不上，
        导致"重新生成"时预失效恒为 0 条 —— 用户点重新生成拿到的是 1 小时内的旧正文，
        感知为"重新生成无效"（且白白烧掉一轮 AI 调用）。
        修复：新增按 id 精确失效，调用方改为传 section_id。
        """
        if not ids:
            return 0
        async with self._lock:
            prefix = (
                f"section_content:{project_id}:"
                if project_id is not None
                else "section_content:"
            )
            id_set = {str(i).strip() for i in ids if str(i).strip()}
            keys_to_del = [
                k for k in self._cache
                if k.startswith(prefix) and k.rsplit(":", 1)[-1] in id_set
            ]
            for k in keys_to_del:
                del self._cache[k]
            return len(keys_to_del)

    async def clear(self) -> None:
        """清空所有缓存（与 invalidate_by_title(None) 等价，提供一致的 API 形态）"""
        async with self._lock:
            self._cache.clear()

    # BUG-FIX 2026-09-18（P2）：此处曾有两个同名 invalidate_ids_sync 定义，
    # 前者（逻辑一致但 docstring 缺“锁仅保护异步路径”说明）被后定义静默
    # 遮蔽，属死代码+维护陷阱，已删除重复体，仅保留下方唯一实现。
    def invalidate_ids_sync(self, project_id: int | None, ids: list | None = None) -> int:
        """同步版按 id 失效（供非异步上下文的一致性修复/版本回滚等写正文后调用）。

        BUG-FIX 2026-09-18（P1）：consistency_auditor/consistency_repair/content_version
        等旁路直写 sections.content 后无法调用 async 版失效，旧正文缓存最长
        1 小时内会被“重新生成”回灌，静默覆盖修复成果。此处提供同步入口；
        dict.pop 在 GIL 下安全，锁仅保护异步路径的批量操作，不会丢失正确性。
        ids 为 None 时清空该项目全部条目。
        """
        prefix = (
            f"section_content:{project_id}:"
            if project_id is not None
            else "section_content:"
        )
        if ids:
            id_set = {str(i).strip() for i in ids if str(i).strip()}
            keys_to_del = [
                k for k in list(self._cache)
                if k.startswith(prefix) and k.rsplit(":", 1)[-1] in id_set
            ]
        else:
            keys_to_del = [k for k in list(self._cache) if k.startswith(prefix)]
        for k in keys_to_del:
            self._cache.pop(k, None)
        return len(keys_to_del)


_section_content_cache = SectionContentCache()


def _format_facts_list(facts: list) -> str:
    """格式化全局事实列表为提示词友好格式

    BUG-FIX 2026-09-14（高）：事实项在库里的结构是 {key,label,value,category}
    （见 workflows_analysis._dict_to_fact_items），**没有 title/content 字段**。
    原实现只认 title/content，于是 label 被整体丢弃，AI 拿到的是一串无主语的
    裸值（"- 450 日历天"、"- 某某医院工程"），无法判断"450 日历天"到底是
    工期还是质保期 → 事实误用 / 编造。这里改为按 key/label/value 取值。
    """
    if not facts:
        return ""
    lines = []
    for item in facts:
        if isinstance(item, dict):
            label = (
                item.get("label")
                or item.get("title")
                or item.get("key")
                or ""
            )
            content = (
                item.get("value")
                or item.get("content")
                or item.get("default")
                or ""
            )
            label = str(label).strip()
            content = str(content).strip()
            if label and content:
                lines.append(f"- **{label}**: {content}")
            elif content:
                lines.append(f"- {content}")
        elif isinstance(item, str):
            if item.strip():
                lines.append(f"- {item.strip()}")
    return "\n".join(lines)


def _format_facts_for_prompt(facts: Any) -> str:
    """格式化事实变量供 AI 提示词使用"""
    if not facts:
        return ""
    if isinstance(facts, str):
        return facts
    if isinstance(facts, list):
        return _format_facts_list(facts)
    if isinstance(facts, dict):
        parts = []
        for key, value in facts.items():
            if isinstance(value, list):
                items = ", ".join(str(v) for v in value[:5])
                parts.append(f"  {key}: {items}")
            elif isinstance(value, str):
                parts.append(f"  {key}: {value}")
            elif isinstance(value, dict):
                parts.append(f"  {key}: {json.dumps(value, ensure_ascii=False)[:200]}")
            else:
                parts.append(f"  {key}: {value}")
        return "\n".join(parts)
    return str(facts)


def _select_facts_for_section(facts: Any, title: str, section_summary: str = "") -> list:
    """按章节标题筛选最相关的全局事实（P-5）。

    ENHANCE 2026-10-05：替代「全量注入 + 2000 字符硬截断」。
    正本实现见 `services.facts_service.select_facts_for_section`
    （与可研子系统的字符重合度打分同源思路）。

    非 list 输入（如已格式化的 str / dict）无法逐条打分，原样返回，
    由调用方继续走原有的格式化与截断逻辑，保证向后兼容。
    """
    if not isinstance(facts, list) or not facts:
        return facts
    try:
        from ..facts_service import select_facts_for_section
    except Exception as e:  # pragma: no cover - 防御性
        logger.debug("事实筛选不可用，回退全量注入: %s", e)
        return facts
    try:
        return select_facts_for_section(facts, title, section_summary)
    except Exception as e:
        logger.debug("事实筛选失败，回退全量注入: %s", e)
        return facts


def _normalize_priority_for_prompt(priority: Any) -> int:
    """把编排 plan 里的 priority 归一化到 3-5 制后再写进提示词（D-1 下游）。

    旧实现把 `_img.get("priority", 0)` 原样写进正文提示词
    （"AI配图: 计划配图（优先级 N）"）。由于 priority 存在 0-3 / 3-5 两套语义，
    正文模型拿到的数字含义随来源而变。此处统一归一化。
    """
    try:
        from .workflows_orchestration import normalize_priority
    except Exception:  # pragma: no cover
        return int(priority or 0)
    return normalize_priority(priority, needed=True)


#: 本平台**已知不可渲染**的配图类型 → 最接近的可消费类型降级映射
#: （2026-10-20 遗留项加固：易标 HTML 配图类）。
#:
#: 背景：易标 illustration type 含第四类 "html"（AI 生成自包含 HTML 图表），
#: 但本平台导出/渲染链路只消费 mermaid 围栏 / chart-json / AI 图片三类——
#: `export.py` 全文 grep 证实无任何 HTML→图片/Word 的渲染能力
#: （mermaid 走专用渲染服务，非通用 HTML 引擎）。若编排/落库出现 "html"，
#: 原实现 `_ill_type == "mermaid"` / `== "ai"` 两分支都不命中 → 模型收不到
#: 任何配图意图，既无图也无告警，即"死数据"。
#:
#: 策略：不静默丢弃、不引入无消费方的类型——把 "html" 降级为 "ai"
#: （AI 配图可表达任意图示，是语义最接近的可消费类型）并告警留痕。
#: 只处理**已知不可渲染**的值：`generate-illustration` 路由写入的图片
#: 风格串（engineering_diagram 等）与本判定无关，原样返回，不做值域收敛，
#: 避免把风格串误判为非法值刷告警。
#:
#: 未来导出端建立 HTML 图表消费能力后：删除本映射，并在下方提示词分支补
#: "HTML图: 计划配图（自包含 HTML，导出时渲染为图片）" 即可，无需改其它代码。
_UNRENDERABLE_ILLUSTRATION_MAP = {"html": "ai"}


def _degrade_unrenderable_illustration(value: Any) -> Any:
    """把本平台无法渲染的配图类型降级为最接近的可消费类型（2026-10-20）。

    只映射 `_UNRENDERABLE_ILLUSTRATION_MAP` 中的已知不可渲染值（当前仅
    易标 "html"）；其余值（含图片风格串）原样返回，行为零变化。
    """
    v = str(value or "").strip().lower()
    if v in _UNRENDERABLE_ILLUSTRATION_MAP:
        degraded = _UNRENDERABLE_ILLUSTRATION_MAP[v]
        logger.warning(
            "[配图降级] illustration_type=%r 本平台无渲染消费方，已降级为 %r"
            "（配图意图不丢失；待导出端具备 HTML 图表渲染能力后可恢复原类型）",
            value, degraded,
        )
        return degraded
    return value


def _format_chapter_list(chapters: list) -> str:
    """格式化章节列表"""
    if not chapters:
        return ""
    lines = []
    for ch in chapters:
        if isinstance(ch, dict):
            ch_id = ch.get("id", "")
            title = ch.get("title", "")
            desc = ch.get("description", "")
            line = f"- [{ch_id}] {title}" if ch_id else f"- {title}"
            if desc:
                line += f" — {desc}"
            lines.append(line)
        elif isinstance(ch, str):
            lines.append(f"- {ch}")
    return "\n".join(lines)


def _format_decision(decision: Any) -> str:
    """格式化决策点"""
    if not decision:
        return ""
    if isinstance(decision, str):
        return decision
    if isinstance(decision, list):
        return "\n".join(f"- {d}" for d in decision)
    if isinstance(decision, dict):
        parts = []
        for key, val in decision.items():
            parts.append(f"{key}: {val}")
        return " | ".join(parts)
    return str(decision)


def extract_section_charts(content: str, section_title: str = "", summary: str = "") -> list:
    """从正文中提取图表标记（容错解析器）

    返回条目的两种形态（调用方**必须**区分处理，见 `routers/sse/sse_charts.py`）：

      1. **真实数据条目**：标记内是有效 JSON（含 `type` 与 nodes/tasks/code/tree/
         milestones/zones/phases 等真实数据）→ 可直接落库 `sections.*_json`。
      2. **提示条目**（带 `_hint: True`）：正文只标注了「此处应配某类图」，
         **不含任何数据**。必须交给配图编排管线生成真实数据；
         严禁把提示当成图表数据落库（历史缺陷：纯文本占位符被当作数据落库后，
         `_auto_generate_charts_for_section` 会在阶段一直接 return，
         真实配图不再生成，且导出端把无效载荷渲染失败 → 文档静默丢图）。

    支持格式（与 `content_cleaning.iter_chart_markers` 口径完全一致）：
      A. `<<CHART:{"type":"flowchart","title":"xxx","nodes":[]}>>`  真实 JSON 数据
      B. `<<CHART:{type: "flowchart", title: "xxx"}>>`              宽松字典（补引号后可解析）
      C. `<<CHART:GANTT>>` / `<<CHART:{gantt}>>`                    类型提示
      D. `<<GANTT:>>` / `<<GANTT:施工进度计划>>`                    显式类型前缀提示
      E. `<<CHART:{沉降观测数据分析图}>>`                            纯文本标题 → 关键词推断类型
    """
    if not content:
        return []

    from ...services.content_cleaning import iter_chart_fences, iter_chart_markers, resolve_chart_marker

    charts = []
    # FIX 2026-10-08（P1 空壳载荷）：{"type":"gantt"} 这类只有 type/标题、
    # 无任何数据字段的 dict 不得当作真实数据落库——落库后 _auto_generate_charts
    # 判定「已有真实图」整体跳过编排管线，该图永不生成、导出渲染失败静默丢图。
    # 归一为提示条目（_hint）交配图编排生成真实数据。
    _CHART_DATA_FIELDS = (
        "nodes", "edges", "tasks", "milestones", "phases", "zones", "tree",
        "code", "data", "rows", "columns", "series", "values", "items", "steps",
    )

    def _chart_has_real_data(d: dict) -> bool:
        return any(d.get(_f) for _f in _CHART_DATA_FIELDS)

    def _real_data_or_none(d: dict) -> dict | None:
        """真实可用数据返回 dict；空壳或结构校验未通过返回 None（需降级为提示条目）。

        ENHANCE 2026-10-08（已修复）：`_validate_chart_data` 已接入落库前判定链，
        调用 `chart_validators` 权威校验器；畸形数据不再伪装成真实图表堵住配图编排。
        """
        if not isinstance(d, dict) or not _chart_has_real_data(d):
            return None
        _ok, _err = _validate_chart_data(str(d.get("type") or ""), d)
        if not _ok:
            logger.info(
                "[图表降级] 数据结构校验未通过，降级为配图提示: type=%s reason=%s",
                d.get("type"), _err,
            )
            return None
        return d
    # ENHANCE 2026-10-04（D2）：```mermaid / ```chart-json 围栏块 → 真实数据条目。
    # 数据链路与 <<CHART:{JSON}>> 标记完全同构：type + 平铺字段（code/tree/tasks...）
    # 落库 sections.*_json；导出端标记与围栏两条路径载荷一致（缓存键剔除图号）。
    for _lang, _body, _marker_text in iter_chart_fences(content):
        if _marker_text is None:
            continue  # 坏块（内容无法识别）：删除不入库，不猜测渲染
        try:
            _mt = json.loads(_marker_text)
            _desc_obj = json.loads(_mt.get("description") or "")
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(_desc_obj, dict) and _desc_obj.get("type"):
            if _real_data_or_none(_desc_obj) is not None:
                _desc_obj["_marker"] = _body
                charts.append(_desc_obj)
            else:
                # 空壳（只有 type）：降级为提示条目，交配图编排生成真实数据
                charts.append({
                    "type": _desc_obj.get("type"),
                    "chart_title": _desc_obj.get("title") or _desc_obj.get("chart_title") or "",
                    "title": _desc_obj.get("title") or _desc_obj.get("chart_title") or "",
                    "_hint": True,
                    "_inferred": True,
                    "_marker": _body,
                })
    for marker, prefix, raw in iter_chart_markers(content):
        data = None

        # Strategy A: strict JSON
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                # FIX 2026-10-08（P1 空壳载荷）：只有 type/标题、无数据字段的 dict
                # 归为「配图位置提示」（走 Strategy C），不当真实数据落库。
                # FIX 2026-10-08（遗留3）：结构校验未通过同样降级。
                data = _real_data_or_none(parsed)
        except (json.JSONDecodeError, TypeError):
            pass

        # Strategy B: loose dict fix → JSON
        if data is None and raw:
            fixed = raw.replace("“", '"').replace("”", '"').replace("‘", "'").replace("’", "'").replace("：", ":")
            # Quote bare keys like {type: val, title: val} → {"type": "val", "title": "val"}
            def _quote_bare_keys(s: str) -> str:
                """把裸 key 字典 {type: val} → {"type": val}，正确处理嵌套和中文字符。

                BUG-FIX-88: 原实现用「向前回溯到 {,」」定位 key 起点，在嵌套对象里
                会把整个子串 "type: flow..., title: 测试, nodes" 当成单个 key 名，
                导致 title 字段变成整个字典的字符串。
                新实现逐字符线性扫描，用状态机区分「字符串内/外」，
                只在字符串外把 "key:" 模式替换为 '"key":'，支持中文 key 和嵌套对象。
                """
                result = []
                i = 0
                in_str = False
                n = len(s)
                while i < n:
                    c = s[i]
                    if c == '"' and (i == 0 or s[i-1] != '\\'):
                        in_str = not in_str
                        result.append(c); i += 1; continue
                    if in_str:
                        result.append(c); i += 1; continue
                    if c.isalpha() or c == '_' or ('\u4e00' <= c <= '\u9fff'):
                        j = i
                        while j < n:
                            cc = s[j]
                            if cc.isalpha() or cc == '_' or ('\u4e00' <= cc <= '\u9fff') or cc.isdigit():
                                j += 1
                            else:
                                break
                        key = s[i:j]
                        if j < n and s[j] == ':':
                            result.append(f'"{key}":')
                            i = j + 1
                            if i < n and s[i] == ' ':
                                result.append(' ')
                                i += 1
                            continue
                        else:
                            result.append(c); i += 1; continue
                    result.append(c); i += 1
                return ''.join(result)
            fixed2 = _quote_bare_keys(fixed)
            try:
                parsed = json.loads(fixed2)
                if isinstance(parsed, dict):
                    data = _real_data_or_none(parsed)
            except (json.JSONDecodeError, TypeError):
                pass
        # Strategy C: 无内联数据 → 解析为「配图位置提示」（类型 / 标题，不含数据）
        if data is None:
            resolved = resolve_chart_marker(prefix, raw)
            if not resolved:
                logger.warning("图表标记解析失败（无数据且类型无法识别）: %s...", marker[:50])
                continue
            chart_type, chart_title = resolved
            hint = {
                "type": chart_type,
                "chart_title": chart_title,
                "title": chart_title,
                "_hint": True,
                # 兼容历史字段名（旧实现用 _inferred 表示「无数据的推断条目」）
                "_inferred": True,
                "_marker": marker,
            }
            charts.append(hint)
            continue

        data["_marker"] = marker
        charts.append(data)

    return charts


def _extract_balanced_json(text: str, start_pos: int = 0, depth: int = 0) -> tuple:
    """提取平衡的 JSON 对象"""
    for i in range(start_pos, len(text)):
        c = text[i]
        if c == '{':
            result, end_pos = _extract_balanced_json(text, i + 1, depth + 1)
            if result is not None:
                return result, end_pos
        elif c == '}':
            return text[start_pos:i], i + 1
    return None, -1


def _extract_charts_from_content(content: str) -> list:
    """从内容中提取所有图表标记（容错解析）"""
    # Delegate to extract_section_charts for consistency
    return extract_section_charts(content)


def _validate_chart_data(chart_type: str, data: dict) -> tuple:
    """验证图表数据有效性 (chart_type, data) -> (is_valid, error_msg)

    2026-10-08（已修复）：已统一接入 `chart_validators` 权威校验器作为唯一判定
    标准。校验失败的数据在此降级为「配图提示」，不阻塞生成主链路。
      - 未覆盖的图表类型 → 视为通过（保持对既有数据形态的兼容）；
      - 校验器不可导入（循环依赖/模块缺失）→ 视为通过并告警，
        不因工具链问题阻断生成主链路。
    """
    if not isinstance(data, dict):
        return False, "数据必须是字典"

    _VALIDATOR_BY_TYPE = {
        "flowchart": "validate_flowchart_structure",
        "gantt": "validate_gantt_plan",
        "timeline": "validate_timeline_data",
        "labor": "validate_labor_data",
        "layout": "validate_layout_data",
        "architecture": "validate_architecture_tree",
    }
    _fn_name = _VALIDATOR_BY_TYPE.get((chart_type or "").strip().lower())
    if not _fn_name:
        return True, ""
    try:
        import importlib

        _mod = importlib.import_module("app.services.chart_validators")
        _fn = getattr(_mod, _fn_name, None)
    except Exception as _e:  # pragma: no cover - 校验器不可用
        logger.warning("[图表校验] %s 校验器不可用，跳过校验: %s", _fn_name, _e)
        return True, ""
    if _fn is None:
        return True, ""
    try:
        _ok, _errors = _fn(data)
    except Exception as _e:
        logger.warning("[图表校验] %s 执行异常，跳过校验: %s", _fn_name, _e)
        return True, ""
    if isinstance(_ok, bool) and not _ok:
        _detail = "; ".join(str(x) for x in (list(_errors or [])[:3]))
        return False, (_detail or "数据结构校验未通过")
    return True, ""


def _scoring_context_to_text(scoring_context: Any) -> str:
    """把评分点上下文归一化为「一行一条」的紧凑纯文本，供提示词使用。

    BUG-FIX 2026-09-18（高）：调用方实际传入的是 list[dict]（`_fetch_project_data_async`
    解析出的 scoring_points 列表，经 `generate_sections` 的
    `"scoring_standards": "scoring_context"` 映射原样透传），而
    `build_chapter_content_messages` 一直按 str 处理，后果见该函数内的详细注释。
    本函数兼容 list[dict] / list[str] / dict / str / None 各种形态。
    """
    if scoring_context is None:
        return ""
    if isinstance(scoring_context, str):
        return scoring_context
    if isinstance(scoring_context, dict):
        picked = None
        for key in ("items", "scoring_items", "scoring_points", "data", "result"):
            if isinstance(scoring_context.get(key), list):
                picked = scoring_context[key]
                break
        scoring_context = picked if picked is not None else [scoring_context]
    if not isinstance(scoring_context, (list, tuple)):
        return str(scoring_context)

    items: list[str] = []
    for it in scoring_context:
        if isinstance(it, str):
            s = it.strip()
        elif isinstance(it, dict):
            # BUG-FIX 2026-09-18（中）：跳过 `normalize_writing_targets` 插入的系统
            # 提示项（`__weight_warning__`）。它是给用户看的权重校验提醒，不是评分
            # 要点；不过滤的话，每一章正文提示词的「评分要点」里都会混进一行
            # 「[权重异常] 评分项权重之和为 90，请检查是否遗漏评分项」。
            # 与 outline_gate / workflows_outline_v51 的过滤保持同一判据
            # （ENHANCE 2026-09-18：三处现均调用 app.utils.scoring_points，
            #  判据天然同源，不会再各自漂移）。
            if is_scoring_placeholder(it):
                continue
            parts: list[str] = []
            for k in (
                "item", "name", "title", "rating_item", "content",
                "description", "standard", "requirement", "score", "weight",
            ):
                v = it.get(k)
                if v in (None, "", [], {}):
                    continue
                parts.append(f"{k}={v}" if k in ("score", "weight") else str(v))
            s = " ".join(parts).strip()
        else:
            s = str(it).strip()
        if s:
            items.append(s)
    return "\n".join(items)


def _resolve_word_control_override(word_control: Any, chapter_info: Any) -> dict | None:
    """从强控覆盖表取本章节参数（易标 R5/R8，2026-10-12）。

    覆盖表由 sse_content 按「字数控制生效快照」装配（``word_control_runtime.
    build_section_override``）后经 context 透传；未配置/未强控/章节未命中时
    返回 None，调用方回落既有档位逻辑。解析异常不阻断生成（记 debug 日志）。
    """
    if not word_control or not isinstance(chapter_info, dict):
        return None
    try:
        from ..word_control_runtime import resolve_override

        return resolve_override(word_control, chapter_info)
    except Exception as e:
        logger.debug("[silent-except] 强控覆盖解析失败 - %s", e)
        return None


def _build_strict_redline(override: dict) -> str:
    """强控章节字数红线（R5 话术 + R13 保真条款 + 平台防堆砌/防省略约束）。

    R5/R8/R13 的话术唯一来源是 ``word_control_runtime.build_strict_section_
    requirement``（内部引用纯函数常量），此处只追加平台级红线；严禁在本函数
    内重写区间/目标数字，避免与校验侧口径分叉。
    """
    try:
        from ..word_control_runtime import build_strict_section_requirement

        requirement = build_strict_section_requirement(override)
    except Exception as e:
        logger.debug("[silent-except] 强控话术构造失败 - %s", e)
        requirement = ""
    return (
        "【强控字数红线】" + requirement +
        "测量口径为本章节自身正文（不含下级目录章节——下级目录章节由下级章节"
        "独立生成并自行控制字数）。必须严格遵守："
        "① 不得为凑字数而堆砌空话套话、不重复表述同一意思；"
        "② 不得为省字数而省略必含要素（项目概况、评分要点响应、施工部署、"
        "质量控制、安全保证、进度安排、资源配置、应急预案、验收标准等）；"
        "③ 不得出现【待补充】等占位内容；"
        "④ 数据详实、结构清晰、专业术语准确，紧扣评分点。"
    )


def build_chapter_content_messages(chapter_info: dict, facts: Any = None, 
                                    scoring_context: str = "",
                                    project_scope: str = "",
                                    sibling_chapters: list | None = None,
                                    parent_chapters: list | None = None,
                                    **kwargs: Any) -> list:
    """构建正文生成的 AI 消息列表

    PERF-P0-B2 2026-09-05: Prompt 从 ~11KB → ~3KB。
    - 删除 tender_context（整篇招标文件全文，每章重复发送完全浪费）
    - 删除 industry_profile（行业背景画像只在编排/规划阶段需要）
    - summary 截断从 1500 → 800 字
    - scoring_context 截断从全量 → 与本章 title 做关键词匹配后最多 500 字
    - facts 保持不变但已经是压缩格式
    - instructions 精简从 6 → 4 条
    """
    messages = []
    title = chapter_info.get("title", "")
    section_summary = chapter_info.get("summary", "")

    # BUG-FIX 2026-09-16（P1 死代码）：调用方 `_do_generate_section_with_optimizations`
    # 的调用形态是 `build_chapter_content_messages(section, **context)` —— 而
    # parent_chapters / sibling_chapters 是**本函数的形参**，只会从 context 关键字
    # 取到。leaf dict 里虽然已经算好这两份数据（`_build_leaf_dicts` 产出），
    # 但它们藏在 chapter_info 里，永远不会变成关键字参数 → 下面两个注入分支
    # 恒不执行 →「上级章节（承接关系）」「同级章节（避免内容重复）」从未下发。
    # 这里回退读 chapter_info，与 `generate_sections` 注入的 _local_ctx 形成双保险。
    if parent_chapters is None:
        parent_chapters = chapter_info.get("parent_chapters") or None
    if sibling_chapters is None:
        sibling_chapters = chapter_info.get("sibling_chapters") or None

    # 从 kwargs 提取（精简后）增强上下文
    kw_summary = (kwargs.get("summary") or "")[:800]
    kw_writing_length = kwargs.get("writing_length") or ""
    # 2026-10-18 ENHANCE: previous section summaries for ordered context continuation
    kw_prev_sections = kwargs.get("previous_sections_summary") or ""

    # ENHANCE 2026-10-05（P-9）：招标文件已提取的「工程作法/施工方案」原文。
    # 对齐易标规格五「已有方案 vs 无方案差异化流程」：
    #   - 有原文 → 本方已在招标文件中给出施工思路，正文应「承接并扩写」既有方案，
    #     不得另起炉灶提出与之冲突的新工艺；
    #   - 无原文 → 本方需「从头拟定」方案，允许自行组织技术路线。
    # 放在稳定上下文中（项目级、跨章节不变），不影响 [4] 的变化性，缓存收益不变。
    kw_construction_method = (kwargs.get("construction_method") or "").strip()
    # 空数组/空对象视为「无方案」
    if kw_construction_method in ("[]", "{}", "null", "None"):
        kw_construction_method = ""

    # === 评分点匹配：只保留和本章 title 相关的评分点 ===
    # BUG-FIX 2026-09-18（高）：`scoring_context` 的实际类型是 list[dict]（调用方
    # sse_content.py 传的是解析后的 scoring_points 列表，经 generate_sections 的
    # `"scoring_standards": "scoring_context"` 映射原样透传），而本函数一直按 str 处理：
    #   1. `len(scoring_context) > 500` 对 list 恒为 False（列表长度是条目数，不是字符数）
    #      → 关键词过滤分支从不执行，本章评分要点从未被裁剪；
    #   2. `filtered_scoring[:600]` 退化为列表切片，f-string 插值后把 **Python list repr**
    #      （带单引号、逗号、花括号的机器文本）塞进 prompt，模型看到的是非自然语言，
    #      「紧扣评分点」这一核心约束实际失效。
    # 统一归一化为纯文本后再走原有逻辑。
    scoring_context = _scoring_context_to_text(scoring_context)
    filtered_scoring = scoring_context
    if scoring_context and len(scoring_context) > 500:
        # BUG-FIX 2026-09-18（P1）：原实现用 title.split() 切关键词，但中文标题
        # 无空格 → 整标题成为单一 token，要求评分点某行整段包含完整章节标题
        # 才命中，matched 几乎恒空 → 每章都注入同一段 scoring[:500]，
        # 「本章紧扣其评分点」的核心约束实际从未生效。改为：①正则抽取
        # 字母数字词 + 中文连续串；②中文串再滑窗切 bigram；③行内命中任一
        # 关键词即保留；④全未命中时退化为前 500 字（原行为）。
        tokens = re.findall(r"[A-Za-z0-9]{2,}|[\u4e00-\u9fa5]+", title.replace("（", "(").replace("）", ")"))
        kw_set: set[str] = set()
        for tok in tokens:
            if re.match(r"^[\u4e00-\u9fa5]", tok):
                if len(tok) == 2:
                    kw_set.add(tok)
                else:
                    # 滑窗二元组："混凝土施工方案" → 混凝土/土施/施工/工方/方案...
                    kw_set.update(tok[i : i + 2] for i in range(len(tok) - 1))
            else:
                kw_set.add(tok.lower())
        # 过短且泛化的常见词单独命中无区分度，剔除
        kw_set -= {"施工", "方案", "要求", "内容", "说明", "措施", "保证", "管理"}
        if kw_set:
            lines = scoring_context.split("\n")
            matched = [
                ln for ln in lines
                if any((kw in ln) or (kw in ln.lower()) for kw in kw_set)
            ]
            if matched:
                filtered_scoring = "\n".join(matched[:20])
            else:
                filtered_scoring = scoring_context[:500]
        else:
            filtered_scoring = scoring_context[:500]

    # ==================================================================
    # System message（ENHANCE 2026-10-05：恒定化 + 分层多消息）
    #
    # 旧实现有三处问题：
    #   1. :717 / :720-723 用 `messages[0].content += ...` **条件性**追加约束，
    #      取决于该章节有无编排决策 —— 有编排的章节与无编排的章节 system
    #      前缀不同，直接破坏前缀缓存（易标规格九主张的核心收益）。
    #   2. 全部提示词拼进**单条** user message，无法利用消息级前缀缓存。
    #   3. prompts/content.py 中设计的 CONTENT_GENERATION_SYSTEM（含「事实精准性
    #      最高优先级」「严禁编造数字」「必须与 __GLOBAL_FACTS__ 一致」等铁律）
    #      从未被引用（2026-09-14 已补回两条核心铁律，此处保留）。
    #
    # 新结构（对齐易标规格四.4 / 九：稳定前置、任务差异后置）：
    #   [0] system              —— 恒定，跨章节完全一致
    #   [1] 项目稳定上下文       —— 项目摘要/承包范围/事实变量，同项目内跨章节不变
    #   [2] 上级章节             —— 承接关系
    #   [3] 同级章节             —— 避免重复
    #   [4] 当前章节任务         —— 标题/概要/评分要点/目标字数/编排决策（唯一变化）
    # 缓存命中区间 = [0]+[1]（同项目同事实集下），[2][3][4] 随章节变化。
    # ==================================================================
    # --- [0] system（恒定，跨章节完全一致 → 缓存命中区间起点）---
    # ENHANCE 2026-10-05（D-4）：文案正本注册在 prompts/content.py 的
    # CONTENT_SECTION_SYSTEM，提示词管理页可编辑。
    # FIX 2026-10-08（遗留9）：注册表不可用时明确报错，不再回落到矛盾口径的兜底文案。
    sys_prompt = _resolve_section_system_prompt(
        planned=bool(kwargs.get("illustration_phase_planned"))
    )
    # ENHANCE 2026-10-14（facts-ns P1-3，对齐易标 buildContentFactCompletenessInstruction）：
    # omit/placeholder 模式下追加「缺值口径铁律」——正文引用笼统承诺/【待填写】
    # 事实时沿用口径不得自行补具体值。fabricate 模式指令为空串，行为不变；
    # 另受 FACTS_COMPLETENESS_INSTRUCTION_ENABLED 开关控制（默认关）。
    # 追加发生在 [0] system 层：同一项目同一策略下文本稳定，不破坏缓存前缀。
    try:
        from ..facts_service import completeness_instruction_enabled, content_completeness_instruction
        if completeness_instruction_enabled():
            _instr = content_completeness_instruction(kwargs.get("facts_policy"))
            if _instr:
                sys_prompt = f"{sys_prompt}\n{_instr}"
    except Exception as e:  # pragma: no cover - 防御性（与事实筛选同一兜底策略）
        logger.debug("缺值口径指令不可用，跳过: %s", e)
    messages.append(AIMessage(role="system", content=sys_prompt))

    # --- [1] 项目稳定上下文（同项目内完全不变 → 缓存命中区间）---
    # SECURITY-FIX 2026-10-06：项目摘要/承包范围/工程作法均来自招标文件解析
    # （用户/外部内容），手动拼入消息前做元标记中和，防御提示词注入。
    stable_parts = []
    if kw_summary:
        safe_summary = sanitize_prompt_value(kw_summary) if sanitize_prompt_value else kw_summary
        stable_parts.append(f"项目摘要: {safe_summary}")
    if project_scope:
        safe_scope = sanitize_prompt_value(project_scope[:300]) if sanitize_prompt_value else project_scope[:300]
        stable_parts.append(f"承包范围: {safe_scope}")
    # ENHANCE 2026-10-05（P-9）：招标文件已载明的「工程作法」原文，
    # 截断到 1500 字，避免与「项目摘要」争抢 token 预算。
    if kw_construction_method:
        safe_cm = sanitize_prompt_value(kw_construction_method[:1500]) if sanitize_prompt_value else kw_construction_method[:1500]
        stable_parts.append(
            f"招标文件已载明的工程作法（原文摘录，本方须承接扩写而非另起炉灶）: "
            f"{safe_cm}"
        )
    # ENHANCE 2026-10-18（第二轮）：适用规范清单 —— 从招标文本确定性抽取的
    # GB/JGJ/CJJ 等标准编号（ordered_generation.extract_standards_from_text）。
    # 项目级稳定上下文：同项目内跨章节不变，不影响 [0]+[1] 缓存前缀。
    # 无命中（空列表）时不注入，行为与改动前一致。
    kw_standards = kwargs.get("standards_list")
    if isinstance(kw_standards, list) and kw_standards:
        _std_lines = []
        for _st in kw_standards[:20]:
            if not isinstance(_st, dict):
                continue
            _code = str(_st.get("code") or "").strip()
            _title = str(_st.get("title") or "").strip()
            if not _code:
                continue
            _std_lines.append(f"  - {_code}" + (f"《{_title}》" if _title else ""))
        if _std_lines:
            _safe_std = sanitize_prompt_value("\n".join(_std_lines)) if sanitize_prompt_value else "\n".join(_std_lines)
            stable_parts.append(
                "适用规范清单（招标文件载明的标准编号，技术参数与验收标准"
                "须与这些规范一致，不得引用清单外标准）:\n" + _safe_std
            )
    # ENHANCE 2026-10-07：全局数据字典（关键一致性字段权威值，全文唯一）→ 注入稳定上下文。
    # 放在 [1] 使缓存命中区间 [0]+[1] 不受影响；正文必须「一字不差引用」字典值，
    # 这是「前后数据一致性」的核心机制：同一字段在所有章节读取同一权威值。
    _field_contract = kwargs.get("field_contract")
    if _field_contract and format_global_dictionary_for_prompt is not None:
        _dict_text = format_global_dictionary_for_prompt(_field_contract.get("global_dictionary") or {})
        if _dict_text:
            stable_parts.append(_dict_text)
    # ENHANCE 2026-10-19（岳阳路事故·专业隔离）：项目类型锁定 + 域外术语禁入。
    # 装修项目正文此前被含地勘术语的标题误导，强行往「地质勘察」上扯。
    # 注入 [1] 稳定上下文层：同项目跨章节不变，不破坏 [0]+[1] 缓存前缀。
    try:
        from .outline_common import (
            _build_professional_isolation_block,
            _detect_project_type,
        )
        _p_type = _detect_project_type(kw_summary or "", facts)
        _iso_block = _build_professional_isolation_block(_p_type)
        if _iso_block:
            stable_parts.append(_iso_block.strip())
    except Exception as e:  # pragma: no cover - 防御性（与事实筛选同一兜底策略）
        logger.debug("专业隔离约束注入失败（跳过）: %s", e)
    if stable_parts:
        messages.append(AIMessage(role="user", content="\n\n".join(stable_parts)))

    # --- [2] 事实变量（按章节相关性排序，故与 [1] 分开：
    # 否则每章不同的事实前缀会打断 [1] 的缓存命中）---
    if facts:
        # ENHANCE 2026-10-05（P-5）：按章节标题筛选最相关事实，替换「全量注入 +
        # 2000 字符截断」。旧实现在事实条目多（>100 条）时，早期章节占满预算、
        # 尾部章节拿不到；且与易标规格三.4「按章节标题选择最相关事实并注入」
        # 不符。此处保留 2000 字符截断作为安全上限。
        _chapter_facts = None
        # ENHANCE 2026-10-14（facts-ns P0，对齐易标两阶段消费）：编排阶段 AI 只按
        # 标题清单选出 plan.facts.titles，这里按标题取完整事实注入（AI 语义选择
        # 优于字符打分）；开关默认关 / 未选定 / 标题全未命中时回退确定性打分，
        # 行为与今天完全一致。
        try:
            from ..facts_service import orchestrated_select_enabled, resolve_facts_by_titles
            if orchestrated_select_enabled() and isinstance(facts, list):
                _sel_titles = ((chapter_info.get("plan") or {}).get("facts") or {}).get("titles") or []
                if _sel_titles:
                    _chapter_facts = resolve_facts_by_titles(_sel_titles, facts) or None
                    # PERF 2026-10-17: 标题解析命中/未命中指标
                    from ..facts_metrics import inc as _inc_metrics
                    _inc_metrics("fact_resolve_runs")
                    if _chapter_facts:
                        _inc_metrics("fact_resolve_hits", len(_chapter_facts))
                    else:
                        _inc_metrics("fact_resolve_misses", len(_sel_titles))
        except Exception as e:  # pragma: no cover - 防御性
            logger.debug("编排选事实通道不可用，回退打分筛选: %s", e)

        # ENHANCE 2026-10-18: 显式字段映射优先注入（替代纯关键词打分筛选）
        if not _chapter_facts:
            try:
                from ...config import CONTENT_FACTS_EXPLICIT_MAP
                if CONTENT_FACTS_EXPLICIT_MAP:
                    from .ordered_generation import select_facts_by_chapter_map
                    _explicit_facts = select_facts_by_chapter_map(
                        title, facts, kwargs.get("field_contract", {}).get("extraction_fields") if kwargs.get("field_contract") else None,
                        industry=kwargs.get("industry"),
                    )
                    if _explicit_facts:
                        _chapter_facts = _explicit_facts
            except Exception as e:  # pragma: no cover - 防御性
                logger.debug("显式字段映射不可用，回退打分筛选: %s", e)

        if not _chapter_facts:
            _chapter_facts = _select_facts_for_section(facts, title, section_summary)
        if _chapter_facts:
            facts_text = _format_facts_for_prompt(_chapter_facts)
            if len(facts_text) > 2000:
                facts_text = facts_text[:2000] + "\n…（事实变量已截断）"
            # SECURITY-FIX 2026-10-06：事实变量来自招标文件解析，做元标记中和。
            safe_facts = sanitize_prompt_value(facts_text) if sanitize_prompt_value else facts_text
            messages.append(AIMessage(role="user", content=f"事实变量:\n{safe_facts}"))

    # --- [2.5] 本章按需字段（提取项目 + 相关事实，按「章节→所需字段」映射完整注入）---
    # ENHANCE 2026-10-07：替代「只发 summary/全局 facts 让模型自己找」的松散做法，
    # 显式把本章必须使用的提取项目字段与事实变量列出，避免遗漏/截断/错映射。
    # 该消息随章节变化，不进入缓存前缀 [0]+[1]。
    _field_trace = kwargs.get("field_trace")
    if _field_contract and select_fields_for_chapter is not None:
        _chapter_fields = select_fields_for_chapter(
            title, facts or [], _field_contract.get("extraction_fields") or {},
            industry=kwargs.get("industry"),
        )
        _cf_text = format_chapter_fields_for_prompt(_chapter_fields)
        if _cf_text:
            safe_cf = sanitize_prompt_value(_cf_text) if sanitize_prompt_value else _cf_text
            messages.append(AIMessage(role="user", content=safe_cf))
        # 可追溯：记录本章实际注入的字段（标签 / 来源 / 值预览），
        # 供生成后自检与「章节 → 所需事实」调用完整性审计（二.3）。
        if isinstance(_field_trace, list):
            # ENHANCE 2026-10-21（正文 #4）：追溯键补 section_id，供生成后
            # 校验按章节 id 而非 title 匹配（同名章碰撞/标题 sanitize 不再误报）。
            _sec_id = chapter_info.get("id")
            for _lbl, _val in (_chapter_fields.get("extraction") or {}).items():
                if _val:
                    _field_trace.append({
                        "chapter": title, "field": _lbl, "source": "extraction",
                        "section_id": _sec_id, "value": str(_val)[:60],
                    })
            for _lbl, _val in (_chapter_fields.get("facts") or {}).items():
                if _val:
                    _field_trace.append({
                        "chapter": title, "field": _lbl, "source": "facts",
                        "section_id": _sec_id, "value": str(_val)[:60],
                    })

    # --- [2] 上级章节（承接关系）---
    # 参考标准（外部引用软件）：字段访问用安全取值，避免缺字段/None 值抛异常
    # 或把 None 渲染成字面量 "None" 进入提示词。
    if parent_chapters:
        _p_lines = []
        for _p in parent_chapters:
            if not isinstance(_p, dict):
                continue
            _pt = _p.get("title") or ""
            if not _pt:
                continue
            _pd = (_p.get("description") or "")[:80]
            _p_lines.append(f"  - {_pt}" + (f"（{_pd}）" if _pd else ""))
        if _p_lines:
            messages.append(AIMessage(
                role="user", content="上级章节（承接关系，请与之呼应而非重复）:\n" + "\n".join(_p_lines)
            ))

    # --- [3] 同级章节（避免重复）---
    # 标书智能体（4）关键改进（缓存命中，冲突时以外部引用标准为准）：
    # 同级章节列表**包含当前章节**且不按叶子过滤——同一父章节下所有叶子
    # 渲染出字节级一致的 sibling_context，稳定前缀可命中服务商缓存（旧实现
    # 在此排除当前章节，同父章节下每个叶子的同级列表都不同，前缀逐叶子漂移）。
    # 「AI 自我参考/内容重复」的风险由本消息头与任务差异消息里的显式指令
    # 约束：「请避免与除当前章节外的其他章节内容重复」。
    if sibling_chapters:
        _s_lines = []
        for _s in sibling_chapters:
            if not isinstance(_s, dict):
                continue
            _st = _s.get("title") or ""
            if not _st:
                continue
            _sd = (_s.get("description") or "")[:60]
            _s_lines.append(f"  - {_st}" + (f"（{_sd}）" if _sd else ""))
        if _s_lines:
            messages.append(AIMessage(
                role="user", content="同级章节（列表含当前章节；请避免与除当前章节外的其他章节内容重复，突出本章节独特性）:\n" + "\n".join(_s_lines)
            ))

    # --- [3.2] 2026-10-18 ENHANCE: 章节位置与必含要素 ---
    # 注入当前章节在目录树中的位置信息（第几章第几节）和九大章节必含要素清单，
    # 帮助 AI 明确章节定位、避免跨章节内容、确保要素齐全。
    try:
        from .ordered_generation import build_chapter_position_context, build_nine_chapter_elements
        _pos_ctx = build_chapter_position_context(chapter_info, kwargs.get("sections_by_id", {}).values() if kwargs.get("sections_by_id") else [])
        _nine_elem = build_nine_chapter_elements(title, industry=kwargs.get("industry"))
        _pos_parts = []
        if _pos_ctx:
            _pos_parts.append(f"章节位置: {_pos_ctx}")
        if _nine_elem:
            _pos_parts.append(_nine_elem)
        if _pos_parts:
            _safe_pos = sanitize_prompt_value("\n\n".join(_pos_parts)) if sanitize_prompt_value else "\n\n".join(_pos_parts)
            messages.append(AIMessage(role="user", content=_safe_pos))
    except Exception as e:  # pragma: no cover - 防御性
        logger.debug("章节位置/必含要素注入不可用: %s", e)

    # --- [3.5] 下级目录章节（仅作交叉引用，禁止复述）---
    _child_chapters = chapter_info.get("child_chapters") or []
    if _child_chapters:
        _c_lines = []
        for _c in _child_chapters:
            _ct = _c.get("title", "")
            _cd = (_c.get("description") or "")[:80]
            _cl = _c.get("level", 4)
            _c_lines.append(f"  - [{_cl}L] {_ct}" + (f"（{_cd}）" if _cd else ""))
        if _c_lines:
            messages.append(AIMessage(
                role="user",
                content="下级目录章节（仅作交叉引用参考，禁止复述其文字；文字由下级章节在独立任务中生成）:\n" + "\n".join(_c_lines)
            ))

    # --- [3.7] 全篇叙事骨架（E-4，跨章连贯约束）---
    # ENHANCE 2026-10-09：正文按叶子独立并发生成，几十万字时各章「各自成文」。
    # 编排阶段一次性生成的全篇骨架在此按章节前缀匹配注入（祖先链相关段），
    # 只约束行文衔接与章节定位、不含数据，因此同项目内注入文本稳定，
    # 不影响 [0]+[1] 的缓存前缀。
    _skeleton = kwargs.get("full_skeleton")
    if _skeleton is None:
        _skeleton = chapter_info.get("full_skeleton")
    if isinstance(_skeleton, dict) and _skeleton:
        try:
            from .workflows_orchestration import select_skeleton_for_section
        except Exception:  # pragma: no cover - 防御性（与事实筛选同一兜底策略）
            select_skeleton_for_section = None
        if select_skeleton_for_section is not None:
            _skel_text = select_skeleton_for_section(_skeleton, str(chapter_info.get("id", "")))
            if _skel_text:
                _safe_skel = sanitize_prompt_value(_skel_text) if sanitize_prompt_value else _skel_text
                messages.append(AIMessage(
                    role="user",
                    content=(
                        "全篇叙事骨架（跨章承接约束：与本骨架衔接、不抢后续章节的内容；"
                        "骨架不含数据，具体数据以全局事实为准）:\n" + _safe_skel
                    ),
                ))

    # --- [3.75] 2026-10-18 ENHANCE: 前序章节内容摘要（顺序生成时上下文承接）---
    if kw_prev_sections:
        _safe_prev = sanitize_prompt_value(kw_prev_sections) if sanitize_prompt_value else kw_prev_sections
        messages.append(AIMessage(
            role="user",
            content=(
                "前序章节内容摘要（请自然承接，保持行文连贯，避免与前节内容重复）:\n"
                + _safe_prev
            ),
        ))

    # --- [3.8] 2026-10-18 ENHANCE（第二轮）：危大工程判定参数（按章节相关性注入）---
    # 编排/生成前由 sse_content 扫描招标文本识别危大工程（hazard_projects），
    # 此处仅对**相关章节**注入「必含章节/表格/图表、安全阈值、适用规范」，
    # 不把全部危大信息注入每一章（按需注入）。
    kw_hazard = kwargs.get("hazard_info")
    if kw_hazard and isinstance(kw_hazard, dict):
        try:
            from .hazard_projects import (
                HAZARD_PROJECTS,
                format_hazard_context,
                match_hazards_for_chapter,
            )

            _detected = [
                hp for hp in HAZARD_PROJECTS
                if hp.code in {d.get("code") for d in (kw_hazard.get("detected") or [])}
            ]
            _matched = match_hazards_for_chapter(
                title,
                chapter_info.get("ancestors") or [],
                detected=_detected or None,
            )
            if _matched:
                _hazard_text = format_hazard_context(_matched)
                if _hazard_text:
                    _safe_hz = sanitize_prompt_value(_hazard_text) if sanitize_prompt_value else _hazard_text
                    messages.append(AIMessage(role="user", content=_safe_hz))
        except Exception as e:  # pragma: no cover - 防御性
            logger.debug("危大工程上下文注入失败（跳过）: %s", e)

    # --- [3.8] 编排选定的知识库素材（E-2）---
    # ENHANCE 2026-10-09：补齐易标规格 D7 的 knowledge.item_ids 通道 —— 编排阶段
    # AI 从素材清单中为本节挑选的条目，在此取全文注入（条目缺失自动跳过）。
    _kn_items = kwargs.get("knowledge_items")
    if _kn_items is None:
        _kn_items = chapter_info.get("knowledge_items")
    if isinstance(_kn_items, list) and _kn_items:
        _kn_lines = []
        _kn_budget = 1500
        for _it in _kn_items:
            if not isinstance(_it, dict):
                continue
            _kt = str(_it.get("title") or "").strip()
            _kc = str(_it.get("content") or "").strip()
            if not _kt and not _kc:
                continue
            if len(_kc) > 600:
                _kc = _kc[:600] + "…"
            _line = f"[{_it.get('id', '')}] {_kt}: {_kc}" if _kt else f"[{_it.get('id', '')}] {_kc}"
            if _kn_budget - len(_line) < 0:
                break
            _kn_budget -= len(_line)
            _kn_lines.append(_line)
        if _kn_lines:
            _kn_text = "知识库素材（编排为本节选定，仅作参考依据；禁止虚构素材中不存在的数据）:\n" + "\n".join(_kn_lines)
            _safe_kn = sanitize_prompt_value(_kn_text) if sanitize_prompt_value else _kn_text
            messages.append(AIMessage(role="user", content=_safe_kn))

    # --- [4] 当前章节任务（唯一随章节变化的部分）---
    # SECURITY-FIX 2026-10-06：章节标题/概要/评分要点均来自解析结果（用户内容），做元标记中和。
    safe_title = sanitize_prompt_value(title) if sanitize_prompt_value else title
    prompt_parts = [f"# 章节: {safe_title}"]
    if section_summary:
        safe_summary = sanitize_prompt_value(section_summary[:300]) if sanitize_prompt_value else section_summary[:300]
        prompt_parts.append(f"概要: {safe_summary}")
    if filtered_scoring:
        safe_scoring = sanitize_prompt_value(filtered_scoring[:600]) if sanitize_prompt_value else filtered_scoring[:600]
        prompt_parts.append(f"评分要点: {safe_scoring}")

    # 字数控制（2026-10-09 方案三）：三级目录 L3 自身正文 300 \u00b1 20%（240~360）
    #   - L3 叶子（无下级目录章节）：自身正文 300 字；测量口径=自身正文，四档 800/1200/2000/2500
    #     作为「L3 子树总字数」口径仅在 L3 存在下级目录章节（L4+）时参与校验。
    #   - L3 非叶子（存在 L4+）：按四档「子树总字数」控制；正文不含下级目录章节，
    #     下级目录章节由下级章节在独立任务中生成并自行控制字数。
    #   - L1/L2 是父节点，不生成正文，不注入字数指令。
    _chapter_level = int(chapter_info.get("level", 3) or 3)
    _mode = normalize_writing_length(kw_writing_length)
    # 体系合并 2026-10-20：体系 A（档位五级优先级）与体系 B（易标强控覆盖
    # R5/R8）合并为**单一优先级链**，唯一入口 ``services/word_target_resolver.py``
    # （``strict_override`` 命中即短路返回 R5 区间，source=word_control_strict）；
    # 本处不再维护「先查覆盖表、未命中再走档位」的双分支。
    # 契约变更（D6 取消）：旧「提示词侧契约 P-22」（L2+ 忽略显式 target_words）
    # 已移除，生成侧与校验/压缩/R12 链同样尊重单节 ``target_words``。
    # 注：叶子装配（_build_leaf_dicts）的 ``target_words`` 字段写的是解析后的
    # 有效目标（历史行为，供面板/落库展示），生成侧契约只认 ``target_words_raw``
    # （DB 原始值；键缺失时兼容直接传 DB 行的旧调用方）。
    _wc_ov = _resolve_word_control_override(kwargs.get("word_control"), chapter_info)
    _wc_resolved = None
    _strict_hit = False
    if _chapter_level >= 3:
        from ..word_target_resolver import (
            SOURCE_L3_OWN as _SRC_L3_OWN,
            SOURCE_SECTION_TARGET_WORDS as _SRC_SECTION_TW,
            SOURCE_STRICT as _SRC_STRICT,
            resolve_section_word_target_from_mapping as _resolve_wt,
        )

        _wc_resolved = _resolve_wt(
            {
                "level": _chapter_level,
                "target_words": (
                    chapter_info.get("target_words_raw")
                    if "target_words_raw" in chapter_info
                    else chapter_info.get("target_words")
                ),
                "word_count_tier": chapter_info.get("word_count_tier"),
            },
            writing_length=_mode,
            is_l3_own=(
                bool(chapter_info.get("is_l3_own")) if _chapter_level == 3 else None
            ),
            strict_override=_wc_ov,
        )
        _strict_hit = bool(_wc_resolved and _wc_resolved.source == _SRC_STRICT)
    if not _strict_hit:
        # 覆盖表未命中/非法（缺键、S<=0、hi<=0）时不启用强控话术，与旧行为一致。
        _wc_ov = None
    if _strict_hit:
        # 体系 B 强控：R5 区间 + R8 初稿锚点（与 build_strict_section_requirement
        # 同口径）；倒推目标 G（可能为 0=未设全文上限）作锚点，无 G 时用标称 S。
        _wc_anchor = int(_wc_resolved.generation_anchor) or int(_wc_resolved.target)
        _tier_key = None
        _tier_label = "强控"
        _tier_target = int(_wc_resolved.target)
        _tolerance = float(_wc_resolved.tolerance)
    elif _chapter_level == 3 and chapter_info.get("is_l3_own") and (
        _wc_resolved is None or _wc_resolved.source in (_SRC_L3_OWN, _SRC_SECTION_TW)
    ):
        # L3 叶子：自身正文 300 ± 20%；用户显式设了单节 target_words 时尊重
        # 覆盖（解析器首项，区间 ±10%）。
        _tier_key = None
        _tier_label = (
            "单节目标覆盖"
            if _wc_resolved is not None and _wc_resolved.source == _SRC_SECTION_TW
            else "L3 自身正文"
        )
        _tier_target = int(_wc_resolved.target) if _wc_resolved is not None else 300
        _tolerance = float(_wc_resolved.tolerance) if _wc_resolved is not None else 0.20
    elif _chapter_level >= 3 and _wc_resolved is not None:
        # L3 非叶子：四档子树总字数（含下级目录章节）；
        # L4+ 深层叶子跟随同一档位，测量口径为自身正文。
        _tier_key = chapter_info.get("word_count_tier") or _mode
        _tier_cfg = get_tier_config(_tier_key) or get_default_tier_config()
        _tier_label = (
            "单节目标覆盖"
            if _wc_resolved.source == _SRC_SECTION_TW
            else _tier_cfg.get("label", _mode)
        )
        _tier_target = int(_wc_resolved.target)
        _tolerance = float(_wc_resolved.tolerance)
    elif _chapter_level == 2:
        # L2 是父节点，不生成正文；实际生成路径不会进入 L2
        # 保留原提示词格式以兼容旧流程
        _tier_key = chapter_info.get("word_count_tier") or _mode
        _tier_cfg = get_tier_config(_tier_key) or get_default_tier_config()
        _tier_label = _tier_cfg.get("label", _mode)
        _tier_target = int(_tier_cfg.get("target_words", 1200))
        _tolerance = float(_tier_cfg.get("tolerance_ratio", 0.10))
    else:
        # L1 顶层章节：不注入字数指令，由 AI 自由决定体量
        _tier_target = 0
        _tolerance = 0.0
        _tier_label = ""
        _tier_key = None

    lo = int(_tier_target * (1 - _tolerance))
    hi = int(_tier_target * (1 + _tolerance))
    if _strict_hit:
        # 强控区间是 R5 ceil/floor 派生，不重算，直取统一解析结果。
        lo, hi = int(_wc_resolved.lo), int(_wc_resolved.hi)
    # BUG-FIX 2026-10-11（字数偏差控制）：将「240-360」范围表达改为
    # 「约 300 字左右（硬性上限 360 字，最低 240 字）」—— 实测 10 个 L3 章节
    # 平均偏差 +34.2%、100% 系统性偏高、最极端 +120%。根因：模型面对
    # 「240-360」区间时会按「至少 240」理解，倾向超长。改为「约 N 字 + 硬性
    # 上限 M」后，模型有明确锚点值与不可逾越的上限，超长倾向显著降低。
    if _strict_hit:
        target_wc = f"约 {_wc_anchor} 字左右（硬性上限 {hi} 字，最低 {lo} 字）"
        target_note = (
            f"（强控·每节目标 {_tier_target} 字，硬性区间 {lo}~{hi} 字，"
            f"允许浮动 \u00b1{int(_tolerance * 100)}%）"
        )
    else:
        target_wc = f"约 {_tier_target} 字左右（硬性上限 {hi} 字，最低 {lo} 字）"
        target_note = (
            f"（{_tier_label}档位·目标 {_tier_target} 字，"
            f"允许浮动 \u00b1{int(_tolerance * 100)}%）"
        )

    # 字数控制核心指令
    if _chapter_level >= 3:
        prompt_parts.append(
            f"请撰写本章节完整内容，字数严格控制在 {target_wc}{target_note}。"
            f"要求：① 结构清晰有小标题；② 数据详实有具体参数；③ 专业术语准确；"
            f"④ 紧扣评分点。"
            f"【自检要求】撰写完成后请自查总字数是否落在目标区间内——"
            f"若超出硬性上限 {hi} 字，请主动删减空话套话、重复表述与冗余举例，"
            f"再输出；若低于最低 {lo} 字，请补充具体数据、参数或细节。"
        )
        # 字数控制红线（防止堆砌与省略）
        if _wc_ov:
            # 易标 R5/R8/R13：强控命中 → 统一强控话术（数字源自快照派生，
            # 与压缩侧/校验侧同源），盖过下面的档位化红线分支
            prompt_parts.append(_build_strict_redline(_wc_ov))
        elif _chapter_level == 3 and chapter_info.get("is_l3_own"):
            # L3 自身正文：300 ± 20%，测量口径=自身正文，不含下级目录章节
            prompt_parts.append(
                "【字数控制红线】"
                f"本章节为三级目录（L3），目标字数 {_tier_target} 字"
                f"（允许浮动 ±{int(_tolerance * 100)}%，{lo}~{hi} 字，硬性上限 {hi} 字）。"
                "测量口径为 L3 自身正文（标题、描述、项目数据、全局事实、专项方案名称相关内容），"
                "不含下级目录章节。必须严格遵守："
                "① 不得为凑字数而堆砌空话套话、不重复表述同一意思；"
                "② 不得为省字数而省略必含要素（项目概况、评分要点响应、施工部署、"
                "质量控制、安全保证、进度安排、资源配置、应急预案、验收标准等）；"
                "③ 不得出现【待补充】等占位内容；"
                "④ 详细/超长档位应体现更多细节（工艺参数、时间节点、责任人、验收标准、"
                "可视化数据表）而非堆砌；简短/标准档位应精炼而非遗漏关键要素；"
                "⑤ 严禁复述、转述或包含下级目录章节内容——下级目录章节仅由下级章节独立生成，"
                "本节正文只引用其数据，不复述其标题或正文；"
                "⑥ 数据详实、结构清晰、专业术语准确，紧扣评分点。"
            )
        elif _chapter_level == 3:
            # L3 存在下级目录章节：子树总字数控制
            prompt_parts.append(
                "【字数控制红线】"
                f"本章节为三级目录（L3，非叶子），目标字数 {_tier_target} 字（{_tier_label}档位，"
                f"允许浮动 \u00b1{int(_tolerance * 100)}%），测量口径为 L3 子树总字数（含下级目录章节）。"
                "必须严格遵守："
                "① 不得为凑字数而堆砌空话套话、不重复表述同一意思；"
                "② 不得为省字数而省略必含要素（项目概况、评分要点响应、施工部署、"
                "质量控制、安全保证、进度安排、资源配置、应急预案、验收标准等）；"
                "③ 不得复述、转述下级目录章节的文字——下级目录章节由独立章节任务生成并自行控制字数；"
                "④ 数据详实、结构清晰、专业术语准确，紧扣评分点。"
            )
        else:
            # L4+ 深层叶子：跟随同一档位，测量口径为自身正文
            prompt_parts.append(
                "【字数控制红线】"
                f"本章节为第 {_chapter_level} 级目录，目标字数 {_tier_target} 字"
                f"（{_tier_label}档位，允许浮动 \u00b1{int(_tolerance * 100)}%，{lo}~{hi} 字）。必须严格遵守："
                "① 不得为凑字数而堆砌空话套话；"
                "② 不得为省字数而省略必含要素；"
                "③ 不得出现【待补充】等占位内容；"
                "④ 数据详实、结构清晰、专业术语准确，紧扣评分点。"
            )
    elif _chapter_level == 2:
        # L2 是父节点，不生成正文；实际生成路径不会进入 L2
        # 保留原提示词格式以兼容旧流程
        prompt_parts.append(
            f"请撰写本章节完整内容，目标 {target_wc} 字{target_note}。"
            f"要求：① 结构清晰有小标题；② 数据详实有具体参数；③ 专业术语准确；"
            f"④ 紧扣评分点。"
        )
    else:
        # L1 顶层章节：不注入字数指令，由 AI 自由决定体量
        prompt_parts.append(
            "请撰写本章节完整内容。要求：① 结构清晰有小标题；"
            "② 数据详实有具体参数；③ 专业术语准确；④ 紧扣评分点。"
        )
    # ENHANCE 2026-10-05（P-9）：已有方案 vs 无方案差异化指令
    if kw_construction_method:
        prompt_parts.append(
            "【方案承接要求】本项目招标文件已载明工程作法（见上文『工程作法』原文）。"
            "撰写时须：① 承接并细化原文已有的施工工艺与技术路线，不提出与原文冲突的替代方案；"
            "② 在原文基础上补充实施细节、资源配置、质量控制与应急预案，形成可落地的扩写；"
            "③ 原文未覆盖的技术要点方可自行拟定，并标注为补充内容。"
        )
    else:
        prompt_parts.append(
            "【方案拟定要求】本项目招标文件未载明具体工程作法，须自行拟定完整技术方案："
            "明确施工工艺选择依据、关键技术路线、资源配置与质量控制手段，"
            "并说明方案对评分要点的响应关系。"
        )

    # 标书智能体（六）：注入编排决策（plan + illustration_type），
    # 让 AI 知道本章节表格/图片意图，正文与配图不两张皮
    _plan = chapter_info.get("plan") or {}
    # BUG-FIX 2026-10-20（遗留项·HTML 配图防死数据）：已知不可渲染的类型
    # （易标 html）降级为最接近的可消费类型（ai）并告警，绝不让配图意图
    # 静默丢失。详见 _degrade_unrenderable_illustration 的说明。
    _ill_type = _degrade_unrenderable_illustration(
        chapter_info.get("illustration_type")
    )

    # BUG-FIX 2026-10-20（配图意图未传递）：原判据是 `if _plan:`——编排返回
    # 空 plan（{}）时，即使 illustration_type 已裁定为 ai/mermaid，配图意图
    # 也整体不进正文提示词（用户规格五「配图意图未传递」的残留形态）。
    # illustration_type 是程序侧裁定的独立结论，不应被 plan 是否为空 gate 掉；
    # 两者任一有信息量就应注入。plan 为空且类型为 none/None 时行为不变
    # （无任何编排信息，不注入"正文编排决策"块）。
    if _plan or _ill_type not in (None, "", "none"):
        _tbl = _plan.get("table") or {}
        _mmd = _plan.get("mermaid") or {}
        _img = _plan.get("image") or {}
        _cht = _plan.get("chart") or {}
        orch_lines = []

        if _tbl.get("needed"):
            _tpurpose = _tbl.get("purpose", "")[:80]
            orch_lines.append(f"表格: 需要（{_tpurpose}）" if _tpurpose else "表格: 需要")

        # 标书智能体（6）：配图意图**完整**传给正文模型（外部文档关键点——
        # 「预编排结果不只是给程序看的，也会传给正文生成模型」，让正文提前
        # 为图铺垫，避免图文两张皮）。这里给出图类型与图名，正文据此把该图
        # 要表达的关系与要素用文字讲清楚。
        _mmd_title = str(_mmd.get("title") or "")[:40]
        if _ill_type == "mermaid" or (_mmd.get("needed") and not _img.get("needed")):
            _mreason = _mmd.get("reason", "")[:60]
            _line = "Mermaid图: 计划配图"
            if _mmd_title:
                _line += f"（图名：{_mmd_title}）"
            elif _mreason:
                _line += f"（{_mreason}）"
            orch_lines.append(_line)

        _cht_type = str(_cht.get("type") or "")[:20]
        _cht_title = str(_cht.get("title") or "")[:40]
        if _ill_type == "chart" or _cht.get("needed"):
            _line = f"数据型图表: 计划配图（类型：{_cht_type or '待定'}）"
            if _cht_title:
                _line += f"（图名：{_cht_title}）"
            orch_lines.append(_line)

        if _ill_type == "ai" or _img.get("needed"):
            _ipriority = _img.get("priority", 0)
            _ipriority = _normalize_priority_for_prompt(_ipriority)
            _ptag = f"（优先级 {_ipriority}）" if _ipriority else ""
            _img_title = str(_img.get("title") or "")[:40]
            _line = f"AI配图: 计划配图{_ptag}"
            if _img_title:
                _line += f"（图名：{_img_title}）"
            orch_lines.append(_line)

        if orch_lines:
            orch_lines.append(
                "（以上配图由系统在正文生成完成后统一插入；正文请把该图要表达的"
                "关系与要素写清楚，但不要输出任何图表代码块或图片语法）"
            )
            prompt_parts.append("正文编排决策:\n" + "\n".join(orch_lines))

    # --- [4.5] 2026-10-18 ENHANCE: 顺序约束与输出结构约束 ---
    # 明确当前节点在目录树中的位置，禁止跨章节内容，确保编号继承与图表同步。
    # 第二轮增强：显式给出「第几章第几节 / 目录树第几个节点」位置信息，
    # 与上级章节逻辑相承、避免与同级章节重复、承接前一节自然过渡。
    _order_lines = [
        "① 本章节内容必须严格限定在当前章节范围内，不得提前写后续章节内容；"
        "② 正文编号继承目录编号体系（如目录为 1.2.3，正文小标题按 1.2.3.1、1.2.3.2 递增）；"
        "③ 图表与正文同步，图号不虚跳（如引用图 1-1 则必须有对应图 1-1）；"
        "④ 提供的全局事实数据必须优先使用，不得前后矛盾；"
        "⑤ 未提供具体数据时，不要主动编造人员数量、工期、设备型号、质保期等关键数据；"
        "⑥ 关键数据必须与全局事实一致，确保全文数据统一。"
    ]
    _tree_index = kwargs.get("tree_index")
    _tree_total = kwargs.get("tree_total")
    if _tree_index and _tree_total:
        _order_lines.append(
            f"⑦ 本章为按目录树顺序生成的第 {_tree_index}/{_tree_total} 个章节，"
            "须与上级章节逻辑相承、避免与除当前章节外的同级章节内容重复"
            + ("，并自然承接前一节内容" if kw_prev_sections else "") + "。"
        )
    prompt_parts.append("【顺序与结构约束】\n" + "\n".join(_order_lines))

    messages.append(AIMessage(role="user", content="\n\n".join(prompt_parts)))

    # PERF-P0-B2: 记录 prompt 实际大小（日志级别 DEBUG，生产不增加开销）
    logger.debug(
        "Prompt size: title=%s, messages=%d, total_user_chars=%d",
        title, len(messages),
        sum(len(m.content) for m in messages if m.role == "user"),
    )

    # ENHANCE 2026-10-11（引入项 #8）：正文分层消息栈「格式稳定」原则消费点。
    # 现实校正：本函数**已**实现易标「大稳定前置、任务差异后置」的分层排布
    # （[0] system →[1] 项目稳定 →[2] 事实 →…→[4] 当前任务），且比
    # `prompt_layering.build_chapter_content_messages` 的通用 8 层栈多了
    # 事实按标题筛选 / 评分点关键词裁剪 / 暗标脱敏 / 全局字段契约 / 全篇骨架 /
    # 知识库条目注入等能力。故**不采纳**引入报告 §4.2 原设想的「整体切换到
    # prompt_layering」——那会回退上述既有能力，属负优化。
    # 门禁 SPLIT_PROMPT_LAYERS=true 时仅落地方法论第 4 条「同一份数据组织格式
    # 保持稳定」：用 `normalize_readable_text` 统一各层换行符与行尾空白，抬高
    # 跨章节前缀缓存命中率；false（默认）时逐字保持既有消息，行为零变更。
    try:
        from ...config import SPLIT_PROMPT_LAYERS
    except Exception:  # pragma: no cover - 配置读取兜底
        SPLIT_PROMPT_LAYERS = False
    if SPLIT_PROMPT_LAYERS:
        from .prompt_layering import normalize_readable_text
        for _m in messages:
            _m.content = normalize_readable_text(_m.content)

    return messages


# 编排主入口（完整实现移到 workflows_orchestration.py，
# 这里保留包装函数保持向后兼容）
from .workflows_orchestration import (
    orchestrate_content as _orch_impl,
    apply_orchestration_to_leaves,
    _adjudicate_illustrations,
)

async def orchestrate_content(provider: Any, outline: list,
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
    """编排正文生成任务（标书智能体六完整实现）

    主实现见 workflows_orchestration.orchestrate_content()。
    此处为兼容旧调用签名的包装层。

    ENHANCE 2026-10-05（P-11）：新增 max_ai_images / table_ratio /
    prefer_mermaid / section_per_segment 透传，见 workflows_orchestration 同名参数。
    ENHANCE 2026-10-09（E-2）：新增 knowledge_catalog 透传（知识素材通道）。
    ENHANCE 2026-10-14（facts-ns P0）：新增 fact_titles 透传（编排选事实标题通道）。
    """
    return await _orch_impl(
        provider, outline,
        scoring_standards=scoring_standards,
        orch_sys=orch_sys, orch_user=orch_user,
        project_overview=project_overview,
        children_map=children_map,
        concurrency=concurrency,
        cancel_event=cancel_event,
        max_ai_images=max_ai_images,
        table_ratio=table_ratio,
        prefer_mermaid=prefer_mermaid,
        section_per_segment=section_per_segment,
        knowledge_catalog=knowledge_catalog,
        fact_titles=fact_titles,
    )


# BUG-FIX 2026-09-16：此处原有一个 `async def _extract_charts_from_content`，
# 与上面（容错 JSON 解析版）的同步同名函数重复定义 —— 模块级后定义者生效，
# 于是这个「只认严格 JSON」的协程版本把容错版本整体遮蔽：任何按同步方式调用
# `_extract_charts_from_content(content)` 的代码拿到的都是 coroutine 对象
# （既不会抛错也不会返回图表，属最难排查的一类静默失效）。
# 全仓检索确认该名字已无调用方，故直接删除重复定义，保留上面那一个实现。


# =============================================================================
# 2026-10-11：正文超长压缩（字数偏差控制）
# -----------------------------------------------------------------------------
# 背景：DB 实测 10 个 L3 章节平均偏差 +34.2%、100% 系统性偏高、最极端 +120%。
# 根因：提示词以区间表达，模型倾向超长；生成后无压缩机制。
# 修复：本函数在检测到实际字数 > target × threshold 时，调用 AI 将正文压缩到
# 目标区间内，保留必含要素与结构，剔除空话套话。调用方（sse_content.py）在
# 主生成完成后、兜底扩写之前调用。
# 幂等与安全：
#   - 若原文字数 <= target × threshold：直接返回原文（零 AI 调用）
#   - 若压缩后字数仍 > 原字数：保留原文（AI 未成功压缩，避免越压越长）
#   - 若 AI 调用失败或返回空：返回原文（零回归）
#   - 图表代码块（```mermaid / ```chart-json / ```table）原样保留
# =============================================================================


# =============================================================================
# 2026-10-11：字数偏差控制批处理函数（供 sse_content 生成完成后调用）
# -----------------------------------------------------------------------------
# 缺口：DB 实测 10 个 L3 章节平均偏差 +34.2%、100% 系统性偏高、最极端 +120%。
# 修复：主生成完成后，对超出 target × threshold 的章节批量压缩到目标区间。
# 失败安全：压缩失败/超时/取消均保留原文；异常向上被捕获，不影响主生成。
# =============================================================================


async def condense_overlong_results(
    results: list,
    sections_by_id: dict,
    provider: Any,
    writing_length: str | None = None,
    *,
    threshold: float | None = None,
    max_rounds: int | None = None,
    enabled: bool | None = None,
    cancel_event: Any = None,
    word_control: dict | None = None,
) -> dict:
    """对超长章节批量压缩到目标区间内（就地修改 results）。

    Args:
        results: 生成结果列表（每项含 id/content/status）
        sections_by_id: 章节字典索引
        provider: AI provider
        writing_length: 全局档位
        threshold: 触发压缩的偏差率阈值（None 时从 config 读）
        max_rounds: 最多压缩轮数
        enabled: 总开关（None 时从 config 读）
        cancel_event: 取消事件
        word_control: 强控覆盖表（易标 R5/R8）。体系合并 2026-10-20：作为
            统一解析器的 ``strict_override`` 输入，命中章节短路返回 R5 区间
            （目标 S、容差 ±20%，超过硬性上限 1.2S 即触发压缩）；未命中
            回落体系 A 档位/单节目标逻辑（逐值不变）。

    Returns:
        dict: {
            'condensed_count': 压缩成功章节数,
            'fail_count': 失败章节数,
            'total_before': 压缩前字数总和,
            'total_after': 压缩后字数总和,
            'enabled': 是否启用,
            'threshold': 使用的阈值,
            'max_rounds': 使用的最大轮数,
        }
    """
    from .writing_length_config import (
        get_tier_config,
        get_default_tier_config,
        DEFAULT_WORD_COUNT_TIER,
    )
    from ...utils import count_readable_words as _crw
    from ..word_control_runtime import resolve_override as _resolve_ov
    from ...config import (
        CONTENT_USE_WORD_COUNT_ENFORCEMENT_DEFAULT,
        CONTENT_WORD_COUNT_CONDENSE_THRESHOLD,
        CONTENT_WORD_COUNT_CONDENSE_MAX_ROUNDS,
    )

    if enabled is None:
        enabled = CONTENT_USE_WORD_COUNT_ENFORCEMENT_DEFAULT
    if threshold is None:
        threshold = CONTENT_WORD_COUNT_CONDENSE_THRESHOLD
    if max_rounds is None:
        max_rounds = CONTENT_WORD_COUNT_CONDENSE_MAX_ROUNDS

    summary = {
        "condensed_count": 0,
        "fail_count": 0,
        "total_before": 0,
        "total_after": 0,
        "enabled": enabled,
        "threshold": threshold,
        "max_rounds": max_rounds,
        # HARDEN 2026-10-12（契约显式化）：调用方（sse_content.py）需要按
        # section_id 批量落库。此前依赖隐式约定「r["original_content"] != r["content"]
        # ⇒ 已压缩」，若 condense_overlong_content 内部返回与原文等价的字符串
        # （如 AI 判定不修改），会被漏更新；若未来 results 结构演进（新增
        # pre_expand_content / post_audit_content 字段），隐式约定会失效。
        # 显式维护 condensed_ids，让调用方按需精确落库。
        "condensed_ids": [],
    }
    # BUG-FIX 2026-10-17（第七轮·P2 压缩落库口径分叉）：压缩结果与主生成
    # 落库共用同一清洗器（ContentCleaner.clean），消除 AI 残留重复标题 /
    # ## 标记 / 首行编号直写 DB 的缺口。单例复用，避免逐章节构造。
    from ...services.content_cleaning import ContentCleaner as _ContentCleaner

    _condense_cleaner = _ContentCleaner()
    if not enabled or max_rounds <= 0:
        return summary

    for r in results:
        if cancel_event is not None and getattr(cancel_event, "is_set", lambda: False)():
            break
        if r.get("status") != "completed":
            continue
        sid = r.get("id")
        sec = sections_by_id.get(sid) if sections_by_id else None
        if not sec:
            continue
        sec_level = sec.get("level", 3)
        if sec_level < 3:
            continue

        # 体系合并 2026-10-20：强控覆盖（体系 B R5）与档位链（体系 A）合流为
        # 单一优先级链——统一解析器 ``strict_override`` 命中即短路返回 R5 区间；
        # 计量全程用 R1 可读字数（D2 已收敛，与校验页 / R9R10 / R12 同事实源）。
        _ov = None
        try:
            _ov = _resolve_ov(word_control, sec)
        except Exception:
            _ov = None
        r_content = r.get("content") or ""
        r_wc = _crw(r_content) if r_content else 0
        if r_wc == 0:
            continue

        # 解析目标字数与容差（与 sse_utils._get_effective_target_words 同口径）
        from ..word_target_resolver import (
            SOURCE_STRICT as _SRC_STRICT,
            resolve_section_word_target_from_mapping as _resolve_wt,
        )

        _resolved = _resolve_wt(
            sec,
            writing_length=writing_length or DEFAULT_WORD_COUNT_TIER,
            strict_override=_ov,
        )
        target = int(_resolved.target) if _resolved else 0
        tolerance = float(_resolved.tolerance) if _resolved else 0.10
        if _resolved is not None and _resolved.source == _SRC_STRICT:
            # 强控（体系 B）：超过硬性上限 hi=1.2S 即触发压缩（R5 硬区间）。
            thr = float(_resolved.hi)
        else:
            # BUG-FIX 2026-10-17（第七轮·P2 压缩触发即空转）：外层触发阈值
            # target×threshold 与 condense_overlong_content 内部 early-return
            # 阈值 int(target×(1+tolerance)) 曾是两份独立口径。当容差较大时
            # （L3 自身正文 0.20 → 内部 hi=1.2×target > 外部 1.15×target），
            # 介于两者之间的章节「触发了压缩流程但内部直接返回原文」——既不进
            # condensed_count 也不进 fail_count，前端 word_count_condense
            # 事件显示 0，CONTENT_WORD_COUNT_CONDENSE_THRESHOLD 对 L3 实际
            # 失效（探针 _audit7_probe.py::P3 实证：hi=360 > 触发=345）。
            # 修复：外层触发阈值取「配置阈值」与「内部 hi 区间上限」的较大者，
            # 保证「触发必压缩、无需压缩必不触发」；档位路径（tol=0.10 →
            # hi=1.1t < 1.15t）行为逐值不变。
            thr = max(target * threshold, int(target * (1 + tolerance))) if target else 0
        if not target or target <= 0:
            continue

        summary["total_before"] += r_wc
        if r_wc > thr:
            # BUG-FIX 2026-10-17（第七轮·P2 口径分叉）：压缩全程（触发判定 /
            # 内部 early-return / 多轮进度 / 结果计量）与**触发侧**共用同一
            # 计量口径。D2 修复 2026-10-20：强控/非强控统一改用易标 R1 可读字数
            # （原「强控 _crw / 非强控 _cw」双口径合并），与校验侧 / R12 同事实源。
            _wc_fn = _crw
            try:
                condensed, meta = await condense_overlong_content(
                    r_content,
                    target_words=target,
                    tolerance_ratio=tolerance,
                    provider=provider,
                    section_title=sec.get("title", ""),
                    max_rounds=max_rounds,
                    cancel_event=cancel_event,
                    count_fn=_wc_fn,
                )
                if meta.get("condensed") and condensed:
                    # HARDEN 2026-10-12：AI 压缩返回值可能带 \r\n（AI 网关在
                    # Windows 环境下常见），压缩链路不经过 clean_generated_content,
                    # 必须在接受判定前显式归一，否则会把 CRLF 写回 DB，
                    # 让下游围栏识别 / parse_content 再次踩坑（BUG-2 同源问题）。
                    condensed = normalize_newlines(condensed)
                    # BUG-FIX 2026-10-17（第七轮·P2 压缩落库口径分叉）：压缩
                    # 结果此前以裸 SQL 直写 DB（sse_content._batch_update_condensed），
                    # 未经 ContentCleaner——AI 不遵守压缩提示词时（"不要重复章节
                    # 标题、不要输出标题标记"），重复标题 / ## 标记 / 首行编号
                    # 会残留落库，与主生成落库（_save_section_to_db 必经
                    # ContentCleaner.clean）口径不一致（探针 P5 实证旧路径
                    # 无清洗）。现在接受压缩后统一过同一清洗器；清洗把正文
                    # 洗空的极端病理（全文即重复标题）保留未清洗版本。
                    _cleaned = _condense_cleaner.clean(sec.get("title", ""), condensed)
                    if _cleaned and _cleaned.strip():
                        condensed = _cleaned
                    new_wc = _wc_fn(condensed)
                    r["content"] = condensed
                    r["original_content"] = r_content
                    r["original_wc"] = r_wc
                    r["condensed_wc"] = new_wc
                    summary["total_after"] += new_wc
                    summary["condensed_count"] += 1
                    # HARDEN 2026-10-12：显式记录被压缩章节 id（契约给调用方）。
                    summary["condensed_ids"].append(sid)
                    logger.info(
                        "[字数偏差控制] 章节 %s 压缩成功: %d → %d 字 (目标 %d)",
                        sid, r_wc, new_wc, target,
                    )
                else:
                    summary["total_after"] += r_wc
                    if meta.get("error"):
                        summary["fail_count"] += 1
            except Exception as ce:
                logger.warning("[字数偏差控制] 章节 %s 压缩异常: %s", sid, ce)
                summary["total_after"] += r_wc
                summary["fail_count"] += 1
        else:
            summary["total_after"] += r_wc
        # 偏差可追溯：无论是否触发压缩，都记录目标与偏差
        final_wc = r.get("condensed_wc", r_wc)
        r["word_count_target"] = target
        r["word_count_deviation"] = final_wc - target
        r["word_count_deviation_rate"] = round((final_wc - target) / target, 4)

    return summary


async def condense_overlong_content(
    content: str,
    *,
    target_words: int,
    tolerance_ratio: float,
    provider: Any,
    model: str | None = None,
    section_title: str = "",
    timeout_seconds: int = 120,
    max_rounds: int = 1,
    cancel_event: Any = None,
    count_fn: Callable | None = None,
) -> tuple[str, dict]:
    """将超长正文压缩到目标字数区间内（异步）。

    Args:
        content: 原始正文（含图表代码块）
        target_words: 目标字数（例如 L3 自身正文 = 300）
        tolerance_ratio: 允许浮动比例（例如 0.20 即 ±20%）
        provider: AI provider 实例（需有 chat 异步接口）
        model: 可选，指定模型
        section_title: 章节标题（仅用于日志与提示词上下文）
        timeout_seconds: 单次 AI 调用超时
        max_rounds: 最多压缩轮数（默认 1）
        cancel_event: asyncio.Event，取消事件（与主生成共享）
        count_fn: 字数计量函数（默认 count_words）。BUG-FIX 2026-10-17
            （第七轮·P2 口径分叉）：调用方 condense_overlong_results 的
            触发判定用可读字数（强控）或 count_words（非强控），旧实现内部
            恒用 count_words → 强控章节可能「可读字数触发、内部字数判定
            已达标」而静默空转。现触发与压缩全程共用同一口径。

    Returns:
        tuple[str, dict]:
            - content: 压缩后的正文（若无需压缩或压缩失败则返回原文）
            - meta: 元数据
                - target_words, tolerance, lo, hi: 目标区间
                - original_wc, condensed_wc: 原始与压缩后字数
                - rounds: 实际执行轮数
                - condensed: 是否发生实际压缩
                - error: 压缩失败原因（成功时为空字符串）
    """
    meta = {
        "target_words": target_words,
        "tolerance": tolerance_ratio,
        "lo": int(target_words * (1 - tolerance_ratio)),
        "hi": int(target_words * (1 + tolerance_ratio)),
        "original_wc": 0,
        "condensed_wc": 0,
        "rounds": 0,
        "condensed": False,
        "error": "",
    }
    # BUG-FIX 2026-10-17（第七轮·P2 计量口径统一）：见 count_fn 参数说明。
    _wc_fn = count_fn if callable(count_fn) else count_words
    # BUG-FIX 2026-10-17（第七轮·P2 未闭合围栏裸露送压缩 AI）：
    # 本函数的输入可能是 DB 历史正文 / 编辑写入的正文（未经主链路的
    # _force_close_unclosed_chart_fence 双保险），而
    # _mask_chart_fences_for_compression 只保护**已闭合**图表围栏
    # （scan_chart_fence_blocks 不返回未闭合块）——未闭合围栏的
    # mermaid / chart-json 源码会裸露送给压缩 AI，被截断/篡改后图表
    # 静默损坏（探针 _audit7_probe.py::P10 实证）。入口统一兜底闭合
    # （幂等）；压缩失败时函数尾部返回的是入参原 content，
    # 「保留原文」契约不变。
    content, _entry_closed = _force_close_unclosed_chart_fence(content)
    try:
        _wc = _wc_fn(content) if content else 0
    except Exception:
        _wc = len(content or "")
    meta["original_wc"] = _wc

    threshold = int(target_words * (1 + tolerance_ratio))
    if _wc <= threshold:
        meta["condensed_wc"] = _wc
        return content, meta

    # 触发压缩：加载系统提示词
    try:
        from .prompts._cache import get_prompt as _get_prompt

        sys_prompt = None
        try:
            if _get_prompt is not None:
                sys_prompt = _get_prompt("CONTENT_CONDENSE_SYSTEM")
        except Exception:
            sys_prompt = None
        if not sys_prompt:
            meta["error"] = "CONTENT_CONDENSE_SYSTEM 提示词不可用"
            meta["condensed_wc"] = _wc
            return content, meta
    except Exception as _e:
        meta["error"] = f"提示词加载失败: {_e}"
        meta["condensed_wc"] = _wc
        return content, meta

    current_content = content
    current_wc = _wc
    actual_rounds = 0
    for _round in range(max(1, max_rounds)):
        if cancel_event is not None and getattr(cancel_event, "is_set", lambda: False)():
            meta["error"] = "用户取消"
            break
        actual_rounds += 1
        try:
            # BUG-P1b FIX 2026-10-11：压缩前先把所有图表代码块替换为占位符。
            # AI 压缩时可能会截断/篡改 ```mermaid / ```chart-json 围栏内容，
            # 导致渲染失败。占位符是纯文本 <<CHART_BLOCK_N>>，不参与字数统计
            # 且 AI 不理解其含义（不会修改），压缩完成后按序还原为原文。
            _masked_content, _chart_originals = _mask_chart_fences_for_compression(
                current_content
            )
            # 占位符自身是简单文本，字数远小于原始图表代码块——把目标字数
            # 相应下调，否则 AI 看到的正文偏短、会错误扩张图表块的占位符
            # （虽然无法真正扩张，但可能把占位符字面量写进压缩后正文）。
            # BUG-FIX 2026-10-17（P2·多轮压缩基准陈旧）：_charts_delta 旧按
            # **首轮**原始值 _wc 计算，第 2 轮起 current_content 已变（首轮
            # 已压缩），偏移量系统性偏大 → _effective_target 过小、提示词
            # 「原文 N 字」播报陈旧。改为按**本轮** current_content 计算。
            _cur_wc = _wc_fn(current_content) if current_content else 0
            _charts_delta = max(_cur_wc - _wc_fn(_masked_content), 0)
            _effective_target = max(target_words - _charts_delta, 100)

            user_msg = (
                f"章节标题：{section_title}\n\n"
                f"目标字数：约 {_effective_target} 字（允许浮动 ±{int(tolerance_ratio * 100)}%，"
                f"区间 {max(meta['lo'] - _charts_delta, 50)}~{max(meta['hi'] - _charts_delta, 100)} 字）。\n\n"
                f"以下是当前正文（{_wc_fn(_masked_content)} 字，原文 {_cur_wc} 字），超出目标区间，请压缩到目标区间内，"
                f"保留必含要素、数据、结构与小标题，删除空话套话与冗余表述：\n\n"
                f"{_masked_content}"
            )
            messages = [
                AIMessage(role="system", content=sys_prompt),
                AIMessage(role="user", content=user_msg),
            ]
            if provider is None or not hasattr(provider, "chat"):
                raise RuntimeError("provider 为空或无 chat 接口")
            # 使用统一的 with_timeout 保护（来自 workflows_base）
            try:
                from .workflows_base import with_timeout
            except Exception:
                with_timeout = None
            if with_timeout is not None:
                raw = await with_timeout(
                    provider.chat(messages, temperature=0.3),
                    timeout=timeout_seconds,
                )
            else:
                raw = await provider.chat(messages, temperature=0.3)
            # HARDEN 2026-10-12：AI 网关在 Windows 环境下常返回 \r\n，
            # 压缩链路不走 clean_generated_content（否则会把图表占位符
            # <<CHART_BLOCK_N>> 识别为普通段落），此处独立归一一层。
            condensed_masked = normalize_newlines(str(raw or "")).strip()
            if not condensed_masked:
                meta["error"] = "AI 返回空内容"
                break
            # BUG-FIX 2026-10-17（P1）：还原前校验占位符存在性 + 唯一性。
            # AI 删除占位符 → 图表静默丢失；AI 复制占位符 → 图表重复插入。
            # 二者都不报错但数据坏，必须拒绝压缩、保留原文（见函数文档）。
            _ph_ok, _ph_reason = _validate_chart_placeholders(
                condensed_masked, _chart_originals
            )
            if not _ph_ok:
                logger.warning(
                    "[压缩] 第 %d 轮图表占位符校验失败（%s），拒绝本次压缩、保留原文",
                    _round + 1, _ph_reason,
                )
                meta["error"] = f"AI 破坏图表占位符（{_ph_reason}），保留原文"
                break
            # BUG-P1b FIX 2026-10-11：压缩后还原图表代码块。
            condensed = _restore_chart_fences_after_compression(
                condensed_masked, _chart_originals
            )
            # Risk-P2b FIX 2026-10-11：检查残留占位符。
            # AI 可能在压缩时篡改占位符（加标点、改字符、把占位符写进正文），
            # 导致 restore 无法匹配，condensed 里残留占位符痕迹。
            # 用正则匹配 <<...BLOCK...>> 模式（中间可能被篡改），比硬编码
            # 子串搜索更鲁棒。残留则拒绝压缩，保留原文。
            _leftover_re = re.compile(r"<<[^>]*[Bb][Ll][Oo][Cc][Kk][^>]*>>")
            _leftover_placeholder = bool(_leftover_re.search(condensed))
            if _leftover_placeholder:
                logger.warning(
                    "[压缩] 第 %d 轮还原后仍残留占位符（AI 可能篡改了占位符文本），"
                    "拒绝本次压缩、保留原文", _round + 1,
                )
                meta["error"] = "AI 篡改占位符导致还原失败，保留原文"
                break
            _new_wc = _wc_fn(condensed) if condensed else 0
            # BUG-FIX 2026-10-17（P2·多轮压缩结构性不可达 + 部分成果被丢弃）：
            # 旧判定 `_new_wc <= threshold` 与紧随的 `if _new_wc <= meta["hi"]:
            # break` 中，threshold 与 meta["hi"] 是同式（int(target*(1+tol))）
            # → 第 2 轮起要么因「达标」break、要么因「未达标」break，
            # max_rounds>1 是死配置；且「有进展但未进区间」的压缩稿被整体
            # 丢弃、保留更长原文（探针 _audit6_probe.py::C1 实证只跑 1 轮）。
            # 新判定：只要相对上一轮有实质压缩进展就接受本轮成果；已进
            # hi 区间或轮数用尽才退出；毫无进展才报错并保留上一版。
            if _new_wc <= current_wc:
                current_content = condensed
                current_wc = _new_wc
                if _new_wc <= meta["hi"]:
                    break
                if _round + 1 >= max(1, max_rounds):
                    logger.info(
                        "[压缩] 第 %d 轮后 %d 字仍超上限 %d，轮数用尽，保留已压缩成果",
                        _round + 1, _new_wc, meta["hi"],
                    )
                    break
                logger.info(
                    "[压缩] 第 %d 轮后 %d 字仍超上限 %d，继续第 %d 轮",
                    _round + 1, _new_wc, meta["hi"], _round + 2,
                )
            else:
                meta["error"] = f"压缩后字数未减少（上轮 {current_wc} → 新 {_new_wc}）"
                break
        except Exception as _e:
            meta["error"] = f"压缩第 {_round + 1} 轮异常: {_e}"
            break

    meta["rounds"] = actual_rounds
    meta["condensed_wc"] = current_wc
    meta["condensed"] = current_wc < _wc
    if current_wc < _wc:
        return current_content, meta
    return content, meta





def _is_rate_limit_error(error: Exception) -> bool:
    """判断错误是否为「上游 API 限流（HTTP 429）」。

    BUG-FIX 2026-10-07：此前该函数定义了却**零调用点**（死代码），
    导致上层无法区分「限流」与「业务错误」，只能对所有失败用同一套
    1s/2s 退避重试 —— 在持续限流时反而放大冲击（实测 528 次 429 全军覆没）。
    现由 routers/sse/sse_content.py 的章节级重试调用以分流退避策略。

    关键词依据 logs/backend.log 中的真实错误文本：
        请求频率过高，重试失败 (HTTP 429): {... "您已达到免费用户的 API 速率限制 ..."}
    因此除英文关键词外，必须覆盖中文「速率限制 / 频率过高 / 限流 / 配额」等表述。
    """
    if error is None:
        return False
    err_str = str(error).lower()
    keywords = (
        "rate limit",
        "rate_limit",
        "too many requests",
        "http 429",
        "(429)",
        "限流",
        "速率限制",
        "频率过高",
        "请求过于频繁",
        "超过速率",
    )
    return any(kw in err_str for kw in keywords)



# ENHANCE 2026-10-06（续写衔接加固）：finish_reason=='length' 表示上一轮被输出长度上限
# 截断，模型并不清楚确切截断点，续写时极易把「已写内容末段」整段重发，造成正文重复。
# 仅在「接缝处」剥离 extra 前缀中与 final_content 尾部完全一致的重叠段：
#   - 只比对 final_content 末尾 _MAX_SEAM_OVERLAP 字符，避免误伤正文内部的正常重复短语；
#   - 剥离的是重复而非新内容，不破坏 CONTENT_CONTINUE_PROMPT 要求的「无缝衔接」设计。
_MAX_SEAM_OVERLAP = 400
# 续写提示词注入的已写内容上限（字符）。超长章节（extra_long 数千字）逐轮把全部
# 已写内容回灌会撑爆上下文窗口；模型续写只需「最近尾部」即可无缝衔接，故超阈值只取尾段。
_MAX_CONTINUE_CONTEXT_CHARS = 6000


def _seam_overlap_min_chars() -> int:
    """读 config.SEAM_OVERLAP_MIN_CHARS（惰性读取以支持测试打桩）。

    0 表示完全回退到历史行为（k=1 起逐字探测）。
    """
    from app.config import SEAM_OVERLAP_MIN_CHARS  # noqa: PLC0415

    return int(SEAM_OVERLAP_MIN_CHARS)


def _strip_seam_overlap(final_content: str, extra: str) -> str:
    """剥离 extra 前缀中与 final_content 尾部重叠的重复段（接缝去重）。

    返回去掉重叠前缀后的 extra；无重叠则原样返回。

    Risk-P2c FIX 2026-10-15：为「部分重叠」设置最小长度阈值。
    旧实现从最长一路探测到 k=1，任何 1~2 字的巧合匹配都被当作「截断续写」剥掉，
    例如 final="本阶段已完成采购" + extra="采购的时间节点如下：" → 剥掉「采购」
    留下「的时间节点如下：」——主语丢失、句子语义断裂。中文 2 字词（采购/完成/
    阶段/体系/质量/安全）在相邻段落高频同现，误剥概率很高。

    现规则（探针：backend/scripts/_audit5_recheck.py）：
      - **完全重叠**（extra 整体都在 final 尾部窗口内，k == len(extra)）：
        无条件剥离，不受阈值影响。此时 AI 未写出任何新内容，保留只是把同一段话
        重复一遍——历史契约见 test_content_continue_hardening_2026_10_06.py::
        test_strip_seam_overlap_full_overlap（k=4 仍须整段剥离）。
      - **部分重叠**：仅当 k >= SEAM_OVERLAP_MIN_CHARS（默认 6）才剥离；
        低于阈值的短匹配视为「新句首词与上一轮截断处巧合相同」。
    """
    if not final_content or not extra:
        return extra
    tail = final_content[-_MAX_SEAM_OVERLAP:]
    min_k = _seam_overlap_min_chars()
    for k in range(min(len(tail), len(extra)), 0, -1):
        if extra[:k] != tail[-k:]:
            continue
        # 完全重叠（剥完 extra 归零）无条件生效；部分重叠须达最小长度。
        if k >= min_k or k >= len(extra):
            return extra[k:]
    return extra


# =====================================================================
# BUG-P1a FIX 2026-10-11：续写截断在图表围栏内部 → 产生未闭合围栏
# -----------------------------------------------------------------------------
# 缺口：AI 输出因 finish_reason=='length' 被截断时，截断点可能正处于
# ```mermaid / ```chart-json 围栏内部。续写路径 _auto_continue_on_length
# 不感知围栏状态，续写出的正文会包含一个永远不会被闭合的图表围栏 →
# content_cleaning.parse_content 把它当成坏块整块删除 → 图表静默丢失。
#
# 修复：检测未闭合图表围栏，改写续写 prompt 明确告知 AI 先补完截断处
# 并闭合围栏再继续写正文。
# =====================================================================
def _detect_unclosed_chart_fence(content: str) -> dict | None:
    """检测正文中是否存在未闭合的图表围栏。

    BUG-FIX 2026-10-15（第六轮·消除第三份围栏骨架）：旧实现自带
    ``_FENCE_OPEN_RE`` / ``_FENCE_CLOSE_RE`` 两个手写正则，与 content_cleaning 的
    单一权威规划器 ``plan_chart_fence_lines`` 分叉，实测三类误判：

      ① **假阳性（最严重）**：AI 把闭合行也写上属性串（`` ```mermaid {json} ``，
         这是 BUG-FIX 2026-10-09 明确支持的写法），旧 ``_FENCE_CLOSE_RE`` 的
         ``[A-Za-z0-9_\\-+.#]*`` 匹配不到 ``{json}`` → 把已闭合块判为未闭合 →
         ``_force_close_unclosed_chart_fence`` 在**文档末尾**追加一行游离的裸围栏
         标记 → 该行原样进入正文与 SSE 流（导出文档尾部出现孤立的 `` ``` ``）。
      ② **假阴性**：闭合判定不看开启标记的字符与长度，``~~~mermaid`` 被
         `` ``` `` 关闭、```` ````mermaid`` 被 `` ``` `` 关闭都判为「已闭合」。
      ③ 无「带 info-string 的围栏行 = 新块起始」的前瞻判定，语义与
         ``plan_chart_fence_lines``（BUG-FIX 2026-10-14）不一致。

    现直接调用 ``content_cleaning.find_unclosed_chart_fence``（与解析侧共用同一份
    规划器），解析 / 检测 / 兜底三处口径彻底同源，不可能再各自漂移。

    Returns:
        dict: {
            "lang": "mermaid" / "chart-json",
            "open_line": str,         # 围栏开启行原文（含 ``` 前缀）
            "open_line_no": int,      # 围栏开启所在行号（1-based）
            "open_marker": str,       # 开启标记原文（``` / ~~~ / ```` …）
            "open_marker_len": int,   # 开启标记长度
            "close_insert_idx": int,  # 闭合标记应插入的行号（0-based）
            "content_before_open": str,  # 围栏之前的正文
            "content_inside": str,      # 围栏内部的（被截断的）载荷行
        }
        None: 无未闭合图表围栏
    """
    if not content:
        return None
    lines = content.split("\n")
    info = content_cleaning.find_unclosed_chart_fence(lines)
    if info is None:
        return None
    open_idx = info["open_index"]
    insert_at = info["close_insert_idx"]
    return {
        "lang": info["lang"],
        "open_line": lines[open_idx] if open_idx < len(lines) else "```mermaid",
        "open_line_no": open_idx + 1,
        "open_marker": info["marker"],
        "open_marker_len": info["marker_len"],
        "close_insert_idx": insert_at,
        "content_before_open": "\n".join(lines[:open_idx]),
        # 精确取「未闭合块的载荷行」（plan 已剥离误吞进围栏体的正文段），
        # 旧版取到「整个文档最后一个非空行」，会把块之后的正文一并算进来。
        "content_inside": "\n".join(lines[open_idx + 1: insert_at]),
    }


# =====================================================================
# Risk-P2a FIX 2026-10-11：程序化兜底闭合未闭合图表围栏
# -----------------------------------------------------------------------------
# BUG-P1a 的 prompt 追加指令是"软约束"——AI 可能仍忽略、或续写后又被
# length 截断（续写的续写），最终内容里仍残留未闭合围栏。
# 本函数在 AI 返回后做确定性后处理：检测到未闭合围栏 → 直接在末尾
# 追加 ``` 闭合行。比 content_cleaning 把整块当坏块删除好：
#   - 至少保留了图表源码（用户能看到原始 mermaid/chart-json）
#   - 渲染器会尝试渲染，失败则记录警告（不是静默丢失）
# =====================================================================
def _force_close_unclosed_chart_fence(content: str) -> tuple[str, bool]:
    """程序化兜底：检测并闭合未闭合的图表围栏。

    Returns:
        tuple: (处理后的内容, 是否发生了闭合)

    BUG-FIX 2026-10-15（第六轮·加固），两处旧行为修正：

      ① 闭合标记原本硬编码为 3 个反引号，而开启标记可能是波浪线或 4+ 反引号
         （``~~~mermaid`` / ```` ````mermaid`` 会被补成 `` ``` ``）→ 产出的围栏
         不满足 Markdown「闭合标记同字符且长度不小于开启标记」的要求。
         现按开启标记的**原字符与原长度**补闭合行。
      ② 原本一律把闭合行追加到**文档末尾**。当未闭合块位于文档中部时
         （AI 漏写闭合行后紧接写下一张同语言图），追加到末尾会
         (a) 留下一行游离的裸围栏标记进入正文与 SSE 流；
         (b) 被误吞进第一个围栏体的正文行仍留在围栏内。
         现改为插到**载荷末尾**（``close_insert_idx``）——对「续写被截断在
         文档末尾」这一原始场景，插入点恰好就是 EOF，行为与旧版完全一致。
    """
    unclosed = _detect_unclosed_chart_fence(content)
    if unclosed is None:
        return content, False
    marker = unclosed.get("open_marker") or "```"
    insert_at = unclosed["close_insert_idx"]
    lines = content.split("\n")
    if insert_at >= len(lines):
        # 未闭合块直达 EOF（续写被截断的典型场景）：保持旧版尾部形态——
        # 整体 rstrip 后闭合标记独占一行、文档以换行结尾。
        head = "\n".join(lines).rstrip()
        candidate = f"{head}\n{marker}\n"
    else:
        # 未闭合块位于文档中部（AI 漏写闭合行后紧接写下一张图）：把闭合行插到
        # 载荷末尾而非追加到 EOF——否则被 _split_fence_body 剥离出的正文会留在
        # 围栏体外，且文档尾部残留一行游离的裸围栏标记。
        lines.insert(insert_at, marker)
        candidate = "\n".join(lines)
    # HARDEN 2026-10-15（第六轮·幂等）：病态超长围栏的载荷行数会触及
    # content_cleaning._HARD_CHART_FENCE_LINE_LIMIT，规划器看不到截断点之后的行，
    # 插入闭合行对它无效。不拦截的话，重复调用会每次往同一位置再插一行标记
    # （实测文档每次 +4 字节）。以「同一位置的同一未闭合块」为无进展判据。
    again = _detect_unclosed_chart_fence(candidate)
    if (again is not None
            and again["open_line_no"] == unclosed["open_line_no"]
            and again["close_insert_idx"] == insert_at
            and again["content_inside"] == unclosed["content_inside"]):
        logger.warning(
            "[围栏兜底] 未闭合 %s 围栏（第 %d 行）载荷超出围栏行数硬上限，"
            "程序化闭合无法生效，保持原样（该块按坏块处理）",
            unclosed["lang"], unclosed["open_line_no"],
        )
        return content, False
    logger.warning(
        "[围栏兜底] 已为未闭合 %s 围栏（第 %d 行，%s 标记）在第 %d 行后补 %s 闭合",
        unclosed["lang"], unclosed["open_line_no"], marker, insert_at, marker,
    )
    return candidate, True


# =====================================================================
# BUG-P1b FIX 2026-10-11：压缩正文时保护图表代码块
# -----------------------------------------------------------------------------
# 缺口：condense_overlong_content 直接把含 ```mermaid / ```chart-json
# 的正文送给 AI 压缩。虽然提示词 CONTENT_CONDENSE_SYSTEM 写了"图表代码块
# 原样保留"，但 AI 不总是遵守指令——特别是当图表块很长、压缩目标很紧时，
# AI 可能截断 mermaid 源码或修改 JSON payload → 渲染失败。
#
# 修复：压缩前把所有图表代码块替换为占位符 <<CHART_BLOCK_1>>，
# 压缩后再按序还原。占位符不参与压缩计数，还原后语法 100% 与原文一致。
# =====================================================================
# ENHANCE 2026-10-15（第六轮·围栏骨架单一权威化）：围栏标记提取与图表围栏
# 区间扫描的权威实现已下沉到 content_cleaning（见其「第六轮·图表围栏「行区间」
# 单一权威化」注释块）。本模块保留同名私有别名——既有调用方与测试的 import
# 路径完全不变，且不存在两份遍历骨架。
#: 向后兼容别名（权威实现：content_cleaning.FENCE_HEAD_PATTERN / fence_marker）
_FENCE_HEAD_RE = content_cleaning.FENCE_HEAD_PATTERN
_fence_marker = content_cleaning.fence_marker


def _scan_chart_fence_blocks(lines: list[str]) -> list[tuple[int, int]]:
    """扫描**已闭合**的图表围栏块，返回 [(开行号, 闭行号)]（0-based，含端点）。

    BUG-FIX 2026-10-15（P1·图表在压缩环节静默损坏）：
    旧实现是 hand-rolled 正则 `r"(```...```)"`，硬编码 3 个反引号，两类问题：
      1. 不认波浪线围栏 —— `~~~mermaid` / `~~~chart-json` 不被 mask，原始
         围栏整块送入 AI 压缩，AI 可截断/改写 mermaid 源码与 JSON 载荷；
      2. 按子串匹配而非按围栏标记行匹配 —— ```` ````mermaid ````（4 反引号，
         嵌套代码块的常见写法）被从第 2 个反引号起错位匹配成 ```mermaid，
         占位符被多余的 ` 包裹（`<<CHART_BLOCK_1>>`），AI 可能改写反引号，
         还原后围栏语法损坏。

    2026-10-15（第五轮）改为按行扫描并复用 content_cleaning 的围栏判定口径；
    2026-10-15（第六轮）进一步下沉到 content_cleaning.scan_chart_fence_blocks——
    本模块不再保留任何自写遍历骨架，与解析侧 plan_chart_fence_lines 彻底同源。
    附带修正：旧实现的闭合规则比解析侧**更严**（额外要求同字符且长度 >= 开启
    标记），导致「~~~mermaid … ```」这类跨字符围栏在解析侧被判为有效块并渲染、
    却在保护侧不被 mask → 原始 mermaid 源码裸露送给压缩 AI。现两处口径一致，
    受保护范围严格变宽。
    未闭合块不返回（保持原样，交由 _force_close_unclosed_chart_fence 兜底闭合）。
    """
    return content_cleaning.scan_chart_fence_blocks(lines)


def _mask_chart_fences_for_compression(content: str) -> tuple[str, list[str]]:
    """把正文中的 ```mermaid / ```chart-json 围栏代码块替换为占位符。

    Returns:
        tuple: (masked_content, originals)
        - masked_content: 替换后的文本（含 <<CHART_BLOCK_1>> 等占位符）
        - originals: 被替换掉的原始围栏代码块列表（完整围栏原文，含标记行）

    BUG-FIX 2026-10-15（P1）：改为「行区间扫描 + 行区间替换」，围栏口径与
    content_cleaning 共享（见 _scan_chart_fence_blocks）。行区间替换保证块前后
    的空行与文本逐字符不变，mask → restore 严格往返。
    """
    if not content:
        return content, []
    lines = content.split("\n")
    blocks = _scan_chart_fence_blocks(lines)
    if not blocks:
        return content, []
    originals = ["\n".join(lines[s:e + 1]) for s, e in blocks]
    out = lines[:]
    # 从后往前替换，避免行号失效
    for idx in range(len(blocks) - 1, -1, -1):
        s, e = blocks[idx]
        out[s:e + 1] = [f"<<CHART_BLOCK_{idx + 1}>>"]
    return "\n".join(out), originals


def _restore_chart_fences_after_compression(masked_content: str, originals: list[str]) -> str:
    """把压缩后正文中的占位符还原为原始图表代码块。"""
    if not originals:
        return masked_content
    restored = masked_content
    for i, original in enumerate(originals, start=1):
        placeholder = f"<<CHART_BLOCK_{i}>>"
        # 精确匹配占位符字面量，不吞前后空白
        # 这样 original 前后的所有 \n（来自 mask 时的 regex 捕获 + AI 压缩可能添加的段落分隔）
        # 都能被完整保留
        restored = restored.replace(placeholder, original)
    return restored


# 压缩占位符字面量（与 _mask_chart_fences_for_compression 产出一一对应）
_CHART_PLACEHOLDER_RE = re.compile(r"<<CHART_BLOCK_(\d+)>>")


def _validate_chart_placeholders(masked_content: str, originals: list[str]) -> tuple[bool, str]:
    """校验 AI 压缩返回中每个图表占位符**恰好出现一次**。

    BUG-FIX 2026-10-17（P1·压缩环节图表静默丢失/重复插入）：
    ``_restore_chart_fences_after_compression`` 用 ``str.replace`` 按序还原，
    对 AI 破坏占位符毫无抵抗力：
      · AI **删除**某占位符 → 对应图表静默丢失。还原后残留正则
        （``<<...BLOCK...>>``）只能发现「被篡改」，不能发现「被删除」
        （探针 scripts/_audit6_probe.py::B1 实证：chart-json 载荷消失且
        残留检测不报警）；
      · AI **复制**占位符 → 同一图表被重复插入正文（B1b 实证）。
    二者都是「不报错但数据坏」的静默故障，故在还原前做存在性 + 唯一性校验。

    Args:
        masked_content: AI 压缩返回（尚未还原占位符的文本）
        originals: mask 时保护的原始图表块列表

    Returns:
        tuple[bool, str]: (是否通过, 失败原因)；通过时原因为空串。
    """
    if not originals:
        return True, ""
    counts: dict[int, int] = {}
    for m in _CHART_PLACEHOLDER_RE.finditer(masked_content or ""):
        idx = int(m.group(1))
        counts[idx] = counts.get(idx, 0) + 1
    missing = [i for i in range(1, len(originals) + 1) if counts.get(i, 0) == 0]
    if missing:
        return False, f"占位符被删除: {missing}"
    duplicated = sorted(i for i, c in counts.items() if c > 1)
    if duplicated:
        return False, f"占位符被复制: {duplicated}"
    return True, ""


# BUG-FIX 2026-10-17（第七轮·列表样式被拍平）：续写内容的结构标记行首
# 判定（列表 / ATX 标题 / 引用）。用于 _auto_continue_on_length 的合并
# 换行决策，详见该处注释。
_CONTINUE_STRUCT_LINE_START_RE = re.compile(
    r"^(?:"
    r"[-*+][ \t]"                    # 无序列表：- / * / + + 空白
    r"|\d{1,4}[.、．)）][ \t]"       # 有序列表：1. / 1、/ 1) + 空白
    r"|#{1,6}[ \t]*[^#\s]"           # ATX 标题：## 小标题（排除裸 ###）
    r"|>[ \t]?"                      # 引用块：> text
    r")"
)


async def _auto_continue_on_length(
    provider: Any,
    existing_content: str,
    section: dict,
    context: dict,
    base_chat_kwargs: dict,
    max_rounds: int = 3,
) -> str:
    """当 finish_reason=='length' 时自动续写（CONTENT_CONTINUE_PROMPT）。

    最多续写 max_rounds 轮，每轮合并新内容。终止条件：
      - finish_reason 不再是 length（正常结束）
      - 续写内容为空/重复
      - 达到最大轮数
      - 用户取消
    """
    from .providers.base import AIMessage as _AIMessage
    import logging as _logging
    _logger = _logging.getLogger(__name__)
    final_content = existing_content
    writing_length = context.get("writing_length", "detailed")
    cancel_event = context.get("cancel_event")

    for round_num in range(1, max_rounds + 1):
        if cancel_event and cancel_event.is_set():
            _logger.warning("[续写] 第 %d 轮前用户取消", round_num)
            break

        try:
            target_chars = min(
                final_content.__len__() * 2,
                max(1500, final_content.__len__() + 2000),
            )
            # ENHANCE 2026-10-17（P2·续写与字数控制对齐）：target_chars 原按
            # 「字符数 ×2 / +2000」翻倍，L3 章节（目标 300 字）因 finish_reason
            # ==length 被截断续写时越写越长，随后又被 condense_overlong_content
            # 压回硬区间，形成「续写涨→压缩删」的无效往返（多一次 AI 调用、
            # 过程中偏离 [lo,hi]）。当章节带 target_words（_build_leaf_dicts
            # 已为叶子装配）时，按「剩余字数预算 + 200 余量」收敛续写目标；
            # 无 target_words（L1/L2 叶子等）时行为与旧实现逐字一致。
            _wc_target = None
            try:
                _wt = section.get("target_words") if isinstance(section, dict) else None
                if _wt:
                    _wc_target = int(_wt)
            except (TypeError, ValueError):
                _wc_target = None
            if _wc_target and _wc_target > 0:
                _remain = _wc_target - (count_words(final_content) if final_content else 0)
                target_chars = min(target_chars, max(200, _remain + 200))
            # ENHANCE 2026-10-06：超长章节逐轮把全部已写内容回灌会撑爆上下文窗口。
            # 续写只需「最近尾部」即可无缝衔接，故超过上限只取末尾 _MAX_CONTINUE_CONTEXT_CHARS。
            _existing_for_prompt = (
                final_content
                if len(final_content) <= _MAX_CONTINUE_CONTEXT_CHARS
                else final_content[-_MAX_CONTINUE_CONTEXT_CHARS:]
            )
            # SECURITY-FIX 2026-10-06：existing_content 为已生成正文（基于招标文件内容），
            # 注入续写提示词前做元标记中和，纵深防御。
            prompt_text = get_prompt(
                "CONTENT_CONTINUE_PROMPT",
                existing_content=_existing_for_prompt,
                target_chars=target_chars,
                writing_length=writing_length,
                sanitize=True,
            )
            # BUG-P1a FIX 2026-10-11：检测截断是否发生在图表围栏内部。
            # 若 final_content 有未闭合的 ```mermaid / ```chart-json 围栏，
            # 则续写 prompt 末尾追加强制指令：先补完截断处、闭合围栏再继续写正文。
            # 否则 AI 会在未闭合的围栏后直接写普通正文 → 最终正文包含一个
            # 永远不会被闭合的图表围栏 → content_cleaning 把整块当坏块删除 →
            # 图表静默丢失（前端无任何感知）。
            _unclosed = _detect_unclosed_chart_fence(final_content)
            if _unclosed is not None:
                _logger.info(
                    "[续写] 检测到未闭合 %s 围栏（第 %d 行），追加闭合指令",
                    _unclosed["lang"], _unclosed["open_line_no"],
                )
                _fence_guard = (
                    f"\n\n### 强制指令（围栏修复）\n"
                    f"上方正文在第 {_unclosed['open_line_no']} 行开始了一个"
                    f" {_unclosed['open_marker']}{_unclosed['lang']} 代码块，"
                    f"但该代码块因输出被截断而未闭合。"
                    f"请先从截断处继续补完该 {_unclosed['lang']} 代码块的剩余部分，"
                    f"然后用同样的围栏标记（{_unclosed['open_marker']}）闭合代码块。"
                    f"**闭合围栏之后**再继续写正文剩余内容。"
                    f"严禁跳过闭合围栏直接从截断处写普通正文，"
                    f"那样会导致图表永远无法被正确解析。"
                )
                prompt_text = prompt_text + _fence_guard
        except Exception as prompt_err:
            _logger.warning("[续写] prompt 渲染失败: %s", prompt_err)
            break

        continue_messages = [_AIMessage(role="user", content=prompt_text)]
        continue_kwargs = dict(base_chat_kwargs)
        # BUG-FIX 2026-10-04（P1·BUG-A）：原实现 pop 掉 return_meta，provider 只返回 str，
        # 下方 next_finish 恒为 "stop" → 第 1 轮后必 break，max_rounds=3 形同虚设，
        # 超长章节仍被截断。显式置 True 让每轮都能拿到真实 finish_reason。
        # （provider 若不支持 return_meta 会返回 str，下方 isinstance 分支已兜底）
        continue_kwargs["return_meta"] = True

        try:
            raw = await provider.chat(continue_messages, **continue_kwargs)
        except Exception as chat_err:
            _logger.warning("[续写] 第 %d 轮 AI 调用失败: %s", round_num, chat_err)
            break

        if isinstance(raw, dict):
            extra = raw.get("content", "")
            next_finish = raw.get("finish_reason", "stop")
        elif hasattr(raw, "content"):
            # 与主调用保持一致：部分 provider 返回带 .content 属性的对象，
            # 若走 str(raw) 会把对象 repr 当成正文拼进章节。
            extra = raw.content
            next_finish = getattr(raw, "finish_reason", "stop")
        else:
            extra = str(raw)
            next_finish = "stop"

        extra = extra.strip()
        if not extra:
            _logger.info("[续写] 第 %d 轮返回空内容，停止续写", round_num)
            break

        # ENHANCE 2026-10-06：接缝去重 —— 模型被 length 截断后续写常把末段整段重发。
        # 剥离 extra 前缀中与 final_content 尾部重叠的重复段（仅接缝处，不影响无缝衔接）。
        _extra_before = extra
        extra = _strip_seam_overlap(final_content, extra)
        if extra != _extra_before:
            _logger.info(
                "[续写] 第 %d 轮接缝去重剥离 %d 字重复段", round_num,
                len(_extra_before) - len(extra),
            )
        if not extra:
            _logger.info("[续写] 第 %d 轮接缝去重后无新内容，停止续写", round_num)
            break

        if extra == final_content.strip() or extra in final_content:
            _logger.info("[续写] 第 %d 轮返回重复内容，停止续写", round_num)
            break

        # BUG-FIX 2026-10-17（第七轮·列表样式被拍平）：续写内容以列表 / 标题 /
        # 引用等**结构标记**开头时必须插换行——AI 被 length 截断后，续写轮
        # 常以新列表项或小标题起笔，旧实现直接字符串拼接会把「…要求如下：」
        # 与「- 材料准备」粘成同一行，导出端列表被拍平为段落文本（用户修复
        # 重点「列表样式被拍平」）。无缝接续的普通句子仍不插换行（中英文
        # 截断续写不应被强行断行）。有序列表分支要求「分隔符 + 空白」，
        # 不误伤 "1.8米" / "2026年" / "1) " 之外的数字开头正文。
        if _CONTINUE_STRUCT_LINE_START_RE.match(extra):
            final_content = final_content.rstrip() + "\n" + extra
        else:
            final_content = final_content.rstrip() + extra
        _logger.info(
            "[续写] 第 %d 轮完成，新增 %d 字，总 %d 字 (finish_reason=%s)",
            round_num, len(extra), len(final_content), next_finish,
        )

        if next_finish != "length":
            break

    # Risk-P2a FIX 2026-10-11：续写退出前做一次程序化兜底闭合。
    # 无论正常结束还是 max_rounds 耗尽，都保证最终返回的 content 不含未闭合围栏。
    final_content, _closed = _force_close_unclosed_chart_fence(final_content)

    return final_content



def _context_message_priority(content: str) -> int:
    """上下文消息裁剪优先级（tier 越小越先被裁剪）。

    依据外部引用软件《标书智能体（3）——生成标书正文代码+提示词》的关键细节：
    「上下文过长时按优先级截断，优先保留上级章节与当前章节信息」。

    tier 语义：
      0 同级章节（仅用于防重复，可最先裁）→ 1 事实变量 → 2 下级目录章节
      → 3 全篇叙事骨架 → 4 前序章节内容摘要 → 5 知识库素材 → 6 其他参考段
      → 7 项目稳定上下文 → 8 上级章节（最后裁）→ 9 当前章节任务（永不裁）
    """
    head = (content or "")[:60]
    if head.startswith("# 章节: "):
        return 9
    for tier, prefix in (
        (0, "同级章节"),
        (1, "事实变量:"),
        (2, "下级目录章节"),
        (3, "全篇叙事骨架"),
        (4, "前序章节内容摘要"),
        (5, "知识库素材"),
        (6, "章节位置:"),
        (7, "项目摘要:"),
        (7, "承包范围:"),
        (7, "招标文件已载明的工程作法"),
        (7, "适用规范清单"),
        (8, "上级章节"),
    ):
        if head.startswith(prefix):
            return tier
    return 6


# tier 9 = 当前章节任务信息，按参考标准「优先保留当前章节信息」永不裁剪。
_CONTEXT_TIER_NEVER_TRUNCATE = 9


def _degrade_long_context_messages(messages: list, section: dict) -> list:
    """长上下文确定性降级（2026-10-20 易标机制引入，规格三.7）。

    易标在消息总长超模型上下文 70%（AGENT_CONTEXT_THRESHOLD_RATIO）时自动
    切 Agent 隔离环境处理；本软件无 Agent 运行时，等效降级策略：
      1. 总长 ≤ limit×ratio 阈值 → 原样返回（零开销路径，默认关闭时不进本函数）；
      2. 超阈值 → **按优先级**裁剪参考资料段（优先保留上级章节与当前章节信息，
         见 _context_message_priority），逐条削减直到总长回到阈值内；
      3. 被裁剪的消息尾部追加「已截断」标记，避免模型把半截资料当完整资料；
      4. section['_context_degraded'] 留痕，供上游 SSE verification 事件上报。

    保守性：只减参考性长段（user 消息），绝不删消息、绝不动 system；
    AIMessage/dict 两种形态都支持；任何异常回落原始 messages（宁可不降级，
    不能降坏）。调用方受 CONTENT_CONTEXT_DEGRADE_ENABLED 开关控制，默认关闭。
    """
    try:
        from ...config import (
            CONTENT_CONTEXT_DEGRADE_RATIO,
            CONTENT_CONTEXT_LIMIT_CHARS,
        )

        threshold = int(CONTENT_CONTEXT_LIMIT_CHARS * CONTENT_CONTEXT_DEGRADE_RATIO)

        def _content_of(m: Any) -> str:
            if isinstance(m, dict):
                return str(m.get("content") or "")
            return str(getattr(m, "content", "") or "")

        def _total_len(ms: list) -> int:
            return sum(len(_content_of(m)) for m in ms)

        total = _total_len(messages)
        if total <= threshold:
            return messages

        overflow = total - threshold
        truncated_any = False
        # 截断标记长度：必须按实际长度计入预算，否则「裁剪后补标记」会再次
        # 撑破阈值，剩余溢出量转而去裁更高优先级段（上级章节），与参考标准
        # 「优先保留上级章节与当前章节信息」相悖。
        _MARKER = "\n…（参考资料因超出模型上下文已截断，以完整资料页为准）"
        _marker_len = len(_MARKER)
        # 裁剪顺序 = 语义优先级升序（同级章节 → 事实 → 骨架 → … → 上级章节），
        # 当前章节任务（tier 9）永不进入候选。
        # BUG-FIX 2026-10-23（对齐外部引用标准「优先保留上级章节与当前章节信息」）：
        # 原实现按「消息长度降序」裁剪 —— 最长的「当前章节任务」段（标题+概要+
        # 评分要点+字数区间+生成要求，通常是全栈最长）**最先被裁**，「上级章节」
        # 段也可能先于同级被裁，与参考标准正好相反。现改为按优先级裁剪。
        candidates = []
        for i, m in enumerate(messages):
            if getattr(m, "role", None) == "system" or (
                isinstance(m, dict) and m.get("role") == "system"
            ):
                continue
            content = _content_of(m)
            tier = _context_message_priority(content)
            if tier >= _CONTEXT_TIER_NEVER_TRUNCATE:
                continue  # 当前章节任务信息：参考标准要求优先保留，绝不裁剪
            candidates.append((tier, i, content))
        # 同优先级内仍按长度降序（长段先裁，收敛更快）
        candidates.sort(key=lambda x: (x[0], -len(x[2])))
        for _tier, idx, content in candidates:
            if overflow <= 0:
                break
            # 每条最多削减自身 80%，保留主体语境；
            # 裁剪量需覆盖「溢出量 + 标记长度」，保证裁完即回到阈值内
            # （否则会溢出到更高优先级段）。
            cut = min(len(content) - 200 if len(content) > 400 else 0,
                      overflow + _marker_len, int(len(content) * 0.8))
            if cut <= 0:
                continue
            new_content = (
                content[: len(content) - cut] + _MARKER
            )
            m = messages[idx]
            if isinstance(m, dict):
                m["content"] = new_content
            else:
                try:
                    m.content = new_content
                except Exception:
                    messages[idx] = type(m)(role=getattr(m, "role", "user"), content=new_content)
            truncated_any = True
            overflow = _total_len(messages) - threshold

        if truncated_any:
            section["_context_degraded"] = True
            logger.warning(
                "[长上下文降级] 章节 %s 消息总长超阈值 %.0f%%，已按优先级裁剪至 %d 字（阈值 %d）",
                section.get("id", "?"),
                CONTENT_CONTEXT_DEGRADE_RATIO * 100,
                _total_len(messages),
                threshold,
            )
        return messages
    except Exception as _dg_e:  # pragma: no cover - 防御性：降级失败不阻断生成
        logger.warning("[长上下文降级] 执行异常，保持原消息: %s", _dg_e)
        return messages


async def _do_generate_section_with_optimizations(
    provider: Any, section: dict, context: dict
) -> str:
    """带优化的章节正文生成（缓存+并发控制+续写）

    关键修复（PERF-CACHE-ISOLATE / PERF-CANCEL-PASS 2026-09-08）：
      1. 缓存 key 加 project_id 前缀，避免多项目并发时跨项目错误命中。
      2. provider.chat 透传 cancel_event / max_retries，客户端断开时
         正在飞的 AI 调用可被立即取消（否则必须等满 120-180s 超时）。
    """
    # 1) project_id 隔离的缓存 key
    project_id = context.get("project_id") or section.get("project_id")
    cache_key = f"section_content:{project_id}:{section.get('id', '')}"
    # BUG-FIX 2026-09-16（P1）：字数采样的「探针」章节必须绕过内容缓存。
    # 探针的 prompt 上下文是残缺的（无事实变量、无同级/上级章节、无承包范围），
    # 若把它的输出写进 `section_content:<pid>:<sid>`，紧接着的正式生成会直接命中
    # 这份残次品 —— 被抽中的 3 个章节永远拿不到带事实约束的正文，且前端看到
    # "瞬间完成"却不知为何内容偏薄。调用方传 use_cache=False 即退出缓存读写。
    use_cache = context.get("use_cache", True)
    if use_cache:
        cached = await _section_content_cache.get(cache_key)
        if cached:
            # PERF-CACHE-LOG 2026-09-08：缓存命中打 INFO 日志，便于排障时区分
            # "瞬间完成是因为缓存" vs "AI 慢响应"——避免误判为卡顿。
            logger.info(
                "[正文生成] 缓存命中 project=%s section=%s (%d 字)",
                project_id, section.get("id"), len(cached),
            )
            # ENHANCE 2026-10-05（P-23）：正文缓存命中率埋点
            record_store("section_content", True, size=len(cached))
            return cached
        # ENHANCE 2026-10-05（P-23）：未命中也要计数（分母）
        record_store("section_content", False)

    messages = build_chapter_content_messages(section, **context)

    # 2026-10-20 易标机制引入（规格三.7）：长上下文降级。
    # 默认 CONTENT_CONTEXT_DEGRADE_ENABLED=false 保持现有行为；开启后消息
    # 总长超 limit×ratio（易标同口径 70%）时裁剪参考资料段，避免请求撑爆
    # 上下文整条失败（本软件无 Agent 隔离运行时，裁剪为等效降级）。
    try:
        from ...config import CONTENT_CONTEXT_DEGRADE_ENABLED as _CC_DEGRADE

        if _CC_DEGRADE:
            messages = _degrade_long_context_messages(messages, section)
    except Exception as _cc_e:  # pragma: no cover - 防御性
        logger.debug("[silent-except] 长上下文降级开关读取失败（忽略）: %s", _cc_e)

    # ENHANCE 2026-10-18（第二轮）：上下文注入校验（生成前审计）。
    # 检查消息栈层级齐全 / 长度未超限 / 未被截断 / 顺序模式下前序摘要存在，
    # 结果挂到 section["_context_check"]（_gen_one 收进 result.context_check），
    # 供生成后汇总与 SSE verification 事件。纯本地计算，零 AI 调用。
    try:
        from ...config import CONTENT_CONTEXT_AUDIT as _CTX_AUDIT

        if _CTX_AUDIT:
            from .ordered_generation import audit_message_stack

            section["_context_check"] = audit_message_stack(
                messages,
                section,
                field_contract=context.get("field_contract"),
                require_previous=bool(context.get("previous_sections_summary")),
            )
    except Exception as _ctx_audit_e:  # pragma: no cover - 防御性
        logger.debug("[silent-except] 上下文注入校验失败（忽略）: %s", _ctx_audit_e)

    # 2) 透传 cancel_event + max_retries 到 provider.chat
    chat_kwargs: dict[str, Any] = {}
    cancel_event = context.get("cancel_event")
    if cancel_event is not None:
        chat_kwargs["cancel_event"] = cancel_event
    if "max_retries" in context and context["max_retries"] is not None:
        chat_kwargs["max_retries"] = context.get("max_retries")
    # 任务类型便于 ai_monitor 统计
    if context.get("task_type"):
        chat_kwargs["task_type"] = context.get("task_type")

    try:
        chat_kwargs["return_meta"] = True
        response = await provider.chat(messages, **chat_kwargs)

        # 兼容两种返回格式: dict (return_meta) 或 str (旧版)
        if isinstance(response, dict):
            content = response.get("content", "")
            finish_reason = response.get("finish_reason", "stop")
            # ENHANCE 2026-10-05（P-23）：采集 prompt cache token 用量；
            # 标书智能体（4）（2026-10-23）：带 task_type/section_id 记账，
            # 使命中率可按任务类型观测（缓存优化是否生效可量化）。
            record_prompt_cache(
                response.get("usage"),
                task_type=context.get("task_type") or "section_content",
                section_id=str(section.get("id", "")),
            )
        elif hasattr(response, 'content'):
            content = response.content
            finish_reason = getattr(response, "finish_reason", "stop")
            record_prompt_cache(
                getattr(response, "usage", None),
                task_type=context.get("task_type") or "section_content",
                section_id=str(section.get("id", "")),
            )
        else:
            content = str(response)
            finish_reason = "stop"

        # CONTENT_CONTINUE_PROMPT 自动续写
        if finish_reason == "length" and content and get_prompt is not None:
            content = await _auto_continue_on_length(
                provider, content, section, context, chat_kwargs
            )

        # Risk-P2a FIX 2026-10-11：最终出口的双保险兜底闭合。
        # 必须在 cache.set 之前做——否则缓存里存的仍是未闭合围栏版本，
        # 后续缓存命中会绕过所有安全网。续写内部已经做过一次，
        # 但主调用直接返回（没走续写）时也可能被 finish_reason=="stop"
        # 截断在围栏内部（AI 自己输出不完整）。
        content, _closed = _force_close_unclosed_chart_fence(content)

        if use_cache:
            await _section_content_cache.set(cache_key, content)
        return content
    except Exception as e:
        logger.error(f"章节生成失败 {section.get('title', '')}: {e}")
        raise


async def generate_section_content(
    provider: Any,
    section: dict,
    context: dict = None,
    progress_callback: Callable | None = None,
) -> str:
    """生成单个章节的正文内容

    progress_callback 签名（与 sse_handlers.progress_callback 对齐）：
        async def cb(section_id, title, status, progress, message, word_count=0)
    """
    context = context or {}
    # 2026-10-20 遗留 5（content_mode 消费点）：目录生成落库的 content_mode
    # 在正文生成入口统一解析。当前仅 ai-generate 有实现；模板填充/点对点
    # 模式若出现在历史/外部数据中，resolve_content_mode 会显式告警并回落，
    # 避免该字段“只透出不生效”。未来在此按模式分流到不同生成实现。
    #
    # 注意：必须在**调用方原 dict** 上原地归一化，不能 `section = dict(section)`
    # 拷贝——`_do_generate_section_with_optimizations` 会把 `_context_check` /
    # `_context_degraded` 挂在 section 上作为侧信道，`generate_sections` 的
    # `_gen_one` 从原对象读回；拷贝会让上下文校验/降级观测静默丢失。
    from .outline_alignment import resolve_content_mode

    section["content_mode"] = resolve_content_mode(section)
    section_id = section.get("id", "")
    section_title = section.get("title", "")
    try:
        content = await _do_generate_section_with_optimizations(provider, section, context)
        # F-P0-2（2026-10-07）：空正文显式失败，避免静默写入空文档（日志曾见 AI 响应长度 0）。
        if not content or not content.strip():
            raise EmptyAIResponseError("章节正文为空", f"section={section_id} title={section_title}")
        if progress_callback:
            try:
                # BUG-FIX 2026-09-16：原用 len(content)（字符数）上报，
                # 与落库的 word_count（count_words：中文按字、英文按词）口径不同，
                # 导致前端树节点字数与进度事件字数对不上。统一用 count_words。
                wc = count_words(content) if content else 0
                await progress_callback(
                    section_id, section_title, "completed", 100, "已生成", wc
                )
            except Exception as _e:
                logger.debug("[silent-except] generate_section_content progress_callback error - %s", _e)
        return content
    except Exception as e:
        logger.error("generate_section_content failed for %s: %s",
                     section.get("title", "?"), e)
        if progress_callback:
            try:
                await progress_callback(
                    section_id, section_title, "failed", 0, str(e)[:200]
                )
            except Exception as _e:
                logger.debug("[silent-except] generate_section_content error callback - %s", _e)
        raise


async def generate_sections(
    provider: Any,
    sections: list,
    context: dict = None,
    progress_callback: Callable | None = None,
    concurrency: int = 3,
    # generation_mode 默认 None → 回落 config.CONTENT_GENERATION_MODE（batch）。
    # 2026-10-18 第二轮统一：不再硬编码 "parallel"，保证「系统默认 = 后端
    # 默认 = 前端默认」同一事实源；显式传 "parallel" 仍是旧行为（向后兼容）。
    generation_mode: str | None = None,
    **kwargs: Any,
) -> list:
    """并发生成多个章节的正文

    Args:
        provider: AI 提供商
        sections: 章节字典列表（每个 dict 包含 id, title, level, path, ancestors 等）
        context: 上下文字典（summary, tender_context, scoring_context, project_scope, facts 等）
        progress_callback: 单章节进度回调 async(section_id, title, status, progress, message, word_count=0)
        concurrency: 并发度（由调用方钳制到 MAX_CONTENT_CONCURRENCY=3）
        **kwargs: 调用方透传的命名参数：
            tender_context: str    招标文件全文
            summary: str           项目摘要
            industry_profile: str  行业背景画像
            scoring_standards: str 评分标准 JSON（传给 scoring_context）
            writing_length: str    写作长度 short/standard/detailed/extra_long
            global_facts: list     全局事实变量（传给 facts）
            project_scope: str     承包范围
            project_id: int        项目 ID
            section_complete_callback: Callable  章节完成回调 async(result_dict)
            cancel_event: asyncio.Event          取消信号

    Returns:
        list[dict]: 每个章节的生成结果，包含 id/title/status/content/error/quality 等字段
    """
    # --- 合并上下文：支持两种调用风格 ---
    merged_context: dict = dict(context or {})
    kw_to_ctx = {
        "summary": "summary",
        "tender_context": "tender_context",
        "scoring_standards": "scoring_context",
        "project_scope": "project_scope",
        "global_facts": "facts",
        "industry_profile": "industry_profile",
        "writing_length": "writing_length",
        # ENHANCE 2026-10-05（P-9）：招标文件已提取的「工程作法」原文
        "construction_method": "construction_method",
        # ENHANCE 2026-10-07：字段契约（提取项目字段 + 全局数据字典）与字段调用追溯
        "field_contract": "field_contract",
        "field_trace": "field_trace",
        # ENHANCE 2026-10-07（第二轮）：行业名 → 章节归属随行业模板词表匹配
        "industry": "industry",
        # 易标 R5/R8（2026-10-12）：强控覆盖表（{section_id: {...}}），由
        # sse_content 按快照装配后透传；空表/缺省时下游回落既有档位逻辑。
        "word_control": "word_control",
        # ENHANCE 2026-10-14（facts-ns P1-3）：缺失值策略透传，供正文 system
        # 追加 omit/placeholder 缺值口径铁律（fabricate/缺省不追加，行为不变）。
        "facts_policy": "facts_policy",
        # 2026-10-18 ENHANCE: generation mode for ordered batch processing
        "generation_mode": "generation_mode",
        # 2026-10-18 ENHANCE（第二轮）：危大工程判定参数 / 适用规范清单。
        # 必须在此登记——漏登记会被静默丢弃（kwargs 不进 merged_context），
        # 表现为「扫描到了但正文永远注入不到」的幽灵功能。
        "hazard_info": "hazard_info",
        "standards_list": "standards_list",
        # 标书智能体（6）：是否启用「预编排驱动」的正文口径（正文与配图分离）。
        # 必须在此登记——漏登记会被静默丢弃，表现为「配图阶段开着、正文却还在
        # 输出图表围栏」。
        "illustration_phase_planned": "illustration_phase_planned",
    }
    # P0-TEXT-FLOW: 注入同级/上级章节映射（防车轱辘话、承上启下）
    # 从 sections_by_id + children_map 计算，每个 _gen_one 时按需取用
    _sid_map = kwargs.get("sections_by_id") or {}
    _cld_map = kwargs.get("children_map") or {}

    # siblings 映射: section_id -> [{id, title, description}]
    # 标书智能体（4）关键改进（缓存命中，冲突时以外部引用标准为准）：
    # 同级章节列表**包含当前章节**，且同一父章节下所有叶子共用**同一个列表
    # 对象**（按父章节统一构造+统一截取）→ 渲染出的 sibling_context 对所有
    # 叶子字节级一致。旧实现在此按叶子排除当前章节（`b.get("id") != _sid`），
    # 同父章节下每个叶子的同级列表都不同 → 服务商看到的请求前缀逐叶子漂移，
    #  prompt cache 只能部分命中。「AI 把自己当作参考对象」的风险改由任务
    # 差异消息里的显式指令约束（build_chapter_content_messages 的
    # 「请避免与除当前章节外的其他章节内容重复」），不再靠数据层过滤。
    _siblings_map: dict = {}
    _parents_map: dict = {}
    _sibling_list_by_parent: dict = {}
    try:
        from ...config import CONTENT_SIBLING_MAX_ITEMS as _SIBLING_CAP
    except Exception:  # pragma: no cover - 配置读取兜底
        _SIBLING_CAP = 8
    try:
        # 审计7增强（2026-10-27）：超长目录降描述保留标题的扩容上限
        from ...config import CONTENT_SIBLING_TITLES_MAX_ITEMS as _SIBLING_TITLES_CAP
    except Exception:  # pragma: no cover - 配置读取兜底
        _SIBLING_TITLES_CAP = 40
    for _s in sections:
        _sid = _s.get("id")
        if not _sid:
            continue
        _pid = _s.get("parent_id")
        # 找 siblings（同父节点的全部孩子，含当前章节；同父章节统一截取）
        if _pid and _pid in _cld_map:
            if _pid not in _sibling_list_by_parent:
                _children = [
                    {"id": b.get("id"), "title": b.get("title", ""), "description": b.get("description", "")}
                    for b in _cld_map[_pid]
                    if b.get("title")
                ]
                if len(_children) > max(1, _SIBLING_CAP):
                    # 超长目录（孩子数 > 容量）：旧口径只取前 8 个 → 第 9 个及
                    # 之后的章节不在自己的同级列表里（自定位/兄弟感知缺失）。
                    # 改为整表降描述仅保留标题、条数扩至 titles 上限：
                    # 标题很短，token 量与旧口径相当；仍按父章节共享同一列表，
                    # 前缀缓存命中设计不变（渲染端对空 description 安全）。
                    _children = [
                        {"id": c.get("id"), "title": c.get("title", ""), "description": ""}
                        for c in _children
                    ][: max(max(1, _SIBLING_CAP), _SIBLING_TITLES_CAP)]
                _sibling_list_by_parent[_pid] = _children
            _shared = _sibling_list_by_parent[_pid]
            # 审计8修复（BUG-A）：同级列表截断不变量。设计约定（L3037-3043）要求
            # 每个章节的同级列表**包含当前章节**（自定位/兄弟感知）。当父节点孩子数
            # 超过 titles 上限时，共享列表被 `[:cap]` 截断，第 cap+1 个及之后的章节
            # 不在共享列表里 → 违反不变量（第 41 个章节看不到自己，自定位缺失）。
            # 修复：仅对被截掉的章节单独补一条「自身 + 共享列表」的列表；共享列表
            # 本身不动，未截断章节仍共用同一对象（前缀缓存命中设计不变）。
            if not any(_sb.get("id") == _sid for _sb in _shared):
                _siblings_map[_sid] = [
                    {"id": _sid, "title": _s.get("title", ""), "description": ""},
                ] + _shared
            else:
                _siblings_map[_sid] = _shared
        # 找 parent 链（向上最多 3 层）
        _parents_list = []
        _cur_pid = _pid
        for _ in range(3):
            if not _cur_pid or _cur_pid not in _sid_map:
                break
            _p = _sid_map[_cur_pid]
            _parents_list.insert(0, {
                "id": _p.get("id"), "title": _p.get("title", ""),
                "description": _p.get("description", ""),
            })
            _cur_pid = _p.get("parent_id")
        if _parents_list:
            _parents_map[_sid] = _parents_list
    for kw_key, ctx_key in kw_to_ctx.items():
        if kw_key in kwargs and ctx_key not in merged_context:
            merged_context[ctx_key] = kwargs[kw_key]

    # --- 提取可选控制参数 ---
    section_complete_callback = kwargs.get("section_complete_callback")
    cancel_event: asyncio.Event | None = kwargs.get("cancel_event")

    # 关键修复（PERF-CANCEL-PASS 2026-09-08）：将 cancel_event / project_id 注入
    # merged_context，让 _do_generate_section_with_optimizations → provider.chat
    # 能拿到取消信号。否则客户端断开时，正在飞的 AI 调用必须等满 timeout 才会退出。
    if cancel_event is not None and "cancel_event" not in merged_context:
        merged_context["cancel_event"] = cancel_event
    if "project_id" in kwargs and kwargs["project_id"] is not None:
        merged_context.setdefault("project_id", kwargs["project_id"])
    # 透传任务类型，便于 ai_monitor 统计
    if "task_type" not in merged_context and kwargs.get("task_type"):
        merged_context["task_type"] = kwargs["task_type"]

    # --- 执行主体 ---
    semaphore = asyncio.Semaphore(max(1, min(concurrency, len(sections))))
    results: list[dict | None] = [None] * len(sections)

    # 2026-10-18 ENHANCE: previous section summaries for ordered context injection
    prev_summaries_map: dict[int, str] = {}

    # 2026-10-18 ENHANCE（第二轮）：实际生成顺序追踪器。
    # 调用方（SSE 主循环）可经 kwargs 传入共享实例，使主生成 + 重试的完成
    # 顺序进入同一序列，供生成后统一校验；未提供时本函数自建（仅覆盖本次）。
    _order_tracker = kwargs.get("order_tracker")
    if _order_tracker is None:
        try:
            from .ordered_generation import GenerationOrderTracker

            _order_tracker = GenerationOrderTracker()
        except Exception:  # pragma: no cover - 防御性
            _order_tracker = None

    async def _gen_one(idx: int, section: dict) -> None:
        """为单个章节生成正文并填充 results[idx]"""
        # 1) 取消检查（进入并发槽位后立即检查，避免已取消还发起 AI 调用）
        if cancel_event is not None and cancel_event.is_set():
            results[idx] = {
                "id": section.get("id"),
                "title": section.get("title", ""),
                "status": "cancelled",
                "content": "",
                "error": "已取消",
            }
            return

        async with semaphore:
            sid = section.get("id")
            title = section.get("title", "")
            # 2) 生成中事件（progress_callback 签名兼容）
            if progress_callback is not None:
                try:
                    await progress_callback(
                        sid, title, "generating", 0, "正在生成...",
                    )
                except Exception as _e:
                    logger.debug("[silent-except] _gen_one generating progress_callback - %s", _e)

            # PERF-PROGRESS-TICKER 2026-09-08：
            # 长章节 AI 生成 60-180s 期间，原实现没有任何中间进度推送，
            # 前端只看到"转圈"无任何反馈，体感为"卡了"。
            # 增加 ticker：每 12s 推一次 progress=50 中间事件，AI 响应快时
            # ticker 自然不触发，慢响应时让前端能看到"AI 正在生成 X 字"。
            ticker_task: asyncio.Task | None = None
            if progress_callback is not None:
                async def _ticker() -> None:
                    try:
                        # 等首次 12s（首次响应通常 < 12s 时 ticker 不发）
                        await asyncio.sleep(12)
                        while True:
                            try:
                                await progress_callback(
                                    sid, title, "generating", 50,
                                    "AI 正在生成中...",
                                )
                            except Exception:
                                return
                            await asyncio.sleep(12)
                    except asyncio.CancelledError:
                        return

                ticker_task = asyncio.create_task(_ticker(), name=f"ticker-{section.get('id')}")

            # 3) 调用底层生成
            content = ""
            error_msg: str | None = None
            # P0-TEXT-FLOW: 注入 sibling/parent 上下文到本章节的 context 副本
            _sid_now = section.get("id")
            _local_ctx = dict(merged_context)
            if _sid_now in _siblings_map:
                _local_ctx["sibling_chapters"] = _siblings_map[_sid_now]
            if _sid_now in _parents_map:
                _local_ctx["parent_chapters"] = _parents_map[_sid_now]
            # 2026-10-18 ENHANCE: 前序章节摘要（顺序生成时上下文承接，
            # 单一实现见 ordered_generation.inject_previous_section_context）
            if _sid_now in prev_summaries_map:
                _local_ctx["previous_sections_summary"] = prev_summaries_map[_sid_now]
            # 2026-10-18 ENHANCE（第二轮）：目录树位置（第几章第几节 / 第几个节点），
            # 供提示词明确顺序约束；不改变任何生成行为。
            if idx is not None:
                _local_ctx["tree_index"] = idx + 1
                _local_ctx["tree_total"] = len(sections)
            try:
                content = await generate_section_content(
                    provider, section, _local_ctx, None
                )
            except asyncio.CancelledError:
                if ticker_task is not None and not ticker_task.done():
                    ticker_task.cancel()
                if cancel_event is not None and cancel_event.is_set():
                    results[idx] = {
                        "id": sid,
                        "title": title,
                        "status": "cancelled",
                        "content": "",
                        "error": "已取消",
                    }
                    return
                raise
            except Exception as exc:
                error_msg = str(exc)
                logger.error(
                    "[正文生成] 章节生成失败 section=%s: %s",
                    title, exc,
                )
            finally:
                # 无论成功失败都停掉 ticker，避免泄漏
                if ticker_task is not None and not ticker_task.done():
                    ticker_task.cancel()
                    try:
                        await ticker_task
                    except (asyncio.CancelledError, Exception):
                        pass

            # 4) 组装结果 dict（与 sse_handlers section_complete_callback 的契约对齐）
            # 注：sid/title 已在 #2 处声明，此处复用即可
            final_status = "failed" if error_msg else "completed"
            # ENHANCE 2026-09-18（消除幽灵功能）：`quality` 此前恒为 None，而
            # routers/sse/sse_content.py 的 `quality_summary` 事件以
            # `r["quality"]["score"]` 为条件 → 该事件永不发出，「质量检查」在前端
            # 完全不可见。这里接入确定性本地打分（零 AI 调用、可复现），
            # 让质量分布真正可见；AI 版 evaluate_sections_quality 仍保留供离线深度评估。
            _quality = None
            if final_status == "completed" and content:
                try:
                    from .workflows_quality import score_section_quality_local

                    _quality = score_section_quality_local(
                        content, title, section.get("target_words")
                    )
                except Exception as _q_e:
                    logger.debug("[silent-except] 本地质量打分失败 - %s", _q_e)
            result: dict[str, Any] = {
                "id": sid,
                "title": title,
                "status": final_status,
                "content": content or "",
                "error": error_msg,
                "quality": _quality,
            }
            # 2026-10-18 ENHANCE（第二轮）：上下文注入校验结果（生成前审计，
            # 由 build_chapter_content_messages 挂到 section 上）。
            _ctx_check = section.get("_context_check")
            if _ctx_check is not None:
                result["context_check"] = _ctx_check
            # BUG-FIX 2026-10-20（灰度开关观测闭环）：长上下文降级留痕随结果
            # 上报。此前 `_degrade_long_context_messages` 只把
            # `section['_context_degraded']` 挂在 section 上就再无消费方——
            # 开关开启后运维只有一条 WARNING 日志，SSE/前端完全不可见，
            # 无法回答「哪些章节被裁剪、裁了多少」，而这正是把
            # CONTENT_CONTEXT_DEGRADE_ENABLED 从默认关推向默认开所必需的
            # 采样数据。现在随 result 上报，由 sse_content 汇总进
            # verification 事件（与 context_check 同一模式）。
            if section.get("_context_degraded"):
                result["context_degraded"] = True
            results[idx] = result

            # 2026-10-18 ENHANCE（第二轮）：记录实际完成顺序（含并发/重试），
            # 供生成后校验「实际生成顺序 vs 目录树顺序」。
            try:
                _order_tracker.record(sid, final_status)
            except Exception:  # pragma: no cover - 防御性
                pass

            # 5) 进度回调：生成完成
            # BUG-FIX 2026-09-16（P2 计数翻倍）：SSE 主链路上「章节终态」由
            # section_complete_callback 统一派发（它带 saved/word_count，且完成后才落库）。
            # 原实现在这里**再发一次**同 section_id 的 completed/failed 事件，导致：
            #   · 前端 failed 分支按事件逐条累加（不校验 saved）→ 失败数翻倍；
            #   · 出现两种 schema 的同名 completed 事件（带/不带 saved），
            #     前端只能靠 `d.saved` 过滤，逻辑脆弱。
            # 现在仅在「没有 section_complete_callback」的调用方（非 SSE 路径）兜底发送。
            if progress_callback is not None and section_complete_callback is None:
                try:
                    wc = count_words(content) if content else 0
                    await progress_callback(
                        sid, title, final_status, 100,
                        "已生成" if final_status == "completed" else f"失败: {error_msg}",
                        wc,
                    )
                except Exception as _e:
                    logger.debug("[silent-except] _gen_one completed progress_callback - %s", _e)

            # 6) 章节完成回调（由调用方负责 DB 持久化和终止事件派发）
            if section_complete_callback is not None:
                try:
                    await section_complete_callback(result)
                except Exception as cb_exc:
                    logger.error(
                        "[正文生成] section_complete_callback 异常 section=%s: %s",
                        title, cb_exc,
                    )

    # --- 并发调度 ---
    # 2026-10-18 ENHANCE: support three generation modes:
    #   parallel (default): asyncio.gather all at once (legacy behavior)
    #   batch: ordered batches, parallel within batch, sequential across batches
    #   serial: one section at a time, with previous section context injection
    #
    # 第二轮增强（顺序异常处理）：
    #   * 批内章节失败 → 按目录树顺序立即重试（不跳过后移），重试仍失败才
    #     标记待人工处理并继续后续批次，不阻塞整体流程；
    #   * 前序摘要统一走 ordered_generation.inject_previous_section_context
    #     （单一实现，消除内联漂移）。
    _gen_mode = str(merged_context.get("generation_mode") or "").strip().lower()
    if _gen_mode not in ("parallel", "batch", "serial"):
        # 未传/非法 → 回落系统默认（config.CONTENT_GENERATION_MODE，导入时已校验）
        try:
            from ...config import CONTENT_GENERATION_MODE as _default_mode

            _gen_mode = _default_mode
        except Exception:  # pragma: no cover - 防御性
            _gen_mode = "batch"
    _prev_summaries: list[dict] = []
    gathered: list = []

    # id → 原始下标（O(1) 定位，替代 sections.index 的 O(n²) 值比较）
    _idx_by_id = {s.get("id"): i for i, s in enumerate(sections)}

    # 批内失败按顺序重试轮数（config 可配，0 = 关闭回落旧行为）
    _batch_retry_rounds = 0
    try:
        from ...config import CONTENT_BATCH_RETRY_ROUNDS as _brr

        _batch_retry_rounds = max(0, int(_brr))
    except Exception:  # pragma: no cover - 防御性
        _batch_retry_rounds = 0

    async def _run_batch_sections(batch_sections: list[dict]) -> None:
        """并发执行一批章节（保持目录树顺序创建任务）。"""
        _batch_tasks = []
        for _sec in batch_sections:
            _orig_idx = _idx_by_id.get(_sec.get("id"))
            if _orig_idx is None:  # 防御性：批次外的脏数据
                continue
            _batch_tasks.append(asyncio.create_task(
                _gen_one(_orig_idx, _sec),
                name=f"section-gen-{_sec.get('id')}-{_orig_idx}",
            ))
        if _batch_tasks:
            _batch_gathered = await asyncio.gather(*_batch_tasks, return_exceptions=True)
            gathered.extend(_batch_gathered)

    if _gen_mode in ("batch", "serial") and build_generation_batches is not None:
        # Ordered generation mode
        _batch_size = 1 if _gen_mode == "serial" else max(1, min(concurrency, 8))
        _batches = build_generation_batches(sections, max_batch_size=_batch_size)

        for _batch_idx, _batch in enumerate(_batches):
            # Inject previous section summaries into context for this batch
            # （单一实现：ordered_generation.inject_previous_section_context）
            if _prev_summaries:
                for _b_sec in _batch:
                    _b_sid = _b_sec.get("id")
                    _ctx_probe = inject_previous_section_context(
                        {}, list(_prev_summaries)
                    )
                    if "previous_sections_summary" in _ctx_probe:
                        prev_summaries_map[_b_sid] = _ctx_probe["previous_sections_summary"]

            await _run_batch_sections(_batch)

            # 顺序异常处理：批内失败章节按目录树顺序立即重试，不跳过后移。
            # 重试仍失败 → 保留 failed 状态（标记待人工处理），继续后续批次。
            if _batch_retry_rounds > 0:
                for _round in range(_batch_retry_rounds):
                    _failed_in_batch = [
                        s for s in _batch
                        if isinstance(results[_idx_by_id[s["id"]]], dict)
                        and results[_idx_by_id[s["id"]]].get("status") == "failed"
                    ]
                    if not _failed_in_batch or (
                        cancel_event is not None and cancel_event.is_set()
                    ):
                        break
                    logger.info(
                        "[正文生成] 批 %d/%d 内 %d 章失败，按顺序重试 Round %d/%d",
                        _batch_idx + 1, len(_batches), len(_failed_in_batch),
                        _round + 1, _batch_retry_rounds,
                    )
                    await asyncio.sleep(1.0 * (2 ** _round))
                    await _run_batch_sections(_failed_in_batch)

            # Extract summaries from completed sections in this batch
            if extract_section_summary is not None:
                for _batch_sec in _batch:
                    _bsid = _batch_sec.get("id")
                    _r = results[_idx_by_id[_bsid]] if _bsid in _idx_by_id else None
                    if isinstance(_r, dict) and _r.get("status") == "completed" and _r.get("content"):
                        _prev_summaries.append({
                            "title": _batch_sec.get("title", ""),
                            "summary": extract_section_summary(_r["content"]),
                        })
                    prev_summaries_map.pop(_bsid, None)
    else:
        # Legacy parallel mode: all tasks start at once
        tasks = [
            asyncio.create_task(
                _gen_one(i, s),
                name=f"section-gen-{s.get('id')}-{i}",
            )
            for i, s in enumerate(sections)
        ]
        gathered = await asyncio.gather(*tasks, return_exceptions=True)

    # --- 生成后自检（ENHANCE 2026-10-07）---
    # 有字段契约时，对每个成功章节做「占位标记 / 与全局数据字典冲突 / 过短」自检，
    # 报告附加到 result["self_check"]；错误级问题打 INFO 日志供审计。
    if merged_context.get("field_contract"):
        try:
            from ...services.content_field_contract import run_content_self_checks

            _reports = run_content_self_checks(
                [r for r in results if r is not None],
                merged_context["field_contract"].get("global_dictionary"),
            )
            _bad = [rep for rep in _reports if not rep.get("ok")]
            if _bad:
                logger.info(
                    "[正文生成] 生成后自检: %d/%d 章存在待处理问题（详情见 result.self_check）",
                    len(_bad), len(_reports),
                )
        except Exception as _sc_e:
            logger.warning("[正文生成] 生成后自检执行失败（忽略）: %s", _sc_e)

    # --- 2026-10-18 ENHANCE: 生成后综合校验（顺序/完整性/一致性/事实调用）---
    try:
        from ...config import CONTENT_ORDER_VERIFY
        if CONTENT_ORDER_VERIFY:
            from .ordered_generation import verify_generation_completeness
            _expected_ids = [s.get("id") for s in sections if s.get("id") is not None]
            _verify_result = verify_generation_completeness(
                [r for r in results if r is not None],
                _expected_ids,
                field_contract=merged_context.get("field_contract"),
                generation_mode=_gen_mode,
                field_trace=merged_context.get("field_trace"),
                facts=merged_context.get("facts"),
                order_tracker=_order_tracker,
                industry=kwargs.get("industry"),
            )
            # 实际完成顺序随结果透传（SSE 复核试后的最终顺序用）
            _clean_results = [r for r in results if r is not None]
            if _clean_results:
                _clean_results[0]["_verification"] = _verify_result
                if _order_tracker is not None:
                    _clean_results[0]["_completion_order"] = list(
                        getattr(_order_tracker, "sequence", []) or []
                    )
            if not _verify_result.get("ok"):
                logger.info(
                    "[正文生成] 生成后综合校验: %d 个错误, %d 个警告",
                    len(_verify_result.get("issues", [])),
                    len(_verify_result.get("warnings", [])),
                )
    except Exception as _verify_e:
        logger.warning("[正文生成] 生成后综合校验执行失败（忽略）: %s", _verify_e)

    # 最终返回干净的 list[dict]（过滤掉 None 异常占位）
    return [r for r in results if r is not None]
