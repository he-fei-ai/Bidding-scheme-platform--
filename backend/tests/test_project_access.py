"""项目访问控制回归测试。"""
from app.security.auth import User, check_project_access


def test_admin_can_access_any_project():
    assert check_project_access(999999, User(1, "admin", "admin")) is True


def test_non_admin_fails_closed_without_membership_model():
    assert check_project_access(1, User(2, "member", "user")) is False
