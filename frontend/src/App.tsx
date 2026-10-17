import { Routes, Route, Navigate } from "react-router-dom";
import { lazy, Suspense } from "react";
import Layout from "./components/Layout";
import ErrorBoundary from "./components/ErrorBoundary";

// 使用lazy实现代码分割和懒加载
const LoginPage = lazy(() => import("./pages/LoginPage"));
const ProtectedRoute = lazy(() => import("./components/ProtectedRoute"));
const ProjectListPage = lazy(() => import("./pages/ProjectListPage"));
const FileUploadPage = lazy(() => import("./pages/FileUploadPage"));
const SummaryReviewPage = lazy(() => import("./pages/SummaryReviewPage"));
const OutlineEditorPage = lazy(() => import("./pages/OutlineEditorPage"));
const TemplatePage = lazy(() => import("./pages/TemplatePage"));
const SectionEditorPage = lazy(() => import("./pages/SectionEditorPage"));
const ExportConfigPage = lazy(() => import("./pages/ExportConfigPage"));
const PromptEditorPage = lazy(() => import("./pages/PromptEditorPage"));
const RejectionCheckPage = lazy(() => import("./pages/RejectionCheckPage"));
const DuplicateCheckPage = lazy(() => import("./pages/DuplicateCheckPage"));
const FeasibilityReportPage = lazy(() => import("./pages/FeasibilityReportPage"));
const KnowledgeBasePage = lazy(() => import("./pages/KnowledgeBasePage"));
const IndustryTreePage = lazy(() => import("./pages/IndustryTreePage"));
const ApiKeyManagementPage = lazy(() => import("./pages/ApiKeyManagementPage"));
const CacheMonitorPage = lazy(() => import("./pages/CacheMonitorPage"));
const ImageConfigPage = lazy(() => import("./pages/ImageConfigPage"));
const AIConfigPage = lazy(() => import("./pages/AIConfigPage"));
const TextConfigPage = lazy(() => import("./pages/TextConfigPage"));
const QAPage = lazy(() => import("./pages/QAPage"));
const WordCountPage = lazy(() => import("./pages/WordCountPage"));
const ConsistencyPage = lazy(() => import("./pages/ConsistencyPage"));
const WordControlHelpPage = lazy(() => import("./pages/WordControlHelpPage"));
const I18nSettingsPage = lazy(() => import("./pages/I18nSettingsPage"));

// 加载状态组件
function LoadingFallback() {
  return (
    <div className="route-loading" role="status" aria-label="页面加载中">
      <div className="route-loading__bars"><span /><span /><span /><span /></div>
      <span>页面加载中…</span>
    </div>
  );
}

function App() {
  return (
    <ErrorBoundary>
      <Suspense fallback={<LoadingFallback />}>
        <Routes>
          {/* 登录页不套 Layout（无侧栏）；其余路由经 ProtectedRoute 守卫 */}
          <Route path="/login" element={<LoginPage />} />
          <Route element={<ProtectedRoute />}>
            <Route element={<Layout />}>
              <Route path="/" element={<ProjectListPage />} />
            <Route path="/image-config" element={<ImageConfigPage />} />
            <Route path="/text-config" element={<TextConfigPage />} />
            <Route path="/ai-config" element={<AIConfigPage />} />
            <Route path="/prompts" element={<PromptEditorPage />} />
            <Route path="/rejection-check" element={<RejectionCheckPage />} />
            <Route path="/duplicate-check" element={<DuplicateCheckPage />} />
            <Route path="/feasibility-report" element={<FeasibilityReportPage />} />
            <Route path="/knowledge-base" element={<KnowledgeBasePage />} />
            <Route path="/industry-tree" element={<IndustryTreePage />} />
            <Route path="/api-keys" element={<ApiKeyManagementPage />} />
            <Route path="/cache-monitor" element={<CacheMonitorPage />} />
            <Route path="/projects/:id/files" element={<FileUploadPage />} />
            <Route path="/projects/:id/review" element={<SummaryReviewPage />} />
            <Route path="/projects/:id/outline" element={<OutlineEditorPage />} />
            <Route path="/projects/:id/templates" element={<TemplatePage />} />
            <Route path="/projects/:id/sections" element={<SectionEditorPage />} />
            <Route path="/projects/:id/export" element={<ExportConfigPage />} />
            <Route path="/projects/:id/qa" element={<QAPage />} />
            <Route path="/projects/:id/word-count" element={<WordCountPage />} />
            <Route path="/projects/:id/consistency" element={<ConsistencyPage />} />
            <Route path="/word-control-standard" element={<WordControlHelpPage />} />
            <Route path="/i18n-settings" element={<I18nSettingsPage />} />
            <Route path="*" element={<Navigate to="/" replace />} />
            </Route>
          </Route>
        </Routes>
      </Suspense>
    </ErrorBoundary>
  );
}

export default App;