import { useCallback, useEffect, useRef, useState } from "react";
import {
  Alert, Button, Card, Empty, Progress, Space, Table, Tag, Typography, message,
} from "antd";
import { ReloadOutlined, SafetyOutlined } from "@ant-design/icons";
import { getRejectionCoverage } from "../../api";
import type { RejectionCoverageItem } from "../../api";
import { extractApiError } from "../../utils";

const { Text, Paragraph } = Typography;

export interface RejectionCoverageTabProps {
  projectId: number;
}

/**
 * 「废标条款应答覆盖率」Tab —— 统计项目正文对招标废标条款的应答覆盖率。
 *
 * 后端机制：
 *   扫描已有的大数据废标检查结果，判断每个废标条款是否在正文中有足够
 *   字数的实质性应答。返回 covered / weak / empty 三种状态。
 *
 * 端点性质：**确定性**（不调 AI、不查网络），只查已有废标任务结果。
 * 如果项目从未跑过废标检查，返回 status='no_checks'（不是错误）。
 */
export default function RejectionCoverageTab({ projectId }: RejectionCoverageTabProps) {
  const [loading, setLoading] = useState(false);
  const [items, setItems] = useState<RejectionCoverageItem[]>([]);
  const [summary, setSummary] = useState<{
    status: string;
    scannedClauses: number;
    fullChars: number;
    threshold: number;
    weakCount: number;
    emptyCount: number;
  } | null>(null);

  // 响应竞态保护
  const loadSeqRef = useRef(0);

  const isValidProject = Number.isFinite(projectId) && projectId > 0;

  const load = useCallback(async () => {
    if (!isValidProject) return;
    const seq = ++loadSeqRef.current;
    setLoading(true);
    try {
      const res = await getRejectionCoverage(projectId);
      if (seq !== loadSeqRef.current) return;
      setItems(res.items || []);
      setSummary({
        status: res.status,
        scannedClauses: res.scanned_clauses || 0,
        fullChars: res.full_chars || 0,
        threshold: res.threshold || 0,
        weakCount: res.weak_count || 0,
        emptyCount: res.empty_count || 0,
      });
    } catch (e) {
      if (seq === loadSeqRef.current) {
        message.error(extractApiError(e));
      }
    } finally {
      if (seq === loadSeqRef.current) setLoading(false);
    }
  }, [projectId, isValidProject]);

  useEffect(() => {
    load();
  }, [load]);

  const statusColor: Record<string, string> = {
    covered: "green",
    weak: "orange",
    empty: "red",
  };

  const statusLabel: Record<string, string> = {
    covered: "已覆盖",
    weak: "覆盖较弱",
    empty: "未应答",
  };

  const columns = [
    {
      title: "废标条款",
      dataIndex: "name",
      key: "name",
      width: 200,
      render: (v: string) => <Text strong>{v}</Text>,
    },
    {
      title: "废标原文",
      dataIndex: "text",
      key: "text",
      ellipsis: true,
      render: (v: string) => <Text type="secondary">{v}</Text>,
    },
    {
      title: "应答状态",
      dataIndex: "status",
      key: "status",
      width: 110,
      render: (v: string) => (
        <Tag color={statusColor[v] || "default"}>
          {statusLabel[v] || v}
        </Tag>
      ),
    },
    {
      title: "应答强度",
      dataIndex: "coverage",
      key: "coverage",
      width: 140,
      render: (v: number) => (
        <Progress
          percent={Math.round(v * 100)}
          size="small"
          strokeColor={
            v >= 0.8 ? "#52c41a" : v >= 0.4 ? "#faad14" : "#ff4d4f"
          }
        />
      ),
    },
    {
      title: "正文章节命中",
      dataIndex: "section_hits",
      key: "section_hits",
      render: (hits: string[]) => (
        <Space size={[4, 4]} wrap>
          {(hits || []).length > 0 ? (
            hits.map((h, i) => <Tag key={i} color="blue">{h}</Tag>)
          ) : (
            <Text type="secondary">—</Text>
          )}
        </Space>
      ),
    },
  ];

  if (!isValidProject) {
    return <Alert type="error" showIcon message="项目 ID 无效" />;
  }

  const coveredCount = summary
    ? summary.scannedClauses - summary.weakCount - summary.emptyCount
    : 0;

  return (
    <Space direction="vertical" style={{ width: "100%" }} size="middle">
      <Alert
        type="info"
        showIcon
        icon={<SafetyOutlined />}
        message="废标条款应答覆盖率"
        description={
          <>
            统计项目正文对招标废标条款的应答覆盖率：每个条款返回「已覆盖 / 覆盖较弱 / 未应答」三档状态。低于阈值（默认 {summary?.threshold ?? 200} 字）的正文命中视为覆盖较弱。此端点
            <Text strong>确定性</Text>（不调 AI），仅依赖已有的大数据废标检查结果。
          </>
        }
      />

      {/* 还没跑过废标检查 —— 优雅提示 */}
      {summary?.status === "no_checks" && (
        <Alert
          type="warning"
          showIcon
          icon={<SafetyOutlined />}
          message="项目尚未运行过废标检查"
          description={
            <Paragraph style={{ marginBottom: 0 }}>
              废标覆盖率依赖已有的大数据废标检查结果。请先在「废标检查」模块运行一次完整检查，然后回到此页查看应答覆盖率。
            </Paragraph>
          }
        />
      )}

      <Card
        title={<Space><SafetyOutlined />覆盖率统计</Space>}
        extra={
          <Button icon={<ReloadOutlined />} loading={loading} onClick={load}>
            刷新
          </Button>
        }
      >
        {summary && summary.scannedClauses > 0 ? (
          <>
            <Space size={[12, 12]} wrap style={{ marginBottom: 16 }}>
              <Tag>废标条款总数 {summary.scannedClauses}</Tag>
              <Tag color="green">已覆盖 {coveredCount}</Tag>
              <Tag color="orange">覆盖较弱 {summary.weakCount}</Tag>
              <Tag color="red">未应答 {summary.emptyCount}</Tag>
              <Tag>应答正文总字数 {summary.fullChars}</Tag>
              <Tag>单条阈值 {summary.threshold} 字</Tag>
            </Space>

            {/* 总览进度 */}
            <Space style={{ marginBottom: 12 }}>
              <Text>整体覆盖率：</Text>
              <Progress
                percent={Math.round(
                  (coveredCount / summary.scannedClauses) * 100,
                )}
                strokeColor={
                  coveredCount / summary.scannedClauses >= 0.8
                    ? "#52c41a"
                    : coveredCount / summary.scannedClauses >= 0.5
                    ? "#faad14"
                    : "#ff4d4f"
                }
                style={{ minWidth: 300 }}
              />
            </Space>
          </>
        ) : summary?.status !== "no_checks" ? (
          <Empty description={loading ? "正在加载..." : "暂无废标数据"} />
        ) : null}
      </Card>

      <Card title="废标条款明细" size="small">
        <Table
          rowKey="clause_id"
          loading={loading}
          dataSource={items}
          columns={columns}
          size="middle"
          pagination={{ pageSize: 10, showSizeChanger: true }}
          locale={{
            emptyText:
              loading ? (
                "正在加载..."
              ) : summary?.status === "no_checks" ? (
                <Empty description="请先在废标检查模块运行一次完整检查" />
              ) : (
                <Empty description="暂无废标条款" />
              ),
          }}
        />
      </Card>
    </Space>
  );
}
