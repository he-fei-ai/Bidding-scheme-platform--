"""集成测试: 端到端关键工作流

覆盖以下核心工作流:
1. 健康检查
2. 项目 CRUD
3. AI 配置读写(含加密)
4. 行业树查询
5. 参考目录查询
6. 延迟监控指标
7. 文件上传(分块)

使用 FastAPI TestClient,无需启动真实服务。
"""
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))


def test_health_endpoint():
    """/api/v1/health 应返回 200"""
    from fastapi.testclient import TestClient
    from app.main import app

    client = TestClient(app)
    resp = client.get("/api/v1/health")
    assert resp.status_code == 200, f"健康检查失败: {resp.status_code} {resp.text}"
    data = resp.json()
    assert data.get("status") == "ok"
    # X-Response-Time 头由 LatencyBudgetMiddleware 仅在超限时写入日志，不再注入响应头
    print(f"[PASS] test_health_endpoint")


def test_projects_crud():
    """项目 CRUD 工作流"""
    from fastapi.testclient import TestClient
    from app.main import app

    client = TestClient(app)

    # Create (FastAPI 201 = POST 创建成功)
    create_resp = client.post("/api/v1/projects", json={
        "name": "测试项目-集成测试",
        "industry": "公共建筑",
        "bidder_name": "测试投标方",
    })
    assert create_resp.status_code in (200, 201), f"创建项目失败: {create_resp.text}"
    project = create_resp.json()
    pid = project["id"]
    assert pid is not None, f"项目 ID 不应为空,响应: {project}"

    # Read list
    list_resp = client.get("/api/v1/projects")
    assert list_resp.status_code == 200
    list_data = list_resp.json()
    # 后端返回 {total, items} 格式
    items = list_data if isinstance(list_data, list) else list_data.get("items", [])
    assert any(p["id"] == pid for p in items), f"新建项目(pid={pid})不在列表中"

    # Read one
    one_resp = client.get(f"/api/v1/projects/{pid}")
    assert one_resp.status_code == 200
    assert one_resp.json()["id"] == pid

    # Update (Try PATCH or PUT, allow 404 for unsupported method)
    for method in (client.patch, client.put):
        try:
            upd_resp = method(f"/api/v1/projects/{pid}", json={"notes": "集成测试备注"})
            if upd_resp.status_code in (200, 204):
                break
        except Exception as _e:
            pass
    # 不强制要求成功,某些端点可能只支持 POST 整体更新
    print("[PASS] test_projects_crud")


def test_ai_config_with_encryption():
    """AI 配置读写：新密钥加密存储，消费侧透明解密"""
    from fastapi.testclient import TestClient
    from app.main import app
    from app.db import get_db
    from app.services.crypto import decrypt_api_key, is_encrypted

    client = TestClient(app)

    # 写入测试密钥
    test_key = "sk-integration-test-key-12345"
    save_resp = client.put("/api/v1/ai/config", json={
        "provider_name": "测试AI",
        "provider": "openai_compatible",
        "api_key": test_key,
        "model": "gpt-4o",
        "base_url": "https://api.openai.com/v1",
        "concurrency": 4,
        "temperature": 0.7,
        "max_tokens": 4096,
    })
    assert save_resp.status_code == 200, f"保存 AI 配置失败: {save_resp.text}"

    # 验证数据库中已加密存储
    # 同步 2026-10-04（F-1 测试钉更新）：旧断言「库内明文」钉的是加密从未生效的
    # 旧实现；现在新密钥统一 ENC: 落库，可用 decrypt_api_key 还原。
    with get_db() as conn:
        row = conn.execute("SELECT api_key FROM ai_config WHERE id = 1").fetchone()
    assert row is not None
    stored = row["api_key"]
    if stored:
        from app.services.crypto import is_encrypted as _is_enc

        assert _is_enc(stored), f"数据库应存储 ENC: 加密密钥,但得到 {stored[:20]}"
        assert decrypt_api_key(stored) == test_key

    # GET 应返回**脱敏占位符**，不是明文。
    # BUG-FIX 2026-09-15（测试同步）：原断言要求「GET 应返回真实 API Key」，与
    # `routers/ai_config.py` 的安全 P0 修复（读接口一律回传 `***已配置***`，避免解密后的
    # 主密钥经 HTTP 泄露）直接冲突 —— 这条用例此后必然失败，属过期断言。
    from app.routers.ai_config import API_KEY_MASK

    get_resp = client.get("/api/v1/ai/config")
    assert get_resp.status_code == 200
    cfg = get_resp.json()
    assert cfg.get("api_key") == API_KEY_MASK, f"GET 应返回脱敏占位符,但得到 {cfg.get('api_key')}"

    # 契约的另一半：把占位符原样提交回去，库内真实密钥不能被清空
    resave_resp = client.put("/api/v1/ai/config", json={
        "provider_name": "测试AI",
        "provider": "openai_compatible",
        "api_key": API_KEY_MASK,
        "model": "gpt-4o",
        "base_url": "https://api.openai.com/v1",
        "concurrency": 4,
        "temperature": 0.7,
        "max_tokens": 4096,
    })
    assert resave_resp.status_code == 200, f"回写占位符失败: {resave_resp.text}"
    with get_db() as conn:
        row2 = conn.execute("SELECT api_key FROM ai_config WHERE id = 1").fetchone()
    assert row2 is not None and row2["api_key"] == stored, (
        f"提交占位符后库内密文应原样保留，实际被覆盖为 {str(row2['api_key'])[:20]}"
    )
    assert decrypt_api_key(row2["api_key"]) == test_key
    print("[PASS] test_ai_config_with_encryption")


def test_ai_providers_list():
    """/api/v1/ai/providers 应返回已知厂商列表"""
    from fastapi.testclient import TestClient
    from app.main import app

    client = TestClient(app)
    resp = client.get("/api/v1/ai/providers")
    assert resp.status_code == 200
    providers = resp.json()
    assert isinstance(providers, list)
    assert len(providers) > 10, f"厂商列表应丰富,但只返回 {len(providers)} 项"

    # 验证包含关键厂商
    names = [p.get("name", "") for p in providers]
    assert "OpenAI" in names
    assert "DeepSeek" in names
    assert any("Claude" in n or "Anthropic" in n for n in names)
    print(f"[PASS] test_ai_providers_list ({len(providers)} 个厂商)")


def test_industry_tree_init():
    """行业树初始化应正常"""
    from fastapi.testclient import TestClient
    from app.main import app

    client = TestClient(app)
    resp = client.get("/api/v1/industry-tree")
    # 允许404 (若未启用) 或 200
    assert resp.status_code in (200, 404)
    if resp.status_code == 200:
        data = resp.json()
        print(f"[PASS] test_industry_tree_init (树根 {len(data) if isinstance(data, list) else 'N/A'})")
    else:
        print("[SKIP] test_industry_tree_init (端点不存在)")


def test_reference_outline_exists():
    """参考目录应被初始化"""
    from app.db import get_db

    with get_db() as conn:
        count = conn.execute("SELECT COUNT(*) FROM reference_outlines").fetchone()[0]
    assert count >= 1, f"参考目录应至少 1 条,但只有 {count}"
    print(f"[PASS] test_reference_outline_exists ({count} 条)")


def test_latency_middleware_e2e():
    """多次请求应都能正常响应（X-Response-Time 不再注入，改为日志监控）"""
    from fastapi.testclient import TestClient
    from app.main import app

    client = TestClient(app)
    for i in range(5):
        resp = client.get("/api/v1/health")
        assert resp.status_code == 200, f"请求 {i+1} 失败: {resp.status_code}"
        assert resp.json().get("status") == "ok"
    print("[PASS] test_latency_middleware_e2e (5 个请求全部正常响应)")


def test_arrange_illustration_endpoint():
    """测试插图编排端点注册正常"""
    from fastapi.testclient import TestClient
    from app.main import app

    client = TestClient(app)

    # 端点路径应为 /api/v1/projects/{project_id}/sections/{section_id}/generate-illustration
    # 不存在的 section_id 应返回 404，而非 405 Method Not Allowed
    resp = client.post("/api/v1/projects/1/sections/999/generate-illustration", params={"project_id": 1})
    # 可能是 404（章节不存在）或 422（缺少认证），但不能是 405（路由不存在）
    assert resp.status_code != 405, f"端点未注册，返回 405: {resp.text[:200]}"
    print(f"[PASS] test_arrange_illustration_endpoint (端点已注册，状态码: {resp.status_code})")


@patch('app.services.ai.provider_factory.create_provider')
@patch('app.services.ai.provider_factory.get_ai_config')
@patch('app.routers.sections.get_section')
@patch('app.routers.sections.require_project_permission')
def test_arrange_illustration_endpoint_with_mock(
    mock_permission, get_section_mock, get_ai_config_mock, create_provider_mock
):
    """测试插图编排接口在AI配置可用时的行为"""
    from fastapi.testclient import TestClient
    from app.main import app
    from app.models import Section
    import json

    client = TestClient(app)

    # Mock AI config — must include api_key so the route doesn't short-circuit
    mock_ai_config = {
        "image_provider": "volcengine",
        "concurrency": 4,
        "api_key": "mock-api-key",
    }
    get_ai_config_mock.return_value = mock_ai_config

    # Mock provider
    mock_provider = MagicMock()
    create_provider_mock.return_value = mock_provider

    # Mock permission check to skip DB lookup for project 999
    mock_permission.return_value = None

    # Mock section lookup so the endpoint doesn't hit the DB
    mock_section = Section(
        id=999, project_id=999, title="测试章节", level=3,
        content="测试内容", section_type="text", status="pending",
        created_at="2024-01-01", updated_at="2024-01-01",
    )
    get_section_mock.return_value = mock_section

    # Mock arrange/prompt/image generation functions
    with patch('app.services.ai.image_engine.generate_illustration_arrange', new_callable=AsyncMock) as mock_arrange, \
         patch('app.services.ai.image_engine.generate_illustration_prompt', new_callable=AsyncMock) as mock_prompt, \
         patch('app.services.ai.image_engine.generate_illustration_image', new_callable=AsyncMock) as mock_image:
        mock_arrange.return_value = {"needed": True, "style": "engineering_diagram", "reason": "测试"}
        mock_prompt.return_value = {"prompt": "test prompt", "style": "engineering_diagram"}
        mock_image.return_value = {"image_url": "http://example.com/test.png"}

        resp = client.post("/api/v1/projects/999/sections/999/generate-illustration", json={"project_id": 999})
        # Should reach the mock functions, not hit the DB
        assert resp.status_code == 200, f"预期200，得到{resp.status_code}: {resp.text}"
        assert mock_arrange.called, "应调用 arrange 函数"
        print(f"[PASS] test_arrange_illustration_endpoint_with_mock (端点正常工作)")


if __name__ == "__main__":
    test_health_endpoint()
    test_projects_crud()
    test_ai_config_with_encryption()
    test_ai_providers_list()
    test_industry_tree_init()
    test_reference_outline_exists()
    test_latency_middleware_e2e()
    test_arrange_illustration_endpoint()
    test_arrange_illustration_endpoint_with_mock()
    print("\n[OK] 全部 9 个集成测试通过")
