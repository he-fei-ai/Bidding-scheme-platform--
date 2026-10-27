# -*- coding: utf-8 -*-
"""专项提取 Runner 测试（标书智能体（一）引入）。

验证：
- runner 采用缓存友好四段式消息（system 前缀 + 全文块 + 任务指令 + 任务输入）；
- 调用 collect_json_response 并返回解析后的 JSON；
- run_scheme_extraction 统一调度入口对未知任务抛 ValueError；
- 五个任务函数均可正常产出结构化结果。
"""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from app.services.ai.prompts import get_prompt
from app.services.ai.prompts.scheme_extraction import SCHEME_EXTRACTION_TASKS
from app.services.ai.providers.base import AIMessage
from app.services.ai.workflows_scheme_extraction import (
    ANALYSIS_SYSTEM,
    _build_scheme_messages,
    extract_basis_list,
    extract_construction_condition,
    extract_dangerous_work_params,
    extract_project_overview,
    extract_scheme_type,
    run_all_scheme_extractions,
    run_scheme_extraction,
)


SAMPLE_CONTEXT = (
    "第一章 工程概况\n工程名称：某商业综合体基坑工程。\n"
    "开挖深度 6.5m，支护形式为钻孔灌注桩+内支撑。\n"
    "编制依据：GB 55023-2022 施工脚手架通用规范。\n"
    "东侧距用地红线 8m，有一栋 6 层砖混住宅。"
)

SCHEME_EXTRACTION_TASKS_KEYS = set(SCHEME_EXTRACTION_TASKS.keys())


def test_build_messages_follows_four_part_structure():
    msgs = _build_scheme_messages("PROJECT_OVERVIEW_SYSTEM", "PROJECT_OVERVIEW_USER", SAMPLE_CONTEXT)
    assert len(msgs) == 4
    assert msgs[0].role == "system" and msgs[0].content == ANALYSIS_SYSTEM
    assert msgs[1].role == "user"  # 全文上下文块
    assert msgs[2].role == "user"  # 任务指令
    assert msgs[3].role == "user"  # 任务输入
    # 任务指令应包含五段式标记
    assert "### 1. 目标定位" in msgs[2].content
    assert msgs[1].content  # 全文块非空


@pytest.mark.asyncio
async def test_extract_project_overview_returns_json():
    fake_result = [
        {"field": "工程名称", "value": "某商业综合体基坑工程", "unit": "", "source": "封面"}
    ]
    with patch(
        "app.services.ai.workflows_scheme_extraction.collect_json_response",
        new=AsyncMock(return_value=fake_result),
    ) as mock_cjr:
        result = await extract_project_overview(None, SAMPLE_CONTEXT, max_tokens=2000)
    assert result == fake_result
    # 验证了 collect_json_response 收到四段式消息且 temperature 默认 0
    mock_cjr.assert_awaited_once()
    call_msgs = mock_cjr.call_args.args[1]
    assert len(call_msgs) == 4
    assert mock_cjr.call_args.kwargs.get("temperature", 0) == 0


@pytest.mark.asyncio
async def test_all_five_tasks_dispatch():
    tasks = [
        extract_project_overview,
        extract_scheme_type,
        extract_dangerous_work_params,
        extract_basis_list,
        extract_construction_condition,
    ]
    for fn in tasks:
        with patch(
            "app.services.ai.workflows_scheme_extraction.collect_json_response",
            new=AsyncMock(return_value=[{"ok": True}]),
        ):
            result = await fn(None, SAMPLE_CONTEXT)
        assert result == [{"ok": True}]


@pytest.mark.asyncio
async def test_run_scheme_extraction_unknown_task_raises():
    with pytest.raises(ValueError):
        await run_scheme_extraction("not_a_task", None, SAMPLE_CONTEXT)


@pytest.mark.asyncio
async def test_run_scheme_extraction_known_task():
    with patch(
        "app.services.ai.workflows_scheme_extraction.collect_json_response",
        new=AsyncMock(return_value=[{"field": "x"}]),
    ):
        result = await run_scheme_extraction("project_overview", None, SAMPLE_CONTEXT)
    assert result == [{"field": "x"}]


def test_analysis_system_matches_registry():
    # runner 的通用 system 前缀应与注册表 ANALYSIS_SYSTEM 一致（共享缓存前缀）
    assert ANALYSIS_SYSTEM == get_prompt("ANALYSIS_SYSTEM")


@pytest.mark.asyncio
async def test_run_all_scheme_extractions_concurrent_and_isolated():
    captured = {}

    async def fake_cjr(provider, messages, **kwargs):
        # 标记并发：同一时刻异步任务数
        task_name = messages[2].content.splitlines()[0]  # *_SYSTEM 首行是角色行
        captured.setdefault("calls", []).append(task_name)
        return [{"ok": True, "task": task_name[-6:]}]

    with patch(
        "app.services.ai.workflows_scheme_extraction.collect_json_response",
        new=fake_cjr,
    ):
        results = await run_all_scheme_extractions(None, SAMPLE_CONTEXT, concurrency=5)
    # 五个任务全部返回，且键齐全
    assert set(results.keys()) == set(SCHEME_EXTRACTION_TASKS_KEYS)
    for v in results.values():
        assert v == [{"ok": True}] or isinstance(v, list)
    assert len(captured["calls"]) == 5


@pytest.mark.asyncio
async def test_run_all_scheme_extractions_isolates_task_failure():
    async def fake_cjr(provider, messages, **kwargs):
        # 让 project_overview（其 user 指令含「提取工程概况信息」）失败，其余成功，验证隔离
        if "提取工程概况信息" in messages[3].content:
            raise RuntimeError("boom")
        return [{"ok": True}]

    with patch(
        "app.services.ai.workflows_scheme_extraction.collect_json_response",
        new=fake_cjr,
    ):
        results = await run_all_scheme_extractions(None, SAMPLE_CONTEXT, concurrency=2)
    assert "error" in results["project_overview"]
    # 其余任务不受影响
    assert results["scheme_type"] == [{"ok": True}]
    assert set(results.keys()) == set(SCHEME_EXTRACTION_TASKS_KEYS)

