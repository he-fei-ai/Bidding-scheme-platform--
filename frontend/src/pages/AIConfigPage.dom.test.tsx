// @vitest-environment jsdom
/**
 * AI 模型配置中心页 —— 组件级（DOM）交互测试。
 *
 * 钉住 HARDENING 2026-10-11 新增的前端契约集成：
 * 1. 「清空主密钥」按钮 → 确认弹框 → 调用 POST /ai/config/clear-api-key
 * 2. 「配置审计」Tab → 切换后加载审计日志并渲染（G-5 过滤 UI 入口存在）
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor, fireEvent, cleanup } from "@testing-library/react";
import { Modal } from "antd";

// vi.mock 工厂会被 hoist 到模块顶部，外部 const 不可直接引用 → 用 vi.hoisted 创建
const { clearAiConfigApiKeyMock, getConfigAuditMock } = vi.hoisted(() => ({
  clearAiConfigApiKeyMock: vi.fn(async () => ({ status: "ok" })),
  getConfigAuditMock: vi.fn(async () => [
    {
      actor: "admin",
      action: "clear_api_key",
      target: "ai_config",
      detail: "主配置密钥已清空",
      created_at: "2026-10-11 00:00:00",
    },
  ]),
}));

vi.mock("../api", () => ({
  // 文本/图像 provider 配置（本测试不涉及，空实现）
  getImageModelConfig: vi.fn(async () => ({})),
  saveImageModelConfig: vi.fn(async () => undefined),
  testImageConnection: vi.fn(async () => ({ status: "error" })),
  getImageModelProviders: vi.fn(async () => ({ providers: [] })),
  fetchKnownProviders: vi.fn(async () => []),
  // Profile
  fetchAIProfiles: vi.fn(async () => []),
  getAIDefaultProfile: vi.fn(async () => null),
  createAIPProfile: vi.fn(async () => ({})),
  updateAIPProfile: vi.fn(async () => ({})),
  deleteAIPProfile: vi.fn(async () => ({ status: "ok" })),
  setAIDefaultProfile: vi.fn(async () => ({ status: "ok" })),
  applyAIPProfile: vi.fn(async () => ({ status: "ok" })),
  exportAIPProfiles: vi.fn(async () => ({ profiles: [] })),
  importAIPProfiles: vi.fn(async () => ({ status: "ok" })),
  // HARDENING 2026-10-11 新增
  clearAiConfigApiKey: clearAiConfigApiKeyMock,
  getConfigAudit: getConfigAuditMock,
  // Usage & Health
  getUsageDashboard: vi.fn(async () => ({ total_tokens: 0, total_cost: 0 })),
  getHealthDashboard: vi.fn(async () => ({ providers: [] })),
  getUsageTrend: vi.fn(async () => ({ data: [] })),
  truncateUsageStats: vi.fn(async () => ({ deleted_records: 0 })),
}));

import AIConfigPage from "./AIConfigPage";

beforeEach(() => {
  clearAiConfigApiKeyMock.mockClear();
  getConfigAuditMock.mockClear();
});

afterEach(() => {
  // 清理 Modal.confirm portal 等测试残留 DOM，避免跨用例污染
  cleanup();
  document.body.innerHTML = "";
});

describe("AIConfigPage —— HARDENING 2026-10-11 集成", () => {
  it("渲染「清空主密钥」按钮，确认后调用 clearAiConfigApiKey", async () => {
    const confirmSpy = vi.spyOn(Modal, "confirm");
    render(<AIConfigPage />);

    fireEvent.click(await screen.findByText("清空主密钥"));

    // Modal.confirm 应被触发；直接调用其 onOk，避免 jsdom portal 渲染问题
    expect(confirmSpy).toHaveBeenCalled();
    const args = confirmSpy.mock.calls[0][0] as { onOk?: () => void };
    expect(typeof args.onOk).toBe("function");

    await args.onOk?.();

    await waitFor(() => expect(clearAiConfigApiKeyMock).toHaveBeenCalled());
    confirmSpy.mockRestore();
  });

  it("切到「配置审计」Tab 后加载并渲染审计日志", async () => {
    render(<AIConfigPage />);

    fireEvent.click(await screen.findByText(/配置审计/));

    await waitFor(() => expect(getConfigAuditMock).toHaveBeenCalled());

    expect(await screen.findByText("clear_api_key")).toBeTruthy();
    expect(await screen.findByText(/主配置密钥已清空/)).toBeTruthy();
  });
});