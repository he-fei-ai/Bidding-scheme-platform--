"""目录生成模块：占位标题修复与消歧增强（2026-10-09）。

对应《缺陷修复方案》BUG-2026-1008-02（三级标题重复/占位）在「生成大纲模块
· 目录生成」代码层的落地：

  1. L3 兜底标题不再产出「专项N」/「实施要点」占位（语义化兜底引擎）；
  2. 全局去重与冲突修复不再追加「（2）/（二）」式裸编号，改用专业执行
     维度词消歧（如「总体实施策划专项方案」）；
  3. 新增占位标题清洗，兜底清除任何遗留「专项N」脏标题。

全部为纯函数测试（不依赖 DB / AI），mock 环境即可运行。
"""

import re
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.services.ai.outline_common import (
    _cleanup_placeholder_titles,
    _disambiguate_title,
    _is_dirty_l3_title_deep,
    _semantic_fallback_title,
    _short_parent_token,
    _uniquify_outline_titles,
)
from app.services.ai.workflows_outline_v51 import _generate_diverse_l3_title

_PLACEHOLDER_RE = re.compile(r"^专项\d+$")


def _has_placeholder(titles):
    return any(_PLACEHOLDER_RE.match(t) or t == "实施要点" for t in titles)


def _collect_titles(tree):
    out = []

    def _walk(nodes):
        for n in nodes:
            if isinstance(n, dict):
                if n.get("title"):
                    out.append(n["title"])
                _walk(n.get("children", []) or [])

    _walk(tree)
    return out


# ===========================================================================
# 1. 语义化兜底：永不产出「专项N」占位
# ===========================================================================


class TestSemanticFallbackTitle:
    """_semantic_fallback_title 必须产出有意义且唯一的标题。"""

    def test_no_placeholder_when_pool_exhausted(self):
        # 模拟 L3 池被全局用尽（超大 idx + 共享 used 集合），仍不得出现「专项N」
        used = set()
        titles = [_semantic_fallback_title("脚手架总体施工部署与区段划分", i, used) for i in range(30)]
        assert not _has_placeholder(titles), f"出现占位标题: {titles}"
        assert len(set(titles)) == len(titles), "标题未保证唯一"

    def test_meaningful_token_from_domain(self):
        # 父级含域关键词「脚手架」→ 标题应带「脚手架」前缀，且为专业维度
        t = _semantic_fallback_title("脚手架总体施工部署与区段划分", 0, set())
        assert "脚手架" in t
        assert any(d in t for d in ("施工工艺", "质量管控", "安全保障", "进度安排"))

    def test_empty_parent_falls_back_to_safe_token(self):
        t = _semantic_fallback_title("", 0, set())
        assert t and not _PLACEHOLDER_RE.match(t)

    def test_unique_across_different_parents(self):
        used = set()
        a = _semantic_fallback_title("脚手架施工部署", 0, used)
        used.add(a)
        b = _semantic_fallback_title("临时用电部署", 0, used)
        assert a != b


# ===========================================================================
# 2. _generate_diverse_l3_title：直接验证无占位输出
# ===========================================================================


class TestGenerateDiverseL3Title:
    """L3 生成入口的占位防护。"""

    def test_no_placeholder_with_exhausted_global_used(self):
        # 用一个已被海量标题占满的 used 集合，迫使兜底路径
        used = {f"脚手架{d}" for d in range(50)}
        titles = [
            _generate_diverse_l3_title("脚手架总体施工部署与区段划分", i, "脚手架工程", used, 0)
            for i in range(15)
        ]
        assert not _has_placeholder(titles), f"出现占位: {titles}"

    def test_empty_l2_l1_still_meaningful(self):
        t = _generate_diverse_l3_title("", 0, "", set(), 0)
        assert t and not _PLACEHOLDER_RE.match(t) and t != "实施要点"

    def test_l3_corresponds_to_parent(self):
        t = _generate_diverse_l3_title("钢筋工程", 0, "主体结构施工", set(), 0)
        assert t  # 不强制含父词（规则3：L3 独立），但必须非占位且唯一


# ===========================================================================
# 3. 占位标题清洗：清除遗留「专项N」/「实施要点」
# ===========================================================================


class TestCleanupPlaceholderTitles:
    def test_rewrites_placeholders_and_counts(self):
        tree = [
            {
                "title": "第一节、脚手架专项施工方案",
                "children": [
                    {
                        "title": "一、脚手架总体施工部署",
                        "children": [
                            {"title": "专项6"},
                            {"title": "专项7"},
                            {"title": "实施要点"},
                        ],
                    },
                    {
                        "title": "二、脚手架分项工程",
                        "children": [{"title": "专项6"}],
                    },
                ],
            }
        ]
        fixed = _cleanup_placeholder_titles(tree)
        # 2026-10-27（L2 清洗升级）：L2 标题「一、…」「二、…」带编号前缀（DB title
        # 不携带编号），现在也会被清洗（剥前缀保主体）。因此：
        #   L2×2（剥前缀）+ L3×4（3 个专项 + 1 个实施要点）= 6 处
        assert fixed == 6, f"应清洗 6 个（L2×2 剥前缀 + L3×4 占位），实际 {fixed}"
        titles = _collect_titles(tree)
        assert not _has_placeholder(titles), f"仍有占位: {titles}"
        # 同父下的占位改写为带域 token 的语义标题，且相互唯一
        node1 = tree[0]["children"][0]["children"]
        assert node1[0]["title"] != node1[1]["title"]
        # 2026-10-27 新增断言：L2 前缀被剥掉，保留主体
        assert tree[0]["children"][0]["title"] == "脚手架总体施工部署"
        assert tree[0]["children"][1]["title"] == "脚手架分项工程"

    def test_keeps_normal_titles(self):
        tree = [{"title": "正常章节", "children": [{"title": "施工工艺与操作要点"}]}]
        fixed = _cleanup_placeholder_titles(tree)
        assert fixed == 0
        assert tree[0]["children"][0]["title"] == "施工工艺与操作要点"


# ===========================================================================
# 4. 全局去重 / 消歧：不再裸编号
# ===========================================================================


class TestUniquifyAndDisambiguate:
    def test_uniquify_uses_semantic_suffix_not_bare_number(self):
        out = [
            {"title": "总体实施策划", "children": []},
            {"title": "总体实施策划", "children": []},
            {"title": "总体实施策划", "children": []},
        ]
        _uniquify_outline_titles(out)
        titles = [o["title"] for o in out]
        assert titles[0] == "总体实施策划"
        # 后续重名应被改写为语义化标题，而非「（2）/（二）」裸编号
        for t in titles[1:]:
            assert not re.search(r"（\d+）$", t), f"出现裸编号: {t}"
            # 2026-10-15 增强：改写结果必须是「非空壳」的语义标题。
            # 旧实现退化为「总体实施策划专项方案」（泛化词干+泛化维度词）或
            # 「总体实施策划之四」（纯序数），两头都判不了脏；现改走主体专属
            # 语义池，用 `_is_dirty_l3_title_deep` 复检。
            assert not _is_dirty_l3_title_deep(t), f"改写结果仍是空壳标题: {t}"
            assert t.startswith("总体实施策划之") is False, (
                f"退化为纯序数后缀: {t}"
            )

    def test_disambiguate_distinct_and_no_bare_number(self):
        seen = set()
        a = _disambiguate_title("总体实施策划", seen)
        seen.add(a)
        b = _disambiguate_title("总体实施策划", seen)
        seen.add(b)
        assert a != b
        for t in (a, b):
            assert not re.search(r"（\d+）$", t)

    def test_short_parent_token_strips_enumeration(self):
        # 「一、概述…」应剥掉首部编号，得到干净 token
        tok = _short_parent_token("一、概述与重难点分析总体实施策划")
        assert "、" not in tok
        assert tok in ("概述",) or len(tok) <= 6

    def test_heavy_repetition_uses_parent_prefix_not_bare_number(self):
        # 模拟《招投标项目》样例：通用标题「总体实施策划」被数十次复用
        # 维度词（10）+ 原文（1）= 11 个唯一后仍可能碰撞，需父级 token 前缀兜底
        parents = [f"第{i+1}章脚手架施工部署" for i in range(20)]
        seen = set()
        titles = []
        for p in parents:
            t = _disambiguate_title("总体实施策划", seen, parent_title=p)
            seen.add(t)
            titles.append(t)
        # 全部唯一
        assert len(set(titles)) == len(titles), "海量重复未全部消歧唯一"
        # 不得出现裸编号「（2）/（其2）」等落败形态（前 11 个维度词 + 父级前缀足够覆盖 20 个）
        for t in titles:
            assert not re.search(r"（\d+）$", t), f"出现裸编号兜底: {t}"
            assert t != "总体实施策划" or titles.count(t) == 1


# ===========================================================================
# 5. 端到端：构造接近样例的重复骨架，整树清洗后应无占位/裸编号
# ===========================================================================


class TestEndToEndTocClean:
    def test_repeated_skeleton_becomes_meaningful(self):
        # 模拟《招投标项目》样例：同一套 L3 骨架在多个 L2 下重复。
        #
        # 2026-10-11 政策修正：跨二级章的同名三级条目是标书目录的**合法形态**
        # （Word 自动目录里呈现为 3.1 / 4.1 / 5.1），必须原样保留。
        # 旧实现强制「全树唯一」，为消除这类合法重复不断追加「专项方案」等
        # 无意义维度词，最终退化为「（其3）（其4）（其5）」式冗余后缀 ——
        # 即用户反馈的目录污染。现改为同级作用域：跨父同名保持不动。
        skeleton_l3 = ["总体实施策划", "分阶段推进计划", "资源配置与协调保障"]
        tree = []
        for ci in range(4):  # 4 个 L2 复用同一骨架
            l2 = {"title": f"第{ci+1}节施工部署", "children": []}
            for li, base in enumerate(skeleton_l3):
                l2["children"].append({"title": base})
            tree.append(l2)
        # 同级去重安全网（模拟）+ 占位清洗（兜底）
        _uniquify_outline_titles(tree)
        _cleanup_placeholder_titles(tree)
        titles = _collect_titles(tree)
        assert not _has_placeholder(titles)
        # 跨父同名必须原样保留（12 个 L3 全部保持骨架原标题）
        l3_titles = [c["title"] for l2 in tree for c in l2["children"]]
        assert l3_titles == skeleton_l3 * 4, f"跨父合法同名被误改: {l3_titles}"
        for t in l3_titles:
            assert not re.search(r"（\d+）$", t), f"出现裸编号兜底: {t}"
            assert "（其" not in t, f"出现冗余序数后缀: {t}"

    def test_same_parent_duplicates_are_disambiguated(self):
        # 真·同级重名（同一 L2 下同名 L3）才是缺陷，必须消歧且不用裸编号
        tree = [
            {
                "title": "第1节施工部署",
                "children": [{"title": "总体实施策划"} for _ in range(4)],
            }
        ]
        _uniquify_outline_titles(tree)
        titles = [c["title"] for c in tree[0]["children"]]
        assert titles[0] == "总体实施策划", "首项必须保持原样"
        assert len(set(titles)) == 4, f"同级重名未全部消歧: {titles}"
        for t in titles:
            assert not re.search(r"（\d+）$", t), f"出现裸编号兜底: {t}"
            assert "（其" not in t, f"出现冗余序数后缀: {t}"

    def test_legacy_qi_suffix_is_cleaned(self):
        # 存量脏数据：已落库的「（其N）」后缀必须在去重安全网中被剥掉，
        # 标题主体（业务语义）完整保留
        tree = [
            {
                "title": "A",
                "children": [
                    {"title": "B1", "children": [{"title": "砌体成品保护"}]},
                    {"title": "B2", "children": [{"title": "砌体成品保护（其2）"}]},
                    {"title": "B3", "children": [{"title": "机电管线材料管理实施细则（其3）"}]},
                ],
            }
        ]
        _uniquify_outline_titles(tree)
        titles = [c["children"][0]["title"] for c in tree[0]["children"]]
        assert titles == [
            "砌体成品保护",
            "砌体成品保护",
            "机电管线材料管理实施细则",
        ], f"（其N）未被剥离: {titles}"

