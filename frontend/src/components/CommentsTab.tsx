import { useCallback, useEffect, useState } from "react";
import {
  Avatar, Button, Card, Empty, Form, Input, List, Popconfirm, Space, Tag, Tooltip, Typography, message,
} from "antd";
import {
  CheckCircleOutlined, DeleteOutlined, MessageOutlined, ReloadOutlined, SendOutlined, UserOutlined,
} from "@ant-design/icons";
import { getComments, addComment, resolveComment, getAuthToken } from "../../api";
import type { Comment } from "../../api";
import { extractApiError } from "../../utils";

const { Text, Paragraph } = Typography;
const { TextArea } = Input;

export interface CommentsTabProps {
  projectId: number;
  sectionId: number;
}

/** 从 localStorage 读取当前用户名（与 LoginPage 的 user_name 一致） */
function getCurrentUser(): string {
  try {
    const token = getAuthToken();
    if (!token) return "anonymous";
    // JWT payload 中的 username（base64 解码）
    const payload = JSON.parse(atob(token.split(".")[1]));
    return payload.username || payload.sub || "anonymous";
  } catch {
    return "anonymous";
  }
}

export default function CommentsTab({ projectId, sectionId }: CommentsTabProps) {
  const [comments, setComments] = useState<Comment[]>([]);
  const [loading, setLoading] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [newComment, setNewComment] = useState("");
  const currentUser = getCurrentUser();

  const loadComments = useCallback(async () => {
    setLoading(true);
    try {
      const res = await getComments(projectId, sectionId);
      setComments(res.comments || []);
    } catch (err) {
      message.error(extractApiError(err) || "加载评论失败");
    } finally {
      setLoading(false);
    }
  }, [projectId, sectionId]);

  useEffect(() => {
    loadComments();
  }, [loadComments]);

  const handleAdd = useCallback(async () => {
    const content = newComment.trim();
    if (!content) {
      message.warning("请输入评论内容");
      return;
    }
    setSubmitting(true);
    try {
      await addComment(projectId, sectionId, content, currentUser);
      setNewComment("");
      await loadComments();
      message.success("评论已添加");
    } catch (err) {
      message.error(extractApiError(err) || "添加评论失败");
    } finally {
      setSubmitting(false);
    }
  }, [projectId, sectionId, currentUser, newComment, loadComments]);

  const handleResolve = useCallback(async (commentId: number) => {
    try {
      await resolveComment(projectId, sectionId, commentId, currentUser);
      await loadComments();
      message.success("评论已标记为已解决");
    } catch (err) {
      message.error(extractApiError(err) || "操作失败");
    }
  }, [projectId, sectionId, currentUser, loadComments]);

  return (
    <div style={{ padding: "12px 0" }}>
      <Space style={{ width: "100%", justifyContent: "space-between", marginBottom: 16 }}>
        <Space>
          <MessageOutlined />
          <Text strong>章节评论</Text>
          <Text type="secondary">({comments.length})</Text>
        </Space>
        <Button icon={<ReloadOutlined />} onClick={loadComments} loading={loading}>
          刷新
        </Button>
      </Space>

      <Card size="small" style={{ marginBottom: 16 }}>
        <Form layout="vertical" onFinish={handleAdd}>
          <Form.Item noStyle>
            <TextArea
              rows={3}
              placeholder="输入评论，按 Ctrl+Enter 提交..."
              value={newComment}
              onChange={(e) => setNewComment(e.target.value)}
              onPressEnter={(e) => {
                if (e.ctrlKey || e.metaKey) handleAdd();
              }}
            />
          </Form.Item>
          <Form.Item noStyle style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
            <Text type="secondary" style={{ fontSize: 12 }}>
              以 <Text code>{currentUser}</Text> 身份评论 · Ctrl+Enter 提交
            </Text>
            <Button
              type="primary"
              icon={<SendOutlined />}
              loading={submitting}
              onClick={handleAdd}
            >
              发表评论
            </Button>
          </Form.Item>
        </Form>
      </Card>

      {comments.length === 0 && !loading ? (
        <Empty description="暂无评论" />
      ) : (
        <List
          loading={loading}
          dataSource={comments}
          renderItem={(item) => (
            <List.Item
              key={item.id}
              actions={
                item.status === "resolved" ? [] : [
                  <Popconfirm
                    key="resolve"
                    title="确认标记为已解决？"
                    onConfirm={() => handleResolve(item.id)}
                    okText="确认"
                    cancelText="取消"
                  >
                    <Button
                      type="text"
                      size="small"
                      icon=<CheckCircleOutlined />
                    >
                      已解决
                    </Button>
                  </Popconfirm>
                ]
              }
            >
              <List.Item.Meta
                avatar={
                  <Avatar icon={<UserOutlined />} style={{ backgroundColor: item.status === "resolved" ? "#52c41a" : "#1677ff" }} />
                }
                title={
                  <Space>
                    <Text strong>{item.user_name || "anonymous"}</Text>
                    {item.status === "resolved" && <Tag color="green">已解决</Tag>}
                    <Text type="secondary" style={{ fontSize: 12 }}>
                      {new Date(item.created_at).toLocaleString("zh-CN")}
                    </Text>
                  </Space>
                }
                description={
                  <Paragraph style={{ margin: 0, whiteSpace: "pre-wrap", wordBreak: "break-word" }}>
                    {item.content}
                  </Paragraph>
                }
              />
            </List.Item>
          )}
        />
      )}
    </div>
  );
}