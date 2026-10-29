"""可观测性查询 API（ENHANCE 2026-10-29）。

提供：
- GET  /observability/logs                    日志组合查询（分页/排序/导出）
- GET  /observability/trace/{trace_id}        完整调用链（span 层级标注 + 断链标记）
- GET  /observability/trajectory/{task_id}    任务运行轨迹（生命周期时间线）
- GET  /observability/trajectories            任务轨迹列表（按状态/类型/时间筛选）
- GET  /observability/metrics                 指标看板快照
- GET  /observability/alerts                  告警历史
- GET  /observability/storage                 日志存储状态
- POST /observability/cleanup                 手动触发轮转清理
- GET  /observability/dashboard               前端查看界面（自包含 HTML）
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse, PlainTextResponse

from ..security.auth import get_current_user
from ..observability import metrics as _metrics
from ..observability import alerts as _alerts
from ..observability import query as _query
from ..observability import storage as _storage
from ..observability import trajectory as _trajectory

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/observability",
    tags=["observability"],
    dependencies=[Depends(get_current_user)],
)


@router.get("/logs")
async def logs(
    trace_id: str = Query("", description="按 trace_id 过滤"),
    task_id: str = Query("", description="按 task_id 过滤"),
    project_id: str = Query("", description="按 project_id 过滤"),
    module: str = Query("", description="按模块过滤"),
    level: str = Query("", description="按级别过滤"),
    event: str = Query("", description="按事件过滤"),
    error_code: str = Query("", description="按错误码过滤"),
    keyword: str = Query("", description="关键词搜索（消息/详情）"),
    start: str = Query("", description="起始时间（含）"),
    end: str = Query("", description="结束时间（含）"),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    sort: str = Query("desc", pattern="^(asc|desc)$"),
):
    """日志组合查询。"""
    try:
        return _query.query_logs(
            trace_id=trace_id, task_id=task_id, project_id=project_id,
            module=module, level=level, event=event, error_code=error_code,
            keyword=keyword, start=start, end=end,
            limit=limit, offset=offset, sort=sort,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("observability.logs 查询失败: %s", e)

@router.get("/logs/export")
async def export_logs(
    fmt: str = Query("json", pattern="^(json|csv)$"),
    trace_id: str = Query(""),
    task_id: str = Query(""),
    project_id: str = Query(""),
    module: str = Query(""),
    level: str = Query(""),
    error_code: str = Query(""),
    keyword: str = Query(""),
    limit: int = Query(1000, ge=1, le=10000),
):
    """导出日志（CSV/JSON）。"""
    try:
        result = _query.query_logs(
            trace_id=trace_id, task_id=task_id, project_id=project_id,
            module=module, level=level, error_code=error_code, keyword=keyword,
            limit=limit,
        )
        items = result["items"]
        if fmt == "csv":
            return PlainTextResponse(
                _query.export_csv(items), media_type="text/csv; charset=utf-8",
                headers={"Content-Disposition": "attachment; filename=observability_logs.csv"},
            )
        return PlainTextResponse(
            _query.export_json(items), media_type="application/json; charset=utf-8",
            headers={"Content-Disposition": "attachment; filename=observability_logs.json"},
        )
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail="导出失败") from e


@router.get("/trace/{trace_id}")
async def trace_chain(trace_id: str):
    """按 trace_id 返回完整调用链。"""
    try:
        return _query.get_trace_chain(trace_id)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail="调用链查询失败") from e


@router.get("/trajectory/{task_id}")
async def trajectory(task_id: str):
    """按 task_id 返回任务运行轨迹。"""
    try:
        items = _trajectory.get_trajectory(task_id)
        return {"task_id": task_id, "total": len(items), "items": items}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail="运行轨迹查询失败") from e


@router.get("/trajectories")
async def trajectories(
    status: str = Query(""),
    task_type: str = Query(""),
    task_id: str = Query(""),
    project_id: str = Query(""),
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    """任务轨迹列表（按状态/类型/时间筛选）。"""
    try:
        return _trajectory.list_trajectories(
            status=status, task_type=task_type, task_id=task_id,
            project_id=project_id, limit=limit, offset=offset,
        )
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail="轨迹列表查询失败") from e


@router.get("/metrics")
async def metrics():
    """指标看板快照（P50/P95/P99/成功率/Token/成本/错误码TOP10/队列积压）。"""
    try:
        snap = _metrics.metrics_snapshot()
        snap["storage"] = _storage.stats()
        snap["alert_history"] = _alerts.alert_history(20)
        return snap
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail="指标查询失败") from e


@router.get("/alerts")
async def alerts(limit: int = Query(50, ge=1, le=500)):
    """告警历史。"""
    try:
        return {"items": _alerts.alert_history(limit)}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail="告警查询失败") from e


@router.get("/storage")
async def storage_stats():
    """日志存储状态（队列/丢弃/降级/写盘错误）。"""
    try:
        return _storage.stats()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail="存储状态查询失败") from e


@router.post("/cleanup")
async def cleanup():
    """手动触发轮转清理（压缩 + 保留清理）。"""
    try:
        return _storage.cleanup()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail="清理失败") from e


@router.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
async def dashboard():
    """前端查看界面（自包含 HTML，含日志列表/详情/链路视图/时间线视图/搜索过滤/实时刷新/导出）。"""
    try:
        from ..observability_dashboard import DASHBOARD_HTML

        return HTMLResponse(DASHBOARD_HTML)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail="看板加载失败") from e

        raise HTTPException(status_code=500, detail="日志查询失败") from e
