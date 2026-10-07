/**
 * 上传解析模块 —— 前端交互纯函数组件级测试（2026-10-05）。
 *
 * 覆盖从 FileUploadPage.tsx 抽取的解析状态映射与计数纯函数：
 * - mapFileStatusTag：前后端解析状态契约（BUG-FIX 2026-09-18 P1）
 * - countExtractedFiles：提取完成计数（success 兼容历史 extracted）
 */
import { describe, it, expect } from "vitest";
import { mapFileStatusTag, countExtractedFiles } from "./utils";

describe("mapFileStatusTag（前后端解析状态契约）", () => {
  it("后端当前写入态：success/failed/processing/pending", () => {
    expect(mapFileStatusTag("success")).toEqual({ color: "success", text: "已提取" });
    expect(mapFileStatusTag("failed")).toEqual({ color: "error", text: "提取失败" });
    expect(mapFileStatusTag("processing")).toEqual({ color: "processing", text: "提取中" });
    expect(mapFileStatusTag("pending")).toEqual({ color: "default", text: "待提取" });
  });

  it("历史旧 key 兼容：extracted/uploaded/extracting 仍映射，不显示灰色原始值", () => {
    expect(mapFileStatusTag("extracted")).toEqual({ color: "success", text: "已提取" });
    expect(mapFileStatusTag("uploaded")).toEqual({ color: "default", text: "已上传" });
    expect(mapFileStatusTag("extracting")).toEqual({ color: "processing", text: "提取中" });
  });

  it("未知状态回退为原始值展示（不崩溃、不吞信息）", () => {
    expect(mapFileStatusTag("weird_state")).toEqual({ color: "default", text: "weird_state" });
  });
});

describe("countExtractedFiles（提取完成计数）", () => {
  it("空列表为 0", () => {
    expect(countExtractedFiles([])).toBe(0);
  });

  it("success 与历史 extracted 均计入，其余不计", () => {
    const files = [
      { status: "success" },
      { status: "extracted" },
      { status: "processing" },
      { status: "failed" },
      { status: "uploaded" },
    ];
    expect(countExtractedFiles(files)).toBe(2);
  });
});
