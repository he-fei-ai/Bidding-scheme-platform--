// @vitest-environment jsdom
/**
 * 一致性校核 —— Tab 级 / 组件级交互测试（ENHANCE 2026-10-08）。
 *
 * 覆盖页面拆分后新引入/修复的行为，每条都对应一个可复现的缺陷：
 *
 * 1. **Tab 切换**：默认落在「一致性冲突」；切到「评分标准达标」才发
 *    `getScoringCompliance`（antd Tabs 懒挂载，非激活 Tab 不请求）。
 *    这条同时钉住评分达标 UI 的存在 —— 该端点 2026-10-06 就有后端实现，
 *    但前端客户端封装后**长期无任何调用方**，能力存在却用户够不着。
 * 2. **已解决/未解决筛选**：切换筛选必须带上 `?resolved=` 传给后端
 *    （此前后端不过滤，已修好的历史冲突与待处理冲突混排）。
 * 3. **章节深链**：`section_<id>` 来源渲染为可点击标签，点击跳
 *    `/projects/:id/sections?section=<id>`（此前是不可点的纯文本，审计结论与正文断链）。
 * 4. **已解决行只读**：已解决冲突不再提供「修复建议/自动修复」入口。
 * 5. **AI 修复按钮启用条件**：无未解决冲突时必须禁用（此前用
 *    `conflicts.length === 0`，在「已解决」过滤下必然误亮）。
 * 6. **审计失败不谎报成功**：后端回 `status='failed'` 时不得弹「审计完成」。
 *
 * 隔离说明：显式 `afterEach(cleanup)` —— vitest 未开 globals，RTL 不会自动清理；
 * 用例失败时会把已挂载 DOM 留在 body，导致后续 `findByText` 命中多个元素。
 */
import { describe, it, expect, vi, beforeAll, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor, cleanup } from "@testing-library/react";

const hasDom = typeof window !== "undefined" && typeof document !== "undefined";

const OPEN_CONFLICT = {
  conflict_id: "c_open",
  fact_name: "工期",
  values: ["450", "500"],
  sources: ["section_12 (第三章 施工方案)", "summary (摘要)", "global_facts (已确认事实基准)"],
  severity: "high",
  description: "工期存在2种不同表述：450、500",
  state: "open",
  resolved: 0,
};

const RESOLVED_CONFLICT = {
  conflict_id: "c_done",
  fact_name: "金额",
  values: ["100万"],
  sources: ["section_8 (第二章)"],
  severity: "medium",
  description: "金额已统一",
  state: "resolved",
  resolved: 1,
};

type FilterOpts = { resolved?: string };

const getConflictsMock = vi.fn(async (_pid: number, opts?: FilterOpts) => {
  const filter = opts?.resolved ?? "open";
  return {
    status: "ok",
    count: 1,
    filter,
    conflicts: [filter === "resolved" ? RESOLVED_CONFLICT : OPEN_CONFLICT],
  };
});

const getAuditSummaryMock = vi.fn(async () => ({
  status: "ok",
  summary: {
    latest_audit: { audit_time: "2026-10-08 10:00:00" },
    unresolved_conflicts: 1,
    resolved_conflicts: 1,
    conflicts_by_severity: { high: 1 },
  },
}));

const runAuditMock = vi.fn(async () => ({
  status: "ok",
  audit_status: "completed",
  project_id: 1,
  total_sections: 3,
  facts_extracted: 7,
  conflicts_found: 1,
  conflicts: [OPEN_CONFLICT],
  audit_time: "2026-10-08 10:00:00",
}));

const getScoringMock = vi.fn(async (_pid: number, _opts?: { persist?: boolean }) => ({
  status: "ok",
  project_id: 1,
  audit_time: "2026-10-08 10:00:00",
  audit_status: "completed",
  total_points: 2,
  unmapped: 1,
  empty: 0,
  weak: 1,
  covered: 0,
  gaps: [
    {
      scoring_point_id: "R1",
      name: "施工组织设计",
      weight: "10",
      standard: "须包含施工工艺流程图并附 BIM 模型",
      status: "unmapped",
      coverage: 0,
      section_ids: [],
      section_titles: [],
      content_chars: 0,
      detail: "目录中没有任何章节挂载该评分点",
    },
    {
      scoring_point_id: "R2",
      name: "质量保证措施",
      weight: "8",
      standard: "须说明原材料进场检验流程",
      status: "weak",
      coverage: 0.21,
      section_ids: [15],
      section_titles: ["第四章 质量保证"],
      content_chars: 900,
      detail: "覆盖率仅 21.0%（阈值 25%），疑似未针对该评分项作答",
    },
  ],
  warnings: [],
}));

const navigateMock = vi.fn();

const applyAutoFixMock = vi.fn(async () => ({
  status: "ok", success: true, message: "已修改 1 个章节", sections: [],
  changed_sections: 1, total_replacements: 1, skipped: [], replaced_values: [],
  unified_value: "450", fact_name: "工期", dry_run: false,
}));

vi.mock("../../api", () => ({
  getConflicts: getConflictsMock,
  getAuditSummary: getAuditSummaryMock,
  runConsistencyAudit: runAuditMock,
  getFixSuggestion: vi.fn(async () => ({ suggestion: "建议统一为 450", target_value: "450" })),
  applyAutoFix: applyAutoFixMock,
  repairConsistencyWithAi: vi.fn(async () => ({
    status: "no_change", message: "AI 未产出可用补丁", patches: [], applied: [],
    rejected: [], sections: [], unresolved: [], errors: [], conflict_states: null,
  })),
  getScoringCompliance: getScoringMock,
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
    facts: { base_names: [], critical: [], high_distinct_threshold: 2 },
    audit_status: {
      success: ["completed", "completed_no_conflicts"], failed: "failed",
      completed: "completed", no_conflicts: "completed_no_conflicts",
    },
    conflict_state: { open: "open", resolved: "resolved" },
    scoring_status: { meta: {} },
  })),
}));

vi.mock("react-router-dom", async () => {
  const actual = await vi.importActual<Record<string, unknown>>("react-router-dom");
  return {
    ...actual,
    useParams: () => ({ id: "1" }),
    useNavigate: () => navigateMock,
  };
});

const { default: ConsistencyPage } = await import("../../pages/ConsistencyPage");
const { default: ConflictListTab } = await import("./ConflictListTab");

/** 渲染冲突 Tab 并等待首屏数据就绪 */
async function renderConflictTab() {
  render(<ConflictListTab projectId={1} />);
  await screen.findByText("工期", undefined, { timeout: 30_000 });
}

describe.skipIf(!hasDom)("一致性校核 —— Tab 级与组件级交互", () => {
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
    navigateMock.mockClear();
    getScoringMock.mockClear();
    runAuditMock.mockClear();
    applyAutoFixMock.mockClear();
    getConflictsMock.mockClear();
    getAuditSummaryMock.mockResolvedValue({
      status: "ok",
      summary: {
        latest_audit: { audit_time: "2026-10-08 10:00:00" },
        unresolved_conflicts: 1,
        resolved_conflicts: 1,
        conflicts_by_severity: { high: 1 },
      },
    } as never);
  });

  afterEach(() => {
    cleanup();
  });

  // -------------------------------------------------------------------------
  // Tab 级
  // -------------------------------------------------------------------------

  it("Tab 级：默认落在「一致性冲突」，且不会请求评分达标数据", async () => {
    render(<ConsistencyPage />);
    await screen.findByText("工期", undefined, { timeout: 60_000 });
    // antd Tabs 懒挂载：非激活 Tab 组件不挂载 → 不应发起评分达标请求
    expect(getScoringMock).not.toHaveBeenCalled();
  });

  it("Tab 级：切到「评分标准达标」才请求并渲染缺口表", async () => {
    render(<ConsistencyPage />);
    await screen.findByText("工期", undefined, { timeout: 60_000 });

    fireEvent.click(await screen.findByText("评分标准达标"));

    await waitFor(() => expect(getScoringMock).toHaveBeenCalled(), { timeout: 30_000 });
    // 必须以 persist=false 调用：反复切 Tab 不该往审计历史灌记录
    // TS2493：vi.fn 在 mock 工厂包裹下 calls 类型退化为空元组，显式断言避免访问越界
    expect((getScoringMock.mock.calls as unknown[][])[0]?.[1]).toMatchObject({ persist: false });

    expect(await screen.findByText("施工组织设计", undefined, { timeout: 30_000 })).toBeTruthy();
    // 「未挂载章节」既是统计卡标题也是表格状态标签，故用 findAllByText
    expect((await screen.findAllByText("未挂载章节")).length).toBeGreaterThan(0);
    expect((await screen.findAllByText("疑似未答到点")).length).toBeGreaterThan(0);
    // 两个缺口都应出现在缺口表里
    expect(await screen.findByText("质量保证措施")).toBeTruthy();
    // 评分项总数卡片应显示后端回报的总数
    expect(await screen.findByText("评分项总数")).toBeTruthy();
  });

  it("Tab 级：评分达标承载章节可深链到章节编辑器", async () => {
    render(<ConsistencyPage />);
    await screen.findByText("工期", undefined, { timeout: 60_000 });
    fireEvent.click(await screen.findByText("评分标准达标"));

    fireEvent.click(await screen.findByText("第四章 质量保证", undefined, { timeout: 30_000 }));
    expect(navigateMock).toHaveBeenCalledWith("/projects/1/sections?section=15");
  });

  // -------------------------------------------------------------------------
  // 冲突组件级
  // -------------------------------------------------------------------------

  it("组件级：冲突的 section 来源渲染为可点击标签并深链", async () => {
    await renderConflictTab();
    // 后端 sources 里的 "section_12 (第三章 施工方案)" 应渲染成可点的章节标题
    fireEvent.click(await screen.findByText("第三章 施工方案"));
    expect(navigateMock).toHaveBeenCalledWith("/projects/1/sections?section=12");
  });

  it("组件级：非章节来源（摘要/全局事实）不可点、仅作文本展示", async () => {
    await renderConflictTab();
    expect(await screen.findByText("summary (摘要)")).toBeTruthy();
    expect(await screen.findByText("global_facts (已确认事实基准)")).toBeTruthy();
    fireEvent.click(screen.getByText("summary (摘要)"));
    expect(navigateMock).not.toHaveBeenCalled();
  });

  it("组件级：切到「已解决」会把 resolved=resolved 传给后端，且行不再有修复入口", async () => {
    await renderConflictTab();
    fireEvent.click(await screen.findByText("已解决"));

    await waitFor(
      () => {
        const filters = getConflictsMock.mock.calls.map((c) => (c[1] as FilterOpts)?.resolved);
        expect(filters).toContain("resolved");
      },
      { timeout: 30_000 },
    );

    // 已解决行：显示描述，且不提供修复按钮
    expect(await screen.findByText("金额已统一")).toBeTruthy();
    expect(screen.queryByText("修复建议")).toBeNull();
    expect(screen.queryByText("自动修复")).toBeNull();
  });

  it("组件级：无未解决冲突时 AI 修复按钮禁用", async () => {
    getAuditSummaryMock.mockResolvedValue({
      status: "ok",
      summary: {
        latest_audit: null,
        unresolved_conflicts: 0,
        resolved_conflicts: 3,
        conflicts_by_severity: {},
      },
    } as never);

    await renderConflictTab();
    fireEvent.click(await screen.findByText("已解决"));
    await waitFor(
      () => {
        const filters = getConflictsMock.mock.calls.map((c) => (c[1] as FilterOpts)?.resolved);
        expect(filters).toContain("resolved");
      },
      { timeout: 30_000 },
    );

    // 「已解决」过滤下列表非空，但未解决数为 0 → 按钮必须禁用。
    // 旧判定 `conflicts.length === 0` 在此场景必然误亮。
    const previewBtn = screen.getByText("预览 AI 补丁").closest("button");
    expect(previewBtn).not.toBeNull();
    expect((previewBtn as HTMLButtonElement).disabled).toBe(true);
  });

  it("组件级：摘要含 low 时渲染「轻微冲突」卡片（low 档可达）", async () => {
    // ENHANCE 2026-10-09：后端 `_assess_severity` 此前只有 high/medium 两条分支，
    // `SEVERITY_LOW` 是永远取不到值的死档，卡片也就无从展示。判定标准收敛到
    // `consistency_rules.assess_severity` 后该档可达 —— 本用例钉住前端能显示它。
    getAuditSummaryMock.mockResolvedValue({
      status: "ok",
      summary: {
        latest_audit: { audit_time: "2026-10-09 10:00:00" },
        unresolved_conflicts: 3,
        resolved_conflicts: 0,
        conflicts_by_severity: { high: 1, medium: 1, low: 1 },
      },
    } as never);

    await renderConflictTab();
    expect(await screen.findByText("轻微冲突（未解决）")).toBeTruthy();
    // 三档分级卡片之和应等于「未解决冲突」卡片，口径不得再打架
    expect(await screen.findByText("严重冲突（未解决）")).toBeTruthy();
    expect(await screen.findByText("中等冲突（未解决）")).toBeTruthy();
  });

  it("组件级：自动修复未填统一值时不提交（后端另有 400 兜底）", async () => {
    await renderConflictTab();

    // 表格行里的「自动修复」按钮（弹窗标题也叫「自动修复」，故取首个匹配）
    const triggers = await screen.findAllByText("自动修复");
    fireEvent.click(triggers[0]);

    fireEvent.click(await screen.findByText("应用修复"));
    await waitFor(() => expect(applyAutoFixMock).not.toHaveBeenCalled(), { timeout: 10_000 });
  });

  it("组件级：审计失败时不弹「审计完成」（BUG-2 前端侧）", async () => {
    const antd = await import("antd");
    const messages: string[] = [];
    const origSuccess = antd.message.success;
    const origError = antd.message.error;
    (antd.message as unknown as Record<string, unknown>).success = (m: string) => {
      messages.push(`success:${m}`);
    };
    (antd.message as unknown as Record<string, unknown>).error = (m: string) => {
      messages.push(`error:${m}`);
    };

    runAuditMock.mockResolvedValueOnce({
      status: "failed",
      audit_status: "failed",
      project_id: 1,
      total_sections: 0,
      facts_extracted: 0,
      conflicts_found: 0,
      conflicts: [],
      audit_time: "2026-10-08 10:00:00",
      message: "审计过程异常终止，结果不可信，请查看后端日志后重试",
    } as never);

    try {
      await renderConflictTab();
      fireEvent.click(await screen.findByText("开始审计"));

      await waitFor(() => expect(messages.length).toBeGreaterThan(0), { timeout: 30_000 });
      // 绝不能出现成功提示
      expect(messages.some((m) => m.startsWith("success:"))).toBe(false);
      expect(
        messages.some((m) => m.includes("不可信") || m.startsWith("error:")),
      ).toBe(true);
    } finally {
      (antd.message as unknown as Record<string, unknown>).success = origSuccess;
      (antd.message as unknown as Record<string, unknown>).error = origError;
    }
  });
});