from app.services.files import _pick_main_file, guess_file_role


def test_guess_file_role_main():
    assert guess_file_role("招标文件.pdf") == "main"
    assert guess_file_role("招标公告.docx") == "main"


def test_guess_file_role_fallback():
    assert guess_file_role("营业执照.pdf") == "main"
    assert guess_file_role("其他文件.pdf") == "main"


def test_pick_main_file_single():
    files = [{"id": 1, "filename": "招标文件.pdf", "file_role": "main"}]
    result = _pick_main_file(files)
    assert result is not None
    assert result["id"] == 1


def test_pick_main_file_no_main():
    assert _pick_main_file([]) is None
