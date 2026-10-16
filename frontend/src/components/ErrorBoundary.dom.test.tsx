// @vitest-environment jsdom
/**
 * ErrorBoundary —— 就地重试行为（BUG-FIX 2026-10-15 新增能力）。
 *
 * 背景：改造前 ErrorBoundary 只有「刷新页面 / 返回首页」两个动作，且唯一的实例
 * 挂在 SPA 根层 —— 任何一个子页面抛错都会把整棵 React 树（含登录壳、侧栏、路由
 * 状态）卸载，用户只能强刷整站。
 *
 * 本轮行为不变量：
 *   1. 挂 label 时兜底页文案带上页面名，便于区分「是哪一层被接管」；
 *   2. 「重试」把 `hasError` 归零、让子树重新渲染 —— 子组件不再抛错时，页面内容
 *      直接恢复（**不刷新整站、不丢路由状态**）；
 *   3. 缺省 label 时文案回落到「应用」，与改造前根层行为兼容。
 */
import { describe, it, expect } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import ErrorBoundary from "./ErrorBoundary";

const hasDom = typeof window !== "undefined" && typeof document !== "undefined";

function setupRecovery() {
  let shouldThrow = true;
  function ToggleBoom() {
    if (shouldThrow) {
      throw new Error("boom-for-retry-test");
    }
    return <div data-testid="recovered">已恢复</div>;
  }
  return {
    ToggleBoom,
    stopThrowing: () => {
      shouldThrow = false;
    },
  };
}

describe.skipIf(!hasDom)("ErrorBoundary 就地重试", () => {
  it("携带 label 时兜底文案带上页面名；重试后不用刷新整站即可恢复子组件", () => {
    const { ToggleBoom, stopThrowing } = setupRecovery();
    render(
      <ErrorBoundary label="一致性校核页">
        <ToggleBoom />
      </ErrorBoundary>,
    );
    // 错误被接管：页面名 + 「重试」按钮
    expect(screen.getByText("一致性校核页出现了一些问题")).toBeTruthy();
    const retryBtn = screen.getByText("重试").closest("button");
    expect(retryBtn).toBeTruthy();

    // 子组件不再抛错，点击「重试」就地恢复 —— 没有任何 location 刷新
    stopThrowing();
    fireEvent.click(retryBtn as HTMLElement);
    expect(screen.getByTestId("recovered")).toBeTruthy();
    expect(screen.queryByText("重试")).toBeNull();
  });

  it("缺省 label 时兜底文案回落到「应用」（兼容根层既有行为）", () => {
    const { ToggleBoom } = setupRecovery();
    render(
      <ErrorBoundary>
        <ToggleBoom />
      </ErrorBoundary>,
    );
    expect(screen.getByText("应用出现了一些问题")).toBeTruthy();
  });

  it("导出默认组件（供 App.tsx 与页面级复用）", () => {
    expect(typeof ErrorBoundary).toBe("function");
  });
});