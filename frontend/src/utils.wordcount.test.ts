/**
 * 方案三混合口径（2026-10-12）：resolveTargetForNode / resolveToleranceForNode 单测。
 *
 * 叶子 L3 固定 300±20%，不受四档下拉影响；含下级(L4+)的 L3 走四档「子树总字数」；
 * 覆盖优先级：单节点 target_words > 单节点 word_count_tier > 上述规则。
 */
import { describe, it, expect } from "vitest";
import { resolveTargetForNode, resolveToleranceForNode } from "./utils";
import type { SectionTreeNode } from "./types";

const node = (over: Partial<SectionTreeNode> = {}): SectionTreeNode =>
  ({
    id: 1,
    title: "1.1.1",
    level: 3,
    sort_order: 1,
    section_type: "text",
    status: "pending",
    children: [],
    has_content: false,
    word_count: 0,
    cumulative_word_count: 0,
    target_words: null,
    word_count_tier: null,
    ...over,
  } as unknown as SectionTreeNode);

describe("resolveTargetForNode（方案三混合口径）", () => {
  it("叶子 L3 无覆盖 → 固定 300，不受全局档位影响", () => {
    expect(resolveTargetForNode(node(), "detailed")).toBe(300);
    expect(resolveTargetForNode(node(), "long")).toBe(300);
    expect(resolveTargetForNode(node())).toBe(300);
  });

  it("单节点 target_words 精确覆盖最高优先级", () => {
    expect(resolveTargetForNode(node({ target_words: 888 }), "standard")).toBe(888);
  });

  it("单节点 word_count_tier 覆盖 → 档位目标", () => {
    expect(resolveTargetForNode(node({ word_count_tier: "detailed" }))).toBe(2000);
  });

  it("含下级的 L3 → 全局档位作为子树总字数", () => {
    const subtree = node({ children: [node({ id: 2, title: "1.1.1.1", level: 4 })] });
    expect(resolveTargetForNode(subtree, "detailed")).toBe(2000);
    expect(resolveTargetForNode(subtree)).toBe(1200); // 默认 standard
  });

  it("L1/L2 不参与 → null", () => {
    expect(resolveTargetForNode(node({ level: 1 }))).toBeNull();
    expect(resolveTargetForNode(node({ level: 2 }))).toBeNull();
  });

  it("L4+ 叶子跟随全局档位", () => {
    expect(resolveTargetForNode(node({ level: 4 }), "long")).toBe(2500);
  });
});

describe("resolveToleranceForNode（方案三混合口径）", () => {
  it("叶子 L3 → ±20%", () => {
    expect(resolveToleranceForNode(node())).toBeCloseTo(0.2, 5);
  });

  it("含下级的 L3 → ±10%", () => {
    const subtree = node({ children: [node({ id: 2, level: 4 })] });
    expect(resolveToleranceForNode(subtree)).toBeCloseTo(0.1, 5);
  });

  it("单节点档位 → 档位容差(0.10)", () => {
    expect(resolveToleranceForNode(node({ word_count_tier: "standard" }))).toBeCloseTo(0.1, 5);
  });

  it("L4+ → ±10%", () => {
    expect(resolveToleranceForNode(node({ level: 4 }))).toBeCloseTo(0.1, 5);
  });
});
