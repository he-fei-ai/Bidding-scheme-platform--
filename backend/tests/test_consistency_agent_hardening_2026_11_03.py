"""一致性 Agent 修复遗留建议加固回归（2026-11-03）。

来源（一致性校核与导出文档模块_第九轮深度加固_2026-10-27.md §7）：

- 建议4：为 `/consistency/agent` 链路补一条「事件循环不被阻塞」的**行为级**
  测试（此前仅有 R7 源码契约钉住 to_thread，无真实调度行为证明）。
- 建议3：Agent 修复 `unresolved` 跨轮去重 —— 模型重复上报同一
  (section_id, reason) 时 UI 计数虚高，按二元组去重、首见保留。

运行：
    cd backend
    python -m pytest tests/test_consistency_agent_hardening_2026_11_03.py -q
"""
from __future__ import annotations

import asyncio
import contextlib
import time

from app.db import get_db, init_db

OWNER = 100


def _new_user_project_with_conflict(name: str, conflict_id: str) -> int:
    init_db()
    with get_db() as conn:
        conn.execute(
            "INSERT INTO projects (name, industry, created_by) VALUES (?, ?, ?)",
            (name, "通用", OWNER),
        )
        pid = conn.execute(
            "SELECT id FROM projects ORDER BY id DESC LIMIT 1"
        ).fetchone()["id"]
        conn.execute(
            "INSERT INTO sections (project_id, title, level, sort_order, content, status) "
            "VALUES (?, '第一节 总体部署', 1, 0, ?, 'completed')",
            (pid, "本项目总体部署依据招标文件与国家标准编制。"),
        )
        conn.execute(
            "INSERT INTO consistency_conflicts "
            "(project_id, conflict_id, fact_name, values_json, sources_json, "
            " severity, description, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                pid,
                conflict_id,
                "工期",
                '[{"value": "30天", "source": "第一节 总体部署", "section_id": 1}]',
                "[]",
                "high",
                f"测试冲突 {conflict_id}",
                "2026-11-03 00:00:00",
            ),
        )
        return pid


# ============================================================
# 建议3：unresolved 跨轮去重
# ============================================================


class TestUnresolvedDedup:
    def test_same_signature_deduped_across_and_within_turns(self, monkeypatch):
        """同轮重复 + 跨轮复述的 (section_id, reason) 只登记一次。"""
        from app.services import agent_repair

        pid = _new_user_project_with_conflict("Agent去重-项目", "dup-c1")
        calls = {"n": 0}

        async def fake_turn(provider, ctx, feedback, turn):
            calls["n"] += 1
            if calls["n"] == 1:
                return {
                    "done": False,
                    "actions": [],
                    "unresolved": [
                        {"section_id": "1", "reason": "无法唯一匹配 old_text"},
                        {"section_id": "1", "reason": "无法唯一匹配 old_text"},  # 同轮重复
                        {"section_id": "2", "reason": "缺上下文"},
                    ],
                }
            return {
                "done": True,
                "actions": [],
                "unresolved": [
                    {"section_id": "1", "reason": "无法唯一匹配 old_text"},  # 跨轮复述
                ],
            }

        monkeypatch.setattr(agent_repair, "_agent_turn", fake_turn)
        report = asyncio.run(
            agent_repair.run_agent_repair(pid, dry_run=True, provider=object())
        )
        assert report["turns"] == 2
        sigs = [(u["section_id"], u["reason"]) for u in report["unresolved"]]
        assert len(sigs) == 2, (
            f"同 (section_id, reason) 只应登记一次，实际 {sigs}"
        )
        assert set(sigs) == {("1", "无法唯一匹配 old_text"), ("2", "缺上下文")}

    def test_distinct_unresolved_entries_all_kept(self, monkeypatch):
        """回归对照：不同 (section_id, reason) 全部保留，去重不误伤。"""
        from app.services import agent_repair

        pid = _new_user_project_with_conflict("Agent去重对照-项目", "dup-c2")

        async def fake_turn(provider, ctx, feedback, turn):
            return {
                "done": True,
                "actions": [],
                "unresolved": [
                    {"section_id": "1", "reason": "原因甲"},
                    {"section_id": "1", "reason": "原因乙"},  # 同 section 不同 reason → 保留
                    {"section_id": "2", "reason": "原因甲"},  # 不同 section 同 reason → 保留
                ],
            }

        monkeypatch.setattr(agent_repair, "_agent_turn", fake_turn)
        report = asyncio.run(
            agent_repair.run_agent_repair(pid, dry_run=True, provider=object())
        )
        assert len(report["unresolved"]) == 3

    def test_malformed_unresolved_items_still_skipped(self, monkeypatch):
        """回归对照：非 dict / 全空字段条目维持原有过滤行为。"""
        from app.services import agent_repair

        pid = _new_user_project_with_conflict("Agent去重畸形-项目", "dup-c3")

        async def fake_turn(provider, ctx, feedback, turn):
            return {
                "done": True,
                "actions": [],
                "unresolved": [
                    "不是字典",
                    {},
                    {"section_id": "", "reason": ""},
                    {"section_id": "1", "reason": "合法条目"},
                ],
            }

        monkeypatch.setattr(agent_repair, "_agent_turn", fake_turn)
        report = asyncio.run(
            agent_repair.run_agent_repair(pid, dry_run=True, provider=object())
        )
        assert report["unresolved"] == [
            {"section_id": "1", "reason": "合法条目"}
        ]


# ============================================================
# 建议4：事件循环不被阻塞（行为级）
# ============================================================


class TestEventLoopNotBlocked:
    def test_workspace_prep_off_loop_heartbeat_progresses(self, monkeypatch):
        """行为级证明：工作区准备（同步重操作）执行期间事件循环可调度其他协程。

        与 R7 源码契约（assert "asyncio.to_thread(build_agent_workspace" in src）
        互补：契约钉写法，本用例钉真实调度行为 —— 若有人把 to_thread 摘掉改回
        直接调用，0.6s 的同步 sleep 将冻结心跳任务，本用例即红。
        """
        from app.services import agent_repair

        def slow_workspace(project_id, base):  # 模拟全量正文读取等同步重操作
            time.sleep(0.6)
            return {"sections": [], "conflicts": [], "ai_findings": []}

        monkeypatch.setattr(agent_repair, "build_agent_workspace", slow_workspace)
        beats = {"n": 0}

        async def heartbeat():
            while True:
                await asyncio.sleep(0.01)
                beats["n"] += 1

        async def main():
            hb = asyncio.create_task(heartbeat())
            try:
                return await agent_repair.run_agent_repair(
                    999999999, dry_run=True, provider=object()
                )
            finally:
                hb.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await hb

        report = asyncio.run(main())
        # 空 sections 早退：证明确实走完了工作区准备路径且未触碰 provider
        assert report["message"] == "没有可供修复的正文内容"
        # 0.6s 阻塞窗口内 10ms 心跳应推进 ≥20 次；若事件循环被冻结则接近 0
        assert beats["n"] >= 20, (
            f"工作区准备期间事件循环心跳仅推进 {beats['n']} 次 —— "
            "同步重操作疑似被移回事件循环内直接执行"
        )
