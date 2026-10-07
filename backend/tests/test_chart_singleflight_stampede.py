# -*- coding: utf-8 -*-
"""图表渲染并发缓存击穿（single-flight）回归测试（2026-10-06）。

背景：预渲染线程池（≤8 线程）与正文渲染可能同时请求「同一份图表」
（payload 仅 fig_no 不同，已被 _strip_fig_no_for_cache 归一化为同 key）。
旧实现 L1/L2 缓存 miss 后各自调 render_mermaid_to_bytes → 同图被重复渲染 N 次。

本测试用计数桩替换真实渲染器，断言：
  - N 个线程并发请求同一份图表数据，真实渲染只发生 1 次（owner）；
  - 其余线程等待并共享产物，全部成功；
  - owner 渲染失败时，等待方拿到 None（不静默成功、不伪造数据）。
"""
import sys
import threading
import time
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest

_BACKEND = Path(__file__).parent.parent
sys.path.insert(0, str(_BACKEND))

from app.services import export as E


PAYLOAD = {
    "type": "labor",
    "title": "劳动力计划",
    "phases": ["基础", "主体"],
    "categories": ["普工", "木工"],
    "data": [[10, 20], [30, 40]],
}


class _RenderCounter:
    def __init__(self, fail=False, delay=0.05):
        self.n = 0
        self.lock = threading.Lock()
        self.fail = fail
        self.delay = delay
        self.orig = E.render_mermaid_to_bytes

    def __enter__(self):
        def fake(code, chart_type, *, skip_http=False):
            with self.lock:
                self.n += 1
            time.sleep(self.delay)
            if self.fail:
                return None
            from PIL import Image
            buf = BytesIO()
            Image.new("RGB", (8, 8), (255, 255, 255)).save(buf, format="PNG")
            buf.seek(0)
            return buf

        E.render_mermaid_to_bytes = fake
        return self

    def __exit__(self, *a):
        E.render_mermaid_to_bytes = self.orig


def _run_concurrent(n=8, **cfg):
    sd = SimpleNamespace(title="测试章", project_id=999, labor_json=PAYLOAD, content="")
    c = E.ExportConfig(image_max_width_cm=18.0, image_dpi=150)
    results = [None] * n

    def worker(i):
        results[i] = E._add_inline_chart(
            None, "labor", "", sd, c,
            preload_only=True, _http_service_available=False,
            parent_section_number="3",
        )

    ts = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    return results


@pytest.fixture(autouse=True)
def _clean_caches(tmp_path):
    E._reset_figure_counter(999)
    E._chart_render_cache.clear()
    # single-flight 注册表必须清空，否则跨用例互相影响
    with E._inflight_lock:
        E._inflight_renders.clear()
        E._inflight_results.clear()
    # 磁盘 L2 缓存也换成临时目录，避免历史产物命中干扰桩计数
    orig_disk_cache = E._chart_cache
    E._chart_cache = E.ChartCache(cache_dir=str(tmp_path / "charts"))
    yield
    E._chart_cache = orig_disk_cache


def test_concurrent_same_payload_renders_once():
    """8 线程并发同一份数据，真实渲染只触发 1 次（缓存击穿已修）。"""
    with _RenderCounter(delay=0.2) as cnt:
        results = _run_concurrent(8)
    assert cnt.n == 1, f"同 payload 并发应合并为 1 次渲染，实际 {cnt.n} 次"
    assert all(r is True for r in results), f"所有线程应成功，实际 {results}"


def test_owner_failure_propagates_to_waiters():
    """owner 渲染失败 → 等待方拿到失败（None/False），不静默成功。"""
    with _RenderCounter(fail=True, delay=0.05):
        results = _run_concurrent(6)
    assert all(r is False for r in results), f"owner 失败时等待方应同步失败，实际 {results}"


def test_distinct_payloads_render_independently():
    """不同 payload 不互相阻塞，各自渲染（合并不能误伤不同图表）。"""
    other = dict(PAYLOAD)
    other["data"] = [[99, 88], [77, 66]]
    sd_a = SimpleNamespace(title="A", project_id=999, labor_json=PAYLOAD, content="")
    sd_b = SimpleNamespace(title="B", project_id=999, labor_json=other, content="")
    c = E.ExportConfig(image_max_width_cm=18.0, image_dpi=150)
    with _RenderCounter(delay=0.05) as cnt:
        def work(sd):
            return E._add_inline_chart(
                None, "labor", "", sd, c,
                preload_only=True, _http_service_available=False,
                parent_section_number="3",
            )
        ta = threading.Thread(target=work, args=(sd_a,))
        tb = threading.Thread(target=work, args=(sd_b,))
        ta.start(); tb.start(); ta.join(); tb.join()
    assert cnt.n == 2, f"两份不同数据应各自渲染 2 次，实际 {cnt.n} 次"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v", "-p", "no:cacheprovider"]))
