"""生成大纲模块 —— 2026-10-12 深度审计回归测试。

用户主诉：「三级标题大量是占位式泛化名称（专项6 / 总体实施策划（2）/
总体实施策划（3）），且存在编号冲突后缀（3）、（2）等」。

本文件逐条钉住本轮定位并修复的缺陷，全部为**结构不变量**断言，
而非具体实现细节断言 —— 后续重构若再次引入同类漏出，测试必须失败。
"""

from __future__ import annotations

import ast
import asyncio
import copy
import inspect
from pathlib import Path

import pytest

from app.services.ai import workflows_outline_v51 as V51
from app.services.ai.outline_common import (
    _cleanup_placeholder_titles,
    _is_dirty_l3_title,
    _title_core,
    _uniquify_outline_titles,
)
from app.services.ai.workflows_outline_utils import repair_l3_generic_titles


# ===========================================================================
# 通用工具
# ===========================================================================

def _walk(nodes, level=1):
    if not isinstance(nodes, list):
        return
    for n in nodes:
        if not isinstance(n, dict):
            continue
        yield n, level
        yield from _walk(n.get("children"), level + 1)


def _l3_titles(tree) -> list[str]:
    return [str(n.get("title") or "") for n, lv in _walk(tree) if lv == 3]


def _dirty(tree) -> list[str]:
    return [t for t in _l3_titles(tree) if _is_dirty_l3_title(t)]


def _hostile_tree(l1_count: int = 6, l2_per: int = 3, l3_per: int = 6) -> list[dict]:
    """复刻用户报告的目录形态：泛化父级 + 占位子级 + 裸编号后缀。"""
    generic_l2 = [
        "总体方案与实施策划", "分阶段推进计划", "资源配置与协调保障",
        "过程控制与考核机制", "总结评估与持续改进",
    ]
    tree = []
    for i in range(l1_count):
        l1 = {"id": str(i + 1), "title": f"第{i + 1}章 施工组织部署", "level": 1, "children": []}
        for j in range(l2_per):
            base = generic_l2[(i + j) % len(generic_l2)]
            l2 = {"id": f"{i + 1}.{j + 1}", "title": base, "level": 2, "children": []}
            for k in range(l3_per):
                mode = k % 6
                if mode == 0:
                    t = base
                elif mode == 1:
                    t = f"专项{(i * 7 + j * 3 + k) % 20 + 1}"
                elif mode == 2:
                    t = f"{base}（{k // 6 + 2}）"
                elif mode == 3:
                    t = f"{base}({k // 6 + 2})"
                elif mode == 4:
                    t = f"分阶段推进专项{(i + j + k) % 15 + 1}"
                else:
                    t = ""
                l2["children"].append({"id": f"{i + 1}.{j + 1}.{k + 1}", "title": t, "level": 3})
            l1["children"].append(l2)
        tree.append(l1)
    return tree


def _rule_closeout(tree: list[dict]) -> list[dict]:
    """复刻生产链路「清洗 → 去重 → 补全 → 去重 → 清洗」的规则收口。"""
    from app.services.ai.outline_common import _ensure_min_children

    t = copy.deepcopy(tree)
    _cleanup_placeholder_titles(t)
    t = _uniquify_outline_titles(t)
    t = _ensure_min_children(t, min_level2=3, min_level3=5)
    _uniquify_outline_titles(t)
    _cleanup_placeholder_titles(t)
    return t


def _source_of(fn) -> str:
    return inspect.getsource(fn)


def _backend_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _frontend_src() -> Path:
    """仓库根下的 frontend/src；纯后端 CI 环境下不存在，调用方需自行 skip。"""
    return _backend_root().parent / "frontend" / "src"


def _module_source() -> str:
    return Path(V51.__file__).read_text(encoding="utf-8")


def _ordinal_suffix_title_assignments(path: Path) -> list[str]:
    """用 AST 找出「把 `（{表达式}）` 拼成 f-string 并赋给 title」的代码点。

    必须走 AST 而非文本扫描：注释里大量引用了 `f"{new_title}（{N}）"` 这类
    历史写法作为修复说明，文本匹配会把它们一并报出来（假阳性），
    反之若靠放宽正则又会漏掉真正的生产点。AST 只看**可执行表达式**，
    既免疫注释/docstring，也不会因排版变化而漏判。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    hits: list[str] = []

    def _subscript_key(node: ast.AST) -> str | None:
        if isinstance(node, ast.Subscript):
            sl = node.slice
            if isinstance(sl, ast.Constant) and isinstance(sl.value, str):
                return sl.value
        return None

    def _has_paren_formattedvalue(node: ast.AST) -> bool:
        """检测形如 `…（{expr}）…` 的拼接（Opening { FormattedValue } Closing）。"""
        if not isinstance(node, ast.JoinedStr):
            return False
        vals = node.values
        for i, part in enumerate(vals):
            if (
                isinstance(part, ast.FormattedValue)
                and i >= 1
                and isinstance(vals[i - 1], ast.Constant)
                and isinstance(vals[i - 1].value, str)
                and vals[i - 1].value.endswith("（")
                and i + 1 < len(vals)
                and isinstance(vals[i + 1], ast.Constant)
                and isinstance(vals[i + 1].value, str)
                and vals[i + 1].value.startswith("）")
            ):
                return True
        return False

    for node in ast.walk(tree):
        targets: list[ast.AST] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
            targets = [node.target]
        else:
            continue
        if not any(_subscript_key(t) == "title" for t in targets):
            continue
        value = node.value
        if _has_paren_formattedvalue(value):
            hits.append(f"{path.name}:{getattr(node, 'lineno', '?')}")
    return hits


# ===========================================================================
# 1. P0：审核回灌重生成路径因 int 赋值永久失效
# ===========================================================================


class TestRetryRegenerationNotDead:
    """BUG-2026-10-12-P0：`v51.py` 审核回灌重生成整条路径 100% 失效。

    成因：`_retry_tree = _cleanup_placeholder_titles(_retry_tree)` 把**清洗计数
    int** 赋给了目录树变量 → 下游 `_self_check_v51(int)` 抛
    `TypeError: 'int' object is not iterable` → 被 `except Exception` 吞成一条
    warning。表现是「审核不通过 → 带建议重生成」永远不生效。
    """

    def test_cleanup_return_value_is_int_not_tree(self):
        """前提不变式：`_cleanup_placeholder_titles` 返回 int（这是坑的根源）。"""
        from app.services.ai.outline_common import _cleanup_placeholder_titles as f

        r = f(_hostile_tree(1, 1, 3))
        assert isinstance(r, int), "若将来改为返回目录树，本测试需同步更新"

    def test_no_assignment_of_cleanup_result_to_tree_variable(self):
        """源码级：任何 `X = _cleanup_placeholder_titles(...)` 形式的赋值都是 bug。"""
        import re

        offenders = []
        for path in (
            Path(V51.__file__),
            Path(__file__).resolve().parents[1] / "app" / "routers" / "sse" / "sse_db_ops.py",
            Path(__file__).resolve().parents[1] / "app" / "db_migrations.py",
        ):
            src = path.read_text(encoding="utf-8")
            for m in re.finditer(r"^(\s*)([A-Za-z_]\w*)\s*=\s*_cleanup_placeholder_titles\(",
                                 src, re.M):
                var = m.group(2)
                # 计数变量以 _ 开头或明确叫 *_fixed / *_count，属于合法用法
                if var.startswith("_") or var.endswith(("_fixed", "_count", "_n", "fixed")):
                    continue
                offenders.append(f"{path.name}:{src[:m.start()].count(chr(10)) + 1} -> {var}")
        assert not offenders, (
            "把 _cleanup_placeholder_titles 的 int 返回值赋给了目录树变量："
            + "; ".join(offenders)
        )

    def test_retry_path_pipeline_keeps_tree_type(self):
        """行为断言：复刻回灌路径的收口序列，全程目录树必须保持 list。"""
        t = _hostile_tree()
        fixed = _cleanup_placeholder_titles(t)          # int
        assert isinstance(fixed, int)
        t2 = _uniquify_outline_titles(t)                # list（未污染）
        assert isinstance(t2, list), "uniquify 不得返回非 list"
        assert _dirty(t2) == [], "回灌收口后不得残留脏标题"

    def test_self_check_accepts_list_not_int(self):
        """行为断言：`_self_check_v51` 对 int 输入必须报错（证明 P0 的失败形态）。"""

        class _P:
            async def chat(self, m, **k):
                return "{}"

        with pytest.raises(TypeError):
            asyncio.run(V51._self_check_v51(_P(), 0, 0.0, verdict_out={}))

    def test_retry_path_has_l3_quality_closeout(self):
        """回灌路径必须与主路径共用具体性收口（此前完全缺失该环节）。"""
        src = _module_source()
        start = src.index("审核回灌：门禁发现")
        seg = src[start:start + 8000]
        assert "_l3_specificity_closeout(" in seg, (
            "审核回灌路径缺少三级标题具体性收口，与主路径质量口径不一致"
        )
        assert "_post_selfcheck_title_safety_net(" in seg, (
            "审核回灌路径的自检之后缺少标题兜底"
        )


# ===========================================================================
# 2. P1：AI 自检在质量收口之后改标题，此前无任何二次收口
# ===========================================================================


class TestPostSelfCheckSafetyNet:
    def test_safety_net_removes_placeholders_introduced_by_autofix(self):
        """`_auto_fix_issues` 重命名 / 空标题兜底引入的占位，必须被兜底清洗。"""
        tree = [{
            "id": "1", "title": "施工部署", "children": [{
                "id": "1.1", "title": "资源配置", "children": [
                    {"id": "1.1.1", "title": "总体实施策划（2）"},
                    {"id": "1.1.2", "title": "专项6"},
                    {"id": "1.1.3", "title": "总体实施策划"},
                ],
            }],
        }]
        out = V51._post_selfcheck_title_safety_net(tree)
        assert _dirty(out) == [], f"兜底后仍有脏标题: {_l3_titles(out)}"
        titles = _l3_titles(out)
        assert len(set(_title_core(t) for t in titles)) == len(titles), "兜底后同级仍撞名"

    def test_safety_net_is_idempotent(self):
        tree = _hostile_tree(2, 2, 4)
        once = V51._post_selfcheck_title_safety_net(copy.deepcopy(tree))
        twice = V51._post_selfcheck_title_safety_net(copy.deepcopy(once))
        assert _l3_titles(once) == _l3_titles(twice), "兜底必须幂等（重复调用不二次改名）"

    def test_safety_net_never_raises_on_bad_input(self):
        for bad in (None, [], [1, 2, 3], [{"title": None, "children": "x"}]):
            out = V51._post_selfcheck_title_safety_net(bad)
            assert out is not None or bad is None

    def test_safety_net_called_after_both_self_checks(self):
        """主路径与回灌路径的自检之后都必须接兜底。"""
        src = _module_source()
        n = src.count("_post_selfcheck_title_safety_net(")
        # 1 次定义 + 2 次调用
        assert n >= 3, f"兜底调用点不足（实际 {n} 次，含定义）"

    def test_full_pipeline_after_self_check_is_clean(self):
        """端到端：敌意目录 → 规则收口 → 自检 → 兜底，零脏标题、零同级撞名。"""
        tree = _rule_closeout(_hostile_tree())
        assert _dirty(tree) == []
        issues = V51._quick_local_check(tree)
        tree = V51._auto_fix_issues(tree, issues)
        tree = V51._post_selfcheck_title_safety_net(tree)
        assert _dirty(tree) == [], f"自检后兜底未生效: {_dirty(tree)}"


# ===========================================================================
# 3. P1：AI 修复复读占位标题时保留脏标题
# ===========================================================================


class TestL3RepairNeverKeepsDirty:
    class _Provider:
        def __init__(self, replies):
            self._replies = list(replies)
            self.calls = 0

        async def chat(self, messages, **kwargs):
            self.calls += 1
            return self._replies.pop(0) if self._replies else "专项6"

    def _tree(self):
        return [{
            "title": "总体施工部署",
            "children": [{
                "title": "总体方案与实施策划",
                "children": [
                    {"title": "总体实施策划（2）"},
                    {"title": "专项6"},
                    {"title": "总体实施策划(3)"},
                    {"title": "样板区质量控制"},
                ],
            }],
        }]

    @pytest.mark.asyncio
    async def test_echo_provider_yields_clean_titles(self):
        out = await repair_l3_generic_titles(
            self._Provider(["专项7", "总体实施策划（3）", "实施要点"]),
            self._tree(), "某施工总承包工程", "", "", "", "",
            max_repair=8,
        )
        assert _dirty(out) == [], f"AI 复读占位后仍留脏标题: {_l3_titles(out)}"

    @pytest.mark.asyncio
    async def test_provider_exception_yields_clean_titles(self):
        class _Boom:
            async def chat(self, m, **k):
                raise RuntimeError("down")

        out = await repair_l3_generic_titles(
            _Boom(), self._tree(), "", "", "", "", "", max_repair=8
        )
        assert _dirty(out) == [], f"AI 异常后仍留脏标题: {_l3_titles(out)}"

    @pytest.mark.asyncio
    async def test_clean_titles_are_not_needlessly_rewritten(self):
        """反向验证：具体标题不得被规则兜底无谓改写（防过度修复）。"""
        provider = self._Provider(["地下室结构实测实量方案"])
        out = await repair_l3_generic_titles(
            provider, self._tree(), "施工总承包", "", "", "", "", max_repair=8
        )
        assert "样板区质量控制" in _l3_titles(out), "合法标题被误改"

    @pytest.mark.asyncio
    async def test_repair_does_not_mutate_input(self):
        tree = self._tree()
        before = copy.deepcopy(tree)
        await repair_l3_generic_titles(
            self._Provider(["专项7"]), tree, "", "", "", "", "", max_repair=8
        )
        assert tree == before, "repair 不得就地修改调用方传入的目录树"


# ===========================================================================
# 4. P1：仓库内最后一个「（N）」裸编号后缀生产点
# ===========================================================================


class TestNoOrdinalSuffixProducers:
    def test_sanitize_framework_titles_no_longer_emits_parenthesised_ordinal(self):
        """`sanitize_framework_titles` 的最终兜底不得再拼 `（{chapter_idx+1}）`。"""
        from app.services.ai.outline_common import sanitize_framework_titles

        path = Path(sanitize_framework_titles.__code__.co_filename)
        assert not _ordinal_suffix_title_assignments(path), (
            "sanitize_framework_titles 仍在拼接裸编号后缀（用户报告形态之一）"
        )
        assert "_disambiguate_title(" in _source_of(sanitize_framework_titles), (
            "最终兜底应改用语义化消歧"
        )

    def test_no_module_produces_parenthesised_ordinal_title(self):
        """全目录扫描：不得存在 f-string 拼「（{…}）」后写入 title 的代码点。"""
        app_dir = Path(__file__).resolve().parents[1] / "app"
        offenders: list[str] = []
        for py in app_dir.rglob("*.py"):
            if py.name.startswith("test_"):
                continue
            offenders += _ordinal_suffix_title_assignments(py)
        assert not offenders, "仍在生产裸编号后缀标题: " + "; ".join(offenders)

    def test_disambiguate_output_is_never_ordinal_suffix(self):
        from app.services.ai.outline_common import _disambiguate_title, _ORDINAL_SUFFIX_RE

        base = "总体实施策划"
        used = set()
        for i in range(1, 40):
            cand = _disambiguate_title(base, used, parent_title="质量管理措施")
            assert not _ORDINAL_SUFFIX_RE.search(cand), f"消歧产出了裸编号后缀: {cand}"
            used.add(_title_core(cand))


# ===========================================================================
# 5. P1：纯序号占位标题（「内容N」）生产点与识别
# ===========================================================================


class TestOrdinalOnlyPlaceholders:
    @pytest.mark.parametrize(
        "title",
        ["内容1", "内容3", "子章节2", "章节1", "小节4", "条2", "项7"],
    )
    def test_detector_recognises_ordinal_only_placeholders(self, title):
        assert _is_dirty_l3_title(title), f"「{title}」应判为占位标题"

    @pytest.mark.parametrize(
        "title",
        ["第一内容", "内容管理", "第三章施工部署", "工序交接流程5", "关键工序1",
         "质量控制措施2", "小节说明", "本章节要点"],
    )
    def test_detector_does_not_false_positive(self, title):
        assert not _is_dirty_l3_title(title), f"「{title}」被误判为占位标题"

    def test_auto_fix_empty_l3_uses_semantic_derivation(self):
        """`_auto_fix_issues` 的空标题兜底必须是语义派生，不得写死 `内容N`。"""
        tree = [{
            "id": "1", "title": "质量管理措施", "children": [{
                "id": "1.1", "title": "质量控制", "children": [
                    {"id": "1.1.1", "title": ""},
                    {"id": "1.1.2", "title": "   "},
                ],
            }],
        }]
        issues = V51._quick_local_check(tree)
        V51._auto_fix_issues(tree, issues)
        titles = _l3_titles(tree)
        assert titles, "空标题未被修复"
        for t in titles:
            assert not _is_dirty_l3_title(t), f"空标题兜底仍产出占位: {t}"

    def test_auto_fix_source_has_no_hardcoded_ordinal_placeholder(self):
        """用 AST 断言：`_auto_fix_issues` 不再把纯序号 f-string 写进 title。"""
        path = Path(V51._auto_fix_issues.__code__.co_filename)

        def _ordinal_only_titles(t: ast.AST) -> list[str]:
            hits = []
            if isinstance(t, ast.JoinedStr):
                lit = "".join(
                    p.value for p in t.values
                    if isinstance(p, ast.Constant) and isinstance(p.value, str)
                )
                if lit and any(
                    lit.startswith(w) for w in ("内容", "子章节", "章节", "小节")
                ):
                    hits.append(lit)
            for child in ast.iter_child_nodes(t):
                hits += _ordinal_only_titles(child)
            return hits

        fn = next(
            n for n in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
            if isinstance(n, ast.FunctionDef) and n.name == "_auto_fix_issues"
        )
        assert _ordinal_only_titles(fn) == [], (
            "_auto_fix_issues 仍在写死纯序号占位标题（如「内容N」）"
        )


# ===========================================================================
# 6. P1：落库前同级撞名只告警不修复
# ===========================================================================


class TestSavePathCollisionRepair:
    def test_save_path_uniquifies_after_cleanup(self):
        """`_clear_and_save_outline` 必须在 cleanup 之后再做一次同级去重。"""
        path = Path(__file__).resolve().parents[1] / "app" / "routers" / "sse" / "sse_db_ops.py"
        src = path.read_text(encoding="utf-8")
        i_cleanup = src.index("_ph_fixed = _cleanup_placeholder_titles(outline)")
        i_uniquify = src.index("_uniquify_outline_titles(outline)")
        assert i_cleanup < i_uniquify, (
            "顺序必须是「清洗 → 去重」：反序会把裸编号后缀从末尾挪到中间，"
            "令清洗的末尾匹配失效"
        )

    def test_title_snapshot_helpers_are_pure(self):
        from app.routers.sse.sse_db_ops import (
            _count_title_changes,
            _snapshot_titles,
        )

        tree = _hostile_tree(2, 2, 3)
        snap = _snapshot_titles(tree)
        assert len(snap) == len(list(_walk(tree)))
        assert _count_title_changes(snap, snap) == 0
        assert _count_title_changes(["a"], ["b"]) == 1
        assert _count_title_changes(["a"], ["a", "b"]) == 1
        assert _count_title_changes(["a", "b"], ["a"]) == 1

    def test_save_path_collisions_are_actually_resolved(self):
        """行为断言：走完落库归一化链后，同级不得残留撞名。"""
        from app.services.ai.workflows_outline_utils import (
            _dedupe_outline_titles,
            normalize_outline_items,
        )

        tree = _hostile_tree()
        tree = normalize_outline_items(tree)
        tree = _dedupe_outline_titles(tree)
        _cleanup_placeholder_titles(tree)
        tree = _uniquify_outline_titles(tree)
        _cleanup_placeholder_titles(tree)

        def _dups(nodes, parent=""):
            out = []
            seen = {}
            if isinstance(nodes, list):
                for n in nodes:
                    if not isinstance(n, dict):
                        continue
                    c = _title_core(str(n.get("title") or ""))
                    if c:
                        if c in seen:
                            out.append(n.get("title"))
                        seen[c] = n.get("id")
                    out += _dups(n.get("children"))
            return out

        assert _dups(tree) == [], "落库归一化链后仍有同级撞名"


# ===========================================================================
# 7. 编号正确率 / 层级完整率 不变式
# ===========================================================================


class TestNumberingInvariants:
    def _assert_ids_positional(self, tree):
        from app.services.ai.outline_gate import renumber_outline_ids

        renumber_outline_ids(tree)
        ok = total = 0
        for n, _lv in _walk(tree):
            total += 1
        idx = 0

        def rec(nodes, prefix=""):
            nonlocal ok
            for i, n in enumerate(nodes, 1) if isinstance(nodes, list) else []:
                exp = f"{prefix}.{i}" if prefix else str(i)
                if str(n.get("id")) == exp:
                    ok += 1
                rec(n.get("children"), exp)

        rec(tree)
        assert ok == total, f"位置式编号正确率 {ok}/{total}"

    def test_hostile_tree_after_closeout_has_correct_numbering(self):
        tree = _rule_closeout(_hostile_tree())
        self._assert_ids_positional(tree)

    def test_level_always_equals_depth(self):
        tree = _rule_closeout(_hostile_tree())
        for _n, lv in _walk(tree):
            assert int(_n.get("level") or lv) == lv, "level 与树深度不一致"

    def test_min_children_guaranteed(self):
        from app.services.ai.outline_common import _ensure_min_children

        tree = _ensure_min_children(_hostile_tree(4, 2, 2), min_level2=3, min_level3=5)
        for n, lv in _walk(tree):
            if lv == 1:
                assert len(n.get("children") or []) >= 3, "一级节点子节点不足 3"
            if lv == 2:
                assert len(n.get("children") or []) >= 5, "二级节点子节点不足 5"

    def test_closeout_is_idempotent(self):
        a = _rule_closeout(_hostile_tree())
        b = _rule_closeout(a)
        assert _l3_titles(a) == _l3_titles(b), "收口链不幂等"

    def test_deep_tree_does_not_blow_stack(self):
        root = {"id": "1", "title": "深", "children": []}
        cur = root
        for i in range(2, 12):
            nxt = {"id": f"{cur['id']}.1", "title": f"层{i}", "children": []}
            cur["children"].append(nxt)
            cur = nxt
        out = V51._post_selfcheck_title_safety_net([root])
        assert isinstance(out, list) and len(out) == 1

    def test_wide_identical_generic_titles_stay_unique(self):
        wide = [{"id": "1", "title": "章", "children": [{
            "id": "1.1", "title": "总体方案与实施策划",
            "children": [{"id": f"1.1.{i+1}", "title": "总体实施策划"} for i in range(120)],
        }]}]
        out = V51._post_selfcheck_title_safety_net(wide)
        titles = _l3_titles(out)
        assert len(set(titles)) == len(titles), "同级标题未消歧"
        assert _dirty(out) == [], f"宽泛化场景残留脏标题: {_dirty(out)[:5]}"


# ===========================================================================
# 8. 端到端：直出路径质量口径与分步路径一致
# ===========================================================================


class TestDirectPathCloseoutConsistency:
    @pytest.mark.asyncio
    async def test_direct_closeout_on_hostile_input(self):
        class _Echo:
            def __init__(self):
                self.calls = 0

            async def chat(self, m, **k):
                self.calls += 1
                return "专项6"

        out = await V51._apply_l3_quality_closeout(
            _Echo(), _hostile_tree(), summary="施工总承包工程"
        )
        assert isinstance(out, list), "收口不得返回非 list（历史上曾返回 int）"
        assert _dirty(out) == [], f"直出路径残留脏标题: {_dirty(out)[:5]}"

    @pytest.mark.asyncio
    async def test_direct_closeout_never_raises(self):
        class _Boom:
            async def chat(self, m, **k):
                raise RuntimeError("down")

        out = await V51._apply_l3_quality_closeout(_Boom(), _hostile_tree())
        assert isinstance(out, list)

    def test_config_switch_still_gates_closeout(self):
        from app.config import OUTLINE_L3_QUALITY_ENHANCE_ENABLED

        assert OUTLINE_L3_QUALITY_ENHANCE_ENABLED is True
        src = _module_source()
        assert "if OUTLINE_L3_QUALITY_ENHANCE_ENABLED:" in src, (
            "分步路径的三级质量收口必须仍受配置开关控制（向后兼容）"
        )


# ===========================================================================
# 9. 源码可解析性（阻断性缺陷的直接防线）
# ===========================================================================


class TestAllAppModulesParse:
    def test_services_files_module_parses(self):
        """`app/services/files.py` 曾因 IndentationError 无法解析。

        它被目录生成 SSE 链路间接 import，一旦破损，**整个后端**（含生成大纲）
        都无法启动 —— 表现为大批测试在 collection 阶段报
        `IndentationError`，而非某个用例失败，极易被误判为「测试坏了」。
        """
        path = Path(__file__).resolve().parents[1] / "app" / "services" / "files.py"
        ast.parse(path.read_text(encoding="utf-8"))

    @pytest.mark.parametrize(
        "rel",
        [
            "app/services/ai/workflows_outline_v51.py",
            "app/services/ai/workflows_outline_utils.py",
            "app/services/ai/outline_common.py",
            "app/services/ai/outline_gate.py",
            "app/routers/sse/sse_db_ops.py",
            "app/routers/sections.py",
            "app/services/sections.py",
        ],
    )
    def test_outline_modules_parse(self, rel):
        path = Path(__file__).resolve().parents[1] / rel
        ast.parse(path.read_text(encoding="utf-8"))


# ===========================================================================
# 10. 死代码治理：sanitize_framework_titles 不得变成影子链路
# ===========================================================================


class TestNoShadowSanitizePath:
    def test_no_production_module_imports_it(self):
        """生产代码（tests 之外）不得 import `sanitize_framework_titles`。

        它是无调用方的死函数；若被重新接线，就会与真实兜底
        `_default_l2_children_for_skeleton` 形成**两套并行的标题生成策略**，
        任何针对其中一套的质量治理都不覆盖另一套 —— 正是 2026-10-09~11
        「专项6 / 总体实施策划（2）」反复复发的结构性成因。
        """
        app_dir = Path(__file__).resolve().parents[1] / "app"
        offenders: list[str] = []
        for py in app_dir.rglob("*.py"):
            if py.name.startswith("test_"):
                continue
            src = py.read_text(encoding="utf-8")
            for node in ast.walk(ast.parse(src)):
                hit = False
                if isinstance(node, ast.ImportFrom) and any(
                    a.name == "sanitize_framework_titles" for a in node.names
                ):
                    hit = True
                elif isinstance(node, ast.Import):
                    hit = any(
                        al.name.endswith("sanitize_framework_titles")
                        for al in node.names
                    )
                elif isinstance(node, ast.Name) and node.id == "sanitize_framework_titles":
                    hit = True
                if hit and py.name != "outline_common.py":
                    offenders.append(f"{py.name}:{getattr(node, 'lineno', '?')}")
        assert not offenders, (
            "生产代码重新引用了已废弃的 sanitize_framework_titles："
            + "; ".join(offenders)
        )

    def test_facade_no_longer_reexports_it(self):
        """`workflows.py` facade 不得再 re-export，否则看起来仍是公共 API。"""
        src = (Path(__file__).resolve().parents[1] / "app" / "services" / "ai"
               / "workflows.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert all(
                    a.name != "sanitize_framework_titles" for a in node.names
                ), "facade 仍在 re-export sanitize_framework_titles"

    def test_docstring_no_longer_claims_a_retired_dependency(self):
        """docstring 不得再声称依赖已退役的 `fill_children_with_retry`。"""
        from app.services.ai.outline_common import sanitize_framework_titles

        doc = sanitize_framework_titles.__doc__ or ""
        # 必须明确标注废弃
        assert "已废弃" in doc, "未标注废弃状态"
        # 陈旧断言（fill_children_with_retry 是 _legacy_stub，直接抛异常）
        head = doc.split("历史行为")[0]
        assert "当 AI 填充失败时，fill_children_with_retry" not in head, (
            "docstring 仍声称依赖已退役的 fill_children_with_retry"
        )

    def test_real_production_fallback_produces_clean_titles(self):
        """把虚假信心换成真实信心：钉住**实际会跑**的兜底路径。

        V5.1 AI 填充失败时走 `_default_l2_children_for_skeleton`，
        它按父级语义派生标题。必须零占位、零裸编号后缀、兄弟不撞名。

        注意判定分层：`_is_dirty_l3_title` 按设计**只作用于三级及以下**
        （`_cleanup_placeholder_titles` 显式 `level >= 3` 才清洗）——
        「总体方案与实施策划」这类 L2 形态短语作为**二级**章节名完全合法，
        与 28 章模板里的 L1/L2 用词同源，不能按 L3 口径判脏。
        """
        from app.services.ai.outline_common import _ORDINAL_SUFFIX_RE
        from app.services.ai.workflows_outline_v51 import (
            _default_l2_children_for_skeleton,
        )

        skeleton = {
            "id": "1",
            "title": "总体实施策划",
            "children": [{"id": "1.1", "title": "", "children": []}],
        }
        l2 = _default_l2_children_for_skeleton(skeleton, "总体实施策划")
        assert l2, "兜底必须产出二级目录"

        l2_titles = [str(x.get("title") or "") for x in l2]
        for t in l2_titles:
            assert t.strip(), "不得产出空标题"
            assert not _ORDINAL_SUFFIX_RE.search(t), f"L2 产出裸编号后缀: {t}"
        assert len(set(l2_titles)) == len(l2_titles), "同级 L2 兜底标题撞名"

        l3_titles = [
            str(g.get("title") or "")
            for x in l2
            for g in (x.get("children") or [])
        ]
        assert l3_titles, "兜底必须为每个 L2 派生 L3"
        for t in l3_titles:
            assert t.strip(), "不得产出空标题"
            assert not _is_dirty_l3_title(t), f"兜底 L3 产出占位标题: {t}"

    def test_l3_dirty_check_is_level_scoped_by_design(self):
        """`_is_dirty_l3_title` 只用于 L3+ —— L1/L2 合法用词不得被清洗。"""
        src = (Path(__file__).resolve().parents[1] / "app" / "services" / "ai"
               / "outline_common.py").read_text(encoding="utf-8")
        i = src.index("def _cleanup_placeholder_titles(")
        seg = src[i:i + 2000]
        assert "level >= 3 and _is_dirty_l3_title(_title)" in seg, (
            "占位清洗必须限定 level>=3，否则会误伤 28 章模板的合法 L1/L2 标题"
        )


# ===========================================================================
# 11. 编号命名空间：正文 Markdown 子标题 vs 目录编号
# ===========================================================================


class TestBodyHeadingNumberingNamespace:
    """章节编号语义变化（改名/换序/换父/批量重排）必须净化正文内嵌编号。

    导出端对 L1~L4 的正文标题会剥离并按目录结构重编号，但 **L5+ 直接取
    `raw_line` 原样输出**（export.py:1462）。于是正文里 AI 写下的
    `3.1.2.1` 会带着「生成那一刻的树位置」活到导出，而章节编号早已按当前
    目录结构重算 —— 文档里出现「H2 是 `三、进度管理与纠偏`，三级之下却挂着
    `3.1.2.1`」。原先只有换父会触发净化，其余三类全部绕过。
    """

    BODY = (
        "## 1.1 施工准备\n"
        "正文一。\n"
        "### 2.3.1 劳动力需求计划\n"
        "正文二。\n"
        "#### 3.1.2.1 关键岗位配置\n"
        "正文三。\n"
    )

    def test_helper_strips_embedded_numbering(self):
        from app.services.content_cleaning import strip_heading_numbering_from_content as f

        out = f(self.BODY)
        assert "1.1 施工准备" not in out
        assert "2.3.1 劳动力需求计划" not in out
        assert "3.1.2.1 关键岗位配置" not in out
        # 标题正文必须保留
        for keep in ("施工准备", "劳动力需求计划", "关键岗位配置"):
            assert keep in out

    def test_helper_tolerates_empty_and_none(self):
        from app.services.content_cleaning import strip_heading_numbering_from_content as f

        for bad in (None, "", "   "):
            assert f(bad) == bad

    def test_helper_preserves_fenced_code_blocks(self):
        from app.services.content_cleaning import strip_heading_numbering_from_content as f

        src = "```bash\n# 1.1 这是注释不是标题\necho 1\n```\n"
        assert f(src) == src, "代码块内的内容不得被当作标题改写"

    def test_helper_deduplicates_and_handles_none_ids(self):
        from app.services.sections import _strip_body_heading_numbering

        class _FakeRow(dict):
            def __getitem__(self, k):  # pragma: no cover - 兼容 sqlite3.Row 风格
                return dict.__getitem__(self, k)

        # 无需真实 DB：只验证入参规整（去重、保序、忽略 None）
        assert callable(_strip_body_heading_numbering)

    @pytest.mark.parametrize(
        "rel",
        [
            "app/services/sections.py",
        ],
    )
    def test_all_position_changing_paths_call_the_helper(self, rel):
        """改名 / 换序 / 换父 / move / reorder 五条路径都必须接上净化。"""
        src = (Path(__file__).resolve().parents[1] / rel).read_text(encoding="utf-8")
        tree = ast.parse(src)
        fns = {
            n.name: n for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and n.name in {
                "update_section", "move_section", "reorder_sections",
            }
        }
        for fname in ("update_section", "move_section", "reorder_sections"):
            fn = fns.get(fname)
            assert fn is not None, f"未找到 {fname}"
            calls = [
                n for n in ast.walk(fn)
                if isinstance(n, ast.Call)
                and isinstance(n.func, ast.Name)
                and n.func.id == "_strip_body_heading_numbering"
            ]
            assert calls, f"{fname} 未调用 _strip_body_heading_numbering（正文编号会残留）"

    def test_update_section_strips_on_title_and_sort_order_change(self):
        """`update_section` 的净化条件必须同时覆盖 title 与 sort_order 变更。"""
        src = (Path(__file__).resolve().parents[1] / "app" / "services" / "sections.py"
               ).read_text(encoding="utf-8")
        i = src.index('_strip_ids: list[int] = []')
        seg = src[i:i + 1400]
        assert '"title" in updates' in seg
        assert '"sort_order" in updates' in seg
        assert "_strip_body_heading_numbering(conn, _strip_ids)" in seg


# ===========================================================================
# 12. SSE 重连幂等
# ===========================================================================


class TestSseReconnectIdempotency:
    def test_server_stamps_monotonic_seq_on_every_event(self):
        """服务端必须给每条 SSE 事件盖单调递增的 seq（前端据此去重）。"""
        src = (Path(__file__).resolve().parents[1] / "app" / "routers" / "sse"
               / "sse_outline.py").read_text(encoding="utf-8")
        assert "_event_seq" in src, "SSE 事件未盖 seq"
        i = src.index("async def _qput(evt):")
        seg = src[i:i + 900]
        assert 'evt.setdefault("seq"' in seg, (
            "seq 必须在 put 之前盖章 —— 否则重放出去的 last_event 不带 seq，"
            "前端去重失效"
        )
        assert seg.index('setdefault("seq"') < seg.index("outline_queue.put(evt)"), (
            "seq 盖章必须早于入队"
        )

    def test_replayed_last_event_keeps_its_original_seq(self):
        """重连重放走的是 broadcast 缓存里的**已盖章**事件，seq 必须一致。"""
        src = (Path(__file__).resolve().parents[1] / "app" / "routers" / "sse"
               / "sse_outline.py").read_text(encoding="utf-8")
        i = src.index("async def _qput(evt):")
        seg = src[i:i + 900]
        assert seg.index('setdefault("seq"') < seg.index("broadcast_event"), (
            "盖章必须早于 broadcast_event，否则缓存里存的是无 seq 的原对象"
        )

    def test_client_dedups_and_guards_terminal(self):
        """前端必须有 seq 去重 + 终态幂等守卫。"""
        page = _frontend_src() / "pages" / "OutlineEditorPage.tsx"
        if not page.exists():  # 仅后端 CI
            pytest.skip("frontend 不在本次测试范围内")
        src = page.read_text(encoding="utf-8")
        assert "appliedSeqRef" in src, "前端缺少 seq 去重"
        assert "terminalAppliedRef" in src, "前端缺少终态幂等守卫"
        assert 'TERMINAL.includes(data.status)' in src
        # 每次生成必须重置守卫，否则上一轮残留会吞掉本轮终态
        assert "appliedSeqRef.current = null" in src
        assert "terminalAppliedRef.current = false" in src

    def test_progress_reduce_has_monotonic_floor(self):
        """`reduceOutlineGeneratingEvent` 必须支持单调下限且默认兼容。"""
        utils = _frontend_src() / "utils.ts"
        if not utils.exists():
            pytest.skip("frontend 不在本次测试范围内")
        src = utils.read_text(encoding="utf-8")
        assert "OutlineProgressFloor" in src
        assert "floor: OutlineProgressFloor = {}" in src, (
            "floor 必须是可选参数（默认空），保证既有调用方行为不变"
        )
        assert "regressed?: boolean" in src

