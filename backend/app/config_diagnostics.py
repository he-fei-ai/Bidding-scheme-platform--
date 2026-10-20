"""运行期配置自诊断（P0/P1 配置缺口可观测化，2026-10-18 第三轮）。

背景：审计发现三处高频告警的根因并非代码缺陷，而是**部署配置缺失**：
  1. `AGNES_API_KEY` 未配置 → 文本/配图 AI 全链路 401（P0）；
  2. OCR 引擎（`OCR_ENGINE=mineru`）未安装 → 扫描件不识别（P1）；
  3. Mermaid HTTP 渲染服务未部署 → 持续 WARNING 噪声（P1）。

这些缺口此前只能从散落的 `401 / 不可用 / 渲染失败` 日志里人工拼出，定位成本高。
本模块把它们**收敛成一个结构化清单**，供健康检查/运维看板直接消费，并给出明确的
补齐动作（hint）。

设计约束：
- **纯只读探测**：不写库、不发外部请求、不改动任何配置；
- **绝不抛异常**：每个探测项独立 try/except，探测失败只降级为 `unknown`；
- **不落敏感信息**：只报告"是否配置"，**绝不回显密钥原文**；
- **默认不影响现有行为**：仅提供数据，由健康检查端点决定是否暴露。
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any

logger = logging.getLogger(__name__)

#: 探测项状态常量
STATUS_OK = "ok"
STATUS_MISSING = "missing"
STATUS_UNKNOWN = "unknown"


def _env_flag(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


def _safe_get(fn, *args, **kwargs) -> tuple[Any, str | None]:
    """执行探测；返回 (值, 错误信息)。任何异常都只降级为 unknown。"""
    try:
        return fn(*args, **kwargs), None
    except Exception as e:  # noqa: BLE001
        return None, f"{type(e).__name__}: {e}"


def _probe_text_model() -> dict[str, Any]:
    """文本模型：检查是否有可用 provider（仅判断存在性，不泄露密钥）。"""
    try:
        from .services.ai import provider_factory as _pf
    except Exception:  # noqa: BLE001
        return {"key": "text_model", "status": STATUS_UNKNOWN,
                "message": "无法加载 provider_factory", "hint": "检查 app.services.ai 导入"}

    try:
        cfg = _pf.get_ai_config() if hasattr(_pf, "get_ai_config") else {}
    except Exception as e:  # noqa: BLE001
        cfg = {}
        logger.debug("text_model 配置读取失败: %s", e)

    api_key = (cfg or {}).get("api_key") or os.getenv("AGNES_API_KEY", "")
    provider = (cfg or {}).get("provider_name") or (cfg or {}).get("provider") or "agnes"
    if api_key:
        return {"key": "text_model", "status": STATUS_OK,
                "message": f"已配置文本模型 provider={provider}",
                "hint": ""}
    return {
        "key": "text_model",
        "status": STATUS_MISSING,
        "severity": "P0",
        "layer": "config",
        "message": f"未配置文本模型密钥（provider={provider}）",
        "hint": "设置环境变量 AGNES_API_KEY=<密钥>，或在前端「设置」页配置对应 provider 后保存落库",
    }


def _probe_image_model() -> dict[str, Any]:
    """配图模型：检查是否已注册并配置图像 provider。"""
    try:
        from .services.ai.image_providers.registry import ProviderRegistry
    except Exception:  # noqa: BLE001
        return {"key": "image_model", "status": STATUS_UNKNOWN,
                "message": "无法加载图像 provider 注册表", "hint": ""}
    try:
        names = ProviderRegistry.list_providers() or []
    except Exception:  # noqa: BLE001
        names = []
    if names:
        return {"key": "image_model", "status": STATUS_OK,
                "message": f"已注册图像 provider: {', '.join(map(str, names))}",
                "hint": ""}
    return {
        "key": "image_model",
        "status": STATUS_MISSING,
        "severity": "P1",
        "layer": "config",
        "message": "无可用图像 provider（配图将全部失败并降级）",
        "hint": "在「设置-图像模型」配置至少一个 provider 的密钥与端点",
    }


def _probe_ocr() -> dict[str, Any]:
    """OCR 引擎：比较声明值与实际可用性。"""
    engine = (os.getenv("OCR_ENGINE", "mineru") or "mineru").strip().lower()
    if engine in ("none", "off", "disabled"):
        return {"key": "ocr", "status": STATUS_OK,
                "message": f"OCR 已按配置关闭（OCR_ENGINE={engine}）", "hint": ""}

    available: list[str] = []
    for mod, label in (
        ("paddleocr", "paddleocr"),
        ("pytesseract", "tesseract"),
    ):
        try:
            __import__(mod)
            available.append(label)
        except Exception:  # noqa: BLE001
            continue
    try:
        import shutil

        if shutil.which("mineru"):
            available.append("mineru")
    except Exception:  # noqa: BLE001
        pass

    if available:
        return {"key": "ocr", "status": STATUS_OK,
                "message": f"OCR 可用引擎: {', '.join(available)}", "hint": ""}
    return {
        "key": "ocr",
        "status": STATUS_MISSING,
        "severity": "P1",
        "layer": "config",
        "message": f"OCR 引擎不可用（OCR_ENGINE={engine}），扫描件/图片将无法识别",
        "hint": "安装任一 OCR 引擎（mineru / paddleocr / tesseract）并确保可执行文件在 PATH",
    }


def _probe_mermaid_service() -> dict[str, Any]:
    """Mermaid HTTP 渲染服务：检查是否已配置服务地址。"""
    url = (
        os.getenv("MERMAID_SERVICE_URL")
        or os.getenv("MERMAID_HTTP_URL")
        or os.getenv("MERMAID_RENDER_URL")
        or ""
    )
    if url:
        return {"key": "mermaid_service", "status": STATUS_OK,
                "message": f"已配置 Mermaid 渲染服务 {url}", "hint": ""}
    return {
        "key": "mermaid_service",
        "status": STATUS_MISSING,
        "severity": "P1",
        "layer": "config",
        "message": "未配置 Mermaid HTTP 渲染服务，图表已降级为 PIL v2（质量略降、产生 WARNING 噪声）",
        "hint": "部署 Mermaid 渲染服务（Docker/Railway/mmdc）并配置 MERMAID_SERVICE_URL",
    }


#: 探测项注册表（顺序即输出顺序）
_PROBES = (
    _probe_text_model,
    _probe_image_model,
    _probe_ocr,
    _probe_mermaid_service,
)


def collect_config_gaps() -> dict[str, Any]:
    """收集运行期配置缺口，返回结构化清单。

    Returns:
        {
          "generated_at": "YYYY-mm-dd HH:MM:SS",
          "overall": "ok" | "degraded",
          "items": [ {key, status, severity?, layer?, message, hint}, ... ],
          "counts": {"P0": int, "P1": int, "P2": int},
        }

    设计要点：
      - 任一探测项自身异常都不影响其他项，也不会向上抛出；
      - `severity` 仅在 `status == "missing"` 时给出；
      - 不返回任何密钥/令牌原文。
    """
    items: list[dict[str, Any]] = []
    for probe in _PROBES:
        value, err = _safe_get(probe)
        if err is not None:
            items.append(
                {
                    "key": getattr(probe, "__name__", "unknown").replace("_probe_", ""),
                    "status": STATUS_UNKNOWN,
                    "layer": "diagnostics",
                    "message": f"探测项执行失败: {err}",
                    "hint": "",
                }
            )
        elif isinstance(value, dict):
            items.append(value)
        else:
            items.append({"key": "unknown", "status": STATUS_UNKNOWN,
                          "message": "探测项返回格式异常", "hint": ""})

    counts = {"P0": 0, "P1": 0, "P2": 0}
    for it in items:
        if it.get("status") == STATUS_MISSING:
            sev = it.get("severity", "P2")
            counts[sev] = counts.get(sev, 0) + 1

    overall = "ok" if counts["P0"] == 0 and counts["P1"] == 0 else "degraded"
    return {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
        "overall": overall,
        "items": items,
        "counts": counts,
    }


def log_config_gaps(gaps: dict[str, Any] | None = None) -> dict[str, Any]:
    """把配置缺口写入日志（启动时/按需调用）。

    - 缺失项按 P0/P1 用 ERROR/WARNING 输出；
    - 输出精简摘要行，避免刷屏；
    - 不输出密钥原文。
    """
    gaps = gaps if gaps is not None else collect_config_gaps()
    try:
        for it in gaps.get("items", []):
            if it.get("status") != STATUS_MISSING:
                continue
            sev = it.get("severity", "P2")
            log_fn = logger.error if sev == "P0" else logger.warning
            log_fn(
                "[配置缺口][%s] %s | 补齐动作: %s",
                sev,
                it.get("message", ""),
                it.get("hint", ""),
            )
        logger.info(
            "[配置自诊断] overall=%s P0=%d P1=%d P2=%d",
            gaps.get("overall"),
            gaps.get("counts", {}).get("P0", 0),
            gaps.get("counts", {}).get("P1", 0),
            gaps.get("counts", {}).get("P2", 0),
        )
    except Exception as e:  # noqa: BLE001
        logger.debug("[silent-except] config_diagnostics.py: log_config_gaps - %s", e)
    return gaps
