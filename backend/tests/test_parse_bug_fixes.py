"""解析模块 BUG 修复回归测试

覆盖本次修复的 3 个 BUG：
- B1: .txt/.md 硬编码 utf-8 读取，GBK 编码文件抛 UnicodeDecodeError → 解析失败
- B2: 提取文本文件名仅用 stem，同名不同扩展名文件互相覆盖（数据丢失）
- B3: 项目文本缓存（_text_cache，600s）在上传/删除/重提取/清空后不失效，
      导致 AI 分析使用旧文本

所有测试均使用临时目录与 mock DB，不污染真实数据。
"""

import asyncio
import shutil
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.services import files as files_mod
from app.services.files import extract_text_from_path, save_upload


# ---------- 工具: 临时环境 ----------

@pytest.fixture
def isolated_env(monkeypatch, tmp_path):
    """将 PROJECTS_DATA_DIR 指向临时目录，并替换 get_db 为 mock"""
    from app.config import PROJECTS_DATA_DIR as real_dir
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(files_mod, "PROJECTS_DATA_DIR", projects_dir)
    monkeypatch.setattr(files_mod, "_text_cache", {})
    return projects_dir


def make_db_mock(rows_by_project=None):
    """构造 get_db 的 mock 上下文管理器

    rows_by_project: {project_id: [row_dict, ...]},供 latest_extracted_text 查询
    """
    conn = MagicMock()
    result = MagicMock()
    if rows_by_project:
        def _fetchall():
            return rows_by_project.get(conn._project_id, [])
        result.fetchall.side_effect = _fetchall
    conn.execute.return_value = result

    class Ctx:
        def __enter__(self):
            return conn

        def __exit__(self, *a):
            return False

    return Ctx, conn


def make_upload_ctx():
    """save_upload 用的 mock DB（INSERT 返回递增 lastrowid）"""
    conn = MagicMock()
    cursor = MagicMock()
    cursor.lastrowid = 1
    conn.execute.return_value = cursor

    class Ctx:
        def __enter__(self):
            return conn

        def __exit__(self, *a):
            return False

    return Ctx, cursor


# ---------- B1: GBK txt 解析 ----------

class TestB1GbkTextFile:
    def test_gbk_txt_extracts_without_error(self, tmp_path):
        gbk_file = tmp_path / "招标文件.txt"
        gbk_file.write_bytes("第一章 招标公告\n这是GBK编码的中文内容".encode("gbk"))
        result = extract_text_from_path(str(gbk_file))
        assert "第一章 招标公告" in result
        assert "GBK编码" in result

    def test_gbk_md_extracts_without_error(self, tmp_path):
        gbk_file = tmp_path / "说明.md"
        gbk_file.write_bytes("# 标题\n中文内容".encode("gbk"))
        result = extract_text_from_path(str(gbk_file))
        assert "标题" in result

    def test_utf8_txt_still_works(self, tmp_path):
        f = tmp_path / "normal.txt"
        f.write_text("正常 utf-8 内容", encoding="utf-8")
        assert "正常" in extract_text_from_path(str(f))

    def test_corrupt_bytes_falls_back_gracefully(self, tmp_path):
        """既非 utf-8 也非 gb18030 的坏字节不应抛异常"""
        f = tmp_path / "bad.txt"
        f.write_bytes(b"\xff\xfe\x00\x01\x80 bad bytes")
        result = extract_text_from_path(str(f))  # 不应抛异常
        assert isinstance(result, str)


# ---------- B2: 同名不同扩展名文本文件不互相覆盖 ----------

class TestB2TextFilenameCollision:
    def test_same_stem_different_ext_no_overwrite(self, isolated_env, monkeypatch):
        ctx, _cursor = make_upload_ctx()
        monkeypatch.setattr(files_mod, "get_db", lambda: ctx())

        f1 = isolated_env / "招标文件.docx"
        f1.write_bytes(b"PK fake docx bytes")
        r1 = save_upload(99901, "招标文件.docx", f1.read_bytes(), "main")

        f2 = isolated_env / "招标文件.pdf"
        f2.write_bytes(b"%PDF-1.4 fake pdf")
        r2 = save_upload(99901, "招标文件.pdf", f2.read_bytes(), "supporting")

        # save_upload 返回值不含 extracted_text_path，按命名规则构造期望路径
        t1 = isolated_env / "99901" / "texts" / "招标文件.docx.txt"
        t2 = isolated_env / "99901" / "texts" / "招标文件.pdf.txt"
        # 修复后：文本文件名含完整原名+扩展名，路径必须不同
        assert t1.name == "招标文件.docx.txt", t1
        assert t2.name == "招标文件.pdf.txt", t2
        assert t1 != t2
        assert t1.exists() and t2.exists()

    def test_upload_clears_text_cache(self, isolated_env, monkeypatch):
        """save_upload 后必须使项目文本缓存失效（B3 关联）"""
        ctx, _cursor = make_upload_ctx()
        monkeypatch.setattr(files_mod, "get_db", lambda: ctx())
        files_mod._text_cache_set(99901, "旧缓存内容")
        f1 = isolated_env / "a.txt"
        f1.write_text("内容A", encoding="utf-8")
        save_upload(99901, "a.txt", f1.read_bytes(), "main")
        assert files_mod._text_cache_get(99901) is None


# ---------- B3: 文本缓存失效 ----------

class TestB3TextCacheInvalidation:
    def _ctx_with_rows(self, rows):
        conn = MagicMock()
        result = MagicMock()
        result.fetchall.return_value = rows
        conn.execute.return_value = result

        class Ctx:
            def __enter__(self):
                return conn

            def __exit__(self, *a):
                return False

        return Ctx

    def test_upload_after_cache_sees_new_file(self, isolated_env, monkeypatch):
        """上传 B 后（缓存已被 save_upload 清除），latest_extracted_text 必须包含 B"""
        a_txt = isolated_env / "a.txt"
        a_txt.write_text("这是招标文件A的完整内容", encoding="utf-8")
        b_txt = isolated_env / "b.txt"
        b_txt.write_text("这是招标文件B的内容", encoding="utf-8")

        def make_rows(*items):
            return [dict(item) for item in items]

        state = {"phase": 0}

        def get_db(readonly: bool = False, **_kwargs):
            # ENHANCE 2026-09-16：生产代码现在以 readonly=True 取连接（不抢写锁），
            # 替身必须跟上真实签名，否则会把新优化误测成 TypeError。
            if state["phase"] == 0:
                rows = make_rows(
                    {"id": 1, "filename": "招标文件A.docx", "file_role": "main",
                     "status": "success", "extracted_text_path": str(a_txt)},
                )
            else:
                rows = make_rows(
                    {"id": 1, "filename": "招标文件A.docx", "file_role": "main",
                     "status": "success", "extracted_text_path": str(a_txt)},
                    {"id": 2, "filename": "招标文件B.docx", "file_role": "supporting",
                     "status": "success", "extracted_text_path": str(b_txt)},
                )
            return self._ctx_with_rows(rows)()

        monkeypatch.setattr(files_mod, "get_db", get_db)

        # 第一次读：构建缓存（只有 A）
        state["phase"] = 0
        text1 = asyncio.run(files_mod.latest_extracted_text(99902))
        assert "招标文件A" in text1

        # 模拟上传 B：save_upload 内部应清除缓存
        state["phase"] = 1
        # save_upload 的 INSERT 路径
        ctx, cursor = make_upload_ctx()
        cursor.lastrowid = 2
        monkeypatch.setattr(files_mod, "get_db", lambda: ctx())
        files_mod.save_upload(99902, "招标文件B.docx", b_txt.read_bytes(), "supporting")
        assert files_mod._text_cache_get(99902) is None, "上传后缓存未失效"

        # 恢复查询 mock 并二次读取：必须包含 B
        monkeypatch.setattr(files_mod, "get_db", get_db)
        text2 = asyncio.run(files_mod.latest_extracted_text(99902))
        assert "招标文件B" in text2

    def test_delete_clears_cache(self, isolated_env, monkeypatch):
        ctx, _cursor = make_upload_ctx()
        monkeypatch.setattr(files_mod, "get_db", lambda: ctx())
        files_mod._text_cache_set(99903, "缓存")
        file_path = isolated_env / "x.txt"
        file_path.write_text("x", encoding="utf-8")
        text_path = isolated_env / "x.txt.txt"
        text_path.write_text("x", encoding="utf-8")
        # 模拟 DB 查询返回该文件记录
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = {
            "path": str(file_path), "extracted_text_path": str(text_path),
        }
        monkeypatch.setattr(files_mod, "get_db", lambda: ctx())
        # 覆盖 delete_file 内的 DB 读取
        real_delete = files_mod.delete_file

        class DeleteCtx:
            def __enter__(self_inner):
                return conn

            def __exit__(self_inner, *a):
                return False

        monkeypatch.setattr(files_mod, "get_db", lambda: DeleteCtx())
        files_mod.delete_file(99903, 1)
        assert files_mod._text_cache_get(99903) is None, "删除后缓存未失效"
        assert not text_path.exists()

    def test_clear_project_texts_clears_cache(self, isolated_env, monkeypatch):
        ctx, _cursor = make_upload_ctx()
        monkeypatch.setattr(files_mod, "get_db", lambda: ctx())
        files_mod._text_cache_set(99904, "缓存")
        files_mod.clear_project_texts(99904)
        assert files_mod._text_cache_get(99904) is None

    def test_re_extract_clears_cache(self, isolated_env, monkeypatch):
        files_mod._text_cache_set(99905, "缓存")
        src = isolated_env / "r.txt"
        src.write_text("原始内容", encoding="utf-8")
        text_path = isolated_env / "r.txt.txt"
        text_path.write_text("旧提取", encoding="utf-8")

        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = {
            "id": 1, "filename": "r.txt", "path": str(src),
            "extracted_text_path": str(text_path),
        }

        class Ctx:
            def __enter__(self):
                return conn

            def __exit__(self, *a):
                return False

        monkeypatch.setattr(files_mod, "get_db", lambda: Ctx())
        result = files_mod.re_extract_text(99905, 1)
        assert result["status"] == "success"
        assert files_mod._text_cache_get(99905) is None, "重提取后缓存未失效"
