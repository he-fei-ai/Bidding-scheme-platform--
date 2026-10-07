"""遗留问题收口回归测试 —— 2026-10-06（承接同日 H1 修复之后的第二批）

覆盖本轮四项加固：

- G2 `tender_context` 无锚点命中时改为**头尾双取**（原纯头截断会静默丢掉
  尾部评分细则/技术要求）
- G3 `generate_content_impl` 位置参数错位的**静态门禁**（防复发，带自证用例）
- G4 评分标准达标对齐（确定性）：区分「有无章节」与「答没答到点子上」
- G5 正文质量门禁默认值改为 env 可覆盖，且**默认行为逐字不变**
"""
import ast
import inspect
import json
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.services import tender_context as tc  # noqa: E402


# ===========================================================================
# G2 —— tender_context 无锚点截断改为头尾双取
# ===========================================================================

class TestTenderContextHeadTail:
    def test_tail_is_preserved_when_no_anchor(self):
        """核心回归：无锚点命中时，尾部内容必须仍出现在结果里。

        修复前 `full_text[:max_chars]` 只保留头部，尾部（评分细则常在此处）
        被静默丢弃。
        """
        head = "封面目录等无关键词内容。" * 200
        tail = "第五章 评分细则：应包含BIM模型与施工工艺流程图。" * 200
        text = head + tail
        out = tc.extract_tender_context(text, max_chars=2000)
        assert "评分细则" in out, "尾部评分细则被丢弃了"
        assert "BIM模型" in out
        assert len(out) <= 2000, "结果不得超过 max_chars 预算"

    def test_head_is_preserved_too(self):
        text = "技术标说明章节内容。" * 300 + "尾部其它内容。" * 300
        out = tc.extract_tender_context(text, max_chars=2000)
        assert "技术标" in out, "头部内容也必须保留"

    def test_short_text_returned_untouched(self):
        """短于预算的文本原样返回（不引入省略标记）"""
        text = "某项目招标文件正文，未命中任何锚点关键词。"
        assert tc.extract_tender_context(text, max_chars=40000) == text

    def test_omission_is_explicit_not_silent(self):
        """截断必须是显式的：结果里能看到省略标记（不静默丢信息）"""
        text = "无锚点内容甲乙丙丁戊己庚辛壬癸。" * 500
        out = tc.extract_tender_context(text, max_chars=2000)
        assert "此处省略" in out, "截断必须是显式标记，符合本模块既有噪声压缩原则"

    def test_tiny_budget_falls_back_safely(self):
        """预算小到放不下省略标记时退回原行为，不构造残缺文本"""
        text = "甲" * 500
        out = tc._head_tail_excerpt(text, 10)
        assert out == text[:10]

    def test_anchor_path_unaffected(self):
        """有锚点命中的正常路径不受本修复影响（防误伤）"""
        text = "\n".join(
            [f"第{i}行 投标人须知" for i in range(40)]
            + ["技术标要求：施工方案应含BIM模型与施工工艺流程图，并附进度计划表。"]
            + [f"第{i}行 格式条款" for i in range(40)]
        )
        out = tc.extract_tender_context(text, max_chars=40000)
        assert "BIM模型" in out


# ===========================================================================
# G3 —— generate_content_impl 位置参数错位静态门禁（带自证用例）
# ===========================================================================

def _query_param_names(fn) -> list[str]:
    """列出函数签名里所有 FastAPI Query 参数名（保持声明顺序）"""
    from fastapi import params as fastapi_params

    out = []
    for name, p in inspect.signature(fn).parameters.items():
        if isinstance(p.default, fastapi_params.Query):
            out.append(name)
    return out


def _positional_call_arg_count(module_path: Path, func_name: str) -> int | None:
    """用 AST 找到 `func_name(...)` 调用点并返回其**位置参数**个数。

    用 AST 而非字符串计数：不会被换行/括号换行/尾随逗号骗过。
    """
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
            if name == func_name:
                return len(node.args)
    return None


class TestGenerateContentImplParamGate:
    def test_router_and_impl_query_params_match(self):
        """门禁本体：router 暴露的 Query 参数必须与 impl 的形参逐一同序。

        这正是历史 TypeError 的根因防线 —— routers/sections.py::generate_content
        以**位置参数**调用 generate_content_impl，任何一侧单独加/删/换序参数
        都会让 Query 默认值被当实参传入，最终 int(concurrency) 收到 Query
        对象抛 TypeError。
        """
        from app.routers import sections
        from app.routers.sse import sse_content

        router_params = _query_param_names(sections.generate_content)
        impl_params = _query_param_names(sse_content.generate_content_impl)
        assert router_params == impl_params, (
            "router 与 impl 的 Query 参数必须完全一致且同序；"
            f"router={router_params} impl={impl_params}"
        )
        assert len(router_params) >= 14, "参数数量异常，签名可能被误改"

    def test_call_site_passes_all_params_positionally(self):
        """门禁本体：调用点必须把 project_id + 全部 Query 参数按位置传全。

        少传一个不会立刻报错，只会让尾部参数静默退化成 Query 对象 ——
        这类「不报错但行为错」的漂移比直接 TypeError 更难查。
        """
        from app.routers.sse import sse_content

        expected = 1 + len(_query_param_names(sse_content.generate_content_impl))
        actual = _positional_call_arg_count(
            BACKEND_DIR / "app" / "routers" / "sections.py", "generate_content_impl"
        )
        assert actual is not None, "未找到 generate_content_impl 的调用点"
        assert actual == expected, (
            f"调用点位置参数个数 {actual} != 期望 {expected}（project_id + {expected - 1} 个 Query 参数）"
        )

    def test_gate_actually_detects_drift(self):
        """自证用例：证明本门禁真能抓到漂移（否则规则写坏了会永绿）。

        必须用**真正的 Query 默认值**构造样本 —— 用普通默认值的话两个函数
        都会返回空列表，断言会「因为相等而通过」，自证就失去意义。
        """
        from fastapi import Query

        def router_ok(a: int = Query(1), b: int = Query(2)):
            ...

        def impl_ok(a: int = Query(1), b: int = Query(2)):
            ...

        def impl_drifted(a: int = Query(1)):
            ...

        def impl_reordered(a: int = Query(1), b: int = Query(2)):
            ...

        # 一致 → 通过
        assert _query_param_names(router_ok) == _query_param_names(impl_ok)
        # 少一个参数 → 必须被抓到
        assert _query_param_names(router_ok) != _query_param_names(impl_drifted)
        # 多一个参数 → 必须被抓到
        assert _query_param_names(router_ok) != ["a", "b", "c"]

        # 非 Query 的形参（如 path 参数 project_id、Depends 注入）不应计入
        def router_with_path(pid: int, a: int = Query(1), u=Query(None)):
            ...

        assert _query_param_names(router_with_path) == ["a", "u"]

    def test_ast_counter_detects_missing_arg(self):
        """自证用例：AST 位置参数计数确实能分辨少传/多传"""
        import tempfile

        src = "f(a, b, c)\nf(a, b)\n"
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8") as fh:
            fh.write(src)
            p = Path(fh.name)
        try:
            # 本函数返回**首个**匹配调用的计数
            assert _positional_call_arg_count(p, "f") == 3
        finally:
            p.unlink(missing_ok=True)


# ===========================================================================
# G4 —— 评分标准达标对齐（确定性）
# ===========================================================================

def _new_project(name: str) -> int:
    from app.db import get_db, init_db

    init_db()
    with get_db() as conn:
        cur = conn.execute("INSERT INTO projects (name) VALUES (?)", (name,))
        return int(cur.lastrowid)


def _seed_analysis(pid: int, *, scoring_points: list[dict]) -> None:
    from app.db import get_db

    with get_db() as conn:
        conn.execute("INSERT OR IGNORE INTO analysis (project_id) VALUES (?)", (pid,))
        conn.execute(
            "UPDATE analysis SET scoring_points_json = ? WHERE project_id = ?",
            (json.dumps(scoring_points, ensure_ascii=False), pid),
        )


def _seed_section(pid: int, *, title: str, content: str, order: int, sp_id: str | None) -> int:
    from app.db import get_db

    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO sections (project_id, title, level, sort_order, content, status, scoring_point_id) "
            "VALUES (?, ?, 1, ?, ?, 'completed', ?)",
            (pid, title, order, content, sp_id),
        )
        return int(cur.lastrowid)


class TestScoringCompliance:
    """核心价值主张：区分「章节挂上了」（存在性）与「答到点子上了」（达标性）"""

    def test_existence_only_would_pass_but_compliance_flags_weak(self):
        """本模块存在的理由：预检判「已覆盖」，但正文其实没答"""
        from app.services.scoring_compliance import audit_scoring_compliance

        pid = _new_project("G4-挂载但没答")
        _seed_analysis(pid, scoring_points=[{
            "id": "R1", "name": "施工组织设计", "weight": "10",
            "standard": "须包含施工工艺流程图并附BIM模型技术文件",
        }])
        # 正文长度必须超过 MIN_CONTENT_CHARS(50)，否则会先被 empty 分支拦下，
        # 测的就不是「有正文但没答到点子上」这一场景了。
        _seed_section(pid, title="施工组织设计",
                      content="本章介绍施工工艺相关说明与现场组织安排。" * 3, order=0, sp_id="R1")
        rep = audit_scoring_compliance(pid)
        weak = rep.by_status("weak")
        assert len(weak) == 1, "内容没覆盖评分标准却没被标记 —— 与预检的存在性检查无异"
        assert weak[0].scoring_point_id == "R1"
        from app.services.scoring_compliance import COVERAGE_WEAK

        assert weak[0].coverage < COVERAGE_WEAK
        assert "BIM" in weak[0].standard

    def test_well_answered_point_is_not_flagged(self):
        """零假阳性：正文确实答到了标准要求 → 不进 gaps"""
        from app.services.scoring_compliance import audit_scoring_compliance

        pid = _new_project("G4-答到了")
        _seed_analysis(pid, scoring_points=[{
            "id": "R1", "name": "施工组织设计", "weight": "10",
            "standard": "须包含施工工艺流程图并附BIM模型技术文件",
        }])
        _seed_section(
            pid, title="施工组织设计", order=0, sp_id="R1",
            content="本方案包含施工工艺流程图，并附BIM模型技术文件，"
                    "涵盖工艺流程、施工进度与技术措施，并明确各工序衔接方式。",
        )
        rep = audit_scoring_compliance(pid)
        assert rep.gaps == [], f"答到了却仍被报为缺口：{[g.to_dict() for g in rep.gaps]}"
        assert rep.total_points == 1

    def test_unmapped_point(self):
        from app.services.scoring_compliance import audit_scoring_compliance

        pid = _new_project("G4-未挂载")
        _seed_analysis(pid, scoring_points=[{"id": "R9", "name": "应急预案", "standard": "须含应急演练流程"}])
        rep = audit_scoring_compliance(pid)
        assert [g.status for g in rep.gaps] == ["unmapped"]
        assert "没有任何章节挂载" in rep.gaps[0].detail

    def test_mapped_but_empty_point(self):
        from app.services.scoring_compliance import audit_scoring_compliance

        pid = _new_project("G4-空章节")
        _seed_analysis(pid, scoring_points=[{"id": "R1", "name": "应急预案", "standard": "须含应急演练流程与责任人"}])
        _seed_section(pid, title="应急预案", content="待补充", order=0, sp_id="R1")
        rep = audit_scoring_compliance(pid)
        assert [g.status for g in rep.gaps] == ["empty"]
        assert rep.gaps[0].content_chars == 3

    def test_placeholder_scoring_point_is_stripped(self):
        """权重异常的伪评分项不得被算成「1 个未达标」（否则用户永远消不掉）"""
        from app.services.scoring_compliance import audit_scoring_compliance

        pid = _new_project("G4-伪评分项")
        _seed_analysis(pid, scoring_points=[
            {"id": "__weight_warning__", "name": "[权重异常] 权重之和为 88"},
            {"id": "R1", "name": "应急预案", "standard": "须含应急演练流程与责任人分工"},
        ])
        _seed_section(pid, title="应急预案", content="本方案含应急演练流程与责任人分工，覆盖预案启动条件。", order=0, sp_id="R1")
        rep = audit_scoring_compliance(pid)
        assert rep.total_points == 1, "伪评分项未被剔除"
        assert all(g.scoring_point_id != "__weight_warning__" for g in rep.gaps)

    def test_no_scoring_points_is_warned_not_crashed(self):
        from app.services.scoring_compliance import audit_scoring_compliance

        pid = _new_project("G4-无评分点")
        _seed_analysis(pid, scoring_points=[])
        rep = audit_scoring_compliance(pid)
        assert rep.gaps == []
        assert rep.warnings and "scoring_points_json" in rep.warnings[0]

    def test_malformed_json_degrades(self):
        """脏 JSON 不得抛异常打断审计（降级为无评分点）"""
        from app.db import get_db
        from app.services.scoring_compliance import audit_scoring_compliance

        pid = _new_project("G4-脏JSON")
        with get_db() as conn:
            conn.execute("INSERT OR IGNORE INTO analysis (project_id) VALUES (?)", (pid,))
            conn.execute("UPDATE analysis SET scoring_points_json = ? WHERE project_id = ?", ("{不是json", pid))
        rep = audit_scoring_compliance(pid)
        assert rep.total_points == 0
        assert rep.warnings

    def test_threshold_is_parameterizable(self):
        """阈值参数化：调严后同一个项目会报出缺口（向后兼容默认值的可覆盖性）"""
        from app.services.scoring_compliance import audit_scoring_compliance

        pid = _new_project("G4-阈值可调")
        _seed_analysis(pid, scoring_points=[{
            "id": "R1", "name": "施工方案", "standard": "须包含施工工艺流程图并附BIM模型",
        }])
        _seed_section(pid, title="施工方案",
                      content="本文介绍施工工艺流程图与BIM模型的基本概念、用途与实施要点，"
                              "并详细说明其在现场组织管理、进度协调与质量控制中的"
                              "具体应用方式与注意事项。",
                      order=0, sp_id="R1")
        loose = audit_scoring_compliance(pid, coverage_weak=0.01)
        strict = audit_scoring_compliance(pid, coverage_weak=0.99)
        assert len(strict.gaps) >= 1, "阈值调到 0.99 时必然报缺口"
        assert loose.gaps == [], "阈值放到 0.01 时不应报缺口（同一项目、同一内容）"
        assert strict.by_status("weak")[0].coverage == pytest.approx(
            loose.by_status("covered")[0].coverage, abs=1e-6
        ), "同一输入在两种阈值下算出的覆盖率必须一致（阈值只影响分类，不影响度量）"

    def test_persist_and_endpoint(self):
        """端到端：HTTP 200 + 落库审计历史"""
        from fastapi.testclient import TestClient

        from app.db import get_db
        from app.main import app
        from app.security.auth import User, get_current_user

        pid = _new_project("G4-端到端")
        _seed_analysis(pid, scoring_points=[{"id": "R1", "name": "应急预案", "standard": "须含应急演练流程"}])
        _seed_section(pid, title="应急预案", content="待补充", order=0, sp_id="R1")

        uid = 1
        app.dependency_overrides[get_current_user] = lambda: User(uid, "u", "owner")
        try:
            with TestClient(app) as client:
                resp = client.get(f"/api/v1/consistency/scoring-compliance/{pid}")
        finally:
            app.dependency_overrides.clear()

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "ok"
        assert body["total_points"] == 1
        assert body["empty"] == 1
        assert body["gaps"][0]["scoring_point_id"] == "R1"

        with get_db(readonly=True) as conn:
            row = conn.execute(
                "SELECT total_points, empty FROM scoring_compliance_audit WHERE project_id = ?",
                (pid,),
            ).fetchone()
        assert row is not None, "审计结果未落库"
        assert row["total_points"] == 1 and row["empty"] == 1


# ===========================================================================
# G5 —— 质量门禁默认值 env 可覆盖且默认行为不变
# ===========================================================================

class TestQualityGateDefaults:
    def test_defaults_are_false_without_env(self):
        """向后兼容：未设 env 时三项门禁默认仍为 False（与改动前逐字一致）"""
        from app import config

        assert config.CONTENT_USE_ORCHESTRATION_DEFAULT is False
        assert config.CONTENT_USE_WORD_SAMPLING_DEFAULT is False
        assert config.CONTENT_USE_CONSISTENCY_AUDIT_DEFAULT is False

    def test_env_bool_parser(self):
        from app.config import _env_bool

        assert _env_bool("__NOPE__", "false") is False
        assert _env_bool("__NOPE__", "true") is True
        import os

        os.environ["__G5_TEST__"] = "1"
        try:
            assert _env_bool("__G5_TEST__") is True
        finally:
            del os.environ["__G5_TEST__"]

    def test_router_defaults_read_from_config(self):
        """router 的 Query 默认值确实取自 config（env 生效链路通）"""
        from app.routers import sections

        from app import config

        defaults = {}
        for n, p in inspect.signature(sections.generate_content).parameters.items():
            d = p.default
            if hasattr(d, "default"):  # Query 对象
                defaults[n] = d.default
        assert defaults["use_orchestration"] == config.CONTENT_USE_ORCHESTRATION_DEFAULT
        assert defaults["use_word_sampling"] == config.CONTENT_USE_WORD_SAMPLING_DEFAULT
        assert defaults["use_consistency_audit"] == config.CONTENT_USE_CONSISTENCY_AUDIT_DEFAULT
        assert "use_orchestration" in _query_param_names(sections.generate_content)
