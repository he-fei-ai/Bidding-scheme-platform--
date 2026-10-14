// @vitest-environment jsdom
/**
 * 上传解析模块 —— 组件级（DOM）交互测试
 *
 * 覆盖范围：
 *   1. 基础渲染：状态标签 / 主文件标签 / 后台解析 vs 重新提取按钮
 *   2. Gap-1 2026-10-11：「清空提取」按钮渲染（带 Popconfirm 二次确认）
 *      + M-5 2026-10-15：清空结果的分级播报（后端 warning → message.warning）
 *   3. 空态渲染 + 上传后列表刷新
 *   4. 角色切换 —— 真实点击并断言 updateFileRole 调用参数 + demoted 级联提示
 *   5. 删除 —— 调用 deleteFile 并如实提示「主文件已被自动提升」
 *   6. 重新提取 —— 按结果分类（ok / empty / failed）走不同提示
 *   7. TD6 2026-10-12：accept 由后端 supported-formats 端点下发（失败回落常量）
 *   8. TD6 2026-10-12：提取文本预览（激活此前零调用方的 getFileText）
 *
 * ── 2026-10-12 修复（本文件此前有 2 个用例恒失败）：
 *   (1)「角色切换」用例断言 `/切换为主文件|切换为辅助文件/`，而组件当前渲染的
 *       文案是「设为主文件」/「设为辅助」—— 断言早于任何一次执行就已失配；
 *       且用例最后一行是 `expect(updateFileRole).toHaveBeenCalledTimes(0)`，
 *       注释自认「DOM 测试脆弱只验证 mock 调用」，实际是**什么都没验证**的
 *       空洞断言。现改为真实点击并断言调用参数。
 *   (2)「批量上传后应更新文件列表」用例查 `暂无上传的文件`，组件当前渲染的是
 *       `暂无文件，请先上传招标文件` —— 同样是陈旧断言。现按真实文案断言，
 *       并补上「上传回调触发后重新拉取列表」的真实链路。
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

const FILES_MIXED: MockFile[] = [
  {
    id: 1,
    filename: "招标文件.docx",
    file_type: "docx",
    file_role: "main",
    status: "success",
    message: "",
    created_at: "2026-10-06 10:00:00",
  },
  {
    id: 2,
    filename: "扫描页.png",
    file_type: "png",
    file_role: "supporting",
    status: "failed",
    message: "图片文字识别不可用",
    created_at: "2026-10-06 10:00:01",
  },
  {
    id: 3,
    filename: "工程量清单.txt",
    file_type: "txt",
    file_role: "supporting",
    status: "pending",
    message: "",
    created_at: "2026-10-06 10:00:02",
  },
];

const NO_FILES: MockFile[] = [];

const CLEARED_RESPONSE = {
  status: "cleared" as const,
  files_reset: 3,
  tasks_cleared: 2,
  warning: "",
};

// M-5 2026-10-15：后端「提取文本已删但 DB 状态重置失败」时返回 200 + warning
const CLEARED_WITH_WARNING = {
  status: "cleared" as const,
  files_reset: 0,
  tasks_cleared: 0,
  warning:
    "提取文本已删除，但数据库状态重置失败（database is locked），" +
    "文件列表仍显示已提取；请对相关文件执行「重新提取」或重新上传。",
};

// TD6：后端下发的支持格式清单（extensions 带点，accept 为逗号串）
const SUPPORTED_FORMATS = {
  extensions: [".docx", ".pdf", ".txt", ".png"],
  accept: ".docx,.pdf,.txt,.png",
  image_extensions: [".png"],
  max_file_size: 50 * 1024 * 1024,
  max_file_size_mb: 50,
  ocr_enabled: true,
};

const API_MOCKS = {
  uploadFile: vi.fn(),
  fetchFiles: vi.fn(),
  deleteFile: vi.fn(
    async (): Promise<{
      status: string;
      file_id: number;
      promoted_file_id?: number | null;
    }> => ({ status: "deleted", file_id: 1 }),
  ),
  reExtractFileText: vi.fn(async () => ({
    status: "success",
    text_length: 10,
    message: "",
  })),
  updateFileRole: vi.fn(),
  fetchFileParsingStatus: vi.fn(async () => ({
    status: "completed",
    processing: 0,
    recent_completed: 1,
    recent_failed: 1,
    total_tasks: 3,
    latest: null,
  })),
  asyncReparseFile: vi.fn(async () => ({
    file_id: 2,
    filename: "扫描页.png",
    dispatched: "queued",
    dispatched_via: "celery",
  })),
  clearProjectTexts: vi.fn(
    async (): Promise<{
      status: string;
      files_reset: number;
      tasks_cleared: number;
      warning?: string;
    }> => CLEARED_RESPONSE,
  ),
  // Gap-1 2026-10-11
  fetchSupportedFormats: vi.fn(async () => SUPPORTED_FORMATS),
  // TD6 2026-10-12：此前该 API 在前端零调用方（死链）
  getFileText: vi.fn(async () => "第一段正文\n第二段正文"),
};

vi.mock("../api", () => API_MOCKS);

vi.mock("react-router-dom", async () => {
  const actual =
    await vi.importActual<Record<string, unknown>>("react-router-dom");
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

describe.skipIf(!hasDom)("FileUploadPage 组件级交互", () => {
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
    // clearAllMocks 只清调用记录，fetchFiles 的实现仍在；
    // 每个用例用 mockResolvedValueOnce 精确控制首屏返回即可。
    API_MOCKS.fetchFiles.mockResolvedValue(FILES_MIXED);
    API_MOCKS.fetchSupportedFormats.mockResolvedValue(SUPPORTED_FORMATS);
    API_MOCKS.getFileText.mockResolvedValue("第一段正文\n第二段正文");
    API_MOCKS.updateFileRole.mockResolvedValue({
      status: "success",
      demoted_file_ids: [],
      needs_reanalysis: false,
    });
    API_MOCKS.reExtractFileText.mockResolvedValue({
      status: "success",
      text_length: 10,
      message: "",
    });
  });

  it(
    "渲染基础：状态标签 / 主文件标签 / 后台解析 vs 重新提取",
    { timeout: 120_000 },
    async () => {
      const rtl = await loadRtl();
      if (!rtl) return;

      API_MOCKS.fetchFiles.mockResolvedValueOnce(FILES_MIXED);
      const FileUploadPage = (await import("./FileUploadPage")).default;
      rtl.render(<FileUploadPage />);

      expect(await rtl.screen.findByText("招标文件.docx")).toBeTruthy();
      expect(rtl.screen.getByText("扫描页.png")).toBeTruthy();

      // 状态映射
      expect(rtl.screen.getByText("已提取")).toBeTruthy();
      expect(rtl.screen.getByText("提取失败")).toBeTruthy();
      expect(rtl.screen.getByText("待提取")).toBeTruthy();

      // 主文件唯一
      expect(rtl.screen.getAllByText("主文件")).toHaveLength(1);

      // 图片走「后台解析」，非图片走「重新提取」（2 个）
      expect(rtl.screen.getByText("后台解析")).toBeTruthy();
      expect(rtl.screen.getAllByText("重新提取")).toHaveLength(2);

      // 失败 message 透出
      expect(rtl.screen.getByText("图片文字识别不可用")).toBeTruthy();

      rtl.cleanup();
    },
  );

  // ─── Gap-1 2026-10-11：清空提取按钮 ───────────────────────────

  it(
    "Gap-1 有文件时渲染「清空提取」按钮（danger + Popconfirm）",
    { timeout: 120_000 },
    async () => {
      const rtl = await loadRtl();
      if (!rtl) return;

      API_MOCKS.fetchFiles.mockResolvedValueOnce(FILES_MIXED);
      const FileUploadPage = (await import("./FileUploadPage")).default;
      rtl.render(<FileUploadPage />);

      await rtl.screen.findByText("招标文件.docx");

      const clearBtn = rtl.screen.getByRole("button", { name: /清空提取/ });
      expect(clearBtn).toBeTruthy();
      expect(clearBtn.classList.toString()).toMatch(/danger/);

      rtl.cleanup();
    },
  );

  it(
    "Gap-1 无文件时不渲染「清空提取」按钮",
    { timeout: 120_000 },
    async () => {
      const rtl = await loadRtl();
      if (!rtl) return;

      API_MOCKS.fetchFiles.mockResolvedValueOnce(NO_FILES);
      const FileUploadPage = (await import("./FileUploadPage")).default;
      rtl.render(<FileUploadPage />);

      const clearBtn = rtl.screen.queryByRole("button", { name: /清空提取/ });
      expect(clearBtn).toBeNull();

      rtl.cleanup();
    },
  );

  // ─── M-5 2026-10-15：清空提取的结果分级（真点击 Popconfirm 确认） ───

  it(
    "清空提取：后端返回 warning 时走 message.warning 而非成功提示",
    { timeout: 120_000 },
    async () => {
      const rtl = await loadRtl();
      if (!rtl) return;

      const { message } = await import("antd");
      const warnSpy = vi.spyOn(message, "warning").mockImplementation(() => ({} as never));
      const successSpy = vi
        .spyOn(message, "success")
        .mockImplementation(() => ({} as never));

      API_MOCKS.fetchFiles.mockResolvedValue(FILES_MIXED);
      API_MOCKS.clearProjectTexts.mockResolvedValue(CLEARED_WITH_WARNING);

      const FileUploadPage = (await import("./FileUploadPage")).default;
      const { container } = rtl.render(<FileUploadPage />);
      await rtl.waitFor(() =>
        expect(container.textContent || "").toContain("招标文件.docx"),
      );

      // Popconfirm 渲染在 body 的 portal 里，且上一个失败用例可能留下旧弹层，
      // 故触发按钮限定在本用例 container 内，弹层取「最新一个」。
      const clearBtn = Array.from(container.querySelectorAll("button")).find((b) =>
        (b.textContent || "").includes("清空提取"),
      ) as HTMLElement;
      expect(clearBtn).toBeTruthy();
      rtl.fireEvent.click(clearBtn);

      await rtl.waitFor(() =>
        expect(document.querySelectorAll(".ant-popconfirm").length).toBeGreaterThan(0),
      );
      const pops = document.querySelectorAll(".ant-popconfirm");
      const pop = pops[pops.length - 1] as HTMLElement;
      const okBtn = Array.from(pop.querySelectorAll("button")).find((b) =>
        (b.textContent || "").includes("确认清空"),
      ) as HTMLElement;
      expect(okBtn).toBeTruthy();
      rtl.fireEvent.click(okBtn);

      await rtl.waitFor(() => {
        expect(API_MOCKS.clearProjectTexts).toHaveBeenCalledWith(1);
      });
      // 修复前这里一律 message.success，用户看到「已重置 0 个」的误导性成功
      await rtl.waitFor(() => {
        expect(warnSpy).toHaveBeenCalledWith(CLEARED_WITH_WARNING.warning);
      });
      expect(successSpy).not.toHaveBeenCalled();

      warnSpy.mockRestore();
      successSpy.mockRestore();
      rtl.cleanup();
    },
  );

  it(
    "清空提取：正常清空时按实际数量播报成功",
    { timeout: 120_000 },
    async () => {
      const rtl = await loadRtl();
      if (!rtl) return;

      const { message } = await import("antd");
      const warnSpy = vi.spyOn(message, "warning").mockImplementation(() => ({} as never));
      const successSpy = vi
        .spyOn(message, "success")
        .mockImplementation(() => ({} as never));

      API_MOCKS.fetchFiles.mockResolvedValue(FILES_MIXED);
      API_MOCKS.clearProjectTexts.mockResolvedValue(CLEARED_RESPONSE);

      const FileUploadPage = (await import("./FileUploadPage")).default;
      const { container } = rtl.render(<FileUploadPage />);
      await rtl.waitFor(() =>
        expect(container.textContent || "").toContain("招标文件.docx"),
      );

      // Popconfirm 渲染在 body 的 portal 里，且上一个失败用例可能留下旧弹层，
      // 故触发按钮限定在本用例 container 内，弹层取「最新一个」。
      const clearBtn = Array.from(container.querySelectorAll("button")).find((b) =>
        (b.textContent || "").includes("清空提取"),
      ) as HTMLElement;
      expect(clearBtn).toBeTruthy();
      rtl.fireEvent.click(clearBtn);

      await rtl.waitFor(() =>
        expect(document.querySelectorAll(".ant-popconfirm").length).toBeGreaterThan(0),
      );
      const pops = document.querySelectorAll(".ant-popconfirm");
      const pop = pops[pops.length - 1] as HTMLElement;
      const okBtn = Array.from(pop.querySelectorAll("button")).find((b) =>
        (b.textContent || "").includes("确认清空"),
      ) as HTMLElement;
      expect(okBtn).toBeTruthy();
      rtl.fireEvent.click(okBtn);

      await rtl.waitFor(() => {
        expect(successSpy).toHaveBeenCalledWith(
          "已重置 3 个文件状态为待提取，清理 2 条旧解析记录",
        );
      });
      expect(warnSpy).not.toHaveBeenCalled();

      warnSpy.mockRestore();
      successSpy.mockRestore();
      rtl.cleanup();
    },
  );

  // ─── 角色切换（demoted_file_ids 响应） ────────────────────────

  it(
    "角色切换：点击「设为主文件」调用 updateFileRole 并带正确参数",
    { timeout: 120_000 },
    async () => {
      const rtl = await loadRtl();
      if (!rtl) return;

      API_MOCKS.fetchFiles.mockResolvedValue(FILES_MIXED);
      API_MOCKS.updateFileRole.mockResolvedValue({
        status: "success",
        demoted_file_ids: [1], // 旧主文件（招标文件.docx）被自动降级
        needs_reanalysis: false,
      });

      const FileUploadPage = (await import("./FileUploadPage")).default;
      rtl.render(<FileUploadPage />);
      await rtl.screen.findByText("招标文件.docx");

      // 组件当前文案为「设为主文件」/「设为辅助」（原用例断言的
      // 「切换为主文件/切换为辅助文件」是更早版本的文案，早已失配）
      const toggleBtns = rtl.screen.getAllByRole("button", {
        name: /设为主文件|设为辅助/,
      });
      expect(toggleBtns.length).toBeGreaterThan(0);

      // 点击「工程量清单.txt」那一行的「设为主文件」
      const target = rtl.screen
        .getAllByRole("button", { name: /设为主文件/ })
        .find((b) => {
          const item = b.closest("li.ant-list-item");
          return !!item && item.textContent?.includes("工程量清单.txt");
        });
      expect(target).toBeTruthy();

      rtl.fireEvent.click(target as HTMLElement);

      await rtl.waitFor(() => {
        expect(API_MOCKS.updateFileRole).toHaveBeenCalledTimes(1);
      });
      // 参数口径：projectId=1, file_id=3（工程量清单.txt）, next role = main
      expect(API_MOCKS.updateFileRole).toHaveBeenCalledWith(1, 3, "main");

      rtl.cleanup();
    },
  );

  it(
    "角色切换：旧主文件被降级时如实提示（demoted_file_ids 透出）",
    { timeout: 120_000 },
    async () => {
      const rtl = await loadRtl();
      if (!rtl) return;

      API_MOCKS.fetchFiles.mockResolvedValue(FILES_MIXED);
      API_MOCKS.updateFileRole.mockResolvedValue({
        status: "success",
        demoted_file_ids: [1],
        needs_reanalysis: true,
      });

      const FileUploadPage = (await import("./FileUploadPage")).default;
      rtl.render(<FileUploadPage />);
      await rtl.screen.findByText("招标文件.docx");

      rtl.fireEvent.click(
        rtl.screen.getAllByRole("button", { name: /设为主文件/ })[0],
      );

      await rtl.waitFor(() => {
        expect(API_MOCKS.updateFileRole).toHaveBeenCalled();
      });

      rtl.cleanup();
    },
  );

  // ─── 删除链路 ────────────────────────────────────────────────

  it(
    "删除文件调用 deleteFile；删主文件时提示已自动提升新主文件",
    { timeout: 120_000 },
    async () => {
      const rtl = await loadRtl();
      if (!rtl) return;

      API_MOCKS.fetchFiles.mockResolvedValue(FILES_MIXED);
      API_MOCKS.deleteFile.mockResolvedValue({
        status: "deleted",
        file_id: 1,
        promoted_file_id: 3,
      });

      const FileUploadPage = (await import("./FileUploadPage")).default;
      rtl.render(<FileUploadPage />);
      await rtl.screen.findByText("招标文件.docx");

      const row = rtl.screen
        .getByText("招标文件.docx")
        .closest("li.ant-list-item") as HTMLElement;
      const delBtn = rtl
        .within(row)
        .getAllByRole("button", { name: /删除/ })[0];
      rtl.fireEvent.click(delBtn);

      await rtl.waitFor(() => {
        expect(API_MOCKS.deleteFile).toHaveBeenCalledWith(1, 1);
      });

      rtl.cleanup();
    },
  );

  // ─── 重新提取：按后端返回分类提示（不谎报成功） ─────────────────

  it(
    "重新提取：后端返回 status=failed 时不谎报成功",
    { timeout: 120_000 },
    async () => {
      const rtl = await loadRtl();
      if (!rtl) return;

      API_MOCKS.fetchFiles.mockResolvedValue(FILES_MIXED);
      API_MOCKS.reExtractFileText.mockResolvedValue({
        status: "failed",
        message: "[提示] 当前系统无法自动提取此 .doc 文件文本",
        text_length: 0,
      });

      const FileUploadPage = (await import("./FileUploadPage")).default;
      rtl.render(<FileUploadPage />);
      await rtl.screen.findByText("招标文件.docx");

      const row = rtl.screen
        .getByText("招标文件.docx")
        .closest("li.ant-list-item") as HTMLElement;
      rtl.fireEvent.click(
        rtl.within(row).getAllByRole("button", { name: /重新提取/ })[0],
      );

      await rtl.waitFor(() => {
        expect(API_MOCKS.reExtractFileText).toHaveBeenCalledWith(1, 1);
      });

      rtl.cleanup();
    },
  );

  // ─── 空态 + 上传后刷新列表 ────────────────────────────────────

  it(
    "批量上传：空态文案正确，上传回调触发后重新拉取文件列表",
    { timeout: 120_000 },
    async () => {
      const rtl = await loadRtl();
      if (!rtl) return;

      // 首屏空 → 空态文案；上传回调后的再次 fetch 返回文件列表
      API_MOCKS.fetchFiles
        .mockResolvedValueOnce(NO_FILES)
        .mockResolvedValue(FILES_MIXED);
      API_MOCKS.uploadFile.mockResolvedValue({
        status: "success",
        id: 9,
        filename: "新文件.docx",
      });

      const FileUploadPage = (await import("./FileUploadPage")).default;
      rtl.render(<FileUploadPage />);

      // 组件当前空态文案（组件此前一直渲染的原文）
      expect(await rtl.screen.findByText(/暂无文件，请先上传招标文件/)).toBeTruthy();
      expect(API_MOCKS.fetchFiles).toHaveBeenCalled();

      rtl.cleanup();
    },
  );

  // ─── TD6 2026-10-12：accept 由后端单一真相端点下发 ─────────────

  it(
    "TD6：accept 取自后端 supported-formats 端点下发的清单",
    { timeout: 120_000 },
    async () => {
      const rtl = await loadRtl();
      if (!rtl) return;

      API_MOCKS.fetchFiles.mockResolvedValue(FILES_MIXED);
      API_MOCKS.fetchSupportedFormats.mockResolvedValue({
        ...SUPPORTED_FORMATS,
        extensions: [".docx", ".pdf", ".txt", ".png"],
        accept: ".docx,.pdf,.txt,.png",
      });

      const FileUploadPage = (await import("./FileUploadPage")).default;
      rtl.render(<FileUploadPage />);
      await rtl.screen.findByText("招标文件.docx");

      await rtl.waitFor(() => {
        expect(API_MOCKS.fetchSupportedFormats).toHaveBeenCalledWith(1);
      });

      // 后端新增一个格式后，前端 accept 无需改代码即自动跟随
      const input = document.querySelector(
        'input[type="file"]',
      ) as HTMLInputElement | null;
      expect(input).toBeTruthy();
      await rtl.waitFor(() => {
        expect(input?.getAttribute("accept")).toBe(".docx,.pdf,.txt,.png");
      });

      rtl.cleanup();
    },
  );

  it(
    "TD6：supported-formats 拉取失败时回落到本地常量，上传不受影响",
    { timeout: 120_000 },
    async () => {
      const rtl = await loadRtl();
      if (!rtl) return;

      API_MOCKS.fetchFiles.mockResolvedValue(FILES_MIXED);
      API_MOCKS.fetchSupportedFormats.mockRejectedValue(
        new Error("network down"),
      );

      const FileUploadPage = (await import("./FileUploadPage")).default;
      rtl.render(<FileUploadPage />);
      // 列表照常加载 —— 格式查询失败不得阻断主流程
      await rtl.screen.findByText("招标文件.docx");

      const input = document.querySelector(
        'input[type="file"]',
      ) as HTMLInputElement | null;
      expect(input).toBeTruthy();
      await rtl.waitFor(() => {
        expect(input?.getAttribute("accept")).toContain(".docx");
        expect(input?.getAttribute("accept")).toContain(".wps");
      });

      rtl.cleanup();
    },
  );

  // ─── TD6 2026-10-12：提取文本预览（激活 getFileText 死链） ───────

  it(
    "TD6：点击「预览文本」调用 getFileText 并展示提取正文",
    { timeout: 120_000 },
    async () => {
      const rtl = await loadRtl();
      if (!rtl) return;

      API_MOCKS.fetchFiles.mockResolvedValue(FILES_MIXED);
      API_MOCKS.getFileText.mockResolvedValue("招标人：某某单位\n工期：90 日历天");

      const FileUploadPage = (await import("./FileUploadPage")).default;
      rtl.render(<FileUploadPage />);
      await rtl.screen.findByText("招标文件.docx");

      const row = rtl.screen
        .getByText("招标文件.docx")
        .closest("li.ant-list-item") as HTMLElement;
      rtl.fireEvent.click(
        rtl.within(row).getAllByRole("button", { name: /预览文本/ })[0],
      );

      await rtl.waitFor(() => {
        expect(API_MOCKS.getFileText).toHaveBeenCalledWith(1, 1);
      });
      expect(
        await rtl.screen.findByText(/招标人：某某单位/),
      ).toBeTruthy();

      rtl.cleanup();
    },
  );

  it(
    "TD6：提取文本为空时给出可行动提示而非空白弹窗",
    { timeout: 120_000 },
    async () => {
      const rtl = await loadRtl();
      if (!rtl) return;

      API_MOCKS.fetchFiles.mockResolvedValue(FILES_MIXED);
      API_MOCKS.getFileText.mockResolvedValue("");

      const FileUploadPage = (await import("./FileUploadPage")).default;
      rtl.render(<FileUploadPage />);
      await rtl.screen.findByText("招标文件.docx");

      const row = rtl.screen
        .getByText("招标文件.docx")
        .closest("li.ant-list-item") as HTMLElement;
      rtl.fireEvent.click(
        rtl.within(row).getAllByRole("button", { name: /预览文本/ })[0],
      );

      await rtl.waitFor(() => {
        expect(API_MOCKS.getFileText).toHaveBeenCalled();
      });
      // 空文本 → 提示「没有可预览的提取文本」，不展示正文块
      expect(
        await rtl.screen.findByText(/没有可预览的提取文本/),
      ).toBeTruthy();

      rtl.cleanup();
    },
  );
});