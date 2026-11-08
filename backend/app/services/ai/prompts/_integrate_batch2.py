"""收编批次 2（2026-10-17 审计遗留 D2）：图表 / 配图 / 内容生成提示词。

背景
----
第八轮审计（D2，P0）确认：``image_engine.py`` 存在 14 处三引号 f-string 形式的
``user_prompt`` 硬编码，虽其 system 侧已注册表化，但 **user 侧完全绕过注册表**，后果：

1. 管理页不可见、不可覆盖、不可版本化、漂移检测失效；
2. **安全过滤强度不一致**（本批修复的重点）：
   - 组 A（7 处编排判断）**裸插值** `section_content` / `project_overview`，
     既无 `sanitize_prompt_value` 也无 `sanitize=True`；
   - 组 B（3 处生成）用了 `sanitize_prompt_value`；
   - 组 B′（2 处生成）前三个字段 sanitize 了，但 `industry_profile` / `writing_length`
     仍**裸插值**。
   同一数据流上三套过滤口径，是典型的「跨模块调用不一致」债务。
3. 12 处模板结构**逐字重复**（仅末句图表名词不同），改一处忘其余必然漂移。

本文件的收编原则（**零行为变更**）
--------------------------------
所有模板内容与原 f-string **逐字一致**；差异仅在于：

- 原地内联的兜底文案（``{project_overview or "未提供项目概述"}``）上移到调用点，
  渲染结果完全相同；
- 末句图表名词抽成变量（``{CHART_LABEL}``），由调用点传入原值；
- 组 A / B′ 的裸插值统一加 ``sanitize=True``（与组 B、workflows_analysis 的
  既有安全口径对齐，属**修复**而非行为漂移——元标记中和只影响
  ``{shared:NAME}`` / ``__VAR__`` 这类模块自有元语法，正常招标正文零影响）。

`tests/test_prompts_adoption_2026_10_18.py::TestRenderEquivalence` 对每一处
收编点做「注册表渲染结果 == 原 f-string 结果」的等价断言，确保重构零回归。
"""

from ._registry import _reg


# ──────────────────────────────────────────────────────────────────────
# 1) 组 A：图表编排判断（user 侧）—— 6 处共用，末句仅图表名词不同
# ──────────────────────────────────────────────────────────────────────
CHART_ARRANGE_USER = """章节标题：{section_title}

章节内容：
{section_content}

项目概述：
{project_overview}

评分标准：
{scoring_standard}

行业背景：
{industry_profile}

请根据以上信息判断该章节是否需要生成{CHART_LABEL}，并返回JSON格式的判断结果。"""

CHART_ARRANGE_USER = _reg(
    "CHART_ARRANGE_USER",
    "配图",
    "图表编排判断（用户提示词，通用）",
    CHART_ARRANGE_USER,
)


# 组 A 的加长变体：配图插图（末句额外列出图像风格枚举）
# 2026-11-04（ILLUSTRATION-REMOVAL）：人工配图/生图端点已下线，本 key 已无任何
# get_prompt 注入点，按 db.py::seed_prompt_templates 的 _deprecated_prompt_keys
# 口径不再注册（保留常量仅维持 __all__ 与向后导入兼容，避免破坏 from . import *）。
ILLUSTRATION_ARRANGE_USER = """章节标题：{section_title}

章节内容：
{section_content}

项目概述：
{project_overview}

评分标准：
{scoring_standard}

行业背景：
{industry_profile}

请根据以上信息判断该章节是否需要配图插图，如果需要，建议使用的图像风格（engineering_diagram/realistic_photo/technical_drawing/custom），并说明理由。返回JSON格式的判断结果。"""


# ──────────────────────────────────────────────────────────────────────
# 2) 组 B：Mermaid 图生成（user 侧）—— 3 处共用
# ──────────────────────────────────────────────────────────────────────
CHART_MERMAID_GEN_USER = """章节标题：{safe_title}

章节内容：
{safe_content}

项目概述：
{safe_overview}

编排结果：
{arrange_result}

请根据以上信息生成Mermaid{CHART_KIND}代码。"""

CHART_MERMAID_GEN_USER = _reg(
    "CHART_MERMAID_GEN_USER",
    "配图",
    "Mermaid 图生成（用户提示词，通用）",
    CHART_MERMAID_GEN_USER,
)


# ──────────────────────────────────────────────────────────────────────
# 3) 组 B′：结构化图表数据生成（user 侧）—— 2 处共用
#    比组 B 多「写作长度」「行业背景」两段
# ──────────────────────────────────────────────────────────────────────
CHART_STRUCT_DATA_GEN_USER = """章节标题：{safe_title}

章节内容：
{safe_content}

项目概述：
{safe_overview}

编排结果：
{arrange_result}

写作长度：{writing_length}

行业背景：
{industry_profile}

请根据以上信息生成{SCHEMA}格式的{CHART_DESC}。"""

CHART_STRUCT_DATA_GEN_USER = _reg(
    "CHART_STRUCT_DATA_GEN_USER",
    "配图",
    "结构化图表数据生成（用户提示词，通用）",
    CHART_STRUCT_DATA_GEN_USER,
)


# ──────────────────────────────────────────────────────────────────────
# 4) 图表数据降级修复（user 侧，原 image_engine.py:592）
# ──────────────────────────────────────────────────────────────────────
# 注意：原文里 {_repair_prompt} 与「原始数据：」之间**没有换行**（修复上下文块
# 自带结尾换行），此处逐字保持不变，避免改变发给模型的字节序列。
CHART_DATA_FIX_USER = """图表类型：{chart_type}
{mismatch_warning}
章节标题：{section_title}

章节内容：
{section_content}

错误信息：
{error_message}
{repair_context}原始数据：
{original_data}

请根据以上信息生成降级可用的图表数据。"""

CHART_DATA_FIX_USER = _reg(
    "CHART_DATA_FIX_USER",
    "配图",
    "图表数据降级修复（用户提示词）",
    CHART_DATA_FIX_USER,
)

# 修复上下文块（机械化修复诊断信息，原 image_engine.py:568-577）
CHART_REPAIR_CONTEXT_BLOCK = """
[机械化修复上下文 v{repair_version}
  repair_mermaid 触发: {repair_triggered}
  已执行修复步骤: {repair_steps}
  步骤触发次数: {step_counts}
  结构化校验错误: {struct_errors}
  修复前代码片段: {before_snippet}
  修复后代码片段: {after_snippet}{parsed_summary}

"""

CHART_REPAIR_CONTEXT_BLOCK = _reg(
    "CHART_REPAIR_CONTEXT_BLOCK",
    "配图",
    "图表机械化修复上下文块",
    CHART_REPAIR_CONTEXT_BLOCK,
)

# 类型错配警告（原 image_engine.py:588）
CHART_TYPE_MISMATCH_WARNING = (
    "\n⚠️ 历史上 {chart_type} 类型曾多次（≥{threshold}次）被误输出为 {types_to_avoid} 格式，"
    "请务必输出正确的 {chart_type} 格式，不要混淆！\n"
)

CHART_TYPE_MISMATCH_WARNING = _reg(
    "CHART_TYPE_MISMATCH_WARNING",
    "配图",
    "图表类型错配警告（模板）",
    CHART_TYPE_MISMATCH_WARNING,
)


# ──────────────────────────────────────────────────────────────────────
# 5) 配图方案规划（user 侧，原 image_engine.py:1058）
# ──────────────────────────────────────────────────────────────────────
# 2026-11-04（ILLUSTRATION-REMOVAL）：人工配图/生图端点已下线，本 key 已无任何
# get_prompt 注入点，按 db.py::seed_prompt_templates 的 _deprecated_prompt_keys
# 口径不再注册（保留常量仅维持 __all__ 与向后导入兼容）。
ILLUSTRATION_PLAN_USER = """请为以下章节规划一张配图方案：

## 章节路径
{section_path}

## 章节标题
{section_title}

## 章节正文（摘要）
{section_content}

## 行业背景
{industry_profile}

请严格按照系统提示词中的 JSON 格式输出。"""


# ──────────────────────────────────────────────────────────────────────
# 6) Mermaid 修复（user 侧，原 image_engine.py:937 / 1021）
# ──────────────────────────────────────────────────────────────────────
MERMAID_REPAIR_USER = """原始 Mermaid 代码：
{raw_content}

校验问题：{error_message}"""

MERMAID_REPAIR_USER = _reg(
    "MERMAID_REPAIR_USER",
    "配图",
    "Mermaid 修复（用户提示词）",
    MERMAID_REPAIR_USER,
)


# ──────────────────────────────────────────────────────────────────────
# 7) 内联数据图表适配判断（system + user，原 image_engine.py:2131 / 2141）
#    —— 此前 **system 侧也是硬编码**，是本批唯一「两侧都未注册」的点
# ──────────────────────────────────────────────────────────────────────
INLINE_CHART_DETECT_SYSTEM = """你是标书编制专家，擅长判断哪些章节内容适合生成数据图表。

判断标准：
1. 包含多组对比数据（如不同方案的成本对比）→ 适合柱状图/对比图
2. 包含趋势分析（如工期-资源投入曲线）→ 适合折线图
3. 包含比例分布（如资金分配比例）→ 适合饼图
4. 包含结构化数据（如设备清单）→ 适合表格

返回 JSON：{"needed": true/false, "chart_type": "bar/pie/line/table", "data_source": "数据来源说明", "reason": "判断理由"}"""

INLINE_CHART_DETECT_SYSTEM = _reg(
    "INLINE_CHART_DETECT_SYSTEM",
    "配图",
    "内联数据图表适配判断（系统提示词）",
    INLINE_CHART_DETECT_SYSTEM,
)

INLINE_CHART_DETECT_USER = """章节标题：{section_title}
章节描述：{section_desc}
项目事实：{facts_json}

请判断该章节是否适合生成内联数据图表，返回JSON格式的判断结果。"""

INLINE_CHART_DETECT_USER = _reg(
    "INLINE_CHART_DETECT_USER",
    "配图",
    "内联数据图表适配判断（用户提示词）",
    INLINE_CHART_DETECT_USER,
)


# ──────────────────────────────────────────────────────────────────────
# 8) 正文骨架规划（user 侧，原 workflows_orchestration.py:812）
#    system 侧已于 BUG-FIX-PROMPT-E3 收编为 CONTENT_SKELETON_SYSTEM
# ──────────────────────────────────────────────────────────────────────
CONTENT_SKELETON_USER = """### 项目概述
{project_overview}

### 需要写骨架的章节（section_id<TAB>标题）
{node_listing}

请为以上每个 section_id 输出一篇 1~2 句的篇章骨架，返回严格 JSON。"""

CONTENT_SKELETON_USER = _reg(
    "CONTENT_SKELETON_USER",
    "正文生成",
    "篇章骨架规划（用户提示词）",
    CONTENT_SKELETON_USER,
)


# ──────────────────────────────────────────────────────────────────────
# 9) 内容质量评估（user 侧，原 workflows_quality.py:242）
#    system 侧已收编为 QUALITY_EVAL_SYSTEM
#    注意：原 f-string 用 {{ }} 转义输出字面 JSON 花括号，注册表里必须写**单花括号**
# ──────────────────────────────────────────────────────────────────────
QUALITY_EVAL_USER = """请评估以下标书章节的内容质量，从以下几个维度打分（0-100分）：

【章节标题】
{section_title}

【评估维度】
1. 结构完整性（structure_score）：是否有清晰的段落划分、逻辑层次是否分明
2. 专业性（professional_score）：是否使用建筑行业术语、是否符合标书专业风格
3. 相关性（relevance_score）：内容是否紧扣章节主题、是否有跑题或泛泛而谈

【评估要求】
- 结构完整性：检查是否有引言、主体、结论；段落之间是否有逻辑过渡；是否使用了合适的小标题
- 专业性：检查是否使用专业术语（如"混凝土浇筑"、"土方开挖"等）；是否符合工程技术规范
- 相关性：检查内容是否围绕章节主题展开；是否有无关内容

【输出格式】
请直接返回JSON对象，不要包含其他文字：
{
    "structure_score": 85.5,
    "professional_score": 78.0,
    "relevance_score": 90.0,
    "issues": ["问题1", "问题2"],
    "suggestions": ["建议1", "建议2"]
}

【正文内容】
{content_sample}
"""

QUALITY_EVAL_USER = _reg(
    "QUALITY_EVAL_USER",
    "正文生成",
    "内容质量评估（用户提示词）",
    QUALITY_EVAL_USER,
)


# ──────────────────────────────────────────────────────────────────────
# 10) 目录扩展 / 正文扩写（原 workflows_expand.py 204-216 / 386-398）
#     该文件原本 **完全没有 get_prompt 调用**（grep 命中 0）
# ──────────────────────────────────────────────────────────────────────
OUTLINE_EXPAND_SYSTEM = "你是一个标书目录扩展专家，擅长补充完善目录结构。请直接输出 JSON 数组。"
OUTLINE_EXPAND_SYSTEM = _reg("OUTLINE_EXPAND_SYSTEM", "目录生成", "目录扩展（系统提示词）", OUTLINE_EXPAND_SYSTEM)

OUTLINE_EXPAND_USER = """你是一个标书目录扩展专家。当前目录字数不足，需要补充更多二三级目录来增加内容量。

【补充原则】
1. 只补充二级和三级子目录，不修改一级目录结构
2. 在每个一级目录下补充 1-3 个新的二级目录，每个二级下补充 2-3 个三级目录
3. 补充的目录必须与所属一级章节的主题相关，不得跑题
4. 补充的目录要具体、可操作，不得使用'其他''补充'等占位词
5. 保持三级目录结构，新增三级目录的 children 为空数组

【当前目录】
{outline_json}

【项目概述】
{summary}

【评分点】
{scoring_points}

请直接输出补充后的完整目录 JSON 数组。严禁附加解释文字。"""

OUTLINE_EXPAND_USER = _reg("OUTLINE_EXPAND_USER", "目录生成", "目录扩展（用户提示词）", OUTLINE_EXPAND_USER)

SECTION_EXPAND_SYSTEM = "你是一个标书正文扩写专家，擅长在保留原有内容的基础上扩充正文。"
SECTION_EXPAND_SYSTEM = _reg("SECTION_EXPAND_SYSTEM", "正文生成", "正文扩写（系统提示词）", SECTION_EXPAND_SYSTEM)

SECTION_EXPAND_USER = """你是一个标书正文扩写专家。请扩写以下章节正文内容，使其更充实、专业。

【扩写原则】
1. 在原有内容基础上扩充，不删除原有内容
2. 使用 insert/replace 方式：在段落之间插入新内容，或替换过于简略的段落
3. 扩充内容必须与章节主题相关，不得跑题
4. 保持标书正文的专业风格，使用行业术语
5. 补充具体的技术参数、标准编号、施工方法、管理措施
6. 目标字数：从 {words} 字扩写到 {expansion_target} 字以上

【章节标题】
{section_title}

【当前正文】
{content}

请直接输出扩写后的完整正文，严禁附加解释文字。"""

SECTION_EXPAND_USER = _reg("SECTION_EXPAND_USER", "正文生成", "正文扩写（用户提示词）", SECTION_EXPAND_USER)


# ──────────────────────────────────────────────────────────────────────
# 11) 一致性审计 / 审计修复（原 workflows_expand.py 795-814 / 917-923）
#     system 侧已收编 CONSISTENCY_AGENT_SYSTEM；此处补 user 侧
#     注意：原 f-string 的 {{ }} 需在注册表里写成单花括号
# ──────────────────────────────────────────────────────────────────────
CONSISTENCY_AUDIT_SYSTEM = "你是一个标书一致性审计专家，擅长发现正文中的矛盾和不一致。"
CONSISTENCY_AUDIT_SYSTEM = _reg(
    "CONSISTENCY_AUDIT_SYSTEM", "废标检查", "一致性审计（系统提示词）", CONSISTENCY_AUDIT_SYSTEM
)

CONSISTENCY_AUDIT_USER = """你是一个标书一致性审计专家。请检查以下正文内容是否与项目事实变量存在冲突或不一致。

{facts_block}{completeness_block}【项目事实】
{summary}

【正文内容（第 {group_index}/{total_groups} 组）】
{group_content}

【一致性检查清单】
1. 项目名称、工期、质量标准等关键数据是否与项目事实一致
2. 技术参数、标准编号是否合理
3. 不同章节之间对同一事项的描述是否一致
4. 是否存在明显的矛盾或冲突

{evidence_rule}【输出格式】
返回 JSON 对象: {
  "has_conflicts": true/false,
  "conflicts": [
    {conflict_fields}
  ]
}
如果没有冲突，返回 {"has_conflicts": false, "conflicts": []}
严禁附加解释文字。"""

CONSISTENCY_AUDIT_USER = _reg("CONSISTENCY_AUDIT_USER", "废标检查", "一致性审计（用户提示词）", CONSISTENCY_AUDIT_USER)

CONSISTENCY_REPAIR_SYSTEM = "你是一个标书正文修复专家，请根据审计意见精确修复正文。"
CONSISTENCY_REPAIR_SYSTEM = _reg(
    "CONSISTENCY_REPAIR_SYSTEM", "废标检查", "一致性修复（系统提示词）", CONSISTENCY_REPAIR_SYSTEM
)

CONSISTENCY_REPAIR_USER = """请修复以下正文中的一致性问题。

【问题描述】
{issue}

【修复建议】
{suggestion}

【当前正文】
{current_content}

请直接输出修复后的完整正文，仅修改问题部分，保留其他内容不变。"""

CONSISTENCY_REPAIR_USER = _reg("CONSISTENCY_REPAIR_USER", "废标检查", "一致性修复（用户提示词）", CONSISTENCY_REPAIR_USER)


__all__ = [
    "CHART_ARRANGE_USER",
    "ILLUSTRATION_ARRANGE_USER",
    "CHART_MERMAID_GEN_USER",
    "CHART_STRUCT_DATA_GEN_USER",
    "CHART_DATA_FIX_USER",
    "CHART_REPAIR_CONTEXT_BLOCK",
    "CHART_TYPE_MISMATCH_WARNING",
    "ILLUSTRATION_PLAN_USER",
    "MERMAID_REPAIR_USER",
    "INLINE_CHART_DETECT_SYSTEM",
    "INLINE_CHART_DETECT_USER",
    "CONTENT_SKELETON_USER",
    "QUALITY_EVAL_USER",
    "OUTLINE_EXPAND_SYSTEM",
    "OUTLINE_EXPAND_USER",
    "SECTION_EXPAND_SYSTEM",
    "SECTION_EXPAND_USER",
    "CONSISTENCY_AUDIT_SYSTEM",
    "CONSISTENCY_AUDIT_USER",
    "CONSISTENCY_REPAIR_SYSTEM",
    "CONSISTENCY_REPAIR_USER",
]
