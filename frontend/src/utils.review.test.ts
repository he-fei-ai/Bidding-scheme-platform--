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
    // BUG-FIX 2026-10-09：断言从「整体 toEqual」改为逐字段校验。
    // parseScoringPoints 现在会**透传**原始条目（`...p`）并补齐
    // category / source_ref / source_page / confidence —— 目的是让要点提取页
    // 用表格视图确认时，JSON.stringify(scoringPoints) 不再把后端
    // normalize_writing_targets 写出的溯源字段整条丢掉（原先是数据破坏）。
    // 旧的 toEqual 精确断言等于把「丢弃字段」这个 bug 钉死成了契约，
    // 因此改为断言真正需要保证的不变量：5 个核心字段按别名规则归一化。
    expect(out[0].id).toBe("R1");
    expect(out[0].name).toBe("施工组织");
    expect(out[0].weight).toBe(10);
    expect(out[0].standard).toBe("优 5 分");
    expect(out[0].source).toBe("");
    expect(out[1].id).toBe("R2");
    expect(out[1].name).toBe("报价");
    expect(out[1].weight).toBe(30);
    expect(out[1].standard).toBe("合理低价");
    expect(out[1].source).toBe("评标办法");
  });

  // BUG-FIX 2026-10-09（跨模块数据链回归）：后端 normalize_writing_targets
  // 会写出 category / source_ref / source_page / confidence 四项结构化溯源字段，
  // 它们经「表格视图确认 → POST /ai/scoring/confirm」回写数据库。
  // 修复前 mapPoint 只挑 5 个字段，这条链路上四项全部丢失（数据破坏）。
  it("保留后端结构化溯源字段（category/source_ref/source_page/confidence）", () => {
    const raw = [
      {
        id: "R1",
        name: "施工组织",
        weight: "10",
        standard: "优",
        source: "评标办法",
        category: "技术评分",
        source_ref: "第三章 3.2",
        source_page: 12,
        confidence: 0.87,
      },
    ];
    const out = parseScoringPoints(JSON.stringify(raw));
    expect(out[0].category).toBe("技术评分");
    expect(out[0].source_ref).toBe("第三章 3.2");
    expect(out[0].source_page).toBe(12);
    expect(out[0].confidence).toBe(0.87);
    // 关键契约：经一次 stringify/parse 往返（模拟前端确认保存）后仍完整。
    expect(JSON.parse(JSON.stringify(out))[0]).toMatchObject(raw[0]);
  });

  it("缺失溯源字段时补后端默认值，不产生 undefined", () => {
    const out = parseScoringPoints(JSON.stringify([{ name: "甲" }]));
    expect(out[0].category).toBe("评分项");
    expect(out[0].source_ref).toBe("");
    expect(out[0].source_page).toBeNull();
    expect(out[0].confidence).toBe(1.0);
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
