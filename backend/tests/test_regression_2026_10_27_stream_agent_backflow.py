"""第九轮加固回归测试（2026-10-27）：导出 SSE 流生命周期 + Agent 修复 + 事实回流。

对应本轮修复（全部有代码依据，基线 1074 passed 全绿后新增）：

- R1（P1）SSE 导出「启动前取消」组锁永久泄漏：构建任务 / connected 事件 /
  drain 线程提前到路由体启动，生成器不被迭代也不影响放锁。
- R2（P3）drain 线程 1h 空闲超时封存时无任何事件：补合成 error 终态事件。
- R3（P3）内联图表缓存命中率计数器重绑定（`from x import int; int += 1`
  只改局部名）：改为模块属性访问后计数器真实增长。
- R4（P2）Agent 修复 `rejected` 条目契约错位（原始 action dict 丢失宿主
  拒绝原因，前端每条显示兜底文案）：归一化为 {section_id, title, error}。
- R5（P3）Agent 修复单次非法 JSON 终结全会话：改为反馈 + 续跑（max_turns
  封顶不变）。
- R6（P2）`plan_fact_merge` dedup 撞键静默丢弃：计入 skipped 并带可读原因。
- R7（P1/P3 源码契约）run_agent_repair 同步重操作移入 asyncio.to_thread；
  build_agent_workspace 纯读改 readonly 连接。

运行：
    cd backend
    python -m pytest tests/test_regression_2026_10_27_stream_agent_backflow.py -v
"""
from __future__ import annotations

import asyncio
import inspect
import queue
import time
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.db import get_db, init_db
from app.models import ExportConfig
from app.security.auth import User

OWNER = 100


def _new_user_project_with_section(name: str) -> int:
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
            (pid, "本项目总体部署依据招标文件与国家标准编制，含进度计划与资源配置安排。"),
        )
        return pid


def _seed_conflict(pid: int, conflict_id: str, fact_name: str = "工期") -> None:
    with get_db() as conn:
        conn.execute(
            "INSERT INTO consistency_conflicts "
            "(project_id, conflict_id, fact_name, values_json, sources_json, "
            " severity, description, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                pid,
                conflict_id,
                fact_name,
                '[{"value": "30天", "source": "第一节 总体部署", "section_id": 1}]',
                "[]",
                "high",
                f"测试冲突 {conflict_id}",
                "2026-10-27 00:00:00",
            ),
        )


class _FakeRequest:
    async def is_disconnected(self) -> bool:
        return False


# ============================================================
# R1：SSE「启动前取消」不再泄漏导出组锁
# ============================================================


class TestSSEStreamLifecycle:
    def test_generator_never_iterated_still_releases_lock_and_records_terminal(self):
        """客户端在首个 yield 前取消（生成器未被迭代）→ 构建照常启动、锁照常释放。

        回归背景：原实现 `_run_build` 的 create_task 在 event_stream() 生成器
        第一次迭代时才执行，客户端断开即永不启动，锁与 keepalive 永久悬挂。
        """
        from app.routers import export as er

        pid = _new_user_project_with_section("R1锁泄漏回归-项目")
        resp = asyncio.run(
            er.export_docx_stream(
                _FakeRequest(), pid, ExportConfig(), identity=User(OWNER, "o", "user")
            )
        )
        # 关键回归动作：**不迭代** body_iterator（模拟 Starlette 在首次拉取
        # 生成器前取消 StreamingResponse 的启动前取消场景）。

        # connected 事件已在路由体注册为 seq 1
        journal = None
        with er._STREAM_JOURNAL_LOCK:
            for j in reversed(list(er._STREAM_JOURNALS.values())):
                if j.project_id == pid:
                    journal = j
                    break
        assert journal is not None, "路由体必须注册 journal"
        assert journal.events and journal.events[0][1]["event"] == "connected"
        assert journal.events[0][0] == 1, "connected 必须是 seq=1（前端 resume 锚点）"

        # 构建结束后锁必然释放：同项目再次抢锁成功即证明无泄漏
        deadline = time.time() + 90
        acquired = None
        while time.time() < deadline:
            try:
                acquired = asyncio.run(er._acquire_export_lock(pid))
                break
            except HTTPException as e:
                if e.status_code != 409:
                    raise
                time.sleep(0.3)
        assert acquired is not None, (
            "90s 内导出锁仍未释放 —— 启动前取消场景组锁泄漏回归（R1 未生效）"
        )
        try:
            assert journal.terminal, "构建结束后 journal 必须封存为终态"
            events = [e for _, e in journal.events]
            terminal = [e for e in events if e.get("event") in ("completed", "error")]
            assert terminal, "journal 必须包含显式终态事件（completed/error）"
            assert terminal[0]["event"] == "completed", (
                f"单章节正常构建应产出 completed 终态，实际: {terminal[0]['event']}"
            )
        finally:
            asyncio.run(er._release_export_lock(acquired[0], acquired[1], why="R1 测试清理"))

    def test_drain_idle_timeout_appends_synthetic_error(self, monkeypatch):
        """R2：drain 空闲超时封存时必须补合成 error 事件（resume 端可见终态原因）。"""
        from app.routers import export as er

        monkeypatch.setattr(er, "_DRAIN_IDLE_TIMEOUT_SECONDS", 0.05)
        j = er._StreamJournal("drain-timeout-r2", 1, queue.Queue())
        er._drain_queue_to_journal(j)
        deadline = time.time() + 5
        while not j.terminal and time.time() < deadline:
            time.sleep(0.02)
        assert j.terminal, "drain 空闲超时后必须封存 journal"
        events = [e for _, e in j.events]
        assert events, "封存前必须落至少一条事件（合成 error）"
        assert events[-1]["event"] == "error"
        assert "等待超时" in (events[-1].get("detail") or "")

    def test_inline_marker_cache_counters_actually_increment(self):
        """R3：命中/未命中计数必须是模块级真实增长（命中率统计不再恒 0.0%）。"""
        from app.services import export_cache
        from app.services import export_chart_pipeline as ecp

        sec = SimpleNamespace(
            id=987654321987, content="普通正文，不含任何图表标记。"
        )
        with export_cache._inline_marker_cache_lock:
            export_cache._inline_marker_cache.pop(sec.id, None)
        misses0 = export_cache._inline_marker_cache_misses
        hits0 = export_cache._inline_marker_cache_hits
        ecp._get_cached_inline_chart_markers(sec)  # 首次：miss
        ecp._get_cached_inline_chart_markers(sec)  # 二次：hit
        assert export_cache._inline_marker_cache_misses == misses0 + 1, (
            "模块级 misses 计数未增长 —— 若仍为局部重绑定 BUG 此处必失败"
        )
        assert export_cache._inline_marker_cache_hits == hits0 + 1, (
            "模块级 hits 计数未增长 —— 若仍为局部重绑定 BUG 此处必失败"
        )


# ============================================================
# R4/R5：Agent 修复报告契约与容错
# ============================================================


class TestAgentRepairContract:
    def test_rejected_entries_carry_host_reason(self, monkeypatch):
        """R4：被拒补丁必须归一化为 {section_id, title, error}，带宿主拒绝原因。"""
        from app.services import agent_repair

        pid = _new_user_project_with_section("R4拒绝契约-项目")
        _seed_conflict(pid, "r4-c1")
        sid_row = None
        with get_db(readonly=True) as conn:
            sid_row = conn.execute(
                "SELECT id FROM sections WHERE project_id = ? LIMIT 1", (pid,)
            ).fetchone()

        async def fake_turn(provider, ctx, feedback, turn):
            return {
                "done": True,
                "actions": [
                    {
                        "action": "replace",
                        "section_id": str(sid_row["id"]),
                        "old_text": "正文里根本没有这句话XYZ",
                        "new_text": "替换文本",
                        "reason": "测试：old_text 不存在",
                    }
                ],
                "unresolved": [],
            }

        monkeypatch.setattr(agent_repair, "_agent_turn", fake_turn)
        report = asyncio.run(
            agent_repair.run_agent_repair(pid, dry_run=True, provider=object())
        )
        assert report["rejected"], "不存在的 old_text 必须产生被拒条目"
        entry = report["rejected"][0]
        assert set(entry.keys()) == {"section_id", "title", "error"}, (
            f"rejected 条目契约应为 {{section_id, title, error}}，实际 {sorted(entry)}"
        )
        assert entry["section_id"] == str(sid_row["id"])
        assert entry["title"] == "第一节 总体部署"
        assert "未找到" in entry["error"], (
            "必须携带宿主真实拒绝原因（而非丢弃后由前端兜底「匹配失败」）"
        )

    def test_unparseable_turn_feeds_back_and_continues(self, monkeypatch):
        """R5：单次非法 JSON 不再烧掉剩余轮数 —— 反馈后续跑。"""
        from app.services import agent_repair

        pid = _new_user_project_with_section("R5解析容错-项目")
        _seed_conflict(pid, "r5-c1")
        calls = {"n": 0}

        async def fake_turn(provider, ctx, feedback, turn):
            calls["n"] += 1
            if calls["n"] == 1:
                return None  # 模拟 max_tokens 截断 / 非法 JSON
            assert "不可解析" in feedback, (
                "第二轮反馈必须包含「上轮返回不可解析」提示（反馈回路）"
            )
            return {"done": True, "actions": [], "unresolved": []}

        monkeypatch.setattr(agent_repair, "_agent_turn", fake_turn)
        report = asyncio.run(
            agent_repair.run_agent_repair(pid, dry_run=True, provider=object())
        )
        assert calls["n"] == 2, "解析失败后必须继续下一轮（而非 break 终结会话）"
        assert report["turns"] == 2
        assert any("不可解析" in e for e in report["errors"])

    def test_run_agent_repair_offloads_sync_db_to_thread(self):
        """R7（契约）：async 里的同步重操作必须走 asyncio.to_thread（事件循环不阻塞）。"""
        from app.services import agent_repair

        src = inspect.getsource(agent_repair.run_agent_repair)
        assert "asyncio.to_thread(build_agent_workspace" in src
        assert "asyncio.to_thread(_persist_changed_sections)" in src
        assert "asyncio.to_thread(\n            invalidate_content_caches" in src

    def test_build_agent_workspace_uses_readonly_connection(self):
        """R7（契约）：纯读工作区准备不得抢全局写锁。"""
        from app.services import agent_repair

        src = inspect.getsource(agent_repair.build_agent_workspace)
        assert "get_db(readonly=True)" in src
        assert "with get_db() as conn" not in src


# ============================================================
# R6：plan_fact_merge dedup 撞键必须计入 skipped
# ============================================================


class TestFactBackflowMerge:
    def test_dedup_collision_records_skipped_reason(self):
        """R6：key 撞既有条目（跨轮 rescan 新 winner / 重复采纳）→ skipped 带原因。"""
        from app.services.consistency_backflow import plan_fact_merge

        existing = [
            {
                "key": "audit_c1",
                "label": "工期",
                "value": "30天",
                "category": "时间",
                "source": "测试既有条目",
            }
        ]
        # 同一 conflict_id 的新 winner：key 恒为 audit_c1 → 撞键
        proposals = [
            {
                "conflict_id": "c1",
                "fact_name": "工期",
                "value": "45天",
                "category": "时间",
                "source": "第一节 总体部署",
                "section_id": 1,
            }
        ]
        merged_list, added, skipped = plan_fact_merge(existing, proposals)
        assert len(merged_list) == 1, "撞键候选不得新增事实（仍只有既有条目）"
        assert added == 0, "撞键候选不得计入新增"
        assert len(skipped) == 1, (
            f"dedup 撞键必须计入 skipped（原实现裸 continue 静默丢弃），实际 {skipped}"
        )
        assert "已采纳过" in skipped[0]["reason"]
        assert skipped[0]["value"] == "45天"
        assert skipped[0]["fact_name"] == "工期"

    def test_non_conflicting_proposal_still_merged(self):
        """回归对照：不撞键的正常候选合并行为不受 R6 影响。"""
        from app.services.consistency_backflow import plan_fact_merge

        existing = [
            {
                "key": "audit_c2",
                "label": "人员数量",
                "value": "10人",
                "category": "资源",
                "source": "测试既有条目",
            }
        ]
        proposals = [
            {
                "conflict_id": "c9",
                "fact_name": "人员数量",
                "value": "12人",
                "category": "资源",
                "source": "第二节",
                "section_id": 2,
            }
        ]
        merged_list, added, skipped = plan_fact_merge(existing, proposals)
        assert added == 1 and len(merged_list) == 2
        assert skipped == []
