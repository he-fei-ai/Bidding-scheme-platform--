/**
 * Vitest 规范化启动器（L-1 修复，2026-10-17）
 *
 * 根因（Windows 盘符大小写 → vitest 双实例 → runner 状态分裂）：
 *   Node 的 ESM 模块缓存以 URL 为身份，Windows 盘符大小写敏感：
 *   `file:///j:/...` 与 `file:///J:/...` 是两个不同模块实例。
 *   当从小写盘符 cwd（如 `cd /d j:\...`）启动 `vitest` 时：
 *     - worker 引导进程（dist/worker.js）以小写 `j:` URL 加载 vitest 内部
 *       chunk，并注册 runner 状态（globalThis.__vitest_worker__ +
 *       run chunk 模块级的 defaultSuite/collectorContext）；
 *     - 测试文件内 `import ... from "vitest"` 经 vite 解析器拿到**大写** `J:`
 *       URL（realpath 按磁盘实际大小写规范化）→ 同一份 chunk 以第二身份再次
 *       加载，其模块级 `defaultSuite` 未初始化 → `describe(...)` 一调用即抛
 *       `Vitest failed to find the current suite` / `Cannot read properties
 *       of undefined (reading 'config')`，全库测试文件收集期全灭。
 *   实证：同一命令从大写 `J:\` cwd 启动即全部通过。
 *
 * 修复：本启动器把 cwd 规范化（realpath + 盘符强制大写）后，
 * 以**同一规范化路径**启动 vitest.mjs —— 引导与测试文件的模块身份恒一致。
 *
 * 用法（package.json 已接线）：
 *   npm test            → node scripts/run-vitest.mjs run
 *   npm run test:watch  → node scripts/run-vitest.mjs
 * 额外参数原样透传：npm test -- src/utils.export.test.ts
 */
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { createRequire } from "node:module";

function canonicalize(p) {
  let real = p;
  try {
    real = fs.realpathSync(p);
  } catch {
    // 目录可能刚创建，回退原路径
  }
  // Windows 盘符强制大写；非 Windows 原样返回
  return real.replace(/^([a-z]):/, (_m, d) => `${d.toUpperCase()}:`);
}

// 项目根 = 本脚本所在目录的上一级（与 cwd 无关，任意位置启动均可）
const projectRoot = canonicalize(path.resolve(import.meta.dirname ?? ".", ".."));
const requireFromRoot = createRequire(path.join(projectRoot, "package.json"));

// 定位 vitest 的 bin 入口（与 .bin/vitest 解析到同一份物理文件）
const vitestPkgDir = path.dirname(requireFromRoot.resolve("vitest/package.json"));
const vitestEntry = path.join(canonicalize(vitestPkgDir), "vitest.mjs");
if (!fs.existsSync(vitestEntry)) {
  console.error(`[run-vitest] 未找到 vitest 入口: ${vitestEntry}`);
  process.exit(1);
}

const args = process.argv.slice(2);
const result = spawnSync(process.execPath, [vitestEntry, ...args], {
  cwd: projectRoot,
  stdio: "inherit",
  env: {
    ...process.env,
    // 双保险：子进程内再取 cwd 时也是规范形态
    PWD: projectRoot,
  },
});
process.exit(result.status ?? 1);
