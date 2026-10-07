"""查看并分析最近一次目录生成结果的结构"""
pytestmark = pytest.mark.skip(
    reason="[V3 LEGACY] workflows_outline_v2v3.py 已物理删除，统一走 V5.1 generate_outline()，跳过已退役路径测试"
)
import asyncio
import json
import sys
import logging
from io import StringIO

sys.path.insert(0, "J:/编程/招投标方案平台/backend")

from app.services.ai.workflows import generate_outline_v3, FIXED_28_CHAPTERS
from tests.test_outline_integration import MockOutlineProvider, _make_supplement_response, _make_children_response


async def main():
    v60_sort_responses = ["", "{}"]
    analysis_responses = ["[]", "[]", "[]"]
    responses = [_make_supplement_response()] + v60_sort_responses + analysis_responses + [
        _make_children_response(ch["title"]) for ch in FIXED_28_CHAPTERS
    ]
    provider = MockOutlineProvider(responses)

    result = await generate_outline_v3(
        provider=provider,
        summary="某市城市轨道交通5号线机电系统集成项目",
        scoring_points=json.dumps([
            {"point": "BIM技术应用", "weight": 10}, {"point": "绿色施工", "weight": 8},
            {"point": "智能化系统", "weight": 12}, {"point": "工期保障", "weight": 7},
            {"point": "质量管理体系", "weight": 10}, {"point": "安全管理", "weight": 8},
            {"point": "造价控制", "weight": 5}, {"point": "售后服务", "weight": 5},
        ]),
        industry_profile="城市轨道交通",
        tender_context="招标文件全文内容",
        reference_outline="",
        project_scope="城市轨道交通5号线机电系统集成，含通风空调、消防、给排水、低压配电、照明、综合监控、站台门等系统",
        project_info={"name": "某市城市轨道交通5号线机电系统集成项目"},
        progress_callback=None,
        phase_callback=None,
    )

    print("=" * 70)
    print("生成的目录结构（30 章）")
    print("=" * 70)

    # 统计统计
    total_l1 = len(result)
    total_l2 = 0
    total_l3 = 0
    chapters_with_children = 0
    chapters_empty = 0

    for i, ch in enumerate(result):
        title = ch.get("title", "无标题")
        children = ch.get("children", [])
        l2_count = len(children)
        l3_count = sum(len(c.get("children", [])) for c in children)
        total_l2 += l2_count
        total_l3 += l3_count

        status = "✅" if l2_count > 0 else "⚠️"
        print(f"  [{i+1:2d}] {status} {title} ({l2_count} 二级, {l3_count} 三级)")

        if l2_count > 0:
            chapters_with_children += 1
            for j, c in enumerate(children):
                c_title = c.get("title", "无标题")
                l3s = c.get("children", [])
                if l3s:
                    print(f"        └─ {c_title} ({len(l3s)} 三级)")
                else:
                    print(f"        └─ {c_title} (无三级)")
        else:
            chapters_empty += 1

    print()
    print("-" * 70)
    print(f"统计汇总：")
    print(f"  一级章节总数: {total_l1}")
    print(f"  二级章节总数: {total_l2}")
    print(f"  三级章节总数: {total_l3}")
    print(f"  含二级目录的章节: {chapters_with_children} / {total_l1}")
    print(f"  无二级目录的章节: {chapters_empty} / {total_l1}")
    print(f"  节点总计: {total_l1 + total_l2 + total_l3}")
    print("-" * 70)

    # 检查与 FIXED_28_CHAPTERS 的对应关系
    print("\n[固定模板章节覆盖情况]")
    fixed_titles = {ch["title"] for ch in FIXED_28_CHAPTERS}
    result_titles = {ch.get("title", "") for ch in result}
    missing = fixed_titles - result_titles
    if missing:
        print(f"  ⚠️  缺失章节: {missing}")
    else:
        print(f"  ✅ 全部 28 章固定模板已覆盖")

    if result_titles - fixed_titles:
        extra = result_titles - fixed_titles
        print(f"  📌 补充章节: {sorted(extra)}")

    # 保存 JSON 结果供后续分析
    output_path = "J:/编程/招投标方案平台/doc/生成的目录结构_30章.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"\n✅ 目录结构已保存到: {output_path}")


if __name__ == "__main__":
    asyncio.run(main())
