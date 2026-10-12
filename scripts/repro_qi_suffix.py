"""复现：目录 L3 标题被追加「（其N）」等冗余后缀的问题。

结构取自用户截图（「专业分包管理」一章下有多个 L2 分组，各分组的 L3 条目大量同名）：
  三、专业分包资源配置与协调保障 / 四、专业分包过程控制与考核机制 / 五、专业分包总结评估与持续改进
各分组下都出现「专业分包地下施工工艺」「专业分包砌体质量控制」等同名条目 —— 这在标书目录里
是合法的（分属不同二级章，靠父级区分）。当前 `_uniquify_outline_titles` 做「全树全局去重」，
把这些合法同名条目强制改名，最终退化为「（其3）（其4）（其5）」或「专项方案/补充措施」
这类无意义后缀。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from app.services.ai.outline_common import _uniquify_outline_titles  # noqa: E402

POOL = [
    "专业分包地下施工工艺",
    "专业分包砌体质量控制",
    "专业分包机电管线安全防护",
    "专业分包桩基技术交底",
    "专业分包装配式验收标准",
    "专业分包给排水操作要点",
    "专业分包地下施工准备",
]


def build_tree(n_groups: int, n_items: int) -> list[dict]:
    """构造 n_groups 个 L2 分组，每组 n_items 个 L3 条目（标题从同一 POOL 循环取）。"""
    l2s = []
    for g in range(n_groups):
        l2s.append(
            {
                "title": f"专业分包第{g + 1}分组过程控制与考核机制",
                "level": 2,
                "children": [
                    {"title": POOL[i % len(POOL)], "level": 3, "children": []}
                    for i in range(n_items)
                ],
            }
        )
    return [{"title": "专业分包管理", "level": 1, "children": l2s}]


def walk(nodes, depth=0, lines=None):
    if lines is None:
        lines = []
    for n in nodes:
        lines.append("  " * depth + f"{n['title']}")
        if n.get("children"):
            walk(n["children"], depth + 1, lines)
    return lines


if __name__ == "__main__":
    for n_groups in (2, 3, 4, 6):
        for n_items in (5, 9):
            tree = build_tree(n_groups, n_items)
            _uniquify_outline_titles(tree)
            out = walk(tree)
            bad_qi = [t for t in out if "（其" in t]
            bad_dim = [t for t in out if t.endswith(("专项方案", "补充措施", "保障措施", "管控要点"))]
            print(f"--- L2 分组 {n_groups} × 每组 L3 {n_items} ---")
            print(f"   含「（其N）」: {len(bad_qi)}   含裸追加维度词: {len(bad_dim)}")
            for t in bad_qi[:6]:
                print("      ", t)
            for t in bad_dim[:6]:
                print("      ", t)
