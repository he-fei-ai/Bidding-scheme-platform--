"""AI 工作流 facade 模块（自动生成）

本文件由 scripts/split_workflows.py 拆分生成。
真实代码已分散到 workflows_*.py 子模块，此处仅保留 import 区与
全部符号的 re-export，以兼容既有 `from .workflows import X` 调用。
"""

# ruff: noqa: F401

from ...utils.json_parser import extract_json_from_text as parse_json_response
from .outline_common import (
    FIXED_28_CHAPTERS,
    _classify_chapter_type,
    _count_outline_nodes,
    _ensure_min_children,
    _generate_diverse_l2_title,
    _get_template_list_json,
    build_chapter7_framework,
    build_framework_for_chapter,
    generate_nodes_distribution,
    sanitize_framework_titles,
)
from .workflows_analysis import normalize_writing_targets
from .workflows_base import (
    _adaptive_controller,
    _fill_template,
    _get_heading_spec,
    _normalize_outline_list_response,
    _safe_asyncio_run,
    collect_json_response,
    with_timeout,
)
from .workflows_outline_utils import (
    _L2_TITLE_PATTERNS,
    _L3_TITLE_PATTERNS,
    normalize_outline_items,
    validate_outline_business,
)
from .workflows_outline_v51 import (
    _auto_fix_issues,
    _call_ai_v51,
    _fix_ids_v51,
    _generate_diverse_l3_title,
    _generate_level1_v51,
    _generate_level2_v51,
    _generate_level3_v51,
    _quick_local_check,
    _self_check_v51,
    # BUG-FIX 2026-09-18（P2）：移除对死代码 _validate_level1_v51 的 re-export（已删）
    _wait_for_v51_review,
    generate_outline_direct,
    generate_outline_v51,
    should_use_direct_outline,
)
from .workflows_quality import review_outline, review_outline_with_autofix


# generate_outline → alias to generate_outline_v51 (V5.1 is now the only path)
generate_outline = generate_outline_v51


def _is_outline_complete(outline: list) -> bool:
    """检查目录是否完整（所有 L1/L2/L3 节点都有非空标题）"""
    if not outline:
        return False
    for l1 in outline:
        if not l1.get("title", "").strip():
            return False
        for l2 in l1.get("children", []):
            if not l2.get("title", "").strip():
                return False
            for l3 in l2.get("children", []):
                if not l3.get("title", "").strip():
                    return False
    return True
from .workflows_analysis import _extract_array_from_response

# BUG-FIX 2026-10-08（P0·后生成一致性审计整条链路失效）：此前本 facade 只
# re-export 了 `estimate_total_words_from_probe`，**漏掉了 `consistency_audit_content`**。
# 而 `routers/sse/sse_content.py`（正文生成 SSE 的后生成审计块）恰恰是
# `from ...services.ai.workflows import consistency_audit_content` —— 于是每次
# 带 `use_consistency_audit=true` 生成正文，走到该分支必然抛 ImportError，
# 又被外层裸 `except Exception` 吞成一条 `logger.warning("一致性审计失败")`。
# 后果：**用户勾选了「全文一致性审计」，实际一次都没跑过，且全程无任何用户可见报错**
# （SSE 仍正常推 completed，退出码 0）。已复现：ImportError: cannot import name
# 'consistency_audit_content' from 'app.services.ai.workflows'。
# 修复：补齐 re-export。
from .workflows_expand import consistency_audit_content, estimate_total_words_from_probe
from .workflows_sections import (
    SectionContentCache,
    _section_content_cache,
    extract_section_charts,
    generate_section_content,
    generate_sections,
    orchestrate_content,
)


# ---------------------------------------------------------------------------
# Legacy V2/V3 stubs — workflows_outline_v2v3.py 已物理删除。
# 这些符号曾由 V3 legacy 路径使用，现统一走 V5.1 唯一入口 generate_outline()。
# 保留 stub 定义以防老代码/测试在 import 时崩溃。
# ---------------------------------------------------------------------------

def _legacy_stub(name: str):
    def _stub(*args, **kwargs):
        raise NotImplementedError(
            f"{name} 来自已退役的 workflows_outline_v2v3，"
            f"请改用 workflows_outline_v51.generate_outline()"
        )
    return _stub


generate_outline_v3 = _legacy_stub("generate_outline_v3")
fill_children_with_retry = _legacy_stub("fill_children_with_retry")
generate_supplement_chapters = _legacy_stub("generate_supplement_chapters")
mark_irrelevant_chapters = _legacy_stub("mark_irrelevant_chapters")
merge_outline_with_supplements = _legacy_stub("merge_outline_with_supplements")
smart_merge_outline = _legacy_stub("smart_merge_outline")
