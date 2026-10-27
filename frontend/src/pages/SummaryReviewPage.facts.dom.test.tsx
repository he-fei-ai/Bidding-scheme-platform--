// @vitest-environment jsdom
/**
 * 全局事实 Tab —— 组件级（DOM）交互测试（ENHANCE 2026-10-06）。
 *
 * 此前前端对后端下发的 `fact_conflicts`（事实集内部互斥分组）零消费：
 * api 类型有定义、后端 GET /facts 与 /generate 都下发，但页面不渲染，
 * 用户确认前看不到「项目经理：张伟」与「pm：李强」并存这类矛盾。
 * 本测试把该契约钉住：
 *   ① 事实表格按条目渲染（标签/值/占位角标）
 *   ② fact_conflicts 非空时渲染告警卡片 + 冲突取值对照
 *   ③ 无冲突时不渲染告警
 *
 * 运行条件：jsdom + @testing-library/react（已在 devDependencies）；
 * 依赖未安装时整组 skip，不阻塞纯函数测试。
 */
import { describe, it, expect, vi, beforeAll } from "vitest";

const hasDom = typeof window !== "undefined" && typeof document !== "undefined";

const FACTS = {
  items: [
    { key: "projectName", label: "项目名称", value: "示范市政工程", category: "basic", source: "ai", verified: true },
    { key: "projectManager", label: "项目经理", value: "张伟", category: "personnel", source: "ai" },
    { key: "pmAlt", label: "项目经理", value: "李强", category: "personnel", source: "manual" },
    { key: "warrantyPeriod", label: "质保期", value: "【待填写：质保期年数】", category: "schedule", source: "ai", needs_manual_input: true },
  ],
  confirmed: false,
  missing_value_policy: "placeholder" as const,
  placeholder_count: 1,
  categories: { personnel: "人员配置", schedule: "工期计划", basic: "基本信息" },
  last_audit: null,
  audited_at: null,
  fact_conflicts: [
    {
      label: "项目经理",
      key: "projectManager",
      values: [
        { key: "projectManager", label: "项目经理", value: "张伟", category: "personnel" },
        { key: "pmAlt", label: "项目经理", value: "李强", category: "personnel" },
      ],
    },
  ],
};

vi.mock("../api", () => ({
  getProject: vi.fn(async () => ({ id: 1, name: "测试项目" })),
  getAnalysis: vi.fn(async () => ({
    summary_json: "摘要",
    scoring_points_json: "[]",
    boq_json: "[]",
    work_interface_json: "[]",
    construction_method_json: "[]",
    confirmed_summary: true,
    confirmed_scoring: true,
    confirmed_boq: false,
    confirmed_work_interface: false,
    confirmed_construction_method: false,
  })),
  generateSummary: vi.fn(),
  generateScoring: vi.fn(),
  confirmSummary: vi.fn(),
  confirmScoring: vi.fn(),
  clearAnalysis: vi.fn(),
  analyzeBoq: vi.fn(),
  confirmBoq: vi.fn(),
  analyzeWorkInterface: vi.fn(),
  confirmWorkInterface: vi.fn(),
  analyzeConstructionMethod: vi.fn(),
  confirmConstructionMethod: vi.fn(),
  getFacts: vi.fn(async () => FACTS),
  saveFacts: vi.fn(async () => ({ ok: true, fact_conflicts: [] })),
  generateFacts: vi.fn(),
  confirmFacts: vi.fn(),
  auditFactsConsistency: vi.fn(),
  // R9 2026-10-26：页面新增专项方案解析卡片（loadData 会读取 scheme_extraction，
  // 有结果时再取推荐模板），mock 必须补齐这两个导出，否则组件渲染时调用 undefined。
  generateSchemeExtraction: vi.fn(),
  fetchSchemeOutlineTemplate: vi.fn(),
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

describe.skipIf(!hasDom)("SummaryReviewPage 事实 Tab 组件级交互", () => {
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

  it("渲染事实表格 + 内部矛盾告警卡片（fact_conflicts 消费）", async () => {
    const rtl = await loadRtl();
    if (!rtl) return;

    const Page = (await import("./SummaryReviewPage")).default;
    rtl.render(<Page />);

    // ① 事实表格已渲染（值列是受控 Input，value 不成文本节点；
    //    以表头/操作按钮作为表格在场的锚点）
    expect(await rtl.screen.findByText("新增事实")).toBeTruthy();

    // ② BUG-FIX 2026-10-06：后端下发的 fact_conflicts 必须被渲染成告警卡片
    expect(
      await rtl.screen.findByText(/检测到 \d+ 组事实内部矛盾/),
    ).toBeTruthy();
    // 冲突对照表里两个互斥值都要展示在 Tag 里（文本节点可查）
    expect(rtl.screen.getAllByText("张伟").length).toBeGreaterThan(0);
    expect(rtl.screen.getAllByText("李强").length).toBeGreaterThan(0);

    // ③ 占位条目渲染「待人工填写」角标
    expect(rtl.screen.getByText("待人工填写")).toBeTruthy();

    rtl.cleanup();
  });

  it("无冲突时不渲染矛盾告警", async () => {
    const rtl = await loadRtl();
    if (!rtl) return;

    const { getFacts } = await import("../api");
    // 用 mockResolvedValue 覆盖所有调用（组件初始化可能多次调用 getFacts）
    (getFacts as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...FACTS,
      fact_conflicts: [],
    });

    const Page = (await import("./SummaryReviewPage")).default;
    rtl.render(<Page />);

    expect(await rtl.screen.findByText("新增事实")).toBeTruthy();
    // 等待状态更新完成后再断言
    await rtl.waitFor(() => {
      expect(rtl.screen.queryByText(/检测到 \d+ 组事实内部矛盾/)).toBeNull();
    });

    rtl.cleanup();
    // 恢复默认 mock
    (getFacts as unknown as ReturnType<typeof vi.fn>).mockResolvedValue(FACTS);
  });
});
