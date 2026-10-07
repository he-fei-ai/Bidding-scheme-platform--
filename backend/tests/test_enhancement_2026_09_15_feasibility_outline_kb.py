# -*- coding: utf-8 -*-
"""可研报告「大纲树形编辑器 + 知识库选材」回归测试（ENHANCE 2026-09-15）。

覆盖两块第九轮新增能力：
A. 目录树可编辑：净化/编号重映射搬运正文/失效语义/运行中任务锁；
B. 参考知识库选材：文档级选择 + 章节级相关度匹配 + 精确覆盖 + 素材拼接。

只测确定性逻辑与 HTTP 契约，不调 AI。
"""

import pytest

from app.db import get_db
from app.services import feasibility as store
from app.services import feasibility_knowledge as kb
from app.services import knowledge as knowledge_store

_TEST_DOC_PREFIX = "可研选材测试-"


@pytest.fixture(autouse=True)
def _isolate():
    store.clear_workspace()
    _cleanup_knowledge()
    yield
    store.clear_workspace()
    _cleanup_knowledge()


def _cleanup_knowledge():
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id FROM knowledge_documents WHERE filename LIKE ?",
            (f"{_TEST_DOC_PREFIX}%",),
        ).fetchall()
        ids = [r["id"] for r in rows]
        if not ids:
            return
        marks = ",".join("?" for _ in ids)
        conn.execute(f"DELETE FROM knowledge_items WHERE source_doc_id IN ({marks})", ids)
        conn.execute(f"DELETE FROM knowledge_documents WHERE id IN ({marks})", ids)


def _seed_document(filename: str, items: list[dict]) -> int:
    doc_id = knowledge_store.create_document(
        filename=f"{_TEST_DOC_PREFIX}{filename}",
        file_path="",
        file_type="docx",
        industry="新能源",
    )
    with get_db() as conn:
        for item in items:
            conn.execute(
                "INSERT INTO knowledge_items "
                "(title, summary, content, keywords, industry, tags, source_doc_id, source_ref) "
                "VALUES (?, ?, ?, ?, ?, '[]', ?, '')",
                (
                    item["title"],
                    item.get("summary", ""),
                    item.get("content", ""),
                    item.get("keywords", "[]"),
                    "新能源",
                    doc_id,
                ),
            )
    return doc_id


def _seed_outline(with_content: bool = True) -> None:
    store.save_project_info({"projectName": "选材测试项目"})
    outline = store.assign_outline_ids(
        store.normalize_outline_tree(
            [
                {"title": "概述", "description": "项目概况"},
                {
                    "title": "建设方案",
                    "description": "工艺与设备",
                    "children": [{"title": "工艺路线", "description": "技术路线"}],
                },
            ]
        )
    )
    store.update_report_state({"outlineData": {"outline": outline}})
    if with_content:
        store.write_chapter_content("1", "概述正文")
        store.write_chapter_content("2.1", "工艺路线正文")


def _leaf_contents() -> dict[str, str]:
    data = store.load_report()["outlineData"]
    return {leaf["id"]: leaf.get("content") or "" for leaf in store.collect_leaves(data["outline"])}


# ======================= A. 目录树保存（树形编辑器） =======================


class TestOutlineSanitize:
    def test_caps_levels_and_drops_empty(self):
        l4 = {"title": "四级", "description": "d"}
        l3 = {"title": "三级", "description": "d", "children": [l4]}
        l2 = {"title": "二级", "description": "d", "children": [l3]}
        raw = [{"title": "一级", "description": "d", "children": [l2]}, {"title": "", "description": ""}]
        cleaned = store.sanitize_outline_for_save(raw)
        assert len(cleaned) == 1
        third = cleaned[0]["children"][0]["children"][0]
        assert third["title"] == "三级"
        assert "children" not in third  # 第四层被裁掉

    def test_collect_leaves_includes_nested_levels(self):
        tree = store.assign_outline_ids(
            store.normalize_outline_tree(
                [
                    {"title": "总体说明", "description": "d"},
                    {"title": "建设方案", "description": "d", "children": [{"title": "工艺路线", "description": "d"}]},
                ]
            )
        )
        leaves = store.collect_leaves(tree)
        # BUG-FIX 2026-09-15 回归：嵌套层级的叶子此前被整体丢弃
        assert [leaf["id"] for leaf in leaves] == ["1", "2.1"]
        assert leaves[1]["trail"] == ["建设方案", "工艺路线"]

    def test_keeps_human_title_verbatim(self):
        cleaned = store.sanitize_outline_for_save([{"title": "1.1 投资估算", "description": ""}])
        # 人工标题不做剥编号（与 AI 输出清洗不同），前端已负责显示编号
        assert cleaned[0]["title"] == "1.1 投资估算"

    def test_carry_content_and_knowledge_ids_survive_renumber(self):
        tree = store.assign_outline_ids(
            store.sanitize_outline_for_save(
                [{"title": "概述", "description": "d", "content": "正文X", "knowledgeItemIds": ["3", 3, "bad"]}]
            )
        )
        assert tree[0]["content"] == "正文X"
        assert tree[0]["knowledge_item_ids"] == [3]


class TestOutlineSaveContentCarry:
    def test_delete_first_root_moves_content_to_new_ids(self):
        _seed_outline()
        # 前端删除「1 概述」后重新编号：旧 2 → 新 1、旧 2.1 → 新 1.1
        store.save_outline(
            {
                "outline": [
                    {
                        "id": "1",
                        "title": "建设方案",
                        "description": "工艺与设备",
                        "children": [{"id": "1.1", "title": "工艺路线", "description": "技术路线"}],
                    }
                ]
            },
            reason="delete",
            id_map={"2": "1", "2.1": "1.1"},
        )
        assert _leaf_contents() == {"1.1": "工艺路线正文"}

    def test_affected_node_content_cleared(self):
        _seed_outline()
        store.save_outline(
            {
                "outline": [
                    {"id": "1", "title": "概述", "description": "改后的要点"},
                    {
                        "id": "2",
                        "title": "建设方案",
                        "description": "工艺与设备",
                        "children": [{"id": "2.1", "title": "工艺路线", "description": "技术路线"}],
                    },
                ]
            },
            reason="edit",
            affected_node_ids=["1"],
        )
        assert _leaf_contents() == {"1": "", "2.1": "工艺路线正文"}

    def test_missing_content_in_payload_restored_from_snapshot(self):
        _seed_outline()
        # 客户端只提交标题（未回传正文）时也必须保住已生成正文
        store.save_outline(
            {
                "outline": [
                    {"id": "1", "title": "概述（改名）", "description": "项目概况"},
                    {
                        "id": "2",
                        "title": "建设方案",
                        "description": "工艺与设备",
                        "children": [{"id": "2.1", "title": "工艺路线", "description": "技术路线"}],
                    },
                ]
            },
            reason="edit",
        )
        assert _leaf_contents() == {"1": "概述正文", "2.1": "工艺路线正文"}

    def test_replace_reason_clears_all_content(self):
        _seed_outline()
        store.save_outline({"outline": [{"title": "新目录", "description": "d"}]}, reason="replace")
        assert _leaf_contents() == {"1": ""}

    def test_sort_keeps_content_and_parameters(self):
        _seed_outline()
        # 直接写 meta（save_key_parameters 会按语义清空正文，这里只是准备前置条件）
        store.update_report_state({"keyParametersMarkdown": "## 关键参数"})
        store.save_outline(
            {
                "outline": [
                    {
                        "id": "1",
                        "title": "建设方案",
                        "description": "工艺与设备",
                        "children": [{"id": "1.1", "title": "工艺路线", "description": "技术路线"}],
                    },
                    {"id": "2", "title": "概述", "description": "项目概况"},
                ]
            },
            reason="sort",
            id_map={"2": "1", "2.1": "1.1", "1": "2"},
        )
        report = store.load_report()
        # 排序不失效下游：参数保留，且正文随节点整体搬迁
        assert "关键参数" in report["keyParametersMarkdown"]
        assert _leaf_contents() == {"1.1": "工艺路线正文", "2": "概述正文"}


class TestTaskLockOnEdit:
    def test_save_outline_rejected_while_task_running(self):
        _seed_outline()
        store.save_task_state("feasibility-outline", {"task_id": "t", "status": "running", "progress": 30})
        with pytest.raises(store.TaskRunningError, match="正在运行"):
            store.save_outline({"outline": [{"title": "x", "description": ""}]}, reason="edit")

    def test_save_chapter_rejected_while_task_running(self):
        _seed_outline()
        store.save_task_state("feasibility-content", {"task_id": "t", "status": "running", "progress": 10})
        with pytest.raises(store.TaskRunningError):
            store.save_chapter_content("1", "人工改写")
        # 任务内部 checkpoint 走底层写库，不受编辑锁限制
        store.write_chapter_content("1", "任务写入")
        assert _leaf_contents()["1"] == "任务写入"

    def test_node_knowledge_save_roundtrip(self):
        _seed_outline()
        doc_id = _seed_document("A", [{"title": "光伏组件选型", "content": "组件效率 21%。"}])
        report = store.save_reference_documents([doc_id])
        assert report["referenceDocumentIds"] == [doc_id]
        item_id = _first_item_id(doc_id)

        store.save_node_knowledge_items("2.1", [item_id])
        leaf = store.collect_leaves(store.load_report()["outlineData"]["outline"])[1]
        assert leaf["knowledgeItemIds"] == [item_id]

        store.save_node_knowledge_items("2.1", [])
        leaf = store.collect_leaves(store.load_report()["outlineData"]["outline"])[1]
        assert "knowledgeItemIds" not in leaf


# ======================= B. 知识库选材 =======================


def _first_item_id(doc_id: int) -> int:
    with get_db() as conn:
        row = conn.execute(
            "SELECT id FROM knowledge_items WHERE source_doc_id = ? ORDER BY id ASC", (doc_id,)
        ).fetchone()
    return int(row["id"])


class TestKnowledgeScoring:
    def test_normalize_rejects_invalid(self):
        assert kb.normalize_knowledge_item({"id": "abc"}) is None
        assert kb.normalize_knowledge_item({"id": 0}) is None
        assert kb.normalize_knowledge_item("x") is None
        item = kb.normalize_knowledge_item({"id": "7", "title": "T", "keywords": "光伏"})
        assert item["id"] == 7 and item["keywords"] == ["光伏"]

    def test_score_counts_unique_matched_chars(self):
        item = {"title": "光伏组件选型", "summary": "", "keywords": [], "content": ""}
        assert kb.score_knowledge_item(item, {"title": "光伏组件", "description": ""}) == 4
        assert kb.score_knowledge_item(item, {"title": "施工工期安排", "description": ""}) == 0

    def test_explicit_ids_override_auto_match(self):
        items = [
            {"id": 1, "title": "光伏组件选型", "summary": "", "keywords": [], "content": ""},
            {"id": 2, "title": "风机基础", "summary": "", "keywords": [], "content": ""},
        ]
        picked = kb.select_knowledge_items(items, {"title": "光伏组件"}, explicit_ids=[2])
        assert [item["id"] for item in picked] == [2]

    def test_explicit_missing_falls_back_to_auto(self):
        items = [{"id": 1, "title": "光伏组件选型", "summary": "", "keywords": [], "content": ""}]
        picked = kb.select_knowledge_items(items, {"title": "光伏组件"}, explicit_ids=[999])
        assert [item["id"] for item in picked] == [1]

    def test_top_n_and_min_score(self):
        items = [
            {"id": index, "title": "光伏组件选型", "summary": "", "keywords": [], "content": ""}
            for index in range(1, 6)
        ]
        picked = kb.select_knowledge_items(items, {"title": "光伏组件"}, top_n=2)
        assert [item["id"] for item in picked] == [1, 2]  # 同分按 id 升序，结果稳定
        assert kb.select_knowledge_items(items, {"title": "无关主题"}) == []

    def test_format_truncates_single_item_and_total(self):
        item = {"id": 1, "title": "标题", "content": "字" * 100}
        text = kb.format_knowledge_material([item], item_chars=10)
        assert text == "### 标题\n\n" + "字" * 10
        many = [{"id": i, "title": f"T{i}", "content": "字" * 500} for i in range(1, 6)]
        total = kb.format_knowledge_material(many, item_chars=500, max_chars=1200)
        assert len(total) <= 1200

    def test_lightweight_catalog(self):
        catalog = kb.format_lightweight_catalog(
            [{"id": 3, "title": "光伏组件选型", "summary": "组件效率对比" * 40}]
        )
        assert catalog.startswith("- [3] 光伏组件选型：")
        assert len(catalog.splitlines()[0]) < 200  # 摘要被截断

    def test_normalize_outline_tree_filters_hallucinated_ids(self):
        raw = [{"title": "概述", "description": "d", "knowledge_item_ids": [1, 999]}]
        filtered = store.normalize_outline_tree(raw, allowed_knowledge_ids=[1, 2])
        assert filtered[0]["knowledge_item_ids"] == [1]
        # 未传白名单时不做事后过滤（AI 结果由调用方决定）
        assert store.normalize_outline_tree(raw)[0]["knowledge_item_ids"] == [1, 999]


class TestReferenceDocuments:
    def test_normalize_document_ids(self):
        assert store.normalize_document_ids(["3", 3, "0", "abc", -1, 5]) == [3, 5]
        assert store.normalize_document_ids("not-a-list") == []
        assert len(store.normalize_document_ids(list(range(1, 80)))) == store.MAX_REFERENCE_DOCUMENTS

    def test_save_and_reset(self):
        report = store.save_reference_documents(["7", 7, "x"])
        assert report["referenceDocumentIds"] == [7]
        assert store.load_report()["referenceDocumentIds"] == [7]
        store.clear_workspace()
        assert store.load_report()["referenceDocumentIds"] == []

    def test_document_summaries_and_items(self):
        doc_id = _seed_document(
            "B",
            [
                {"title": "光伏组件选型", "summary": "效率对比", "content": "组件效率 21%。", "keywords": '["光伏"]'},
                {"title": "风机基础", "summary": "基础形式", "content": "采用桩基础。"},
            ],
        )
        summaries = {d["id"]: d for d in knowledge_store.list_document_summaries()}
        assert summaries[doc_id]["itemCount"] == 2
        assert summaries[doc_id]["itemChars"] > 0

        rows = knowledge_store.list_items_by_document_ids([doc_id])
        assert len(rows) == 2
        assert rows[0]["keywords"] == ["光伏"]  # JSON 字段已解析

        items = kb.load_reference_items([doc_id])
        assert len(items) == 2
        assert items[0]["documentName"].startswith(_TEST_DOC_PREFIX)
        assert kb.load_reference_items([]) == []

    def test_chapter_material_prefers_explicit_then_auto(self):
        doc_id = _seed_document(
            "C",
            [
                {"title": "光伏组件选型", "summary": "", "content": "组件效率 21%。"},
                {"title": "风力发电机组", "summary": "", "content": "单机 5MW。"},
            ],
        )
        items = kb.load_reference_items([doc_id])
        auto = kb.chapter_material_text({"title": "光伏组件", "description": ""}, items)
        assert "光伏组件选型" in auto and "风力发电机组" not in auto

        by_id = {item["title"]: item["id"] for item in items}
        explicit = kb.chapter_material_text(
            {"title": "光伏组件", "knowledge_item_ids": [by_id["风力发电机组"]]}, items
        )
        assert "风力发电机组" in explicit and "光伏组件选型" not in explicit

    def test_chapter_overview_and_node_lookup(self):
        doc_id = _seed_document("D", [{"title": "光伏组件选型", "summary": "s", "content": "c"}])
        store.save_reference_documents([doc_id])
        _seed_outline(with_content=False)
        outline = store.load_report()["outlineData"]["outline"]

        node = kb.find_outline_node(outline, "2.1")
        assert node and node["title"] == "工艺路线"
        assert kb.find_outline_node(outline, "9.9") is None

        overview = kb.chapter_knowledge_overview(node, kb.load_reference_items([doc_id]))
        assert overview["explicit"] is False
        assert overview["candidates"]
        assert all("score" in candidate for candidate in overview["candidates"])


# ======================= C. HTTP 契约 =======================


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as test_client:
        yield test_client


class TestRoutes:
    def test_reference_documents_roundtrip(self, client):
        doc_id = _seed_document("E", [{"title": "光伏组件选型", "content": "c"}])
        resp = client.put("/api/v1/feasibility/reference-documents", json={"document_ids": [doc_id]})
        assert resp.status_code == 200, resp.text
        assert resp.json()["referenceDocumentIds"] == [doc_id]
        assert client.get("/api/v1/feasibility").json()["referenceDocumentIds"] == [doc_id]

    def test_knowledge_documents_listing(self, client):
        doc_id = _seed_document("F", [{"title": "光伏组件选型", "content": "c"}])
        resp = client.get("/api/v1/feasibility/knowledge-documents")
        assert resp.status_code == 200
        assert any(doc["id"] == doc_id for doc in resp.json()["documents"])

    def test_chapter_knowledge_404_and_200(self, client):
        _seed_outline(with_content=False)
        assert client.get("/api/v1/feasibility/chapters/9.9/knowledge").status_code == 404

        doc_id = _seed_document("G", [{"title": "工艺路线比选", "content": "比选论证"}])
        client.put("/api/v1/feasibility/reference-documents", json={"document_ids": [doc_id]})
        resp = client.get("/api/v1/feasibility/chapters/2.1/knowledge")
        assert resp.status_code == 200, resp.text
        assert resp.json()["itemPoolSize"] == 1

    def test_outline_save_via_http_keeps_content(self, client):
        _seed_outline()
        resp = client.put(
            "/api/v1/feasibility/outline",
            json={
                "outline_data": {
                    "outline": [
                        {
                            "id": "1",
                            "title": "建设方案",
                            "description": "d",
                            "children": [{"id": "1.1", "title": "工艺路线", "description": "d"}],
                        }
                    ]
                },
                "reason": "delete",
                "id_map": {"2": "1", "2.1": "1.1"},
                "affected_node_ids": [],
            },
        )
        assert resp.status_code == 200, resp.text
        leaves = store.collect_leaves(resp.json()["outlineData"]["outline"])
        assert leaves[0]["content"] == "工艺路线正文"

    def test_outline_save_conflict_when_task_running(self, client):
        _seed_outline()
        store.save_task_state("feasibility-outline", {"task_id": "t", "status": "running", "progress": 10})
        resp = client.put(
            "/api/v1/feasibility/outline",
            json={"outline_data": {"outline": [{"title": "x", "description": ""}]}, "reason": "edit"},
        )
        assert resp.status_code == 409

    def test_node_knowledge_save_via_http(self, client):
        _seed_outline(with_content=False)
        doc_id = _seed_document("H", [{"title": "工艺路线比选", "content": "比选论证"}])
        item_id = _first_item_id(doc_id)
        resp = client.put(f"/api/v1/feasibility/chapters/2.1/knowledge", json={"item_ids": [item_id]})
        assert resp.status_code == 200, resp.text
        leaf = store.collect_leaves(resp.json()["outlineData"]["outline"])[1]
        assert leaf["knowledgeItemIds"] == [item_id]
        assert client.put("/api/v1/feasibility/chapters/9.9/knowledge", json={"item_ids": []}).status_code == 404
