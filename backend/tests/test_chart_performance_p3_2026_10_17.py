"""P3 性能调优测试（2026-10-17）

覆盖：
  P3-1: 同代码 hash 去重缓存
  P3-2: 动态批量大小优化
  P3-3: 渲染耗时 profiling
"""

import pytest
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


class TestP3_1_HashDedup:
    """P3-1 同代码 hash 去重测试"""

    def setup_method(self):
        """每个测试前清空缓存"""
        from app.services.export_chart_pipeline import clear_chart_hash_cache
        clear_chart_hash_cache()

    def test_chart_hash_generation(self):
        """hash 生成：相同代码相同 hash"""
        from app.services.export_chart_pipeline import _get_chart_hash
        code = "graph TD\n    A-->B"
        hash1 = _get_chart_hash(code)
        hash2 = _get_chart_hash(code)
        assert hash1 == hash2
        assert len(hash1) == 40  # SHA1 长度

    def test_chart_hash_different_codes(self):
        """hash 生成：不同代码不同 hash"""
        from app.services.export_chart_pipeline import _get_chart_hash
        hash1 = _get_chart_hash("graph TD\n    A-->B")
        hash2 = _get_chart_hash("graph TD\n    A-->C")
        assert hash1 != hash2

    def test_cache_miss_then_hit(self):
        """缓存：先 miss 后 hit"""
        from app.services.export_chart_pipeline import (
            _get_cached_chart_render,
            _cache_chart_render,
        )
        code = "graph TD\n    A-->B"
        result = _get_cached_chart_render(code)
        assert result is None
        _cache_chart_render(code, b"rendered_data")
        result = _get_cached_chart_render(code)
        assert result == b"rendered_data"

    def test_cache_different_codes_independent(self):
        """缓存：不同代码独立"""
        from app.services.export_chart_pipeline import (
            _get_cached_chart_render,
            _cache_chart_render,
        )
        code1 = "graph TD\n    A-->B"
        code2 = "graph TD\n    A-->C"
        _cache_chart_render(code1, b"data1")
        _cache_chart_render(code2, b"data2")
        assert _get_cached_chart_render(code1) == b"data1"
        assert _get_cached_chart_render(code2) == b"data2"

    def test_cache_stats(self):
        """缓存统计"""
        from app.services.export_chart_pipeline import (
            _get_cached_chart_render,
            _cache_chart_render,
            get_chart_hash_cache_stats,
        )
        code = "graph TD\n    A-->B"
        _get_cached_chart_render(code)
        _cache_chart_render(code, b"data")
        _get_cached_chart_render(code)
        stats = get_chart_hash_cache_stats()
        assert stats["hits"] == 1
        assert stats["misses"] == 1


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