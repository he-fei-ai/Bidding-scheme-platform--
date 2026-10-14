// @vitest-environment jsdom
/**
 * 文本模型配置模块 —— API 封装层契约测试（2026-10-14）。
 *
 * 钉住前后端接口契约，防止响应形状漂移再次引发页面崩溃：
 *  1. `getConfigAudit`：后端返回 `{items, total}`，封装必须归一为**数组**。
 *     修复前封装按 `Array<...>` 声明并直接 `return res.data`，AIConfigPage 会把
 *     一个对象塞进 antd `<Table dataSource>` —— 审计页运行时崩溃（dataSource 无 .map）。
 *  2. `fetchModelInfo`：新增可选 `provider_id` 字段必须原样透传到请求体。
 *  3. `saveTextProviderConfig`：URL 必须对中文厂商名做 encodeURIComponent。
 */
import { describe, it, expect, vi, beforeEach } from "vitest";

const { getMock, postMock, putMock } = vi.hoisted(() => ({
  getMock: vi.fn(),
  postMock: vi.fn(),
  // 声明带参签名（url, payload），使 mock.calls[0][0]/[0][1] 具备正确元组类型；
  // 此前零参实现会让 calls[0] 推断为空元组 []，取 [0] 报 TS2493/TS2352。
  putMock: vi.fn(async (_url: string, _payload?: unknown) => ({ data: { status: "ok" } })),
}));

vi.mock("axios", () => {
  const instance = {
    get: getMock,
    post: postMock,
    put: putMock,
    delete: vi.fn(),
    interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } },
    defaults: { headers: { common: {} } },
  };
  const create = vi.fn(() => instance);
  return { default: { create }, create };
});

import { getConfigAudit, fetchModelInfo, saveTextProviderConfig } from "./api/index";

beforeEach(() => {
  getMock.mockReset();
  postMock.mockReset();
  putMock.mockClear();
});

describe("getConfigAudit（配置审计接口契约）", () => {
  it("后端返回 {items,total} 时归一为数组", async () => {
    const items = [{ actor: "admin", action: "save_config", target: "DeepSeek", detail: "d", created_at: "t" }];
    getMock.mockResolvedValue({ data: { items, total: 1 } });

    const rows = await getConfigAudit({ limit: 50 });
    expect(Array.isArray(rows)).toBe(true);
    expect(rows).toEqual(items);
    expect(getMock).toHaveBeenCalledWith("/ai/config-audit", { params: { limit: 50 } });
  });

  it("后端返回裸数组（旧实现/代理改写）时同样返回数组", async () => {
    const items = [{ actor: "a", action: "b", target: "c", detail: "d", created_at: "e" }];
    getMock.mockResolvedValue({ data: items });
    await expect(getConfigAudit()).resolves.toEqual(items);
  });

  it("空响应 / 缺 items 时返回空数组，绝不返回对象", async () => {
    getMock.mockResolvedValue({ data: {} });
    await expect(getConfigAudit()).resolves.toEqual([]);

    getMock.mockResolvedValue({ data: null });
    await expect(getConfigAudit()).resolves.toEqual([]);
  });
});

describe("fetchModelInfo（模型元数据接口契约）", () => {
  it("透传 provider_id（后端按 id 查元数据表）", async () => {
    postMock.mockResolvedValue({ data: { model: "glm-4-flash", max_tokens: 8192, concurrency: 20, source: "builtin" } });

    await fetchModelInfo({
      api_key: "ENC:xxx",
      base_url: "https://open.bigmodel.cn/api/paas/v4",
      model: "glm-4-flash",
      provider: "智谱AI (GLM)",
      provider_id: "zhipu",
    });

    expect(postMock).toHaveBeenCalledWith("/ai/model-info", expect.objectContaining({ provider_id: "zhipu" }));
  });

  it("不传 provider_id 时请求体不注入该键（保持对旧后端的完全兼容）", async () => {
    postMock.mockResolvedValue({ data: { model: "m", max_tokens: 1, concurrency: 1, source: "default" } });
    await fetchModelInfo({ api_key: "k", base_url: "u", model: "m", provider: "p" });
    const body = postMock.mock.calls[0][1];
    expect(Object.prototype.hasOwnProperty.call(body, "provider_id")).toBe(false);
  });
});

describe("saveTextProviderConfig（厂商配置保存接口契约）", () => {
  it("中文厂商名必须 URL 编码", async () => {
    await saveTextProviderConfig({
      provider_name: "阿里云百炼（通义千问）",
      provider_type: "openai_compatible",
      api_key: "***已配置***",
      base_url: "https://dashscope.aliyuncs.com/compatible-mode/v1",
      model: "qwen-turbo",
      max_tokens: 4096,
      concurrency: 4,
      temperature: 0.7,
      document_profile: "",
    });

    const url = putMock.mock.calls[0][0] as string;
    expect(url.startsWith("/ai/provider-configs/")).toBe(true);
    expect(url).not.toContain("（");
    expect(url).toContain(encodeURIComponent("阿里云百炼（通义千问）"));
  });
});