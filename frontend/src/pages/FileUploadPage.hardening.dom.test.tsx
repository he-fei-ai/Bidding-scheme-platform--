// @vitest-environment jsdom
/**
 * 上传解析模块 —— 2026-10-26 第九轮组件级交互测试（HARDENING）
 *
 * 覆盖九轮审计中修复的高危缺陷的**组件级回归**（既有 4 个 dom 测试文件均未覆盖）：
 *
 *   1. loadFiles 失败必须可见 —— 原 catch 完全静默且 loading 永不置 true，
 *      项目被删 / 403 无权访问时用户对着「暂无文件」的空态继续上传。
 *   2. 删除必须经过二次确认 —— 删除不可逆，原实现连点还会触发第二次 404。
 *   3. 「下一步」流程门禁 —— 原判定只看 files.length > 0，只上传了 OCR 失败的
 *      扫描件也能点进提取页，直到点「生成摘要」才被后端 400 拦住。
 *   4. OCR 关闭时必须提示 —— 原 ocr_enabled 字段后端下发、前端零消费，
 *      OCR 关闭时仍鼓励上传图片，用户必然得到「解析成功但文本为空」。
 *   5. pending 文件不得注册全局解析任务（幽灵任务）—— pending 只有
 *      clear_project_texts 一个写入方，它不会派发任何解析任务。
 *   6. 超过大小上限的文件也计入批尾汇总（R-4）—— 原实现超限分支在
 *      pendingRef 递增前就 return，10 个文件全超限时用户只看到 10 条独立
 *      toast，没有任何「本批结果」提示。
 *   7. 「清空提取」返回 files_kept_processing 时如实告知在途文件未受影响。
 *
 * 运行条件：jsdom + @testing-library/react（已在 devDependencies）；
 * 依赖未安装时整组 skip，不阻塞纯函数测试。
 */
import { describe, it, expect, vi, beforeAll, afterEach } from "vitest";

const hasDom = typeof window !== "undefined" && typeof document !== "undefined";

type MockFile = {
  id: number;
  filename: string;
  file_type: string;
  file_role: "main" | "supporting";
  status: string;
  message: string;
  created_at: string;
};

function mkFile(p: Partial<MockFile> & { id: number; filename: string }): MockFile {
  return {
    file_type: "docx",
    file_role: "main",
    status: "success",
    message: "",
    created_at: "2026-10-26 10:00:00",
    ...p,
  };
}

const OK_FILES: MockFile[] = [mkFile({ id: 1, filename: "招标文件.docx" })];

const FAILED_ONLY: MockFile[] = [
  mkFile({
    id: 1,
    filename: "扫描件.png",
    file_type: "png",
    file_role: "supporting",
    status: "failed",
    message: "图片文字识别不可用",
  }),
];

const PROCESSING_ONLY: MockFile[] = [
  mkFile({
    id: 1,
    filename: "扫描件.png",
    file_type: "png",
    file_role: "supporting",
    status: "processing",
  }),
];

const PENDING_ONLY: MockFile[] = [
  mkFile({
    id: 1,
    filename: "工程量清单.txt",
    file_type: "txt",
    file_role: "supporting",
    status: "pending",
  }),
];

const CLEAR_TARGETS: MockFile[] = [
  mkFile({ id: 1, filename: "招标文件.docx" }),
  mkFile({
    id: 2,
    filename: "扫描件.png",
    file_type: "png",
    file_role: "supporting",
    status: "processing",
  }),
];

const SUPPORTED_FORMATS = {
  extensions: [".docx", ".pdf", ".txt", ".png"],
  accept: ".docx,.pdf,.txt,.png",
  image_extensions: [".png"],
  max_file_size: 50 * 1024 * 1024,
  max_file_size_mb: 50,
  max_file_size_exact_mb: 50,
  ocr_enabled: true,
  ocr_max_file_size_mb: 50,
};

const API_MOCKS = {
  uploadFile: vi.fn(async () => ({ status: "success" as const })),
  fetchFiles: vi.fn(async (): Promise<MockFile[]> => OK_FILES),
  deleteFile: vi.fn(async (): Promise<{
    status: string;
    file_id: number;
    promoted_file_id?: number | null;
  }> => ({ status: "deleted", file_id: 1 })),
  reExtractFileText: vi.fn(async () => ({
    status: "success" as const,
    text_length: 10,
    message: "",
  })),
  updateFileRole: vi.fn(async () => ({ status: "success", demoted_file_ids: [] })),
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
    status: "cleared" as const,
    files_reset: 1,
    tasks_cleared: 0,
    files_kept_processing: 0,
  })),
  fetchSupportedFormats: vi.fn(async () => SUPPORTED_FORMATS),
  getFileText: vi.fn(async () => "正文"),
};

vi.mock("../api", async () => {
  const actual = await vi.importActual<typeof import("../api")>("../api");
  return {
    ...actual,
    uploadFile: API_MOCKS.uploadFile,
    fetchFiles: API_MOCKS.fetchFiles,
    deleteFile: API_MOCKS.deleteFile,
    reExtractFileText: API_MOCKS.reExtractFileText,
    updateFileRole: API_MOCKS.updateFileRole,
    fetchFileParsingStatus: API_MOCKS.fetchFileParsingStatus,
    asyncReparseFile: API_MOCKS.asyncReparseFile,
    clearProjectTexts: API_MOCKS.clearProjectTexts,
    fetchSupportedFormats: API_MOCKS.fetchSupportedFormats,
    getFileText: API_MOCKS.getFileText,
  };
});

// 与 FileUploadPage.delete.dom.test.tsx 同口径：taskStore 必须 mock，
// 否则 startTask 是真实实例方法、没有 .mock 元数据可断言
vi.mock("@/services/taskStore", () => {
  const inst = {
    startTask: vi.fn(),
    completeTask: vi.fn(),
    stopTracking: vi.fn(),
  };
  return { taskStore: inst, default: inst };
});

vi.mock("react-router-dom", async () => {
  const actual = await vi.importActual<Record<string, unknown>>("react-router-dom");
  const navigateSpy = vi.fn();
  return {
    ...actual,
    useParams: () => ({ id: "1" }),
    useNavigate: () => navigateSpy,
    __navigateSpy: navigateSpy,
  };
});

async function loadRtl() {
  try {
    return await import("@testing-library/react");
  } catch {
    return null;
  }
}

type Rtl = NonNullable<Awaited<ReturnType<typeof loadRtl>>>;

function makeFile(name: string, size = 1024): File {
  return new File([new Uint8Array(size)], name, {
    type: "application/octet-stream",
  });
}

/**
 * 找到 container 内文本匹配的按钮（限定作用域，避免多个实例/弹层串扰）。
 */
function findButton(container: HTMLElement, text: string): HTMLElement {
  const btn = Array.from(container.querySelectorAll("button")).find((b) =>
    (b.textContent || "").includes(text),
  );
  expect(btn, `应渲染出包含「${text}」的按钮`).toBeTruthy();
  return btn as HTMLElement;
}

/**
 * 取最新一个 Popconfirm 弹层里的确认/取消按钮。
 *
 * antd 的弹层渲染在 body 的 portal 里且没有 button role 可达性树，
 * 只能按 .ant-popconfirm + textContent 定位（与 .dom.test.tsx 同口径）。
 */
async function openConfirm(rtl: Rtl, okText: string) {
  await rtl.waitFor(() => {
    expect(document.querySelectorAll(".ant-popconfirm").length).toBeGreaterThan(0);
  });
  const pops = document.querySelectorAll(".ant-popconfirm");
  const pop = pops[pops.length - 1] as HTMLElement;
  const byText = (t: string) =>
    Array.from(pop.querySelectorAll("button")).find((b) =>
      (b.textContent || "").includes(t),
    ) as HTMLElement | undefined;
  const ok = byText(okText);
  expect(ok, `弹层内应渲染出「${okText}」按钮`).toBeTruthy();
  // 取消按钮文案由 antd locale 决定（未配中文 locale 时为 "Cancel"），
  // 故取弹层内除确认键之外的第一个按钮
  const cancel = Array.from(pop.querySelectorAll("button"))
    .filter((b) => b !== (ok as HTMLElement))
    .find((b) => (b.textContent || "").trim().length > 0) as HTMLElement | undefined;
  return { ok: ok as HTMLElement, cancel };
}

describe.skipIf(!hasDom)("FileUploadPage 第九轮加固（2026-10-26）", () => {
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
    API_MOCKS.fetchFiles.mockResolvedValue(OK_FILES);
    API_MOCKS.fetchSupportedFormats.mockResolvedValue(SUPPORTED_FORMATS);
    API_MOCKS.deleteFile.mockResolvedValue({ status: "deleted", file_id: 1 });
    API_MOCKS.uploadFile.mockResolvedValue({ status: "success" });
    API_MOCKS.clearProjectTexts.mockResolvedValue({
      status: "cleared",
      files_reset: 1,
      tasks_cleared: 0,
      files_kept_processing: 0,
    });
  });

  it(
    "文件列表加载失败必须显示错误态，不得谎报「暂无文件」（含重试入口）",
    async () => {
      const rtl = await loadRtl();
      if (!rtl) return;

      API_MOCKS.fetchFiles.mockRejectedValue({
        message: "Request failed",
        response: { data: { detail: "禁止访问，权限不足" } },
      });

      const Page = (await import("./FileUploadPage")).default;
      const { container } = rtl.render(<Page />);

      await rtl.waitFor(() =>
        expect(container.textContent || "").toContain("文件列表加载失败"),
      );
      expect(
        container.textContent || "",
        "失败态不得与空态混淆",
      ).not.toContain("暂无文件，请先上传招标文件");
      expect(container.textContent || "").toContain("禁止访问，权限不足");
      // 提供重试入口
      const retryBtn = Array.from(container.querySelectorAll("button")).filter((b) =>
        (b.textContent || "").includes("刷新"),
      );
      expect(retryBtn.length, "失败态应提供刷新重试入口").toBeGreaterThanOrEqual(1);

      rtl.cleanup();
    },
  );

  it(
    "删除必须经过二次确认：点删除按钮不直接调用 deleteFile，取消后仍可删除",
    async () => {
      const rtl = await loadRtl();
      if (!rtl) return;

      API_MOCKS.fetchFiles.mockResolvedValue(OK_FILES);
      const Page = (await import("./FileUploadPage")).default;
      const { container } = rtl.render(<Page />);
      await rtl.waitFor(() =>
        expect(container.textContent || "").toContain("招标文件.docx"),
      );

      const delBtn = findButton(container, "删除");
      rtl.fireEvent.click(delBtn);

      // ① 只弹出确认框，尚未真正删除
      expect(API_MOCKS.deleteFile, "未确认前不得调用 deleteFile").not.toHaveBeenCalled();
      const { ok, cancel } = await openConfirm(rtl, "确认删除");
      expect(cancel, "应渲染出取消按钮").toBeTruthy();

      // ② 取消：仍然不删除，文件行保留
      rtl.fireEvent.click(cancel!);
      expect(API_MOCKS.deleteFile).not.toHaveBeenCalled();
      expect(container.textContent || "").toContain("招标文件.docx");

      // ③ 再次点击并确认：这次才真正调用
      rtl.fireEvent.click(delBtn);
      const second = await openConfirm(rtl, "确认删除");
      rtl.fireEvent.click(second.ok);
      await rtl.waitFor(() => {
        expect(API_MOCKS.deleteFile).toHaveBeenCalledWith(1, 1);
      });

      rtl.cleanup();
    },
  );

  it(
    "无已提取文本时「下一步」不放行（在途→等解析完成；失败→先重新提取）",
    async () => {
      const rtl = await loadRtl();
      if (!rtl) return;

      const { message } = await import("antd");
      const warnSpy = vi.spyOn(message, "warning").mockImplementation(() => ({} as never));

      // 首屏：只有一份解析中的文件 → 应提示「请等解析完成」
      API_MOCKS.fetchFiles.mockResolvedValue(PROCESSING_ONLY);
      const Page = (await import("./FileUploadPage")).default;
      const { container } = rtl.render(<Page />);
      await rtl.waitFor(() =>
        expect(container.textContent || "").toContain("扫描件.png"),
      );

      const nextBtn = findButton(container, "下一步：要点提取");
      rtl.fireEvent.click(nextBtn);
      await rtl.waitFor(() =>
        expect(warnSpy).toHaveBeenCalledWith(
          "有文件正在解析中，请等解析完成后再进入要点提取",
        ),
      );

      // 刷新成全部失败 → 应换成「没有已提取到文本」的文案
      API_MOCKS.fetchFiles.mockResolvedValue(FAILED_ONLY);
      rtl.fireEvent.click(findButton(container, "刷新"));
      await rtl.waitFor(() => {
        expect(container.textContent || "").not.toContain("提取中");
      });
      rtl.fireEvent.click(findButton(container, "下一步：要点提取"));
      await rtl.waitFor(() =>
        expect(warnSpy).toHaveBeenCalledWith(
          "没有已提取到文本的文件，请先点「重新提取」或重新上传可解析的文件",
        ),
      );

      warnSpy.mockRestore();
      rtl.cleanup();
    },
  );

  it(
    "OCR 关闭时必须展示告警（不再零消费 ocr_enabled）",
    async () => {
      const rtl = await loadRtl();
      if (!rtl) return;

      API_MOCKS.fetchSupportedFormats.mockResolvedValue({
        ...SUPPORTED_FORMATS,
        ocr_enabled: false,
      });

      const Page = (await import("./FileUploadPage")).default;
      const { container } = rtl.render(<Page />);

      await rtl.waitFor(() =>
        expect(container.textContent || "").toContain("OCR 引擎未启用"),
      );
      expect(container.textContent || "").toContain("无法解析出文本");

      // 反向对照：OCR 可用时不得出现该告警
      expect(container.textContent || "").not.toContain("请到「图片配置」");
      // 提示指向解决路径
      expect(container.textContent || "").toContain("启用 OCR 引擎");

      rtl.cleanup();
    },
  );

  it(
    "pending 文件不得注册全局解析任务（清空提取不再制造幽灵任务）",
    async () => {
      const rtl = await loadRtl();
      if (!rtl) return;

      const { taskStore } = await import("@/services/taskStore");
      const { message } = await import("antd");
      vi.spyOn(message, "success").mockImplementation(() => ({} as never));
      vi.spyOn(message, "warning").mockImplementation(() => ({} as never));

      API_MOCKS.fetchFiles.mockResolvedValue(PENDING_ONLY);

      const Page = (await import("./FileUploadPage")).default;
      const { container } = rtl.render(<Page />);
      await rtl.waitFor(() =>
        expect(container.textContent || "").toContain("工程量清单.txt"),
      );

      // pending 是「已入库、从未解析」的静态态，不允许有任何解析任务在跑
      await new Promise((r) => setTimeout(r, 300));
      expect(
        (taskStore.startTask as ReturnType<typeof vi.fn>).mock.calls.filter(
          (c: unknown[]) => (c[0] as { taskType: string }).taskType === "file_parse",
        ),
        "pending 文件不应注册 file_parse 任务（清空提取后不应出现永不进终态的幽灵任务）",
      ).toHaveLength(0);

      // 对照：同一页面出现 processing 文件时应正常注册（证明修复不是把注册关掉了）
      API_MOCKS.fetchFiles.mockResolvedValue(PROCESSING_ONLY);
      rtl.fireEvent.click(findButton(container, "刷新"));
      await rtl.waitFor(() => {
        expect(
          (taskStore.startTask as ReturnType<typeof vi.fn>).mock.calls.filter(
            (c: unknown[]) => (c[0] as { taskType: string }).taskType === "file_parse",
          ),
        ).toHaveLength(1);
      });

      rtl.cleanup();
    },
  );

  it(
    "超过大小上限的文件也计入批尾汇总（R-4：前置校验失败不再漏计）",
    async () => {
      const rtl = await loadRtl();
      if (!rtl) return;

      const { message } = await import("antd");
      const warnSpy = vi.spyOn(message, "warning").mockImplementation(() => ({} as never));
      const errorSpy = vi.spyOn(message, "error").mockImplementation(() => ({} as never));

      // 把上限压到 1MB，两个 2MB 文件都应被前端拦下
      API_MOCKS.fetchFiles.mockResolvedValue([]);
      API_MOCKS.fetchSupportedFormats.mockResolvedValue({
        ...SUPPORTED_FORMATS,
        max_file_size: 1024 * 1024,
        max_file_size_mb: 1,
        max_file_size_exact_mb: 1,
      });

      const Page = (await import("./FileUploadPage")).default;
      const { container } = rtl.render(<Page />);
      await rtl.waitFor(() =>
        expect(container.textContent || "").toContain("暂无文件"),
      );

      const input = container.querySelector(
        'input[type="file"]',
      ) as HTMLInputElement;
      expect(input, "必须渲染出文件输入框").toBeTruthy();
      Object.defineProperty(input, "files", {
        value: Object.assign([], [
          makeFile("大1.txt", 2 * 1024 * 1024),
          makeFile("大2.txt", 2 * 1024 * 1024),
        ]) as unknown as FileList,
        configurable: true,
      });
      rtl.fireEvent.change(input);

      // ① 两份都被前端拦下，未发起任何上传请求
      await rtl.waitFor(() => {
        expect(errorSpy).toHaveBeenCalledTimes(2);
      });
      expect(API_MOCKS.uploadFile, "超限文件不应请求后端").not.toHaveBeenCalled();

      // ② 核心：批尾汇总仍然出现（修复前 pendingRef 在递增前就 return，
      //    两份都超限 → 只看到 2 条独立 toast、没有任何「本批结果」）
      await rtl.waitFor(() => {
        expect(warnSpy).toHaveBeenCalledWith(
          "批量上传完成：成功 0 个，上传失败 2 个",
        );
      });
      expect(warnSpy).toHaveBeenCalledTimes(1);

      // ③ 上传窗口正确结束，拖拽区恢复
      await rtl.waitFor(() =>
        expect(container.querySelector(".ant-upload-drag-icon")).toBeTruthy(),
      );

      warnSpy.mockRestore();
      errorSpy.mockRestore();
      rtl.cleanup();
    },
  );

  it(
    "「清空提取」返回 files_kept_processing 时如实告知在途文件未受影响",
    async () => {
      const rtl = await loadRtl();
      if (!rtl) return;

      const { message } = await import("antd");
      const successSpy = vi.spyOn(message, "success").mockImplementation(() => ({} as never));
      vi.spyOn(message, "warning").mockImplementation(() => ({} as never));
      vi.spyOn(message, "error").mockImplementation(() => ({} as never));

      API_MOCKS.fetchFiles.mockResolvedValue(CLEAR_TARGETS);
      API_MOCKS.clearProjectTexts.mockResolvedValue({
        status: "cleared",
        files_reset: 1,
        tasks_cleared: 0,
        files_kept_processing: 1,
      });

      const Page = (await import("./FileUploadPage")).default;
      const { container } = rtl.render(<Page />);
      await rtl.waitFor(() =>
        expect(container.textContent || "").toContain("招标文件.docx"),
      );

      rtl.fireEvent.click(findButton(container, "清空提取"));
      const { ok } = await openConfirm(rtl, "确认清空");
      rtl.fireEvent.click(ok);

      await rtl.waitFor(() => {
        expect(successSpy).toHaveBeenCalledWith(
          expect.stringContaining("1 个正在解析中的文件未受影响"),
        );
      });
      // 后端确实被调用
      expect(API_MOCKS.clearProjectTexts).toHaveBeenCalledWith(1);

      successSpy.mockRestore();
      rtl.cleanup();
    },
  );
});
