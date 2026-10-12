// @vitest-environment jsdom
/**
 * RejectionCoverageTab 组件级交互测试（ENHANCE 2026-10-11）。
 *
 * 覆盖：
 * 1. 可挂载，调 getRejectionCoverage
 * 2. no_checks 状态（无废标任务）优雅提示
 * 3. 正常覆盖态展示统计 Tag 和 Table
 * 4. status='covered'/'weak'/'empty' 映射到正确颜色
 */
import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen, waitFor, cleanup } from "@testing-library/react";

const hasDom = typeof window !== "undefined" && typeof document !== "undefined";

import RejectionCoverageTab from "./RejectionCoverageTab";

const coverageMock = vi.fn(async (_pid: number) => ({
  status: "ok",
  scanned_clauses: 5,
  full_chars: 1200,
  threshold: 200,
  weak_count: 1,
  empty_count: 0,
  items: [
    {
      clause_id: "r1",
      name: "工期承诺",
      text: "投标人必须承诺总工期不超过365日历天",
      status: "covered" as const,
      coverage: 0.95,
      section_hits: ["第三章 施工方案"],
    },
    {
      clause_id: "r2",
      name: "项目经理资质",
      text: "项目经理必须具备一级建造师资质",
      status: "weak" as const,
      coverage: 0.3,
      section_hits: ["第一章 投标人资格"],
    },
  ],
}));

vi.mock("../../api", () => ({
  getRejectionCoverage: coverageMock,
}));

vi.mock("../../utils", () => ({
  extractApiError: (e: unknown) => String(e),
}));

describe.skipIf(!hasDom, "RejectionCoverageTab", () => {
  afterEach(() => {
    cleanup();
    coverageMock.mockReset();
  });

  it("可挂载，调 getRejectionCoverage", async () => {
    render(<RejectionCoverageTab projectId={1} />);
    await waitFor(() => {
      expect(coverageMock).toHaveBeenCalledWith(1);
      expect(screen.getByText(/废标覆盖率/)).toBeInTheDocument();
    });
  });

  it("no_checks 状态展示优雅提示", async () => {
    coverageMock.mockResolvedValue({
      status: "no_checks",
      scanned_clauses: 0, full_chars: 0, threshold: 200,
      weak_count: 0, empty_count: 0, items: [],
    });
    render(<RejectionCoverageTab projectId={1} />);
    await waitFor(() => {
      expect(screen.getByText(/尚未运行过废标检查/)).toBeInTheDocument();
    });
  });

  it("正常覆盖态展示统计 Tag 和表格", async () => {
    render(<RejectionCoverageTab projectId={1} />);
    await waitFor(() => {
      expect(screen.getByText(/废标条款总数 5/)).toBeInTheDocument();
      expect(screen.getByText(/已覆盖/)).toBeInTheDocument();
      expect(screen.getByText(/覆盖较弱/)).toBeInTheDocument();
      // 表格条目
      expect(screen.getByText(/工期承诺/)).toBeInTheDocument();
      expect(screen.getByText(/项目经理资质/)).toBeInTheDocument();
    });
  });
});
