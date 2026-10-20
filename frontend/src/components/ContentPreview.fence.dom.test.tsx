// @vitest-environment jsdom
/**
 * ContentPreview 图表围栏解析回归测试（2026-10-17，前端/后端口径对齐）。
 *
 * 覆盖与后端 content_cleaning 围栏状态机同口径的三个场景：
 *  1. AI 用 `​``mermaid` 作闭合行（带 info-string）→ 正常解析，不丢图
 *  2. AI 漏写闭合行、两张同语言图相邻 → 前块删、后块正常解析，源码不泄漏为正文
 *  3. chart_json / 属性串变体 → 正常解析
 */
import { describe, it, expect } from "vitest";
import { render } from "@testing-library/react";
import ContentPreview from "./ContentPreview";

describe("ContentPreview 图表围栏解析（口径对齐后端 content_cleaning）", () => {
  it("闭合行携带 mermaid 语言标记时正常解析（BUG-FIX 2026-10-09 后端口径对齐）", () => {
    const content = [
      "前文段落。",
      "",
      "```mermaid",
      "flowchart TD",
      "    A-->B",
      "```mermaid",
      "",
      "后文段落。",
    ].join("\n");
    const { container } = render(<ContentPreview content={content} />);
    const text = container.textContent || "";
    // 前后文完整保留
    expect(text).toContain("前文段落。");
    expect(text).toContain("后文段落。");
    // mermaid 源码绝不作为正文显示
    expect(text).not.toContain("flowchart TD");
    expect(text).not.toContain("A-->B");
    // 围栏标记行不暴露
    expect(text).not.toContain("```");
  });

  it("未闭合围栏后跟同语言新图：前块删、后块正常解析（BUG-FIX 2026-10-14 后端口径对齐）", () => {
    const content = [
      "```mermaid",
      "flowchart TD",
      "    A-->B",
      "```mermaid",
      "flowchart TD",
      "    X-->Y",
      "```",
      "",
      "收尾段落。",
    ].join("\n");
    const { container } = render(<ContentPreview content={content} />);
    const text = container.textContent || "";
    // 旧实现 i = j + 1 会吞掉第二张图的起始行 → "flowchart TD / X-->Y" 泄漏为正文
    expect(text).not.toContain("X-->Y");
    expect(text).not.toContain("flowchart TD");
    // 收尾正文不丢
    expect(text).toContain("收尾段落。");
  });

  it("chart_json 变体围栏正常解析为图表块", () => {
    const payload = { type: "labor", title: "劳动力配置", phases: ["准备"], categories: ["管理"], data: [[3]] };
    const content = [
      "正文段落。",
      "",
      "```chart_json",
      JSON.stringify(payload),
      "```",
    ].join("\n");
    const { container } = render(<ContentPreview content={content} />);
    const text = container.textContent || "";
    expect(text).toContain("正文段落。");
    // JSON 源码不泄漏
    expect(text).not.toContain('"phases"');
    expect(text).not.toContain("```chart_json");
  });

  it("chart-json 闭合行带属性串仍可解析（与后端 _FENCE_ATTR 同口径）", () => {
    const content = [
      "```chart-json",
      '{"type":"comparison","title":"方案对比","rows":[{"label":"A","value":1}]}',
      "``` {\"id\": 1}",
      "",
      "尾段。",
    ].join("\n");
    const { container } = render(<ContentPreview content={content} />);
    const text = container.textContent || "";
    expect(text).toContain("尾段。");
    expect(text).not.toContain('"rows"');
    expect(text).not.toContain('{"id": 1}');
  });

  it("普通正文与普通代码围栏不受图表解析影响", () => {
    const content = "说明文字。\n\n```python\nprint('hello')\n```\n\n更多文字。";
    const { container } = render(<ContentPreview content={content} />);
    const text = container.textContent || "";
    expect(text).toContain("说明文字。");
    expect(text).toContain("更多文字。");
  });
});
