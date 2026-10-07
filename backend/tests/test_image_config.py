#!/usr/bin/env python3
"""图像模型配置模块单元测试

覆盖：
- build_endpoint URL 拼接防御（防 /images/generations 双路径）
- load_image_config 返回字段与 _get_adapter 读取字段对齐（image_ 前缀）
- ImageGenerationService 生成链路使用图像专用配置
- 主平台失败后降级成功时返回成功响应（不再隐式 None）
- ImageTask 模型对数据库 INTEGER project_id 兼容
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.routers.images import ImageTask
from app.services.ai.image_providers.base import build_endpoint
from app.services.ai.image_providers.service import ImageGenerationService, load_image_config


# ─────────────────────────────────────────────────────────────
# build_endpoint
# ─────────────────────────────────────────────────────────────
def test_build_endpoint_normal():
    url = build_endpoint("https://api.agnes-ai.cn/v1", "images", "generations")
    assert url == "https://api.agnes-ai.cn/v1/images/generations"


def test_build_endpoint_strips_generations_suffix():
    """用户误填完整端点时不应双路径拼接"""
    url = build_endpoint("https://api.agnes-ai.cn/v1/images/generations", "images", "generations")
    assert url == "https://api.agnes-ai.cn/v1/images/generations"


def test_build_endpoint_strips_dashscope_suffix():
    url = build_endpoint("https://api.agnes-ai.cn/v1/dashscope/images/generations", "models")
    assert url == "https://api.agnes-ai.cn/v1/models"


def test_build_endpoint_empty():
    assert build_endpoint("") == ""
    assert build_endpoint(None, "models") == ""


# ─────────────────────────────────────────────────────────────
# load_image_config
# ─────────────────────────────────────────────────────────────
def test_load_image_config_returns_image_prefix_keys():
    """返回的 key 必须带 image_ 前缀，且与 _get_adapter 读取字段一致"""
    cfg = load_image_config()
    for key in (
        "image_provider", "image_model", "image_base_url", "image_api_key_enc",
        "image_max_concurrency", "image_max_per_project", "image_quality",
        "image_style_injection", "image_enabled", "image_default_size",
        "image_default_ratio", "image_price_per_image",
    ):
        assert key in cfg, f"缺少配置字段: {key}"
    assert cfg.get("image_provider") in ("agnes", "dashscope", "volcengine", "hunyuan", "zhipu", "stepfun", "baidu")


def test_load_image_config_falls_back_on_db_error():
    """数据库异常时兜底返回默认配置（不抛出）"""
    with patch("app.db.get_db_with_retry", side_effect=RuntimeError("db down")):
        cfg = load_image_config(1)
        assert cfg["image_provider"] == "agnes"


# ─────────────────────────────────────────────────────────────
# 假适配器
# ─────────────────────────────────────────────────────────────
class FakeImageAdapter:
    """可配置成功/失败的假图像适配器"""

    provider_id = "fake"
    default_model = "fake-model"
    created_configs = []

    def __init__(self, config: dict):
        self.config = config
        FakeImageAdapter.created_configs.append(config)
        self.call_kwargs = None
        self.supported_sizes = ["1K", "2K"]

    async def generate(self, prompt, model=None, size=None, ratio=None, reference_images=None, **kwargs):
        self.call_kwargs = {"prompt": prompt, "model": model, "size": size, "ratio": ratio}
        if self.config.get("_fail"):
            return {"success": False, "error": "模拟失败", "provider": self.config.get("_provider", "x")}
        return {
            "success": True,
            "image_url": "https://img.example.com/gen.png",
            "image_base64": None,
            "cost": 0.1,
            "provider": self.config.get("_provider", "agnes"),
            "model": model or "m",
            "error": None,
        }


class FakeImageFailedAdapter(FakeImageAdapter):
    pass


class FakeDB:
    """最小化 SQLite 连接替身（execute + commit）"""

    def __init__(self):
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        return MagicMock()

    def commit(self):
        pass


IMAGE_CFG = {
    "image_provider": "agnes",
    "image_model": "agnes-image-2.1-flash",
    "image_base_url": "https://api.agnes-ai.cn/v1",
    "image_api_key_enc": "sk-image-secret-key",
    "image_max_concurrency": 2,
    "image_max_per_project": 10,
    "image_quality": "standard",
    "image_style_injection": None,
    "image_enabled": True,
    "image_default_size": "2K",
    "image_default_ratio": "1:1",
    "image_price_per_image": 0.0,
}


# ─────────────────────────────────────────────────────────────
# ImageGenerationService.generate 使用图像配置
# ─────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_service_generate_uses_image_config_key():
    """生成链路必须把图像配置的 api_key/base_url/model 传给适配器"""
    import app.services.ai.image_providers.service as svc_mod
    FakeImageAdapter.created_configs = []

    with patch.object(svc_mod, "load_image_config", return_value=dict(IMAGE_CFG)), \
         patch.object(svc_mod.ProviderRegistry, "_adapters", {"agnes": FakeImageAdapter}), \
         patch.object(svc_mod.ImageGenerationService, "_download_image", new=AsyncMock(return_value=None)):
        db = FakeDB()
        service = ImageGenerationService(db, config_service=None)
        result = await service.generate(
            {"prompt": "测试图", "project_id": "1"}, 1
        )

    assert result["success"] is True, f"生成应成功: {result}"
    assert FakeImageAdapter.created_configs, "应该创建过适配器"
    adapter_cfg = FakeImageAdapter.created_configs[0]
    assert adapter_cfg["api_key"] == "sk-image-secret-key", "应使用图像配置的 API Key(而非文本模型key)"
    assert adapter_cfg["base_url"] == "https://api.agnes-ai.cn/v1"
    assert adapter_cfg["model"] == "agnes-image-2.1-flash"


@pytest.mark.asyncio
async def test_service_generate_fallback_returns_success():
    """主平台失败 → 降级 agnes 成功 → 必须返回 success（修复隐式 None）"""
    import app.services.ai.image_providers.service as svc_mod

    cfg = dict(IMAGE_CFG)
    cfg["image_provider"] = "dashscope"  # 主配置平台会失败

    # dashscope 适配器配置 _fail=True；agnes 正常
    def _make_adapter(config):
        if config.get("_provider") == "dashscope":
            config["_fail"] = True
        return FakeImageAdapter(config)

    with patch.object(svc_mod, "load_image_config", return_value=cfg), \
         patch.object(
             svc_mod.ProviderRegistry, "_adapters",
             {"dashscope": lambda c: _make_adapter({**c, "_provider": "dashscope"}),
              "agnes": lambda c: _make_adapter({**c, "_provider": "agnes"})},
         ), \
         patch.object(svc_mod.ImageGenerationService, "_download_image", new=AsyncMock(return_value=None)):
        db = FakeDB()
        service = ImageGenerationService(db, config_service=None)
        result = await service.generate(
            {"prompt": "降级测试", "project_id": "2"}, 1
        )

    assert result["success"] is True, f"降级后应返回成功: {result}"
    assert result["provider"] == "agnes"


# ─────────────────────────────────────────────────────────────
# ImageTask 模型
# ─────────────────────────────────────────────────────────────
def test_image_task_accepts_int_project_id():
    """数据库 project_id 为 INTEGER，模型必须兼容"""
    task = ImageTask(
        id="img_abc12345",
        project_id=5,
        chapter_id=None,
        provider="agnes",
        model="agnes-image-2.1-flash",
        prompt="测试",
        status="success",
        created_at="2026-08-19 10:00:00",
        image_url="http://img/x.png",
        cost=0.0,
        image_path="data/images/5/img_abc12345.png",
    )
    assert task.project_id == 5
    assert task.image_path.endswith(".png")


# ─────────────────────────────────────────────────────────────
# 旧表结构迁移
# ─────────────────────────────────────────────────────────────
def test_migration_upgrades_old_image_tasks_schema(tmp_path, monkeypatch):
    """早期版本创建的 image_generation_tasks 表缺少 6 列，迁移必须补齐

    回归场景：生产库在旧版代码下已建表（只有 section_index 没有 chapter_id 等），
    新版代码 INSERT/SELECT 使用新列会失败；ensure_enhancement_tables() 需
    ALTER TABLE 逐列补齐，保证任务记录可写入、任务列表可查询。
    """
    from contextlib import contextmanager
    import sqlite3

    from app.db_migrations import ensure_enhancement_tables

    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(db_path)
    # 生产环境 init_db 先建 ai_config 再跑 ensure_enhancement_tables，
    # 测试临时库需补齐（迁移第 15/17 步读取 api_key/model/base_url）
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ai_config (
            id INTEGER PRIMARY KEY,
            api_key TEXT,
            model TEXT,
            base_url TEXT
        )
    """)
    conn.execute("INSERT OR IGNORE INTO ai_config (id, api_key, model, base_url) VALUES (1, '', '', '')")
    # 旧结构：模拟早期版本 schema
    conn.execute("""
        CREATE TABLE IF NOT EXISTS image_generation_tasks (
            id TEXT PRIMARY KEY,
            project_id INTEGER,
            section_index TEXT,
            provider TEXT NOT NULL,
            model TEXT NOT NULL,
            prompt TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            image_url TEXT,
            error_message TEXT,
            cost REAL,
            created_at TEXT,
            completed_at TEXT
        )
    """)
    conn.commit()
    conn.close()

    @contextmanager
    def fake_get_db():
        c = sqlite3.connect(db_path)
        c.row_factory = sqlite3.Row  # 与生产 get_db 保持一致，迁移内按列名取值
        try:
            yield c
            c.commit()
        finally:
            c.close()

    monkeypatch.setattr("app.db_migrations.get_db", fake_get_db)
    ensure_enhancement_tables()

    conn = sqlite3.connect(db_path)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(image_generation_tasks)").fetchall()]
    conn.close()
    for col in ("chapter_id", "injected_prompt", "size", "ratio", "image_path", "retry_count"):
        assert col in cols, f"迁移后缺少列: {col}"
    # 旧列不应丢失
    assert "section_index" in cols


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
