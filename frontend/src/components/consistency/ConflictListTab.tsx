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
import type { AiRepairReport, AuditResult, AuditSummary, Conflict, ConflictFilter } from "../../api";
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
export default function ConflictListTab({ projectId }: ConflictListTabProps) {
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

  // BUG-FIX 2026-10-08（响应竞态）：每次 load 自增序号，回来时序号不一致即丢弃。
  // 快速切换项目/筛选或并发「修复后刷新」时，慢响应会覆盖新结果。
  const loadSeqRef = useRef(0);

  const isValidProject = Number.isFinite(projectId) && projectId > 0;

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
      setConflicts(result.conflicts || []);
      message.success(`审计完成，发现 ${result.conflicts_found} 处冲突`);
      try {
        const summaryRes = await getAuditSummary(projectId);
        setSummary(summaryRes.summary || null);
      } catch { /* 摘要刷新失败不阻断主流程 */ }
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
      width: 180,
      render: (v: string) => <Text strong>{v}</Text>,
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
      width: 180,
      render: (values: string[]) => (
        <Space size={[4, 4]} wrap>
          {(values || []).map((v, i) => (
            <Tag key={i} color="red">{v}</Tag>
          ))}
        </Space>
      ),
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
              <Text strong>事实项：</Text>
              <Text>{suggestionModal.conflict.fact_name}</Text>
            </div>
            <div>
              <Text strong>冲突数值：</Text>
              <Space size={[4, 4]} wrap>
                {(suggestionModal.conflict.values || []).map((v, i) => (
                  <Tag key={i} color="red">{v}</Tag>
                ))}
              </Space>
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
              <Text strong>事实项：</Text>
              <Text>{fixModal.conflict.fact_name}</Text>
            </div>
            <div>
              <Text strong>冲突数值：</Text>
              <Space size={[4, 4]} wrap>
                {(fixModal.conflict.values || []).map((v, i) => (
                  <Tag key={i} color="red">{v}</Tag>
                ))}
              </Space>
            </div>
            <div>
              <Text strong>统一后的数值：</Text>
            </div>
            <Input
              value={fixModal.newValue}
              onChange={(e) => setFixModal((s) => ({ ...s, newValue: e.target.value }))}
              placeholder="请输入统一后的数值，将应用到所有涉及章节"
            />
            <Text type="secondary" style={{ fontSize: 12 }}>
              <CloseCircleOutlined /> 注意：修复操作将覆盖所有涉及章节中的该事实表述。
            </Text>
          </Space>
        )}
      </Modal>
    </>
  );
}