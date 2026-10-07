"""写锁争用可重试性回归测试（配合 test_rate_gate_2026_10_07.py）。

BUG-FIX 2026-10-07：修复 `logs/backend.log` 中 8 次
    app.routers.sections:489 自动修复结果落库失败 project=990308: db locked

根因：db.py 的进程级写锁（_db_write_lock）超时抛的是**裸 RuntimeError**，
而 `_is_io_error` 只识别 sqlite3.OperationalError，导致这条典型的瞬时争用
既不重试、也没有 5xx 语义，直接变成 500 抛给用户。

验证：
  1. DatabaseWriteLockTimeout 被判定为可重试
  2. 因此 query_retry / get_db_with_retry 会真正重试该异常
  3. 未受影响的类型（如 ValueError）仍按原逻辑立即抛出，不被误吞
"""

import sqlite3

import pytest

from app.db import DatabaseWriteLockTimeout, _is_io_error, query_retry


class TestIsIoError:
    def test_write_lock_timeout_is_retryable(self):
        assert _is_io_error(DatabaseWriteLockTimeout("获取数据库写锁超时（8.0秒）")) is True

    def test_sqlite_locked_is_retryable(self):
        assert _is_io_error(sqlite3.OperationalError("database is locked")) is True
        assert _is_io_error(sqlite3.OperationalError("disk I/O error")) is True

    def test_unrelated_errors_are_not_retryable(self):
        assert _is_io_error(ValueError("bad input")) is False
        assert _is_io_error(RuntimeError("something else")) is False
        assert _is_io_error(sqlite3.IntegrityError("UNIQUE constraint failed")) is False


class TestQueryRetryWithWriteLock:
    def test_retries_on_write_lock_timeout_then_succeeds(self):
        """第 1、2 次争用失败，第 3 次成功 —— 必须重试而非直接抛出。"""
        calls = {"n": 0}

        @query_retry(max_retries=3, retry_delay=0.01)
        def _do():
            calls["n"] += 1
            if calls["n"] < 3:
                raise DatabaseWriteLockTimeout("获取数据库写锁超时（8.0秒）")
            return "ok"

        assert _do() == "ok"
        assert calls["n"] == 3, "写锁超时必须触发重试"

    def test_exhausted_retries_raise_original(self):
        @query_retry(max_retries=2, retry_delay=0.01)
        def _always_locked():
            raise DatabaseWriteLockTimeout("获取数据库写锁超时（8.0秒）")

        with pytest.raises(DatabaseWriteLockTimeout):
            _always_locked()

    def test_non_retryable_error_propagates_immediately(self):
        """非I/O 错误必须立刻抛出，不能浪费重试预算。"""
        calls = {"n": 0}

        @query_retry(max_retries=3, retry_delay=0.01)
        def _bad():
            calls["n"] += 1
            raise ValueError("参数错误")

        with pytest.raises(ValueError):
            _bad()
        assert calls["n"] == 1, "非可重试异常不应重试"


class TestWriteLockTimeoutSemantics:
    def test_is_runtime_error_subclass(self):
        """刻意保持 RuntimeError 子类：现有 `except RuntimeError` 分支不受影响。"""
        assert issubclass(DatabaseWriteLockTimeout, RuntimeError)

    def test_not_subclass_of_operational_error(self):
        """不应误冒充 SQLite 原生异常，避免被当作连接损坏丢弃连接的逻辑误判。"""
        assert not issubclass(DatabaseWriteLockTimeout, sqlite3.OperationalError)

    def test_message_mentions_concurrency_hint(self):
        err = DatabaseWriteLockTimeout("x")
        assert str(err) == "x"
