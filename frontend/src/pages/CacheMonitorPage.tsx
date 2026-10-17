import { useEffect, useState, useCallback } from "react";
import {
  Card, Typography, Space, Tag, Button, Alert, Progress, Statistic, Row, Col,
  Table, Tabs, message, Tooltip, Switch, Descriptions,
} from "antd";
import {
  CheckCircleOutlined, CloseCircleOutlined,
  ReloadOutlined, DatabaseOutlined, CloudServerOutlined, FileTextOutlined,
  PieChartOutlined, HddOutlined, RiseOutlined, FallOutlined,
  ClockCircleOutlined, WarningOutlined, ThunderboltOutlined,
  BarChartOutlined, AlertOutlined, MergeCellsOutlined,
} from "@ant-design/icons";
import {
  getCacheStats, clearCache, checkCacheHealth, getCacheInfo,
  getMonitorAiStats, clearMonitorAlerts, configureMonitor, getMonitorHealth,
  getMonitorCacheStats, getMonitorConcurrencyStats,
  getRepairStats, flushRepairStats, getRepairStatsHistory, cleanupRepairStats,
} from "../api";

const { Title, Text, Paragraph } = Typography;

interface CacheInstance {
  name: string;
  type: string;
  hits: number;
  misses: number;
  hit_rate: number | string;
  size?: number;
  max_size?: number;
  ttl_seconds?: number;
  file_count?: number;
  total_size_mb?: number;
}

interface RedisServer {
  memory_used_bytes?: number;
  memory_used_human?: string;
  connected_clients?: number;
  uptime_seconds?: number;
  evicted_keys?: number;
  keyspace_hits?: number;
  keyspace_misses?: number;
  error?: string;
}

interface KeyDistribution {
  type: string;
  pattern: string;
  count: number;
}

const REFRESH_INTERVAL = 30000;

/* ============ 缓存监控 TAB ============ */

function CacheMonitorTab() {
  const [caches, setCaches] = useState<CacheInstance[]>([]);
  const [redisConnected, setRedisConnected] = useState(false);
  const [health, setHealth] = useState<any>(null);
  const [loading, setLoading] = useState(false);
  const [redisServer, setRedisServer] = useState<RedisServer>({});
  const [keyDistribution, setKeyDistribution] = useState<KeyDistribution[]>([]);
  const [totalKeys, setTotalKeys] = useState(0);
  const [lastUpdated, setLastUpdated] = useState<string>("");
  const [autoRefresh, setAutoRefresh] = useState(true);

  const fetchData = useCallback(async () => {
    setLoading(true);
    try {
      const [statsRes, healthRes, cacheInfo] = await Promise.all([
        getCacheStats(),
        checkCacheHealth(),
        getCacheInfo(),
      ]);
      setCaches((statsRes.caches || []) as unknown as CacheInstance[]);
      setRedisConnected(statsRes.redis_connected);
      setHealth(healthRes);
      setRedisServer(cacheInfo.redis_server || {});
      setKeyDistribution(cacheInfo.key_distribution || []);
      setTotalKeys(cacheInfo.total_keys || 0);
      setLastUpdated(new Date().toLocaleTimeString("zh-CN"));
    } catch {
      // error handled by interceptor
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchData();
  }, [fetchData]);

  useEffect(() => {
    if (!autoRefresh) return;
    const timer = setInterval(fetchData, REFRESH_INTERVAL);
    return () => clearInterval(timer);
  }, [autoRefresh, fetchData]);

  const handleClearCache = async (pattern: string) => {
    try {
      const res = await clearCache(pattern);
      message.success(res.message || "缓存已清除");
      fetchData();
    } catch {
      // error handled by interceptor
    }
  };

  const parseHitRate = (rate: number | string | undefined): number => {
    if (rate === undefined || rate === null) return 0;
    if (typeof rate === "number") return rate;
    return parseFloat(String(rate)) || 0;
  };

  const getHitRateColor = (rate: number) => {
    if (rate >= 80) return "#52c41a";
    if (rate >= 50) return "#faad14";
    return "#ff4d4f";
  };

  const getTypeIcon = (type: string) => {
    switch (type) {
      case "redis": return <CloudServerOutlined />;
      case "memory": return <DatabaseOutlined />;
      case "file": return <HddOutlined />;
      case "file_index": return <FileTextOutlined />;
      default: return <PieChartOutlined />;
    }
  };

  const getTypeLabel = (type: string) => {
    const map: Record<string, string> = {
      redis: "Redis",
      memory: "内存",
      file: "文件缓存",
      file_index: "文件索引",
    };
    return map[type] || type;
  };

  const getNameLabel = (name: string) => {
    const map: Record<string, string> = {
      redis_cache: "Redis 缓存",
      memory_fallback: "内存缓存（降级）",
      inline_marker_cache: "内联图表标记缓存",
      chart_cache: "图表渲染缓存",
      export_result_cache: "导出结果缓存",
    };
    return map[name] || name;
  };

  const formatUptime = (seconds: number) => {
    if (!seconds) return "-";
    const days = Math.floor(seconds / 86400);
    const hours = Math.floor((seconds % 86400) / 3600);
    const mins = Math.floor((seconds % 3600) / 60);
    if (days > 0) return `${days}天 ${hours}小时`;
    if (hours > 0) return `${hours}小时 ${mins}分钟`;
    return `${mins}分钟`;
  };

  const getKeyDistColor = (count: number, max: number) => {
    if (max === 0) return "#d9d9d9";
    const ratio = count / max;
    if (ratio > 0.7) return "#52c41a";
    if (ratio > 0.3) return "#faad14";
    return "#1890ff";
  };

  const keyDistColumns = [
    { title: "缓存类型", dataIndex: "type", key: "type", render: (t: string) => <Text strong>{t}</Text> },
    { title: "键模式", dataIndex: "pattern", key: "pattern", render: (t: string) => <Text code style={{ fontSize: 12 }}>{t}</Text> },
    { title: "键数量", dataIndex: "count", key: "count", width: 120, render: (c: number) => <Text strong>{c.toLocaleString()}</Text> },
    {
      title: "占比", key: "bar", width: 200,
      render: (_: any, record: KeyDistribution) => (
        <Tooltip title={`${record.count} 个键`}>
          <Progress percent={totalKeys > 0 ? Math.round((record.count / totalKeys) * 100) : 0} size="small" showInfo={false} strokeColor={getKeyDistColor(record.count, totalKeys)} />
        </Tooltip>
      ),
    },
  ];

  const overallHitRate = redisServer.keyspace_hits !== undefined && redisServer.keyspace_misses !== undefined
    ? redisServer.keyspace_hits + redisServer.keyspace_misses > 0
      ? (redisServer.keyspace_hits / (redisServer.keyspace_hits + redisServer.keyspace_misses)) * 100
      : 0
    : undefined;

  return (
    <div>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 16 }}>
        <Text type="secondary">
          {autoRefresh && `每 ${REFRESH_INTERVAL / 1000} 秒自动刷新`}
          {lastUpdated && ` · 最后更新：${lastUpdated}`}
        </Text>
        <Space>
          <Switch checked={autoRefresh} onChange={setAutoRefresh} size="small" checkedChildren="自动" uncheckedChildren="关闭" />
          <Button icon={<ReloadOutlined />} onClick={fetchData} loading={loading}>刷新</Button>
        </Space>
      </div>

      {redisConnected && (
        <Card title={<Space><CloudServerOutlined /> Redis 服务端</Space>} size="small" style={{ marginBottom: 16 }}>
          <Row gutter={[16, 16]}>
            <Col xs={12} sm={8} md={4}>
              <Statistic title="内存使用" value={redisServer.memory_used_human || "-"} suffix={<RiseOutlined style={{ color: "#1890ff" }} />} valueStyle={{ fontSize: 18 }} />
            </Col>
            <Col xs={12} sm={8} md={4}>
              <Statistic title="连接客户端" value={redisServer.connected_clients || 0} prefix={<DatabaseOutlined />} valueStyle={{ fontSize: 18 }} />
            </Col>
            <Col xs={12} sm={8} md={4}>
              <Statistic title="运行时间" value={formatUptime(redisServer.uptime_seconds || 0)} prefix={<ClockCircleOutlined />} valueStyle={{ fontSize: 18 }} />
            </Col>
            <Col xs={12} sm={8} md={4}>
              <Statistic title="缓存键总数" value={totalKeys} prefix={<PieChartOutlined />} valueStyle={{ fontSize: 18 }} />
            </Col>
            <Col xs={12} sm={8} md={4}>
              <Statistic title="系统命中率" value={overallHitRate !== undefined ? overallHitRate.toFixed(1) : "-"} suffix="%" prefix={<FallOutlined />} valueStyle={{ fontSize: 18, color: overallHitRate !== undefined ? getHitRateColor(overallHitRate) : undefined }} />
            </Col>
            <Col xs={12} sm={8} md={4}>
              <Statistic title="淘汰键数" value={redisServer.evicted_keys || 0} prefix={redisServer.evicted_keys && redisServer.evicted_keys > 0 ? <WarningOutlined style={{ color: "#faad14" }} /> : <CheckCircleOutlined style={{ color: "#52c41a" }} />} valueStyle={{ fontSize: 18, color: (redisServer.evicted_keys || 0) > 0 ? "#faad14" : undefined }} />
            </Col>
          </Row>
          {redisServer.memory_used_bytes !== undefined && redisServer.memory_used_bytes > 0 && (
            <div style={{ marginTop: 16 }}>
              <div style={{ display: "flex", justificationContent: "space-between", marginBottom: 4 }}>
                <Text type="secondary" style={{ fontSize: 12 }}>内存占用</Text>
                <Text style={{ fontSize: 12 }}>{redisServer.memory_used_bytes} bytes</Text>
              </div>
              <Progress percent={Math.min(100, Math.round((redisServer.memory_used_bytes / (1024 * 1024 * 1024)) * 100 * 10)) / 10} strokeColor="#1890ff" size="small" showInfo={false} />
            </div>
          )}
        </Card>
      )}

      <Row gutter={[16, 16]} style={{ marginBottom: 16 }}>
        <Col xs={24} md={12}>
          <Card title="Redis 连接状态" size="small">
            <Space direction="vertical" style={{ width: "100%" }}>
              <div>
                <Text type="secondary">连接状态:</Text>
                <br />
                {redisConnected ? <Tag color="green" icon={<CheckCircleOutlined />}>已连接</Tag> : <Tag color="orange" icon={<CloseCircleOutlined />}>未连接（降级模式）</Tag>}
              </div>
              {health && (
                <>
                  {health.latency_ms !== undefined && (
                    <div><Text type="secondary">响应延迟:</Text><br /><Text strong>{health.latency_ms.toFixed(2)} ms</Text></div>
                  )}
                  <div><Text type="secondary">服务状态:</Text><br /><Tag color={health.status === "healthy" ? "green" : "orange"}>{health.status === "healthy" ? "健康" : "降级"}</Tag></div>
                </>
              )}
            </Space>
          </Card>
        </Col>
        <Col xs={24} md={12}>
          <Card title="缓存实例概览" size="small">
            <Row gutter={16}>
              <Col span={12}>
                <Statistic title="缓存实例数" value={caches.length} valueStyle={{ fontSize: 24 }} prefix={<DatabaseOutlined />} suffix={<Text type="secondary">个</Text>} />
              </Col>
              <Col span={12}>
                <Statistic title="Redis 状态" value={redisConnected ? "已连接" : "降级中"} valueStyle={{ fontSize: 24, color: redisConnected ? "#52c41a" : "#faad14" }} />
              </Col>
            </Row>
          </Card>
        </Col>
      </Row>

      {redisConnected && keyDistribution.length > 0 && (
        <Card title={<Space><PieChartOutlined /> 缓存键分布</Space>} size="small" style={{ marginBottom: 16 }}>
          <Table columns={keyDistColumns} dataSource={keyDistribution} rowKey="pattern" pagination={false} size="small" />
        </Card>
      )}

      {caches.length > 0 && (
        <Row gutter={[16, 16]}>
          {caches.map((cache, index) => {
            const rate = parseHitRate(cache.hit_rate);
            const rateColor = getHitRateColor(rate);
            const total = (cache.hits || 0) + (cache.misses || 0);
            return (
              <Col xs={24} md={12} key={index}>
                <Card title={<Space>{getTypeIcon(cache.type)}<Text strong>{getNameLabel(cache.name)}</Text><Tag>{getTypeLabel(cache.type)}</Tag></Space>} size="small"
                  extra={cache.type === "redis" && <Tooltip title="清除该类型缓存"><Button size="small" type="text" danger onClick={() => handleClearCache(`*${cache.name}*`)}>清除</Button></Tooltip>}>
                  <Row gutter={12}>
                    <Col span={8}><Statistic title="命中" value={cache.hits || 0} valueStyle={{ fontSize: 20, color: "#52c41a" }} suffix={<Text type="secondary" style={{ fontSize: 12 }}>次</Text>} /></Col>
                    <Col span={8}><Statistic title="未命中" value={cache.misses || 0} valueStyle={{ fontSize: 20, color: "#ff4d4f" }} suffix={<Text type="secondary" style={{ fontSize: 12 }}>次</Text>} /></Col>
                    <Col span={8}><Statistic title="总请求" value={total} valueStyle={{ fontSize: 20 }} suffix={<Text type="secondary" style={{ fontSize: 12 }}>次</Text>} /></Col>
                  </Row>
                  <div style={{ marginTop: 12 }}>
                    <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 4 }}>
                      <Text type="secondary" style={{ fontSize: 13 }}>命中率</Text>
                      <Text strong style={{ color: rateColor, fontSize: 13 }}>{rate.toFixed(1)}%</Text>
                    </div>
                    <Progress percent={Math.min(100, Math.round(rate))} strokeColor={rateColor} size="small" showInfo={false} />
                  </div>
                  <div style={{ marginTop: 8, fontSize: 13, color: "#888" }}>
                    {cache.size !== undefined && <div><Text type="secondary">条目数: </Text><Text>{cache.size}{cache.max_size ? ` / ${cache.max_size}` : ""}</Text></div>}
                    {cache.file_count !== undefined && <div><Text type="secondary">文件数: </Text><Text>{cache.file_count}</Text></div>}
                    {cache.total_size_mb !== undefined && <div><Text type="secondary">占用空间: </Text><Text>{cache.total_size_mb} MB</Text></div>}
                    {cache.ttl_seconds !== undefined && <div><Text type="secondary">TTL: </Text><Text>{cache.ttl_seconds}s</Text></div>}
                  </div>
                </Card>
              </Col>
            );
          })}
        </Row>
      )}

      {!redisConnected && (
        <Alert type="warning" message="Redis 未连接，将使用内存缓存作为降级方案" showIcon style={{ marginTop: 16 }} />
      )}
    </div>
  );
}

/* ============ AI 调用统计 TAB ============ */

function AiStatsTab() {
  const [data, setData] = useState<any>(null);
  const [loading, setLoading] = useState(false);

  const fetchData = useCallback(async () => {
    setLoading(true);
    try {
      const res = await getMonitorAiStats();
      setData(res);
    } catch {
      // handled by interceptor
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { fetchData(); }, [fetchData]);

  if (!data) return <Card loading={loading}>加载中...</Card>;

  const providers = data.providers || [];
  const summary = data.summary || {};

  return (
    <div>
      <Space style={{ marginBottom: 16 }}>
        <Button icon={<ReloadOutlined />} onClick={fetchData} loading={loading}>刷新</Button>
      </Space>

      {summary.total_calls !== undefined && (
        <Row gutter={[16, 16]} style={{ marginBottom: 16 }}>
          <Col xs={12} sm={8} md={4}>
            <Statistic title="总调用次数" value={summary.total_calls} prefix={<ThunderboltOutlined />} valueStyle={{ fontSize: 20, color: "#1890ff" }} />
          </Col>
          <Col xs={12} sm={8} md={4}>
            <Statistic title="总 Token" value={summary.total_tokens} prefix={<DatabaseOutlined />} valueStyle={{ fontSize: 20 }} />
          </Col>
          <Col xs={12} sm={8} md={4}>
            <Statistic title="成功率" value={summary.success_rate} suffix="%" valueStyle={{ fontSize: 20, color: (summary.success_rate || 0) >= 95 ? "#52c41a" : "#faad14" }} />
          </Col>
        </Row>
      )}

      {providers.length > 0 ? (
        <Row gutter={[16, 16]}>
          {providers.map((p: any, i: number) => (
            <Col xs={24} md={12} key={i}>
              <Card title={<Space><ThunderboltOutlined /><Text strong>{p.provider || `Provider ${i + 1}`}</Text></Space>} size="small">
                <Row gutter={12}>
                  <Col span={8}><Statistic title="调用次数" value={p.total_calls || 0} valueStyle={{ fontSize: 16 }} /></Col>
                  <Col span={8}><Statistic title="Token 用量" value={p.total_tokens || 0} valueStyle={{ fontSize: 16 }} /></Col>
                  <Col span={8}><Statistic title="成功率" value={p.success_rate || 0} suffix="%" valueStyle={{ fontSize: 16, color: (p.success_rate || 0) >= 95 ? "#52c41a" : "#faad14" }} /></Col>
                </Row>
                {p.recent_calls && p.recent_calls > 0 && (
                  <div style={{ marginTop: 8 }}>
                    <Text type="secondary" style={{ fontSize: 12 }}>最近调用: {p.recent_calls} 次</Text>
                  </div>
                )}
              </Card>
            </Col>
          ))}
        </Row>
      ) : (
        <Card><Text type="secondary">暂无 AI 调用统计数据</Text></Card>
      )}
    </div>
  );
}

/* ============ 告警管理 TAB ============ */

function AlertsTab() {
  const [alerts, setAlerts] = useState<any[]>([]);
  const [health, setHealth] = useState<any>(null);
  const [loading, setLoading] = useState(false);
  const [clearing, setClearing] = useState(false);

  const fetchData = useCallback(async () => {
    setLoading(true);
    try {
      const h = await getMonitorHealth();
      setHealth(h);
      setAlerts(h.alerts || []);
    } catch {
      // handled
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { fetchData(); }, [fetchData]);

  const handleClearAll = async () => {
    setClearing(true);
    try {
      await clearMonitorAlerts();
      message.success("告警已清除");
      fetchData();
    } catch {
      // handled
    } finally {
      setClearing(false);
    }
  };

  return (
    <div>
      <Space style={{ marginBottom: 16 }}>
        <Button icon={<ReloadOutlined />} onClick={fetchData} loading={loading}>刷新</Button>
        {alerts.length > 0 && (
          <Button danger icon={<AlertOutlined />} onClick={handleClearAll} loading={clearing}>清除全部告警</Button>
        )}
      </Space>

      {health && (
        <Descriptions title="监控服务健康状态" bordered size="small" style={{ marginBottom: 16 }}>
          <Descriptions.Item label="状态">
            <Tag color={health.status === "healthy" ? "green" : "orange"}>{health.status === "healthy" ? "健康" : "异常"}</Tag>
          </Descriptions.Item>
          {health.timestamp && <Descriptions.Item label="检查时间">{health.timestamp}</Descriptions.Item>}
        </Descriptions>
      )}

      {alerts.length === 0 ? (
        <Card><Alert type="success" message="当前无活跃告警" showIcon /></Card>
      ) : (
        <Card title={`活跃告警 (${alerts.length})`} size="small">
          <Table
            dataSource={alerts}
            rowKey={(_, i) => String(i)}
            pagination={false}
            size="small"
            columns={[
              { title: "级别", dataIndex: "level", key: "level", render: (l: string) => <Tag color={l === "critical" ? "red" : l === "warning" ? "orange" : "blue"}>{l}</Tag> },
              { title: "来源", dataIndex: "source", key: "source" },
              { title: "消息", dataIndex: "message", key: "message" },
              { title: "时间", dataIndex: "timestamp", key: "timestamp", width: 180 },
            ]}
          />
        </Card>
      )}
    </div>
  );
}

/* ============ 并发统计 TAB ============ */

function ConcurrencyTab() {
  const [data, setData] = useState<any>(null);
  const [loading, setLoading] = useState(false);

  const fetchData = useCallback(async () => {
    setLoading(true);
    try {
      const res = await getMonitorConcurrencyStats();
      setData(res);
    } catch {
      // handled
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { fetchData(); }, [fetchData]);

  if (!data) return <Card loading={loading}>加载中...</Card>;

  const limits = data.limits || {};
  const current = data.current || {};
  const history = data.history || [];

  return (
    <div>
      <Button icon={<ReloadOutlined />} onClick={fetchData} loading={loading} style={{ marginBottom: 16 }}>刷新</Button>

      <Row gutter={[16, 16]} style={{ marginBottom: 16 }}>
        {Object.entries(limits).map(([key, val]: [string, number]) => {
          const cur = current[key];
          const ratio = cur !== undefined ? Math.round((cur / val) * 100) : 0;
          return (
            <Col xs={24} sm={12} md={8} key={key}>
              <Card size="small">
                <Text strong>{key}</Text>
                <div style={{ marginTop: 8 }}>
                  <Progress percent={ratio} strokeColor={ratio > 80 ? "#ff4d4f" : ratio > 50 ? "#faad14" : "#52c41a"} />
                  <div style={{ display: "flex", justifyContent: "space-between", marginTop: 4 }}>
                    <Text type="secondary" style={{ fontSize: 12 }}>当前: {cur ?? "-"}</Text>
                    <Text type="secondary" style={{ fontSize: 12 }}>上限: {val}</Text>
                  </div>
                </div>
              </Card>
            </Col>
          );
        })}
      </Row>

      {history.length > 0 && (
        <Card title="并发历史" size="small">
          <Table dataSource={history.slice(-20)} rowKey={(_, i) => String(i)} pagination={false} size="small"
            columns={[
              { title: "时间", dataIndex: "timestamp", key: "timestamp", width: 180 },
              { title: "并发数", dataIndex: "concurrent", key: "concurrent", render: (v: number) => <Text strong>{v}</Text> },
              { title: "排队", dataIndex: "queued", key: "queued", render: (v: number) => v > 0 ? <Text type="warning">{v}</Text> : <Text>{v || 0}</Text> },
            ]} />
        </Card>
      )}
    </div>
  );
}

/* ============ 修复统计 TAB ============ */

function RepairStatsTab() {
  const [data, setData] = useState<any>(null);
  const [history, setHistory] = useState<any[]>([]);
  const [loading, setLoading] = useState(false);
  const [flushing, setFlushing] = useState(false);
  const [cleaning, setCleaning] = useState(false);

  const fetchData = useCallback(async () => {
    setLoading(true);
    try {
      const [stats, hist] = await Promise.all([getRepairStats(), getRepairStatsHistory()]);
      setData(stats);
      setHistory(hist.snaps || hist.history || []);
    } catch {
      // handled
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { fetchData(); }, [fetchData]);

  const handleFlush = async () => {
    setFlushing(true);
    try {
      await flushRepairStats();
      message.success("统计已刷新");
      fetchData();
    } catch {
      // handled
    } finally {
      setFlushing(false);
    }
  };

  const handleCleanup = async () => {
    setCleaning(true);
    try {
      await cleanupRepairStats();
      message.success("历史已清理");
      fetchData();
    } catch {
      // handled
    } finally {
      setCleaning(false);
    }
  };

  if (!data) return <Card loading={loading}>加载中...</Card>;

  const steps = data.steps || {};
  const summary = data.summary || {};

  return (
    <div>
      <Space style={{ marginBottom: 16 }}>
        <Button icon={<ReloadOutlined />} onClick={fetchData} loading={loading}>刷新</Button>
        <Button icon={<MergeCellsOutlined />} onClick={handleFlush} loading={flushing}>刷新统计</Button>
        {history.length > 0 && <Button danger onClick={handleCleanup} loading={cleaning}>清理历史</Button>}
      </Space>

      {summary.total_repairs !== undefined && (
        <Row gutter={[16, 16]} style={{ marginBottom: 16 }}>
          <Col xs={12} sm={8}>
            <Statistic title="总修复次数" value={summary.total_repairs} prefix={<MergeCellsOutlined />} valueStyle={{ fontSize: 20, color: "#722ed1" }} />
          </Col>
          <Col xs={12} sm={8}>
            <Statistic title="成功率" value={summary.success_rate || 0} suffix="%" valueStyle={{ fontSize: 20, color: (summary.success_rate || 0) >= 90 ? "#52c41a" : "#faad14" }} />
          </Col>
          <Col xs={12} sm={8}>
            <Statistic title="步骤种类" value={Object.keys(steps).length} prefix={<BarChartOutlined />} valueStyle={{ fontSize: 20 }} />
          </Col>
        </Row>
      )}

      {Object.keys(steps).length > 0 && (
        <Card title="各步骤修复统计" size="small" style={{ marginBottom: 16 }}>
          <Table dataSource={Object.entries(steps).map(([name, s]: [string, any]) => ({ ...s, name }))} rowKey="name" pagination={false} size="small"
            columns={[
              { title: "步骤", dataIndex: "name", key: "name", render: (n: string) => <Text strong>{n}</Text> },
              { title: "总次数", dataIndex: "total", key: "total", width: 100 },
              { title: "成功", dataIndex: "success", key: "success", width: 100, render: (v: number) => <Text style={{ color: "#52c41a" }}>{v}</Text> },
              { title: "失败", dataIndex: "fail", key: "fail", width: 100, render: (v: number) => v > 0 ? <Text style={{ color: "#ff4d4f" }}>{v}</Text> : <Text>{v || 0}</Text> },
              { title: "成功率", key: "rate", width: 100, render: (_: any, r: any) => r.total > 0 ? `${Math.round((r.success / r.total) * 100)}%` : "-" },
            ]} />
        </Card>
      )}

      {history.length > 0 && (
        <Card title="历史趋势" size="small">
          <Table dataSource={history.slice(-30)} rowKey={(_, i) => String(i)} pagination={false} size="small"
            columns={[
              { title: "时间", dataIndex: "timestamp", key: "timestamp", width: 180 },
              { title: "总修复", dataIndex: "total_repairs", key: "total_repairs", render: (v: number) => <Text strong>{v}</Text> },
              { title: "成功", dataIndex: "success", key: "success", render: (v: number) => <Text style={{ color: "#52c41a" }}>{v}</Text> },
              { title: "失败", dataIndex: "fail", key: "fail", render: (v: number) => v > 0 ? <Text style={{ color: "#ff4d4f" }}>{v}</Text> : <Text>{v || 0}</Text> },
            ]} />
        </Card>
      )}
    </div>
  );
}

/* ============ 主页面：系统监控中心 ============ */

export default function CacheMonitorPage() {
  const [activeTab, setActiveTab] = useState("cache");

  return (
    <div style={{ maxWidth: 1200, margin: "0 auto" }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 16 }}>
        <Title level="3" style={{ margin: 0 }}>
          <CloudServerOutlined /> 系统监控中心
        </Title>
      </div>

      <Tabs
        activeKey={activeTab}
        onChange={setActiveTab}
        items={[
          { key: "cache", label: <span><DatabaseOutlined /> 缓存监控</span>, children: <CacheMonitorTab /> },
          { key: "ai-stats", label: <span><ThunderboltOutlined /> AI 调用统计</span>, children: <AiStatsTab /> },
          { key: "alerts", label: <span><AlertOutlined /> 告警管理</span>, children: <AlertsTab /> },
          { key: "concurrency", label: <span><BarChartOutlined /> 并发统计</span>, children: <ConcurrencyTab /> },
          { key: "repair-stats", label: <span><MergeCellsOutlined /> 修复统计</span>, children: <RepairStatsTab /> },
        ]}
      />
    </div>
  );
}