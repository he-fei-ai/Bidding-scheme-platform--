"""七模块跨模块贯通与前后端同步专项测试（2026-10-04 跨模块轮）。

覆盖本轮跨模块探索确认的链路契约：
  1. 目录生成 outline_mode（易标规格二.2 直出/分步）：
     后端 Query 参数 ↔ 前端 URL 透传 ↔ sse_outline 形参三层一致（前后端同步）；
     should_use_direct_outline 判别口径（auto/direct/stepwise/长短时/包装形态）。
  2. 全局事实 → 正文生成按章筛选（易标规格三.4 / P-5）：
     select_facts_for_section 基线必含 + 相关性排序 + 条数截断；
     workflows_sections 侧接线为真实 import（非被 except 吞掉的死路径）。
  3. 上传解析 PDF 表格感知提取（易标规格一.1 / P-2）：
     开关语义与 pdfplumber 不可用时的 pypdf 回退（向后兼容）。
  4. 上传解析 → 提取项目数据链：
     GBK 编码 txt 自动降级解码；file_role 手工切换后合并文本主文件选择即时生效
     （缓存失效，无脏读）；REVIEW-FIX 2026-10-04 边界钉：上传自动判角色同样
     受 main 互斥约束（M1）、唯一 main 不可降级为 0 main（M2）、主源换出时
     透出 needs_reanalysis（m2）。

所有测试不依赖真实 AI / 网络，使用独立项目与唯一文件名保证可重复运行。
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from app.db import get_db, init_db
from app.services import files as file_service
from app.services.projects import create_project
from app.models import ProjectCreate


@pytest.fixture(autouse=True)
def _init_database():
    init_db()


def _mk_project(name: str) -> int:
    return create_project(ProjectCreate(name=name, industry="市政工程")).id


def _cleanup_project(pid: int) -> None:
    from app.services.projects import delete_project

    delete_project(pid)


def _frontend_file(rel: str) -> Path:
    """frontend 仓内文件（测试从 backend/tests 或 backend 目录运行均可定位）"""
    base = Path(__file__).resolve().parent
    for _up in range(4):
        cand = base.parents[_up] / "frontend" / "src" / rel
        if cand.exists():
            return cand
    raise pytest.skip(f"未找到前端文件 {rel}")


# ============================================================
# 1. outline_mode 前后端契约（跨模块：目录生成 ↔ 前端）
# ============================================================


class TestOutlineModeContract:
    def test_backend_route_accepts_outline_mode(self):
        """路由 Query 参数三值枚举，与前端 OutlineMode 类型一一对应"""
        from app.routers.sections import generate_outline_sse

        import inspect

        src = inspect.getsource(generate_outline_sse)
        assert "outline_mode: str=Query('auto', pattern='^(auto|direct|stepwise)$')" in src, (
            "后端 outline_mode 枚举口径变化必须同步前端"
        )

    def test_frontend_passes_outline_mode(self):
        """前端 URL 生成与 EventSource 必须透传 outline_mode（CROSS-FIX 2026-10-04）"""
        api_ts = _frontend_file("api/index.ts").read_text(encoding="utf-8")
        assert 'outline_mode=${outlineMode}' in api_ts, (
            "前端 generateOutlineSSEUrl 丢失 outline_mode 透传，后端直出/分步选择不可达"
        )
        assert 'export type OutlineMode = "auto" | "direct" | "stepwise"' in api_ts
        page_tsx = _frontend_file("pages/OutlineEditorPage.tsx").read_text(encoding="utf-8")
        assert "createOutlineEventSource(projectId, outlineMode)" in page_tsx, (
            "目录页未把模式选择接入生成链路"
        )

    def test_sse_impl_accepts_outline_mode(self):
        """sse_outline 实现层必须接收并下传 outline_mode（兜底重跑同口径）"""
        import inspect

        from app.routers.sse import sse_outline

        sig = inspect.signature(sse_outline.generate_outline_sse_impl)
        assert "outline_mode" in sig.parameters
        src = inspect.getsource(sse_outline)
        assert src.count("outline_mode=outline_mode") >= 2, (
            "主流程与兜底重跑都必须下传 outline_mode，防降级路径丢用户选择"
        )

    def test_should_use_direct_outline_modes(self):
        from app.services.ai.workflows_outline_v51 import should_use_direct_outline

        short = json.dumps([{"item": f"评分{i}", "score": 10} for i in range(3)], ensure_ascii=False)
        long_text = json.dumps(
            [{"item": f"评分项目条目内容{i}" * 10, "score": 10} for i in range(20)], ensure_ascii=False
        )
        # auto：短→直出，长→分步
        assert should_use_direct_outline(short, "auto") is True
        assert should_use_direct_outline(long_text, "auto") is False
        # 显式模式覆盖长度判别
        assert should_use_direct_outline(long_text, "direct") is True
        assert should_use_direct_outline(short, "stepwise") is False
        # 包装形态：dict 包 items 按内层条数判
        wrapped = json.dumps({"items": [{"item": "a"}]}, ensure_ascii=False)
        assert should_use_direct_outline(wrapped, "auto") is True
        # 空文本保守走分步（无从判别规模）
        assert should_use_direct_outline("", "auto") is False


# ============================================================
# 2. 全局事实 → 正文生成按章筛选（P-5 跨模块数据链）
# ============================================================


class TestFactsPerSectionSelection:
    def _facts(self):
        return [
            {"key": "projectName", "label": "项目名称", "value": "某某市政项目", "category": "basic"},
            {"key": "scheduleDuration", "label": "建设工期", "value": "450 日历天", "category": "schedule"},
            {"key": "concreteGrade", "label": "混凝土标号", "value": "C30", "category": "technical"},
            {"key": "craneModel", "label": "塔吊型号", "value": "QTZ63", "category": "technical"},
            {"key": "pmName", "label": "项目经理", "value": "张三", "category": "personnel"},
        ]

    def test_baseline_always_included_and_relevance_first(self):
        from app.services.facts_service import select_facts_for_section

        picked = select_facts_for_section(self._facts(), "混凝土施工方案", "")
        keys = [f["key"] for f in picked]
        # 相关性最高命中的技术事实排首位
        assert keys[0] == "concreteGrade", keys
        # 基线事实（basic/schedule）无论匹配度都必须入选
        idx = {k: i for i, k in enumerate(keys)}
        assert "projectName" in keys and "scheduleDuration" in keys
        # 弱相关设备事实排在基线之后（基线优先于零散命中补位）
        assert idx["craneModel"] > min(idx["projectName"], idx["scheduleDuration"])
        assert len(picked) == 5  # 未超上限时不丢条目

    def test_max_items_truncates_by_priority(self):
        from app.services.facts_service import select_facts_for_section

        picked = select_facts_for_section(self._facts(), "混凝土施工方案", "", max_items=3)
        keys = [f["key"] for f in picked]
        assert set(["projectName", "scheduleDuration", "concreteGrade"]) <= set(keys), (
            "截断必须保住基线 + 最相关事实，被丢弃的只能是弱相关项"
        )

    def test_non_list_input_returned_as_is(self):
        """已格式化 str 不可被逐字符打分吞掉全部事实（原缺陷回归）"""
        from app.services.facts_service import select_facts_for_section

        raw = "项目名称：某某市政项目"
        assert select_facts_for_section(raw, "任意章节", "") == raw

    def test_workflows_sections_wiring_is_live_import(self):
        """接线必须是真实可达 import：相对层级错误会被 except 静默吞掉，
        使 P-5 筛选在生产链路永远不生效（历史缺陷模式：死接线）"""
        from app.services.ai import workflows_sections as ws

        facts = [
            {"key": "projectName", "label": "项目名称", "value": "X", "category": "basic"},
            {"key": "craneModel", "label": "塔吊型号", "value": "QTZ63", "category": "technical"},
        ]
        picked = ws._select_facts_for_section(facts, "混凝土施工方案", "")
        assert isinstance(picked, list) and picked[0]["category"] == "basic", (
            "筛选未生效（回退全量注入）——多半是 facts_service 接线 import 失败"
        )


# ============================================================
# 3. 上传解析：PDF 表格感知提取开关与回退（P-2）
# ============================================================


class TestPdfTableExtract:
    def test_config_default_and_env_override(self):
        from app import config

        # 默认开启（pdfplumber 不可用时安全回退，见下）；
        # 若测试环境显式设置则以环境为准（不作断言）
        if "PDF_TABLE_EXTRACT_ENABLED" not in __import__("os").environ:
            assert config.PDF_TABLE_EXTRACT_ENABLED is True

    def test_pdfplumber_unavailable_degrades_to_empty(self, monkeypatch):
        """pdfplumber 缺失时返回空串，由 _extract_pdf 回退 pypdf（不抛错）"""
        import builtins

        real_import = builtins.__import__

        def _no_pdfplumber(name, *a, **k):
            if name == "pdfplumber":
                raise ImportError("no pdfplumber")
            return real_import(name, *a, **k)

        monkeypatch.setattr(builtins, "__import__", _no_pdfplumber)
        assert file_service._extract_pdf_pdfplumber("whatever.pdf") == ""

    def test_disabled_switch_skips_plumber_branch(self):
        """PDF_TABLE_EXTRACT_ENABLED=false：强制旧 pypdf 路径（向后兼容出口）"""
        import inspect
        import re

        src = inspect.getsource(file_service._extract_pdf)
        # 开关守卫必须在 plumber 提取之前（关闭时不得触碰 pdfplumber 分支）。
        # R-1 2026-10-12：提取器搬迁后，开关与被调用方都经 _late() 晚解析
        # （保住 monkeypatch 接缝），源码形态随之变化 —— 这里用正则容忍
        # 「裸名 / _late("名")」两种写法，只钉住「守卫先于调用」这一语义。
        m_guard = re.search(r"if\s+.*?PDF_TABLE_EXTRACT_ENABLED", src)
        assert m_guard, "缺少 PDF_TABLE_EXTRACT_ENABLED 开关守卫"
        m_plumber = re.search(r"_extract_pdf_pdfplumber", src)
        assert m_plumber, "未找到 pdfplumber 提取分支调用"
        assert m_guard.start() < m_plumber.start()


# ============================================================
# 4. 上传解析 → 提取项目：编码降级 + 角色切换数据链
# ============================================================


class TestUploadChainDataFlow:
    def test_gbk_txt_uploads_without_failure(self):
        pid = _mk_project("跨模块-GBK编码")
        try:
            content = "市政道路工程，工期：450 天。".encode("gb18030")
            fname = f"招标文件gbk_{uuid.uuid4().hex[:6]}.txt"
            r = file_service.save_upload(pid, fname, content, "main")
            assert r["status"] == "success", r
            merged = __import__("asyncio").run(file_service.latest_extracted_text(pid))
            assert "450" in merged and "市政道路" in merged, (
                "GBK 编码降级解码失败，下游 AI 将拿到空文本"
            )
        finally:
            _cleanup_project(pid)

    def test_role_switch_reselects_main_file(self):
        """切主文件后合并文本立即换源（缓存失效，无脏读）"""
        pid = _mk_project("跨模块-角色切换")
        try:
            a = f"招标主文件A_{uuid.uuid4().hex[:6]}.txt"
            b = f"采购主文件B_{uuid.uuid4().hex[:6]}.txt"
            ra = file_service.save_upload(pid, a, "文件A内容 工期100天".encode("utf-8"), "main")
            rb = file_service.save_upload(pid, b, "文件B内容 工期200天".encode("utf-8"), "supporting")
            assert ra["status"] == rb["status"] == "success"

            merged = __import__("asyncio").run(file_service.latest_extracted_text(pid))
            assert "文件A内容" in merged.split("文件B内容")[0] or merged.startswith("文件A内容")

            # 手工把 B 设为主文件 → 合并文本必须以 B 开头
            res = file_service.set_file_role(pid, rb["id"], "main")
            assert res["file_role"] == "main"
            # main 全项目互斥：A 被自动降为 supporting（随响应透出）
            assert res["demoted_file_ids"] == [ra["id"]], res
            merged2 = __import__("asyncio").run(file_service.latest_extracted_text(pid))
            assert merged2.startswith("文件B内容"), (
                "角色切换后缓存未失效/旧 main 未降级，下游摘要/评分仍基于旧主文件"
            )
        finally:
            _cleanup_project(pid)

    def test_parse_task_row_updated_with_filename(self):
        """解析任务行按 filename 精确收口（并发上传不串写，回归钉）"""
        pid = _mk_project("跨模块-任务行")
        try:
            fname = f"任务行测试_{uuid.uuid4().hex[:6]}.txt"
            r = file_service.save_upload(pid, fname, "内容".encode("utf-8"), "main")
            assert r["status"] == "success"
            with get_db() as conn:
                row = conn.execute(
                    "SELECT status, filename FROM file_parse_tasks "
                    "WHERE project_id = ? AND filename = ? ORDER BY id DESC LIMIT 1",
                    (pid, r["filename"]),
                ).fetchone()
            assert row and row["status"] == "completed"
        finally:
            _cleanup_project(pid)

    def test_upload_multiple_mains_keeps_exclusive(self):
        """REVIEW-FIX M1：上传自动判角色也必须遵守 main 互斥。
        guess_file_role 对命中「招标」的非图片文件判 main，此前连传两份
        会产生多 main 行，_pick_main_file 按 id 取旧主文件 —— BUG-3 同症状
        的「重新上传换主文件」入口。"""
        pid = _mk_project("跨模块-上传多主文件")
        try:
            fa = f"招标文件甲_{uuid.uuid4().hex[:6]}.txt"
            fb = f"招标文件乙_{uuid.uuid4().hex[:6]}.txt"
            # 不显式传角色 → 走 guess_file_role 自动检测（均命中「招标」判 main）
            ra = file_service.save_upload(pid, fa, "甲文件内容A 工期100天".encode("utf-8"))
            rb = file_service.save_upload(pid, fb, "乙文件内容B 工期200天".encode("utf-8"))
            assert ra["status"] == rb["status"] == "success"
            assert ra["file_role"] == rb["file_role"] == "main"
            assert ra["demoted_file_ids"] == []            # 首个 main 无旧主可降
            assert rb["demoted_file_ids"] == [ra["id"]], rb  # 后传 main 自动降级旧 main
            with get_db() as conn:
                rows = conn.execute(
                    "SELECT id FROM uploaded_files WHERE project_id = ? AND file_role = 'main'",
                    (pid,),
                ).fetchall()
            assert [r["id"] for r in rows] == [rb["id"]], "上传后必须只剩最新一个 main"
            merged = __import__("asyncio").run(file_service.latest_extracted_text(pid))
            assert merged.startswith("乙文件内容B"), (
                "「重新上传」换主文件后下游合并文本仍读旧主文件（M1 复现）"
            )
        finally:
            _cleanup_project(pid)

    def test_demote_only_main_rejected(self):
        """REVIEW-FIX M2：唯一 main 不可被降为 0 main（0 main 态静默选中空文本图片）"""
        pid = _mk_project("跨模块-降级唯一主文件")
        try:
            fname = f"招标文件唯一_{uuid.uuid4().hex[:6]}.txt"
            r = file_service.save_upload(pid, fname, "唯一主文件内容".encode("utf-8"), "main")
            with pytest.raises(file_service.MainFileRequiredError):
                file_service.set_file_role(pid, r["id"], "supporting")
            with get_db() as conn:
                role = conn.execute(
                    "SELECT file_role FROM uploaded_files WHERE id = ?", (r["id"],)
                ).fetchone()["file_role"]
            assert role == "main", "拒绝后角色必须保持不变（不得出现 0 main 态）"
        finally:
            _cleanup_project(pid)

    def test_main_switch_flags_needs_reanalysis(self):
        """REVIEW-FIX m2：主文件换源且已有缓存摘要/评分时必须透出 needs_reanalysis。
        /ai/summary、/ai/scoring 会先查 analysis 表缓存短路返回，仅清合并文本
        缓存不足以让旧摘要换源，前端需据此提醒重新「提取项目」。"""
        pid = _mk_project("跨模块-换主提醒重分析")
        try:
            file_service.save_upload(
                pid, f"招标文件A_{uuid.uuid4().hex[:6]}.txt", "A内容".encode("utf-8"), "main"
            )
            rb = file_service.save_upload(
                pid, f"采购文件B_{uuid.uuid4().hex[:6]}.txt", "B内容".encode("utf-8"), "supporting"
            )
            with get_db() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO analysis"
                    " (project_id, summary_json, scoring_points_json)"
                    " VALUES (?, '{\"projectName\": \"A项目\"}', '[]')",
                    (pid,),
                )
            res = file_service.set_file_role(pid, rb["id"], "main")
            assert res["needs_reanalysis"] is True, (
                "主文件已换源但旧摘要/评分未被标记需重算（缓存将短路返回旧结果）"
            )
            # 未换源的重复设置不提醒（幂等操作不应反复告警）
            res2 = file_service.set_file_role(pid, rb["id"], "main")
            assert res2["needs_reanalysis"] is False
        finally:
            _cleanup_project(pid)
