/**
 * 目录生成 + 正文生成模块 —— 前端交互纯函数组件级测试（2026-10-05）。
 *
 * 覆盖从生产组件 SSE onmessage 分支抽取的交互逻辑纯函数：
 * - reduceOutlineGeneratingEvent：目录生成 SSE generating 事件 → UI 进度状态增量
 *   （钉住 BUG-OUTLINE-03：所有 phase 统一按 outline_step 计算百分比，消除进度跳跃）
 * - upsertSectionProgressItem：正文生成逐章节 SSE 进度条目 upsert 合并逻辑
 *
 * 抽自 OutlineEditorPage.tsx / SectionEditorPage.tsx，行为逐分支等价，
 * 组件与单测共用同一事实源（../utils），避免逻辑漂移。
 */
import { describe, it, expect } from "vitest";
import {
  OUTLINE_STEPS_V51,
  reduceOutlineGeneratingEvent,
  upsertSectionProgressItem,
} from "./utils";

describe("reduceOutlineGeneratingEvent（目录生成 SSE → UI 进度）", () => {
  const total = OUTLINE_STEPS_V51.length; // 13（2026-10-19 起：并发+增量+智能自检口径）

  it("outline_phase：按 outline_step 计算百分比并回填阶段标签", () => {
    const patch = reduceOutlineGeneratingEvent({
      phase: "outline_phase",
      outline_step: 3,
    });
    expect(patch.outlineStep).toBe(3);
    expect(patch.progressPercent).toBe(
      Math.min(100, Math.round(((3 - 1) / total) * 100)),
    );
    // 无 message 时回退到阶段标签文案
    expect(patch.progressMessage).toBe(OUTLINE_STEPS_V51[2]);
  });

  it("outline_phase 带 message：优先使用事件 message", () => {
    const patch = reduceOutlineGeneratingEvent({
      phase: "outline_phase",
      outline_step: 1,
      message: "正在生成一级目录",
    });
    expect(patch.progressMessage).toBe("正在生成一级目录");
    expect(patch.progressPercent).toBe(0);
  });

  it("heartbeat：仅更新提示文案，不改变步骤与百分比（BUG-OUTLINE-03）", () => {
    const patch = reduceOutlineGeneratingEvent({ phase: "heartbeat" });
    expect(patch.outlineStep).toBeUndefined();
    expect(patch.progressPercent).toBeUndefined();
    expect(patch.progressMessage).toBe("AI 正在生成中，请稍候...");
  });

  it("其他阶段（context/saving/v51）带 outline_step：与 outline_phase 同口径计算，消除跳跃", () => {
    const patch = reduceOutlineGeneratingEvent({
      phase: "context",
      outline_step: 5,
    });
    expect(patch.outlineStep).toBe(5);
    expect(patch.progressPercent).toBe(
      Math.min(100, Math.round(((5 - 1) / total) * 100)),
    );
    expect(patch.progressMessage).toBe("");
  });

  it("其他阶段无 outline_step：仅更新 message，不动步骤与百分比", () => {
    const patch = reduceOutlineGeneratingEvent({
      phase: "saving",
      message: "正在保存",
    });
    expect(patch.outlineStep).toBeUndefined();
    expect(patch.progressPercent).toBeUndefined();
    expect(patch.progressMessage).toBe("正在保存");
  });

  it("百分比封顶 100（outline_step 越界不产生 >100 进度）", () => {
    const patch = reduceOutlineGeneratingEvent({
      phase: "outline_phase",
      outline_step: 100,
    });
    expect(patch.progressPercent).toBe(100);
  });
});

interface ProgressItem {
  section_id: number;
  status?: string;
  word_count?: number;
}

describe("upsertSectionProgressItem（正文生成逐章节 SSE 合并）", () => {
  it("空列表：追加首条", () => {
    const out = upsertSectionProgressItem<ProgressItem>([], {
      section_id: 1,
      status: "generating",
    });
    expect(out).toEqual([{ section_id: 1, status: "generating" }]);
  });

  it("已存在同 section_id：浅合并更新而非重复追加", () => {
    const prev: ProgressItem[] = [{ section_id: 1, status: "generating", word_count: 0 }];
    const out = upsertSectionProgressItem(prev, {
      section_id: 1,
      status: "completed",
      word_count: 1200,
    });
    expect(out).toHaveLength(1);
    expect(out[0]).toEqual({
      section_id: 1,
      status: "completed",
      word_count: 1200,
    });
  });

  it("不同 section_id：追加为独立条目", () => {
    const prev: ProgressItem[] = [{ section_id: 1 }];
    const out = upsertSectionProgressItem(prev, { section_id: 2 });
    expect(out.map((p) => p.section_id)).toEqual([1, 2]);
  });

  it("不原地修改入参（返回新数组，保障 React 状态不可变性）", () => {
    const prev: ProgressItem[] = [{ section_id: 1, status: "generating" }];
    const snapshot = JSON.parse(JSON.stringify(prev));
    upsertSectionProgressItem(prev, { section_id: 1, status: "failed" });
    expect(prev).toEqual(snapshot);
  });
});
