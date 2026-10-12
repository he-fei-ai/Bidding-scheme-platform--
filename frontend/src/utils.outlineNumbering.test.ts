/**
 * 生成大纲模块 · 编号与目录树归一化回归（BUG-FIX 2026-10-08）
 *
 * 覆盖本轮修复的 5 条前端故障链：
 *  1. outlineDictToTreeNodes 临时 ID 冲突：L2 / 最终审核下发的是**嵌套树**，
 *     旧实现 `id: n.id ?? -(idx+1)` 只按同层局部序号编号 → 多个父节点各自的
 *     首个子节点同为 -1，直接导致 (a) reviewEditTitles 改一个改名一片、
 *     (b) React key 重复、(c) 编号按 id 回查互相覆盖。
 *  2. level 派生：后端 review_needed 直出 `_generate_level1_v51` 的原始结构，
 *     L1 节点根本没有 level 字段，旧实现 `?? 1` 把 L2/L3 也当一级 → 编号错位。
 *  3. sort_order：旧实现 0 起且可能与兄弟已显式给出的值撞号。
 *  4. annotateOutlineWithNumbers 不再依赖 id 唯一性（编号命名空间统一）。
 *  5. 空数据/脏数据：非对象节点、非字符串 title 不再让整棵树渲染崩溃。
 */
import { describe, expect, it } from "vitest";

import {
  annotateOutlineWithNumbers,
  computeHeadingCounters,
  outlineDictToTreeNodes,
  nextFreeSortOrder,
  allocTempNodeId,
  formatV21Heading,
  normalizeSectionStatus,
  collectLeafIds,
  getPendingFailedLeafCount,
} from "./utils";
import type { SectionTreeNode } from "./types";

/** 收集整棵树的所有 id（含重复） */
function collectIds(nodes: SectionTreeNode[]): number[] {
  const out: number[] = [];
  const stack: SectionTreeNode[][] = [nodes];
  while (stack.length > 0) {
    const cur = stack.pop()!;
    for (const n of cur) {
      out.push(n.id);
      if (Array.isArray(n.children) && n.children.length > 0) stack.push(n.children);
    }
  }
  return out;
}

/** 收集整棵树所有 number 字符串 */
function collectNumbers(nodes: Array<SectionTreeNode & { number?: string }>): string[] {
  const out: string[] = [];
  const stack: Array<SectionTreeNode & { number?: string }> = [...nodes];
  while (stack.length > 0) {
    const cur = stack.pop()!;
    if (cur.number) out.push(cur.number);
    if (Array.isArray(cur.children) && cur.children.length > 0) {
      stack.push(...(cur.children as Array<SectionTreeNode & { number?: string }>));
    }
  }
  return out;
}

describe("outlineDictToTreeNodes：未落库临时 ID 全树唯一（BUG-01 回归）", () => {
  it("嵌套树无 id 时所有节点 ID 互不相同（旧实现必然重复）", () => {
    // 复刻 L2 / 最终审核阶段后端下发的形状：3 个 L1，各带 2 个 L2，各带 2 个 L3
    const nested = Array.from({ length: 3 }, (_, i) => ({
      title: `第${i + 1}章`,
      children: Array.from({ length: 2 }, (_, j) => ({
        title: `${i + 1}.${j + 1}`,
        children: Array.from({ length: 2 }, (_, k) => ({ title: `${i + 1}.${j + 1}.${k + 1}` })),
      })),
    }));
    expect(nested).toHaveLength(3);

    const tree = outlineDictToTreeNodes(nested);
    const ids = collectIds(tree);
    // 3 + 3*2 + 3*2*2 = 21
    expect(ids).toHaveLength(21);
    expect(new Set(ids).size).toBe(ids.length);
  });

  it("临时 ID 恒为负数，永不与数据库正整数主键冲突", () => {
    const tree = outlineDictToTreeNodes([{ title: "A" }, { title: "B" }]);
    for (const id of collectIds(tree)) expect(id).toBeLessThan(0);
  });

  it("多次转换 / 添加-删除-再添加 不复用 ID", () => {
    const first = collectIds(outlineDictToTreeNodes([{ title: "A" }, { title: "B" }]));
    const added = allocTempNodeId();
    const second = collectIds(outlineDictToTreeNodes([{ title: "A" }, { title: "B" }]));
    expect(new Set([...first, added, ...second]).size).toBe(4 + 1);
    expect(added).toBeLessThan(0);
  });

  it("后端已给出合法正整数 id 时原样保留（向后兼容）", () => {
    const tree = outlineDictToTreeNodes([
      { id: 7, title: "甲" },
      { id: 9, title: "乙" },
    ]);
    expect(tree.map((n) => n.id)).toEqual([7, 9]);
  });

  it("后端给出重复/非法 id 时自动换成临时 id，不产生撞号", () => {
    const tree = outlineDictToTreeNodes([
      { id: 5, title: "甲" },
      { id: 5, title: "乙" },
      { id: -3, title: "丙" },
    ]);
    const ids = tree.map((n) => n.id);
    expect(new Set(ids).size).toBe(3);
    expect(ids[0]).toBe(5); // 首次出现的合法 id 保留
  });
});

describe("outlineDictToTreeNodes：level 按递归深度派生（BUG-09 回归）", () => {
  it("后端不带 level 时按深度派生，审核弹窗编号才对得上落库结果", () => {
    const tree = outlineDictToTreeNodes([
      { title: "一", children: [{ title: "一.1", children: [{ title: "一.1.1" }] }] },
    ]);
    expect(tree[0].level).toBe(1);
    expect(tree[0].children![0].level).toBe(2);
    expect(tree[0].children![0].children![0].level).toBe(3);
  });

  it("沿用后端显式给出的合法 level", () => {
    const tree = outlineDictToTreeNodes([{ title: "一", level: 1, children: [{ title: "一.1", level: 2 }] }]);
    expect(tree[0].level).toBe(1);
    expect(tree[0].children![0].level).toBe(2);
  });

  it("非法 level（0 / 负数 / 非数字）回退到深度派生而非一律当一级", () => {
    const tree = outlineDictToTreeNodes([
      { title: "一", level: 0, children: [{ title: "一.1", level: "abc" }] },
    ]);
    expect(tree[0].level).toBe(1);
    expect(tree[0].children![0].level).toBe(2);
  });
});

describe("outlineDictToTreeNodes：sort_order 1 起连续且不撞号", () => {
  it("缺省时按兄弟序补 1 起的连续值", () => {
    const tree = outlineDictToTreeNodes([{ title: "A" }, { title: "B" }, { title: "C" }]);
    expect(tree.map((n) => n.sort_order)).toEqual([1, 2, 3]);
  });

  it("已显式给出的 sort_order 保留，缺省的避开已占用值", () => {
    const tree = outlineDictToTreeNodes([
      { title: "A", sort_order: 1 },
      { title: "B" },
      { title: "C", sort_order: 5 },
      { title: "D" },
    ]);
    const orders = tree.map((n) => n.sort_order);
    expect(orders[0]).toBe(1);
    expect(orders[2]).toBe(5);
    expect(new Set(orders).size).toBe(4);
    expect(Math.min(...orders)).toBeGreaterThanOrEqual(1);
  });

  it("nextFreeSortOrder 避开已占用的连续值", () => {
    const base = outlineDictToTreeNodes([
      { title: "A", sort_order: 1 },
      { title: "B", sort_order: 2 },
    ]);
    expect(nextFreeSortOrder(base)).toBe(3);
    const gapped = outlineDictToTreeNodes([{ title: "A", sort_order: 1 }, { title: "B", sort_order: 4 }]);
    expect(nextFreeSortOrder(gapped)).toBe(2);
  });
});

describe("outlineDictToTreeNodes：脏数据容错（BUG-08 回归）", () => {
  it("title 为 null / 数字 / 空白 统一转字符串，不给下游 .trim() 留坑", () => {
    const tree = outlineDictToTreeNodes([
      { title: null },
      { title: 42 },
      { title: "   " },
      { title: "  有效标题  " },
    ]);
    expect(tree.map((n) => n.title)).toEqual(["", "42", "", "有效标题"]);
    for (const n of tree) expect(typeof n.title).toBe("string");
  });

  it("非对象节点被跳过而非让整棵转换抛错", () => {
    const tree = outlineDictToTreeNodes([null, undefined, "字符串", 7, { title: "OK" }]);
    expect(tree).toHaveLength(1);
    expect(tree[0].title).toBe("OK");
  });

  it("children 非数组时安全收敛为空，不递归爆栈", () => {
    const tree = outlineDictToTreeNodes([
      { title: "A", children: { "0": { title: "A1" } } },
      { title: "B", children: "not-a-list" },
    ]);
    expect(tree).toHaveLength(2);
    expect(tree[0].children).toEqual([]);
    expect(tree[1].children).toEqual([]);
  });

  it("入参非数组返回空树", () => {
    expect(outlineDictToTreeNodes(undefined as never)).toEqual([]);
    expect(outlineDictToTreeNodes(null as never)).toEqual([]);
  });
});

describe("编号正确率：审核态嵌套树的展示编号与落库口径一致（BUG-01/07/09 联动回归）", () => {
  it("L1/L2/L3 分别渲染为 第一节 / 一、 / 1、 且互不串号", () => {
    const tree = outlineDictToTreeNodes([
      {
        title: "编制依据",
        children: [{ title: "编制说明" }, { title: "引用规范" }],
      },
      {
        title: "施工方案",
        children: [{ title: "总体部署" }],
      },
    ]);
    const annotated = annotateOutlineWithNumbers(tree);
    expect(annotated.map((n) => n.number)).toEqual(["第一节、", "第二节、"]);

    const l1 = annotated[0].children as Array<SectionTreeNode & { number?: string }>;
    const l2 = annotated[1].children as Array<SectionTreeNode & { number?: string }>;
    // 旧实现在子节点 id 撞号（都为 -1）时，两棵树的所有子节点会拿到同一个编号
    expect(l1.map((n) => n.number)).toEqual(["一、", "二、"]);
    expect(l2.map((n) => n.number)).toEqual(["一、"]);
  });

  it("三级编号按 父章.父节.自身 递增", () => {
    const tree = outlineDictToTreeNodes([
      {
        title: "一",
        children: [
          { title: "一.1", children: [{ title: "一.1.1" }, { title: "一.1.2" }] },
          { title: "一.2", children: [{ title: "一.2.1" }] },
        ],
      },
    ]);
    const annotated = annotateOutlineWithNumbers(tree);
    const c1 = annotated[0].children as Array<SectionTreeNode & { number?: string }>;
    const g1 = c1[0].children as Array<SectionTreeNode & { number?: string }>;
    const g2 = c1[1].children as Array<SectionTreeNode & { number?: string }>;
    expect(g1.map((n) => n.number)).toEqual(["1、", "2、"]);
    expect(g2.map((n) => n.number)).toEqual(["1、"]);
    expect(c1.map((n) => n.number)).toEqual(["一、", "二、"]);
  });

  it("同层所有编号互不相同（旧实现 id 撞号时会重复）", () => {
    const nested = Array.from({ length: 4 }, (_, i) => ({
      title: `L1-${i}`,
      children: Array.from({ length: 3 }, (_, j) => ({ title: `L2-${i}-${j}` })),
    }));
    const annotated = annotateOutlineWithNumbers(outlineDictToTreeNodes(nested));
    const all = collectNumbers(annotated);
    const perParent: string[][] = [];
    for (const root of annotated) {
      const kids = root.children as Array<SectionTreeNode & { number?: string }>;
      perParent.push(kids.map((k) => k.number as string));
    }
    for (const group of perParent) {
      expect(new Set(group).size).toBe(group.length);
    }
    expect(all.length).toBe(4 + 12);
  });
});

describe("annotateOutlineWithNumbers：编号与 id 唯一性解耦（BUG-07 回归）", () => {
  it("id 全部重复时编号仍然正确（旧实现会互相覆盖 + 静默降级为「第一节」）", () => {
    const dup = (level: number, title: string, children: SectionTreeNode[] = []): SectionTreeNode => ({
      id: -1, // 全部撞号
      title,
      level,
      sort_order: 1,
      section_type: "text",
      scoring_point_id: null,
      status: "active",
      children,
      has_content: false,
      word_count: 0,
    });
    const tree: SectionTreeNode[] = [
      dup(1, "一", [dup(2, "一.1"), dup(2, "一.2")]),
      dup(1, "二", [dup(2, "二.1")]),
    ];
    const annotated = annotateOutlineWithNumbers(tree);
    expect(annotated.map((n) => n.number)).toEqual(["第一节、", "第二节、"]);
    const first = annotated[0].children as Array<SectionTreeNode & { number?: string }>;
    const second = annotated[1].children as Array<SectionTreeNode & { number?: string }>;
    expect(first.map((n) => n.number)).toEqual(["一、", "二、"]);
    expect(second.map((n) => n.number)).toEqual(["一、"]);
  });

  it("非法 level（缺失 / 0 / null / 字符串）归一为一级，不再错分组", () => {
    const mk = (id: number, level: unknown, title: string): SectionTreeNode =>
      ({
        id,
        title,
        level,
        sort_order: 1,
        section_type: "text",
        scoring_point_id: null,
        status: "active",
        children: [],
        has_content: false,
        word_count: 0,
      }) as unknown as SectionTreeNode;
    const annotated = annotateOutlineWithNumbers([mk(1, undefined, "A"), mk(2, 0, "B"), mk(3, "x", "C")]);
    expect(annotated.map((n) => n.number)).toEqual([
      formatV21Heading(1, [1], ""),
      formatV21Heading(1, [2], ""),
      formatV21Heading(1, [3], ""),
    ]);
  });

  it("非对象节点被跳过且不抛错（脏数据容错）", () => {
    const dirty = [null, 42, "x", { id: 1, title: "A", level: 1, children: [] }] as never[];
    expect(() => annotateOutlineWithNumbers(dirty)).not.toThrow();
    expect(annotateOutlineWithNumbers(dirty)).toHaveLength(1);
  });

  it("不修改入参（纯函数）", () => {
    const tree = outlineDictToTreeNodes([{ title: "A", children: [{ title: "A1" }] }]);
    const snapshot = JSON.parse(JSON.stringify(tree));
    annotateOutlineWithNumbers(tree);
    expect(tree).toEqual(snapshot);
  });

  it("computeHeadingCounters 与 annotateOutlineWithNumbers 口径一致（编号命名空间唯一）", () => {
    const tree = outlineDictToTreeNodes([
      {
        title: "一",
        children: [{ title: "一.1", children: [{ title: "一.1.1" }, { title: "一.1.2" }] }],
      },
      { title: "二" },
    ]);
    const paths = computeHeadingCounters(tree);
    const annotated = annotateOutlineWithNumbers(tree);
    const l1 = annotated[0].children as Array<SectionTreeNode & { number?: string }>;
    const l3 = l1[0].children as Array<SectionTreeNode & { number?: string }>;
    expect(formatV21Heading(3, paths.get(l3[0].id) as number[], "")).toBe(l3[0].number);
    expect(formatV21Heading(3, paths.get(l3[1].id) as number[], "")).toBe(l3[1].number);
  });
});

describe("超深层级不爆栈（BUG-08 回归）", () => {
  it("2000 层链式目录的编号派生不抛 RangeError", () => {
    let root: Record<string, unknown> = { title: "N2000" };
    for (let i = 1999; i >= 1; i -= 1) root = { title: `N${i}`, children: [root] };
    const tree = outlineDictToTreeNodes([root]);
    expect(() => annotateOutlineWithNumbers(tree)).not.toThrow();
    expect(collectIds(tree)).toHaveLength(2000);
  });
});
/* ==========================================================================
 * 章节状态机归一化（BUG-FIX 2026-10-09 · 跨模块数据链回归）
 *
 * 背景：`outlineDictToTreeNodes` 此前把每个节点的状态硬编码为 `"active"`。
 * 后端 sections 表的真实状态机只有五个取值：
 *   pending → generating → completed | failed | cancelled
 * （写入方：sse_db_ops 的 _save_section_to_db / _mark_sections_generating /
 *   _rollback_dirty_sections，task_control 的 stop_generation）
 * `'active'` 全仓库无人写入、也不被任何消费者识别，后果是：
 *   · `collectLeafIds(tree, /* filterPendingFailed *\/ true)` 只收 pending/failed
 *     → 大纲刚生成、尚未写正文的章节在「补全生成」里永远选不中；
 *   · SectionEditorPage.getStatusIcon 无 'active' 分支，一律落到默认灰色时钟。
 * 后端写入侧已改为 'pending' 并提供一次性迁移（db_migrations 的 active→pending），
 * 前端此处做兜底归一化，保证新旧数据都能被正确渲染与选取。
 * ========================================================================== */

describe("normalizeSectionStatus：章节状态归一化（BUG-FIX 2026-10-09 回归）", () => {
  it("已知状态原样透传", () => {
    for (const s of ["pending", "generating", "completed", "failed", "cancelled"]) {
      expect(normalizeSectionStatus(s, false)).toBe(s);
    }
  });

  it("历史脏数据 'active' 按有无正文归一，重新进入可补全集合", () => {
    // 无正文 → pending：能被 collectLeafIds(filterPendingFailed) 选中
    expect(normalizeSectionStatus("active", false)).toBe("pending");
    // 有正文 → completed：不应被重复生成
    expect(normalizeSectionStatus("active", true)).toBe("completed");
  });

  it("未知值 / 非字符串同样归一，不产生 undefined", () => {
    expect(normalizeSectionStatus(undefined, false)).toBe("pending");
    expect(normalizeSectionStatus(null, false)).toBe("pending");
    expect(normalizeSectionStatus(123, false)).toBe("pending");
    expect(normalizeSectionStatus("weird", true)).toBe("completed");
  });

  it("outlineDictToTreeNodes 不再产出 'active'（核心回归）", () => {
    const tree = outlineDictToTreeNodes([
      { title: "A", children: [{ title: "A1" }] },
      { title: "B" },
    ]);
    const statuses: string[] = [];
    const stack: SectionTreeNode[][] = [tree];
    while (stack.length) {
      const cur = stack.pop()!;
      for (const n of cur) {
        statuses.push(String(n.status));
        if (n.children?.length) stack.push(n.children);
      }
    }
    expect(statuses).toHaveLength(3);
    expect(statuses.every((s) => s !== "active")).toBe(true);
    // 目录刚生成、全部无正文 → 全部 pending，因此都应进入「补全生成」候选
    expect(statuses.every((s) => s === "pending")).toBe(true);
  });

  it("「补全生成」能选中刚生成的大纲章节（端到端契约）", () => {
    // 复现原故障：outlineDictToTreeNodes 产出 active 的节点，
    // collectLeafIds(..., true) 一个都选不中 → 补全生成对整棵新大纲失效。
    const tree = outlineDictToTreeNodes([
      { title: "A", children: [{ title: "A1" }, { title: "A2" }] },
      { title: "B" },
    ]);
    expect(collectLeafIds(tree, true).length).toBe(3);
    expect(getPendingFailedLeafCount(tree)).toBe(3);
  });

  it("已生成正文的章节不会被重复选中", () => {
    const tree = outlineDictToTreeNodes([
      { title: "A", has_content: true, status: "completed" },
      { title: "B", has_content: true, status: "active" },
    ]);
    expect(collectLeafIds(tree, true)).toHaveLength(0);
  });
});
