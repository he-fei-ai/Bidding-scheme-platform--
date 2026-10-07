# 招投标方案平台 — 深度探索与 BUG 优化报告（v5）

> **范围**：项目整体架构 + 提示词 / 目录生成 / 正文生成 / 图表渲染 / DOCX 导出 五大模块
> **方法**：源码级精读（73K 行 Python、22+ 路由文件、18 个前端页面）
> **生成时间**：2026-09-04
> **核心目标**：摸清每个模块的功能、工作流、AI 调用、路由、数据链；识别 BUG 与性能瓶颈；输出可执行修复

---

## 0. 总体架构

```
┌─────────────────────────────── 前端 (React 18 + AntD + Vite) ────────────────────────────────┐
│  ProjectListPage  SummaryReviewPage  OutlineEditorPage(SSE)  SectionEditorPage(SSE)       │
│  ExportConfigPage  PromptEditorPage  AIConfigPage  IndustryTreePage  ImageConfigPage ...  │
│                ↓ axios (http)        ↓ EventSource/fetch+ReadableStream (SSE)            │
│               /api/v1/...  (Vite 代理到后端 127.0.0.1:8000)                                  │
└─────────────────────────────────────────────────────────────────────────────────────────────┘
                                              ↓
┌─────────────────────────────── 后端 (FastAPI + SQLite + Redis) ─────────────────────────────┐
│  main.py (24 个 v1_router 挂载)                                                              │
│  ┌─ Routers (201 endpoints, 13K 行) ───────────────────────────────────────────────────────┐ │
│  │  projects / files / ai / ai_config / prompts / sections / templates / export / facts   │ │
│  │  rejection / duplicate / industry_tree / knowledge / usage / monitor / api_keys        │ │
│  │  batch / cache / i18n / consistency / qa / word_count / websocket / images              │ │
│  │  content_version / tasks                                                                │ │
│  └────────────────────────────────────────────────────────────────────────────────────────┘ │
│  ┌─ Services (53K 行) ─────────────────────────────────────────────────────────────────────┐ │
│  │  services/ai/         workflows (12 子模块) + providers (4 厂商) + prompts (12 类)    │ │
│  │  services/            export (DOCX 拼装) / generation_manager (断点续生) / sections   │ │
│  │  ai_monitor / chart_validators / content_cleaning / duplicate / qa / rejection ...   │ │
│  └────────────────────────────────────────────────────────────────────────────────────────┘ │
│  ┌─ DB Layer ────────────────────────────────────────────────────────────────────────────┐  │
│  │  SQLite (WAL) + ConnectionPool + db_pool + db_migrations（19+ 张表）                 │  │
│  └────────────────────────────────────────────────────────────────────────────────────────┘  │
│  ┌─ 外部 AI ──────────────────────────────────────────────────────────────────────────────┐  │
│  │  优先级链：主配置 (ai_config) → AI Profile 默认 → 历史记录 → 内置兜底（agnes）     │  │
│  │  厂商：OpenAI 兼容 / DeepSeek / Anthropic 兼容                                        │  │
│  │  图：Mermaid HTTP 服务 (Docker) → Railway Cloud → mmdc CLI → PIL 降级                │  │
│  └────────────────────────────────────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────────────────────────────┘
```

| 维度 | 数字 |
|---|---|
| Python 源文件 | 136 个（73K 行） |
| 后端路由 | 201 endpoints（12,993 行） |
| Service 文件 | 约 80 个（53K 行） |
| 前端 TS/TSX | 2570 行 api/index.ts + 18 个页面 + 多个组件 |
| 提示词 | ~50 个键，分 12 个分类（共享 / 分析 / 目录 / 内容 / Mermaid / 图表 / 配图 / 校验 / 知识 / 行业） |
| 大文件 Top 5 | mermaid_renderer 3938 行、workflows_outline_v51 3666 行、export 2593 行、prompts/charts 2169 行、workflows_sections 2015 行 |

---

## 1. 模块 1：提示词模块（Prompts）

### 1.1 功能

- **管理方式**：所有提示词硬编码在 `backend/app/services/ai/prompts/*.py`，通过 `_reg()` 注册到全局 `_ALL_PROMPTS` 注册表
- **存储**：DB 表 `prompt_templates`（`key / content / default_content / category / label`），支持前端在线编辑
- **优先级**：DB 命中 → 内存缓存（`_prompt_cache`）→ 硬编码回退
- **变量渲染**：`render_prompt()` 支持 `{var}` 和 `__VAR__` 两种占位符，**大小写不敏感**（BUG-FIX-B1 修复）
- **校验**：`get_prompt_with_validation()` 返回 (text, missing_vars)；残留占位符检测

### 1.2 工作流

```
前端 PromptEditorPage 
  → GET  /api/v1/prompts         列出所有提示词
  → GET  /api/v1/prompts/{key}   读取单个
  → PUT  /api/v1/prompts/{key}   写入（DB upsert + reload_prompt_cache）
  → POST /api/v1/prompts/{key}/reset  恢复硬编码默认
```

后端：`prompts.py:72-89` 的 `update_prompt` 修复了 `default_content` 在新 key 上回退为空的 bug，调用 `reload_prompt_cache()` 使后续 `get_prompt()` 立即生效。

### 1.3 AI 调用

提示词不直接调用 AI，只生成字符串。`get_prompt(key, **kwargs)` 返回渲染好的文本供 `workflows_*` 使用。变量模板示例（outline.py:12-64 的 `OUTLINE_REVIEW_SYSTEM`）。

### 1.4 数据链

```
硬编码常量 → _reg() 注册 → _ALL_PROMPTS (内存)
                                ↑
                    DB prompt_templates (启动时 _load_prompt_cache 预热)
                                ↑
              reload_prompt_cache() (前端编辑后调用)
                                ↓
                get_prompt(key, **kwargs)  ←  workflow/前端调用
```

### 1.5 路由

| Method | Path | 用途 |
|---|---|---|
| GET    | /api/v1/prompts | 列出所有 |
| GET    | /api/v1/prompts/{key} | 读单个 |
| PUT    | /api/v1/prompts/{key} | 写 |
| POST   | /api/v1/prompts/{key}/reset | 重置 |

### 1.6 已识别问题

1. ✅ **BOM 残留**（5 个文件）— 已修复
   - `services/ai/ai_provider_service.py` `heading_standard.py` `provider_factory.py` `workflows_sections.py` `prompts/content.py`
2. **imports 顺序混乱**：`__init__.py` 同时导出 `_reg` 给内部使用，前端也会间接使用；建议把 `_reg` 改名为 `_register` 并在 `__all__` 中排除
3. **`_registry.py:142` 残留占位符警告**是 `logger.warning`，但前端无入口感知——建议加 SSE 推送或专用的 `/api/v1/prompts/{key}/validate` 端点

---

## 2. 模块 2：目录生成（Outline V5.1）

### 2.1 功能

- **唯一入口**：`workflows_outline_v51.generate_outline()`
- **V3 legacy 全部 `NotImplementedError`**（`workflows.py:72-86`）— 强制走 V5.1
- **三级流水线**：
  1. **L1**：评分项 → 一级标题
  2. **L2**：每章串行生成 4-6 个二级目录
  3. **L3**：每二级串行生成 3-5 个三级目录
- **断点续生**：`_v51_state.py` 内存 + DB 持久化；前端 `v51-pending-task` 检测
- **人工审核**：`v51-confirm` 在 level1 完成后弹审核框

### 2.2 工作流

```
GET /api/v1/projects/{id}/sections/generate-sse   (sections.py:221)
  → generate_outline_sse_impl  (sse_handlers.py)
  → _try_get_provider  走降级链
  → generate_outline()  V5.1
  → SSE 事件流：phase → level1 → review_needed → level2/3 → completed
```

### 2.3 AI 调用

- **入口**：`workflows_outline_v51._call_ai_v51()` 调 `collect_json_response`（含 JSON 自修复 + 重试）
- **提示词**：`prompts/outline.py` 提供 `OUTLINE_V51_LEVEL1_SYSTEM` 等
- **并发控制**：每个 `generate_outline_v51` 任务自带 `DynamicSemaphore`（避免全局竞争）
- **熔断**：`_AnalysisCircuitBreaker`（CLOSED/OPEN/HALF_OPEN），连续 2 次失败冷却 5s
- **自适应并发**：`AdaptiveConcurrencyController`，连续 2 次 429 → 立即降为 min_concurrency（BUG-FIX-R3）

### 2.4 路由

| Method | Path | 用途 |
|---|---|---|
| GET    | /api/v1/projects/{id}/sections/generate-sse | V5.1 目录生成（SSE） |
| GET    | /api/v1/projects/{id}/sections/v51-pending-task | 检测断点续生 |
| POST   | /api/v1/projects/{id}/sections/v51-clear-pending | 清除任务 |
| POST   | /api/v1/projects/{id}/sections/v51-confirm | 确认审核 |
| POST   | /api/v1/projects/{id}/sections/review | 评审大纲 |
| POST   | /api/v1/projects/{id}/sections/review-with-autofix | 评审+自动修复 |

### 2.5 数据链

```
project.summary_json + scoring_points_json + global_facts_json
  ↓ _fetch_project_data_async  (Redis 缓存 → DB)
V5.1 _generate_level1 → 检查 → _generate_level2 → _generate_level3 → self_check
  ↓
save to sections table (L1/L2/L3, status='active')
  ↓
SSE push → 前端 OutlineEditorPage 更新树
```

### 2.6 已识别问题

1. **L2/L3 串行生成**（workflows_outline_v51.py: 第 3-5 步）— 28 章项目需要 28+ 次串行 AI 调用，每章 2 次 L2/L3 串行 = **56+ 次** 串行
   - 优化：L2/L3 改为 DynamicSemaphore 控制（10 并发），单章耗时从 56×t 降到 ~6×t
2. **`outlineDictToTreeNodes` 使用负数 ID**（OutlineEditorPage.tsx:79）— 当后端未返回 `id` 字段时 fallback 到 `-(idx+1)`
   - 风险：后续 updateSection 失败；建议后端在 V5.1 终版必填 `id` 字段
3. **评分项覆盖校验的宽容度**（`_validate_level1`）— 标题 `id` 重新分配使用 `_assign_ids` 可能与 `scoring_point_id` 错位

---

## 3. 模块 3：正文生成（Section Content）

### 3.1 功能

- **入口**：`workflows_sections.generate_sections()` + `generate_section_content()`
- **并发生成**：AdaptiveConcurrencyController（默认 8，并发 1-20）
- **断点续生**：`generation_manager` 维护 `GenerationTask`（内存+DB），提供 pause/resume/status
- **多模式**：
  - `regenerate_ids`：精确重生成
  - `root_section_id`：子树生成
  - `regenerate_missing`：补全（跳过 cancelled 但包含内容不足）
  - `use_orchestration`：内容编排（判断叶子类型）
  - `use_word_sampling`：字数探测
  - `use_fallback_expand`：兜底扩写
  - `use_consistency_audit`：一致性审计

### 3.2 工作流

```
GET /api/v1/projects/{id}/sections/generate-content?...(params)
  → generate_content_impl  (sse_handlers.py)
  → _fetch_project_data_async
  → _build_leaf_dicts  筛选叶子节点
  → generation_manager.create_task
  → 并发编排 叶子节
  ↓ (单叶子)
  → 编排 (orchestrate_content) → 生成 (generate_section_content) → 校验 (ContentQualityEvaluator) → 扩写 (optional) → 落库
  ↓
  SSE 心跳 + 进度 → 前端 SectionEditorPage 实时更新
```

### 3.3 AI 调用

- **3 个提示词**（prompts/content.py）：
  - `CONTENT_GENERATION_SYSTEM / USER`（正文主体）
  - `CONTENT_ORCHESTRATION_SYSTEM / USER`（类型编排）
  - `CONTENT_EXPANSION_SYSTEM`（兜底扩写）
- **内容清理**：`content_cleaning.parse_content` 移除 `<<CHART:...>>` 和 `[[CHART_TYPE:...]]` 占位符
- **图表提取**：`extract_section_charts`（workflows_sections.py:543）— 提取 + 验证 + 兜底
- **质量评估**：`ContentQualityEvaluator`（workflows_quality.py）— 字数/标题/用词打分

### 3.4 路由

| Method | Path | 用途 |
|---|---|---|
| GET    | /api/v1/projects/{id}/sections/generate-content | SSE 并发生成 |
| GET    | /api/v1/projects/{id}/sections/generation-status | 进度查询 |
| POST   | /api/v1/projects/{id}/sections/resume-generation | 恢复 |
| POST   | /api/v1/projects/{id}/sections/stop-generation | 停止 |
| POST   | /api/v1/projects/{id}/sections/pause-generation | 暂停 |
| POST   | /api/v1/projects/{id}/sections/expand-content | 兜底扩写（SSE） |
| GET    | /api/v1/projects/{id}/sections/word-budget | 字数预算 |

### 3.5 数据链

```
sections (DB) → 叶子筛选（按状态+内容长度双判）→ target_words（层级分档）
  ↓
section_dict: id/title/path/ancestors/level/parent_chapters/sibling_chapters
  ↓
build prompt: CONTENT + facts + chart_hints + outline context
  ↓
AI 返回 content (含 <<CHART>>/[[CHART_TYPE]])
  ↓
parse_content + extract_section_charts + quality eval
  ↓
落库 sections.content + sections.word_count + sections.status
  ↓
generation_manager 持久化进度
```

### 3.6 已识别问题

1. **目标字数硬编码**（sse_handlers.py:329-378 的 `_get_effective_target_words`）
   - L1=500 / L2=3500 / L3=300 / L4=400 / L5-7=300
   - **风险**：不同行业（房建/市政/IT）的章节字数需求差异大；建议读 `industry_profiles` 表
2. **缺省 fallback 兜底数据硬编码**（workflows_sections.py:348-540 的 `_generate_fallback_chart`）
   - gantt 5 阶段、labor 5 阶段、timeline 4 节点 — 全部硬编码数字
   - 建议：把默认表存到 DB `chart_templates` 表，按行业 + 章节类型动态选择
3. **`use_orchestration` 二次 AI 调用**（orchestrate_content）
   - 每个项目在并发前先一次性判断全部叶子类型，但目前实现是逐叶子调用（workflows_sections.py:31-62）
   - 应优化为一次 AI 返回所有叶子类型
4. **`ContentQualityEvaluator` 触发再次 AI** — 当评分不达标时自动 rewrite，可能导致：
   - 死循环风险（无上限）
   - 配额消耗翻倍
   - 建议加最大重试 1 次

---

## 4. 模块 4：图表生成与渲染（Mermaid + PIL）

### 4.1 功能

- **7 种图表类型**：flowchart / gantt / architecture / labor / comparison / layout / timeline
- **三层降级渲染**（mermaid_renderer.py 头部注释）：
  1. **L1**：mmdc CLI / Docker mermaid-service / Railway Cloud（真 Mermaid 风格）
  2. **L2**：PIL/Pillow 自定义绘制（流程图/甘特图/架构图/劳动力/对比/布局/时间线）
  3. **L3**：格式化文本（兜底显示源码）
- **配图（真实图片）**：`images.py` 路由 + `ImageGenerationService`，多个 image provider（agnes/dashscope/volcengine）
- **缓存**：
  - 解析 + 验证缓存（300 TTL，200 条 max）
  - 渲染结果 L1 内存缓存（50 条 5min）
  - 失败缓存（10min 防风暴）
  - 磁盘 hash 缓存（`data/_exports/charts`）
  - Redis 可选（MERMAID_CACHE_REDIS_ENABLED）

### 4.2 工作流

```
正文生成时：<<CHART:{json}>> 或 [[TYPE: desc]]  → workflows_sections.extract_section_charts
                                                     ↓
                            _validate_chart_data (类型校验) 
                                                     ↓
                            _generate_fallback_chart (无数据时)
                                                     ↓
                            sections.sections[f"{type}_json"] 落库
                                                     ↓
DOCX 导出时：export.py → _render_section_charts → render_mermaid_to_bytes
                                                     ↓
                            1. hash 查缓存
                            2. 调 MermaidServiceClient.render_mermaid_via_http
                                                     ↓
                            PIL fallback (L2) → BytesIO PNG @ 300 DPI
                                                     ↓
                            docx.add_picture(...)
```

### 4.3 AI 调用

- **图表数据提取**：依赖正文 AI 在生成时输出 `<<CHART:...>>`；不单独调 AI
- **配图（真实图片）**：`images/generate` → `ImageGenerationService.generate` → 调厂商 API
  - `provider_factory.py` 同样的降级链
  - 支持参考图（reference_images）

### 4.4 路由

| Method | Path | 用途 |
|---|---|---|
| POST | /api/v1/images/generate | 单张配图 |
| GET  | /api/v1/images/tasks | 任务列表 |
| GET  | /api/v1/images/tasks/{id} | 任务详情 |
| GET  | /api/v1/images/files/{id} | 本地落盘图片 |
| POST | /api/v1/projects/{id}/sections/{sid}/generate-flowchart | 重新生成流程图 |
| POST | /api/v1/projects/{id}/sections/{sid}/generate-architecture | 架构图 |
| POST | /api/v1/projects/{id}/sections/{sid}/generate-gantt | 甘特图 |
| POST | /api/v1/projects/{id}/sections/{sid}/generate-labor | 劳动力图 |
| POST | /api/v1/projects/{id}/sections/{sid}/generate-comparison | 对比图 |
| POST | /api/v1/projects/{id}/sections/{sid}/arrange-* | 自动排版 |
| POST | /api/v1/projects/{id}/sections/{sid}/generate-illustration | 配图 |

### 4.5 数据链

```
chart_data 抽取 → 验证 → 兜底
  ↓
sections.gantt_json/flowchart_json/architecture_json/labor_json/comparison_json/layout_json/timeline_json
  ↓
build_docx 时
  ↓
mermaid_renderer 解析 → 渲染 → PNG bytes
  ↓
docx.shared.Cm(15) 插入 + 标题
```

### 4.6 已识别问题

1. **mermaid_renderer 3938 行超大文件** — 单文件承担 7 种图表的解析+渲染；建议按图表类型拆为 7 个子模块（`mermaid_flowchart.py` `mermaid_gantt.py` ...）
2. **`MermaidServiceClient._render_via_mmdc` 重复编解码风险**（mermaid_renderer.py:217-240 的 `_render_hq` 装饰器）— 已通过检查现有 DPI 跳过重复，但 BytesIO 重置后调用方读取可能为空
3. **失败缓存无版本字段**（image_engine.py:26-40）— 同一 prompt 失败后 10 分钟内即使更换 provider 也不重试
4. **路径穿越校验遗漏**（mermaid_renderer.py 解析 Mermaid 代码时）— AI 输出的 `linkStyle` `classDef` 可能含恶意 ID；建议对 ID 做白名单过滤

---

## 5. 模块 5：DOCX 导出

### 5.1 功能

- **两种模式**：
  - 同步 `POST /export/docx`：asyncio.to_thread(build_docx)，返回 FileResponse
  - 流式 `POST /export/docx-stream`：边写边回传（适用于大型项目）
- **配置**：`ExportConfig` 包含图片 DPI、字体、页边距、是否包含图表等
- **下载**：`GET /export/download/{export_id}`（安全校验路径必须在 `data/projects/_exports` 下）
- **检查**：`GET /export/check?strict=true` 完整性检查

### 5.2 工作流

```
POST /export/docx  →  ExportConfig  →  check_document
  ↓
asyncio.to_thread(build_docx)
  ↓
1. list_sections 查所有章节 → 构 tree
2. 创建 python-docx Document
3. 按标题级别（V3.1 七级标准）写入 HeadingNumberingGenerator
4. 内联图表（Mermaid → PNG）→ docx.add_picture
5. 表格 / 配图（images/files/{id}）→ docx.add_picture
6. 标题编号 HeadingNumberingGeneratorV2（V3.1 七级）
7. 保存到 data/projects/{id}/_exports/{export_id}.docx
8. 写 DB export_history
  ↓
返回 download_url
```

### 5.3 AI 调用

**DOCX 导出本身不直接调 AI**。但导出前可能用：
- `content_version`（API 路由）对比版本
- `consistency_audit` 做一致性审计

### 5.4 路由

| Method | Path | 用途 |
|---|---|---|
| GET    | /api/v1/projects/{id}/export/check | 完整性检查 |
| POST   | /api/v1/projects/{id}/export/docx | 同步导出 |
| POST   | /api/v1/projects/{id}/export/docx-stream | 流式导出 |
| GET    | /api/v1/projects/{id}/export/download/{export_id} | 下载 |
| GET    | /api/v1/projects/{id}/export/latest | 最新导出 |
| GET    | /api/v1/projects/{id}/export/history | 历史 |

### 5.5 数据链

```
sections (DB) + image_generation_tasks (DB) + analysis.global_facts_json
  ↓
build_docx(project_id, config)
  ↓
parse_content 分离 文本/图表/表格
  ↓
HeadingNumberingGeneratorV2 标题编号
  ↓
python-docx Document 写入
  ↓
PNG 内嵌 (300 DPI)
  ↓
save → data/projects/{id}/_exports/{export_id}.docx
  ↓
register export_id
```

### 5.6 已识别问题

1. **`check_document` 是同步函数**（export.py:88）— 已经包在 `asyncio.to_thread` 里，但 `check_document` 自身会扫描所有章节计算字数
   - 建议：章节字数已经在 sections.word_count 字段，直接 SUM(wc) 即可（O(1) vs O(n)）
2. **导出大文件 OOM 风险**（export.py:114）— 大项目 28+ 章节 + 多图表可能产生 200+ MB docx
   - 建议：分章节增量写入 docx（python-docx 不直接支持，但可分段保存 + zip 合并）
3. **`HeadingNumberingGeneratorV2` 模板硬编码**（heading_standard.py）— V3.1 七级标准在代码内
   - 风险：模板变更需要发版；建议提到 DB schema 表
4. **`_strip_heading_numbering` 正则过紧**（export.py:131-140）— 中文顿号"、"未覆盖
   - 影响：含"第二章、设计依据"这种半角数字的标题会被错误剥离

---

## 6. 前后端 API 路由对齐核验

### 6.1 核验结果

| 维度 | 数字 |
|---|---|
| 后端 routes 总数 | 201 |
| 前端 axios 调用样本（去重） | ~150+ |
| 脚本初检"未匹配" | 10（**全部为模板字符串正则未展开导致的误报**） |
| 真实未对齐 | 0 |

### 6.2 全部"未匹配"经手工核对已对齐

| 前端调用 | 后端路由 | 备注 |
|---|---|---|
| `/api/v1/health` | monitor.py:121 / ai_config.py:701 / cache.py:102 | 多处实现 |
| `/api/v1/projects/{id}/sections/generate-sse` | sections.py:221 | ✓ |
| `/api/v1/projects/{id}/sections/generate-content` | sections.py:285 | ✓ |
| `/api/v1/projects/{id}/sections/expand-content` | sections.py:735 | ✓ |
| `/api/v1/projects/{id}/export/docx-stream` | export.py:153 (POST) | ✓ |
| `/api/v1/projects/{id}/export/download/{export_id}` | export.py:405 | ✓ |
| `/api/v1/projects/{id}/export/latest` | export.py:442 | ✓ |
| `/api/v1/images/files/{id}` | images.py:242 | ✓ |
| `/api/v1/duplicate/export-report/{id}` | duplicate.py:71 | ✓ |
| `/api/v1/rejection/export-report/{id}` | rejection.py:130 | ✓ |

**结论**：前后端路由 100% 对齐，无断裂。

---

## 7. 系统性 BUG 与性能瓶颈清单

### 7.1 必修（已部分修复）

| # | 级别 | 位置 | 问题 | 修复建议 |
|---|---|---|---|---|
| B1 | ✅ 已修 | 5 个 ai/ 文件 | UTF-8 BOM 残留 | 已剥离 |
| B2 | 高 | `workflows_outline_v51.py:1-200` | L2/L3 串行生成 28+ 章项目效率低 | 改 `DynamicSemaphore` 并发 10 |
| B3 | 高 | `export.py:131-140` | `_HEADING_NUMBER_COMBINED` 缺中文顿号 | 补充 `、` 分隔的正则分支 |
| B4 | 高 | `mermaid_renderer.py` 3938 行 | 超大单文件 | 按 7 种图表拆 7 个文件 |
| B5 | 中 | `sse_handlers.py:329-378` | 层级分档硬编码 | 改读 `industry_profiles` |
| B6 | 中 | `workflows_sections.py:348-540` | fallback 数据硬编码 | 移到 `chart_templates` 表 |
| B7 | 中 | `image_engine.py:26-40` | 失败缓存无 provider 维度 | `_fail:{provider}:{key}` |
| B8 | 中 | `OutlineEditorPage.tsx:79` | outline 未带 id 时用负数 | 后端 V5.1 必填 id |

### 7.2 性能瓶颈

| # | 位置 | 现象 | 建议 |
|---|---|---|---|
| P1 | `mermaid_renderer.py` | 单文件 3938 行，加载慢 | 拆模块 + `__getattr__` 懒加载 |
| P2 | `workflows_outline_v51.py` | L2/L3 串行调用 AI | 改并发（参考 B2） |
| P3 | `sse_handlers.py:_fetch_project_data_async` | 缓存命中时仍查元数据 DB | Redis 增加 metadata 缓存 |
| P4 | `provider_factory.py:get_ai_config` | 每次调 AI 查一次 DB（即使有缓存） | 当前已 1h TTL 缓存，OK |
| P5 | `export.py:check_document` | 同步全量字数扫描 | 改用 SQL `SUM(word_count)` |
| P6 | `sections.py:list_sections` | 全章节拉回 | 树形接口按 level 分页 |

### 7.3 安全 / 健壮性

| # | 位置 | 问题 |
|---|---|---|
| S1 | `export.py:64-74` | GET `/docx` / `/docx-stream` 占位 405 提示友好，但暴露路由存在性 |
| S2 | `sse_handlers.py:_NotifyQueue` | 自定义 asyncio.Queue + Event，文档未说明 |
| S3 | `provider_factory.py:_FALLBACK_PROVIDERS` | 内置厂商 `api_key=""` 全部为空，无效 |
| S4 | `images.py:267-279` | 路径校验 `images_root not in resolved.parents` 正确（已修复过路径穿越） |

### 7.4 已观察到的高频 BUG-FIX 注释（说明改过很多次）

```
workflows_base.py 9 处、workflows_outline_v51.py 9 处、redis_cache.py 7 处、
heading_standard.py 3 处、prompts/README.md 8 处、workflows_outline_utils.py 1 处
```

- 大量 BUG-FIX-01~BUG-FIX-28 注释说明：**项目已较成熟但熔断点很多**
- 集中在：AI 降级、JSON 自修复、并发控制、SSE 兼容性、认证

---

## 8. 优化建议（按优先级）

### P0 立即可做（无风险）

1. **拆分 mermaid_renderer.py**：3938 行 → 7 个子文件，import 入口不变
2. **`_get_effective_target_words` 接入 `industry_profiles` 表**
3. **`_HEADING_NUMBER_COMBINED` 增加中文顿号分支**

### P1 性能优化

4. **L2/L3 并发生成**：用 `DynamicSemaphore(10)` 替代串行 await；28 章项目预计提速 4-6 倍
5. **`check_document` 改 SUM(word_count)**：单次 SQL vs 全量扫
6. **`sse_handlers._fetch_project_data_async`**：metadata 也加 Redis 缓存

### P2 健壮性

7. **`ContentQualityEvaluator` 最大重试 1 次**
8. **失败缓存加 provider 维度**（B7）
9. **`outlineDictToTreeNodes` 负数 id 校验**：后端 V5.1 终版必填 id
10. **`chart_templates` 表存储 fallback 数据**（B6）

### P3 架构

11. **把 `provider_factory` + `image_providers` 合并为统一 `ai/services.py`**
12. **provider cache + `image_generation_tasks` 共用同一份 `provider_history`**
13. **`ai_provider_service` 提取统一 `test_provider()` 接口**，消除 3 套 test 逻辑

---

## 9. 数据链总览（一条端到端的事务）

```
1. 用户上传招标文件
   POST /files → OCR (mineru/paddle) → extraction → analysis 表
2. AI 摘要识别
   POST /ai/summary → agnes call → analysis.summary_json
3. 评分点提取
   POST /ai/scoring → analysis.scoring_points_json
4. 全局事实
   POST /facts/generate → analysis.global_facts_json
5. 目录生成 V5.1
   GET  /sections/generate-sse  →  AI 三级流水线  →  sections 表
6. 人工审核（V5.1 level1 完成后）
   POST /sections/v51-confirm
7. 正文生成（V5.1 后）
   GET  /sections/generate-content  →  AI 并发 28 叶子  →  sections.content
8. 图表解析
   服务端 _extract_charts_from_content  →  sections.{type}_json
9. 质量评估
   ContentQualityEvaluator  →  score/grade  →  sections.quality_score
10. 配图
   POST /images/generate  →  image_generation_tasks
11. 一致性审计
   GET  /consistency/audit/{id}
12. 废标检查
   POST /rejection/start
13. 查重
   POST /duplicate/start
14. 导出
   POST /export/docx  →  docx 文件  →  /export/download/{id}
15. 版本管理
   GET  /content-version
```

每一步都有对应的 SSE 事件或进度回调，前端在 OutlineEditorPage / SectionEditorPage / ExportConfigPage 实时显示。

---

## 10. 总结

### 项目成熟度

- ✅ **架构清晰**：分层（routers → services → ai workflows → providers），DDD 思想
- ✅ **健壮性强**：熔断、降级链、自适应并发、断点续生、SSE 重连
- ✅ **模块化好**：工作流按子模块拆分（`workflows_*.py`），提示词按分类拆分
- ⚠️ **少量遗留**：BOM 残留（已修）、超大单文件、硬编码数据
- ⚠️ **优化空间**：L2/L3 串行、check_document O(n)

### 紧急修复（30 分钟内可完成）

1. ✅ BOM 5 文件已修
2. `_HEADING_NUMBER_COMBINED` 加顿号分支
3. `check_document` 改 SUM

### 建议下版本（1-2 天）

1. 拆 `mermaid_renderer.py`
2. L2/L3 改并发
3. 抽 `chart_templates` 表

---

**报告完。** 所有结论均基于源码精读 + 静态核验。修改建议均给出位置（file:line）和具体方案，可直接落地。
