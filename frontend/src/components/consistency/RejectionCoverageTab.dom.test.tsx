// @vitest-environment jsdom
/**
 * RejectionCoverageTab 组件级交互测试（ENHANCE 2026-10-11；2026-10-12 修复加载失败；
 * 2026-10-13 修契约与 Router 缺失）。
 *
 * 覆盖：
 * 1. 可挂载，调 getRejectionCoverage
 * 2. no_checks 状态（无废标任务）优雅提示
 * 3. 正常覆盖态展示统计 Tag 和表格
 * 4. **契约一致性**：section_hits 按对象渲染（不出现 `[object Object]`）、阈值按
 *    覆盖率百分比展示、rowKey 取后端真实的 `id`、章节标签可点深链
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
 *
 * ── 2026-10-13 修复（**本文件此前把错误契约固化成了「期望值」**）：
 *   桩数据写的是 `{clause_id, section_hits: ["第三章 施工方案"], threshold: 200}`，
 *   而后端 `services/rejection_coverage.py` 真实返回
 *   `{id, section_hits: [{section_id, matched_bigrams}], threshold: 0.25}`。
 *   也就是说：测试全绿，而组件 `rowKey="clause_id"` 全表失效、
 *   `section_hits` 列渲染出 `[object Object]`、阈值被当成字数写成「200 字」——
 *   三个用户可见缺陷同时存在，且测试**在替它们背书**。
 *   现把桩数据换成后端真实结构，并新增针对这三点的断言。
 *   组件侧新增 `useNavigate`（章节深链）后必须挂 Router，故 render 一律包
 *   `MemoryRouter`。
 */
import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen, waitFor, cleanup } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

const hasDom = typeof window !== "undefined" && typeof document !== "undefined";

import RejectionCoverageTab from "./RejectionCoverageTab";

// vi.hoisted：mock 实例与 vi.mock 工厂一并提升，规避上面的 TDZ。
const { coverageMock } = vi.hoisted(() => ({ coverageMock: vi.fn() }));

// 桩数据严格照抄后端 `rejection_answer_coverage()` 的返回结构。
// 后端里：threshold 是**覆盖率比值**（默认 COVERAGE_WEAK=0.25），
// section_hits 是 `[{section_id, matched_bigrams}]` 对象数组。
const DEFAULT_COVERAGE = async (_pid: number) => ({
  status: "ok",
  scanned_clauses: 2,
  full_chars: 1200,
  threshold: 0.25,
  weak_count: 1,
  empty_count: 0,
  items: [
    {
      id: "r1",
      name: "工期承诺",
      text: "投标人必须承诺总工期不超过365日历天",
      status: "covered" as const,
      coverage: 0.95,
      section_hits: [{ section_id: 12, matched_bigrams: 37 }],
    },
    {
      id: "r2",
      name: "项目经理资质",
      text: "项目经理必须具备一级建造师资质",
      status: "weak" as const,
      coverage: 0.3,
      section_hits: [{ section_id: 4, matched_bigrams: 6 }],
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

function renderTab(projectId = 1) {
  // useNavigate 需要 Router 上下文（组件侧新增章节深链后引入）
  return render(
    <MemoryRouter>
      <RejectionCoverageTab projectId={projectId} />
    </MemoryRouter>,
  );
}

describe.skipIf(!hasDom)("RejectionCoverageTab", () => {
  afterEach(() => {
    cleanup();
    // mockReset 会连实现一起清掉，必须显式恢复默认实现，
    // 否则前一个用例里的 mockResolvedValue 会污染后续用例。
    coverageMock.mockReset();
    coverageMock.mockImplementation(DEFAULT_COVERAGE);
  });

  it("可挂载，调 getRejectionCoverage", async () => {
    renderTab();
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
      threshold: 0.25,
      weak_count: 0,
      empty_count: 0,
      items: [],
    });
    renderTab();
    await waitFor(() => {
      expect(screen.getByText(/尚未运行过废标检查/)).toBeTruthy();
    });
  });

  it("正常覆盖态展示统计 Tag 和表格", async () => {
    renderTab();
    await waitFor(() => {
      expect(screen.getByText(/废标条款总数 2/)).toBeTruthy();
      // 「已覆盖」同时出现在 Alert 描述句与状态 Tag 里，getByText 会抛
      // "Found multiple elements"，改用 getAllByText 断言存在性。
      expect(screen.getAllByText(/已覆盖/).length).toBeGreaterThan(0);
      expect(screen.getAllByText(/覆盖较弱/).length).toBeGreaterThan(0);
      expect(screen.getByText(/工期承诺/)).toBeTruthy();
      expect(screen.getByText(/项目经理资质/)).toBeTruthy();
    });
  });

  // ── BUG-FIX 2026-10-13：三条针对「契约不一致」的回归断言 ──────────

  it("section_hits 按对象渲染，不出现 [object Object]", async () => {
    renderTab();
    await waitFor(() => {
      expect(screen.getByText(/工期承诺/)).toBeTruthy();
    });
    // 修复前：后端给 [{section_id, matched_bigrams}]，组件按 string[] 直出
    // `{h}` → 页面显示 "[object Object]"。
    expect(document.body.textContent || "").not.toContain("[object Object]");
    // 现在按对象渲染成「章节 12」这样的可读标签
    expect(screen.getByText(/章节 12/)).toBeTruthy();
    expect(screen.getByText(/章节 4/)).toBeTruthy();
  });

  it("阈值按覆盖率百分比展示，而非当成字数", async () => {
    renderTab();
    await waitFor(() => {
      expect(screen.getByText(/工期承诺/)).toBeTruthy();
    });
    // 修复前写的是「单条阈值 0.25 字」/「默认 200 字」——单位完全讲错。
    const text = document.body.textContent || "";
    expect(text).not.toMatch(/阈值\s*0\.25\s*字/);
    expect(text).not.toMatch(/阈值\s*200\s*字/);
    expect(text).toContain("覆盖率阈值 0.25");
    expect(text).toContain("25%");
  });

  it("条款行以真实 id 作 rowKey（不产生 undefined 行标识）", async () => {
    // antd Table 在 rowKey 取不到值时 React 会抛 duplicate-key 警告，
    // 该警告是「rowKey 字段名写错」最直接的信号。
    const errSpy = vi.spyOn(console, "error").mockImplementation(() => {});
    renderTab();
    await waitFor(() => {
      expect(screen.getByText(/工期承诺/)).toBeTruthy();
    });
    const messages = errSpy.mock.calls.map((c) => String(c[0]));
    expect(messages.filter((m) => m.includes("same key"))).toEqual([]);
    errSpy.mockRestore();
  });
});