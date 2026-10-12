// @vitest-environment jsdom
/**
 * 报告下载按钮图标 —— 回归测试（BUG-FIX 2026-10-11）
 *
 * 现象：「废标检查项」「重复内容检查项」两个页面在**任务已完成**时整页白屏，
 * 全局 ErrorBoundary 显示：
 *   Minified React error #31 ... object with keys {$$typeof, render}
 *
 * 根因：两处把图标**组件本身**传给了 antd Button 的 `icon`（ReactNode）属性——
 *   <Button icon={ImportOutlined as any}>下载报告</Button>      ← 应为 <ImportOutlined />
 * 且 `as any` 直接绕过类型检查（ReactNode ≠ 组件类型），因此 tsc 恒绿。
 *
 * React 18.3 里 `React.forwardRef(render)` 的返回值恰为 `{ $$typeof, render }`
 * （两个自有键），forwardRef 组件被当子节点渲染 → 正是报出的 "keys {$$typeof, render}"。
 *
 * 修复：改为 JSX 元素，并顺手把语义错误的 ImportOutlined 换成 DownloadOutlined，
 *      删除 `as any`。本测试钉住：① 根因机制；② 两个页面下载按钮真实渲染出 <svg>。
 */
import React, { Component, type ReactNode } from "react";
import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen, cleanup } from "@testing-library/react";
import { Button } from "antd";
import { ImportOutlined } from "@ant-design/icons";

// vi.mock 工厂会被 hoist，需先创建再引用
const { listRejectionTasksMock, listDuplicateTasksMock } = vi.hoisted(() => ({
  listRejectionTasksMock: vi.fn(),
  listDuplicateTasksMock: vi.fn(),
}));

vi.mock("../api", () => ({
  // 废标检查
  startRejectionCheck: vi.fn(),
  getRejectionStatus: vi.fn(),
  getRejectionResult: vi.fn(),
  listRejectionTasks: listRejectionTasksMock,
  markFalsePositive: vi.fn(),
  retryRejectionDimension: vi.fn(),
  importRejectionFromProject: vi.fn(),
  batchDeleteRejectionTasks: vi.fn(),
  batchMarkFalsePositive: vi.fn(),
  getRejectionTasksByProject: vi.fn(async () => []),
  getKnowledgeSuggestions: vi.fn(async () => []),
  getRejectionReportUrl: vi.fn(() => "/api/v1/report/docx"),
  applySpellingFix: vi.fn(),
  // 内容查重
  startDuplicateCheck: vi.fn(),
  getDuplicateStatus: vi.fn(),
  getDuplicateResult: vi.fn(),
  listDuplicateTasks: listDuplicateTasksMock,
  getDuplicateReportUrl: vi.fn(() => "/api/v1/report/docx"),
}));

import RejectionCheckPage from "./RejectionCheckPage";
import DuplicateCheckPage from "./DuplicateCheckPage";

/** 已完成任务的最低可用夹具：只需 status/id/results 触发下载卡片 */
const completedRejectionTask = {
  id: 1,
  status: "completed",
  progress: 100,
  progress_message: "检查完成",
  enabled_dimensions: ["rejection", "spelling", "logic"],
  bid_documents: [],
  check_items: [],
  results: { rejection: [], spelling: [], logic: [] },
  false_positives: [],
  error: "",
  created_at: "2026-10-11 00:00:00",
  updated_at: "2026-10-11 00:00:00",
  project_id: null,
} as any;

const completedDuplicateTask = {
  id: 2,
  file_ids: [1],
  tender_file_id: null,
  signature: "",
  status: "completed",
  progress: 100,
  progress_message: "查重完成",
  results: {
    metadata: { metadata: [], anomalies: [] },
    toc: { tocs: [], matrix: [], filenames: [], duplicate_groups: [] },
    content: {
      duplicate_groups: [],
      similar_groups: [],
      stats: { total_sentences: 0, duplicate_count: 0, similar_count: 0 },
    },
    images: { total_images: 0, duplicate_groups: [] },
  },
} as any;

/** 简易错误边界：把渲染期抛错转成可断言的标记文本 */
class CatchBoundary extends Component<{ children: ReactNode }, { boom: boolean }> {
  state = { boom: false };
  static getDerivedStateFromError() {
    return { boom: true };
  }
  render() {
    return this.state.boom ? <div>RENDER_CRASH</div> : this.props.children;
  }
}

afterEach(() => {
  cleanup();
  document.body.innerHTML = "";
  vi.clearAllMocks();
});

describe("根因机制 —— forwardRef 组件对象即 {$$typeof, render}", () => {
  it("React 18.3 forwardRef 返回值恰为两个自有键 $$typeof / render", () => {
    const fwd = React.forwardRef<HTMLDivElement, object>(() => <div />);
    expect(Object.keys(fwd)).toEqual(["$$typeof", "render"]);
  });

  it("把图标组件本身当 icon 传入会触发 React error #31（旧写法复现）", () => {
    // 与修复前 <Button icon={ImportOutlined as any}> 完全等价的错误写法
    render(
      <CatchBoundary>
        <Button icon={ImportOutlined as unknown as ReactNode}>下载报告</Button>
      </CatchBoundary>,
    );
    expect(screen.getByText("RENDER_CRASH")).toBeTruthy();
  });

  it("改用 JSX 元素后正常渲染出 <svg>", () => {
    render(<Button icon={<ImportOutlined />}>下载报告</Button>);
    const btn = screen.getByRole("button", { name: /下载报告/ });
    expect(btn.querySelector(".anticon")).toBeTruthy();
    expect(btn.querySelector(".anticon svg")).toBeTruthy();
  });
});

/**
 * 页面级用例的真实图标断言。
 *
 * 超时说明：vitest 默认 testTimeout=5000ms，但这两个页面首次渲染含 antd Table，
 * 在 jsdom 里很慢（见 setupTests.ts 的 getComputedStyle polyfill 注释）。
 * 单独跑本文件时约 1–3s，随全量套件 4 worker 并发跑时 CPU 争用会翻倍到 5s+，
 * 撞穿默认超时会以 "Test timed out in 5000ms" 失败（断言本身从未执行）。
 * 故显式放宽到 30s；findByRole 的 waitForOptions.timeout 同步给到 10s 兜底。
 */
function expectRealIcon(label: string | RegExp) {
  return screen
    .findByRole("button", { name: label }, { timeout: 10_000 })
    .then((btn) => {
      expect(btn.querySelector(".anticon")).toBeTruthy();
      expect(btn.querySelector(".anticon svg")).toBeTruthy();
    });
}

describe("废标检查项页 —— 报告下载按钮渲染不崩", () => {
  it("已完成任务的「下载报告」「下载 Excel 报告」都渲染出真实图标", async () => {
    listRejectionTasksMock.mockResolvedValue([completedRejectionTask]);
    render(<RejectionCheckPage />);

    for (const label of [/下载报告$/, /下载 Excel 报告/]) {
      await expectRealIcon(label);
    }
  }, 30_000);
});

describe("重复内容检查项页 —— 报告下载按钮渲染不崩", () => {
  it("已完成任务的「Word 报告」「Excel 报告（六表）」都渲染出真实图标", async () => {
    listDuplicateTasksMock.mockResolvedValue([completedDuplicateTask]);
    render(<DuplicateCheckPage />);

    for (const label of [/Word 报告/, /Excel 报告（六表）/]) {
      await expectRealIcon(label);
    }
  }, 30_000);
});
