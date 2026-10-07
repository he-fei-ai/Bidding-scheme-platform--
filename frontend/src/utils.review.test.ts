/**
 * 提取项目（要点/评分点）模块 —— 前端交互纯函数组件级测试（2026-10-05）。
 *
 * 覆盖从 SummaryReviewPage.tsx 抽取的评分点/摘要解析纯函数，
 * 钉住对多种后端/AI 返回形态的容错逻辑。
 */
import { describe, it, expect } from "vitest";
import { parseScoringPoints, isValidSummary, parseArrayData } from "./utils";

describe("parseScoringPoints（评分点解析容错）", () => {
  it("直接数组：字段别名映射 name/title、weight/score、standard/criteria", () => {
    const out = parseScoringPoints(
      JSON.stringify([
        { title: "施工组织", score: 10, criteria: "优 5 分" },
        { name: "报价", weight: 30, standard: "合理低价", source: "评标办法" },
      ]),
    );
    expect(out).toHaveLength(2);
    expect(out[0]).toEqual({ id: "R1", name: "施工组织", weight: 10, standard: "优 5 分", source: "" });
    expect(out[1]).toEqual({ id: "R2", name: "报价", weight: 30, standard: "合理低价", source: "评标办法" });
  });

  it("保留显式 id", () => {
    const out = parseScoringPoints(JSON.stringify([{ id: "A1", name: "x" }]));
    expect(out[0].id).toBe("A1");
  });

  it("包裹对象形态：从 scoring_points/points/items/data/result/results/list 提取数组", () => {
    const out = parseScoringPoints(JSON.stringify({ scoring_points: [{ name: "a" }, { name: "b" }] }));
    expect(out.map((p) => p.name)).toEqual(["a", "b"]);
    const out2 = parseScoringPoints(JSON.stringify({ data: [{ name: "c" }] }));
    expect(out2[0].name).toBe("c");
  });

  it("非法条目(null)回退为空字段项，仍占位保序", () => {
    const out = parseScoringPoints(JSON.stringify([null, { name: "ok" }]));
    expect(out[0]).toEqual({ id: "R1", name: "", weight: "", standard: "", source: "" });
    expect(out[1].id).toBe("R2");
  });

  it("无法解析/空对象无数组：返回空数组不抛异常", () => {
    expect(parseScoringPoints("not json")).toEqual([]);
    expect(parseScoringPoints(JSON.stringify({ foo: 1 }))).toEqual([]);
    expect(parseScoringPoints("")).toEqual([]);
  });
});

describe("isValidSummary（摘要有效性）", () => {
  it("空/占位 {} [] 判为无效", () => {
    expect(isValidSummary("")).toBe(false);
    expect(isValidSummary("{}")).toBe(false);
    expect(isValidSummary("[]")).toBe(false);
  });

  it("合法 JSON 对象/数组判为有效", () => {
    expect(isValidSummary(JSON.stringify({ a: 1 }))).toBe(true);
    expect(isValidSummary(JSON.stringify([1, 2]))).toBe(true);
  });

  it("非 JSON 文本按长度>20 判定", () => {
    expect(isValidSummary("短文本")).toBe(false);
    expect(isValidSummary("这是一段足够长度的中文招标摘要内容描述，包含项目概况与技术要求")).toBe(true);
  });
});

describe("parseArrayData（宽松数组解析）", () => {
  it("仅顶层合法数组返回原值", () => {
    expect(parseArrayData("[1,2,3]")).toEqual([1, 2, 3]);
  });
  it("空串/[] /非法/非数组均返回空数组", () => {
    expect(parseArrayData("")).toEqual([]);
    expect(parseArrayData("[]")).toEqual([]);
    expect(parseArrayData("bad")).toEqual([]);
    expect(parseArrayData('{"a":1}')).toEqual([]);
  });
});
