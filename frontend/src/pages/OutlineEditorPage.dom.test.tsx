// @vitest-environment jsdom
/**
 * 生成大纲模块 —— 2026-10-12 后续加固 · 组件级交互测试。
 *
 * 覆盖此前零组件测试、导致多类缺陷长期潜藏的关键路径：
 *   - handleEditTitle：改名的请求参数与服务端规范标题回写（双重编号防线）
 *   - SSE onmessage 分发：generating / completed / error / review_needed
 *   - 重连幂等：seq 去重、终态只应用一次、进度不回退
 *   - V5.1 审核弹窗：本地编辑 → v51Confirm 提交的目录树形状
 *
 * Mock 口径沿用仓库既有惯例（见 WordCountPage.dom.test.tsx / AIConfigPage.dom.test.tsx）：
 * `vi.mock("../api", …)` 是**整体替换**，页面 import 的每个符号都必须列出，
 * 否则该符号变成 undefined、页面在首屏 load() 就抛错。
 */
import { beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

const hasDom = typeof window !== "undefined" && typeof document !== "undefined";

// ---------------------------------------------------------------- API mocks

const getProjectMock = vi.fn(async () => ({
  id: 1,
  name: "测试项目",
  file_name: "招标文件.docx",
}));
const fetchSectionsMock = vi.fn(async () => [
  {
    id: 10, title: "施工部署", level: 1, sort_order: 1, section_type: "text",
    scoring_point_id: null, status: "pending", children: [
      {
        id: 11, title: "资源配置", level: 2, sort_order: 1, section_type: "text",
        scoring_point_id: null, status: "pending", children: [
          {
            id: 12, title: "劳动力配置", level: 3, sort_order: 1,
            section_type: "text", scoring_point_id: null, status: "pending",
            children: [],
          },
        ],
      },
    ],
  },
]);
const updateSectionMock = vi.fn(async (_p: number, _s: number, _d: unknown) => ({
  id: 12, project_id: 1, parent_id: 11, title: "施工部署", level: 3,
  sort_order: 1, section_type: "text", scoring_point_id: null, status: "pending",
  content: "", target_words: null,
}));
const deleteSectionMock = vi.fn(async () => {});
const createSectionMock = vi.fn(async () => ({
  id: 99, project_id: 1, parent_id: 10, title: "新章节", level: 2,
  sort_order: 2, section_type: "text", scoring_point_id: null, status: "pending",
  content: "", target_words: null,
}));
const moveSectionMock = vi.fn(async () => fetchSectionsMock());
const v51ConfirmMock = vi.fn(async () => ({ ok: true }));

/** 可被测试驱动的 SSE 事件源替身 */
let sseHandlers: { onmessage: ((e: { data: string }) => void) | null } = {
  onmessage: null,
};
const closeSpy = vi.fn();
const markTerminalSpy = vi.fn();
const detachSpy = vi.fn();

vi.mock("../api", () => ({
  getProject: getProjectMock,
  fetchSections: fetchSectionsMock,
  updateSection: updateSectionMock,
  deleteSection: deleteSectionMock,
  createSection: createSectionMock,
  moveSection: moveSectionMock,
  reviewOutline: vi.fn(async () => ({ passed: true, issues: [] })),
  reviewOutlineWithAutofix: vi.fn(async () => ({ repaired: false, passed: true, issues: [] })),
  clearSections: vi.fn(async () => {}),
  stopGeneration: vi.fn(async () => {}),
  v51Confirm: v51ConfirmMock,
  fetchPendingV51Task: vi.fn(async () => ({ has_pending: false })),
  clearV51PendingTask: vi.fn(async () => {}),
  getWordControlSettings: vi.fn(async () => ({
    min_leaf_count: 3, max_leaf_count: 60, total_target_words: 60000,
    enabled: true, tier_mode: "standard",
  })),
  createOutlineEventSource: vi.fn(() => {
    sseHandlers = { onmessage: null };
    return {
      set onmessage(fn: ((e: { data: string }) => void) | null) { sseHandlers.onmessage = fn; },
      get onmessage() { return sseHandlers.onmessage; },
      set onerror(_fn: unknown) { /* 测试不驱动错误分支 */ },
      get onerror() { return null; },
      close: closeSpy,
      detach: detachSpy,
      markTerminalEvent: markTerminalSpy,
      promise: Promise.resolve(),
    };
  }),
  getStoredOutlineMode: () => "auto" as const,
  storeOutlineMode: vi.fn(),
  // RecoveryBanner 依赖它；页面本身不 import，但同一次 vi.mock 是模块级的
  fetchTaskRecoveryStatus: vi.fn(async () => ({
    recovery_options: [],
    running: false,
  })),
}));

vi.mock("@/services/taskStore", () => {
  // 页面用的是**具名**导出 `taskStore`（taskStore.ts 末尾 `export const taskStore`）
  const inst = {
    startTask: vi.fn(),
    updateTask: vi.fn(),
    completeTask: vi.fn(),
    getTask: vi.fn(() => undefined),
  };
  return { taskStore: inst, default: inst };
});

vi.mock("react-router-dom", async () => {
  const actual = await vi.importActual<Record<string, unknown>>("react-router-dom");
  return { ...actual, useParams: () => ({ id: "1" }), useNavigate: () => vi.fn() };
});

async function loadRtl() {
  try { return await import("@testing-library/react"); } catch { return null; }
}

const emit = (payload: Record<string, unknown>) =>
  sseHandlers.onmessage?.({ data: JSON.stringify(payload) });

// ---------------------------------------------------------------- tests

describe.skipIf(!hasDom)("OutlineEditorPage 组件级交互（2026-10-12 加固）", () => {
  let rtl: Awaited<ReturnType<typeof loadRtl>>;
  let OutlineEditorPage: React.ComponentType<Record<string, unknown>>;
  // 项目 tsconfig target=ES2020（无 Array.prototype.at），用下标取末位调用记录
  const lastCall = <T extends unknown[]>(calls: T[]): T | undefined =>
    calls.length > 0 ? calls[calls.length - 1] : undefined;

  // 本文件要动态 import 整个页面（1500+ 行、18 个图标、14 个 antd 组件），
  // 冷启动本身就慢；全量并行跑时 vitest 默认 10s 的 hook 超时会被击穿，
  // 表现为「Hook timed out in 10000ms」—— 与被测代码无关的假失败。
  // 与既有 dom 测试（如 FactBackflowTab）的处理一致：显式放宽。
  beforeAll(async () => {
    rtl = await loadRtl();
    if (!rtl) return;
    if (typeof window.matchMedia !== "function") {
      Object.defineProperty(window, "matchMedia", {
        writable: true,
        value: (q: string) => ({
          matches: false, media: q, onchange: null,
          addListener: () => {}, removeListener: () => {},
          addEventListener: () => {}, removeEventListener: () => {},
          dispatchEvent: () => false,
        }),
      });
    }
    ({ default: OutlineEditorPage } = await import("./OutlineEditorPage"));
  }, 60_000);

  beforeEach(() => {
    vi.clearAllMocks();
    sseHandlers = { onmessage: null };
    getProjectMock.mockResolvedValue({ id: 1, name: "测试项目", file_name: "x.docx" });
    fetchSectionsMock.mockResolvedValue([
      {
        id: 10, title: "施工部署", level: 1, sort_order: 1, section_type: "text",
        scoring_point_id: null, status: "pending", children: [
          {
            id: 11, title: "资源配置", level: 2, sort_order: 1, section_type: "text",
            scoring_point_id: null, status: "pending", children: [
              {
                id: 12, title: "劳动力配置", level: 3, sort_order: 1,
                section_type: "text", scoring_point_id: null, status: "pending",
                children: [],
              },
            ],
          },
        ],
      },
    ]);
    updateSectionMock.mockImplementation(async (_p: number, s: number) => ({
      id: s, project_id: 1, parent_id: 11, title: "服务端规范标题", level: 3,
      sort_order: 1, section_type: "text", scoring_point_id: null, status: "pending",
      content: "", target_words: null,
    }));
  });

  const renderPage = async () => {
    if (!rtl || !OutlineEditorPage) return;
    const view = rtl.render(<OutlineEditorPage />);
    // 等首屏 loadTree（getProject + fetchSections）落地
    await rtl.waitFor(() => expect(fetchSectionsMock).toHaveBeenCalled());
    return view;
  };

  // ===============================================================
  // 改名：服务端规范标题回写（FE-2）
  // ===============================================================

  it("改名以服务端返回的规范标题回写本地树，而不是用用户原始输入", async () => {
    if (!rtl) return;
    const { Modal } = await import("antd");
    const confirmSpy = vi.spyOn(Modal, "confirm");
    await renderPage();

    // antd Dropdown 默认 trigger=hover，jsdom 里必须用 mouseOver/mouseEnter 才会展开。
    // 触发按钮既可能暴露为 title 也可能被 antd 映射成 aria-label，两种都兼容。
    const findTrigger = (from: HTMLElement | null): HTMLElement | null => {
      let r: HTMLElement | null = from;
      while (r) {
        const btn = r.querySelector(
          "button[title='更多操作'], button[aria-label='更多操作']",
        );
        if (btn) return btn as HTMLElement;
        r = r.parentElement;
      }
      return null;
    };
    const row = await rtl.waitFor(() => {
      const titleNode = Array.from(document.querySelectorAll("*")).find(
        (el) => el.children.length === 0 && el.textContent?.trim() === "劳动力配置",
      ) as HTMLElement | undefined;
      expect(titleNode).toBeTruthy();
      const t = findTrigger(titleNode as HTMLElement);
      expect(t).toBeTruthy();
      return t as HTMLElement;
    });
    const trigger = row;
    rtl.fireEvent.mouseOver(trigger);
    rtl.fireEvent.mouseEnter(trigger);
    rtl.fireEvent.click(trigger);

    const menuItem = await rtl.waitFor(() => {
      const item = Array.from(
        document.querySelectorAll('li[role="menuitem"], li'),
      ).find((el) => el.textContent?.trim() === "编辑标题") as HTMLElement | undefined;
      expect(item).toBeTruthy();
      return item as HTMLElement;
    });
    rtl.fireEvent.click(menuItem);

    await rtl.waitFor(() => expect(confirmSpy).toHaveBeenCalled());

    // Modal.confirm 的 content 渲染了带 id 的输入框；页面用
    // document.getElementById(`edit-title-${node.id}`) 回读，模拟真实输入。
    const args = (lastCall(confirmSpy.mock.calls) as [Record<string, unknown>])[0] as {
      content: React.ReactElement;
      onOk?: () => void | Promise<void>;
    };
    rtl.render(args.content);
    const inputId = "edit-title-12";
    const input = document.getElementById(inputId) as HTMLInputElement;
    expect(input).toBeTruthy();
    const setter = Object.getOwnPropertyDescriptor(
      window.HTMLInputElement.prototype, "value",
    )!.set!;
    setter.call(input, "3.1.2 劳动力配置");
    input.dispatchEvent(new Event("input", { bubbles: true }));
    await args.onOk?.();

    expect(updateSectionMock).toHaveBeenCalledWith(1, 12, { title: "3.1.2 劳动力配置" });
    // 服务端剥掉编号前缀后返回规范标题，本地树必须跟随服务端而非用户原始输入
    await rtl.waitFor(() => {
      const found = Array.from(document.querySelectorAll("*")).some(
        (el) => el.children.length === 0 && el.textContent?.trim() === "服务端规范标题",
      );
      expect(found).toBe(true);
    });
    // 且绝不能再出现双重编号形态
    expect(document.body.textContent).not.toContain("3.1.2 劳动力配置");
    confirmSpy.mockRestore();
  });

  // ===============================================================
  // SSE 分发 + 重连幂等（FE-4）
  // ===============================================================

  it("generating 事件驱动进度条与全局任务条", async () => {
    if (!rtl) return;
    await renderPage();
    const { taskStore } = await import("@/services/taskStore");
    const start = (document.body.textContent || "").includes("生成");
    expect(start).toBe(true);

    // 触发一次生成以拿到 SSE handler
    const genBtn = Array.from(document.querySelectorAll("button")).find((b) =>
      (b.textContent || "").includes("生成目录"),
    ) as HTMLElement | undefined;
    expect(genBtn).toBeTruthy();
    genBtn!.click();
    await rtl.waitFor(() => expect(sseHandlers.onmessage).toBeTruthy());

    emit({ status: "connected" });
    emit({ status: "generating", phase: "outline_phase", outline_step: 5, message: "生成三级" });
    await rtl.waitFor(() => {
      expect((taskStore.updateTask as ReturnType<typeof vi.fn>)).toHaveBeenCalled();
    });
    const _lc = lastCall((taskStore.updateTask as ReturnType<typeof vi.fn>).mock.calls) as [string, { progress?: number }];
    const lastCallData = _lc[1];
    expect(lastCallData.progress).toBe(50); // step5/8 → (5-1)/8 = 50%
  });

  it("重连重放的旧进度事件不得让进度倒退", async () => {
    if (!rtl) return;
    await renderPage();
    const genBtn = Array.from(document.querySelectorAll("button")).find((b) =>
      (b.textContent || "").includes("生成目录"),
    ) as HTMLElement | undefined;
    genBtn!.click();
    await rtl.waitFor(() => expect(sseHandlers.onmessage).toBeTruthy());

    emit({ status: "connected" });
    emit({ status: "generating", phase: "outline_phase", outline_step: 6, seq: 10 });
    await rtl.waitFor(() => {
      expect(document.body.textContent).toContain("50");
    });
    // 重放一条更早的进度事件（服务端 last_event）
    emit({ status: "generating", phase: "outline_phase", outline_step: 3, seq: 4 });
    // 不应回退到 25%
    await new Promise((r) => setTimeout(r, 50));
    const { taskStore } = await import("@/services/taskStore");
    const calls = (taskStore.updateTask as ReturnType<typeof vi.fn>).mock.calls;
    const last = (lastCall(calls) as [string, { progress?: number }])[1];
    expect(last.progress).toBe(63); // step6/8 → 5/8 = 63%，未倒退
  });

  it("seq 相同的重放事件被直接丢弃", async () => {
    if (!rtl) return;
    await renderPage();
    const { message } = await import("antd");
    const errSpy = vi.spyOn(message, "error");
    const genBtn = Array.from(document.querySelectorAll("button")).find((b) =>
      (b.textContent || "").includes("生成目录"),
    ) as HTMLElement | undefined;
    genBtn!.click();
    await rtl.waitFor(() => expect(sseHandlers.onmessage).toBeTruthy());

    emit({ status: "connected", seq: 1 });
    emit({ status: "error", message: "首次失败", seq: 2 });
    await rtl.waitFor(() => expect(errSpy).toHaveBeenCalledTimes(1));

    // 重放同一条终态：必须被忽略（否则双弹 toast）
    emit({ status: "error", message: "首次失败", seq: 2 });
    await new Promise((r) => setTimeout(r, 50));
    expect(errSpy).toHaveBeenCalledTimes(1);
    errSpy.mockRestore();
  });

  it("终态事件只应用一次（不重复 close / completeTask）", async () => {
    if (!rtl) return;
    await renderPage();
    const { taskStore } = await import("@/services/taskStore");
    const genBtn = Array.from(document.querySelectorAll("button")).find((b) =>
      (b.textContent || "").includes("生成目录"),
    ) as HTMLElement | undefined;
    genBtn!.click();
    await rtl.waitFor(() => expect(sseHandlers.onmessage).toBeTruthy());

    emit({ status: "completed", message: "目录生成完成", seq: 99 });
    await rtl.waitFor(() => expect(closeSpy).toHaveBeenCalled());
    expect((taskStore.completeTask as ReturnType<typeof vi.fn>)).toHaveBeenCalledTimes(1);

    emit({ status: "completed", message: "目录生成完成", seq: 99 });
    await new Promise((r) => setTimeout(r, 50));
    expect(closeSpy).toHaveBeenCalledTimes(1);
    expect((taskStore.completeTask as ReturnType<typeof vi.fn>)).toHaveBeenCalledTimes(1);
  });

  it("章节进度 completed 取单调上限（重放不倒退）", async () => {
    if (!rtl) return;
    await renderPage();
    const genBtn = Array.from(document.querySelectorAll("button")).find((b) =>
      (b.textContent || "").includes("生成目录"),
    ) as HTMLElement | undefined;
    genBtn!.click();
    await rtl.waitFor(() => expect(sseHandlers.onmessage).toBeTruthy());

    emit({
      status: "generating", phase: "children", seq: 1,
      progress: { level1: 8, completed: 7, total: 8 },
    });
    await rtl.waitFor(() => expect(document.body.textContent).toContain("章节进度"));
    expect(document.body.textContent).toContain("7 / 8");

    emit({
      status: "generating", phase: "children", seq: 2,
      progress: { level1: 8, completed: 3, total: 8 },
    });
    await new Promise((r) => setTimeout(r, 50));
    expect(document.body.textContent).toContain("7 / 8");
  });

  // ===============================================================
  // V5.1 审核弹窗（跨模块数据传递）
  // ===============================================================

  it("review_needed 弹出审核窗；改标题后确认提交编辑后的目录", async () => {
    if (!rtl) return;
    await renderPage();
    const genBtn = Array.from(document.querySelectorAll("button")).find((b) =>
      (b.textContent || "").includes("生成目录"),
    ) as HTMLElement | undefined;
    genBtn!.click();
    await rtl.waitFor(() => expect(sseHandlers.onmessage).toBeTruthy());

    emit({
      status: "review_needed",
      phase: "level1",
      message: "请审核一级目录",
      seq: 1,
      outline: [
        { id: "1", title: "施工部署", children: [
          { id: "1.1", title: "资源配置", children: [] },
        ] },
      ],
    });
    await rtl.waitFor(() => expect(document.body.textContent).toContain("请审核一级目录"));

    // 编辑一级标题
    // BUG-FIX 2026-10-13（测试健壮性 · 非本模块，顺手修）：原用例在
    // `waitFor(消息文案出现)` 之后**同步**查询 `input.ant-input`。但 antd Modal 是
    // 先渲染标题/描述、后挂载 children 的，消息文案出现并不代表输入框已挂载 ——
    // 全量并发跑（worker 抢 CPU）时这一步偶发拿到 0 个 input 而失败，单跑则稳定绿。
    // 正确做法是 waitFor **它真正要断言的东西**（可编辑输入框出现），而不是
    // waitFor 一个相关但更早出现的信号。
    const editable = await rtl.waitFor(() => {
      const found = Array.from(
        document.querySelectorAll('input.ant-input'),
      ).filter((el) => (el as HTMLInputElement).value === "施工部署");
      expect(found.length).toBeGreaterThan(0);
      return found;
    });
    const setter = Object.getOwnPropertyDescriptor(
      window.HTMLInputElement.prototype, "value",
    )!.set!;
    setter.call(editable[0], "施工部署与协调");
    editable[0].dispatchEvent(new Event("input", { bubbles: true }));

    const confirmBtn = Array.from(document.querySelectorAll("button")).find((b) =>
      (b.textContent || "").includes("确认并继续"),
    ) as HTMLElement | undefined;
    expect(confirmBtn).toBeTruthy();
    confirmBtn!.click();

    await rtl.waitFor(() => expect(v51ConfirmMock).toHaveBeenCalled());
    const body = (lastCall(v51ConfirmMock.mock.calls) as unknown as [number, Record<string, unknown>])[1] as {
      edited_outline: Array<{ title: string; children: unknown[] }>;
    };
    expect(body.edited_outline[0].title).toBe("施工部署与协调");
    // 未被编辑的二级标题必须原样保留
    expect(body.edited_outline[0].children[0]).toMatchObject({ title: "资源配置" });
  });

  it("review_needed 重放不会把已确认状态打回未确认", async () => {
    if (!rtl) return;
    await renderPage();
    const genBtn = Array.from(document.querySelectorAll("button")).find((b) =>
      (b.textContent || "").includes("生成目录"),
    ) as HTMLElement | undefined;
    genBtn!.click();
    await rtl.waitFor(() => expect(sseHandlers.onmessage).toBeTruthy());

    emit({ status: "review_auto_confirm", phase: "level1", seq: 1 });
    await rtl.waitFor(() => {
      expect(document.body.textContent).toContain("审核确认");
    });
    emit({
      status: "review_needed", phase: "level1", message: "请审核一级目录", seq: 2,
      outline: [{ id: "1", title: "施工部署", children: [] }],
    });
    await new Promise((r) => setTimeout(r, 50));
    // 弹窗可再次打开，但不得把「V5.1 审核确认」按钮的已确认态清掉
    const btn = Array.from(document.querySelectorAll("button")).find((b) =>
      (b.textContent || "").includes("审核确认"),
    ) as HTMLButtonElement | undefined;
    expect(btn).toBeTruthy();
    expect(btn!.disabled).toBe(false);
  });
});
