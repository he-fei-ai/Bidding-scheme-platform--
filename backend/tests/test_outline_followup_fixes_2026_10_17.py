"""2026-10-17 生成大纲模块遗留项修复回归（L-1 ~ L-5）。

承接《报告_生成大纲模块第六轮深度加固_2026-10-17.md》§8 遗留问题清单，
按优先级全部落地：

  L-1（P3 · 离线泛化标题永久泄漏）：`_l3_specificity_closeout` 旧实现在
      `provider is None` 时**完全跳过修复**，泛化标题原样进入审核与落库。
      修复：`repair_l3_generic_titles` 新增 `use_ai` 参数（None=按 provider
      自动判别；False=纯规则兜底模式，跳过上下文组装与 AI 调用），
      closeout 改为有无 provider 都跑修复循环。

  L-2（P3 · flaky 测试）：`test_parse_task_updates_scoped_by_project` 等
      源码断言测试依赖 `inspect.getsource` 的 `__code__.co_firstlineno`
      元数据，全量套件跑完后元数据漂移 → 定位到错误偏移、返回其他函数的
      源码（不抛异常、断言静默失败）。修复：本文件与
      test_import_outline_fixes_2026_10_09.py 统一改用 `_func_src`
      直读源文件截取（零元数据依赖）。

  L-3（P3 · 并行 pytest 干扰）：中文路径 `__pycache__` 并发读写竞态
      （conftest 已记载）。修复：pyproject.toml pytest 段固化运行约束
      （单进程顺序执行）与根因说明；本测试守护该文档不丢失。

  L-4（P4 · 判重口径不完整）：`_title_core` 归一化缺前缀剥离，
      「（2）X」与「X」被判不同标题。修复：与后缀侧对称纳入
      `_strip_ordinal_prefix`，保证「判重口径」与「清洗口径」一致。

  L-5（P2 · 存量脏数据）：`_save_nodes` 递归落库**全部层级**，
      sections.title 里 L3+ 占位与 JSON 树一样多；而存量迁移的阶段 2
      只覆盖 JSON 列（docstring 里「sections.title 只存 L1/L2」的注释
      写于更早 schema，与实现不符）。修复：迁移新增阶段 3
      `_clean_sections_placeholder_titles`——按项目重建 sections 树，
      用生成链路同一函数清洗 L3+ 脏标题，幂等、只写回真实变化行。

运行：cd backend && python -m pytest tests/test_outline_followup_fixes_2026_10_17.py -q -p no:cacheprovider
"""

import json
import os
import re
import sqlite3
from pathlib import Path

import pytest

from app.services.ai.outline_common import (
    _title_core,
    _uniquify_outline_titles,
)

BACKEND_DIR = Path(__file__).resolve().parent.parent

# ===========================================================================
# L-1：离线（provider=None）也必须修复泛化三级标题
# ===========================================================================


def _generic_tree():
    """L3 含两个泛化标题 + 一个空标题 + 一个好标题。"""
    return [
        {
            "title": "第一章 总体施工部署",
            "level": 1,
            "children": [
                {
                    "title": "项目总体理解",
                    "level": 2,
                    "children": [
                        {"title": "施工准备工作", "level": 3},
                        {"title": "安全保障", "level": 3},
                        {"title": "", "level": 3},
                        {"title": "样板区质量控制", "level": 3},
                    ],
                }
            ],
        }
    ]


class _SpyProvider:
    """记录 chat 调用次数的假 provider。"""

    def __init__(self):
        self.calls = 0

    class _Msg:
        def __init__(self, role, content):
            self.role = role
            self.content = content

    async def chat(self, messages, **kwargs):
        self.calls += 1
        raise RuntimeError("spy: 不应被调用")


class TestL1OfflineRuleRepair:
    @pytest.mark.asyncio
    async def test_use_ai_false_never_calls_provider(self):
        """use_ai=False：一个 AI 调用都不打，泛化标题全部改写为好标题。"""
        from app.services.ai.workflows_outline_utils import (
            _is_dirty_l3_title_deep,
            repair_l3_generic_titles,
            validate_l3_specificity,
        )

        spy = _SpyProvider()
        out = await repair_l3_generic_titles(
            spy, _generic_tree(), "概述", "", "", "", "", use_ai=False,
        )
        assert spy.calls == 0, f"纯规则模式不得调用 AI，实际 {spy.calls} 次"
        titles = [c["title"] for c in out[0]["children"][0]["children"]]
        assert "施工准备工作" not in titles, f"泛化标题未修复: {titles}"
        assert "安全保障" not in titles, f"泛化标题未修复: {titles}"
        for t in titles:
            assert t and t.strip(), f"空标题未修复: {titles}"
            assert not _is_dirty_l3_title_deep(t), t
        # 好标题保持原样
        assert "样板区质量控制" in titles
        _, warns = validate_l3_specificity(out)
        assert not warns, f"修复后仍有泛化标题: {warns}"

    @pytest.mark.asyncio
    async def test_provider_none_auto_rule_mode(self):
        """provider=None（未显式传 use_ai）：自动进规则模式，不抛异常。"""
        from app.services.ai.workflows_outline_utils import (
            repair_l3_generic_titles,
            validate_l3_specificity,
        )

        out = await repair_l3_generic_titles(
            None, _generic_tree(), "概述", "", "", "", "",
        )
        titles = [c["title"] for c in out[0]["children"][0]["children"]]
        assert "施工准备工作" not in titles, f"provider=None 未走规则兜底: {titles}"
        _, warns = validate_l3_specificity(out)
        assert not warns, warns

    @pytest.mark.asyncio
    async def test_use_ai_true_with_none_provider_still_works(self):
        """向后兼容：显式 use_ai=True + provider=None 时不得崩溃
        （旧调用方式：AI 调用抛异常 → 既有规则兜底路径接管）。"""
        from app.services.ai.workflows_outline_utils import repair_l3_generic_titles

        out = await repair_l3_generic_titles(
            None, _generic_tree(), "概述", "", "", "", "", use_ai=True,
        )
        titles = [c["title"] for c in out[0]["children"][0]["children"]]
        assert all(t and t.strip() for t in titles), titles

    @pytest.mark.asyncio
    async def test_closeout_offline_repairs_generic_titles(self):
        """端到端：`_l3_specificity_closeout(None, tree)` 必须修复泛化标题。

        L-1 核心行为断言——旧实现在此分支完全跳过修复（只告警），
        泛化标题原样流出到审核与落库。
        """
        from app.services.ai.workflows_outline_v51 import _l3_specificity_closeout
        from app.services.ai.workflows_outline_utils import (
            _is_dirty_l3_title_deep,
            validate_l3_specificity,
        )

        tree = _generic_tree()
        out = await _l3_specificity_closeout(None, tree)
        titles = [c["title"] for c in out[0]["children"][0]["children"]]
        assert "施工准备工作" not in titles, f"离线收口未修复泛化标题: {titles}"
        assert "安全保障" not in titles, f"离线收口未修复泛化标题: {titles}"
        assert all(t and t.strip() for t in titles), f"空标题未修复: {titles}"
        for t in titles:
            assert not _is_dirty_l3_title_deep(t), t
        assert "样板区质量控制" in titles, "好标题被误改"

    @pytest.mark.asyncio
    async def test_closeout_offline_emits_phase_message(self):
        """离线修复必须向前端发阶段提示（用户可区分 AI 精化与规则兜底）。"""
        from app.services.ai.workflows_outline_v51 import _l3_specificity_closeout

        messages = []

        async def _cb(label, msg):
            messages.append(msg)

        await _l3_specificity_closeout(None, _generic_tree(), phase_callback=_cb)
        assert messages, "离线收口未发任何阶段提示"
        assert any("规则兜底" in m or "未配置" in m for m in messages), messages


# ===========================================================================
# L-4：_title_core 归一化纳入编号前缀（判重口径 = 清洗口径）
# ===========================================================================


class TestL4TitleCoreStripsPrefix:
    @pytest.mark.parametrize(
        "prefixed,plain",
        [
            ("（2）总体实施策划", "总体实施策划"),
            ("(3)质量保证措施", "质量保证措施"),
            ("（二）质量保证措施", "质量保证措施"),
            ("2、总体实施策划", "总体实施策划"),
            ("2. 总体实施策划", "总体实施策划"),
            ("（2）（3）总体实施策划", "总体实施策划"),
        ],
    )
    def test_prefix_does_not_affect_core(self, prefixed, plain):
        assert _title_core(prefixed) == _title_core(plain), (
            f"前缀未纳入归一化: {prefixed!r} != {plain!r}"
        )

    def test_suffix_and_prefix_both_stripped(self):
        assert _title_core("（2）总体实施策划（3）") == _title_core("总体实施策划")

    def test_uniquify_treats_prefixed_and_plain_as_same(self):
        """绕过清洗直达去重的路径（`_uniquify_outline_titles` 单独调用）：
        「（2）X」与「X」必须判重并消歧，不得双双保留。"""
        tree = [
            {
                "title": "第一章",
                "level": 1,
                "children": [
                    {
                        "title": "项目总体理解",
                        "level": 2,
                        "children": [
                            {"title": "混凝土施工工艺", "level": 3},
                            {"title": "（2）混凝土施工工艺", "level": 3},
                        ],
                    }
                ],
            }
        ]
        _uniquify_outline_titles(tree)
        titles = [c["title"] for c in tree[0]["children"][0]["children"]]
        assert len(set(titles)) == 2, f"前缀污染标题与干净标题双双保留: {titles}"
        assert "（2）混凝土施工工艺" not in titles, titles


# ===========================================================================
# L-3：pytest 运行约束文档守护
# ===========================================================================


class TestL3PytestRunConstraintDocumented:
    def test_pyproject_documents_serial_run_constraint(self):
        """并行 pytest 互相干扰的根因与约束必须固化在 pyproject，
        防止后续有人（或 CI）并行跑导致间歇性计数漂移/收集失败。"""
        text = (BACKEND_DIR / "pyproject.toml").read_text(encoding="utf-8")
        assert "不要并行跑多个 pytest 进程" in text, "运行约束未固化到 pyproject"
        assert "PYTHONDONTWRITEBYTECODE" in text, "根因（字节码竞态）未说明"


# ===========================================================================
# L-5：存量 sections 表 L3+ 占位标题树清洗（迁移阶段 3）
# ===========================================================================


def _make_sections_db(tmp_path):
    """构造带 sections 表 + 两个项目存量脏数据的 sqlite3。"""
    path = os.path.join(str(tmp_path), "legacy_sections.db")
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE sections ("
        "id INTEGER PRIMARY KEY, project_id INTEGER, parent_id INTEGER, "
        "title TEXT, level INTEGER, sort_order INTEGER, content TEXT)"
    )
    # 项目 1：L1/L2 干净（用户正式章节名），L3 三个脏 + 一个好
    conn.execute(
        "INSERT INTO sections (id, project_id, parent_id, title, level, sort_order) "
        "VALUES (1, 100, NULL, '第一章 质量管理措施', 1, 1)"
    )
    conn.execute(
        "INSERT INTO sections (id, project_id, parent_id, title, level, sort_order) "
        "VALUES (2, 100, 1, '总体方案与实施策划', 2, 1)"
    )
    for i, t in enumerate(["专项6", "总体实施策划（2）", "（2）安全保障", "样板区质量控制"], start=1):
        conn.execute(
            "INSERT INTO sections (id, project_id, parent_id, title, level, sort_order) "
            f"VALUES ({10 + i}, 100, 2, '{t}', 3, {i})"
        )
    # 项目 2：同一批脏标题（验证项目间隔离 + 复用不串）
    conn.execute(
        "INSERT INTO sections (id, project_id, parent_id, title, level, sort_order) "
        "VALUES (20, 200, NULL, '第二章 进度管理', 1, 1)"
    )
    conn.execute(
        "INSERT INTO sections (id, project_id, parent_id, title, level, sort_order) "
        "VALUES (21, 200, 20, '总体方案与实施策划', 2, 1)"
    )
    for i, t in enumerate(["专项6", "第六专项", "项目6"], start=1):
        conn.execute(
            "INSERT INTO sections (id, project_id, parent_id, title, level, sort_order) "
            f"VALUES ({30 + i}, 200, 21, '{t}', 3, {i})"
        )
    conn.commit()
    return conn


class TestL5LegacySectionsCleanup:
    def test_sections_l3_placeholders_cleaned(self, tmp_path):
        from app.db_migrations import clean_legacy_outline_title_suffixes
        from app.services.ai.outline_common import _is_dirty_l3_title_deep

        conn = _make_sections_db(tmp_path)
        stats = clean_legacy_outline_title_suffixes(conn)
        assert stats.get("sections.placeholder", 0) >= 5, (
            f"sections L3+ 占位标题未清洗: {stats}"
        )
        rows = conn.execute(
            "SELECT id, title, level FROM sections ORDER BY id"
        ).fetchall()
        by_id = {r["id"]: r for r in rows}
        # L3 行全部干净
        for r in rows:
            if r["level"] >= 3:
                assert not _is_dirty_l3_title_deep(r["title"]), f"DB 残留脏标题: {r['title']}"
        # L1/L2 用户正式章节名绝不被碰
        assert by_id[1]["title"] == "第一章 质量管理措施"
        assert by_id[2]["title"] == "总体方案与实施策划"
        assert by_id[20]["title"] == "第二章 进度管理"
        assert by_id[21]["title"] == "总体方案与实施策划"
        # 好标题保留
        assert by_id[14]["title"] == "样板区质量控制"
        # 两项目不互相污染（同批脏标题各自改写，且项目 2 的也被清洗）
        assert by_id[31]["title"] != "专项6"
        assert by_id[32]["title"] != "第六专项"
        assert by_id[33]["title"] != "项目6"
        conn.close()

    def test_sections_cleanup_is_idempotent(self, tmp_path):
        from app.db_migrations import clean_legacy_outline_title_suffixes

        conn = _make_sections_db(tmp_path)
        clean_legacy_outline_title_suffixes(conn)
        stats2 = clean_legacy_outline_title_suffixes(conn)
        assert stats2.get("sections.placeholder", 0) == 0, (
            f"二次执行应有零改动（幂等）: {stats2}"
        )
        conn.close()

    def test_missing_sections_table_is_safe(self, tmp_path):
        """表缺失/缺列时静默跳过，不抛异常、不记 stats。"""
        from app.db_migrations import clean_legacy_outline_title_suffixes

        path = os.path.join(str(tmp_path), "empty.db")
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE other (x TEXT)")
        conn.commit()
        stats = clean_legacy_outline_title_suffixes(conn)
        assert "sections.placeholder" not in stats, stats
        conn.close()


# ===========================================================================
# L-2：源码断言改用直读截取（_func_src 行为 + 回归文件接线）
# ===========================================================================


class TestL2FuncSrcExtraction:
    def test_func_src_helper_contract(self):
        """`_func_src` 必须：命中函数、在下一个同级 def/class 处停止、
        不被行号元数据影响（读的是当前源文件）。"""
        from tests.test_import_outline_fixes_2026_10_09 import _func_src

        src = _func_src("app/services/ai/outline_common.py", "_title_core")
        assert "def _title_core(" in src
        assert "return tt" in src, "未截取到函数体"
        # 下一个函数体不得被卷入
        assert "_clean_legacy_suffix_tree(level1" not in src.split("def ", 2)[-1] or True
        assert "def _clean_legacy_suffix_tree" not in src, "截取未在下一个 def 处停止"

    def test_func_src_missing_function_raises(self):
        from tests.test_import_outline_fixes_2026_10_09 import _func_src

        with pytest.raises(AssertionError):
            _func_src("app/services/ai/outline_common.py", "__不存在的函数__")

    def test_regression_file_no_longer_uses_getsource_for_these(self):
        """接线守护：两个 uploaded_files 源码断言测试不得再用
        inspect.getsource（元数据漂移 flaky 根因）。

        注：先剥整行注释再断言——修复注释里刻意引用旧写法作为说明，
        与「代码仍在使用」是两回事（与 tests/test_import_outline_fixes_
        2026_10_09.py 里 task_src 的注释剥离同口径）。
        """
        text = (BACKEND_DIR / "tests" / "test_import_outline_fixes_2026_10_09.py").read_text(
            encoding="utf-8"
        )
        seg = text[text.index("class TestUploadedFilesScopedByProject"):]
        seg = "\n".join(
            line for line in seg.splitlines() if not line.lstrip().startswith("#")
        )
        assert "inspect.getsource" not in seg, "uploaded_files 断言仍依赖 inspect 元数据"
        assert '_func_src("app/services/files.py"' in seg, "未改用直读源文件截取"
