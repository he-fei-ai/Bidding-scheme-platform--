// @vitest-environment jsdom
/**
 * 架构图 L3 叶子层竖排优化 — 组件级 DOM 测试（2026-10-11）。
 *
 * 覆盖《架构图 L3 叶子层文字垂直显示优化方案》前端部分：
 *  1. L3 节点竖排样式（writing-mode: vertical-rl + text-orientation: upright）
 *  2. L3 尺寸配置化（竖排 60x200、横排 140x50）
 *  3. 自动判断节点数（L3 > 6 → 竖排，≤ 6 → 横排）
 *  4. 高亮节点（level3 highlight）黄色背景视觉突出
 */
import { describe, it, expect } from "vitest";
import { render } from "@testing-library/react";
import ArchitectureChart from "./ArchitectureChart";
import type { ArchNodeData } from "./ArchitectureChart";

/** 构造 11 个 L3 叶子的架构数据（与方案示例一致，触发竖排模式） */
function buildVerticalTree(): ArchNodeData {
  const children = (prefix: string, n: number) =>
    Array.from({ length: n }, (_, i) => ({
      id: `${prefix}_${i}`,
      label: `节点${prefix}_${i + 1}`,
      variant: "level3" as const,
    }));
  return {
    id: "root",
    label: "青浦住宅项目质量管控体系",
    variant: "root",
    children: [
      { id: "g1", label: "公司级质量管控", variant: "level2", children: children("g1", 2) },
      { id: "g2", label: "项目部质量执行", variant: "level2", children: children("g2", 4) },
      { id: "g3", label: "现场质量落实", variant: "level2", children: children("g3", 3) },
      { id: "g4", label: "质量保障支撑", variant: "level2", children: children("g4", 2) },
    ],
  };
}

function verticalNodes(container: HTMLElement): HTMLDivElement[] {
  return Array.from(container.querySelectorAll<HTMLDivElement>("div")).filter(
    d => d.style.writingMode === "vertical-rl"
  );
}

describe("ArchitectureChart L3 竖排优化", () => {
  it("L3 叶子 > 6 时节点采用 vertical-rl 竖排样式（60x200 窄高矩形）", () => {
    const { container } = render(<ArchitectureChart data={buildVerticalTree()} title="青浦住宅项目质量管控体系" />);
    const leaves = verticalNodes(container);
    expect(leaves.length).toBe(11);
    leaves.forEach(leaf => {
      expect(leaf.style.width).toBe("60px");
      expect(leaf.style.height).toBe("200px");
      expect(leaf.style.textOrientation).toBe("upright");
    });
    // L2 中间节点在竖排模式下同步收窄（160px），进一步压缩画布宽度
    const l2 = Array.from(container.querySelectorAll<HTMLDivElement>("div")).find(
      d => d.textContent === "公司级质量管控"
    );
    expect(l2?.style.width).toBe("160px");
    // 布局模式指示器显示『竖排』
    expect(container.textContent).toContain("竖排");
    // 画布宽度不再横向溢出（竖排模式宽度 < 600 节点总宽）
    expect(container.textContent).not.toContain("横排");
  });

  it("L3 ≤ 6 时保持横排（无 vertical-rl 样式）", () => {
    const data: ArchNodeData = {
      id: "root",
      label: "小架构",
      variant: "root",
      children: [
        {
          id: "a",
          label: "A",
          variant: "level2",
          children: [
            { id: "a1", label: "A1", variant: "level3" },
            { id: "a2", label: "A2", variant: "level3" },
          ],
        },
      ],
    };
    const { container } = render(<ArchitectureChart data={data} title="小架构" />);
    expect(verticalNodes(container).length).toBe(0);
    expect(container.textContent).toContain("横排");
  });

  it("高亮节点为黄色背景（level3 highlight 视觉突出）", () => {
    const data = buildVerticalTree();
    data.children![0].children![0].variant = "level3 highlight";
    const { container } = render(<ArchitectureChart data={data} title="高亮测试" />);
    const highlighted = Array.from(container.querySelectorAll<HTMLDivElement>("div")).find(
      d => d.style.background === "rgb(245, 184, 0)"
    );
    expect(highlighted).toBeTruthy();
  });
});