"""一致性校核模块第 11 轮深度加固回归（2026-11-04）。

来源（本轮审计报告 `报告_一致性校核模块第11轮深度加固_2026-11-04.md`）：

- P1·双轨制失效：真实库基准值是裸短语（「617日历天」「一次性验收合格」），
  基准摄取走带标签正则一个都抽不出 → 已确认事实基准对审计器整体不可见，
  「正文写错工期也报不出冲突」。现按 `extract_baseline_fact_value`
  做无标签取值段兜底（基准值已确认/已剔占位，误抽面远小于正文）。
- B-1·回流采纳门误拦：`_find_baseline_conflict` 按原文归一判等，与审计器
  实际比对口径（先抽取再归一）相反 ——「基准 450日历天 vs 候选 450」被
  误判冲突拒绝采纳，docstring 声称「视为同值不拦」与实现相反。
- B-2·autofix 叠字污染：`replace_values` 对「已是目标写法」的出现位置
  仍按旧值前缀替换（「1200万元」→「1200万元元」），无报错误标已解决；
  以及带空格金额写法（「1200 万元」）字面定位失效 → autofix 对该写法
  整体修不动。现加「已达标后缀守卫」+ 空白容忍**针级**兜底定位。
- B-3·Agent 工作区死配置：`AGENT_REPAIR_WORKSPACE_SUBDIR` 声明「相对
  DATA_DIR」但 `_workspace_dir` 硬编码 `{backend}/data/agent_repair`。
- B-4·抽取漏报（语料库已扩，本文件补端到端联动）：金额高频标签
  （合同价格/合同总价/投标总价/中标价/总价）与「为/是/约」间隔词、
  资质「特级」、常见现场岗位（安全员/质检员等）此前整类漏抽。
- B-5·export 预检闸门纯读抢写锁（readonly 收口，源码契约钉住）。

运行：
    cd backend
    python -m pytest tests/test_consistency_round11_2026_11_04.py -q
"""
from __future__ import annotations

import json
from pathlib import Path

from app.db import get_db, init_db

OWNER = 1100


def _new_project(name: str) -> int:
    init_db()
    with get_db() as conn:
        conn.execute(
            "INSERT INTO projects (name, industry, created_by) VALUES (?, ?, ?)",
            (name, "通用", OWNER),
        )
        return conn.execute(
            "SELECT id FROM projects ORDER BY id DESC LIMIT 1"
        ).fetchone()["id"]


def _seed_section(pid: int, title: str, content: str, sort_order: int = 0) -> int:
    with get_db() as conn:
        conn.execute(
            "INSERT INTO sections (project_id, title, level, sort_order, content, status) "
            "VALUES (?, ?, 1, ?, ?, 'completed')",
            (pid, title, sort_order, content),
        )
        return conn.execute(
            "SELECT id FROM sections WHERE project_id = ? ORDER BY id DESC LIMIT 1",
            (pid,),
        ).fetchone()["id"]


def _set_global_facts(pid: int, items: list[dict]) -> None:
    with get_db() as conn:
        conn.execute(
            "INSERT INTO analysis (project_id, global_facts_json) VALUES (?, ?) "
            "ON CONFLICT(project_id) DO UPDATE SET global_facts_json = excluded.global_facts_json",
            (pid, json.dumps(items, ensure_ascii=False)),
        )


# ============================================================
# P1：基准裸值摄取（双轨制对真实库形态生效）
# ============================================================


class TestBaselineBareValueIngestion:
    def test_bare_schedule_value_is_visible(self):
        from app.services.consistency_auditor import ConsistencyAuditor

        auditor = ConsistencyAuditor()
        # 真实库实测样本（schedule 类别）：裸短语无标签词
        assert auditor.extract_baseline_fact_value("617日历天", "工期") == "617"
        assert auditor.extract_baseline_fact_value("180日历天", "工期") == "180"
        # 带标签基准（手工补录形态）与正文同口径
        assert auditor.extract_baseline_fact_value("总工期 450 天", "工期") == "450"

    def test_bare_quality_value_maps_to_closed_set(self):
        from app.services.consistency_auditor import ConsistencyAuditor

        auditor = ConsistencyAuditor()
        assert (
            auditor.extract_baseline_fact_value("一次性验收合格", "质量标准") == "合格"
        )
        assert auditor.extract_baseline_fact_value("合格", "质量标准") == "合格"

    def test_safety_phrase_baseline_stays_invisible_by_design(self):
        """安全标准裸短语基准**有意不摄取**（宁漏报不误报）。

        不同表述的兼容语句（基准「零死亡、零重伤」vs 正文
        「安全生产目标：杜绝重大伤亡事故」）会被短语级判异打成噪声冲突——
        恰是 2026-09-29 专项消除过的「安全标准噪声」类，不能借基准摄取绕回来。
        带标签的安全基准走标准抽取路径本就可见（下断言）。
        """
        from app.services.consistency_auditor import ConsistencyAuditor

        auditor = ConsistencyAuditor()
        assert auditor.extract_baseline_fact_value("零死亡", "安全标准") is None
        assert auditor.extract_baseline_fact_value("零死亡、零重伤、零重大安全事故", "安全标准") is None
        assert auditor.extract_baseline_fact_value("专职安全管理人员不少于3人", "安全标准") is None
        # 带标签形态可见（标准路径，与正文同口径）
        assert auditor.extract_baseline_fact_value("安全生产目标：零死亡", "安全标准") == "零死亡"
        # 超长描述性文本不当作取值（宁漏报不误报）
        long_text = "建立安全管理体系并落实各级安全生产责任制，确保施工全过程安全可控规范运行严格管理"
        assert auditor.extract_baseline_fact_value(long_text, "安全标准") is None

    def test_unmapped_fact_name_returns_none(self):
        from app.services.consistency_auditor import ConsistencyAuditor

        auditor = ConsistencyAuditor()
        # 基准类别之外的事实（金额/人员）不在裸值兜底表内
        assert auditor.extract_baseline_fact_value("1200万元", "金额") is None

    def test_full_audit_detects_content_vs_bare_baseline_conflict(self):
        """端到端：正文工期写错 → 与裸形态基准「617日历天」判冲突且建议以基准为目标。"""
        from app.services.consistency_auditor import consistency_auditor

        pid = _new_project("R11-基准裸值-冲突")
        _set_global_facts(pid, [{"key": "工期", "label": "工期", "value": "617日历天", "category": "schedule"}])
        _seed_section(pid, "第一节", "本工程总工期 600 日历天。", sort_order=0)
        _seed_section(pid, "第二节", "施工周期按 600 天控制。", sort_order=1)

        result = consistency_auditor.run_full_audit(pid)
        assert result.status in ("completed", "completed_no_conflicts")
        duration_conflicts = [c for c in result.conflicts if c.fact_name == "工期"]
        assert duration_conflicts, "正文 600 与基准 617 的矛盾必须被检出（双轨制对裸值基准生效）"
        conflict = duration_conflicts[0]
        assert set(conflict.values) == {"617", "600"}
        assert conflict.severity == "high", "工期是关键事实，一冲突即严重"
        assert any(s.startswith("global_facts") for s in conflict.sources), (
            "冲突来源必须包含已确认事实基准"
        )

    def test_full_audit_no_conflict_when_content_matches_bare_baseline(self):
        from app.services.consistency_auditor import consistency_auditor

        pid = _new_project("R11-基准裸值-一致")
        _set_global_facts(pid, [{"key": "工期", "label": "工期", "value": "617日历天", "category": "schedule"}])
        _seed_section(pid, "第一节", "本工程总工期 617 个日历天。", sort_order=0)
        _seed_section(pid, "第二节", "施工周期 617 日历天。", sort_order=1)

        result = consistency_auditor.run_full_audit(pid)
        duration_conflicts = [c for c in result.conflicts if c.fact_name == "工期"]
        assert not duration_conflicts, "正文与裸值基准一致（617）不得报冲突"


# ============================================================
# B-1：回流采纳门与审计器判等口径对齐
# ============================================================


class TestBackflowGateEquivalence:
    def test_equivalent_unit_forms_are_not_a_conflict(self):
        """「基准 450日历天 vs 候选 450」在审计器口径下同值 → 采纳门放行。"""
        from app.services.consistency_backflow import _find_baseline_conflict

        items = [
            {"key": "k1", "label": "工期", "value": "450日历天", "category": "schedule"}
        ]
        candidate = {
            "fact_name": "工期", "value": "450", "category": "schedule", "key": "audit_x"
        }
        assert _find_baseline_conflict(items, candidate) is None, (
            "审计器按抽取口径视为同值，采纳门不得误拦"
        )

    def test_real_different_value_still_blocked(self):
        from app.services.consistency_backflow import _find_baseline_conflict

        items = [
            {"key": "k1", "label": "工期", "value": "450日历天", "category": "schedule"}
        ]
        candidate = {
            "fact_name": "工期", "value": "60", "category": "schedule", "key": "audit_y"
        }
        conflict_with = _find_baseline_conflict(items, candidate)
        assert conflict_with is not None, "真异值（450 vs 60）必须仍被拦截"
        assert conflict_with["value"] == "450日历天"

    def test_value_equivalent_both_directions(self):
        from app.services.consistency_backflow import _value_equivalent

        assert _value_equivalent("450", "450日历天", "工期") is True
        assert _value_equivalent("450日历天", "450", "工期") is True
        assert _value_equivalent("617", "617日历天", "工期") is True
        assert _value_equivalent("60", "450日历天", "工期") is False
        assert _value_equivalent("", "450日历天", "工期") is False

    def test_plan_fact_merge_r14_still_blocks_real_conflicts(self):
        """R-14 基准类别互斥对真异值保持拦截（不因口径对齐而放松）。"""
        from app.services.consistency_backflow import plan_fact_merge

        items, added, skipped = plan_fact_merge(
            [{"key": "k1", "label": "工期", "value": "450日历天", "category": "schedule"}],
            [
                {"fact_name": "工期", "value": "450", "category": "schedule",
                 "key": "audit_a", "suggested_key": "audit_a"},
                {"fact_name": "工期", "value": "60", "category": "schedule",
                 "key": "audit_b", "suggested_key": "audit_b"},
            ],
        )
        assert added == 1, "与基准同值的候选应被采纳"
        assert [(s["fact_name"], s["value"]) for s in skipped] == [("工期", "60")], (
            "与基准异值的候选仍须被 R-14 拦截并带可读原因"
        )


# ============================================================
# B-2：autofix 叠字守卫 + 空白容忍针级兜底
# ============================================================


class TestReplaceValuesGuard:
    def test_already_target_form_not_double_replaced(self):
        """「已是目标写法」的出现位置跳过，不再产出「1200万元元」。"""
        from app.services.text_patch import replace_values

        result = replace_values("总额 1200万元。", {"1200万": "1200万元"})
        assert result["content"] == "总额 1200万元。"
        assert result["errors"] == []

    def test_suffix_addition_skips_equivalent_unit_tail(self):
        """补单位形态（450→450天）：已带「日历天」处跳过，裸数字处补齐。"""
        from app.services.text_patch import replace_values

        result = replace_values(
            "工期 450 日历天；备用工期 450。", {"450": "450天"}
        )
        assert result["content"] == "工期 450 日历天；备用工期 450天。"

    def test_spaced_amount_located_and_replaced_whole_span(self):
        """带空格金额（「1200 万元」）此前字面定位失效；现整跨度替换不残留半截单位。"""
        from app.services.text_patch import replace_values

        result = replace_values(
            "合同价 1200 万元，造价 12000000 元。",
            {"1200万元": "1300万元", "12000000元": "1300万元"},
        )
        assert result["content"] == "合同价 1300万元，造价 1300万元。"

    def test_spaced_shorthand_amount(self):
        from app.services.text_patch import replace_values

        result = replace_values("总额 800 万", {"800万": "900万"})
        assert result["content"] == "总额 900万"

    def test_digit_boundary_still_holds_for_tolerant_spans(self):
        """容忍跨度同样受数字边界保护：「11200 万元」不被「1200万元」吃进。"""
        from app.services.text_patch import replace_values

        result = replace_values("共 11200 万元资产", {"1200万元": "1300万元"})
        assert result["content"] == "共 11200 万元资产"
        assert result["applied"] == []

    def test_literal_overlap_still_rejected_whole_batch(self):
        """两个取值都能**字面**命中且区间重叠 → 依旧整批拒绝（round6 E3 契约）。"""
        from app.services.text_patch import replace_values

        result = replace_values("本工程工期 20天。", {"20": "30", "20天": "30"})
        assert result["changed"] is False
        assert result["applied"] == []
        assert result["errors"], "字面重叠必须整批拒绝"

    def test_plain_value_swap_unaffected(self):
        from app.services.text_patch import replace_values

        result = replace_values("工期 460 日历天", {"460": "450"})
        assert result["content"] == "工期 450 日历天"

    def test_autofix_route_fixes_spaced_amount_end_to_end(self):
        """E2E：冲突值带空格写法（「1200 万元」）的章节可被 autofix 真实落文。"""
        from fastapi.testclient import TestClient

        from app.main import app
        from app.security import auth

        app.dependency_overrides[auth.get_current_user] = lambda: auth.User(OWNER, "u", "user")
        pid = _new_project("R11-带空格金额-autofix")
        sec = _seed_section(pid, "造价", "合同价 1200 万元。", sort_order=0)
        with get_db() as conn:
            conn.execute(
                "INSERT INTO consistency_conflicts "
                "(project_id, conflict_id, fact_name, values_json, sources_json, "
                " severity, description, created_at) "
                "VALUES (?, ?, '金额', ?, ?, 'high', '金额存在2种不同表述', datetime('now','localtime'))",
                (
                    pid,
                    f"conflict_{pid}_金额_r11",
                    json.dumps(["1200万元", "1300万元"], ensure_ascii=False),
                    json.dumps([f"section_{sec} (造价)"], ensure_ascii=False),
                ),
            )
            cid = conn.execute(
                "SELECT conflict_id FROM consistency_conflicts WHERE project_id = ?",
                (pid,),
            ).fetchone()["conflict_id"]

        with TestClient(app) as client:
            resp = client.post(
                f"/api/v1/consistency/conflicts/{cid}/autofix",
                params={"project_id": pid, "new_value": "1300万元"},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok", body
        assert body["changed_sections"] == 1
        with get_db(readonly=True) as conn:
            content = conn.execute(
                "SELECT content FROM sections WHERE id = ?", (sec,)
            ).fetchone()["content"]
        assert content == "合同价 1300万元。"


# ============================================================
# B-3：Agent 工作区目录接配置（死配置复活 + 路径口径归 DATA_DIR）
# ============================================================


class TestAgentWorkspaceDir:
    def test_explicit_base_still_honored(self, tmp_path):
        from app.services.agent_repair import _workspace_dir

        got = _workspace_dir(42, base=tmp_path)
        assert got == tmp_path / "42"

    def test_default_reads_config(self, monkeypatch, tmp_path):
        import app.config as config
        from app.services import agent_repair

        monkeypatch.setattr(config, "DATA_DIR", tmp_path)
        monkeypatch.setattr(config, "AGENT_REPAIR_WORKSPACE_SUBDIR", "agent_ws")
        got = agent_repair._workspace_dir(7)
        assert got == tmp_path / "agent_ws" / "7", (
            "默认路径必须是 {DATA_DIR}/{AGENT_REPAIR_WORKSPACE_SUBDIR}/{pid}（配置声明口径）"
        )


# ============================================================
# B-4：抽取加宽的端到端联动（语料库之外补冲突检测级断言）
# ============================================================


class TestWidenedExtractionEndToEnd:
    def test_site_role_headcount_conflict_detected(self):
        """「安全员 2 人 vs 3 人」跨章矛盾必须被检出（此前整类漏抽）。"""
        from app.services.consistency_auditor import ConsistencyAuditor

        pid = _new_project("R11-岗位分桶-安全员")
        _seed_section(pid, "甲", "现场安全员 2 人。", sort_order=0)
        _seed_section(pid, "乙", "安全员 3 人。", sort_order=1)
        conflicts = [
            c for c in ConsistencyAuditor().detect_conflicts(pid)
            if c.fact_name == "安全员数量"
        ]
        assert conflicts, "安全员人数跨章矛盾必须被检出"
        assert set(conflicts[0].values) == {"2", "3"}

    def test_total_price_label_conflict_detected(self):
        """「合同总价 980 万元 vs 1000 万元」跨章矛盾必须被检出。"""
        from app.services.consistency_auditor import ConsistencyAuditor

        pid = _new_project("R11-金额标签-总价")
        _seed_section(pid, "甲", "合同总价 980 万元。", sort_order=0)
        _seed_section(pid, "乙", "合同总价 1000 万元。", sort_order=1)
        conflicts = [
            c for c in ConsistencyAuditor().detect_conflicts(pid)
            if c.fact_name == "金额"
        ]
        assert conflicts, "总价族标签的金额矛盾必须被检出"
        # 单位归一后判等：980万 vs 1000万 是真异值
        assert len(ConsistencyAuditor._distinct_values(conflicts[0].values, fact_name="金额")) == 2

    def test_special_grade_qualification_extracted(self):
        """「施工总承包特级」关键事实必须可抽取（此前整类漏抽）。"""
        from app.services.consistency_auditor import ConsistencyAuditor

        facts = ConsistencyAuditor().extract_facts_from_text("企业资质：施工总承包特级")
        assert ("资质等级", "特级") in [(f.name, f.value) for f in facts]


# ============================================================
# B-5：导出预检一致性闸门纯读走 readonly（源码契约钉住）
# ============================================================


class TestExportGateReadonlySourceContract:
    def test_high_conflict_gate_uses_readonly_connection(self):
        """闸门两条纯读 SELECT 必须用 get_db(readonly=True)，不抢全局写锁。"""
        source = Path(__file__).resolve().parents[1] / "app" / "services" / "export.py"
        text = source.read_text(encoding="utf-8")
        anchor = text.find("RESOLVED_SQL_OPEN")
        assert anchor > 0, "导出预检应引用 consistency_rules.RESOLVED_SQL_OPEN"
        with_pos = text.find("with get_db(", anchor)
        assert with_pos > 0, "闸门应通过 with get_db(...) 取连接"
        line_end = text.find("\n", with_pos)
        with_line = text[with_pos:line_end]
        assert "readonly=True" in with_line, (
            "一致性闸门（block_on_high_conflicts）的纯读 SELECT 应使用 readonly 连接"
        )


# ============================================================
# 端到端：事实回流链路（扫描 → 采纳）对等价/异值候选的新契约
# ============================================================


class TestBackflowRouteE2E:
    def _seed_project_with_conflict(
        self, name: str, baseline_value: str | None, sections: list[str], conflict_values: list[str]
    ) -> int:
        pid = _new_project(name)
        if baseline_value is not None:
            _set_global_facts(
                pid,
                [{"key": "工期", "label": "工期", "value": baseline_value, "category": "schedule"}],
            )
        sources = []
        for idx, content in enumerate(sections):
            sid = _seed_section(pid, f"第{idx + 1}节", content, sort_order=idx)
            sources.append(f"section_{sid} (第{idx + 1}节)")
        with get_db() as conn:
            conn.execute(
                "INSERT INTO consistency_conflicts "
                "(project_id, conflict_id, fact_name, values_json, sources_json, "
                " severity, description, created_at) "
                "VALUES (?, ?, '工期', ?, ?, 'high', '工期存在2种不同表述', datetime('now','localtime'))",
                (
                    pid,
                    f"conflict_{pid}_工期_r11e2e",
                    json.dumps(conflict_values, ensure_ascii=False),
                    json.dumps(sources, ensure_ascii=False),
                ),
            )
        return pid

    def test_equivalent_winner_not_proposed_and_real_winner_adoptable(self):
        """等价候选（与基准同值异写法）不进候选清单；无基准时真异值候选可采纳。"""
        from fastapi.testclient import TestClient

        from app.main import app
        from app.security import auth

        app.dependency_overrides[auth.get_current_user] = lambda: auth.User(OWNER, "u", "user")
        # 基准 450日历天；冲突值 ["450","460"]，正文里 450 出现 3 次、460 出现 1 次
        # → winner=450 与基准等价 → 不应提出；本用例验证扫描结果不含该候选。
        pid = self._seed_project_with_conflict(
            "R11-回流E2E",
            baseline_value="450日历天",
            sections=[
                "本工程总工期 450 日历天。",
                "施工周期按 450 天控制，节点工期 450 天内完成。",
                "备注：个别段落写 460 天。",
            ],
            conflict_values=["450", "460"],
        )
        with TestClient(app) as client:
            scan = client.get(f"/api/v1/consistency/facts/proposals/{pid}", params={"min_occurrences": 1})
            assert scan.status_code == 200
            body = scan.json()
            proposed = [p["value"] for p in body.get("proposals", [])]
            assert "450" not in proposed, (
                f"与基准 450日历天 审计口径等价的候选 450 不应再被提出（现提出：{proposed}）"
            )

        # 无基准场景：冲突值 ["60","450"]，正文 60 占多数 → 60 被提出并采纳入库；
        # （若已有基准 450日历天 在场，异值候选 60 会被 R-14 正确拦截 —— 见
        #  TestBackflowGateEquivalence::test_plan_fact_merge_r14_still_blocks_real_conflicts）
        pid2 = self._seed_project_with_conflict(
            "R11-回流E2E-无基准",
            baseline_value=None,
            sections=[
                "本工程总工期 60 日历天。",
                "施工周期按 60 天控制。",
                "备注：历史段落写 450 天。",
            ],
            conflict_values=["60", "450"],
        )
        with TestClient(app) as client:
            scan2 = client.get(
                f"/api/v1/consistency/facts/proposals/{pid2}", params={"min_occurrences": 1}
            )
            assert scan2.status_code == 200
            proposals2 = scan2.json().get("proposals", [])
            assert proposals2, "无基准时 winner（60）应被提出为候选"
            target = next(p for p in proposals2 if p["value"] == "60")
            accept = client.post(
                f"/api/v1/consistency/facts/proposals/{pid2}/accept",
                json={"proposals": [target]},
            )
            assert accept.status_code == 200
            accept_body = accept.json()
            assert accept_body.get("added") == 1, accept_body
            assert accept_body.get("skipped") == []
        with get_db(readonly=True) as conn:
            facts_json = conn.execute(
                "SELECT global_facts_json FROM analysis WHERE project_id = ?", (pid2,)
            ).fetchone()["global_facts_json"]
        items = json.loads(facts_json)
        assert any(
            str(it.get("value")) == "60" and it.get("category") == "schedule"
            for it in items
        ), "采纳后 60 应进入全局事实基准（schedule 类别）"
