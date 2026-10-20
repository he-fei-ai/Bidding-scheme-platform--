import { useEffect, useState, useCallback, useRef } from "react";
import { useParams, useNavigate } from "react-router-dom";
import {
  Button, Card, Skeleton, message, Space, Typography, Tag, Input, Empty,
  Descriptions, InputNumber, Switch, Table, Alert, Statistic, Select, Progress, Modal,
} from "antd";
import {
  ArrowLeftOutlined, DownloadOutlined, CheckCircleOutlined,
  FileWordOutlined, FilePdfOutlined, FileTextOutlined, HistoryOutlined, WarningOutlined, LoadingOutlined,
} from "@ant-design/icons";
import { getProject, checkDocument, exportDocx, exportDocxStream, getExportHistory, getExportDownloadUrl, getLatestExportUrl, getExportPresets, exportPdf, exportHtml } from "../api";
import { extractApiError, formatExportFileSize, reduceExportProgressEvent, classifyExportFailure } from "../utils";
import type { DocumentCheckResult, ExportRecord, ExportResult, ExportPresetCatalog } from "../types";
import { taskStore } from "../services/taskStore";

const { Text, Title } = Typography;

const FONT_OPTIONS = [
  { label: "宋体 (SimSun)", value: "宋体" },
  { label: "黑体 (SimHei)", value: "黑体" },
  { label: "仿宋 (FangSong)", value: "仿宋" },
  { label: "楷体 (KaiTi)", value: "楷体" },
  { label: "微软雅黑 (Microsoft YaHei)", value: "微软雅黑" },
  { label: "Arial", value: "Arial" },
  { label: "Times New Roman", value: "Times New Roman" },
  { label: "Calibri", value: "Calibri" },
  { label: "Cambria", value: "Cambria" },
  { label: "Verdana", value: "Verdana" },
  { label: "Tahoma", value: "Tahoma" },
];

const SIZE_OPTIONS = [
  { label: "初号 (42pt)", value: 42 },
  { label: "小初 (36pt)", value: 36 },
  { label: "一号 (26pt)", value: 26 },
  { label: "小一 (24pt)", value: 24 },
  { label: "二号 (22pt)", value: 22 },
  { label: "小二 (18pt)", value: 18 },
  { label: "三号 (16pt)", value: 16 },
  { label: "小三 (15pt)", value: 15 },
  { label: "四号 (14pt)", value: 14 },
  { label: "小四 (12pt)", value: 12 },
  { label: "五号 (10.5pt)", value: 10.5 },
  { label: "小五 (9pt)", value: 9 },
  { label: "", value: 0, disabled: true },
  { label: "48pt", value: 48 },
  { label: "28pt", value: 28 },
  { label: "20pt", value: 20 },
  { label: "11pt", value: 11 },
  { label: "10pt", value: 10 },
  { label: "8pt", value: 8 },
];

export default function ExportConfigPage() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const projectId = Number(id);
  const isProjectIdValid = !isNaN(projectId) && projectId > 0;

  const [projectName, setProjectName] = useState("");
  const [bidderName, setBidderName] = useState("");
  const [loading, setLoading] = useState(false);
  const [checking, setChecking] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [exportProgress, setExportProgress] = useState(0);
  const [exportPhase, setExportPhase] = useState("");
  const [checkResult, setCheckResult] = useState<DocumentCheckResult | null>(null);
  const [exportResult, setExportResult] = useState<ExportResult | null>(null);
  const [history, setHistory] = useState<ExportRecord[]>([]);
  // ENHANCE 2026-10-04（T1）：PDF 按需转换期间记录行 id，按钮展示 loading 防重复点击
  const [pdfLoadingId, setPdfLoadingId] = useState<number | null>(null);
  // ENHANCE 2026-10-13：HTML 导出 loading（第二交付格式入口，与 DOCX 导出互斥提示）
  const [htmlLoading, setHtmlLoading] = useState(false);
  // BUG-EXPORT-03 修复：暴露 SSE 流中止能力 + 记录是否用户主动取消
  const exportAbortRef = useRef<(() => void) | null>(null);
  const cancelRequestedRef = useRef(false);

  // BUG-FIX 2026-09-16（中）：组件卸载时中止仍在进行的导出 SSE 流。
  // 原实现 exportAbortRef 只被「取消导出」按钮使用；导出进行中（最长数分钟）
  // 切走页面，fetch 流继续跑、onProgress 持续对已卸载组件 setState（内存泄漏）。
  useEffect(() => {
    return () => {
      exportAbortRef.current?.();
      exportAbortRef.current = null;
    };
  }, []);

  // Config
  const [fontTitle, setFontTitle] = useState("宋体");
  const [fontBody, setFontBody] = useState("宋体");
  const [sizeTitle1, setSizeTitle1] = useState(14);
  const [sizeTitle2, setSizeTitle2] = useState(14);
  const [sizeTitle3, setSizeTitle3] = useState(14);
  const [sizeTitleSub, setSizeTitleSub] = useState(14);
  const [sizeBody, setSizeBody] = useState(12);
  const [lineSpacing, setLineSpacing] = useState(1.5);
  const [showTitlePage, setShowTitlePage] = useState(true);
  const [showToc, setShowToc] = useState(true);
  const [showHeaderFooter, setShowHeaderFooter] = useState(true);
  // BUG-FIX 2026-09-18（P2）：严格模式此前硬编码 false 且无 UI 开关，
  // 后端 strict_mode 才判定的空章节/评分点覆盖缺口永远不触发。
  const [strictMode, setStrictMode] = useState(false);
  // D5 加固（2026-10-07）：图表标题兜底补图开关。开启后章节无图表数据时按标题本地模板自动补图。
  const [autoFallbackCharts, setAutoFallbackCharts] = useState(false);
  // L-6（2026-10-17）：图题编号分隔符（图3-1 标题 / 图3-1：标题），默认空格=历史口径，
  // 非法取值由后端 _figure_caption 回退空格
  const [figureNoSeparator, setFigureNoSeparator] = useState<string>(" ");
  // ENHANCE 2026-09-14：导出格式预设（版式 + 主题），移植自易标 exportFormatPresets
  const [presetCatalog, setPresetCatalog] = useState<ExportPresetCatalog | null>(null);
  const [presetId, setPresetId] = useState("");
  const [themeId, setThemeId] = useState("");
  // 预设只在首次挂载时拉取一次；用 ref 守卫避免 applyLayoutPreset 身份变化导致的重复请求
  const presetsLoadedRef = useRef(false);

  /** 套用版式预设：把预设字段写进本地表单，用户随后仍可自由微调 */
  const applyLayoutPreset = useCallback((id: string, catalog?: ExportPresetCatalog | null) => {
    setPresetId(id);
    const source = catalog ?? presetCatalog;
    const preset = (source?.layouts || []).find((item) => item.id === id);
    const c = preset?.config || {};
    if (c.font_title) setFontTitle(c.font_title);
    if (c.font_body) setFontBody(c.font_body);
    if (typeof c.size_title_1 === "number") setSizeTitle1(c.size_title_1);
    if (typeof c.size_title_2 === "number") setSizeTitle2(c.size_title_2);
    if (typeof c.size_title_3 === "number") setSizeTitle3(c.size_title_3);
    if (typeof c.size_title_sub === "number") setSizeTitleSub(c.size_title_sub);
    if (typeof c.size_body === "number") setSizeBody(c.size_body);
    if (typeof c.line_spacing === "number") setLineSpacing(c.line_spacing);
  }, [presetCatalog]);

  useEffect(() => {
    if (presetsLoadedRef.current) return;
    presetsLoadedRef.current = true;
    getExportPresets()
      .then((catalog) => {
        setPresetCatalog(catalog);
        // 默认套用推荐版式，避免用户面对空白配置无从下手
        const defaultLayout = catalog.defaults?.layout_id || catalog.layouts?.[0]?.id || "";
        if (defaultLayout) applyLayoutPreset(defaultLayout, catalog);
        if (catalog.defaults?.theme_id) setThemeId(catalog.defaults.theme_id);
      })
      .catch(() => {
        // 预设加载失败不阻塞导出：用户仍可用手调配置导出
        presetsLoadedRef.current = false;
        console.warn("加载导出预设失败，已回退为手动配置");
      });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const activeLayout = (presetCatalog?.layouts || []).find((item) => item.id === presetId);
  const activeTheme = (presetCatalog?.themes || []).find((item) => item.id === themeId);

  const loadData = useCallback(async () => {
    if (!isProjectIdValid) return;
    setLoading(true);
    try {
      const p = await getProject(projectId, { silent: true });
      setProjectName(p.name);
      setBidderName(p.bidder_name || "");
    } catch (e: any) {
      if (e?.response?.status === 404) {
        localStorage.removeItem("tzb_active_project_id");
        message.warning("项目不存在，已返回首页");
        navigate("/");
      } else {
        message.error("加载项目信息失败");
      }
    }
    try {
      const h = await getExportHistory(projectId);
      setHistory(h);
    } catch (e) {
      console.warn("加载导出历史失败:", e);
    } finally {
      setLoading(false);
    }
  }, [projectId]);

  useEffect(() => {
    loadData();
  }, [loadData]);

  const handleCheck = async () => {
    setChecking(true);
    try {
      const result = await checkDocument(projectId, strictMode);
      setCheckResult(result);
      if (result.passed) {
        message.success("文档完整性检查通过");
      } else {
        message.warning(`检查发现问题: ${result.errors.join("; ")}`);
      }
    } catch (e: any) {
      message.error(extractApiError(e));
    } finally {
      setChecking(false);
    }
  };

  /**
   * 组装导出配置对象（DOCX / HTML 两条导出链路共用同一份配置）。
   * ENHANCE 2026-10-13：从 handleExport 内联提取，供 HTML 导出复用；
   * 字段口径与既有 DOCX 导出完全一致（preset_id/theme_id 由后端解析补全）。
   */
  const buildExportConfig = () => ({
    font_title: fontTitle,
    font_body: fontBody,
    size_title_1: sizeTitle1,
    size_title_2: sizeTitle2,
    size_title_3: sizeTitle3,
    size_title_sub: sizeTitleSub,
    size_body: sizeBody,
    line_spacing: lineSpacing,
    show_title_page: showTitlePage,
    show_toc: showToc,
    show_header_footer: showHeaderFooter,
    bidder_name: bidderName,
    // BUG-FIX-53 修复：原未传递这些字段，用户无法配置图表参数和严格模式
    image_max_width_cm: 18.0,
    image_dpi: 300,
    gantt_max_columns: 18,
    strict_mode: strictMode,
    // D5 加固（2026-10-07）：图表标题兜底补图（仅命中强关键词的图表相关章节，不伪造数据）
    auto_fallback_charts: autoFallbackCharts,
    // L-6（2026-10-17）：图题编号分隔符透传后端（" " / "："）
    figure_no_separator: figureNoSeparator,
    // ENHANCE 2026-09-14：把预设 id 传给后端。颜色/页边距/纸张/页码/分页等
    // 未在此显式列出的字段，由后端按 preset_id + theme_id 解析补全，
    // 这样新增预设项不必每次都改前端传参清单。
    preset_id: presetId,
    theme_id: themeId,
  });

  const handleExport = async () => {
    const exportTaskId = `export-${projectId}`;
    setExporting(true);
    setExportProgress(0);
    setExportPhase("");
    cancelRequestedRef.current = false;
    exportAbortRef.current = null;
    // ENHANCE 2026-10-17（后台任务挂接）：把导出任务登记到全局 taskStore，
    // 使顶栏 TaskStatusBar 可跨页面展示导出进度，切走页面后返回仍能看到状态。
    taskStore.startTask({
      taskId: exportTaskId,
      projectId,
      taskType: "export",
      status: "running",
      progress: 0,
      message: "正在导出文档...",
      startedAt: Date.now(),
      lastUpdated: Date.now(),
    });
    try {
      const config = buildExportConfig();

      // 优先走 SSE 流式导出（实时进度）；失败时降级为同步接口
      try {
        setExportPhase("正在读取章节数据...");
        const stream = exportDocxStream(projectId, config, (evt) => {
          // ENHANCE 2026-10-04：事件→UI 状态判定抽取为纯函数 reduceExportProgressEvent
          // （可组件级测试）；log 事件保持仅控制台输出。
          if (evt.event === "log") {
            console.debug("[export]", evt.content || evt.step);
            return;
          }
          const ui = reduceExportProgressEvent(evt);
          if (ui) {
            if (evt.event === "progress") setExportProgress(ui.progress);
            setExportPhase(ui.phase);
            taskStore.updateTask(exportTaskId, { progress: ui.progress, message: ui.phase });
          }
        });
        // BUG-EXPORT-03 修复：保存 abort 句柄，供"取消导出"按钮调用
        exportAbortRef.current = stream.abort;
        const result = await stream.promise;
        setExportProgress(100);
        setExportPhase("正在完成导出...");
        setExportResult({
          filename: result.filename,
          file_path: result.file_path,
          file_size: result.file_size,
          // BUG-FIX 2026-09-18（P2）：此前丢掉了 SSE completed 里的 export_id，
          // 下载按钮永远回退 /export/latest；现在透传，并在导出记录写库失败
          // （record_failed）时明确告知用户本次文件不在导出历史中。
          export_id: result.export_id,
          // ENHANCE 2026-10-11（P3）：透传 SSE completed 的下载 URL / 磁盘相对路径，
          // 与同步 /export/docx 路径的 ExportResult 字段保持一致。
          download_url: result.download_url,
          relative_path: result.relative_path,
          check: result.check as DocumentCheckResult,
        });
        if (result.record_failed || result.export_id == null) {
          message.warning("导出记录未能写入历史：本次文件可直接下载，但不会出现在导出历史列表");
        }
        // SSE 仅发送摘要，需要单独获取完整检查结果
        try {
          const fullCheck = await checkDocument(projectId, strictMode);
          setCheckResult(fullCheck);
        } catch {
          // 摘要数据已通过 SSE 返回，不影响主流程
        }
        message.success(`导出成功: ${result.filename}`);
        loadData();
        taskStore.completeTask(exportTaskId, "completed", `导出成功: ${result.filename}`);
      } catch (sseErr: any) {
        // ENHANCE 2026-10-04：取消/冲突/降级三分支判定抽取为纯函数
        // classifyExportFailure（行为与原内联逻辑逐分支一致）。
        const failureKind = classifyExportFailure(sseErr, cancelRequestedRef.current);
        // BUG-EXPORT-03 修复：用户主动取消时不降级重跑同步导出
        if (failureKind === "cancelled") {
          throw Object.assign(new Error("导出已取消"), { __cancelled: true });
        }
        // BUG-FIX 2026-09-15（中）：锁冲突（409）是「抢占锁失败」的预条件错误，
        // 不是 SSE 传输失败——此时导出根本没启动。若降级重跑同步 exportDocx，
        // 它抢的是同一把 export 组锁，必然也 409，反而重复弹窗 + 无意义重试。
        // 故冲突时直接把友好提示抛给外层 catch 单独呈现，不降级。
        if (failureKind === "conflict") {
          throw Object.assign(
            new Error(sseErr.message || "当前已有导出任务在进行，请稍候再试"),
            { __conflict: true },
          );
        }
        // SSE 传输/构建失败（非冲突）时降级为同步接口
        setExportProgress(20);
        setExportPhase("正在读取章节数据...");
        const result = await exportDocx(projectId, config);
        setExportProgress(100);
        setExportResult(result);
        setCheckResult(result.check);
        message.success(`导出成功: ${result.filename}`);
        loadData();
        taskStore.completeTask(exportTaskId, "completed", `导出成功: ${result.filename}`);
      }
    } catch (e: any) {
      // BUG-EXPORT-03 修复：用户主动取消时静默处理，不弹错误提示
      if (e?.__cancelled || cancelRequestedRef.current) {
        setExportPhase("已取消");
        taskStore.stopTracking(exportTaskId);
        return;
      }
      // BUG-FIX 2026-09-15（中）：组锁冲突用专门的「导出冲突」对话框，
      // 与真正的「导出失败」区分，避免用户误以为导出坏了、也避免三连弹窗。
      if (e?.__conflict) {
        Modal.error({
          title: "导出冲突",
          content: e.message || "当前已有导出任务在进行，请稍候再试",
          icon: <WarningOutlined />,
        });
        setExportPhase("");
        taskStore.stopTracking(exportTaskId);
        return;
      }
      setExportProgress(0);
      const detail = extractApiError(e);
      Modal.error({
        title: "导出失败",
        content: detail || "请检查章节内容是否完整",
        icon: <WarningOutlined />,
      });
      message.error(detail || "导出失败");
      taskStore.completeTask(exportTaskId, "failed", detail || "导出失败");
    } finally {
      setExporting(false);
      setExportPhase("");
      exportAbortRef.current = null;
    }
  };

  // BUG-EXPORT-03 修复：取消导出（中止 SSE 流，不降级重跑同步导出）
  const handleCancelExport = () => {
    cancelRequestedRef.current = true;
    if (exportAbortRef.current) {
      exportAbortRef.current();
      exportAbortRef.current = null;
    }
  };

  // ENHANCE 2026-10-13：HTML 导出（第二交付格式）。与 DOCX 共用同一份配置；
  // 后端已按导出组锁串行化同项目导出，冲突（409）直接提示，不重试。
  const handleExportHtml = async () => {
    if (htmlLoading || exporting) return;
    const exportTaskId = `export-html-${projectId}`;
    setHtmlLoading(true);
    taskStore.startTask({
      taskId: exportTaskId,
      projectId,
      taskType: "export",
      status: "running",
      progress: 0,
      message: "正在导出 HTML...",
      startedAt: Date.now(),
      lastUpdated: Date.now(),
    });
    try {
      await exportHtml(projectId, buildExportConfig());
      message.success("HTML 已下载");
      loadData();
      taskStore.completeTask(exportTaskId, "completed", "HTML 已下载");
    } catch (e: any) {
      if (e?.status === 409) {
        Modal.error({
          title: "导出冲突",
          content: e?.message || "当前已有导出任务在进行，请稍候再试",
          icon: <WarningOutlined />,
        });
        taskStore.stopTracking(exportTaskId);
      } else {
        message.error(extractApiError(e) || "HTML 导出失败");
        taskStore.completeTask(exportTaskId, "failed", extractApiError(e) || "HTML 导出失败");
      }
    } finally {
      setHtmlLoading(false);
    }
  };

  // ENHANCE 2026-10-04（T1）：历史/结果区共用的 PDF 下载处理。
  // 后端 501（未装 LibreOffice）的 detail 已含安装指引，直接展示即可。
  //
  // X-7（2026-10-16，N-4 新发现）：改用 extractApiError 与其余 handler
  // 同口径解析后端错误；501 或长错误信息走 Modal 展示（安装指引通常超过
  // message.error 一行显示的容量），短错误仍走 message.error。
  const handleDownloadPdf = async (exportId: number) => {
    if (pdfLoadingId !== null) return;
    setPdfLoadingId(exportId);
    try {
      await exportPdf(projectId, exportId);
      message.success("PDF 已下载");
    } catch (e: any) {
      const detail = extractApiError(e) || "PDF 转换失败";
      // X-7：501（LibreOffice 未安装）或后端返回较长安装指引时走 Modal 完整展示。
      // axios 错误对象 status 在 `e.response?.status`；兜底也检查 `e.status` 兼容非 axios 场景。
      const respStatus = e?.response?.status ?? e?.status;
      const isServerHint = respStatus === 501 || detail.length > 60;
      if (isServerHint) {
        Modal.error({
          title: "PDF 转换不可用",
          content: detail,
          icon: <WarningOutlined />,
        });
      } else {
        message.error(detail);
      }
    } finally {
      setPdfLoadingId(null);
    }
  };

  if (loading) {
    return <Skeleton active paragraph={{ rows: 8 }} />;
  }

  const historyColumns = [
    {
      title: "文件名",
      dataIndex: "filename",
      key: "filename",
      ellipsis: true,
    },
    {
      title: "大小",
      dataIndex: "file_size",
      key: "file_size",
      width: 100,
      // ENHANCE 2026-10-04：两处内联大小格式化统一为 formatExportFileSize（含 B 档）
      render: (size: number) => formatExportFileSize(size),
    },
    {
      title: "导出时间",
      dataIndex: "created_at",
      key: "created_at",
      width: 160,
    },
    {
      // ENHANCE 2026-10-04（T2）：失败导出现在也会落库（status='failed'），
      // 列表直接展示状态，避免一条错误日志都看不到。
      title: "状态",
      dataIndex: "status",
      key: "status",
      width: 80,
      render: (status: string) =>
        status === "failed" ? (
          <Tag color="error">失败</Tag>
        ) : (
          <Tag color="success">成功</Tag>
        ),
    },
    {
      title: "操作",
      key: "actions",
      width: 130,
      render: (_: any, record: ExportRecord) =>
        record.status === "failed" ? (
          <Button type="link" icon={<DownloadOutlined />} disabled>
            下载
          </Button>
        ) : (
          <>
            <Button
              type="link"
              icon={<DownloadOutlined />}
              href={getExportDownloadUrl(projectId, record.id)}
              target="_blank"
            >
              下载
            </Button>
            {/* ENHANCE 2026-10-04（T1）：completed 行提供 PDF 按需转换下载 */}
            <Button
              type="link"
              icon={<FilePdfOutlined />}
              loading={pdfLoadingId === record.id}
              disabled={pdfLoadingId !== null && pdfLoadingId !== record.id}
              onClick={() => handleDownloadPdf(record.id)}
            >
              PDF
            </Button>
          </>
        ),
    },
  ];

  return (
    <div>
      <Space style={{ marginBottom: 16 }}>
        <Button icon={<ArrowLeftOutlined />} onClick={() => navigate(`/projects/${projectId}/sections`)}>
          返回正文编辑
        </Button>
        <Typography.Title level={4} style={{ margin: 0 }}>
          {projectName} — 文档导出
        </Typography.Title>
      </Space>

      {exporting && (
        <Card size="small" style={{ marginBottom: 16 }} styles={{ body: { padding: "12px 16px" } }}>
          <Space direction="vertical" style={{ width: "100%" }} size={6}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
              <Space>
                <LoadingOutlined style={{ color: "#1677ff" }} spin />
                <span style={{ fontSize: 13, color: "#595959" }}>正在导出：</span>
                <Text strong style={{ fontSize: 13, color: "#1677ff" }}>{exportPhase || "生成 DOCX 文档..."}</Text>
              </Space>
              <Tag color="processing" icon={<LoadingOutlined />}>导出中</Tag>
            </div>
            <Progress percent={exportProgress} status="active" showInfo size="small" />
            {/* BUG-EXPORT-03 修复：提供取消导出按钮，中止 SSE 流而不降级重跑 */}
            <Space>
              <Button size="small" danger onClick={handleCancelExport}>取消导出</Button>
            </Space>
          </Space>
        </Card>
      )}

      <div style={{ display: "flex", gap: 16 }}>
        {/* Left: Export Config */}
        <div style={{ flex: 1 }}>
          {/* ENHANCE 2026-09-14：导出格式预设选择器（版式 × 主题） */}
          <Card title="排版预设" style={{ marginBottom: 16 }}>
            <Space direction="vertical" style={{ width: "100%" }} size={10}>
              <Space wrap size={8} align="center">
                <span style={{ fontSize: 13, color: "#595959" }}>版式</span>
                <Select
                  value={presetId}
                  onChange={(v) => applyLayoutPreset(v)}
                  size="small"
                  style={{ width: 200 }}
                  placeholder="自定义"
                  options={[
                    { label: "自定义（不改排版）", value: "" },
                    ...(presetCatalog?.layouts || []).map((item) => ({ label: item.label, value: item.id })),
                  ]}
                />
                <span style={{ fontSize: 13, color: "#595959", marginLeft: 8 }}>主题</span>
                <Select
                  value={themeId}
                  onChange={(v) => setThemeId(v)}
                  size="small"
                  style={{ width: 160 }}
                  placeholder="无"
                  options={[
                    { label: "无（不套用主题）", value: "" },
                    ...(presetCatalog?.themes || []).map((item) => ({ label: item.label, value: item.id })),
                  ]}
                />
                {activeTheme?.swatches?.length ? (
                  <Space size={4}>
                    {activeTheme.swatches.map((color) => (
                      <span
                        key={color}
                        title={color}
                        style={{
                          display: "inline-block",
                          width: 16,
                          height: 16,
                          borderRadius: 3,
                          border: "1px solid #d9d9d9",
                          background: color,
                        }}
                      />
                    ))}
                  </Space>
                ) : null}
              </Space>

              {activeLayout ? (
                <Alert
                  type="info"
                  showIcon
                  style={{ padding: "6px 10px" }}
                  message={<span style={{ fontSize: 12 }}>{activeLayout.description}</span>}
                  description={
                    <span style={{ fontSize: 12 }}>
                      纸张 {activeLayout.config?.page_orientation === "landscape" ? "A4 横向" : "A4 纵向"}
                      {" ｜ "}页边距 上{activeLayout.config?.page_margin_top_cm} 下{activeLayout.config?.page_margin_bottom_cm} 左
                      {activeLayout.config?.page_margin_left_cm} 右{activeLayout.config?.page_margin_right_cm} cm
                      {" ｜ "}一级标题另起页 {activeLayout.config?.heading_level1_page_break ? "是" : "否"}
                      {" ｜ "}页码 {activeLayout.config?.page_number_enabled ? "显示" : "隐藏"}
                      {activeLayout.config?.heading_border_enabled ? " ｜ 章框装饰线 开" : ""}
                    </span>
                  }
                />
              ) : (
                <Alert
                  type="warning"
                  showIcon
                  style={{ padding: "6px 10px" }}
                  message={<span style={{ fontSize: 12 }}>当前为自定义排版：仅使用下方手动配置，不套用任何预设。</span>}
                />
              )}
              <Text type="secondary" style={{ fontSize: 12 }}>
                共 {(presetCatalog?.layouts || []).length} 套版式 × {(presetCatalog?.themes || []).length} 套主题。
                套用版式后仍可在下方逐项微调；主题只影响标题色、表头底色与边框色。
              </Text>
            </Space>
          </Card>

          <Card title="导出配置" style={{ marginBottom: 24 }}>
            <Descriptions column={2} size="small" bordered>
              <Descriptions.Item label="标题字体">
                <Select value={fontTitle} onChange={(v) => setFontTitle(v)} size="small" style={{ width: 160 }} options={FONT_OPTIONS} />
              </Descriptions.Item>
              <Descriptions.Item label="正文字体">
                <Select value={fontBody} onChange={(v) => setFontBody(v)} size="small" style={{ width: 160 }} options={FONT_OPTIONS} />
              </Descriptions.Item>
              <Descriptions.Item label="一级标题字号">
                <Select value={sizeTitle1} onChange={(v) => setSizeTitle1(v)} size="small" style={{ width: 140 }} options={SIZE_OPTIONS} />
              </Descriptions.Item>
              <Descriptions.Item label="二级标题字号">
                <Select value={sizeTitle2} onChange={(v) => setSizeTitle2(v)} size="small" style={{ width: 140 }} options={SIZE_OPTIONS} />
              </Descriptions.Item>
              <Descriptions.Item label="三级标题字号">
                <Select value={sizeTitle3} onChange={(v) => setSizeTitle3(v)} size="small" style={{ width: 140 }} options={SIZE_OPTIONS} />
              </Descriptions.Item>
              <Descriptions.Item label="5-8级标题字号">
                <Select value={sizeTitleSub} onChange={(v) => setSizeTitleSub(v)} size="small" style={{ width: 140 }} options={SIZE_OPTIONS} />
              </Descriptions.Item>
              <Descriptions.Item label="正文字号">
                <Select value={sizeBody} onChange={(v) => setSizeBody(v)} size="small" style={{ width: 140 }} options={SIZE_OPTIONS} />
              </Descriptions.Item>
              <Descriptions.Item label="行距">
                <InputNumber value={lineSpacing} onChange={(v) => setLineSpacing(v || 1.5)} min={1} max={3} step={0.25} size="small" />
              </Descriptions.Item>
              <Descriptions.Item label="投标单位">
                <Input value={bidderName} onChange={(e) => setBidderName(e.target.value)} size="small" style={{ width: 200 }} placeholder="可选" />
              </Descriptions.Item>
              <Descriptions.Item label="标题页">
                <Switch checked={showTitlePage} onChange={setShowTitlePage} />
              </Descriptions.Item>
              <Descriptions.Item label="目录页">
                <Switch checked={showToc} onChange={setShowToc} />
              </Descriptions.Item>
              <Descriptions.Item label="页眉页脚">
                <Switch checked={showHeaderFooter} onChange={setShowHeaderFooter} />
              </Descriptions.Item>
              <Descriptions.Item label="严格模式">
                <Space direction="vertical" size={0}>
                  <Switch checked={strictMode} onChange={setStrictMode} />
                  <Text type="secondary" style={{ fontSize: 12 }}>
                    开启后检查将把空章节/评分点覆盖缺口计入错误
                  </Text>
                </Space>
              </Descriptions.Item>
              <Descriptions.Item label="图表标题兜底补图">
                <Space direction="vertical" size={0}>
                  <Switch checked={autoFallbackCharts} onChange={setAutoFallbackCharts} />
                  <Text type="secondary" style={{ fontSize: 12 }}>
                    开启后：章节无图表数据时，按标题本地模板自动补图（仅图表相关章节）
                  </Text>
                </Space>
              </Descriptions.Item>
              <Descriptions.Item label="图题分隔符">
                <Space direction="vertical" size={0}>
                  <Select
                    value={figureNoSeparator}
                    onChange={setFigureNoSeparator}
                    style={{ width: 180 }}
                    options={[
                      { value: " ", label: "空格（图3-1 标题）" },
                      { value: "：", label: "冒号（图3-1：标题）" },
                    ]}
                  />
                  <Text type="secondary" style={{ fontSize: 12 }}>
                    图表编号与标题间的分隔符；默认空格（历史口径）
                  </Text>
                </Space>
              </Descriptions.Item>
            </Descriptions>
          </Card>

          <div style={{ display: "flex", gap: 12, marginBottom: 24, flexWrap: "wrap", alignItems: "center" }}>
            <Button
              icon={<CheckCircleOutlined />}
              onClick={handleCheck}
              loading={checking}
              size="large"
            >
              完整性检查
            </Button>
            <Button
              type="primary"
              icon={<FileWordOutlined />}
              onClick={handleExport}
              loading={exporting}
              size="large"
            >
              导出 DOCX
            </Button>
            {/* ENHANCE 2026-10-13：HTML 导出入口（后端 /export/html 已上线，
                图表输出占位符；需要真实图表请选 DOCX/PDF） */}
            <Button
              icon={<FileTextOutlined />}
              onClick={handleExportHtml}
              loading={htmlLoading}
              disabled={exporting}
              size="large"
            >
              导出 HTML
            </Button>
          </div>

          {checkResult && (
            <Card title="检查结果" size="small" style={{ marginBottom: 24 }}>
              <div style={{ marginBottom: 12 }}>
                <Tag color={checkResult.passed ? "green" : "red"} style={{ fontSize: 14, padding: "4px 12px" }}>
                  {checkResult.passed ? "通过" : "未通过"}
                </Tag>
              </div>
              <Space size={24}>
                <Statistic title="总章节" value={checkResult.total_sections} />
                <Statistic title="总字数" value={checkResult.total_chars} />
                <Statistic title="空章节" value={checkResult.empty_sections.length} valueStyle={{ color: checkResult.empty_sections.length > 0 ? "#ff4d4f" : "#52c41a" }} />
                <Statistic title="未覆盖评分点" value={checkResult.uncovered_scoring_points.length} valueStyle={{ color: checkResult.uncovered_scoring_points.length > 0 ? "#faad14" : "#52c41a" }} />
              </Space>
              {checkResult.warnings.length > 0 && (
                <div style={{ marginTop: 12 }}>
                  {checkResult.warnings.map((w, i) => (
                    <Alert key={i} message={w} type="warning" showIcon style={{ marginBottom: 4 }} />
                  ))}
                </div>
              )}
              {checkResult.errors.length > 0 && (
                <div style={{ marginTop: 12 }}>
                  {checkResult.errors.map((e, i) => (
                    <Alert key={i} message={e} type="error" showIcon style={{ marginBottom: 4 }} />
                  ))}
                </div>
              )}
              {/* 内容质量规则（《文档质量分析报告》修复 15~24）：
                  结构化问题清单，按 severity 着色，标题旁显示所在章节便于定位 */}
              {checkResult.quality_issues && checkResult.quality_issues.length > 0 && (
                <div style={{ marginTop: 12 }}>
                  <div style={{ marginBottom: 8, fontWeight: 600 }}>
                    内容质量规则问题（{checkResult.quality_issues.length}）
                  </div>
                  {checkResult.quality_issues.map((q, i) => {
                    const alertType =
                      q.severity === "high"
                        ? "error"
                        : q.severity === "medium"
                          ? "warning"
                          : "info";
                    return (
                      <Alert
                        key={i}
                        type={alertType}
                        showIcon
                        style={{ marginBottom: 4 }}
                        message={
                          <span>
                            <span style={{ fontWeight: 600 }}>
                              [{q.rule_id}] {q.title}
                            </span>
                            {q.section_title && (
                              <Text type="secondary" style={{ marginLeft: 8, fontSize: 12 }}>
                                定位：{q.section_title}
                              </Text>
                            )}
                          </span>
                        }
                        description={
                          <div>
                            <div>{q.message}</div>
                            {q.suggestion && (
                              <Text type="secondary" style={{ fontSize: 12 }}>
                                建议：{q.suggestion}
                              </Text>
                            )}
                          </div>
                        }
                      />
                    );
                  })}
                </div>
              )}
            </Card>
          )}

          {exportResult && (
            <Card title="导出成功" size="small" style={{ marginBottom: 24 }}>
              <Descriptions column={2} size="small">
                <Descriptions.Item label="文件名">{exportResult.filename}</Descriptions.Item>
                <Descriptions.Item label="大小">
                  {formatExportFileSize(exportResult.file_size)}
                </Descriptions.Item>
              </Descriptions>
              <Space style={{ marginTop: 12 }}>
                <Button
                  type="primary"
                  icon={<DownloadOutlined />}
                  href={exportResult.export_id
                    ? getExportDownloadUrl(projectId, exportResult.export_id)
                    : getLatestExportUrl(projectId)}
                  target="_blank"
                >
                  下载文档
                </Button>
                {/* ENHANCE 2026-10-04（T1）：本次导出结果也可直接转 PDF；
                    export_id 缺失（写库失败）时无历史可查，不展示入口 */}
                {exportResult.export_id != null && (
                  <Button
                    icon={<FilePdfOutlined />}
                    loading={pdfLoadingId === exportResult.export_id}
                    disabled={pdfLoadingId !== null && pdfLoadingId !== exportResult.export_id}
                    onClick={() => handleDownloadPdf(exportResult.export_id as number)}
                  >
                    下载 PDF
                  </Button>
                )}
              </Space>
            </Card>
          )}
        </div>

        {/* Right: Export History */}
        <div style={{ width: 400 }}>
          <Card
            title={<span><HistoryOutlined /> 导出历史</span>}
          >
            {history.length > 0 ? (
              <Table
                dataSource={history}
                columns={historyColumns}
                rowKey="id"
                size="small"
                pagination={false}
              />
            ) : (
              <Empty description="暂无导出记录" />
            )}
          </Card>
        </div>
      </div>
    </div>
  );
}