// @vitest-environment jsdom
/**
 * 一致性校核 —— 审计摘要卡片组件级测试（BUG-FIX 2026-10-15 续 / 2026-10-17）。
 *
 * 背景：`consistency_conflicts` 同时承载确定性规则冲突与 AI 语义审计发现，AI 发现
 * 参与统计会把「中等冲突」卡片顶高（2026-10-15 修复：分级卡片优先
 * `conflicts_by_severity_rule`）。此前该行为只通过 ConflictListTab 间接覆盖
 * （「其中 N 条为 AI 语义发现」一条断言），卡片自身的其余不变量没有直测。
 *
 * 本轮行为不变量：
 *   1. 分级卡片优先用 `conflicts_by_severity_rule`（只算规则冲突）；
 *      老后端不下发该字段时回退 `conflicts_by_severity`，行为与改造前一致；
 *   2. 「未解决冲突」卡片单独标注其中 AI 语义发现的条数；
 *   3. 「最近审计时间」附来源标签（rule=规则审计 / ai=AI 审计）；
 *   4. **无任何审计记录**（latest_audit=null）时不显示来源标签 ——
 *      BUG-FIX 2026-10-17：此前 `audit_source` 默认回落 'rule'，时间显示「—」
 *      却挂着「规则审计」标签，用户以为跑过一次审计。
 *   5. summary 为 null（未加载）时不渲染任何卡片。
 */
import { describe, it, expect, afterEach } from "vitest";
import { render, screen, cleanup } from "@testing-library/react";

const hasDom = typeof window !== "undefined" && typeof document !== "undefined";

import AuditSummaryCards from "./AuditSummaryCards";
import type { AuditSummary } from "../../api";

afterEach(() => {
  cleanup();
});

const RULE_ONLY_SUMMARY: AuditSummary = {
  latest_audit: { audit_time: "2026-10-17 10:00:00" },
  unresolved_conflicts: 2,
  resolved_conflicts: 1,
  conflicts_by_severity: { high: 1, medium: 1, low: 0 },
  conflicts_by_severity_rule: { high: 1, medium: 0, low: 1 },
  unresolved_rule_conflicts: 2,
  unresolved_ai_findings: 0,
  resolved_ai_findings: 0,
  audit_source: "rule",
};

const MIXED_SUMMARY: AuditSummary = {
  latest_audit: { audit_time: "2026-10-17 11:00:00" },
  unresolved_conflicts: 4,
  resolved_conflicts: 0,
  conflicts_by_severity: { high: 1, medium: 3, low: 0 },
  conflicts_by_severity_rule: { high: 1, medium: 1, low: 0 },
  unresolved_rule_conflicts: 2,
  unresolved_ai_findings: 2,
  resolved_ai_findings: 1,
  audit_source: "ai",
};

const LEGACY_SUMMARY: AuditSummary = {
  latest_audit: { audit_time: "2026-10-08 10:00:00" },
  unresolved_conflicts: 2,
  resolved_conflicts: 0,
  conflicts_by_severity: { high: 1, medium: 1, low: 0 },
};

const NO_AUDIT_SUMMARY: AuditSummary = {
  latest_audit: null,
  unresolved_conflicts: 0,
  resolved_conflicts: 0,
  conflicts_by_severity: {},
  audit_source: "rule",
};

describe.skipIf(!hasDom)("AuditSummaryCards", () => {
  it("分级卡片优先 rule 口径，AI 发现不顶高「中等冲突」", () => {
    render(<AuditSummaryCards summary={MIXED_SUMMARY} />);
    // rule 口径：high=1 / medium=1 / low=0（老口径混排会是 medium=3）
    expect(screen.getByText("严重冲突（未解决）").parentElement?.textContent).toContain("1");
    const mediumCard = screen.getByText("中等冲突（未解决）").parentElement;
    expect(mediumCard?.textContent).toContain("1");
    expect(mediumCard?.textContent).not.toContain("3");
    // 未解决总数 4 = 规则 2 + AI 2，卡片单独标注 AI 条数
    const unresolvedCard = screen.getByText("未解决冲突").parentElement;
    expect(unresolvedCard?.textContent).toContain("4");
    expect(screen.getByText(/其中 2 条为 AI 语义发现/)).toBeTruthy();
  });

  it("老后端无 rule 拆分字段时回退混排口径（向后兼容）", () => {
    render(<AuditSummaryCards summary={LEGACY_SUMMARY} />);
    const mediumCard = screen.getByText("中等冲突（未解决）").parentElement;
    expect(mediumCard?.textContent).toContain("1");
    // 老后端无 AI 计数字段 → 不渲染「其中 N 条」标注
    expect(screen.queryByText(/其中 \d+ 条为 AI 语义发现/)).toBeNull();
  });

  it("来源标签：rule=规则审计 / ai=AI 审计", () => {
    const { unmount } = render(<AuditSummaryCards summary={RULE_ONLY_SUMMARY} />);
    expect(screen.getByText("规则审计")).toBeTruthy();
    unmount();
    render(<AuditSummaryCards summary={MIXED_SUMMARY} />);
    expect(screen.getByText("AI 审计")).toBeTruthy();
  });

  it("无审计记录时不显示来源标签（时间显示 —）", () => {
    render(<AuditSummaryCards summary={NO_AUDIT_SUMMARY} />);
    // 最近审计时间为占位符
    expect(screen.getByText("最近审计时间").parentElement?.textContent).toContain("—");
    // 但不得出现「规则审计」/「AI 审计」来源标签
    expect(screen.queryByText("规则审计")).toBeNull();
    expect(screen.queryByText("AI 审计")).toBeNull();
  });

  it("summary 为 null 时不渲染卡片", () => {
    const { container } = render(<AuditSummaryCards summary={null} />);
    expect(container.firstChild).toBeNull();
  });
});
