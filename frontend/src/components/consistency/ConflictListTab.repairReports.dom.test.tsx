// @vitest-environment jsdom
/**
 * ConflictListTab 组件级交互测试 —— 修复报告新契约（G-4 组件级测试缺口补齐，2026-11-02）。
 *
 * 覆盖四组行为不变量（对应后端 2026-11-02 轮次的纯新增字段与 TD-8 归一）：
 *
 * 1. **G-1 判定依据浮层**：`POST /consistency/audit` 响应的 `conflicts[].fact_evidence`
 *    随审计暂存，严重程度列出现「依据」触发钮，hover 展示命中分支人话解释与阈值。
 *    遗留项 L5（2026-11-03）：依据落库后 `GET /conflicts` 回放同样下发 —— 无需
 *    重新审计即可打开浮层；无依据行（AI 发现 / 老后端 / 脏数据）不渲染该钮。
 * 2. **TD-8 缺键防御**：`repair_with_ai` 报告缺 `patches/rejected/unresolved/errors`
 *    等集合键（历史响应/第三方调用）时不再裸取 `.length` 崩整树 —— 组件级
 *    归一后按 0 渲染（2026-10-13 Agent 链路白屏 P0 的同类隐患回归用例）。
 * 3. **F-1/F-2 新报告键**：`written_sections`（实际落库）/ `unwritten`（落库未命中）/​
 *    `ai_findings_used`（语义发现仅参考）/ `conflict_states.not_verifiable`（需人工核对）
 *    的标签与明细按真实口径渲染。
 * 4. **autofix scope_missing**：`scope_missing_section_ids`（来源声明但本项目查不到的
 *    章节）触发警告 Alert，用户不再对「只改了一半」无从排查。
 *
 * mock 模式沿用 `ConflictListTab.autofixError.dom.test.tsx`（vi.hoisted + 顶层
 * await import + matchMedia stub + react-router-dom 局部 mock）。
 */
import { describe, it, expect, vi, beforeAll, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, cleanup } from "@testing-library/react";

const hasDom = typeof window !== "undefined" && typeof document !== "undefined";

const {
  getConflictsMock,
  getAuditSummaryMock,
  runAuditMock,
  applyAutoFixMock,
  repairAiMock,
} = vi.hoisted(() => ({
  getConflictsMock: vi.fn(),
  getAuditSummaryMock: vi.fn(),
  runAuditMock: vi.fn(),
  applyAutoFixMock: vi.fn(),
  repairAiMock: vi.fn(),
}));

/** 规则冲突行（GET /conflicts 回放形态；L5 落库后回放可带 fact_evidence） */
const RULE_CONFLICT = {
  conflict_id: "conflict_gongqi_1",
  fact_name: "工期",
  values: ["450", "460"],
  sources: ["section_3"],
  severity: "high",
  description: "「工期」存在 2 种不同表述",
  source_kind: "rule",
};

/** 审计执行响应形态：同一冲突带 G-1 判定依据 */
const AUDIT_EVIDENCE = {
  severity: "high",
  reason: "「工期」是关键事实（工期/金额/资质等级），一冲突即判严重",
  critical_fact: true,
  distinct_count: 2,
  distinct_values: ["450", "460"],
  section_count: 2,
  thresholds: { high_distinct_gt: 2, low_max_sections: 1 },
  instance_count: 3,
  section_ids: [3, 4],
};

const SUMMARY = {
  latest_audit: { audit_time: "2026-11-02 10:00:00" },
  unresolved_conflicts: 1,
  resolved_conflicts: 0,
  conflicts_by_severity: { high: 1, medium: 0, low: 0 },
  conflicts_by_severity_rule: { high: 1, medium: 0, low: 0 },
  unresolved_rule_conflicts: 1,
  unresolved_ai_findings: 0,
  resolved_ai_findings: 0,
  resolved_rule_conflicts: 0,
  audit_source: "rule",
};

const installDefaultMocks = () => {
  getConflictsMock.mockResolvedValue({
    status: "ok", count: 1, filter: "open", conflicts: [RULE_CONFLICT],
  });
  getAuditSummaryMock.mockResolvedValue({ status: "ok", summary: SUMMARY });
  runAuditMock.mockResolvedValue({
    status: "ok",
    audit_status: "completed",
    project_id: 1,
    total_sections: 2,
    facts_extracted: 5,
    conflicts_found: 1,
    audit_time: "2026-11-02 10:00:01",
    conflicts: [{ ...RULE_CONFLICT, fact_evidence: AUDIT_EVIDENCE }],
    message: "审计完成",
  });
  repairAiMock.mockResolvedValue({
    status: "no_change", success: false, dry_run: true, project_id: 1,
    facts_used: 1, conflicts_used: 1, ai_findings_used: 0, batches: 0,
    patches: [], applied: [], rejected: [], sections: [], unresolved: [],
    written_sections: 0, unwritten: [], conflict_states: null, errors: [],
    message: "没有可自动修复的规则冲突",
  });
};

vi.mock("../../api", () => ({
  getConflicts: getConflictsMock,
  getAuditSummary: getAuditSummaryMock,
  runConsistencyAudit: runAuditMock,
  getFixSuggestion: vi.fn(async () => ({ status: "ok", suggestion: "建议统一工期", target_value: "450" })),
  applyAutoFix: applyAutoFixMock,
  repairConsistencyWithAi: repairAiMock,
}));

vi.mock("../../utils", async (importOriginal) => {
  const actual = await importOriginal<Record<string, unknown>>();
  return {
    ...actual,
    extractApiError: (e: unknown) => (e instanceof Error ? e.message : String(e)),
  };
});

vi.mock("react-router-dom", async () => {
  const actual = await vi.importActual<Record<string, unknown>>("react-router-dom");
  return {
    ...actual,
    useParams: () => ({ id: "1" }),
    useNavigate: () => vi.fn(),
  };
});

const { default: ConflictListTab } = await import("./ConflictListTab");

describe.skipIf(!hasDom)("ConflictListTab 修复报告新契约（组件级交互）", () => {
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
    installDefaultMocks();
  });

  afterEach(() => {
    cleanup();
  });

  // ── G-1：判定依据浮层 ──────────────────────────────────────────────

  it("审计前（列表回放）不渲染「依据」触发钮", async () => {
    render(<ConflictListTab projectId={1} />);
    await screen.findByText("工期", undefined, { timeout: 15_000 });
    expect(screen.queryByLabelText("判定依据")).toBeNull();
  });

  // L5(2026-11-03)：依据落库后，GET /conflicts 回放也带 fact_evidence ——
  // 重新进入页面/刷新后无需重新审计即可打开依据浮层。
  it("列表回放带 fact_evidence 时：无需审计即出现「依据」钮，hover 内容完整", async () => {
    getConflictsMock.mockResolvedValue({
      status: "ok", count: 1, filter: "open",
      conflicts: [{ ...RULE_CONFLICT, fact_evidence: AUDIT_EVIDENCE }],
    });
    render(<ConflictListTab projectId={1} />);
    const trigger = await screen.findByLabelText("判定依据", undefined, { timeout: 15_000 });
    fireEvent.mouseEnter(trigger);
    expect(await screen.findByText(/关键事实（工期\/金额\/资质等级）/, undefined, { timeout: 15_000 })).toBeTruthy();
    expect(document.body.textContent).toContain("命中实例 3 条");
  });

  it("审计响应带 fact_evidence 时：行内出现「依据」钮，hover 展示命中分支与阈值", async () => {
    render(<ConflictListTab projectId={1} />);
    await screen.findByText("工期", undefined, { timeout: 15_000 });

    fireEvent.click(screen.getByRole("button", { name: /开始审计/ }));
    // 审计完成 → 表格行出现判定依据触发钮
    const trigger = await screen.findByLabelText("判定依据", undefined, { timeout: 15_000 });
    fireEvent.mouseEnter(trigger);
    // 浮层内容：命中分支人话解释 + 阈值口径（rules.severity_rationale 同源）
    expect(await screen.findByText(/关键事实（工期\/金额\/资质等级）/, undefined, { timeout: 15_000 })).toBeTruthy();
    expect(document.body.textContent).toContain("命中实例 3 条");
    expect(document.body.textContent).toContain("取值数 > 2 判严重");
  });

  // ── TD-8：AI 报告缺集合键的防御 ───────────────────────────────────

  it("AI 报告缺 patches/rejected/unresolved/errors 集合键时不崩溃，按 0 渲染", async () => {
    // 历史响应形态：只有 message 与少量标量，集合键全部缺失
    repairAiMock.mockResolvedValue({
      status: "no_change", success: false, dry_run: true, project_id: 1,
      facts_used: 1, conflicts_used: 1, batches: 0,
      message: "没有可自动修复的规则冲突",
    });
    const errSpy = vi.spyOn(console, "error").mockImplementation(() => {});
    render(<ConflictListTab projectId={1} />);
    await screen.findByText("工期", undefined, { timeout: 15_000 });

    fireEvent.click(screen.getByRole("button", { name: /预览 AI 补丁/ }));
    await screen.findByText(/没有可自动修复的规则冲突/, undefined, { timeout: 15_000 });
    // 归一后按 0 渲染（修复前这里会 TypeError: reading 'length' 白屏）
    expect(screen.getAllByText(/补丁 0 条/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/可应用 0/).length).toBeGreaterThan(0);
    // 组件树未被卸载：操作区按钮仍在
    expect(screen.getByRole("button", { name: /开始审计/ })).toBeTruthy();
    const renderErrors = errSpy.mock.calls
      .map((c) => String(c[0]))
      .filter((m) => m.includes("reading 'length'") || m.includes("TypeError"));
    expect(renderErrors).toEqual([]);
    errSpy.mockRestore();
  });

  // ── F-1/F-2：AI 补丁报告新键 ──────────────────────────────────────

  it("AI 报告的 written_sections/unwritten/ai_findings_used/not_verifiable 按真实口径渲染", async () => {
    repairAiMock.mockResolvedValue({
      status: "ok", success: true, dry_run: false, project_id: 1,
      facts_used: 2, conflicts_used: 2, ai_findings_used: 3, batches: 1,
      patches: [{}, {}],
      applied: [{ section_id: "3", title: "施工方案", index: 0, reason: "", old_text: "460", new_text: "450", applied: true, error: null }],
      rejected: [],
      sections: [{ section_id: "3", title: "施工方案", applied: 1, rejected: 0 }],
      unresolved: [],
      written_sections: 2,
      unwritten: [
        { section_id: "4", reason: "章节不属于本项目或已被删除/重建，UPDATE 未命中任何行" },
      ],
      conflict_states: { resolved: 1, still_open: 0, not_verifiable: 2 },
      errors: [],
      message: "AI 修复完成",
    });
    render(<ConflictListTab projectId={1} />);
    await screen.findByText("工期", undefined, { timeout: 15_000 });

    fireEvent.click(screen.getByRole("button", { name: /AI 补丁修复/ }));
    await screen.findByText(/AI 修复完成/, undefined, { timeout: 15_000 });
    expect(screen.getAllByText(/实际落库 2 章/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/落库未命中 1 章/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/语义发现 3 条（仅参考）/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/需人工核对 2/).length).toBeGreaterThan(0);
    // 落库未命中明细区
    expect(screen.getAllByText(/UPDATE 未命中任何行/).length).toBeGreaterThan(0);
  });

  // ── F-1：autofix scope_missing 警告 ──────────────────────────────

  it("autofix 报告带 scope_missing_section_ids 时渲染来源缺失警告", async () => {
    applyAutoFixMock.mockResolvedValue({
      status: "partial",
      success: true,
      dry_run: false,
      unified_value: "450",
      fact_name: "工期",
      replaced_values: ["460"],
      sections: [{ section_id: 3, title: "施工方案", replacements: 1 }],
      changed_sections: 1,
      written_sections: 1,
      total_replacements: 1,
      skipped: [],
      filtered: [],
      scoped_section_ids: [3],
      scope_missing_section_ids: [99],
      message: "已修改 1 个章节、共 1 处；另有 1 个章节未能安全替换，冲突保持未解决，请人工处理",
    });
    render(<ConflictListTab projectId={1} />);
    const autoFixSpan = await screen.findByText("自动修复", undefined, { timeout: 15_000 });
    fireEvent.click(autoFixSpan.closest("button") as HTMLButtonElement);
    const input = await screen.findByPlaceholderText(/统一后的数值/, undefined, { timeout: 15_000 });
    fireEvent.change(input, { target: { value: "450" } });
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(dialog.querySelector(".ant-btn-primary") as HTMLButtonElement);

    // 报告卡片：来源缺失章节显式告警（修复前静默丢失）
    expect(
      await screen.findByText(/冲突来源声明的 1 个章节在本项目未找到：99/, undefined, { timeout: 15_000 }),
    ).toBeTruthy();
    // 「实际落库」口径替换「修改章节」计数
    expect(screen.getAllByText(/修改章节 1 个/).length).toBeGreaterThan(0);
  });
});
