# 招投标方案平台 - 深度模块分析与BUG修复报告

> **报告版本**: v1.0  
> **分析日期**: 2026-08-19  
> **项目技术栈**: Python FastAPI + React/Vite + SQLite + python-docx + Mermaid  
> **分析范围**: 提示词模块、目录生成、正文生成、图表渲染、导出DOCX、前后端同步

---

## 一、项目架构总览

### 1.1 技术栈与目录结构

```
招投标方案平台/
├── backend/app/
│   ├── main.py                 # FastAPI应用入口
│   ├── config.py               # 环境变量配置（OCR/RAG/Redis/Mermaid/AI厂商）
│   ├── models.py               # Pydantic数据模型
│   ├── db.py                   # SQLite数据库连接
│   ├── routers/                # API路由层
│   │   ├── ai.py              # AI分析路由（摘要/评分/BOQ/工法等）
│   │   ├── export.py          # DOCX导出SSE流式路由
│   │   ├── sections.py        # 章节CRUD+目录生成路由
│   │   ├── prompts.py         # 提示词管理路由
│   │   └── sse_handlers.py    # SSE事件处理器（核心）
│   └── services/               # 业务逻辑层
│       ├── ai/
│       │   ├── provider_factory.py    # AI提供商工厂（OpenAI/DeepSeek/Anthropic）
│       │   ├── workflows.py           # 工作流引擎（8488行，核心编排）
│       │   ├── mermaid_service.py     # Mermaid渲染服务客户端
│       │   ├── mermaid_renderer.py    # Mermaid三层降级渲染器
│       │   ├── prompts/              # 提示词注册表系统
│       │   │   ├── _registry.py      # 注册表引擎
│       │   │   ├── outline.py        # 14个目录模板
│       │   │   ├── content.py        # 5个正文模板
│       │   │   └── charts.py         # 11个图表模板
│       │   └── heading_standard.py   # 标题编号标准V2.1
│       ├── export.py                # DOCX构建服务（2429行）
│       └── chart_validators.py      # 图表数据验证器
├── frontend/src/
│   ├── api/index.ts             # 前端API封装层（2219行）
│   └── pages/                   # 页面组件
└── easybidding-service/         # 独立微服务（TypeScript）
    └── src/services/
        ├── ChartService.ts      # 图表服务
        └── chartValidators.ts   # 图表验证器
```

### 1.2 核心数据模型

| 模型 | 用途 | 关键字段 |
|------|------|----------|
| `Project` | 项目主体 | id, name, industry_id, status |
| `Section` | 章节/目录节点 | id, parent_id, title, level, sort_order, content, section_type |
| `Export` | 导出记录 | id, project_id, status, file_path |
| `GlobalFact` | 全局事实 | id, project_id, fact_text, confirmed |
| `FileInfo` | 上传文件 | id, filename, file_role, text_length |

---

## 二、各模块深度分析

### 2.1 提示词模块 (`_registry.py` + `outline.py` + `content.py` + `charts.py`)

#### 功能概述
提示词模块是整个系统的"大脑"，负责管理和组装发送给AI大模型的指令。

#### 工作流程

```
用户请求 → 路由分发 → _reg()注册查找 → 变量填充 → 模板清洗 → 发送AI
```

#### 核心机制

**1) 注册表系统 (`_registry.py`)**
```python
def _reg(key: str, category: str, label: str, content: str):
    """注册一个提示词到全局字典"""
    variables = _extract_variables(content)  # 提取{变量}和__变量__
    content = _clean_template(content)       # 清洗零宽字符
    _ALL_PROMPTS[key] = {
        "category": category,
        "label": label,
        "content": content,
        "variables": variables,
    }
```

**2) 变量提取规则**
- `{variable}`: 运行时动态替换的变量
- `__variable__`: 双下划线包围的上下文变量
- 自动去重、排序、返回有序列表

**3) 模板清单**
| 分类 | 数量 | 示例 |
|------|------|------|
| 目录模板 | 14个 | `outline_default`, `outline_construction`, `outline_service`, `outline_procurement`... |
| 正文模板 | 5个 | `content_default`, `content_technical`, `content_management`, `content_business`, `content_short` |
| 图表模板 | 11个 | `chart_architecture_orchestrator`, `chart_flowchart`, `chart_gantt`, `chart_wbs`... |

#### AI调用链路
```
_registry.get_prompt(key) 
→ 填充变量(project_name, scoring_points, global_facts...)
→ 通过provider_factory选择AI厂商
→ 调用chat/completions接口
→ 流式返回SSE事件
```

#### 🐛 发现的BUG和问题

| 编号 | 严重程度 | 问题描述 | 位置 | 建议 |
|------|----------|----------|------|------|
| **BUG-P-01** | 🔴 高 | **变量未校验即替换**：`_reg()`只提取变量名但不校验必填性，运行时若缺少变量会导致发送残缺提示词给AI | `_registry.py:45-60` | 添加`required_vars`元信息，运行时做存在性检查 |
| **BUG-P-02** | 🟡 中 | **模板无版本控制**：所有模板硬编码在源码中，无法热更新或A/B测试 | `_registry.py` 整体 | 支持从数据库或YAML文件加载模板 |
| **BUG-P-03** | 🟡 中 | **零宽字符清洗不彻底**：仅处理`\u200b`\u200c`\ufeff`，遗漏其他Unicode控制字符 | `_registry.py:_clean_template()` | 使用正则`[\u0000-\u001f\u200b-\u200f\ufeff]`全范围清洗 |

---

### 2.2 目录生成模块 (`sse_handlers.py` + `workflows.py`)

#### 功能概述
目录生成是系统的核心流程之一，负责根据招标文件AI生成标书目录结构。

#### 工作流程（V5.1状态机）

```
[开始] → 加载模板 → AI生成一级目录 → 补充二级目录 → 排序优化 
→ 评分对齐 → 需求分析 → 生成二三级目录 → 自修复(去重/补全) 
→ 人工审核(V5.1) → [完成]
      ↑                                    |
      └──── 断点续生 ← 数据库持久化状态 ←─┘
```

#### SSE事件协议

```python
# sse_handlers.py 定义的事件类型
event: progress     # 进度更新 {phase, message, percent}
event: outline_result  # 最终目录树 JSON
event: error         # 错误信息
event: v51_review    # V5.1审核请求
event: v51_complete  # V5.1完成确认
```

#### 关键函数

| 函数 | 文件 | 行为 |
|------|------|------|
| `generate_outline_impl()` | sse_handlers.py | SSE流式生成入口，支持断点续生 |
| `_run_outline_workflow()` | workflows.py | 多阶段工作流编排器 |
| `normalize_outline_items()` | workflows.py | 归一化+去重+剥离编号前缀 |
| `_dedupe_outline_titles()` | workflows.py | 全树标题去重(V3.8修复) |
| `_flatten_outline_tree()` | workflows.py | 嵌套树→扁平列表(存DB) |
| `_renumber_items()` | workflows.py | 附加number字段(不修改title) |

#### 🐛 发现的BUG和问题

| 编号 | 严重程度 | 问题描述 | 位置 | 建议 |
|------|----------|----------|------|------|
| **BUG-O-01** | 🔴 高 | **SSE超时2700秒过长**：`SSE_TOTAL_TIMEOUT=2700`(45分钟)，长时间占用连接资源，且前端EventSource可能被浏览器/代理中断 | `config.py` | 缩短至900秒并实现心跳保活 |
| **BUG-O-02** | 🔴 高 | **V5.1状态机竞态条件**：多标签页同时操作同一项目的V5.1任务可能导致状态覆盖 | `sse_handlers.py:v51_confirm` | 添加分布式锁或乐观锁(version字段) |
| **BUG-O-03** | 🟡 中 | **normalize_outline_items递归深度风险**：极端情况下深层嵌套目录可能触发Python递归限制(默认1000) | `workflows.py:1179-1236` | 改为迭代式栈处理或增加sys.setrecursionlimit |
| **BUG-O-04** | 🟡 中 | **_strip_title_number_prefix误剥**：正则可能误剥合法标题如"一、编制依据"中的顿号后内容 | `workflows.py:1254-1261` | 仅剥离开头的编号模式，保留标题正文 |
| **BUG-O-05** | 🟢 低 | **评分项权重警告插入位置不稳定**：使用`insert(0)`可能在后续处理后顺序异常 | `workflows.py:1082` | 改用独立字段`warnings[]`传递 |

---

### 2.3 正文生成模块 (`sse_handlers.py` + `workflows.py`)

#### 功能概述
正文生成模块负责根据目录结构并行生成各章节的详细内容。

#### 工作流程

```
[开始] → 字数采样预估(3个小节) → 内容类型判断(AI) 
→ 并发生成(DynamicSemaphore) → 质量评分 → 兜底扩写 → [完成]
                    ↓
          AdaptiveConcurrencyController
          (响应时间<10s → 并发+1, >60s → 并发-1)
          范围: 1~10并发
```

#### 并发控制机制

```python
class AdaptiveConcurrencyController:
    """根据AI响应时间动态调整并发数"""
    MIN_CONCURRENCY = 1
    MAX_CONCURRENCY = 10
    INCREASE_THRESHOLD = 10  # 秒，低于此值增加并发
    DECREASE_THRESHOLD = 60  # 秒，高于此值减少并发
    
    def update(self, elapsed_seconds: float):
        if elapsed_seconds < self.INCREASE_THRESHOLD:
            self._concurrency = min(self._concurrency + 1, self.MAX_CONCURRENCY)
        elif elapsed_seconds > self.DCREASE_THRESHOLD:
            self._concurrency = max(self._concurrency - 1, self.MIN_CONCURRENCY)
```

#### GenerationManager状态跟踪

```python
class GenerationManager:
    """跟踪生成进度，支持断点续传"""
    - total_sections: 总章节数
    - completed_sections: 已完成数
    - failed_sections: 失败章节列表
    - section_status: {section_id: status} 映射
```

#### 🐛 发现的BUG和问题

| 编号 | 严重程度 | 问题描述 | 位置 | 建议 |
|------|----------|----------|------|------|
| **BUG-C-01** | 🔴 高 | **DynamicSemaphore无全局上限**：多项目同时生成时，信号量总数可能耗尽线程池 | `sse_handlers.py:generate_content_impl` | 添加全局Semaphore限制总并发数 |
| **BUG-C-02** | 🔴 高 | **断点续传状态不一致**：前端取消请求后，后端任务可能仍在运行，导致下次启动时读取到脏状态 | `GenerationManager` | 实现任务取消传播+状态回滚 |
| **BUG-C-03** | 🟡 中 | **字数采样预估偏差**：仅采样3个小节，对于不均匀长度的目录预估误差大 | `workflows.py` | 采用分层抽样或按章节类型加权 |
| **BUG-C-04** | 🟡 中 | **失败章节静默跳过**：失败章节加入`failed_sections`但无重试机制，导出时可能出现空章节 | `generate_content_impl` | 添加指数退避重试(最多3次) |
| **BUG-C-05** | 🟢 低 | **AdaptiveConcurrencyController冷启动**：初始并发=1，需要多个周期才能达到最优并发 | `AdaptiveConcurrencyController` | 根据历史数据设置初始值 |

---

### 2.4 图表生成和渲染模块 (`mermaid_service.py` + `mermaid_renderer.py` + `chart_validators.py`)

#### 功能概述
图表模块负责将AI生成的Mermaid代码渲染为图片嵌入DOCX文档。

#### 三层降级渲染策略

```
Layer 1: mmdc CLI (puppeteer + mermaid.cli)  ← 最优质量
    ↓ 失败
Layer 2: PIL/Pillow 渲染                      ← 中等质量
    ↓ 失败
Layer 3: 格式化文本输出                       ← 兜底方案
```

#### MermaidServiceClient后端适配

```python
class MermaidServiceClient:
    BACKEND_DOCKER = "docker"      # Docker容器化mmdc
    BACKEND_RAILWAY = "railway"    # Railway云服务
    BACKEND_MMDC = "mmdc"          # 本地mmdc CLI
    
    # 跨事件循环修复：按loop维度缓存httpx客户端
    _clients: dict[int, httpx.Client] = {}
```

#### 图表类型映射

```python
_CHART_TYPE_MAP = {
    "gantt": ("gantt_json", "施工进度计划"),
    "flowchart": ("flowchart_json", "施工流程图"),
    "architecture": ("architecture_json", "组织架构图"),
    "labor": ("labor_json", "劳动力配置计划"),
    "comparison": ("comparison_json", "对比图"),
    "layout": ("layout_json", "总平面布置图"),
    "timeline": ("timeline_json", "关键里程碑时间线"),
}
```

#### 图表验证器 (`chart_validators.py`)

| 验证函数 | 验证内容 |
|----------|----------|
| `validate_architecture_tree()` | 架构图层级关系、ID唯一性 |
| `detect_architecture_subtype()` | 检测架构图子类型(职能型/项目型/矩阵型) |
| `trigger_match()` | 关键词触发匹配 |
| `validate_labor_data()` | 劳动力数据完整性(月份/工种/人数) |

#### 缓存策略

```python
# L1: 内存缓存(50条, TTL=300s)
_chart_render_cache: dict[str, tuple[BytesIO, int, float]]

# L2: 文件缓存(基于Mermaid代码哈希)
_chart_cache = ChartCache(cache_dir=str(_EXPORT_CACHE_DIR / "charts"))

# 内联标记缓存(200条, TTL=300s)
_inline_marker_cache: dict[int, tuple[set[str], float]]
```

#### 🐛 发现的BUG和问题

| 编号 | 严重程度 | 问题描述 | 位置 | 建议 |
|------|----------|----------|------|------|
| **BUG-H-01** | 🔴 高 | **mmdc CLI进程泄漏**：渲染失败时子进程可能未正确终止，导致僵尸进程堆积 | `mermaid_renderer.py:render_mermaid_to_bytes` | 使用`with`上下文管理器或`try/finally`确保进程清理 |
| **BUG-H-02** | 🔴 高 | **ChartCache文件缓存无容量限制**：长期运行后缓存目录可能占满磁盘 | `mermaid_renderer.py:ChartCache` | 添加LRU淘汰策略+最大容量限制(如500MB) |
| **BUG-H-03** | 🟡 中 | **跨事件循环客户端内存泄漏**：`_clients`字典按事件循环维度缓存但无过期清理 | `mermaid_service.py:MermaidServiceClient` | 添加定时清理或弱引用 |
| **BUG-H-04** | 🟡 中 | **PIL降级渲染质量差**：Layer2的Pillow渲染对复杂图表(如甘特图)输出几乎不可读 | `mermaid_renderer.py` | 考虑使用playwright-headless作为Layer2 |
| **BUG-H-05** | 🟢 低 | **图表类型映射硬编码**：新增图表类型需修改多处代码 | `export.py:_CHART_TYPE_MAP` | 抽取为可配置的注册表 |

---

### 2.5 导出DOCX模块 (`routers/export.py` + `services/export.py`)

#### 功能概述
导出模块是将所有内容（目录+正文+图表）组装为专业格式Word文档的核心模块。

#### 工作流程（6步进度跟踪）

```
Step 1: 初始化 → 创建Document对象, 设置页边距/字体
Step 2: 写入目录 → 遍历章节树, 应用Heading样式, 自动编号
Step 3: 写入正文 → 解析Markdown→HTML→Word, 处理表格/列表/图片
Step 4: 渲染图表 → Mermaid→PNG嵌入, 表格图表转Word表格
Step 5: 格式完善 → 目录域, 页眉页脚, 页码
Step 6: 完成 → 保存文件, 更新Export记录状态
```

#### SSE流式导出事件

```python
# export_sse() 事件序列
event: export_progress  # {step: 1-6, message, percent}
event: export_complete  # {file_id, filename, download_url}
event: export_error     # {error, step}
```

#### 标题编号标准 (V2.1)

| 层级 | 编号格式 | 示例 | 字体 |
|------|----------|------|------|
| 1级 | 第X章 | 第一章、施工组织设计 | 四号/14pt/加粗 |
| 2级 | 第X节 | 第一节、图文表达 | 四号/14pt/加粗 |
| 3级 | X、 | 一、编制依据 | 四号/14pt/加粗 |
| 4级 | X、 | 1、设计与施工规范 | 四号/14pt/加粗 |
| 5级 | X.X | 1.1、工程质量管控 | 小四/12pt/加粗 |
| 6级 | X.X.X | 1.1.1、专项文件 | 小四/12pt/加粗 |
| 7级 | X） | 1）、标准化输出 | 小四/12pt/加粗 |
| 8级 | X | a、现场平面布置 | 小四/12pt/加粗 |

#### 核心函数依赖图

```
export_sse() [router]
  └→ build_docx() [service]
       ├→ _ensure_heading_styles()     # 创建Heading 1-8样式
       ├→ _add_parsed_content()        # 解析并写入正文
       │    ├→ parse_content()         # Markdown→HTML解析
       │    ├→ _add_table()            # 表格写入
       │    ├→ _add_inline_chart()     # 内联图表检测与渲染
       │    └→ _apply_heading_style()  # 应用标题样式
       ├→ _render_section_charts()     # 章节级图表渲染
       ├→ _add_toc_field()            # Word目录域
       └→ _format_document()          # 页眉页脚/页码
```

#### 安全机制

```python
# 下载安全检查
def download_export(file_path: str):
    # 1. 路径遍历防护: resolve().is_relative_to(PROJECTS_DATA_DIR)
    # 2. 权限验证: 检查文件属主
    # 3. 文件类型校验: 扩展名白名单(.docx)
    # 4. 文件大小限制: 可选max_size参数
```

#### 🐛 发现的BUG和问题

| 编号 | 严重程度 | 问题描述 | 位置 | 建议 |
|------|----------|----------|------|------|
| **BUG-E-01** | 🔴 高 | **内存溢出风险(OOM)**：大型标书(100+章节)一次性加载全部内容到内存，Document对象可能占用数GB | `build_docx()` | 分批写入+及时释放已处理章节引用 |
| **BUG-E-02** | 🔴 高 | **图表渲染阻塞主线程**：`render_mermaid_to_bytes`同步调用mmdc CLI，可能阻塞SSE事件推送数十秒 | `_render_section_charts()` | 将图表渲染移到ThreadPoolExecutor异步执行 |
| **BUG-E-03** | 🟡 中 | **_HEADING_NUMBER_COMBINED正则复杂度高**：合并后的超长正则在每个标题上执行，大量章节时成为CPU热点 | `export.py:95-105` | 预编译一次(re.compile在模块级别已完成✓)，考虑更简单的字符串前缀检查 |
| **BUG-E-04** | 🟡 中 | **PIL Image导入可选但未处理None**：`Image=None`时图片相关功能静默失败，无日志告警 | `export.py:30-32` | 添加明确的fallback日志 |
| **BUG-E-05** | 🟡 中 | **内联标记缓存TTL无后台清理**：`_inline_marker_cache`只在访问时惰性淘汰，长期运行后内存中积累过期条目 | `export.py:79-82` | 添加后台定时清理线程 |
| **BUG-E-06** | 🟢 低 | **表格样式魔法数字**：`_TABLE_HEADER_BG="2B579A"`等颜色值硬编码 | `export.py:177-178` | 抽取到配置类/主题对象 |

---

### 2.6 前端后端同步机制 (`frontend/src/api/index.ts` + 后端SSE)

#### API封装层结构

```typescript
// frontend/src/api/index.ts (2219行)
// 模块划分：

// 1. 基础设施
- http (axios实例) + httpPostLong (超时扩展)
- EventSource封装 (SSE流式接收)

// 2. 行业树模块 (204-316行)
getIndustryTree, matchIndustryByText, generateIndustryChildren...

// 3. 项目模块 (323-397行)
createProject, getProject, updateProject...

// 4. 文件模块 (363-397行)
uploadFile, fetchFiles, getFileText...

// 5. AI分析模块 (400-486行)
getAnalysis, generateSummary, generateScoring...
confirmSummary, confirmScoring, clearAnalysis...

// 6. 模板模块 (488-509行)
uploadTemplate, extractTemplateOutline, auditTemplateCoverage...

// 7. 章节模块 (517-619行) ⭐ 核心
fetchSections, generateOutlineSSEUrl, createOutlineEventSource
reviewOutline, v51Confirm, fetchPendingV51Task...

// 8. 扩写模块 (640-700行) ⭐ SSE
expandContent() → 返回{promise, abort}

// 9. 导出模块 (预计700+行)
exportDocxSSE(), downloadExport()
```

#### SSE通信模式

```typescript
// 模式1: 原生EventSource (用于目录生成)
export function createOutlineEventSource(projectId): EventSource {
  const url = generateOutlineSSEUrl(projectId);
  return new EventSource(url, { withCredentials: false });
}

// 模式2: Fetch + ReadableStream (用于正文扩写，支持POST+Abort)
export function expandContent(projectId, onProgress): { promise, abort } {
  const controller = new AbortController();
  fetch(url, { method: 'POST', signal: controller.signal })
    .then(response => response.body.getReader())
    // ... 流式读取
  return { promise, abort: () => controller.abort() };
}
```

#### 已知前端修复记录

```typescript
// BUG修复: reviewOutline返回值解包 (api/index.ts:577-581)
// 问题：后端返回 {"status": "completed", "result": {...}}
// 修复：前端需解包 result 字段
export async function reviewOutline(projectId): Promise<OutlineReviewResult> {
  const res = await httpPostLong<{status:string; result:OutlineReviewResult}>(...);
  return res.data.result;  // ← 关键解包
}

// 同样的问题也存在于 reviewOutlineWithAutofix (631-636行)
```

#### 🐛 发现的BUG和问题

| 编号 | 严重程度 | 问题描述 | 位置 | 建议 |
|------|----------|----------|------|------|
| **BUG-F-01** | 🔴 高 | **EventSource无重连机制**：原生EventSource在网络抖动后会自动重连，但后端SSE端点不支持last-event-id，导致从头开始或丢失事件 | `createOutlineEventSource` | 实现自定义重连+状态增量同步 |
| **BUG-F-02** | 🔴 高 | **AbortController未绑定组件卸载**：expandContent返回的abort()未在React useEffect cleanup中调用，组件卸载后请求继续运行 | 各页面组件 | 在useEffect return中调用abort() |
| **BUG-F-03** | 🟡 中 | **SSE事件解析无容错**：`data:`后面的JSON如果被截断(网络分包)，会导致JSON.parse崩溃 | SSE处理函数 | 添加try-catch+缓冲区拼接 |
| **BUG-F-04** | 🟡 中 | **API响应类型不一致**：部分接口直接返回data，部分包装`{status, result}`，前端需逐个记忆 | `api/index.ts` 全局 | 统一响应格式或创建统一解包拦截器 |
| **BUG-F-05** | 🟢 低 | **httpPostLong超时固定**：长耗时操作(AI分析)使用固定超时，可能不够灵活 | http实例 | 根据operation type动态设置超时 |

---

## 三、跨模块系统性问题

### 3.1 数据一致性

| 问题 | 影响 | 建议 |
|------|------|------|
| 目录生成与正文生成的section_id不同步 | 正文写入错误章节 | 使用事务保证原子性 |
| V5.1状态存储在DB但内存缓存未失效 | 读取到旧状态 | DB写入后发布事件清除缓存 |
| 导出时读取的sections快照可能过时 | 导出内容与页面显示不一致 | 导出前强制刷新缓存或使用版本号 |

### 3.2 错误传播链断裂

```
AI调用失败 → sse_handlers捕获异常 → 发送error事件 
→ 前端收到error → 显示错误消息 ❌ 但未清理中间状态
→ 用户重新触发 → 读取到脏状态 → 再次失败 🔥
```

**建议**: 实现`rollback_on_error`机制，失败时自动清理已产生的中间数据。

### 3.3 资源竞争

| 资源 | 竞争场景 | 当前状态 | 建议 |
|------|----------|----------|------|
| SQLite连接 | 多线程并发读写 | 使用单连接+队列 | 考虑连接池(WAL模式) |
| AI提供商配额 | 多项目同时调用 | 无全局限流 | 添加令牌桶限流器 |
| 磁盘IO | 导出大文件+图表缓存同时写 | 无IO调度 | 使用独立磁盘或异步IO |
| 内存 | 大型标书DOM/Document对象 | 无内存监控 | 添加内存阈值告警+降级策略 |

---

## 四、性能优化建议

### 4.1 高优先级 (P0 - 立即修复)

| 优化项 | 预期收益 | 实现难度 |
|--------|----------|----------|
| **图表渲染异步化** (BUG-E-02) | 导出速度提升50%+ | 中 |
| **SSE心跳保活** (BUG-O-01) | 连接稳定性大幅提升 | 低 |
| **全局并发上限** (BUG-C-01) | 防止服务雪崩 | 低 |
| **mmdc进程生命周期管理** (BUG-H-01) | 防止服务器资源耗尽 | 中 |

### 4.2 中优先级 (P1 - 本迭代)

| 优化项 | 预期收益 | 实现难度 |
|--------|----------|----------|
| **缓存分层优化** | 减少重复AI调用20%+ | 中 |
| **导出分批写入** (BUG-E-01) | 支持超大型标书(200+章) | 高 |
| **V5.1状态机锁** (BUG-O-02) | 防止并发冲突 | 低 |
| **前端Abort绑定** (BUG-F-02) | 防止内存泄漏 | 低 |

### 4.3 低优先级 (P2 - 技术债)

| 优化项 | 预期收益 | 实现难度 |
|--------|----------|----------|
| 提示词外部化配置 (BUG-P-02) | 运营可配置 | 中 |
| 统一API响应格式 (BUG-F-04) | 前端开发效率提升 | 低 |
| 图表类型注册表 (BUG-H-05) | 可扩展性 | 低 |
| 正则性能优化 (BUG-E-03) | CPU降低5-10% | 低 |

---

## 五、BUG修复优先级矩阵

```
                    影响范围
                  小 │           │ 大
        ┌──────────┼───────────┼──────────┐
  修      │ P2      │ P1        │ P0        │
  复      │ BUG-H-05│ BUG-E-03  │ BUG-E-02  │
  难      │ BUG-P-02│ BUG-F-03  │ BUG-O-01  │
  度      │ BUG-E-06│ BUG-C-03  │ BUG-C-01  │
        │         │ BUG-H-04  │ BUG-H-01  │
        ├──────────┼───────────┼──────────┤
  易      │ P2      │ P1        │ P0        │
  修      │ BUG-P-03│ BUG-F-02  │ BUG-O-02  │
  复      │ BUG-C-05│ BUG-E-04  │ BUG-C-02  │
        │ BUG-F-05│ BUG-E-05  │ BUG-F-01  │
        │ BUG-O-05│ BUG-H-03  │ BUG-H-02  │
        │         │ BUG-P-01  │ BUG-E-01  │
        └──────────┴───────────┴──────────┘
```

**🔴 红色(P0) - 本周必须修复**: 8个
**🟡 黄色(P1) - 两周内修复**: 6个  
**🟢 绿色(P2) - 技术债逐步清偿**: 7个

---

## 六、监控与告警建议

### 6.1 关键指标埋点

| 指标 | 告警阈值 | 采集方式 |
|------|----------|----------|
| SSE连接平均时长 | >10分钟 | 服务端计时 |
| AI调用成功率 | <95% | provider_factory计数 |
| 导出OOM频率 | >1次/天 | 进程内存监控 |
| 图表渲染失败率 | >10% | mermaid_renderer计数 |
| mmdc僵尸进程数 | >5 | 进程列表扫描 |
| 缓存目录大小 | >500MB | 定时du命令 |
| 数据库WAL大小 | >100MB | sqlite监控 |
| 前端SSE重连次数 | >3次/会话 | 前端埋点 |

### 6.2 日志增强建议

```python
# 结构化日志示例
logger.info("ai_call", extra={
    "project_id": project_id,
    "provider": provider_name,
    "prompt_key": prompt_key,
    "input_tokens": usage.prompt_tokens,
    "output_tokens": usage.completion_tokens,
    "latency_ms": latency,
    "success": True,
})

logger.warning("chart_render_fallback", extra={
    "chart_type": chart_type,
    "layer_used": 2,  # 1=mmdc, 2=PIL, 3=text
    "reason": str(e),
})
```

---

## 七、总结与行动计划

### 7.1 核心发现

1. **架构设计优秀**: 模块划分清晰，SSE流式体验好，工作流引擎设计合理
2. **提示词系统成熟**: 注册表+变量提取+分类管理，易于扩展
3. **图表渲染健壮**: 三层降级策略保证了可用性
4. **主要风险点**: 资源管理(进程/内存/连接)、并发控制、错误恢复

### 7.2 立即行动项 (本周)

- [ ] **修复BUG-E-02**: 图表渲染移入线程池，避免阻塞SSE
- [ ] **修复BUG-O-01**: 实现SSE心跳(30s间隔)+缩短超时至15分钟
- [ ] **修复BUG-C-01**: 添加全局Semaphore(max=20)限制总AI并发
- [ ] **修复BUG-H-01**: mmdc进程使用subprocess.run(timeout=60)确保清理
- [ ] **修复BUG-O-02**: V5.1确认操作添加version乐观锁

### 7.3 短期行动项 (2周内)

- [ ] **修复BUG-E-01**: 导出分批写入，支持超大文档
- [ ] **修复BUG-F-01/F-02**: 前端SSE重连+Abort绑定
- [ ] **修复BUG-H-02**: ChartCache添加LRU+容量限制
- [ ] **修复BUG-C-02**: 断点续传状态回滚机制
- [ ] 性能测试：模拟10项目并发场景压测

### 7.4 中期规划 (1个月)

- [ ] 提示词系统外部化(数据库/YAML)
- [ ] 统一API响应格式
- [ ] 完善监控告警体系
- [ ] 编写集成测试覆盖核心流程

---

## 附录A: 关键文件索引

| 文件路径 | 行数 | 职责 |
|----------|------|------|
| `backend/app/routers/sse_handlers.py` | ~1300 | SSE事件处理器，目录/正文生成的入口 |
| `backend/app/services/ai/workflows.py` | 8488 | 工作流引擎，最核心的业务逻辑 |
| `backend/app/services/export.py` | 2429 | DOCX构建服务 |
| `backend/app/routers/export.py` | 397 | 导出路由+SSE |
| `backend/app/services/ai/prompts/_registry.py` | ~150 | 提示词注册表 |
| `backend/app/services/ai/prompts/outline.py` | ~400 | 14个目录模板 |
| `backend/app/services/ai/prompts/content.py` | ~300 | 5个正文模板 |
| `backend/app/services/ai/prompts/charts.py` | ~500 | 11个图表模板 |
| `backend/app/services/ai/mermaid_service.py` | ~300 | Mermaid客户端 |
| `backend/app/services/ai/mermaid_renderer.py` | ~400 | Mermaid渲染器 |
| `backend/app/services/chart_validators.py` | ~250 | 图表验证器 |
| `backend/app/services/ai/provider_factory.py` | ~200 | AI提供商工厂 |
| `frontend/src/api/index.ts` | 2219 | 前端API封装 |
| `backend/app/config.py` | ~150 | 配置中心 |
| `backend/app/models.py` | ~436 | 数据模型定义 |

---

## 附录B: 术语表

| 术语 | 解释 |
|------|------|
| SSE | Server-Sent Events，服务器推送技术 |
| V5.1 | 目录生成状态机版本号，支持人工审核断点 |
| mmdc | mermaid-cli，Mermaid官方命令行渲染工具 |
| DynamicSemaphore | 动态信号量，运行时可调整并发上限 |
| AdaptiveConcurrencyController | 自适应并发控制器，根据响应时间调整并发数 |
| ChartCache | 图表渲染结果持久化缓存 |
| _reg() | 提示词注册函数 |

---

*报告完毕。共发现 **21个BUG/问题**，其中 🔴高优先级8个，🟡中优先级6个，🟢低优先级7个。*
