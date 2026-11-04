"""2026-11-02 测试隔离守卫：功能开关的逐用例基线清理（conftest._clean_functional_switch_env）。

背景：conftest 原只在 pytest_collection_modifyitems（收集后）清一次 .env 功能开关，
但任何测试在**执行期**才首次 `from app.main import app`（如 TestClient 惰性导入），
main.py 的 load_dotenv(override=False) 会把「未设置」的开关从 .env 重新填回
os.environ，污染其后所有测试的 _env_flag() 判定——实证为
round6→facts_audit 组合下 2 个用例的顺序依赖假阴性。
本轮 conftest 新增 autouse fixture 在每个用例 setup 时兜底清除。

本守卫按定义顺序执行：前一个用例模拟「执行期污染」，后一个用例断言基线已恢复
（默认关）。若有人删掉该 fixture，第二个用例必红。
"""

import os


def test_pollute_env_switch_mid_session():
    """模拟执行期污染：直接写 os.environ 的功能开关应立即生效（证明测的是活路径）。"""
    from app.services import facts_service

    os.environ["CONSISTENCY_PATCH_REPAIR_ENABLED"] = "1"
    assert facts_service.consistency_patch_repair_enabled() is True


def test_baseline_restored_by_autouse_fixture():
    """上一用例的污染不得存活到下一用例：autouse fixture setup 期已清除。"""
    from app.services import facts_service

    assert "CONSISTENCY_PATCH_REPAIR_ENABLED" not in os.environ
    assert facts_service.consistency_patch_repair_enabled() is False
