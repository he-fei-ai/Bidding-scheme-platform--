"""统一降级埋点（DEGRADATION 2026-11-04）。

背景：全仓存在大量 ``except Exception: pass`` / ``return 默认值`` 式静默兜底，
运维与前端无法区分「正常空结果」与「能力降级」。本模块把这类兜底统一为
**可观测降级**：

1. 标准 ``logging.WARNING``（固定 ``[降级]`` 前缀，便于 grep 与 caplog 断言）；
2. 结构化事件 ``degrade``（``app.observability.log_degrade``：JSON 落文件，
   并在 ``DB_WRITE_EVENTS`` 中时入库，供 /observability 查询）。

约束：
- 埋点失败**绝不影响主流程**（两路都吞异常）；
- 不改变任何业务返回值/行为，只补可观测性；
- 上下文只放摘要（文件名、引擎名、章节 id、长度等），不放正文全文。
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def log_degradation(
    module: str,
    *,
    trigger: str,
    action: str,
    reason: str = "",
    logger_obj: logging.Logger | None = None,
    **context: Any,
) -> None:
    """记录一次降级。

    Args:
        module: 模块标识（parse/export/content/consistency/ai_config/chart/...）。
        trigger: 触发条件（如 ``OCR 图片超限``）。
        action: 降级后的实际行为（如 ``跳过 OCR，返回空文本``）。
        reason: 原因摘要（调用方负责截断异常文本）。
        logger_obj: 调用方 logger；缺省用本模块 logger。
        context: 额外上下文（键值会拼进日志，并作为结构化事件字段）。
    """
    detail = " ".join(f"{k}={v}" for k, v in context.items() if v not in (None, ""))
    msg = f"[降级] {trigger} -> {action}"
    if reason:
        msg += f" | 原因: {reason}"
    if detail:
        msg += f" | {detail}"
    try:
        (logger_obj or logger).warning(msg)
    except Exception:
        pass
    try:
        from ..observability import log_degrade

        log_degrade(
            module=module,
            from_mode=trigger,
            to_mode=action,
            reason=reason,
            message=msg,
            **context,
        )
    except Exception:
        pass
