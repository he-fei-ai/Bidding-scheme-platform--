# -*- coding: utf-8 -*-
"""专项方案解析提取 Runner（标书智能体（一）引入的五个提取任务）。

对应「引入方案」第五节五个提取任务：工程概况 / 专项方案类型 / 危大工程判定参数 /
编制依据清单 / 施工条件。

实现要点（与 analysis 模块既有口径一致，便于复用与维护）：
- 消息结构沿用「缓存友好」四段式：通用 system 前缀（ANALYSIS_SYSTEM，跨任务一致）
  + 文档全文独占 user 块（ANALYSIS_CONTEXT_BLOCK，大且稳定 → 命中 Prompt Cache）
  + 任务专属指令（*_SYSTEM）+ 任务输入（*_USER）；
- JSON 提取复用 ``collect_json_response``，自动处理 json_mode 超时/参数错误降级，
  无需自造重试（引入方案 BUG 修复重点 #7）；
- 全文注入经 ``sanitize`` + ``desensitize``，防止元标记注入与 PII 出网。
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from datetime import datetime
from typing import Any

from .prompts import get_prompt, truncate_to_tokens
from .prompts.scheme_extraction import SCHEME_EXTRACTION_TASKS
from .providers.base import AIMessage
from .workflows_base import collect_json_response


logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 结构化日志与耗时指标（ENHANCE 2026-10-26）
#
# 背景：五类专项任务并发执行且单任务失败被隔离，此前只有「开始任务 / 任务失败」
# 两条自由文本日志——运维无法回答「哪一类任务长期失败」「哪一类最慢」
# 「一批任务整体耗时多少」。这里按任务类型累计进程内指标（线程安全、零 IO），
# 并输出带固定字段（``event`` / ``scheme_task`` / ``duration_ms`` / ``outcome``）
# 的结构化日志行，便于日志平台按字段聚合。
#
# 与 cache_metrics 一致：进程内统计，重启归零；对业务逻辑完全 fail-soft，
# 任何埋点异常都不得影响提取结果。
# ---------------------------------------------------------------------------
_METRICS_LOCK = threading.Lock()

#: task -> {runs, success, failure, total_duration_ms, last_duration_ms,
#:          max_duration_ms, last_outcome, last_error, last_finished_at}
_TASK_METRICS: "dict[str, dict[str, Any]]" = {}

#: 批次级（run_all_scheme_extractions 一次调用）指标
_BATCH_METRICS: "dict[str, Any]" = {
    "batches": 0,
    "task_runs": 0,
    "failed_tasks": 0,
    "total_duration_ms": 0,
    "last_duration_ms": 0,
    "max_duration_ms": 0,
    "last_finished_at": None,
}


def _now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _empty_task_metric() -> "dict[str, Any]":
    return {
        "runs": 0,
        "success": 0,
        "failure": 0,
        "total_duration_ms": 0,
        "last_duration_ms": 0,
        "max_duration_ms": 0,
        "last_outcome": "",
        "last_error": "",
        "last_finished_at": None,
    }


def record_scheme_task_metric(
    task: str, duration_ms: int, *, ok: bool, error: str = ""
) -> None:
    """记录单任务一次执行结果并输出结构化日志（fail-soft，绝不抛错）。

    Args:
        task: 任务键（``SCHEME_EXTRACTION_TASKS`` 的键）。
        duration_ms: 本次执行耗时（毫秒，非负）。
        ok: 是否成功。
        error: 失败原因（成功时为空）。
    """
    outcome = "success" if ok else "failed"
    try:
        with _METRICS_LOCK:
            m = _TASK_METRICS.setdefault(task, _empty_task_metric())
            m["runs"] += 1
            m["success" if ok else "failure"] += 1
            m["total_duration_ms"] += int(duration_ms)
            m["last_duration_ms"] = int(duration_ms)
            m["max_duration_ms"] = max(m["max_duration_ms"], int(duration_ms))
            m["last_outcome"] = outcome
            m["last_error"] = (error or "")[:300]
            m["last_finished_at"] = _now_iso()
    except Exception:  # pragma: no cover - 埋点不得影响业务
        pass

    try:
        logger.log(
            logging.INFO if ok else logging.WARNING,
            "[专项提取] 任务完成 event=scheme_extraction.task task=%s "
            "outcome=%s duration_ms=%d%s",
            task,
            outcome,
            int(duration_ms),
            f" error={error[:200]}" if not ok else "",
            extra={
                "event": "scheme_extraction.task",
                "scheme_task": task,
                "duration_ms": int(duration_ms),
                "outcome": outcome,
                "error": (error or "")[:300],
            },
        )
    except Exception:  # pragma: no cover - 日志格式化失败不影响业务
        pass


def record_scheme_batch_metric(
    duration_ms: int, *, task_count: int, failed_count: int
) -> None:
    """记录一批（五个任务并发）的整体耗时与失败数，并输出结构化日志。"""
    try:
        with _METRICS_LOCK:
            _BATCH_METRICS["batches"] += 1
            _BATCH_METRICS["task_runs"] += int(task_count)
            _BATCH_METRICS["failed_tasks"] += int(failed_count)
            _BATCH_METRICS["total_duration_ms"] += int(duration_ms)
            _BATCH_METRICS["last_duration_ms"] = int(duration_ms)
            _BATCH_METRICS["max_duration_ms"] = max(
                _BATCH_METRICS["max_duration_ms"], int(duration_ms)
            )
            _BATCH_METRICS["last_finished_at"] = _now_iso()
    except Exception:  # pragma: no cover - 埋点不得影响业务
        pass

    try:
        logger.log(
            logging.INFO if failed_count == 0 else logging.WARNING,
            "[专项提取] 批次完成 event=scheme_extraction.batch tasks=%d "
            "failed=%d duration_ms=%d",
            int(task_count),
            int(failed_count),
            int(duration_ms),
            extra={
                "event": "scheme_extraction.batch",
                "task_count": int(task_count),
                "failed_count": int(failed_count),
                "duration_ms": int(duration_ms),
            },
        )
    except Exception:  # pragma: no cover - 日志格式化失败不影响业务
        pass


def scheme_extraction_metrics_snapshot() -> "dict[str, Any]":
    """返回专项提取的累计指标快照（含平均耗时），供运维接口查询。"""
    with _METRICS_LOCK:
        tasks: "dict[str, Any]" = {}
        for task, m in _TASK_METRICS.items():
            avg = int(m["total_duration_ms"] / m["runs"]) if m["runs"] else 0
            tasks[task] = {**m, "avg_duration_ms": avg}
        batch = {
            **_BATCH_METRICS,
            "avg_duration_ms": (
                int(_BATCH_METRICS["total_duration_ms"] / _BATCH_METRICS["batches"])
                if _BATCH_METRICS["batches"]
                else 0
            ),
        }
    return {"tasks": tasks, "batch": batch}


def reset_scheme_extraction_metrics() -> None:
    """清空累计指标（测试隔离用）。"""
    with _METRICS_LOCK:
        _TASK_METRICS.clear()
        for k in _BATCH_METRICS:
            _BATCH_METRICS[k] = 0 if isinstance(_BATCH_METRICS[k], int) else None


# 与 workflows_analysis.ANALYSIS_CONTEXT_TOKEN_CAP 保持一致：文档全文软上限截断，
# 仅极端长文档被裁切，正常文档不降级。
SCHEME_CONTEXT_TOKEN_CAP = 20000

# 跨任务一致的通用 system 前缀（与 analysis 模块共用同一注册表键）。
ANALYSIS_SYSTEM = get_prompt("ANALYSIS_SYSTEM")


def _build_scheme_messages(
    system_prompt_name: str,
    user_prompt_name: str,
    context: str,
    *,
    ctx_block: "str | None" = None,
) -> "list[AIMessage]":
    """构造缓存友好的专项提取消息列表（四段式，详见模块 docstring）。

    结构固定为 4 条：
      [0] system : 通用稳定前缀（ANALYSIS_SYSTEM）
      [1] user   : 文档全文上下文块（大且稳定，跨任务一致 → 可复用缓存）
      [2] user   : 本任务专属指令（*_SYSTEM 五段式正文）
      [3] user   : 本任务输入与差异（*_USER）

    Args:
        ctx_block: 预渲染的文档全文上下文块。并发批量提取时由调用方**只渲染一次**
            并透传，避免五个任务各自重复渲染同一份大全文（见 ``run_all_scheme_extractions``）。
    """
    if ctx_block is None:
        ctx_block = get_prompt(
            "ANALYSIS_CONTEXT_BLOCK",
            CONTEXT=truncate_to_tokens(context, SCHEME_CONTEXT_TOKEN_CAP),
            sanitize=True,
            desensitize=True,
        )
    sys_text = get_prompt(system_prompt_name)
    user_text = get_prompt(user_prompt_name, sanitize=True, desensitize=True)
    return [
        AIMessage(role="system", content=ANALYSIS_SYSTEM),
        AIMessage(role="user", content=ctx_block),
        AIMessage(role="user", content=sys_text),
        AIMessage(role="user", content=user_text),
    ]


async def _run_scheme_task(
    task: str, provider: Any, context: str, **kwargs: Any
) -> Any:
    """按任务名执行单个专项提取任务，返回 ``collect_json_response`` 解析后的 JSON。

    Args:
        task: ``SCHEME_EXTRACTION_TASKS`` 中的键。
        provider: 已构造的 AI provider（调用方负责其错误处理语义）。
        context: 文档全文（内部做 Token 截断 + 脱敏）。
        **kwargs: 透传给 ``collect_json_response``（如 temperature / max_tokens / validator）。
    """
    names = SCHEME_EXTRACTION_TASKS.get(task)
    if names is None:
        raise ValueError(f"未知专项提取任务: {task}")
    system_name, user_name = names
    messages = _build_scheme_messages(system_name, user_name, context)
    # temperature 默认 0：提取类任务要求稳定、可复现的结果。
    kwargs.setdefault("temperature", 0)
    logger.info("[专项提取] 开始任务 event=scheme_extraction.task_start task=%s",
                task, extra={"event": "scheme_extraction.task_start", "scheme_task": task})
    _t0 = time.perf_counter()
    try:
        result = await collect_json_response(provider, messages, **kwargs)
    except Exception as _e:
        # 结构化埋点 + 指标：失败也必须记录耗时（否则「失败任务到底卡了多久」不可见）。
        record_scheme_task_metric(
            task, int((time.perf_counter() - _t0) * 1000), ok=False, error=str(_e)
        )
        raise
    record_scheme_task_metric(task, int((time.perf_counter() - _t0) * 1000), ok=True)
    # 标书智能体（4）缓存可观测性：专项提取同样按「稳定全文前缀 + 任务
    # 差异后置」组织，按任务类型埋点（fail-soft）。
    try:
        from .cache_metrics import record_provider_cache_usage

        record_provider_cache_usage(provider, task_type=f"scheme_extract_{task}")
    except Exception:  # pragma: no cover - 防御性
        pass
    return result


async def run_all_scheme_extractions(
    provider: Any,
    context: str,
    *,
    concurrency: int = 5,
    **kwargs: Any,
) -> "dict[str, Any]":
    """并发执行全部五个专项提取任务（标书智能体（一）「引入 asyncio 并发控制」）。

    五个任务共享同一份文档全文：串行会重复渲染大全文且放大 AI 配额/限流压力；
    这里把全文上下文块**只渲染一次**（``ANALYSIS_CONTEXT_BLOCK`` 渲染代价不低），
    用信号量把并发度限制在 ``concurrency`` 之内，再用 ``asyncio.gather`` 一次性收回。

    Args:
        provider: 已构造的 AI provider（配置故障由调用方在外部处理，保持 500 语义）。
        context: 文档全文。
        concurrency: 最大并发任务数（默认 5，即五个任务同时跑；降为 1 即退化为串行）。
        **kwargs: 透传给 ``collect_json_response``。

    Returns:
        以任务键为键的结果字典，结构示例：:
            {
              "project_overview": [...],
              "scheme_type": [...],
              "dangerous_work_params": [...],
              "basis_list": [...],
              "construction_condition": [...],
            }
        单个任务失败会被隔离：该项值设为 ``{"error": "<异常信息>"}``，不影响其余任务返回。
    """
    # 共享同一份文档全文上下文块，仅渲染一次。
    shared_ctx = get_prompt(
        "ANALYSIS_CONTEXT_BLOCK",
        CONTEXT=truncate_to_tokens(context, SCHEME_CONTEXT_TOKEN_CAP),
        sanitize=True,
        desensitize=True,
    )
    sem = asyncio.Semaphore(max(1, concurrency))
    kwargs.setdefault("temperature", 0)

    async def _one(task: str) -> "tuple[str, Any]":
        system_name, user_name = SCHEME_EXTRACTION_TASKS[task]
        async with sem:
            _t0 = time.perf_counter()
            try:
                logger.info(
                    "[专项提取] 并发开始任务 event=scheme_extraction.task_start task=%s",
                    task,
                    extra={"event": "scheme_extraction.task_start", "scheme_task": task},
                )
                result = await collect_json_response(
                    provider,
                    _build_scheme_messages(system_name, user_name, context, ctx_block=shared_ctx),
                    **kwargs,
                )
                record_scheme_task_metric(
                    task, int((time.perf_counter() - _t0) * 1000), ok=True
                )
                # 标书智能体（4）缓存可观测性：并发批量提取按任务类型埋点
                # （fail-soft，不影响单任务失败隔离语义）。
                try:
                    from .cache_metrics import record_provider_cache_usage

                    record_provider_cache_usage(
                        provider, task_type=f"scheme_extract_{task}"
                    )
                except Exception:  # pragma: no cover - 防御性
                    pass
                return task, result
            except Exception as _e:  # 单任务失败隔离，避免「一损俱损」
                record_scheme_task_metric(
                    task,
                    int((time.perf_counter() - _t0) * 1000),
                    ok=False,
                    error=str(_e),
                )
                logger.error("[专项提取] 任务 %s 失败（其余任务不受影响）: %s", task, _e)
                return task, {"error": str(_e)[:300]}

    _batch_t0 = time.perf_counter()
    results: "dict[str, Any]" = {}
    for task, res in await asyncio.gather(*(_one(t) for t in SCHEME_EXTRACTION_TASKS)):
        results[task] = res
    _failed = sum(1 for r in results.values() if isinstance(r, dict) and "error" in r)
    record_scheme_batch_metric(
        int((time.perf_counter() - _batch_t0) * 1000),
        task_count=len(SCHEME_EXTRACTION_TASKS),
        failed_count=_failed,
    )
    return results


async def extract_project_overview(provider: Any, context: str, **kwargs: Any) -> Any:
    """工程概况提取（替代"项目概述提取"）。"""
    return await _run_scheme_task("project_overview", provider, context, **kwargs)


async def extract_scheme_type(provider: Any, context: str, **kwargs: Any) -> Any:
    """专项方案类型识别。"""
    return await _run_scheme_task("scheme_type", provider, context, **kwargs)


async def extract_dangerous_work_params(provider: Any, context: str, **kwargs: Any) -> Any:
    """危大工程判定参数提取。"""
    return await _run_scheme_task("dangerous_work_params", provider, context, **kwargs)


async def extract_basis_list(provider: Any, context: str, **kwargs: Any) -> Any:
    """编制依据清单提取。"""
    return await _run_scheme_task("basis_list", provider, context, **kwargs)


async def extract_construction_condition(provider: Any, context: str, **kwargs: Any) -> Any:
    """施工条件提取。"""
    return await _run_scheme_task("construction_condition", provider, context, **kwargs)


# ---------------------------------------------------------------------------
# 专项提取结果的读取口径（单一真相）
# ---------------------------------------------------------------------------
def parse_scheme_extraction_raw(raw: Any) -> "tuple[str, list]":
    """把 ``analysis.scheme_extraction_json`` 的原始值解析为 ``(方案类型, 危大参数列表)``。

    TECH-DEBT 2026-10-26（R9，双份解析逻辑漂移）：ai.py 的 ``_read_scheme_extraction``
    与 sse_outline.py 的 ``_read_scheme_extraction_for_outline`` 曾各持一份**逐行相同**
    的解析代码（约 25 行），任一侧修 bug 都会造成「预览模板」与「实际生成模板」口径
    不一致。现收敛到本函数，两边只保留各自的 DB 读取与日志文案。

    解析兼容口径（均为历史已落库的真实形态）：
      - ``scheme_type`` 可能是 ``[{"scheme_type": "基坑工程"}]``（AI 默认输出数组）；
      - 也可能是裸字符串 ``"基坑工程"``（人工/旧数据）；
      - ``dangerous_work_params`` 非数组（缺失/脏数据）一律降级为空列表。

    Args:
        raw: 数据库列原始值，可为 ``str``（JSON 文本）、``dict``、``None`` 或脏数据。

    Returns:
        ``(scheme_type, dangerous_params)``；任何无法解析的输入都安全降级为
        ``("", [])``，绝不抛错（调用方据此回退通用目录模板）。
    """
    if not raw:
        return "", []
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        return "", []
    if not isinstance(data, dict):
        return "", []
    st = data.get("scheme_type")
    scheme_type = ""
    if isinstance(st, list) and st:
        first = st[0]
        scheme = first.get("scheme_type") if isinstance(first, dict) else str(first)
        scheme_type = (scheme or "").strip()
    elif isinstance(st, str):
        scheme_type = st.strip()
    dp = data.get("dangerous_work_params")
    dangerous_params = dp if isinstance(dp, list) else []
    return scheme_type, dangerous_params


async def run_scheme_extraction(task: str, provider: Any, context: str, **kwargs: Any) -> Any:
    """统一调度入口：按任务名执行对应专项提取。

    Args:
        task: 任务键，取值见 ``SCHEME_EXTRACTION_TASKS``。
        provider: 已构造的 AI provider。
        context: 文档全文。
        **kwargs: 透传给 ``collect_json_response``。

    Returns:
        解析后的 JSON（通常是 list[dict]）。
    """
    return await _run_scheme_task(task, provider, context, **kwargs)
