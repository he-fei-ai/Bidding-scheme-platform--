import { useEffect, useState } from "react";
import {
  Card, Row, Col, Statistic, Table, Alert, Spin, message, Button, Typography,
  Progress, Divider, Tag,
} from "antd";
import {
  ReloadOutlined, WarningOutlined, ApiOutlined, DatabaseOutlined,
  SyncOutlined, ExperimentOutlined,
} from "@ant-design/icons";
import {
  fetchPromptMetrics, fetchPromptAlerts, syncHardcodePrompts,
  fetchMetricsHistory, fetchMetricsByCategory,
  type PromptMetrics, type PromptAlert, type CategoryStats,
} from "../api";

const { Title, Text } = Typography;

interface MetricsRow {
  key: string;
  name: string;
  calls: number;
  dbHits: number;
  fallbackHits: number;
  dbHitRatio: string;
  totalTokens: number;
}

export default function PromptMetricsPanel() {
  const [loading, setLoading] = useState(true);
  const [syncing, setSyncing] = useState(false);
  const [checking, setChecking] = useState(false);
  const [summary, setSummary] = useState({ total_prompts: 0, total_calls: 0, total_estimated_tokens: 0 });
  const [topPrompts, setTopPrompts] = useState<MetricsRow[]>([]);
  const [alerts, setAlerts] = useState<PromptAlert[]>([]);
  const [history, setHistory] = useState<Array<{ timestamp: number; total_tokens: number; total_calls: number }>>([]);
  const [dbHitRatio, setDbHitRatio] = useState(0);
  // 后端 /prompts/metrics/categories 返回 CategoryStats[]（含 prompt_count 字段）
  const [categoryStats, setCategoryStats] = useState<CategoryStats[]>([]);

  const loadData = async () => {
    setLoading(true);
    try {
      const [metricsRes, alertsRes, historyRes, categoryRes] = await Promise.all([
        fetchPromptMetrics(),
        fetchPromptAlerts(),
        fetchMetricsHistory(24),
        fetchMetricsByCategory(),
      ]);

      setSummary(metricsRes.summary);
      setAlerts(alertsRes.alerts);
      setHistory(historyRes.history);
      setDbHitRatio(historyRes.summary.db_hit_ratio);
      setCategoryStats(categoryRes.categories);

      // 转换 Top10 数据
      const rows: MetricsRow[] = Object.entries(metricsRes.metrics)
        .map(([key, data]) => ({
          key,
          name: key,
          calls: data.calls,
          dbHits: data.db_hits,
          fallbackHits: data.fallback_hits,
          dbHitRatio: data.calls > 0 ? `${((data.db_hits / data.calls) * 100).toFixed(1)}%` : "-",
          totalTokens: data.total_tokens,
        }))
        .sort((a, b) => b.calls - a.calls)
        .slice(0, 10);

      setTopPrompts(rows);
    } catch (e) {
      message.error("加载监控数据失败");
      console.error(e);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadData();
    // 每30秒自动刷新
    const timer = setInterval(loadData, 30000);
    return () => clearInterval(timer);
  }, []);

  const handleSync = async () => {
    setSyncing(true);
    try {
      const res = await syncHardcodePrompts(false);
      message.success(res.message);
      loadData();
    } catch (e) {
      message.error("同步失败");
      console.error(e);
    } finally {
      setSyncing(false);
    }
  };

  const handleCheckAlerts = async () => {
    setChecking(true);
    try {
      const res = await fetchPromptAlerts();
      setAlerts(res.alerts);
      if (res.alert_count > 0) {
        message.warning(`发现 ${res.alert_count} 条告警`);
      } else {
        message.success("检查完成，无告警");
      }
    } catch (e) {
      message.error("检查失败");
      console.error(e);
    } finally {
      setChecking(false);
    }
  };

  const columns = [
    {
      title: "提示词名称",
      dataIndex: "name",
      key: "name",
      ellipsis: true,
    },
    {
      title: "调用次数",
      dataIndex: "calls",
      key: "calls",
      sorter: (a: MetricsRow, b: MetricsRow) => a.calls - b.calls,
    },
    {
      title: "DB命中率",
      dataIndex: "dbHitRatio",
      key: "dbHitRatio",
    },
    {
      title: "回退次数",
      dataIndex: "fallbackHits",
      key: "fallbackHits",
    },
    {
      title: "估算Token",
      dataIndex: "totalTokens",
      key: "totalTokens",
      sorter: (a: MetricsRow, b: MetricsRow) => a.totalTokens - b.totalTokens,
    },
  ];

  // 分类饼图用的颜色
  const categoryColors = [
    "#1890ff", "#52c41a", "#faad14", "#f5222d", "#722ed1",
    "#13c2c2", "#eb2f96", "#fa8c16", "#a0d911", "#2f54eb",
  ];

  if (loading) {
    return (
      <div style={{ textAlign: "center", padding: "100px" }}>
        <Spin size="large" />
      </div>
    );
  }

  return (
    <div style={{ padding: "24px" }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 24 }}>
        <Title level={4} style={{ margin: 0 }}>提示词监控面板</Title>
        <div>
          <Button
            icon={<ReloadOutlined />}
            onClick={loadData}
            style={{ marginRight: 8 }}
          >
            刷新
          </Button>
          <Button
            icon={<ExperimentOutlined spin={checking} />}
            onClick={handleCheckAlerts}
            style={{ marginRight: 8 }}
          >
            立即检查告警
          </Button>
          <Button
            type="primary"
            icon={<DatabaseOutlined />}
            loading={syncing}
            onClick={handleSync}
          >
            同步硬编码到DB
          </Button>
        </div>
      </div>

      {/* 告警列表 */}
      {alerts.length > 0 && (
        <Alert
          message={`发现 ${alerts.length} 条告警`}
          type="warning"
          showIcon
          icon={<WarningOutlined />}
          style={{ marginBottom: 16 }}
          description={
            <ul style={{ margin: 0, paddingLeft: 20 }}>
              {alerts.map((alert, idx) => (
                <li key={idx}>{alert.message}</li>
              ))}
            </ul>
          }
        />
      )}

      {/* 统计卡片 */}
      <Row gutter={16} style={{ marginBottom: 24 }}>
        <Col span={6}>
          <Card>
            <Statistic
              title="总调用次数"
              value={summary.total_calls}
              prefix={<ApiOutlined />}
            />
          </Card>
        </Col>
        <Col span={6}>
          <Card>
            <Statistic
              title="已监控提示词"
              value={summary.total_prompts}
              prefix={<DatabaseOutlined />}
            />
          </Card>
        </Col>
        <Col span={6}>
          <Card>
            <Statistic
              title="估算Token消耗"
              value={summary.total_estimated_tokens}
              precision={0}
            />
          </Card>
        </Col>
        <Col span={6}>
          <Card>
            <Statistic
              title="告警数量"
              value={alerts.length}
              valueStyle={{ color: alerts.length > 0 ? "#cf1322" : "#3f8600" }}
            />
          </Card>
        </Col>
      </Row>

      <Row gutter={16} style={{ marginBottom: 24 }}>
        {/* DB命中率仪表盘 */}
        <Col span={8}>
          <Card title="DB命中率（24h）">
            <div style={{ textAlign: "center", padding: "20px 0" }}>
              <Progress
                type="dashboard"
                percent={dbHitRatio * 100}
                size={180}
                strokeColor={dbHitRatio >= 0.8 ? "#52c41a" : "#faad14"}
                format={(percent) => `${percent?.toFixed(1)}%`}
              />
              <div style={{ marginTop: 16 }}>
                <Text type="secondary">
                  {dbHitRatio >= 0.8 ? "状态良好" : "低于阈值80%，建议检查DB同步"}
                </Text>
              </div>
            </div>
          </Card>
        </Col>

        {/* 调用量分类分布 */}
        <Col span={16}>
          <Card title="调用量分类分布">
            {categoryStats.length === 0 ? (
              <Text type="secondary">暂无数据</Text>
            ) : (
              <div>
                {categoryStats.map((cat, idx) => {
                  const totalCalls = categoryStats.reduce((sum, c) => sum + c.calls, 0);
                  const percent = totalCalls > 0 ? (cat.calls / totalCalls) * 100 : 0;
                  const color = categoryColors[idx % categoryColors.length];
                  return (
                    <div key={cat.category} style={{ marginBottom: 12 }}>
                      <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 4 }}>
                        <span>
                          <Tag color={color}>{cat.category}</Tag>
                          {cat.prompt_count} 个提示词
                        </span>
                        <span>
                          {cat.calls} 次 · {percent.toFixed(1)}%
                        </span>
                      </div>
                      <Progress
                        percent={percent}
                        showInfo={false}
                        strokeColor={color}
                        size="small"
                      />
                    </div>
                  );
                })}
              </div>
            )}
          </Card>
        </Col>
      </Row>

      {/* Token 消耗趋势（简化版：最近10个数据点） */}
      <Card title="Token 消耗趋势（最近24小时）" style={{ marginBottom: 24 }}>
        {history.length === 0 ? (
          <Text type="secondary">暂无历史数据，系统将每5分钟自动记录一次快照</Text>
        ) : (
          <div>
            <div style={{ display: "flex", alignItems: "flex-end", height: 100, gap: 4 }}>
              {history.slice(-20).map((point, idx) => {
                const maxTokens = Math.max(...history.map(h => h.total_tokens), 1);
                const height = (point.total_tokens / maxTokens) * 100;
                return (
                  <div
                    key={idx}
                    style={{
                      flex: 1,
                      height: `${height}%`,
                      backgroundColor: "#1890ff",
                      borderRadius: "4px 4px 0 0",
                      minHeight: 4,
                    }}
                    title={`${new Date(point.timestamp * 1000).toLocaleTimeString()}: ${point.total_tokens} tokens`}
                  />
                );
              })}
            </div>
            <div style={{ marginTop: 8, display: "flex", justifyContent: "space-between" }}>
              <Text type="secondary" style={{ fontSize: 12 }}>最早</Text>
              <Text type="secondary" style={{ fontSize: 12 }}>最近</Text>
            </div>
          </div>
        )}
      </Card>

      {/* Top10 调用次数 */}
      <Card title="调用次数 Top 10" style={{ marginBottom: 24 }}>
        <Table
          dataSource={topPrompts}
          columns={columns}
          pagination={false}
          size="small"
        />
      </Card>

      <Text type="secondary">
        数据每30秒自动刷新 · 点击「立即检查告警」手动触发检测 · Token历史快照每5分钟自动记录
      </Text>
    </div>
  );
}
