// @vitest-environment jsdom
/**
 * ContentPreview 图表「模板数据」来源标签回归测试（2026-11-01，落地设计文档
 * 《方案_前端图表载荷来源标签与JSON编辑》P0/P0-可选增强）。
 *
 * 覆盖：
 *  1. chart-json 围栏含 source:"local" → 渲染「模板数据」标签；AI / 无 source 不渲染
 *  2. mermaid 围栏前引导语含「（模板示例数据…）」括注 → 渲染标签；普通引导语不渲染
 */
import { describe, it, expect } from "vitest";
import { render } from "@testing-library/react";
import ContentPreview from "./ContentPreview";

describe("ContentPreview 图表来源标签（模板数据）", () => {
  it("chart-json 围栏 source=local 时渲染「模板数据」标签", () => {
    const content = [
      "前文。",
      "",
      "```chart-json",
      '{"type":"labor","title":"劳动力配置","source":"local","phases":["准备"],"categories":["管理"],"data":[[3]]}',
      "```",
      "",
      "后文。",
    ].join("\n");
    const { container } = render(<ContentPreview content={content} />);
    expect(container.textContent || "").toContain("模板数据");
  });

  it("chart-json 围栏无 source 时不渲染标签（向后兼容）", () => {
    const content = [
      "前文。",
      "",
      "```chart-json",
      '{"type":"labor","title":"劳动力配置","phases":["准备"],"categories":["管理"],"data":[[3]]}',
      "```",
      "",
      "后文。",
    ].join("\n");
    const { container } = render(<ContentPreview content={content} />);
    expect(container.textContent || "").not.toContain("模板数据");
  });

  it("mermaid 引导语含模板括注时渲染「模板数据」标签", () => {
    const content = [
      "如下图所示（模板示例数据，请按项目实际核定）。",
      "",
      "```mermaid",
      "flowchart TD",
      "    A-->B",
      "```",
    ].join("\n");
    const { container } = render(<ContentPreview content={content} />);
    expect(container.textContent || "").toContain("模板数据");
  });

  it("普通 mermaid 引导语不渲染标签", () => {
    const content = [
      "如下图所示。",
      "",
      "```mermaid",
      "flowchart TD",
      "    A-->B",
      "```",
    ].join("\n");
    const { container } = render(<ContentPreview content={content} />);
    expect(container.textContent || "").not.toContain("模板数据");
  });
});
