# -*- coding: utf-8 -*-
"""正文生成模块深度审计 BUG 回归测试（BUG-FIX 2026-10-04）。

覆盖本次修复的四个缺陷：
- BUG-A（P1）：_auto_continue_on_length 把 return_meta 从续写 kwargs 中 pop 掉，
  provider 只返回 str → finish_reason 恒为 "stop" → 续写最多 1 轮，
  max_rounds=3 形同虚设，超长章节仍被截断。
- BUG-B（P1）：正文生成 SSE 心跳/事件写失败分支 cancel_event.set() 杀死
  后台续跑任务，与 finally「保留任务后台运行」/_drain_queue/
  run_generation client_disconnected finalize/前端 detach+pollAndReconnect
  契约全部矛盾；外层 CancelledError 分支漏置 client_disconnected，
  导致 finally 误走 task.cancel() 路径。
- BUG-C（P1）：update_section 修改 content 时只在返回对象里重算 word_count，
  未持久化 DB 列——一致性审计/前端编辑保存后目录字数与正文实际字数不一致。
- BUG-D（P2）：编排调用对已是 JSON 字符串的 scoring_points 做 json.dumps
  二次编码，转义引号吞噬提示词 1000 字截断预算并破坏可读性。
"""
from __future__ import annotations

import asyncio
import inspect
import re

import pytest

from app.db import get_db, init_db
from app.models import SectionUpdate
from app.services import sections as sections_service
from app.services.sections import _count_words


# ---------------------------------------------------------------------------
# BUG-A：续写必须保留 return_meta，多轮续写生效
# ---------------------------------------------------------------------------

class _MetaProvider:
    """假 provider：return_meta=True 时返回 dict（含 finish_reason）。

    第 1、2 次返回 length（触发继续续写），第 3 次返回 stop。
    同时记录每次调用收到的 kwargs，用于断言 return_meta 未被剥离。
    """

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def chat(self, messages, **options):
        self.calls.append(dict(options))
        n = len(self.calls)
        if options.get("return_meta"):
            finish = "length" if n < 3 else "stop"
            return {"content": f"[续写{n}]", "finish_reason": finish, "usage": None}
        return f"[续写{n}]"


@pytest.mark.parametrize(
    "module_name",
    ["app.services.ai.workflows_sections"],
)
def test_auto_continue_keeps_return_meta_and_runs_multiple_rounds(module_name, monkeypatch):
    import importlib

    ws = importlib.import_module(module_name)
    # 隔离提示词注册表依赖：固定渲染结果
    monkeypatch.setattr(ws, "get_prompt", lambda key, **kw: f"prompt::{key}")

    provider = _MetaProvider()
    context = {"writing_length": "standard", "cancel_event": None}
    base_kwargs = {"return_meta": True, "max_retries": 1}

    final = asyncio.run(
        ws._auto_continue_on_length(
            provider, "初始正文", {"title": "测试章"}, context,
            base_kwargs, max_rounds=3,
        )
    )

    # ① 每轮续写调用都必须带 return_meta=True（BUG-A 核心断言）
    assert len(provider.calls) == 3, f"应续写 3 轮，实际 {len(provider.calls)} 轮"
    for call in provider.calls:
        assert call.get("return_meta") is True, "续写调用不得剥离 return_meta"

    # ② 多轮内容全部拼接（旧 bug 只会拼第 1 轮即 break）
    for i in (1, 2, 3):
        assert f"[续写{i}]" in final

    # ③ 原始 kwargs 不被污染（dict(base) 拷贝语义）
    assert base_kwargs == {"return_meta": True, "max_retries": 1}


def test_auto_continue_handles_object_response(monkeypatch):
    """provider 返回带 .content 属性对象时，不能把 repr 拼进正文。"""
    import app.services.ai.workflows_sections as ws

    monkeypatch.setattr(ws, "get_prompt", lambda key, **kw: "prompt")

    class _ObjResp:
        content = "[对象响应正文]"
        finish_reason = "stop"

    class _ObjProvider:
        async def chat(self, messages, **options):
            return _ObjResp()

    final = asyncio.run(
        ws._auto_continue_on_length(
            _ObjProvider(), "旧正文", {"title": "章"}, 
            {"writing_length": "standard", "cancel_event": None},
            {"return_meta": True}, max_rounds=3,
        )
    )
    assert "[对象响应正文]" in final
    assert "_ObjResp" not in final, "对象 repr 被拼进了正文"


# ---------------------------------------------------------------------------
# BUG-B：断线语义统一为「后台续跑」，源码级不变量固化
# ---------------------------------------------------------------------------

def _generate_region() -> str:
    from app.routers.sse import sse_content

    src = inspect.getsource(sse_content)
    start = src.index("async def generate_content_impl")
    end = src.index("async def expand_content_impl")
    return src[start:end]


def _code_lines(region: str) -> list[str]:
    """过滤掉注释行，只保留可执行语句（注释中允许提及历史行为）。"""
    return [
        ln for ln in region.splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]


def test_disconnect_branch_never_sets_cancel_event():
    """generate_content_impl 内不得再有断线→cancel_event.set()（BUG-B 核心）。"""
    region = _generate_region()
    offenders = [ln for ln in _code_lines(region) if "cancel_event.set()" in ln]
    assert not offenders, f"断线不应杀死后台任务，仍存在取消调用: {offenders}"


def test_outer_cancelled_error_marks_client_disconnected():
    """外层 CancelledError 分支必须置 client_disconnected=True 后再 raise，

    否则 finally 的 `if not client_disconnected: task.cancel()` 会杀死生成任务。
    """
    region = _generate_region()
    m = re.search(
        r"except asyncio\.CancelledError:(?:\s*#[^\n]*)*(?:\s*[^\n#]*)?client_disconnected = True"
        r"[^\n]*\n\s*(?:#[^\n]*\n\s*)*logger[^\n]*主循环[^\n]*\n\s*raise",
        region,
    )
    assert m, "外层 except asyncio.CancelledError 分支未先置 client_disconnected=True 再 raise"
    # 断线标志位总数：心跳失败 / 事件写失败 / wait_for CE / 外层 CE = 4 处
    assert region.count("client_disconnected = True") >= 4


def test_finally_keeps_task_on_client_disconnect():
    """finally 的后台保留路径必须存在：断开时不 cancel、启动 _drain_queue。"""
    region = _generate_region()
    assert "if not client_disconnected:" in region
    assert "task.cancel()" in region
    assert "_drain_queue" in region
    assert "保留任务在后台运行" in region


# ---------------------------------------------------------------------------
# BUG-C：update_section 改正文必须持久化 word_count
# ---------------------------------------------------------------------------

def _seed_section(pid: int, title: str, content: str = "旧") -> int:
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO sections (project_id, title, level, sort_order, content, "
            "word_count, status) VALUES (?, ?, 1, 1, ?, 0, 'completed')",
            (pid, title, content),
        )
        return cur.lastrowid


def test_update_section_persists_word_count():
    init_db()
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO projects (name, industry) VALUES (?, '通用')",
            ("BUG-C-字数持久化",),
        )
        pid = cur.lastrowid
    sid = _seed_section(pid, "章一")

    text = "这是一段用于验证字数持久化的中文正文，包含标点和数字 12345。" * 4
    result = sections_service.update_section(sid, SectionUpdate(content=text))
    assert result is not None
    # 返回对象与 DB 列必须同源同值（旧 bug：只有返回对象是新的）
    assert result.word_count == _count_words(text)
    with get_db() as conn:
        row = conn.execute("SELECT word_count FROM sections WHERE id = ?", (sid,)).fetchone()
    assert row["word_count"] == _count_words(text), (
        "DB word_count 列未随 content 更新持久化（BUG-C 回归）"
    )
    assert row["word_count"] > 0


def test_update_section_without_content_keeps_word_count():
    """只改标题等非正文字段时，不得误触 word_count 重写。"""
    init_db()
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO projects (name, industry) VALUES (?, '通用')",
            ("BUG-C-非正文更新",),
        )
        pid = cur.lastrowid
    sid = _seed_section(pid, "章一", "三百字正文" * 100)
    sections_service.update_section(sid, SectionUpdate(content="三百字正文" * 100))
    with get_db() as conn:
        before = conn.execute("SELECT word_count FROM sections WHERE id = ?", (sid,)).fetchone()
    sections_service.update_section(sid, SectionUpdate(title="改名章"))
    with get_db() as conn:
        after = conn.execute("SELECT word_count, title FROM sections WHERE id = ?", (sid,)).fetchone()
    assert after["word_count"] == before["word_count"]
    assert "改名" in after["title"] or after["title"] == "改名章"


# ---------------------------------------------------------------------------
# BUG-D：编排提示词禁止对 JSON 字符串二次编码
# ---------------------------------------------------------------------------

def test_orchestration_scoring_standards_not_double_encoded():
    region = _generate_region()
    assert "scoring_standards=json.dumps(scoring_points" not in region, (
        "scoring_points 已是 JSON 字符串，json.dumps 二次编码会破坏提示词（BUG-D 回归）"
    )
    assert 'scoring_standards=scoring_points or ""' in region


# ---------------------------------------------------------------------------
# 遗留问题1：死配置 SSE_TIMEOUT_CONTENT 移除（正文生成已改动态超时）
# ---------------------------------------------------------------------------

def test_dead_config_sse_timeout_content_removed():
    from app import config as app_config

    assert not hasattr(app_config, "SSE_TIMEOUT_CONTENT"), (
        "SSE_TIMEOUT_CONTENT 已无代码读取，应从 config 移除（遗留问题1）"
    )
    # 目录生成侧配置保留（不在本次模块范围）
    assert hasattr(app_config, "SSE_TIMEOUT_OUTLINE")


# ---------------------------------------------------------------------------
# 遗留问题2钉死：“1）”前缀标题优先于列表的既定取舍 + 导出编号不丢（BUG-E）
# ---------------------------------------------------------------------------

def test_paren_digit_parsed_as_l6_heading_not_list():
    """钉死审计定性："1）xxx" 解析为 L6 标题（七级编号标准），text 去编号、
    raw_line 保留原始整行；真 bullet 列表走 list_bullet 不受影响。

    2026-10-07 D2 契约更新：列表块由「list_item + 硬编码符号」改为
    「list_ordered / list_bullet 类型化符号」，导出端按类型写真正的 Word
    编号（numPr bullet/decimal），因此 bullet 文本不再携带 "• " 前缀
    （export.py L1230/L1745 已兼容三种类型）。"""
    from app.services.content_cleaning import parse_content

    blocks = parse_content("1）施工组织设计\na、人员部署\n- 机械配置")
    types = [b.type for b in blocks]
    assert types == ["heading6", "heading7", "list_bullet"], f"既定解析行为漂移: {types}"
    assert blocks[0].text == "施工组织设计"
    assert blocks[0].raw_line == "1）施工组织设计", "raw_line 必须保留原始编号行"
    assert blocks[2].text == "机械配置", "D2 后 bullet 符号由 Word 编号承担，文本不带前缀"


def test_export_l5_plus_keeps_numbering():
    """BUG-E 回归：导出文档 L5+ 标题必须带原始编号前缀（旧实现丢失 1）/a、）。"""
    from docx import Document

    from app.models import ExportConfig
    from app.services.export import _add_parsed_content

    doc = Document()
    config = ExportConfig(font_body="宋体", size_body=12)
    _add_parsed_content(
        doc, "1）施工组织设计\na、人员部署\n- 机械配置", config, base_level=0,
    )
    texts = [p.text for p in doc.paragraphs if p.text.strip()]
    assert any("1）施工组织设计" in t for t in texts), f"L6 编号前缀丢失: {texts}"
    assert any("a、人员部署" in t for t in texts), f"L7 编号前缀丢失: {texts}"
    assert any("机械配置" in t for t in texts)


# ---------------------------------------------------------------------------
# 遗留问题3：expand 端点断线转后台续跑（对齐 generate 契约）
# ---------------------------------------------------------------------------

def test_expand_disconnect_keeps_task_in_background():
    """expand_content_impl 源码不变量：
    ① 无可执行断线→cancel_event.set()；
    ② 三处断线路径均置 client_disconnected；
    ③ finally 有后台保留分支（等待协程 + 排空队列 + 延后清理）。
    """
    from app.routers.sse import sse_content

    src = inspect.getsource(sse_content)
    start = src.index("async def expand_content_impl")
    end = src.rindex("return StreamingResponse", start)
    region = src[start:end]

    offenders = [ln for ln in region.splitlines()
                 if ln.strip() and not ln.strip().startswith("#")
                 and "cancel_event.set()" in ln]
    assert not offenders, f"扩写断线不应杀死任务: {offenders}"

    assert region.count("client_disconnected = True") >= 3, "三处断线路径需都置标志"
    assert "_keep_running" in region
    assert "_await_expand_then_cleanup" in region
    assert "_drain_expand_queue" in region
    assert "asyncio.shield(expand_task)" in region, "后台等待不得误伤任务本体"
    # 清理（含任务组锁释放）必须抽到延后执行的 _cleanup_after_expand 内，
    # 而非在 finally 中 inline 释放（否则断线续跑期间锁提前释放，
    # 正文生成流水线可插队并发覆写同一批 sections）。
    assert "async def _cleanup_after_expand" in region
    assert "if _keep_running:" in region
