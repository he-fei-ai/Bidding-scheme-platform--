"""2026-09-18 任务层审计 —— 「空任务 / 硬编码 / 运行无结果」修复回归测试

审计对象：Celery 任务层（`app/tasks/__init__.py`、`app/celery_app.py`）与任务
基础设施（`task_lock` / `task_snapshot` / `generation_manager` / `task_registry`）。

A. Celery 层不再是「已注册但永不执行」的空任务
   A1 6 个业务任务全部登记进 TASK_CATALOG（celery 缺失时也可导入）
   A2 未登记的任务名 → 显式 not_dispatched，不抛异常
   A3 超长任务在无 broker 时禁止就地执行（不静默丢弃、也不拖死进程）
   A4 轻量任务可就地执行并返回结构化的派发结果

B. `cleanup_old_results` 真实清理（原先每小时空转并谎报 cleaned）
   B1 心跳超时的僵尸锁被回收
   B2 僵死的运行中快照被收敛为 interrupted
   B3 过期的生成任务与其孤儿进度明细被删除
   B4 返回值字段齐全且为真实计数

C. 导出任务不再重复记账（原先每天多插一条残缺的 exports）
   C1 `export_to_docx_recorded` 透出 export_id
   C2 任务体不再自己 INSERT exports
   C3 任务返回真实 filename 与 export_id

D. 导出预检「全空」判据（P0 回归）
   D1 路由层不再用 `len(empty_sections) == total_sections`
   D2 「1 父 + 1 空叶子」场景下旧判据失效、新判据生效

E. `create_report_schedule` 的 `enabled` 不再被硬编码成 True

F. `cleanup_completed_tasks` 返回真实删除条数并清理孤儿明细

G. `task_registry.cancel_task` 同步清理项目索引（不再泄漏）

H. Celery 任务的死参数不再被静默忽略
"""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path

import pytest


def _new_project(name: str) -> int:
    """在当前测试库中创建一个项目并返回其 id。

    conftest.py 已把 `BID_ASSISTANT_DATA_DIR` 指向独立临时目录并调用过 `init_db()`。
    """
    from app.db import get_db, init_db

    init_db()
    with get_db() as conn:
        conn.execute("INSERT INTO projects (name, industry) VALUES (?, ?)", (name, "通用"))
        return conn.execute("SELECT id FROM projects ORDER BY id DESC LIMIT 1").fetchone()["id"]


def _source_without_comments(path: str) -> str:
    """读取源码并去掉整行注释，避免「修复注释里原文引用旧写法」造成假阳性。"""
    text = Path(path).read_text(encoding="utf-8")
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


@pytest.fixture(autouse=True)
def _release_db_pool_after_each_test():
    """每个用例结束后归还数据库连接池配额。

    `DBPool(max=32)` 是**按线程**分配连接的，而死亡线程占用的连接要闲置 60s
    才被回收。本文件大量使用 `asyncio.run(...)`（每个用例新建一个事件循环），
    很容易把 32 个配额吃满；池一旦耗尽，**后续无关用例**会拿到
    `Database connection pool exhausted (max=32, active=32)`，
    表现为 HTTP 500 或 RuntimeError —— 实测全量跑到 `test_integration.py`
    时就是这样挂的，而单独复跑那几条又全过，极易被误判成自己的回归。

    与 `tests/test_enhancement_2026-09-16_hardening.py`、
    `tests/test_enhancement_2026_09_14.py` 同一套做法。
    """
    yield
    try:
        from app.db import get_pool

        get_pool().close_all()
    except Exception:  # 收尾动作不得反过来让用例失败
        pass


BUSINESS_TASKS = (
    "tasks.file.parse_and_extract",
    "tasks.content.generate_sections",
    "tasks.export.generate_docx",
    "tasks.knowledge.process_document",
    "tasks.check.rejection_check",
    "tasks.check.duplicate_check",
)


# ============================================================================
# A. Celery 层不再是「已注册但永不执行」的空任务
# ============================================================================
class TestACeleryLayerNotDead:
    """BUG-FIX 2026-09-18（高）：celery 是可选依赖，旧实现在导入期就 import 它，
    缺失时 `app.tasks` 整个包 ImportError —— 6 个任务连函数对象都拿不到；
    而即便装了 celery，全库也没有任何 `.delay()` / `.apply_async()` / `.send_task()`
    分发点。两头落空，这就是典型的「空任务运行」。
    """

    def test_a1_all_business_tasks_registered(self):
        from app import celery_app as ca

        names = ca.list_registered_tasks()
        for expected in BUSINESS_TASKS:
            assert expected in names, f"任务 {expected} 未登记，已登记: {names}"
        assert "app.celery_app.cleanup_old_results" in names

    def test_a2_unknown_task_returns_not_dispatched(self):
        from app import celery_app as ca

        result = ca.dispatch_task("tasks.does.not.exist", force_inline=True)
        assert result["status"] == "not_dispatched"
        assert result["task_name"] == "tasks.does.not.exist"

    def test_a3_long_running_task_refuses_inline(self):
        """超长任务（章节生成，time_limit=3600）禁止在 Web 进程内就地执行。

        关键是**不静默丢弃**：必须返回 not_dispatched 并留下可观测的痕迹。
        """
        from app import celery_app as ca

        result = ca.dispatch_task("tasks.content.generate_sections", force_inline=True)
        assert result["status"] == "not_dispatched", (
            "无 broker 时不得把 1 小时级的任务塞进请求线程执行"
        )
        assert result["dispatched_via"] == "none"
        assert "INLINE_BLOCKED_TASKS" in result["error"]

    def test_a4_lightweight_task_runs_inline(self):
        from app import celery_app as ca

        result = ca.dispatch_task("app.celery_app.cleanup_old_results", force_inline=True)
        assert result["dispatched_via"] == "inline"
        assert result["status"] == "ok", f"就地执行失败: {result}"
        assert set(["locks_reclaimed", "snapshots_interrupted", "generation_tasks_deleted"]) <= set(
            result["result"]
        )

    def test_a5_no_dispatch_point_regression_guard(self):
        """`app/tasks/__init__.py` 只登记任务、不自己分发（分发统一走 dispatch_task）。"""
        src = _source_without_comments("app/tasks/__init__.py")
        assert ".delay(" not in src
        assert "apply_async(" not in src


# ============================================================================
# B. cleanup_old_results 真实清理
# ============================================================================
def _insert_expired_lock(project_id: int, lock_key: str) -> None:
    from app.db import get_db
    from app.services import task_lock

    with get_db() as conn:
        task_lock.ensure_schema(conn)
        conn.execute(
            "INSERT INTO task_locks (lock_key, group_name, task_type, project_id, owner_id, "
            "status, acquired_at, heartbeat_at, expires_at, note) "
            "VALUES (?,?,?,?,?, 'active', '2020-01-01 00:00:00', '2020-01-01 00:00:00', "
            "'2020-01-01 00:05:00', 'test')",
            (lock_key, "content-generation", "content-generation", project_id, f"owner-{lock_key}"),
        )


def _read_lock_status(lock_key: str) -> str | None:
    from app.db import get_db

    with get_db(readonly=True) as conn:
        row = conn.execute(
            "SELECT status FROM task_locks WHERE lock_key = ?", (lock_key,)
        ).fetchone()
    return row["status"] if row else None


class TestBCleanupOldResultsDoesRealWork:
    """BUG-FIX 2026-09-18（高）：原实现只打两条日志就 `return {"status": "cleaned"}`，
    注释把责任推给 Celery 的 result_expires —— 但 conf 里从没设过这一项，且它只
    作用于 result backend 的返回值，碰不到 task_locks / background_tasks /
    generation_tasks 三张业务表。僵尸锁会把后续任务永久拦在门外。
    """

    def test_b1_stale_lock_is_reclaimed(self):
        from app.celery_app import cleanup_old_results

        pid = _new_project("B1-僵尸锁")
        key = f"content-generation:{pid}:probe-b1"
        _insert_expired_lock(pid, key)
        assert _read_lock_status(key) == "active"

        result = cleanup_old_results()
        assert result["locks_reclaimed"] >= 1, f"应回收至少 1 个超时锁: {result}"
        assert _read_lock_status(key) == "expired", "超时锁必须被回收为 expired"

    def test_b2_stale_snapshot_becomes_interrupted(self):
        from app.celery_app import cleanup_old_results
        from app.db import get_db
        from app.services import task_snapshot

        pid = _new_project("B2-僵死快照")
        task_id = "snap-probe-b2"
        with get_db() as conn:
            task_snapshot.ensure_schema(conn)
            conn.execute(
                "INSERT INTO background_tasks (task_id, project_id, task_type, status, phase, "
                "progress, current_step, total_steps, message, owner_id, payload_json, "
                "started_at, heartbeat_at, updated_at, acknowledged) "
                "VALUES (?,?,?, 'running', '生成正文', 40, 2, 5, '', 'owner', '{}', "
                "'2020-01-01 00:00:00', '2020-01-01 00:00:00', '2020-01-01 00:00:00', 0)",
                (task_id, pid, "content"),
            )

        result = cleanup_old_results()
        assert result["snapshots_interrupted"] >= 1, f"应收敛至少 1 条僵死快照: {result}"

        with get_db(readonly=True) as conn:
            row = conn.execute(
                "SELECT status FROM background_tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
        assert row["status"] == "interrupted", "僵死快照必须收敛为 interrupted"

    def test_b3_expired_generation_tasks_deleted(self):
        from app.celery_app import cleanup_old_results
        from app.db import get_db

        pid = _new_project("B3-过期生成任务")
        gen_id = "gen-probe-b3"
        with get_db() as conn:
            conn.execute(
                "INSERT INTO generation_tasks (generation_id, project_id, status, outline_version, "
                "started_at, completed_at, total_sections, completed_count, failed_count, "
                "cancelled_count, writing_length, concurrency, lock_key, lock_owner) "
                "VALUES (?,?, 'completed', 1, '2020-01-01 00:00:00', '2020-01-01 01:00:00', "
                "3, 3, 0, 0, 'standard', 2, '', '')",
                (gen_id, pid),
            )
            conn.execute(
                "INSERT INTO section_generation_progress (generation_id, section_id, status, "
                "progress, retry_count, last_error) VALUES (?, 999, 'completed', 100, 0, '')",
                (gen_id,),
            )

        result = cleanup_old_results()
        assert result["generation_tasks_deleted"] >= 1, f"应删除至少 1 条过期任务: {result}"

        with get_db(readonly=True) as conn:
            assert (
                conn.execute(
                    "SELECT COUNT(*) AS c FROM generation_tasks WHERE generation_id = ?", (gen_id,)
                ).fetchone()["c"]
                == 0
            )
            # 主表行删除后，明细不应留成孤儿
            assert (
                conn.execute(
                    "SELECT COUNT(*) AS c FROM section_generation_progress WHERE generation_id = ?",
                    (gen_id,),
                ).fetchone()["c"]
                == 0
            )

    def test_b4_result_shape_is_real_counts(self):
        from app.celery_app import cleanup_old_results

        result = cleanup_old_results()
        assert result["status"] == "ok", f"清理步骤有失败: {result['errors']}"
        assert isinstance(result["locks_reclaimed"], int)
        assert isinstance(result["snapshots_interrupted"], int)
        assert isinstance(result["generation_tasks_deleted"], int)
        assert isinstance(result["errors"], list)


# ============================================================================
# C. 导出任务不再重复记账
# ============================================================================
class TestCExportTaskNoDuplicateLedger:
    """BUG-FIX 2026-09-18（高）：Celery 导出任务在 `build_docx` 已写入 exports 之后，
    又自己 INSERT 了一条残缺记录（config_json 硬编码 '{}'、filename 硬编码
    project_{id}_{ts}.docx、created_at 用本地时间）。前端按 id 取最新一条，
    拿到的正是这条残缺记录 —— 显示的文件名与真实落盘文件对不上。
    """

    def test_c1_recorded_entrypoint_exists(self):
        from app.services.export import export_to_docx_recorded

        assert callable(export_to_docx_recorded)

    def test_c2_task_body_no_longer_inserts_exports(self):
        src = _source_without_comments("app/tasks/__init__.py")
        assert "INSERT INTO exports" not in src, "导出任务不得再自己记账"
        assert "export_to_docx_recorded" in src

    def test_c3_task_returns_real_filename_and_export_id(self, monkeypatch):
        from pathlib import Path as _Path

        from app.services import export as export_mod
        from app.tasks import generate_docx_export

        fake_path = _Path("报价格式标书_20260101_000000.docx")

        monkeypatch.setattr(
            export_mod,
            "export_to_docx_recorded",
            lambda project_id, config=None: (fake_path, 4242),
            raising=True,
        )

        # 用不会与其它用例撞车的 project_id：本任务会抢「export」任务组锁，
        # 若同会话里已有别处持锁，这里会被拦成 failed（非本用例要验证的行为）。
        # BUG-FIX 2026-10-04：任务为 bind=True，代理直调时自动注入 self，
        # 再显式传 _InlineTaskSelf() 会多一个位置参数 → TypeError。
        result = generate_docx_export(987651, {})
        assert result["status"] == "completed"
        assert result["export_id"] == 4242
        assert result["filename"] == "报价格式标书_20260101_000000.docx", (
            "必须回传真实落盘文件名，而非硬编码的 project_{id}_{ts}.docx"
        )


# ============================================================================
# D. 导出预检「全空」判据（P0 回归）
# ============================================================================
class TestDAllEmptyExportGate:
    """BUG-FIX 2026-09-18（P0）：本轮早先修好了 `check_document` 的父章节误报
    （父章节不再计入 empty_sections），但路由层仍用
    `len(empty_sections) == total_sections` 判「全部为空」。一旦文档里存在父章节，
    该等式**恒为假** —— 全空文档不再被拦住，照样导出一份 0 字的 DOCX。
    """

    def test_d1_router_no_longer_uses_empties_equality(self):
        src = _source_without_comments("app/routers/export.py")
        assert "len(check.empty_sections) == check.total_sections" not in src
        assert "not (check.total_chars or 0)" in src

    def test_d2_new_criterion_catches_all_empty_with_parent(self):
        from app.db import get_db
        from app.services.export import check_document

        pid = _new_project("D2-全空带父章节")

        def _mk(pid_, title, level, parent_id, content):
            with get_db() as conn:
                cur = conn.execute(
                    "INSERT INTO sections (project_id, parent_id, title, level, sort_order, "
                    "section_type, content) VALUES (?,?,?,?,?,?,?)",
                    (pid_, parent_id, title, level, 0, "normal", content),
                )
                return cur.lastrowid

        p1 = _mk(pid, "第一章 总体概述", 1, None, "")
        _mk(pid, "1.1 空叶子", 2, p1, "")

        r = check_document(pid)

        # 修复后：父章节不计入 empty_sections（正确），所以旧判据失效
        assert len(r.empty_sections) != r.total_sections, "本用例的前提：存在父章节"
        # 新判据：总字数为 0 即为空，与正文挂在哪类节点解耦
        assert not (r.total_chars or 0), "全空文档的总字数必须为 0"
        assert any("所有章节内容均为空" in e for e in r.errors), "check_document 必须判致命"


# ============================================================================
# E. create_report_schedule 的 enabled 不再被硬编码
# ============================================================================
class TestEReportScheduleEnabledWorks:
    """BUG-FIX 2026-09-18（中）：`enabled` 形参此前完全没进 INSERT 语句，
    取值全靠表定义 `DEFAULT 1` —— 传 False 建出来的调度依然启用，
    「先建好、稍后再启用」的用法彻底失效，等同于把启用状态硬编码成 True。
    """

    def test_enabled_false_is_persisted(self):
        from app.db import get_db, init_db
        from app.services.rejection import create_report_schedule

        init_db()
        with get_db() as conn:
            cur = conn.execute(
                "INSERT INTO rejection_checks (project_id, status) VALUES (?, 'pending')", (0,)
            )
            task_id = cur.lastrowid

        schedule_id = create_report_schedule(task_id, "daily", schedule_time="09:00", enabled=False)
        with get_db(readonly=True) as conn:
            row = conn.execute(
                "SELECT enabled FROM rejection_report_schedules WHERE id = ?", (schedule_id,)
            ).fetchone()
        assert row["enabled"] == 0, "enabled=False 必须真正落库，而不是被表默认值写死成 1"

    def test_enabled_true_is_persisted(self):
        from app.db import get_db, init_db
        from app.services.rejection import create_report_schedule

        init_db()
        with get_db() as conn:
            cur = conn.execute(
                "INSERT INTO rejection_checks (project_id, status) VALUES (?, 'pending')", (0,)
            )
            task_id = cur.lastrowid

        schedule_id = create_report_schedule(task_id, "weekly")
        with get_db(readonly=True) as conn:
            row = conn.execute(
                "SELECT enabled, schedule_type FROM rejection_report_schedules WHERE id = ?",
                (schedule_id,),
            ).fetchone()
        assert row["enabled"] == 1
        assert row["schedule_type"] == "weekly"


# ============================================================================
# F. cleanup_completed_tasks 返回真实条数并清理孤儿明细
# ============================================================================
class TestFCleanupCompletedTasksReturnsCount:
    """BUG-FIX 2026-09-18（中）：原实现不返回任何结果（调用方无法判断清理是否生效），
    且只删主表，不删 `section_generation_progress` 明细 —— 主表行一删，
    明细就成了永久孤儿行，只增不减。
    """

    def test_returns_deleted_count_and_drops_orphans(self):
        from app.db import get_db
        from app.services.generation_manager import generation_manager

        pid = _new_project("F-清理计数")
        gen_id = "gen-probe-f"
        orphan_id = "gen-orphan-f"
        with get_db() as conn:
            conn.execute(
                "INSERT INTO generation_tasks (generation_id, project_id, status, outline_version, "
                "started_at, completed_at, total_sections, completed_count, failed_count, "
                "cancelled_count, writing_length, concurrency, lock_key, lock_owner) "
                "VALUES (?,?, 'completed', 1, '2020-01-01 00:00:00', '2020-01-01 01:00:00', "
                "1, 1, 0, 0, 'standard', 1, '', '')",
                (gen_id, pid),
            )
            conn.execute(
                "INSERT INTO section_generation_progress (generation_id, section_id, status, "
                "progress, retry_count, last_error) VALUES (?, 555, 'completed', 100, 0, '')",
                (orphan_id,),
            )

        deleted = asyncio.run(generation_manager.cleanup_completed_tasks(max_hours=24))
        assert deleted >= 1, f"应返回真实删除条数，实际 {deleted}"

        with get_db(readonly=True) as conn:
            assert (
                conn.execute(
                    "SELECT COUNT(*) AS c FROM generation_tasks WHERE generation_id = ?", (gen_id,)
                ).fetchone()["c"]
                == 0
            )
            assert (
                conn.execute(
                    "SELECT COUNT(*) AS c FROM section_generation_progress WHERE generation_id = ?",
                    (orphan_id,),
                ).fetchone()["c"]
                == 0
            ), "孤儿进度明细必须一并清理"


# ============================================================================
# G. task_registry.cancel_task 同步清理项目索引
# ============================================================================
class TestGCancelTaskCleansProjectIndex:
    """BUG-FIX 2026-09-18（低）：`cancel_task` 只删 `_tasks`，漏删 `_project_index`
    —— 索引里永远留着指向已删除 task_id 的条目。`get_project_tasks()` 每次都要
    按索引去查一个不存在的 id 再静默跳过。
    """

    def test_index_entry_removed(self):
        from app.services.task_registry import TaskRegistry

        registry = TaskRegistry()
        info = asyncio.run(registry.create_task("outline", 99001))
        assert 99001 in registry._project_index
        assert registry._project_index[99001].get("outline") == info.task_id

        asyncio.run(registry.cancel_task(info.task_id))
        assert info.task_id not in registry._tasks
        assert "outline" not in registry._project_index.get(99001, {}), (
            "取消任务后项目索引里的条目必须一并清除"
        )


# ============================================================================
# H. Celery 任务的死参数不再被静默忽略
# ============================================================================
class TestHDeadParamsAreExplicit:
    """BUG-FIX 2026-09-18（中）：任务形参被静默忽略，调用方以为「传了就生效」。

    - `run_duplicate_check(file_ids=...)`：服务层只按 task_id 取文件，参数无效；
      现在显式告警并在返回值里标注 ignored_params。
    - `run_rejection_check(project_id=...)` / `process_knowledge_document(file_path, industry)`：
      保留形参，但记录日志说明「以 task_id / doc_id 为准」。
    """

    def test_h1_source_has_explicit_ignored_params(self):
        src = _source_without_comments("app/tasks/__init__.py")
        assert "ignored_params" in src
        assert "标书查重不支持按 file_ids 过滤" in src

    def test_h2_duplicate_task_marks_ignored(self, monkeypatch):
        import app.services.ai.workflows as workflows_mod
        import app.services.duplicate as duplicate_mod

        from app.tasks import run_duplicate_check

        called: dict = {}

        def _fake_run(task_id):
            called["task_id"] = task_id
            return None

        # 服务层替身：不做任何 IO，只记录被真正用到的参数
        monkeypatch.setattr(duplicate_mod, "run_duplicate_check", _fake_run, raising=True)
        # _fake_run 返回 None（非协程），替身 _safe_asyncio_run 直接放行
        monkeypatch.setattr(workflows_mod, "_safe_asyncio_run", lambda coro: None, raising=True)

        # BUG-FIX 2026-10-04：bind=True 代理直调自动注入 self，不能再显式传替身
        result = run_duplicate_check(777, [11, 22])
        assert result["status"] == "completed"
        assert result["ignored_params"] == {"file_ids": [11, 22]}, (
            "被忽略的 file_ids 必须回传给调用方，不能静默丢弃"
        )
        assert called.get("task_id") == 777, "服务层仍按 task_id 执行（未受 file_ids 影响）"

    def test_h3_knowledge_task_state_documents_dead_params(self):
        src = _source_without_comments("app/tasks/__init__.py")
        assert "file_path/industry 仅作备注" in src


# ============================================================================
# I. fire-and-forget 任务必须被强引用（否则可能被 GC 中途回收）
# ============================================================================
class TestIFireAndForgetTasksKeepReference:
    """BUG-FIX 2026-09-18（中）：`asyncio.create_task()` 返回的句柄若没有活跃强引用，
    事件循环只持有弱引用，任务可能在协程执行到一半时被垃圾回收 ——
    表现为「旧 AsyncClient 的 aclose 时灵时不灵」，且没有任何报错。
    """

    def test_i1_pending_set_exists(self):
        from app.services.ai.mermaid_service import _PENDING_CLOSE_TASKS

        assert isinstance(_PENDING_CLOSE_TASKS, set)

    def test_i2_close_task_is_registered(self):
        src = _source_without_comments("app/services/ai/mermaid_service.py")
        assert "_PENDING_CLOSE_TASKS.add(_close_task)" in src
        assert "_close_task.add_done_callback(_PENDING_CLOSE_TASKS.discard)" in src


# ============================================================================
# J. 可研任务收尾不再误删「已被新任务接管」的状态行
# ============================================================================
class TestJFinishTaskOnlyClearsOwnRun:
    """BUG-FIX 2026-09-18（中）：`_finish_task` 的 `task_id` 是死参数，收尾时按
    `type` 整行 DELETE。若期间已有同类型的新任务启动，会把新任务状态一起抹掉。
    """

    @staticmethod
    def _state_of(task_type: str):
        from app.db import get_db

        with get_db(readonly=True) as conn:
            return conn.execute(
                "SELECT task_id FROM feasibility_tasks WHERE type = ?", (task_type,)
            ).fetchone()

    def test_j1_foreign_task_is_not_cleared(self):
        from app.db import init_db
        from app.services.feasibility import clear_task_state_if_owner, save_task_state

        init_db()
        task_type = "feasibility-probe-j1"
        save_task_state(task_type, {"task_id": "run-A", "status": "running", "progress": 10})

        # 旧任务 Run-A 收尾，但当前已被 Run-B 接管
        assert clear_task_state_if_owner(task_type, "run-B") is False
        assert self._state_of(task_type) is not None, "不得删掉新任务的状态行"

    def test_j2_own_task_is_cleared(self):
        from app.db import init_db
        from app.services.feasibility import clear_task_state_if_owner, save_task_state

        init_db()
        task_type = "feasibility-probe-j2"
        save_task_state(task_type, {"task_id": "run-A", "status": "running", "progress": 10})

        assert clear_task_state_if_owner(task_type, "run-A") is True
        assert self._state_of(task_type) is None, "自己这一轮的任务行必须被清除"

    def test_j3_legacy_empty_task_id_is_cleared(self):
        """历史数据没有 task_id（空串）时仍按旧行为清除，避免留下僵尸行。"""
        from app.db import get_db, init_db
        from app.services.feasibility import clear_task_state_if_owner

        init_db()
        task_type = "feasibility-probe-j3"
        with get_db() as conn:
            conn.execute(
                "INSERT INTO feasibility_tasks (type, task_id, status, progress, updated_at) "
                "VALUES (?, '', 'running', 10, datetime('now','localtime'))",
                (task_type,),
            )

        assert clear_task_state_if_owner(task_type, "run-new") is True
        assert self._state_of(task_type) is None

    def test_j4_finish_task_uses_owner_aware_clear(self):
        src = _source_without_comments("app/services/feasibility_tasks.py")
        assert "store.clear_task_state_if_owner(task_type, task_id)" in src


# ============================================================================
# K. 任务接口的硬编码假字段
# ============================================================================
class TestKNoHardcodedFakeTaskId:
    """BUG-FIX 2026-09-18（低）：`POST /feasibility/analysis/run` 原先返回
    `{"task_id": "", ...}` —— 硬编码的空字符串。可研的状态走 `GET /feasibility`
    整体返回，没有按 id 查询的端点，前端若拿这个空 id 去轮询永远查不到进度。
    """

    def test_no_empty_task_id_literal(self):
        src = _source_without_comments("app/routers/feasibility.py")
        assert '"task_id": ""' not in src

    def test_analysis_run_returns_task_type_only(self):
        src = _source_without_comments("app/routers/feasibility.py")
        assert '{"status": "running", "task_type": "feasibility-analysis"}' in src


# ============================================================================
# L. task_with_retry 的 delay 不再是死参数
# ============================================================================
class TestLTasksWithRetryDelayIsUsed:
    """BUG-FIX 2026-09-18（低）：`delay` 形参此前收下后从未使用，实际退避起始间隔
    被 `retry_backoff=True` 硬编码成 1 秒。
    """

    def test_delay_flows_into_retry_backoff(self):
        src = _source_without_comments("app/celery_app.py")
        assert "retry_backoff=max(1, int(delay or 1))" in src
        # 末尾带逗号才是真正的 kwarg；docstring 里引用旧写法的那处没有逗号
        assert "retry_backoff=True," not in src


# ============================================================================
# N. 连接池「冷却期内不回收」导致池满即永久失败
# ============================================================================
class TestNDbPoolForceReapBeforeExhausted:
    """BUG-FIX 2026-09-18（高）：`_reap_dead_thread_connections` 有 10s 冷却。

    若上一轮回收刚发生过，这期间死掉的线程持有的连接要等冷却结束才释放 ——
    而调用方马上就要因「池满」抛 `Database connection pool exhausted`。
    该异常一旦抛出，**整个进程的所有数据库操作永久失败、只能重启**。
    现改为：判池满之前先强制再扫一次。

    这也是本轮全量测试里 `test_integration.py` 假红灯的根因：日志里只有
    `pool exhausted (max=32, active=32)`，而单独复跑那些用例又全过。
    """

    def test_n1_force_reap_bypasses_cooldown(self):
        import threading
        import time

        from app.db import DBPool

        pool = DBPool(max_connections=8)
        try:
            # 在一条专用线程里取连接，线程退出后其连接即成为「死线程连接」
            holder = {}

            def _take():
                holder["conn"] = pool.get_connection()

            worker = threading.Thread(target=_take)
            worker.start()
            worker.join()

            assert len(pool._all_connections) == 1
            dead_thread_id = pool._all_connections[0][0]
            assert dead_thread_id != threading.get_ident()

            # 把「最后使用时间」推远，确保满足 DEAD_THREAD_IDLE_SECONDS
            pool._last_used[dead_thread_id] = time.time() - 10_000

            # 模拟刚回收过 → 冷却期内普通回收应被跳过
            pool._last_reap_time = time.time()
            assert pool._reap_dead_thread_connections() == 0, "冷却期内不应重复扫描"
            assert len(pool._all_connections) == 1

            # 强制回收必须跳过冷却并真正回收
            reaped = pool._reap_dead_thread_connections(force=True)
            assert reaped == 1, f"force=True 必须跳过冷却完成回收，实际 {reaped}"
            assert len(pool._all_connections) == 0
        finally:
            pool.close_all()

    def test_n2_pool_full_still_raises_when_nothing_to_reap(self):
        """强制回收救不回来时，仍然照原样抛错（不能吞掉池满这个致命信号）。"""
        import threading

        from app.db import DBPool

        pool = DBPool(max_connections=1)
        try:
            # 当前（存活）线程先占满唯一配额
            pool.get_connection()
            assert len(pool._all_connections) == 1

            # 从另一条**存活**线程再取 → 无死线程连接可回收，必须抛错
            box: dict = {}

            def _take():
                try:
                    pool.get_connection()
                    box["raised"] = False
                except RuntimeError as exc:
                    box["raised"] = True
                    box["msg"] = str(exc)

            worker = threading.Thread(target=_take)
            worker.start()
            worker.join()

            assert box.get("raised") is True, "无连接可回收时必须照常抛池满错误"
            assert "pool exhausted" in box["msg"]
        finally:
            pool.close_all()


# ============================================================================
# M. rejection_report_schedules 的执行器（此前是只写不读的空任务表）
# ============================================================================
def _mk_check() -> int:
    from app.db import get_db, init_db

    init_db()
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO rejection_checks (project_id, status) VALUES (?, 'pending')", (0,)
        )
        return cur.lastrowid


def _read_schedule(schedule_id: int) -> dict:
    from app.db import get_db

    with get_db(readonly=True) as conn:
        row = conn.execute(
            "SELECT * FROM rejection_report_schedules WHERE id = ?", (schedule_id,)
        ).fetchone()
    return dict(row) if row is not None else {}


class TestMReportScheduleExecutor:
    """BUG-FIX 2026-09-18（高）：`rejection_report_schedules` 表只有 CRUD 五个端点、
    **没有任何执行器** —— 全库无读取 `schedule_type`/`schedule_time` 并触发报告生成
    的代码。用户配好的「每日报告」永远不产出，也没有任何报错。
    """

    @staticmethod
    def _run_only(monkeypatch, schedule_id: int, now):
        """让执行器只看到这一条调度，避免被同库其它用例的数据干扰。"""
        from app.services import rejection as rejection_mod
        from app.services.report_schedule_runner import run_due_schedules

        monkeypatch.setattr(
            rejection_mod, "get_all_report_schedules", lambda limit=100: [_read_schedule(schedule_id)]
        )
        monkeypatch.setattr(
            rejection_mod, "export_report", lambda task_id: f"/tmp/report-{task_id}.docx"
        )
        return run_due_schedules(now=now)

    def test_m1_execution_columns_exist(self):
        from app.db import get_db, init_db

        init_db()
        with get_db(readonly=True) as conn:
            cols = {
                r["name"]
                for r in conn.execute("PRAGMA table_info(rejection_report_schedules)").fetchall()
            }
        assert {"last_run_at", "run_count", "last_error"} <= cols, (
            "执行器需要回写执行记录，迁移必须补这三列"
        )

    def test_m2_daily_due_is_generated(self, monkeypatch):
        from datetime import datetime

        from app.services.rejection import create_report_schedule

        task_id = _mk_check()
        sid = create_report_schedule(task_id, "daily", "09:00", enabled=True)
        result = self._run_only(monkeypatch, sid, datetime(2026, 3, 10, 10, 0))

        assert result["generated"] == 1, f"到点的每日调度必须生成报告: {result}"
        row = _read_schedule(sid)
        assert int(row["run_count"]) == 1
        assert row["last_run_at"], "必须回写执行时间，否则下小时会重复生成"
        assert row["last_error"] is None

    def test_m3_daily_already_ran_today_is_skipped(self, monkeypatch):
        from datetime import datetime

        from app.db import get_db
        from app.services.rejection import create_report_schedule

        task_id = _mk_check()
        sid = create_report_schedule(task_id, "daily", "09:00", enabled=True)
        with get_db() as conn:
            conn.execute(
                "UPDATE rejection_report_schedules SET last_run_at = '2026-03-10 09:05:00' "
                "WHERE id = ?",
                (sid,),
            )

        result = self._run_only(monkeypatch, sid, datetime(2026, 3, 10, 10, 0))
        assert result["generated"] == 0 and result["skipped"] == 1, (
            f"同一天已生成过就不得重复生成: {result}"
        )
        assert int(_read_schedule(sid)["run_count"]) == 0

    def test_m4_daily_before_trigger_time_is_skipped(self, monkeypatch):
        from datetime import datetime

        from app.services.rejection import create_report_schedule

        task_id = _mk_check()
        sid = create_report_schedule(task_id, "daily", "09:00", enabled=True)
        result = self._run_only(monkeypatch, sid, datetime(2026, 3, 10, 8, 0))
        assert result["generated"] == 0 and result["skipped"] == 1

    def test_m5_disabled_schedule_is_skipped(self, monkeypatch):
        from datetime import datetime

        from app.services.rejection import create_report_schedule

        task_id = _mk_check()
        sid = create_report_schedule(task_id, "daily", "09:00", enabled=False)
        result = self._run_only(monkeypatch, sid, datetime(2026, 3, 10, 10, 0))
        assert result["generated"] == 0 and result["skipped"] == 1

    def test_m6_weekly_and_monthly_semantics(self):
        """周期判定：同周期不重复、跨周期可再生成。"""
        from datetime import datetime, timedelta

        from app.services.report_schedule_runner import _week_key, is_due

        now = datetime(2026, 3, 10, 10, 0)

        weekly = {"schedule_type": "weekly", "schedule_time": "09:00", "run_count": 0}
        assert is_due({**weekly, "last_run_at": None}, now) is True
        assert is_due({**weekly, "last_run_at": "2026-03-10 09:00:00"}, now) is False, (
            "同一周已生成过"
        )
        prev_week = now - timedelta(days=7)
        assert _week_key(prev_week) != _week_key(now)
        assert is_due({**weekly, "last_run_at": prev_week.strftime("%Y-%m-%d %H:%M:%S")}, now) is True

        monthly = {"schedule_type": "monthly", "schedule_time": "09:00", "run_count": 0}
        assert is_due({**monthly, "last_run_at": "2026-03-01 09:00:00"}, now) is False
        assert is_due({**monthly, "last_run_at": "2026-01-15 09:00:00"}, now) is True

    def test_m7_once_runs_exactly_once(self, monkeypatch):
        from datetime import datetime

        from app.services.rejection import create_report_schedule

        task_id = _mk_check()
        sid = create_report_schedule(task_id, "once", "2020-01-01 09:00:00", enabled=True)

        first = self._run_only(monkeypatch, sid, datetime(2026, 3, 10, 10, 0))
        assert first["generated"] == 1, f"once 到点后必须生成: {first}"
        assert int(_read_schedule(sid)["run_count"]) == 1

        second = self._run_only(monkeypatch, sid, datetime(2026, 3, 10, 11, 0))
        assert second["generated"] == 0 and second["skipped"] == 1, (
            f"once 只能执行一次（用 run_count 判，进程重启也不重跑）: {second}"
        )

    def test_m8_missing_task_is_recorded_as_error(self, monkeypatch):
        from datetime import datetime

        from app.services import rejection as rejection_mod
        from app.services.rejection import create_report_schedule
        from app.services.report_schedule_runner import run_due_schedules

        task_id = _mk_check()
        sid = create_report_schedule(task_id, "daily", "09:00", enabled=True)

        # 任务行被清理（外键级联在真实删除下会带走调度，这里直接模拟"查不到任务"）
        monkeypatch.setattr(
            rejection_mod, "get_all_report_schedules", lambda limit=100: [_read_schedule(sid)]
        )
        monkeypatch.setattr(rejection_mod, "get_task", lambda tid: None)

        result = run_due_schedules(now=datetime(2026, 3, 10, 10, 0))

        assert result["failed"] == 1 and result["generated"] == 0, f"任务不存在应记为失败: {result}"
        row = _read_schedule(sid)
        assert row["last_error"], "失败原因必须回写，否则用户只看到「没生成」而不知为何"
        assert int(row["run_count"]) == 0

    def test_m9_unsupported_type_is_skipped_with_reason(self):
        from datetime import datetime

        from app.services.report_schedule_runner import due_reason

        assert due_reason({"schedule_type": "hourly", "enabled": 1}, datetime(2026, 3, 10, 10, 0))
        assert (
            due_reason({"schedule_type": "daily", "enabled": 0}, datetime(2026, 3, 10, 10, 0))
            == "disabled"
        )

    def test_m10_executor_task_is_registered(self):
        from app import celery_app as ca

        assert "app.celery_app.run_scheduled_reports" in ca.list_registered_tasks()

    def test_m11_executor_task_runs_inline(self):
        from app import celery_app as ca

        result = ca.dispatch_task("app.celery_app.run_scheduled_reports", force_inline=True)
        assert result["dispatched_via"] == "inline"
        assert result["status"] == "ok", f"执行器就地运行不应失败: {result}"
        for key in ("scanned", "generated", "skipped", "failed"):
            assert key in result["result"]


class TestOIdleConnectionReclaim:
    """O. 连接池不再被「存活但长期空闲」的线程永久占满（generation 机制）

    2026-09-18 全量测试的 `Database connection pool exhausted (max=32, active=32)`
    就是它造成的：asyncio 默认 ThreadPoolExecutor 的 idle worker 永不退出，
    每新建一个事件循环就新起一批线程 —— 死线程回收（`_reap_dead_thread_connections`）
    一个都救不回来。而**抛错之后整个进程的所有数据库操作永久失败、只能重启**，
    这正是「任务运行无任何结果」最严重的一种形态。
    """

    def _spawn_holder(self, pool, release, box: dict) -> threading.Thread:
        """起一个常驻线程占住一条连接配额，并返回该线程。"""
        got = threading.Event()

        def worker():
            box["first"] = pool.get_connection()
            got.set()
            release.wait(15)

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        assert got.wait(10), "worker 线程未能取得连接"
        return t

    def test_o1_reclaim_bumps_only_victim_epoch(self):
        from app.db import DBPool

        pool = DBPool(max_connections=4)
        release = threading.Event()
        box: dict = {}
        t = self._spawn_holder(pool, release, box)
        try:
            first = box["first"]
            pool._last_used[t.ident] = time.time() - 10_000  # 模拟长期空闲
            assert pool._reclaim_idle_connection() == 1, "空闲连接应可被抢占"
            assert pool._thread_epoch.get(t.ident, 0) == 1, "只递增被抢占线程的世代号"
            assert not any(c is first for _, c in pool._all_connections), "连接必须移出池"
            assert pool._stats["idle_reclaimed"] == 1
        finally:
            release.set()
            t.join(10)
            pool.close_all()

    def test_o2_victim_thread_rebuilds_instead_of_reusing_closed_conn(self):
        from app.db import DBPool

        pool = DBPool(max_connections=4)
        release = threading.Event()
        step2 = threading.Event()
        done = threading.Event()
        box: dict = {}

        def worker():
            box["first"] = pool.get_connection()
            release.wait(15)
            # 连接已被抢占，这里必须重建，而不是继续用那个已 close 的 sqlite3 对象
            second = pool.get_connection()
            second.execute("SELECT 1")  # 拿到已关闭连接会在此处抛错
            box["second"] = second
            done.set()

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        assert pool._thread_epoch is not None
        deadline = time.time() + 10
        while "first" not in box and time.time() < deadline:
            time.sleep(0.01)
        pool._last_used[t.ident] = time.time() - 10_000
        assert pool._reclaim_idle_connection() == 1
        release.set()
        try:
            assert done.wait(10), "被抢占的线程应能自行重建连接（而不是卡死或抛错）"
            assert box["second"] is not box["first"], "必须新建连接，不能复用已关闭的那个"
        finally:
            t.join(10)
            pool.close_all()

    def test_o3_pool_full_steals_from_idle_live_thread(self):
        from app.db import DBPool

        pool = DBPool(max_connections=2)
        release = threading.Event()
        box: dict = {}
        holder = self._spawn_holder(pool, release, box)
        try:
            pool.get_connection()  # 主线程占第二条配额，此时池满
            pool._last_used[holder.ident] = time.time() - 10_000

            late_box: dict = {}
            got = threading.Event()

            def late():
                try:
                    late_box["conn"] = pool.get_connection()
                except BaseException as exc:  # noqa: BLE001 - 需要在主线程断言
                    late_box["err"] = exc
                got.set()

            t2 = threading.Thread(target=late, daemon=True)
            t2.start()
            assert got.wait(10), "池满时应抢占空闲连接，而不是抛 RuntimeError"
            assert "err" not in late_box, f"不应抛错: {late_box.get('err')}"
            assert len(pool._all_connections) <= pool._max_connections
            t2.join(10)
        finally:
            release.set()
            holder.join(10)
            pool.close_all()

    def test_o4_no_steal_when_connection_recently_used(self):
        from app.db import DBPool

        pool = DBPool(max_connections=1)
        release = threading.Event()
        box: dict = {}
        holder = self._spawn_holder(pool, release, box)
        try:
            assert pool._reclaim_idle_connection() == 0, "刚用过的连接不能被抢占"
            with pytest.raises(RuntimeError, match="pool exhausted"):
                pool.get_connection()
        finally:
            release.set()
            holder.join(10)
            pool.close_all()

    def test_o5_main_thread_connection_is_never_stolen(self):
        from app.db import DBPool

        pool = DBPool(max_connections=1)
        try:
            conn = pool.get_connection()
            pool._last_used[threading.main_thread().ident] = time.time() - 10_000
            assert pool._reclaim_idle_connection(exclude_thread_id=-1) == 0
            assert pool.get_connection() is conn, "主线程连接不应被抢占"
        finally:
            pool.close_all()
