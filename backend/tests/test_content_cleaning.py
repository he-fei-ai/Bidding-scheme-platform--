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
