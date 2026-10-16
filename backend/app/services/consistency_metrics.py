"""一致性校核模块运行时指标（ENHANCE 2026-10-16 / T5）。

此前审计失败率 / 生成守卫拦截数 / AI 补丁拒绝率只有散落在各端点的日志，无从
聚合观测。本模块提供进程内、线程安全的计数器与只读快照接口，随
``GET /consistency/metrics`` 下发。

设计约束：
  - 纯内存、无持久化——进程重启即归零，仅用于实时观测，不作为审计结论的
    数据来源（审计结论一律看 ``consistency_audit_log``）。
  - 计数器为「累计值」语义；调用方若需「最近 N 分钟」可自行在外层做差分快照。
  - 与 ``services/ai/prompts`` 的 ``get_prompt_metrics`` 同构（模块级 RLock +
    深拷贝快照），便于后续统一接入监控看板（跨模块基建，另立专项）。
"""
import logging
import threading

logger = logging.getLogger(__name__)

#: 已知指标键（快照渲染时缺键补 0，前端卡片不出现「undefined」）。
KNOWN_KEYS: tuple[str, ...] = (
    "audit_runs",            # 确定性规则审计执行次数（成功完成）
    "audit_failed",          # 确定性规则审计异常终止次数
    "ai_audit_runs",         # AI 语义审计落库次数（正文生成收尾段）
    "ai_findings",           # AI 语义审计累计发现条数
    "ai_findings_repaired",  # AI 语义审计中已被就地修复的发现条数
    "ai_patch_rejected",     # AI 语义补丁被唯一性校验拒绝的累计条数
    "guard_interceptions",   # 生成中守卫拦截（409）次数（事实回流采纳路径）
)


class _MetricsRegistry:
    """进程内线程安全计数器（与 prompts 的 ``get_prompt_metrics`` 同构）。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._counters: dict[str, int] = {}

    def inc(self, name: str, n: int = 1) -> None:
        """递增某个指标计数。"""
        with self._lock:
            self._counters[name] = self._counters.get(name, 0) + n

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


#: 全局唯一实例。调用方 ``from ..services.consistency_metrics import
#: consistency_metrics`` 后 ``.inc(...)`` 使用。
consistency_metrics = _MetricsRegistry()


# 模块级便捷函数（委托给全局实例，便于单行调用）
def inc(name: str, n: int = 1) -> None:
    consistency_metrics.inc(name, n)


def snapshot() -> dict[str, int]:
    return consistency_metrics.snapshot()


def snapshot_with_defaults() -> dict[str, int]:
    return consistency_metrics.snapshot_with_defaults()


def reset() -> None:
    consistency_metrics.reset()
