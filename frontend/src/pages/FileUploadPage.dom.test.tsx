// @vitest-environment jsdom
/**
 * 上传解析模块 —— 组件级（DOM）交互测试（遗留修复 2026-10-05/06 L5）
 *
 * 此前前端只有纯函数测试（utils.import.test.ts / utils.upload.test.ts），
 * 页面渲染与「状态 → 文案/可操作项」的映射完全没有被覆盖：
 *   - 后端 status（success/failed/pending/processing）到中文标签的映射
 *   - 主/辅文件标签
 *   - 图片文件走「后台解析」、其它格式走「重新提取」（遗留 L2）
 *   - 已提取计数提示
 *
 * 运行条件：需要 jsdom + @testing-library/react（已加入 devDependencies，
 * 执行 `pnpm install` 后 `pnpm test` 即可）。缺少 DOM 环境时整体 skip，
 * 不会阻塞纯函数测试套件。
 */
import { describe, it, expect, vi, beforeAll } from "vitest";

const hasDom = typeof window !== "undefined" && typeof document !== "undefined";

const FILES = [
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

vi.mock("../api", () => ({
  uploadFile: vi.fn(),
  fetchFiles: vi.fn(async () => FILES),
  deleteFile: vi.fn(async () => ({ status: "deleted", file_id: 1 })),
  reExtractFileText: vi.fn(async () => ({ status: "success", text_length: 10 })),
  updateFileRole: vi.fn(async () => ({ status: "success", demoted_file_ids: [] })),
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
}));

// 页面依赖 useParams/useNavigate，这里直接给出固定路由参数，免去 Router 包裹
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

describe.skipIf(!hasDom)("FileUploadPage 组件级渲染", () => {
  beforeAll(() => {
    // jsdom 不实现 matchMedia，antd 响应式组件会用到
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

  // BUG-FIX 2026-10-06：首跑需要现场转译整个 antd 组件树，远超 vitest
  // 默认 5s 超时（实测首跑 ~60s，缓存后 ~3s）。为渲染用例显式放宽超时。
  it("渲染文件列表：状态标签 / 主文件标签 / 后台解析与重新提取按钮", { timeout: 120_000 }, async () => {
    const rtl = await loadRtl();
    if (!rtl) {
      // 依赖未安装（未执行 pnpm install）时跳过，不阻塞其它测试
      return;
    }

    const FileUploadPage = (await import("./FileUploadPage")).default;
    rtl.render(<FileUploadPage />);

    // 等列表加载完成（loading → 渲染）
    expect(await rtl.screen.findByText("招标文件.docx")).toBeTruthy();
    expect(rtl.screen.getByText("扫描页.png")).toBeTruthy();
    expect(rtl.screen.getByText("工程量清单.txt")).toBeTruthy();

    // 状态映射：success→已提取 / failed→提取失败 / pending→待提取
    expect(rtl.screen.getByText("已提取")).toBeTruthy();
    expect(rtl.screen.getByText("提取失败")).toBeTruthy();
    expect(rtl.screen.getByText("待提取")).toBeTruthy();

    // 主文件标签唯一（main 全项目互斥）
    expect(rtl.screen.getAllByText("主文件")).toHaveLength(1);

    // 已提取计数提示（3 个文件中 1 个完成）
    expect(
      rtl.screen.getByText("已有 1 个文件完成文本提取，可以进入下一步"),
    ).toBeTruthy();

    // 遗留修复 L2：图片走「后台解析」，其余格式走「重新提取」（2 个非图片文件）
    expect(rtl.screen.getByText("后台解析")).toBeTruthy();
    expect(rtl.screen.getAllByText("重新提取")).toHaveLength(2);

    // 失败文件的 message 必须透出（否则用户只看到「提取失败」却不知原因）
    expect(rtl.screen.getByText("图片文字识别不可用")).toBeTruthy();

    rtl.cleanup();
  });
});
