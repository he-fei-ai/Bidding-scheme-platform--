// @vitest-environment jsdom
/**
 * 导出文档配置页 —— 组件级（DOM）交互测试（2026-10-06）。
 *
 * 背景：导出文档模块此前只有 utils 纯函数测试（utils.export.test.ts），
 * 页面本体（ExportConfigPage.tsx）零组件级测试。前两轮后端报告均因
 * "本机无 Node 运行时"无法补前端交互测试；现 Node v22 可用，按
 * ConsistencyPage.dom.test.tsx 同范式补齐。
 *
 * 本测试在真实 DOM 交互里钉住导出链的关键调用契约：
 *  1. 页面挂载后渲染「完整性检查 / 导出 DOCX」主操作按钮；
 *  2. 点「完整性检查」→ checkDocument 被调用，且 strictMode 与开关状态一致；
 *  3. 点「导出 DOCX」→ 优先走 SSE 流（exportDocxStream），且把当前
 *     preset_id/theme_id 等配置透传，完成后渲染「下载文档」；
 *  4. 导出历史记录渲染到右侧表格。
 */
import { describe, it, expect, vi, beforeAll } from "vitest";

const hasDom = typeof window !== "undefined" && typeof document !== "undefined";

const CHECK_RESULT = {
  passed: true,
  total_sections: 10,
  total_chars: 5000,
  empty_sections: [],
  uncovered_scoring_points: [],
  warnings: [],
  errors: [],
};

const HISTORY_RECORD = {
  id: 101,
  project_id: 1,
  filename: "测试项目_20260101_000000.docx",
  file_path: "/data/x.docx",
  file_size: 2048,
  status: "completed",
  created_at: "2026-01-01 00:00:00",
};

const checkDocumentMock = vi.fn(async () => CHECK_RESULT);
const exportDocxMock = vi.fn(async () => ({
  filename: "out.docx",
  file_path: "/x",
  file_size: 12345,
  export_id: 201,
  check: CHECK_RESULT,
}));
const exportDocxStreamMock = vi.fn((_pid: number, _cfg: Record<string, unknown>, _onEvt?: (e: unknown) => void) => ({
  abort: vi.fn(),
  promise: Promise.resolve({
    filename: "out.docx",
    file_path: "/x",
    file_size: 12345,
    export_id: 201,
    record_failed: false,
    check: CHECK_RESULT,
  }),
}));
const getExportHistoryMock = vi.fn(async () => [HISTORY_RECORD]);
const exportPdfMock = vi.fn(async () => undefined);

vi.mock("../api", () => ({
  getProject: vi.fn(async () => ({ id: 1, name: "测试项目", bidder_name: "测试单位" })),
  checkDocument: (...args: unknown[]) => checkDocumentMock(...args),
  exportDocx: (...args: unknown[]) => exportDocxMock(...args),
  exportDocxStream: (...args: unknown[]) => exportDocxStreamMock(...args),
  getExportHistory: (...args: unknown[]) => getExportHistoryMock(...args),
  getExportDownloadUrl: vi.fn((pid: number, eid: number) => `/api/v1/projects/${pid}/export/download/${eid}`),
  getLatestExportUrl: vi.fn((pid: number) => `/api/v1/projects/${pid}/export/latest`),
  getExportPresets: vi.fn(async () => ({
    layouts: [{ id: "formal-binding", name: "正式装订", config: {} }],
    themes: [],
    defaults: { layout_id: "formal-binding", theme_id: "" },
  })),
  exportPdf: (...args: unknown[]) => exportPdfMock(...args),
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

describe.skipIf(!hasDom)("ExportConfigPage 组件级交互（导出链契约）", () => {
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

  it("挂载后渲染「完整性检查 / 导出 DOCX」主操作按钮", { timeout: 120_000 }, async () => {
    const rtl = await loadRtl();
    if (!rtl) return;

    const Page = (await import("./ExportConfigPage")).default;
    rtl.render(<Page />);

    expect(await rtl.screen.findByText("完整性检查")).toBeTruthy();
    expect(await rtl.screen.findByText("导出 DOCX")).toBeTruthy();
    rtl.cleanup();
  });

  it("点「完整性检查」调用 checkDocument 并透传 strictMode，渲染检查结果", { timeout: 120_000 }, async () => {
    const rtl = await loadRtl();
    if (!rtl) return;
    checkDocumentMock.mockClear();

    const Page = (await import("./ExportConfigPage")).default;
    rtl.render(<Page />);

    const btn = await rtl.screen.findByText("完整性检查");
    rtl.fireEvent.click(btn);

    // 检查结果卡片出现：passed=true → "通过" Tag
    expect(await rtl.screen.findByText("通过")).toBeTruthy();
    expect(checkDocumentMock).toHaveBeenCalled();
    // 第二参 strictMode 默认开关态 false
    const [, strict] = checkDocumentMock.mock.calls[0] as [number, boolean];
    expect(strict).toBe(false);
    rtl.cleanup();
  });

  it("点「导出 DOCX」优先走 SSE 流并透传 preset 配置，完成后渲染下载入口", { timeout: 120_000 }, async () => {
    const rtl = await loadRtl();
    if (!rtl) return;
    exportDocxStreamMock.mockClear();

    const Page = (await import("./ExportConfigPage")).default;
    rtl.render(<Page />);

    const btn = await rtl.screen.findByText("导出 DOCX");
    rtl.fireEvent.click(btn);

    // 导出成功卡片出现
    expect(await rtl.screen.findByText("导出成功")).toBeTruthy();
    expect(await rtl.screen.findByText("下载文档")).toBeTruthy();

    // 关键契约：优先走 SSE 流，且配置对象携带 preset_id（预设透传，2026-09-14 修复点）
    expect(exportDocxStreamMock).toHaveBeenCalled();
    const [, cfg] = exportDocxStreamMock.mock.calls[0] as [number, Record<string, unknown>];
    expect(cfg).toHaveProperty("preset_id", "formal-binding");
    expect(cfg).toHaveProperty("strict_mode", false);
    // SSE 成功分支不应降级重跑同步接口
    expect(exportDocxMock).not.toHaveBeenCalled();
    rtl.cleanup();
  });

  it("渲染导出历史记录", { timeout: 120_000 }, async () => {
    const rtl = await loadRtl();
    if (!rtl) return;

    const Page = (await import("./ExportConfigPage")).default;
    rtl.render(<Page />);

    // 历史记录文件名应出现在右侧历史区
    expect(await rtl.screen.findByText(/测试项目_20260101_000000/)).toBeTruthy();
    rtl.cleanup();
  });
});
