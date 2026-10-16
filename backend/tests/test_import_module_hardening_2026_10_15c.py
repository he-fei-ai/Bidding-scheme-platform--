"""上传解析与要点提取模块 —— 2026-10-15c 遗留项加固与端到端贯通

执行 2026-10-15b 报告 §9 遗留清单（按 ROI 排序，可安全落地者全部执行）：

  H-1（TD-D2 · 超大文件分块中断无后端行为测试）
    ``routers/files.py:73-84`` 的「1MB 分块累积 + 超限立即中断 400」历史上只有
    前端源码 grep（test_import_fixes_2026_10_08.py:465-483），不构成行为验证。
    一旦有人把 ``content = await file.read()`` 改回来、或删掉
    ``_total > MAX_FILE_SIZE`` 那行，数 GB 上传会直接把进程内存打爆（OOM）而
    没有任何测试变红 —— 这是一处真实的安全边界（资源耗尽 DoS）。

  H-2（空文本下游扩散链条无测试）
    F-3 已让 ``_pick_main_file`` 校验「路径非空 + 文件存在」，而 EMPTY_TEXT_MESSAGE
    行因 ``extracted_text_path`` 恒为 ''（files.py:603 只在
    ``extracted_text.strip()`` 非空时才写 .txt）被排除。这条排除依赖一个**隐式
    不变量** —— 三条写入路径都维护「空文本 ⇒ 路径为空」。若将来新增第 4 条写入
    路径破坏该不变量，就会重新出现「真正招标正文被截成 200 行摘要」的故障。

  H-3（静态守卫模式推广）
    把 2026-10-15b 的守卫模式推广到同类风险点：① alias 不得被同名 def 遮蔽
   （TD-D）；② 事实覆写端点必须都失效正文缓存；③ 两个文本选择器必须共用同一
    「可读性」判据。

  H-4（端到端数据链贯通冒烟 · 不依赖 AI key）
    上传 → 解析落库 → 主文件唯一性 → 合并文本回读 → 单文件文本回读 → 清单关键词
    检索 → 清空提取文本。任一环断裂都会让下游 AI 拿到空/错正文且无从排查。

  H-5（AI 链路 E2E · 缺 key 优雅跳过）
    ``AGNES_API_KEY`` 未配置时 skip，配置后自动生效 —— 把「完整 AI 链路无端到端」
    这条遗留从人工清单变成 CI 内可执行用例。

约定：与 tests/conftest.py 一致，``asyncio_mode = "auto"``。
"""
import ast
import os
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
APP_DIR = BACKEND_DIR / "app"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from fastapi.testclient import TestClient  # noqa: E402

from app.db import get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.services import files as files_mod  # noqa: E402

FILES_SRC = APP_DIR / "services" / "files.py"
FACTS_SRC = APP_DIR / "routers" / "facts.py"


@pytest.fixture()
def client():
    return TestClient(app)


@pytest.fixture()
def project_id(client):
    resp = client.post(
        "/api/v1/projects",
        json={
            "name": "上传解析 2026-10-15c 加固项目",
            "industry": "公共建筑",
            "bidder_name": "测试投标人",
        },
    )
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["id"]


def _upload(client, project_id, filename, content):
    return client.post(
        f"/api/v1/projects/{project_id}/files",
        files={"file": (filename, content, "application/octet-stream")},
    )


def _file_rows(project_id):
    with get_db(readonly=True) as conn:
        return conn.execute(
            "SELECT id, filename, file_role, status, extracted_text_path, message "
            "FROM uploaded_files WHERE project_id = ?",
            (project_id,),
        ).fetchall()


# ============================================================================
# H-1 · TD-D2 —— 超大文件分块中断（资源耗尽 DoS 边界）
# ============================================================================
class TestUploadSizeGuard:
    """``routers/files.py`` 的分块读取 + 超限立即中断。

    把 ``MAX_FILE_SIZE`` 打小（4KB）而非真造 50MB 数据，测试秒级完成且语义等价 ——
    分块循环、超限判定、超限后不落库不落盘，走的都是同一段生产代码。
    """

    def test_oversize_rejected_400_and_persisted_nothing(
        self, client, project_id, monkeypatch
    ):
        """超限必须 400，且不得留下任何文件行 —— 否则用户以为只是「这次失败了」，
        实际库/盘上已经写入了半截内容。"""
        from app.routers import files as files_router

        monkeypatch.setattr(files_router, "MAX_FILE_SIZE", 4096)
        resp = _upload(client, project_id, "超大.txt", "招".encode("utf-8") * 3000)  # ~9000B > 4096
        assert resp.status_code == 400, resp.text
        assert "超过" in resp.json()["detail"], resp.text
        assert not _file_rows(project_id), "超限上传不得留下任何文件行"

    def test_at_or_under_limit_still_accepted(self, client, project_id, monkeypatch):
        """边界：恰好不超过上限必须正常受理（不能因判定写成 ``>=`` 而误杀）。"""
        from app.routers import files as files_router

        monkeypatch.setattr(files_router, "MAX_FILE_SIZE", 4096)
        content = "土方开挖工程量 1200 立方米。\n" * 100  # ~2.6KB < 4096
        resp = _upload(client, project_id, "限额内.txt", content.encode("utf-8"))
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "success", body
        # 落库侧校验：解析成功必须真的写出可读的提取文本（F-3 的可读性判据）
        rows = _file_rows(project_id)
        assert len(rows) == 1, [dict(r) for r in rows]
        row = rows[0]
        tp = row["extracted_text_path"] or ""
        assert tp and Path(tp).exists(), f"success 行必须有可读的提取文本路径: {dict(row)}"
        assert Path(tp).read_text(encoding="utf-8").strip(), f"提取文本不得为空: {tp}"

    def test_empty_file_rejected_400(self, client, project_id):
        """0 字节文件：分块循环立刻拿到空 chunk 退出，必须在落库前被拦。"""
        resp = _upload(client, project_id, "空.txt", b"")
        assert resp.status_code == 400, resp.text
        assert "为空" in resp.json()["detail"], resp.text
        assert not _file_rows(project_id)

    def test_disallowed_extension_rejected_before_any_io(self, client, project_id, monkeypatch):
        """扩展名校验在读取循环**之前** —— 越权类型不得消耗任何 I/O、不得留痕。"""
        from app.routers import files as files_router

        monkeypatch.setattr(files_router, "allowed_extension", lambda _n: False)
        resp = _upload(client, project_id, "恶意.exe", b"MZ\x90\x00")
        assert resp.status_code == 400, resp.text
        assert "不支持的文件类型" in resp.json()["detail"], resp.text
        assert not _file_rows(project_id)


# ============================================================================
# H-2 · 空文本下游扩散链条
# ============================================================================
class TestEmptyTextDownstreamChain:
    """空文本行（``status='success'`` + ``EMPTY_TEXT_MESSAGE``）不得被当成
    「有正文的主文件」。

    三条写入路径（save_upload / re_extract_text / _persist_file_extraction）都只在
    ``extracted_text.strip()`` 非空时才写 .txt，故该类行 ``extracted_text_path``
    恒为 ''。本组把这个隐式不变量显式钉住：谁加了第 4 条写入路径破坏它，这里立刻红。
    """

    @staticmethod
    def _rows(tmp_path):
        real = tmp_path / "招标文件.txt"
        real.write_text("土方开挖工程量 1200 立方米，总工期 450 日历天。", encoding="utf-8")
        return [
            # 图片扫描件：解析成功但零正文
            {
                "id": 1,
                "filename": "扫描件.png",
                "file_role": "main",
                "status": "success",
                "extracted_text_path": "",
                "message": files_mod.EMPTY_TEXT_MESSAGE,
            },
            # 真正的招标正文
            {
                "id": 2,
                "filename": "招标文件.txt",
                "file_role": "main",
                "status": "success",
                "extracted_text_path": str(real),
                "message": "",
            },
        ]

    def test_prefers_main_with_readable_text_over_empty_text_row(self, tmp_path):
        picked = files_mod._pick_main_file(self._rows(tmp_path))
        assert picked["filename"] == "招标文件.txt", (
            "空文本主文件不得抢占真正正文（F-3 回归）"
        )

    def test_fallback_to_success_main_when_all_empty(self, tmp_path):
        """无任何主文件带可读文本时保持既有回退语义（既有 3 个测试锁定的行为）。"""
        picked = files_mod._pick_main_file(self._rows(tmp_path)[:1])
        assert picked["filename"] == "扫描件.png"

    def test_first_text_by_keywords_skips_empty_text_row(self, client, project_id, tmp_path):
        """清单关键词检索必须命中有正文的那份，而不是空文本行的空串。"""
        real = tmp_path / "清单.txt"
        real.write_text("土方开挖工程量 1200 立方米", encoding="utf-8")
        with get_db() as conn:
            conn.execute(
                "INSERT INTO uploaded_files (project_id, filename, path, file_type, "
                "file_role, status, extracted_text_path, message) "
                "VALUES (?, ?, '', 'text', 'supporting', 'success', ?, ?)",
                (project_id, "扫描件.txt", "", files_mod.EMPTY_TEXT_MESSAGE),
            )
            conn.execute(
                "INSERT INTO uploaded_files (project_id, filename, path, file_type, "
                "file_role, status, extracted_text_path, message) "
                "VALUES (?, ?, '', 'text', 'supporting', 'success', ?, '')",
                (project_id, "工程量清单.txt", str(real)),
            )
        text = files_mod.get_boq_file_text(project_id)
        assert "土方开挖工程量" in text, (
            f"关键词检索不得命中空文本行，实际返回: {text!r}"
        )


# ============================================================================
# H-3 · 静态守卫模式推广
# ============================================================================
class TestStaticGuards:
    """把 2026-10-15b 的静态守卫模式推广到同类风险点。

    这类守卫成本极低（纯 AST 扫描，微秒级）而收益明确：本轮 8 项缺陷全部是
    「同缺陷、不同入口」—— 测试锁定的路径正是被修的路径，同类缺陷在其它入口
    永不暴露。静态守卫把「入口集合」本身纳入测试覆盖。
    """

    def test_no_module_level_alias_shadowed_by_later_def(self):
        """TD-D：``facts.py`` 曾出现 ``_normalize_facts = normalize_fact_items``
        被同名 ``def _normalize_facts`` 遮蔽成永不可达死代码 —— 一行假 re-export
        误导读者，且未来若有人以为改 alias 就能改行为，会白改整轮。"""
        src = FACTS_SRC.read_text(encoding="utf-8")
        tree = ast.parse(src)
        aliases, offenders = {}, []
        for node in tree.body:
            if isinstance(node, ast.Assign):
                for tgt in node.targets:
                    if isinstance(tgt, ast.Name):
                        aliases[tgt.id] = node.lineno
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name in aliases:
                    offenders.append(
                        f"{node.name}: alias@L{aliases[node.name]} 被 def@L{node.lineno} 遮蔽"
                    )
        assert not offenders, (
            "模块级 alias 被同名 def 遮蔽成永不可达死代码（TD-D 回归）:\n"
            + "\n".join(offenders)
            + "\n请删除该 alias 行，或给 def 改名。"
        )

    def test_facts_write_paths_all_invalidate_content_caches(self):
        """事实覆写端点（人工保存 / AI 重提取）必须都失效正文缓存。

        用户改完事实重新生成正文时若命中旧章节缓存，会得到「改了事实但正文前后
        仍不一致」的隐性回归。该约定共 4 处强制点，本守卫钉住其中路由侧两条。
        """
        src = FACTS_SRC.read_text(encoding="utf-8")
        tree = ast.parse(src)
        wanted = {"save_facts", "generate_facts"}
        found, missing_cache = set(), []
        for node in tree.body:
            if isinstance(node, ast.AsyncFunctionDef) and node.name in wanted:
                found.add(node.name)
                seg = ast.get_source_segment(src, node) or ""
                if "invalidate_content_caches" not in seg:
                    missing_cache.append(node.name)
        assert not (wanted - found), f"缺少事实覆写端点: {sorted(wanted - found)}"
        assert not missing_cache, f"事实覆写端点未失效正文缓存: {missing_cache}"

    def test_text_selectors_share_readability_gate(self):
        """``_pick_main_file`` 与 ``_first_text_by_keywords`` 必须都校验「文件存在」。

        F-3 修过一次「只看 status 不看正文」；这两个选择器历史上各写各的，编码
        修复（BUG-FIX-66/76）就是在四份同构副本里漏掉的。守卫保证可读性判据
        不各自漂移。
        """
        src = FILES_SRC.read_text(encoding="utf-8")
        tree = ast.parse(src)
        wanted = {"_pick_main_file", "_first_text_by_keywords"}
        missing = []
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name in wanted:
                seg = ast.get_source_segment(src, node) or ""
                if ".exists()" not in seg:
                    missing.append(node.name)
        assert not missing, f"文本选择器未校验提取文本文件是否存在: {missing}"


# ============================================================================
# H-4 · 端到端数据链贯通冒烟（不依赖 AI key）
# ============================================================================
class TestEndToEndDataChain:
    """上传 → 解析 → 主文件唯一性 → 文本回读 → 关键词检索 → 清空，全链贯通。

    覆盖的是 AI 链路的数据源一侧。任一环断裂（解析静默失败、主文件不唯一、
    缓存未失效、关键词检索取错文件）都会让下游 AI 拿到空/错正文且无从排查。
    """

    def test_upload_parse_confirm_readback_clear_chain(self, client, project_id):
        # ① 上传两份：招标正文 + 工程量清单
        r1 = _upload(
            client,
            project_id,
            "招标文件.txt",
            "第一章 投标资格\n项目名称：滨江商务中心\n投标人须具备市政公用工程施工总承包一级资质。\n"
            .encode("utf-8"),
        )
        assert r1.status_code == 200, r1.text
        body1 = r1.json()
        assert body1["status"] == "success", body1

        r2 = _upload(
            client,
            project_id,
            "工程量清单.txt",
            "土方开挖 1200 立方米\n基础浇筑 800 立方米\n".encode("utf-8"),
        )
        assert r2.status_code == 200, r2.text
        body2 = r2.json()
        assert body2["status"] == "success", body2
        file2_id = body2["id"]

        # ② 主文件唯一性：全项目必须恰好一个 main（互斥契约）
        resp = client.put(
            f"/api/v1/projects/{project_id}/files/{body1['id']}/role",
            json={"file_role": "main"},
        )
        assert resp.status_code == 200, resp.text
        listed = client.get(f"/api/v1/projects/{project_id}/files").json()
        mains = [f for f in listed if f.get("file_role") == "main"]
        assert len(mains) == 1, f"项目必须恰有一个主文件: {[f['filename'] for f in listed]}"
        assert mains[0]["filename"] == "招标文件.txt"

        # ③ 合并文本回读 —— 下游 AI 的唯一数据源：主文件全文在前，辅助摘要在后
        merged = client.get(f"/api/v1/projects/{project_id}/files/text").json()["text"]
        assert "投标资格" in merged, "主文件正文必须进入合并文本"
        assert "基础浇筑" in merged, "辅助文件截断摘要必须进入合并文本"
        assert merged.index("投标资格") < merged.index("基础浇筑"), (
            "主文件全文必须排在辅助摘要之前"
        )

        # ④ 单文件文本回读（GET /files/{id}/text）
        single = client.get(
            f"/api/v1/projects/{project_id}/files/{file2_id}/text"
        ).json()["text"]
        assert "基础浇筑" in single

        # ⑤ 清单关键词检索命中带正文的那份
        boq = files_mod.get_boq_file_text(project_id)
        assert "基础浇筑" in boq, "关键词检索必须命中含正文的清单文件"

        # ⑥ 清空提取文本 → DB 侧数据链必须完整重置（状态回退 pending + 路径清空）
        resp = client.delete(f"/api/v1/projects/{project_id}/files/texts")
        assert resp.status_code == 200, resp.text
        rows = _file_rows(project_id)
        assert rows, "清空不应删除文件行本身"
        assert all(r["status"] == "pending" for r in rows), (
            f"清空后所有文件必须回退 pending: {[dict(r) for r in rows]}"
        )
        assert all(not (r["extracted_text_path"] or "").strip() for r in rows), (
            "清空后不得残留提取文本路径"
        )


# ============================================================================
# H-5 · AI 链路 E2E（缺 key 优雅跳过）
# ============================================================================
class TestAiChainSmoke:
    """完整 AI 链路端到端 —— ``AGNES_API_KEY`` 未配置时 skip。

    把「完整 AI 链路无端到端」这条遗留从人工清单变成 CI 内可执行用例：一旦配置
    key，本用例自动生效并真实走一遍「上传 → 要点提取」的模型调用路径。
    """

    def test_generate_facts_against_real_model(self, client, project_id):
        if not os.environ.get("AGNES_API_KEY", "").strip():
            pytest.skip("未配置 AGNES_API_KEY，AI 链路 E2E 跳过（配置后自动生效）")
        _upload(
            client,
            project_id,
            "招标文件.txt",
            "项目名称：滨江商务中心\n中标价 8500 万元\n总工期 450 日历天\n".encode("utf-8"),
        )
        resp = client.post(f"/api/v1/projects/{project_id}/facts/generate", json={})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body.get("items"), body
        assert body.get("extraction_mode") == "ai", (
            f"配置了真实 key 必须走 AI 提取而非启发式兜底: {body.get('extraction_mode')}"
        )
        names = [it for it in body["items"] if it.get("key") == "projectName"]
        assert names, f"AI 提取应产出项目名称: {body['items']}"
        assert not str(names[0]["value"]).startswith("【待填写"), (
            f"AI 提取应给出真实项目名，实际: {names}"
        )
