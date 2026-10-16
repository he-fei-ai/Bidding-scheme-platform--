// @vitest-environment jsdom
/**
 * FileUploadPage 删除链路与异步解析轮询 —— 组件级交互测试（2026-10-16）
 *
 * 覆盖本轮（第八轮）定位并修复的两个组件级缺陷：
 *
 *  BUG-FIX 2026-10-16-A（前后端状态不同步）
 *    handleDelete 此前只做本地乐观删除、**不重新拉取列表**。删掉主文件时
 *    后端 delete_file → _promote_next_main 会把剩余文件中最合适的一个提升
 *    为 main，但前端列表仍是旧数据：被提升的文件显示「辅助文件」、整个列表
 *    一个「主文件」金色标签都不剩 —— 与刚弹出的提示「系统已自动将下一个
 *    文件设为主文件」自相矛盾，且用户随后的角色切换基于陈旧数据。
 *    同模块其余四条变更路径（上传 finally / 切角色 / 重提取 / 清空）都调了
 *    loadFiles，删除是唯一的漏网之鱼。
 *
 *  BUG-FIX 2026-10-16-B（异步解析进度缺口）
 *    R-3 起图片走后台异步解析，上传响应即时返回 status='processing'。若
 *    图片恰是本批最后一个文件，pendingRef 归零 → uploading 立即变 false
 *    → 解析状态轮询随之停止，而 OCR 还要跑几十秒：文件行一直显示「提取中」
 *    直到用户手动点刷新。轮询条件已扩展为「上传在途 或 仍有 processing
 *    文件」，终态落库并经 loadFiles 刷新后自然收敛。
 *
 * 测试走真实组件渲染（antd List / Popconfirm / message），只 mock API 层。
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
    created_at: "2026-10-16 10:00:00",
    ...partial,
  };
}

const MAIN_DOC = mkFile({ id: 1, filename: "招标文件.docx", file_type: "docx", file_role: "main" });
const BOQ_TXT = mkFile({ id: 2, filename: "工程量清单.txt", file_role: "supporting" });
// 删除 main 后后端 promote 的权威状态：清单被提升为 main
const BOQ_PROMOTED = mkFile({ id: 2, filename: "工程量清单.txt", file_role: "main" });

const API_MOCKS = {
  uploadFile: vi.fn(),
  fetchFiles: vi.fn(async () => [MAIN_DOC, BOQ_TXT] as MockFile[]),
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
    filename: "a.png",
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

// HARDENING 2026-10-16（S-3 / TD-4 闭环）：页面把异步解析任务接入全局 taskStore，
// 这里 mock 掉（真实单例的 30s 清理计时器会在测试进程留下未决 timer）。
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

/** 找到某文件名所在列表行内的指定按钮 */
function findRowButton(
  rtl: Rtl,
  filename: string,
  buttonName: RegExp,
): HTMLElement | undefined {
  return rtl.screen
    .getAllByRole("button", { name: buttonName })
    .find((b) => {
      const item = b.closest("li.ant-list-item");
      return !!item && item.textContent?.includes(filename);
    });
}

describe.skipIf(!hasDom)("FileUploadPage 删除链路与异步解析轮询（2026-10-16）", () => {
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
    API_MOCKS.fetchFiles.mockResolvedValue([MAIN_DOC, BOQ_TXT]);
    API_MOCKS.deleteFile.mockResolvedValue({ status: "deleted", file_id: 1 });
  });

  // ─── BUG-FIX 2026-10-16-A：删除后必须重新拉取服务端权威状态 ─────────

  it(
    "删除成功后重新拉取文件列表（删主文件后 promoted 状态同步到 UI）",
    async () => {
      const rtl = await loadRtl();
      if (!rtl) return;

      const { message } = await import("antd");
      const successSpy = vi.spyOn(message, "success").mockImplementation(() => ({} as never));
      const infoSpy = vi.spyOn(message, "info").mockImplementation(() => ({} as never));

      // 删除主文件：后端返回 promoted_file_id，且列表刷新后清单已被提升为 main
      API_MOCKS.deleteFile.mockResolvedValue({
        status: "deleted",
        file_id: 1,
        promoted_file_id: 2,
      });
      const calls: MockFile[][] = [
        [MAIN_DOC, BOQ_TXT],
        [BOQ_PROMOTED],
      ];
      API_MOCKS.fetchFiles.mockImplementation(async () => calls.shift() ?? [BOQ_PROMOTED]);

      const FileUploadPage = (await import("./FileUploadPage")).default;
      rtl.render(<FileUploadPage />);
      await rtl.screen.findByText("招标文件.docx");
      expect(API_MOCKS.fetchFiles.mock.calls.length).toBe(1);

      const delBtn = findRowButton(rtl, "招标文件.docx", /删除/);
      expect(delBtn, "应渲染出删除按钮").toBeTruthy();
      rtl.fireEvent.click(delBtn as HTMLElement);

      // ① 提示如实说明主文件已被自动提升
      await rtl.waitFor(() => {
        expect(infoSpy).toHaveBeenCalledWith(
          expect.stringContaining("已自动将下一个文件设为主文件"),
        );
      });
      expect(successSpy).not.toHaveBeenCalledWith("文件已删除");

      // ② 核心：删除后**重新拉取列表**（修复前只有本地乐观删除，fetchFiles
      //    停在 1 次，被提升的文件在 UI 上永远显示「辅助文件」）
      await rtl.waitFor(() => {
        expect(API_MOCKS.fetchFiles.mock.calls.length).toBeGreaterThanOrEqual(2);
      });

      // ③ 刷新后的列表里恰好一个「主文件」标签，且挂在被提升的清单上
      await rtl.waitFor(() => {
        const mainTags = rtl.screen.getAllByText("主文件");
        expect(mainTags).toHaveLength(1);
      });
      const mainTagItem = rtl.screen
        .getAllByText("主文件")[0]
        .closest("li.ant-list-item") as HTMLElement;
      expect(mainTagItem.textContent || "").toContain("工程量清单.txt");
      // 旧主文件已从列表消失
      expect(rtl.screen.queryByText("招标文件.docx")).toBeNull();

      infoSpy.mockRestore();
      successSpy.mockRestore();
      rtl.cleanup();
    },
  );

  it(
    "删除非主文件后也刷新列表（保持与其余变更路径同口径）",
    async () => {
      const rtl = await loadRtl();
      if (!rtl) return;

      const { message } = await import("antd");
      const successSpy = vi.spyOn(message, "success").mockImplementation(() => ({} as never));

      API_MOCKS.deleteFile.mockResolvedValue({ status: "deleted", file_id: 2 });

      const FileUploadPage = (await import("./FileUploadPage")).default;
      rtl.render(<FileUploadPage />);
      await rtl.screen.findByText("工程量清单.txt");

      const delBtn = findRowButton(rtl, "工程量清单.txt", /删除/);
      expect(delBtn).toBeTruthy();
      rtl.fireEvent.click(delBtn as HTMLElement);

      await rtl.waitFor(() => {
        expect(successSpy).toHaveBeenCalledWith("文件已删除");
      });
      // 无论是否触发 promote，删除后都以服务端权威状态为准
      await rtl.waitFor(() => {
        expect(API_MOCKS.fetchFiles.mock.calls.length).toBeGreaterThanOrEqual(2);
      });
      // 剩余主文件仍在列表中且仍带主文件标签
      await rtl.waitFor(() => {
        expect(rtl.screen.getByText("招标文件.docx")).toBeTruthy();
        expect(rtl.screen.getAllByText("主文件")).toHaveLength(1);
      });

      successSpy.mockRestore();
      rtl.cleanup();
    },
  );

  // ─── BUG-FIX 2026-10-16-B：processing 文件在批完成后继续轮询直至终态 ──

  it(
    "图片异步解析（processing）在上传窗口结束后继续轮询，终态落库后收敛",
    async () => {
      const rtl = await loadRtl();
      if (!rtl) return;

      const { message } = await import("antd");
      vi.spyOn(message, "success").mockImplementation(() => ({} as never));
      vi.spyOn(message, "info").mockImplementation(() => ({} as never));

      const PROCESSING_IMG = mkFile({
        id: 7,
        filename: "扫描件.png",
        file_type: "png",
        file_role: "supporting",
        status: "processing",
      });
      const DONE_IMG = mkFile({
        id: 7,
        filename: "扫描件.png",
        file_type: "png",
        file_role: "supporting",
        status: "success",
      });

      // 首屏即有一份后台解析中的图片（模拟图片上传完成、OCR 仍在跑的窗口）
      API_MOCKS.fetchFiles.mockResolvedValue([PROCESSING_IMG]);

      const FileUploadPage = (await import("./FileUploadPage")).default;
      rtl.render(<FileUploadPage />);
      await rtl.screen.findByText("扫描件.png");
      await rtl.waitFor(() => expect(rtl.screen.getByText("提取中")).toBeTruthy());

      // ① 上传并未在途（uploading=false），但 processing 文件仍在 →
      //    轮询必须持续（修复前此刻已停止，UI 永远停在「提取中」）
      await rtl.waitFor(
        () => {
          expect(API_MOCKS.fetchFileParsingStatus.mock.calls.length).toBeGreaterThanOrEqual(2);
        },
        { timeout: 8_000 },
      );
      // 轮询同时刷新文件列表（loadFiles 与状态端点同频）
      await rtl.waitFor(
        () => {
          expect(API_MOCKS.fetchFiles.mock.calls.length).toBeGreaterThanOrEqual(2);
        },
        { timeout: 8_000 },
      );

      // ② OCR 完成：列表刷新出终态 success → 轮询自然收敛
      // （等待上限需覆盖 1.5s 轮询间隔，默认 1s 会假失败）
      API_MOCKS.fetchFiles.mockResolvedValue([DONE_IMG]);
      await rtl.waitFor(() => expect(rtl.screen.getByText("已提取")).toBeTruthy(), {
        timeout: 8_000,
      });

      // effect 依赖收敛时会重建一次（带来至多一次额外的 fetchOnce），
      // 先等它落地再取基线，避免把重建抖动误判为「仍在轮询」
      await new Promise((r) => setTimeout(r, 2500));
      const settled = API_MOCKS.fetchFileParsingStatus.mock.calls.length;
      await new Promise((r) => setTimeout(r, 4000));
      expect(
        API_MOCKS.fetchFileParsingStatus.mock.calls.length,
        "终态后轮询应停止（effect 依赖收敛），不得无限空转",
      ).toBe(settled);

      rtl.cleanup();
    },
  );

  // ─── HARDENING 2026-10-16（S-3 / TD-4）：异步解析接入全局 taskStore ──────

  it(
    "图片后台解析接入全局 taskStore：注册 → 终态完成 → 卸载清理",
    async () => {
      const rtl = await loadRtl();
      if (!rtl) return;

      const { taskStore } = await import("@/services/taskStore");
      const { message } = await import("antd");
      vi.spyOn(message, "success").mockImplementation(() => ({} as never));
      vi.spyOn(message, "info").mockImplementation(() => ({} as never));

      const PROCESSING_IMG = mkFile({
        id: 7,
        filename: "扫描件.png",
        file_type: "png",
        file_role: "supporting",
        status: "processing",
      });
      const DONE_IMG = mkFile({
        id: 7,
        filename: "扫描件.png",
        file_type: "png",
        file_role: "supporting",
        status: "success",
      });

      API_MOCKS.fetchFiles.mockResolvedValue([PROCESSING_IMG]);

      const FileUploadPage = (await import("./FileUploadPage")).default;
      rtl.render(<FileUploadPage />);

      // ① 出现 processing 文件 → 注册 file_parse 任务（TD-4 生产端闭环）
      await rtl.waitFor(() => {
        expect(taskStore.startTask).toHaveBeenCalledTimes(1);
      });
      const started = (taskStore.startTask as ReturnType<typeof vi.fn>).mock.calls[0][0];
      expect(started.taskType).toBe("file_parse");
      expect(started.projectId).toBe(1);
      expect(started.taskId).toBe("file_parse_7");
      expect(started.message).toContain("扫描件.png");

      // ② 终态 success 落库 → completeTask(completed)，任务离开活跃列表
      API_MOCKS.fetchFiles.mockResolvedValue([DONE_IMG]);
      await rtl.waitFor(() => expect(rtl.screen.getByText("已提取")).toBeTruthy(), {
        timeout: 8_000,
      });
      await rtl.waitFor(() => {
        expect(taskStore.completeTask).toHaveBeenCalledWith(
          "file_parse_7",
          "completed",
          expect.stringContaining("已完成"),
        );
      });

      // ③ 终态完成后注册表已清空：卸载时**无需** stopTracking（completeTask
      //    已让任务离开活跃列表，taskStore 的 30s 计时器负责回收）——
      //    这里反向锁定「终态后无残留追踪」。
      rtl.cleanup();
      expect(taskStore.stopTracking).not.toHaveBeenCalled();
    },
  );

  it(
    "未终态任务在页面卸载时被 stopTracking，不悬挂在顶栏",
    async () => {
      const rtl = await loadRtl();
      if (!rtl) return;

      const { taskStore } = await import("@/services/taskStore");
      const { message } = await import("antd");
      vi.spyOn(message, "success").mockImplementation(() => ({} as never));
      vi.spyOn(message, "info").mockImplementation(() => ({} as never));

      const PROCESSING_IMG = mkFile({
        id: 9,
        filename: "慢扫描件.png",
        file_type: "png",
        file_role: "supporting",
        status: "processing",
      });
      API_MOCKS.fetchFiles.mockResolvedValue([PROCESSING_IMG]);

      const FileUploadPage = (await import("./FileUploadPage")).default;
      rtl.render(<FileUploadPage />);
      await rtl.waitFor(() => {
        expect(taskStore.startTask).toHaveBeenCalledTimes(1);
      });

      // OCR 未结束时用户离开页面：轮询随卸载停止，若不 stopTracking，
      // 顶栏会永久显示「文件解析 0%」（completeTask 的 30s 清理只回收终态任务）
      rtl.cleanup();
      await rtl.waitFor(() => {
        expect(taskStore.stopTracking).toHaveBeenCalledWith("file_parse_9");
      });
    },
  );

  it(
    "解析失败的文件以 failed 态收尾全局任务",
    async () => {
      const rtl = await loadRtl();
      if (!rtl) return;

      const { taskStore } = await import("@/services/taskStore");
      const { message } = await import("antd");
      vi.spyOn(message, "success").mockImplementation(() => ({} as never));

      const PROCESSING_IMG = mkFile({
        id: 8,
        filename: "坏扫描件.png",
        file_type: "png",
        file_role: "supporting",
        status: "processing",
      });
      const FAILED_IMG = mkFile({
        id: 8,
        filename: "坏扫描件.png",
        file_type: "png",
        file_role: "supporting",
        status: "failed",
        message: "OCR 引擎不可用",
      });

      API_MOCKS.fetchFiles.mockResolvedValue([PROCESSING_IMG]);

      const FileUploadPage = (await import("./FileUploadPage")).default;
      rtl.render(<FileUploadPage />);
      await rtl.waitFor(() => {
        expect(taskStore.startTask).toHaveBeenCalledTimes(1);
      });

      API_MOCKS.fetchFiles.mockResolvedValue([FAILED_IMG]);
      await rtl.waitFor(() => expect(rtl.screen.getByText("提取失败")).toBeTruthy(), {
        timeout: 8_000,
      });
      await rtl.waitFor(() => {
        expect(taskStore.completeTask).toHaveBeenCalledWith(
          "file_parse_8",
          "failed",
          expect.stringContaining("解析失败"),
        );
      });

      rtl.cleanup();
    },
  );

  // ─── HARDENING 2026-10-16（S-4）：静态守卫，防 BUG-FIX B 的轮询条件被回退 ──

  it(
    "静态守卫：解析轮询条件不得回退为仅 uploading（否则 processing 窗口失明复现）",
    async () => {
      const { readFileSync } = await import("node:fs");
      const { resolve } = await import("node:path");
      // 注：jsdom 环境下 import.meta.url 不是 file:// 方案（new URL 会抛
      // "The URL must be of scheme file"），用 cwd 定位源码文件。
      const src = readFileSync(
        resolve(process.cwd(), "src/pages/FileUploadPage.tsx"),
        "utf-8",
      );
      // BUG-FIX 2026-10-16-B 的核心是一行条件；任何人「顺手优化」回
      // `if (!uploading)` 都会让 processing 文件重新失明（UI 永卡「提取中」），
      // 而现有 DOM 用例在热缓存下不一定复现时序，用静态守卫兜底。
      expect(src).toContain("!uploading && !hasProcessingFiles");
      expect(src).toContain("hasProcessingFiles");
    },
  );
});
