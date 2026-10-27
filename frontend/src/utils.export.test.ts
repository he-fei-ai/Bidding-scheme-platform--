/**
 * 导出文档模块 —— 前端交互纯函数组件级测试（2026-10-04）。
 *
 * 覆盖从 ExportConfigPage.tsx 抽取的三个交互逻辑纯函数：
 * - formatExportFileSize：历史表格/结果区两处内联格式化统一（修复结果区 <1KB
 *   显示 "0.0 KB" 且无 B 档的问题）
 * - reduceExportProgressEvent：SSE 事件 → UI 进度/阶段状态映射
 * - classifyExportFailure：取消 / 409 组锁冲突 / 降级同步接口三分支
 *
 * 与后端 SSE completed 事件契约（tests/test_bugfix_2026_10_04_export_docx.py
 * TestSseCompletedContract）互为前后端双侧钉住。
 */
import { describe, it, expect } from "vitest";
import {
  formatExportFileSize,
  reduceExportProgressEvent,
  classifyExportFailure,
} from "./utils";

describe("formatExportFileSize（导出文件大小格式化）", () => {
  it("B 档：<1024 显示字节数（结果区旧版此处显示 0.0 KB）", () => {
    expect(formatExportFileSize(0)).toBe("0 B");
    expect(formatExportFileSize(512)).toBe("512 B");
    expect(formatExportFileSize(1023)).toBe("1023 B");
  });

  it("KB 档：保留一位小数", () => {
    expect(formatExportFileSize(1024)).toBe("1.0 KB");
    expect(formatExportFileSize(1536)).toBe("1.5 KB");
    expect(formatExportFileSize(1024 * 1024 - 1)).toBe("1024.0 KB");
  });

  it("MB 档：≥1MiB 切换 MB", () => {
    expect(formatExportFileSize(1024 * 1024)).toBe("1.0 MB");
    expect(formatExportFileSize(3.6 * 1024 * 1024)).toBe("3.6 MB");
  });

  it("非法值兜底为占位符，不再渲染 NaN/负数", () => {
    expect(formatExportFileSize(-1)).toBe("-");
    expect(formatExportFileSize(NaN)).toBe("-");
    expect(formatExportFileSize(Infinity)).toBe("-");
  });
});

describe("reduceExportProgressEvent（SSE 事件 → UI 状态）", () => {
  it("start 事件：重置阶段文案", () => {
    expect(reduceExportProgressEvent({ event: "start" })).toEqual({
      phase: "开始构建文档...",
      progress: 0,
    });
  });

  it("progress 事件：百分比取整 + step 作为阶段文案", () => {
    expect(
      reduceExportProgressEvent({ event: "progress", step: "正在渲染图表", progress: 0.4567 }),
    ).toEqual({ phase: "正在渲染图表", progress: 46 });
  });

  it("progress 事件缺 step：使用默认文案", () => {
    const ui = reduceExportProgressEvent({ event: "progress", progress: 0.5 });
    expect(ui?.phase).toBe("正在生成 DOCX 文档...");
    expect(ui?.progress).toBe(50);
  });

  it("progress 为负数/非数字时忽略（不污染进度条）", () => {
    expect(reduceExportProgressEvent({ event: "progress", progress: -1 })).toBeNull();
    expect(reduceExportProgressEvent({ event: "progress" })).toBeNull();
  });

  it("log/connected/completed/error/keep_alive 均不进 UI 状态", () => {
    for (const event of ["log", "connected", "completed", "error", "keep_alive"]) {
      expect(reduceExportProgressEvent({ event })).toBeNull();
    }
  });
});

describe("classifyExportFailure（导出失败三分支判定）", () => {
  it("用户主动取消 → cancelled（不降级重跑同步导出，BUG-EXPORT-03）", () => {
    expect(classifyExportFailure(new Error("abort"), true)).toBe("cancelled");
    expect(
      classifyExportFailure(Object.assign(new Error("x"), { __cancelled: true }), false),
    ).toBe("cancelled");
  });

  it("组锁 409 → conflict（不降级，避免同锁二次 409 连环弹窗）", () => {
    expect(classifyExportFailure({ status: 409, message: "冲突" }, false)).toBe("conflict");
    expect(classifyExportFailure({ response: { status: 409 } }, false)).toBe("conflict");
  });

  it("其他 SSE 传输/构建失败 → fallback（降级同步接口）", () => {
    expect(classifyExportFailure(new Error("network down"), false)).toBe("fallback");
    expect(classifyExportFailure({ status: 500 }, false)).toBe("fallback");
  });

  it("优先级：取消判定先于 409（取消时即便服务端报 409 也不再弹冲突）", () => {
    expect(classifyExportFailure({ status: 409 }, true)).toBe("cancelled");
  });

  // BUG-FIX 2026-10-27（P2·误降级同步导出）：已收到 stream_id 说明构建已在
  // 后端启动并持有导出组锁 —— 降级同步 exportDocx 必然撞 409 或重复构建，
  // 必须归为独立 background 分支（转「后台继续」提示，不降级不报错）。
  it("构建已启动后流中断 → background（不降级，提示查导出历史）", () => {
    expect(
      classifyExportFailure(Object.assign(new Error("流中断"), { __stream_started: true }), false),
    ).toBe("background");
  });

  it("优先级：409 冲突判定先于 background（锁冲突仍单独提示）", () => {
    expect(
      classifyExportFailure(
        Object.assign({ status: 409 }, { __stream_started: true }),
        false,
      ),
    ).toBe("conflict");
    // 取消优先级同样高于 background
    expect(
      classifyExportFailure(
        Object.assign(new Error("x"), { __stream_started: true, __cancelled: true }),
        false,
      ),
    ).toBe("cancelled");
  });
});
