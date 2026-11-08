"""收编批次 4（BUG-FIX-PROMPT-E10，2026-11-03）：知识补漏 user 侧 + 可研模块提示词。

背景
----
第九轮审计（f4 / B3c）确认的未注册提示词聚集点：

- ``services/ai/knowledge_refill.py`` 两个 **user 侧** 提示词为调用点内联
  f-string（KB_REFILL_EXTRACT_USER / KB_REFILL_GAP_USER），绕过注册表
  （管理页看不到、无法覆盖），与其 system 侧（KB_REFILL_EXTRACT_SYSTEM，
  批次 1 已收编）不对称。
- ``services/feasibility_prompts.py`` 的 14 个 ``FEASIBILITY_*`` 提示词——
  可研模块独立体系，全部在注册表之外：管理页不可见、不可覆盖、不可版本化、
  漂移检测失效。

零行为变更原则
--------------
所有模板内容与原实现（fallback 常量 / 内联 f-string）**逐字一致**；差异仅在
于把动态段抽成 ``{变量}`` 占位符（``{segment_index}``/``{total_segments}``/
``{fields_json}``、``{outline_template_md}``/``{knowledge_rule}``、
``{batch}``、``{existing_titles_text}``/``{section_titles_text}``/``{bid_source}``），
由调用点经 ``render_prompt`` 注入原值，渲染输出与收编前逐字节一致。

12 个静态 ``_FB_*`` 常量直接**导入**自 ``feasibility_prompts``（而非内联复制），
以构造性保证「fallback 常量 == 注册表正本」，杜绝手工复制漂移；
``tests/test_prompts_integrate_batch4_2026_11_03.py`` 对每一处做逐字等价断言。

注：本文件与 ``_integrate_batch2/3.py`` 同属收编批次，由 ``prompts/__init__.py``
导入以触发注册。仅依赖 ``. _registry`` 的 ``_reg`` 与
``app.services.feasibility_prompts`` 的 ``_FB_*`` 常量（后者模块级不导入
prompts，无导入环）。
"""

from ._registry import _reg

# 12 个静态可研提示词 fallback（逐字正本在 feasibility_prompts.py；此处导入
# 而非复制，保证「fallback == 注册表」构造性成立）。
from app.services.feasibility_prompts import (  # noqa: E402
    _FB_ANALYSIS_MERGE_SYSTEM,
    _FB_ANALYSIS_MERGE_USER,
    _FB_ANALYSIS_SYSTEM,
    _FB_CONTENT_SYSTEM,
    _FB_CONTENT_WRITING_RULES,
    _FB_HUMAN_WRITING_SYSTEM,
    _FB_KB_BACKGROUND_NOTE,
    _FB_KB_CATALOG_NOTE,
    _FB_KB_MATERIAL_NOTE,
    _FB_OUTLINE_SYSTEM,
    _FB_PARAMETERS_SYSTEM,
    _FB_PARAMETERS_USER,
)


# ──────────────────────────────────────────────────────────────────────
# 1) 可研模块静态提示词（12 个，fallback 常量 → 注册表正本）
# ──────────────────────────────────────────────────────────────────────
FEASIBILITY_ANALYSIS_SYSTEM = _reg(
    "FEASIBILITY_ANALYSIS_SYSTEM",
    "可研报告",
    "资料分析（系统提示词）",
    _FB_ANALYSIS_SYSTEM,
)
FEASIBILITY_ANALYSIS_MERGE_SYSTEM = _reg(
    "FEASIBILITY_ANALYSIS_MERGE_SYSTEM",
    "可研报告",
    "资料合并（系统提示词）",
    _FB_ANALYSIS_MERGE_SYSTEM,
)
FEASIBILITY_ANALYSIS_MERGE_USER = _reg(
    "FEASIBILITY_ANALYSIS_MERGE_USER",
    "可研报告",
    "资料合并（用户指令）",
    _FB_ANALYSIS_MERGE_USER,
)
FEASIBILITY_OUTLINE_SYSTEM = _reg(
    "FEASIBILITY_OUTLINE_SYSTEM",
    "可研报告",
    "目录生成（系统提示词）",
    _FB_OUTLINE_SYSTEM,
)
FEASIBILITY_PARAMETERS_SYSTEM = _reg(
    "FEASIBILITY_PARAMETERS_SYSTEM",
    "可研报告",
    "关键参数审校（系统提示词）",
    _FB_PARAMETERS_SYSTEM,
)
FEASIBILITY_PARAMETERS_USER = _reg(
    "FEASIBILITY_PARAMETERS_USER",
    "可研报告",
    "关键参数审校（用户指令）",
    _FB_PARAMETERS_USER,
)
FEASIBILITY_CONTENT_SYSTEM = _reg(
    "FEASIBILITY_CONTENT_SYSTEM",
    "可研报告",
    "正文生成（系统提示词）",
    _FB_CONTENT_SYSTEM,
)
FEASIBILITY_CONTENT_WRITING_RULES = _reg(
    "FEASIBILITY_CONTENT_WRITING_RULES",
    "可研报告",
    "正文写作规则",
    _FB_CONTENT_WRITING_RULES,
)
FEASIBILITY_KB_BACKGROUND_NOTE = _reg(
    "FEASIBILITY_KB_BACKGROUND_NOTE",
    "可研报告",
    "知识库背景约束",
    _FB_KB_BACKGROUND_NOTE,
)
FEASIBILITY_KB_CATALOG_NOTE = _reg(
    "FEASIBILITY_KB_CATALOG_NOTE",
    "可研报告",
    "知识库目录约束",
    _FB_KB_CATALOG_NOTE,
)
FEASIBILITY_KB_MATERIAL_NOTE = _reg(
    "FEASIBILITY_KB_MATERIAL_NOTE",
    "可研报告",
    "知识库素材约束",
    _FB_KB_MATERIAL_NOTE,
)
FEASIBILITY_HUMAN_WRITING_SYSTEM = _reg(
    "FEASIBILITY_HUMAN_WRITING_SYSTEM",
    "可研报告",
    "人工化润色（系统提示词）",
    _FB_HUMAN_WRITING_SYSTEM,
)


# ──────────────────────────────────────────────────────────────────────
# 2) 可研模块动态提示词（2 个，模板与 fallback f-string 逐字一致，仅动态段
#    抽成 {变量} 占位符）
# ──────────────────────────────────────────────────────────────────────
FEASIBILITY_ANALYSIS_USER = """请把当前资料中可用于编制可行性研究报告的信息整理为 JSON。

要求：
1. project_overview：项目名称、单位、地点、性质、规模、建设内容、工期等。
2. background_and_necessity：背景、问题、规划政策关系和建设必要性。
3. demand_and_output：需求对象、现状缺口、建设规模依据和预期产出。
4. site_and_conditions：选址、用地、交通、市政、资源、审批和建设条件。
5. construction_and_technical_conditions：技术路线、工程、设备、数字化和建设管理资料。
6. operation_conditions：运营模式、组织、安全、绩效和运维资料。
7. investment_and_financing：投资、费用、资金来源、收益、成本和融资资料。
8. impact_and_risks：经济、社会、生态、能源、碳排放和风险资料。
9. missing_information：只列出本段明显缺失、矛盾或需要用户确认的关键资料；不要把当前分段未出现但可能存在于其他分段的内容武断判定为缺失。
10. 每个字段输出 Markdown 文本；没有信息时输出空字符串。只返回 JSON。

当前资料分段：{segment_index}/{total_segments}

JSON 格式：
{fields_json}"""

FEASIBILITY_ANALYSIS_USER = _reg(
    "FEASIBILITY_ANALYSIS_USER",
    "可研报告",
    "资料分析（用户指令）",
    FEASIBILITY_ANALYSIS_USER,
)

FEASIBILITY_OUTLINE_USER = """请基于以下材料，生成可行性研究报告目录，只返回 JSON。

【项目基础参数】
（见上一条消息）

【大纲框架】
{outline_template_md}

【输出格式】
返回 JSON 对象：{"outline": [{"title": "章节标题", "description": "本节写作重点", "children": [...]}]}

规则：
1. 目录最多三级；title 只写纯标题，不要带章节编号或 Markdown 标记。
2. description 写明本节应论证的重点、已知资料和缺失资料处理要求。
3. 只能输出 title/description/children 三类字段，不要输出正文，不要编造项目事实。
4. 一级目录保留大纲主框架，可按项目明显不适用的内容合并调整，但不得遗漏结论、风险、影响和投资相关章节。
5. 二、三级目录必须结合项目资料具体化，大纲中的二级标题是细化起点而非参考清单。
{knowledge_rule}"""

FEASIBILITY_OUTLINE_USER = _reg(
    "FEASIBILITY_OUTLINE_USER",
    "可研报告",
    "目录生成（用户指令）",
    FEASIBILITY_OUTLINE_USER,
)


# ──────────────────────────────────────────────────────────────────────
# 3) 知识补漏 user 侧（2 个，与 knowledge_refill.py 内联 f-string 逐字一致）
# ──────────────────────────────────────────────────────────────────────
KB_REFILL_EXTRACT_USER = """请提取以下招标文件内容中的关键知识条目：

{batch}"""

KB_REFILL_EXTRACT_USER = _reg(
    "KB_REFILL_EXTRACT_USER",
    "知识库",
    "知识补漏·第一轮提取（用户指令）",
    KB_REFILL_EXTRACT_USER,
)

KB_REFILL_GAP_USER = """请检查招标文件中是否有以下目录章节涉及但未提取的关键信息。

已有知识条目：
{existing_titles_text}

目录章节：
{section_titles_text}

招标文件正文（取材来源，只允许从中提取、禁止编造）：
{bid_source}

请从上述招标文件正文中提取与这些章节相关、但尚未包含在已有知识条目中的关键信息。

输出格式：JSON 数组，每个元素包含：
- title: 知识条目标题
- content: 知识条目详细内容
- category: 分类
- keywords: 关键词数组

只返回 JSON 数组，不要包含任何解释或 markdown 围栏。"""

KB_REFILL_GAP_USER = _reg(
    "KB_REFILL_GAP_USER",
    "知识库",
    "知识补漏·第二轮查漏（用户指令）",
    KB_REFILL_GAP_USER,
)
