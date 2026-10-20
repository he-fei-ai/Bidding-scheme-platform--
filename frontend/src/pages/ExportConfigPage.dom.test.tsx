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
 *  4. 点「导出 HTML」（ENHANCE 2026-10-13）→ 调用 exportHtml 且透传同一份配置；
 *  5. 导出历史记录渲染到右侧表格。
 */
import { describe, it, expect, vi, beforeAll } from "vitest";
import type { DocumentCheckResult } from "../types";
import { taskStore } from "../services/taskStore";

const hasDom = typeof window !== "undefined" && typeof document !== "undefined";

const CHECK_RESULT: DocumentCheckResult = {
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

const checkDocumentMock = vi.fn(async (_pid: number, _strict?: boolean) => CHECK_RESULT);
const exportDocxMock = vi.fn(async (_pid: number, _cfg?: Record<string, unknown>) => ({
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
const getExportHistoryMock = vi.fn(async (_pid: number) => [HISTORY_RECORD]);
const exportPdfMock = vi.fn(async (_pid: number) => undefined);
// ENHANCE 2026-10-13：HTML 导出（第二交付格式）mock
const exportHtmlMock = vi.fn(async (_pid: number, _cfg?: Record<string, unknown>) => undefined);

vi.mock("../api", () => ({
  getProject: vi.fn(async () => ({ id: 1, name: "测试项目", bidder_name: "测试单位" })),
  // spread 用显式元组类型，避免 TS2556（vi.fn 的 Parameters 退化为 unknown[] 不可 spread）
  checkDocument: (...args: [number, boolean?]) => checkDocumentMock(...args),
  exportDocx: (...args: [number, Record<string, unknown>?]) => exportDocxMock(...args),
  exportDocxStream: (...args: [number, Record<string, unknown>, ((e: unknown) => void)?]) => exportDocxStreamMock(...args),
  getExportHistory: (...args: [number]) => getExportHistoryMock(...args),
  getExportDownloadUrl: vi.fn((pid: number, eid: number) => `/api/v1/projects/${pid}/export/download/${eid}`),
  getLatestExportUrl: vi.fn((pid: number) => `/api/v1/projects/${pid}/export/latest`),
  getExportPresets: vi.fn(async () => ({
    layouts: [{ id: "formal-binding", name: "正式装订", config: {} }],
    themes: [],
    defaults: { layout_id: "formal-binding", theme_id: "" },
  })),
  exportPdf: (...args: [number]) => exportPdfMock(...args),
  exportHtml: (...args: [number, Record<string, unknown>?]) => exportHtmlMock(...args),
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

  // 首个用例需先由 Vite 冷加载 ExportConfigPage + antd 全量依赖，本机实测可达
  // 180s+，故放宽到 300s（与 quality_issues 用例一致）；否则慢机器上恒超时，
  // 且超时残留的定时器/React 树会级联污染后续用例。
  it("挂载后渲染「完整性检查 / 导出 DOCX」主操作按钮", { timeout: 300_000 }, async () => {
    const rtl = await loadRtl();
    if (!rtl) return;

    const Page = (await import("./ExportConfigPage")).default;
    rtl.render(<Page />);

    expect(await rtl.screen.findByText("完整性检查")).toBeTruthy();
    expect(await rtl.screen.findByText("导出 DOCX")).toBeTruthy();
    rtl.cleanup();
  });

  it("点「完整性检查」调用 checkDocument 并透传 strictMode，渲染检查结果", async () => {
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
    const [, strict] = checkDocumentMock.mock.calls[0] ?? [0, false];
    expect(strict).toBe(false);
    rtl.cleanup();
  });

  // 说明：本文件首个用例需先由 Vite 冷加载 ExportConfigPage + antd 全量依赖，
  // 慢机器上加载耗时可达 130s+（超过 120s 默认预算），故此处放宽到 300s。
  it("检查结果含 quality_issues 时渲染内容质量规则问题列表", { timeout: 300_000 }, async () => {
    const rtl = await loadRtl();
    if (!rtl) return;
    checkDocumentMock.mockClear();
    // async mock 的返回值类型是 Promise<...>，故需包一层 Promise.resolve
    checkDocumentMock.mockReturnValueOnce(
      Promise.resolve({
        ...CHECK_RESULT,
        quality_issues: [
          {
            rule_id: "CON-01",
            severity: "high",
            title: "工期不一致",
            message: "各阶段工期之和与总工期不符",
            suggestion: "修正阶段工期使其加和等于总工期",
            section_id: 12,
            section_title: "6.3 施工进度计划",
          },
        ],
      }),
    );

    const Page = (await import("./ExportConfigPage")).default;
    rtl.render(<Page />);

    const btn = await rtl.screen.findByText("完整性检查");
    rtl.fireEvent.click(btn);

    // 标题（含条数）、规则号、正文、建议、章节定位都应出现
    // 注意：Alert 外层 span 的 textContent 会串起 message 与定位文本，
    // 因此统一用 findAllByText（≥1）而非 findByText（唯一匹配）
    expect((await rtl.screen.findAllByText(/内容质量规则问题/)).length).toBeGreaterThan(0);
    expect((await rtl.screen.findAllByText(/\[CON-01\]/)).length).toBeGreaterThan(0);
    expect(
      (await rtl.screen.findAllByText((_, el) => el?.textContent === "各阶段工期之和与总工期不符")).length
    ).toBeGreaterThan(0);
    expect((await rtl.screen.findAllByText(/建议：修正阶段工期使其加和等于总工期/)).length).toBeGreaterThan(0);
    expect((await rtl.screen.findAllByText(/定位：6\.3 施工进度计划/)).length).toBeGreaterThan(0);
    rtl.cleanup();
  });

  it("点「导出 DOCX」优先走 SSE 流并透传 preset 配置，完成后渲染下载入口", async () => {
    const rtl = await loadRtl();
    if (!rtl) return;
    exportDocxStreamMock.mockClear();
    // FIX-E（2026-10-17）：钉住导出任务挂接全局 taskStore 的状态流转
    const startSpy = vi.spyOn(taskStore, "startTask");
    const completeSpy = vi.spyOn(taskStore, "completeTask");

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
    // FIX-E：任务启动时登记到全局 store（taskType=export，顶栏跨页面可见）
    expect(startSpy).toHaveBeenCalledWith(
      expect.objectContaining({ projectId: 1, taskType: "export" }),
    );
    // FIX-E：完成后以终态收尾（taskId=export-<pid>，status=completed）
    await vi.waitFor(() =>
      expect(completeSpy).toHaveBeenCalledWith("export-1", "completed", expect.any(String)),
    );
    // SSE 成功分支不应降级重跑同步接口
    expect(exportDocxMock).not.toHaveBeenCalled();
    startSpy.mockRestore();
    completeSpy.mockRestore();
    rtl.cleanup();
  });

  it("渲染导出历史记录", async () => {
    const rtl = await loadRtl();
    if (!rtl) return;

    const Page = (await import("./ExportConfigPage")).default;
    rtl.render(<Page />);

    // 历史记录文件名应出现在右侧历史区
    expect(await rtl.screen.findByText(/测试项目_20260101_000000/)).toBeTruthy();
    rtl.cleanup();
  });

  // ENHANCE 2026-10-13：HTML 导出入口（后端 /export/html 上线后前端无入口，
  // 功能不可达）。钉住：按钮存在 + 点击调用 exportHtml + 透传同一份配置。
  it("点「导出 HTML」调用 exportHtml 并透传配置（含 preset_id）", async () => {
    const rtl = await loadRtl();
    if (!rtl) return;
    exportHtmlMock.mockClear();

    const Page = (await import("./ExportConfigPage")).default;
    rtl.render(<Page />);

    const btn = await rtl.screen.findByText("导出 HTML");
    rtl.fireEvent.click(btn);

    await vi.waitFor(() => expect(exportHtmlMock).toHaveBeenCalled());
    const [pid, cfg] = exportHtmlMock.mock.calls[0] as [number, Record<string, unknown>];
    expect(pid).toBe(1);
    expect(cfg).toHaveProperty("preset_id", "formal-binding");
    expect(cfg).toHaveProperty("strict_mode", false);
    rtl.cleanup();
  });

  it("HTML 导出失败时展示错误提示（不静默）", async () => {
    const rtl = await loadRtl();
    if (!rtl) return;
    exportHtmlMock.mockClear();
    exportHtmlMock.mockRejectedValueOnce(new Error("文档为空，无法导出"));

    const Page = (await import("./ExportConfigPage")).default;
    rtl.render(<Page />);

    const btn = await rtl.screen.findByText("导出 HTML");
    rtl.fireEvent.click(btn);

    // antd message.error 文案出现（按钮恢复可点）
    await vi.waitFor(() => expect(exportHtmlMock).toHaveBeenCalled());
    expect(await rtl.screen.findByText("文档为空，无法导出")).toBeTruthy();
    rtl.cleanup();
  });

  it("PDF 下载失败且后端返回 501 长安装指引时走 Modal.error 展示（X-7）", async () => {
    const rtl = await loadRtl();
    if (!rtl) return;
    const antd = (await import("antd")) as any;
    const modalErrorSpy = vi.spyOn(antd.Modal, "error").mockImplementation(() => undefined);
    const messageErrorSpy = vi.spyOn(antd.message, "error").mockImplementation(() => undefined);

    exportPdfMock.mockClear();
    exportPdfMock.mockRejectedValueOnce({
      response: { status: 501, data: { detail: "PDF 转换不可用：未检测到 LibreOffice。请安装 LibreOffice（soffice）或设置环境变量 PDF_SOFFICE_PATH 指向 soffice.exe，详见项目 README。" } },
    } as any);

    const Page = (await import("./ExportConfigPage")).default;
    rtl.render(<Page />);

    // 点击历史表格第一行「PDF」按钮
    const btn = await rtl.screen.findByText("PDF");
    rtl.fireEvent.click(btn);

    await vi.waitFor(() => expect(exportPdfMock).toHaveBeenCalled());
    // 关键断言：长错误 + status 501 → 走 Modal.error 而非 message.error
    await vi.waitFor(() => expect(modalErrorSpy).toHaveBeenCalled());
    const firstCall = modalErrorSpy.mock.calls[0] as any[];
    expect(firstCall[0]).toMatchObject({ title: "PDF 转换不可用" });
    expect(String(firstCall[0].content)).toContain("LibreOffice");
    expect(String(firstCall[0].content)).toContain("PDF_SOFFICE_PATH");

    modalErrorSpy.mockRestore();
    messageErrorSpy.mockRestore();
    rtl.cleanup();
  });

  it("PDF 下载失败且错误信息较短时走 message.error（X-7 短错误路径）", async () => {
    const rtl = await loadRtl();
    if (!rtl) return;
    const antd = (await import("antd")) as any;
    const modalErrorSpy = vi.spyOn(antd.Modal, "error").mockImplementation(() => undefined);
    const messageErrorSpy = vi.spyOn(antd.message, "error").mockImplementation(() => undefined);

    exportPdfMock.mockClear();
    exportPdfMock.mockRejectedValueOnce({
      response: { status: 500, data: { detail: "服务暂时不可用" } },
    } as any);

    const Page = (await import("./ExportConfigPage")).default;
    rtl.render(<Page />);

    const btn = await rtl.screen.findByText("PDF");
    rtl.fireEvent.click(btn);

    await vi.waitFor(() => expect(exportPdfMock).toHaveBeenCalled());
    // 短错误走 message.error，不走 Modal
    await vi.waitFor(() => expect(messageErrorSpy).toHaveBeenCalled());
    const firstCall = messageErrorSpy.mock.calls[0] as any[];
    expect(String(firstCall[0])).toContain("服务暂时不可用");
    expect(modalErrorSpy).not.toHaveBeenCalled();

    modalErrorSpy.mockRestore();
    messageErrorSpy.mockRestore();
    rtl.cleanup();
  });
});
