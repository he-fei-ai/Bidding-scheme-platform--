import React from "react";
import { Progress, List, Tag, Typography, Space } from "antd";
import { CloseCircleFilled, LoadingOutlined, SaveOutlined, StopOutlined, EditOutlined, AuditOutlined, ExpandOutlined, FileSearchOutlined } from "@ant-design/icons";

const { Text } = Typography;

export interface ProgressItem {
  section_id: number;
  title: string;
  status: string;
  progress: number;
  message: string;
  word_count?: number;
  saved?: boolean;
}

interface ProgressPanelProps {
  items: ProgressItem[];
  total: number;
  success: number;
  failed: number;
  isRunning: boolean;
  cancelled?: number;
}

const PHASE_CONFIG: Record<string, { icon: React.ReactNode; color: string; text: string }> = {
  planning: { icon: <FileSearchOutlined style={{ color: "#722ed1" }} />, color: "purple", text: "编排中" },
  generating: { icon: <LoadingOutlined style={{ color: "#1677ff" }} />, color: "processing", text: "生成中" },
  rewriting: { icon: <LoadingOutlined style={{ color: "#fa8c16" }} />, color: "orange", text: "重写中" },
  cached: { icon: <SaveOutlined style={{ color: "#13c2c2" }} />, color: "cyan", text: "缓存命中" },
  expanding: { icon: <ExpandOutlined style={{ color: "#fa8c16" }} />, color: "orange", text: "扩写中" },
  auditing: { icon: <AuditOutlined style={{ color: "#13c2c2" }} />, color: "cyan", text: "审计中" },
  completed: { icon: <SaveOutlined style={{ color: "#52c41a" }} />, color: "green", text: "已保存" },
  failed: { icon: <CloseCircleFilled style={{ color: "#ff4d4f" }} />, color: "red", text: "失败" },
  cancelled: { icon: <StopOutlined style={{ color: "#999" }} />, color: "default", text: "已取消" },
};

export default React.memo(function ProgressPanel({ items, total, success, failed, isRunning, cancelled = 0 }: ProgressPanelProps) {
  const done = success + failed + cancelled;
  const percent = total > 0 ? Math.round((done / total) * 100) : 0;

  // BUG-F-05 修复：计算整体进度时纳入生成中的章节进度
  // 原代码仅基于完成数/总数，生成中的章节被忽略导致进度条长时间不动
  const generatingItems = items.filter((i) => i.status === "generating" || i.status === "rewriting");
  const generatingProgress = generatingItems.length > 0
    ? Math.round(generatingItems.reduce((acc, i) => acc + (i.progress || 0), 0) / total)
    : 0;
  const effectivePercent = total > 0 && generatingProgress > 0
    ? Math.min(100, percent + generatingProgress)
    : percent;

  const statusIcon = (status: string) => {
    return PHASE_CONFIG[status]?.icon || null;
  };

  const statusColor = (status: string) => {
    return PHASE_CONFIG[status]?.color || "default";
  };

  const statusText = (status: string) => {
    return PHASE_CONFIG[status]?.text || status;
  };

  return (
    <div>
      <div style={{ display: "flex", alignItems: "center", gap: 4, marginBottom: 4, flexWrap: "wrap" }}>
        <Tag color="blue" style={{ fontSize: 11, lineHeight: "16px", padding: "0 4px", marginRight: 2 }}>{total}总</Tag>
        <Tag color="green" style={{ fontSize: 11, lineHeight: "16px", padding: "0 4px", marginRight: 2 }}>{success}存</Tag>
        {failed > 0 && <Tag color="red" style={{ fontSize: 11, lineHeight: "16px", padding: "0 4px", marginRight: 2 }}>{failed}败</Tag>}
        {cancelled > 0 && <Tag style={{ fontSize: 11, lineHeight: "16px", padding: "0 4px", marginRight: 2 }}>{cancelled}取</Tag>}
        {isRunning && <Tag color="processing" style={{ fontSize: 11, lineHeight: "16px", padding: "0 4px" }}>生成中</Tag>}
      </div>
      <Progress
        percent={effectivePercent}
        size="small"
        strokeWidth={6}
        status={isRunning ? "active" : (failed > 0 ? "exception" : "success")}
      />
      {items.length > 0 && (
      <div style={{ maxHeight: 100, overflow: "auto", marginTop: 4 }}>
        <List
          size="small"
          dataSource={items}
          renderItem={(item) => (
            <List.Item style={{ padding: "1px 0" }}>
              <Space size={4} style={{ width: "100%", justifyContent: "space-between" }}>
                <Space size={4}>
                  {statusIcon(item.status)}
                  <Tag color={statusColor(item.status)} style={{ minWidth: 40, textAlign: "center", fontSize: 11, lineHeight: "14px", padding: "0 3px", margin: 0 }}>
                    {statusText(item.status)}
                  </Tag>
                  <Text
                    ellipsis
                    style={{
                      maxWidth: 180,
                      fontSize: 11,
                      textDecoration: item.status === "failed" ? "line-through" : "none",
                      color: item.status === "failed" ? "#ff4d4f" : item.status === "cancelled" ? "#999" : undefined,
                    }}
                  >
                    {item.title || `章节 #${item.section_id}`}
                  </Text>
                </Space>
                <Space size={4}>
                  {(item.status === "generating" || item.status === "rewriting") && item.progress > 0 && (
                    <Text type="secondary" style={{ fontSize: 11 }}>{item.progress}%</Text>
                  )}
                  {item.word_count != null && item.word_count > 0 && (
                    <Text type="secondary" style={{ fontSize: 11 }}>{item.word_count}字</Text>
                  )}
                </Space>
              </Space>
            </List.Item>
          )}
          locale={{ emptyText: null }}
        />
      </div>
      )}
    </div>
  );
});