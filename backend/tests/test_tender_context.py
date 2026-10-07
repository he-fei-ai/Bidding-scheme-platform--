from app.services.tender_context import extract_tender_context


def test_extract_context_empty():
    assert extract_tender_context("") == ""


def test_extract_context_short():
    text = "这是一个招标公告"
    result = extract_tender_context(text)
    assert result == text


def test_extract_context_keywords():
    text = "评分标准：技术方案占30分，需提供详细的技术路线图。"
    result = extract_tender_context(text)
    assert "技术方案" in result


def test_extract_context_no_keywords():
    text = "简单的描述文本内容"
    result = extract_tender_context(text)
    assert result == text
