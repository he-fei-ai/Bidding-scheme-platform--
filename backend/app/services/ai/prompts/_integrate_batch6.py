"""收编批次 6（2026-11-08）：全局事实提取 user 侧提示词（2 个提示词）。

收编 ``routers/facts.py`` 的两处内联 f-string（此前绕过注册表、管理页不可见/
不可覆盖，与已收编的 ``FACTS_EXTRACT_SYSTEM`` 不对称）：

- ``FACTS_EXTRACT_USER``：单段提取 user 提示词（原 facts.py L807-825 内联构建）
- ``FACTS_EXTRACT_SEGMENT_USER``：分段提取 user 提示词（原 facts.py L866-869 内联构建）

接线（routers/facts.py）与 fallback 常量（``_FACTS_EXTRACT_USER_FB`` /
``_FACTS_EXTRACT_SEGMENT_USER_FB``）保持逐字一致；注册表/DB 无行时行为零变化。

动态块（Step02 解析结果 / 摘要 / 评分点 / 目录块）作为变量注入，模板外壳与
原 f-string 逐字符一致。
"""

from ._registry import _reg

FACTS_EXTRACT_USER = """请从以下项目文档中提取全局事实变量：

{source_text}
{outline_block}
{chapter_scope_block}
{step02_block}{summary_block}{scoring_block}

请提取所有可确定的事实变量，输出 JSON 对象（dict）。"""

FACTS_EXTRACT_USER = _reg(
    "FACTS_EXTRACT_USER",
    "文件解析",
    "全局事实提取（用户提示词正本）",
    FACTS_EXTRACT_USER,
)

FACTS_EXTRACT_SEGMENT_USER = """请从以下项目文档中提取全局事实变量（这是第 {seg_index}/{seg_total} 个分段，只提取本分段有明确依据的事实；本分段没有提及不代表整份文档没有提及，不要因某项信息未在本段出现而放弃提取）：

{seg}
{outline_block}
{chapter_scope_block}{extract_tail}"""

FACTS_EXTRACT_SEGMENT_USER = _reg(
    "FACTS_EXTRACT_SEGMENT_USER",
    "文件解析",
    "全局事实提取·分段（用户提示词）",
    FACTS_EXTRACT_SEGMENT_USER,
)
