"""行业树提示词（2 个提示词）

用于行业树子节点生成。
"""

# 提示词注册到全局 _ALL_PROMPTS 注册表
from ._registry import _reg


# ===== 行业树 =====

INDUSTRY_GENERATE_CHILDREN_SYSTEM = """### 角色
你是一位资深的工程项目分类专家，熟悉中国建设工程行业分类标准（GB/T 50841-2013）及建筑工程领域的专业技术划分。

### 任务
为指定的行业分类父节点生成 {COUNT} 个合理的、直接子级的专业领域分类。

### 父节点信息
- 名称：{PARENT_NAME}
- 层级：第 {PARENT_LEVEL} 级
- 描述：{PARENT_DESCRIPTION}

### 生成规则
1. 子节点必须属于父节点的直接下级（二级或三级）
2. 每个子节点名称：4-10 个汉字，专业、具体
3. 每个子节点需包含简要描述（20-50 字）
4. 子节点之间应互斥、不重叠
5. 名称避免使用"其他"、"综合"等空泛词汇
6. 若父节点已是第三级，返回空数组（不继续生成为第四级）

### 输出格式
只返回合法的 JSON 数组：
[
  { "name": "子节点名称", "description": "子节点描述" },
  ...
]

### 约束
- 若父节点层级已达最大深度，返回空数组 []
- 直接返回 JSON，不要 Markdown 代码块，不要解释文字"""

INDUSTRY_GENERATE_CHILDREN_SYSTEM = _reg(
    "INDUSTRY_GENERATE_CHILDREN_SYSTEM",
    "行业树",
    "行业树子节点生成（系统提示词）",
    INDUSTRY_GENERATE_CHILDREN_SYSTEM,
)

# ENHANCE 2026-11-08（收编批次 5）：industry_tree.match_industry_by_ai 的 user 侧
# 提示词此前为调用点内联 f-string，绕过注册表（管理页不可见/不可覆盖/无版本），
# 与 system 侧（INDUSTRY_MATCH_SYSTEM，_integrate.py）不对称。现收编为
# INDUSTRY_MATCH_USER，调用方 get_prompt 渲染 + 逐字一致硬编码 fallback。
#
# 注意：f-string 中的 `{{"node_id"...}}` 在此写为单花括号 `{"node_id"...}`——
# 该形态不匹配 _VARIABLE_PATTERN（要求 { 后紧跟字母开头 ≥2 字符），渲染时
# 不会被当作占位符替换，逐字一致地进入发给模型的文本。
INDUSTRY_MATCH_USER = """
请根据以下项目描述，从行业树中匹配最合适的{top_k}个行业节点。

项目描述：{text}

行业树摘要（叶子节点）：
{tree_summary}

请按置信度排序返回 JSON 数组：
[
  {"node_id": "节点ID", "node_name": "节点名称", "confidence": 0.95, "reason": "匹配原因"},
  ...
]

要求：
1. confidence 范围 0-1，表示匹配程度
2. 至少返回 1 个结果，最多返回 {top_k} 个
3. 如果没有合适匹配，返回空数组 []
"""

INDUSTRY_MATCH_USER = _reg(
    "INDUSTRY_MATCH_USER",
    "行业树",
    "行业树语义匹配（用户提示词）",
    INDUSTRY_MATCH_USER,
)
