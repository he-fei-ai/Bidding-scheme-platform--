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

from app.db import get_db, init_db
from app.services import consistency_rules as rules
from app.services.consistency_auditor import consistency_auditor
from app.services.consistency_repair import refresh_conflict_states


OWNER = 3501


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
