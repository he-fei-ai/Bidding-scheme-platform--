"""收编批次 7（2026-11-08）：目录自修复 + Mermaid 直修 user 侧（3 个提示词）。

收编三处绕过注册表的提示词：

- ``OUTLINE_REPAIR_SYSTEM``：目录审核未通过的自修复 system（原
  workflows_quality.py L933 内联硬编码）。
- ``OUTLINE_REPAIR_USER``：目录自修复 user（原 workflows_quality.py L918-927
  内联 f-string，含 issues/suggestions/当前目录/评分点动态块）。
- ``MERMAID_REPAIR_DIRECT_USER``：Mermaid 直接修复 user（原 mermaid_repair.py
  L143 内联 f-string；区别于 image_engine 已收编的 ``MERMAID_REPAIR_USER``——
  两者文本与变量不同，是独立提示词）。

接线与 fallback 均保持逐字一致；注册表/DB 无行时行为零变化。
"""

from ._registry import _reg

OUTLINE_REPAIR_SYSTEM = "你是一个标书目录修复专家，擅长根据审核意见修复目录结构。请直接输出修复后的 JSON 数组。"

OUTLINE_REPAIR_SYSTEM = _reg(
    "OUTLINE_REPAIR_SYSTEM",
    "目录",
    "目录审核未通过·自修复（系统提示词）",
    OUTLINE_REPAIR_SYSTEM,
)

OUTLINE_REPAIR_USER = """你是一个标书目录修复专家。以下目录审核未通过，请根据审核意见修复目录。

【审核结果】
issues: {issues_json}
suggestions: {suggestions_json}

【当前目录】
{current_outline}

【评分点列表】
{scoring_points}

请直接输出修复后的完整目录 JSON 数组，保持三级结构不变，仅针对审核意见中的问题进行修正。严禁附加解释文字。"""

OUTLINE_REPAIR_USER = _reg(
    "OUTLINE_REPAIR_USER",
    "目录",
    "目录审核未通过·自修复（用户提示词）",
    OUTLINE_REPAIR_USER,
)

MERMAID_REPAIR_DIRECT_USER = """请修复以下 Mermaid 代码：

{current_code}

错误信息：{syntax_error}"""

MERMAID_REPAIR_DIRECT_USER = _reg(
    "MERMAID_REPAIR_DIRECT_USER",
    "配图",
    "Mermaid 直修（用户提示词）",
    MERMAID_REPAIR_DIRECT_USER,
)
