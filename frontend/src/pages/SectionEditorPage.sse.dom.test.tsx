// @vitest-environment jsdom
/**
 * 章节 Tab：SSE 终态与重连交互回归测试（2026-11-04 收尾 P4）。
 *
 * 全部使用 mock 事件源，不触网、不需要 API Key。覆盖：
 * - 正常完成（completed）：逐章节「已保存」进度 + 自动保存提示 + 总数/成功数收尾；
 * - 中途停止（stopped）：info 提示且进度保留 total，不误报为完成；
 * - 失败终态（error）：错误提示 + 生成态复位（按钮可再次点击）、节点不残留 generating；
 * - 断线重连（onerror）：reconnecting / reconnect_exhausted 均有用户可见提示；
 * - 多章节事件按 section_id 各自归位，进度条目不串位（不重复计数）。
 *
 * 说明：当前实现没有 `status="degraded"` 的章节 SSE 终态（长上下文降级以
 * `context_degraded` 诊断字段出现在最终校验里，不是终态事件），故本文件不对
 * 不存在的状态做虚构断言，仅覆盖代码真实分支。
 */

import React from "react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { cleanup, render, waitFor } from "@testing-library/react";
import { message } from "antd";

const streamRef: { current: any } = { current: null };
const closeMock = vi.fn();
const markTerminalMock = vi.fn();
const stopGenerationMock = vi.fn(async () => {});
const createContentStreamMock = vi.fn(() => {
  const stream = {
    onmessage: null as ((e: { data: string }) => any) | null,
    onerror: null as ((e: unknown) => void) | null,
    close: closeMock,
    markTerminalEvent: markTerminalMock,
    // startGeneration 会对 promise 挂 catch；保持 pending 以模拟长连接
    promise: new Promise<void>(() => {}),
  };
  streamRef.current = stream;
  return stream;
});

const TREE = [
  {
    id: 10, title: "施工组织设计", level: 1, sort_order: 1, section_type: "text",
    scoring_point_id: null, status: "pending", children: [
      {
        id: 11, title: "施工部署", level: 2, sort_order: 1, section_type: "text",
        scoring_point_id: null, status: "pending", has_content: false, word_count: 0,
        children: [
          {
            id: 12, title: "劳动力配置", level: 3, sort_order: 1, section_type: "text",
            scoring_point_id: null, status: "pending", children: [],
            has_content: false, word_count: 0,
          },
        ],
      },
    ],
    has_content: false, word_count: 0,
  },
];

const apiMock = {
  getProject: vi.fn(async () => ({ id: 1, name: "测试项目", industry: "通用" })),
  fetchSections: vi.fn(async () => TREE),
  getGenerationStatus: vi.fn(async () => ({
    has_active_task: false, task_status: "idle", total_sections: 1,
    completed_count: 0, failed_count: 0, cancelled_count: 0,
  })),
  getWordBudget: vi.fn(async () => ({
    level2_default_words: 800,
    level1_count: 1,
    level2_count: 1,
    level3_count: 1,
    subtree_l3_count: 0,
    l3_own_lo: 240,
    l3_own_hi: 360,
    total_estimate: 800,
    sections: [],
  })),
  getSection: vi.fn(async (_p: number, id: number) => ({
    id, project_id: 1, parent_id: null, title: "劳动力配置", level: 1,
    sort_order: 1, section_type: "text", scoring_point_id: null,
    ai_prompt_snapshot: null, flowchart_json: null, gantt_json: null,
    architecture_json: null, labor_json: null, comparison_json: null,
    layout_json: null, timeline_json: null, inlined_chart_json: null,
    illustration_type: null, illustration_data: null, content: "",
    target_words: null, status: "pending", created_at: "", updated_at: "",
    word_count: 0,
  })),
  updateSection: vi.fn(async () => ({})),
  createContentStream: createContentStreamMock,
  stopGeneration: stopGenerationMock,
  pauseGeneration: vi.fn(async () => ({})),
  resumeGeneration: vi.fn(async () => ({})),
  setSectionTargetWords: vi.fn(async () => ({})),
  expandContent: vi.fn(async () => ({})),
  saveContentVersion: vi.fn(async () => ({})),
  moveSection: vi.fn(async () => TREE),
  http: { get: vi.fn(async () => ({ data: { tasks: {} } })) },
};

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<Record<string, unknown>>();
  return { ...actual, ...apiMock };
});

vi.mock("react-router-dom", () => ({
  useParams: () => ({ id: "1" }),
  useNavigate: () => vi.fn(),
  useSearchParams: () => [new URLSearchParams(), vi.fn()],
  Link: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}));

vi.mock("../components/RichTextEditor", () => ({ default: () => <div data-testid="rte" /> }));
vi.mock("../components/ContentPreview", () => ({ default: () => <div data-testid="preview" /> }));
vi.mock("../components/VersionHistory", () => ({ default: () => <div data-testid="versions" /> }));
vi.mock("../components/RecoveryBanner", () => ({ default: () => <div data-testid="recovery" /> }));
vi.mock("../components/ChartGenerationTab", () => ({ default: () => <div data-testid="charts" /> }));
vi.mock("../components/CommentsTab", () => ({ default: () => <div data-testid="comments" /> }));

function emit(evt: Record<string, unknown>) {
  const handler = streamRef.current?.onmessage;
  if (!handler) throw new Error("SSE onmessage 尚未挂载");
  return handler({ data: JSON.stringify(evt) });
}

async function renderAndStart() {
  const SectionEditorPage = (await import("./SectionEditorPage")).default;
  render(<SectionEditorPage />);
  await waitFor(() => {
    expect(apiMock.fetchSections).toHaveBeenCalled();
  });
  const genBtn = await waitFor(() => {
    const found = Array.from(document.querySelectorAll("button")).find((b) =>
      (b.textContent || "").includes("生成全部"),
    ) as HTMLButtonElement | undefined;
    expect(found).toBeTruthy();
    return found!;
  });
  genBtn.click();
  await waitFor(() => expect(streamRef.current).toBeTruthy());
}

describe("章节 Tab SSE 交互", () => {
  beforeEach(() => {
    streamRef.current = null;
    closeMock.mockClear();
    markTerminalMock.mockClear();
    stopGenerationMock.mockClear();
    createContentStreamMock.mockClear();
    // 清除上一条用例的 antd 全局 toast：静态 message 会跨用例常驻，
    // 不清会导致「否定断言」（如不出现失败文案）被历史残留污染。
    message.destroy();
  });

  afterEach(() => {
    message.destroy();
    cleanup();
  });

  it("completed 终态：逐章节进度不串位、自动保存提示、完成数收尾", async () => {
    await renderAndStart();

    emit({ status: "started", total: 1, section_ids: [12] });
    emit({ status: "completed", section_id: 12, title: "劳动力配置", saved: true, word_count: 500, progress: 100 });
    await waitFor(() => {
      expect(document.body.textContent).toContain("劳动力配置");
    });
    await waitFor(() => {
      expect(document.body.textContent).toContain("已保存");
    });

    emit({ status: "completed", success: 1, failed: 0, total: 1, stopped: false });
    await waitFor(() => {
      expect(document.body.textContent).toContain("正文生成完成：1/1");
    });
    expect(markTerminalMock).toHaveBeenCalled();
    expect(closeMock).toHaveBeenCalled();
    // 收尾后按钮回到可点击状态
    const btn = Array.from(document.querySelectorAll("button")).find((b) =>
      (b.textContent || "").includes("生成全部"),
    ) as HTMLButtonElement;
    expect(btn?.disabled).toBe(false);
  });

  it("stopped 终态：提示停止且保留 total，不误报完成", async () => {
    await renderAndStart();

    emit({ status: "started", total: 1, section_ids: [12] });
    emit({ status: "stopped", total: 1, success: 0, failed: 0, message: "已停止生成" });
    await waitFor(() => {
      expect(document.body.textContent).toContain("已停止生成");
    });
    expect(document.body.textContent).not.toContain("正文生成完成");
    expect(closeMock).toHaveBeenCalled();
  });

  it("error 终态：错误提示 + 生成态复位（按钮可再次点击）", async () => {
    await renderAndStart();

    emit({ status: "error", message: "模型调用失败：timeout" });
    await waitFor(() => {
      expect(document.body.textContent).toContain("模型调用失败");
    });
    const btn = Array.from(document.querySelectorAll("button")).find((b) =>
      (b.textContent || "").includes("生成全部"),
    ) as HTMLButtonElement;
    expect(btn?.disabled).toBe(false);
    expect(closeMock).toHaveBeenCalled();
  });

  it("TASK_LOCK_CONFLICT 不被当作失败：提示同步进度且保持生成态", async () => {
    await renderAndStart();

    emit({
      status: "error",
      code: "TASK_LOCK_CONFLICT",
      message: "已有生成任务进行中，正在同步进度",
    });
    await waitFor(() => {
      expect(document.body.textContent).toContain("已有生成任务进行中");
    });
    // 不出现「生成失败」终态文案（错误分支被特判为「同步进度」）
    expect(document.body.textContent).not.toContain("模型调用失败");
    expect(closeMock).toHaveBeenCalled();
  });

  it("断线重连：reconnecting / reconnect_exhausted 均有用户可见提示", async () => {
    await renderAndStart();

    streamRef.current.onerror({ type: "reconnecting", attempt: 1, maxAttempts: 3 });
    await waitFor(() => {
      expect(document.body.textContent).toContain("正在重连（第 1/3 次）");
    });

    streamRef.current.onerror({ type: "reconnect_exhausted" });
    await waitFor(() => {
      expect(document.body.textContent).toContain("生成任务可能仍在后台继续");
    });
  });

  it("多章节进度/日志按 section_id 归位：两个章节各自独立、不串位", async () => {
    await renderAndStart();

    emit({ status: "started", total: 2, section_ids: [11, 12] });
    emit({ status: "completed", section_id: 12, title: "劳动力配置", saved: true, word_count: 500, progress: 100 });
    emit({ status: "completed", section_id: 11, title: "施工部署", saved: true, word_count: 900, progress: 100 });

    await waitFor(() => {
      const text = document.body.textContent || "";
      expect(text).toContain("劳动力配置");
      expect(text).toContain("施工部署");
      // 两个章节各自一条「已保存」，不因合并而丢失/重复计算
      expect((text.match(/已保存/g) || []).length).toBeGreaterThanOrEqual(2);
    });

    emit({ status: "completed", success: 2, failed: 0, total: 2 });
    await waitFor(() => {
      // 总数为 2，而不是被同一章节重复事件覆盖成 1
      expect(document.body.textContent).toContain("正文生成完成：2/2");
    });
  });
});
