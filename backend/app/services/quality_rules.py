"""内容质量校验规则 —— 《装饰装修专项施工方案》文档质量分析报告·修复 15~24 落地。

## 背景

报告第二部分对 11 类内容问题给出了修复方案（修复 15~24）。这些规则的共同
特征是：**跨章节、语义级、需要结构化 Issue 输出**，与既有模块的分工如下：

===================== =============================================
既有模块               分工
===================== =============================================
`consistency_auditor`  事实值冲突（同一事实多个取值，如两个总工期）
`scoring_compliance`   评分点覆盖率（有没有写到这一章）
`export.check_document` 结构预检（空章节/孤立节点/控制字符）
本模块                 报告提出的内容质量规则（方案类型/工期加和/
                       规范引用/单位/套话/术语/重叠/编号去重）
===================== =============================================

## 输出契约

Issue 与 `chart_validators.check_duplicate_title_in_content`（CMP-07）同形：

    {"rule_id", "severity", "title", "message", "suggestion",
     "section_id", "section_title"}

`severity` 取值复用 `consistency_rules` 的 high/medium/low 常量，
保证预检/前端的严重度口径一致。

## 设计原则：宁可漏报，不可误报阻断

所有规则都是**告警级**（进 `DocumentCheckResult.warnings` / `quality_issues`，
不进 errors），并且：

- 方案类型无法推断时整组规则跳过（不做无谓告警）；
- 工期加和只有在「显式 a+b+c 加和表达式 + 全文总工期」同时存在才判罚；
- 规范引用只对**已收录**编号判名称错配，未收录编号降为 low；
- 套话/术语按规则与全文限额截断，防止一次预检刷出上千条。
"""

from __future__ import annotations

import inspect
import json
import logging
import re
from dataclasses import dataclass

from . import consistency_rules as rules

logger = logging.getLogger(__name__)


# ===========================================================================
# Issue 构造
# ===========================================================================


def make_issue(
    rule_id: str,
    severity: str,
    title: str,
    message: str,
    suggestion: str = "",
    section_id: int | None = None,
    section_title: str | None = None,
) -> dict:
    """构造结构化 Issue（与 CMP-07 同形）。"""
    return {
        "rule_id": rule_id,
        "severity": rules.normalize_severity(severity),
        "title": title,
        "message": message,
        "suggestion": suggestion,
        "section_id": section_id,
        "section_title": section_title,
    }


def _sec_title(section) -> str:
    """取章节标题（对任何输入都安全，返回纯文本）。"""
    return str(getattr(section, "title", "") or "").strip()


def _sec_content(section) -> str:
    """取章节正文（对任何输入都安全，返回纯文本）。"""
    return str(getattr(section, "content", "") or "")


def _sec_id(section) -> int | None:
    """取章节主键；不可解析时返回 None，不抛异常。"""
    try:
        value = getattr(section, "id", None)
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


# ===========================================================================
# 修复 16：方案类型模板校验（CMP-01 缺少必需章节 / CMP-02 禁止章节）
# ===========================================================================


@dataclass(frozen=True)
class SchemeProfile:
    """方案类型画像：识别关键词 + 必需章节组 + 禁止章节（子串, 严重度）。"""

    scheme_type: str
    keywords: tuple[str, ...]
    #: 每组 (章节说明, 候选子串) —— 组内任一子串命中即视为覆盖该必需章节
    required_any: tuple[tuple[str, tuple[str, ...]], ...]
    forbidden: tuple[tuple[str, str], ...]


SCHEME_TYPE_PROFILES: tuple[SchemeProfile, ...] = (
    SchemeProfile(
        scheme_type="装饰装修",
        keywords=("装饰装修", "装修", "修缮", "维修改造", "涂饰", "饰面"),
        required_any=(
            ("工程概况", ("工程概况", "工程简介")),
            ("编制依据", ("编制依据", "编制说明")),
            ("施工进度/施工计划", ("施工进度", "施工计划", "进度计划")),
            ("施工工艺", ("施工工艺", "工艺技术", "施工方法")),
            ("安全保证", ("安全",)),
            ("验收要求", ("验收",)),
            ("应急处置", ("应急",)),
        ),
        forbidden=(
            # 报告内容问题 11：装饰装修专项方案混入综合方案模板章节
            ("PC工程吊装", rules.SEVERITY_HIGH),
            ("吊装专项", rules.SEVERITY_HIGH),
            ("深基坑", rules.SEVERITY_HIGH),
            ("基坑支护", rules.SEVERITY_HIGH),
            ("基坑降水", rules.SEVERITY_HIGH),
            ("基坑排水", rules.SEVERITY_HIGH),
            ("桩基", rules.SEVERITY_HIGH),
            ("土方开挖", rules.SEVERITY_HIGH),
            ("塔吊", rules.SEVERITY_HIGH),
            ("大体积混凝土", rules.SEVERITY_HIGH),
            ("高支模", rules.SEVERITY_MEDIUM),
            ("脚手架专项", rules.SEVERITY_MEDIUM),
            # FIX 2026-10-26（《招投标软件导出文档 — 分类问题报告》D-1~D-7）：
            # 既有科研楼室内装修项目混入土建/外立面章节。以下术语在室内装修
            # 项目中通常不适用，但可能出现在「地下室装修」等合理场景，
            # 故统一降为 medium，仅告警不阻断。
            ("脚手架连墙件", rules.SEVERITY_MEDIUM),
            ("外墙脚手架", rules.SEVERITY_MEDIUM),
            ("外墙技术交底", rules.SEVERITY_MEDIUM),
            ("混凝土养护", rules.SEVERITY_MEDIUM),
            ("主体结构", rules.SEVERITY_MEDIUM),
            ("地下室", rules.SEVERITY_MEDIUM),
        ),
    ),
    SchemeProfile(
        scheme_type="脚手架",
        keywords=("脚手架",),
        required_any=(
            ("工程概况", ("工程概况", "工程简介")),
            ("编制依据", ("编制依据", "编制说明")),
            ("安全保证", ("安全",)),
            ("应急处置", ("应急",)),
            ("计算书", ("计算书", "受力验算", "承载力验算")),
        ),
        forbidden=(
            ("PC工程吊装", rules.SEVERITY_HIGH),
            ("装饰装修专项", rules.SEVERITY_MEDIUM),
            ("深基坑", rules.SEVERITY_MEDIUM),
        ),
    ),
    SchemeProfile(
        scheme_type="临时用电",
        keywords=("临时用电", "施工用电"),
        required_any=(
            ("工程概况", ("工程概况", "工程简介")),
            ("编制依据", ("编制依据", "编制说明")),
            ("安全保证", ("安全",)),
            ("应急处置", ("应急",)),
        ),
        forbidden=(("PC工程吊装", rules.SEVERITY_HIGH),),
    ),
)

#: 推断优先级：装饰装修方案常含脚手架章节，反之罕见 —— 先命中者为准
_PROFILE_ORDER = {p.scheme_type: i for i, p in enumerate(SCHEME_TYPE_PROFILES)}


def infer_scheme_type(
    project_name: str = "", section_titles: list[str] | None = None
) -> str | None:
    """推断方案类型：项目名优先，目录标题兜底；无法判定返回 None（跳过整组校验）。"""
    # (优先级, -命中关键词数, scheme_type)
    candidates: list[tuple[int, int, str]] = []
    for source in (project_name or "", *tuple(section_titles or ())):
        if not source:
            continue
        for profile in SCHEME_TYPE_PROFILES:
            hits = sum(1 for kw in profile.keywords if kw in source)
            if hits:
                candidates.append(
                    (_PROFILE_ORDER[profile.scheme_type], -hits, profile.scheme_type)
                )
    if not candidates:
        return None
    # 优先级小者优先；同优先级取命中关键词多者
    candidates.sort(key=lambda item: (item[0], item[1]))
    return candidates[0][2]


def validate_scheme_sections(
    sections: list,
    project_name: str = "",
    scheme_type: str | None = None,
) -> list[dict]:
    """修复 16：校验章节结构是否符合方案类型（CMP-01 / CMP-02）。"""
    issues: list[dict] = []
    if not sections:
        return issues
    scheme = scheme_type or infer_scheme_type(
        project_name, [_sec_title(s) for s in sections]
    )
    if not scheme:
        return issues
    profile = next((p for p in SCHEME_TYPE_PROFILES if p.scheme_type == scheme), None)
    if profile is None:
        return issues

    titles = [(s, _sec_title(s)) for s in sections]
    all_text = "\n".join(t for _, t in titles)

    for label, candidates in profile.required_any:
        if not any(c in all_text for c in candidates):
            issues.append(
                make_issue(
                    "CMP-01",
                    rules.SEVERITY_HIGH,
                    "缺少必需章节",
                    f"{scheme}方案缺少必需章节：{label}",
                    "补充该章节后重新导出",
                )
            )

    for s, title in titles:
        if not title:
            continue
        for keyword, severity in profile.forbidden:
            if keyword in title:
                issues.append(
                    make_issue(
                        "CMP-02",
                        severity,
                        "章节与方案类型不符",
                        f"{scheme}方案不应包含章节：「{title}」（命中禁止项「{keyword}」）",
                        "删除该章节或更换方案类型模板",
                        section_id=_sec_id(s),
                        section_title=title,
                    )
                )
                break  # 同一章节只报一次（多个禁止项同时命中取首个）
    return issues


# ===========================================================================
# 修复 17：工期一致性校验（CON-01 阶段工期之和 ≠ 总工期）
# ===========================================================================

_TOTAL_DURATION_RE = re.compile(
    r"(?:总工期|合同工期|计划工期|建设工期)[^\n]{0,12}?(\d{1,4})\s*(?:个日历天|日历天|天)"
)
_TOTAL_CALENDAR_RE = re.compile(r"(\d{1,4})\s*日历天")
_STAGE_LINE_RE = re.compile(r"阶段|分项|各工序|各阶段")


def _extract_total_duration(text: str) -> int | None:
    """全文总工期（日历天）：优先「总工期…N天」句式，退回「N日历天」。"""
    m = _TOTAL_DURATION_RE.search(text)
    if m:
        return int(m.group(1))
    m = _TOTAL_CALENDAR_RE.search(text)
    if m:
        return int(m.group(1))
    return None


def _extract_stage_sum(line: str) -> list[int] | None:
    """从「阶段工期」行提取加和项。

    兼容两种现实形态（`=` 左侧全部为加数，右侧为合计）：
      - 裸数字：`各阶段工期：15+30+120+18=183天`
      - 带中文单位：`准备阶段15天+拆除阶段30天+收尾18天=183天`
    任一段取不到数字（表达式被文本打断）即放弃该行，宁可漏报不误报。
    """
    if "+" not in line:
        return None
    left = re.split(r"[=＝]", line)[0]
    parts = left.split("+")
    if len(parts) < 2:
        return None
    terms: list[int] = []
    for part in parts:
        m = re.search(r"(\d{1,4})", part)
        if not m:
            return None
        terms.append(int(m.group(1)))
    return terms or None


def validate_schedule_consistency(sections: list) -> list[dict]:
    """修复 17：各阶段工期之和与总工期不符（CON-01，报告内容问题 5）。

    报告实例：总工期 195 日历天，各阶段 15+30+120+18=183 天。
    只有同时满足「显式加和表达式所在行提到工期/阶段」且「全文能提取到总工期」
    才判罚，避免把普通算式（配比、坐标）误判成工期。
    """
    issues: list[dict] = []
    full_text = "\n".join(_sec_content(s) for s in sections)
    total = _extract_total_duration(full_text)
    if not total:
        return issues

    for s in sections:
        content = _sec_content(s)
        if not content:
            continue
        for line in content.split("\n"):
            if not _STAGE_LINE_RE.search(line):
                continue
            if "天" not in line and "工期" not in line:
                continue
            terms = _extract_stage_sum(line)
            if not terms:
                continue
            # 防误报：单项工期 ≤ 366 天、加和 ≤ 3 个总工期（超出即非工期语义）
            if max(terms) > 366 or sum(terms) > total * 3:
                continue
            stage_sum = sum(terms)
            if stage_sum != total:
                issues.append(
                    make_issue(
                        "CON-01",
                        rules.SEVERITY_HIGH,
                        "工期不一致",
                        (
                            f"章节「{_sec_title(s) or '?'}」各阶段工期之和（{stage_sum}天，"
                            f"{' + '.join(str(t) for t in terms)}）与总工期（{total}天）不符"
                        ),
                        "核对阶段工期划分，使各阶段之和等于总工期",
                        section_id=_sec_id(s),
                        section_title=_sec_title(s),
                    )
                )
    return issues


# ===========================================================================
# 修复 19：规范引用校验（STD-02 名称错配 / STD-03 未收录）
# ===========================================================================

#: 已收录的规范编号 → 标准名称（编号为归一化形态：去空格、全大写、统一破折号）。
#: 首批数据来自报告修复 19 的样例与装饰装修/临时用电常用规范。
STANDARD_DATABASE: dict[str, str] = {
    "GB50210-2018": "建筑装饰装修工程质量验收标准",
    "GB50325-2020": "民用建筑工程室内环境污染控制标准",
    "GB50222-2017": "建筑内部装修设计防火规范",
    "GB55034-2022": "建筑与市政施工现场安全卫生与职业健康通用规范",
    "JGJ/T46-2024": "建筑与市政工程施工现场临时用电安全技术标准",
    "JGJ46-2005": "施工现场临时用电安全技术规范",
    "GB50411-2019": "建筑节能工程施工质量验收标准",
    "GB50209-2010": "建筑地面工程施工质量验收规范",
    "GB33372-2020": "胶粘剂中有害物质限量",
    "GB/T23997-2009": "室内装饰装修用水性木器涂料",
    "JGJ/T231-2021": "建筑施工承插型盘扣式钢管脚手架安全技术标准",
    "GB50300-2013": "建筑工程施工质量验收统一标准",
    "GB50327-2001": "住宅装饰装修工程施工规范",
    "GB50243-2016": "通风与空调工程施工质量验收规范",
    "GB50303-2015": "建筑电气工程施工质量验收规范",
    "GB50242-2002": "建筑给水排水及采暖工程施工质量验收规范",
    "GB50204-2015": "混凝土结构工程施工质量验收规范",
    "GB50205-2020": "钢结构工程施工质量验收标准",
    "GB/T50326-2017": "建设工程项目管理规范",
    "JGJ59-2011": "建筑施工安全检查标准",
    "GB50720-2011": "建设工程施工现场消防安全技术规范",
    "GB50016-2014": "建筑设计防火规范（2018年版）",
    "GB18580-2017": "室内装饰装修材料人造板及其制品中甲醛释放限量",
    "GB18581-2020": "木器涂料中有害物质限量",
    "GB18582-2020": "建筑用墙面涂料中有害物质限量",
    "GB18583-2008": "室内装饰装修材料胶粘剂中有害物质限量",
    "GB50189-2015": "公共建筑节能设计标准",
    "GB50034-2013": "建筑照明设计标准",
    "GB50176-2016": "民用建筑热工设计规范",
    "GB/T50378-2019": "绿色建筑评价标准",
    "GB55005-2021": "木结构通用规范",
    "GB55001-2021": "工程结构通用规范",
}

_STD_REF_RE = re.compile(
    r"《([^》]{2,60})》\s*[（(]\s*"
    r"([A-Za-z]{1,6}\s*(?:/\s*T)?\s*\d+(?:\.\d+)?\s*[-—–－]\s*\d{4})\s*[）)]"
)


def _normalize_std_code(code: str) -> str:
    """规范编号归一：去空格、大写、统一破折号（`GB 50325-2020` → `GB50325-2020`）。"""
    s = re.sub(r"\s+", "", code or "").upper()
    for dash in ("—", "–", "－"):
        s = s.replace(dash, "-")
    return s


def validate_standard_references(sections: list) -> list[dict]:
    """修复 19：规范引用编号与名称错配（STD-02）/ 编号未收录（STD-03）。

    报告实例：
      - 《建筑外墙用氟碳漆》（GB/T23997-2009）—— GB/T 23997 实为
        《室内装饰装修用水性木器涂料》→ STD-02；
      - 《地面材料中有害物质限量》（GB 33372-2020）—— 实为
        《胶粘剂中有害物质限量》→ STD-02。
    """
    issues: list[dict] = []
    unknown_codes: dict[str, str] = {}  # 归一编号 → 首见名称（去重后只报一次）
    seen_mismatch: set[str] = set()

    for s in sections:
        content = _sec_content(s)
        if not content or "《" not in content:
            continue
        for m in _STD_REF_RE.finditer(content):
            name = m.group(1).strip()
            code = _normalize_std_code(m.group(2))
            expected = STANDARD_DATABASE.get(code)
            if expected is None:
                unknown_codes.setdefault(code, name)
                continue
            if name == expected or expected in name or name in expected:
                continue
            if code in seen_mismatch:
                continue
            seen_mismatch.add(code)
            issues.append(
                make_issue(
                    "STD-02",
                    rules.SEVERITY_HIGH,
                    "规范引用错误",
                    (
                        f"规范编号 {code} 对应名称应为「{expected}」，"
                        f"实际引用「{name}」"
                    ),
                    "核对规范编号与名称的对应关系",
                    section_id=_sec_id(s),
                    section_title=_sec_title(s),
                )
            )

    for code, name in unknown_codes.items():
        issues.append(
            make_issue(
                "STD-03",
                rules.SEVERITY_LOW,
                "规范编号未收录",
                f"规范编号 {code}（{name}）未在标准库中，请核实编号是否正确",
                "核实规范编号；如为新标准可反馈补充标准库",
            )
        )
    return issues


# ===========================================================================
# 修复 18：数值单位校验（STD-01 限值缺单位 / STD-04 元素符号混淆）
# ===========================================================================

#: 室内环境有害物质限值语句（报告内容问题 6 的场景）
_POLLUTANT_RE = re.compile(
    r"甲醛|TVOC|总挥发性|苯系物|甲苯|二甲苯|乙苯|苯|氨|氡"
)
#: 限值表达式：`≤0.07` / `< 0.05` / `≤ =` 兼容全角
_LIMIT_VALUE_RE = re.compile(r"[≤<]\s*=?\s*(\d+(?:\.\d+)?)")
#: 合法单位（限值语句上下文窗口内命中即认为单位完整）
_LIMIT_UNIT_RE = re.compile(
    r"mg\s*/\s*m\s*[³3]|μg\s*/\s*m|ug\s*/\s*m|Bq\s*/\s*m|mg\s*/\s*kg"
    r"|mg\s*/\s*L|ppm|ppb|%|℃|μg|ug",
    re.IGNORECASE,
)
#: 报告内容问题 6：表格中「氨 222Rn」应为「氡 222Rn」
_RN_CONFUSION_RE = re.compile(r"氨\s*222\s*Rn|222\s*Rn\s*氨")


def validate_limit_units(sections: list) -> list[dict]:
    """修复 18：有害物质限值缺单位（STD-01）、氨/氡符号混淆（STD-04）。

    只针对「污染物名 + 限值符号」这一强上下文，不做全文任意数字查单位
    （那会把日期、编号、轴线全部误报）。
    """
    issues: list[dict] = []
    seen_no_unit: set[str] = set()

    for s in sections:
        content = _sec_content(s)
        if not content:
            continue

        for m in _RN_CONFUSION_RE.finditer(content):
            issues.append(
                make_issue(
                    "STD-04",
                    rules.SEVERITY_HIGH,
                    "元素符号混淆",
                    (
                        f"章节「{_sec_title(s) or '?'}」中「{m.group(0).strip()}」有误："
                        "222Rn 是氡（Rn），不是氨（NH3）"
                    ),
                    "将「氨 222Rn」更正为「氡 222Rn」",
                    section_id=_sec_id(s),
                    section_title=_sec_title(s),
                )
            )
            break  # 每章只报一次

        for pm in _POLLUTANT_RE.finditer(content):
            # 以污染物名为中心取上下文窗口，窗口内找限值表达式
            window_end = min(len(content), pm.end() + 80)
            window = content[pm.start(): window_end]
            vm = _LIMIT_VALUE_RE.search(window)
            if not vm:
                continue
            # 单位检查范围：污染物名 → 限值数值 + 余量（单位可能写在数值前后）
            unit_zone = content[pm.start(): min(len(content), vm.end() + 24)]
            if _LIMIT_UNIT_RE.search(unit_zone):
                continue
            value = vm.group(1) or "?"
            key = f"{pm.group(0)}{value}"
            if key in seen_no_unit:
                continue
            seen_no_unit.add(key)
            issues.append(
                make_issue(
                    "STD-01",
                    rules.SEVERITY_MEDIUM,
                    "数值缺单位",
                    (
                        f"章节「{_sec_title(s) or '?'}」{pm.group(0)}限值「≤{value}」"
                        "缺少单位（应为 mg/m³ 等）"
                    ),
                    "补全限值单位并核对数值",
                    section_id=_sec_id(s),
                    section_title=_sec_title(s),
                )
            )
    return issues


# ===========================================================================
# 修复 20：空话套话检测（QLT-01）/ 修复 21：术语统一检测（QLT-02）
# ===========================================================================

#: 报告修复 20 的套话模式 + 同类常见模板句
FILLER_PATTERNS: tuple[str, ...] = (
    r"确保[^，。；\n]{0,14}(?:完成|实现|达成|受控|到位|正常)",
    r"切实[^，。；\n]{0,14}(?:做好|落实|加强|抓好)",
    r"高度重视[^，。；\n]{0,20}",
    r"建立健全[^，。；\n]{0,20}",
    r"严格按照[^，。；\n]{0,20}",
    r"进一步[^，。；\n]{0,12}(?:加强|提高|完善|优化|规范)",
    r"全面提升[^，。；\n]{0,14}",
    r"认真贯彻[^，。；\n]{0,20}",
)
_FILLER_RES = [re.compile(p) for p in FILLER_PATTERNS]

#: 单章节每种套话最多报 2 条 / 全文最多 40 条（防止预检刷屏）
_FILLER_PER_SECTION_CAP = 2
_FILLER_TOTAL_CAP = 40


def detect_filler_content(sections: list) -> list[dict]:
    """修复 20：检测空话套话（QLT-01，报告内容问题 8，severity=low）。"""
    issues: list[dict] = []
    total = 0
    for s in sections:
        content = _sec_content(s)
        if not content:
            continue
        section_count = 0
        for regex in _FILLER_RES:
            for m in regex.finditer(content):
                if section_count >= _FILLER_PER_SECTION_CAP or total >= _FILLER_TOTAL_CAP:
                    break
                issues.append(
                    make_issue(
                        "QLT-01",
                        rules.SEVERITY_LOW,
                        "空话套话",
                        (
                            f"章节「{_sec_title(s) or '?'}」检测到空洞表述："
                            f"「{m.group(0)}」"
                        ),
                        "替换为具体措施、责任主体或量化指标",
                        section_id=_sec_id(s),
                        section_title=_sec_title(s),
                    )
                )
                section_count += 1
                total += 1
            if section_count >= _FILLER_PER_SECTION_CAP or total >= _FILLER_TOTAL_CAP:
                break
        if total >= _FILLER_TOTAL_CAP:
            break
    return issues


@dataclass(frozen=True)
class TermRule:
    """术语统一规则：标准词 + 混用形式（可带复合词前缀排除，防误报）。"""

    standard: str
    variants: tuple[str, ...]
    #: 复合词前缀：变体前若以这些词结尾则不判
    #: （如「产业园区」中「园区」前是「业」而非「产」，按字符排除会漏）
    exclude_prefixes: tuple[str, ...] = ()
    #: 仅当全文命中此上下文词时才启用（如幼儿类术语仅在幼儿园项目启用）
    require_context: str = ""


#: 「园区」的常见复合词前缀（不作为幼儿园项目的术语混用计数）
园区_COMPOUND_PREFIXES: tuple[str, ...] = (
    "产业", "科技", "工业", "商贸", "智慧", "示范", "绿色", "文化",
    "教育", "医疗", "居住", "物流", "农业", "生态", "文体", "商务",
)

TERMINOLOGY_RULES: tuple[TermRule, ...] = (
    TermRule("建设单位", ("发包人", "甲方")),
    TermRule("施工单位", ("承包人", "乙方")),
    TermRule("装饰装修", ("修缮", "维修改造")),
    TermRule("幼儿园", ("园所",)),
    TermRule("幼儿园", ("园区",), exclude_prefixes=园区_COMPOUND_PREFIXES),
    TermRule("幼儿", ("儿童", "小朋友"), require_context="幼儿"),
)

#: 每条术语规则全文最多报 1 条（附计数），防止刷屏
_TERMINOLOGY_GLOBAL_CAP = 12


def detect_terminology_issues(
    sections: list, project_name: str = ""
) -> list[dict]:
    """修复 21：专业术语不统一检测（QLT-02，报告内容问题 9）。

    仅**检测告警**，不做自动替换：自动替换（报告修复 21 的 unify_terminology）
    在通用平台上有误伤风险（如「产业园区」被改成「产幼儿园」），把决定权留给用户。
    """
    issues: list[dict] = []
    full_text = "\n".join(_sec_content(s) for s in sections)
    context_source = (project_name or "") + "\n" + full_text

    for rule in TERMINOLOGY_RULES:
        if rule.require_context and rule.require_context not in context_source:
            continue
        for variant in rule.variants:
            count = 0
            example_title = ""
            example_section: object | None = None
            for s in sections:
                content = _sec_content(s)
                if not content or variant not in content:
                    continue
                for m in re.finditer(re.escape(variant), content):
                    if rule.exclude_prefixes:
                        prior = content[max(0, m.start() - 8): m.start()]
                        if any(prior.endswith(p) for p in rule.exclude_prefixes):
                            continue
                    count += 1
                    if not example_title:
                        example_title = _sec_title(s)
                        example_section = s
            if count <= 0:
                continue
            issues.append(
                make_issue(
                    "QLT-02",
                    rules.SEVERITY_LOW,
                    "术语不统一",
                    (
                        f"术语「{variant}」出现 {count} 处，"
                        f"建议统一为「{rule.standard}」"
                        + (f"（例：章节「{example_title}」）" if example_title else "")
                    ),
                    f"全文将「{variant}」统一为「{rule.standard}」",
                    section_id=_sec_id(example_section),
                    section_title=example_title or None,
                )
            )
            if len(issues) >= _TERMINOLOGY_GLOBAL_CAP:
                return issues
    return issues


# ===========================================================================
# 修复 22：章节内容重叠检测（DUP-01）/ 修复 12：空章节（DLV-01）
# ===========================================================================

#: 重叠检测参数：仅参与正文 ≥ 150 字的章节；字符 3-gram；阈值取报告的 0.6
_OVERLAP_MIN_CHARS = 150
_OVERLAP_SHINGLE_N = 3
_OVERLAP_THRESHOLD = 0.6
_OVERLAP_SAMPLE_CHARS = 1200
_OVERLAP_MAX_ISSUES = 10

#: 报告修复 12 的空章节阈值（与 check_document 的「少于50字」同口径）
_EMPTY_SECTION_MIN_CHARS = 50


def _shingles(text: str, n: int = _OVERLAP_SHINGLE_N) -> set[str]:
    """字符 n-gram 集合（去空白与标点，仅对截断样本计算）。"""
    normalized = re.sub(r"[\s\W]+", "", text)[:_OVERLAP_SAMPLE_CHARS]
    if len(normalized) < n:
        return {normalized} if normalized else set()
    return {normalized[i: i + n] for i in range(len(normalized) - n + 1)}


def detect_content_overlap(sections: list) -> list[dict]:
    """修复 22：章节内容重叠检测（DUP-01，报告内容问题 10）。"""
    issues: list[dict] = []
    candidates: list[tuple[object, set[str]]] = []
    for s in sections:
        content = _sec_content(s)
        if len(content) < _OVERLAP_MIN_CHARS:
            continue
        sh = _shingles(content)
        if sh:
            candidates.append((s, sh))

    checked = 0
    for i, (s1, sh1) in enumerate(candidates):
        if len(issues) >= _OVERLAP_MAX_ISSUES:
            break
        for s2, sh2 in candidates[i + 1:]:
            if len(issues) >= _OVERLAP_MAX_ISSUES:
                break
            checked += 1
            if checked > 3000:  # 病态规模保护（数百章节两两组合）
                return issues
            union = len(sh1 | sh2)
            if not union:
                continue
            similarity = len(sh1 & sh2) / union
            if similarity >= _OVERLAP_THRESHOLD:
                issues.append(
                    make_issue(
                        "DUP-01",
                        rules.SEVERITY_MEDIUM,
                        "章节内容重叠",
                        (
                            f"章节「{_sec_title(s1) or '?'}」与"
                            f"「{_sec_title(s2) or '?'}」内容相似度 "
                            f"{similarity:.0%}"
                        ),
                        "合并或重新划分章节，消除重复表述",
                        section_id=_sec_id(s1),
                        section_title=_sec_title(s1),
                    )
                )
    return issues


def detect_empty_sections(sections: list) -> list[dict]:
    """修复 12：空章节 / 过短章节结构化输出（DLV-01，报告格式问题 14）。"""
    issues: list[dict] = []
    for s in sections:
        content = _sec_content(s).strip()
        if len(content) >= _EMPTY_SECTION_MIN_CHARS:
            continue
        empty = not content
        issues.append(
            make_issue(
                "DLV-01",
                rules.SEVERITY_HIGH if empty else rules.SEVERITY_MEDIUM,
                "空章节" if empty else "章节正文过短",
                (
                    f"章节「{_sec_title(s) or '?'}」"
                    + ("缺少正文" if empty else f"正文仅 {len(content)} 字（<50 字）")
                ),
                "重新生成或补写该章节内容",
                section_id=_sec_id(s),
                section_title=_sec_title(s),
            )
        )
    return issues


# ===========================================================================
# 修复 24：章节编号全局去重（CMP-08）
# ===========================================================================

#: 标题中显式携带的编号前缀（目录导入/手工录入可能保留）
_LEADING_NUMBER_TOKEN_RE = re.compile(
    r"^\s*(第[一二三四五六七八九十百千\d]+[章节]"
    r"|[一二三四五六七八九十]+[、．.]"
    r"|\d+(?:\.\d+)+[、．.]?"
    r"|\d{1,3}[、．.）)])"
)


def detect_duplicate_numbering(sections: list) -> list[dict]:
    """修复 24：同级兄弟标题显式编号重复（CMP-08，报告内容问题 3）。

    平台正常链路的编号由树位置派生（导出时重排），标题本身不带编号；
    该规则针对**导入/手工录入**仍携带编号的标题 —— 报告「第二节」重复
    即由此类数据产生。未携带编号的标题不参与判定（不误报）。
    """
    issues: list[dict] = []
    by_parent: dict[object, list[object]] = {}
    for s in sections:
        by_parent.setdefault(getattr(s, "parent_id", None), []).append(s)

    for _parent, siblings in by_parent.items():
        token_map: dict[str, list[object]] = {}
        for s in siblings:
            title = _sec_title(s)
            if not title:
                continue
            m = _LEADING_NUMBER_TOKEN_RE.match(title)
            if not m:
                continue
            token_map.setdefault(m.group(1), []).append(s)
        for token, group in token_map.items():
            if len(group) < 2:
                continue
            titles = "、".join(_sec_title(s) for s in group[:4])
            issues.append(
                make_issue(
                    "CMP-08",
                    rules.SEVERITY_MEDIUM,
                    "章节编号重复",
                    (
                        f"同级目录中编号「{token}」重复出现 {len(group)} 次：{titles}"
                        + ("…" if len(group) > 4 else "")
                    ),
                    "重新生成目录或手工修正编号，确保同级唯一",
                    section_id=_sec_id(group[0]),
                    section_title=_sec_title(group[0]),
                )
            )
    return issues


# ===========================================================================
# 修复 25：标题-正文语义错位（CMP-09）/ 正文截断（DLV-02）
# 《招投标软件导出文档 — 分类问题报告》C-1~C-9 / H-1
# ===========================================================================

#: 标题/正文比对时忽略的通用词二元组 —— 它们在任意章节都会出现，
#: 若计入会让「标题-正文」重合度虚高，掩盖真正的文不对题。
_TITLE_GENERIC_BIGRAMS: frozenset[str] = frozenset(
    {
        "措施", "方案", "要求", "指标", "工艺", "技术", "施工", "管理",
        "控制", "计划", "配置", "程序", "标准", "验收", "要点", "方法",
        "体系", "工作", "工程", "项目", "质量", "安全", "组织", "实施",
        "应用", "分析", "规定", "内容", "相关", "进行", "确保", "加强",
        "落实", "建立", "完善", "提高", "保证", "情况", "方面", "过程",
    }
)

#: 标题里需要剔除的装饰性字符（编号、标点、括号、连接符）
_TITLE_NOISE_RE = re.compile(r"[\s\u3000，。；：、（）()《》“”\"'\[\]【】\-—…·/]+")

#: 判定阈值：标题非通用二元组在正文中的命中率低于该值即视为错位
_TITLE_MATCH_MIN_RATIO = 0.15
#: 正文过短/标题过短时不判罚（样本不足，易误报）
_TITLE_MATCH_MIN_CONTENT_CHARS = 100
_TITLE_MATCH_MIN_TITLE_CHARS = 5
#: 单次预检最多报 15 条，防止刷屏
_TITLE_MATCH_MAX_ISSUES = 15


def _title_bigrams(title: str) -> set[str]:
    """标题 → 非通用字符二元组集合（用于与正文做字面重合度比对）。"""
    cleaned = _LEADING_NUMBER_TOKEN_RE.sub("", title or "")
    cleaned = _TITLE_NOISE_RE.sub("", cleaned)
    grams = {cleaned[i: i + 2] for i in range(len(cleaned) - 1)}
    return {g for g in grams if g not in _TITLE_GENERIC_BIGRAMS}


def detect_title_content_mismatch(sections: list) -> list[dict]:
    """修复 25：标题与正文语义错位（CMP-09，报告 C-1~C-9）。

    报告实例：「消防管线支吊架固定」的正文却是「安全管理与文明施工」、
    「给排水管道耐压耐腐蚀指标」的正文却是「工程概况」。此类错位会让
    评标专家一眼判定为模板拼接，必须阻断。

    实现：标题去掉编号与标点后取二元组（剔除通用词），与正文做字面重合
    检查。标题的主题词在正文中完全找不到（命中率 < 15%）且正文长度足够，
    判定为错位。宁可漏报不误报：标题过短或正文过短一律跳过。
    """
    issues: list[dict] = []
    for s in sections:
        if len(issues) >= _TITLE_MATCH_MAX_ISSUES:
            break
        title = _sec_title(s)
        content = _sec_content(s)
        cleaned_title = _TITLE_NOISE_RE.sub(
            "", _LEADING_NUMBER_TOKEN_RE.sub("", title)
        )
        if len(cleaned_title) < _TITLE_MATCH_MIN_TITLE_CHARS:
            continue
        if len(content.strip()) < _TITLE_MATCH_MIN_CONTENT_CHARS:
            continue
        grams = _title_bigrams(title)
        if not grams:
            continue
        hit = sum(1 for g in grams if g in content)
        ratio = hit / len(grams)
        if ratio >= _TITLE_MATCH_MIN_RATIO:
            continue
        issues.append(
            make_issue(
                "CMP-09",
                rules.SEVERITY_HIGH,
                "标题与正文不符",
                (
                    f"章节「{title}」的正文与该标题主题无关"
                    f"（主题词命中率 {ratio:.0%}），疑似模板内容错位"
                ),
                "围绕标题重写该章节正文，或修正标题使其与正文一致",
                section_id=_sec_id(s),
                section_title=title,
            )
        )
    return issues


#: 正文以这些字符收尾 → 句子未写完（正常的段落应终于句号/问号/右括号）
_TRUNCATED_TAIL_CHARS = ("，", ",", "、", "：", ":", "；", ";", "（", "(")


#: 同级标题重复检测的最小标题长度（短标题噪声大，不参与）
_DUP_TITLE_MIN_CHARS = 4
#: 单次预检最多报 15 条
_DUP_TITLE_MAX_ISSUES = 15


def detect_duplicate_titles(sections: list) -> list[dict]:
    """修复 25：同级标题重复（CMP-10，报告 B-5 / I-1）。

    报告实例：第十五节「一、主要分部工程施工方案」与「二、关键工序施工技术
    措施」下各有一个「1、施工工艺流程与要点」；多个子节反复使用同一
    「工程概况」标题（报告 B-2）。同一层级下标题完全相同的节点只会让
    目录看起来像模板复制，评标专家会直接扣分。
    """
    groups: dict[tuple[int, str], list] = {}
    for s in sections:
        title = _sec_title(s)
        if len(title) < _DUP_TITLE_MIN_CHARS:
            continue
        # 去掉显式编号后再比对：「1、施工工艺流程与要点」与「施工工艺流程与要点」
        # 视为同名（编号由导出端按树位置重排，不构成区分信息）。
        normalized = _LEADING_NUMBER_TOKEN_RE.sub("", title).strip()
        if len(normalized) < _DUP_TITLE_MIN_CHARS:
            continue
        level = int(getattr(s, "level", 0) or 0)
        groups.setdefault((level, normalized), []).append(s)

    issues: list[dict] = []
    for (level, normalized), group in groups.items():
        if len(group) < 2:
            continue
        issues.append(
            make_issue(
                "CMP-10",
                rules.SEVERITY_MEDIUM,
                "同级标题重复",
                (
                    f"第 {level} 级目录中标题「{normalized}」重复出现 "
                    f"{len(group)} 次（{('、'.join(_sec_title(s) for s in group[:4]))}）"
                ),
                "合并重复章节或改用与本章内容对应的差异化标题",
                section_id=_sec_id(group[0]),
                section_title=_sec_title(group[0]),
            )
        )
        if len(issues) >= _DUP_TITLE_MAX_ISSUES:
            break
    return issues


def detect_truncated_sections(sections: list) -> list[dict]:
    """修复 25：正文疑似截断（DLV-02，报告 H-1）。

    报告实例：「废料暂存点与清运路线」只写了开头两段就出现系统提示并被截断。
    系统提示已在清洗管线删除，但截断留下的半句仍需暴露给用户：
    正文以逗号/顿号/冒号等「非句末标点」收尾即判定为疑似截断。
    """
    issues: list[dict] = []
    for s in sections:
        content = _sec_content(s).strip()
        if len(content) < _EMPTY_SECTION_MIN_CHARS:
            continue  # 空/过短已由 DLV-01 承载
        if not content.endswith(_TRUNCATED_TAIL_CHARS):
            continue
        issues.append(
            make_issue(
                "DLV-02",
                rules.SEVERITY_HIGH,
                "正文疑似截断",
                f"章节「{_sec_title(s) or '?'}」正文以「{content[-1]}」收尾，疑似未写完",
                "重新生成该章节或补全结尾内容",
                section_id=_sec_id(s),
                section_title=_sec_title(s),
            )
        )
    return issues


# ===========================================================================
# 修复 26：图表一致性（CHT-01 图题无图 / CHT-02 图题重复）
# 《招投标软件导出文档 — 分类问题报告》F-2 / F-5 / E-6 / B-4
# ===========================================================================

#: Section 上承载图表数据的 JSON 字段（与 models.Section 保持一致）
_CHART_JSON_FIELDS: tuple[str, ...] = (
    "flowchart_json",
    "gantt_json",
    "architecture_json",
    "labor_json",
    "comparison_json",
    "layout_json",
    "timeline_json",
    "inlined_chart_json",
)

#: 正文中以「图X-Y」开头的图题行（行首锚定，避免误伤「如图1-1所示」）
_FIG_CAPTION_LINE_RE = re.compile(r"^[ \t]*图\s*\d+\s*[-－—]\s*\d+")
#: 图题前缀（归一化时剥离）
_FIG_NO_PREFIX_TITLE_RE = re.compile(r"^\s*图\s*\d+\s*[-－—]\s*\d+\s*[：:]?\s*")
#: 正文内的图表围栏 / 占位标记
_CHART_FENCE_RE = re.compile(r"```[ \t]*(?:mermaid|chart-json)", re.IGNORECASE)
_CHART_PLACEHOLDER_RE = re.compile(r"<<\s*(?:图表|CHART)\b|【\s*图表")
#: Markdown 图片 / HTML img（有真实图片引用时不算「图题无图」）
_INLINE_IMAGE_RE = re.compile(r"!\[|<img\b", re.IGNORECASE)
#: 图题重复判定最小长度（归一化后）
_CHART_TITLE_MIN_CHARS = 4
_CHART_ISSUE_MAX = 15


def _chart_payload_present(section) -> bool:
    """章节是否绑定了非空图表数据。"""
    for field in _CHART_JSON_FIELDS:
        value = getattr(section, field, None)
        if not isinstance(value, str):
            continue
        text = value.strip()
        if text and text not in ("{}", "[]", "null"):
            return True
    return False


def _chart_titles(section) -> list[str]:
    """提取章节图表数据中的标题（title / caption）。"""
    titles: list[str] = []
    for field in _CHART_JSON_FIELDS:
        raw = getattr(section, field, None)
        if not isinstance(raw, str) or not raw.strip():
            continue
        try:
            obj = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        items = obj if isinstance(obj, list) else [obj]
        for item in items:
            if not isinstance(item, dict):
                continue
            title = item.get("title") or item.get("caption")
            if isinstance(title, str) and title.strip():
                titles.append(title.strip())
    return titles


def detect_figure_without_payload(sections: list) -> list[dict]:
    """修复 26：正文有图题但无图（CHT-01，报告 F-5 / E-6）。

    报告现场：「图1-1装饰装修独立区段」等图题后没有任何图对象 —— AI 只写了
    图题文字，图表数据字段为空，导出时既不插图也不会报错。此处检出
    「有图题、无图表数据、无图表围栏、无图片引用」的章节。
    """
    issues: list[dict] = []
    for s in sections:
        if len(issues) >= _CHART_ISSUE_MAX:
            break
        content = _sec_content(s)
        if not content.strip():
            continue
        if _chart_payload_present(s):
            continue
        if _CHART_FENCE_RE.search(content) or _CHART_PLACEHOLDER_RE.search(content):
            continue
        if _INLINE_IMAGE_RE.search(content):
            continue
        captions = [
            line.strip()
            for line in content.splitlines()
            if _FIG_CAPTION_LINE_RE.match(line)
        ]
        if not captions:
            continue
        issues.append(
            make_issue(
                "CHT-01",
                rules.SEVERITY_HIGH,
                "图题无对应图",
                (
                    f"章节「{_sec_title(s) or '?'}」正文出现 {len(captions)} 条图题，"
                    f"但本章没有图表数据或图表块（如「{captions[0][:30]}」）"
                ),
                "补充该图的图表数据，或删除无图的孤立图题",
                section_id=_sec_id(s),
                section_title=_sec_title(s),
            )
        )
    return issues


def _chapter_root_map(sections: list) -> dict[int, int]:
    """parent_id 链路上溯到一级章节 id（图题重复只在同一章内判罚）。"""
    by_id = {}
    for s in sections:
        sid = _sec_id(s)
        if sid is not None:
            by_id[sid] = s

    def _root(section) -> int:
        seen: set[int] = set()
        cur = section
        while True:
            sid = _sec_id(cur)
            parent = getattr(cur, "parent_id", None)
            if sid in seen or parent is None or parent not in by_id:
                return sid if sid is not None else -1
            seen.add(sid)
            cur = by_id[parent]

    result: dict[int, int] = {}
    for s in sections:
        sid = _sec_id(s)
        if sid is not None:
            result[sid] = _root(s)
    return result


def detect_duplicate_figure_titles(sections: list) -> list[dict]:
    """修复 26：同一章内图题重复（CHT-02，报告 F-2 / B-4）。

    报告现场：「图7-2、图7-3、图7-4」三张图共用同一图题，评标时会被判为
    模板堆砌。归一化后（剥图号与标点）同名且同属一级章即判重复。
    """
    root_map = _chapter_root_map(sections)
    groups: dict[tuple[int, str], list[tuple[object, str]]] = {}
    for s in sections:
        sid = _sec_id(s)
        root = root_map.get(sid, -1)
        for title in _chart_titles(s):
            key = _TITLE_NOISE_RE.sub("", _FIG_NO_PREFIX_TITLE_RE.sub("", title))
            if len(key) < _CHART_TITLE_MIN_CHARS:
                continue
            groups.setdefault((root, key), []).append((s, title))

    issues: list[dict] = []
    for (_root, key), group in groups.items():
        if len(group) < 2:
            continue
        issues.append(
            make_issue(
                "CHT-02",
                rules.SEVERITY_MEDIUM,
                "同一章图题重复",
                (
                    f"同一章内图题「{key}」重复出现 {len(group)} 次，"
                    "导出后会出现多张同名图"
                ),
                "保留一张图，其余改用能区分内容的图题或删除",
                section_id=_sec_id(group[1][0]),
                section_title=_sec_title(group[1][0]),
            )
        )
        if len(issues) >= _CHART_ISSUE_MAX:
            break
    return issues


# ===========================================================================
# 修复 26：正文混用多级编号（CMP-11，报告 G-1 / G-2 / G-4）
# ===========================================================================

#: 行首多级点号编号（如 "1.2.1.1 围挡设置标准"），要求至少三段
_DOTTED_HEADING_LINE_RE = re.compile(r"^[ \t]*\d+(?:\.\d+){2,}[、．.]?\s*\S")
#: 只判定短行（标题形态），长行是正文叙述里的数据引用
_DOTTED_HEADING_MAX_CHARS = 60


def detect_content_numbering_style(sections: list) -> list[dict]:
    """修复 26：正文残留 AI 自编多级编号（CMP-11，报告 G-1 / G-2 / G-4）。

    平台口径是「编号由程序按章节树生成，AI 不输出编号」。正文里出现的
    「1.2.1.1 围挡设置标准」这类多级点号编号会与导出端自动编号叠加，
    形成报告所述的「1、施工隔离围挡设置」下直接挂 「1.2.1.1」的层级断裂。
    """
    issues: list[dict] = []
    for s in sections:
        if len(issues) >= _DUP_TITLE_MAX_ISSUES:
            break
        hits: list[str] = []
        for line in _sec_content(s).splitlines():
            stripped = line.strip()
            if not stripped or len(stripped) > _DOTTED_HEADING_MAX_CHARS:
                continue
            if _DOTTED_HEADING_LINE_RE.match(stripped):
                hits.append(stripped[:40])
        if not hits:
            continue
        issues.append(
            make_issue(
                "CMP-11",
                rules.SEVERITY_MEDIUM,
                "正文残留多级编号",
                (
                    f"章节「{_sec_title(s) or '?'}」正文含 {len(hits)} 处"
                    f"AI 自编多级编号（如「{hits[0]}」），会与程序自动编号叠加"
                ),
                "删除正文中的编号前缀，编号统一由导出端按章节树生成",
                section_id=_sec_id(s),
                section_title=_sec_title(s),
            )
        )
    return issues


# ===========================================================================
# 修复 26：必需表格 / 关键图缺失（DLV-03 / DLV-04，报告 H-9 / H-10）
# ===========================================================================

#: 方案类型 → 必需表格（表名, 正文候选关键词）
_REQUIRED_TABLES: dict[str, tuple[tuple[str, tuple[str, ...]], ...]] = {
    "装饰装修": (
        ("材料进场检验记录表", ("材料进场检验", "材料进场验收")),
        ("隐蔽工程验收记录表", ("隐蔽工程验收", "隐蔽验收")),
        ("消防联动调试记录表", ("消防联动调试", "联动调试")),
    ),
    "脚手架": (
        ("脚手架验收记录表", ("脚手架验收",)),
    ),
    "临时用电": (
        ("临时用电验收记录表", ("临时用电验收",)),
    ),
}

#: 方案类型 → 关键图（图名, 承载字段候选, 正文关键词候选）
_REQUIRED_CHARTS: dict[
    str, tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...]
] = {
    "装饰装修": (
        ("施工总平面布置图", ("layout_json",), ("总平面布置",)),
        ("施工进度计划横道图", ("gantt_json",), ("横道图", "进度计划")),
        ("劳动力计划曲线图", ("labor_json",), ("劳动力曲线", "劳动力计划")),
    ),
    "脚手架": (
        ("脚手架施工进度横道图", ("gantt_json",), ("横道图",)),
    ),
    "临时用电": (
        ("临时用电系统布置图", ("layout_json",), ("布置图",)),
    ),
}


def _resolve_scheme(sections: list, project_name: str) -> str | None:
    return infer_scheme_type(project_name, [_sec_title(s) for s in sections])


def detect_missing_required_tables(sections: list, project_name: str = "") -> list[dict]:
    """修复 26：必需表格缺失（DLV-03，报告 H-9）。

    装饰装修投标文件的评标细则通常要求材料进场检验、隐蔽工程验收、
    消防联动调试三类记录表；缺失直接影响评标得分。
    """
    scheme = _resolve_scheme(sections, project_name)
    if not scheme:
        return []
    required = _REQUIRED_TABLES.get(scheme)
    if not required:
        return []
    all_text = "\n".join(
        f"{_sec_title(s)}\n{_sec_content(s)}" for s in sections
    )
    issues: list[dict] = []
    for table_name, candidates in required:
        if any(kw in all_text for kw in candidates):
            continue
        issues.append(
            make_issue(
                "DLV-03",
                rules.SEVERITY_HIGH,
                "关键表格缺失",
                f"{scheme}方案缺少关键表格：{table_name}",
                "在相应章节插入该记录表模板（表头与必填字段需完整）",
            )
        )
    return issues


def detect_missing_key_charts(sections: list, project_name: str = "") -> list[dict]:
    """修复 26：关键图缺失（DLV-04，报告 H-10 / F-4）。

    判据：既无图表数据字段，也没有「图表围栏 + 图名关键词」的组合 ——
    只有文字描述（报告 F-4 的「施工总平面布置图只有文字描述」）不算覆盖。
    """
    scheme = _resolve_scheme(sections, project_name)
    if not scheme:
        return []
    required = _REQUIRED_CHARTS.get(scheme)
    if not required:
        return []
    all_text = "\n".join(_sec_content(s) for s in sections)
    payload_fields: set[str] = set()
    for s in sections:
        for field in _CHART_JSON_FIELDS:
            value = getattr(s, field, None)
            if isinstance(value, str) and value.strip() not in ("", "{}", "[]", "null"):
                payload_fields.add(field)
    has_fence = bool(_CHART_FENCE_RE.search(all_text))

    issues: list[dict] = []
    for chart_name, fields, keywords in required:
        if any(f in payload_fields for f in fields):
            continue
        if has_fence and any(kw in all_text for kw in keywords):
            continue
        issues.append(
            make_issue(
                "DLV-04",
                rules.SEVERITY_HIGH,
                "关键图缺失",
                f"{scheme}方案缺少关键图：{chart_name}（仅有文字描述不算覆盖）",
                "补充该图的图表数据，或在对应章节生成并插入该图",
            )
        )
    return issues


# ===========================================================================
# 编排入口
# ===========================================================================

#: 进入 warnings 聚合时排除的规则 —— DLV-01 与 check_document 既有的
#: 「内容为空 / 少于50字」告警等价，重复进 warnings 会刷屏；结构化
#: quality_issues 中仍保留（供前端渲染问题清单）。
WARN_EXCLUDED_RULE_IDS = frozenset({"DLV-01"})

#: quality_issues 总条数上限（防止极端文档把响应体撑爆）
_QUALITY_ISSUE_CAP = 200

#: 规则执行顺序：高价值规则在前，截断时优先保留
_RULE_PIPELINE = (
    validate_scheme_sections,
    detect_title_content_mismatch,
    validate_schedule_consistency,
    validate_standard_references,
    validate_limit_units,
    detect_duplicate_numbering,
    detect_duplicate_titles,
    detect_content_numbering_style,
    detect_figure_without_payload,
    detect_duplicate_figure_titles,
    detect_content_overlap,
    detect_empty_sections,
    detect_truncated_sections,
    detect_missing_required_tables,
    detect_missing_key_charts,
    detect_filler_content,
    detect_terminology_issues,
)


def run_quality_checks(sections: list, project_name: str = "") -> list[dict]:
    """执行全部内容质量规则（报告修复 15~24），返回结构化 Issue 列表。

    - 任何单条规则异常都不影响其余规则（隔离降级 + warning 日志）；
    - 结果按严重度排序（high → medium → low），并截断到
      :data:`_QUALITY_ISSUE_CAP`。
    """
    if not sections:
        return []
    logger = logging.getLogger(__name__)
    issues: list[dict] = []
    for func in _RULE_PIPELINE:
        try:
            if "project_name" in inspect.signature(func).parameters:
                issues.extend(func(sections, project_name=project_name))
            else:
                issues.extend(func(sections))
        except Exception as exc:  # 规则相互隔离，绝不让预检整体失败
            logger.warning("质量规则 %s 执行失败（已跳过）: %s", func.__name__, exc)
    issues.sort(key=lambda it: rules.severity_rank(it.get("severity")))
    return issues[:_QUALITY_ISSUE_CAP]


def summarize_quality_issues(issues: list[dict]) -> list[str]:
    """结构化 Issue → 聚合告警文案（每类规则一条，附最多 2 个示例）。

    供 `check_document` 拼入 `warnings`；`WARN_EXCLUDED_RULE_IDS` 中的规则
    不再重复进告警（已有等价预检告警承载）。
    """
    grouped: dict[str, list[dict]] = {}
    for issue in issues:
        rule_id = str(issue.get("rule_id") or "")
        if not rule_id or rule_id in WARN_EXCLUDED_RULE_IDS:
            continue
        grouped.setdefault(rule_id, []).append(issue)

    summaries: list[str] = []
    # 高严重度规则的告警排前面
    ordered = sorted(
        grouped.items(),
        key=lambda kv: min(rules.severity_rank(i.get("severity")) for i in kv[1]),
    )
    for rule_id, group in ordered:
        title = group[0].get("title") or rule_id
        examples = "；".join(
            str(i.get("message") or "") for i in group[:2] if i.get("message")
        )
        suffix = f"，例如：{examples}" if examples else ""
        more = f"（共 {len(group)} 处）" if len(group) > 1 else ""
        summaries.append(f"[内容质量] {title}（{rule_id}）{more}{suffix}")
    return summaries







# 导出给 `check_document` / 单元测试调用的公共接口
__all__ = [
    "run_quality_checks",
    "summarize_quality_issues",
    "validate_scheme_sections",
    "validate_schedule_consistency",
    "validate_standard_references",
    "validate_limit_units",
    "detect_filler_content",
    "detect_terminology_issues",
    "detect_content_overlap",
    "detect_empty_sections",
    "detect_duplicate_numbering",
    "detect_title_content_mismatch",
    "detect_duplicate_titles",
    "detect_truncated_sections",
    "detect_content_numbering_style",
    "detect_figure_without_payload",
    "detect_duplicate_figure_titles",
    "detect_missing_required_tables",
    "detect_missing_key_charts",
]
