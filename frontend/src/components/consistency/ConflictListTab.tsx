import { useCallback, useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  Alert, Button, Card, Empty, Input, Modal, Segmented, Space, Table, Tag, Typography, message,
} from "antd";
import {
  BulbOutlined, CheckCircleOutlined, CheckOutlined, CloseCircleOutlined,
  ReloadOutlined, ThunderboltOutlined, WarningOutlined,
} from "@ant-design/icons";
import {
  applyAutoFix, getAuditSummary, getConflicts, getFixSuggestion, repairConsistencyWithAi,
  runConsistencyAudit,
} from "../../api";
import type {
  AiRepairReport, AuditResult, AuditSummary, AutoFixReport, Conflict, ConflictFilter,
  ConsistencyRules,
} from "../../api";
import { extractApiError, mapConsistencySeverity, parseConflictSources } from "../../utils";
import AuditSummaryCards from "./AuditSummaryCards";

const { Text, Paragraph } = Typography;

/** 冲突表默认展示的来源条数，其余折叠为 +N */
const SOURCE_PREVIEW = 3;

/** 冲突筛选口径 */
const FILTER_OPTIONS: { label: string; value: ConflictFilter }[] = [
  { label: "待处理", value: "open" },
  { label: "已解决", value: "resolved" },
  { label: "全部", value: "all" },
];

export interface ConflictListTabProps {
  projectId: number;
  /**
   * 审核规则快照（页面壳已拉取 `GET /consistency/rules`）。冲突 Tab 用它取
   * `autofix_value_max_chars` 给「统一后的数值」输入框设上限提示 —— 后端在
   * `apply_autofix` 对超长 `new_value` 回 400，与其让用户提交了才知道，不如
   * 输入阶段就拦。老调用方不传时按 undefined 兜底（不设 maxLength，行为不变）。
   */
  rules?: ConsistencyRules | null;
}

/**
 * 「一致性冲突」Tab —— 全文一致性审计的冲突清单与修复工作流。
 *
 * ENHANCE 2026-10-08：从 `ConsistencyPage.tsx` 抽出为独立组件（原 484 行单文件），
 * 使 Tab 级/组件级交互测试可以只挂载这一块，不必牵动页面壳与其它 Tab。
 *
 * 本轮一并修掉的问题：
 * 1. **谎报成功**：`runConsistencyAudit` 返回体新增 `audit_status`，审计内部失败
 *    （此前被路由降级成全 0 且回 status='ok'）时不再弹「审计完成」。
 * 2. **历史冲突混入**：默认只拉未解决冲突，并提供「待处理/已解决/全部」筛选；
 *    后端 `/consistency/conflicts` 现支持 `?resolved=` 过滤。
 * 3. **响应竞态**：`load()` 引入请求序号，慢响应不会覆盖新选择/新筛选的结果。
 * 4. **AI 按钮误启用**：原判定用 `conflicts.length === 0`，而列表在「已解决」
 *    过滤下非空 —— 此时后端只会回「没有未解决的一致性冲突」。现按未解决数判定。
 * 5. **断链**：「涉及章节」的 `section_<id>` 来源改为可点击深链到章节编辑器。
 */
export default function ConflictListTab({ projectId, rules }: ConflictListTabProps) {
  const navigate = useNavigate();

  const [audit, setAudit] = useState<AuditResult | null>(null);
  const [conflicts, setConflicts] = useState<Conflict[]>([]);
  const [summary, setSummary] = useState<AuditSummary | null>(null);
  const [loading, setLoading] = useState(false);
  const [auditing, setAuditing] = useState(false);
  const [filter, setFilter] = useState<ConflictFilter>("open");

  const [suggestionModal, setSuggestionModal] = useState<{
    conflict: Conflict | null; suggestion: string; targetValue: string; loading: boolean;
  }>({ conflict: null, suggestion: "", targetValue: "", loading: false });

  const [fixModal, setFixModal] = useState<{
    conflict: Conflict | null; newValue: string; loading: boolean;
  }>({ conflict: null, newValue: "", loading: false });

  const [aiRepairing, setAiRepairing] = useState(false);
  const [aiReport, setAiReport] = useState<AiRepairReport | null>(null);

  // BUG-FIX 2026-10-13（工作流缺口·关键结果不可见）：autofix 的真实报告会返回
  // `filtered`（被「就近标签词校验」判为其他事实、按设计跳过的取值计数）与
  // `scoped_section_ids` / `scope_fallback_reason`（替换范围是否被限定在冲突
  // 来源章节内）。这些**决定了替换结果**的信息此前只出现在 message 的一句话里
  // 甚至完全不出现，用户看到「改了 3 处」却不知道另有 2 处没改、为什么没改 ——
  // 这正是 2026-10-09 引入两道闸门后最需要向用户解释的事。现独立成卡片常驻。
  const [fixReport, setFixReport] = useState<AutoFixReport | null>(null);

  // BUG-FIX 2026-10-08（响应竞态）：每次 load 自增序号，回来时序号不一致即丢弃。
  // 快速切换项目/筛选或并发「修复后刷新」时，慢响应会覆盖新结果。
  const loadSeqRef = useRef(0);

  const isValidProject = Number.isFinite(projectId) && projectId > 0;

  // autofix 的 new_value 长度上限（后端 `apply_autofix` 对超长值回 400）。
  // 页面壳已把规则快照下发下来，这里给输入框设 maxLength 前置拦截；
  // 老后端/不传 rules 时 undefined，等同不限长（行为不变）。
  const autofixValueMaxChars = rules?.facts?.autofix_value_max_chars;

  // BUG-FIX 2026-10-13：autofix 报告里的 `filtered` / `scoped_section_ids` /
  // `scope_fallback_reason` 是后端**一直在返回、类型里刚补上**的可选字段。
  // 这里归一一次，JSX 里即可按必填数组处理（同时过 TS 收窄）。
  const fixFiltered = fixReport?.filtered ?? [];
  const fixSkipped = fixReport?.skipped ?? [];

  const load = useCallback(async (resolvedFilter: ConflictFilter = filter) => {
    if (!isValidProject) return;
    const seq = ++loadSeqRef.current;
    setLoading(true);
    try {
      const [conflictRes, summaryRes] = await Promise.all([
        getConflicts(projectId, { resolved: resolvedFilter }),
        getAuditSummary(projectId),
      ]);
      if (seq !== loadSeqRef.current) return; // 已有更新的请求，丢弃本次结果
      setConflicts(conflictRes.conflicts || []);
      setSummary(summaryRes.summary || null);
    } catch (e) {
      if (seq !== loadSeqRef.current) return;
      message.error(extractApiError(e));
    } finally {
      if (seq === loadSeqRef.current) setLoading(false);
    }
  }, [projectId, filter, isValidProject]);

  useEffect(() => {
    load(filter);
  }, [load, filter]);

  const handleAudit = async () => {
    setAuditing(true);
    try {
      const result = await runConsistencyAudit(projectId);
      // BUG-FIX 2026-10-08（P1·谎报成功）：审计内部失败时后端回
      // `status='failed'`，此前前端一律 message.success("审计完成")，
      // 用户据此以为全文一致、实际是审计崩了（且冲突数显示为 0）。
      if (result.status !== "ok" || result.audit_status === "failed") {
        message.error(result.message || "一致性审计失败，结果不可信，请重试");
        // 仍然刷新一次：审计日志/摘要可能有变化
        await load(filter);
        return;
      }
setAudit(result);
      // BUG-FIX 2026-10-09（筛选态被覆盖）：审计接口 `runConsistencyAudit`
      // 返回的是**本次新检出**的冲突（后端 `_save_conflicts` 只处理
      // resolved=0 的行），语义上恒等于「未解决」列表。此前这里直接
      // `setConflicts(result.conflicts || [])` 覆盖当前表格，于是当用户停留在
      // 「已解决」页签时点一次「开始审计」，表格会被换成**未解决**的冲突，
      // 而上方 Segmented 仍显示「已解决」—— 视图与数据自相矛盾，
      // 用户会以为系统把已修复的冲突又翻了出来。
      // 修复：审计后按当前筛选态重新拉取，而非用审计返回值直接覆盖。
      // 同时 `setAudit(result)` 保留（它承载 audit_time/conflicts_found 等
      // 本次审计的元信息，与页签无关）。
      //
      // BUG-FIX 2026-10-13（重复请求）：`load()` 内部 `Promise.all` 已同时拉
      // conflicts 与 summary，此处原先在 `await load()` 之后**又**单独调了一次
      // `getAuditSummary(projectId)` —— 每次点「开始审计」多打一次无用请求，
      // 且两次取回的 summary 存在竞态（后到的可能覆盖前者）。删除。
      await load(filter);
      message.success(`审计完成，发现 ${result.conflicts_found} 处冲突`);
    } catch (e) {
      message.error(extractApiError(e));
    } finally {
      setAuditing(false);
    }
  };

  const handleGetSuggestion = async (conflict: Conflict) => {
    setSuggestionModal({ conflict, suggestion: "", targetValue: "", loading: true });
    try {
      const res = await getFixSuggestion(conflict.conflict_id, projectId);
      setSuggestionModal({
        conflict,
        suggestion: res.suggestion || "",
        // BUG-FIX 2026-10-06（P1）：用后端结构化纯目标值，而非整段建议文案。
        // 否则「采纳建议」会把“建议将以下内容统一为：450（已确认…）”当 new_value，
        // 提交 autofix 后正文旧值被替换成这段说明文字。
        targetValue: res.target_value || "",
        loading: false,
      });
    } catch (e) {
      setSuggestionModal({ conflict, suggestion: "", targetValue: "", loading: false });
      message.error(extractApiError(e));
    }
  };

  const handleApplyFix = async () => {
    const { conflict, newValue } = fixModal;
    if (!conflict || !newValue.trim()) {
      message.warning("请输入统一后的数值");
      return;
    }
    setFixModal((s) => ({ ...s, loading: true }));
    try {
      const report = await applyAutoFix(conflict.conflict_id, projectId, newValue.trim());
      setFixModal({ conflict: null, newValue: "", loading: false });
      // BUG-FIX 2026-10-13：把报告存下来在表格上方常驻展示（见 fixReport 注释）。
      setFixReport(report);
      // BUG-FIX 2026-09-14（高）：不能再无条件弹「已应用修复」。
      // 后端原实现只标记冲突已解决、从不改写正文；现在按真实报告区分展示。
      if (report.status === "ok") {
        message.success(report.message || "已应用修复");
      } else {
        message.warning(report.message || "未做任何修改");
      }
      await load(filter);
    } catch (e) {
      setFixModal((s) => ({ ...s, loading: false }));
      message.error(extractApiError(e));
    }
  };

  const handleAiRepair = async (dryRun: boolean) => {
    setAiRepairing(true);
    try {
      const report = await repairConsistencyWithAi(projectId, dryRun);
      setAiReport(report);
      if (report.status === "ok") {
        message.success(report.message || "AI 修复完成");
      } else {
        message.warning(report.message || "AI 未产出可用补丁");
      }
      if (!dryRun) await load(filter);
    } catch (e) {
      message.error(extractApiError(e));
    } finally {
      setAiRepairing(false);
    }
  };

  const openSection = (sectionId: number) => {
    navigate(`/projects/${projectId}/sections?section=${sectionId}`);
  };

  // 未解决冲突数：驱动 AI 修复按钮的可用性。
  // BUG-FIX 2026-10-08：原用 `conflicts.length === 0`，在「已解决」过滤下非空，
  // 按钮会亮着但后端必然回「没有未解决的一致性冲突」——白跑一次 AI。
  const unresolvedCount = filter === "open" ? conflicts.length : (summary?.unresolved_conflicts ?? 0);
  const canAiRepair = unresolvedCount > 0;

  const columns = [
    {
      title: "事实项",
      dataIndex: "fact_name",
      key: "fact_name",
      width: 200,
      render: (v: string, record: Conflict) => {
        // BUG-FIX 2026-10-15（P1·AI 发现被当成「事实项」渲染）：
        // `source_kind === 'ai'` 的冲突是正文生成收尾时 AI 审计的自由文本发现，
        // `fact_name` 是后端截短的 issue 原文（最长 100 字），不是事实名 ——
        // 此前当「事实项」加粗渲染，一长句话杵在表格里，用户以为是系统把这段
        // 话当成了事实项。现区分展示：AI 发现挂紫色标签 + 截断文本（悬浮看全文），
        // 规则冲突保持原样。
        const isAi = record.source_kind === "ai";
        if (!isAi) return <Text strong>{v}</Text>;
        const shown = v.length > 24 ? `${v.slice(0, 24)}…` : v;
        return (
          <Space size={4} wrap>
            <Tag color="purple" style={{ marginInlineEnd: 0 }}>AI 发现</Tag>
            <Text type="secondary" style={{ fontSize: 12 }} title={v}>{shown}</Text>
          </Space>
        );
      },
    },
    {
      title: "严重程度",
      dataIndex: "severity",
      key: "severity",
      width: 100,
      render: (v: string) => {
        const cfg = mapConsistencySeverity(v);
        return <Tag color={cfg.color}>{cfg.label}</Tag>;
      },
    },
    {
      title: "状态",
      dataIndex: "state",
      key: "state",
      width: 90,
      render: (v: string | undefined, record: Conflict) => {
        const resolved = v ? v === "resolved" : record.resolved === 1;
        return resolved
          ? <Tag color="green">已解决</Tag>
          : <Tag color="volcano">待处理</Tag>;
      },
    },
    {
      title: "冲突数值",
      dataIndex: "values",
      key: "values",
      width: 160,
      render: (values: string[], record: Conflict) => {
        // BUG-FIX 2026-10-15：AI 语义发现没有结构化取值（后端恒写 `[]`），
        // 此前渲染成一个空单元格，用户不知道为什么这一行的「冲突数值」是空的。
        if (record.source_kind === "ai") {
          return <Text type="secondary" style={{ fontSize: 12 }}>—（AI 语义发现，无结构化取值）</Text>;
        }
        return (
          <Space size={[4, 4]} wrap>
            {(values || []).map((v, i) => (
              <Tag key={i} color="red">{v}</Tag>
            ))}
          </Space>
        );
      },
    },
    {
      title: "冲突说明",
      dataIndex: "description",
      key: "description",
      ellipsis: true,
      render: (v: string) => <Text type="secondary">{v}</Text>,
    },
    {
      title: "涉及章节",
      dataIndex: "sources",
      key: "sources",
      render: (sources: string[]) => {
        const refs = parseConflictSources(sources);
        const shown = refs.slice(0, SOURCE_PREVIEW);
        return (
          <Space size={[4, 4]} wrap>
            {shown.map((r, i) =>
              r.linkable ? (
                <Tag
                  key={i}
                  color="blue"
                  style={{ maxWidth: 160, cursor: "pointer" }}
                  onClick={() => openSection(r.sectionId as number)}
                >
                  {r.title || `章节 ${r.sectionId}`}
                </Tag>
              ) : (
                <Tag key={i} style={{ maxWidth: 160 }}>{r.raw}</Tag>
              ),
            )}
            {refs.length > SOURCE_PREVIEW && (
              <Tag>+{refs.length - SOURCE_PREVIEW}</Tag>
            )}
          </Space>
        );
      },
    },
    {
      title: "操作",
      key: "action",
      width: 180,
      render: (_: unknown, record: Conflict) => {
        const isResolved = record.state ? record.state === "resolved" : record.resolved === 1;
        if (isResolved) {
          // 已解决的冲突无需（也不应）再提供修复入口
          return <Text type="secondary">已处理</Text>;
        }
        return (
          <Space>
            <Button
              size="small"
              icon={<BulbOutlined />}
              onClick={() => handleGetSuggestion(record)}
            >
              修复建议
            </Button>
            <Button
              size="small"
              type="primary"
              icon={<CheckOutlined />}
              onClick={() => setFixModal({ conflict: record, newValue: "", loading: false })}
              disabled={record.source_kind === "ai"}
              title={
                record.source_kind === "ai"
                  ? "AI 语义发现没有结构化取值（统一不了具体数值），请查看修复建议后人工处理"
                  : undefined
              }
            >
              自动修复
            </Button>
          </Space>
        );
      },
    },
  ];

  if (!isValidProject) {
    return <Alert type="error" showIcon message="项目 ID 无效，无法加载一致性校核" />;
  }

  const emptyHint = auditing
    ? "正在审计..."
    : filter === "resolved"
      ? "暂无已解决的冲突"
      : "暂无冲突数据，点击上方「开始审计」";

  return (
    <>
      <div style={{ marginBottom: 16, display: "flex", justifyContent: "flex-end" }}>
        <Space>
          <Button
            icon={<ThunderboltOutlined />}
            loading={aiRepairing}
            onClick={() => handleAiRepair(true)}
            disabled={!canAiRepair}
            title={canAiRepair ? undefined : "没有未解决的冲突，无需 AI 修复"}
          >
            预览 AI 补丁
          </Button>
          <Button
            icon={<ThunderboltOutlined />}
            loading={aiRepairing}
            onClick={() => handleAiRepair(false)}
            disabled={!canAiRepair}
            title={canAiRepair ? undefined : "没有未解决的冲突，无需 AI 修复"}
          >
            AI 补丁修复
          </Button>
          <Button
            type="primary"
            icon={<ReloadOutlined />}
            loading={auditing}
            onClick={handleAudit}
          >
            {audit ? "重新审计" : "开始审计"}
          </Button>
        </Space>
      </div>

      {fixReport && (
        <Card
          size="small"
          style={{ marginBottom: 16 }}
          title={
            <Space>
              <CheckOutlined /> 自动修复结果
              <Tag color={fixReport.status === "ok" ? "green" : "orange"}>
                {fixReport.status}
              </Tag>
              <Text type="secondary" style={{ fontWeight: 400 }}>
                统一为：{fixReport.unified_value}
              </Text>
            </Space>
          }
          extra={<Button size="small" type="link" onClick={() => setFixReport(null)}>关闭</Button>}
        >
          <Paragraph style={{ marginBottom: 8 }}>{fixReport.message}</Paragraph>
          <Space size={[8, 8]} wrap style={{ marginBottom: 8 }}>
            <Tag>修改章节 {fixReport.changed_sections} 个</Tag>
            <Tag color="green">替换 {fixReport.total_replacements} 处</Tag>
            {!!fixReport.replaced_values?.length && (
              <Tag color="volcano">统一掉旧值 {fixReport.replaced_values.join("、")}</Tag>
            )}
            {fixSkipped.length > 0 && (
              <Tag color="red">未能安全替换 {fixSkipped.length} 章</Tag>
            )}
            {fixFiltered.length > 0 && (
              <Tag color="orange">
                就近校验拦下 {fixFiltered.length} 章
              </Tag>
            )}
          </Space>
          {fixReport.scope_fallback_reason && (
            <Alert
              type="warning"
              showIcon
              style={{ marginBottom: 8 }}
              message={fixReport.scope_fallback_reason}
              description="本次替换未限定在冲突来源章节内，请核对下方明细以免误改无关数字。"
            />
          )}
          {!!fixReport.scoped_section_ids?.length && (
            <Text type="secondary" style={{ fontSize: 12, display: "block", marginBottom: 4 }}>
              替换范围已限定在冲突来源章节：{fixReport.scoped_section_ids.join("、")}
            </Text>
          )}
          {fixSkipped.map((s, i) => (
            <div key={`sk${i}`}>
              <Text type="danger" style={{ fontSize: 12 }}>
                [{s.section_id} {s.title}] {s.reason}
              </Text>
            </div>
          ))}
          {fixFiltered.map((f, i) => (
            <div key={`ft${i}`}>
              <Text type="warning" style={{ fontSize: 12 }}>
                [{f.section_id} {f.title}] {f.reason}
              </Text>
            </div>
          ))}
        </Card>
      )}

      {aiReport && (
        <Card
          size="small"
          style={{ marginBottom: 16 }}
          title={
            <Space>
              <ThunderboltOutlined /> AI 补丁修复{aiReport.dry_run ? "（预览）" : ""}
              <Tag color={aiReport.status === "ok" ? "green" : "orange"}>{aiReport.status}</Tag>
            </Space>
          }
          extra={<Button size="small" type="link" onClick={() => setAiReport(null)}>关闭</Button>}
        >
          <Paragraph style={{ marginBottom: 8 }}>{aiReport.message}</Paragraph>
          <Space size={[8, 8]} wrap style={{ marginBottom: aiReport.rejected.length ? 8 : 0 }}>
            <Tag>补丁 {aiReport.patches.length} 条</Tag>
            <Tag color="green">可应用 {aiReport.applied.length}</Tag>
            {aiReport.rejected.length > 0 && <Tag color="orange">被拒绝 {aiReport.rejected.length}</Tag>}
            <Tag>改动小节 {aiReport.sections.length}</Tag>
            {aiReport.conflict_states && (
              <Tag color="blue">
                冲突已解决 {aiReport.conflict_states.resolved} / 仍待处理 {aiReport.conflict_states.still_open}
              </Tag>
            )}
          </Space>
          {aiReport.rejected.length > 0 && (
            <div style={{ marginBottom: aiReport.unresolved.length ? 8 : 0 }}>
              {aiReport.rejected.slice(0, 8).map((r, i) => (
                <div key={i}>
                  <Text type="warning">
                    [{r.section_id} {r.title}] {r.error}
                  </Text>
                </div>
              ))}
            </div>
          )}
          {aiReport.unresolved.length > 0 && (
            <div>
              <Text type="secondary">AI 判定无法局部修复：</Text>
              {aiReport.unresolved.slice(0, 8).map((u, i) => (
                <div key={i}>
                  <Text type="secondary">
                    [{u.section_id}] {u.reason}
                  </Text>
                </div>
              ))}
            </div>
          )}
          {aiReport.errors.length > 0 && (
            <div>
              {aiReport.errors.slice(0, 5).map((e, i) => (
                <div key={i}>
                  <Text type="danger">{e}</Text>
                </div>
              ))}
            </div>
          )}
        </Card>
      )}

      <AuditSummaryCards summary={summary} />

      <Card
        title={
          <Space>
            <WarningOutlined /> 冲突列表
            {conflicts.length > 0 && <Tag color="red">{conflicts.length} 处</Tag>}
          </Space>
        }
        extra={
          <Segmented
            size="small"
            value={filter}
            options={FILTER_OPTIONS}
            onChange={(v) => setFilter(v as ConflictFilter)}
          />
        }
      >
        {audit && audit.status === "ok" && audit.audit_status !== "failed" && audit.conflicts_found === 0 && (
          <Alert
            type="success"
            showIcon
            icon={<CheckCircleOutlined />}
            style={{ marginBottom: 16 }}
            message={`审计完成：共检查 ${audit.total_sections} 个章节，提取 ${audit.facts_extracted} 个事实，未发现冲突。`}
          />
        )}
        <Table
          rowKey="conflict_id"
          loading={loading}
          dataSource={conflicts}
          columns={columns}
          pagination={{ pageSize: 10, showSizeChanger: true }}
          locale={{ emptyText: <Empty description={emptyHint} /> }}
          size="middle"
        />
      </Card>

      {/* 修复建议弹窗 */}
      <Modal
        title={<Space><BulbOutlined /> 修复建议</Space>}
        open={!!suggestionModal.conflict}
        onCancel={() => setSuggestionModal({ conflict: null, suggestion: "", targetValue: "", loading: false })}
        footer={<Button onClick={() => setSuggestionModal({ conflict: null, suggestion: "", targetValue: "", loading: false })}>关闭</Button>}
      >
        {suggestionModal.conflict && (
          <Space direction="vertical" style={{ width: "100%" }}>
            <div>
              {/* BUG-FIX 2026-10-17（P3·AI 发现被标注为「事实项」）：第六轮修了
                  表格列（AI 发现挂紫色标签 + 截断文本），漏了两个弹窗 —— 这里把
                  AI 发现的 issue 原文（最长 100 字自由文本）标注成「事实项」，
                  用户以为是系统把这段话当成了事实名。与表格同口径区分展示。 */}
              <Text strong>
                {suggestionModal.conflict.source_kind === "ai" ? "AI 语义发现：" : "事实项："}
              </Text>
              <Text>{suggestionModal.conflict.fact_name}</Text>
            </div>
            <div>
              <Text strong>冲突数值：</Text>
              {suggestionModal.conflict.source_kind === "ai" ? (
                // 表格列有占位说明（第六轮），弹窗此前渲染成空 Tag 组，用户
                // 不知道这一行为什么没有数值。
                <Text type="secondary" style={{ fontSize: 12 }}>
                  —（AI 语义发现，无结构化取值）
                </Text>
              ) : (
                <Space size={[4, 4]} wrap>
                  {(suggestionModal.conflict.values || []).map((v, i) => (
                    <Tag key={i} color="red">{v}</Tag>
                  ))}
                </Space>
              )}
            </div>
            <div>
              <Text strong>涉及章节：</Text>
              <Space size={[4, 4]} wrap>
                {(suggestionModal.conflict.sources || []).map((s, i) => {
                  const r = parseConflictSources([s])[0];
                  return r?.linkable ? (
                    <Tag
                      key={i}
                      color="blue"
                      style={{ cursor: "pointer" }}
                      onClick={() => openSection(r.sectionId as number)}
                    >
                      {r.title || `章节 ${r.sectionId}`}
                    </Tag>
                  ) : (
                    <Tag key={i} style={{ maxWidth: 200 }}>{s}</Tag>
                  );
                })}
              </Space>
            </div>
            <Paragraph
              style={{ marginTop: 8, background: "#f5f5f5", padding: 12, borderRadius: 8 }}
            >
              {suggestionModal.loading
                ? "正在生成建议..."
                : suggestionModal.suggestion || "暂无建议，请手动确认统一数值。"}
            </Paragraph>
            <Button
              type="primary"
              disabled={!suggestionModal.targetValue}
              onClick={() => {
                setFixModal({
                  conflict: suggestionModal.conflict,
                  // BUG-FIX 2026-10-06（P1）：采纳的是结构化纯目标值 targetValue，
                  // 而非整段建议文案 suggestion（后者含"建议将以下内容统一为：…"前缀）。
                  newValue: suggestionModal.targetValue || "",
                  loading: false,
                });
                setSuggestionModal({ conflict: null, suggestion: "", targetValue: "", loading: false });
              }}
            >
              采纳建议并修复
            </Button>
          </Space>
        )}
      </Modal>

      {/* 自动修复弹窗 */}
      <Modal
        title={<Space><CheckOutlined /> 自动修复</Space>}
        open={!!fixModal.conflict}
        onCancel={() => setFixModal({ conflict: null, newValue: "", loading: false })}
        onOk={handleApplyFix}
        confirmLoading={fixModal.loading}
        okText="应用修复"
      >
        {fixModal.conflict && (
          <Space direction="vertical" style={{ width: "100%" }}>
            <div>
              {/* 与建议弹窗同口径区分 AI 发现（防御性：当前 UI 路径下 AI 发现
                  到不了本弹窗 —— 表格按钮禁用 + 采纳按钮禁用；但弹窗状态是组件
                  级的，不依赖入口假设）。 */}
              <Text strong>
                {fixModal.conflict.source_kind === "ai" ? "AI 语义发现：" : "事实项："}
              </Text>
              <Text>{fixModal.conflict.fact_name}</Text>
            </div>
            <div>
              <Text strong>冲突数值：</Text>
              {fixModal.conflict.source_kind === "ai" ? (
                <Text type="secondary" style={{ fontSize: 12 }}>
                  —（AI 语义发现，无结构化取值）
                </Text>
              ) : (
                <Space size={[4, 4]} wrap>
                  {(fixModal.conflict.values || []).map((v, i) => (
                    <Tag key={i} color="red">{v}</Tag>
                  ))}
                </Space>
              )}
            </div>
            <div>
              <Text strong>统一后的数值：</Text>
            </div>
            <Input
              value={fixModal.newValue}
              onChange={(e) => setFixModal((s) => ({ ...s, newValue: e.target.value }))}
              placeholder="请输入统一后的数值，将应用到所有涉及章节"
              maxLength={autofixValueMaxChars}
            />
            <Text type="secondary" style={{ fontSize: 12 }}>
              <CloseCircleOutlined /> 注意：修复操作将覆盖所有涉及章节中的该事实表述。
              {typeof autofixValueMaxChars === "number" && (
                <> 统一值最长 {autofixValueMaxChars} 字（与后端 `apply_autofix` 的上限对齐）。</>
              )}
            </Text>
          </Space>
        )}
      </Modal>
    </>
  );
}