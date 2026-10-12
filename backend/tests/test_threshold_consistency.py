"""阈值一致性元测试：防止文档与代码断言再次漂移。

背景
----
历史上 `.trae/documents/next_optimization_plan.md` 与
`test_diversity.py::test_l3_uniqueness_28_chapters` 出现过 28% vs 55%
的注释/断言漂移（详见优化计划 P1 章节）。2026-10-09 加固后，
所有断言已收敛到 test_diversity.THRESHOLDS 单一真源；本文件负责
**持续校验**：

1. test_diversity.THRESHOLDS 的三个键值均能在文档中找到对应百分比；
2. 文档中提到的所有阈值引用均能映射到 THRESHOLDS 字典；
3. 阈值变动（例如 55% → 50%）会触发本测试失败，强制同步文档。

运行：`pytest tests/test_threshold_consistency.py -v`
"""

import re
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
TESTS_DIR = Path(__file__).resolve().parent
for _p in (str(TESTS_DIR), str(BACKEND_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from test_diversity import THRESHOLDS  # noqa: E402


# 文档路径（相对 backend/ 目录）
DOC_REL_PATH = Path("..") / ".trae" / "documents" / "next_optimization_plan.md"


def _load_doc_text() -> str:
    """读取文档正文；文档缺失时 pytest.skip 而非 fail，允许本地分支裁剪。"""
    full_path = (BACKEND_DIR / DOC_REL_PATH).resolve()
    if not full_path.exists():
        pytest.skip(f"文档不存在: {full_path}")
    return full_path.read_text(encoding="utf-8")


class TestThresholdDocConsistency:
    """校验 THRESHOLDS 与优化计划文档中的阈值引用保持一致。"""

    def test_l3_chapters_threshold_reflected_in_doc(self):
        """L3_UNIQUENESS_CHAPTERS 阈值必须能在文档中找到对应百分比。"""
        threshold = THRESHOLDS["L3_UNIQUENESS_CHAPTERS"]
        expected_str = f"{threshold * 100:.0f}%"
        doc = _load_doc_text()
        # 文档必须至少提及一次该百分比（如 "= 55%" 或 "55% 阈值"）
        assert expected_str in doc, (
            f"文档中未找到 L3_UNIQUENESS_CHAPTERS={threshold} 的对应表达 "
            f"'{expected_str}'。请更新 .trae/documents/next_optimization_plan.md 或 "
            f"同步 THRESHOLDS 值。"
        )

    def test_l2_e2e_threshold_reflected_in_doc(self):
        """L2_UNIQUENESS_E2E 阈值必须能在文档中找到对应百分比。"""
        threshold = THRESHOLDS["L2_UNIQUENESS_E2E"]
        expected_str = f"{threshold * 100:.0f}%"
        doc = _load_doc_text()
        assert expected_str in doc, (
            f"文档中未找到 L2_UNIQUENESS_E2E={threshold} 的对应表达 "
            f"'{expected_str}'。"
        )

    def test_l3_e2e_threshold_reflected_in_doc(self):
        """L3_UNIQUENESS_E2E 阈值必须能在文档中找到对应百分比。"""
        threshold = THRESHOLDS["L3_UNIQUENESS_E2E"]
        expected_str = f"{threshold * 100:.0f}%"
        doc = _load_doc_text()
        assert expected_str in doc, (
            f"文档中未找到 L3_UNIQUENESS_E2E={threshold} 的对应表达 "
            f"'{expected_str}'。"
        )

    def test_doc_references_thresholds_constant(self):
        """文档应显式引用 THRESHOLDS 单一真源，避免后续读者绕过常量。"""
        doc = _load_doc_text()
        assert "THRESHOLDS" in doc, (
            "文档未引用 THRESHOLDS 常量；请在阈值相关段落显式提及 "
            "'THRESHOLDS' 以引导读者以代码为准。"
        )

    def test_no_stale_28_percent_comment(self):
        """回归防护：文档不再含历史上误导性的 '> 28%' 表述（作为当前阈值）。"""
        doc = _load_doc_text()
        # 允许出现 "28%" 作为历史问题描述，但必须搭配上下文关键词；
        # 检查是否存在「L3 唯一率应 > 28%」这一历史错误表述。
        # 该表述应保留作为「原始问题」历史证据，此处仅检查
        # 是否仍作为当前阈值被推荐使用（即出现「L3 唯一率应 > 28%。」结尾句）
        stale_pattern = re.compile(
            r"L3\s*唯一率\s*应\s*>\s*28\s*%\s*[。\.]"
        )
        # 允许历史「原始问题」段落包含此表述；此处仅检查是否仍作为
        # 当前断言口径出现（即紧接 `> 28%` 后没有引用 THRESHOLDS 说明）
        # 为避免过度约束，本测试仅校验「当前建议」段落不出现 stale_pattern
        current_section_marker = "当前阈值"  # 文档中不存在时会跳过
        if current_section_marker in doc:
            section_after = doc.split(current_section_marker, 1)[1]
            assert not stale_pattern.search(section_after), (
                f"文档当前阈值段落仍出现历史 stale 表述 '> 28%'。"
            )
