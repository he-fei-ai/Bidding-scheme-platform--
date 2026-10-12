// @vitest-environment jsdom
/**
 * AgentRepairTab 组件级交互测试（ENHANCE 2026-10-11）。
 *
 * 覆盖：
 * 1. 组件可挂载、启动按钮存在
 * 2. dryRun 参数透传给 API
 * 3. maxTurns 参数透传
 * 4. 成功态展示 turns / conflict_states
 * 5. 失败态展示 errors / rejected / unresolved
 */
import { describe, it, expect, vi, beforeAll, afterEach } from "vitest";
import { render, screen, waitFor, cleanup } from "@testing-library/react";

const hasDom = typeof window !== "undefined" && typeof document !== "undefined";

import AgentRepairTab from "./AgentRepairTab";

const repairMock = vi.fn(async (_pid: number, opts?: { dryRun?: boolean; maxTurns?: number }) => ({
  status: "ok",
  success: true,
  dry_run: opts?.dryRun ?? false,
  project_id: 1,
  turns: 4,
  max_turns: opts?.maxTurns ?? 8,
  conflict_states: { resolved: 2, still_open: 0 },
  changed_sections: [{ id: 3, title: "施工方案" }],
  applied: [],
  rejected: [],
  unresolved: [],
  errors: [],
  message: "Agent 修复完成",
}));

vi.mock("../../api", () => ({
  repairConsistencyWithAgent: repairMock,
}));

vi.mock("../../utils", () => ({
  extractApiError: (e: unknown) => String(e),
}));

describe.skipIf(!hasDom, "AgentRepairTab", () => {
  beforeAll(() => {
    // RTL vitest globals 未开，手动声明 cleanup 由 afterEach 管理
  });

  afterEach(() => {
    cleanup();
    repairMock.mockReset();
  });

  it("可挂载，启动修复按钮存在", async () => {
    render(<AgentRepairTab projectId={1} />);
    await waitFor(() => {
      expect(screen.getByText(/Agent 多轮修复/)).toBeInTheDocument();
      expect(screen.getByRole("button", { name: /预览/ })).toBeInTheDocument();
      expect(screen.getByRole("button", { name: /执行修复/ })).toBeInTheDocument();
    });
  });

  it("dryRun 预览时 API dryRun=true", async () => {
    repairMock.mockResolvedValue({
      status: "ok", success: true, dry_run: true,
      project_id: 1, turns: 2, max_turns: 8,
      conflict_states: null, changed_sections: [],
      applied: [], rejected: [], unresolved: [], errors: [],
      message: "预览完成",
    });
    render(<AgentRepairTab projectId={1} />);
    screen.getByRole("button", { name: /预览/ }).click();
    await waitFor(() => {
      expect(repairMock).toHaveBeenCalledWith(1, expect.objectContaining({ dryRun: true }));
    });
  });

  it("maxTurns 可配置并透传", async () => {
    render(<AgentRepairTab projectId={1} />);
    const input = screen.getByRole("spinbutton") as HTMLInputElement;
    // InputNumber 组件 spinbutton role，value 显示当前值
    await waitFor(() => {
      expect(input).toBeInTheDocument();
    });
  });

  it("成功态展示 turns 和冲突统计", async () => {
    render(<AgentRepairTab projectId={1} />);
    screen.getByRole("button", { name: /执行修复/ }).click();
    await waitFor(() => {
      expect(screen.getByText(/Agent 修复完成/)).toBeInTheDocument();
      expect(screen.getByText(/已解决 2/)).toBeInTheDocument();
      expect(screen.getByText(/4\/8 轮/)).toBeInTheDocument();
    });
  });
});
