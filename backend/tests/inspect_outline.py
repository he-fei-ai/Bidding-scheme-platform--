import json
from pathlib import Path

data = json.loads(Path("../doc/生成的目录结构_30章.json").read_text(encoding="utf-8"))
print(f"一级章节数: {len(data)}")
t2 = sum(len(ch.get("children", [])) for ch in data)
t3 = sum(sum(len(c.get("children", [])) for c in ch.get("children", [])) for ch in data)
print(f"二级章节数: {t2}")
print(f"三级章节数: {t3}")
print(f"总节点数:   {len(data) + t2 + t3}")
print()
print("一级章节列表:")
for i, ch in enumerate(data, 1):
    l2 = len(ch.get("children", []))
    print(f"  {i:2d}. {ch['title']} ({l2} 个二级)")
