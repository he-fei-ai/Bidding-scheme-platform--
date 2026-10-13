import { useCallback, useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  Alert, Button, Card, Col, Empty, Progress, Row, Space, Statistic, Table, Tag, Tooltip, Typography, message,
} from "antd";
import { FileSearchOutlined, ReloadOutlined } from "@ant-design/icons";
import { getScoringCompliance } from "../../api";
import type { ScoringComplianceResult, ScoringGap } from "../../api";
import { extractApiError, mapConsistencyScoringStatus } from "../../utils";

const { Text, Paragraph } = Typography;

/** 缺口表只展示非 covered 项；covered 数量在统计卡片里体现 */
const GAP_PAGE_SIZE = 10;

export interface ScoringComplianceTabProps {
  projectId: number;
}

/**
 * 「评分标准达标」Tab —— 评分标准**达标性**对齐（ENHANCE 2026-10-08 新增 UI）。
 *
 * ## 这个 Tab 补的缺口
 *
 * 评分点检查此前只有**导出预检**里的 `uncovered_scoring_points`，且它只判**存在性**：
 * 目录里挂上了这个评分点就算「已覆盖」，完全不看章节正文到底写没写评分标准要的
 * 内容。也就是下面这种情形会被判为「全覆盖」：
 *
 *     评分点 R1「施工组织设计」标准：须包含施工工艺流程图并附 BIM 模型
 *     章节正文：本文介绍施工工艺。            ← 什么都没答
 *
 * 投标文件丢分点恰恰在这里。后端 `GET /consistency/scoring-compliance/{id}`
 * （`services/scoring_compliance.py`，2026-10-06 新增）早已实现四态判定，但
 * **前端 API 客户端已封装、UI 却完全没接**（`getScoringCompliance` 在
 * `api/index.ts` 里定义后无任何调用方）—— 能力存在但用户够不着。本 Tab 补上这一环。
 *
 * ## 为什么用确定性 n-gram 而不是 AI
 *
 * 覆盖率是字符三元组重合度，零 AI 花费、零超时、结果可复现；原始 coverage 数字
 * 始终返回，用户可按更严标准自行解读。
 *
 * BUG-FIX 2026-10-13（P1 · 状态文案硬编码副本）：本文件此前自带一份写死的
 * `STATUS_META`，与后端 `consistency_rules.SCORING_STATUS_META` 各写一份。
 * `ConsistencyPage` 虽然拉了 `GET /consistency/rules`，却只 apply 了
 * `severity.meta`，评分状态的这份副本等于从未与后端对齐过 —— 后端改文案或新增
 * 一档状态，本 Tab 完全无感知。现改走 `utils.mapConsistencyScoringStatus`
 * （静态兜底 + 运行时对齐，与严重度同一套机制）。
 */
export default function ScoringComplianceTab({ projectId }: ScoringComplianceTabProps) {
  const navigate = useNavigate();

  const [report, setReport] = useState<ScoringComplianceResult | null>(null);
  const [loading, setLoading] = useState(false);
  const seqRef = useRef(0);

  const isValidProject = Number.isFinite(projectId) && projectId > 0;

  const runAudit = useCallback(async () => {
    if (!isValidProject) return;
    const seq = ++seqRef.current;
    setLoading(true);
    try {
      // persist=false：切 Tab 反复查看不应往审计历史里灌记录
      const res = await getScoringCompliance(projectId, { persist: false });
      if (seq !== seqRef.current) return;
      setReport(res);
    } catch (e) {
      if (seq !== seqRef.current) return;
      message.error(extractApiError(e));
    } finally {
      if (seq === seqRef.current) setLoading(false);
    }
  }, [projectId, isValidProject]);

  // 进入 Tab 即拉一次；项目切换时重拉
  useEffect(() => {
    runAudit();
  }, [runAudit]);

  const openSection = (sectionId: number) => {
    navigate(`/projects/${projectId}/sections?section=${sectionId}`);
  };

  const columns = [
    {
      title: "评分项",
      dataIndex: "name",
      key: "name",
      width: 200,
      render: (v: string, record: ScoringGap) => (
        <Space direction="vertical" size={0}>
          <Text strong>{v}</Text>
          {record.weight && <Text type="secondary" style={{ fontSize: 12 }}>权重 {record.weight}</Text>}
        </Space>
      ),
    },
    {
      title: "达标状态",
      dataIndex: "status",
      key: "status",
      width: 130,
      render: (v: string) => {
        const meta = mapConsistencyScoringStatus(v);
        return <Tag color={meta.color}>{meta.label}</Tag>;
      },
    },
    {
      title: "覆盖率",
      dataIndex: "coverage",
      key: "coverage",
      width: 150,
      render: (v: number) => (
        <Tooltip title="评分标准用词在该评分项已挂载章节正文中的字符三元组重合度（确定性计算，非 AI 判断）">
          <Progress percent={Math.round((v || 0) * 100)} size="small" />
        </Tooltip>
      ),
    },
    {
      title: "评分标准",
      dataIndex: "standard",
      key: "standard",
      ellipsis: true,
      render: (v: string) => <Text type="secondary">{v || "—"}</Text>,
    },
    {
      title: "承载章节",
      key: "sections",
      render: (_: unknown, record: ScoringGap) => {
        const ids = record.section_ids || [];
        if (!ids.length) return <Text type="secondary">未挂载</Text>;
        return (
          <Space size={[4, 4]} wrap>
            {ids.map((sid, i) => (
              <Tag
                key={i}
                color="blue"
                style={{ cursor: "pointer" }}
                onClick={() => openSection(sid)}
              >
                {record.section_titles?.[i] || `章节 ${sid}`}
              </Tag>
            ))}
          </Space>
        );
      },
    },
    {
      title: "判定说明",
      dataIndex: "detail",
      key: "detail",
      ellipsis: true,
      render: (v: string) => <Text type="secondary">{v}</Text>,
    },
  ];

  if (!isValidProject) {
    return <Alert type="error" showIcon message="项目 ID 无效，无法加载评分标准达标对齐" />;
  }

  const total = report?.total_points ?? 0;
  const gapCount = report?.gaps?.length ?? 0;

  return (
    <>
      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 16 }}
        message="评分标准「达标性」对齐：检查每个评分项挂载到的章节正文里，是否真的答到了评分标准要求的内容。"
        description="与导出预检的评分点检查分工不同——导出预检只判「有没有挂载章节」（存在性），本 Tab 判「挂载的那一章到底写没写」（达标性）。判定为确定性的字符三元组重合度，不调用 AI、可复现。"
      />

      <div style={{ marginBottom: 16, display: "flex", justifyContent: "flex-end" }}>
        <Button
          type="primary"
          icon={<ReloadOutlined />}
          loading={loading}
          onClick={runAudit}
        >
          重新检测
        </Button>
      </div>

      {report && (
        <Row gutter={16} style={{ marginBottom: 16 }}>
          <Col xs={12} md={6}>
            <Card><Statistic title="评分项总数" value={total} /></Card>
          </Col>
          <Col xs={12} md={6}>
            <Card>
              <Statistic
                title="未挂载章节"
                value={report.unmapped}
                valueStyle={{ color: report.unmapped > 0 ? "#ff4d4f" : "#52c41a" }}
              />
            </Card>
          </Col>
          <Col xs={12} md={6}>
            <Card>
              <Statistic
                title="疑似未答到点"
                value={report.weak}
                valueStyle={{ color: report.weak > 0 ? "#faad14" : "#52c41a" }}
              />
            </Card>
          </Col>
          <Col xs={12} md={6}>
            <Card>
              <Statistic
                title="已达标"
                value={report.covered}
                valueStyle={{ color: report.covered === total ? "#52c41a" : undefined }}
              />
            </Card>
          </Col>
        </Row>
      )}

      {report?.warnings?.map((w, i) => (
        <Alert key={i} type="warning" showIcon style={{ marginBottom: 8 }} message={w} />
      ))}

      <Card
        title={
          <Space>
            <FileSearchOutlined /> 评分标准达标情况
            {gapCount > 0 && <Tag color="red">{gapCount} 项未达标</Tag>}
          </Space>
        }
      >
        {!report && !loading && (
          <Empty description="点击「重新检测」开始评分标准达标对齐" />
        )}
        {report && total === 0 && (
          <Empty description="该项目尚未解析出评分点，无法做达标对齐" />
        )}
        {report && total > 0 && (
          <>
            <Table
              rowKey="scoring_point_id"
              loading={loading}
              dataSource={report.gaps || []}
              columns={columns}
              pagination={{ pageSize: GAP_PAGE_SIZE, showSizeChanger: true }}
              locale={{
                emptyText: (
                  <Empty description={`全部 ${report.covered} 个评分项均已达标`} />
                ),
              }}
              size="middle"
            />
            <Paragraph type="secondary" style={{ marginTop: 12, marginBottom: 0, fontSize: 12 }}>
              仅展示未达标项（共 {report.covered}/{total} 项已达标）。
              {report.audit_time ? ` 检测时间：${report.audit_time}` : ""}
            </Paragraph>
          </>
        )}
      </Card>
    </>
  );
}