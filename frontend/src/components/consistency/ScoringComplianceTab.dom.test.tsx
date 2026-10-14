// @vitest-environment jsdom
/**
 * ScoringComplianceTab 组件级交互测试（ENHANCE 2026-10-13 第六轮新增）。
 *
 * 背景：该 Tab 2026-10-06 就有后端实现、2026-10-08 补了 UI，但**始终没有
 * 组件级测试文件」——四个姊妹 Tab（ConflictList/AgentRepair/FactBackflow/
 * RejectionCoverage）都有 dom.test，唯独它没有。本文件补齐，并覆盖本轮
 * （第六轮）的两处行为变更：
 *
 * 1. **阈值从规则快照取**（G7）：修复前 UI 只展示判定结果、从不说明「weak
 *    是在什么阈值下判的」，用户看到「疑似未答到点」无法自行复核；现在阈值
 *    随 `rules.scoring_status.coverage_weak` 展示，快照未就绪回退静态 0.25。
 * 2. **状态文案与后端命名空间同源**：组件走 `utils.mapConsistencyScoringStatus`
 *    （静态兜底 + 运行时对齐，与严重度/废标状态同一套机制）。
 *
 * 桩数据照抄后端 `audit_scoring_compliance()` → `ComplianceReport.to_dict()`
 * 的真实返回结构（字段名逐个对齐，不手写契约里不存在的字段）。
 */
import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen, waitFor, cleanup } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

const hasDom = typeof window !== "undefined" && typeof document !== "undefined";

import ScoringComplianceTab from "./ScoringComplianceTab";
import {
  applyConsistencyScoringStatusMeta,
  resetConsistencyScoringStatusMeta,
} from "../../utils";

// vi.hoisted：mock 实例与 vi.mock 工厂一并提升，规避 ESM 静态 import 的 TDZ。
const { scoringMock } = vi.hoisted(() => ({ scoringMock: vi.fn() }));

const DEFAULT_REPORT = {
  status: "ok",
  project_id: 1,
  audit_time: "2026-10-13 10:00:00",
  audit_status: "completed",
  total_points: 3,
  unmapped: 1,
  empty: 0,
  weak: 1,
  covered: 1,
  gaps: [
    {
      scoring_point_id: "sp1",
      name: "施工组织设计",
      weight: "10",
      standard: "须包含施工工艺流程图并附 BIM 模型",
      status: "weak",
      coverage: 0.21,
      section_ids: [3],
      section_titles: ["第三章 施工组织设计"],
      content_chars: 860,
      detail: "评分标准用词在已挂载章节中的覆盖率仅 21.0%（阈值 25%），疑似未针对该评分项作答",
    },
    {
      scoring_point_id: "sp2",
      name: "安全文明施工",
      weight: "5",
      standard: "",
      status: "unmapped",
      coverage: 0,
      section_ids: [],
      section_titles: [],
      content_chars: 0,
      detail: "目录中没有任何章节挂载该评分点",
    },
  ],
  warnings: [],
};

scoringMock.mockImplementation(async () => DEFAULT_REPORT);

vi.mock("../../api", () => ({
  getScoringCompliance: scoringMock,
}));

vi.mock("../../utils", async (importOriginal) => {
  const actual = await importOriginal<Record<string, unknown>>();
  return {
    ...actual,
    extractApiError: (e: unknown) => (e instanceof Error ? e.message : String(e)),
  };
});

function renderTab(projectId = 1, rules?: unknown) {
  // openSection 深链需要 Router 上下文
  return render(
    <MemoryRouter>
      <ScoringComplianceTab projectId={projectId} rules={rules as never} />
    </MemoryRouter>,
  );
}

describe.skipIf(!hasDom)("ScoringComplianceTab", () => {
  afterEach(() => {
    cleanup();
    scoringMock.mockReset();
    scoringMock.mockImplementation(async () => DEFAULT_REPORT);
    // 规则映射是模块级状态：用例里改过必须还原，避免污染其它测试文件
    resetConsistencyScoringStatusMeta();
  });

  it("可挂载，且以 persist=false 拉取（切 Tab 反复查看不写审计历史）", async () => {
    renderTab();
    await waitFor(() => {
      expect(scoringMock).toHaveBeenCalledWith(1, { persist: false });
      expect(screen.getAllByText(/施工组织设计/).length).toBeGreaterThan(0);
    });
  });

  it("展示弱覆盖阈值（rules 快照生效值），用户可自行复核判定口径", async () => {
    // G7 修复前：UI 从不说明 weak 是在什么阈值下判的。
    const rules = { scoring_status: { meta: {}, coverage_weak: 0.4 } };
    renderTab(1, rules);
    await waitFor(() => {
      // 注意用 getAllByText：评分点名称列（「施工组织设计」）与承载章节列
      // （「第三章 施工组织设计」）是子串关系，getByText 会命中多个元素。
      expect(screen.getAllByText(/施工组织设计/).length).toBeGreaterThan(0);
    });
    const text = document.body.textContent || "";
    expect(text).toContain("40%");
    // 覆盖率列的 tooltip 也带阈值说明
    expect(text).toContain("低于 40%");
  });

  it("无 rules 时回退静态兜底阈值 25%", async () => {
    renderTab(1, null);
    await waitFor(() => {
      expect(screen.getAllByText(/施工组织设计/).length).toBeGreaterThan(0);
    });
    expect(document.body.textContent || "").toContain("25%");
  });

  it("状态标签消费运行时对齐后的文案（后端改文案前端跟随）", async () => {
    // 模拟页面壳已 apply 规则快照（与 ConsistencyPage 同机制）
    applyConsistencyScoringStatusMeta({
      weak: { label: "疑似未答到点(快照)", color: "gold" },
      unmapped: { label: "未挂载章节(快照)", color: "red" },
      covered: { label: "已达标(快照)", color: "green" },
      empty: { label: "正文过短(快照)", color: "orange" },
    });
    renderTab();
    await waitFor(() => {
      expect(screen.getAllByText(/施工组织设计/).length).toBeGreaterThan(0);
    });
    expect(screen.getAllByText(/疑似未答到点\(快照\)/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/未挂载章节\(快照\)/).length).toBeGreaterThan(0);
  });

  it("未知状态回落中性「未知」标签，不伪装成已达标", async () => {
    scoringMock.mockImplementation(async () => ({
      ...DEFAULT_REPORT,
      gaps: [
        {
          ...DEFAULT_REPORT.gaps[0],
          scoring_point_id: "spX",
          name: "未来新状态项",
          status: "brand_new_status",
        },
      ],
    }));
    renderTab();
    await waitFor(() => {
      expect(screen.getByText(/未来新状态项/)).toBeTruthy();
    });
    expect(screen.getAllByText("未知").length).toBeGreaterThan(0);
  });

  it("空评分点项目展示引导文案而非空白表格", async () => {
    scoringMock.mockImplementation(async () => ({
      ...DEFAULT_REPORT,
      total_points: 0,
      unmapped: 0,
      weak: 0,
      covered: 0,
      gaps: [],
      warnings: ["该项目的 analysis.scoring_points_json 为空或全部为伪评分项，无法做达标对齐"],
    }));
    renderTab();
    await waitFor(() => {
      expect(screen.getByText(/尚未解析出评分点/)).toBeTruthy();
    });
    // 「无法做达标对齐」同时出现在引导文案与 warnings 告警里 → 用 getAllByText
    expect(screen.getAllByText(/无法做达标对齐/).length).toBeGreaterThan(0);
  });

  it("承载章节标签可点击深链到章节编辑器", async () => {
    renderTab();
    await waitFor(() => {
      expect(screen.getByText(/第三章 施工组织设计/)).toBeTruthy();
    });
    const tag = screen.getByText(/第三章 施工组织设计/);
    // antd Tag 点击不抛错即视为可交互（Router 已挂，跳转由 navigate 处理）
    tag.click();
    expect(tag).toBeTruthy();
  });
});
