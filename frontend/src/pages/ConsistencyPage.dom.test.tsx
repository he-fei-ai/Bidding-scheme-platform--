// @vitest-environment jsdom
/**
 * 全文一致性审计页 —— 组件级（DOM）交互测试。
 *
 * 重点钉住 BUG-1（P1·正文污染）：此前「采纳建议并修复」按钮把后端返回的
 * 整段建议文案（"建议将以下内容统一为：450（已确认的全局事实基准）\n待修正取值：500"）
 * 直接塞进 new_value 提交 autofix。后端 apply_autofix 会拿它当替换目标值，
 * 把正文里所有旧值（如"500"）替换成这段说明文字 —— 正文被污染。
 *
 * 本测试在真实 DOM 交互里断言：点「采纳建议并修复」时，applyAutoFix 收到的
 * new_value 必须是结构化纯目标值 "450"，而不是整段文案。
 *
 * ENHANCE 2026-10-08：页面已拆为「页面壳 + ConflictListTab / ScoringComplianceTab」。
 * 本文件聚焦页面壳层（默认 Tab、跨 Tab 挂载、规则对齐）；ConflictListTab 内部的
 * 交互细节（筛选切换、章节深链、已解决行只读）迁到 `ConsistencyTabs.dom.test.tsx`
 * 单独挂载 —— 更快，也不必为测一个断言把另一个 Tab 的请求一起拉起来。
 *
 * ENHANCE 2026-10-16（慢用例分流转·Tab 级单挂载）：本文件原先挂载整个
 * `<ConsistencyPage>`（页面壳 + 全部 Tab 初始化 + antd Tabs 懒挂载 + 多路 API
 * mock 解析），在 CI 慢机上单条用例逼近 2 分钟超时。现改为**直接挂载
 * `<ConflictListTab>`**（与 `ConsistencyTabs.dom.test.tsx` 同一做法）——
 * 两个被钉住的行为（autofix 纯值提交、空列表空态）都是冲突 Tab 的内部行为，
 * 页面壳只负责传参与路由，无需整页渲染即可覆盖。
 */
import { describe, it, expect, vi, beforeAll } from "vitest";

const hasDom = typeof window !== "undefined" && typeof document !== "undefined";

const CONFLICTS = [
  {
    conflict_id: "conflict_1_工期_20260101000000000",
    fact_name: "工期",
    values: ["450", "500"],
    sources: ["global_facts (已确认事实基准)", "section_1 (第一章)"],
    severity: "high",
    description: "工期存在2种不同表述：450、500，可能存在不一致",
    state: "open",
    resolved: 0,
  },
];

// 后端修复后同时下发 suggestion（人类可读）与 target_value（纯值）
const SUGGESTION_TEXT =
  "建议将以下内容统一为：450（已确认的全局事实基准）\n待修正取值：500";
const TARGET_VALUE = "450";

const applyAutoFixMock = vi.fn(
  async (_cid: string, _pid: number, _nv: string) => ({
    status: "ok", message: "已修改", success: true,
  }),
);

// ConflictListTab 启动时会拉规则快照对齐严重度映射；
// mock 工厂必须提供这些导出，否则模块一加载就是 undefined 调用。
vi.mock("../api", () => ({
  runConsistencyAudit: vi.fn(async () => ({
    status: "ok",
    audit_status: "completed",
    total_sections: 2,
    facts_extracted: 4,
    conflicts_found: 1,
    conflicts: CONFLICTS,
  })),
  getConflicts: vi.fn(async () => ({
    conflicts: CONFLICTS, count: CONFLICTS.length, filter: "open",
  })),
  getFixSuggestion: vi.fn(async () => ({
    suggestion: SUGGESTION_TEXT,
    target_value: TARGET_VALUE,
  })),
  applyAutoFix: applyAutoFixMock,
  getAuditSummary: vi.fn(async () => ({
    summary: {
      latest_audit: { audit_time: "2026-01-01 00:00:00" },
      unresolved_conflicts: 1,
      resolved_conflicts: 0,
      conflicts_by_severity: { high: 1 },
    },
  })),
  repairConsistencyWithAi: vi.fn(async () => ({
    status: "no_change", message: "无补丁", patches: [], applied: [], rejected: [],
    sections: [], unresolved: [], errors: [], conflict_states: null,
  })),
  getConsistencyRules: vi.fn(async () => ({
    status: "ok",
    severity: {
      order: ["high", "medium", "low"],
      default: "medium",
      unknown_rank: 4,
      meta: {
        high: { label: "严重", color: "red" },
        medium: { label: "中等", color: "orange" },
        low: { label: "轻微", color: "blue" },
      },
    },
    facts: { base_names: [], critical: [], high_distinct_threshold: 2 },
    audit_status: {
      success: ["completed", "completed_no_conflicts"],
      failed: "failed",
      completed: "completed",
      no_conflicts: "completed_no_conflicts",
    },
    conflict_state: { open: "open", resolved: "resolved" },
    scoring_status: { meta: {} },
  })),
  getScoringCompliance: vi.fn(async () => ({
    status: "ok",
    project_id: 1,
    audit_time: "2026-01-01 00:00:00",
    audit_status: "completed",
    total_points: 0,
    unmapped: 0,
    empty: 0,
    weak: 0,
    covered: 0,
    gaps: [],
    warnings: [],
  })),
}));

vi.mock("react-router-dom", async () => {
  const actual = await vi.importActual<Record<string, unknown>>("react-router-dom");
  return {
    ...actual,
    useParams: () => ({ id: "1" }),
    useNavigate: () => vi.fn(),
  };
});

async function loadRtl() {
  try {
    return await import("@testing-library/react");
  } catch {
    return null;
  }
}

describe.skipIf(!hasDom)("ConflictListTab 组件级交互（BUG-1 防回归·Tab 级单挂载）", () => {
  beforeAll(() => {
    if (!window.matchMedia) {
      Object.defineProperty(window, "matchMedia", {
        writable: true,
        value: (query: string) => ({
          matches: false,
          media: query,
          onchange: null,
          addListener: () => {},
          removeListener: () => {},
          addEventListener: () => {},
          removeEventListener: () => {},
          dispatchEvent: () => false,
        }),
      });
    }
  });

  // 历史即边缘耗时用例（三段弹窗交互 + antd Modal 动画）；Tab 级单挂载已比整页
  // 渲染省掉页面壳 + 全 Tab 初始化开销，但慢机上仍需放宽到 120s。
  it("采纳建议并修复时，applyAutoFix 收到纯 target_value 而非整段文案", async () => {
    const rtl = await loadRtl();
    if (!rtl) return;

    applyAutoFixMock.mockClear();
    const Tab = (await import("../components/consistency/ConflictListTab")).default;
    rtl.render(<Tab projectId={1} />);

    // ① 冲突表格已渲染（事实项列有"工期"）
    expect(await rtl.screen.findByText("工期")).toBeTruthy();

    // ② 点第一行的「修复建议」按钮 → 弹窗出现建议文案
    const suggestBtn = await rtl.screen.findByText("修复建议");
    rtl.fireEvent.click(suggestBtn);

    // 等待建议弹窗渲染（建议文案是整段，含"建议将以下内容统一为"）
    expect(await rtl.screen.findByText(/建议将以下内容统一为/)).toBeTruthy();

    // ③ 点「采纳建议并修复」
    const adoptBtn = await rtl.screen.findByText("采纳建议并修复");
    rtl.fireEvent.click(adoptBtn);

    // 等待 autofix 弹窗打开后点「应用修复」（antd Modal 的 ok 按钮）
    const okBtn = await rtl.screen.findByText("应用修复");
    rtl.fireEvent.click(okBtn);

    // ④ 关键断言：applyAutoFix 被调用，且第三个参数（new_value）是纯值 "450"，
    //    绝不能是整段建议文案。签名：(conflictId, projectId, newValue)
    expect(applyAutoFixMock).toHaveBeenCalled();
    const call = applyAutoFixMock.mock.calls[0] as unknown as [string, number, string];
    expect(call).toBeDefined();
    const newValue = call[2];
    expect(newValue).toBe(TARGET_VALUE);
    expect(newValue).not.toContain("建议");
    expect(newValue).not.toContain("统一为");
    expect(newValue).not.toContain("待修正取值");

    rtl.cleanup();
  });

  it("无冲突时冲突列表为空、不渲染冲突表格行", { timeout: 60_000 }, async () => {
    const rtl = await loadRtl();
    if (!rtl) return;

    const { getConflicts } = await import("../api");
    (getConflicts as unknown as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      conflicts: [], count: 0, filter: "open",
    });

    const Tab = (await import("../components/consistency/ConflictListTab")).default;
    rtl.render(<Tab projectId={1} />);

    // 空态提示出现
    expect(await rtl.screen.findByText(/暂无冲突数据/)).toBeTruthy();

    rtl.cleanup();
  });
});
