/**
 * 上传解析模块 —— TD6 单一真相 + 文本预览 纯函数单测
 *
 * 覆盖（2026-10-12 新增）：
 *   1. resolveAccept：后端下发的 accept/extensions → 前端可用 accept 串
 *   2. FALLBACK_ACCEPT 与后端 SUPPORTED_EXTENSIONS 逐项一致（跨层契约）
 *   3. buildTextPreview：超长正文截断（防 Modal 渲染几十万字卡死页面）
 *   4. formatEmptyPreviewText：按文件状态给出可行动提示
 */
import { describe, it, expect } from "vitest";
import {
  resolveAccept,
  FALLBACK_ACCEPT,
  buildTextPreview,
  formatEmptyPreviewText,
} from "./utils";

describe("上传解析 · resolveAccept（TD6 单一真相）", () => {
  it("优先采用后端下发的 accept 串", () => {
    expect(
      resolveAccept({ accept: ".docx,.pdf", extensions: [".xls"] }),
    ).toBe(".docx,.pdf");
  });

  it("accept 缺失时回落到 extensions 数组", () => {
    expect(resolveAccept({ accept: "", extensions: [".docx", ".pdf"] })).toBe(
      ".docx,.pdf",
    );
  });

  it("缺前导点时自动补点", () => {
    expect(resolveAccept({ accept: "docx,pdf" })).toBe(".docx,.pdf");
  });

  it("去重并统一大小写", () => {
    expect(resolveAccept({ accept: ".DOCX, .docx ,.PDF" })).toBe(
      ".docx,.pdf",
    );
  });

  it("后端返回空/畸形时回落本地常量（优化不得变成新故障）", () => {
    expect(resolveAccept(null)).toBe(FALLBACK_ACCEPT);
    expect(resolveAccept(undefined)).toBe(FALLBACK_ACCEPT);
    expect(resolveAccept({})).toBe(FALLBACK_ACCEPT);
    expect(resolveAccept({ accept: "   ", extensions: [] })).toBe(
      FALLBACK_ACCEPT,
    );
    expect(resolveAccept({ accept: ",,," })).toBe(FALLBACK_ACCEPT);
  });

  it("支持自定义兜底值", () => {
    expect(resolveAccept(null, ".x")).toBe(".x");
  });

  // 跨层契约：后端 services/files.SUPPORTED_EXTENSIONS 是唯一真相，
  // 本常量只是网络失败时的降级值。两边不一致意味着后端加了格式、
  // 前端在降级路径下却拦不住（或反之多放行）—— 这正是 2026-09-18
  // 「服务层早已支持 .wps 与图片 OCR、路由层不放行 → 入口封死」的前端版本。
  it("FALLBACK_ACCEPT 与后端 SUPPORTED_EXTENSIONS 集合一致", () => {
    const backend = [
      ".bmp", ".doc", ".docx", ".htm", ".html", ".jpeg", ".jpg", ".md",
      ".pdf", ".png", ".ppt", ".pptx", ".tiff", ".txt", ".webp", ".wps",
      ".xls", ".xlsx",
    ];
    expect(FALLBACK_ACCEPT.split(",").sort()).toEqual(backend.sort());
  });
});

describe("上传解析 · buildTextPreview", () => {
  it("未超限：原样返回", () => {
    const r = buildTextPreview("短文本", 100);
    expect(r).toEqual({ preview: "短文本", truncated: false, total: 3 });
  });

  it("超限：截断并标记 truncated", () => {
    const long = "字".repeat(500);
    const r = buildTextPreview(long, 100);
    expect(r.truncated).toBe(true);
    expect(r.total).toBe(500);
    expect(r.preview).toHaveLength(100);
  });

  it("空/空值输入不抛错", () => {
    expect(buildTextPreview(null)).toEqual({
      preview: "",
      truncated: false,
      total: 0,
    });
    expect(buildTextPreview(undefined)).toEqual({
      preview: "",
      truncated: false,
      total: 0,
    });
    expect(buildTextPreview("")).toEqual({
      preview: "",
      truncated: false,
      total: 0,
    });
  });

  it("默认上限 20000 字（防大文件把 Modal 渲染卡死）", () => {
    const huge = "字".repeat(25_000);
    const r = buildTextPreview(huge);
    expect(r.preview).toHaveLength(20_000);
    expect(r.truncated).toBe(true);
  });
});

describe("上传解析 · formatEmptyPreviewText", () => {
  it("failed 状态引导重试", () => {
    expect(formatEmptyPreviewText("failed")).toContain("重新提取");
  });

  it("pending 状态引导触发解析", () => {
    expect(formatEmptyPreviewText("pending")).toContain("尚未提取");
  });

  it("其他状态给出通用原因说明", () => {
    expect(formatEmptyPreviewText("success")).toContain("没有提取到文本");
    expect(formatEmptyPreviewText(null)).toContain("没有提取到文本");
    expect(formatEmptyPreviewText(undefined)).toContain("没有提取到文本");
  });
});