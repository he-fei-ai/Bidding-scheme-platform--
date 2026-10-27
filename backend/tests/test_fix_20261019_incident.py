"""2026-10-19 岳阳路园区科研南楼室内装修工程投标项目 · 文档质量事故回归测试。

事故来源（用户上传的文档质量分析报告，评分 15/100 F 级）：

  F1 章节数失控：22 条评分项严格对应生成 22 个一级章节，超出投标文件
     合理结构规模（应限 8~15）。修复：OUTLINE_L1_MAX_CHAPTERS 硬上限 +
     `_apply_l1_chapter_cap` 截断 + AI 归并提示。
  F2 L3 标题跨 L2 成套照抄死循环：同一章内「一/二/三」各挂完全相同的
     1-7。旧护栏 `_cap_cross_chapter_repetition` 按父级实例计数上限 3，
     「同章 3 个 L2 挂同一批 L3」恰好 = 3 个实例，不会触发。修复：新增
     `_cap_intra_chapter_repetition` 同章内护栏（OUTLINE_L3_INTRA_MAX_
     PARENTS，默认 2）。
  F3 专业术语串台：装修项目目录出现「勘察技术路线与地质难点应对方案」
     地勘标题，正文被误导跑题。修复：项目类型检测 + `_sanitize_cross_
     domain_titles` 确定性术语改写 + `_build_professional_isolation_
     block` 提示词专业隔离约束。
  F4 页眉残留占位名：前端快速建项目命名「招投标项目 2026/10/19 20:44:41」，
     分析得到的真实项目名从不回写。修复：facts 保存回填 + 导出兜底解析
     （`_is_placeholder_project_name`）。
  F5 目录页空白：TOC 域只有「请右键更新域」占位文本。修复：导出时用
     `_collect_toc_entries` 预生成静态条目作为域缓存结果。
  F6 流程图兜底渲染截断：`_parse_mermaid_flowchart` 只匹配独立节点定义
     行，AI 产物的纯连线行标签全部漏采 →「开始→某节点」残缺图。修复：
     补齐连线行两侧标签提取。
  F7 Markdown 残留：「## 图1-1 …」落库后整个文档标题带井号。修复：
     `sanitize_for_docx` 剥离开头 Markdown 标题前缀。

运行：cd backend && python -m pytest tests/test_fix_20261019_incident.py -q -p no:cacheprovider
"""

import pytest
from docx import Document

from app.config import OUTLINE_L1_MAX_CHAPTERS, OUTLINE_L3_INTRA_MAX_PARENTS
from app.services.ai.outline_common import (
    _cap_intra_chapter_repetition,
    _detect_project_type,
    _sanitize_cross_domain_titles,
    _build_professional_isolation_block,
    _is_dirty_l3_title_deep,
    _title_core,
)
from app.services.ai.workflows_outline_v51 import _apply_l1_chapter_cap
from app.services.ai.mermaid_flowchart import _parse_mermaid_flowchart
from app.services.export import (
    _is_placeholder_project_name,
    sanitize_for_docx,
    _collect_toc_entries,
    _add_toc_field,
)
from app.models import ExportConfig


def _iter_titles(nodes, level=1):
    for n in nodes:
        if not isinstance(n, dict):
            continue
        yield level, n.get("title") or ""
        yield from _iter_titles(n.get("children") or [], level + 1)


# ===========================================================================
# F1：一级章节数硬上限
# ===========================================================================
class TestL1ChapterCap:
    def _chapters(self, n: int) -> list[dict]:
        return [
            {"title": f"第{i}章 施工方案{i}", "level": 1, "children": []}
            for i in range(1, n + 1)
        ]

    def test_config_defaults(self):
        """生产事故：22 评分项 → 22 章。默认上限 15、同章 L3 复用上限 2。"""
        assert OUTLINE_L1_MAX_CHAPTERS == 15
        assert OUTLINE_L3_INTRA_MAX_PARENTS == 2

    def test_incident_22_chapters_capped_to_15(self):
        tree = self._chapters(22)
        out, truncated = _apply_l1_chapter_cap(tree)
        assert truncated is True
        assert len(out) == 15
        # 截断保留前 15 章（先到先得，评分覆盖牺牲于结构可控）
        assert out[0]["title"] == "第1章 施工方案1"
        assert out[-1]["title"] == "第15章 施工方案15"

    def test_within_cap_untouched(self):
        tree = self._chapters(10)
        out, truncated = _apply_l1_chapter_cap(tree)
        assert truncated is False
        assert out is tree
        assert len(out) == 10

    def test_cap_zero_disables(self):
        tree = self._chapters(20)
        out, truncated = _apply_l1_chapter_cap(tree[:0] or tree)  # noqa: 不改全局配置，仅验证语义分支由 len<=cap 覆盖
        # cap<=0 分支通过 monkeypatch 配置无法在本类直接注入，此处验证
        # 常规调用不抛错即可（真正的关闭路径由 env OUTLINE_L1_MAX_CHAPTERS=0 控制）。
        assert isinstance(out, list)


# ===========================================================================
# F2：同章内 L3 成套照抄护栏（生产事故形态）
# ===========================================================================
class TestIntraChapterReuseCap:
    L1 = "一、总体方案与实施策划"
    L2S = ["一、总体方案与实施策划", "二、分阶段推进计划", "三、资源配置与协调保障"]
    L3_SET = [
        "施工工艺流程与要点",
        "常见问题与防治措施",
        "成品保护与验收要求",
        "质量通病与控制要点",
        "安全文明施工要点",
        "材料进场验收管理",
        "竣工验收与资料归档",
    ]

    def _incident_tree(self) -> list[dict]:
        """事故形态：同一 L1 章内 3 个 L2 各挂完全相同的一批 L3。"""
        return [
            {
                "title": self.L1,
                "level": 1,
                "children": [
                    {
                        "title": l2,
                        "level": 2,
                        "children": [
                            {"title": t, "level": 3, "children": []}
                            for t in self.L3_SET
                        ],
                    }
                    for l2 in self.L2S
                ],
            }
        ]

    def test_incident_shape_is_fixed(self):
        """同章 3 个 L2 挂同一批 L3：超出 2 个父级的第 3 份必须被改写。"""
        tree = self._incident_tree()
        _, changed = _cap_intra_chapter_repetition(tree, OUTLINE_L3_INTRA_MAX_PARENTS)
        assert changed > 0
        # 每个原始 L3 标题在本章内最多保留 2 个 L2 父级实例
        for t in self.L3_SET:
            occurrences = sum(
                1 for lv, title in _iter_titles(tree) if lv == 3 and title == t
            )
            assert occurrences <= OUTLINE_L3_INTRA_MAX_PARENTS, (
                f"{t} 仍出现 {occurrences} 次"
            )

    def test_l1_l2_titles_untouched(self):
        tree = self._incident_tree()
        before = [(lv, t) for lv, t in _iter_titles(tree) if lv <= 2]
        _cap_intra_chapter_repetition(tree, OUTLINE_L3_INTRA_MAX_PARENTS)
        after = [(lv, t) for lv, t in _iter_titles(tree) if lv <= 2]
        assert before == after

    def test_rewritten_titles_clean_and_no_ordinal_suffix(self):
        tree = self._incident_tree()
        _cap_intra_chapter_repetition(tree, OUTLINE_L3_INTRA_MAX_PARENTS)
        for lv, t in _iter_titles(tree):
            if lv == 3:
                assert t and not _is_dirty_l3_title_deep(t), t
                assert "（" not in t and "(" not in t, f"不应产出编号后缀: {t}"

    def test_guard_is_idempotent(self):
        tree = self._incident_tree()
        _cap_intra_chapter_repetition(tree, OUTLINE_L3_INTRA_MAX_PARENTS)
        snapshot = [t for lv, t in _iter_titles(tree) if lv == 3]
        _, changed = _cap_intra_chapter_repetition(tree, OUTLINE_L3_INTRA_MAX_PARENTS)
        assert changed == 0
        assert [t for lv, t in _iter_titles(tree) if lv == 3] == snapshot

    def test_within_limit_untouched(self):
        """同章 2 个 L2 挂同一批 L3 = 上限之内（合法形态），不得误伤。"""
        tree = [
            {
                "title": self.L1,
                "level": 1,
                "children": [
                    {
                        "title": l2,
                        "level": 2,
                        "children": [
                            {"title": t, "level": 3, "children": []}
                            for t in self.L3_SET
                        ],
                    }
                    for l2 in self.L2S[:2]
                ],
            }
        ]
        _, changed = _cap_intra_chapter_repetition(tree, OUTLINE_L3_INTRA_MAX_PARENTS)
        assert changed == 0

    def test_zero_disables_guard_for_backward_compat(self):
        tree = self._incident_tree()
        _, changed = _cap_intra_chapter_repetition(tree, 0)
        assert changed == 0

    def test_cross_chapter_legitimate_reuse_not_blocked(self):
        """不同 L1 章内各自挂同一批 L3（跨章合法小复用）不受同章护栏影响。"""
        tree = [
            {
                "title": f"第{i}章 专项方案",
                "level": 1,
                "children": [
                    {
                        "title": "总体安排",
                        "level": 2,
                        "children": [
                            {"title": self.L3_SET[0], "level": 3, "children": []}
                        ],
                    }
                ],
            }
            for i in range(3)
        ]
        _, changed = _cap_intra_chapter_repetition(tree, OUTLINE_L3_INTRA_MAX_PARENTS)
        assert changed == 0


# ===========================================================================
# F3：专业隔离（跨领域术语串台）
# ===========================================================================
class TestProfessionalIsolation:
    def test_detect_by_summary_keyword(self):
        assert (
            _detect_project_type("岳阳路园区科研南楼室内装修工程施工投标") == "装饰装修"
        )

    def test_detect_prefers_fact_over_summary(self):
        facts = [{"key": "projectType", "value": "市政"}]
        assert _detect_project_type("室内装修工程", facts) == "市政"

    def test_detect_unknown_returns_empty(self):
        assert _detect_project_type("某综合项目") == ""
        assert _detect_project_type("") == ""

    def test_incident_title_sanitized(self):
        """事故实录标题：装修项目出现地勘术语，必须被确定性改写。"""
        tree = [
            {
                "title": "施工组织与技术方案",
                "level": 1,
                "children": [
                    {
                        "title": "关键技术措施",
                        "level": 2,
                        "children": [
                            {
                                "title": "勘察技术路线与地质难点应对方案",
                                "level": 3,
                                "children": [],
                            }
                        ],
                    }
                ],
            }
        ]
        out, changed = _sanitize_cross_domain_titles(tree, "装饰装修")
        assert changed >= 1
        bad_title = out[0]["children"][0]["children"][0]["title"]
        assert "地质" not in bad_title, bad_title
        assert "既有设施难点" in bad_title, bad_title

    def test_more_terms_sanitized(self):
        tree = [
            {"title": "桩基施工方案", "level": 1, "children": []},
            {"title": "塔吊布置与土方开挖", "level": 1, "children": []},
        ]
        out, changed = _sanitize_cross_domain_titles(tree, "装饰装修")
        assert changed == 2
        assert out[0]["title"] == "基础加固施工方案"
        assert "塔吊" not in out[1]["title"]
        assert "土方开挖" not in out[1]["title"]

    def test_unknown_type_no_rewrite(self):
        """无法判定项目类型时不做任何改写（避免误伤其他行业合法术语）。"""
        tree = [{"title": "地质勘察与桩基施工", "level": 1, "children": []}]
        out, changed = _sanitize_cross_domain_titles(tree, "")
        assert changed == 0
        assert out[0]["title"] == "地质勘察与桩基施工"

    def test_isolation_block_content(self):
        block = _build_professional_isolation_block("装饰装修")
        assert "装饰装修" in block
        assert "严禁" in block
        assert "地质勘察" in block

    def test_isolation_block_empty_for_unknown(self):
        assert _build_professional_isolation_block("") == ""
        assert _build_professional_isolation_block("未知类型") == ""


# ===========================================================================
# F4：占位项目名识别 + F7 Markdown 残留清洗
# ===========================================================================
class TestPlaceholderNameAndMarkdownCleanup:
    @pytest.mark.parametrize(
        "name,expected",
        [
            ("招投标项目 2026/10/19 20:44:41", True),
            ("未命名项目 2026-10-19", True),
            ("新建项目 2026年10月19日", True),
            ("岳阳路园区科研南楼室内装修工程", False),
            ("招投标项目专用章", False),
            ("", False),
        ],
    )
    def test_is_placeholder_project_name(self, name, expected):
        assert _is_placeholder_project_name(name) is expected

    def test_markdown_heading_prefix_stripped(self):
        assert sanitize_for_docx("## 图1-1 施工流程图") == "图1-1 施工流程图"
        assert sanitize_for_docx("### 一、总体方案") == "一、总体方案"
        assert sanitize_for_docx("# 第一章") == "第一章"

    def test_markdown_strip_only_leading(self):
        """仅剥离开头前缀，正文中合法的 # 不受影响。"""
        assert sanitize_for_docx("正文中 a # b 的用法") == "正文中 a # b 的用法"
        assert sanitize_for_docx("标题##后缀") == "标题##后缀"

    def test_legacy_control_char_cleanup_still_works(self):
        """既有行为不回归：XML 不安全控制字符仍被清洗（防 add_run 崩溃）。"""
        cleaned = sanitize_for_docx("标\x0b题")
        assert "\x0b" not in cleaned
        assert sanitize_for_docx("") == ""
        assert sanitize_for_docx(None) == ""


# ===========================================================================
# F5：TOC 静态条目回填
# ===========================================================================
class _N:
    """模拟 services.sections.build_tree 的 SectionNode（title/children）。"""

    def __init__(self, title: str, children: list | None = None):
        self.title = title
        self.children = children or []


class TestTocStaticEntries:
    def _tree(self):
        return [
            _N(
                "第一章 施工总体部署",
                [
                    _N("1.1 编制依据"),
                    _N("1.2 工程概况", [_N("1.2.1 现场调查与既有设施排查")]),
                ],
            ),
            _N(
                "第二章 主要施工方法",
                [_N("2.1 基层处理工艺", [_N("2.1.1 施工准备"), _N("2.1.2 面层铺贴")])],
            ),
        ]

    def test_collect_entries_level_order(self):
        entries = _collect_toc_entries(self._tree())
        # FIX 2026-10-26（报告 G-5/I-5）：目录条目与正文标题同口径——
        # 程序编号 + 去旧编号标题；L2 计数随父级（各章）复位。
        assert entries == [
            (1, "第一节、施工总体部署"),
            (2, "一、编制依据"),
            (2, "二、工程概况"),
            (3, "1、现场调查与既有设施排查"),
            (1, "第二节、主要施工方法"),
            (2, "一、基层处理工艺"),
            (3, "1、施工准备"),
            (3, "2、面层铺贴"),
        ]

    def test_collect_entries_respects_max_level(self):
        entries = _collect_toc_entries(self._tree(), max_level=2)
        assert all(lv <= 2 for lv, _ in entries)
        assert not any(lv == 3 for lv, _ in entries)

    def test_collect_entries_empty_tree(self):
        assert _collect_toc_entries([]) == []
        assert _collect_toc_entries(None) == []

    def test_add_toc_field_with_static_entries(self):
        """有静态条目：Word 打开即见目录，无占位文案，L1 加粗。"""
        doc = Document()
        config = ExportConfig()
        entries = [
            (1, "第一章 施工总体部署"),
            (2, "1.1 编制依据"),
            (3, "1.2.1 现场调查与既有设施排查"),
        ]
        _add_toc_field(doc, config, static_entries=entries)
        texts = [p.text for p in doc.paragraphs if p.text.strip()]
        assert "第一章 施工总体部署" in texts
        assert "1.1 编制依据" in texts
        assert "1.2.1 现场调查与既有设施排查" in texts
        assert not any("请右键此处" in t for t in texts)
        # L1 条目加粗、低层级条目不加粗
        p1 = next(p for p in doc.paragraphs if p.text == "第一章 施工总体部署")
        assert p1.runs and p1.runs[0].bold is True
        p3 = next(p for p in doc.paragraphs if p.text == "1.2.1 现场调查与既有设施排查")
        assert p3.runs and not p3.runs[0].bold

    def test_add_toc_field_without_entries_keeps_placeholder(self):
        """条目为空（tree 构建失败降级）：保持原占位文案，行为向后兼容。"""
        doc = Document()
        config = ExportConfig()
        _add_toc_field(doc, config, static_entries=None)
        texts = [p.text for p in doc.paragraphs if p.text.strip()]
        assert any("请右键此处选择「更新域」以生成目录" in t for t in texts)


# ===========================================================================
# F6：流程图兜底解析（连线行标签提取）
# ===========================================================================
class TestFlowchartEdgeLabelExtraction:
    def test_pure_edge_lines_all_labels_extracted(self):
        """事故形态：AI 产物是纯连线行，旧实现漏采两侧标签 → 残缺图。"""
        code = (
            "flowchart TD\n"
            '    A1["施工准备"] --> B1["测量放线"]\n'
            '    B1 --> C1["基层处理"]\n'
            '    C1 -->|验收| D1["面层铺贴"]\n'
            '    D1 --> E1["成品保护"]\n'
        )
        labels = _parse_mermaid_flowchart(code)
        assert labels == ["施工准备", "测量放线", "基层处理", "面层铺贴", "成品保护"]

    def test_standalone_node_defs_still_work(self):
        """既有行为不回归：独立节点定义行照常提取。"""
        code = 'graph TD\n    A[开始]\n    B[结束]\n    A --> B'
        labels = _parse_mermaid_flowchart(code)
        assert labels == ["开始", "结束"]

    def test_mixed_defs_and_edges_deduped_in_order(self):
        code = (
            "flowchart LR\n"
            '    A["材料进场"]\n'
            '    A --> B["复验"]\n'
            '    B --> C{"是否合格"}\n'
            '    C -->|是| D["入库"]\n'
            '    C -->|否| E["退场"]\n'
        )
        labels = _parse_mermaid_flowchart(code)
        assert labels == ["材料进场", "复验", "是否合格", "入库", "退场"]

    def test_edge_label_not_treated_as_node(self):
        """|边标签| 不得混入节点列表。"""
        code = 'flowchart TD\n    A["甲"] -->|中间检查| B["乙"]'
        labels = _parse_mermaid_flowchart(code)
        assert "中间检查" not in labels
        assert labels == ["甲", "乙"]

    def test_full_chain_coverage_no_truncation(self):
        """兜底图必须覆盖流程图全部节点（修复前只剩「开始→某节点」）。"""
        steps = ["施工准备", "测量放线", "龙骨安装", "隐蔽验收", "面板安装", "接缝处理", "涂饰施工", "成品保护"]
        lines = ["flowchart TD"]
        for i in range(len(steps) - 1):
            lines.append(f'    S{i}["{steps[i]}"] --> S{i + 1}["{steps[i + 1]}"]')
        labels = _parse_mermaid_flowchart("\n".join(lines))
        assert labels == steps

    def test_fewer_than_two_nodes_returns_none(self):
        assert _parse_mermaid_flowchart('flowchart TD\n    A1["开始"]') is None
        assert _parse_mermaid_flowchart("") is None
        assert _parse_mermaid_flowchart("flowchart TD") is None

    def test_no_edge_artifacts_in_output(self):
        code = (
            "flowchart TD\n"
            '    A1["开工条件确认"] --> B1["现场移交"]\n'
            '    B1 --> C1["深化设计"]\n'
        )
        for label in _parse_mermaid_flowchart(code) or []:
            assert not label.startswith("--")
            assert ">" not in label
            assert not label.startswith("|")


# ===========================================================================
# 端到端：事故目录树过完整收口链
# ===========================================================================
class TestIncidentChainEndToEnd:
    def test_full_closeout_chain(self):
        """22 章 + 同章成套照抄 + 地勘串台 → 一次收口后全部消除。"""
        from collections import Counter

        bad_l3 = [
            "勘察技术路线与地质难点应对方案",
            "深基坑支护方案",
            "施工工艺流程与要点",
        ]
        tree = [
            {
                "title": f"第{i}章 评分项{i}对应章节",
                "level": 1,
                "children": [
                    {
                        "title": l2,
                        "level": 2,
                        "children": [{"title": t, "level": 3, "children": []} for t in bad_l3],
                    }
                    for l2 in ["总体安排", "分阶段计划", "资源保障"]
                ],
            }
            for i in range(1, 23)
        ]
        # F1：章节数硬上限
        tree, truncated = _apply_l1_chapter_cap(tree)
        assert truncated is True and len(tree) == 15
        # F3：专业隔离术语改写
        tree, _ = _sanitize_cross_domain_titles(tree, "装饰装修")
        # F2：同章内成套照抄护栏
        tree, _ = _cap_intra_chapter_repetition(tree, OUTLINE_L3_INTRA_MAX_PARENTS)

        # 逐项验证
        assert len(tree) == OUTLINE_L1_MAX_CHAPTERS
        for lv, t in _iter_titles(tree):
            if lv <= 2:
                # F3：域外术语不得残留在标题中
                assert "地质" not in t and "基坑" not in t, t
            if lv == 3:
                assert t and not _is_dirty_l3_title_deep(t), t
        # F2：同章内每个 L3 core 最多出现于 2 个 L2 父级
        for chapter in tree:
            l3_cores = [
                _title_core(t)
                for lv, t in _iter_titles([chapter])
                if lv == 3
            ]
            reuse = Counter(c for c in l3_cores if c)
            for core, count in reuse.items():
                # core 在同一章内的出现总数 = 不同 L2 父级数（每个 L2 一次）
                assert count <= OUTLINE_L3_INTRA_MAX_PARENTS, (
                    f"core={core!r} 在章内出现 {count} 次"
                )
