"""2026-10-16 全局事实「既有方案（模板）补充 patch」回归（P2-4）。

覆盖内容（对齐易标 `runOriginalPlanGlobalFactPatches` 的「原方案保留」环节）：

  A. `facts_service.template_patch_enabled` 开关默认关/可开启/非法回退；
  B. `merge_template_patch_items` / `merge_patch_items` 合并语义：
     同 key 基线优先、基线占位让位模板实值、基线缺失追加、来源标记 template；
  C. `/facts/generate` 端到端：开启开关后基线提取 + 模板补充各一次 AI 调用，
     模板候选并入最终清单（source=template、基线值不被覆盖）；
  D. 开关默认关时完全不读模板（AI 调用次数不变、template_patches_applied=0）；
  E. 模板文件缺失/提取失败时降级跳过，不影响基线提取。

设计原则：不依赖真实 AI（可编程假 provider）；只测行为不测实现。
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from app.db import get_db, init_db
from app.config import PROJECTS_DATA_DIR


def _new_project(name: str) -> int:
    init_db()
    with get_db() as conn:
        conn.execute("INSERT INTO projects (name, industry) VALUES (?, ?)", (name, "通用"))
        return conn.execute("SELECT id FROM projects ORDER BY id DESC LIMIT 1").fetchone()["id"]


def _insert_ready_template(project_id: int, text: str) -> str:
    """插入一条 ready 状态的模板记录并写模板目录文件，返回 file_path。

    G1（2026-10-28 遗留加固）：模板补丁路径守卫要求 file_path 落在
    PROJECTS_DATA_DIR/<project_id>/templates 内（template_expansion 的写入位置）。
    原实现写系统临时目录，会被守卫按设计拒绝。现按生产契约写合规路径。
    """
    tpl_dir = PROJECTS_DATA_DIR / str(project_id) / "templates"
    tpl_dir.mkdir(parents=True, exist_ok=True)
    file_path = tpl_dir / f"tpl_{project_id}.txt"
    file_path.write_text(text, encoding="utf-8")
    with get_db() as conn:
        conn.execute(
            "INSERT INTO project_templates (project_id, filename, file_path, status) "
            "VALUES (?, ?, ?, 'ready')",
            (project_id, "历史标书模板.txt", str(file_path)),
        )
    return str(file_path)


# ==================== A. 开关 ====================


def test_template_patch_switch_default_off(monkeypatch):
    from app.services import facts_service as fs

    monkeypatch.delenv("FACTS_TEMPLATE_PATCH_ENABLED", raising=False)
    assert fs.template_patch_enabled() is False
    monkeypatch.setenv("FACTS_TEMPLATE_PATCH_ENABLED", "1")
    assert fs.template_patch_enabled() is True
    monkeypatch.setenv("FACTS_TEMPLATE_PATCH_ENABLED", "0")
    assert fs.template_patch_enabled() is False
    monkeypatch.setenv("FACTS_TEMPLATE_PATCH_ENABLED", "yes")
    assert fs.template_patch_enabled() is True


# ==================== B. 合并语义 ====================


def test_merge_template_patch_items_base_priority_and_placeholder_yield():
    """模板补充与基线合并：同 key 基线优先、基线占位让位实值、缺失追加并标记 template。"""
    from app.services.facts_service import (
        merge_kb_patch_items,
        merge_patch_items,
        merge_template_patch_items,
        normalize_fact_items,
    )

    base = [
        {"key": "scheduleDuration", "label": "建设工期", "value": "90 日历天", "category": "schedule"},
        {"key": "projectManager", "label": "项目经理", "value": "【待填写】", "category": "personnel"},
    ]
    patch = [
        {"key": "scheduleDuration", "label": "建设工期", "value": "120 日历天"},
        {"key": "projectManager", "label": "项目经理", "value": "张三"},
        {"key": "warrantyPeriod", "label": "质保期", "value": "3 年"},
    ]
    # 泛化入口的 source 参数与两个特化入口等价（均未规范化的原始拼接结果；
    # 注意 normalize_fact_items 会原地补 category，故等价性断言放在规范化之前）
    assert merge_patch_items(base, patch, source="template") == merge_template_patch_items(base, patch)
    assert merge_patch_items(base, patch, source="knowledge") == merge_kb_patch_items(base, patch)

    merged = merge_template_patch_items(base, patch)
    # 原始拼接：基线在前、补充在后，补充条目统一带 source 标记
    assert [i["key"] for i in merged] == ["scheduleDuration", "projectManager", "scheduleDuration", "projectManager", "warrantyPeriod"]
    assert all(i.get("source") == "template" for i in merged[2:])

    normalized = normalize_fact_items(merged, fallback_text="", policy="fabricate")
    by_key = {i["key"]: i for i in normalized}
    # 同 key 冲突：基线（招标）优先，不被模板覆盖
    assert by_key["scheduleDuration"]["value"] == "90 日历天"
    # 基线占位 → 模板实值替换
    assert by_key["projectManager"]["value"] == "张三"
    # 基线缺失 → 追加且标记模板来源
    assert by_key["warrantyPeriod"]["value"] == "3 年"
    assert by_key["warrantyPeriod"]["source"] == "template"
    # 模板来源条目不带 verified / needs_manual_input
    assert "verified" not in by_key["warrantyPeriod"]


def test_merge_patch_items_rejects_non_dict_patch_entries():
    from app.services.facts_service import merge_patch_items

    base = [{"key": "a", "label": "A", "value": "1"}]
    out = merge_patch_items(base, ["不是字典", None, {"key": "b", "label": "B", "value": "2"}], "template")
    assert [i["key"] for i in out] == ["a", "b"]
    assert out[1]["source"] == "template"


# ==================== C. /facts/generate 端到端 ====================


def test_generate_facts_applies_template_patch(monkeypatch):
    """开启 FACTS_TEMPLATE_PATCH_ENABLED 后，模板文件二次提取并入最终事实清单。"""
    from app.models import FactsGenerateRequest
    from app.routers import facts as facts_router
    from app.services.ai import provider_factory, workflows

    monkeypatch.setattr(
        facts_router, "get_facts_file_text", lambda _pid: "招标文件正文：工期要求 90 日历天。"
    )
    monkeypatch.setattr(facts_router, "template_patch_enabled", lambda: True)
    monkeypatch.setattr(facts_router, "kb_patch_enabled", lambda: False)
    monkeypatch.setattr(facts_router, "segmented_extraction_enabled", lambda: False)
    monkeypatch.setattr(facts_router, "outline_input_enabled", lambda: False)

    pid = _new_project("模板补充测试")
    _insert_ready_template(
        pid, "历史标书模板：本项目免费质保期为 3 年，故障 24 小时响应，项目经理张三。"
    )

    calls = {"n": 0}

    async def _fake_collect(_provider, _messages, **_kwargs):
        calls["n"] += 1
        if calls["n"] == 1:  # 第一次：基线提取
            return {"projectName": "示范工程", "scheduleDuration": "90 日历天"}
        # 第二次：模板补充提取
        return {"warrantyPeriod": "3 年", "scheduleDuration": "120 日历天"}

    monkeypatch.setattr(provider_factory, "get_ai_config", lambda: object())
    monkeypatch.setattr(provider_factory, "create_provider", lambda _cfg: object())
    monkeypatch.setattr(workflows, "collect_json_response", _fake_collect)

    result = asyncio.run(
        facts_router.generate_facts(pid, FactsGenerateRequest(missing_value_policy="fabricate"))
    )
    assert calls["n"] == 2, "基线 + 模板各一次提取"
    items = {i["key"]: i for i in result["items"]}
    # 基线缺失项被模板补齐，且标记来源
    assert items["warrantyPeriod"]["value"] == "3 年"
    assert items["warrantyPeriod"]["source"] == "template"
    # 同 key 冲突以基线为准（模板的 120 不覆盖 90）
    assert items["scheduleDuration"]["value"] == "90 日历天"
    assert result["template_patches_applied"] >= 1
    # 未启用知识库时知识库计数为 0
    assert result["knowledge_patches_applied"] == 0


def test_generate_facts_template_patch_disabled_by_default(monkeypatch):
    """开关默认关：不读模板、不产生第二次 AI 调用、template_patches_applied=0。"""
    from app.models import FactsGenerateRequest
    from app.routers import facts as facts_router
    from app.services.ai import provider_factory, workflows

    monkeypatch.setattr(
        facts_router, "get_facts_file_text", lambda _pid: "招标文件正文：工期要求 90 日历天。"
    )
    monkeypatch.setattr(facts_router, "template_patch_enabled", lambda: False)
    monkeypatch.setattr(facts_router, "kb_patch_enabled", lambda: False)
    monkeypatch.setattr(facts_router, "segmented_extraction_enabled", lambda: False)
    monkeypatch.setattr(facts_router, "outline_input_enabled", lambda: False)

    pid = _new_project("模板开关关闭测试")
    _insert_ready_template(pid, "历史标书模板：质保期 3 年。")

    calls = {"n": 0}

    async def _fake_collect(_provider, _messages, **_kwargs):
        calls["n"] += 1
        return {"projectName": "示范工程", "scheduleDuration": "90 日历天"}

    monkeypatch.setattr(provider_factory, "get_ai_config", lambda: object())
    monkeypatch.setattr(provider_factory, "create_provider", lambda _cfg: object())
    monkeypatch.setattr(workflows, "collect_json_response", _fake_collect)

    result = asyncio.run(
        facts_router.generate_facts(pid, FactsGenerateRequest(missing_value_policy="fabricate"))
    )
    assert calls["n"] == 1, "默认关时只有基线一次提取"
    assert result["template_patches_applied"] == 0
    assert all(i.get("source") != "template" for i in result["items"])


def test_generate_facts_template_missing_file_degrades(monkeypatch):
    """模板文件缺失时降级跳过，基线提取不受影响（不抛错、不二次调用）。"""
    from app.models import FactsGenerateRequest
    from app.routers import facts as facts_router
    from app.services.ai import provider_factory, workflows

    monkeypatch.setattr(
        facts_router, "get_facts_file_text", lambda _pid: "招标文件正文：工期要求 90 日历天。"
    )
    monkeypatch.setattr(facts_router, "template_patch_enabled", lambda: True)
    monkeypatch.setattr(facts_router, "kb_patch_enabled", lambda: False)
    monkeypatch.setattr(facts_router, "segmented_extraction_enabled", lambda: False)
    monkeypatch.setattr(facts_router, "outline_input_enabled", lambda: False)

    pid = _new_project("模板缺失降级测试")
    # G1：指向模板目录内的不存在文件，保持「提取失败降级」语义（路径在守卫内）
    missing_path = str(PROJECTS_DATA_DIR / str(pid) / "templates" / f"missing_{pid}.txt")
    with get_db() as conn:
        conn.execute(
            "INSERT INTO project_templates (project_id, filename, file_path, status) "
            "VALUES (?, ?, ?, 'ready')",
            (pid, "不存在的模板.txt", missing_path),
        )

    calls = {"n": 0}

    async def _fake_collect(_provider, _messages, **_kwargs):
        calls["n"] += 1
        return {"projectName": "示范工程", "scheduleDuration": "90 日历天"}

    monkeypatch.setattr(provider_factory, "get_ai_config", lambda: object())
    monkeypatch.setattr(provider_factory, "create_provider", lambda _cfg: object())
    monkeypatch.setattr(workflows, "collect_json_response", _fake_collect)

    result = asyncio.run(
        facts_router.generate_facts(pid, FactsGenerateRequest(missing_value_policy="fabricate"))
    )
    # 提取失败 → 无素材 → 不发起第二次 AI 调用，基线结果完整
    assert calls["n"] == 1
    assert result["template_patches_applied"] == 0
    items = {i["key"]: i for i in result["items"]}
    assert items["scheduleDuration"]["value"] == "90 日历天"


def test_generate_facts_template_path_outside_dir_guard(monkeypatch):
    """G1（2026-10-28 遗留加固）：模板 file_path 在项目模板目录外 → fail-closed 跳过。

    DB 被手工改写/历史数据存任意路径（用户绝对路径、UNC、系统临时目录）时，
    路径守卫必须拒绝读取（SSRF/越权防护），不发起模板补丁 AI 调用、不报错，
    基线提取不受影响。
    """
    import tempfile

    from app.models import FactsGenerateRequest
    from app.routers import facts as facts_router
    from app.services.ai import provider_factory, workflows

    monkeypatch.setattr(
        facts_router, "get_facts_file_text", lambda _pid: "招标文件正文：工期要求 90 日历天。"
    )
    monkeypatch.setattr(facts_router, "template_patch_enabled", lambda: True)
    monkeypatch.setattr(facts_router, "kb_patch_enabled", lambda: False)
    monkeypatch.setattr(facts_router, "segmented_extraction_enabled", lambda: False)
    monkeypatch.setattr(facts_router, "outline_input_enabled", lambda: False)

    pid = _new_project("模板路径守卫测试")
    # 把模板写在系统临时目录（目录外、真实存在、内容合法）——守卫应拒绝
    outside_dir = Path(tempfile.gettempdir()) / "eb_facts_tpl_guard"
    outside_dir.mkdir(parents=True, exist_ok=True)
    outside_path = outside_dir / f"evil_{pid}.txt"
    outside_path.write_text("历史标书模板：质保期 3 年。", encoding="utf-8")
    with get_db() as conn:
        conn.execute(
            "INSERT INTO project_templates (project_id, filename, file_path, status) "
            "VALUES (?, ?, ?, 'ready')",
            (pid, "越权模板.txt", str(outside_path)),
        )

    calls = {"n": 0}

    async def _fake_collect(_provider, _messages, **_kwargs):
        calls["n"] += 1
        return {"projectName": "示范工程", "scheduleDuration": "90 日历天"}

    monkeypatch.setattr(provider_factory, "get_ai_config", lambda: object())
    monkeypatch.setattr(provider_factory, "create_provider", lambda _cfg: object())
    monkeypatch.setattr(workflows, "collect_json_response", _fake_collect)

    result = asyncio.run(
        facts_router.generate_facts(pid, FactsGenerateRequest(missing_value_policy="fabricate"))
    )
    # 守卫拒绝目录外路径 → 无模板素材 → 只有基线一次 AI 调用
    assert calls["n"] == 1, "目录外模板必须被跳过，不发起模板补丁调用"
    assert result["template_patches_applied"] == 0
    items = {i["key"]: i for i in result["items"]}
    assert items["scheduleDuration"]["value"] == "90 日历天"
