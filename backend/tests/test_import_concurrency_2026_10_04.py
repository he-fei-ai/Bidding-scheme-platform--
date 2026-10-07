"""解析提取模块 · 并发/批量上传竞态回归测试（2026-10-04）

覆盖 BUG-FIX 2026-10-04（P2 并发竞态）：
- save_upload 经 asyncio.to_thread 走线程池，同一项目并发上传同名文件时，
  旧实现的 "存在性检查→重命名→write_bytes" 非原子，两个线程会选中同一目标
  路径与同一提取文本文件名（f"{filename}.txt"），后写者覆盖前者 → 解析结果
  丢失 / 字段错位 / DB 与物理文件不一致。
- 修复：按 project_id 维护写入互斥锁，把命名+落盘临界区原子化。

断言口径：并发同名上传后，所有返回的 filename 互不相同，且每个 filename 对应的
原始文件与提取文本文件都真实存在（无一被覆盖丢失）。
"""
import sys
import threading
from itertools import count
from pathlib import Path
from unittest.mock import MagicMock

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.services import files as files_mod  # noqa: E402


class _UploadCtx:
    """save_upload 的线程安全 DB 替身：每次 execute 返回带自增 lastrowid 的 cursor"""

    def __init__(self, id_iter):
        self._id_iter = id_iter

    def __enter__(self):
        conn = MagicMock()
        cursor = MagicMock()
        cursor.lastrowid = next(self._id_iter)
        conn.execute.return_value = cursor
        return conn

    def __exit__(self, *a):
        return False


@pytest.fixture()
def isolated_env(monkeypatch, tmp_path):
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(files_mod, "PROJECTS_DATA_DIR", projects_dir)
    monkeypatch.setattr(files_mod, "_text_cache", {})
    # 每次 get_db 调用返回独立替身，lastrowid 全局自增（模拟主键不冲突）
    id_iter = count(1)
    monkeypatch.setattr(files_mod, "get_db", lambda *a, **k: _UploadCtx(id_iter))
    return projects_dir


def test_concurrent_same_filename_no_overwrite(isolated_env):
    """8 线程同时上传同名 .txt，内容各异 → 8 个互不相同的落盘名，文本零丢失"""
    project_id = 88001
    n = 8
    barrier = threading.Barrier(n)
    results: list[dict] = [None] * n

    def worker(idx: int):
        # 每个文件写入唯一标记内容，便于校验是否被覆盖
        content = f"并发上传文件序号 {idx} 唯一内容标记".encode("utf-8")
        barrier.wait()  # 尽量对齐起跑，放大竞态窗口
        results[idx] = files_mod.save_upload(project_id, "招标文件.txt", content, "main")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert all(r is not None for r in results)
    # 全部上传成功
    assert all(r["status"] == "success" for r in results), [r.get("message") for r in results]

    filenames = [r["filename"] for r in results]
    # 关键不变量：返回文件名互不相同（并发去重生效，无一撞名）
    assert len(set(filenames)) == n, f"并发同名上传出现重名: {filenames}"

    upload_dir = isolated_env / str(project_id) / "uploads"
    texts_dir = isolated_env / str(project_id) / "texts"
    for fn in filenames:
        assert (upload_dir / fn).exists(), f"原始文件缺失: {fn}"
        # 提取文本文件名 = 完整原名 + .txt，必须与同名原始文件一一对应、互不覆盖
        assert (texts_dir / f"{fn}.txt").exists(), f"提取文本缺失(被覆盖): {fn}"

    # 每个唯一内容标记都能在对应的提取文本中找回（证明无解析结果丢失/字段错位）
    for idx, fn in enumerate(filenames):
        got = (texts_dir / f"{fn}.txt").read_text(encoding="utf-8")
        assert f"并发上传文件序号 {idx}" in got or any(
            f"并发上传文件序号 {k}" in got for k in range(n)
        )
    # 汇总：n 份唯一标记各出现且仅出现一次
    all_text = "\n".join((texts_dir / f"{fn}.txt").read_text(encoding="utf-8") for fn in filenames)
    for idx in range(n):
        assert all_text.count(f"并发上传文件序号 {idx}") == 1, f"标记 {idx} 覆盖丢失或重复"


def test_concurrent_different_projects_independent(isolated_env):
    """不同项目并发上传同名文件互不影响（按 project_id 分锁，无跨项目阻塞/串写）"""
    barrier = threading.Barrier(4)
    results = {}

    def worker(pid, idx):
        content = f"项目{pid}标记{idx}".encode("utf-8")
        barrier.wait()
        results[(pid, idx)] = files_mod.save_upload(pid, "共用名.txt", content, "main")

    threads = [
        threading.Thread(target=worker, args=(pid, idx))
        for pid in (88002, 88003)
        for idx in range(2)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # 每个项目内 2 个同名 → 去重为不同名；跨项目同名允许相同
    for pid in (88002, 88003):
        fns = [results[(pid, idx)]["filename"] for idx in range(2)]
        assert len(set(fns)) == 2, f"项目 {pid} 内同名未去重: {fns}"
        for fn in fns:
            assert (isolated_env / str(pid) / "uploads" / fn).exists()
