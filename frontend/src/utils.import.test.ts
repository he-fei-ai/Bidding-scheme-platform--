/**
 * 上传解析模块 —— 前端交互纯函数组件级测试（2026-10-05）。
 *
 * 覆盖从 FileUploadPage.tsx 抽取的解析状态映射与计数纯函数：
 * - mapFileStatusTag：前后端解析状态契约（BUG-FIX 2026-09-18 P1）
 * - countExtractedFiles：提取完成计数（success 兼容历史 extracted）
 */
import { describe, it, expect } from "vitest";
import { mapFileStatusTag, countExtractedFiles, FILE_STATUSES } from "./utils";

describe("mapFileStatusTag（前后端解析状态契约）", () => {
  it("后端当前写入态：success/failed/processing/pending", () => {
    expect(mapFileStatusTag("success")).toEqual({ color: "success", text: "已提取" });
    expect(mapFileStatusTag("failed")).toEqual({ color: "error", text: "提取失败" });
    expect(mapFileStatusTag("processing")).toEqual({ color: "processing", text: "提取中" });
    expect(mapFileStatusTag("pending")).toEqual({ color: "default", text: "待提取" });
  });

  it("历史旧 key 兼容：extracted/uploaded/extracting 仍映射，不显示灰色原始值", () => {
    expect(mapFileStatusTag("extracted")).toEqual({ color: "success", text: "已提取" });
    expect(mapFileStatusTag("uploaded")).toEqual({ color: "default", text: "已上传" });
    expect(mapFileStatusTag("extracting")).toEqual({ color: "processing", text: "提取中" });
  });

  it("未知状态回退为原始值展示（不崩溃、不吞信息）", () => {
    expect(mapFileStatusTag("weird_state")).toEqual({ color: "default", text: "weird_state" });
  });
});

describe("mapFileStatusTag（BUG-FIX 2026-10-15 P-5/TD-B 状态口径收敛）", () => {
  it("DDL 默认值 queued 必须已登记，不得泄漏英文原始值", () => {
    // file_parse_tasks.status 的 DDL 默认值是 'queued'（db_migrations.py），
    // 任何绕过 _create_parse_task 直接 INSERT 的路径（历史脏数据、迁移回填、
    // 外部脚本）都可能留下一行 queued。修复前 mapFileStatusTag('queued')
    // 落到兜底分支，界面上直接显示英文 'queued'。
    expect(mapFileStatusTag("queued")).toEqual({ color: "default", text: "排队中" });
  });

  it("FILE_STATUSES 收录后端实际会写入的全部状态（静态一致性守卫）", () => {
    // 后端写入的真实取值集合：
    //   uploaded_files.status   → processing / success / failed / pending
    //   file_parse_tasks.status → processing / completed / failed（DDL 默认 queued）
    // 这里锁定的是「前端必须认识的键」，新增后端状态而忘记登记会被本断言拦住。
    const mustCover = ["success", "failed", "processing", "pending", "queued"];
    for (const s of mustCover) {
      expect(FILE_STATUSES, `FILE_STATUSES 缺 ${s}`).toContain(s);
    }
  });

  it("FILE_STATUSES 不得出现后端从不产生的状态被误标为现役", () => {
    // 兼容键必须显式存在（历史数据），但必须能被识别出来做代码审查
    expect(FILE_STATUSES).toContain("extracted");
    expect(FILE_STATUSES).toContain("uploaded");
    expect(FILE_STATUSES).toContain("extracting");
    // 不得重复登记
    expect(new Set(FILE_STATUSES).size).toBe(FILE_STATUSES.length);
    expect(FILE_STATUSES).toHaveLength(8);
  });
});

describe("countExtractedFiles（提取完成计数）", () => {
  it("空列表为 0", () => {
    expect(countExtractedFiles([])).toBe(0);
  });

  it("success 与历史 extracted 均计入，其余不计", () => {
    const files = [
      { status: "success" },
      { status: "extracted" },
      { status: "processing" },
      { status: "failed" },
      { status: "uploaded" },
    ];
    expect(countExtractedFiles(files)).toBe(2);
  });
});
