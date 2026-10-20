// @vitest-environment jsdom
/**
 * 正文生成顺序模式（generation_mode）前后端契约测试（2026-10-18 第二轮）。
 *
 * 钉住：
 *  1. `generateContentSSEUrl` 显式传入 generationMode 时必须下发
 *     `generation_mode` 查询参数（batch/serial/parallel 三值）；
 *  2. 不传（undefined）时**不得**下发该参数 —— 保持旧调用路径零回归，
 *     走后端 Query 默认 "parallel"；
 *  3. 参数必须追加在签名末尾，既有位置参数调用不受影响。
 */
import { describe, it, expect, beforeEach } from "vitest";
import { generateContentSSEUrl } from "./api/index";

beforeEach(() => {
  localStorage.clear();
});

function queryOf(url: string): URLSearchParams {
  const qIndex = url.indexOf("?");
  expect(qIndex).toBeGreaterThan(-1);
  return new URLSearchParams(url.slice(qIndex + 1));
}

describe("generateContentSSEUrl generation_mode 透传", () => {
  it("显式传入 batch/serial/parallel 时下发参数", () => {
    for (const mode of ["batch", "serial", "parallel"] as const) {
      const url = generateContentSSEUrl(1, "standard", undefined, null, false, undefined, false, false, false, false, 4, undefined, null, mode);
      expect(queryOf(url).get("generation_mode")).toBe(mode);
    }
  });

  it("不传 generationMode 时不下发该参数（旧行为零回归）", () => {
    const url = generateContentSSEUrl(1, "standard", undefined, null, false, undefined, false, false, false, false, 4, undefined, null);
    expect(queryOf(url).has("generation_mode")).toBe(false);
  });

  it("既有参数不受末尾新增参数影响", () => {
    const url = generateContentSSEUrl(7, "detailed", [3, 4], 9, true, 3500, true, true, true, true, 6, { maxAiImages: 2, tableRatio: "heavy", preferMermaid: false, sectionPerSegment: 8 }, true, "serial");
    const q = queryOf(url);
    expect(q.get("writing_length")).toBe("detailed");
    expect(q.get("regenerate_ids")).toBe("3,4");
    expect(q.get("root_section_id")).toBe("9");
    expect(q.get("regenerate_missing")).toBe("true");
    expect(q.get("use_orchestration")).toBe("true");
    expect(q.get("concurrency")).toBe("6");
    expect(q.get("orch_max_ai_images")).toBe("2");
    expect(q.get("orch_table_ratio")).toBe("heavy");
    expect(q.get("use_word_adjust")).toBe("true");
    expect(q.get("generation_mode")).toBe("serial");
  });

  it("URL 指向 generate-content SSE 端点", () => {
    const url = generateContentSSEUrl(42, "standard", undefined, null, false, undefined, false, false, false, false, undefined, undefined, null, "batch");
    expect(url).toContain("/api/v1/projects/42/sections/generate-content?");
  });
});
