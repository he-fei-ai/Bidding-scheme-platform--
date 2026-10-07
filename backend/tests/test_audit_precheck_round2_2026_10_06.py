"""审核与预检模块第二轮增强测试（2026-10-06）。

覆盖本轮两处修复：

BUG-1（P1·正文污染）：一致性「采纳建议并修复」此前把整段建议文案
（"建议将以下内容统一为：450（已确认…）\\n待修正取值：45"）当 new_value
提交 autofix，会把正文旧值替换成说明文字。现后端结构化下发 target_value。

BUG-2（P2·事件循环阻塞）：duplicate /status /result 此前在 async def 里
同步直调写锁 DB 的 duplicate.get_task（前端每 2s 轮询）。现移出事件循环。
"""
from __future__ import annotations

import asyncio
import json

import pytest

from app.db import get_db, init_db


# ---------------- fixture 工具（与 test_facts_module_interactions 同口径） ----------------

def _new_project(name: str) -> int:
    init_db()
    with get_db() as conn:
        conn.execute("INSERT INTO projects (name, industry) VALUES (?, ?)", (name, "通用"))
        return conn.execute("SELECT id FROM projects ORDER BY id DESC LIMIT 1").fetchone()["id"]


def _seed_analysis(project_id: int, items: list) -> None:
    with get_db() as conn:
        conn.execute(
            "INSERT INTO analysis (project_id, global_facts_json, facts_policy) VALUES (?, ?, ?) "
            "ON CONFLICT(project_id) DO UPDATE SET global_facts_json = excluded.global_facts_json",
            (project_id, json.dumps(items, ensure_ascii=False), "fabricate"),
        )


def _seed_section(project_id: int, title: str, content: str, sort_order: int = 0) -> None:
    with get_db() as conn:
        conn.execute(
            "INSERT INTO sections (project_id, title, content, sort_order, status) "
            "VALUES (?, ?, ?, ?, 'completed')",
            (project_id, title, content, sort_order),
        )


# ---------------- BUG-1：target_value 必须是纯目标值，不是整段文案 ----------------

class TestSuggestTargetValueIsPlainValue:
    def _seed(self, facts: list, contents: list[str]) -> int:
        pid = _new_project("审核-目标值契约")
        _seed_analysis(pid, facts)
        for i, c in enumerate(contents):
            _seed_section(pid, f"第{i+1}章", c, sort_order=i)
        return pid

    def test_target_value_is_plain_baseline_value(self):
        """有全局基准时，target_value 应是纯值「450」，而非整段建议文案。"""
        from app.services.consistency_auditor import ConsistencyAuditor

        pid = self._seed(
            [{"key": "d", "label": "工期", "value": "工期：450天", "category": "schedule"}],
            ["工期：500天。", "工期：500天。"],
        )
        auditor = ConsistencyAuditor()
        conflicts = [c for c in auditor.detect_conflicts(pid) if c.fact_name == "工期"]
        assert conflicts

        tv = auditor.suggest_fix_target_value(conflicts[0].conflict_id, pid)
        text = auditor.suggest_fixes(conflicts[0].conflict_id, pid)

        # 关键断言：target_value 是纯值，不含文案前缀
        assert tv == "450", f"target_value 应为纯目标值，实际：{tv!r}"
        assert tv not in ("", None)
        assert "建议" not in tv
        assert "统一为" not in tv
        # suggestion 文案保持向后兼容
        assert text and "统一为：450" in text

    def test_target_value_falls_back_to_summary(self):
        """无全局基准时，target_value 回退到摘要口径（与 suggestion 同源）。"""
        from app.services.consistency_auditor import ConsistencyAuditor

        pid = _new_project("审核-目标值摘要兜底")
        # 无 analysis 全局事实，靠 projects.notes 作为摘要来源
        with get_db() as conn:
            conn.execute("UPDATE projects SET notes = ? WHERE id = ?", ("工期：300天。", pid))
        _seed_section(pid, "第一章", "工期：450天。", sort_order=0)
        _seed_section(pid, "第二章", "工期：450天。", sort_order=1)

        auditor = ConsistencyAuditor()
        conflicts = [c for c in auditor.detect_conflicts(pid) if c.fact_name == "工期"]
        assert conflicts
        tv = auditor.suggest_fix_target_value(conflicts[0].conflict_id, pid)
        assert tv == "300", f"无基准时应回退摘要口径 300，实际：{tv!r}"

    def test_target_value_matches_apply_autofix_unified_value(self):
        """防回归：target_value 必须能直接喂给 autofix 作为 unified_value。

        这是 BUG-1 的本质校验——前端把 target_value 当 new_value 提交，
        后端应能把它当作合法统一值落文，而不是把说明文字写进正文。
        """
        from app.services.consistency_auditor import ConsistencyAuditor

        pid = self._seed(
            [{"key": "d", "label": "工期", "value": "工期：450天", "category": "schedule"}],
            ["工期：500天。", "工期：500天。"],
        )
        auditor = ConsistencyAuditor()
        conflicts = [c for c in auditor.detect_conflicts(pid) if c.fact_name == "工期"]
        assert conflicts
        tv = auditor.suggest_fix_target_value(conflicts[0].conflict_id, pid)
        assert tv and " " not in tv and "：" not in tv, "target_value 必须是可直接替换的纯值"


# ---------------- BUG-2：duplicate 轮询端点让出事件循环 ----------------

class TestDuplicateStatusYieldsLoop:
    """行为层：把底层 get_task 换成耗时 0.25s 的同步函数，事件循环里跑心跳协程。

    若端点仍同步直调，loop 被卡死，心跳几乎不推进；
    若正确 to_thread，心跳正常推进。
    """

    def test_status_endpoint_yields_control(self, monkeypatch):
        import app.routers.duplicate as dup_router
        from app.services import duplicate as dup_svc

        async def _fake_access(*a, **k):
            return None

        monkeypatch.setattr(dup_router, "enforce_task_access", _fake_access)

        # 替换成耗时同步函数，模拟写锁 DB 往返
        def _slow_get_task(task_id):
            import time
            time.sleep(0.25)
            return {"id": task_id, "status": "completed", "progress": 100,
                    "progress_message": "done", "error": ""}

        monkeypatch.setattr(dup_svc, "get_task", _slow_get_task)

        beats = {"n": 0}

        async def _heartbeat():
            for _ in range(40):
                await asyncio.sleep(0.005)
                beats["n"] += 1

        async def call_endpoint():
            return await dup_router.get_status(1, user=object())

        async def main():
            hb = asyncio.create_task(_heartbeat())
            result = await call_endpoint()
            hb.cancel()
            return result

        result = asyncio.run(main())
        assert result["status"] == "completed"
        # 若同步直调 0.25s，心跳只能推进 ~1-2 次；to_thread 后应 ≥ 5 次
        assert beats["n"] >= 5, (
            f"duplicate /status 似乎仍在事件循环内同步直调 get_task：心跳仅推进 "
            f"{beats['n']} 次"
        )
