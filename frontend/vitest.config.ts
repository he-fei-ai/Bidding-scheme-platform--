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
  },
});
