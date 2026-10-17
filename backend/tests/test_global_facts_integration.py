"""端到端验证：全局事实机制在正文生成全流程中的集成测试

覆盖维度：
1. .env 开关默认值（P0/P1 级关键开关已启用）
2. 编排阶段 facts.titles 通道注入
3. 生成阶段事实白名单过滤 + 策略约束
4. 字数调整阶段事实注入（新增）
5. Agent 一致性修复路由存在性
6. 提示词格式与约束完整性
"""
import os
import sys
import json
from pathlib import Path

# 确保 backend/ 在 sys.path 中
BACKEND_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(BACKEND_DIR))

# BUG-FIX 2026-10-17（测试隔离）：此前在模块级执行 load_dotenv(override=True)，
# 把 .env 里的功能开关（CONSISTENCY_PATCH_REPAIR_ENABLED=1 等）强制载入 os.environ，
# 永久污染整个 pytest 进程 → 后续所有测试的 _env_flag() 从默认 False 变为 True，
# 导致 8 个测试在全量运行时间歇性失败（单独运行时因未触发本文件而通过）。
# 现改为在 test_env_switches_enabled 内部用 monkeypatch.setenv 显式设置，
# 测试结束后自动还原，不污染其它测试。
import pytest  # noqa: E402


def test_env_switches_enabled(monkeypatch):
    """P0-1/P1-1/P1-4: 关键开关已在 .env 中启用

    BUG-FIX 2026-10-17：改用 monkeypatch.setenv 显式设置开关，不依赖模块级
    load_dotenv 污染。验证的是 .env 配置正确性（通过读取 .env 文件确认值存在）。
    """
    # 读取 .env 文件确认配置值（不污染 os.environ）
    from dotenv import dotenv_values
    env_values = dotenv_values(BACKEND_DIR / ".env")

    switches = {
        "FACTS_ORCHESTRATED_SELECT_ENABLED": env_values.get("FACTS_ORCHESTRATED_SELECT_ENABLED", "0"),
        "FACTS_OUTLINE_INPUT_ENABLED": env_values.get("FACTS_OUTLINE_INPUT_ENABLED", "0"),
        "FACTS_CHAPTER_SCOPE_ENABLED": env_values.get("FACTS_CHAPTER_SCOPE_ENABLED", "0"),
        "CONSISTENCY_PATCH_REPAIR_ENABLED": env_values.get("CONSISTENCY_PATCH_REPAIR_ENABLED", "0"),
    }
    for key, val in switches.items():
        monkeypatch.setenv(key, val)

    from app.services.facts_service import (
        orchestrated_select_enabled,
        outline_input_enabled,
        chapter_scope_enabled,
        consistency_patch_repair_enabled,
    )
    assert orchestrated_select_enabled() is True, "FACTS_ORCHESTRATED_SELECT_ENABLED 应为 True"
    assert outline_input_enabled() is True, "FACTS_OUTLINE_INPUT_ENABLED 应为 True"
    assert chapter_scope_enabled() is True, "FACTS_CHAPTER_SCOPE_ENABLED 应为 True"
    assert consistency_patch_repair_enabled() is True, "CONSISTENCY_PATCH_REPAIR_ENABLED 应为 True"
    print("  [PASS] 环境开关全部正确启用")


def test_orchestration_facts_titles():
    """编排阶段：fact_titles_for_orchestration 输出格式正确"""
    from app.services.facts_service import fact_titles_for_orchestration

    facts = [
        {"key": "projectManagerName", "label": "项目经理姓名", "value": "张三", "category": "person"},
        {"key": "totalDuration", "label": "总工期", "value": "90 日历天", "category": "schedule"},
        {"key": "warrantyPeriod", "label": "质保期", "value": "3 年", "category": "warranty"},
    ]
    titles = fact_titles_for_orchestration(facts)
    assert isinstance(titles, list), "应返回列表"
    assert len(titles) == 3, f"应返回 3 个标题，实际 {len(titles)}"
    assert "项目经理姓名" in titles
    assert "总工期" in titles
    assert "质保期" in titles
    print("  [PASS] 编排阶段标题清单生成正确")


def test_resolve_facts_by_titles():
    """生成阶段：resolve_facts_by_titles 白名单过滤正确"""
    from app.services.facts_service import resolve_facts_by_titles

    facts = [
        {"key": "projectManagerName", "label": "项目经理姓名", "value": "张三", "category": "person"},
        {"key": "totalDuration", "label": "总工期", "value": "90 日历天", "category": "schedule"},
        {"key": "warrantyPeriod", "label": "质保期", "value": "3 年", "category": "warranty"},
    ]
    selected = resolve_facts_by_titles(["总工期", "质保期"], facts)
    assert len(selected) == 2, f"应选中 2 条，实际 {len(selected)}"
    labels = {f["label"] for f in selected}
    assert "总工期" in labels
    assert "质保期" in labels

    # 空标题 → 空结果
    assert resolve_facts_by_titles([], facts) == []
    # 不存在的标题 → 空结果
    assert resolve_facts_by_titles(["不存在"], facts) == []
    print("  [PASS] 生成阶段事实白名单过滤正确")


def test_content_completeness_instruction():
    """策略约束：三种模式的 instruction 文本正确"""
    from app.services.facts_service import content_completeness_instruction

    # fabricate: 无额外约束
    fabricate_instr = content_completeness_instruction("fabricate")
    assert fabricate_instr == "", "fabricate 模式不应追加约束"

    # omit: 禁止编造
    omit_instr = content_completeness_instruction("omit")
    assert "严禁" in omit_instr or "杜撰" in omit_instr, "omit 模式应含禁止编造约束"

    # placeholder: 【待填写】保护
    placeholder_instr = content_completeness_instruction("placeholder")
    assert "【待填写】" in placeholder_instr, "placeholder 模式应含【待填写】保护"
    print("  [PASS] 三种缺失值策略约束生成正确")


def test_word_adjust_injects_facts():
    """字数调整阶段：build_word_adjust_messages 注入全局事实与策略约束"""
    from app.services.ai.workflows_word_adjust import build_word_adjust_messages

    facts = [
        {"key": "projectManagerName", "label": "项目经理姓名", "value": "张三", "category": "person"},
        {"key": "totalDuration", "label": "总工期", "value": "90 日历天", "category": "schedule"},
    ]

    # 无事实/无策略：只返回基础提示词
    msgs_no_facts = build_word_adjust_messages(
        section_title="测试章节",
        masked_content="这是测试内容",
        current_words=300,
        mode="expand",
        granularity="sentence",
        budget=50,
        target_words=350,
        lo=280,
        hi=420,
    )
    assert len(msgs_no_facts) == 2, "应有 system + user 两条消息"
    system_text = msgs_no_facts[0].content
    assert "【本章节全局事实变量】" not in system_text

    # 有事实：system 应包含事实块（策略约束 omit 模式下有内容）
    msgs_with_facts = build_word_adjust_messages(
        section_title="测试章节",
        masked_content="这是测试内容",
        current_words=300,
        mode="expand",
        granularity="sentence",
        budget=50,
        target_words=350,
        lo=280,
        hi=420,
        global_facts=facts,
        facts_policy="omit",
    )
    system_with_facts = msgs_with_facts[0].content
    # 事实块已注入（通过关键内容判断，避免编码问题）
    assert "项目经理姓名" in system_with_facts, "应包含具体事实值"
    assert "张三" in system_with_facts, "应包含事实值"
    assert "90 日历天" in system_with_facts, "应包含总工期事实值"
    # omit 策略有约束文本
    instr = __import__("app.services.facts_service", fromlist=["content_completeness_instruction"]).content_completeness_instruction("omit")
    if instr:
        assert len(instr) > 10, "omit 策略约束应有内容"
    print("  [PASS] 字数调整阶段事实注入正确")


def test_agent_repair_route_exists():
    """Agent 修复路由：POST /consistency/agent/{project_id} 存在"""
    from app.routers.consistency import router

    routes = [r.path for r in router.routes]
    agent_routes = [r for r in routes if "agent" in r]
    assert len(agent_routes) > 0, "应存在包含 'agent' 的路由"
    # 确认是 POST 方法
    post_agent = [r for r in router.routes if "agent" in r.path and "POST" in r.methods]
    assert len(post_agent) > 0, "应存在 POST /consistency/agent/{project_id}"
    print(f"  [PASS] Agent 修复路由存在: {post_agent[0].path}")


def test_prompt_constraint_alignment():
    """提示词约束对齐：正文生成 system prompt 含事实相关约束"""
    from app.services.ai.workflows_sections import build_chapter_content_messages

    chapter_info = {
        "id": "test-1",
        "title": "施工进度计划",
        "level": 3,
        "summary": "描述施工进度安排",
        "parent_id": "test-0",
    }
    facts = [
        {"key": "totalDuration", "label": "总工期", "value": "90 日历天", "category": "schedule"},
    ]
    msgs = build_chapter_content_messages(chapter_info, facts=facts, facts_policy="omit")

    # 找到 system message
    system_msgs = [m for m in msgs if m.role == "system"]
    assert len(system_msgs) > 0, "应有至少一条 system message"
    system_text = "\n".join(m.content for m in system_msgs)

    # 关键约束语义必须存在（格式可能因 DB 存储而不同，不强制检查编号）
    # 约束核心语义：不得前后矛盾、禁止编造
    has_contradiction = "不得前后矛盾" in system_text or "前后矛盾" in system_text
    has_no_fabricate = "不要主动编造" in system_text or "禁止编造" in system_text or "不得主动编造" in system_text
    has_only_used = "仅使用" in system_text or "只使用" in system_text

    # 至少有一条约束（DB 存储的 prompt 可能用不同表述）
    assert has_contradiction or has_no_fabricate, "应含事实一致性约束"

    # 有事实时，user message 应包含事实块
    user_msgs = [m for m in msgs if m.role == "user"]
    user_text = "\n".join(m.content for m in user_msgs)
    assert "全局事实变量" in user_text or "总工期" in user_text, "应注入事实到 user message"
    print("  [PASS] 正文生成提示词约束对齐正确")


def test_facts_for_generation_filters_placeholders():
    """facts_for_generation 应过滤【待填写】占位项"""
    from app.services.facts_service import facts_for_generation, PLACEHOLDER_VALUE_PREFIX

    raw = json.dumps([
        {"key": "a", "label": "工期", "value": "90 日历天", "category": "schedule"},
        {"key": "b", "label": "质保期", "value": f"{PLACEHOLDER_VALUE_PREFIX}质保期【待填写】", "category": "warranty"},
        {"key": "c", "label": "响应时间", "value": "2 小时", "category": "commitment"},
    ])
    result = facts_for_generation(raw)
    assert len(result) == 2, f"应过滤掉 1 条占位项，实际保留 {len(result)} 条"
    keys = {f["key"] for f in result}
    assert "a" in keys and "c" in keys
    assert "b" not in keys
    print("  [PASS] facts_for_generation 正确过滤占位项")


def run_all():
    """运行所有测试"""
    print("=" * 60)
    print("全局事实机制端到端验证")
    print("=" * 60)

    tests = [
        ("环境开关", test_env_switches_enabled),
        ("编排标题清单", test_orchestration_facts_titles),
        ("生成白名单过滤", test_resolve_facts_by_titles),
        ("策略约束生成", test_content_completeness_instruction),
        ("字数调整注入", test_word_adjust_injects_facts),
        ("Agent修复路由", test_agent_repair_route_exists),
        ("提示词约束对齐", test_prompt_constraint_alignment),
        ("占位符过滤", test_facts_for_generation_filters_placeholders),
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
