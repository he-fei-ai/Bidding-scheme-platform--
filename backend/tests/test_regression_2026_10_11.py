"""2026-10-11 二轮修复回归测试（I-8 / I-9 / O-6）

I-8  tasks/__init__.py:70 空文本跳过（与 save_upload 口径对齐）
I-9  routers/ai.py extract_global_facts 异常包裹（评分点已保存、事实提取失败不 500）
O-6  _save_nodes.saved_node.status 与 UPDATE 写入 DB 的 status 保持同源
"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


def _new_project(name: str) -> int:
    from app.db import get_db

    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO projects (name, bidder_name) VALUES (?, ?)", (name, "测试投标方")
        )
        return int(cur.lastrowid)


# ---------- I-8：tasks/__init__.py 空文本跳过 ----------

class TestI8AsyncParseSkipsEmptyTextFile:
    """BUG-FIX 2026-10-11（I-8）：异步解析任务原在 text_path.write_text
    处无条件写入 extracted_text，即使为空串也写出一个 0 字节 .txt 文件到
    磁盘，DB 里 extracted_text_path 又指向它 —— 下游 latest_extracted_text
    命中「存在的空文件」路径分支，但读到 0 字节，缓存 key 与 DB 指针一致
    却无实际内容，前端显示「解析成功」但下游 AI 拿到空上下文。
    save_upload（files.py:503-509）早已按 `if extracted_text.strip():` 跳过空文本，
    本任务此前遗漏，双链路口径漂移。"""

    def _run_task(self, provider_reply: str, tmp_path, monkeypatch, file_id: int, project_id: int, file_path: str):
        """用 monkeypatch 替换 extract_text_from_path，走完整的 parse_and_extract_file。"""
        import asyncio
        from app.services import files as files_mod

        monkeypatch.setattr(files_mod, "extract_text_from_path", lambda _p: provider_reply)
        # 用 monkeypatch 让任务写到的 texts/ 目录指向 tmp_path
        monkeypatch.setattr(files_mod, "PROJECTS_DATA_DIR", tmp_path)

        from app.tasks import parse_and_extract_file
        return parse_and_extract_file(
            file_path=file_path, project_id=project_id, file_id=file_id, create_task_row=False
        )

    def test_empty_text_does_not_write_file(self, tmp_path, monkeypatch):
        from app.services import files as files_mod
        from app.db import get_db

        pid = _new_project("I8-空文本")
        uploads_dir = tmp_path / "uploads"
        uploads_dir.mkdir()
        fake_file = uploads_dir / "空白.xlsx"
        fake_file.write_bytes(b"")

        # 造 uploaded_files 行
        with get_db() as conn:
            cur = conn.execute(
                "INSERT INTO uploaded_files (project_id, filename, path, file_type, "
                "file_role, status) VALUES (?, ?, ?, ?, ?, ?)",
                (pid, fake_file.name, str(fake_file), ".xlsx", "main", "processing"),
            )
            fid = int(cur.lastrowid)

        monkeypatch.setattr(files_mod, "extract_text_from_path", lambda _p: "")
        # tasks/__init__.py 内是 `from ..config import PROJECTS_DATA_DIR` 延迟导入，
        # 必须 patch config 模块本身（而非 services.files 的副本）才能生效。
        import app.config as cfg_mod
        monkeypatch.setattr(cfg_mod, "PROJECTS_DATA_DIR", tmp_path)
        from app.tasks import parse_and_extract_file

        res = parse_and_extract_file(
            file_path=str(fake_file), project_id=pid, file_id=fid, create_task_row=False
        )
        assert res["status"] == "success", "空文本仍应标为 success（不是提示串），仅跳过写盘"
        # 关键断言：磁盘上不得留下 0 字节 .txt
        txt_path = tmp_path / str(pid) / "texts" / f"{fake_file.name}.txt"
        assert not txt_path.exists(), "空文本不应写出 .txt 文件（与 save_upload 口径不一致）"
        # DB 仍应记录 extracted_text_path（save_upload 也是这么做的），但指向不存在的文件
        with get_db() as conn:
            row = conn.execute(
                "SELECT status, message, extracted_text_path FROM uploaded_files WHERE id = ?", (fid,)
            ).fetchone()
        assert row["status"] == "success"
        assert "empty" in (row["message"] or "").lower() or "空" in (row["message"] or "")

    def test_non_empty_text_writes_file(self, tmp_path, monkeypatch):
        from app.services import files as files_mod
        from app.db import get_db

        pid = _new_project("I8-非空文本")
        uploads_dir = tmp_path / "uploads"
        uploads_dir.mkdir()
        fake_file = uploads_dir / "正常.docx"
        fake_file.write_bytes(b"PK\x03\x04fake")

        with get_db() as conn:
            cur = conn.execute(
                "INSERT INTO uploaded_files (project_id, filename, path, file_type, "
                "file_role, status) VALUES (?, ?, ?, ?, ?, ?)",
                (pid, fake_file.name, str(fake_file), ".docx", "main", "processing"),
            )
            fid = int(cur.lastrowid)

        monkeypatch.setattr(files_mod, "extract_text_from_path", lambda _p: "真实正文内容")
        import app.config as cfg_mod
        monkeypatch.setattr(cfg_mod, "PROJECTS_DATA_DIR", tmp_path)
        from app.tasks import parse_and_extract_file

        res = parse_and_extract_file(
            file_path=str(fake_file), project_id=pid, file_id=fid, create_task_row=False
        )
        assert res["status"] == "success"
        txt_path = tmp_path / str(pid) / "texts" / f"{fake_file.name}.txt"
        assert txt_path.exists(), "非空文本仍应写出 .txt 文件"
        assert txt_path.read_text(encoding="utf-8") == "真实正文内容"


# ---------- I-9：extract_global_facts 异常包裹 ----------

class TestI9GlobalFactsFailureDoesNotThrow:
    """BUG-FIX 2026-10-11（I-9）：routers/ai.py 里评分流程的 extract_global_facts
    原在 try 之外调用。:364 已把 scoring_points_json 落库，一旦事实提取抛异常
    （provider 502 / 超时 / 网络错误），整段以 500 冒到前端，但评分点已保存 ——
    用户看到「失败」而 DB 里已有新评分点，下次点识别评分点会因缓存直接返回，
    用户误以为上次成功了但页面崩了。现异常时降级为 global_facts=[] 返回。"""

    def test_extract_global_facts_failure_is_captured_in_code(self):
        """源码层面守卫：extract_global_facts 必须位于 try 块内，
        且异常路径必须给 global_facts 兜底为空 list（保持响应结构一致）。"""
        src = (BACKEND_DIR / "app" / "routers" / "ai.py").read_text(encoding="utf-8")
        # 定位识别评分点端点内的关键片段
        idx = src.find("await asyncio.to_thread(_sync_insert_or_update, project_id, scoring_json, 'scoring_points_json')")
        assert idx != -1, "评分点落库语句消失"
        tail = src[idx: idx + 4000]
        # try 必须出现在评分点落库**之后**、extract_global_facts 调用之前
        try_idx = tail.find("try:")
        facts_idx = tail.find("extract_global_facts(")
        except_idx = tail.find("except Exception as _facts_e")
        assert try_idx != -1, "缺少包裹 extract_global_facts 的 try 块"
        assert facts_idx != -1, "extract_global_facts 调用消失"
        assert except_idx != -1, "缺少 except 分支（异常吞掉无兜底）"
        assert try_idx < facts_idx < except_idx, "顺序错乱：try → extract_global_facts → except"
        # 兜底必须给空 list，而不是抛给前端
        assert "global_facts = []" in tail[except_idx:except_idx + 400]


# ---------- O-6：saved_node.status 与 DB 写入的 status 同源 ----------

class TestO6SavedNodeStatusConsistentWithDb:
    """BUG-FIX 2026-10-11（O-6）：_save_nodes 里 INSERT 后回填正文的 UPDATE
    写入 DB 的 status = `_prev.get("status") or "completed"`，但 saved_node
    却固定写 "pending"。于是「重新生成目录后」返回给 SSE / 前端 / 上层 workflow
    的 saved_node 声称该章节「待生成」，而 DB 里该 section 是 "completed"。
    前端 collectLeafIds(filterPendingFailed=true) 只挑 pending/failed → 会选中
    这些实际已有正文的章节进入「补全生成」集合 → AI 覆盖旧正文。
    现抽出 _node_status 变量，UPDATE 与 saved_node 共用。"""

    def test_restored_content_status_is_completed(self):
        """正文被回填的节点：saved_node.status 必须与 DB 一致为 completed。"""
        from app.routers.sse import sse_db_ops
        from app.db import get_db

        pid = _new_project("O6-status对齐-回填")
        outline = [{"id": "1", "title": "施工部署", "children": []}]
        sse_db_ops._clear_and_save_outline(pid, outline)
        with get_db() as conn:
            sid = conn.execute(
                "SELECT id FROM sections WHERE project_id = ?", (pid,)
            ).fetchone()["id"]
            conn.execute(
                "UPDATE sections SET content = ?, status = 'completed' WHERE id = ?",
                ("已生成的正文段落", sid),
            )

        # 重建同名目录 → 触发回填
        saved2 = sse_db_ops._clear_and_save_outline(pid, outline)
        assert saved2[0]["has_content"] is True
        # 关键断言：saved_node.status 必须与 DB 一致
        assert saved2[0]["status"] == "completed", (
            f"回填正文的节点 saved_node.status 应为 completed，实际 {saved2[0]['status']}"
        )
        with get_db() as conn:
            row = conn.execute(
                "SELECT status, content FROM sections WHERE id = ?", (saved2[0]["id"],)
            ).fetchone()
        assert row["status"] == saved2[0]["status"], "saved_node.status 与 DB status 不一致"
        assert row["content"] == "已生成的正文段落"

    def test_fresh_node_status_is_pending(self):
        """全新节点（无旧正文可回填）：saved_node.status 必须保持 pending。"""
        from app.routers.sse import sse_db_ops
        from app.db import get_db

        pid = _new_project("O6-status对齐-全新")
        saved = sse_db_ops._clear_and_save_outline(
            pid, [{"id": "1", "title": "全新章节", "children": []}]
        )
        assert saved[0]["has_content"] is False
        assert saved[0]["status"] == "pending"
        with get_db() as conn:
            row = conn.execute(
                "SELECT status FROM sections WHERE id = ?", (saved[0]["id"],)
            ).fetchone()
        assert row["status"] == "pending" == saved[0]["status"]

    def test_legacy_status_in_preserved_content_is_propagated(self):
        """旧章节的 status 若非 completed（如 generating/failed），回填时应当
        保留该状态而不是硬编码为 completed —— 这是原 UPDATE 分支
        `_prev.get("status") or "completed"` 的语义，现应继续在 saved_node 里体现。"""
        from app.routers.sse import sse_db_ops
        from app.db import get_db

        pid = _new_project("O6-status对齐-legacy")
        outline = [{"id": "1", "title": "施工部署", "children": []}]
        sse_db_ops._clear_and_save_outline(pid, outline)
        with get_db() as conn:
            sid = conn.execute(
                "SELECT id FROM sections WHERE project_id = ?", (pid,)
            ).fetchone()["id"]
            conn.execute(
                "UPDATE sections SET content = ?, status = 'failed' WHERE id = ?",
                ("上一次失败的正文", sid),
            )

        saved2 = sse_db_ops._clear_and_save_outline(pid, outline)
        assert saved2[0]["status"] == "failed", (
            f"应沿用旧章节的 'failed' 状态，实际 {saved2[0]['status']}"
        )
        with get_db() as conn:
            row = conn.execute(
                "SELECT status FROM sections WHERE id = ?", (saved2[0]["id"],)
            ).fetchone()
        assert row["status"] == "failed" == saved2[0]["status"]


# ---------- O-7：V5.1 resume_v51_task 层级深度校验 ----------

class TestO7ResumePhaseDepthValidation:
    """BUG-FIX 2026-10-11（O-7）：`resume_v51_task` 此前无条件接受
    `edited_outline`。用户在 L1 审核窗口提交 L3 完整树（例如从浏览器工具改
    payload，或前端刷新后缓存了旧 outline）时，工作流后续
    `_generate_level2_v51` → `_build_level2_skeleton`
    （`workflows_outline_v51.py:2107`）会按 L1 标题完全重建 L2+L3 骨架，
    **静默丢弃用户精心编辑的 children**。现按 `state.current_phase` 校验
    outline max_depth，不匹配拒绝写入并透出 `_last_reject_reason`，
    路由层据此返回 400（层级不匹配）而非笼统的 409。"""

    # ---- 辅助：创建带 phase 的 V51TaskState，测试结束自动清理 ----
    @staticmethod
    def _state_with_phase(phase: str, pid: int = 990_001):
        from app.services import v51_state as vs
        state = vs.create_v51_state(pid)
        state.current_phase = phase
        return state, pid

    def _cleanup(self, pid: int):
        from app.services import v51_state as vs
        vs.remove_v51_state(pid)

    # ---- 1. _outline_max_depth 辅助函数 ----

    def test_outline_max_depth_empty_and_invalid(self):
        """空/非 list/纯 dict 都应返回 0 而非崩溃。"""
        from app.services.v51_state import _outline_max_depth

        assert _outline_max_depth([]) == 0
        assert _outline_max_depth(None) == 0
        assert _outline_max_depth("not-a-list") == 0
        assert _outline_max_depth(42) == 0
        assert _outline_max_depth({"title": "x"}) == 0  # 非 list 也算 0

    def test_outline_max_depth_counts_levels(self):
        """1 层 / 2 层 / 3 层结构各自正确计数，空 children 不额外计深。"""
        from app.services.v51_state import _outline_max_depth

        l1 = [{"title": "A"}, {"title": "B"}]
        assert _outline_max_depth(l1) == 1

        l2 = [{"title": "A", "children": [{"title": "A.1"}, {"title": "A.2"}]}]
        assert _outline_max_depth(l2) == 2

        l3 = [{
            "title": "A",
            "children": [{
                "title": "A.1",
                "children": [{"title": "A.1.1"}],
            }],
        }]
        assert _outline_max_depth(l3) == 3

        # 空 children 列表不额外加深
        l1_with_empty_children = [{"title": "A", "children": []}]
        assert _outline_max_depth(l1_with_empty_children) == 1

    # ---- 2. 各 phase 深度校验（正例 + 反例） ----

    def test_l1_phase_rejects_l3_outline(self):
        """L1 审核窗口拒绝 L3 完整树，并保留原 edited_outline 不变。"""
        state, pid = self._state_with_phase("level1", pid=990_011)
        try:
            from app.services import v51_state as vs

            l3_tree = [{
                "title": "工程概况",
                "children": [{
                    "title": "项目简介",
                    "children": [{"title": "项目背景"}],
                }],
            }]
            ok = vs.resume_v51_task(pid, l3_tree)
            assert ok is False, "L1 阶段不应接受 L3 完整树"
            assert vs.get_v51_last_reject_reason(pid) == "depth_mismatch"
            assert state.edited_outline is None, "拒绝后不得写入 edited_outline"
        finally:
            self._cleanup(pid)

    def test_l1_phase_accepts_l1_only_outline(self):
        """L1 审核窗口接受 depth<=1 的一级目录。"""
        state, pid = self._state_with_phase("level1", pid=990_012)
        try:
            from app.services import v51_state as vs

            l1_tree = [{"title": "工程概况"}, {"title": "施工方案"}]
            ok = vs.resume_v51_task(pid, l1_tree)
            assert ok is True
            assert state.edited_outline == l1_tree
            assert vs.get_v51_last_reject_reason(pid) is None, "成功后应清空 _last_reject_reason"
            # version 递增
            assert state._confirm_version == 1
        finally:
            self._cleanup(pid)

    def test_l2_phase_accepts_l2_outline(self):
        """L2 审核窗口接受 depth<=2 的目录。"""
        state, pid = self._state_with_phase("level2", pid=990_013)
        try:
            from app.services import v51_state as vs

            l2_tree = [{
                "title": "工程概况",
                "children": [{"title": "项目简介"}, {"title": "建设规模"}],
            }]
            ok = vs.resume_v51_task(pid, l2_tree)
            assert ok is True
            assert state.edited_outline == l2_tree
        finally:
            self._cleanup(pid)

    def test_l2_phase_rejects_l3_outline(self):
        """L2 审核窗口拒绝 L3 完整树（避免误以为已审核 L3）。"""
        state, pid = self._state_with_phase("level2", pid=990_014)
        try:
            from app.services import v51_state as vs

            l3_tree = [{
                "title": "工程概况",
                "children": [{
                    "title": "项目简介",
                    "children": [{"title": "项目背景"}],
                }],
            }]
            assert vs.resume_v51_task(pid, l3_tree) is False
            assert vs.get_v51_last_reject_reason(pid) == "depth_mismatch"
            assert state.edited_outline is None
        finally:
            self._cleanup(pid)

    def test_l3_phase_accepts_full_tree(self):
        """L3 审核窗口接受完整 L3 树。"""
        state, pid = self._state_with_phase("level3", pid=990_015)
        try:
            from app.services import v51_state as vs

            l3_tree = [{
                "title": "工程概况",
                "children": [{
                    "title": "项目简介",
                    "children": [{"title": "项目背景"}],
                }],
            }]
            ok = vs.resume_v51_task(pid, l3_tree)
            assert ok is True
            assert state.edited_outline == l3_tree
        finally:
            self._cleanup(pid)

    def test_final_phase_accepts_any_depth(self):
        """final 阶段无深度上限，接受空树与完整树。"""
        state, pid = self._state_with_phase("final", pid=990_016)
        try:
            from app.services import v51_state as vs

            # 空树
            assert vs.resume_v51_task(pid, []) is True
            assert state.edited_outline == []
            # 深度 5 也接受（final 无上限）
            deep_tree = [{
                "title": "L1",
                "children": [{
                    "title": "L2",
                    "children": [{
                        "title": "L3",
                        "children": [{
                            "title": "L4",
                            "children": [{"title": "L5"}],
                        }],
                    }],
                }],
            }]
            assert vs.resume_v51_task(pid, deep_tree) is True
            assert state.edited_outline == deep_tree
        finally:
            self._cleanup(pid)

    # ---- 3. 兼容性 & 边界 ----

    def test_edited_outline_none_keeps_old_behavior(self):
        """`edited_outline=None` 时不做深度校验，保持旧行为（纯确认不编辑）。"""
        state, pid = self._state_with_phase("level1", pid=990_017)
        try:
            from app.services import v51_state as vs

            # 预置一个 L2 树
            state.edited_outline = [{"title": "A", "children": [{"title": "A.1"}]}]
            ok = vs.resume_v51_task(pid, edited_outline=None)
            assert ok is True
            # 预置的 edited_outline 不应被清除
            assert state.edited_outline == [{"title": "A", "children": [{"title": "A.1"}]}]
            assert state._confirm_version == 1
        finally:
            self._cleanup(pid)

    def test_invalid_outline_type_returns_400_reason(self):
        """edited_outline 不是 list 时返回 False 且 reason='invalid_outline'。"""
        state, pid = self._state_with_phase("level1", pid=990_018)
        try:
            from app.services import v51_state as vs

            ok = vs.resume_v51_task(pid, {"title": "wrong shape"})
            assert ok is False
            assert vs.get_v51_last_reject_reason(pid) == "invalid_outline"
        finally:
            self._cleanup(pid)

    def test_version_conflict_still_returns_409_reason(self):
        """乐观锁冲突路径不变，reason='version_conflict'。"""
        state, pid = self._state_with_phase("level1", pid=990_019)
        try:
            from app.services import v51_state as vs

            # 先成功一次，version 递增到 1
            assert vs.resume_v51_task(pid, [{"title": "A"}]) is True
            assert state._confirm_version == 1
            # 用过期 version=0 尝试 resume，应触发冲突
            ok = vs.resume_v51_task(pid, [{"title": "B"}], expected_version=0)
            assert ok is False
            assert vs.get_v51_last_reject_reason(pid) == "version_conflict"
            # 冲突时 edited_outline 不应被覆盖
            assert state.edited_outline == [{"title": "A"}]
        finally:
            self._cleanup(pid)

    def test_success_clears_previous_reject_reason(self):
        """本次成功后 _last_reject_reason 必须清空，否则路由层会误判为失败。"""
        state, pid = self._state_with_phase("level1", pid=990_020)
        try:
            from app.services import v51_state as vs

            # 先制造一次拒绝
            vs.resume_v51_task(pid, [
                {"title": "A", "children": [{"title": "A.1", "children": [{"title": "A.1.1"}]}]},
            ])
            assert vs.get_v51_last_reject_reason(pid) == "depth_mismatch"
            # 然后正常提交
            ok = vs.resume_v51_task(pid, [{"title": "A"}])
            assert ok is True
            # 关键断言：成功清空残留
            assert vs.get_v51_last_reject_reason(pid) is None
        finally:
            self._cleanup(pid)

    def test_no_state_returns_false_and_reason_no_state(self):
        """状态不存在时返回 False 且 reason='no_state'。"""
        from app.services import v51_state as vs

        nonexistent_pid = 999_999_999
        assert vs.resume_v51_task(nonexistent_pid, [{"title": "A"}]) is False
        assert vs.get_v51_last_reject_reason(nonexistent_pid) == "no_state"
        assert vs.get_v51_confirm_version(nonexistent_pid) is None
        assert vs.get_v51_phase(nonexistent_pid) is None

    # ---- 4. 路由层拒绝原因码 → HTTP 状态码映射（源码守卫） ----

    def test_v51_confirm_route_maps_reasons_to_http_codes(self):
        """sections.py::v51_confirm_route 必须按 _last_reject_reason 区分 409/400，
        让用户看到可操作提示而不是笼统的"冲突"。"""
        src = (BACKEND_DIR / "app" / "routers" / "sections.py").read_text(encoding="utf-8")
        # 定位 v51_confirm_route 内的拒绝分支
        idx = src.find("@router.post('/v51-confirm'")
        assert idx != -1, "v51-confirm 端点消失"
        tail = src[idx: idx + 3000]
        # 必须包含 depth_mismatch → 400 分支（代码里可能用单引号或双引号）
        assert "depth_mismatch" in tail, "缺少 depth_mismatch 分支"
        assert "status_code=400" in tail, "缺少 400 Bad Request"
        # 必须包含 invalid_outline → 400 分支
        assert "invalid_outline" in tail, "缺少 invalid_outline 分支"
        # 必须保留 409 Conflict 兜底
        assert "status_code=409" in tail, "409 Conflict 兜底分支消失"
        # 必须调用 get_v51_last_reject_reason 分流
        assert "get_v51_last_reject_reason" in tail
