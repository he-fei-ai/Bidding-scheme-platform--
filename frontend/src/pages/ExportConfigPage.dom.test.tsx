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

const checkDocumentMock = vi.fn(async (_pid: number, _strict?: boolean, _block?: boolean) => CHECK_RESULT);
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
// B-3/B-4（2026-11-04）：预设目录 mock 提为具名可控行。夹具字段同步修正为
// 与后端 GET /export/presets（export_presets.list_presets）及前端
// ExportLayoutPreset 类型一致的 **label** 字段——旧夹具写 `name`，生产代码与
// 类型读 `label`，测试里下拉选项 label 恒为 undefined（夹具/类型漂移）。
const PRESET_CATALOG = {
  layouts: [
    {
      id: "formal-binding",
      label: "正式装订",
      description: "测试版式：适合打印后装订递交。",
      config: {},
    },
  ],
  themes: [],
  defaults: { layout_id: "formal-binding", theme_id: "" },
};
const getExportPresetsMock = vi.fn(async () => PRESET_CATALOG);

vi.mock("../api", () => ({
  getProject: vi.fn(async () => ({ id: 1, name: "测试项目", bidder_name: "测试单位" })),
  // spread 用显式元组类型，避免 TS2556（vi.fn 的 Parameters 退化为 unknown[] 不可 spread）
  checkDocument: (...args: [number, boolean?, boolean?]) => checkDocumentMock(...args),
  exportDocx: (...args: [number, Record<string, unknown>?]) => exportDocxMock(...args),
  exportDocxStream: (...args: [number, Record<string, unknown>, ((e: unknown) => void)?]) => exportDocxStreamMock(...args),
  getExportHistory: (...args: [number]) => getExportHistoryMock(...args),
  getExportDownloadUrl: vi.fn((pid: number, eid: number) => `/api/v1/projects/${pid}/export/download/${eid}`),
  getLatestExportUrl: vi.fn((pid: number) => `/api/v1/projects/${pid}/export/latest`),
  getExportPresets: (...args: []) => getExportPresetsMock(...args),
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

  // 2026-11-04（收尾 P4）：缓存指纹命中 —— 后端 SSE progress.step 透传到用户可见
  // 进度文案，证明「同输入复用渲染结果」不是静默复用，用户能看到当前阶段。
  it("导出 SSE 的缓存指纹命中阶段透传到界面（非静默复用）", async () => {
    const rtl = await loadRtl();
    if (!rtl) return;
    exportDocxMock.mockClear();
    exportDocxStreamMock.mockClear();

    // 显式标注返回类型，避免 Promise<unknown> 与 mockImplementationOnce 期望的
    // Promise<ExportResult> 不兼容（TS2345）。
    type StreamResult = {
      filename: string;
      file_path: string;
      file_size: number;
      export_id: number;
      record_failed: boolean;
      check: DocumentCheckResult;
    };
    let resolveStream!: (v: StreamResult) => void;
    const pending = new Promise<StreamResult>((resolve) => {
      resolveStream = resolve;
    });
    exportDocxStreamMock.mockImplementationOnce(
      (_pid: number, _cfg: Record<string, unknown>, onEvt?: (e: unknown) => void) => {
        onEvt?.({
          event: "progress",
          progress: 0.6,
          step: "命中缓存：12 个章节复用指纹一致的渲染结果",
        });
        return { abort: vi.fn(), promise: pending };
      },
    );

    const Page = (await import("./ExportConfigPage")).default;
    rtl.render(<Page />);
    rtl.fireEvent.click(await rtl.screen.findByText("导出 DOCX"));

    // 进度阶段文案直接出现在界面（缓存命中不是静默跳过）
    await rtl.waitFor(() => {
      expect(document.body.textContent).toContain("命中缓存：12 个章节复用指纹一致的渲染结果");
    });
    expect(exportDocxMock).not.toHaveBeenCalled();

    resolveStream({
      filename: "out.docx",
      file_path: "/x",
      file_size: 12345,
      export_id: 201,
      record_failed: false,
      check: CHECK_RESULT,
    });
    expect(await rtl.screen.findByText("导出成功")).toBeTruthy();
    rtl.cleanup();
  });

  // BUG-FIX 2026-10-27（P2·误降级同步导出）：已收到 stream_id 说明构建已在
  // 后端启动并持有导出组锁 —— 降级同步 exportDocx 必然撞 409 或重复构建。
  // 页面必须转「后台继续」分支：不调 exportDocx，任务以 completed 收尾并提示
  // 用户到导出历史下载。
  it("SSE 流中断且构建已启动时转后台执行，不降级同步导出", async () => {
    const rtl = await loadRtl();
    if (!rtl) return;
    exportDocxMock.mockClear();
    exportDocxStreamMock.mockClear();
    // 构造一个「已处理过」的 rejected promise：页面在 await 前存在微任务间隙，
    // 直接 mockReturnValueOnce({promise: Promise.reject(...)}) 会触发 Node 的
    // unhandledRejection（vitest 记为 Errors, 退出码 1）。
    const bgRejection = Promise.reject(
      Object.assign(new Error("导出流连接中断，导出任务仍在后台执行"), {
        __stream_started: true,
      }),
    );
    bgRejection.catch(() => {});
    exportDocxStreamMock.mockReturnValueOnce({
      abort: vi.fn(),
      promise: bgRejection,
    });
    const completeSpy = vi.spyOn(taskStore, "completeTask");

    const Page = await import("./ExportConfigPage");
    rtl.render(<Page.default />);

    const btn = await rtl.screen.findByText("导出 DOCX");
    rtl.fireEvent.click(btn);

    await vi.waitFor(() =>
      expect(completeSpy).toHaveBeenCalledWith(
        "export-1",
        "completed",
        expect.stringContaining("导出历史"),
      ),
    );
    // 关键契约：不降级 —— 同步接口绝不能被再次调用（409 误报 / 重复构建）
    expect(exportDocxMock).not.toHaveBeenCalled();
    completeSpy.mockRestore();
    rtl.cleanup();
  });

  // BUG-FIX 2026-11-02（B-1·前端硬编码覆盖预设）：image_max_width_cm /
  // gantt_max_columns / image_dpi 属"预设/后端配置管辖"字段，前端显式传值
  // 会在 resolve_export_config 中以最高优先级覆盖预设值（选「宽表格横版」
  // 等版式时图片宽度与甘特列数恒被 18 中和）。钉住导出请求体不再携带这
  // 三个字段，由后端按 preset_id + 模型默认值补全（向后兼容）。
  it("导出 DOCX 配置不携带 image_max_width_cm/gantt_max_columns/image_dpi（B-1）", async () => {
    const rtl = await loadRtl();
    if (!rtl) return;
    exportDocxStreamMock.mockClear();

    const Page = (await import("./ExportConfigPage")).default;
    rtl.render(<Page />);

    const btn = await rtl.screen.findByText("导出 DOCX");
    rtl.fireEvent.click(btn);

    expect(await rtl.screen.findByText("导出成功")).toBeTruthy();
    expect(exportDocxStreamMock).toHaveBeenCalled();
    const [, cfg] = exportDocxStreamMock.mock.calls[0] as [number, Record<string, unknown>];
    expect(cfg).not.toHaveProperty("image_max_width_cm");
    expect(cfg).not.toHaveProperty("gantt_max_columns");
    expect(cfg).not.toHaveProperty("image_dpi");
    // 预设透传链路不受影响
    expect(cfg).toHaveProperty("preset_id", "formal-binding");
    rtl.cleanup();
  });

  // 对照用例：stream_id 尚未下发（构建未启动）的传输失败仍走同步降级
  it("SSE 流异常结束且构建未启动时仍降级同步导出", async () => {
    const rtl = await loadRtl();
    if (!rtl) return;
    exportDocxMock.mockClear();
    exportDocxStreamMock.mockClear();
    const fallRejection = Promise.reject(new Error("SSE 流异常结束，未收到完成事件"));
    fallRejection.catch(() => {});
    exportDocxStreamMock.mockReturnValueOnce({
      abort: vi.fn(),
      promise: fallRejection,
    });

    const Page = await import("./ExportConfigPage");
    rtl.render(<Page.default />);

    const btn = await rtl.screen.findByText("导出 DOCX");
    rtl.fireEvent.click(btn);

    // 降级同步导出完成后正常渲染成功卡片
    expect(await rtl.screen.findByText("导出成功", {}, { timeout: 20_000 })).toBeTruthy();
    expect(exportDocxMock).toHaveBeenCalledTimes(1);
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

  // BUG-FIX 2026-11-06（组件级覆盖补齐）：导出进行中点「取消导出」→ 中止 SSE 流、
  // 不降级重跑同步接口、任务停止追踪。原 handleCancelExport 的 abort/取消分支此前
  // 无组件级测试（只有 utils.classifyExportFailure 纯函数覆盖取消判定）。
  it("导出进行中点「取消导出」中止 SSE 流且不降级同步导出", async () => {
    const rtl = await loadRtl();
    if (!rtl) return;
    exportDocxMock.mockClear();
    exportDocxStreamMock.mockClear();
    // 模拟真实 SSE 语义：abort() 使挂起 promise reject（AbortError）——
    // 否则 promise 永不 settle，handleExport 的 catch/finally 永不执行，
    // taskStore.stopTracking 也不会被调用，断言无法收敛。
    let rejectPending: (e: any) => void = () => {};
    const pendingPromise = new Promise<never>((_res, rej) => { rejectPending = rej; });
    const abortMock = vi.fn(() => rejectPending(new Error("SSE 导出已被取消")));
    exportDocxStreamMock.mockReturnValueOnce({
      abort: abortMock,
      promise: pendingPromise,
    });
    const stopSpy = vi.spyOn(taskStore, "stopTracking");

    const Page = (await import("./ExportConfigPage")).default;
    rtl.render(<Page />);

    const btn = await rtl.screen.findByText("导出 DOCX");
    rtl.fireEvent.click(btn);
    // 等待导出卡出现（挂起中）
    await rtl.screen.findByText("取消导出");

    const cancelBtn = await rtl.screen.findByText("取消导出");
    rtl.fireEvent.click(cancelBtn);

    await vi.waitFor(() => expect(abortMock).toHaveBeenCalled());
    // 关键契约：取消不降级 —— 同步接口绝不能被调用
    expect(exportDocxMock).not.toHaveBeenCalled();
    // 取消后任务停止追踪（cancelRequestedRef → catch 的取消分支）
    await vi.waitFor(() => expect(stopSpy).toHaveBeenCalledWith("export-1"));
    stopSpy.mockRestore();
    rtl.cleanup();
  });

  // 组件级覆盖补齐：勾选「严格模式」「一致性冲突阻断导出」后点「完整性检查」，
  // 必须把 strict=true / block=true 透传给 checkDocument（此前只测默认 false）。
  it("勾选严格模式与一致性闸门后点「完整性检查」透传 true", async () => {
    const rtl = await loadRtl();
    if (!rtl) return;
    checkDocumentMock.mockClear();

    const Page = (await import("./ExportConfigPage")).default;
    rtl.render(<Page />);

    // antd Switch 序列：标题页/目录页/页眉页脚/严格模式/一致性冲突阻断/图表兜底补图
    const switches = rtl.screen.getAllByRole("switch");
    rtl.fireEvent.click(switches[3]); // 严格模式
    rtl.fireEvent.click(switches[4]); // 一致性冲突阻断导出

    const btn = await rtl.screen.findByText("完整性检查");
    rtl.fireEvent.click(btn);

    await vi.waitFor(() => expect(checkDocumentMock).toHaveBeenCalled());
    const [, strict, block] = checkDocumentMock.mock.calls[0] as [number, boolean, boolean];
    expect(strict).toBe(true);
    expect(block).toBe(true);
    rtl.cleanup();
  });

  // 组件级覆盖补齐：图题分隔符选择「：」后点「导出 DOCX」，SSE 请求体携带
  // figure_no_separator="："（L-6 2026-10-17 图题格式配置链路）。
  it("选择冒号图题分隔符后导出配置携带 figure_no_separator=：", async () => {
    const rtl = await loadRtl();
    if (!rtl) return;
    exportDocxStreamMock.mockClear();

    const Page = (await import("./ExportConfigPage")).default;
    rtl.render(<Page />);

    // 点开分隔符 Select（value 显示「空格（图3-1 标题）」；用正则匹配避免
    // antd 对全角括号/空白节点的精确文本差异导致漏匹配）
    const sepSelect = await rtl.screen.findByText(/空格（图3-1 标题）/);
    rtl.fireEvent.mouseDown(sepSelect);
    // 选中「冒号（图3-1：标题）」选项
    const colonOption = await rtl.screen.findByText(/冒号（图3-1：标题）/);
    rtl.fireEvent.click(colonOption);

    const btn = await rtl.screen.findByText("导出 DOCX");
    rtl.fireEvent.click(btn);

    expect(await rtl.screen.findByText("导出成功")).toBeTruthy();
    expect(exportDocxStreamMock).toHaveBeenCalled();
    const [, cfg] = exportDocxStreamMock.mock.calls[0] as [number, Record<string, unknown>];
    expect(cfg).toHaveProperty("figure_no_separator", "：");
    rtl.cleanup();
  });

  // B-3（2026-11-04 导出模块加固轮·夹具/类型漂移钉住）：预设目录的版式条目
  // 按 ExportLayoutPreset 契约携带 label/description；页面把 label 渲染进
  // 「版式」Select 的选中项、description 渲染进说明 Alert。旧测试夹具写
  // `name` 字段，label 恒 undefined → 下拉显示 value（"formal-binding"），
  // 该契约此前无断言。
  it("预设目录加载后版式 label/description 正确渲染（B-3）", async () => {
    const rtl = await loadRtl();
    if (!rtl) return;
    getExportPresetsMock.mockClear();

    const Page = (await import("./ExportConfigPage")).default;
    rtl.render(<Page />);

    // 默认版式被自动套用（defaults.layout_id）→ Select 选中项显示 label 文本
    expect(await rtl.screen.findByText("正式装订")).toBeTruthy();
    // 说明 Alert 渲染 description（同一目录的另一契约字段）
    expect(
      await rtl.screen.findByText("测试版式：适合打印后装订递交。"),
    ).toBeTruthy();
    // 目录计数文案来自同一 catalog
    expect(await rtl.screen.findByText(/共 1 套版式/)).toBeTruthy();
    expect(getExportPresetsMock).toHaveBeenCalledTimes(1);
    rtl.cleanup();
  });

  // B-4（2026-11-04 导出模块加固轮）：预设加载失败必须可感知、可重试。
  // 旧实现 catch 里复位 presetsLoadedRef 但挂载 effect 依赖 []，重试是死代码。
  it("预设加载失败展示重试按钮，点击重试成功后提示消失（B-4）", async () => {
    const rtl = await loadRtl();
    if (!rtl) return;
    getExportPresetsMock.mockClear();
    // 首次（挂载）调用失败 → 重试入口出现；重试走默认成功实现
    getExportPresetsMock.mockRejectedValueOnce(new Error("预设服务暂时不可用"));

    const Page = (await import("./ExportConfigPage")).default;
    rtl.render(<Page />);

    const retryBtn = await rtl.screen.findByText("重试加载预设");
    expect(await rtl.screen.findByText(/预设目录加载失败/)).toBeTruthy();

    rtl.fireEvent.click(retryBtn);
    // 重试成功（默认实现）→ 失败提示与按钮消失，目录正常渲染
    await rtl.screen.findByText("正式装订");
    await vi.waitFor(() =>
      expect(rtl.screen.queryByText("重试加载预设")).toBeNull(),
    );
    expect(getExportPresetsMock).toHaveBeenCalledTimes(2);
    rtl.cleanup();
  });
});
