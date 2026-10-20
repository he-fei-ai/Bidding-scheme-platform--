# -*- coding: utf-8 -*-
"""test_bugfix_2026_10_20_illustration_html_degradation.py — 配图类型降级守卫

背景（2026-10-20 遗留项加固）：
    易标配图类型含第四类 "html"（AI 生成自包含 HTML 图表），本平台导出/渲染
    链路只消费 mermaid 围栏 / chart-json / AI 图片（`export.py` grep 证实无
    HTML→图片/Word 渲染能力）。若 "html" 经落库/外部链路进入正文生成，原实现
    `_ill_type == "mermaid"` / `== "ai"` 两分支均不命中 → 模型收不到配图意图，
    既无图也无告警（死数据）。

修复：`workflows_sections._degrade_unrenderable_illustration` 把已知不可渲染
    类型降级为语义最接近的可消费类型（html → ai）并告警；其余值（含
    generate-illustration 路由写入的图片风格串）原样返回，行为零变化。

本文件锁定：
    1. html（含大小写/空白变体）→ ai 且告警；
    2. 可消费类型与 None/风格串原样返回、不告警；
    3. 端到端：illustration_type="html" 时正文提示词确实拿到 AI 配图意图
       （降级不丢意图）。
"""

import logging
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.ai import workflows_sections as ws  # noqa: E402


class TestDegradeUnrenderableIllustration:
    def test_html_degrades_to_ai(self, caplog):
        with caplog.at_level(logging.WARNING, logger="app.services.ai.workflows_sections"):
            assert ws._degrade_unrenderable_illustration("html") == "ai"
        assert any("html" in r.message and "降级" in r.message for r in caplog.records)

    @pytest.mark.parametrize("variant", ["HTML", " html ", "Html", "\thtml\n"])
    def test_html_variants_normalized(self, variant):
        """大小写/空白变体同样降级（DB 自由 TEXT 列不保证规范大小写）。"""
        assert ws._degrade_unrenderable_illustration(variant) == "ai"

    @pytest.mark.parametrize("value", ["ai", "mermaid", "none", None, "", "engineering_diagram"])
    def test_consumable_and_style_values_unchanged(self, value, caplog):
        """可消费类型 / None / 图片风格串原样返回，且不产生告警噪声。"""
        with caplog.at_level(logging.WARNING, logger="app.services.ai.workflows_sections"):
            assert ws._degrade_unrenderable_illustration(value) == value
        assert not caplog.records

    def test_map_only_contains_known_unrenderable(self):
        """映射表只收已知不可渲染类型——防止未来误把可消费类型加进去。"""
        assert set(ws._UNRENDERABLE_ILLUSTRATION_MAP) == {"html"}
        assert ws._UNRENDERABLE_ILLUSTRATION_MAP["html"] == "ai"


class TestEndToEndPromptKeepsIntent:
    def _user_texts(self, messages):
        return [str(m.content) for m in messages if getattr(m, "role", "") == "user"]

    def test_html_illustration_produces_ai_figure_instruction(self):
        """端到端：illustration_type=html 时正文提示词注入 AI 配图意图。"""
        messages = ws.build_chapter_content_messages(
            {
                "id": "3.2.1",
                "title": "施工工艺流程",
                "plan": {},
                "illustration_type": "html",
            }
        )
        joined = "\n".join(self._user_texts(messages))
        # 编排决策块必须出现，且配图意图落在可消费的 AI 配图上
        assert "正文编排决策" in joined, "html 降级后编排决策块不应丢失"
        assert "AI配图: 计划配图" in joined, "html 降级为 ai 后，正文必须拿到配图意图"
        assert "HTML图" not in joined, "本平台无 HTML 渲染消费方，不得指示模型产出 HTML 图"

    def test_none_illustration_no_figure_instruction(self):
        """illustration_type=none 时不注入编排决策块（行为不变）。

        注：system 铁律里本就含通用图表说明（"需要图：Mermaid图…"），故只断言
        **编排决策块**不出现，不裸查 "Mermaid图" 字样。
        """
        messages = ws.build_chapter_content_messages(
            {"id": "3.2.2", "title": "安全措施", "plan": {}, "illustration_type": "none"}
        )
        joined = "\n".join(self._user_texts(messages))
        assert "正文编排决策" not in joined
        assert "AI配图: 计划配图" not in joined

    def test_empty_plan_with_ai_type_still_injects(self):
        """BUG-FIX 2026-10-20：plan 为空但类型已裁定为 ai 时，配图意图不丢失。"""
        messages = ws.build_chapter_content_messages(
            {"id": "3.2.3", "title": "总平面布置", "plan": {}, "illustration_type": "ai"}
        )
        joined = "\n".join(self._user_texts(messages))
        assert "AI配图: 计划配图" in joined


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
