"""七模块 HTTP 路由级端到端验收（ENHANCE 2026-09-29 收尾验证）。

与 `test_e2e_2026_09_18_eight_module_chain.py`（服务层契约贯通）互补，本文件从
**前端实际经过的 HTTP 路由**贯穿七模块：
  上传解析 → 提取项目 → 目录生成 → 全局事实 → 正文生成 → 审核预检 → 导出文档
并固化本轮修复的三条跨模块不变量：
  1. 链首路由（files/ai/projects）与后段路由同一属权口径（开发模式 mock 身份全链路放行）；
  2. 目录全量重建后，正文生成入口的 Redis 整包快照立即失效（不再拿旧章节 id）；
  3. 清除分析（DELETE /ai/analysis）后旧事实审计同步作废（GET /facts 不再回看）。

AI 调用全部注入替身：验证的是路由/数据链，不是模型输出。
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from app.db import get_db, init_db


API = "/api/v1"

TENDER_TEXT = (
    "第一章 招标公告\n本项目为市政道路工程，工期：450 天。\n"
    "第二章 评标办法\n评分标准：施工组织设计 30 分，质量保证措施 20 分。\n"
)

SCORING_JSON = [
    {"item": "施工组织设计", "score": 30, "writing_targets": ["施工方案"]},
    {"item": "质量保证措施", "score": 20, "writing_targets": ["质量体系"]},
]

FACTS_FROM_AI = [
    {"key": "duration", "label": "工期", "value": "450 日历天", "category": "schedule"},
]


@pytest.fixture()
def client():
    init_db()
    from app.main import app

    return TestClient(app)


def _mock_ai(monkeypatch):
    """把提取项目环节的三个 AI 出口替换为确定性替身（routers.ai 命名空间）。"""
    import app.routers.ai as ai_mod

    async def _fake_ctx(text):
        return text

    async def _fake_provider(_cfg):
        return MagicMock(), None

    async def _fake_summary(_p, _ctx):
        return json.dumps({"project_name": "市政道路工程", "duration": "450天"}, ensure_ascii=False)

    async def _fake_scoring(_p, _ctx, _summary):
        return SCORING_JSON

    async def _fake_facts(_p, _ctx, _summary, work_interface="", construction_method="", policy="fabricate"):
        return FACTS_FROM_AI

    monkeypatch.setattr(ai_mod, "extract_tender_context_async", _fake_ctx)
    monkeypatch.setattr(ai_mod, "get_working_provider", _fake_provider)
    monkeypatch.setattr(ai_mod, "generate_tender_summary", _fake_summary)
    monkeypatch.setattr(ai_mod, "extract_scoring_points", _fake_scoring)
    monkeypatch.setattr(ai_mod, "extract_global_facts", _fake_facts)


class TestSevenModuleHttpChain:
    def test_full_chain_and_invariants(self, client, monkeypatch):
        _mock_ai(monkeypatch)

        # ① 提取项目：创建项目（链首属权收口后，mock 身份创建→后续全链路可用）
        r = client.post(f"{API}/projects", json={"name": "七模块贯通", "industry": "市政工程"})
        assert r.status_code == 201, r.text
        pid = r.json()["id"]

        # ② 上传解析：HTTP 上传 → 解析任务 completed → 步骤推进到 2
        r = client.post(
            f"{API}/projects/{pid}/files",
            files={"file": ("招标文件.txt", TENDER_TEXT.encode("utf-8"), "text/plain")},
            data={"file_role": "main"},
        )
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "success"
        r = client.get(f"{API}/projects/{pid}/files")
        assert r.status_code == 200 and len(r.json()) >= 1
        r = client.get(f"{API}/projects/{pid}/files/text")
        assert "450" in r.json()["text"], "合并文本应含招标文件内容"
        assert client.get(f"{API}/projects/{pid}").json()["current_step"] == 2

        # ③ 提取项目（AI 分析）：摘要 → 评分（自动带出全局事实）→ 双确认 → 步骤 3
        r = client.post(f"{API}/projects/{pid}/ai/summary")
        assert r.status_code == 200 and r.json()["summary_json"]
        r = client.post(f"{API}/projects/{pid}/ai/scoring")
        assert r.status_code == 200, r.text
        assert len(json.loads(r.json()["scoring_points_json"])) == 2
        assert r.json()["global_facts"] == FACTS_FROM_AI
        summary_json = r.json()["summary_json"]
        scoring_json = r.json()["scoring_points_json"]
        assert client.post(f"{API}/projects/{pid}/ai/summary/confirm",
                           json={"summary_json": summary_json}).status_code == 200
        r = client.post(f"{API}/projects/{pid}/ai/scoring/confirm",
                        json={"scoring_points_json": scoring_json})
        assert r.status_code == 200
        assert client.get(f"{API}/projects/{pid}").json()["current_step"] == 3

        # ④ 全局事实：评分链路自动提取的事实经 GET /facts 同口径读出 → 人工修订 → 确认
        r = client.get(f"{API}/projects/{pid}/facts")
        assert r.status_code == 200
        assert [f["key"] for f in r.json()["items"]] == ["duration"]
        edited = [{"key": "duration", "label": "工期", "value": "460 日历天", "category": "schedule"}]
        assert client.post(f"{API}/projects/{pid}/facts", json={"items": edited}).status_code == 200
        r = client.get(f"{API}/projects/{pid}/facts")
        assert r.json()["items"][0]["value"] == "460 日历天"
        assert client.post(f"{API}/projects/{pid}/facts/confirm").status_code == 200

        # ⑤ 目录生成：走目录收尾同一路径（DELETE+重建），并验证缓存断点修复
        from app.routers.sse.sse_db_ops import _clear_and_save_outline
        from app.services.redis_cache import CacheService

        CacheService.set_project_sections(pid, [{"id": -999999, "title": "旧快照"}])
        outline = [
            {"title": "施工组织设计", "children": [{"title": "施工部署", "children": []}]},
            {"title": "质量保证措施", "children": []},
        ]
        saved = _clear_and_save_outline(pid, outline)
        assert saved and saved[0]["id"] > 0
        cached = CacheService.get_project_sections(pid)
        assert not (cached and any(isinstance(i, dict) and i.get("id") == -999999 for i in cached)), \
            "目录重建后正文生成快照必须失效（本轮跨模块缓存断点修复）"
        r = client.get(f"{API}/projects/{pid}/sections")
        assert r.status_code == 200
        tree = r.json()
        assert {n["title"] for n in tree} == {"施工组织设计", "质量保证措施"}

        # ⑥ 正文生成：按生成收尾路径逐章落库（含 word_count 统一口径），步骤推进 5
        from app.routers.sse.sse_db_ops import _save_section_to_db
        from app.services.projects import update_step

        with get_db() as conn:
            leaf_ids = [row["id"] for row in conn.execute(
                "SELECT id FROM sections WHERE project_id = ? ORDER BY id", (pid,)).fetchall()]
        body = "本工程工期 460 日历天，执行三检制：自检、互检、专检。" * 3
        for sid in leaf_ids:
            _save_section_to_db(sid, "completed", body, project_id=pid)
        update_step(pid, 5)
        r = client.get(f"{API}/word-count/overview/{pid}")
        assert r.status_code == 200 and r.json()["total_words"] > 0
        assert r.json()["empty_sections"] == 0

        # ⑦ 审核预检 + 导出文档：/export/check 预检 → /export/docx 出件
        r = client.get(f"{API}/projects/{pid}/export/check")
        assert r.status_code == 200
        check = r.json()
        assert check["total_sections"] == len(leaf_ids)
        # POST /docx 按既有契约返回 JSON 元数据（filename/download_url/export_id），
        # 二进制经 GET /export/download/{export_id} 获取 —— 与前端 useExport 流程一致，
        # 端到端验收按真实协议两段式断言。
        r = client.post(f"{API}/projects/{pid}/export/docx", json={})
        assert r.status_code == 200, r.text
        meta = r.json()
        assert meta["filename"].endswith(".docx") and meta["file_size"] > 0
        assert meta["download_url"] and meta["export_id"]
        r = client.get(f"{API}/projects/{pid}/export/download/{meta['export_id']}")
        assert r.status_code == 200, r.text
        assert r.content[:2] == b"PK", "下载应返回合法 DOCX（zip 魔数）"
        assert client.get(f"{API}/projects/{pid}").json()["current_step"] == 6, "导出后步骤推进到 6"

        # 不变量 3：清除分析 → 旧事实审计同步作废（跨模块：提取项目→全局事实）
        with get_db() as conn:
            conn.execute(
                "UPDATE analysis SET facts_audit_json = '{\"status\":\"ok\"}', "
                "facts_audit_at = datetime('now','localtime') WHERE project_id = ?", (pid,))
        r = client.delete(f"{API}/projects/{pid}/ai/analysis")
        assert r.status_code == 200
        r = client.get(f"{API}/projects/{pid}/facts")
        assert r.status_code == 200
        assert r.json()["last_audit"] is None, "清除分析后旧审计不得残留"

    def test_chain_first_routers_reject_stranger(self, client, monkeypatch):
        """链首属权收口：他人项目经 HTTP 一律 403（与后段模块同口径）。"""
        from app.main import app
        from app.security.auth import User, get_current_user

        with get_db() as conn:
            cur = conn.execute(
                "INSERT INTO projects (name, industry, created_by) VALUES ('陌生人项目', '市政', 12345)")
            r_pid = cur.lastrowid

        # FastAPI 依赖在导入期即被捕获，只能走 dependency_overrides 换身份
        app.dependency_overrides[get_current_user] = lambda: User(999, "foreign", "user")
        try:
            assert client.get(f"{API}/projects/{r_pid}").status_code == 403
            assert client.get(f"{API}/projects/{r_pid}/files").status_code == 403
            assert client.get(f"{API}/projects/{r_pid}/ai/analysis").status_code == 403
            assert client.post(f"{API}/projects/{r_pid}/ai/summary").status_code == 403
            assert client.get(f"{API}/projects/{r_pid}/facts").status_code == 403
        finally:
            app.dependency_overrides.clear()
