import { useEffect, useState } from "react";
import {
  Button, Card, Table, Tag, Typography, message, Modal, Form, Input,
  Switch, Space, Popconfirm, Tooltip, Alert, Divider, Statistic, Select, InputNumber,
} from "antd";
import {
  PlusOutlined, DeleteOutlined, EyeOutlined,
  EyeInvisibleOutlined, ReloadOutlined, ApiOutlined,
  CheckCircleOutlined, CloseCircleOutlined, ClockCircleOutlined, CopyOutlined,
} from "@ant-design/icons";
import {
  createApiKey, listApiKeys, revokeApiKey, getApiKeyUsage,
  type ApiKeyInfo, type ApiKeyListEntry,
} from "../api";

const { Title, Text, Paragraph } = Typography;

const PERMISSION_OPTIONS = [
  { label: "项目读取", value: "projects:read" },
  { label: "项目写入", value: "projects:write" },
  { label: "章节读取", value: "sections:read" },
  { label: "章节写入", value: "sections:write" },
  { label: "导出读取", value: "export:read" },
  { label: "导出写入", value: "export:write" },
  { label: "文件上传", value: "files:upload" },
  { label: "文件删除", value: "files:delete" },
  { label: "全部权限", value: "*:*" },
];

export default function ApiKeyManagementPage() {
  const [keys, setKeys] = useState<ApiKeyListEntry[]>([]);
  const [loading, setLoading] = useState(false);
  const [createModalOpen, setCreateModalOpen] = useState(false);
  const [form] = Form.useForm();
  const [usageData, setUsageData] = useState<Record<number, any>>({});
  const [creating, setCreating] = useState(false);
  const [newKeyInfo, setNewKeyInfo] = useState<ApiKeyInfo | null>(null);
  const [copiedKeyId, setCopiedKeyId] = useState<number | null>(null);

  const fetchKeys = async () => {
    setLoading(true);
    try {
      const res = await listApiKeys();
      setKeys(res.keys || []);
    } catch {
      // error handled by interceptor
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchKeys();
  }, []);

  const handleCreate = async () => {
    try {
      const values = await form.validateFields();
      setCreating(true);
      const permList = PERMISSION_OPTIONS.filter(
        (p) => values.permissions?.includes(p.value),
      ).map((p) => p.value);
      if (permList.length === 0) permList.push("projects:read");
      const res = await createApiKey(values.name, permList, values.expiresDays || 365);
      setNewKeyInfo(res);
      form.resetFields();
      message.success("API Key 创建成功");
      fetchKeys();
    } catch {
      // error handled by interceptor
    } finally {
      setCreating(false);
    }
  };

  const handleRevoke = async (keyId: number) => {
    try {
      await revokeApiKey(keyId);
      message.success("API Key 已撤销");
      fetchKeys();
    } catch {
      // error handled by interceptor
    }
  };

  const handleCopyKey = async (key: string, keyId: number) => {
    try {
      await navigator.clipboard.writeText(key);
      setCopiedKeyId(keyId);
      setTimeout(() => setCopiedKeyId(null), 2000);
      message.success("已复制到剪贴板");
    } catch {
      message.error("复制失败，请手动选择复制");
    }
  };

  const handleFetchUsage = async (keyId: number) => {
    try {
      const res = await getApiKeyUsage(keyId, 7);
      setUsageData((prev) => ({ ...prev, [keyId]: res.usage }));
    } catch {
      // ignore
    }
  };

  const columns = [
    {
      title: "名称",
      dataIndex: "name",
      key: "name",
      render: (text: string) => <Text strong>{text}</Text>,
    },
    {
      title: "密钥前缀",
      dataIndex: "prefix",
      key: "prefix",
      render: (prefix: string) => (
        <CodeBox text={prefix} onCopy={() => {}} />
      ),
    },
    {
      title: "权限",
      dataIndex: "permissions",
      key: "permissions",
      render: (perms: string | string[] | undefined) => {
        const permsList = Array.isArray(perms) ? perms : typeof perms === "string" ? JSON.parse(perms || "[]") : [];
        return (
          <Space wrap>
            {permsList.map((p: string) => (
              <Tag key={p} color="blue">{p}</Tag>
            ))}
          </Space>
        );
      },
    },
    {
      title: "状态",
      dataIndex: "is_active",
      key: "is_active",
      render: (active: boolean) => (
        <Tag color={active ? "green" : "red"}>
          {active ? <CheckCircleOutlined /> : <CloseCircleOutlined />}
          {active ? "活跃" : "已撤销"}
        </Tag>
      ),
    },
    {
      title: "使用次数",
      dataIndex: "usage_count",
      key: "usage_count",
      render: (count: number) => <Text>{count || 0}</Text>,
    },
    {
      title: "过期时间",
      dataIndex: "expires_at",
      key: "expires_at",
      render: (val: string) => (
        val ? <Text type="secondary">{val}</Text> : <Text type="secondary">永不过期</Text>
      ),
    },
    {
      title: "操作",
      key: "actions",
      render: (_: any, record: ApiKeyListEntry) => (
        <Space>
          <Button
            size="small"
            icon={<ClockCircleOutlined />}
            onClick={() => handleFetchUsage(record.id)}
            disabled={!record.is_active}
          >
            统计
          </Button>
          <Popconfirm
            title="确定撤销此 API Key？"
            onConfirm={() => handleRevoke(record.id)}
            disabled={record.is_active === false}
          >
            <Button
              size="small"
              danger
              icon={<DeleteOutlined />}
              disabled={record.is_active === false}
            >
              撤销
            </Button>
          </Popconfirm>
        </Space>
      ),
    },
  ];

  return (
    <div style={{ maxWidth: 1200, margin: "0 auto" }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 24 }}>
        <div>
          <Title level={3}>
            <ApiOutlined /> API Key 管理
          </Title>
          <Text type="secondary">管理第三方集成的 API 访问密钥</Text>
        </div>
        <Space>
          <Button icon={<ReloadOutlined />} onClick={fetchKeys}>
            刷新
          </Button>
          <Button
            type="primary"
            icon={<PlusOutlined />}
            onClick={() => {
              setCreateModalOpen(true);
              setNewKeyInfo(null);
            }}
          >
            创建 API Key
          </Button>
        </Space>
      </div>

      <Alert
        message="使用说明"
        description={
          <Paragraph style={{ marginBottom: 0 }}>
            API Key 用于第三方系统集成。使用时需在请求头中添加{" "}
            <CodeInline text="X-API-Key: sk-xxxxx" />。
            每个 Key 默认限制 100 次/小时请求。创建后请立即保存密钥，后续无法再次查看完整密钥。
          </Paragraph>
        }
        type="info"
        showIcon
        style={{ marginBottom: 24 }}
      />

      {Object.values(usageData).length > 0 && (
        <Card title="最近使用统计（7天）" style={{ marginBottom: 24 }}>
          <Table
            dataSource={Object.entries(usageData).map(([id, usage]) => ({
              key: id,
              "API Key ID": id,
              "总使用次数": usage.total_usage || 0,
              "日均使用": usage.daily_breakdown?.length
                ? Math.round((usage.total_usage || 0) / Math.max(usage.days || 1, 1))
                : 0,
            }))}
            columns={[
              { title: "API Key ID", dataIndex: "API Key ID", key: "API Key ID" },
              { title: "总使用次数", dataIndex: "总使用次数", key: "总使用次数" },
              { title: "日均使用", dataIndex: "日均使用", key: "日均使用" },
            ]}
            pagination={false}
            size="small"
          />
        </Card>
      )}

      <Table
        columns={columns}
        dataSource={keys}
        loading={loading}
        rowKey="id"
        pagination={{ pageSize: 10 }}
      />

      <Modal
        title={newKeyInfo ? "API Key 创建成功" : "创建新的 API Key"}
        open={createModalOpen}
        forceRender
        onCancel={() => {
          setCreateModalOpen(false);
          setNewKeyInfo(null);
        }}
        footer={
          newKeyInfo
            ? [
                <Button
                  key="back"
                  type="primary"
                  onClick={() => {
                    setCreateModalOpen(false);
                    setNewKeyInfo(null);
                  }}
                >
                  返回列表
                </Button>,
              ]
            : [
                <Button key="cancel" onClick={() => setCreateModalOpen(false)}>
                  取消
                </Button>,
                <Button
                  key="submit"
                  type="primary"
                  loading={creating}
                  onClick={handleCreate}
                >
                  创建
                </Button>,
              ]
        }
      >
        {newKeyInfo ? (
          <div>
            <Alert
              message="警告"
              description="请妥善保存以下 API Key，后续将无法再次查看完整密钥！"
              type="warning"
              showIcon
              style={{ marginBottom: 16 }}
            />
            <Card>
              <Statistic
                title="API Key"
                value={newKeyInfo.api_key}
                valueStyle={{ fontSize: 16 }}
                suffix={
                  <Button
                    size="small"
                    icon={copiedKeyId === newKeyInfo.key_id ? <CheckCircleOutlined /> : <CopyOutlined />}
                    onClick={() => handleCopyKey(newKeyInfo.api_key, newKeyInfo.key_id)}
                  />
                }
              />
              <Divider style={{ margin: "12px 0" }} />
              <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
                <div>
                  <Text type="secondary">名称</Text>
                  <br />
                  <Text strong>{newKeyInfo.name}</Text>
                </div>
                <div>
                  <Text type="secondary">过期时间</Text>
                  <br />
                  <Text>{newKeyInfo.expires_at}</Text>
                </div>
              </div>
            </Card>
          </div>
        ) : (
          <Form form={form} layout="vertical" initialValues={{ expiresDays: 365, permissions: ["projects:read", "sections:read", "export:read"] }}>
            <Form.Item
              name="name"
              label="Key 名称"
              rules={[{ required: true, message: "请输入 Key 名称" }]}
              initialValue="默认密钥"
            >
              <Input placeholder="用于识别此 Key 的名称" />
            </Form.Item>
            <Form.Item
              name="permissions"
              label="权限"
              rules={[{ required: true, message: "请至少选择一个权限" }]}
            >
              <Select
                mode="multiple"
                options={PERMISSION_OPTIONS}
                placeholder="选择权限"
              />
            </Form.Item>
            <Form.Item name="expiresDays" label="有效期（天）">
              <InputNumber min={1} max={3650} style={{ width: "100%" }} />
            </Form.Item>
          </Form>
        )}
      </Modal>
      {/* 保持 Form 在树中以避免 antd useForm 警告 */}
      <Form form={form} style={{ display: "none" }} />
    </div>
  );
}

function CodeInline({ text }: { text: string }) {
  return (
    <code
      style={{
        background: "#f5f5f5",
        padding: "2px 6px",
        borderRadius: 4,
        fontSize: "0.9em",
      }}
    >
      {text}
    </code>
  );
}

function CodeBox({ text, onCopy }: { text: string; onCopy: () => void }) {
  return (
    <Space>
      <code style={{ background: "#f5f5f5", padding: "2px 6px", borderRadius: 4 }}>
        {text}
      </code>
      <Tooltip title="复制">
        <Button size="small" icon={<CopyOutlined />} onClick={onCopy} />
      </Tooltip>
    </Space>
  );
}
