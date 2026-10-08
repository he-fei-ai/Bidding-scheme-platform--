import { Card, Col, Row, Statistic, Typography } from "antd";
import type { AuditSummary } from "../../api";

const { Text } = Typography;

export interface AuditSummaryCardsProps {
  summary: AuditSummary | null;
}

/**
 * 一致性校核的统计卡片。
 *
 * BUG-FIX 2026-10-08（统计口径统一）：后端此前 `conflicts_by_severity` 未过滤
 * `resolved`，把**已解决**的历史冲突也计入「严重/中等冲突」卡片，而同一屏的
 * 「未解决冲突」只算未解决的 —— 用户看到「未解决 2 / 严重 1 / 中等 1」，点进
 * 表格却发现那条中等早已修好。现在后端两个数字同口径（都只算未解决），本组件
 * 的卡片与之严格对应，并在标题上标注「未解决」，消除歧义。
 *
 * 「已解决」作为**历史**单独展示，不参与严重度分级。
 *
 * ENHANCE 2026-10-09：补「轻微冲突」卡片 —— 该档此前在后端永不产出，判定标准
 * 收敛后可达（见 `consistency_rules.assess_severity`）。
 */
export default function AuditSummaryCards({ summary }: AuditSummaryCardsProps) {
  if (!summary) return null;

  const unresolved = summary.unresolved_conflicts || 0;
  // 仅统计未解决冲突（后端已按 open 口径返回）
  const bySeverity = summary.conflicts_by_severity || {};
  const high = bySeverity.high || 0;
  const medium = bySeverity.medium || 0;
  // ENHANCE 2026-10-09：`low`（轻微）此前在后端判定里是**永不产出的死档**
  // （`_assess_severity` 只有 high/medium 两条分支），卡片也就无从展示。判定标准
  // 收敛到 `consistency_rules.assess_severity` 后该档可达，这里补上对应卡片，
  // 否则「未解决冲突 N」与三张分级卡片之和会对不上。
  const low = bySeverity.low || 0;
  const resolved = summary.resolved_conflicts || 0;

  return (
    <Row gutter={16} style={{ marginBottom: 16 }}>
      <Col xs={12} md={6}>
        <Card>
          <Statistic
            title="最近审计时间"
            value={(summary.latest_audit?.audit_time as string) || "—"}
            valueStyle={{ fontSize: 18 }}
          />
        </Card>
      </Col>
      <Col xs={12} md={6}>
        <Card>
          <Statistic
            title="未解决冲突"
            value={unresolved}
            valueStyle={{ color: unresolved > 0 ? "#ff4d4f" : "#52c41a" }}
          />
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