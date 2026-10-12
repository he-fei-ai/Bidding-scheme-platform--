"""上传解析模块 2026-10-11 加固测试

钉住三类修复：
  BUG-1  _text_cache_clear 竞态条件（两锁块间隙被并发切入）
  BUG-2  _extract_doc 重复死代码分支（第二个 elif 用旧的 ignore）
  BUG-3  _upload_write_locks 无界增长（LRU 清理机制）

本文件所有测试共享 autouse fixture，自动保存/恢复模块级可变状态
（_text_cache、_project_versions、_upload_write_locks、_UPLOAD_WRITE_LOCK_MAX、
_UPLOAD_WRITE_LOCK_IDLE_SECS），避免单个测试失败后污染后续测试。
"""
import threading
import time
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _clean_module_state():
    """自动清理 files.py 模块级可变状态，每个测试后恢复原值。

    失败的断言会让后续测试看到被污染的字典和常量值。此 fixture
    在每个测试函数/类方法结束后统一恢复，无需每个测试手写 try/finally。
    """
    from app.services import files as files_mod

    # 保存原值
    saved_text_cache = dict(files_mod._text_cache)
    saved_project_versions = dict(files_mod._project_versions)
    saved_locks = dict(files_mod._upload_write_locks)
    saved_max = files_mod._UPLOAD_WRITE_LOCK_MAX
    saved_idle = files_mod._UPLOAD_WRITE_LOCK_IDLE_SECS

    try:
        yield
    finally:
        files_mod._text_cache.clear()
        files_mod._text_cache.update(saved_text_cache)
        files_mod._project_versions.clear()
        files_mod._project_versions.update(saved_project_versions)
        files_mod._upload_write_locks.clear()
        files_mod._upload_write_locks.update(saved_locks)
        files_mod._UPLOAD_WRITE_LOCK_MAX = saved_max
        files_mod._UPLOAD_WRITE_LOCK_IDLE_SECS = saved_idle


# ─────────────────────────────────────────────────────────────────────────
# BUG-1：_text_cache_clear 竞态条件（pop + 版本递增原子完成）
# ─────────────────────────────────────────────────────────────────────────


def test_text_cache_clear_single_lock_sweeps_race():
    """BUG-1：clear 的 pop + 版本递增必须在同一锁块内。

    修复前两者分在两个独立的 with _text_cache_lock 块中 —— 两锁之间
    可能被 latest_extracted_text 的并发读切入，在 pop 之后、版本递增
    之前写入陈旧缓存（带着旧版本号，绕过 I-10 守卫）。
    修复后两者在同一锁块中原子完成。
    """
    from app.services import files as files_mod

    project_id = 77771
    files_mod._text_cache[project_id] = ("old text", time.time())
    files_mod._project_versions[project_id] = 5

    results = []

    def racer():
        with files_mod._text_cache_lock:
            # 此锁与 clear 中的锁互斥 —— 若 clear 的 pop/版本递增分块，
            # 本线程有机会在两锁块之间切入
            pass

    def reader_writer():
        start = files_mod._get_project_version(project_id)
        cached = files_mod._text_cache_get(project_id)
        if cached is None:
            files_mod._text_cache_set(project_id, "new text", since_version=start)
            results.append(("reader_wrote_stale", start))
        else:
            results.append(("cache_hit", start))

    t1 = threading.Thread(target=racer)
    t2 = threading.Thread(target=reader_writer)
    t1.start()
    t2.start()
    files_mod._text_cache_clear(project_id)
    t1.join()
    t2.join()

    final_version = files_mod._get_project_version(project_id)
    final_cache = files_mod._text_cache.get(project_id, (None,))[0]
    assert final_version == 6, f"clear 后版本应为 6，实际 {final_version}"
    assert final_cache is None, f"clear 后缓存应被清除，实际 {final_cache!r}"


def test_latest_extracted_text_version_guard_cross_check():
    """回归：clear 修复后，latest_extracted_text 的版本守卫仍正确工作。

    并发场景：两线程同时 clear（版本升为 11）和 read（start_version=10）。
    修复后 clear 的版本递增与 pop 原子完成，读操作看不到中间态。
    """
    from app.services import files as files_mod

    pid = 66661
    files_mod._project_versions[pid] = 10
    files_mod._text_cache[pid] = ("old text", time.time())

    def read_and_write():
        start = files_mod._get_project_version(pid)
        files_mod._text_cache_set(pid, "computed", since_version=start)

    t_clear = threading.Thread(target=lambda: files_mod._text_cache_clear(pid))
    t_read = threading.Thread(target=read_and_write)
    t_clear.start()
    t_read.start()
    t_clear.join()
    t_read.join()

    assert files_mod._get_project_version(pid) == 11


# ─────────────────────────────────────────────────────────────────────────
# BUG-2：_extract_doc 重复死代码分支
# ─────────────────────────────────────────────────────────────────────────


def test_extract_doc_no_duplicate_elif_branch():
    """BUG-2：_extract_doc 的 olefile 循环中只能有一个
    `elif "text" in name or "table" in name:` 分支。

    修复前第二个分支因 if/elif/elif 结构永远不可达（死代码），
    且用旧的 errors="ignore"（违背 2026-10-08 的 ignore→replace 修复）。
    本测试扫描源码验证只有一个匹配。
    """
    from app.services import files as files_mod

    source_file = Path(files_mod.__file__)
    doc_lines = source_file.read_text(encoding="utf-8").split("\n")

    # 定位 _extract_doc 函数源码范围
    in_func = False
    func_lines = []
    for i, line in enumerate(doc_lines, 1):
        if "def _extract_doc(" in line:
            in_func = True
            continue
        if in_func and line.strip().startswith("def "):
            break
        if in_func:
            func_lines.append((i, line))

    hits = []
    for i, line in func_lines:
        stripped = line.strip()
        if stripped.startswith('elif "text" in name or "table" in name:'):
            hits.append(i)

    assert len(hits) == 1, (
        f"_extract_doc 中应只有一个 elif text/table 分支，实际 {len(hits)} 个 "
        f"（行号 {hits}）。重复的第二个分支是死代码。"
    )


# ─────────────────────────────────────────────────────────────────────────
# BUG-3：_upload_write_locks LRU 清理
# ─────────────────────────────────────────────────────────────────────────


class TestUploadWriteLocksLRU:
    """BUG-3：_upload_write_locks 字典必须能清理闲置锁，避免内存泄漏。

    每个测试后由模块级 autouse fixture 清理所有可变状态，
    方法内无需再手动恢复常量。
    """

    def test_lru_cleanup_removes_idle_locks(self):
        """超过 MAX 且闲置超时 → 被清理"""
        from app.services import files as files_mod

        now = time.monotonic()
        files_mod._UPLOAD_WRITE_LOCK_MAX = 3
        files_mod._upload_write_locks[99991] = (threading.Lock(), now - 7200)  # 闲置 2h
        files_mod._upload_write_locks[99992] = (threading.Lock(), now - 7200)
        files_mod._upload_write_locks[99993] = (threading.Lock(), now - 100)  # 刚刚

        _ = files_mod._get_upload_write_lock(99994)  # 超过阈值触发清理

        assert 99991 not in files_mod._upload_write_locks, "闲置 2h 的锁应被清理"
        assert 99992 not in files_mod._upload_write_locks
        assert 99993 in files_mod._upload_write_locks, "刚刚被访问的锁不应被清理"
        assert 99994 in files_mod._upload_write_locks

    def test_lru_cleanup_keeps_active_locks(self):
        """所有锁都活跃 → 不应被清理"""
        from app.services import files as files_mod

        now = time.monotonic()
        files_mod._UPLOAD_WRITE_LOCK_MAX = 3
        for pid in (88881, 88882, 88883):
            files_mod._upload_write_locks[pid] = (threading.Lock(), now - 100)

        _ = files_mod._get_upload_write_lock(88884)

        for pid in (88881, 88882, 88883, 88884):
            assert pid in files_mod._upload_write_locks, f"活跃锁 {pid} 不应被清理"

    def test_get_lock_updates_timestamp(self):
        """重复获取同一 project_id 的锁应更新时间戳（防清理）"""
        from app.services import files as files_mod

        files_mod._UPLOAD_WRITE_LOCK_IDLE_SECS = 100
        now = time.monotonic()
        files_mod._upload_write_locks[55555] = (threading.Lock(), now - 1000)
        old_ts = files_mod._upload_write_locks[55555][1]

        time.sleep(0.01)
        _ = files_mod._get_upload_write_lock(55555)
        new_ts = files_mod._upload_write_locks[55555][1]

        assert new_ts > old_ts, f"时间戳应更新 old={old_ts} new={new_ts}"
