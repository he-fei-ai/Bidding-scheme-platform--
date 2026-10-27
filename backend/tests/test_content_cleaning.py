from app.services.content_cleaning import (
    clean_generated_content,
    parse_content,
    _split_html_line,
)


def test_clean_basic_text():
    assert clean_generated_content("这是正文") == "这是正文"


def test_clean_think_tags():
    """测试移除 think/reason 标签"""
    raw = "好的，我来生成内容\n\n这是正文"
    result = clean_generated_content(raw)
    assert "这是正文" in result


def test_clean_code_fence():
    raw = "```\nhello\n```"
    assert clean_generated_content(raw) == "hello"


def test_clean_code_fence_json():
    raw = '```json\n{"a": 1}\n```正文内容'
    result = clean_generated_content(raw)
    assert "正文内容" in result or '{"a": 1}' in result


def test_clean_leading_hash():
    """测试清理行首 # 和 ** 粗体标记"""
    raw = "### 标题\n**粗体**"
    result = clean_generated_content(raw)
    assert result == "标题\n粗体"


def test_clean_bold_text():
    assert clean_generated_content("Hello **World**") == "Hello World"


# ===== BUG C 修复：富文本 HTML（<img> 图片 + 标签剥离） =====

def test_parse_content_image_block():
    """RichTextEditor 插入的 <img> 应解析为 image 块而非原始 HTML 文本"""
    data_url = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAA=="
    content = f'<div style="text-align: center;"><img src="{data_url}" alt="流程图" /></div>'
    blocks = parse_content(content)
    images = [b for b in blocks if b.type == "image"]
    assert len(images) == 1, f"应解析出 1 个 image 块，实际 {len(images)}"
    assert images[0].text == data_url


def test_parse_content_strips_html_tags():
    """<p>/<b> 等富文本标签应剥离为纯文本，导出为干净段落"""
    content = "<p>本条依据<b>《建设工程质量管理条例》</b>执行。</p><p>下一段正文内容</p>"
    blocks = parse_content(content)
    texts = [b.text for b in blocks if isinstance(b.text, str)]
    assert any("本条依据《建设工程质量管理条例》执行" in t for t in texts), f"HTML 标签未剥离: {texts}"
    assert any("下一段正文内容" in t for t in texts), f"多段 HTML 未正确拆分: {texts}"
    # 不应对导出造成影响的原材料（原始 HTML 标签）不应残留
    assert not any("<p>" in t or "<b>" in t for t in texts)


def test_parse_content_image_plus_text_line():
    """同一行既有 <img> 又有文本时：图片成 image 块，文本成 paragraph 块"""
    data_url = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAA=="
    content = f'<p><img src="{data_url}" /> 现场布置示意图</p>'
    blocks = parse_content(content)
    assert any(b.type == "image" and b.text == data_url for b in blocks)
    assert any(b.type == "paragraph" and "现场布置示意图" in b.text for b in blocks)


def test_split_html_line_entities():
    """HTML 实体应反转义，<br> 被剥离"""
    images, text = _split_html_line("<p>温度&nbsp;≥&nbsp;35℃<br>禁止作业</p>")
    assert images == []
    assert "≥" in text and "禁止作业" in text
    assert "<" not in text


def test_parse_content_does_not_confuse_table_marker():
    """<<表格：...>> 占位符不应被 HTML 分支误判（回归保护）"""
    content = "<<表格：表1 人员配置\n| 岗位 | 人数 |\n| 项目经理 | 1 |\n>>"
    blocks = parse_content(content)
    assert any(b.type == "table" for b in blocks), "表格占位符应解析为 table 块"


# ============================================================================
# 《招投标软件导出文档 — 分类问题报告》A-1/A-3/A-4/E-1~E-5（2026-10-26）
# ============================================================================


def test_clean_removes_system_residue():
    """系统提示 / 内部章节标记 / 示意性注释不得进入正文（E-1/E-3/E-5）。"""
    raw = (
        "这是正常正文，长度足够写满一句话。\n\n"
        "请确认是否生成正式投标文本。\n\n"
        "【章节L3】应急预案与风险控制（第9章）\n\n"
        "注：图中数据为资金分配逻辑示例，非真实数据。"
    )
    out = clean_generated_content(raw)
    assert "请确认是否生成" not in out
    assert "【章节L3】" not in out
    assert "资金分配逻辑示例" not in out
    assert "这是正常正文" in out


def test_clean_removes_non_body_block():
    """【…非正文…】标记及其后整段审阅稿一并删除（E-2）。"""
    raw = (
        "正式正文第一段，内容完整。\n\n"
        "【本章背景补充（供审阅溯源，非正文）】\n"
        "这段是审阅用的背景补充，不应出现在标书里。\n\n"
        "正式正文第二段，内容完整。"
    )
    out = clean_generated_content(raw)
    assert "非正文" not in out
    assert "审阅用的背景补充" not in out
    assert "正式正文第一段" in out and "正式正文第二段" in out


def test_clean_git_conflict_keeps_head_side():
    """Git 冲突块默认保留 HEAD 一侧，dev 侧与标记全部删除（E-4）。"""
    raw = (
        "冲突前的正文。\n\n"
        "<<<<<<< HEAD\n"
        "HEAD 侧应保留的内容。\n"
        "=======\n"
        "dev 侧应丢弃的内容。\n"
        ">>>>>>> dev\n\n"
        "冲突后的正文。"
    )
    out = clean_generated_content(raw)
    assert "HEAD 侧应保留的内容" in out
    assert "dev 侧应丢弃的内容" not in out
    assert "<<<<<<<" not in out and "=======" not in out and ">>>>>>>" not in out


def test_clean_strips_html_tags():
    """正文 `<br>` 折行、其余 HTML 标签删除（A-3）。"""
    raw = "北侧物料主入口<br>1.5 m×2.2m装配式大门<p>叉车水平搬运</p>额定1.5吨"
    out = clean_generated_content(raw)
    assert "<br>" not in out and "<p>" not in out
    assert "北侧物料主入口" in out and "1.5 m×2.2m装配式大门" in out
    assert "叉车水平搬运" in out and "额定1.5吨" in out


def test_clean_strips_stray_mermaid_lines():
    """漏在围栏外的 Mermaid 关键字/连线行应删除（A-4）。"""
    raw = (
        "施工区段入口设置告示牌。\n\n"
        "graph LR\n"
        "A[施工区段入口] \\(\\rightarrow\\) B[告示牌/当日作业信息牌]\n\n"
        "flowchart LR\n\n"
        "后续正常正文，内容完整。"
    )
    out = clean_generated_content(raw)
    assert "graph LR" not in out and "flowchart LR" not in out
    assert "A[施工区段入口]" not in out
    assert "施工区段入口设置告示牌" in out
    assert "后续正常正文" in out


def test_clean_dedupes_consecutive_paragraphs():
    """连续重复段落只保留一次（A-1/B-1）；首段带编号、次段无编号也能识别。"""
    para = "围挡采用2.5m高彩钢板，底部设置30cm高砖砌挡水台，转角处加设斜撑固定。"
    raw = f"1.2.1.1 {para}\n\n{para}\n\n后续不同内容，必须保留。"
    out = clean_generated_content(raw)
    assert out.count(para) == 1, out
    assert "后续不同内容" in out


def test_clean_keeps_short_repeated_lines():
    """短句/列表项重复不属于段落重复，不得误删。"""
    raw = "（1）检查\n\n（1）检查\n\n正常段落内容。"
    out = clean_generated_content(raw)
    assert out.count("（1）检查") == 2, out


def test_clean_inline_text_for_table_cells():
    """表格单元格行内清洗：LaTeX 与 HTML 残留不得进入 DOCX（A-2/A-3）。"""
    from app.services.content_cleaning import clean_inline_text

    assert clean_inline_text(r"合格率 \(100\%\)") == "合格率 100%"
    assert clean_inline_text("北侧物料主入口<br>1.5 m×2.2m装配式大门") == (
        "北侧物料主入口\n1.5 m×2.2m装配式大门"
    )
    assert "<br>" not in clean_inline_text("叉车水平搬运<br>额定1.5吨")
