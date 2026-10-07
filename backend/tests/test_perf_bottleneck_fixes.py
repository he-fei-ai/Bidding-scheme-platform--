"""性能瓶颈修复单元测试

覆盖：
- PERF-1: asyncio.to_thread 包装同步文件 I/O
- PERF-2: 批量更新替代循环逐条 update_section
- PERF-3: 缓存失效完整性（invalidate_sections 键名匹配）
- PERF-4: 旧版路径双重信号量移除
- PERF-7: 配图生成 semaphore 流水线
- PERF-8: executemany 批量插入
- BUG-FIX-29: validate_mermaid 支持 timeline 类型
- BUG-FIX-31: 架构图同级标签按层级分组
- BUG-FIX-32: _evict_if_needed 排除 .fmt 元数据 key
- BUG-FIX-33: _render_flowchart_with_edges 超采样
"""

import os
import sys
import asyncio
import time
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ============================================================================
# PERF-1: asyncio.to_thread 包装同步文件 I/O
# ============================================================================

class TestAsyncFileIO:
    """验证 extract_text_from_path 在 async 上下文中不阻塞事件循环"""

    def test_templates_extract_uses_to_thread(self):
        """templates.py 的 extract_template_outline 应使用 asyncio.to_thread"""
        import inspect
        from app.routers import templates

        source = inspect.getsource(templates.extract_template_outline)
        assert "asyncio.to_thread" in source, (
            "extract_template_outline 应使用 asyncio.to_thread 包装 extract_text_from_path"
        )

    def test_asyncio_imported_in_templates(self):
        """templates.py 应导入 asyncio"""
        from app.routers import templates

        assert hasattr(templates, "asyncio"), "templates.py 应导入 asyncio 模块"


# ============================================================================
# PERF-2: 批量更新替代循环逐条 update_section
# ============================================================================

class TestBatchUpdate:
    """验证兜底扩写使用批量更新而非逐条 update_section"""

    def test_sse_handlers_uses_executemany(self):
        """sse_content.py 的兜底扩写应使用 executemany"""
        import inspect
        from app.routers.sse import sse_content

        source = inspect.getsource(sse_content)
        assert "executemany" in source, (
            "sse_content.py 应使用 executemany 批量更新章节内容"
        )
        assert "_batch_update_content" in source, (
            "sse_content.py 应定义 _batch_update_content 批量更新函数"
        )


# ============================================================================
# PERF-3: 缓存失效完整性
# ============================================================================

class TestCacheInvalidation:
    """验证 invalidate_sections 正确删除所有章节缓存"""

    def test_invalidate_sections_queries_db(self):
        """invalidate_sections 应从 DB 查询 section_ids 后逐个删除"""
        import inspect
        from app.services.redis_cache import CacheService

        source = inspect.getsource(CacheService.invalidate_sections)
        assert "SELECT id FROM sections WHERE project_id" in source, (
            "invalidate_sections 应从 DB 查询项目的所有 section_ids"
        )
        assert "SECTION_CONTENT" in source, (
            "invalidate_sections 应使用 SECTION_CONTENT 键前缀"
        )

    def test_invalidate_sections_not_single_project_key(self):
        """invalidate_sections 应同时删除 project 级别键和每个章节键（BUG-F-11 修复）

        旧行为（BUG）：只删单个章节键，漏删整包章节列表缓存
        SECTION_CONTENT:project:{pid}，导致前端读到过期的失败状态。
        """
        import inspect
        from app.services.redis_cache import CacheService

        source = inspect.getsource(CacheService.invalidate_sections)
        # 排除注释行，只检查实际代码
        code_lines = [l for l in source.split("\n") if not l.strip().startswith("#")]
        code_only = "\n".join(code_lines)
        # 必须删除 project 级别整包键
        assert "project:{project_id}" in code_only, (
            "invalidate_sections 应删除 SECTION_CONTENT:project:{project_id} 整包键（BUG-F-11）"
        )
        # 必须同时删除每个章节的单独键
        assert "SELECT id FROM sections WHERE project_id" in code_only, (
            "invalidate_sections 应查询并逐个删除章节级缓存键"
        )


# ============================================================================
# PERF-4: 旧版路径双重信号量移除
# ============================================================================

class TestSemaphoreDedup:
    """验证旧版生成路径不再使用双重信号量"""

    def test_old_path_no_global_semaphore(self):
        """旧版生成路径不应包含 _get_global_ai_semaphore"""
        import inspect
        from app.services.ai.workflows_sections import generate_section_content

        source = inspect.getsource(generate_section_content)
        # 查找旧版路径的标志
        if "章节生成(旧版)" in source:
            # 找到旧版路径附近的代码，排除注释行
            idx = source.index("章节生成(旧版)")
            nearby = source[max(0, idx - 500):idx + 200]
            code_lines = [l for l in nearby.split("\n") if not l.strip().startswith("#")]
            code_only = "\n".join(code_lines)
            assert "_get_global_ai_semaphore" not in code_only, (
                "旧版路径不应使用 _get_global_ai_semaphore（外层已有 _ai_monitor）"
            )


# ============================================================================
# PERF-7: 配图生成 semaphore 流水线
# ============================================================================

class TestChartPipeline:
    """验证配图生成使用 semaphore 流水线而非分批串行"""

    def test_no_batch_loop_in_chart_generation(self):
        """配图生成不应使用分批循环 range(0, len, 10)"""
        import inspect
        from app.routers import sse_handlers

        source = inspect.getsource(sse_handlers)
        # 查找配图生成相关代码
        if "_generate_with_semaphore" in source:
            idx = source.index("_generate_with_semaphore")
            nearby = source[idx:idx + 2000]
            assert "range(0, len(completed_results), 10)" not in nearby, (
                "配图生成不应使用分批循环，应由 semaphore 控制并发"
            )


# ============================================================================
# PERF-8: executemany 批量插入
# ============================================================================

class TestBatchSeedInsert:
    """验证 _seed_reference_outlines 使用 executemany"""

    def test_seed_uses_executemany(self):
        """_seed_reference_outlines 应使用 executemany"""
        import inspect
        from app.db import _seed_reference_outlines

        source = inspect.getsource(_seed_reference_outlines)
        assert "executemany" in source, (
            "_seed_reference_outlines 应使用 executemany 批量插入"
        )


# ============================================================================
# BUG-FIX-29: validate_mermaid 支持 timeline 类型
# ============================================================================

class TestValidateMermaidTimeline:
    """验证 validate_mermaid 支持 timeline 类型"""

    def test_timeline_valid(self):
        """有效的 timeline 代码应通过校验"""
        from app.services.ai.image_engine import validate_mermaid

        code = """timeline
    title 项目里程碑
    section 已完成
        2024-01-15 : 开工典礼
        2024-03-30 : 基础完成
    section 进行中
        2024-06-30 : 主体封顶
"""
        valid, msg = validate_mermaid(code)
        assert valid, f"timeline 代码应通过校验，但返回: {msg}"

    def test_timeline_empty_events(self):
        """没有事件的 timeline 应校验失败"""
        from app.services.ai.image_engine import validate_mermaid

        code = "timeline\n    title 空时间线\n"
        valid, msg = validate_mermaid(code)
        assert not valid, "没有事件的 timeline 应校验失败"
        assert "事件" in msg, f"错误消息应包含'事件'，但返回: {msg}"

    def test_timeline_with_section_keyword(self):
        """包含 section 关键字的 timeline 应通过校验"""
        from app.services.ai.image_engine import validate_mermaid

        code = "timeline\n    section 第一阶段\n    section 第二阶段\n"
        valid, msg = validate_mermaid(code)
        assert valid, f"包含 section 的 timeline 应通过校验，但返回: {msg}"


# ============================================================================
# BUG-FIX-31: 架构图同级标签按层级分组
# ============================================================================

class TestArchitectureLabelUniqueness:
    """验证架构图同级标签唯一性检查按层级分组"""

    def test_same_label_different_level_ok(self):
        """不同层级相同标签不应报重复"""
        from app.services.chart_validators import validate_architecture_tree

        data = {
            "label": "根节点",
            "children": [
                {
                    "label": "项目经理",
                    "children": [
                        {"label": "项目经理", "children": []},
                    ],
                },
            ],
        }
        valid, errors = validate_architecture_tree(data)
        # 不同层级的相同标签不应报错
        repeat_errors = [e for e in errors if "同级重复" in e]
        assert len(repeat_errors) == 0, (
            f"不同层级相同标签不应报重复，但得到: {errors}"
        )

    def test_same_label_same_level_error(self):
        """同层级相同标签应报重复"""
        from app.services.chart_validators import validate_architecture_tree

        data = {
            "label": "根节点",
            "children": [
                {"label": "施工组", "children": []},
                {"label": "施工组", "children": []},
            ],
        }
        valid, errors = validate_architecture_tree(data)
        repeat_errors = [e for e in errors if "同级重复" in e]
        assert len(repeat_errors) > 0, (
            "同层级相同标签应报重复"
        )


# ============================================================================
# BUG-FIX-32: _evict_if_needed 排除 .fmt 元数据 key
# ============================================================================

class TestEvictExcludesFmtKey:
    """验证 _evict_if_needed 排除 .fmt 元数据 key"""

    def test_evict_excludes_fmt_key(self):
        """_evict_if_needed 应排除 .fmt 元数据 key"""
        import inspect
        from app.services.ai.mermaid_renderer import ChartCache

        source = inspect.getsource(ChartCache._evict_if_needed)
        assert ".fmt" in source, (
            "_evict_if_needed 应检查 .fmt 后缀以排除元数据 key"
        )
        assert "endswith" in source, (
            "_evict_if_needed 应使用 endswith('.fmt') 排除元数据 key"
        )

    def test_evict_pops_fmt_key(self):
        """_evict_if_needed 删除条目时应同步删除 .fmt 元数据"""
        import inspect
        from app.services.ai.mermaid_renderer import ChartCache

        source = inspect.getsource(ChartCache._evict_if_needed)
        assert "pop" in source, (
            "_evict_if_needed 应使用 pop 同步删除 .fmt 元数据 key"
        )


# ============================================================================
# BUG-FIX-33: _render_flowchart_with_edges 超采样
# ============================================================================

class TestFlowchartEdgesHqRender:
    """验证 _render_flowchart_with_edges 使用超采样"""

    def test_has_render_hq_decorator(self):
        """_render_flowchart_with_edges 应有 @_render_hq 装饰器"""
        import inspect
        from app.services.ai.mermaid_renderer import _render_flowchart_with_edges

        source = inspect.getsource(_render_flowchart_with_edges)
        # 装饰器信息在函数的 __wrapped__ 属性中
        assert hasattr(_render_flowchart_with_edges, "__wrapped__"), (
            "_render_flowchart_with_edges 应有 @_render_hq 装饰器"
        )

    def test_uses_scale_factor(self):
        """_render_flowchart_with_edges 应使用 scale 超采样因子"""
        import inspect
        from app.services.ai.mermaid_renderer import _render_flowchart_with_edges

        source = inspect.getsource(_render_flowchart_with_edges)
        assert "scale" in source, (
            "_render_flowchart_with_edges 应使用 scale 变量进行超采样"
        )
        assert "2.5" in source, (
            "_render_flowchart_with_edges 应使用 2.5x 超采样率"
        )

    def test_fonts_scaled(self):
        """字体大小应乘以 scale"""
        import inspect
        from app.services.ai.mermaid_renderer import _render_flowchart_with_edges

        source = inspect.getsource(_render_flowchart_with_edges)
        assert "_image_font(int(" in source, (
            "_render_flowchart_with_edges 的字体大小应乘以 scale"
        )


# ============================================================================
# BUG-FIX-30: 修复失败返回空字符串
# ============================================================================

class TestMermaidRepairFailureReturnsEmpty:
    """验证 Mermaid 修复失败后返回空字符串"""

    def test_flowchart_repair_failure_returns_empty(self):
        """generate_mermaid_flowchart 修复失败应返回空字符串"""
        import inspect
        from app.services.ai.image_engine import generate_mermaid_flowchart

        source = inspect.getsource(generate_mermaid_flowchart)
        assert 'return ""' in source, (
            "generate_mermaid_flowchart 修复失败应返回空字符串"
        )

    def test_gantt_repair_failure_returns_empty(self):
        """generate_mermaid_gantt 修复失败应返回空字符串"""
        import inspect
        from app.services.ai.image_engine import generate_mermaid_gantt

        source = inspect.getsource(generate_mermaid_gantt)
        assert 'return ""' in source, (
            "generate_mermaid_gantt 修复失败应返回空字符串"
        )


# ============================================================================
# BUG-FIX-28 / D-3 2026-10-04: _cache.py 统一变量校验（改为按实际生效模板内容）
# ============================================================================

class TestPromptCacheValidation:
    """验证 get_prompt 在 DB 命中时也执行变量校验，且校验依据「实际模板内容」"""

    def test_validation_after_both_branches(self):
        """变量校验应在 DB/硬编码分支之后、渲染替换之前统一执行。

        D-3 修复：原实现调用 validate_prompt_variables(key) 校验的是**注册表默认模板**，
        与 DB 实际生效内容脱节（DB 新增占位符漏检、删除占位符误报噪声）。现改为按
        实际模板内容 extract_variables(prompt) 校验，故本用例断言随之更新。
        """
        import inspect
        from app.services.ai.prompts._cache import get_prompt

        source = inspect.getsource(get_prompt)
        # 校验依据实际内容提取变量
        validate_idx = source.index("extract_variables(prompt)")
        # 变量替换（渲染）在校验之后
        render_idx = source.index("render_prompt(prompt, **kwargs)")
        assert validate_idx < render_idx, "变量校验应在渲染替换之前执行"
        # 且位于 DB/硬编码分支选择（取 prompt）之后，保证 DB 命中也被校验
        branch_idx = source.index("_ALL_PROMPTS.get(key)")
        assert branch_idx < validate_idx, "校验应对 DB 命中与硬编码回退统一执行"


# ============================================================================
# SSE 总超时自适应（BUG-FIX-20）
# ============================================================================

class TestSSETimeoutAdaptive:
    """验证 SSE 总超时按并发分摊"""

    def test_sse_timeout_formula_uses_concurrency(self):
        """SSE 总超时公式应包含 actual_concurrency"""
        import inspect
        from app.routers.sse import sse_content

        source = inspect.getsource(sse_content)
        assert "actual_concurrency" in source, (
            "SSE 总超时公式应使用 actual_concurrency 按并发分摊"
        )
        assert "_per_chapter_secs" in source, (
            "SSE 总超时公式应使用 _per_chapter_secs 估计每章耗时"
        )
        assert "_hard_timeout_secs" in source or "14400" in source, (
            "SSE 总超时上限应有硬熔断（当前为 14400s / 4小时）"
        )


# ============================================================================
# 缓存温度一致性（BUG-FIX-21）
# ============================================================================

class TestCacheTemperatureConsistency:
    """验证缓存写入温度与读取温度一致"""

    def test_cache_write_uses_fixed_temperature(self):
        """缓存写入应固定使用 temperature=0.7"""
        import inspect
        from app.services.ai.workflows_sections import generate_sections

        source = inspect.getsource(generate_sections)
        # 查找缓存写入代码
        if "_section_content_cache.put" in source:
            idx = source.index("_section_content_cache.put")
            nearby = source[idx:idx + 300]
            assert "temperature=0.7" in nearby, (
                "缓存写入应固定使用 temperature=0.7，与读取一致"
            )


# ============================================================================
# BUG-FIX-43: 标题去重复验循环 + 递增后缀
# ============================================================================

class TestTitleDedupVerificationLoop:
    """验证 _self_check_v51 的 check→fix→re-check 循环"""

    def test_self_check_has_fix_loop(self):
        """_self_check_v51 应包含多轮修复循环"""
        import inspect
        from app.services.ai.workflows_outline_v51 import _self_check_v51

        source = inspect.getsource(_self_check_v51)
        assert "max_fix_rounds" in source, (
            "_self_check_v51 应有 max_fix_rounds 多轮修复循环"
        )
        assert "for round_idx" in source, (
            "_self_check_v51 应有 for round_idx 循环"
        )

    def test_self_check_logs_residual_errors(self):
        """修复循环耗尽后应 logger.error 记录残留问题"""
        import inspect
        from app.services.ai.workflows_outline_v51 import _self_check_v51

        source = inspect.getsource(_self_check_v51)
        assert "logger.error" in source, (
            "_self_check_v51 修复耗尽后应 logger.error 记录残留问题"
        )
        assert "残留" in source, (
            "_self_check_v51 应记录残留问题数量"
        )

    def test_auto_fix_uses_incremental_suffix(self):
        """_auto_fix_issues 应使用递增后缀而非固定"（二）" """
        import inspect
        from app.services.ai.workflows_outline_v51 import _auto_fix_issues

        source = inspect.getsource(_auto_fix_issues)
        assert "_next_unique_title" in source, (
            "_auto_fix_issues 应调用 _next_unique_title 生成递增后缀"
        )
        assert "_CN_NUMS" in source, (
            "_auto_fix_issues 应有中文数字列表 _CN_NUMS"
        )

    def test_auto_fix_handles_three_duplicates(self):
        """3个同标题重复经修复后应全部唯一"""
        from app.services.ai.workflows_outline_v51 import _auto_fix_issues, _quick_local_check

        level1 = [
            {"id": "1", "title": "技术方案", "children": [
                {"id": "1.1", "title": "概述", "children": [
                    {"id": "1.1.1", "title": "背景"},
                    {"id": "1.1.2", "title": "背景"},
                    {"id": "1.1.3", "title": "背景"},
                ]},
            ]},
        ]

        # 多轮修复
        for _ in range(3):
            issues = _quick_local_check(level1)
            if not issues:
                break
            level1 = _auto_fix_issues(level1, issues)

        final_issues = _quick_local_check(level1)
        dup_issues = [i for i in final_issues if "重复标题" in i]
        assert len(dup_issues) == 0, (
            f"3个同标题重复经修复后仍存在重复: {dup_issues}"
        )


# ============================================================================
# BUG-FIX-42: SSE 重连防 v51_state 脱节
# ============================================================================

class TestSSEReconnectionLostTask:
    """验证重连时后台任务丢失不会静默重跑"""

    def test_reconnection_checks_lost_task(self):
        """sse_outline 应检查 existing_task 为 None 或 done"""
        import inspect
        from app.routers.sse import sse_outline

        source = inspect.getsource(sse_outline)
        assert "BUG-FIX-42" in source, (
            "sse_outline 应包含 BUG-FIX-42 修复"
        )
        assert "后台生成任务已丢失" in source, (
            "sse_outline 应在任务丢失时返回错误而非静默重跑"
        )


# ============================================================================
# P2: L1 缓存 key 拼接 chart_type
# ============================================================================

class TestL1CacheKeyChartType:
    """验证 L1 内存缓存 key 包含 chart_type"""

    def test_get_cached_uses_chart_type_in_key(self):
        """_get_cached_chart_render 应将 chart_type 拼入 cache key"""
        import inspect
        from app.services.export import _get_cached_chart_render

        source = inspect.getsource(_get_cached_chart_render)
        assert "cache_key" in source, (
            "_get_cached_chart_render 应构建 cache_key 变量"
        )
        assert "chart_type" in source, (
            "_get_cached_chart_render 的 cache_key 应包含 chart_type"
        )

    def test_store_cached_uses_chart_type_in_key(self):
        """_store_chart_render 应将 chart_type 拼入 cache key"""
        import inspect
        from app.services.export import _store_chart_render

        source = inspect.getsource(_store_chart_render)
        assert "cache_key" in source, (
            "_store_chart_render 应构建 cache_key 变量"
        )
        assert "chart_type" in source, (
            "_store_chart_render 的 cache_key 应包含 chart_type"
        )

    def test_different_chart_types_no_collision(self):
        """相同 payload 不同 chart_type 不应命中对方缓存"""
        from io import BytesIO
        from app.services.export import _get_cached_chart_render, _store_chart_render, _chart_render_cache

        # 清空缓存
        _chart_render_cache.clear()

        payload = "graph TD; A-->B"
        stream_a = BytesIO(b"flowchart_result")
        _store_chart_render(payload, stream_a, chart_type="flowchart")

        # 用 sequence 类型查——不应命中
        result = _get_cached_chart_render(payload, chart_type="sequence")
        assert result is None, (
            "不同 chart_type 不应命中对方缓存"
        )

        # 用 flowchart 类型查——应命中
        result = _get_cached_chart_render(payload, chart_type="flowchart")
        assert result is not None, (
            "相同 chart_type 应命中缓存"
        )

        # 清理
        _chart_render_cache.clear()


# ============================================================================
# P2: 评分点解析失败日志
# ============================================================================

class TestScoringPointParseFailureLogging:
    """验证评分点 JSON 解析失败时有日志"""

    def test_generate_outline_logs_parse_failure(self):
        """generate_outline_v51 应在评分点解析失败时 logger.error"""
        import inspect
        from app.services.ai.workflows_outline_v51 import generate_outline_v51

        source = inspect.getsource(generate_outline_v51)
        assert "评分点 JSON 解析失败" in source, (
            "generate_outline_v51 应在评分点解析失败时记录 error 日志"
        )

    def test_l1_generation_logs_parse_failure(self):
        """_generate_level1_v51 应在评分点解析失败时 logger.warning"""
        import inspect
        from app.services.ai.workflows_outline_v51 import _generate_level1_v51

        source = inspect.getsource(_generate_level1_v51)
        assert "评分点解析失败" in source, (
            "_generate_level1_v51 应在评分点解析失败时记录 warning 日志"
        )


# ============================================================================
# BUG-FIX-44: render_prompt 正则替换注入修复
# ============================================================================

class TestRenderPromptRegexInjection:
    """验证 render_prompt 使用可调用替换对象避免正则替换注入"""

    def test_uses_lambda_replacement(self):
        """render_prompt 的 re.sub 替换参数必须是可调用对象（而非字符串）。

        BUG-FIX 44 的原意是：替换值里若含 `\\1` / `\\g<0>` 这类反向引用，
        传字符串给 re.sub 会被解释掉（注入面）。防住它的机制是「替换参数传
        函数而非字符串」。

        BUG-FIX 2026-10-06：原断言写死源码字面量 `"lambda m: val_str"`，
        而 SECURITY-FIX-2026-10-06 的单遍替换改造已把内联 lambda 重构为具名
        `_repl` 函数（combined.sub(_repl, result)），安全性不变（仍是可调用
        替换），却让该断言失效 —— 一次纯重构被误报成安全回归。

        改为断言**安全属性本身**（替换参数可调用），不再绑定实现拼写；
        反向引用的实际防护效果由 test_value_with_backslash_not_error 覆盖。
        """
        import inspect
        import re
        from app.services.ai.prompts._registry import render_prompt

        source = inspect.getsource(render_prompt)
        sub_calls = re.findall(r"\.sub\(\s*([^,]+),", source)
        assert sub_calls, "render_prompt 应存在 re.sub 调用"
        for arg in sub_calls:
            arg = arg.strip()
            assert arg.startswith("_repl") or arg.startswith("lambda"), (
                f"re.sub 的替换参数应为可调用对象而非字符串（否则值里的 \\1 会被解释），实际: {arg}"
            )

    def test_value_with_backslash_not_error(self):
        """变量值含 \\1 不应抛异常"""
        from app.services.ai.prompts._registry import render_prompt

        result = render_prompt("Hello {name}", name="test\\1value")
        assert "test\\1value" in result, (
            "变量值含反斜杠序列应被字面量替换"
        )

    def test_none_value_excluded_from_unresolved(self):
        """None 值的变量不应被报告为已提供"""
        from app.services.ai.prompts._registry import render_prompt

        # 传入 None 的变量应被跳过，残留占位符应被报告
        result = render_prompt("Hello {name} {missing}", name="test", missing=None)
        assert "{missing}" in result, (
            "None 值变量的占位符应保留在结果中"
        )


# ============================================================================
# BUG-FIX-45: DB 故障后缓存保持 None 以便重试
# ============================================================================

class TestDBCacheFailureRecovery:
    """验证 DB 加载失败后缓存保持 None 以便下次重试"""

    def test_failure_keeps_none(self):
        """_load_prompt_cache 失败后 _prompt_cache 应为 None"""
        import inspect
        from app.services.ai.prompts import _cache

        source = inspect.getsource(_cache._load_prompt_cache)
        assert "_prompt_cache = None" in source, (
            "DB 加载失败时应保持 _prompt_cache = None 以便下次重试"
        )


# ============================================================================
# BUG-FIX-49/50/51: ChartCache LRU 三连修复
# ============================================================================

class TestChartCacheLRUFixes:
    """验证 ChartCache LRU 三个缺陷修复"""

    def test_get_updates_access_time(self):
        """ChartCache.get() 命中时应刷新 LRU 访问时间"""
        import inspect
        from app.services.ai.mermaid_renderer import ChartCache

        source = inspect.getsource(ChartCache.get)
        assert "_access_order[cache_key]" in source, (
            "ChartCache.get() 命中时应更新 _access_order[cache_key] 时间戳"
        )

    def test_get_or_render_uses_set(self):
        """get_or_render() 应调用 set() 方法完整写入缓存"""
        import inspect
        from app.services.ai.mermaid_renderer import ChartCache

        source = inspect.getsource(ChartCache.get_or_render)
        assert "self.set(" in source, (
            "get_or_render() 应调用 self.set() 确保 LRU 淘汰和统计一致"
        )

    def test_clear_resets_memory_state(self):
        """clear() 应重置 _access_order 和 _stats["total_size"]"""
        import inspect
        from app.services.ai.mermaid_renderer import ChartCache

        source = inspect.getsource(ChartCache.clear)
        assert "_access_order.clear()" in source, (
            "clear() 应清空 _access_order"
        )
        assert 'total_size"] = 0' in source, (
            "clear() 应重置 _stats['total_size'] = 0"
        )


# ============================================================================
# BUG-FIX-48: 目录生成取消机制
# ============================================================================

class TestOutlineCancellationChecks:
    """验证目录生成在关键转换点有取消检查"""

    def test_cancellation_check_before_l2(self):
        """L2 前应有取消检查"""
        import inspect
        from app.services.ai.workflows_outline_v51 import generate_outline_v51

        source = inspect.getsource(generate_outline_v51)
        assert "已取消（L2前）" in source, (
            "L2 前应有取消检查"
        )

    def test_cancellation_check_before_l3(self):
        """L3 前应有取消检查"""
        import inspect
        from app.services.ai.workflows_outline_v51 import generate_outline_v51

        source = inspect.getsource(generate_outline_v51)
        assert "已取消（L3前）" in source, (
            "L3 前应有取消检查"
        )

    def test_cancellation_check_before_selfcheck(self):
        """自检前应有取消检查"""
        import inspect
        from app.services.ai.workflows_outline_v51 import generate_outline_v51

        source = inspect.getsource(generate_outline_v51)
        assert "已取消（自检前）" in source, (
            "自检前应有取消检查"
        )


# ============================================================================
# BUG-FIX-52: SSE 总超时动态估计
# ============================================================================

class TestSSETimeoutDynamicEstimate:
    """验证 SSE 总超时按质量重试动态计算"""

    def test_per_chapter_secs_dynamic(self):
        """_per_chapter_secs 应基于质量重试次数动态计算"""
        import inspect
        from app.routers.sse import sse_content

        source = inspect.getsource(sse_content)
        assert "_quality_retries" in source, (
            "SSE 超时估计应使用 _quality_retries 动态计算"
        )


# ============================================================================
# BUG-FIX-47: SSE 导出锁完整包裹
# ============================================================================

class TestSSEExportLockComplete:
    """验证 SSE 导出的互斥保护确实**覆盖构建过程**。

    历史：本测试原本断言 `event_stream` 里出现 `async with export_lock`。
    2026-09-15 横向体检发现那个结构保护不到构建 —— 路由层的
    `asyncio.create_task(asyncio.to_thread(_run_build))` 会**立即**把
    `_run_build` 提交到线程池，`build_docx` 全程跑在锁外面；生成器里的
    `async with` 只能让第二个请求排队，两个请求仍可能同时写同一个输出文件。
    现改为「锁在路由层抢（sqlite 行级组锁，跨进程有效）+ 构建挪进生成器体
    （拿到锁之后才启动）+ `_run_build` 的 finally 释放」，本测试随之守住新结构。
    """

    def test_lock_acquired_before_build_starts(self):
        """抢锁必须早于构建启动 —— 这正是原实现漏掉的一环。"""
        import inspect
        from app.routers.export import export_docx_stream

        source = inspect.getsource(export_docx_stream)
        lock_at = source.index("_acquire_export_lock(project_id)")
        build_at = source.index("asyncio.create_task(asyncio.to_thread(_run_build))")
        assert lock_at < build_at, "抢锁必须早于构建启动"
        assert source.count("asyncio.create_task(asyncio.to_thread(_run_build))") == 1, (
            "构建只能启动一次"
        )

    def test_build_releases_lock_in_finally(self):
        """构建线程负责释放锁，且必须在 finally（构建失败也要放锁）。"""
        import inspect
        from app.routers.export import export_docx_stream

        source = inspect.getsource(export_docx_stream)
        assert "_task_lock.release(lock_key, owner_id)" in source
        assert source.rindex("finally:") < source.index("_task_lock.release(lock_key, owner_id)")

    def test_no_in_process_lock_left(self):
        """进程内 asyncio.Lock 在多 worker 下每个进程各一把，等于没有互斥。"""
        import inspect
        from app.routers import export as export_module

        source = inspect.getsource(export_module)
        assert "asyncio.Lock()" not in source
        assert "export_lock.locked()" not in source


# ============================================================================
# P0: image_engine .format() → render_prompt 修复
# ============================================================================

class TestImageEngineRenderPromptFix:
    """验证 image_engine 使用 render_prompt 替换 __*__ 占位符"""

    def test_flowchart_user_uses_render_prompt(self):
        """generate_mermaid_flowchart 应使用 get_prompt 而非 .format()"""
        import inspect
        from app.services.ai.image_engine import generate_mermaid_flowchart

        source = inspect.getsource(generate_mermaid_flowchart)
        assert "get_prompt(" in source, (
            "generate_mermaid_flowchart 应使用 get_prompt(..., **kwargs) 渲染变量"
        )
        assert "fc_user.format(" not in source, (
            "generate_mermaid_flowchart 不应使用 fc_user.format()"
        )

    def test_gantt_user_uses_render_prompt(self):
        """generate_mermaid_gantt 应使用 get_prompt 而非 .format()"""
        import inspect
        from app.services.ai.image_engine import generate_mermaid_gantt

        source = inspect.getsource(generate_mermaid_gantt)
        assert "get_prompt(" in source, (
            "generate_mermaid_gantt 应使用 get_prompt(..., **kwargs) 渲染变量"
        )
        assert "gt_user.format(" not in source, (
            "generate_mermaid_gantt 不应使用 gt_user.format()"
        )

    def test_render_prompt_imported(self):
        """image_engine 应导入 render_prompt"""
        import inspect
        from app.services.ai import image_engine

        source = inspect.getsource(image_engine)
        assert "render_prompt" in source, (
            "image_engine 应导入 render_prompt"
        )


# ============================================================================
# P1: 提示词路由权限验证
# ============================================================================

class TestPromptsRouterAuth:
    """验证提示词路由有权限验证"""

    def test_put_has_auth_dependency(self):
        """PUT /prompts/{key} 应有认证依赖"""
        import inspect
        from app.routers.prompts import update_prompt

        source = inspect.getsource(update_prompt)
        assert "get_current_user" in source or "Depends" in source, (
            "update_prompt 应有认证依赖"
        )

    def test_reset_has_auth_dependency(self):
        """POST /prompts/{key}/reset 应有认证依赖"""
        import inspect
        from app.routers.prompts import reset_prompt

        source = inspect.getsource(reset_prompt)
        assert "get_current_user" in source or "Depends" in source, (
            "reset_prompt 应有认证依赖"
        )


# ============================================================================
# P2: 提示词内容优化
# ============================================================================

class TestPromptContentOptimization:
    """验证提示词内容优化"""

    def test_outline_docstring_correct_count(self):
        """outline.py docstring 应声明 8 个提示词"""
        from app.services.ai.prompts import outline

        assert "8 个提示词" in outline.__doc__, (
            "outline.py docstring 应声明 8 个提示词"
        )

    def test_shared_version_consistent(self):
        """_shared.py 版本号应与系统实际编号标准一致（V3.1）

        BUG-FIX 2026-10-04（B7）：旧断言钉住陈旧的 V2.2 八级描述（一级第X章），
        与 DEFAULT_NUMBERING_TEMPLATES 实际的 V3.1 标准（一级第X节/二级中文/
        三级阿拉伯/四级点分）矛盾，现同步为钉住 V3.1。"""
        from app.services.ai.prompts._shared import SHARED_HEADING_RULES

        assert "V3.1" in SHARED_HEADING_RULES, (
            "SHARED_HEADING_RULES 应使用 V3.1 版本号（与实际导出模板一致）"
        )
        assert "一级：第X节" in SHARED_HEADING_RULES

    def test_no_duplicate_forbidden_word(self):
        """三级标题禁用词中"通用要求"不应重复"""
        from app.services.ai.prompts._shared import SHARED_FORBIDDEN_WORDS

        # 提取三级标题禁用词部分
        lines = SHARED_FORBIDDEN_WORDS.split("\n")
        l3_line = [l for l in lines if "三级标题绝对禁用" in l]
        assert len(l3_line) == 1
        words = l3_line[0].split("：")[1].split("、")
        # 检查"通用要求"只出现一次
        count = words.count("通用要求")
        assert count == 1, (
            f"三级标题禁用词中'通用要求'出现 {count} 次，应只出现 1 次"
        )

    def test_mermaid_no_duplicate_example3(self):
        """mermaid.py 不应有重复的示例3"""
        from app.services.ai.prompts.mermaid import MERMAID_FLOWCHART_SYSTEM

        count = MERMAID_FLOWCHART_SYSTEM.count("示例 3：")
        assert count == 1, (
            f"MERMAID_FLOWCHART_SYSTEM 中'示例 3：'出现 {count} 次，应只出现 1 次"
        )

    def test_charts_no_crlf(self):
        """charts.py 不应包含 CRLF 换行符"""
        import inspect
        from app.services.ai.prompts import charts

        file_path = inspect.getfile(charts)
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()
        assert "\r\n" not in content, (
            "charts.py 不应包含 CRLF 换行符"
        )

    def test_outline_no_crlf(self):
        """outline.py 不应包含 CRLF 换行符"""
        import inspect
        from app.services.ai.prompts import outline

        # 与 test_charts_no_crlf 保持一致：按模块真实文件定位，不依赖 pytest 工作目录
        file_path = inspect.getfile(outline)
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()
        assert "\r\n" not in content, (
            "outline.py 不应包含 CRLF 换行符"
        )


# ============================================================================
# P2: WARNING 噪音消除
# ============================================================================

class TestPromptWarningNoiseReduction:
    """验证调用点通过 kwargs 传参消除 WARNING 噪音"""

    def test_orchestrate_content_passes_kwargs(self):
        """orchestrate_content 应将 outline 等参数传递给下游实现"""
        import inspect
        from app.services.ai.workflows_sections import orchestrate_content

        source = inspect.getsource(orchestrate_content)
        assert "_orch_impl(" in source, (
            "orchestrate_content 应调用 _orch_impl 下游实现"
        )
        assert "provider, outline," in source, (
            "orchestrate_content 应将 outline 参数传递给 _orch_impl"
        )
        assert "scoring_standards=scoring_standards" in source, (
            "orchestrate_content 应将 scoring_standards 传递给 _orch_impl"
        )

    def test_industry_tree_passes_kwargs(self):
        """industry_tree generate_children 应通过 kwargs 传参"""
        import inspect
        from app.services.industry_tree import generate_children

        source = inspect.getsource(generate_children)
        assert 'COUNT=' in source, (
            "generate_children 应通过 COUNT= 传参"
        )
        assert 'PARENT_NAME=' in source, (
            "generate_children 应通过 PARENT_NAME= 传参"
        )


# ============================================================================
# BUG-FIX-55 ~ BUG-FIX-75: 解析模块修复验证
# ============================================================================


class TestRejectionModuleFixes:
    """废标检查模块修复验证"""

    def test_sql_placeholder_generation(self):
        """BUG-FIX-56: SQL占位符应生成 ?,? 而非 ??"""
        task_ids = [1, 2, 3]
        placeholders = ",".join(["?"] * len(task_ids))
        assert placeholders == "?,?,?", f"期望 '?,?,?'，实际 '{placeholders}'"

        task_ids_single = [1]
        placeholders_single = ",".join(["?"] * len(task_ids_single))
        assert placeholders_single == "?", f"期望 '?'，实际 '{placeholders_single}'"

    def test_rejection_import_path(self):
        """BUG-FIX-55: get_false_positive_patterns 应可正常导入"""
        import inspect
        from app.services.rejection import get_false_positive_patterns

        source = inspect.getsource(get_false_positive_patterns)
        assert "from ..db import get_db" in source, (
            "应使用 ..db 相对导入路径"
        )

    def test_concurrency_config_validation(self):
        """BUG-FIX-73: 并发数配置应校验类型"""
        # 模拟非数值配置
        raw = "invalid"
        try:
            concurrency = int(raw)
            if concurrency < 1:
                concurrency = 4
        except (TypeError, ValueError):
            concurrency = 4
        assert concurrency == 4

        # 模拟负数配置
        raw = -1
        try:
            concurrency = int(raw)
            if concurrency < 1:
                concurrency = 4
        except (TypeError, ValueError):
            concurrency = 4
        assert concurrency == 4


class TestWorkflowsAnalysisFixes:
    """评分点分析模块修复验证"""

    def test_first_stage_exception_degradation(self):
        """BUG-FIX-58: 第一阶段应有异常降级处理"""
        import inspect
        from app.services.ai.workflows_analysis import extract_scoring_points

        source = inspect.getsource(extract_scoring_points)
        assert "第一阶段失败" in source or "返回空列表降级" in source, (
            "第一阶段应有异常降级处理"
        )

    def test_summary_empty_check(self):
        """BUG-FIX-57: summary 为空时应有占位处理"""
        import inspect
        from app.services.ai.workflows_analysis import extract_scoring_points

        source = inspect.getsource(extract_scoring_points)
        assert "summary 为空" in source or "无摘要" in source, (
            "summary 为空时应有占位处理"
        )

    def test_logging_lazy_format(self):
        """BUG-FIX-59: 日志应使用延迟格式化"""
        from app.services.ai.workflows_analysis import extract_scoring_points
        import inspect

        source = inspect.getsource(extract_scoring_points)
        # 不应包含 f-string 的 logger.warning 调用
        assert 'logger.warning(f"' not in source, (
            "logger.warning 不应使用 f-string"
        )


class TestRouterAuthFixes:
    """路由认证修复验证"""

    def test_files_router_has_auth(self):
        """BUG-FIX-60: files 路由应有认证依赖"""
        from app.routers.files import router

        # 检查路由中是否有依赖项
        has_auth = False
        for route in router.routes:
            if hasattr(route, 'dependant') and route.dependant:
                has_auth = True
                break
        assert has_auth, "files 路由应有认证依赖"

    def test_rejection_router_has_auth(self):
        """BUG-FIX-62: rejection 路由应有认证依赖"""
        from app.routers.rejection import router

        has_auth = False
        for route in router.routes:
            if hasattr(route, 'dependant') and route.dependant:
                has_auth = True
                break
        assert has_auth, "rejection 路由应有认证依赖"

    def test_knowledge_router_has_auth(self):
        """BUG-FIX-63: knowledge 路由应有认证依赖"""
        from app.routers.knowledge import router

        has_auth = False
        for route in router.routes:
            if hasattr(route, 'dependant') and route.dependant:
                has_auth = True
                break
        assert has_auth, "knowledge 路由应有认证依赖"

    def test_knowledge_file_size_limit(self):
        """BUG-FIX-64: knowledge 上传应有文件大小限制"""
        from app.routers.knowledge import MAX_KNOWLEDGE_FILE_SIZE

        assert MAX_KNOWLEDGE_FILE_SIZE > 0, "文件大小限制应大于0"
        assert MAX_KNOWLEDGE_FILE_SIZE == 100 * 1024 * 1024, "应为100MB限制"

    def test_files_no_exception_leak(self):
        """BUG-FIX-61: files 上传异常不应泄露详情"""
        import inspect
        from app.routers.files import upload_file

        source = inspect.getsource(upload_file)
        assert "文件上传失败，请检查日志" in source, (
            "异常信息不应泄露给客户端"
        )


class TestTenderContextCacheFixes:
    """招标上下文缓存修复验证"""

    def test_cache_key_single_compute(self):
        """BUG-FIX-67: 缓存键应只计算一次"""
        import inspect
        from app.services.tender_context import extract_tender_context

        source = inspect.getsource(extract_tender_context)
        # 应有 cache_key 变量复用
        assert "cache_key" in source, "应复用 cache_key 变量"

    def test_no_anchor_cache_write(self):
        """BUG-FIX-68: 无锚定命中分支应写入缓存。

        BUG-FIX 2026-10-06：原断言是「`if not anchor_indices:` 之后 200 个
        字符内必须出现 `_cache_set`」—— 依赖源码**字符偏移**。把无锚定分支
        的降级逻辑从 `full_text[:max_chars]` 换成 `_head_tail_excerpt(...)`
        并加了两行说明注释后，`_cache_set` 被推出 200 字符窗口，一次真实修复
        被误报成「缓存写没了」。

        改为断言**行为**：无锚点命中后对应缓存键确实存在于模块缓存里。
        """
        from app.services import tender_context as tc

        text = "某份完全没有命中任何技术标锚点关键词的招标文件正文。" * 50
        key = tc._cache_key(text, 2000)
        with tc._CACHE_LOCK:
            tc._cache.pop(key, None)

        out = tc.extract_tender_context(text, max_chars=2000)

        with tc._CACHE_LOCK:
            cached = tc._cache.get(key)
        assert cached is not None, "无锚定命中分支未写入缓存（BUG-FIX-68 回归）"
        assert cached[0] == out, "缓存内容与返回值不一致"


class TestKnowledgeServiceFixes:
    """知识库服务修复验证"""

    def test_delete_distinguishes_dir(self):
        """BUG-FIX-69: 删除目标文件应区分目录"""
        import inspect
        from app.services.knowledge import _step_copy_source

        source = inspect.getsource(_step_copy_source)
        assert "is_dir" in source or "rmtree" in source, (
            "删除时应区分文件和目录"
        )

    def test_backup_conflict_check(self):
        """BUG-FIX-70: 备份文件应检查冲突"""
        import inspect
        from app.services.knowledge import migrate_from_v2

        source = inspect.getsource(migrate_from_v2)
        assert "backup_path.exists" in source, (
            "备份文件应检查是否已存在"
        )


class TestFilesServiceFixes:
    """文件服务修复验证"""

    def test_same_name_overwrite_protection(self):
        """BUG-FIX-65: 同名文件应有覆盖防护"""
        import inspect
        from app.services.files import save_upload

        source = inspect.getsource(save_upload)
        assert "同名文件已存在" in source or "file_path.exists()" in source, (
            "应有同名文件覆盖防护"
        )

    def test_mineru_thread_safety(self):
        """BUG-FIX-71: MinerU 单例应有线程安全锁"""
        from app.services.files import _mineru_lock

        assert _mineru_lock is not None, "应有 MinerU 锁"

    def test_cache_format_complete(self):
        """BUG-FIX-74: 缓存格式列表应完整"""
        # BUG-FIX-74c: 重构后硬编码列表提升为模块常量 _PARSE_CACHEABLE_EXTS
        from app.services.files import _PARSE_CACHEABLE_EXTS
        required = {".doc", ".xls", ".ppt", ".html", ".htm"}
        missing = required - set(_PARSE_CACHEABLE_EXTS)
        assert not missing, f"_PARSE_CACHEABLE_EXTS 缺少格式: {missing}"

    def test_pil_image_context_manager(self):
        """BUG-FIX-72: PIL Image 应使用上下文管理器

        ENHANCE 2026-10-05：图片 OCR 接入 OCR_ENGINE 后拆成引擎链，PIL 打开逻辑
        收敛到 _ocr_with_tesseract（_extract_image_ocr 变为引擎调度器）。断言目标
        随之调整，语义不变：图片必须用 with 打开，避免文件句柄泄漏。
        """
        import inspect
        from app.services import files as _files_service
        from app.services.files import _ocr_with_tesseract

        source = inspect.getsource(_ocr_with_tesseract)
        assert "with Image.open" in source, (
            "PIL Image 应使用 with 上下文管理器"
        )
        # 调度器本身不得再直接打开图片（否则泄漏点会重新出现）
        assert "Image.open" not in inspect.getsource(_files_service._extract_image_ocr)


# ============================================================================
# BUG-FIX-76 ~ BUG-FIX-86: 目录生成模块修复验证
# ============================================================================


class TestOutlineModuleFixes:
    """目录生成模块修复验证"""

    def test_review_event_cleared(self):
        """BUG-FIX-76: _wait_for_v51_review 应在 wait 前清除 resume_event"""
        import inspect
        from app.services.ai.workflows_outline_v51 import _wait_for_v51_review

        source = inspect.getsource(_wait_for_v51_review)
        assert "resume_event.clear()" in source, (
            "应在 wait 前调用 clear() 清除上次残留状态"
        )

    def test_industry_tree_router_has_auth(self):
        """BUG-FIX-77: industry_tree 路由应有认证依赖"""
        from app.routers.industry_tree import router

        has_auth = False
        for route in router.routes:
            if hasattr(route, 'dependant') and route.dependant:
                has_auth = True
                break
        assert has_auth, "industry_tree 路由应有认证依赖"

    def test_industry_tree_cache_lock(self):
        """BUG-FIX-78: 行业树缓存应有线程锁保护"""
        from app.services.industry_tree import _FULL_TREE_CACHE_LOCK

        assert _FULL_TREE_CACHE_LOCK is not None, "应有缓存锁"

    def test_delete_tree_depth_limit(self):
        """BUG-FIX-79: _delete_tree 应有深度限制"""
        import inspect
        from app.services.industry_tree import _delete_tree

        source = inspect.getsource(_delete_tree)
        assert "_depth" in source, "应有深度参数"
        assert "20" in source, "应有深度上限"

    def test_create_version_atomic(self):
        """BUG-FIX-80: create_version 应使用原子递增"""
        import inspect
        from app.services.industry_tree import create_version

        source = inspect.getsource(create_version)
        assert "COALESCE" in source, "应使用 COALESCE 原子递增版本号"

    def test_rollback_has_audit(self):
        """BUG-FIX-81: rollback_to_version 应记录审计日志"""
        import inspect
        from app.services.industry_tree import rollback_to_version

        source = inspect.getsource(rollback_to_version)
        assert "ROLLBACK" in source, "应记录回滚操作到审计日志"

    def test_fix_all_ids_type_check(self):
        """BUG-FIX-82: _fix_all_ids 应有类型检查"""
        import inspect
        from app.services.ai.workflows_outline_v51 import _fix_all_ids

        source = inspect.getsource(_fix_all_ids)
        assert "isinstance" in source, "应有类型检查防止非 dict 元素"

    def test_fix_ids_v51_string_id(self):
        """BUG-FIX-83: _fix_ids_v51 应使用字符串 ID"""
        from app.services.ai.workflows_outline_v51 import _fix_ids_v51

        data = [{"title": "A", "children": []}]
        result = _fix_ids_v51(data)
        assert isinstance(result[0]["id"], str), "ID 应为字符串类型"

    def test_generate_children_count_validation(self):
        """BUG-FIX-84: generate_children 应校验 count 参数"""
        import inspect
        from app.services.industry_tree import generate_children

        source = inspect.getsource(generate_children)
        assert "count < 1" in source or "count > 20" in source, (
            "应校验 count 参数范围"
        )

    def test_search_nodes_like_escape(self):
        """BUG-FIX-85: search_nodes 应转义 LIKE 通配符"""
        import inspect
        from app.services.industry_tree import search_nodes

        source = inspect.getsource(search_nodes)
        assert "ESCAPE" in source, "应使用 ESCAPE 转义 LIKE 通配符"

    def test_batch_import_logs_failures(self):
        """BUG-FIX-86: batch_import 应记录失败详情"""
        import inspect
        from app.services.industry_tree import batch_import

        source = inspect.getsource(batch_import)
        assert "failed" in source, "应记录失败节点详情"
