import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import { ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import App from "./App";
import { syncWordCountTiers } from "./utils";
import { brandTheme } from "./styles/theme";
import "./styles/global.css";

// === Unhandled Rejection Handler ===
window.addEventListener("unhandledrejection", (event) => {
  const reason = event.reason;
  const message = reason?.message || String(reason);
  // Suppress known benign cancellations (e.g. AbortController abort)
  if (reason?.name === "AbortError") return;
  console.error("[UnhandledRejection]", message);
  event.preventDefault();
});

// === Global Error Handler ===
window.addEventListener("error", (event) => {
  // Suppress known benign errors (e.g. image load failures, script errors from 3rd party)
  if (event.colno === 0 && event.lineno === 0) return;
  const msg = event.message;
  if (msg.includes("Script error.") || msg.includes("Adjust chunk size limit")) return;
  console.error("[GlobalError]", msg, "at", event.filename, ":", event.lineno);
  event.preventDefault();
});

// 禁用 StrictMode：开发模式下 React 18 会对组件执行 mount → unmount → mount 三连，
// 导致 SSE 的 AbortController 被 cleanup 提前触发，浏览器报 net::ERR_ABORTED。
// 生产环境 StrictMode 仅一次渲染，无此问题。
// G7（2026-10-28 遗留加固）：启动即从后端 /word-count/tiers 同步字数档位
// （DB 优先，运维可改）。fail-soft：失败保留静态默认，不阻塞页面渲染。
syncWordCountTiers().catch(() => undefined);

ReactDOM.createRoot(document.getElementById("root")!).render(
  // <React.StrictMode>
    <ConfigProvider locale={zhCN} theme={brandTheme}>
      <BrowserRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
        <App />
      </BrowserRouter>
    </ConfigProvider>
  // </React.StrictMode>
);
