"""最近错误收集器（用于调试前端 500 错误的根因）"""

from collections import deque
import threading
import time
from typing import Any


_LOCK = threading.Lock()
_RECENT_ERRORS: deque = deque(maxlen=50)


def record_error(
    method: str, path: str, error_type: str, error_detail: str, traceback: str
) -> None:
    """记录一条最近错误"""
    entry = {
        "ts": time.time(),
        "iso": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
        "method": method,
        "path": path,
        "error_type": error_type,
        "error_detail": (error_detail or "")[:500],
        "traceback": (traceback or "")[:2000],
    }
    with _LOCK:
        _RECENT_ERRORS.appendleft(entry)


def get_recent_errors(limit: int = 20) -> list[dict[str, Any]]:
    """获取最近的错误列表"""
    with _LOCK:
        items = list(_RECENT_ERRORS)[:limit]
    return items


def clear_recent_errors() -> int:
    """清空最近错误列表，返回清空的条数"""
    with _LOCK:
        count = len(_RECENT_ERRORS)
        _RECENT_ERRORS.clear()
    return count
