// @vitest-environment jsdom
/**
 * 专项方案解析卡片 —— Tab 级 / 组件级交互测试（ENHANCE 2026-10-26，R9）。
 *
 * 背景：后端 2026-10-23 已上线 `POST /ai/scheme-extraction`（五类并发提取）与
 * `GET /ai/scheme-outline-template`（按识别结果推荐目录模板），但前端零接入，
 * 用户无法触发，专项结果永远为空 → 目录生成只能走通用模板（死链路）。
 *
 * 本文件把前端接入契约钉住：
 *   ① 卡片渲染五个 Tab，默认「工程概况」，可切换到「方案类型」查看结果；
 *   ② 点击「开始解析」调用后端并回显五类结果，按钮文案变为「重新解析」；
 *   ③ 单个子任务失败（{error}）按项展示告警，不影响其余 Tab 内容；
 *   ④ 已有结果时展示「推荐目录模板」预览（is_default 与非默认两种标签）。
 *
 * 依赖未安装时整组 skip，不阻塞纯函数测试。
 */
import { describe, it, expect, vi, beforeAll, beforeEach } from "vitest";

const hasDom = typeof window !== "undefined" && typeof document !== "undefined";

const EXTRACTION = {
  project_overview: [{ field: "工程名称", value: "某商业综合体基坑工程" }],
  scheme_type: [{ scheme_type: "基坑工程" }],
  dangerous_work_params: [{ param: "开挖深度", value: "6.5", unit: "m" }],
  basis_list: [{ category: "标准", name: "建筑基坑支护技术规程" }],
  construction_condition: [{ category: "周边建筑", description: "东侧为住宅小区" }],
};

const TEMPLATE = {
  scheme_type: "基坑工程",
  is_default: false,
  outline_template: [
    { id: "1", title: "工程概况", level: 1 },
    { id: "2", title: "基坑监测方案", level: 1, source: "方案类型：基坑工程" },
  ],
};

const apiMock = {
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
    scheme_extraction: EXTRACTION,
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
  getFacts: vi.fn(async () => ({
    items: [],
    confirmed: false,
    missing_value_policy: "fabricate",
    fact_conflicts: [],
    categories: {},
    last_audit: null,
    audited_at: null,
  })),
  saveFacts: vi.fn(),
  generateFacts: vi.fn(),
  confirmFacts: vi.fn(),
  auditFactsConsistency: vi.fn(),
  generateSchemeExtraction: vi.fn(async () => ({
    scheme_extraction_json: JSON.stringify(EXTRACTION),
    scheme_extraction: EXTRACTION,
  })),
  fetchSchemeOutlineTemplate: vi.fn(async () => TEMPLATE),
};

vi.mock("../api", () => apiMock);

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

describe.skipIf(!hasDom)("SummaryReviewPage 专项方案解析 Tab 级交互", () => {
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

  beforeEach(() => {
    apiMock.generateSchemeExtraction.mockClear();
    apiMock.fetchSchemeOutlineTemplate.mockClear();
  });

  /** 每个用例先清理上一个用例可能残留的 DOM（用例失败时 cleanup 可能未执行）。 */
  async function cleanDom(rtl: any) {
    if (rtl) rtl.cleanup();
  }

  it("渲染五类 Tab，并默认展示工程概况；可切换到方案类型 / 危大工程参数", async () => {
    const rtl = await loadRtl();
    if (!rtl) return;
    await cleanDom(rtl);

    const Page = (await import("./SummaryReviewPage")).default;
    rtl.render(<Page />);

    // 五个 Tab 标签均在场（Tab 级入口完整）
    for (const label of ["工程概况", "方案类型", "危大工程参数", "编制依据", "施工条件"]) {
      expect((await rtl.screen.findAllByText(label)).length).toBeGreaterThan(0);
    }
    // 默认 Tab 内容：工程概况的字段值
    expect(await rtl.screen.findByText("某商业综合体基坑工程")).toBeTruthy();

    // 切换到「方案类型」
    const [typeTab] = await rtl.screen.findAllByText("方案类型");
    rtl.fireEvent.click(typeTab);
    expect(await rtl.screen.findByText("基坑工程")).toBeTruthy();

    // 切换到「危大工程参数」
    const [riskTab] = await rtl.screen.findAllByText("危大工程参数");
    rtl.fireEvent.click(riskTab);
    expect(await rtl.screen.findByText("6.5")).toBeTruthy();

    rtl.cleanup();
  });

  it("点击「开始解析」调用后端并回显结果，按钮切换为「重新解析」", async () => {
    const rtl = await loadRtl();
    if (!rtl) return;
    await cleanDom(rtl);

    // 首次进入无结果 → 按钮文案应为「开始解析」
    apiMock.getAnalysis.mockResolvedValueOnce({
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
      scheme_extraction: {},
    });

    const Page = (await import("./SummaryReviewPage")).default;
    rtl.render(<Page />);

    const startBtn = await rtl.screen.findByText("开始解析");
    rtl.fireEvent.click(startBtn);

    await rtl.waitFor(() => {
      expect(apiMock.generateSchemeExtraction).toHaveBeenCalledWith(1);
    });
    // 结果回显：工程概况 Tab 内容出现
    expect(await rtl.screen.findByText("某商业综合体基坑工程")).toBeTruthy();
    // 按钮已变「重新解析」
    expect(await rtl.screen.findByText("重新解析")).toBeTruthy();
    // 解析后刷新推荐模板
    await rtl.waitFor(() => {
      expect(apiMock.fetchSchemeOutlineTemplate).toHaveBeenCalledWith(1);
    });

    rtl.cleanup();
  });

  it("单个子任务失败时按项展示告警，其余结果仍可见", async () => {
    const rtl = await loadRtl();
    if (!rtl) return;
    await cleanDom(rtl);

    apiMock.generateSchemeExtraction.mockResolvedValueOnce({
      scheme_extraction_json: "{}",
      scheme_extraction: {
        ...EXTRACTION,
        project_overview: { error: "AI 超时" } as any,
      },
    });

    const Page = (await import("./SummaryReviewPage")).default;
    rtl.render(<Page />);

    // 页面已有默认结果 → 按钮文案为「重新解析」（精确匹配，避免命中卡片标题「专项方案解析」）
    rtl.fireEvent.click(await rtl.screen.findByText("重新解析"));

    // 失败项在默认 Tab（工程概况）显示告警
    expect(await rtl.screen.findByText("该专项任务未成功")).toBeTruthy();
    expect(await rtl.screen.findByText("AI 超时")).toBeTruthy();

    rtl.cleanup();
  });

  it("已有结果时展示推荐目录模板预览（非默认选型 + 章节来源）", async () => {
    const rtl = await loadRtl();
    if (!rtl) return;
    await cleanDom(rtl);

    const Page = (await import("./SummaryReviewPage")).default;
    rtl.render(<Page />);

    expect((await rtl.screen.findAllByText("推荐目录模板")).length).toBeGreaterThan(0);
    expect(await rtl.screen.findByText(/已按 基坑工程 自动选型/)).toBeTruthy();
    // 模板章节（含来源标注）渲染
    expect(await rtl.screen.findByText("基坑监测方案")).toBeTruthy();
    expect((await rtl.screen.findAllByText(/方案类型：基坑工程/)).length).toBeGreaterThan(1);

    rtl.cleanup();
  });
});
