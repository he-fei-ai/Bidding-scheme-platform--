"""DDL 双写一致性守卫：``db.py::init_db`` 与 ``db_migrations.ensure_enhancement_tables``。

背景（BUG 2026-10-12）
---------------------
本平台表结构有两处 DDL 来源：

1. ``app/db.py::init_db`` —— 启动时全量建表 / 补列（新库主路径，末尾调用迁移）；
2. ``app/db_migrations.py::ensure_enhancement_tables`` —— 老库增量迁移（步骤 1..37）。

两者手工双写，已经出过两类事故：

- 易标字数控制移植时只改 ``init_db``，漏了 ``db_migrations``（移植报告 §6.5）；
- 补上迁移步骤 37 后，老库/测试夹具里 ``sections`` 表尚未建出时
  ``ALTER TABLE sections`` 直接抛 ``no such table``，把整条迁移链打崩
  （``test_image_config::test_migration_upgrades_old_image_tasks_schema``）。

守卫范围（有意收敛，不做「全表列集合全等」）
------------------------------------------
``init_db`` 末尾**必定**调用 ``ensure_enhancement_tables()``（本文件第 3 节钉住
这一前提），因此两处 DDL 是**并集生效**，合法的「只在其中一侧声明」并不构成
缺陷；真正的风险只有三类，故只钉这三类：

1. **前提漂移**：哪天 ``init_db`` 不再调用迁移，双写立刻退化为「只有一半」；
2. **同构表列冲突**：同一张表两侧都完整声明（``word_control_settings``）时列集合
   必须一致，否则「按新配置生成、按旧列校验」类分叉会重现；
3. **关键字数列被漏掉**：R12 警告回写 / 字数统计强依赖的 sections 列，任一侧漏写
   都会让对应特性在特定库形态上失效（本次即由守卫报出 ``sections.target_words``
   只在 ``init_db``、``sections.word_count`` 只在迁移侧的不对称）。
"""

from __future__ import annotations

import io
import os
import re

import pytest


APP_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")
DB_PY = os.path.join(APP_DIR, "db.py")
MIGRATIONS_PY = os.path.join(APP_DIR, "db_migrations.py")

#: 双方都**完整**声明（列集合应完全一致）的表
FULLY_DUPLICATED_TABLES = frozenset({"word_control_settings"})

#: 字数控制强依赖的 sections 列（R12 警告回写 / 校验页 / 提示词都要读）
SECTIONS_WORD_COLUMNS = (
    "target_words",
    "word_count",
    "readable_word_count",
    "word_count_target",
    "word_count_deviation",
    # ENHANCE（本轮跨模块深度探索）：单节点档位覆盖列。check_document 的
    # L3 区间判定、build_tree / update_section_word_count_tier 都读它。
    "word_count_tier",
    # ENHANCE（本轮跨模块深度探索）：图表数据列。导出侧
    # _batch_load_section_details / _render_section_charts 直接 SELECT。
    "gantt_json",
    "architecture_json",
    "labor_json",
    "inlined_chart_json",
    "comparison_json",
    "layout_json",
    "timeline_json",
)

#: R12 阶段引入、迁移侧**必须**补齐的列（老库升级后要能回写偏差）
SECTIONS_WORD_COLUMNS_MIGRATION_REQUIRED = (
    "readable_word_count",
    "word_count_target",
    "word_count_deviation",
    # ENHANCE（本轮跨模块深度探索）：这 8 列 2026-10-21 前只在 db.py::
    # init_db 侧声明，「只跑迁移不跑 init_db」的路径会 no such column，
    # 现补入迁移链（db_migrations 步骤 37）并由本守卫钉住不再回退。
    "word_count_tier",
    "gantt_json",
    "architecture_json",
    "labor_json",
    "inlined_chart_json",
    "comparison_json",
    "layout_json",
    "timeline_json",
)


def _read(path: str) -> str:
    with io.open(path, "r", encoding="utf-8") as fh:
        return fh.read()


# ``CREATE TABLE [IF NOT EXISTS] name ( ... )``
_RE_CREATE = re.compile(
    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[\"']?([A-Za-z_][A-Za-z0-9_]*)[\"']?\s*\((.*?)\)",
    re.IGNORECASE | re.DOTALL,
)
# ``ALTER TABLE t ADD COLUMN c``
_RE_ALTER = re.compile(
    r"ALTER\s+TABLE\s+([A-Za-z_][A-Za-z0-9_]*)\s+ADD\s+COLUMN\s+([A-Za-z_][A-Za-z0-9_]*)",
    re.IGNORECASE,
)
# ``_add_column_if_not_exists("t", "c", ...)``（db_migrations.py 的写法）
_RE_HELPER = re.compile(
    r"_add_column_if_not_exists\(\s*\"([A-Za-z_][A-Za-z0-9_]*)\"\s*,\s*\"([A-Za-z_][A-Za-z0-9_]*)\"",
    re.IGNORECASE,
)
# ``_ensure_column(conn, "t", "c", ...)``（db.py 的写法）
_RE_HELPER_ENSURE = re.compile(
    r"_ensure_column\(\s*conn\s*,\s*\"([A-Za-z_][A-Za-z0-9_]*)\"\s*,\s*\"([A-Za-z_][A-Za-z0-9_]*)\"",
    re.IGNORECASE,
)
# 数据驱动补列：``for col, coldef in [("a", "TYPE"), ...]`` + 紧随其后的 helper 调用
_RE_FORLIST = re.compile(r"for\s+\w+\s*,\s*\w+\s+in\s*\[(.*?)\]", re.DOTALL)
_RE_PAIR = re.compile(r"\(\s*[\"']([A-Za-z_][A-Za-z0-9_]*)[\"']\s*,\s*[\"'][^\"']*[\"']\s*\)")
_RE_HELPER_TABLE = re.compile(
    r"_add_column_if_not_exists\(\s*[\"']([A-Za-z_][A-Za-z0-9_])[\"']"
)
_RE_HELPER_TABLE_FIXED = re.compile(
    r"_add_column_if_not_exists\(\s*[\"']([A-Za-z_][A-Za-z0-9_]*)[\"']"
)


def _split_columns(body: str) -> list[str]:
    """把建表体拆成列名（跳过约束关键字，忽略括号内的嵌套）。"""
    cols: list[str] = []
    depth = 0
    current = ""
    for ch in body:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            cols.append(current)
            current = ""
        else:
            current += ch
    cols.append(current)
    out: list[str] = []
    for raw in cols:
        token = raw.strip().split()[0] if raw.strip() else ""
        token = token.strip("\"'`[]()")
        if not token or token.upper() in {
            "PRIMARY", "FOREIGN", "UNIQUE", "CHECK", "CONSTRAINT", "KEY",
        }:
            continue
        out.append(token)
    return out


def extract_ddl(source: str) -> tuple[dict[str, set[str]], set[str]]:
    """抽取 (表 → 列集合, 补列声明 "table.col" 集合)。名字统一小写。"""
    tables: dict[str, set[str]] = {}
    for m in _RE_CREATE.finditer(source):
        tables.setdefault(m.group(1).lower(), set()).update(
            c.lower() for c in _split_columns(m.group(2))
        )
    added: set[str] = set()
    for m in _RE_ALTER.finditer(source):
        added.add(f"{m.group(1).lower()}.{m.group(2).lower()}")
    for m in _RE_HELPER.finditer(source):
        added.add(f"{m.group(1).lower()}.{m.group(2).lower()}")
    for m in _RE_HELPER_ENSURE.finditer(source):
        added.add(f"{m.group(1).lower()}.{m.group(2).lower()}")
    for m in _RE_FORLIST.finditer(source):
        table_m = _RE_HELPER_TABLE_FIXED.search(source[m.end():m.end() + 400])
        if not table_m:
            continue
        table = table_m.group(1).lower()
        for pair in _RE_PAIR.finditer(m.group(1)):
            added.add(f"{table}.{pair.group(1).lower()}")
    return tables, added


@pytest.fixture(scope="module")
def ddl() -> dict:
    init_tables, init_added = extract_ddl(_read(DB_PY))
    mig_tables, mig_added = extract_ddl(_read(MIGRATIONS_PY))
    return {
        "init_tables": init_tables,
        "init_added": init_added,
        "mig_tables": mig_tables,
        "mig_added": mig_added,
    }


def _columns(ddl: dict, side: str, table: str) -> set[str]:
    tables = ddl[f"{side}_tables"].get(table, set())
    added = {c.split(".", 1)[1] for c in ddl[f"{side}_added"] if c.startswith(f"{table}.")}
    return set(tables) | added


# ---------------------------------------------------------------------------
# 1. 抽取器自检（防止正则退化导致后续断言全部空转）
# ---------------------------------------------------------------------------


class TestExtractor:
    def test_create_and_columns(self):
        src = """
        CREATE TABLE IF NOT EXISTS demo (
            id INTEGER PRIMARY KEY,
            name TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
            FOREIGN KEY(project_id) REFERENCES projects(id)
        );
        """
        tables, _ = extract_ddl(src)
        assert {"id", "name", "created_at"} <= tables["demo"]
        assert "foreign" not in tables["demo"]

    def test_alter_and_helpers(self):
        src = '''
        conn.execute("ALTER TABLE sections ADD COLUMN readable_word_count INTEGER NOT NULL DEFAULT 0")
        _add_column_if_not_exists("sections", "word_count_target", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "sections", "target_words", "INTEGER DEFAULT NULL")
        for col, coldef in [("foo_bar", "TEXT"), ("baz_qux", "INTEGER")]:
            _add_column_if_not_exists("sections", col, coldef)
        '''
        _, added = extract_ddl(src)
        for expect in (
            "sections.readable_word_count",
            "sections.word_count_target",
            "sections.target_words",
            "sections.foo_bar",
            "sections.baz_qux",
        ):
            assert expect in added, f"抽取漏掉 {expect}"

    def test_real_sources_non_trivial(self, ddl):
        assert len(ddl["init_tables"]) >= 20, "init_db 建表数量异常，抽取器可能失效"
        assert len(ddl["mig_tables"]) >= 3, "迁移建表数量异常，抽取器可能失效"
        assert len(ddl["init_added"]) >= 4, "init_db 补列数量异常，抽取器可能失效"
        assert len(ddl["mig_added"]) >= 3, "迁移补列数量异常，抽取器可能失效"


# ---------------------------------------------------------------------------
# 2. 前提：init_db 末尾必定执行迁移（双写并集生效）
# ---------------------------------------------------------------------------


class TestInitDbRunsMigrations:
    def test_init_db_calls_ensure_enhancement_tables(self):
        src = _read(DB_PY)
        assert "from .db_migrations import" in src and "ensure_enhancement_tables" in src, (
            "db.py 不再导入迁移模块 —— 双写前提失效，需重新评估守卫范围"
        )

    def test_ensure_enhancement_tables_is_invoked(self):
        src = _read(DB_PY)
        calls = [ln.strip() for ln in src.splitlines() if ln.strip().startswith("ensure_enhancement_tables(")]
        assert calls, "db.py 里没有 ensure_enhancement_tables(...) 的调用"


# ---------------------------------------------------------------------------
# 3. 同构表：word_control_settings 列集合必须一致
# ---------------------------------------------------------------------------


class TestDuplicatedTableParity:
    @pytest.mark.parametrize("table", sorted(FULLY_DUPLICATED_TABLES))
    def test_columns_match(self, ddl, table):
        assert table in ddl["init_tables"], f"init_db 未声明 {table}"
        assert table in ddl["mig_tables"], f"迁移未声明 {table}"
        init_cols = _columns(ddl, "init", table)
        mig_cols = _columns(ddl, "mig", table)
        assert init_cols == mig_cols, (
            f"{table} 列不一致：init_db 多 {sorted(init_cols - mig_cols)}，"
            f"迁移多 {sorted(mig_cols - init_cols)}"
        )

    def test_word_control_snapshot_columns_present(self, ddl):
        cols = _columns(ddl, "mig", "word_control_settings")
        for col in (
            "minimum_words", "maximum_words", "section_words",
            "strict_section_words", "outline_word_control_snapshot_json",
            "snapshot_at", "word_control_warning",
        ):
            assert col in cols, f"word_control_settings 缺列 {col}（R14 快照锚定依赖）"


# ---------------------------------------------------------------------------
# 4. sections 字数相关列：两侧并集必须齐全，迁移侧必须覆盖 R12 三列
# ---------------------------------------------------------------------------


class TestSectionsWordColumns:
    def test_columns_covered_by_union(self, ddl):
        init_cols = _columns(ddl, "init", "sections")
        mig_cols = _columns(ddl, "mig", "sections")
        missing = [
            col
            for col in SECTIONS_WORD_COLUMNS
            if col not in init_cols and col not in mig_cols
        ]
        assert not missing, f"sections 字数列两侧都未声明：{missing}"

    @pytest.mark.parametrize("col", SECTIONS_WORD_COLUMNS_MIGRATION_REQUIRED)
    def test_migration_side_has_r12_columns(self, ddl, col):
        assert col in _columns(ddl, "mig", "sections"), (
            f"迁移侧缺 sections.{col} —— 老库升级后 R12 警告/偏差无法回写"
        )

    @pytest.mark.parametrize("col", ("target_words", "word_count"))
    def test_init_side_has_core_columns(self, ddl, col):
        """本次修复的对称项：init_db 侧此前漏声明，由守卫钉住不再回退。"""
        assert col in _columns(ddl, "init", "sections"), (
            f"init_db 侧缺 sections.{col} —— 只在迁移里 ALTER 过，"
            "「只跑 init_db 不跑迁移」的路径会 no such column"
        )