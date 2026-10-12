"""门禁（outline_gate）同级去重作用域回归验证。"""

from app.services.ai import outline_gate


def outline():
    return [
        {"id": "1", "title": "总体实施策划", "children": [
            {"id": "1.1", "title": "施工组织设计", "children": []},
            {"id": "1.2", "title": "资源配备计划", "children": []},
            {"id": "1.3", "title": "施工组织设计（其2）", "children": []},
        ]},
        {"id": "2", "title": "技术保障措施", "children": [
            {"id": "2.1", "title": "施工组织设计", "children": []},
            {"id": "2.2", "title": "质量保证体系", "children": []},
        ]},
        {"id": "3", "title": "质量保证措施", "children": [
            {"id": "3.1", "title": "质量保证体系", "children": []},
        ]},
    ]


facts = outline_gate.compute_outline_facts(outline())

print("duplicate_titles:",
      [(g["title"], [n["path"] for n in g["nodes"]]) for g in facts["duplicate_titles"]])
print("near_duplicate_titles:",
      [(d["a"]["title"], d["b"]["title"], round(d["similarity"], 2))
       for d in facts["near_duplicate_titles"]])

# 同父下的「施工组织设计」与「施工组织设计（其2）」= 一组重复
assert len(facts["duplicate_titles"]) == 1, "同父下 组织设计 vs 组织设计（其2） 应识别为一组重复"
group = facts["duplicate_titles"][0]
assert {n["path"] for n in group["nodes"]} == {"1/1.1", "1/1.3"}
assert {n["title"] for n in group["nodes"]} == {"施工组织设计", "施工组织设计（其2）"}
# 跨父同名（2.1 / 1.1 的组织施工设计；2.2 / 3.1 的质量保证体系）不报重复
assert facts["near_duplicate_titles"] == [], "跨父同名不应再报近义重复"

# 同父近义重复仍要报（同父 + 同层 + 近义）
o2 = outline()
o2[1]["children"].append({"id": "2.3", "title": "质量保证措施", "children": []})
facts2 = outline_gate.compute_outline_facts(o2)
print("near_duplicate_titles(同父近义):",
      [(d["a"]["title"], d["b"]["title"]) for d in facts2["near_duplicate_titles"]])
assert any(
    (d["a"]["title"], d["b"]["title"]) in {("质量保证体系", "质量保证措施")}
    for d in facts2["near_duplicate_titles"]
), "同父近义重复仍应被检出"

print("GATE OK")
