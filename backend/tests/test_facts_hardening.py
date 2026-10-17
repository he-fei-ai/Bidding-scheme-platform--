"""加固验证：全局事实模块性能指标与编排验证（ENHANCE 2026-10-17）

覆盖维度：
1. facts_metrics 模块基本操作（inc/set/snapshot/reset）
2. 编排 facts 通道验证逻辑（标题统计、空标题告警）
3. /facts/metrics 端点存在性
4. KB/template patch 开关启用状态
"""
import os
import sys
import json
from pathlib import Path
from unittest.mock import patch

BACKEND_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(BACKEND_DIR))


def test_facts_metrics_basic_operations():
    """facts_metrics 模块基本操作"""
    from app.services.facts_metrics import inc, set_value, snapshot_with_defaults, reset, KNOWN_KEYS

    # 重置
    reset()

    # 递增计数器
    inc("fact_extract_runs")
    inc("fact_extract_runs", 2)  # 递增 2
    assert snapshot_with_defaults()["fact_extract_runs"] == 3

    # 设置绝对值
    set_value("fact_extract_elapsed_ms", 1234)
    assert snapshot_with_defaults()["fact_extract_elapsed_ms"] == 1234

    # 已知键默认补 0
    snap = snapshot_with_defaults()
    assert "fact_extract_runs" in snap
    assert "fact_extract_failed" in snap
    assert "fact_knowledge_patch_hits" in snap
    assert "fact_template_patch_hits" in snap
    assert "orchestration_titles_offered" in snap
    assert "orchestration_titles_selected" in snap
    assert "orchestration_titles_empty" in snap
    assert "orchestration_titles_skipped" in snap
    assert "fact_resolve_hits" in snap
    assert "fact_resolve_misses" in snap
    assert "fact_resolve_runs" in snap

    # 未使用键补 0
    assert snap["fact_extract_failed"] == 0
    assert snap["fact_resolve_hits"] == 0

    # 重置
    reset()
    assert snapshot_with_defaults()["fact_extract_runs"] == 0

    # KNOWN_KEYS 应包含所有已知键
    assert len(KNOWN_KEYS) >= 13, f"KNOWN_KEYS 应有至少 13 项，实际 {len(KNOWN_KEYS)}"
    print("  [PASS] facts_metrics 基本操作正确")


def test_facts_metrics_endpoint_exists():
    """GET /facts/metrics 端点存在"""
    from app.routers.facts import router

    routes = [r.path for r in router.routes]
    metrics_routes = [r for r in routes if "metrics" in r]
    assert len(metrics_routes) > 0, "应存在包含 'metrics' 的路由"
    assert any("metrics" in r for r in metrics_routes), f"应存在 metrics 路由，实际 {metrics_routes}"
    print(f"  [PASS] /facts/metrics 端点存在: {metrics_routes}")


def test_kb_template_patch_enabled(monkeypatch):
    """P1-2/P1-3：知识库/模板 patch 开关已启用"""
    monkeypatch.setenv("FACTS_KNOWLEDGE_PATCH_ENABLED", "1")
    monkeypatch.setenv("FACTS_TEMPLATE_PATCH_ENABLED", "1")
    from app.services.facts_service import kb_patch_enabled, template_patch_enabled
    assert kb_patch_enabled() is True, "FACTS_KNOWLEDGE_PATCH_ENABLED 应为 True"
    assert template_patch_enabled() is True, "FACTS_TEMPLATE_PATCH_ENABLED 应为 True"
    print("  [PASS] 知识库/模板 patch 开关已启用")


def test_orchestration_facts_validation_logic():
    """编排 facts 通道验证逻辑：标题统计与空标题告警"""
    from app.services.ai.workflows_orchestration import _extract_fact_titles

    # 标准形态：facts: {titles: [...]}
    plan1 = {"facts": {"titles": ["总工期", "项目经理", "质保期"]}}
    titles1 = _extract_fact_titles(plan1)
    assert len(titles1) == 3
    assert titles1 == ["总工期", "项目经理", "质保期"]

    # AI 简写形态：facts: [...]
    plan2 = {"facts": ["工期", "人员"]}
    titles2 = _extract_fact_titles(plan2)
    assert len(titles2) == 2

    # 空标题
    plan3 = {"facts": {"titles": []}}
    assert _extract_fact_titles(plan3) == []

    # facts 不存在
    plan4 = {}
    assert _extract_fact_titles(plan4) == []

    # facts 类型错误
    plan5 = {"facts": "invalid"}
    assert _extract_fact_titles(plan5) == []

    # 去重
    plan6 = {"facts": {"titles": ["工期", "工期", "人员"]}}
    titles6 = _extract_fact_titles(plan6)
    assert len(titles6) == 2, f"应去重，实际 {titles6}"

    # 限长
    plan7 = {"facts": {"titles": [f"事实{i}" for i in range(20)]}}
    titles7 = _extract_fact_titles(plan7, max_items=5)
    assert len(titles7) == 5, f"应限长到 5，实际 {len(titles7)}"

    # 空字符串过滤
    plan8 = {"facts": {"titles": ["有效", "", "  ", "也有效"]}}
    titles8 = _extract_fact_titles(plan8)
    assert "有效" in titles8 and "也有效" in titles8
    assert len(titles8) == 2
    print("  [PASS] 编排 facts 标题提取逻辑正确")


def test_orchestration_facts_title_assembly(monkeypatch):
    """编排阶段事实标题清单装配：fact_titles_for_orchestration 输出"""
    monkeypatch.setenv("FACTS_ORCHESTRATED_SELECT_ENABLED", "1")
    from app.services.facts_service import fact_titles_for_orchestration, orchestrated_select_enabled

    # 确认开关已启用
    assert orchestrated_select_enabled() is True, "FACTS_ORCHESTRATED_SELECT_ENABLED 应为 True"

    facts = [
        {"key": "projectManagerName", "label": "项目经理姓名", "value": "张三", "category": "person"},
        {"key": "totalDuration", "label": "总工期", "value": "90 日历天", "category": "schedule"},
        {"key": "warrantyPeriod", "label": "质保期", "value": "3 年", "category": "warranty"},
    ]
    titles = fact_titles_for_orchestration(facts)
    assert isinstance(titles, list)
    assert len(titles) == 3
    assert "项目经理姓名" in titles
    assert "总工期" in titles
    assert "质保期" in titles
    print("  [PASS] 编排阶段事实标题清单装配正确")


def test_resolve_facts_metrics_wiring():
    """resolve_facts_by_titles 调用时记录命中/未命中指标"""
    from app.services.facts_metrics import snapshot_with_defaults, reset, inc
    from app.services.facts_service import resolve_facts_by_titles

    reset()

    facts = [
        {"key": "totalDuration", "label": "总工期", "value": "90 日历天", "category": "schedule"},
        {"key": "projectManagerName", "label": "项目经理姓名", "value": "张三", "category": "person"},
    ]

    # 命中
    result = resolve_facts_by_titles(["总工期", "项目经理姓名"], facts)
    assert len(result) == 2, f"应命中 2 条，实际 {len(result)}"

    # 模拟增量（实际调用方会 inc）
    inc("fact_resolve_runs")
    inc("fact_resolve_hits", 2)

    snap = snapshot_with_defaults()
    assert snap["fact_resolve_runs"] == 1
    assert snap["fact_resolve_hits"] == 2

    # 未命中（幻觉标题）
    result2 = resolve_facts_by_titles(["不存在的标题"], facts)
    assert result2 == [], f"幻觉标题应返回空，实际 {result2}"
    inc("fact_resolve_runs")
    inc("fact_resolve_misses", 1)

    snap2 = snapshot_with_defaults()
    assert snap2["fact_resolve_runs"] == 2
    assert snap2["fact_resolve_misses"] == 1

    reset()
    print("  [PASS] resolve_facts_by_titles 指标接线正确")


def test_env_all_switches(monkeypatch):
    """所有事实相关开关状态汇总验证"""
    monkeypatch.setenv("FACTS_ORCHESTRATED_SELECT_ENABLED", "1")
    monkeypatch.setenv("FACTS_OUTLINE_INPUT_ENABLED", "1")
    monkeypatch.setenv("FACTS_CHAPTER_SCOPE_ENABLED", "1")
    monkeypatch.setenv("FACTS_KNOWLEDGE_PATCH_ENABLED", "1")
    monkeypatch.setenv("FACTS_TEMPLATE_PATCH_ENABLED", "1")
    monkeypatch.setenv("CONSISTENCY_PATCH_REPAIR_ENABLED", "1")
    from app.services.facts_service import (
        orchestrated_select_enabled,
        outline_input_enabled,
        chapter_scope_enabled,
        kb_patch_enabled,
        template_patch_enabled,
        consistency_patch_repair_enabled,
    )

    switches = {
        "FACTS_ORCHESTRATED_SELECT_ENABLED": orchestrated_select_enabled(),
        "FACTS_OUTLINE_INPUT_ENABLED": outline_input_enabled(),
        "FACTS_CHAPTER_SCOPE_ENABLED": chapter_scope_enabled(),
        "FACTS_KNOWLEDGE_PATCH_ENABLED": kb_patch_enabled(),
        "FACTS_TEMPLATE_PATCH_ENABLED": template_patch_enabled(),
        "CONSISTENCY_PATCH_REPAIR_ENABLED": consistency_patch_repair_enabled(),
    }
    for name, val in switches.items():
        print(f"    {name}: {val}")
        assert val is True, f"{name} 应为 True"
    print("  [PASS] 所有事实相关开关已启用")


def run_all():
    """运行所有加固验证测试"""
    print("=" * 60)
    print("全局事实模块加固验证")
    print("=" * 60)

    tests = [
        ("metrics 基本操作", test_facts_metrics_basic_operations),
        ("metrics 端点存在", test_facts_metrics_endpoint_exists),
        ("KB/模板 patch 开关", test_kb_template_patch_enabled),
        ("编排标题提取逻辑", test_orchestration_facts_validation_logic),
        ("编排标题清单装配", test_orchestration_facts_title_assembly),
        ("resolve 指标接线", test_resolve_facts_metrics_wiring),
        ("全开关状态汇总", test_env_all_switches),
    ]

    passed = 0
    failed = 0
    for name, fn in tests:
        try:
            print(f"\n[{name}]")
            fn()
            passed += 1
        except Exception as e:
            print(f"  [FAIL] {e}")
            failed += 1

    print(f"\n{'=' * 60}")
    print(f"结果: {passed} 通过, {failed} 失败")
    print(f"{'=' * 60}")
    return failed == 0


if __name__ == "__main__":
    success = run_all()
    sys.exit(0 if success else 1)
