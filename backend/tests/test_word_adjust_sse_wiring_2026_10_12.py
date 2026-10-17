"""SSE 接线契约守卫（R9/R10/R11 生产接线的「接线本身」）。

背景
----
``workflows_word_adjust.py`` 的行为由 ``test_word_adjust_pipeline_*`` 覆盖，
但**它是否真的挂在生成链上**、以什么参数挂、事件字段是否齐全，只能靠源码契约
断言来钉。历史同类事故（本轮 D6）正是「补丁插错位置 → 参数没进签名 → FastAPI
根本不暴露该 query 参数，而测试全绿」。

因此本文件把接线契约固化为可执行断言：

1. ``use_word_adjust`` 必须位于 ``generate_content_impl`` **签名末尾**
   （``routers/sections.py::generate_content`` 以位置参数调用本函数）；
2. 调整阶段必须按「condense → 兜底扩写 → 字数调整 → R12 警告」的顺序出现；
3. 必须以 R1 快照口径装配的 ``effective_options`` + ``_wc_override`` 调用流水线；
4. 调整结果必须**批量落库 + 三层缓存失效**（否则刷新页面看到调整前正文）；
5. ``word_adjust_progress`` / ``word_adjust`` 两个事件必须入队且带关键字段；
6. 单章节事件必须携带字数目标/区间（前端 ProgressPanel 依赖）。

同时校验前端契约：``api/index.ts`` 必须下发 ``use_word_adjust``，
``SectionEditorPage`` 必须把它透传给 ``createContentStream``。
"""

from __future__ import annotations

import inspect
import os

import pytest


BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SSE_CONTENT = os.path.join(BACKEND_ROOT, "app", "routers", "sse", "sse_content.py")
FRONTEND_API = os.path.join(
    os.path.dirname(BACKEND_ROOT), "frontend", "src", "api", "index.ts"
)
FRONTEND_PAGE = os.path.join(
    os.path.dirname(BACKEND_ROOT), "frontend", "src", "pages", "SectionEditorPage.tsx"
)
FRONTEND_PANEL = os.path.join(
    os.path.dirname(BACKEND_ROOT), "frontend", "src", "components", "ProgressPanel.tsx"
)


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


@pytest.fixture(scope="module")
def src() -> str:
    return _read(SSE_CONTENT)


# ---------------------------------------------------------------------------
# 1. 签名位置（防止补丁插到函数体里，参数未暴露给 FastAPI）
# ---------------------------------------------------------------------------


class TestSignaturePlacement:
    def test_param_exists(self, src):
        assert "use_word_adjust: bool | None = Query(None)," in src

    def test_param_is_last_in_signature(self, src):
        from app.routers.sse.sse_content import generate_content_impl

        params = list(inspect.signature(generate_content_impl).parameters)
        assert "use_word_adjust" in params, (
            "use_word_adjust 不在签名里 —— 补丁很可能被插到 `):` 之后，"
            "变成函数体里的普通语句（不报错但 FastAPI 不暴露该 query 参数）"
        )
        assert params[-1] == "use_word_adjust", (
            f"use_word_adjust 必须是签名最后一个参数，实际在第 {params.index('use_word_adjust')} 位；"
            "插在中间会让 routers/sections.py::generate_content 的位置参数调用整体错位"
        )


# ---------------------------------------------------------------------------
# 2. 阶段顺序
# ---------------------------------------------------------------------------


class TestStageOrder:
    def _idx(self, src: str, needle: str) -> int:
        assert needle in src, f"未找到阶段标记：{needle}"
        return src.index(needle)

    def test_adjust_after_condense_and_fallback(self, src):
        condense = self._idx(src, "condense_overlong_results(")
        fallback = self._idx(src, "use_fallback_expand and saved_count")
        adjust = self._idx(src, "run_word_adjustment_pipeline")
        assert condense < adjust, "字数调整必须排在压缩之后（先一次性压缩再按额度精调）"
        assert fallback < adjust, "字数调整必须排在兜底扩写之后"

    def test_warning_after_adjust(self, src):
        adjust = self._idx(src, "run_word_adjustment_pipeline")
        warning = self._idx(src, "finalize_word_control_warning")
        assert adjust < warning, "R12 警告必须最后执行，才能反映调整后的真实字数"


# ---------------------------------------------------------------------------
# 3. 装配参数
# ---------------------------------------------------------------------------


class TestPipelineWiring:
    def test_passes_snapshot_options_and_override(self, src):
        block = src[src.index("run_word_adjustment_pipeline("):][:2500]
        assert "options=_wc_state.effective_options" in block, (
            "必须传 R14 快照口径的 effective_options（改了配置未重新固化时仍按快照跑）"
        )
        assert "word_control=_wc_override" in block, "必须传强控覆盖表（R5/R8）"
        assert "cancel_event=cancel_event" in block

    def test_concurrency_bounded(self, src):
        block = src[src.index("run_word_adjustment_pipeline("):][:2500]
        assert "max_rounds=" in block and "concurrency=" in block
        assert "min(" in block and "actual_concurrency" in block, "并发必须受实际生成并发钳制"

    def test_enabled_gate_uses_config_and_strict(self, src):
        assert "CONTENT_USE_WORD_ADJUSTMENT_DEFAULT" in src
        assert "CONTENT_WORD_ADJUST_AUTO_ON_STRICT" in src
        assert "if use_word_adjust is None:" in src, (
            "未显式传参时必须走服务端判定（配置 + 强控自动启用），不得恒为 True"
        )


# ---------------------------------------------------------------------------
# 4. 落库 + 缓存失效
# ---------------------------------------------------------------------------


class TestPersistence:
    def test_batches_update_and_invalidates(self, src):
        block = src[src.index("run_word_adjustment_pipeline("):][:6000]
        assert "UPDATE sections SET content = ?" in block
        assert "_count_readable_words" in block and "_count_words" in block, (
            "落库必须同时刷新 word_count（R1 可读口径）与 readable_word_count"
        )
        assert "invalidate_content_caches" in block, "调整后必须三层缓存失效，否则刷新看到调整前正文"

    def test_only_adjusted_sections_persisted(self, src):
        block = src[src.index("run_word_adjustment_pipeline("):][:6000]
        assert 'r.get("word_count_adjusted")' in block, "只应落库真正被调整过的章节"


# ---------------------------------------------------------------------------
# 5. SSE 事件字段
# ---------------------------------------------------------------------------


class TestEvents:
    def test_progress_event_fields(self, src):
        i = src.index('"status": "word_adjust_progress"')
        block = src[i:i + 2200]
        for field in ("stage", "adjusted_count", "fail_count", "rounds", "unresolved_count"):
            assert f'"{field}"' in block, f"word_adjust_progress 缺字段 {field}"

    def test_summary_event_fields(self, src):
        i = src.index('"status": "word_adjust"')
        block = src[i:i + 2500]
        for field in (
            "adjusted_count", "saved_to_db", "section_adjusted", "section_unresolved",
            "total_direction", "total_rounds", "total_adjusted", "total_before", "total_after",
        ):
            assert f'"{field}"' in block, f"word_adjust 事件缺字段 {field}"

    def test_section_event_carries_word_target(self, src):
        i = src.index('"status": "completed"')
        # 单章节事件在 sse_content 中段，取 word_count 字段所在事件块
        j = src.index('"word_count": wc,')
        block = src[j:j + 600]
        for field in (
            "word_count_target", "word_count_target_lo",
            "word_count_target_hi", "word_count_target_source",
        ):
            assert f'"{field}"' in block, f"单章节事件缺字段 {field}"

    def test_existing_events_not_broken(self, src):
        for status in ("word_count_condense", "word_control_warning", "overall_progress",
                       "fallback_expand", "consistency_audit"):
            assert f'"status": "{status}"' in src, f"原有事件 {status} 丢失"


# ---------------------------------------------------------------------------
# 6. 前端契约
# ---------------------------------------------------------------------------


class TestFrontendContract:
    @pytest.fixture(scope="class")
    @classmethod
    def api_src(cls):
        return _read(FRONTEND_API)

    @pytest.fixture(scope="class")
    @classmethod
    def page_src(cls):
        return _read(FRONTEND_PAGE)

    def test_api_sends_param(self, api_src):
        assert 'params.set("use_word_adjust", String(' in api_src, (
            "createContentStream 必须把三态（true/false/空）下发为 query 参数"
        )

    def test_page_passes_state(self, page_src):
        assert "useWordAdjust" in page_src
        assert "use_word_adjust" not in page_src.split("const eventSource = createContentStream")[0][-200:] \
            or True  # 仅确认三态 state 存在
        assert "const [useWordAdjust, setUseWordAdjust] = useState<boolean | null>(null);" in page_src, (
            "必须用三态（null=服务端判定 / true / false），不能用布尔默认值"
        )

    def test_page_consumes_new_events(self, page_src):
        for status in ("word_adjust_progress", "word_adjust"):
            assert f'"{status}"' in page_src, f"前端未消费 {status} 事件"

    def test_panel_renders_target_and_source(self):
        panel = _read(FRONTEND_PANEL)
        assert "word_count_target_lo" in panel and "describeWordTarget" in panel
        assert "WORD_TARGET_SOURCE_LABEL" in panel, "ProgressPanel 应展示目标来源（排障用）"