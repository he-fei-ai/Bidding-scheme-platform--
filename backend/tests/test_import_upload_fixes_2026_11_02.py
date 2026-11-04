# -*- coding: utf-8 -*-
"""上传解析模块 2026-11-02 深度审计修复的回归测试（B1/B2/B3/B5）。

对应缺陷（均有代码行号证据，见审计报告）：
  B1 _save_upload_image_async：图片 uploaded_files INSERT 失败不回滚物理文件 → 孤儿图片；
  B2 save_upload：去重检查在上传锁内、INSERT 在锁外（提取之后），并发同内容击穿去重；
  B3 save_upload 失败重传自愈：只删旧失败 DB 行不删其物理文件 → 孤儿文件 + 同名重传被改名 _1；
  B5 file_parse_tasks.status 默认 'queued' 从不被写入（僵尸态陷阱），应等于 'processing'。

测试策略：全部在服务层用真实临时文件 + monkeypatch 精确复现缺陷分支，
确定性可复现，不依赖真实并发线程。
"""
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.db import get_db  # noqa: E402
from app.models import ProjectCreate  # noqa: E402
from app.services import files as file_service  # noqa: E402
from app.services.projects import create_project, delete_project  # noqa: E402


def _uploads_dir(pid: int) -> Path:
    return file_service.PROJECTS_DATA_DIR / str(pid) / "uploads"


def _texts_dir(pid: int) -> Path:
    return file_service.PROJECTS_DATA_DIR / str(pid) / "texts"


def _uploaded_rows(pid: int):
    with get_db(readonly=True) as conn:
        return conn.execute(
            "SELECT id, filename, status FROM uploaded_files WHERE project_id = ? ORDER BY id",
            (pid,),
        ).fetchall()


def _processing_task_count(pid: int) -> int:
    with get_db(readonly=True) as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM file_parse_tasks WHERE project_id = ? AND status = 'processing'",
            (pid,),
        ).fetchone()[0]


# ---------------------------------------------------------------------------
# B1：图片异步快路径 DB 落库失败应回滚物理文件
# ---------------------------------------------------------------------------
class _FakeConnFailOnUploadInsert:
    """get_db 替身：仅让 uploaded_files 的 INSERT 抛错，模拟 DB 落库失败。"""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        if "INSERT INTO uploaded_files" in str(sql):
            raise RuntimeError("simulated DB down")

        class _Cur:
            rowcount = 0
            lastrowid = None

        return _Cur()


class TestB1ImageOrphanRollback:
    def test_db_insert_failure_unlinks_physical_image(self, monkeypatch, tmp_path):
        """uploaded_files INSERT 失败时，已落盘的图片物理文件必须被回滚删除。"""
        pid = create_project(ProjectCreate(name="B1-图片孤儿回滚")).id
        try:
            img = tmp_path / "扫描图.png"
            img.write_bytes(b"\x89PNG\r\n\x1a\n fake png body for rollback test")
            assert img.exists()

            # 让 save_upload/_save_upload_image_async 里裸用的模块级 get_db 抛错
            monkeypatch.setattr(
                file_service, "get_db", lambda *a, **k: _FakeConnFailOnUploadInsert()
            )
            r = file_service._save_upload_image_async(
                pid, "扫描图.png", str(img), "supporting"
            )
            assert r["status"] == "failed", r
            assert not img.exists(), "DB 落库失败后图片物理文件应被回滚删除（B1）"
        finally:
            delete_project(pid)


# ---------------------------------------------------------------------------
# B2 / R1：并发同内容上传 —— 写锁内 content_hash 等值复核命中应回滚后到者
# ---------------------------------------------------------------------------
class TestB2ConcurrentDedup:
    def test_inlock_hash_recheck_rolls_back_late_committer(self, monkeypatch):
        """确定性复现击穿：早期查重（_find_duplicate_upload）漏看对端未提交行，
        但对端此刻已把同 content_hash 行提交入库；save_upload 在 get_db 写锁内的
        content_hash 等值复核命中 → 判 duplicate 后到者。

        期望：不新建 uploaded_files 行（除对端那一行）、回滚已落盘文件、
        不留 processing 任务行、返回 duplicate + duplicate_of 指向对端。
        """
        import hashlib as _hl

        pid = create_project(ProjectCreate(name="B2-并发去重击穿")).id
        try:
            content = "招标正文 工期100天 唯一内容 B2".encode("utf-8")
            digest = _hl.sha256(content).hexdigest()
            # 预置一条「并发对端已提交」的同哈希行（走真实 DB，供写锁内等值命中）。
            with get_db() as conn:
                cur = conn.execute(
                    "INSERT INTO uploaded_files "
                    "(project_id, filename, path, file_type, file_role, status, "
                    " content_hash, message) "
                    "VALUES (?, '招标文件.txt', ?, 'txt', 'main', 'success', ?, '')",
                    (pid, str(_uploads_dir(pid) / "peer.txt"), digest),
                )
                peer_id = cur.lastrowid

            # 强制早期查重漏看（模拟对端当时尚未提交），把判重交给写锁内 content_hash 复核。
            monkeypatch.setattr(file_service, "_find_duplicate_upload", lambda *a, **k: None)

            r = file_service.save_upload(pid, "招标文件.txt", content, "main")

            assert r["status"] == "duplicate", r
            assert r["duplicate_of"] == peer_id, r
            # 只应有对端那一行，本后到者不得再插入第二行
            rows = _uploaded_rows(pid)
            assert len(rows) == 1 and rows[0]["id"] == peer_id, rows
            # 已落盘的原始文件应被回滚删除
            assert not (_uploads_dir(pid) / "招标文件.txt").exists(), "后到者物理文件应回滚"
            # 不应残留 processing 任务行
            assert _processing_task_count(pid) == 0, "后到者任务行应被删除（不留 processing 残留）"
        finally:
            delete_project(pid)


# ---------------------------------------------------------------------------
# R1：成功上传应把内容 SHA-256 落到 uploaded_files.content_hash
# ---------------------------------------------------------------------------
class TestR1ContentHashStored:
    def test_success_stores_content_hash(self):
        import hashlib as _hl

        pid = create_project(ProjectCreate(name="R1-哈希落列")).id
        try:
            content = "R1 唯一招标内容 落哈希".encode("utf-8")
            r = file_service.save_upload(pid, "r1.txt", content, "main")
            assert r["status"] == "success", r
            with get_db(readonly=True) as conn:
                row = conn.execute(
                    "SELECT content_hash FROM uploaded_files WHERE id = ?",
                    (r["id"],),
                ).fetchone()
            assert row is not None, row
            assert row["content_hash"] == _hl.sha256(content).hexdigest(), row
        finally:
            delete_project(pid)

    def test_hash_equal_query_supersedes_full_file_scan(self, monkeypatch):
        """已落哈希的历史行由 content_hash 等值命中，无需读盘扫描（提速 + 竞争免疫基础）。"""
        import hashlib as _hl

        pid = create_project(ProjectCreate(name="R1-等值命中")).id
        try:
            content = "R1 等值命中唯一内容".encode("utf-8")
            digest = _hl.sha256(content).hexdigest()
            with get_db() as conn:
                conn.execute(
                    "INSERT INTO uploaded_files "
                    "(project_id, filename, path, file_type, file_role, status, "
                    " content_hash, message) "
                    "VALUES (?, 'peer.txt', ?, 'txt', 'main', 'success', ?, '')",
                    (pid, str(_uploads_dir(pid) / "nonexistent_peer.txt"), digest),
                )
            # path 指向不存在的文件：旧全量扫描会 os.stat 失败漏判；哈希等值应命中。
            hit = file_service._find_duplicate_upload(pid, content)
            assert hit is not None and hit["filename"] == "peer.txt", hit
        finally:
            delete_project(pid)



# ---------------------------------------------------------------------------
# B3：失败重传自愈应删除旧失败行物理文件，复用原名、不留孤儿
# ---------------------------------------------------------------------------
class TestB3SelfHealOrphanCleanup:
    def test_reupload_failed_reuses_original_name_and_removes_orphan(self):
        """重传同一失败文件：删除旧行 + 旧物理文件 + 旧提取文本，同名覆盖不留 _1、不留孤儿。"""
        pid = create_project(ProjectCreate(name="B3-自愈孤儿清理")).id
        try:
            content = "自愈重传正文内容 B3 唯一".encode("utf-8")
            uploads = _uploads_dir(pid)
            texts = _texts_dir(pid)
            uploads.mkdir(parents=True, exist_ok=True)
            texts.mkdir(parents=True, exist_ok=True)

            old_file = uploads / "f.txt"
            old_file.write_bytes(content)  # 与重传内容一致，保证按内容哈希命中
            old_txt = texts / "f.txt.txt"
            old_txt.write_text("损坏的旧提取文本", encoding="utf-8")

            with get_db() as conn:
                conn.execute(
                    "INSERT INTO uploaded_files "
                    "(project_id, filename, path, file_type, file_role, status, "
                    " extracted_text_path, message) "
                    "VALUES (?, 'f.txt', ?, 'txt', 'main', 'failed', ?, '旧解析失败')",
                    (pid, str(old_file), str(old_txt)),
                )

            r = file_service.save_upload(pid, "f.txt", content, "main")

            assert r["status"] == "success", r
            assert r["filename"] == "f.txt", f"同名重传应复用原名而非改名 _1：{r}"
            # 恰好一行 uploaded_files（旧失败行被删、新行插入）
            rows = _uploaded_rows(pid)
            assert len(rows) == 1, rows
            assert rows[0]["filename"] == "f.txt", rows
            # 不应产生 _1 文件
            assert not (uploads / "f_1.txt").exists(), "不应因旧文件残留而改名 f_1.txt"
            # 同名重传会把提取文本写回同一路径：旧「损坏」内容应被新提取内容覆盖，
            # 而非留下无人指向的孤儿（新 uploaded_files 行正指向它）。
            assert old_txt.exists()
            body = old_txt.read_text(encoding="utf-8")
            assert "损坏的旧提取文本" not in body, f"旧失败提取文本应被自愈覆盖：{body!r}"
            assert "自愈重传正文内容" in body, f"应写入本次重传的新提取内容：{body!r}"
        finally:
            delete_project(pid)


# ---------------------------------------------------------------------------
# B5：file_parse_tasks.status 默认值收敛到 'processing'（消除僵尸态陷阱）
# ---------------------------------------------------------------------------
class TestB5ParseTaskStatusDefault:
    def test_migration_ddl_default_is_processing_not_queued(self):
        """新建库 DDL 的 status 默认值应为 'processing'，不得残留 'queued'。"""
        from app import db_migrations

        src = Path(db_migrations.__file__).read_text(encoding="utf-8")
        # file_parse_tasks 段落内不再出现 DEFAULT 'queued'
        seg = src[src.index("CREATE TABLE IF NOT EXISTS file_parse_tasks"):]
        seg = seg[: seg.index(")")]
        assert "status TEXT NOT NULL DEFAULT 'processing'" in seg, seg
        assert "DEFAULT 'queued'" not in seg, seg

    def test_create_parse_task_row_is_processing(self):
        """_create_parse_task 落库的任务行状态恒为 processing（不依赖列默认值）。"""
        pid = create_project(ProjectCreate(name="B5-任务状态默认值")).id
        try:
            file_service._create_parse_task(pid, "护栏.txt", "txt")
            with get_db(readonly=True) as conn:
                row = conn.execute(
                    "SELECT status FROM file_parse_tasks "
                    "WHERE project_id = ? AND filename = '护栏.txt' ORDER BY id DESC LIMIT 1",
                    (pid,),
                ).fetchone()
            assert row is not None and row["status"] == "processing", row
        finally:
            delete_project(pid)


# ---------------------------------------------------------------------------
# B4：任务终态按 task_id / file_id 精确命中，同名多 processing 行不再交叉串写
# ---------------------------------------------------------------------------
def _task_status_by_id(task_id: int) -> str:
    with get_db(readonly=True) as conn:
        row = conn.execute(
            "SELECT status FROM file_parse_tasks WHERE id = ?", (task_id,)
        ).fetchone()
    return row["status"] if row else "<missing>"


class TestB4NoCrossWrite:
    def test_update_by_task_id_only_hits_target_row(self):
        """两行同名 processing 任务，按 task_id 更新其中一行 → 只有它变终态。

        回归根因：旧实现靠「project+filename+processing」匹配，同名两行会被同一条
        UPDATE 一起改写（串写）。task_id 精确命中后，另一行必须仍是 processing。
        """
        pid = create_project(ProjectCreate(name="B4-无串写")).id
        try:
            t1 = file_service._create_parse_task(pid, "同名.txt", "txt")
            t2 = file_service._create_parse_task(pid, "同名.txt", "txt")
            assert t1 and t2 and t1 != t2, (t1, t2)
            assert _task_status_by_id(t1) == "processing"
            assert _task_status_by_id(t2) == "processing"

            file_service._update_parse_task(
                pid, "completed", 100, "", "同名.txt", task_id=t1
            )

            assert _task_status_by_id(t1) == "completed", "命中行应转终态"
            assert _task_status_by_id(t2) == "processing", "另一同名行不得被串写（B4）"
        finally:
            delete_project(pid)

    def test_force_failed_by_task_id_overrides_only_target(self):
        """force 覆盖终态同样按 task_id 精确命中，不误伤同名另一行。"""
        pid = create_project(ProjectCreate(name="B4-force无串写")).id
        try:
            t1 = file_service._create_parse_task(pid, "同名F.txt", "txt")
            t2 = file_service._create_parse_task(pid, "同名F.txt", "txt")
            file_service._force_parse_task_failed(
                pid, "同名F.txt", "写盘失败", task_id=t1
            )
            assert _task_status_by_id(t1) == "failed"
            assert _task_status_by_id(t2) == "processing", "force 不得串写同名另一行"
        finally:
            delete_project(pid)

    def test_link_and_update_by_file_id(self):
        """_link_parse_task_file 回填 file_id 后，按 file_id 精确收尾命中本行。"""
        pid = create_project(ProjectCreate(name="B4-file_id关联")).id
        try:
            t = file_service._create_parse_task(pid, "关联.txt", "txt")
            # 造一个真实文件行拿 file_id
            content = "B4 关联唯一内容".encode("utf-8")
            r = file_service.save_upload(pid, "关联源.txt", content, "main")
            fid = r["id"]

            file_service._link_parse_task_file(t, fid)
            with get_db(readonly=True) as conn:
                row = conn.execute(
                    "SELECT file_id FROM file_parse_tasks WHERE id = ?", (t,)
                ).fetchone()
            assert row and row["file_id"] == fid, row

            # 直接把该任务行改回 processing 以便用 file_id 收尾（模拟 worker 收尾时机）
            with get_db() as conn:
                conn.execute(
                    "UPDATE file_parse_tasks SET status='processing' WHERE id=?", (t,)
                )
            file_service._update_parse_task(
                pid, "completed", 100, "", "关联.txt", file_id=fid
            )
            assert _task_status_by_id(t) == "completed"
        finally:
            delete_project(pid)

