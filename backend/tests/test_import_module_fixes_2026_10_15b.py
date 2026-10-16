"""上传解析与要点提取模块 —— 2026-10-15b 专项回归（F-1 … F-5）

本轮在既有 6 轮审计（HEAD a2ef390）基础上做**独立复核**，定位到的都是
「同缺陷、不同入口」的历史漏网之鱼 —— 同一类失败模式在某条写入路径上修过，
另一条没有修，于是以不同面貌复现。

  F-1（save_upload 写盘失败 → 假 success）
    `re_extract_text` 在 2026-09-18 就已对「.txt 落盘失败」如实返回 failed，
    但 `save_upload` 的**同一分支**只 `logger.error` 就继续往下走 —— 而
    status/message 早在 extract 分支被置成 'success'/''，随后 INSERT 把
    `status='success' + extracted_text_path='' + message=''` 一起入库，任务行
    也已被写成 completed。用户看到「解析成功」却没有任何正文。

  F-2（_persist_file_extraction 同型漏网 · Celery 异步主链路）
    与 F-1 完全同型，但这是**异步解析主链路**（后台「重新解析」、图片批量
    上传都走它），影响面更大。

  F-3（_pick_main_file 不看 extracted_text_path）
    `_truncate_supporting_file` 会校验「路径非空 + 文件存在」，本函数只看
    status。主文件行 `status='success'` 但 .txt 缺失时（历史脏行 / 外部清理 /
    clear 与解析竞态），真正文被推到 supporting 通道截断成 200 行「摘要」。

  F-4（delete_file 漏清解析结果缓存）
    `_clear_parse_cache` 在另三条写入路径都已调用，删除是漏网之鱼。删除后
    重传同名文件若 (size, mtime) 哈希相同，会命中旧缓存把**已删除文件**的
    正文当成新文件正文写回。

  F-5（启发式兜底绕过缺失值策略）
    `_generate_default_facts` 无事实时硬编码 `projectName="待确认"` 与
    `projectType="建筑工程"`。`is_placeholder_value` 只认「【待填写」前缀，
    所以「待确认」**不被识别为占位**：不计入 placeholder_count、不打
    needs_manual_input、不被正文生成的占位过滤剔除 —— 会被当成真实项目名
    注入全篇提示词；「建筑工程」是未经佐证的断言，omit/placeholder 策略下
    仍拿得到，策略选择形同虚设。

约定：与 tests/conftest.py 一致，``asyncio_mode = "auto"``。
"""
import re
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from fastapi.testclient import TestClient  # noqa: E402

from app.db import get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.routers.facts import _generate_default_facts  # noqa: E402
from app.services import files as files_mod  # noqa: E402
from app.services.facts_service import (  # noqa: E402
    count_placeholders,
    is_placeholder_value,
)


@pytest.fixture()
def client():
    return TestClient(app)


@pytest.fixture()
def project_id(client):
    resp = client.post("/api/v1/projects", json={
        "name": "上传解析 2026-10-15b 回归项目",
        "industry": "公共建筑",
        "bidder_name": "测试投标人",
    })
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["id"]


def _upload(client, project_id, filename, content):
    resp = client.post(
        f"/api/v1/projects/{project_id}/files",
        files={"file": (filename, content, "application/octet-stream")},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _task_row(project_id: int, filename: str) -> dict:
    """该项目指定文件名的最新一条解析任务行"""
    with get_db(readonly=True) as conn:
        row = conn.execute(
            "SELECT status, progress, completed_files, error_message, filename "
            "FROM file_parse_tasks WHERE project_id = ? AND filename = ? "
            "ORDER BY id DESC LIMIT 1",
            (project_id, filename),
        ).fetchone()
    return dict(row) if row else {}


def _file_row(file_id: int) -> dict:
    with get_db(readonly=True) as conn:
        row = conn.execute(
            "SELECT status, message, extracted_text_path FROM uploaded_files WHERE id = ?",
            (file_id,),
        ).fetchone()
    return dict(row) if row else {}


def _read_source() -> str:
    with open(files_mod.__file__, encoding="utf-8") as f:
        return f.read()


def _func_src(name: str) -> str:
    """抽出指定函数的完整源码块（用于静态守卫）"""
    m = re.search(rf"^def {re.escape(name)}\(.*?(?=^def |\Z)", _read_source(), re.S | re.M)
    assert m, f"未找到函数 {name}"
    return m.group(0)


def _func_sql(name: str) -> str:
    """抽出指定函数源码中的 UPDATE SQL 字面量（精确到 SQL 块，避免文档误报）

    必须锚定表名：docstring 里会出现「UPDATE 0 行」这类解释性文字，
    只匹配裸 UPDATE 关键字会误命中文档。
    """
    src = _func_src(name)
    m = re.search(r"UPDATE\s+file_parse_tasks.*?(?=\"\"\")", src, re.S)
    assert m, f"{name} 中未找到 UPDATE file_parse_tasks SQL"
    return m.group(0)


def _raise_on_txt_write(monkeypatch, error: OSError):
    """让 ``Path.write_text`` 仅在目标为 .txt 时抛错（原始文件走 write_bytes，不受影响）"""
    real = files_mod.Path.write_text

    def raiser(self, data, *args, **kwargs):
        if str(self).endswith(".txt"):
            raise error
        return real(self, data, *args, **kwargs)

    monkeypatch.setattr(files_mod.Path, "write_text", raiser)


# ============================================================================
# F-1 / F-2：写盘失败必须如实降级为 failed（三条写入路径同口径）
# ============================================================================


def test_write_text_failure_marks_upload_failed(client, project_id, monkeypatch):
    """F-1：save_upload 提取成功但 .txt 落盘失败 → 不得谎报 success"""
    _raise_on_txt_write(monkeypatch, OSError("disk full"))

    result = _upload(client, project_id, "含正文.txt", "招标正文若干".encode("utf-8"))

    assert result["status"] == "failed", f"写盘失败却报成功: {result}"
    assert "写入提取文本失败" in result["message"]

    row = _file_row(result["id"])
    assert row["status"] == "failed"
    assert "写入提取文本失败" in row["message"]
    # 未落盘 → 路径必须为空串（不能留下悬空路径）
    assert row["extracted_text_path"] == ""

    task = _task_row(project_id, result["filename"])
    assert task, "任务行缺失"
    # 关键：任务行先被写成 completed，再被强制覆盖回 failed（UPDATE 0 行则失败）
    assert task["status"] == "failed", f"任务行未被覆盖: {task}"
    assert task["error_message"]


def test_write_text_failure_marks_persist_failed(client, project_id, monkeypatch, tmp_path):
    """F-2：_persist_file_extraction（Celery 异步主链路）同口径"""
    _raise_on_txt_write(monkeypatch, OSError("permission denied"))

    raw = tmp_path / "含正文.txt"
    raw.write_bytes("异步链路正文".encode("utf-8"))
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO uploaded_files (project_id, filename, path, file_type, file_role, status) "
            "VALUES (?, ?, ?, 'txt', 'supporting', 'processing')",
            (project_id, "含正文.txt", str(raw)),
        )
        file_id = cur.lastrowid

    status = files_mod._persist_file_extraction(
        project_id, file_id, str(raw), "含正文.txt", ".txt", create_task_row=False
    )

    assert status == "failed", f"写盘失败却返回成功: {status}"
    row = _file_row(file_id)
    assert row["status"] == "failed"
    assert "写入提取文本失败" in row["message"]
    assert row["extracted_text_path"] == ""


def test_force_parse_task_failed_overwrites_completed_row(project_id):
    """F-1/F-2 的根因：_update_parse_task 的 WHERE 带 status='processing'，
    无法覆盖已写成 completed 的行（UPDATE 0 行）。本 helper 专为该场景设计。"""
    files_mod._create_parse_task(project_id, "覆盖.txt", ".txt")
    files_mod._update_parse_task(project_id, "completed", 100, error_message="", filename="覆盖.txt")

    before = _task_row(project_id, "覆盖.txt")
    assert before["status"] == "completed"
    # 对照：老接口无法覆盖（UPDATE 0 行，静默失效）
    files_mod._update_parse_task(project_id, "failed", 0, "老接口", filename="覆盖.txt")
    assert _task_row(project_id, "覆盖.txt")["status"] == "completed", "对照应仍是 completed"

    files_mod._force_parse_task_failed(project_id, "覆盖.txt", "强制覆盖")
    after = _task_row(project_id, "覆盖.txt")
    assert after["status"] == "failed"
    assert after["error_message"] == "强制覆盖"


def test_write_success_path_is_unchanged(client, project_id):
    """反向对照：写盘正常时行为与修复前一致（不得把正常态改坏）"""
    result = _upload(client, project_id, "正常.txt", "正常正文".encode("utf-8"))
    assert result["status"] == "success", result
    assert result["message"] == ""
    row = _file_row(result["id"])
    assert row["status"] == "success"
    assert row["extracted_text_path"] and Path(row["extracted_text_path"]).exists()
    assert _task_row(project_id, result["filename"])["status"] == "completed"


# ============================================================================
# F-3：主文件选择必须与 supporting 的「可读性」判定同口径
# ============================================================================


def test_pick_main_file_prefers_file_with_readable_text(tmp_path):
    """两个 success 主文件：文本缺失者不得压过文本可用者（否则真正文被截成 200 行摘要）"""
    real = tmp_path / "a.txt"
    real.write_text("真实正文", encoding="utf-8")
    dangling = tmp_path / "missing.txt"  # 故意不创建
    files = [
        {"id": 1, "filename": "a.txt", "file_role": "main", "status": "success",
         "extracted_text_path": str(real)},
        {"id": 2, "filename": "b.txt", "file_role": "main", "status": "success",
         "extracted_text_path": str(dangling)},
    ]
    # 把缺文本的放前面，验证不是「取第一个」而是「取有文本的」
    assert files_mod._pick_main_file(files)["id"] == 1


def test_pick_main_file_fallback_when_no_main_has_text():
    """没有任何主文件带有效文本时，保持原有回退语义（既有测试锁定的行为不变）"""
    files = [
        {"id": 7, "filename": "a.txt", "file_role": "main", "status": "success",
         "extracted_text_path": ""},
    ]
    assert files_mod._pick_main_file(files)["id"] == 7


def test_pick_main_file_backward_compat_missing_keys():
    """缺 status / extracted_text_path 键的入参（旧调用方、既有夹具）不得抛错"""
    files = [{"id": 1, "filename": "招标文件.pdf", "file_role": "main"}]
    assert files_mod._pick_main_file(files)["id"] == 1
    assert files_mod._pick_main_file([]) is None


# ============================================================================
# F-4：删除路径必须清理解析结果缓存
# ============================================================================


def test_delete_file_clears_parse_cache(client, project_id, monkeypatch):
    """删除后重传同名文件不得命中旧解析结果缓存"""
    result = _upload(client, project_id, "会重传.txt", "第一轮正文".encode("utf-8"))
    file_id = result["id"]

    calls: list[str] = []
    monkeypatch.setattr(files_mod, "_clear_parse_cache", lambda p: calls.append(str(p)))

    with get_db(readonly=True) as conn:
        path_row = conn.execute(
            "SELECT path FROM uploaded_files WHERE id = ?", (file_id,)
        ).fetchone()
    stored_path = path_row["path"]

    resp = client.delete(f"/api/v1/projects/{project_id}/files/{file_id}")
    assert resp.status_code == 200, resp.text

    assert calls, "delete_file 未调用 _clear_parse_cache"
    assert str(stored_path) in calls, f"未清理解析文件本身的缓存: {calls}"


# ============================================================================
# F-5：启发式兜底必须落在占位契约内、并遵守缺失值策略
# ============================================================================


def test_default_facts_placeholder_policy_marks_project_name():
    """placeholder 策略：兜底项必须被 is_placeholder_value 识别"""
    items = _generate_default_facts("", "", policy="placeholder")
    assert items, "必须产出兜底项"
    for it in items:
        assert is_placeholder_value(it["value"]), (
            f"{it['key']} 的兜底值 {it['value']!r} 未被识别为占位 —— 会被注入正文提示词"
        )
    assert count_placeholders(items) == len(items)


def test_default_facts_omit_policy_emits_no_fabricated_type():
    """omit（不编造）策略：不得产出未经资料佐证的工程类型断言"""
    items = _generate_default_facts("", "", policy="omit")
    by_key = {i["key"]: i for i in items}
    assert "projectType" not in by_key, "omit 策略下不应编造工程类型"
    assert "projectName" in by_key
    assert is_placeholder_value(by_key["projectName"]["value"])


def test_default_facts_fabricate_policy_backward_compat():
    """fabricate（默认）策略：projectType 保持历史默认值，历史部署行为不变"""
    items = _generate_default_facts("", "", policy="fabricate")
    by_key = {i["key"]: i for i in items}
    assert by_key["projectType"]["value"] == "建筑工程"
    # 但 projectName 一律是占位：假项目名注入全篇比留占位更危险
    assert is_placeholder_value(by_key["projectName"]["value"])


def test_default_facts_default_arg_is_fabricate():
    """未传 policy 时默认 fabricate（向后兼容既有调用方）"""
    items = _generate_default_facts("", "")
    by_key = {i["key"]: i for i in items}
    assert by_key["projectType"]["value"] == "建筑工程"


def test_default_facts_heuristic_branch_ignores_policy():
    """正则确实抽到事实时不受策略影响（策略只约束「无事实时的兜底」）"""
    text = "项目名称：某某市政道路工程\n工期要求：420 日历天"
    items = _generate_default_facts(text, "", policy="omit")
    by_key = {i["key"]: i for i in items}
    assert by_key["projectName"]["value"] == "某某市政道路工程"
    assert by_key["scheduleDuration"]["value"] == "420 日历天"
    assert all(not is_placeholder_value(i["value"]) for i in items)


# ============================================================================
# 静态守卫：防止同一缺陷再次以「不同入口」的形式回退
# ============================================================================


def test_all_write_paths_treat_text_write_failure_as_failed():
    """三条写入路径都必须把「.txt 落盘失败」判为 failed。

    F-1/F-2 的根治守卫：历史上该缺陷在 re_extract_text 修过、在 save_upload
    与 _persist_file_extraction 漏修。静态断言确保三处永远同口径。
    """
    for name in ("save_upload", "_persist_file_extraction", "re_extract_text"):
        src = _func_src(name)
        assert "写入提取文本失败" in src, (
            f"{name} 的 .txt 写盘失败分支没有如实返回 failed（F-1/F-2 回归）"
        )


def test_delete_file_covers_both_caches():
    """delete_file 必须同时清「合并文本缓存」与「解析结果缓存」"""
    src = _func_src("delete_file")
    assert "_text_cache_clear" in src, "漏清合并文本缓存"
    assert "_clear_parse_cache" in src, "漏清解析结果缓存（F-4 回归）"


def test_force_parse_task_failed_has_no_processing_filter():
    """helper 必须能覆盖终态行；若有人给它加回 status='processing' 过滤，
    F-1/F-2 会立即复活（UPDATE 0 行，静默失效）。

    注意：只检查 SQL 字面量本身，不检查函数源码整体 —— docstring 里会解释性地
    引用 `status = 'processing'` 这个过滤条件，直接子串匹配会误报。
    """
    sql = _func_sql("_force_parse_task_failed")
    assert "status = 'processing'" not in sql, "加了 processing 过滤就无法覆盖已写终态"
    assert "status = 'failed'" in sql, "必须写入 failed 终态"
    assert "filename = ?" in sql, "必须按 filename 精确过滤，避免串写同项目其它文件"

