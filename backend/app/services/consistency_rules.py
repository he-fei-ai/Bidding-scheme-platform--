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
        and len(values) <= SEVERITY_HIGH_DISTINCT_THRESHOLD
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
# 对外暴露的规则快照
# ============================================================================


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
        "scoring_status": {"meta": SCORING_STATUS_META},
    }
