// @vitest-environment jsdom
/**
 * 上传解析模块 —— Tab 级（流程导航）组件交互测试（2026-10-08）。
 *
 * 此前 FileUploadPage 的组件级测试只覆盖「列表渲染」，没有覆盖
 * 上传完成后的**跨模块流程入口**：
 *   - 有文件时显示「下一步：要点提取」按钮（跳转 /projects/:id/review）；
 *   - 无文件时该入口不应出现（避免空项目直接进要点提取）。
 *
 * 这一流转是「上传解析 → 要点提取」模块衔接的可见入口，需被钉住，
 * 否则换路由/改条件会被静默破坏而前端无感知。
 *
 * 运行条件：jsdom + @testing-library/react（已在 devDependencies）；
 * 依赖未安装时整组 skip，不阻塞纯函数测试。
 */
import { describe, it, expect, vi, beforeAll } from "vitest";

const hasDom = typeof window !== "undefined" && typeof document !== undefined;

const FILES = [
  {
    id: 1,
    filename: "招标文件.docx",
    file_type: "docx",
    file_role: "main",
    status: "success",
    message: "",
    created_at: "2026-10-08 10:00:00",
  },
];

const navigateSpy = vi.fn();

vi.mock("../api", () => ({
  uploadFile: vi.fn(async () => ({ status: "success" })),
  fetchFiles: vi.fn(async () => FILES),
  deleteFile: vi.fn(async () => ({ status: "deleted", file_id: 1 })),
  reExtractFileText: vi.fn(async () => ({ status: "success", text_length: 10 })),
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
    filename: "扫描页.png",
    dispatched: "queued",
    dispatched_via: "celery",
  })),
}));

vi.mock("react-router-dom", async () => {
  const actual = await vi.importActual<Record<string, unknown>>("react-router-dom");
  return {
    ...actual,
    useParams: () => ({ id: "1" }),
    useNavigate: () => navigateSpy,
  };
});

async function loadRtl() {
  try {
    return await import("@testing-library/react");
  } catch {
    return null;
  }
}

describe.skipIf(!hasDom)("FileUploadPage → 要点提取 流程入口（Tab 级交互）", () => {
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

  it("有文件时展示「下一步：要点提取」并跳转到 /review", { timeout: 120_000 }, async () => {
    const rtl = await loadRtl();
    if (!rtl) return;

    const { fetchFiles } = await import("../api");
    (fetchFiles as unknown as ReturnType<typeof vi.fn>).mockResolvedValue(FILES);

    const Page = (await import("./FileUploadPage")).default;
    rtl.render(<Page />);

    const btn = await rtl.screen.findByRole("button", { name: /下一步：要点提取/ });
    rtl.fireEvent.click(btn);

    expect(navigateSpy).toHaveBeenCalledWith("/projects/1/review");
    rtl.cleanup();
  });

  it("无文件时不展示「下一步：要点提取」入口", { timeout: 120_000 }, async () => {
    const rtl = await loadRtl();
    if (!rtl) return;

    const { fetchFiles } = await import("../api");
    (fetchFiles as unknown as ReturnType<typeof vi.fn>).mockResolvedValue([]);

    const Page = (await import("./FileUploadPage")).default;
    rtl.render(<Page />);

    await rtl.waitFor(() => {
      expect(rtl.screen.queryByText(/下一步：要点提取/)).toBeNull();
    });

    // 还原默认 mock，避免影响同文件其它用例
    (fetchFiles as unknown as ReturnType<typeof vi.fn>).mockResolvedValue(FILES);
    rtl.cleanup();
  });
});
