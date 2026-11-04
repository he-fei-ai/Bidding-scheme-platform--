// @vitest-environment jsdom
/**
 * 导出下载 URL 后端源统一（B-2，2026-11-04 导出模块加固轮）——纯函数级钉住。
 *
 * 背景：getExportDownloadUrl / getLatestExportUrl / exportPdf / exportHtml 此前
 * 用裸相对路径（/api/v1/...），而 SSE 导出通道走 toSSEFullUrl（含
 * VITE_API_BASE_URL 前缀）。跨域部署（VITE_API_BASE_URL 非空）时 SSE 能通，
 * 但 <a href> 下载与 fetch 下载打到前端源 → 404，同一模块两条通道分叉。
 *
 * 修复后四条通道统一经 toApiFullUrl 补后端源：
 *  - 未配置 VITE_API_BASE_URL → 输出与历史逐字相同（相对路径 + token），向后兼容；
 *  - 配置后 → 与 SSE 同源。
 *
 * SSE_BASE_URL 是模块加载时常量，故每个用例先 stubEnv 再 resetModules 动态导入。
 */
import { afterEach, describe, expect, it, vi } from "vitest";

async function loadApi(): Promise<typeof import("./api")> {
  vi.resetModules();
  return await import("./api");
}

describe("B-2 导出下载 URL 后端源统一", () => {
  afterEach(() => {
    vi.unstubAllEnvs();
    vi.unstubAllGlobals();
    window.localStorage.removeItem("auth_token");
  });

  it("未配置 VITE_API_BASE_URL 时保持相对路径（历史口径，向后兼容）", async () => {
    vi.stubEnv("VITE_API_BASE_URL", "");
    const api = await loadApi();
    const url = api.getExportDownloadUrl(1, 9);
    expect(url.startsWith("/api/v1/projects/1/export/download/9")).toBe(true);
    const latest = api.getLatestExportUrl(2);
    expect(latest.startsWith("/api/v1/projects/2/export/latest")).toBe(true);
  });

  it("配置 VITE_API_BASE_URL 时下载 URL 与 SSE 同源（B-2 修复点）", async () => {
    vi.stubEnv("VITE_API_BASE_URL", "https://api.example.com");
    const api = await loadApi();
    const url = api.getExportDownloadUrl(1, 9);
    expect(url.startsWith("https://api.example.com/api/v1/projects/1/export/download/9")).toBe(
      true,
    );
    const latest = api.getLatestExportUrl(2);
    expect(latest.startsWith("https://api.example.com/api/v1/projects/2/export/latest")).toBe(
      true,
    );
  });

  it("token 查询参数在后端源前缀之后追加，且幂等于两种模式", async () => {
    window.localStorage.setItem("auth_token", "tok-123");

    // SSE_BASE_URL 是模块加载时常量：每次切换 stub 后必须重载模块
    vi.stubEnv("VITE_API_BASE_URL", "");
    let api = await loadApi();
    let url = api.getExportDownloadUrl(1, 9);
    expect(url).toBe("/api/v1/projects/1/export/download/9?token=tok-123");

    vi.stubEnv("VITE_API_BASE_URL", "https://api.example.com");
    api = await loadApi();
    url = api.getExportDownloadUrl(1, 9);
    expect(url).toBe("https://api.example.com/api/v1/projects/1/export/download/9?token=tok-123");
  });

  it("无 token 时不追加查询参数（行为与历史一致）", async () => {
    vi.stubEnv("VITE_API_BASE_URL", "");
    const api = await loadApi();
    expect(api.getLatestExportUrl(3)).toBe("/api/v1/projects/3/export/latest");
  });
});
