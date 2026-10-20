// @vitest-environment jsdom
/**
 * ProgressPanel 状态/进度显示回归测试（BUG-FIX 2026-10-20）。
 *
 * 钉住两个此前会误导用户的显示缺陷：
 *  1. 暂停 / 中途停止（isRunning=false 且 failed=0，但未跑满）旧实现渲染成
 *     绿色 success（对勾），看起来像「已完成」；现应为 normal（灰色静止态）。
 *  2. 重试章节先 failed 后 completed 令 done>total，旧实现 percent 不钳位渲染
 *     >100%；现无条件钳到 [0,100]，且只有真正全部完成才 success。
 */
import { describe, it, expect } from "vitest";
import { render } from "@testing-library/react";
import ProgressPanel from "./ProgressPanel";
import type { ProgressItem } from "./ProgressPanel";

const mkItems = (): ProgressItem[] => [
  { section_id: 1, title: "第一章", status: "completed", progress: 100, message: "" },
  { section_id: 2, title: "第二章", status: "generating", progress: 50, message: "" },
];

const statusClassOf = (html: string): string | null => {
  const m = html.match(/ant-progress-status-(normal|success|exception|active)/);
  return m ? m[1] : null;
};

describe("ProgressPanel 状态与进度显示", () => {
  it("暂停 / 中途停止（未跑满、无失败）显示 normal，而非误导性的 success", () => {
    const { container } = render(
      <ProgressPanel items={mkItems()} total={10} success={3} failed={0} cancelled={0} isRunning={false} />,
    );
    const cls = statusClassOf(container.innerHTML);
    expect(cls).toBe("normal");
    expect(container.innerHTML).not.toContain("ant-progress-status-success");
  });

  it("真正全部完成（done>=total、无失败）才显示 success", () => {
    const { container } = render(
      <ProgressPanel items={mkItems()} total={3} success={3} failed={0} cancelled={0} isRunning={false} />,
    );
    expect(statusClassOf(container.innerHTML)).toBe("success");
  });

  it("存在失败章节显示 exception", () => {
    const { container } = render(
      <ProgressPanel items={mkItems()} total={10} success={3} failed={1} cancelled={0} isRunning={false} />,
    );
    expect(statusClassOf(container.innerHTML)).toBe("exception");
  });

  it("运行中显示 active", () => {
    const { container } = render(
      <ProgressPanel items={mkItems()} total={10} success={3} failed={0} cancelled={0} isRunning={true} />,
    );
    expect(statusClassOf(container.innerHTML)).toBe("active");
  });

  it("done>total（重试双计的历史越界场景）进度钳位到 100%，绝不超过", () => {
    const { container } = render(
      <ProgressPanel items={mkItems()} total={2} success={3} failed={0} cancelled={0} isRunning={false} />,
    );
    const bg = container.querySelector(".ant-progress-bg") as HTMLElement | null;
    expect(bg).toBeTruthy();
    const width = parseFloat((bg!.style.width || "").replace("%", ""));
    expect(width).toBeLessThanOrEqual(100);
    expect(width).toBeGreaterThan(0);
  });
});
