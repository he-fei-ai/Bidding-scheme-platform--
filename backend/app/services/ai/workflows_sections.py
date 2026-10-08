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

try:
    from app.services.utils import count_words
except ImportError:
    try:
        from ..utils import count_words
    except ImportError:
        def count_words(text: str) -> int:
            """简易字数统计（兜底）"""
            return len(text.replace(" ", "").replace("\n", ""))
from ...utils.scoring_points import is_scoring_placeholder
from .cache_metrics import record_prompt_cache, record_store
from .heading_standard import HeadingNumberingGenerator
from .writing_length_config import (
    WRITING_LENGTH_RANGE,
    WRITING_LENGTH_LABEL,
    normalize_writing_length,
    get_l3_own_tolerance,
    get_l3_own_target,
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


logger = logging.getLogger(__name__)


def _resolve_section_system_prompt() -> str:
    """解析章节正文 system 提示词（唯一正本：prompts/content.py）。

    FIX 2026-10-08（遗留9）：此处原先存在一份「兜底提示词」，其配图口径是
    `<<CHART:类型>>` 文本标记（并要求「不要输出 Mermaid 代码块」），与注册正本
    `CONTENT_SECTION_SYSTEM` 的 **mermaid/chart-json 围栏**口径完全相反。
    一旦提示词注册表不可用（模块导入失败/提示词未注册），生成会静默切到错误口径，
    AI 输出的配图形态与 `content_cleaning` 的解析口径错配 → 图表被判坏块删除。

    与其静默降级到矛盾文案，不如明确失败：注册正本缺失属于严重配置缺陷，
    必须被听见。
    """
    if get_prompt is not None:
        try:
            _prompt = get_prompt("CONTENT_SECTION_SYSTEM")
        except Exception as _e:  # 注册表损坏：与缺失同等对待
            logger.error("[提示词] CONTENT_SECTION_SYSTEM 读取失败: %s", _e)
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

        FIX 2026-10-08（遗留3）：把 `_validate_chart_data` 真正接进落库前的判定链，
        终结「两套校验都零调用」的状态。畸形数据不再伪装成真实图表堵住配图编排。
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

    FIX 2026-10-08（遗留3）：历史上存在两套互不一致的校验实现（本函数与
    `services/chart_validators.py`），且两套都**零调用**，形同虚设——毫无校验地
    把 AI 输出落库，正是「空壳/畸形数据伪装成真实图表」类缺陷的土壤。

    现统一口径：**`chart_validators` 为唯一权威**，本函数退化为薄适配层：
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


def _generate_fallback_chart(chart_type: str, section_title: str = "", 
                             summary: str = "", nodes: list = None, date: str = "") -> dict:
    """DEPRECATED 2026-10-08（遗留3）：**不得接入任何主链路**，保留仅为兼容引用。

    这是历史上的「凭空构造图表数据」实现，与当前的无降级原则直接冲突
    （`resolve_chart_marker` 文档明确：宁缺毋假，真实数据一律来自配图编排管线
    写入的 `sections.*_json`）。全仓零调用方；新代码请走
    `extract_section_charts` → 配图编排（hint 条目）链路。
    """
    if chart_type == "flowchart":
        return {
            "type": "flowchart",
            "nodes": nodes or [
                {"id": "N1", "label": section_title or "步骤1"},
                {"id": "N2", "label": "步骤2"},
                {"id": "N3", "label": "完成"},
            ],
            "edges": [[0, 1], [1, 2]],
        }
    elif chart_type == "gantt":
        return {
            "type": "gantt",
            "date_format": "YYYY-MM-DD",
            "duration": [
                {"id": "T1", "name": section_title or "阶段1", "start": date or "2025-01-01", "duration": 10},
                {"id": "T2", "name": "阶段2", "start": date or "2025-01-11", "duration": 15},
            ],
            "tasks": [],
        }
    return {"type": chart_type, "nodes": nodes or []}


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
    sys_prompt = _resolve_section_system_prompt()
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
    # ENHANCE 2026-10-07：全局数据字典（关键一致性字段权威值，全文唯一）→ 注入稳定上下文。
    # 放在 [1] 使缓存命中区间 [0]+[1] 不受影响；正文必须「一字不差引用」字典值，
    # 这是「前后数据一致性」的核心机制：同一字段在所有章节读取同一权威值。
    _field_contract = kwargs.get("field_contract")
    if _field_contract and format_global_dictionary_for_prompt is not None:
        _dict_text = format_global_dictionary_for_prompt(_field_contract.get("global_dictionary") or {})
        if _dict_text:
            stable_parts.append(_dict_text)
    if stable_parts:
        messages.append(AIMessage(role="user", content="\n\n".join(stable_parts)))

    # --- [2] 事实变量（按章节相关性排序，故与 [1] 分开：
    # 否则每章不同的事实前缀会打断 [1] 的缓存命中）---
    if facts:
        # ENHANCE 2026-10-05（P-5）：按章节标题筛选最相关事实，替换「全量注入 +
        # 2000 字符截断」。旧实现在事实条目多（>100 条）时，早期章节占满预算、
        # 尾部章节拿不到；且与易标规格三.4「按章节标题选择最相关事实并注入」
        # 不符。此处保留 2000 字符截断作为安全上限。
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
        # 可追溯：记录本章实际注入的字段（键 / 来源），供生成后自检与审计。
        if isinstance(_field_trace, list):
            _ext_keys = [k for k, v in _chapter_fields.get("extraction", {}).items() if v]
            _fac_keys = [k for k, v in _chapter_fields.get("facts", {}).items() if v]
            for _k in _ext_keys:
                _field_trace.append({"chapter": title, "field": _k, "source": "extraction"})
            for _k in _fac_keys:
                _field_trace.append({"chapter": title, "field": _k, "source": "facts"})

    # --- [2] 上级章节（承接关系）---
    if parent_chapters:
        _p_lines = []
        for _p in parent_chapters:
            _pt = _p.get("title", "")
            _pd = (_p.get("description") or "")[:80]
            _p_lines.append(f"  - {_pt}" + (f"（{_pd}）" if _pd else ""))
        if _p_lines:
            messages.append(AIMessage(
                role="user", content="上级章节（承接关系，请与之呼应而非重复）:\n" + "\n".join(_p_lines)
            ))

    # --- [3] 同级章节（避免重复）---
    if sibling_chapters:
        _s_lines = []
        for _s in sibling_chapters:
            _st = _s.get("title", "")
            _sd = (_s.get("description") or "")[:60]
            _s_lines.append(f"  - {_st}" + (f"（{_sd}）" if _sd else ""))
        if _s_lines:
            messages.append(AIMessage(
                role="user", content="同级章节（请避免内容重复，突出本章节独特性）:\n" + "\n".join(_s_lines)
            ))

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
    if _chapter_level == 3 and chapter_info.get("is_l3_own"):
        # L3 叶子：自身正文 300 \u00b1 20%
        _tier_key = None
        _tier_label = "L3 自身正文"
        _tier_target = get_l3_own_target()
        _tolerance = float(get_l3_own_tolerance())
    elif _chapter_level >= 3:
        # L3 非叶子：四档子树总字数（含下级目录章节）；
        # L4+ 深层叶子跟随同一档位，测量口径为自身正文。
        _tier_key = chapter_info.get("word_count_tier") or _mode
        _tier_cfg = get_tier_config(_tier_key) or get_default_tier_config()
        _tier_label = _tier_cfg.get("label", _mode)
        _tier_target = int(_tier_cfg.get("target_words", 1200))
        _tolerance = float(_tier_cfg.get("tolerance_ratio", 0.10))
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
    target_wc = f"{lo}-{hi}"
    target_note = (
        f"（{_tier_label}档位·目标 {_tier_target} 字，"
        f"允许浮动 \u00b1{int(_tolerance * 100)}%）"
    )

    # 字数控制核心指令
    if _chapter_level >= 3:
        prompt_parts.append(
            f"请撰写本章节完整内容，目标 {target_wc} 字{target_note}。"
            f"要求：① 结构清晰有小标题；② 数据详实有具体参数；③ 专业术语准确；"
            f"④ 紧扣评分点。"
        )
        # 字数控制红线（防止堆砌与省略）
        if _chapter_level == 3 and chapter_info.get("is_l3_own"):
            # L3 自身正文：300 \u00b1 20%，测量口径=自身正文，不含下级目录章节
            prompt_parts.append(
                "【字数控制红线】"
                f"本章节为三级目录（L3），目标字数 {_tier_target} 字"
                f"（允许浮动 \u00b1{int(_tolerance * 100)}%，{lo}~{hi} 字）。"
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
    _ill_type = chapter_info.get("illustration_type")

    if _plan:
        _tbl = _plan.get("table") or {}
        _mmd = _plan.get("mermaid") or {}
        _img = _plan.get("image") or {}
        orch_lines = []

        if _tbl.get("needed"):
            _tpurpose = _tbl.get("purpose", "")[:80]
            orch_lines.append(f"表格: 需要（{_tpurpose}）" if _tpurpose else "表格: 需要")

        if _ill_type == "mermaid" or (_mmd.get("needed") and not _img.get("needed")):
            _mreason = _mmd.get("reason", "")[:60]
            orch_lines.append(f"Mermaid图: 计划配图（{_mreason}）" if _mreason else "Mermaid图: 计划配图")

        if _ill_type == "ai" or _img.get("needed"):
            _ipriority = _img.get("priority", 0)
            _ipriority = _normalize_priority_for_prompt(_ipriority)
            _ptag = f"（优先级 {_ipriority}）" if _ipriority else ""
            orch_lines.append(f"AI配图: 计划配图{_ptag}")

        if orch_lines:
            prompt_parts.append("正文编排决策:\n" + "\n".join(orch_lines))

    messages.append(AIMessage(role="user", content="\n\n".join(prompt_parts)))

    # PERF-P0-B2: 记录 prompt 实际大小（日志级别 DEBUG，生产不增加开销）
    logger.debug(
        "Prompt size: title=%s, messages=%d, total_user_chars=%d",
        title, len(messages),
        sum(len(m.content) for m in messages if m.role == "user"),
    )
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
                               section_per_segment: int = 10) -> list:
    """编排正文生成任务（标书智能体六完整实现）

    主实现见 workflows_orchestration.orchestrate_content()。
    此处为兼容旧调用签名的包装层。

    ENHANCE 2026-10-05（P-11）：新增 max_ai_images / table_ratio /
    prefer_mermaid / section_per_segment 透传，见 workflows_orchestration 同名参数。
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
    )


# BUG-FIX 2026-09-16：此处原有一个 `async def _extract_charts_from_content`，
# 与上面（容错 JSON 解析版）的同步同名函数重复定义 —— 模块级后定义者生效，
# 于是这个「只认严格 JSON」的协程版本把容错版本整体遮蔽：任何按同步方式调用
# `_extract_charts_from_content(content)` 的代码拿到的都是 coroutine 对象
# （既不会抛错也不会返回图表，属最难排查的一类静默失效）。
# 全仓检索确认该名字已无调用方，故直接删除重复定义，保留上面那一个实现。


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


def _strip_seam_overlap(final_content: str, extra: str) -> str:
    """剥离 extra 前缀中与 final_content 尾部重叠的重复段（接缝去重）。

    返回去掉重叠前缀后的 extra；无重叠则原样返回。
    """
    if not final_content or not extra:
        return extra
    tail = final_content[-_MAX_SEAM_OVERLAP:]
    max_k = min(len(tail), len(extra))
    # 由最长向最短探测：找到 extra 前缀 == tail 后缀 的最大重叠长度
    for k in range(max_k, 0, -1):
        if extra[:k] == tail[-k:]:
            return extra[k:]
    return extra


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
    section_title = section.get("title", "")
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

        final_content = final_content.rstrip() + extra
        _logger.info(
            "[续写] 第 %d 轮完成，新增 %d 字，总 %d 字 (finish_reason=%s)",
            round_num, len(extra), len(final_content), next_finish,
        )

        if next_finish != "length":
            break

    return final_content



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
            # ENHANCE 2026-10-05（P-23）：采集 Anthropic prompt cache token 用量
            record_prompt_cache(response.get("usage"))
        elif hasattr(response, 'content'):
            content = response.content
            finish_reason = getattr(response, "finish_reason", "stop")
            record_prompt_cache(getattr(response, "usage", None))
        else:
            content = str(response)
            finish_reason = "stop"

        # CONTENT_CONTINUE_PROMPT 自动续写
        if finish_reason == "length" and content and get_prompt is not None:
            content = await _auto_continue_on_length(
                provider, content, section, context, chat_kwargs
            )

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
    }
    # P0-TEXT-FLOW: 注入同级/上级章节映射（防车轱辘话、承上启下）
    # 从 sections_by_id + children_map 计算，每个 _gen_one 时按需取用
    _sid_map = kwargs.get("sections_by_id") or {}
    _cld_map = kwargs.get("children_map") or {}

    # siblings 映射: section_id -> [{id, title, description}]
    _siblings_map: dict = {}
    _parents_map: dict = {}
    for _s in sections:
        _sid = _s.get("id")
        if not _sid:
            continue
        _pid = _s.get("parent_id")
        # 找 siblings（同父节点的其他孩子）
        if _pid and _pid in _cld_map:
            _brothers = [
                {"id": b.get("id"), "title": b.get("title", ""), "description": b.get("description", "")}
                for b in _cld_map[_pid]
                if b.get("id") != _sid and b.get("title")
            ]
            _siblings_map[_sid] = _brothers[:8]  # 最多 8 个防 token 爆
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
            results[idx] = result

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

    # --- 并发调度（gather + return_exceptions，让异常章节不拖累整体）---
    tasks = [
        asyncio.create_task(
            _gen_one(i, s),
            name=f"section-gen-{s.get('id')}-{i}",
        )
        for i, s in enumerate(sections)
    ]
    gathered = await asyncio.gather(*tasks, return_exceptions=True)

    # 任何意外异常的兜底处理
    for i, item in enumerate(gathered):
        if isinstance(item, BaseException):
            logger.error("[正文生成] _gen_one 未捕获异常 idx=%d: %s", i, item)
            section = sections[i]
            results[i] = {
                "id": section.get("id"),
                "title": section.get("title", ""),
                "status": "failed",
                "content": "",
                "error": f"内部错误: {item}",
            }

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

    # 最终返回干净的 list[dict]（过滤掉 None 异常占位）
    return [r for r in results if r is not None]
