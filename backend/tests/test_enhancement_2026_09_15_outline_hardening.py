"""目录生成模块约束性加固回归测试（2026-09-15）

深度探索目录生成（技术方案 V5.1 SSE 链路）后落地的四个修复，均为
"历史上单测覆盖不到、线上必崩或静默丢数据" 的缺陷：

1. `POST /sections/review` 与 `/sections/review-with-autofix` 从 services 层
   取不到 `review_outline` / `review_outline_with_autofix`，端点必抛
   ImportError → HTTP 500，前端「AI 审核目录 / 自动修复」按钮从未可用；
2. 技术方案侧的目录编辑端点没有「任务运行中」防护，SSE 目录生成收尾的
   `DELETE FROM sections` + 全量 INSERT 会静默覆盖用户的人工改动；
3. 归一化能力（剥编号 / 过滤空标题与 AI 错误响应 / 同级去重 / level 按父子
   关系重算）此前只存在于工具库并被单测引用，主链路落库前未接，AI 偶尔返回
   带编号前缀的标题会直接入库；
4. `/review-with-autofix`、`/reorder`、`/expand-content` 缺 `limit_per_endpoint`。

按项目约定：只测行为，不测实现细节。
"""

import asyncio

import pytest
from fastapi.testclient import TestClient

from app.db import get_db
from app.main import app
from app.models import SectionCreate
from app.services import task_lock
from app.services.sections import create_section

_SECTIONS_BASE = "/api/v1/projects/{pid}/sections"


def _seed_project(name: str) -> int:
    with get_db() as conn:
        cur = conn.execute("INSERT INTO projects (name, industry) VALUES (?, ?)", (name, "市政"))
        return int(cur.lastrowid)


def _seed_section(project_id: int, title: str) -> int:
    return int(create_section(project_id, SectionCreate(title=title)).id)


def _seed_analysis(project_id: int, scoring_points: str = "[]") -> None:
    with get_db() as conn:
        conn.execute(
            "INSERT INTO analysis (project_id, scoring_points_json) VALUES (?, ?) "
            "ON CONFLICT(project_id) DO UPDATE SET scoring_points_json = excluded.scoring_points_json",
            (project_id, scoring_points),
        )


def _patch_ai_provider(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.services.ai.provider_factory.get_ai_config", lambda: {"api_key": "test"}
    )
    monkeypatch.setattr("app.services.ai.provider_factory.create_provider", lambda cfg: object())


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client


# ─────────────────────────────────────────────────────────────────────────────
# 1. 目录审核端点接线
# ─────────────────────────────────────────────────────────────────────────────


class TestReviewEndpointsWiring:
    def test_facade_exports_review_symbols(self):
        """BUG-FIX 回归：facade 未导出 review_* 时，两个审核端点必 ImportError"""
        from app.services.ai import workflows

        assert callable(workflows.review_outline)
        assert callable(workflows.review_outline_with_autofix)

    def test_review_endpoint_returns_result(self, client, monkeypatch):
        """`/review` 必须返回 200 + {"status": "completed", "result": {...}}"""
        import app.services.ai.workflows as workflows

        pid = _seed_project("目录审核端点")
        _seed_analysis(pid)
        _patch_ai_provider(monkeypatch)

        async def _fake_review(provider, outline, scoring_points, summary="", industry_profile=""):
            assert isinstance(outline, list), "审核入参应为已解析的目录树列表"
            return {"passed": True, "issues": [], "suggestions": []}

        monkeypatch.setattr(workflows, "review_outline", _fake_review)

        resp = client.post(_SECTIONS_BASE.format(pid=pid) + "/review")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "completed"
        assert body["result"]["passed"] is True

    def test_review_with_autofix_endpoint_returns_result(self, client, monkeypatch):
        """`/review-with-autofix` 同样不能 500"""
        import app.services.ai.workflows as workflows

        pid = _seed_project("目录自动修复端点")
        _seed_analysis(pid)
        _patch_ai_provider(monkeypatch)

        async def _fake_autofix(provider, scoring_points, outline, *args, **kwargs):
            return {"passed": False, "repaired": True, "issues": [], "suggestions": []}

        monkeypatch.setattr(workflows, "review_outline_with_autofix", _fake_autofix)

        resp = client.post(_SECTIONS_BASE.format(pid=pid) + "/review-with-autofix")
        assert resp.status_code == 200, resp.text
        assert resp.json()["result"]["repaired"] is True

    def test_review_requires_scoring_points(self, client, monkeypatch):
        """未做评分点识别的项目，审核应给出 400 而不是 500"""
        import app.services.ai.workflows as workflows

        pid = _seed_project("无评分点项目")
        _patch_ai_provider(monkeypatch)
        monkeypatch.setattr(workflows, "review_outline", lambda *a, **k: {"passed": True})

        resp = client.post(_SECTIONS_BASE.format(pid=pid) + "/review")
        assert resp.status_code == 400, resp.text


class TestPlainReviewServiceBehavior:
    """review_outline 服务自身的兜底行为（无需 AI、无需网络）"""

    @staticmethod
    def _run(monkeypatch, collect_result):
        from app.services.ai import workflows_quality as quality

        async def _fake_collect(provider, messages, **kwargs):
            return collect_result

        monkeypatch.setattr(quality, "collect_json_response", _fake_collect)
        return asyncio.run(quality.review_outline(object(), [], "[]"))

    def test_missing_passed_is_treated_as_failed(self, monkeypatch):
        """模型漏写 passed 时按未通过处理（历史实现默认 True 会跳过自修复）"""
        result = self._run(monkeypatch, {"issues": ["缺少评分点覆盖"], "suggestions": []})
        assert result["passed"] is False
        assert result["issues"] == ["缺少评分点覆盖"]
        assert result["suggestions"] == []
        assert result["fixed_outline"] is None

    def test_junk_ai_output_returns_failed_not_none(self, monkeypatch):
        """AI 返回非 JSON / None 时必须返回可读的兜底结构，路由层不必再判空"""
        result = self._run(monkeypatch, None)
        assert result["passed"] is False
        assert result["issues"], "兜底结果必须带可读文案"

    def test_ai_exception_returns_failed_not_raise(self, monkeypatch):
        """provider 抛异常时不向上冒泡，端点仍能给出未通过结论"""
        from app.services.ai import workflows_quality as quality

        async def _boom(provider, messages, **kwargs):
            raise RuntimeError("provider down")

        monkeypatch.setattr(quality, "collect_json_response", _boom)
        result = asyncio.run(quality.review_outline(object(), [], "[]"))
        assert result["passed"] is False

    def test_passthrough_of_valid_result(self, monkeypatch):
        result = self._run(
            monkeypatch,
            {"passed": True, "confidence": 0.9, "issues": [], "suggestions": ["可补充环保措施"]},
        )
        assert result["passed"] is True
        assert result["suggestions"] == ["可补充环保措施"]


# ─────────────────────────────────────────────────────────────────────────────
# 2. 目录编辑的任务运行中防护
# ─────────────────────────────────────────────────────────────────────────────


class TestOutlineEditingGuarded:
    def test_structural_edits_rejected_while_generation_running(self, client):
        """任务在跑时，目录结构的增删改移动/重排一律 409（此前全裸奔）"""
        pid = _seed_project("生成中禁止编辑")
        sec_id = _seed_section(pid, "既有章节")
        base = _SECTIONS_BASE.format(pid=pid)

        record = task_lock.acquire("outline-generation", project_id=pid, note="单测")
        assert record.get("acquired")
        try:
            assert client.post(base, json={"title": "新章节"}).status_code == 409
            assert client.patch(f"{base}/{sec_id}", json={"title": "改名"}).status_code == 409
            assert client.delete(f"{base}/{sec_id}").status_code == 409
            assert client.post(f"{base}/{sec_id}/move", params={"direction": "up"}).status_code == 409
            assert client.post(
                base + "/reorder",
                json={"groups": [{"parent_id": None, "ordered_ids": [sec_id]}]},
            ).status_code == 409
        finally:
            task_lock.release(record["lock_key"], record["owner_id"])

    def test_content_edit_allowed_while_content_generation_running(self, client):
        """正文生成期间仍要能手写章节内容（结构化改动则拒绝）"""
        pid = _seed_project("正文生成中仍能写正文")
        sec_id = _seed_section(pid, "可润色章节")
        base = _SECTIONS_BASE.format(pid=pid)

        record = task_lock.acquire("content-generation", project_id=pid, note="单测")
        try:
            resp = client.patch(f"{base}/{sec_id}", json={"content": "边生成边润色"})
            assert resp.status_code == 200, resp.text
            assert resp.json()["content"] == "边生成边润色"
            # 结构性改动仍然被拒
            assert client.post(base, json={"title": "正文生成中新增"}).status_code == 409
            assert client.delete(f"{base}/{sec_id}").status_code == 409
        finally:
            task_lock.release(record["lock_key"], record["owner_id"])

    def test_outline_generation_blocks_everything_including_content_edit(self, client):
        """目录生成会整表重建章节，此时连正文改动也必须拒绝"""
        pid = _seed_project("目录生成中禁止改正文")
        sec_id = _seed_section(pid, "待重建章节")
        base = _SECTIONS_BASE.format(pid=pid)

        record = task_lock.acquire("outline-generation", project_id=pid, note="单测")
        try:
            resp = client.patch(f"{base}/{sec_id}", json={"content": "会被整表重建冲掉"})
            assert resp.status_code == 409, resp.text
            assert "目录已锁定" in resp.json()["detail"]
        finally:
            task_lock.release(record["lock_key"], record["owner_id"])

    def test_read_endpoints_still_work_while_generation_running(self, client):
        """只挡写，不挡读：生成过程中前端仍需刷新进度/查看章节树"""
        pid = _seed_project("生成中可读")
        _seed_section(pid, "只读取章节")
        base = _SECTIONS_BASE.format(pid=pid)

        record = task_lock.acquire("outline-generation", project_id=pid, note="单测")
        try:
            assert client.get(base).status_code == 200
            assert client.get(base + "/flat").status_code == 200
        finally:
            task_lock.release(record["lock_key"], record["owner_id"])

    def test_edits_allowed_after_lock_released(self, client):
        """锁释放后必须立刻可编辑，不能把项目永久锁死"""
        pid = _seed_project("释放后可编辑")
        record = task_lock.acquire("outline-generation", project_id=pid, note="单测")
        task_lock.release(record["lock_key"], record["owner_id"])

        resp = client.post(_SECTIONS_BASE.format(pid=pid), json={"title": "释放后新增"})
        assert resp.status_code == 201, resp.text

    def test_expired_lock_does_not_block_editing(self, client):
        """持有者进程崩溃留下超时锁时，编辑必须照常放行"""
        pid = _seed_project("过期锁放行")
        record = task_lock.acquire("outline-generation", project_id=pid, ttl_seconds=1, note="单测")
        with get_db() as conn:
            conn.execute(
                "UPDATE task_locks SET expires_at = datetime('now', 'localtime', '-1 hour') "
                "WHERE id = ?",
                (record["id"],),
            )
        task_lock.release(record["lock_key"], record["owner_id"])

        resp = client.post(_SECTIONS_BASE.format(pid=pid), json={"title": "过期锁后新增"})
        assert resp.status_code == 201, resp.text


class TestSaveOutlineNormalization:
    @staticmethod
    def _save(pid: int, outline: list) -> list:
        from app.routers.sse.sse_db_ops import _clear_and_save_outline

        return _clear_and_save_outline(pid, outline)

    def test_strips_number_prefix_and_drops_empty_and_duplicates(self):
        pid = _seed_project("落库归一化")
        outline = [
            {
                "title": "第一章 总体施工组织布置及规划",
                "level": 1,
                "children": [
                    {
                        "title": "1.1 施工总体布置",
                        "children": [{"title": "1.1.1 布置原则"}],
                    },
                    {"title": "   ", "children": []},
                    {"title": "施工总体布置"},
                ],
            },
            {"title": "", "error": "AI 返回异常"},
        ]

        saved = self._save(pid, outline)

        assert [n["title"] for n in saved] == ["总体施工组织布置及规划"]
        l2 = saved[0]["children"]
        assert [n["title"] for n in l2] == ["施工总体布置"]
        assert l2[0]["children"][0]["title"] == "布置原则"
        # level 始终按父子关系重算，不信任 AI 的字段
        assert saved[0]["level"] == 1
        assert l2[0]["level"] == 2
        assert l2[0]["children"][0]["level"] == 3

    def test_stripped_titles_are_persisted(self):
        """剥编号必须真的写进 DB，而不只是返回给前端的结构"""
        pid = _seed_project("落库归一化-持久化")
        self._save(pid, [{"title": "3、施工现场总平面布置", "level": 1}])

        with get_db(readonly=True) as conn:
            titles = [
                r["title"]
                for r in conn.execute(
                    "SELECT title FROM sections WHERE project_id = ? ORDER BY sort_order", (pid,)
                ).fetchall()
            ]
        assert titles == ["施工现场总平面布置"]

    def test_rerun_replaces_previous_outline(self):
        """重跑目录时旧章节必须被清空，不能叠加"""
        pid = _seed_project("落库归一化-重跑")
        self._save(pid, [{"title": "旧方案"}, {"title": "旧辅助章节"}])
        saved = self._save(pid, [{"title": "新方案"}])

        assert [n["title"] for n in saved] == ["新方案"]
        with get_db(readonly=True) as conn:
            count = conn.execute(
                "SELECT COUNT(*) AS c FROM sections WHERE project_id = ?", (pid,)
            ).fetchone()["c"]
        assert count == 1


# ─────────────────────────────────────────────────────────────────────────────
# 5. 落库前全树去重（跨父同名标题）+ 断点续生持久化
# ─────────────────────────────────────────────────────────────────────────────


class TestOutlineSaveCrossParentDedupe:
    """BUG-FIX 目录生成：落库前去重的作用域。

    2026-10-11 修正：作用域由「全树」收窄为「同级」。
    旧实现为消除**不同章节下合法的同名子标题**而把它们整段删掉，导致落库后
    正文章节直接缺失；同时生成阶段又为这类「重复」追加「（其N）」式后缀，
    形成目录污染。现在：跨父同名全部保留，只有同一父节点下的真·重复才被剔除。
    """

    @staticmethod
    def _save(pid: int, outline: list) -> list:
        from app.routers.sse.sse_db_ops import _clear_and_save_outline

        return _clear_and_save_outline(pid, outline)

    def test_cross_parent_duplicate_titles_preserved(self):
        pid = _seed_project("跨父去重")
        outline = [
            {
                "title": "第一章 施工部署",
                "children": [{"title": "质量控制", "children": []}],
            },
            {
                "title": "第二章 安全管理",
                "children": [
                    {"title": "质量控制", "children": []},
                    {"title": "进度管理", "children": []},
                ],
            },
        ]
        saved = self._save(pid, outline)
        assert [n["title"] for n in saved] == ["施工部署", "安全管理"]
        # 第二个「质量控制」（不同父节点）是标书合法形态，必须保留
        assert [n["title"] for n in saved[0]["children"]] == ["质量控制"]
        assert [n["title"] for n in saved[1]["children"]] == ["质量控制", "进度管理"]

    def test_same_parent_duplicate_titles_still_deduped(self):
        pid = _seed_project("同级去重")
        outline = [
            {
                "title": "第一章 施工部署",
                "children": [
                    {"title": "质量控制", "children": []},
                    {"title": "质量控制", "children": []},
                ],
            },
        ]
        saved = self._save(pid, outline)
        assert [n["title"] for n in saved[0]["children"]] == ["质量控制"]


class TestDedupeOutlineTitles:
    """直接验证 _dedupe_outline_titles 的同级去重语义（不依赖落库）。"""

    def test_cross_parent_duplicates_preserved(self):
        from app.services.ai.workflows_outline_utils import _dedupe_outline_titles

        tree = [
            {"title": "A", "children": [{"title": "X", "children": []}]},
            {"title": "B", "children": [{"title": "X", "children": []}]},
        ]
        result = _dedupe_outline_titles(tree)
        assert [n["title"] for n in result] == ["A", "B"]
        # 跨父同名的 X 全部保留（不同章节下的合法重复，不能删内容）
        assert [c["title"] for c in result[0]["children"]] == ["X"]
        assert [c["title"] for c in result[1]["children"]] == ["X"]

    def test_same_parent_duplicate_removed(self):
        from app.services.ai.workflows_outline_utils import _dedupe_outline_titles

        tree = [
            {
                "title": "A",
                "children": [
                    {"title": "X", "children": []},
                    {"title": "X", "children": []},
                ],
            },
        ]
        result = _dedupe_outline_titles(tree)
        assert [c["title"] for c in result[0]["children"]] == ["X"]

    def test_same_parent_duplicate_with_legacy_suffix_removed(self):
        """「X」与「X（其2）」归一化后同名 —— 同级下应视为重复。"""
        from app.services.ai.workflows_outline_utils import _dedupe_outline_titles

        tree = [
            {
                "title": "A",
                "children": [
                    {"title": "砌体成品保护", "children": []},
                    {"title": "砌体成品保护（其2）", "children": []},
                ],
            },
        ]
        result = _dedupe_outline_titles(tree)
        assert [c["title"] for c in result[0]["children"]] == ["砌体成品保护"]


class TestV51ResumePersistence:
    """BUG-FIX 断点续生：full_outline 必须随状态持久化，否则 final_outline_json 恒为 []。"""

    def test_full_outline_persisted(self):
        from app.services.v51_state import (
            clear_v51_state_from_db,
            create_v51_state,
            load_v51_state_from_db,
            save_v51_state_to_db,
        )
        pid = _seed_project("断点续生持久化")
        st = create_v51_state(pid)
        st.full_outline = [{"title": "已生成章节", "children": [{"title": "子"}]}]
        st.summary = "摘要"
        assert save_v51_state_to_db(st)
        try:
            loaded = load_v51_state_from_db(pid)
            assert loaded is not None
            assert loaded.full_outline == st.full_outline
        finally:
            clear_v51_state_from_db(pid)


# ─────────────────────────────────────────────────────────────────────────────
# 4. 端点限流一致性
# ─────────────────────────────────────────────────────────────────────────────


class TestRateLimitCoverage:
    @staticmethod
    def _limited_section_paths() -> set:
        """找出所有挂了全局限流依赖的 sections 路由。

        两个实现细节:
        - `limit_per_endpoint` 是 `Depends(...)` 实例，FastAPI 会解包成 Dependant，
          所以比对的是最终的依赖函数而不是 Depends 对象；
        - 直接从 `sections.router` 读取而非 `app.routes`：当前 FastAPI 版本把
          include 进来的路由器包装成 `_IncludedRouter` 并延迟展开路由，遍历
          `app.routes` 拿不到各端点的 dependant。
        """
        from app.routers.rate_limiter import _common_rate_limit_func as limit_fn
        from app.routers.sections import router as sections_router

        found = set()
        for route in sections_router.routes:
            dependant = getattr(route, "dependant", None)
            for dep in getattr(dependant, "dependencies", None) or []:
                if getattr(dep, "call", None) is limit_fn:
                    found.add(getattr(route, "path", ""))
                    break
        return found

    def test_write_endpoints_have_rate_limit(self):
        """BUG-FIX 回归：历史上有三个写端点漏挂限流依赖"""
        limited = self._limited_section_paths()
        prefix = "/projects/{project_id}/sections"
        required = [
            prefix,  # 创建章节
            prefix + "/{section_id}",  # 删除章节
            prefix + "/{section_id}/move",
            prefix + "/reorder",
            prefix + "/review",
            prefix + "/review-with-autofix",
            prefix + "/generate-sse",
            prefix + "/expand-content",
        ]
        missing = [p for p in required if p not in limited]
        assert not missing, f"以下写端点缺少 limit_per_endpoint 依赖: {missing}"
