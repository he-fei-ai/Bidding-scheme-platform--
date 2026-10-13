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
    // 取 20000ms：对 jsdom + antd 的组件测试而言仍远大于真实耗时（最慢的用例
    // 约 3s），因此**不会**掩盖真正的挂起/死等——真挂起时它照样会超时失败，
    // 只是不再因 CPU 争用误报。
    testTimeout: 20000,
    hookTimeout: 20000,
  },
});
