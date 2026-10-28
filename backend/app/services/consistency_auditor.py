"""全文一致性审计服务

检测标书文档中的事实变量冲突，提供修复建议。
支持工期、金额、技术标准等关键信息的一致性检查。
"""

from dataclasses import dataclass, field
from datetime import datetime
import json
import logging
import re
from typing import Any, ClassVar

from ..db import get_db
from . import consistency_rules as rules
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
        # BUG-FIX 2026-10-10（P1·日历天/个日历天 全部漏抽）：原式只认 `(\d+)\s*天`，
        # 于是「建设工期：450 日历天」「总工期 450 个日历天」这类招标文件最高频写法
        # **一个都抽不出来**——而 `_collect_fact_instances` 的注释举的标杆例子
        # 「建设工期：450 日历天」本身就是抽不出来的。后果：工期事实（关键事实，
        # 一冲突即 high）在整个审计链路里静默缺席，正文写错工期也不会报冲突。
        # 现补 `个` 量词与 日历天/自然日/工作日 三种同义单位（与
        # `facts_service.normalize_fact_value` 的单位归一口径一致），
        # 并允许标签词与数值之间隔「为/是/约」（「合同工期为 450 日历天」）。
        "工期": r"(?:工期|周期|天数|天)\s*[:：]?\s*(?:为|是|约)*\s*(\d+)\s*(?:个)?\s*(?:日历天|自然日|工作日|天)",
        # BUG-FIX 2026-10-09（P1·金额单位丢失 → 误报 + 修复建议有害）：原式
        # `(?:...)\s*[:：]?\s*([\d,\.]+)\s*(?:万元|元|百万|千万)?` 只捕获**数字**、
        # 把单位留在非捕获组里统统丢弃。于是「合同价 1200 万元」与「造价 12000000 元」
        # 被抽成 ["1200","12000000"] —— 实为同一金额却判成冲突（误报）；采纳修复
        # 建议后还会把 12000000 替换成 1200，正文金额被改小一万倍。
        # 现把单位一并捕获进第 2 组，判等前由 `rules.normalize_amount_to_yuan`
        # 统一换算到「元」基准（见 `_distinct_values`）。
        "金额": r"(?:金额|造价|预算|报价|合同价)\s*[:：]?\s*([\d,\.]+)\s*(万元|亿元|百万|千万|万|元)?",
        # BUG-FIX 2026-10-04（正则不分岗位 → 结构性误报）：原式
        # `(?:项目经理|技术人员|管理人员|人员)\s*[:：]?\s*(\d+)\s*人` 把**不同岗位**的
        # 人数统统归到同一个事实名「人员数量」。于是正文里一句「项目经理 1 人、
        # 技术人员 20 人、管理人员 5 人」——本就各岗位独立、互不矛盾——被抽成
        # [1,20,5] 三个值，判定为「人员数量存在 3 种不同表述」的假阳性冲突（与
        # 2026-09-29 修复的「安全标准」正则过宽同类）。现把岗位词一并捕获为前缀，
        # 抽取时按岗位拆分事实名（见 extract_facts_from_text），只有**同岗位**数量
        # 不一致才判冲突；裸「人员」（施工/作业等未列举岗位）仍归入「人员数量」。
        #
        # BUG-FIX 2026-10-13（第六轮·P2·岗位分支两处口径遗漏，均经语料库实测确认）：
        #   1. **漏报**：标签词与数值之间不允许「为/是/约」——「施工人员约 12 人」
        #      「管理人员是 5 人」「技术负责人为 2 人」一个都抽不出。工期分支早在
        #      2026-10-10 就补了 `(?:为|是|约)*`（「合同工期为 450 日历天」），
        #      人员分支漏了同一处理。中文里「约」是人数表述的高频修饰，漏掉它等于
        #      整类合法写法静默缺席（写错人数也不报冲突）。
        #   2. **误报**：数值后缺负向断言——「技术人员 5 人天」「施工人员 3 人次」
        #      「作业人员 2 人月」被当成人数抽走（人天/人次/人月/人年是**工时
        #     单位**，不是人数）。总括分支 2026-10-11 已加
        #      `(?!\s*[天次年月工时受死失遇被])`，岗位分支同样漏网。两处口径
        #      现已对齐；抽取面放宽仅限「岗位词 + 间隔词 + 数字 + 人」这一形态，
        #      不影响既有用例（语料库 test_consistency_fact_corpus 全量钉住）。
        # BUG-FIX 2026-10-11（P2·「本项目共 N 人」整类漏抽）：原式只有「岗位词 + 数字 + 人」
        # 一种写法，于是「本项目共 20 人参与施工」「合计 30 人到岗」这类**总括式**表述
        # 一个都抽不出来（中文里总括词在数字前、没有岗位词）。而自动修复侧的结构校验
        # `rules._PERSONNEL_VALUE_TAIL_RE`（取值后紧跟「人」即放行）却**认**这种写法 ——
        # 检测路径看不见、修复路径却会改，两侧口径不一致：正文两处「共 20 人」写成
        # 不一致也不会报冲突，用户改完再审仍会看到残留差异。现补一个总括分支，
        # 岗位词组留空（`personnel_fact_name("")` 归到通用「人员数量」，与「裸人员」
        # 同一桶）。
        # 安全约束（防误抽）：
        #  1. 必须有总括词（共/合计/共计/总计/总共）开头，且总括词与数字之间**最多**
        #     隔一个 投入/配备/组织/动员 —— 不允许任填词，因此
        #     「共发生 3 起事故」「累计 5 人受伤」这类安全章节表述不会被当人数；
        #  2. 「人」之后禁止紧跟单位/事故量词 —— 排除人天/人月/人工/人次/人年，
        #     以及事故伤亡类复合词的人受(伤)/人死(亡)/人失(踪)/人遇(难)/人被(困)。
        #     断言只看「人」后的第一个字，故 受伤/死亡 分别以 受/死 领头被拦下。
        # HARDEN 2026-10-11（R-1·收窄动词表）：初版动词表含 安排/参与，实测会把
        # 「共安排 5 人值守」「共参与 12 人投标评审」这类**子集**人数抽进通用「人员
        # 数量」桶。通用桶不与岗位桶区分（personnel_fact_name("") 归通用），于是同篇
        # 内「共投入 30 人施工」+「共安排 5 人值守」会互判成 30 vs 5 的假冲突——
        # 与 2026-10-04 修的「不同岗位归一桶」是同一类结构性误报。现只保留表示
        # **项目级人力投放**的 投入/配备/组织/动员，剔除表示排班/参与子集的 安排/参与。
        # 注意动词组是**可选**的，故「共 20 人参与施工」（总括词后无动词）不受影响。
        #
        # BUG-FIX 2026-10-13（第六轮·R-13 收敛，原「副作用」已消除）：
        # R-1 收窄后本分支比 autofix 的取值结构校验闸门更窄，「共安排 5 人值守」
        # 这类写法对**抽取**不可见、对**autofix 就近校验**仍可见 —— 即 2026-10-11
        # P2 修过的「检测看不见、修复会改」在该子集写法上部分回归，此前记为
        # 「已知且接受」的遗留项。现在岗位分支与总括分支的正则本体一并上移到
        # `rules`（`PERSONNEL_ROLE_PATTERN` / `PERSONNEL_TOTAL_PATTERN` 合成的
        # `PERSONNEL_FACT_PATTERN`），闸门侧用同一批片段编译、按取值字符跨度精确
        # 比对，两侧口径**由代码保证一致**，遗留项闭环。误拦代价已重新评估：
        # autofix 的目标值来自冲突、冲突来自抽取，故等宽不会新增「采纳了建议却
        # 没位置可改」。完整说明见 `consistency_rules` 顶部「人员取值口径」注释。
        "人员数量": rules.PERSONNEL_FACT_PATTERN,
        # BUG-FIX 2026-10-10（P1·「资质等级：一级」全部漏抽）：原值捕获组
        # `([甲乙丙]+[一二三级]?)` 要求以甲/乙/丙开头，于是「资质等级：一级」
        # 「资质等级：一级建筑业企业」「企业资质：施工总承包一级」这些最标准写法
        # 全部抽不出 —— 资质等级是关键事实（一冲突即 high），整类事实近乎失效。
        # 现支持 甲/乙/丙级 与 一/二/三/四级，并允许中间隔 总承包/专业/施工/工程
        # 这类资格类型词（可多个连用）。
        "资质等级": r"(?:资质|级别|等级)\s*[:：]?\s*(?:总承包|专业|施工|工程|承包)*\s*([甲乙丙]+级?|[一二三四]级)",
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
    #: ENHANCE 2026-10-13（第六轮）：本体上移到 `consistency_rules.FACT_BASELINE_
    #: CATEGORIES`（回流侧需要用同一份映射判定事实冲突），这里保留为别名以兼容
    #: 既有调用方（`consistency_repair`、路由与测试）。
    FACT_BASELINE_CATEGORIES: ClassVar[dict[str, str]] = rules.FACT_BASELINE_CATEGORIES

    def extract_facts_from_text(self, text: str) -> list[FactVariable]:
        """从文本中提取事实变量

        Args:
            text: 文本内容

        Returns:
            事实变量列表
        """
        facts = []

        # BUG-FIX 2026-10-10（中·非字符串输入直接崩溃）：`re.findall` 对 None /
        # 数字等非字符串对象抛 TypeError。此前该异常会一路冒泡到 `run_full_audit`
        # 的兜底 except，把整轮审计降级为 failed —— 用户看到「审计失败」，实际
        # 只是某处正文为空或 None。这里做防御性归一：空输入一律返回空列表。
        text = str(text or "")

        for fact_type, pattern in self.FACT_PATTERNS.items():
            matches = re.findall(pattern, text, re.IGNORECASE)
            for match in matches:
                # 双捕获组的模式（「人员数量」= (岗位, 数值)、「金额」= (数值, 单位)）
                # 返回 tuple：
                #  - 人员按岗位拆分事实名，避免不同岗位人数被合并成一条「人员数量」误报；
                #  - 金额把单位拼回取值，供后续按「元」基准换算消除单位口径误报。
                if isinstance(match, tuple):
                    if fact_type == rules.FACT_AMOUNT:
                        fact_name = rules.FACT_AMOUNT
                        value = f"{str(match[0]).strip()}{str(match[1] or '').strip()}"
                    else:
                        # 「人员数量」是两条互斥分支（岗位式 / 总括式「共 N 人」），
                        # 各带一个数值捕获组 → match 是三元组，数值落在第一个**非空**
                        # 组上。总括分支的岗位组是空串 → `personnel_fact_name("")` 归到
                        # 通用「人员数量」，与裸「人员」同一桶。
                        role = str(match[0]) if len(match) > 0 else ""
                        fact_name = rules.personnel_fact_name(role)
                        value = next(
                            (str(g).strip() for g in match[1:] if str(g).strip()),
                            "",
                        )
                else:
                    fact_name = f"{fact_type}"
                    value = str(match).strip()
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
        # PERF（2026-10-08）：本方法只读不写，改 readonly 连接，不再在事件循环
        # 所在的 to_thread 里抢全局写锁。此前用无参 `get_db()`（写连接），与
        # `routers/consistency.py` 里自己写的「纯读必须 readonly」注释自相矛盾，
        # 在生成高峰期会与正文写库争锁、拉长 SSE 延迟。
        with get_db(readonly=True) as conn:
            # BUG-FIX 2026-10-09（P0·事实来源张冠李戴 / sqlite3.Row 惰性解引用）：
            # `conn.execute()` 在 CPython 里**复用同一个 statement 对象**，而
            # `row_factory=sqlite3.Row` 返回的行是**惰性解引用**的（取值时实时读
            # statement 当前行，并不缓存列值）。因此「先 fetch 到变量、后面再取列」
            # 在第二条 `conn.execute()` 执行后就会读到**别人的行**：
            #   summary_row / sections / facts_row 三个变量彼此互相污染。
            # 实测后果：`sections` 在 `facts_row = conn.execute(...)` 之后取列，
            # `section["id"]` 会读出别的 id（本例把 id=2 读成 4）→
            # ① 冲突 `sources` 里的 `section_<id>` 指向错误章节，前端「定位到该章节」
            #    跳到无关内容；② `apply_autofix` 的「只改冲突来源章节」闸门用这个 id
            #    列表 → 改到不在来源里的章节、同时漏掉真正的来源章节。
            # 修法是**每条语句取完立刻物化为普通 Python 值**，不跨语句持有 Row。
            summary_row = conn.execute(
                "SELECT notes, bidder_name, industry FROM projects WHERE id = ?",
                (project_id,),
            ).fetchone()
            summary = dict(summary_row) if summary_row is not None else None

            summary_text = ""
            if summary:
                if summary["notes"]:
                    summary_text += f"项目备注: {summary['notes']}\n"
                if summary["bidder_name"]:
                    summary_text += f"投标人: {summary['bidder_name']}\n"
                if summary["industry"]:
                    summary_text += f"行业: {summary['industry']}\n"

            # 获取所有章节内容（立即物化，避免被下一条语句污染）
            sections = [
                {
                    "id": r["id"],
                    "title": r["title"],
                    "content": r["content"],
                }
                for r in conn.execute(
                    "SELECT id, title, content FROM sections "
                    "WHERE project_id = ? ORDER BY sort_order",
                    (project_id,),
                ).fetchall()
            ]

            # ENHANCE 2026-09-29（双轨制）：读取已确认的全局事实基准。
            # 旧库可能没有 global_facts_json 列（由 _ensure_column 后补），故先判列名。
            facts_row = conn.execute(
                "SELECT global_facts_json FROM analysis WHERE project_id = ?",
                (project_id,),
            ).fetchone()
            global_facts_json = (
                facts_row["global_facts_json"] if facts_row is not None else ""
            )

        # 提取所有事实
        all_facts = {}

        # 事实基准排在最前：后面生成修复建议时以它为优先目标值
        for item in parse_stored_items(global_facts_json):
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

    # DEAD-CODE REMOVED 2026-10-08：原 `count_extracted_facts(project_id)` 已删除。
    # 它自述「供 run_full_audit 写入 facts_extracted」，但 `run_full_audit` 实际
    # 直接用 `_collect_fact_instances` 的返回值求和（ENHANCE 2026-10-04 改的），
    # 该方法在全仓（含全部测试）无任何调用方。留着等于提供一条「调它就会把全部
    # 章节正文再扫一遍」的重复路径，是误导性 API，故移除而非保留。

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
            distinct_values = self._distinct_values(values, fact_name=fact_name)
            if len(distinct_values) < 2:
                continue

            # 冲突取值落在多少个**正文章节**里：只有一处 → 正文内部自洽，
            # 只是与基准/摘要不符（判 low）。基准与摘要的 section_id 为 None，不计入。
            section_count = len(
                {inst.get("section_id") for inst in instances if inst.get("section_id") is not None}
            )

            # 发现冲突
            severity = self._assess_severity(
                fact_name, values, section_count=section_count
            )

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

        # 保存冲突到数据库；`_save_conflicts` 会复用同名「已解决」历史行，
        # 因此**实际落库的 conflict_id 可能与上面生成的不同**——把映射拿回来
        # 回写进 Conflict 对象，保证审计响应 / 审计日志里的 ID 与数据库一致
        # （BUG-FIX 2026-10-13 第六轮：此前复用时响应体里带回的是新生成的 ID，
        # 客户端拿它调 `/fix-suggestion` / `/autofix` 会 404「未找到」——
        # 冲突就在眼前，ID 却指向一条不存在的行）。
        id_map = self._save_conflicts(project_id, conflicts)
        for conflict in conflicts:
            actual_id = id_map.get(conflict.fact_name)
            if actual_id:
                conflict.conflict_id = actual_id

        return conflicts

    @classmethod
    def _distinct_values(cls, values: list[str], *, fact_name: str | None = None) -> list[str]:
        """按归一化口径去重，保留每种写法的首个原文形式（展示/替换仍用原文）。

        归一化只用于**判等**，不能用于落文：`apply_autofix` 拿到的仍是原文值，
        才能做带数字边界保护的安全替换。

        金额事实（`fact_name == "金额"`）额外按「元」基准换算判等：
        「1200万元」与「12000000元」归一后相同，不判冲突（见
        `rules.normalize_amount_to_yuan` 的 BUG-FIX 说明）。换算不可用时
        回退通用归一化，保持「宁多报也不漏报」。
        """
        reps: dict[str, str] = {}
        for value in values or []:
            text = str(value or "").strip()
            if not text:
                continue
            norm = normalize_fact_value(text)
            if fact_name == rules.FACT_AMOUNT:
                yuan = rules.normalize_amount_to_yuan(text)
                if yuan is not None:
                    norm = f"yuan:{yuan}"
            if norm and norm not in reps:
                reps[norm] = text
        return list(reps.values())

    def _assess_severity(
        self,
        fact_name: str,
        values: list[str],
        *,
        section_count: int | None = None,
    ) -> str:
        """评估冲突严重程度

        Args:
            fact_name: 事实名称
            values: 冲突的值列表
            section_count: 冲突取值落在多少个正文章节里（None = 未知，不判 low）

        Returns:
            严重程度：high/medium/low（取值与排序见 `consistency_rules`）
        """
        # ENHANCE 2026-10-09：判定标准整体收敛到 `consistency_rules.assess_severity`。
        # 此前本方法自带 `critical_facts` 字面量与 `>2` 魔法阈值，且与前端
        # `CONSISTENCY_SEVERITY_META` 各写一套；更重要的是它**永不产出 low**
        # —— 命名空间里定义的一档严重度是死档。现在审计器只做委托。
        return rules.assess_severity(
            fact_name,
            self._distinct_values(values, fact_name=fact_name),
            section_count=section_count,
        )

    def _describe_conflict(self, fact_name: str, values: list[str]) -> str:
        """描述冲突

        Args:
            fact_name: 事实名称
            values: 冲突的值列表

        Returns:
            冲突描述
        """
        unique_values = self._distinct_values(values, fact_name=fact_name)
        return (
            f"{fact_name}存在{len(unique_values)}种不同表述："
            + "、".join(unique_values)
            + "，可能存在不一致"
        )

    def _save_conflicts(self, project_id: int, conflicts: list[Conflict]) -> dict[str, str]:
        """保存冲突记录到数据库

        BUG-FIX 2026-09-18（高）：原实现是无条件 `INSERT`，配合「跨秒重复审计」
        会让同一项目的旧冲突永不清除，`GET /consistency/conflicts` 结果无限膨胀，
        用户看到的是一堆已经不存在于正文中的历史冲突。
        修复：本次审计是**全量重扫**，写入前先清空该项目「未解决」的旧冲突
        （`resolved=1` 的保留作为历史），再写入本次结果，保证
        「一次审计 = 一份当前冲突快照」，且重复执行幂等。

        BUG-FIX 2026-10-09（P2·已解决历史无限膨胀 + upsert 死代码）：
        `conflict_id` 带微秒时间戳，每次审计都产生全新 ID —— 原实现里的
        `ON CONFLICT(conflict_id)` 分支**从未被触发过**（是死代码），而
        「已解决」历史行因为 ID 永不重复，会随每次审计**逐轮累加**：3 轮审计 +
        每轮手动标记已解决 → 库里留下 3 行同名同值的「已解决」冲突。
        `GET /consistency/conflicts?state=all` 把它们全部返回，摘要卡片也会
        重复计数，看起来像同一个问题被修了 3 次。

        修复口径（不改 ID 生成规则、不需要数据库迁移）：
          1. 先把「已解决」历史折叠为**每个 (project_id, fact_name) 一行**；
          2. 本次审计仍检出的冲突，若存在同名已解决历史 → **复用该行**
             （更新取值/来源/严重度并置 resolved=0，即「重新打开」），不新建行；
          3. 仍无历史时才 INSERT 新 ID。
        同时移除原 upsert 里的 `resolved = 0` 重置分支 —— 它是埋雷：一旦将来把
        conflict_id 改成稳定值，用户手动标记的「已解决」会在下一次审计被静默
        抹回「未解决」。

        Returns:
            `{fact_name: 实际落库的 conflict_id}`。复用历史行时该 ID 与传入
            Conflict 对象里生成的 ID **不同**（BUG-FIX 2026-10-13 第六轮：
            此前调用方不回写，审计响应与审计日志里带回不存在的 ID）。
        """
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with get_db() as conn:
            # 1) 清理未解决的旧冲突（本次全量重扫后它们已过期）
            # BUG-FIX 2026-10-13（第六轮·P1·误删 AI 审计发现）：此前的 DELETE
            # 不带来源前缀，会把 AI 审计路径（`ai/workflows_expand.
            # _persist_ai_audit_findings`）写入的 `AI-%` 行一并清掉 —— 而
            # G1 修复的初衷正是让「生成时 AI 审计查了什么」可事后复查。两侧
            # 口径不对称（AI 路径只清 `AI-%`，确定性路径却清全部）导致用户
            # 点一次「开始审计」，生成时的 AI 发现就全部消失。现与 AI 路径
            # 对齐：**各清各的**，`AI-%` 行由下一轮生成时审计自行轮换。
            conn.execute(
                "DELETE FROM consistency_conflicts "
                "WHERE project_id = ? AND COALESCE(resolved, 0) = 0 "
                "AND conflict_id NOT LIKE 'AI-%'",
                (project_id,),
            )

            # 2) 已解决历史折叠：每个 fact_name 只保留最新一行（避免历史无限累加）
            # 同上：折叠只作用于确定性路径自己的行，不碰 AI 审计的已解决历史。
            keep_ids = [
                row[0]
                for row in conn.execute(
                    """SELECT conflict_id FROM (
                           SELECT conflict_id,
                                  ROW_NUMBER() OVER (
                                      PARTITION BY fact_name
                                      ORDER BY COALESCE(resolved_at, created_at) DESC,
                                               id DESC
                                  ) AS rn
                           FROM consistency_conflicts
                           WHERE project_id = ? AND COALESCE(resolved, 0) = 1
                                 AND conflict_id NOT LIKE 'AI-%'
                       ) WHERE rn = 1""",
                    (project_id,),
                ).fetchall()
            ]
            if keep_ids:
                placeholders = ",".join("?" for _ in keep_ids)
                conn.execute(
                    "DELETE FROM consistency_conflicts "
                    "WHERE project_id = ? AND COALESCE(resolved, 0) = 1 "
                    f"AND conflict_id NOT IN ({placeholders})",
                    (project_id, *keep_ids),
                )
            else:
                conn.execute(
                    "DELETE FROM consistency_conflicts "
                    "WHERE project_id = ? AND COALESCE(resolved, 0) = 1 "
                    "AND conflict_id NOT LIKE 'AI-%'",
                    (project_id,),
                )
            # {fact_name: 实际落库的 conflict_id} —— 复用行时与生成 ID 不同，
            # 由调用方回写进 Conflict 对象（见 `detect_conflicts` 的说明）。
            id_map: dict[str, str] = {}
            for conflict in conflicts:
                # BUG-FIX 2026-10-08：写库前归一化严重度，杜绝 `severity=''`
                # 这类脏值（`severity` 列 NOT NULL 但**空串合法**，而
                # `ORDER BY CASE severity ... END` 若不写 ELSE，未命中 CASE
                # 的行在 SQLite 里求值为 NULL 并排到最前 → 冲突列表顺序错乱）。
                severity = rules.normalize_severity(conflict.severity)
                values_json = self._json_serialize(conflict.values)
                sources_json = self._json_serialize(conflict.sources)

                # 3) 复用同事实的「已解决」历史行（重新打开），避免重复行累加
                # 只复用确定性路径自己的历史行：AI 发现的 fact_name 是自由文本
                # （issue 原文），与事实名（工期/金额/…）理论上不撞，但显式排除
                # `AI-%` 可从根本上杜绝「AI 已解决历史被确定性冲突重新打开」。
                reused = conn.execute(
                    "SELECT conflict_id FROM consistency_conflicts "
                    "WHERE project_id = ? AND fact_name = ? AND COALESCE(resolved, 0) = 1 "
                    "AND conflict_id NOT LIKE 'AI-%' LIMIT 1",
                    (project_id, conflict.fact_name),
                ).fetchone()
                if reused is not None:
                    conn.execute(
                        """UPDATE consistency_conflicts
                           SET values_json = ?, sources_json = ?, severity = ?,
                               description = ?, resolved = 0, resolved_value = NULL,
                               resolved_at = NULL, created_at = ?
                           WHERE conflict_id = ? AND project_id = ?""",
                        (
                            values_json,
                            sources_json,
                            severity,
                            conflict.description,
                            now_str,
                            reused["conflict_id"],
                            project_id,
                        ),
                    )
                    id_map[conflict.fact_name] = reused["conflict_id"]
                    continue

                conn.execute(
                    """INSERT INTO consistency_conflicts
                       (project_id, conflict_id, fact_name, values_json,
                        sources_json, severity, description, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        project_id,
                        conflict.conflict_id,
                        conflict.fact_name,
                        values_json,
                        sources_json,
                        severity,
                        conflict.description,
                        now_str,
                    ),
                )
                id_map[conflict.fact_name] = conflict.conflict_id
        return id_map

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
        cls, values: list, sources: list, *, fact_name: str | None = None
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
        # BUG-FIX 2026-10-13（第六轮·P2·空取值不再走到 IndexError）：
        # AI 审计发现的 values 恒为 `[]`。旧路径会继续往下走
        # `Counter([]).most_common(1)[0]` → IndexError → 被
        # `_load_suggestion_payload` 的兜底 except 吞成 (None, None) →
        # 路由回 404「未找到修复建议」——冲突明明存在，却说「建议不存在」。
        # 这里显式短路：空取值推不出目标值，两个都返回 None，由调用方决定
        # 文案回退（见 `_load_suggestion_payload`）。
        if not values:
            return None, None

        baseline_value = None
        for value, source in zip(values, sources, strict=False):
            if str(source).startswith("global_facts"):
                baseline_value = value
                break

        if baseline_value is not None:
            baseline_norm = cls._norm_for_compare(baseline_value, fact_name)
            others = [
                v
                for v in cls._distinct_values(values, fact_name=fact_name)
                if cls._norm_for_compare(v, fact_name) != baseline_norm
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
            f"冲突值：{', '.join(cls._distinct_values(values, fact_name=fact_name))}"
        )
        return str(most_common), text

    @staticmethod
    def _norm_for_compare(value: Any, fact_name: str | None) -> str:
        """取值判等用的归一化键：金额先按「元」基准换算，其余走通用归一化。"""
        text = str(value or "").strip()
        if fact_name == rules.FACT_AMOUNT:
            yuan = rules.normalize_amount_to_yuan(text)
            if yuan is not None:
                return f"yuan:{yuan}"
        return normalize_fact_value(text)

    def suggest_fixes(self, conflict_id: str, project_id: int) -> str | None:
        """生成修复建议（人类可读文案；向后兼容返回 str）。

        机器可直接采纳的纯目标值请用 :meth:`suggest_fix_target_value`。
        """
        # 只需要文案；目标值由 `suggest_fix_target_value` 单独取（下划线前缀表明
        # 此处有意不解包使用，避免 RUF059 之类告警掩盖真正的未使用变量）。
        _target_value, text = self._load_suggestion_payload(conflict_id, project_id)
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
        """读冲突记录并推导出 (target_value, suggestion_text)；无记录/异常返回 (None, None)。

        PERF 2026-10-27（并发·纯读跳过全局写锁）：本函数只做一次 SELECT，是
        「采纳建议 / 修复建议」前端按钮的每次点击热路径。此前用无参
        ``get_db()`` 抢全局写锁 —— 任何项目的审计落库 / 导出 / 生成高峰期，
        该读都会在 ``_db_write_lock``（8s 超时）上排队，纯属自伤。WAL 模式下
        readonly 连接走并发读通道，与写事务互不阻塞。
        """
        with get_db(readonly=True) as conn:
            row = conn.execute(
                "SELECT * FROM consistency_conflicts WHERE conflict_id = ? AND project_id = ?",
                (conflict_id, project_id),
            ).fetchone()
            # BUG-FIX 2026-10-09：sqlite3.Row 是**惰性解引用**（读的是连接上那个共享、
            # 会在 with 块退出时 commit() 被 finalize 掉的预处理语句），旧实现把 Row
            # 带回块外再取 values_json / sources_json / fact_name，有串列与读到残留内存
            # 的风险。这里在块内立即物化。同类修复：consistency_auditor.
            # _collect_fact_instances、consistency_repair._load_context。
            raw_values = raw_sources = raw_fact_name = raw_description = None
            if row:
                raw_values = row["values_json"] or "[]"
                raw_sources = row["sources_json"] or "[]"
                # 注意：**必须**用 `row.keys()`。sqlite3.Row 是序列，`"x" in row`
                # 比较的是**值**而不是列名，改成 `in row` 会让这两行永远取 None
                # （AI 发现的修复建议回退随即失效）。此处保留 `.keys()` 写法，
                # 宁可留着 ruff SIM118 告警也不牺牲正确性。
                raw_description = row["description"] if "description" in row.keys() else None
                raw_fact_name = row["fact_name"] if "fact_name" in row.keys() else None

        import json

        try:
            values = json.loads(raw_values)
            if not isinstance(values, list):
                return None, None
            try:
                sources = json.loads(raw_sources)
            except (json.JSONDecodeError, TypeError):
                sources = []
            target_value, text = self._build_suggestion(
                values, sources, fact_name=raw_fact_name
            )
            if target_value is None and text is None and not values and raw_description:
                # BUG-FIX 2026-10-13（第六轮·P2·AI 语义发现的建议回退）：
                # AI 审计写入的冲突没有结构化取值（`values_json='[]'`），
                # 推不出「统一为 X」的目标值；但它**有**人类可读的 description
                # （issue + 修复建议，由 `_persist_ai_audit_findings` 落库）。
                # 旧行为是连文案一起回 (None, None) → 路由 404，用户点
                # 「修复建议」只看到一句「未找到修复建议」，AI 给的修复方向
                # 反而看不到。现回退为纯文本建议：`target_value=None` 使前端
                # 「采纳建议并修复」保持禁用（没有可机器采纳的值），建议正文
                # 照常展示，用户可据此人工处理。
                text = (
                    f"{raw_description}\n\n"
                    "（该冲突为 AI 语义发现，无结构化取值、无法自动统一，"
                    "请人工核对上述建议后处理）"
                )
            return target_value, text
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
            "filtered": [],
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
                    try:
                        sources = json.loads(row["sources_json"] or "[]")
                    except (json.JSONDecodeError, TypeError):
                        sources = []
                except (json.JSONDecodeError, TypeError):
                    values, sources = [], []
                if not isinstance(values, list):
                    values = []
                if not isinstance(sources, list):
                    sources = []

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

                # BUG-FIX 2026-10-09（P1·autofix 过度替换污染正文）：两道防护闸门。
                # 闸门一 —— 章节归属：只改冲突来源章节（sources_json 里的
                # `section_<id>`）。此前扫的是**全文所有章节**，于是「工期 45 天」
                # 的修复会顺手把「钢筋直径 30 mm」改成 45 mm。
                # 闸门二 —— 标签词就近：见下方 `_keep_occurrence`。
                source_section_ids = [
                    int(sid)
                    for sid in rules.extract_source_section_ids(sources)
                    if sid is not None
                ]
                scope_limited = bool(
                    rules.autofix_scope_to_source_sections() and source_section_ids
                )
                if scope_limited:
                    placeholders = ",".join("?" for _ in source_section_ids)
                    sections = conn.execute(
                        "SELECT id, title, content FROM sections "
                        "WHERE id IN (" + placeholders + ") "
                        "AND content IS NOT NULL AND content != '' "
                        "ORDER BY sort_order, id",
                        (*source_section_ids,),
                    ).fetchall()
                    report["scoped_section_ids"] = source_section_ids
                else:
                    sections = conn.execute(
                        "SELECT id, title, content FROM sections "
                        "WHERE project_id = ? AND content IS NOT NULL AND content != '' "
                        "ORDER BY sort_order, id",
                        (project_id,),
                    ).fetchall()
                    if not source_section_ids:
                        # 无章节来源（旧数据只有摘要/基准来源）→ 范围无法限定
                        report["scope_fallback_reason"] = "冲突无章节来源，已回退全文扫描"

                # 逐章节计算替换；先全部算完再决定是否落库
                planned: list[tuple[int, str, str]] = []  # (section_id, title, new_content)
                require_keyword = rules.autofix_requires_fact_keyword()
                fact_name = report["fact_name"]
                for section in sections:
                    content = section["content"] or ""
                    present = {
                        value: new_value
                        for value in stale_values
                        if text_patch.find_value_occurrences(content, value)
                    }
                    if not present:
                        continue
                    filtered_count = 0

                    # 闸门二：取值附近必须出现该事实的标签词（仅对短数字最敏感，
                    # 但对所有取值统一执行，避免「同章内另一处无关数字」被误改）。
                    def _keep_occurrence(
                        start: int, end: int, _content=content
                    ) -> bool:
                        nonlocal filtered_count
                        if rules.fact_has_keyword_nearby(
                            _content, start, end, fact_name
                        ):
                            return True
                        filtered_count += 1
                        return False

                    result = text_patch.replace_values(
                        content,
                        present,
                        keep=_keep_occurrence if require_keyword else None,
                    )
                    if result["errors"] or not result["changed"]:
                        detail = "；".join(result["errors"])
                        if not detail and filtered_count:
                            # BUG-FIX 2026-10-10（P1·就近校验把「正确拦截」误报成
                            # 「修复失败」）：整章取值全被就近校验拦下，说明这些取值
                            # **不属于本事实**——例如人员数量冲突（20 vs 30）修复时，
                            # 同章「技术人员 20 人」「钢筋直径 20 mm」里的 20 被正确
                            # 拒绝。这是闸门正常工作，不是失败：若据此把冲突留在未解决
                            # 态，用户会反复点「采纳修复」反复看到失败，冲突永远关不掉，
                            # 而正文其实已经改对。单独计入 filtered 并照常关闭冲突；
                            # 只有真正报错、或没有任何拦截却仍未改动，才算 skipped。
                            report["filtered"].append(
                                {
                                    "section_id": section["id"],
                                    "title": section["title"],
                                    "occurrences": filtered_count,
                                    "reason": (
                                        f"全部 {filtered_count} 处取值附近未出现"
                                        f"「{fact_name}」的标签词，判定为其他事实，已跳过"
                                    ),
                                }
                            )
                            continue
                        detail = detail or "替换后内容无变化"
                        report["skipped"].append(
                            {
                                "section_id": section["id"],
                                "title": section["title"],
                                "reason": detail,
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
                if report["filtered"]:
                    report["message"] += (
                        f"；另有 {len(report['filtered'])} 个章节的取值经就近校验"
                        "判定为其他事实，已按设计跳过"
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

            # 统计章节数（纯读 → readonly 连接，见 _collect_fact_instances 的 PERF 说明）
            with get_db(readonly=True) as conn:
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
                status=(
                    rules.AUDIT_STATUS_COMPLETED
                    if conflicts
                    else rules.AUDIT_STATUS_NO_CONFLICTS
                ),
            )

            # 记录审计日志
            self._save_audit_log(project_id, result)

            return result

        except Exception as e:
            logger.error("完整审计失败: %s", e, exc_info=True)
            # BUG-FIX 2026-10-08（P1·谎报成功）：降级结果必须带明确失败态且
            # **不能带 0 值冒充「审计了 0 章节 0 事实」**。调用方（路由层）此前
            # 无条件回 `status='ok'`，前端弹「审计完成，发现 0 处冲突」——
            # 实际是审计在第一步就崩了。现回 failed，路由层据此回 500。
            return AuditResult(
                project_id=project_id,
                total_sections=0,
                facts_extracted=0,
                conflicts_found=0,
                audit_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                status=rules.AUDIT_STATUS_FAILED,
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
                    conflicts_found, conflicts_json, audit_time, status,
                    source_kind)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    project_id,
                    result.total_sections,
                    result.facts_extracted,
                    result.conflicts_found,
                    conflicts_json,
                    result.audit_time,
                    result.status,
                    # T2 根治（2026-10-16）：确定性规则审计路径，来源恒为 'rule'。
                    rules.CONFLICT_SOURCE_RULE,
                ),
            )


# 全局实例
consistency_auditor = ConsistencyAuditor()


def cleanup_old_consistency_audit(retention_days: int = 90) -> int:
    """清理超过 retention_days 的一致性审计日志（consistency_audit_log）。

    G3（2026-10-28 遗留加固）：consistency_audit_log 每行存全量 conflicts_json，
    此前无任何 retention 策略，历史表持续膨胀。按 audit_time 早于截止时间删除，
    与配置审计表 ai_config_audit 的清理口径一致（main.py 已注册 CONFIG_AUDIT_
    RETENTION_DAYS=90 模式）。返回删除条数；失败返回 0（调用方记告警）。
    """
    from datetime import datetime, timedelta

    try:
        cutoff = datetime.now() - timedelta(days=max(1, int(retention_days)))
        cutoff_str = cutoff.strftime("%Y-%m-%d %H:%M:%S")
        with get_db() as conn:
            cur = conn.execute(
                "DELETE FROM consistency_audit_log "
                "WHERE audit_time < ? ",
                (cutoff_str,),
            )
            conn.commit()
            return max(0, cur.rowcount or 0)
    except Exception:
        return 0

