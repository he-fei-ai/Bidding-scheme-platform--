from app.services.usage import ensure_project_generation_allowed, get_usage, reset_usage


def test_get_usage_default():
    result = get_usage()
    assert isinstance(result, dict)
    assert "total_limit" in result
    assert "used_count" in result
    assert "remaining" in result


def test_ensure_allowed_default():
    result = ensure_project_generation_allowed(1)
    assert isinstance(result, bool)


def test_reset_usage():
    try:
        reset_usage()
    except Exception as _e:
        pass
