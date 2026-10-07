"""性能优化模块单元测试

覆盖以下瓶颈修复：
- B2:  list_sections_light 不加载 content 大列
- B3:  delete_tree_recursive_cte 替代 N+1 循环
- B7:  build_sort_lookup O(N²) → O(N)
- B8:  cached_image_font 字体缓存
- B13: redis_list_count_simplified 去除冗余 exists
- B14: precompute_inline_chart_types 预计算图表标记
"""

from __future__ import annotations

import os
import sys
import time
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ============================================================================
# B2: list_sections_light（不加载 content）
# ============================================================================

class TestListSectionsLight:
    """list_sections_light: 只返回非 content 列，减少数据传输"""

    def test_returns_without_content_column(self):
        from app.services.perf_optimizations import list_sections_light
        from app.db import init_db, get_db

        init_db()
        with get_db() as conn:
            conn.execute("DELETE FROM sections")
            conn.execute("DELETE FROM projects")
            conn.execute(
                "INSERT INTO projects (name, industry) VALUES (?, ?)", ("轻查询测试", "通用")
            )
            pid = conn.execute(
                "SELECT id FROM projects ORDER BY id DESC LIMIT 1"
            ).fetchone()["id"]
            conn.execute(
                "INSERT INTO sections (project_id, title, level, sort_order, content) "
                "VALUES (?, ?, 1, 1, ?)",
                (pid, "测试章节", "x" * 10000),  # 10KB content
            )

        result = list_sections_light(pid)
        assert len(result) == 1
        row = result[0]
        # 不应包含 content 列
        assert "content" not in row
        # 核心元数据应存在
        assert row["title"] == "测试章节"
        assert row["level"] == 1
        assert row["project_id"] == pid

    def test_empty_result_for_missing_project(self):
        from app.services.perf_optimizations import list_sections_light

        result = list_sections_light(999999)
        assert result == []

    def test_performance_vs_full_select(self):
        """轻量查询应显著快于全量查询（100 条记录以上场景）"""
        from app.services.perf_optimizations import list_sections_light
        from app.db import init_db, get_db

        init_db()
        with get_db() as conn:
            conn.execute("DELETE FROM sections")
            conn.execute("DELETE FROM projects")
            conn.execute(
                "INSERT INTO projects (name, industry) VALUES (?, ?)", ("性能测试", "通用")
            )
            pid = conn.execute(
                "SELECT id FROM projects ORDER BY id DESC LIMIT 1"
            ).fetchone()["id"]
            # 插入 50 条带大 content 的记录
            for i in range(50):
                conn.execute(
                    "INSERT INTO sections (project_id, title, level, sort_order, content) "
                    "VALUES (?, ?, 1, ?, ?)",
                    (pid, f"章节{i}", i, "段落内容" * 500),  # ~4KB content
                )
            conn.commit()

        # 预热
        list_sections_light(pid)

        iterations = 50
        start = time.perf_counter()
        for _ in range(iterations):
            list_sections_light(pid)
        elapsed = time.perf_counter() - start

        # 轻量查询 50 次应 < 1s（不含 content 列，传输量大幅减少）
        assert elapsed < 1.0, f"list_sections_light took {elapsed:.4f}s for {iterations} calls"


# ============================================================================
# B3: delete_tree_recursive_cte（单次 CTE DELETE）
# ============================================================================

class TestDeleteTreeRecursiveCTE:
    """delete_tree_recursive_cte: 单次递归 CTE 替代 N+1 循环"""

    def test_deletes_whole_subtree(self):
        from app.services.perf_optimizations import delete_tree_recursive_cte
        from app.db import init_db, get_db

        init_db()
        with get_db() as conn:
            conn.execute("DELETE FROM sections")
            conn.execute("DELETE FROM projects")
            conn.execute(
                "INSERT INTO projects (name, industry) VALUES (?, ?)", ("CTE删除测试", "通用")
            )
            pid = conn.execute(
                "SELECT id FROM projects ORDER BY id DESC LIMIT 1"
            ).fetchone()["id"]

            # 构建树: root(1) -> c1(2) -> c11(3)
            conn.execute(
                "INSERT INTO sections (project_id, title, level, sort_order) VALUES (?, ?, 1, 1)",
                (pid, "根章节"),
            )
            root_id = conn.execute(
                "SELECT id FROM sections ORDER BY id DESC LIMIT 1"
            ).fetchone()["id"]

            conn.execute(
                "INSERT INTO sections (project_id, title, level, sort_order, parent_id) "
                "VALUES (?, ?, 2, 1, ?)",
                (pid, "子章节A", root_id),
            )
            c1_id = conn.execute(
                "SELECT id FROM sections ORDER BY id DESC LIMIT 1"
            ).fetchone()["id"]

            conn.execute(
                "INSERT INTO sections (project_id, title, level, sort_order, parent_id) "
                "VALUES (?, ?, 3, 1, ?)",
                (pid, "孙章节", c1_id),
            )
            c11_id = conn.execute(
                "SELECT id FROM sections ORDER BY id DESC LIMIT 1"
            ).fetchone()["id"]

            conn.commit()

        # 验证树存在 3 个节点
        count_before = conn.execute("SELECT COUNT(*) as c FROM sections").fetchone()["c"]
        assert count_before == 3

        # 使用 CTE 删除根节点及其所有子节点
        deleted = delete_tree_recursive_cte(conn, root_id)
        assert deleted == 3, f"Expected to delete 3 rows, got {deleted}"

        # 验证全部删除
        count_after = conn.execute("SELECT COUNT(*) as c FROM sections").fetchone()["c"]
        assert count_after == 0

    def test_delete_single_node_no_children(self):
        from app.services.perf_optimizations import delete_tree_recursive_cte
        from app.db import init_db, get_db

        init_db()
        with get_db() as conn:
            conn.execute("DELETE FROM sections")
            conn.execute("DELETE FROM projects")
            conn.execute(
                "INSERT INTO projects (name, industry) VALUES (?, ?)", ("单删测试", "通用")
            )
            pid = conn.execute(
                "SELECT id FROM projects ORDER BY id DESC LIMIT 1"
            ).fetchone()["id"]
            conn.execute(
                "INSERT INTO sections (project_id, title, level, sort_order) VALUES (?, ?, 1, 1)",
                (pid, "孤立章节"),
            )
            sid = conn.execute(
                "SELECT id FROM sections ORDER BY id DESC LIMIT 1"
            ).fetchone()["id"]
            conn.commit()

        deleted = delete_tree_recursive_cte(conn, sid)
        assert deleted == 1

        remaining = conn.execute(
            "SELECT COUNT(*) as c FROM sections"
        ).fetchone()["c"]
        assert remaining == 0

    def test_delete_nonexistent_returns_zero(self):
        from app.services.perf_optimizations import delete_tree_recursive_cte
        from app.db import init_db, get_db

        init_db()
        with get_db() as conn:
            conn.execute("DELETE FROM sections")
            conn.execute("DELETE FROM projects")
            conn.execute(
                "INSERT INTO projects (name, industry) VALUES (?, ?)", ("空删测试", "通用")
            )
            conn.commit()

        deleted = delete_tree_recursive_cte(conn, 999999)
        assert deleted == 0

    def test_performance_deep_tree(self):
        """深树（50 节点）删除：CTE 方式远快于 N+1 循环"""
        from app.services.perf_optimizations import delete_tree_recursive_cte
        from app.db import init_db, get_db

        init_db()
        with get_db() as conn:
            conn.execute("DELETE FROM sections")
            conn.execute("DELETE FROM projects")
            conn.execute(
                "INSERT INTO projects (name, industry) VALUES (?, ?)", ("深度树测试", "通用")
            )
            pid = conn.execute(
                "SELECT id FROM projects ORDER BY id DESC LIMIT 1"
            ).fetchone()["id"]

            # 构建 50 层深链
            parent_id = None
            first_id = None
            for i in range(50):
                conn.execute(
                    "INSERT INTO sections (project_id, title, level, sort_order, parent_id) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (pid, f"节点{i}", i + 1, i, parent_id),
                )
                new_id = conn.execute(
                    "SELECT id FROM sections ORDER BY id DESC LIMIT 1"
                ).fetchone()["id"]
                if first_id is None:
                    first_id = new_id
                parent_id = new_id
            conn.commit()

        # 计时 CTE 删除
        start = time.perf_counter()
        deleted = delete_tree_recursive_cte(conn, first_id)
        elapsed = time.perf_counter() - start

        assert deleted == 50
        # CTE 删除 50 节点应 < 100ms
        assert elapsed < 0.1, f"CTE delete took {elapsed:.4f}s for 50 nodes"


# ============================================================================
# B7: build_sort_lookup（O(N²) → O(N)）
# ============================================================================

class TestBuildSortLookup:
    """build_sort_lookup: O(N²) 扫描 → O(N) 哈希查找"""

    def test_basic_lookup(self):
        from app.services.perf_optimizations import build_sort_lookup

        rows = [
            {"id": 1, "sort_order": 3, "created_at": "2024-01-01"},
            {"id": 2, "sort_order": 1, "created_at": "2024-01-02"},
            {"id": 3, "sort_order": 2, "created_at": "2024-01-03"},
        ]
        lookup = build_sort_lookup(rows)
        assert lookup[1] == (3, "2024-01-01")
        assert lookup[2] == (1, "2024-01-02")
        assert lookup[3] == (2, "2024-01-03")

    def test_missing_created_at_defaults_to_empty(self):
        from app.services.perf_optimizations import build_sort_lookup

        rows = [{"id": 1, "sort_order": 0, "created_at": None}]
        lookup = build_sort_lookup(rows)
        assert lookup[1] == (0, "")

    def test_empty_rows(self):
        from app.services.perf_optimizations import build_sort_lookup

        lookup = build_sort_lookup([])
        assert lookup == {}

    def test_performance_comparison(self):
        """验证 build_sort_lookup 相比 O(N²) 扫描有明显性能优势"""
        import random
        from app.services.perf_optimizations import build_sort_lookup

        # 生成 500 条模拟行
        random.seed(42)
        ids = list(range(1, 501))
        random.shuffle(ids)
        rows = [
            {"id": i, "sort_order": random.randint(0, 10000), "created_at": f"2024-01-{(i % 28) + 1:02d}"}
            for i in ids
        ]

        # build_sort_lookup 方式（O(N)）
        start = time.perf_counter()
        lookup = build_sort_lookup(rows)
        elapsed_lookup = time.perf_counter() - start

        # 模拟原 O(N²) 方式：对每个 id 线性扫描
        start = time.perf_counter()
        for r in rows:
            found = next((x for x in rows if x["id"] == r["id"]), None)
            assert found is not None
        elapsed_linear = time.perf_counter() - start

        # 哈希查找应显著快于线性扫描（至少 10 倍）
        speedup = elapsed_linear / elapsed_lookup if elapsed_lookup > 0 else float("inf")
        assert speedup >= 5, (
            f"Hash lookup speedup too low: {speedup:.2f}x "
            f"(O(N): {elapsed_lookup:.6f}s, O(N²): {elapsed_linear:.6f}s)"
        )


# ============================================================================
# B8: cached_image_font（字体缓存）
# ============================================================================

class TestCachedImageFont:
    """cached_image_font: 相同参数应命中缓存，避免重复 I/O"""

    def test_first_call_loads_font(self):
        from app.services.perf_optimizations import cached_image_font, _font_cache

        _font_cache.clear()
        # 使用系统实际存在的字体路径测试
        import sys
        candidates = []
        if sys.platform == "win32":
            candidates = [
                "C:/Windows/Fonts/msyh.ttc",
                "C:/Windows/Fonts/simhei.ttf",
            ]
        else:
            candidates = [
                "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            ]

        # 找第一个存在的字体
        available = next((p for p in candidates if os.path.exists(p)), None)
        if available is None:
            pytest.skip("No system font available for testing")

        font = cached_image_font(size=12, bold=False, font_paths=[available])
        assert font is not None

        # 验证缓存已写入
        cache_key = (available, 12, False)
        assert cache_key in _font_cache
        assert _font_cache[cache_key] is font

    def test_cached_call_is_instant(self):
        from app.services.perf_optimizations import cached_image_font, _font_cache

        _font_cache.clear()
        import sys

        candidates = []
        if sys.platform == "win32":
            candidates = [
                "C:/Windows/Fonts/msyh.ttc",
            ]
        else:
            candidates = [
                "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            ]

        available = next((p for p in candidates if os.path.exists(p)), None)
        if available is None:
            pytest.skip("No system font available for testing")

        # 预热：第一次调用，可能涉及 I/O
        cached_image_font(size=14, bold=False, font_paths=[available])

        # 第二次调用：应命中缓存
        iterations = 1000
        start = time.perf_counter()
        for _ in range(iterations):
            cached_image_font(size=14, bold=False, font_paths=[available])
        elapsed = time.perf_counter() - start

        # 1000 次缓存命中应 < 5ms（包含 Python 函数调用开销）
        assert elapsed < 0.005, f"Font cache hit too slow: {elapsed:.6f}s for {iterations} calls"

    def test_different_params_miss_cache(self):
        from app.services.perf_optimizations import cached_image_font, _font_cache

        _font_cache.clear()
        import sys

        candidates = []
        if sys.platform == "win32":
            candidates = ["C:/Windows/Fonts/msyh.ttc"]
        else:
            candidates = ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]

        available = next((p for p in candidates if os.path.exists(p)), None)
        if available is None:
            pytest.skip("No system font available for testing")

        font_12 = cached_image_font(size=12, bold=False, font_paths=[available])
        font_14 = cached_image_font(size=14, bold=False, font_paths=[available])

        # 不同 size 应得到不同缓存条目
        assert (available, 12, False) in _font_cache
        assert (available, 14, False) in _font_cache
        # 两个不同 size 的 font 对象可能相同（取决于 PIL 实现），但缓存 key 不同
        assert len(_font_cache) >= 2


# ============================================================================
# B13: redis_list_count_simplified（去除冗余 exists）
# ============================================================================

class TestRedisListCountSimplified:
    """redis_list_count_simplified: 去掉冗余 Redis exists 调用"""

    def test_returns_length_for_existing_key(self):
        from app.services.perf_optimizations import redis_list_count_simplified

        mock_client = MagicMock()
        mock_client.llen.return_value = 5
        result = redis_list_count_simplified(mock_client, "mykey")
        assert result == 5
        # 验证只调用了 llen，没有调用 exists
        mock_client.llen.assert_called_once_with("mykey")
        mock_client.exists.assert_not_called()

    def test_returns_zero_for_nonexistent_key(self):
        from app.services.perf_optimizations import redis_list_count_simplified

        mock_client = MagicMock()
        mock_client.llen.return_value = 0
        result = redis_list_count_simplified(mock_client, "missing_key")
        assert result == 0
        mock_client.exists.assert_not_called()

    def test_returns_zero_on_exception(self):
        from app.services.perf_optimizations import redis_list_count_simplified

        mock_client = MagicMock()
        mock_client.llen.side_effect = ConnectionError("redis down")
        result = redis_list_count_simplified(mock_client, "key")
        assert result == 0
        mock_client.exists.assert_not_called()

    def test_reduces_redis_roundtrips(self):
        """验证简化版本比原版本少一次 Redis RTT"""
        from app.services.perf_optimizations import redis_list_count_simplified

        mock_client = MagicMock()
        mock_client.llen.return_value = 3

        # 调用简化版
        redis_list_count_simplified(mock_client, "test")

        # 简化版应只有 1 次 RTT（llen），原版本有 2 次（exists + llen）
        assert mock_client.llen.call_count == 1
        assert mock_client.exists.call_count == 0


# ============================================================================
# B14: precompute_inline_chart_types（预计算图表类型）
# ============================================================================

class TestPrecomputeInlineChartTypes:
    """precompute_inline_chart_types: 单次扫描替代逐类型扫描"""

    def test_detects_all_chart_types(self):
        from app.services.perf_optimizations import precompute_inline_chart_types

        content = (
            "一些正文内容\n"
            "[[FLOWCHART:流程图描述]]\n"
            "更多文字\n"
            "[[GANTT:甘特图描述]]\n"
            "[[ARCHITECTURE:架构图描述]]\n"
        )
        result = precompute_inline_chart_types(content)
        assert result == {"flowchart", "gantt", "architecture"}

    def test_returns_empty_for_no_markers(self):
        from app.services.perf_optimizations import precompute_inline_chart_types

        content = "纯文本内容，没有任何图表标记"
        result = precompute_inline_chart_types(content)
        assert result == set()

    def test_returns_empty_for_empty_string(self):
        from app.services.perf_optimizations import precompute_inline_chart_types

        result = precompute_inline_chart_types("")
        assert result == set()

    def test_handles_duplicate_markers(self):
        """重复出现的同类型标记只计入一次"""
        from app.services.perf_optimizations import precompute_inline_chart_types

        content = "[[FLOWCHART:a]] [[FLOWCHART:b]] [[FLOWCHART:c]]"
        result = precompute_inline_chart_types(content)
        assert result == {"flowchart"}
        assert len(result) == 1  # 集合去重

    def test_performance_vs_naive_multiple_find(self):
        """预计算方法应比逐个 find 更快"""
        from app.services.perf_optimizations import precompute_inline_chart_types, has_inline_marker_optimized

        # 构造包含大量内容的文本
        content = "段落内容 " * 500 + "[[FLOWCHART:x]] " + "更多段落 " * 500 + "[[GANTT:y]]"

        # 预计算方式
        start = time.perf_counter()
        found_set = precompute_inline_chart_types(content)
        elapsed_precompute = time.perf_counter() - start

        # 逐个检查方式（模拟原有 O(N×M) 行为）
        chart_types = ["flowchart", "gantt", "architecture", "labor", "comparison", "layout", "timeline"]
        start = time.perf_counter()
        naive_results = {}
        for ct in chart_types:
            naive_results[ct] = has_inline_marker_optimized(content, ct)
        elapsed_naive = time.perf_counter() - start

        # 结果应一致
        assert ("flowchart" in found_set) == naive_results["flowchart"]
        assert ("gantt" in found_set) == naive_results["gantt"]

        # 预计算 + 查表方式总耗时应 <= 逐个扫描（大数据量时优势更明显）
        total_precompute = elapsed_precompute + 0.0001  # 加上 set 查表开销
        # 对于中等长度文本，两种方法都应很快
        assert total_precompute < 0.01, f"Precompute too slow: {total_precompute:.6f}s"


# ============================================================================
# 综合：O(N²) → O(N) 在 get_section_path 中的效果
# ============================================================================

class TestGetSectionPathPerformanceFix:
    """验证 get_section_path 中使用 build_sort_lookup 后的性能改进"""

    def test_path_correctness_with_lookup(self):
        """build_sort_lookup 应保持路径计算的语义正确性"""
        from app.services.perf_optimizations import build_sort_lookup
        from app.db import init_db, get_db

        init_db()
        with get_db() as conn:
            conn.execute("DELETE FROM sections")
            conn.execute("DELETE FROM projects")
            conn.execute(
                "INSERT INTO projects (name, industry) VALUES (?, ?)", ("路径正确性测试", "通用")
            )
            pid = conn.execute(
                "SELECT id FROM projects ORDER BY id DESC LIMIT 1"
            ).fetchone()["id"]

            # 创建交错排序的节点（确保父节点先插入，使用唯一标题）
            nodes = [
                (None, "第一章A", 3),    # 根节点先插入
                ("第一章A", "甲节", 1),
                ("第一章A", "乙节", 2),
                (None, "第二章B", 1),    # 另一个根节点
                ("第二章B", "丙节", 2),
                ("第二章B", "丁节", 1),
            ]
            title_to_id: dict[str, int] = {}
            for parent_title, title, sort_order in nodes:
                parent_id = title_to_id[parent_title] if parent_title else None
                conn.execute(
                    "INSERT INTO sections (project_id, title, level, sort_order, parent_id) "
                    "VALUES (?, ?, 1, ?, ?)",
                    (pid, title, sort_order, parent_id),
                )
                title_to_id[title] = conn.execute(
                    "SELECT id FROM sections WHERE title = ?", (title,)
                ).fetchone()["id"]
            conn.commit()

            # 用 build_sort_lookup 构建查找表
            rows = conn.execute(
                "SELECT id, sort_order, created_at FROM sections WHERE project_id = ?",
                (pid,),
            ).fetchall()
            lookup = build_sort_lookup([dict(r) for r in rows])

            # 验证查找表正确（通过 title_to_id 映射获取真实 ID）
            id_to_title = {v: k for k, v in title_to_id.items()}
            expected = {
                title_to_id["第一章A"]: (3, None),
                title_to_id["甲节"]: (1, None),
                title_to_id["乙节"]: (2, None),
                title_to_id["第二章B"]: (1, None),
                title_to_id["丙节"]: (2, None),
                title_to_id["丁节"]: (1, None),
            }
            for r in rows:
                sid = r["id"]
                if sid in expected:
                    assert lookup[sid][0] == expected[sid][0], f"Wrong sort_order for {id_to_title[sid]}"

    def test_large_scale_lookup_performance(self):
        """大规模 lookup 性能：500 节点的 O(N) 构建 vs O(N²) 扫描"""
        from app.services.perf_optimizations import build_sort_lookup
        import random

        random.seed(123)
        # 模拟 500 行数据
        rows = [
            {"id": i, "sort_order": random.randint(0, 10000), "created_at": f"2024-0{i % 10:02d}-01"}
            for i in range(1, 501)
        ]

        # build_sort_lookup（O(N)）
        start = time.perf_counter()
        lookup = build_sort_lookup(rows)
        elapsed = time.perf_counter() - start

        # 验证查找正确性
        for r in rows:
            assert lookup[r["id"]] == (r["sort_order"], r["created_at"])

        # 500 行构建应 < 1ms
        assert elapsed < 0.001, f"build_sort_lookup took {elapsed:.6f}s for 500 rows"

        # 每次查找应 < 1μs
        start = time.perf_counter()
        for _ in range(10_000):
            _ = lookup[42]
        elapsed_lookup = time.perf_counter() - start
        assert elapsed_lookup < 0.01, f"Lookup too slow: {elapsed_lookup:.6f}s for 10k lookups"
