"""图表自动编排闭环服务（2026-10-17，需求 1/3/4 端到端接线）

把 ``chart_orchestrator`` 的「分析 → 载荷生成 → 插入位置建议」接成端到端闭环：

    分析(analyze_chart_needs) → 生成载荷(generate_chart_payload)
      → 统一闸门校验修复(validate_chart_for_ai_pipeline，内含 repair_mermaid)
      → 围栏代码块构建（与正文 Markdown 完全一致的 ```mermaid / ```chart-json）
      → 最佳位置插入正文（suggest_insertion_position）
      → 落库（update_section）

设计原则：
  * 图表全部自动生成，不设人工配图/生图入口（人工逐类生成端点保留但不再是主链路）。
  * 正文已有图表围栏时**跳过插入**（去重防重入）；已有**同类型**内联标记时
    围栏同样不插、载荷降级落库 chart 字段（防同章双图/图号虚跳）。
  * 校验失败不写坏块：先走 repair_mermaid 修复，仍失败则整体跳过（容错降级，
    不占图号——导出侧对不存在的围栏天然无感）。
  * 正文为空无法插入时，降级把载荷持久化到章节 chart 字段（*_json），
    导出侧按 chart 字段路径照常渲染。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from ..models import SectionUpdate
from .ai.chart_orchestrator import (
    analyze_chart_needs,
    content_has_extractable_data,
    generate_chart_payload,
    suggest_insertion_position,
)
from .sections import get_section, update_section

logger = logging.getLogger(__name__)

#: 图表类型 → 章节 chart 字段（与 routers/sse/sse_utils._resolve_chart_field 同口径；
#: 服务层不能反向 import 路由层，这里维护唯一服务层正本，路由侧后续收敛到此）
CHART_FIELD_MAP: dict[str, str] = {
    "flowchart": "flowchart_json",
    "gantt": "gantt_json",
    "architecture": "architecture_json",
    "labor": "labor_json",
    "comparison": "comparison_json",
    "layout": "layout_json",
    "timeline": "timeline_json",
}

#: 图表类型通用名（图题第 4 优先级兜底，与 _CHART_TYPE_MAP 中文名一致）
_CHART_TYPE_LABELS: dict[str, str] = {
    "flowchart": "流程图",
    "gantt": "甘特图",
    "architecture": "架构图",
    "labor": "劳动力配置图",
    "comparison": "对比图",
    "layout": "平面布置图",
    "timeline": "里程碑时间线",
}

# Mermaid 代码型图表集合（围栏语言判定：mermaid vs chart-json）——已废弃：
# 围栏语言最终按**载荷形态**（是否含 code 字段）判定，见 build_chart_fence。

#: 本地模板数据披露括注（A3 建议 1·显式披露）。挂在 source="local" 载荷的
#: Mermaid 引导语上，前端打「模板数据」标签 + DOCX 正文双重可见；
#: 导出图题链路 _clean_lead_in_title 会剥离该括注，保证图题干净。
_LOCAL_TEMPLATE_DISCLAIMER = "（模板示例数据，请按项目实际核定）"


# ============================================================
# 纯函数：围栏构建 / 去重检测 / 位置插入（可独立单元测试）
# ============================================================


def build_chart_fence(chart_type: str, payload: dict[str, Any]) -> tuple[str, str]:
    """把图表载荷转换为与正文 Markdown 完全一致的围栏代码块。

    围栏语言按**载荷形态**判定（与 SSE 生成侧口径一致）：
      - 载荷带非空 ``code`` 字段 → Mermaid 源码 → `` ```mermaid `` 围栏；
      - 其余（tree/phases/rows/zones/milestones 等结构化数据）→
        `` ```chart-json `` 围栏。
    同一 chart_type 两种形态都可能出现（本地模板产 JSON、AI 产 Mermaid），
    按类型硬判会在混源时写错围栏语言。

    Returns:
        (lead_in, fence)：
        - lead_in: 引导语段落（图题第 3 优先级来源）。mermaid 围栏载荷本身
          不携带 title，靠引导语把图名带进导出图题链路；chart-json 载荷
          内嵌 title 字段（图题第 1 优先级），引导语返回空串避免冗余。
        - fence: ```` ```mermaid ```` / ```` ```chart-json ```` 围栏代码块文本。
    """
    title = str(payload.get("title") or "").strip()
    code = str(payload.get("code") or "").strip()
    if code:
        fence = "```mermaid\n" + code + "\n```"
        base = title or _CHART_TYPE_LABELS.get((chart_type or "").lower(), "图表")
        # A3 建议 1（显式披露）：本地模板载荷的 Mermaid 图无内嵌 title，
        # 披露括注挂引导语（DOCX 正文可见）；图题链路经 _clean_lead_in_title
        # 剥离括注，导出图题保持干净。AI 载荷与 chart-json 型不加括注——
        # chart-json 的披露责任在前端「模板数据」标签（source 键随 JSON
        # 序列化进围栏，前端可读），塞进 title 会污染导出图题。
        if str(payload.get("source") or "") == "local":
            base += _LOCAL_TEMPLATE_DISCLAIMER
        lead_in = f"下图为{base}："
        return lead_in, fence
    # 结构化 JSON 载荷：title 内嵌于载荷（图题第 1 优先级）
    fence = "```chart-json\n" + json.dumps(payload, ensure_ascii=False) + "\n```"
    return "", fence


def content_has_chart_fence(content: str) -> bool:
    """正文是否已包含图表围栏（去重防重入）。

    与 content_cleaning.CHART_FENCE_OPEN_PATTERN 同口径（```
    / ~~~ + mermaid / chart[-_]?json 变体），只判存在性，不解析。
    """
    if not content:
        return False
    from .content_cleaning import CHART_FENCE_OPEN_PATTERN

    for line in content.split("\n"):
        if CHART_FENCE_OPEN_PATTERN.match(line.strip()):
            return True
    return False


#: 单章图表围栏数量上限（与 prompts/content.py「每章最多 2 个图表且类型不重复」
#: 的写作契约对齐；ENHANCE 2026-11-04 前编排侧是「任一围栏即全拒」，与该契约矛盾）
MAX_CHART_FENCES_PER_SECTION = 2


def content_chart_fence_types(content: str) -> list[str]:
    """正文已有图表围栏的类型列表（按出现顺序；类型不可识别时计作 "unknown"）。

    ENHANCE 2026-11-04（需求 3/4·编排增强）：去重守卫从「任一围栏即全拒」
    放宽为「同类型去重 + 每章 ≤2 图」——正文已有甘特图围栏时，仍可为该章
    补插流程图（图文互补），与 CONTENT_SECTION_SYSTEM 的写作契约一致。
    类型判定复用 content_cleaning.iter_chart_fences（与 parse_content 同源
    状态机）：chart-json 围栏读载荷 type，mermaid 围栏按首行关键字推断，
    与 _fence_payload_to_marker_text 的推断口径一致。
    """
    if not content:
        return []
    from .content_cleaning import iter_chart_fences

    types: list[str] = []
    for _lang, _body, marker_text in iter_chart_fences(content):
        ct = ""
        if marker_text:
            try:
                mt = json.loads(marker_text)
                ct = str(mt.get("chart_type") or "").strip().lower()
            except (json.JSONDecodeError, TypeError, ValueError):
                ct = ""
        types.append(ct or "unknown")
    return types


def chart_fence_type_conflict(content: str, chart_type: str) -> bool:
    """编排去重守卫（ENHANCE 2026-11-04）：正文已有该类型围栏，或围栏数已达上限。

    替代旧的 content_has_chart_fence 全拒语义：
      - 同类型围栏已存在 → 拒（防同章双同类图/图号冲突，F2 语义延续）；
      - 不同类型但已有 2 个围栏 → 拒（每章 ≤2 图上限）；
      - 不同类型且 <2 个 → 放行（补插第二类图）。
    """
    if not content:
        return False
    fence_types = content_chart_fence_types(content)
    ct = (chart_type or "").strip().lower()
    if ct and ct in fence_types:
        return True
    return len(fence_types) >= MAX_CHART_FENCES_PER_SECTION


def content_has_chart_marker_type(content: str, chart_type: str) -> bool:
    """正文是否已包含**指定类型**的 ``<<CHART:...>>`` 内联标记（不查围栏）。

    F2 修复 2026-11-03（需求 11·同章双图 / 编号冲突）：导出侧 parse_content
    把 ``<<CHART:gantt>>`` 标记与 ```` ```mermaid ```` 围栏**都**转为
    chart_marker 块、各自调用一次 _add_inline_chart 渲染。编排侧旧去重只查
    围栏（content_has_chart_fence），正文已有该类型标记时仍插入围栏 →
    标记+围栏并存 → 同章渲染两张同类图、图号多占一位。
    注意必须按**类型**判定：不同类型的标记与围栏并存是合理形态
    （甘特图围栏 + 流程图标记 = 两张不同的图），不得互相阻止。
    """
    if not content or not chart_type:
        return False
    from .content_cleaning import iter_chart_markers, resolve_chart_marker

    ct = (chart_type or "").strip().lower()
    if not ct or ct == "none":
        return False
    for _marker, prefix, desc in iter_chart_markers(content):
        resolved = resolve_chart_marker(prefix, desc)
        if resolved and resolved[0] == ct:
            return True
    return False


def _compute_code_fence_regions(lines: list[str]) -> list[tuple[int, int]]:
    """计算正文中所有代码围栏（``` 或 ~~~，含图表围栏与普通代码块）的行区间。

    C7 修复（2026-11-04）的共享工具：与 content_cleaning.plan_chart_fence_lines
    的宽松 toggle 口径一致（同字符开闭、忽略 info-string），只用于**移动插入
    位置**，绝不做删除/改写，误判的最坏后果是插入点平移到围栏外。
    """
    regions: list[tuple[int, int]] = []
    _start: int | None = None
    _ch = ""
    for i, ln in enumerate(lines):
        _s = ln.lstrip()
        if _s.startswith("```") or _s.startswith("~~~"):
            _c = _s[0] * 3
            if _start is None:
                _start, _ch = i, _c
            elif _c == _ch:
                regions.append((_start, i))
                _start, _ch = None, ""
    if _start is not None:
        # 未闭合围栏：视为延伸到文末（插入点避让其内部）
        regions.append((_start, len(lines) - 1))
    return regions


def _clamp_line_no_outside_fences(lines: list[str], line_no: int) -> int:
    """把目标行号钳制到代码围栏之外；找不到围栏外非空行时退到文末追加。"""
    _regions = _compute_code_fence_regions(lines)
    if not _regions:
        return line_no

    def _inside(_i: int) -> bool:
        return any(_a <= _i <= _b for _a, _b in _regions)

    if not _inside(line_no):
        return line_no
    # 向后找区域外的非空行（保持「插在相关段后」的语义）
    for _j in range(line_no + 1, len(lines)):
        if lines[_j].strip() and not _inside(_j):
            return _j
    # 向前找区域外的非空行
    for _j in range(line_no - 1, -1, -1):
        if lines[_j].strip() and not _inside(_j):
            return _j
    # 整篇都在围栏内：退到文末（append 语义，最不具破坏性）
    return len(lines) - 1


def insert_fence_at_paragraph(
    content: str,
    fence: str,
    lead_in: str,
    paragraph_index: int,
) -> str:
    """把围栏块插入到正文的第 ``paragraph_index`` 个非空段之后。

    位置来源为 suggest_insertion_position（关键词打分），保证「图文逻辑连贯」：
    流程图插在工艺流程段后、甘特图插在进度计划段后。空正文时直接以
    「引导语 + 围栏」作为正文主体。

    BUG-FIX 2026-11-04（P3·C7）：suggest_insertion_position 只按关键词打分、
    不感知普通 ``` 代码围栏 —— 正文含配置示例/脚本代码块且其中恰有
    「流程/步骤/如下图」等关键词行时，图表围栏被插进普通围栏内部中段，
    iter_chart_fences/parse_content 把整段当坏块处理，导出删坏块后正文缺段。
    现插入前行号强制钳制到所有代码围栏之外。
    """
    lines = content.split("\n")
    block = (lead_in + "\n\n" + fence) if lead_in else fence
    block_lines = block.split("\n")
    non_empty = [k for k, ln in enumerate(lines) if ln.strip()]
    if not non_empty:
        return block
    idx = max(0, min(paragraph_index, len(non_empty) - 1))
    line_no = _clamp_line_no_outside_fences(lines, non_empty[idx])
    new_lines = lines[: line_no + 1] + ["", *block_lines] + lines[line_no + 1 :]
    return "\n".join(new_lines)


def _persist_chart_field_result(
    result: dict[str, Any],
    upd_sec,
    section_id: int,
    chart_type: str,
    payload: dict[str, Any],
    reason: str,
    source: str = "",
) -> None:
    """把图表载荷降级持久化到章节 chart 字段（*_json）。

    F2 重构 2026-11-03：降级落库口径统一收口——正文为空 / 禁止插入正文 /
    同类型内联标记已存在三条路径复用同一写库代码，避免各分支漂移。

    Args:
        reason: 透传到 ``result["skipped_reason"]``：
          - ``content_empty_fallback_chart_field``：正文为空或禁止插入正文（旧口径）；
          - ``chart_marker_exists_fallback_chart_field``：正文已有同类型
            ``<<CHART:...>>`` 标记，围栏不得再插（防同章双图），标记渲染路径
            消费该 chart 字段出图。
        source: 分析来源（local/ai），R2 观测加固：随日志输出用于统计
            AI 命中率（``[自动编排]`` 日志驱动的全链路观测）。
    """
    field = CHART_FIELD_MAP.get(chart_type)
    if not field:
        result["skipped_reason"] = "unsupported_chart_type"
        return
    upd_sec(
        section_id,
        SectionUpdate(**{field: json.dumps(payload, ensure_ascii=False)}),
    )
    result["persisted_chart_field"] = field
    result["skipped_reason"] = reason
    logger.info(
        "[自动编排] 已持久化 chart 字段 section=%d field=%s reason=%s source=%s",
        section_id, field, reason, source,
    )


# ============================================================
# 闭环编排（含 DB 读写；DB 依赖通过模块级名字注入，便于单测 monkeypatch）
# ============================================================


def section_to_dict(section: Any) -> dict[str, Any]:
    """把章节对象统一归一为 dict。

    BUG-FIX 2026-11-04（P1·契约不一致）：闭环编排/配图阶段全链路按「dict 章节」
    编写（`section.get("title")` 等），而服务层 `services.sections.get_section`
    返回 pydantic `Section`（无 `.get`）。生产路径（未注入测试 fake）首次访问即
    AttributeError → 单章节自动编排接口 500 / 配图阶段整体崩溃。测试全量注入
    dict fake 掩盖了该缺陷。此处统一归一，同时兼容 dict / pydantic v2 模型 /
    其它 duck-typed 对象；None 原样返回由调用方判空。
    """
    if section is None or isinstance(section, dict):
        return section
    if hasattr(section, "model_dump"):
        try:
            return section.model_dump()
        except Exception:  # pragma: no cover - model_dump 异常时退回字段拷贝
            pass
    return dict(vars(section)) if hasattr(section, "__dict__") else dict(section)


def auto_orchestrate_section(
    project_id: int,
    section_id: int,
    *,
    insert_into_content: bool = True,
    ai_client: Any = None,
    _get_section=None,
    _update_section=None,
) -> dict[str, Any]:
    """对单个章节执行自动图表编排闭环。

    Args:
        project_id: 项目 ID（用于日志与权限上下文）
        section_id: 章节 ID
        insert_into_content: True 时把图表围栏写入正文最佳位置（默认）；
            False 或正文不可写时降级持久化 chart 字段。
        ai_client: 可选 AI 客户端（None 走本地推断/模板生成，快速路径）
        _get_section/_update_section: 测试注入点（默认用 services.sections 实现）

    Returns:
        结果字典（永远可 JSON 序列化，含 analysis/inserted/skipped_reason 等）
    """
    get_sec = _get_section or get_section
    upd_sec = _update_section or update_section

    result: dict[str, Any] = {
        "ok": True,
        "section_id": section_id,
        "analysis": None,
        "inserted": False,
        "persisted_chart_field": "",
        "skipped_reason": "",
        "validation_errors": [],
        "insert_paragraph_index": -1,
        "payload": None,
        "fence": "",
    }

    section = section_to_dict(get_sec(section_id))
    if not section:
        result["ok"] = False
        result["skipped_reason"] = "section_not_found"
        return result

    title = section.get("title", "") or ""
    content = section.get("content", "") or ""

    # ① 分析：AI/本地判断是否需要配图与图表类型
    analysis = analyze_chart_needs(
        section_title=title, content=content, ai_client=ai_client
    )
    analysis["insert_paragraph_index"] = (
        suggest_insertion_position(content, analysis.get("chart_type", "none"), title)
        if analysis.get("needed")
        else -1
    )
    result["analysis"] = analysis
    result["insert_paragraph_index"] = analysis["insert_paragraph_index"]

    if not analysis.get("needed"):
        result["skipped_reason"] = "not_needed"
        return result

    chart_type = analysis.get("chart_type", "none")
    title_hint = analysis.get("title_hint", "") or title

    # ② 生成载荷（本地模板快速路径，可选 AI 增强）
    payload = generate_chart_payload(
        section_title=title,
        content=content,
        chart_type=chart_type,
        title_hint=title_hint,
        ai_client=ai_client,
    )
    if not payload:
        result["skipped_reason"] = "payload_generation_failed"
        logger.info(
            "[自动编排] 载荷生成失败，跳过 project=%d section=%d type=%s",
            project_id, section_id, chart_type,
        )
        return result

    # ②.5 A3 建议 2（空态优于错态，2026-11-03 遗留加固）：本地模板兜底仅在
    # 正文提供可提取数据线索时才进投标文件——正文有内容但零线索（数据型
    # 零「数字+单位」/ 结构型零关键词命中）时，模板数字/节点纯属虚构，
    # 宁缺毋假不插围栏、不落库。仅约束 source="local" 兜底载荷：AI 载荷
    # 受提示词数据约束且基于正文生成，不做此判定；空正文走既有降级落库
    # 契约（content_empty_fallback_chart_field，披露标签兜底）不在此收紧。
    # 正文已有该类型围栏/已达上限时本载荷因去重必然不出图（chart_fence_type_conflict
    # 语义更精确，ENHANCE 2026-11-04 起「同类型/达上限」才让位），
    # 空态判定让位——只拦「会让模板载荷实际出图」的路径（插围栏/标记降级落库）。
    if (
        str(payload.get("source") or "") == "local"
        and content.strip()
        and not chart_fence_type_conflict(content, chart_type)
        and not content_has_extractable_data(content, chart_type)
    ):
        result["skipped_reason"] = "no_real_data_local"
        logger.info(
            "[自动编排] 正文无可提取数据，本地模板兜底跳过（宁缺毋假）"
            "project=%d section=%d type=%s",
            project_id, section_id, chart_type,
        )
        return result

    # ③ 统一闸门校验 + 修复（repair_mermaid 内聚于此）
    from .ai.image_engine import validate_chart_for_ai_pipeline

    try:
        ok, errors, fixed_payload, _mutated, _ctx = validate_chart_for_ai_pipeline(
            chart_type, payload, verbose=True, section_title=title
        )
    except Exception as e:  # 校验器异常不得炸穿编排
        ok, errors, fixed_payload = False, [f"validator_exception: {e}"], payload
    if not ok:
        # 容错降级：坏块不写入正文（不占图号，导出侧无感）
        result["skipped_reason"] = "validation_failed"
        result["validation_errors"] = list(errors)[:5]
        logger.info(
            "[自动编排] 校验未通过，跳过 project=%d section=%d type=%s errors=%s",
            project_id, section_id, chart_type, result["validation_errors"],
        )
        return result

    # ④ 插入正文（去重防重入）或降级持久化 chart 字段
    result["payload"] = fixed_payload
    # F2 修复 2026-11-03（需求 11·同章双图 / 编号冲突）：正文已有**同类型**
    # ``<<CHART:...>>`` 内联标记时不再插入围栏——导出侧 parse_content 把标记
    # 与围栏都渲染成图，并存即同章两张同类图、图号多占一位。按类型判定，
    # 不同类型的标记与围栏并存是合理形态（甘特图围栏 + 流程图标记），互不阻止。
    # ENHANCE 2026-11-04（需求 3/4·编排增强）：围栏去重同步从「任一围栏即全拒」
    # 放宽为「同类型去重 + 每章 ≤2 图」（chart_fence_type_conflict），对齐
    # CONTENT_SECTION_SYSTEM「每章最多 2 个图表且类型不重复」的写作契约——
    # 正文已有甘特图时仍可补插流程图，恢复图文互补能力。
    _has_same_type_marker = content_has_chart_marker_type(content, chart_type)
    if (
        insert_into_content
        and content
        and not chart_fence_type_conflict(content, chart_type)
        and not _has_same_type_marker
    ):
        lead_in, fence = build_chart_fence(chart_type, fixed_payload)
        # RACE-GUARD 2026-10-27（需求 11·并发竞态）：自「读取正文」到「写回」
        # 之间存在窗口，其他编排请求/生成任务可能已写入围栏或改写正文。
        # 插入前重读最新正文：已含围栏 → 跳过（防同章双图/图号虚跳）；
        # 正文已变 → 以最新正文重算插入位置，保证图文连贯。
        try:
            # section_to_dict：与函数首部同口径归一（RACE-GUARD 重读同样可能
            # 拿到 pydantic Section，旧实现 AttributeError 被 except 吞掉后
            # 恒走 stale-content 分支，防双图/重算位置守卫在生产环境形同虚设）。
            _latest = section_to_dict(get_sec(section_id)) or {}
            _latest_content = _latest.get("content", "") or ""
        except Exception:
            _latest_content = content
        if _latest_content and chart_fence_type_conflict(_latest_content, chart_type):
            result["skipped_reason"] = "chart_fence_exists"
            return result
        if _latest_content != content:
            if content_has_chart_marker_type(_latest_content, chart_type):
                # 竞态窗口内同类型标记已写入 → 转降级落库
                # （标记渲染路径消费 chart 字段出图，不再插围栏防双图）
                _persist_chart_field_result(
                    result, upd_sec, section_id, chart_type, fixed_payload,
                    "chart_marker_exists_fallback_chart_field",
                    source=analysis.get("source", ""),
                )
                return result
            content = _latest_content
            analysis["insert_paragraph_index"] = suggest_insertion_position(
                content, chart_type, title
            )
            result["insert_paragraph_index"] = analysis["insert_paragraph_index"]
        new_content = insert_fence_at_paragraph(
            content, fence, lead_in, analysis["insert_paragraph_index"]
        )
        upd_sec(section_id, SectionUpdate(content=new_content))
        result["inserted"] = True
        result["fence"] = fence
        logger.info(
            "[自动编排] 已插入图表围栏 project=%d section=%d type=%s source=%s 段落=%d",
            project_id, section_id, chart_type,
            analysis.get("source", ""), analysis["insert_paragraph_index"],
        )
        return result

    if content and chart_fence_type_conflict(content, chart_type):
        result["skipped_reason"] = "chart_fence_exists"
        return result

    # 降级把载荷写入章节 chart 字段（导出按 chart 字段路径渲染）：
    #   - 正文为空 / 禁止插入正文 → content_empty_fallback_chart_field（旧口径）；
    #   - 正文已有同类型内联标记 → chart_marker_exists_fallback_chart_field
    #     （防双图，标记渲染路径消费该字段）。
    _fallback_reason = (
        "chart_marker_exists_fallback_chart_field"
        if _has_same_type_marker
        else "content_empty_fallback_chart_field"
    )
    _persist_chart_field_result(
        result, upd_sec, section_id, chart_type, fixed_payload, _fallback_reason,
        source=analysis.get("source", ""),
    )

    return result
