# -*- coding: utf-8 -*-
"""可行性研究报告提示词与大纲模板（ENHANCE 2026-09-15）。

移植自易标 `feasibilityReportPrompts.cjs` / `feasibilityOutlineTask.cjs`，核心原则保持一致：
- **严禁编造**：只能基于项目参数与用户资料提取事实；缺失值写【待补充】、冲突写【待确认】；
- 通用大纲是一级章节框架 + 二级细化起点，AI 必须结合项目资料具体化，不得输出空泛标题；
- 关键参数阶段不自动计算 NPV/IRR 等财务指标。
"""

from __future__ import annotations

import re
from typing import Any

# ===== 通用大纲模板（7 套，章节 + 建议二级标题） =====

OUTLINE_TEMPLATES: dict[str, dict[str, Any]] = {
    "government": {
        "label": "政府投资项目通用大纲（2023版标准）",
        "chapters": [
            "概述", "项目建设背景和必要性", "项目需求分析与产出方案", "项目选址与要素保障",
            "项目建设方案", "项目运营方案", "项目投融资与财务方案", "项目影响效果分析",
            "项目风险管控方案", "研究结论及建议",
        ],
        "suggested_children": [
            ["项目概况", "项目单位概况", "编制依据", "主要结论和建议"],
            ["项目建设背景", "规划政策符合性", "项目建设必要性"],
            ["需求分析", "建设内容和规模", "项目产出方案"],
            ["项目选址", "项目建设条件", "要素保障分析"],
            ["技术方案", "设备方案", "工程方案", "用地征收补偿方案", "数字化方案", "建设管理方案"],
            ["运营模式选择", "运营组织方案", "安全保障方案", "绩效管理方案"],
            ["投资估算", "融资方案", "财务可持续性分析"],
            ["经济影响分析", "社会影响分析", "生态环境影响分析", "资源和能源利用效果分析", "碳达峰碳中和分析"],
            ["风险识别与评价", "风险管控方案", "风险应急预案"],
            ["主要研究结论", "问题与建议"],
        ],
    },
    "enterprise": {
        "label": "企业投资项目参考大纲（2023版标准）",
        "chapters": [
            "概述", "项目建设背景、需求分析及产出方案", "项目选址与要素保障", "项目建设方案",
            "项目运营方案", "项目投融资与财务方案", "项目影响效果分析", "项目风险管控方案",
            "研究结论及建议",
        ],
        "suggested_children": [
            ["项目概况", "项目单位概况", "编制依据", "主要结论和建议"],
            ["项目建设背景", "规划政策符合性", "项目需求分析", "项目产出方案"],
            ["项目选址", "项目建设条件", "要素保障分析"],
            ["技术方案", "设备方案", "工程方案", "数字化方案", "建设管理方案"],
            ["运营模式选择", "运营组织方案", "安全保障方案", "绩效管理方案"],
            ["投资估算", "盈利能力分析", "融资方案", "债务清偿能力分析", "财务可持续性分析"],
            ["经济影响分析", "社会影响分析", "生态环境影响分析", "资源和能源利用效果分析", "碳达峰碳中和分析"],
            ["风险识别与评价", "风险管控方案", "风险应急预案"],
            ["主要研究结论", "问题与建议"],
        ],
    },
    "industrial": {
        "label": "工业与高端制造可行性研究大纲",
        "chapters": [
            "概述", "项目建设背景与必要性", "产品方案与生产规模", "工艺技术与主要设备方案",
            "厂址选择与公用工程", "节能减排与环境保护", "项目实施进度与组织架构",
            "投资估算与资金筹措", "财务评价与风险评估", "结论及建议",
        ],
        "suggested_children": [
            ["项目概况", "编制依据与范围", "主要技术经济指标", "结论与建议"],
            ["国家与行业政策背景", "市场需求与产能缺口", "建设必要性分析"],
            ["产品大纲及质量标准", "生产规模及产能安排", "产品市场竞争力"],
            ["工艺技术路线与原理", "主要生产及检测设备选型", "设备自动化与智能化"],
            ["厂址选址与建设条件", "总图运输与公用工程", "辅助生产设施"],
            ["能耗分析与节能措施", "三废治理与环保方案", "绿色制造与低碳指标"],
            ["组织机构与劳动定员", "人员培训计划", "项目实施进度节点"],
            ["建设投资与流动资金估算", "资金筹措方案及还本付息"],
            ["营业收入及成本预测", "NPV/IRR/投资回收期计算", "敏感性分析与风险管控"],
            ["综合可行性评估", "存在问题及后续实施建议"],
        ],
    },
    "hi_tech": {
        "label": "高新技术与数字化/信息化大纲",
        "chapters": [
            "概述", "项目建设背景与必要性", "技术路线与系统架构设计", "数据要素与网络安全方案",
            "软硬件设备与部署实施", "运营模式与运维保障", "投资估算与资金筹措",
            "效益分析与财务评价", "风险管控与结论",
        ],
        "suggested_children": [
            ["项目背景", "编制依据", "技术亮点", "总体结论"],
            ["数字经济/高新技术政策背景", "行业痛点与技术创新必要性", "经济与社会价值"],
            ["总体技术架构", "核心算法/关键技术突破", "模块划分与数据流向"],
            ["数据采集与存储方案", "网络安全与等级保护", "数据合规与隐私保护"],
            ["软硬件资源配置", "云原生/边缘计算部署", "系统集成与测试"],
            ["业务运营模式", "运维管理与SLA保障", "团队组织与人员培训"],
            ["研发及软硬件投入估算", "资金筹措与使用计划"],
            ["直接与间接经济效益", "财务可行性指标分析"],
            ["技术风险与合规风险管控", "研究结论与建议"],
        ],
    },
    "infrastructure": {
        "label": "基础设施与公用事业大纲",
        "chapters": [
            "概述", "建设背景与规划符合性", "建设规模与工程技术方案", "线路选址与要素保障",
            "工程建设与运营保障方案", "投资估算与融资方案", "社会经济与公益效益评价", "风险管控与结论",
        ],
        "suggested_children": [
            ["项目概况", "规划依据", "建设规模", "研究结论"],
            ["区域发展规划符合性", "基础设施建设必要性", "交通/公用事业需求"],
            ["工程建设规模", "主体工程与路线方案", "配套公用设施方案"],
            ["选址及用地征收方案", "水资源及能源保障", "生态与环境影响评价"],
            ["建设工期安排与施工方案", "运营管理模式与安全保障", "应急响应预案"],
            ["工程总投资估算", "政府专项债/PPP/自筹融资方案"],
            ["社会影响与民生改善评价", "区域经济拉动效益"],
            ["工程安全与社会稳定风险评估", "总体研究结论"],
        ],
    },
    "eco_environmental": {
        "label": "农业与生态环保项目大纲",
        "chapters": [
            "概述", "背景与生态保护必要性", "资源禀赋与工程选址", "技术方案与工艺流程",
            "生态效益与碳中和评估", "项目建设与运营管理", "投资估算与资金筹措",
            "财务与可持续性分析", "风险防范与结论",
        ],
        "suggested_children": [
            ["项目概况", "编制依据", "生态目标", "结论建议"],
            ["国家生态文明政策", "区域生态环境现状与痛点", "项目建设必要性"],
            ["自然地理与资源条件", "工程选址与合规性分析", "用地与水资源保障"],
            ["生态修复/农业循环技术方案", "主要工艺设备与治理流程", "自动化监控系统"],
            ["生态系统服务价值评估", "减碳/固碳量预测与碳达峰"],
            ["工程建设进度安排", "生态监测与长效运营机制"],
            ["工程建设与生态投资估算", "绿色金融/政府补贴资金"],
            ["直接收益与生态补偿收益", "财务可持续性评估"],
            ["生态风险与环境敏感区防范", "可行性结论与建议"],
        ],
    },
    "commercial_realestate": {
        "label": "商业/园区与地产开发大纲",
        "chapters": [
            "概述", "项目背景与区域市场分析", "建设规模与规划设计方案", "选址条件与配套要素保障",
            "开发进度与营销运营方案", "投资估算与资金筹措", "财务评价与敏感性分析",
            "社会效益与风险控制", "研究结论与建议",
        ],
        "suggested_children": [
            ["项目名称与性质", "编制依据", "项目定位", "综合结论"],
            ["区域经济与商业环境", "周边竞争格局与供需分析", "项目定位与客群分析"],
            ["建筑总体规划与指标", "空间布局与功能分区", "景观与建筑风格"],
            ["项目选址与交通条件", "市政配套与要素保障", "土地获取与权属说明"],
            ["开发建设周期与节点", "招商与营销推广方案", "物业运营与商业管理"],
            ["开发建设总投资估算", "资金筹措与动态现金流"],
            ["销售/租赁收入预测", "NPV/IRR/动态回收期分析", "售价及出租率敏感性测试"],
            ["区域商业激活与就业带动", "市场与政策风险防范"],
            ["总体可行性结论", "项目实施推进建议"],
        ],
    },
}

#: 资料分析九板块（字段 | 中文标题）
ANALYSIS_FIELDS: list[tuple[str, str]] = [
    ("project_overview", "项目概况"),
    ("background_and_necessity", "建设背景与必要性"),
    ("demand_and_output", "需求分析与产出方案"),
    ("site_and_conditions", "项目选址与要素保障"),
    ("construction_and_technical_conditions", "建设内容与技术条件"),
    ("operation_conditions", "运营条件"),
    ("investment_and_financing", "投资与资金资料"),
    ("impact_and_risks", "影响效果与风险"),
    ("missing_information", "缺失资料清单"),
]

#: 章节编号前缀（AI 输出目录时可能带编号，归一化时剥掉）
_TITLE_PREFIX_RE = re.compile(r"^第?[一二三四五六七八九十百零〇\d.、\s]+[章节篇]?\s*")


def normalize_template(template_id: Any) -> str:
    tid = str(template_id or "").strip()
    return tid if tid in OUTLINE_TEMPLATES else "government"


def get_template_label(template_id: Any) -> str:
    return OUTLINE_TEMPLATES[normalize_template(template_id)]["label"]


def format_project_info(info: dict) -> str:
    """项目基础参数块（易标 formatProjectInfo；缺项按【待补充】占位，AI 不得编造）。"""
    info = info or {}
    type_label = "企业投资项目" if info.get("projectType") == "enterprise" else "政府投资项目"
    return "\n".join(
        [
            f"项目名称：{info.get('projectName') or '【待补充】'}",
            f"项目类型：{type_label}",
            f"所属行业：{info.get('industry') or '【待补充】'}",
            f"建设单位：{info.get('constructionUnit') or '【待补充】'}",
            f"建设地点：{info.get('location') or '【待补充】'}",
            f"建设内容与规模：{info.get('constructionContent') or '【待补充】'}",
            f"建设期：{info.get('constructionPeriodYears') or '【待补充】'} 年",
            f"运营期：{info.get('operationPeriodYears') or '【待补充】'} 年",
            f"总投资：{info.get('totalInvestment') or '【待补充】'}",
            f"资金来源：{info.get('fundingSource') or '【待补充】'}",
        ]
    )


def format_outline_template_tree(template_id: Any) -> str:
    template = OUTLINE_TEMPLATES[normalize_template(template_id)]
    lines: list[str] = []
    for index, title in enumerate(template["chapters"]):
        lines.append(f"{index + 1}. {title}")
        for child_index, child in enumerate(template["suggested_children"][index] or []):
            lines.append(f"  {index + 1}.{child_index + 1} {child}")
    return "\n".join(lines)


def build_outline_template_markdown(template_id: Any, target_words: Any) -> str:
    """大纲模板 Markdown（目录生成任务的输入材料之一）。"""
    template = OUTLINE_TEMPLATES[normalize_template(template_id)]
    return "\n".join(
        [
            f"# 选用的通用大纲：{template['label']}",
            "",
            f"目标总字数约 {int(target_words) if target_words else 30000} 字。",
            "",
            "一级目录原则上保留本大纲主框架，可根据项目明显不适用的内容合并或调整，"
            "但不得遗漏结论、风险、影响和投资相关内容。",
            "二、三级目录必须结合本项目资料具体化；下列二级标题是大纲组成部分，"
            "应作为细化起点写入目录，而不是可忽略的参考清单。",
            "",
            format_outline_template_tree(template_id),
        ]
    )


def analysis_to_markdown(payload: dict) -> str:
    """把资料分析 JSON（九板块）渲染为 Markdown。"""
    payload = payload if isinstance(payload, dict) else {}
    sections: list[str] = []
    for key, title in ANALYSIS_FIELDS:
        raw = str(payload.get(key) or "").strip()
        if not raw:
            raw = (
                "- 【待补充】尚未识别到明确缺失项，请人工核对资料完整性。"
                if key == "missing_information"
                else "【待补充】现有资料未提供足够信息。"
            )
        sections.append(f"## {title}\n\n{raw}")
    return "\n\n".join(sections)


def build_analysis_system_prompt() -> str:
    return (
        "你是严谨的中国建设项目可行性研究资料分析专家。只能基于项目参数和用户资料提取事实；"
        "不得编造金额、规模、地点、期限、政策名称或技术参数。"
        "无资料文件时只使用项目参数（含建设内容与规模）；此时资料块可能为\"未导入资料文件\"。"
    )


def build_analysis_user_instruction(segment_index: int, total_segments: int) -> str:
    fields = {key: "Markdown 文本" for key, _title in ANALYSIS_FIELDS}
    import json as _json
    return (
        "请把当前资料中可用于编制可行性研究报告的信息整理为 JSON。\n\n"
        "要求：\n"
        "1. project_overview：项目名称、单位、地点、性质、规模、建设内容、工期等。\n"
        "2. background_and_necessity：背景、问题、规划政策关系和建设必要性。\n"
        "3. demand_and_output：需求对象、现状缺口、建设规模依据和预期产出。\n"
        "4. site_and_conditions：选址、用地、交通、市政、资源、审批和建设条件。\n"
        "5. construction_and_technical_conditions：技术路线、工程、设备、数字化和建设管理资料。\n"
        "6. operation_conditions：运营模式、组织、安全、绩效和运维资料。\n"
        "7. investment_and_financing：投资、费用、资金来源、收益、成本和融资资料。\n"
        "8. impact_and_risks：经济、社会、生态、能源、碳排放和风险资料。\n"
        "9. missing_information：只列出本段明显缺失、矛盾或需要用户确认的关键资料；"
        "不要把当前分段未出现但可能存在于其他分段的内容武断判定为缺失。\n"
        "10. 每个字段输出 Markdown 文本；没有信息时输出空字符串。只返回 JSON。\n\n"
        f"当前资料分段：{segment_index}/{total_segments}\n\n"
        "JSON 格式：\n"
        f"{_json.dumps(fields, ensure_ascii=False, indent=2)}"
    )


def build_analysis_merge_system_prompt() -> str:
    return "你是严谨的可行性研究资料合并专家。只能合并用户提供的分析结果，不得创造新事实。"


def build_analysis_merge_user_instruction() -> str:
    return (
        "请合并重复信息、保留具体事实并标明资料矛盾。missing_information 只保留综合全部分段后"
        "仍然缺失、矛盾或需要确认的关键项。只返回与输入相同字段的 JSON。"
    )


def build_outline_system_prompt() -> str:
    return (
        "你是可行性研究报告总编。请基于项目实际资料，在通用大纲框架内形成完整、可执行、"
        "可编辑的三级以内报告目录。"
    )


def build_outline_user_instruction(template_id: Any, target_words: Any, has_knowledge: bool = False) -> str:
    knowledge_rule = (
        '6. 可选为叶子节点推荐素材："knowledge_item_ids": [素材编号]。'
        "编号只能取自知识素材清单，不确定就留空，不要编造编号。\n"
        if has_knowledge
        else ""
    )
    return (
        "请基于以下材料，生成可行性研究报告目录，只返回 JSON。\n\n"
        f"【项目基础参数】\n（见上一条消息）\n\n"
        f"【大纲框架】\n{build_outline_template_markdown(template_id, target_words)}\n\n"
        "【输出格式】\n"
        '返回 JSON 对象：{"outline": [{"title": "章节标题", "description": "本节写作重点", '
        '"children": [...]}]}\n'
        "规则：\n"
        "1. 目录最多三级；title 只写纯标题，不要带章节编号或 Markdown 标记。\n"
        "2. description 写明本节应论证的重点、已知资料和缺失资料处理要求。\n"
        "3. 只能输出 title/description/children 三类字段，不要输出正文，不要编造项目事实。\n"
        "4. 一级目录保留大纲主框架，可按项目明显不适用的内容合并调整，"
        "但不得遗漏结论、风险、影响和投资相关章节。\n"
        "5. 二、三级目录必须结合项目资料具体化，大纲中的二级标题是细化起点而非参考清单。\n"
        + knowledge_rule
    )


def build_parameters_system_prompt() -> str:
    return (
        "你是可行性研究报告的关键参数审校专家。你必须区分已知事实与缺失信息，"
        "严禁自行编造数字、政策、设备参数、地点或资金安排。"
    )


def build_parameters_user_instruction() -> str:
    return (
        "请生成\"关键参数与编制口径\"Markdown，至少包含：项目身份信息、建设目标与规模、"
        "建设地点与条件、建设期与进度、技术路线与主要设备、投资与资金来源、运营期与组织、"
        "安全环保能源口径、经济社会效益口径、风险与待确认事项。\n\n"
        "规则：\n"
        "1. 已有明确事实直接写入。\n"
        "2. 未提供的关键参数统一写\"【待补充】\"，不要填常见值或经验值。\n"
        "3. 资料存在冲突时写\"【待确认】\"并列出冲突内容。\n"
        "4. 本阶段不自动计算 NPV、IRR、回收期等财务指标。\n"
        "5. 使用二级标题和简短 bullet，直接输出 Markdown。"
    )


def build_content_system_prompt() -> str:
    return (
        "你是专业的可行性研究报告编制专家。正文必须基于用户提供的项目事实和资料，"
        "论证清晰、语言正式。不得编造金额、规模、地点、期限、批复、政策名称、设备参数或财务指标。"
    )


def build_content_writing_rules() -> str:
    return (
        "写作规则：\n"
        "1. 只生成当前叶子章节正文，不重复输出章节标题。\n"
        "2. 对已有资料进行分析、论证和结构化表达；可以使用 Markdown 小标题、列表和必要表格。\n"
        "3. 没有依据的关键数据明确写\"【待补充】\"或采用不含虚构数字的定性表达。\n"
        "4. 不把需求、建议或通用规范写成已经完成的事实。\n"
        "5. 与全文关键参数保持一致。\n"
        "6. 若当前章节涉及项目选址与建设条件、总图布置、工艺流程、环保设施或实施进度等"
        "工程技术/选址章节，请在最佳位置嵌入且仅嵌入 1 处插图指引框，格式固定为：\n"
        "> **【插图指引】：图片名称**\n"
        "> *说明：此处请插入...*\n"
        "7. 直接输出 Markdown 正文。"
    )


def build_knowledge_background_note() -> str:
    """资料分析阶段注入知识库素材时的约束（ENHANCE 2026-09-15）。

    知识库素材来自其它项目的同类报告：只能用于理解行业背景、专业术语与论证口径，
    **不得**当成本项目事实（金额/规模/地点/批复等），否则会把别的项目数据带进分析结论。
    """
    return (
        "以下清单是行业知识素材（来自其它项目的同类报告），仅用于理解行业背景、"
        "专业术语与常见论证口径。严禁把这些素材里的金额、规模、地点、期限、批复、"
        "政策名称或技术参数当作本项目事实写入分析结果；本项目事实只能来自项目基础参数与本项目资料。"
    )


def build_knowledge_catalog_note() -> str:
    """目录/参数阶段注入知识库条目清单时的约束。"""
    return (
        "以下是可选用的知识素材清单（编号 + 标题 + 摘要），可用于细化相关章节的写作重点。"
        "不需要逐条使用，也不要引用清单编号或提及素材来源。"
    )


def build_knowledge_material_note() -> str:
    """正文生成阶段注入知识库素材时的约束（改写为本项目语境）。"""
    return (
        "以下是可吸收的知识素材。请把它们改写到本项目的语境与数据体系中，"
        "不要提及\"知识库\"\"历史文档\"或资料来源；素材与本项目资料冲突时，以本项目资料为准。"
    )


def build_human_writing_system_prompt() -> str:
    return (
        "你是中文公文与工程咨询报告润色编辑。请对给定章节做自然化审校，使行文更像人工撰写。"
        "保护事实和参数，压缩重复表达，改善中文词序，清理模板腔与模型腔，同时保持正式可研文风。"
        "必须完整保留所有数量、单位、金额、日期、比例，以及【待补充】【待确认】标记。"
        "不得新增事实、不得删改结论口径、不得计算财务指标。"
        "不得输出章节标题，不得补写资料中不存在的事实。只输出修订后的 Markdown 正文，不要解释。"
    )


def render_outline_for_prompt(items: list, prefix: str = "") -> str:
    """把目录树渲染为带编号的文本（供提示词引用）。"""
    lines: list[str] = []
    for index, item in enumerate(items or []):
        number = f"{prefix}.{index + 1}" if prefix else f"{index + 1}"
        title = str((item or {}).get("title") or "").strip()
        description = str((item or {}).get("description") or "").strip()
        lines.append(f"{number} {title}" + (f"：{description}" if description else ""))
        children = (item or {}).get("children") or []
        if children:
            lines.extend(render_outline_for_prompt(children, number))
    return lines


def strip_title_prefix(title: Any) -> str:
    """剥掉 AI 输出目录里可能带的章节编号前缀。"""
    return _TITLE_PREFIX_RE.sub("", str(title or "").strip())
