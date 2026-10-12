"""性能基准测试 — 2026-10-12 新增模块。

运行：
    cd backend
    python -m pytest tests/test_2026_10_12_performance.py -v -s
"""

from __future__ import annotations

import time
import pytest

from app.services.ai.outline_numbering import (
    NumberingTemplate,
    default_numbering_template,
    renumber_outline_with_template,
    to_chinese,
)
from app.services.ai.hazard_projects import (
    HAZARD_PROJECTS,
    scan_hazard_projects,
    build_hazard_outline_sections,
)
from app.services.ai.json_repair import (
    quick_extract_json,
    validate_outline_json,
    validate_content_json,
)
from app.services.ai.mermaid_repair import (
    check_mermaid_syntax,
    extract_mermaid_blocks,
)
from app.services.ai.illustration_planning import (
    plan_illustrations,
    plan_outline_illustrations,
    count_illustrations,
)
from app.services.ai.knowledge_refill import (
    KnowledgeItem,
    split_text_into_batches,
    merge_knowledge_items,
    _normalize_title,
    _is_duplicate,
)
from app.utils.text_edit import (
    apply_text_edit,
    apply_text_edits,
    find_all_occurrences,
    replace_range,
)


# ========== 测试数据 ==========

LARGE_BID_TEXT = """
深基坑工程是建筑施工中的危险性较大的分部分项工程，基坑支护采用排桩加内支撑方案。
基坑深度 8 米，采用地下连续墙作为支护结构。
基坑开挖需分层次进行，每次开挖深度不超过 2 米。
基坑变形监测采用自动化监测系统，监测频率为每天一次。
降水采用管井降水方案，降水深度需满足基坑开挖要求。

本项目包含高大模板工程，模板支撑体系采用满堂支撑架。
模板支撑高度为 5 米，立杆间距为 1.2 米。
剪刀撑设置在模板支撑体系四周，间距不超过 5 米。
模板安装与拆除需按照专项施工方案执行。

脚手架采用盘扣式脚手架，外立面脚手架高度为 30 米。
连墙件间距为 2 步 3 跨，扫地杆设置在脚手架底部。
脚手板采用钢脚手板，铺设严密。
脚手架验收需由专业人员进行。

起重吊装采用塔吊，起重量为 10 吨。
吊装作业需编制专项施工方案，经专家论证。
吊具与索具需定期检查，确保安全可靠。
吊装作业现场需设置警戒区域。

施工现场临时用电采用 TN-S 系统，三级配电两级保护。
配电箱采用标准配电箱，电缆采用铜芯电缆。
电缆敷设需穿管保护，避免机械损伤。
照明系统采用 LED 灯具，节能高效。

施工现场消防采用临时消防设施，灭火器配置满足规范要求。
消防通道保持畅通，宽度不小于 4 米。
动火作业需办理动火证，配备看火人员。
易燃易爆物品单独存放，远离火源。
"""

LARGE_OUTLINE = [
    {"id": "1", "title": "项目概述", "children": [
        {"id": "1.1", "title": "项目背景"},
        {"id": "1.2", "title": "工程范围"},
    ]},
    {"id": "2", "title": "施工部署", "children": [
        {"id": "2.1", "title": "施工准备"},
        {"id": "2.2", "title": "资源配置"},
    ]},
    {"id": "3", "title": "主要施工方法", "children": [
        {"id": "3.1", "title": "深基坑工程"},
        {"id": "3.2", "title": "模板工程"},
        {"id": "3.3", "title": "脚手架工程"},
    ]},
    {"id": "4", "title": "安全文明施工", "children": [
        {"id": "4.1", "title": "安全管理"},
        {"id": "4.2", "title": "文明施工"},
    ]},
]

VALID_MERMAID = """flowchart TD
    A[开始] --> B[准备工作]
    B --> C[基坑开挖]
    C --> D[支护施工]
    D --> E[完成]
"""

INVALID_MERMAID = """flowchart TD
    A[开始 --> B[准备工作]
    B --> C[基坑开挖
    C --> D[支护施工]
    D --> E[完成]
"""

VALID_JSON = '{"outline": [{"id": "1", "title": "第一章", "children": []}]}'
INVALID_JSON = '{"outline": [{"id": "1", "title": "第一章", "children": []}}'
JSON_WITH_FENCE = """```json
{"outline": [{"id": "1", "title": "第一章", "children": []}]}
```"""

KNOWLEDGE_ITEMS = [
    KnowledgeItem(id="1", title="项目概述", content="本项目为建筑工程项目", category="项目信息"),
    KnowledgeItem(id="2", title="深基坑工程", content="基坑深度 8 米", category="技术要求"),
    KnowledgeItem(id="3", title="模板工程", content="模板支撑高度 5 米", category="技术要求"),
]


class TestOutlineNumberingPerformance:
    def test_render_path_performance(self):
        template = default_numbering_template()
        start = time.perf_counter()
        for i in range(1000):
            template.render_path([1, i % 10, i % 20])
        elapsed = time.perf_counter() - start
        print(f"\n[PERF] render_path x1000: {elapsed*1000:.2f}ms")
        assert elapsed < 0.1

    def test_renumber_outline_performance(self):
        template = default_numbering_template()
        start = time.perf_counter()
        for _ in range(100):
            renumber_outline_with_template(LARGE_OUTLINE, template, mutate=False)
        elapsed = time.perf_counter() - start
        print(f"\n[PERF] renumber_outline x100: {elapsed*1000:.2f}ms")
        assert elapsed < 1.0

    def test_to_chinese_performance(self):
        start = time.perf_counter()
        for i in range(1, 10001):
            to_chinese(i)
        elapsed = time.perf_counter() - start
        print(f"\n[PERF] to_chinese x10000: {elapsed*1000:.2f}ms")
        assert elapsed < 0.5


class TestHazardProjectsPerformance:
    def test_scan_performance_small_text(self):
        text = "本项目包含深基坑工程和高大模板工程。"
        start = time.perf_counter()
        for _ in range(1000):
            scan_hazard_projects(text)
        elapsed = time.perf_counter() - start
        print(f"\n[PERF] scan_hazard (small) x1000: {elapsed*1000:.2f}ms")
        assert elapsed < 1.0

    def test_scan_performance_large_text(self):
        start = time.perf_counter()
        for _ in range(100):
            scan_hazard_projects(LARGE_BID_TEXT)
        elapsed = time.perf_counter() - start
        print(f"\n[PERF] scan_hazard (large) x100: {elapsed*1000:.2f}ms")
        assert elapsed < 5.0

    def test_build_sections_performance(self):
        result = scan_hazard_projects(LARGE_BID_TEXT)
        start = time.perf_counter()
        for _ in range(100):
            build_hazard_outline_sections(result.detected)
        elapsed = time.perf_counter() - start
        print(f"\n[PERF] build_hazard_sections x100: {elapsed*1000:.2f}ms")
        assert elapsed < 0.5


class TestJsonRepairPerformance:
    def test_quick_extract_valid_json(self):
        start = time.perf_counter()
        for _ in range(10000):
            result, error = quick_extract_json(VALID_JSON)
            assert result is not None
        elapsed = time.perf_counter() - start
        print(f"\n[PERF] quick_extract_json (valid) x10000: {elapsed*1000:.2f}ms")
        assert elapsed < 1.0

    def test_quick_extract_with_fence(self):
        start = time.perf_counter()
        for _ in range(10000):
            result, error = quick_extract_json(JSON_WITH_FENCE)
            assert result is not None
        elapsed = time.perf_counter() - start
        print(f"\n[PERF] quick_extract_json (with fence) x10000: {elapsed*1000:.2f}ms")
        assert elapsed < 2.0

    def test_quick_extract_invalid_json(self):
        start = time.perf_counter()
        for _ in range(10000):
            result, error = quick_extract_json(INVALID_JSON)
            assert result is None
        elapsed = time.perf_counter() - start
        print(f"\n[PERF] quick_extract_json (invalid) x10000: {elapsed*1000:.2f}ms")
        assert elapsed < 2.0

    def test_validate_outline_performance(self):
        data = {"outline": LARGE_OUTLINE}
        start = time.perf_counter()
        for _ in range(1000):
            errors = validate_outline_json(data)
        elapsed = time.perf_counter() - start
        print(f"\n[PERF] validate_outline x1000: {elapsed*1000:.2f}ms")
        assert elapsed < 0.5


class TestMermaidRepairPerformance:
    def test_check_syntax_valid(self):
        start = time.perf_counter()
        for _ in range(10000):
            error = check_mermaid_syntax(VALID_MERMAID)
            assert error is None
        elapsed = time.perf_counter() - start
        print(f"\n[PERF] check_mermaid_syntax (valid) x10000: {elapsed*1000:.2f}ms")
        assert elapsed < 0.5

    def test_check_syntax_invalid(self):
        start = time.perf_counter()
        for _ in range(10000):
            error = check_mermaid_syntax(INVALID_MERMAID)
            assert error is not None
        elapsed = time.perf_counter() - start
        print(f"\n[PERF] check_mermaid_syntax (invalid) x10000: {elapsed*1000:.2f}ms")
        assert elapsed < 0.5

    def test_extract_blocks_performance(self):
        text = f"```mermaid\n{VALID_MERMAID}\n```\n\n正文内容。\n\n```mermaid\n{VALID_MERMAID}\n```"
        start = time.perf_counter()
        for _ in range(10000):
            blocks = extract_mermaid_blocks(text)
            assert len(blocks) == 2
        elapsed = time.perf_counter() - start
        print(f"\n[PERF] extract_mermaid_blocks x10000: {elapsed*1000:.2f}ms")
        assert elapsed < 1.0



class TestIllustrationPlanningPerformance:
    def test_plan_illustrations_performance(self):
        content = "本项目包含深基坑工程和高d模板工程。" * 10
        start = time.perf_counter()
        for _ in range(1000):
            plan_illustrations("1", "章节标题", content)
        elapsed = time.perf_counter() - start
        print(f"\n[PERF] plan_illustrations x1000: {elapsed*1000:.2f}ms")
        assert elapsed < 0.5

    def test_plan_outline_illustrations_performance(self):
        start = time.perf_counter()
        for _ in range(100):
            plans = plan_outline_illustrations(LARGE_OUTLINE)
        elapsed = time.perf_counter() - start
        print(f"\n[PERF] plan_outline_illustrations x100: {elapsed*1000:.2f}ms")
        assert elapsed < 2.0

    def test_count_illustrations_performance(self):
        plans = plan_outline_illustrations(LARGE_OUTLINE)
        start = time.perf_counter()
        for _ in range(10000):
            count_illustrations(plans)
        elapsed = time.perf_counter() - start
        print(f"\n[PERF] count_illustrations x10000: {elapsed*1000:.2f}ms")
        assert elapsed < 1.0


class TestKnowledgeRefillPerformance:
    def test_split_text_performance(self):
        start = time.perf_counter()
        for _ in range(100):
            batches = split_text_into_batches(LARGE_BID_TEXT, max_batch_chars=500)
            assert len(batches) > 1
        elapsed = time.perf_counter() - start
        print(f"\n[PERF] split_text x100: {elapsed*1000:.2f}ms")
        assert elapsed < 0.5

    def test_normalize_title_performance(self):
        titles = ["项目概述", "深基坑工程", "模板工程", "脚手架工程"] * 100
        start = time.perf_counter()
        for title in titles:
            _normalize_title(title)
        elapsed = time.perf_counter() - start
        print(f"\n[PERF] _normalize_title x{len(titles)}: {elapsed*1000:.2f}ms")
        assert elapsed < 0.1

    def test_is_duplicate_performance(self):
        existing_titles = {f"title_{i}" for i in range(100)}
        existing_contents = {f"content_{i}" for i in range(100)}
        test_item = KnowledgeItem(id="test", title="test_title", content="test_content")
        start = time.perf_counter()
        for _ in range(10000):
            _is_duplicate(test_item, existing_titles, existing_contents)
        elapsed = time.perf_counter() - start
        print(f"\n[PERF] _is_duplicate x10000: {elapsed*1000:.2f}ms")
        assert elapsed < 0.5

    def test_merge_performance(self):
        new_items = [
            KnowledgeItem(id=f"new_{i}", title=f"新条目{i}", content=f"内容{i}")
            for i in range(50)
        ]
        start = time.perf_counter()
        for _ in range(100):
            merge_knowledge_items(KNOWLEDGE_ITEMS, new_items)
        elapsed = time.perf_counter() - start
        print(f"\n[PERF] merge_knowledge_items x100: {elapsed*1000:.2f}ms")
        assert elapsed < 0.5


class TestTextEditPerformance:
    def test_apply_text_edit_performance(self):
        text = "hello world" * 100
        start = time.perf_counter()
        for _ in range(10000):
            result = apply_text_edit(text, "world", "planet")
        elapsed = time.perf_counter() - start
        print(f"\n[PERF] apply_text_edit x10000: {elapsed*1000:.2f}ms")
        assert elapsed < 1.0

    def test_find_occurrences_performance(self):
        text = "aa aa aa aa aa" * 10
        start = time.perf_counter()
        for _ in range(10000):
            positions = find_all_occurrences(text, "aa")
        elapsed = time.perf_counter() - start
        print(f"\n[PERF] find_all_occurrences x10000: {elapsed*1000:.2f}ms")
        assert elapsed < 1.0

    def test_apply_text_edits_performance(self):
        text = "hello world foo bar"
        edits = [
            {"old_text": "hello", "new_text": "hi"},
            {"old_text": "world", "new_text": "earth"},
            {"old_text": "foo", "new_text": "bar"},
        ]
        start = time.perf_counter()
        for _ in range(10000):
            apply_text_edits(text, edits)
        elapsed = time.perf_counter() - start
        print(f"\n[PERF] apply_text_edits x10000: {elapsed*1000:.2f}ms")
        assert elapsed < 2.0



class TestNewModulesFunctionality:
    """新增模块功能验证测试。"""

    def test_json_repair_quick_extract(self):
        result, error = quick_extract_json(VALID_JSON)
        assert result is not None
        assert error is None
        assert "outline" in result

        result, error = quick_extract_json(JSON_WITH_FENCE)
        assert result is not None

        result, error = quick_extract_json(INVALID_JSON)
        assert result is None
        assert error is not None

    def test_mermaid_repair_check_syntax(self):
        error = check_mermaid_syntax(VALID_MERMAID)
        assert error is None

        error = check_mermaid_syntax(INVALID_MERMAID)
        assert error is not None

    def test_mermaid_extract_blocks(self):
        text = f"正文内容。\n\n```mermaid\n{VALID_MERMAID}\n```\n\n更多正文。"
        blocks = extract_mermaid_blocks(text)
        assert len(blocks) == 1
        assert "flowchart" in blocks[0]

    def test_illustration_planning_detect_types(self):
        content = "本项目施工流程分为以下阶段：准备阶段、实施阶段、验收阶段。"
        plan = plan_illustrations("1", "施工流程", content)
        assert len(plan.illustrations) > 0
        assert plan.illustrations[0].type == "mermaid"

    def test_knowledge_refill_dedup(self):
        existing = [
            KnowledgeItem(id="1", title="项目概述", content="项目基本信息"),
        ]
        new_items = [
            KnowledgeItem(id="2", title="项目概述", content="项目基本信息"),
            KnowledgeItem(id="3", title="施工部署", content="施工部署内容"),
        ]
        merged = merge_knowledge_items(existing, new_items)
        assert len(merged) == 2

    def test_outline_numbering_render(self):
        template = default_numbering_template()
        assert template.render_path([1]) == "第1章"
        assert template.render_path([1, 2]) == "1.2"
        assert template.render_path([1, 2, 3]) == "1.2.3"

    def test_hazard_scan_detects(self):
        result = scan_hazard_projects(LARGE_BID_TEXT)
        assert len(result.detected) >= 5

    def test_validate_outline_json(self):
        data = {"outline": LARGE_OUTLINE}
        errors = validate_outline_json(data)
        assert errors == []

        # Empty outline is valid (no errors)
        data = {"outline": []}
        errors = validate_outline_json(data)
        assert errors == []

        # Missing both outline and sections
        data = {"other": []}
        errors = validate_outline_json(data)
        assert len(errors) > 0

        data = {"outline": [{"title": "无 id 节点"}]}
        errors = validate_outline_json(data)
        assert any("id" in e for e in errors)


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])

