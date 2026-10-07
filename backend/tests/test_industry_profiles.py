from app.services.ai.industry_profiles import INDUSTRY_PROFILES, get_industry_profile


def test_industry_profiles_exist():
    assert len(INDUSTRY_PROFILES) > 0


def test_industry_profile_known():
    profile = get_industry_profile("信息技术")
    assert profile is not None
    profile_str = str(profile)
    assert "技术" in profile_str


def test_industry_profile_unknown():
    profile = get_industry_profile("不存在的行业")
    assert profile is not None


def test_industry_profile_multiple():
    for industry in ["信息技术", "建筑工程", "医疗设备", "教育服务"]:
        profile = get_industry_profile(industry)
        assert profile is not None
