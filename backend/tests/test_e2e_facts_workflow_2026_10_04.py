"""全局事实模块 —— HTTP 路由级端到端工作流验收（ENHANCE 2026-10-04）。

与 `test_facts_module_interactions.py`（服务层/函数级契约）互补，本文件从
**前端实际经过的 HTTP 路由**贯穿全局事实模块的完整工作流：

  提取（POST /facts/generate，placeholder 策略）
  → 回读（GET /facts：items/policy/categories/placeholder_count 四元契约）
  → 人工编辑（新增/删行/改值）→ 保存（POST /facts）
  → 写入侧校验（空标识 400 / 同 key 重复 400 / 生成中 409）
  → 确认（POST /facts/confirm：空基准 400 防御）
  → 审计（POST /facts/audit）→ 落库回看（GET /facts.last_audit）
  → 重新保存后审计作废（缓存失效链）
  → 正文生成读取口径（facts_for_generation 与 GET 同口径，占位不外泄）

AI 调用全部注入替身：验证的是路由/数据链，不是模型输出。
"""
from __future__ import annotations

import json

from fastapi.testclient import TestClient
import pytest

from app.db import get_db, init_db


API = "/api/v1"

AI_FACTS_DICT = {
    "projectName": "市政道路改造工程",
    "scheduleDuration": "450 日历天",
}


@pytest.fixture()
def client():
    init_db()
    from app.main import app

    return TestClient(app)


def _new_project(client) -> int:
    r = client.post(f"{API}/projects", json={"name": "事实端到端", "industry": "市政工程"})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _patch_extract_ai(monkeypatch, payload):
    """替换 /facts/generate 的 AI 与文件来源（与交互测试同一打桩口径）。"""
    from app.routers import facts as facts_router
    from app.services.ai import provider_factory, workflows

    monkeypatch.setattr(facts_router, "get_facts_file_text", lambda _pid: "招标文件正文：工期要求 450 日历天。")
    monkeypatch.setattr(provider_factory, "get_ai_config", lambda: object())
    monkeypatch.setattr(provider_factory, "create_provider", lambda _cfg: object())

    async def _fake_collect(_provider, _messages, **_kwargs):
        return payload

    monkeypatch.setattr(workflows, "collect_json_response", _fake_collect)


def _patch_audit_ai(monkeypatch, responder):
    """替换 /facts/audit 的 AI（按送审批次号返回确定性结论）。"""
    import re

    from app.services.ai import provider_factory, workflows

    monkeypatch.setattr(provider_factory, "get_ai_config", lambda: object())
    monkeypatch.setattr(provider_factory, "create_provider", lambda _cfg: object())

    async def _fake_collect(_provider, messages, **_kwargs):
        user = str(getattr(messages[1], "content", ""))
        matched = re.search(r"第 (\d+) 批", user)
        return responder(int(matched.group(1)) if matched else 1)

    monkeypatch.setattr(workflows, "collect_json_response", _fake_collect)


class TestFactsHttpWorkflow:
    def test_generate_read_edit_save_confirm_audit_reload_chain(self, client, monkeypatch):
        """主链贯通：提取(placeholder) → 回读 → 编辑保存 → 确认 → 审计 → 刷新回看。"""
        pid = _new_project(client)
        _patch_extract_ai(monkeypatch, AI_FACTS_DICT)

        # ① 提取：placeholder 策略。dict 里没有项目经理 → 不编造，且「必须有工期」
        #    规则兜底出 scheduleDuration（值来自 AI，不再追加默认行）。
        r = client.post(f"{API}/projects/{pid}/facts/generate", json={"missing_value_policy": "placeholder"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["extraction_mode"] == "ai"
        assert body["missing_value_policy"] == "placeholder"
        keys = {i["key"] for i in body["items"]}
        assert {"projectName", "scheduleDuration"} <= keys

        # ② 回读：GET /facts 与 POST /generate 同一解析口径，且下发分类正本
        r = client.get(f"{API}/projects/{pid}/facts")
        assert r.status_code == 200
        got = r.json()
        assert [i["key"] for i in got["items"]] == [i["key"] for i in body["items"]]
        assert got["missing_value_policy"] == "placeholder"
        assert got["categories"]["implementation"] == "实施做法"
        assert got["confirmed"] is False

        # ③ 人工编辑：补一条 manual 行、改掉占位缺值标记（服务端以 value 为准）
        edited = [
            {"key": "projectName", "label": "项目名称", "value": "市政道路改造工程", "category": "basic"},
            {"key": "scheduleDuration", "label": "建设工期", "value": "460 日历天", "category": "schedule"},
            {"key": "projectManager", "label": "项目经理", "value": "张工", "category": "personnel", "source": "manual"},
        ]
        r = client.post(f"{API}/projects/{pid}/facts", json={"items": edited})
        assert r.status_code == 200, r.text

        # ④ 确认 + 幂等重放（前端「已确认再点击」路径）
        assert client.post(f"{API}/projects/{pid}/facts/confirm").status_code == 200
        assert client.post(f"{API}/projects/{pid}/facts/confirm").status_code == 200
        r = client.get(f"{API}/projects/{pid}/facts")
        assert r.json()["confirmed"] is True
        by_key = {i["key"]: i for i in r.json()["items"]}
        assert by_key["projectManager"]["source"] == "manual", "手工补录来源不得被校形丢掉"

        # ⑤ 审计：需有已完成正文
        with get_db() as conn:
            conn.execute(
                "INSERT INTO sections (project_id, title, level, sort_order, content, status) "
                "VALUES (?, '第一章 施工部署', 1, 0, '本工程工期 460 天，项目经理张工。', 'completed')",
                (pid,),
            )
        _patch_audit_ai(monkeypatch, lambda idx: [
            {"fact_key": "scheduleDuration", "status": "consistent"},
            {"fact_key": "projectName", "status": "missing", "detail": f"批{idx}未见项目名称"},
        ])
        r = client.post(f"{API}/projects/{pid}/facts/audit")
        assert r.status_code == 200, r.text
        audit = r.json()
        assert audit["audit_status"] == "ok", audit
        assert audit["audited_sections"] == 1

        # ⑥ 刷新回看：审计结果随 GET 回传（旧实现刷新即丢、重复点烧配额）
        r = client.get(f"{API}/projects/{pid}/facts")
        last = r.json()["last_audit"]
        assert last and last["audit_status"] == "ok"
        # BUG-FIX 2026-10-04（前后端接口不一致）：落库载荷历史上只有 `status`，
        # 而前端回看读 `audit_status` → 刷新后降级标签静默消失。现双写，
        # 且两键必须同值（前端回退读 status 兼容旧数据行）。
        assert last["status"] == last["audit_status"]
        assert {i["fact_key"] for i in last["items"]} == {"scheduleDuration", "projectName"}

        # ⑦ 缓存失效链：再次保存 → 旧审计立即作废（另一批值的结论不可复用）
        r = client.post(
            f"{API}/projects/{pid}/facts",
            json={"items": [edited[0], dict(edited[1], value="470 日历天"), edited[2]]},
        )
        assert r.status_code == 200
        r = client.get(f"{API}/projects/{pid}/facts")
        assert r.json()["last_audit"] in (None, {})
        assert r.json()["confirmed"] is False

    def test_write_side_validation_over_http(self, client, monkeypatch):
        """写入侧三类拦截：空标识 400 / 同 key 重复 400 / 生成进行中 409。"""
        from app.services import task_lock

        pid = _new_project(client)
        good = [{"key": "k1", "label": "工期", "value": "450天", "category": "schedule"}]

        r = client.post(f"{API}/projects/{pid}/facts", json={"items": [{"key": "  ", "label": "无标识", "value": "x"}]})
        assert r.status_code == 400 and "标识" in r.json()["detail"]

        r = client.post(
            f"{API}/projects/{pid}/facts",
            json={"items": good + [{"key": "k1", "label": "工期2", "value": "500天", "category": "schedule"}]},
        )
        assert r.status_code == 400 and "k1" in r.json()["detail"]

        # 合法保存一次，确认拦截是「写前校验」而非「整端点不可用」
        assert client.post(f"{API}/projects/{pid}/facts", json={"items": good}).status_code == 200

        lock = task_lock.acquire("content-generation", project_id=pid)
        try:
            r = client.post(f"{API}/projects/{pid}/facts", json={"items": good})
            assert r.status_code == 409, "正文生成进行中不得覆盖事实基准"
        finally:
            task_lock.release(lock["lock_key"], lock["owner_id"])

    def test_confirm_requires_usable_baseline_over_http(self, client):
        """确认防御：空事实集/全占位经 HTTP 同样被 400 拦下，库里不残留 confirmed=1。"""
        pid = _new_project(client)
        r = client.post(f"{API}/projects/{pid}/facts/confirm")
        assert r.status_code == 400 and "没有可确认" in r.json()["detail"]

        with get_db() as conn:
            conn.execute(
                "INSERT INTO analysis (project_id, global_facts_json) VALUES (?, ?) "
                "ON CONFLICT(project_id) DO UPDATE SET global_facts_json = excluded.global_facts_json",
                (pid, json.dumps(
                    [{"key": "k1", "label": "工期", "value": "【待填写】", "category": "schedule"}],
                    ensure_ascii=False,
                )),
            )
        r = client.post(f"{API}/projects/{pid}/facts/confirm")
        assert r.status_code == 400
        with get_db() as conn:
            row = conn.execute("SELECT confirmed_facts FROM analysis WHERE project_id = ?", (pid,)).fetchone()
        assert row is None or not row["confirmed_facts"]

    def test_generation_reader_shares_placeholder_filter(self, client, monkeypatch):
        """跨模块数据链：placeholder 策略下，正文生成读取口径必须剔掉占位行，
        而 GET /facts（人工视图）保留它们 —— 两个口径由同一正本派生。"""
        from app.services.facts_service import facts_for_generation

        pid = _new_project(client)
        _patch_extract_ai(monkeypatch, {"projectName": "示例工程"})  # 无工期 → 兜底行按策略留占位

        r = client.post(f"{API}/projects/{pid}/facts/generate", json={"missing_value_policy": "placeholder"})
        assert r.status_code == 200
        assert r.json()["placeholder_count"] >= 1

        with get_db() as conn:
            raw = conn.execute(
                "SELECT global_facts_json FROM analysis WHERE project_id = ?", (pid,)
            ).fetchone()["global_facts_json"]

        stored = json.loads(raw)
        assert any("【待填写" in str(i.get("value")) for i in stored), "人工视图库里应留有占位行"
        gen = facts_for_generation(raw)
        assert all("【待填写" not in str(i.get("value")) for i in gen), "生成口径必须剔除占位，防泄漏进正文"
        assert {"projectName"} <= {i["key"] for i in gen}, "非占位事实不得被误滤"
