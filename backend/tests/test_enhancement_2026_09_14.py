"""回归测试：从「易标投标工具箱」移植的能力增强（2026-09-14 第五轮）

覆盖范围：
  A. JSON 容错解析链（非法转义 / 裸控制字符 / 尾随逗号 / 注释）
  B. 导出格式预设体系（版式 + 主题，数据层与合并语义）
  C. 导出格式预设体系（DOCX 渲染层：标题色、章框线、表头底色、边框色、页码格式）
  D. build_docx 端到端（预设真正落到文档属性上）
  E. 查重：目录结构相似度（覆盖率 + LCS 顺序）
  F. 查重：文档同源指纹（RSID / PDF 结构 / WPS 痕迹）
  G. 废标检查：错别字定位校验（幻觉条目丢弃 + 去重 + 摘录/行号）
  H. 废标检查：三轮流程（范围分析 → 初稿 → 定稿）与宿主侧证据核验
  I. 目录审核门禁：确定性事实层 + 与自检阶段的接线
  J. 正文一致性修补：精确补丁引擎 + 真实落文 + AI 补丁路径
  K. 任务组互斥锁：sqlite 行级互斥 + 心跳超时 + 多进程残留判定

设计原则：只测行为不测实现。所有用例不依赖真实 AI 与外部网络。
"""

from __future__ import annotations

import asyncio
import functools
import itertools
import json
import zipfile

import pytest
from docx import Document

from app.models import ExportConfig
from app.services.export import (
    _add_section_heading,
    _add_table_from_markup,
    build_docx,
)
from app.services.export_presets import (
    LAYOUT_PRESETS,
    THEME_PRESETS,
    get_layout_preset,
    get_theme_preset,
    list_presets,
    resolve_export_config,
)
from app.utils.json_parser import (
    extract_json_from_text,
    repair_invalid_json_escapes,
)


# ==================== A. JSON 容错解析链 ====================


@pytest.mark.parametrize(
    ("name", "raw", "expected"),
    [
        ("纯 JSON", '{"a": 1}', {"a": 1}),
        ("代码围栏", '说明\n```json\n{"a": 1}\n```\n结尾', {"a": 1}),
        ("文本内嵌", '前缀 {"a": {"b": 2}} 后缀', {"a": {"b": 2}}),
        ("顶层数组", '[{"x": 1},]', [{"x": 1}]),
        # 模型把 Windows 路径写进 JSON 时极易出现非法转义 \U
        ("非法转义-路径", '{"p": "C:\\Users\\data"}', {"p": "C:\\Users\\data"}),
        # 模型把 Markdown 转义 \. 写进 JSON 字符串
        ("非法转义-Markdown", '{"t": "1\\. 步骤"}', {"t": "1. 步骤"}),
        # 字符串内裸换行（模型分点输出时最常见）
        ("裸换行", '{"t": "第一行\n第二行"}', {"t": "第一行\n第二行"}),
        ("尾随逗号", '{"a": 1,}', {"a": 1}),
        ("行注释", '{"a": 1, // 备注\n "b": 2}', {"a": 1, "b": 2}),
        ("块注释", '{"a": 1 /* 备注 */, "b": 2}', {"a": 1, "b": 2}),
        ("合法 unicode 转义", '{"a": "\\u4e2d"}', {"a": "中"}),
    ],
)
def test_extract_json_from_text_candidates(name, raw, expected):
    """各候选级都能提取出正确 JSON（ENHANCE 2026-09-14 新增后两级本地修复）"""
    assert extract_json_from_text(raw) == expected, f"用例 {name} 解析失败"


def test_extract_json_from_text_invalid_returns_none():
    """完全无法解析时必须返回 None，而不是抛异常"""
    assert extract_json_from_text("") is None
    assert extract_json_from_text("这段文字里没有 JSON") is None
    assert extract_json_from_text("{'a': 1}") is None  # 单引号不属 JSON，不猜测


def test_repair_invalid_json_escapes_is_idempotent_on_valid_json():
    """合法 JSON 经过转义修复必须完全不变（幂等），否则会破坏正常输出"""
    valid_samples = [
        '{"a": 1}',
        '{"a": "x\\ny"}',
        '{"a": "\\u4e2d\\u6587"}',
        '{"a": "quote\\"inside"}',
        '{"path": "a\\\\b"}',
        '{"slash": "a\\/b"}',
        '{"tab": "a\\tb"}',
    ]
    for sample in valid_samples:
        assert repair_invalid_json_escapes(sample) == sample, f"合法 JSON 被改动: {sample}"


def test_repair_invalid_json_escapes_keeps_unicode_escape():
    """\\uXXXX 必须原样保留（转成 \\\\u 会改变语义）"""
    assert repair_invalid_json_escapes('{"a": "\\u4e2d"}') == '{"a": "\\u4e2d"}'


def test_repair_invalid_json_escapes_handles_truncated_unicode():
    """\\u 后不足 4 位十六进制时不能越界或产生非法转义"""
    repaired = repair_invalid_json_escapes('{"a": "\\u12"}')
    # 关键：修复后必须仍是合法 JSON（内容允许变化，结构不能坏）
    parsed = extract_json_from_text(repaired)
    assert parsed is not None, f"修复后应为合法 JSON，实际: {repaired!r}"


# ==================== B. 导出格式预设（数据层） ====================


def test_preset_catalog_size_and_required_fields():
    """8 套版式 × 12 套主题，且元数据字段完整"""
    assert len(LAYOUT_PRESETS) == 8
    assert len(THEME_PRESETS) == 12
    meta = list_presets()
    assert len(meta["layouts"]) == 8
    assert len(meta["themes"]) == 12
    for layout in meta["layouts"]:
        assert layout["id"] and layout["label"] and layout["description"]
        assert layout["config"], f"版式 {layout['id']} 的 config 片段为空"
    for theme in meta["themes"]:
        assert theme["swatches"], f"主题 {theme['id']} 缺少色板"
        assert theme["config"], f"主题 {theme['id']} 的 config 片段为空"


def test_layout_preset_ids_unique():
    ids = [item["id"] for item in LAYOUT_PRESETS]
    assert len(ids) == len(set(ids)), "版式预设 id 重复"
    theme_ids = [item["id"] for item in THEME_PRESETS]
    assert len(theme_ids) == len(set(theme_ids)), "主题预设 id 重复"


def test_resolve_without_preset_keeps_defaults():
    """不传预设时，合并结果必须与默认值完全一致（历史行为不变）"""
    resolved = resolve_export_config(ExportConfig())
    assert resolved.model_dump() == ExportConfig().model_dump()


def test_resolve_applies_preset_for_open_api_usage():
    """开放 API 只传 preset_id/theme_id 时，应得到完整可用的排版配置"""
    resolved = resolve_export_config(ExportConfig(preset_id="formal-binding", theme_id="blue"))
    preset = get_layout_preset("formal-binding")
    assert resolved.font_body == preset["config"]["font_body"]
    assert resolved.size_title_1 == preset["config"]["size_title_1"]
    assert resolved.heading_level1_page_break is True
    assert resolved.page_margin_left_cm == preset["config"]["page_margin_left_cm"]
    theme = get_theme_preset("blue")
    assert resolved.heading_color == theme["heading_color"]
    assert resolved.table_header_bg == theme["table_header_bg"]


def test_resolve_explicit_fields_win_over_preset():
    """调用方显式传值的字段优先于预设，避免服务端悄悄回滚用户改动"""
    resolved = resolve_export_config(
        ExportConfig(preset_id="standard-bid", theme_id="blue", size_body=14.0, font_body="微软雅黑")
    )
    assert resolved.size_body == 14.0
    assert resolved.font_body == "微软雅黑"
    # 未显式传值的字段仍继承预设
    assert resolved.size_title_1 == 18.0
    assert resolved.line_spacing == 1.2
    assert resolved.heading_color == "173f82"


def test_resolve_invalid_preset_id_is_silent():
    """非法预设 id 不应抛异常，也不应污染配置"""
    resolved = resolve_export_config(ExportConfig(preset_id="不存在", theme_id="也不存在"))
    assert resolved.font_title == "宋体"
    assert resolved.heading_color == ""


def test_resolve_landscape_preset_sets_orientation_and_wider_image():
    """横版预设必须真的切到横向，并放宽图片宽度与甘特列数"""
    resolved = resolve_export_config(ExportConfig(preset_id="wide-table-landscape"))
    assert resolved.page_orientation == "landscape"
    portrait = resolve_export_config(ExportConfig(preset_id="standard-bid"))
    assert resolved.image_max_width_cm > portrait.image_max_width_cm
    assert resolved.gantt_max_columns > portrait.gantt_max_columns


def test_resolve_normalizes_hash_prefixed_colors():
    """带 # 的颜色必须被规范化，否则 w:shd/@w:fill 会拿到非法值"""
    resolved = resolve_export_config(ExportConfig(theme_id="green"))
    assert "#" not in resolved.heading_color
    assert len(resolved.heading_color) == 6
    assert resolved.heading_color == "116a3a"


# ==================== C. 导出格式预设（DOCX 渲染层） ====================


def _paragraph_xml(paragraph) -> str:
    return paragraph._p.xml


def test_heading_uses_theme_color():
    doc = Document()
    config = resolve_export_config(ExportConfig(theme_id="blue"))
    _add_section_heading(doc, "第一章 项目概述", 1, config)
    run = doc.paragraphs[-1].runs[0]
    assert run.font.color.rgb is not None
    assert str(run.font.color.rgb) == "173F82"


def test_heading_default_color_is_black():
    """不选主题时标题仍是黑色（历史行为）"""
    doc = Document()
    _add_section_heading(doc, "第一章 项目概述", 1, ExportConfig())
    assert str(doc.paragraphs[-1].runs[0].font.color.rgb) == "000000"


def test_heading_border_only_when_enabled():
    """章框装饰线只在 heading_border_enabled=True 时写入 pBdr"""
    doc_off = Document()
    _add_section_heading(doc_off, "第一章 项目概述", 1, ExportConfig())
    assert "w:pBdr" not in _paragraph_xml(doc_off.paragraphs[-1])

    doc_on = Document()
    config = resolve_export_config(ExportConfig(preset_id="chapter-frame", theme_id="green"))
    _add_section_heading(doc_on, "第一章 项目概述", 1, config)
    xml = _paragraph_xml(doc_on.paragraphs[-1])
    assert "w:pBdr" in xml
    assert "22a05a" in xml.lower()  # 主题的章框线颜色


def test_heading_level1_page_break_flag():
    """一级标题另起页使用 Word 原生 pageBreakBefore"""
    doc = Document()
    config = resolve_export_config(ExportConfig(preset_id="formal-binding"))
    _add_section_heading(doc, "第一章 项目概述", 1, config)
    assert "w:pageBreakBefore" in _paragraph_xml(doc.paragraphs[-1])


def test_heading_fonts_per_level_applied():
    """逐级标题字体生效（预设给 L4 用楷体，这是标书排版惯例）"""
    doc = Document()
    config = resolve_export_config(ExportConfig(preset_id="standard-bid"))
    _add_section_heading(doc, "1.1 编制依据", 4, config)
    run = doc.paragraphs[-1].runs[0]
    assert run.font.name == "楷体", f"L4 应为楷体，实际 {run.font.name}"


def test_heading_fonts_fallback_when_absent():
    """未提供逐级字体时回退 font_title（旧调用方行为不变）"""
    doc = Document()
    _add_section_heading(doc, "1.1 编制依据", 4, ExportConfig(font_title="黑体"))
    assert doc.paragraphs[-1].runs[0].font.name == "黑体"


def test_table_header_and_border_follow_theme():
    """表头底色与表格边框色都应跟随主题"""
    doc = Document()
    config = resolve_export_config(ExportConfig(theme_id="orange"))
    markup = "| 序号 | 名称 |\n| --- | --- |\n| 1 | 塔吊 |"
    _add_table_from_markup(doc, markup, config)
    assert len(doc.tables) == 1
    table = doc.tables[0]
    header_xml = table.rows[0].cells[0]._tc.xml
    assert "fff3d6" in header_xml.lower(), "表头底色未跟随主题"
    assert "f2c46f" in table._tbl.tblPr.xml.lower(), "表格边框色未跟随主题"


def test_table_alternating_rows_use_theme_color():
    doc = Document()
    config = resolve_export_config(ExportConfig(theme_id="light-purple"))
    markup = "| 序号 | 名称 |\n| --- | --- |\n| 1 | 甲 |\n| 2 | 乙 |\n| 3 | 丙 |"
    _add_table_from_markup(doc, markup, config)
    table = doc.tables[0]
    # 第 1 个数据行（索引 1）不染色，第 2 个数据行（索引 2）染斑马纹
    assert "f8f5ff" not in table.rows[1].cells[0]._tc.xml.lower()
    assert "f8f5ff" in table.rows[2].cells[0]._tc.xml.lower()


def test_body_first_line_indent_is_configurable():
    """正文首行缩进字符数可被预设调整"""
    from app.services.export import _add_paragraph_with_format

    doc = Document()
    config = resolve_export_config(ExportConfig(preset_id="standard-bid"))
    config.body_first_line_indent_chars = 0
    _add_paragraph_with_format(doc, "这是一段正文。", config, "paragraph")
    assert 'w:firstLineChars="0"' in _paragraph_xml(doc.paragraphs[-1])


# ==================== D. build_docx 端到端 ====================


def _seed_project(project_id: int = 9001) -> int:
    """在测试库中写入一个最小项目（1 个一级 + 1 个二级章节，含正文）"""
    from app.db import get_db

    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO projects (id, name, industry, bidder_name, current_step) "
            "VALUES (?, ?, '', ?, 6)",
            (project_id, "预设测试项目", "测试投标单位"),
        )
        conn.execute("DELETE FROM sections WHERE project_id = ?", (project_id,))
        conn.execute(
            "INSERT INTO sections (project_id, parent_id, title, level, sort_order, content) "
            "VALUES (?, NULL, ?, 1, 0, '')",
            (project_id, "第一章 项目概述"),
        )
        root_id = conn.execute(
            "SELECT id FROM sections WHERE project_id = ? AND level = 1", (project_id,)
        ).fetchone()["id"]
        conn.execute(
            "INSERT INTO sections (project_id, parent_id, title, level, sort_order, content) "
            "VALUES (?, ?, ?, 2, 0, ?)",
            (project_id, root_id, "1.1 编制依据", "本节依据招标文件及现行国家标准编制。"),
        )
        conn.execute("DELETE FROM exports WHERE project_id = ?", (project_id,))
    return project_id


@pytest.mark.parametrize("index", range(len(LAYOUT_PRESETS)))
def test_build_docx_with_every_layout_preset(index):
    """8 套版式预设逐一跑通 build_docx，产物必须是合法可打开的 DOCX"""
    preset_id = LAYOUT_PRESETS[index]["id"]
    project_id = _seed_project(9000 + index)
    config = ExportConfig(preset_id=preset_id, theme_id="blue")
    output_path, _export_id = build_docx(project_id, config=config)
    assert output_path and output_path.endswith(".docx")
    with zipfile.ZipFile(output_path) as zf:
        assert "word/document.xml" in zf.namelist()
        document_xml = zf.read("word/document.xml").decode("utf-8")
    # 主题色必须真的落到文档里
    assert "173f82" in document_xml.lower(), f"版式 {preset_id} 未应用主题色"


def test_build_docx_landscape_sets_page_size():
    """横版预设必须把页面改成 29.7cm × 21cm"""
    project_id = _seed_project(9101)
    config = ExportConfig(preset_id="wide-table-landscape", theme_id="blue")
    output_path, _ = build_docx(project_id, config=config)
    with zipfile.ZipFile(output_path) as zf:
        document_xml = zf.read("word/document.xml").decode("utf-8")
    # 29.7cm ≈ 16838 twips，21cm ≈ 11906 twips
    assert 'w:w="16838"' in document_xml, "横版页面宽度未生效"
    assert 'w:h="11906"' in document_xml, "横版页面高度未生效"


def test_build_docx_page_number_disabled_omits_footer_field():
    """页码关闭时页脚不应再写 PAGE 域"""
    project_id = _seed_project(9102)
    config = resolve_export_config(
        ExportConfig(preset_id="standard-bid", show_header_footer=True)
    )
    assert config.page_number_enabled is False
    output_path, _ = build_docx(project_id, config=config)
    with zipfile.ZipFile(output_path) as zf:
        footer_files = [n for n in zf.namelist() if n.startswith("word/footer")]
        footer_xml = "".join(zf.read(n).decode("utf-8") for n in footer_files) if footer_files else ""
    assert "PAGE" not in footer_xml, "页码已关闭但仍写入了 PAGE 域"


def test_build_docx_page_number_format_applied():
    """页码格式来自预设（紧凑版只显示纯数字）"""
    project_id = _seed_project(9103)
    config = resolve_export_config(
        ExportConfig(preset_id="compact-review", show_header_footer=True)
    )
    assert config.page_number_enabled is True
    assert config.page_number_format == "{page}"
    output_path, _ = build_docx(project_id, config=config)
    with zipfile.ZipFile(output_path) as zf:
        footer_files = [n for n in zf.namelist() if n.startswith("word/footer")]
        footer_xml = "".join(zf.read(n).decode("utf-8") for n in footer_files)
    assert "PAGE" in footer_xml
    assert "第" not in footer_xml, "紧凑版页码不应带「第…页」前缀"


# ==================== E. 查重：目录结构相似度（移植自易标 buildOutlineComparison） ====================


def test_normalize_outline_title_strips_numbering():
    from app.services.duplicate import normalize_outline_title

    cases = {
        "## 1.2 施工组织设计": "施工组织设计",
        "第一章 项目概述": "项目概述",
        "一、编制依据": "编制依据",
        "（一）安全管理": "安全管理",
        "1) 质量控制": "质量控制",
        "施工组织设计": "施工组织设计",
    }
    for raw, expected in cases.items():
        assert normalize_outline_title(raw) == expected, f"{raw} → {normalize_outline_title(raw)}"


def test_bigram_dice_similarity_behaviour():
    from app.services.duplicate import bigram_dice_similarity

    assert bigram_dice_similarity("施工组织设计", "施工组织设计") == 1.0
    # 长标题只差一个字时 Dice 应超过近义阈值 0.86
    assert bigram_dice_similarity("施工组织设计与进度计划安排", "施工组织设计与进度计划") >= 0.86
    # 完全不同的话题应远低于阈值
    assert bigram_dice_similarity("施工组织设计", "安全管理体系") < 0.4
    assert bigram_dice_similarity("", "abc") == 0.0


def test_is_near_synonym_title_covers_short_titles():
    """短标题加字改写时 Dice 不足 0.86，必须由「包含度」判据兜住。

    这是对易标原实现的增强：易标只用 Dice ≥ 0.86，
    对 4~6 字的中文短标题（本项目从纯文本抽取目录时的常见长度）几乎不生效。
    """
    from app.services.duplicate import is_near_synonym_title

    matched, score, criterion = is_near_synonym_title("施工组织设计", "施工组织设计方案")
    assert matched, "短标题改写未被识别"
    assert criterion == "containment"
    assert score < 0.86  # 说明确实是靠包含度判据命中的

    # 不相关标题不能被误判
    assert not is_near_synonym_title("施工组织设计", "安全文明施工")[0]
    assert not is_near_synonym_title("质量控制措施", "进度计划安排")[0]


def test_lcs_similarity_captures_order():
    from app.services.duplicate import lcs_similarity

    left = ["a", "b", "c", "d"]
    assert lcs_similarity(left, left) == 1.0
    # 顺序完全颠倒时公共子序列只剩 1 个
    assert lcs_similarity(left, ["d", "c", "b", "a"]) == 0.25
    assert lcs_similarity([], left) == 0.0


def test_outline_risk_thresholds():
    from app.services.duplicate import outline_risk_from_score

    assert outline_risk_from_score(0.80) == "high"
    assert outline_risk_from_score(0.75) == "high"
    assert outline_risk_from_score(0.60) == "medium"
    assert outline_risk_from_score(0.40) == "low"
    assert outline_risk_from_score(0.10) == "none"


def _toc_file(name: str, titles: list[str]) -> dict:
    """构造查重所需的文件信息（仅目录维度用得到 text/filename/file_id）"""
    return {"filename": name, "file_id": abs(hash(name)) % 100000, "text": "\n".join(titles)}


def test_analyze_toc_identical_outlines_score_one():
    from app.services.duplicate import analyze_toc

    titles = ["第一章 项目概述", "第二章 施工组织设计", "第三章 质量保证措施"]
    result = analyze_toc([_toc_file("a.docx", titles), _toc_file("b.docx", titles)])
    assert result["pairwise"][0]["score"] == 1.0
    assert result["pairwise"][0]["risk"] == "high"
    assert result["duplicate_groups"][0]["common_count"] == 3


def test_analyze_toc_small_outline_fully_covered_is_high_risk():
    """原 Jaccard 算法的核心缺陷：小目录被大目录完全覆盖时只给 0.09 分。

    新算法用「覆盖率」= |交集| / min(|A|,|B|)，此处必须为 1.0（高危抄袭）。
    """
    from app.services.duplicate import analyze_toc

    small = ["第一章 项目概述", "第二章 施工组织设计"]
    large = [f"第{i}章 章节{i}" for i in range(1, 11)] + small
    result = analyze_toc([_toc_file("small.docx", small), _toc_file("large.docx", large)])
    pair = result["pairwise"][0]
    assert pair["title_overlap"] == 1.0, f"覆盖率应为 1.0，实际 {pair['title_overlap']}"
    assert pair["risk"] in ("high", "medium"), f"应判为高风险，实际 {pair['risk']}"


def test_analyze_toc_order_matters():
    """标题集合相同但顺序完全不同时，综合分必须低于顺序一致的情况"""
    from app.services.duplicate import analyze_toc

    titles = ["第一章 甲", "第二章 乙", "第三章 丙", "第四章 丁"]
    ordered = analyze_toc([_toc_file("a.docx", titles), _toc_file("b.docx", titles)])
    reversed_result = analyze_toc([_toc_file("a.docx", titles), _toc_file("b.docx", list(reversed(titles)))])
    assert ordered["pairwise"][0]["score"] > reversed_result["pairwise"][0]["score"]
    assert reversed_result["pairwise"][0]["order_similarity"] == 0.25


def test_analyze_toc_detects_near_synonym_titles():
    from app.services.duplicate import analyze_toc

    result = analyze_toc(
        [
            _toc_file("a.docx", ["第一章 施工组织设计方案"]),
            _toc_file("b.docx", ["第一章 施工组织设计"]),
        ]
    )
    assert result["similar_groups"], "近义标题未被识别"
    assert result["similar_groups"][0]["criterion"] in ("dice", "containment")


def test_analyze_toc_keeps_backward_compatible_keys():
    """前端与报告仍依赖 tocs / matrix / filenames / duplicate_groups 四个旧字段"""
    from app.services.duplicate import analyze_toc

    titles = ["第一章 项目概述", "第二章 施工组织设计"]
    result = analyze_toc([_toc_file("a.docx", titles), _toc_file("b.docx", titles)])
    for key in ("tocs", "matrix", "filenames", "duplicate_groups"):
        assert key in result, f"缺少兼容字段 {key}"
    assert len(result["matrix"]) == 2
    assert result["matrix"][0][1] == result["matrix"][1][0]
    assert result["duplicate_groups"][0]["similarity"] == 1.0


# ==================== F. 查重：文档同源指纹（移植自易标 extractMetadata 指纹体系） ====================


def _make_docx_with_rsid(path, rsid_root: str, rsid_values: list[str], extra_props: str = ""):
    """构造一个带指定 RSID 的最小 docx（只保留指纹提取需要的部件）"""
    document_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f'<w:body><w:p w:rsidR="{rsid_values[0]}" w:rsidRDefault="{rsid_values[0]}" w:rsidRoot="{rsid_root}">'
        "<w:r><w:t>正文</w:t></w:r></w:p>"
        + "".join(f'<w:p w:rsidR="{v}"><w:r><w:t>x</w:t></w:r></w:p>' for v in rsid_values[1:])
        + "</w:body></w:document>"
    )
    core_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties">'
        "<cp:lastModifiedBy>测试</cp:lastModifiedBy></cp:coreProperties>"
    )
    app_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties">'
        f"<Application>{extra_props or 'Microsoft Office Word'}</Application></Properties>"
    )
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("word/document.xml", document_xml)
        zf.writestr("docProps/core.xml", core_xml)
        zf.writestr("docProps/app.xml", app_xml)


def test_extract_docx_fingerprints_reads_rsid(tmp_path):
    from app.services.duplicate import _extract_docx_fingerprints

    path = tmp_path / "a.docx"
    _make_docx_with_rsid(path, "00AB12CD", ["00AB12CD", "00EF34AB"])
    fp = _extract_docx_fingerprints(str(path))
    assert fp["word_rsid_root"] == "00AB12CD"
    assert fp["word_rsid_count"] == 2
    assert fp["word_rsid_fingerprint"]


def test_extract_docx_fingerprints_collects_wps_signals(tmp_path):
    from app.services.duplicate import _extract_docx_fingerprints

    path = tmp_path / "wps.docx"
    _make_docx_with_rsid(
        path, "00AB12CD", ["00AB12CD"], extra_props="Kingsoft WPS Office user_id=zhangsan@example.com"
    )
    fp = _extract_docx_fingerprints(str(path))
    assert fp.get("wps_signals"), "未采集到 WPS/账号痕迹"


def test_extract_pdf_fingerprints_structure(tmp_path):
    from app.services.duplicate import _extract_pdf_fingerprints

    pdf_body = (
        b"%PDF-1.7\n"
        b"1 0 obj << /Type /Catalog >> endobj\n"
        b"2 0 obj << /Type /Sig /FT /Sig /ByteRange [0 10 20 30] >> endobj\n"
        b"3 0 obj << /Type /EmbeddedFile >> endobj\n"
        b"trailer << /ID [<AABBCCDDEEFF00112233445566778899> <AABBCCDDEEFF00112233445566778899>] >>\n"
        b"startxref\n100\n%%EOF\n"
        b"startxref\n200\n%%EOF\n"
    )
    path = tmp_path / "a.pdf"
    path.write_bytes(pdf_body)
    fp = _extract_pdf_fingerprints(str(path))
    assert fp["pdf_header_version"] == "1.7"
    assert fp["pdf_object_count"] == 3
    # 两次 startxref 说明做过一次增量保存
    assert fp["pdf_incremental_update_count"] == 1
    assert fp["pdf_trailer_id"] == "AABBCCDDEEFF00112233445566778899"
    assert fp["pdf_signature_count"] >= 1
    assert fp["pdf_embedded_file_count"] == 1


def test_compare_same_source_flags_shared_rsid_root():
    from app.services.duplicate import compare_same_source

    files = [{"filename": "a.docx"}, {"filename": "b.docx"}, {"filename": "c.pdf"}]
    fingerprints = [
        {"word_rsid_root": "00AB12CD", "word_rsid_fingerprint": "deadbeef"},
        {"word_rsid_root": "00AB12CD", "word_rsid_fingerprint": "deadbeef"},
        {"pdf_trailer_id": "OTHER"},
    ]
    pairs = compare_same_source(files, fingerprints)
    assert len(pairs) == 1
    assert pairs[0]["file_a"] == "a.docx" and pairs[0]["file_b"] == "b.docx"
    assert pairs[0]["severity"] == "high"
    assert any("编辑会话根 ID" in e for e in pairs[0]["evidence"])


def test_compare_same_source_flags_shared_wps_signals_as_medium():
    from app.services.duplicate import compare_same_source

    files = [{"filename": "a.docx"}, {"filename": "b.docx"}]
    fingerprints = [
        {"wps_signals": ["Kingsoft WPS user_id=zhangsan"]},
        {"wps_signals": ["Kingsoft WPS user_id=zhangsan"]},
    ]
    pairs = compare_same_source(files, fingerprints)
    assert len(pairs) == 1
    assert pairs[0]["severity"] == "medium"


def test_compare_same_source_no_false_positive_on_different_files():
    from app.services.duplicate import compare_same_source

    files = [{"filename": "a.docx"}, {"filename": "b.docx"}]
    fingerprints = [
        {"word_rsid_root": "00AB12CD"},
        {"word_rsid_root": "00FF99EE"},
    ]
    assert compare_same_source(files, fingerprints) == []


def test_analyze_metadata_includes_fingerprints_and_pairs(tmp_path):
    """analyze_metadata 必须把指纹与同源疑点一并返回，供报告与前端消费"""
    from app.services.duplicate import analyze_metadata

    path_a = tmp_path / "a.docx"
    path_b = tmp_path / "b.docx"
    _make_docx_with_rsid(path_a, "00AB12CD", ["00AB12CD"])
    _make_docx_with_rsid(path_b, "00AB12CD", ["00AB12CD"])
    result = analyze_metadata(
        [
            {"filename": "a.docx", "file_id": 1, "path": str(path_a), "file_type": "docx"},
            {"filename": "b.docx", "file_id": 2, "path": str(path_b), "file_type": "docx"},
        ]
    )
    assert len(result["fingerprints"]) == 2
    assert result["same_source_pairs"], "未识别出同源文件"
    assert any(a["type"] == "same_source" for a in result["anomalies"])


# ==================== G. 废标检查：错别字定位校验（移植自易标 findVerifiedTypoPosition） ====================


def test_locate_text_span_exact_match():
    from app.services.rejection import locate_text_span

    haystack = "本工程采用框架结构施工。"
    assert locate_text_span("框架结构", haystack) == 5


def test_locate_text_span_tolerates_whitespace_diff():
    """AI 复述时常把原文的换行吃掉，第 2 级去空白匹配要能兜住"""
    from app.services.rejection import locate_text_span

    haystack = "本工程采用\n框架结构施工。"
    # 期望命中原文下标 3（"采"），而不是 compact 文本里的下标
    assert locate_text_span("采用框架结构", haystack) == 3


def test_locate_text_span_tolerates_quote_style_diff():
    """AI 常把中文引号写成 ASCII 引号，第 3 级要能忽略引号差异"""
    from app.services.rejection import locate_text_span

    haystack = "项目名称为“智慧工地”系统"
    assert locate_text_span('"智慧工地"', haystack) == 6


def test_locate_text_span_returns_minus_one_when_absent():
    from app.services.rejection import locate_text_span

    assert locate_text_span("不存在的片段", "本工程采用框架结构施工。") == -1
    assert locate_text_span("", "任意文本") == -1
    assert locate_text_span("任意", "") == -1


def test_build_verified_excerpt_window_is_eight_chars():
    from app.services.rejection import build_verified_excerpt

    text = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    # position=12("M")、span=1 → [12-8, 12+1+8) = [4, 21)
    assert build_verified_excerpt(text, 12, 1) == "EFGHIJKLMNOPQRSTU"


def test_build_verified_excerpt_flattens_newlines():
    from app.services.rejection import build_verified_excerpt

    text = "第一行\n第二行错误字\n第三行"
    position = text.find("错误字")
    excerpt = build_verified_excerpt(text, position, 3)
    assert "\n" not in excerpt
    assert "错误字" in excerpt


def test_build_verified_excerpt_handles_bad_position():
    from app.services.rejection import build_verified_excerpt

    assert build_verified_excerpt("文本", -1, 2) == ""
    assert build_verified_excerpt("", 0, 2) == ""


def test_build_line_hint_counts_lines():
    from app.services.rejection import build_line_hint

    text = "第一行\n第二行错误字\n第三行"
    assert build_line_hint(text, text.find("错误字")) == "第 2 行"
    assert build_line_hint(text, 0) == "第 1 行"
    assert build_line_hint(text, -1) == ""


def test_verify_spelling_error_discards_hallucination():
    """AI 幻觉出的错别字（原文根本没有）必须被标记丢弃，而不是进报告"""
    from app.services.rejection import verify_spelling_error

    original_text = "本工程采用框架结构施工。"
    result = verify_spelling_error({"original": "框架结钩"}, original_text)
    assert result["verified"] is False
    assert result["discard_reason"] == "not_found_in_source"
    assert "position" not in result


def test_verify_spelling_error_missing_original_field():
    from app.services.rejection import verify_spelling_error

    result = verify_spelling_error({"quote": "只有引用没有原文"}, "本工程采用框架结构施工。")
    assert result["verified"] is False
    assert result["discard_reason"] == "missing_original"


def test_verify_spelling_error_fills_position_excerpt_line_hint():
    from app.services.rejection import verify_spelling_error

    original_text = "第一段内容。\n\n本工程采用框架结构施工，工期100日历天。"
    result = verify_spelling_error({"original": "框架结构"}, original_text)
    assert result["verified"] is True
    assert result["position"] == original_text.find("框架结构")
    assert "框架结构" in result["excerpt"]
    assert result["line_hint"] == "第 3 行"


def test_verify_spelling_error_accepts_legacy_field_names():
    """兼容 AI 输出 wrong / wrongText 两种历史字段名"""
    from app.services.rejection import verify_spelling_error

    original_text = "本工程采用框架结构施工。"
    for key in ("wrong", "wrongText"):
        result = verify_spelling_error({key: "框架结构"}, original_text)
        assert result["verified"] is True, f"字段 {key} 未生效"


def test_verify_spelling_error_tolerates_whitespace_diff_in_source():
    from app.services.rejection import verify_spelling_error

    original_text = "本工程采用\n框架结构施工。"
    result = verify_spelling_error({"original": "采用框架结构"}, original_text)
    assert result["verified"] is True
    assert result["position"] == 3


def _fake_collect_factory(payload):
    async def _fake_collect(provider, messages, max_retries=2):
        return payload

    return _fake_collect


async def test_check_spelling_dimension_drops_hallucinations_and_dedupes(monkeypatch):
    """定位不到原文的条目被丢弃；同位置同原文的重复条目只保留一条"""
    from app.services import rejection

    bid_doc = {
        "id": 1,
        "name": "技术标.docx",
        "text": "本工程采用框架结构施工。\n\n施工工期为100日历天。",
    }
    payload = [
        {"original": "框架结钩"},  # 幻觉：原文没有 → 丢弃
        {"original": "施工工期为100日历天"},  # 命中 → 保留
        {"original": "施工工期为100日历天"},  # 与上一条完全同位置 → 去重
        {"original": "本工程采用框架结构施工。"},  # 命中 → 保留
    ]
    monkeypatch.setattr(
        rejection, "collect_json_response", _fake_collect_factory(payload)
    )

    errors = await rejection.check_spelling_dimension(object(), bid_doc)

    assert len(errors) == 2, f"期望 2 条，实际 {len(errors)} 条"
    assert all(e["verified"] is True for e in errors)
    assert all(e["category"] == "错别字" for e in errors)
    assert all(e["bid_doc_name"] == "技术标.docx" for e in errors)
    # 定位信息必须补齐，报告才有人工复核价值
    assert all(e.get("excerpt") and e.get("line_hint") for e in errors)
    originals = {e["original"] for e in errors}
    assert "框架结钩" not in originals


async def test_check_spelling_dimension_handles_provider_failure(monkeypatch):
    """AI 调用失败时应降级为返回空列表，而不是抛出中断整轮检查"""
    from app.services import rejection

    async def _boom(provider, messages, max_retries=2):
        raise RuntimeError("provider down")

    monkeypatch.setattr(rejection, "collect_json_response", _boom)
    errors = await rejection.check_spelling_dimension(
        object(), {"id": 2, "name": "x.docx", "text": "本工程采用框架结构施工。"}
    )
    assert errors == []


# ==================== H. 废标项三轮流程（移植自易标 rejectionCheckTask.cjs） ====================


class _StubTextProvider:
    """记录 chat 调用、按顺序返回预置文本的假 provider（用于三轮流程编排测试）"""

    def __init__(self, replies: list[str]):
        self._replies = list(replies)
        self.calls: list[list] = []

    async def chat(self, messages, **_kwargs):
        self.calls.append(list(messages))
        return self._replies.pop(0) if self._replies else ""

    def system_prompts(self) -> list[str]:
        return [str(c[0].content) for c in self.calls]


def _long_bid_text() -> str:
    """构造带 5 个章节标题的投标文件，保证 _extract_outline_hint 走结构分支"""
    return (
        "第一章 投标函\n工期为100日历天。\n"
        "第二章 施工组织设计\n本工程采用框架结构施工。\n"
        "第三章 项目管理机构\n项目经理张三。\n"
        "第四章 投标报价\n投标总价100万元。\n"
        "第五章 质量承诺\n质量目标合格。"
    )


def test_assign_item_codes_fills_and_dedupes():
    from app.services.rejection import _assign_item_codes

    coded = _assign_item_codes(
        [
            {"description": "无 id"},
            {"id": "r001", "description": "保留原 id"},
            {"id": "r001", "description": "重复 id 必须换新"},
            {"description": "另一个无 id"},
        ]
    )
    ids = [c["id"] for c in coded]
    assert len(set(ids)) == len(ids), f"id 必须唯一，实际 {ids}"
    # 关键回归：自增编号不能"抢走"后面某一项本来合法的 id
    assert coded[1]["id"] == "r001", "已有合法 id 的项必须保留原 id"
    assert coded[2]["id"] != "r001", "重复 id 必须重新分配"
    assert all(i.startswith("r") for i in ids)


def test_assign_item_codes_handles_empty():
    from app.services.rejection import _assign_item_codes

    assert _assign_item_codes([]) == []
    assert _assign_item_codes(None) == []


def test_extract_outline_hint_returns_structure_not_full_text():
    from app.services.rejection import _extract_outline_hint

    hint = _extract_outline_hint(_long_bid_text())
    assert "第一章" in hint and "第五章" in hint
    # 结构分支只返回标题行，不应把正文一起带出来
    assert "本工程采用框架结构施工" not in hint
    assert len(hint) < len(_long_bid_text())


def test_extract_outline_hint_falls_back_for_unstructured_text():
    from app.services.rejection import _extract_outline_hint

    plain = "这是一段没有任何章节标题的投标说明文字。" * 20
    hint = _extract_outline_hint(plain, fallback_chars=100)
    assert hint == plain[:100]
    assert _extract_outline_hint("") == ""


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("critical", "critical"),
        ("high", "critical"),
        ("HIGH", "critical"),
        ("warning", "warning"),
        ("medium", "warning"),
        ("info", "info"),
        ("low", "info"),
        ("", "warning"),
        (None, "warning"),
        ("完全无法识别的值", "warning"),
    ],
)
def test_normalize_severity_aliases(raw, expected):
    """易标用 high/medium/low，本软件前端约定 critical/warning/info，必须归一"""
    from app.services.rejection import _normalize_severity

    assert _normalize_severity(raw) == expected


@pytest.mark.parametrize(
    ("payload", "expected_len"),
    [
        ([{"a": 1}], 1),
        ([{"a": 1}, "非字典项"], 1),
        ({"findings": [{"a": 1}, {"b": 2}]}, 2),
        ({"issues": [{"a": 1}]}, 1),
        ({"description": "单条记录"}, 1),
        (None, 0),
        ("纯文本", 0),
        ({}, 0),
    ],
)
def test_coerce_issue_list_accepts_wrapped_formats(payload, expected_len):
    """第三轮提示词要求数组，但模型常包一层 {"findings": [...]}（易标就是这格式）"""
    from app.services.rejection import _coerce_issue_list

    assert len(_coerce_issue_list(payload)) == expected_len


def test_verify_rejection_evidence_locates_quote():
    from app.services.rejection import verify_rejection_evidence

    text = _long_bid_text()
    result = verify_rejection_evidence({"quote": "工期为100日历天。"}, text)
    assert result["validated"] is True
    assert result["evidence_kind"] == "quote"
    assert result["position"] == text.find("工期为100日历天。")
    assert result["line_hint"] == "第 2 行"
    assert "工期为100日历天" in result["excerpt"]


def test_verify_rejection_evidence_flags_unlocatable_quote():
    """AI 改写过的摘录无法定位 → validated=False，但**不丢弃**（供人工重点复核）"""
    from app.services.rejection import verify_rejection_evidence

    result = verify_rejection_evidence({"quote": "本项目采用钢结构形式。"}, _long_bid_text())
    assert result["validated"] is False
    assert result["evidence_kind"] == "quote"
    assert "position" not in result


def test_verify_rejection_evidence_treats_missing_quote_as_structural():
    """材料缺失类风险本就没有原文摘录，应视为结构性线索而非核验失败"""
    from app.services.rejection import verify_rejection_evidence

    result = verify_rejection_evidence({"description": "缺少资质证书"}, _long_bid_text())
    assert result["validated"] is None
    assert result["evidence_kind"] == "structural"


def test_verify_rejection_evidence_accepts_legacy_field_names():
    from app.services.rejection import verify_rejection_evidence

    text = _long_bid_text()
    for key in ("bidEvidence", "evidence"):
        result = verify_rejection_evidence({key: "工期为100日历天。"}, text)
        assert result["validated"] is True, f"字段 {key} 未生效"


def test_dedup_final_issues_merges_same_code_and_similar_description():
    from app.services.rejection import _dedup_final_issues

    items = [
        {"check_item_id": "r001", "description": "投标工期超过招标文件要求"},
        {"check_item_id": "r001", "description": "投标工期超过招标文件要求"},
    ]
    merged = _dedup_final_issues(items)
    assert len(merged) == 1
    assert merged[0]["merged_count"] == 2


def test_dedup_final_issues_keeps_different_codes_separate():
    from app.services.rejection import _dedup_final_issues

    items = [
        {"check_item_id": "r001", "description": "投标工期超过招标文件要求"},
        {"check_item_id": "r002", "description": "投标工期超过招标文件要求"},
    ]
    assert len(_dedup_final_issues(items)) == 2


def test_dedup_final_issues_prefers_verified_evidence():
    from app.services.rejection import _dedup_final_issues

    items = [
        {"check_item_id": "r001", "description": "工期超期", "validated": False, "quote": "旧"},
        {"check_item_id": "r001", "description": "工期超期", "validated": True, "quote": "新"},
    ]
    merged = _dedup_final_issues(items)
    assert len(merged) == 1
    assert merged[0]["validated"] is True
    assert merged[0]["quote"] == "新"


def test_finalize_rejection_issues_end_to_end():
    from app.services.rejection import _finalize_rejection_issues

    text = _long_bid_text()
    coded = [{"id": "r001", "description": "工期要求"}, {"id": "r002", "description": "报价要求"}]
    raw = [
        {
            "check_item_id": "r001",
            "description": "工期超过要求",
            "severity": "high",
            "quote": "工期为100日历天。",
        },
        {"check_item_id": "不存在的编号", "description": "模型自造条款", "severity": "info"},
        {"check_item_id": "r001", "description": "工期超过要求", "severity": "high"},
    ]
    finalized = _finalize_rejection_issues(raw, {"id": 7, "name": "技术标.docx"}, coded, text)

    assert len(finalized) == 2, "同编号同描述应合并"
    by_code = {item["check_item_id"]: item for item in finalized}
    assert by_code["r001"]["severity"] == "critical", "high 应归一为 critical"
    assert by_code["r001"]["code_matched"] is True
    assert by_code["r001"]["validated"] is True
    assert by_code["r001"]["bid_doc_id"] == 7
    assert by_code["r001"]["bid_doc_name"] == "技术标.docx"
    assert by_code["不存在的编号"]["code_matched"] is False


def test_three_round_enabled_reads_env(monkeypatch):
    from app.services import rejection

    monkeypatch.delenv(rejection.REJECTION_THREE_ROUND_ENV, raising=False)
    assert rejection._three_round_enabled() is True
    for value in ("0", "false", "OFF", "no"):
        monkeypatch.setenv(rejection.REJECTION_THREE_ROUND_ENV, value)
        assert rejection._three_round_enabled() is False, f"{value} 应关闭三轮流程"
    monkeypatch.setenv(rejection.REJECTION_THREE_ROUND_ENV, "1")
    assert rejection._three_round_enabled() is True


async def test_three_rounds_run_in_order_and_chain_outputs(monkeypatch):
    """三轮必须按 分析 → 初稿 → 定稿 顺序执行，且后轮能看到前轮产出"""
    from app.services import rejection

    provider = _StubTextProvider(
        [
            "分析结论：r001 可从电子文件判断；签章事项不纳入检查。",
            "### [r001] 工期超过招标要求\n- 证据：工期为100日历天。\n- 风险：可能废标",
        ]
    )
    captured: dict = {}

    async def _fake_final(_provider, messages, max_retries=2):
        captured["messages"] = messages
        return [
            {
                "check_item_id": "r001",
                "description": "工期超过招标要求",
                "severity": "critical",
                "quote": "工期为100日历天。",
            }
        ]

    monkeypatch.setattr(rejection, "collect_json_response", _fake_final)

    result = await rejection.check_rejection_dimension(
        provider,
        "",
        {"id": 1, "name": "技术标.docx", "text": _long_bid_text()},
        [{"id": "r001", "description": "工期超过招标文件要求"}],
    )

    # 1 次范围分析 + 1 次初稿（文本只有一段）
    assert len(provider.calls) == 2, f"文本轮次数不对: {len(provider.calls)}"
    systems = provider.system_prompts()
    assert any("检查范围分析" in s for s in systems), "缺少第一轮范围分析提示词"
    assert any("初步风险清单草稿" in s for s in systems), "缺少第二轮初稿提示词"

    # 第二轮必须拿到第一轮的分析结论
    assert "分析结论：r001 可从电子文件判断" in str(provider.calls[1][1].content)

    # 第三轮必须拿到前两轮的产出
    final_user = str(captured["messages"][1].content)
    assert "分析结论：r001 可从电子文件判断" in final_user
    assert "### [r001] 工期超过招标要求" in final_user
    assert "REJECTION_FINAL_SYSTEM" not in final_user  # 提示词在 system 侧

    # 结果经过收尾：证据已定位
    assert len(result) == 1
    assert result[0]["validated"] is True
    assert result[0]["line_hint"] == "第 2 行"


async def test_three_rounds_degrade_when_analysis_empty(monkeypatch):
    """第一轮返回空 → 不进入第二轮，直接降级单轮"""
    from app.services import rejection

    provider = _StubTextProvider([""])
    calls: list = []

    async def _fake_json(_provider, messages, max_retries=2):
        calls.append(messages)
        return [
            {
                "check_item_id": "r001",
                "description": "单轮兜底结果",
                "severity": "warning",
                "quote": "投标总价100万元。",
            }
        ]

    monkeypatch.setattr(rejection, "collect_json_response", _fake_json)

    result = await rejection.check_rejection_dimension(
        provider,
        "",
        {"id": 1, "name": "技术标.docx", "text": _long_bid_text()},
        [{"id": "r001", "description": "报价要求"}],
    )

    assert len(provider.calls) == 1, "第一轮失败后不应继续第二轮"
    assert calls, "应回退到单轮检查"
    # 降级路径必须用的是单轮提示词（内容里能看出"对照废标条款清单"）
    assert "对照废标条款清单" in str(calls[0][0].content)
    assert result and result[0]["description"] == "单轮兜底结果"


async def test_three_rounds_degrade_when_final_round_fails(monkeypatch):
    """第三轮异常 → 降级单轮，且单轮结果仍走同一套收尾"""
    from app.services import rejection

    provider = _StubTextProvider(["分析结论", "### [r001] 初稿"])
    call_count = {"n": 0}

    async def _flaky(_provider, messages, max_retries=2):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("定稿轮失败")
        return [{"check_item_id": "r001", "description": "单轮兜底", "severity": "info"}]

    monkeypatch.setattr(rejection, "collect_json_response", _flaky)

    result = await rejection.check_rejection_dimension(
        provider,
        "",
        {"id": 1, "name": "技术标.docx", "text": _long_bid_text()},
        [{"id": "r001", "description": "检查项"}],
    )

    assert call_count["n"] == 2, "应发生一次失败的定稿 + 一次单轮兜底"
    assert result and result[0]["description"] == "单轮兜底"
    assert result[0]["severity"] == "info"
    assert result[0]["code_matched"] is True


async def test_three_rounds_disabled_uses_single_round(monkeypatch):
    """开关关闭时完全不调用文本轮，直接走单轮"""
    from app.services import rejection

    monkeypatch.setenv(rejection.REJECTION_THREE_ROUND_ENV, "0")
    provider = _StubTextProvider([])

    async def _fake_json(_provider, messages, max_retries=2):
        return [{"check_item_id": "r001", "description": "单轮结果", "severity": "warning"}]

    monkeypatch.setattr(rejection, "collect_json_response", _fake_json)

    result = await rejection.check_rejection_dimension(
        provider,
        "",
        {"id": 1, "name": "技术标.docx", "text": _long_bid_text()},
        [{"id": "r001", "description": "检查项"}],
    )

    assert provider.calls == [], "关闭三轮后不应有任何文本轮调用"
    assert result and result[0]["description"] == "单轮结果"


async def test_three_rounds_reports_progress(monkeypatch):
    """三轮流程必须上报轮次文案，否则前端看起来像卡死"""
    from app.services import rejection

    provider = _StubTextProvider(["分析结论", "### [r001] 初稿"])

    async def _fake_final(_provider, messages, max_retries=2):
        return []

    monkeypatch.setattr(rejection, "collect_json_response", _fake_final)

    seen: list[str] = []
    await rejection.check_rejection_dimension(
        provider,
        "",
        {"id": 1, "name": "技术标.docx", "text": _long_bid_text()},
        [{"id": "r001", "description": "检查项"}],
        progress_callback=seen.append,
    )

    assert any("第一轮" in m for m in seen)
    assert any("第二轮" in m for m in seen)
    assert any("第三轮" in m for m in seen)


async def test_progress_callback_failure_does_not_break_check(monkeypatch):
    """进度回调抛异常不能影响检查本身"""
    from app.services import rejection

    provider = _StubTextProvider(["分析结论", "### [r001] 初稿"])

    async def _fake_final(_provider, messages, max_retries=2):
        return [{"check_item_id": "r001", "description": "结果", "severity": "warning"}]

    monkeypatch.setattr(rejection, "collect_json_response", _fake_final)

    def _boom(_message: str) -> None:
        raise RuntimeError("回调炸了")

    result = await rejection.check_rejection_dimension(
        provider,
        "",
        {"id": 1, "name": "技术标.docx", "text": _long_bid_text()},
        [{"id": "r001", "description": "检查项"}],
        progress_callback=_boom,
    )
    assert result and result[0]["description"] == "结果"


async def test_three_rounds_empty_findings_is_valid_not_degraded(monkeypatch):
    """第三轮合法返回空数组（确实没问题）不应触发降级——否则会白跑一遍单轮"""
    from app.services import rejection

    provider = _StubTextProvider(["分析结论", "未发现风险"])
    call_count = {"n": 0}

    async def _fake_final(_provider, messages, max_retries=2):
        call_count["n"] += 1
        return []

    monkeypatch.setattr(rejection, "collect_json_response", _fake_final)

    result = await rejection.check_rejection_dimension(
        provider,
        "",
        {"id": 1, "name": "技术标.docx", "text": _long_bid_text()},
        [{"id": "r001", "description": "检查项"}],
    )

    assert result == []
    assert call_count["n"] == 1, "空结果不应再跑一次单轮"


def test_export_report_includes_evidence_verification():
    """报告必须带上证据核验结论，人工复核才能直接跳到行"""
    from app.services import rejection

    task_id = rejection.create_task(
        project_id=None,
        tender_text="招标文件",
        bid_documents=[{"id": 1, "name": "技术标.docx", "text": "工期为100日历天。"}],
        check_items=[{"id": "r001", "description": "工期要求"}],
        enabled_dimensions=["rejection"],
    )
    rejection.update_task_status(
        task_id,
        "completed",
        results={
            "rejection": [
                {
                    "id": "i1",
                    "description": "工期超期",
                    "severity": "critical",
                    "quote": "工期为100日历天。",
                    "validated": True,
                    "line_hint": "第 2 行",
                },
                {
                    "id": "i2",
                    "description": "摘录对不上",
                    "severity": "warning",
                    "quote": "不存在的原文",
                    "validated": False,
                },
                {"id": "i3", "description": "缺少资质证书", "severity": "warning", "validated": None},
            ],
            "spelling": [],
            "logic": [],
        },
    )

    path = rejection.export_report(task_id)
    from docx import Document

    text = "\n".join(p.text for p in Document(path).paragraphs)
    assert "废标项证据核验" in text, "报告缺少证据核验概览"
    assert "已在原文定位 1 项" in text
    assert "未能核验 1 项" in text
    assert "第 2 行" in text, "已核验条目必须给出行号"
    assert "未能核验（摘录与原文不一致" in text, "未核验条目必须给出提示"


# ==================== I. 目录审核门禁（移植自易标 outlineGenerationTaskV2.cjs 的 outline_review 阶段） ====================


def _gate_outline() -> list[dict]:
    """一份结构合规的目录：2 个一级章节，每个 2 个二级，每个二级 2 个三级（共 8 个叶子）"""
    return [
        {
            "id": "1",
            "title": "施工组织设计",
            "children": [
                {
                    "id": "1.1",
                    "title": "编制依据与适用范围",
                    "children": [
                        {"id": "1.1.1", "title": "国家标准与规范引用", "children": []},
                        {"id": "1.1.2", "title": "地方标准与规范引用", "children": []},
                    ],
                },
                {
                    "id": "1.2",
                    "title": "总体施工部署",
                    "children": [
                        {"id": "1.2.1", "title": "施工区段划分原则", "children": []},
                        {"id": "1.2.2", "title": "施工顺序与流水段安排", "children": []},
                    ],
                },
            ],
        },
        {
            "id": "2",
            "title": "质量管理体系",
            "children": [
                {
                    "id": "2.1",
                    "title": "质量目标与指标",
                    "children": [
                        {"id": "2.1.1", "title": "分项工程合格率指标", "children": []},
                        {"id": "2.1.2", "title": "观感质量验收指标", "children": []},
                    ],
                },
                {
                    "id": "2.2",
                    "title": "质量责任划分",
                    "children": [
                        {"id": "2.2.1", "title": "项目经理质量职责", "children": []},
                        {"id": "2.2.2", "title": "技术负责人质量职责", "children": []},
                    ],
                },
            ],
        },
    ]


def test_flatten_outline_covers_all_levels():
    from app.services.ai import outline_gate

    records = outline_gate._flatten_outline(_gate_outline())
    assert len(records) == 14, f"2+4+8=14，实际 {len(records)}"
    assert {r["level"] for r in records} == {1, 2, 3}
    assert all(r["path"] for r in records)
    # 叶子（三级）节点没有子节点
    assert all(r["child_count"] == 0 for r in records if r["level"] == 3)


def test_compute_facts_counts_and_depth():
    from app.services.ai import outline_gate

    facts = outline_gate.compute_outline_facts(_gate_outline())
    assert facts["node_count"] == 14
    assert facts["level1_count"] == 2
    assert facts["leaf_count"] == 8
    assert facts["max_depth"] == 3
    assert facts["node_counts_by_level"] == {"1": 2, "2": 4, "3": 8}
    assert facts["structure_valid"] is True


def test_compute_facts_detects_single_child():
    from app.services.ai import outline_gate

    outline = _gate_outline()
    outline[0]["children"][0]["children"].pop()  # 1.1 只剩一个三级
    facts = outline_gate.compute_outline_facts(outline)
    assert [n["path"] for n in facts["single_child_nodes"]] == ["1/1.1"]
    assert facts["structure_valid"] is False


@pytest.mark.parametrize(
    "bad_title",
    ["__SUMMARY__ 施工部署", "{{项目名称}}部署", "待补充", "TODO 补充说明", "XXXX 章节", "（略）"],
)
def test_compute_facts_detects_placeholder_residue(bad_title):
    """占位符/未渲染残留：框架一直在报 missing variables，但此前没人检查最终目录"""
    from app.services.ai import outline_gate

    outline = _gate_outline()
    outline[0]["children"][0]["children"][0]["title"] = bad_title
    facts = outline_gate.compute_outline_facts(outline)
    assert facts["placeholder_titles"], f"未检出占位符: {bad_title}"
    assert facts["placeholder_titles"][0]["title"] == bad_title


@pytest.mark.parametrize(
    ("mutate", "description"),
    [
        (lambda n: n.pop("id", None), "缺 id"),
        (lambda n: n.update(id="1.1.1.1"), "段数多于层级"),
        (lambda n: n.update(id="1"), "段数少于层级"),
        (lambda n: n.update(id="1.01"), "前导零"),
        (lambda n: n.update(id="1.a"), "非数字"),
        (lambda n: n.update(id="0.1"), "零开头"),
    ],
)
def test_compute_facts_detects_invalid_ids(mutate, description):
    """id 必须形如 1 / 1.1 / 1.1.1（参考实现的 schema：^[1-9]\\d*(\\.[1-9]\\d*){level-1}$）"""
    from app.services.ai import outline_gate

    outline = _gate_outline()
    target = outline[0]["children"][0]["children"][0]
    mutate(target)
    facts = outline_gate.compute_outline_facts(outline)
    assert facts["invalid_id_nodes"], f"未检出非法 id（{description}）"
    assert facts["structure_valid"] is False


def test_compute_facts_detects_exact_duplicate_titles():
    from app.services.ai import outline_gate

    outline = _gate_outline()
    outline[0]["children"][1]["title"] = "编制依据与适用范围"
    facts = outline_gate.compute_outline_facts(outline)
    assert len(facts["duplicate_titles"]) == 1
    assert len(facts["duplicate_titles"][0]["nodes"]) == 2


def test_compute_facts_detects_near_duplicate_titles():
    from app.services.ai import outline_gate

    outline = _gate_outline()
    outline[0]["children"][0]["title"] = "质量保证体系"
    outline[0]["children"][1]["title"] = "质量保证措施"
    facts = outline_gate.compute_outline_facts(outline)
    pairs = [(d["a"]["title"], d["b"]["title"]) for d in facts["near_duplicate_titles"]]
    assert ("质量保证体系", "质量保证措施") in pairs
    assert facts["near_duplicate_titles"][0]["similarity"] >= 0.8


def test_compute_facts_skips_near_duplicate_when_too_large():
    """节点数超过上限时只查完全重复，避免 O(n²) 拖垮自检"""
    from app.services.ai import outline_gate

    outline = [
        {
            "id": str(i + 1),
            "title": f"章节{i + 1}",
            "children": [
                {
                    "id": f"{i + 1}.1",
                    "title": f"子项{i + 1}",
                    "children": [
                        {"id": f"{i + 1}.1.{j + 1}", "title": f"叶子{i + 1}-{j + 1}", "children": []}
                        for j in range(4)
                    ],
                }
            ],
        }
        for i in range(180)
    ]
    facts = outline_gate.compute_outline_facts(outline)
    assert facts["node_count"] > outline_gate._NEAR_DUP_NODE_LIMIT
    assert facts["near_duplicate_skipped"] is True
    assert facts["near_duplicate_titles"] == []


def test_compute_facts_title_length_violations():
    from app.services.ai import outline_gate

    outline = _gate_outline()
    long_title = "这是一个特别特别长的标题用于验证长度上限是否生效超过三十个字符"
    assert len(long_title) > outline_gate.TITLE_MAX_CHARS
    outline[0]["children"][0]["children"][0]["title"] = "岗"
    outline[0]["children"][0]["children"][1]["title"] = long_title
    facts = outline_gate.compute_outline_facts(outline)
    lengths = sorted(v["length"] for v in facts["title_length_violations"])
    assert lengths == [1, len(long_title)]


def test_compute_facts_leaf_range():
    from app.services.ai import outline_gate

    facts = outline_gate.compute_outline_facts(_gate_outline(), expected_leaf_count=8)
    assert facts["target_leaf_count"] == 8
    assert facts["leaf_acceptable_range"] == [6, 10]
    assert facts["leaf_within_range"] is True

    facts = outline_gate.compute_outline_facts(_gate_outline(), expected_leaf_count=20)
    assert facts["leaf_within_range"] is False


def test_compute_facts_without_expected_leaf_count_skips_check():
    from app.services.ai import outline_gate

    facts = outline_gate.compute_outline_facts(_gate_outline())
    assert facts["target_leaf_count"] is None
    assert facts["leaf_acceptable_range"] is None
    assert facts["leaf_within_range"] is None


def test_compute_facts_score_coverage_reports_missing():
    from app.services.ai import outline_gate

    facts = outline_gate.compute_outline_facts(
        _gate_outline(), scoring_titles=["施工组织设计", "质量管理体系", "安全文明施工"]
    )
    coverage = facts["score_coverage"]
    assert len(coverage["matched"]) == 2
    assert coverage["missing"] == ["安全文明施工"]
    assert coverage["unexpected"] == []
    assert coverage["valid"] is False


def test_compute_facts_score_coverage_reports_unexpected():
    from app.services.ai import outline_gate

    facts = outline_gate.compute_outline_facts(
        _gate_outline(), scoring_titles=["施工组织设计", "质量管理体系"]
    )
    coverage = facts["score_coverage"]
    assert coverage["missing"] == []
    assert coverage["unexpected"] == []
    assert coverage["valid"] is True

    facts = outline_gate.compute_outline_facts(
        _gate_outline(), scoring_titles=["施工组织设计"]
    )
    assert [u["title"] for u in facts["score_coverage"]["unexpected"]] == ["质量管理体系"]


def test_compute_facts_without_scoring_skips_coverage():
    from app.services.ai import outline_gate

    assert outline_gate.compute_outline_facts(_gate_outline())["score_coverage"] is None


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"min_words": 10000, "max_words": 20000}, 5),  # ceil(15000/3000)
        ({"max_words": 30000}, 8),  # 30000//3000 - 2
        ({"min_words": 15000}, 7),  # ceil(15000/3000) + 2
        ({"total_words": 24000}, 8),  # ceil(24000/3000)
        ({}, None),
        ({"total_words": 0}, None),
        ({"total_words": "不是数字"}, None),
    ],
)
def test_derive_target_leaf_count(kwargs, expected):
    """移植自参考实现 deriveTargetLeafCount（sectionWords 默认 3000）"""
    from app.services.ai import outline_gate

    assert outline_gate.derive_target_leaf_count(**kwargs) == expected


def test_derive_target_leaf_count_respects_section_words():
    from app.services.ai import outline_gate

    assert outline_gate.derive_target_leaf_count(total_words=10000, section_words=2000) == 5
    assert outline_gate.derive_target_leaf_count(total_words=10000, section_words=0) is None


def test_renumber_outline_ids_fills_missing_and_makes_contiguous():
    from app.services.ai import outline_gate

    outline = _gate_outline()
    # 模拟 _ensure_min_children 补全出来的节点：只有 title/level/children，没有 id
    outline[0]["children"].append(
        {
            "title": "补充的二级目录",
            "level": 2,
            "children": [
                {"title": "补充的三级目录一", "level": 3, "children": []},
                {"title": "补充的三级目录二", "level": 3, "children": []},
            ],
        }
    )
    facts = outline_gate.compute_outline_facts(outline)
    assert facts["invalid_id_nodes"], "缺 id 的节点应被检出"

    outline, changed = outline_gate.renumber_outline_ids(outline)
    assert changed > 0
    facts = outline_gate.compute_outline_facts(outline)
    assert facts["invalid_id_nodes"] == []
    assert outline[0]["children"][2]["id"] == "1.3"
    assert outline[0]["children"][2]["children"][0]["id"] == "1.3.1"


def test_renumber_outline_ids_is_idempotent():
    from app.services.ai import outline_gate

    outline = _gate_outline()
    _, first = outline_gate.renumber_outline_ids(outline)
    _, second = outline_gate.renumber_outline_ids(outline)
    assert first == 0, "本就合规的编号不应被改动"
    assert second == 0


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ('[{"name": "施工组织设计"}, {"title": "质量管理体系"}]', ["施工组织设计", "质量管理体系"]),
        (["施工组织设计", "质量管理体系"], ["施工组织设计", "质量管理体系"]),
        ({"items": [{"name": "A"}, {"name": "B"}]}, ["A", "B"]),
        ({"scoring_items": [{"name": "A"}]}, ["A"]),
        ("", []),
        (None, []),
        ("这是一段非 JSON 的评分说明\n第二行", ["这是一段非 JSON 的评分说明", "第二行"]),
    ],
)
def test_parse_scoring_item_titles(payload, expected):
    from app.services.ai import outline_gate

    assert outline_gate.parse_scoring_item_titles(payload) == expected


def test_parse_scoring_item_titles_filters_system_warning():
    """normalize_writing_targets 会插入 __weight_warning__ 系统提示项，不能当评分要点"""
    from app.services.ai import outline_gate

    payload = [
        {"id": "__weight_warning__", "name": "[权重异常] 评分项权重之和为 90"},
        {"name": "施工组织设计"},
    ]
    assert outline_gate.parse_scoring_item_titles(payload) == ["施工组织设计"]


def test_collect_deterministic_issues_severity_and_confirmation():
    from app.services.ai import outline_gate

    outline = _gate_outline()
    outline[0]["children"][0]["children"][0]["title"] = "__SUMMARY__"
    outline[0]["children"][0]["children"][1]["title"] = "岗"
    facts = outline_gate.compute_outline_facts(
        outline, scoring_titles=["施工组织设计", "质量管理体系", "安全文明施工"]
    )
    issues = outline_gate.collect_deterministic_issues(facts)
    by_category = {i["category"]: i for i in issues}

    # 占位符：error，且可机械修复 → 不需用户确认
    assert by_category["placeholder"]["severity"] == "error"
    assert by_category["placeholder"]["confirmation_required"] is False
    # 标题过短：warning，不需用户确认
    assert by_category["title-length"]["severity"] == "warning"
    assert by_category["title-length"]["confirmation_required"] is False
    # 评分未覆盖：warning，需用户决定是加章节还是改标题
    assert by_category["score-coverage"]["severity"] == "warning"
    assert by_category["score-coverage"]["confirmation_required"] is True
    assert all(i["source"] == "host" for i in issues)


def test_collect_deterministic_issues_empty_for_clean_outline():
    from app.services.ai import outline_gate

    facts = outline_gate.compute_outline_facts(_gate_outline())
    assert outline_gate.collect_deterministic_issues(facts) == []


@pytest.mark.parametrize(
    ("issues", "expected"),
    [
        ([], "passed"),
        ([{"severity": "warning", "confirmation_required": False}], "simple_fix"),
        ([{"severity": "error", "confirmation_required": False}], "user_feedback"),
        ([{"severity": "warning", "confirmation_required": True}], "user_feedback"),
        (
            [
                {"severity": "warning", "confirmation_required": False},
                {"severity": "info", "confirmation_required": False},
            ],
            "simple_fix",
        ),
    ],
)
def test_derive_status(issues, expected):
    from app.services.ai import outline_gate

    assert outline_gate.derive_status(issues) == expected


def test_map_ai_issues_uses_category_policy():
    from app.services.ai import outline_gate

    mapped = outline_gate.map_ai_issues(
        [
            {"type": "generic_l3", "node_id": "1.1.1", "fix": "注入技术含量"},
            {"type": "overlap", "nodes": ["1.4.5", "1.5.2"], "fix": "区分侧重点"},
            {"type": "truncated", "fix": "继续生成"},
            "不是字典",
        ]
    )
    assert len(mapped) == 3
    assert mapped[0]["category"] == "generic_l3"
    assert mapped[0]["confirmation_required"] is False
    assert mapped[0]["source"] == "ai"
    assert mapped[1]["node_ids"] == ["1.4.5", "1.5.2"]
    assert mapped[1]["confirmation_required"] is True
    assert mapped[2]["severity"] == "error"


def test_map_ai_issues_handles_non_list():
    from app.services.ai import outline_gate

    assert outline_gate.map_ai_issues(None) == []
    assert outline_gate.map_ai_issues({"issues": []}) == []


def test_build_verdict_dedupes_same_problem():
    from app.services.ai import outline_gate

    facts = outline_gate.compute_outline_facts(_gate_outline())
    det = [{"category": "placeholder", "problem": "同一问题", "severity": "error", "confirmation_required": False}]
    ai = [{"category": "placeholder", "problem": "同一问题", "severity": "error", "confirmation_required": False}]
    verdict = outline_gate.build_verdict(facts, det, ai)
    assert verdict["issue_count"] == 1
    assert verdict["ai_reviewed"] is True


def test_build_verdict_degrades_when_ai_fails():
    """AI 审查失败必须降级放行，不能因为一次超时就毁掉整份目录"""
    from app.services.ai import outline_gate

    facts = outline_gate.compute_outline_facts(_gate_outline())
    verdict = outline_gate.build_verdict(facts, [], None, ai_error="超时")
    assert verdict["status"] == "passed"
    assert verdict["ai_reviewed"] is False
    assert verdict["ai_error"] == "超时"


@pytest.mark.parametrize(
    ("node_count", "det_count", "expected"),
    [
        (10, 0, False),  # 干净的小目录：跳过 AI，保持既有延迟特征
        (200, 0, True),  # 大目录：跑 AI（原实现的阈值就是 80）
        (10, 1, True),  # 宿主已发现问题：必须跑 AI 拿语义层补充判断
    ],
)
def test_should_run_ai_review(node_count, det_count, expected):
    from app.services.ai import outline_gate

    facts = {"node_count": node_count}
    det = [{"category": "placeholder"}] * det_count
    assert outline_gate.should_run_ai_review(facts, det) is expected


def test_summarize_issues_groups_by_category():
    from app.services.ai import outline_gate

    assert outline_gate.summarize_issues([]) == "未发现问题"
    summary = outline_gate.summarize_issues(
        [{"category": "placeholder"}, {"category": "placeholder"}, {"category": "single-child"}]
    )
    assert "placeholder×2" in summary
    assert "single-child×1" in summary


def test_build_gate_user_prompt_hands_facts_to_ai():
    """关键设计：宿主算好的事实要前置给 AI，并明确要求不要重算"""
    from app.services.ai import outline_gate

    facts = outline_gate.compute_outline_facts(_gate_outline())
    prompt = outline_gate.build_gate_user_prompt(facts, "[]")
    assert "宿主已完成的确定性统计" in prompt
    assert "不要重新统计" in prompt
    assert "节点总数" in prompt
    assert "评分要点覆盖情况" in prompt


# ---- I.2 门禁与自检阶段的接线 ----


def _outline_with_missing_ids() -> list[dict]:
    """模拟 `_ensure_min_children` 补全后的目录：新增节点只有 title/level/children，没有 id"""
    outline = _gate_outline()
    outline[0]["children"].append(
        {
            "title": "补充的二级目录",
            "level": 2,
            "children": [
                {"title": "补充的三级目录一", "level": 3, "children": []},
                {"title": "补充的三级目录二", "level": 3, "children": []},
            ],
        }
    )
    return outline


def test_ensure_min_children_output_lacks_ids_but_gate_fixes_it():
    """端到端回归：`_ensure_min_children` 补全的节点确实不带 id（真 BUG）。

    它在 `_fix_all_ids` 之后运行，而最终审核会把这份目录推给前端编辑；
    缺 id 会让前端按 id 定位节点时改错对象。门禁必须检出并自动重排。
    """
    from app.services.ai import outline_gate
    from app.services.ai.outline_common import _ensure_min_children

    outline = [
        {"id": "1", "title": "施工组织设计", "children": []},
        {
            "id": "2",
            "title": "质量管理体系",
            "children": [{"id": "2.1", "title": "质量目标", "children": []}],
        },
    ]
    filled = _ensure_min_children(outline, min_level2=3, min_level3=5)

    facts_before = outline_gate.compute_outline_facts(filled)
    assert facts_before["invalid_id_nodes"], "补全出来的节点应缺 id（这是被修复的真 BUG）"

    fixed, changed = outline_gate.renumber_outline_ids(filled)
    assert changed > 0
    assert outline_gate.compute_outline_facts(fixed)["invalid_id_nodes"] == []


async def test_self_check_renumbers_missing_ids(monkeypatch):
    """门禁自动修复缺 id 节点，且修复动作记录在裁决里"""
    from app.services.ai import workflows_outline_v51 as w
    from app.services.ai import outline_gate
    from types import SimpleNamespace

    async def _unexpected(*_a, **_k):
        raise AssertionError("干净且小规模的目录不应触发 AI 审查")

    monkeypatch.setattr(w, "collect_json_response", _unexpected)
    state = SimpleNamespace()

    result = await w._self_check_v51(object(), _outline_with_missing_ids(), 0.0, v51_state=state)

    assert outline_gate.compute_outline_facts(result)["invalid_id_nodes"] == []
    assert any("编号重排" in a for a in state.gate_report["auto_fixed"])


async def test_self_check_writes_gate_report_and_reports_progress(monkeypatch):
    """门禁裁决要落到 v51_state 并通过进度回调上报（此前自检结论只进日志，无人知晓）"""
    from app.services.ai import workflows_outline_v51 as w
    from types import SimpleNamespace

    outline = _gate_outline()
    outline[0]["children"][0]["children"][0]["title"] = "__SUMMARY__ 待补充"

    async def _fake(_provider, _messages, **_kwargs):
        return {"issues": [{"type": "generic_l3", "node_id": "1.1.1", "fix": "注入技术含量"}]}

    monkeypatch.setattr(w, "collect_json_response", _fake)
    state = SimpleNamespace()
    messages: list[str] = []

    async def _cb(_phase, message):
        messages.append(message)

    await w._self_check_v51(
        object(), outline, 0.0, phase_callback=_cb, v51_state=state
    )

    report = state.gate_report
    assert report is not None
    categories = {i["category"] for i in report["issues"]}
    assert "placeholder" in categories, "占位符残留必须被检出"
    assert "generic_l3" in categories, "AI 语义审结论必须并入同一份裁决"
    assert report["status"] == "user_feedback"
    assert report["ai_reviewed"] is True
    assert any("门禁检查完成" in m for m in messages)


async def test_self_check_degrades_when_ai_fails(monkeypatch):
    """AI 审查失败只降级，不阻断：目录照常返回，失败原因记录在裁决里"""
    from app.services.ai import workflows_outline_v51 as w
    from types import SimpleNamespace

    outline = _gate_outline()
    outline[0]["children"][0]["children"][0]["title"] = "__SUMMARY__ 待补充"

    async def _boom(*_a, **_k):
        raise RuntimeError("AI 挂了")

    monkeypatch.setattr(w, "collect_json_response", _boom)
    state = SimpleNamespace()

    result = await w._self_check_v51(object(), outline, 0.0, v51_state=state)

    assert result, "AI 失败不得让自检返回空目录"
    assert state.gate_report["ai_error"]
    assert state.gate_report["ai_reviewed"] is False
    assert state.gate_report["status"] == "user_feedback"


async def test_self_check_skips_ai_for_clean_small_outline(monkeypatch):
    """干净的小目录跳过 AI 审查，保持既有延迟特征（原实现就是 >80 节点才做 AI 深度检查）"""
    from app.services.ai import workflows_outline_v51 as w
    from types import SimpleNamespace

    calls = {"n": 0}

    async def _fake(*_a, **_k):
        calls["n"] += 1
        return {"issues": []}

    monkeypatch.setattr(w, "collect_json_response", _fake)
    state = SimpleNamespace()

    await w._self_check_v51(object(), _gate_outline(), 0.0, v51_state=state)

    assert calls["n"] == 0
    assert state.gate_report["ai_skipped"] is True
    assert state.gate_report["status"] == "passed"


async def test_ai_deep_check_injects_host_facts(monkeypatch):
    """关键设计：宿主算好的事实前置给 AI，并明确要求不要重算"""
    from app.services.ai import workflows_outline_v51 as w
    from app.services.ai import outline_gate

    captured: dict = {}

    async def _fake(_provider, messages, **_kwargs):
        captured["messages"] = messages
        return {"issues": [{"type": "generic_l3", "node_id": "1.1.1", "fix": "注入技术含量"}]}

    monkeypatch.setattr(w, "collect_json_response", _fake)
    outline = _gate_outline()
    facts = outline_gate.compute_outline_facts(outline)

    issues, error = await w._ai_deep_check(object(), outline, None, facts=facts)

    assert error is None
    assert issues and issues[0]["category"] == "generic_l3"
    assert issues[0]["source"] == "ai"
    user_prompt = str(captured["messages"][1].content)
    assert "宿主已完成的确定性统计" in user_prompt
    assert "不要重新统计" in user_prompt
    assert "节点总数" in user_prompt
    # system 侧仍是既有的自检提示词（用户可在提示词管理页编辑）
    assert "检查提纲结构问题" in str(captured["messages"][0].content)


async def test_ai_deep_check_reports_bad_structure(monkeypatch):
    from app.services.ai import workflows_outline_v51 as w

    async def _fake(*_a, **_k):
        return ["这不是字典"]

    monkeypatch.setattr(w, "collect_json_response", _fake)
    issues, error = await w._ai_deep_check(object(), _gate_outline())

    assert issues is None
    assert error, "AI 返回结构异常必须给出错误信息（供裁决降级）"


# ---- I.3 字数预算解析（接通叶子数区间检查） ----


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("技术方案不少于 20000 字", {"min_words": 20000, "max_words": None, "total_words": None}),
        ("技术方案不低于 2 万字", {"min_words": 20000, "max_words": None, "total_words": None}),
        ("技术方案不超过 30000 字", {"min_words": None, "max_words": 30000, "total_words": None}),
        ("技术方案最多 3 万字", {"min_words": None, "max_words": 30000, "total_words": None}),
        ("技术方案 10000~20000 字", {"min_words": 10000, "max_words": 20000, "total_words": None}),
        ("技术方案 1 万~2 万字", {"min_words": 10000, "max_words": 20000, "total_words": None}),
        ("技术方案 3-5万字", {"min_words": 30000, "max_words": 50000, "total_words": None}),
        ("技术方案约 30000 字", {"min_words": None, "max_words": None, "total_words": 30000}),
        ("投标文件总字数约 5 万字", {"min_words": None, "max_words": None, "total_words": 50000}),
        ("技术方案 30000 字以上", {"min_words": None, "max_words": None, "total_words": 30000}),
        ("详见招标文件第三章", {"min_words": None, "max_words": None, "total_words": None}),
        ("", {"min_words": None, "max_words": None, "total_words": None}),
    ],
)
def test_parse_word_budget(text, expected):
    from app.services.ai import outline_gate

    assert outline_gate.parse_word_budget(text) == expected


def test_parse_word_budget_rejects_non_string():
    from app.services.ai import outline_gate

    for bad in (None, 123, [], {"min_words": 1}):
        assert outline_gate.parse_word_budget(bad) == {
            "min_words": None,
            "max_words": None,
            "total_words": None,
        }


def test_parse_word_budget_prefers_range_over_min():
    """区间信息量最大：同时出现区间与下限时以区间为准"""
    from app.services.ai import outline_gate

    budget = outline_gate.parse_word_budget("技术方案 10000~20000 字，不得少于 8000 字")
    assert budget["min_words"] == 10000
    assert budget["max_words"] == 20000


def test_parse_word_budget_feeds_derive_target_leaf_count():
    """两个函数的返回/入参契约必须能直接对接（防止将来签名漂移）"""
    from app.services.ai import outline_gate

    budget = outline_gate.parse_word_budget("技术方案 10000~20000 字")
    assert outline_gate.derive_target_leaf_count(**budget) == 5


async def test_self_check_derives_leaf_count_from_word_budget(monkeypatch):
    """ENHANCE 2026-09-14：`expected_leaf_count` 此前无任何调用方赋值，
    leaf-count 检查实际是死代码；现由字数预算推导接通。"""
    from app.services.ai import workflows_outline_v51 as w
    from types import SimpleNamespace

    async def _empty(*_a, **_k):
        return {"issues": []}

    monkeypatch.setattr(w, "collect_json_response", _empty)

    # 1) 预算 30000 字 → 目标 10 → 区间 [8,12]，8 个叶子落在区间内
    state = SimpleNamespace()
    await w._self_check_v51(
        object(),
        _gate_outline(),
        0.0,
        v51_state=state,
        word_budget_text="技术方案约 30000 字",
    )
    facts = state.gate_report["facts"]
    assert facts["target_leaf_count"] == 10
    assert facts["leaf_acceptable_range"] == [8, 12]
    assert facts["leaf_within_range"] is True

    # 2) 预算「不少于 60000 字」→ 目标 22 → 8 个叶子远低于区间，应报 leaf-count
    state2 = SimpleNamespace()
    await w._self_check_v51(
        object(),
        _gate_outline(),
        0.0,
        v51_state=state2,
        word_budget_text="技术方案不少于 60000 字",
    )
    facts2 = state2.gate_report["facts"]
    assert facts2["target_leaf_count"] == 22
    assert facts2["leaf_within_range"] is False
    leaf_issues = [i for i in state2.gate_report["issues"] if i["category"] == "leaf-count"]
    assert leaf_issues and leaf_issues[0]["confirmation_required"] is True

    # 3) 显式传入优先于预算解析
    state3 = SimpleNamespace()
    await w._self_check_v51(
        object(),
        _gate_outline(),
        0.0,
        v51_state=state3,
        expected_leaf_count=8,
        word_budget_text="技术方案约 30000 字",
    )
    assert state3.gate_report["facts"]["target_leaf_count"] == 8

    # 4) 没有预算文本 → 跳过区间检查（保持既有行为）
    state4 = SimpleNamespace()
    await w._self_check_v51(object(), _gate_outline(), 0.0, v51_state=state4)
    assert state4.gate_report["facts"]["target_leaf_count"] is None
    assert state4.gate_report["facts"]["leaf_within_range"] is None


# ==================== J. 正文一致性修补（移植自易标 auditing 阶段的补丁引擎） ====================


# ---- J.1 补丁引擎（纯函数） ----


def test_find_exact_occurrences_is_non_overlapping():
    from app.services import text_patch as tp

    assert tp.find_exact_occurrences("aaaa", "aa") == [0, 2]
    assert tp.find_exact_occurrences("abc", "x") == []
    assert tp.find_exact_occurrences("abc", "") == []
    assert tp.find_exact_occurrences("", "a") == []


def test_extract_line_range_text_bounds():
    from app.services import text_patch as tp

    content = "第一行\n第二行\n第三行"
    assert tp.extract_line_range_text(content, 1, 1) == "第一行"
    assert tp.extract_line_range_text(content, 2, 3) == "第二行\n第三行"
    # end 超过总行数 → 视为无效线索（与参考实现一致）
    assert tp.extract_line_range_text(content, 1, 9) is None
    # start_line=0 表示「AI 没给行号线索」，按参考实现夹到第 1 行；
    # `apply_exact_patch` 里用 `start_line > 0` 判断是否尝试行号区间，所以 0 不会走这条分支。
    assert tp.extract_line_range_text(content, 0, 1) == "第一行"
    # 负行号同样被夹到 1
    assert tp.extract_line_range_text(content, -5, 1) == "第一行"


def test_replace_line_range():
    from app.services import text_patch as tp

    content = "第一行\n第二行\n第三行"
    assert tp.replace_line_range(content, 2, 2, "改后") == "第一行\n改后\n第三行"
    assert tp.replace_line_range(content, 2, 3, "A\nB") == "第一行\nA\nB"


def test_normalize_patch_accepts_aliases():
    """AI 输出字段名不稳定，别名兼容是必须的（参考实现也做了同样兼容）"""
    from app.services import text_patch as tp

    patch = tp.normalize_patch(
        {"oldText": "旧", "replacement": "新", "startLine": 3, "line_end": 4, "description": "因为"}
    )
    assert patch["old_text"] == "旧"
    assert patch["new_text"] == "新"
    assert patch["start_line"] == 3
    assert patch["end_line"] == 4
    assert patch["reason"] == "因为"


def test_normalize_patch_falls_back_when_section_id_is_placeholder():
    """AI 常把提示词里的占位符原样抄回来，此时必须回退到宿主已知的小节号"""
    from app.services import text_patch as tp

    assert tp.normalize_patch({"section_id": "当前小节编号"}, "1.2.3")["section_id"] == "1.2.3"
    assert tp.normalize_patch({}, "1.2.3")["section_id"] == "1.2.3"
    assert tp.normalize_patch({"sectionId": "2.1"}, "1.2.3")["section_id"] == "2.1"


@pytest.mark.parametrize(
    "payload",
    [
        [{"old_text": "a", "new_text": "b"}],
        {"patches": [{"old_text": "a", "new_text": "b"}]},
        {"operations": [{"old_text": "a", "new_text": "b"}]},
        {"old_text": "a", "new_text": "b"},
        {"result": {"patches": [{"old_text": "a", "new_text": "b"}]}},
    ],
)
def test_normalize_patches_unwraps_all_shapes(payload):
    from app.services import text_patch as tp

    patches = tp.normalize_patches(payload)
    assert len(patches) == 1
    assert patches[0]["old_text"] == "a"
    assert patches[0]["new_text"] == "b"


def test_normalize_patches_ignores_unrelated_payload():
    from app.services import text_patch as tp

    assert tp.normalize_patches(None) == []
    assert tp.normalize_patches("不是 JSON 结构") == []
    assert tp.normalize_patches({"foo": "bar"}) == []


def test_apply_exact_patch_unique_match():
    from app.services import text_patch as tp

    content = "本工程总工期为 30 天。\n第二段内容。"
    result = tp.apply_exact_patch(
        content, {"old_text": "本工程总工期为 30 天。", "new_text": "本工程总工期为 45 天。"}
    )
    assert result == "本工程总工期为 45 天。\n第二段内容。"


def test_apply_exact_patch_rejects_missing_old_text():
    from app.services import text_patch as tp

    with pytest.raises(tp.PatchApplyError, match="未在当前小节正文中找到"):
        tp.apply_exact_patch("正文", {"old_text": "不存在的句子", "new_text": "x"})


def test_apply_exact_patch_rejects_ambiguous_old_text():
    """多次命中必须拒绝——这是防止 AI 改错位置的核心闸门"""
    from app.services import text_patch as tp

    with pytest.raises(tp.PatchApplyError, match="出现 2 次"):
        tp.apply_exact_patch("30 天。\n30 天。", {"old_text": "30 天。", "new_text": "45 天。"})


@pytest.mark.parametrize(
    ("patch", "match"),
    [
        ({"old_text": "", "new_text": "x"}, "old_text 为空"),
        ({"old_text": "a", "new_text": ""}, "new_text 为空"),
        ({"old_text": "a", "new_text": "a"}, "相同"),
    ],
)
def test_apply_exact_patch_rejects_invalid_patch(patch, match):
    from app.services import text_patch as tp

    with pytest.raises(tp.PatchApplyError, match=match):
        tp.apply_exact_patch("a", patch)


def test_apply_exact_patch_prefers_line_range():
    """行号区间命中且逐字相等 → 即使 old_text 全文多次出现也能唯一定位"""
    from app.services import text_patch as tp

    content = "标题\n30 天。\n30 天。"
    result = tp.apply_exact_patch(
        content, {"start_line": 2, "end_line": 2, "old_text": "30 天。", "new_text": "45 天。"}
    )
    assert result == "标题\n45 天。\n30 天。"


def test_apply_exact_patch_falls_back_when_line_range_mismatches():
    """行号线索对不上就必须回退到精确匹配，不能盲信 AI 给的行号"""
    from app.services import text_patch as tp

    content = "标题\n唯一句子"
    result = tp.apply_exact_patch(
        content, {"start_line": 1, "end_line": 1, "old_text": "唯一句子", "new_text": "改后"}
    )
    assert result == "标题\n改后"


def test_apply_exact_patch_strips_echoed_line_numbers():
    """AI 常把提示词里的 `[0002] ` 行号抄进 old_text，必须自动剥掉"""
    from app.services import text_patch as tp

    content = "第一行\n第二行"
    result = tp.apply_exact_patch(content, {"old_text": "[0002] 第二行", "new_text": "改后"})
    assert result == "第一行\n改后"


def test_apply_exact_patch_normalizes_crlf():
    from app.services import text_patch as tp

    assert tp.apply_exact_patch("A\r\nB", {"old_text": "A\nB", "new_text": "X"}) == "X"


def test_apply_patches_isolates_failures():
    """逐条错误隔离：一条失败不能拖垮整批"""
    from app.services import text_patch as tp

    content = "甲句。乙句。"
    result = tp.apply_patches(
        content,
        [
            {"old_text": "甲句。", "new_text": "甲改。"},
            {"old_text": "不存在的句子", "new_text": "x"},
            {"old_text": "乙句。", "new_text": "乙改。"},
        ],
    )
    assert result["content"] == "甲改。乙改。"
    assert result["applied_count"] == 2
    assert result["rejected_count"] == 1
    assert result["changed"] is True
    assert len(result["errors"]) == 1
    assert [r["applied"] for r in result["results"]] == [True, False, True]


def test_apply_patches_reports_unchanged():
    from app.services import text_patch as tp

    result = tp.apply_patches("正文", [])
    assert result["changed"] is False
    assert result["applied_count"] == 0
    assert result["results"] == []


def test_format_content_with_line_numbers():
    from app.services import text_patch as tp

    assert tp.format_content_with_line_numbers("甲\n乙") == "[001] 甲\n[002] 乙"


# ---- J.2 字符区间编辑与数字边界保护 ----


@pytest.mark.parametrize(
    ("content", "value", "expected_count"),
    [
        ("共 300 个日历天", "30", 0),  # 后面紧跟数字 → 拒绝
        ("1300 元", "30", 0),  # 前面是数字 → 拒绝
        ("单价 3.30 元", "30", 0),  # 前是小数点 → 拒绝
        ("30 天后 30 天", "30", 2),
        ("工期 30 天", "30", 1),
    ],
)
def test_find_value_occurrences_digit_boundary(content, value, expected_count):
    """数字边界保护：本软件会把工期抽成 "30" 这种极短串，不加保护会改坏 "300"/"3.30" """
    from app.services import text_patch as tp

    assert len(tp.find_value_occurrences(content, value)) == expected_count


def test_find_value_occurrences_non_numeric_has_no_boundary_restriction():
    from app.services import text_patch as tp

    assert tp.find_value_occurrences("甲乙甲", "甲") == [0, 2]
    assert tp.find_value_occurrences("AB 与 AB", "AB") == [0, 5]


def test_replace_values_replaces_all():
    from app.services import text_patch as tp

    result = tp.replace_values("工期 45 天，另 45 天调试。", {"45": "30"})
    assert result["content"] == "工期 30 天，另 30 天调试。"
    assert len(result["applied"]) == 2


def test_replace_values_rejects_overlapping_ranges():
    """区间编辑靠下标定位，重叠必须整批拒绝（半吊子替换比不改更糟）"""
    from app.services import text_patch as tp

    result = tp.replace_values("此处 ABC 结束", {"AB": "Z", "ABC": "Z"})
    assert result["changed"] is False
    assert result["content"] == "此处 ABC 结束"
    assert result["errors"] and "重叠" in result["errors"][0]


def test_apply_range_edits_applies_in_reverse_order():
    """倒序应用：否则前面的编辑位移会让后面的下标错位"""
    from app.services import text_patch as tp

    result = tp.apply_range_edits("0123456789", [
        {"start": 0, "end": 2, "new_text": "AA"},
        {"start": 8, "end": 10, "new_text": "BB"},
    ])
    assert result["content"] == "AA234567BB"
    assert result["changed"] is True


@pytest.mark.parametrize(
    ("edit", "match"),
    [
        ({"start": "x", "end": 1, "new_text": "y"}, "必须是整数"),
        ({"start": 0, "end": 99, "new_text": "y"}, "越界"),
        ({"start": 2, "end": 1, "new_text": "y"}, "越界"),
        ({"start": 0, "end": 1, "new_text": "0"}, "没有变化"),
    ],
)
def test_apply_range_edits_rejects_bad_edit(edit, match):
    from app.services import text_patch as tp

    result = tp.apply_range_edits("0123", [edit])
    assert result["changed"] is False
    assert result["content"] == "0123"
    assert result["errors"] and match in result["errors"][0]


def test_text_metrics_detects_change():
    from app.services import text_patch as tp

    assert tp.text_metrics("abc")["chars"] == 3
    assert tp.text_metrics("abc")["hash"] == tp.text_metrics("abc")["hash"]
    assert tp.text_metrics("abc")["hash"] != tp.text_metrics("abd")["hash"]


# ---- J.3 apply_autofix 端到端：必须真的改写正文 ----


def _seed_consistency(project_id, sections, conflict=None, facts=None):
    """写入项目 / 小节 / 冲突 / 全局事实。sections 为 [(id, title, content)]。"""
    from app.db import get_db

    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO projects (id, name) VALUES (?, ?)", (project_id, f"P{project_id}")
        )
        for index, (section_id, title, content) in enumerate(sections, 1):
            conn.execute(
                "INSERT OR REPLACE INTO sections "
                "(id, project_id, title, content, status, sort_order) VALUES (?, ?, ?, ?, ?, ?)",
                (section_id, project_id, title, content, "completed", index),
            )
        if facts is not None:
            conn.execute(
                "INSERT INTO analysis (project_id, global_facts_json) VALUES (?, ?) "
                "ON CONFLICT(project_id) DO UPDATE SET global_facts_json = excluded.global_facts_json",
                (project_id, json.dumps(facts, ensure_ascii=False)),
            )
        if conflict:
            conn.execute(
                "INSERT OR REPLACE INTO consistency_conflicts "
                "(project_id, conflict_id, fact_name, values_json, sources_json, severity, "
                "description, created_at, resolved) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)",
                (
                    project_id,
                    conflict["id"],
                    conflict["name"],
                    json.dumps(conflict["values"], ensure_ascii=False),
                    "[]",
                    conflict.get("severity", "high"),
                    conflict.get("desc", ""),
                    "2026-09-14",
                ),
            )


def _section_content(section_id):
    from app.db import get_db

    with get_db() as conn:
        row = conn.execute("SELECT content FROM sections WHERE id = ?", (section_id,)).fetchone()
    return row["content"] if row else None


def _conflict_resolved(conflict_id):
    from app.db import get_db

    with get_db() as conn:
        row = conn.execute(
            "SELECT resolved, resolved_value FROM consistency_conflicts WHERE conflict_id = ?",
            (conflict_id,),
        ).fetchone()
    return (row["resolved"], row["resolved_value"]) if row else (None, None)


def test_apply_autofix_really_writes_content():
    """BUG-FIX 2026-09-14（高）回归：原实现只标 resolved、从不改写正文。

    后果是接口回「冲突已修复」，用户以为改了、实际一个字没动，
    而且冲突被标 resolved 后从待处理列表消失，问题被隐藏。
    """
    from app.services.consistency_auditor import consistency_auditor as ca

    _seed_consistency(
        990101,
        [(9901011, "施工部署", "本工程总工期为 30 天，共 300 个日历天。"), (9901012, "工期承诺", "承诺工期 45 天完成。")],
        conflict={"id": "cf_990101", "name": "工期", "values": ["30", "45"]},
    )

    report = ca.apply_autofix("cf_990101", 990101, "30")

    assert report["success"] is True
    assert report["total_replacements"] == 1
    assert report["changed_sections"] == 1
    # 真的落文了
    assert _section_content(9901012) == "承诺工期 30 天完成。"
    # 数字边界保护：300 里的 30 不能被误改
    assert _section_content(9901011) == "本工程总工期为 30 天，共 300 个日历天。"
    assert _conflict_resolved("cf_990101")[0] == 1


def test_apply_autofix_dry_run_does_not_write():
    from app.services.consistency_auditor import consistency_auditor as ca

    _seed_consistency(
        990102,
        [(9901021, "工期承诺", "承诺工期 45 天完成。")],
        conflict={"id": "cf_990102", "name": "工期", "values": ["30", "45"]},
    )

    report = ca.apply_autofix("cf_990102", 990102, "30", dry_run=True)

    assert report["success"] is True
    assert report["dry_run"] is True
    assert report["total_replacements"] == 1
    # 预览不得改正文、不得改冲突状态
    assert _section_content(9901021) == "承诺工期 45 天完成。"
    assert _conflict_resolved("cf_990102")[0] == 0


def test_apply_autofix_does_not_resolve_when_nothing_found():
    """未命中任何原文时绝不能标 resolved —— 那等于把问题藏起来"""
    from app.services.consistency_auditor import consistency_auditor as ca

    _seed_consistency(
        990103,
        [(9901031, "商务部分", "本工程报价合理。")],
        conflict={"id": "cf_990103", "name": "金额", "values": ["888", "999"]},
    )

    report = ca.apply_autofix("cf_990103", 990103, "888")

    assert report["success"] is False
    assert report["total_replacements"] == 0
    assert "未做任何修改" in report["message"]
    assert _conflict_resolved("cf_990103")[0] == 0
    assert _section_content(9901031) == "本工程报价合理。"


def test_apply_autofix_keeps_conflict_open_when_a_section_skipped():
    """有章节无法安全替换时，冲突保持未解决并说明原因，不谎报成功"""
    from app.services.consistency_auditor import consistency_auditor as ca

    _seed_consistency(
        990104,
        [
            (9901041, "可替换", "此处 AB 结束"),
            (9901042, "有歧义", "此处 ABC 结束"),
        ],
        conflict={"id": "cf_990104", "name": "术语", "values": ["AB", "ABC"]},
    )

    report = ca.apply_autofix("cf_990104", 990104, "Z")

    assert report["success"] is False
    assert report["changed_sections"] == 1
    assert len(report["skipped"]) == 1
    assert report["skipped"][0]["section_id"] == 9901042
    assert _section_content(9901041) == "此处 Z 结束"
    assert _section_content(9901042) == "此处 ABC 结束"
    assert _conflict_resolved("cf_990104")[0] == 0


def test_apply_autofix_missing_conflict():
    from app.services.consistency_auditor import consistency_auditor as ca

    report = ca.apply_autofix("cf_不存在", 990105, "x")
    assert report["success"] is False
    assert report["message"] == "未找到该冲突记录"


# ---- J.4 AI 补丁路径 ----


def test_plan_batches_splits_by_char_budget():
    from app.services.consistency_repair import plan_batches

    sections = [{"node_id": i, "content": "x" * 10} for i in range(5)]
    batches = plan_batches(sections, batch_chars=25)
    assert [len(b) for b in batches] == [2, 2, 1]
    # 单个小节超预算时自成一批，不切分正文（切分会破坏补丁定位）
    batches = plan_batches([{"node_id": 1, "content": "x" * 100}], batch_chars=10)
    assert len(batches) == 1 and len(batches[0]) == 1


def test_build_facts_text_accepts_both_shapes():
    from app.services.consistency_repair import build_facts_text

    text = build_facts_text([{"label": "总工期", "value": "90 天"}, {"key": "金额", "value": "100 万"}])
    assert "总工期：90 天" in text
    assert "金额：100 万" in text
    assert build_facts_text([]) == "未提供"


async def test_repair_with_ai_applies_patches_and_converges_conflicts(monkeypatch):
    """AI 补丁路径端到端：补丁落文 + 冲突按可验证判据收敛"""
    from app.services import consistency_repair as cr

    _seed_consistency(
        990201,
        [(9902011, "工期承诺", "承诺总工期 45 日历天。")],
        conflict={"id": "cf_990201", "name": "工期", "values": ["45", "90"]},
        facts=[{"label": "总工期", "value": "90 日历天"}],
    )

    async def _fake(_provider, _messages, **_kwargs):
        return {
            "patches": [
                {
                    "section_id": "9902011",
                    "old_text": "承诺总工期 45 日历天。",
                    "new_text": "承诺总工期 90 日历天。",
                    "reason": "与事实变量一致",
                }
            ],
            "unresolved": [],
        }

    monkeypatch.setattr(cr, "collect_json_response", _fake)

    report = await cr.repair_with_ai(990201, provider=object())

    assert report["success"] is True
    assert len(report["applied"]) == 1
    assert _section_content(9902011) == "承诺总工期 90 日历天。"
    # 正文里只剩一个取值 → 冲突按可验证判据收敛为已解决
    assert report["conflict_states"] == {"resolved": 1, "still_open": 0}
    assert _conflict_resolved("cf_990201")[0] == 1


async def test_repair_with_ai_rejects_hallucinated_old_text(monkeypatch):
    """AI 编造的 old_text 必须被拒——这是「不让 AI 直接重写正文」的核心收益"""
    from app.services import consistency_repair as cr

    _seed_consistency(
        990202,
        [(9902021, "工期承诺", "承诺总工期 45 日历天。")],
        conflict={"id": "cf_990202", "name": "工期", "values": ["45", "90"]},
        facts=[{"label": "总工期", "value": "90 日历天"}],
    )

    async def _fake(_provider, _messages, **_kwargs):
        return {"patches": [{"section_id": "9902021", "old_text": "正文里根本没有这句话", "new_text": "x"}]}

    monkeypatch.setattr(cr, "collect_json_response", _fake)

    report = await cr.repair_with_ai(990202, provider=object())

    assert report["success"] is False
    assert len(report["applied"]) == 0
    assert len(report["rejected"]) == 1
    assert "未在当前小节正文中找到" in report["rejected"][0]["error"]
    assert _section_content(9902021) == "承诺总工期 45 日历天。"
    assert _conflict_resolved("cf_990202")[0] == 0


async def test_repair_with_ai_drops_out_of_scope_section_id(monkeypatch):
    """AI 编造的小节编号必须丢弃，否则会改错小节"""
    from app.services import consistency_repair as cr

    _seed_consistency(
        990203,
        [(9902031, "工期承诺", "承诺总工期 45 日历天。")],
        conflict={"id": "cf_990203", "name": "工期", "values": ["45", "90"]},
        facts=[{"label": "总工期", "value": "90 日历天"}],
    )

    async def _fake(_provider, _messages, **_kwargs):
        return {
            "patches": [
                {"section_id": "999999", "old_text": "承诺总工期 45 日历天。", "new_text": "改错的小节"},
            ]
        }

    monkeypatch.setattr(cr, "collect_json_response", _fake)

    report = await cr.repair_with_ai(990203, provider=object())

    assert report["patches"] == []
    assert _section_content(9902031) == "承诺总工期 45 日历天。"


async def test_repair_with_ai_dry_run_does_not_write(monkeypatch):
    from app.services import consistency_repair as cr

    _seed_consistency(
        990204,
        [(9902041, "工期承诺", "承诺总工期 45 日历天。")],
        conflict={"id": "cf_990204", "name": "工期", "values": ["45", "90"]},
        facts=[{"label": "总工期", "value": "90 日历天"}],
    )

    async def _fake(_provider, _messages, **_kwargs):
        return {"patches": [{"section_id": "9902041", "old_text": "承诺总工期 45 日历天。", "new_text": "承诺总工期 90 日历天。"}]}

    monkeypatch.setattr(cr, "collect_json_response", _fake)

    report = await cr.repair_with_ai(990204, dry_run=True, provider=object())

    assert report["success"] is True
    assert report["dry_run"] is True
    assert _section_content(9902041) == "承诺总工期 45 日历天。"
    assert _conflict_resolved("cf_990204")[0] == 0


async def test_repair_with_ai_degrades_when_ai_fails(monkeypatch):
    """AI 不可用只降级，不抛异常——正文一个字不动"""
    from app.services import consistency_repair as cr

    _seed_consistency(
        990205,
        [(9902051, "工期承诺", "承诺总工期 45 日历天。")],
        conflict={"id": "cf_990205", "name": "工期", "values": ["45", "90"]},
        facts=[{"label": "总工期", "value": "90 日历天"}],
    )

    async def _boom(*_a, **_k):
        raise RuntimeError("AI 挂了")

    monkeypatch.setattr(cr, "collect_json_response", _boom)

    report = await cr.repair_with_ai(990205, provider=object())

    assert report["success"] is False
    assert report["errors"] and "AI 调用失败" in report["errors"][0]
    assert _section_content(9902051) == "承诺总工期 45 日历天。"


async def test_repair_with_ai_skips_ai_when_no_conflicts(monkeypatch):
    from app.services import consistency_repair as cr

    _seed_consistency(990206, [(9902061, "工期承诺", "内容")], facts=[{"label": "总工期", "value": "90"}])

    calls = {"n": 0}

    async def _fake(*_a, **_k):
        calls["n"] += 1
        return {"patches": []}

    monkeypatch.setattr(cr, "collect_json_response", _fake)

    report = await cr.repair_with_ai(990206, provider=object())

    assert calls["n"] == 0
    assert report["message"] == "没有未解决的一致性冲突，无需修复"


async def test_repair_with_ai_skips_ai_when_no_content(monkeypatch):
    from app.services import consistency_repair as cr

    _seed_consistency(
        990207,
        [(9902071, "空章节", "")],
        conflict={"id": "cf_990207", "name": "工期", "values": ["45", "90"]},
    )

    calls = {"n": 0}

    async def _fake(*_a, **_k):
        calls["n"] += 1
        return {"patches": []}

    monkeypatch.setattr(cr, "collect_json_response", _fake)

    report = await cr.repair_with_ai(990207, provider=object())

    assert calls["n"] == 0
    assert report["message"] == "没有可供修复的正文内容"


def test_refresh_conflict_states_keeps_open_when_two_values_remain():
    """冲突收敛必须按可验证判据：正文里还能找到 ≥2 个取值就不许标已解决"""
    from app.db import get_db
    from app.services.consistency_repair import refresh_conflict_states

    _seed_consistency(
        990208,
        [(9902081, "A", "工期 45 天"), (9902082, "B", "工期 90 天")],
        conflict={"id": "cf_990208", "name": "工期", "values": ["45", "90"]},
    )

    with get_db() as conn:
        states = refresh_conflict_states(conn, 990208, {9902081: "工期 45 天", 9902082: "工期 90 天"})

    assert states == {"resolved": 0, "still_open": 1}
    assert _conflict_resolved("cf_990208")[0] == 0


# ==================== K. 任务组互斥锁与任务生命周期 ====================
# ENHANCE 2026-09-15：移植自易标 taskService.cjs 的 lockPolicy:'group-exclusive'。
# 关键差异：参考实现是 Electron 单进程 + 内存 Map，本软件是 FastAPI + Celery 多进程，
# 所以锁下沉到 sqlite 行级唯一约束（task_locks 表 + 部分唯一索引）。

_LOCK_PID = itertools.count(971000)


def _next_pid() -> int:
    return next(_LOCK_PID)


def _seed_lock_project(project_id: int) -> None:
    from app.db import get_db

    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO projects (id, name) VALUES (?, ?)",
            (project_id, f"锁测试项目{project_id}"),
        )


@pytest.fixture
def lock_pid():
    """每个用例独占一个 project_id，并负责收尾清理。

    必须清理，否则：
    - 残留活跃锁会撞上 `uq_task_locks_active` 唯一索引，污染后续用例；
    - `generation_manager._persist_locks` 里的 asyncio.Lock 会跨事件循环复用
      （asyncio_mode=auto 下每个用例一个新 loop），导致 "bound to a different
      event loop" 报错；
    - **连接池配额**：本节的 async 用例每个都跑在独立 event loop 里，
      `generation_manager` 内部的 `loop.run_in_executor(...)` 会为该 loop 新建
      worker 线程，而 DBPool 是按线程分配连接的（上限 32），死线程的连接要闲置
      60 秒才回收。不主动释放的话，跑到第 30 多个用例就会触发
      "Database connection pool exhausted"，并把后续**无关模块**的用例一起拖挂
      （表现为 test_integration.py 里冒出 UnboundLocalError —— 那其实是
      db.py:267 连接池耗尽分支里 `from e` 的既有缺陷）。
      所以这里显式 close_all() 归还配额。
    """
    from app.db import get_db, get_pool
    from app.services.generation_manager import generation_manager

    pid = _next_pid()
    _seed_lock_project(pid)
    yield pid

    generation_manager._tasks.pop(pid, None)
    generation_manager._persist_locks.pop(pid, None)
    with get_db() as conn:
        conn.execute(
            "UPDATE task_locks SET status = 'released' WHERE project_id = ? AND status = 'active'",
            (pid,),
        )
    get_pool().close_all()


def _expire_lock(owner_id: str) -> None:
    """把某持有者的锁改成已超时（模拟持有者进程死亡）。"""
    from app.db import get_db

    with get_db() as conn:
        conn.execute(
            "UPDATE task_locks SET expires_at = datetime('now', 'localtime', '-1 hour') "
            "WHERE owner_id = ? AND status = 'active'",
            (owner_id,),
        )


def _running_task_count(project_id: int) -> int:
    from app.db import get_db

    with get_db(readonly=True) as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM generation_tasks WHERE project_id = ? AND status = 'running'",
            (project_id,),
        ).fetchone()
    return row["c"] if row else -1


# ---- K.1 锁键 / 分组 / 参数校验 ----


def test_lock_key_scoped_by_project():
    from app.services import task_lock

    assert task_lock.lock_key_for("technical-plan", 7) == "technical-plan:7"
    assert task_lock.lock_key_for("technical-plan", None) == "technical-plan:global"


def test_every_task_group_has_label():
    from app.services import task_lock

    for task_type, group in task_lock.TASK_GROUPS.items():
        assert group in task_lock.GROUP_LABELS, f"{task_type} 的组 {group} 缺标签"
        assert task_type in task_lock.TASK_LABELS, f"{task_type} 缺任务标签"


def test_content_and_outline_share_group():
    """参考实现里目录生成与正文生成同属 technical-plan 组，必须互斥。"""
    from app.services import task_lock

    assert task_lock.group_for_task("content-generation") == "technical-plan"
    assert task_lock.group_for_task("outline-generation") == "technical-plan"


def test_rejection_and_duplicate_are_separate_groups():
    from app.services import task_lock

    groups = {
        task_lock.group_for_task("rejection-check-run"),
        task_lock.group_for_task("duplicate-analysis"),
        task_lock.group_for_task("content-generation"),
    }
    assert len(groups) == 3


def test_group_for_unknown_task_is_none():
    from app.services import task_lock

    assert task_lock.group_for_task("not-a-real-task") is None


def test_acquire_ungrouped_task_is_noop(lock_pid):
    from app.services import task_lock

    result = task_lock.acquire("not-a-real-task", project_id=lock_pid)
    assert result["acquired"] is False
    assert result["reason"] == "ungrouped"
    assert result["lock_key"] == ""


@pytest.mark.parametrize("ttl", [0, -1, -600])
def test_acquire_rejects_non_positive_ttl(lock_pid, ttl):
    from app.services import task_lock

    with pytest.raises(ValueError, match="必须为正数"):
        task_lock.acquire("content-generation", project_id=lock_pid, ttl_seconds=ttl)


def test_acquire_returns_expected_fields(lock_pid):
    from app.services import task_lock

    record = task_lock.acquire("content-generation", project_id=lock_pid, note="单测")
    assert record["acquired"] is True
    assert record["lock_key"] == f"technical-plan:{lock_pid}"
    assert record["group_name"] == "technical-plan"
    assert record["task_type"] == "content-generation"
    assert record["owner_id"]
    assert record["expires_at"] > record["acquired_at"]


def test_make_owner_id_is_unique_and_tagged():
    from app.services import task_lock

    a = task_lock.make_owner_id("content-generation", 5)
    b = task_lock.make_owner_id("content-generation", 5)
    assert a != b
    assert a.startswith("content-generation:5:")


# ---- K.2 同组互斥 ----


def test_acquire_same_group_different_type_conflicts(lock_pid):
    from app.services import task_lock

    task_lock.acquire("content-generation", project_id=lock_pid)
    with pytest.raises(task_lock.TaskLockConflict) as exc:
        task_lock.acquire("outline-generation", project_id=lock_pid)
    assert exc.value.lock_key == f"technical-plan:{lock_pid}"
    assert exc.value.holder["task_type"] == "content-generation"


def test_conflict_message_uses_human_labels(lock_pid):
    from app.services import task_lock

    task_lock.acquire("content-generation", project_id=lock_pid)
    with pytest.raises(task_lock.TaskLockConflict) as exc:
        task_lock.acquire("outline-generation", project_id=lock_pid)
    message = str(exc.value)
    assert "技术方案" in message
    assert "正文生成" in message
    assert "目录生成" in message


def test_acquire_different_projects_do_not_conflict(lock_pid):
    from app.services import task_lock

    other = _next_pid()
    _seed_lock_project(other)
    try:
        task_lock.acquire("content-generation", project_id=lock_pid)
        record = task_lock.acquire("content-generation", project_id=other)
        assert record["acquired"] is True
        assert record["lock_key"] == f"technical-plan:{other}"
    finally:
        with __import__("app.db", fromlist=["get_db"]).get_db() as conn:
            conn.execute(
                "UPDATE task_locks SET status = 'released' WHERE project_id = ? AND status = 'active'",
                (other,),
            )


def test_acquire_same_owner_is_reentrant(lock_pid):
    from app.services import task_lock

    owner = "fixed-owner-1"
    first = task_lock.acquire("content-generation", project_id=lock_pid, owner_id=owner)
    second = task_lock.acquire("content-generation", project_id=lock_pid, owner_id=owner)
    assert second["acquired"] is True
    assert second.get("reentrant") is True
    assert second["owner_id"] == first["owner_id"]


def test_reentrant_acquire_can_be_disabled(lock_pid):
    from app.services import task_lock

    owner = "fixed-owner-2"
    task_lock.acquire("content-generation", project_id=lock_pid, owner_id=owner)
    with pytest.raises(task_lock.TaskLockConflict):
        task_lock.acquire(
            "content-generation", project_id=lock_pid, owner_id=owner, allow_same_owner=False
        )


def test_conflict_does_not_create_second_row(lock_pid):
    from app.db import get_db
    from app.services import task_lock

    task_lock.acquire("content-generation", project_id=lock_pid)
    with pytest.raises(task_lock.TaskLockConflict):
        task_lock.acquire("outline-generation", project_id=lock_pid)
    with get_db(readonly=True) as conn:
        total = conn.execute(
            "SELECT COUNT(*) AS c FROM task_locks WHERE project_id = ?", (lock_pid,)
        ).fetchone()["c"]
    assert total == 1


def test_different_groups_do_not_conflict(lock_pid):
    from app.services import task_lock

    task_lock.acquire("content-generation", project_id=lock_pid)
    record = task_lock.acquire("rejection-check-run", project_id=lock_pid)
    assert record["acquired"] is True
    assert record["group_name"] == "rejection-check"


# ---- K.3 心跳 ----


def test_heartbeat_extends_expiry(lock_pid):
    from app.services import task_lock

    record = task_lock.acquire("content-generation", project_id=lock_pid, ttl_seconds=60)
    before = record["expires_at"]
    assert task_lock.heartbeat(record["lock_key"], record["owner_id"], ttl_seconds=600) is True
    after = task_lock.get_active_lock("content-generation", lock_pid)
    assert after is not None
    assert after["expires_at"] > before


def test_heartbeat_wrong_owner_returns_false(lock_pid):
    from app.services import task_lock

    record = task_lock.acquire("content-generation", project_id=lock_pid)
    assert task_lock.heartbeat(record["lock_key"], "someone-else") is False


def test_heartbeat_after_release_returns_false(lock_pid):
    from app.services import task_lock

    record = task_lock.acquire("content-generation", project_id=lock_pid)
    task_lock.release(record["lock_key"], record["owner_id"])
    assert task_lock.heartbeat(record["lock_key"], record["owner_id"]) is False


@pytest.mark.parametrize(
    ("lock_key", "owner_id"),
    [("", "owner"), ("technical-plan:1", ""), ("", "")],
)
def test_heartbeat_empty_args_returns_false(lock_key, owner_id):
    from app.services import task_lock

    assert task_lock.heartbeat(lock_key, owner_id) is False


def test_heartbeat_rejects_non_positive_ttl(lock_pid):
    from app.services import task_lock

    record = task_lock.acquire("content-generation", project_id=lock_pid)
    with pytest.raises(ValueError, match="必须为正数"):
        task_lock.heartbeat(record["lock_key"], record["owner_id"], ttl_seconds=0)


# ---- K.4 释放（含 ABA 保护）----


def test_release_requires_owner_match(lock_pid):
    """ABA 保护：旧持有者不能释放新持有者的锁。"""
    from app.services import task_lock

    old = task_lock.acquire("content-generation", project_id=lock_pid)
    _expire_lock(old["owner_id"])
    task_lock.reclaim_stale()
    new = task_lock.acquire("content-generation", project_id=lock_pid)

    assert task_lock.release(new["lock_key"], old["owner_id"]) is False
    assert task_lock.get_active_lock("content-generation", lock_pid) is not None
    assert task_lock.release(new["lock_key"], new["owner_id"]) is True
    assert task_lock.get_active_lock("content-generation", lock_pid) is None


def test_release_unknown_key_returns_false():
    from app.services import task_lock

    assert task_lock.release("", "owner") is False
    assert task_lock.release("technical-plan:999999", "owner") is False


def test_release_then_reacquire(lock_pid):
    from app.services import task_lock

    first = task_lock.acquire("content-generation", project_id=lock_pid)
    assert task_lock.release(first["lock_key"], first["owner_id"]) is True
    second = task_lock.acquire("outline-generation", project_id=lock_pid)
    assert second["acquired"] is True


def test_release_is_idempotent(lock_pid):
    from app.services import task_lock

    record = task_lock.acquire("content-generation", project_id=lock_pid)
    assert task_lock.release(record["lock_key"], record["owner_id"]) is True
    assert task_lock.release(record["lock_key"], record["owner_id"]) is False


# ---- K.5 超时回收 ----


def test_reclaim_stale_marks_expired(lock_pid):
    from app.db import get_db
    from app.services import task_lock

    record = task_lock.acquire("content-generation", project_id=lock_pid)
    _expire_lock(record["owner_id"])
    assert task_lock.reclaim_stale() >= 1
    with get_db(readonly=True) as conn:
        row = conn.execute(
            "SELECT status, released_at FROM task_locks WHERE owner_id = ?", (record["owner_id"],)
        ).fetchone()
    assert row["status"] == "expired"
    assert row["released_at"]


def test_reclaim_stale_leaves_live_locks(lock_pid):
    from app.services import task_lock

    record = task_lock.acquire("content-generation", project_id=lock_pid, ttl_seconds=3600)
    task_lock.reclaim_stale()
    assert task_lock.get_active_lock("content-generation", lock_pid) is not None
    assert task_lock.is_lock_live(record["lock_key"]) is True


def test_acquire_reclaims_expired_lock_automatically(lock_pid):
    """持有者进程死亡后，不必等运维介入 —— 下一次 acquire 就应能拿到。"""
    from app.services import task_lock

    dead = task_lock.acquire("content-generation", project_id=lock_pid)
    _expire_lock(dead["owner_id"])
    fresh = task_lock.acquire("outline-generation", project_id=lock_pid)
    assert fresh["acquired"] is True
    assert fresh["owner_id"] != dead["owner_id"]


def test_reclaim_stale_returns_zero_when_nothing_to_do():
    from app.services import task_lock

    assert task_lock.reclaim_stale() == 0


def test_is_lock_live_true_and_false(lock_pid):
    from app.services import task_lock

    record = task_lock.acquire("content-generation", project_id=lock_pid, ttl_seconds=600)
    assert task_lock.is_lock_live(record["lock_key"]) is True
    _expire_lock(record["owner_id"])
    assert task_lock.is_lock_live(record["lock_key"]) is False


def test_is_lock_live_empty_key_is_false():
    from app.services import task_lock

    assert task_lock.is_lock_live("") is False


def test_read_lock_state_free(lock_pid):
    from app.db import get_db
    from app.services import task_lock

    with get_db(readonly=True) as conn:
        state = task_lock.read_lock_state(conn, "content-generation", lock_pid)
    assert state == {"held": False, "live": False, "holder": None}


def test_read_lock_state_held_and_live(lock_pid):
    from app.db import get_db
    from app.services import task_lock

    task_lock.acquire("content-generation", project_id=lock_pid, ttl_seconds=600)
    with get_db(readonly=True) as conn:
        state = task_lock.read_lock_state(conn, "content-generation", lock_pid)
    assert state["held"] is True
    assert state["live"] is True
    assert state["holder"]["group_name"] == "technical-plan"


def test_read_lock_state_held_but_expired(lock_pid):
    from app.db import get_db
    from app.services import task_lock

    record = task_lock.acquire("content-generation", project_id=lock_pid)
    _expire_lock(record["owner_id"])
    with get_db(readonly=True) as conn:
        state = task_lock.read_lock_state(conn, "content-generation", lock_pid)
    assert state["held"] is True
    assert state["live"] is False


def test_read_lock_state_ungrouped(lock_pid):
    from app.db import get_db
    from app.services import task_lock

    with get_db(readonly=True) as conn:
        state = task_lock.read_lock_state(conn, "not-a-real-task", lock_pid)
    assert state["held"] is False


# ---- K.6 assert_group_available ----


def test_assert_group_available_passes_when_free(lock_pid):
    from app.services import task_lock

    task_lock.assert_group_available("content-generation", project_id=lock_pid)


def test_assert_group_available_raises_when_held(lock_pid):
    from app.services import task_lock

    task_lock.acquire("content-generation", project_id=lock_pid)
    with pytest.raises(task_lock.TaskLockConflict):
        task_lock.assert_group_available("outline-generation", project_id=lock_pid)


def test_assert_group_available_passes_when_expired(lock_pid):
    from app.services import task_lock

    record = task_lock.acquire("content-generation", project_id=lock_pid)
    _expire_lock(record["owner_id"])
    task_lock.assert_group_available("outline-generation", project_id=lock_pid)


def test_assert_group_available_ungrouped_passes(lock_pid):
    from app.services import task_lock

    task_lock.acquire("content-generation", project_id=lock_pid)
    task_lock.assert_group_available("not-a-real-task", project_id=lock_pid)


# ---- K.7 锁记录查询 ----


def test_get_active_lock_free_returns_none(lock_pid):
    from app.services import task_lock

    assert task_lock.get_active_lock("content-generation", lock_pid) is None


def test_get_active_lock_ungrouped_returns_none(lock_pid):
    from app.services import task_lock

    assert task_lock.get_active_lock("not-a-real-task", lock_pid) is None


def test_list_locks_active_only(lock_pid):
    from app.services import task_lock

    record = task_lock.acquire("content-generation", project_id=lock_pid)
    active = [lk for lk in task_lock.list_locks(active_only=True) if lk["project_id"] == lock_pid]
    assert len(active) == 1
    task_lock.release(record["lock_key"], record["owner_id"])
    active_after = [
        lk for lk in task_lock.list_locks(active_only=True) if lk["project_id"] == lock_pid
    ]
    assert active_after == []


def test_list_locks_includes_history_when_not_active_only(lock_pid):
    from app.services import task_lock

    record = task_lock.acquire("content-generation", project_id=lock_pid)
    task_lock.release(record["lock_key"], record["owner_id"])
    history = [
        lk for lk in task_lock.list_locks(active_only=False) if lk["project_id"] == lock_pid
    ]
    assert len(history) >= 1
    assert {lk["status"] for lk in history} == {"released"}


# ---- K.8 GenerationManager 接入 ----


async def test_create_task_acquires_group_lock(lock_pid):
    from app.services import task_lock
    from app.services.generation_manager import generation_manager

    generation_id = await generation_manager.create_task(lock_pid, "hashA", {"writing_length": "short"})
    assert generation_id
    record = task_lock.get_active_lock("content-generation", lock_pid)
    assert record is not None
    task = generation_manager._tasks[lock_pid]
    assert task.lock_key == record["lock_key"]
    assert task.lock_owner == record["owner_id"]


async def test_create_task_blocks_second_concurrent(lock_pid):
    from app.services import task_lock
    from app.services.generation_manager import generation_manager

    await generation_manager.create_task(lock_pid, "hashA", {"writing_length": "short"})
    with pytest.raises(task_lock.TaskLockConflict):
        await generation_manager.create_task(lock_pid, "hashB", {"writing_length": "short"})


async def test_create_task_block_leaves_no_extra_row(lock_pid):
    """被拦的第二次调用不能在 generation_tasks 留下第二行 running。"""
    from app.services import task_lock
    from app.services.generation_manager import generation_manager

    await generation_manager.create_task(lock_pid, "hashA", {"writing_length": "short"})
    with pytest.raises(task_lock.TaskLockConflict):
        await generation_manager.create_task(lock_pid, "hashB", {"writing_length": "short"})
    assert _running_task_count(lock_pid) == 1


async def test_finalize_task_releases_lock(lock_pid):
    from app.services import task_lock
    from app.services.generation_manager import generation_manager

    await generation_manager.create_task(lock_pid, "hashA", {"writing_length": "short"})
    assert task_lock.get_active_lock("content-generation", lock_pid) is not None
    await generation_manager.finalize_task(lock_pid, task_status="completed")
    assert task_lock.get_active_lock("content-generation", lock_pid) is None


async def test_create_task_after_finalize_works(lock_pid):
    from app.services.generation_manager import generation_manager

    first = await generation_manager.create_task(lock_pid, "hashA", {"writing_length": "short"})
    await generation_manager.finalize_task(lock_pid, task_status="completed")
    second = await generation_manager.create_task(lock_pid, "hashB", {"writing_length": "short"})
    assert second != first


async def test_cancelled_task_also_releases_lock(lock_pid):
    from app.services import task_lock
    from app.services.generation_manager import generation_manager

    await generation_manager.create_task(lock_pid, "hashA", {"writing_length": "short"})
    await generation_manager.finalize_task(lock_pid, task_status="cancelled")
    assert task_lock.get_active_lock("content-generation", lock_pid) is None


async def test_heartbeat_throttled_by_interval(lock_pid):
    """章节进度更新会触发续约，但同一间隔内只写一次锁表。"""
    from app.services.generation_manager import generation_manager

    await generation_manager.create_task(lock_pid, "hashA", {"writing_length": "short"})
    task = generation_manager._tasks[lock_pid]
    task.total_sections = 2
    task.last_heartbeat_at = 0.0  # 强制第一次续约
    await generation_manager.update_section_progress(lock_pid, 1, "completed", 100, content="x")
    stamped = task.last_heartbeat_at
    assert stamped > 0.0
    await generation_manager.update_section_progress(lock_pid, 2, "completed", 100, content="y")
    assert task.last_heartbeat_at == stamped


async def test_progress_marks_owned_elsewhere_when_foreign_lock(lock_pid):
    """多进程正确性：别的进程持锁时，本进程不得把任务判死。

    这正是原判据（「DB 有 running 行但本进程内存无实例 → 标记 failed」）的误杀场景。
    """
    from app.db import get_db
    from app.services import task_lock
    from app.services.generation_manager import generation_manager

    with get_db() as conn:
        conn.execute(
            "INSERT INTO generation_tasks (project_id, generation_id, status, total_sections) "
            "VALUES (?, 'foreign-gen', 'running', 3)",
            (lock_pid,),
        )
    task_lock.acquire(
        "content-generation", project_id=lock_pid, owner_id="other-process:abc", ttl_seconds=3600
    )
    generation_manager._tasks.pop(lock_pid, None)

    progress = await generation_manager.get_project_progress(lock_pid)
    assert progress["owned_elsewhere"] is True
    assert progress["has_active_task"] is True
    assert "auto_corrected" not in progress

    with get_db(readonly=True) as conn:
        status = conn.execute(
            "SELECT status FROM generation_tasks WHERE project_id = ? AND generation_id = 'foreign-gen'",
            (lock_pid,),
        ).fetchone()["status"]
    assert status == "running", "别的进程正在跑的任务被本进程判死了"


async def test_progress_corrects_when_lock_expired(lock_pid):
    """锁过期后同样的场景应恢复原有残留修正行为（判死）。"""
    from app.db import get_db
    from app.services import task_lock
    from app.services.generation_manager import generation_manager

    with get_db() as conn:
        conn.execute(
            "INSERT INTO generation_tasks (project_id, generation_id, status, total_sections) "
            "VALUES (?, 'orphan-gen', 'running', 3)",
            (lock_pid,),
        )
    dead = task_lock.acquire("content-generation", project_id=lock_pid, owner_id="dead-process:zzz")
    _expire_lock(dead["owner_id"])
    generation_manager._tasks.pop(lock_pid, None)

    progress = await generation_manager.get_project_progress(lock_pid)
    assert progress["auto_corrected"] is True
    assert progress["has_active_task"] is False
    assert progress["task_status"] == "failed"


async def test_progress_owned_elsewhere_false_without_lock(lock_pid):
    from app.db import get_db
    from app.services.generation_manager import generation_manager

    with get_db() as conn:
        conn.execute(
            "INSERT INTO generation_tasks (project_id, generation_id, status, total_sections) "
            "VALUES (?, 'orphan2-gen', 'running', 3)",
            (lock_pid,),
        )
    generation_manager._tasks.pop(lock_pid, None)
    progress = await generation_manager.get_project_progress(lock_pid)
    assert progress.get("owned_elsewhere") is False
    assert progress["auto_corrected"] is True


# ---- K.9 rollback_dirty_sections（BUG-FIX 2026-09-15）----


async def test_rollback_resets_generating_sections(lock_pid):
    """原实现写入不存在的 sections.error_message 列 → 整事务回滚 → 脏状态从没被清过。"""
    from app.db import get_db
    from app.services.generation_manager import generation_manager

    with get_db() as conn:
        conn.execute(
            "INSERT INTO sections (project_id, title, level, status) VALUES (?, '脏', 1, 'generating')",
            (lock_pid,),
        )
    await generation_manager.rollback_dirty_sections(lock_pid)
    with get_db(readonly=True) as conn:
        status = conn.execute(
            "SELECT status FROM sections WHERE project_id = ?", (lock_pid,)
        ).fetchone()["status"]
    assert status == "failed"


async def test_rollback_resets_processing_by_generation_id(lock_pid):
    from app.db import get_db
    from app.services.generation_manager import generation_manager

    with get_db() as conn:
        conn.execute(
            "INSERT INTO section_generation_progress (generation_id, section_id, status) "
            "VALUES ('gen-x', 555001, 'processing')"
        )
    await generation_manager.rollback_dirty_sections(lock_pid, "gen-x")
    with get_db(readonly=True) as conn:
        status = conn.execute(
            "SELECT status FROM section_generation_progress WHERE generation_id = 'gen-x'"
        ).fetchone()["status"]
    assert status == "failed"


async def test_rollback_none_only_touches_active_generations(lock_pid):
    """generation_id=None 时只清理 running/paused 任务的章节，不动已完成任务。"""
    from app.db import get_db
    from app.services.generation_manager import generation_manager

    with get_db() as conn:
        conn.execute(
            "INSERT INTO generation_tasks (project_id, generation_id, status) VALUES (?, 'live-g', 'running')",
            (lock_pid,),
        )
        conn.execute(
            "INSERT INTO generation_tasks (project_id, generation_id, status) VALUES (?, 'done-g', 'completed')",
            (lock_pid,),
        )
        conn.execute(
            "INSERT INTO section_generation_progress (generation_id, section_id, status) "
            "VALUES ('live-g', 555002, 'processing')"
        )
        conn.execute(
            "INSERT INTO section_generation_progress (generation_id, section_id, status) "
            "VALUES ('done-g', 555003, 'processing')"
        )
    await generation_manager.rollback_dirty_sections(lock_pid)
    with get_db(readonly=True) as conn:
        live = conn.execute(
            "SELECT status FROM section_generation_progress WHERE generation_id = 'live-g'"
        ).fetchone()["status"]
        done = conn.execute(
            "SELECT status FROM section_generation_progress WHERE generation_id = 'done-g'"
        ).fetchone()["status"]
    assert live == "failed"
    assert done == "processing", "已完成任务的章节不该被回滚"


async def test_rollback_leaves_completed_sections_alone(lock_pid):
    from app.db import get_db
    from app.services.generation_manager import generation_manager

    with get_db() as conn:
        conn.execute(
            "INSERT INTO sections (project_id, title, level, status) VALUES (?, '好', 1, 'completed')",
            (lock_pid,),
        )
    await generation_manager.rollback_dirty_sections(lock_pid)
    with get_db(readonly=True) as conn:
        status = conn.execute(
            "SELECT status FROM sections WHERE project_id = ?", (lock_pid,)
        ).fetchone()["status"]
    assert status == "completed"


# ---- K.10 schema 归属（不要写回启动钩子）----


def test_task_locks_created_by_init_db():
    """锁表必须由 db.init_db 建，否则自定义数据目录下会 no such table。"""
    from app.db import get_db

    with get_db(readonly=True) as conn:
        table = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'task_locks'"
        ).fetchone()
        index = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND name = 'uq_task_locks_active'"
        ).fetchone()
    assert table is not None
    assert index is not None, "缺少活跃锁唯一索引，互斥就退化成先查后插"


_LOCK_INSERT = (
    "INSERT INTO task_locks (lock_key, group_name, task_type, project_id, owner_id, "
    "status, acquired_at, heartbeat_at, expires_at) "
    "VALUES (?, 'technical-plan', ?, ?, ?, 'active', "
    "'2026-09-15 00:00:00', '2026-09-15 00:00:00', '2036-09-15 00:00:00')"
)


def test_active_lock_unique_index_enforced():
    """同 key 的两行活跃锁必须被数据库拒绝（互斥的真正保证）。

    刻意用**裸 sqlite3 连接**而不是 get_db()：get_db 的 except 分支只清
    `_local.connection`、不从 `_all_connections` 摘除连接（db.py 第 50 行注释
    已记录该已知问题），因此每次「在 get_db 里触发异常」都会泄漏一个连接池
    槽位（max=10），跑几次就把后续用例连带拖死成 "pool exhausted"。
    测约束本身不需要经过应用连接池。
    """
    import sqlite3

    from app.config import DATABASE_PATH
    from app.db import get_db

    pid = _next_pid()
    _seed_lock_project(pid)
    key = f"technical-plan:{pid}"
    conn = sqlite3.connect(str(DATABASE_PATH), timeout=10)
    try:
        conn.execute(_LOCK_INSERT, (key, "content-generation", pid, "o1"))
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(_LOCK_INSERT, (key, "outline-generation", pid, "o2"))
        conn.rollback()
    finally:
        conn.execute("DELETE FROM task_locks WHERE lock_key = ?", (key,))
        conn.commit()
        conn.close()

    with get_db(readonly=True) as check:
        left = check.execute(
            "SELECT COUNT(*) AS c FROM task_locks WHERE lock_key = ?", (key,)
        ).fetchone()["c"]
    assert left == 0

# ==================== L. 后台任务持久化与中断对账（Task 9）====================
# ENHANCE 2026-09-15：移植自易标 taskService.cjs 的
# `recoverInterruptedOutlineGenerationTask`（第 1002-1093 行）。
# 参考实现的核心不是「断点续跑」而是**启动时的诚实对账**：把上次异常退出留下的
# 活跃任务明确收敛成终态，别让用户面对一个永远「运行中」的幽灵任务。
# 本软件对应实现：background_tasks 表（运行期持久化 + 心跳）+ 启动对账
# + GET /tasks/status 的 recovery_options。

_SNAP_PID = itertools.count(972000)


def _next_snap_pid() -> int:
    return next(_SNAP_PID)


@pytest.fixture
def snap_pid():
    """每个用例独占一个 project_id，并负责收尾清理。

    必须清理 `background_tasks` / `v51_tasks`，否则：
    - 残留的 running 行会撞上 `uq_background_tasks_running` 唯一索引，
      让后续用例的 register 抛 TaskSnapshotConflict；
    - 残留的 v51_tasks 行会让 load_v51_state_from_db 返回上一轮的阶段，
      污染「活跃状态保护」相关断言。
    """
    from app.db import get_db, get_pool

    pid = _next_snap_pid()
    _seed_lock_project(pid)
    yield pid

    with get_db() as conn:
        conn.execute("DELETE FROM background_tasks WHERE project_id = ?", (pid,))
        conn.execute("DELETE FROM v51_tasks WHERE project_id = ?", (pid,))
    from app.services import v51_state as vs

    vs.remove_v51_state(pid)
    get_pool().close_all()


def _age_snapshot(task_id: str, seconds: int | None = None) -> None:
    """把快照心跳改成 N 秒之前，模拟持有者进程已死。

    默认取「超时阈值 + 120 秒」，留出余量避免边界抖动。
    """
    from app.db import get_db
    from app.services import task_snapshot as ts

    if seconds is None:
        seconds = ts.DEFAULT_STALE_SECONDS + 120
    with get_db() as conn:
        conn.execute(
            "UPDATE background_tasks SET heartbeat_at = datetime('now', 'localtime', ?) "
            "WHERE task_id = ?",
            (f"-{seconds} seconds", task_id),
        )


def _snap_row(task_id: str) -> dict:
    from app.db import get_db

    with get_db(readonly=True) as conn:
        row = conn.execute(
            "SELECT * FROM background_tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
    return dict(row) if row else {}


def _insert_v51_row(project_id: int, phase: str = "level1") -> None:
    """直接写一行 v51_tasks（模拟「别处持久化过的旧阶段」）。"""
    from app.db import get_db

    with get_db() as conn:
        conn.execute("DELETE FROM v51_tasks WHERE project_id = ?", (project_id,))
        conn.execute(
            "INSERT INTO v51_tasks (project_id, phase, current_phase, level1_outline_json, "
            "cancelled, expired_at) VALUES (?, ?, ?, '[]', 0, '2036-01-01 00:00:00')",
            (project_id, phase, phase),
        )


def _read_backend_source(rel_path: str) -> str:
    from pathlib import Path

    return (
        Path(__file__).resolve().parent.parent / rel_path
    ).read_text(encoding="utf-8")


# ---- L.1 schema / 常量 ----


def test_background_tasks_created_by_init_db():
    """快照表必须由 db.init_db 建，否则自定义数据目录下会 no such table。"""
    from app.db import get_db

    with get_db(readonly=True) as conn:
        table = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'background_tasks'"
        ).fetchone()
        index = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' "
            "AND name = 'uq_background_tasks_running'"
        ).fetchone()
    assert table is not None
    assert index is not None, "缺少运行中唯一索引，同键并发登记就退化成先查后插"


_BG_INSERT = (
    "INSERT INTO background_tasks (task_id, project_id, task_type, status, phase, "
    "progress, current_step, total_steps, message, owner_id, payload_json, "
    "started_at, heartbeat_at, updated_at, acknowledged) "
    "VALUES (?, ?, ?, 'running', '', 0, 0, 0, '', ?, '{}', "
    "'2026-09-15 00:00:00', '2026-09-15 00:00:00', '2026-09-15 00:00:00', 0)"
)


def test_running_unique_index_enforced():
    """同 (project, task_type) 的第二行 running 必须被数据库拒绝。

    刻意用**裸 sqlite3 连接**：get_db 的 except 分支只清 `_local.connection`、
    不从 `_all_connections` 摘除，每次在 get_db 里触发异常都会泄漏一个连接池槽位。
    测约束本身不需要经过应用连接池。
    """
    import sqlite3

    from app.config import DATABASE_PATH
    from app.db import get_db

    pid = _next_snap_pid()
    _seed_lock_project(pid)
    conn = sqlite3.connect(str(DATABASE_PATH), timeout=10)
    try:
        conn.execute(_BG_INSERT, ("t1", pid, "outline_v51", "o1"))
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(_BG_INSERT, ("t2", pid, "outline_v51", "o2"))
        conn.rollback()
    finally:
        conn.execute("DELETE FROM background_tasks WHERE project_id = ?", (pid,))
        conn.commit()
        conn.close()


def test_terminal_rows_can_coexist(snap_pid):
    """唯一索引只约束 running 行：多行 interrupted 必须能共存（审计轨迹）。"""
    from app.db import get_db

    with get_db() as conn:
        for i in range(3):
            conn.execute(
                "INSERT INTO background_tasks (task_id, project_id, task_type, status, "
                "phase, progress, current_step, total_steps, message, owner_id, payload_json, "
                "started_at, heartbeat_at, updated_at, acknowledged) "
                "VALUES (?, ?, 'outline_v51', 'interrupted', 'x', 0, 0, 0, '', '', '{}', "
                "'2026-09-15 00:00:00', '2026-09-15 00:00:00', '2026-09-15 00:00:00', 0)",
                (f"hist-{i}", snap_pid),
            )
        count = conn.execute(
            "SELECT COUNT(*) AS c FROM background_tasks WHERE project_id = ?", (snap_pid,)
        ).fetchone()["c"]
    assert count == 3


def test_task_type_labels_cover_known_types():
    from app.services import task_snapshot as ts

    assert ts.TASK_TYPE_LABELS["outline_v51"] == "目录生成"
    assert ts.TASK_TYPE_LABELS["content_generation"] == "正文生成"


# ---- L.2 register ----


def test_register_returns_running_row(snap_pid):
    from app.services import task_snapshot as ts

    row = ts.register(snap_pid, "outline_v51", total_steps=8, phase="准备", owner_id="o1")
    assert row["registered"] is True
    assert row["status"] == ts.STATUS_RUNNING
    assert row["project_id"] == snap_pid
    assert row["total_steps"] == 8
    assert row["phase"] == "准备"
    assert row["acknowledged"] == 0


def test_register_conflicts_for_other_owner(snap_pid):
    from app.services import task_snapshot as ts

    first = ts.register(snap_pid, "outline_v51", owner_id="o1")
    with pytest.raises(ts.TaskSnapshotConflict) as exc:
        ts.register(snap_pid, "outline_v51", owner_id="o2")
    assert (exc.value.existing or {}).get("task_id") == first["task_id"]


def test_register_reentrant_for_same_owner(snap_pid):
    from app.services import task_snapshot as ts

    first = ts.register(snap_pid, "outline_v51", owner_id="o1")
    again = ts.register(snap_pid, "outline_v51", owner_id="o1", message="重入")
    assert again["reentrant"] is True
    assert again["task_id"] == first["task_id"]
    assert again["message"] == "重入"


def test_register_isolated_per_project(snap_pid):
    from app.db import get_db
    from app.services import task_snapshot as ts

    other_pid = snap_pid + 100000
    _seed_lock_project(other_pid)
    try:
        ts.register(snap_pid, "outline_v51", owner_id="o1")
        other = ts.register(other_pid, "outline_v51", owner_id="o2")
        assert other["status"] == ts.STATUS_RUNNING
        assert other["project_id"] == other_pid
    finally:
        with get_db() as conn:
            conn.execute("DELETE FROM background_tasks WHERE project_id = ?", (other_pid,))


def test_register_reconciles_stale_running_row(snap_pid):
    """僵尸 running 行必须先被收敛，否则唯一索引会把新任务挡在门外。

    收敛后立刻被 `_supersede_previous` 标成 superseded —— 语义是「新一轮已经
    开始了，上一轮的中断提示不必再挂在横幅上」。`interrupted_reason` 仍留在行里，
    审计轨迹不丢。
    """
    from app.services import task_snapshot as ts

    old = ts.register(snap_pid, "outline_v51", owner_id="dead")
    _age_snapshot(old["task_id"])
    new = ts.register(snap_pid, "outline_v51", owner_id="alive")
    assert new["task_id"] != old["task_id"]
    assert _snap_row(old["task_id"])["status"] == ts.STATUS_SUPERSEDED
    assert "心跳停止" in _snap_row(old["task_id"])["interrupted_reason"]
    assert _snap_row(new["task_id"])["status"] == ts.STATUS_RUNNING


def test_register_supersedes_previous_interrupted(snap_pid):
    """开了新一轮，旧的中断提示就不该再挂在横幅上。"""
    from app.services import task_snapshot as ts

    old = ts.register(snap_pid, "outline_v51", owner_id="dead")
    _age_snapshot(old["task_id"])
    ts.reconcile_stale()
    assert len(ts.list_interruptions(snap_pid)) == 1
    ts.register(snap_pid, "outline_v51", owner_id="alive")
    assert ts.list_interruptions(snap_pid) == []


# ---- L.3 心跳 / 进度 ----


def test_update_progress_writes_phase_and_refreshes_heartbeat(snap_pid):
    from app.services import task_snapshot as ts

    row = ts.register(snap_pid, "outline_v51", owner_id="o1")
    # 默认回拨「阈值 + 120 秒」，确保真的越过 DEFAULT_STALE_SECONDS 判定线
    _age_snapshot(row["task_id"])
    assert ts.get_by_task_id(row["task_id"])["stale"] is True
    ok = ts.update_progress(
        row["task_id"], phase="二级目录生成", progress=45.5, current_step=4, message="填充中"
    )
    assert ok is True
    fresh = ts.get_by_task_id(row["task_id"])
    assert fresh["phase"] == "二级目录生成"
    assert abs(float(fresh["progress"]) - 45.5) < 0.01
    assert fresh["current_step"] == 4
    assert fresh["stale"] is False, "update_progress 必须顺带刷新心跳"


def test_heartbeat_only_affects_running(snap_pid):
    from app.services import task_snapshot as ts

    row = ts.register(snap_pid, "outline_v51", owner_id="o1")
    ts.finish(row["task_id"], ts.STATUS_COMPLETED)
    assert ts.heartbeat(row["task_id"]) is False


def test_heartbeat_unknown_task_id_returns_false():
    from app.services import task_snapshot as ts

    assert ts.heartbeat("no-such-task") is False
    assert ts.heartbeat("") is False


def test_update_progress_after_finish_returns_false(snap_pid):
    from app.services import task_snapshot as ts

    row = ts.register(snap_pid, "outline_v51", owner_id="o1")
    ts.finish(row["task_id"], ts.STATUS_COMPLETED)
    assert ts.update_progress(row["task_id"], phase="x") is False


# ---- L.4 终态 ----


def test_finish_sets_terminal_status(snap_pid):
    from app.services import task_snapshot as ts

    row = ts.register(snap_pid, "outline_v51", owner_id="o1")
    assert ts.finish(row["task_id"], ts.STATUS_COMPLETED) is True
    saved = _snap_row(row["task_id"])
    assert saved["status"] == ts.STATUS_COMPLETED
    assert saved["finished_at"]


def test_finish_does_not_overwrite_terminal_row(snap_pid):
    """已经是终态的行不能被二次收尾覆盖，否则审计轨迹就废了。"""
    from app.services import task_snapshot as ts

    row = ts.register(snap_pid, "outline_v51", owner_id="o1")
    ts.finish(row["task_id"], ts.STATUS_COMPLETED)
    assert ts.finish(row["task_id"], ts.STATUS_FAILED) is False
    assert _snap_row(row["task_id"])["status"] == ts.STATUS_COMPLETED


def test_finish_rejects_invalid_status(snap_pid):
    from app.services import task_snapshot as ts

    row = ts.register(snap_pid, "outline_v51", owner_id="o1")
    with pytest.raises(ValueError):
        ts.finish(row["task_id"], "bogus")


def test_finished_row_leaves_list_running(snap_pid):
    from app.services import task_snapshot as ts

    row = ts.register(snap_pid, "outline_v51", owner_id="o1")
    assert len(ts.list_running(snap_pid)) == 1
    ts.finish(row["task_id"], ts.STATUS_CANCELLED)
    assert ts.list_running(snap_pid) == []


# ---- L.5 只读中断查询 ----


def test_list_interruptions_reports_stale_running_without_writing(snap_pid):
    """轮询接口用的查询必须**纯读**：用户刷页面不能改库。"""
    from app.services import task_snapshot as ts

    row = ts.register(snap_pid, "outline_v51", phase="二级目录生成", progress=45.0, owner_id="o1")
    _age_snapshot(row["task_id"])
    items = ts.list_interruptions(snap_pid)
    assert len(items) == 1
    assert items[0]["effective_status"] == ts.STATUS_INTERRUPTED
    assert items[0]["stale"] is True
    assert _snap_row(row["task_id"])["status"] == ts.STATUS_RUNNING, "只读查询写库了"


def test_list_interruptions_skips_healthy_running(snap_pid):
    from app.services import task_snapshot as ts

    ts.register(snap_pid, "outline_v51", owner_id="o1")
    assert ts.list_interruptions(snap_pid) == []


def test_list_interruptions_skips_acknowledged(snap_pid):
    from app.services import task_snapshot as ts

    row = ts.register(snap_pid, "outline_v51", owner_id="o1")
    _age_snapshot(row["task_id"])
    ts.reconcile_stale()
    assert len(ts.list_interruptions(snap_pid)) == 1
    ts.acknowledge(snap_pid)
    assert ts.list_interruptions(snap_pid) == []


def test_list_interruptions_returns_latest_per_key(snap_pid):
    """每个 (project, task_type) 只暴露最新一条，避免前端列表里出现一堆同类条目。"""
    from app.services import task_snapshot as ts

    for i in range(3):
        row = ts.register(snap_pid, "outline_v51", phase=f"阶段{i}", owner_id=f"o{i}")
        _age_snapshot(row["task_id"])
        ts.reconcile_stale()
    items = ts.list_interruptions(snap_pid)
    assert len(items) == 1
    assert items[0]["phase"] == "阶段2"


def test_list_interruptions_filters_by_task_type(snap_pid):
    from app.services import task_snapshot as ts

    a = ts.register(snap_pid, "outline_v51", owner_id="o1")
    _age_snapshot(a["task_id"])
    ts.reconcile_stale()
    b = ts.register(snap_pid, "content_generation", owner_id="o2")
    _age_snapshot(b["task_id"])
    ts.reconcile_stale()
    assert len(ts.list_interruptions(snap_pid)) == 2
    assert len(ts.list_interruptions(snap_pid, task_type="outline_v51")) == 1


def test_interruption_summary_mentions_phase_and_progress(snap_pid):
    from app.services import task_snapshot as ts

    row = ts.register(snap_pid, "outline_v51", phase="二级目录生成", progress=45.0, owner_id="o1")
    _age_snapshot(row["task_id"])
    text = ts.interruption_summary(ts.list_interruptions(snap_pid)[0])
    assert "目录生成" in text
    assert "二级目录生成" in text
    assert "45" in text


# ---- L.6 reconcile_stale ----


def test_reconcile_stale_marks_interrupted(snap_pid):
    from app.services import task_snapshot as ts

    row = ts.register(snap_pid, "outline_v51", phase="AI 自检", owner_id="o1")
    _age_snapshot(row["task_id"])
    converged = {r["task_id"] for r in ts.reconcile_stale()}
    assert row["task_id"] in converged
    saved = _snap_row(row["task_id"])
    assert saved["status"] == ts.STATUS_INTERRUPTED
    assert "心跳停止" in saved["interrupted_reason"]


def test_reconcile_stale_leaves_fresh_rows(snap_pid):
    """关键：判据必须是**心跳超时**，不能是「进程重启了所以全死了」。

    多 worker 部署下，后启动的进程会把先启动进程正在跑的任务全部误杀 ——
    这正是 Task 8 在 generation_manager 里修过的同类缺陷。
    """
    from app.services import task_snapshot as ts

    row = ts.register(snap_pid, "outline_v51", owner_id="o1")
    ts.reconcile_stale()
    assert _snap_row(row["task_id"])["status"] == ts.STATUS_RUNNING


def test_reconcile_stale_is_idempotent(snap_pid):
    from app.services import task_snapshot as ts

    row = ts.register(snap_pid, "outline_v51", owner_id="o1")
    _age_snapshot(row["task_id"])
    ts.reconcile_stale()
    assert _snap_row(row["task_id"])["status"] == ts.STATUS_INTERRUPTED
    ts.reconcile_stale()
    assert _snap_row(row["task_id"])["status"] == ts.STATUS_INTERRUPTED


# ---- L.7 acknowledge ----


def test_acknowledge_scoped_by_task_type(snap_pid):
    from app.services import task_snapshot as ts

    a = ts.register(snap_pid, "outline_v51", owner_id="o1")
    _age_snapshot(a["task_id"])
    ts.reconcile_stale()
    b = ts.register(snap_pid, "content_generation", owner_id="o2")
    _age_snapshot(b["task_id"])
    ts.reconcile_stale()
    assert ts.acknowledge(snap_pid, task_type="outline_v51") == 1
    left = ts.list_interruptions(snap_pid)
    assert len(left) == 1
    assert left[0]["task_type"] == "content_generation"


def test_acknowledge_keeps_audit_trail(snap_pid):
    """确认只置标记，不能删行 —— 中断轨迹要留作审计。"""
    from app.services import task_snapshot as ts

    row = ts.register(snap_pid, "outline_v51", owner_id="o1")
    _age_snapshot(row["task_id"])
    ts.reconcile_stale()
    ts.acknowledge(snap_pid)
    assert _snap_row(row["task_id"])["status"] == ts.STATUS_INTERRUPTED
    assert _snap_row(row["task_id"])["acknowledged"] == 1

def test_acknowledge_reconciles_stale_running(snap_pid):
    """BUG-FIX 2026-09-15：ack 必须能确认掉「心跳超时但仍是 running」的行。

    多 worker / 未重启场景下启动对账还没跑到那一条，`list_interruptions` 已经
    把它报告为中断，但 `acknowledge` 原来只认 `status = 'interrupted'` → 返回 0，
    前端横幅点不掉（用户对着假中断反复点击）。这条用例刻意**不调 reconcile_stale**，
    复现原始触发条件。
    """
    from app.services import task_snapshot as ts

    row = ts.register(snap_pid, "outline_v51", owner_id="o1")
    _age_snapshot(row["task_id"])
    # 前提：读侧已经把它算作中断，写侧却还没收敛
    assert len(ts.list_interruptions(snap_pid)) == 1
    assert _snap_row(row["task_id"])["status"] == ts.STATUS_RUNNING

    assert ts.acknowledge(snap_pid) == 1

    assert _snap_row(row["task_id"])["status"] == ts.STATUS_INTERRUPTED
    assert _snap_row(row["task_id"])["acknowledged"] == 1
    assert "心跳停止" in _snap_row(row["task_id"])["interrupted_reason"]
    assert ts.list_interruptions(snap_pid) == []


def test_acknowledge_reconcile_scope_is_limited_to_project(snap_pid):
    """收敛范围要限定在本项目：确认 A 项目不能顺手改写 B 项目的审计轨迹。"""
    from app.services import task_snapshot as ts

    other_pid = snap_pid + 500000
    _seed_lock_project(other_pid)
    try:
        mine = ts.register(snap_pid, "outline_v51", owner_id="o1")
        theirs = ts.register(other_pid, "outline_v51", owner_id="o2")
        _age_snapshot(mine["task_id"])
        _age_snapshot(theirs["task_id"])

        assert ts.acknowledge(snap_pid) == 1

        assert _snap_row(mine["task_id"])["status"] == ts.STATUS_INTERRUPTED
        # 另一个项目仍是 running，等它自己的入口或启动对账处理
        assert _snap_row(theirs["task_id"])["status"] == ts.STATUS_RUNNING
    finally:
        from app.db import get_db

        with get_db() as conn:
            conn.execute("DELETE FROM background_tasks WHERE project_id = ?", (other_pid,))



# ---- L.8 read_state（在调用方连接上读）----


def test_read_state_returns_none_without_running_row(snap_pid):
    from app.db import get_db
    from app.services import task_snapshot as ts

    with get_db() as conn:
        assert ts.read_state(conn, snap_pid, "outline_v51") is None


def test_read_state_reads_within_caller_transaction(snap_pid):
    """read_state 不能在调用方事务中触发 commit（否则外层 BEGIN 会被提前提交）。"""
    from app.db import get_db
    from app.services import task_snapshot as ts

    row = ts.register(snap_pid, "outline_v51", owner_id="o1")
    with get_db() as conn:
        conn.execute("BEGIN")
        state = ts.read_state(conn, snap_pid, "outline_v51")
        assert state is not None
        assert state["task_id"] == row["task_id"]
        assert state["task_type_label"] == "目录生成"
        conn.execute("ROLLBACK")


# ---- L.9 V5.1 活跃状态保护（BUG-FIX 2026-09-15）----


def test_load_v51_state_does_not_overwrite_active_run(snap_pid):
    """运行中的 V51TaskState 不能被 DB 行重建覆盖。

    覆盖之后 cancel_v51_task / resume_v51_task 改的是新对象，而正在跑的工作流
    检查的是旧对象 —— 「停止生成」会静默失效。
    """
    from app.services import v51_state as vs

    state = vs.create_v51_state(snap_pid)
    state.current_phase = "level2"
    vs.set_v51_active_run(snap_pid, True)
    _insert_v51_row(snap_pid, "level1")

    loaded = vs.load_v51_state_from_db(snap_pid)

    assert loaded is state, "活跃运行中的状态被 DB 行覆盖了"
    assert vs.get_v51_state(snap_pid) is state
    assert state.current_phase == "level2"


def test_cancel_reaches_the_running_object(snap_pid):
    """用户点「停止生成」必须作用在工作流持有的那个对象上。"""
    from app.services import v51_state as vs

    state = vs.create_v51_state(snap_pid)
    vs.set_v51_active_run(snap_pid, True)
    _insert_v51_row(snap_pid, "level1")

    # 模拟「别处触发了 DB 重载」（修复前这里会换掉 _v51_states 里的对象）
    vs.load_v51_state_from_db(snap_pid)
    vs.cancel_v51_task(snap_pid)

    assert state.cancelled is True, "取消打到了新对象上，运行中的任务收不到取消信号"


def test_load_v51_state_reloads_after_run_finished(snap_pid):
    """收尾后必须恢复「能从 DB 重建」的行为，否则状态永远陈旧。"""
    from app.services import v51_state as vs

    state = vs.create_v51_state(snap_pid)
    state.current_phase = "level3"
    vs.set_v51_active_run(snap_pid, True)
    vs.set_v51_active_run(snap_pid, False)
    _insert_v51_row(snap_pid, "level1")

    loaded = vs.load_v51_state_from_db(snap_pid)

    assert loaded is not None
    assert loaded is not state
    assert loaded.current_phase == "level1"


def test_set_v51_active_run_returns_false_without_state(snap_pid):
    from app.services import v51_state as vs

    vs.remove_v51_state(snap_pid)
    assert vs.set_v51_active_run(snap_pid, True) is False


def test_v51_state_defaults_to_inactive():
    from app.services import v51_state as vs

    state = vs.V51TaskState(999999)
    assert state.active_run is False


# ---- L.10 目录生成接线（源码级静态检查）----


def test_outline_sse_acquires_group_lock():
    """目录生成必须走 Task 8 建的「技术方案」组锁，否则同组互斥形同虚设。"""
    src = _read_backend_source("app/routers/sse/sse_outline.py")
    assert "_task_lock.acquire" in src
    assert '"outline-generation"' in src
    assert "_task_lock.TaskLockConflict" in src
    assert "TASK_LOCK_CONFLICT" in src


def test_outline_sse_registers_snapshot_and_finishes():
    src = _read_backend_source("app/routers/sse/sse_outline.py")
    assert "task_snapshot.register" in src
    assert "task_snapshot.finish" in src
    assert 'task_snapshot.list_interruptions, project_id, task_type="outline_v51"' in src


def test_outline_sse_has_dedicated_heartbeat_loop():
    """心跳必须独立于事件流：L2 填充 / AI 自检可能十几分钟没有事件。"""
    src = _read_backend_source("app/routers/sse/sse_outline.py")
    assert "async def _outline_heartbeat_loop(" in src
    assert "_task_lock.heartbeat" in src
    assert "task_snapshot.heartbeat" in src


def test_outline_sse_releases_lock_with_shield():
    """释放锁要用 shield：收尾阶段被再次取消时，释放动作仍要跑完。"""
    src = _read_backend_source("app/routers/sse/sse_outline.py")
    assert "asyncio.shield" in src
    assert "_task_lock.release" in src


def test_is_reconnection_no_longer_uses_timestamp_heuristic():
    """原判据对「两次请求间隔 < 0.1 秒」的并发入口失效，必须换成确定性事实。"""
    src = _read_backend_source("app/routers/sse/sse_outline.py")
    assert "task_info.started_at < time.time() - 0.1" not in src
    assert 'getattr(task_info, "_task", None) is not None' in src


def test_outline_heartbeat_interval_leaves_margin():
    from app.routers.sse import sse_outline
    from app.services import task_lock, task_snapshot

    assert 0 < sse_outline.HEARTBEAT_INTERVAL_SECONDS < task_snapshot.DEFAULT_STALE_SECONDS
    assert sse_outline.HEARTBEAT_INTERVAL_SECONDS * 2 < task_lock.DEFAULT_TTL_SECONDS


def test_lock_owner_encodes_task_id():
    from app.routers.sse import sse_outline

    assert sse_outline._make_lock_owner(42, "abc") == "outline-generation:42:abc"


async def test_snapshot_progress_is_noop_without_snapshot_id():
    from app.routers.sse import sse_outline

    await sse_outline._snapshot_progress({"snapshot_task_id": ""}, phase="x")
    await sse_outline._snapshot_progress(None, phase="x")


def test_startup_reconciles_task_snapshots():
    """启动钩子必须做一次对账，否则幽灵任务永远挂在「运行中」。"""
    src = _read_backend_source("app/main.py")
    assert "task_snapshot" in src
    assert "reconcile_stale" in src


# ---- L.11 接口接线 ----


def test_tasks_router_exposes_interrupted_endpoints():
    from app.routers import tasks as tasks_router

    paths = {r.path for r in tasks_router.router.routes}
    assert "/projects/{project_id}/tasks/interrupted" in paths
    assert "/projects/{project_id}/tasks/interrupted/ack" in paths


async def test_status_reports_interrupted_snapshot(snap_pid):
    from app.routers.tasks import get_all_tasks_status
    from app.services import task_snapshot as ts

    row = ts.register(snap_pid, "outline_v51", phase="二级目录生成", progress=45.0, owner_id="o1")
    _age_snapshot(row["task_id"])

    result = await get_all_tasks_status(snap_pid, None)

    types = [o["type"] for o in result["recovery_options"]]
    assert "outline_interrupted" in types
    assert result["has_pending_tasks"] is True
    assert result["recovery_available"] is True
    assert result["tasks"]["interrupted_outline_v51"]["phase"] == "二级目录生成"


async def test_interrupted_endpoint_returns_items(snap_pid):
    from app.routers.tasks import get_interrupted_tasks
    from app.services import task_snapshot as ts

    row = ts.register(snap_pid, "outline_v51", phase="AI 自检", progress=88.0, owner_id="o1")
    _age_snapshot(row["task_id"])

    res = await get_interrupted_tasks(snap_pid, None)

    assert res["total"] == 1
    assert res["items"][0]["phase"] == "AI 自检"
    assert res["items"][0]["task_type_label"] == "目录生成"
    assert "中断" in res["items"][0]["summary"]


async def test_ack_endpoint_clears_banner(snap_pid):
    from app.routers.tasks import ack_interrupted_tasks
    from app.services import task_snapshot as ts

    row = ts.register(snap_pid, "outline_v51", owner_id="o1")
    _age_snapshot(row["task_id"])

    res = await ack_interrupted_tasks(snap_pid, None, None)

    assert res["acknowledged"] == 1
    assert ts.list_interruptions(snap_pid) == []

# ==================== M. 任务组锁的横向落地（Task 25~28）====================
# ENHANCE 2026-09-15：Task 8 建了 `TASK_GROUPS` 分组注册表，但只把锁接进了
# 「技术方案」组的两条路径（目录生成 / 正文生成）。本次横向体检发现
# 「废标项检查」「标书查重」「文档导出」三组**登记了但生产零调用方** ——
# 其中导出还额外有两个真 BUG（进程内锁 + 构建跑在锁外）。
# 本节守住「接线顺序」与「行为」两层，防止再次出现「登记了但没人用」。


def _line_of(src: str, needle: str) -> int:
    """返回 `needle` 首次出现的行号（1 基）；找不到就报错而不是静默返回 -1。"""
    for i, line in enumerate(src.split("\n"), 1):
        if needle in line:
            return i
    raise AssertionError("源码里找不到: %s" % needle)


def _slice_lines(src: str, start: str, end: str | None = None) -> str:
    """按**行**截取源码片段（含 `start` 行，不含 `end` 行）。

    注意别写成 `src[_line_of(src, x):]` —— `_line_of` 返回的是行号，
    当字符索引使用会从「第 N 个字符」开始截，取到的是文件开头的模块 docstring，
    断言失败时打印出来的"实际内容"完全对不上（本节最初就踩了这个坑）。
    """
    lines = src.split("\n")
    a = _line_of(src, start) - 1
    b = _line_of(src, end) - 1 if end else len(lines)
    return "\n".join(lines[a:b])


async def _acquire_via_thread(task_lock_mod, task_type: str, project_id: int) -> tuple[str, str]:
    """走 `task_lock` 的通用入口抢锁（不经过路由辅助），返回 `(lock_key, owner_id)`。"""
    owner = task_lock_mod.make_owner_id(task_type, project_id)
    rec = await asyncio.to_thread(
        functools.partial(task_lock_mod.acquire, task_type, project_id=project_id, owner_id=owner)
    )
    return rec.get("lock_key", ""), owner


# ---- M.1 接线顺序（源码级）----


def test_rejection_acquires_lock_before_creating_task():
    """抢锁必须早于建任务：反过来的话抢锁失败时已经落了一行 rejection_tasks 脏数据。"""
    src = _read_backend_source("app/routers/rejection.py")
    assert "_acquire_check_lock(req.project_id)" in src
    assert _line_of(src, "_acquire_check_lock(req.project_id)") < _line_of(
        src, "bg.add_task(_run_safely"
    )
    assert "rejection.create_task(" in src or "rejection.create_task," in src, (
        "建任务调用不应被移除；PERF-2026-10-06 起它以 "
        "`asyncio.to_thread(rejection.create_task, ...)` 形态出现，故同时接受 `(` 与 `,`"
    )


def test_duplicate_acquires_lock_before_creating_task():
    src = _read_backend_source("app/routers/duplicate.py")
    assert "_acquire_dup_lock(req.project_id)" in src
    assert _line_of(src, "_acquire_dup_lock(req.project_id)") < _line_of(
        src, "bg.add_task(_run_safely"
    )
    assert "duplicate.create_task(" in src


def test_export_acquires_lock_before_build_starts():
    """SSE 导出的构建必须跑在锁内。

    原实现把 `asyncio.create_task(asyncio.to_thread(_run_build))` 放在路由层、
    锁却是在 SSE 生成器里才 `async with` —— 而 `asyncio.to_thread` 会**立即**
    把 `_run_build` 提交到线程池，于是 `build_docx` 全程跑在锁外面。
    """
    src = _read_backend_source("app/routers/export.py")
    lock_line = _line_of(src, "lock_key, owner_id = await _acquire_export_lock(project_id)")
    build_line = _line_of(src, "asyncio.create_task(asyncio.to_thread(_run_build))")
    assert lock_line < build_line, "抢锁(L%d) 必须早于构建启动(L%d)" % (lock_line, build_line)
    # 构建只能启动一次（曾经因为补丁失误留了两处）
    assert src.count("asyncio.create_task(asyncio.to_thread(_run_build))") == 1


def test_export_dropped_the_in_process_lock():
    """进程内 asyncio.Lock 在多 worker 下每个进程各一把，等于没有互斥。"""
    src = _read_backend_source("app/routers/export.py")
    assert "_export_locks_guard" not in src
    assert "export_lock.locked()" not in src
    assert "async with export_lock" not in src
    assert "asyncio.Lock()" not in src, "导出侧不该再自建锁容器"
    assert "task_lock" in src


def test_background_task_locks_released_in_finally():
    """BackgroundTasks 模式下请求早已返回，锁只能在任务收尾的 finally 释放。"""
    for rel in ("app/routers/rejection.py", "app/routers/duplicate.py"):
        src = _read_backend_source(rel)
        tail = _slice_lines(src, "async def _run_safely(")
        assert "finally:" in tail, "%s 的 _run_safely 缺少 finally" % rel
        assert "_release_" in tail
    # 锁不能只在请求体里释放（那样任务还没跑锁就没了）
    rej = _read_backend_source("app/routers/rejection.py")
    body = _slice_lines(rej, "async def start_check(", "async def _run_safely(")
    assert body.count("_release_check_lock") == 1, "请求体内只应有「建任务失败回滚」这一处释放"


def test_export_stream_releases_lock_in_build_finally():
    src = _read_backend_source("app/routers/export.py")
    tail = _slice_lines(src, "def _run_build(")
    assert "finally:" in tail
    assert "_task_lock.release(lock_key, owner_id)" in tail


def test_export_sync_path_releases_lock_in_finally():
    """同步导出同样要 try/finally 释放，否则异常时锁要挂到 TTL 过期。"""
    src = _read_backend_source("app/routers/export.py")
    assert "_release_export_lock(lock_key, owner_id, why=f'同步导出 project={project_id}')" in src


def test_duplicate_cached_branch_releases_lock():
    """命中缓存时不会起后台任务，锁没有收尾者，必须当场释放。

    不释放的话该项目 30 分钟内再也发不起查重（被自己的锁挡住）。
    """
    src = _read_backend_source("app/routers/duplicate.py")
    assert "_release_dup_lock" in _slice_lines(src, "if is_cached:", "async def _run_safely(")


# ---- M.2 行为：抢锁 / 冲突 / 释放 ----


async def test_rejection_lock_conflict_returns_409(lock_pid):
    from fastapi import HTTPException

    from app.routers import rejection as rej

    key, owner = await rej._acquire_check_lock(lock_pid)
    assert key and owner
    with pytest.raises(HTTPException) as ei:
        await rej._acquire_check_lock(lock_pid)
    assert ei.value.status_code == 409
    assert "废标项检查" in ei.value.detail
    await rej._release_check_lock(key, owner, why="test")
    key2, owner2 = await rej._acquire_check_lock(lock_pid)
    assert key2 == key, "释放后应当能重新拿到同一把锁"
    await rej._release_check_lock(key2, owner2, why="test")


async def test_duplicate_lock_conflict_returns_409(lock_pid):
    from fastapi import HTTPException

    from app.routers import duplicate as dup

    key, owner = await dup._acquire_dup_lock(lock_pid)
    with pytest.raises(HTTPException) as ei:
        await dup._acquire_dup_lock(lock_pid)
    assert ei.value.status_code == 409
    assert "标书查重" in ei.value.detail
    await dup._release_dup_lock(key, owner, why="test")
    key2, owner2 = await dup._acquire_dup_lock(lock_pid)
    assert key2 == key
    await dup._release_dup_lock(key2, owner2, why="test")


async def test_export_lock_conflict_returns_409(lock_pid):
    from fastapi import HTTPException

    from app.routers import export as exp

    key, owner = await exp._acquire_export_lock(lock_pid)
    with pytest.raises(HTTPException) as ei:
        await exp._acquire_export_lock(lock_pid)
    assert ei.value.status_code == 409
    assert "文档导出" in ei.value.detail
    await exp._release_export_lock(key, owner, why="test")
    key2, owner2 = await exp._acquire_export_lock(lock_pid)
    assert key2 == key
    await exp._release_export_lock(key2, owner2, why="test")


async def test_acquire_helpers_return_lock_key_and_owner(lock_pid):
    """三个辅助函数都必须返回 `(lock_key, owner_id)` —— 调用方要靠它释放锁。"""
    from app.routers import duplicate as dup
    from app.routers import export as exp
    from app.routers import rejection as rej

    for acquire, release in (
        (rej._acquire_check_lock, rej._release_check_lock),
        (dup._acquire_dup_lock, dup._release_dup_lock),
        (exp._acquire_export_lock, exp._release_export_lock),
    ):
        key, owner = await acquire(lock_pid)
        assert isinstance(key, str) and key, "lock_key 不能为空"
        assert isinstance(owner, str) and owner, "owner_id 不能为空"
        assert ":" in key, "锁键形如 {group}:{project_id}"
        await release(key, owner, why="test")


async def test_release_helpers_are_noop_on_empty_args(lock_pid):
    """未登记分组时 `acquire` 返回空 `lock_key`，释放辅助必须容忍空值而不抛。"""
    from app.routers import duplicate as dup
    from app.routers import export as exp
    from app.routers import rejection as rej

    await rej._release_check_lock("", "", why="test")
    await rej._release_check_lock("k", "", why="test")
    await dup._release_dup_lock("", "", why="test")
    await exp._release_export_lock("", "", why="test")


async def test_router_lock_release_is_idempotent(lock_pid):
    """重复释放不报错（收尾路径可能被执行两次）。"""
    from app.routers import rejection as rej

    key, owner = await rej._acquire_check_lock(lock_pid)
    await rej._release_check_lock(key, owner, why="test")
    await rej._release_check_lock(key, owner, why="test")


async def test_group_locks_isolated_per_project(lock_pid):
    """不同项目的同组锁互不干扰（锁键含 project_id）。"""
    from app.routers import rejection as rej

    other = lock_pid + 900000
    _seed_lock_project(other)
    try:
        k1, o1 = await rej._acquire_check_lock(lock_pid)
        k2, o2 = await rej._acquire_check_lock(other)
        assert k1 != k2
        assert str(other) in k2
        await rej._release_check_lock(k2, o2, why="test")
        await rej._release_check_lock(k1, o1, why="test")
    finally:
        from app.db import get_db

        with get_db() as conn:
            conn.execute("DELETE FROM task_locks WHERE project_id = ?", (other,))


async def test_same_group_cross_task_type_mutex(lock_pid):
    """同组跨任务类型互斥：废标检查跑着时，同组的「无效与废标项解析」也进不来。

    这是「分组」概念的实质检验 —— 如果锁键用了 task_type 而不是 group，这条会失败。
    """
    from app.services import task_lock

    k, o = await _acquire_via_thread(task_lock, "rejection-check-run", lock_pid)
    try:
        with pytest.raises(task_lock.TaskLockConflict):
            await _acquire_via_thread(task_lock, "rejection-items-extraction", lock_pid)
    finally:
        await asyncio.to_thread(task_lock.release, k, o)


# ---- M.3 并发 ----


async def test_concurrent_acquire_only_one_wins(lock_pid):
    """真并发：三个协程同时抢同一把锁，只能有一个成功。

    `_acquire_check_lock` 内部走 `asyncio.to_thread` → sqlite 行级唯一索引，
    不是「先查后插」的 TOCTOU。
    """
    from fastapi import HTTPException

    from app.routers import rejection as rej

    async def attempt():
        try:
            k, o = await rej._acquire_check_lock(lock_pid)
            return ("ok", k, o)
        except HTTPException as e:
            return ("conflict", e.status_code, e.detail)

    results = await asyncio.gather(attempt(), attempt(), attempt())
    oks = [r for r in results if r[0] == "ok"]
    conflicts = [r for r in results if r[0] == "conflict"]
    assert len(oks) == 1, "并发下应只有一个成功，实际 %d" % len(oks)
    assert len(conflicts) == 2
    assert all(c[1] == 409 for c in conflicts)
    await rej._release_check_lock(oks[0][1], oks[0][2], why="test")


# ---- M.4 分组注册表一致性 ----


def test_wired_task_types_are_registered():
    """已接锁的 task_type 必须登记在 `TASK_GROUPS`。

    未登记的话 `acquire` 会静默返回 `acquired=False, reason='ungrouped'`
    —— 锁看起来「抢到了」，实际什么都没做。
    """
    from app.services import task_lock

    wired = {
        "outline-generation": "technical-plan",
        "content-generation": "technical-plan",
        "rejection-check-run": "rejection-check",
        "duplicate-analysis": "duplicate-check",
        "export": "export",
    }
    for task_type, group in wired.items():
        assert task_lock.group_for_task(task_type) == group, "%s 应属于 %s" % (task_type, group)
        assert task_lock.TASK_LABELS.get(task_type), "%s 缺少中文标签" % task_type


def test_unwired_registry_entries_are_documented():
    """未接锁的登记项必须**恰好**是这几个，防止无声增删。

    分两类：
    - 僵尸登记项（本软件无对应功能，从参考软件照搬）：bid-section-extraction /
      bid-analysis / outline-adjustment / global-facts-adjustment；
    - 同步短任务（无不可恢复损害，评估后不接锁）：global-facts-generation /
      rejection-items-extraction。

    任一类发生变化都应当是有意识的决定，而不是悄悄漂移。
    """
    from app.services import task_lock

    unwired = {
        "bid-section-extraction",
        "bid-analysis",
        "outline-adjustment",
        "global-facts-adjustment",
        "global-facts-generation",
        "rejection-items-extraction",
    }
    wired = {
        "outline-generation",
        "content-generation",
        "rejection-check-run",
        "duplicate-analysis",
        # ENHANCE 2026-09-15：可行性研究报告模块（四个 AI 任务共用一组互斥锁）
        "feasibility-analysis",
        "feasibility-outline",
        "feasibility-parameters",
        "feasibility-content",
        "export",
    }
    assert set(task_lock.TASK_GROUPS) == unwired | wired
    assert len(task_lock.GROUP_LABELS) == 5


def test_group_labels_cover_all_groups():
    """每个组都要有中文标签，否则 409 提示会退化成英文组名。"""
    from app.services import task_lock

    for group in set(task_lock.TASK_GROUPS.values()):
        assert group in task_lock.GROUP_LABELS, "组 %s 缺少中文标签" % group


# ============================================================================
# N. 前端 409 组锁冲突体检回归（Task 11）
# ============================================================================

def test_export_lock_conflict_returns_friendly_chinese_detail():
    """组锁冲突必须返回面向用户的中文提示——即前端 409 弹窗展示的文案。

    Task 11 前端 409 体检：SSE/同步导出抢锁失败，路由抛
    `HTTPException(409, detail=str(e))`，`str(e)` 就是 `TaskLockConflict` 的
    中文消息，前端 `message.error(detail)` 原样展示。若有人把它改成
    「请稍候再试」之类硬编码，用户就看不到是哪个任务在占用。
    """
    from app.services import task_lock

    pid = 912011
    owner_a = task_lock.make_owner_id("export", pid)
    owner_b = owner_a + "-conflict"
    rec = task_lock.acquire("export", project_id=pid, owner_id=owner_a, note="regression-conflict")
    try:
        with pytest.raises(task_lock.TaskLockConflict) as exc:
            task_lock.acquire("export", project_id=pid, owner_id=owner_b, note="regression-conflict-b")
        msg = str(exc.value)
        assert "正在执行" in msg, "冲突文案应说明「谁正在执行」"
        assert "请完成后再启动" in msg, "冲突文案应给出「请完成后再启动」引导"
        # 必须是给用户看的纯中文，不能泄露内部异常类名/栈
        assert "TaskLockConflict" not in msg
        assert "Traceback" not in msg
    finally:
        task_lock.release(rec["lock_key"], owner_a)


def test_routers_map_lock_conflict_to_409_with_friendly_detail():
    """rejection/duplicate/export 三个抢组锁路由都必须把冲突转成
    `HTTPException(409, detail=str(e))`，确保友好中文透传到前端（而非 500/泛型）。

    Task 11 体检：只要这行在，前端拦截器就能 `message.error(detail)` 展示
    「当前X正在执行Y，请完成后再启动Z」。若改成硬编码 409 文案，用户失去
    「是谁占用」的上下文。
    """
    import inspect
    from app.routers import rejection, duplicate, export as export_router

    for mod in (rejection, duplicate, export_router):
        src = inspect.getsource(mod)
        assert "raise HTTPException(status_code=409, detail=str(e))" in src, (
            f"{mod.__name__} 必须把 TaskLockConflict 转成 409 且 detail=str(e)（透传友好中文）"
        )
