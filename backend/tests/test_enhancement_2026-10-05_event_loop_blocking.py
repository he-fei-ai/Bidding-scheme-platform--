"""回归测试（2026-10-05）：事件循环阻塞端点收敛 + 目录树轻量投影

覆盖的性能瓶颈（详见 analysis_reports/性能瓶颈分析与优化方案_2026-10-05.md）：
- P-1  一批 `async def` 端点在事件循环线程内直接做同步 SQLite I/O：
       纯读查询误用 `get_db()`（无 readonly）抢全局写锁（超时上限 60s），
       正文生成高峰期锁争用会冻结整个事件循环，拖死并发 SSE 心跳与其他请求。
       修复口径：纯读改 `get_db(readonly=True)` + `asyncio.to_thread`；
       真实写入整段派发 `to_thread`（锁在 worker 线程阻塞，loop 不受影响）。
       权限门 require_project_permission / _ensure_project_access 同口径收敛。
- P-2  services/sections.py::build_tree 缓存 miss 时走 list_sections
       （SELECT * 含 MB 级正文）并逐行 count_words。改为列投影查询
       （不传输 content/ai_prompt_snapshot），has_content 用 length(content)
       在 SQL 侧判定；仅对「持久化 word_count=0 且正文非空」的存量脏行
       回读正文重算并惰性回填自愈。

测试原则：只测行为（正确性 + 规模化护栏 + 静态护栏），不测实现细节、
不与具体 SQL 文本耦合，避免掩盖逻辑缺陷。
"""
from __future__ import annotations

import ast
import asyncio
import sqlite3
import time
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.config import DATABASE_PATH
from app.db import get_db
from app.models import ProjectCreate, SectionCreate, SectionUpdate
from app.services import sections as sections_service
from app.services.projects import create_project
from app.utils import count_words

_ROUTERS_DIR = Path(sections_service.__file__).parent.parent / "routers"

# 本文件创建的项目 id，teardown 用「关外键的独立连接」直接删除，
# 避免 ON DELETE CASCADE 逐层递归污染同库后续测试（口径同 09-16 性能回归）。
_created_project_ids: list[int] = []


@pytest.fixture(autouse=True)
def _cleanup_created_projects():
    yield
    if not _created_project_ids:
        return
    conn = sqlite3.connect(str(DATABASE_PATH))
    try:
        conn.execute("PRAGMA foreign_keys=OFF")
        placeholders = ",".join("?" for _ in _created_project_ids)
        conn.execute(f"DELETE FROM sections WHERE project_id IN ({placeholders})", _created_project_ids)
        conn.execute(f"DELETE FROM projects WHERE id IN ({placeholders})", _created_project_ids)
        conn.commit()
    finally:
        conn.close()
        _created_project_ids.clear()


def _make_project(name: str = "event-loop 回归测试项目") -> int:
    pid = create_project(ProjectCreate(name=name)).id
    _created_project_ids.append(pid)
    return pid


def _add(pid: int, title: str, parent_id=None, level=1, sort_order=0) -> int:
    return sections_service.create_section(
        pid, SectionCreate(title=title, parent_id=parent_id, level=level, sort_order=sort_order)
    ).id


def _raw_update(section_id: int, **cols) -> None:
    """绕过服务层 word_count 维护直写列，构造「存量脏数据」场景（P-2 用）。"""
    sets = ", ".join(f"{k} = ?" for k in cols)
    with get_db() as conn:
        conn.execute(f"UPDATE sections SET {sets} WHERE id = ?", (*cols.values(), section_id))


def _flatten(nodes) -> dict[int, object]:
    out: dict[int, object] = {}
    stack = list(nodes)
    while stack:
        n = stack.pop()
        out[n.id] = n
        stack.extend(n.children)
    return out


# ============================================================================
# A. P-1 静态护栏：async 端点不得在事件循环内裸抢写锁 / 做同步 DB I/O
# ============================================================================

# 附 B 「本轮未动、留后续」端点白名单。
# 一旦这些端点被收敛，应从白名单移除；新登记的阻塞端点必须在此显式说明理由。
# 判据仅命中「async 端点直接体内裸抢 get_db()（无 readonly）」——ai_config 中
# 纯读的 GET 端点（get_ai_config / get_text_provider_config 等）用 readonly=True
# 或经 to_thread 派发，本就不在违规集，故不入白名单。
# 收敛进度：ai_config 8 个写路径端点（to_thread+readonly）与 images.generate_image
# （服务自管短事务重构）均已修复，白名单清空——护栏收紧为「全库零阻塞端点」。
_P1_DEFERRED_ALLOWLIST: set[tuple[str, str]] = set()


def _iter_direct_calls(node):
    """遍历 node 子树，但**不下钻进嵌套的函数/异步函数/Lambda 体**。

    被 `asyncio.to_thread` 派发的同步 I/O 都收敛在嵌套 `def _read()/_write()` 里，
    不在事件循环线程执行；只有直接位于 async 端点函数体（含 with/for/if 等
    复合语句）内的 `get_db()` 才是「在 loop 内做同步 I/O」。
    """
    stack = list(ast.iter_child_nodes(node))
    while stack:
        cur = stack.pop()
        if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue  # 嵌套函数体不属本层事件循环，不纳入扫描
        yield cur
        stack.extend(ast.iter_child_nodes(cur))


def _collect_blocking_endpoints() -> list[tuple[str, str, int]]:
    """扫描全部 router 模块，返回「async 端点直接体内裸用 get_db（无 readonly）」的清单。

    判据（与修复口径一致）：
    - 端点为 `async def` 且带 `@router.*` 装饰器；
    - 直接函数体（排除经 to_thread 派发的嵌套同步 helper）内存在
      `get_db()`/`get_db_with_retry()` 调用且**不带 readonly=True**（抢全局写锁，最危险）。
    """
    violations: list[tuple[str, str, int]] = []
    for path in sorted(_ROUTERS_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.AsyncFunctionDef):
                continue
            decs = [ast.unparse(d) for d in node.decorator_list]
            if not any(d.startswith("router.") for d in decs):
                continue
            for call in _iter_direct_calls(node):
                if not isinstance(call, ast.Call):
                    continue
                fn = call.func
                name = getattr(fn, "id", None) or getattr(fn, "attr", None)
                if name not in ("get_db", "get_db_with_retry"):
                    continue
                has_readonly = any(kw.arg == "readonly" and getattr(kw.value, "value", False) is True for kw in call.keywords)
                if not has_readonly:
                    violations.append((path.name, node.name, node.lineno))
                    break
    return violations


class TestEventLoopBlockingGuardrail:
    def test_no_unhandled_blocking_async_endpoints(self):
        """除附 B 白名单外，不得存在事件循环内裸抢写锁的 async 端点。"""
        violations = _collect_blocking_endpoints()
        remaining = {(f, n) for (f, n, _ln) in violations}
        unexpected = remaining - {k for k in _P1_DEFERRED_ALLOWLIST}
        assert not unexpected, f"以下 async 端点在事件循环内裸用 get_db（无 readonly/to_thread），应收敛：{sorted(unexpected)}"

    def test_allowlist_endpoints_are_actually_fixed_or_declared(self):
        """白名单项必须与实际违规集合对应：被修复的端点应从白名单移除（防白名单腐烂）。"""
        violations = {(f, n) for (f, n, _ln) in _collect_blocking_endpoints()}
        stale = {k for k in _P1_DEFERRED_ALLOWLIST} - violations
        assert not stale, f"以下端点已收敛但仍留在白名单，请移除：{sorted(stale)}"

    @pytest.mark.parametrize("module,endpoint", [
        ("consistency.py", "get_conflicts"),
        ("consistency.py", "get_audit_summary"),
        ("content_version.py", "list_content_versions"),
        ("content_version.py", "get_content_version"),
        ("content_version.py", "save_content_version"),
        ("content_version.py", "restore_content_version"),
        ("content_version.py", "delete_content_version"),
        ("images.py", "list_image_tasks"),
        ("images.py", "get_image_task"),
        ("images.py", "get_image_file"),
        ("knowledge.py", "get_stats"),
        ("prompts.py", "update_prompt"),
        ("prompts.py", "reset_prompt"),
        ("prompts.py", "rollback_prompt"),
        ("templates.py", "list_templates"),
        ("templates.py", "delete_template"),
        ("templates.py", "audit_template_coverage"),
        ("export.py", "download_export"),
        ("export.py", "export_pdf"),
        ("ai_config.py", "save_ai_config"),
        ("ai_config.py", "quick_setup"),
        ("ai_config.py", "save_image_model_config"),
        ("ai_config.py", "clear_provider_history"),
        ("ai_config.py", "save_text_provider_config"),
        ("ai_config.py", "activate_text_provider_config"),
        ("ai_config.py", "save_image_provider_config"),
        ("ai_config.py", "activate_image_provider_config"),
        ("images.py", "generate_image"),
    ])
    def test_fixed_endpoints_no_longer_blocking(self, module, endpoint):
        """本轮修复的端点必须已脱离阻塞清单。"""
        violations = {(f, n) for (f, n, _ln) in _collect_blocking_endpoints()}
        assert (module, endpoint) not in violations, f"{module}::{endpoint} 仍在事件循环内裸抢写锁"


# ============================================================================
# B. P-2 build_tree 轻量投影：正确性等价 + 脏数据自愈 + 规模化护栏
# ============================================================================


class TestBuildTreeProjection:
    def test_node_fields_match_full_load_reference(self):
        """轻量投影结果须与「全量 list_sections 手工构建」逐字段一致（含 chart_flags）。"""
        pid = _make_project()
        root = _add(pid, "第一章", sort_order=1)
        c1 = _add(pid, "有图有文", parent_id=root, level=2, sort_order=1)
        _raw_update(c1, content="正文一二三四字", flowchart_json='{"a":1}', gantt_json="", word_count=6)
        c2 = _add(pid, "空章节", parent_id=root, level=2, sort_order=2)
        c3 = _add(pid, "仅架构图", parent_id=root, level=2, sort_order=3)
        _raw_update(c3, architecture_json='{"nodes":[]}', labor_json='{"x":1}', word_count=0)
        sections_service._invalidate_build_tree_cache(pid)

        nodes = _flatten(sections_service.build_tree(pid))
        assert nodes[c1].has_content is True
        assert set(nodes[c1].chart_flags) == {"flowchart"}  # gantt_json 为空串不算
        assert nodes[c2].has_content is False
        assert nodes[c2].word_count == 0
        assert set(nodes[c3].chart_flags) == {"architecture", "labor"}
        assert nodes[c3].has_content is False  # 只有 JSON，无正文

    def test_dirty_word_count_recomputed_and_self_healed(self):
        """存量脏行（word_count=0 但正文非空）：树节点字数即时重算，且回写 DB 自愈。"""
        pid = _make_project()
        sid = _add(pid, "脏字数章节", sort_order=1)
        content = "这是一段用于验证惰性回填的中文正文内容"
        _raw_update(sid, content=content, word_count=0)  # 模拟历史/chunk 直写遗留
        expected = count_words(content)
        assert expected > 0
        sections_service._invalidate_build_tree_cache(pid)

        nodes = _flatten(sections_service.build_tree(pid))
        assert nodes[sid].word_count == expected, "脏行应按正文重算而非返回陈旧的 0"

        # 自愈：DB 持久化列已被回填为正确值，下次重建直接命中
        with get_db(readonly=True) as conn:
            healed = conn.execute("SELECT word_count FROM sections WHERE id = ?", (sid,)).fetchone()["word_count"]
        assert healed == expected, "word_count 惰性回填未落库"

    def test_clean_word_count_not_recounted(self):
        """干净行（持久化 word_count 与实际一致）不被无谓回读改写。"""
        pid = _make_project()
        sid = _add(pid, "已维护字数", sort_order=1)
        content = "已经通过正常写入口维护字数的正文"
        sections_service.update_section(sid, SectionUpdate(content=content))
        good = count_words(content)
        with get_db(readonly=True) as conn:
            assert conn.execute("SELECT word_count FROM sections WHERE id = ?", (sid,)).fetchone()["word_count"] == good
        sections_service._invalidate_build_tree_cache(pid)
        nodes = _flatten(sections_service.build_tree(pid))
        assert nodes[sid].word_count == good

    def test_tree_shape_and_ordering_preserved(self):
        """父子挂载与兄弟排序（sort_order, created_at）与旧实现一致。"""
        pid = _make_project()
        root = _add(pid, "根", sort_order=1)
        b = _add(pid, "乙", parent_id=root, level=2, sort_order=2)
        a = _add(pid, "甲", parent_id=root, level=2, sort_order=1)
        sections_service._invalidate_build_tree_cache(pid)

        tree = sections_service.build_tree(pid)
        assert len(tree) == 1 and tree[0].id == root
        assert [ch.id for ch in tree[0].children] == [a, b]  # 按 sort_order 升序

    def test_large_content_project_builds_fast(self):
        """规模化护栏：多章节大正文项目树重建耗时有限（不再逐行 count_words 全量正文）。"""
        pid = _make_project()
        root = _add(pid, "大项目根", sort_order=1)
        big = "标" * 50
        for i in range(1, 61):
            sid = _add(pid, f"章节{i}", parent_id=root, level=2, sort_order=i)
            # 干净行（word_count 已维护），投影查询根本不读取 content 列
            _raw_update(sid, content=big * 100, word_count=count_words(big * 100))
        sections_service._invalidate_build_tree_cache(pid)

        t0 = time.perf_counter()
        tree = sections_service.build_tree(pid)
        elapsed = time.perf_counter() - t0
        assert len(_flatten(tree)) == 61
        # 干净路径不传输/不扫描正文，60 节 × 5000 字应在极短时间内完成
        assert elapsed < 1.5, f"build_tree 耗时 {elapsed:.3f}s，疑似退化回全量加载正文"


# ============================================================================
# C. P-1c 权限门：to_thread 收敛后鉴权语义不变
# ============================================================================

OWNER = 900101
STRANGER = 900102


def _new_project_with_owner(created_by: int | None) -> int:
    init_conn_pid = create_project(ProjectCreate(name="权限门项目")).id
    with get_db() as conn:
        conn.execute("UPDATE projects SET created_by = ? WHERE id = ?", (created_by, init_conn_pid))
    _created_project_ids.append(init_conn_pid)
    return init_conn_pid


class TestPermissionGateSemantics:
    def test_owner_passes(self):
        from app.routers.sections import require_project_permission
        from app.security.auth import User

        pid = _new_project_with_owner(OWNER)
        # 不抛异常即放行
        asyncio.run(require_project_permission(pid, User(OWNER, "owner", "user")))

    def test_stranger_403(self):
        from app.routers.sections import require_project_permission
        from app.security.auth import User

        pid = _new_project_with_owner(OWNER)
        with pytest.raises(HTTPException) as exc:
            asyncio.run(require_project_permission(pid, User(STRANGER, "s", "user")))
        assert exc.value.status_code == 403

    def test_missing_project_404(self):
        from app.routers.sections import require_project_permission
        from app.security.auth import User

        with pytest.raises(HTTPException) as exc:
            asyncio.run(require_project_permission(999999, User(OWNER, "owner", "user")))
        assert exc.value.status_code == 404

    def test_legacy_null_owner_passes(self):
        from app.routers.sections import require_project_permission
        from app.security.auth import User

        pid = _new_project_with_owner(None)
        asyncio.run(require_project_permission(pid, User(STRANGER, "s", "user")))

    def test_enforce_project_access_stranger_403(self):
        """project_access.enforce_project_access 复用同一权限门，语义一致。"""
        from app.routers.project_access import enforce_project_access

        pid = _new_project_with_owner(OWNER)
        with pytest.raises(HTTPException) as exc:
            asyncio.run(enforce_project_access(pid, {"id": STRANGER, "username": "s", "role": "user"}))
        assert exc.value.status_code == 403


# ============================================================================
# D. images.generate_image 结构性缺陷修复：外部 HTTP 生成期间不得持有 DB 会话/写锁
#    （旧实现：路由 `with get_db()` 包裹整段 await，全局写锁跨秒~分钟级 HTTP
#    全程持有，冻结所有写请求。修复后服务自管短事务，写锁仅在写点的
#    worker 线程内短暂获取。）
# ============================================================================

_IMAGE_CFG_MIN = {
    "image_provider": "agnes", "image_model": "agnes-image-2.1-flash",
    "image_base_url": "https://api.agnes-ai.cn/v1", "image_api_key_enc": "",
    "image_max_concurrency": 2, "image_max_per_project": 10, "image_quality": "standard",
    "image_style_injection": None, "image_enabled": True,
    "image_default_size": "2K", "image_default_ratio": "1:1", "image_price_per_image": 0.0,
}


class _SessionTracker:
    """记录 get_db() 短会话的并发活跃数，用于验证生成链路不跨 HTTP 持锁。"""

    def __init__(self):
        self.active = 0
        self.max_active = 0
        self.writes: list[tuple[str, object]] = []
        self.snapshot_during_gen: int | None = None

    def _open(self):
        tracker = self

        class _Conn:
            def __enter__(self):
                tracker.active += 1
                tracker.max_active = max(tracker.max_active, tracker.active)
                return self

            def __exit__(self, *exc):
                tracker.active -= 1
                return False

            def execute(self, sql, params=None):
                tracker.writes.append((sql, params))
                return None

            def commit(self):
                pass

        return _Conn()


class _ProbeAdapter:
    """假适配器：在 generate（模拟外部 HTTP）被 await 的当下快照活跃会话数。"""

    default_model = "probe-model"

    def __init__(self, config, tracker):
        self.config = config
        self.tracker = tracker

    async def generate(self, prompt, model=None, size=None, ratio=None, reference_images=None, **kw):
        self.tracker.snapshot_during_gen = self.tracker.active
        return {
            "success": True, "image_url": "https://img.example.com/gen.png",
            "image_base64": None, "cost": 0.1, "provider": "agnes",
            "model": model or "probe-model", "error": None,
        }


def test_generate_image_holds_no_db_session_across_http():
    """行为不变式：外部 HTTP 生成期间无任何 DB 会话被持有；写为短事务且无泄漏。"""
    import unittest.mock as m
    import app.services.ai.image_providers.service as svc_mod

    tracker = _SessionTracker()

    async def _run():
        with m.patch("app.db.get_db", lambda *a, **k: tracker._open()), \
             m.patch.object(svc_mod, "load_image_config", return_value=dict(_IMAGE_CFG_MIN)), \
             m.patch.object(svc_mod.ProviderRegistry, "_adapters", {"agnes": lambda c: _ProbeAdapter(c, tracker)}), \
             m.patch.object(svc_mod.ImageGenerationService, "_download_image", new=m.AsyncMock(return_value="/tmp/gen.png")):
            service = svc_mod.ImageGenerationService()  # db_conn=None → 自管短事务
            return await service.generate({"prompt": "测试配图", "project_id": "1"}, 1)

    result = asyncio.run(_run())
    assert result["success"] is True, result
    assert tracker.snapshot_during_gen == 0, "外部 HTTP 生成期间不应持有 DB 会话/写锁（旧缺陷根因）"
    assert any("INSERT" in s.upper() and "image_generation_tasks" in s for s, _ in tracker.writes), "任务应持久化"
    assert tracker.active == 0, "短会话应全部关闭，无连接/锁泄漏"
    assert tracker.max_active <= 1, "任一时刻至多一个写会话（串行短事务，不跨 await 持锁）"


def test_generate_image_injected_conn_still_supported():
    """向后兼容：显式注入连接时走直接写路径（单测 FakeDB 契约，不依赖自管会话）。"""
    import unittest.mock as m
    import app.services.ai.image_providers.service as svc_mod

    writes: list[tuple[str, object]] = []

    class _Injected:
        def execute(self, sql, params=None):
            writes.append((sql, params))
            return None

        def commit(self):
            pass

    throwaway = _SessionTracker()

    async def _run():
        with m.patch.object(svc_mod, "load_image_config", return_value=dict(_IMAGE_CFG_MIN)), \
             m.patch.object(svc_mod.ProviderRegistry, "_adapters", {"agnes": lambda c: _ProbeAdapter(c, throwaway)}), \
             m.patch.object(svc_mod.ImageGenerationService, "_download_image", new=m.AsyncMock(return_value=None)):
            service = svc_mod.ImageGenerationService(_Injected(), config_service=None)
            return await service.generate({"prompt": "x", "project_id": "1"}, 1)

    result = asyncio.run(_run())
    assert result["success"] is True, result
    assert any("INSERT" in s.upper() and "image_generation_tasks" in s for s, _ in writes), "注入连接应被直接写入"
