"""全文一致性校核 —— 审核规则命名空间（单一事实来源）。

## 为什么需要这个模块

在 2026-10-08 之前的代码里，「一致性校核」这套判定标准散落在至少 5 个地方、
互不引用、也没有任何一致性约束：

| 位置 | 散落的内容 |
|---|---|
| `consistency_auditor.FACT_PATTERNS` | 事实名（"工期"/"金额"/…）作为正则 dict 的 key |
| `consistency_auditor._assess_severity` | `critical_facts = ["工期","金额","资质等级"]` 字面量 |
| `consistency_auditor._assess_severity` / `AuditResult` / `run_full_audit` | 严重度字面量 `"high"/"medium"/"low"`、`"completed"`/`"failed"` |
| `routers/consistency.py` `ORDER BY CASE severity …` | 严重度排序（且**漏写 ELSE**，见下） |
| `frontend/src/utils.ts` `CONSISTENCY_SEVERITY_META` | 前端严重度 → 文案/配色 |
| `routers/consistency.py` `Query(0.10)` vs `scoring_compliance.COVERAGE_WEAK=0.25` | 评分达标阈值 |

这直接产生过两个线上级缺陷：
1. 严重度排序 `CASE … END` 没有 `ELSE`，SQLite 里未命中 CASE 的表达式求值为
   NULL，而 NULL 在 ASC 排序里**排在最前** —— 于是 `severity=''` 的行会顶到
   `high` 行前面，冲突列表顺序错乱。
2. 评分达标阈值在服务层已经按实测标定上调到 0.25（模块注释里明确写了「初版取
   0.10 过于宽松……故上调」），但**路由层仍硬编码 0.10**，生产唯一入口用的正是
   被作者自己判定为过松的那个值 —— 服务层常量根本没被 HTTP 路径触达。

本模块把这些判定标准收敛成一处，并让前后端都从这里取：

- 后端：`consistency_auditor` / `routers/consistency.py` 一律引用本模块常量；
- 前端：通过 `GET /api/v1/consistency/rules` 拉取同一份定义（`GET /consistency/rules`），
  在 `frontend/src/utils.ts` 的静态兜底之外做一次对齐，保证「严重」的含义、
  排序、以及文案配色不会再两边各写一套。

## 向后兼容

本模块只做**定义收敛**，不改任何对外字段名、取值语义或数据库结构：
`severity` 列仍存 `high/medium/low`、`audit_time` 仍是 `%Y-%m-%d %H:%M:%S`、
接口路径与响应体字段一律不动。新增的 `GET /consistency/rules` 是纯新增端点。
"""

from __future__ import annotations

import re
from typing import Any


# ============================================================================
# 严重度（severity）—— 判定标准与排序的唯一来源
# ============================================================================

SEVERITY_HIGH = "high"
SEVERITY_MEDIUM = "medium"
SEVERITY_LOW = "low"

#: 排序优先级：数字越小越靠前。与前端表格顺序、摘要卡片顺序一致。
SEVERITY_ORDER: tuple[str, ...] = (SEVERITY_HIGH, SEVERITY_MEDIUM, SEVERITY_LOW)

#: severity 值 → 排序权重（1 起）
SEVERITY_RANK: dict[str, int] = {name: i + 1 for i, name in enumerate(SEVERITY_ORDER)}

#: 未知/空/None 严重度的兜底值。写库前统一归一，避免出现 `severity=''` 这种脏值。
SEVERITY_DEFAULT = SEVERITY_MEDIUM

#: 未识别严重度在 ORDER BY 里的排序权重。
#: BUG-FIX 2026-10-08：原 `CASE severity WHEN 'high' THEN 1 … END` **漏写 ELSE**，
#: SQLite 对未命中 CASE 的行求值为 NULL，而 NULL 在 ASC 中排最前，导致脏数据
#: （`severity=''`）被顶到 `high` 行之前。此常量让「未识别」明确排在所有已知级别之后。
SEVERITY_UNKNOWN_RANK = len(SEVERITY_ORDER) + 1

#: 严重度 → 展示文案/配色。后端 `/consistency/rules` 下发给前端，
#: 使「严重/中等/轻微」的定义不再前后端各写一份。
SEVERITY_META: dict[str, dict[str, str]] = {
    SEVERITY_HIGH: {"label": "严重", "color": "red"},
    SEVERITY_MEDIUM: {"label": "中等", "color": "orange"},
    SEVERITY_LOW: {"label": "轻微", "color": "blue"},
}


def normalize_severity(value: Any) -> str:
    """把任意严重度输入归一到一个**合法且可排序**的值。

    `None` / `''` / `'critical'` / 大小写异常等一律回落到 :data:`SEVERITY_DEFAULT`。
    在写库前调用即可根除 `severity=''` 这类脏值（`severity` 列虽 NOT NULL，
    但空串合法，正是排序错乱的来源）。
    """
    text = str(value or "").strip().lower()
    return text if text in SEVERITY_RANK else SEVERITY_DEFAULT


def severity_rank(value: Any) -> int:
    """严重度的排序权重；未识别值返回 :data:`SEVERITY_UNKNOWN_RANK`（排在最后）。"""
    text = str(value or "").strip().lower()
    return SEVERITY_RANK.get(text, SEVERITY_UNKNOWN_RANK)


def severity_sort_sql(column: str = "severity") -> str:
    """生成带 `ELSE` 的严重度 ORDER BY 片段（升序）。

    BUG-FIX 2026-10-08：`ELSE` 是必需的。缺了它，未命中 CASE 的行在 SQLite 里
    求值为 NULL 并**排到最前**，而不是我们期望的「排最后」。
    """
    whens = " ".join(f"WHEN '{name}' THEN {rank}" for name, rank in SEVERITY_RANK.items())
    return f"CASE {column} {whens} ELSE {SEVERITY_UNKNOWN_RANK} END"


# ============================================================================
# 事实项（fact_name）—— 抽取与冲突判定的命名空间
# ============================================================================

#: 由 `FACT_PATTERNS` 直接产出的基础事实名（顺序即冲突列表的展示顺序）
BASE_FACT_NAMES: tuple[str, ...] = (
    "工期",
    "金额",
    "人员数量",
    "资质等级",
    "质量标准",
    "安全标准",
)

FACT_DURATION = "工期"
FACT_AMOUNT = "金额"
FACT_QUALIFICATION = "资质等级"
FACT_QUALITY = "质量标准"
FACT_SAFETY = "安全标准"
FACT_PERSONNEL = "人员数量"

#: 命中即判 `high` 的关键事实项。
#: ENHANCE 2026-10-08：从 `_assess_severity` 里的字面量列表提升为模块级常量，
#: 便于新增规则时统一收口，也便于测试直接引用。
CRITICAL_FACT_NAMES: frozenset[str] = frozenset(
    {FACT_DURATION, FACT_AMOUNT, FACT_QUALIFICATION}
)

#: 「不同取值数超过此值」即升格为 `high`
SEVERITY_HIGH_DISTINCT_THRESHOLD = 2

#: 已确认事实基准（`analysis.global_facts_json`）的 category → 审计器事实名。
#: ENHANCE 2026-09-29（双轨制）+ ENHANCE 2026-10-13（第六轮·单一来源）：
#: 本映射原先只挂在 `ConsistencyAuditor.FACT_BASELINE_CATEGORIES` 上，而**回流侧**
#: （`consistency_backflow`）要判断「两条事实会不会互判冲突」时必须反过来查它 ——
#: 服务层之间只能靠 `ConsistencyAuditor` 这个类属性互相引用，权限/顺序耦合且不
#: 好测试。现上移到本命名空间，审计器保留同名类属性做**别名**（向后兼容既有
#: 调用 `ConsistencyAuditor.FACT_BASELINE_CATEGORIES` 的路由与测试）。
#:
#: 语义：只纳入可与正则抽取结果直接比较的三类（人员/技术类事实口径不同不纳入）。
#: **同一 category 下只允许存在一种取值**：审计器按 category 收基准，并把该事实名
#: 下**所有**值收进 `all_facts` 互比（没有「首条胜出」），因此同一 category 里若
#: 有两条取值不同的事实，它们会互相判成冲突，且该冲突来自 global_facts 本身、
#: 用户改正文也无法消解。回流侧据此在采纳时拦截（见 `consistency_backflow`
#: 的 R-14 注释）。
FACT_BASELINE_CATEGORIES: dict[str, str] = {
    "schedule": FACT_DURATION,
    "quality": FACT_QUALITY,
    "safety": FACT_SAFETY,
}


def is_critical_fact(fact_name: Any) -> bool:
    """该事实项是否属于「一冲突即严重」的关键事实。"""
    return str(fact_name or "").strip() in CRITICAL_FACT_NAMES


# ============================================================================
# 金额单位归一（消除「万元 / 元」口径不同造成的误报与有害修复建议）
# ============================================================================

#: 金额单位 → 换算到「元」的倍率。**不含**单位的裸数字按「元」处理。
AMOUNT_UNIT_TO_YUAN: dict[str, float] = {
    "": 1.0,
    "元": 1.0,
    "万": 10_000.0,
    "万元": 10_000.0,
    "百万": 1_000_000.0,
    "千万": 10_000_000.0,
    "亿": 100_000_000.0,
    "亿元": 100_000_000.0,
}

#: 金额值形态：<数字><单位?>（单位段只接受上表内的字眼）
_AMOUNT_VALUE_RE = re.compile(r"^\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*([^\d\s]*)\s*$")


def normalize_amount_to_yuan(value: Any) -> str | None:
    """把金额值换算成以「元」为基准的可比较串；无法换算返回 None。

    BUG-FIX 2026-10-09（P1·金额单位丢失）：`FACT_PATTERNS["金额"]` 原先**只捕获
    数字、把单位留在捕获组外**，于是「合同价 1200 万元」与「造价 12000000 元」被抽成
    `["1200", "12000000"]` 两个值 —— 判成冲突（实为同一金额的两种写法，**误报**）；
    更糟的是采纳修复建议后会把 `12000000` 替换成 `1200`，正文金额被改小一万倍
    （**修复建议有害**）。

    现在抽取时把单位一并捕获，判等前先按「元」基准换算：
    `1200万元 == 12000000元` → 归一后相同 → 不判冲突。
    无法换算（无数字 / 单位不在表内）时返回 None，调用方回退通用归一化，
    保证「宁多报也不漏报」。

    Returns:
        以元为单位的整数/小数字符串（如 `"12000000"`）；不可换算时 `None`。
    """
    text = str(value or "").strip()
    if not text:
        return None
    match = _AMOUNT_VALUE_RE.match(text)
    if not match:
        return None
    try:
        amount = float(match.group(1).replace(",", ""))
    except (TypeError, ValueError):
        return None
    factor = AMOUNT_UNIT_TO_YUAN.get((match.group(2) or "").strip())
    if factor is None:
        return None
    yuan = amount * factor
    if yuan == int(yuan):
        return str(int(yuan))
    return f"{yuan:.6f}".rstrip("0").rstrip(".")


# ============================================================================
# 严重度判定标准（唯一实现；`consistency_auditor` 只做委托）
# ============================================================================

#: 判 `low`（轻微）的「单点偏差」上限：冲突取值只落在不超过该数量的正文章节里，
#: 说明正文内部自洽、只是与基准/摘要不符 —— 改一处即可收敛，影响面最小。
#: `section_count` 未知时（调用方未传）**不判 low**，保守回退 medium。
SEVERITY_LOW_MAX_SECTIONS = 1


def assess_severity(
    fact_name: Any,
    distinct_values: Any,
    *,
    section_count: int | None = None,
) -> str:
    """统一的严重度判定（审计器 / 路由 / 前端文案共用同一份标准）。

    判定顺序（先严后宽）：

    1. `high` —— 关键事实（工期 / 金额 / 资质等级）一冲突即严重；
    2. `high` —— 不同取值数 > :data:`SEVERITY_HIGH_DISTINCT_THRESHOLD`（≥3 种表述）；
    3. `low`  —— 非关键事实 + 恰好 2 种表述 + 这些取值只落在单个正文章节
       （正文内部自洽，仅与已确认基准/摘要有偏差）；
    4. `medium` —— 其余。

    ENHANCE 2026-10-09（P1·`low` 档从未可达）：旧判定只有 `high`/`medium` 两条
    分支，`SEVERITY_LOW`（及前端「轻微」蓝色标签、`summary` 的 low 计数）是**永远
    取不到值的死档** —— 规则命名空间定义了一档却不产出它。本函数把判定收口到
    rules 模块并让 `low` 可达。

    Args:
        fact_name: 事实名
        distinct_values: 已按归一化口径去重后的取值列表
        section_count: 冲突取值出现在多少个正文章节里；`None` 表示未知（不判 low）

    Returns:
        `high` / `medium` / `low`
    """
    values = list(distinct_values or [])
    if is_critical_fact(fact_name):
        return SEVERITY_HIGH
    if len(values) > SEVERITY_HIGH_DISTINCT_THRESHOLD:
        return SEVERITY_HIGH
    if (
        section_count is not None
        and len(values) == SEVERITY_HIGH_DISTINCT_THRESHOLD
        and section_count <= SEVERITY_LOW_MAX_SECTIONS
    ):
        return SEVERITY_LOW
    return SEVERITY_MEDIUM


def personnel_fact_name(role: str) -> str:
    """按岗位生成人员类事实名（岗位间互不构成冲突）。

    裸「人员」（施工/作业等未列举岗位）复用 :data:`FACT_PERSONNEL`；
    具体岗位则拼成 `<岗位>数量`，使「项目经理 1 人 / 技术人员 20 人」不被
    误判成「人员数量有 3 种表述」。与 `consistency_auditor` 的抽取口径一致。
    """
    text = str(role or "").strip()
    if not text or text == FACT_PERSONNEL:
        return FACT_PERSONNEL
    return f"{text}数量"


# ============================================================================
# 事实标签词 —— 自动修复（autofix）的「就近定位」唯一来源
# ============================================================================

#: 事实名 → 抽取正则前缀里的「标签词」集合。
#: 与 `consistency_auditor.FACT_PATTERNS` 的标签组保持同一口径；
#: `人员数量` 的岗位前缀单独由 `personnel_fact_keywords` 动态推导。
FACT_KEYWORDS: dict[str, frozenset[str]] = {
    FACT_DURATION: frozenset({"工期", "周期", "天数"}),
    FACT_AMOUNT: frozenset({"金额", "造价", "预算", "报价", "合同价"}),
    FACT_QUALIFICATION: frozenset({"资质", "级别"}),
    FACT_QUALITY: frozenset({"质量"}),
    FACT_SAFETY: frozenset({"安全生产目标", "安全管理目标", "事故控制", "事故发生率", "伤亡控制"}),
}

#: 「人员数量」及其岗位变体额外可匹配的通用标签词
PERSONNEL_GENERIC_KEYWORDS: frozenset[str] = frozenset({"人员", "人"})

#: 岗位前缀 → 通用人员标签词（与 :data:`PERSONNEL_GENERIC_KEYWORDS` 并集使用）
PERSONNEL_ROLE_PREFIXES: tuple[str, ...] = (
    "项目经理",
    "技术负责人",
    "技术人员",
    "管理人员",
    "作业人员",
    "施工人员",
)

#: 标签词长度下限：比它短的标签词（如「人」「天」「级」）**不参与**就近判定。
#: 这类字在中文正文里几乎无处不在，拿它们做窗口校验只会产生假阴性
#: （把合法取值过滤掉 → 用户明明采纳了建议却「没有可替换的位置」）。
KEYWORD_MATCH_MIN_LEN = 2

#: autofix 就近校验的搜索半径（历史对称口径，保留以兼容 rules_meta 消费者）。
AUTOFIX_KEYWORD_WINDOW = 40

#: autofix 就近校验的**非对称**默认半径（BUG-FIX 2026-10-09·P1 续）：
#: 中文习惯是标签词在数值**之前**（「工期 30 天」），故前向窗口收紧到 12；
#: 后向窗口更窄（6），只覆盖「30 天工期」这种倒装。对称 40 会把
#: 「总工期 30 天，其中钢筋进场 30 批」里的第二个 30 误判为工期取值。
AUTOFIX_KEYWORD_WINDOW_BEFORE = 12
AUTOFIX_KEYWORD_WINDOW_AFTER = 6


#: 人员类事实的取值单位校验：取值之后必须出现人数单位「人」，中间允许隔
#: 「名/位」量词或「施工人员/作业人员」这类岗位词（至多 4 个汉字）。
#: 是比「附近出现标签词」更强的**结构约束**。
#: BUG-FIX 2026-10-10（P1·人数类自动修复零闸门）：裸「人员数量」此前
#: `personnel_fact_keywords` 返回空集 → `fact_has_keyword_nearby` 直接放行，
#: 自动修复对人数取值完全绕过标签词校验，「钢筋直径 20 mm」「构件编号 20 号」
#: 会被当成「共 20 人」改写。改用结构校验后这类无关数字一律拦下；上限 4 个汉字
#: 保证「钢筋直径 20 mm，共 20 人到场」里的钢筋 20 仍被拦（其后 4 字内无「人」）。
#:
#: == 人员取值口径：抽取侧与闸门侧的**唯一来源**（BUG-FIX 2026-10-13 第六轮·R-13 收敛）==
#:
#: 历史形态：抽取侧在 `consistency_auditor.FACT_PATTERNS["人员数量"]` 写一套正则，
#: 闸门侧在这里写另一套**近似**正则（「数字 + ≤4 汉字 + 人」+ 岗位词回看）。两侧
#: 只能靠注释互相提醒（原 R-13 注释：闸门「有意宽于」抽取，勿按对齐收紧），结果是
#: 一条常驻的口径裂缝 —— **检测看不见、修复却在改**：例如「共安排 5 人值守」
#: 抽取侧不认（动词表已按 R-1 收窄），闸门侧却放行；修复因此可能改到抽取根本
#: 没看到的短语。
#:
#: 收敛后：两侧共用下面这组片段。抽取侧直接用 `PERSONNEL_FACT_PATTERN`；闸门侧用
#: 同一批片段编译出的 `_PERSONNEL_CONTEXT_RE`（带命名组，按**取值字符跨度**精确
#: 比对）。于是「闸门放行某个出现位置」⟺「抽取能在同一位置抽到该取值」，
#: 口径一致性从「注释约定」升级为「代码约束」。
#:
#: 误拦代价已重新评估（原 R-13 的主要顾虑）：autofix 的待替换值来自冲突，而冲突
#: 来自抽取——凡是抽取抽不到的取值，压根不会成为 autofix 的目标值，因此「闸门
#: 与抽取等宽」不会新增「用户采纳了建议却没位置可改」的失败模式。该等价性由
#: `tests/test_consistency_round6_2026_10_13.py::TestPersonnelGateMatchesExtraction`
#: 用事实抽取语料库逐条交叉验证（抽取抽得到 → 闸门放行；抽取抽不到 → 闸门拒绝）。
_PERSONNEL_ROLE_ALT = "|".join(re.escape(r) for r in (*PERSONNEL_ROLE_PREFIXES, "人员"))
#: 岗位词与数值之间的允许间隔：可选冒号 + 可选的「为/是/约」（与工期分支同口径）。
_PERSONNEL_MODIFIERS = r"\s*[:：]?\s*(?:为|是|约)*\s*"
#: 总括词前缀（共/合计/…）+ 可选动词（投入/配备/组织/动员），动词表按 R-1 收窄。
#: 收窄动词表正是「共安排 5 人值守」这类**子集**写法被排除的原因：安排/参与
#: 不在动词表里，前缀匹配不上，整条总括分支不命中（与后段宽松无关）。
_PERSONNEL_TOTAL_PREFIX = r"(?:共计|总计|合计|总共|共)\s*(?:投入|配备|组织|动员)?\s*"
#: 数字与人数单位之间的允许间隔：量词「名/位」或岗位词（「共 20 名施工人员到场」）。
#: BUG-FIX 2026-10-13（第六轮·补抽取而非收闸门）：R-13 收敛把闸门收得与抽取等宽后，
#: 「共 20 名施工人员到场」这类**合法**人数写法两侧同时失效（原闸门靠
#: 「数字 + ≤4 汉字 + 人」能认，抽取不认）。按最优解是补**抽取**：间隔最多 4 个
#: 汉字，与旧闸门同宽。安全性由前缀与单位守卫共同保证 —— 前缀要求总括词（且
#: 其后的动词不在表内即不命中），守卫禁止「人」后紧跟 天/次/月/年/工/时/受/死/失/
#: 遇/被，故「共安排 5 人值守」（前缀不命中）、「共 3 人受伤」（守卫）、
#: 「共投入 45 人天」（守卫）依旧全部排除。
_PERSONNEL_VALUE_GAP = r"\s*[一-鿿]{0,4}?\s*"
#: 「人」之后禁止紧跟单位/事故量词：排除 人天/人月/人次/人年/人工，
#: 以及事故伤亡类复合词（人受伤/人死亡/人失踪/人遇难/人被被困）。
_PERSONNEL_UNIT_GUARD = r"(?!\s*[天次年月工时受死失遇被])"

#: 岗位分支 / 总括分支 / 二者合成（**抽取侧**使用，见 `consistency_auditor.FACT_PATTERNS`）
PERSONNEL_ROLE_PATTERN = (
    rf"({_PERSONNEL_ROLE_ALT}){_PERSONNEL_MODIFIERS}(\d+)\s*人{_PERSONNEL_UNIT_GUARD}"
)
PERSONNEL_TOTAL_PATTERN = (
    rf"{_PERSONNEL_TOTAL_PREFIX}(\d+){_PERSONNEL_VALUE_GAP}人{_PERSONNEL_UNIT_GUARD}"
)
PERSONNEL_FACT_PATTERN = PERSONNEL_ROLE_PATTERN + "|" + PERSONNEL_TOTAL_PATTERN

#: **闸门侧**使用：与抽取完全同口径，但带命名组 —— 按取值跨度精确判定归属。
_PERSONNEL_CONTEXT_RE = re.compile(
    rf"(?P<role>{_PERSONNEL_ROLE_ALT}){_PERSONNEL_MODIFIERS}"
    rf"(?P<role_value>\d+)\s*人{_PERSONNEL_UNIT_GUARD}"
    rf"|{_PERSONNEL_TOTAL_PREFIX}(?P<total_value>\d+){_PERSONNEL_VALUE_GAP}人{_PERSONNEL_UNIT_GUARD}"
)

#: 兼容旧引用的别名：闸门曾按「取值后 ≤4 汉字内出现『人』」判定，现由
#: `_PERSONNEL_CONTEXT_RE` 取代；保留常量名仅为避免历史补丁脚本/引用断裂。
_PERSONNEL_VALUE_TAIL_RE = re.compile(r"\s*[一-鿿]{0,4}?\s*人")


def is_personnel_fact_name(fact_name: Any) -> bool:
    """是否属于人员数量类事实（通用「人员数量」或「<岗位>数量」变体）。

    只把**确实带人数单位「人」**的事实判为人员类，避免「设备数量」「工程量」
    这类同名后缀的非人员事实被误套人数结构校验。
    """
    text = str(fact_name or "").strip()
    if not text.endswith("数量"):
        return False
    role = "" if text == FACT_PERSONNEL else text[: -len("数量")]
    if not role:
        return True  # 通用「人员数量」
    for prefix in PERSONNEL_ROLE_PREFIXES:
        if role == prefix or role.startswith(prefix):
            return True
    return role.endswith(("人员", "人"))


def personnel_fact_keywords(fact_name: Any) -> frozenset[str]:
    """取事实名的标签词集合（唯一实现，供 autofix 就近校验使用）。

    返回空集表示该事实名**无标签词定义**：调用方必须回退到既有行为
    （只靠数字边界保护），不得因此拒绝替换。

    BUG-FIX 2026-10-10（P1·裸「人员数量」无标签词）：原实现对
    `FACT_PERSONNEL` 本身返回空集（条件 `text != FACT_PERSONNEL` 把它排除在
    岗位分支之外，又不在 `FACT_KEYWORDS` 里）→ 该事实名没有任何标签词 →
    标签词闸门对它**完全不生效**。现在裸「人员数量」返回通用人员标签词，
    保证未列举岗位（如「后勤人员」）的人数取值也能被就近校验拦住。
    """
    text = str(fact_name or "").strip()
    if text in FACT_KEYWORDS:
        return FACT_KEYWORDS[text]
    if text.endswith("数量"):
        role = "" if text == FACT_PERSONNEL else text[: -len("数量")]
        for prefix in PERSONNEL_ROLE_PREFIXES:
            if role and (role == prefix or role.startswith(prefix)):
                return frozenset({role}) | PERSONNEL_GENERIC_KEYWORDS
        if role:
            return frozenset({role})
        # 裸「人员数量」：返回通用标签词，使闸门对未列举岗位生效
        return frozenset(PERSONNEL_GENERIC_KEYWORDS)
    return frozenset()


def _personnel_occurrence_matches_fact(
    text: str, start: int, end: int, fact_name: Any
) -> bool:
    """人员类事实的出现位置归属判定 —— 与抽取正则同口径（BUG-FIX 2026-10-13·R-13）。

    Returns:
        True 仅当该出现位置正是抽取正则会在**同一字符跨度**抽出的那个人数取值，
        且取值归属的事实名一致：

          * 命中岗位分支 → 归「<岗位>数量」；角色词为裸「人员」时归「人员数量」；
          * 命中总括分支（共/合计/…）→ 只归通用「人员数量」；
          * 两个分支都不命中 → 拒绝（抽取也看不见它）。

    这同时继承了 2026-10-10 的跨岗位保护：岗位不一致（如用「技术人员数量」去改
    「管理人员 5 人」）必然落在 `m.group("role") != fact_role` 上被拒。
    """
    fact_name_text = str(fact_name or "").strip()
    fact_role = "" if fact_name_text == FACT_PERSONNEL else fact_name_text[: -len("数量")]
    for m in _PERSONNEL_CONTEXT_RE.finditer(text):
        role_value = m.group("role_value")
        if role_value is not None and (m.start("role_value"), m.end("role_value")) == (start, end):
            # 具体岗位事实只认领同名岗位；通用「人员数量」只认领裸「人员」写法
            return (m.group("role") == fact_role) if fact_role else (m.group("role") == "人员")
        total_value = m.group("total_value")
        if total_value is not None and (m.start("total_value"), m.end("total_value")) == (start, end):
            # 总括式归通用桶，具体岗位事实不得认领
            return not fact_role
    return False


def fact_has_keyword_nearby(
    content: str,
    occurrence_start: int,
    occurrence_end: int,
    fact_name: Any,
    *,
    window: int | None = None,
) -> bool:
    """判定取值的某处出现位置附近是否出现该事实的标签词。

    BUG-FIX 2026-10-09（P1·autofix 过度替换污染正文）：`apply_autofix` 此前把
    待统一值在**全文所有章节**里的所有出现位置一律替换。事实取值多为「30」「1」
    这类极短串，数字边界保护挡不住「钢筋直径 30 mm」「本项目划分为 1 个标段」
    这些与事实无关的数字 —— 实测一次 autofix 会在无关章节改出 4 处错误（把
    钢筋直径、构件数量、批次号统统改成工期/人数），而冲突却因「替换成功」
    被标为已解决，正文被静默污染。

    本函数提供第二道闸门：要求取值附近出现该事实的标签词。两道闸门的分工——

      - **章节归属**（`apply_autofix` 侧）：只扫冲突来源章节，管「在哪个章节」；
      - **标签词就近**（本函数）：要求取值与标签词同现，管「是不是这个事实的值」。

    Returns:
        True 表示允许替换。空正文、无标签词定义的事实名一律返回 True（不阻断）。
        人员数量类事实不看标签词窗口，改用「取值是否紧跟人数单位『人』」的结构
        校验（详见函数体 BUG-FIX 2026-10-10 注释）。
    """
    text = str(content or "")
    if not text:
        return True

    # BUG-FIX 2026-10-10（P1·人数类事实零闸门）：人员数量类事实改用**结构校验**——
    # 取值之后必须紧跟人数单位「人」（可隔「名/位」量词），与抽取正则
    # `(\d+)\s*人` 的口径一致。原走标签词窗口会两头落空：通用标签词只有
    # 「人员/人」，「人」长度 1 被 KEYWORD_MATCH_MIN_LEN=2 过滤掉，「人员」又不会
    # 出现在「共 20 人」里 → 合法取值被整批误拦；而放宽长度下限到 1 又会因
    # 「钢筋直径 20 mm」邻近「…共 20 人参与施工」而被放行。结构校验同时满足两侧。
    if is_personnel_fact_name(fact_name):
        # 与抽取同口径的结构校验（R-13 收敛，见上方「人员取值口径」注释）。
        # 原实现分两步：① 取值后 ≤4 汉字内有「人」；② 取值前回看岗位词是否一致。
        # ① 比抽取宽（会放行抽取看不见的写法，如「共安排 5 人值守」）；② 用回看
        # 窗口近似判定归属（「技术人员约 20 人」靠 `[^0-9]{0,4}` 勉强覆盖）。
        # 现按**取值字符跨度**精确匹配：命中岗位分支必须岗位一致，命中总括分支
        # 只能归通用桶，未命中任何分支一律拒绝。
        return _personnel_occurrence_matches_fact(
            text, int(occurrence_start), int(occurrence_end), fact_name
        )

    keywords = personnel_fact_keywords(fact_name)
    if not keywords:
        # 该事实名无标签词定义：回退既有行为（只靠数字边界保护），不阻断替换。
        return True

    # 非对称窗口：前向（标签词在数值之前，中文常态）比后向（倒装）宽。
    # BUG-FIX 2026-10-09（P1 续）：对称 ±40 会把「总工期 30 天，其中钢筋进场
    # 30 批」的第二个 30 误判成工期取值并改掉。前向收紧 + 后向更窄后拦掉该误改。
    # BUG-FIX 2026-10-10（P1·非对称窗口是死代码）：原默认
    # `window=AUTOFIX_KEYWORD_WINDOW`（40，真值）使
    # `window if window else autofix_keyword_window_before()` 永远走 40 分支，
    # 12/6 的非对称口径与 CONSISTENCY_AUTOFIX_KEYWORD_WINDOW_BEFORE/AFTER 两个
    # 配置项**从未生效**。默认改为 None = 用非对称口径；显式传 window 才表示
    # 「前后都用同一半径」的对称覆盖（保留既有调用契约）。
    if window:
        before_win, after_win = int(window), int(window)
    else:
        before_win = autofix_keyword_window_before()
        after_win = autofix_keyword_window_after()
    start = max(0, int(occurrence_start) - before_win)
    neighborhood = text[start:int(occurrence_start)]
    tail = text[
        int(occurrence_end) : min(len(text), int(occurrence_end) + after_win)
    ]
    return any(
        kw in neighborhood or kw in tail
        for kw in keywords
        if len(kw) >= KEYWORD_MATCH_MIN_LEN
    )


_SOURCE_SECTION_RE = re.compile(r"section_(\d+)")


def extract_source_section_ids(sources: Any) -> list[int | None]:
    """从冲突的 `sources_json` 内容里提取章节 ID 列表（去重、保序）。

    `consistency_auditor` 的 sources 形如
    ``["section_12 (施工组织方案)", "summary (摘要)", "global_facts (已确认事实基准)"]``；
    摘要与基准来源没有 section_id，本方法只返回能定位到章节的 ID。

    返回空列表表示**该冲突没有任何章节来源**（例如旧数据只写了摘要来源），
    调用方必须回退到全文扫描并说明原因，不能因此静默跳过修复。
    """
    seen: list[int | None] = []
    for source in sources or []:
        match = _SOURCE_SECTION_RE.search(str(source or ""))
        if not match:
            continue
        section_id = int(match.group(1))
        if section_id not in seen:
            seen.append(section_id)
    return seen


def autofix_scope_to_source_sections() -> bool:
    """是否把替换范围限定在冲突来源章节内（见 config 同名开关）。"""
    try:
        from ..config import CONSISTENCY_AUTOFIX_SCOPE_TO_SOURCE_SECTIONS

        return bool(CONSISTENCY_AUTOFIX_SCOPE_TO_SOURCE_SECTIONS)
    except Exception:  # pragma: no cover - 配置不可用时保持默认（限定）
        return True


def autofix_requires_fact_keyword() -> bool:
    """是否启用标签词就近校验（见 config 同名开关）。"""
    try:
        from ..config import CONSISTENCY_AUTOFIX_REQUIRE_FACT_KEYWORD

        return bool(CONSISTENCY_AUTOFIX_REQUIRE_FACT_KEYWORD)
    except Exception:  # pragma: no cover - 配置不可用时保持默认（启用）
        return True


def autofix_keyword_window_before() -> int:
    """标签词在数值**之前**的搜索半径（见 config 同名开关）。"""
    try:
        from ..config import CONSISTENCY_AUTOFIX_KEYWORD_WINDOW_BEFORE

        return max(0, int(CONSISTENCY_AUTOFIX_KEYWORD_WINDOW_BEFORE))
    except Exception:  # pragma: no cover - 配置不可用时用模块默认值
        return AUTOFIX_KEYWORD_WINDOW_BEFORE


def autofix_keyword_window_after() -> int:
    """标签词在数值**之后**的搜索半径（见 config 同名开关）。"""
    try:
        from ..config import CONSISTENCY_AUTOFIX_KEYWORD_WINDOW_AFTER

        return max(0, int(CONSISTENCY_AUTOFIX_KEYWORD_WINDOW_AFTER))
    except Exception:  # pragma: no cover - 配置不可用时用模块默认值
        return AUTOFIX_KEYWORD_WINDOW_AFTER


# ============================================================================
# 审计状态（AuditResult.status）
# ============================================================================

AUDIT_STATUS_COMPLETED = "completed"
AUDIT_STATUS_NO_CONFLICTS = "completed_no_conflicts"
AUDIT_STATUS_FAILED = "failed"

#: 成功状态集合 —— 只有落在集合内的 status 才允许路由层回 `status='ok'`。
#: BUG-FIX 2026-10-08（P1·谎报成功）：`run_full_audit` 内部把异常吞掉并返回
#: `status='failed'` + 全 0，而 `GET /consistency/audit/{id}` 此前**无条件**回
#: `status='ok'`。前端随即弹「审计完成，发现 0 处冲突」——实际是审计崩了。
AUDIT_SUCCESS_STATUSES: frozenset[str] = frozenset(
    {AUDIT_STATUS_COMPLETED, AUDIT_STATUS_NO_CONFLICTS}
)


def is_audit_success(status: Any) -> bool:
    """本次审计是否真正跑完（而非内部异常降级）。"""
    return str(status or "") in AUDIT_SUCCESS_STATUSES


# ============================================================================
# 冲突状态（consistency_conflicts.resolved）
# ============================================================================

CONFLICT_STATE_OPEN = "open"
CONFLICT_STATE_RESOLVED = "resolved"

#: `resolved` 列 → 前端可见状态名
CONFLICT_STATE_BY_RESOLVED: dict[int, str] = {
    0: CONFLICT_STATE_OPEN,
    1: CONFLICT_STATE_RESOLVED,
}


def conflict_state(resolved: Any) -> str:
    """`resolved` 列（0/1/None）→ 规范化冲突状态名。"""
    try:
        return CONFLICT_STATE_BY_RESOLVED.get(int(resolved), CONFLICT_STATE_OPEN)
    except (TypeError, ValueError):
        return CONFLICT_STATE_OPEN


# ============================================================================
# 冲突来源（conflict_source）—— 同一张表里的两条审计链路
# ============================================================================
#
# `consistency_conflicts` 表被两条**语义完全不同**的链路共用：
#
# | 来源 | 写入方 | conflict_id 形态 | values_json | 可机器自动修复? |
# |---|---|---|---|---|
# | `rule`（确定性规则审计） | `consistency_auditor._save_conflicts` | `conflict_<fact>_<ts>` | 结构化取值 | 是（autofix 按取值替换） |
# | `ai`（AI 语义审计） | `ai/workflows_expand._persist_ai_audit_findings` | `AI-{project}-{hex}` | 恒为 `'[]'` | 否（无取值可替换） |
#
# 在 2026-10-13 之前，这个区分**只存在于 ID 前缀这一个隐式约定里**，没有任何
# 函数把它抽出来——于是三处 SQL 各自硬编码 `conflict_id LIKE 'AI-%'`（审计器的
# 清理/折叠/复用三处 + 收敛判据一处），而**对外接口一个都没暴露**。后果是：
#
# 1. `GET /consistency/conflicts` 把 AI 发现与规则冲突混排返回，且 AI 发现的
#    `fact_name` 是自由文本 issue（最长 100 字），前端「事实项」列把它当成事实名
#    渲染成 `<Text strong>...</Text>`，用户看到一长段句子以为是"事实项"；
# 2. `GET /consistency/summary` 的 `conflicts_by_severity` 把 AI 发现一并计入
#    「中等冲突」卡片（AI 发现恒为 `SEVERITY_DEFAULT`）——「中等 3」里可能有 2 条
#    根本不是规则冲突，用户据此评估风险等级会被误导；
# 3. 前端「自动修复」按钮对 AI 发现仍可点：`apply_autofix` 因 `values=[]` 必然
#    回 `no_change`（「该冲突没有需要替换的旧值」），用户反复点、反复看到失败。
#
# 现把该口径收敛成本命名空间的单一事实来源，并随 `GET /consistency/rules` 下发，
# 前端据此区分展示与禁用相应操作。**对外只增字段、不改任何既有字段名与取值语义**
# （`conflict_id` 前缀约定不动，历史数据无需迁移）。
CONFLICT_SOURCE_RULE = "rule"
CONFLICT_SOURCE_AI = "ai"

#: AI 语义审计写入的 `conflict_id` 前缀（与
#: `ai/workflows_expand._persist_ai_audit_findings` 的生成规则保持严格一致）。
AI_CONFLICT_ID_PREFIX = "AI-"

#: 冲突来源 → 展示文案/配色（随规则快照下发，前端「AI 发现」标签用）。
CONFLICT_SOURCE_META: dict[str, dict[str, str]] = {
    CONFLICT_SOURCE_RULE: {"label": "规则冲突", "color": "red"},
    CONFLICT_SOURCE_AI: {"label": "AI 发现", "color": "purple"},
}


def is_ai_conflict_id(conflict_id: Any) -> bool:
    """该冲突 ID 是否属于 AI 语义审计链路。

    防御式判断：`None` / 非字符串一律按规则冲突处理（宁可漏标也不误把规则
    冲突当成 AI 发现——后者会导致 autofix 被禁用）。
    """
    return str(conflict_id or "").startswith(AI_CONFLICT_ID_PREFIX)


def conflict_source_kind(conflict_id: Any) -> str:
    """`conflict_id` → 冲突来源名（`rule` / `ai`）。"""
    return CONFLICT_SOURCE_AI if is_ai_conflict_id(conflict_id) else CONFLICT_SOURCE_RULE


def conflict_source_meta(source_kind: Any) -> dict[str, str]:
    """冲突来源的文案/配色；未知值安全降级为规则冲突（不冒充 AI 发现）。"""
    text = str(source_kind or "").strip().lower()
    return CONFLICT_SOURCE_META.get(text, dict(CONFLICT_SOURCE_META[CONFLICT_SOURCE_RULE]))


# ============================================================================
# 评分标准达标状态（scoring_compliance.ScoringGap.status）
# ============================================================================

SCORING_STATUS_UNMAPPED = "unmapped"
SCORING_STATUS_EMPTY = "empty"
SCORING_STATUS_WEAK = "weak"
SCORING_STATUS_COVERED = "covered"

SCORING_STATUS_META: dict[str, dict[str, str]] = {
    SCORING_STATUS_UNMAPPED: {"label": "未挂载章节", "color": "red"},
    SCORING_STATUS_EMPTY: {"label": "正文过短", "color": "orange"},
    SCORING_STATUS_WEAK: {"label": "疑似未答到点", "color": "gold"},
    SCORING_STATUS_COVERED: {"label": "已达标", "color": "green"},
}


#: 未知评分状态的兜底展示
SCORING_STATUS_UNKNOWN_META: dict[str, str] = {"label": "未知", "color": "default"}


def scoring_status_meta(status: Any) -> dict[str, str]:
    """评分达标状态的文案/配色；未知/空状态安全降级为中性标签。"""
    return SCORING_STATUS_META.get(str(status or ""), dict(SCORING_STATUS_UNKNOWN_META))


# ============================================================================
# 废标条款应答覆盖率状态（rejection_coverage.RejectionItem.status）
# ============================================================================

REJECTION_STATUS_COVERED = "covered"
REJECTION_STATUS_WEAK = "weak"
REJECTION_STATUS_EMPTY = "empty"

#: 状态 → 展示文案/配色。
#: ENHANCE 2026-10-13（第六轮·统一命名空间）：废标覆盖率的三档状态此前只存在于
#: 前端 `RejectionCoverageTab.tsx` 的本地 `statusLabel` / `statusColor` 两张
#: 写死表里，与后端零共享——和 2026-10-08（严重度）、2026-10-13（评分状态）
#: 修掉的是同一类漂移，这是漏网的第三处。现收敛进命名空间并随
#: `GET /consistency/rules` 下发，前端经 utils 映射消费。
REJECTION_STATUS_META: dict[str, dict[str, str]] = {
    REJECTION_STATUS_COVERED: {"label": "已覆盖", "color": "green"},
    REJECTION_STATUS_WEAK: {"label": "覆盖较弱", "color": "orange"},
    REJECTION_STATUS_EMPTY: {"label": "未应答", "color": "red"},
}

#: 未知废标状态的兜底展示（与评分状态同口径：不回落到真实状态，
#: 避免把「后端新增了一档」伪装成「已覆盖」）。
REJECTION_STATUS_UNKNOWN_META: dict[str, str] = {"label": "未知", "color": "default"}


def rejection_status_meta(status: Any) -> dict[str, str]:
    """废标应答状态的文案/配色；未知/空状态安全降级为中性标签。"""
    return REJECTION_STATUS_META.get(str(status or ""), dict(REJECTION_STATUS_UNKNOWN_META))


# ============================================================================
# 对外暴露的规则快照
# ============================================================================


#: autofix 的 `new_value` 长度上限。超过即拒：正文里一个事实值不可能这么长，
#: 超长输入只可能来自误填（把整段说明文案当值）或恶意请求。
#:
#: BUG-FIX 2026-10-13（命名空间收口）：此前该常量定义在
#: `routers/consistency.py`（`AUTOFIX_VALUE_MAX_CHARS`），而本模块才是
#: 「审核规则命名空间」的单一事实来源 —— 一个前端需要展示、且会被 400 拒绝的
#: 判定阈值，却没有出现在 `GET /consistency/rules` 里，前端无从提示用户上限。
#: 现移入本模块；`routers/consistency.py` 保留同名 import 以向后兼容既有引用。
AUTOFIX_VALUE_MAX_CHARS = 200


def autofix_value_max_chars() -> int:
    """autofix ``new_value`` 的长度上限（字符数）。"""
    return AUTOFIX_VALUE_MAX_CHARS


def scoring_coverage_weak() -> float:
    """评分标准「疑似未答到点」的覆盖率阈值（服务层标定值）。"""
    from .scoring_compliance import COVERAGE_WEAK

    return float(COVERAGE_WEAK)


def rejection_coverage_weak() -> float:
    """废标条款「覆盖较弱」的覆盖率阈值。

    与 :func:`scoring_coverage_weak` **同量级但不同实现**：评分达标走字符三元组
    （`scoring_compliance._terms`），废标应答走字符二元组（`rejection_coverage.
    _char_bigrams`）。两个指标都叫「覆盖率」，粒度却不同，故各自独立标定，
    不能互相复用常量——此处单列出来只为让 `/consistency/rules` 能把两个阈值都
    下发，避免前端再各自硬编码。
    """
    from .rejection_coverage import COVERAGE_WEAK

    return float(COVERAGE_WEAK)


def rules_payload() -> dict[str, Any]:
    """`GET /consistency/rules` 的响应体 —— 前后端共用的判定标准快照。"""
    return {
        "severity": {
            "order": list(SEVERITY_ORDER),
            "default": SEVERITY_DEFAULT,
            "unknown_rank": SEVERITY_UNKNOWN_RANK,
            "meta": SEVERITY_META,
        },
        "facts": {
            "base_names": list(BASE_FACT_NAMES),
            "critical": sorted(CRITICAL_FACT_NAMES),
            "high_distinct_threshold": SEVERITY_HIGH_DISTINCT_THRESHOLD,
            "low_max_sections": SEVERITY_LOW_MAX_SECTIONS,
            "amount_units": sorted(AMOUNT_UNIT_TO_YUAN),
            # 自动修复防护口径（2026-10-09 深度审计加固）：前端可据此向用户说明
            # 「为什么有些位置没改」——替换只在冲突来源章节内进行，且值附近必须
            # 出现事实标签词。挂在 facts 之下而非新增顶层键，以保持
            # test_rules_payload_shape 对顶层键集合的既有断言成立。
            #
            # BUG-FIX 2026-10-13（P1 · 快照不反映配置生效值）：本块此前下发的是
            # **模块常量** `AUTOFIX_KEYWORD_WINDOW_BEFORE/AFTER`（12 / 6），而
            # 真正生效的是 `autofix_keyword_window_before()/after()`（读
            # `CONSISTENCY_AUTOFIX_KEYWORD_WINDOW_BEFORE/AFTER` 配置项）。运维一旦
            # 调环境变量，实际闸门口径变了，而这个「规则快照」端点**照旧返回 12/6**
            # —— 前端据此展示的「替换保护范围」说明从此是错的，且没有任何
            # 日志或告警。同理 `autofix_scope` 原是写死的字符串
            # `"source_sections"`，而实际由 `autofix_scope_to_source_sections()`
            # 开关决定。现一律改为下发**生效值**。
            "autofix_keywords": {
                name: sorted(keywords) for name, keywords in FACT_KEYWORDS.items()
            },
            "autofix_keyword_window_before": autofix_keyword_window_before(),
            "autofix_keyword_window_after": autofix_keyword_window_after(),
            "autofix_keyword_min_len": KEYWORD_MATCH_MIN_LEN,
            "autofix_scope": (
                "source_sections"
                if autofix_scope_to_source_sections()
                else "all_sections"
            ),
            "autofix_require_keyword": autofix_requires_fact_keyword(),
            # 2026-10-13：把 400 门槛一并下发，前端输入框可据此设 maxLength 提示
            "autofix_value_max_chars": autofix_value_max_chars(),
            "personnel_generic_keywords": sorted(PERSONNEL_GENERIC_KEYWORDS),
            "personnel_role_prefixes": list(PERSONNEL_ROLE_PREFIXES),
        },
        "audit_status": {
            "success": sorted(AUDIT_SUCCESS_STATUSES),
            "failed": AUDIT_STATUS_FAILED,
            "completed": AUDIT_STATUS_COMPLETED,
            "no_conflicts": AUDIT_STATUS_NO_CONFLICTS,
        },
        "conflict_state": {
            "open": CONFLICT_STATE_OPEN,
            "resolved": CONFLICT_STATE_RESOLVED,
        },
        # 冲突来源命名空间：`consistency_conflicts` 表同时承载「规则冲突」与
        # 「AI 语义发现」两条链路（ID 前缀约定），前端据此区分展示与禁用操作。
        # 新增顶层键（test_rules_payload_shape 已放宽为「必备键 ⊆ 顶层键」）。
        "conflict_source": {
            "rule": CONFLICT_SOURCE_RULE,
            "ai": CONFLICT_SOURCE_AI,
            "ai_id_prefix": AI_CONFLICT_ID_PREFIX,
            "meta": CONFLICT_SOURCE_META,
        },
        "scoring_status": {
            "meta": SCORING_STATUS_META,
            # 阈值下发：此前前端 `ScoringComplianceTab` 只展示判定结果、从不说明
            # 「weak 是在什么阈值下判的」，用户看到「疑似未答到点」却无法自行复核
            "coverage_weak": scoring_coverage_weak(),
        },
        "rejection_coverage": {
            "coverage_weak": rejection_coverage_weak(),
            # ENHANCE 2026-10-13（第六轮）：三档状态文案随快照下发，消除前端
            # 本地硬编码副本（statusLabel / statusColor 两张写死表）。
            "status_meta": REJECTION_STATUS_META,
        },
    }
