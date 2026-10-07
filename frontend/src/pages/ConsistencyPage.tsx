import { useEffect, useState } from "react";
import { useParams, useNavigate } from "react-router-dom";
import {
  Alert, Button, Card, Col, Empty, Modal, Row, Space, Statistic, Table, Tag, Typography, message, Input,
} from "antd";
import {
  ArrowLeftOutlined, AuditOutlined, CheckCircleOutlined, WarningOutlined, CloseCircleOutlined,
  BulbOutlined, ReloadOutlined, CheckOutlined, ThunderboltOutlined,
} from "@ant-design/icons";
import {
  runConsistencyAudit, getConflicts, getFixSuggestion, applyAutoFix, getAuditSummary,
  repairConsistencyWithAi,
} from "../api";
import type { AiRepairReport, AuditResult, Conflict } from "../api";
import { extractApiError, mapConsistencySeverity } from "../utils";

const { Text, Paragraph } = Typography;

// severityConfig 已抽为 utils 纯函数 mapConsistencySeverity（组件级可测化 2026-10-05）

export default function ConsistencyPage() {
  const { id } = useParams<{ id: string }>();
  const projectId = Number(id);
  const navigate = useNavigate();

  const [audit, setAudit] = useState<AuditResult | null>(null);
  const [conflicts, setConflicts] = useState<Conflict[]>([]);
  const [summary, setSummary] = useState<any>(null);
  const [loading, setLoading] = useState(false);
  const [auditing, setAuditing] = useState(false);

  const [suggestionModal, setSuggestionModal] = useState<{
    conflict: Conflict | null; suggestion: string; targetValue: string; loading: boolean;
  }>({ conflict: null, suggestion: "", targetValue: "", loading: false });

  const [fixModal, setFixModal] = useState<{
    conflict: Conflict | null; newValue: string; loading: boolean;
  }>({ conflict: null, newValue: "", loading: false });

  const [aiRepairing, setAiRepairing] = useState(false);
  const [aiReport, setAiReport] = useState<AiRepairReport | null>(null);

  const load = async () => {
    setLoading(true);
    try {
      const [conflictRes, summaryRes] = await Promise.all([
        getConflicts(projectId),
        getAuditSummary(projectId),
      ]);
      setConflicts(conflictRes.conflicts || []);
      setSummary(summaryRes.summary || null);
    } catch (e) {
      message.error(extractApiError(e));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    load();
  }, [projectId]);

  const handleAudit = async () => {
    setAuditing(true);
    try {
      const result = await runConsistencyAudit(projectId);
      setAudit(result);
      setConflicts(result.conflicts || []);
      message.success(`审计完成，发现 ${result.conflicts_found} 处冲突`);
      // 刷新摘要
      try {
        const summaryRes = await getAuditSummary(projectId);
        setSummary(summaryRes.summary || null);
      } catch { /* ignore */ }
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
      load();
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
      if (!dryRun) load();
    } catch (e) {
      message.error(extractApiError(e));
    } finally {
      setAiRepairing(false);
    }
  };

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
      title: "冲突数值",
      dataIndex: "values",
      key: "values",
      width: 180,
      render: (values: string[]) => (
        <Space size={[4, 4]} wrap>
          {values.map((v, i) => (
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
      render: (sources: string[]) => (
        <Space size={[4, 4]} wrap>
          {(sources || []).slice(0, 3).map((s, i) => (
            <Tag key={i} color="blue" style={{ maxWidth: 160 }}>{s}</Tag>
          ))}
          {(sources || []).length > 3 && <Tag>+{(sources || []).length - 3}</Tag>}
        </Space>
      ),
    },
    {
      title: "操作",
      key: "action",
      width: 180,
      render: (_: unknown, record: Conflict) => (
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
      ),
    },
  ];

  return (
    <div style={{ maxWidth: 1200, margin: "0 auto" }}>
      <div style={{ marginBottom: 16, display: "flex", alignItems: "center", justifyContent: "space-between" }}>
        <Space>
          <Button icon={<ArrowLeftOutlined />} onClick={() => navigate(`/projects/${projectId}/sections`)}>
            返回正文
          </Button>
          <Typography.Title level={4} style={{ margin: 0 }}>
            <AuditOutlined /> 全文一致性审计
          </Typography.Title>
        </Space>
        <Space>
          <Button
            icon={<ThunderboltOutlined />}
            loading={aiRepairing}
            onClick={() => handleAiRepair(true)}
            disabled={conflicts.length === 0}
          >
            预览 AI 补丁
          </Button>
          <Button
            icon={<ThunderboltOutlined />}
            loading={aiRepairing}
            onClick={() => handleAiRepair(false)}
            disabled={conflicts.length === 0}
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

      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 16 }}
        message="对全文关键事实（如工期、金额、人员配置、技术参数等）进行交叉一致性审计，发现同一事实在不同章节中表述不一致的冲突。"
        description="两种修复方式：表格里的「自动修复」把冲突取值统一成同一个值（适合「30 天 / 45 天」这类）；「AI 补丁修复」由 AI 读懂语义后产出局部替换补丁（适合「2026 年 5 月竣工」vs「工期 90 天」这类不同措辞的冲突）。补丁只替换 AI 逐字引用的原文片段，定位不到或有多处命中都会被拒绝，不会误改正文。"
      />

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

      {summary && (
        <Row gutter={16} style={{ marginBottom: 16 }}>
          <Col xs={12} md={6}>
            <Card>
              <Statistic title="最近审计时间" value={summary.latest_audit?.audit_time || "—"} />
            </Card>
          </Col>
          <Col xs={12} md={6}>
            <Card>
              <Statistic
                title="未解决冲突"
                value={summary.unresolved_conflicts || 0}
                valueStyle={{ color: (summary.unresolved_conflicts || 0) > 0 ? "#ff4d4f" : "#52c41a" }}
              />
            </Card>
          </Col>
          <Col xs={12} md={6}>
            <Card>
              <Statistic
                title="严重冲突"
                value={(summary.conflicts_by_severity?.high) || 0}
                valueStyle={{ color: (summary.conflicts_by_severity?.high || 0) > 0 ? "#ff4d4f" : "#52c41a" }}
              />
            </Card>
          </Col>
          <Col xs={12} md={6}>
            <Card>
              <Statistic
                title="中等冲突"
                value={(summary.conflicts_by_severity?.medium) || 0}
                valueStyle={{ color: (summary.conflicts_by_severity?.medium || 0) > 0 ? "#faad14" : "#52c41a" }}
              />
            </Card>
          </Col>
        </Row>
      )}

      <Card
        title={
          <Space>
            <WarningOutlined /> 冲突列表
            {conflicts.length > 0 && <Tag color="red">{conflicts.length} 处</Tag>}
          </Space>
        }
      >
        {audit && audit.conflicts_found === 0 && (
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
          locale={{ emptyText: <Empty description={auditing ? "正在审计..." : "暂无冲突数据，点击上方「开始审计」"} /> }}
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
                {suggestionModal.conflict.values.map((v, i) => (
                  <Tag key={i} color="red">{v}</Tag>
                ))}
              </Space>
            </div>
            <div>
              <Text strong>涉及章节：</Text>
              <Space size={[4, 4]} wrap>
                {suggestionModal.conflict.sources.map((s, i) => (
                  <Tag key={i} color="blue">{s}</Tag>
                ))}
              </Space>
            </div>
            <Paragraph
              style={{ marginTop: 8, background: "#f5f5f5", padding: 12, borderRadius: 8 }}
            >
              {suggestionModal.loading ? "正在生成建议..." : suggestionModal.suggestion || "暂无建议，请手动确认统一数值。"}
            </Paragraph>
            <Button
              type="primary"
              disabled={!suggestionModal.targetValue}
              onClick={() => {
                setFixModal({
                  conflict: suggestionModal.conflict,
                  // BUG-FIX 2026-10-06（P1）：采纳的是结构化纯目标值 targetValue，
                  // 而非整段建议文案 suggestion（后者含“建议将以下内容统一为：…”前缀）。
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
                {fixModal.conflict.values.map((v, i) => (
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
    </div>
  );
}
