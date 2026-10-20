import { Card, Col, Row, Statistic, Tag, Typography } from "antd";
import type { AuditSummary } from "../../api";

const { Text } = Typography;

export interface AuditSummaryCardsProps {
  summary: AuditSummary | null;
}

/**
 * 一致性校核的统计卡片。
 *
 * BUG-FIX 2026-10-15（P1·AI 发现被混进分级卡片，数字被虚高）：
 * `consistency_conflicts` 同时承载确定性规则冲突与 AI 语义审计发现，而 AI 发现
 * 恒为 medium 严重度（详见 `Conflict.source_kind` 的说明）。此前后端把两类混在
 * `conflicts_by_severity` 里下发，「中等冲突」卡片被 AI 发现一并顶高。
 * 现在：
 *   - 分级卡片**优先**用 `conflicts_by_severity_rule`（只算规则冲突）；
 *     老后端不返回该字段时回退 `conflicts_by_severity`，行为与改造前一致；
 *   - 「未解决冲突」卡片单独标出其中 AI 发现的条数，用户能看出「有多少需要
 *     人工核对、有多少可自动修复」；
 *   - 「最近审计时间」附来源标签（rule=开始审计 / ai=正文生成收尾审计）。
 */
export default function AuditSummaryCards({ summary }: AuditSummaryCardsProps) {
  if (!summary) return null;

  const unresolved = summary.unresolved_conflicts || 0;
  const aiFindings = summary.unresolved_ai_findings ?? 0;
  // 老后端无按来源拆分的字段时，回退老口径（混排）以保持行为不变
  const bySeverity = summary.conflicts_by_severity_rule
    ?? summary.conflicts_by_severity ?? {};
  const high = bySeverity.high || 0;
  const medium = bySeverity.medium || 0;
  // ENHANCE 2026-10-09：`low`（轻微）此前在后端判定里是**永不产出的死档**
  // （`_assess_severity` 只有 high/medium 两条分支），卡片也就无从展示。判定标准
  // 收敛到 `consistency_rules.assess_severity` 后该档可达，这里补上对应卡片，
  // 否则「未解决冲突 N」与三张分级卡片之和会对不上。
  const low = bySeverity.low || 0;
  const resolved = summary.resolved_conflicts || 0;

  const auditSource = summary.audit_source;
  const auditSourceLabel =
    auditSource === "ai"
      ? { text: "AI 审计", color: "purple" }
      : auditSource === "rule"
        ? { text: "规则审计", color: "blue" }
        : null;

  return (
    <Row gutter={16} style={{ marginBottom: 16 }}>
      <Col xs={12} md={6}>
        <Card>
          <Statistic
            title="最近审计时间"
            value={(summary.latest_audit?.audit_time as string) || "—"}
            valueStyle={{ fontSize: 18 }}
          />
          {/*
            BUG-FIX 2026-10-17（P3·来源标签误导）：`audit_source` 在后端无任何
            审计记录时默认回落 'rule'（`consistency.py::get_audit_summary`），
            此前这里照常渲染「规则审计」标签 —— 时间显示「—」却说来源是规则
            审计，用户以为跑过一次审计且结论来自规则路径。仅在确有审计记录时
            展示来源标签。
          */}
          {auditSourceLabel && summary.latest_audit && (
            <Tag color={auditSourceLabel.color} style={{ marginTop: 4 }}>
              {auditSourceLabel.text}
            </Tag>
          )}
        </Card>
      </Col>
      <Col xs={12} md={6}>
        <Card>
          <Statistic
            title="未解决冲突"
            value={unresolved}
            valueStyle={{ color: unresolved > 0 ? "#ff4d4f" : "#52c41a" }}
          />
          {aiFindings > 0 && (
            <Text type="secondary" style={{ fontSize: 12 }}>
              其中 {aiFindings} 条为 AI 语义发现（需人工核对）
            </Text>
          )}
        </Card>
      </Col>
      <Col xs={8} md={8}>
        <Card>
          <Statistic
            title="严重冲突（未解决）"
            value={high}
            valueStyle={{ color: high > 0 ? "#ff4d4f" : "#52c41a" }}
          />
        </Card>
      </Col>
      <Col xs={8} md={8}>
        <Card>
          <Statistic
            title="中等冲突（未解决）"
            value={medium}
            valueStyle={{ color: medium > 0 ? "#faad14" : "#52c41a" }}
          />
        </Card>
      </Col>
      <Col xs={8} md={8}>
        <Card>
          <Statistic
            title="轻微冲突（未解决）"
            value={low}
            valueStyle={{ color: low > 0 ? "#1677ff" : "#52c41a" }}
          />
          {resolved > 0 && (
            <Text type="secondary" style={{ fontSize: 12 }}>
              另有 {resolved} 处已解决
            </Text>
          )}
        </Card>
      </Col>
    </Row>
  );
}