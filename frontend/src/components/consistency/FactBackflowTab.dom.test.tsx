// @vitest-environment jsdom
/**
 * FactBackflowTab 组件级交互测试（ENHANCE 2026-10-11；2026-10-12 修复加载失败）。
 *
 * 覆盖：
 * 1. 组件可挂载，空项目时优雅展示
 * 2. 候选列表渲染：Table + rowSelection
 * 3. 采纳按钮：selectedKeys 触发 acceptFactProposals
 * 4. 无选中时采纳按钮禁用
 *
 * ── 2026-10-12 修复：本文件此前**从未真正执行过任何用例**（vitest 报
 * 「Failed Suites / No test suite found」）。叠加三因：
 *   (1) `describe.skipIf(!hasDom, "FactBackflowTab", () => {...})` 参数错位 ——
 *       `describe.skipIf(cond)` 只接受条件，suite 名必须传给返回的函数；
 *       原写法把名字传给了 skipIs，最终 `describe(回调)` 把回调当 options，
 *       vitest 从未注册任何 suite。正解：`describe.skipIf(cond)("名称", fn)`。
 *   (2) `vi.mock` 工厂的 TDZ（静态 import 组件 + 顶层 const mock）。
 *       改用 `vi.hoisted()` 提升 mock 实例。
 *   (3) 断言用了未安装的 `@testing-library/jest-dom` matcher
 *       `toBeInTheDocument`（Invalid Chai property），改为 vitest 原生断言。
 *   (4) `vi.mock("../../utils", ...)` 用单键桩整体替换 utils 模块，
 *       改为透传原模块后只覆盖目标导出。
 * 另外原用例对「30」这类文本用 getByText，而它同时出现在取值 Tag 与
 * 出现次数 Tag 中，会抛 "Found multiple elements" —— 改用 getAllByText。
 */
import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen, waitFor, cleanup, fireEvent } from "@testing-library/react";

const hasDom = typeof window !== "undefined" && typeof document !== "undefined";

import FactBackflowTab from "./FactBackflowTab";

const { proposalsMock, acceptMock } = vi.hoisted(() => ({
  proposalsMock: vi.fn(),
  acceptMock: vi.fn(),
}));

const DEFAULT_PROPOSALS = async (_pid: number) => ({
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
});

const DEFAULT_ACCEPT = async (_pid: number, _proposals: unknown[]) => ({
  status: "ok",
  added: 1,
  merged_total: 1,
});

proposalsMock.mockImplementation(DEFAULT_PROPOSALS);
acceptMock.mockImplementation(DEFAULT_ACCEPT);

vi.mock("../../api", () => ({
  proposeFactsFromConflicts: proposalsMock,
  acceptFactProposals: acceptMock,
}));

vi.mock("../../utils", async (importOriginal) => {
  const actual = await importOriginal<Record<string, unknown>>();
  return {
    ...actual,
    extractApiError: (e: unknown) =>
      e instanceof Error ? e.message : String(e),
  };
});

describe.skipIf(!hasDom)("FactBackflowTab", () => {
  afterEach(() => {
    cleanup();
    // mockReset 会连实现一起清掉，必须显式恢复默认实现，
    // 否则前一个用例里的 mockResolvedValue 会污染后续用例。
    proposalsMock.mockReset();
    proposalsMock.mockImplementation(DEFAULT_PROPOSALS);
    acceptMock.mockReset();
    acceptMock.mockImplementation(DEFAULT_ACCEPT);
  });

  it("可挂载，触发初始扫描", async () => {
    render(<FactBackflowTab projectId={1} />);
    await waitFor(() => {
      expect(proposalsMock).toHaveBeenCalledWith(1, expect.any(Object));
      expect(screen.getByText(/事实回流/)).toBeTruthy();
    });
  });

  it("候选列表渲染表格", async () => {
    render(<FactBackflowTab projectId={1} />);
    await waitFor(() => {
      expect(screen.getByText(/工期/)).toBeTruthy();
      // 「30」同时出现在取值 Tag 与出现次数 Tag 中，用 getAllByText。
      expect(screen.getAllByText(/30/).length).toBeGreaterThan(0);
      expect(screen.getByText(/duration_days/)).toBeTruthy();
    });
  });

  it("空项目（零候选）展示优雅提示", async () => {
    proposalsMock.mockResolvedValue({
      status: "ok",
      proposals: [],
      skipped: 0,
      conflicts_scanned: 0,
    });
    render(<FactBackflowTab projectId={1} />);
    await waitFor(() => {
      expect(
        screen.getByText(/没有可扫描的未解决一致性冲突/),
      ).toBeTruthy();
    });
  });

  it("采纳按钮在无选中时禁用", async () => {
    render(<FactBackflowTab projectId={1} />);
    await waitFor(() => {
      expect(screen.getByText(/工期/)).toBeTruthy();
    });
    const btn = screen.getByRole("button", { name: /采纳选中/ });
    expect((btn as HTMLButtonElement).disabled).toBe(true);
  });

  it("选中后点击采纳调 acceptFactProposals", async () => {
    render(<FactBackflowTab projectId={1} />);
    await waitFor(() => {
      expect(screen.getByText(/工期/)).toBeTruthy();
    });
    const checkbox = document.querySelector(
      'input[type="checkbox"]',
    ) as HTMLInputElement | null;
    expect(checkbox).toBeTruthy();
    fireEvent.click(checkbox as HTMLInputElement);

    const acceptBtn = screen.getByRole("button", {
      name: /采纳选中/,
    });
    await waitFor(() => {
      expect((acceptBtn as HTMLButtonElement).disabled).toBe(false);
    });
    fireEvent.click(acceptBtn);

    await waitFor(() => {
      expect(acceptMock).toHaveBeenCalled();
      expect(acceptMock).toHaveBeenCalledWith(
        1,
        expect.arrayContaining([
          expect.objectContaining({ conflict_id: "c1" }),
        ]),
      );
    });
  });

  // BUG-FIX 2026-10-27（P3·采纳无独立 loading）：采纳请求进行中双击不得重复
  // POST —— ref 同步守卫（React 批处理下同 tick 双击读不到最新 state）。
  it("采纳请求进行中双击只发一次 POST（防重入）", async () => {
    let release!: (v: unknown) => void;
    acceptMock.mockImplementation(
      () =>
        new Promise((resolve) => {
          release = resolve;
        }),
    );
    render(<FactBackflowTab projectId={1} />);
    await waitFor(() => {
      expect(screen.getByText(/工期/)).toBeTruthy();
    });
    const checkbox = document.querySelector(
      'input[type="checkbox"]',
    ) as HTMLInputElement | null;
    fireEvent.click(checkbox as HTMLInputElement);

    const acceptBtn = screen.getByRole("button", { name: /采纳选中/ });
    await waitFor(() => {
      expect((acceptBtn as HTMLButtonElement).disabled).toBe(false);
    });
    // 同 tick 双击：两次 onClick 都会触发，第二次必须被 ref 守卫拦下
    fireEvent.click(acceptBtn);
    fireEvent.click(acceptBtn);

    // 请求仍在挂起（release 未调用）——此时只应有 1 次 POST
    await new Promise((r) => setTimeout(r, 50));
    expect(acceptMock).toHaveBeenCalledTimes(1);
    // 请求期间按钮进入 loading（禁用态）
    expect((acceptBtn as HTMLButtonElement).disabled).toBe(true);

    release({ status: "ok", added: 1, merged_total: 1 });
    await waitFor(() => {
      expect((screen.getByRole("button", { name: /采纳选中/ }) as HTMLButtonElement).disabled).toBe(
        false,
      );
    });
  });

  // BUG-FIX 2026-10-27（P2·dedup 撞键静默丢弃）：后端 dedup 撞键的候选现以
  // skipped 条目回传（带可读原因），前端应渲染告警而不是只弹「采纳 0 条」。
  it("采纳返回 skipped 条目时渲染未采纳原因告警", async () => {
    acceptMock.mockResolvedValue({
      status: "ok",
      added: 0,
      merged_total: 0,
      skipped: [
        {
          fact_name: "工期",
          value: "45天",
          category: "schedule",
          reason: "同名候选此前已采纳过（建议键 audit_c1 已存在）。若正文口径已变更，请先在全局事实库修改既有事实的取值，或删除旧条目后重新扫描采纳。",
        },
      ],
    });
    render(<FactBackflowTab projectId={1} />);
    await waitFor(() => {
      expect(screen.getByText(/工期/)).toBeTruthy();
    });
    const checkbox = document.querySelector(
      'input[type="checkbox"]',
    ) as HTMLInputElement | null;
    fireEvent.click(checkbox as HTMLInputElement);
    fireEvent.click(screen.getByRole("button", { name: /采纳选中/ }));

    await waitFor(() => {
      expect(screen.getAllByText(/已采纳过/).length).toBeGreaterThan(0);
    });
  });
});