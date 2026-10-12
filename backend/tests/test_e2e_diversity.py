"""端到端 Mock 集成测试：模拟真实数据跑完整目录生成流程，验证多样性效果。"""
import logging
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

# 阈值统一从 test_diversity.THRESHOLDS 引用，避免与本文件出现口径漂移。
# 参见 test_diversity.py 顶部 THRESHOLDS 的注释说明。
from test_diversity import THRESHOLDS


def _make_chapters():
    """构造 28 章的真实 Mock 数据，模拟技术评分大类。"""
    return [
        {"title": "工程概况与建设规模", "scoring_points": []},
        {"title": "施工质量目标与质量保证措施", "scoring_points": []},
        {"title": "施工安全与文明施工保障措施", "scoring_points": []},
        {"title": "施工进度计划与工期保障措施", "scoring_points": []},
        {"title": "施工总平面布置方案", "scoring_points": []},
        {"title": "主要分部工程施工方法", "scoring_points": []},
        {"title": "季节性施工措施", "scoring_points": []},
        {"title": "项目管理机构及人员配置", "scoring_points": []},
        {"title": "主要施工机械与设备投入计划", "scoring_points": []},
        {"title": "劳动力资源配置计划", "scoring_points": []},
        {"title": "材料采购与供应保障方案", "scoring_points": []},
        {"title": "工程质量通病防治方案", "scoring_points": []},
        {"title": "安全风险辨识与应急预案", "scoring_points": []},
        {"title": "关键工序施工技术专项方案", "scoring_points": []},
        {"title": "与建设单位协调配合方案", "scoring_points": []},
        {"title": "项目重难点分析与应对措施", "scoring_points": []},
        {"title": "环境保护与节能减排措施", "scoring_points": []},
        {"title": "成品保护与交付维护方案", "scoring_points": []},
        {"title": "绿色施工与智慧工地方案", "scoring_points": []},
        {"title": "施工测量与沉降观测方案", "scoring_points": []},
        {"title": "机电安装施工方案", "scoring_points": []},
        {"title": "装饰装修工程施工方案", "scoring_points": []},
        {"title": "基坑支护与降水施工方案", "scoring_points": []},
        {"title": "钢结构安装施工方案", "scoring_points": []},
        {"title": "脚手架与临边防护方案", "scoring_points": []},
        {"title": "消防与临时用电安全方案", "scoring_points": []},
        {"title": "竣工资料编制与归档方案", "scoring_points": []},
        {"title": "附圖、附件", "scoring_points": []},
    ]


def test_end_to_end_diversity():
    from app.services.ai.workflows import (
        _classify_chapter_type,
        _generate_diverse_l2_title,
        _generate_diverse_l3_title,
        sanitize_framework_titles,
    )

    chapters = _make_chapters()
    print(f"\n{'='*70}")
    print(f"端到端 Mock 测试：{len(chapters)} 章真实场景")
    print(f"{'='*70}\n")

    all_l2_titles = []
    all_l3_titles = []
    l2_uniques = set()
    l3_uniques = set()
    global_pool: set = set()  # ← V5.2 跨章节共享去重池

    for idx, ch in enumerate(chapters):
        ch_type = _classify_chapter_type(ch["title"])
        # 模拟一个带占位符的框架
        framework = {
            "parent_title": ch["title"],
            "children": [
                {
                    "title": "【请根据项目实际替换为具体标题】二级第1项",
                    "level": 2,
                    "children": [
                        {"title": "【请替换为具体标题】三级第1项", "level": 3, "children": []},
                        {"title": "【请替换为具体标题】三级第2项", "level": 3, "children": []},
                        {"title": "【请替换为具体标题】三级第3项", "level": 3, "children": []},
                    ],
                },
                {
                    "title": "【请根据项目实际替换为具体标题】二级第2项",
                    "level": 2,
                    "children": [
                        {"title": "【请替换为具体标题】三级第1项", "level": 3, "children": []},
                        {"title": "【请替换为具体标题】三级第2项", "level": 3, "children": []},
                        {"title": "【请替换为具体标题】三级第3项", "level": 3, "children": []},
                    ],
                },
                {
                    "title": "【请根据项目实际替换为具体标题】二级第3项",
                    "level": 2,
                    "children": [
                        {"title": "【请替换为具体标题】三级第1项", "level": 3, "children": []},
                        {"title": "【请替换为具体标题】三级第2项", "level": 3, "children": []},
                        {"title": "【请替换为具体标题】三级第3项", "level": 3, "children": []},
                    ],
                },
            ],
        }
        result = sanitize_framework_titles(
            framework, ch["title"], chapter_idx=idx,
            global_used_titles=global_pool,  # ← V5.2 跨章节去重
        )

        l2_titles = []
        l3_titles = []
        for child in result.get("children", []):
            l2_titles.append(child.get("title", ""))
            for gc in child.get("children", []):
                l3_titles.append(gc.get("title", ""))

        all_l2_titles.extend(l2_titles)
        all_l3_titles.extend(l3_titles)
        l2_uniques.update(l2_titles)
        l3_uniques.update(l3_titles)

        print(f"[{idx+1:02d}] {ch['title'][:30]:<30} type={ch_type:<12}")
        for lt in l2_titles:
            print(f"    ├─ {lt}")
        for lt in l3_titles[:3]:
            print(f"    │   └─ {lt}")
        if len(l3_titles) > 3:
            print(f"    │   └─ ... ({len(l3_titles)} total)")

    print(f"\n{'='*70}")
    print(f"多样性统计")
    print(f"{'='*70}")
    l2_rate = len(l2_uniques) / len(all_l2_titles) * 100 if all_l2_titles else 0
    l3_rate = len(l3_uniques) / len(all_l3_titles) * 100 if all_l3_titles else 0
    print(f"  L2 总数量: {len(all_l2_titles)}, 唯一数: {len(l2_uniques)}, 唯一率: {l2_rate:.1f}%")
    print(f"  L3 总数量: {len(all_l3_titles)}, 唯一数: {len(l3_uniques)}, 唯一率: {l3_rate:.1f}%")

    # 检查是否还有重复标题
    from collections import Counter
    l2_dup = {t: c for t, c in Counter(all_l2_titles).items() if c > 1}
    l3_dup = {t: c for t, c in Counter(all_l3_titles).items() if c > 1}
    print(f"\n  L2 重复标题: {len(l2_dup)} 个")
    for t, c in list(l2_dup.items())[:5]:
        print(f"    '{t}' 出现 {c} 次")
    print(f"  L3 重复标题: {len(l3_dup)} 个")
    for t, c in list(l3_dup.items())[:5]:
        print(f"    '{t}' 出现 {c} 次")

    # 阈值统一从 THRESHOLDS 引用，避免与本文件出现口径漂移
    l2_threshold = THRESHOLDS["L2_UNIQUENESS_E2E"] * 100
    l3_threshold = THRESHOLDS["L3_UNIQUENESS_E2E"] * 100
    assert l2_rate >= l2_threshold, f"L2 唯一率 {l2_rate:.1f}% 低于 {l2_threshold:.0f}% 阈值"
    assert l3_rate >= l3_threshold, f"L3 唯一率 {l3_rate:.1f}% 低于 {l3_threshold:.0f}% 阈值"
    print(f"\n  ✓ 多样性达标！")


if __name__ == "__main__":
    test_end_to_end_diversity()
