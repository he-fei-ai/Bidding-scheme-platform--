"""illustration_phase.py — 配图阶段（标书智能体（6）图文控制对齐）。

外部引用软件《超长文本生成和图文控制》定义的三段式工作流：

    ① 预编排（正文生成前）：AI 对全文目录提名「本节要不要表格 / Mermaid /
       数据型图表 / AI 生图」以及优先级（3=有价值 4=推荐 5=强推荐）；
    ② 正文生成（只写正文）：正文提示词**严禁输出图表代码块**，配图由系统另行处理；
       编排结果（配图意图）传给正文模型 → 正文提前为图铺垫，避免「图文两张皮」；
    ③ 配图阶段（正文全部完成后统一执行）：校验 → 必要时修复 → 渲染 → 追加正文。

本模块实现第 ③ 步，并遵守两条铁律：

  * **失败成本隔离**：图与文字的失败成本不同——正文失败是真失败，配图失败
    不应影响正文结果，最多提示「这张图没生成成功，正文已保留」；
  * **AI 提名、程序拍板**：最终采用的图表类型由程序裁定（illustration_type），
    候选多、额度少时按优先级分段择优（分段逻辑在 workflows_orchestration
    的 _adjudicate_illustrations 中），本模块只执行已裁定的结论。

围栏格式与既有链路完全一致（```mermaid / ```chart-json），因此：
  页面预览（前端本地渲染）→ 正文存储（保存代码块）→ Word 导出（转图片插入）
的执行链路无需改动。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

logger = logging.getLogger(__name__)


#: 插图风格固定要求（外部文档口径：避免「宣传海报风 / 科技蓝大屏风」）。
#: 拼接在编排阶段产出的 image.prompt 之后。
IMAGE_STYLE_SUFFIX = {
    "engineering_diagram": (
        "画面采用工程项目图示风格，结构清晰、专业克制、适合专项施工方案插图。"
        "避免出现品牌标识、水印、夸张营销元素和无关文字。"
    ),
    "realistic_photo": (
        "画面采用专业实景照片风格，真实、克制、适合专项施工方案插图。"
        "避免出现品牌标识、水印、夸张营销元素和无关文字。"
    ),
}


@dataclass
class IllustrationTarget:
    """一个待执行的配图目标（由预编排 plan + 程序裁定结果装配）。"""

    section_id: int
    title: str
    kind: str                 # mermaid | chart | image
    priority: int = 3
    payload: dict[str, Any] = field(default_factory=dict)
    reason: str = ""


# ============================================================
# 目标装配
# ============================================================

def _block_priority(block: dict) -> int:
    """取 plan 块的归一化优先级（3-5 制；缺失按 3 处理）。"""
    try:
        from .workflows_orchestration import normalize_priority

        return normalize_priority((block or {}).get("priority"), needed=True)
    except Exception:  # pragma: no cover - 防御性
        try:
            return max(3, min(5, int((block or {}).get("priority") or 3)))
        except (TypeError, ValueError):
            return 3


def collect_illustration_targets(
    leaf_dicts: Iterable[dict],
    max_charts: int | None = None,
) -> list[IllustrationTarget]:
    """从编排后的叶子字典里装配配图目标。

    Args:
        leaf_dicts: 已写入 plan / illustration_type 的叶子字典列表
            （apply_orchestration_to_leaves 的产物）。
        max_charts: 全文配图硬上限；0 或 None = 不限；超出时按优先级降序择优，
            再恢复目录顺序输出（避免前面章节把额度用完）。

    Returns:
        list[IllustrationTarget]
    """
    targets: list[IllustrationTarget] = []
    for leaf in leaf_dicts or []:
        if not isinstance(leaf, dict):
            continue
        plan = leaf.get("plan") or {}
        if not isinstance(plan, dict):
            continue
        ill = str(leaf.get("illustration_type") or "none").strip().lower()
        if ill in ("", "none"):
            continue
        kind = "image" if ill == "ai" else ill
        if kind not in ("mermaid", "chart", "image"):
            continue
        try:
            section_id = int(leaf.get("id"))
        except (TypeError, ValueError):
            continue
        block = plan.get(kind) or {}
        if not isinstance(block, dict):
            block = {}
        payload: dict[str, Any] = {}
        if kind == "mermaid":
            payload = {
                "title": str(block.get("title") or leaf.get("title") or ""),
                "code": str(block.get("code") or ""),
            }
        elif kind == "chart":
            payload = {
                "title": str(block.get("title") or leaf.get("title") or ""),
                "chart_type": str(block.get("type") or ""),
                "data": block.get("data") or {},
            }
        else:
            payload = {
                "title": str(block.get("title") or leaf.get("title") or ""),
                "style": str(block.get("style") or ""),
                "prompt": str(block.get("prompt") or ""),
            }
        targets.append(
            IllustrationTarget(
                section_id=section_id,
                title=str(leaf.get("title") or ""),
                kind=kind,
                priority=_block_priority(block),
                payload=payload,
                reason=str(block.get("reason") or ""),
            )
        )

    if max_charts:
        limit = int(max_charts)
        if limit > 0 and len(targets) > limit:
            # 按优先级降序择优（同优先级保持目录顺序），再恢复目录顺序输出
            order = sorted(
                range(len(targets)),
                key=lambda i: (-targets[i].priority, i),
            )
            keep = sorted(order[:limit])
            targets = [targets[i] for i in keep]
    return targets


# ============================================================
# 载荷构建 / 校验
# ============================================================

def build_mermaid_payload(code: str, title: str) -> dict[str, Any]:
    """构建 mermaid 载荷（与 chart_auto_orchestration.build_chart_fence 同构）。"""
    return {"type": "flowchart", "title": title or "流程图", "code": code}


def build_chart_payload(chart_type: str, title: str, data: dict) -> dict[str, Any]:
    """构建数据型图表载荷（type + title + 真实数据）。"""
    payload: dict[str, Any] = {"type": chart_type, "title": title or "图表"}
    for k, v in (data or {}).items():
        if k in ("type", "title"):
            continue
        payload[k] = v
    return payload


def compose_image_prompt(prompt: str, style: str) -> str:
    """拼固定风格要求（外部文档口径：生图模型容易出海报风）。"""
    base = str(prompt or "").strip()
    suffix = IMAGE_STYLE_SUFFIX.get(style, "")
    if not base:
        return ""
    return f"{base}\n{suffix}" if suffix else base


async def _ai_repair_mermaid(code: str, provider: Any, title: str) -> str:
    """用 AI 修复不可渲染的 Mermaid 代码（失败返回空串）。

    修复提示词要点（外部文档）：优先 flowchart TD、节点 ID 只用 ASCII 字母数字
    下划线、中文标签写成 A["中文标签"]、不用多节点连接简写、不用分号、
    不输出代码围栏、结构过复杂时简化为可渲染的核心流程图。
    """
    if not provider or not code:
        return ""
    try:
        from .mermaid_repair import repair_mermaid_with_ai
        from .providers.base import AIMessage

        async def _ai_call(messages: list) -> str:
            converted = [
                AIMessage(role=str(m.get("role") or "user"), content=str(m.get("content") or ""))
                for m in messages
            ]
            raw = await provider.chat(converted, max_tokens=1200, temperature=0.2)
            return raw if isinstance(raw, str) else str((raw or {}).get("content") or "")

        result = await repair_mermaid_with_ai(code, _ai_call)
        if getattr(result, "success", False) and getattr(result, "result", None):
            return str(result.result)
        return ""
    except Exception as e:
        logger.warning("[配图阶段] Mermaid AI 修复失败 title=%s: %s", title, e)
        return ""


async def _validate_mermaid_payload(
    code: str,
    title: str,
    provider: Any = None,
) -> tuple[bool, str, str]:
    """校验 Mermaid 代码；返回 (是否通过, 错误摘要, 修复后代码)。

    校验口径复用既有闸门 validate_chart_for_ai_pipeline（内含规则修复 +
    语法校验 + 结构化完整性校验），失败时再走一次 AI 修复（外部文档要求：
    「编排阶段返回的 code 不能直接相信，先校验能否渲染，不能渲染再让 AI 修复」）。
    """
    try:
        from .image_engine import validate_chart_for_ai_pipeline

        payload = build_mermaid_payload(code, title)
        ok, errors, fixed, _mutated, _ctx = validate_chart_for_ai_pipeline(
            "flowchart", payload, verbose=False, section_title=title
        )
        if ok:
            return True, "", str((fixed or {}).get("code") or code)
        err = "; ".join(str(e) for e in (errors or [])[:3]) or "validation_failed"
    except Exception as e:  # 校验器异常不得炸穿配图阶段
        err = f"validator_exception: {e}"

    repaired = await _ai_repair_mermaid(code, provider, title)
    if repaired:
        try:
            from .image_engine import validate_chart_for_ai_pipeline

            payload = build_mermaid_payload(repaired, title)
            ok, errors, fixed, _mutated, _ctx = validate_chart_for_ai_pipeline(
                "flowchart", payload, verbose=False, section_title=title
            )
            if ok:
                return True, "", str((fixed or {}).get("code") or repaired)
            err = "; ".join(str(e) for e in (errors or [])[:3]) or err
        except Exception as e:
            err = f"{err}; revalidate_exception: {e}"
    return False, err, ""


def _validate_chart_payload(chart_type: str, data: dict) -> tuple[bool, str]:
    """校验数据型图表载荷（复用统一闸门）。"""
    if not chart_type or not isinstance(data, dict) or not data:
        return False, "chart_payload_empty"
    try:
        from .image_engine import validate_chart_payload

        ok, errors = validate_chart_payload(chart_type, data)
        if ok:
            return True, ""
        return False, "; ".join(str(e) for e in (errors or [])[:3])
    except Exception as e:
        return False, f"validator_exception: {e}"


# ============================================================
# 配图阶段执行
# ============================================================

async def execute_illustration_phase(
    project_id: int,
    targets: list[IllustrationTarget],
    *,
    provider: Any = None,
    ai_client: Any = None,
    get_section: Callable[[int], dict] | None = None,
    update_section: Callable[[int, Any], Any] | None = None,
    progress_callback: Callable[[dict], Any] | None = None,
) -> dict[str, Any]:
    """执行配图阶段：校验 → 修复 → 插入正文 → 落库。

    失败隔离：单图失败只跳过该图（不占图号），正文内容不受影响。

    Args:
        project_id: 项目 ID（仅用于日志）。
        targets: collect_illustration_targets 的产物。
        provider: AI 提供商（用于 Mermaid AI 修复；None 时跳过修复）。
        ai_client: 兼容参数（与既有图表编排接口保持同名，便于调用方透传）。
        get_section / update_section: 依赖注入点（默认用 services.sections）。
        progress_callback: 进度回调（每处理完一个目标调用一次）。

    Returns:
        结果字典（可 JSON 序列化）
    """
    from ...models import SectionUpdate
    from ..chart_auto_orchestration import (
        build_chart_fence,
        content_has_chart_fence,
        insert_fence_at_paragraph,
    )
    from ..sections import get_section as _get_section
    from ..sections import update_section as _update_section

    get_sec = get_section or _get_section
    upd_sec = update_section or _update_section

    result: dict[str, Any] = {
        "ok": True,
        "project_id": project_id,
        "total": len(targets or []),
        "inserted": 0,
        "skipped": 0,
        "failed": 0,
        "items": [],
    }

    for target in targets or []:
        item: dict[str, Any] = {
            "section_id": target.section_id,
            "title": target.title,
            "kind": target.kind,
            "priority": target.priority,
            "status": "skipped",
            "reason": "",
        }
        try:
            section = get_sec(target.section_id)
        except Exception as e:
            item["reason"] = f"get_section_failed: {e}"
            result["items"].append(item)
            result["skipped"] += 1
            continue
        if not section:
            item["reason"] = "section_not_found"
            result["items"].append(item)
            result["skipped"] += 1
            continue

        content = str(section.get("content") or "")
        # 去重：正文已有图表围栏时不再插入（防重入 / 防图号虚跳）
        if content_has_chart_fence(content):
            item["reason"] = "chart_fence_exists"
            result["items"].append(item)
            result["skipped"] += 1
            continue

        try:
            if target.kind == "mermaid":
                ok, err, code = await _validate_mermaid_payload(
                    str(target.payload.get("code") or ""),
                    str(target.payload.get("title") or target.title),
                    provider=provider,
                )
                if not ok or not code:
                    item["reason"] = f"mermaid_invalid: {err}"
                    result["failed"] += 1
                    result["items"].append(item)
                    continue
                payload = build_mermaid_payload(
                    code, str(target.payload.get("title") or target.title)
                )
                lead_in, fence = build_chart_fence("flowchart", payload)
                new_content = _insert(
                    insert_fence_at_paragraph, content, fence, lead_in,
                    target.title, "flowchart",
                )
            elif target.kind == "chart":
                ctype = str(target.payload.get("chart_type") or "")
                data = target.payload.get("data") or {}
                ok, err = _validate_chart_payload(ctype, data)
                if not ok:
                    item["reason"] = f"chart_invalid: {err}"
                    result["failed"] += 1
                    result["items"].append(item)
                    continue
                payload = build_chart_payload(
                    ctype, str(target.payload.get("title") or target.title), data
                )
                lead_in, fence = build_chart_fence(ctype, payload)
                new_content = _insert(
                    insert_fence_at_paragraph, content, fence, lead_in,
                    target.title, ctype,
                )
            else:  # image（AI 生图默认关闭，见 config 口径）
                item["reason"] = "ai_image_disabled"
                result["skipped"] += 1
                result["items"].append(item)
                continue

            if not new_content or new_content == content:
                item["reason"] = "content_unchanged"
                result["skipped"] += 1
                result["items"].append(item)
                continue

            upd_sec(target.section_id, SectionUpdate(content=new_content))
            item["status"] = "inserted"
            item["fence"] = fence
            result["inserted"] += 1
            result["items"].append(item)
            logger.info(
                "[配图阶段] 已插入图表围栏 project=%s section=%s kind=%s",
                project_id, target.section_id, target.kind,
            )
        except Exception as e:
            item["status"] = "failed"
            item["reason"] = f"exception: {e}"
            result["failed"] += 1
            logger.warning(
                "[配图阶段] 配图失败（不影响正文） project=%s section=%s kind=%s: %s",
                project_id, target.section_id, target.kind, e,
            )
        finally:
            if progress_callback is not None:
                try:
                    progress_callback(dict(item))
                except Exception:  # pragma: no cover - 进度回调不得影响主流程
                    pass

    return result


def _insert(
    insert_fn,
    content: str,
    fence: str,
    lead_in: str,
    title: str,
    chart_type: str,
) -> str:
    """按图表类型推荐位置插入围栏（复用既有位置打分逻辑）。"""
    try:
        from .chart_orchestrator import suggest_insertion_position

        idx = suggest_insertion_position(content, chart_type, title)
    except Exception:  # pragma: no cover - 位置打分兜底
        idx = -1
    if idx < 0:
        idx = 0
    return insert_fn(content, fence, lead_in, idx)
