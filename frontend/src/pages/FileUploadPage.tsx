import React, { useState, useEffect, useCallback, useRef } from "react";
import { Card, Upload, Button, message, List, Tag, Space, Typography, Spin, Alert, Popconfirm, Modal, Tooltip } from "antd";
import {
  UploadOutlined,
  DeleteOutlined,
  FileWordOutlined,
  FilePdfOutlined,
  FileExcelOutlined,
  FilePptOutlined,
  FileOutlined,
  ReloadOutlined,
  ArrowRightOutlined,
  InfoCircleOutlined,
  StarOutlined,
  ClearOutlined,
  EyeOutlined,
} from "@ant-design/icons";
import type { UploadProps } from "antd";
import { useParams, useNavigate } from "react-router-dom";
import {
  uploadFile,
  fetchFiles,
  deleteFile,
  reExtractFileText,
  updateFileRole,
  fetchFileParsingStatus,
  asyncReparseFile,
  clearProjectTexts,
  fetchSupportedFormats,
  getFileText,
} from "../api";
import type { FileInfo, FileParsingStatus } from "../types";
import {
  mapFileStatusTag,
  countExtractedFiles,
  classifyUploadResult,
  accumulateBatchTotals,
  shouldNotifySingle,
  formatBatchSummary,
  formatUploadFailureText,
  classifyReExtractResult,
  formatReExtractResultText,
  nextFileRole,
  formatRoleToggleMessage,
  formatParsingStatusText,
  formatClearTextsResult,
  isImageFile,
  EMPTY_BATCH_TOTALS,
  resolveAccept,
  FALLBACK_ACCEPT,
  buildTextPreview,
  formatEmptyPreviewText,
  type UploadBatchTotals,
} from "../utils";
import PageHeader from "@/components/PageHeader";
import ProgressPanel, { type ProgressItem } from "@/components/ProgressPanel";
// R-5 2026-10-12：上传页文案接入 i18n（此前全部硬编码中文，i18n.ts 无对应 key）。
// 默认语言 zh-CN，t() 回落 zh-CN，故中文渲染与改动前逐字一致。
import { t } from "../i18n";

const { Text, Paragraph } = Typography;

type UploadRequestOptions = Parameters<NonNullable<UploadProps["customRequest"]>>[0];

function getFileIcon(filename: string) {
  const lower = filename.toLowerCase();
  if (lower.endsWith(".doc") || lower.endsWith(".docx") || lower.endsWith(".wps")) return <FileWordOutlined style={{ color: "#1677ff", fontSize: 20 }} />;
  if (lower.endsWith(".pdf")) return <FilePdfOutlined style={{ color: "#dc2626", fontSize: 20 }} />;
  // ENHANCE 2026-10-06（R5）：清单/演示文稿是解析模块的高频格式，
  // 此前一律灰色 FileOutlined，列表里无法一眼区分文件类型。
  if (lower.endsWith(".xls") || lower.endsWith(".xlsx")) return <FileExcelOutlined style={{ color: "#107c41", fontSize: 20 }} />;
  if (lower.endsWith(".ppt") || lower.endsWith(".pptx")) return <FilePptOutlined style={{ color: "#d8442d", fontSize: 20 }} />;
  return <FileOutlined style={{ color: "#8a94a6", fontSize: 20 }} />;
}

function statusTag(status: string) {
  // BUG-FIX 2026-09-18（P1）：前后端状态契约对齐（映射抽取为纯函数
  // mapFileStatusTag，与 utils 单测钉住）——后端写入 success / failed
  // （Celery 链路另有 processing），保留旧 key 兼容历史数据。
  const cfg = mapFileStatusTag(status);
  return <Tag color={cfg.color} style={{ borderRadius: 5, marginInlineEnd: 0 }}>{cfg.text}</Tag>;
}

export default function FileUploadPage() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const projectId = id ? Number(id) : null;

  const [files, setFiles] = useState<FileInfo[]>([]);
  const [loading, setLoading] = useState(true);
  // BUG-FIX 2026-10-04（P1 批量上传状态错乱）：Upload.Dragger 设了 multiple，
  // antd 会对每个文件并发回调 customRequest。此前用单个 uploading 布尔，
  // 每个回调 finally 都置 false —— 第一个文件上传完就解除 loading/禁用，
  // 其余文件仍在传却显示空闲，用户可重复触发。改为在途计数（ref）驱动状态。
  const [uploadingCount, setUploadingCount] = useState(0);
  const pendingRef = useRef(0);
  // 本批次聚合计数（成功 / 解析失败 / 请求失败三类），pending 归零时汇总并重置。
  // 计数与文案规则已抽取为 utils 纯函数（组件级可测），此处只负责驱动。
  const batchRef = useRef<UploadBatchTotals>({ ...EMPTY_BATCH_TOTALS });
  const uploading = uploadingCount > 0;
  // ENHANCE 2026-10-05（G8 数据链闭环）：后端 GET /tasks/files/status 的项目级解析
  // 状态聚合此前前端完全没有消费方。上传在途期间按 1.5s 轮询，让批量上传有可见进度。
  const [parseStatus, setParseStatus] = useState<FileParsingStatus | null>(null);
  const loadFilesRef = useRef(false);
  // Gap-1 2026-10-11：清空提取按钮的在途状态（防重复点击）
  const [clearing, setClearing] = useState(false);
  // TD6 2026-10-12：accept 由后端单一真相端点下发，拉取失败回落本地常量。
  const [acceptAttr, setAcceptAttr] = useState<string>(FALLBACK_ACCEPT);
  // TD6 2026-10-12：单文件大小上限同样由后端下发，避免前端 50MB 硬编码
  // 与后端 MAX_FILE_SIZE 漂移（后端调大后前端仍按 50MB 拦，用户传不上去）。
  const [maxFileSizeMb, setMaxFileSizeMb] = useState<number>(50);
  // 提取文本预览（激活 getFileText 死链）
  const [previewOpen, setPreviewOpen] = useState(false);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [preview, setPreview] = useState<{
    filename: string;
    text: string;
    truncated: boolean;
    total: number;
  } | null>(null);
  // BUG-FIX 2026-10-12（并发竞态）：快速连续点不同文件的「预览」时，
  // 先发的请求可能后到，把 A 文件的正文显示在 B 文件的标题下。
  // 用请求序号守卫，只接受最后一次发起的请求结果。
  const previewSeqRef = useRef(0);

  useEffect(() => {
    if (!projectId) return;
    let cancelled = false;
    (async () => {
      try {
        const info = await fetchSupportedFormats(projectId);
        if (cancelled) return;
        setAcceptAttr(resolveAccept(info));
        if (info?.max_file_size_mb > 0) setMaxFileSizeMb(info.max_file_size_mb);
      } catch {
        // 格式查询失败不影响上传主流程，静默使用本地兜底常量
        if (!cancelled) setAcceptAttr(FALLBACK_ACCEPT);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [projectId]);

  const loadFiles = useCallback(async () => {
    if (!projectId) return;
    // BUG-FIX 2026-10-08：批量上传时多个 handleUpload 并发调用 loadFiles，
    // 导致竞态和不必要的网络请求。用 ref 标记在途状态，已有请求在途时跳过。
    if (loadFilesRef.current) return;
    loadFilesRef.current = true;
    try {
      const data = await fetchFiles(projectId);
      setFiles(data || []);
    } catch {
      // 静默处理
    } finally {
      setLoading(false);
      loadFilesRef.current = false;
    }
  }, [projectId]);

  useEffect(() => {
    loadFiles();
  }, [loadFiles]);

  // 解析状态轮询：进入页面时取一次（展示上次结果），上传在途期间每 1.5s 刷新，
  // 全部结束后自然停止（依赖 uploading 变化重建 effect）。
  useEffect(() => {
    if (!projectId) return;
    let cancelled = false;
    const fetchOnce = async () => {
      try {
        const data = await fetchFileParsingStatus(projectId);
        if (!cancelled) setParseStatus(data);
      } catch {
        // 状态端点不可用不影响上传主流程，静默处理
      }
    };
    void fetchOnce();
    if (!uploading) {
      return () => {
        cancelled = true;
      };
    }
    // ENHANCE 2026-10-12（R-3 数据链闭环）：后台异步解析的图片文件在上传响应里
    // 先以 status='processing' 返回，解析任务完成后才落终态。上传在途期间除轮询
    // 聚合解析状态外，必须同步刷新文件列表，否则 UI 会一直显示「解析中」直到手动
    // 刷新。loadFiles 自带在途 ref 守卫，1.5s 轮询重复触发是安全的。
    const timer = window.setInterval(() => {
      void fetchOnce();
      void loadFiles();
    }, 1500);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [projectId, uploading, loadFiles]);

  const handleUpload = async (options: UploadRequestOptions) => {
    const { file, onSuccess, onError } = options;
    if (!projectId) return;

    // BUG-FIX 2026-10-08：客户端文件大小校验，避免超大文件先读入内存再被后端拒绝
    // TD6 2026-10-12：上限改由后端 supported-formats 端点下发（默认仍是 50MB），
    // 与后端 MAX_FILE_SIZE 不再需要人工保持一致。
    const MAX_FILE_SIZE = maxFileSizeMb * 1024 * 1024;
    if ((file as File).size > MAX_FILE_SIZE) {
      message.error(`${(file as File).name} 文件大小超过 ${maxFileSizeMb}MB 限制`);
      onError?.(new Error(`文件大小超过 ${maxFileSizeMb}MB 限制`));
      return;
    }

    pendingRef.current += 1;
    setUploadingCount(pendingRef.current);
    const finish = () => {
      pendingRef.current = Math.max(0, pendingRef.current - 1);
      setUploadingCount(pendingRef.current);
      // 整批完成：汇总提示一次，替代逐文件刷屏
      if (pendingRef.current === 0) {
        const summary = formatBatchSummary(batchRef.current);
        if (summary) {
          if (summary.type === "success") message.success(summary.text);
          else message.warning(summary.text);
        }
        batchRef.current = { ...EMPTY_BATCH_TOTALS };
      }
    };

    try {
      const result = await uploadFile(projectId, file as File);
      // BUG-FIX 2026-10-05（B1）：后端「HTTP 200 但 status=failed」（已入库、解析
      // 失败）此前被计入成功，批尾汇总谎报"全部成功"。按结果分类计数。
      const outcome = classifyUploadResult(result);
      batchRef.current = accumulateBatchTotals(batchRef.current, outcome);
      const fileName = (file as File).name;
      if (outcome === "parse_failed") {
        message.error(formatUploadFailureText(fileName, "parse_failed", result?.message));
      } else if (shouldNotifySingle(pendingRef.current, batchRef.current)) {
        message.success(`${fileName} 上传成功`);
      }
      onSuccess?.({}, new XMLHttpRequest());
    } catch (err: any) {
      batchRef.current = accumulateBatchTotals(batchRef.current, "error");
      message.error(
        formatUploadFailureText((file as File).name, "error", err?.message),
      );
      onError?.(err);
    } finally {
      // 每份文件落库后刷新一次列表（批量下由 loadFiles 幂等覆盖，无副作用）
      await loadFiles();
      finish();
    }
  };

  const handleDelete = async (fileId: number) => {
    if (!projectId) return;
    try {
      const res = await deleteFile(projectId, fileId);
      setFiles((prev) => prev.filter((f) => f.id !== fileId));
      // ENHANCE 2026-10-05（B5）：删掉主文件后后端会自动把下一个文件提升为主文件，
      // 如实告知用户，避免"我明明还留着文件，却没有主文件"的困惑。
      if (res?.promoted_file_id) {
        message.info("已删除主文件，系统已自动将下一个文件设为主文件");
      } else {
        message.success("文件已删除");
      }
    } catch (err: any) {
      message.error(err.message || "删除失败");
    }
  };

  // BUG-FIX 2026-10-06：此前 handleAsyncReparse 的定义被误插入 handleDelete 的
  // try 块内部，导致 handleDelete 未闭合、handleAsyncReparse 作用域丢失，
  // 项目 tsc -b / vite build 直接编译失败（Cannot find name 'handleAsyncReparse'）。
  // 现恢复为两个独立的顶层函数。
  // 遗留修复 2026-10-06（L2）：图片等耗时解析交给后台任务队列，避免 OCR 长时间
  // 占住请求。无 broker 时后端会降级为进程内直调（dispatched_via="inline"）。
  const handleAsyncReparse = async (fileId: number) => {
    if (!projectId) return;
    try {
      const res = await asyncReparseFile(projectId, fileId);
      if (res.dispatched_via === "celery") {
        message.success("已提交后台解析，完成后自动更新文本");
      } else if (res.dispatched_via === "inline") {
        message.success("后台队列不可用，已在服务端直接完成解析");
      } else {
        message.warning("未能提交后台解析，请改用“重新提取”");
      }
      await loadFiles();
    } catch (err: any) {
      message.error(err?.response?.data?.detail || err.message || "提交后台解析失败");
    }
  };

  const handleReExtract = async (fileId: number) => {
    if (!projectId) return;
    try {
      message.loading({ content: "正在重新提取...", key: "reextract" });
      // BUG-FIX 2026-10-06（R5）：此前不检查返回体，HTTP 200 一律弹「成功」。
      // 后端「已入库但解析失败（降级提示串）」与「成功但空文本」都会被谎报。
      const res = await reExtractFileText(projectId, fileId);
      const outcome = classifyReExtractResult(res);
      const text = formatReExtractResultText(outcome, res?.message);
      if (outcome === "failed") {
        message.error({ content: text, key: "reextract", duration: 6 });
      } else if (outcome === "empty") {
        message.warning({ content: text, key: "reextract", duration: 6 });
      } else {
        message.success({ content: text, key: "reextract" });
      }
      await loadFiles();
    } catch (err: any) {
      message.error({ content: err.message || "重新提取失败", key: "reextract" });
    }
  };

  // ENHANCE 2026-10-04（P4）：手动切换主/辅助文件。自动角色检测基于文件名关键词，
  // 命名不规范时会误判，而主文件选择直接决定下游摘要/评分点/目录生成取哪份文本。
  const handleToggleRole = async (file: FileInfo) => {
    if (!projectId) return;
    const next = nextFileRole(file.file_role);
    try {
      const res = await updateFileRole(projectId, file.id, next);
      if (next === "main") {
        // CROSS-FIX 2026-10-04：main 全项目互斥，旧 main 被后端自动降级，如实提示避免用户困惑
        message.success(
          formatRoleToggleMessage(
            file.filename,
            next,
            res.demoted_file_ids?.length ?? 0,
          ),
        );
        // REVIEW-FIX 2026-10-04（m2）：主文件换源后旧摘要/评分仍基于旧文本，
        // 后端已有缓存产物时提醒重新生成（否则“提取项目”页会短路返回旧结果）
        if (res.needs_reanalysis) {
          message.warning("主文件已变更，请到「提取项目」重新生成摘要与评分点", 6);
        }
      } else {
        message.success("已设为辅助文件");
      }
      await loadFiles();
    } catch (err: any) {
      // REVIEW-FIX 2026-10-04（M2）：降级唯一 main 被后端 422 拒绝，
      // 透出 detail 中文文案（axios 默认 err.message 是英文状态码描述）
      message.error(err?.response?.data?.detail || err.message || "角色切换失败");
    }
  };

  // 成功后端状态为 "success"（兼容历史 "extracted"）；计数逻辑抽取为纯函数 countExtractedFiles
  const extractedCount = countExtractedFiles(files);
  // 解析任务状态文案（来自 GET /tasks/files/status，仅在上传在途且有实质信息时展示）
  const parseStatusText = uploading ? formatParsingStatusText(parseStatus) : null;
  // 遗留修复 2026-10-06（L4）：ProgressPanel 的数据源。文件落库后即出现在列表里，
  // 已完成的按 success/failed 映射，其余视为「解析中」；在途数计入总数，
  // 使批量上传时进度条能随文件逐个落库增长。
  const failedCount = files.filter((f) => f.status === "failed").length;
  const progressItems: ProgressItem[] = files.map((f) => ({
    section_id: f.id,
    title: f.filename,
    status:
      f.status === "success" ? "completed" : f.status === "failed" ? "failed" : "generating",
    progress: f.status === "success" ? 100 : 0,
    message: f.message,
  }));
  const progressTotal = Math.max(files.length, extractedCount + failedCount + uploadingCount);

  // Gap-1 2026-10-11：清空项目所有已提取文本
  const handleClearTexts = useCallback(async () => {
    if (!projectId || clearing) return;
    setClearing(true);
    try {
      const result = await clearProjectTexts(projectId);
      // M-5 2026-10-15：后端在「文本已删但 DB 未重置」时返回 200 + warning，
      // 此前这里一律 message.success，用户看到的是误导性成功。
      const clearOutcome = formatClearTextsResult(result);
      if (clearOutcome.level === "warning") {
        message.warning(clearOutcome.text);
      } else {
        message.success(clearOutcome.text);
      }
      await loadFiles();
    } catch (err) {
      const detail = err instanceof Error ? err.message : String(err);
      message.error(`清空提取失败：${detail}`);
    } finally {
      setClearing(false);
    }
  }, [projectId, clearing, loadFiles]);

  // TD6 2026-10-12：提取文本预览 —— 激活 GET /files/{id}/text。
  // 该 API 在 api 层导出已久却没有任何调用方（死链），用户无法核对系统究竟
  // 解析出了什么正文；「显示已提取却提不出要点」这类问题完全无法自查。
  const handlePreviewText = async (file: FileInfo) => {
    if (!projectId) return;
    previewSeqRef.current += 1;
    const seq = previewSeqRef.current;
    setPreviewOpen(true);
    setPreviewLoading(true);
    setPreview({ filename: file.filename, text: "", truncated: false, total: 0 });
    try {
      const text = await getFileText(projectId, file.id);
      // 并发守卫：只有最后一次发起的请求才允许写入展示态
      if (previewSeqRef.current !== seq) return;
      if (!text || !text.trim()) {
        setPreview(null);
        message.info(formatEmptyPreviewText(file.status));
        return;
      }
      const built = buildTextPreview(text);
      setPreview({
        filename: file.filename,
        text: built.preview,
        truncated: built.truncated,
        total: built.total,
      });
    } catch (err) {
      if (previewSeqRef.current !== seq) return;
      setPreview(null);
      message.error(
        `获取提取文本失败：${err instanceof Error ? err.message : String(err)}`,
      );
    } finally {
      if (previewSeqRef.current === seq) setPreviewLoading(false);
    }
  };

  return (
    <div>
      <PageHeader
        icon={<UploadOutlined />}
        title={t("upload.pageTitle")}
        description={t("upload.pageDesc")}
        extra={
          files.length > 0 ? (
            <Space size={12}>
              {/* Gap-1 2026-10-11：清空提取按钮 —— 带二次确认，防误触
                  不删除原始文件，只清空提取文本 + 重置状态 */}
              <Popconfirm
                title={t("upload.confirmClearTitle")}
                description={t("upload.confirmClearDesc")}
                okText={t("upload.okClear")}
                okButtonProps={{ danger: true }}
                cancelText={t("common.cancel")}
                onConfirm={handleClearTexts}
                disabled={clearing || files.length === 0}
              >
                <Button danger icon={<ClearOutlined />} loading={clearing}>
                  {t("upload.clearTexts")}
                </Button>
              </Popconfirm>
              <Button
                type="primary"
                onClick={() => navigate(`/projects/${projectId}/review`)}
              >
                {t("upload.nextExtract")} <ArrowRightOutlined />
              </Button>
            </Space>
          ) : null
        }
      />

      <Space direction="vertical" size={18} style={{ width: "100%" }}>
        {parseStatusText ? (
          <Alert
            type={parseStatus && parseStatus.recent_failed > 0 ? "warning" : "info"}
            showIcon
            style={{ borderRadius: 8 }}
            message={parseStatusText}
          />
        ) : null}
        <Card
          styles={{ body: { padding: 0 } }}
          style={{ borderRadius: 12, overflow: "hidden" }}
        >
          <Upload.Dragger
            name="file"
            multiple
            accept={acceptAttr}
            customRequest={handleUpload}
            showUploadList={false}
            disabled={uploading}
            style={{ border: "none", background: uploading ? "#f5f9ff" : undefined }}
          >
            <div style={{ padding: "34px 24px 30px" }}>
              {uploading ? (
                // 遗留修复 2026-10-06（L4）：改为复用通用进度组件 ProgressPanel，
                // 不再自己写 Spin + 计数文案（与正文生成等模块进度展示口径统一）。
                <div style={{ maxWidth: 520, margin: "0 auto", textAlign: "left" }}>
                  <ProgressPanel
                    items={progressItems}
                    total={progressTotal}
                    success={extractedCount}
                    failed={failedCount}
                    isRunning={uploading}
                  />
                </div>
              ) : (
                <>
                  <p className="ant-upload-drag-icon" style={{ marginBottom: 14 }}>
                    <UploadOutlined style={{ fontSize: 40, color: "#1677ff" }} />
                  </p>
                  <p className="ant-upload-text" style={{ fontSize: 15, fontWeight: 600, color: "#1f2733" }}>
                    {t("upload.dragTitle")}
                  </p>
                  <p className="ant-upload-hint" style={{ color: "#8a94a6", fontSize: 13 }}>
                    {t("upload.dragHint")}
                  </p>
                </>
              )}
            </div>
          </Upload.Dragger>
        </Card>

        <Card
          title={
            <Space size={8}>
              <span>{t("upload.uploadedFiles")}</span>
              {files.length > 0 && <Tag color="blue" style={{ borderRadius: 5 }}>{files.length}</Tag>}
            </Space>
          }
          extra={
            <Button
              size="small"
              icon={<ReloadOutlined />}
              onClick={loadFiles}
            >
              {t("upload.refresh")}
            </Button>
          }
          style={{ borderRadius: 12 }}
        >
          {loading ? (
            <div style={{ textAlign: "center", padding: "40px 0" }}>
              <Spin />
            </div>
          ) : files.length === 0 ? (
            <div className="empty-state" style={{ borderStyle: "dashed" }}>
              <InfoCircleOutlined style={{ fontSize: 28, color: "#b4bccb", marginBottom: 12 }} />
              <Text style={{ color: "#5b6675" }}>{t("upload.empty")}</Text>
            </div>
          ) : (
            <>
              {extractedCount > 0 && (
                <Alert
                  type="success"
                  showIcon
                  style={{ marginBottom: 14, borderRadius: 8 }}
                  message={t("upload.extractedReady", { count: String(extractedCount) })}
                />
              )}
              <List
                dataSource={files}
                renderItem={(file) => (
                  <List.Item
                    actions={[
                      <Tooltip key="preview" title={t("upload.previewTooltip")}>
                        <Button
                          size="small"
                          type="text"
                          icon={<EyeOutlined />}
                          onClick={() => handlePreviewText(file)}
                        >
                          {t("upload.previewText")}
                        </Button>
                      </Tooltip>,
                      <Button
                        key="role"
                        size="small"
                        type="text"
                        icon={<StarOutlined style={{ color: file.file_role === "main" ? "#faad14" : "#b4bccb" }} />}
                        onClick={() => handleToggleRole(file)}
                      >
                        {file.file_role === "main" ? t("upload.setSupporting") : t("upload.setMain")}
                      </Button>,
                      <Button
                        key="reextract"
                        size="small"
                        type="text"
                        icon={<ReloadOutlined />}
                        onClick={() =>
                          isImageFile(file.file_type, file.filename)
                            ? handleAsyncReparse(file.id)
                            : handleReExtract(file.id)
                        }
                      >
                        {/* 遗留修复 L2：图片走后台异步解析（OCR 耗时），其余格式同步重提取 */}
                        {isImageFile(file.file_type, file.filename) ? t("upload.asyncParse") : t("upload.reExtract")}
                      </Button>,
                      <Button
                        key="delete"
                        size="small"
                        type="text"
                        danger
                        icon={<DeleteOutlined />}
                        onClick={() => handleDelete(file.id)}
                        >
                        {t("upload.delete")}
                        </Button>,
                    ]}
                  >
                    <List.Item.Meta
                      avatar={getFileIcon(file.filename)}
                      title={<Text style={{ fontSize: 13.5 }}>{file.filename}</Text>}
                      description={
                        <Space size={10} wrap>
                          {file.file_role === "main" ? (
                            <Tag color="gold" style={{ borderRadius: 5, marginInlineEnd: 0 }}>{t("upload.mainFile")}</Tag>
                          ) : (
                            <Tag style={{ borderRadius: 5, marginInlineEnd: 0 }}>{t("upload.supportingFile")}</Tag>
                          )}
                          {statusTag(file.status)}
                          {file.file_type ? (
                            <Text type="secondary" style={{ fontSize: 12 }}>
                              {t("upload.docType", { type: file.file_type.toUpperCase() })}
                            </Text>
                          ) : null}
                          {file.status === "failed" && file.message ? (
                            <Text type="danger" style={{ fontSize: 12 }} ellipsis>
                              {file.message}
                            </Text>
                          ) : null}
                          {/* BUG-FIX 2026-09-29：解析成功但文本为空时服务层会下发提示，
                              此前只在 failed 时展示 message，用户无从发现空内容文件 */}
                          {file.status !== "failed" && file.message ? (
                            <Text type="warning" style={{ fontSize: 12 }} ellipsis>
                              {file.message}
                            </Text>
                          ) : null}
                        </Space>
                      }
                    />
                  </List.Item>
                )}
              />
              <Paragraph type="secondary" style={{ fontSize: 12, marginTop: 14, marginBottom: 0 }}>
                {t("upload.afterUploadHint")}
              </Paragraph>
            </>
          )}
        </Card>
      </Space>

      {/* TD6 2026-10-12：提取文本预览弹窗（激活 getFileText 死链） */}
      <Modal
        open={previewOpen}
        title={preview ? `${t("upload.previewModalTitle")} · ${preview.filename}` : t("upload.previewModalTitle")}
        onCancel={() => setPreviewOpen(false)}
        footer={null}
        width={900}
        destroyOnHidden
      >
        {previewLoading ? (
          <div style={{ textAlign: "center", padding: "40px 0" }}>
            <Spin />
          </div>
        ) : preview ? (
          <>
            <Alert
              type={preview.truncated ? "warning" : "info"}
              showIcon
              style={{ borderRadius: 8, marginBottom: 12 }}
              message={
                preview.truncated
                  ? t("upload.previewTruncated", {
                      total: String(preview.total),
                      shown: String(preview.text.length),
                    })
                  : t("upload.previewTotal", { total: String(preview.total) })
              }
            />
            <Text
              style={{
                display: "block",
                maxHeight: "55vh",
                overflow: "auto",
                whiteSpace: "pre-wrap",
                wordBreak: "break-all",
                fontSize: 13,
                lineHeight: 1.7,
              }}
            >
              {preview.text}
            </Text>
          </>
        ) : (
          <Text type="secondary">{t("upload.noPreview")}</Text>
        )}
      </Modal>
    </div>
  );
}
