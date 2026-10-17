"""全局事实模块运行时指标（ENHANCE 2026-10-17 / 加固）。

此前事实提取/编排选择/标题解析的耗时与命中情况只有散落在各端点的日志，
无从聚合观测。本模块提供进程内、线程安全的计数器与只读快照接口，随
``GET /facts/metrics`` 下发。

设计约束（与 ``consistency_metrics.py`` 同构）：
  - 纯内存、无持久化——进程重启即归零，仅用于实时观测。
  - 计数器为「累计值」语义；耗时为「最近一次」语义。
  - 与 ``consistency_metrics`` / ``prompts.get_prompt_metrics`` 同构，
    便于后续统一接入监控看板。
"""
import logging
import threading

logger = logging.getLogger(__name__)

#: 已知指标键（快照渲染时缺键补 0，前端卡片不出现「undefined」）。
KNOWN_KEYS: tuple[str, ...] = (
    # 事实提取
    "fact_extract_runs",        # 事实提取执行次数（成功完成）
    "fact_extract_failed",      # 事实提取异常终止次数
    "fact_extract_elapsed_ms",  # 最近一次提取耗时（毫秒，累计值不递增）
    "fact_extract_items_total", # 累计提取事实条目数
    "fact_knowledge_patch_hits",# 知识库补充实际生效条数
    "fact_template_patch_hits", # 既有方案补充实际生效条数
    # 编排阶段事实标题
    "orchestration_titles_offered", # 编排时提供给 AI 的事实标题数（累计）
    "orchestration_titles_selected", # 编排返回的 facts.titles 总数（累计）
    "orchestration_titles_empty",     # 编排返回空标题的次数（告警信号）
    "orchestration_titles_skipped",   # 编排开关关闭被跳过的次数
    # 生成阶段标题解析
    "fact_resolve_hits",   # 按标题命中事实的次数
    "fact_resolve_misses", # 标题在白名单外被丢弃的次数
    "fact_resolve_runs",   # 标题解析执行次数
)


class _MetricsRegistry:
    """进程内线程安全计数器（与 consistency_metrics 同构）。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._counters: dict[str, int] = {}

    def inc(self, name: str, n: int = 1) -> None:
        """递增某个指标计数。"""
        with self._lock:
            self._counters[name] = self._counters.get(name, 0) + n

    def set(self, name: str, value: int) -> None:
        """设置某个指标的绝对值（用于耗时等「最近一次」语义）。"""
        with self._lock:
            self._counters[name] = value

    def snapshot(self) -> dict[str, int]:
        """返回当前所有指标的深拷贝快照（不含默认键补 0）。"""
        with self._lock:
            return dict(self._counters)

    def snapshot_with_defaults(self) -> dict[str, int]:
        """返回快照，已知键缺省补 0（供前端渲染）。"""
        with self._lock:
            base = dict(self._counters)
        for k in KNOWN_KEYS:
            base.setdefault(k, 0)
        return base

    def reset(self) -> None:
        """清空全部计数（测试用）。"""
        with self._lock:
            self._counters.clear()


#: 全局唯一实例。
facts_metrics = _MetricsRegistry()


# 模块级便捷函数（委托给全局实例，便于单行调用）
def inc(name: str, n: int = 1) -> None:
    facts_metrics.inc(name, n)


def set_value(name: str, value: int) -> None:
    facts_metrics.set(name, value)


def snapshot() -> dict[str, int]:
    return facts_metrics.snapshot()


def snapshot_with_defaults() -> dict[str, int]:
    return facts_metrics.snapshot_with_defaults()


def reset() -> None:
    facts_metrics.reset()
