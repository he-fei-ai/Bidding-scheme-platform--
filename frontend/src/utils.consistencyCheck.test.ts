/**
 * 一致性校核 —— 纯函数单测（2026-10-08 新增）
 *
 * 覆盖 `utils.ts` 中本轮新增/改写的两个纯函数：
 *  1. `parseConflictSources` —— 冲突来源字符串 → 可深链的章节引用
 *  2. `applyConsistencySeverityMeta` / `getConsistencySeverityMeta` /
 *     `resetConsistencySeverityMeta` —— 严重度映射与后端规则快照对齐
 *
 * 这两个函数是从组件里抽出来的纯函数（与 `mapConsistencySeverity` 同一思路），
 * 目的是让「来源解析」与「规则覆盖」的行为可以在不挂载 antd 的前提下被精确断言。
 */
import { describe, it, expect, beforeEach } from "vitest";
import {
  parseConflictSources,
  applyConsistencySeverityMeta,
  getConsistencySeverityMeta,
  resetConsistencySeverityMeta,
  mapConsistencySeverity,
  DEFAULT_CONSISTENCY_SEVERITY,
} from "./utils";

describe("parseConflictSources —— 冲突来源解析", () => {
  it("解析 section_<id> (标题) 为可深链引用", () => {
    const refs = parseConflictSources(["section_12 (第三章 施工方案)"]);
    expect(refs).toHaveLength(1);
    expect(refs[0]).toMatchObject({
      sectionId: 12,
      title: "第三章 施工方案",
      linkable: true,
    });
  });

  it("summary / global_facts 来源不可深链", () => {
    const refs = parseConflictSources([
      "summary (摘要)",
      "global_facts (已确认事实基准)",
    ]);
    expect(refs.map((r) => r.linkable)).toEqual([false, false]);
    expect(refs[0].sectionId).toBeNull();
    expect(refs[1].raw).toBe("global_facts (已确认事实基准)");
  });

  it("容忍多空格与前后空白", () => {
    const refs = parseConflictSources(["  section_7   (第七章)  "]);
    expect(refs[0]).toMatchObject({ sectionId: 7, title: "第七章", linkable: true });
  });

  it("无标题的 section 来源仍可深链，标题回退为空串", () => {
    const refs = parseConflictSources(["section_3"]);
    expect(refs[0]).toMatchObject({ sectionId: 3, title: "", linkable: true });
  });

  it("标题含括号时仍能正确切出 id 与标题", () => {
    const refs = parseConflictSources(["section_9 (第三章（重点）方案)"]);
    expect(refs[0]).toMatchObject({ sectionId: 9, title: "第三章（重点）方案" });
  });

  it("非数字 id 不被当作可深链（防跳到错误章节）", () => {
    const refs = parseConflictSources(["section_abc (某章)", "section_-1 (负数)"]);
    expect(refs.map((r) => r.linkable)).toEqual([false, false]);
    expect(refs.every((r) => r.sectionId === null)).toBe(true);
  });

  it("空/非法输入安全返回空数组，不抛错", () => {
    expect(parseConflictSources(null)).toEqual([]);
    expect(parseConflictSources(undefined)).toEqual([]);
    expect(parseConflictSources([])).toEqual([]);
  });

  it("混合来源保持原有顺序", () => {
    const refs = parseConflictSources([
      "global_facts (已确认事实基准)",
      "section_1 (第一章)",
      "summary (摘要)",
      "section_2 (第二章)",
    ]);
    expect(refs.map((r) => r.sectionId)).toEqual([null, 1, null, 2]);
  });
});

describe("严重度映射与后端规则对齐", () => {
  beforeEach(() => {
    resetConsistencySeverityMeta();
  });

  it("默认映射与后端 SEVERITY_META 一致（ENHANCE 2026-10-08 统一命名空间）", () => {
    expect(getConsistencySeverityMeta()).toEqual({
      high: { color: "red", label: "严重" },
      medium: { color: "orange", label: "中等" },
      low: { color: "blue", label: "轻微" },
    });
  });

  it("未知严重度回退到默认值", () => {
    expect(DEFAULT_CONSISTENCY_SEVERITY).toBe("medium");
    expect(mapConsistencySeverity("nonsense").label).toBe("中等");
    expect(mapConsistencySeverity("").label).toBe("中等");
  });

  it("应用后端规则快照后文案随之改变（前后端同源）", () => {
    const applied = applyConsistencySeverityMeta({
      high: { label: "致命", color: "volcano" },
      medium: { label: "重要", color: "gold" },
      low: { label: "提示", color: "cyan" },
    });
    expect(applied).toBe(true);
    expect(mapConsistencySeverity("high")).toEqual({ label: "致命", color: "volcano" });
    expect(mapConsistencySeverity("low")).toEqual({ label: "提示", color: "cyan" });
  });

  it("非法/空快照不覆盖现有映射（不得把标签文案清空）", () => {
    expect(applyConsistencySeverityMeta(null)).toBe(false);
    expect(applyConsistencySeverityMeta(undefined)).toBe(false);
    expect(applyConsistencySeverityMeta({})).toBe(false);
    expect(applyConsistencySeverityMeta({ high: null } as never)).toBe(false);
    expect(applyConsistencySeverityMeta({ high: {} } as never)).toBe(false);
    // 仍保持内置三档
    expect(Object.keys(getConsistencySeverityMeta()).sort()).toEqual(["high", "low", "medium"]);
    expect(mapConsistencySeverity("high").label).toBe("严重");
  });

  it("部分有效的快照只采纳有效条目", () => {
    const applied = applyConsistencySeverityMeta({
      high: { label: "严重", color: "red" },
      broken: { label: 123 } as never,
    });
    expect(applied).toBe(true);
    const meta = getConsistencySeverityMeta();
    expect(meta.high).toEqual({ label: "严重", color: "red" });
    expect(meta.broken).toBeUndefined();
  });

  it("getConsistencySeverityMeta 返回副本，外部改写不影响内部状态", () => {
    const snapshot = getConsistencySeverityMeta();
    snapshot.high.label = "被篡改";
    expect(mapConsistencySeverity("high").label).toBe("严重");
  });

  it("reset 恢复内置静态兜底（测试隔离）", () => {
    applyConsistencySeverityMeta({ high: { label: "X", color: "y" } });
    expect(mapConsistencySeverity("high").label).toBe("X");
    resetConsistencySeverityMeta();
    expect(mapConsistencySeverity("high").label).toBe("严重");
  });
});