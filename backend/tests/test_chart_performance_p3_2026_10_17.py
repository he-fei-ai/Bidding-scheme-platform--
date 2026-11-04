"""P3 性能调优测试（2026-10-17）

覆盖：
  P3-2: 动态批量大小优化
  P3-3: 渲染耗时 profiling

TD-1 清理（2026-11-02）：原 P3-1「同代码 hash 去重」测试类
TestP3_1_HashDedup 随 export_chart_pipeline.py 死代码一并删除——该系列
从未接线生产链路，同代码去重的等价能力由 export_cache L1 渲染缓存提供，
其生产行为测试见 tests/test_perf_bottleneck_fixes.py（双参
_get_cached_chart_render / _store_chart_render）。
"""

import pytest
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


class TestL1RenderCacheEviction:
    """TD-1 迁移（2026-11-02）：原 R-3 对 pipeline 死代码 hash 缓存的
    容量淘汰断言，迁移到生产主链路实际消费的 export_cache L1 渲染缓存
    （_store_chart_render 的 TTL + LRU 容量淘汰路径）。"""

    def test_lru_eviction_on_overflow(self, monkeypatch):
        from io import BytesIO
        from app.services import export_cache as ec

        monkeypatch.setattr(ec, "_chart_render_cache_max", 2)
        ec._chart_render_cache.clear()
        payloads = ["payload-A", "payload-B", "payload-C"]
        for p in payloads:
            ec._store_chart_render(p, BytesIO(b"PNGDATA"), chart_type="flowchart")
        # 容量上限 2：写入 3 条后淘汰最旧 1 条，仅剩 2 条
        assert len(ec._chart_render_cache) == 2
        # 最旧条目（payload-A）应已被淘汰
        assert ec._get_cached_chart_render(payloads[0], "flowchart") is None
        # 最新条目仍在，且返回独立 BytesIO 副本（X-9 语义）
        hit = ec._get_cached_chart_render(payloads[2], "flowchart")
        assert hit is not None
        assert hit[0].read() == b"PNGDATA"
        ec._chart_render_cache.clear()


class TestP3_2_AdaptiveBatchSize:
    """P3-2 动态批量大小测试"""

    def test_small_batch(self):
        """小批量（<=10）：全速并行"""
        from app.services.export_chart_pipeline import _adaptive_batch_size
        size = _adaptive_batch_size(5, cpu_count=8)
        assert size == 16  # min(8*2, 16)

    def test_medium_batch(self):
        """中批量（11-50）：适度并行"""
        from app.services.export_chart_pipeline import _adaptive_batch_size
        size = _adaptive_batch_size(30, cpu_count=8)
        assert size == 8  # cpu_count

    def test_large_batch(self):
        """大批量（>50）：保守并行"""
        from app.services.export_chart_pipeline import _adaptive_batch_size
        size = _adaptive_batch_size(100, cpu_count=8)
        assert size == 4  # 8//2

    def test_auto_cpu_detection(self):
        """自动 CPU 检测"""
        from app.services.export_chart_pipeline import _adaptive_batch_size
        size = _adaptive_batch_size(50)
        assert size >= 2

    def test_edge_cases(self):
        """边界情况"""
        from app.services.export_chart_pipeline import _adaptive_batch_size
        assert _adaptive_batch_size(0, cpu_count=4) >= 2
        assert _adaptive_batch_size(1, cpu_count=4) >= 2
        assert _adaptive_batch_size(10, cpu_count=4) >= 2
        assert _adaptive_batch_size(50, cpu_count=4) >= 2


class TestP3_3_RenderProfile:
    """P3-3 渲染耗时 profiling 测试"""

    def setup_method(self):
        """每个测试前清空记录"""
        from app.services.export_chart_pipeline import clear_render_profile
        clear_render_profile()

    def test_record_render_time(self):
        """记录渲染耗时"""
        from app.services.export_chart_pipeline import _record_render_time, get_render_profile
        _record_render_time("section1:gantt", 150.5)
        _record_render_time("section2:flowchart", 200.3)
        profile = get_render_profile()
        assert len(profile) == 2
        assert profile[0]["chart_id"] == "section1:gantt"
        assert profile[0]["duration_ms"] == 150.5
        assert profile[1]["chart_id"] == "section2:flowchart"

    def test_record_cached_render(self):
        """记录缓存命中"""
        from app.services.export_chart_pipeline import _record_render_time, get_render_profile
        _record_render_time("section1:gantt", 5.0, cached=True)
        profile = get_render_profile()
        assert profile[0]["cached"] is True

    def test_profile_summary(self):
        """摘要统计"""
        from app.services.export_chart_pipeline import (
            _record_render_time,
            get_render_profile_summary,
        )
        _record_render_time("s1:gantt", 100.0)
        _record_render_time("s2:flowchart", 200.0, cached=True)
        _record_render_time("s3:architecture", 300.0)
        summary = get_render_profile_summary()
        assert summary["count"] == 3
        assert summary["avg_ms"] == 200.0
        assert summary["max_ms"] == 300.0
        assert summary["min_ms"] == 100.0
        assert summary["cached_count"] == 1

    def test_empty_profile_summary(self):
        """空记录摘要"""
        from app.services.export_chart_pipeline import get_render_profile_summary
        summary = get_render_profile_summary()
        assert summary["count"] == 0
        assert summary["avg_ms"] == 0

    def test_profile_max_size(self):
        """最大记录数限制"""
        from app.services.export_chart_pipeline import _record_render_time, get_render_profile
        for i in range(1005):
            _record_render_time(f"s{i}", 100.0)
        profile = get_render_profile()
        assert len(profile) == 1000