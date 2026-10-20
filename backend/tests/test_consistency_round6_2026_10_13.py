"""一致性校核模块 —— 第六轮（2026-10-13）缺陷修复回归测试。

本文件覆盖本轮在既有五轮加固之上新定位并修复的缺陷。全部用例先用「缺陷当时的
真实形态」复现（探针脚本实测确认，非推测），再断言修复后的可观察行为：

| 用例组 | 缺陷 | 严重度 |
|---|---|---|
| `TestEmptyValueConflictsNotFalselyResolved` | `refresh_conflict_states` 把无结构化取值（AI 审计发现，`values_json='[]'`）的冲突误标已解决 —— 每跑一次 AI/Agent 修复，未解决 AI 发现全部静默消失 | **P1** |
| `TestDeterministicAuditPreservesAiFindings` | `_save_conflicts` 的 DELETE/折叠/复用不带来源前缀，确定性审计会清掉 AI 审计写入的 `AI-%` 行（与 AI 路径「只清 AI-%」的设计不对称） | **P1** |
| `TestAiConflictSuggestionFallsBack` | 空取值冲突走 `/fix-suggestion` 时 `Counter([]).most_common(1)[0]` → IndexError → 404「未找到修复建议」（冲突存在却说建议不存在） | P2 |
| `TestOpenApiOperationIdsUnique` | `api_route(methods=['GET','POST'])` 使同一路由两个 operation 共用同一 `operation_id`（OpenAPI 契约损坏） | P2 |
| `TestAutofixPartialStatus` | autofix 部分落文成功时回 `no_change`（「未做任何修改」）与 message「已修改 N 个章节」自相矛盾 | P2 |
| `TestRejectionStatusNamespace` | 废标应答三档状态文案/配色收敛进 `consistency_rules` 命名空间并随 `/rules` 下发 | 增强 |

数据来源约定：AI 审计发现的行形（`AI-%` 前缀、`values_json='[]'`、description 含
「（修复建议：…）」）与 `app/services/ai/workflows_expand.py::_persist_ai_audit_findings`
的写入口径逐字段一致 —— 直接照抄生产写入形态，避免测试替身与真实契约漂移。
"""

from __future__ import annotations

import json

import pytest

from app.db import get_db, init_db
from app.services import consistency_rules as rules
from app.services.consistency_auditor import consistency_auditor
from app.services.consistency_repair import refresh_conflict_states


def _load_fact_corpus() -> list[tuple[str, list[tuple[str, str]]]]:
    """按文件路径加载事实抽取语料库（R-13 双向断言复用同一份基线）。

    不走 `import` 是刻意的：pytest 的 `sys.path` 注入行为随 import mode 配置
    变化，而语料库与本文件是同目录兄弟文件，按路径加载在任何配置下都成立。
    """
    import importlib.util
    from pathlib import Path

    path = Path(__file__).with_name("test_consistency_fact_corpus_2026_10_13.py")
    spec = importlib.util.spec_from_file_location("_consistency_fact_corpus", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.CORPUS


CORPUS = _load_fact_corpus()


OWNER = 3501

#: 与语料库测试同名的审计器实例别名（两文件对同一单例的称呼保持一致）
AUDITOR = consistency_auditor


def _new_project(name: str) -> int:
    init_db()
    with get_db() as conn:
        conn.execute(
            "INSERT INTO projects (name, industry, created_by) VALUES (?, ?, ?)",
            (name, "通用", OWNER),
        )
        return conn.execute("SELECT id FROM projects ORDER BY id DESC LIMIT 1").fetchone()["id"]


def _seed_section(project_id: int, *, title: str, content: str, sort_order: int = 0) -> int:
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO sections (project_id, title, level, sort_order, content, status, section_type) "
            "VALUES (?, ?, 1, ?, ?, 'completed', 'normal')",
            (project_id, title, sort_order, content),
        )
        return cur.lastrowid


def _seed_conflict(
    project_id: int,
    *,
    conflict_id: str,
    fact_name: str = "工期",
    values: str = '["30","45"]',
    sources: str = '["1"]',
    severity: str = "high",
    description: str = "工期存在2种不同表述：30、45，可能存在不一致",
    resolved: int = 0,
) -> str:
    """插一条冲突行；`values` / `sources` 传 JSON 字符串，便于构造空取值等边角。"""
    with get_db() as conn:
        conn.execute(
            """INSERT INTO consistency_conflicts
               (project_id, conflict_id, fact_name, values_json,
                sources_json, severity, description, resolved)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (project_id, conflict_id, fact_name, values, sources, severity, description, resolved),
        )
    return conflict_id


def _seed_ai_finding(
    project_id: int,
    *,
    issue: str = "第3章工期表述与全局事实不一致",
    suggestion: str = "统一为 450 日历天",
    section_id: str = "1",
    resolved: int = 0,
) -> str:
    """按 `_persist_ai_audit_findings` 的生产写入口径插一条 AI 审计发现。"""
    description = issue + (f"（修复建议：{suggestion}）" if suggestion else "")
    return _seed_conflict(
        project_id,
        conflict_id=f"AI-{project_id}-{abs(hash((issue, section_id))) % 10**12:x}",
        fact_name=issue[:100] or "AI 一致性发现",
        values="[]",
        sources=json.dumps([section_id], ensure_ascii=False) if section_id else "[]",
        severity=rules.SEVERITY_DEFAULT,
        description=description,
        resolved=resolved,
    )


def _conflict_row(conflict_id: str) -> dict | None:
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM consistency_conflicts WHERE conflict_id = ?", (conflict_id,)
        ).fetchone()
    return dict(row) if row else None


def _client():
    from fastapi.testclient import TestClient

    from app.main import app
    from app.security import auth

    app.dependency_overrides[auth.get_current_user] = lambda: auth.User(OWNER, "u", "user")
    return TestClient(app)


# ============================================================================
# BUG-A（P1）：空取值冲突（AI 语义发现）不得被收敛判定误标已解决
# ============================================================================


class TestEmptyValueConflictsNotFalselyResolved:
    """`refresh_conflict_states` 的判据是「全文还能找到 >=2 个冲突取值」。

    AI 审计路径写入的冲突 `values_json` 恒为 `'[]'`（自由文本发现，没有结构化
    取值）。旧实现对这类行：`present=[]` → `len(unique_present) <= 1` 恒成立 →
    每次跑 AI 补丁修复 / Agent 修复都把全部未解决 AI 发现静默标成
    `resolved=1`：用户看到冲突凭空消失，而正文里的问题可能原样存在。

    实测（修复前探针）：
        refresh_conflict_states(...) -> {'resolved': 1, 'still_open': 0}
        AI 冲突仍为未解决? False   ← 缺陷
    """

    def test_ai_finding_stays_open_and_counted(self):
        pid = _new_project("R6-A-空取值保持未解决")
        cid = _seed_ai_finding(pid)
        _seed_section(pid, title="第一章", content="施工组织设计", sort_order=1)
        _seed_section(pid, title="第二章", content="本工程工期 450 日历天", sort_order=2)

        with get_db() as conn:
            states = refresh_conflict_states(
                conn,
                pid,
                {
                    1: "施工组织设计",
                    2: "本工程工期 450 日历天",
                },
            )

        assert states == {"resolved": 0, "still_open": 1}, (
            "空取值冲突无法验证收敛，必须保持未解决并计入 still_open"
        )
        row = _conflict_row(cid)
        assert row is not None and row["resolved"] == 0
        assert row["resolved_at"] is None, "不得顺带写 resolved_at（会被前端当成已处理）"

    def test_empty_values_do_not_block_verifiable_conflicts(self):
        """同一轮里既有 AI 发现又有可验证冲突时，两条判定互不干扰。"""
        pid = _new_project("R6-A-混合冲突")
        ai_cid = _seed_ai_finding(pid)
        det_cid = _seed_conflict(pid, conflict_id=f"conflict_{pid}_工期_mix", values='["30","45"]')

        with get_db() as conn:
            states = refresh_conflict_states(conn, pid, {1: "本工程工期 45 天"})

        # 可验证冲突：45 仍在、30 不在 → distinct present = 1 → 已解决
        assert states == {"resolved": 1, "still_open": 1}
        assert _conflict_row(ai_cid)["resolved"] == 0
        assert _conflict_row(det_cid)["resolved"] == 1

    def test_blank_and_malformed_values_also_kept_open(self):
        """全空白取值与坏 JSON 同样不可验证 → 保持未解决（保守口径）。"""
        pid = _new_project("R6-A-空白取值")
        blank_cid = _seed_conflict(pid, conflict_id=f"conflict_{pid}_blank", values='["", "  "]')
        with get_db() as conn:
            states = refresh_conflict_states(conn, pid, {1: "任意正文"})
        assert states["still_open"] == 1 and states["resolved"] == 0
        assert _conflict_row(blank_cid)["resolved"] == 0

        pid2 = _new_project("R6-A-坏JSON")
        bad_cid = _seed_conflict(pid2, conflict_id=f"conflict_{pid2}_bad", values="not-json")
        with get_db() as conn:
            states2 = refresh_conflict_states(conn, pid2, {1: "任意正文"})
        assert states2["still_open"] == 1 and states2["resolved"] == 0
        assert _conflict_row(bad_cid)["resolved"] == 0


# ============================================================================
# BUG-B（P1）：确定性审计不得清掉 AI 审计写入的 AI-% 行
# ============================================================================


class TestDeterministicAuditPreservesAiFindings:
    """`_save_conflicts` 此前的 DELETE/折叠/复用查询都不带来源前缀。

    AI 路径（`_persist_ai_audit_findings`）的设计是「只清 `AI-%`、绝不动
    `conflict_` 行」；而确定性路径反过来清**全部**未解决行 —— 两侧不对称的
    后果：用户点一次「开始审计」，生成时 AI 审计的发现就全部消失，G1 修复
    （让 AI 审计结论可事后复查）被实质性架空。

    实测（修复前探针）：
        确定性审计后 AI 冲突仍存在? False   ← 缺陷
    """

    def test_open_ai_findings_survive_deterministic_audit(self):
        pid = _new_project("R6-B-审计保留AI未解决行")
        ai_cid = _seed_ai_finding(pid)
        _seed_section(pid, title="第一章", content="本工程工期 30 天", sort_order=1)
        _seed_section(pid, title="第二章", content="本工程工期 45 天", sort_order=2)

        result = consistency_auditor.run_full_audit(pid)

        assert rules.is_audit_success(result.status)
        assert result.conflicts_found >= 1, "两章工期不一致应被确定性审计检出"
        row = _conflict_row(ai_cid)
        assert row is not None and row["resolved"] == 0, (
            "确定性审计只应清理自己的 conflict_ 行，AI-% 发现必须保留供用户复查"
        )

    def test_resolved_ai_findings_survive_and_not_reused(self):
        """已解决的 AI 历史行不被折叠删除，也不被同名确定性冲突「重新打开」。"""
        pid = _new_project("R6-B-审计保留AI已解决行")
        ai_cid = _seed_ai_finding(pid, resolved=1)
        _seed_section(pid, title="第一章", content="本工程工期 30 天", sort_order=1)
        _seed_section(pid, title="第二章", content="本工程工期 45 天", sort_order=2)

        consistency_auditor.run_full_audit(pid)

        row = _conflict_row(ai_cid)
        assert row is not None, "AI 已解决历史行不得被确定性审计的折叠逻辑删掉"
        assert row["resolved"] == 1, "AI 历史行不得被同名确定性冲突复用/重新打开"

    def test_deterministic_rows_still_fully_replaced(self):
        """修复不得削弱确定性路径的既定语义：未解决旧行仍被清、同名复用仍生效。"""
        pid = _new_project("R6-B-确定性语义不变")
        stale = _seed_conflict(
            pid,
            conflict_id=f"conflict_{pid}_工期_stale",
            values='["30","45"]',
            sources='["1"]',
        )
        _seed_section(pid, title="第一章", content="本工程工期 60 天", sort_order=1)

        consistency_auditor.run_full_audit(pid)

        # 60 单一取值不构成冲突 → 上一轮的未解决旧行必须被清掉（全量重扫语义）
        assert _conflict_row(stale) is None

        # 再造一轮真实冲突，验证「同名已解决历史复用」仍然生效（不多一行）
        _seed_section(pid, title="第二章", content="本工程工期 90 天", sort_order=2)
        first = consistency_auditor.run_full_audit(pid)
        assert first.conflicts_found == 1
        open_rows = [r for r in [_conflict_row(c.conflict_id) for c in first.conflicts] if r]
        assert len(open_rows) == 1
        # 手动标解决后再审计 → 复用同一行重新打开，不新增行
        with get_db() as conn:
            conn.execute(
                "UPDATE consistency_conflicts SET resolved = 1, resolved_at = ? "
                "WHERE conflict_id = ?",
                ("2026-10-13 00:00:00", first.conflicts[0].conflict_id),
            )
        second = consistency_auditor.run_full_audit(pid)
        assert second.conflicts_found == 1
        with get_db() as conn:
            all_rows = [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM consistency_conflicts WHERE project_id = ?", (pid,)
                ).fetchall()
            ]
        still_open = [r for r in all_rows if not r["resolved"]]
        assert len(still_open) == 1, "复用历史行不得额外新增行"
        assert still_open[0]["conflict_id"] == first.conflicts[0].conflict_id, (
            "同名已解决历史必须被复用（重新打开），而不是新建一行"
        )
        # BUG-FIX 2026-10-13（第六轮）：复用时**审计响应里的 conflict_id 也必须
        # 与库里的真实 ID 一致** —— 此前响应带回新生成的 ID，客户端拿它调
        # `/fix-suggestion` 会 404（冲突就在眼前，ID 却指向不存在的行）。
        assert second.conflicts[0].conflict_id == first.conflicts[0].conflict_id


# ============================================================================
# BUG-C（P2）：AI 语义发现的修复建议回退为描述文本，而不是 404
# ============================================================================


class TestAiConflictSuggestionFallsBack:
    """空取值冲突走 `suggest_fixes` 旧路径：

        Counter([]).most_common(1)[0] → IndexError
        → `_load_suggestion_payload` 兜底 except → (None, None)
        → 路由 `if suggestion:` 不成立 → 404「未找到修复建议」

    冲突明明存在、AI 也给了修复方向（description 里），用户却只看到
    「未找到修复建议」——建议不是不存在，是取不出来。
    """

    def test_service_returns_description_based_text(self):
        pid = _new_project("R6-C-服务层回退")
        cid = _seed_ai_finding(pid, issue="第3章工期与事实不一致", suggestion="统一为 450 天")

        text = consistency_auditor.suggest_fixes(cid, pid)
        assert text is not None, "AI 发现必须能给出人类可读建议（回退 description）"
        assert "第3章工期与事实不一致" in text
        assert "统一为 450 天" in text
        assert "无法自动统一" in text, "必须显式说明为何没有可机器采纳的目标值"
        # 目标值仍为 None：前端「采纳建议并修复」保持禁用，不会把文案当值提交
        assert consistency_auditor.suggest_fix_target_value(cid, pid) is None

    def test_route_returns_200_with_suggestion(self):
        pid = _new_project("R6-C-路由层 200")
        cid = _seed_ai_finding(pid)
        c = _client()

        r = c.get(
            f"/api/v1/consistency/conflicts/{cid}/fix-suggestion",
            params={"project_id": pid},
        )
        assert r.status_code == 200, "AI 发现的建议端点不得回 404"
        body = r.json()
        assert body["status"] == "ok"
        assert body["suggestion"]
        assert body["target_value"] is None

    def test_missing_conflict_still_404(self):
        """回归保护：冲突真的不存在时，404 语义不变（不许把 404 也一并吞掉）。"""
        pid = _new_project("R6-C-不存在仍404")
        c = _client()
        r = c.get(
            "/api/v1/consistency/conflicts/AI-does-not-exist/fix-suggestion",
            params={"project_id": pid},
        )
        assert r.status_code == 404

    def test_normal_conflict_suggestion_unchanged(self):
        """结构化冲突的建议推导完全不走回退分支（契约零变化）。"""
        pid = _new_project("R6-C-普通冲突不变")
        cid = _seed_conflict(pid, conflict_id=f"conflict_{pid}_工期_norm", values='["30","45"]')
        text = consistency_auditor.suggest_fixes(cid, pid)
        assert text and "建议将以下内容统一为" in text
        target = consistency_auditor.suggest_fix_target_value(cid, pid)
        assert target in ("30", "45")


# ============================================================================
# BUG-D（P2）：OpenAPI operation_id 唯一（GET/POST 双动词不再撞 ID）
# ============================================================================


class TestOpenApiOperationIdsUnique:
    """`api_route(methods=['GET','POST'])` 注册的同一路由会生成两个 operation
    却共用同一个 `operation_id`（`generate_unique_id` 只取 `methods[0]`）。

    实测（修复前）：`app.openapi()` 告警
        Duplicate Operation ID run_audit_api_v1_consistency_audit__project_id__get
    schema 内 GET/POST 两条目 operationId 相同 —— Swagger UI 行为异常，按
    OpenAPI 生成客户端的代码会互相覆盖（POST 被 GET 顶掉）。
    """

    def test_no_duplicate_operation_ids_app_wide(self):
        import warnings

        from app.main import app

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            schema = app.openapi()
        dup_warnings = [w for w in caught if "Duplicate Operation ID" in str(w.message)]
        assert not dup_warnings, (
            f"OpenAPI 存在重复 operation_id: {[str(w.message) for w in dup_warnings]}"
        )

        seen: set[str] = set()
        dups: list[str] = []
        for path, item in schema.get("paths", {}).items():
            for method, op in item.items():
                if isinstance(op, dict) and "operationId" in op:
                    if op["operationId"] in seen:
                        dups.append(f"{method.upper()} {path} -> {op['operationId']}")
                    seen.add(op["operationId"])
        assert not dups, f"schema 内重复 operationId: {dups}"

    def test_audit_get_is_deprecated_and_post_is_not(self):
        """legacy GET 在 OpenAPI 中显式标注 deprecated，POST 是规范入口。"""
        from app.main import app

        schema = app.openapi()
        audit = schema["paths"]["/api/v1/consistency/audit/{project_id}"]
        assert audit["get"].get("deprecated") is True
        assert not audit["post"].get("deprecated")
        assert audit["get"]["operationId"] != audit["post"]["operationId"]

    def test_both_verbs_keep_identical_response_contract(self):
        """双动词响应体一字不差（既有契约，与 2026-10-13 契约测试同口径）。"""
        pid = _new_project("R6-D-双动词契约")
        _seed_section(pid, title="第一章", content="总工期：30 天。", sort_order=1)
        c = _client()

        post = c.post(f"/api/v1/consistency/audit/{pid}")
        get = c.get(f"/api/v1/consistency/audit/{pid}")
        assert post.status_code == get.status_code == 200
        assert set(post.json()) == set(get.json())
        assert post.json()["status"] == "ok"


# ============================================================================
# E3（P2）：autofix 部分成功返回 partial，不再谎称 no_change
# ============================================================================


class TestAutofixPartialStatus:
    """部分章节落文成功、个别章节被安全闸门拦下时，旧契约一律回
    `no_change`（未做任何修改）—— 与 message「已修改 N 个章节」自相矛盾。

    构造形态：冲突取值 `["20", "20天"]`（同一取值的两种写法）：
      - 章节甲含「工期 20天」→ 两个取值的替换区间重叠 → `replace_values`
        整批拒绝（skipped，带 errors）；
      - 章节乙含「工期 20 天」（20 与天之间有空格）→ 仅「20」可替换且
        就近有标签词 → 落文成功。

    注意：不能把章节甲构造成「共 20 人施工」——那会被**标签词就近校验**
    正确拦下并计入 `filtered`（按设计关闭冲突、不算 skipped），走不到
    partial 分支。partial 的前提是「有 errors 的跳过」。
    """

    def _seed_partial_case(self, pid: int) -> tuple[int, int, str]:
        sec_a = _seed_section(pid, title="甲章", content="本工程工期 20天。", sort_order=1)
        sec_b = _seed_section(pid, title="乙章", content="本工程工期 20 天。", sort_order=2)
        cid = _seed_conflict(
            pid,
            conflict_id=f"conflict_{pid}_工期_partial",
            fact_name="工期",
            values='["20","20天"]',
            sources=json.dumps([f"section_{sec_a} (甲章)", f"section_{sec_b} (乙章)"]),
            description="工期存在2种不同表述：20、20天，可能存在不一致",
        )
        return sec_a, sec_b, cid

    def test_partial_success_reports_partial_status(self):
        pid = _new_project("R6-E3-部分成功")
        sec_a, sec_b, cid = self._seed_partial_case(pid)
        c = _client()

        r = c.post(
            f"/api/v1/consistency/conflicts/{cid}/autofix",
            params={"project_id": pid, "new_value": "30"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "partial", "有落文 + 有跳过必须报 partial"
        assert body["changed_sections"] == 1
        assert body["skipped"], "被闸门拦下的章节必须出现在 skipped 里"
        assert "已修改 1 个章节" in body["message"]
        # 冲突保持未解决（有 skipped）—— 不谎报修复完成
        assert _conflict_row(cid)["resolved"] == 0
        # 落文真实发生：乙章已改、甲章原样
        with get_db() as conn:
            contents = {
                row["id"]: row["content"]
                for row in conn.execute(
                    "SELECT id, content FROM sections WHERE project_id = ?", (pid,)
                ).fetchall()
            }
        assert contents[sec_b] == "本工程工期 30 天。"
        assert contents[sec_a] == "本工程工期 20天。"

    def test_nothing_changed_still_no_change(self):
        """一个都没改时仍是 no_change（回归保护，不被 partial 吞掉）。"""
        pid = _new_project("R6-E3-无改动")
        cid = _seed_conflict(
            pid,
            conflict_id=f"conflict_{pid}_工期_none",
            fact_name="工期",
            values='["20","20人"]',
            sources=json.dumps(["section_1 (人员章)"]),
        )
        _seed_section(pid, title="人员章", content="本工程共 20 人施工。", sort_order=1)
        c = _client()

        r = c.post(
            f"/api/v1/consistency/conflicts/{cid}/autofix",
            params={"project_id": pid, "new_value": "30"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "no_change"
        assert body["changed_sections"] == 0
        # 无改动 → 冲突保持未解决（人工处理），不得被标已解决
        assert _conflict_row(cid)["resolved"] == 0

    def test_dry_run_preview_is_ok_status(self):
        """dry_run 预览即使有 skipped 也是 ok（预览不落文、不关冲突）。"""
        pid = _new_project("R6-E3-预览")
        sec_a, sec_b, cid = self._seed_partial_case(pid)
        c = _client()

        r = c.post(
            f"/api/v1/consistency/conflicts/{cid}/autofix",
            params={"project_id": pid, "new_value": "30", "dry_run": "true"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok" and body["dry_run"] is True
        assert body["changed_sections"] == 1
        # 预览不写库：两章原文都在
        with get_db() as conn:
            rows = conn.execute(
                "SELECT content FROM sections WHERE project_id = ? ORDER BY id", (pid,)
            ).fetchall()
        assert [row["content"] for row in rows] == ["本工程工期 20天。", "本工程工期 20 天。"]


# ============================================================================
# R-14 闭环（遗留项）：禁止把「会互判冲突」的事实写进基准
# ============================================================================


class TestBackflowBaselineConflictGuard:
    """原 R-14（2026-10-11 记为「有意不改」的遗留项）：跨轮同名不同 value 的候选
    能通过 dedup，于是用户自己确认的两条事实会互相判成冲突 —— 且该冲突来自
    global_facts 本身，**改正文无法消解**（审计器按 category 收基准，并把该事实名
    下所有值收进 `all_facts` 互比，没有「首条胜出」）。

    2026-10-11 不改的理由是「label 去重会静默丢弃用户明确确认的候选（UI 回报
    0 条新增却查不出原因）」。本轮的最优解同时避开两头：**不静默丢弃，而是拦截
    并回传可读原因**（`plan_fact_merge` 第三返回值 → `accept_fact_proposals` 的
    `skipped` 字段）。拦截判据复用审计器自身的等价判定 `_distinct_values`，
    因此「拦不拦」与「审计器会不会判冲突」永远一致。
    """

    def test_baseline_category_rejects_different_value(self):
        from app.services.consistency_backflow import plan_fact_merge

        existing = [{"key": "a", "label": "工期", "value": "450", "category": "schedule"}]
        items, added, skipped = plan_fact_merge(
            existing,
            [{"fact_name": "工期", "value": "60", "category": "schedule", "conflict_id": "c1"}],
        )
        assert added == 0
        assert len(skipped) == 1
        assert skipped[0]["fact_name"] == "工期"
        assert "工期=450" in skipped[0]["reason"], "拒绝原因必须指名与之冲突的既有事实"
        assert items == existing, "被拒绝的候选不得进清单"

    def test_baseline_category_allows_same_value(self):
        """同值（含金额单位换算）不被拦：拦了才是真正的过度拦截。"""
        from app.services.consistency_backflow import plan_fact_merge

        items, added, skipped = plan_fact_merge(
            [{"key": "a", "label": "金额", "value": "1200万元", "category": "basic"}],
            [
                {
                    "fact_name": "合同价",
                    "value": "12000000元",
                    "category": "basic",
                    "conflict_id": "c1",
                }
            ],
        )
        assert added == 1
        assert skipped == []

    def test_non_baseline_category_allows_multiple(self):
        """非基准类别（basic 等）本就允许多条并存，审计器不拿它们互比。"""
        from app.services.consistency_backflow import plan_fact_merge

        items, added, skipped = plan_fact_merge(
            [{"key": "a", "label": "项目经理", "value": "张三", "category": "basic"}],
            [
                {
                    "fact_name": "项目经理",
                    "value": "李四",
                    "category": "basic",
                    "conflict_id": "c1",
                }
            ],
        )
        assert added == 1 and skipped == []

    def test_batch_internal_conflict_keeps_first_only(self):
        """同一批里两条同类别异值候选：只留第一条，第二条带原因返回。"""
        from app.services.consistency_backflow import plan_fact_merge

        items, added, skipped = plan_fact_merge(
            [],
            [
                {"fact_name": "质量标准", "value": "合格", "category": "quality", "conflict_id": "c1"},
                {"fact_name": "质量标准", "value": "优良", "category": "quality", "conflict_id": "c2"},
            ],
        )
        assert added == 1
        assert [it["value"] for it in items] == ["合格"]
        assert len(skipped) == 1 and skipped[0]["value"] == "优良"

    def test_legacy_two_tuple_contract_preserved(self):
        """`merge_fact_proposals` 的二元组契约不变（既有调用方与测试）。"""
        from app.services.consistency_backflow import merge_fact_proposals

        items, added = merge_fact_proposals(
            [], [{"fact_name": "工期", "value": "450", "conflict_id": "c1"}]
        )
        assert added == 1 and len(items) == 1

    def test_accept_returns_skipped_and_does_not_persist_conflict(self):
        """端到端：accept 回传 skipped，且冲突值不落库（不产生无法消解的冲突）。"""
        from app.services.consistency_backflow import accept_fact_proposals

        pid = _new_project("R6-R14-accept")
        with get_db() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO analysis (project_id, global_facts_json) VALUES (?, ?)",
                (
                    pid,
                    json.dumps(
                        [{"key": "a", "label": "工期", "value": "450", "category": "schedule"}],
                        ensure_ascii=False,
                    ),
                ),
            )

        result = accept_fact_proposals(
            pid,
            [
                {"fact_name": "工期", "value": "60", "category": "schedule", "conflict_id": "c1"},
                {"fact_name": "质量标准", "value": "合格", "category": "quality", "conflict_id": "c2"},
            ],
        )
        assert result["added"] == 1, "只有质量标准应入库"
        assert len(result["skipped"]) == 1
        assert "工期=450" in result["skipped"][0]["reason"]

        with get_db() as conn:
            row = conn.execute(
                "SELECT global_facts_json FROM analysis WHERE project_id = ?", (pid,)
            ).fetchone()
        stored = json.loads(row["global_facts_json"])
        assert [it["value"] for it in stored if it["category"] == "schedule"] == ["450"], (
            "工期不得出现两个互相冲突的取值"
        )

    def test_guard_matches_auditor_verdict(self):
        """结构约束：拦不拦必须与审计器的等价判定一致（单一判据）。"""
        from app.services.consistency_backflow import plan_fact_merge

        for existing_value, new_value, should_block in [
            ("450", "450", False),
            ("450", "60", True),
            ("450", "450 日历天", True),  # 审计器判为异值 → 一并入库必然互判冲突
        ]:
            _items, added, skipped = plan_fact_merge(
                [{"key": "a", "label": "工期", "value": existing_value, "category": "schedule"}],
                [
                    {
                        "fact_name": "工期",
                        "value": new_value,
                        "category": "schedule",
                        "conflict_id": "c1",
                    }
                ],
            )
            blocked = bool(skipped) or added == 0
            assert blocked is should_block, f"{existing_value} vs {new_value}"
            # 与审计器自身判定逐条对齐
            distinct = AUDITOR._distinct_values([existing_value, new_value], fact_name="工期")
            assert (len(distinct) > 1) is should_block


# ============================================================================
# R-13 收敛（遗留项闭环）：人员取值「抽取侧 ⟺ 闸门侧」口径一致
# ============================================================================


class TestPersonnelGateMatchesExtraction:
    """原 R-13（2026-10-11 记为「已知且接受」的遗留项）：autofix 的取值结构闸门
    比抽取正则宽，导致「检测看不见、修复却在改」——例如「共安排 5 人值守」对抽取
    不可见（动词表已按 R-1 收窄），对闸门却放行。

    修复后两侧共用 `consistency_rules` 的同一批正则片段：抽取用
    `PERSONNEL_FACT_PATTERN`，闸门用同片段编译的 `_PERSONNEL_CONTEXT_RE` 并按
    **取值字符跨度**精确比对。本组用例把「一致」钉成可执行断言（双向）：

      1. 抽取抽到的人员取值 → 闸门必须在同一位置放行；
      2. 闸门放行的位置 → 抽取必须抽得到同值同事实名（不得放宽）。

    第 2 条是真正的闭环：它同时禁止将来再以「放宽闸门」的方式悄悄退回旧行为。
    """

    #: 语料库里出现过的全部人员类事实名（通用桶 + 各岗位桶）
    FACT_NAMES = (
        rules.FACT_PERSONNEL,
        *[f"{role}数量" for role in rules.PERSONNEL_ROLE_PREFIXES],
    )

    @staticmethod
    def _occurrences(text: str, value: str) -> list[int]:
        from app.services import text_patch

        return list(text_patch.find_value_occurrences(text, value))

    @pytest.mark.parametrize(
        ("text", "expected"),
        [(t, e) for t, e in CORPUS],
        ids=[t[:24] if t else "<empty>" for t, _ in CORPUS],
    )
    def test_gate_and_extraction_agree_both_ways(self, text: str, expected):
        import re as _re

        extracted = [
            (name, value)
            for name, value in ((f.name, f.value) for f in AUDITOR.extract_facts_from_text(text))
            if rules.is_personnel_fact_name(name)
        ]
        expected_personnel = [
            (name, value) for name, value in expected if rules.is_personnel_fact_name(name)
        ]
        assert extracted == expected_personnel

        # 方向 1：抽得到 → 闸门放行（否则用户采纳建议后「没有可替换的位置」）
        for name, value in expected_personnel:
            occurrences = self._occurrences(text, value)
            assert occurrences, f"语料库期望 {name}={value}，但正文里找不到该取值：{text!r}"
            assert any(
                rules.fact_has_keyword_nearby(text, pos, pos + len(value), name)
                for pos in occurrences
            ), f"抽取抽到了 {name}={value}，闸门却在全部出现位置拒绝：{text!r}"

        # 方向 2：闸门放行 → 抽取抽得到（否则「检测看不见、修复却在改」回归）
        for match in _re.finditer(r"\d+", text):
            value, start, end = match.group(), match.start(), match.end()
            for name in self.FACT_NAMES:
                if not rules.fact_has_keyword_nearby(text, start, end, name):
                    continue
                assert (name, value) in expected_personnel, (
                    f"闸门放行了 {name}={value}（位置 {start}），抽取却抽不到：{text!r} "
                    "—— 这正是 R-13 的「检测看不见、修复却在改」"
                )

    @pytest.mark.parametrize(
        ("text", "fact_name", "expected"),
        [
            # 原 R-13 的裂缝形态：子集动词对抽取不可见，闸门此前却放行
            ("共安排 5 人值守", "人员数量", False),
            ("共参与 12 人投标评审", "人员数量", False),
            ("共计安排 10 人轮流值班", "人员数量", False),
            # 项目级投放动词：两侧都认
            ("共投入 30 人施工", "人员数量", True),
            ("合计 30 人到岗", "人员数量", True),
            # 岗位归属：岗位一致放行，跨岗位拒绝
            ("技术人员 20 人", "技术人员数量", True),
            ("施工人员约 12 人", "施工人员数量", True),
            ("管理人员是 5 人", "管理人员数量", True),
            ("技术人员 20 人", "人员数量", False),
            ("技术人员 20 人", "管理人员数量", False),
            # 工时单位与伤亡复合词：两侧都不认
            ("人工 12 人天", "人员数量", False),
            ("技术人员 5 人天", "技术人员数量", False),
            ("累计 5 人受伤", "人员数量", False),
            # 无关数字
            ("钢筋直径 20 mm。", "人员数量", False),
        ],
        ids=lambda v: str(v)[:20],
    )
    def test_gate_verdicts(self, text: str, fact_name: str, expected: bool):
        import re as _re

        match = _re.search(r"\d+", text)
        assert match is not None
        assert (
            rules.fact_has_keyword_nearby(text, match.start(), match.end(), fact_name)
            is expected
        ), text

    def test_gate_constants_share_single_source(self):
        """结构约束：抽取侧用的正则必须**就是** rules 的常量（防止再复制一份）。"""
        assert AUDITOR.FACT_PATTERNS["人员数量"] == rules.PERSONNEL_FACT_PATTERN
        assert rules.PERSONNEL_FACT_PATTERN == (
            rules.PERSONNEL_ROLE_PATTERN + "|" + rules.PERSONNEL_TOTAL_PATTERN
        )


# ============================================================================
# 增强：废标应答状态命名空间（REJECTION_STATUS_META 随 /rules 下发）
# ============================================================================


class TestRejectionStatusNamespace:
    """第三处「前端本地硬编码状态表」漂移的收口（严重度 2026-10-08、评分状态
    2026-10-13 之后，废标应答三档状态是漏网的最后一处）。"""

    def test_helper_maps_known_and_unknown(self):
        assert rules.rejection_status_meta("covered") == {"label": "已覆盖", "color": "green"}
        assert rules.rejection_status_meta("weak") == {"label": "覆盖较弱", "color": "orange"}
        assert rules.rejection_status_meta("empty") == {"label": "未应答", "color": "red"}
        # 未知状态回落中性标签，而不是伪装成某个真实状态
        assert rules.rejection_status_meta("brand_new_state") == {
            "label": "未知",
            "color": "default",
        }
        assert rules.rejection_status_meta("") == {"label": "未知", "color": "default"}

    def test_rules_payload_ships_rejection_status_meta(self):
        payload = rules.rules_payload()
        block = payload["rejection_coverage"]
        assert block["coverage_weak"] > 0
        assert block["status_meta"]["covered"]["label"] == "已覆盖"
        assert set(block["status_meta"]) == {"covered", "weak", "empty"}

    def test_rules_endpoint_serves_rejection_block(self):
        c = _client()
        r = c.get("/api/v1/consistency/rules")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok"
        assert body["rejection_coverage"]["status_meta"]["weak"]["label"] == "覆盖较弱"
        assert body["rejection_coverage"]["coverage_weak"] > 0
