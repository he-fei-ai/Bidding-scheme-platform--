"""V5.1 目录模块遗留问题加固验证（2026-10-08）。

覆盖本次对 §8 遗留问题的执行：
- 参考目录反漂移：REFERENCE_TEMPLATE_V51 由 FIXED_28_CHAPTERS 过滤派生（单一标题来源）。
- 配置可调：各阶段 AI 调用 max_tokens 暴露为 OUTLINE_MAX_TOKENS_*，默认值与历史行为一致。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config as app_config  # noqa: E402
from app.services.ai.outline_common import FIXED_28_CHAPTERS  # noqa: E402
from app.services.ai.workflows_outline_v51 import (  # noqa: E402
    REFERENCE_TEMPLATE_V51,
)


def test_outline_max_tokens_config_defaults_match_historical():
    """max_tokens 配置默认值必须与历史硬编码值一致（向后兼容，防止无意改动）。"""
    assert app_config.OUTLINE_MAX_TOKENS_DIRECT == 6000
    assert app_config.OUTLINE_MAX_TOKENS_L1 == 8192
    assert app_config.OUTLINE_MAX_TOKENS_L2 == 4096
    assert app_config.OUTLINE_MAX_TOKENS_SELFCHECK == 5000


def test_reference_template_derived_from_fixed_28():
    """23 章兜底模板必须完全来自 FIXED_28_CHAPTERS（单一标题来源，反漂移）。

    每项要么是 FIXED 原标题，要么是显式登记的改写；不得出现 FIXED 之外的陌生标题。
    """
    fixed_titles = {c["title"] for c in FIXED_28_CHAPTERS}
    v51_titles = [c["title"] for c in REFERENCE_TEMPLATE_V51]
    # 唯一允许的改写：id24 原标题 → 「与各参建方的配合协调」
    allowed_rewrite = {"对总包管理及专业分包配合方案／与各参建方配合": "与各参建方的配合协调"}

    for t in v51_titles:
        assert (t in fixed_titles) or (t in allowed_rewrite.values()), (
            f"REFERENCE_TEMPLATE_V51 出现 FIXED_28_CHAPTERS 之外的陌生标题：{t}"
        )

    # 长度与 id 规范
    assert len(v51_titles) == 23
    assert [c["id"] for c in REFERENCE_TEMPLATE_V51] == [f"ref_{i}" for i in range(1, 24)]
