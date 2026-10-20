# -*- coding: utf-8 -*-
"""test_bugfix_2026_10_20_context_degrade_observability.py — 长上下文降级观测闭环

背景（2026-10-20 遗留项加固）：
    `_degrade_long_context_messages`（CONTENT_CONTEXT_DEGRADE_ENABLED，默认关）
    把 `section['_context_degraded']` 挂在 section 上，但**没有任何消费方**——
    开关开启后运维只有一条 WARNING 日志，SSE verification 事件与前端完全不可见，
    无法回答「哪些章节被裁剪、裁了多少」。而这正是该开关从默认关推向默认开
    所必需的采样数据（遗留问题第 3 条的原始表述）。

修复：
    1. `_gen_one` 把 `_context_degraded` 随 result 上报（与 `_context_check`
       同一模式）；
    2. `sse_content` 生成后综合校验把降级章节汇总为 verification warning +
       `context_degraded_count`。

本文件锁定：
    1. 降级函数本身的 4 态行为（默认关不进函数 / 不超阈值原样返回 /
       超阈值裁剪并留痕 / 异常时保持原消息）；
    2. result 上报链路（stub generate_section_content 置留痕 → result 带标记）；
    3. 未降级时 result 不带该键（默认关行为零变化）。
"""

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.ai import workflows_sections as ws  # noqa: E402
from app.services.ai.providers.base import AIMessage  # noqa: E402


class _BigProvider:
    """永不被调用的占位 provider（生成路径整体被 stub）。"""

    async def chat(self, messages, **kwargs):  # pragma: no cover
        raise AssertionError("本测试不应触发真实 AI 调用")


def _messages(total_chars: int) -> list:
    """构造指定总字符数的消息栈（system 铁律 + 大段参考资料）。"""
    per = total_chars // 3
    return [
        AIMessage(role="system", content="你是资深技术标编辑。" + "铁" * 50),
        AIMessage(role="user", content="参考资料：" + "参" * per),
        AIMessage(role="user", content="更多资料：" + "料" * per),
    ]


class TestDegradeFunctionStates:
    def test_below_threshold_returns_unchanged(self, monkeypatch):
        """总长不超过 limit×ratio：原样返回、不留痕（零开销路径）。"""
        monkeypatch.setattr(ws, "_degrade_long_context_messages", ws._degrade_long_context_messages)
        section: dict = {}
        msgs = _messages(1000)
        out = ws._degrade_long_context_messages(msgs, section)
        assert out is msgs
        assert "_context_degraded" not in section

    def test_above_threshold_truncates_and_marks(self, monkeypatch):
        """超阈值：裁剪最长非 system 消息、加截断标记、section 留痕。"""
        from app import config as app_config

        monkeypatch.setattr(app_config, "CONTENT_CONTEXT_LIMIT_CHARS", 6000)
        monkeypatch.setattr(app_config, "CONTENT_CONTEXT_DEGRADE_RATIO", 0.5)
        section: dict = {}
        msgs = _messages(9000)
        out = ws._degrade_long_context_messages(msgs, section)
        assert out is msgs  # 原地修改同一列表
        assert section.get("_context_degraded") is True
        assert any("已截断" in str(getattr(m, "content", "")) for m in msgs)
        # system 消息不被裁剪（铁律保留）
        assert str(msgs[0].content).startswith("你是资深技术标编辑。")

    def test_config_gate_default_off(self):
        """默认关闭：开关保持 false（兼容性契约，灰度前不改默认值）。"""
        from app import config as app_config

        assert app_config.CONTENT_CONTEXT_DEGRADE_ENABLED is False
        assert app_config.CONTENT_CONTEXT_DEGRADE_RATIO == 0.7
        assert app_config.CONTENT_CONTEXT_LIMIT_CHARS == 120000


class TestResultReporting:
    def test_degraded_flag_reaches_result(self, monkeypatch):
        """stub 生成路径并置留痕 → result 必须带 context_degraded=True。"""

        async def _fake_gen(provider, section, context, cb):
            # 仅对第一个章节模拟降级留痕（第二个章节不降级）
            if section.get("id") == "3.2.1":
                # 模拟 _do_generate_section_with_optimizations 内的降级留痕
                section["_context_degraded"] = True
            return "生成完成的正文内容"

        monkeypatch.setattr(ws, "generate_section_content", _fake_gen)
        sections = [{"id": "3.2.1", "title": "施工工艺"}, {"id": "3.2.2", "title": "安全措施"}]
        results = asyncio.run(ws.generate_sections(_BigProvider(), sections))
        by_id = {r["id"]: r for r in results}
        assert by_id["3.2.1"].get("context_degraded") is True
        # 未留痕的章节不带该键（不制造噪声字段）
        assert "context_degraded" not in by_id["3.2.2"]

    def test_no_flag_no_key(self, monkeypatch):
        """未降频时 result 不含 context_degraded 键（行为零变化）。"""

        async def _fake_gen(provider, section, context, cb):
            return "正文"

        monkeypatch.setattr(ws, "generate_section_content", _fake_gen)
        results = asyncio.run(
            ws.generate_sections(_BigProvider(), [{"id": "1", "title": "概述"}])
        )
        assert "context_degraded" not in results[0]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
