// @vitest-environment jsdom
/**
 * 一致性校核 —— 冲突来源（rule/ai）区分展示组件级测试（BUG-FIX 2026-10-15）。
 *
 * 背景：`consistency_conflicts` 同时承载确定性规则冲突与 AI 语义审计发现（后者
 * `conflict_id` 以 `AI-` 开头、`values_json` 恒为 `'[]'`）。修前前端无法区分：
 *   ① 把 AI 发现的 `fact_name`（最长 100 字的自由文本 issue）当「事实项」加粗渲染；
 *   ② 「自动修复」按钮对 AI 发现仍可点，而 `apply_autofix` 因 `values=[]` 必然
 *      回 `no_change`，用户反复点、反复看到「没有需要替换的旧值」；
 *   ③ 摘要卡片把 AI 发现混进「中等冲突」分级，数字被虚高。
 *
 * 本轮行为不变量：
 *   1. AI 发现行渲染紫色「AI 发现」标签，且「冲突数值」列不再是空的；
 *   2. AI 发现的「自动修复」按钮被禁用（没有结构化取值可统一），「修复建议」
 *      仍可用且给出 AI 原文建议（target_value 为 null →「采纳建议并修复」禁用）；
 *   3. 摘要卡片标注「其中 N 条为 AI 语义发现（需人工核对）」。
 */
import { describe, it, expect, vi, beforeAll, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, cleanup } from "@testing-library/react";

const hasDom = typeof window !== "undefined" && typeof document !== "undefined";

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

const AI_FINDING = {
  conflict_id: "AI-1-a1b2c3d4e5f6",
  fact_name: "第四章施工现场描述与全局事实不一致（修复建议：统一工期口径）",
  values: [],
  sources: ["section_3"],
  severity: "medium",
  description: "第四章施工现场描述与全局事实不一致（修复建议：统一工期口径）",
  state: "open",
  resolved: 0,
  source_kind: "ai",
};

const AI_SUGGESTION_TEXT =
  "第四章施工现场描述与全局事实不一致\n（该冲突为 AI 语义发现，无结构化取值、无法自动统一，请人工核对上述建议后处理）";

const getConflictsMock = vi.fn(async () => ({
  status: "ok",
  count: 2,
  filter: "open",
  conflicts: [AI_FINDING, RULE_CONFLICT],
}));

const getAuditSummaryMock = vi.fn(async () => ({
  status: "ok",
  summary: {
    latest_audit: { audit_time: "2026-10-15 10:00:00" },
    unresolved_conflicts: 2,
    resolved_conflicts: 0,
    conflicts_by_severity: { high: 1, medium: 1 },
    conflicts_by_severity_rule: { high: 1 },
    unresolved_rule_conflicts: 1,
    unresolved_ai_findings: 1,
    resolved_ai_findings: 0,
    audit_source: "rule",
  },
}));

const getFixSuggestionMock = vi.fn(async () => ({
  status: "ok",
  suggestion: AI_SUGGESTION_TEXT,
  target_value: null,
}));

vi.mock("../../api", () => ({
  getConflicts: getConflictsMock,
  getAuditSummary: getAuditSummaryMock,
  runConsistencyAudit: vi.fn(async () => ({ status: "ok", audit_status: "completed", conflicts: [] })),
  getFixSuggestion: getFixSuggestionMock,
  applyAutoFix: vi.fn(async () => ({ status: "no_change", message: "无变化" })),
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
describe.skipIf(!hasDom)("冲突来源（rule/ai）区分展示", () => {
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
    getFixSuggestionMock.mockClear();
  });

  afterEach(() => {
    cleanup();
  });

  it("AI 发现行：紫色「AI 发现」标签 + 冲突数值列不再是空洞", { timeout: 120_000 }, async () => {
    render(<ConflictListTab projectId={1} />);
    // rc-table 在 jsdom 下渲染极慢：先等表格行里的短标签，再断言其余不变式。
    // AI 发现的 issue 原文被 UI 截断为「前 24 字 + …」，但悬浮 title 保留全文
    //（后端最长 100 字）—— 用 getByTitle 钉住「截断 + 悬浮可读全文」这一行为。
    expect(await screen.findByText("AI 发现", undefined, { timeout: 90_000 })).toBeTruthy();
    expect(screen.getByText("工期")).toBeTruthy();
    // 截断的「事实项」文本带悬浮 title=完整 issue；同屏其它单元格也可能复用该
    // title（例如描述列），故用 getAllByTitle 断言「至少一处可悬浮读全文」。
    expect(screen.getAllByTitle(AI_FINDING.fact_name).length).toBeGreaterThan(0);
    expect(screen.getByText(/无结构化取值/)).toBeTruthy();
  });

  it("AI 发现行的「自动修复」被禁用，规则冲突行的可用", { timeout: 120_000 }, async () => {
    render(<ConflictListTab projectId={1} />);
    await screen.findByText("AI 发现", undefined, { timeout: 60_000 });
    const buttons = Array.from(document.querySelectorAll("button"));
    const aiRowButton = buttons.find(
      (b) => b.textContent === "自动修复" && b.title?.includes("AI 语义发现没有结构化取值"),
    );
    expect(aiRowButton).toBeTruthy();
    expect((aiRowButton as HTMLButtonElement).disabled).toBe(true);
    const ruleButton = buttons.find((b) => b.textContent === "自动修复" && !(b.title || "").includes("AI 语义发现"));
    expect(ruleButton).toBeTruthy();
    expect((ruleButton as HTMLButtonElement).disabled).toBe(false);
  });

  it("AI 发现的「修复建议」给出原文，且「采纳建议并修复」被禁用", { timeout: 120_000 }, async () => {
    render(<ConflictListTab projectId={1} />);
    await screen.findByText("AI 发现", undefined, { timeout: 60_000 });
    // 两行各有「修复建议」按钮 —— 点第一个（列表序默认 AI 行在前）
    fireEvent.click((await screen.findAllByText("修复建议", undefined, { timeout: 60_000 }))[0]);
    expect(await screen.findByText(/AI 语义发现，无结构化取值/, undefined, { timeout: 60_000 })).toBeTruthy();
    const adoptBtn = Array.from(document.querySelectorAll(".ant-modal button"))
      .find((b) => b.textContent?.includes("采纳建议并修复"));
    expect(adoptBtn).toBeTruthy();
    expect((adoptBtn as HTMLButtonElement).disabled).toBe(true);
  });

  it("摘要卡片标注「其中 N 条为 AI 语义发现」", { timeout: 120_000 }, async () => {
    render(<ConflictListTab projectId={1} />);
    expect(await screen.findByText(/其中 1 条为 AI 语义发现/, undefined, { timeout: 60_000 })).toBeTruthy();
  });
});