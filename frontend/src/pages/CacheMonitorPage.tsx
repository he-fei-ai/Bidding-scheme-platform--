import { useEffect, useState, useCallback } from "react";
import {
  Card, Typography, Space, Tag, Button, Alert, Progress, Statistic, Row, Col,
  Table, message, Tooltip,
} from "antd";
import {
  CheckCircleOutlined, CloseCircleOutlined,
  ReloadOutlined, DatabaseOutlined, CloudServerOutlined, FileTextOutlined,
  PieChartOutlined, HddOutlined, RiseOutlined, FallOutlined,
  ClockCircleOutlined, WarningOutlined,
} from "@ant-design/icons";
import {
  getCacheStats, clearCache, checkCacheHealth, getCacheInfo,
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

export default function CacheMonitorPage() {
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
    {
      title: "缓存类型",
      dataIndex: "type",
      key: "type",
      render: (text: string) => <Text strong>{text}</Text>,
    },
    {
      title: "键模式",
      dataIndex: "pattern",
      key: "pattern",
      render: (text: string) => <Text code style={{ fontSize: 12 }}>{text}</Text>,
    },
    {
      title: "键数量",
      dataIndex: "count",
      key: "count",
      width: 120,
      render: (count: number) => <Text strong>{count.toLocaleString()}</Text>,
    },
    {
      title: "占比",
      key: "bar",
      width: 200,
      render: (_: any, record: KeyDistribution) => (
        <Tooltip title={`${record.count} 个键`}>
          <Progress
            percent={totalKeys > 0 ? Math.round((record.count / totalKeys) * 100) : 0}
            size="small"
            showInfo={false}
            strokeColor={getKeyDistColor(record.count, totalKeys)}
          />
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
    <div style={{ maxWidth: 1200, margin: "0 auto" }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 24 }}>
        <div>
          <Title level={3}>
            <CloudServerOutlined /> 缓存监控
          </Title>
          <Text type="secondary">
            {autoRefresh && `每 ${REFRESH_INTERVAL / 1000} 秒自动刷新`}
            {lastUpdated && ` · 最后更新：${lastUpdated}`}
          </Text>
        </div>
        <Space>
          <Button
            type="text"
            size="small"
            onClick={() => setAutoRefresh(v => !v)}
            style={{ color: autoRefresh ? "#52c41a" : "#999" }}
          >
            {autoRefresh ? <CheckCircleOutlined /> : <CloseCircleOutlined />}
            {autoRefresh ? "自动刷新开" : "自动刷新关"}
          </Button>
          <Button icon={<ReloadOutlined />} onClick={fetchData} loading={loading}>
            手动刷新
          </Button>
        </Space>
      </div>

      {/* Redis 服务端指标 */}
      {redisConnected && (
        <Card title={<Space><CloudServerOutlined /> Redis 服务端</Space>} size="small" style={{ marginBottom: 16 }}>
          <Row gutter={[16, 16]}>
            <Col xs={12} sm={8} md={4}>
              <Statistic
                title="内存使用"
                value={redisServer.memory_used_human || "-"}
                suffix={<RiseOutlined style={{ color: "#1890ff" }} />}
                valueStyle={{ fontSize: 18 }}
              />
            </Col>
            <Col xs={12} sm={8} md={4}>
              <Statistic
                title="连接客户端"
                value={redisServer.connected_clients || 0}
                prefix={<DatabaseOutlined />}
                valueStyle={{ fontSize: 18 }}
              />
            </Col>
            <Col xs={12} sm={8} md={4}>
              <Statistic
                title="运行时间"
                value={formatUptime(redisServer.uptime_seconds || 0)}
                prefix={<ClockCircleOutlined />}
                valueStyle={{ fontSize: 18 }}
              />
            </Col>
            <Col xs={12} sm={8} md={4}>
              <Statistic
                title="缓存键总数"
                value={totalKeys}
                prefix={<PieChartOutlined />}
                valueStyle={{ fontSize: 18 }}
              />
            </Col>
            <Col xs={12} sm={8} md={4}>
              <Statistic
                title="系统命中率"
                value={overallHitRate !== undefined ? overallHitRate.toFixed(1) : "-"}
                suffix="%"
                prefix={<FallOutlined />}
                valueStyle={{
                  fontSize: 18,
                  color: overallHitRate !== undefined
                    ? getHitRateColor(overallHitRate)
                    : undefined,
                }}
              />
            </Col>
            <Col xs={12} sm={8} md={4}>
              <Statistic
                title="淘汰键数"
                value={redisServer.evicted_keys || 0}
                prefix={redisServer.evicted_keys && redisServer.evicted_keys > 0
                  ? <WarningOutlined style={{ color: "#faad14" }} />
                  : <CheckCircleOutlined style={{ color: "#52c41a" }} />
                }
                valueStyle={{
                  fontSize: 18,
                  color: (redisServer.evicted_keys || 0) > 0 ? "#faad14" : undefined,
                }}
              />
            </Col>
          </Row>

          {/* 内存条 */}
          {redisServer.memory_used_bytes !== undefined && redisServer.memory_used_bytes > 0 && (
            <div style={{ marginTop: 16 }}>
              <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 4 }}>
                <Text type="secondary" style={{ fontSize: 12 }}>内存占用</Text>
                <Text style={{ fontSize: 12 }}>
                  {redisServer.memory_used_bytes} bytes
                </Text>
              </div>
              <Progress
                percent={Math.min(100, Math.round((redisServer.memory_used_bytes / (1024 * 1024 * 1024)) * 100 * 10)) / 10}
                strokeColor="#1890ff"
                size="small"
                showInfo={false}
              />
            </div>
          )}
        </Card>
      )}

      {/* 缓存实例列表 */}
      <Row gutter={[16, 16]} style={{ marginBottom: 16 }}>
        <Col xs={24} md={12}>
          <Card title="Redis 连接状态" size="small">
            <Space direction="vertical" style={{ width: "100%" }}>
              <div>
                <Text type="secondary">连接状态:</Text>
                <br />
                {redisConnected ? (
                  <Tag color="green" icon={<CheckCircleOutlined />}>已连接</Tag>
                ) : (
                  <Tag color="orange" icon={<CloseCircleOutlined />}>未连接（降级模式）</Tag>
                )}
              </div>
              {health && (
                <>
                  {health.latency_ms !== undefined && (
                    <div>
                      <Text type="secondary">响应延迟:</Text>
                      <br />
                      <Text strong>{health.latency_ms.toFixed(2)} ms</Text>
                    </div>
                  )}
                  <div>
                    <Text type="secondary">服务状态:</Text>
                    <br />
                    <Tag color={health.status === "healthy" ? "green" : "orange"}>
                      {health.status === "healthy" ? "健康" : "降级"}
                    </Tag>
                  </div>
                </>
              )}
            </Space>
          </Card>
        </Col>
        <Col xs={24} md={12}>
          <Card title="缓存实例概览" size="small">
            <Row gutter={16}>
              <Col span={12}>
                <Statistic
                  title="缓存实例数"
                  value={caches.length}
                  valueStyle={{ fontSize: 24 }}
                  prefix={<DatabaseOutlined />}
                  suffix={<Text type="secondary">个</Text>}
                />
              </Col>
              <Col span={12}>
                <Statistic
                  title="Redis 状态"
                  value={redisConnected ? "已连接" : "降级中"}
                  valueStyle={{ fontSize: 24, color: redisConnected ? "#52c41a" : "#faad14" }}
                />
              </Col>
            </Row>
          </Card>
        </Col>
      </Row>

      {/* 键分布统计 */}
      {redisConnected && keyDistribution.length > 0 && (
        <Card
          title={<Space><PieChartOutlined /> 缓存键分布</Space>}
          size="small"
          style={{ marginBottom: 16 }}
        >
          <Table
            columns={keyDistColumns}
            dataSource={keyDistribution}
            rowKey="pattern"
            pagination={false}
            size="small"
            summary={() => (
              <Table.Summary fixed>
                <Table.Summary.Row>
                  <Table.Summary.Cell index={0} colSpan={2}>
                    <Text strong>合计</Text>
                  </Table.Summary.Cell>
                  <Table.Summary.Cell index={1}>
                    <Text strong>{totalKeys.toLocaleString()}</Text>
                  </Table.Summary.Cell>
                  <Table.Summary.Cell index={2}>
                    <Progress percent={100} size="small" showInfo={false} />
                  </Table.Summary.Cell>
                </Table.Summary.Row>
              </Table.Summary>
            )}
          />
        </Card>
      )}

      {/* 缓存实例卡片 */}
      {caches.length > 0 && (
        <Row gutter={[16, 16]}>
          {caches.map((cache, index) => {
            const rate = parseHitRate(cache.hit_rate);
            const rateColor = getHitRateColor(rate);
            const total = (cache.hits || 0) + (cache.misses || 0);

            return (
              <Col xs={24} md={12} key={index}>
                <Card
                  title={
                    <Space>
                      {getTypeIcon(cache.type)}
                      <Text strong>{getNameLabel(cache.name)}</Text>
                      <Tag>{getTypeLabel(cache.type)}</Tag>
                    </Space>
                  }
                  size="small"
                  extra={
                    cache.type === "redis" && (
                      <Tooltip title="清除该类型缓存">
                        <Button
                          size="small"
                          type="text"
                          danger
                          onClick={() => handleClearCache(`*${cache.name}*`)}
                        >
                          清除
                        </Button>
                      </Tooltip>
                    )
                  }
                >
                  <Row gutter={12}>
                    <Col span={8}>
                      <Statistic
                        title="命中"
                        value={cache.hits || 0}
                        valueStyle={{ fontSize: 20, color: "#52c41a" }}
                        suffix={<Text type="secondary" style={{ fontSize: 12 }}>次</Text>}
                      />
                    </Col>
                    <Col span={8}>
                      <Statistic
                        title="未命中"
                        value={cache.misses || 0}
                        valueStyle={{ fontSize: 20, color: "#ff4d4f" }}
                        suffix={<Text type="secondary" style={{ fontSize: 12 }}>次</Text>}
                      />
                    </Col>
                    <Col span={8}>
                      <Statistic
                        title="总请求"
                        value={total}
                        valueStyle={{ fontSize: 20 }}
                        suffix={<Text type="secondary" style={{ fontSize: 12 }}>次</Text>}
                      />
                    </Col>
                  </Row>

                  <div style={{ marginTop: 12 }}>
                    <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 4 }}>
                      <Text type="secondary" style={{ fontSize: 13 }}>命中率</Text>
                      <Text strong style={{ color: rateColor, fontSize: 13 }}>{rate.toFixed(1)}%</Text>
                    </div>
                    <Progress
                      percent={Math.min(100, Math.round(rate))}
                      strokeColor={rateColor}
                      size="small"
                      showInfo={false}
                    />
                  </div>

                  <div style={{ marginTop: 8, fontSize: 13, color: "#888" }}>
                    {cache.size !== undefined && (
                      <div>
                        <Text type="secondary">条目数: </Text>
                        <Text>{cache.size}{cache.max_size ? ` / ${cache.max_size}` : ""}</Text>
                      </div>
                    )}
                    {cache.file_count !== undefined && (
                      <div>
                        <Text type="secondary">文件数: </Text>
                        <Text>{cache.file_count}</Text>
                      </div>
                    )}
                    {cache.total_size_mb !== undefined && (
                      <div>
                        <Text type="secondary">占用空间: </Text>
                        <Text>{cache.total_size_mb} MB</Text>
                      </div>
                    )}
                    {cache.ttl_seconds !== undefined && (
                      <div>
                        <Text type="secondary">TTL: </Text>
                        <Text>{cache.ttl_seconds}s</Text>
                      </div>
                    )}
                  </div>
                </Card>
              </Col>
            );
          })}
        </Row>
      )}

      {!redisConnected && (
        <Alert
          type="warning"
          message="Redis 未连接，将使用内存缓存作为降级方案"
          showIcon
          style={{ marginTop: 16 }}
        />
      )}
    </div>
  );
}
