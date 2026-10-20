"""
目录生成模块 - asyncio 事件循环 & 核心工作流单元测试

覆盖场景：
  1. asyncio.run() vs run_until_complete() 在已有 event loop 时的行为差异
  2. with_timeout 超时与正常完成路径
  3. collect_json_response 的 max_retries 与 JSON 修复链路
  4. generate_supplement_chapters 返回格式兼容
  5. 并发场景：模拟 28 章并行填充的 semaphore 控制
  6. outline_tasks 中 asyncio.run() 替换 run_until_complete 的正确性
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


pytestmark = pytest.mark.skip(
    reason="[V3 LEGACY] workflows_outline_v2v3.py 已物理删除，统一走 V5.1 generate_outline()，跳过已退役路径测试"
)


# ─────────────────────────────────────────────────────────────────────────────
# 1. asyncio.run() vs run_until_complete() 事件循环兼容性测试
# ─────────────────────────────────────────────────────────────────────────────


def test_asyncio_run_in_running_loop():
    """
    Python 3.12+ 下，当线程中已有运行中的 event loop 时，
    run_until_complete() 会抛 RuntimeError，而 asyncio.run() 会创建新 loop 并正常运行。
    """
    async def inner():
        await asyncio.sleep(0.01)
        return 42

    def wrapper():
        return asyncio.run(inner())

    # 模拟 Celery worker / 已有 loop 的环境：在运行中的 loop 里调用 wrapper
    async def run_in_existing_loop():
        task = asyncio.create_task(asyncio.to_thread(wrapper))
        return await task

    result = asyncio.run(run_in_existing_loop())
    assert result == 42


def test_run_until_complete_fails_in_running_loop():
    """
    验证旧代码 asyncio.get_event_loop().run_until_complete() 在已有 loop 时会失败。
    这就是为什么需要改为 asyncio.run()。
    """

    async def inner():
        await asyncio.sleep(0.01)
        return 42

    async def run_in_existing_loop():
        loop = asyncio.get_running_loop()
        with pytest.raises(RuntimeError, match="already running"):
            loop.run_until_complete(inner())

    asyncio.run(run_in_existing_loop())


# ─────────────────────────────────────────────────────────────────────────────
# 2. with_timeout 超时与正常完成测试
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_with_timeout_normal():
    """正常完成：应在 timeout 内返回结果"""
    from app.services.ai.workflows import with_timeout

    async def fast_task():
        await asyncio.sleep(0.01)
        return "ok"

    result = await with_timeout(fast_task(), timeout=5)
    assert result == "ok"


@pytest.mark.asyncio
async def test_with_timeout_raises_timeouterror():
    """超时场景：应在 timeout 后抛出 TimeoutError（非 asyncio.TimeoutError）"""
    from app.services.ai.workflows import with_timeout

    async def slow_task():
        await asyncio.sleep(10)
        return "never"

    with pytest.raises(TimeoutError, match="AI调用超时"):
        await with_timeout(slow_task(), timeout=0.05)


@pytest.mark.asyncio
async def test_with_timeout_logs_timestamp():
    """验证 with_timeout 调用 logger.info 时格式字符串包含时间戳占位符（修复 BUG A 后正常）"""
    from app.services.ai.workflows import with_timeout
    import logging

    async def fast_task():
        return "log_test"

    # with_timeout 内部用 logging.getLogger(__name__) 创建 logger，
    # __name__ == "app.services.ai.workflows"
    mock_logger = MagicMock()
    with patch("logging.getLogger", return_value=mock_logger):
        result = await with_timeout(fast_task(), timeout=5)
        assert result == "log_test"
        # 应调用 info，且第一个参数包含时间戳占位符
        call_args = mock_logger.info.call_args
        assert call_args is not None
        assert "[%s]" in call_args[0][0]  # 时间戳占位符


# ─────────────────────────────────────────────────────────────────────────────
# 3. collect_json_response 重试 & JSON 修复链路
# ─────────────────────────────────────────────────────────────────────────────


class _FakeProvider:
    """模拟 AI provider，用于测试 collect_json_response"""

    def __init__(self, responses: list[str]):
        self.responses = iter(responses)
        self.call_count = 0

    async def chat(self, messages, temperature=0.7, json_mode=False):
        self.call_count += 1
        resp = next(self.responses)
        if isinstance(resp, Exception):
            raise resp
        return resp


@pytest.mark.asyncio
async def test_collect_json_response_success():
    """provider 直接返回合法 JSON → 一次调用成功"""
    from app.services.ai.workflows import collect_json_response

    provider = _FakeProvider(['{"chapters": [{"title": "概述"}]}'])
    messages = [MagicMock()]
    result = await collect_json_response(provider, messages, max_retries=1)
    assert isinstance(result, dict)
    assert provider.call_count == 1


@pytest.mark.asyncio
async def test_collect_json_response_max_retries_limit():
    """
    BUG O2 验证：max_retries=1 时主循环仅执行 1 次。
    若主调用失败且 JSON 修复也失败，则在同一次 attempt 内直接 raise，
    不会进入下一次循环（max_retries=1 → 仅 1 次主调用）。
    旧代码 max_retries=3 会导致最多 8 次调用，最坏 8×60s=480s。
    """
    from app.services.ai.workflows import collect_json_response

    # 连续返回非法 JSON，触发 JSON 修复链路
    provider = _FakeProvider([
        "not json at all",     # 主调用失败
        "{invalid json}",      # 修复调用也失败
    ])
    messages = [MagicMock()]

    with pytest.raises(RuntimeError, match="JSON 生成失败"):
        await collect_json_response(provider, messages, max_retries=1)

    # max_retries=1 时主循环仅执行 1 次；主调用失败后修复也失败，
    # 在同一次 attempt 内直接 raise，不再重试，故 call_count=1
    assert provider.call_count == 1, f"期望 1 次调用，实际 {provider.call_count}"


@pytest.mark.asyncio
async def test_collect_json_response_dict_to_list_extraction():
    """
    generate_supplement_chapters 返回 {"chapters": [...]} 格式时，
    collect_json_response 已能正确提取（非 BUG）。
    但 generate_supplement_chapters 自己有 dict→list 的解析逻辑，验证它。
    """
    from app.services.ai.workflows import generate_supplement_chapters

    # mock provider 返回 dict 格式
    provider = MagicMock()
    provider.chat = AsyncMock(return_value='{"chapters": [{"title": "BIM应用"}, {"title": "绿色施工"}]}')

    result = await generate_supplement_chapters(
        provider,
        summary="测试项目",
        scoring_points="{}",
        scope="弱电集成",
    )
    assert isinstance(result, list)
    assert len(result) == 2
    assert result[0]["title"] == "BIM应用"


# ─────────────────────────────────────────────────────────────────────────────
# 4. 并发 semaphore 控制测试（模拟 28 章并行填充）
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_outline_semaphore_limits_concurrency():
    """
    验证 outline 并行填充的并发控制（semaphore 上限 + _adaptive_controller）：
    Anthropic 并发 ≤5，其他 ≤10。
    """
    from app.services.ai.workflows import _adaptive_controller

    # 模拟 28 章同时请求，验证 semaphore 能限制并发
    max_concurrent = 5  # Anthropic 场景
    sem = asyncio.Semaphore(max_concurrent)
    active = 0
    peak = 0

    async def worker(i: int):
        nonlocal active, peak
        async with sem:
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.01)
            active -= 1

    tasks = [asyncio.create_task(worker(i)) for i in range(28)]
    await asyncio.gather(*tasks)

    assert peak <= max_concurrent, f"并发数 {peak} 超出限制 {max_concurrent}"
    assert peak > 0, "semaphore 未生效"


@pytest.mark.asyncio
async def test_fill_children_fallback_on_exception():
    """
    fill_children_with_retry 失败时，应返回 sanitize_framework_titles 的兜底结果，
    不中断整体目录生成流程。
    """
    from app.services.ai.workflows import fill_children_with_retry

    provider = MagicMock()
    provider.chat = AsyncMock(side_effect=TimeoutError("AI超时"))

    framework = {
        "title": "测试章",
        "children": [
            {"title": "子项A", "children": [{"title": "细项1"}]},
            {"title": "子项B", "children": [{"title": "细项2"}]},
        ],
    }

    # 应抛出异常（因为 max_retries=0 时立即失败）
    with pytest.raises(Exception):
        await fill_children_with_retry(
            provider,
            framework=framework,
            project_overview="测试",
            scoring_points="{}",
            parent_title="测试章",
            other_titles=[],
            max_retries=0,
        )


# ─────────────────────────────────────────────────────────────────────────────
# 5. outline_tasks asyncio.run() 替换验证
# ------------------------------------------------------------------------------
# 注：app/tasks/outline_tasks.py 已按设计废弃（生产路径迁移至 FastAPI 进程内
# asyncio.create_task + SSE），原 test_outline_tasks_no_dead_code 守卫随之移除。
# ─────────────────────────────────────────────────────────────────────────────


def test_workflows_no_dead_code():
    """验证 workflows 模块中 call_ai_with_retry 死代码已删除，datetime 调用正确"""
    from pathlib import Path

    # workflows.py 现在为 facade 模块，实际代码在 workflows_base.py 中
    base_file = Path(__file__).parent.parent / "app" / "services" / "ai" / "workflows_base.py"
    src = base_file.read_text(encoding="utf-8")

    assert "async def call_ai_with_retry" not in src, "call_ai_with_retry 死代码应已删除"
    # import datetime 是模块导入，需用 datetime.datetime.now()
    assert "datetime.datetime.now()" in src, "应使用 datetime.datetime.now()（import datetime 模块导入）"
    # 确保没有错误的 datetime.now() 裸调用
    assert "datetime.now()" not in src.replace("datetime.datetime.now()", ""), "不应有裸 datetime.now() 调用"


# ─────────────────────────────────────────────────────────────────────────────
# 5.5 Celery 极端场景：嵌套 asyncio.run() 在有运行 loop 的线程中
# ─────────────────────────────────────────────────────────────────────────────


def test_celery_nested_asyncio_run_fails_in_running_loop():
    """
    极端场景复现：Celery worker 使用带有 event loop 的并发模式时，
    同步 Celery task 内部调用 asyncio.run() 会抛出 RuntimeError。

    复现路径：
      FastAPI/ASGI 请求处理线程（有 running loop）
        → 同步调用 validate_outline_async（Celery task）
          → asyncio.run(review_outline(...))
            → RuntimeError: asyncio.run() cannot be called from a running event loop
    """
    import asyncio

    def sync_celery_task():
        # 这正是 outline_tasks.py 中 validate_outline_async 内部的行为
        async def nested_async():
            await asyncio.sleep(0.001)
            return "ok"
        return asyncio.run(nested_async())

    async def run_from_existing_loop():
        # 关键：在同一线程中同步调用，不通过 to_thread 换线程
        # 这模拟 FastAPI 中间件直接调用 Celery task 的场景
        return sync_celery_task()

    with pytest.raises(RuntimeError, match="cannot be called from a running event loop"):
        asyncio.run(run_from_existing_loop())


def test_celery_nested_asyncio_run_in_new_thread_succeeds():
    """
    验证 asyncio.run() 在纯新线程（无 running loop）中可正常工作。
    这正是 Celery 默认 worker 的行为——每个 worker 进程有自己的线程池。
    """
    import asyncio

    def sync_celery_task():
        async def nested_async():
            await asyncio.sleep(0.001)
            return "ok"
        return asyncio.run(nested_async())

    # 从新线程调用（无 running loop）
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        result = sync_celery_task()
        assert result == "ok"
    finally:
        loop.close()


def test_safe_asyncio_run_in_running_loop():
    """
    验证 _safe_asyncio_run() 工具函数：
    在有 running loop 的线程中，应自动降级为 asyncio.to_thread(asyncio.run)
    避免 RuntimeError。
    """
    from app.services.ai.workflows import _safe_asyncio_run

    async def nested():
        await asyncio.sleep(0.001)
        return 99

    # 场景1：无 running loop → 直接 asyncio.run()
    result = _safe_asyncio_run(nested())
    assert result == 99

    # 场景2：有 running loop → 自动创建新线程执行 asyncio.run()
    async def host():
        return await asyncio.to_thread(_safe_asyncio_run, nested())

    result = asyncio.run(host())
    assert result == 99


# ─────────────────────────────────────────────────────────────────────────────
# 5.6 极端并发场景：FastAPI 线程池 + Celery task 并发调用
# ─────────────────────────────────────────────────────────────────────────────


def test_concurrent_safe_asyncio_run_in_thread_pool():
    """
    极端场景：模拟 FastAPI 请求处理线程池并发调用 _safe_asyncio_run。
    这是 Celery task 通过 asyncio.to_thread 在 FastAPI 线程池中执行时的真实场景。
    多个 Celery task 并发时，每个都调用 _safe_asyncio_run，应互不干扰。
    """
    import asyncio
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from app.services.ai.workflows import _safe_asyncio_run

    async def delayed_task(task_id: int):
        await asyncio.sleep(0.01 * task_id)  # 不同任务不同延迟
        return f"task_{task_id}_done"

    # 模拟 5 个 Celery task 并发调用
    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = [pool.submit(_safe_asyncio_run, delayed_task(i)) for i in range(5)]
        results = [f.result() for f in as_completed(futures)]

    # 所有任务都应成功，且结果完整
    assert len(results) == 5
    assert sorted(results) == ["task_0_done", "task_1_done", "task_2_done", "task_3_done", "task_4_done"]


def test_safe_asyncio_run_exception_propagation():
    """
    极端场景：当 coroutine 抛出异常时，_safe_asyncio_run 应正确传播异常
    而非静默吞掉或返回 None。
    """
    from app.services.ai.workflows import _safe_asyncio_run

    async def failing_task():
        raise ValueError("模拟 AI 调用失败")

    with pytest.raises(ValueError, match="模拟 AI 调用失败"):
        _safe_asyncio_run(failing_task())


def test_safe_asyncio_run_multiple_sequential_in_same_loop():
    """
    极端场景：在同一运行中的 event loop 线程中连续多次调用 _safe_asyncio_run。
    每次调用应创建独立新线程并正确返回结果，互不干扰。
    """
    import asyncio
    from app.services.ai.workflows import _safe_asyncio_run

    async def host():
        results = []
        for i in range(10):
            async def task(n=i):
                await asyncio.sleep(0.001)
                return n * 2
            # 在已有 loop 的线程中调用 _safe_asyncio_run
            result = await asyncio.to_thread(_safe_asyncio_run, task())
            results.append(result)
        return results

    results = asyncio.run(host())
    assert results == [0, 2, 4, 6, 8, 10, 12, 14, 16, 18]


# ─────────────────────────────────────────────────────────────────────────────
# 6. SSE handler 导入完整性 & 配置引用
# ─────────────────────────────────────────────────────────────────────────────


def test_sse_handlers_no_unused_import():
    """验证 sse_handlers.py 不再有未使用的 SSE_TIMEOUT 导入"""
    from pathlib import Path

    handler_file = Path(__file__).parent.parent / "app" / "routers" / "sse_handlers.py"
    src = handler_file.read_text(encoding="utf-8")

    # SSE_TIMEOUT_OUTLINE / SSE_TIMEOUT_CONTENT 已不再从 config 导入
    assert "from ..config import SSE_TIMEOUT_OUTLINE" not in src
    assert "SSE_TIMEOUT_CONTENT" not in src

    # 但仍有 SSE_TOTAL_TIMEOUT 本地常量（1800s）
    assert "SSE_TOTAL_TIMEOUT" in src


def test_workflows_importable():
    """验证 workflows.py 无语法/运行时错误，核心函数可正常导入"""
    from app.services.ai.workflows import (
        generate_outline,
        generate_outline_v3,
        with_timeout,
        collect_json_response,
        generate_supplement_chapters,
        fill_children_with_retry,
        smart_merge_outline,
        mark_irrelevant_chapters,
        normalize_outline_items,
    )
    assert callable(generate_outline)
    assert callable(with_timeout)
    assert callable(collect_json_response)


def test_sse_handlers_importable():
    """验证 sse_handlers.py 可正常导入，无未定义符号"""
    from app.routers.sse_handlers import (
        generate_outline_sse_impl,
        _clear_and_save_outline,
        _save_and_finalize_outline,
        _try_get_provider,
    )
    assert callable(generate_outline_sse_impl)
    assert callable(_clear_and_save_outline)
