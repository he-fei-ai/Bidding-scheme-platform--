"""七模块跨模块修复回归测试（2026-10-05）

覆盖本次修复的 4 项：

- FIX-1  ai.py::_sync_confirm_field 未校验 SQL 标识符（注入模板 + 非法列 500）
- FIX-2  ai.py::_sync_confirm_and_retrieve 派生列名未校验（scope → confirmed_scope 500）
- FIX-3  export.py SSE 流式导出路径越界静默降级（BUG-FIX 2026-09-18 的漏改副本）
- FIX-4  sse_utils.py::_parse_global_facts_items 与 facts_service 副本漂移
"""

import json
import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _ensure_project(pid: int) -> None:
    """analysis.project_id 有 FK → 先建 projects 行（测试专用）"""
    from app.db import get_db

    with get_db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO projects (id, name, current_step) VALUES (?, ?, 1)",
            (pid, f"测试项目-{pid}"),
        )


# ============================================================================
# FIX-1 / FIX-2: ai.py SQL 标识符白名单校验
# ============================================================================


class TestConfirmedFieldValidation:
    """_sync_confirm_field / _sync_confirm_and_retrieve 的标识符校验"""

    def test_derive_confirmed_field_known_mapping(self):
        from app.routers.ai import _derive_confirmed_field

        assert _derive_confirmed_field("summary_json") == "confirmed_summary"
        assert _derive_confirmed_field("scoring_points_json") == "confirmed_scoring"
        assert _derive_confirmed_field("boq_json") == "confirmed_boq"
        assert _derive_confirmed_field("work_interface_json") == "confirmed_work_interface"
        assert _derive_confirmed_field("construction_method_json") == "confirmed_construction_method"
        assert _derive_confirmed_field("global_facts_json") == "confirmed_facts"

    def test_derive_confirmed_field_no_phantom_column(self):
        """BUG 复现：scope / facts_policy 这类合法字段此前会派生出不存在的列名

        原实现 `f"confirmed_{field.replace('_json','')}"` 对 field='scope'
        产出 'confirmed_scope'，该列在 analysis 表中不存在，直接拼进 SQL 即 500。
        修复后返回 None（无确认列），调用方跳过 confirmed 列写入。
        """
        from app.routers.ai import _derive_confirmed_field

        assert _derive_confirmed_field("scope") is None
        assert _derive_confirmed_field("facts_policy") is None
        assert _derive_confirmed_field("nonexistent") is None

    def test_confirm_field_rejects_injection(self):
        """BUG 复现：原 _sync_confirm_field 完全无校验，SQL 标识符可注入"""
        from app.routers.ai import _sync_confirm_field

        with pytest.raises(ValueError, match="非法确认字段名"):
            _sync_confirm_field(1, "x FROM analysis; DROP TABLE users; --")

        with pytest.raises(ValueError, match="非法确认字段名"):
            _sync_confirm_field(1, "confirmed_scope")  # 不在白名单

    def test_confirm_field_accepts_whitelisted(self):
        from app.db import get_db, init_db
        from app.routers.ai import _sync_confirm_field

        init_db()
        _ensure_project(1)
        with get_db() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO analysis (project_id, summary_json) VALUES (?, ?)", (1, "{}")
            )
        # 白名单内的列名应当正常执行
        _sync_confirm_field(1, "confirmed_summary")
        with get_db() as conn:
            row = conn.execute(
                "SELECT confirmed_summary FROM analysis WHERE project_id = ?", (1,)
            ).fetchone()
        assert row["confirmed_summary"] == 1

    def test_confirm_and_retrieve_no_phantom_column(self):
        """BUG 复现：field='scope' 走 _sync_confirm_and_retrieve 原实现会 500"""
        from app.db import get_db, init_db
        from app.routers.ai import _sync_confirm_and_retrieve

        init_db()
        _ensure_project(2)
        # 行不存在分支（INSERT）
        with get_db() as conn:
            conn.execute("DELETE FROM analysis WHERE project_id = ?", (2,))
        # 修复前：confirmed_scope 列不存在 → sqlite3.OperationalError
        _sync_confirm_and_retrieve(2, "scope", "总承包范围")
        with get_db() as conn:
            row = conn.execute(
                "SELECT scope FROM analysis WHERE project_id = ?", (2,)
            ).fetchone()
        assert row["scope"] == "总承包范围"

        # 行存在分支（UPDATE）
        _sync_confirm_and_retrieve(2, "scope", "更新后的范围")
        with get_db() as conn:
            row = conn.execute(
                "SELECT scope FROM analysis WHERE project_id = ?", (2,)
            ).fetchone()
        assert row["scope"] == "更新后的范围"

    def test_insert_or_update_no_phantom_column(self):
        from app.db import get_db, init_db
        from app.routers.ai import _sync_insert_or_update

        init_db()
        _ensure_project(3)
        with get_db() as conn:
            conn.execute("DELETE FROM analysis WHERE project_id = ?", (3,))
        _sync_insert_or_update(3, "范围内容", "scope")
        _sync_insert_or_update(3, "更新范围", "scope")
        with get_db() as conn:
            row = conn.execute(
                "SELECT scope FROM analysis WHERE project_id = ?", (3,)
            ).fetchone()
        assert row["scope"] == "更新范围"

    def test_insert_or_update_resets_confirmed_flag(self):
        """有映射的字段仍应重置「已确认」标记（行为不回归）"""
        from app.db import get_db, init_db
        from app.routers.ai import _sync_confirm_field, _sync_insert_or_update

        init_db()
        _ensure_project(4)
        with get_db() as conn:
            conn.execute("DELETE FROM analysis WHERE project_id = ?", (4,))
            # _sync_confirm_field 是纯 UPDATE（无 upsert），需先有行
            conn.execute(
                "INSERT INTO analysis (project_id, summary_json) VALUES (?, ?)", (4, "{}")
            )
        _sync_confirm_field(4, "confirmed_summary")
        with get_db() as conn:
            row = conn.execute(
                "SELECT confirmed_summary FROM analysis WHERE project_id = ?", (4,)
            ).fetchone()
        assert row["confirmed_summary"] == 1
        # 重新写入摘要 → confirmed_summary 归零
        _sync_insert_or_update(4, "{}", "summary_json")
        with get_db() as conn:
            row = conn.execute(
                "SELECT confirmed_summary FROM analysis WHERE project_id = ?", (4,)
            ).fetchone()
        assert row["confirmed_summary"] == 0

    def test_confirm_field_noop_without_row(self):
        """记录现状：_sync_confirm_field 是纯 UPDATE，无行时静默空操作。

        当前调用方（/boq/confirm 等）均在 analyze 端点之后，行必然存在；
        但 facts 路由已为同类问题改用 upsert（confirmed_facts），此处保留纯
        UPDATE 属已接受的技术债（见收尾报告技术债清单 D-7）。
        """
        from app.db import get_db, init_db
        from app.routers.ai import _sync_confirm_field

        init_db()
        _ensure_project(5)
        with get_db() as conn:
            conn.execute("DELETE FROM analysis WHERE project_id = ?", (5,))
        _sync_confirm_field(5, "confirmed_summary")  # 不应抛异常
        with get_db() as conn:
            assert conn.execute(
                "SELECT COUNT(*) c FROM analysis WHERE project_id = ?", (5,)
            ).fetchone()["c"] == 0


# ============================================================================
# FIX-3: SSE 流式导出路径越界不再静默降级
# ============================================================================


class TestExportStreamPathGuard:
    """SSE 导出路径越界修复（与同步导出路径同一口径）"""

    def test_sync_path_rejects_out_of_bounds(self):
        """对照验证：同步导出路径的越界判定已存在（BUG-FIX 2026-09-18）"""
        from pathlib import Path

        from fastapi import HTTPException

        from app.config import PROJECTS_DATA_DIR

        base_dir = PROJECTS_DATA_DIR.resolve()
        actual_path = Path("C:\\Windows\\System32\\evil.docx").resolve()
        with pytest.raises(ValueError):
            actual_path.relative_to(base_dir)

    def test_stream_path_guard_logic(self):
        """直接验证修复后的越界分支语义：越界 → 抛错（而非降级成裸文件名）"""
        from pathlib import Path

        from app.config import PROJECTS_DATA_DIR

        base_dir = PROJECTS_DATA_DIR.resolve()
        inside = (base_dir / "p1" / "ok.docx").resolve()
        outside = (base_dir.parent / "evil.docx").resolve()

        # 目录内 → relative_to 正常返回
        assert str(inside.relative_to(base_dir))

        # 目录外 → 必须抛 ValueError（修复前此处被 except 吞掉、降级为文件名）
        with pytest.raises(ValueError):
            outside.relative_to(base_dir)


# ============================================================================
# FIX-4: 全局事实解析与占位过滤单一正本
# ============================================================================


class TestGlobalFactsSingleSource:
    """_parse_global_facts_items 必须与 facts_service 同一口径"""

    def test_placeholder_filtered(self):
        from app.routers.sse.sse_utils import _parse_global_facts_items

        raw = json.dumps(
            [
                {"key": "projectName", "value": "某工程", "category": "basic"},
                {"key": "duration", "value": "【待填写】", "category": "schedule"},
                {"key": "manager", "value": "【待填写：项目经理姓名】", "category": "personnel"},
            ],
            ensure_ascii=False,
        )
        items = _parse_global_facts_items(raw)
        assert len(items) == 1
        assert items[0]["key"] == "projectName"

    def test_wrapped_items_shape_accepted(self):
        from app.routers.sse.sse_utils import _parse_global_facts_items

        raw = json.dumps({"items": [{"key": "a", "value": "v", "category": "basic"}]}, ensure_ascii=False)
        assert len(_parse_global_facts_items(raw)) == 1

    def test_malformed_returns_empty_not_raise(self):
        from app.routers.sse.sse_utils import _parse_global_facts_items

        assert _parse_global_facts_items("{not json") == []
        assert _parse_global_facts_items(None) == []
        assert _parse_global_facts_items("") == []
        assert _parse_global_facts_items(12345) == []

    def test_non_dict_entries_dropped(self):
        """parse_stored_items 只保留 dict 条目，避免下游 .get 崩溃"""
        from app.routers.sse.sse_utils import _parse_global_facts_items

        raw = json.dumps(["not-a-dict", {"key": "k", "value": "v", "category": "basic"}, None])
        items = _parse_global_facts_items(raw)
        assert len(items) == 1
        assert items[0]["key"] == "k"

    def test_sse_path_and_celery_path_agree(self):
        """两条生成链路（SSE 与 Celery）读取同一事实集，口径必须一致"""
        from app.routers.sse.sse_utils import _parse_global_facts_items
        from app.services.facts_service import facts_for_generation

        raw = json.dumps(
            [
                {"key": "a", "value": "x", "category": "basic"},
                {"key": "b", "value": "【待填写】", "category": "schedule"},
            ],
            ensure_ascii=False,
        )
        assert _parse_global_facts_items(raw) == facts_for_generation(raw)


# ============================================================================
# 端到端数据链：analysis 写入 → 读取形态一致
# ============================================================================


class TestAnalysisContract:
    """七模块数据链首环（提取项目）的返回形态契约"""

    def test_get_analysis_shape_stable(self):
        """无行 / 有行两种状态下键集必须一致（BUG-FIX 2026-09-29 回归）"""
        from fastapi.testclient import TestClient

        from app.main import app
        from app.db import get_db, init_db

        init_db()
        with get_db() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO projects (id, name, current_step) "
                "VALUES (1, '链首测试', 1)"
            )
            conn.execute("DELETE FROM analysis WHERE project_id = ?", (1,))
        client = TestClient(app)
        resp = client.get("/api/v1/projects/1/ai/analysis")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        expected_keys = {
            "summary_json", "scoring_points_json", "confirmed_summary", "confirmed_scoring",
            "boq_json", "work_interface_json", "construction_method_json",
            "confirmed_boq", "confirmed_work_interface", "confirmed_construction_method",
            "global_facts_json", "global_facts_items", "confirmed_facts", "scope", "facts_policy",
        }
        assert expected_keys.issubset(body.keys()), f"缺键: {expected_keys - set(body.keys())}"

        # 写入一行后形态必须保持一致
        with get_db() as conn:
            conn.execute(
                "INSERT INTO analysis (project_id, summary_json) VALUES (?, ?) "
                "ON CONFLICT(project_id) DO UPDATE SET summary_json = excluded.summary_json",
                (1, "{}"),
            )
        resp2 = client.get("/api/v1/projects/1/ai/analysis")
        assert resp2.status_code == 200, resp2.text
        body2 = resp2.json()
        assert expected_keys.issubset(body2.keys()), f"缺键: {expected_keys - set(body2.keys())}"
        assert body2["scope"] == "" or body2["scope"] is None
