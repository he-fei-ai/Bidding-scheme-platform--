/**
 * 上传解析模块 —— 第十二轮回归（2026-11-04）纯函数测试。
 *
 * 对应修复（均有后端代码行号证据，见 报告_上传解析与要点提取模块_第十二轮）：
 * - F-12-1 formatParsingStatusText：后端 GET /projects/{id}/tasks/files/status
 *   在项目无任何解析任务行时只返回 `{status:'idle', message:...}`
 *   （routers/tasks.py:322），不含计数字段。旧实现拿 undefined 参与比较与
 *   拼接，产出「最近解析完成 undefined 个，失败 undefined 个」。
 *   现 idle 短路 + 计数字段缺省防御。
 * - F-12-2 formatBatchSummary：整批全部 duplicate（ok=0 且无在途/失败）时
 *   旧实现走 success 分支，绿色「成功 0 个文件」。现 warning 级直说内容重复。
 * - F-12-3 FileParsingStatus 类型可选化（types/index.ts）随本组测试的行为
 *   断言一起钉住（idle 响应形状）。
 */
import { describe, it, expect } from "vitest";
import { formatParsingStatusText, formatBatchSummary, EMPTY_BATCH_TOTALS } from "./utils";

describe("formatParsingStatusText · F-12-1 idle 响应防御", () => {
  it("后端真实 idle 响应（仅 status+message）返回 null，不产出 undefined 文案", () => {
    // routers/tasks.py:322：return status or {'status': 'idle', 'message': '没有文件解析任务'}
    const realIdle = { status: "idle", message: "没有文件解析任务" };
    expect(formatParsingStatusText(realIdle as any)).toBeNull();
  });

  it("非 idle 但计数字段缺省（老后端/异常响应）返回 null", () => {
    expect(formatParsingStatusText({ status: "processing" } as any)).toBeNull();
    expect(
      formatParsingStatusText({ status: "completed", processing: 0 } as any),
    ).toBeNull();
  });

  it("null / undefined 仍返回 null（既有契约）", () => {
    expect(formatParsingStatusText(null)).toBeNull();
    expect(formatParsingStatusText(undefined)).toBeNull();
  });

  it("字段齐全的聚合响应不受影响（回归）", () => {
    expect(
      formatParsingStatusText({
        status: "processing",
        processing: 2,
        recent_completed: 1,
        recent_failed: 0,
        total_tasks: 3,
        latest: null,
      }),
    ).toBe("正在解析 2 个文件…");
    expect(
      formatParsingStatusText({
        status: "completed",
        processing: 0,
        recent_completed: 3,
        recent_failed: 0,
        total_tasks: 3,
        latest: null,
      }),
    ).toBe("最近解析完成 3 个文件");
  });
});

describe("formatBatchSummary · F-12-2 纯重复批级别", () => {
  it("整批全部 duplicate 且无成功/在途/失败 → warning 直说内容重复", () => {
    const res = formatBatchSummary({
      ...EMPTY_BATCH_TOTALS,
      duplicate: 3,
    });
    expect(res).not.toBeNull();
    expect(res!.type).toBe("warning");
    expect(res!.text).toContain("3 个文件内容已存在");
    expect(res!.text).not.toContain("成功 0 个");
  });

  it("含成功文件 + 部分重复 → 维持既有 success 文案（回归）", () => {
    const res = formatBatchSummary({
      ...EMPTY_BATCH_TOTALS,
      ok: 2,
      duplicate: 1,
    });
    expect(res).not.toBeNull();
    expect(res!.type).toBe("success");
    expect(res!.text).toContain("成功 2 个文件");
    expect(res!.text).toContain("1 个重复未上传");
  });

  it("含在途（后台解析）文件 + 重复 → 维持既有 success 文案（回归）", () => {
    const res = formatBatchSummary({
      ...EMPTY_BATCH_TOTALS,
      ok: 1,
      pending: 1,
      duplicate: 1,
    });
    expect(res).not.toBeNull();
    expect(res!.type).toBe("success");
    expect(res!.text).toContain("1 个正在后台解析");
  });

  it("有解析失败时仍走既有 warning 汇总（回归）", () => {
    const res = formatBatchSummary({
      ...EMPTY_BATCH_TOTALS,
      ok: 1,
      parseFailed: 2,
    });
    expect(res).not.toBeNull();
    expect(res!.type).toBe("warning");
    expect(res!.text).toContain("解析失败 2 个");
  });
});
