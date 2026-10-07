import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";
import { syncBackendPort } from "./src/vite-plugins/sync-backend-port";

export default defineConfig(({ mode }) => {
  // 显式加载 .env.development / .env.production 等，确保 VITE_* 变量注入 import.meta.env
  const env = loadEnv(mode, process.cwd(), "");
  const SSE_BASE = env.VITE_API_BASE_URL || "";
  const BACKEND_PORT = env.BID_ASSISTANT_PORT || "8000";
  const BACKEND_TARGET = `http://127.0.0.1:${BACKEND_PORT}`;
  console.log(`[Vite] mode=${mode}, VITE_API_BASE_URL=${SSE_BASE || "(not set, will use proxy)"}, backend=${BACKEND_TARGET}`);

  return {
    plugins: [react(), syncBackendPort()],
    resolve: {
      alias: {
        "@": "/src",
      },
    },
    server: {
      host: "0.0.0.0",
      port: 5175,
      proxy: {
        "/api/v1": {
          target: BACKEND_TARGET,
          changeOrigin: true,
          ws: true,
          timeout: 3600000,
          proxyTimeout: 3600000,
          // 关键修复：确保 SSE 流式响应不被 Vite 代理缓冲
          configure: (proxy) => {
            // 所有 SSE 端点的路径关键字
            const SSE_KEYWORDS = ["/generate-sse", "/generate-content", "/expand-content", "/export/docx-stream"];
            const isSSE = (url?: string) =>
              !!url && SSE_KEYWORDS.some((k) => url.includes(k));

            // 请求级：为 SSE 请求禁用代理缓冲
            proxy.on("proxyReq", (proxyReq, req) => {
              if (isSSE(req.url)) {
                proxyReq.setHeader("Accept-Encoding", "identity");
                proxyReq.setHeader("X-Accel-Buffering", "no");
                proxyReq.setHeader("Connection", "keep-alive");
                proxyReq.on("error", () => {});
              }
            });

            // 响应级：确保 SSE 响应头正确
            proxy.on("proxyRes", (proxyRes, req) => {
              if (proxyRes.headers["content-type"]?.includes("text/event-stream")) {
                delete proxyRes.headers["content-length"];
                delete proxyRes.headers["content-encoding"];
                proxyRes.headers["cache-control"] = "no-cache, no-transform";
                proxyRes.headers["connection"] = "keep-alive";
                proxyRes.headers["x-accel-buffering"] = "no";
                proxyRes.headers["accept-ranges"] = "no";
              }
              if (isSSE(req.url)) {
                proxyRes.on("error", () => {});
              }
            });

            // 代理错误级：处理连接被拒等错误
            proxy.on("proxyError", (err, req, res) => {
              if (req.url?.includes("/health")) {
                // 健康检查静默处理
              } else if (isSSE(req.url)) {
                try { res.end(); } catch {}
              } else {
                console.error("[Vite Proxy Error]", err.message);
              }
            });
          },
        },
      },
    },
  };
});
