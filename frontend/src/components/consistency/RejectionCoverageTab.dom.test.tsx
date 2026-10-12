// @vitest-environment jsdom
/**
 * RejectionCoverageTab 组件级交互测试（ENHANCE 2026-10-11；2026-10-12 修复加载失败）。
 *
 * 覆盖：
 * 1. 可挂载，调 getRejectionCoverage
 * 2. no_checks 状态（无废标任务）优雅提示
 * 3. 正常覆盖态展示统计 Tag 和 Table
 *
 * ── 2026-10-12 修复：本文件此前**从未真正执行过任何用例**（vitest 报
 * 「Failed Suites / No test suite found」），组件级测试形同虚设。叠加三因：
 *   (1) `describe.skipIf(!hasDom, "RejectionCoverageTab", () => {...})` 参数错位：
 *       `describe.skipIf(cond)` 返回可链式函数并**只接受条件**作为参数，
 *       suite 名必须传给返回的函数。原写法把名字传给了 skipIf，
 *       最终执行 `describe(回调)` —— 回调被当成 suite 的 options，
 *       vitest 从未注册任何 suite。正解：`describe.skipIf(cond)("名称", fn)`。
 *   (2) `vi.mock` 工厂的 TDZ：本文件**静态** import 了组件（ESM 中 import
 *       先于模块体执行），被 mock 的 `../../api` 在顶层 `const coverageMock`
 *       初始化之前就被求值 → ReferenceError，收集阶段直接崩溃。
 *       改用 `vi.hoisted()` 提升 mock 实例。
 *   (3) 断言用了未安装的 `@testing-library/jest-dom` matcher
 *       `toBeInTheDocument`（Invalid Chai property），统一改为 vitest 原生断言。
 * 另外原 `vi.mock("../../utils", ...)` 用单键桩整体替换了 1500+ 行的 utils 模块，
 * 会让任何经由 utils 的间接依赖拿到 undefined；改为透传原模块后只覆盖目标导出。
 */
import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen, waitFor, cleanup } from "@testing-library/react";

const hasDom = typeof window !== "undefined" && typeof document !== "undefined";

import RejectionCoverageTab from "./RejectionCoverageTab";

// vi.hoisted：mock 实例与 vi.mock 工厂一并提升，规避上面的 TDZ。
const { coverageMock } = vi.hoisted(() => ({ coverageMock: vi.fn() }));

const DEFAULT_COVERAGE = async (_pid: number) => ({
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
});

coverageMock.mockImplementation(DEFAULT_COVERAGE);

vi.mock("../../api", () => ({
  getRejectionCoverage: coverageMock,
}));

vi.mock("../../utils", async (importOriginal) => {
  const actual = await importOriginal<Record<string, unknown>>();
  return {
    ...actual,
    extractApiError: (e: unknown) =>
      e instanceof Error ? e.message : String(e),
  };
});

describe.skipIf(!hasDom)("RejectionCoverageTab", () => {
  afterEach(() => {
    cleanup();
    // mockReset 会连实现一起清掉，必须显式恢复默认实现，
    // 否则前一个用例里的 mockResolvedValue 会污染后续用例。
    coverageMock.mockReset();
    coverageMock.mockImplementation(DEFAULT_COVERAGE);
  });

  it("可挂载，调 getRejectionCoverage", async () => {
    render(<RejectionCoverageTab projectId={1} />);
    await waitFor(() => {
      expect(coverageMock).toHaveBeenCalledWith(1);
      // 组件当前标题文案为「废标条款应答覆盖率」；原用例写的「废标覆盖率」
      // 是更早版本的文案，在本文件能执行之前就已失配。
      expect(screen.getByText(/废标条款应答覆盖率/)).toBeTruthy();
    });
  });

  it("no_checks 状态展示优雅提示", async () => {
    coverageMock.mockResolvedValue({
      status: "no_checks",
      scanned_clauses: 0,
      full_chars: 0,
      threshold: 200,
      weak_count: 0,
      empty_count: 0,
      items: [],
    });
    render(<RejectionCoverageTab projectId={1} />);
    await waitFor(() => {
      expect(screen.getByText(/尚未运行过废标检查/)).toBeTruthy();
    });
  });

  it("正常覆盖态展示统计 Tag 和表格", async () => {
    render(<RejectionCoverageTab projectId={1} />);
    await waitFor(() => {
      expect(screen.getByText(/废标条款总数 5/)).toBeTruthy();
      // 「已覆盖」同时出现在 Alert 描述句与状态 Tag 里，getByText 会抛
      // "Found multiple elements"，改用 getAllByText 断言存在性。
      expect(screen.getAllByText(/已覆盖/).length).toBeGreaterThan(0);
      expect(screen.getAllByText(/覆盖较弱/).length).toBeGreaterThan(0);
      expect(screen.getByText(/工期承诺/)).toBeTruthy();
      expect(screen.getByText(/项目经理资质/)).toBeTruthy();
    });
  });
});