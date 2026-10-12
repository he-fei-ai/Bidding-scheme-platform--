"""三级标题占位泛化 / 裸编号后缀问题回归测试（2026-10-10）。

针对用户报告的目录缺陷：

    「三级标题大量是占位式泛化名称（如"专项6""总体实施策划（2）"
      "总体实施策划（3）"），且存在编号冲突后缀（3）、（2）等。」

根因（全部有代码依据，见各修复点注释）：
  1. `_PLACEHOLDER_TITLE_RE = ^专项\\d+$` 只能命中「专项6」，漏掉
     「分阶段推进专项13」这类带前缀变体；
  2. `_uniquify_outline_titles._core()` 归一化不剥离「（第N项）」，
     「总体方案质量管控」与「总体方案质量管控（第6项）」被判不重复而双双保留；
  3. `_semantic_fallback_title` 维度池耗尽后必然产出「父级token+维度（第N项）」；
  4. `_uniquify_outline_titles` 的裸编号回退循环在归一化剥离后必然死循环；
  5. `validate_l3_specificity` / `repair_l3_generic_titles` 长期无生产调用方，
     且 `node.get("level", 0) != 3` 对不带 level 字段的 V5.1 三级节点永远空跑；
  6. `_is_l3_title_project_specific` 只看通用词表 + 长度 ≥7，导致
     「总体实施策划（2）」（9 字、不在通用词表）被误判为具体标题 →
     即便接回了校验入口，整轮校验仍会 passed=True 空跑；
  7. `_L3_SUFFIX_POOL["generic"]` 的 5 个短语（「总体实施策划」「分阶段推进计划」
     「资源配置与协调保障」…）本质是 L2 级章名，域池耗尽后原样落到 L3，
     与别的章节 L2 撞名且读起来像章名而非条目；
  8. `_l3_entity_candidates` 只对外部 `used` 去重、不对本次返回的 `out` 去重，
     实体/维度数量非互质时 `(e,d)` 组合会周期性重复。
  9. 第二轮（遗留建议 1）：过程/制度类词（技术交底、工序交接、见证取样…）
     与动作词做笛卡尔积，产出「作业指导样板引路」「工序交接实测实量」这类
     两个过程名被粘在一起、读不出修饰关系的生硬标题；
 10. 第二轮（遗留建议 3）：`provider is None` 分支完全沉默，
     用户在界面上无法区分「标题已由 AI 精化」与「标题为规则组合生成」；
 11. 第二轮（遗留建议 4）：`repair_l3_generic_titles` 用 `asyncio.gather`
     全量并发，实际并发数等于待修复条数（上限 24），每条都携带 ≤6000 token
     的项目上下文，瞬时打出数十个长上下文请求，撞 provider 速率限制。

运行：cd backend && .venv\\Scripts\\python.exe -m pytest \\
        tests\\test_l3_title_quality_2026_10_10.py -q
"""

import re
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.config import (  # noqa: E402
    OUTLINE_L3_QUALITY_ENHANCE_ENABLED,
    OUTLINE_L3_REPAIR_CONCURRENCY,
    OUTLINE_L3_REPAIR_MAX,
)
from app.services.ai.outline_common import (  # noqa: E402
    _GENERIC_ONLY_TITLES,
    _L3_ENTITY_WORDS,
    _L3_L2LIKE_TITLES,
    _L3_PROCEDURE_DIMS,
    _L3_PROCEDURE_WORDS,
    _cleanup_placeholder_titles,
    _derive_child_title,
    _is_dirty_l3_title,
    _l3_entity_candidates,
    _l3_dims_for,
    _semantic_fallback_title,
    _strip_ordinal_suffix,
    _uniquify_outline_titles,
)
from app.services.ai.workflows_outline_utils import (  # noqa: E402
    _is_l3_title_project_specific,
    validate_l3_specificity,
)

# 复现脚本中的审计口径：占位 / 裸编号后缀 / 纯泛化词
BARE_SUFFIX_RE = re.compile(r"^[^（）]+（[0-9一二三四五六七八九十]+）$")
PLACEHOLDER_RE = re.compile(r"^专项\d+$")
PREFIXED_PLACEHOLDER_RE = re.compile(r"专项\s*\d+")


def _audit(tree):
    """返回 (总数, 占位标题, 裸编号后缀标题, 纯泛化标题, 重复标题, L3与L2撞名)。"""
    total = 0
    dirty, bare, generic, seen = [], [], [], {}
    l2_titles = set()
    for l1 in tree:
        if not isinstance(l1, dict):
            continue
        for l2 in l1.get("children") or []:
            if isinstance(l2, dict) and (l2.get("title") or "").strip():
                l2_titles.add(l2["title"].strip())
    l3_vs_l2 = []
    for l1 in tree:
        if not isinstance(l1, dict):
            continue
        for l2 in l1.get("children") or []:
            if not isinstance(l2, dict):
                continue
            for l3 in l2.get("children") or []:
                if not isinstance(l3, dict):
                    continue
                t = (l3.get("title") or "").strip()
                total += 1
                if t in l2_titles:
                    l3_vs_l2.append(t)
                if not t or PLACEHOLDER_RE.match(t) or PREFIXED_PLACEHOLDER_RE.search(t):
                    dirty.append(t)
                elif BARE_SUFFIX_RE.match(t):
                    bare.append(t)
                elif t in _GENERIC_ONLY_TITLES:
                    generic.append(t)
                else:
                    seen[t] = seen.get(t, 0) + 1
    dup = {t: c for t, c in seen.items() if c > 1}
    return total, dirty, bare, generic, dup, l3_vs_l2


def build_generic_tree():
    """模拟真实标书：4 个一级章，多个二级标题为通用型 / 跨章重复型。"""
    return [
        {"title": "总体施工部署", "children": [
            {"title": "总体方案与实施策划", "children": [{"title": ""} for _ in range(6)]},
            {"title": "总体方案与实施策划", "children": [{"title": ""} for _ in range(6)]},
            {"title": "分阶段推进计划", "children": [{"title": ""} for _ in range(6)]},
        ]},
        {"title": "质量管理措施", "children": [
            {"title": "总体方案与实施策划", "children": [{"title": ""} for _ in range(6)]},
            {"title": "质量保证组织体系", "children": [{"title": ""} for _ in range(6)]},
        ]},
        {"title": "安全管理措施", "children": [
            {"title": "总体方案与实施策划", "children": [{"title": ""} for _ in range(6)]},
            {"title": "分阶段推进计划", "children": [{"title": ""} for _ in range(6)]},
        ]},
        {"title": "进度控制措施", "children": [
            {"title": "总体方案与实施策划", "children": [{"title": ""} for _ in range(6)]},
            {"title": "资源配置与协调保障", "children": [{"title": ""} for _ in range(6)]},
        ]},
    ]


# ===========================================================================
# 1. 占位泛化标题识别器：覆盖所有已知变体
# ===========================================================================


class TestDirtyTitleDetector:
    """脏标题判定必须覆盖用户报告的全部变体，而不是只认「专项6」。"""

    def test_recognizes_all_placeholder_variants(self):
        for bad in (
            "",
            None,
            "专项6",
            "分阶段推进专项13",          # 带前缀变体：旧正则完全漏掉
            "总体实施策划专项6",
            "总体实施策划（2）",          # 裸编号后缀（2）
            "总体实施策划（3）",
            "总体方案质量管控（第6项）",  # 裸编号后缀（第N项）
            "总体实施策划（二）",         # 中文数字
            "总体实施策划",               # 纯泛化词
            "其他",
            "实施要点",
            "专项方案",
        ):
            assert _is_dirty_l3_title(bad), f"漏判脏标题: {bad!r}"

    def test_does_not_false_positive_on_concrete_titles(self):
        for good in (
            "样板区质量控制",
            "关键线路进度计划",
            "地下室防水施工工艺",
            "总平面布置",
            "脚手架专项施工方案",       # 「专项」但不带数字 → 不是占位
            "质量保证组织体系",
        ):
            assert not _is_dirty_l3_title(good), f"误判为脏标题: {good!r}"


class TestOrdinalSuffixStripping:
    """归一化必须剥离裸编号后缀，否则语义重复会无限堆叠。"""

    def test_strips_all_ordinal_forms(self):
        assert _strip_ordinal_suffix("总体实施策划（2）") == "总体实施策划"
        assert _strip_ordinal_suffix("总体实施策划（第3项）") == "总体实施策划"
        assert _strip_ordinal_suffix("总体实施策划（二）") == "总体实施策划"
        assert _strip_ordinal_suffix("总体实施策划（ 4 ）") == "总体实施策划"

    def test_preserves_meaningful_parenthetical_content(self):
        # 非裸编号的括号内容不得被剥离
        assert _strip_ordinal_suffix("深基坑（超5m）支护方案") == "深基坑（超5m）支护方案"


# ===========================================================================
# 2. 工程实体 × 动作维度组合池
# ===========================================================================


class TestEntityCandidates:
    """组合池必须产出结构化、可读、互不重复的具体标题。"""

    def test_candidates_are_concrete_and_non_dirty(self):
        used = set()
        for dom_parent in ("总体方案与实施策划", "资源配置与协调保障"):
            cands = _l3_entity_candidates(dom_parent, 0, used)
            assert len(cands) >= 20, f"候选过少: {dom_parent} → {len(cands)}"
            for c in cands:
                assert not _is_dirty_l3_title(c), f"候选本身是脏标题: {c}"
                used.add(c)

    def test_candidates_cover_real_entities(self):
        cands = _l3_entity_candidates("总体方案与实施策划", 0, set())
        joined = "".join(cands)
        # 必须落到真实工程实体，而不是抽象管理词
        assert any(
            ent in joined for ent in ("地下室", "主体结构", "屋面", "基坑")
        ), joined[:120]

    def test_generic_domain_does_not_produce_mangled_phrases(self):
        """generic 域回退到 work 域，不得产出「阶段目标总体实施策划」这类伪标题。"""
        cands = _l3_entity_candidates("总体方案与实施策划", 0, set())
        for c in cands:
            for bad in ("阶段目标总体", "目标总体实施策划", "总体实施策划总体"):
                assert bad not in c, f"拼接出伪标题: {c}"
            assert len(c) <= 14, f"标题过长: {c}"

    def test_entity_candidates_are_unique_within_one_call(self):
        cands = _l3_entity_candidates("质量保证组织体系", 0, set())
        assert len(cands) == len(set(cands))


class TestEntityCandidateNaturalness:
    """过程/制度类实体必须接名词性维度，不得两个过程词直接粘在一起。

    2026-10-10 遗留建议 1：旧实现把过程词当实体词与动作词池做笛卡尔积，
    产出「作业指导样板引路」「工序交接实测实量」「见证取样检测试验」这类
    两个过程名被粘在一起、读不出修饰关系的生硬标题。
    """

    def _procedure_prefix(self, cand):
        """返回 cand 开头的过程/制度词（按最长优先），无则返回 None。"""
        for w in sorted(_L3_PROCEDURE_WORDS, key=len, reverse=True):
            if cand.startswith(w):
                return w
        return None

    def test_no_double_procedure_concatenation(self):
        """不得出现「过程词 + 过程词」的拼接。"""
        used = set()
        all_cands: list[str] = []
        for idx in range(16):
            all_cands += _l3_entity_candidates("总体方案与实施策划", idx, used)
            used.update(all_cands)
        assert all_cands, "候选池为空"
        for c in all_cands:
            w = self._procedure_prefix(c)
            if w is None:
                continue
            rest = c[len(w):]
            assert rest not in _L3_PROCEDURE_WORDS, f"双过程词拼接: {c}"

    def test_procedure_entities_pair_with_noun_dimensions(self):
        """过程/制度类实体必须接 _L3_PROCEDURE_DIMS 里的名词性维度。"""
        used = set()
        for parent in ("质量保证组织体系", "总体方案与实施策划", "分阶段推进计划"):
            for idx in range(8):
                for c in _l3_entity_candidates(parent, idx, used):
                    w = self._procedure_prefix(c)
                    if w is None:
                        continue
                    assert c[len(w):] in _L3_PROCEDURE_DIMS, (
                        f"过程词「{w}」未接名词性维度: {c}"
                    )
                    used.add(c)

    def test_dims_selector_is_domain_aware(self):
        """过程词走专属维度池，实体词走原动作池。"""
        actions = _l3_dims_for("地下室", ("质量控制",))
        assert actions == ("质量控制",)
        assert _l3_dims_for("作业指导", ("质量控制",)) == _L3_PROCEDURE_DIMS


class TestL2LikeTitleBlocking:
    """L2 形态短语不得落到 L3 —— 读起来像章名，且极易与其它章节 L2 撞名。

    2026-10-10 遗留建议 2：`_L3_SUFFIX_POOL["generic"]` 的 5 个短语
    （「总体实施策划」「分阶段推进计划」「资源配置与协调保障」…）本质是
    二级章名，域池耗尽后原样落到三级。
    """

    def test_l2_like_phrases_are_marked_dirty(self):
        assert _L3_L2LIKE_TITLES, "L2 形态短语词表为空"
        for t in ("总结评估与持续改进", "资源配置与协调保障",
                  "分阶段推进计划", "总体方案与实施策划"):
            assert _is_dirty_l3_title(t), f"漏判 L2 形态短语: {t}"

    def test_generic_pools_no_longer_hold_l2_chapter_names(self):
        """generic 池本身不得再放 L2 级章名（源头切断）。"""
        import app.services.ai.outline_common as oc

        for pool_name, pool in (
            ("_L3_SUFFIX_POOL[generic]", oc._L3_SUFFIX_POOL.get("generic", ())),
            ("_L3_SHORT_ACTIONS[generic]", oc._L3_SHORT_ACTIONS.get("generic", ())),
        ):
            for phrase in pool:
                assert phrase not in _L3_L2LIKE_TITLES, (
                    f"{pool_name} 仍含 L2 章名: {phrase}"
                )

    def test_l1_l2_legal_words_stay_out_of_the_blocklist(self):
        """「其他」「说明」「概述」在 L1/L2 合法，不得并进 L3 禁用表。"""
        for w in ("其他", "说明", "概述"):
            assert w in _GENERIC_ONLY_TITLES, w
            assert w not in _L3_L2LIKE_TITLES, f"合法章节词被误列: {w}"

    def test_entity_pool_and_dimension_pool_stay_consistent(self):
        """新增的过程词/维度词必须是有效中文，且长度受控。"""
        for w in _L3_PROCEDURE_WORDS:
            assert 2 <= len(w) <= 6, f"过程词长度异常: {w}"
            assert all("\u4e00" <= ch <= "\u9fff" for ch in w), f"过程词含非中文: {w}"
        for d in _L3_PROCEDURE_DIMS:
            assert 1 <= len(d) <= 4, f"维度词长度异常: {d}"
            assert all("\u4e00" <= ch <= "\u9fff" for ch in d), f"维度词含非中文: {d}"
        assert _L3_PROCEDURE_WORDS.isdisjoint(_L3_PROCEDURE_DIMS), (
            "过程词与维度词不应重叠（会拼出叠词标题）"
        )

    def test_procedure_word_count_covers_the_entity_pool(self):
        """实体池里的过程类词应基本都被 _L3_PROCEDURE_WORDS 覆盖。

        覆盖不全时该实体只能接动作词池，会退回「过程词 + 动作词」的生硬拼接，
        此测试用于提醒后续维护者同步维护两个词表。
        """
        covered = {
            e for pool in _L3_ENTITY_WORDS.values() for e in pool
            if e in _L3_PROCEDURE_WORDS
        }
        assert covered, "没有任何实体词被识别为过程词"
        assert len(covered) >= 12, f"过程词覆盖不足: {sorted(covered)}"




# ===========================================================================
# 3. 语义化兜底：永不产出「（第N项）」
# ===========================================================================


class TestSemanticFallbackNoOrdinal:
    """兜底标题在池耗尽时也不得回退到裸编号后缀。"""

    @pytest.mark.parametrize("parent", [
        "总体方案与实施策划",            # 泛化父级 → 走实体组合
        "脚手架总体施工部署与区段划分",    # 专业父级 → 保留原有维度词路径
        "",                              # 空父级
    ])
    def test_never_emits_bare_ordinal_suffix(self, parent):
        used = set()
        titles = [_semantic_fallback_title(parent, i, used) for i in range(40)]
        assert len(set(titles)) == len(titles), "兜底标题未保证唯一"
        for t in titles:
            assert not BARE_SUFFIX_RE.match(t), f"产出裸编号后缀: {t}"
            assert not t.endswith("项）"), f"产出（第N项）: {t}"

    def test_generic_parent_falls_back_to_concrete_entities(self):
        used = set()
        for i in range(12):
            t = _semantic_fallback_title(
                "总体方案与实施策划", i, used, context_title="质量管理措施"
            )
            assert not _is_dirty_l3_title(t), f"仍产出脏标题: {t}"


# ===========================================================================
# 4. 派生标题：泛化 L2 必须继承 L1 专业领域
# ===========================================================================


class TestDeriveChildFromGenericL2:
    """L2 是通用型时，L3 应从 L1 继承专业领域，而非退化成泛化短语。"""

    def test_generic_l2_under_quality_l1_yields_concrete_titles(self):
        used = set()
        titles = []
        for i in range(6):
            t = _derive_child_title(
                "总体方案与实施策划", i, used, level=3,
                grandparent_title="质量管理措施",
            )
            assert not _is_dirty_l3_title(t), f"泛化 L2 派生出脏标题: {t}"
            used.add(t)
            titles.append(t)
        assert len(set(titles)) == 6

    def test_professional_l2_keeps_semantic_isolation(self):
        # 2026-09-04 用户定制规则 3：L3 完全独立生成，不得继承父级核心短语。
        # 语义域判定为 work（非 generic）时走过滤池路径，前缀拼合被禁用。
        t = _derive_child_title("脚手架总体施工部署", 0, set(), level=3)
        assert not _is_dirty_l3_title(t), f"专业 L2 派生出脏标题: {t}"
        assert "总体施工部署" not in t, "违反规则 3：L3 复制了父级核心短语"
        assert t != "脚手架总体施工部署"

    def test_level2_path_is_unchanged(self):
        # 改动只作用于 level==3，L2 派生路径保持原样
        assert _derive_child_title("质量管理措施", 0, set(), level=2)


# ===========================================================================
# 5. 全局去重：语义重复不再穿透
# ===========================================================================


class TestUniquifyOrdinalDuplicates:
    """归一化剥离裸编号后，语义重复标题必须被合并消歧。"""

    def test_ordinal_variants_are_collapsed(self):
        tree = [
            {"title": "第一章", "children": [
                {"title": "一、总体方案与实施策划", "children": [
                    {"title": "总体方案质量管控"},
                    {"title": "总体方案质量管控（第2项）"},
                    {"title": "总体方案质量管控（第3项）"},
                    {"title": "总体方案质量管控（第6项）"},
                ]}
            ]}
        ]
        out = _uniquify_outline_titles(tree)
        titles = [c["title"] for c in out[0]["children"][0]["children"]]
        assert titles[0] == "总体方案质量管控"
        assert len(titles) == 4
        assert len(set(titles)) == 4, f"仍存在重复: {titles}"
        for t in titles:
            assert not BARE_SUFFIX_RE.match(t), f"消歧结果仍带裸编号: {t}"

    def test_no_infinite_loop_when_core_collides(self):
        """回归：归一化剥离裸编号后，旧回退循环必然死循环。"""
        tree = [
            {"title": "第一章", "children": [
                {"title": "一、总体方案", "children": [
                    {"title": "总体实施策划"},
                    {"title": "总体实施策划（2）"},
                    {"title": "总体实施策划（3）"},
                    {"title": "总体实施策划（4）"},
                    {"title": "总体实施策划（5）"},
                ]}
            ]}
        ]
        # 若死循环，本测试会挂起超时（旧实现必然触发）
        out = _uniquify_outline_titles(tree)
        titles = [c["title"] for c in out[0]["children"][0]["children"]]
        assert len(set(titles)) == 5

    def test_uniquify_is_idempotent(self):
        tree = [
            {"title": "A", "children": [
                {"title": "A1", "children": [{"title": "样板区质量控制"}]}
            ]}
        ]
        once = _uniquify_outline_titles(tree)
        twice = _uniquify_outline_titles(once)
        assert twice[0]["children"][0]["children"][0]["title"] == "样板区质量控制"


# ===========================================================================
# 6. 占位清洗：层级作用域
# ===========================================================================


class TestCleanupPlaceholderLevelScope:
    """脏标题清洗只应作用于三级及以下，不得误伤合法的一/二级章节标题。"""

    def test_l1_generic_title_is_preserved(self):
        # 28 章模板的合法一级标题（FIXED_28_CHAPTERS[27] == "其他"）
        tree = [
            {"title": "其他", "children": [
                {"title": "其他说明", "children": [{"title": "专项6"}]}
            ]}
        ]
        fixed = _cleanup_placeholder_titles(tree)
        assert tree[0]["title"] == "其他", "合法一级标题被误改"
        assert tree[0]["children"][0]["title"] == "其他说明", "合法二级标题被误改"
        assert fixed == 1
        assert tree[0]["children"][0]["children"][0]["title"] != "专项6"

    def test_prefix_placeholder_variant_is_cleaned(self):
        """「分阶段推进专项13」这类带前缀变体旧正则完全漏掉。"""
        tree = [
            {"title": "一、总体施工部署", "children": [
                {"title": "二、分阶段推进", "children": [
                    {"title": "分阶段推进专项13"},
                    {"title": "分阶段推进专项14"},
                ]}
            ]}
        ]
        fixed = _cleanup_placeholder_titles(tree)
        assert fixed == 2, f"应清洗 2 个，实际 {fixed}"
        titles = [c["title"] for c in tree[0]["children"][0]["children"]]
        assert len(set(titles)) == 2, f"清洗后仍重复: {titles}"

    def test_non_dict_children_do_not_crash(self):
        tree = [
            {"title": "A", "children": [
                "不是字典",
                {"title": "A1", "children": [{"title": "专项6"}]},
            ]}
        ]
        assert _cleanup_placeholder_titles(tree) == 1


# ===========================================================================
# 7. L3 具体性校验：不再依赖 level 字段
# ===========================================================================


class TestValidateL3Specificity:
    """V5.1 三级节点不带 level 字段，旧实现永远空跑。"""

    def test_detects_generic_titles_without_level_field(self):
        tree = [
            {"title": "总体施工部署", "children": [
                {"title": "总体方案与实施策划", "children": [
                    {"title": "总体实施策划（2）"},
                    {"title": "样板区质量控制"},
                ]}
            ]}
        ]
        passed, warnings = validate_l3_specificity(tree)
        assert passed is False, "未检测到占位标题"
        assert any("总体实施策划（2）" in w["title"] for w in warnings)
        # 结构化具体标题不应被误报
        assert all(w["title"] != "样板区质量控制" for w in warnings)

    def test_works_with_explicit_level_field(self):
        tree = [
            {"title": "A", "level": 1, "children": [
                {"title": "A1", "level": 2, "children": [
                    {"title": "总体实施策划（2）", "level": 3}
                ]}
            ]}
        ]
        passed, warnings = validate_l3_specificity(tree)
        assert not passed and len(warnings) == 1

    def test_entity_action_titles_pass(self):
        tree = [
            {"title": "质量管理措施", "children": [
                {"title": "质量保证组织体系", "children": [
                    {"title": "样板区质量控制"},
                    {"title": "见证取样检测试验"},
                    {"title": "关键工序旁站监督"},
                ]}
            ]}
        ]
        passed, warnings = validate_l3_specificity(tree)
        assert passed is True, f"结构化具体标题被误报: {warnings}"

    def test_empty_and_malformed_inputs(self):
        assert validate_l3_specificity([]) == (True, [])
        assert validate_l3_specificity(None)[0] is True
        passed, warnings = validate_l3_specificity([
            "不是字典",
            {"title": "A", "children": None},
        ])
        assert passed is True and warnings == []


class TestProjectSpecificityRecognizer:
    """含工程实体词的结构化标题应直接判为通过，不触发无谓的 AI 修复。"""

    def test_entity_action_titles_recognized_as_specific(self):
        for t in ("样板区质量控制", "关键线路进度计划", "地下室防水施工工艺"):
            assert _is_l3_title_project_specific(t), t

    def test_pure_generic_titles_still_rejected(self):
        assert not _is_l3_title_project_specific("总体实施策划（2）")
        assert not _is_l3_title_project_specific("实施要点")
        assert not _is_l3_title_project_specific("总体方案")


class TestL3GenericTitleRepair:
    """AI 修复路径：签名可调用、结果经脏标题校验、拒绝占位返回。"""

    class _FakeProvider:
        def __init__(self, replies):
            self._replies = list(replies)
            self.calls = 0

        async def chat(self, messages, **kwargs):
            self.calls += 1
            return self._replies.pop(0) if self._replies else "兜底标题"

    def _tree_with_placeholder(self):
        return [
            {"title": "总体施工部署", "children": [
                {"title": "总体方案与实施策划", "children": [
                    {"title": "总体实施策划（2）"},
                    {"title": "样板区质量控制"},
                ]}
            ]}
        ]

    @pytest.mark.asyncio
    async def test_rejects_placeholder_ai_reply(self):
        """AI 复读占位标题时必须改用规则兜底，不得把占位结果写进目录。

        BUG-FIX 2026-10-12：旧行为是「拒绝 AI 结果 + 原样保留脏标题」，
        于是「总体实施策划（2）」「专项6」原封不动流到最终审核与落库。
        现在 AI 不可用时回落到 `_semantic_fallback_title`（工程实体 × 专业维度），
        保证任何分支下都不会留下占位标题。
        """
        from app.services.ai.workflows_outline_utils import repair_l3_generic_titles

        provider = self._FakeProvider(["专项7", "总体实施策划（3）"])
        tree = self._tree_with_placeholder()
        out = await repair_l3_generic_titles(
            provider, tree, "", "", "", "", "", max_repair=2
        )
        titles = [c["title"] for c in out[0]["children"][0]["children"]]
        assert not any(_is_dirty_l3_title(t) for t in titles), f"仍有占位标题: {titles}"
        assert titles[1] == "样板区质量控制", "未脏标题不得被误改"
        assert titles[0] != "总体实施策划（2）", f"脏标题被保留: {titles}"

    @pytest.mark.asyncio
    async def test_accepts_concrete_ai_reply(self):
        from app.services.ai.workflows_outline_utils import repair_l3_generic_titles

        provider = self._FakeProvider(["地下室结构实测实量方案"])
        tree = self._tree_with_placeholder()
        out = await repair_l3_generic_titles(
            provider, tree, "", "", "", "", "", max_repair=2
        )
        titles = [c["title"] for c in out[0]["children"][0]["children"]]
        assert titles[0] == "地下室结构实测实量方案", titles
        assert titles[1] == "样板区质量控制"
        assert provider.calls >= 1

    @pytest.mark.asyncio
    async def test_no_warnings_returns_deepcopy_untouched(self):
        from app.services.ai.workflows_outline_utils import repair_l3_generic_titles

        provider = self._FakeProvider([])
        tree = self._tree_with_placeholder()
        tree[0]["children"][0]["children"][0]["title"] = "样板区质量控制"
        out = await repair_l3_generic_titles(
            provider, tree, "", "", "", "", "", max_repair=2
        )
        assert out is not tree, "必须返回深拷贝而非原对象"
        assert out[0]["children"][0]["children"][0]["title"] == "样板区质量控制"
        assert provider.calls == 0

    @pytest.mark.asyncio
    async def test_provider_exception_does_not_raise(self):
        """AI 异常不得向上传播（增强环节，不能阻断目录生成）。

        BUG-FIX 2026-10-12：异常同样走规则兜底，绝不把脏标题留给用户。
        """
        from app.services.ai.workflows_outline_utils import repair_l3_generic_titles

        class _BoomProvider:
            async def chat(self, messages, **kwargs):
                raise RuntimeError("provider down")

        tree = self._tree_with_placeholder()
        out = await repair_l3_generic_titles(
            _BoomProvider(), tree, "", "", "", "", "", max_repair=2
        )
        titles = [c["title"] for c in out[0]["children"][0]["children"]]
        assert not any(_is_dirty_l3_title(t) for t in titles), f"异常后仍是脏标题: {titles}"

    @pytest.mark.asyncio
    async def test_concurrency_is_bounded_by_config(self):
        """2026-10-10 遗留建议 4：旧实现 asyncio.gather 全量并发，
        实际并发数等于待修复条数，会瞬时打出数十个长上下文请求。"""
        import asyncio as _asyncio

        from app.services.ai.workflows_outline_utils import repair_l3_generic_titles

        in_flight = 0
        peak = 0
        counter = 0

        class _SlowProvider:
            async def chat(self, messages, **kwargs):
                nonlocal in_flight, peak, counter
                counter += 1
                in_flight += 1
                peak = max(peak, in_flight)
                await _asyncio.sleep(0.02)
                in_flight -= 1
                return f"工序交接流程{counter}"

        dirty = [
            "总体实施策划（2）", "专项6", "实施要点", "总体实施策划（3）",
            "专项7", "实施要点（2）", "专项8", "总体实施策划（4）",
        ]
        tree = [
            {"title": "总体施工部署", "children": [
                {"title": "总体方案与实施策划", "children": [
                    {"title": t} for t in dirty
                ]}
            ]}
        ]
        out = await repair_l3_generic_titles(
            _SlowProvider(), tree, "", "", "", "", "", max_repair=24
        )
        assert counter >= 2, f"待修复任务不足，无法验证并发: counter={counter}"
        # 硬上限：任一时刻在飞请求数不得超过配置并发上限
        assert peak <= OUTLINE_L3_REPAIR_CONCURRENCY, (
            f"并发超出上限: peak={peak} > {OUTLINE_L3_REPAIR_CONCURRENCY}"
        )
        # 反向验证：信号量不得过度限流（否则会退化成串行）
        assert peak == min(counter, OUTLINE_L3_REPAIR_CONCURRENCY), (
            f"并发数异常: peak={peak}, counter={counter}, "
            f"上限={OUTLINE_L3_REPAIR_CONCURRENCY}"
        )
        titles = [c["title"] for c in out[0]["children"][0]["children"]]
        assert len(titles) == len(dirty)
        for t in titles:
            assert not _is_dirty_l3_title(t), f"修复后仍是脏标题: {t}"

    @pytest.mark.asyncio
    async def test_concurrency_cap_of_one_serializes(self):
        """并发上限为 1 时应严格串行（供低速率 provider 使用）。"""
        import asyncio as _asyncio

        from app.services.ai.workflows_outline_utils import repair_l3_generic_titles
        import app.services.ai.workflows_outline_utils as uo

        in_flight = 0
        peak = 0
        counter = 0

        class _SlowProvider:
            async def chat(self, messages, **kwargs):
                nonlocal in_flight, peak, counter
                counter += 1
                in_flight += 1
                peak = max(peak, in_flight)
                await _asyncio.sleep(0.01)
                in_flight -= 1
                return f"工序交接要点{counter}"

        old = uo.OUTLINE_L3_REPAIR_CONCURRENCY
        uo.OUTLINE_L3_REPAIR_CONCURRENCY = 1
        try:
            tree = self._tree_with_placeholder()
            tree[0]["children"][0]["children"] = [
                {"title": "总体实施策划（2）"},
                {"title": "总体实施策划（3）"},
            ]
            await repair_l3_generic_titles(
                _SlowProvider(), tree, "", "", "", "", "", max_repair=2
            )
        finally:
            uo.OUTLINE_L3_REPAIR_CONCURRENCY = old
        assert peak == 1, f"并发上限 1 时仍出现并发: peak={peak}"


class TestLocateByPath:
    """O-5 修复依赖的路径递归定位助手：必须支持任意深度，不得硬编码 3 层。"""

    def _tree(self):
        return [
            {"title": "L1", "children": [
                {"title": "L2", "children": [
                    {"title": "L3", "children": [
                        {"title": "L4"},
                    ]},
                ]},
            ]},
        ]

    def test_three_level_path(self):
        from app.services.ai.workflows_outline_utils import _locate_by_path

        node, parent_title, ancestors = _locate_by_path(self._tree(), "1.1.1")
        assert node["title"] == "L3"
        assert parent_title == "L2"
        assert ancestors == ["L1", "L2"]

    def test_four_level_path_is_supported(self):
        """核心回归：L4 节点可定位（旧实现 `len(parts) != 3` 会跳过）。"""
        from app.services.ai.workflows_outline_utils import _locate_by_path

        node, parent_title, ancestors = _locate_by_path(self._tree(), "1.1.1.1")
        assert node["title"] == "L4"
        assert parent_title == "L3"
        assert ancestors == ["L1", "L2", "L3"]

    def test_illegal_or_out_of_range_path_returns_none(self):
        from app.services.ai.workflows_outline_utils import _locate_by_path

        assert _locate_by_path(self._tree(), "9.9.9") == (None, "", [])
        assert _locate_by_path(self._tree(), "1.1.1.1.1") == (None, "", [])
        assert _locate_by_path(self._tree(), "not-a-path") == (None, "", [])
        assert _locate_by_path(self._tree(), "") == (None, "", [])
        assert _locate_by_path([], "1.1.1") == (None, "", [])


class TestL3RepairHandlesDeepNesting:
    """O-5 回归：L4+ 嵌套下的脏 L3 标题必须被修复，不得被 `len(parts) != 3` 跳过。"""

    class _FakeProvider:
        def __init__(self, replies):
            self._replies = list(replies)
            self.calls = 0

        async def chat(self, messages, **kwargs):
            self.calls += 1
            return self._replies.pop(0) if self._replies else "合规兜底标题"

    @pytest.mark.asyncio
    async def test_l4_nested_dirty_title_is_repaired(self):
        """AI 在 L3 下挂了 L4 子节点并显式标记 level=3 时，
        validate 会产出 4 段 path（如 1.1.1.1）。旧实现硬编码 3 层会静默漏修。"""
        from app.services.ai.workflows_outline_utils import repair_l3_generic_titles

        tree = [
            {"title": "总体施工部署", "level": 1, "children": [
                {"title": "总体方案与实施策划", "level": 2, "children": [
                    {"title": "总体实施策划(2)", "level": 3, "children": [
                        {"title": "地下室监测实施要点", "level": 4, "children": []},
                    ]},
                ]},
            ]},
        ]
        provider = self._FakeProvider(["基坑支护监测方案"])
        out = await repair_l3_generic_titles(
            provider, tree, "", "", "", "", "", max_repair=2
        )
        assert provider.calls == 1, "L4 场景下 LLM 未被调用，说明脏标题被跳过了"
        new_title = out[0]["children"][0]["children"][0]["title"]
        assert new_title == "基坑支护监测方案", f"脏标题未被修复: {new_title}"

    @pytest.mark.asyncio
    async def test_illegal_path_is_skipped_safely(self):
        """路径越界不得抛异常、不得写坏树，其余合法项仍应正常修复。"""
        from app.services.ai.workflows_outline_utils import repair_l3_generic_titles

        tree = [
            {"title": "总体施工部署", "children": [
                {"title": "总体方案与实施策划", "children": [
                    {"title": "总体实施策划（2）"},
                ]},
            ]},
        ]
        # 手工注入一个越界 path，模拟 validate 结果与树不一致的极端情况。
        import app.services.ai.workflows_outline_utils as _u

        real_validate = _u.validate_l3_specificity
        _u.validate_l3_specificity = lambda outline: (
            [{"path": "9.9.9", "title": "总体实施策划（2）",
              "parent_title": "总体方案与实施策划", "reason": "generic"}],
            "",
        )
        try:
            provider = self._FakeProvider(["基坑支护监测方案"])
            out = await repair_l3_generic_titles(
                provider, tree, "", "", "", "", "", max_repair=2
            )
        finally:
            _u.validate_l3_specificity = real_validate
        assert provider.calls == 0, "越界 path 不得触发 AI 调用"
        assert out[0]["children"][0]["children"][0]["title"] == "总体实施策划（2）"


class TestL3QualityNoProviderWiring:
    """无 AI provider 时规则兜底标题必须显式暴露（遗留建议 3）。

    旧行为：`provider is None` 分支完全沉默，用户在界面上无法区分
    「标题已由 AI 精化」与「标题为规则组合生成」，只能靠肉眼比对质量。
    接线类改动若被误删，pytest 不会失败 —— 因此按 test_outline_asyncio.py
    `test_sse_handlers_no_unused_import` 的既有惯例，用源码断言守住接线。
    """

    def _block(self) -> str:
        """主链路收口块 + 被它委托的 `_l3_specificity_closeout` 实现。

        BUG-FIX 2026-10-12：主链路的「具体性校验 → AI 修复」内联逻辑已抽成
        `_l3_specificity_closeout`（与审核回灌路径共用，消除两份易漂移的副本）。
        本类断言的语义是「这两个函数确实被调用、且失败不阻断生成」，
        因此扫描范围必须同时覆盖调用块与被调用函数体，否则会在重构后误报。
        """
        from app.services.ai import workflows_outline_v51 as v51

        src = Path(v51.__file__).read_text(encoding="utf-8")
        start = src.index("L3 标题质量收口")
        end = src.index("# ===== AI 自检 =====")
        main_block = src[start:end]
        h_start = src.index("async def _l3_specificity_closeout(")
        h_end = src.index("async def _apply_l3_quality_closeout(")
        return main_block + "\n# ---- 委托实现 ----\n" + src[h_start:h_end]

    def test_dead_code_is_actually_invoked(self):
        """本轮修复核心：两个函数必须在 v51 主链路里被真正调用。"""
        from app.services.ai import workflows_outline_v51 as v51

        assert "validate_l3_specificity" in v51.__dict__, "validate 未从 utils 导入"
        assert "repair_l3_generic_titles" in v51.__dict__, "repair 未从 utils 导入"
        src = Path(v51.__file__).read_text(encoding="utf-8")
        assert "_l3_specificity_closeout(" in src, "主链路未调用具体性收口函数"
        block = self._block()
        assert "validate_l3_specificity(outline)" in block, "validate 未被调用"
        assert "repair_l3_generic_titles(" in block, "repair 未被调用"

    def test_quality_block_is_gated_by_config(self):
        """总开关必须包住整块，false 才能完全回退到历史行为。"""
        from app.config import OUTLINE_L3_QUALITY_ENHANCE_ENABLED

        assert OUTLINE_L3_QUALITY_ENHANCE_ENABLED is True
        block = self._block()
        assert "OUTLINE_L3_QUALITY_ENHANCE_ENABLED" in block, "收口块未受开关控制"

    def test_no_provider_branch_is_not_silent(self):
        """provider 缺失时必须落 warning 日志并向前端提示，不得静默跳过。"""
        block = self._block()
        assert "elif not _passed:" in block, "缺少无 provider 分支（静默跳过）"
        seg = block[block.index("elif not _passed:"):]
        assert "logger.warning" in seg, "无 provider 时未落 warning 日志"
        assert "await phase_callback(" in seg, "无 provider 时未向前端提示"

    def test_exception_never_blocks_generation(self):
        """质量收口是增强环节，任何异常都不得阻断目录生成。"""
        block = self._block()
        assert "except Exception" in block, "缺少异常兜底"

    def test_repair_concurrency_is_bounded(self):
        """AI 修复并发必须受信号量约束（遗留建议 4）。"""
        from app.services.ai import workflows_outline_utils as uo

        src = Path(uo.__file__).read_text(encoding="utf-8")
        assert "asyncio.Semaphore" in src, "未接入信号量限流"
        assert "OUTLINE_L3_REPAIR_CONCURRENCY" in src
        assert src.index("asyncio.Semaphore") < src.index("asyncio.gather("), (
            "信号量必须在 gather 之前创建"
        )


# ===========================================================================
# 8. 端到端：真实生成链路零脏标题
# ===========================================================================


class TestL3GenerationEndToEnd:
    """完整规则链路（生成 → 差异化 → 清洗 → 去重）后不得残留任何脏标题。"""

    @pytest.mark.asyncio
    async def test_no_dirty_titles_after_full_pipeline(self):
        from app.services.ai.workflows_outline_v51 import (
            _ensure_l2_l3_differentiation,
            _generate_level3_v51,
        )

        tree = build_generic_tree()
        await _generate_level3_v51(None, tree, "", "")
        _ensure_l2_l3_differentiation(tree)
        _cleanup_placeholder_titles(tree)
        tree = _uniquify_outline_titles(tree)

        total, dirty, bare, generic, dup, l3_vs_l2 = _audit(tree)
        assert total == 54
        assert not dirty, f"残留占位标题: {dirty}"
        assert not bare, f"残留裸编号后缀: {bare}"
        assert not generic, f"残留纯泛化标题: {generic}"
        assert not dup, f"残留跨章节重复: {dup}"
        # L3 不得与任何 L2 标题撞名（否则 L3 读起来像章名而非条目）
        assert not l3_vs_l2, f"L3 与 L2 撞名: {l3_vs_l2}"

    @pytest.mark.asyncio
    async def test_all_titles_are_non_empty_and_bounded(self):
        from app.services.ai.workflows_outline_v51 import (
            _ensure_l2_l3_differentiation,
            _generate_level3_v51,
        )

        tree = build_generic_tree()
        await _generate_level3_v51(None, tree, "", "")
        _ensure_l2_l3_differentiation(tree)
        _cleanup_placeholder_titles(tree)
        tree = _uniquify_outline_titles(tree)
        for l1 in tree:
            for l2 in l1.get("children") or []:
                for l3 in l2.get("children") or []:
                    t = l3.get("title") or ""
                    assert 4 <= len(t) <= 22, f"标题长度越界: {t!r}"


# ===========================================================================
# 8b. 直出路径（短标书直出）同样经过 L3 质量收口（BUG-2026-1009-01/02/03）
# ===========================================================================


class TestDirectPathQualityCloseout:
    """直出路径在返回前必须执行与分步路径同套的 L3 质量收口。

    背景：短标书直出模式（generate_outline_direct 成功后）此前在
    generate_outline_v51 中直接 return，绕过分步路径的「清洗 → 去重 →
    具体性校验 → AI 修复」防线，直出产物仍可能带「专项6」「总体实施策划（2）」
    这类脏标题。修复：复用 _apply_l3_quality_closeout，先收口再进入最终审核。
    本类用源码接线断言 + 行为断言双重锁定。
    """

    def test_direct_branch_invokes_quality_closeout(self):
        """直出成功分支（return direct 之前）必须调用共用收口函数。"""
        from app.services.ai import workflows_outline_v51 as v51

        assert callable(v51._apply_l3_quality_closeout), "共用收口函数缺失"
        # 直出分支在 return 之前调用收口（确保产物经过清洗/去重/校验）
        src = Path(v51.__file__).read_text(encoding="utf-8")
        start = src.index("if should_use_direct_outline(scoring_points, outline_mode):")
        end = src.index('logger.warning("[V5.1] 直出路径未产出结果，回退分步路径")')
        direct_seg = src[start:end]
        assert "_apply_l3_quality_closeout(" in direct_seg, "直出路径未接入共用质量收口"
        # 收口调用必须位于 return direct 之前
        assert direct_seg.index("_apply_l3_quality_closeout(") < direct_seg.index("return direct"), (
            "质量收口必须发生在 return direct 之前"
        )

    @pytest.mark.asyncio
    async def test_closeout_cleans_dirty_direct_title(self):
        """共用收口函数：含脏标题的直出产物被清洗为无脏标题。"""
        from app.services.ai.workflows_outline_v51 import _apply_l3_quality_closeout

        tree = build_generic_tree()[:1]
        # 人为注入直出路径会产出的脏标题形态
        tree[0]["children"] = [
            {"title": "总体方案与实施策划", "children": [
                {"title": "总体实施策划（2）"},
                {"title": "专项6"},
                {"title": "分阶段推进专项13"},
                {"title": "资源配置与协调保障"},
            ]},
            {"title": "总体方案与实施策划", "children": [
                {"title": "总体实施策划（3）"},
                {"title": "专项7"},
                {"title": "样板区质量控制"},
            ]},
        ]
        out = await _apply_l3_quality_closeout(None, tree, "", "", "", "")
        for l1 in out:
            for l2 in l1.get("children") or []:
                for l3 in l2.get("children") or []:
                    t = l3.get("title") or ""
                    assert not _is_dirty_l3_title(t), f"直出收口后仍残留脏标题: {t}"
                    assert 4 <= len(t) <= 22, f"标题长度越界: {t!r}"
        # 清洗后不得出现重复三级标题（跨章节去重）
        seen: set[str] = set()
        for l1 in out:
            for l2 in l1.get("children") or []:
                for l3 in l2.get("children") or []:
                    t = l3.get("title") or ""
                    assert t not in seen, f"直出收口后仍残留重复标题: {t}"
                    seen.add(t)

    @pytest.mark.asyncio
    async def test_closeout_is_idempotent_and_non_blocking(self):
        """共用收口函数：对已干净标题无副作用；异常不阻断。"""
        from app.services.ai.workflows_outline_v51 import _apply_l3_quality_closeout

        tree = build_generic_tree()[:1]
        tree[0]["children"] = tree[0]["children"][:1]
        out1 = await _apply_l3_quality_closeout(None, tree, "", "", "", "")
        titles1 = [l3["title"] for l2 in out1[0].get("children", []) for l3 in l2.get("children", [])]
        assert titles1, "收口后不应清空目录"
        # 二次处理不再报错、不改变标题
        out2 = await _apply_l3_quality_closeout(None, out1, "", "", "", "")
        titles2 = [l3["title"] for l2 in out2[0].get("children", []) for l3 in l2.get("children", [])]
        assert titles1 == titles2

        # 异常 provider 不得阻断（内部 try/except 兜底）
        class _BoomProvider:
            async def chat(self, messages, **kwargs):
                raise RuntimeError("boom")

        out3 = await _apply_l3_quality_closeout(_BoomProvider(), tree, "", "", "", "")
        assert out3, "异常 provider 不得清空目录"


# ===========================================================================
# 9. 配置默认值与向后兼容
# ===========================================================================


class TestConfigDefaults:
    """新增配置项默认开启，且未设置环境变量时保持向后兼容。"""

    def test_quality_enhance_enabled_by_default(self):
        assert OUTLINE_L3_QUALITY_ENHANCE_ENABLED is True

    def test_repair_max_is_sensible(self):
        assert 1 <= OUTLINE_L3_REPAIR_MAX <= 100

    def test_repair_concurrency_is_sensible(self):
        """并发上限必须 ≥1（0/负数会让信号量永不放行，任务永久挂起）。"""
        assert OUTLINE_L3_REPAIR_CONCURRENCY >= 1, (
            f"并发上限非法: {OUTLINE_L3_REPAIR_CONCURRENCY}"
        )
        assert OUTLINE_L3_REPAIR_CONCURRENCY <= 20, (
            f"并发上限过大，会压垮 provider: {OUTLINE_L3_REPAIR_CONCURRENCY}"
        )
        # 并发应明显小于单次修复条数，否则限流形同虚设
        assert OUTLINE_L3_REPAIR_CONCURRENCY < OUTLINE_L3_REPAIR_MAX

    def test_legacy_placeholder_regex_is_still_exported(self):
        """向后兼容：旧常量名保留，避免外部引用断裂。"""
        from app.services.ai import outline_common

        assert outline_common._PLACEHOLDER_TITLE_RE.pattern == r"^专项\d+$"

    def test_generic_only_titles_covers_legacy_check(self):
        # 旧实现硬编码 `_title == "实施要点"`，新词表必须覆盖
        assert "实施要点" in _GENERIC_ONLY_TITLES
