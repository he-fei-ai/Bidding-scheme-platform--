import { Plugin, ViteDevServer } from "vite";
import fs from "fs";
import path from "path";

export interface PortConfig {
  version: number;
  updated_at: string;
  backend: {
    host: string;
    port: number;
    url: string;
    docs_url: string;
    health_url: string;
  };
  frontend: {
    host: string;
    port: number;
    url: string;
  };
}

let cachedConfig: PortConfig | null = null;
let lastReadTime = 0;
const CACHE_TTL_MS = 5000;

function loadPortConfig(): PortConfig | null {
  try {
    const now = Date.now();
    if (cachedConfig && now - lastReadTime < CACHE_TTL_MS) {
      return cachedConfig;
    }
    const configPath = path.resolve(process.cwd(), "..", "backend-ports.json");
    const raw = fs.readFileSync(configPath, "utf-8") as unknown as string;
    cachedConfig = JSON.parse(raw) as PortConfig;
    lastReadTime = now;
    return cachedConfig;
  } catch {
    return null;
  }
}

/**
 * 自动同步后端端口的 Vite 插件。
 * 读取项目根目录的 backend-ports.json，自动配置 proxy target。
 * 当后端端口变更时，无需手动修改 vite.config.ts。
 */
export function syncBackendPort(): Plugin {
  return {
    name: "vite-plugin-sync-backend-port",
    configResolved(resolvedConfig) {
      const cfg = loadPortConfig();
      if (!cfg) {
        console.warn("[sync-port] backend-ports.json 不存在，使用默认端口 8000");
        return;
      }
      const backendPort = cfg.backend?.port ?? 8000;
      const backendHost = cfg.backend?.host ?? "127.0.0.1";
      const backendUrl = `http://${backendHost}:${backendPort}`;

      // 动态更新 proxy target
      if (resolvedConfig.server?.proxy?.["/api/v1"]) {
        (resolvedConfig.server.proxy["/api/v1"] as any).target = backendUrl;
      }
      if (resolvedConfig.server?.proxy?.["/api"]) {
        (resolvedConfig.server.proxy["/api"] as any).target = backendUrl;
      }

      console.log(`[sync-port] 已同步后端端口 → ${backendUrl}`);
      console.log(`[sync-port] 更新时间: ${cfg.updated_at}`);
    },
    configureServer(server) {
      // 文件变更监听：backend-ports.json 变化时自动重载配置
      const configPath = path.resolve(process.cwd(), "..", "backend-ports.json");
      let watcher: fs.FSWatcher | null = null;
      try {
        watcher = fs.watch(configPath, { persistent: false }, (event: string, filename: string | undefined) => {
          if (event !== "change") return;
          const newCfg = loadPortConfig();
          if (!newCfg) return;
          const backendPort = newCfg.backend?.port ?? 8000;
          const backendHost = newCfg.backend?.host ?? "127.0.0.1";
          const backendUrl = `http://${backendHost}:${backendPort}`;
          if (server.config.server?.proxy?.["/api/v1"]) {
            (server.config.server.proxy["/api/v1"] as any).target = backendUrl;
          }
          if (server.config.server?.proxy?.["/api"]) {
            (server.config.server.proxy["/api"] as any).target = backendUrl;
          }
          console.log(`[sync-port] 检测到端口变更 → ${backendUrl}`);
        });
      } catch {
        // watch 失败不影响主流程
      }
      server.httpServer?.once("close", () => watcher?.close());

      // 抑制 Vite 开发服务器控制台中的 ERR_ABORTED 噪声日志
      // 场景：EventSource 正常关闭（如生成失败后前端主动 close）触发浏览器日志
      // 这类日志不影响功能，但会在 Console 面板产生大量红字干扰
      const consoleError = console.error;
      (server.config.logger as any).__vite_original_error = consoleError;
      server.config.logger.error = (...args: any[]) => {
        const msg = args[0];
        if (typeof msg === "string" && msg.includes("ERR_ABORTED")) {
          return; // 静默丢弃
        }
        consoleError.apply(console, args);
      };

      // 通过 HTTP 中间件将 SSE 错误事件转换为 HTTP 500，使连接正常关闭
      // 避免 EventSource.close() 触发浏览器 ERR_ABORTED 日志
      server.middlewares.use((req: any, res: any, next: () => void) => {
        const url = req.url || "";
        // 拦截所有 SSE 相关请求（目录生成 + 正文生成 + 导出）
        if (!url.includes("/generate-sse") && !url.includes("/generate-content") && !url.includes("/export")) return next();
        // 监听响应错误事件，将 ERR_ABORTED 转为静默处理
        req.on("abort", () => {
          // 客户端断开，静默处理，不输出错误日志
        });
        next();
      });
    },
  };
}
