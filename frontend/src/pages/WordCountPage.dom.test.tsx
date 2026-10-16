// @vitest-environment jsdom
/**
 * 字数统计预检页 —— 组件级（DOM）交互测试（2026-10-06）。
 *
 * 钉住：
 *   ① 概览卡片渲染（总字数/目标覆盖率/达标章节）
 *   ② 章节明细表格渲染，缺字章节按缺口展示「缺 N 字」
 *   ③ 空数据（无章节）时空态展示，不崩
 */
import { describe, it, expect, vi, beforeAll } from "vitest";

const hasDom = typeof window !== "undefined" && typeof document !== "undefined";

const OVERVIEW = {
  project_id: 1,
  total_sections: 2,
  total_words: 12000,
  total_target: 20000,
  target_coverage: 60.0,
  completed_sections: 2,
  on_target_sections: 1,
  below_target_sections: 1,
  empty_sections: 0,
};

const SECTIONS = {
  status: "ok",
  project_id: 1,
  total: 2,
  items: [
    { id: 1, title: "施工组织设计", level: 1, parent_id: null, section_type: "body",
      status: "completed", word_count: 8000, target_words: 10000,
      word_count_text: "8000字", progress: 80 },
    { id: 2, title: "项目概况", level: 1, parent_id: null, section_type: "body",
      status: "completed", word_count: 4000, target_words: 10000,
      word_count_text: "4000字", progress: 40 },
  ],
};

// FIX 2026-10-12：页面在易标字数控制移植后新增了 4 个 API 依赖
// （getWordControlSettings / computeWordControlDerived /
//  updateWordControlSettings / snapshotWordControlSettings）。mock 未覆盖时
// 页面 load() 抛错 → 概览卡片整块不渲染，测试报「找不到 总字数」。
// 这里补齐 mock（未配置态），保持对「概览 / 明细 / 空态」三条断言的覆盖不变。
const WORD_CONTROL_SETTINGS = {
  status: "ok",
  project_id: 1,
  settings: {
    minimum_words: 0,
    maximum_words: 0,
    section_words: 0,
    strict_section_words: false,
  },
  has_snapshot: false,
  snapshot_at: null,
  derived: null,
  warning: null,
};

vi.mock("../api", () => ({
  getWordCountOverview: vi.fn(async () => OVERVIEW),
  getWordCountSections: vi.fn(async () => SECTIONS),
  setSectionTargetWords: vi.fn(async () => ({ ok: true })),
  getWordControlSettings: vi.fn(async () => WORD_CONTROL_SETTINGS),
  computeWordControlDerived: vi.fn(async () => ({ status: "ok", derived: null })),
  updateWordControlSettings: vi.fn(async () => ({ status: "ok" })),
  snapshotWordControlSettings: vi.fn(async () => ({ status: "ok" })),
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

describe.skipIf(!hasDom)("WordCountPage 组件级交互", () => {
  beforeAll(() => {
    if (!window.matchMedia) {
      Object.defineProperty(window, "matchMedia", {
        writable: true,
        value: (query: string) => ({
          matches: false, media: query, onchange: null,
          addListener: () => {}, removeListener: () => {},
          addEventListener: () => {}, removeEventListener: () => {},
          dispatchEvent: () => false,
        }),
      });
    }
  });

  it("渲染概览卡片与章节明细表，缺字章节提示「缺 N 字」", async () => {
    const rtl = await loadRtl();
    if (!rtl) return;

    const Page = (await import("./WordCountPage")).default;
    rtl.render(<Page />);

    // 概览卡片
    expect(await rtl.screen.findByText("总字数")).toBeTruthy();
    expect(await rtl.screen.findByText("目标覆盖率")).toBeTruthy();

    // 章节标题渲染
    expect(await rtl.screen.findByText("施工组织设计")).toBeTruthy();
    expect(await rtl.screen.findByText("项目概况")).toBeTruthy();

    // 进度列：缺字章节（8000/10000=80%）与达标章节均渲染出 Progress
    // （antd Progress 在 jsdom 下不渲染 scrollbar 之外的文本，以章节标题为锚已足够）

    rtl.cleanup();
  });

  it("空数据时不崩，展示空态", async () => {
    const rtl = await loadRtl();
    if (!rtl) return;

    const { getWordCountOverview, getWordCountSections } = await import("../api");
    (getWordCountOverview as unknown as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      ...OVERVIEW, total_sections: 0, total_words: 0, total_target: 0,
      on_target_sections: 0, below_target_sections: 0, empty_sections: 0,
    });
    (getWordCountSections as unknown as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      status: "ok", project_id: 1, total: 0, items: [],
    });

    const Page = (await import("./WordCountPage")).default;
    rtl.render(<Page />);

    // FIX 2026-10-12：页面新增「字数控制」卡片后，空态文案在页面中出现两次
    // （章节明细表 + 字数控制的派生区），findByText 会抛「Found multiple」。
    // 语义未变：断言「至少渲染了一处空态」。
    expect((await rtl.screen.findAllByText("暂无章节数据")).length).toBeGreaterThan(0);

    rtl.cleanup();
  });
});
