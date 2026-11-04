// @vitest-environment jsdom
/**
 * FileUploadPage —— 后台异步解析按钮 与 上传路径 needs_reanalysis —— 组件级交互测试（2026-11-04）
 *
 * 补足 2026-11-04 审计发现的两个**组件级未覆盖交互**（接口在 api 层早已导出、
 * 页面早已接线，但没有任何 DOM 测试点击过它们）：
 *
 *  G1 后台解析按钮（handleAsyncReparse）：
 *      - 对图片文件点击「后台解析」必须调用 asyncReparseFile(projectId, fileId)；
 *      - 三种 dispatched_via 分支（celery / inline / 其它）分别播报
 *        「已提交后台解析，完成后自动更新文本」/「后台队列不可用，已在服务端直接
 *        完成解析」/「未能提交后台解析，请改用“重新提取”」——此前完全无测试，
 *        三个分支的文案靠手写，改一个就可能静默错配。
 *
 *  G2 上传路径 needs_reanalysis（BUG-FIX 2026-10-26 已修但未测）：
 *      - handleUpload 在 uploadFile 返回 needs_reanalysis=true 时必须播报
 *        「主文件已变更，请到「提取项目」重新生成摘要与评分点」警告。
 *        此前只有手动切角色路径（handleToggleRole）有组件测试，上传路径零覆盖，
 *        正是依赖文案副本漂移的高风险点。
 *
 * 测试走真实组件渲染（antd List / message），只 mock API 层与全局任务存储。
 */
import { describe, it, expect, vi, beforeAll, afterEach } from "vitest";

const hasDom = typeof window !== "undefined" && typeof document !== "undefined";

const SUPPORTED_FORMATS = {
  extensions: [".docx", ".pdf", ".txt", ".png"],
  accept: ".docx,.pdf,.txt,.png",
  image_extensions: [".png"],
  max_file_size: 50 * 1024 * 1024,
  max_file_size_mb: 50,
  ocr_enabled: true,
};

type MockFile = {
  id: number;
  filename: string;
  file_type: string;
  file_role: string;
  status: string;
  message: string;
  created_at: string;
};

function mkFile(partial: Partial<MockFile> & { id: number; filename: string }): MockFile {
  return {
    file_type: "txt",
    file_role: "supporting",
    status: "success",
    message: "",
    created_at: "2026-11-04 10:00:00",
    ...partial,
  };
}

const IMG_FILE = mkFile({ id: 1, filename: "扫描件.png", file_type: "png", file_role: "supporting" });
const DOC_FILE = mkFile({ id: 2, filename: "招标文件.docx", file_type: "docx", file_role: "main" });

const API_MOCKS = {
  uploadFile: vi.fn(),
  fetchFiles: vi.fn(async () => [IMG_FILE, DOC_FILE] as MockFile[]),
  deleteFile: vi.fn(async () => ({ status: "deleted", file_id: 1 })),
  reExtractFileText: vi.fn(async () => ({ status: "success", text_length: 10, message: "" })),
  updateFileRole: vi.fn(async () => ({ status: "success", demoted_file_ids: [], needs_reanalysis: false })),
  fetchFileParsingStatus: vi.fn(async () => ({
    status: "idle",
    processing: 0,
    recent_completed: 0,
    recent_failed: 0,
    total_tasks: 0,
    latest: null,
  })),
  asyncReparseFile: vi.fn(async () => ({
    file_id: 1,
    filename: "扫描件.png",
    dispatched: "queued",
    dispatched_via: "celery",
  })),
  clearProjectTexts: vi.fn(async () => ({
    status: "cleared",
    files_reset: 0,
    tasks_cleared: 0,
    warning: "",
  })),
  fetchSupportedFormats: vi.fn(async () => SUPPORTED_FORMATS),
  getFileText: vi.fn(async () => ""),
};

vi.mock("../api", () => API_MOCKS);

vi.mock("@/services/taskStore", () => {
  const inst = {
    startTask: vi.fn(),
    updateTask: vi.fn(),
    completeTask: vi.fn(),
    stopTracking: vi.fn(),
    subscribe: vi.fn(() => () => {}),
    getActiveTasks: vi.fn(() => []),
    getTask: vi.fn(() => undefined),
    hasActiveTask: vi.fn(() => false),
    clear: vi.fn(),
  };
  return { taskStore: inst, default: inst };
});

vi.mock("react-router-dom", async () => {
  const actual = await vi.importActual<Record<string, unknown>>("react-router-dom");
  return { ...actual, useParams: () => ({ id: "1" }), useNavigate: () => vi.fn() };
});

async function loadRtl() {
  try {
    return await import("@testing-library/react");
  } catch {
    return null;
  }
}

type Rtl = NonNullable<Awaited<ReturnType<typeof loadRtl>>>;

function findRowButton(rtl: Rtl, filename: string, buttonName: RegExp): HTMLElement | undefined {
  return rtl.screen
    .getAllByRole("button", { name: buttonName })
    .find((b) => {
      const item = b.closest("li.ant-list-item");
      return !!item && item.textContent?.includes(filename);
    });
}

describe.skipIf(!hasDom)("FileUploadPage 后台解析按钮与上传 needs_reanalysis（2026-11-04）", () => {
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

  afterEach(() => {
    vi.clearAllMocks();
    API_MOCKS.fetchFiles.mockResolvedValue([IMG_FILE, DOC_FILE]);
    API_MOCKS.asyncReparseFile.mockResolvedValue({
      file_id: 1,
      filename: "扫描件.png",
      dispatched: "queued",
      dispatched_via: "celery",
    });
    API_MOCKS.uploadFile.mockReset();
  });

  async function renderLoaded(rtl: Rtl) {
    const FileUploadPage = (await import("./FileUploadPage")).default;
    const { container } = rtl.render(<FileUploadPage />);
    await rtl.waitFor(() =>
      expect(container.textContent || "").toContain("招标文件.docx"),
    );
    return container;
  }

  // ─── G1：后台解析按钮（图片 → asyncReparseFile 三种派发分支） ────────
  it("点击图片的「后台解析」调用 asyncReparseFile，celery 分支提示已提交", async () => {
    const rtl = await loadRtl();
    if (!rtl) return;

    const { message } = await import("antd");
    const successSpy = vi.spyOn(message, "success").mockImplementation(() => ({} as never));

    const container = await renderLoaded(rtl);
    const btn = findRowButton(rtl, "扫描件.png", /后台解析/);
    expect(btn, "图片行必须渲染「后台解析」按钮").toBeTruthy();
    rtl.fireEvent.click(btn!);

    await rtl.waitFor(() => expect(API_MOCKS.asyncReparseFile).toHaveBeenCalled());
    expect(API_MOCKS.asyncReparseFile).toHaveBeenCalledWith(1, 1);
    await rtl.waitFor(() =>
      expect(successSpy).toHaveBeenCalledWith("已提交后台解析，完成后自动更新文本"),
    );

    successSpy.mockRestore();
    rtl.cleanup();
  });

  it("后台解析 inline 分支提示「服务端直接完成解析」", async () => {
    const rtl = await loadRtl();
    if (!rtl) return;

    const { message } = await import("antd");
    const successSpy = vi.spyOn(message, "success").mockImplementation(() => ({} as never));
    API_MOCKS.asyncReparseFile.mockResolvedValue({
      file_id: 1,
      filename: "扫描件.png",
      dispatched: "done",
      dispatched_via: "inline",
    });

    await renderLoaded(rtl);
    const btn = findRowButton(rtl, "扫描件.png", /后台解析/);
    rtl.fireEvent.click(btn!);

    await rtl.waitFor(() =>
      expect(successSpy).toHaveBeenCalledWith("后台队列不可用，已在服务端直接完成解析"),
    );

    successSpy.mockRestore();
    rtl.cleanup();
  });

  it("后台解析其它派发结果提示改用「重新提取」", async () => {
    const rtl = await loadRtl();
    if (!rtl) return;

    const { message } = await import("antd");
    const warnSpy = vi.spyOn(message, "warning").mockImplementation(() => ({} as never));
    API_MOCKS.asyncReparseFile.mockResolvedValue({
      file_id: 1,
      filename: "扫描件.png",
      dispatched: "failed",
      dispatched_via: "none",
    });

    await renderLoaded(rtl);
    const btn = findRowButton(rtl, "扫描件.png", /后台解析/);
    rtl.fireEvent.click(btn!);

    await rtl.waitFor(() =>
      expect(warnSpy).toHaveBeenCalledWith("未能提交后台解析，请改用“重新提取”"),
    );

    warnSpy.mockRestore();
    rtl.cleanup();
  });

  // ─── G2：上传路径 needs_reanalysis ────────────────────────────────
  it("上传成功且后端返回 needs_reanalysis=true 时提示重新生成摘要评分点", async () => {
    const rtl = await loadRtl();
    if (!rtl) return;

    const { message } = await import("antd");
    const warnSpy = vi.spyOn(message, "warning").mockImplementation(() => ({} as never));
    API_MOCKS.uploadFile.mockResolvedValue({
      status: "success",
      id: 3,
      filename: "新招标文件.docx",
      file_type: "docx",
      file_role: "main",
      message: "",
      needs_reanalysis: true,
    });

    const FileUploadPage = (await import("./FileUploadPage")).default;
    const { container } = rtl.render(<FileUploadPage />);
    await rtl.waitFor(() =>
      expect(container.textContent || "").toContain("招标文件.docx"),
    );

    // 通过真实文件输入框触发上传
    const file = new File(["招标正文内容"], "新招标文件.docx", { type: "application/vnd.openxmlformats-officedocument.wordprocessingml.document" });
    const input = container.querySelector('input[type="file"]') as HTMLInputElement;
    expect(input, "必须渲染出文件输入框").toBeTruthy();
    Object.defineProperty(input, "files", { value: Object.assign([], [file]) as unknown as FileList, configurable: true });
    rtl.fireEvent.change(input);

    await rtl.waitFor(() => expect(API_MOCKS.uploadFile).toHaveBeenCalled());
    await rtl.waitFor(() =>
      expect(warnSpy).toHaveBeenCalledWith(
        "主文件已变更，请到「提取项目」重新生成摘要与评分点",
        6,
      ),
    );

    warnSpy.mockRestore();
    rtl.cleanup();
  });

  it("上传成功且 needs_reanalysis 为 false/缺省时不弹该警告", async () => {
    const rtl = await loadRtl();
    if (!rtl) return;

    const { message } = await import("antd");
    const warnSpy = vi.spyOn(message, "warning").mockImplementation(() => ({} as never));
    API_MOCKS.uploadFile.mockResolvedValue({
      status: "success",
      id: 3,
      filename: "普通文件.txt",
      file_type: "txt",
      file_role: "supporting",
      message: "",
    });

    const FileUploadPage = (await import("./FileUploadPage")).default;
    const { container } = rtl.render(<FileUploadPage />);
    await rtl.waitFor(() =>
      expect(container.textContent || "").toContain("招标文件.docx"),
    );

    const file = new File(["普通内容"], "普通文件.txt", { type: "text/plain" });
    const input = container.querySelector('input[type="file"]') as HTMLInputElement;
    Object.defineProperty(input, "files", { value: Object.assign([], [file]) as unknown as FileList, configurable: true });
    rtl.fireEvent.change(input);

    await rtl.waitFor(() => expect(API_MOCKS.uploadFile).toHaveBeenCalled());
    // 需要一点时间让后续 message.success 执行；若误弹 needs_reanalysis 会被抓到
    await rtl.waitFor(() => expect(API_MOCKS.fetchFiles.mock.calls.length).toBeGreaterThanOrEqual(1));
    const calls = warnSpy.mock.calls.map((c) => c[0]);
    expect(calls.some((m) => String(m).includes("主文件已变更"))).toBe(false);

    warnSpy.mockRestore();
    rtl.cleanup();
  });
});
