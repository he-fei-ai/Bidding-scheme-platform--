/**
 * 全局事实一致性审计 —— 判定纯函数测试（BUG-FIX 2026-10-15 P-4）。
 *
 * 判定逻辑原内联在 SummaryReviewPage 的一致性审计按钮 onClick 里，其 else
 * 分支把「不是 failed 也不是 partial」全部当成通过。后端一旦漏回
 * audit_status（契约漂移）或将来新增取值，界面就会**谎报**「一致性检查通过」
 * —— 最危险的失败形态：用户以为已核验，实际上审计可能根本没跑。
 *
 * 现在判定抽为 utils.classifyAuditOutcome，本文件把「只有显式 ok 才通过」
 * 这一不变式钉死。
 */
import { describe, it, expect } from "vitest";
import { classifyAuditOutcome } from "./utils";

describe("classifyAuditOutcome（审计结论判定）", () => {
  it("显式 ok 且无异常项 → success 一致性检查通过", () => {
    const v = classifyAuditOutcome({
      items: [{ status: "consistent" }, { status: "consistent" }],
      audit_status: "ok",
    });
    expect(v.type).toBe("success");
    expect(v.text).toBe("一致性检查通过");
  });

  it("P-4 核心：audit_status 缺失时不得谎报通过", () => {
    const v = classifyAuditOutcome({ items: [{ status: "consistent" }] });
    expect(v.type).toBe("warning");
    expect(v.text).toContain("字段缺失");
    expect(v.text).not.toBe("一致性检查通过");
  });

  it("P-4 核心：audit_status 未知取值时不得谎报通过", () => {
    const v = classifyAuditOutcome({
      items: [{ status: "consistent" }],
      audit_status: "unknown_new_value",
    });
    expect(v.type).toBe("warning");
    expect(v.text).toContain("unknown_new_value");
  });

  it("failed 优先于一切（即使有不一致项也要先说明审计没跑起来）", () => {
    const v = classifyAuditOutcome({
      items: [{ status: "inconsistent" }],
      audit_status: "failed",
    });
    expect(v.type).toBe("error");
    expect(v.text).toContain("未能执行");
  });

  it("有 inconsistent 项 → warning 并给出数量", () => {
    const v = classifyAuditOutcome({
      items: [{ status: "inconsistent" }, { status: "inconsistent" }, { status: "consistent" }],
      audit_status: "ok",
    });
    expect(v.type).toBe("warning");
    expect(v.text).toContain("2 处事实不一致");
  });

  it("partial 覆盖率不足 → warning 并给出章节覆盖数", () => {
    const v = classifyAuditOutcome({
      items: [{ status: "consistent" }],
      audit_status: "partial",
      audited_sections: 3,
      total_sections: 7,
    });
    expect(v.type).toBe("warning");
    expect(v.text).toContain("3/7");
    expect(v.text).toContain("不等于全部一致");
  });

  it("无矛盾但有 missing 项 → warning", () => {
    const v = classifyAuditOutcome({
      items: [{ status: "consistent" }, { status: "missing" }],
      audit_status: "ok",
    });
    expect(v.type).toBe("warning");
    expect(v.text).toContain("1 项事实");
  });

  it("未传 items（契约漂移）不得崩溃", () => {
    expect(() => classifyAuditOutcome({ audit_status: "ok" })).not.toThrow();
    expect(() => classifyAuditOutcome({})).not.toThrow();
  });

  it("判定顺序稳定：failed > inconsistent > partial > missing > ok > 未知", () => {
    const cases: Array<[Parameters<typeof classifyAuditOutcome>[0], string]> = [
      [{ items: [{ status: "inconsistent" }], audit_status: "failed" }, "error"],
      [{ items: [{ status: "inconsistent" }], audit_status: "partial", audited_sections: 1, total_sections: 2 }, "warning"],
      [{ items: [{ status: "missing" }], audit_status: "partial", audited_sections: 1, total_sections: 2 }, "warning"],
      [{ items: [], audit_status: "ok" }, "success"],
      [{ items: [], audit_status: undefined }, "warning"],
    ];
    for (const [input, type] of cases) {
      expect(classifyAuditOutcome(input).type, JSON.stringify(input)).toBe(type);
    }
  });
});
