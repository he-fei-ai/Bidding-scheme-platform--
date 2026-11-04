import { describe, it, expect } from "vitest";
import {
  validateTimelineData,
  checkDuplicateTitleInContent,
} from "../services/chartValidators";

describe("chartValidators 补齐：时间线校验（后端 parity）", () => {
  it("少于 2 个里程碑判定无效", () => {
    const r = validateTimelineData({ milestones: [{ date: "2026-01-01", title: "开工" }] });
    expect(r.valid).toBe(false);
    expect(r.errors.some((e) => e.includes("至少2个"))).toBe(true);
  });

  it("正常升序时间线通过", () => {
    const r = validateTimelineData({
      milestones: [
        { date: "2026-01-01", title: "开工" },
        { date: "2026-06-30", title: "竣工" },
      ],
    });
    expect(r.valid).toBe(true);
  });

  it("日期逆序被检出", () => {
    const r = validateTimelineData({
      milestones: [
        { date: "2026-06-30", title: "竣工" },
        { date: "2026-01-01", title: "开工" },
      ],
    });
    expect(r.valid).toBe(false);
    expect(r.errors.some((e) => e.includes("逆序"))).toBe(true);
  });

  it("非法日期格式被检出", () => {
    const r = validateTimelineData({
      milestones: [
        { date: "2026/01/01", title: "开工" },
        { date: "2026-06-30", title: "竣工" },
      ],
    });
    expect(r.valid).toBe(false);
    expect(r.errors.some((e) => e.includes("YYYY-MM-DD"))).toBe(true);
  });
});

describe("chartValidators 补齐：重复标题预检 CMP-07（后端 parity）", () => {
  it("给定标题在前 5 行重复出现判定为重复", () => {
    const content = ["# 施工部署", "正文说明", "# 施工部署", "结尾"];
    expect(checkDuplicateTitleInContent(content.join("\n"), "施工部署")).toBe(true);
  });

  it("标题仅出现一次不判定重复", () => {
    const content = ["# 施工部署", "正文说明", "# 进度计划", "结尾"];
    expect(checkDuplicateTitleInContent(content.join("\n"), "施工部署")).toBe(false);
  });

  it("未给定标题时，前 5 行存在完全相同行判定重复", () => {
    const content = ["第一章", "第一节", "第一章", "结尾"];
    expect(checkDuplicateTitleInContent(content.join("\n"))).toBe(true);
  });

  it("空内容不判定重复", () => {
    expect(checkDuplicateTitleInContent("", "标题")).toBe(false);
  });
});
