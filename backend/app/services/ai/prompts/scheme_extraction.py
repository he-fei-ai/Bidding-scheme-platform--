# -*- coding: utf-8 -*-
"""专项方案解析提取提示词（标书智能体（一）五段式方法引入）。

将《标书智能体（一）——AI解析招标文件代码+提示词》的「五段式结构化提示词方法」
（目标定位 → 提取内容要求 → 处理规则 → 输出示例 → 验证步骤）引入本软件，并适配
"建筑工程专项施工方案编写"定位，替代原招投标场景的"项目概述 / 技术评分要求"提取。

原文档对应映射：
    - 项目概述提取      → 工程概况提取（PROJECT_OVERVIEW）
    - 技术评分要求提取  → 拆为四个专项任务：
        · 专项方案类型识别（SCHEME_TYPE）
        · 危大工程判定参数提取（DANGEROUS_WORK_PARAMS）
        · 编制依据清单提取（BASIS_LIST）
        · 施工条件提取（CONSTRUCTION_CONDITION）

全部提示词复用 analysis 模块已确立的「缓存友好」结构：通用 system 前缀
（ANALYSIS_SYSTEM）+ 文档全文独占 user 块（ANALYSIS_CONTEXT_BLOCK），任务差异仅放在
最后的 user 消息。因此本模块只定义"任务专属指令（*_SYSTEM）"与"任务输入（*_USER）"，
不重复携带全文，保证同一份文件多次提取时全文前缀命中 Prompt Cache。
"""

from ._registry import _reg

# 统一角色前缀：所有专项提取任务共享。
_ROLE = (
    "你是一名专业的建筑工程专项施工方案分析专家。请严格基于上方提供的文档原文"
    "（招标文件、施工图、勘察报告、施工组织设计、专项方案原稿等）完成提取任务。\n"
    "通用要求：\n"
    "1. 保持提取信息的全面性和准确性，尽量使用原文内容，不要自行编造；\n"
    "2. 某项信息缺失时填\"未提及\"，绝不凭空补充；\n"
    "3. 只输出最终结构化结果，不要输出额外说明、过程或客套话。"
)


def build_five_part_prompt(
    *,
    goal: "str | list[str]",
    fields: "str | list[str]",
    rules: "str | list[str]",
    examples: "str | list[str]",
    self_check: "str | list[str]",
    output_hint: str = "",
) -> str:
    """按「标书智能体（一）」引入的五段式结构化提示词模板组装系统提示词。

    五段式：1. 目标定位  2. 提取内容要求  3. 处理规则  4. 输出示例  5. 验证步骤。
    所有提取任务统一采用此结构，让模型在输出前自检，提高提取质量。

    Args:
        goal / fields / rules / examples / self_check: 各段内容，字符串或条目列表。
        output_hint: 额外的输出格式说明（如 JSON 字段约定）。
    """
    def _block(x) -> str:
        if isinstance(x, str):
            return x.strip()
        return "\n".join(f"{i + 1}. {item}" for i, item in enumerate(x))

    parts = [
        "### 1. 目标定位",
        _block(goal),
        "",
        "### 2. 提取内容要求",
        _block(fields),
        "",
        "### 3. 处理规则",
        _block(rules),
        "",
        "### 4. 输出示例",
        _block(examples),
        "",
        "### 5. 验证步骤",
        "提取完成后，逐项执行以下自检（自检通过后才返回结果，自检过程与结论不需要输出）：",
        _block(self_check),
    ]
    if output_hint:
        parts.append("")
        parts.append("### 输出格式")
        parts.append(output_hint.strip())
    return "\n".join(parts)


# ============================================================================
# 一、工程概况提取（替代"项目概述提取"）
# ============================================================================

PROJECT_OVERVIEW_SYSTEM = _ROLE + "\n\n" + build_five_part_prompt(
    goal=[
        "从文档中识别并提取工程概况相关章节：工程名称、工程地点、建设规模、结构形式、"
        "专项工程特征参数、地质与水文条件、周边环境、气候特征、参建单位等。",
        "忽略商务报价、合同条款、资质业绩等非技术实施内容。",
    ],
    fields=[
        "以 JSON 数组返回，每个字段一项，结构：",
        '  {"field": "字段名", "value": "提取值（尽量用原文）", "unit": "单位（无则空串）", "source": "数据来源（如第N页/章节X.Y）"}',
        "字段名范围：工程名称、工程地点、建设规模、结构形式、专项工程特征参数"
        "（基坑深度/模板高度与跨度/荷载/脚手架类型与高度/吊装参数等）、地质条件、"
        "水文条件、周边环境、气候特征、参建单位。",
    ],
    rules=[
        "数值必须保留原文单位（如「6.5m」「C35」「800kN·m」），不擅自换算；",
        "信息缺失填\"未提及\"，不推断、不编造；",
        "表格数据按行提取，并在 source 中标注「[表格数据]」；",
        "分层信息（如多栋楼、多剖面）用多条记录或层级体现。",
    ],
    examples=[
        '[{"field":"工程名称","value":"XX商业综合体基坑工程","unit":"","source":"封面"},'
        '{"field":"专项工程特征参数","value":"基坑开挖深度 6.5m，支护形式为钻孔灌注桩+内支撑","unit":"m","source":"第3.2节"}]',
        '[{"field":"工程名称","value":"XX高层脚手架工程","unit":"","source":"封面"},'
        '{"field":"专项工程特征参数","value":"落地式钢管脚手架搭设高度 48m","unit":"m","source":"第4.1节"}]',
    ],
    self_check=[
        "是否覆盖工程概况所需的全部字段（名称/地点/规模/结构/专项参数/地质/水文/周边/气候/参建单位）？",
        "所有数值是否都带单位？",
        "信息缺失项是否标注\"未提及\"而非编造？",
        "表格数据是否标注了来源？",
    ],
    output_hint='直接返回 JSON 数组，严禁附加解释或代码围栏。',
)

PROJECT_OVERVIEW_SYSTEM = _reg(
    "PROJECT_OVERVIEW_SYSTEM", "文件解析", "工程概况提取（系统提示词，五段式）", PROJECT_OVERVIEW_SYSTEM
)

PROJECT_OVERVIEW_USER = """请分析上方提供的文档全文，按上述要求提取工程概况信息。

请直接以 JSON 数组格式返回，每项包含 field / value / unit / source 字段。
若某项信息缺失，value 填"未提及"。不要输出任何额外说明或代码围栏。"""

PROJECT_OVERVIEW_USER = _reg(
    "PROJECT_OVERVIEW_USER", "文件解析", "工程概况提取（用户提示词）", PROJECT_OVERVIEW_USER
)


# ============================================================================
# 二、专项方案类型识别（替代"技术评分要求提取"之一）
# ============================================================================

SCHEME_TYPE_SYSTEM = _ROLE + "\n\n" + build_five_part_prompt(
    goal=[
        "从文档中识别专项工程描述、施工内容、工序、工艺与对象，判断其对应的专项方案类型。",
        "专项方案类型按六大类危大工程分类匹配：基坑工程 / 模板工程及支撑体系 / 脚手架工程 / "
        "起重吊装及安装拆卸工程 / 拆除工程 / 其他。",
    ],
    fields=[
        "以 JSON 数组返回，每项结构：",
        '  {"scheme_type": "方案类型", "main_content": "主要施工内容",'
        ' "process": "关键工序", "craft": "主要工艺", "object": "施工对象",'
        ' "confidence": "高/中/低"}',
        "scheme_type 必须从六大类中取值，无法确定时填\"待确认\"。",
    ],
    rules=[
        "方案类型必须与文档描述的施工内容一致；",
        "六大类之外或证据不足时标\"待确认\"，不强行归类；",
        "同一文档涉及多类专项工程时返回多条。",
    ],
    examples=[
        '[{"scheme_type":"基坑工程","main_content":"深基坑开挖与支护","process":"降水→开挖→支护","craft":"钻孔灌注桩+内支撑","object":"地下三层车库基坑","confidence":"高"}]',
        '[{"scheme_type":"模板工程及支撑体系","main_content":"高大模板支撑","process":"搭设→验收→浇筑","craft":"扣件式钢管满堂架","object":"中庭高支模区域","confidence":"高"}]',
    ],
    self_check=[
        "识别出的 scheme_type 是否与文档描述的施工内容一致？",
        "证据不足的项是否标为\"待确认\"而非强行归类？",
        "是否遗漏文档中明确涉及的其它专项工程？",
    ],
    output_hint='直接返回 JSON 数组，严禁附加解释或代码围栏。',
)

SCHEME_TYPE_SYSTEM = _reg(
    "SCHEME_TYPE_SYSTEM", "文件解析", "专项方案类型识别（系统提示词，五段式）", SCHEME_TYPE_SYSTEM
)

SCHEME_TYPE_USER = """请分析上方提供的文档全文，按上述要求识别专项方案类型。

请直接以 JSON 数组格式返回，每项包含 scheme_type / main_content / process /
craft / object / confidence 字段。不要输出任何额外说明或代码围栏。"""

SCHEME_TYPE_USER = _reg(
    "SCHEME_TYPE_USER", "文件解析", "专项方案类型识别（用户提示词）", SCHEME_TYPE_USER
)


# ============================================================================
# 三、危大工程判定参数提取（替代"技术评分要求提取"之二）
# ============================================================================

DANGEROUS_WORK_PARAMS_SYSTEM = _ROLE + "\n\n" + build_five_part_prompt(
    goal=[
        "从文档中识别与危大工程判定相关的数值章节：开挖深度、支撑高度、跨度、施工总荷载、"
        "集中线荷载、起吊重量、脚手架搭设高度、幕墙施工高度、钢结构跨度、人工挖孔桩深度。",
        "这些参数用于判断是否属于「超过一定规模的危险性较大的分部分项工程」及是否需要专家论证。",
    ],
    fields=[
        "以 JSON 数组返回，每项结构：",
        '  {"param": "参数名", "value": "数值", "unit": "单位",'
        ' "threshold": "是否超过一定规模及阈值（如「超过一定规模（≥5m）」）",'
        ' "source": "数据来源"}',
        "参数名范围见目标定位列举的十类。",
    ],
    rules=[
        "数值必须带单位，缺失标\"未提及\"，绝不推断；",
        "明确标注该参数是否超过判定阈值（依据住建部危大工程管理规定）；",
        "表格数据按行提取并标注来源。",
    ],
    examples=[
        '[{"param":"开挖深度","value":"6.5","unit":"m","threshold":"超过一定规模（≥5m）","source":"第3.2节"}]',
        '[{"param":"脚手架搭设高度","value":"48","unit":"m","threshold":"超过一定规模（≥50m 之外，属一般危大）","source":"第4.1节"}]',
    ],
    self_check=[
        "是否覆盖六大类危大工程判定所需的全部参数（开挖深度/支撑高度/跨度/荷载/起吊重量/搭设高度等）？",
        "所有数值是否都带单位？",
        "是否标注了是否超过判定阈值？",
        "信息缺失项是否标\"未提及\"而非编造？",
    ],
    output_hint='直接返回 JSON 数组，严禁附加解释或代码围栏。',
)

DANGEROUS_WORK_PARAMS_SYSTEM = _reg(
    "DANGEROUS_WORK_PARAMS_SYSTEM",
    "文件解析",
    "危大工程判定参数提取（系统提示词，五段式）",
    DANGEROUS_WORK_PARAMS_SYSTEM,
)

DANGEROUS_WORK_PARAMS_USER = """请分析上方提供的文档全文，按上述要求提取危大工程判定参数。

请直接以 JSON 数组格式返回，每项包含 param / value / unit / threshold / source 字段。
数值必须带单位，缺失标"未提及"。不要输出任何额外说明或代码围栏。"""

DANGEROUS_WORK_PARAMS_USER = _reg(
    "DANGEROUS_WORK_PARAMS_USER",
    "文件解析",
    "危大工程判定参数提取（用户提示词）",
    DANGEROUS_WORK_PARAMS_USER,
)


# ============================================================================
# 四、编制依据清单提取
# ============================================================================

BASIS_LIST_SYSTEM = _ROLE + "\n\n" + build_five_part_prompt(
    goal=[
        "从文档中识别编制依据相关章节：法律法规、标准规范、施工图、施工组织设计、"
        "勘察报告、合同等。",
        "这些依据将作为专项方案「编制依据」章节的引用清单。",
    ],
    fields=[
        "以 JSON 数组返回，每项结构：",
        '  {"category": "类别", "name": "名称", "code": "编号", "source": "数据来源"}',
        "category 取值：法规 / 标准 / 施工图 / 施组 / 勘察报告 / 合同。",
    ],
    rules=[
        "标准编号保留原文，不补全、不简写（如「GB 55023-2022」不得写成「GB55023」）；",
        "法规与标准分开列出，不要合并；",
        "编号缺失时 code 填\"未提及\"。",
    ],
    examples=[
        '[{"category":"标准","name":"施工脚手架通用规范","code":"GB 55023-2022","source":"第2章 编制依据"}]',
        '[{"category":"施工图","name":"基坑支护施工图","code":"建施-结-03","source":"图纸目录"}]',
    ],
    self_check=[
        "是否覆盖编制依据所需的全部类别（法规/标准/施工图/施组/勘察报告/合同）？",
        "标准编号是否保留原文（未补全、未简写）？",
        "编号缺失项是否标\"未提及\"？",
    ],
    output_hint='直接返回 JSON 数组，严禁附加解释或代码围栏。',
)

BASIS_LIST_SYSTEM = _reg(
    "BASIS_LIST_SYSTEM", "文件解析", "编制依据清单提取（系统提示词，五段式）", BASIS_LIST_SYSTEM
)

BASIS_LIST_USER = """请分析上方提供的文档全文，按上述要求提取编制依据清单。

请直接以 JSON 数组格式返回，每项包含 category / name / code / source 字段。
标准编号保留原文，缺失标"未提及"。不要输出任何额外说明或代码围栏。"""

BASIS_LIST_USER = _reg(
    "BASIS_LIST_USER", "文件解析", "编制依据清单提取（用户提示词）", BASIS_LIST_USER
)


# ============================================================================
# 五、施工条件提取
# ============================================================================

CONSTRUCTION_CONDITION_SYSTEM = _ROLE + "\n\n" + build_five_part_prompt(
    goal=[
        "从文档中识别施工条件相关章节：场地条件、交通条件、水电条件、周边建筑、地下管线等。",
        "这些信息用于施工组织设计中场地布置、交通组织、管线保护等措施的编制。",
    ],
    fields=[
        "以 JSON 数组返回，每项结构：",
        '  {"category": "类别", "description": "描述", "value": "关键数值",'
        ' "unit": "单位", "source": "数据来源"}',
        "category 取值：场地 / 交通 / 水电 / 周边建筑 / 地下管线。",
    ],
    rules=[
        "描述性内容保留原文关键信息，数值必须带单位；",
        "周边建筑需记录距离与高度，地下管线需记录类型与埋深；",
        "信息缺失标\"未提及\"。",
    ],
    examples=[
        '[{"category":"周边建筑","description":"东侧距用地红线 8m，有一栋 6 层砖混住宅","value":"8","unit":"m","source":"第1.3节"}]',
        '[{"category":"地下管线","description":"场区西北角有 DN300 给水管，埋深约 1.2m","value":"1.2","unit":"m","source":"管线交底记录"}]',
    ],
    self_check=[
        "是否覆盖施工组织所需的关键条件（场地/交通/水电/周边建筑/地下管线）？",
        "数值是否都带单位？",
        "周边建筑的距离高度、管线的类型埋深是否完整？",
        "缺失项是否标\"未提及\"？",
    ],
    output_hint='直接返回 JSON 数组，严禁附加解释或代码围栏。',
)

CONSTRUCTION_CONDITION_SYSTEM = _reg(
    "CONSTRUCTION_CONDITION_SYSTEM",
    "文件解析",
    "施工条件提取（系统提示词，五段式）",
    CONSTRUCTION_CONDITION_SYSTEM,
)

CONSTRUCTION_CONDITION_USER = """请分析上方提供的文档全文，按上述要求提取施工条件信息。

请直接以 JSON 数组格式返回，每项包含 category / description / value / unit / source 字段。
数值带单位，缺失标"未提及"。不要输出任何额外说明或代码围栏。"""

CONSTRUCTION_CONDITION_USER = _reg(
    "CONSTRUCTION_CONDITION_USER",
    "文件解析",
    "施工条件提取（用户提示词）",
    CONSTRUCTION_CONDITION_USER,
)


# 五个提取任务的（系统提示词名, 用户提示词名）映射，供 runner 统一调度。
SCHEME_EXTRACTION_TASKS = {
    "project_overview": ("PROJECT_OVERVIEW_SYSTEM", "PROJECT_OVERVIEW_USER"),
    "scheme_type": ("SCHEME_TYPE_SYSTEM", "SCHEME_TYPE_USER"),
    "dangerous_work_params": ("DANGEROUS_WORK_PARAMS_SYSTEM", "DANGEROUS_WORK_PARAMS_USER"),
    "basis_list": ("BASIS_LIST_SYSTEM", "BASIS_LIST_USER"),
    "construction_condition": ("CONSTRUCTION_CONDITION_SYSTEM", "CONSTRUCTION_CONDITION_USER"),
}
