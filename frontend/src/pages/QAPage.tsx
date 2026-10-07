import { useEffect, useRef, useState } from "react";
import { useParams, useNavigate } from "react-router-dom";
import {
  Alert, Avatar, Button, Card, Empty, Input, List, Space, Spin, Tag, Typography, message,
} from "antd";
import {
  SendOutlined, RobotOutlined, UserOutlined, ClearOutlined, LinkOutlined, ArrowLeftOutlined,
} from "@ant-design/icons";
import { qaChat, getQaHistory, clearQaHistory } from "../api";
import type { QaMessage } from "../api";
import { extractApiError } from "../utils";

const { Text, Paragraph } = Typography;

export default function QAPage() {
  const { id } = useParams<{ id: string }>();
  const projectId = Number(id);
  const navigate = useNavigate();

  const [messages, setMessages] = useState<QaMessage[]>([]);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const [sources, setSources] = useState<string[]>([]);
  const [projectName, setProjectName] = useState("");
  const listRef = useRef<HTMLDivElement>(null);

  // 加载历史
  useEffect(() => {
    (async () => {
      try {
        const res = await getQaHistory(projectId);
        if (res.items && res.items.length > 0) {
          setMessages(res.items);
        }
      } catch {
        // ignore
      }
    })();
  }, [projectId]);

  // 自动滚动到底部
  useEffect(() => {
    if (listRef.current) {
      listRef.current.scrollTop = listRef.current.scrollHeight;
    }
  }, [messages, loading]);

  const handleSend = async () => {
    const question = input.trim();
    if (!question) return;
    if (loading) return;

    const newMessages: QaMessage[] = [
      ...messages,
      { role: "user", content: question },
    ];
    setMessages(newMessages);
    setInput("");
    setLoading(true);
    setSources([]);

    try {
      const res = await qaChat(projectId, question, messages);
      setMessages([...newMessages, { role: "assistant", content: res.answer }]);
      setSources(res.sources || []);
      if (res.project_name) setProjectName(res.project_name);
    } catch (e) {
      message.error(extractApiError(e));
      setMessages(newMessages);
    } finally {
      setLoading(false);
    }
  };

  const handleClear = async () => {
    try {
      await clearQaHistory(projectId);
      setMessages([]);
      setSources([]);
      message.success("对话历史已清空");
    } catch (e) {
      message.error(extractApiError(e));
    }
  };

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%", maxWidth: 960, margin: "0 auto" }}>
      <div style={{ marginBottom: 16, display: "flex", alignItems: "center", justifyContent: "space-between" }}>
        <Space>
          <Button icon={<ArrowLeftOutlined />} onClick={() => navigate(`/projects/${projectId}/sections`)}>
            返回正文
          </Button>
          <Typography.Title level={4} style={{ margin: 0 }}>
            <RobotOutlined /> 智能问答
          </Typography.Title>
          {projectName && <Tag color="blue">{projectName}</Tag>}
        </Space>
        <Button icon={<ClearOutlined />} onClick={handleClear} disabled={messages.length === 0}>
          清空对话
        </Button>
      </div>

      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 16 }}
        message="基于当前项目的招标文件与已生成章节内容回答您的标书相关问题。"
      />

      <Card
        style={{ flex: 1, display: "flex", flexDirection: "column", overflow: "hidden" }}
        styles={{ body: { flex: 1, overflow: "hidden", display: "flex", flexDirection: "column", padding: 0 } }}
      >
        <div ref={listRef} style={{ flex: 1, overflow: "auto", padding: 16 }}>
          {messages.length === 0 && !loading ? (
            <Empty
              style={{ marginTop: 80 }}
              description={
                <Text type="secondary">
                  尝试提问，例如：<br />"本项目工期要求是什么？"<br />"第一章技术方案的写作重点有哪些？"
                </Text>
              }
            />
          ) : (
            <List
              dataSource={messages}
              renderItem={(msg, idx) => (
                <List.Item style={{ border: "none", padding: "8px 0", justifyContent: msg.role === "user" ? "flex-end" : "flex-start" }}>
                  <div style={{ maxWidth: "85%", display: "flex", gap: 8, flexDirection: msg.role === "user" ? "row-reverse" : "row" }}>
                    <Avatar
                      icon={msg.role === "user" ? <UserOutlined /> : <RobotOutlined />}
                      style={{ background: msg.role === "user" ? "#1677ff" : "#52c41a", flexShrink: 0 }}
                    />
                    <div
                      style={{
                        background: msg.role === "user" ? "#1677ff" : "#f5f5f5",
                        color: msg.role === "user" ? "#fff" : "rgba(0,0,0,0.88)",
                        borderRadius: 8,
                        padding: "8px 12px",
                        whiteSpace: "pre-wrap",
                      }}
                    >
                      {msg.content}
                      {msg.role === "assistant" && idx === messages.length - 1 && sources.length > 0 && (
                        <div style={{ marginTop: 8, borderTop: "1px solid #d9d9d9", paddingTop: 6 }}>
                          <Text type="secondary" style={{ fontSize: 12 }}>
                            <LinkOutlined /> 参考章节：
                          </Text>
                          <Space size={[4, 4]} wrap style={{ marginTop: 4 }}>
                            {sources.map((s, i) => (
                              <Tag key={i} color="blue" style={{ fontSize: 12 }}>{s}</Tag>
                            ))}
                          </Space>
                        </div>
                      )}
                    </div>
                  </div>
                </List.Item>
              )}
            />
          )}
          {loading && (
            <div style={{ display: "flex", gap: 8, padding: "8px 0" }}>
              <Avatar icon={<RobotOutlined />} style={{ background: "#52c41a" }} />
              <div style={{ background: "#f5f5f5", borderRadius: 8, padding: "8px 12px" }}>
                <Spin size="small" /> <Text type="secondary" style={{ fontSize: 13 }}>正在思考中...</Text>
              </div>
            </div>
          )}
        </div>

        <div style={{ padding: 16, borderTop: "1px solid #f0f0f0", display: "flex", gap: 8 }}>
          <Input.TextArea
            value={input}
            onChange={(e) => setInput(e.target.value)}
            placeholder="请输入您的问题，Enter 发送，Shift+Enter 换行"
            autoSize={{ minRows: 1, maxRows: 4 }}
            onPressEnter={(e) => {
              if (!e.shiftKey) {
                e.preventDefault();
                handleSend();
              }
            }}
            disabled={loading}
          />
          <Button
            type="primary"
            icon={<SendOutlined />}
            onClick={handleSend}
            loading={loading}
            style={{ height: "auto" }}
          >
            发送
          </Button>
        </div>
      </Card>
    </div>
  );
}
