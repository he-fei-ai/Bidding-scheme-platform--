# 三大改进项执行计划

## 1. 拆分超大文件

### workflows.py (407KB → 8个子模块)
```
workflows/                    # 包目录
├── __init__.py               # 兼容导出：from .workflows import X → from .workflows.x import X
├── base.py                   # DynamicSemaphore, AdaptiveConcurrencyController, IncrementalGenerationState
├── outline.py                # generate_outline dispatcher + v3/v51/step_by_step/one_shot 
├── outline_helpers.py        # _fill_template, _normalize_outline_list_response, _is_outline_complete, _parse_outline_text_to_list
├── outline_v51.py            # _self_check_v51, _fix_mixed_language/overlap/count_v51, _call_ai_v51
├── content.py                # generate_section_content, generate_sections, _do_generate_section_with_optimizations
├── mermaid_gen.py            # generate_flowchart/gantt/architecture/labor/comparison...
├── templates.py              # CHAPTER_TITLE_TEMPLATES, FIXED_28_CHAPTERS, FIXED_REFERENCE_CHAPTERS
```
**风险**: 中等 — 大量内部交叉引用，__init__.py 需兼容旧导入路径
**验证**: 全量 pytest 通过 + 导入测试

### export.py (106KB → 5个子模块)
```
export/
├── __init__.py
├── cache.py                  # ChartCache, export cache, inline_marker_cache
├── styles.py                 # heading styles, fonts, formatting
├── content.py                # _add_parsed_content, _add_table, _add_image, parse_content
├── builder.py                # build_docx (主流程)
```
**风险**: 低 — export 是叶子模块，外部依赖少

### sse_handlers.py (103KB → 3个子模块)
```
sse_handlers/
├── __init__.py
├── common.py                 # _NotifyQueue, _try_get_provider, _extract_project_context
├── outline.py                # generate_outline_sse_impl
├── content.py                # generate_content_impl, expand_content_impl
```
**风险**: 低 — 函数间引用清晰

### mermaid_renderer.py (152KB → 5个子模块)
```
mermaid_renderer/
├── __init__.py
├── fonts.py                  # 字体检测 + 渲染基础工具
├── cache.py                  # ChartCache
├── flowchart.py              # 流程图解析 + 渲染
├── gantt.py                  # 甘特图解析 + 渲染
├── architecture.py           # 架构图渲染
├── other_charts.py           # labor/comparison/layout/timeline
```
**风险**: 低 — 纯函数，无外部依赖

---

## 2. SQLite 写锁优化

**现状**: 全局 `threading.RLock` 串行化所有写操作

**方案**: 改用**异步写队列** + **批量合并**
- 写操作提交到 `asyncio.Queue`（非阻塞）
- 后台消费者串行执行，同一表的相邻写操作可合并
- 读操作始终绕过锁（已实现）
- 紧急写操作（如 SSE 进度推送）使用高优先级队列

**不做的方案**: 分库 / PostgreSQL 迁移（scope 太大）

---

## 3. 缓存监控

**新增 API**: `GET /api/v1/cache/stats`
- 返回所有缓存实例的命中率、大小、TTL 配置
- 覆盖：`_MemoryCache`, `_inline_marker_cache`, `ChartCache`, `_EXPORT_CACHE`

**前端**: 在 `CacheMonitorPage.tsx` 新增展示面板

---

## 执行顺序

1. ✅ **拆分 files**（先做——低风险，给后续重构打好基础）
2. ⬜ **SQLite 写锁优化**（中等风险，需仔细验证）
3. ⬜ **缓存监控**（新功能，无风险）

先确认方案，还是直接开始执行？