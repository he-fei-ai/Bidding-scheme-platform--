"""Mermaid 图表渲染引擎（facade）。

本文件是拆分后的统一入口：保留向后兼容的公共 API，
实际渲染逻辑已拆分到 7 个图表子文件：

  - mermaid_common.py         公共工具（字体、文本绘制、300 DPI 装饰器）
  - mermaid_flowchart.py      流程图
  - mermaid_gantt.py          甘特图
  - mermaid_architecture.py   组织架构图
  - mermaid_comparison.py     对比图 / 饼图 / 柱状图
  - mermaid_labor.py          劳动力配置图
  - mermaid_layout.py         总平面布置图
  - mermaid_timeline.py       关键里程碑时间轴

外部调用方继续使用：
    from .mermaid_renderer import render_mermaid_to_bytes, ChartCache

所有拆分前可用的内部函数（_render_xxx_image_v2、_parse_xxx、_repair_xxx_data 等）
均通过 re-export 暴露，import 路径不变。
"""
from __future__ import annotations

import asyncio
from io import BytesIO
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import threading
import time

from .mermaid_service import MermaidRenderError, get_mermaid_service_client


logger = logging.getLogger(__name__)


# =========================================================================
# P0-5 加固：里程碑时间线自动识别
# -------------------------------------------------------------------------
# 部分 AI 把「里程碑时间线」以 flowchart 形式输出（含日期节点的有向图），
# 此时应识别为时间线并以 timeline 渲染器输出，而非普通流程图。
# 判定：≥3 个含 YYYY-MM-DD 日期的节点 + 至少一个里程碑关键词。
# =========================================================================
_MILESTONE_KEYWORDS = ("开工", "竣工", "封顶", "里程碑", "关键节点", "主体", "完工", "交付")
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _extract_flowchart_node_labels(mermaid_code: str) -> list[str]:
    """从 flowchart Mermaid 代码中提取所有节点标签（去重相邻重复）。

    优先识别 `id["label"]`（sanitizer 输出），其次 `id[label]`；
    同一节点若连续出现两次（如 `A --> B` 中 A 是上一行终点），只保留一次。
    """
    if not mermaid_code:
        return []
    # 先尝试带双引号的标准形式
    labels = re.findall(r'\w+\s*\["([^"]+)"\]', mermaid_code)
    if not labels:
        labels = re.findall(r'\w+\s*\[([^\]]+)\]', mermaid_code)
    out: list[str] = []
    for label in labels:
        if not out or label != out[-1]:
            out.append(label)
    return out


def _is_milestone_timeline(mermaid_code: str) -> bool:
    """判断一段 flowchart Mermaid 代码是否实为里程碑时间线。"""
    nodes = _extract_flowchart_node_labels(mermaid_code)
    if len(nodes) < 3:
        return False
    date_nodes = [n for n in nodes if _DATE_RE.search(n)]
    if len(date_nodes) < 3:
        return False
    return any(kw in mermaid_code for kw in _MILESTONE_KEYWORDS)


def _flowchart_to_timeline_plan(mermaid_code: str) -> dict | None:
    """将含日期节点的 flowchart 转换为 TimelinePlan JSON（供 timeline 渲染器）。

    节点标签形如 `开工: 2026-09-22` 或 `2026-09-22 开工`，提取日期与名称，
    按日期升序排列为里程碑。解析失败返回 None（调用方回落普通流程图渲染）。
    """
    nodes = _extract_flowchart_node_labels(mermaid_code)
    if len(nodes) < 3:
        return None
    milestones: list[dict] = []
    for label in nodes:
        m = _DATE_RE.search(label)
        if not m:
            return None
        date = m.group(0)
        raw = label.strip().strip('"').strip("'")
        name = _DATE_RE.sub("", raw).strip(" :：").strip()
        if not name:
            name = raw
        milestones.append({"name": name, "date": date, "status": "pending"})
    milestones.sort(key=lambda x: x["date"])
    # 让首个为已完成、其余待定，避免全部同态（与 timeline 兜底规范一致）
    if milestones:
        milestones[0]["status"] = "completed"
    return {"type": "timeline", "title": "项目关键里程碑节点", "milestones": milestones}


def render_mermaid_to_bytes(
    mermaid_code: str,
    chart_type: str = "flowchart",
    *,
    duration: int = 90,
    unit: str = "天",
    tick: int = 5,
    skip_http: bool = False,
) -> BytesIO | None:
    """
    将 Mermaid 代码/JSON 数据渲染为 PNG 图片字节流（商业级渲染管道 — 无降级 V5.0）

    分层策略（严格执行"不做降级、按最优标准渲染"）：
      1. Level 1.5: Mermaid HTTP Service (Docker/Railway) → 2x 高清 PNG（所有图表类型）
      2. Level 2: PIL/Pillow 自定义 v2 商业级渲染器（流程图/甘特图/架构图/对比图/劳动力图/布局图/时间轴）
         • 2.5x 超采样抗锯齿 + 300 DPI 元数据（@_render_hq 装饰器）
      3. 返回 None（调用方自行处理，不做降级）

    V5.0 核心变更：
      - 移除 Level 1 (mmdc CLI) 降级路径（mmdc ~96 DPI 低于 PIL v2 2x 超采样 + 300 DPI）
      - 移除 Level 3 (v1 PIL 渲染器) 降级路径（无抗锯齿、无 DPI 元数据）
      - 所有图表类型仅走 Level 1.5 → Level 2 双路径
      - 符合"不做降级、按最优标准渲染"原则

    Args:
        mermaid_code: Mermaid 代码字符串 或 结构化 JSON 字符串
        chart_type: 图表类型 (flowchart|gantt|architecture|comparison|labor|layout|timeline|chart)
        duration: 甘特图总工期
        unit: 甘特图时间单位
        tick: 甘特图刻度间隔
        skip_http: BUG-E-02 修复：跳过 HTTP Service 渲染路径（当 HTTP 服务不可用时）
                   直接走 PIL v2 渲染路径，节省重试超时等待

    Returns:
        BytesIO of PNG image, or None if all levels fail
    """
    import time as _time

    _t0 = _time.perf_counter()
    logger.info("[渲染] 开始 chart_type=%s code长度=%d", chart_type, len(mermaid_code or ""))

    if not mermaid_code or not str(mermaid_code).strip():
        logger.warning("[渲染] 空输入直接返回 None")
        return None

    chart_type = (chart_type or "flowchart").lower().strip()
    code = str(mermaid_code).strip()
    logger.info("[渲染] chart_type=%s code前80字=%r", chart_type, code[:80])

    # ---------- 解析 chart 通配符（自动从内部 type 字段识别） ----------
    if chart_type == "chart":
        try:
            parsed = json.loads(code)
            if isinstance(parsed, dict):
                inner_type = (parsed.get("type") or "").lower()
                if inner_type in {
                    "flowchart",
                    "gantt",
                    "architecture",
                    "labor",
                    "comparison",
                    "layout",
                    "timeline",
                }:
                    chart_type = inner_type
                    logger.info("[渲染] 通配符解析: chart→%s", chart_type)
        except (json.JSONDecodeError, TypeError):
            pass

    # ---------- V6.1 载荷归一化：解包数据库存储格式 ----------
    # 数据库/编排链路存储格式为 JSON 包装器：{"type": ..., "code": "<载荷>", "title": ...}
    # 不解包会导致：
    #   1. Mermaid 系类型（flowchart/gantt/comparison）：HTTP Service 收到 JSON 而非
    #      Mermaid 语法 → 第一优先方案从未真正执行成功，白白消耗重试等待
    #   2. layout/timeline：提示词 v2.1 实际产出 JSON 数据（zones/milestones），
    #      经 "code" 字段二次包装成双层 JSON → PIL v2 直接解析必失败
    # 归一化规则：
    #   - 包装器含 "code" 字符串字段 → 提取内层载荷
    #     · 内层为 Mermaid 语法（非 { 开头）→ 作为纯 Mermaid 代码（HTTP 可渲染）
    #     · 内层为 JSON → 解包为结构化数据（PIL v2 渲染），继承外层 title
    #   - 包装器无 "code" 字段（architecture/labor 等原生数据）→ 保持原样，PIL v2 直接处理
    try:
        _wrapper = json.loads(code)
        if isinstance(_wrapper, dict):
            _inner = _wrapper.get("code")
            if isinstance(_inner, str) and _inner.strip():
                _inner = _inner.strip()
                if not _inner.startswith("{"):
                    # 内层是纯 Mermaid 代码 → 第一优先方案（HTTP Service）可执行
                    code = _inner
                    logger.info("[渲染] 载荷归一化: 提取内层 Mermaid 代码 (%d 字符)", len(code))
                else:
                    # 内层仍是 JSON（layout/timeline 双层包装）→ 解包为数据载荷
                    try:
                        _inner_data = json.loads(_inner)
                        if isinstance(_inner_data, dict):
                            if _wrapper.get("title") and not _inner_data.get("title"):
                                _inner_data["title"] = _wrapper.get("title")
                            code = json.dumps(_inner_data, ensure_ascii=False)
                            logger.info(
                                "[渲染] 载荷归一化: 解包内层 JSON 数据 (keys=%s)",
                                list(_inner_data.keys())[:6],
                            )
                    except (json.JSONDecodeError, TypeError):
                        pass
    except (json.JSONDecodeError, TypeError):
        # 非 JSON → 纯 Mermaid 代码，无需归一化
        pass

    # ---------- BUG-2026-1008-01（P0）：Mermaid 语法预处理 ----------
    # AI 生成 Mermaid 代码时经常违反语法规范（节点标签未加引号、含中文冒号/
    # 日期/百分号、连写多节点、行末分号、中文节点 ID），导致 HTTP 渲染服务与
    # PIL v2 解析器都失败，图表被静默降级为纯文本。
    # 预处理策略：仅对 Mermaid 语法代码生效（JSON 数据载荷不进入本层）；
    # 仅当检测到需要清洗的字符模式时才调用 sanitize，避免对合规代码做无用功。
    if not code.lstrip().startswith("{"):
        from .mermaid_sanitizer import SANITIZER

        if SANITIZER.needs_sanitization(code):
            _sanitized = SANITIZER.sanitize(code)
            if _sanitized != code:
                logger.info(
                    "[渲染] MermaidSanitizer 生效: chart_type=%s 原长=%d 新长=%d",
                    chart_type,
                    len(code),
                    len(_sanitized),
                )
                code = _sanitized

    # ---------- P0-5 加固：里程碑时间线自动识别与路由 ----------
    # 若 chart_type 为 flowchart，但代码实为里程碑时间线（含多个日期节点 +
    # 里程碑关键词），则转「垂直里程碑流程图」渲染器，输出与参考图一致的
    # 浅紫色圆角矩形 + 黑色向下箭头样式；失败时静默回落普通流程图渲染。
    if chart_type == "flowchart" and not code.lstrip().startswith("{"):
        try:
            if _is_milestone_timeline(code):
                _tl_plan = _flowchart_to_timeline_plan(code)
                if _tl_plan:
                    _tl_img = _render_milestone_flowchart_image_v2(_tl_plan)
                    if _tl_img is not None:
                        elapsed = _time.perf_counter() - _t0
                        logger.info(
                            "[渲染] 里程碑时间线自动路由成功 chart_type=%s 耗时=%.2fs",
                            chart_type,
                            elapsed,
                        )
                        return _tl_img
        except Exception as _e:
            # ENHANCE 2026-10-11（渲染策略可观测性）：里程碑时间线路由失败
            # 意味着本该用专用渲染器输出更好的样式，却回落到普通流程图渲染。
            # 这是可观测的渲染质量降级，debug 级别运维感知不到，升级为 info。
            logger.info(
                "[渲染] 里程碑时间线自动路由失败，回落普通流程图: %s", _e
            )

    # =========================================================
    # 渲染管道优先级（商业级最优策略 — 无降级 V5.0）
    # =========================================================
    #
    # 优先级策略（2026-08 增强版 — 严格执行"不做降级、按最优标准渲染"）：
    #   1. Level 1.5: Mermaid HTTP Service (Docker/Railway) — 主路径
    #      • 支持全部 7 种图表类型
    #      • 2.5x 高清 PNG，Mermaid 原生渲染引擎，质量最高
    #      • 本地 Docker / Railway Cloud / mmdc 自动检测
    #      • 超时 30s，最多重试 2 次
    #   2. Level 2: PIL/Pillow v2 商业级渲染器 — 零外部依赖，始终可用
    #      • 2.5x 超采样抗锯齿 + 300 DPI 元数据（@_render_hq 装饰器）
    #      • 支持全部 7 种图表类型（流程图/甘特图/架构图/对比图/劳动力图/布局图/时间轴）
    #      • 质量不低于 HTTP Service（超采样补偿）
    #   3. 不再使用 Level 1 (mmdc CLI) 降级路径：
    #      • mmdc 屏幕分辨率渲染（~96 DPI）低于 PIL v2（2.5x 超采样 + 300 DPI）
    #      • 移除 mmdc 可避免 60 秒超时等待、降低系统依赖
    #      • 符合"不做降级、按最优标准渲染"原则
    #   4. 不再使用 Level 3 (v1 PIL 渲染器) 降级路径：
    #      • v1 渲染器无抗锯齿、无 DPI 元数据，质量无法满足商业级要求
    #      • 所有 v1 渲染器已被功能更全的 v2 版本替代
    #
    # 核心变更（V5.0）：
    #   - 移除 Level 1 (mmdc) 降级回退
    #   - 确保所有图表类型仅走 Level 1.5 → Level 2 双路径
    #   - 当两者均失败时返回 None（调用方自行处理，不做降级）
    # =========================================================

    # ---------- Level 1.5 (主路径): Mermaid HTTP Service 渲染（2x 高清 PNG，Mermaid 语法载荷）----------
    # BUG-F-01 修复：_http_renderable 必须先初始化。
    # 原代码只在 skip_http=True 分支赋值，skip_http=False（HTTP 服务可用/检测结果未知）时
    # 直接引用未定义局部变量，抛 UnboundLocalError，导致所有图表渲染失败（图表全部丢失）。
    _http_renderable = not skip_http
    # BUG-E-02 修复：skip_http=True 时完全跳过 HTTP Service 路径
    # 在本地开发环境中 HTTP Service 通常不可用，每图尝试 30s 超时严重拖慢导出
    if skip_http:
        logger.info(
            "[渲染] Level 1.5 跳过: skip_http=True, 直接走 PIL v2 (chart_type=%s)", chart_type
        )
    # V6.1: HTTP Service 仅接受 Mermaid 语法。JSON 数据载荷（architecture/labor 的
    # 原生数据、layout/timeline 解包后的 zones/milestones）对 HTTP 服务必然失败，
    # 此类类型的第一优先方案即 PIL v2 商业级渲染器（唯一的原生渲染路径）。
    # 跳过无效 HTTP 尝试可省去 2 次失败重试（每次数秒），显著提升导出速度。
    _http_renderable = _http_renderable and not code.lstrip().startswith("{")
    if not _http_renderable:
        logger.info("[渲染] Level 1.5 跳过: JSON 数据载荷直接走 PIL v2 (chart_type=%s)", chart_type)
    _mermaid_client = get_mermaid_service_client(timeout=30)
    _http_bytes: bytes | None = None
    _max_retries = 2
    # BUG-F-02 修复：为整段 HTTP 尝试设置总预算（20s）。
    # 原代码每次尝试 30s、最多 2 次（+1s sleep），最坏可叠加到 62s，超过 _add_inline_chart
    # 外层超时保护后，渲染线程被 cancel 但无法回收（shutdown(wait=False)），
    # 滞留线程持续占用 httpx 连接与 CPU，多次导出后线程堆积导致导出卡顿/无响应。
    _http_total_budget = 20.0
    _http_attempt_start = _time.perf_counter()
    for _retry in range(_max_retries if _http_renderable else 0):
        if _retry > 0:
            logger.info("[渲染] Level 1.5 重试第 %d 次 (chart_type=%s)", _retry + 1, chart_type)
        _remaining = _http_total_budget - (_time.perf_counter() - _http_attempt_start)
        if _remaining <= 0:
            logger.warning(
                "[渲染] Level 1.5 总预算耗尽（%.1fs），放弃后续 HTTP 尝试", _http_total_budget
            )
            break
        try:
            _render_coro = asyncio.wait_for(
                _mermaid_client.render_mermaid_via_http(
                    code=code,
                    theme="neutral",
                    scale=2.5,
                ),
                timeout=_remaining,
            )
            from .workflows import _safe_asyncio_run

            if hasattr(_render_coro, "__await__"):
                try:
                    _http_bytes = _safe_asyncio_run(_render_coro)
                except Exception:
                    # 协程泄漏防护：_safe_asyncio_run 异常时协程可能未被消费，
                    # 显式关闭避免 "coroutine was never awaited"（对已完成协程是 no-op）
                    if asyncio.iscoroutine(_render_coro):
                        try:
                            _render_coro.close()
                        except Exception as _e:
                            logger.debug("[silent-except] mermaid_renderer.py: line 232 - %s", _e)
                    raise
            else:
                _http_bytes = _render_coro

            if _http_bytes and len(_http_bytes) > 100:
                # BUG-FIX 2026-09-18（P2）：只认 PNG 魔数。mermaid 服务对语法错误
                # 常返回 200 + 错误页 SVG/HTML（>100 字节），旧实现按长度判成功，
                # 坏字节被当图片下传并污染磁盘缓存，最终在导出端静默丢图。
                if _http_bytes[:8] == b"\x89PNG\r\n\x1a\n":
                    logger.info(
                        "MermaidRenderer: 使用 HTTP Service 渲染成功 (%s, 2.5x, 重试=%d/2)",
                        chart_type,
                        _retry + 1,
                    )
                    return BytesIO(_http_bytes)
                logger.warning(
                    "[MermaidRenderer] HTTP 渲染响应非 PNG（格式=%.8s，%d bytes），"
                    "视为失败落 PIL v2 兜底: chart_type=%s",
                    _http_bytes[:8],
                    len(_http_bytes),
                    chart_type,
                )
                _http_bytes = None
            else:
                logger.warning(
                    "[MermaidRenderer] HTTP 渲染返回空/过小数据 (%d bytes)，重试...",
                    len(_http_bytes or b""),
                )
                _http_bytes = None
        except MermaidRenderError as e:
            logger.warning("Mermaid HTTP Service 渲染失败（第%d次）: %s", _retry + 1, e)
        except Exception as e:
            logger.warning("Mermaid HTTP Service 异常（第%d次）: %s", _retry + 1, e)
        if _http_bytes is None and _retry < _max_retries - 1:
            # BUG-FIX 2026-09-18（P2）：去掉固定 sleep(1.0)——重试段已运行在
            # 共享渲染池（仅 4 线程）内，空睡期间 worker 完全白白占用，
            # 加剧池饥饿；整段 HTTP 已有 20s 总预算控速，无需额外固定退避。
            continue

    # ---------- Level 1 (mmdc) 已移除 ----------
    # 原因：mmdc 渲染质量（~96 DPI 屏幕分辨率）低于 PIL v2 商业级渲染器（2.5x 超采样 + 300 DPI）
    # 且 mmdc 需要额外安装 Node.js 依赖，增加系统复杂度。
    # 符合"不做降级、按最优标准渲染"原则。
    # ---------- Level 2 (PIL v2): 商业级渲染器（零外部依赖，始终可用）----------
    logger.info("[渲染] Level 2 (主路径): 尝试 PIL v2 商业级渲染器 chart_type=%s", chart_type)
    _level2_result: BytesIO | None = None
    try:
        # === 流程图（支持 mermaid 语法 + JSON 节点列表）===
        if chart_type == "flowchart":
            if (
                code.startswith("flowchart")
                or code.startswith("graph")
                or code.startswith("%%{")
                or "-->" in code
            ):
                _level2_result = _render_flowchart_image_v2(code)
            if _level2_result is None:
                _plain_nodes: list[str] | None = None
                try:
                    parsed = json.loads(code)
                    if isinstance(parsed, dict):
                        nodes = parsed.get("nodes", [])
                        # 条件放宽：仅有 nodes 也能走完整渲染链路（函数内部自动补 edges）
                        if isinstance(nodes, list) and len(nodes) >= 2:
                            # 第一优先：JSON → 标准 Mermaid → v2 超采样渲染器（决策菱形 + 专业配色）
                            _edges_mermaid = flowchart_json_to_mermaid(parsed)
                            if _edges_mermaid:
                                _level2_result = _render_flowchart_image_v2(_edges_mermaid)
                            # 次优先：完整流程图渲染器（带箭头标签，300 DPI）
                            if _level2_result is None:
                                _level2_result = _render_flowchart_with_edges(parsed)
                        if _level2_result is None and isinstance(nodes, list) and len(nodes) >= 2:
                            _plain_nodes = [str(n) for n in nodes if str(n).strip()]
                except (json.JSONDecodeError, TypeError):
                    pass
                if _level2_result is None and _plain_nodes is None:
                    _plain_nodes = _parse_mermaid_flowchart(code)
                # 最终防线：纯节点列表 → 构造顺序连线 Mermaid → v2 商业级渲染
                if _level2_result is None and _plain_nodes and len(_plain_nodes) >= 2:
                    _seq_code = "flowchart TD\n    " + " --> ".join(
                        f'N{i}["{str(n).replace(chr(34), chr(39))}"]'
                        for i, n in enumerate(_plain_nodes, 1)
                    )
                    _level2_result = _render_flowchart_image_v2(_seq_code)

        # === 甘特图（优先 v2 商业级：自动开始日期 + 真实进度）===
        if _level2_result is None and chart_type == "gantt":
            if "gantt" in code.lower() and "section" in code.lower():
                tasks = _parse_mermaid_gantt(code)
                if tasks:
                    max_end = max((t["end"] for t in tasks), default=duration)
                    plan = {
                        "type": "gantt",
                        "title": "施工进度计划",
                        "totalDays": max(max_end, duration),
                        "tasks": [
                            {
                                "id": idx + 1,
                                "name": t["task"],
                                "start": t["start"],
                                "end": t["end"],
                                "dependencies": [],
                                "isMilestone": t["start"] == t["end"],
                            }
                            for idx, t in enumerate(tasks[:30])
                        ],
                    }
                    _level2_result = _render_gantt_image_v2(plan)
            if _level2_result is None:
                try:
                    parsed = json.loads(code)
                    if isinstance(parsed, dict) and "tasks" in parsed:
                        tasks_list = parsed.get("tasks", [])
                        has_enhanced = any(
                            t.get("id") or t.get("dependencies") or t.get("critical")
                            for t in tasks_list
                            if isinstance(t, dict)
                        )
                        if has_enhanced:
                            _level2_result = _render_gantt_with_dependencies(parsed)
                        if _level2_result is None:
                            _level2_result = _render_gantt_image_v2(parsed)
                except (json.JSONDecodeError, TypeError):
                    pass
                if _level2_result is None:
                    # V6.1 无降级：宽松解析结果构造 plan dict，仍走 v2 商业级渲染器
                    tasks = _parse_mermaid_gantt(code)
                    if tasks:
                        max_end = max((t["end"] for t in tasks), default=duration)
                        plan = {
                            "type": "gantt",
                            "title": "施工进度计划",
                            "totalDays": max(max_end, duration),
                            "tasks": [
                                {
                                    "id": idx + 1,
                                    "name": t["task"],
                                    "start": t["start"],
                                    "end": t["end"],
                                    "dependencies": [],
                                    "isMilestone": t["start"] == t["end"],
                                }
                                for idx, t in enumerate(tasks[:30])
                            ],
                        }
                        _level2_result = _render_gantt_image_v2(plan)

        # === 架构图（商业级 v2 树形可视化）===
        if _level2_result is None and chart_type == "architecture":
            try:
                parsed = json.loads(code) if code.startswith(("{", "[")) else None
            except (json.JSONDecodeError, TypeError):
                parsed = None
            if isinstance(parsed, dict):
                root_node = parsed
                if not (parsed.get("label") or parsed.get("name") or parsed.get("children")):
                    for key in ("root", "tree", "data", "node"):
                        nested = parsed.get(key)
                        if isinstance(nested, dict) and (
                            nested.get("label") or nested.get("name") or nested.get("children")
                        ):
                            root_node = nested
                            break
                    # 兼容 {type: "architecture", root: "xxx", nodes: [...]} 格式
                    if not (
                        root_node.get("label") or root_node.get("name") or root_node.get("children")
                    ):
                        nodes_list = parsed.get("nodes")
                        if isinstance(nodes_list, list) and nodes_list:
                            root_node = {
                                "label": str(parsed.get("root") or "组织架构图"),
                                "children": nodes_list,
                            }
                if root_node.get("label") or root_node.get("name") or root_node.get("children"):
                    _level2_result = _render_architecture_image_v2(root_node)
            # V6.1 无降级：v2 树形渲染失败即返回 None，不再回退 v1 表格渲染器

        # === 劳动力图（商业级 v2 四合一视图）===
        if _level2_result is None and chart_type == "labor":
            try:
                parsed = json.loads(code) if code.startswith("{") else None
            except (json.JSONDecodeError, TypeError):
                parsed = None
            if isinstance(parsed, dict):
                _level2_result = _render_labor_image_v2(parsed)
            # V6.1 无降级：v2 四合一视图渲染失败即返回 None，不再回退 v1 渲染器

        # === 对比图（商业级 v2 饼图 + 柱状图组合）===
        if _level2_result is None and chart_type == "comparison":
            try:
                parsed = json.loads(code) if code.startswith("{") else None
            except (json.JSONDecodeError, TypeError):
                parsed = None
            if isinstance(parsed, dict):
                _level2_result = _render_comparison_image_v2(parsed)
            if _level2_result is None:
                # V6.1: Mermaid pie / xychart-beta 代码 → 解析为数据后走 v2 渲染器
                # （HTTP Service 不可用时的高质量备份路径，渲染质量与主路径一致）
                _mermaid_cmp_data = _parse_mermaid_pie(code) or _parse_mermaid_xychart(code)
                if _mermaid_cmp_data:
                    logger.info(
                        "[渲染] 对比图: 解析 Mermaid 代码为 v2 数据 (items=%d)",
                        len(_mermaid_cmp_data.get("items", [])),
                    )
                    _level2_result = _render_comparison_image_v2(_mermaid_cmp_data)
            # V6.1 无降级：v2 饼图+柱状图渲染失败即返回 None，不再回退 v1 表格渲染器

        # === 布局图（商业级 v2 空间布局）===
        if _level2_result is None and chart_type == "layout":
            try:
                parsed = json.loads(code) if code.startswith("{") else None
            except (json.JSONDecodeError, TypeError):
                parsed = None
            if isinstance(parsed, dict):
                if not parsed.get("zones") and parsed.get("areas"):
                    parsed["zones"] = parsed["areas"]
                _level2_result = _render_layout_image_v2(parsed)
                if _level2_result is None:
                    repaired = _repair_layout_data(parsed)
                    if repaired:
                        _level2_result = _render_layout_image_v2(repaired)

        # === 时间轴/里程碑图（商业级 v2）===
        if _level2_result is None and chart_type == "timeline":
            try:
                parsed = json.loads(code) if code.startswith("{") else None
            except (json.JSONDecodeError, TypeError):
                parsed = None
            if isinstance(parsed, dict):
                for m in parsed.get("milestones", []):
                    if isinstance(m, dict) and not m.get("name") and m.get("title"):
                        m["name"] = m["title"]
                _level2_result = _render_timeline_image_v2(parsed)
                if _level2_result is None:
                    repaired = _repair_timeline_data(parsed)
                    if repaired:
                        _level2_result = _render_timeline_image_v2(repaired)

    except Exception as e:
        logger.debug("PIL v2 渲染失败 (%s): %s", chart_type, e)
        logger.exception("详细错误：")

    if _level2_result is not None:
        elapsed = _time.perf_counter() - _t0
        logger.info(
            "[渲染] 成功 chart_type=%s 策略=PIL_v2 耗时=%.2fs 大小=%dKB",
            chart_type,
            elapsed,
            len(_level2_result.getvalue()) // 1024,
        )
        return _level2_result

    elapsed = _time.perf_counter() - _t0
    logger.warning("[渲染] 全部渲染策略失败 chart_type=%s 耗时=%.2fs", chart_type, elapsed)
    logger.warning("[渲染] 建议检查: 1)HTTP服务是否可用 2)PIL v2渲染器是否正常")
    return None

class ChartCache:
    """图表渲染缓存 - 基于 Mermaid 代码/JSON 数据的哈希值

    避免重复渲染相同图表，提升批量导出性能。

    特性：
    - 自动缓存目录管理
    - 缓存统计信息（命中率、大小等）
    - LRU 淘汰 + 容量上限（V2.0 新增，修复 BUG-H-02）
    - 智能清理（按时间或大小）
    - 线程安全的文件操作（V1.1 新增锁保护）

    使用示例：
        cache = ChartCache("./cache/charts")
        # 尝试获取缓存
        cached = cache.get(mermaid_code)
        if cached:
            return cached
        # 渲染并缓存
        result = render_mermaid_to_bytes(mermaid_code, "flowchart")
        if result:
            cache.set(mermaid_code, result.getvalue())
    """

    def __init__(self, cache_dir: str = "./cache/charts", max_size_mb: float = 500.0):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._max_size_bytes = int(max_size_mb * 1024 * 1024)  # 默认 500MB
        self._stats = {"hits": 0, "misses": 0, "total_size": 0}
        # 使用可重入锁：set() 持锁期间会调用 _evict_if_needed() 再次加锁，
        # 非重入的 threading.Lock 会自死锁（V2.0 LRU 淘汰引入的回归缺陷）
        self._lock = threading.RLock()  # V1.1 新增：线程安全锁
        # V2.0 新增：LRU 访问顺序追踪 {cache_key: mtime}
        self._access_order: dict[str, float] = {}
        # BUG-FIX 2026-09-14（中）：进程重启后 _access_order / total_size 归零，
        # 但磁盘上已有大量历史缓存文件 → _evict_if_needed 找不到任何可淘汰条目
        # 会立即 return，LRU 容量上限彻底失效，charts 目录无限增长直到磁盘写满。
        # 修复：构造时扫描目录重建索引，并顺带清理超期文件。
        self._rebuild_index_from_disk()
        try:
            removed = self.cleanup_old_files(max_age_days=7)
            if removed:
                logger.info("ChartCache: 启动时清理 %d 个过期缓存", removed)
        except Exception as _e:
            logger.debug("ChartCache 启动清理失败: %s", _e)

    def _rebuild_index_from_disk(self) -> None:
        """扫描缓存目录，重建 _access_order 与 total_size（进程重启后 LRU 不失效）"""
        try:
            total = 0
            for f in self.cache_dir.glob("*.*"):
                if f.name.endswith(".tmp"):
                    # 清理上次原子写残留的临时文件
                    try:
                        f.unlink()
                    except OSError:
                        pass
                    continue
                try:
                    st = f.stat()
                except OSError:
                    continue
                total += st.st_size
                key = f.name.rsplit(".", 1)[0]
                if key.endswith(".fmt"):
                    continue
                # 以 mtime 作为初始访问时间，保证淘汰顺序合理
                self._access_order[key] = st.st_mtime
                self._access_order[f"{key}.fmt"] = f.suffix.lstrip(".")
            self._stats["total_size"] = total
            logger.debug(
                "ChartCache: 重建索引完成，%d 个条目，%.1fMB",
                len([k for k in self._access_order if not k.endswith(".fmt")]),
                total / 1024 / 1024,
            )
        except Exception as _e:
            logger.debug("ChartCache 重建索引失败: %s", _e)

    def get(self, mermaid_code: str, fmt: str = "png", chart_type: str = "") -> Path | None:
        """获取缓存的图表

        Args:
            mermaid_code: Mermaid 代码或 JSON 数据字符串
            fmt: 图片格式（png/svg）
            chart_type: 图表类型（可选，拼入缓存 key 避免同 payload 不同类型互踩）

        Returns:
            缓存文件路径，未命中返回 None
        """
        # BUG修复：原 key 只含 payload MD5，同一 payload 以不同 chart_type 渲染会互踩缓存
        cache_key = hashlib.md5(f"{chart_type}:{mermaid_code}".encode()).hexdigest()
        cache_path = self.cache_dir / f"{cache_key}.{fmt}"
        if cache_path.exists():
            with self._lock:
                self._stats["hits"] += 1
                # BUG-FIX-49 修复：get() 命中时未刷新 _access_order 时间0 时间戳，
                # 导致频繁访问的热点缓存被 LRU 误判为"最久未访问"而错误淘汰。
                import time as _time
                self._access_order[cache_key] = _time.time()
                return cache_path
        # BUG-FIX 2026-09-14（低）：misses 计数原先在锁外自增，并发下计数丢失
        with self._lock:
            self._stats["misses"] += 1
        return None

    def set(
        self, mermaid_code: str, content: bytes, fmt: str = "png", chart_type: str = ""
    ) -> Path:
        """缓存图表

        Args:
            mermaid_code: Mermaid 代码或 JSON 数据字符串
            content: 图片二进制数据
            fmt: 图片格式
            chart_type: 图表类型（可选，与 get() 的 key 规则一致）

        Returns:
            缓存文件路径
        """
        import time

        # BUG修复：与 get() 一致，key 拼入 chart_type，避免同 payload 不同类型互踩
        cache_key = hashlib.md5(f"{chart_type}:{mermaid_code}".encode()).hexdigest()
        cache_path = self.cache_dir / f"{cache_key}.{fmt}"
        with self._lock:
            # V2.0 新增：LRU 淘汰 - 当总大小超限时，按访问最久顺序清理最早条目
            self._evict_if_needed(cache_key, len(content))
            # BUG-FIX 2026-09-14（高）：原实现直接 write_bytes 覆盖。
            # 预渲染（8 线程）与正文渲染会并发写同一 key，读者可能读到半截 / 0 字节 PNG，
            # 下游 PIL 校验失败被静默吞掉 → 文档缺图且无任何报错。
            # 修复：先写 .tmp 再 os.replace 原子替换（同目录保证原子性）。
            tmp_path = cache_path.with_suffix(cache_path.suffix + ".tmp")
            # BUG-FIX 2026-09-18（P2）：覆盖已存在 key 且内容字节数变化时，
            # 旧实现“已存在则不加不减”，total_size 与磁盘实际占用永久背离，
            # LRU 淘汰基于失真数字提前/延迟。现写盘前先记下旧文件真实大小，
            # 记账改为 减旧加新（必须在 os.replace 之前取旧尺寸）。
            _old_size = 0
            if cache_key in self._access_order:
                _old_fmt = self._access_order.get(f"{cache_key}.fmt")
                _old_path = (self.cache_dir / f"{cache_key}.{_old_fmt}") if _old_fmt else cache_path
                try:
                    _old_size = _old_path.stat().st_size
                except OSError:
                    _old_size = 0
            tmp_path.write_bytes(content)
            os.replace(tmp_path, cache_path)
            self._stats["total_size"] = max(0, self._stats["total_size"] - _old_size) + len(content)
            self._access_order[cache_key] = time.time()
            # 持久化格式元数据，供 _evict_if_needed 构造正确的文件路径（防止硬编码 .png 导致 SVG 条目找不到）
            self._access_order[f"{cache_key}.fmt"] = fmt
        logger.debug("ChartCache: 已缓存图表 %s (%s)", cache_key[:12], fmt)
        return cache_path

    def _evict_if_needed(self, new_key: str, new_size: int) -> None:
        """V2.0 新增：LRU 淘汰 - 按容量上限淘汰最久未访问的缓存条目"""
        while True:
            with self._lock:
                current_size = self._stats["total_size"] + new_size
                if current_size <= self._max_size_bytes:
                    return
                # 找到最久未访问的条目（排除当前要写入的 key 和 .fmt 元数据 key）
                oldest_key = None
                oldest_time = float("inf")
                for k, t in self._access_order.items():
                    # BUG-FIX-32 修复：排除 .fmt 元数据 key，避免误删格式信息
                    if k != new_key and not k.endswith(".fmt") and t < oldest_time:
                        oldest_time = t
                        oldest_key = k
                if oldest_key is None:
                    return
                # 删除最久未访问的缓存文件并更新状态
                # BUG 修复：原代码硬编码 `.png` 后缀，若某条目是 SVG 则 path 指向不存在的文件，
                # 且 total_size 不减，导致 LRU 判定持续偏高而误淘汰有效条目。
                oldest_fmt = self._access_order.get(oldest_key + ".fmt", "png")
                oldest_path = self.cache_dir / f"{oldest_key}.{oldest_fmt}"
                try:
                    if oldest_path.exists():
                        self._stats["total_size"] -= oldest_path.stat().st_size
                        oldest_path.unlink()
                except OSError:
                    pass
                del self._access_order[oldest_key]
                # BUG-FIX-32 修复：同步删除对应的 .fmt 元数据 key
                self._access_order.pop(f"{oldest_key}.fmt", None)

    def get_or_render(self, mermaid_code: str, chart_type: str, fmt: str = "png") -> BytesIO | None:
        """获取缓存或渲染新图表（线程安全，V1.1 新增）

        Args:
            mermaid_code: Mermaid 代码或 JSON 数据字符串
            chart_type: 图表类型
            fmt: 图片格式

        Returns:
            BytesIO of PNG image, or None if rendering fails
        """
        # BUG-FIX-14: 缓存 key 拼入 chart_type，与 get/set 方法一致
        # 原实现仅用 mermaid_code 的 MD5，导致同一 payload 不同 chart_type 缓存互踩
        cache_key = hashlib.md5(f"{chart_type}:{mermaid_code}".encode()).hexdigest()
        cache_path = self.cache_dir / f"{cache_key}.{fmt}"
        # BUG-FIX-15: 先在锁内检查缓存，未命中则释放锁再渲染，避免锁内渲染导致并发串行化
        with self._lock:
            cached_content = None
            if cache_path.exists():
                cached_content = cache_path.read_bytes()
                self._stats["hits"] += 1
            if cached_content is not None:
                return BytesIO(cached_content)
        # 锁外渲染（render_mermaid_to_bytes 可能耗时数秒到20秒）
        result = render_mermaid_to_bytes(mermaid_code, chart_type)
        if result:
            content = result.getvalue()
            # 渲染完成后再加锁写入
            # BUG-FIX-50 修复：原直接 write_bytes 未调用 _evict_if_needed，
            # 也未更新 _access_order 和 _stats["total_size"]，导致缓存可能
            # 超 500MB 上限且统计失真。改为调用 set() 方法的完整逻辑。
            self.set(mermaid_code, content, fmt=fmt, chart_type=chart_type)
            with self._lock:
                self._stats["misses"] += 1
            return BytesIO(content)
        with self._lock:
            self._stats["misses"] += 1
        return None

    def clear(self, max_age_days: int | None = None):
        """清理缓存

        Args:
            max_age_days: 仅清理超过 N 天的缓存；None 表示全部清理
        """
        if max_age_days is None:
            for f in self.cache_dir.glob("*.*"):
                try:
                    f.unlink()
                except Exception as _e:
                    logger.debug("[silent-except] mermaid_renderer.py: line 650 - %s", _e)
            # BUG-FIX-51 修复：原 clear() 删除磁盘文件但不重置 _access_order
            # 和 _stats["total_size"]，导致清空后 LRU 判定异常（total_size 仍为
            # 旧值，新缓存被错误淘汰）。必须重启进程才能恢复。
            with self._lock:
                self._access_order.clear()
                self._stats["total_size"] = 0
            logger.info("ChartCache: 已清空全部缓存")
        else:
            import time

            cutoff = time.time() - max_age_days * 86400
            count = 0
            for f in self.cache_dir.glob("*.*"):
                try:
                    if f.stat().st_mtime < cutoff:
                        freed = f.stat().st_size
                        f.unlink()
                        count += 1
                        # BUG-FIX-51: 同步更新内存状态
                        with self._lock:
                            cache_key = f.stem
                            self._access_order.pop(cache_key, None)
                            self._access_order.pop(f"{cache_key}.fmt", None)
                            self._stats["total_size"] = max(0, self._stats["total_size"] - freed)
                except Exception as _e:
                    logger.debug("[silent-except] mermaid_renderer.py: line 676 - %s", _e)
            logger.info("ChartCache: 已清理 %d 个过期缓存（>%d天）", count, max_age_days)

    def get_stats(self) -> dict:
        """获取缓存统计信息

        Returns:
            包含 hits, misses, hit_rate, file_count, total_size 的字典
        """
        # BUG-FIX 2026-09-18（P2）：glob 后逐个 stat 与并发淘汰/原子写存在竞态，
        # 文件可能在 stat 前被删 → FileNotFoundError 冒泡，监控端点 500；
        # 且 .tmp 残留被计入与内存记账口径不一致。现跳过 .tmp 并容忍消失文件。
        file_count = 0
        total_size = 0
        for _f in self.cache_dir.glob("*.*"):
            if _f.name.endswith(".tmp"):
                continue
            try:
                if _f.is_file():
                    file_count += 1
                    total_size += _f.stat().st_size
            except OSError:
                continue
        total_requests = self._stats["hits"] + self._stats["misses"]
        hit_rate = (self._stats["hits"] / total_requests * 100) if total_requests > 0 else 0.0

        return {
            "hits": self._stats["hits"],
            "misses": self._stats["misses"],
            "hit_rate": f"{hit_rate:.1f}%",
            "file_count": file_count,
            "total_size": total_size,
            "total_size_mb": round(total_size / (1024 * 1024), 2),
        }

    def cleanup_old_files(self, max_age_days: int = 7) -> int:
        """清理旧缓存文件

        Args:
            max_age_days: 清理超过 N 天的文件

        Returns:
            清理的文件数量
        """
        import time

        cutoff = time.time() - max_age_days * 86400
        count = 0
        for f in self.cache_dir.glob("*.*"):
            try:
                if f.stat().st_mtime < cutoff:
                    # BUG-FIX 2026-09-18（P2）：原删文件后不更新内存状态，
                    # _access_order 残留幽灵键、total_size 永久虚高，LRU 淘汰
                    # 判断与监控统计全部失真。对齐 cleanup_expired 的做法。
                    freed = f.stat().st_size
                    f.unlink()
                    count += 1
                    with self._lock:
                        cache_key = f.stem
                        self._access_order.pop(cache_key, None)
                        self._access_order.pop(f"{cache_key}.fmt", None)
                        self._stats["total_size"] = max(0, self._stats["total_size"] - freed)
            except Exception as _e:
                logger.debug("[silent-except] mermaid_renderer.py: cleanup_old_files - %s", _e)
        if count > 0:
            logger.info("ChartCache: 清理了 %d 个超过 %d 天的缓存文件", count, max_age_days)
        return count

def render_mermaid(mermaid_code: str, chart_type: str = "flowchart") -> str:
    """[DEPRECATED] 兼容接口：返回 Mermaid 源码（不实际渲染）。

    完整渲染请使用 render_mermaid_to_bytes()。
    本函数于 2026-10-11 标记为 deprecated，从未被生产代码调用——仅为
    向后兼容预留。保留到后续版本确认无外部调用者后移除。
    """
    import warnings
    warnings.warn(
        "render_mermaid() 已弃用（仅返回源码不做渲染），"
        "请使用 render_mermaid_to_bytes() 替代。",
        DeprecationWarning,
        stacklevel=2,
    )
    logger.warning(
        "[DEPRECATED] render_mermaid() 被调用——返回源码而非图片，调用方可能无法正常渲染图表"
    )
    return mermaid_code



# ============================================================
# 向后兼容 re-export（拆分前可用的所有内部符号）
# ============================================================
from .mermaid_common import (  # noqa: F401
    _build_gantt_marks,
    _draw_text_center,
    _fit_text_with_ellipsis,
    _image_font,
    _image_to_stream,
    _render_hq,
    _text_height,
    _text_width,
    _wrap_text,
)
from .mermaid_flowchart import (  # noqa: F401
    _DECISION_KEYWORDS,
    _extract_mermaid_node_label,
    _is_decision_label,
    _parse_flowchart_structure,
    _parse_mermaid_flowchart,
    _register_node_if_match,
    _render_flowchart_image_v2,
    _render_flowchart_with_edges,
    _split_mermaid_edge,
    _strip_mermaid_node_brackets,
    flowchart_json_to_mermaid,
)
from .mermaid_gantt import (  # noqa: F401
    _parse_mermaid_gantt,
    _render_gantt_image_v2,
    _render_gantt_with_dependencies,
    gantt_json_to_mermaid,
)
from .mermaid_architecture import (  # noqa: F401
    _layout_architecture_tree,
    _render_architecture_image_v2,
)
from .mermaid_comparison import (  # noqa: F401
    _parse_mermaid_pie,
    _parse_mermaid_xychart,
    _render_comparison_image_v2,
)
from .mermaid_labor import (  # noqa: F401
    _render_labor_image_v2,
)
from .mermaid_layout import (  # noqa: F401
    _render_layout_image_v2,
    _repair_layout_data,
)
from .mermaid_timeline import (  # noqa: F401
    _render_milestone_flowchart_image_v2,
    _render_timeline_image_v2,
    _repair_timeline_data,
)

