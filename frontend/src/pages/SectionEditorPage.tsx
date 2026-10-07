import React, { useEffect, useState, useCallback, useRef, useMemo } from "react";
import { useParams, useNavigate } from "react-router-dom";
import {
  Button, Card, Skeleton, message, Space, Typography, Tag, Empty,
  Spin, Select, Tabs, Divider, Alert, InputNumber, Tooltip, Badge, Row, Col, Checkbox,
} from "antd";
import {
  ArrowLeftOutlined, ThunderboltOutlined, EditOutlined, PictureOutlined,
  FolderOutlined, FileTextOutlined, CheckCircleFilled, BranchesOutlined,
  CloseCircleFilled, ClockCircleOutlined, TableOutlined, FileWordOutlined,
  StopOutlined, AimOutlined, CalculatorOutlined,
  WarningFilled, CheckOutlined, ReloadOutlined, SaveOutlined,
  DeleteOutlined, PauseCircleOutlined, PlayCircleOutlined,
  HistoryOutlined, RightOutlined, DownOutlined, UpOutlined, ArrowUpOutlined, ArrowDownOutlined,
  SlidersOutlined,
} from "@ant-design/icons";
import {
  getProject, fetchSections, updateSection as updateSectionApi,
  getSection,
  stopGeneration, pauseGeneration, resumeGeneration, getWordBudget, setSectionTargetWords, createContentStream,
  getGenerationStatus, expandContent, saveContentVersion, moveSection as moveSectionApi,
  http,
} from "../api";
import type { Section, SectionTreeNode } from "../types";
import ProgressPanel, { ProgressItem } from "../components/ProgressPanel";
import ContentPreview from "../components/ContentPreview";
import RichTextEditor from "../components/RichTextEditor";
import RecoveryBanner from "../components/RecoveryBanner";
import VersionHistory from "../components/VersionHistory";
import { extractApiError, annotateOutlineWithNumbers, getLevelColor, getLevelLabel, findNodeInTree, updateNodeInTree, collectLeafIds, countSubtreeLeaves, getLeafCount, getPendingFailedLeafCount, getWordDeficientStats, upsertSectionProgressItem } from "../utils";
const { Text } = Typography;

/** 兜底扩写的平铺阈值下限（与后端 expand_content_impl 的 minimum_words 映射一致） */
const EXPAND_FLAT_MINIMUM: Record<string, number> = {
  short: 800,
  standard: 2000,
  detailed: 4000,
  extra_long: 6000,
};

const getExpandFlatMinimum = (writingLength: string): number =>
  EXPAND_FLAT_MINIMUM[writingLength] ?? 2000;

/** 目录树批量更新：合并窗口与「最大等待」——避免高频 SSE 事件把 debounce 饿死 */
const TREE_BATCH_DEBOUNCE_MS = 50;
const TREE_BATCH_MAX_WAIT_MS = 300;

function getStatusIcon(status: string) {
  switch (status) {
    case "completed": return <CheckCircleFilled style={{ color: "#52c41a" }} />;
    case "failed": return <CloseCircleFilled style={{ color: "#ff4d4f" }} />;
    case "generating": return <Spin size="small" />;
    case "cancelled": return <StopOutlined style={{ color: "#999" }} />;
    default: return <ClockCircleOutlined style={{ color: "#999" }} />;
  }
}

function getWordCountStatus(wordCount: number, targetWords: number | null | undefined, level: number): "ok" | "warning" | "none" {
  if (!wordCount || wordCount === 0) return "none";
  let effectiveTarget = targetWords;
  if (!effectiveTarget || effectiveTarget <= 0) {
    // 与后端 sse_utils._get_effective_target_words 分档保持一致
    if (level === 1) effectiveTarget = 500;
    else if (level === 2) effectiveTarget = 3500;
    else if (level === 3) effectiveTarget = 300;
    else if (level === 4) effectiveTarget = 400;
    else if (level >= 5) effectiveTarget = 300;
    else return "none";
  }
  if (level === 1) {
    return wordCount >= effectiveTarget ? "ok" : "warning";
  }
  const deviation = Math.abs(wordCount - effectiveTarget) / effectiveTarget;
  if (deviation > 0.15) return "warning";
  return "ok";
}

interface WordBudgetInfo {
  level1_count: number;
  level2_count: number;
  level1_min: number;
  level2_default: number;
  total_estimate: number;
}

export default React.memo(function SectionEditorPage() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const projectId = Number(id);
  const isProjectIdValid = !isNaN(projectId) && projectId > 0;

  const [projectName, setProjectName] = useState("");
  const [tree, setTree] = useState<SectionTreeNode[]>([]);
  const [selectedSection, setSelectedSection] = useState<SectionTreeNode | null>(null);
  // BUG-FIX 2026-09-16（中）：SSE 终态分支读 ref 而非闭包捕获的 state ——
  // 生成动辄数分钟，期间用户切换章节后，旧的 onmessage 闭包仍用调用时刻的
  // selectedSection 调 loadSectionDetail，把已不关注的章节内容刷进编辑器。
  const selectedSectionRef = useRef<SectionTreeNode | null>(null);
  const [sectionContent, setSectionContent] = useState("");
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [writingLength, setWritingLength] = useState("standard");
  const [useCustomWords, setUseCustomWords] = useState(false);
  const [activeTab, setActiveTab] = useState("text");

  const [isGenerating, setIsGenerating] = useState(false);
  const isGeneratingRef = useRef(false);
  const [isPaused, setIsPaused] = useState(false);
  const isPausedRef = useRef(false);
  const [progressItems, setProgressItems] = useState<ProgressItem[]>([]);
  const [progressTotal, setProgressTotal] = useState(0);
  const [progressSuccess, setProgressSuccess] = useState(0);
  const [progressFailed, setProgressFailed] = useState(0);
  const [progressCancelled, setProgressCancelled] = useState(0);
  const [collapsedIds, setCollapsedIds] = useState<Set<number>>(() => {
    const s = new Set<number>();
    // 初始默认展开所有一级和二级节点，折叠三级（避免过深）
    return s;
  });
  const eventSourceRef = useRef<{ close: () => void; detach?: () => void; markTerminalEvent: () => void; promise: Promise<void> } | null>(null);
  const isMountedRef = useRef(true);
  useEffect(() => {
    selectedSectionRef.current = selectedSection;
  }, [selectedSection]);
  // 用户主动停止生成时标记，防止 onerror 触发错误弹窗
  const userStoppedRef = useRef(false);
  // 防止进度计数器双重更新：全局 completed 事件到达后置 true，阻止章节级事件重复计数
  const globalCompletedReceivedRef = useRef(false);
  // 修复：树更新批处理——累积 pending updates，在微任务结束时批量应用一次 setTree
  const treeUpdateBatchRef = useRef<Map<number, Partial<SectionTreeNode>>>(new Map());
  const treeBatchTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  // BUG-FIX 2026-09-16（中）：批更新的首次调度时间，用于给 debounce 加「最大等待」，
  // 防止高频 SSE 事件把 50ms 定时器无限重置（目录树长期不刷新）。
  const treeBatchFirstAtRef = useRef<number>(0);
  // BUG-FIX 2026-09-16（低）：「已自动保存」提示的收起定时器需可撤销，
  // 否则多章节连续完成时会互相干扰，且卸载后仍 setState。
  const autoSaveTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  // BUG-F-02 修复：存储 expandContent 的 abort 函数，组件卸载时调用防止泄漏
  const expandAbortRef = useRef<(() => void) | null>(null);
  // BUG-F-04 修复：防止 startGeneration 并发调用导致重复 SSE 连接
  // 场景：用户快速连续点击"生成"按钮，会创建多个 SSE 连接，
  // 前一个被 close() 中断（ERR_ABORTED），后一个也可能被中断
  const isStartingRef = useRef(false);

  const [sectionDetail, setSectionDetail] = useState<Section | null>(null);

  const [wordBudget, setWordBudget] = useState<WordBudgetInfo | null>(null);
  const [level2DefaultWords, setLevel2DefaultWords] = useState<number>(3500);
  const [savingTargetWords, setSavingTargetWords] = useState<number | null>(null);

  // 正文生成高级选项（对应后端 SSE 参数）
  const [useOrchestration, setUseOrchestration] = useState(false);
  const [useWordSampling, setUseWordSampling] = useState(false);
  const [useFallbackExpand, setUseFallbackExpand] = useState(false);
  const [useConsistencyAudit, setUseConsistencyAudit] = useState(false);
  // 并发生成正文并发数（可选项：2/4/6/8，后端硬上限 MAX_CONTENT_CONCURRENCY=8）
  const [concurrency, setConcurrency] = useState<number>(4);

  // ENHANCE 2026-10-05（P-11）：正文编排参数（对齐易标规格六.4/六.5）
  // 表格密度 / AI 生图上限 / mermaid 偏好 / 分段择优粒度
  const [orchTableRatio, setOrchTableRatio] = useState<"none" | "light" | "moderate" | "heavy">("moderate");
  const [orchMaxAiImages, setOrchMaxAiImages] = useState<number>(6);
  const [orchPreferMermaid, setOrchPreferMermaid] = useState<boolean>(true);
  const [orchSectionPerSegment, setOrchSectionPerSegment] = useState<number>(10);
  const [showOrchOptions, setShowOrchOptions] = useState(false);
  // 兜底扩写进行中标记（ENHANCE 2026-09-15：扩写与生成是两条流水线，按钮状态独立）
  const [isExpanding, setIsExpanding] = useState(false);

  const [autoSavedNotice, setAutoSavedNotice] = useState<{ title: string; wordCount: number } | null>(null);
  const [previewMode, setPreviewMode] = useState(false);

  useEffect(() => {
    isGeneratingRef.current = isGenerating;
  }, [isGenerating]);

  useEffect(() => {
    isMountedRef.current = true;
    return () => {
      isMountedRef.current = false;
      // 2026-08-21 增强：组件卸载时使用 detach 模式保留后端任务
      // 改为 detach() 而非 close()，让后端任务继续在后台运行
      if (eventSourceRef.current) {
        if (eventSourceRef.current.detach) {
          eventSourceRef.current.detach();
        } else {
          eventSourceRef.current.close();
        }
        eventSourceRef.current = null;
      }
      // BUG-F-02 修复：组件卸载时中止正在进行的 expandContent 请求
      if (expandAbortRef.current) {
        expandAbortRef.current();
        expandAbortRef.current = null;
      }
      if (treeBatchTimerRef.current) {
        clearTimeout(treeBatchTimerRef.current);
        treeBatchTimerRef.current = null;
      }
      if (autoSaveTimerRef.current) {
        clearTimeout(autoSaveTimerRef.current);
        autoSaveTimerRef.current = null;
      }
      treeBatchFirstAtRef.current = 0;
      treeUpdateBatchRef.current.clear();
    };
  }, []);

  const loadWordBudget = useCallback(async () => {
    try {
      const budget = await getWordBudget(projectId, level2DefaultWords);
      setWordBudget(budget);
    } catch {
      // ignore
    }
  }, [projectId, level2DefaultWords]);

  function sanitizeTreeIfNotGenerating(nodes: SectionTreeNode[]): SectionTreeNode[] {
    if (isGeneratingRef.current) return nodes;
    return nodes.map((n) => {
      const newChildren = n.children && n.children.length > 0
        ? sanitizeTreeIfNotGenerating(n.children)
        : n.children;
      if (n.status === "generating") {
        return { ...n, status: n.has_content ? "completed" : "failed", children: newChildren };
      }
      return { ...n, children: newChildren };
    });
  }

  const loadTree = useCallback(async () => {
    if (!isProjectIdValid) return;
    setLoading(true);
    try {
      const p = await getProject(projectId, { silent: true });
      setProjectName(p.name);
      
      // 首先检查是否有正在进行的生成任务（数据库层面）
      let hasActiveTask = false;
      let activeTaskStatus = "";
      try {
        const status = await getGenerationStatus(projectId);
        hasActiveTask = status.has_active_task || false;
        activeTaskStatus = status.task_status || "";

        // 如果有活跃任务且状态为 running，恢复前端生成状态
        if (hasActiveTask && activeTaskStatus === "running" && !isGeneratingRef.current) {
          setIsGenerating(true);
        } else if (hasActiveTask && activeTaskStatus === "paused") {
          // BUG-FIX 2026-09-16：刷新/重进页面后暂停态丢失。旧实现只识别 running，
          // 暂停中的项目重载后既没有进度面板也没有「恢复生成」按钮，
          // 用户只能重新点「生成全部」——那会绕过断点续生语义。
          setIsPaused(true);
          isPausedRef.current = true;
        }
      } catch (err) {
        // 忽略状态查询错误，继续加载目录
        console.warn("[同步] 获取生成状态失败:", err);
      }
      
      const t = await fetchSections(projectId);
      
      // 如果有活跃任务，不要重置 generating 状态的节点
      // （paused 同理：暂停中那些"生成到一半"的章节本就该保持 generating，
      //   sanitize 会把它们误标成 failed/completed，恢复后界面与实际不符）
      if (hasActiveTask && (activeTaskStatus === "running" || activeTaskStatus === "paused")) {
        setTree(t);
      } else {
        setTree(sanitizeTreeIfNotGenerating(t));
      }
      
      await loadWordBudget();
    } catch (e: any) {
      if (e?.response?.status === 404) {
        localStorage.removeItem("tzb_active_project_id");
        message.warning("项目不存在，已返回首页");
        navigate("/");
      } else {
        message.error("加载目录失败");
      }
    } finally {
      setLoading(false);
    }
  }, [projectId, loadWordBudget]);

  const silentRefreshTree = useCallback(async (): Promise<SectionTreeNode[]> => {
    try {
      const t = await fetchSections(projectId);
      const sanitized = sanitizeTreeIfNotGenerating(t);
      setTree(sanitized);
      return sanitized;
    } catch {
      return [];
    }
  }, [projectId]);

  // 修复：批量应用树更新——避免频繁 setTree 导致渲染卡顿
  const flushTreeUpdates = useCallback(() => {
    const pending = treeUpdateBatchRef.current;
    if (pending.size === 0) return;

    // BUG-FIX 2026-09-16（高 丢更新）：updater 必须是**纯函数**，且要在
    // 生成快照之后再交给 setState。旧实现在 updater 内部 `pending.clear()` ——
    // updater 由 React 在渲染阶段调用（StrictMode 下还会重复调用一次），
    // 期间新到的 SSE 事件又往同一个 Map 里写了条目，紧接着被这句 clear() 抹掉，
    // 章节状态/字数永久停在旧值；第二次调用拿到空 Map 也不会再更新。
    // 现在：先在外部把待更新数据快照出来并清空 batch，updater 只负责纯计算。
    const snapshot = Array.from(pending.entries());
    pending.clear();

    setTree((prev) => {
      let updated = prev;
      for (const [sectionId, updates] of snapshot) {
        updated = updateNodeInTree(updated, sectionId, updates);
      }
      return updated;
    });
  }, []);

  const scheduleTreeUpdate = useCallback(
    (sectionId: number, updates: Partial<SectionTreeNode>) => {
      treeUpdateBatchRef.current.set(sectionId, updates);

      // 清除之前的定时器
      if (treeBatchTimerRef.current) {
        clearTimeout(treeBatchTimerRef.current);
        treeBatchTimerRef.current = null;
      }

      // BUG-FIX 2026-09-16（中）：纯 debounce 会被高频事件「饿死」——
      // 多章节并发生成时 scheduleTreeUpdate 的间隔常小于 50ms，定时器被无限重置，
      // 目录树的实时字数/状态长时间不刷新。这里补一个 300ms 最大等待：
      // 距首次调度超过阈值就立即 flush 并重排下一次。
      const nowTs = Date.now();
      if (!treeBatchFirstAtRef.current) {
        treeBatchFirstAtRef.current = nowTs;
      }
      if (nowTs - treeBatchFirstAtRef.current >= TREE_BATCH_MAX_WAIT_MS) {
        treeBatchFirstAtRef.current = 0;
        flushTreeUpdates();
        return;
      }

      // 50ms 后批量应用（合并同一短时间内的多次更新）
      treeBatchTimerRef.current = setTimeout(() => {
        treeBatchFirstAtRef.current = 0;
        flushTreeUpdates();
        treeBatchTimerRef.current = null;
      }, TREE_BATCH_DEBOUNCE_MS);
    },
    [flushTreeUpdates],
  );

  function markNodesGenerating(nodes: SectionTreeNode[], ids: Set<number>): SectionTreeNode[] {
    return nodes.map((n) => {
      const isTarget = ids.has(n.id);
      const newChildren = n.children && n.children.length > 0
        ? markNodesGenerating(n.children, ids)
        : n.children;
      if (isTarget) {
        return { ...n, status: "generating", has_content: false, word_count: 0, children: newChildren };
      }
      return { ...n, children: newChildren };
    });
  }

  function resetGeneratingNodes(nodes: SectionTreeNode[]): SectionTreeNode[] {
    return nodes.map((n) => {
      const newChildren = n.children && n.children.length > 0
        ? resetGeneratingNodes(n.children)
        : n.children;
      if (n.status === "generating") {
        return { ...n, status: "failed", children: newChildren };
      }
      return { ...n, children: newChildren };
    });
  }

  useEffect(() => {
    loadTree();
  }, [loadTree]);

  useEffect(() => {
    if (tree.length > 0) {
      loadWordBudget();
    }
    // BUG #5 修复：移除 tree 依赖，仅监听 tree.length（结构变化）和 level2DefaultWords。
    // 原实现依赖 tree 引用，生成期间每次 SSE 进度事件都更新 tree 导致此 effect 被触发数十次，
    // 产生大量无意义的 /word-budget 请求。字数预算仅取决于目录结构和配置，与节点状态无关。
  }, [level2DefaultWords, tree.length, loadWordBudget]);

  useEffect(() => {
    if (!isGenerating && tree.length > 0) {
      setTree((prev) => sanitizeTreeIfNotGenerating(prev));
    }
  }, [isGenerating]);

  const loadSectionDetail = async (sectionId: number) => {
    try {
      const section = await getSection(projectId, sectionId);
      setSectionDetail(section);
      setSectionContent(section.content || "");
      // 图表全部自动生成：正文以内嵌围栏代码块（```mermaid / ```chart-json）承载，
      // 导出时按需渲染；不再提供/加载独立的人工配图、流程图、甘特图面板。
      setActiveTab("text");
    } catch {
      // ignore
    }
  };

  const toggleCollapse = useCallback((nodeId: number, e: React.MouseEvent) => {
    e.stopPropagation();
    setCollapsedIds((prev) => {
      const next = new Set(prev);
      if (next.has(nodeId)) {
        next.delete(nodeId);
      } else {
        next.add(nodeId);
      }
      return next;
    });
  }, []);

  const handleMoveSection = useCallback(async (sectionId: number, direction: "up" | "down", e: React.MouseEvent) => {
    e.stopPropagation();
    try {
      const newTree = await moveSectionApi(projectId, sectionId, direction);
      setTree(newTree);
      // 如果移动的是当前选中的章节，更新选中状态
      if (selectedSection?.id === sectionId) {
        // 在新树中找到对应节点
        const findInTree = (nodes: SectionTreeNode[], id: number): SectionTreeNode | null => {
          for (const n of nodes) {
            if (n.id === id) return n;
            if (n.children) {
              const f = findInTree(n.children, id);
              if (f) return f;
            }
          }
          return null;
        };
        const updated = findInTree(newTree, sectionId);
        if (updated) setSelectedSection(updated);
      }
      message.success(direction === "up" ? "已上移" : "已下移");
    } catch (err: any) {
      message.error(err?.message || "移动失败");
    }
  }, [projectId, selectedSection]);

  const handleSelectSection = (node: SectionTreeNode) => {
    setSelectedSection(node);
    const hasChildren = node.children && node.children.length > 0;
    if (!hasChildren) {
      loadSectionDetail(node.id);
    } else {
      setSectionDetail(null);
      setSectionContent("");
    }
  };

  const handleSaveContent = async () => {
    if (!selectedSection) return;
    setSaving(true);
    try {
      await updateSectionApi(projectId, selectedSection.id, { content: sectionContent });
      message.success("已保存");
      silentRefreshTree();
    } catch {
      message.error("保存失败");
    } finally {
      setSaving(false);
    }
  };

  const startGeneration = async (regenIds?: number[], rootSectionId?: number | null, regenerateMissing?: boolean) => {
    // BUG-F-04 修复：防止并发调用 startGeneration
    // 当一个 startGeneration 正在执行时，拒绝新的请求
    if (isStartingRef.current) {
      console.warn("[startGeneration] 已有生成任务在启动中，忽略重复请求");
      return;
    }
    isStartingRef.current = true;
    
    try {
    // 修复：确保参数互斥 - regenIds、rootSectionId、regenerateMissing 只能有一个被指定
    const specifiedParams = [regenIds, rootSectionId, regenerateMissing].filter(p => p !== undefined && p !== null).length;
    if (specifiedParams > 1) {
      console.warn("startGeneration: 检测到多个生成参数同时指定，将优先使用 regenIds（单个章节）或 rootSectionId（章节树）或 regenerateMissing（补全）");
    }
    
    // 修复：空数组视为未指定，不应触发 regenerate_ids 逻辑
    if (regenIds && regenIds.length === 0) {
      regenIds = undefined;
    }
    
    // 关闭旧 SSE 连接（BIG-F-04: 先 markTerminalEvent 防止 close() 内 abort 触发 ERR_ABORTED）
    if (eventSourceRef.current) {
      try { eventSourceRef.current.markTerminalEvent(); } catch { /* ignore */ }
      eventSourceRef.current.close();
      eventSourceRef.current = null;
    }

    setIsGenerating(true);
    setProgressItems([]);
    setProgressTotal(0);
    setProgressSuccess(0);
    setProgressFailed(0);
    setProgressCancelled(0);
    setAutoSavedNotice(null);
    // 重置用户停止标记，以便新一次生成能正常处理 onerror
    userStoppedRef.current = false;

    const targetSet = new Set<number>();
    if (regenIds && regenIds.length > 0) {
      regenIds.forEach((id) => targetSet.add(id));
    } else if (rootSectionId != null) {
      const rootNode = findNodeInTree(tree, rootSectionId);
      if (rootNode) {
        collectLeafIds([rootNode], false).forEach((id) => targetSet.add(id));
      }
    } else if (regenerateMissing) {
      collectLeafIds(tree, true).forEach((id) => targetSet.add(id));
    } else {
      // 生成全部：跳过已生成内容的章节（与后端逻辑一致）
      collectLeafIds(tree, false).forEach((id) => {
        const node = findNodeInTree(tree, id);
        if (node && (!node.has_content || node.status !== "completed")) {
          targetSet.add(id);
        }
      });
    }
    if (targetSet.size > 0) {
      setTree((prev) => markNodesGenerating(prev, targetSet));
    } else {
      // 所有章节均已生成，无需发起空请求
      setIsGenerating(false);
      message.info("所有章节均已生成，无需重新生成");
      isStartingRef.current = false;  // BUG-F-04 修复：手动重置锁
      return;
    }

    const eventSource = createContentStream(projectId, writingLength, regenIds, rootSectionId, regenerateMissing, level2DefaultWords, useOrchestration, useWordSampling, useFallbackExpand, useConsistencyAudit, concurrency, useOrchestration ? {
      maxAiImages: orchMaxAiImages,
      tableRatio: orchTableRatio,
      preferMermaid: orchPreferMermaid,
      sectionPerSegment: orchSectionPerSegment,
    } : undefined);
    eventSourceRef.current = eventSource;
    let finishedNormally = false;
    let hasConnected = false;
    let connectTimeout: ReturnType<typeof setTimeout> | null = null;
    globalCompletedReceivedRef.current = false;

    const clearAllTimeouts = () => {
      // BUG-FIX 2026-09-16（低）：删除从未赋值的 errorTimeout 死代码残留
      if (connectTimeout) {
        clearTimeout(connectTimeout);
        connectTimeout = null;
      }
    };

    eventSource.onmessage = async ({ data }) => {
      try {
        // 修复：组件卸载后仍继续处理 SSE 事件会导致 setState 触达已卸载组件
        if (!isMountedRef.current) return;
        const d = JSON.parse(data);

        if (d.section_id) {
          // 抽取为纯函数 upsertSectionProgressItem（与 utils 单测钉住）：
          // 按 section_id 浅合并更新，不存在则追加，返回新数组不原地修改
          setProgressItems((prev) => upsertSectionProgressItem(prev, d));

          if (d.status === "generating") {
            // BUG-F-05 修复：使用事件中的字数更新树节点，而非固定 0
            const hasWords = d.word_count != null && d.word_count > 0;
            scheduleTreeUpdate(d.section_id, {
              status: "generating",
              has_content: hasWords,
              word_count: d.word_count || 0,
            });
            return;
          }

          if (d.status === "completed" && d.saved) {
            scheduleTreeUpdate(d.section_id, {
              status: "completed",
              has_content: true,
              word_count: d.word_count || 0,
            });
            if (!globalCompletedReceivedRef.current) {
              setProgressSuccess((s) => s + 1);
            }
            if (selectedSectionRef.current && selectedSectionRef.current.id === d.section_id) {
              if (!selectedSectionRef.current.children || selectedSectionRef.current.children.length === 0) {
                getSection(projectId, d.section_id).then((sec) => {
                  if (!isMountedRef.current) return;
                  setSectionDetail(sec);
                  setSectionContent(sec.content || "");
                }).catch(() => {});
              }
            }
            setAutoSavedNotice({ title: d.title || `#${d.section_id}`, wordCount: d.word_count || 0 });
            // BUG-FIX 2026-09-16（低）：原实现每次都新建一个不可撤销的定时器 ——
            // 连续完成多个章节时，前一个会把后一个的提示提前关掉；
            // 组件已卸载时还会触发 setState。改为登记可撤销的单一计时器。
            if (autoSaveTimerRef.current) clearTimeout(autoSaveTimerRef.current);
            autoSaveTimerRef.current = setTimeout(() => {
              setAutoSavedNotice(null);
              autoSaveTimerRef.current = null;
            }, 2000);
          }

          if (d.status === "failed") {
            scheduleTreeUpdate(d.section_id, {
              status: "failed",
            });
            if (!globalCompletedReceivedRef.current) {
              setProgressFailed((f) => f + 1);
            }
          }

          if (d.status === "cancelled") {
            // 修复：cancelled 状态不再映射为 failed，直接存储真实状态
            // 使前端能区分"用户取消"和"生成失败"两种情况
            scheduleTreeUpdate(d.section_id, {
              status: "cancelled",
            });
            if (!globalCompletedReceivedRef.current) {
              setProgressCancelled((c) => c + 1);
            }
          }

          return;
        }

        if (d.status === "connected") {
          hasConnected = true;
          clearAllTimeouts();
        } else if (d.status === "started") {
          setProgressTotal(d.total);
          if (d.section_ids && Array.isArray(d.section_ids)) {
            const idSet = new Set<number>(d.section_ids);
            setTree((prev) => markNodesGenerating(prev, idSet));
          }
        } else if (d.status === "word_sampling") {
          message.info(d.message || "正在进行字数采样预估...", 3);
        } else if (d.status === "fallback_expand") {
          message.info(d.message || "正在对字数不足的章节进行兜底扩写...", 3);
        } else if (d.status === "consistency_audit") {
          message.info(d.message || "正在进行全文一致性审计...", 3);
        } else if (d.status === "quality_summary") {
          if (d.avg_score !== undefined) {
            message.success(`质量评估完成：平均 ${d.avg_score} 分（${d.evaluated_count} 个章节）`, 5);
          }
        } else if (d.status === "cache_stats") {
          // 修复：后端 get_stats() 返回 cache_size/hits/misses，前端原来读 size/hit_count/miss_count 恒为 0
          console.info(`[缓存统计] 命中=${d.hits || 0}, 未命中=${d.misses || 0}, 大小=${d.cache_size || 0}, 命中率=${d.hit_rate || "-"}`);
        } else if (d.status === "retry_round") {
          // ENHANCE 2026-09-16：后端失败章节的指数退避重试此前**完全没有前端反馈**
          // （事件已下发但无分支处理），大项目重试期间界面体感为"卡住"。
          message.info(d.message || `失败章节重试中（第 ${d.round ?? 1}/${d.total_rounds ?? 1} 轮）`, 3);
        } else if (d.status === "chart_draining") {
          // ENHANCE 2026-09-16：配图流水线收尾（最长 60s）同样无反馈，现补上。
          message.loading({ content: d.message || "配图收尾中...", key: "chartDraining", duration: 2 });
        } else if (d.status === "slow_warning") {
          // ENHANCE 2026-09-16：软告警（超过估算 1.5x）只提醒不熔断，让用户知道"慢但没死"。
          message.warning(d.message || "AI 响应较慢，生成仍在继续", 4);
        } else if (d.status === "completed") {
          finishedNormally = true;
          clearAllTimeouts();
          flushTreeUpdates();
          setIsGenerating(false);
          globalCompletedReceivedRef.current = true;
          setProgressSuccess(d.success);
          setProgressFailed(d.failed || 0);
          setProgressCancelled(d.cancelled || 0);
          if (d.stopped) {
            message.warning(`生成已停止：已保存 ${d.success}/${d.total} 章节${d.cancelled ? `，已取消 ${d.cancelled} 章节` : ""}${d.failed ? `，失败 ${d.failed} 章节` : ""}`);
          } else {
            message.success(`正文生成完成：${d.success}/${d.total} 章节已自动保存${d.failed ? `，${d.failed} 章节失败` : ""}`);
          }
          await silentRefreshTree();
          if (selectedSectionRef.current) {
            loadSectionDetail(selectedSectionRef.current.id);
          }
          eventSource.markTerminalEvent();  // BUG-F-04 修复：标记终端事件，避免 close() 时 abort
          eventSource.close();
          eventSourceRef.current = null;
        } else if (d.status === "stopped") {
          finishedNormally = true;
          clearAllTimeouts();
          flushTreeUpdates();
          setIsGenerating(false);
          globalCompletedReceivedRef.current = true;
          setProgressTotal(0);
          setProgressSuccess(d.success || 0);
          setProgressFailed(d.failed || 0);
          setProgressCancelled(d.cancelled || 0);
          message.info(d.message || "生成已停止");
          await silentRefreshTree();
          if (selectedSectionRef.current) {
            loadSectionDetail(selectedSectionRef.current.id);
          }
          eventSource.markTerminalEvent();  // BUG-F-04 修复：标记终端事件，避免 close() 时 abort
          eventSource.close();
          eventSourceRef.current = null;
        } else if (d.status === "error") {
          finishedNormally = true;
          clearAllTimeouts();
          flushTreeUpdates();
          setIsGenerating(false);
          setTree((prev) => resetGeneratingNodes(prev));
          message.error(d.message || "生成失败");
          eventSource.markTerminalEvent();  // BUG-F-04 修复：标记终端事件，避免 close() 时 abort
          eventSource.close();
          eventSourceRef.current = null;
        } else if (d.status === "info") {
          // 新增：后端下发的纯提示性消息（如"所有章节已完成，无需重新生成"），
          // 显示为蓝色 info toast，不触发红色 error
          finishedNormally = true;
          clearAllTimeouts();
          flushTreeUpdates();
          setIsGenerating(false);
          setTree((prev) => resetGeneratingNodes(prev));
          message.info(d.message || "提示");
          eventSource.markTerminalEvent();
          eventSource.close();
          eventSourceRef.current = null;
        }
      } catch {
        // ignore parse errors
      }
    };

    // 错误处理：SSE 连接失败时尝试重连，而非直接关闭
    // BUG-F-07 修复：原代码在 SSE 失败后 5 秒即关闭，导致生成中的任务被强制中断
    // 改为：失败后轮询任务状态，若任务仍在运行则等待并重连，不直接关闭 SSE
    let _pollRetryCount = 0;
    const MAX_POLL_RETRIES = 6;  // 6 × 10s = 60s 等待窗口

    const pollAndReconnect = async () => {
      if (finishedNormally || !isMountedRef.current || userStoppedRef.current) {
        // 外部已处理（正常完成/组件卸载/用户停止），不再重连
        return;
      }
      // BUG-FIX 2026-09-16（高）：连接身份校验。
      // 裸 setTimeout 自我延续的轮询链无法被新一次生成取消：用户断线后手动
      // 重新点生成时，旧轮询 10s 后醒来会 close() 掉**新**连接并 setIsGenerating(false)，
      // 新生成被凭空中断。轮询只允许操作自己那一代的连接。
      if (eventSourceRef.current !== eventSource) {
        return;
      }

      if (_pollRetryCount >= MAX_POLL_RETRIES) {
        // 超时耗尽，最终放弃
        setIsGenerating(false);
        setTree((prev) => resetGeneratingNodes(prev));
        message.error("SSE 连接中断，请刷新页面查看最新结果");
        if (eventSourceRef.current) {
          eventSourceRef.current.markTerminalEvent();
          eventSourceRef.current.close();
          eventSourceRef.current = null;
        }
        return;
      }

      _pollRetryCount++;
      
      try {
        const statusResp = await http.get(`/projects/${projectId}/tasks/status`, { silent: true } as any);
        const tasks = statusResp.data?.tasks || {};
        const contentTask = tasks['content_generation'];
        const taskStatus = contentTask?.task_status || contentTask?.status;
        
        // 任务已完成或已失败
        if (taskStatus === 'completed' || taskStatus === 'failed' || taskStatus === 'cancelled') {
          console.info(`[SSE] 任务已终止 (status=${taskStatus})，刷新结果`);
          await silentRefreshTree();
          if (selectedSectionRef.current) {
            loadSectionDetail(selectedSectionRef.current.id);
          }
          setIsGenerating(false);
          if (eventSourceRef.current) {
            eventSourceRef.current.markTerminalEvent();
            eventSourceRef.current.close();
            eventSourceRef.current = null;
          }
          return;
        }
        
        // 无任务信息（可能已完成或从未开始）
        if (!contentTask && !statusResp.data?.has_pending_tasks) {
          console.info('[SSE] 无活跃任务，视为已完成');
          await silentRefreshTree();
          setIsGenerating(false);
          if (eventSourceRef.current) {
            eventSourceRef.current.markTerminalEvent();
            eventSourceRef.current.close();
            eventSourceRef.current = null;
          }
          return;
        }
        
        // 任务仍在运行，继续等待
        console.info(`[SSE] 任务仍在运行 (${_pollRetryCount}/${MAX_POLL_RETRIES})，10s 后重试`);
        setTimeout(pollAndReconnect, 10000);
      } catch {
        // 轮询失败，继续重试
        setTimeout(pollAndReconnect, 10000);
      }
    };
    
    eventSourceRef.current!.promise.catch(async (e) => {
      if (finishedNormally || !isMountedRef.current) return;
      if (userStoppedRef.current) {
        eventSource.markTerminalEvent();
        eventSource.close();
        eventSourceRef.current = null;
        return;
      }
      // HTTP 错误（401/429/503）：清除所有超时，立即显示真实错误信息
      if ((e as any).isHttpError) {
        clearAllTimeouts();
        setIsGenerating(false);
        setTree((prev) => resetGeneratingNodes(prev));
        message.error(String(e));
        eventSource.markTerminalEvent();
        eventSource.close();
        eventSourceRef.current = null;
        return;
      }
      
      // BUG-F-07 修复：SSE 连接失败后轮询任务状态并重连，而非 5 秒后直接关闭
      clearAllTimeouts();
      // 2 秒后开始轮询（给系统喘息时间）
      setTimeout(pollAndReconnect, 2000);
    });

    // 连接超时：AI 预热或低并发服务器可能需要较长时间建立首个 SSE 事件，使用 45 秒超时
    connectTimeout = setTimeout(() => {
      if (!hasConnected && !finishedNormally && isMountedRef.current) {
        setIsGenerating(false);
        message.error("服务响应超时，请检查网络连接或 AI 配置");
        eventSource.markTerminalEvent();
        eventSource.close();
        eventSourceRef.current = null;
      }
    }, 45000);

    eventSourceRef.current = eventSource;
    } finally {
      // BUG-F-04 修复：重置启动锁，允许下次生成请求
      isStartingRef.current = false;
    }
  };

  const handleStopGeneration = async () => {
    try {
      // 防止重复点击（BUG 3 修复：使用 ref 而非 state，避免闭包陈旧值）
      if (isGeneratingRef.current === false) {
        return;
      }

      // 关键修复：先关闭 SSE 连接和标记用户停止，防止 onerror 触发"SSE 连接中断"错误弹窗
      userStoppedRef.current = true;

      if (eventSourceRef.current) {
        eventSourceRef.current.markTerminalEvent();
        eventSourceRef.current.close();
        eventSourceRef.current = null;
      }

      // 先调用后端 API，确保取消信号已发送后再更新前端状态
      try {
        await stopGeneration(projectId);
      } catch (apiErr) {
        // 即使后端调用失败，前端状态仍然更新
      }

      // API 返回后再更新前端状态
      setIsGenerating(false);
      setIsPaused(false);
      isPausedRef.current = false;
      setTree((prev) => resetGeneratingNodes(prev));

      message.success("生成已停止");

      // 刷新目录树以获取最新状态
      setTimeout(() => silentRefreshTree(), 500);
    } catch (err) {
      console.error("[停止生成] 错误:", err);
      message.error("停止失败");
    }
  };

  const handlePauseGeneration = async () => {
    try {
      if (eventSourceRef.current) {
        eventSourceRef.current.markTerminalEvent();
        eventSourceRef.current.close();
        eventSourceRef.current = null;
      }
      // BUG-FIX 2026-09-16（P0 状态撒谎）：原实现忽略返回值，无论后端是否真的
      // 停住了都提示「已暂停」并显示「恢复」按钮 —— 而后端在竞态下会返回
      // {"status":"error","message":"没有正在进行的生成任务"}（任务恰好已收尾）。
      // 此时用户看到的是"已暂停"，点恢复又失败，只能刷页。
      // 现在按真实结果分支：失败则回滚到"未暂停"，并如实提示。
      const result = await pauseGeneration(projectId);
      if (result?.status === "ok") {
        setIsGenerating(false);
        setIsPaused(true);
        isPausedRef.current = true;
        message.success("生成已暂停，可恢复断点续生");
      } else {
        setIsGenerating(false);
        setIsPaused(false);
        isPausedRef.current = false;
        message.warning(result?.message || "暂停失败：没有正在进行的生成任务");
      }
      setTimeout(() => silentRefreshTree(), 300);
    } catch (err) {
      console.error("[暂停生成] 错误:", err);
      message.error("暂停失败");
    }
  };

  const handleResumeGeneration = async () => {
    try {
      const result = await resumeGeneration(projectId);
      if (result.status === "ok") {
        setIsPaused(false);
        isPausedRef.current = false;
        message.success("已恢复生成，重新连接流...");
        // 重新连接 SSE 流以继续生成。
        // BUG-FIX 2026-09-16：必须走「补全生成」模式（regenerateMissing=true）。
        // 默认模式会跳过 status='cancelled' 的章节，而暂停瞬间正在生成的那些章节
        // 正是被落库为 cancelled —— 用默认模式恢复会让它们永远停在 cancelled，
        // 断点续生名不副实。补全模式同时覆盖 pending/failed/cancelled 与空壳章节。
        setTimeout(() => {
          startGeneration(undefined, undefined, true);
        }, 300);
        setTimeout(() => silentRefreshTree(), 800);
      } else {
        message.warning(result.message || "无可恢复的暂停任务");
      }
    } catch (err) {
      console.error("[恢复生成] 错误:", err);
      message.error("恢复失败");
    }
  };

  const handleGenerateAll = () => {
    startGeneration();
  };

  const handleGenerateCurrent = () => {
    if (!selectedSection) return;
    const leafCount = countSubtreeLeaves(selectedSection);
    startGeneration(undefined, selectedSection.id);
    message.loading({ content: `开始生成「${selectedSection.title}」及子章节，共 ${leafCount} 个叶子章节`, key: "genCurrent", duration: 2 });
  };

  const handleRegenerateMissing = () => {
    const pendingCount = getPendingFailedLeafCount(tree);
    if (pendingCount === 0) {
      message.info("所有章节均已生成，无需重新生成");
      return;
    }
    startGeneration(undefined, null, true);
    message.loading({ content: `开始生成未完成/失败/已取消章节，共 ${pendingCount} 个章节`, key: "regenMissing", duration: 2 });
  };

  const handleExpandContent = () => {
    // BUG-FIX 2026-09-15：兜底扩写的目标集合是「已生成但字数不足」的章节，
    // 旧实现误用 pendingFailedCount（待生成/失败/取消数）做门控与提示，
    // 导致全部生成完成但字数不足时按钮被禁用（核心场景不可用）。
    const deficient = getWordDeficientStats(tree, getExpandFlatMinimum(writingLength), level2DefaultWords);
    if (deficient.count === 0) {
      message.info("没有字数不足的已生成章节，无需扩写");
      return;
    }
    if (isGeneratingRef.current) {
      message.warning("正文生成进行中，请先等待完成或暂停后再扩写");
      return;
    }
    if (isExpanding) return;
    setIsExpanding(true);
    message.loading({
      content: `正在进行兜底扩写：${deficient.count} 个章节字数不足，缺口约 ${deficient.deficitWords} 字...`,
      key: "expandContent",
      duration: 0,
    });
    const { promise, abort } = expandContent(
      projectId,
      writingLength,
      level2DefaultWords,
      concurrency,
      (event) => {
        if (event.status === "section_completed" && event.section_id) {
          message.success(`章节「${event.title}」扩写完成（${event.word_count || 0} 字）`);
          // 扩写完成后立即刷新树节点字数，徽标实时回落
          scheduleTreeUpdate(event.section_id, {
            status: "completed",
            has_content: true,
            word_count: event.word_count || 0,
          });
        } else if (event.status === "generating" && event.phase === "expand") {
          message.loading({ content: event.message || "正在进行兜底扩写...", key: "expandContent", duration: 0 });
        } else if (event.phase) {
          message.info(`${event.phase}: ${event.message || ""}`, 2);
        }
      },
    );
    // BUG-F-02 修复：存储 abort 函数供组件卸载时调用
    expandAbortRef.current = abort;
    promise
      .then((result) => {
        expandAbortRef.current = null;
        setIsExpanding(false);
        message.destroy("expandContent");
        if (result.status === "completed") {
          const after = result.deficient_after ?? 0;
          message.success(
            `扩写完成：本次达标 ${result.expanded_count || 0}/${result.total || 0} 章` +
              (after > 0 ? `，仍有 ${after} 章未达标（可再次扩写）` : ""),
          );
          silentRefreshTree();
        }
      })
      .catch((err) => {
        expandAbortRef.current = null;
        setIsExpanding(false);
        message.destroy("expandContent");
        message.error(`扩写失败: ${err.message || "未知错误"}`);
      });
  };

  const handleRegenerate = async (sectionId: number) => {
    // 修复：只传入单个章节ID的数组，不设置 rootSectionId，避免与 regenerate_missing 冲突
    startGeneration([sectionId]);
  };

  const handleClearSection = async (sectionId: number) => {
    if (!window.confirm('确定要清除此章节的内容吗？此操作不可恢复。')) {
      return;
    }

    try {
      await updateSectionApi(projectId, sectionId, { content: '' });
      message.success('章节内容已清除');

      // 更新树状态
      setTree((prev) =>
        updateNodeInTree(prev, sectionId, { has_content: false, word_count: 0 })
      );

      // 如果当前正在编辑的是被清除的章节，也清空编辑器内容
      if (selectedSection && selectedSection.id === sectionId) {
        setSectionContent('');
        if (sectionDetail) {
          setSectionDetail({ ...sectionDetail, content: '' });
        }
      }

      // 刷新获取最新状态，并同步 selectedSection 指向新树中的节点
      const newTree = await silentRefreshTree();
      const refreshedNode = findNodeInTree(newTree, sectionId);
      if (refreshedNode) {
        setSelectedSection(refreshedNode);
        getSection(projectId, sectionId).then((sec) => {
          setSectionDetail(sec);
          setSectionContent(sec.content || '');
        }).catch(() => {});
      } else {
        setSelectedSection(null);
        setSectionDetail(null);
        setSectionContent('');
      }
    } catch (error) {
      console.error('清除章节内容失败:', error);
      message.error('清除章节内容失败');
    }
  };

  const handleWritingLengthChange = (value: string) => {
    setWritingLength(value);
    setUseCustomWords(false);
    const wordMap: Record<string, number> = {
      short: 1800,
      standard: 3500,
      detailed: 5500,
      extra_long: 7000,
    };
    if (wordMap[value]) {
      setLevel2DefaultWords(wordMap[value]);
    }
  };

  const handleCustomWordsChange = (checked: boolean) => {
    setUseCustomWords(checked);
    if (checked) {
      setWritingLength("standard");
    }
  };

  const handleSetTargetWords = async (value: number | null) => {
    if (!selectedSection || selectedSection.level !== 2) return;
    const targetVal = value && value > 0 ? value : null;
    setSavingTargetWords(selectedSection.id);
    try {
      await setSectionTargetWords(projectId, selectedSection.id, targetVal);
      message.success("目标字数已更新");
      silentRefreshTree();
    } catch {
      message.error("更新目标字数失败");
    } finally {
      setSavingTargetWords(null);
    }
  };

  // 图表均自动生成（AI 在正文最优位置输出围栏代码块），不提供人工生成流程图/甘特图/配图的按钮与处理函数。

  // BUG #7 修复：移除了重复的 EventSource cleanup effect，
  // 组件顶部（line ~182）已有完整的清理逻辑（关闭 EventSource + 清除 batch timer）

  const renderTreeList = (nodes: (SectionTreeNode & { number?: string })[], depth = 0): React.ReactNode => {
    return nodes.map((node, idx) => {
      const hasChildren = node.children && node.children.length > 0;
      const isCollapsed = collapsedIds.has(node.id);
      const isSelected = selectedSection?.id === node.id;
      const wcStatus = getWordCountStatus(node.word_count, node.target_words, node.level);
      const isLevel1 = node.level === 1;
      // 检查一级节点是否是同级中的第一个/最后一个
      const isFirstL1 = idx === 0;
      const isLastL1 = idx === nodes.length - 1;
      const sectionTypeIcon = () => {
        if (node.section_type === "flowchart") return <BranchesOutlined style={{ color: "#faad14", fontSize: 12 }} />;
        if (node.section_type === "gantt") return <TableOutlined style={{ color: "#52c41a", fontSize: 12 }} />;
        if (node.section_type === "illustration") return <PictureOutlined style={{ color: "#eb2f96", fontSize: 12 }} />;
        return hasChildren ? (
          <FolderOutlined style={{ color: getLevelColor(node.level), fontSize: 13 }} />
        ) : (
          <FileTextOutlined style={{ color: getLevelColor(node.level), fontSize: 12 }} />
        );
      };
      return (
        <div key={node.id}>
          <div
            onClick={() => handleSelectSection(node)}
            style={{
              display: "flex",
              alignItems: "center",
              gap: 6,
              padding: "5px 12px",
              cursor: "pointer",
              borderRadius: 4,
              background: isSelected
                ? "#e6f4ff"
                : node.status === "generating"
                  ? "#f0f5ff"
                  : wcStatus === "warning"
                    ? "#fff2e8"
                    : "transparent",
              borderLeft: isSelected
                ? `3px solid ${getLevelColor(node.level)}`
                : node.status === "failed"
                  ? "3px solid #ff4d4f"
                  : wcStatus === "warning"
                    ? "3px solid #ff7a45"
                    : "3px solid transparent",
              marginBottom: 2,
              marginLeft: depth * 16,
              transition: "all 0.2s",
              position: "relative",
            }}
            onMouseEnter={(e) => {
              if (!isSelected && wcStatus !== "warning" && node.status !== "generating" && node.status !== "failed") {
                (e.currentTarget as HTMLElement).style.background = "#f5f5f5";
              }
            }}
            onMouseLeave={(e) => {
              if (!isSelected && wcStatus !== "warning" && node.status !== "generating" && node.status !== "failed") {
                (e.currentTarget as HTMLElement).style.background = "transparent";
              }
            }}
          >
            {/* 展开/折叠按钮 */}
            {hasChildren ? (
              <Button
                type="text"
                size="small"
                icon={isCollapsed ? <RightOutlined style={{ fontSize: 10 }} /> : <DownOutlined style={{ fontSize: 10 }} />}
                onClick={(e) => toggleCollapse(node.id, e)}
                style={{
                  width: 18,
                  height: 18,
                  minWidth: 18,
                  padding: 0,
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "center",
                  color: "#999",
                }}
                title={isCollapsed ? "展开" : "折叠"}
              />
            ) : (
              <span style={{ width: 18, height: 18, display: "inline-block" }} />
            )}
            {sectionTypeIcon()}
            <Text
              ellipsis
              style={{
                flex: 1,
                fontSize: 13,
                fontWeight: node.level === 1 ? 600 : 400,
              }}
            >
              <span style={{ color: "#999", marginRight: 4, fontWeight: 400 }}>{node.number}</span>{node.title}
            </Text>
            {node.word_count > 0 && (
              <Tooltip title={
                node.level >= 1
                  ? (() => {
                      let target = node.target_words;
                      let source = "";
                      if (target && target > 0) {
                        source = "自定义";
                      } else {
                        if (node.level === 1) { target = 500; source = "L1 固定分档"; }
                        else if (node.level === 2) { target = level2DefaultWords; source = "L2 默认分档"; }
                        else if (node.level === 3) { target = 300; source = "L3 默认分档"; }
                        else if (node.level === 4) { target = 400; source = "L4 默认分档"; }
                        else { target = 300; source = "L5+ 默认分档"; }
                      }
                      if (target && target > 0) {
                        const dev = Math.round((node.word_count - target) / target * 100);
                        return `当前 ${node.word_count} 字 / 目标 ${target} 字（${dev >= 0 ? "+" : ""}${dev}%）· 来源：${source}`;
                      }
                      return `${node.word_count} 字`;
                    })()
                  : `${node.word_count} 字`
              }>
                <span style={{
                  fontSize: 11,
                  color: wcStatus === "warning" ? "#ff4d4f" : wcStatus === "ok" ? "#52c41a" : node.status === "failed" ? "#ff4d4f" : "#999",
                  display: "inline-flex",
                  alignItems: "center",
                  gap: 2,
                }}>
                  {wcStatus === "warning" ? <WarningFilled /> : wcStatus === "ok" ? <CheckOutlined /> : null}
                  {node.word_count}
                </span>
              </Tooltip>
            )}
            {getStatusIcon(node.status)}
            {node.has_content && (
              <Tag color="green" style={{ fontSize: 10, lineHeight: "16px", padding: "0 4px" }}>
                <SaveOutlined style={{ fontSize: 9 }} />
              </Tag>
            )}
            {node.status === "failed" && (
              <Tag color="red" style={{ fontSize: 10, lineHeight: "16px", padding: "0 4px" }}>失败</Tag>
            )}
            {node.status === "cancelled" && (
              <Tag color="default" style={{ fontSize: 10, lineHeight: "16px", padding: "0 4px" }}>已取消</Tag>
            )}
            {/* 一级目录的上移/下移按钮 */}
            {isLevel1 && (
              <span style={{ display: "inline-flex", gap: 2, marginLeft: 2 }}>
                <Button
                  type="text"
                  size="small"
                  icon={<ArrowUpOutlined style={{ fontSize: 11 }} />}
                  onClick={(e) => handleMoveSection(node.id, "up", e)}
                  disabled={isFirstL1}
                  style={{ padding: "0 4px", height: 22, opacity: isFirstL1 ? 0.3 : 1 }}
                  title="上移一级"
                />
                <Button
                  type="text"
                  size="small"
                  icon={<ArrowDownOutlined style={{ fontSize: 11 }} />}
                  onClick={(e) => handleMoveSection(node.id, "down", e)}
                  disabled={isLastL1}
                  style={{ padding: "0 4px", height: 22, opacity: isLastL1 ? 0.3 : 1 }}
                  title="下移一级"
                />
              </span>
            )}
            {/* 添加消除按钮 - 仅对叶子节点显示 */}
            {!hasChildren && node.has_content && (
              <Button
                type="text"
                size="small"
                icon={<DeleteOutlined style={{ color: "#ff4d4f", fontSize: 12 }} />}
                onClick={(e) => {
                  e.stopPropagation();
                  handleClearSection(node.id);
                }}
                style={{ padding: "0 4px", marginLeft: 4 }}
                title="清除内容"
              />
            )}
          </div>
          {/* 折叠状态下不渲染子节点 */}
          {hasChildren && !isCollapsed && renderTreeList(node.children, depth + 1)}
        </div>
      );
    });
  };

  // 章节绑定的真实图表数据（供正文预览里的「配图位置提示」标记就地渲染）
  // 依赖逐字段声明，避免 sectionDetail 其它字段变化导致 ContentPreview 无谓重渲染
  // BUG-FIX 2026-09-29：声明必须位于 tabItems 之前——正文预览 Tab（1452 行附近）
  // 引用了 sectionCharts，而此 useMemo 原先声明在 tabItems 之后，const 块级作用域
  // 下构成 TDZ（used before declaration），严格编译报 TS2448/TS2454。
  const sectionCharts = useMemo(
    () => ({
      flowchart: sectionDetail?.flowchart_json || null,
      gantt: sectionDetail?.gantt_json || null,
      architecture: sectionDetail?.architecture_json || null,
      labor: sectionDetail?.labor_json || null,
      comparison: sectionDetail?.comparison_json || null,
      layout: sectionDetail?.layout_json || null,
      timeline: sectionDetail?.timeline_json || null,
    }),
    [
      sectionDetail?.flowchart_json,
      sectionDetail?.gantt_json,
      sectionDetail?.architecture_json,
      sectionDetail?.labor_json,
      sectionDetail?.comparison_json,
      sectionDetail?.layout_json,
      sectionDetail?.timeline_json,
    ],
  );

  const tabItems = selectedSection && sectionDetail ? [
    {
      key: "text",
      label: <span><FileTextOutlined /> 正文</span>,
      children: (
        <div style={{ flex: 1, display: "flex", flexDirection: "column", overflow: "hidden" }}>
          {selectedSection.level === 2 && (
            <div style={{ marginBottom: 12, padding: "8px 12px", background: "#f6ffed", border: "1px solid #b7eb8f", borderRadius: 6 }}>
              <Space>
                <AimOutlined style={{ color: "#52c41a" }} />
                <Text strong style={{ fontSize: 13 }}>目标字数：</Text>
                <Space.Compact>
                  <InputNumber
                    min={3000}
                    max={10000}
                    step={500}
                    value={selectedSection.target_words && selectedSection.target_words > 0 ? selectedSection.target_words : level2DefaultWords}
                    onChange={handleSetTargetWords}
                    disabled={savingTargetWords === selectedSection.id}
                    style={{ width: 120 }}
                  />
                  <Button disabled>字</Button>
                </Space.Compact>
                <Text type="secondary" style={{ fontSize: 12 }}>
                  （目标字数，AI 按 ±15% 弹性生成，超出范围时橙色预警）
                </Text>
                {selectedSection.target_words && selectedSection.target_words !== level2DefaultWords && (
                  <Button
                    type="link"
                    size="small"
                    onClick={() => handleSetTargetWords(null)}
                    style={{ padding: 0, fontSize: 12 }}
                  >
                    恢复默认
                  </Button>
                )}
              </Space>
            </div>
          )}
          {selectedSection.level === 1 && (
            <Alert
              message="一级目录章节字数规则"
              description="正文字数不少于 500 字，超过部分由 AI 根据内容丰富度自动扩展。"
              type="info"
              showIcon
              style={{ marginBottom: 12 }}
            />
          )}
          <div style={{ position: "relative", flex: 1, display: "flex", flexDirection: "column", overflow: "hidden" }}>
            <div style={{ marginBottom: 8, display: "flex", alignItems: "center", gap: 8 }}>
              <Button
                size="small"
                type={previewMode ? "default" : "primary"}
                onClick={() => setPreviewMode(false)}
              >
                编辑
              </Button>
              <Button
                size="small"
                type={previewMode ? "primary" : "default"}
                onClick={() => setPreviewMode(true)}
              >
                预览
              </Button>
            </div>
            {previewMode ? (
              <ContentPreview content={sectionContent} sectionCharts={sectionCharts} />
            ) : (
              <RichTextEditor
                value={sectionContent}
                onChange={setSectionContent}
                placeholder="正文内容将在此显示，您可以直接编辑..."
                style={{
                  minHeight: 300,
                  maxHeight: 600,
                }}
                onInsertImage={(dataUrl) => {
                  // 在光标位置插入图片
                  const insertHtml = `<img src="${dataUrl}" style="max-width: 100%; height: auto; margin: 8px 0;" />`;
                  document.execCommand("insertHTML", false, insertHtml);
                  setSectionContent(document.querySelector('[contenteditable]')?.innerHTML || sectionContent);
                  message.success("图片已插入");
                }}
              />
            )}
          </div>
          <div style={{ marginTop: 8, textAlign: "right" }}>
            <Space>
              <Tag color="blue" icon={<SaveOutlined />}>生成内容已自动保存</Tag>
              {(() => {
                const wc = sectionContent.length;
                let target: number | null = null;
                if (selectedSection.level === 1) target = selectedSection.target_words || 500;
                else if (selectedSection.level === 2) target = selectedSection.target_words || level2DefaultWords;
                const wcStat = getWordCountStatus(wc, target, selectedSection.level);
                return (
                  <>
                    {target && target > 0 && (
                      <Text type={wcStat === "warning" ? "danger" : wcStat === "ok" ? "success" : "secondary"}>
                        {wc} / {target} 字
                        {wc > 0 && target > 0 && (
                          <span style={{ marginLeft: 4 }}>
                            ({wc >= target ? "+" : ""}{Math.round((wc - target) / target * 100)}%)
                          </span>
                        )}
                      </Text>
                    )}
                    {(!target || target <= 0) && <Text type="secondary">{wc} 字</Text>}
                  </>
                );
              })()}
              <Button type="primary" onClick={handleSaveContent} loading={saving}>
                保存编辑
              </Button>
            </Space>
          </div>
        </div>
      ),
    },
    {
      key: "history",
      label: <span><HistoryOutlined /> 版本历史</span>,
      children: (
        <VersionHistory
          projectId={projectId}
          section={selectedSection ? { ...selectedSection, project_id: projectId } as any : null}
          currentContent={sectionContent}
          onSaveVersion={async (sid, content, label) => {
            await saveContentVersion(projectId, sid, content, label);
          }}
        />
      ),
    },
  ] : [];

  const leafCount = getLeafCount(tree);
  const pendingFailedCount = getPendingFailedLeafCount(tree);
  // 兜底扩写目标统计：已生成但字数不足的章节（ENHANCE 2026-09-15）
  const wordDeficient = getWordDeficientStats(tree, getExpandFlatMinimum(writingLength), level2DefaultWords);
  const annotatedTree = useMemo(() => annotateOutlineWithNumbers(tree), [tree]);

  if (loading) {
    return <Skeleton active paragraph={{ rows: 12 }} />;
  }

  return (
    <div className="section-editor-shell" style={{ display: "flex", flexDirection: "column", gap: 0, flex: 1, minHeight: 0, overflow: "hidden" }}>
      {/* 后台任务恢复提示（全宽） */}
      <RecoveryBanner projectId={projectId} />

      {/* 真正的左右两列 */}
      <div style={{ display: "flex", flex: 1, minHeight: 0, overflow: "hidden" }}>

        {/* 左列 340px：返回目录 + 章节树（合并自两行的左半） */}
        <div style={{ width: 340, flexShrink: 0, display: "flex", flexDirection: "column", borderRight: "1px solid #f0f0f0", overflow: "hidden" }}>
          <div style={{ display: "flex", alignItems: "center", gap: 8, padding: "6px 12px", borderBottom: "1px solid #f0f0f0", flexShrink: 0 }}>
            <Button icon={<ArrowLeftOutlined />} onClick={() => navigate(`/projects/${projectId}/outline`)} size="small">
              返回目录
            </Button>
            <Typography.Text strong style={{ marginLeft: 4 }}>{projectName}</Typography.Text>
          </div>
          {/* 目录树滚动区：自带纵向滚动条，滚动不外溢到右栏 */}
          <div
            className="section-tree-scroll"
            style={{
              flex: 1,
              minHeight: 0,
              height: "100%",
              overflowY: "auto",
              overflowX: "hidden",
              overscrollBehavior: "contain",
              paddingRight: 4,
              paddingBottom: 16,
            }}
          >
            {tree.length > 0 ? (
              renderTreeList(annotatedTree)
            ) : (
              <Empty description="请先在目录页面生成目录" />
            )}
          </div>
        </div>

        {/* 右列 flex:1：顶部横条 + 按钮栏 + 主内容（自身独立滚动） */}
        <div className="section-editor-main" style={{ flex: 1, display: "flex", flexDirection: "column", minWidth: 0, overflowX: "hidden", paddingBottom: 12 }}>
          {/* 顶部横条：字数预算 + 生成进度 */}
          <div style={{ display: "flex", gap: 8, padding: "6px 12px", flexShrink: 0, borderBottom: "1px solid #f0f0f0" }}>
          {wordBudget && (
          <Card
            size="small"
            title={<span style={{ fontSize: 12 }}><CalculatorOutlined /> 字数预算</span>}
            style={{ flex: "0 0 25%", minWidth: 0 }}
            styles={{ body: { padding: "4px 10px" } }}
          >
            <div style={{ fontSize: 12 }}>
              <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 2 }}>
                <Text type="secondary">一级章节 ({wordBudget.level1_count} 个)</Text>
                <Text>≥{wordBudget.level1_min} 字/章</Text>
              </div>
              <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 4 }}>
                <Text type="secondary">二级章节 ({wordBudget.level2_count} 个)</Text>
                <Text style={{ color: "#1677ff" }}>
                  {useCustomWords ? `${level2DefaultWords} 字/章` :
                   writingLength === "short" ? "1000~1800字" :
                   writingLength === "standard" ? "1800~3500字" :
                   writingLength === "detailed" ? "3500~5500字" :
                   "5500~7000字"}
                </Text>
              </div>
              <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
                <Select
                  value={writingLength}
                  onChange={handleWritingLengthChange}
                  size="small"
                  style={{ flex: 1 }}
                  disabled={useCustomWords}
                  options={[
                    { value: "short", label: "简短（1000~1800字）" },
                    { value: "standard", label: "标准（1800~3500字）" },
                    { value: "detailed", label: "详细（3500~5500字）" },
                    { value: "extra_long", label: "超长（5500~7000字）" },
                  ]}
                  onMouseDown={(e) => { e.preventDefault(); }}
                />
                {useCustomWords && (
                  <Space.Compact size="small">
                    <InputNumber
                      min={3000} max={10000} step={500}
                      value={level2DefaultWords}
                      onChange={(v) => v && setLevel2DefaultWords(v)}
                      style={{ width: 60 }}
                      onKeyDown={(e) => {
                        if (e.key === "Enter" || e.key === " ") e.preventDefault();
                      }}
                    />
                    <Button disabled size="small">字</Button>
                  </Space.Compact>
                )}
              </div>
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginTop: 4 }}>
                <Text strong style={{ fontSize: 11 }}>预估总字数</Text>
                <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
                  <Text strong style={{ color: "#1677ff", fontSize: 13 }}>
                    ≈{wordBudget.total_estimate.toLocaleString()} 字
                  </Text>
                  <Checkbox checked={useCustomWords} onChange={(e) => handleCustomWordsChange(e.target.checked)} style={{ margin: 0 }}>
                    <Text style={{ fontSize: 11 }}>自定义</Text>
                  </Checkbox>
                </div>
              </div>
            </div>
          </Card>
          )}

          {(isGenerating || isPaused) ? (
            <Card
              size="small"
              title={<span style={{ fontSize: 12 }}>生成进度</span>}
              style={{ flex: 1, minWidth: 0 }}
              styles={{ body: { padding: "4px 10px" } }}
              extra={
                <Space size={4}>
                  {isGenerating && (
                    <>
                      <Tooltip title="暂停生成（可断点续生）">
                        <Button size="small" icon={<PauseCircleOutlined />} onClick={handlePauseGeneration}>暂停</Button>
                      </Tooltip>
                      <Button size="small" danger icon={<StopOutlined />} onClick={handleStopGeneration}>停止</Button>
                    </>
                  )}
                  {isPaused && (
                    <Button size="small" type="primary" icon={<PlayCircleOutlined />} onClick={handleResumeGeneration}>恢复</Button>
                  )}
                </Space>
              }
            >
              <ProgressPanel
                items={progressItems}
                total={progressTotal}
                success={progressSuccess}
                failed={progressFailed}
                cancelled={progressCancelled}
                isRunning={isGenerating}
              />
            </Card>
          ) : (
            <Card
              size="small"
              title={<span style={{ fontSize: 12 }}>生成进度</span>}
              style={{ flex: 1, minWidth: 0 }}
              styles={{ body: { padding: "4px 10px" } }}
            >
              <div style={{ textAlign: "center", color: "#999", fontSize: 12, padding: "4px 0" }}>
                暂无生成任务
              </div>
            </Card>
          )}
          </div>

          {/* 按钮栏 */}
          <div style={{ margin: "8px 12px 0", display: "flex", gap: 6, flexShrink: 0, justifyContent: "space-between" }}>
          <Tooltip title={`共 ${leafCount} 个叶子章节；已完成且内容有效的章节会自动跳过，只生成剩余章节`}>
            <Button
              type="primary"
              size="small"
              icon={<ThunderboltOutlined />}
              onClick={handleGenerateAll}
              loading={isGenerating}
              disabled={leafCount === 0}
            >
              生成全部 ({leafCount})
            </Button>
          </Tooltip>
          <Tooltip title={pendingFailedCount > 0 ? `共 ${pendingFailedCount} 个章节待生成/失败/已取消` : "所有章节均已生成"}>
            <Button
              size="small"
              icon={<ReloadOutlined />}
              onClick={handleRegenerateMissing}
              loading={isGenerating}
              disabled={pendingFailedCount === 0}
            >
              <Badge size="small" count={pendingFailedCount} offset={[4, -2]}>
                补全生成
              </Badge>
            </Button>
          </Tooltip>
          <Button
              size="small"
              icon={<BranchesOutlined />}
              onClick={handleGenerateCurrent}
              disabled={!selectedSection || isGenerating}
            >
              生成当前章节
            </Button>
          {isGenerating && (
            <Tooltip title="暂停生成（可断点续生）">
              <Button
                size="small"
                icon={<PauseCircleOutlined />}
                onClick={handlePauseGeneration}
              >
                暂停
              </Button>
            </Tooltip>
          )}
          {isPaused && (
            <Tooltip title="从断点继续生成">
              <Button
                size="small"
                type="primary"
                icon={<PlayCircleOutlined />}
                onClick={handleResumeGeneration}
              >
                恢复生成
              </Button>
            </Tooltip>
          )}
          {isGenerating && !isPaused && (
            <Button
              size="small"
              danger
              icon={<StopOutlined />}
              onClick={handleStopGeneration}
            >
              停止
            </Button>
          )}
          <Tooltip
            title={
              wordDeficient.count > 0
                ? `${wordDeficient.count} 个已生成章节字数不足，缺口约 ${wordDeficient.deficitWords} 字${pendingFailedCount > 0 ? `；另有 ${pendingFailedCount} 章未生成，建议先补全生成` : ""}`
                : pendingFailedCount > 0
                  ? "没有字数不足的已生成章节；建议先补全生成"
                  : "所有已生成章节字数均达标"
            }
          >
            <Button
              size="small"
              icon={<FileWordOutlined />}
              onClick={handleExpandContent}
              loading={isExpanding}
              disabled={isExpanding || isGenerating || wordDeficient.count === 0}
            >
              <Badge size="small" count={wordDeficient.count} offset={[4, -2]}>
                兜底扩写
              </Badge>
            </Button>
          </Tooltip>
          <Tooltip title="同时生成的章节数；生成/扩写启动后修改需下次任务生效">
            <Select
              size="small"
              value={concurrency}
              onChange={(v) => setConcurrency(v)}
              style={{ width: 90 }}
              options={[
                { value: 2, label: "并发 2" },
                { value: 4, label: "并发 4" },
                { value: 6, label: "并发 6" },
                { value: 8, label: "并发 8" },
              ]}
              disabled={isGenerating || isExpanding}
            />
          </Tooltip>

          {/* ENHANCE 2026-10-05（P-11）：正文编排参数（对齐易标规格六.4/六.5） */}
          <Tooltip title="启用 AI 内容编排：预判每章配图类型与表格密度（表格/图片/mermaid 偏好）">
            <Button
              size="small"
              type={showOrchOptions ? "primary" : "default"}
              icon={<SlidersOutlined />}
              onClick={() => setShowOrchOptions((v) => !v)}
              disabled={isGenerating || isExpanding}
            >
              编排选项{showOrchOptions && useOrchestration ? " ✓" : ""}
            </Button>
          </Tooltip>
        </div>

        {showOrchOptions && (
          <div style={{ margin: "6px 12px 0", padding: "8px 10px", background: "#fafafa", border: "1px solid #f0f0f0", borderRadius: 6, display: "flex", flexWrap: "wrap", gap: "6px 16px", alignItems: "center" }}>
            <Checkbox
              checked={useOrchestration}
              onChange={(e) => setUseOrchestration(e.target.checked)}
              disabled={isGenerating || isExpanding}
            >
              <Text style={{ fontSize: 12 }}>启用 AI 内容编排</Text>
            </Checkbox>
            <Space size={4}>
              <Text style={{ fontSize: 12 }}>表格密度</Text>
              <Select
                size="small"
                value={orchTableRatio}
                onChange={setOrchTableRatio}
                style={{ width: 96 }}
                disabled={isGenerating || isExpanding}
                options={[
                  { value: "none", label: "无表格" },
                  { value: "light", label: "较少" },
                  { value: "moderate", label: "适中" },
                  { value: "heavy", label: "密集" },
                ]}
              />
            </Space>
            <Space size={4}>
              <Text style={{ fontSize: 12 }}>AI 生图上限</Text>
              <InputNumber
                size="small"
                min={0}
                max={50}
                value={orchMaxAiImages}
                onChange={(v) => setOrchMaxAiImages(v ?? 0)}
                style={{ width: 64 }}
                disabled={isGenerating || isExpanding}
              />
            </Space>
            <Space size={4}>
              <Text style={{ fontSize: 12 }}>图表粒度</Text>
              <InputNumber
                size="small"
                min={3}
                max={40}
                value={orchSectionPerSegment}
                onChange={(v) => setOrchSectionPerSegment(v ?? 10)}
                style={{ width: 64 }}
                disabled={isGenerating || isExpanding}
              />
            </Space>
            <Checkbox
              checked={orchPreferMermaid}
              onChange={(e) => setOrchPreferMermaid(e.target.checked)}
              disabled={isGenerating || isExpanding}
            >
              <Text style={{ fontSize: 12 }}>mermaid 优先</Text>
            </Checkbox>
          </div>
        )}

        {autoSavedNotice && (
          <Alert
            message={
              <Space size={4}>
                <SaveOutlined style={{ color: "#52c41a" }} />
                <Text style={{ fontSize: 12 }}>「{autoSavedNotice.title}」已自动保存（{autoSavedNotice.wordCount} 字）</Text>
              </Space>
            }
            type="success"
            showIcon={false}
            style={{ marginBottom: 8, padding: "4px 10px" }}
            closable
            onClose={() => setAutoSavedNotice(null)}
          />
        )}

        <div style={{ marginBottom: 8 }}>
          <Text type="secondary" style={{ fontSize: 11 }}>
            <WarningFilled style={{ color: "#ff7a45" }} /> 橙色背景：字数偏差 ±15% 以上 | <SaveOutlined /> 已保存
          </Text>
        </div>
        {selectedSection && sectionDetail ? (
          <>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 12, flexWrap: "wrap", gap: 8 }}>
              <Space>
                <Tag color={getLevelColor(selectedSection.level)}>{getLevelLabel(selectedSection.level)}</Tag>
                <Text strong style={{ fontSize: 16 }}>{selectedSection.title}</Text>
                {selectedSection.has_content && (
                  <Tag color="green" icon={<SaveOutlined />}>已保存</Tag>
                )}
              </Space>
              <Space>
                <Button
                  size="small"
                  icon={<BranchesOutlined />}
                  onClick={handleGenerateCurrent}
                  disabled={isGenerating}
                  type="default"
                >
                  生成本子章节
                </Button>
                <Button
                  size="small"
                  icon={<EditOutlined />}
                  onClick={() => handleRegenerate(selectedSection.id)}
                  disabled={isGenerating}
                >
                  重新生成
                </Button>
                {isGenerating && (
                  <Tooltip title="暂停生成（可断点续生）">
                    <Button
                      size="small"
                      icon={<PauseCircleOutlined />}
                      onClick={handlePauseGeneration}
                    >
                      暂停
                    </Button>
                  </Tooltip>
                )}
                {isPaused && (
                  <Tooltip title="从断点继续生成">
                    <Button
                      size="small"
                      type="primary"
                      icon={<PlayCircleOutlined />}
                      onClick={handleResumeGeneration}
                    >
                      恢复
                    </Button>
                  </Tooltip>
                )}
              </Space>
            </div>

            <Tabs
              activeKey={activeTab}
              onChange={setActiveTab}
              items={tabItems}
              style={{ flex: 1, display: "flex", flexDirection: "column" }}
            />
            <Divider style={{ marginTop: 0, marginBottom: 12 }} />
            <div style={{ textAlign: "center", marginBottom: 8 }}>
              <Button
                type="primary"
                size="large"
                icon={<FileWordOutlined />}
                onClick={() => navigate(`/projects/${projectId}/export`)}
              >
                下一步：导出 DOCX
              </Button>
            </div>
          </>
        ) : (
          <div style={{ display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", height: "100%" }}>
            <Empty
              description={
                selectedSection
                  ? <Text>已选择「{selectedSection.title}」目录，可使用上方按钮生成章节内容</Text>
                  : "请在左侧选择一个章节，或使用上方按钮并发生成全部正文"
              }
            />
          </div>
        )}
      </div>
    </div>
    </div>
  );
});

