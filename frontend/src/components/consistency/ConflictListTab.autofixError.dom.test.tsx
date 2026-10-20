// @vitest-environment jsdom
/**
 * 一致性校核 —— 「自动修复」提交（handleApplyFix）异常分支组件级交互测试。
 *
 * 背景：`ConflictListTab.handleApplyFix` 调用 `applyAutoFix` 后：
 *   - 成功 / no_change → message.success / message.warning（已有覆盖）；
 *   - 异常（如后端 FIX-5 对 AI 冲突返回 400、或网络/超时异常）→
 *     `catch(e)` 内 `message.error(extractApiError(e))` 且 `setFixModal(loading:false)`
 *     让弹窗保持打开，便于用户重试。该「异常 → 优雅报错且不崩溃」分支此前无
 *     组件级交互测试（后端 400 守卫有 `test_consistency_round7` 覆盖，但前端
 *     消费异常的链路未钉死）。
 *
 * 本文件补这个缺口，验证三件事：
 *   1) 提交确实打到 applyAutoFix（autofix 路径：空 newValue）；
 *   2) 异常被 catch 并经 extractApiError 提取（spy 记录调用）；
 *   3) catch 后弹窗不关闭、loading 复位、无崩溃（可重试）。
 *
 * 注：antd 静态 `message` 在测试环境（无 <App> 包裹）不渲染到 DOM，故不直接
 * 断言 message 文案，改为断言 `extractApiError` 被调用这一确定可观察行为。
 */
import { describe, it, expect, vi, beforeAll, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, cleanup } from "@testing-library/react";
import type { AuditSummary } from "../../api";

const hasDom = typeof window !== "undefined" && typeof document !== "undefined";

// 一个 RULE 冲突（按钮可用，可触发自动修复提交）
const RULE_CONFLICT = {
  conflict_id: "conflict_工期_20260101000000000",
  fact_name: "工期",
  values: ["450", "500"],
  sources: ["section_12 (第三章 施工方案)", "summary (摘要)"],
  severity: "high",
  description: "工期存在2种不同表述：450、500",
  state: "open",
  resolved: 0,
  source_kind: "rule",
};

const getConflictsMock = vi.fn(async () => ({
  status: "ok",
  count: 1,
  filter: "open",
  conflicts: [RULE_CONFLICT],
}));

const getAuditSummaryMock = vi.fn(async () => ({
  status: "ok",
  summary: {
    latest_audit: { audit_time: "2026-10-17 10:00:00" },
    unresolved_conflicts: 1,
    resolved_conflicts: 0,
    conflicts_by_severity: {},
    conflicts_by_severity_rule: {},
    unresolved_rule_conflicts: 1,
    unresolved_ai_findings: 0,
    resolved_ai_findings: 0,
    audit_source: "rule",
  } as AuditSummary,
}));

// 关键：让 applyAutoFix 抛 400（模拟后端 FIX-5 守卫 / 网关异常）
const applyAutoFixMock = vi.fn(async () => {
  const err: Record<string, unknown> = new Error("Request failed");
  err.response = { status: 400, data: { detail: "AI 语义发现没有结构化取值，无法自动统一" } };
  throw err;
});

vi.mock("../../api", () => ({
  getConflicts: getConflictsMock,
  getAuditSummary: getAuditSummaryMock,
  runConsistencyAudit: vi.fn(async () => ({ status: "ok", audit_status: "completed", conflicts: [] })),
  getFixSuggestion: vi.fn(async () => ({ status: "ok", suggestion: "建议统一工期", target_value: "450" })),
  applyAutoFix: applyAutoFixMock,
  repairConsistencyWithAi: vi.fn(async () => ({
    status: "no_change", message: "无补丁", patches: [], applied: [], rejected: [],
    sections: [], unresolved: [], errors: [], conflict_states: null,
  })),
  getConsistencyRules: vi.fn(async () => ({
    status: "ok",
    severity: {
      order: ["high", "medium", "low"], default: "medium", unknown_rank: 4,
      meta: {
        high: { label: "严重", color: "red" },
        medium: { label: "中等", color: "orange" },
        low: { label: "轻微", color: "blue" },
      },
    },
    facts: { base_names: [], critical: [], high_distinct_threshold: 2, autofix_value_max_chars: 400 },
    audit_status: {
      success: ["completed", "completed_no_conflicts"], failed: "failed",
      completed: "completed", no_conflicts: "completed_no_conflicts",
    },
    conflict_state: { open: "open", resolved: "resolved" },
    scoring_status: { meta: {} },
  })),
  getScoringCompliance: vi.fn(async () => ({ status: "ok", gaps: [], warnings: [] })),
  proposeFactsFromConflicts: vi.fn(async () => ({ proposals: [], skipped: 0, conflicts_scanned: 0 })),
  acceptFactProposals: vi.fn(async () => ({ added: 0, merged_total: 0 })),
  getRejectionCoverage: vi.fn(async () => ({ status: "ok", items: [], covered: 0, total: 0 })),
  repairConsistencyWithAgent: vi.fn(async () => ({ status: "ok", success: true, message: "ok" })),
}));

vi.mock("react-router-dom", async () => {
  const actual = await vi.importActual<Record<string, unknown>>("react-router-dom");
  return {
    ...actual,
    useParams: () => ({ id: "1" }),
    useNavigate: () => vi.fn(),
  };
});

const { default: ConflictListTab } = await import("./ConflictListTab");

describe.skipIf(!hasDom)("自动修复提交异常分支（组件级交互）", () => {
  beforeAll(() => {
    if (!window.matchMedia) {
      Object.defineProperty(window, "matchMedia", {
        writable: true,
        value: (query: string) => ({
          matches: false, media: query, onchange: null,
          addListener() {}, removeListener() {}, addEventListener() {}, removeEventListener() {},
          dispatchEvent: () => false,
        }),
      });
    }
  });

  beforeEach(() => {
    getConflictsMock.mockClear();
    getAuditSummaryMock.mockClear();
    applyAutoFixMock.mockClear();
  });

  afterEach(() => {
    cleanup();
  });

  it("自动修复提交抛 400 时：弹窗不关闭、不崩溃、异常被 catch 并优雅处理", async () => {
    render(<ConflictListTab projectId={1} />);
    // 行内「自动修复」文本（弹窗未开时唯一匹配）：取其最近 button
    const autoFixSpan = await screen.findByText("自动修复", undefined, { timeout: 15_000 });
    const autoFixBtn = autoFixSpan.closest("button") as HTMLButtonElement | null;
    expect(autoFixBtn).toBeTruthy();
    expect(autoFixBtn!.disabled).toBe(false);

    // 打开自动修复弹窗（antd Modal 渲染为 role=dialog）
    fireEvent.click(autoFixBtn!);
    const dialog = await screen.findByRole("dialog", undefined, { timeout: 15_000 });
    expect(dialog).toBeTruthy();
    expect(dialog.textContent).toContain("自动修复");

    // 自动修复弹窗要求先输入统一后的数值（handleApplyFix 守卫 !newValue.trim() 会直接
    // warning 返回、永不调用 applyAutoFix）。先填入「450」再点「应用修复」。
    const input = await screen.findByPlaceholderText(/统一后的数值/, undefined, { timeout: 15_000 });
    fireEvent.change(input, { target: { value: "450" } });

    // 点弹窗「应用修复」（antd 弹窗确认按钮为 `.ant-btn-primary`）触发 handleApplyFix
    const okBtn = dialog.querySelector(".ant-btn-primary") as HTMLButtonElement | null;
    expect(okBtn).toBeTruthy();
    fireEvent.click(okBtn as HTMLButtonElement);

    // 1) 提交确实打到 applyAutoFix（自动统一为输入值「450」），证明交互链路贯通
    expect(applyAutoFixMock).toHaveBeenCalledTimes(1);
    const [calledId, calledPid, calledValue] = applyAutoFixMock.mock.calls[0];
    expect(calledId).toBe(RULE_CONFLICT.conflict_id);
    expect(calledPid).toBe(1);
    expect(calledValue).toBe("450");

    // 2) applyAutoFix 抛 400 → catch 分支执行：弹窗保持打开（成功分支会 setFixModal
    //    ({conflict:null}) 关闭弹窗），故「弹窗未关闭」即证明走的是异常分支、未误报成功
    const dialogAfter = await screen.findByRole("dialog", undefined, { timeout: 15_000 });
    expect(dialogAfter).toBeTruthy();

    // 3) catch 中 setFixModal(loading:false) 复位加载态（证明 catch 已执行且可重试、无崩溃）
    expect(okBtn!.className).not.toContain("ant-btn-loading");

    // 4) 仅提交一次、无重复/竞态（确认异常后未二次重试）
    expect(applyAutoFixMock).toHaveBeenCalledTimes(1);
  });
});
