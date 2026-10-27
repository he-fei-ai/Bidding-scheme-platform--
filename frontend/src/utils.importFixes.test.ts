/**
 * 上传解析模块 —— 2026-10-26 第九轮纯函数测试
 *
 * 覆盖本轮新增/变更的纯函数与常量（组件级测试见
 * FileUploadPage.hardening.dom.test.tsx）：
 *
 *   - isInFlightFileStatus / IN_FLIGHT_FILE_STATUSES：在途状态唯一口径
 *    （pending 不得被当在途，否则清空提取会制造幽灵任务）
 *   - parseProjectId：路由参数 → 正整数（此前 "1.5" 会被放行）
 *   - checkReadyForExtraction：进入「要点提取」的流程门禁
 *   - resolveUploadLimitMb：后端下发上限的精确解析（防整数截断漂移）
 *   - isOcrDisabled：ocr_enabled 的消费口径
 *   - NEEDS_REANALYSIS_MESSAGE：主文件换源提示的单一真相
 *   - formatClearTextsResult：files_kept_processing 的文案口径
 */
import { describe, it, expect } from "vitest";

import {
  isInFlightFileStatus,
  IN_FLIGHT_FILE_STATUSES,
  parseProjectId,
  checkReadyForExtraction,
  resolveUploadLimitMb,
  isOcrDisabled,
  NEEDS_REANALYSIS_MESSAGE,
  formatClearTextsResult,
  formatParsingStatusText,
} from "./utils";

// ─── isInFlightFileStatus ──────────────────────────────────────

describe("isInFlightFileStatus（在途状态唯一口径）", () => {
  it("processing / queued 视为在途", () => {
    expect(isInFlightFileStatus("processing")).toBe(true);
    expect(isInFlightFileStatus("queued")).toBe(true);
  });

  it("pending 不是在途（清空提取后不会被当成解析中）", () => {
    expect(isInFlightFileStatus("pending"), "pending 不得被当在途").toBe(false);
  });

  it("success / failed 不是在途", () => {
    expect(isInFlightFileStatus("success")).toBe(false);
    expect(isInFlightFileStatus("failed")).toBe(false);
    expect(isInFlightFileStatus("extracted")).toBe(false);
  });

  it("空值与未知值安全返回 false（不抛错）", () => {
    expect(isInFlightFileStatus(undefined)).toBe(false);
    expect(isInFlightFileStatus(null)).toBe(false);
    expect(isInFlightFileStatus("")).toBe(false);
    expect(isInFlightFileStatus("unknown_status")).toBe(false);
  });

  it("集合为冻结常量且不含 pending", () => {
    expect(Object.isFrozen(IN_FLIGHT_FILE_STATUSES)).toBe(true);
    expect(Array.from(IN_FLIGHT_FILE_STATUSES)).not.toContain("pending");
  });
});

// ─── parseProjectId ────────────────────────────────────────────

describe("parseProjectId（路由参数校验）", () => {
  it("合法正整数原样返回", () => {
    expect(parseProjectId("1")).toBe(1);
    expect(parseProjectId("42")).toBe(42);
    expect(parseProjectId("007")).toBe(7);
  });

  it("缺失 / 空串返回 null", () => {
    expect(parseProjectId(null)).toBeNull();
    expect(parseProjectId(undefined)).toBeNull();
    expect(parseProjectId("")).toBeNull();
  });

  it("非整数 / 非数字 / 非正值返回 null", () => {
    expect(parseProjectId("1.5"), "小数不得放行").toBeNull();
    expect(parseProjectId("1abc"), "脏串不得放行").toBeNull();
    expect(parseProjectId("abc")).toBeNull();
    expect(parseProjectId("0")).toBeNull();
    expect(parseProjectId("-3")).toBeNull();
    expect(parseProjectId("Infinity")).toBeNull();
    expect(parseProjectId("NaN")).toBeNull();
  });
});

// ─── checkReadyForExtraction ───────────────────────────────────

describe("checkReadyForExtraction（进入要点提取的流程门禁）", () => {
  it("有已提取成功的文件 → 放行", () => {
    expect(
      checkReadyForExtraction([{ status: "failed" }, { status: "success" }]),
    ).toEqual({ ready: true });
  });

  it("兼容历史 extracted 状态", () => {
    expect(checkReadyForExtraction([{ status: "extracted" }])).toEqual({
      ready: true,
    });
  });

  it("只有失败文件 → 拦截并说明「没有已提取到文本」", () => {
    const r = checkReadyForExtraction([{ status: "failed" }]);
    expect(r.ready).toBe(false);
    expect((r as { reason: string }).reason).toContain("没有已提取到文本");
  });

  it("文件列表为空 → 拦截", () => {
    expect(checkReadyForExtraction([]).ready).toBe(false);
  });

  it("有处理中的文件 → 拦截并提示「请等解析完成」（优先于失败文案）", () => {
    const r = checkReadyForExtraction([
      { status: "failed" },
      { status: "processing" },
    ]);
    expect(r.ready).toBe(false);
    expect((r as { reason: string }).reason).toContain("正在解析中");
  });

  it("只有 pending（清空后的静态态）→ 拦截且不谎称在解析", () => {
    const r = checkReadyForExtraction([{ status: "pending" }]);
    expect(r.ready).toBe(false);
    expect((r as { reason: string }).reason).not.toContain("正在解析中");
  });
});

// ─── resolveUploadLimitMb ──────────────────────────────────────

describe("resolveUploadLimitMb（后端下发上限的精确解析）", () => {
  it("优先使用精确浮点值（防整数截断漂移）", () => {
    expect(
      resolveUploadLimitMb({ max_file_size_mb: 25, max_file_size_exact_mb: 25.5 }),
    ).toBe(25.5);
  });

  it("缺精确值时回落整 MB 值（老后端兼容）", () => {
    expect(resolveUploadLimitMb({ max_file_size_mb: 50 })).toBe(50);
  });

  it("再回落字节值换算", () => {
    expect(resolveUploadLimitMb({ max_file_size: 3 * 1024 * 1024 })).toBe(3);
  });

  it("全空 / 非法 / 零值 → 本地兜底", () => {
    expect(resolveUploadLimitMb(null)).toBe(50);
    expect(resolveUploadLimitMb(undefined)).toBe(50);
    expect(resolveUploadLimitMb({})).toBe(50);
    expect(
      resolveUploadLimitMb({ max_file_size_exact_mb: 0, max_file_size_mb: -1 }),
    ).toBe(50);
    expect(resolveUploadLimitMb({ max_file_size_mb: null }, 128)).toBe(128);
  });

  it("忽略非数字的精确值（防御畸形响应）", () => {
    expect(resolveUploadLimitMb({ max_file_size_exact_mb: NaN, max_file_size_mb: 42 })).toBe(42);
  });
});

// ─── isOcrDisabled ─────────────────────────────────────────────

describe("isOcrDisabled（OCR 开关消费口径）", () => {
  it("仅当后端明确返回 false 时才判定为关闭", () => {
    expect(isOcrDisabled({ ocr_enabled: false })).toBe(true);
    expect(isOcrDisabled({ ocr_enabled: true })).toBe(false);
  });

  it("缺字段 / 查询失败按可用处理（避免网络抖动把图片入口误封）", () => {
    expect(isOcrDisabled(null)).toBe(false);
    expect(isOcrDisabled(undefined)).toBe(false);
    expect(isOcrDisabled({})).toBe(false);
    expect(isOcrDisabled({ ocr_enabled: null })).toBe(false);
  });
});

// ─── NEEDS_REANALYSIS_MESSAGE ──────────────────────────────────

describe("NEEDS_REANALYSIS_MESSAGE（主文件换源提示单一真相）", () => {
  it("文案指向「提取项目」且标明主文件已变更", () => {
    expect(NEEDS_REANALYSIS_MESSAGE.length).toBeGreaterThan(0);
    expect(NEEDS_REANALYSIS_MESSAGE).toContain("提取项目");
    expect(NEEDS_REANALYSIS_MESSAGE).toContain("主文件已变更");
  });
});

// ─── formatClearTextsResult · files_kept_processing ────────────

describe("formatClearTextsResult（含 files_kept_processing 口径）", () => {
  it("有在途文件被保留时如实告知（不再让用户误以为全被清空）", () => {
    expect(
      formatClearTextsResult({
        files_reset: 1,
        tasks_cleared: 0,
        files_kept_processing: 2,
      }),
    ).toEqual({
      level: "success",
      text: "已重置 1 个文件状态为待提取，清理 0 条旧解析记录；2 个正在解析中的文件未受影响",
    });
  });

  it("老后端不下发该字段 → 文案与旧版逐字一致（向后兼容）", () => {
    expect(formatClearTextsResult({ files_reset: 3, tasks_cleared: 2 })).toEqual({
      level: "success",
      text: "已重置 3 个文件状态为待提取，清理 2 条旧解析记录",
    });
  });

  it("warning 优先级最高（重置失败仍降级为告警）", () => {
    const r = formatClearTextsResult({
      files_reset: 0,
      tasks_cleared: 0,
      files_kept_processing: 1,
      warning: "提取文本已删除，但数据库状态重置失败",
    });
    expect(r.level).toBe("warning");
    expect(r.text).toContain("数据库状态重置失败");
  });
});

// ─── formatParsingStatusText · 失败原因透出 ────────────────────

describe("formatParsingStatusText（失败分支原因透出）", () => {
  const base = {
    status: "completed" as const,
    processing: 0,
    recent_completed: 2,
    recent_failed: 1,
    total_tasks: 3,
  };

  it("最近一条为失败时追加失败原因", () => {
    expect(
      formatParsingStatusText({
        ...base,
        latest: {
          status: "failed",
          filename: "扫描.png",
          file_type: "png",
          progress: 100,
          total_files: 1,
          completed_files: 0,
          error_message: "OCR 引擎不可用",
          started_at: "",
          updated_at: "",
        },
      }),
    ).toBe("最近解析完成 2 个，失败 1 个（OCR 引擎不可用）");
  });

  it("最近一条不是失败时不追加无关原因", () => {
    expect(
      formatParsingStatusText({
        ...base,
        latest: {
          status: "completed",
          filename: "甲.txt",
          file_type: "txt",
          progress: 100,
          total_files: 1,
          completed_files: 1,
          error_message: "",
          started_at: "",
          updated_at: "",
        },
      }),
    ).toBe("最近解析完成 2 个，失败 1 个");
  });

  it("latest 为空时保持纯计数文案", () => {
    expect(formatParsingStatusText({ ...base, latest: null })).toBe(
      "最近解析完成 2 个，失败 1 个",
    );
  });
});
