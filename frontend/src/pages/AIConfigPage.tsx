/**
 * AI 配置中心 — 增强版
 *
 * 功能模块：
 *  1. AI Profile 管理（多配置：目录生成/正文撰写/分析/快速草稿）
 *  2. 用量统计仪表盘（Token 用量/成本/成功率/响应时间）
 *  3. Provider 健康度面板（7 天健康评分）
 *  4. 预算追踪（每日 Token 预算）
 *  5. 配置导入/导出
 */
import { useEffect, useState, useCallback, useRef } from "react";
import {
  Button, Card, Form, Input, InputNumber, Select, Slider, message, Spin,
  Typography, Alert, Space, Tag, Tooltip, Descriptions, Tabs, Switch,
  Modal, List, Progress, Statistic, Row, Col, Divider, Badge,
} from "antd";
import {
  SaveOutlined, ApiOutlined, ReloadOutlined, CheckCircleOutlined,
  CloseCircleOutlined, ThunderboltOutlined, InfoCircleOutlined,
  PictureOutlined, SettingOutlined, BulbOutlined, FileImageOutlined,
  DashboardOutlined, BarChartOutlined,
  ImportOutlined, ExportOutlined, PlusOutlined, DeleteOutlined,
  EditOutlined, RocketOutlined,
} from "@ant-design/icons";
import {
  getImageModelConfig, saveImageModelConfig, testImageConnection, getImageModelProviders, fetchKnownProviders,
  // Profile APIs
  fetchAIProfiles, getAIDefaultProfile, createAIPProfile, updateAIPProfile,
  deleteAIPProfile, setAIDefaultProfile, applyAIPProfile,
  exportAIPProfiles, importAIPProfiles,
  // Usage & Health
  getUsageDashboard, getHealthDashboard, getUsageTrend, truncateUsageStats,
} from "../api";
import { extractApiError } from "../utils";
import type {
  KnownProvider, AIPProfile,
  UsageSummary, HealthProvider, DailyTrend, BudgetStatus,
} from "../types";

const PROFILE_PRESETS = [
  {
    name: "outline",
    display_name: "目录生成（精准）",
    description: "生成投标文件目录：低温度、长上下文、输出稳定",
    temperature: 0.3, max_tokens: 8192, concurrency: 3, task_type: "outline",
  },
  {
    name: "content",
    display_name: "正文撰写（平衡）",
    description: "正文内容生成：中等温度、平衡创意与准确性",
    temperature: 0.7, max_tokens: 4096, concurrency: 6, task_type: "content",
  },
  {
    name: "analysis",
    display_name: "分析任务（JSON）",
    description: "招标分析/评分点提取：强制 JSON 输出，温度极低",
    temperature: 0.1, max_tokens: 4096, concurrency: 2, task_type: "analysis", json_mode: true,
  },
  {
    name: "quick",
    display_name: "快速草稿（高速）",
    description: "快速草稿/预览：低延迟，允许一定容错",
    temperature: 0.9, max_tokens: 2048, concurrency: 12, task_type: "quick",
  },
];

// ═══════════════════════════════════════════════════════════════
// 子组件：Profile 管理面板
// ═══════════════════════════════════════════════════════════════
function ProfileManager({
  profiles, activeProfile, onRefresh,
}: {
  profiles: AIPProfile[];
  activeProfile: string;
  onRefresh: () => void;
}) {
  const [visible, setVisible] = useState(false);
  const [editing, setEditing] = useState<AIPProfile | null>(null);
  const [form] = Form.useForm();
  const [saving, setSaving] = useState(false);
  const [providers, setProviders] = useState<KnownProvider[]>([]);
  // 保存待填充的表单数据，避免在 Form 挂载前调用 form 方法
  const [pendingFormData, setPendingFormData] = useState<Record<string, any> | null>(null);

  // 修复：Tabs 用 destroyOnHidden，切走时组件会卸载，需要防止卸载后 setState
  useEffect(() => {
    let alive = true;
    fetchKnownProviders().then((list) => { if (alive) setProviders(list); }).catch(() => {});
    return () => { alive = false; };
  }, []);

  // 关键修复：Modal 打开后再初始化 Form 数据，避免 useForm "未连接到 Form 元素" 警告
  useEffect(() => {
    if (visible && pendingFormData) {
      form.resetFields();
      form.setFieldsValue(pendingFormData);
      setPendingFormData(null);
    }
  }, [visible, pendingFormData, form]);

  const openCreate = () => {
    setEditing(null);
    setPendingFormData({
      temperature: 0.7, max_tokens: 16384, concurrency: 4, timeout: 120,
      json_mode: false, is_default: false,
    });
    setVisible(true);
  };

  const openEdit = (p: AIPProfile) => {
    setEditing(p);
    setPendingFormData(p as unknown as Record<string, any>);
    setVisible(true);
  };

  const handleSave = async () => {
    try {
      const values = await form.validateFields();
      setSaving(true);
      if (editing) {
        await updateAIPProfile(editing.name, values);
        message.success(`Profile "${values.display_name || values.name}" 已更新`);
      } else {
        await createAIPProfile(values);
        message.success(`Profile "${values.display_name || values.name}" 已创建`);
      }
      setVisible(false);
      onRefresh();
    } catch (err: any) {
      if (err?.errorFields) return;
      message.error(extractApiError(err));
    } finally {
      setSaving(false);
    }
  };

  const handleDelete = (p: AIPProfile) => {
    if (p.is_default) {
      message.warning("不能删除默认 Profile");
      return;
    }
    Modal.confirm({
      title: `删除 Profile "${p.display_name}"?`,
      content: "删除后将无法恢复，当前正在使用的 Profile 也会失效",
      okButtonProps: { danger: true },
      onOk: async () => {
        // 修复：此前 onOk 无异常捕获，删除失败会产生 unhandled rejection 且静默无提示
        try {
          await deleteAIPProfile(p.name);
          message.success("已删除");
          onRefresh();
        } catch (err: any) {
          message.error(extractApiError(err));
        }
      },
    });
  };

  const handleSetDefault = async (p: AIPProfile) => {
    try {
      await setAIDefaultProfile(p.name);
      message.success(`已切换默认 Profile 为 "${p.display_name}"`);
      onRefresh();
    } catch (err: any) {
      message.error(extractApiError(err));
    }
  };

  const handleApply = async (p: AIPProfile) => {
    try {
      await applyAIPProfile(p.name);
      message.success(`已应用 Profile "${p.display_name}" 到当前配置`);
      onRefresh();
    } catch (err: any) {
      message.error(extractApiError(err));
    }
  };

  const providerOptions = providers.map(p => ({
    value: p.name, label: p.is_default ? `${p.name}（默认）` : p.name,
  }));

  return (
    <>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 16 }}>
        <Typography.Text strong>AI Profile 管理</Typography.Text>
        <Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>
          新建 Profile
        </Button>
      </div>

      <List
        dataSource={profiles}
        locale={{ emptyText: "暂无 AI Profile，点击右上角「新建 Profile」创建" }}
        renderItem={(p) => {
          const isActive = p.name === activeProfile;
          return (
            <List.Item
              style={{
                background: isActive ? "#e6f4ff" : undefined,
                borderRadius: 8,
                border: isActive ? "1px solid #1890ff" : undefined,
                marginBottom: 8,
                padding: "12px 16px",
              }}
              extra={
                <Space>
                  {isActive && <Tag color="blue">当前激活</Tag>}
                  {p.is_default && <Tag color="green">默认</Tag>}
                  {p.json_mode && <Tag color="purple">JSON</Tag>}
                  <Tooltip title="设为默认"><Button size="small" icon={<SettingOutlined />} onClick={() => handleSetDefault(p)} /></Tooltip>
                  <Tooltip title="应用到当前配置"><Button size="small" icon={<SaveOutlined />} onClick={() => handleApply(p)} /></Tooltip>
                  <Tooltip title="编辑"><Button size="small" icon={<EditOutlined />} onClick={() => openEdit(p)} /></Tooltip>
                  <Tooltip title="删除"><Button size="small" danger icon={<DeleteOutlined />} onClick={() => handleDelete(p)} /></Tooltip>
                </Space>
              }
            >
              <List.Item.Meta
                title={
                  <Space>
                    <span>{p.display_name || p.name}</span>
                    {p.task_type && <Tag>{p.task_type}</Tag>}
                  </Space>
                }
                description={
                  <Space direction="vertical" size={2} style={{ width: "100%" }}>
                    <Typography.Text type="secondary" style={{ fontSize: 12 }}>{p.description || "暂无描述"}</Typography.Text>
                    <Space size="middle" style={{ fontSize: 12 }}>
                      <span>模型: <Tag>{p.model}</Tag></span>
                      <span>温度: <Tag>{p.temperature}</Tag></span>
                      <span>MaxTokens: <Tag>{p.max_tokens}</Tag></span>
                      <span>并发: <Tag>{p.concurrency}</Tag></span>
                      {p.token_budget_daily > 0 && <span>日预算: <Tag>{p.token_budget_daily.toLocaleString()}</Tag></span>}
                    </Space>
                  </Space>
                }
              />
            </List.Item>
          );
        }}
      />

      <Modal
        title={editing ? `编辑 Profile: ${editing.display_name}` : "新建 AI Profile"}
        open={visible}
        onOk={handleSave}
        onCancel={() => setVisible(false)}
        confirmLoading={saving}
        width={640}
        okText="保存"
        cancelText="取消"
        forceRender
      >
        <Form form={form} layout="vertical" style={{ marginTop: 16 }}>
          {!editing && (
            <Form.Item
              label="官方预设"
              tooltip="选择预设可一键填充调参组合（名称/描述/温度/并发/任务类型），填充后仍可手动调整"
            >
              <Select
                allowClear
                placeholder="从预设创建（可选）"
                options={PROFILE_PRESETS.map((p) => ({ value: p.name, label: p.display_name }))}
                onChange={(v) => {
                  const preset = PROFILE_PRESETS.find((p) => p.name === v);
                  if (!preset) return;
                  form.setFieldsValue({ ...preset });
                }}
              />
            </Form.Item>
          )}

          <Row gutter={16}>
            <Col span={12}>
              <Form.Item name="name" label="Profile 名称" rules={[{ required: true }]}>
                <Input placeholder="如: outline, content" disabled={!!editing} />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item name="display_name" label="显示名称">
                <Input placeholder="如: 目录生成（精准）" />
              </Form.Item>
            </Col>
          </Row>
          <Form.Item name="description" label="描述">
            <Input.TextArea rows={2} placeholder="描述此 Profile 的用途..." />
          </Form.Item>

          <Form.Item name="provider_name" label="API 厂商">
            <Select options={providerOptions} showSearch />
          </Form.Item>

          <Form.Item name="model" label="模型名称">
            <Input placeholder="如: gpt-4o, agnes-2.0-flash" />
          </Form.Item>

          <Form.Item name="base_url" label="API Base URL">
            <Input placeholder="https://api.example.com/v1" />
          </Form.Item>

          <Form.Item name="api_key" label="API Key">
            <Input.Password placeholder="输入 API Key（留空则保持原值）" />
          </Form.Item>

          <Divider orientation="left">生成参数</Divider>

          <Row gutter={16}>
            <Col span={12}>
              <Form.Item name="temperature" label="Temperature">
                <Slider min={0} max={2} step={0.1} marks={{ 0: "精确", 0.7: "平衡", 1: "创意", 2: "随机" }} />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item name="max_tokens" label="Max Tokens">
                <InputNumber min={256} max={1000000} step={1024} style={{ width: "100%" }} />
              </Form.Item>
            </Col>
          </Row>

          <Row gutter={16}>
            <Col span={8}>
              <Form.Item name="concurrency" label="并发数">
                <InputNumber min={1} max={32} style={{ width: "100%" }} />
              </Form.Item>
            </Col>
            <Col span={8}>
              <Form.Item name="timeout" label="超时(秒)">
                <InputNumber min={10} max={600} style={{ width: "100%" }} />
              </Form.Item>
            </Col>
            <Col span={8}>
              <Form.Item name="json_mode" label="JSON 模式" valuePropName="checked">
                <Switch />
              </Form.Item>
            </Col>
          </Row>

          <Row gutter={16}>
            <Col span={12}>
              <Form.Item name="task_type" label="任务类型">
                <Select options={[
                  { value: "", label: "通用" },
                  { value: "outline", label: "目录生成" },
                  { value: "content", label: "正文撰写" },
                  { value: "analysis", label: "分析任务" },
                  { value: "quick", label: "快速草稿" },
                ]} />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item name="token_budget_daily" label="日 Token 预算">
                <InputNumber min={0} step={10000} style={{ width: "100%" }} placeholder="0 = 不限制" />
              </Form.Item>
            </Col>
          </Row>

          <Form.Item name="is_default" label="设为默认 Profile" valuePropName="checked">
            <Switch />
          </Form.Item>
        </Form>
      </Modal>
    </>
  );
}

// ═══════════════════════════════════════════════════════════════
// 子组件：用量仪表盘
// ═══════════════════════════════════════════════════════════════
function UsageDashboard({ data, loading }: { data: any; loading: boolean }) {
  if (loading) return <Spin />;
  const s = data?.summary;
  const b = data?.budget;
  if (!s) return null;

  return (
    <Spin spinning={loading}>
      <Row gutter={16} style={{ marginBottom: 16 }}>
        <Col span={6}>
          <Card size="small" style={{ textAlign: "center" }}>
            <Statistic title="调用次数" value={s.total_calls} suffix="次" prefix={<ApiOutlined />} />
          </Card>
        </Col>
        <Col span={6}>
          <Card size="small" style={{ textAlign: "center" }}>
            <Statistic title="Token 总量" value={s.total_tokens.toLocaleString()} prefix={<BarChartOutlined />} />
          </Card>
        </Col>
        <Col span={6}>
          <Card size="small" style={{ textAlign: "center" }}>
            <Statistic
              title="总成本"
              value={`¥${s.total_cost_cny}`}
              precision={2}
              prefix={<BulbOutlined />}
            />
          </Card>
        </Col>
        <Col span={6}>
          <Card size="small" style={{ textAlign: "center" }}>
            <Statistic
              title="成功率"
              value={s.success_rate}
              suffix="%"
              prefix={<CheckCircleOutlined />}
              valueStyle={{ color: s.success_rate >= 95 ? "#52c41a" : s.success_rate >= 80 ? "#faad14" : "#ff4d4f" }}
            />
          </Card>
        </Col>
      </Row>

      <Row gutter={16} style={{ marginBottom: 16 }}>
        <Col span={8}>
          <Card size="small" title="平均响应时间">
            <Statistic value={s.avg_response_time_ms} suffix="ms" />
          </Card>
        </Col>
        <Col span={8}>
          <Card size="small" title="每次平均 Token">
            <Statistic value={s.avg_tokens_per_call} suffix="tokens" />
          </Card>
        </Col>
        <Col span={8}>
          <Card size="small" title="成功 / 失败">
            <Space>
              <Badge status="success" text={`${s.success_count} 次`} />
              <Badge status="error" text={`${s.error_count} 次`} />
            </Space>
          </Card>
        </Col>
      </Row>

      {b && b.has_budget && (
        <Card size="small" title={
          <Space>
            <BulbOutlined />
            <span>日 Token 预算</span>
            {b.budget_exceeded && <Badge status="error" text="已超预算" />}
          </Space>
        }>
          <Progress
            percent={Math.min(b.usage_pct, 100)}
            status={b.budget_exceeded ? "exception" : b.usage_pct > 80 ? "active" : "normal"}
            format={() => (
              <span style={{ fontSize: 12 }}>
                {b.used_today.toLocaleString()} / {b.daily_limit.toLocaleString()}
                <span style={{ color: "#999", marginLeft: 8 }}>剩余 {b.remaining.toLocaleString()}</span>
              </span>
            )}
          />
        </Card>
      )}
    </Spin>
  );
}

// ═══════════════════════════════════════════════════════════════
// 子组件：健康度面板
// ═══════════════════════════════════════════════════════════════
function HealthPanel({ providers, loading }: { providers: HealthProvider[]; loading: boolean }) {
  const statusColor = (s: string) => s === "healthy" ? "success" : s === "degraded" ? "warning" : "error";
  const statusText = (s: string) => s === "healthy" ? "健康" : s === "degraded" ? "降级" : "异常";

  return (
    <Spin spinning={loading}>
      {providers.length === 0 ? (
        <Alert type="info" message="暂无健康数据，请等待首次 AI 调用后查看" />
      ) : (
        providers.map((p, i) => (
          <Card
            key={i}
            size="small"
            style={{ marginBottom: 8 }}
            extra={
              <Badge status={statusColor(p.status)} text={statusText(p.status)} />
            }
          >
            <Space direction="vertical" style={{ width: "100%" }} size={4}>
              <Space>
                <Typography.Text strong>{p.provider}</Typography.Text>
                <Tag>{p.model}</Tag>
                <Tag color="blue">{p.total_calls} 次调用</Tag>
              </Space>
              <Space size="large" style={{ fontSize: 12 }}>
                <span>成功率: <b>{p.success_rate}%</b></span>
                <span>平均延迟: <b>{p.avg_response_time_ms}ms</b></span>
                <span>健康分: <b>{p.health_score}</b></span>
                <span>最后使用: <b>{p.last_used || "未知"}</b></span>
              </Space>
              <Progress
                percent={p.health_score}
                format={() => `${p.health_score}分`}
                status={p.health_score >= 80 ? "success" : p.health_score >= 50 ? "active" : "exception"}
                size="small"
              />
            </Space>
          </Card>
        ))
      )}
    </Spin>
  );
}

// ═══════════════════════════════════════════════════════════════
// 主页面组件
// ═══════════════════════════════════════════════════════════════
export default function AIConfigPage() {
  const [activeTab, setActiveTab] = useState("profiles");
  const mountedRef = useRef(false);

  // Profile 状态
  const [profiles, setProfiles] = useState<AIPProfile[]>([]);
  const [activeProfile, setActiveProfile] = useState("default");
  const [profilesLoading, setProfilesLoading] = useState(true);
  // 加载失败原因（此前失败只是静默清空列表，用户无法区分「没有数据」和「加载失败」）
  const [profilesError, setProfilesError] = useState<string | null>(null);

  // 用量状态
  const [usageData, setUsageData] = useState<any>(null);
  const [usageLoading, setUsageLoading] = useState(true);

  // 健康度状态
  const [healthData, setHealthData] = useState<{ providers: HealthProvider[] }>({ providers: [] });
  const [healthLoading, setHealthLoading] = useState(true);

  // 加载 Profile 列表
  const loadProfiles = useCallback(async () => {
    setProfilesError(null);
    try {
      const [list, def] = await Promise.all([fetchAIProfiles(), getAIDefaultProfile().catch(() => null)]);
      if (mountedRef.current) {
        setProfiles(list);
        setActiveProfile(def?.name || "default");
      }
    } catch (err: any) {
      if (mountedRef.current) {
        setProfiles([]);
        setProfilesError(extractApiError(err));
      }
    } finally {
      if (mountedRef.current) setProfilesLoading(false);
    }
  }, []);

  // 加载用量仪表盘
  const loadDashboard = useCallback(async () => {
    try {
      const data = await getUsageDashboard();
      if (mountedRef.current) setUsageData(data);
    } catch {
      if (mountedRef.current) setUsageData(null);
    } finally {
      if (mountedRef.current) setUsageLoading(false);
    }
  }, []);

  // 加载健康度面板
  const loadHealth = useCallback(async () => {
    try {
      const data = await getHealthDashboard();
      if (mountedRef.current) setHealthData(data);
    } catch {
      if (mountedRef.current) setHealthData({ providers: [] });
    } finally {
      if (mountedRef.current) setHealthLoading(false);
    }
  }, []);

  useEffect(() => {
    mountedRef.current = true;
    loadProfiles();
    loadDashboard();
    loadHealth();

    return () => {
      mountedRef.current = false;
    };
  }, [loadProfiles, loadDashboard, loadHealth]);

  // 刷新函数
  const handleRefresh = useCallback(() => {
    loadProfiles();
    loadDashboard();
    loadHealth();
  }, [loadProfiles, loadDashboard, loadHealth]);

  // 导入配置
  const handleImport = () => {
    Modal.confirm({
      title: "导入 AI 配置",
      content: (
        <Input.TextArea
          rows={6}
          placeholder="粘贴 JSON 配置数据..."
          id="import-textarea"
        />
      ),
      okText: "导入",
      cancelText: "取消",
      onOk: async () => {
        const textarea = document.getElementById("import-textarea") as HTMLTextAreaElement;
        let data: any;
        // 修复：此前 JSON 解析与接口调用共用一个 catch，后端返回
        // 「导入数据格式错误」时也被误报成「JSON 解析失败」，无法定位真实原因
        try {
          data = JSON.parse(textarea?.value || "{}");
        } catch {
          message.error("JSON 解析失败，请检查格式");
          return;
        }
        if (!data || !Array.isArray(data.profiles)) {
          message.error("配置格式错误：需要包含 profiles 数组");
          return;
        }
        try {
          const result = await importAIPProfiles(data);
          const skipped = result.skipped ? `，跳过 ${result.skipped} 个` : "";
          message.success(`导入完成：新增 ${result.imported} 个，更新 ${result.updated} 个${skipped}`);
          handleRefresh();
        } catch (err: any) {
          message.error(extractApiError(err));
        }
      },
    });
  };

  // 导出配置
  const handleExport = async () => {
    try {
      const data = await exportAIPProfiles();
      // 二次加固：后端导出已不含密钥，这里再剥一次历史导出文件里可能残留的密钥字段
      if (data && Array.isArray(data.profiles)) {
        data.profiles = data.profiles.map((p: any) => {
          const { api_key, api_key_enc, ...rest } = p || {};
          return rest;
        });
      }
      const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `ai-config-${new Date().toISOString().slice(0, 10)}.json`;
      // 修复：<a> 未挂到 document 时部分浏览器不会触发下载，且 click 后
      // 同步 revokeObjectURL 会在下载真正开始前就把 Blob 释放掉
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 0);
      message.success("配置已导出（不含 API Key）");
    } catch (err: any) {
      message.error(extractApiError(err));
    }
  };

  // 清理过期数据
  const handleTruncate = async () => {
    Modal.confirm({
      title: "清理过期统计",
      content: "将删除 90 天前的 AI 调用记录，此操作不可撤销。",
      okButtonProps: { danger: true },
      onOk: async () => {
        try {
          const result = await truncateUsageStats(90);
          message.success(`已清理 ${result.deleted_records} 条记录`);
          loadDashboard();
        } catch (err: any) {
          message.error(extractApiError(err));
        }
      },
    });
  };

  const tabs = [
    {
      key: "profiles",
      label: <span><RocketOutlined /> AI Profile</span>,
      children: (
        <div>
          <div style={{ display: "flex", justifyContent: "flex-end", gap: 8, marginBottom: 16 }}>
            <Tooltip title="导出配置">
              <Button icon={<ExportOutlined />} onClick={handleExport}>导出</Button>
            </Tooltip>
            <Tooltip title="导入配置">
              <Button icon={<ImportOutlined />} onClick={handleImport}>导入</Button>
            </Tooltip>
          </div>
          {profilesError && (
            <Alert
              type="error"
              showIcon
              style={{ marginBottom: 12 }}
              message="加载 AI Profile 失败"
              description={profilesError}
              action={
                <Button size="small" onClick={loadProfiles}>重试</Button>
              }
            />
          )}
          <Spin spinning={profilesLoading}>
            <ProfileManager
              profiles={profiles}
              activeProfile={activeProfile}
              onRefresh={handleRefresh}
            />
          </Spin>
        </div>
      ),
    },
    {
      key: "usage",
      label: <span><BarChartOutlined /> 用量统计</span>,
      children: (
        <div>
          <div style={{ display: "flex", justifyContent: "flex-end", gap: 8, marginBottom: 16 }}>
            <Button icon={<ReloadOutlined />} onClick={loadDashboard}>刷新</Button>
            <Button danger icon={<DeleteOutlined />} onClick={handleTruncate}>清理旧数据</Button>
          </div>
          <UsageDashboard data={usageData} loading={usageLoading} />
        </div>
      ),
    },
    {
      key: "health",
      label: <span><DashboardOutlined /> 健康度</span>,
      children: (
        <div>
          <div style={{ marginBottom: 16 }}>
            <Button icon={<ReloadOutlined />} onClick={loadHealth}>刷新健康数据</Button>
          </div>
          <HealthPanel providers={healthData.providers} loading={healthLoading} />
        </div>
      ),
    },
  ];

  return (
    <div style={{ maxWidth: 1100, margin: "0 auto" }}>
      <Typography.Title level={4} style={{ marginBottom: 8 }}>
        <SettingOutlined /> AI 模型配置中心
      </Typography.Title>
      <Typography.Paragraph type="secondary" style={{ marginBottom: 24 }}>
        管理 AI 模型配置、Profile 预设、用量统计和 Provider 健康度
      </Typography.Paragraph>

      <Tabs activeKey={activeTab} onChange={setActiveTab} destroyOnHidden items={tabs} />
    </div>
  );
}
