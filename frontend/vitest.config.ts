import path from "node:path";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// 遗留 L3 修复（2026-10-04）：前端编号纯函数单测基建。
// BUG-FIX 2026-10-06：此前本配置没有 "@" 别名，也没有 react 插件 ——
// 组件级 DOM 测试（FileUploadPage.dom.test.tsx）加载页面组件时，
// `@/components/PageHeader` 无法解析，组件级交互测试恒失败
// （tsc 走 tsconfig.app.json 的 paths 所以编译期看不出来）。
// 各测试文件仍用文件头 `// @vitest-environment` 自行声明运行环境。
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "src"),
    },
  },
  test: {
    environment: "node",
    include: ["src/**/*.test.ts", "src/**/*.test.tsx"],
    setupFiles: ["./src/setupTests.ts"],
    // BUG-FIX 2026-10-13（消除 DOM 测试 flaky）：此前未设 `testTimeout`，走 vitest
    // 默认的 **5000ms**。而组件级交互测试要真渲染 antd（Table/Modal/Tabs 在 jsdom
    // 下单次渲染就要数百毫秒），再叠加 `waitFor` 的异步重试与 26 个测试文件并发
    // 抢 CPU —— 单跑绿的用例在全量跑时稳定超时。实测：
    //   FactBackflowTab「采纳按钮在无选中时禁用」单跑 2.9s、并发时 >5s；
    //   OutlineEditorPage 两个用例同样在全量下超时。
    // 这类失败与被测代码无关（纯环境耗时），但会淹没真实回归、并让 CI 变成红灯
    // 噪音。此前只能逐个用例传 `{ timeout }` 打补丁，既治标又很快会漏。
    //
    // HARDENING 2026-10-16（消除冷启动假失败）：上一条把全局提到 20000ms，但
    // 组件级用例仍在文件里逐个写 `{ timeout: 120_000 }` 覆盖全局值。实测本机
    // vitest **冷缓存**（新克隆 / CI 无缓存）首次 transform antd+jsdom 依赖，
    // 每个测试文件的**首个用例**要 260s+ 才能开始执行 —— 超过 120s 用例级
    // timeout，产生一批与被测代码无关的假失败（本轮实测 7 个）。且用例级
    // 覆盖让新写的 DOM 测试一旦忘记加大 timeout 就必假失败。
    // 根治：全局一次到位，**删除各处用例级 120_000 覆盖**，由本配置统一兜底。
    //
    // 取 300000ms（5 分钟）：覆盖冷启动实测极值 266s 仍有余量；纯函数/热缓存
    // 场景下用例毫秒级结束，不受影响。真挂起时它照样会超时失败 —— 不会掩盖
    // 真实缺陷，只是不再因环境耗时误报。
    testTimeout: 300000,
    hookTimeout: 60000,
    // HARDENING 2026-10-16b（消除全量并发 flaky）：测试文件增至 33 个后，vitest
    // 默认按逻辑核数（本机 8）开满 worker，7 个 antd + jsdom 同时渲染把 CPU/内存
    // 打满 —— 实测全量跑有 4 个用例因 waitFor 超时假失败（WordCountPage 161s、
    // SummaryReviewPage.facts 138s 逼近 timeout；单独重跑 6/6 全过，确认与代码
    // 无关）。限制到 4 worker（物理核数）从根因降低争抢。
    // BUG-FIX 2026-10-17（消除 OutlineEditorPage 全量假失败）：此前 maxThreads=4
    // 时，antd+jsdom 的 9 个 DOM 测试文件同时渲染仍把 CPU/内存打满 ——
    // OutlineEditorPage.dom.test.tsx 在全量并发时需 191s（单跑 74s），其 setup
    // 阶段被并行争抢拖到超时，导致 8 tests 全部 skip + 文件级 ❯ 标记失败。
    // 单独运行 8/8 通过，确认非代码缺陷。现在降到 2 worker（物理核数/2）从根因
    // 消除争抢；retry 升到 2 给偶发一次额外重试。纯函数用例不受影响（毫秒级）。
    poolOptions: {
      threads: {
        maxThreads: 2,
      },
    },
    // 三保险：retry=2（最多重试 2 次），确定性回归重试后仍失败、照常红灯。
    retry: 2,
  },
});
