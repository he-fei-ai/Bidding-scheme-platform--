# 招投标方案平台 · 软件架构与 OpenBidKit 易标差距对照报告

- **分析日期**：2026-10-12
- **分析范围**：`J:\编程\招投标方案平台\`（含 backend / frontend / easybidding-service / acl-repair / 平台智能体设计 / 图表提示词 / scripts / translations / data / doc / docs / deliverables）
- **分析方式**：只读；结论均附文件路径与行号（未标注行号者以"文件存在性 + 关键词命中"佐证）
- **对照基线**：OpenBidKit 易标（下文简称"易标"）。参考点在代码中被大量引用（`backend/app/routers/*.py` 中 `ENHANCE 2026-09-14/15` 注释），且 `easybidding-service/` 目录保留了易标的 Node 版图表校验服务

---

## A. 软件整体架构

### A.1 目录结构（顶层 + 1 层子目录）

```
招投标方案平台/
├── backend/                     # Python/FastAPI 后端（核心）
│   ├── app/                     #   业务代码
│   │   ├── routers/             #   37 个 API 路由（含 sse/ 子目录）
│   │   ├── services/            #   业务服务层（50+ 文件，含 ai/ charts/ rag/）
│   │   ├── tasks/               #   Celery 异步任务
│   │   ├── middleware/          #   CORS / SPA fallback / 延迟监控
│   │   ├── security/            #   认证（JWT / API Key）
│   │   ├── projects/            #   项目管理
│   │   ├── utils/               #   工具
│   │   ├── main.py              #   FastAPI 入口（837 行）
│   │   ├── models.py            #   Pydantic 数据模型（20 KB）
│   │   ├── config.py  db.py  db_pool.py  db_migrations.py
│   │   └── celery_app.py  health_check.py  logging_context.py
│   ├── tests/                   #   pytest 测试（含大量回归测试）
│   ├── data/  cache/  logs/     #   运行期数据
│   └── Dockerfile  requirements.txt  pyproject.toml
├── frontend/                    # React + TypeScript + Vite
│   ├── src/
│   │   ├── pages/               #   26 个页面（含 DOM 测试）
│   │   ├── components/          #   17 个组件
│   │   ├── api/index.ts         #   2200+ 行 API 客户端
│   │   ├── types/index.ts       #   853 行类型定义
│   │   ├── services/  hooks/  styles/  vite-plugins/
│   │   └── App.tsx  main.tsx  i18n.ts  utils.ts
│   └── package.json  vite.config.ts  Dockerfile
├── easybidding-service/         # 易标 Node.js 图表校验子服务（保留移植参考）
│   └── src/services/            #   ChartService / ChartCache / chartValidators / technicalPlanService
├── acl-repair/                  # Agent 模式一致性修复工作区（仅 jsonl 日志）
├── 平台智能体设计/              # 易标方法论文档 14 篇
├── 图表提示词/                  # 图表生成提示词 15 篇（架构图/流程图/甘特图/劳动力/行业树）
├── charts/                      # 图表 HTML 示例 3 个
├── translations/                # 国际化资源（en-US.json / zh-CN.json）
├── scripts/                     # 部署/探针脚本 10 个
├── data/  doc/  docs/  deliverables/  analysis_reports/  logs/  test_data/
├── backend_autostart.bat        # 后端守护（168 行 watchdog，自动重启 + 健康检查）
├── start_all.bat  启动.bat      # 一键启动
└── docker-compose.yml           # 5 服务编排（backend / frontend / redis / celery_worker / celery_beat）
```

### A.2 技术栈（依据 `技术架构文档.MD:16-46` + `docker-compose.yml`）

| 层 | 技术 |
|---|---|
| 前端 | React 18.3 + TypeScript 5.x + Vite 6.x + Ant Design 5.22 + Zustand + Axios + EventSource |
| 后端 | Python 3.12 + FastAPI 0.115 + uvicorn + aiosqlite + httpx + cryptography + Pillow + python-docx |
| 缓存 | Redis 7 (Alpine) + 内存 LRU 降级 |
| 异步 | Celery + Redis broker（`celery_worker` / `celery_beat` 容器） |
| AI 文本 | OpenAI / Anthropic / DeepSeek / SiliconFlow / 通义 / 智谱 / Agnes（`services/ai/providers/`） |
| AI 图像 | 8 家 Provider：agnes / baidu / dashscope / hunyuan / stepfun / volcano / zhipu（`services/ai/image_providers/`） |
| 图表 | Mermaid（`mermaid_renderer.py` + `sanitizer` + 6 个 `mermaid_*.py` 子渲染器）+ 自建 HTML/SVG 渲染 |
| 解析 | python-docx + pdfplumber（表格感知）+ PyPDF + Tesseract OCR |
| 数据库 | SQLite（`data/bid_assistant.sqlite3`）+ Chroma 向量库（`data/chroma/`） |
| 部署 | Docker Compose + Windows 守护脚本；后端 8000 / 前端 5173 |

### A.3 启动方式

- `backend_autostart.bat`（168 行 watchdog v2.0）：
  - `python -m uvicorn app.main:app --host 127.0.0.1 --port 8000`
  - 每 5 分钟调 `/api/v1/health` 全量健康检查，异常 `taskkill + restart`
  - 每小时最多重启 10 次；日志滚动保留最近 1000 行
- `docker-compose.yml`：5 服务 + 2 卷（`backend_data`、`redis_data`）；后端 memory limit 2G
- `start_all.bat`（8.2 KB）、`启动.bat`（5.7 KB）：一键联启脚本

### A.4 主要功能模块清单（UI 页面 ↔ 后端路由）

| UI 页面 | 后端路由 |
|---|---|
| `ProjectListPage.tsx` | `routers/projects.py` |
| `FileUploadPage.tsx` | `routers/files.py` |
| `SummaryReviewPage.tsx`（含 facts） | `routers/ai.py` + `routers/facts.py` |
| `OutlineEditorPage.tsx` | `routers/sse/sse_outline.py` |
| `SectionEditorPage.tsx` | `routers/sections.py` + `routers/sse/sse_content.py` |
| `ExportConfigPage.tsx` | `routers/export.py` |
| `KnowledgeBasePage.tsx` | `routers/knowledge.py` |
| `IndustryTreePage.tsx` | `routers/industry_tree.py` |
| `PromptEditorPage.tsx` + `PromptMetricsPanel.tsx` | `routers/prompts.py` |
| `AIConfigPage.tsx` `TextConfigPage.tsx` `ImageConfigPage.tsx` | `routers/ai_config.py` + `routers/images.py` |
| `ConsistencyPage.tsx` | `routers/consistency.py` |
| `RejectionCheckPage.tsx` | `routers/rejection.py` |
| `DuplicateCheckPage.tsx` | `routers/duplicate.py` |
| `WordCountPage.tsx` | `routers/word_count.py` |
| `QAPage.tsx` | `routers/qa.py` |
| `FeasibilityReportPage.tsx` | `routers/feasibility.py`（易标移植） |
| `CacheMonitorPage.tsx` | `routers/cache.py` + `routers/monitor.py` |
| `ApiKeyManagementPage.tsx` | `routers/api_keys.py`（开放平台 API Key） |
| `TemplatePage.tsx` | `routers/templates.py` |

---

## B. 关键模块定位

| # | 模块 | 主要文件 | 入口函数/组件 | 主要职责 |
|---|------|---------|--------------|---------|
| 1 | 招标文件上传与解析 | `backend/app/routers/files.py` + `services/files.py`（2565 行）+ `docx_extractor.py`（633 行）+ `ocr_parser.py` | `save_upload` / `_do_extract_text(:769)` / `extract_text_from_path` / pdfplumber 表格感知(:998) | 多格式上传（doc/docx/pdf/img）、类型嗅探、编码检测、表格标记、按页页码 |
| 2 | 项目概述/评分/事实提取 | `routers/ai.py` + `services/ai/workflows_analysis.py` | `generate_tender_summary` / `extract_scoring_points` / `extract_global_facts` / `analyze_boq` / `analyze_work_interface` / `analyze_construction_method` | AI 摘要、评分点两阶段审查、全局事实三档策略、工程量/工作界面/工程作法分析 |
| 3 | 技术评分要求提取 | 同上 + `services/scoring_compliance.py` | 内置于 `extract_scoring_points` | 评分点结构化，100 分自检、漏项检测 |
| 4 | 知识库 | `routers/knowledge.py` + `services/knowledge.py` + `services/rag/` | `KnowledgeItem` 模型 | 文档 → 条目（title/summary/content/keywords/tags/industry），Chroma 向量库检索 |
| 5 | 全局事实模块 | `routers/facts.py` + `services/facts_service.py` | `GlobalFact` 模型 + 三档 `missing_value_policy`（fabricate/omit/placeholder） | 移植自易标 `globalFactsTask`；覆盖六大类：人员角色、工期/运维、金额/合同、技术参数、评分规则、资质证明 |
| 6 | 目录/大纲生成 | `routers/sse/sse_outline.py` + `services/ai/workflows_outline_v51.py`（5190 行）+ `outline_common.py` + `outline_gate.py` + `outline_sort_optimizer.py` + `workflows_outline_utils.py` | `generate_outline_v51(:4813)` / `generate_outline_direct(:158)` / `should_use_direct_outline(:104)` | 分步生成（L1 AI → 审核 → L2 骨架+并发填充 → 审核 → L3 规则兜底 → 门禁 → 最终审核）+ 短标书直出路径 |
| 7 | 目录编辑与编号 | `frontend/src/pages/OutlineEditorPage.tsx` + `services/ai/outline_gate.py::renumber_outline_ids(:260)` + `workflows_outline_utils.py::_renumber_items(:272)` | `services/sections.py::get_section_path(:928)` | 位置式点号编号（1/1.1/1.1.1）、`number` 字段动态派生、L3 标题质量清洗 |
| 8 | 正文编排（图表/知识库匹配） | `services/ai/workflows_orchestration.py` + `services/ai/image_engine.py` + `services/knowledge.py` | 图文编排入口 | 图表候选注入、知识库素材召回、章节字段契约注入 |
| 9 | 正文生成 | `services/ai/workflows_sections.py` + `workflows_expand.py` + `workflows_quality.py` + `routers/sse/sse_content.py` | `build_chapter_content_messages` + `generate_sections` | 并发主循环、暂停/续跑、单章重生成、字数档位（brief/standard/detailed/long）、内容自检 |
| 10 | 配图 | `services/ai/mermaid_renderer.py`（3938 行）+ 6 个 `mermaid_{flowchart,gantt,architecture,labor,comparison,layout,timeline}.py` + `mermaid_sanitizer.py` + `image_providers/`（8 家） | `render_mermaid_to_bytes` / `_render_commercial` | 7 类 Mermaid 图 + AI 生图（暗标合规下自动禁用）、渲染失败回滚、图号管理 |
| 11 | 一致性校核/审核 | `services/consistency_auditor.py` + `consistency_rules.py` + `consistency_repair.py` + `consistency_backflow.py` + `agent_repair.py` + `text_patch.py` + `routers/consistency.py` | `run_full_audit` / `suggest_fixes` / `apply_autofix` / `repair_with_ai` / Agent 模式 | 事实基线 + 冲突检测 + 三档严重度 + 确定性 autofix + AI 补丁修复 + Agent 修复（`acl-repair/`） |
| 12 | 提示词管理 | `services/ai/prompts/` 目录：`_registry.py` + `_cache.py` + `_shared.py` + `_security.py` + `_context.py` + `_output_repair.py` + 10 个阶段子模板 + `chapter_title_templates.json`（46 KB，28 顶层专项方案类别） | `routers/prompts.py`：列表/详情/保存/重置/版本/回滚/指标/告警/成本/预算 | 60 个注册项（57 提示词 + 3 共享规则），提示词版本控制（≤50 版本）、跨变量注入中和、脱敏 |
| 13 | 导出文档 | `routers/export.py`（40 KB，626 行）+ `services/export.py` + `export_presets.py` + `pdf_export.py` + `quality_rules.py` | 同步导出 / SSE 流式 / 断线续传 / PDF 转换 / 预检（`check_document`） | DOCX 构建 + 图表渲染（图号章号）+ 原子写盘 + 内容指纹缓存 + 版式/主题预设（易标 `EXPORT_LAYOUT_PRESETS` 移植） |
| 14 | 图表 | `services/ai/mermaid_*.py`（7 类图）+ `easybidding-service/src/services/ChartService.ts`（Node 版保留）+ `charts/`（HTML 示例）+ `图表提示词/`（15 篇方法论） | `arrangeArchitecture` / `detectArchitectureSubtype`（Node 侧）；`image_engine.py`（Python 侧编排） | 7 类图表（flowchart/gantt/architecture/labor/comparison/layout/timeline），架构图 4 子类型（organization/wbs/system/infrastructure） |

---

## C. 数据结构与 API 接口

### C.1 Outline / Section 数据结构

**后端 Pydantic 模型** `backend/app/models.py:97-125` `Section`：

```
id / project_id / parent_id / title / level / sort_order
section_type / scoring_point_id / ai_prompt_snapshot
flowchart_json / gantt_json / architecture_json / labor_json /
comparison_json / layout_json / timeline_json / inlined_chart_json
illustration_type / illustration_data / content / target_words
word_count_tier (brief|standard|detailed|long) / status / word_count
created_at / updated_at
```

**前端类型** `frontend/src/types/index.ts:97-115` `SectionTreeNode`：`id / title / level / sort_order / section_type / scoring_point_id / target_words / word_count_tier / tier_label / tier_target / cumulative_word_count / status / children / has_content / word_count / chart_flags[]`

**编号方案**：结构 `id`（点号 1/1.1/1.1.1）由 `outline_gate.renumber_outline_ids` 位置式派生；显示 `number` 字段由 `_renumber_items` 附加，`title` 本身不含编号（`normalize_outline_items` 按 DFS 深度重算 `level`，不信任 AI 返回值）。

### C.2 Chapter 数据结构

无独立 `Chapter` 类；章节即 `Section`（`level=1` 即 Chapter）。九大章节标准由 `backend/app/services/content_field_contract.py:53-99` `NINE_CHAPTERS` 定义，通过 `theme + keywords` 描述（工程概况 / 施工组织 / 主要分部分项 / 专项施工方案 / 施工进度 / 资源配置 / 质量体系 / 安全文明 / 应急预案），配合 `BUILTIN_CHAPTER_KEYWORD_ALIASES`（跨模板同义词，:118-128）和 `INDUSTRY_CHAPTER_KEYWORDS`（行业专项词表，11 个行业，:131-）。

### C.3 GlobalFacts / KnowledgeEntry 数据结构

**`GlobalFact`**（`models.py:61-78`）：`key / label / value / category / source / verified / needs_manual_input`。三大字段契约（`content_field_contract.py`）：`EXTRACTION_PROJECT_FIELDS`（13 项）、`GLOBAL_FACTS_FIELD_GROUPS`（按九大章节分组）、`KEY_CONSISTENCY_FIELDS`（跨章节唯一权威值）。

**`KnowledgeItem`**（`models.py:533-549` + `types/index.ts:708-719`）：`title / summary / content / keywords[] / industry / tags[] / source_doc_id / source_ref`。

### C.4 AI 请求封装

- **客户端**：`services/ai/providers/` 目录下 `base.py` / `openai_compatible.py`（httpx 复用 + 429 独立预算）/ `anthropic_compatible.py` / `deepseek.py`
- **服务端入口**：`services/ai/ai_provider_service.py` + `provider_factory.py`（三级降级：当前配置 → 最近使用 → 内置兜底）
- **限流/并发**：`services/ai/rate_gate.py`（RPM 令牌桶 + AIMD 自适应并发）；`workflows_base.py` 的 `AdaptiveConcurrencyController`（FAST<8s / SLOW>30s，窗口 20）+ `AnalysisCircuitBreaker`（失败阈值 2，冷却 5s）
- **任务级信号量**：`generate_outline_v51(:4987)` 自建 `DynamicSemaphore(16/32)`（替代全局变量）
- **SSE 推送**：`routers/sse/` 目录（`sse_outline.py` / `sse_content.py` / `sse_charts.py` / `sse_db_ops.py` / `sse_utils.py` / `sse_shared.py`）

### C.5 提示词加载机制

- **注册**：`prompts/_registry.py::_reg(key, category, label, value)` 写入 `_ALL_PROMPTS`，同时 `extract_variables` 提取 `{var}` 与 `__CONSTANT__`
- **渲染**：`render_prompt`（单遍替换、大小写不敏感、共享注入 `{shared:NAME}`）；`render_prompt_safe`（含 `sanitize` / `desensitize` opt-in）
- **缓存**：`_cache.py::get_prompt`（DB 优先 → 硬编码回退 → 冷却期 → RLock 线程安全）
- **上下文**：`_context.py::assemble_prompt_context` / `build_chapter_context` / `truncate_to_tokens`（2026-10-11 修 C9：ASCII 预算浪费 75%）
- **版本管理**：`prompt_versions` 表，每 key ≤50 版本；`routers/prompts.py` 支持保存/重置/回滚/成本预算/告警
- **共享规则**：`SHARED_HEADING_RULES` / `SHARED_FORBIDDEN_WORDS` / `SHARED_OUTPUT_SPEC`

---

## D. 与 OpenBidKit 易标参考方案的差距分析

代码中大量引用易标作为移植参照（`ENHANCE 2026-09-14/15` 注释 40+ 处），差距已在报告中标注为"补上易标没有的能力"或"移植易标能力"。

### D.1 已具备能力（本软件已实现，标注易标对应实现）

| 能力 | 本软件位置 | 易标对应 |
|---|---|---|
| 短标书直出三级目录 | `config.py:163` `DIRECT_OUTLINE_*` + `workflows_outline_v51.generate_outline_direct(:158)` | 易标规格二.2 |
| 全局事实三档策略（fabricate/omit/placeholder） | `models.py:84-94` `FactsGenerateRequest` + `facts_service.py` | `globalFactsTask` |
| PDF 按页提取 + 页码标识 + 表格标记 | `services/files.py:998-1090` | 易标规格一.1 |
| 缓存命中率监控 | `routers/ai_config.py:770-` | 易标规格九 |
| Agent 模式全文一致性修复 | `routers/consistency.py:380-` | 易标 D1「Agent 修复」 |
| 导出格式预设（版式 + 主题） | `routers/export.py:694-` + `services/export_presets.py` | `exportFormatPresets` |
| 查重六 Sheet Excel 导出 | `routers/duplicate.py:128-` | `checkResultExportService.cjs` |
| 废标项三轮流程 + 一键替换错别字 | `services/rejection.py:543-` `routers/rejection.py:219-` | `rejectionCheckTask.cjs` |
| 可研报告六步工作流（4 后台任务） | `routers/feasibility.py` + `services/feasibility_*.py` | `feasibility-report` 模块 |
| 后台任务快照对账（`recoverInterruptedOutlineGenerationTask`） | `main.py:737-` + `routers/tasks.py:293-` | 易标同名任务 |
| AI 生图硬上限 + 表格密度 + Mermaid 优先 | `routers/sse/sse_content.py:161-` | 易标 `useAiImages` / `maxAiImages` |
| 提示词缓存命中收益验证 | `services/ai/cache_metrics.py` | 易标"提示词顺序优化，让缓存命中" |
| 智能问答（Q&A 基于项目上下文） | `routers/qa.py` + `QAPage.tsx` | 无明确对标 |
| API Key + 开放平台接入 | `routers/api_keys.py` + `ApiKeyManagementPage.tsx` | 主要功能.txt 第 8 条 |
| 多用户隔离 + 认证依赖注入 | `security/auth.py` + `main.py` 认证依赖注入修复 | 未见对应 |
| 国际化 | `translations/en-US.json` + `zh-CN.json` + `services/i18n.py` | 未见对应 |

### D.2 缺失或明显弱化的能力

| 能力 | 说明 | 依据 |
|---|---|---|
| 完整 MCP 端点 | 主要功能.txt 声称"提供 API Key 与 MCP 端点"，但 `routers/` 中未见 `mcp.py`；只有 `api_keys.py`（API Key 认证）+ `api_docs.py` | 目录 grep 未见 `mcp` |
| 章节字数分档行业化 | `analysis_reports/项目深度探索与BUG优化报告_v5.md:248-250` 已记录 `L1=500/L2=3500/L3=300` 硬编码，未接 `industry_profiles`；`word_count_tier` 是全局四档，未按行业/评分点差异化 | 同上 |
| 图表校验体系分裂 | `报告_正文生成模块第二轮深度审计与修复_2026-10-08.md:114` 明确"两套图表校验体系均为死代码"（`chart_validators.py` 7 函数 + `workflows_sections._validate_chart_data` 零调用） | 同报告 |
| 图号章号取节点自身层级序号 | 已修（E-1，`report 2026-10-11`），但仍有 `parent_section_number` 为空时"图default-1"格式腐化边缘 | 报告 2026-10-11 §8.5 |
| 一致性 Agent 修复工作区 | `acl-repair/` 目录只有 jsonl 日志，未见持久化产物结构 | 目录列表 |
| 端到端实机验证 | 多份报告声明"环境无 Node/AI Key，未跑通整链 HTTP" | 报告 2026-10-11 提示词 §9.3 |
| 前端组件级/Tab 级交互测试 | 报告 2026-10-09 §七 P-4 声明"环境受限未执行" | 同上 |

### D.3 本软件独有能力（易标未见对应）

| 能力 | 依据 |
|---|---|
| **九大章节标准字段契约**（`NINE_CHAPTERS` 9 章 + 4 层词表） | `content_field_contract.py:53-128` |
| **12 个行业知识库 + 行业专项词表**（消防/弱电/电力/装饰/市政/设备/物业/公水检测/电梯/石化/广告/通用） | `industry_profiles.py:17-` + `content_field_contract.py:131-` |
| **28 顶层专项方案标题模板**（`chapter_title_templates.json` 46 KB） | `backend/tests/test_chapter_title_templates.py:24` 明确"约 28 个顶层专项方案类别" |
| **一致性校对 Agent 模式**（工作区文件 + 事实底稿 + 冲突清单） | `agent_repair.py:117-` |
| **三档一致性规则**（严重/中等/轻微，`consistency_rules` 单一事实来源） | `routers/consistency.py:71-` |
| **AI 配置审计日志 + 每日清理**（默认保留 90 天） | `main.py:768-792` |
| **防雷同 + 防串标三重机制**（bigram+Dice 查重、图像双指纹、同标段素材唯一占用） | 主要功能.txt 第 7 条 |
| **暗标合规自动锁定无图模式 + 逐字脱敏** | 主要功能.txt 第 6 条 |
| **可行性报告独立模块**（单工作区六步） | `routers/feasibility.py` |
| **行业树 3 级种子**（12 知识库对齐）+ AI 扩展子节点 | `routers/industry_tree.py` + `IndustryTreePage.tsx` |

---

## E. 建筑工程专项施工方案场景适配点

### E.1 九大章节结构定义

- **权威定义**：`backend/app/services/content_field_contract.py:53-99` `NINE_CHAPTERS`（idx=1..9，每章含 `theme` + `keywords`）
- **测试锚点**：`backend/tests/test_content_field_contract_2026_10_07.py:84-87` `test_nine_chapters`（断言 `range(1,10)`）
- **章节→字段映射**：`CHAPTER_FIELD_MAP`（`content_field_contract.py`），每章字段按 `chapters=[N]` 归属，`"*"` 表示全局
- **行业化扩展**：`BUILTIN_CHAPTER_KEYWORD_ALIASES`（每章 4-8 词同义词）+ `INDUSTRY_CHAPTER_KEYWORDS`（11 个行业专项词表，每章 2-7 词）+ `register_chapter_keywords()`（运行时注册扩展）
- **章节分类算法**：`classify_chapter(title, industry)`（`content_field_contract.py:243-276`），按关键词长度累加计分

### E.2 六大类危大工程覆盖点

**未找到**独立常量（`SIX_CATEGORY_*` / `DANGER_*` 未定义）。六大类危大工程能力**内嵌在行业模板库**中，通过 `db.py` 内置的行业标准目录实现：

| 危大类别 | 覆盖位置 |
|---|---|
| 深基坑 | `db.py:2253` / `2420` / `2513`（房建、高层、市政模板） |
| 高支模 | `db.py:2421` / `2594`；`outline_common.py:1996-`（"高支模施工方案"） |
| 脚手架 | `db.py:2595`；`chapter_title_templates.json:434-437`（"脚手架搭设与使用安全"） |
| 起重吊装 | `db.py:2251` / `2423` / `2593` |
| 临时用电 | `db.py:2252` / `2424` |
| 消防保卫 | `db.py:2425`；`outline_common.py:51`（"危大工程及专项施工方案、紧急情况预案与风险控制措施"） |

配套：
- `chapter_title_templates.json:877-892` 有专门的"危大工程"章节 L2/L3 模板（"危大工程范围识别与清单"、"深基坑高支模专项方案编制"、"脚手架与起重吊装专项方案"、"生产安全事故应急预案编制"）
- `outline_common.py:36-51` 有 `FIXED_28_CHAPTERS`（28 项固定一级目录），其中"装配式混凝土结构（PC）工程专项方案"（`conditional="has_pc"`）与"危大工程及专项施工方案"为建筑工程专项方案场景的核心章

### E.3 相关配置文件

| 文件 | 作用 |
|---|---|
| `backend/app/services/ai/prompts/chapter_title_templates.json`（46 KB） | 28 顶层专项方案标题库，含 level2/level3 子项 |
| `backend/app/services/content_field_contract.py` | 九大章节字段契约 + 11 行业专项词表 |
| `backend/app/services/ai/industry_profiles.py` | 12 个行业 `INDUSTRY_PROFILES`（keywords + profile） |
| `backend/app/services/ai/writing_length_config.py` | 字数档位（brief/standard/detailed/long）+ `level2_target`/`level_target` |
| `backend/app/db.py:2200-2600` | 内置行业标准目录（房建装配式 / 高层 / 市政 / 装饰装修） |
| `doc/房建装配式－预制构件类项目投标技术标文件一级目录.md` | 装配式项目一级目录（13 章）参考 |
| `doc/生成的目录结构_30章.json` | 30 章完整目录样例 |
| `charts/` `图表提示词/` | 图表生成提示词（15 篇） |
| `data/chroma/` | 向量库（知识库检索） |
| `translations/en-US.json` `zh-CN.json` | 国际化 |

---

## F. 关键文件清单

| 文件路径 | 关键函数/组件 | 作用 |
|---|---|---|
| `backend/app/main.py` | `app`（FastAPI 实例）+ `_startup` + `_lifespan` | 应用入口、路由挂载、后台任务对账、审计日志清理 |
| `backend/app/models.py` | `Section`(:97) / `GlobalFact`(:61) / `KnowledgeItem`(:533) / `IndustryNode`(:582) | Pydantic 数据模型 |
| `backend/app/services/content_field_contract.py` | `NINE_CHAPTERS`(:53) / `classify_chapter`(:243) / `build_field_contract` / `run_content_self_checks` | 九大章节字段契约 + 全局数据字典 |
| `backend/app/services/ai/workflows_outline_v51.py` | `generate_outline_v51(:4813)` / `generate_outline_direct(:158)` / `_generate_level1_v51(:552)` / `_self_check_v51(:4078)` | 目录生成主入口（分步 + 直出） |
| `backend/app/services/ai/outline_gate.py` | `renumber_outline_ids(:260)` / `compute_outline_facts(:656)` / `collect_deterministic_issues(:899)` | 目录门禁与编号 |
| `backend/app/services/ai/prompts/_registry.py` | `_reg` / `render_prompt` / `render_prompt_safe` / `validate_prompt_variables` | 提示词注册表（60 注册项） |
| `backend/app/services/ai/prompts/chapter_title_templates.json` | 28 顶层专项方案模板 | 建筑工程专项方案标题源 |
| `backend/app/services/ai/industry_profiles.py` | `INDUSTRY_PROFILES`(:17) / `get_industry_profile`(:207) | 12 个行业知识库 |
| `backend/app/services/ai/provider_factory.py` | `get_working_provider`（三级降级） | AI Provider 工厂 |
| `backend/app/services/ai/rate_gate.py` | `slot`（RPM 令牌桶 + AIMD） | 限流/自适应并发 |
| `backend/app/services/ai/workflows_sections.py` | `generate_sections` / `extract_section_charts` | 正文生成并发主循环 |
| `backend/app/services/ai/workflows_orchestration.py` | 图文编排入口 | 章节图表/知识库匹配 |
| `backend/app/services/ai/mermaid_renderer.py` | `render_mermaid_to_bytes` | Mermaid 图表渲染 |
| `backend/app/services/facts_service.py` | `parse_stored_items` / `normalize_facts_policy` | 全局事实解析与三档策略 |
| `backend/app/services/consistency_auditor.py` | `ConsistencyAuditor` / `run_full_audit` / `suggest_fixes` / `apply_autofix` | 一致性审计 |
| `backend/app/services/consistency_rules.py` | `assess_severity` | 一致性规则单一事实来源 |
| `backend/app/services/consistency_repair.py` | `repair_with_ai` | AI 补丁修复 |
| `backend/app/services/agent_repair.py` | `prepare_agent_workspace`(:117) | Agent 模式一致性修复工作区 |
| `backend/app/services/export.py` | `build_docx` / `_add_inline_chart` / `_render_section_charts` / `_atomic_save_docx` | DOCX 构建 + 图表渲染 + 原子写盘 |
| `backend/app/routers/sse/sse_outline.py` | `generate_outline_sse_impl(:463)` / `_outline_main_loop(:137)` | 目录生成 SSE |
| `backend/app/routers/sse/sse_content.py` | `run_generation` / `_drain_queue` / `section_complete_callback` | 正文生成 SSE |
| `backend/app/routers/files.py` + `services/files.py` | `save_upload` / `_do_extract_text(:769)` / pdfplumber 表格感知(:998) | 文件上传解析 |
| `backend/app/routers/feasibility.py` | 6 步工作流（易标移植） | 可行性报告 |
| `backend/app/routers/prompts.py` | `_shared_target` / 版本管理 / 成本预算 | 提示词管理 |
| `backend/app/routers/consistency.py` | `_LockManager` / `POST /audit` / `POST /repair` / `POST /agent-repair` | 一致性校对路由 |
| `frontend/src/pages/OutlineEditorPage.tsx` | OutlineEditor 主组件 | 目录编辑 |
| `frontend/src/pages/SectionEditorPage.tsx` | SectionEditor 主组件 | 章节编辑（SSE 消费） |
| `frontend/src/pages/ExportConfigPage.tsx` | handleExport / startSSEExport / resumeSSEExport | 导出配置 |
| `frontend/src/api/index.ts`（2200+ 行） | 全套 API 客户端 | HTTP + SSE 封装 |
| `frontend/src/types/index.ts`（853 行） | `SectionTreeNode`(:97) / `KnowledgeItem`(:708) / `IndustryNode`(:753) | TS 类型 |
| `easybidding-service/src/services/technicalPlanService.ts` | `TechnicalPlanService.arrangeArchitecture` / `detectArchitectureSubtype` | 易标 Node 版图表校验（保留） |
| `easybidding-service/src/services/ChartService.ts` + `ChartCache.ts` + `chartValidators.ts` | 图表校验服务 | 易标图表管线（保留） |
| `docker-compose.yml` | backend / frontend / redis / celery_worker / celery_beat | 部署编排 |
| `backend_autostart.bat` | Watchdog v2.0（168 行） | 后端守护 |
| `技术架构文档.MD`（v4.1, 2026-10-06） | 6000+ 行完整设计 | 权威设计文档 |

---

**报告结论**：本平台已在目录生成、正文流式生成、一致性校对、导出文档四大主链路实现了对易标的完整移植（含大量 ENHANCE 2026-09-14/15 移植注释），并在九大章节字段契约、12 行业知识库、行业专项词表、Agent 模式一致性修复、防雷同/防串标三重机制、暗标合规自动锁定等维度实现了超越易标的差异化能力。主要短板集中在：图表校验体系死代码未统一、章节字数分档未按行业差异化、端到端实机验证受限、MCP 端点未完整实现（仅有 API Key）。
