// @vitest-environment jsdom
/**
 * ConsistencyMetricsDrawer 组件级交互测试（第 11 轮补齐，2026-11-04）。
 *
 * 背景：运维指标抽屉（遗留项 L3，2026-11-03 引入）是 components/consistency/
 * 下唯一没有专项组件测试的成员 —— 它的消费契约（GET /consistency/metrics 的
 * `metrics` / `since` / `notes`）与「点开才拉取、失败不出局、可手动刷新」的
 * 交互此前零回归防护。本轮顺带补齐 T7-3 六个运维键的中文标签（此前渲染成
 * 裸英文键名）。
 *
 * 覆盖：
 * 1. open=false 不发请求（点开才拉取，不影响首屏）；
 * 2. 打开 → 拉取 → 指标按中文标签渲染（含 T7-3 运维键）+ since/notes 展示；
 * 3. 老后端（无 since/notes）回退不展示对应区块，指标照常渲染；
 * 4. 拉取失败 → 错误 Alert，不崩、不渲染指标；
 * 5. 「刷新」按钮重新拉取（open 保持时）。
 */
import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen, waitFor, cleanup, fireEvent } from "@testing-library/react";

const hasDom = typeof window !== "undefined" && typeof document !== "undefined";

import ConsistencyMetricsDrawer from "./ConsistencyMetricsDrawer";

const { metricsMock } = vi.hoisted(() => ({
  metricsMock: vi.fn(),
}));

const DEFAULT_METRICS = async () => ({
  status: "ok",
  metrics: {
    audit_runs: 5,
    audit_failed: 1,
    ai_audit_runs: 3,
    ai_findings: 7,
    ai_findings_repaired: 2,
    ai_patch_rejected: 1,
    guard_interceptions: 0,
    audit_duration_ms_total: 12345,
    conflicts_detected_total: 9,
    autofix_applied_total: 4,
    autofix_no_change_total: 2,
    autofix_failed_total: 0,
    autofix_edits_total: 11,
  },
  since: "2026-11-04 08:00:00",
  notes: "累计值自 since（本进程启动时刻）起计，进程重启归零。",
});

metricsMock.mockImplementation(DEFAULT_METRICS);

vi.mock("../../api", () => ({
  getConsistencyMetrics: metricsMock,
  extractApiError: (e: unknown) => (e instanceof Error ? e.message : String(e)),
}));

describe.skipIf(!hasDom)("ConsistencyMetricsDrawer", () => {
  afterEach(() => {
    cleanup();
    metricsMock.mockClear();
    metricsMock.mockImplementation(DEFAULT_METRICS);
  });

  it("open=false 不发请求（点开才拉取）", () => {
    render(<ConsistencyMetricsDrawer open={false} onClose={() => {}} />);
    expect(metricsMock).not.toHaveBeenCalled();
  });

  it("打开后拉取并按中文标签渲染指标 + since/notes", async () => {
    render(<ConsistencyMetricsDrawer open onClose={() => {}} />);
    await waitFor(() => expect(metricsMock).toHaveBeenCalledTimes(1));
    // 中文标签与值分属 Descriptions 的相邻单元格，分别断言
    // （T7-3 运维键有中文标签，不再渲染裸英文键名）
    expect(screen.getByText("审计执行次数")).toBeTruthy();
    expect(screen.getByText("审计累计耗时（毫秒）")).toBeTruthy();
    expect(screen.getByText("自动修复累计改写处数")).toBeTruthy();
    expect(screen.getByText("5")).toBeTruthy();
    expect(screen.getByText("12345")).toBeTruthy();
    expect(screen.getByText("11")).toBeTruthy();
    // G-3：计数起点与口径说明
    expect(screen.getByText(/计数起点（本进程启动时刻）：2026-11-04 08:00:00/)).toBeTruthy();
    expect(screen.getByText(/进程重启归零/)).toBeTruthy();
  });

  it("老后端无 since/notes 时回退不展示对应区块", async () => {
    metricsMock.mockImplementation(async () => ({
      status: "ok",
      metrics: { audit_runs: 2 },
    }));
    render(<ConsistencyMetricsDrawer open onClose={() => {}} />);
    await waitFor(() => expect(metricsMock).toHaveBeenCalledTimes(1));
    expect(screen.getByText("审计执行次数")).toBeTruthy();
    expect(screen.getByText("2")).toBeTruthy();
    expect(screen.queryByText(/计数起点/)).toBeNull();
    // 固定的口径提示段落仍然在（组件自述文案，与后端 notes 无关）
    expect(screen.getByText(/仅用于实时观测，不作为审计结论来源/)).toBeTruthy();
  });

  it("拉取失败渲染错误 Alert，不渲染指标表", async () => {
    metricsMock.mockImplementation(async () => {
      throw new Error("网络异常");
    });
    render(<ConsistencyMetricsDrawer open onClose={() => {}} />);
    await waitFor(() => expect(screen.getByText("网络异常")).toBeTruthy());
    expect(screen.queryByText("审计执行次数")).toBeNull();
  });

  it("「刷新」按钮在 open 态下重新拉取", async () => {
    render(<ConsistencyMetricsDrawer open onClose={() => {}} />);
    await waitFor(() => expect(metricsMock).toHaveBeenCalledTimes(1));
    fireEvent.click(screen.getByRole("button", { name: /刷新/ }));
    await waitFor(() => expect(metricsMock).toHaveBeenCalledTimes(2));
  });
});
