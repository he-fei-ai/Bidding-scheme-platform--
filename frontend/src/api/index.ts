import axios from "axios";
import { message } from "antd";
import type {
  Project, ProjectCreate, ProjectUpdate, ProjectList,
  FileInfo, AnalysisResult, UploadResult, FileParsingStatus,
  SectionTreeNode, Section, SectionCreate, SectionUpdate,
  ExportConfig, DocumentCheckResult, ExportRecord, AIConfig, KnownProvider, PromptTemplate,
  PromptVersionMeta, PromptVersionDetail,
  ExportPresetCatalog,
  RejectionStartRequest, RejectionCheckTask, RejectionCheckItem,
  DuplicateStartRequest, DuplicateCheckTask,
  KnowledgeDocument, KnowledgeItem, KnowledgeItemUpdate, KnowledgeSearchResult, KnowledgeStats,
  IndustryNode, IndustryNodeCreate, IndustryNodeUpdate,
  GenerateChildrenRequest, MatchIndustryResult, MatchIndustryAIResult, IndustryStats,
  IndustryReference, AuditLog, OutlineReviewResult, ScoringItem,
  ChartType, ValidationResult,
  AIPProfile, UsageSummary, HealthProvider, DailyTrend, BudgetStatus,
  TextProviderConfig, ImageProviderConfig,
} from "../types";

// ========== 认证 Token 管理 ==========
// BUG-FIX-24 修复：前端未注入 Authorization 头，AUTH_ENABLED=True 时全面 401。
// 提供 token 存取 + 请求拦截器自动注入 + SSE URL 追加 token 查询参数。
const _AUTH_TOKEN_KEY = "auth_token";

export function getAuthToken(): string | null {
  return localStorage.getItem(_AUTH_TOKEN_KEY);
}

export function setAuthToken(token: string): void {
  localStorage.setItem(_AUTH_TOKEN_KEY, token);
}

export function clearAuthToken(): void {
  localStorage.removeItem(_AUTH_TOKEN_KEY);
}

export function isAuthenticated(): boolean {
  return !!getAuthToken();
}

export interface LoginResult {
  access_token: string;
  token_type: string;
  expires_in: number;
  user: { id: number; username: string; role: string };
}

/** 登录并保存 token（BUG-FIX 2026-09-16：配合 LoginPage / ProtectedRoute 使用）。
 *  silent 配置避免错误凭据的 401 触发全局「重定向登录页」逻辑（本来就在登录页）。
 */
export async function login(username: string, password: string): Promise<LoginResult> {
  const resp = await http.post<LoginResult>("/auth/login", { username, password }, { silent: true } as any);
  setAuthToken(resp.data.access_token);
  return resp.data;
}

/** 退出登录：清除本地 token（后端 JWT 无需服务端注销） */
export function logout(): void {
  clearAuthToken();
}

// ========== 断线重连 & 自动重试机制 ==========
let _isServerReachable = true;
let _reconnectTimer: ReturnType<typeof setTimeout> | null = null;
let _retryQueue: Array<{ config: any; resolve: (v: any) => void; reject: (e: any) => void }> = [];
const MAX_RETRY_COUNT = 3;
const RETRY_BASE_DELAY = 2000;

// 401 → 登录页重定向去重标志（并行请求同时 401 时只跳转一次；整页跳转后模块重载自动复位）
let _redirectingToLogin = false;

export async function healthCheck(): Promise<void> {
  // BUG-FIX 2026-09-16（低）：裸 fetch 无超时——后端 TCP 挂起（非拒绝）时
  // fetch 可悬挂数分钟，健康状态永远停在旧值。加 5s 超时兜底。
  const res = await fetch("/api/v1/health", { signal: AbortSignal.timeout(5000) });
  if (!res.ok) throw new Error("health check failed");
}

function checkServerReachable() {
  fetch("/api/v1/health")
    .then(() => {
      if (!_isServerReachable) {
        _isServerReachable = true;
        console.log("[网络] 后端已恢复，处理排队请求");
        message.success("后端服务已恢复");
        _processRetryQueue();
      }
    })
    .catch(() => {
      _isServerReachable = false;
    });
}

function scheduleReconnectCheck() {
  if (_reconnectTimer) return;
  _reconnectTimer = setTimeout(() => {
    _reconnectTimer = null;
    checkServerReachable();
    if (!_isServerReachable) {
      scheduleReconnectCheck();
    }
  }, 5000);
}

function _processRetryQueue() {
  const queue = [..._retryQueue];
  _retryQueue = [];
  queue.forEach(({ config, resolve, reject }) => {
    axios(config).then(resolve).catch(reject);
  });
}

function enqueueRetry(config: any, resolve: (v: any) => void, reject: (e: any) => void) {
  _retryQueue.push({ config, resolve, reject });
}

function retryWithBackoff(originalConfig: any, attempt: number, resolve: (v: any) => void, reject: (e: any) => void): void {
  if (attempt >= MAX_RETRY_COUNT) {
    reject(new Error("服务暂时不可用，请稍后重试"));
    return;
  }
  const delay = RETRY_BASE_DELAY * Math.pow(2, attempt);
  console.log(`[网络] 后端请求失败，${delay}ms 后重试 (第 ${attempt + 1}/${MAX_RETRY_COUNT} 次)`);
  setTimeout(() => {
    axios(originalConfig)
      .then(resolve)
      .catch((e: any) => {
        // BUG-F-06 修复：ECONNABORTED 也加入重试
        // 原代码仅对 500/ECONNREFUSED/ERR_NETWORK 重试，超时会直接 reject
        // BUG-FIX 2026-09-16（中）：只自动重试幂等的 GET 请求。
        // POST（启动生成/导出/查重等任务型接口）超时（ECONNABORTED）重发时
        // 后端可能已收到并开始执行，重试会创建第二个任务，409 冲突弹窗反而误导用户。
        const method = (originalConfig?.method || "get").toLowerCase();
        if (
          method === "get" &&
          (e?.response?.status === 500 || e?.code === "ECONNREFUSED" || e?.code === "ERR_NETWORK" || e?.code === "ECONNABORTED")
        ) {
          retryWithBackoff(originalConfig, attempt + 1, resolve, reject);
        } else {
          reject(e);
        }
      });
  }, delay);
}

function _extractErrorDetail(data: unknown, fallback: string): string {
  if (!data) return fallback;
  const detail = (data as Record<string, unknown>)?.detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    const parts = detail.map((item: Record<string, unknown>) => {
      const loc = Array.isArray(item.loc) ? item.loc.join(".") : "";
      return loc ? `${loc}: ${item.msg}` : String(item.msg);
    });
    return parts.join("; ") || fallback;
  }
  if (typeof detail === "object" && detail !== null) {
    return JSON.stringify(detail);
  }
  return fallback;
}

// 创建带错误处理的 axios 实例
// baseURL 使用相对路径，由 Vite 代理到后端（同源部署或开发态）
// BUG-F-06 修复：超时从 30s 增加到 60s，避免 AI 生成期间普通请求超时
// 后端 AI 模型加载 + 章节生成期间事件循环繁忙，30s 不足以覆盖
export const http = axios.create({
  baseURL: "/api/v1",
  timeout: 60000,
});

// SSE 专用：绕过 Vite 代理，直连后端（避免代理缓冲导致 ERR_ABORTED）
// 生产部署时通过 VITE_API_BASE_URL 注入后端地址
const SSE_BASE_URL = import.meta.env.VITE_API_BASE_URL || "";

/** 将 /api/v1/... 相对路径拼接为 SSE 直连完整 URL（绕过 Vite 代理缓冲）。
 *  BUG-FIX-24 修复：EventSource 不支持自定义头，通过 URL 查询参数传递 token。
 */
function toSSEFullUrl(path: string): string {
  const token = getAuthToken();
  const separator = path.includes("?") ? "&" : "?";
  const authParam = token ? `${separator}token=${encodeURIComponent(token)}` : "";
  if (SSE_BASE_URL) {
    return `${SSE_BASE_URL}${path}${authParam}`;
  }
  return `${path}${authParam}`;
}

/** 目录生成模式（易标规格二.2，ENHANCE 2026-10-05 / P-4）：
 *  auto      —— 后端按评分规模自动判别直出/分步（默认，与旧版行为一致）
 *  direct    —— 强制短标书一次性直出路径
 *  stepwise  —— 强制分步渐进生成路径
 */
export type OutlineMode = "auto" | "direct" | "stepwise";

/// CROSS-FIX 2026-10-04（前后端同步）：后端 /generate-sse 已支持 outline_mode
/// 查询参数，前端补齐透传链路；默认 auto 保持旧行为，完全向后兼容。
const OUTLINE_MODE_STORAGE_KEY = "outline_generation_mode";

export function getStoredOutlineMode(): OutlineMode {
  try {
    const v = localStorage.getItem(OUTLINE_MODE_STORAGE_KEY);
    return v === "direct" || v === "stepwise" ? v : "auto";
  } catch {
    return "auto";
  }
}

export function storeOutlineMode(mode: OutlineMode): void {
  try {
    localStorage.setItem(OUTLINE_MODE_STORAGE_KEY, mode);
  } catch {
    // localStorage 不可用（隐私模式等）时静默降级为不持久化
  }
}

export function generateOutlineSSEUrl(
  projectId: number,
  strategy: string = "v3",
  outlineMode: OutlineMode = "auto",
): string {
  const path = `/api/v1/projects/${projectId}/sections/generate-sse?strategy=${strategy}&outline_mode=${outlineMode}`;
  return toSSEFullUrl(path);
}

export function httpPostLong<T>(url: string, data?: unknown, config?: Record<string, unknown>): Promise<{ data: T }> {
  return http.post<T>(url, data, { timeout: 600000, ...config });
}

// 请求拦截器：添加认证头（如有）
http.interceptors.request.use(
  (config) => {
    // BUG-FIX-24 修复：自动注入 Authorization: Bearer <token>
    const token = getAuthToken();
    if (token) {
      config.headers = config.headers || {};
      config.headers.Authorization = `Bearer ${token}`;
    }
    return config;
  },
  (error) => {
    console.error("[API] 请求拦截错误:", error?.message);
    return Promise.reject(error);
  }
);

// 响应拦截器：统一错误处理 + 自动重试
http.interceptors.response.use(
  (response) => {
    return response;
  },
  (error) => {
    const originalConfig = error.config;

    if (error.response) {
      const { status, data } = error.response;
      const detail = _extractErrorDetail(data, error.message);
      if (status === 401) {
        console.error("未授权，请重新登录或配置 API Key");
        // BUG-FIX-24 修复：401 时清除失效 token
        clearAuthToken();
        // BUG-FIX 2026-09-16（中）：登录态统一处理。原实现只弹 toast，
        // 用户停留在当前页，后续每个请求继续 401（并行请求还会重复弹 toast）。
        // 现在去重提示 + 重定向登录页并携带来源路径；silent 请求（如
        // ProtectedRoute 的认证探测、登录接口本身）不触发跳转，由调用方处理。
        if (!originalConfig?.silent && !_redirectingToLogin) {
          _redirectingToLogin = true;
          message.error("登录状态已失效，请重新登录");
          const from = encodeURIComponent(window.location.pathname + window.location.search);
          window.location.assign(`/login?from=${from}`);
        }
      } else if (status === 403) {
        console.error("禁止访问，权限不足");
        message.error("禁止访问，权限不足");
      } else if (status === 409) {
        // BUG-FIX-54 修复：原未处理409状态码，用户看到默认错误提示而非友好消息
        console.error("资源冲突:", detail);
        message.error(detail || "操作冲突，请稍候再试");
      } else if (status === 404) {
        console.error("[静默] 请求的资源不存在:", detail);
        if (!originalConfig?.silent) {
          message.error(detail || "请求的资源不存在");
        }
      } else if (status >= 500) {
        console.error("服务器错误:", detail, "| URL:", originalConfig?.url, "| Method:", originalConfig?.method);
        message.error(`服务器错误: ${detail}`);
        if (!_isServerReachable) {
          scheduleReconnectCheck();
        }
      } else {
        console.error("请求失败:", detail);
        message.error(`请求失败: ${detail}`);
      }
    } else if (error.code === "ECONNABORTED") {
      // BUG-F-06 修复：超时不再误报为"请求超时"并触发恐慌
      // 场景：AI 生成期间后端事件循环繁忙，普通请求可能耗时较长
      // 改为静默重试（限非关键路径），不弹窗打扰用户
      const retryable = !originalConfig?.silent && !originalConfig?.skipRetry;
      if (retryable) {
        console.warn("[超时] 请求超时，将自动重试:", originalConfig?.url);
        return new Promise((resolve, reject) => {
          retryWithBackoff(originalConfig, 0, resolve, reject);
        });
      } else {
        console.warn("[超时] 请求超时（静默模式）:", originalConfig?.url);
        return Promise.reject(error);
      }
    } else if (error.code === "ECONNREFUSED" || error.code === "ERR_NETWORK") {
      console.warn("[网络] 后端连接被拒绝，启动自动重试机制");
      _isServerReachable = false;
      if (!window.navigator.onLine) {
        message.warning("网络已断开，正在等待后端恢复...");
      } else {
        message.warning("后端服务暂不可用，正在尝试重连...");
        scheduleReconnectCheck();
      }
      return new Promise((resolve, reject) => {
        retryWithBackoff(originalConfig, 0, resolve, reject);
      });
    } else if (error.code === "ERR_CANCELED" || error.code === "CANCELED" || (error.message || "") === "canceled") {
      return Promise.reject(error);
    } else if (!window.navigator.onLine) {
      console.error("网络已断开，请检查网络连接");
      message.error("网络已断开，请检查网络连接");
    } else {
      console.error("未知错误:", error.message);
      message.error(`未知错误: ${error.message}`);
    }
    return Promise.reject(error);
  }
);

// 页面可见性变化时触发健康检查
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible" && !_isServerReachable) {
    console.log("[网络] 页面重新可见，检查后端状态");
    checkServerReachable();
  }
});

// 页面加载时检查后端状态
if (!window.navigator.onLine) {
  _isServerReachable = false;
  message.warning("检测到网络断开，正在等待后端恢复...");
}
window.addEventListener("online", () => {
  _isServerReachable = false;
  checkServerReachable();
});

// Projects
export async function fetchProjects(): Promise<ProjectList> {
  const res = await http.get<ProjectList>("/projects");
  return res.data;
}

// ========== 行业树模块 ==========

export async function getIndustryTree(active_only: boolean = true): Promise<IndustryNode[]> {
  const res = await http.get("/industry-tree/tree", { params: { active_only } });
  return res.data;
}

export async function getIndustryRoots(active_only: boolean = true): Promise<IndustryNode[]> {
  const res = await http.get("/industry-tree/roots", { params: { active_only } });
  return res.data;
}

export async function getIndustryChildren(node_id: string, active_only: boolean = true): Promise<IndustryNode[]> {
  const res = await http.get(`/industry-tree/nodes/${node_id}/children`, { params: { active_only } });
  return res.data;
}

export async function getIndustryNode(node_id: string): Promise<IndustryNode> {
  const res = await http.get(`/industry-tree/nodes/${node_id}`);
  return res.data;
}

export async function getIndustryPath(node_id: string): Promise<string[]> {
  const res = await http.get(`/industry-tree/nodes/${node_id}/path`);
  return res.data;
}

export async function searchIndustryNodes(keyword: string, level?: number, limit: number = 50): Promise<IndustryNode[]> {
  const res = await http.get("/industry-tree/search", { params: { keyword, level, limit } });
  return res.data;
}

export async function matchIndustryByText(text: string): Promise<MatchIndustryResult[]> {
  const res = await http.post("/industry-tree/match", { text });
  return res.data;
}

export async function createIndustryNode(req: IndustryNodeCreate): Promise<IndustryNode> {
  const res = await http.post("/industry-tree/nodes", req);
  return res.data;
}

export async function updateIndustryNode(node_id: string, req: IndustryNodeUpdate): Promise<IndustryNode> {
  const res = await http.put(`/industry-tree/nodes/${node_id}`, req);
  return res.data;
}

export async function deleteIndustryNode(node_id: string, force: boolean = false): Promise<void> {
  await http.delete(`/industry-tree/nodes/${node_id}`, { params: { force } });
}

export async function generateIndustryChildren(node_id: string, req: GenerateChildrenRequest = {}): Promise<{
  nodes: IndustryNode[]; count: number;
}> {
  const res = await http.post(`/industry-tree/nodes/${node_id}/generate-children`, req);
  return res.data;
}

export async function batchImportIndustryNodes(nodes: IndustryNodeCreate[]): Promise<{ imported: number }> {
  const res = await http.post("/industry-tree/batch-import", { nodes });
  return res.data;
}

export async function reorderIndustryNodes(ordered_ids: string[]): Promise<void> {
  await http.post("/industry-tree/reorder", { ordered_ids });
}

export async function getIndustryTreeStats(): Promise<IndustryStats> {
  const res = await http.get("/industry-tree/stats");
  return res.data;
}

export async function linkKnowledgeToIndustry(doc_id: number, industry_id: string): Promise<{ message: string }> {
  const res = await http.post("/industry-tree/link-knowledge", null, {
    params: { doc_id, industry_id },
  });
  return res.data;
}

export async function getIndustryKnowledgeMap(): Promise<Record<string, string[]>> {
  const res = await http.get("/industry-tree/knowledge-map");
  return res.data;
}

export async function getIndustryReferences(node_id: string): Promise<IndustryReference[]> {
  const res = await http.get(`/industry-tree/references/${node_id}`);
  return res.data;
}

export async function getIndustryScoringItems(node_id: string): Promise<{ scoring_items: ScoringItem[] }> {
  const res = await http.get(`/industry-tree/scoring-items/${node_id}`);
  return res.data;
}

export async function matchIndustryByAI(text: string): Promise<MatchIndustryAIResult[]> {
  const res = await http.post("/industry-tree/match-ai", { text });
  return res.data;
}

export async function getAuditLogs(node_id?: string, limit: number = 50): Promise<AuditLog[]> {
  const res = await http.get("/industry-tree/audit-logs", { params: { node_id, limit } });
  return res.data;
}

export async function createVersion(changelog: string = ""): Promise<{ version_number: number; message: string }> {
  const res = await http.post("/industry-tree/create-version", null, { params: { changelog } });
  return res.data;
}

export async function rollbackToVersion(version_num: number): Promise<{ message: string }> {
  const res = await http.post(`/industry-tree/rollback/${version_num}`);
  return res.data;
}

export async function getVersions(): Promise<any[]> {
  const res = await http.get("/industry-tree/versions");
  return res.data;
}

export async function createProject(data: ProjectCreate): Promise<{ id: number }> {
  const res = await http.post<{ id: number }>("/projects", data);
  return res.data;
}

export async function getProject(id: number, options?: { silent?: boolean }): Promise<Project> {
  const res = await http.get<Project>(`/projects/${id}`, options as any);
  return res.data;
}

export async function updateProject(id: number, data: ProjectUpdate): Promise<Project> {
  const res = await http.patch<Project>(`/projects/${id}`, data);
  return res.data;
}

export async function deleteProject(id: number): Promise<void> {
  await http.delete(`/projects/${id}`);
}

// Usage
export interface UsageInfo {
  total_limit: number;
  used_count: number;
  remaining: number;
  consumed_project_ids: number[];
  project_consumed?: boolean;
}

export async function getUsage(projectId?: number): Promise<UsageInfo> {
  const url = projectId !== undefined ? `/usage/${projectId}` : "/usage";
  const res = await http.get<UsageInfo>(url);
  return res.data;
}

export async function resetUsage(): Promise<{ ok: boolean; message: string }> {
  const res = await http.post("/usage/reset");
  return res.data;
}

// Files
export async function uploadFile(
  projectId: number,
  file: File,
  fileRole: string = "",
): Promise<UploadResult> {
  const formData = new FormData();
  formData.append("file", file);
  if (fileRole) formData.append("file_role", fileRole);
  const res = await http.post<UploadResult>(`/projects/${projectId}/files`, formData);
  return res.data;
}

export async function fetchFiles(projectId: number): Promise<FileInfo[]> {
  const res = await http.get<FileInfo[]>(`/projects/${projectId}/files`);
  return res.data;
}

export async function getFileText(projectId: number, fileId?: number): Promise<string> {
  const path = fileId
    ? `/projects/${projectId}/files/${fileId}/text`
    : `/projects/${projectId}/files/text`;
  const res = await http.get<{ text: string }>(path);
  return res.data.text;
}

export async function deleteFile(projectId: number, fileId: number): Promise<{
  status: string; file_id: number; promoted_file_id?: number | null;
}> {
  const res = await http.delete(`/projects/${projectId}/files/${fileId}`);
  return res.data;
}

// ENHANCE 2026-10-05（G8 数据链闭环）：项目级解析任务状态聚合
// （GET /tasks/files/status，后端 2026-10-04 新增，此前前端无任何消费方）
export async function fetchFileParsingStatus(
  projectId: number,
): Promise<FileParsingStatus> {
  const res = await http.get<FileParsingStatus>(
    `/projects/${projectId}/tasks/files/status`,
  );
  return res.data;
}

export async function reExtractFileText(projectId: number, fileId: number): Promise<{
  file_id: number; filename: string; status: string; message: string; text_length: number;
}> {
  const res = await http.post(`/projects/${projectId}/files/${fileId}/re-extract`);
  return res.data;
}

// ENHANCE 2026-10-04（P4）：手动切换文件主/辅助角色（影响下游主文件选择）
// CROSS-FIX 2026-10-04：main 角色全项目互斥，后端自动将其余 main 降为
// supporting，并通过 demoted_file_ids 透出（旧后端无此字段，可选兼容）
// REVIEW-FIX 2026-10-04（m2）：needs_reanalysis —— 主文件换源且已有缓存摘要/
// 评分时提醒重新「提取项目」；降级唯一 main 后端返回 422（detail 透出到 UI）
// 遗留修复 2026-10-06（L2）：Celery 任务 tasks.file.parse_and_extract 的正式入口。
// 此前该任务只被注册、从不被派发（死链路）。用于耗时解析（扫描件 OCR / 大 PDF）。
export async function asyncReparseFile(
  projectId: number,
  fileId: number,
): Promise<{
  file_id: number;
  filename: string;
  dispatched: string;
  dispatched_via: string;
  celery_task_id?: string | null;
  detail?: unknown;
}> {
  const res = await http.post(`/projects/${projectId}/files/${fileId}/async-reparse`);
  return res.data;
}

export async function updateFileRole(projectId: number, fileId: number, fileRole: "main" | "supporting"): Promise<{
  status: string; file_id: number; filename: string; file_role: string;
  demoted_file_ids?: number[]; needs_reanalysis?: boolean;
}> {
  const res = await http.put(`/projects/${projectId}/files/${fileId}/role`, { file_role: fileRole });
  return res.data;
}

// Gap-1 2026-10-11：清空项目所有文件的已提取文本并重置状态
// ⚠️ 不删除已上传的原始文件，只清空提取文本 + 重置文件状态为 pending + 清理解析任务终态行
export async function clearProjectTexts(projectId: number): Promise<{
  status: "cleared";
  files_reset: number;
  tasks_cleared: number;
}> {
  const res = await http.delete(`/projects/${projectId}/files/texts`);
  return res.data;
}

// AI Analysis
export async function getAnalysis(projectId: number): Promise<AnalysisResult> {
  const res = await http.get<AnalysisResult>(`/projects/${projectId}/ai/analysis`);
  return res.data;
}

export async function saveProjectScope(projectId: number, scope: string): Promise<void> {
  await http.patch(`/projects/${projectId}/ai/analysis/scope`, { scope });
}

export async function generateSummary(projectId: number): Promise<{ summary_json: string }> {
  const res = await httpPostLong<{ summary_json: string }>(`/projects/${projectId}/ai/summary`);
  return res.data;
}

export async function generateScoring(projectId: number): Promise<{ scoring_points_json: string; summary_json?: string; global_facts?: any[] }> {
  const res = await httpPostLong<{ scoring_points_json: string; summary_json?: string; global_facts?: any[] }>(`/projects/${projectId}/ai/scoring`);
  return res.data;
}

export async function confirmSummary(projectId: number, summaryJson: string): Promise<void> {
  await http.post(`/projects/${projectId}/ai/summary/confirm`, {
    summary_json: summaryJson,
  });
}

export async function confirmScoring(projectId: number, scoringPointsJson: string): Promise<void> {
  await http.post(`/projects/${projectId}/ai/scoring/confirm`, {
    scoring_points_json: scoringPointsJson,
  });
}

export async function clearAnalysis(projectId: number): Promise<void> {
  await http.delete(`/projects/${projectId}/ai/analysis`);
}

// Enhanced Analysis
export async function analyzeBoq(projectId: number): Promise<{ boq_json: string }> {
  const res = await httpPostLong<{ boq_json: string }>(`/projects/${projectId}/ai/boq`);
  return res.data;
}

export async function confirmBoq(projectId: number): Promise<void> {
  await http.post(`/projects/${projectId}/ai/boq/confirm`);
}

export async function analyzeWorkInterface(projectId: number): Promise<{ work_interface_json: string }> {
  const res = await httpPostLong<{ work_interface_json: string }>(`/projects/${projectId}/ai/work-interface`);
  return res.data;
}

export async function confirmWorkInterface(projectId: number): Promise<void> {
  await http.post(`/projects/${projectId}/ai/work-interface/confirm`);
}

export async function analyzeConstructionMethod(projectId: number): Promise<{ construction_method_json: string }> {
  const res = await httpPostLong<{ construction_method_json: string }>(`/projects/${projectId}/ai/construction-method`);
  return res.data;
}

export async function confirmConstructionMethod(projectId: number): Promise<void> {
  await http.post(`/projects/${projectId}/ai/construction-method/confirm`);
}

// Global Facts
/**
 * 全局事实条目（与后端 `models.GlobalFact` 字段契约同源）。
 *
 * BUG-FIX 2026-09-29（前后端字段契约）：`needs_manual_input` 此前只在后端
 * /facts/generate 里打给前端，前端类型里没有这个字段（页面靠 `any` 绕过），
 * 而保存时后端用 GlobalFact 校形，一旦前端回传不带的字段会被 Pydantic
 * 默认策略丢弃 → 标记静默消失。现在两端同一个字段集写进类型，避免后续再漏。
 */
export interface GlobalFactItem {
  key: string;
  label?: string;
  value?: string;
  /** 分类英文键，以后端 FACT_CATEGORIES 为基准（中文名由 getFacts().categories 下发） */
  category?: string;
  /** ai = 模型提取；heuristic = 规则启发式兜底；default = 内置兜底；manual = 人工录入 */
  source?: "ai" | "heuristic" | "default" | "manual" | string;
  verified?: boolean;
  /** placeholder 策略下为 true：值仍是【待填写】占位，需人工补齐 */
  needs_manual_input?: boolean;
  [k: string]: any;
}

/**
 * 事实集**内部**矛盾分组（后端 facts_service.group_fact_conflicts）。
 * BUG-FIX 2026-10-06：后端 GET /facts(facts.py:372) 与 POST /facts/generate(facts.py:644)
 * 一直都会下发 `fact_conflicts`，但此前前端返回类型里没有这个字段、页面也不消费，
 * 导致「同一事实被填了不同值」（如项目经理既有 AI 提取的【张伟】、又手工补录
 * 【李强】）这一高危矛盾在界面上完全不可见 —— 后端算好了却没人看。
 * 非阻断字段：声明为可选，既有调用方零改动。
 */
export interface FactConflict {
  label: string;
  key: string;
  values: Array<{
    key: string;
    label: string;
    value: string;
    category?: string;
    norm_value?: string;
    [k: string]: any;
  }>;
}

/** POST /facts/audit 返回体（ENHANCE 2026-09-29：覆盖率与降级状态一并下发） */
export interface FactsAuditResult {
  items: Array<{
    fact_key?: string;
    fact_label?: string;
    fact_value?: string;
    status?: "consistent" | "inconsistent" | "missing" | string;
    detail?: string;
    [k: string]: any;
  }>;
  total_facts: number;
  audited_sections: number;
  /** 本次送审覆盖率诊断：共多少已完成章节 / 是否被截断 / 分了几组 / 失败几组 */
  total_sections?: number;
  truncated?: boolean;
  audit_status?: "ok" | "partial" | "failed";
  /** BUG-FIX 2026-10-04：落库形态（GET /facts 的 last_audit）历史键名叫 `status`，
   *  与 HTTP 返回体的 `audit_status` 双写并存，前端回看时任一命中。 */
  status?: "ok" | "partial" | "failed";
  audit_groups?: number;
  failed_groups?: number;
  /** 因仍是【待填写】而未参与审计的事实条数 */
  skipped_placeholder?: number;
}

export async function getFacts(projectId: number): Promise<{
  items: GlobalFactItem[];
  confirmed: boolean;
  /** ENHANCE 2026-09-15：上次提取时使用的缺失值策略 */
  missing_value_policy?: FactsMissingValuePolicy;
  /** placeholder 策略下仍留【待填写】的事实条数 */
  placeholder_count?: number;
  /** ENHANCE 2026-09-29：上一次一致性审计结果（刷新页面不丢） */
  last_audit?: FactsAuditResult | null;
  audited_at?: string | null;
  /** ENHANCE 2026-09-29：分类英文键 → 中文名，前端不再自己维护第二份映射 */
  categories?: Record<string, string>;
  /** BUG-FIX 2026-10-06：事实集内部互斥取值分组（后端一直在下发，此前类型缺失） */
  fact_conflicts?: FactConflict[];
}> {
  const res = await http.get(`/projects/${projectId}/facts`);
  return res.data;
}

export async function saveFacts(
  projectId: number,
  items: GlobalFactItem[],
): Promise<{ ok: boolean; fact_conflicts?: FactConflict[] }> {
  // ENHANCE 2026-10-06：后端保存响应即时回传事实集内部互斥分组
  // （同 label 不同 key/值），前端不必等下次 GET 才发现矛盾。
  const res = await http.post<{ ok: boolean; fact_conflicts?: FactConflict[] }>(
    `/projects/${projectId}/facts`,
    { items },
  );
  return res.data;
}

/** 一键应用错别字修正（ENHANCE 2026-09-15） */
export async function applySpellingFix(req: {
  task_id: number;
  item_ids: string[];
  writeback_project?: boolean;
}): Promise<{
  ok: boolean;
  task_id: number;
  applied: Array<{
    item_id: string;
    before: string;
    after: string;
    line_hint?: string;
    excerpt?: string;
    section_id?: number | null;
  }>;
  skipped: Array<{ item_id: string; reason: string }>;
  applied_count: number;
  skipped_count: number;
}> {
  const res = await http.post("/rejection/apply-spelling", req);
  return res.data;
}

export interface GenerateFactsResult {
  items: GlobalFactItem[];
  confirmed: boolean;
  /** "ai" = AI 精准提取；"heuristic" = AI 失败/空结果后的规则启发式兜底，需人工确认 */
  extraction_mode?: "ai" | "heuristic";
  warning?: string;
  /** ENHANCE 2026-09-15：本次采用的缺失值策略 */
  missing_value_policy?: FactsMissingValuePolicy;
  /** ENHANCE 2026-09-15：placeholder 策略下留了【待填写】的事实条数 */
  placeholder_count?: number;
  /** ENHANCE 2026-09-29：分类英文键 → 中文名（与 GET /facts 同一来源） */
  categories?: Record<string, string>;
  /** BUG-FIX 2026-10-06：事实集内部互斥取值分组（与 GET /facts 同一来源） */
  fact_conflicts?: FactConflict[];
}

/**
 * 全局事实缺失值策略（移植自易标 globalFactsTask 的三档模式）：
 * - fabricate   资料没给值时可模拟生成合理值（历史默认行为）
 * - omit        不编造，改为不含具体值的笼统承诺
 * - placeholder 不编造，留「【待填写】」占位交人工补齐
 */
export type FactsMissingValuePolicy = "fabricate" | "omit" | "placeholder";

export async function generateFacts(
  projectId: number,
  missingValuePolicy: FactsMissingValuePolicy = "fabricate",
): Promise<GenerateFactsResult> {
  // BUG-F-18 修复：/facts/generate 处理招标文件全文提取 6 大类事实变量，
  // AI 调用耗时可达 100-300s，普通 http.post 只有 60s 超时会导致 ERR_ABORTED。
  // 与 /ai/summary、/ai/scoring、/ai/construction-method 对齐使用 httpPostLong(600s)。
  const res = await httpPostLong<GenerateFactsResult>(`/projects/${projectId}/facts/generate`, {
    missing_value_policy: missingValuePolicy,
  });
  return res.data;
}

export async function confirmFacts(projectId: number): Promise<void> {
  await http.post(`/projects/${projectId}/facts/confirm`);
}

export async function auditFactsConsistency(projectId: number): Promise<FactsAuditResult> {
  // BUG-FIX 2026-09-29：本接口要对所有已完成章节分箱送审（旧实现只送前 5 章），
  // 多组并发下耗时可到分钟级，用长超时通道，避免 60s 处 ERR_ABORTED 后被当成“无问题”。
  const res = await httpPostLong<FactsAuditResult>(`/projects/${projectId}/facts/audit`, {});
  return res.data;
}

// Templates (已有方案扩写)
export async function uploadTemplate(projectId: number, file: File): Promise<{ template_id: number; filename: string; status: string }> {
  const form = new FormData();
  form.append("file", file);
  const res = await http.post(`/projects/${projectId}/templates/upload`, form, {
    headers: { "Content-Type": "multipart/form-data" },
    timeout: 120000,
  });
  return res.data;
}

export async function extractTemplateOutline(projectId: number, templateId: number): Promise<{ template_id: number; outline: any[]; title_count: number }> {
  const res = await http.post(`/projects/${projectId}/templates/${templateId}/extract`, {}, { timeout: 300000 });
  return res.data;
}

export async function listTemplates(projectId: number): Promise<{ items: any[] }> {
  const res = await http.get(`/projects/${projectId}/templates`);
  return res.data;
}

export async function deleteTemplate(projectId: number, templateId: number): Promise<void> {
  await http.delete(`/projects/${projectId}/templates/${templateId}`);
}

export async function auditTemplateCoverage(projectId: number, templateId: number): Promise<{ template_id: number; total: number; covered: number; missing: number; coverage_rate: number; items: any[] }> {
  const res = await http.post(`/projects/${projectId}/templates/${templateId}/audit`, {}, { timeout: 60000 });
  return res.data;
}

// Sections
export async function fetchSections(projectId: number): Promise<SectionTreeNode[]> {
  const res = await http.get<SectionTreeNode[]>(`/projects/${projectId}/sections`);
  return res.data;
}

export async function fetchSectionsFlat(projectId: number): Promise<Section[]> {
  const res = await http.get<Section[]>(`/projects/${projectId}/sections/flat`);
  return res.data;
}

export async function getSection(projectId: number, sectionId: number): Promise<Section> {
  const res = await http.get<Section>(`/projects/${projectId}/sections/${sectionId}`);
  return res.data;
}

export async function createSection(projectId: number, data: SectionCreate): Promise<Section> {
  const res = await http.post<Section>(`/projects/${projectId}/sections`, data);
  return res.data;
}

export async function updateSection(projectId: number, sectionId: number, data: SectionUpdate): Promise<Section> {
  const res = await http.patch<Section>(`/projects/${projectId}/sections/${sectionId}`, data);
  return res.data;
}

export async function deleteSection(projectId: number, sectionId: number): Promise<void> {
  await http.delete(`/projects/${projectId}/sections/${sectionId}`);
}

export async function clearSections(projectId: number): Promise<void> {
  await http.delete(`/projects/${projectId}/sections`);
}

export async function moveSection(projectId: number, sectionId: number, direction: "up" | "down"): Promise<SectionTreeNode[]> {
  const res = await http.post(`/projects/${projectId}/sections/${sectionId}/move?direction=${direction}`);
  return res.data;
}

export async function reorderSections(projectId: number, groups: { parent_id: number | null; ordered_ids: number[] }[]): Promise<SectionTreeNode[]> {
  const res = await http.post(`/projects/${projectId}/sections/reorder`, { groups });
  return res.data;
}

/**
 * 用 fetch + ReadableStream 替代 EventSource，消除 Chrome DevTools ERR_ABORTED 日志噪音
 * 与 createContentStream 采用相同的流式处理模式
 */
export interface OutlineEventSource {
  onmessage: ((event: { data: string }) => void) | null;
  onerror: ((event: unknown) => void) | null;
  close: () => void;
  detach: () => void;  // 后台模式：断开前端但不中止后端任务
  markTerminalEvent: () => void;  // 标记已收到终端事件（避免 close() 时 abort 产生 ERR_ABORTED）
  promise: Promise<void>;
}

export function createOutlineEventSource(
  projectId: number,
  outlineMode: OutlineMode = getStoredOutlineMode(),
): OutlineEventSource {
  const url = generateOutlineSSEUrl(projectId, "v3", outlineMode);

  let aborted = false;
  let streamFinished = false;
  let detached = false;  // 2026-08-21 增强：标记为后台模式（页面导航时使用）
  let _terminalEventReceived = false;  // BUG-F-04 修复：标记已收到终端事件，避免 close() 时 abort 产生 ERR_ABORTED
  let controller: AbortController | null = null;
  // BUG-F-01 修复：添加重连计数器
  let reconnectAttempts = 0;
  const MAX_RECONNECT_ATTEMPTS = 3;
  const RECONNECT_DELAYS = [1000, 2000, 4000]; // 指数退避：1s, 2s, 4s

  const close = () => {
    aborted = true;
    // 流已正常结束时不再 abort，避免浏览器日志中出现 ERR_ABORTED
    // 后台模式（detached）：不 abort，让后端任务继续运行
    // BUG-F-04 修复：已收到终端事件时不 abort，让流自然结束以避免 ERR_ABORTED
    if (!streamFinished && !detached && !_terminalEventReceived && controller) {
      controller.abort();
      controller = null;
    }
  };

  /** 标记已收到终端事件（completed/error/stopped/cancelled），后续 close() 不会 abort */
  const markTerminalEvent = () => {
    _terminalEventReceived = true;
  };

  /** 后台模式：页面导航时调用，不中断后端任务 */
  const detach = () => {
    detached = true;
    aborted = true;  // 标记已断开，不再处理流数据
    // 不调用 controller.abort()，后端任务继续运行
  };

  let resolve: (() => void) | null = null;
  let reject: ((reason: unknown) => void) | null = null;

  const doFetch = async (): Promise<void> => {
    controller = new AbortController();
    const signal = controller.signal;

    try {
      const response = await fetch(url, { signal });
      if (!response.ok || !response.body) {
        // 非 SSE 响应（如 401/429/503），读取错误详情后立即抛出，不尝试重连
        let errorMessage = `HTTP ${response.status}: ${response.statusText}`;
        try {
          const contentType = response.headers.get("content-type") || "";
          if (contentType.includes("application/json")) {
            const body = await response.json().catch(() => null);
            if (body && typeof body === "object" && "detail" in body) {
              errorMessage = String(body.detail);
            }
          } else {
            const text = await response.text().catch(() => "");
            if (text) errorMessage = text.slice(0, 200);
          }
        } catch {}
        // BUG-FIX 2026-09-16（高）：同 `createChapterContentStream`，
        // HTTP 级错误（401/429/503）必须带标记，否则会走 SSE 重连分支白等 ~7 秒。
        throw Object.assign(new Error(errorMessage), {
          isHttpError: true,
          status: response.status,
        });
      }
      if (aborted) return;

      // 连接成功，重置重连计数器
      reconnectAttempts = 0;

      const reader = response.body.getReader();
      const decoder = new TextDecoder("utf-8");
      let buffer = "";

      // BUG-OUTLINE-08 修复：重写 SSE 事件解析器，正确处理 \r\n 行尾和事件边界，
      // 支持多 data: 行拼接（SSE 协议允许多行 data 事件用换行拼接），
      // 过滤掉 : (comment) 行和空行，确保事件完整正确解析
      const processBuffer = (buf: string, cb: ((event: { data: string }) => void) | null) => {
        if (!cb || !buf.length) return;
        // 规范化换行符并按 SSE 事件边界（空行）分割
        const normalized = buf.replace(/\r\n/g, '\n').replace(/\r/g, '\n');
        const eventBlocks = normalized.split('\n\n');
        for (const block of eventBlocks) {
          if (!block.trim()) continue;
          // 收集所有 data: 行（SSE 支持多 data 行拼接），忽略 : 注释行
          const dataLines: string[] = [];
          for (const line of block.split('\n')) {
            if (line.startsWith('data:')) {
              dataLines.push(line.slice(5).replace(/^\s/, ''));
            }
          }
          if (dataLines.length > 0) {
            const content = dataLines.join('\n');
            cb({ data: content });
          }
        }
      };

      while (true) {
        if (aborted) break;
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const lastNewline = buffer.lastIndexOf("\n");
        if (lastNewline >= 0) {
          const chunk = buffer.slice(0, lastNewline);
          buffer = buffer.slice(lastNewline + 1);
          processBuffer(chunk, outlineStream._onmessage ?? null);
        }
        // lastNewline < 0 时保留不完整数据在 buffer，等下一批 chunk 拼合后再处理
      }

      // 流正常结束时，flush 缓冲区残留数据（防止最后一条事件被丢弃）
      if (buffer.length > 0 && !aborted) {
        processBuffer(buffer, outlineStream._onmessage ?? null);
        buffer = "";
      }

      streamFinished = true;
      if (!aborted) {
        if (resolve) resolve();
      }
    } catch (e: unknown) {
      if (aborted || (e instanceof DOMException && e.name === "AbortError")) {
        if (resolve) resolve();
        return;
      }

      // BUG-F-01 修复：非 abort 错误时尝试自动重连
      if (reconnectAttempts < MAX_RECONNECT_ATTEMPTS && !aborted && !detached && !streamFinished) {
        reconnectAttempts++;
        const delay = RECONNECT_DELAYS[reconnectAttempts - 1] || RECONNECT_DELAYS[RECONNECT_DELAYS.length - 1];
        console.warn(
          `[SSE] 大纲流连接断开，${delay}ms 后尝试第 ${reconnectAttempts}/${MAX_RECONNECT_ATTEMPTS} 次重连...`
        );
        // 通知上层正在重连
        if (outlineStream._onerror) {
          outlineStream._onerror({ type: "reconnecting", attempt: reconnectAttempts, maxAttempts: MAX_RECONNECT_ATTEMPTS });
        }
        await new Promise((r) => setTimeout(r, delay));
        if (!aborted && !detached && !streamFinished) {
          await doFetch(); // 递归重连
        }
      } else {
        // 重连次数耗尽或用户主动关闭
        if (reject) reject(e);
        if (outlineStream._onerror) {
          outlineStream._onerror(e);
        }
      }
    }
  };

  const outlineStream: {
    onmessage: ((event: { data: string }) => void) | null;
    onerror: ((event: unknown) => void) | null;
    close: () => void;
    detach: () => void;
    markTerminalEvent: () => void;
    promise: Promise<void>;
    _onmessage?: ((event: { data: string }) => void) | null;
    _onerror?: ((event: unknown) => void) | null;
  } = {
    onmessage: null,
    onerror: null,
    close,
    detach,
    markTerminalEvent,
    promise: (() => {
      // BUG 修复：将 doFetch 启动推迟到下一微任务，确保事件处理器先被设置
      return new Promise<void>((res, rej) => {
        resolve = res;
        reject = rej;
        queueMicrotask(() => {
          if (!aborted && !detached) {
            doFetch();
          }
        });
      });
    })(),
  };

  Object.defineProperty(outlineStream, "onmessage", {
    get() { return this._onmessage; },
    set(fn) { this._onmessage = fn; },
    configurable: true,
  });
  Object.defineProperty(outlineStream, "onerror", {
    get() { return this._onerror; },
    set(fn) { this._onerror = fn; },
    configurable: true,
  });

  return outlineStream;
}

export async function reviewOutline(projectId: number): Promise<OutlineReviewResult> {
  // BUG 修复：后端返回 {"status": "completed", "result": {...}}，需解包 result 字段，
  // 否则前端校验 result.passed/issues 时拿到的是外层包装对象，导致审核按钮失效。
  const res = await httpPostLong<{ status: string; result: OutlineReviewResult }>(`/projects/${projectId}/sections/review`);
  return res.data.result;
}

/** V5.1 人工审核确认：提交审核通过的目录树，恢复生成流程 */
export async function v51Confirm(
  projectId: number,
  data: { edited_outline: SectionTreeNode[] },
): Promise<{ status: string; phase: string; next_phase: string }> {
  const res = await http.post<{ status: string; phase: string; next_phase: string }>(
    `/projects/${projectId}/sections/v51-confirm`,
    data,
  );
  return res.data;
}

/** 查询项目是否存在未完成的 V5.1 任务（断点续生） */
export interface V51PendingTask {
  has_pending: boolean;
  phase: string | null;
  outline: SectionTreeNode[] | null;
  message: string;
}

export async function fetchPendingV51Task(
  projectId: number,
): Promise<V51PendingTask> {
  const res = await http.get<V51PendingTask>(
    `/projects/${projectId}/sections/v51-pending-task`,
  );
  // 契约BUG2 修复：后端 /v51-pending-task 直接返回 {has_pending, phase, outline, message}，
  // 无 data 包装层（axios 实例亦无包装拦截器）。旧实现读 res.data.data 恒为
  // undefined，断点续生提示弹窗永远不出现。
  return res.data;
}

/** 清除未完成的 V5.1 任务状态（用于重新开始生成） */
export async function clearV51PendingTask(projectId: number): Promise<{ status: string }> {
  const res = await http.post<{ status: string }>(
    `/projects/${projectId}/sections/v51-clear-pending`,
    {},
  );
  return res.data;
}

// ========== 目录 AI 审核与自修复 ==========

export interface OutlineReviewWithAutofixResult {
  passed: boolean;
  issues: string[];
  suggestions: string[];
  repaired: boolean;
  /** AI 修复后的裸目录 JSON（无数据库 id，仅作展示/降级用） */
  fixed_outline?: unknown[] | null;
  /** BUG-FIX 2026-09-29：修复结果落库后后端返回的带数据库 ID 完整目录树 */
  tree?: SectionTreeNode[];
}

export async function reviewOutlineWithAutofix(projectId: number): Promise<OutlineReviewWithAutofixResult> {
  // BUG 修复：后端返回 {"status": "completed", "result": {...}}，需解包 result 字段，
  // 否则前端校验 result.passed/repaired/fixed_outline 拿到的是外层包装对象，导致自动修复按钮失效。
  const res = await httpPostLong<{ status: string; result: OutlineReviewWithAutofixResult }>(`/projects/${projectId}/sections/review-with-autofix`);
  return res.data.result;
}

// ========== 兜底扩写 ==========

export interface ExpandContentResult {
  status: string;
  message?: string;
  expanded_count?: number;
  total?: number;
  section_id?: number;
  title?: string;
  phase?: string;
  /** 单章扩写完成事件携带的字数 */
  word_count?: number;
  /** 扩写后仍未达标的章节数 */
  deficient_after?: number;
  /** 实际生效的并发数（后端钳制后） */
  concurrency?: number;
}

export function expandContent(
  projectId: number,
  writingLength: string = "standard",
  level2Default: number = 3500,
  concurrency: number = 2,
  onProgress?: (event: ExpandContentResult) => void,
): { promise: Promise<ExpandContentResult>; abort: () => void } {
  let aborted = false;
  let abortController: AbortController | null = new AbortController();

  const cleanup = () => {
    aborted = true;
    if (abortController) {
      try { abortController.abort(); } catch { /* ignore */ }
      abortController = null;
    }
  };

  let reject: (reason: any) => void;
  let resolve: (value: ExpandContentResult) => void;

  return {
    promise: new Promise<ExpandContentResult>((res, rej) => {
      resolve = res;
      reject = rej;

      const signal = abortController!.signal;
      const params = new URLSearchParams();
      params.set("writing_length", writingLength);
      params.set("level2_default", String(level2Default));
      if (concurrency && concurrency > 0) {
        params.set("concurrency", String(concurrency));
      }
      const url = toSSEFullUrl(`/api/v1/projects/${projectId}/sections/expand-content?${params.toString()}`);

      fetch(url, {
        method: "POST",
        headers: { Accept: "text/event-stream" },
        signal,
      })
        .then(async (response) => {
          if (!response.ok || !response.body) {
            let detail = `HTTP ${response.status}`;
            try {
              const j = await response.json();
              detail = j?.detail || detail;
            } catch { /* ignore */ }
            reject(new Error(detail));
            return;
          }

          const reader = response.body.getReader();
          const decoder = new TextDecoder("utf-8");
          let buffer = "";
          let currentData = "";

          const processLine = (line: string) => {
            if (line === "") {
              if (currentData) {
                let parsed: ExpandContentResult | null = null;
                try {
                  parsed = JSON.parse(currentData);
                } catch { /* ignore parse error */ }
                if (parsed) {
                  // BUG-FIX 2026-09-16（P1 promise 悬挂）：原实现把 onProgress 与
                  // resolve/reject 放在同一个 try 里 —— onProgress 一旦抛错，异常被
                  // 这个 catch 静默吞掉，**后续的终态判定一起被跳过**，
                  // 扩写 promise 永远不会 settle：调用方 isExpanding 卡 true、
                  // 按钮一直转圈且没有任何提示。
                  // 现在把「解析 / 回调 / 终态判定」彻底分离，回调异常不影响 promise。
                  try {
                    if (onProgress) onProgress(parsed);
                  } catch (cbErr) {
                    console.warn("[扩写] onProgress 回调异常（已忽略）:", cbErr);
                  }
                  if (parsed.status === "completed" || parsed.status === "stopped") {
                    resolve(parsed);
                  } else if (parsed.status === "error") {
                    reject(new Error(parsed.message || "扩写失败"));
                  }
                }
              }
              currentData = "";
              return;
            }
            if (line.startsWith(":")) return;
            if (line.startsWith("data:")) {
              currentData = line.slice(5).trim();
            }
          };

          let streamEnded = false;
          // eslint-disable-next-line no-constant-condition
          while (!aborted) {
            const { value, done } = await reader.read();
            if (done) { streamEnded = true; break; }
            buffer += decoder.decode(value, { stream: true });
            let idx;
            while ((idx = buffer.indexOf("\n")) >= 0) {
              const line = buffer.slice(0, idx).replace(/\r$/, "");
              buffer = buffer.slice(idx + 1);
              processLine(line);
            }
          }
          if (streamEnded && !aborted) {
            reject(new Error("SSE 流异常结束，未收到完成事件"));
          }
        })
        .catch((e: any) => {
          if (e?.name === "AbortError") {
            reject(new Error("扩写已被取消"));
            return;
          }
          reject(new Error("扩写请求失败: " + (e?.message || "未知错误")));
        });
    }),
    abort: () => {
      cleanup();
    },
  };
}

// ========== 全文一致性审计 ==========
// 注：consistencyAudit 已废弃，正确实现为 runConsistencyAudit (GET /consistency/audit/{projectId})
// 保留 ConsistencyAuditResult 供类型引用（如需）

/** 正文编排参数（ENHANCE 2026-10-05 / P-11）
 *
 * 对齐易标规格六.4/六.5：表格密度、图片上限、图表偏好、分段择优粒度。
 * 所有字段可选，缺省时后端使用默认值（见 routers/sse/sse_content.py）。
 */
export interface ContentOrchOptions {
  /** 全文 AI 生图硬上限，0 = 禁用 AI 生图（对应易标 useAiImages=false） */
  maxAiImages?: number;
  /** 表格密度 none / light / moderate / heavy（对应 20% / 40% / 宽松） */
  tableRatio?: "none" | "light" | "moderate" | "heavy";
  /** true = 同章节两者都需时 mermaid 优先；false = 两类按优先级合并竞争同一配额 */
  preferMermaid?: boolean;
  /** 分段择优的段长（越小图片分布越均匀） */
  sectionPerSegment?: number;
}

export function generateContentSSEUrl(
  projectId: number,
  writingLength: string = "standard",
  regenerateIds?: number[],
  rootSectionId?: number | null,
  regenerateMissing?: boolean,
  level2Default?: number,
  useOrchestration?: boolean,
  useWordSampling?: boolean,
  useFallbackExpand?: boolean,
  useConsistencyAudit?: boolean,
  concurrency?: number,
  orchOptions?: ContentOrchOptions,
): string {
  const params = new URLSearchParams();
  params.set("writing_length", writingLength);
  if (regenerateIds && regenerateIds.length > 0) {
    params.set("regenerate_ids", regenerateIds.join(","));
  }
  if (rootSectionId != null) {
    params.set("root_section_id", String(rootSectionId));
  }
  if (regenerateMissing) {
    params.set("regenerate_missing", "true");
  }
  if (level2Default && level2Default > 0) {
    params.set("level2_default", String(level2Default));
  }
  if (useOrchestration) {
    params.set("use_orchestration", "true");
  }
  if (useWordSampling) {
    params.set("use_word_sampling", "true");
  }
  if (useFallbackExpand) {
    params.set("use_fallback_expand", "true");
  }
  if (useConsistencyAudit) {
    params.set("use_consistency_audit", "true");
  }
  if (concurrency && concurrency > 0) {
    params.set("concurrency", String(concurrency));
  }
  // ENHANCE 2026-10-05（P-11）：正文编排参数透传
  if (orchOptions) {
    if (typeof orchOptions.maxAiImages === "number") {
      params.set("orch_max_ai_images", String(orchOptions.maxAiImages));
    }
    if (orchOptions.tableRatio) {
      params.set("orch_table_ratio", orchOptions.tableRatio);
    }
    if (typeof orchOptions.preferMermaid === "boolean") {
      params.set("orch_prefer_mermaid", String(orchOptions.preferMermaid));
    }
    if (typeof orchOptions.sectionPerSegment === "number") {
      params.set("orch_section_per_segment", String(orchOptions.sectionPerSegment));
    }
  }
  return toSSEFullUrl(`/api/v1/projects/${projectId}/sections/generate-content?${params.toString()}`);
}

export function createContentEventSource(
  projectId: number,
  writingLength: string = "standard",
  regenerateIds?: number[],
  rootSectionId?: number | null,
  regenerateMissing?: boolean,
  level2Default?: number,
  useOrchestration?: boolean,
  useWordSampling?: boolean,
  useFallbackExpand?: boolean,
  useConsistencyAudit?: boolean,
  concurrency?: number,
  orchOptions?: ContentOrchOptions,
): EventSource {
  const url = generateContentSSEUrl(projectId, writingLength, regenerateIds, rootSectionId, regenerateMissing, level2Default, useOrchestration, useWordSampling, useFallbackExpand, useConsistencyAudit, concurrency, orchOptions);
  return new EventSource(url, { withCredentials: false });
}

/**
 * 用 fetch + ReadableStream 替代 EventSource，消除 Chrome DevTools ERR_ABORTED 日志噪音
 * 与 exportDocxStream 采用相同的流式处理模式
 */
export function createContentStream(
  projectId: number,
  writingLength: string = "standard",
  regenerateIds?: number[],
  rootSectionId?: number | null,
  regenerateMissing?: boolean,
  level2Default?: number,
  useOrchestration?: boolean,
  useWordSampling?: boolean,
  useFallbackExpand?: boolean,
  useConsistencyAudit?: boolean,
  concurrency?: number,
  orchOptions?: ContentOrchOptions,
): {
  onopen: (() => void) | null;
  onmessage: ((event: { data: string }) => void) | null;
  onerror: ((event: unknown) => void) | null;
  close: () => void;
  detach: () => void;
  markTerminalEvent: () => void;
  promise: Promise<void>;
} {
  const url = generateContentSSEUrl(projectId, writingLength, regenerateIds, rootSectionId, regenerateMissing, level2Default, useOrchestration, useWordSampling, useFallbackExpand, useConsistencyAudit, concurrency, orchOptions);

  let aborted = false;
  let detached = false;
  let _terminalEventReceived = false;  // BUG-F-04 修复：标记已收到终端事件，避免 close() 时 abort 产生 ERR_ABORTED
  let controller: AbortController | null = null;
  // BUG-F-01 修复：添加重连计数器
  let reconnectAttempts = 0;
  const MAX_RECONNECT_ATTEMPTS = 3;
  const RECONNECT_DELAYS = [1000, 2000, 4000]; // 指数退避：1s, 2s, 4s
  // BUG-FIX 2026-10-08（P0）：本次连接是否已读到响应体内容。
  // 只有「读到字节」才算一次健康连接；仅拿到 HTTP 200 不算（中途断流同样会先给 200）。
  let _gotFirstFrame = false;
  // BUG-F-03 修复：追踪 fetch 是否已启动，防止 close() 在 fetch 启动前丢失 abort 信号
  let fetchStarted = false;
  // 流正常结束后标记，防止 close() 在结束后仍 abort 触发 ERR_ABORTED
  let streamFinished = false;

  const close = () => {
    aborted = true;
    // 流已正常结束时不再 abort，避免浏览器日志中出现 ERR_ABORTED
    // 后台模式（detached）：不 abort，让后端任务继续运行
    // BUG-F-04 修复：已收到终端事件时不 abort，让流自然结束以避免 ERR_ABORTED
    if (!streamFinished && !detached && !_terminalEventReceived && controller) {
      controller.abort();
      controller = null;
    }
  };

  /** 标记已收到终端事件（completed/error/stopped），后续 close() 不会 abort */
  const markTerminalEvent = () => {
    _terminalEventReceived = true;
  };

  // 2026-08-21 增强：detach 模式 - 断开前端连接但保留后端任务
  const detach = () => {
    detached = true;
    aborted = true; // 标记已断开，不再处理流数据
    // BUG-FIX 2026-10-08（P1 连接泄漏）：旧实现**故意不 abort**，理由是「不调用
    // controller.abort()，后端任务继续运行」。但这是误解：abort 只是关闭本端的
    // HTTP 响应体，后端 run_generation 是独立 asyncio 任务，客户端断开时它本来就会
    // 转入后台续跑（见 sse_content.py 的 _drain_queue）。不 abort 的话响应体一直挂着，
    // 直到后端下一次写；浏览器同源连接上限约 6 条 —— 「生成中刷新/切页」6 次就把整个
    // 标签页的连接池耗尽（后续所有请求排队）。
    // 对齐 expandContent（同文件 cleanup 分支）的做法：abort 释放 socket。
    if (controller) {
      try {
        controller.abort();
      } catch {
        /* 已结束，忽略 */
      }
      controller = null;
    }
  };

  let resolve: (() => void) | null = null;
  let reject: ((reason: unknown) => void) | null = null;

  const doFetch = async (): Promise<void> => {
    // BUG-F-03 修复：如果 close() 已被调用，跳过 fetch 启动
    // 原代码：close() 在 doFetch() 之前调用时，controller 仍为 null，
    // abort 信号丢失，导致 fetch 仍会启动但前端已放弃处理
    if (aborted || detached) {
      if (resolve) resolve();
      return;
    }
    fetchStarted = true;
    controller = new AbortController();
    const signal = controller.signal;

    try {
      const response = await fetch(url, { signal });
      if (!response.ok || !response.body) {
        // BUG-FIX 2026-09-16（高）：这个 Error 没打 `isHttpError` 标记，
        // 而下面的 catch 与 SectionEditorPage 都靠该标记做「HTTP 错误快失败」——
        // 结果 401/429/503 全落到 SSE 重连分支去重试 3 次（1s/2s/4s）；
        // `/generate-content` 又是 GET 且会触发生成，重试等于重复拉任务。
        throw Object.assign(new Error(`HTTP ${response.status}: ${response.statusText}`), {
          isHttpError: true,
          status: response.status,
        });
      }
      // BUG-F-03 修复：在 fetch 成功后再次检查 aborted 状态
      // 防止 close() 在 await fetch() 期间被调用
      if (aborted || detached) {
        if (resolve) resolve();
        return;
      }

      // BUG-FIX 2026-10-08（P0 无限重连 + 伪报失败）：旧实现在这里（**刚拿到 HTTP 200、
      // 一个字节的响应体都还没读**）就 `reconnectAttempts = 0`。
      // 而 SSE 场景最常见的失败不是「连不上」，而是「连上了、然后中途断了」
      // （nginx proxy_read_timeout、worker 重启、笔记本休眠）。
      // 于是每次中途断开 → 计数 +1 → 重连 → 又拿到 200 → 计数清零 → 再断 → 再 +1 …，
      // MAX_RECONNECT_ATTEMPTS=3 永远达不到，变成**无上限重连循环**。
      // 更糟的是每次重连都是对 /generate-content 的**同一个 GET**（会触发生成），
      // 极易撞上后端任务组互斥，后端推 `{"status":"error","code":"TASK_LOCK_CONFLICT"}`，
      // 前端把它当终态 → 弹「生成失败」并把所有节点标红，而后端任务其实在正常跑完所有章节。
      // 修复：计数器**只在真正读到响应体第一帧之后**才重置（`_gotFirstFrame`），
      // 这样「中途断流」会被连续计数，3 次后走 exhausted 分支。
      // 后端任务在断流期间继续运行（后台续跑），因此 exhausted 时不伪造失败，
      // 而是 reject 一个带 `connectionLost` 标记的错误，由调用方降级为状态轮询。

      const reader = response.body.getReader();
      const decoder = new TextDecoder("utf-8");
      let buffer = "";

      // BUG-FIX 2026-10-08（P1 SSE 分帧）：旧实现按「行」切分并逐行回调
      // （buf.split("\n")），不是按 SSE 帧解析：
      //   - 多行 data: 帧会被拆成多条非法 JSON；
      //   - `data:`（无空格）会连前缀一起丢给 JSON.parse；
      //   - `: keep-alive` 注释、event:/id:/retry: 行也会被当数据；
      //   - 每个 chunk 都重新处理整个未截断 buffer（O(n²)）。
      // 这里按规范：以空行分帧、合并多行 data、跳过注释与其它字段。
      // 与本文件 exportDocxStream 的 processLine 口径一致。
      // eslint-disable-next-line no-loop-func
      const processBuffer = (buf: string, cb: ((event: { data: string }) => void) | null, flush: boolean) => {
        if (!cb || !buf) return;
        // 归一化换行：SSE 规范允许 \r\n / \r
        const normalized = buf.replace(/\r\n/g, "\n").replace(/\r/g, "\n");
        const frames = normalized.split("\n\n");
        // 最后一段可能是未闭合的半帧；flush=false 时留到下一批
        const tail = flush ? "" : frames[frames.length - 1];
        const complete = flush ? frames : frames.slice(0, -1);
        for (const frame of complete) {
          if (!frame.trim()) continue;
          const dataLines: string[] = [];
          for (const raw of frame.split("\n")) {
            const line = raw.trimEnd();
            if (!line || line.startsWith(":")) continue; // keep-alive 注释
            if (line.startsWith("data:")) {
              dataLines.push(line.slice(5).replace(/^ /, ""));
            }
            // event:/id:/retry: 本流未使用，忽略
          }
          if (dataLines.length === 0) continue;
          try {
            cb({ data: dataLines.join("\n") });
          } catch {
            // 忽略单帧处理异常
          }
        }
        buffer = tail;
      };

      // 流正常结束时，处理缓冲区残留数据
      const flushBuffer = () => {
        if (buffer.length > 0) {
          processBuffer(buffer, (stream as any)._onmessage, true);
          buffer = "";
        }
      };

      // eslint-disable-next-line no-constant-condition
      while (true) {
        if (aborted) break;
        const { value, done } = await reader.read();
        if (done) break;
        const chunk = decoder.decode(value, { stream: true });
        if (!_gotFirstFrame && chunk.length > 0) {
          // 真的读到了响应体内容 → 这次连接是健康的，重置计数器
          _gotFirstFrame = true;
          reconnectAttempts = 0;
        }
        buffer += chunk;
        processBuffer(buffer, (stream as any)._onmessage, false);
      }

      // 流正常结束时，flush 缓冲区残留数据（防止最后一条事件被丢弃）
      flushBuffer();

      streamFinished = true;
      if (!aborted) {
        // BUG-FIX 2026-09-16（P1 UI 卡死）：流「干净结束」但从未收到终端事件
        // （completed / stopped / error / info）时，旧实现直接 resolve() —— 而调用方
        // （SectionEditorPage.startGeneration）只挂了 `.catch()`，成功路径没有任何收尾，
        // 于是 `isGenerating` 永远为 true、按钮一直转圈，用户必须刷新页面才能恢复。
        // 这里与 expandContent（同文件 "SSE 流异常结束"）对齐，reject 出明确原因，
        // 交给调用方的 pollAndReconnect 轮询兜底。
        if (!_terminalEventReceived) {
          if (reject) reject(new Error("SSE 流异常结束，未收到完成事件"));
        } else if (resolve) {
          resolve();
        }
      }
    } catch (e: unknown) {
      // BUG-F-03 修复：优先检查 aborted/detached 状态
      // 原代码先检查 AbortError 类型，但当 close() 在 fetch 成功后调用，
      // fetch 不会抛 AbortError，而是后续 reader.read() 才抛，
      // 导致 catch 逻辑走错分支。现在统一用 aborted/detached 检查。
      if (aborted || detached || (e instanceof DOMException && e.name === "AbortError")) {
        if (resolve) resolve();
        return;
      }

      // HTTP 错误（401/429/503 等）不应尝试重连，直接报错
      if ((e as any).isHttpError) {
        if (reject) reject(e);
        if ((stream as any)._onerror) {
          (stream as any)._onerror(e);
        }
        return;
      }

      // BUG-F-01 修复：非 abort 错误时尝试自动重连
      // BUG-F-03 修复：增加 fetchStarted 检查，防止在 close() 后重连
      if (reconnectAttempts < MAX_RECONNECT_ATTEMPTS && !aborted && !detached && fetchStarted) {
        reconnectAttempts++;
        const delay = RECONNECT_DELAYS[reconnectAttempts - 1] || RECONNECT_DELAYS[RECONNECT_DELAYS.length - 1];
        console.warn(
          `[SSE] 连接断开，${delay}ms 后尝试第 ${reconnectAttempts}/${MAX_RECONNECT_ATTEMPTS} 次重连...`
        );
        // 通知上层正在重连
        if ((stream as any)._onerror) {
          (stream as any)._onerror({ type: "reconnecting", attempt: reconnectAttempts, maxAttempts: MAX_RECONNECT_ATTEMPTS });
        }
        await new Promise((r) => setTimeout(r, delay));
        if (!aborted && !detached) {
          // BUG-FIX 2026-10-08（P0）：重连前先释放上一条连接，并复位「已读到首帧」标记，
          // 否则新连接的首帧不会重置计数器（_gotFirstFrame 仍是 true）。
          if (controller) {
            try {
              controller.abort();
            } catch {
              /* 忽略 */
            }
            controller = null;
          }
          _gotFirstFrame = false;
          await doFetch(); // 递归重连
        }
      } else if (_gotFirstFrame) {
        // BUG-FIX 2026-10-08（P0 伪报失败）：连接中途断开且重连次数耗尽。
        // 后端任务仍在后台继续跑（已落库的章节不回滚），此时**不能** reject 一个
        // 裸错误让调用方弹「生成失败」——那正是旧 bug：用户以为失败、手动重试，
        // 实际上一轮已在写盘。改为 reject 带 `connectionLost` 标记的错误，
        // 调用方据此降级为「连接中断，后台继续」+ 状态轮询，而不是标红。
        const lost = Object.assign(
          new Error(
            `SSE 连接中断（已重连 ${MAX_RECONNECT_ATTEMPTS} 次），生成任务可能仍在后台继续，请稍候查看进度`
          ),
          { connectionLost: true, cause: e },
        );
        if ((stream as any)._onerror) {
          (stream as any)._onerror({ type: "reconnect_exhausted", attempts: MAX_RECONNECT_ATTEMPTS });
        }
        if (reject) reject(lost);
      } else {
        // 重连次数耗尽或用户主动关闭
        if (reject) reject(e);
      }
    }
  };

  const stream: {
    onopen: (() => void) | null;
    onmessage: ((event: { data: string }) => void) | null;
    onerror: ((event: unknown) => void) | null;
    close: () => void;
    detach: () => void;
    markTerminalEvent: () => void;
    promise: Promise<void>;
  } = {
    onopen: null,
    onmessage: null,
    onerror: null,
    close,
    detach,
    markTerminalEvent,
    promise: (() => {
      // BUG 修复：将 doFetch 启动推迟到下一微任务，确保事件处理器先被设置
      return new Promise<void>((res, rej) => {
        resolve = res;
        reject = rej;
        queueMicrotask(() => {
          if (!aborted && !detached) {
            doFetch();
          }
        });
      });
    })(),
  };

  // 重写 onmessage/onerror 为直接属性，避免 getter this 问题
  Object.defineProperty(stream, "onmessage", {
    get() { return (this as any)._onmessage; },
    set(fn: ((event: { data: string }) => void) | null) { (this as any)._onmessage = fn; },
    configurable: true,
  });
  Object.defineProperty(stream, "onerror", {
    get() { return (this as any)._onerror; },
    set(fn: ((event: unknown) => void) | null) { (this as any)._onerror = fn; },
    configurable: true,
  });

  return stream;
}

export async function stopGeneration(projectId: number): Promise<{ status: string; message: string }> {
  const res = await http.post(`/projects/${projectId}/sections/stop-generation`);
  return res.data;
}

export async function pauseGeneration(projectId: number): Promise<{ status: string; message: string; action: string }> {
  const res = await http.post(`/projects/${projectId}/sections/pause-generation`);
  return res.data;
}

export async function resumeGeneration(projectId: number): Promise<{ status: string; message: string; generation_id: string; action: string }> {
  const res = await http.post(`/projects/${projectId}/sections/resume-generation`);
  return res.data;
}

export async function getGenerationStatus(projectId: number): Promise<{
  has_active_task: boolean;
  generation_id?: string;
  task_status?: string;
  total_sections?: number;
  completed_count?: number;
  failed_count?: number;
  cancelled_count?: number;
  started_at?: string;
  paused_at?: string;
  sections?: Array<{ id: number; title: string; status: string; progress: number; quality_score?: number }>;
}> {
  const res = await http.get(`/projects/${projectId}/sections/generation-status`);
  return res.data;
}

// ========== API Key 管理 ==========

export interface ApiKeyInfo {
  key_id: number;
  name: string;
  api_key: string;
  prefix: string;
  permissions: string[];
  expires_at: string;
  warning?: string;
}

export interface ApiKeyListEntry {
  id: number;
  name: string;
  prefix: string;
  permissions: string[];
  rate_limit: number;
  usage_count: number;
  last_used_at?: string;
  expires_at?: string;
  is_active: boolean;
  created_at: string;
}

export async function createApiKey(
  name: string = "默认密钥",
  permissions: string[] = ["projects:read", "sections:read", "export:read"],
  expiresDays: number = 365,
): Promise<ApiKeyInfo> {
  const permStr = permissions.join(",");
  const res = await http.post<ApiKeyInfo>("/keys", null, {
    params: { name, permissions: permStr, expires_days: expiresDays },
  });
  return res.data;
}

export async function listApiKeys(): Promise<{ status: string; count: number; keys: ApiKeyListEntry[] }> {
  const res = await http.get<{ status: string; count: number; keys: ApiKeyListEntry[] }>("/keys");
  return res.data;
}

export async function revokeApiKey(keyId: number): Promise<{ status: string; message: string }> {
  const res = await http.delete<{ status: string; message: string }>(`/keys/${keyId}`);
  return res.data;
}

export async function getApiKeyUsage(keyId: number, days: number = 7): Promise<{
  status: string;
  usage: { key_id: number; days: number; total_usage: number; daily_breakdown: Array<{ day: string; cnt: number }> };
}> {
  const res = await http.get(`/keys/${keyId}/usage`, { params: { days } });
  return res.data;
}

export async function getWordBudget(projectId: number, level2Default: number = 3500): Promise<{
  level1_count: number;
  level2_count: number;
  level3_count: number;
  level1_min: number;
  level2_default: number;
  total_estimate: number;
  tier_key?: string;
  tier_label?: string;
  tier_target?: number;
  tier_tolerance?: number;
  level3_default?: number;
  l3_own_target?: number;
  l3_own_tolerance?: number;
  l3_own_lo?: number;
  l3_own_hi?: number;
  leaf_l3_count?: number;
  subtree_l3_count?: number;
}> {
  const res = await http.get(`/projects/${projectId}/sections/word-budget`, {
    params: { level2_default: level2Default },
  });
  return res.data;
}

export async function setSectionTargetWords(
  projectId: number,
  sectionId: number,
  targetWords: number | null,
): Promise<{ status: string; target_words: number | null }> {
  const res = await http.patch(
    `/projects/${projectId}/sections/${sectionId}/target-words`,
    null,
    { params: { target_words: targetWords ?? 0 } },
  );
  return res.data;
}

/**
 * 设置章节字数档位（2026-10-08 新增：三级目录控制）
 * tier: 'brief' | 'standard' | 'detailed' | 'long' | ''（空串=清除覆盖，跟随全局默认）
 */
export async function setSectionWordCountTier(
  projectId: number,
  sectionId: number,
  tier: string,
): Promise<{
  status: string;
  section_id: number;
  word_count_tier: string | null;
  tier_label: string;
  tier_target: number;
  tier_tolerance: number;
}> {
  const res = await http.patch(
    `/projects/${projectId}/sections/${sectionId}/word-count-tier`,
    null,
    { params: { word_count_tier: tier } },
  );
  return res.data;
}

// ─────────────────────────────────────────────────────────────────────────────
// Image Engine
// 已实现的后端路由：
//   - POST /{section_id}/generate-flowchart  ✅
//   - POST /{section_id}/generate-gantt      ✅
//   - POST /{section_id}/generate-illustration ✅
//   - POST /{section_id}/arrange-architecture ✅
//   - POST /{section_id}/generate-architecture ✅
//   - POST /{section_id}/arrange-labor       ✅
//   - POST /{section_id}/generate-labor      ✅
//   - POST /{section_id}/arrange-comparison  ✅
//   - POST /{section_id}/generate-comparison ✅
//
// 未实现：
//   - POST /{section_id}/arrange-flowchart   ✅ (已实现)
//
// arrangeFlowchart() 暂未被页面调用，保留供后续扩展
// 后端路由: POST /projects/{id}/sections/{section_id}/arrange-flowchart
// ─────────────────────────────────────────────────────────────────────────────
export async function arrangeFlowchart(projectId: number, sectionId: number): Promise<{ need_arrange: boolean; reason: string }> {
  const res = await http.post(`/projects/${projectId}/sections/${sectionId}/arrange-flowchart`);
  return res.data;
}

export async function arrangeArchitecture(projectId: number, sectionId: number): Promise<{ need_arrange: boolean; reason: string }> {
  const res = await http.post(`/projects/${projectId}/sections/${sectionId}/arrange-architecture`);
  return res.data;
}

export async function generateArchitecture(projectId: number, sectionId: number): Promise<{
  tree: Record<string, unknown>; mermaid: string;
}> {
  const res = await http.post(`/projects/${projectId}/sections/${sectionId}/generate-architecture`);
  return res.data;
}

export async function arrangeLabor(projectId: number, sectionId: number): Promise<{ need_arrange: boolean; reason: string }> {
  const res = await http.post(`/projects/${projectId}/sections/${sectionId}/arrange-labor`);
  return res.data;
}

export async function generateLabor(projectId: number, sectionId: number, writingLength: string = "standard"): Promise<{
  labor_data: Record<string, unknown>; valid: boolean;
}> {
  const res = await http.post(`/projects/${projectId}/sections/${sectionId}/generate-labor?writing_length=${encodeURIComponent(writingLength)}`);
  return res.data;
}

export async function arrangeComparison(projectId: number, sectionId: number): Promise<{ need_arrange: boolean; reason: string }> {
  const res = await http.post(`/projects/${projectId}/sections/${sectionId}/arrange-comparison`);
  return res.data;
}

export async function generateComparison(projectId: number, sectionId: number): Promise<{ mermaid_code: string; valid: boolean }> {
  const res = await http.post(`/projects/${projectId}/sections/${sectionId}/generate-comparison`);
  return res.data;
}

// 图表全部自动生成（正文内联围栏代码块），已移除人工配图 generate-illustration 前端 API。

// Export
export async function checkDocument(projectId: number, strict = false): Promise<DocumentCheckResult> {
  // BUG-FIX 2026-09-18（P2）：后端 /export/check 支持 strict 参数控制严格模式，
  // 此前前端永远不传，严格模式下的空章节/评分点缺口检查完全不可达。
  const res = await http.get(`/projects/${projectId}/export/check`, { params: { strict } });
  return res.data;
}

/**
 * 获取导出格式预设目录（版式 + 主题）。
 * ENHANCE 2026-09-14：新增端点，移植自易标 EXPORT_LAYOUT_PRESETS / EXPORT_THEME_PRESETS。
 */
export async function getExportPresets(): Promise<ExportPresetCatalog> {
  const res = await http.get(`/export/presets`, { silent: true } as any);
  return res.data;
}

export async function exportDocx(projectId: number, config?: ExportConfig): Promise<{  filename: string; file_path: string; file_size: number; export_id?: number; check: DocumentCheckResult; download_url?: string | null; relative_path?: string;
}> {
  const res = await httpPostLong<{
    filename: string; file_path: string; file_size: number; export_id?: number; check: DocumentCheckResult; download_url?: string | null; relative_path?: string;
  }>(`/projects/${projectId}/export/docx`, config || {});
  return res.data;
}

export interface ExportProgressEvent {
  event: "connected" | "start" | "progress" | "completed" | "error" | "keep_alive" | "log";
  step?: string;
  detail?: string;
  progress?: number;
  filename?: string;
  /** 下载 URL 口径（2026-10-04 起与同步响应统一；旧版为磁盘相对路径） */
  file_path?: string;
  /** 磁盘相对路径（相对 PROJECTS_DATA_DIR，运维排查用） */
  relative_path?: string;
  /** 下载 API URL（export_id 缺失时为 null） */
  download_url?: string | null;
  /** 断线续传流标识（connected 事件携带，2026-10-04 T3） */
  stream_id?: string;
  file_size?: number;
  project_id?: number;
  level?: string;
  content?: string;
  timestamp?: number;
  [key: string]: any;
}

/**
 * 通过 SSE 流式导出 DOCX，实时回调进度
 * @param onProgress 进度回调，参数 (event: ExportProgressEvent)
 * @returns 最终结果 Promise
 */
export function exportDocxStream(
  projectId: number,
  config: ExportConfig | undefined,
  onProgress: (event: ExportProgressEvent) => void,
): {
  promise: Promise<{
    filename: string; file_path: string; file_size: number; export_id?: number; record_failed?: boolean; check: DocumentCheckResult; download_url?: string | null; relative_path?: string;
  }>;
  abort: () => void;
} {
  let aborted = false;
  let abortController: AbortController | null = null;
  // ENHANCE 2026-10-04（T3）：断线续传状态 —— stream_id 来自后端 connected 事件，
  // lastEventId 来自 SSE 帧的 id: 行；非终态断流时尝试续传端点接回一次。
  let streamId: string | null = null;
  let lastEventId = 0;
  let resumed = false;

  // 清理函数
  const cleanup = () => {
    aborted = true;
    if (abortController) {
      try { abortController.abort(); } catch { /* ignore */ }
      abortController = null;
    }
  };

  let reject: (reason: any) => void;
  let resolve: (value: any) => void;

  return {
    promise: new Promise<{
      filename: string; file_path: string; file_size: number; export_id?: number; record_failed?: boolean; check: DocumentCheckResult; download_url?: string | null; relative_path?: string;
    }>((res, rej) => {
      resolve = res;
      reject = rej;

      let settled = false;
      const settleOk = (v: any) => { if (!settled) { settled = true; resolve(v); } };
      const settleErr = (e: any) => { if (!settled) { settled = true; reject(e); } };

      // 创建 AbortController 用于控制 fetch 超时
      abortController = new AbortController();
      const signal = abortController.signal;

      // EventSource 不支持 POST，使用 fetch + ReadableStream
      // BUG 修复：与大纲/正文流一致，走 SSE 直连 URL 绕过代理缓冲；
      // 原先硬编码相对路径，生产环境配置 VITE_API_BASE_URL 时导出流仍走代理，
      // 会被代理缓冲导致进度卡死/ERR_ABORTED。
      const url = toSSEFullUrl(`/api/v1/projects/${projectId}/export/docx-stream`);

      const fetchPromise = fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
        body: JSON.stringify(config || {}),
        signal,
      });

      // 消费一条 SSE 流（主连接与续传连接共用）；processLine 状态每次独立。
      const runConsume = async (body: ReadableStream<Uint8Array>) => {
        const reader = body.getReader();
        const decoder = new TextDecoder("utf-8");
        let buffer = "";
        let currentData = "";

        const processLine = (line: string) => {
          if (line === "") {
            // 事件结束
            if (currentData) {
              try {
                const parsed: ExportProgressEvent = JSON.parse(currentData);

                // 处理 keep_alive 事件（仅刷新步骤信息，不重置进度）
                if (parsed.event === "keep_alive") {
                  onProgress({ event: "keep_alive", step: "保持连接...", detail: "正在生成文档..." });
                  currentData = "";
                  return;
                }

                // ENHANCE 2026-10-04（T3）：记录续传所需的 stream_id
                if (parsed.event === "connected" && parsed.stream_id) {
                  streamId = String(parsed.stream_id);
                }

                onProgress(parsed);
                if (parsed.event === "completed") {
                  settleOk({
                    filename: parsed.filename || "",
                    file_path: parsed.file_path || "",
                    file_size: parsed.file_size || 0,
                    export_id: parsed.export_id,
                    // 后端 completed 事件新增 record_failed：导出记录写库失败时为 true
                    record_failed: parsed.record_failed,
                    // ENHANCE 2026-10-11（P3）：透传 SSE completed 携带的下载 URL 与磁盘
                    // 相对路径，令 SSE 路径与同步 /export/docx 路径的 ExportResult 字段对齐。
                    download_url: parsed.download_url,
                    relative_path: parsed.relative_path,
                    check: parsed.check || {} as any,
                  });
                } else if (parsed.event === "error") {
                  settleErr(new Error(parsed.detail || "导出失败"));
                }
              } catch (e) {
                /* ignore parse error */
              }
            }
            currentData = "";
            return;
          }
          // ENHANCE 2026-10-04（T3）：解析 id: 行（业务事件的单调序号，续传游标）
          if (line.startsWith("id:")) {
            const n = parseInt(line.slice(3).trim(), 10);
            if (!Number.isNaN(n)) lastEventId = n;
            return;
          }
          if (line.startsWith(":")) {
            // 注释/心跳
            return;
          }
          if (line.startsWith("data:")) {
            currentData += (currentData ? "\n" : "") + line.slice(5).trim();
          }
        };

        while (!aborted && !settled) {
          const { value, done } = await reader.read();
          if (done) return;
          buffer += decoder.decode(value, { stream: true });
          let idx;
          while ((idx = buffer.indexOf("\n")) >= 0) {
            const line = buffer.slice(0, idx).replace(/\r$/, "");
            buffer = buffer.slice(idx + 1);
            processLine(line);
            if (settled) return;
          }
        }
      };

      fetchPromise
        .then(async (response) => {
          if (!response.ok || !response.body) {
            let detail = `HTTP ${response.status}`;
            try {
              const j = await response.json();
              detail = j?.detail || detail;
            } catch {
              /* ignore */
            }
            // BUG-FIX 2026-09-15：把 HTTP 状态码挂到错误上，便于调用方区分
            // 409 锁冲突（预条件失败，不该降级重跑）与真正的传输/构建失败。
            const err = new Error(detail);
            (err as any).status = response.status;
            settleErr(err);
            return;
          }

          await runConsume(response.body);

          // ENHANCE 2026-10-04（T3）：流断且未收终态 → 携 last_event_id 续传一次。
          // 后端断线不中断构建，journal 已接回剩余事件；续传 404（过期/跨 worker）
          // 或二次断流时落入下方统一失败处理 → 调用方降级同步接口/导出历史兜底。
          if (!settled && !aborted && streamId && !resumed) {
            resumed = true;
            onProgress({ event: "log", level: "warn", content: "导出流连接中断，正在尝试续传..." });
            try {
              const resumeUrl = toSSEFullUrl(
                `/api/v1/projects/${projectId}/export/docx-stream/resume/${streamId}?last_event_id=${lastEventId}`,
              );
              const r = await fetch(resumeUrl, { headers: { Accept: "text/event-stream" }, signal });
              if (r.ok && r.body) {
                await runConsume(r.body);
              }
            } catch (e) {
              if (aborted) {
                settleErr(new Error("SSE 导出已被取消"));
                return;
              }
              // 续传失败：落到统一失败处理
            }
          }

          // BUG 修复：abort 发生在同步行处理期间时，循环直接 break 且
          // streamEnded=false，Promise 永不 settle（调用方 await 永久挂起）。
          // 统一在循环出口处理：已取消 → reject 取消；流异常结束且无终止事件 → reject。
          if (settled) return;
          if (aborted) {
            settleErr(new Error("SSE 导出已被取消"));
          } else {
            settleErr(new Error("SSE 流异常结束，未收到完成事件"));
          }
        })
        .catch((e: any) => {
          if (e?.name === 'AbortError') {
            console.warn("[exportDocxStream] 请求被用户取消");
            settleErr(new Error("SSE 导出已被取消"));
            return;
          }
          console.warn("[exportDocxStream] 导出流请求失败:", e?.message || e);
          settleErr(new Error("SSE 导出流请求失败: " + (e?.message || "未知错误")));
        });
    }),
    abort: () => {
      cleanup();
    },
  };
}

export async function getExportHistory(projectId: number): Promise<ExportRecord[]> {
  const res = await http.get(`/projects/${projectId}/export/history`);
  return res.data;
}

// BUG-FIX-41 修复：浏览器原生 <a href> 下载不携带 Authorization 头，
// AUTH_ENABLED=true 时导出下载端点 401。后端 JwtAuthMiddleware 已支持
// ?token=xxx 查询参数（BUG-FIX-24），此处追加 token 作为后备认证。
function _appendAuthToken(baseUrl: string): string {
  const token = getAuthToken();
  if (token) {
    const sep = baseUrl.includes("?") ? "&" : "?";
    return `${baseUrl}${sep}token=${encodeURIComponent(token)}`;
  }
  return baseUrl;
}

export function getExportDownloadUrl(projectId: number, exportId: number): string {
  return _appendAuthToken(`/api/v1/projects/${projectId}/export/download/${exportId}`);
}

export function getLatestExportUrl(projectId: number): string {
  return _appendAuthToken(`/api/v1/projects/${projectId}/export/latest`);
}

/**
 * ENHANCE 2026-10-04（T1）：把既有 DOCX 导出产物按需转换为 PDF 并触发浏览器下载。
 *
 * 走 fetch+blob（而非 <a href>）的原因：需要读取非 2xx 响应体里的中文 detail
 * （501 未安装 LibreOffice / 404 记录不存在 等）并展示给用户。
 * 鉴权与下载端点一致：?token= 查询参数（JwtAuthMiddleware 支持）。
 * @throws Error message 为后端 detail（已含安装/配置指引），调用方直接 message.error
 */
export async function exportPdf(projectId: number, exportId: number): Promise<void> {
  const url = _appendAuthToken(`/api/v1/projects/${projectId}/export/pdf/${exportId}`);
  const resp = await fetch(url);
  if (!resp.ok) {
    let detail = `HTTP ${resp.status}`;
    try {
      const j = await resp.json();
      detail = j?.detail || detail;
    } catch {
      /* ignore */
    }
    const err = new Error(detail);
    (err as any).status = resp.status;
    throw err;
  }
  const blob = await resp.blob();
  // 文件名优先取 Content-Disposition，回退为「导出文档.pdf」
  let filename = "导出文档.pdf";
  const cd = resp.headers.get("Content-Disposition") || "";
  const mStar = /filename\*=UTF-8''([^;]+)/i.exec(cd);
  const mPlain = /filename="?([^";]+)"?/i.exec(cd);
  try {
    if (mStar) filename = decodeURIComponent(mStar[1]);
    else if (mPlain) filename = mPlain[1];
  } catch {
    /* ignore */
  }
  const objectUrl = URL.createObjectURL(blob);
  try {
    const a = document.createElement("a");
    a.href = objectUrl;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
  } finally {
    URL.revokeObjectURL(objectUrl);
  }
}

// AI Config
export async function getAIConfig(): Promise<AIConfig> {
  const res = await http.get<AIConfig>("/ai/config");
  return res.data;
}

export async function saveAIConfig(config: AIConfig): Promise<void> {
  await http.put("/ai/config", config);
}

// ========== Image Model Config APIs ==========
export interface ImageModelConfig {
  provider: string;
  model: string;
  base_url: string;
  api_key: string;
  max_concurrency: number;
  max_per_project: number;
  quality: string;
  style_injection: string;
  enabled: boolean;
  default_size: string;
  default_ratio: string;
  price_per_image: number;
}

export async function getImageModelConfig(user_id?: number): Promise<ImageModelConfig> {
  const res = await http.get<ImageModelConfig>("/ai/image-model", { params: user_id ? { user_id } : {} });
  return res.data;
}

export async function saveImageModelConfig(config: Omit<ImageModelConfig, 'is_default'>): Promise<void> {
  await http.put("/ai/image-model", config);
}

export async function testImageConnection(config: Omit<ImageModelConfig, 'is_default'>): Promise<{ status: string; message: string }> {
  const res = await http.post("/ai/image-model/test", config);
  return res.data;
}

export async function fetchKnownProviders(): Promise<KnownProvider[]> {
  const res = await http.get("/ai/providers");
  return res.data;
}

export async function getImageModelProviders(): Promise<{ id: string; name: string; price: string; is_default: boolean }[]> {
  const res = await http.get("/ai/image-model/providers");
  return res.data;
}

export async function getImageUsageStats(user_id?: number): Promise<{
  total: number;
  monthly: number;
  success_count: number;
  success_rate: number;
  total_cost: number;
  platform_distribution: Record<string, number>;
}> {
  const res = await http.get("/ai/image-model/usage", { params: user_id ? { user_id } : {} });
  return res.data;
}

// ========== 图像生成任务 ==========
export interface ImageGenTask {
  id: string;
  project_id?: number | string | null;
  chapter_id?: number | string | null;
  provider: string;
  model: string;
  prompt: string;
  injected_prompt?: string | null;
  size?: string | null;
  ratio?: string | null;
  status: string;
  image_url?: string | null;
  image_path?: string | null;
  cost?: number | null;
  retry_count?: number | null;
  error_message?: string | null;
  created_at?: string | null;
  completed_at?: string | null;
}

export async function getImageTasks(params?: {
  project_id?: number | string;
  status?: string;
  limit?: number;
  offset?: number;
}): Promise<{ tasks: ImageGenTask[]; total: number }> {
  const res = await http.get<{ tasks: ImageGenTask[]; total: number }>("/images/tasks", {
    params: { limit: 50, ...params },
  });
  return res.data;
}

/**
 * 构造任务图片的展示 URL：
 * - 本地已落盘（image_path）优先走后端文件端点，远程 URL 过期后仍可预览；
 * - 否则回退远程 image_url（可能已过期，前端 <img> onError 可兜底隐藏）。
 */
export function resolveImageTaskSrc(task: ImageGenTask): string {
  if (task.image_path) {
    return `/api/v1/images/files/${encodeURIComponent(task.id)}`;
  }
  if (task.image_url) {
    return task.image_url;
  }
  return "";
}

export async function fetchModels(apiKey: string, baseUrl: string): Promise<{ models: string[]; suggest_alternatives?: Array<{ name: string; base_url: string; description: string }> }> {
  const res = await http.post<{ models: string[]; suggest_alternatives?: Array<{ name: string; base_url: string; description: string }> }>("/ai/models", { api_key: apiKey, base_url: baseUrl });
  return res.data;
}

// ========== Per-Provider Config APIs (厂商隔离配置) ==========

export interface ModelInfo {
  model: string;
  max_tokens: number;
  concurrency: number;
  source: "builtin" | "api" | "default";
}

// 文本：获取所有厂商配置
export async function getTextProviderConfigs(): Promise<{ configs: TextProviderConfig[] }> {
  const res = await http.get<{ configs: TextProviderConfig[] }>("/ai/provider-configs");
  return res.data;
}

// 文本：获取单个厂商配置
export async function getTextProviderConfig(providerName: string): Promise<TextProviderConfig> {
  const res = await http.get<TextProviderConfig>(`/ai/provider-configs/${encodeURIComponent(providerName)}`);
  return res.data;
}

// 文本：保存单个厂商配置
export async function saveTextProviderConfig(config: TextProviderConfig): Promise<void> {
  await http.put(`/ai/provider-configs/${encodeURIComponent(config.provider_name)}`, config);
}

// 文本：激活厂商配置（同步到主表）
export async function activateTextProviderConfig(providerName: string): Promise<void> {
  await http.put(`/ai/provider-configs/${encodeURIComponent(providerName)}/activate`);
}

// 图像：获取所有厂商配置
export async function getImageProviderConfigs(): Promise<{ configs: ImageProviderConfig[] }> {
  const res = await http.get<{ configs: ImageProviderConfig[] }>("/ai/image-provider-configs");
  return res.data;
}

// 图像：获取单个厂商配置
export async function getImageProviderConfig(providerName: string): Promise<ImageProviderConfig> {
  const res = await http.get<ImageProviderConfig>(`/ai/image-provider-configs/${encodeURIComponent(providerName)}`);
  return res.data;
}

// 图像：保存单个厂商配置
export async function saveImageProviderConfig(config: ImageProviderConfig): Promise<void> {
  await http.put(`/ai/image-provider-configs/${encodeURIComponent(config.provider_name)}`, config);
}

// 图像：激活厂商配置
export async function activateImageProviderConfig(providerName: string): Promise<void> {
  await http.put(`/ai/image-provider-configs/${encodeURIComponent(providerName)}/activate`);
}

// 模型元数据自动获取（增强版：内置表 + API 获取）
export async function fetchModelInfo(data: {
  api_key: string;
  base_url: string;
  model: string;
  provider: string;
}): Promise<ModelInfo> {
  const res = await http.post<ModelInfo>("/ai/model-info", data);
  return res.data;
}

export async function testAIConnection(config: {
  provider: string; api_key: string; model: string; base_url: string;
  max_tokens?: number; temperature?: number;
}): Promise<{ status: string; message: string }> {
  const res = await http.post("/ai/test", config);
  return res.data;
}

// HARDENING 2026-10-11（G-6 前端集成）：显式清空主 AI 配置的已保存 API Key
export async function clearAiConfigApiKey(): Promise<{ status: string }> {
  const res = await http.post("/ai/config/clear-api-key");
  return res.data;
}

// HARDENING 2026-10-11（G-5 前端集成）：查询配置变更审计日志（支持 action/target 过滤）
export async function getConfigAudit(params?: {
  limit?: number;
  action?: string;
  target?: string;
}): Promise<Array<{ actor: string; action: string; target: string; detail: string; created_at: string }>> {
  const res = await http.get("/ai/config-audit", { params });
  return res.data;
}

// ========== AI 快速配置 ==========
export async function quickSetup(data: {
  api_key: string;
  provider_name?: string;
  model?: string;
}): Promise<{
  success: boolean;
  message: string;
  applied_config?: Record<string, any>;
  tested_providers: Array<Record<string, any>>;
}> {
  const res = await http.post("/ai/quick-setup", data);
  return res.data;
}

export async function imageQuickSetup(data: {
  api_key: string;
  provider_name?: string;
}): Promise<{
  success: boolean;
  message: string;
  applied_config?: Record<string, any>;
  tested_providers: Array<Record<string, any>>;
}> {
  const res = await http.post("/ai/image-model/quick-setup", data);
  return res.data;
}

// ========== AI Profile 多配置系统 ==========
export async function fetchAIProfiles(): Promise<AIPProfile[]> {
  const res = await http.get("/ai/profiles");
  return res.data;
}

export async function getAIDefaultProfile(): Promise<AIPProfile> {
  const res = await http.get("/ai/profiles/default", { silent: true } as any);
  return res.data;
}

export async function createAIPProfile(data: Partial<AIPProfile>): Promise<AIPProfile> {
  const res = await http.post<AIPProfile>("/ai/profiles", data);
  return res.data;
}

export async function updateAIPProfile(name: string, data: Partial<AIPProfile>): Promise<AIPProfile> {
  const res = await http.put<AIPProfile>(`/ai/profiles/${encodeURIComponent(name)}`, data);
  return res.data;
}

export async function deleteAIPProfile(name: string): Promise<void> {
  await http.delete(`/ai/profiles/${encodeURIComponent(name)}`);
}

export async function setAIDefaultProfile(name: string): Promise<{ status: string; profile: string }> {
  const res = await http.post(`/ai/profiles/${encodeURIComponent(name)}/set-default`);
  return res.data;
}

export async function applyAIPProfile(name: string): Promise<{ status: string }> {
  const res = await http.post(`/ai/profiles/${encodeURIComponent(name)}/apply`);
  return res.data;
}

export async function exportAIPProfiles(): Promise<{ version: string; exported_at: string; profiles: any[] }> {
  const res = await http.get("/ai/profiles/export");
  return res.data;
}

export async function importAIPProfiles(data: { profiles: any[] }): Promise<{ status: string; imported: number; updated: number; skipped?: number; total?: number }> {
  const res = await http.post("/ai/profiles/import", data);
  return res.data;
}

// ========== AI 用量统计与健康度 ==========
export async function getUsageSummary(days?: number, task_type?: string, profile?: string): Promise<UsageSummary> {
  const params: Record<string, any> = {};
  if (days) params.days = days;
  if (task_type) params.task_type = task_type;
  if (profile) params.profile = profile;
  const res = await http.get("/ai/usage/summary", { params });
  return res.data;
}

export async function getUsageDashboard(): Promise<{
  summary: UsageSummary;
  daily_trend: DailyTrend[];
  budget: BudgetStatus;
  active_profile: string;
}> {
  const res = await http.get("/ai/usage/dashboard");
  return res.data;
}

export async function getHealthDashboard(): Promise<{ providers: HealthProvider[]; total_records: number }> {
  const res = await http.get("/ai/health/dashboard");
  return res.data;
}

export async function getUsageTrend(days?: number): Promise<DailyTrend[]> {
  const res = await http.get("/ai/usage/trend", { params: days ? { days } : {} });
  return res.data;
}

export async function truncateUsageStats(days?: number): Promise<{ status: string; deleted_records: number }> {
  const res = await http.delete("/ai/usage/truncate", { params: days ? { days } : {} });
  return res.data;
}

// Prompts
export async function fetchPrompts(): Promise<PromptTemplate[]> {
  const res = await http.get<PromptTemplate[]>("/prompts");
  return res.data;
}

export async function updatePrompt(key: string, content: string, force = false): Promise<void> {
  await http.put(`/prompts/${key}${force ? "?force=true" : ""}`, { content });
}

export async function resetPrompt(key: string): Promise<void> {
  await http.post(`/prompts/${key}/reset`);
}

/** 获取提示词历史版本列表（按时间倒序，不含正文） */
export async function fetchPromptVersions(key: string): Promise<PromptVersionMeta[]> {
  const res = await http.get<PromptVersionMeta[]>(`/prompts/${key}/versions`);
  return res.data;
}

/** 获取某个历史版本的完整内容 */
export async function fetchPromptVersion(key: string, versionId: number): Promise<PromptVersionDetail> {
  const res = await http.get<PromptVersionDetail>(`/prompts/${key}/versions/${versionId}`);
  return res.data;
}

/** 回滚到指定历史版本 */
export async function rollbackPrompt(key: string, versionId: number): Promise<void> {
  await http.post(`/prompts/${key}/versions/${versionId}/rollback`);
}

// ========== 提示词监控模块 ==========

export interface PromptMetrics {
  calls: number;
  db_hits: number;
  fallback_hits: number;
  not_found: number;
  total_chars: number;
  total_tokens: number;
}

export interface PromptMetricsResponse {
  status: string;
  summary: {
    total_prompts: number;
    total_calls: number;
    total_estimated_tokens: number;
  };
  metrics: Record<string, PromptMetrics>;
}

/** 获取提示词使用监控指标 */
export async function fetchPromptMetrics(): Promise<PromptMetricsResponse> {
  const res = await http.get<PromptMetricsResponse>("/prompts/metrics");
  return res.data;
}

export interface PromptAlert {
  level: string;
  type: string;
  key: string;
  message: string;
}

export interface PromptAlertsResponse {
  status: string;
  alert_count: number;
  alerts: PromptAlert[];
}

/** 获取提示词监控告警 */
export async function fetchPromptAlerts(): Promise<PromptAlertsResponse> {
  const res = await http.get<PromptAlertsResponse>("/prompts/alerts");
  return res.data;
}

/** 同步硬编码提示词到数据库 */
export async function syncHardcodePrompts(overwrite: boolean = false): Promise<{
  status: string;
  message: string;
  synced: string[];
  skipped: string[];
  total: number;
}> {
  const res = await http.post(`/prompts/sync-hardcode?overwrite=${overwrite}`);
  return res.data;
}

// 历史数据点
export interface MetricsHistoryPoint {
  timestamp: number;
  total_calls: number;
  total_tokens: number;
  total_db_hits: number;
}

export interface MetricsHistoryResponse {
  status: string;
  history: MetricsHistoryPoint[];
  summary: {
    total_points: number;
    db_hit_ratio: number;
  };
}

/** 获取提示词监控历史数据 */
export async function fetchMetricsHistory(hours: number = 24): Promise<MetricsHistoryResponse> {
  const res = await http.get<MetricsHistoryResponse>(`/prompts/metrics/history?hours=${hours}`);
  return res.data;
}

/** 手动触发一次指标快照 */
export async function triggerMetricsSnapshot(): Promise<{
  status: string;
  snapshot: MetricsHistoryPoint;
}> {
  const res = await http.post("/prompts/metrics/snapshot");
  return res.data;
}

export interface CategoryStats {
  category: string;
  calls: number;
  total_tokens: number;
  prompt_count: number;
}

export interface CategoryStatsResponse {
  status: string;
  categories: CategoryStats[];
}

/** 按分类统计提示词调用量 */
export async function fetchMetricsByCategory(): Promise<CategoryStatsResponse> {
  const res = await http.get<CategoryStatsResponse>("/prompts/metrics/by-category");
  return res.data;
}

// ========== 废标检查模块 ==========

export async function startRejectionCheck(req: RejectionStartRequest): Promise<{
  task_id: number; status: string; check_items: RejectionCheckItem[];
}> {
  const res = await http.post("/rejection/start", req);
  return res.data;
}

export async function getRejectionStatus(task_id: number): Promise<{
  id: number; status: string; progress: number; progress_message: string; error: string;
}> {
  const res = await http.get(`/rejection/status/${task_id}`);
  return res.data;
}

export async function getRejectionResult(task_id: number): Promise<RejectionCheckTask> {
  const res = await http.get(`/rejection/result/${task_id}`);
  return res.data;
}

export async function markFalsePositive(task_id: number, item_id: string, is_false_positive: boolean = true): Promise<{
  task_id: number; false_positives: string[];
}> {
  const res = await http.post("/rejection/mark-false-positive", {
    item_id, is_false_positive,
  }, { params: { task_id } });
  return res.data;
}

export async function retryRejectionDimension(task_id: number, dimension: string): Promise<{
  task_id: number; status: string;
}> {
  const res = await http.post("/rejection/retry-dimension", { dimension }, { params: { task_id } });
  return res.data;
}

export function getRejectionReportUrl(
  task_id: number,
  format: "docx" | "xlsx" = "docx",
): string {
  // BUG-FIX 2026-09-18（P1）：与 getExportDownloadUrl 同样需要携带 token——
  // 浏览器原生 <a href> 下载不发 Authorization 头，AUTH_ENABLED=true 时 401。
  return _appendAuthToken(`/api/v1/rejection/export-report/${task_id}?format=${format}`);
}

export async function listRejectionTasks(project_id?: number, limit: number = 20): Promise<RejectionCheckTask[]> {
  const res = await http.get("/rejection/tasks", { params: { project_id, limit } });
  return res.data;
}

export async function extractRejectionItems(tender_text: string): Promise<{ items: RejectionCheckItem[] }> {
  const res = await http.post("/rejection/extract-items", { tender_text });
  return res.data;
}

export async function importRejectionFromProject(project_id: number): Promise<{
  tender_text: string; bid_text: string; project_id: number;
}> {
  const res = await http.get(`/rejection/import-from-project/${project_id}`);
  return res.data;
}

export async function getKnowledgeSuggestions(project_id: number): Promise<{
  status: string; suggestions: any[];
}> {
  const res = await http.get(`/rejection/knowledge-suggestions/${project_id}`);
  return res.data;
}

// ========== 报告调度 ==========

export async function scheduleReport(req: {
  task_id: number;
  schedule_type?: string;
  schedule_time?: string;
  enabled?: boolean;
}): Promise<{ status: string; schedule_id: number; message: string }> {
  const res = await http.post("/rejection/schedule-report", req);
  return res.data;
}

export async function getReportSchedules(task_id: number): Promise<{
  total: number;
  items: any[];
}> {
  const res = await http.get(`/rejection/schedules/${task_id}`);
  return res.data;
}

export async function updateScheduleStatus(schedule_id: number, enabled: boolean): Promise<{
  status: string;
  enabled: boolean;
}> {
  const res = await http.patch(`/rejection/schedule/${schedule_id}/enable`, null, {
    params: { enabled },
  });
  return res.data;
}

export async function deleteSchedule(schedule_id: number): Promise<{
  status: string;
  message: string;
}> {
  const res = await http.delete(`/rejection/schedule/${schedule_id}`);
  return res.data;
}

// ========== 批量操作 ==========

export async function batchDeleteRejectionTasks(task_ids: number[]): Promise<{
  status: string; success: number; failed: number; message?: string;
}> {
  const res = await http.post("/rejection/batch/delete", { task_ids });
  return res.data;
}

export async function batchMarkFalsePositive(task_id: number, item_ids: string[]): Promise<{
  status: string; task_id: number; false_positives: string[];
}> {
  const res = await http.post("/rejection/batch/mark-false-positive", { task_id, item_ids });
  return res.data;
}

export async function getRejectionTasksByProject(project_id: number, limit: number = 100): Promise<{
  total: number; items: RejectionCheckTask[];
}> {
  const res = await http.get(`/rejection/project-tasks/${project_id}`, { params: { limit } });
  return res.data;
}

// ========== 标书查重模块 ==========

export async function startDuplicateCheck(req: DuplicateStartRequest): Promise<{
  task_id: number; status: string; cached: boolean;
}> {
  const res = await http.post("/duplicate/start", req);
  return res.data;
}

export async function getDuplicateStatus(task_id: number): Promise<{
  id: number; status: string; progress: number; progress_message: string; error: string;
}> {
  const res = await http.get(`/duplicate/status/${task_id}`);
  return res.data;
}

export async function getDuplicateResult(task_id: number): Promise<DuplicateCheckTask> {
  const res = await http.get(`/duplicate/result/${task_id}`);
  return res.data;
}

export function getDuplicateReportUrl(
  task_id: number,
  format: "docx" | "xlsx" = "docx",
): string {
  // BUG-FIX 2026-09-18（P1）：补齐下载 token，理由同 getRejectionReportUrl。
  return _appendAuthToken(`/api/v1/duplicate/export-report/${task_id}?format=${format}`);
}

export async function listDuplicateTasks(limit: number = 20, project_id?: number): Promise<DuplicateCheckTask[]> {
  // 后端 /duplicate/tasks 已支持按项目过滤（created/查询链路接通 project_id）
  const res = await http.get("/duplicate/tasks", { params: { limit, project_id } });
  return res.data;
}

// ========== 知识库模块 ==========

export async function uploadKnowledgeDocument(
  file: File,
  industry: string = "",
): Promise<{ doc_id: number; status: string; filename: string }> {
  const formData = new FormData();
  formData.append("file", file);
  formData.append("industry", industry);
  const res = await http.post("/knowledge/documents", formData);
  return res.data;
}

export async function listKnowledgeDocuments(limit: number = 50): Promise<KnowledgeDocument[]> {
  const res = await http.get("/knowledge/documents", { params: { limit } });
  return res.data;
}

export async function getKnowledgeDocumentStatus(doc_id: number): Promise<{
  id: number; filename: string; status: string; coverage: number;
  step_log: Array<{ step: string; status: string; message: string; timestamp: string }>;
  error: string;
}> {
  const res = await http.get(`/knowledge/documents/${doc_id}/status`);
  return res.data;
}

export async function deleteKnowledgeDocument(doc_id: number): Promise<void> {
  await http.delete(`/knowledge/documents/${doc_id}`);
}

export async function searchKnowledgeItems(params: {
  keyword?: string; industry?: string; tag?: string; limit?: number; offset?: number;
} = {}): Promise<KnowledgeSearchResult> {
  const res = await http.get("/knowledge/items", { params });
  return res.data;
}

export async function getKnowledgeItem(item_id: number): Promise<KnowledgeItem> {
  const res = await http.get(`/knowledge/items/${item_id}`);
  return res.data;
}

export async function updateKnowledgeItem(item_id: number, data: KnowledgeItemUpdate): Promise<KnowledgeItem> {
  const res = await http.put(`/knowledge/items/${item_id}`, data);
  return res.data;
}

export async function deleteKnowledgeItem(item_id: number): Promise<void> {
  await http.delete(`/knowledge/items/${item_id}`);
}

export async function listKnowledgeIndustries(): Promise<{ industries: string[] }> {
  const res = await http.get("/knowledge/industries");
  return res.data;
}

export async function listKnowledgeTags(): Promise<{ tags: string[] }> {
  const res = await http.get("/knowledge/tags");
  return res.data;
}

export async function migrateKnowledgeFromV2(): Promise<{
  migrated: boolean; migrated_count?: number; skipped_count?: number; message?: string;
}> {
  const res = await http.post("/knowledge/migrate-from-v2");
  return res.data;
}

export async function getKnowledgeStats(): Promise<KnowledgeStats> {
  const res = await http.get("/knowledge/stats");
  return res.data;
}

// ========== 多语言支持 ==========

export interface LanguageInfo {
  code: string;
  name: string;
  nativeName: string;
  direction: 'ltr' | 'rtl';
}

export interface LanguageResponse {
  status: string;
  default_language: string;
  languages: LanguageInfo[];
}

export async function getLanguages(): Promise<LanguageResponse> {
  const res = await http.get<LanguageResponse>("/i18n/languages");
  return res.data;
}

export async function translateText(key: string, language: string): Promise<{
  status: string; key: string; language: string; text: string;
}> {
  const res = await http.get<{ status: string; key: string; language: string; text: string }>(
    "/i18n/translate",
    { params: { key, language } },
  );
  return res.data;
}

export async function setLanguage(language: string): Promise<{
  status: string; message: string; current_language: string;
}> {
  const res = await http.post<{ status: string; message: string; current_language: string }>(
    "/i18n/set-language",
    null,
    { params: { language } },
  );
  return res.data;
}

export async function getPromptTemplates(language: string = "zh-CN"): Promise<{
  status: string; language: string; prompts: Record<string, string>;
}> {
  const res = await http.get<{ status: string; language: string; prompts: Record<string, string> }>(
    "/i18n/prompt-templates",
    { params: { language } },
  );
  return res.data;
}

// ========== 全文一致性审计 ==========

export interface Conflict {
  conflict_id: string;
  fact_name: string;
  values: string[];
  sources: string[];
  severity: string;
  description: string;
  /**
   * BUG-FIX 2026-10-08：后端此前把已解决（resolved=1）的历史冲突与待处理冲突
   * 一并返回，前端表格混排、用户无法分辨哪些还要动手。现后端默认只回未解决，
   * 并显式下发状态字段供 UI 区分与筛选。
   */
  /** 0=未解决 / 1=已解决（历史保留） */
  resolved?: number;
  /** 规范化状态名：open | resolved */
  state?: string;
  created_at?: string;
  resolved_value?: string | null;
  resolved_at?: string | null;
}

export interface AuditResult {
  status: string;
  /**
   * BUG-FIX 2026-10-08（P1·谎报成功）：`status` 恒表示「HTTP 请求是否成功」，
   * 审计本身的结论在 `audit_status`（completed / completed_no_conflicts / failed）。
   * 此前后端审计内部崩溃被降级成全 0 且路由无条件回 status='ok'，前端据此弹
   * 「审计完成，发现 0 处冲突」—— 实为谎报，真实结论是审计崩了。
   */
  audit_status?: string;
  project_id: number;
  total_sections: number;
  facts_extracted: number;
  conflicts_found: number;
  conflicts: Conflict[];
  audit_time: string;
  message?: string;
}

/** 冲突过滤口径：open=仅未解决(默认) / resolved=仅已解决 / all=全部 */
export type ConflictFilter = "open" | "resolved" | "all";

export async function runConsistencyAudit(projectId: number): Promise<AuditResult> {
  const res = await http.get<AuditResult>(`/consistency/audit/${projectId}`);
  return res.data;
}

export async function getConflicts(
  projectId: number,
  opts: { resolved?: ConflictFilter } = {},
): Promise<{
  status: string; count: number; filter: string; conflicts: Conflict[];
}> {
  const res = await http.get<{ status: string; count: number; filter: string; conflicts: Conflict[] }>(
    `/consistency/conflicts/${projectId}`,
    { params: { resolved: opts.resolved ?? "open" } },
  );
  return res.data;
}

// ---------------------------------------------------------------------------
// 审核规则命名空间（2026-10-08 新增，后端 GET /consistency/rules）
// ---------------------------------------------------------------------------

/**
 * 严重度取值/排序/文案配色的单一事实来源（后端 `services/consistency_rules.py`）。
 *
 * BUG-FIX 2026-10-08：此前「严重/中等/轻微」的取值、排序与配色在前端 `utils.ts`
 * 与后端 auditor 各写一套且无任何一致性约束 —— 后端调过阈值或文案时前端不会跟着变。
 * 页面启动时拉一次对齐，拉取失败则退回 `utils.ts` 的静态兜底（保持离线可用）。
 */
export interface ConsistencyRules {
  status: string;
  severity: {
    order: string[];
    default: string;
    unknown_rank: number;
    meta: Record<string, { label: string; color: string }>;
  };
  facts: {
    base_names: string[];
    critical: string[];
    high_distinct_threshold: number;
  };
  audit_status: {
    success: string[];
    failed: string;
    completed: string;
    no_conflicts: string;
  };
  conflict_state: { open: string; resolved: string };
  scoring_status: { meta: Record<string, { label: string; color: string }> };
}

export async function getConsistencyRules(): Promise<ConsistencyRules> {
  const res = await http.get<ConsistencyRules>(`/consistency/rules`);
  return res.data;
}

export async function getFixSuggestion(conflictId: string, projectId: number): Promise<{
  status: string; suggestion?: string; target_value?: string | null;
}> {
  const res = await http.get<{ status: string; suggestion?: string; target_value?: string | null }>(
    `/consistency/conflicts/${conflictId}/fix-suggestion`,
    { params: { project_id: projectId } },
  );
  return res.data;
}

// BUG-FIX 2026-09-14（高）：原后端 autofix 只标记冲突已解决、**从不改写正文**，
// 前端却无条件弹「已应用修复」——用户以为正文改了，实际一个字没动。
// 现在后端返回真实报告，前端必须按 status 区分展示。
export interface AutoFixSection {
  section_id: number;
  title: string;
  replacements: number;
}

export interface AutoFixReport {
  status: "ok" | "no_change";
  success: boolean;
  dry_run: boolean;
  unified_value: string;
  fact_name: string;
  replaced_values: string[];
  sections: AutoFixSection[];
  changed_sections: number;
  total_replacements: number;
  skipped: { section_id: number; title: string; reason: string }[];
  message: string;
}

export async function applyAutoFix(
  conflictId: string,
  projectId: number,
  newValue: string,
  dryRun = false,
): Promise<AutoFixReport> {
  const res = await http.post<AutoFixReport>(
    `/consistency/conflicts/${conflictId}/autofix`,
    null,
    { params: { project_id: projectId, new_value: newValue, dry_run: dryRun } },
  );
  return res.data;
}

// AI 补丁修复（移植自易标 auditing 阶段）：处理「同一事实用不同措辞表达」的语义冲突。
// AI 只产出 {old_text,new_text} 局部补丁，正文由后端精确改写；编造的 old_text 会被拒绝。
export interface AiRepairPatchResult {
  section_id: string;
  title: string;
  index: number;
  reason: string;
  old_text: string | null;
  new_text: string | null;
  applied: boolean;
  error: string | null;
}

export interface AiRepairReport {
  status: string;
  success: boolean;
  dry_run: boolean;
  project_id: number;
  facts_used: number;
  conflicts_used: number;
  batches: number;
  patches: unknown[];
  applied: AiRepairPatchResult[];
  rejected: AiRepairPatchResult[];
  sections: { section_id: string; title: string; applied: number; rejected: number }[];
  unresolved: { section_id: string; reason: string }[];
  conflict_states: { resolved: number; still_open: number } | null;
  errors: string[];
  message: string;
}

// ---------------------------------------------------------------------------
// 评分标准达标对齐（2026-10-06 新增，后端 GET /consistency/scoring-compliance/{id}）
// ---------------------------------------------------------------------------

/** 单个评分点的达标判定（与后端 ScoringGap.to_dict 字段一一对应） */
export interface ScoringGap {
  scoring_point_id: string;
  name: string;
  weight: string;
  standard: string;
  /** unmapped=目录没挂章节；empty=挂了但正文太空；weak=有正文但疑似没答到点上；covered=达标 */
  status: "unmapped" | "empty" | "weak" | "covered" | string;
  /** 评分标准用词在章节正文中的覆盖率 0~1（后端确定性 n-gram 计算，非 AI 判断） */
  coverage: number;
  section_ids: number[];
  section_titles: string[];
  content_chars: number;
  detail: string;
}

export interface ScoringComplianceResult {
  status: string;
  project_id: number;
  audit_time: string;
  audit_status: string;
  total_points: number;
  unmapped: number;
  empty: number;
  weak: number;
  covered: number;
  gaps: ScoringGap[];
  warnings: string[];
}

export async function getScoringCompliance(
  projectId: number,
  opts: { persist?: boolean; coverageWeak?: number } = {},
): Promise<ScoringComplianceResult> {
  const res = await http.get<ScoringComplianceResult>(
    `/consistency/scoring-compliance/${projectId}`,
    {
      params: {
        persist: opts.persist ?? true,
        ...(opts.coverageWeak !== undefined ? { coverage_weak: opts.coverageWeak } : {}),
      },
    },
  );
  return res.data;
}

export async function repairConsistencyWithAi(
  projectId: number,
  dryRun = false,
  sectionIds?: number[],
): Promise<AiRepairReport> {
  const res = await http.post<AiRepairReport>(`/consistency/repair/${projectId}`, null, {
    params: {
      dry_run: dryRun,
      ...(sectionIds && sectionIds.length ? { section_ids: sectionIds.join(",") } : {}),
    },
  });
  return res.data;
}

export interface AuditSummary {
  latest_audit: Record<string, unknown> | null;
  /**
   * 未解决冲突数。与 `conflicts_by_severity` 同一口径（都只算未解决）——
   * BUG-FIX 2026-10-08：此前 `conflicts_by_severity` 未过滤 `resolved`，把已解决的
   * 也算进「严重/中等」卡片，同屏两个卡片口径打架。
   */
  unresolved_conflicts: number;
  /** 已解决（历史）冲突数，2026-10-08 新增 */
  resolved_conflicts?: number;
  /** 仅统计**未解决**冲突的严重度分布 */
  conflicts_by_severity: Record<string, number>;
}

export async function getAuditSummary(projectId: number): Promise<{
  status: string; summary: AuditSummary;
}> {
  const res = await http.get<{
    status: string; summary: AuditSummary;
  }>(`/consistency/summary/${projectId}`);
  return res.data;
}

// ---------------------------------------------------------------------------
// Agent 模式全文一致性修复（E-1，后端 POST /consistency/agent/{projectId}）
// ---------------------------------------------------------------------------

export interface AgentRepairReport {
  status: string;
  success: boolean;
  dry_run: boolean;
  project_id: number;
  turns: number;
  max_turns: number;
  conflict_states: { resolved: number; still_open: number } | null;
  changed_sections: { id: number; title: string }[];
  applied: unknown[];
  rejected: unknown[];
  unresolved: { section_id: string; reason: string }[];
  errors: string[];
  message: string;
}

export async function repairConsistencyWithAgent(
  projectId: number,
  opts: { dryRun?: boolean; maxTurns?: number } = {},
): Promise<AgentRepairReport> {
  const res = await http.post<AgentRepairReport>(`/consistency/agent/${projectId}`, null, {
    params: {
      dry_run: opts.dryRun ?? false,
      max_turns: opts.maxTurns ?? 8,
    },
    timeout: 600000, // Agent 多轮往返可能耗时较长
  });
  return res.data;
}

// ---------------------------------------------------------------------------
// 事实回流（E-5，审计结果 → 全局事实候选）
// ---------------------------------------------------------------------------

export interface FactProposal {
  conflict_id: string;
  fact_name: string;
  value: string;
  occurrences: number;
  category: string;
  suggested_key: string;
  occurrences_map: { value: string; count: number }[];
}

export async function proposeFactsFromConflicts(
  projectId: number,
  opts: { minOccurrences?: number } = {},
): Promise<{
  status: string;
  proposals: FactProposal[];
  skipped: number;
  conflicts_scanned: number;
}> {
  const res = await http.get<{
    status: string;
    proposals: FactProposal[];
    skipped: number;
    conflicts_scanned: number;
  }>(`/consistency/facts/proposals/${projectId}`, {
    params: { min_occurrences: opts.minOccurrences ?? 3 },
  });
  return res.data;
}

export async function acceptFactProposals(
  projectId: number,
  proposals: FactProposal[],
): Promise<{ status: string; added: number; merged_total: number }> {
  const res = await http.post<{ status: string; added: number; merged_total: number }>(
    `/consistency/facts/proposals/${projectId}/accept`,
    { proposals },
  );
  return res.data;
}

// ---------------------------------------------------------------------------
// 废标条款应答覆盖率（E-6，后端 GET /consistency/rejection-coverage/{id}）
// ---------------------------------------------------------------------------

export interface RejectionCoverageItem {
  clause_id: string;
  name: string;
  text: string;
  status: "covered" | "weak" | "empty";
  coverage: number;
  section_hits: string[];
}

export interface RejectionCoverageResult {
  status: string;
  scanned_clauses: number;
  full_chars: number;
  threshold: number;
  weak_count: number;
  empty_count: number;
  items: RejectionCoverageItem[];
}

export async function getRejectionCoverage(projectId: number): Promise<RejectionCoverageResult> {
  const res = await http.get<RejectionCoverageResult>(
    `/consistency/rejection-coverage/${projectId}`,
  );
  return res.data;
}

// ========== 协作功能 ==========

export interface Comment {
  id: number;
  project_id: number;
  section_id?: number;
  user_id: string;
  content: string;
  position?: number;
  parent_id?: number;
  resolved: number;
  resolved_by?: string;
  resolved_at?: string;
  created_at: string;
  user_name?: string;
}

// BUG-FIX-27 修复：原 sectionId 可选时调用 /projects/{id}/comments，
// 该端点后端不存在（404）。改为必填参数，与后端路由一致。
export async function getComments(projectId: number, sectionId: number): Promise<{
  status: string; count: number; comments: Comment[];
}> {
  const url = `/projects/${projectId}/sections/${sectionId}/comments`;
  const res = await http.get<{ status: string; count: number; comments: Comment[] }>(url);
  return res.data;
}

export async function addComment(
  projectId: number,
  sectionId: number,
  content: string,
  userId: string = "anonymous",
  position?: number,
  parentId?: number,
): Promise<{ status: string; comment_id: number; message: string }> {
  const res = await http.post<{ status: string; comment_id: number; message: string }>(
    `/projects/${projectId}/sections/${sectionId}/comments`,
    null,
    { params: { content, user_id: userId, position, parent_id: parentId } },
  );
  return res.data;
}

export async function resolveComment(
  projectId: number,
  sectionId: number,
  commentId: number,
  userId: string = "anonymous",
): Promise<{ status: string; message: string }> {
  const res = await http.post<{ status: string; message: string }>(
    `/projects/${projectId}/sections/${sectionId}/comments/${commentId}/resolve`,
    null,
    { params: { user_id: userId } },
  );
  return res.data;
}

// ========== WebSocket 状态 ==========

export interface WSStats {
  status: string;
  websocket: {
    total_connections: number;
    active_rooms: number;
    rooms: Record<string, number>;
  };
}

export async function getWSStats(): Promise<WSStats> {
  const res = await http.get<WSStats>("/ws/stats");
  return res.data;
}

// ========== 批量操作 ==========

export async function getStatistics(): Promise<{
  status: string; statistics: Record<string, any>;
}> {
  const res = await http.get<{ status: string; statistics: Record<string, any> }>("/batch/statistics");
  return res.data;
}

export async function batchUpsertProjects(projects: Array<{
  id?: number; name: string; description?: string; status?: string;
}>): Promise<{ status: string; message: string; project_ids: number[] }> {
  const res = await http.post<{ status: string; message: string; project_ids: number[] }>(
    "/batch/projects",
    projects,
  );
  return res.data;
}

export async function batchUpdateSectionStatus(
  projectId: number,
  sectionUpdates: [number, string][],
): Promise<{ status: string; message: string; updated_count: number }> {
  const res = await http.post<{ status: string; message: string; updated_count: number }>(
    `/batch/sections/status?project_id=${projectId}`,
    sectionUpdates,
  );
  return res.data;
}

export async function batchGetFiles(projectIds: string): Promise<{
  status: string; files: Record<number, FileInfo[]>;
}> {
  const res = await http.get<{ status: string; files: Record<number, FileInfo[]> }>(
    "/batch/files",
    { params: { project_ids: projectIds } },
  );
  return res.data;
}

// ========== 缓存管理 ==========

export async function getCacheStats(): Promise<{
  status: string; caches: Array<Record<string, any>>; redis_connected: boolean;
}> {
  const res = await http.get<{ status: string; caches: Array<Record<string, any>>; redis_connected: boolean }>(
    "/cache/stats",
  );
  return res.data;
}

export async function clearCache(pattern: string = "all"): Promise<{
  status: string; message: string; cleared_count: number;
}> {
  const res = await http.post<{ status: string; message: string; cleared_count: number }>(
    "/cache/clear",
    null,
    { params: { pattern } },
  );
  return res.data;
}

export async function checkCacheHealth(): Promise<{
  status: string; redis_available: boolean; latency_ms?: number;
  message?: string; fallback?: string; stats?: Record<string, any>;
}> {
  const res = await http.get<{
    status: string; redis_available: boolean; latency_ms?: number;
    message?: string; fallback?: string; stats?: Record<string, any>;
  }>("/cache/health");
  return res.data;
}

export async function getCacheInfo(): Promise<{
  status: string;
  redis_connected: boolean;
  redis_server: Record<string, any>;
  key_distribution: Array<{ type: string; pattern: string; count: number }>;
  total_keys: number;
  app_stats: Record<string, any>;
  section_content_cache: Record<string, any>;
}> {
  const res = await http.get<{
    status: string;
    redis_connected: boolean;
    redis_server: Record<string, any>;
    key_distribution: Array<{ type: string; pattern: string; count: number }>;
    total_keys: number;
    app_stats: Record<string, any>;
    section_content_cache: Record<string, any>;
  }>("/monitor/cache-info");
  return res.data;
}

// ========== API 文档 ==========

export async function exportAPIDocument(format: string = "markdown"): Promise<{
  status: string; content: string; content_type: string; generated_at: string;
}> {
  const res = await http.get<{ status: string; content: string; content_type: string; generated_at: string }>(
    "/docs",
    { params: { format } },
  );
  return res.data;
}

export async function getAPISummary(): Promise<{
  status: string; summary: { total_endpoints: number; groups: any[] };
}> {
  const res = await http.get<{ status: string; summary: { total_endpoints: number; groups: any[] } }>("/docs/summary");
  return res.data;
}

// ========== Chart Validation Helpers (Client-side) ==========

/**
 * 检测图表类型（根据 Mermaid 代码内容）
 */
export function detectChartType(code: string): ChartType | null {
  const trimmed = code.trim().toLowerCase();
  
  if (trimmed.startsWith("gantt") || trimmed.includes("gantt")) return "gantt";
  if (trimmed.includes("architecture") || trimmed.includes("tree")) return "architecture";
  if (trimmed.includes("labor") || trimmed.includes("劳动力")) return "labor";
  if (trimmed.includes("comparison") || trimmed.includes("对比")) return "comparison";
  if (trimmed.includes("layout") || trimmed.includes("布局")) return "layout";
  if (trimmed.includes("timeline") || trimmed.includes("时间线") || trimmed.includes("里程碑")) return "timeline";
  
  // Default to flowchart for most Mermaid diagrams
  if (trimmed.startsWith("flowchart") || trimmed.startsWith("graph")) return "flowchart";
  
  return null;
}

/**
 * 获取关键词建议（基于当前输入的内容）
 */
export function getKeywordSuggestions(code: string): string[] {
  const suggestions: string[] = [];
  const lowerCode = code.toLowerCase();
  
  // Check if we need more nodes
  if (lowerCode.includes("node") || lowerCode.includes("节点") && !lowerCode.includes("end")) {
    suggestions.push("\n    N2[下一步]");
  }
  
  // Check decision nodes
  if (!lowerCode.includes("{")) {
    suggestions.push("\n    B{判断条件?}");
  }
  
  // Add common Mermaid keywords
  if (code.split("-->").length < 3 && (lowerCode.includes("td") || lowerCode.includes("lr"))) {
    suggestions.push("\n    A --> B");
  }
  
  // Title suggestion
  if (!lowerCode.includes("title")) {
    suggestions.push("\n    title 流程图标题");
  }
  
  return suggestions;
}

/**
 * 验证架构图树（调用后端服务）
 */
export async function validateArchitectureTree(data: Record<string, any>, subtype: string = "organization"): Promise<ValidationResult> {
  try {
    // Backend validation would be called here
    // For now, use client-side basic validation
    return validateArchitectureTreeClient(data, subtype);
  } catch (error) {
    // Fallback to client-side validation
    console.warn("Backend validation service not available, using client-side validation", error);
    return validateArchitectureTreeClient(data, subtype);
  }
}

/**
 * 客户端端架构图树验证（实现 v3.1 规范的核心逻辑）
 */
function validateArchitectureTreeClient(data: Record<string, any>, subtype: string = "organization"): ValidationResult {
  const errors: string[] = [];
  
  if (!data || typeof data !== 'object') {
    return { valid: false, errors: ["数据必须是对象格式"] };
  }
  
  // Check root label or name
  if (!(data.label || data.name)) {
    errors.push('缺少根节点标签（label 或 name 字段）');
    return { valid: false, errors };
  }
  
  // Calculate depth
  const depth = calculateTreeDepth(data);
  if (depth > 5) {
    errors.push(`架构深度超过5层限制: ${depth}层`);
    return { valid: false, errors };
  }
  
  // Check unique labels at same level
  const labelsSeen = new Set<string>();
  for (const nodeInfo of flattenWithLevel(data)) {
    const label = nodeInfo.label ?? "";
    if (labelsSeen.has(label)) {
      errors.push(`检测到同级重复标签: '${label}'`);
    }
    labelsSeen.add(label);
  }
  
  // Check node count range based on subtype
  const totalNodes = countAllNodes(data);
  const expectedRanges: Record<string, [number, number]> = {
    organization: [6, 18],
    wbs: [5, 15],
    system: [6, 12],
    infrastructure: [5, 12],
  };
  
  if (expectedRanges[subtype]) {
    const [minN, maxN] = expectedRanges[subtype];
    if (totalNodes < minN) {
      errors.push(`${subtype}类型最少需${minN}个节点，实际${totalNodes}`);
    } else if (totalNodes > maxN) {
      errors.push(`${subtype}类型最多${maxN}个节点，实际${totalNodes}`);
    }
  }
  
  return { valid: errors.length === 0, errors };
}

// Helper functions for architecture tree validation
function calculateTreeDepth(node: Record<string, any>, currentDepth = 1): number {
  if (!node || !Array.isArray(node.children) || node.children.length === 0) {
    return currentDepth;
  }
  
  let maxDepth = currentDepth;
  for (const child of node.children) {
    const depth = calculateTreeDepth(child, currentDepth + 1);
    maxDepth = Math.max(maxDepth, depth);
  }
  
  return maxDepth;
}

function flattenWithLevel(node: Record<string, any>, depth = 0): Array<{ label: string | undefined; level: number }> {
  const result: Array<{ label: string | undefined; level: number }> = [];
  
  if (node.label) {
    result.push({ label: node.label, level: depth });
  } else if (node.name) {
    result.push({ label: node.name, level: depth });
  }
  
  if (Array.isArray(node.children)) {
    for (const child of node.children) {
      result.push(...flattenWithLevel(child, depth + 1));
    }
  }
  
  return result;
}

function countAllNodes(node: Record<string, any>): number {
  let count = 0;
  if (node.label || node.name) count++;
  
  if (Array.isArray(node.children)) {
    for (const child of node.children) {
      count += countAllNodes(child);
    }
  }
  
  return count;
}

// ========== 智能问答 ==========

export interface QaMessage {
  role: "user" | "assistant";
  content: string;
}

export interface QaChatResult {
  status: string;
  answer: string;
  sources: string[];
  project_name: string;
}

export async function qaChat(
  projectId: number,
  question: string,
  history: QaMessage[] = [],
): Promise<QaChatResult> {
  const res = await http.post<QaChatResult>("/qa/chat", { project_id: projectId, question, history });
  return res.data;
}

export async function getQaHistory(projectId: number): Promise<{
  project_id: number; items: QaMessage[]; total: number;
}> {
  const res = await http.get<{ project_id: number; items: QaMessage[]; total: number }>(
    `/qa/history/${projectId}`,
  );
  return res.data;
}

export async function clearQaHistory(projectId: number): Promise<{ status: string; project_id: number }> {
  const res = await http.post<{ status: string; project_id: number }>(
    "/qa/clear",
    null,
    { params: { project_id: projectId } },
  );
  return res.data;
}

// ========== 字数统计 ==========

export interface WordCountOverview {
  status: string;
  project_id: number;
  total_sections: number;
  total_words: number;
  total_target: number;
  target_coverage: number;
  completed_sections: number;
  on_target_sections: number;
  below_target_sections: number;
  exceeded_target_sections: number;
  empty_sections: number;
  stats: {
    per_level: Record<string, number>;
    per_status: Record<string, number>;
    level3_stats: {
      count: number;
      on_target: number;
      below_target: number;
      exceeded_target: number;
      rate: number;
    };
    per_tier_stats: Record<string, {
      count: number;
      on_target: number;
      below: number;
      exceeded: number;
      rate: number;
    }>;
  };
}

export interface WordCountItem {
  id: number;
  title: string;
  level: number;
  parent_id: number | null;
  section_type: string;
  status: string;
  word_count: number;
  target_words: number | null;
  word_count_text: string;
  progress: number;
  // 2026-10-08 新增：档位与达标状态
  word_count_tier: string | null;
  tier_label: string | null;
  tier_target: number;
  on_target: boolean | string; // boolean 或 'unknown'
}

// ========== 字数档位配置 API（2026-10-08）==========

export interface WordCountTierConfig {
  key: string;
  label: string;
  target_words: number;
  tolerance_ratio: number;
  is_default: boolean;
  sort_order: number;
  enabled: number;
}

export async function getWordCountTiers(): Promise<{
  status: string;
  tiers: WordCountTierConfig[];
  default_key: string;
}> {
  const res = await http.get<{ status: string; tiers: WordCountTierConfig[]; default_key: string }>(
    '/word-count/tiers',
  );
  return res.data;
}

export async function updateWordCountTier(
  tierKey: string,
  updates: Partial<Pick<WordCountTierConfig, 'target_words' | 'tolerance_ratio' | 'is_default' | 'label' | 'enabled'>>,
): Promise<{ status: string; tier: WordCountTierConfig }> {
  const res = await http.put<{ status: string; tier: WordCountTierConfig }>(
    `/word-count/tiers/${tierKey}`,
    updates,
  );
  return res.data;
}

export async function getWordCountOverview(projectId: number): Promise<WordCountOverview> {
  const res = await http.get<WordCountOverview>(`/word-count/overview/${projectId}`);
  return res.data;
}

export async function getWordCountSections(projectId: number): Promise<{
  status: string; project_id: number; total: number; items: WordCountItem[];
}> {
  const res = await http.get<{ status: string; project_id: number; total: number; items: WordCountItem[] }>(
    `/word-count/sections/${projectId}`,
  );
  return res.data;
}

// =============================================================================
// 易标（OpenBidKit）字数控制（ENHANCE 2026-10-12 移植）
// -----------------------------------------------------------------------------
// 与上方「档位（tier）」API 并列、互不覆盖：
//   updateWordCountTier  —— 全局档位字典（简短/标准/详细/超长），管理员维护；
//   updateWordControlSettings —— 本项目一次正文任务的绝对字数目标
//                                （最少/最多/每节 + 强控），作者填写。
//
// 所有派生指标（页数预估 / 小节 ±20% 区间 / 目标小节数 / 初稿目标 /
// 可扩缩容量 / 叶子数可行性）都由后端纯函数计算，前端只做展示，
// 避免「前端一套公式、后端一套公式」的口径分叉。
// =============================================================================

/** 字数控制配置（后端 WordControlSettingsInput 的 wire 形态）。 */
export interface WordControlSettings {
  minimum_words: number;
  maximum_words: number;
  section_words: number;
  strict_section_words: boolean;
}

/** 字数控制派生指标（后端 _options_payload 的返回）。 */
export interface WordControlDerived {
  status?: "ok";
  valid: boolean;
  errors: string[];
  /** 强控小节区间 [ceil(0.8S), floor(1.2S)] */
  section_bounds: [number, number];
  /** 目标小节数区间 [ceil(min/S), floor(max/S)]（max 未设时为 null） */
  leaf_count_range: [number, number | null];
  target_leaf_count: number | null;
  /** 目录二审容差区间 [target-2, target+2] */
  outline_review_bounds: [number, number] | null;
  /** 初稿目标字数（强控 + 设了全文上限时才有值） */
  generation_word_target: number | null;
  /** 页数预估（每页 650 字） */
  estimated_pages: number | null;
  /** 可行性提示文案；null 表示可行 */
  word_capacity: string | null;
  /** 仅在传 project_id 时返回 */
  project_id?: number;
  current_leaf_count?: number;
  leaf_outside_range?: boolean;
  leaf_count_warning?: WordControlLeafWarning | null;
}

/** 目录阶段叶子数偏差警告（R12 leaf-count 分支）。 */
export interface WordControlLeafWarning {
  kind: "leaf-count";
  message: string;
  expected: number;
  actual: number;
  review_bounds: [number, number] | null;
}

/** 正文收尾字数警告（R12）。 */
export interface WordControlWarning {
  kind: string;
  message: string;
  [key: string]: unknown;
}

/** 字数控制配置 + 快照 + 派生指标（GET 返回）。 */
export interface WordControlSettingsResponse {
  status: string;
  project_id: number;
  settings: WordControlSettings;
  snapshot: WordControlSettings | null;
  snapshot_at: string | null;
  last_warning: string | null;
  derived: WordControlDerived;
}

/** 读取项目的字数控制配置。 */
export async function getWordControlSettings(
  projectId: number,
): Promise<WordControlSettingsResponse> {
  const res = await http.get<WordControlSettingsResponse>(
    `/word-count/word-control-settings/${projectId}`,
  );
  return res.data;
}

/** 写入字数控制配置；配置不合理时后端 422 拒绝。 */
export async function updateWordControlSettings(
  projectId: number,
  settings: WordControlSettings,
): Promise<WordControlSettingsResponse & { snapshot_invalidated: boolean }> {
  const res = await http.put<WordControlSettingsResponse & { snapshot_invalidated: boolean }>(
    `/word-count/word-control-settings/${projectId}`,
    settings,
  );
  return res.data;
}

/**
 * 字数控制派生指标（即改即算，不落库）。
 *
 * @param body 预览用的配置；带 project_id 时会附带当前目录叶子数与可行性。
 */
export async function computeWordControlDerived(
  body: WordControlSettings & { project_id?: number },
): Promise<WordControlDerived> {
  const res = await http.post<WordControlDerived>(
    "/word-count/word-control-derived",
    body,
  );
  return res.data;
}

/** 把当前配置固化为生效快照（生成前的必经闸门）。 */
export async function snapshotWordControlSettings(
  projectId: number,
): Promise<{
  status: string;
  project_id: number;
  snapshot: WordControlSettings & { snapshot_at: string; section_count: number };
  valid: boolean;
  has_valid_snapshot: boolean;
  snapshot_bounds: Record<string, unknown>;
}> {
  const res = await http.post<{
    status: string;
    project_id: number;
    snapshot: WordControlSettings & { snapshot_at: string; section_count: number };
    valid: boolean;
    has_valid_snapshot: boolean;
    snapshot_bounds: Record<string, unknown>;
  }>(`/word-count/word-control-snapshot/${projectId}`);
  return res.data;
}

/** 字数偏差审计（章节级明细 + R12 结构化警告）。 */
export async function getWordCountAudit(
  projectId: number,
): Promise<{
  status: string;
  project_id: number;
  total_l3_sections: number;
  total_target: number;
  total_actual: number;
  target_coverage: number;
  /** 易标 R1 口径的目标覆盖率 */
  readable_coverage: number;
  avg_deviation_rate: number;
  std_deviation_rate: number;
  category_counts: Record<string, number>;
  on_target_rate: number;
  tier_breakdown: Record<string, unknown>;
  items: Array<Record<string, unknown>>;
  /** 易标 R1 口径的全文可读字数合计 */
  readable_words_total: number;
  /** 未配置字数控制时为 null */
  word_control_warning: WordControlWarning | null;
}> {
  const res = await http.get<{
    status: string;
    project_id: number;
    total_l3_sections: number;
    total_target: number;
    total_actual: number;
    target_coverage: number;
    readable_coverage: number;
    avg_deviation_rate: number;
    std_deviation_rate: number;
    category_counts: Record<string, number>;
    on_target_rate: number;
    tier_breakdown: Record<string, unknown>;
    items: Array<Record<string, unknown>>;
    readable_words_total: number;
    word_control_warning: WordControlWarning | null;
  }>(`/word-count/audit/${projectId}`);
  return res.data;
}

// ========== 统一任务恢复 API ==========

export interface TaskRecoveryStatus {
  project_id: number;
  has_pending_tasks: boolean;
  has_active_tasks: boolean;
  tasks: Record<string, any>;
  recovery_available: boolean;
  recovery_options: Array<{
    type: string;
    label: string;
    phase?: string;
    // ENHANCE 2026-09-15（Task 9）：中断类条目会带上中断时的进度百分比
    progress?: number;
    description: string;
  }>;
}

export interface V51RecoveryInfo {
  recoverable: boolean;
  phase: string;
  cancelled: boolean;
  has_edited_outline: boolean;
  level1_count: number;
  summary: string;
  message: string;
}

export interface ContentRecoveryInfo {
  recoverable: boolean;
  total_sections: number;
  completed_count: number;
  pending_count: number;
  pending_sections: Array<{ id: number; title: string; content?: string }>;
  message: string;
}

export async function fetchTaskRecoveryStatus(projectId: number): Promise<TaskRecoveryStatus> {
  const res = await http.get<TaskRecoveryStatus>(`/projects/${projectId}/tasks/status`);
  return res.data;
}

export async function fetchV51RecoveryInfo(projectId: number): Promise<V51RecoveryInfo> {
  const res = await http.get<V51RecoveryInfo>(`/projects/${projectId}/tasks/v51/recovery`);
  return res.data;
}

export async function fetchContentRecoveryInfo(projectId: number): Promise<ContentRecoveryInfo> {
  const res = await http.get<ContentRecoveryInfo>(`/projects/${projectId}/tasks/content/recovery`);
  return res.data;
}

export async function fetchContentPendingSections(projectId: number, offset = 0, limit = 50): Promise<{
  total: number; offset: number; limit: number; items: any[];
}> {
  const res = await http.get<{ total: number; offset: number; limit: number; items: any[] }>(
    `/projects/${projectId}/tasks/content/pending-sections`,
    { params: { offset, limit } },
  );
  return res.data;
}

// ========== 内容版本管理 API ==========

export interface ContentVersion {
  id: number;
  section_id: number;
  label: string;
  content_preview: string;
  content_length: number;
  created_at: string;
}

export async function listContentVersions(projectId: number, sectionId: number, limit = 20): Promise<ContentVersion[]> {
  const res = await http.get<ContentVersion[]>(
    `/projects/${projectId}/content-versions`,
    { params: { section_id: sectionId, limit } },
  );
  return res.data;
}

export async function saveContentVersion(projectId: number, sectionId: number, content: string, label = ""): Promise<{ id: number; created_at: string }> {
  const res = await http.post<{ version_id: number; message: string }>(
    `/projects/${projectId}/content-versions`,
    { section_id: sectionId, content, label },
  );
  return { id: res.data.version_id, created_at: new Date().toISOString() };
}
export async function restoreContentVersion(projectId: number, versionId: number): Promise<{ section_id: number; content_preview: string; message: string }> {
  // BUG-FIX 2026-09-16（高）：URL 漏拼 projectId —— 请求打到了 `/projects//...`，
  // 版本回滚 100% 404（对照 listContentVersions / saveContentVersion 两处的写法）。
  const res = await http.post<{ section_id: number; content_preview: string; message: string }>(
    `/projects/${projectId}/content-versions/restore`,
    { version_id: versionId },
  );
  return res.data;
}

export async function deleteContentVersion(projectId: number, versionId: number): Promise<{ status: string; message: string }> {
  // BUG-FIX 2026-09-16（高）：URL 既漏 projectId 又漏 versionId ——
  // 删除永远 404；更危险的是一旦后端有通配路由匹配到空路径段，就可能是「批量删」。
  const res = await http.delete<{ status: string; message: string }>(
    `/projects/${projectId}/content-versions/${versionId}`,
  );
  return res.data;
}

// ========== 可行性研究报告模块（ENHANCE 2026-09-15，移植自易标 feasibility-report） ==========

export type FeasibilityStep = "materials" | "analysis" | "outline" | "parameters" | "content";
export type FeasibilityTaskType =
  | "feasibility-analysis"
  | "feasibility-outline"
  | "feasibility-parameters"
  | "feasibility-content";

export interface FeasibilityProjectInfo {
  projectName: string;
  projectType: "government" | "enterprise";
  industry: string;
  constructionUnit: string;
  location: string;
  constructionContent: string;
  constructionPeriodYears: string;
  operationPeriodYears: string;
  totalInvestment: string;
  fundingSource: string;
}

export interface FeasibilitySourceFile {
  id: string;
  fileName: string;
  markdownChars: number;
  importedAt: string;
}

export interface FeasibilityTaskState {
  task_id: string;
  type: FeasibilityTaskType;
  status: "running" | "success" | "error" | "paused";
  progress: number;
  stats?: { phase?: string; current?: string } | null;
  error?: string;
}

export interface FeasibilityOutlineNode {
  id: string;
  title: string;
  description: string;
  content?: string;
  /** 章节精确指定的知识条目 id（ENHANCE 2026-09-15，空则按相关度自动匹配） */
  knowledgeItemIds?: number[];
  children?: FeasibilityOutlineNode[];
}

export interface FeasibilityReportState {
  step: FeasibilityStep;
  projectInfo: FeasibilityProjectInfo;
  sourceFiles: FeasibilitySourceFile[];
  analysisMarkdown: string;
  outlineTemplate: string;
  targetWords: number;
  keyParametersMarkdown: string;
  /** 参考知识库文档 id（可研选材） */
  referenceDocumentIds: number[];
  outlineData: { outline: FeasibilityOutlineNode[]; project_name?: string } | null;
  "feasibility-analysis"?: FeasibilityTaskState;
  "feasibility-outline"?: FeasibilityTaskState;
  "feasibility-parameters"?: FeasibilityTaskState;
  "feasibility-content"?: FeasibilityTaskState;
}

export interface FeasibilityKnowledgeDocument {
  id: number;
  filename: string;
  status: string;
  industry: string;
  itemCount: number;
  itemChars: number;
}

export interface FeasibilityKnowledgeCandidate {
  id: number;
  title: string;
  summary: string;
  documentName: string;
  score: number;
  selected: boolean;
}

export interface FeasibilityChapterKnowledge {
  nodeId: string;
  title: string;
  explicit: boolean;
  explicitItemIds: number[];
  selectedItemIds: number[];
  candidates: FeasibilityKnowledgeCandidate[];
  referenceDocumentCount: number;
  itemPoolSize: number;
}

export function getFeasibilityReportUrl(): string {
  // BUG-FIX 2026-09-18（P1）：补齐下载 token，理由同 getRejectionReportUrl。
  return _appendAuthToken("/api/v1/feasibility/export-report");
}

export async function getFeasibilityReport(): Promise<FeasibilityReportState> {
  const res = await http.get<FeasibilityReportState>("/feasibility");
  return res.data;
}

export async function getFeasibilityOutlineTemplates(): Promise<
  Record<string, { label: string; chapters: string[] }>
> {
  const res = await http.get("/feasibility/outline-templates");
  return res.data;
}

export async function saveFeasibilityProjectInfo(
  projectInfo: FeasibilityProjectInfo,
  clearDownstream = true,
): Promise<FeasibilityReportState> {
  const res = await http.put("/feasibility/project-info", {
    project_info: projectInfo,
    clear_downstream: clearDownstream,
  });
  return res.data;
}

export async function addFeasibilitySource(fileName: string, content: string): Promise<FeasibilityReportState> {
  const res = await http.post("/feasibility/sources", { file_name: fileName, content });
  return res.data;
}

export async function removeFeasibilitySource(sourceId: string): Promise<FeasibilityReportState> {
  const res = await http.delete(`/feasibility/sources/${sourceId}`);
  return res.data;
}

export async function clearFeasibilityWorkspace(): Promise<FeasibilityReportState> {
  const res = await http.delete("/feasibility");
  return res.data;
}

export async function runFeasibilityAnalysis(): Promise<FeasibilityReportState["outlineData"]> {
  const res = await http.post("/feasibility/analysis/run");
  return res.data;
}

export async function saveFeasibilityAnalysis(markdown: string): Promise<FeasibilityReportState> {
  const res = await http.put("/feasibility/analysis", { markdown });
  return res.data;
}

export async function saveFeasibilityOutlineConfig(
  outlineTemplate: string,
  targetWords: number,
  referenceDocumentIds?: number[],
): Promise<FeasibilityReportState> {
  const res = await http.put("/feasibility/outline-config", {
    outline_template: outlineTemplate,
    target_words: targetWords,
    reference_document_ids: referenceDocumentIds,
  });
  return res.data;
}

export async function saveFeasibilityReferenceDocuments(
  documentIds: number[],
): Promise<FeasibilityReportState> {
  const res = await http.put("/feasibility/reference-documents", { document_ids: documentIds });
  return res.data;
}

export async function getFeasibilityKnowledgeDocuments(): Promise<{
  documents: FeasibilityKnowledgeDocument[];
}> {
  const res = await http.get<{ documents: FeasibilityKnowledgeDocument[] }>(
    "/feasibility/knowledge-documents",
  );
  return res.data;
}

export async function getFeasibilityChapterKnowledge(
  nodeId: string,
): Promise<FeasibilityChapterKnowledge> {
  const res = await http.get<FeasibilityChapterKnowledge>(
    `/feasibility/chapters/${encodeURIComponent(nodeId)}/knowledge`,
  );
  return res.data;
}

export async function saveFeasibilityChapterKnowledge(
  nodeId: string,
  itemIds: number[],
): Promise<FeasibilityReportState> {
  const res = await http.put(`/feasibility/chapters/${encodeURIComponent(nodeId)}/knowledge`, {
    item_ids: itemIds,
  });
  return res.data;
}

export async function runFeasibilityOutline(): Promise<{ status: string }> {
  const res = await httpPostLong<{ status: string }>("/feasibility/outline/run");
  return res.data;
}

export async function saveFeasibilityOutline(
  outlineData: { outline: FeasibilityOutlineNode[] },
  reason = "edit",
  idMap: Record<string, string> = {},
  affectedNodeIds: string[] = [],
): Promise<FeasibilityReportState> {
  const res = await http.put("/feasibility/outline", {
    outline_data: outlineData,
    reason,
    id_map: idMap,
    affected_node_ids: affectedNodeIds,
  });
  return res.data;
}

export async function runFeasibilityParameters(): Promise<{ status: string }> {
  const res = await httpPostLong<{ status: string }>("/feasibility/parameters/run");
  return res.data;
}

export async function saveFeasibilityParameters(markdown: string): Promise<FeasibilityReportState> {
  const res = await http.put("/feasibility/parameters", { markdown });
  return res.data;
}

export async function runFeasibilityContent(
  onlyMissing = false,
  skipPolish = false,
): Promise<{ status: string }> {
  const res = await httpPostLong<{ status: string }>("/feasibility/content/run", {
    only_missing: onlyMissing,
    skip_polish: skipPolish,
  });
  return res.data;
}

export async function saveFeasibilityChapter(
  nodeId: string,
  content: string,
): Promise<FeasibilityReportState> {
  const res = await http.put(`/feasibility/chapters/${encodeURIComponent(nodeId)}`, { content });
  return res.data;
}
