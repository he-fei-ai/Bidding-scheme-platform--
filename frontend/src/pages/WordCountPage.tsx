import { useEffect, useState } from "react";
import { useParams, useNavigate } from "react-router-dom";
import {
  Button, Card, Col, Empty, InputNumber, Progress, Row, Space, Statistic, Table, Tag, Typography, message,
} from "antd";
import {
  ArrowLeftOutlined, FileTextOutlined, CheckCircleOutlined, AimOutlined, WarningOutlined, BarChartOutlined,
} from "@ant-design/icons";
import { getWordCountOverview, getWordCountSections, setSectionTargetWords } from "../api";
import type { WordCountOverview, WordCountItem } from "../api";
import { extractApiError, getLevelColor, getLevelLabel, resolveProgressLowerBound } from "../utils";

const { Text } = Typography;

const statusLabel: Record<string, string> = {
  pending: "待生成",
  generating: "生成中",
  completed: "已完成",
  failed: "失败",
};

const statusColor: Record<string, string> = {
  pending: "default",
  generating: "processing",
  completed: "success",
  failed: "error",
};

export default function WordCountPage() {
  const { id } = useParams<{ id: string }>();
  const projectId = Number(id);
  const navigate = useNavigate();

  const [overview, setOverview] = useState<WordCountOverview | null>(null);
  const [items, setItems] = useState<WordCountItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [drafts, setDrafts] = useState<Record<number, number | null>>({});

  const load = async () => {
    setLoading(true);
    try {
      const [ov, list] = await Promise.all([
        getWordCountOverview(projectId),
        getWordCountSections(projectId),
      ]);
      setOverview(ov);
      setItems(list.items || []);
    } catch (e) {
      message.error(extractApiError(e));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    load();
  }, [projectId]);

  const handleSetTarget = async (sectionId: number, value: number | null) => {
    try {
      await setSectionTargetWords(projectId, sectionId, value ?? 0);
      message.success("目标字数已更新");
      load();
    } catch (e) {
      message.error(extractApiError(e));
    }
  };

  const columns = [
    {
      title: "标题",
      dataIndex: "title",
      key: "title",
      ellipsis: true,
      render: (title: string, record: WordCountItem) => (
        <Space>
          <Tag color={getLevelColor(record.level)} style={{ marginRight: 0 }}>
            {getLevelLabel(record.level)}
          </Tag>
          <Text style={{ paddingLeft: (record.level - 1) * 12 }}>{title}</Text>
        </Space>
      ),
    },
    {
      title: "状态",
      dataIndex: "status",
      key: "status",
      width: 100,
      render: (status: string) => (
        <Tag color={statusColor[status] || "default"}>{statusLabel[status] || status}</Tag>
      ),
    },
    {
      title: "当前字数",
      dataIndex: "word_count",
      key: "word_count",
      width: 120,
      sorter: (a: WordCountItem, b: WordCountItem) => a.word_count - b.word_count,
      render: (v: number) => <Text strong>{v.toLocaleString()}</Text>,
    },
    {
      title: "目标字数",
      dataIndex: "target_words",
      key: "target_words",
      width: 180,
      render: (v: number | null, record: WordCountItem) => (
        <InputNumber
          value={drafts[record.id] !== undefined ? drafts[record.id] : v}
          min={0}
          placeholder="未设置"
          style={{ width: 140 }}
          onChange={(value) => setDrafts((prev) => ({ ...prev, [record.id]: value ?? null }))}
          onBlur={() => {
            const draft = drafts[record.id];
            if (draft !== undefined && draft !== v) {
              handleSetTarget(record.id, draft);
            }
          }}
          // BUG-FIX 2026-09-16（中）：表格提示是「回车保存」，但此前只实现了失焦保存 ——
          // 按提示敲 Enter 什么都不会发生，切换页面/重新查询后草稿静默丢失。
          onPressEnter={(e) => {
            const draft = drafts[record.id];
            if (draft !== undefined && draft !== v) {
              handleSetTarget(record.id, draft);
            }
            (e.target as unknown as HTMLInputElement).blur();
          }}
        />
      ),
    },
    {
      title: "达成进度",
      dataIndex: "progress",
      key: "progress",
      width: 180,
      render: (progress: number, record: WordCountItem) => {
        // BUG-FIX 2026-10-09（前后端判定不一致）：本列此前用
        // `word_count < target_words` / `>= target_words` 判定，即**零容差**，
        // 完全忽略后端 `GET /word-count/sections` 已经算好的容差区间。
        // 于是同一个 300 字目标、实际 280 字的 L3 章节，后端 `on_target`
        // 判定为「在 240~360 内 = 达标」，本列却渲染成「缺 20 字」+ 红色进度条
        // —— 用户看到的画面与导出预检（services/export.py 走同一套区间）
        // 直接矛盾，且无法自查原因。
        // 现统一以服务端给出的 `on_target` 为准（它是唯一事实来源，
        // 已包含 L3 自身 300±20% 与四档档位各自 tolerance_ratio 的判定），
        // 「缺多少字」也改为相对**区间下界**计算，与红色进度条语义对齐。
        const onTarget = record.on_target === true;
        const unknown = record.on_target === "unknown";
        // 缺字数以达标区间下界为基准；后端未直接给区间时用 target - tolerance
        // 估算，tolerance 取自档位配置（tier_target 与 target_words 的关系由
        // 后端口径保证，前端仅在缺少 tier 信息时按 ±10% 保守估算）。
        const lowerBound = resolveProgressLowerBound(record);
        const hasTarget = record.target_words != null && record.target_words > 0;
        const below = !onTarget && !unknown && hasTarget && record.word_count < lowerBound;
        const done = onTarget;
        return (
          <Space direction="vertical" size={2} style={{ width: "100%" }}>
            <Progress
              percent={progress}
              size="small"
              status={done ? "success" : below ? "exception" : "active"}
            />
            {below && (
              <Text type="warning" style={{ fontSize: 12 }}>
                缺 {(lowerBound - record.word_count).toLocaleString()} 字
              </Text>
            )}
            {done && <Text type="success" style={{ fontSize: 12 }}>已达标</Text>}
            {!done && !below && unknown && (
              <Text type="secondary" style={{ fontSize: 12 }}>待生成</Text>
            )}
          </Space>
        );
      },
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
            <BarChartOutlined /> 字数统计
          </Typography.Title>
        </Space>
        <Button onClick={load} loading={loading}>刷新</Button>
      </div>

      {overview && (
        <Row gutter={16} style={{ marginBottom: 16 }}>
          <Col xs={12} md={6}>
            <Card>
              <Statistic
                title="总字数"
                value={overview.total_words}
                prefix={<FileTextOutlined />}
                suffix={<Text type="secondary" style={{ fontSize: 13 }}>字</Text>}
              />
            </Card>
          </Col>
          <Col xs={12} md={6}>
            <Card>
              <Statistic
                title="目标总字数"
                value={overview.total_target}
                prefix={<AimOutlined />}
                suffix={<Text type="secondary" style={{ fontSize: 13 }}>字</Text>}
              />
            </Card>
          </Col>
          <Col xs={12} md={6}>
            <Card>
              <Statistic
                title="目标覆盖率"
                value={overview.target_coverage}
                suffix="%"
                valueStyle={{ color: overview.target_coverage >= 100 ? "#52c41a" : "#faad14" }}
              />
            </Card>
          </Col>
          <Col xs={12} md={6}>
            <Card>
              <Statistic
                title="达标章节"
                value={overview.on_target_sections}
                prefix={<CheckCircleOutlined />}
                suffix={
                  <Text type="secondary" style={{ fontSize: 13 }}>
                    / {overview.total_sections} 章节，缺字 {overview.below_target_sections}
                  </Text>
                }
              />
            </Card>
          </Col>
        </Row>
      )}

      {overview && (overview.empty_sections > 0 || overview.below_target_sections > 0) && (
        <Card style={{ marginBottom: 16 }}>
          <Space direction="vertical" style={{ width: "100%" }}>
            {overview.empty_sections > 0 && (
              <Space>
                <WarningOutlined style={{ color: "#faad14" }} />
                <Text>
                  有 <Text strong type="warning">{overview.empty_sections}</Text> 个章节尚未生成内容
                </Text>
              </Space>
            )}
            {overview.below_target_sections > 0 && (
              <Space>
                <WarningOutlined style={{ color: "#faad14" }} />
                <Text>
                  有 <Text strong type="warning">{overview.below_target_sections}</Text> 个章节未达目标字数
                </Text>
              </Space>
            )}
          </Space>
        </Card>
      )}

      <Card
        title={<Space><FileTextOutlined /> 章节字数明细</Space>}
        extra={<Text type="secondary">点击目标字数输入框回车保存</Text>}
      >
        <Table
          rowKey="id"
          loading={loading}
          dataSource={items}
          columns={columns}
          pagination={{ pageSize: 20, showSizeChanger: true }}
          locale={{ emptyText: <Empty description="暂无章节数据" /> }}
          size="middle"
        />
      </Card>
    </div>
  );
}
