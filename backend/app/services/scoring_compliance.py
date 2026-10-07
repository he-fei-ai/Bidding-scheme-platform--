"""评分标准达标对齐审计（Scoring Standard Compliance）—— 2026-10-06 新增

## 为什么需要它

导出预检里已有的评分点检查（`export.check_document` → `uncovered_scoring_points`）
只做**存在性**判断：

    评分点集合 − 挂了 scoring_point_id 的章节集合 = 未覆盖

也就是说，只要目录里**挂上了**这个评分点，预检就认为它被覆盖了 —— 完全不看
章节正文里到底写没写评分标准要求的东西。于是下面这种情形会被判为「全覆盖」：

    评分点 R1「施工组织设计」标准：须包含施工工艺流程图并附 BIM 模型
    章节「施工组织设计」正文：本文介绍施工工艺。        ← 什么都没答

投标文件丢分点恰恰在这里，而系统此前**没有任何环节**能发现它。

## 本模块做什么

在既有 `sections.scoring_point_id` 映射（目录生成时落库，`sse_db_ops.py:623`）
之上，加一层**内容覆盖度**判定：把评分标准的词项与章节正文做重合度计算，
给出 `unmapped / empty / weak / covered` 四态，并把**原始覆盖率数字**一并
返回，让使用者自行判断阈值。

## 设计取舍（重要）

1. **确定性、不调 AI**：纯字符 n-gram 重合度，零 AI 花费、零超时、可复现，
   也能在导出预检里低成本复用。语义级「是否真的答到了点子上」交给后续
   可选的 AI 层，不混进这个确定性基线里。
2. **零假阳性优先**（本仓一贯取舍）：`weak` 阈值取得很低（默认 0.10），
   只在「评分标准用词几乎完全没出现」时才告警；宁可漏报也不误报。
   原始 coverage 数字始终返回，用户可自行按更严标准解读。
3. **必须先 strip 伪评分项**：`extract_scoring_points` 在权重异常时会插入
   `__weight_warning__` 伪项（见 `utils/scoring_points.py`），不剔除会凭空
   多出「1 个评分点未达标」。
4. **零迁移**：结果表 `scoring_compliance_audit` 走 `db.init_db` 里的
   `CREATE TABLE IF NOT EXISTS`，不需要独立迁移脚本；读取时对缺列做降级。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime

from ..utils.scoring_points import strip_scoring_placeholders

# ===== 阈值（集中在此，便于按项目调严/调松）=====

#: 章节正文短于此长度视为「有章节但无实质正文」
MIN_CONTENT_CHARS = 50

#: 覆盖率低于此值 → weak（疑似未响应评分标准）
#:
#: 标定依据（2026-10-06 实测，非拍脑袋）：
#:   - 真正答到的章节：约 0.9（评分标准用词基本都出现在正文里）
#:   - 纯泛泛而谈、只蹭到通用词（如「施工工艺」）但完全没答流程图/BIM：约 0.21
#:   - 两者之间有很宽的空白带，取 0.25 落在这条带子里：既能抓住「挂了章节
#:     却没答到点上」，又远低于「换个说法作答」的水平。
#: 初版取 0.10 过于宽松，实测被上述 0.21 的反例直接逃过告警，故上调。
#: 需要更严/更松时调 `audit_scoring_compliance(coverage_weak=...)` 或请求参数。
COVERAGE_WEAK = 0.25

#: 覆盖率高于此值 → covered（仅作展示分级，不告警）
COVERAGE_COVERED = 0.35

_CJK = r"一-鿿"
_ASCII_WORD = re.compile(r"[A-Za-z][A-Za-z0-9_\-]{1,}")
_NON_MEANINGFUL = re.compile(rf"[^{_CJK}A-Za-z0-9_\-]+")


def _normalize(text: str) -> str:
    """去掉标点/空白，只保留中英文与数字，避免标点差异干扰重合度。"""
    return _NON_MEANINGFUL.sub("", text or "").lower()


def _terms(text: str) -> set[str]:
    """抽取用于重合度比较的词项集合。

    中文按**字符三元组**切（无需引入分词器依赖，且对「同义不同词」比
    二元组更宽容）；英文/型号按词切（`BIM`、`MVD`、型号编码等）。
    """
    norm = _normalize(text)
    out: set[str] = set()
    for word in _ASCII_WORD.findall(text or ""):
        w = word.lower()
        if len(w) >= 2:
            out.add(w)
    cjk_chars = re.findall(rf"[{_CJK}]+", norm)
    for run in cjk_chars:
        if len(run) == 1:
            out.add(run)
            continue
        if len(run) == 2:
            out.add(run)
            continue
        for i in range(len(run) - 2):
            out.add(run[i : i + 3])
    return out


def _coverage(standard: str, content: str) -> float:
    """评分标准词项在章节正文中出现的比例（0.0 ~ 1.0）。"""
    std_terms = _terms(standard)
    if not std_terms:
        return 1.0  # 没有可比对的标准文本，视为不构成缺口
    content_norm = _normalize(content)
    if not content_norm:
        return 0.0
    hit = 0
    for t in std_terms:
        if len(t) <= 2:
            if t in content_norm:
                hit += 1
        elif t in content_norm:
            hit += 1
    return hit / len(std_terms)


@dataclass
class ScoringGap:
    """单个评分点的达标情况"""

    scoring_point_id: str
    name: str
    weight: str = ""
    standard: str = ""
    status: str = "covered"  # unmapped | empty | weak | covered
    coverage: float = 0.0
    section_ids: list[int] = field(default_factory=list)
    section_titles: list[str] = field(default_factory=list)
    content_chars: int = 0
    detail: str = ""

    def to_dict(self) -> dict:
        return {
            "scoring_point_id": self.scoring_point_id,
            "name": self.name,
            "weight": self.weight,
            "standard": self.standard,
            "status": self.status,
            "coverage": round(self.coverage, 4),
            "section_ids": self.section_ids,
            "section_titles": self.section_titles,
            "content_chars": self.content_chars,
            "detail": self.detail,
        }


@dataclass
class ComplianceReport:
    project_id: int
    total_points: int = 0
    #: 全部评分点的判定结果（含 covered），计数与统计都基于它
    all_points: list[ScoringGap] = field(default_factory=list)
    audit_time: str = ""
    status: str = "completed"
    warnings: list[str] = field(default_factory=list)

    @property
    def gaps(self) -> list[ScoringGap]:
        """只含非 covered 项，便于直接当「问题清单」展示。"""
        return [g for g in self.all_points if g.status != "covered"]

    def by_status(self, status: str) -> list[ScoringGap]:
        return [g for g in self.all_points if g.status == status]

    def to_dict(self) -> dict:
        # 注意：这里用 audit_status 而不是 status —— 路由层返回体最外层已有
        # status='ok' 表示 HTTP 层成功，同名键会互相覆盖（曾把 'ok' 覆盖成
        # 'completed'，导致前端无法区分「请求成功」与「审计完成」）。
        return {
            "project_id": self.project_id,
            "audit_time": self.audit_time,
            "audit_status": self.status,
            "total_points": self.total_points,
            "unmapped": len(self.by_status("unmapped")),
            "empty": len(self.by_status("empty")),
            "weak": len(self.by_status("weak")),
            "covered": len(self.by_status("covered")),
            "gaps": [g.to_dict() for g in self.gaps],
            "warnings": self.warnings,
        }


def load_scoring_points(project_id: int) -> list[dict]:
    """从 analysis.scoring_points_json 读取评分点（已剔除伪项）。"""
    from ..db import get_db

    with get_db(readonly=True) as conn:
        try:
            row = conn.execute(
                "SELECT scoring_points_json FROM analysis WHERE project_id = ?",
                (project_id,),
            ).fetchone()
        except Exception:
            return []
    if not row or not row["scoring_points_json"]:
        return []
    try:
        parsed = json.loads(row["scoring_points_json"])
    except (json.JSONDecodeError, TypeError):
        return []
    if isinstance(parsed, dict):
        for key in ("scoring_points", "points", "items"):
            if isinstance(parsed.get(key), list):
                parsed = parsed[key]
                break
    if not isinstance(parsed, list):
        return []
    cleaned = strip_scoring_placeholders(parsed)
    return [p for p in cleaned if isinstance(p, dict) and p.get("id")]


def _load_sections(project_id: int) -> list[dict]:
    from ..db import get_db

    with get_db(readonly=True) as conn:
        rows = conn.execute(
            "SELECT id, title, content, scoring_point_id FROM sections "
            "WHERE project_id = ? ORDER BY level, sort_order, id",
            (project_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def audit_scoring_compliance(
    project_id: int,
    *,
    coverage_weak: float = COVERAGE_WEAK,
    coverage_covered: float = COVERAGE_COVERED,
    min_content_chars: int = MIN_CONTENT_CHARS,
) -> ComplianceReport:
    """对项目做评分标准达标对齐审计（确定性，不调 AI）。

    Args:
        project_id: 项目 id
        coverage_weak: 低于此覆盖率判 weak（默认 `COVERAGE_WEAK`）
        coverage_covered: 高于此覆盖率判 covered（仅展示分级）
        min_content_chars: 章节正文短于此长度视为 empty

    Returns:
        ComplianceReport（`gaps` 里只含非 covered 项，便于直接展示问题）
    """
    report = ComplianceReport(
        project_id=project_id,
        audit_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    )
    points = load_scoring_points(project_id)
    report.total_points = len(points)
    if not points:
        report.warnings.append("该项目的 analysis.scoring_points_json 为空或全部为伪评分项，无法做达标对齐")
        return report

    sections = _load_sections(project_id)
    by_sp: dict[str, list[dict]] = {}
    for s in sections:
        sp_id = (s.get("scoring_point_id") or "").strip()
        if sp_id:
            by_sp.setdefault(sp_id, []).append(s)

    for p in points:
        sp_id = str(p.get("id") or "").strip()
        name = str(p.get("name") or "")
        standard = str(p.get("standard") or "")
        mapped = by_sp.get(sp_id, [])
        content = "\n".join(str(s.get("content") or "") for s in mapped)
        content_chars = len(content.strip())
        gap = ScoringGap(
            scoring_point_id=sp_id,
            name=name,
            weight=str(p.get("weight") or ""),
            standard=standard,
            section_ids=[int(s["id"]) for s in mapped],
            section_titles=[str(s.get("title") or "") for s in mapped],
            content_chars=content_chars,
        )
        if not mapped:
            gap.status = "unmapped"
            gap.detail = "目录中没有任何章节挂载该评分点"
        elif content_chars < min_content_chars:
            gap.status = "empty"
            gap.detail = f"已挂载章节但正文仅 {content_chars} 字（< {min_content_chars}），无实质内容"
        else:
            # standard 缺失时退化用评分点名称比对，仍不可比则记 covered
            probe = standard or name
            cov = _coverage(probe, content)
            gap.coverage = cov
            if cov < coverage_weak:
                gap.status = "weak"
                gap.detail = (
                    f"评分标准用词在已挂载章节中的覆盖率仅 {cov:.1%}"
                    f"（阈值 {coverage_weak:.0%}），疑似未针对该评分项作答"
                )
            elif cov >= coverage_covered:
                gap.status = "covered"
                gap.detail = f"覆盖率 {cov:.1%}"
            else:
                gap.status = "covered"
                gap.detail = f"覆盖率 {cov:.1%}（中等，建议人工复核）"
        report.all_points.append(gap)
    return report


def save_report(report: ComplianceReport) -> None:
    """落库审计结果（表由 db.init_db 的 CREATE TABLE IF NOT EXISTS 保证）。"""
    from ..db import get_db

    payload = json.dumps(
        {
            "total_points": report.total_points,
            "gaps": [g.to_dict() for g in report.gaps],
            "warnings": report.warnings,
        },
        ensure_ascii=False,
    )
    with get_db() as conn:
        conn.execute(
            "INSERT INTO scoring_compliance_audit "
            "(project_id, audit_time, total_points, unmapped, empty, weak, covered, result_json, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                report.project_id,
                report.audit_time,
                report.total_points,
                len(report.by_status("unmapped")),
                len(report.by_status("empty")),
                len(report.by_status("weak")),
                len(report.by_status("covered")),
                payload,
                report.status,
            ),
        )
