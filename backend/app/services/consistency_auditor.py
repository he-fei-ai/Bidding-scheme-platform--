"""全文一致性审计服务

检测标书文档中的事实变量冲突，提供修复建议。
支持工期、金额、技术标准等关键信息的一致性检查。
"""

from dataclasses import dataclass, field
from datetime import datetime
import json
import logging
import re
from typing import ClassVar

from ..db import get_db
from . import text_patch
from .facts_service import is_placeholder_value, normalize_fact_value, parse_stored_items


logger = logging.getLogger(__name__)


@dataclass
class FactVariable:
    """事实变量"""

    name: str
    value: str
    source: str  # summary/section_X
    section_id: int | None = None
    confidence: float = 1.0


@dataclass
class Conflict:
    """冲突"""

    conflict_id: str
    fact_name: str
    values: list[str]
    sources: list[str]
    severity: str  # high/medium/low
    description: str
    suggested_fix: str | None = None


@dataclass
class AuditResult:
    """审计结果"""

    project_id: int
    total_sections: int
    facts_extracted: int
    conflicts_found: int
    conflicts: list[Conflict] = field(default_factory=list)
    audit_time: str = ""
    status: str = "completed"  # completed/partial/failed


class ConsistencyAuditor:
    """全文一致性审计器

    职责：
    1. 从摘要和章节中提取事实变量
    2. 检测变量值冲突
    3. 生成修复建议
    4. 应用自动修复
    """

    # 需要检查的关键事实模式（类级只读常量）
    FACT_PATTERNS: ClassVar[dict[str, str]] = {
        "工期": r"(?:工期|周期|天数|天)\s*[:：]?\s*(\d+)\s*天",
        "金额": r"(?:金额|造价|预算|报价|合同价)\s*[:：]?\s*([\d,\.]+)\s*(?:万元|元|百万|千万)?",
        # BUG-FIX 2026-10-04（正则不分岗位 → 结构性误报）：原式
        # `(?:项目经理|技术人员|管理人员|人员)\s*[:：]?\s*(\d+)\s*人` 把**不同岗位**的
        # 人数统统归到同一个事实名「人员数量」。于是正文里一句「项目经理 1 人、
        # 技术人员 20 人、管理人员 5 人」——本就各岗位独立、互不矛盾——被抽成
        # [1,20,5] 三个值，判定为「人员数量存在 3 种不同表述」的假阳性冲突（与
        # 2026-09-29 修复的「安全标准」正则过宽同类）。现把岗位词一并捕获为前缀，
        # 抽取时按岗位拆分事实名（见 extract_facts_from_text），只有**同岗位**数量
        # 不一致才判冲突；裸「人员」（施工/作业等未列举岗位）仍归入「人员数量」。
        "人员数量": r"(项目经理|技术负责人|技术人员|管理人员|作业人员|施工人员|人员)\s*[:：]?\s*(\d+)\s*人",
        "资质等级": r"(?:资质|级别|等级)\s*[:：]?\s*([甲乙丙]+[一二三级]?)",
        "质量标准": r"(?:质量|标准|等级)\s*[:：]?\s*(合格|优良|优秀)",
        # BUG-FIX 2026-09-29（正则过宽 → 结构性误报）：原式
        # `(?:安全|事故|伤亡)\s*[:：]?\s*([^,\n]+?)(?:\n|$)` 把冒号做成可选、
        # 又把值定成“直到行尾的任意内容”，等价于**正文只要出现“安全”两字**就
        # 抓走整行尾巴。不同章节的句子必然互不相同，于是几乎每个项目都会
        # 产出一条“安全标准存在 N 种不同表述”的噪声冲突，把真冲突淹没。现要求
        # 显式标签 + 冒号，且只抓不含句读的短值。
        "安全标准": r"(?:安全(?:生产|管理)?目标|事故(?:控制|发生率)|伤亡控制)\s*[:：]\s*([^\n,，。；;]{2,30})",
    }

    #: 已确认事实基准（`analysis.global_facts_json`）的 category → 本审计器事实名。
    #: ENHANCE 2026-09-29（双轨制）：本模块此前**完全不读**全局事实表，只看
    #: `projects.notes` + `sections.content`；而正文生成却以 global_facts 为基准。
    #: 后果：正文把工期写错时，审计器只拿“正文里已错的两个值”互比，既看不见
    #: 基准值、也无法判断哪个该改成哪个，“以事实为准”的主张在链路里落空。
    #: 现在只纳入可与正则抽取结果直接比较的三类（人员/技术类事实口径不同不纳入）。
    FACT_BASELINE_CATEGORIES: ClassVar[dict[str, str]] = {
        "schedule": "工期",
        "quality": "质量标准",
        "safety": "安全标准",
    }

    def extract_facts_from_text(self, text: str) -> list[FactVariable]:
        """从文本中提取事实变量

        Args:
            text: 文本内容

        Returns:
            事实变量列表
        """
        facts = []

        for fact_type, pattern in self.FACT_PATTERNS.items():
            matches = re.findall(pattern, text, re.IGNORECASE)
            for match in matches:
                # 双捕获组的模式（当前仅「人员数量」）返回 (岗位, 数值)：按岗位
                # 拆分事实名，避免不同岗位人数被合并成一条「人员数量」造成误报。
                if isinstance(match, tuple):
                    role, number = str(match[0]), str(match[1])
                    fact_name = f"{role}数量" if role != fact_type else fact_type
                    value = number.strip()
                else:
                    fact_name = f"{fact_type}"
                    value = match.strip()
                if value:
                    facts.append(
                        FactVariable(
                            name=fact_name,
                            value=value,
                            source="extracted",
                        )
                    )

        return facts

    def _collect_fact_instances(self, project_id: int) -> dict[str, list[dict]]:
        """抽取项目全量事实实例（基准 + 摘要 + 各章节）。

        只做“读库 + 正则抽取”，返回 `{fact_name: [instance, ...]}`；其中每个
        instance 为 `{value, source, section_id}`。**不写库、不判冲突**，供
        `detect_conflicts` 与 `run_full_audit` 共用，避免重复扫描正文。

        ENHANCE 2026-10-04（BUG 修复）：此前 `run_full_audit` 的 `facts_extracted`
        靠累加历史审计日志得到，而首行日志写入的正是累加值 → 真实抽取数从未传出，
        前端「提取 N 个事实」恒显示 0。抽取逻辑下沉到本方法后，全量重扫只需跑一次，
        审计数直接由本方法的返回值计算。
        """
        with get_db() as conn:
            # 获取项目摘要（使用notes字段作为摘要）
            summary_row = conn.execute(
                "SELECT notes, bidder_name, industry FROM projects WHERE id = ?",
                (project_id,),
            ).fetchone()

            # 组合项目信息作为上下文
            summary_text = ""
            if summary_row:
                if summary_row["notes"]:
                    summary_text += f"项目备注: {summary_row['notes']}\n"
                if summary_row["bidder_name"]:
                    summary_text += f"投标人: {summary_row['bidder_name']}\n"
                if summary_row["industry"]:
                    summary_text += f"行业: {summary_row['industry']}\n"

            # 获取所有章节内容
            sections = conn.execute(
                "SELECT id, title, content FROM sections WHERE project_id = ? ORDER BY sort_order",
                (project_id,),
            ).fetchall()

            # ENHANCE 2026-09-29（双轨制）：读取已确认的全局事实基准。
            # 旧库可能没有 global_facts_json 列（由 _ensure_column 后补），故先判列名。
            facts_row = conn.execute(
                "SELECT global_facts_json FROM analysis WHERE project_id = ?",
                (project_id,),
            ).fetchone()

        # 提取所有事实
        all_facts = {}

        # 事实基准排在最前：后面生成修复建议时以它为优先目标值
        for item in parse_stored_items(facts_row["global_facts_json"] if facts_row else ""):
            raw_value = str(item.get("value") or "")
            if not raw_value.strip() or is_placeholder_value(raw_value):
                continue
            fact_name = self.FACT_BASELINE_CATEGORIES.get(str(item.get("category") or ""))
            if not fact_name:
                continue
            # 关键：基准值走**与正文同一套抽取口径**（extract_facts_from_text），
            # 否则“建设工期：450 日历天”这种整句无法与正文抽出的“450”直接比较。
            for fv in self.extract_facts_from_text(raw_value):
                if fv.name != fact_name:
                    continue
                all_facts.setdefault(fact_name, []).append(
                    {
                        "value": fv.value,
                        "source": "global_facts (已确认事实基准)",
                        "section_id": None,
                    }
                )

        # 从摘要提取
        summary_facts = self.extract_facts_from_text(summary_text)
        for fact in summary_facts:
            key = fact.name
            if key not in all_facts:
                all_facts[key] = []
            all_facts[key].append(
                {
                    "value": fact.value,
                    "source": "summary (摘要)",
                    "section_id": None,
                }
            )

        # 从章节提取
        for section in sections:
            if section["content"]:
                section_facts = self.extract_facts_from_text(section["content"])
                for fact in section_facts:
                    key = fact.name
                    if key not in all_facts:
                        all_facts[key] = []
                    all_facts[key].append(
                        {
                            "value": fact.value,
                            "source": f"section_{section['id']} ({section['title']})",
                            "section_id": section["id"],
                        }
                    )

        return all_facts

    def count_extracted_facts(self, project_id: int) -> int:
        """本项目本次全量抽取到的事实实例总数（各事实名下的取值出现次数之和）。

        供 `run_full_audit` 写入审计日志的 `facts_extracted`，是**当前这次审计**
        的真实抽取数，而非历史日志累加。
        """
        all_facts = self._collect_fact_instances(project_id)
        return sum(len(instances) for instances in all_facts.values())

    def detect_conflicts(
        self, project_id: int, *, all_facts: dict[str, list[dict]] | None = None
    ) -> list[Conflict]:
        """检测项目中的所有事实变量冲突

        Args:
            project_id: 项目ID
            all_facts: 可选，预先抽取好的事实实例字典（`run_full_audit` 复用，
                避免重复扫描正文）；为 None 时自行全量抽取。

        Returns:
            冲突列表
        """
        if all_facts is None:
            all_facts = self._collect_fact_instances(project_id)

        # 检测冲突
        conflicts = []
        for fact_name, instances in all_facts.items():
            if len(instances) < 2:
                continue

            values = [inst["value"] for inst in instances]
            # BUG-FIX 2026-09-29（误报）：原判定用 `len(set(values)) > 1` 比较**原文**，
            # 于是“450”/“450 天”/“450日历天”这类同义写法被判成多个冲突值，
            # 审计报告里挤满假阳性。现按归一化口径去重后仍 ≥ 2 种才判冲突。
            distinct_values = self._distinct_values(values)
            if len(distinct_values) < 2:
                continue

            # 发现冲突
            severity = self._assess_severity(fact_name, values)

            conflict = Conflict(
                # BUG-FIX 2026-09-18（高）：原 conflict_id 为
                # `f"conflict_{fact_name}_{%Y%m%d%H%M%S}"` —— ①不含 project_id，
                # 而 consistency_conflicts.conflict_id 是**全局** UNIQUE，两个项目
                # 在同一秒审计出同名事实冲突会互相撞唯一约束抛 IntegrityError；
                # ②只精确到秒，同秒重复审计同样撞约束。现纳入 project_id 并把
                # 时间精度提到微秒，从源头保证唯一。
                conflict_id=(
                    f"conflict_{project_id}_{fact_name}_"
                    f"{datetime.now().strftime('%Y%m%d%H%M%S%f')}"
                ),
                fact_name=fact_name,
                values=values,
                sources=[inst["source"] for inst in instances],
                severity=severity,
                description=self._describe_conflict(fact_name, values),
            )
            conflicts.append(conflict)

        # 保存冲突到数据库
        self._save_conflicts(project_id, conflicts)

        return conflicts

    @staticmethod
    def _distinct_values(values: list[str]) -> list[str]:
        """按归一化口径去重，保留每种写法的首个原文形式（展示/替换仍用原文）。

        归一化只用于**判等**，不能用于落文：`apply_autofix` 拿到的仍是原文值，
        才能做带数字边界保护的安全替换。
        """
        reps: dict[str, str] = {}
        for value in values or []:
            text = str(value or "").strip()
            if not text:
                continue
            norm = normalize_fact_value(text)
            if norm and norm not in reps:
                reps[norm] = text
        return list(reps.values())

    def _assess_severity(self, fact_name: str, values: list[str]) -> str:
        """评估冲突严重程度

        Args:
            fact_name: 事实名称
            values: 冲突的值列表

        Returns:
            严重程度：high/medium/low
        """
        critical_facts = ["工期", "金额", "资质等级"]

        if fact_name in critical_facts:
            return "high"

        if len(self._distinct_values(values)) > 2:
            return "high"

        return "medium"

    def _describe_conflict(self, fact_name: str, values: list[str]) -> str:
        """描述冲突

        Args:
            fact_name: 事实名称
            values: 冲突的值列表

        Returns:
            冲突描述
        """
        unique_values = self._distinct_values(values)
        return (
            f"{fact_name}存在{len(unique_values)}种不同表述："
            + "、".join(unique_values)
            + "，可能存在不一致"
        )

    def _save_conflicts(self, project_id: int, conflicts: list[Conflict]):
        """保存冲突记录到数据库

        BUG-FIX 2026-09-18（高）：原实现是无条件 `INSERT`，配合「跨秒重复审计」
        会让同一项目的旧冲突永不清除，`GET /consistency/conflicts` 结果无限膨胀，
        用户看到的是一堆已经不存在于正文中的历史冲突。
        修复：本次审计是**全量重扫**，写入前先清空该项目「未解决」的旧冲突
        （`resolved=1` 的保留作为历史），再以 upsert 写入本次结果，保证
        「一次审计 = 一份当前冲突快照」，且重复执行幂等。
        """
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with get_db() as conn:
            # 清理未解决的旧冲突（本次全量重扫后它们已过期）
            conn.execute(
                "DELETE FROM consistency_conflicts "
                "WHERE project_id = ? AND COALESCE(resolved, 0) = 0",
                (project_id,),
            )
            for conflict in conflicts:
                conn.execute(
                    """INSERT INTO consistency_conflicts
                       (project_id, conflict_id, fact_name, values_json,
                        sources_json, severity, description, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(conflict_id) DO UPDATE SET
                        values_json = excluded.values_json,
                        sources_json = excluded.sources_json,
                        severity = excluded.severity,
                        description = excluded.description,
                        created_at = excluded.created_at""",
                    (
                        project_id,
                        conflict.conflict_id,
                        conflict.fact_name,
                        self._json_serialize(conflict.values),
                        self._json_serialize(conflict.sources),
                        conflict.severity,
                        conflict.description,
                        now_str,
                    ),
                )

    @staticmethod
    def _json_serialize(data) -> str:
        """序列化数据为JSON字符串"""
        import json

        try:
            return json.dumps(data, ensure_ascii=False)
        except Exception:
            return str(data)

    @classmethod
    def _build_suggestion(
        cls, values: list, sources: list
    ) -> tuple[str | None, str | None]:
        """由 (values, sources) 同时推导出「目标值」与「建议文案」。

        BUG-FIX 2026-10-06（P1·正文污染）：此前前端「采纳建议并修复」按钮把
        **整段建议文案**（形如「建议将以下内容统一为：450（已确认的全局事实基准）
        \\n待修正取值：45」）直接塞进 `new_value` 输入框并提交 autofix。后端
        `apply_autofix` 会拿它当替换目标值，把正文里所有旧值（如「45」）替换成
        这段说明文字 —— 正文被污染。修复：把「目标值」从文案里结构化拆出来，
        路由同时下发 `target_value`（纯值），前端采纳时填它、而不是填整段文案。

        Returns:
            (target_value, suggestion_text)。无可比冲突时两者皆 None。
        """
        baseline_value = None
        for value, source in zip(values, sources, strict=False):
            if str(source).startswith("global_facts"):
                baseline_value = value
                break

        if baseline_value is not None:
            baseline_norm = normalize_fact_value(baseline_value)
            others = [
                v for v in cls._distinct_values(values) if normalize_fact_value(v) != baseline_norm
            ]
            if not others:
                # 归一化后已一致（仅措辞不同），不应给出“统一为”建议误导用户
                return None, None
            text = (
                f"建议将以下内容统一为：{baseline_value}（已确认的全局事实基准）\n"
                f"待修正取值：{', '.join(others)}"
            )
            return str(baseline_value), text

        from collections import Counter

        # 无基准时：摘要口径优先（来自招标资料），再回退频率投票。
        most_common = None
        for value, source in zip(values, sources, strict=False):
            if str(source).startswith("summary") and str(value or "").strip():
                most_common = value
                break
        if most_common is None:
            most_common = Counter(values).most_common(1)[0][0]

        text = (
            f"建议将以下内容统一为：{most_common}\n"
            f"冲突值：{', '.join(cls._distinct_values(values))}"
        )
        return str(most_common), text

    def suggest_fixes(self, conflict_id: str, project_id: int) -> str | None:
        """生成修复建议（人类可读文案；向后兼容返回 str）。

        机器可直接采纳的纯目标值请用 :meth:`suggest_fix_target_value`。
        """
        target_value, text = self._load_suggestion_payload(conflict_id, project_id)
        return text

    def suggest_fix_target_value(self, conflict_id: str, project_id: int) -> str | None:
        """返回建议统一后的**纯目标值**（供前端「采纳建议」按钮直接填入 new_value）。

        BUG-FIX 2026-10-06（P1）：与 :meth:`suggest_fixes` 同源推导，避免前端把
        整段说明文案误当替换值污染正文。
        """
        target_value, _text = self._load_suggestion_payload(conflict_id, project_id)
        return target_value

    def _load_suggestion_payload(
        self, conflict_id: str, project_id: int
    ) -> tuple[str | None, str | None]:
        """读冲突记录并推导出 (target_value, suggestion_text)；无记录/异常返回 (None, None)。"""
        with get_db() as conn:
            row = conn.execute(
                "SELECT * FROM consistency_conflicts WHERE conflict_id = ? AND project_id = ?",
                (conflict_id, project_id),
            ).fetchone()

        if not row:
            return None, None

        import json

        try:
            values = json.loads(row["values_json"])
            if not isinstance(values, list):
                return None, None
            try:
                sources = json.loads(row["sources_json"] or "[]")
            except (json.JSONDecodeError, TypeError):
                sources = []
            return self._build_suggestion(values, sources)
        except Exception as e:
            logger.error("生成修复建议失败: %s", e)
            return None, None

    def apply_autofix(
        self,
        conflict_id: str,
        project_id: int,
        new_value: str,
        *,
        dry_run: bool = False,
    ) -> dict:
        """应用自动修复：把冲突值**真正改写进** `sections.content`。

        BUG-FIX 2026-09-14（高）：原实现只把 `consistency_conflicts` 记录标成
        `resolved=1` 并写 `resolved_value`，**从未改写 `sections.content`**。后果有两个：
          (1) 接口回「冲突已修复，统一值为 X」，用户以为正文已改，实际一个字没动；
          (2) 冲突被标 `resolved` 后从待处理列表消失，问题被**隐藏**而非解决。
        现改为：真实落文 → 只有确实写入了才标 `resolved` → 返回真实报告。

        安全约束（见 `text_patch` 模块头注释）：
          - 纯数字值启用**数字边界保护**，避免把 `30` 匹配到 `300`、`3.30` 里；
          - 所有替换合并成一批区间编辑，**任一区间非法或重叠就整批拒绝**，不做半吊子修改。

        Args:
            conflict_id: 冲突ID
            project_id: 项目ID
            new_value: 统一后的值
            dry_run: 只预览要改什么，不写库、不改冲突状态

        Returns:
            修复报告，含 `success` / `changed_sections` / `total_replacements` /
            `sections`（逐章节命中数）/ `skipped`（跳过原因）/ `message`。
        """
        report: dict = {
            "success": False,
            "dry_run": dry_run,
            "unified_value": new_value,
            "fact_name": "",
            "replaced_values": [],
            "sections": [],
            "changed_sections": 0,
            "total_replacements": 0,
            "skipped": [],
            "message": "",
        }

        try:
            with get_db() as conn:
                row = conn.execute(
                    "SELECT * FROM consistency_conflicts WHERE conflict_id = ? AND project_id = ?",
                    (conflict_id, project_id),
                ).fetchone()
                if not row:
                    report["message"] = "未找到该冲突记录"
                    return report

                report["fact_name"] = row["fact_name"] or ""
                try:
                    values = json.loads(row["values_json"] or "[]")
                except (json.JSONDecodeError, TypeError):
                    values = []
                if not isinstance(values, list):
                    values = []

                # 需要被统一掉的值：与目标值不同、且非空
                stale_values = []
                for value in values:
                    text = str(value or "").strip()
                    if text and text != str(new_value) and text not in stale_values:
                        stale_values.append(text)
                report["replaced_values"] = stale_values

                if not stale_values:
                    report["message"] = "该冲突没有需要替换的旧值（所有取值都等于目标值）"
                    return report

                sections = conn.execute(
                    "SELECT id, title, content FROM sections "
                    "WHERE project_id = ? AND content IS NOT NULL AND content != '' "
                    "ORDER BY sort_order, id",
                    (project_id,),
                ).fetchall()

                # 逐章节计算替换；先全部算完再决定是否落库
                planned: list[tuple[int, str, str]] = []  # (section_id, title, new_content)
                for section in sections:
                    content = section["content"] or ""
                    present = {
                        value: new_value
                        for value in stale_values
                        if text_patch.find_value_occurrences(content, value)
                    }
                    if not present:
                        continue
                    result = text_patch.replace_values(content, present)
                    if result["errors"] or not result["changed"]:
                        report["skipped"].append(
                            {
                                "section_id": section["id"],
                                "title": section["title"],
                                "reason": "；".join(result["errors"]) or "替换后内容无变化",
                            }
                        )
                        continue
                    report["sections"].append(
                        {
                            "section_id": section["id"],
                            "title": section["title"],
                            "replacements": len(result["applied"]),
                        }
                    )
                    report["total_replacements"] += len(result["applied"])
                    planned.append((section["id"], section["title"], result["content"]))

                if not planned:
                    report["message"] = (
                        "未在正文中定位到待统一的值，未做任何修改（冲突保持未解决，可人工处理）"
                    )
                    return report

                report["changed_sections"] = len(planned)

                if dry_run:
                    report["success"] = True
                    report["message"] = (
                        f"预览：将修改 {len(planned)} 个章节、共 {report['total_replacements']} 处"
                    )
                    return report

                now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                for section_id, _title, new_content in planned:
                    conn.execute(
                        "UPDATE sections SET content = ?, updated_at = ? "
                        "WHERE id = ? AND project_id = ?",
                        (new_content, now, section_id, project_id),
                    )

                # BUG-FIX 2026-09-18（P1）：裸 SQL 直写正文后必须失效三层缓存
                # （内存 1h / Redis 30min / build_tree 60s），否则“重新生成”会
                # 命中修复前的旧正文缓存并回写覆盖，修复成果静默丢失。
                from .sections import invalidate_content_caches
                invalidate_content_caches(
                    project_id, [sid for sid, _t, _c in planned]
                )

                # 只有确实落文成功、且没有章节被跳过，才把冲突标为已解决
                if report["skipped"]:
                    report["message"] = (
                        f"已修改 {len(planned)} 个章节、共 {report['total_replacements']} 处；"
                        f"另有 {len(report['skipped'])} 个章节未能安全替换，"
                        "冲突保持未解决，请人工处理"
                    )
                    return report

                conn.execute(
                    """UPDATE consistency_conflicts
                       SET resolved = 1, resolved_value = ?, resolved_at = ?
                       WHERE conflict_id = ? AND project_id = ?""",
                    (new_value, now, conflict_id, project_id),
                )
                report["success"] = True
                report["message"] = (
                    f"已修改 {len(planned)} 个章节、共 {report['total_replacements']} 处，"
                    f"统一为：{new_value}"
                )
                logger.info(
                    "一致性修复落文成功: conflict=%s, 章节=%d, 替换=%d",
                    conflict_id,
                    len(planned),
                    report["total_replacements"],
                )
                return report

        except Exception as e:
            logger.error("应用自动修复失败: %s", e, exc_info=True)
            report["message"] = f"修复失败：{e}"
            return report

    def run_full_audit(self, project_id: int) -> AuditResult:
        """运行完整审计

        Args:
            project_id: 项目ID

        Returns:
            审计结果
        """
        try:
            # ENHANCE 2026-10-04（BUG 修复）：全量抽取只跑一次，抽取数与冲突检测
            # 共用同一份 all_facts，避免重复扫描正文；facts_extracted 用本次真实
            # 抽取数，取代此前“累加历史审计日志”的错误口径（首行日志写入的就是
            # 累加值，导致事实抽取数永远传不出来，前端「提取 N 个事实」恒为 0）。
            all_facts = self._collect_fact_instances(project_id)
            facts_count = sum(len(instances) for instances in all_facts.values())
            conflicts = self.detect_conflicts(project_id, all_facts=all_facts)

            # 统计章节数
            with get_db() as conn:
                total_sections = conn.execute(
                    "SELECT COUNT(*) as cnt FROM sections WHERE project_id = ?",
                    (project_id,),
                ).fetchone()["cnt"]

            result = AuditResult(
                project_id=project_id,
                total_sections=total_sections,
                facts_extracted=facts_count,
                conflicts_found=len(conflicts),
                conflicts=conflicts,
                audit_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                status="completed" if len(conflicts) > 0 else "completed_no_conflicts",
            )

            # 记录审计日志
            self._save_audit_log(project_id, result)

            return result

        except Exception as e:
            logger.error("完整审计失败: %s", e, exc_info=True)
            return AuditResult(
                project_id=project_id,
                total_sections=0,
                facts_extracted=0,
                conflicts_found=0,
                audit_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                status="failed",
            )

    def _save_audit_log(self, project_id: int, result: AuditResult):
        """保存审计日志"""
        import json

        try:
            conflicts_json = json.dumps(
                [
                    {
                        "conflict_id": c.conflict_id,
                        "fact_name": c.fact_name,
                        "values": c.values,
                        "severity": c.severity,
                    }
                    for c in result.conflicts
                ],
                ensure_ascii=False,
            )
        except Exception:
            conflicts_json = "[]"

        with get_db() as conn:
            conn.execute(
                """INSERT INTO consistency_audit_log
                   (project_id, total_sections, facts_extracted,
                    conflicts_found, conflicts_json, audit_time, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    project_id,
                    result.total_sections,
                    result.facts_extracted,
                    result.conflicts_found,
                    conflicts_json,
                    result.audit_time,
                    result.status,
                ),
            )


# 全局实例
consistency_auditor = ConsistencyAuditor()
