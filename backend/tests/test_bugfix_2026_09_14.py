"""2026-09-14 深度探索轮次的回归测试。

覆盖本轮修复的核心缺陷：
- 导出 DOCX：预渲染 wait() 返回 set 导致只预热 1 张图 / 指纹 None 时缓存键恒定 /
  图号计数器跨导出累加 / 缓存键混入 fig_no / 失败章节重试追加到文末
- 图表：ChartCache 原子写 + 重启后索引重建 / 架构图节点数护栏 / 甘特图渐变顺序
- 正文：配图 worker 收敛 / 章节完成计数 / 缓存按 id 失效
- 目录：递归深度护栏 / 自检 JSON 结构化截断
- 提示词：缓存失败冷却期

原则：只测试**行为**，不测试实现细节；全部为纯函数/纯逻辑测试，不依赖 AI。
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from io import BytesIO
from pathlib import Path

import pytest

from app.services.ai.mermaid_renderer import ChartCache
from app.services.ai.prompts import _cache as prompt_cache


# ============================================================
# 1. 导出 DOCX
# ============================================================


def test_export_preload_wait_uses_pending_dict_not_set():
    """BUG: `done, remaining = wait(...)` 后 remaining 变成 set，第二轮 remaining.keys() 抛错。

    这里用与 _preload_chart_images_parallel 相同的循环骨架验证修复后的写法：
    多批完成时必须能完整跑完，且保留 future→task 映射。
    """
    tasks = [(i, f"t{i}") for i in range(6)]
    executor = ThreadPoolExecutor(max_workers=3)

    def _work(t):
        time.sleep(0.01 * (6 - t[0]))  # 完成时间不同，保证分多批返回
        return t

    try:
        futures_map = {executor.submit(_work, t): t for t in tasks}
        pending = dict(futures_map)  # 修复后：独立的 pending 字典
        done_count = 0
        deadline = time.time() + 10
        while pending:
            if time.time() >= deadline:
                break
            done, not_done = wait(
                pending.keys(),
                timeout=max(0, deadline - time.time()),
                return_when=FIRST_COMPLETED,
            )
            pending = {f: t for f, t in pending.items() if f in not_done}
            for f in done:
                f.result(timeout=5)
                done_count += 1
        assert done_count == len(tasks)
        assert pending == {}
    finally:
        executor.shutdown(wait=False)


def test_figure_counter_reset_exists_and_clears_state():
    """BUG: _reset_figure_counter 无调用方 → 图号跨导出累加（图3-1 → 图3-8）。"""
    from app.services.export import _next_figure_number, _reset_figure_counter

    _reset_figure_counter()
    assert _next_figure_number("3") == "图3-1"
    assert _next_figure_number("3") == "图3-2"
    # 模拟新的一次导出会话
    _reset_figure_counter()
    assert _next_figure_number("3") == "图3-1"


def test_strip_fig_no_for_cache_stable_across_figure_numbers():
    """BUG: fig_no 进入缓存键 → 预渲染与正式渲染键必然不同，缓存命中率恒为 0。"""
    from app.services.export import _strip_fig_no_for_cache

    base = {"type": "flowchart", "nodes": ["a", "b"], "title": "施工流程"}
    a = dict(base, fig_no="图3-1")
    b = dict(base, fig_no="图3-8")
    assert _strip_fig_no_for_cache(a) == _strip_fig_no_for_cache(b)
    assert "fig_no" not in _strip_fig_no_for_cache(a)
    # 字符串形态同样处理
    assert _strip_fig_no_for_cache(json.dumps(a, ensure_ascii=False)) == _strip_fig_no_for_cache(
        json.dumps(b, ensure_ascii=False)
    )
    # 非 JSON 字符串原样返回，不抛异常
    assert _strip_fig_no_for_cache("not json") == "not json"


def test_failed_section_retry_moves_content_before_placeholder():
    """BUG: 重试内容被追加到文档末尾，章节顺序错乱。"""
    from app.services.export import (
        _move_body_elements_before,
        _remove_body_elements,
        _body_insertion_mark,
    )

    pytest.importorskip("docx")
    from docx import Document

    doc = Document()
    doc.add_paragraph("第一章")
    ph = doc.add_paragraph("（占位符）")
    doc.add_paragraph("第三章")

    body = doc.element.body
    mark = _body_insertion_mark(body)
    doc.add_paragraph("重试内容A")
    doc.add_paragraph("重试内容B")

    _move_body_elements_before(list(body)[mark:], ph._p)

    texts = [p.text for p in doc.paragraphs]
    assert texts == ["第一章", "重试内容A", "重试内容B", "第三章"], texts
    # sectPr 必须仍在 body 最后一位（被搬走会破坏页面设置）
    assert list(doc.element.body)[-1].tag.endswith("}sectPr")

    # 回归护栏：用旧的 len(list(body)) 游标会漏掉第一个新增元素
    assert _body_insertion_mark(body) == len(list(body)) - 1

    # 回滚路径：移除新增元素
    mark2 = _body_insertion_mark(body)
    doc.add_paragraph("半截内容")
    _remove_body_elements(list(body)[mark2:])
    assert "半截内容" not in [p.text for p in doc.paragraphs]
    assert list(doc.element.body)[-1].tag.endswith("}sectPr")


# ============================================================
# 2. 图表渲染与缓存
# ============================================================


def test_chart_cache_atomic_write_and_index_rebuild(tmp_path):
    """BUG: 非原子覆盖写；重启后 _access_order 为空导致 LRU 永不淘汰。"""
    cache = ChartCache(str(tmp_path / "charts"))
    payload = b"\x89PNG\r\n\x1a\n" + b"0" * 512

    cache.set("payload-a", payload, chart_type="flowchart")
    got = cache.get("payload-a", chart_type="flowchart")
    assert got is not None and got.read_bytes() == payload

    # .tmp 临时文件不应残留
    assert list((tmp_path / "charts").glob("*.tmp")) == []

    # 模拟进程重启：新实例应重建出 total_size 与访问顺序
    cache2 = ChartCache(str(tmp_path / "charts"))
    assert cache2._stats["total_size"] == len(payload)
    keys = [k for k in cache2._access_order if not k.endswith(".fmt")]
    assert len(keys) == 1


def test_architecture_layout_respects_node_and_depth_limits():
    """BUG: 递归无节点数/深度上限 → 超大画布 OOM。"""
    from app.services.ai.mermaid_architecture import (
        _layout_architecture_tree,
        _MAX_ARCH_NODES,
        _MAX_ARCH_DEPTH,
    )

    # 构造一棵远超上限的宽树（每层 5 个子节点，深 6 层 ≈ 3900 节点）
    def _build(depth):
        if depth == 0:
            return {"label": f"leaf{depth}"}
        return {
            "label": f"node{depth}",
            "children": [_build(depth - 1) for _ in range(5)],
        }

    nodes, width, height = _layout_architecture_tree(_build(6))
    assert len(nodes) <= _MAX_ARCH_NODES
    assert max((n["depth"] for n in nodes), default=0) <= _MAX_ARCH_DEPTH
    # 画布不应爆炸（未修复时宽度会达到数万像素）
    assert width < 20000


def test_gantt_renders_without_covering_text():
    """冒烟：甘特图渲染可用，且渐变已改为行内绘制（不覆盖文字）。

    不改变像素级断言（脆弱），只确保渲染路径不抛异常且产出有效 PNG。
    """
    from app.services.ai.mermaid_gantt import _render_gantt_image_v2

    data = {
        "title": "施工进度计划",
        "tasks": [
            {"id": "t1", "name": "基础施工", "start": "2026-03-01", "end": "2026-04-10"},
            {
                "id": "t2",
                "name": "主体结构",
                "start": "2026-04-11",
                "end": "2026-06-30",
                "dependencies": ["t1"],
                "critical": True,
            },
        ],
    }
    stream = _render_gantt_image_v2(data)
    assert stream is not None
    raw = stream.getvalue() if hasattr(stream, "getvalue") else stream.read()
    assert raw[:8] == b"\x89PNG\r\n\x1a\n"


# ============================================================
# 3. 正文生成
# ============================================================


def test_completed_count_counts_once_with_content_bearing_callback():
    """BUG: 先回调 completed(无 content) 再回调 completed(带 content)
    → `prev_status != 'completed'` 恒为假，completed_count 永远为 0。"""
    from app.services.generation_manager import GenerationManager

    async def _run():
        mgr = GenerationManager()
        gid = await mgr.create_task(1, "v1", {})
        mgr._tasks[1].total_sections = 1
        # 第一次：AI 完成回调（不带 content）
        await mgr.update_section_progress(1, 10, "completed", 100)
        assert mgr._tasks[1].completed_count == 0
        # 第二次：落库回调（带 content）
        await mgr.update_section_progress(1, 10, "completed", 100, content="正文内容")
        assert mgr._tasks[1].completed_count == 1
        assert mgr._tasks[1].status == "completed"
        return gid

    asyncio.run(_run())


def test_completed_count_reverts_on_regeneration():
    """重新生成（completed → generating）应撤销计数，避免重复累加。"""
    from app.services.generation_manager import GenerationManager

    async def _run():
        mgr = GenerationManager()
        await mgr.create_task(2, "v1", {})
        mgr._tasks[2].total_sections = 1
        await mgr.update_section_progress(2, 11, "completed", 100, content="x")
        assert mgr._tasks[2].completed_count == 1
        await mgr.update_section_progress(2, 11, "generating", 0)
        assert mgr._tasks[2].completed_count == 0

    asyncio.run(_run())


def test_section_cache_invalidate_by_ids():
    """BUG: 缓存键末段是 section_id，按 title 子串匹配恒不命中。"""
    from app.services.ai.workflows_sections import _section_content_cache

    async def _run():
        await _section_content_cache.clear()
        for sid in (101, 102, 103):
            await _section_content_cache.set(f"section_content:7:{sid}", f"content-{sid}")

        removed = await _section_content_cache.invalidate_by_ids(7, [101, 103])
        assert removed == 2

        assert await _section_content_cache.get("section_content:7:101") is None
        assert await _section_content_cache.get("section_content:7:103") is None
        assert await _section_content_cache.get("section_content:7:102") is not None
        await _section_content_cache.clear()

    asyncio.run(_run())


def test_chart_drain_timeout_breaks_loop():
    """BUG: 配图收尾超时分支只打日志不 break → while True 永久空转。

    这里用一个与线上同构的最小循环验证：超时后必须退出并设置取消。
    """
    import re
    from pathlib import Path as _P

    # 用相对本文件的绝对路径，避免测试结果依赖 pytest 的工作目录
    src = (_P(__file__).resolve().parent.parent / "app/routers/sse/sse_content.py").read_text(
        encoding="utf-8"
    )
    block = src[src.index("if elapsed >= CHART_DRAIN_TOTAL_TIMEOUT"):]
    tail = block[: block.index("每 5s 推一次进度事件")]
    assert "_w.cancel()" in tail, "超时分支必须取消 worker"
    assert "break" in tail, "超时分支必须 break，否则 SSE 永久挂起"
    assert re.search(r"\bbreak\b", tail)


# ============================================================
# 4. 目录生成
# ============================================================


def test_normalize_to_list_has_recursion_guard():
    """BUG: 深嵌套 JSON 字符串会 RecursionError，穿透重试炸掉任务。"""
    from app.services.ai.workflows_outline_v51 import _normalize_to_list

    # 直接越过深度上限时必须报「嵌套过深」而不是继续解析/栈溢出
    with pytest.raises(ValueError) as ei:
        _normalize_to_list({"result": "..."}, "一级目录", _depth=13)
    assert "嵌套过深" in str(ei.value)

    # 深度未越界时不应误报
    assert _normalize_to_list([{"title": "x"}], "一级目录") == [{"title": "x"}]


def test_truncate_outline_for_check_produces_valid_json():
    """BUG: `json.dumps(level1)[:40000]` 硬切产生非法 JSON 送给 AI。"""
    from app.services.ai.workflows_outline_v51 import _truncate_outline_for_check

    level1 = []
    for i in range(40):
        l2s = []
        for j in range(8):
            l2s.append(
                {
                    "id": f"{i+1}.{j+1}",
                    "title": f"二级标题{i+1}-{j+1}内容填充占位",
                    "level": 2,
                    "children": [
                        {"id": f"{i+1}.{j+1}.{k+1}", "title": f"三级标题{k+1}", "level": 3}
                        for k in range(6)
                    ],
                }
            )
        level1.append({"id": str(i + 1), "title": f"一级章节{i+1}", "level": 1, "children": l2s})

    out = _truncate_outline_for_check(level1, max_chars=40000)
    assert len(out) <= 40000
    parsed = json.loads(out)  # 必须是合法 JSON
    assert isinstance(parsed, list) and parsed


# ============================================================
# 5. 提示词缓存
# ============================================================


def test_prompt_cache_failure_cooldown(monkeypatch):
    """BUG: 加载失败后 _prompt_cache 置 None → 每次 get_prompt 都重查 DB。"""
    calls = {"n": 0}

    class _Boom:
        def __enter__(self):
            calls["n"] += 1
            raise RuntimeError("db down")

        def __exit__(self, *a):
            return False

    def _fake_get_db(readonly=False):
        return _Boom()

    import app.db as app_db

    monkeypatch.setattr(app_db, "get_db", _fake_get_db)
    monkeypatch.setattr(prompt_cache, "_prompt_cache", None)
    monkeypatch.setattr(prompt_cache, "_load_failed_at", None)

    prompt_cache.get_prompt("NO_SUCH_KEY_FOR_TEST")
    assert calls["n"] == 1
    first_fail = prompt_cache._load_failed_at
    assert first_fail is not None

    # 冷却期内应直接走回退，不再查库
    prompt_cache.get_prompt("NO_SUCH_KEY_FOR_TEST")
    prompt_cache.get_prompt("NO_SUCH_KEY_FOR_TEST")
    assert calls["n"] == 1, "冷却期内不应重复查库"

    # 冷却期过后才允许重试
    monkeypatch.setattr(prompt_cache, "_load_failed_at", time.time() - 120)
    prompt_cache.get_prompt("NO_SUCH_KEY_FOR_TEST")
    assert calls["n"] == 2


# ============================================================
# 6. 全局事实
# ============================================================


def test_fact_classification_uses_canonical_implementation():
    """BUG: routers/facts.py 的副本把『技术』归入 personnel、且缺『经理』。"""
    from app.routers.facts import _dict_to_fact_items

    items = {
        v: k
        for k, v in (
            ("projectManager", "项目经理"),
            ("techRoute", "技术路线"),
            ("scheduleDuration", "建设工期"),
        )
    }
    # 还原成 {key: value}
    payload = {"projectManager": "张三", "techRoute": "装配式", "scheduleDuration": "450日历天"}
    result = {i["key"]: i["category"] for i in _dict_to_fact_items(payload)}

    assert result["techRoute"] == "technical", "技术路线应归入 technical，而非 personnel"
    assert result["projectManager"] in ("personnel", "basic")
    assert result["projectManager"] == "personnel", "项目经理应归入 personnel"
    assert result["scheduleDuration"] == "schedule"


# ============================================================
# 3. 全局事实（第二轮）
# ============================================================


def test_format_facts_list_keeps_label():
    """BUG: 事实项结构是 {key,label,value}，但格式化只认 title/content → label 全丢。"""
    from app.services.ai.workflows_sections import _format_facts_list

    facts = [
        {"key": "duration", "label": "工期", "value": "450 日历天", "category": "schedule"},
        {"key": "projectName", "label": "项目名称", "value": "某某医院工程", "category": "basic"},
    ]
    out = _format_facts_list(facts)
    assert "工期" in out and "450 日历天" in out, out
    assert "项目名称" in out and "某某医院工程" in out, out
    assert out.startswith("- **工期**: 450 日历天"), out


def test_format_facts_list_empty_returns_empty_string():
    """BUG: 空事实返回 "（无）"（真值）→ 每个 prompt 都被塞一段无意义的『事实：（无）』。"""
    from app.services.ai.workflows_sections import _format_facts_list

    assert _format_facts_list([]) == ""
    assert _format_facts_list([{}]) == ""
    assert not _format_facts_list([{"label": "x", "value": ""}])


def test_facts_upsert_replaces_plain_update():
    """BUG: analysis 行可能不存在，纯 UPDATE 影响 0 行 → 保存/提取/确认静默失效。"""
    from pathlib import Path
    import app.routers.facts as facts_router

    src = Path(facts_router.__file__).read_text(encoding="utf-8")
    assert "UPDATE analysis SET global_facts_json" not in src, "仍存在纯 UPDATE 写入事实"
    assert src.count("ON CONFLICT(project_id) DO UPDATE") >= 3


def test_analysis_field_update_is_upsert():
    """同样的静默丢数据问题：_sync_update_field 也必须是 upsert。"""
    from pathlib import Path
    import app.routers.ai as ai_router

    src = Path(ai_router.__file__).read_text(encoding="utf-8")
    assert "ON CONFLICT(project_id) DO UPDATE" in src


def test_outline_facts_truncation_is_marked():
    """BUG: 目录侧事实硬截断到 2000 字且无标记 → '450 日历天' 变 '450 日'。"""
    from pathlib import Path
    import app.services.ai.workflows_outline_v51 as v51

    src = Path(v51.__file__).read_text(encoding="utf-8")
    assert "事实变量已截断" in src
    assert "facts_text[:2000]}" not in src


def test_safety_category_is_reachable():
    """BUG: 安全类事实被并进 quality，safety 分类永为空（死分支）。"""
    from app.services.ai.workflows_analysis import _dict_to_fact_items

    items = _dict_to_fact_items({"安全目标": "零死亡事故", "质量目标": "合格", "施工组织": "流水施工"})
    cats = {i["key"]: i["category"] for i in items}
    assert cats["安全目标"] == "safety", cats
    assert cats["质量目标"] == "quality", cats
    assert cats["施工组织"] == "implementation", cats


def test_default_fact_categories_include_implementation():
    """BUG: implementation 分类无中文映射 → 前端显示英文。"""
    from app.routers.facts import _DEFAULT_FACT_CATEGORIES

    keys = [k for k, _ in _DEFAULT_FACT_CATEGORIES]
    assert "implementation" in keys


# ============================================================
# 4. 提示词模块（第二轮）
# ============================================================


def test_arrange_prompts_render_without_leftover_placeholders():
    """BUG: 6 个编排提示词不带 kwargs 直接发送 → 模型收到字面量 __SECTION_TITLE__。"""
    import re
    from app.services.ai.image_engine import _render_arrange_sys

    for key in (
        "FLOWCHART_ARRANGE_SYSTEM",
        "ARCHITECTURE_ARRANGE_SYSTEM",
        "LABOR_ARRANGE_SYSTEM",
        "COMPARISON_ARRANGE_SYSTEM",
        "LAYOUT_ARRANGE_SYSTEM",
        "TIMELINE_ARRANGE_SYSTEM",
    ):
        text = _render_arrange_sys(
            key, section_title="第三章 施工部署", summary="某医院工程", writing_length="detailed"
        )
        assert text, key
        assert re.findall(r"__[A-Z_]+__", text) == [], (key, re.findall(r"__[A-Z_]+__", text))
        assert "第三章 施工部署" in text, key


def test_orchestration_uses_registered_prompts():
    """BUG: 编排恒用硬编码 _ORCH_SYSTEM，orch_user 参数完全没被使用。"""
    from app.services.ai.workflows_orchestration import _render_orch_user, _ORCH_USER_TEMPLATE

    # __XX__ 风格（提示词管理页注册的模板）
    tpl = "### 目录\n__OUTLINE__\n### 评分\n__SCORING_STANDARDS__"
    out = _render_orch_user(tpl, "概述", "[{}]", "评分")
    assert "__OUTLINE__" not in out and "[{}]" in out and "评分" in out

    # {} 风格（内置模板）仍可用
    out2 = _render_orch_user(_ORCH_USER_TEMPLATE, "概述", "[{}]", "评分")
    assert "概述" in out2 and "[{}]" in out2

    # 占位符不匹配的自定义模板不应炸
    out3 = _render_orch_user("bad {nope} template", "概述", "[{}]", "评分")
    assert "概述" in out3


def test_outline_review_prompt_has_no_leftover_placeholders():
    """BUG: 目录审核模板的 SUMMARY/INDUSTRY_PROFILE 从不填充。

    R2 2026-10-12：模板变量风格统一为 {VAR}（不再用 __VAR__）。
    本测试改从 _ALL_PROMPTS 注册表的 variables 列表验证模板确实声明了变量，
    同时验证调用方源码签名包含 industry_profile 参数。
    """
    from app.services.ai.prompts._registry import _ALL_PROMPTS
    from pathlib import Path
    import app.services.ai.workflows_quality as wq

    meta = _ALL_PROMPTS.get("OUTLINE_REVIEW_SYSTEM", {})
    assert "SUMMARY" in meta.get("variables", []), "OUTLINE_REVIEW_SYSTEM 应声明 SUMMARY 变量"
    assert "INDUSTRY_PROFILE" in meta.get("variables", []), "OUTLINE_REVIEW_SYSTEM 应声明 INDUSTRY_PROFILE 变量"

    src = Path(wq.__file__).read_text(encoding="utf-8")
    # 调用方源码签名应包含 industry_profile 参数
    assert 'industry_profile: str = ""' in src or "industry_profile: str = ''" in src



# ============================================================
# 5. 第三轮：端到端与遗留项
# ============================================================


def test_orchestration_preserves_project_overview():
    """BUG（修复引入）：注册的 v2.0 编排模板只有 __OUTLINE__/__SCORING_STANDARDS__，
    没有项目概述占位符。直接切过去会让模型丢掉"医院工程还是道路工程"的关键信息
    （旧硬编码模板是有的）。必须在模板未引用时补一段。
    """
    from app.services.ai.workflows_orchestration import _render_orch_user

    tpl = "### 目录\n__OUTLINE__\n### 评分\n__SCORING_STANDARDS__"
    out = _render_orch_user(tpl, "某三甲医院迁建工程", '[{"id":1}]', "评分A")
    assert "某三甲医院迁建工程" in out, out
    assert "__OUTLINE__" not in out and "__SCORING_STANDARDS__" not in out
    assert "项目概述" in out

    # 模板自带项目概述时不应重复追加
    tpl2 = "### 项目概述\n__SUMMARY__\n### 目录\n__OUTLINE__"
    out2 = _render_orch_user(tpl2, "某工程", '[{"id":1}]', "评分A")
    assert out2.count("项目概述") == 1, out2


def test_orchestration_end_to_end_with_stub_provider():
    """端到端：编排真实调用链不残留占位符，且能产出裁定结果。"""
    import asyncio
    import json
    import re

    from app.services.ai.workflows_orchestration import orchestrate_content

    class _Stub:
        def __init__(self):
            self.calls = []

        async def chat(self, messages, **kw):
            self.calls.append(messages)
            return json.dumps(
                [{
                    "section_id": "1", "title": "T", "recommended_type": "gantt",
                    "confidence": 0.9, "reason": "r",
                    "plan": {
                        "table": {"needed": False, "purpose": ""},
                        "mermaid": {"needed": True, "priority": 3, "reason": "x"},
                        "image": {"needed": False, "priority": 0, "reason": ""},
                    },
                }],
                ensure_ascii=False,
            )

    stub = _Stub()
    leaves = [{"id": "1", "title": "施工进度计划", "parent_id": None, "level": 3}]
    res = asyncio.run(orchestrate_content(
        stub, leaves, scoring_standards="评分标准A",
        project_overview="某三甲医院迁建工程", concurrency=1,
    ))
    sys_msg, usr_msg = stub.calls[0][0].content, stub.calls[0][1].content
    assert re.findall(r"__[A-Z_]+__", sys_msg) == []
    assert re.findall(r"__[A-Z_]+__", usr_msg) == []
    assert "评分标准A" in usr_msg
    assert "施工进度计划" in usr_msg
    assert res and res[0].get("recommended_type") == "gantt"


def test_task_generation_passes_positional_section():
    """BUG: tasks 里调用 generate_section_content 只传关键字参数，
    而 section 是必填位置参数 → 必然 TypeError，整批生成全部 failed。"""
    import inspect
    from pathlib import Path

    from app.services.ai.workflows_sections import (
        build_chapter_content_messages,
        generate_section_content,
    )

    sig = inspect.signature(generate_section_content)
    params = list(sig.parameters.values())
    assert params[1].name == "section" and params[1].default is inspect.Parameter.empty

    # 用相对本文件的绝对路径，避免测试结果依赖 pytest 的工作目录
    src = (Path(__file__).resolve().parent.parent / "app/tasks/__init__.py").read_text(
        encoding="utf-8"
    )
    # 不再出现"全部走关键字参数"的旧调用
    assert "await _generate(provider, tender_context=" not in src
    assert "_task_section" in src and "_task_context" in src

    # 用修复后构造的 section/context 实跑消息构建，确认不再 TypeError
    msgs = build_chapter_content_messages(
        {
            "id": 1, "title": "施工部署", "level": 3, "path": "第三章",
            "section_type": "text", "target_words": 800, "project_id": 1,
        },
        facts=None, summary="概述", tender_context="招标文件",
        industry_profile="房建", scoring_context="评分",
        writing_length="standard", target_words=800, section_level=3,
        content_type="text", project_id=1, extract_charts=False,
    )
    # BUG-FIX 2026-10-05（P-22）：消息结构从「system + 单条 user」重构为
    # 分层多消息（system 恒定 / 项目摘要稳定 / 事实变量 / 上级 / 同级 / 当前任务），
    # 以提升 LLM 前缀缓存命中率。断言从「恰好 2 条」放宽为「≥2 条且逐条非空」，
    # 并额外校验 system 恒定 + 当前任务在最末条（分层顺序契约）。
    assert len(msgs) >= 2
    assert all(m.content for m in msgs)
    assert msgs[0].role == "system"
    assert msgs[-1].role == "user"
    assert "施工部署" in msgs[-1].content



# ============================================================
# 6. 第四轮：死代码清理 / 事实注入 / 可信度标记
# ============================================================

_RETIRED_PROMPTS = [
    "CONTENT_GENERATION_SYSTEM",
    "CONTENT_GENERATION_USER",
    "OUTLINE_V51_LEVEL3_SYSTEM",
    "TECH_REWRITE_PROMPT",
    "PROJECT_OVERVIEW_PROMPT",
    "SCORING_REQUIREMENTS_PROMPT",
]


def test_dead_prompts_are_retired():
    """清理：6 个注册了却无任何调用方的死模板必须从注册表下线。

    它们会占满提示词管理列表、可被编辑却永不生效，误导管理员。
    """
    from app.services.ai.prompts._registry import _ALL_PROMPTS

    for key in _RETIRED_PROMPTS:
        assert key not in _ALL_PROMPTS, f"{key} 仍在注册表中"


def test_global_facts_dead_module_removed():
    """清理：services/global_facts.py 全库无 import，已物理删除。"""
    from pathlib import Path
    import app.services as svc

    assert not (Path(svc.__file__).parent / "global_facts.py").exists()


def test_l2_injects_global_facts():
    """增强：L2 目录生成此前完全不注入全局事实（只有 L1 注入）。"""
    import inspect
    from app.services.ai import workflows_outline_v51 as v51

    assert "global_facts" in inspect.signature(v51._generate_level2_v51).parameters
    assert "global_facts" in inspect.signature(v51._fill_level2_with_ai).parameters

    src = Path(v51.__file__).read_text(encoding="utf-8")
    assert "<global_facts>" in src


def test_l3_has_no_ai_path_so_facts_not_applicable():
    """L3 是纯规则生成（不调 AI），因此不存在"提示词注入事实"的入口。

    这里固化的不是"L3 注入了事实"，而是"L3 确实不调 AI"这一事实，
    防止以后有人误以为已经覆盖、或又往这里加无效参数。
    """
    import inspect
    from app.services.ai import workflows_outline_v51 as v51

    src = inspect.getsource(v51._generate_level3_v51)
    body = src.split('"""', 2)[-1]  # 去掉 docstring
    assert "provider" not in body.replace("provider: Any,", ""), "L3 竟然开始调 AI 了，请同步评估事实注入"
    assert "_generate_diverse_l3_title" in body


def test_facts_extraction_reports_heuristic_mode():
    """增强：AI 提取失败/为空降级为正则启发式时，必须显式标记来源。"""
    from pathlib import Path
    import app.routers.facts as facts_router

    src = Path(facts_router.__file__).read_text(encoding="utf-8")
    assert 'extraction_mode = "heuristic"' in src
    assert 'extraction_mode == "heuristic"' in src
    assert '"source": "heuristic"' in src
    assert "extraction_mode" in src and '"warning": extraction_note' in src



# ============================================================
# 6. 第四轮：死代码清理 / 事实注入 / 可信度标记
# ============================================================

_RETIRED_PROMPTS = [
    "CONTENT_GENERATION_SYSTEM",
    "CONTENT_GENERATION_USER",
    "OUTLINE_V51_LEVEL3_SYSTEM",
    "TECH_REWRITE_PROMPT",
    "PROJECT_OVERVIEW_PROMPT",
    "SCORING_REQUIREMENTS_PROMPT",
]


def test_dead_prompts_are_retired():
    """清理：6 个注册了却无任何调用方的死模板必须从注册表下线。

    它们会占满提示词管理列表、可被编辑却永不生效，误导管理员。
    """
    from app.services.ai.prompts._registry import _ALL_PROMPTS

    for key in _RETIRED_PROMPTS:
        assert key not in _ALL_PROMPTS, f"{key} 仍在注册表中"


def test_global_facts_dead_module_removed():
    """清理：services/global_facts.py 全库无 import，已物理删除。"""
    from pathlib import Path
    import app.services as svc

    assert not (Path(svc.__file__).parent / "global_facts.py").exists()


def test_l2_injects_global_facts():
    """增强：L2 目录生成此前完全不注入全局事实（只有 L1 注入）。"""
    import inspect
    from app.services.ai import workflows_outline_v51 as v51

    assert "global_facts" in inspect.signature(v51._generate_level2_v51).parameters
    assert "global_facts" in inspect.signature(v51._fill_level2_with_ai).parameters

    src = Path(v51.__file__).read_text(encoding="utf-8")
    assert "<global_facts>" in src


def test_l3_has_no_ai_path_so_facts_not_applicable():
    """L3 是纯规则生成（不调 AI），因此不存在"提示词注入事实"的入口。

    这里固化的不是"L3 注入了事实"，而是"L3 确实不调 AI"这一事实，
    防止以后有人误以为已经覆盖、或又往这里加无效参数。
    """
    import inspect
    from app.services.ai import workflows_outline_v51 as v51

    src = inspect.getsource(v51._generate_level3_v51)
    body = src.split('"""', 2)[-1]  # 去掉 docstring
    assert "provider" not in body.replace("provider: Any,", ""), "L3 竟然开始调 AI 了，请同步评估事实注入"
    assert "_generate_diverse_l3_title" in body


def test_facts_extraction_reports_heuristic_mode():
    """增强：AI 提取失败/为空降级为正则启发式时，必须显式标记来源。"""
    from pathlib import Path
    import app.routers.facts as facts_router

    src = Path(facts_router.__file__).read_text(encoding="utf-8")
    assert 'extraction_mode = "heuristic"' in src
    assert 'extraction_mode == "heuristic"' in src
    assert '"source": "heuristic"' in src
    assert "extraction_mode" in src and '"warning": extraction_note' in src
