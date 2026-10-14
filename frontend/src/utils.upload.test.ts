/**
 * 上传解析模块 —— 批量上传/角色切换/解析状态 组件级交互测试（2026-10-05）。
 *
 * 覆盖从 FileUploadPage.tsx 抽取的纯函数（组件内联逻辑不可测，先可测化再回归）：
 * - classifyUploadResult：HTTP 200 但解析失败（status=failed）的判定
 * - accumulateBatchTotals / formatBatchSummary：批量上传批尾汇总
 * - shouldNotifySingle：单文件 vs 批量 的逐条提示闸门
 * - formatUploadFailureText / nextFileRole / formatRoleToggleMessage：交互文案
 * - formatParsingStatusText：GET /tasks/files/status 聚合状态的展示映射
 * - formatClearTextsResult：DELETE /files/texts 的「文本已删但库未重置」告警分级（M-5）
 */
import { describe, it, expect } from "vitest";
import {
  classifyUploadResult,
  accumulateBatchTotals,
  totalBatchCount,
  shouldNotifySingle,
  formatBatchSummary,
  formatUploadFailureText,
  nextFileRole,
  formatRoleToggleMessage,
  formatParsingStatusText,
  formatClearTextsResult,
  isImageFile,
  EMPTY_BATCH_TOTALS,
} from "./utils";

describe("classifyUploadResult（上传结果分类）", () => {
  it("解析成功为 ok，解析失败为 parse_failed，无响应为 error", () => {
    expect(classifyUploadResult({ status: "success" })).toBe("ok");
    expect(classifyUploadResult({ status: "failed" })).toBe("parse_failed");
    expect(classifyUploadResult(null)).toBe("error");
    expect(classifyUploadResult(undefined)).toBe("error");
  });

  it("缺少 status 字段（旧后端）不再默认为成功，返回 parse_failed", () => {
    expect(classifyUploadResult({} as { status?: string })).toBe("parse_failed");
  });
});

describe("accumulateBatchTotals（批量计数累加）", () => {
  it("三类结果分别累加，且不修改原对象", () => {
    const t0 = { ...EMPTY_BATCH_TOTALS };
    const t1 = accumulateBatchTotals(t0, "ok");
    const t2 = accumulateBatchTotals(t1, "parse_failed");
    const t3 = accumulateBatchTotals(t2, "error");
    expect(t0).toEqual({ ok: 0, parseFailed: 0, failed: 0 });
    expect(t3).toEqual({ ok: 1, parseFailed: 1, failed: 1 });
    expect(totalBatchCount(t3)).toBe(3);
  });
});

describe("formatBatchSummary（批尾汇总文案）", () => {
  it("只处理 1 个文件时不重复汇总（返回 null）", () => {
    expect(formatBatchSummary({ ok: 1, parseFailed: 0, failed: 0 })).toBeNull();
    expect(formatBatchSummary({ ...EMPTY_BATCH_TOTALS })).toBeNull();
  });

  it("全部成功：success + 成功计数", () => {
    const s = formatBatchSummary({ ok: 3, parseFailed: 0, failed: 0 });
    expect(s).toEqual({ type: "success", text: "批量上传完成，成功 3 个文件" });
  });

  it("BUG-FIX B1 回归：HTTP 成功但解析失败不得计入成功，必须告警", () => {
    const s = formatBatchSummary({ ok: 2, parseFailed: 1, failed: 0 });
    expect(s).not.toBeNull();
    expect(s?.type).toBe("warning");
    expect(s?.text).toContain("成功 2 个");
    expect(s?.text).toContain("解析失败 1 个");
    expect(s?.text).not.toContain("成功 3 个");
  });

  it("请求失败与解析失败分别计数", () => {
    const s = formatBatchSummary({ ok: 1, parseFailed: 2, failed: 3 });
    expect(s?.text).toContain("上传失败 3 个");
    expect(s?.text).toContain("解析失败 2 个");
  });
});

describe("shouldNotifySingle（单文件提示闸门）", () => {
  it("最后一个在途且本批仅 1 个 → 逐条提示", () => {
    expect(shouldNotifySingle(1, { ok: 1, parseFailed: 0, failed: 0 })).toBe(true);
  });

  it("批量场景不逐条刷屏（在途 >1 或本批 >1）", () => {
    expect(shouldNotifySingle(3, { ok: 1, parseFailed: 0, failed: 0 })).toBe(false);
    expect(shouldNotifySingle(1, { ok: 2, parseFailed: 0, failed: 0 })).toBe(false);
  });
});

describe("formatUploadFailureText（失败文案）", () => {
  it("区分解析失败与上传失败", () => {
    expect(formatUploadFailureText("a.pdf", "parse_failed", "不支持的格式")).toBe(
      "a.pdf 解析失败：不支持的格式",
    );
    expect(formatUploadFailureText("b.pdf", "error", "网络错误")).toBe(
      "b.pdf 上传失败：网络错误",
    );
  });

  it("缺省兜底文案", () => {
    expect(formatUploadFailureText("a.pdf", "parse_failed")).toBe(
      "a.pdf 解析失败：请检查文件格式",
    );
    expect(formatUploadFailureText("b.pdf", "error")).toBe("b.pdf 上传失败：未知错误");
  });
});

describe("nextFileRole / formatRoleToggleMessage（主辅切换）", () => {
  it("角色在 main / supporting 之间切换", () => {
    expect(nextFileRole("main")).toBe("supporting");
    expect(nextFileRole("supporting")).toBe("main");
    expect(nextFileRole("")).toBe("main");
    expect(nextFileRole(undefined)).toBe("main");
  });

  it("设为主文件且旧主被降级时如实提示", () => {
    expect(formatRoleToggleMessage("招标文件.pdf", "main", 1)).toBe(
      "已将《招标文件.pdf》设为主文件，原主文件降为辅助",
    );
    expect(formatRoleToggleMessage("招标文件.pdf", "main", 0)).toBe(
      "已将《招标文件.pdf》设为主文件",
    );
  });

  it("设为辅助文件文案", () => {
    expect(formatRoleToggleMessage("清单.xlsx", "supporting", 0)).toBe("已设为辅助文件");
  });
});

describe("isImageFile（图片判定 → 走后台异步解析，遗留修复 L2）", () => {
  it("按 file_type 判定（大小写与前导点容错）", () => {
    expect(isImageFile("png")).toBe(true);
    expect(isImageFile(".JPG")).toBe(true);
    expect(isImageFile("docx")).toBe(false);
    expect(isImageFile("")).toBe(false);
    expect(isImageFile(null)).toBe(false);
  });

  it("file_type 缺失时按文件名后缀判定", () => {
    expect(isImageFile("", "扫描页.PNG")).toBe(true);
    expect(isImageFile(undefined, "招标.pdf")).toBe(false);
    // 边界：不得把含图片后缀的目录名/前缀误判
    expect(isImageFile("", "png说明.docx")).toBe(false);
  });
});

describe("formatParsingStatusText（解析任务状态展示）", () => {
  it("无状态 / 空闲无实质信息时不渲染", () => {
    expect(formatParsingStatusText(null)).toBeNull();
    expect(
      formatParsingStatusText({
        status: "idle",
        processing: 0,
        recent_completed: 0,
        recent_failed: 0,
        total_tasks: 0,
        latest: null,
      }),
    ).toBeNull();
  });

  it("有在途任务时展示在途数量（优先于完成计数）", () => {
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
  });

  it("完成/失败计数区分展示", () => {
    const base = {
      status: "completed",
      processing: 0,
      total_tasks: 3,
      latest: null,
    };
    expect(formatParsingStatusText({ ...base, recent_completed: 3, recent_failed: 0 })).toBe(
      "最近解析完成 3 个文件",
    );
    expect(formatParsingStatusText({ ...base, recent_completed: 2, recent_failed: 1 })).toBe(
      "最近解析完成 2 个，失败 1 个",
    );
  });

  it("解析成功但有非致命警告时一并播报（M-1 诊断字段消费）", () => {
    const warn = {
      status: "completed",
      filename: "招标文件.docx",
      file_type: "docx",
      progress: 100,
      total_files: 1,
      completed_files: 1,
      error_message: "未提取到文本内容（可能为空文件、纯图片扫描件或 OCR 引擎未启用）",
      started_at: "",
      updated_at: "",
    };
    expect(
      formatParsingStatusText({
        status: "completed",
        processing: 0,
        recent_completed: 1,
        recent_failed: 0,
        total_tasks: 1,
        latest: warn,
      }),
    ).toBe("最近解析完成 1 个（未提取到文本内容（可能为空文件、纯图片扫描件或 OCR 引擎未启用））");
    // 警告为空串 / 全空白时保持原纯成功文案
    expect(
      formatParsingStatusText({
        status: "completed",
        processing: 0,
        recent_completed: 3,
        recent_failed: 0,
        total_tasks: 3,
        latest: { ...warn, error_message: "   " },
      }),
    ).toBe("最近解析完成 3 个文件");
    // 失败行的原因不重复播报（已有独立失败计数）
    expect(
      formatParsingStatusText({
        status: "completed",
        processing: 0,
        recent_completed: 2,
        recent_failed: 1,
        total_tasks: 3,
        latest: { ...warn, status: "failed", error_message: "文本提取失败: boom" },
      }),
    ).toBe("最近解析完成 2 个，失败 1 个");
  });
});

describe("formatClearTextsResult（清空提取的结果分级 · M-5）", () => {
  it("后端返回 warning 时降级为 warning 级并原样透出原因", () => {
    const warning =
      "提取文本已删除，但数据库状态重置失败（database is locked），" +
      "文件列表仍显示已提取；请对相关文件执行「重新提取」或重新上传。";
    expect(
      formatClearTextsResult({ files_reset: 0, tasks_cleared: 0, warning }),
    ).toEqual({ level: "warning", text: warning });
  });

  it("无 warning 时按实际重置数量播报成功（不再是「已重置 0 个」的假成功）", () => {
    expect(
      formatClearTextsResult({ files_reset: 3, tasks_cleared: 2, warning: "" }),
    ).toEqual({
      level: "success",
      text: "已重置 3 个文件状态为待提取，清理 2 条旧解析记录",
    });
  });

  it("字段缺失（旧后端）按 0 计数且不抛错", () => {
    expect(formatClearTextsResult({})).toEqual({
      level: "success",
      text: "已重置 0 个文件状态为待提取，清理 0 条旧解析记录",
    });
    // 只有空白警告同样不算失败
    expect(formatClearTextsResult({ warning: "   " }).level).toBe("success");
  });
});
