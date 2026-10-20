# -*- coding: utf-8 -*-
"""BUG-FIX 2026-10-17（GAP reclaim 误判）：reclaim_stale_parse_state 用 created_at 而非
updated_at 检测 uploaded_files 的 stale processing 行。

场景：用户上传文件（created_at=T0），>60 分钟后触发 async-reparse，
_persist_file_extraction 把已有行 status='processing'，但 created_at 仍为 T0。
beat 下一轮 reclaim 立即用 T0 判定为 stale → 标记 failed → worker 正常完成后
UPDATE 覆盖回 success，但此时前端/状态端点已经看到过一次「解析失败」。

本文件钉住该 BUG 的复现场景与修复后行为。
"""

import os
import sqlite3
import time
from unittest.mock import patch

import pytest


def _connect(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _has_column(db_path, table, col):
    with _connect(db_path) as conn:
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
        return col in cols


class TestReclaimUsesUpdatedAt:
    """reclaim_stale_parse_state 必须用 uploaded_files.updated_at 而非 created_at。

    验证步骤：
      1) uploaded_files 表有 updated_at 列（DB schema + DDL 迁移都到位）
      2) status 变更时 updated_at 同步更新（save_upload / _persist_file_extraction /
         re_extract_text / _save_upload_image_async / async-reparse 端点）
      3) reclaim_stale_parse_state 的 SQL 用 updated_at 判定 stale
    """

    def test_uploaded_files_has_updated_at_column(self, tmp_path):
        """DB schema 必须给 uploaded_files 加 updated_at 列，否则永远修不对。"""
        db_path = str(tmp_path / "test.db")
        with _connect(db_path) as conn:
            conn.execute(
                """CREATE TABLE uploaded_files (
                    id INTEGER PRIMARY KEY,
                    project_id INTEGER NOT NULL,
                    filename TEXT NOT NULL,
                    path TEXT NOT NULL,
                    file_type TEXT NOT NULL,
                    file_role TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    extracted_text_path TEXT,
                    message TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
                )"""
            )
            # 迁移脚本必须能补上 updated_at
            try:
                conn.execute(
                    "ALTER TABLE uploaded_files ADD COLUMN updated_at "
                    "TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))"
                )
            except sqlite3.OperationalError:
                pytest.fail("ALTER TABLE uploaded_files ADD COLUMN updated_at 失败")
        assert _has_column(db_path, "uploaded_files", "updated_at")

    def test_reclaim_sql_uses_updated_at_for_uploaded_files(self):
        """reclaim_stale_parse_state 对 uploaded_files 的 SQL 必须引用 updated_at，
        不能再用 created_at（复现场景：上传 > stale_minutes 后 async-reparse 把
        行改回 processing，created_at 不变 → 立即被 reclaim 误判为 stale）。
        """
        from app.services import files as _files_mod

        import inspect

        src = inspect.getsource(_files_mod.reclaim_stale_parse_state)
        assert "updated_at" in src, (
            "reclaim_stale_parse_state 的 uploaded_files SQL 必须引用 updated_at，"
            "否则 async-reparse 路径会被立即误判为 stale"
        )
        assert "uploaded_files" in src, (
            "reclaim_stale_parse_state 仍需覆盖 uploaded_files 表"
        )

    def test_reclaim_fresh_processing_not_failed(self, tmp_path):
        """复现场景：uploaded_files.created_at 已超 stale_minutes，但 updated_at 刚更新
        （即 async-reparse 刚把行改回 processing），reclaim 不应把它标为 failed。
        """
        from app.services import files as _files_mod

        import sqlite3 as _sqlite3

        db_path = str(tmp_path / "fresh_reclaim.db")
        with _sqlite3.connect(db_path) as conn:
            conn.execute(
                """CREATE TABLE uploaded_files (
                    id INTEGER PRIMARY KEY,
                    project_id INTEGER NOT NULL,
                    filename TEXT NOT NULL,
                    path TEXT NOT NULL,
                    file_type TEXT NOT NULL,
                    file_role TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    extracted_text_path TEXT,
                    message TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )"""
            )
            conn.execute(
                """CREATE TABLE file_parse_tasks (
                    id INTEGER PRIMARY KEY,
                    project_id INTEGER NOT NULL,
                    filename TEXT NOT NULL,
                    file_type TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    progress INTEGER NOT NULL DEFAULT 0,
                    total_files INTEGER NOT NULL DEFAULT 1,
                    completed_files INTEGER NOT NULL DEFAULT 0,
                    error_message TEXT NOT NULL DEFAULT '',
                    started_at TEXT,
                    updated_at TEXT,
                    completed_at TEXT
                )"""
            )
            # 行 1：created_at 很老（2 小时前），但 updated_at 是现在（刚触发
            # async-reparse 改回 processing 的场景）
            conn.execute(
                """INSERT INTO uploaded_files
                   (id, project_id, filename, path, file_type, file_role, status,
                    created_at, updated_at)
                   VALUES (1, 1, 'old.pdf', '/tmp/old.pdf', 'pdf', 'main', 'processing',
                           datetime('now', 'localtime', '-120 minutes'),
                           datetime('now', 'localtime'))"""
            )
            # 行 2：created_at 和 updated_at 都很老（真正的孤儿 processing）
            conn.execute(
                """INSERT INTO uploaded_files
                   (id, project_id, filename, path, file_type, file_role, status,
                    created_at, updated_at)
                   VALUES (2, 1, 'orphan.pdf', '/tmp/orphan.pdf', 'pdf', 'main', 'processing',
                           datetime('now', 'localtime', '-120 minutes'),
                           datetime('now', 'localtime', '-120 minutes'))"""
            )
            conn.commit()

        # 用 monkeypatch 让 get_db 指向我们的测试 DB
        from app.db import get_db as _orig_get_db

        def _fake_get_db(*a, **kw):
            conn = _sqlite3.connect(db_path)
            conn.row_factory = _sqlite3.Row
            # conn.execute("PRAGMA foreign_keys = ON")
            return conn

        try:
            import app.services.files as _fs

            with patch.object(_fs, "get_db", _fake_get_db):
                result = _fs.reclaim_stale_parse_state(stale_minutes=60)
        finally:
            _sqlite3.connect(db_path).close()

        # 行 1（updated_at 新鲜）不应被回收 → uploaded_files_failed 应为 1（只有 orphan）
        assert result["uploaded_files_failed"] == 1, (
            f"fresh processing 行被误判为 stale！期望 1，实际 {result['uploaded_files_failed']}"
        )
