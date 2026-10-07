/** VersionHistory — 内容版本历史面板

展示当前章节的所有保存快照，支持一键回滚到任意历史版本。
集成到 SectionEditorPage 的右侧面板或独立 Modal 中使用。
*/
import React, { useEffect, useState, useCallback } from "react";
import {
  List, Button, Tag, Space, Typography, Spin, message, Popconfirm, Empty,
} from "antd";
import {
  HistoryOutlined,
  ReloadOutlined,
  DeleteOutlined,
  CheckCircleOutlined,
  ClockCircleOutlined,
} from "@ant-design/icons";
import {
  listContentVersions,
  restoreContentVersion,
  deleteContentVersion,
  saveContentVersion,
  type ContentVersion,
} from "../api";
import type { Section } from "../types";

const { Text, Paragraph } = Typography;

interface VersionHistoryProps {
  projectId: number;
  section: Section | null;
  currentContent: string;
  /** 每次保存时自动调用，返回版本号/标签（可选） */
  onSaveVersion?: (sectionId: number, content: string, label: string) => Promise<void>;
}

function VersionHistory({ projectId, section, currentContent, onSaveVersion }: VersionHistoryProps) {
  const [versions, setVersions] = useState<ContentVersion[]>([]);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [restoringId, setRestoringId] = useState<number | null>(null);

  const loadVersions = useCallback(async () => {
    if (!section?.id || projectId <= 0) return;
    setLoading(true);
    try {
      const data = await listContentVersions(projectId, section.id, 30);
      setVersions(data);
    } catch (e: any) {
      console.error("加载版本历史失败:", e);
    } finally {
      setLoading(false);
    }
  }, [projectId, section?.id]);

  useEffect(() => {
    void loadVersions();
  }, [loadVersions]);

  // 当内容变化且内容有实质改动时，自动保存版本
  const prevContentRef = React.useRef(currentContent);
  useEffect(() => {
    if (
      !section?.id ||
      !onSaveVersion ||
      currentContent === prevContentRef.current ||
      currentContent.length < 50
    ) return;
    // 防抖：内容变化 3 秒后才自动保存
    const timer = setTimeout(async () => {
      try {
        await onSaveVersion(section.id, currentContent, "自动保存");
        await loadVersions();
        message.success("已自动保存版本");
      } catch {
        // ignore
      }
      prevContentRef.current = currentContent;
    }, 3000);
    return () => clearTimeout(timer);
  }, [currentContent, section?.id, onSaveVersion, loadVersions]);

  const handleRestore = async (versionId: number) => {
    if (!section) return;
    setRestoringId(versionId);
    try {
      const result = await restoreContentVersion(projectId, versionId);
      // BUG-FIX 2026-09-15（中）：后端恢复成功返回 { message }，仅版本不存在时抛 404；
      // 旧代码误判 result.status（该字段根本不存在），导致恢复成功也被当作「恢复失败」。
      message.success(result.message || "已恢复到该版本");
      await loadVersions();
    } catch (e: any) {
      message.error(e?.message || "恢复失败");
    } finally {
      setRestoringId(null);
    }
  };

  const handleDelete = async (versionId: number) => {
    try {
      // BUG-FIX 2026-09-16（中）：后端删除成功返回的是 {status: "deleted", message}，
      // 永远不等于 "ok" —— 即使接口修好了 URL，列表也不会刷新，
      // 用户会以为没删掉而反复点击。成功即刷新。
      await deleteContentVersion(projectId, versionId);
      await loadVersions();
    } catch {
      message.error("删除失败");
    }
  };

  if (!section) {
    return (
      <div style={{ padding: 16, textAlign: "center", color: "#999" }}>
        <Empty description="请先选择章节" image={Empty.PRESENTED_IMAGE_SIMPLE} />
      </div>
    );
  }

  return (
    <div>
      <Space style={{ marginBottom: 12 }}>
        <HistoryOutlined />
        <Text strong>版本历史</Text>
        <Tag color="blue">{versions.length} 个</Tag>
      </Space>

      {loading ? (
        <div style={{ textAlign: "center", padding: 24 }}>
          <Spin />
        </div>
      ) : versions.length === 0 ? (
        <Empty description="暂无版本记录" image={Empty.PRESENTED_IMAGE_SIMPLE} />
      ) : (
        <List
          size="small"
          dataSource={versions}
          renderItem={(item) => (
            <List.Item
              style={{
                padding: "8px 0",
                borderBottom: "1px solid #f0f0f0",
                alignItems: "flex-start",
              }}
              actions={[
                <Popconfirm
                  key="del"
                  title="确定删除此版本？"
                  onConfirm={() => void handleDelete(item.id)}
                  okText="删除"
                  cancelText="取消"
                >
                  <Button type="text" size="small" danger icon={<DeleteOutlined />} />
                </Popconfirm>,
                <Button
                  key="restore"
                  type="text"
                  size="small"
                  icon={<ReloadOutlined />}
                  loading={restoringId === item.id}
                  onClick={() => void handleRestore(item.id)}
                >
                  恢复
                </Button>,
              ]}
            >
              <List.Item.Meta
                title={
                  <Space size={4}>
                    {item.label && <Tag color="geekblue">{item.label}</Tag>}
                    <Text strong style={{ fontSize: 13 }}>{item.label || `版本 #${item.id}`}</Text>
                  </Space>
                }
                description={
                  <Space direction="vertical" size={2} style={{ width: "100%" }}>
                    <Paragraph
                      ellipsis={{ rows: 2 }}
                      style={{ margin: 0, fontSize: 12, color: "#888" }}
                    >
                      {item.content_preview || "（空内容）"}
                    </Paragraph>
                    <Space size={8}>
                      <Text type="secondary" style={{ fontSize: 11 }}>
                        <ClockCircleOutlined /> {item.created_at}
                      </Text>
                      <Text type="secondary" style={{ fontSize: 11 }}>
                        {item.content_length} 字
                      </Text>
                      {item.label === "AI生成" && (
                        <Tag color="green" icon={<CheckCircleOutlined />} style={{ fontSize: 11 }}>AI</Tag>
                      )}
                    </Space>
                  </Space>
                }
              />
            </List.Item>
          )}
        />
      )}
    </div>
  );
}

export default React.memo(VersionHistory);
