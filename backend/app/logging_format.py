"""统一结构化日志（JSON）与标准化错误/步骤埋点（P2，2026-10-18）。

目标（对应审计 §4 日志与可观测性增强）：
1. **结构化日志**：提供 `JsonFormatter`，把每条日志输出为单行 JSON，便于采集/检索/聚合；
   当前默认仍为既有文本格式，需通过环境变量 `LOG_JSON=1` 显式开启，**默认保持现有行为、向后兼容**。
2. **错误标准化**：`log_error()` 统一输出 `error_code` / `error_layer` / `retryable` 字段，
   避免各模块自行拼接、分类口径不一（此前存在 `error=None` 掩盖根因的情况）。
3. **Step log**：`log_step()` 供**模型调用 / 工具调用 / 命令执行**写步骤日志，
   统一携带 `step`、耗时 `duration_ms`、结果状态 `status` 与链路标识。
4. **敏感信息脱敏**：`desensitize()` 对 api_key / password / token / secret / authorization
   等键名统一掩码，防止明文入日志。

设计约束：
- 本模块**只依赖标准库**，不得反向 import 业务模块，避免循环导入；
- 所有函数对异常保持宽容（日志本身绝不能抛异常影响主流程）。
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

#: 视为敏感、需脱敏的键名子串（小写匹配）
SENSITIVE_KEY_HINTS: tuple[str, ...] = (
    "api_key",
    "apikey",
    "password",
    "passwd",
    "token",
    "secret",
    "authorization",
    "access_key",
    "private_key",
)

#: 脱敏掩码
MASK = "***已配置***"


def is_sensitive_key(key: str) -> bool:
    """键名是否命中敏感特征。"""
    k = str(key or "").lower()
    return any(hint in k for hint in SENSITIVE_KEY_HINTS)


def desensitize(value: Any, _depth: int = 0) -> Any:
    """递归脱敏：命中敏感键名的值替换为掩码；dict/list 递归处理。

    仅处理键名（不判断值内容），避免误伤正常长文本。
    """
    if _depth > 6:  # 防御超深嵌套
        return "<deep>"
    if isinstance(value, dict):
        return {
            k: (MASK if is_sensitive_key(k) else desensitize(v, _depth + 1))
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [desensitize(v, _depth + 1) for v in value]
    return value


def _json_default(o: Any) -> str:
    """JSON 兜底序列化：不可序列化对象转 repr。"""
    try:
        return repr(o)
    except Exception:
        return "<unserializable>"


class JsonFormatter(logging.Formatter):
    """单行 JSON 日志格式器。

    输出字段：
      ts（ISO8601 秒级）、level、logger、file、line、msg、rid、pid、sid、tid，
      以及 `extra` 中所有自定义字段（如 step / duration_ms / error_* ）。
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(record.created)),
            "level": record.levelname,
            "logger": record.name,
            "file": record.filename,
            "line": record.lineno,
            "msg": record.getMessage(),
            "rid": getattr(record, "request_id", "-"),
            "pid": getattr(record, "project_id", "-"),
            "sid": getattr(record, "section_id", "-"),
            "tid": getattr(record, "task_id", "-"),
        }
        # 业务侧通过 extra=... 传入的自定义字段
        for key, value in record.__dict__.items():
            if key.startswith("_") or key in payload:
                continue
            if key in (
                "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
                "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
                "created", "msecs", "relativeCreated", "thread", "threadName",
                "processName", "process", "getMessage", "message", "taskName",
            ):
                continue
            payload[key] = desensitize(value)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        try:
            return json.dumps(payload, ensure_ascii=False, default=_json_default)
        except Exception:
            return json.dumps({"msg": payload.get("msg", "<log-format-error>")}, ensure_ascii=False)


def _default_logger() -> logging.Logger:
    return logging.getLogger("app")


def log_step(step: str, status: str = "ok", **fields: Any) -> None:
    """步骤日志：模型调用 / 工具调用 / 命令执行统一入口。

    Args:
        step: 步骤标识，如 `ai_call` / `tool_call` / `cmd_exec`。
        status: 步骤结果，如 `ok` / `failed` / `skipped`。
        **fields: 附加字段（如 provider、model、tokens、duration_ms）。
    """
    logger = _default_logger()
    extra: dict[str, Any] = {"step": step, "status": status}
    extra.update(desensitize(fields) if isinstance(fields, dict) else {})
    try:
        logger.info("[step] %s %s", step, status, extra=extra)
    except Exception:
        pass


def log_error(
    message: str,
    exc: BaseException | None = None,
    *,
    error_code: str = "UNKNOWN",
    layer: str = "UNSPECIFIED",
    retryable: bool = False,
    **fields: Any,
) -> None:
    """标准化错误日志：强制带 error_code / error_layer / retryable。

    Args:
        message: 人类可读的错误描述。
        exc: 原始异常（可选）；提供时会附带异常栈。
        error_code: 稳定错误码（如 `AI_UNAVAILABLE`、`DB_LOCKED`）。
        layer: 出错层（如 `service` / `router` / `ai` / `db`）。
        retryable: 是否可重试。
    """
    logger = _default_logger()
    extra: dict[str, Any] = {
        "error_code": error_code,
        "error_layer": layer,
        "retryable": retryable,
    }
    extra.update(desensitize(fields) if isinstance(fields, dict) else {})
    try:
        logger.error("%s | %s", message, error_code, extra=extra, exc_info=exc is not None)
    except Exception:
        pass


class StepTimer:
    """轻量耗时统计器，配合 `log_step` 使用。

    用法：
        with StepTimer() as t:
            do_something()
        log_step("ai_call", status="ok", duration_ms=t.elapsed_ms())
    """

    def __init__(self) -> None:
        self._start = 0.0
        self._end = 0.0
        self._used = False

    def __enter__(self) -> "StepTimer":
        self._start = time.perf_counter()
        self._used = True
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self._end = time.perf_counter()
        return False

    def elapsed_ms(self) -> float:
        if not self._used:
            return 0.0
        end = self._end or time.perf_counter()
        return round((end - self._start) * 1000, 2)


def perf_step(step: str, logger: logging.Logger | None = None, **fields: Any):
    """装饰器：自动记录函数耗时 step log（异常时记 failed 并保留原异常）。

    适用于模型调用、工具调用、命令执行等需要时延埋点的同步函数。
    """
    import functools

    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            lg = logger or _default_logger()
            t0 = time.perf_counter()
            try:
                result = fn(*args, **kwargs)
                lg.info(
                    "[step] %s ok", step,
                    extra={"step": step, "status": "ok",
                           "duration_ms": round((time.perf_counter() - t0) * 1000, 2),
                           **desensitize(fields)},
                )
                return result
            except BaseException as e:  # noqa: BLE001
                lg.error(
                    "[step] %s failed: %s", step, e,
                    extra={"step": step, "status": "failed",
                           "error_code": "STEP_FAILED", "error_layer": "service",
                           "retryable": False,
                           "duration_ms": round((time.perf_counter() - t0) * 1000, 2),
                           **desensitize(fields)},
                    exc_info=True,
                )
                raise

        return deco

    return deco


def now_ms() -> int:
    """当前时间戳（毫秒）。"""
    return int(time.time() * 1000)
