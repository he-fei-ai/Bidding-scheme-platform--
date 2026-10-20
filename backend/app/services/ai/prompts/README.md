# AI 提示词模块（Agent 搜索启发式）

> 本文档为 AI Agent 编写，旨在帮助 Agent 在 5 分钟内理解本模块的全貌、
> 快速定位所需的提示词常量、调用入口、路由位置和扩展点。

---

## 1. 模块全景

### 1.1 包结构

```
backend/app/services/ai/prompts/
├── __init__.py        # 公共 API：get_prompt / reload_prompt_cache / _ALL_PROMPTS / _reg
├── _cache.py          # 运行时缓存（DB 优先 → 硬编码回退）
├── _registry.py       # 注册表（_ALL_PROMPTS + _reg 注册函数 + {shared:NAME} 注入）
├── _shared.py         # 共享规则库（SHARED_HEADING_RULES / SHARED_FORBIDDEN_WORDS / SHARED_OUTPUT_SPEC）
├── json_repair.py     # 1 个：JSON_REPAIR_PROMPT
├── analysis.py        # 14 个：文件解析阶段（项目概述/评分/BOQ/工接口/施工方法）
├── outline.py         # 6 个：目录审核 / 技术方案 V5.1（L1/L2/自检）/ 三级标题修复
├── content.py         # 3 个：正文编排（系统/用户）/ 正文续写
├── mermaid.py         # 6 个：Mermaid 图（修复/排版/流程图/甘特图）
├── charts.py          # 11 个：图表（架构/劳动力/对比/布局/时间线 × 编排+生成 + 通用修复兜底 CHART_DATA_FIX）
├── illustration.py    # 2 个：ILLUSTRATION_ARRANGE_SYSTEM / ILLUSTRATION_PLAN_SYSTEM
├── checks.py          # 9 个：废标提取/对照/三轮流程(分析/初稿/定稿)/错别字/逻辑/合并/一致性补丁
├── knowledge.py       # 3 个：知识库（提取/匹配/目录增强）
└── industry.py        # 1 个：行业树（INDUSTRY_GENERATE_CHILDREN_SYSTEM）
```

合计 **77 个**提示词常量（62 子模块 + 12 收编 `_integrate.py` + 3 个 SHARED_* 共享规则；
注册到管理界面只读展示，可经 `{shared:NAME}` 注入其他提示词）。

> **数量以代码为准**：本节曾长期写「57 个」而实际已增至 77 个（2026-10-17 修正）。
> 如需核对，运行 `python -c "from app.services.ai.prompts import _ALL_PROMPTS; print(len(_ALL_PROMPTS))"`，
> 或调用 `GET /prompts/overview` 读取 `total` 与 `shared_usage`。
>
> **复用现状（重要）**：`{shared:NAME}` 机制可用，但注册表中**尚无模板引用**它
> （`audit_shared_usage()["adoption_rate"] == 0`）。三条共享规则目前只作为「管理页
> 可查看的只读条目」存在，标题编号规范/禁用词/输出格式规范仍由各模板**逐份内联**
> 维护。收编时须注意：改为 `{shared:}` 引用会改变模板实际内容，需同步回归验证。

### 1.2 公共 API

```python
from app.services.ai.prompts import (
    get_prompt,           # 读取提示词（DB 优先 → 硬编码回退）
    reload_prompt_cache,  # 强制重新从 DB 加载缓存
    _ALL_PROMPTS,         # 全量元信息：{key: {category, label, content}}
    _reg,                 # 注册函数（仅供子模块内部使用）
)
```

### 1.3 加载策略

```
get_prompt(key) 流程
   │
   ├─ 1. 缓存未加载？  →  _load_prompt_cache()
   │                        │
   │                        └─ 从 DB 的 prompt_templates 表 SELECT key, content
   │                           异常时缓存 = {}，不抛错
   │
   ├─ 2. 缓存中存在？  →  返回 DB 内容（覆盖硬编码）
   │
   └─ 3. 回退到 _ALL_PROMPTS[key]['content']
            │
            └─ 仍无？  →  返回空字符串 ""
```

---

### 1.4 共享规则注入 `{shared:NAME}`

重复出现的规则（标题编号规范、标题禁用词、输出格式规范）统一维护在 `_shared.py`，
通过 `{shared:NAME}` 占位符在渲染时注入，避免各提示词各自复制导致的不一致。

- **可用共享规则**：`SHARED_HEADING_RULES` / `SHARED_FORBIDDEN_WORDS` / `SHARED_OUTPUT_SPEC`
- **使用方式**：在任意提示词模板中写 `{shared:SHARED_HEADING_RULES}`，`render_prompt()` 会自动替换为对应共享规则内容。
- **查找顺序**：`NAME` → `SHARED_NAME` → `NAME.upper()` → `SHARED_<NAME.upper()>`，找不到则保留原占位符并告警。
- **注意**：共享规则均为静态文本（不含变量占位符），注入后不会引入未替换的 `{var}`；该机制已落地，各提示词可直接引用而无需再内联复制规则文本。

---

## 2. Agent 速查表

### 2.1 「我需要修改某个提示词」

| 任务 | 操作 |
|---|---|
| 找到某个提示词的内容 | 搜索 `get_prompt("XXX")` 的调用点 → 跳转到对应常量定义 |
| 修改文案 | 直接编辑对应子模块的常量（如 `prompts/outline.py`），无需改 DB |
| 新增提示词 | 在对应子模块写 `XXX = _reg("XXX", "分类", "标签", "内容")` |
| 通过 DB 覆盖 | 调用 `routers/prompts.py` 的保存接口，下次 `get_prompt` 自动优先返回 DB 内容 |

### 2.2 「我需要看某个提示词被谁调用」

```bash
# 在 backend/app 目录下搜索
grep -rn 'get_prompt("KEY_NAME")' app/
```

调用集中点：
- [workflows.py](file:///j:/%E7%BC%96%E7%A8%8B/%E6%8B%9B%E6%8A%95%E6%A0%87%E5%B8%AE%E5%B9%B3%E5%8F%B0/backend/app/services/ai/workflows.py)：文件解析/目录生成/正文生成
- [image_engine.py](file:///j:/%E7%BC%96%E7%A8%8B/%E6%8B%9B%E6%8A%95%E6%A0%87%E5%B8%AE%E5%B9%B3%E5%8F%B0/backend/app/services/ai/image_engine.py)：图像/图表编排与生成
- [rejection.py](file:///j:/%E7%BC%96%E7%A8%8B/%E6%8B%9B%E6%8A%95%E6%A0%87%E5%B8%AE%E5%B9%B3%E5%8F%B0/backend/app/services/rejection.py)：废标/错别字/逻辑检查
- [knowledge.py](file:///j:/%E7%BC%96%E7%A8%8B/%E6%8B%9B%E6%8A%95%E6%A0%87%E5%B8%AE%E5%B9%B3%E5%8F%B0/backend/app/services/knowledge.py)：知识库提取/匹配/目录增强
- [industry_tree.py](file:///j:/%E7%BC%96%E7%A8%8B/%E6%8B%9B%E6%8A%95%E6%A0%87%E5%B8%AE%E5%B9%B3%E5%8F%B0/backend/app/services/industry_tree.py)：行业树子节点生成

### 2.3 「我需要知道某个提示词有哪些占位符」

提示词中的占位符统一用 `__XXX__` 格式（双下划线包围）。

**全量占位符清单（28 个）**：

| 占位符 | 出现次数 | 替换点（业务代码） |
|---|---|---|
| `__SUMMARY__` | 16 | 文件解析后注入的摘要 |
| `__CONTEXT__` | 7 | 章节上下文（评分/BOQ/工接口/施工方法） |
| `__INDUSTRY_PROFILE__` | 6 | 行业画像 JSON |
| `__SCORING_POINTS__` | 6 | 评分点列表 |
| `__WRITING_LENGTH__` | 6 | short/standard/detailed/extra_long |
| `__TENDER_CONTEXT__` | 4 | 招标上下文 |
| `__WORK_INTERFACE__` | 4 | 工作面接口分析结果 |
| `__OUTLINE__` | 3 | 目录树 JSON |
| `__REFERENCE_OUTLINE__` | 3 | 参考目录（合并场景） |
| `__TARGET_CHARS__` | 3 | 目标字数（数字） |
| `__BOQ_ANALYSIS__` | 5 | 工程量清单分析 |
| `__CONSTRUCTION_METHOD__` | 5 | 施工方法分析 |
| `__SECTION_TITLE__` | 5 | 当前章节标题 |
| `__CHILDREN_FRAMEWORK__` | 2 | 子目录框架 |
| `__EXISTING_POINTS__` | 2 | 已有点 |
| `__GLOBAL_FACTS__` | 2 | 全局事实 |
| `__OTHER_CHAPTERS__` | 2 | 其他章节 |
| `__CONTENT_TYPE__` | 1 | 内容类型 |
| `__COUNT__` | 1 | 数量 |
| `__EXISTING_CONTENT__` | 1 | 已写内容 |
| `__PARENT_DESCRIPTION__` | 1 | 父节点描述 |
| `__PARENT_ITEM__` | 1 | 父项 |
| `__PARENT_LEVEL__` | 1 | 父层级 |
| `__PARENT_NAME__` | 1 | 父名称 |
| `__SCORING_STANDARDS__` | 1 | 编排阶段复数评分标准（CONTENT_ORCHESTRATION_USER） |
| `__SCORING_STANDARD__` | 1 | 单章节单数评分标准（历史遗留，CONTENT_GENERATION_USER 已于 2026-09-14 下线） |
| `__SECTION_PATH__` | 1 | 章节路径 |
| `__SP_CHECKLIST__` | 1 | 错别字清单 |

> **注意**：`__SCORING_STANDARDS__` 和 `__SCORING_STANDARD__` 命名虽相似，
> 但是两个不同的占位符（编排复数 vs 单章节单数），不要尝试统一。

---

## 3. 关键分类速查

| 阶段 | 提示词文件 | 调用入口 |
|---|---|---|
| **文件解析** | [analysis.py](file:///j:/%E7%BC%96%E7%A8%8B/%E6%8B%9B%E6%8A%95%E6%A0%87%E5%B8%AE%E5%B9%B3%E5%8F%B0/backend/app/services/ai/prompts/analysis.py) | workflows.py: 解析 TENDER_SUMMARY/SCORING/BOQ/WORK_INTERFACE/CONSTRUCTION_METHOD |
| **目录生成** | [outline.py](file:///j:/%E7%BC%96%E7%A8%8B/%E6%8B%9B%E6%8A%95%E6%A0%87%E5%B8%AE%E5%B9%B3%E5%8F%B0/backend/app/services/ai/prompts/outline.py) | workflows.py: one_shot/step/children/review/supplement/merge/children_framework |
| **正文生成** | [content.py](file:///j:/%E7%BC%96%E7%A8%8B/%E6%8B%9B%E6%8A%95%E6%A0%87%E5%B8%AE%E5%B9%B3%E5%8F%B0/backend/app/services/ai/prompts/content.py) | workflows.py: orchestration/generation/continue |
| **图表编排** | [mermaid.py](file:///j:/%E7%BC%96%E7%A8%8B/%E6%8B%9B%E6%8A%95%E6%A0%87%E5%B8%AE%E5%B9%B3%E5%8F%B0/backend/app/services/ai/prompts/mermaid.py) + [charts.py](file:///j:/%E7%BC%96%E7%A8%8B/%E6%8B%9B%E6%8A%95%E6%A0%87%E5%B8%AE%E5%B9%B3%E5%8F%B0/backend/app/services/ai/prompts/charts.py) | image_engine.py: 各 ARRANGE_/GENERATE_/REPAIR 系列 |
| **配图方案** | [illustration.py](file:///j:/%E7%BC%96%E7%A8%8B/%E6%8B%9B%E6%8A%95%E6%A0%87%E5%B8%AE%E5%B9%B3%E5%8F%B0/backend/app/services/ai/prompts/illustration.py) | image_engine.py: ILLUSTRATION_PLAN_SYSTEM |
| **废标检查** | [checks.py](file:///j:/%E7%BC%96%E7%A8%8B/%E6%8B%9B%E6%8A%95%E6%A0%87%E5%B8%AE%E5%B9%B3%E5%8F%B0/backend/app/services/ai/prompts/checks.py) | rejection.py: REJECTION_*/SPELLING_*/LOGIC_* |
| **知识库** | [knowledge.py](file:///j:/%E7%BC%96%E7%A8%8B/%E6%8B%9B%E6%8A%95%E6%A0%87%E5%B8%AE%E5%B9%B3%E5%8F%B0/backend/app/services/ai/prompts/knowledge.py) | knowledge.py: KB_* |
| **行业树** | [industry.py](file:///j:/%E7%BC%96%E7%A8%8B/%E6%8B%9B%E6%8A%95%E6%A0%87%E5%B8%AE%E5%B9%B3%E5%9F%B0/backend/app/services/ai/prompts/industry.py) | industry_tree.py: INDUSTRY_GENERATE_CHILDREN_SYSTEM |
| **JSON 修复** | [json_repair.py](file:///j:/%E7%BC%96%E7%A8%8B/%E6%8B%9B%E6%8A%95%E6%A0%87%E5%B8%AE%E5%B9%B3%E5%8F%B0/backend/app/services/ai/prompts/json_repair.py) | workflows.py: JSON_REPAIR_PROMPT |

---

## 4. 提示词常量命名规范

- **全大写下划线**：`OUTLINE_ONE_SHOT_SYSTEM`
- **三段式**：`{模块}_{用途}_{角色}`，如 `OUTLINE_CHILDREN_USER`
- **角色后缀**：
  - `_SYSTEM`：系统提示词（角色/规则/输出格式）
  - `_USER`：用户提示词（含 `__XXX__` 占位符）
  - 无后缀：单文件提示词（`JSON_REPAIR_PROMPT` / `CONTENT_CONTINUE_PROMPT` / `CHART_DATA_FIX`）
- **注册表登记**：每写一个新常量必须用 `_reg(KEY, '分类', '中文标签', CONTENT)` 登记，否则不会出现在前端管理界面。

---

## 5. 提示词修改的安全检查清单

修改任何提示词前，请确认：

- [ ] **占位符完整性**：所有 `__XXX__` 必须成对出现，无半截
- [ ] **占位符命名**：使用现有 28 个中的名称，新占位符需同步更新业务代码
- [ ] **零宽字符**：`\u200b` (ZWSP) / `\ufeff` (BOM) / `\u00ad` (软连字符) 一律清除
- [ ] **ASCII 控制字符**：除 `\n \r \t` 外不应存在
- [ ] **三连换行**：避免连续 3 个 `\n\n` 浪费 token
- [ ] **引号归一化**：使用半角 `"` 或 `'`，全角引号 `"` `'` 全部替换
- [ ] **破折号归一化**：`—` `–` 统一为 `--`
- [ ] **运行测试**：`pytest tests/test_prompts_module_2026_10_04.py` 必须全绿
- [ ] **拆分一致性**：拆分前请确认 prompts.py.legacy.bak 中存在该提示词
- [ ] **DB 覆盖**：若修改硬编码，需要同步到 DB——在提示词管理页点「同步硬编码」（`POST /prompts/sync-hardcode`），或对对应提示词执行「重置为默认」；首次初始化由启动期 `app/db.py::seed_prompt_templates()` 负责

---

## 6. 测试

```bash
cd backend
python -m pytest tests/test_prompts_module_2026_10_04.py -v
```

**当前覆盖率**（[test_prompts.py](file:///j:/%E7%BC%96%E7%A8%8B/%E6%8B%9B%E6%8A%95%E6%A0%87%E5%B8%AE%E5%B9%B3%E5%8F%B0/backend/tests/test_prompts_module_2026_10_04.py)）：

| 测试类 | 用例数 | 覆盖目标 |
|---|---|---|
| `TestRegistry` | 60 | 注册表完整、无意外键、字段齐全、中文分类 |
| `TestGetPrompt` | 60 | 公共 API 行为、每个 key 可取回 |
| `TestReloadCache` | 1 | 缓存刷新基础 |
| `TestDBCachePriority` | 3 | DB 优先、回退硬编码、异常回退 |
| `TestCacheReload` | 3 | reload 后能读到 DB 变化、DB 不可用降级、幂等 |
| `TestCacheConsistency` | 2 | 重复调用一致性、基础线程安全 |
| `TestPlaceholders` | 121 | 无效字符、控制字符、orphan 占位符、命名一致性 |
| `TestSubmoduleStructure` | 11 | 12 个子模块文件存在、模块 docstring 完整 |
| `TestSplitConsistency` | 2 | 备份存在、内容与备份完全一致 |
| **合计** | **325** | |

---

## 7. 常见陷阱

| 陷阱 | 说明 | 解决方案 |
|---|---|---|
| 改了硬编码但前端看不到新内容 | DB 中存在旧版本，`get_prompt` 优先返回 DB | 执行 `init_prompt_templates.py` 同步；或调用 `reload_prompt_cache()` |
| 占位符未替换 | `__XXX__` 在 prompt 中以字面量发给 LLM | 确认业务代码中调用了 `.replace("__XXX__", value)` |
| `get_prompt` 返回空 | key 名写错或未注册 | 查 `_ALL_PROMPTS` 确认 key 存在 |
| 跨包导入 `from app.services.ai.prompts` 失败 | 旧版 prompts.py 在子模块未导入时未注册 | 已通过 `__init__.py` 强制 import 所有子模块解决 |
| 修改后 prompts 内容长度异常 | 可能引入了零宽字符或 BOM | 用编辑器开启"显示所有字符"清理 |

---

## 8. 扩展指南

### 8.1 新增提示词

```python
# 在 prompts/charts.py 中追加
NEW_CHART_SYSTEM = """..."""
NEW_CHART_SYSTEM = _reg(
    "NEW_CHART_SYSTEM",
    "图表类",                          # category（前端分组）
    "新图表系统提示词",                # label（前端展示名）
    NEW_CHART_SYSTEM,                  # content
)
```

导入时确保子模块被 `__init__.py` 引用：

```python
# __init__.py 中 from . import (..., charts, ...)
```

### 8.2 修复 BUG 后务必检查

1. `python -m pytest tests/test_prompts_module_2026_10_04.py` 全绿
2. 对应的业务函数（如 `workflows.py:xxx`）单测全绿
3. 如修改了占位符名称，全局搜索 `__OLD_NAME__` / `__NEW_NAME__` 替换
4. 同步 `prompts.py.legacy.bak`（如果新逻辑导致拆分需重做）

---

## 9. 相关文件路径

| 用途 | 路径 |
|---|---|
| 提示词包入口 | [prompts/__init__.py](file:///j:/%E7%BC%96%E7%A8%8B/%E6%8B%9B%E6%8A%95%E6%A0%87%E5%B8%AE%E5%B9%B3%E5%8F%B0/backend/app/services/ai/prompts/__init__.py) |
| 缓存机制 | [prompts/_cache.py](file:///j:/%E7%BC%96%E7%A8%8B/%E6%8B%9B%E6%8A%95%E6%A0%87%E5%B8%AE%E5%B9%B3%E5%8F%B0/backend/app/services/ai/prompts/_cache.py) |
| 注册表 | [prompts/_registry.py](file:///j:/%E7%BC%96%E7%A8%8B/%E6%8B%9B%E6%8A%95%E6%A0%87%E5%B8%AE%E5%B9%B3%E5%8F%B0/backend/app/services/ai/prompts/_registry.py) |
| 业务调用 | [workflows.py](file:///j:/%E7%BC%96%E7%A8%8B/%E6%8B%9B%E6%8A%95%E6%A0%87%E5%B8%AE%E5%B9%B3%E5%8F%B0/backend/app/services/ai/workflows.py) |
| 图表调用 | [image_engine.py](file:///j:/%E7%BC%96%E7%A8%8B/%E6%8B%9B%E6%8A%95%E6%A0%87%E5%B8%AE%E5%B9%B3%E5%8F%B0/backend/app/services/ai/image_engine.py) |
| 废标检查 | [rejection.py](file:///j:/%E7%BC%96%E7%A8%8B/%E6%8B%9B%E6%8A%95%E6%A0%87%E5%B8%AE%E5%B9%B3%E5%8F%B0/backend/app/services/rejection.py) |
| 知识库 | [knowledge.py](file:///j:/%E7%BC%96%E7%A8%8B/%E6%8B%9B%E6%8A%95%E6%A0%87%E5%B8%AE%E5%B9%B3%E5%8F%B0/backend/app/services/knowledge.py) |
| 行业树 | [industry_tree.py](file:///j:/%E7%BC%96%E7%A8%8B/%E6%8B%9B%E6%8A%95%E6%A0%87%E5%B8%AE%E5%B9%B3%E5%8F%B0/backend/app/services/industry_tree.py) |
| 单元测试 | [tests/test_prompts_module_2026_10_04.py](file:///j:/%E7%BC%96%E7%A8%8B/%E6%8B%9B%E6%8A%95%E6%A0%87%E5%B8%AE%E5%B9%B3%E5%8F%B0/backend/tests/test_prompts_module_2026_10_04.py) |
| 原始备份 | `prompts.py.legacy.bak`（仅作拆分一致性比对用） |
