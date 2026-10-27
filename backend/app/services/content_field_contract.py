"""正文生成字段契约与全局数据字典（Content Field Contract）

本模块是「正文生成模块增强」的核心数据层，解决正文生成过程中
**提取项目数据 / 全局事实数据未被有效、完整、一致调用** 的问题：

  1. 提取项目字段清单（13 项，含类型 / 必填 / 默认值 / 来源 / 九大章节归属）
  2. 全局事实字段清单（按九大章节分类，含类型 / 必填 / 默认值 / 来源 / 置信度）
  3. 章节 → 所需字段 映射表（九大章节各自需要哪些字段）
  4. 全局数据字典（关键一致性字段的唯一权威值，正文必须引用不得自行生成）
  5. 按需 / 分层 / 完整 上下文注入
  6. 生成后自检（数据调用 / 上下文 / 前后一致性 / 质量）

设计原则（与既有正文生成链路向后兼容）：
  * 全部为「可选增强」——调用方不传 `field_contract` / `global_dictionary` 时，
    提示词构建行为与旧实现完全一致（不引入任何新依赖、不影响缓存前缀）。
  * 全局数据字典从「全局事实 (facts)」与「提取项目字段」派生，二者同源，
    保证同一字段在全文章节读取同一权威值（前后一致性核心机制）。
  * 所有结论（字段是否调用、是否截断、是否错乱）均可被测试与日志复现。

调用入口：
  * 正文生成 SSE（`routers/sse/sse_content.py`）在取数后调用
    `build_field_contract(project_data, facts, policy)` 一次性构建契约。
  * `build_chapter_content_messages` 从 `kwargs["field_contract"]` 读取并注入。
  * `generate_sections` 在结果收集后调用 `run_content_self_checks` 自检。
"""

from __future__ import annotations

import json
import re
from typing import Any

logger = __import__("logging").getLogger(__name__)


def normalize_policy(value: Any) -> str:
    """缺失值策略归一化（复用 facts_service 三档口径，异常兜底 fabricate）。"""
    try:
        from .facts_service import normalize_facts_policy
        return normalize_facts_policy(value)
    except Exception:  # pragma: no cover - facts_service 不可用时兜底
        v = str(value or "").strip().lower()
        return v if v in ("fabricate", "omit", "placeholder") else "fabricate"


# ============================================================
# 一、九大章节主题（用于章节 → 字段映射与事实分组）
# ============================================================
#
# 技术标一级目录在不同行业模板下命名略有差异，这里用「主题 + 关键词」描述，
# 使映射对具体章节编号/命名具有鲁棒性。顺序即九大章节标准顺序。

NINE_CHAPTERS: list[dict] = [
    {
        "idx": 1,
        "theme": "工程概况与编制依据",
        "keywords": ["工程概况", "编制依据", "项目概况", "编制说明", "现状分析", "施工条件"],
    },
    {
        "idx": 2,
        "theme": "施工组织部署与机构",
        "keywords": ["施工组织部署", "施工部署", "组织机构", "项目班子", "管理体系", "施工总体部署", "任务划分"],
    },
    {
        "idx": 3,
        "theme": "主要分部分项工程施工方案",
        "keywords": ["主要分部", "分部分项", "施工方案", "施工方法", "施工技术", "土建", "主体结构", "装饰装修"],
    },
    {
        "idx": 4,
        "theme": "专项施工方案",
        "keywords": ["专项", "危大", "高支模", "深基坑", "脚手架", "起重吊装", "幕墙", "pc", "装配式", "临时用电"],
    },
    {
        "idx": 5,
        "theme": "施工进度计划及保证措施",
        "keywords": ["进度计划", "工期", "总进度", "施工流水", "里程碑", "进度保障", "工期保证"],
    },
    {
        "idx": 6,
        "theme": "资源配置计划",
        "keywords": ["资源配置", "劳动力", "机械", "设备", "材料", "人员配备", "物资", "投入计划"],
    },
    {
        "idx": 7,
        "theme": "质量管理体系与措施",
        "keywords": ["质量", "质量管理体系", "创优", "验收标准", "质量控制"],
    },
    {
        "idx": 8,
        "theme": "安全管理与文明施工",
        "keywords": ["安全", "文明施工", "环保", "职业健康", "防护", "消防", "绿色施工"],
    },
    {
        "idx": 9,
        "theme": "应急预案与风险控制",
        "keywords": ["应急", "预案", "风险", "突发", "事故", "防汛", "防台", "救援"],
    },
]


# ============================================================
# 一.1 章节词表扩充（随行业模板）
# ============================================================
#
# ENHANCE 2026-10-07 第二轮（遗留问题 5 落地）：
#   * BUILTIN_CHAPTER_KEYWORD_ALIASES —— 跨行业模板的同义词变体（内置，无需配置）
#   * INDUSTRY_CHAPTER_KEYWORDS       —— 行业专项词表，键对齐
#     industry_profiles.INDUSTRY_PROFILES 的行业名（未列出的行业用基础词表）
#   * register_chapter_keywords()     —— 运行时注册扩展词表
#     （供行业模板包 / 前端配置 / 测试注入，如需持久化可由调用方存 DB 后启动时注册）
#
# 匹配规则与基础词表一致：命中关键词按长度累加计分，总分最高者胜出 ——
# 长词（更具体）天然占优，如「专项施工方案」中 专项(2)+施工方案(4) 会让第 4 章
# 击败仅有 施工方案(4) 的第 3 章。

#: 跨模板同义词变体（各行业招标模板常见一级目录命名）
BUILTIN_CHAPTER_KEYWORD_ALIASES: dict[int, tuple[str, ...]] = {
    1: ("项目综述", "总体概述", "工程简介", "概述", "施工条件分析", "编制原则"),
    2: ("施工组织设计", "施工组织", "项目管理机构", "项目组织", "组织部署", "项目部", "施工筹划", "总体部署"),
    3: ("主要施工方案", "施工工艺", "施工工序", "分项工程", "关键工序", "施工技术方案"),
    4: ("专项方案", "危险性较大", "超危大", "特殊施工", "专业工程方案"),
    5: ("施工进度", "进度安排", "进度管理", "施工计划", "工期保障措施", "进度保证措施"),
    6: ("资源配备", "投入计划", "机械设备", "材料计划", "劳动力计划", "人员配置", "施工装备"),
    7: ("质量保证", "质量保障", "质量创优", "质量目标", "质量管理", "工程质量管理"),
    8: ("安全保证", "安全保障", "安全生产", "安全文明", "文明施工及环保", "环境保护", "绿色施工", "职业健康"),
    9: ("应急响应", "应急救援", "风险管理", "风险防控", "突发事件", "应急保障"),
}

#: 行业专项词表（行业键对齐 industry_profiles.INDUSTRY_PROFILES）
INDUSTRY_CHAPTER_KEYWORDS: dict[str, dict[int, tuple[str, ...]]] = {
    "消防工程": {
        3: ("消火栓", "喷淋", "自动报警", "防排烟", "气体灭火"),
        4: ("消防水池", "泵房", "消防电源"),
        7: ("消防验收", "消防检测"),
    },
    "弱电智能化": {
        3: ("综合布线", "安防监控", "门禁", "楼宇自控", "广播系统", "会议系统"),
        4: ("机房工程", "数据中心"),
        6: ("弱电设备", "智能化设备"),
    },
    "电力施工": {
        3: ("电气安装", "电缆敷设", "变配电"),
        4: ("输电线路", "变压器", "配电网", "带电作业"),
        8: ("停电", "高压"),
    },
    "装饰装修": {
        3: ("吊顶", "饰面", "涂饰", "裱糊", "地面工程", "门窗工程"),
        4: ("幕墙工程", "钢结构玻璃", "石材干挂"),
    },
    "市政/园林": {
        3: ("道路工程", "路基", "路面", "桥梁工程", "管网", "排水工程", "绿化种植"),
        4: ("顶管", "基坑支护", "交通导改", "沟槽开挖"),
        9: ("占路", "地下管线"),
    },
    "设备采购安装": {
        3: ("设备安装", "设备调试", "单机试运转"),
        6: ("设备采购", "开箱检验", "随机资料", "备品备件"),
        7: ("出厂检验", "到货验收"),
    },
    "物业/运维服务": {
        2: ("运维团队", "服务架构", "管理架构"),
        9: ("运维应急", "服务中断", "设施故障"),
        6: ("运维人员", "物料储备"),
    },
    "公路水运试验检测": {
        3: ("试验检测", "检测方案", "取样", "标定"),
        7: ("检测报告", "数据审核", "比对试验"),
    },
    "电梯以旧换新/改造": {
        3: ("电梯安装", "井道", "导轨", "层门"),
        4: ("电梯拆除", "旧梯拆除", "吊装就位"),
        8: ("困人", "坠落"),
    },
    "石油化工": {
        3: ("管道安装", "储罐", "工艺管道", "设备组对"),
        4: ("动火作业", "受限空间", "带压作业", "吹扫", "试压"),
        8: ("防爆", "有毒有害", "泄漏"),
    },
    "广告施工": {
        4: ("广告牌", "楼顶广告", "高炮", "发光字", "高空作业"),
        8: ("坠物", "防风"),
    },
    "通用技术服务": {},
}

#: 运行时注册的扩展词表（register_chapter_keywords 写入）
_REGISTERED_CHAPTER_KEYWORDS: dict[int, tuple[str, ...]] = {}

_VALID_CHAPTER_IDX = set(range(1, 10))


def register_chapter_keywords(chapter_idx: int, keywords: list[str]) -> int:
    """注册章节主题扩展关键词（供行业模板包 / 前端配置 / 测试注入）。

    Args:
        chapter_idx: 九大章节序号（1-9）
        keywords: 追加的关键词列表（空串自动过滤）

    Returns:
        实际注册成功的关键词数；非法章节序号抛 ValueError。
    """
    if chapter_idx not in _VALID_CHAPTER_IDX:
        raise ValueError(f"chapter_idx 必须在 1-9 内，收到 {chapter_idx}")
    clean = tuple(kw for kw in (str(k).strip() for k in (keywords or [])) if kw)
    existing = _REGISTERED_CHAPTER_KEYWORDS.get(chapter_idx, ())
    _REGISTERED_CHAPTER_KEYWORDS[chapter_idx] = existing + clean
    return len(clean)


def clear_registered_chapter_keywords() -> None:
    """清空运行时注册的扩展词表（测试/热更新用）。"""
    _REGISTERED_CHAPTER_KEYWORDS.clear()


def _resolve_industry_key(industry: str | None) -> str | None:
    """把行业名解析到 INDUSTRY_CHAPTER_KEYWORDS 的键（含双向包含与关键词匹配）。

    解析规则对齐 industry_profiles._lookup_industry_profile 的宽松匹配：
    精确命中 → 行业名双向包含 → 行业画像 keywords 命中 → None（用基础词表）。
    """
    ind = (industry or "").strip()
    if not ind:
        return None
    if ind in INDUSTRY_CHAPTER_KEYWORDS:
        return ind
    try:
        from .ai.industry_profiles import INDUSTRY_PROFILES
    except Exception:  # pragma: no cover
        return None
    for name in INDUSTRY_CHAPTER_KEYWORDS:
        if name in ind or ind in name:
            return name
    for _name, profile in INDUSTRY_PROFILES.items():
        for kw in profile.get("keywords", []) or []:
            if kw in ind:
                return name if name in INDUSTRY_CHAPTER_KEYWORDS else None
    return None


def classify_chapter(chapter_title: str, industry: str | None = None) -> dict:
    """把章节标题归入九大章节主题之一（最长关键词匹配优先）。

    词表三层叠加（ENHANCE 2026-10-07 第二轮：随行业模板扩充）：
      1. NINE_CHAPTERS 基础关键词
      2. BUILTIN_CHAPTER_KEYWORD_ALIASES 跨模板同义词（各行业模板常见变体）
      3. INDUSTRY_CHAPTER_KEYWORDS 行业专项词表 + register_chapter_keywords
         动态注册（供行业模板/前端/测试扩展，行业名对齐
         industry_profiles.INDUSTRY_PROFILES 的行业键）

    Returns:
        dict: {"idx": int, "theme": str, "keywords": [...]}（未命中返回第 1 章兜底）
    """
    title = (chapter_title or "").lower()
    best: dict | None = None
    best_score = 0
    industry_key = _resolve_industry_key(industry)
    for ch in NINE_CHAPTERS:
        idx = ch["idx"]
        score = 0
        for kw in ch["keywords"]:
            if kw.lower() in title:
                score += len(kw)
        for kw in BUILTIN_CHAPTER_KEYWORD_ALIASES.get(idx, ()):
            if kw.lower() in title:
                score += len(kw)
        for kw in INDUSTRY_CHAPTER_KEYWORDS.get(industry_key, {}).get(idx, ()):
            if kw.lower() in title:
                score += len(kw)
        for kw in _REGISTERED_CHAPTER_KEYWORDS.get(idx, ()):
            if kw.lower() in title:
                score += len(kw)
        if score > best_score:
            best_score = score
            best = ch
    # ENHANCE 2026-10-21（正文 #2）：附加 `matched` 置信度标志——是否真正命中
    # 关键词（False = 纯兜底，落到默认第 1 章）。调用方（事实校验）据此把
    # 「自定义/行业章节按兜底章节注入事实」从静默变为可观测告警。返回值仍含
    # `idx/theme/keywords`，对既有 `ch["idx"]` 消费者向后兼容。
    if best is None:
        return {**NINE_CHAPTERS[0], "matched": False}
    return {**best, "matched": best_score > 0}


# ============================================================
# 二、提取项目字段清单（13 项）
# ============================================================
#
# 每项字段：
#   key        唯一字段键
#   label      中文标签（用于提示词注入与自检）
#   type       数据类型
#   required   是否必填
#   default    默认值（缺失且无事实来源时使用）
#   source     数据来源（DB 列 / 全局事实 category）
#   chapters   九大章节归属（1-9；"*" 表示全局通用）
#   fact_category  从全局事实派生时的 category（可选）
#   fact_keywords  从全局事实派生时匹配 label/value 的关键词

EXTRACTION_PROJECT_FIELDS: list[dict] = [
    {
        "key": "project_name", "label": "项目名称", "type": "str", "required": True,
        "default": "", "source": "facts(basic/project)", "chapters": "*",
        "fact_category": "basic", "fact_keywords": ["项目名称", "工程名称", "标段名称"],
    },
    {
        "key": "project_no", "label": "项目编号", "type": "str", "required": False,
        "default": "", "source": "facts(basic)", "chapters": "*",
        "fact_category": "basic", "fact_keywords": ["项目编号", "招标编号", "标段编号", "招标项目编号"],
    },
    {
        "key": "owner", "label": "业主/建设单位", "type": "str", "required": True,
        "default": "", "source": "facts(project/bidder)", "chapters": [1, 2, 3, 4, 7, 8, 9],
        "fact_category": "project", "fact_keywords": ["建设单位", "业主", "发包人", "招标人", "甲方"],
    },
    {
        "key": "location", "label": "项目地点", "type": "str", "required": False,
        "default": "", "source": "facts(project)", "chapters": [1, 3, 4],
        "fact_category": "project", "fact_keywords": ["项目地点", "建设地点", "工程地点", "坐落", "位于"],
    },
    {
        "key": "scale", "label": "建筑规模", "type": "str", "required": False,
        "default": "", "source": "facts(project)", "chapters": [1, 3, 4],
        "fact_category": "project", "fact_keywords": ["建筑面积", "建筑规模", "建设规模", "占地面积", "规模"],
    },
    {
        "key": "structure_type", "label": "结构形式", "type": "str", "required": False,
        "default": "", "source": "facts(project/technical)", "chapters": [1, 3, 4],
        "fact_category": "project", "fact_keywords": ["结构形式", "结构类型", "框架结构", "剪力墙", "钢结构"],
    },
    {
        "key": "duration", "label": "工期", "type": "str", "required": True,
        "default": "", "source": "facts(schedule)", "chapters": [1, 2, 5, 6, 9],
        "fact_category": "schedule", "fact_keywords": ["工期", "日历天", "总工期", "施工工期", "竣工日期", "开工"],
    },
    {
        "key": "budget", "label": "预算/合同价", "type": "str", "required": False,
        "default": "", "source": "facts(project)", "chapters": [1, 6],
        "fact_category": "project", "fact_keywords": ["预算", "合同价", "总投资", "投标报价", "建安造价", "概算"],
    },
    {
        "key": "special_params", "label": "专项工程特征参数", "type": "str", "required": False,
        "default": "", "source": "facts(technical/implementation)", "chapters": [3, 4],
        "fact_category": "technical", "fact_keywords": ["特征", "专项参数", "关键技术参数", "工程特征"],
    },
    {
        "key": "bid_requirements", "label": "招标要求", "type": "str", "required": False,
        "default": "", "source": "facts(commitment)", "chapters": [1, 7, 8],
        "fact_category": "commitment", "fact_keywords": ["招标要求", "投标人须", "须满足", "要求", "承诺"],
    },
    {
        "key": "tech_params", "label": "技术参数", "type": "str", "required": False,
        "default": "", "source": "facts(technical)", "chapters": [3, 4, 6],
        "fact_category": "technical", "fact_keywords": ["技术参数", "技术指标", "性能指标", "规格参数"],
    },
    {
        "key": "qualification", "label": "资质条件", "type": "str", "required": False,
        "default": "", "source": "facts(bidder/commitment)", "chapters": [1, 2, 7, 8],
        "fact_category": "bidder", "fact_keywords": ["资质", "资格", "等级", "许可证", "认证"],
    },
    {
        "key": "participants", "label": "参建单位", "type": "str", "required": False,
        "default": "", "source": "facts(project/bidder)", "chapters": [1, 2, 7, 8],
        "fact_category": "project", "fact_keywords": ["参建", "勘察", "设计", "监理", "施工总承包", "分包"],
    },
]

_EXTRACTION_BY_KEY: dict[str, dict] = {f["key"]: f for f in EXTRACTION_PROJECT_FIELDS}

#: 仅存在于全局事实（不在提取项目字段清单）中的字段 → 中文标签映射。
#: 用于「章节 → 所需字段」映射时从 facts 按标签抽取。
_FACT_ONLY_LABELS: dict[str, str] = {
    "personnel_count": "人员数量",
    "equipment_model": "设备型号",
    "schedule_detail": "进度节点",
    "milestone": "里程碑",
    "quality_target": "质量目标",
    "safety_target": "安全目标",
    "response_time": "响应时间",
    "warranty": "质保期",
}

#: 仅事实字段的抽取关键词（同义词宽覆盖）。
#: ENHANCE 2026-10-18（第二轮）：显式映射按「章节 → 所需事实」取事实时，
#: 若只用标签窄匹配，「项目经理 / 技术负责人 / 劳动力计划」这类人员事实
#: 会因标签不含「人员数量」四字而漏调（实证：E2E 八模块链中已确认的
#: 项目经理事实进不了正文提示词）。标签本身仍是首个关键词，窄匹配不回归。
#: 匹配顺序为字段名级优先、值级兜底（见 _match_fact），宽词只提升召回。
FACT_ONLY_KEYWORDS: dict[str, list[str]] = {
    "personnel_count": [
        "人员数量", "人员", "劳动力", "管理人员", "作业人员",
        "项目经理", "技术负责人", "项目班子", "人员配置", "人员配备",
    ],
    "equipment_model": [
        "设备型号", "设备", "机械", "塔吊", "起重机械", "施工机械", "设备配置",
    ],
    "schedule_detail": [
        "进度节点", "进度安排", "进度计划", "节点工期", "关键线路",
    ],
    "milestone": [
        "里程碑", "节点目标", "关键节点", "重要节点",
    ],
    "quality_target": [
        "质量目标", "质量标准", "验收标准", "质量等级",
    ],
    "safety_target": [
        "安全目标", "安全等级", "安全标准",
    ],
    "response_time": [
        "响应时间", "响应时限", "到场时间", "服务响应",
    ],
    "warranty": [
        "质保期", "保修期", "质保", "保修",
    ],
}


# ============================================================
# 三、全局事实字段清单（按九大章节分类）
# ============================================================
#
# 与 facts_service.FACT_CATEGORIES 对齐；这里按九大章节给出「每章可调用的事实字段」。
# 每项：
#   key / label / type / required / default / source(category) / confidence(置信度字段)

GLOBAL_FACTS_FIELD_GROUPS: dict[int, list[dict]] = {
    1: [  # 工程概况与编制依据
        {"key": "project_name", "label": "项目名称", "type": "str", "required": True, "default": "", "source": "basic", "confidence": "high"},
        {"key": "project_no", "label": "项目编号", "type": "str", "required": False, "default": "", "source": "basic", "confidence": "high"},
        {"key": "owner", "label": "业主/建设单位", "type": "str", "required": True, "default": "", "source": "project", "confidence": "high"},
        {"key": "location", "label": "项目地点", "type": "str", "required": False, "default": "", "source": "project", "confidence": "mid"},
        {"key": "scale", "label": "建筑规模", "type": "str", "required": False, "default": "", "source": "project", "confidence": "mid"},
        {"key": "structure_type", "label": "结构形式", "type": "str", "required": False, "default": "", "source": "project", "confidence": "mid"},
        {"key": "duration", "label": "工期", "type": "str", "required": True, "default": "", "source": "schedule", "confidence": "high"},
        {"key": "bid_requirements", "label": "招标要求", "type": "str", "required": False, "default": "", "source": "commitment", "confidence": "mid"},
        {"key": "qualification", "label": "资质条件", "type": "str", "required": False, "default": "", "source": "bidder", "confidence": "mid"},
    ],
    2: [  # 施工组织部署与机构
        {"key": "owner", "label": "业主/建设单位", "type": "str", "required": True, "default": "", "source": "project", "confidence": "high"},
        {"key": "duration", "label": "工期", "type": "str", "required": True, "default": "", "source": "schedule", "confidence": "high"},
        {"key": "personnel_count", "label": "人员数量", "type": "str", "required": False, "default": "", "source": "personnel", "confidence": "mid"},
        {"key": "participants", "label": "参建单位", "type": "str", "required": False, "default": "", "source": "project", "confidence": "mid"},
        {"key": "qualification", "label": "资质条件", "type": "str", "required": False, "default": "", "source": "bidder", "confidence": "mid"},
    ],
    3: [  # 主要分部分项工程施工方案
        {"key": "project_name", "label": "项目名称", "type": "str", "required": True, "default": "", "source": "basic", "confidence": "high"},
        {"key": "owner", "label": "业主/建设单位", "type": "str", "required": True, "default": "", "source": "project", "confidence": "high"},
        {"key": "location", "label": "项目地点", "type": "str", "required": False, "default": "", "source": "project", "confidence": "mid"},
        {"key": "scale", "label": "建筑规模", "type": "str", "required": False, "default": "", "source": "project", "confidence": "mid"},
        {"key": "structure_type", "label": "结构形式", "type": "str", "required": False, "default": "", "source": "project", "confidence": "mid"},
        {"key": "special_params", "label": "专项工程特征参数", "type": "str", "required": False, "default": "", "source": "technical", "confidence": "mid"},
        {"key": "tech_params", "label": "技术参数", "type": "str", "required": False, "default": "", "source": "technical", "confidence": "mid"},
    ],
    4: [  # 专项施工方案
        {"key": "owner", "label": "业主/建设单位", "type": "str", "required": True, "default": "", "source": "project", "confidence": "high"},
        {"key": "location", "label": "项目地点", "type": "str", "required": False, "default": "", "source": "project", "confidence": "mid"},
        {"key": "scale", "label": "建筑规模", "type": "str", "required": False, "default": "", "source": "project", "confidence": "mid"},
        {"key": "structure_type", "label": "结构形式", "type": "str", "required": False, "default": "", "source": "project", "confidence": "mid"},
        {"key": "special_params", "label": "专项工程特征参数", "type": "str", "required": False, "default": "", "source": "technical", "confidence": "mid"},
        {"key": "tech_params", "label": "技术参数", "type": "str", "required": False, "default": "", "source": "technical", "confidence": "mid"},
    ],
    5: [  # 施工进度计划及保证措施
        {"key": "duration", "label": "工期", "type": "str", "required": True, "default": "", "source": "schedule", "confidence": "high"},
        {"key": "schedule_detail", "label": "进度节点", "type": "str", "required": False, "default": "", "source": "schedule", "confidence": "mid"},
        {"key": "milestone", "label": "里程碑", "type": "str", "required": False, "default": "", "source": "schedule", "confidence": "low"},
    ],
    6: [  # 资源配置计划
        {"key": "personnel_count", "label": "人员数量", "type": "str", "required": True, "default": "", "source": "personnel", "confidence": "mid"},
        {"key": "equipment_model", "label": "设备型号", "type": "str", "required": False, "default": "", "source": "technical", "confidence": "mid"},
        {"key": "duration", "label": "工期", "type": "str", "required": True, "default": "", "source": "schedule", "confidence": "high"},
        {"key": "budget", "label": "预算/合同价", "type": "str", "required": False, "default": "", "source": "project", "confidence": "mid"},
        {"key": "tech_params", "label": "技术参数", "type": "str", "required": False, "default": "", "source": "technical", "confidence": "mid"},
    ],
    7: [  # 质量管理体系与措施
        {"key": "quality_target", "label": "质量目标", "type": "str", "required": False, "default": "", "source": "quality", "confidence": "mid"},
        {"key": "warranty", "label": "质保期", "type": "str", "required": False, "default": "", "source": "quality", "confidence": "mid"},
        {"key": "owner", "label": "业主/建设单位", "type": "str", "required": True, "default": "", "source": "project", "confidence": "high"},
        {"key": "bid_requirements", "label": "招标要求", "type": "str", "required": False, "default": "", "source": "commitment", "confidence": "mid"},
        {"key": "qualification", "label": "资质条件", "type": "str", "required": False, "default": "", "source": "bidder", "confidence": "mid"},
    ],
    8: [  # 安全管理与文明施工
        {"key": "safety_target", "label": "安全目标", "type": "str", "required": False, "default": "", "source": "safety", "confidence": "mid"},
        {"key": "owner", "label": "业主/建设单位", "type": "str", "required": True, "default": "", "source": "project", "confidence": "high"},
        {"key": "bid_requirements", "label": "招标要求", "type": "str", "required": False, "default": "", "source": "commitment", "confidence": "mid"},
        {"key": "qualification", "label": "资质条件", "type": "str", "required": False, "default": "", "source": "bidder", "confidence": "mid"},
        {"key": "participants", "label": "参建单位", "type": "str", "required": False, "default": "", "source": "project", "confidence": "mid"},
    ],
    9: [  # 应急预案与风险控制
        {"key": "duration", "label": "工期", "type": "str", "required": True, "default": "", "source": "schedule", "confidence": "high"},
        {"key": "owner", "label": "业主/建设单位", "type": "str", "required": True, "default": "", "source": "project", "confidence": "high"},
        {"key": "response_time", "label": "响应时间", "type": "str", "required": False, "default": "", "source": "safety", "confidence": "low"},
        {"key": "participants", "label": "参建单位", "type": "str", "required": False, "default": "", "source": "project", "confidence": "mid"},
    ],
}


# ============================================================
# 四、章节 → 所需字段 映射表（按需注入依据）
# ============================================================
#
# 每个九大章节需要注入的「提取项目字段 + 全局事实字段」。
# 生成时按本表完整注入，不遗漏、不截断、不错映射。

CHAPTER_FIELD_MAP: dict[int, dict] = {
    1: {"extraction": ["project_name", "project_no", "owner", "location", "scale", "structure_type", "duration", "budget", "bid_requirements", "qualification", "participants"],
        "facts": ["project_name", "project_no", "owner", "location", "scale", "structure_type", "duration", "bid_requirements", "qualification"]},
    2: {"extraction": ["owner", "duration", "participants", "qualification"],
        "facts": ["owner", "duration", "personnel_count", "participants", "qualification"]},
    3: {"extraction": ["project_name", "owner", "location", "scale", "structure_type", "special_params", "tech_params"],
        "facts": ["project_name", "owner", "location", "scale", "structure_type", "special_params", "tech_params"]},
    4: {"extraction": ["owner", "location", "scale", "structure_type", "special_params", "tech_params"],
        "facts": ["owner", "location", "scale", "structure_type", "special_params", "tech_params"]},
    5: {"extraction": ["duration"],
        "facts": ["duration", "schedule_detail", "milestone"]},
    6: {"extraction": ["duration", "budget", "tech_params"],
        "facts": ["personnel_count", "equipment_model", "duration", "budget", "tech_params"]},
    7: {"extraction": ["owner", "bid_requirements", "qualification"],
        "facts": ["quality_target", "warranty", "owner", "bid_requirements", "qualification"]},
    8: {"extraction": ["owner", "bid_requirements", "qualification", "participants"],
        "facts": ["safety_target", "owner", "bid_requirements", "qualification", "participants"]},
    9: {"extraction": ["duration", "owner", "participants"],
        "facts": ["duration", "owner", "response_time", "participants"]},
}


# ============================================================
# 五、关键一致性字段（全局数据字典核心，前后章节必须一致）
# ============================================================

KEY_CONSISTENCY_FIELDS: list[str] = [
    "project_name",   # 项目名称
    "project_no",     # 项目编号
    "owner",          # 业主/建设单位
    "duration",       # 工期
    "response_time",  # 响应时间
    "personnel_count",  # 人员数量
    "equipment_model",  # 设备型号
    "warranty",       # 质保期
    "participants",   # 参建单位
    "quality_target", # 质量目标
    "safety_target",  # 安全目标
]


# ============================================================
# 六、工具：从 facts 列表派生字段值
# ============================================================

def _norm_text(v: Any) -> str:
    return ("" if v is None else str(v)).strip()


def _field_label(key: str) -> str:
    """字段的中文标签（提取项目字段 → 仅事实字段 → 原键兜底）。"""
    meta = _EXTRACTION_BY_KEY.get(key)
    if meta:
        return meta["label"]
    return _FACT_ONLY_LABELS.get(key, key)


def _fact_label_match(fact: dict, keywords: list[str]) -> bool:
    """字段名级匹配（label/key/name），避免 value 噪声导致错映射。"""
    hay = " ".join([
        _norm_text(fact.get("label")),
        _norm_text(fact.get("key")),
        _norm_text(fact.get("name")),
    ]).lower()
    return any(kw.lower() in hay for kw in (keywords or []))


def _fact_value_match(fact: dict, keywords: list[str]) -> bool:
    """值级匹配（宽松兜底，仅在字段名级未命中时使用）。"""
    return _norm_text(fact.get("value")).lower() and any(
        kw.lower() in _norm_text(fact.get("value")).lower() for kw in (keywords or [])
    )


def _fact_matches(fact: dict, keywords: list[str]) -> bool:
    return _fact_label_match(fact, keywords) or _fact_value_match(fact, keywords)


def _match_fact(facts: list, field: dict) -> tuple[str, dict | None]:
    """从全局事实列表为某字段抽取权威值，并带回溯源信息。

    四级匹配（与历史 `_extract_from_facts` 行为等价）：
      1) 同 category + 字段名级（label/key）命中 —— 最准
      2) 任意 category + 字段名级命中
      3) 同 category + 值级宽松兜底
      4) 任意 category + 值级宽松兜底

    Returns:
        (value, provenance)；provenance = {"fact_key", "fact_label",
        "matched_by": "label"|"value"}，未命中返回 ("", None)。
    """
    if not facts:
        return "", None
    kws = field.get("fact_keywords") or []
    cat = field.get("fact_category")

    def _first_pass(c: Any, label_only: bool) -> tuple[str, dict | None]:
        for f in facts:
            if not isinstance(f, dict):
                continue
            if c is not None and f.get("category") != c and f.get("cat") != c:
                continue
            hit = _fact_label_match(f, kws) if label_only else _fact_matches(f, kws)
            if hit:
                val = _norm_text(f.get("value"))
                if val and "【待填写" not in val:
                    return val, {
                        "fact_key": _norm_text(f.get("key")),
                        "fact_label": _norm_text(f.get("label")),
                        "matched_by": "label" if label_only else "value",
                    }
        return "", None

    for c, label_only, in ((cat, True), (None, True), (cat, False), (None, False)):
        val, prov = _first_pass(c, label_only)
        if val:
            return val, prov
    return "", None


def _extract_from_facts(facts: list, field: dict) -> str:
    """从全局事实列表中为某个字段抽取权威值（首个匹配且非占位的值）。

    两级匹配：先做字段名级（label/key）精确匹配，避免把「设备型号：QTZ80」
    错映射给「技术参数」这类泛化字段；全部未命中再退化为值级匹配。
    """
    return _match_fact(facts, field)[0]


# ============================================================
# 七、构建提取项目字段（从 facts 派生，保证同源）
# ============================================================

def extract_extraction_fields(facts: list, explicit: dict | None = None) -> dict:
    """构建 13 项提取项目字段的权威值 dict。

    Args:
        facts: 全局事实列表（facts_service 规范化后的 items）
        explicit: 调用方显式提供的字段（优先级最高，用于覆盖/补全）

    Returns:
        dict: {field_key: value(str)}，缺失且无来源时填 default（多为空串）。
    """
    return extraction_fields_record(facts, explicit)["fields"]


def extraction_fields_record(facts: list, explicit: dict | None = None) -> dict:
    """构建可持久化的结构化提取项目字段表（含逐字段溯源）。

    ENHANCE 2026-10-07 第三轮（遗留问题 14.4 落地）：事实写入链路
    （POST /facts、/facts/generate）调用本函数，把 13 项结构化字段表
    落库到 `analysis.extraction_fields_json`；取数链路读出后经
    `project_data["extraction_fields"]` 直供 `build_field_contract`
    的显式通道 —— 正文生成不再在运行时重复跑启发式派生。

    Returns:
        dict: {
            "fields":     {field_key: value},          # 13 键恒齐
            "provenance": {field_key: {"source": "explicit"|"facts",
                                        "fact_key", "fact_label", "matched_by"}},
            "version":    "1.0",
        }
    """
    explicit = explicit or {}
    fields: dict[str, str] = {}
    provenance: dict[str, dict] = {}
    for f in EXTRACTION_PROJECT_FIELDS:
        key = f["key"]
        if _norm_text(explicit.get(key)):
            fields[key] = _norm_text(explicit[key])
            provenance[key] = {"source": "explicit"}
            continue
        val, prov = _match_fact(facts, f)
        fields[key] = val or (f.get("default") or "")
        if val:
            provenance[key] = {"source": "facts", **(prov or {})}
    return {"fields": fields, "provenance": provenance, "version": "1.0"}


def parse_extraction_fields_record(raw: Any) -> dict | None:
    """解析库中存储的结构化字段表（容错：非法/旧格式返回 None 走运行时派生）。"""
    if raw is None:
        return None
    text = raw if isinstance(raw, str) else str(raw)
    text = text.strip()
    if not text or text in ("{}", "null"):
        return None
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(parsed, dict):
        return None
    fields = parsed.get("fields")
    if not isinstance(fields, dict):
        return None
    clean: dict[str, str] = {}
    for f in EXTRACTION_PROJECT_FIELDS:
        v = _norm_text(fields.get(f["key"]))
        if v:
            clean[f["key"]] = v
    if not clean:
        return None
    out = {"fields": clean, "version": str(parsed.get("version") or "1.0")}
    if isinstance(parsed.get("provenance"), dict):
        out["provenance"] = parsed["provenance"]
    return out


# ============================================================
# 八、构建全局数据字典（关键一致性字段的唯一权威值）
# ============================================================

def build_global_data_dictionary(facts: list, extraction_fields: dict | None = None) -> dict:
    """构建全局数据字典：关键一致性字段的权威值。

    字典只保留「有值」的字段；正文生成时必须引用字典值，不得自行生成。
    来源优先级：显式提取字段 > 全局事实派生 > 默认（空）。

    Returns:
        dict: {
            "fields": {key: value},          # 关键一致性字段权威值
            "by_chapter": {chapter_idx: {key: value}},  # 按九大章节归类的可用事实
            "version": "1.0",
        }
    """
    extraction_fields = extraction_fields or {}
    fields: dict[str, str] = {}
    for kf in KEY_CONSISTENCY_FIELDS:
        # 优先提取项目字段；否则从 facts 按关键词抽（避免重复定义映射）
        if extraction_fields.get(kf):
            fields[kf] = _norm_text(extraction_fields[kf])
            continue
        meta = _EXTRACTION_BY_KEY.get(kf)
        if meta:
            val = _extract_from_facts(facts, meta)
            if val:
                fields[kf] = val
                continue
        # 直接按一致性字段的中文标签兜底抽
        _label_map = {
            "response_time": "响应时间", "personnel_count": "人员数量", "equipment_model": "设备型号",
            "warranty": "质保期", "quality_target": "质量目标", "safety_target": "安全目标",
            "schedule_detail": "进度", "milestone": "里程碑",
        }
        lbl = _label_map.get(kf)
        if lbl:
            val = _extract_from_facts(facts, {"fact_category": None, "fact_keywords": [lbl]})
            if val:
                fields[kf] = val

    # 按九大章节归类（用于「按需注入」与自检）
    by_chapter: dict[int, dict] = {}
    for idx, flds in GLOBAL_FACTS_FIELD_GROUPS.items():
        grp: dict[str, str] = {}
        for ff in flds:
            key = ff["key"]
            if fields.get(key):
                grp[key] = fields[key]
                continue
            # 非关键一致性字段也从 facts 抽
            meta = _EXTRACTION_BY_KEY.get(key)
            if meta:
                val = _extract_from_facts(facts, meta)
                if val:
                    grp[key] = val
        if grp:
            by_chapter[idx] = grp

    return {"fields": fields, "by_chapter": by_chapter, "version": "1.0"}


# ============================================================
# 九、章节字段选取（按需注入）
# ============================================================

def select_fields_for_chapter(
    chapter_title: str,
    facts: list,
    extraction_fields: dict | None = None,
    industry: str | None = None,
) -> dict:
    """按「章节 → 所需字段」映射表，返回本章需注入的字段 dict。

    Returns:
        dict: {
            "chapter_idx": int,
            "chapter_theme": str,
            "extraction": {label: value},
            "facts": {label: value},
            "missing": [label...],   # 映射要求但无值的字段（供自检）
        }
    """
    ch = classify_chapter(chapter_title, industry=industry)
    idx = ch["idx"]
    mapping = CHAPTER_FIELD_MAP.get(idx, {"extraction": [], "facts": []})
    extraction_fields = extraction_fields or {}

    ext_out: dict[str, str] = {}
    missing: list[str] = []
    for ek in mapping["extraction"]:
        meta = _EXTRACTION_BY_KEY.get(ek)
        if not meta:
            continue
        val = extraction_fields.get(ek) or _extract_from_facts(facts, meta) or (meta.get("default") or "")
        if val:
            ext_out[meta["label"]] = val
        elif meta.get("required"):
            missing.append(meta["label"])

    fact_out: dict[str, str] = {}
    for fk in mapping["facts"]:
        # 关键字段优先用提取字段（含全局字典派生值），否则按事实标签从 facts 抽
        found = ""
        meta = _EXTRACTION_BY_KEY.get(fk)
        _lbl = meta["label"] if meta else _FACT_ONLY_LABELS.get(fk, fk)
        if meta:
            found = extraction_fields.get(fk) or _extract_from_facts(facts, meta)
        if not found and fk in _FACT_ONLY_LABELS:
            # 仅事实字段：宽关键词表（含同义词），标签窄匹配会漏调人员/设备类事实
            _fk_kws = FACT_ONLY_KEYWORDS.get(fk) or [_FACT_ONLY_LABELS[fk]]
            found = _extract_from_facts(facts, {"fact_category": None, "fact_keywords": _fk_kws})
        if found:
            fact_out[_lbl] = found
    return {
        "chapter_idx": idx,
        "chapter_theme": ch["theme"],
        "extraction": ext_out,
        "facts": fact_out,
        "missing": missing,
    }


# ============================================================
# 十、提示词格式化（分层 / 按需 / 完整注入）
# ============================================================

#: 全局数据字典在提示词中的区块标题（稳定上下文，缓存友好）
GLOBAL_DICT_PROMPT_HEADER = "全局数据字典（权威值，全文唯一，正文必须一字不差引用，严禁自行编造）"


def format_global_dictionary_for_prompt(dictionary: dict) -> str:
    """把全局数据字典格式化为提示词区块（有序、可校验）。"""
    fields = dictionary.get("fields") or {}
    if not fields:
        return ""
    lines = [GLOBAL_DICT_PROMPT_HEADER + "："]
    # 按 KEY_CONSISTENCY_FIELDS 稳定顺序输出，保证缓存前缀一致
    ordered = [k for k in KEY_CONSISTENCY_FIELDS if k in fields]
    ordered += [k for k in fields if k not in KEY_CONSISTENCY_FIELDS]
    for k in ordered:
        lines.append(f"  - {_field_label(k)}：{fields[k]}")
    return "\n".join(lines)


def format_chapter_fields_for_prompt(chapter_fields: dict) -> str:
    """把本章按需选取的字段格式化为提示词区块。"""
    parts = []
    ext = chapter_fields.get("extraction") or {}
    fac = chapter_fields.get("facts") or {}
    if ext:
        lines = ["本章提取项目数据（须准确使用）："]
        for lbl, val in ext.items():
            lines.append(f"  - {lbl}：{val}")
        parts.append("\n".join(lines))
    if fac:
        lines = ["本章相关事实变量（须准确使用）："]
        for lbl, val in fac.items():
            lines.append(f"  - {lbl}：{val}")
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


# ============================================================
# 十一、字段调用对照表（数据源 vs 正文读取，做差集）
# ============================================================

def compute_call_diff(extraction_fields: dict, injected_keys: list[str], truncated_keys: list[str] | None = None) -> dict:
    """比较「提取项目字段清单」与「正文实际注入字段」，输出差集。

    Args:
        extraction_fields: 全部可用提取项目字段 {key: value}
        injected_keys: 实际注入提示词的字段 key 列表
        truncated_keys: 被截断的字段 key 列表（可选）

    Returns:
        dict: {
            "total": int,
            "injected": [...],
            "not_called": [...],          # 有但没读
            "truncated": [...],          # 读了但截断
            "missing_value": [...],       # 字段存在但无值（未注入）
        }
    """
    truncated_keys = truncated_keys or []
    all_keys = list(extraction_fields.keys())
    injected_set = set(injected_keys)
    not_called = [k for k in all_keys if k not in injected_set]
    missing_value = [k for k in all_keys
                     if k in injected_set and not _norm_text(extraction_fields.get(k))]
    return {
        "total": len(all_keys),
        "injected": sorted(injected_set),
        "not_called": not_called,
        "truncated": truncated_keys,
        "missing_value": missing_value,
    }


# ============================================================
# 十二、前后一致性校验（生成后自检核心）
# ============================================================

#: 关键字段在正文中的抽取正则（宽松匹配「标签：值」或「标签 值」）
_FIELD_VALUE_RE = re.compile(
    r"(项目名称|项目编号|建设单位|业主|发包人|招标人|工期|总工期|质保期|质量目标|安全目标|响应时间|"
    r"人员数量|劳动力|设备型号|参建单位|施工总承包)[：: ]?\s*([^\n，。；；、]{1,40})"
)


#: 正文标签 → 字段键 归一化映射（抽取与定点修复共用）
_LABEL_TO_KEY: dict[str, str] = {
    "建设单位": "owner", "业主": "owner", "发包人": "owner", "招标人": "owner",
    "项目名称": "project_name", "项目编号": "project_no",
    "总工期": "duration", "工期": "duration",
    "质保期": "warranty", "质量目标": "quality_target", "安全目标": "safety_target",
    "响应时间": "response_time", "人员数量": "personnel_count", "劳动力": "personnel_count",
    "设备型号": "equipment_model", "参建单位": "participants", "施工总承包": "participants",
}


def extract_key_values_from_content(content: str) -> dict[str, str]:
    """从已生成正文中抽取关键字段出现的值（用于与字典比对）。"""
    out: dict[str, str] = {}
    if not content:
        return out
    for m in _FIELD_VALUE_RE.finditer(content):
        val = m.group(2).strip()
        norm = _LABEL_TO_KEY.get(m.group(1), m.group(1))
        if norm not in out:
            out[norm] = val
    return out


def check_content_consistency(content: str, dictionary: dict) -> list[dict]:
    """检查本章正文关键字段是否与全局数据字典冲突。

    Returns:
        list[dict]: 每项 {field, expected, actual, type:"conflict"}，空列表表示一致。
    """
    fields = dictionary.get("fields") or {}
    if not fields or not content:
        return []
    found = extract_key_values_from_content(content)
    issues: list[dict] = []
    for key, expected in fields.items():
        actual = found.get(key)
        if not actual:
            continue
        # 归一化比较：取 expected 的数字/关键字，actual 包含 expected 视为一致
        _e = _norm_text(expected)
        _a = _norm_text(actual)
        if not _e or not _a:
            continue
        if _e not in _a and _a not in _e:
            issues.append({
                "field": _field_label(key),
                "expected": _e,
                "actual": _a,
                "type": "conflict",
            })
    return issues


def auto_fix_consistency_conflicts(content: str, dictionary: dict) -> tuple[str, list[dict]]:
    """定点自动修复正文与全局数据字典的冲突（只动必要位置）。

    需求四.5「修复时只动必要位置不重写已经正确的内容」的实现：
    仅把「标签：错误值」这一小片段替换为字典权威值，其余正文一字不动。

    Returns:
        (fixed_content, fixes): 修复后的正文与修复清单
            fixes 每项 {field, old, new}；无可修复冲突时返回 (原文, [])
    """
    fields = dictionary.get("fields") or {}
    if not fields or not content:
        return content, []
    fixes: list[dict] = []
    # 第一遍收集需替换的 (start, end, old, new)；从后往前替换避免位置偏移
    replacements: list[tuple[int, int, str, str, str]] = []
    for m in _FIELD_VALUE_RE.finditer(content):
        key = _LABEL_TO_KEY.get(m.group(1))
        expected = _norm_text(fields.get(key)) if key else ""
        actual = m.group(2).strip()
        if not expected or not actual:
            continue
        if expected in actual or actual in expected:
            continue  # 一致，不动
        replacements.append((m.start(2), m.end(2), actual, expected, m.group(1)))
    for start, end, old, expected, label in sorted(replacements, key=lambda r: r[0], reverse=True):
        content = content[:start] + expected + content[end:]
        fixes.append({"field": _field_label(_LABEL_TO_KEY[label]), "old": old, "new": expected})
    return content, fixes


def check_placeholder_in_content(content: str) -> list[str]:
    """检查正文是否出现【待补充】/【待填写】等占位标记（带括号形态优先去重）。"""
    if not content:
        return []
    hits = []
    for pat in ("【待补充", "【待填写", "【待定", "TODO", "待补充", "待填写", "待定"):
        if pat in content:
            hits.append(pat)
    # 带括号形态已命中时不再重复报裸词（如【待补充】与「待补充」）
    bracketed = any(h.startswith("【") for h in hits)
    if bracketed:
        hits = [h for h in hits if h.startswith("【") or h == "TODO"]
    # 归一化为完整标记名，去重
    out: list[str] = []
    for h in hits:
        name = {"【待补充": "【待补充】", "【待填写": "【待填写】", "【待定": "【待定】",
                "待补充": "待补充", "待填写": "待填写", "待定": "待定", "TODO": "TODO"}[h]
        if name not in out:
            out.append(name)
    return out


# ============================================================
# 十三、生成后自检入口（数据调用 / 上下文 / 一致性 / 质量）
# ============================================================

def self_check_content(
    content: str,
    chapter_title: str,
    dictionary: dict | None = None,
    facts: list | None = None,
    extraction_fields: dict | None = None,
    injected_keys: list[str] | None = None,
) -> dict:
    """单章节生成后自检。

    Returns:
        dict: {
            "chapter": str,
            "ok": bool,
            "issues": [ {level, type, detail} ],
            "auto_fixable": [ ... ],   # 可自动修复项
            "needs_review": [ ... ],   # 需人工复核项
        }
    """
    issues: list[dict] = []
    auto_fixable: list[dict] = []
    needs_review: list[dict] = []

    content = content or ""
    # 1) 占位标记
    for ph in check_placeholder_in_content(content):
        issues.append({"level": "error", "type": "placeholder", "detail": f"正文出现占位标记：{ph}"})
        needs_review.append({"type": "placeholder", "detail": ph})

    # 2) 上下文注入自检（字段调用完整性）
    if extraction_fields and injected_keys is not None:
        diff = compute_call_diff(extraction_fields, injected_keys)
        for k in diff["not_called"]:
            issues.append({"level": "warn", "type": "field_not_called",
                           "detail": f"字段未注入：{_field_label(k)}"})
            needs_review.append({"type": "field_not_called", "detail": lbl})

    # 3) 前后一致性（与全局数据字典冲突）
    if dictionary:
        for c in check_content_consistency(content, dictionary):
            issues.append({"level": "error", "type": "consistency_conflict",
                           "detail": f"{c['field']} 字典值「{c['expected']}」与正文「{c['actual']}」冲突"})
            needs_review.append({"type": "consistency_conflict", "detail": c})

    # 4) 质量：空 / 过短
    if len(content.strip()) < 30:
        issues.append({"level": "error", "type": "too_short", "detail": "正文过短（<30字）"})
        needs_review.append({"type": "too_short", "detail": ""})

    return {
        "chapter": chapter_title,
        "ok": len([i for i in issues if i["level"] == "error"]) == 0,
        "issues": issues,
        "auto_fixable": auto_fixable,
        "needs_review": needs_review,
    }


def run_content_self_checks(results: list[dict], dictionary: dict | None = None) -> list[dict]:
    """对一组生成结果批量自检，并把 `self_check` 附加到每个 result。

    Args:
        results: generate_sections 返回的 list[dict]（含 title/content）
        dictionary: 全局数据字典（用于一致性校验）

    Returns:
        list[dict]: 自检报告列表
    """
    reports: list[dict] = []
    for r in results:
        if not isinstance(r, dict):
            continue
        content = r.get("content") or ""
        if not content:
            continue
        rep = self_check_content(content, r.get("title", ""), dictionary=dictionary)
        # 可自动修复 → 立即定点修复（需求六.5：可自动修复立即修复）
        if dictionary and not rep["ok"]:
            fixed, fixes = auto_fix_consistency_conflicts(content, dictionary)
            if fixes:
                r["content"] = fixed
                rep = self_check_content(fixed, r.get("title", ""), dictionary=dictionary)
                rep["auto_fixed"] = fixes
        r["self_check"] = rep
        reports.append(rep)
    return reports


# ============================================================
# 十四、构建完整字段契约（SSE 入口一次性调用）
# ============================================================

def build_field_contract(project_data: dict, facts: list | None = None, policy: str = "fabricate") -> dict:
    """正文生成入口构建完整字段契约。

    Args:
        project_data: SSE 取数结果（含 summary / global_facts_items / construction_method 等）。
            可选键：
              * extraction_fields —— 结构化提取项目字段直供通道（dict，键为
                EXTRACTION_PROJECT_FIELDS 的 key）。优先级最高：解析模块将来落
                结构化字段表后经此通道直供，无需改动任何下游代码；
              * facts_policy —— 缺失值策略（fabricate/omit/placeholder）。
        facts: 全局事实列表（缺省从 project_data["global_facts_items"] 取）
        policy: 缺失值策略（缺省读 project_data["facts_policy"]，兜底 fabricate）

    Returns:
        dict: {
            "extraction_fields": {...},
            "global_dictionary": {...},
            "policy": str,
            "trace": {"built_at": ..., "facts_count": int, "explicit_keys": [...]},
        }
    """
    facts = facts if facts is not None else (project_data.get("global_facts_items") or [])
    if not isinstance(facts, (list, tuple)):
        facts = []
    # 显式提取字段（显式直供通道 > project_data 顶层同名字段 > facts 派生）
    explicit: dict[str, str] = {}
    supplied = project_data.get("extraction_fields")
    if isinstance(supplied, dict):
        for k, v in supplied.items():
            if _norm_text(v):
                explicit[str(k)] = _norm_text(v)
    for k in ("project_name", "project_no", "owner", "location", "scale", "structure_type",
              "duration", "budget"):
        v = project_data.get(k)
        if _norm_text(v):
            explicit.setdefault(k, _norm_text(v))
    if not policy or policy == "fabricate":
        policy = normalize_policy(project_data.get("facts_policy"))
    extraction_fields = extract_extraction_fields(facts, explicit)
    global_dictionary = build_global_data_dictionary(facts, extraction_fields)
    return {
        "extraction_fields": extraction_fields,
        "global_dictionary": global_dictionary,
        "policy": policy,
        "trace": {"built_at": "runtime", "facts_count": len(facts),
                  "explicit_keys": sorted(explicit.keys())},
    }


# ============================================================
# 十五、字段级定向重生成（数据变更传播，需求四.4）
# ============================================================
#
# ENHANCE 2026-10-07 第二轮（遗留问题落地）：
#   全局事实更新时，不再只有「全量失效 / 全量重生成」一种粗粒度选择：
#     1. diff_key_field_changes()     —— 比对更新前后关键一致性字段的值差异
#     2. compute_affected_section_ids() —— 圈定引用了被变更字段的章节
#     3. 调用方（facts 保存端点）据此：① 返回受影响章节清单（前端可按
#        generate_content_impl 既有 regenerate_ids 精确重生成）；
#        ② 全量缓存失效保持不变（安全基线），定向重生成成为增量能力。

def diff_key_field_changes(old_facts: list | None, new_facts: list | None) -> dict[str, dict]:
    """比对更新前后「关键一致性字段」权威值的差异。

    Args:
        old_facts / new_facts: 更新前后的全局事实条目列表

    Returns:
        dict: {field_key: {"old": str, "new": str}}，仅含值确实变化的字段；
              空字典表示关键一致性字段无变化。
    """
    old_dict = build_global_data_dictionary(list(old_facts or [])).get("fields") or {}
    new_dict = build_global_data_dictionary(list(new_facts or [])).get("fields") or {}
    changes: dict[str, dict] = {}
    for key in KEY_CONSISTENCY_FIELDS:
        old_v = _norm_text(old_dict.get(key))
        new_v = _norm_text(new_dict.get(key))
        if old_v != new_v:
            changes[key] = {"old": old_v, "new": new_v}
    return changes


def _chapters_for_field(key: str) -> set[int] | None:
    """引用某字段的章节集合；返回 None 表示全局字段（影响全部九章）。"""
    meta = _EXTRACTION_BY_KEY.get(key)
    if meta and meta.get("chapters") == "*":
        return None
    chapters: set[int] = set()
    for idx, m in CHAPTER_FIELD_MAP.items():
        if key in m["extraction"] or key in m["facts"]:
            chapters.add(idx)
    return chapters


def compute_affected_section_ids(changed_keys: list[str], sections: list) -> dict:
    """圈定引用了被变更字段的章节（定向重生成的精确范围）。

    Args:
        changed_keys: 被变更的字段 key 列表（diff_key_field_changes 的键）
        sections: 章节行列表（每项含 id/title，允许 sqlite.Row → 需先转 dict
                  或仅访问 title/id；此处用 dict 兼容方式读取）

    Returns:
        dict: {
            "section_ids": [...],   # 受影响章节 id（保持传入顺序去重）
            "chapters": [...],      # 受影响的九大章节序号
            "global_change": bool,  # True 表示全局字段变更（全部章节受影响）
        }
    """
    if not changed_keys:
        return {"section_ids": [], "chapters": [], "global_change": False}

    affected_chapters: set[int] = set()
    global_change = False
    for key in changed_keys:
        chs = _chapters_for_field(key)
        if chs is None:
            global_change = True
            affected_chapters = set(range(1, 10))
            break
        affected_chapters |= chs

    section_ids: list = []
    for s in sections or []:
        if not isinstance(s, dict):
            try:
                s = dict(s)
            except Exception:
                continue
        title = s.get("title", "") or ""
        if global_change or classify_chapter(title)["idx"] in affected_chapters:
            sid = s.get("id")
            if sid is not None and sid not in section_ids:
                section_ids.append(sid)
    return {
        "section_ids": section_ids,
        "chapters": sorted(affected_chapters),
        "global_change": global_change,
    }
