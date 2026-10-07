/**
 * V3.1 编号派生纯函数单测（遗留 L3 修复 2026-10-04）
 *
 * 覆盖 computeHeadingCounters / formatV21Heading 的关键契约：
 * - 编号由树位置派生，与后端 heading_standard.DEFAULT_NUMBERING_TEMPLATES 一致
 * - BUG-FIX 2026-10-04（B6）回归：L4/L5/L6 的祖先编号必须取父链 parentPath，
 *   此前恒用局部 levelCounters 导致所有四级标题无论挂在哪一个三级下都显示「1.x」
 */
import { describe, expect, it } from "vitest";

import { computeHeadingCounters, formatV21Heading } from "./utils";
import type { SectionTreeNode } from "./types";

function node(
  id: number,
  level: number,
  title: string,
  children: SectionTreeNode[] = [],
): SectionTreeNode {
  return {
    id,
    title,
    level,
    sort_order: 1,
    section_type: "",
    scoring_point_id: null,
    status: "active",
    children,
    has_content: false,
    word_count: 0,
  };
}

/**
 * 构造树：
 * 1 第一节
 *   1.1 (L2)
 *     1 (L3, id=3)
 *       1.1 (L4, id=5)
 *         1.1.1 (L5, id=12)
 *           1.1.1.1 (L6, id=13)
 *             a) (L7, id=14)
 *       1.2 (L4, id=6)
 *     2 (L3, id=4)
 *       2.1 (L4, id=7)
 * 2 第二节
 *   1 (L2, id=9)
 *     1 (L3, id=10)
 *       1.1 (L4, id=11)
 */
const tree: SectionTreeNode[] = [
  node(1, 1, "第一节标题", [
    node(2, 2, "二级标题", [
      node(3, 3, "三级一", [
        node(5, 4, "四级甲", [
          node(12, 5, "五级", [
            node(13, 6, "六级", [node(14, 7, "七级")]),
          ]),
        ]),
        node(6, 4, "四级乙"),
      ]),
      node(4, 3, "三级二", [node(7, 4, "四级丙")]),
    ]),
  ]),
  node(8, 1, "第二节标题", [
    node(9, 2, "二级", [node(10, 3, "三级", [node(11, 4, "四级")])]),
  ]),
];

describe("computeHeadingCounters（V3.1 路径派生）", () => {
  const paths = computeHeadingCounters(tree);

  it("L1/L2/L3 独立编号", () => {
    expect(paths.get(1)).toEqual([1]);
    expect(paths.get(8)).toEqual([2]); // 第二个一级 → 第二节
    expect(paths.get(2)).toEqual([1]);
    expect(paths.get(3)).toEqual([1]);
    expect(paths.get(4)).toEqual([2]); // 同一 L2 下的第二个三级
  });

  it("B6 回归：L4 祖先编号取父链，不再恒为 1.x", () => {
    expect(paths.get(5)).toEqual([1, 1]); // 三级一 下第一个四级
    expect(paths.get(6)).toEqual([1, 2]); // 三级一 下第二个四级
    expect(paths.get(7)).toEqual([2, 1]); // 三级二 下第一个四级（旧 bug 会给出 [1,1]）
  });

  it("L5/L6 取父链前两级/前三级", () => {
    expect(paths.get(12)).toEqual([1, 1, 1]); // 1.1 下第一个五级
    expect(paths.get(13)).toEqual([1, 1, 1, 1]);
  });

  it("L7 独立编号", () => {
    expect(paths.get(14)).toEqual([1]);
  });

  it("第二棵一级子树内计数器独立起步", () => {
    expect(paths.get(9)).toEqual([1]);
    expect(paths.get(10)).toEqual([1]);
    expect(paths.get(11)).toEqual([1, 1]);
  });
});

describe("formatV21Heading（与后端模板对齐的展示编号）", () => {
  it("各级编号格式符合 V3.1 标准", () => {
    expect(formatV21Heading(1, [2], "标题")).toBe("第二节、标题");
    expect(formatV21Heading(2, [3], "标题")).toBe("三、标题");
    expect(formatV21Heading(3, [2], "标题")).toBe("2、标题");
    expect(formatV21Heading(4, [2, 1], "标题")).toBe("2.1、标题");
    expect(formatV21Heading(5, [1, 1, 2], "标题")).toBe("1.1.2、标题");
    expect(formatV21Heading(6, [1], "标题")).toBe("1）、标题");
    expect(formatV21Heading(7, [2], "标题")).toBe("b、标题");
  });

  it("树 + 编号函数联动：三级二下的四级显示 2.1、", () => {
    const paths = computeHeadingCounters(tree);
    const p = paths.get(7)!;
    expect(formatV21Heading(4, p, "四级丙")).toBe("2.1、四级丙");
  });
});
