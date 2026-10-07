# 招投标方案平台项目记忆

## 正文生成模块深度探索与 BUG 修复 (2026-08-31)

### 模块全景（约 3400 行核心 + SSE 流处理）
- **核心调用链**：`generate_content_impl` (sse_handlers.py:1491) → `event_generator` (:1522) → `run_generation` (:1741) → `_generate_one` (workflows_sections.py) → `_do_generate_section_with_optimizations` → `generate_section_content` (:820) → `provider.chat`
- **并发生成**：`run_generation` 用 `asyncio.gather` 并发调用 `_generate_one`（每章节一个 task），`DynamicSemaphore` 控制并发度，`AIRequestMonitor` 全局上限 20
- **质量评估**：`_do_generate_section_with_optimizations` 循环：生成 → 质量评分 → 不达标则递增温度重试（0.7→0.85→0.95）→ 达标或耗尽重试次数
- **续写**：`_continue_content` — 尾部窗口 2000 字 + 头部摘要递减 + 停滞检测（连续 2 轮 <30 字退出）
- **缓存**：`SectionContentCache` — MD5 键（project_id+title+tender_context+writing_length+target_words+temperature），TTL 1h，max 100
- **SSE 流**：`event_generator` 主循环 — 8 秒心跳 + 10 秒队列等待 + 总超时自适应（900+90*章节数, 上限 2700s）
- **兜底扩写**：`_expand_fallback` (workflows_expand.py) — 对字数不足的章节调用 AI 扩写
- **数据流**：`_build_leaf_dicts` 构建叶子章节 → `_mark_sections_generating` 批量标记 → 并发生成 → `section_complete_callback` 逐章节保存 DB → `generation_manager.update_section_progress` 更新进度 → SSE 推送前端

### 本轮修复（2 高 + 7 中 = 9 个 BUG）

#### 高严重度（2 个，状态死锁类）
1. **B-CONTENT-1 (高·内存泄漏+状态死锁)** — `sse_handlers.py:2001` `run_generation` finally 块不调用 `finalize_task`，客户端断开后后台任务完成时 `generation_manager._tasks` 永远残留 → `get_project_progress` 永远返回 `has_active_task: True`，用户无法再次生成。修复：finally 块中 `client_disconnected and not error_sent and not stopped` 时调用 `finalize_task`
2. **B-CONTENT-2 (高·状态卡死)** — `sse_handlers.py:1700` `section_complete_callback` DB 保存失败时改 `status="failed"` 但不重写 DB，章节永远卡在 `status='generating'`，前端永远转圈。修复：except 块中用 `"failed"` 状态重试一次 `_save_section_to_db`

#### 中严重度（7 个）
3. **B-CONTENT-3 (中·并发超发)** — `workflows_base.py:367` `DynamicSemaphore.set_max_value` 减小并发数时不钳制 `_current_value`，残留许可被新任务获取导致超发。修复：`else` 分支加 `self._current_value = min(self._current_value, new_max)`
4. **B-CONTENT-4 (中·崩溃)** — `workflows_quality.py:615` `build_content_mapping` 用 `int(parts[1])` 解析层级，标题含 `"|"` 时 `split("|")` 产生多于 3 段 → `ValueError`。修复：`try/except (ValueError, IndexError)` 跳过无效键
5. **B-CONTENT-5 (中·空内容存库)** — `workflows_sections.py:1979` AI 返回空内容时仍标记 `status="completed"` 存库，前端显示已生成但正文为空。修复：空内容时返回 `status="failed"` + `error="AI返回空内容"`
6. **B-CONTENT-6 (中·计数不一致)** — `sse_handlers.py:1889` 兜底扩写后 `results = expanded` 但 `saved_count`/`failed_count` 未重新统计，completed 事件报告的 success 数量不准确。修复：扩写后 `saved_count = sum(1 for r in results if r.get("status") == "completed")`
7. **B-CONTENT-7 (中·静默全量重生成)** — `sse_handlers.py:405` `regenerate_ids` 解析失败时静默回退到 `all_leaves`（全量生成），产生大量 AI 调用和计费。修复：改为返回空列表 + warning 日志
8. **B-CONTENT-8 (中·GC 风险)** — `sse_handlers.py:2197` `asyncio.create_task(_drain_queue())` 未保存引用，Task 可能被 GC 回收导致队列排空中断。修复：模块级 `_background_tasks: set` + `add_done_callback(discard)`
9. **B-CONTENT-9 (中·状态不一致)** — `workflows_sections.py:2051` `incremental_state.update_status` 硬编码 `"completed"`，但 `section_complete_callback` 可能改为 `"failed"`。修复：用 `result["status"]` 代替硬编码

### 验证
- pytest 全量：**781 passed, 2 skipped in 110s**（无回归）

### 已知待处理（本轮发现但未修复，需更大改动）
- **CONTENT-10**：重试退避 `sleep` 期间持有 sem 和 _ai_monitor 许可，阻塞其他任务（需重构重试结构）
- **CONTENT-11**：缓存键温度不匹配（line 1688 读取用 0.7，line 1997 写入用 current_temperature），重试章节缓存永久失效
- **CONTENT-12**：SSE 总超时上限 2700s 对 100+ 章节大项目可能不够（需按并发度估算）
- **CONTENT-13**：queue 无背压，客户端断开后 run_generation 继续 put 事件导致内存堆积
- **CONTENT-14**：多章节并发完成时 SQLite 写入竞争（需专用 Lock 串行化）
- **CONTENT-15**：兜底扩写后未同步状态到 generation_manager（进度面板不反映扩写结果）
- **QUALITY-DEAD**：`quality_evaluator.py` 未被任何模块导入，属于死代码

---

## 目录生成模块深度探索与 BUG 修复 (2026-08-31)

### 模块全景（约 14000 行，14 个文件）
- **核心调用链**：`generate_outline` (workflows.py:3227) → `generate_outline_v51` (workflows_outline_v51.py:3017) → `_generate_level1_v51` (:117) → `_call_ai_v51` (:83) → `collect_json_response`
- **V5.1 工作流**：L1(AI生成一级目录) → 人工审核 → L2(预构建骨架+AI并发填充二三级) → 标题质量后处理 → 人工审核 → L3(检查三级完整性) → 兜底补全 → 技术增强 → AI自检 → 最终审核 → 全局去重
- **并发控制**：`DynamicSemaphore` + `AdaptiveConcurrencyController`（滑动窗口动态调整）+ `AIRequestMonitor`（全局并发上限 20）
- **提示词**：`prompts/outline.py` 9+1 个模板，V5.1 三级目录有详细承接铁律、差异化铁律、禁用词、技术落地铁律
- **SSE 流**：`sse_handlers.py`（2405行）3 个 SSE 端点：目录生成 / 正文并发生成 / 兜底扩写
- **路由层**：`routers/sections.py`（1215行）35+ 端点，CRUD + V5.1 审核 + 配图/流程图/甘特图/架构图/劳力图/对比图

### 本轮修复（3 个提示词/代码质量 + 6 个 SSE + 6 个路由 + 5 个工作流 = 20 个 BUG）

#### 提示词与代码质量（3 个）
1. **B-OUT-1 (高·AI质量)** — `prompts/outline.py:114` OUTLINE_V51_LEVEL1_SYSTEM 角色描述完全为空（`### 角色\n\n### 任务`），对比 LEVEL2/LEVEL3 都有明确角色。修复：补全"你是专业的标书编写专家，擅长根据项目需求撰写投标文件技术标提纲，精通各类工程项目的评分项映射与章节体系设计。"
2. **B-OUT-2 (中·代码异味)** — `workflows_outline_v51.py:3162` 局部变量 `review_outline` 遮蔽模块级函数 `async def review_outline(...)`。修复：改名 `outline_for_review`
3. **B-OUT-3 (低·注释)** — `workflows_outline_v51.py:2674` 注释"超过 50 个节点"与代码 `if total_nodes > 80` 不一致。修复：注释改为 80

#### SSE 流处理（6 个，含 1 个阻塞性）
4. **B-SSE-1 (高·阻塞, 首次目录生成必定触发)** — `sse_handlers.py:1360` `_run_outline_task` finally 块引用未定义变量 `level1`（工作流返回值赋给 `outline`）→ `NameError` → V51 状态不持久化 + task_registry 僵尸任务 + `outline_finished[0]` 不设置 → 前端 15 分钟只收到心跳后超时失败。修复：改用 `locals().get('outline')`
5. **B-SSE-2 (高·资源泄漏)** — `sse_handlers.py:2059` 心跳推送失败时 `break` 未设置 `client_disconnected = True`，导致 finally 块误判为正常退出，后台任务被错误取消（与"客户端断开保留任务"设计意图相反）。修复：break 前设置 `client_disconnected = True`
6. **B-SSE-3 (高·资源泄漏)** — `sse_handlers.py:1611` `generate_content_impl` 初始化失败路径未清理 `_cancel_events` 中的 `cancel_event` → 孤儿 Event 内存泄漏。修复：except 块 return 前清理
7. **B-SSE-4 (高·静默失败)** — `sse_handlers.py:1470` `except Exception: pass` 吞掉 `_outline_main_loop` 所有异常，SSE 流正常关闭但无 error 事件，前端永远不知道失败。修复：记录日志 + 发送 error 事件
8. **B-SSE-5 (中·超时)** — `sse_handlers.py:2382` `expand_content_impl` 心跳 timeout=30 秒过长（其他端点 8-10 秒），中间代理可能 30 秒空闲断开。修复：改为 10 秒
9. **B-SSE-6 (中·资源泄漏)** — `sse_handlers.py:2395` `expand_content_impl` finally 块未清理 `_cancel_events`（只清了 `_cancel_tasks`）。修复：追加 `_cancel_events` 清理

#### 路由层（6 个功能性 BUG）
10. **B-SEC-2 (高·数据竞争)** — `sections.py:460` `clear_sections_route` 先 `cancel_v51_task`（仅设标志）紧接着 `DELETE FROM sections`，与并发生成竞态导致幽灵章节/外键失败。修复：先 `await stop_generation` 等待任务真正停止
11. **B-PERF-1 (中·性能)** — `sections.py:73` `require_project_permission` 只读查询用 `get_db()`（获取写锁），20+ 端点串行化。修复：改 `get_db(readonly=True)`
12. **B-SEC-3 (中·DoS)** — `sections.py:284` `concurrency: int | None = Query(None)` 无上限，恶意客户端传 10000 耗尽连接池。修复：`Query(4, ge=1, le=8)`
13. **B-UI-1 (中·错误处理)** — `sections.py:769` `illustration_data` 损坏时返回原始字符串作为 URL，客户端收到无效 URL。修复：返回空字符串 + warning 日志
14. **B-UI-2 (中·数据不一致)** — `sections.py:841` 配图持久化失败仍返回 `success: True`，用户不知丢失。修复：返回 `success: False`
15. **B-UI-3 (低·冗余)** — `sections.py:839` 手动 `conn.commit()` 冗余（`get_db_with_retry` 自动提交）。修复：删除手动 commit

#### 工作流核心（5 个，含 1 个功能性失效）
16. **B-WF-1 (高·功能失效)** — `workflows_sections.py:1362` `IncrementalGenerationState.update_status` 调用 `self._bump_version()` 但该方法从未定义 → `AttributeError` → 增量生成状态机完全失效（所有章节永远 pending）。修复：添加 `_version` 属性和 `_bump_version` 方法
17. **B-WF-2 (中·缓存污染)** — `workflows_sections.py:1992` 质量重试后二次缓存写入硬编码 `temperature=0.7`，但实际可能用 0.85/0.95 生成 → 缓存键与内容不匹配，温度隔离失效。修复：改为 `temperature=current_temperature`
18. **B-WF-3 (中·prompt 超限)** — `workflows_outline_v2v3.py:2363` 跨章节多样性修复传未切片的 `tender_context`（正常路径用 `tender_slice = tender_context[:30000]`），prompt 可能几十万字符。修复：改为 `tender_slice`
19. **B-WF-4 (中·标题错位)** — `workflows_outline_v2v3.py:2318` 异常路径 `chapter_idx=idx`（0-based）与 `_fill_one` 内部 `index + 1`（1-based）不一致，兜底标题选取起点错位。修复：改为 `idx + 1`
20. **B-WF-5 (中·校验降级)** — `workflows_outline_utils.py:56` 导入 `_L3_GENERIC_PATTERNS` 失败时静默降级为空元组，L3 具体性校验形同虚设。修复：添加 `logging.warning` 提示降级

### 验证
- pytest 全量：**781 passed, 2 skipped in 98s**（无回归）
- 所有修复均为最小侵入式，不改变现有 API 行为

### 已知待处理（本轮发现但未修复，需更大改动）
- **SSE-7**：`CancelledError` 被多处捕获不重新抛出，破坏取消传播（需谨慎评估）
- **SSE-8**：`v51_state._v51_states` 全局字典无锁保护，并发请求可能竞态（需加 asyncio.Lock）
- **SSE-9**：非重连路径 `sse_queue` 是"幽灵订阅者"，事件堆积浪费内存（需重构订阅机制）
- **SEC-4**：CRUD 端点普遍缺少权限验证和 section 归属校验（IDOR 漏洞，需逐端点添加 `require_project_permission`）
- **SEC-5**：无效用户 ID 绕过权限验证（`uid = None` 后 `if uid` 短路跳过）
- **WF-6**：V6.0 排序后评分点按错位索引关联（需重构映射逻辑）
- **WF-7**：`validate_outline_business` 失败时仍返回不合法目录（需添加修复逻辑或抛异常）
- **WF-8**：并发下 `progress_callback` 的 `completed` 参数不准确（需原子计数器）

---

## 解析模块深度探索与 BUG 修复 (2026-08-31)

### 模块全景（功能/工作流/路由/数据链/协议）
- **文件解析链**：上传 → `routers/files.py` → `services/files.py`（按类型分发）→ `docx_extractor`（python-docx 主 + docx2python 回退）/ `ocr_parser`（MinerU CLI，扫描件 PDF→Markdown）→ 提取纯文本存 `project_files.text_content`
- **标书上下文**：`tender_context.extract_tender_context` —— 47 个技术标锚词定位 + 25 个商业排除词 + 滑动窗口(100行) + 评分区加窗(+50) + 40k 字符上限 + LRU 缓存(100/1h)
- **AI 分析工作流**：`workflows_analysis` —— 摘要(1次chat/4096token/60s) → 评分点两阶段(初识别60s+二次审查120s) → 全局事实(collect_json_response/重试1) → BOQ/工界面/工法(各1次collect_json)
- **AI 提示词**：`prompts/analysis.py` 14 个模板，经 `_registry._reg` 注册（返回原值，常量仍为字符串），`render_prompt` 替换 `{var}`/`__VAR__`
- **路由端点**（`/projects/{id}/ai`）：`GET /analysis`、`PATCH /analysis/scope`、`POST /summary`、`POST /scoring`、`POST /boq`、`POST /work-interface`、`POST /construction-method`；SQL 字段白名单防注入
- **JSON 解析**：`utils/json_parser` —— 三级策略：直接 loads → ```json 围栏 → 括号配平扫描（处理嵌套/字符串内括号），500k 字符截断防回溯
- **AI 调用响应速度**：provider.chat 签名 `(messages, *, max_retries, **options)`，所有分析端点用 `with_timeout` 包裹（摘要/评分 60s、审查 120s）；`get_working_provider` 支持兜底降级；`/scoring` 命中缓存时早退（已修，避免 4 次浪费 AI 调用）

### 本轮修复（4 个真实 BUG）
1. **B-PARSE-1 (高·正确性)** — `tender_context._cache_key` 仅哈希 `text[:8192]`，前缀相同的不同招标文件（同模板封面+目录）缓存碰撞 → AI 摘要/评分/事实基于错误上下文生成。修复：改为全文长度+全文 sha256
2. **B-PARSE-2 (中·分类错误)** — `workflows_analysis._dict_to_fact_items` 把"技术"放进 personnel tag，导致"技术路线/技术参数"误归 personnel；且 personnel 缺"经理"导致"项目经理"落 basic。修复：personnel 改为 `person/manager/负责人/人员/经理`，technical 加"技术"
3. **B-PARSE-3 (中·性能)** — `docx_extractor.extract_docx_with_fallback` 回退条件 `len<50 or 无表格`，纯文本文档（无表格但字符多）总白跑一次 docx2python。修复：收紧为 `len<50`
4. **B-PARSE-4 (低·边界)** — `ocr_parser.parse_pdf` 用 `"mineru_" in output_dir` 判断是否清理临时目录，用户传入含该子串的 output_dir 会被误删。修复：改用 `_auto_output_dir` 显式标志

### 验证
- 4 个 BUG 针对性验证脚本全通过
- pytest 全量：**781 passed, 2 skipped in 108s**（无回归）

### 观察到的良好实践（无需修复）
- `json_parser` 括号配平 + 字符串转义处理 + 50w 字符截断
- `tender_context` 评分区优先保留 + 排除词窗口裁剪
- `routers/ai` SQL 字段白名单 `_validate_field_name` 防注入
- `ocr_parser` 已修 max_concurrent 误用、_set_to_cache 脱节等历史 BUG
- `prompts/analysis` 提示词带自我反思验证清单 + 兜底规则

---

## BUG 修复记录 (2026-08-31)

### 本轮修复（系统性：深度探索 → 基线定位 → 归类修复 → 全量回归）
1. **B-ENV-1 (环境)** — 火绒 HIPS 拦截 python.exe 写入项目根 `data/`/`logs/`（PowerShell 可写、Python 被拒）。需在火绒中将 python.exe 或项目目录加白。临时方案：`BID_ASSISTANT_DATA_DIR` 指向可写目录
2. **B-T1 (测试隔离)** — `tests/conftest.py`: 测试强制使用临时数据目录 + session 级 `init_db()`，杜绝测试污染真实库、解决空库无 schema 问题
3. **B-BUG-1 (死锁, 产品级)** — `mermaid_renderer.py:3484` ChartCache: `set()` 持锁调用 `_evict_if_needed()` 再抢同一把非重入 `threading.Lock` → 自死锁（V2.0 LRU 引入）。修复：改为 `threading.RLock()`。此前导致 test_all_charts_docx_export / test_chart_pipeline 全文件超时（>180s → 11.8s/17s）
4. **B-T2 (测试前提)** — `test_bug_fixes.py` TestApiKeyWarning: `app.main` 导入时 `load_dotenv(override=True)` 会把 `backend/.env` 的 AGNES_API_KEY 注入 os.environ，导致"未配置警告"测试失效。修复：测试内 `patch.dict(os.environ)` + `pop` 显式保证前提
5. **B-T3 (过时守卫)** — `outline_tasks.py` 已按设计废弃为死代码文档，删除 2 个对应守卫测试（test_outline_asyncio / test_outline_integration）
6. **B-T4 (脚本混入)** — 4 个脚本型验证文件（main() 无 pytest 测试）移至 `temp_debug_scripts/`：test_selfcheck_fix_integration / test_e2e_verification / test_pic_insert_method / test_picture_insert
7. **B-SEC-1 (安全)** — 清除 `temp_debug_scripts/test_selfcheck_fix_integration.py` 硬编码 AGNES key，改为仅环境变量（未设置则退出）。注意 `backend/.env` 含真实 key（已被 .gitignore 忽略，未跟踪，勿提交）

### 验证结果
- pytest: **781 passed, 2 skipped in 108s**（修复前：全量 >20min 超时跑不完，2 文件死锁、18 文件失败/报错）
- TypeScript: 0 errors

### 经验教训
- Windows GBK 控制台下 pytest 输出中文乱码属显示问题，设置 `$env:PYTHONIOENCODING='utf-8'` 可解
- 逐文件测试时被 kill 的超时进程可能留孤儿污染后续结果——先排查进程再定性失败
- "单跑通过、合跑失败" = 跨文件状态污染，优先怀疑 os.environ / load_dotenv / 全局单例

### 已知待处理（沿用）
- B-P0-1: 认证机制仍为 mock（security/auth.py 仅返回 admin）
- B-P0-3: workflows.py 8400行 monolithic（improvement_plan.md 已有拆分方案）
- B-P2-3: 导出时 9 处独立 DB 查询可合并
- B-P2-4: `test_all_charts_docx_export.py` 有 `coroutine never awaited` 警告（mock 不完整），不影响通过

---

## BUG 修复记录 (2026-08-20)

### 本轮修复
1. **B-P0-4 (安全风险)** — `config.py`: 移除硬编码 API Key，未配置时返回空字符串并警告
2. **B-P1-2 (可用性)** — `rate_limiting.py`: Redis 不可用时降级到内存限速（InMemorySlidingWindowLimiter）
3. **B-P1-3 (健壮性)** — `export.py check_document`: 增加 sections=None/非列表 防御，row is None 处理

### 验证结果
- pytest: 756 passed, 2 skipped
- TypeScript: 0 errors
- 后端健康: 200 OK
- 前端运行: 200 OK

### 已知待处理
- B-P0-1: 认证机制仍为 mock（security/auth.py 仅返回 admin）
- B-P0-3: workflows.py 8400行 monolithic（建议拆分）
- B-P2-3: 导出时 9 处独立 DB 查询可合并

---

## 探索发现 (2026-08-20)

### 架构要点
- workflows.py 是最大模块（~8400行），包含并发控制、熔断器、断点续生
- export.py 导出逻辑复杂（~2400行），支持6步文档构建
- V5.1 采用多阶段审核流程，支持人工干预
- 已修复 BUG #18（word_count 未同步）、SQL 参数错位等

### 性能瓶颈
- SQLite 写锁串行化限制并发
- 内存缓存无容量限制可能 OOM
- SSE 队列溢出静默丢弃事件

### 关键文件索引
| 文件 | 行数 | 说明 |
|---|---|---|
| `backend/app/main.py` | 450 | 应用入口、路由注册、启动钩子 |
| `backend/app/db.py` | 1935 | 数据库访问层、连接池、迁移 |
| `backend/app/services/ai/workflows.py` | 8436 | AI 工作流核心 |
| `backend/app/services/export.py` | 2484 | DOCX 导出逻辑 |
| `backend/app/routers/sse_handlers.py` | 2021 | SSE 流处理实现 |
| `backend/app/services/ai/mermaid_renderer.py` | 3557 | PIL 图表渲染引擎 |
| `backend/app/services/ai/provider_factory.py` | 547 | AI 提供商工厂 |
| `frontend/src/api/index.ts` | 2675 | API 封装层 |
| `frontend/src/types/index.ts` | 653 | TypeScript 类型定义 |
