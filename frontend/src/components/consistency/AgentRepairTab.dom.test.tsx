// @vitest-environment jsdom
/**
 * AgentRepairTab 组件级交互测试（ENHANCE 2026-10-11；2026-10-12 修复加载失败）。
 *
 * 覆盖：
 * 1. 组件可挂载、启动修复按钮存在
 * 2. dryRun 参数透传给 API
 * 3. maxTurns 参数透传
 * 4. 成功态展示 turns / conflict_states
 *
 * ── 2026-10-12 修复：本文件此前**从未真正执行过任何用例**，vitest 报
 * 「Failed Suites / No test suite found」，等于组件级测试形同虚设。三个叠加原因：
 *
 *   (1) `describe.skipIf(!hasDom, "AgentRepairTab", () => {...})` 参数错位。
 *       `describe.skipIf(cond)` 返回的是一个**可链式调用的函数**，签名是
 *       `skipIf(condition) => (name, fn) => Suite`，不接受 suite 名作为第二参。
 *       原写法把 suite 名传给了 skipIf，导致最终执行的是
 *       `describe(回调)` —— 回调被当成 suite 的 **options 对象**，
 *       vitest 从未注册任何 suite，于是报「No test suite found」。
 *       正解：`describe.skipIf(cond)("名称", () => {...})`
 *       （同目录 ConsistencyTabs.dom.test.tsx 与 pages/FileUploadPage.dom.test.tsx
 *         用的是正确写法，可见这是本文件独有的笔误。）
 *
 *   (2) `vi.mock` 工厂的 TDZ。本文件**静态** import 了 `./AgentRepairTab`
 *       （ESM 中 import 先于模块体执行），被 mock 的 `../../api` 在顶层
 *       `const repairMock = vi.fn(...)` 之前就被工厂求值 →
 *       `ReferenceError: Cannot access 'repairMock' before initialization`，
 *       文件在**收集阶段**直接崩溃。改用 `vi.hoisted()` 提升 mock 实例。
 *
 *   (3) 断言用了 `toBeInTheDocument`。本仓库未安装
 *       `@testing-library/jest-dom`，`src/setupTests.ts` 也未注册其 matcher，
 *       所有 `toBeInTheDocument()` 都会抛
 *       `Invalid Chai property: toBeInTheDocument`。
 *       统一改为 vitest 原生断言（`toBeTruthy()`，RTL 查询找不到元素时本身即抛错），
 *       不引入新依赖，与其余 DOM 测试口径一致。
 */
import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen, waitFor, cleanup } from "@testing-library/react";

const hasDom = typeof window !== "undefined" && typeof document !== "undefined";

import AgentRepairTab from "./AgentRepairTab";

// vi.hoisted：mock 实例与 vi.mock 工厂一并提升，规避上面的 TDZ。
// 先声明为空 fn 再单独挂默认实现：若把实现写进 vi.fn(...)，TS 会由它推断出
// 形参/返回类型，后续用例传 conflict_states: null 这类「真实接口也允许」的
// 变体就会被误判为类型错误。
const { repairMock } = vi.hoisted(() => ({ repairMock: vi.fn() }));

const DEFAULT_REPAIR = async (
  _pid: number,
  opts?: { dryRun?: boolean; maxTurns?: number },
) => ({
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
});

repairMock.mockImplementation(DEFAULT_REPAIR);

vi.mock("../../api", () => ({
  repairConsistencyWithAgent: repairMock,
}));

// 注意：此处**不再**整体替换 `../../utils`。原写法只导出 extractApiError 一项，
// 会把 1500+ 行的 utils 模块替换成单键桩模块，任何经由 utils 的间接依赖都会拿到
// undefined。改为透传原模块（spread 保留全部导出），只覆盖 extractApiError。
vi.mock("../../utils", async (importOriginal) => {
  const actual = await importOriginal<Record<string, unknown>>();
  return {
    ...actual,
    extractApiError: (e: unknown) =>
      e instanceof Error ? e.message : String(e),
  };
});

describe.skipIf(!hasDom)("AgentRepairTab", () => {
  afterEach(() => {
    cleanup();
    repairMock.mockReset();
    repairMock.mockImplementation(DEFAULT_REPAIR);
  });

  it("可挂载，启动修复按钮存在", async () => {
    render(<AgentRepairTab projectId={1} />);
    await waitFor(() => {
      expect(screen.getByText(/Agent 多轮修复/)).toBeTruthy();
      expect(screen.getByRole("button", { name: /预览/ })).toBeTruthy();
      expect(screen.getByRole("button", { name: /执行修复/ })).toBeTruthy();
    });
  });

  it("dryRun 预览时 API dryRun=true", async () => {
    repairMock.mockResolvedValue({
      status: "ok",
      success: true,
      dry_run: true,
      project_id: 1,
      turns: 2,
      max_turns: 8,
      conflict_states: null,
      changed_sections: [],
      applied: [],
      rejected: [],
      unresolved: [],
      errors: [],
      message: "预览完成",
    });
    render(<AgentRepairTab projectId={1} />);
    screen.getByRole("button", { name: /预览/ }).click();
    await waitFor(() => {
      expect(repairMock).toHaveBeenCalledWith(
        1,
        expect.objectContaining({ dryRun: true }),
      );
    });
  });

  it("maxTurns 可配置并透传", async () => {
    render(<AgentRepairTab projectId={1} />);
    const input = (await screen.findByRole("spinbutton")) as HTMLInputElement;
    expect(input).toBeTruthy();
    screen.getByRole("button", { name: /预览/ }).click();
    await waitFor(() => {
      expect(repairMock).toHaveBeenCalledWith(
        1,
        expect.objectContaining({ maxTurns: expect.any(Number) }),
      );
    });
  });

  it("成功态展示 turns 和冲突统计", async () => {
    render(<AgentRepairTab projectId={1} />);
    screen.getByRole("button", { name: /执行修复/ }).click();
    await waitFor(() => {
      // 用 getAllByText：组件里同一文案可能同时出现在结果卡片与提示条，
      // 原用 getByText 在此抛 "Found multiple elements"。
      expect(screen.getAllByText(/Agent 修复完成/).length).toBeGreaterThan(0);
      expect(screen.getAllByText(/已解决 2/).length).toBeGreaterThan(0);
      expect(screen.getAllByText(/4\/8 轮/).length).toBeGreaterThan(0);
    });
  });
});