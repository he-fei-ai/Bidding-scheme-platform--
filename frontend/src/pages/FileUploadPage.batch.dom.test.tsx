// @vitest-environment jsdom
/**
 * 批量上传前端状态机 —— 组件级并发交互测试（TD-E2 · 2026-10-15c）
 *
 * TD-E2 此前只有纯函数覆盖（utils.upload.test.ts 的 accumulateBatchTotals /
 * shouldNotifySingle / formatBatchSummary），而真正会「状态错乱」的是组件内那三处
 * 接线：pendingRef 的增减、uploadingCount 驱动的 UI 分支、batchRef 在 pending 归零
 * 时的汇总与重置。BUG-FIX 2026-10-08（单个 uploading 布尔被并发回调覆盖 → 首份文件
 * 完成即解除禁用、其余仍在传，用户可重复触发）与 BUG-FIX 2026-10-05 B1（解析失败
 * 计入成功 → 批尾谎报「全部成功」）都发生在这一层，而 FileUploadPage.dom.test.tsx
 * 里的「批量上传」用例只断言了空态文案，从未真正触发过一次上传 —— 这两类缺陷
 * 因此没有任何回归防线。
 *
 * 这里用可控 deferred promise 精确决定每份文件的完成时刻，走真实的
 * antd Upload → customRequest → 状态机链路（不 mock antd Upload，避免只测到壳）。
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

const NO_FILES: unknown[] = [];

const API_MOCKS = {
  uploadFile: vi.fn(),
  fetchFiles: vi.fn(async () => NO_FILES),
  deleteFile: vi.fn(async () => ({ status: "deleted", file_id: 1 })),
  reExtractFileText: vi.fn(async () => ({
    status: "success",
    text_length: 10,
    message: "",
  })),
  updateFileRole: vi.fn(async () => ({
    status: "success",
    demoted_file_ids: [],
    needs_reanalysis: false,
  })),
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
    filename: "a.txt",
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

/** 可控 deferred：让测试精确决定每份文件的完成时刻 */
function makeDeferred<T = unknown>() {
  let resolve!: (v: T) => void;
  let reject!: (e: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

function makeFile(name: string): File {
  return new File([`内容-${name}`], name, { type: "text/plain" });
}

describe.skipIf(!hasDom)("FileUploadPage 批量上传状态机（TD-E2）", () => {
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
    API_MOCKS.fetchFiles.mockResolvedValue(NO_FILES);
    API_MOCKS.fetchSupportedFormats.mockResolvedValue(SUPPORTED_FORMATS);
  });

  /** 挂载页面并等首屏空态渲染完成 */
  async function renderIdle(rtl: Rtl) {
    const FileUploadPage = (await import("./FileUploadPage")).default;
    const { container } = rtl.render(<FileUploadPage />);
    await rtl.waitFor(() =>
      expect(container.textContent || "").toContain("暂无文件"),
    );
    return container;
  }

  /** 通过真实文件输入框触发上传（走完整 antd Upload → customRequest 链路）
   *
   * 注：当前 jsdom 版本未提供 DataTransfer，改用可枚举的类 FileList 数组 ——
   * rc-upload 只依赖 `files.length` 与按下标取值，普通数组即可满足。
   */
  function pickFiles(rtl: Rtl, container: HTMLElement, files: File[]) {
    const fileArr = Object.assign([], files) as unknown as FileList;
    const input = container.querySelector(
      'input[type="file"]',
    ) as HTMLInputElement;
    expect(input, "必须渲染出文件输入框").toBeTruthy();
    Object.defineProperty(input, "files", { value: fileArr, configurable: true });
    rtl.fireEvent.change(input);
  }

  it(
    "三份并发：在途期间拖拽区保持禁用，全部完成后才恢复，批尾汇总只提示一次",
    async () => {
      const rtl = await loadRtl();
      if (!rtl) return;

      const { message } = await import("antd");
      const warnSpy = vi.spyOn(message, "warning").mockImplementation(() => ({} as never));
      const successSpy = vi.spyOn(message, "success").mockImplementation(() => ({} as never));
      const errorSpy = vi.spyOn(message, "error").mockImplementation(() => ({} as never));

      // 三份文件的完成时序由测试精确控制
      const deferred = new Map<string, ReturnType<typeof makeDeferred>>();
      API_MOCKS.uploadFile.mockImplementation((_pid: number, file: File) => {
        const d = makeDeferred();
        deferred.set(file.name, d);
        return d.promise;
      });

      const container = await renderIdle(rtl);
      expect(container.querySelector(".ant-upload-drag-icon"), "上传前应有拖拽提示").toBeTruthy();

      pickFiles(rtl, container, [makeFile("A.txt"), makeFile("B.txt"), makeFile("C.txt")]);

      // ① 三份同时在途：拖拽区被进度面板替换（uploading === true）
      await rtl.waitFor(() => expect(deferred.size).toBe(3));
      expect(API_MOCKS.uploadFile).toHaveBeenCalledTimes(3);
      expect(
        container.querySelector(".ant-upload-drag-icon"),
        "三份在途时拖拽区应被进度面板替换",
      ).toBeNull();

      // ② A 成功：本批 >1 份，不得逐条刷「上传成功」；且 2 份仍在途，不得解除禁用
      deferred.get("A.txt")!.resolve({ status: "success", id: 1, filename: "A.txt" });
      await rtl.waitFor(() =>
        expect(API_MOCKS.fetchFiles.mock.calls.length).toBeGreaterThanOrEqual(2),
      );
      expect(successSpy, "批量场景不应逐条提示上传成功").not.toHaveBeenCalled();
      expect(
        container.querySelector(".ant-upload-drag-icon"),
        "仍有 2 份在途，不应恢复拖拽区（回归 2026-10-08 单布尔被并发覆盖）",
      ).toBeNull();

      // ③ B 解析失败：走 error 提示，而不是被计入成功
      deferred
        .get("B.txt")!
        .resolve({ status: "failed", id: 2, filename: "B.txt", message: "无法提取文本" });
      await rtl.waitFor(() =>
        expect(errorSpy).toHaveBeenCalledWith(expect.stringContaining("B.txt 解析失败")),
      );

      // ④ C 请求失败：计入上传失败
      deferred.get("C.txt")!.reject(new Error("网络中断"));
      await rtl.waitFor(() =>
        expect(errorSpy).toHaveBeenCalledWith(expect.stringContaining("C.txt 上传失败")),
      );

      // ⑤ 全部完成：拖拽区恢复，批尾汇总恰好一次且如实包含三类结果
      await rtl.waitFor(() =>
        expect(container.querySelector(".ant-upload-drag-icon")).toBeTruthy(),
      );
      expect(warnSpy).toHaveBeenCalledTimes(1);
      expect(warnSpy.mock.calls[0][0]).toBe(
        "批量上传完成：成功 1 个，解析失败 1 个，上传失败 1 个",
      );

      warnSpy.mockRestore();
      successSpy.mockRestore();
      errorSpy.mockRestore();
      rtl.cleanup();
    },
  );

  it(
    "单文件上传：逐条提示成功且不出现批尾汇总（shouldNotifySingle/formatBatchSummary 接线）",
    async () => {
      const rtl = await loadRtl();
      if (!rtl) return;

      const { message } = await import("antd");
      const warnSpy = vi.spyOn(message, "warning").mockImplementation(() => ({} as never));
      const successSpy = vi.spyOn(message, "success").mockImplementation(() => ({} as never));

      const deferred = makeDeferred();
      API_MOCKS.uploadFile.mockImplementation(() => deferred.promise);

      const container = await renderIdle(rtl);
      pickFiles(rtl, container, [makeFile("单文件.txt")]);
      await rtl.waitFor(() => expect(API_MOCKS.uploadFile).toHaveBeenCalledTimes(1));

      deferred.resolve({ status: "success", id: 1, filename: "单文件.txt" });

      // 单份在途且本批仅 1 个 → 逐条提示，批尾汇总必须缺席（否则会重复打扰）
      await rtl.waitFor(() =>
        expect(successSpy).toHaveBeenCalledWith("单文件.txt 上传成功"),
      );
      expect(warnSpy).not.toHaveBeenCalled();

      warnSpy.mockRestore();
      successSpy.mockRestore();
      rtl.cleanup();
    },
  );
});
