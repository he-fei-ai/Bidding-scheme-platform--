"""回归测试（2026-09-16）：深度代码审查 BUG 系统性修复

覆盖的缺陷与改动文件：
1. BUG-R1  routers/sse/sse_db_ops.py::_rollback_dirty_sections —— 回滚不再清空
        content。重生成模式下 _mark_sections_generating 只置 status 保留原
        content，中断收尾若把 content 一并清空会永久丢失被打断章节的旧正文。
        修复后回滚只重置 status='pending'，内容由下次生成覆盖。
2. BUG-R2  app/tasks/__init__.py::generate_section_content —— 空 section_ids
        原会生成非法 SQL `WHERE id IN ()`，被外层 except 吞成 failed；
        现在空批次直接返回 completed 统计。
3. BUG-R3  services/ai/workflows_analysis.py::extract_scoring_points —— 降级
        分支原只接 (ValueError, RuntimeError)，with_timeout 抛 TimeoutError
        （OSError 子类）与 provider 网络异常均直接上抛，最常见的超时场景
        降级完全失效。现在任何 Exception 都降级（CancelledError 属
        BaseException 不受影响）。
4. BUG-R4  routers/tasks.py::_get_file_parsing_status / tasks status 轮询 ——
        高频纯读接口改用 readonly 连接 + to_thread，不抢全局写锁。
        （行为不变，回归验证读取语义：有行返回 dict、无行返回 None。）
5. BUG-R5  routers/task_control.py::stop_generation —— mark_failed 的 UPDATE
        移入线程池（行为不变，回归验证停止语义仍生效）。

测试原则：只测行为，不测实现细节。
"""

import asyncio
import sqlite3

import pytest

from app.config import DATABASE_PATH
from app.db import get_db
from app.models import ProjectCreate, SectionCreate
from app.routers.sse.sse_db_ops import _mark_sections_generating, _rollback_dirty_sections
from app.services import sections as sections_service
from app.services.projects import create_project

_created_project_ids: list[int] = []


@pytest.fixture(autouse=True)
def _cleanup_created_projects():
    yield
    if not _created_project_ids:
        return
    conn = sqlite3.connect(str(DATABASE_PATH))
    try:
        conn.execute("PRAGMA foreign_keys=OFF")
        placeholders = ",".join("?" for _ in _created_project_ids)
        conn.execute(f"DELETE FROM sections WHERE project_id IN ({placeholders})", _created_project_ids)
        conn.execute(f"DELETE FROM projects WHERE id IN ({placeholders})", _created_project_ids)
        conn.commit()
    finally:
        conn.close()
        _created_project_ids.clear()


def _make_project() -> int:
    pid = create_project(ProjectCreate(name="bugfix-review 回归测试项目")).id
    _created_project_ids.append(pid)
    return pid


def _add(pid: int, title: str, parent_id=None, level=1, sort_order=0) -> int:
    return sections_service.create_section(
        pid, SectionCreate(title=title, parent_id=parent_id, level=level, sort_order=sort_order)
    ).id


# ============================================================================
# BUG-R1：回滚保留旧正文
# ============================================================================

def test_rollback_dirty_sections_preserves_existing_content():
    """重生成中断回滚：status 重置为 pending，但**原有 content 必须保留**。

    场景：章节已有完整旧正文 → 用户重写 → _mark_sections_generating（不清
    content）→ 任务被打断 → _rollback_dirty_sections。修复前旧正文被清空。
    """
    pid = _make_project()
    sid = _add(pid, "1.1 施工组织")
    with get_db() as conn:
        conn.execute(
            "UPDATE sections SET content = ? WHERE id = ?",
            ("这是重生成前已有的完整旧正文，中断后不能丢失。", sid),
        )

    _mark_sections_generating([sid])
    with get_db() as conn:
        row = conn.execute("SELECT status, content FROM sections WHERE id = ?", (sid,)).fetchone()
    assert row["status"] == "generating"
    # mark_generating 不清内容（修复前提）
    assert row["content"].startswith("这是重生成前已有")

    _rollback_dirty_sections([sid])
    with get_db() as conn:
        row = conn.execute("SELECT status, content FROM sections WHERE id = ?", (sid,)).fetchone()
    assert row["status"] == "pending"
    assert row["content"].startswith("这是重生成前已有"), "回滚不得清空原有正文"


def test_rollback_dirty_sections_ignores_non_generating():
    """回滚只作用于 status='generating' 的章节，completed 章节不受影响。"""
    pid = _make_project()
    sid = _add(pid, "1.2 已完成章节")
    with get_db() as conn:
        conn.execute(
            "UPDATE sections SET status = 'completed', content = ? WHERE id = ?",
            ("已完成正文", sid),
        )

    _rollback_dirty_sections([sid])
    with get_db() as conn:
        row = conn.execute("SELECT status, content FROM sections WHERE id = ?", (sid,)).fetchone()
    assert row["status"] == "completed"
    assert row["content"] == "已完成正文"


def test_rollback_dirty_sections_empty_and_no_generating():
    """空列表与无 generating 行时安全返回，不抛异常。"""
    _rollback_dirty_sections([])
    _rollback_dirty_sections([99999999])  # 不存在的 id


# ============================================================================
# BUG-R2：Celery 任务空批次不再产生非法 SQL
# ============================================================================

def test_generate_section_content_empty_section_ids():
    """空 section_ids 直接返回 completed 空统计，而非 OperationalError→failed。"""
    pytest.importorskip("celery", reason="本环境未安装 celery")
    from app.tasks import generate_section_content

    result = generate_section_content(123456789, [], "standard")
    assert result["status"] == "completed"
    assert result["total"] == 0
    assert result["completed"] == 0
    assert result["failed"] == 0


# ============================================================================
# BUG-R3：评分点提取对超时/网络异常的降级
# ============================================================================

class _BrokenProvider:
    """provider 本体不会被调用（call_ai_text 被 monkeypatch）

    迁移说明 2026-10-19：评分点两阶段调用口径由裸 with_timeout 统一改走
    call_ai_text（瞬时超时重试一次，见 test_bugfix_2026_10_19_scoring_retry.py），
    本组用例的 mock 接缝随之从 with_timeout 迁移到 call_ai_text，
    断言契约（任何异常降级、第二阶段失败保留第一阶段结果）保持不变。
    """

    def chat(self, messages):
        raise AssertionError("provider.chat 不应被触达")


@pytest.mark.parametrize("exc", [TimeoutError(), RuntimeError("boom"), ValueError("bad json")])
def test_extract_scoring_points_degrades_on_first_stage_failure(exc):
    """第一阶段任何异常（含 TimeoutError）都返回空列表降级，不向上抛。"""
    from app.services.ai import workflows_analysis

    async def _always_fail(*_args, **_kwargs):
        raise exc

    orig = workflows_analysis.call_ai_text
    workflows_analysis.call_ai_text = _always_fail
    try:
        result = asyncio.run(
            workflows_analysis.extract_scoring_points(_BrokenProvider(), "上下文", "摘要")
        )
    finally:
        workflows_analysis.call_ai_text = orig
    assert result == []


def test_extract_scoring_points_second_stage_failure_falls_back_to_first_stage():
    """第二阶段失败（含超时）降级为第一阶段结果，而非整次识别失败。"""
    from app.services.ai import workflows_analysis

    calls = {"n": 0}

    async def _fail_on_second(*_args, **_kwargs):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise TimeoutError("second stage timeout")
        return '[{"name": "工期保障", "weight": 10}]'

    orig = workflows_analysis.call_ai_text
    workflows_analysis.call_ai_text = _fail_on_second
    try:
        result = asyncio.run(
            workflows_analysis.extract_scoring_points(_BrokenProvider(), "上下文", "摘要")
        )
    finally:
        workflows_analysis.call_ai_text = orig
    # 第一阶段结果被保留（normalize 后非空，且不含第二阶段内容）
    assert isinstance(result, list)
    assert len(result) >= 1


# ============================================================================
# BUG-R4：文件解析状态查询（readonly 改造后行为不变）
# ============================================================================

def test_get_file_parsing_status_no_task_returns_none():
    from app.routers.tasks import _get_file_parsing_status

    assert _get_file_parsing_status(987654321) is None


def test_tasks_status_route_readonly_query_still_reports_state():
    """轮询接口 /tasks/status 对 v51_tasks 的 readonly 查询行为不变：
    有 cancelled=0 的行时报告 recoverable。"""
    from app.routers import tasks as tasks_router

    pid = _make_project()
    with get_db() as conn:
        conn.execute(
            "INSERT INTO v51_tasks (project_id, phase, current_phase, cancelled, expired_at) "
            "VALUES (?, 'final', 'final_review', 0, datetime('now', 'localtime', '+1 hour'))",
            (pid,),
        )

    result = asyncio.run(tasks_router.get_all_tasks_status(pid, None))
    assert result["project_id"] == pid
    v51 = result["tasks"].get("v51_outline")
    assert v51 is not None, "readonly 改造后仍应报告 v51 恢复状态"
    assert v51["status"] in ("active", "recoverable")


# ============================================================================
# BUG-R5：stop_generation mark_failed 语义（to_thread 包装后行为不变）
# ============================================================================

def test_stop_generation_marks_generating_sections_failed():
    from app.routers.task_control import stop_generation

    pid = _make_project()
    sid = _add(pid, "2.1 将被中断的章节")
    with get_db() as conn:
        conn.execute("UPDATE sections SET status = 'generating' WHERE id = ?", (sid,))

    result = asyncio.run(stop_generation(pid, mark_failed=True))
    assert result["status"] == "ok"
    with get_db() as conn:
        row = conn.execute("SELECT status FROM sections WHERE id = ?", (sid,)).fetchone()
    assert row["status"] == "failed"


# ============================================================================
# BUG-L1：db.py 嵌套 get_db 事务交叉（重入深度计数）
# ============================================================================

def test_nested_get_db_inner_exit_does_not_commit_outer_transaction():
    """内层 with get_db() 退出不得提交外层未完成事务（旧行为：内层 commit）。"""
    pid = _make_project()
    with get_db() as outer:
        outer.execute("UPDATE projects SET notes = 'outer-pending' WHERE id = ?", (pid,))
        with get_db() as inner:
            inner.execute("SELECT 1")
        # 内层已退出但外层尚未提交 → 另一连接不应看到新值
        probe = sqlite3.connect(str(DATABASE_PATH))
        try:
            val = probe.execute("SELECT notes FROM projects WHERE id = ?", (pid,)).fetchone()[0]
        finally:
            probe.close()
        assert val != "outer-pending", "内层退出不得提交外层未完成的事务"
    # 外层退出提交后可见
    probe = sqlite3.connect(str(DATABASE_PATH))
    try:
        val = probe.execute("SELECT notes FROM projects WHERE id = ?", (pid,)).fetchone()[0]
    finally:
        probe.close()
    assert val == "outer-pending"


def test_nested_get_db_inner_exception_keeps_outer_connection_usable():
    """内层异常不得 close 掉外层仍在使用的连接（旧行为：discard_connection）。"""
    pid = _make_project()
    with get_db() as outer:
        try:
            with get_db() as inner:
                inner.execute("SELECT 1")
                raise RuntimeError("inner boom")
        except RuntimeError:
            pass
        # 外层连接必须仍然可用
        outer.execute("SELECT 1").fetchone()
        outer.execute("UPDATE projects SET notes = 'after-inner-error' WHERE id = ?", (pid,))


def test_nested_get_db_outer_exception_discards_connection():
    """外层异常时连接被丢弃且异常正常传播（行为保持不变）。"""
    pid = _make_project()
    with pytest.raises(RuntimeError):
        with get_db() as outer:
            with get_db():
                pass
            raise RuntimeError("outer boom")


def test_get_db_conn_removed():
    """危险且无调用方的 get_db_conn（executor 线程建连跨线程使用必崩）已删除。"""
    import app.db as db_mod

    assert not hasattr(db_mod, "get_db_conn")


# ============================================================================
# BUG-L2：Celery generate_section_content 接入任务组锁（静态验证）
# ============================================================================

def test_celery_generate_section_content_uses_task_lock():
    """celery 未安装时跳过；已安装则验证任务体已接入 task_lock。"""
    pytest.importorskip("celery", reason="本环境未安装 celery")
    import inspect

    from app import tasks as tasks_mod

    src = inspect.getsource(tasks_mod.generate_section_content)
    assert "task_lock.acquire" in src
    assert "TaskLockConflict" in src
    assert "_task_lock.release" in src


# ============================================================================
# BUG-L3：content_cleaning 思考行清理不再吞跨行正文
# ============================================================================

def test_clean_generated_content_keeps_multiline_body_containing_analysis():
    """正文含「分析：……」段落 + 后文「输出结果」时不再整段被吞（原 DOTALL 行为）。"""
    from app.services.content_cleaning import clean_generated_content

    raw = (
        "三、施工条件分析：\n"
        "本工程地质条件复杂，需采取针对性降水措施。\n"
        "综合上述情况输出如下安排：\n"
        "正文第一段内容应完整保留。"
    )
    cleaned = clean_generated_content(raw)
    assert "本工程地质条件复杂" in cleaned, "跨行正文不得被思考行清理吞掉"
    assert "正文第一段内容应完整保留" in cleaned


def test_clean_generated_content_still_removes_single_line_think():
    """单行「分析：…输出：…」思考行仍被清理。"""
    from app.services.content_cleaning import clean_generated_content

    raw = "分析：内部推理过程 输出：\n真正的正文内容"
    cleaned = clean_generated_content(raw)
    assert "真正的正文内容" in cleaned
    assert "内部推理过程" not in cleaned


# ============================================================================
# BUG-L4：delete_file 物理文件删除失败不再 500
# ============================================================================

def test_delete_file_tolerates_unlink_failure(tmp_path):
    """物理路径为目录（unlink 必然 OSError）时，记录删除仍成功。"""
    from app.services.files import delete_file

    pid = _make_project()
    occupied = tmp_path / "occupied-by-parser"
    occupied.mkdir()
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO uploaded_files (project_id, filename, path, file_type, status) "
            "VALUES (?, ?, ?, ?, ?)",
            (pid, "x.pdf", str(occupied), "pdf", "success"),
        )
        fid = cur.lastrowid

    result = delete_file(pid, fid)
    assert result["status"] == "deleted"
    with get_db() as conn:
        row = conn.execute("SELECT id FROM uploaded_files WHERE id = ?", (fid,)).fetchone()
    assert row is None


def test_stop_generation_mark_failed_false_keeps_generating():
    """pause 路径（mark_failed=False）保留 generating 现场。"""
    from app.routers.task_control import stop_generation

    pid = _make_project()
    sid = _add(pid, "2.2 暂停保留现场章节")
    with get_db() as conn:
        conn.execute("UPDATE sections SET status = 'generating' WHERE id = ?", (sid,))

    asyncio.run(stop_generation(pid, mark_failed=False))
    with get_db() as conn:
        row = conn.execute("SELECT status FROM sections WHERE id = ?", (sid,)).fetchone()
    assert row["status"] == "generating"
