/**
 * 审核与预检 + 文本模型配置模块 —— 前端交互纯函数组件级测试（2026-10-05）。
 *
 * - mapConsistencySeverity：一致性冲突严重度 → Tag 颜色/文案（未知回退 medium）
 * - findProviderMeta / TEXT_PROVIDER_META：文本模型厂商默认 base_url/model（大小写不敏感）
 */
import { describe, it, expect } from "vitest";
import { mapConsistencySeverity, findProviderMeta, TEXT_PROVIDER_META } from "./utils";

describe("mapConsistencySeverity（冲突严重度映射）", () => {
  it("high/medium/low 三档标准映射", () => {
    expect(mapConsistencySeverity("high")).toEqual({ color: "red", label: "严重" });
    expect(mapConsistencySeverity("medium")).toEqual({ color: "orange", label: "中等" });
    expect(mapConsistencySeverity("low")).toEqual({ color: "blue", label: "轻微" });
  });

  it("未知/空严重度回退为 medium（不显示 undefined 文案）", () => {
    expect(mapConsistencySeverity("critical")).toEqual({ color: "orange", label: "中等" });
    expect(mapConsistencySeverity("")).toEqual({ color: "orange", label: "中等" });
  });
});

describe("findProviderMeta（文本模型厂商元数据）", () => {
  it("大小写不敏感命中：DASHSCOPE / DashScope 返回同一元数据", () => {
    expect(findProviderMeta("dashscope")).toBe(TEXT_PROVIDER_META.dashscope);
    expect(findProviderMeta("DashScope")).toEqual(findProviderMeta("dashscope"));
    expect(findProviderMeta("AGNES")?.model).toBe("agnes-2.5-flash");
  });

  it("未知厂商返回 null（调用方据此不设默认 base_url/model）", () => {
    expect(findProviderMeta("unknown_vendor")).toBeNull();
  });

  it("每个厂商元数据字段完整，openai_compatible 允许空默认", () => {
    for (const [key, meta] of Object.entries(TEXT_PROVIDER_META)) {
      expect(meta).toHaveProperty("label");
      expect(meta).toHaveProperty("base_url");
      expect(meta).toHaveProperty("model");
      if (key !== "openai_compatible") {
        expect(meta.base_url).not.toBe("");
      }
    }
  });

  it("钉住关键厂商 base_url 口径（防止误改导致连错端点）", () => {
    expect(findProviderMeta("deepseek")?.base_url).toBe("https://api.deepseek.com/v1");
    expect(findProviderMeta("zhipu")?.base_url).toBe("https://open.bigmodel.cn/api/paas/v4");
  });
});
