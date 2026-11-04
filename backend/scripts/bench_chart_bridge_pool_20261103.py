"""图表 AI 桥接线程池微基准（2026-11-03·遗留 5 性能基线）

对比两种桥接策略在 N 次模拟 AI 调用下的总耗时：
  A. 旧实现语义：每次调用新建 ThreadPoolExecutor(max_workers=1)（用完即关）
  B. 新实现语义：模块级池复用（_get_chat_bridge_pool，max_workers=4，F5 落地）

模拟 provider 为 async 函数 + 1ms 模拟延迟，代表一次真实 AI 调用的桥接开销
下界（真实 AI 网络耗时百 ms 级，此处只度量桥接层差异，不度量模型耗时）。

运行：python scripts/bench_chart_bridge_pool_20261103.py
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import sys
import time
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

N_CALLS = 200
AI_LATENCY = 0.001  # 1ms 模拟延迟


class FakeAsyncProvider:
    async def chat(self, messages, **kwargs):
        await asyncio.sleep(AI_LATENCY)
        return "ok"


def _run_chat_sync_old(provider, messages, **kwargs):
    """旧实现：每次新建线程池（F5 修复前 _run_chat_sync 语义）。"""
    coro = provider.chat(messages, **kwargs)
    if not asyncio.iscoroutine(coro):
        return coro
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def _run_chat_sync_new(provider, messages, **kwargs):
    """新实现：模块级复用池（与 app.services.ai.chart_orchestrator 同口径）。"""
    from app.services.ai.chart_orchestrator import _get_chat_bridge_pool

    coro = provider.chat(messages, **kwargs)
    if not asyncio.iscoroutine(coro):
        return coro
    return _get_chat_bridge_pool().submit(asyncio.run, coro).result()


def _bench(name: str, fn) -> float:
    provider = FakeAsyncProvider()
    t0 = time.perf_counter()
    for _ in range(N_CALLS):
        assert fn(provider, []) == "ok"
    dt = time.perf_counter() - t0
    print(f"{name}: {N_CALLS} 次调用共 {dt:.3f}s（平均 {dt / N_CALLS * 1000:.3f} ms/次）")
    return dt


def main() -> None:
    # 预热：app 模块链 import（约 1s 一次性成本）必须在计时前完成，
    # 否则 B 场景首次调用承担全部 import 成本、污染对比（首轮实测暴露）。
    from app.services.ai.chart_orchestrator import _get_chat_bridge_pool  # noqa: F401

    print(f"模拟延迟 {AI_LATENCY * 1000:.0f} ms/次，共 {N_CALLS} 次调用\n")
    dt_old = _bench("A 旧实现（每次新建池）", _run_chat_sync_old)
    dt_new = _bench("B 新实现（模块级池复用）", _run_chat_sync_new)
    saved = dt_old - dt_new
    print(f"\n节省：{saved:.3f}s（{saved / dt_old * 100:.1f}%）；"
          f"折算单次调用节省 {(dt_old - dt_new) / N_CALLS * 1000:.3f} ms")

    # 收尾：关闭基准进程内创建的模块级池
    from app.services.ai import chart_orchestrator as mod
    if mod._CHAT_BRIDGE_POOL is not None:
        mod._CHAT_BRIDGE_POOL.shutdown(wait=False)
        mod._CHAT_BRIDGE_POOL = None


if __name__ == "__main__":
    main()
