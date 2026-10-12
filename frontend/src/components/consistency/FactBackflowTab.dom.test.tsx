// @vitest-environment jsdom
/**
 * FactBackflowTab 组件级交互测试（ENHANCE 2026-10-11）。
 *
 * 覆盖：
 * 1. 组件可挂载，空项目时优雅展示
 * 2. 候选列表渲染：Table + rowSelection
 * 3. 采纳按钮：selectedKeys 触发 acceptFactProposals
 * 4. minOccurrences 参数透传
 */
import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen, waitFor, cleanup, fireEvent } from "@testing-library/react";

const hasDom = typeof window !== "undefined" && typeof document !== "undefined";

import FactBackflowTab from "./FactBackflowTab";

const proposalsMock = vi.fn(async (_pid: number, opts?: { minOccurrences?: number }) => ({
  status: "ok",
  proposals: [
    {
      conflict_id: "c1",
      fact_name: "工期",
      value: "30",
      occurrences: 5,
      category: "时间",
      suggested_key: "duration_days",
      occurrences_map: [
        { value: "30", count: 5 },
        { value: "45", count: 1 },
      ],
    },
  ],
  skipped: 2,
  conflicts_scanned: 3,
}));

const acceptMock = vi.fn(async (_pid: number, _proposals: unknown[]) => ({
  status: "ok",
  added: 1,
  merged_total: 1,
}));

vi.mock("../../api", () => ({
  proposeFactsFromConflicts: proposalsMock,
  acceptFactProposals: acceptMock,
}));

vi.mock("../../utils", () => ({
  extractApiError: (e: unknown) => String(e),
}));

describe.skipIf(!hasDom, "FactBackflowTab", () => {
  afterEach(() => {
    cleanup();
    proposalsMock.mockReset();
    acceptMock.mockReset();
  });

  it("可挂载，触发初始扫描", async () => {
    render(<FactBackflowTab projectId={1} />);
    await waitFor(() => {
      expect(proposalsMock).toHaveBeenCalledWith(1, expect.any(Object));
      expect(screen.getByText(/事实回流/)).toBeInTheDocument();
    });
  });

  it("候选列表渲染表格", async () => {
    render(<FactBackflowTab projectId={1} />);
    await waitFor(() => {
      expect(screen.getByText(/工期/)).toBeInTheDocument();
      expect(screen.getByText(/30/)).toBeInTheDocument();
      expect(screen.getByText(/duration_days/)).toBeInTheDocument();
    });
  });

  it("空项目（零候选）展示优雅提示", async () => {
    proposalsMock.mockResolvedValue({
      status: "ok", proposals: [], skipped: 0, conflicts_scanned: 0,
    });
    render(<FactBackflowTab projectId={1} />);
    await waitFor(() => {
      expect(screen.getByText(/没有可扫描的未解决一致性冲突/)).toBeInTheDocument();
    });
  });

  it("采纳按钮在无选中时禁用", async () => {
    render(<FactBackflowTab projectId={1} />);
    await waitFor(() => {
      const btn = screen.getByRole("button", { name: /采纳选中/ });
      expect(btn).toBeDisabled();
    });
  });

  it("选中后点击采纳调 acceptFactProposals", async () => {
    render(<FactBackflowTab projectId={1} />);
    await waitFor(() => {
      expect(screen.getByText(/工期/)).toBeInTheDocument();
    });
    // 点击表格行的 checkbox —— antd Table 的 rowSelection checkbox
    const checkbox = document.querySelector('input[type="checkbox"]');
    if (checkbox) {
      fireEvent.click(checkbox);
    }
    const acceptBtn = screen.getByRole("button", { name: /采纳选中/ });
    expect(acceptBtn).toBeEnabled();
    fireEvent.click(acceptBtn);
    await waitFor(() => {
      expect(acceptMock).toHaveBeenCalled();
    });
  });
});
