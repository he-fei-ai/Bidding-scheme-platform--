"""易标字数控制运行时装配测试（R5/R8/R12/R14 的实际接线层）。

覆盖本次移植的集成面（纯函数 185 条测试见
``test_word_control_yibiao_2026_10_12.py``，此处不重复公式口径）：

- R14 门槛：配置了绝对目标但缺生效快照 → 拒绝生成；未配置不设门槛；
- R14 配对：正文链只认快照（改了配置未重新固化时按快照跑）；
- R5/R8 装配：强控覆盖表（S / 区间 / 倒推目标）+ 配置失配守卫；
- 提示词注入：强控话术覆盖档位话术、未命中回落既有档位话术（逐字兼容）；
- 压缩链：强控命中按 R1 可读口径与硬性上限触发，未命中回落档位逻辑；
- R12 收尾：结构化警告落库、强控偏差列回写、达标时清除上一轮警告。
"""

import asyncio
import json
import uuid

import pytest

from app.db import get_db
from app.services import word_control as wc
from app.services.word_control_runtime import (
    SNAPSHOT_GATE_MESSAGE,
    ProjectWordControl,
    build_section_override,
    finalize_word_control_warning,
    load_project_word_control,
    resolve_override,
)
from app.services.ai.workflows_sections import (
    build_chapter_content_messages,
    condense_overlong_results,
)


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def _mk_project() -> int:
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO projects (name) VALUES (?)",
            (f"wc-runtime-{uuid.uuid4().hex[:8]}",),
        )
        return cur.lastrowid


def _mk_section(project_id: int, title: str, level: int = 3, content: str = "") -> int:
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO sections (project_id, title, level, sort_order, status, content) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (project_id, title, level, 1, "completed" if content else "pending", content),
        )
        return cur.lastrowid


def _put_settings(
    project_id: int,
    *,
    minimum_words: int = 0,
    maximum_words: int = 0,
    section_words: int = 0,
    strict: bool = False,
    snapshot: dict | None = None,
) -> None:
    with get_db() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO word_control_settings (
                project_id, minimum_words, maximum_words, section_words,
                strict_section_words, outline_word_control_snapshot_json,
                snapshot_at, word_control_warning, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL,
                      datetime('now', 'localtime'), datetime('now', 'localtime'))
            """,
            (
                project_id, minimum_words, maximum_words, section_words,
                1 if strict else 0,
                json.dumps(snapshot, ensure_ascii=False) if snapshot else None,
                "2026-10-12 10:00:00" if snapshot else None,
            ),
        )


def _mk_override(sid, s=3000, g=0):
    return {sid: {"section_words": s, "lo": int(s * 0.8), "hi": int(s * 1.2),
                  "generation_target": g, "strict": True}}


def _text(msgs) -> str:
    return "\n".join(getattr(m, "content", str(m)) for m in msgs)


class _FakeProvider:
    def __init__(self, reply: str):
        self.reply = reply
        self.calls = 0

    async def chat(self, messages, **kwargs):  # noqa: ANN001
        self.calls += 1
        return self.reply


# ---------------------------------------------------------------------------
# R14 门槛与快照配对
# ---------------------------------------------------------------------------


class TestR14Gate:
    def test_unconfigured_project_passes_gate(self):
        pid = _mk_project()
        state = load_project_word_control(pid)
        assert state.configured is False
        assert state.gate_error() is None

    def test_configured_without_snapshot_blocks(self):
        pid = _mk_project()
        _put_settings(pid, minimum_words=8000, maximum_words=12000,
                      section_words=3000, strict=True, snapshot=None)
        state = load_project_word_control(pid)
        assert state.configured is True
        assert state.gate_error() == SNAPSHOT_GATE_MESSAGE
        assert state.strict_active is False

    def test_snapshot_present_passes_and_is_strict(self):
        pid = _mk_project()
        _put_settings(pid, maximum_words=12000, section_words=3000, strict=True,
                      snapshot={"section_words": 3000, "strict_section_words": True,
                                "maximum_words": 12000})
        state = load_project_word_control(pid)
        assert state.gate_error() is None
        assert state.strict_active is True

    @pytest.mark.parametrize("raw", ["", "{}", "null", "not-json"])
    def test_invalid_snapshot_text_blocks(self, raw):
        pid = _mk_project()
        _put_settings(pid, maximum_words=12000, section_words=3000, strict=True)
        with get_db() as conn:
            conn.execute(
                "UPDATE word_control_settings SET outline_word_control_snapshot_json = ? "
                "WHERE project_id = ?", (raw, pid),
            )
        state = load_project_word_control(pid)
        assert state.gate_error() == SNAPSHOT_GATE_MESSAGE

    def test_effective_options_prefer_snapshot_over_current(self):
        """R14 配对：改了配置未重新固化时，正文链必须仍按快照跑。"""
        pid = _mk_project()
        _put_settings(pid, section_words=2000, strict=True,   # 当前配置（新）
                      snapshot={"section_words": 3000,        # 生效快照（旧）
                                "strict_section_words": True})
        state = load_project_word_control(pid)
        assert state.options.section_words == 2000
        assert state.effective_options.section_words == 3000
        built = build_section_override(state, [{"id": 1, "level": 3}])
        assert built[1]["section_words"] == 3000
        assert (built[1]["lo"], built[1]["hi"]) == (2400, 3600)


# ---------------------------------------------------------------------------
# R5/R8 覆盖表装配
# ---------------------------------------------------------------------------


class TestOverrideAssembly:
    def test_not_strict_returns_empty(self):
        opts = wc.WordControlOptions(0, 120000, 3000, False)
        state = ProjectWordControl(1, opts, {"section_words": 3000,
                                             "strict_section_words": False}, "t")
        assert build_section_override(state, [{"id": 1, "level": 3}]) == {}

    def test_strict_builds_bounds_and_derived_target(self):
        opts = wc.WordControlOptions(0, 120000, 3000, True)
        state = ProjectWordControl(1, opts, {"section_words": 3000,
                                             "strict_section_words": True,
                                             "maximum_words": 120000}, "t")
        built = build_section_override(
            state, [{"id": i, "level": 3} for i in range(1, 41)]
        )
        assert len(built) == 40
        assert built[1]["lo"] == 2400 and built[1]["hi"] == 3600
        assert built[1]["generation_target"] == 2400  # floor(0.8*120000/40)

    def test_leaf_count_mismatch_guard_falls_back_to_section_words(self):
        """叶子数远低于配置区间时，倒推目标不得高于硬性上限（防矛盾提示词）。"""
        opts = wc.WordControlOptions(0, 120000, 3000, True)
        state = ProjectWordControl(1, opts, {"section_words": 3000,
                                             "strict_section_words": True,
                                             "maximum_words": 120000}, "t")
        built = build_section_override(state, [{"id": 1, "level": 3}])
        assert built[1]["generation_target"] == 3000
        assert built[1]["generation_target"] <= built[1]["hi"]

    def test_parent_levels_excluded_from_pool(self):
        opts = wc.WordControlOptions(0, 120000, 3000, True)
        state = ProjectWordControl(1, opts, {"section_words": 3000,
                                             "strict_section_words": True,
                                             "maximum_words": 120000}, "t")
        built = build_section_override(
            state,
            [{"id": 1, "level": 1}, {"id": 2, "level": 2}, {"id": 3, "level": 3}],
        )
        assert set(built) == {3}

    def test_resolve_override_missing_sid(self):
        ov = _mk_override(7)
        assert resolve_override(ov, {"id": 7}) is not None
        assert resolve_override(ov, {"id": 8}) is None
        assert resolve_override(None, {"id": 7}) is None


# ---------------------------------------------------------------------------
# 提示词注入（R5/R8/R13）
# ---------------------------------------------------------------------------


class TestPromptInjection:
    SEC = {"id": 101, "title": "施工部署", "level": 3, "is_l3_own": True}

    def test_strict_wording_overrides_tier(self):
        text = _text(build_chapter_content_messages(
            self.SEC, word_control=_mk_override(101), writing_length="standard",
        ))
        assert "【强控字数红线】" in text
        assert "强控·每节目标 3000 字" in text
        assert "约 3000 字左右（硬性上限 3600 字，最低 2400 字）" in text
        assert wc.SECTION_WORD_CONTENT_PRESERVATION_CLAUSE in text  # R13
        assert "L3 自身正文" not in text  # 档位话术被强控盖过

    def test_derived_target_used_as_draft_anchor(self):
        text = _text(build_chapter_content_messages(
            self.SEC, word_control=_mk_override(101, g=2400),
        ))
        assert "约 2400 字左右（硬性上限 3600 字，最低 2400 字）" in text

    def test_tier_fallback_wording_preserved_without_override(self):
        text = _text(build_chapter_content_messages(
            self.SEC, word_control={}, writing_length="standard",
        ))
        assert "【强控字数红线】" not in text
        assert "【字数控制红线】" in text
        assert "L3 自身正文" in text

    def test_deep_level_strict_wording(self):
        sec = {"id": 102, "title": "深层叶子", "level": 4}
        text = _text(build_chapter_content_messages(sec, word_control=_mk_override(102)))
        assert "强控·每节目标 3000 字" in text
        assert "自身正文" in text

    @pytest.mark.parametrize("level", [1, 2])
    def test_parent_levels_not_injected(self, level):
        sec = {"id": 900 + level, "title": "父节点", "level": level}
        text = _text(build_chapter_content_messages(
            sec, word_control=_mk_override(sec["id"]),
        ))
        assert "【强控字数红线】" not in text


# ---------------------------------------------------------------------------
# 压缩链（R5 硬性上限触发 / R1 可读口径）
# ---------------------------------------------------------------------------


class TestCondenseOverride:
    SEC = {1: {"id": 1, "title": "施工部署", "level": 3, "is_l3_own": True}}

    @pytest.fixture(autouse=True)
    def _stub_condense_prompt(self, monkeypatch):
        import app.services.ai.prompts._cache as pc

        monkeypatch.setattr(
            pc, "get_prompt",
            lambda name: "压缩系统提示词" if name == "CONTENT_CONDENSE_SYSTEM" else None,
            raising=False,
        )

    def _run(self, coro):
        return asyncio.run(coro)

    def test_readable_above_hi_triggers_with_target_s(self):
        long_text = "施工部署" * 1250  # 可读 5000 > hi 3600
        results = [{"id": 1, "title": "施工部署", "status": "completed",
                    "content": long_text}]
        prov = _FakeProvider("施工部署" * 750)  # 3000
        summary = self._run(condense_overlong_results(
            results, self.SEC, prov, "standard", word_control=_mk_override(1),
        ))
        assert summary["condensed_count"] == 1
        assert results[0]["word_count_target"] == 3000
        assert results[0]["word_count_deviation"] == 0

    def test_at_hard_cap_not_triggered(self):
        """可读 == 1.2S 未超上限 → 不压缩；档位阈值 1.15S 本会触发，证明覆盖优先。"""
        results = [{"id": 1, "title": "施工部署", "status": "completed",
                    "content": "施工部署" * 900}]  # 3600
        prov = _FakeProvider("施工部署" * 750)
        summary = self._run(condense_overlong_results(
            results, self.SEC, prov, "standard", word_control=_mk_override(1),
        ))
        assert summary["condensed_count"] == 0
        assert prov.calls == 0

    def test_tier_fallback_unchanged_without_override(self):
        results = [{"id": 1, "title": "施工部署", "status": "completed",
                    "content": "施工部署" * 500}]
        prov = _FakeProvider("施工部署" * 75)  # 300，L3 自身档 ±20% 内
        summary = self._run(condense_overlong_results(
            results, self.SEC, prov, "standard", word_control={},
        ))
        assert summary["condensed_count"] == 1
        assert results[0]["word_count_target"] == 300


# ---------------------------------------------------------------------------
# R12 收尾警告与偏差回写
# ---------------------------------------------------------------------------


class TestFinalizeWarning:
    def _state(self, pid: int) -> ProjectWordControl:
        return load_project_word_control(pid)

    def test_warning_persisted_and_deviations_written(self):
        pid = _mk_project()
        snap = {"section_words": 3000, "strict_section_words": True,
                "maximum_words": 12000}
        _put_settings(pid, maximum_words=12000, section_words=3000,
                      strict=True, snapshot=snap)
        sid1 = _mk_section(pid, "A", content="施" * 4000)  # 越上限 3600
        sid2 = _mk_section(pid, "B", content="施" * 3100)  # 达标

        warning = finalize_word_control_warning(
            self._state(pid),
            [
                {"id": sid1, "title": "A", "status": "completed", "content": "施" * 4000},
                {"id": sid2, "title": "B", "status": "completed", "content": "施" * 3100},
            ],
            single_section=False,
        )
        assert warning is not None
        assert warning["sections"]["out_of_range_count"] == 1
        assert warning["sections"]["items"][0]["id"] == sid1
        assert warning["total"]["actual"] == 7100

        with get_db(readonly=True) as conn:
            row = conn.execute(
                "SELECT word_control_warning FROM word_control_settings "
                "WHERE project_id = ?", (pid,),
            ).fetchone()
            stored = json.loads(row["word_control_warning"])
            assert stored["sections"]["out_of_range_count"] == 1

            dev1 = conn.execute(
                "SELECT word_count_target, word_count_deviation FROM sections "
                "WHERE id = ?", (sid1,),
            ).fetchone()
            assert dev1["word_count_target"] == 3000
            assert dev1["word_count_deviation"] == 1000

    def test_compliant_run_clears_previous_warning(self):
        pid = _mk_project()
        snap = {"section_words": 3000, "strict_section_words": True,
                "maximum_words": 12000}
        _put_settings(pid, maximum_words=12000, section_words=3000,
                      strict=True, snapshot=snap)
        with get_db() as conn:
            conn.execute(
                "UPDATE word_control_settings SET word_control_warning = ? "
                "WHERE project_id = ?", ('{"kind": "content"}', pid),
            )
        sid = _mk_section(pid, "A", content="施" * 3000)

        warning = finalize_word_control_warning(
            self._state(pid),
            [{"id": sid, "title": "A", "status": "completed", "content": "施" * 3000}],
        )
        assert warning is None
        with get_db(readonly=True) as conn:
            row = conn.execute(
                "SELECT word_control_warning FROM word_control_settings "
                "WHERE project_id = ?", (pid,),
            ).fetchone()
            assert row["word_control_warning"] is None

    def test_unconfigured_returns_none(self):
        pid = _mk_project()
        assert finalize_word_control_warning(self._state(pid), []) is None

    def test_skips_uncompleted_results(self):
        pid = _mk_project()
        snap = {"section_words": 3000, "strict_section_words": True,
                "minimum_words": 8000, "maximum_words": 12000}
        _put_settings(pid, minimum_words=8000, maximum_words=12000,
                      section_words=3000, strict=True, snapshot=snap)
        warning = finalize_word_control_warning(
            self._state(pid),
            [{"id": 1, "title": "A", "status": "failed", "content": "施" * 100}],
        )
        assert warning is None  # 失败章节不计入，无任何实际字数 → 不判定
