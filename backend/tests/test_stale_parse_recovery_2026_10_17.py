"""孤儿 processing 回收（GAP-FIX 2026-10-17）

背景：图片上传（R-3）走「broker 派发 → worker 收尾」，下列任一情形都会让
``uploaded_files`` / ``file_parse_tasks`` **永久停在 processing**：

  1. dispatch_task 把消息投给了 broker，但 worker 未运行 / 队列消息丢失；
  2. worker 进程被 SIGKILL（Celery time_limit 超时、容器重启），except 分支
     来不及执行，终态 UPDATE 不会发生；
  3. 任务重试耗尽前 worker 崩溃。

此前**没有任何回收路径**：
  - prune_parse_tasks 明确「在途 processing 行一律保留」；
  - cleanup_old_results（每小时 beat）只清 task_lock / task_snapshot /
    generation_tasks，再加一次 prune，不碰这两张表；
  - services/files.py 的 M-4 注释还引用了一个**全仓并不存在**的
    _recover_stale_tasks，把「启动期会回收」当成了事实陈述。

后果：前端 classifyUploadResult 判为「解析中」，进度条永久卡住，用户只能
删除重传。本文件钉住新增的 reclaim_stale_parse_state() 及其 beat 接线。
"""
import inspect
import re
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parent.parent


def _new_project(name: str) -> int:
    from app.db import get_db, init_db

    init_db()
    with get_db() as conn:
        cur = conn.execute("INSERT INTO projects (name) VALUES (?)", (name,))
        return int(cur.lastrowid)


def _mk_upload(pid: int, *, status: str, age_sql: str, filename: str = "扫描件.png") -> int:
    """插入一行 uploaded_files，created_at/updated_at 按 age_sql 回拨。"""
    from app.db import get_db

    with get_db() as conn:
        # BUG-FIX 2026-10-17（同步 updated_at）：新 schema 有 updated_at 列，
        # reclaim_stale_parse_state 用它判 stale。若只设 created_at 而让
        # updated_at 走 DEFAULT，该行 updated_at 会是当前时间 → 永远不 stale。
        cur = conn.execute(
            "INSERT INTO uploaded_files (project_id, filename, path, file_type, "
            "file_role, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, "
            "datetime('now', 'localtime', ?), datetime('now', 'localtime', ?))",
            (pid, filename, f"/tmp/{filename}", "png", "main", status, age_sql, age_sql),
        )
        return int(cur.lastrowid)


def _upload_row(file_id: int) -> dict:
    from app.db import get_db

    with get_db() as conn:
        row = conn.execute(
            "SELECT status, message FROM uploaded_files WHERE id = ?", (file_id,)
        ).fetchone()
    return dict(row)


def _mk_parse_task(pid: int, *, status: str, age_sql: str, filename: str = "扫描件.png") -> int:
    from app.db import get_db

    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO file_parse_tasks (project_id, filename, file_type, status, "
            "progress, total_files, completed_files, started_at, updated_at) "
            "VALUES (?, ?, ?, ?, 0, 1, 0, datetime('now', 'localtime', ?), "
            "datetime('now', 'localtime', ?))",
            (pid, filename, "png", status, age_sql, age_sql),
        )
        return int(cur.lastrowid)


def _task_row(task_id: int) -> dict:
    from app.db import get_db

    with get_db() as conn:
        row = conn.execute(
            "SELECT status, error_message FROM file_parse_tasks WHERE id = ?", (task_id,)
        ).fetchone()
    return dict(row)


# ============================================================================
# 1. uploaded_files：超时 processing 必须收敛为 failed
# ============================================================================


class TestStaleUploadedFilesReclaimed:
    def test_stale_processing_row_becomes_failed(self):
        from app.services.files import reclaim_stale_parse_state

        pid = _new_project("孤儿processing-超时回收")
        fid = _mk_upload(pid, status="processing", age_sql="-2 hours")

        out = reclaim_stale_parse_state(stale_minutes=60)

        assert out["uploaded_files_failed"] == 1, out
        row = _upload_row(fid)
        assert row["status"] == "failed", "超时 processing 必须收敛为 failed"
        assert "worker" in row["message"], row
        assert "重新解析" in row["message"], "失败原因必须可自助，而不是空白"

    def test_fresh_processing_row_is_untouched(self):
        """阈值内的在途行绝对不能碰 —— 否则会把正在跑的大图 OCR 误杀。"""
        from app.services.files import reclaim_stale_parse_state

        pid = _new_project("孤儿processing-新鲜行不碰")
        fid = _mk_upload(pid, status="processing", age_sql="-1 minutes")

        out = reclaim_stale_parse_state(stale_minutes=60)

        assert out["uploaded_files_failed"] == 0, out
        assert _upload_row(fid)["status"] == "processing"

    def test_terminal_rows_are_untouched(self):
        """success / failed 终态行不得被改写（message 也不能被覆盖）。"""
        from app.db import get_db
        from app.services.files import reclaim_stale_parse_state

        pid = _new_project("孤儿processing-终态不碰")
        ok_id = _mk_upload(pid, status="success", age_sql="-5 hours", filename="a.docx")
        bad_id = _mk_upload(pid, status="failed", age_sql="-5 hours", filename="b.docx")
        with get_db() as conn:
            conn.execute(
                "UPDATE uploaded_files SET message = ? WHERE id = ?",
                ("原有的空文本提示", ok_id),
            )

        out = reclaim_stale_parse_state(stale_minutes=60)

        assert out["uploaded_files_failed"] == 0, out
        assert _upload_row(ok_id)["status"] == "success"
        assert _upload_row(ok_id)["message"] == "原有的空文本提示"
        assert _upload_row(bad_id)["status"] == "failed"

    def test_claim_is_idempotent(self):
        """重复调用第二次必须返回 0（不重复改写、不重复计数）。"""
        from app.services.files import reclaim_stale_parse_state

        pid = _new_project("孤儿processing-幂等")
        _mk_upload(pid, status="processing", age_sql="-2 hours")

        first = reclaim_stale_parse_state(stale_minutes=60)
        second = reclaim_stale_parse_state(stale_minutes=60)

        assert first["uploaded_files_failed"] == 1, first
        assert second["uploaded_files_failed"] == 0, second

    def test_disabled_when_stale_minutes_is_zero(self):
        """0 = 关闭回收（与 PARSE_TASK_RETENTION 的 0 语义保持一致）。"""
        from app.services.files import reclaim_stale_parse_state

        pid = _new_project("孤儿processing-关闭")
        fid = _mk_upload(pid, status="processing", age_sql="-99 hours")

        out = reclaim_stale_parse_state(stale_minutes=0)

        assert out == {"uploaded_files_failed": 0, "parse_tasks_failed": 0}, out
        assert _upload_row(fid)["status"] == "processing"

    def test_default_threshold_is_conservative(self):
        """默认阈值必须足够宽松：大图 OCR 正常耗时可达数十分钟，阈值过短会在
        beat 每小时一轮的节奏下误杀合法在途任务。"""
        from app.config import PARSE_TASK_STALE_MINUTES

        assert PARSE_TASK_STALE_MINUTES >= 30, (
            f"PARSE_TASK_STALE_MINUTES={PARSE_TASK_STALE_MINUTES} 过短，会误杀合法在途解析"
        )


# ============================================================================
# 2. file_parse_tasks：同样不得留下永不收敛的 processing 行
# ============================================================================


class TestStaleParseTaskRowsReclaimed:
    def test_stale_processing_task_becomes_failed(self):
        from app.services.files import reclaim_stale_parse_state

        pid = _new_project("孤儿任务行-超时回收")
        tid = _mk_parse_task(pid, status="processing", age_sql="-2 hours")

        out = reclaim_stale_parse_state(stale_minutes=60)

        assert out["parse_tasks_failed"] == 1, out
        row = _task_row(tid)
        assert row["status"] == "failed"
        assert "worker" in (row["error_message"] or "")

    def test_fresh_processing_task_is_untouched(self):
        from app.services.files import reclaim_stale_parse_state

        pid = _new_project("孤儿任务行-新鲜不碰")
        tid = _mk_parse_task(pid, status="processing", age_sql="-1 minutes")

        out = reclaim_stale_parse_state(stale_minutes=60)

        assert out["parse_tasks_failed"] == 0, out
        assert _task_row(tid)["status"] == "processing"

    def test_prune_still_keeps_inflight_rows_by_count(self):
        """prune 的「只淘汰终态行」不变式不得被本修复破坏 —— 数量裁剪与年龄回收
        是两条正交的路径：prune 管行数，reclaim 管年龄。"""
        from app.db import get_db
        from app.services.files import prune_parse_tasks

        pid = _new_project("孤儿任务行-prune不碰在途")
        tids = [
            _mk_parse_task(pid, status="processing", age_sql="-2 hours", filename=f"f{i}.png")
            for i in range(3)
        ]

        deleted = prune_parse_tasks(pid, max_rows=1)

        assert deleted == 0, "在途 processing 行不得被数量裁剪淘汰"
        with get_db() as conn:
            remaining = conn.execute(
                "SELECT COUNT(*) FROM file_parse_tasks WHERE project_id = ?", (pid,)
            ).fetchone()[0]
        assert remaining == len(tids), remaining


# ============================================================================
# 3. beat 接线：回收器必须真的挂上每小时清理任务
# ============================================================================


class TestBeatWiring:
    def test_beat_schedule_registers_cleanup(self):
        from app.celery_app import celery_app

        schedule = celery_app.conf.beat_schedule or {}
        assert "cleanup-old-tasks" in schedule, (
            "cleanup 未注册进 beat_schedule，孤儿 processing 将永不回收"
        )

    def test_cleanup_old_results_calls_reclaim(self):
        """源码守卫：清理任务必须调用 reclaim_stale_parse_state。

        只断言「函数存在」不够 —— 函数可以存在却从不被调用（本缺口正是这么
        形成的：prune 存在但只删终态行）。
        """
        from app.celery_app import cleanup_old_results

        src = inspect.getsource(cleanup_old_results)
        assert "reclaim_stale_parse_state" in src, (
            "cleanup_old_results 未接线 reclaim_stale_parse_state"
        )

    def test_cleanup_runs_reclaim_after_prune(self):
        """顺序守卫：必须先 prune 终态行，再把超时在途行收敛为 failed。

        反序不会报错，但收敛出来的 failed 行要等**下一轮**（1 小时后）才被
        prune 裁掉，表会在两轮之间多留一批终态行。
        """
        from app.celery_app import cleanup_old_results

        src = inspect.getsource(cleanup_old_results)
        prune_at = src.index("prune_parse_tasks")
        reclaim_at = src.index("reclaim_stale_parse_state")
        assert prune_at < reclaim_at, "reclaim 必须排在 prune 之后"

    def test_cleanup_old_results_end_to_end(self):
        """端到端：直接调用 beat 任务本体，孤儿行必须被真实回收。

        源码守卫只能证明「调用了这个名字」，证明不了接线后真的生效 —— 参数
        传错、异常被内部吞掉、阈值被读成 0，都会让它静默空转而源码断言依旧全绿。
        """
        from app.celery_app import cleanup_old_results

        pid = _new_project("孤儿processing-beat端到端")
        fid = _mk_upload(pid, status="processing", age_sql="-3 hours")

        out = cleanup_old_results()

        assert isinstance(out, dict), out
        assert out.get("parse_state_reclaimed", 0) >= 1, out
        assert _upload_row(fid)["status"] == "failed", "beat 未真实回收孤儿行"


# ============================================================================
# 4. 不变式：processing 只能由可回收的路径写入
# ============================================================================


class TestProcessingWriterInvariant:
    def test_only_persist_extraction_writes_processing(self):
        """uploaded_files 的 processing 只允许由 _persist_file_extraction 写入。

        这是本回收器**时钟安全**的前提：该函数在写 processing 后必然写终态，
        且行的 created_at 就是本次解析的起点。若未来新增一条「置 processing 却
        不刷新 created_at」的路径（例如重新提取改为异步并复用旧行），本回收器
        会把刚起步的解析判成超时 —— 届时必须同步引入 updated_at 或改用其他时钟。
        """
        hits = []
        for path in (BACKEND_DIR / "app").rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            for m in re.finditer(
                r"UPDATE\s+uploaded_files\s+SET\s+status\s*=\s*'processing'", text, re.I
            ):
                hits.append(path.relative_to(BACKEND_DIR).as_posix())
        assert hits == ["app/services/files.py"], (
            f"uploaded_files 的 processing 写入点发生了变化：{hits}。"
            "若新增写入点，请确认其时钟安全性并同步更新本守卫。"
        )

    def test_persist_extraction_marks_processing_then_terminal(self):
        """_persist_file_extraction 必须先置 processing、后写终态（缺一即悬空）。"""
        from app.services import files as fmod

        src = inspect.getsource(fmod._persist_file_extraction)
        assert "SET status = 'processing'" in src
        terminal_at = src.index("SET status = ?, extracted_text_path = ?, message = ?")
        processing_at = src.index("SET status = 'processing'")
        assert processing_at < terminal_at, "必须先置 processing 再写终态"
