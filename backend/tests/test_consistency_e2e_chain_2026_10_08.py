"""一致性校核 —— 端到端工作流贯通验证（2026-10-08）

用真实 HTTP 接口串起完整链路，验证「工作流程贯通 + 数据链完整 + 前后端同步」：

    建项目/章节 → 全局事实基准入库 → 跑审计 → 读冲突(默认未解决)
    → 取修复建议(结构化 target_value) → 采纳并真正落文 → 读冲突(已解决)
    → 读摘要(口径一致) → 评分达标对齐 → 规则快照

关键不变量（都是本轮修掉的缺陷所在）：
- 审计成功后 `status='ok'` 且 `audit_status` 为成功态；facts_extracted 真实非 0
- 冲突默认只回未解决；修复后该冲突转入 resolved
- **正文真的被改写了**（不是只把冲突标成已解决 —— 2026-09-14 的老 BUG）
- 摘要口径与冲突列表口径一致（sum(by_severity) == unresolved）
- 修复建议的 target_value 是纯值，不是说明文案
"""
from __future__ import annotations

import json

from fastapi.testclient import TestClient

from app.db import get_db, init_db
from app.security.auth import User


API = "/api/v1"
OWNER = 3600


def _client():
    from app.main import app
    from app.security.auth import get_current_user

    app.dependency_overrides[get_current_user] = lambda: User(OWNER, "u", "user")
    return TestClient(app)


def _new_project(name: str) -> int:
    init_db()
    with get_db() as conn:
        conn.execute(
            "INSERT INTO projects (name, industry, created_by) VALUES (?, '通用', ?)",
            (name, OWNER),
        )
        return conn.execute("SELECT id FROM projects ORDER BY id DESC LIMIT 1").fetchone()["id"]


def _seed_section(pid: int, *, title: str, content: str, sort_order: int = 0) -> int:
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO sections (project_id, title, level, sort_order, content, status, section_type) "
            "VALUES (?, ?, 1, ?, ?, 'completed', 'normal')",
            (pid, title, sort_order, content),
        )
        return cur.lastrowid


def test_consistency_end_to_end_chain():
    client = _client()
    try:
        # ---------- 1. 造一个「工期基准 450 天，正文有 450 和 500」的项目 ----------
        pid = _new_project("E2E-一致性贯通")
        with get_db() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO analysis (project_id, global_facts_json, scoring_points_json) "
                "VALUES (?, ?, ?)",
                (
                    pid,
                    json.dumps(
                        {
                            "items": [
                                # 值里必须带「工期」关键词：基准值要走与正文同一套抽取口径
                                # （extract_facts_from_text），裸 "450天" 抽不出事实
                                {
                                    "category": "schedule",
                                    "label": "建设工期",
                                    "value": "建设工期450天",
                                }
                            ]
                        },
                        ensure_ascii=False,
                    ),
                    json.dumps(
                        [
                            {
                                "id": "R1",
                                "name": "施工组织设计",
                                "weight": "10",
                                "standard": "须包含施工工艺流程图并附 BIM 模型",
                            }
                        ],
                        ensure_ascii=False,
                    ),
                ),
            )
        sec_ok = _seed_section(pid, title="概述", content="本项目建设工期：450天。", sort_order=0)
        sec_bad = _seed_section(pid, title="进度计划", content="计划工期：500天完成主体。", sort_order=1)

        # ---------- 2. 规则快照 ----------
        r = client.get(f"{API}/consistency/rules")
        assert r.status_code == 200, r.text
        rules = r.json()
        assert rules["severity"]["order"] == ["high", "medium", "low"]
        assert rules["severity"]["meta"]["high"]["label"] == "严重"
        # 与前端 utils.ts 静态兜底一致（前端在无法拉取时使用它）
        assert rules["severity"]["meta"]["high"]["color"] == "red"
        assert rules["severity"]["meta"]["medium"]["label"] == "中等"
        assert rules["severity"]["meta"]["low"]["color"] == "blue"

        # ---------- 3. 跑审计 ----------
        r = client.get(f"{API}/consistency/audit/{pid}")
        assert r.status_code == 200, r.text
        audit = r.json()
        assert audit["status"] == "ok"
        assert audit["audit_status"] in {"completed", "completed_no_conflicts"}
        assert audit["facts_extracted"] >= 2, "事实抽取数应真实透传，而非恒 0"
        assert audit["conflicts_found"] >= 1
        conflicts_in_audit = audit["conflicts"]
        assert any(c["fact_name"] == "工期" for c in conflicts_in_audit)
        # 严重度必须是合法取值（响应层已归一化）
        assert all(c["severity"] in {"high", "medium", "low"} for c in conflicts_in_audit)

        # ---------- 4. 读冲突（默认仅未解决） ----------
        r = client.get(f"{API}/consistency/conflicts/{pid}")
        assert r.status_code == 200, r.text
        listing = r.json()
        assert listing["filter"] == "open"
        assert listing["count"] == len(listing["conflicts"])
        assert all(c["state"] == "open" for c in listing["conflicts"])

        conflict = next(c for c in listing["conflicts"] if c["fact_name"] == "工期")
        # 排序：high 在最前（本次无脏数据时）
        assert listing["conflicts"][0]["severity"] == "high"
        # 来源应含全局事实基准与两个章节，供前端深链
        assert any("global_facts" in s for s in conflict["sources"])
        assert any(f"section_{sec_bad}" in s for s in conflict["sources"])

        # ---------- 5. 修复建议：结构化 target_value ----------
        r = client.get(
            f"{API}/consistency/conflicts/{conflict['conflict_id']}/fix-suggestion",
            params={"project_id": pid},
        )
        assert r.status_code == 200, r.text
        suggestion = r.json()
        assert suggestion["target_value"] == "450", "目标值应取已确认的全局事实基准"
        assert "建议将以下内容统一为" in suggestion["suggestion"]

        # ---------- 6. dry_run 预览：不得改任何正文 ----------
        before = {
            r_["id"]: r_["content"]
            for r_ in _rows(pid)
        }
        r = client.post(
            f"{API}/consistency/conflicts/{conflict['conflict_id']}/autofix",
            params={"project_id": pid, "new_value": suggestion["target_value"], "dry_run": True},
        )
        assert r.status_code == 200, r.text
        preview = r.json()
        assert preview["status"] == "ok"
        assert preview["dry_run"] is True
        assert preview["total_replacements"] >= 1
        after_preview = {r_["id"]: r_["content"] for r_ in _rows(pid)}
        assert after_preview == before, "dry_run 不得改写正文"

        # ---------- 7. 真正落文 ----------
        r = client.post(
            f"{API}/consistency/conflicts/{conflict['conflict_id']}/autofix",
            params={"project_id": pid, "new_value": suggestion["target_value"], "dry_run": False},
        )
        assert r.status_code == 200, r.text
        fix = r.json()
        assert fix["status"] == "ok", fix.get("message")
        assert fix["success"] is True
        assert fix["changed_sections"] >= 1
        assert fix["total_replacements"] >= 1

        # 关键：正文**真的**被改了（老 BUG 只标 resolved 不改正文）
        after = {r_["id"]: r_["content"] for r_ in _rows(pid)}
        assert after != before, "autofix 未改写任何正文 —— 谎报修复"
        assert "500天" not in after[sec_bad], f"旧值仍在正文：{after[sec_bad]}"
        assert "450天" in after[sec_bad], f"新值未落入正文：{after[sec_bad]}"

        # ---------- 8. 冲突状态收敛 ----------
        r = client.get(f"{API}/consistency/conflicts/{pid}")
        assert r.json()["count"] == 0, "冲突修复后应从默认列表消失"
        r = client.get(f"{API}/consistency/conflicts/{pid}?resolved=resolved")
        resolved = r.json()
        assert resolved["count"] == 1
        assert resolved["conflicts"][0]["conflict_id"] == conflict["conflict_id"]
        assert resolved["conflicts"][0]["state"] == "resolved"
        assert resolved["conflicts"][0]["resolved"] == 1
        assert resolved["conflicts"][0]["resolved_value"] == "450"
        r = client.get(f"{API}/consistency/conflicts/{pid}?resolved=all")
        assert r.json()["count"] == 1

        # ---------- 9. 摘要口径与冲突列表一致 ----------
        summary = client.get(f"{API}/consistency/summary/{pid}").json()["summary"]
        assert summary["unresolved_conflicts"] == 0
        assert summary["resolved_conflicts"] == 1
        # 已解决的冲突不得再出现在严重度卡片里
        assert summary["conflicts_by_severity"] == {}
        assert summary["latest_audit"] is not None
        assert summary["latest_audit"]["status"] in {"completed", "completed_no_conflicts"}

        # ---------- 10. 重跑审计应幂等（不膨胀） ----------
        r = client.get(f"{API}/consistency/audit/{pid}")
        assert r.status_code == 200
        after_rerun = r.json()
        assert after_rerun["conflicts_found"] == 0, "统一后重跑不应再有冲突"
        with get_db(readonly=True) as conn:
            total = conn.execute(
                "SELECT COUNT(*) c FROM consistency_conflicts WHERE project_id = ?", (pid,)
            ).fetchone()["c"]
        assert total == 1, f"重跑审计导致冲突记录膨胀：{total} 行"

        # ---------- 11. 评分标准达标对齐（此前无 UI 的能力） ----------
        r = client.get(f"{API}/consistency/scoring-compliance/{pid}?persist=false")
        assert r.status_code == 200, r.text
        scoring = r.json()
        assert scoring["status"] == "ok"
        assert scoring["total_points"] == 1
        assert scoring["unmapped"] == 1, "没有任何章节挂 R1，应判未挂载"
        gap = scoring["gaps"][0]
        assert gap["scoring_point_id"] == "R1"
        assert gap["status"] == "unmapped"
        assert gap["detail"]

        # 挂上章节后转为 weak/covered，而不是继续 unmapped
        with get_db() as conn:
            conn.execute(
                "UPDATE sections SET scoring_point_id = 'R1' WHERE project_id = ? AND id = ?",
                (pid, sec_bad),
            )
        r = client.get(f"{API}/consistency/scoring-compliance/{pid}?persist=false")
        scoring2 = r.json()
        assert scoring2["unmapped"] == 0
        assert scoring2["empty"] + scoring2["weak"] + scoring2["covered"] == 1
        # 正文「计划工期：450天完成主体」未提 BIM/流程图 → 覆盖率很低
        assert scoring2["gaps"][0]["status"] in {"weak", "empty"}
        assert scoring2["gaps"][0]["coverage"] < 0.25

        # ---------- 12. 审计历史留痕 ----------
        with get_db(readonly=True) as conn:
            logs = conn.execute(
                "SELECT COUNT(*) c FROM consistency_audit_log WHERE project_id = ?", (pid,)
            ).fetchone()["c"]
        assert logs == 2, f"两次审计应留两条日志，实际 {logs}"
    finally:
        client.app.dependency_overrides.clear()


def _rows(pid: int):
    with get_db(readonly=True) as conn:
        return conn.execute(
            "SELECT id, content FROM sections WHERE project_id = ? ORDER BY sort_order, id", (pid,)
        ).fetchall()