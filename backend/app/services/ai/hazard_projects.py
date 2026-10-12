"""hazard_projects.py — 建筑工程六大类危大工程识别与专项章节注入。

ENHANCE 2026-10-12（E-2）：本软件独有竞争力，参考软件未见对应。

依据《危险性较大的分部分项工程安全管理规定》（住建部令第 37 号）：
深基坑、高大模板、脚手架、起重吊装、临时用电、消防保卫 六类属于
必须编制专项施工方案的危大工程。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set


@dataclass
class HazardProject:
    """危大工程类别定义。"""
    code: str
    name: str
    keywords: List[str]
    negative_keywords: List[str]
    required_sections: List[str]
    required_tables: List[str]
    required_charts: List[str]
    safety_threshold: Dict[str, Any] = field(default_factory=dict)
    reference_standards: List[str] = field(default_factory=list)


HAZARD_PROJECTS: List[HazardProject] = [
    HazardProject(
        code="deep_excavation",
        name="深基坑工程",
        keywords=[
            "深基坑", "基坑支护", "地下开挖", "明挖基坑", "地下连续墙",
            "排桩", "支护桩", "灌注桩", "锚杆", "预应力锚索", "内支撑",
            "钢支撑", "混凝土支撑", "坑底隆起", "基坑变形",
        ],
        negative_keywords=["开挖深度不超过 3 米", "浅基坑", "小型基坑"],
        required_sections=[
            "工程概况", "基坑工程风险分析", "基坑支护方案",
            "基坑开挖方案", "基坑变形监测", "降水与排水方案",
            "应急预案", "安全技术措施",
        ],
        required_tables=[
            "基坑支护设计参数表", "基坑监测点位与频率表", "基坑分级与警戒值表",
        ],
        required_charts=[
            "基坑支护剖面图", "基坑平面布置图", "基坑变形监测示意图",
        ],
        safety_threshold={"depth_m": 3, "critical_depth_m": 5},
        reference_standards=[
            "JGJ 120-2012《建筑基坑支护技术规程》",
            "GB 50497-2019《建筑基坑工程监测技术标准》",
        ],
    ),
    HazardProject(
        code="high_formwork",
        name="高大模板工程",
        keywords=[
            "高大模板", "高支模", "满堂支撑架", "扣件式钢管脚手架支撑",
            "模板支撑体系", "模板支撑架", "立杆间距", "剪刀撑",
            "模板承载力", "支撑高度",
        ],
        negative_keywords=["支撑高度不超过 5 米"],
        required_sections=[
            "工程概况", "模板支撑体系设计", "荷载计算与验算",
            "支撑架搭设方案", "模板安装与拆除方案",
            "模板工程质量控制", "支撑架专项监测", "应急预案",
        ],
        required_tables=[
            "模板荷载参数表", "支撑架立杆间距表", "材料规格与数量表",
        ],
        required_charts=[
            "模板支撑体系平面图", "模板支撑体系立面图", "剪刀撑布置图",
        ],
        safety_threshold={"height_m": 5, "load_kn_m2": 10},
        reference_standards=[
            "JGJ 162-2008《建筑施工模板安全技术规范》",
            "JGJ 130-2011《建筑施工扣件式钢管脚手架安全技术规范》",
        ],
    ),
    HazardProject(
        code="scaffolding",
        name="脚手架工程",
        keywords=[
            "脚手架", "扣件式脚手架", "盘扣式脚手架", "承插型盘扣",
            "附着式升降脚手架", "悬挑脚手架", "移动式脚手架",
            "外脚手架", "内脚手架", "扫地杆", "连墙件",
        ],
        negative_keywords=["高度不超过 2 米"],
        required_sections=[
            "工程概况", "脚手架类型选择", "脚手架搭设方案",
            "扫地杆与连墙件设计", "脚手板与防护栏杆",
            "脚手架验收与使用", "脚手架拆除方案", "安全防护措施",
        ],
        required_tables=[
            "脚手架规格参数表", "连墙件间距表", "脚手材料清单",
        ],
        required_charts=[
            "脚手架立面布置图", "脚手架节点详图", "连墙件布置平面图",
        ],
        safety_threshold={"height_m": 24},
        reference_standards=[
            "JGJ 130-2011《建筑施工扣件式钢管脚手架安全技术规范》",
            "JGJ 166-2016《建筑施工承插型盘扣式钢管支架安全技术标准》",
        ],
    ),
    HazardProject(
        code="lifting",
        name="起重吊装工程",
        keywords=[
            "起重吊装", "塔吊", "塔式起重机", "施工升降机", "汽车吊",
            "履带式起重机", "吊装作业", "吊装方案", "起重量", "吊钩",
            "平衡梁", "吊耳", "钢丝绳",
        ],
        negative_keywords=["重量不超过 1 吨"],
        required_sections=[
            "工程概况", "吊装作业风险分析", "起重机械选型与布置",
            "吊装作业流程", "吊具与索具配置",
            "吊装作业安全控制", "应急预案",
        ],
        required_tables=[
            "起重机械规格表", "吊装作业危险源清单", "吊具与索具检查表",
        ],
        required_charts=[
            "起重机械平面布置图", "吊装作业流程图", "吊装作业立面示意图",
        ],
        safety_threshold={"weight_t": 1},
        reference_standards=[
            "JGJ 196-2010《建筑施工塔式起重机安装、使用、拆卸安全技术规程》",
            "JGJ 88-2010《建筑施工高处作业安全技术规范》",
        ],
    ),
    HazardProject(
        code="temporary_electricity",
        name="施工临时用电工程",
        keywords=[
            "临时用电", "施工现场用电", "三级配电", "两级保护",
            "TN-S 系统", "漏电保护", "重复接地", "配电箱",
            "电缆敷设", "照明系统",
        ],
        negative_keywords=[],
        required_sections=[
            "工程概况", "用电负荷计算", "配电系统方案",
            "三级配电两级保护", "电缆敷设与保护措施",
            "照明系统方案", "用电安全控制", "应急照明与疏散",
        ],
        required_tables=[
            "用电负荷计算表", "配电箱配置表", "电缆规格选择表",
        ],
        required_charts=[
            "临时用电系统图", "配电箱布置平面图", "电缆敷设路线图",
        ],
        safety_threshold={},
        reference_standards=[
            "JGJ 46-2005《施工现场临时用电安全技术规范》",
        ],
    ),
    HazardProject(
        code="fire_safety",
        name="消防安全与保卫",
        keywords=[
            "消防", "消防保卫", "防火", "消防设施", "防火分区",
            "灭火器", "消火栓", "临时消防", "焊接动火", "易燃易爆",
            "文明施工", "安全防护", "个人防护", "安全帽", "安全带",
        ],
        negative_keywords=[],
        required_sections=[
            "工程概况", "施工现场消防安全分析", "临时消防设施配置",
            "防火分区与疏散", "动火作业管理",
            "易燃易爆物品管理", "消防安全应急预案", "消防演练与培训",
        ],
        required_tables=[
            "消防设施配置表", "动火作业审批表", "消防巡查记录表",
        ],
        required_charts=[
            "消防平面图", "疏散路线图", "消防设施布置图",
        ],
        safety_threshold={},
        reference_standards=[
            "GB 50720-2011《建设工程施工现场消防安全技术规范》",
            "JGJ 59-2011《建筑施工安全检查标准》",
        ],
    ),
]


_HAZARD_INDEX: Dict[str, HazardProject] = {hp.code: hp for hp in HAZARD_PROJECTS}

_KEYWORD_INDEX: Dict[str, str] = {}
for _hp in HAZARD_PROJECTS:
    for _kw in _hp.keywords:
        _KEYWORD_INDEX.setdefault(_kw.lower(), _hp.code)


@dataclass
class HazardScanResult:
    """危大工程扫描结果。"""
    detected: List[HazardProject]
    scores: Dict[str, int]
    suggested_sections: List[str]


def scan_hazard_projects(text: Optional[str]) -> HazardScanResult:
    """扫描招标文件文本，识别危大工程类型。

    Args:
        text: 招标文件全文。

    Returns:
        HazardScanResult 包含检测到的危大工程列表、命中分数、建议章节。
    """
    if not text or not isinstance(text, str):
        return HazardScanResult([], {}, [])

    text_lower = text.lower()
    scores: Dict[str, int] = {}
    for hp in HAZARD_PROJECTS:
        score = 0
        for kw in hp.keywords:
            if kw.lower() in text_lower:
                score += 1
        for neg in hp.negative_keywords:
            if neg in text:
                score -= 2
        if score > 0:
            scores[hp.code] = score

    detected: List[HazardProject] = []
    for code, _ in sorted(scores.items(), key=lambda x: -x[1]):
        hp = _HAZARD_INDEX.get(code)
        if hp:
            detected.append(hp)

    seen: Set[str] = set()
    suggested: List[str] = []
    for hp in detected:
        for sec in hp.required_sections:
            if sec not in seen:
                seen.add(sec)
                suggested.append(sec)

    return HazardScanResult(
        detected=detected,
        scores=scores,
        suggested_sections=suggested,
    )


def build_hazard_outline_sections(
    detected: List[HazardProject],
) -> List[Dict[str, Any]]:
    """为每个危大工程生成专项章节节点。"""
    result: List[Dict[str, Any]] = []
    for i, hp in enumerate(detected, 1):
        standards_preview = ", ".join(hp.reference_standards[:2])
        result.append({
            "id": str(i),
            "title": f"{hp.name}专项施工方案",
            "description": f"{hp.name}的专项施工方案（依据 {standards_preview}）",
            "hazard_code": hp.code,
            "children": [
                {
                    "id": f"{i}.{j+1}",
                    "title": sec,
                    "description": sec,
                }
                for j, sec in enumerate(hp.required_sections)
            ],
        })
    return result


def get_hazard_by_code(code: str) -> Optional[HazardProject]:
    """按 code 获取危大工程定义。"""
    return _HAZARD_INDEX.get(code)


def list_hazard_projects() -> List[Dict[str, Any]]:
    """返回所有危大工程的元数据（用于 API 和前端展示）。"""
    return [
        {
            "code": hp.code,
            "name": hp.name,
            "keyword_count": len(hp.keywords),
            "required_sections_count": len(hp.required_sections),
            "reference_standards": hp.reference_standards,
        }
        for hp in HAZARD_PROJECTS
    ]

