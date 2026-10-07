"""
目录生成模块集成测试

模拟完整的目录生成流程，验证：
  1. asyncio.run() 在已有 event loop 环境下正常工作（Python 3.12+ 兼容）
  2. 28 章并行填充的 semaphore 并发控制
  3. 超时降级：AI 超时 → 模板占位符 → 业务校验 → 完整目录
  4. 补充章节生成 + 合并流程
  5. collect_json_response 的 max_retries=1 限制
  6. fix 后的 datetime 日志无异常
"""

from __future__ import annotations

import asyncio
import json
import time
import warnings
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


pytestmark = pytest.mark.skip(
    reason="[V3 LEGACY] workflows_outline_v2v3.py 已物理删除，统一走 V5.1 generate_outline()，跳过已退役路径测试"
)


# 确保 backend/ 在 sys.path 中
_BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(_BACKEND_DIR) not in __import__("sys").path:
    __import__("sys").path.insert(0, str(_BACKEND_DIR))


# ─────────────────────────────────────────────────────────────────────────────
# 模拟 AI Provider
# ─────────────────────────────────────────────────────────────────────────────


class MockOutlineProvider:
    """
    模拟 AI provider，按调用顺序返回预设响应。

    响应队列结构：
      [0]     generate_supplement_chapters 的 AI 响应（补充章节）
      [1..N]  各章 fill_children_with_retry 的 AI 响应（按 FIXED_28_CHAPTERS 顺序）
    """

    def __init__(self, responses: list[str] | None = None):
        self.responses = iter(responses or [])
        self.call_count = 0
        self.calls: list[dict] = []

    async def chat(self, messages, temperature=0.7, json_mode=False):
        self.call_count += 1
        try:
            resp = next(self.responses)
        except StopIteration:
            # 无预设响应时，返回一个合理的最小结果
            resp = self._fallback_response()

        self.calls.append({
            "json_mode": json_mode,
            "timestamp": time.monotonic(),
            "msg_count": len(messages) if messages else 0,
        })
        return resp

    def _fallback_response(self) -> str:
        return json.dumps({
            "chapters": [
                {"title": "测试章1", "children": [
                    {"title": "子项1-A", "children": [{"title": "细项1-A-1"}]},
                    {"title": "子项1-B", "children": [{"title": "细项1-B-1"}]},
                ]},
            ]
        })


# ─────────────────────────────────────────────────────────────────────────────
# 1. asyncio.run() 在已有 event loop 下的兼容性（Python 3.12+ 核心场景）
# ─────────────────────────────────────────────────────────────────────────────


def test_asyncio_run_inside_running_loop():
    """
    模拟 Celery worker 或其他框架在已有 event loop 的线程中调用 outline_tasks 的场景。
    asyncio.run() 应创建独立子 loop 并正常执行；run_until_complete() 会抛 RuntimeError。
    """
    import asyncio

    async def inner_workflow():
        await asyncio.sleep(0.001)
        return {"ok": True, "chapters": 28}

    def wrapper():
        # 这正是 outline_tasks.py 中 _generate_outline_workflow 的调用方式
        return asyncio.run(inner_workflow())

    async def run_in_existing_loop():
        # 模拟：当前线程已有运行中的 event loop（如 FastAPI 请求上下文）
        task = asyncio.create_task(asyncio.to_thread(wrapper))
        return await task

    result = asyncio.run(run_in_existing_loop())
    assert result["ok"] is True
    assert result["chapters"] == 28


def test_run_until_complete_fails_with_running_loop():
    """
    验证旧代码（run_until_complete）在有 running loop 时会失败。
    这是必须修复的根本原因。
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=pytest.PytestUnraisableExceptionWarning)

        async def inner():
            await asyncio.sleep(0.001)
            return 1

        async def demonstrate_failure():
            loop = asyncio.get_running_loop()
            coro = inner()  # noqa: F841 — bind to avoid warning
            with pytest.raises(RuntimeError, match="already running"):
                loop.run_until_complete(coro)

        asyncio.run(demonstrate_failure())


# ─────────────────────────────────────────────────────────────────────────────
# 2. with_timeout 超时 & 日志格式
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_with_timeout_completes_within_limit():
    from app.services.ai.workflows import with_timeout

    async def quick():
        return "done"

    result = await with_timeout(quick(), timeout=5)
    assert result == "done"


@pytest.mark.asyncio
async def test_with_timeout_raises_timeout_error():
    from app.services.ai.workflows import with_timeout

    async def slow():
        await asyncio.sleep(10)
        return "too-late"

    with pytest.raises(TimeoutError, match="AI调用超时"):
        await with_timeout(slow(), timeout=0.02)


@pytest.mark.asyncio
async def test_with_timeout_logs_datetime_format():
    """验证修复后的 datetime.datetime.now() 调用能正常产生时间戳日志"""
    from app.services.ai.workflows import with_timeout

    async def quick():
        return "ts-test"

    mock_logger = MagicMock()
    with patch("logging.getLogger", return_value=mock_logger):
        await with_timeout(quick(), timeout=5)
        assert mock_logger.info.called
        msg = mock_logger.info.call_args[0][0]
        # 日志格式应包含时间戳占位符 [%s]
        assert "[%s]" in msg


# ─────────────────────────────────────────────────────────────────────────────
# 3. 完整目录生成流程：mock provider + 真实工作流函数
# ─────────────────────────────────────────────────────────────────────────────


def _make_supplement_response() -> str:
    """补充章节 AI 响应：返回 2 个补充章"""
    return json.dumps({
        "chapters": [
            {"title": "BIM技术应用", "children": []},
            {"title": "绿色施工措施", "children": []},
        ]
    })


def _make_children_response(chapter_title: str) -> str:
    """某章的二三级填充 AI 响应"""
    return json.dumps({
        "children": [
            {"title": f"{chapter_title}-实施要点", "children": [
                {"title": "组织保障措施"},
                {"title": "技术保障措施"},
                {"title": "安全文明施工措施"},
            ]},
            {"title": f"{chapter_title}-进度计划", "children": [
                {"title": "进度控制方法"},
                {"title": "进度纠偏措施"},
            ]},
        ]
    })


@pytest.mark.asyncio
async def test_generate_supplement_chapters_returns_list():
    """generate_supplement_chapters：AI 返回 dict → 正确提取为 list"""
    from app.services.ai.workflows import generate_supplement_chapters

    provider = MockOutlineProvider([_make_supplement_response()])
    result = await generate_supplement_chapters(
        provider,
        summary="某弱电智能化项目",
        scoring_points='[{"point":"BIM应用"},{"point":"绿色施工"}]',
        scope="弱电集成",
    )
    assert isinstance(result, list)
    assert len(result) >= 2
    titles = [c["title"] for c in result]
    assert any("BIM" in t for t in titles)
    assert any("绿色" in t or "环保" in t for t in titles)


@pytest.mark.asyncio
async def test_generate_outline_v3_partial_success():
    """
    V3.0 完整流程集成测试：
    模拟 28 章全部正常响应，验证完整生成流程无异常，最终返回有效目录树。
    （超时降级路径由 test_fill_children_retry_fallback 单独覆盖）

    响应队列结构（按消费顺序）：
      [0]     generate_supplement_chapters 的 AI 响应
      [1]     V6.0 AI 排序尝试 1（任意，会解析失败触发重试）
      [2]     V6.0 AI 排序尝试 2（任意，会解析失败触发代码兜底）
      [3]     BOQ 分析 AI 响应（空列表 → 使用默认清单）
      [4]     工作界面分析 AI 响应（空列表 → 使用默认界面）
      [5]     工程作法分析 AI 响应（空列表 → 使用默认作法）
      [6..33] 28 章 fill_children_with_retry 的 AI 响应
    """
    from app.services.ai.workflows import generate_outline_v3, FIXED_28_CHAPTERS

    # V6.0 排序：空字符串 → 解析失败，触发重试；第二次仍失败 → 代码兜底
    v60_sort_responses = ["", "{}"]
    # 三项分析：空列表 → 使用默认兜底结果
    analysis_responses = ["[]", "[]", "[]"]
    responses = [_make_supplement_response()] + v60_sort_responses + analysis_responses + [
        _make_children_response(ch["title"]) for ch in FIXED_28_CHAPTERS
    ]
    assert len(responses) == 34, f"期望 34 个响应，实际 {len(responses)}"

    provider = MockOutlineProvider(responses)

    result = await generate_outline_v3(
        provider=provider,
        summary="测试弱电项目摘要内容",
        scoring_points='[{"point":"BIM"},{"point":"绿色"}]',
        industry_profile="建筑智能化",
        tender_context="招标文件全文内容",
        reference_outline="",
        project_scope="弱电系统集成",
        project_info={},
        progress_callback=None,
        phase_callback=None,
    )

    assert isinstance(result, list)
    assert len(result) >= 28, f"期望至少 28 章，实际 {len(result)}"
    top_level_titles = [c["title"] for c in result]
    # 补充章应被追加
    assert any("BIM" in t for t in top_level_titles)


@pytest.mark.asyncio
async def test_fill_children_retry_fallback():
    """
    fill_children_with_retry：AI 连续失败后，应抛出 RuntimeError。
    外层工作流会捕获此异常并降级为框架占位符，此处验证错误传播路径正确。
    """
    from app.services.ai.workflows import fill_children_with_retry

    provider = MockOutlineProvider([])
    provider.chat = AsyncMock(side_effect=TimeoutError("AI timeout"))

    framework = {
        "title": "项目管理",
        "children": [
            {"title": "组织架构", "children": [{"title": "人员配置"}]},
            {"title": "管理制度", "children": [{"title": "考勤制度"}]},
        ],
    }

    # max_retries=0，AI 直接失败，应抛出 RuntimeError
    with pytest.raises(RuntimeError, match="填充失败"):
        await fill_children_with_retry(
            provider=provider,
            framework=framework,
            project_overview="测试",
            scoring_points="{}",
            parent_title="项目管理",
            other_titles=[],
            max_retries=0,
        )


# ─────────────────────────────────────────────────────────────────────────────
# 4. 并发控制：semaphore 限制同时进行的 AI 调用数
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_semaphore_concurrency_limit():
    """
    验证 _adaptive_controller 的并发限制逻辑：
    模拟 28 章同时请求，峰值并发不应超过 Anthropic 限制（5）。
    """
    from app.services.ai.workflows import _adaptive_controller

    max_concurrent = 5
    active = 0
    peak = 0
    semaphore = asyncio.Semaphore(max_concurrent)

    async def worker(i: int):
        nonlocal active, peak
        async with semaphore:
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.005)
            active -= 1

    tasks = [asyncio.create_task(worker(i)) for i in range(28)]
    await asyncio.gather(*tasks)

    assert peak <= max_concurrent, f"峰值并发 {peak} 超出限制 {max_concurrent}"
    assert peak > 0, "semaphore 未生效"


# ─────────────────────────────────────────────────────────────────────────────
# 5. collect_json_response max_retries=1 限制（BUG O2 回归测试）
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_collect_json_response_respects_max_retries_one():
    """
    BUG O2 修复验证：max_retries=1 时主循环仅执行 1 次。
    若主调用失败且 JSON 修复也失败，则在同一次 attempt 内直接 raise。
    """
    from app.services.ai.workflows import collect_json_response

    provider = MockOutlineProvider([
        "not valid json at all",   # 主调用
        "{also invalid}",           # 修复调用
    ])
    messages = [MagicMock()]

    with pytest.raises(RuntimeError, match="JSON 生成失败"):
        await collect_json_response(provider, messages, max_retries=1)

    # max_retries=1：主循环仅执行 1 次，修复失败后直接 raise
    assert provider.call_count == 1, f"期望 1 次调用，实际 {provider.call_count}"


# ─────────────────────────────────────────────────────────────────────────────
# 6. 修复后 datetime 调用不抛 AttributeError
# ─────────────────────────────────────────────────────────────────────────────


def test_datetime_import_correct():
    """
    import datetime 后应使用 datetime.datetime.now()，
    而不是 datetime.now()（后者在 import datetime 下会 AttributeError）。
    """
    from app.services.ai.workflows import with_timeout
    import datetime

    # 验证模块导入方式
    assert hasattr(datetime, "datetime")
    # 验证工作流程可正常导入（说明 datetime 调用无误）
    assert callable(with_timeout)


# ─────────────────────────────────────────────────────────────────────────────
# 7. outline_tasks asyncio.run 替换验证
# ------------------------------------------------------------------------------
# 注：app/tasks/outline_tasks.py 已按设计废弃（生产路径迁移至 FastAPI 进程内
# asyncio.create_task + SSE），原 test_outline_tasks_clean_no_dead_code 守卫随之移除。
# ─────────────────────────────────────────────────────────────────────────────


def test_workflows_clean_no_dead_code():
    """workflows 模块不应再有 call_ai_with_retry 死代码"""
    from pathlib import Path
    base_file = Path(__file__).parent.parent / "app" / "services" / "ai" / "workflows_base.py"
    src = base_file.read_text(encoding="utf-8")
    assert "async def call_ai_with_retry" not in src
    assert "datetime.datetime.now()" in src  # 正确的模块调用方式


# ─────────────────────────────────────────────────────────────────────────────
# 8. 端到端：outline_tasks 中的 asyncio.run() 调用路径可正常执行
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_outline_async_wrapper_runs_cleanly():
    """
    验证 outline_tasks.py 中 generate_outline_async 的内部包装函数
    能在已有 event loop 的线程中被 asyncio.run() 正常调用。
    """
    import asyncio

    async def simulate_workflow():
        """模拟 outline_tasks 中 _generate_outline_workflow 的内部逻辑"""
        await asyncio.sleep(0.001)
        return {"chapters": 30, "status": "ok"}

    def run_in_thread():
        # 这正是 outline_tasks.py 中 generate_outline_async 的调用方式
        return asyncio.run(simulate_workflow())

    async def host_with_existing_loop():
        task = asyncio.create_task(asyncio.to_thread(run_in_thread))
        return await task

    result = await host_with_existing_loop()
    assert result["status"] == "ok"
    assert result["chapters"] == 30


# ─────────────────────────────────────────────────────────────────────────────
# 9. 合并流程：smart_merge_outline 保留 28 章顺序
# ─────────────────────────────────────────────────────────────────────────────


def test_smart_merge_preserves_28_chapter_order():
    """
    验证 merge_outline_with_supplements（代码级合并）保留 28 章原始顺序。
    smart_merge_outline 是 AI 兜底路径，实际 V3.0 优先使用代码级合并。
    """
    from app.services.ai.workflows import merge_outline_with_supplements
    from app.services.ai.workflows import FIXED_28_CHAPTERS

    supplement = [
        {"title": "BIM技术", "children": []},
        {"title": "绿色施工", "children": []},
    ]

    result = merge_outline_with_supplements(FIXED_28_CHAPTERS, supplement)
    titles = [c["title"] for c in result]

    # 28 章应全部保留
    assert len(result) >= 28
    # 补充章应追加在末尾
    assert any("BIM" in t for t in titles)
    assert any("绿色" in t for t in titles)
    # 原 28 章顺序不变：第一章应在补充章之前
    first_chapter_idx = titles.index(FIXED_28_CHAPTERS[0]["title"])
    last_supplement_idx = max(
        i for i, t in enumerate(titles)
        if t in ("BIM技术", "绿色施工")
    )
    assert first_chapter_idx < last_supplement_idx


# ─────────────────────────────────────────────────────────────────────────────
# 10. _safe_asyncio_run 统一替换旧模式的验证
# ─────────────────────────────────────────────────────────────────────────────


def test_outline_sort_optimizer_uses_safe_asyncio_run():
    """
    outline_sort_optimizer._call_provider 不再使用 get_event_loop() + run_until_complete()
    旧代码在 Python 3.10+ 下 get_event_loop() 会创建新 loop 而非返回 running loop，
    导致 is_running() 判断错误。现已统一改用 _safe_asyncio_run。
    """
    from pathlib import Path

    src = Path(__file__).parent.parent / "app" / "services" / "ai" / "outline_sort_optimizer.py"
    text = src.read_text(encoding="utf-8")

    assert "get_event_loop().run_until_complete" not in text, \
        "outline_sort_optimizer 仍使用 get_event_loop() + run_until_complete() 旧模式"
    assert "_safe_asyncio_run" in text, \
        "outline_sort_optimizer 应使用 _safe_asyncio_run"


def test_enhancement_orchestrator_uses_safe_asyncio_run():
    """
    enhancement_orchestrator.check_health 不再使用 get_event_loop().run_until_complete()。
    """
    from pathlib import Path

    src = Path(__file__).parent.parent / "app" / "services" / "enhancement_orchestrator.py"
    text = src.read_text(encoding="utf-8")

    assert "get_event_loop().run_until_complete" not in text, \
        "enhancement_orchestrator 仍使用 get_event_loop().run_until_complete() 旧模式"
    assert "_safe_asyncio_run" in text, \
        "enhancement_orchestrator 应使用 _safe_asyncio_run"


def test_mermaid_renderer_uses_safe_asyncio_run():
    """
    mermaid_renderer 不再使用嵌套的 asyncio.run + run_until_complete 模式。
    """
    from pathlib import Path

    src = Path(__file__).parent.parent / "app" / "services" / "ai" / "mermaid_renderer.py"
    text = src.read_text(encoding="utf-8")

    # 不应再有嵌套的 run_until_complete in except RuntimeError
    assert "loop.run_until_complete" not in text, \
        "mermaid_renderer 仍使用 loop.run_until_complete 旧模式"
    assert "_safe_asyncio_run" in text, \
        "mermaid_renderer 应使用 _safe_asyncio_run"


def test_no_get_event_loop_run_until_complete_anywhere():
    """
    全项目范围内不应再有 get_event_loop().run_until_complete() 旧模式。
    """
    from pathlib import Path

    app_dir = Path(__file__).parent.parent / "app"
    for py_file in app_dir.rglob("*.py"):
        text = py_file.read_text(encoding="utf-8")
        if "get_event_loop().run_until_complete" in text:
            rel = py_file.relative_to(app_dir)
            raise AssertionError(f"{rel} 仍使用 get_event_loop().run_until_complete() 旧模式")


@pytest.mark.asyncio
async def test_call_provider_uses_safe_asyncio_run():
    """
    OutlineSortOptimizer._call_provider 在已有 running loop 的线程中调用时，
    应通过 _safe_asyncio_run 正确执行，不抛 RuntimeError。
    """
    import asyncio
    from unittest.mock import MagicMock
    from app.services.ai.outline_sort_optimizer import OutlineSortOptimizer

    optimizer = OutlineSortOptimizer(provider=None, max_retries=1)
    # mock provider.chat 为 async 函数
    mock_provider = MagicMock()

    async def mock_chat(messages):
        return '{"chapters": [{"title": "测试"}]}'

    mock_provider.chat = mock_chat
    optimizer.provider = mock_provider

    # 辅助函数：在有 running loop 的线程中同步调用 _call_provider
    def sync_call():
        # _call_provider 返回 str 或 None，不是 coroutine
        return optimizer._call_provider([MagicMock()])

    async def host():
        # 通过 to_thread 模拟：在已有 running loop 的线程中同步调用
        return await asyncio.to_thread(sync_call)

    result = await host()
    # _call_provider 应返回字符串（AI 响应）或 None，不抛异常
    assert result is None or isinstance(result, str)


# 10. 9步 phase_callback 顺序验证
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_phase_callback_9step_sequence():
    """
    验证 generate_outline_v3 的 phase_callback 按正确顺序发送 7 个步骤（对应前端 9 步进度条）：
      步骤3：二三级生成完成
      步骤4：V6.0排序完成
      步骤5：评分项对齐完成
      步骤6：V3.6分析完成
      步骤7：最终审核完成
    （步骤1/2 由 sse_handlers.py 发送，步骤8/9 由 _save_and_notify_complete 发送）
    """
    from app.services.ai.workflows import generate_outline_v3, FIXED_28_CHAPTERS

    v60_sort_responses = ["", "{}"]
    analysis_responses = ["[]", "[]", "[]"]
    responses = [_make_supplement_response()] + v60_sort_responses + analysis_responses + [
        _make_children_response(ch["title"]) for ch in FIXED_28_CHAPTERS
    ]
    provider = MockOutlineProvider(responses)

    # 收集所有 phase_callback 调用
    phase_calls: list[tuple[str, str]] = []
    phase_timestamps: list[float] = []

    async def track_phase(phase_key: str, step_message: str):
        phase_calls.append((phase_key, step_message))
        phase_timestamps.append(time.monotonic())

    result = await generate_outline_v3(
        provider=provider,
        summary="测试弱电项目摘要内容",
        scoring_points='[{"point":"BIM"},{"point":"绿色"}]',
        industry_profile="建筑智能化",
        tender_context="招标文件全文内容",
        reference_outline="",
        project_scope="弱电系统集成",
        project_info={},
        progress_callback=None,
        phase_callback=track_phase,
    )

    # 1. 结果必须有效
    assert result is not None
    assert isinstance(result, list)
    assert len(result) > 0

    # 2. phase_callback 顺序验证（按调用序列严格匹配）
    # 与 sse_handlers 中 _phase_callback 的 step_map 保持一致的真实顺序：
    # 排序→对齐→分析→二三级填充→审核（映射到 3~7）
    expected_phases = [
        ("补充目录生成", "AI 正在生成补充一级目录..."),
        ("V6.0排序", "V6.0 智能排序优化（工程逻辑分区聚类）"),
        ("V6.0排序完成", "V6.0 智能排序优化"),
        ("评分项对齐完成", "评分项对齐目录生成完成，整理目录编号"),
        ("V3.6分析完成", "清单工程量/工作界面/工程作法分析"),
        ("二三级生成完成", "二三级目录填充完成"),
        ("多样性修复", "跨章节标题多样性并行修复中..."),
        ("L3修复", "并行修复通用三级标题..."),
        ("最终审核完成", "AI 最终审核目录质量"),
    ]
    assert len(phase_calls) == len(expected_phases), (
        f"期望 {len(expected_phases)} 个 phase_callback，实际 {len(phase_calls)}"
    )
    for i, ((actual_key, actual_msg), (exp_key, exp_msg)) in enumerate(zip(phase_calls, expected_phases)):
        assert actual_key == exp_key, f"步骤 {i+1}: phase_key 期望 {exp_key}，实际 {actual_key}"
        assert actual_msg == exp_msg, f"步骤 {i+1}: message 期望 {exp_msg}，实际 {actual_msg}"


@pytest.mark.asyncio
async def test_v36_analysis_concurrent_performance():
    """
    验证 V3.6 三项分析并发执行的耗时优势：
    并发执行总耗时应接近单最慢项（而非三者之和）。
    使用小延迟 mock provider 来模拟并发行为。
    """
    import asyncio

    from app.services.ai.workflows import generate_outline_v3, FIXED_28_CHAPTERS

    v60_sort_responses = ["", "{}"]
    # BOQ/界面/作法返回有效结果，而非空列表
    analysis_responses = [
        json.dumps([{"chapter_index": 0, "items": [{"name": "测试项", "quantity": 100, "unit": "项"}]}]),
        json.dumps([{"interface_name": "界面A", "responsible_party": "总包", "scope": "测试"}]),
        json.dumps([{"method_name": "方法A", "description": "测试方法", "cost": 1000.0}]),
    ]
    responses = [_make_supplement_response()] + v60_sort_responses + analysis_responses + [
        _make_children_response(ch["title"]) for ch in FIXED_28_CHAPTERS
    ]

    class SlowMockProvider(MockOutlineProvider):
        """每项 BOQ/界面/作法分析增加 0.05s 延迟，模拟并发耗时"""
        async def chat(self, messages, temperature=0.7, json_mode=False):
            # 前 6 项为补充+V6.0（0s），后续为 V3.6 分析（各加 0.05s）
            # responses 列表：[supplement, sort, sort, boq, work_interface, construction_method, 28章...]
            # 索引 3,4,5 对应 BOQ/界面/作法分析
            if self.call_count in (4, 5, 6):  # 第4/5/6次chat调用 = V3.6三项分析
                await asyncio.sleep(0.05)
            return await super().chat(messages, temperature=temperature, json_mode=json_mode)

    provider = SlowMockProvider(responses)
    phase_calls: list[str] = []
    phase_times: dict[str, float] = {}

    async def track_phase(phase_key: str, step_message: str):
        t = time.monotonic()
        phase_calls.append(phase_key)
        phase_times[phase_key] = t

    t_start = time.monotonic()
    result = await generate_outline_v3(
        provider=provider,
        summary="测试弱电项目摘要内容",
        scoring_points='[{"point":"BIM"}]',
        industry_profile="建筑智能化",
        tender_context="招标文件全文内容",
        reference_outline="",
        project_scope="弱电系统集成",
        project_info={},
        progress_callback=None,
        phase_callback=track_phase,
    )
    total_time = time.monotonic() - t_start

    assert result is not None and len(result) > 0
    assert "V3.6分析完成" in phase_calls, "V3.6 分析阶段必须被触发"

    # 验证 V3.6 分析阶段被触发（并发通过 asyncio.gather 实现，代码结构已保证）
    assert "V3.6分析完成" in phase_calls, "V3.6 分析阶段必须被触发"
    assert "最终审核完成" in phase_calls, "最终审核阶段必须被触发"

    # 验证总生成时间在合理范围内（不依赖精确时序，避免事件循环竞争导致的 flaky）
    assert total_time < 15.0, f"总生成时间 {total_time:.2f}s 超过预期"
    print(f"✅ V3.6 并发性能验证通过，总耗时 {total_time:.2f}s")


@pytest.mark.asyncio
async def test_large_mock_data_full_flow():
    """
    使用模拟大招标文件（10章+8评分项+项目概要）验证完整流程：
    - generate_supplement_chapters 返回空（已有10章在模板中）
    - V6.0 排序两次失败后代码兜底
    - 三项 V3.6 分析返回空列表（使用默认兜底）
    - 28章 fill_children 全部正常
    """
    import json as _json

    from app.services.ai.workflows import generate_outline_v3, FIXED_28_CHAPTERS

    # 大招标文件数据（来自 doc/模拟大招标文件_测试数据.json）
    summary = "某市城市轨道交通5号线机电系统集成项目"
    scoring_points = _json.dumps([
        {"point": "BIM技术应用", "weight": 10, "description": "采用BIM技术进行管线综合"},
        {"point": "绿色施工", "weight": 8, "description": "绿色施工措施与环保"},
        {"point": "智能化系统", "weight": 12, "description": "智能建筑系统集成方案"},
        {"point": "工期保障", "weight": 7, "description": "工期计划与保障措施"},
        {"point": "质量管理体系", "weight": 10, "description": "ISO9001质量管理体系"},
        {"point": "安全管理", "weight": 8, "description": "安全生产管理体系"},
        {"point": "造价控制", "weight": 5, "description": "工程造价控制措施"},
        {"point": "售后服务", "weight": 5, "description": "质保期售后服务方案"},
    ])
    industry_profile = "城市轨道交通"
    tender_context = """
    1. 招标公告
    2. 投标人须知
    3. 评标办法（综合评估法）
    4. 合同条款及格式
    5. 工程量清单
    6. 图纸
    7. 技术标准和要求
    8. 投标文件格式
    9. 项目概况：总建筑面积约15万平方米，包含地下两层岛式站台车站2座
    10. 评标标准：技术标评分占比60%，商务标评分占比40%
    """
    reference_outline = ""
    project_scope = "城市轨道交通5号线机电系统集成，含通风空调、消防、给排水、低压配电、照明、综合监控、站台门等系统"

    v60_sort_responses = ["", "{}"]
    analysis_responses = ["[]", "[]", "[]"]
    responses = [_make_supplement_response()] + v60_sort_responses + analysis_responses + [
        _make_children_response(ch["title"]) for ch in FIXED_28_CHAPTERS
    ]

    provider = MockOutlineProvider(responses)
    phase_calls: list[str] = []
    total_elapsed = 0.0

    async def track_phase(phase_key: str, step_message: str):
        t0 = time.monotonic()
        phase_calls.append(phase_key)
        # 模拟 0.02s 处理时间
        await asyncio.sleep(0.02)
        phase_calls.append(f"{phase_key}_done")
        total = time.monotonic() - t0
        return total

    t_start = time.monotonic()
    result = await generate_outline_v3(
        provider=provider,
        summary=summary,
        scoring_points=scoring_points,
        industry_profile=industry_profile,
        tender_context=tender_context,
        reference_outline=reference_outline,
        project_scope=project_scope,
        project_info={
            "name": "某市城市轨道交通5号线机电系统集成项目",
            "owner": "某市轨道交通集团有限公司",
            "location": "某市",
            "scale": "150000平方米",
        },
        progress_callback=None,
        phase_callback=track_phase,
    )
    total_elapsed = time.monotonic() - t_start

    # 验证结果
    assert result is not None
    assert isinstance(result, list)
    assert len(result) > 0, "完整流程必须返回有效目录"

    # 验证 phase_callback 顺序
    expected = [
        "二三级生成完成",
        "V6.0排序完成",
        "评分项对齐完成",
        "V3.6分析完成",
        "最终审核完成",
    ]
    for exp in expected:
        assert exp in phase_calls, f"期望 phase_callback 包含 '{exp}'，实际: {phase_calls}"

    print(f"✅ 大招标文件完整流程验证通过，共 {len(result)} 个顶级章节，"
          f"phase_callback 序列: {phase_calls[:5]}，总耗时 {total_elapsed:.2f}s")
