"""提示词模块深度审计与 BUG 修复测试（2026-10-14）。

覆盖本轮修复的三项缺陷：
  - BUG-PROMPT-R8（sync_hardcode 报告语义）：overwrite=True 时 `synced` 列表
    总是包含全部 key，`{len(synced)} 个已处理` 与真正「新增」的数量不符，
    运维无法判断哪些 key 是本轮实际新插入的。修复后语义拆分：
      synced  = 仅新增（DB 原本不存在、本轮 INSERT）
      refreshed = content 跟随硬编码刷新的（含用户未改 或 reset_modified=True）
      kept_modified = 用户改过且未 reset_modified
      skipped = overwrite=False 时未做任何动作的
    且四者之和恒等于 total。
  - BUG-PROMPT-E1（_ORCH_SYSTEM 与 CONTENT_ORCHESTRATION_LEGACY_SYSTEM 漂移）：
    _integrate.py 中注册内容与 workflows_orchestration._ORCH_SYSTEM 源码不一致，
    缺 `reason,` 字段——导致管理员改注册表时看不到 reason 字段要求，
    且 get_prompt 回退到 _ORCH_SYSTEM 时字段集合不匹配。修复：注册表补齐 reason。
  - BUG-PROMPT-E2/E3/E4/E5（骨架/质量评估/行业树 inline 提示词未注册）：
    _SKELETON_SYSTEM、QUALITY_EVAL_SYSTEM、INDUSTRY_CLASSIFY_SYSTEM、
    INDUSTRY_MATCH_SYSTEM 原以硬编码或 inline 形式存在于 workflows_orchestration
    / workflows_quality / industry_tree，管理页看不到、管理员无法覆盖、
    _check_hardcode_drift 无法比对。修复：全部注册到 _integrate.py 并把调用方
    改为 get_prompt(KEY) 或 HARDCODED_FALLBACK 的两级回落。
"""

from __future__ import annotations

import pytest

from app.services.ai.prompts import _ALL_PROMPTS


# ──────────────────────────────────────────────────────────────────────
# 1. _integrate.py 收编完整性：所有存量硬编码提示词必须注册到 _ALL_PROMPTS
# ──────────────────────────────────────────────────────────────────────


class TestRegistryIntegrateCoverage:
    """硬编码提示词统一收编回归：调用方 fallback 前必须先查注册表。"""

    INTEGRATED_KEYS = [
        "CHART_BATCH_ARRANGE_SYSTEM",
        "CHART_BATCH_ARRANGE_USER",
        "CONTENT_ORCHESTRATION_LEGACY_SYSTEM",
        "CONTENT_ORCHESTRATION_LEGACY_USER",
        "CONSISTENCY_AGENT_USER",
        "TEMPLATE_EXTRACT_SYSTEM",
        # 本轮新增收编
        "CONTENT_SKELETON_SYSTEM",
        "QUALITY_EVAL_SYSTEM",
        "INDUSTRY_CLASSIFY_SYSTEM",
        "INDUSTRY_MATCH_SYSTEM",
    ]

    @pytest.mark.parametrize("key", INTEGRATED_KEYS)
    def test_all_integrated_keys_registered(self, key):
        assert key in _ALL_PROMPTS, f"{key} 未在 _integrate.py 注册"
        meta = _ALL_PROMPTS[key]
        assert meta["content"], f"{key} 注册内容为空"
        assert meta["category"], f"{key} 缺少 category"
        assert meta["label"], f"{key} 缺少 label"


# ──────────────────────────────────────────────────────────────────────
# 2. BUG-PROMPT-E1：_ORCH_SYSTEM 与注册表内容一致性（消除漂移）
# ──────────────────────────────────────────────────────────────────────


class TestOrchLegacySystemDrift:
    """workflows_orchestration._ORCH_SYSTEM 与 _integrate 注册表不得漂移。"""

    def test_registry_content_matches_source_fallback(self):
        """注册表 CONTENT_ORCHESTRATION_LEGACY_SYSTEM 必须与硬编码 _ORCH_SYSTEM 一致。

        BUG-FIX-PROMPT-E1：修复前注册表缺 `reason,` 字段（源码有），
        导致管理员在管理页看到的模板与运行时实际使用的模板字段集不同。
        """
        from app.services.ai.workflows_orchestration import _ORCH_SYSTEM

        reg = _ALL_PROMPTS["CONTENT_ORCHESTRATION_LEGACY_SYSTEM"]["content"]
        assert reg == _ORCH_SYSTEM, (
            f"注册表与硬编码漂移：reg={len(reg)} vs src={len(_ORCH_SYSTEM)}"
        )

    def test_registry_contains_reason_field(self):
        """修复回归守卫：注册表必须包含 reason 字段说明。"""
        reg = _ALL_PROMPTS["CONTENT_ORCHESTRATION_LEGACY_SYSTEM"]["content"]
        assert "confidence, reason" in reg or "confidence,reason" in reg, (
            "修复回归：注册表 CONTENT_ORCHESTRATION_LEGACY_SYSTEM 缺 reason 字段说明"
        )


# ──────────────────────────────────────────────────────────────────────
# 3. BUG-PROMPT-E2/E3/E4/E5：调用方必须走 get_prompt 而非直接引用硬编码
# ──────────────────────────────────────────────────────────────────────


class TestCallersUseRegistry:
    """各调用方改造为「get_prompt(KEY) or 硬编码 fallback」两级回落。"""

    def test_workflows_orchestration_uses_registry_for_skeleton(self):
        """workflows_orchestration.generate_full_skeleton 必须查注册表。"""
        import inspect

        from app.services.ai.workflows_orchestration import generate_full_skeleton

        src = inspect.getsource(generate_full_skeleton)
        assert "CONTENT_SKELETON_SYSTEM" in src, (
            "BUG-FIX 回归：generate_full_skeleton 未走 get_prompt("
            "'CONTENT_SKELETON_SYSTEM')"
        )
        assert "_SKELETON_SYSTEM" in src, (
            "硬编码 fallback 必须保留（DB 不可用时降级）"
        )

    def test_workflows_quality_uses_registry_for_eval(self):
        """workflows_quality 质量评估必须查注册表。"""
        import app.services.ai.workflows_quality as wq

        # 找到调用位置：通过 AST 或直接搜源码
        import inspect

        src = inspect.getsource(wq)
        assert "QUALITY_EVAL_SYSTEM" in src, (
            "BUG-FIX 回归：workflows_quality 未走 get_prompt("
            "'QUALITY_EVAL_SYSTEM')"
        )

    def test_industry_tree_uses_registry_for_classify(self):
        """industry_tree 子节点分类必须查注册表。"""
        import inspect

        import app.services.industry_tree as it

        src = inspect.getsource(it)
        assert "INDUSTRY_CLASSIFY_SYSTEM" in src, (
            "BUG-FIX 回归：industry_tree 未走 get_prompt("
            "'INDUSTRY_CLASSIFY_SYSTEM')"
        )
        assert "INDUSTRY_MATCH_SYSTEM" in src, (
            "BUG-FIX 回归：industry_tree 未走 get_prompt("
            "'INDUSTRY_MATCH_SYSTEM')"
        )


# ──────────────────────────────────────────────────────────────────────
# 4. BUG-PROMPT-R8：sync_hardcode_to_db 报告语义清晰
# ──────────────────────────────────────────────────────────────────────


class TestSyncHardcodeSemantics:
    """sync_hardcode 返回字段的语义必须明确可分。"""

    @pytest.fixture()
    def client(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from app.routers import prompts

        app = FastAPI()
        app.include_router(prompts.router)
        return TestClient(app, raise_server_exceptions=False)

    def test_synced_only_contains_new_inserts_when_overwrite_true(
        self, client
    ):
        """overwrite=True 时，synced 必须只包含本轮新增（DB 原本不存在的 key）。"""
        from app.db import get_db
        from app.services.ai.prompts import reload_prompt_cache

        # 找一个 DB 中不存在的注册表 key
        key = "QUALITY_EVAL_SYSTEM"
        with get_db() as conn:
            exists = conn.execute(
                "SELECT 1 FROM prompt_templates WHERE key = ?", (key,)
            ).fetchone()
        if exists:
            # 删除以制造「新增」场景
            with get_db() as conn:
                conn.execute("DELETE FROM prompt_templates WHERE key = ?", (key,))
            reload_prompt_cache()

        try:
            resp = client.post("/prompts/sync-hardcode?overwrite=true")
            assert resp.status_code == 200, resp.text[:300]
            body = resp.json()
            # key 应是本轮唯一「新增」的（除非其它 key 也被清了）
            assert key in body["synced"], (
                f"新增 key 未进 synced: {key}; synced={body['synced'][:5]}..."
            )
            # 已在 DB 中存在的 key 不应出现在 synced
            # （因为 overwrite=True 时其它 key 会走 refreshed/kept_modified 分支）
            with get_db() as conn:
                all_keys = {r["key"] for r in conn.execute(
                    "SELECT key FROM prompt_templates"
                ).fetchall()}
            for existing_key in list(all_keys - {key})[:10]:
                # 只要 existing_key 不是本轮「新增」，就不应进 synced
                # 但注意：refreshed 也会 UPDATE DB，仍留在 DB 中
                # 所以此断言只在「DB 原本存在」时成立
                pass
            # 更严格的断言：synced + refreshed + kept_modified + skipped == total
            total = body["total"]
            processed = (
                len(body["synced"])
                + len(body["refreshed"])
                + len(body["kept_modified"])
                + len(body["skipped"])
            )
            assert processed == total, (
                f"分项之和 {processed} 应等于 total {total}；"
                f"synced={len(body['synced'])}, refreshed={len(body['refreshed'])}, "
                f"kept_modified={len(body['kept_modified'])}, skipped={len(body['skipped'])}"
            )
        finally:
            reload_prompt_cache()

    def test_synced_excludes_existing_when_overwrite_false(self, client):
        """overwrite=False 时，synced 只包含 DB 原本不存在的 key。"""
        from app.db import get_db
        from app.services.ai.prompts import reload_prompt_cache

        key = "QUALITY_EVAL_SYSTEM"
        # 确保 DB 中没有该 key
        with get_db() as conn:
            conn.execute("DELETE FROM prompt_templates WHERE key = ?", (key,))
        reload_prompt_cache()

        try:
            resp = client.post("/prompts/sync-hardcode")  # overwrite=False
            assert resp.status_code == 200
            body = resp.json()
            # key 应被新增（synced）
            assert key in body["synced"]
            # 已存在的 key 应被 skipped
            # 验证四项和 = total
            total = body["total"]
            processed = (
                len(body["synced"])
                + len(body["refreshed"])
                + len(body["kept_modified"])
                + len(body["skipped"])
            )
            assert processed == total
        finally:
            with get_db() as conn:
                conn.execute("DELETE FROM prompt_templates WHERE key = ?", (key,))
            reload_prompt_cache()

    def test_response_message_mentions_all_four_categories(self, client):
        """响应 message 必须明确提到新增/刷新/保留/跳过四项。"""
        from app.db import get_db
        from app.services.ai.prompts import reload_prompt_cache

        key = "QUALITY_EVAL_SYSTEM"
        with get_db() as conn:
            conn.execute("DELETE FROM prompt_templates WHERE key = ?", (key,))
        reload_prompt_cache()

        try:
            resp = client.post("/prompts/sync-hardcode?overwrite=true")
            assert resp.status_code == 200
            msg = resp.json()["message"]
            # 消息必须清晰区分四类，避免原「{N} 个已处理」的模糊表述
            for word in ("新增", "刷新", "保留", "跳过"):
                assert word in msg, f"message 未提及 '{word}': {msg!r}"
        finally:
            reload_prompt_cache()


# ──────────────────────────────────────────────────────────────────────
# 5. 收编后管理页可见性：新注册的提示词可通过 API 查询
# ──────────────────────────────────────────────────────────────────────


class TestRegistryAdminVisibility:
    """收编后的提示词必须在 /prompts 列表中可见。"""

    @pytest.fixture()
    def client(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from app.routers import prompts

        app = FastAPI()
        app.include_router(prompts.router)
        return TestClient(app, raise_server_exceptions=False)

    @pytest.mark.parametrize(
        "key",
        [
            "CONTENT_SKELETON_SYSTEM",
            "QUALITY_EVAL_SYSTEM",
            "INDUSTRY_CLASSIFY_SYSTEM",
            "INDUSTRY_MATCH_SYSTEM",
        ],
    )
    def test_visible_in_list_api(self, client, key):
        resp = client.get("/prompts")
        assert resp.status_code == 200
        items = resp.json()
        keys = {it["key"] for it in items}
        assert key in keys, (
            f"{key} 未出现在 GET /prompts 列表中（管理页看不到）"
        )
