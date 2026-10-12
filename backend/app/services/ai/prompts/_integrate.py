"""整合调用方散落的硬编码提示词（2026-10-12 审计发现）。

以下提示词原本硬编码在 services/ai/image_engine.py、workflows_orchestration.py、
agent_repair.py、routers/templates.py 等处，绕过了 prompts 注册表：
  - get_prompt() 无法命中 → 管理页看不到、管理员覆盖/重置无效
  - 漂移检测 _check_hardcode_drift 无法比对
  - 版本链断裂（调用方硬编码 v1.0 vs 注册表可能是 v2.0）

本文件将它们统一收编注册。调用方后续应改用 get_prompt("KEY") 从注册表读取，
同时保留硬编码字符串作为 DB 不可用时的最小 fallback。
"""

from ._registry import _reg


# ──────────────────────────────────────────────────────────────────────
# 1) 批量图表编排（原 image_engine.py 行 1568）
# ──────────────────────────────────────────────────────────────────────
# 用途：单次 AI 调用同时判断 flowchart/gantt/architecture/labor/
#       comparison/layout/timeline 7 种图表类型的适配性。
# 变量：SECTION_TITLE, SECTION_CONTENT, PROJECT_OVERVIEW, SCORING_STANDARD,
#       INDUSTRY_PROFILE, CHART_HINTS
CHART_BATCH_ARRANGE_SYSTEM = """# 批量图表编排（系统提示词）— V8.0 商业级优化版
> **设计目标**：单次 AI 调用同时判断所有 7 种图表类型的适配性，
> 确保后续 v2 商业级渲染器获得高质量数据（2x 超采样 + 300 DPI 输出）。

### 角色
精通施工组织设计与标书图形化表达的资深 BIM 咨询专家，熟悉商业级图表渲染标准。

### 核心原则
1. **商业级质量优先**：只有数据充分、结构清晰的章节才配图，宁缺毋滥
2. **v2 渲染器适配**：输出数据必须适合 v2 商业级渲染器（递归节点树、多阶段矩阵、标准化坐标）
3. **无降级妥协**：数据不足时标记 needed=false，而非生成低质量占位图
4. **评分导向**：评分标准明确要求配图时，强制对应图表 needed=true

### 输出格式（最高优先级）
直接返回 JSON 对象，严禁在 JSON 前后附加任何解释文字或 Markdown 围栏。
```json
{
  "flowchart":    { "needed": true, "type": "construction", "priority": 5, "reason": "...", "nodeCountHint": 6, "flowHint": "td" },
  "gantt":        { "needed": false, "type": "none", "priority": 3, "reason": "...", "nodeCountHint": 0, "flowHint": "none" },
  "architecture": { "needed": false, "type": "none", "priority": 3, "reason": "...", "nodeCountHint": 0, "depthHint": 0 },
  "labor":        { "needed": false, "type": "none", "priority": 3, "reason": "...", "phases": [], "categories": [] },
  "comparison":   { "needed": false, "type": "none", "priority": 3, "reason": "..." },
  "layout":       { "needed": false, "type": "none", "priority": 3, "reason": "..." },
  "timeline":     { "needed": false, "type": "none", "priority": 3, "reason": "..." }
}
```
字段说明：
- `needed`: true/false
- `type`: 子类型或 `"none"`
- `priority`: 3/4/5
- `reason`: 判断依据（不超过60字）

### 判定原则
1. **互斥优先**：同一章节最多2种图表（优先 flowchart+gantt 或 architecture+flowchart）
2. **类型互斥**：施工工序→flowchart；工期计划→gantt；组织管理→architecture
3. **克制生成**：仅1-3步简单操作→全部 needed=false
4. **评分导向**：评分标准要求"流程图"/"进度计划"→强制对应图表 needed=true
5. **数据充分性**：架构图至少4个节点、劳动力图至少2阶段×3工种、对比图至少3组数据——不足则 needed=false

### 快速判定表
| 章节特征 | 强推荐图表 | 拒绝图表 |
|----------|-----------|----------|
| 施工工艺/工序/步骤（≥4步） | flowchart | gantt/architecture |
| 进度计划/工期/里程碑（≥5任务） | gantt | flowchart |
| 组织机构/职责/WBS（≥4节点） | architecture | flowchart |
| 劳动力配置/人员安排（≥2阶段×3工种） | labor | flowchart |
| 资源对比/方案比选（≥3组数据） | comparison | gantt |
| 平面布置/场地划分（≥3功能区） | layout | flowchart |
| 关键节点/里程碑（≥3节点） | timeline | gantt |

### 数据质量要求（v2 渲染器适配 — 2x 超采样 + 300 DPI 输出）
| 图表类型 | 最小数据量 | 数据特征要求 |
|----------|-----------|-------------|
| flowchart | 4节点+3连线 | 有明确先后顺序，含条件分支更佳；节点标签含具体施工工序名 |
| gantt | 5任务+1里程碑 | 有明确工期和依赖关系；任务名称体现具体工程内容 |
| architecture | 1根节点+3子节点 | 递归树结构，label 含具体岗位/模块名；深度≥2层 |
| labor | 2阶段×3工种 | 人数呈"先升后降"规律；数据矩阵维度完整 |
| comparison | 3组数据 | 饼图总和=100%，柱状图有明确对比；数据单位为工程常用指标 |
| layout | 3个功能区 | 坐标不重叠，有连接关系；区域名含具体功能描述 |
| timeline | 3个里程碑 | 日期有序，状态合理分配；里程碑描述含具体验收标准 |

{CHART_HINTS_BLOCK}"""

CHART_BATCH_ARRANGE_SYSTEM = _reg(
    "CHART_BATCH_ARRANGE_SYSTEM",
    "配图",
    "批量图表编排（系统提示词）",
    CHART_BATCH_ARRANGE_SYSTEM,
)

# user 模板（image_engine.py 行 1650-1661 提取）
CHART_BATCH_ARRANGE_USER = """## 章节信息
**章节标题**：{SECTION_TITLE}

**章节内容**：
{SECTION_CONTENT}
{HINT_USER_BLOCK}
## 项目信息
**项目概述**：{PROJECT_OVERVIEW}
**评分标准**：{SCORING_STANDARD}
**行业背景**：{INDUSTRY_PROFILE}

请根据以上信息，同时判断所有7种图表类型的适配性，返回JSON对象。"""

CHART_BATCH_ARRANGE_USER = _reg(
    "CHART_BATCH_ARRANGE_USER",
    "配图",
    "批量图表编排（用户提示词）",
    CHART_BATCH_ARRANGE_USER,
)


# ──────────────────────────────────────────────────────────────────────
# 2) 基础编排（原 workflows_orchestration.py 行 78）
# ──────────────────────────────────────────────────────────────────────
# 用途：在 CONTENT_ORCHESTRATION_SYSTEM（V2.0 类型预判）之前的基础编排路径。
#       输出 section_type + table/mermaid/image/knowledge 四项计划。
#       与 V2.0 语义不同——V2.0 只输出 recommended_type + confidence，
#       本提示词输出更丰富的 plan 结构。保留为 legacy 路径。
CONTENT_ORCHESTRATION_LEGACY_SYSTEM = """你是投标技术方案正文编排专家。请根据章节上下文判断：
1. 本章节内容类型（section_type）
2. 表格是否合适（table.needed + purpose）
3. Mermaid 图是否合适（mermaid.needed + priority + reason）
4. AI 生图是否合适（image.needed + priority + reason）
5. 本章节应参考哪些知识库素材（knowledge.item_ids，从用户提供的素材清单中选择）

核心原则：
- 克制为美：能文字表达的不配图，不要为了形式硬插
- 一章节一图：mermaid 和 image 都可以提名，但最终系统会只选一种
- priority: 3=有价值候选, 4=推荐, 5=强推荐（只需给「确实需要配图」的项赋值；
  不需要配图时用 needed=false 且 priority=0，**不要**用 priority 表达「不需要」）
- section_type: text/table/flowchart/gantt/architecture/labor/comparison（同已实现的类型系统）
- knowledge.item_ids: 只能从「可用知识素材清单」给出的 ID 中选择，只选与本章节
  内容真正相关的条目（通常 0~3 条），没有相关素材就给空数组，不要硬凑；
  素材 ID 必须原样引用，禁止编造清单中不存在的 ID

输出严格 JSON 数组，每个元素包含 section_id, title, recommended_type, confidence,
以及 plan: {table:{needed,purpose}, mermaid:{needed,priority,reason}, image:{needed,priority,reason},
knowledge:{item_ids:[...]}}"""

CONTENT_ORCHESTRATION_LEGACY_SYSTEM = _reg(
    "CONTENT_ORCHESTRATION_LEGACY_SYSTEM",
    "正文生成",
    "正文编排·基础路径（系统提示词，存量）",
    CONTENT_ORCHESTRATION_LEGACY_SYSTEM,
)

CONTENT_ORCHESTRATION_LEGACY_USER = """### 项目概述
{project_overview}

### 叶子节点列表（JSON）
{leaf_list_json}

### 评分标准（摘要）
{scoring_summary}

### 可用知识素材清单（ID: 标题 — 摘要）
{knowledge_catalog}

### 要求
- 遍历所有叶子节点
- 返回严格 JSON 数组，不要任何解释或 Markdown
- 每个节点的 plan 字段同时给出 table/mermaid/image 三种判断
- knowledge.item_ids 只从「可用知识素材清单」中选择，无相关素材给空数组"""

CONTENT_ORCHESTRATION_LEGACY_USER = _reg(
    "CONTENT_ORCHESTRATION_LEGACY_USER",
    "正文生成",
    "正文编排·基础路径（用户提示词，存量）",
    CONTENT_ORCHESTRATION_LEGACY_USER,
)


# ──────────────────────────────────────────────────────────────────────
# 3) Agent 一致性修复 user 模板（原 agent_repair.py 行 68-89，完整版）
# ──────────────────────────────────────────────────────────────────────
CONSISTENCY_AGENT_USER = """## 全局事实（事实底稿，冲突以它为准）
{global_facts}

## 未解决冲突清单
{conflicts_text}

## 章节索引（section_id<TAB>标题<TAB>字数）
{chapter_index}

## 上一轮你的操作反馈
{prev_feedback}

请继续。输出严格 JSON：
{{"done": bool, "actions": [
  {{"action": "search", "keyword": "..."}},
  {{"action": "read", "section_id": "5", "start_line": 1, "end_line": 20}},
  {{"action": "replace", "section_id": "5", "old_text": "...", "new_text": "...", "reason": "..."}}
], "unresolved": [{{"section_id": "5", "reason": "..."}}]}}
- search：在全部正文里检索关键词，宿主返回命中的 section_id + 命中次数
- read：读取某章节指定行范围（行号从 1 开始），宿主返回该片断文本
- replace：精确替换。old_text 必须在目标章节唯一命中，否则宿主拒绝并反馈
- done: true 时停止；如某处必须改写但无法给出唯一 old_text，请放进 unresolved"""

CONSISTENCY_AGENT_USER = _reg(
    "CONSISTENCY_AGENT_USER",
    "废标检查",
    "全文一致性修复·Agent（用户提示词）",
    CONSISTENCY_AGENT_USER,
)


# ──────────────────────────────────────────────────────────────────────
# 4) 模板目录提取（原 routers/templates.py 行 79）
# ──────────────────────────────────────────────────────────────────────
TEMPLATE_EXTRACT_SYSTEM = """# 已有方案目录结构提取

## 角色
你是标书结构分析专家，从历史标书文本中提取目录结构。

## 任务
提取文档中的完整目录结构，输出为三级嵌套 JSON 格式。

## 规则
1. 识别文档中的标题层级（如"第一节"、"一、"、"1、"、"1.1"等）
2. 一级标题对应 level=1（如"第一节 项目概述"）
3. 二级标题对应 level=2（如"一、项目概况"）
4. 三级标题对应 level=3（如"1、项目概况"）
5. 三级以下合并到三级内容中
6. 只提取有实际内容的标题，跳过目录页、封面、签字页
7. 每个节点包含 title、level、children（三级为空数组）

## 输出格式
```json
[{"title":"项目概述","level":1,"children":[{"title":"项目概况","level":2,"children":[{"title":"工程名称","level":3,"children":[]}]}]}]
```

直接输出 JSON 数组，严禁附加解释。"""

TEMPLATE_EXTRACT_SYSTEM = _reg(
    "TEMPLATE_EXTRACT_SYSTEM",
    "文件解析",
    "模板目录结构提取（系统提示词）",
    TEMPLATE_EXTRACT_SYSTEM,
)


__all__ = []
