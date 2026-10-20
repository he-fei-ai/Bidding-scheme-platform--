import React, { useEffect, useState, useCallback, useRef, useMemo } from "react";
import { useParams, useNavigate } from "react-router-dom";
import {
  Button, Card, Skeleton, message, Space, Typography, Tag, Input, Empty,
  Spin, Alert, Modal, Popconfirm, Dropdown, Progress, Segmented, Tooltip,
} from "antd";
import {
  ArrowLeftOutlined, ArrowRightOutlined, ThunderboltOutlined, EditOutlined, DeleteOutlined,
  PlusOutlined, ReloadOutlined, CloseCircleOutlined, CheckCircleOutlined, LoadingOutlined, MenuOutlined, UpOutlined,
  DownOutlined, DragOutlined, AuditOutlined, RobotOutlined, FolderOutlined,
  RightOutlined, QuestionCircleOutlined,
} from "@ant-design/icons";
import { taskStore } from "@/services/taskStore";
import {
  getProject, fetchSections, updateSection, deleteSection, createSection,
  moveSection, reorderSections, createOutlineEventSource, type OutlineEventSource,
  reviewOutline, reviewOutlineWithAutofix, clearSections, stopGeneration,
  v51Confirm, fetchPendingV51Task, clearV51PendingTask, getWordControlSettings,
  type WordControlDerived,
  type OutlineMode, getStoredOutlineMode, storeOutlineMode,
} from "../api";
import type { SectionTreeNode } from "../types";
import { extractApiError, annotateOutlineWithNumbers, getLevelColor, getLevelLabel, findNodeInTree, updateNodeInTree, OUTLINE_STEPS_V51, reduceOutlineGeneratingEvent, outlineDictToTreeNodes, nextFreeSortOrder, allocTempNodeId, normalizeNodeTitle } from "../utils";
import RecoveryBanner from "../components/RecoveryBanner";
import OutlineQualityReport, { type OutlineQuality } from "../components/OutlineQualityReport";

const { Text, Title } = Typography;

function countTotalNodes(nodes: SectionTreeNode[]): number {
  // BUG-FIX 2026-10-08（超深层级）：原实现自递归，深目录树（AI 生成异常或
  // 用户手工拖出的长链）在数百层后触发 JS 调用栈上限，页面直接崩白。
  // 改为显式栈迭代，任意深度不爆栈。
  let c = 0;
  const stack: SectionTreeNode[][] = Array.isArray(nodes) ? [nodes] : [];
  while (stack.length > 0) {
    const current = stack.pop()!;
    for (const n of current) {
      if (!n) continue;
      c += 1;
      if (Array.isArray(n.children) && n.children.length > 0) stack.push(n.children);
    }
  }
  return c;
}

function removeNodeFromTree(nodes: SectionTreeNode[], id: number): SectionTreeNode[] {
  return nodes
    .filter((n) => n.id !== id)
    .map((n) => ({
      ...n,
      children: n.children ? removeNodeFromTree(n.children, id) : [],
    }));
}

function addChildToTree(
  nodes: SectionTreeNode[],
  parentId: number | null,
  child: SectionTreeNode,
): SectionTreeNode[] {
  if (parentId === null) {
    return [...nodes, child];
  }
  return nodes.map((n) => {
    if (n.id === parentId) {
      return { ...n, children: [...(n.children || []), child] };
    }
    if (n.children && n.children.length > 0) {
      return { ...n, children: addChildToTree(n.children, parentId, child) };
    }
    return n;
  });
}

export default React.memo(function OutlineEditorPage() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const projectId = Number(id);
  const isProjectIdValid = !isNaN(projectId) && projectId > 0;

  const [project, setProject] = useState<any>(null);
  const [loading, setLoading] = useState(false);
  const [tree, setTree] = useState<SectionTreeNode[]>([]);
  const [isGenerating, setIsGenerating] = useState(false);
  const isGeneratingRef = useRef(false);

  const [progressPercent, setProgressPercent] = useState(0);
  const progressPercentRef = useRef(0);
  // BUG-FIX 2026-10-12：SSE 回调里需要拿到本次生成注册的 taskId 与最新百分比，
  // 但闭包会捕获 startTask 时的旧值。用 ref 承载，避免依赖 stale state。
  const taskIdRef = useRef<string>("");
  // BUG-FIX 2026-10-12（SSE 重连幂等）：已应用的最大事件 seq / 终态是否已落定。
  // 两者都必须是 ref（不是 state）：onmessage 闭包里读 state 会拿到注册回调时的
  // 旧值，去重与幂等守卫会静默失效 —— 与本次同批修掉的 progress stale closure
  // 是同一类缺陷。
  const appliedSeqRef = useRef<number | null>(null);
  const terminalAppliedRef = useRef(false);
  const outlineStepRef = useRef(0);
  const [progressMessage, setProgressMessage] = useState("");
  const [outlineStep, setOutlineStep] = useState(0);
  const [outlineProgress, setOutlineProgress] = useState<{ level1: number; completed: number; total: number } | null>(null);
  const [outlineGenerationDone, setOutlineGenerationDone] = useState(false);
  const [generationEndState, setGenerationEndState] = useState<'completed' | 'error' | 'stopped' | 'cancelled' | null>(null);
  // 2026-10-19 优化 6：目录生成质量报告（completed 事件 quality 字段，后端
  // outline_quality 五维评分）。直出路径/评分关闭时为 null，卡片整体不展示。
  const [outlineQuality, setOutlineQuality] = useState<OutlineQuality | null>(null);
  const [gateIssueCount, setGateIssueCount] = useState<number>(0);
  const [gateSummary, setGateSummary] = useState<string>("");
  const [gateAutoFixed, setGateAutoFixed] = useState<string[]>([]);
  // 预计剩余秒数（按已耗时与当前百分比线性外推；0 表示不展示）
  const [etaSeconds, setEtaSeconds] = useState<number>(0);
  // 本次生成的起始时刻（ETA 计算的基准；ref 避免 SSE 闭包读到旧值）
  const generationStartedAtRef = useRef<number>(0);

  // V5.1 人工审核状态
  const [pendingReviewTree, setPendingReviewTree] = useState<SectionTreeNode[] | null>(null);
  const [reviewModalVisible, setReviewModalVisible] = useState(false);
  const [reviewStage, setReviewStage] = useState<'level1' | 'level2' | 'final'>('level1');
  const [reviewEditTitles, setReviewEditTitles] = useState<Record<number, string>>({});
  const [reviewConfirmed, setReviewConfirmed] = useState(false);

  // V5.1 断点续生：检测未完成的任务
  const [pendingV51Task, setPendingV51Task] = useState<{ has_pending: boolean; phase: string | null; outline: SectionTreeNode[] | null } | null>(null);

  const eventSourceRef = useRef<OutlineEventSource | null>(null);
  const isMountedRef = useRef(true);
  const userStoppedRef = useRef(false);

  // 目录树折叠状态：存储被折叠的节点 ID 集合
  const [collapsedIds, setCollapsedIds] = useState<Set<number>>(new Set());

  // CROSS-FIX 2026-10-04（前后端同步）：目录生成模式（易标规格二.2 直出/分步）。
  // 默认 auto，与后端未传 outline_mode 时的旧行为一致；选择持久化 localStorage。
  const [outlineMode, setOutlineMode] = useState<OutlineMode>(getStoredOutlineMode());

  // 易标 R6/R7 引用：字数控制配置下，当前目录叶子数是否落在允许区间。
  // getWordControlSettings 的 derived 已含 current_leaf_count / leaf_outside_range / leaf_count_range。
  const [leafWarn, setLeafWarn] = useState<{
    configured: boolean;
    outside: boolean;
    current: number;
    range: [number, number | null];
  } | null>(null);

  // 叶子数越界预警刷新：页面加载与每次目录落库变更后调用。
  const refreshLeafWarn = useCallback(async () => {
    if (!isProjectIdValid) return;
    try {
      const res = await getWordControlSettings(projectId);
      const s = res?.settings;
      const d: WordControlDerived | undefined = res?.derived;
      const configured =
        !!s && (s.minimum_words > 0 || s.maximum_words > 0 || s.section_words > 0);
      if (configured && d) {
        setLeafWarn({
          configured: true,
          outside: Boolean(d.leaf_outside_range),
          current: typeof d.current_leaf_count === "number" ? d.current_leaf_count : 0,
          range: d.leaf_count_range || [0, 0],
        });
      } else {
        setLeafWarn({ configured: false, outside: false, current: 0, range: [0, 0] });
      }
    } catch {
      // 配置缺失/接口异常时不阻塞目录编辑
      setLeafWarn(null);
    }
  }, [projectId, isProjectIdValid]);

  useEffect(() => {
    void refreshLeafWarn();
  }, [refreshLeafWarn]);

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

  useEffect(() => {
    isGeneratingRef.current = isGenerating;
  }, [isGenerating]);

  useEffect(() => {
    isMountedRef.current = true;
    return () => {
      isMountedRef.current = false;
      // 2026-08-21 增强：页面卸载时不中止 SSE，仅断开前端监听
      // 后端任务继续在后台运行，用户返回时可重连
      if (eventSourceRef.current) {
        // 契约BUG4 修复：close() 会 abort fetch → 后端检测到 SSE 断开即
        // cancel_event.set() 中止目录生成，与注释"不中止后端任务"矛盾。
        // detach() 只断开前端监听、不 abort 连接，后端任务得以在后台
        // 继续运行，taskStore + /tasks/status 恢复链路才能接续追踪。
        eventSourceRef.current.detach();
        eventSourceRef.current = null;
      }
    };
  }, []);

  const loadTree = useCallback(async () => {
    if (!isProjectIdValid) return;
    setLoading(true);
    try {
      const p = await getProject(projectId, { silent: true });
      setProject(p);
      const t = await fetchSections(projectId);
      setTree(t);
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
  }, [projectId]);

  useEffect(() => {
    loadTree();
  }, [loadTree]);

  // 检测是否有未完成的 V5.1 任务（断点续生）
  useEffect(() => {
    if (!isProjectIdValid) return;
    fetchPendingV51Task(projectId)
      .then((result) => {
        if (result.has_pending && result.phase) {
          setPendingV51Task({
            has_pending: true,
            phase: result.phase,
            outline: result.outline,
          });
        }
      })
      .catch(() => {
        // 忽略错误
      });
  }, [projectId, isProjectIdValid]);

  const startOutlineGeneration = (resume = false) => {
    if (eventSourceRef.current) {
      eventSourceRef.current.close();
      eventSourceRef.current = null;
    }

    // 清除旧的 V5.1 待处理状态（避免意外恢复）
    // BUG 修复：恢复模式（resume=true）下跳过清除——"恢复生成"按钮此前
    // 也走到这里，把用户要恢复的断点状态先删掉再全新生成，与按钮语义相反。
    if (!resume) {
      clearV51PendingTask(projectId).catch(() => {});
    }

    // 2026-08-21 增强：在 taskStore 中注册任务，支持跨页面追踪
    const _taskId = `outline_${projectId}_${Date.now()}`;
    taskIdRef.current = _taskId;
    taskStore.startTask({
      taskId: _taskId,
      projectId,
      taskType: "outline",
      status: "running",
      progress: 0,
      message: "准备生成目录...",
      startedAt: Date.now(),
      lastUpdated: Date.now(),
    });
    setPendingV51Task(null);

    setIsGenerating(true);
    setProgressPercent(0);
    progressPercentRef.current = 0;
    setProgressMessage("准备生成目录...");
    setOutlineStep(0);
    outlineStepRef.current = 0;
    setOutlineProgress(null);
    setOutlineGenerationDone(false);
    setGenerationEndState(null);
    setReviewConfirmed(false);
    // 2026-10-19：重置质量报告与 ETA（上一轮的数据不得残留到新一轮）
    setOutlineQuality(null);
    setGateIssueCount(0);
    setGateSummary("");
    setGateAutoFixed([]);
    setEtaSeconds(0);
    generationStartedAtRef.current = Date.now();
    // 每次生成都是一次全新的事件流：重置 SSE 幂等守卫，
    // 否则上一轮残留的 terminalApplied/seq 会让本轮的终态被当作重放丢弃。
    appliedSeqRef.current = null;
    terminalAppliedRef.current = false;
    userStoppedRef.current = false;

    const eventSource = createOutlineEventSource(projectId, outlineMode);
    let finishedNormally = false;
    // BUG-FIX-CONNECT-01：初始 false，收到 connected 事件后才置 true，
    // 使 onerror 中的连接失败提示分支真正可达
    let hasConnected = false;

    eventSource.onmessage = (event: { data: string }) => {
      try {
        const data = JSON.parse(event.data);

        // BUG-FIX 2026-10-12（SSE 重连幂等）：服务端在重连时会重放
        // `state['last_event']`，而 `task_registry.broadcast_event` 对每一类事件
        // （含终态）都覆盖 `task.last_event` —— 于是客户端已经消费过的进度事件
        // 会被原样重放一次。服务端为此给每条事件盖了单调递增的 `seq`
        // （sse_outline.py `_qput`），这里按 seq 去重，保证「至多应用一次」。
        //
        // 不用 seq 时（服务端未升级 / 事件无 seq）自动跳过该保护，保持兼容。
        const seq = typeof data?.seq === "number" ? data.seq : null;
        if (seq !== null) {
          if (appliedSeqRef.current !== null && seq <= appliedSeqRef.current) return;
          appliedSeqRef.current = seq;
        }

        // 终态幂等：completed/stopped/cancelled/error 各分支都不是幂等的
        //（双弹 toast、两次 loadTree 定时器、close() 调两次）。一旦终态已落定，
        // 后续任何终态事件（含重放）直接忽略。
        const TERMINAL = ["completed", "stopped", "cancelled", "error"];
        if (data && typeof data.status === "string" && TERMINAL.includes(data.status)) {
          if (terminalAppliedRef.current) return;
          terminalAppliedRef.current = true;
        }

        if (data.status === "connected") {
          hasConnected = true;
        } else if (data.status === "generating") {
          // BUG-OUTLINE-03 修复：统一进度计算逻辑（抽取为纯函数 reduceOutlineGeneratingEvent，
          // 与 utils 单测钉住），所有 phase 都按 outline_step 计算百分比，
          // 消除 outline_phase 与其他 phase 的进度跳跃（如 context 阶段突然跳到 90%）
          const patch = reduceOutlineGeneratingEvent(data, OUTLINE_STEPS_V51, {
            step: outlineStepRef.current,
            percent: progressPercentRef.current,
          });
          if (patch.regressed) {
            console.debug(
              `[outline] 忽略回退的进度事件: step=${data.outline_step} < 已展示 ${outlineStepRef.current}`,
            );
          }
          if (patch.outlineStep !== undefined) {
            setOutlineStep(patch.outlineStep);
            outlineStepRef.current = patch.outlineStep;
          }
          if (patch.progressPercent !== undefined) {
            setProgressPercent(patch.progressPercent);
            progressPercentRef.current = patch.progressPercent;
          }
          setProgressMessage(patch.progressMessage);
          // 2026-10-19：预计剩余时间（已耗时 ÷ 当前百分比线性外推；<10% 时
          // 分母太小噪声大不展示，≥100% 清零）。仅生成中有效。
          const _etaPct = patch.progressPercent ?? progressPercentRef.current;
          const _startedAt = generationStartedAtRef.current;
          if (_startedAt > 0 && _etaPct >= 10 && _etaPct < 100) {
            const _elapsedSec = (Date.now() - _startedAt) / 1000;
            setEtaSeconds(
              Math.max(1, Math.round((_elapsedSec * (100 - _etaPct)) / _etaPct))
            );
          } else if (_etaPct >= 100) {
            setEtaSeconds(0);
          }
          // BUG-FIX 2026-10-12（跨模块进度断链）：后端 SSE 在 L2 并发生成期逐章
          // 推送 `progress: { level1, completed, total }`（sse_outline.py:846），
          // 但本页此前**从不读取**该字段，`outlineProgress` 只有初始的 setXxx(null)，
          // 于是「章节进度 n/N 章」区块恒不渲染（死 UI），用户对长方案只能看到一个
          // 不动的总百分比。改为真正消费该字段。
          // completed 取单调上限：重连重放的旧进度事件不得让章数倒退。
          const prog = (data as { progress?: { level1?: number; completed?: number; total?: number } }).progress;
          if (prog && typeof prog.completed === "number" && typeof prog.total === "number") {
            const _total = prog.total;
            const _level1 = typeof prog.level1 === "number" ? prog.level1 : _total;
            setOutlineProgress((prev) => ({
              level1: _level1,
              completed: Math.max(prog.completed as number, prev?.completed ?? 0),
              total: _total,
            }));
          }
          // 顶部全局任务条此前只在 startTask 写一次、此后从不更新，
          // 整个目录生成期间进度条恒为 0%（TaskStatusBar 显示「目录生成 0%」）。
          // 同一处补上 updateTask，让跨页面追踪真正可用。
          const _pct = patch.progressPercent ?? progressPercentRef.current;
          taskStore.updateTask(taskIdRef.current, {
            progress: _pct,
            message: patch.progressMessage,
          });
        } else if (data.status === "completed") {
          finishedNormally = true;
          setIsGenerating(false);
          setOutlineGenerationDone(true);
          setGenerationEndState('completed');
          setProgressPercent(100);
          // 2026-10-19：终态步骤号按 SSE 协议取（13=最终审核通过），
          // 不再硬编码 8；服务端未下发时退回步骤总数兜底
          setOutlineStep(
            typeof data.outline_step === "number" ? data.outline_step : OUTLINE_STEPS_V51.length
          );
          setProgressMessage(data.message || "目录生成完成");
          // 2026-10-19 优化 6：质量评分报告随完成事件下发（直出路径/评分关闭时为 null）
          setOutlineQuality((data.quality as OutlineQuality) ?? null);
          setGateIssueCount(
            typeof data.gate_issue_count === "number" ? data.gate_issue_count : 0
          );
          setGateSummary(typeof data.gate_summary === "string" ? data.gate_summary : "");
          setGateAutoFixed(
            Array.isArray(data.gate_auto_fixed) ? data.gate_auto_fixed.map(String) : []
          );
          setEtaSeconds(0);
          if (data.outline) {
            // BUG-OUTLINE-01 修复：使用 outlineDictToTreeNodes 转换数据，
            // 确保节点包含 sort_order/status/has_content 等前端必需字段，
            // 避免节点状态不显示、排序错乱等问题
            setTree(outlineDictToTreeNodes(data.outline));
          }
          // V5.1 完成：生成过程中的审核门禁（review_needed）已在各阶段逐个确认，
          // 完成后直接标记审核通过，避免最终目录重复弹出审核弹窗
          if (data.outline && data.outline.length > 0) {
            setReviewConfirmed(true);
          }
          message.success(data.message || "目录生成完成");
          // BUG-OUTLINE-05 修复：完成事件后调用 loadTree 从 API 刷新目录树，
          // 确保拿到数据库最新数据（含 status/sort_order/has_content 等字段），
          // 消除 SSE 事件数据不完整导致的节点显示异常
          setTimeout(() => {
            loadTree();
          }, 300);
          // 2026-08-21 增强：在 taskStore 中标记任务完成
          taskStore.completeTask(
            taskStore.getTask(projectId, "outline")?.taskId || `outline_${projectId}`,
            "completed",
            data.message || "目录生成完成"
          );
          eventSource.markTerminalEvent();  // BUG-F-04 修复：标记终端事件，避免 close() 时 abort
          eventSource.close();
          eventSourceRef.current = null;
        } else if (data.status === "stopped") {
          finishedNormally = true;
          setIsGenerating(false);
          setOutlineGenerationDone(true);
          setGenerationEndState('stopped');
          setProgressPercent(100);
          setProgressMessage(data.message || "生成已停止");
          message.info(data.message || "生成已停止");
          taskStore.completeTask(
            taskStore.getTask(projectId, "outline")?.taskId || `outline_${projectId}`,
            "cancelled",
            data.message || "生成已停止"
          );
          eventSource.markTerminalEvent();  // BUG-F-04 修复：标记终端事件，避免 close() 时 abort
          eventSource.close();
          eventSourceRef.current = null;
        } else if (data.status === "cancelled") {
          // BUG-OUTLINE-11 修复：添加 cancelled 状态处理
          finishedNormally = true;
          setIsGenerating(false);
          setOutlineGenerationDone(true);
          setGenerationEndState('cancelled');
          setProgressPercent(100);
          setProgressMessage(data.message || "任务已取消");
          message.info(data.message || "任务已取消");
          taskStore.completeTask(
            taskStore.getTask(projectId, "outline")?.taskId || `outline_${projectId}`,
            "cancelled",
            data.message || "任务已取消"
          );
          eventSource.markTerminalEvent();  // BUG-F-04 修复：标记终端事件，避免 close() 时 abort
          eventSource.close();
          eventSourceRef.current = null;
        } else if (data.status === "error") {
          finishedNormally = true;
          setIsGenerating(false);
          setOutlineGenerationDone(true);
          setGenerationEndState('error');
          setProgressPercent(100);
          setProgressMessage(data.message || "生成失败");
          message.error(data.message || "生成失败");
          taskStore.completeTask(
            taskStore.getTask(projectId, "outline")?.taskId || `outline_${projectId}`,
            "failed",
            data.message || "生成失败"
          );
          eventSource.markTerminalEvent();  // BUG-F-04 修复：标记终端事件，避免 close() 时 abort
          eventSource.close();
          eventSourceRef.current = null;
        } else if (data.status === "fallback") {
          message.warning(data.message || "正在切换兜底提供商...");
        } else if (data.status === "review_auto_confirm") {
          // V5.1 自动确认：记录审核通过状态
          const phase = data.phase || "unknown";
          const phaseLabel = phase === "level1" ? "一级" : phase === "level2" ? "二级" : "最终";
          message.info(`已自动确认${phaseLabel}目录生成结果`);
          if (phase !== "final") {
            setReviewConfirmed(true);
          }
        } else if (data.status === "review_needed") {
          // V5.1 多阶段审核：收到 review_needed 事件时弹出审核 Modal
          const phase = data.phase || "level1";
          const outline = data.outline || [];
          const converted = outlineDictToTreeNodes(outline);
          setPendingReviewTree(converted);
          if (phase === "level2") {
            setReviewStage("level2");
          } else if (phase === "final") {
            setReviewStage("final");
          } else {
            setReviewStage("level1");
          }
          setReviewModalVisible(true);
          // BUG-FIX 2026-10-12（审核弹窗重入）：原实现无条件 `setReviewConfirmed(false)`。
          // 断线重连时服务端可能重放一条**旧的** `review_needed`（`last_event`），
          // 此时工作流其实已经推进到下一个阶段甚至已结束 —— 弹窗会重新弹出盖住
          // 界面，并把「已确认」状态打回未确认，用户不得不重新确认一次。
          // 只在「确实还没确认过任何阶段」时置 false：已确认过说明存在陈旧重放。
          setReviewConfirmed((prev) => (prev ? true : false));
          setReviewEditTitles({});
          const phaseLabel = phase === 'level1' ? '一级' : phase === 'level2' ? '二级' : '最终';
          setProgressMessage(data.message || `请审核${phaseLabel}目录`);
        }
      } catch {
        // ignore parse errors
      }
    };

    eventSource.onerror = (e: unknown) => {
      if (!isMountedRef.current) return;
      if (userStoppedRef.current) {
        eventSource.close();
        eventSourceRef.current = null;
        return;
      }
      if (finishedNormally) {
        eventSource.close();
        eventSourceRef.current = null;
        return;
      }
      // api 层自动重连通知：不关闭连接，仅更新提示
      if (e && typeof e === "object" && (e as { type?: string }).type === "reconnecting") {
        const attempt = (e as { attempt?: number }).attempt ?? 0;
        const maxAttempts = (e as { maxAttempts?: number }).maxAttempts ?? 3;
        setProgressMessage(`连接断开，正在尝试重连(${attempt}/${maxAttempts})...`);
        return;
      }
      if (!hasConnected) {
        setIsGenerating(false);
        message.error("无法连接到生成服务，请检查后端是否正常运行");
        eventSource.close();
        eventSourceRef.current = null;
        return;
      }
      // 已连接过时连接中断：等待 api 层自动重连
    };

    eventSourceRef.current = eventSource;

    // BUG-FIX 2026-09-16（高）：promise 必须挂 catch。
    // api 层 3 次重连全部失败后会 reject，产生 unhandled rejection，
    // 且 isGenerating 永远为 true —— 进度条卡在「正在尝试重连(3/3)...」，只能刷新页面。
    // 对齐 SectionEditorPage 的 promise.catch 兜底模式。
    eventSource.promise.catch(() => {
      if (finishedNormally || !isMountedRef.current || userStoppedRef.current) return;
      setIsGenerating(false);
      setProgressMessage("生成连接中断，任务可能仍在后台运行，请刷新页面查看任务状态");
      message.error("生成连接中断，请稍后刷新页面检查任务状态");
    });
  };

  // 键盘快捷键
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key === "b") {
        e.preventDefault();
        // BUG-FIX-HOTKEY-01：生成中禁止重复触发，避免 close 当前 SSE + 清除断点状态后重新请求
        if (isGeneratingRef.current) {
          message.warning("目录正在生成中，请等待完成或先停止当前生成");
          return;
        }
        startOutlineGeneration();
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [startOutlineGeneration]);

  const handleStopGeneration = () => {
    userStoppedRef.current = true;
    if (eventSourceRef.current) {
      eventSourceRef.current.close();
      eventSourceRef.current = null;
    }
    // 调用后端停止 API，确保后台任务也被取消
    stopGeneration(projectId).catch(() => {});
    setIsGenerating(false);
    setOutlineGenerationDone(true);
    setGenerationEndState('stopped');
    setProgressPercent(100);
    message.info("已停止目录生成");
  };

  const handleEditTitle = async (node: SectionTreeNode) => {
    const { confirm } = Modal;
    // BUG-FIX 2026-09-29：原实现只在 onPressEnter 里保存，点「保存」按钮直接关窗
    // 不落库（按钮无效），用户误以为已改。抽出 doSave 同时挂到 onOk 与回车。
    const doSave = async () => {
      const input = document.getElementById(`edit-title-${node.id}`) as HTMLInputElement | null;
      const newTitle = (input?.value ?? "").trim();
      if (!newTitle) {
        message.warning("标题不能为空");
        return;
      }
      try {
        // BUG-FIX 2026-10-12（前后端标题分歧 / 双重编号）：
        // 后端 `services.sections._strip_title_numbering` 会把用户输入的编号
        // 前缀剥掉再入库（「1.1 施工部署」→ 存「施工部署」），但本处此前
        // 丢弃服务端返回值、直接用原始 newTitle 更新本地树。于是：
        //   界面显示「一、1.1 施工部署」（展示编号 + 标题内嵌编号），
        //   DB 实际是「施工部署」，导出又是「一、施工部署」—— 三处对不上，
        //   且只有手动刷新页面才会纠正。
        // 改为以**服务端返回的规范标题**为准回写本地树；若后端剥过前缀，
        // 额外提示一次，避免用户误以为保存失败。
        const saved = await updateSection(projectId, node.id, { title: newTitle });
        const canonicalTitle = typeof saved?.title === "string" ? saved.title : newTitle;
        setTree((prev) => updateNodeInTree(prev, node.id, { title: canonicalTitle }));
        if (canonicalTitle.trim() !== newTitle) {
          message.info(`编号前缀已自动移除，保存为「${canonicalTitle}」`);
        } else {
          message.success("标题已更新");
        }
      } catch {
        message.error("更新失败");
      }
    };
    confirm({
      title: `编辑「${node.title}」`,
      content: (
        <Input
          defaultValue={node.title}
          id={`edit-title-${node.id}`}
          onPressEnter={doSave}
        />
      ),
      okText: "保存",
      cancelText: "取消",
      onOk: doSave,
      afterClose: () => {
        const input = document.getElementById(`edit-title-${node.id}`) as HTMLInputElement;
        input?.focus();
        input?.select();
      },
    });
    setTimeout(() => {
      const input = document.getElementById(`edit-title-${node.id}`) as HTMLInputElement;
      input?.focus();
      input?.select();
    }, 100);
  };

  const handleDeleteNode = async (node: SectionTreeNode) => {
    try {
      await deleteSection(projectId, node.id);
      setTree((prev) => removeNodeFromTree(prev, node.id));
      message.success("已删除");
      // 易标 R6/R7：删除节点会改变叶子数，刷新越界预警
      void refreshLeafWarn();
    } catch {
      message.error("删除失败");
    }
  };

  const handleMoveNode = async (node: SectionTreeNode, direction: "up" | "down") => {
    try {
      const newTree = await moveSection(projectId, node.id, direction);
      setTree(newTree);
    } catch {
      message.error("移动失败");
    }
  };

  const handleAddChild = async (parentId: number | null) => {
    const { confirm } = Modal;
    const parentNode = parentId ? findNodeInTree(tree, parentId) : null;
    // BUG-FIX 2026-09-29：① 原实现在二级节点下添加子目录也固定传 level=2，
    // 三级章节入库层级错位（多级编号不同步）；level/sort_order 改由服务端按
    // 父子关系/兄弟序推导，前端不再硬编码。② 点「添加」按钮此前不保存（只在
    // 回车生效），补 onOk。③ 局部新节点直接用服务层回传的 level/sort_order，
    // 避免界面位置与刷新后位置不一致。
    const doSave = async () => {
      const input = document.getElementById("add-title-input") as HTMLInputElement | null;
      const title = (input?.value ?? "").trim();
      if (!title) {
        message.warning("请输入目录标题");
        return;
      }
      try {
        const created = await createSection(projectId, {
          title,
          parent_id: parentId,
        });
        const newNode: SectionTreeNode = {
          id: created.id,
          title: created.title,
          level: created.level ?? (parentNode ? (parentNode.level || 1) + 1 : 1),
          sort_order: created.sort_order ?? 0,
          section_type: created.section_type || "text",
          scoring_point_id: created.scoring_point_id,
          status: "pending",
          children: [],
          has_content: false,
          word_count: 0,
        };
        setTree((prev) => addChildToTree(prev, parentId, newNode));
        message.success("已添加");
        // 易标 R6/R7：新增节点会改变叶子数，刷新越界预警
        void refreshLeafWarn();
      } catch {
        message.error("添加失败");
      }
    };
    confirm({
      title: parentId ? `为「${parentNode?.title || "目录"}」添加子目录` : "添加一级目录",
      content: (
        <Input
          id="add-title-input"
          placeholder="请输入目录标题"
          onPressEnter={doSave}
        />
      ),
      okText: "添加",
      cancelText: "取消",
      onOk: doSave,
      afterClose: () => {
        const input = document.getElementById("add-title-input") as HTMLInputElement;
        input?.focus();
      },
    });
    setTimeout(() => {
      const input = document.getElementById("add-title-input") as HTMLInputElement;
      input?.focus();
    }, 100);
  };

  const handleReview = async () => {
    try {
      message.loading({ content: "正在审核目录...", key: "review", duration: 0 });
      const result = await reviewOutline(projectId);
      message.destroy("review");
      if (result.passed) {
        message.success("目录审核通过");
      } else {
        message.warning(`目录审核发现问题：${result.issues.slice(0, 3).join("；")}`);
      }
    } catch (e: any) {
      message.error(extractApiError(e));
    }
  };

  const handleAutoFix = async () => {
    try {
      message.loading({ content: "正在自动修复目录...", key: "autofix", duration: 0 });
      const result = await reviewOutlineWithAutofix(projectId);
      message.destroy("autofix");
      if (result.repaired && result.fixed_outline) {
        // BUG-FIX 2026-09-29：fixed_outline 是 AI 裸 JSON（无数据库 id），直接 setTree
        // 会让后续改名/删除/移动全部失败。后端现在落库后返回带 ID 的 tree，
        // 优先使用；旧版后端无 tree 时退回服务端重新拉取，保证链路不断。
        if (result.tree && result.tree.length > 0) {
          setTree(result.tree);
        } else {
          const fresh = await fetchSections(projectId);
          setTree(fresh);
        }
        message.success("目录已自动修复");
      } else if (result.passed) {
        message.success("目录无需修复");
      } else {
        message.warning(`自动修复后仍有问题：${result.issues.slice(0, 3).join("；")}`);
      }
      // 易标 R6/R7：目录结构变化后刷新叶子数越界预警
      void refreshLeafWarn();
    } catch (e: any) {
      message.error(extractApiError(e));
    }
  };

  const handleClearAll = async () => {
    try {
      await clearSections(projectId);
      await loadTree();
      message.success("目录已清空");
    } catch {
      message.error("清空失败");
    }
  };

  const renderTreeNode = (node: SectionTreeNode & { number?: string }, depth: number = 0, siblingIndex: number = 0, siblingCount: number = 1): React.ReactNode => {
    const hasChildren = node.children && node.children.length > 0;
    const isCollapsed = collapsedIds.has(node.id);
    const isFirstSibling = siblingIndex === 0;
    const isLastSibling = siblingIndex === siblingCount - 1;
    const menuItems: any[] = [
      { key: "edit", icon: <EditOutlined />, label: "编辑标题", onClick: (e: any) => { e.domEvent.stopPropagation(); handleEditTitle(node); } },
      { key: "add", icon: <PlusOutlined />, label: "添加子目录", onClick: (e: any) => { e.domEvent.stopPropagation(); handleAddChild(node.id); } },
      { key: "up", icon: <UpOutlined />, label: "上移", disabled: isFirstSibling, onClick: (e: any) => { e.domEvent.stopPropagation(); handleMoveNode(node, "up"); } },
      { key: "down", icon: <DownOutlined />, label: "下移", disabled: isLastSibling, onClick: (e: any) => { e.domEvent.stopPropagation(); handleMoveNode(node, "down"); } },
      { type: "divider" as const },
      { key: "delete", icon: <DeleteOutlined />, label: "删除", danger: true, onClick: (e: any) => { e.domEvent.stopPropagation(); handleDeleteNode(node); } },
    ];

    return (
      <div key={node.id}>
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: 6,
            padding: "4px 8px",
            borderRadius: 4,
            marginLeft: depth * 20,
            marginRight: 4,
            border: "1px solid transparent",
            marginBottom: 2,
            transition: "all 0.2s",
          }}
          onMouseEnter={(e) => {
            (e.currentTarget as HTMLElement).style.borderColor = "#d9d9d9";
            (e.currentTarget as HTMLElement).style.background = "#fafafa";
          }}
          onMouseLeave={(e) => {
            (e.currentTarget as HTMLElement).style.borderColor = "transparent";
            (e.currentTarget as HTMLElement).style.background = "transparent";
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
                width: 20,
                height: 20,
                minWidth: 20,
                padding: 0,
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                color: "#999",
              }}
              title={isCollapsed ? "展开" : "折叠"}
            />
          ) : (
            <span style={{ width: 20, height: 20, display: "inline-block" }} />
          )}
          <DragOutlined style={{ color: "#bbb", cursor: "grab", fontSize: 14, flexShrink: 0 }} />
          <span style={{ color: getLevelColor(node.level), fontSize: 11, fontWeight: 600, minWidth: 48, flexShrink: 0 }}>
            {getLevelLabel(node.level)}
          </span>
          {/* ENHANCE 2026-10-08（功能缺口 · 编号命名空间统一）：annotatedTree 一直
              在算、却从未被渲染，导致本模块自己派生的标准编号（第一节、/ 1.1、）
              在目录编辑页完全看不到 —— 用户核对目录结构时只能靠层级标签，
              与导出 DOCX 的编号无法对照。正文编辑页已渲染 node.number，
              这里补齐同一口径（两者都出自 annotateOutlineWithNumbers）。 */}
          {node.number && (
            <span
              style={{
                color: "#8c8c8c",
                fontSize: 11,
                flexShrink: 0,
                fontVariantNumeric: "tabular-nums",
              }}
            >
              {node.number}
            </span>
          )}
          <Text
            ellipsis
            style={{
              flex: 1,
              fontSize: 13,
              fontWeight: node.level === 1 ? 600 : 400,
            }}
          >
            {node.title}
          </Text>
          {hasChildren && (
            <Button
              type="text"
              size="small"
              icon={<PlusOutlined style={{ color: "#52c41a" }} />}
              onClick={(e) => { e.stopPropagation(); handleAddChild(node.id); }}
              style={{ padding: "0 4px", flexShrink: 0 }}
              title="添加子目录"
            />
          )}
          <Dropdown menu={{ items: menuItems }} trigger={["click"]} placement="topRight">
            <Button
              type="text"
              size="small"
              icon={<MenuOutlined style={{ color: "#999" }} />}
              style={{ padding: "0 4px", flexShrink: 0 }}
              title="更多操作"
            />
          </Dropdown>
        </div>
        {/* 折叠状态下不渲染子节点 */}
        {hasChildren && !isCollapsed && node.children.map((child, idx) => renderTreeNode(child, depth + 1, idx, node.children.length))}
      </div>
    );
  };

  const totalNodes = countTotalNodes(tree);
  const annotatedTree = useMemo(() => annotateOutlineWithNumbers(tree), [tree]);
  // ENHANCE 2026-10-12（编号命名空间统一）：V5.1 审核弹窗此前自己用
  // `index+1` / `index+1.ci+1` / `index+1.ci+1.gi+1` 现算了一套**独立**编号，
  // 与主目录树（第一节、/ 一、/ 1、，来自 utils.formatV21Heading）、后端
  // 落库口径（1 / 1.2 / 1.2.3）三套并存 —— 同一页面内两处显示不同编号，
  // 用户在审核阶段看到的序号与最终目录/导出完全对不上。
  // 现复用主树同一份 annotateOutlineWithNumbers，审核弹窗与主树、后端
  // 「层级由树位置派生」这一条不变量对齐。
  const annotatedReviewTree = useMemo(
    () => (pendingReviewTree ? annotateOutlineWithNumbers(pendingReviewTree) : null),
    [pendingReviewTree],
  );

  // ===== V5.1 人工审核处理函数 =====
  const handleReviewEditTitle = (nodeId: number, value: string) => {
    setReviewEditTitles((prev) => ({ ...prev, [nodeId]: value }));
  };

  const handleReviewMoveNode = (index: number, direction: "up" | "down") => {
    if (!pendingReviewTree) return;
    const newTree = [...pendingReviewTree];
    const targetIdx = direction === "up" ? index - 1 : index + 1;
    if (targetIdx < 0 || targetIdx >= newTree.length) return;
    [newTree[index], newTree[targetIdx]] = [newTree[targetIdx], newTree[index]];
    setPendingReviewTree(newTree);
  };

  const handleReviewDeleteNode = (index: number) => {
    if (!pendingReviewTree) return;
    const newTree = pendingReviewTree.filter((_, i) => i !== index);
    setPendingReviewTree(newTree);
  };

const handleReviewAddNode = () => {
    if (!pendingReviewTree) return;
    // BUG-FIX 2026-10-08（原实现取「顶层节点的最小 id 再递减」）：
    //  (a) 只扫了顶层，L2/最终审核阶段子节点带的是另一批临时 id，取顶层最小值
    //      必然与某个子节点撞号 → 同 id 节点共用一份 reviewEditTitles 记录，
    //      又一次「改一个改名一片」；
    //  (b) 顶层最小 id 每次新增都复用同一个值，「添加→删除→再添加」会与残留
    //      id 冲突（BUG-FIX-TEMPID-01 只解决了顶层，未覆盖新增子节点）。
    // 改用与 outlineDictToTreeNodes 同一个全局单调递减分配器，永不撞号。
    const newNode: SectionTreeNode = {
      id: allocTempNodeId(),
      title: "新章节",
      level: 1,
      // sort_order 原用 pendingReviewTree.length：与已有节点的 sort_order 撞号
      // 时落库排序不确定。改为 1 起找第一个空位（与 outlineDictToTreeNodes 口径一致）。
      sort_order: nextFreeSortOrder(pendingReviewTree),
      section_type: "text",
      scoring_point_id: null,
      status: "pending",
      children: [],
      has_content: false,
      word_count: 0,
    };
    setPendingReviewTree([...pendingReviewTree, newNode]);
  };

  const handleReviewConfirm = async () => {
    if (!pendingReviewTree) return;

    // 应用编辑后的标题（递归应用到所有层级）
const applyTitles = (nodes: SectionTreeNode[]): SectionTreeNode[] =>
      nodes.map((node) => ({
        ...node,
        // BUG-FIX-EMPTYTITLE-01：区分"未编辑"与"编辑为空"，
        // 避免用户清空标题后 falsy 回退到原标题导致空标题校验失效
        title:
          reviewEditTitles[node.id] !== undefined
            ? reviewEditTitles[node.id]
            : typeof node.title === "string"
              ? node.title
              : normalizeNodeTitle(node.title),
        children: node.children ? applyTitles(node.children) : [],
      }));
    const finalTree = applyTitles(pendingReviewTree);

// 检查是否有空标题（递归检查所有层级）
    // BUG-FIX 2026-10-08（空数据/编码）：原实现 `!n.title.trim()` 在 title
    // 非字符串（null/数字）时抛 TypeError，整个审核确认流程中断且无提示。
    // 统一用 String(...) 归一化后再判空。
    const titleOf = (n: SectionTreeNode): string =>
      typeof n.title === "string" ? n.title.trim() : String(n.title ?? "").trim();
    const hasEmptyTitle = (nodes: SectionTreeNode[]): boolean =>
      nodes.some((n) => !titleOf(n) || (n.children ? hasEmptyTitle(n.children) : false));
    if (hasEmptyTitle(finalTree)) {
      message.warning("存在空标题，请完善后再确认");
      return;
    }

    // 调用 V5.1 审核确认 API，通知后端继续生成
    message.loading({ content: "正在提交审核结果...", key: "v51_review", duration: 0 });
    try {
      const result = await v51Confirm(projectId, { edited_outline: finalTree });
      message.destroy("v51_review");
      if (reviewStage === 'final') {
        message.success("最终目录审核通过，正在保存目录...");
        setProgressMessage("最终审核通过，正在保存目录...");
      } else if (reviewStage === 'level1') {
        message.success("一级目录审核通过，继续生成二级目录...");
        setProgressMessage("一级审核通过，正在生成二级目录...");
      } else {
        message.success("二级目录审核通过，继续生成三级目录...");
        setProgressMessage("二级审核通过，正在生成三级目录...");
      }
      setReviewModalVisible(false);
      setReviewConfirmed(true);
      // 易标 R6/R7：最终目录保存后刷新叶子数越界预警
      void refreshLeafWarn();
    } catch (e: any) {
      message.destroy("v51_review");
      message.error("审核确认失败: " + (extractApiError(e) || "未知错误"));
    }
  };

  const handleReviewRegenerate = () => {
    setReviewModalVisible(false);
    setPendingReviewTree(null);
    // 取消当前 V5.1 任务并重新生成
    stopGeneration(projectId).catch(() => {});
    if (eventSourceRef.current) {
      eventSourceRef.current.close();
      eventSourceRef.current = null;
    }
    setTimeout(() => {
      startOutlineGeneration();
    }, 300);
  };

  if (loading) {
    return <Skeleton active paragraph={{ rows: 8 }} />;
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 0, flex: 1, overflow: "hidden" }}>
      {/* 后台任务恢复提示 */}
      <RecoveryBanner projectId={projectId} />
      {/* LAYOUT-FIX 2026-10-12：顶栏拆成「标题行 + 操作行」两条独立 flex 行。
          旧实现是单行 space-between 塞两个 Space：项目名（自带创建时间戳）一长，
          左侧 Space 就被右侧按钮挤到极窄，标题按字符逐字折行成竖排，按钮组也被
          压得贴脸。现在标题行只保证「返回 + 目录编辑 + 状态标签」单行完整显示，
          项目名降级为副标题并省略号截断（悬停看全称）；操作按钮独占一行，窄屏自行换行。 */}
      <div style={{ display: "flex", alignItems: "center", gap: 12, padding: "8px 16px 0", minWidth: 0 }}>
        <Button
          icon={<ArrowLeftOutlined />}
          onClick={() => navigate(`/projects/${projectId}/files`)}
          size="small"
          style={{ flexShrink: 0 }}
        >
          返回
        </Button>
        <Title level={4} style={{ margin: 0, flexShrink: 0, whiteSpace: "nowrap" }}>
          目录编辑
        </Title>
        {project?.name && (
          <Tooltip title={project.name}>
            <Text
              type="secondary"
              style={{
                fontSize: 13,
                minWidth: 0,
                flex: "0 1 auto",
                whiteSpace: "nowrap",
                overflow: "hidden",
                textOverflow: "ellipsis",
              }}
            >
              {project.name}
            </Text>
          </Tooltip>
        )}
        <Space size={4} style={{ flexShrink: 0, marginLeft: "auto" }}>
          {tree.length > 0 && (
          <Tag color="blue">{totalNodes} 个目录节点</Tag>
          )}
          {reviewConfirmed && (
            <Tag color="green" icon={<CheckCircleOutlined />}>审核已通过</Tag>
          )}
        </Space>
      </div>
      <div style={{ display: "flex", justifyContent: "flex-end", padding: "8px 16px 0", minWidth: 0 }}>
        <Space wrap>
          {/* CROSS-FIX 2026-10-04：生成模式选择器，透传后端 outline_mode */}
          {!isGenerating && (
          <Tooltip title="自动：按评分规模智能判别直出/分步；直出：短标书一次性生成；分步：逐层渐进生成">
            <Segmented
              size="small"
              value={outlineMode}
              options={[
                { label: "自动", value: "auto" },
                { label: "直出", value: "direct" },
                { label: "分步", value: "stepwise" },
              ]}
              onChange={(v) => {
                const mode = v as OutlineMode;
                setOutlineMode(mode);
                storeOutlineMode(mode);
              }}
            />
          </Tooltip>
          )}
          <Button
            type="primary"
            icon={isGenerating ? <Spin size="small" /> : <ThunderboltOutlined />}
            onClick={() => startOutlineGeneration()}
            loading={isGenerating}
          >
            {isGenerating ? "生成中..." : "生成目录"}
          </Button>
          {pendingV51Task?.has_pending && !isGenerating && (
            <Button
              type="default"
              icon={<ReloadOutlined />}
              onClick={() => {
                message.info(`正在恢复 V5.1 任务（当前阶段: ${pendingV51Task.phase}）...`);
                startOutlineGeneration(true);
              }}
            >
              恢复生成
            </Button>
          )}
          {tree.length > 0 && !isGenerating && (
            <>
              <Button
                icon={<AuditOutlined />}
                onClick={() => {
                  if (reviewConfirmed) {
                    // auto_confirm 模式已自动通过，重置后允许重新编辑
                    message.info("已自动确认，如需重新编辑请直接修改目录");
                    return;
                  }
                  if (pendingReviewTree) {
                    // 有未完成的审核（review_needed 事件），直接使用
                    setReviewModalVisible(true);
                  } else {
                    // 无 pending 审核，使用当前树
                    setPendingReviewTree(tree);
                    setReviewStage('level1');
                    setReviewModalVisible(true);
                  }
                }}
                type={reviewConfirmed ? "default" : "primary"}
                ghost={reviewConfirmed}
              >
                {reviewConfirmed ? "重新审核" : "V5.1 审核确认"}
              </Button>
              <Button
                icon={<AuditOutlined />}
                onClick={handleReview}
              >
                审核目录
              </Button>
              <Button
                icon={<RobotOutlined />}
                onClick={handleAutoFix}
              >
                自动修复
              </Button>
              <Popconfirm
                title="确定清空所有目录？"
                onConfirm={handleClearAll}
                okText="清空"
                cancelText="取消"
                okButtonProps={{ danger: true }}
              >
                <Button danger icon={<DeleteOutlined />} disabled={isGenerating}>
                  清空目录
                </Button>
              </Popconfirm>
              <Button
                icon={<ReloadOutlined />}
                onClick={loadTree}
                disabled={isGenerating}
              >
                刷新
              </Button>
            </>
          )}
          <Button icon={<ArrowRightOutlined />} onClick={() => navigate(`/projects/${projectId}/sections`)} size="small" disabled={!tree.length}>
            进入正文编辑
          </Button>
        </Space>
      </div>

      {(isGenerating || outlineGenerationDone) && (
        <Card
          size="small"
          title={
            <Space>
              {isGenerating && <Spin size="small" />}
              <Text strong>
                {isGenerating ? "目录生成进度" 
                 : generationEndState === 'completed' ? "目录生成完成"
                 : generationEndState === 'error' ? "目录生成失败"
                 : generationEndState === 'stopped' ? "生成已停止"
                 : generationEndState === 'cancelled' ? "任务已取消"
                 : "目录生成完成"}
              </Text>
              {isGenerating && (
                <Tag color="processing" icon={<LoadingOutlined />}>
                  执行中
                </Tag>
              )}
              {/* 2026-10-19：完成态标题展示质量评分徽章 */}
              {!isGenerating && generationEndState === 'completed' && outlineQuality && (
                <Tag
                  color={
                    outlineQuality.grade?.startsWith("A")
                      ? "success"
                      : outlineQuality.grade?.startsWith("B")
                      ? "processing"
                      : outlineQuality.grade?.startsWith("C")
                      ? "warning"
                      : "error"
                  }
                >
                  质量 {outlineQuality.total_score} 分（{outlineQuality.grade}）
                </Tag>
              )}
            </Space>
          }
          style={{ marginBottom: 16 }}
        >
          {/* 当前阶段名称 + 预计剩余 */}
          <div style={{ marginBottom: 12, padding: "8px 12px", backgroundColor: "#f6f8fa", borderRadius: 6, border: "1px solid #e8e8e8", display: "flex", justifyContent: "space-between", alignItems: "center" }}>
            <Space>
              <span style={{ color: "#8c8c8c", fontSize: 12 }}>当前阶段：</span>
              <Text strong style={{ fontSize: 13 }}>{progressMessage || OUTLINE_STEPS_V51[outlineStep - 1] || "准备中..."}</Text>
            </Space>
            {/* 2026-10-19：预计剩余时间（ETA）。仅生成中且百分比 ≥10% 时展示 */}
            {isGenerating && etaSeconds > 0 && (
              <Text type="secondary" style={{ fontSize: 11 }}>
                预计剩余约 {etaSeconds >= 60 ? `${Math.round(etaSeconds / 60)} 分 ${etaSeconds % 60} 秒` : `${etaSeconds} 秒`}
              </Text>
            )}
          </div>

          {/* 进度条 */}
          <div style={{ marginBottom: 8 }}>
            <Progress
              percent={progressPercent}
              status={generationEndState === 'error' ? 'exception' : generationEndState === 'stopped' || generationEndState === 'cancelled' ? 'normal' : generationEndState === 'completed' ? 'success' : 'active'}
              showInfo
              size="small"
            />
          </div>

          {/* 章节生成进度 */}
          {outlineProgress && (
            <div style={{ marginBottom: 12, display: "flex", justifyContent: "space-between", alignItems: "center", padding: "6px 10px", backgroundColor: outlineProgress.completed >= outlineProgress.total ? "#f6ffed" : "#e6f7ff", borderRadius: 4 }}>
              <Space>
                <span style={{ fontSize: 12, color: "#595959" }}>章节进度</span>
                <Text strong style={{ fontSize: 13, color: outlineProgress.completed >= outlineProgress.total ? "#52c41a" : "#1677ff" }}>
                  {outlineProgress.completed} / {outlineProgress.total}
                </Text>
                <span style={{ fontSize: 12, color: "#8c8c8c" }}>章</span>
              </Space>
              <Text type="secondary" style={{ fontSize: 11 }}>
                {outlineProgress.completed > 0 && outlineProgress.total > 0
                  ? `第 ${outlineProgress.completed} 章：${progressMessage?.replace("正在生成第", "").replace(`/${outlineProgress.total} 章`, "").trim() || ""}`
                  : ""}
              </Text>
            </div>
          )}

          {/* 进度步骤列表 */}
          <div style={{ marginTop: 12 }}>
            <Text type="secondary" style={{ fontSize: 12, marginBottom: 8, display: "block" }}>工作流程</Text>
            <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
              {OUTLINE_STEPS_V51.map((label, index) => {
                const stepNum = index + 1;
                const isCompleted = outlineStep > stepNum || (generationEndState === 'completed' && outlineStep >= stepNum);
                const isActive = outlineStep === stepNum && isGenerating;
                const icon = isCompleted ? <CheckCircleOutlined style={{ color: "#52c41a" }} />
                  : isActive ? <LoadingOutlined style={{ color: "#1677ff" }} spin />
                  : <span style={{ color: "#d9d9d9", fontSize: 14 }}>○</span>;
                return (
                  <div
                    key={index}
                    style={{
                      display: "flex",
                      alignItems: "center",
                      gap: 8,
                      padding: "4px 8px",
                      borderRadius: 4,
                      backgroundColor: isActive ? "#e6f7ff" : isCompleted ? "#f6ffed" : "transparent",
                      border: isActive ? "1px solid #91d5ff" : isCompleted ? "1px solid #b7eb8f" : "1px solid transparent",
                    }}
                  >
                    <span style={{ fontSize: 11, color: "#8c8c8c", width: 16, textAlign: "center" }}>{stepNum}</span>
                    {icon}
                    <Text style={{ fontSize: 12, color: isCompleted ? "#52c41a" : isActive ? "#1677ff" : "#8c8c8c" }}>
                      {label}
                    </Text>
                    {isActive && <Tag color="processing" style={{ marginLeft: "auto", fontSize: 10, margin: 0 }}>执行中</Tag>}
                    {isCompleted && <Tag color="success" style={{ marginLeft: "auto", fontSize: 10, margin: 0 }}>已完成</Tag>}
                  </div>
                );
              })}
            </div>
          </div>

          {/* 2026-10-19 优化 6：完成态展示「目录生成质量报告」卡片
              （五维评分 + 门禁问题摘要 + 自动修复项；quality 为 null 时组件返回 null） */}
          {!isGenerating && generationEndState === 'completed' && (
            <OutlineQualityReport
              quality={outlineQuality}
              gateIssueCount={gateIssueCount}
              gateSummary={gateSummary}
              gateAutoFixed={gateAutoFixed}
            />
          )}

          {/* 停止按钮 */}
          <div style={{ marginTop: 12, textAlign: "right" }}>
            {isGenerating && (
              <Button size="small" danger icon={<CloseCircleOutlined />} onClick={handleStopGeneration}>
                停止生成
              </Button>
            )}
          </div>
        </Card>
      )}

      {!isGenerating && tree.length === 0 && (
        <Card>
          <Empty
            description={
              <Space direction="vertical">
                <Text type="secondary">暂无目录，请先点击「生成目录」按钮</Text>
                <Text type="secondary" style={{ fontSize: 12 }}>
                  系统将自动提取技术评分大类，构建层次化目录结构
                </Text>
              </Space>
            }
          >
            <Button
              type="primary"
              icon={<ThunderboltOutlined />}
              onClick={() => startOutlineGeneration()}
            >
              生成目录
            </Button>
          </Empty>
        </Card>
      )}

      {tree.length > 0 && (
        <Card
          size="small"
          title={
            <Space>
              <FolderOutlined />
              <span>目录结构</span>
              <Tooltip title="查看字数控制标准（易标 R1–R15）">
                <Button
                  type="text"
                  size="small"
                  icon={<QuestionCircleOutlined />}
                  onClick={() => navigate("/word-control-standard")}
                />
              </Tooltip>
            </Space>
          }
          extra={
            <Space>
              <Button
                size="small"
                icon={<PlusOutlined />}
                onClick={() => handleAddChild(null)}
              >
                添加一级目录
              </Button>
              <Button
                size="small"
                icon={<ReloadOutlined />}
                onClick={loadTree}
              >
                刷新
              </Button>
            </Space>
          }
        >
          <div style={{ maxHeight: 500, overflowY: "auto", border: "1px solid #f0f0f0", borderRadius: 6, padding: 8 }}>
            {annotatedTree.map((node, idx) => <div key={node.id}>{renderTreeNode(node, 0, idx, annotatedTree.length)}</div>)}
          </div>
          <Alert
            style={{ marginTop: 12 }}
            message="提示"
            description="点击目录右侧「···」可编辑标题、添加子目录、上移/下移或删除。快捷键：Ctrl+B 生成目录 / Ctrl+G 生成正文"
            type="info"
            showIcon
          />
          {/* 易标 R6/R7 引用：叶子（小节）数越界预警，手工编辑目录后可能使正文字数不符合预期 */}
          {leafWarn && leafWarn.configured && (
            <Alert
              style={{ marginTop: 12 }}
              type={leafWarn.outside ? "warning" : "success"}
              showIcon
              message={
                leafWarn.outside
                  ? "当前目录叶子数可能不符合字数控制预期"
                  : "当前目录叶子数在允许区间内"
              }
              description={
                <span style={{ fontSize: 12 }}>
                  目标小节数允许区间（易标 R6/R7）：
                  <Text strong>{leafWarn.range[0]}</Text> ~{" "}
                  <Text strong>{leafWarn.range[1] ?? "不限"}</Text> 节；
                  当前目录共 <Text strong>{leafWarn.current}</Text> 个叶子章节。
                  {leafWarn.outside
                    ? " 您手动修改的目录可能导致生成正文字数不符合预期，建议调整字数配置或重新生成目录。"
                    : " 已满足字数控制要求。"}
                </span>
              }
            />
          )}
        </Card>
      )}

      {/* ===== V5.1 人工审核 Modal ===== */}
      <Modal
        title={
          <Space>
            <AuditOutlined />
            <span>
              {reviewStage === 'level1' ? '一级目录审核' 
               : reviewStage === 'level2' ? '二级目录审核'
               : '最终目录审核'}
            </span>
            <Tag color="blue">V5.1 参考样本</Tag>
            <Tag color="processing" style={{ fontSize: 10 }}>阶段 {reviewStage === 'level1' ? '1/3' : reviewStage === 'level2' ? '2/3' : '3/3'}</Tag>
          </Space>
        }
        open={reviewModalVisible}
        onCancel={() => {
          setReviewModalVisible(false);
          if (isGenerating) {
            message.info("审核窗口已关闭，您可随时点击「V5.1 审核确认」按钮重新打开");
          }
        }}
        width={reviewStage === 'final' ? 960 : 800}
        footer={
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
            <Space>
              <Button onClick={handleReviewRegenerate} icon={<ReloadOutlined />}>
                重新生成
              </Button>
              <Button onClick={() => {
                setReviewModalVisible(false);
                if (isGenerating) {
                  message.info("审核窗口已关闭，您可随时点击「V5.1 审核确认」按钮重新打开");
                }
              }}>
                稍后处理
              </Button>
            </Space>
            <Space>
              <Button
                type="primary"
                icon={<CheckCircleOutlined />}
                onClick={handleReviewConfirm}
              >
                {reviewStage === 'final' ? '确认通过' : '确认并继续'}
              </Button>
            </Space>
          </div>
        }
      >
        <div style={{ marginBottom: 12 }}>
          <Alert
            message={
              reviewStage === 'level1'
                ? '请审核 AI 生成的一级目录，可编辑标题、调整顺序、删除或新增章节。确认后将继续生成二级目录。'
                : reviewStage === 'level2'
                ? '请审核二级目录结构，确认后将继续生成三级目录。'
                : '请确认最终目录结构（完整三级树），审核通过后方可保存目录并继续生成正文内容。'
            }
            type="info"
            showIcon
            style={{ marginBottom: 12 }}
          />
          <div style={{ marginBottom: 8, display: "flex", gap: 8, alignItems: "center" }}>
            <Text type="secondary" style={{ fontSize: 12 }}>
              共 {pendingReviewTree?.length || 0} 个一级章节
            </Text>
            {reviewStage !== 'final' && (
              <Button
                size="small"
                icon={<PlusOutlined />}
                onClick={handleReviewAddNode}
              >
                添加章节
              </Button>
            )}
          </div>
        </div>
        <div style={{ maxHeight: 450, overflowY: "auto", border: "1px solid #f0f0f0", borderRadius: 6, padding: "8px 4px" }}>
          {annotatedReviewTree?.map((node, index) => (
            <div key={node.id}>
              <div
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: 8,
                  padding: "6px 8px",
                  borderBottom: "1px solid #f0f0f0",
                  backgroundColor: index % 2 === 0 ? "#fafafa" : "transparent",
                }}
              >
                <span style={{ color: "#8c8c8c", fontSize: 11, minWidth: 20, textAlign: "center" }}>
                  {node.number || index + 1}
                </span>
                <span style={{ color: getLevelColor(1), fontSize: 11, fontWeight: 600, minWidth: 48, flexShrink: 0 }}>
                  {getLevelLabel(1)}
                </span>
                <Input
                  size="small"
                  defaultValue={node.title}
                  onChange={(e) => handleReviewEditTitle(node.id, e.target.value)}
                  style={{ flex: 1, fontSize: 13, fontWeight: 600 }}
                  variant="borderless"
                  onFocus={(e) => {
                    e.currentTarget.style.background = "#fff";
                    e.currentTarget.style.border = "1px solid #d9d9d9";
                  }}
                  onBlur={(e) => {
                    e.currentTarget.style.background = "transparent";
                    e.currentTarget.style.border = "none";
                  }}
                />
                {reviewStage !== 'final' && (
                  <>
                    <Button
                      type="text" size="small" icon={<UpOutlined />}
                      disabled={index === 0}
                      onClick={() => handleReviewMoveNode(index, "up")}
                      style={{ padding: "0 4px" }} title="上移"
                    />
                    <Button
                      type="text" size="small" icon={<DownOutlined />}
                      disabled={index === (annotatedReviewTree?.length ?? 0) - 1}
                      onClick={() => handleReviewMoveNode(index, "down")}
                      style={{ padding: "0 4px" }} title="下移"
                    />
                    <Popconfirm
                      title={`删除「${node.title}」？`}
                      onConfirm={() => handleReviewDeleteNode(index)}
                      okText="删除" cancelText="取消"
                      okButtonProps={{ danger: true }}
                    >
                      <Button type="text" size="small" danger icon={<DeleteOutlined />} style={{ padding: "0 4px" }} title="删除" />
                    </Popconfirm>
                  </>
                )}
                {node.children && node.children.length > 0 && (
                  <span style={{ color: "#bbb", fontSize: 11, marginLeft: 4 }}>
                    {node.children.length} 个子章节
                  </span>
                )}
              </div>
              {/* 二级/最终审核时展示二级目录（一级审核阶段无需展示） */}
              {reviewStage !== 'level1' && node.children && node.children.length > 0 && node.children.map((child, ci) => (
                <div key={child.id} style={{ paddingLeft: 40 }}>
                  <div
                    style={{
                      display: "flex", alignItems: "center", gap: 8,
                      padding: "4px 8px", borderBottom: "1px solid #f5f5f5",
                      backgroundColor: "#fafafa",
                    }}
                  >
                    <span style={{ color: "#bbb", fontSize: 10, minWidth: 30, textAlign: "right" }}>
                      {child.number || `${index + 1}.${ci + 1}`}
                    </span>
                    <span style={{ color: getLevelColor(2), fontSize: 10, fontWeight: 500, minWidth: 36, flexShrink: 0 }}>
                      {getLevelLabel(2)}
                    </span>
                    {reviewStage === 'level2' ? (
                      <Input
                        size="small"
                        defaultValue={child.title}
                        onChange={(e) => handleReviewEditTitle(child.id, e.target.value)}
                        style={{ flex: 1, fontSize: 12 }}
                        variant="borderless"
                        onFocus={(e) => {
                          e.currentTarget.style.background = "#fff";
                          e.currentTarget.style.border = "1px solid #d9d9d9";
                        }}
                        onBlur={(e) => {
                          e.currentTarget.style.background = "transparent";
                          e.currentTarget.style.border = "none";
                        }}
                      />
                    ) : (
                      <Text
                        style={{ flex: 1, fontSize: 12 }}
                        ellipsis
                      >
                        {child.title}
                      </Text>
                    )}
                    {child.children && child.children.length > 0 && (
                      <span style={{ color: "#bbb", fontSize: 10 }}>
                        {child.children.length} 项
                      </span>
                    )}
                  </div>
                  {/* 最终审核时展示三级目录 */}
                  {reviewStage === 'final' && child.children && child.children.length > 0 && child.children.map((grandchild, gi) => (
                    <div key={grandchild.id} style={{ paddingLeft: 60 }}>
                      <div
                        style={{
                          display: "flex", alignItems: "center", gap: 8,
                          padding: "3px 8px", borderBottom: "1px solid #fafafa",
                          backgroundColor: "#fcfcfc",
                        }}
                      >
                        <span style={{ color: "#ccc", fontSize: 10, minWidth: 45, textAlign: "right" }}>
                          {grandchild.number || `${index + 1}.${ci + 1}.${gi + 1}`}
                        </span>
                        <span style={{ color: getLevelColor(3), fontSize: 10, fontWeight: 400, minWidth: 36, flexShrink: 0 }}>
                          {getLevelLabel(3)}
                        </span>
                        <Text
                          style={{ flex: 1, fontSize: 11, color: "#595959" }}
                          ellipsis
                        >
                          {grandchild.title}
                        </Text>
                      </div>
                    </div>
                  ))}
                </div>
              ))}
            </div>
          ))}
          {(!pendingReviewTree || pendingReviewTree.length === 0) && (
            <Empty description="暂无章节" />
          )}
        </div>
      </Modal>
    </div>
  );
});
