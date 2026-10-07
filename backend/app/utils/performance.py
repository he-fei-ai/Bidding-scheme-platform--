"""性能监控工具模块。

提供 @timer 装饰器和 TimerContext 上下文管理器，
用于记录函数和代码块的执行耗时，支持嵌套计时。

用法示例::

    from app.utils.performance import timer, TimerContext


    @timer
    def my_function(): ...


    with TimerContext("数据处理块"):
        ...
"""

from __future__ import annotations

from collections.abc import Callable
import functools
import logging
import threading
import time
from typing import Any


logger = logging.getLogger(__name__)

# 线程本地变量，用于支持嵌套计时时的缩进层级
_tlocal = threading.local()


def _get_depth() -> int:
    """获取当前线程的嵌套计时深度。"""
    return getattr(_tlocal, "timer_depth", 0)


def _set_depth(depth: int) -> None:
    """设置当前线程的嵌套计时深度。"""
    _tlocal.timer_depth = depth


def _get_indent(depth: int) -> str:
    return "  " * depth


def timer(func: Callable[..., Any]) -> Callable[..., Any]:
    """函数执行耗时装饰器。

    记录被装饰函数的执行耗时，通过 ``logger.info`` 输出。
    支持嵌套计时，根据调用深度自动缩进。

    Usage::

        @timer
        def my_function(): ...
    """

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        _set_depth(_get_depth() + 1)
        depth = _get_depth()
        indent = _get_indent(depth - 1)
        func_name = func.__name__
        logger.info("%s[Timer] %s 开始执行...", indent, func_name)
        start = time.perf_counter()
        try:
            result = func(*args, **kwargs)
            return result
        finally:
            elapsed = time.perf_counter() - start
            logger.info(
                "%s[Timer] %s 执行完成，耗时: %.4f 秒",
                indent,
                func_name,
                elapsed,
            )
            _set_depth(_get_depth() - 1)

    return wrapper


class TimerContext:
    """代码块执行耗时上下文管理器。

    用于记录一段代码块的执行耗时，支持嵌套计时。

    Usage::

        with TimerContext("数据处理"):
            ...

        with TimerContext("详细分析", level=logging.DEBUG):
            ...
    """

    def __init__(self, name: str, level: int = logging.INFO):
        self.name = name
        self.level = level
        self.elapsed: float | None = None
        self._start: float = 0.0
        self._depth: int = 0

    def __enter__(self) -> TimerContext:
        _set_depth(_get_depth() + 1)
        self._depth = _get_depth()
        indent = _get_indent(self._depth - 1)
        logger.log(self.level, "%s[TimerContext] %s 开始...", indent, self.name)
        self._start = time.perf_counter()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.elapsed = time.perf_counter() - self._start
        indent = _get_indent(self._depth - 1)
        logger.log(
            self.level,
            "%s[TimerContext] %s 完成，耗时: %.4f 秒",
            indent,
            self.name,
            self.elapsed,
        )
        _set_depth(_get_depth() - 1)
