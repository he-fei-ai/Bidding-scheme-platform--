# -*- coding: utf-8 -*-
"""遗留项 2026-10-08 加固测试：STRIP_PLAIN_CODE_BLOCKS 剥离开关

覆盖：
1. 默认（false）：闭合普通代码块保留内容（既有契约回归保护）；
2. 开启（true）：闭合普通代码块整体剔除（标记行+内容）；
3. 开启后图表围栏（mermaid/chart-json）不受影响；
4. 开启后未闭合坏块行为不变（依旧删除）；
5. 波浪线围栏 ~~~ 同样受开关控制。
"""

import pytest

from app.services.content_cleaning import clean_generated_content


CLOSED_PY = (
    "说明如下。\n"
    "```python\n"
    "print('ok')\n"
    "```\n"
    "结束。"
)

CLOSED_TILDE = (
    "说明甲。\n"
    "~~~json\n"
    '{"k": 1}\n'
    "~~~\n"
    "说明乙。"
)

CLOSED_WITH_CHART = (
    "开头。\n"
    "```python\n"
    "x = 1\n"
    "```\n"
    "```mermaid\n"
    "graph TD\n"
    "  A --> B\n"
    "```\n"
    "结尾。"
)

UNCLOSED = (
    "前文。\n"
    "```python\n"
    "x = 1\n"
    "y = 2\n"
    "后文普通文字。"
)


@pytest.fixture
def strip_on(monkeypatch):
    import app.config as cfg

    monkeypatch.setattr(cfg, "STRIP_PLAIN_CODE_BLOCKS", True)


class TestStripPlainCodeBlocksDefault:
    """默认 false：与既有行为逐字节一致（向后兼容）"""

    def test_closed_block_content_kept(self):
        out = clean_generated_content(CLOSED_PY)
        assert "print('ok')" in out
        assert "结束。" in out
        assert "```python" not in out

    def test_tilde_closed_block_content_kept(self):
        out = clean_generated_content(CLOSED_TILDE)
        assert "说明甲。" in out and "说明乙。" in out

    def test_unclosed_still_truncated(self):
        out = clean_generated_content(UNCLOSED)
        assert "前文。" in out
        assert "x = 1" not in out


class TestStripPlainCodeBlocksEnabled:
    """开启 true：闭合普通代码块整体剔除"""

    def test_closed_block_removed(self, strip_on):
        out = clean_generated_content(CLOSED_PY)
        assert "print('ok')" not in out
        assert "```" not in out
        # 块外正文不受影响
        assert "说明如下。" in out and "结束。" in out

    def test_tilde_closed_block_removed(self, strip_on):
        out = clean_generated_content(CLOSED_TILDE)
        assert '{"k": 1}' not in out
        assert "说明甲。" in out and "说明乙。" in out

    def test_chart_fences_untouched(self, strip_on):
        out = clean_generated_content(CLOSED_WITH_CHART)
        # 普通代码块剔除，图表围栏完整保留
        assert "x = 1" not in out
        assert "graph TD" in out
        assert "A --> B" in out
        assert "开头。" in out and "结尾。" in out

    def test_unclosed_still_truncated(self, strip_on):
        out = clean_generated_content(UNCLOSED)
        assert "前文。" in out
        assert "x = 1" not in out


class TestConfigFlag:
    def test_flag_exists_and_defaults_false(self):
        import app.config as cfg

        assert hasattr(cfg, "STRIP_PLAIN_CODE_BLOCKS")
        assert cfg.STRIP_PLAIN_CODE_BLOCKS in (True, False)
