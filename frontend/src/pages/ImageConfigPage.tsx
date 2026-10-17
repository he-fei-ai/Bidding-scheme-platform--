import { useEffect, useState, useCallback, useRef } from "react";
import {
  Button, Card, Col, Form, Input, InputNumber, Row, Select, message, Spin,
  Typography, Space, Tag, Tooltip, Modal, Alert, Steps, Switch,
} from "antd";
import type { DefaultOptionType } from "antd/es/select";
import {
  SaveOutlined, ReloadOutlined, CheckCircleOutlined,
  CloseCircleOutlined, PictureOutlined, BarChartOutlined,
  EyeOutlined, EyeInvisibleOutlined, HistoryOutlined,
  UpOutlined, DownOutlined, CheckOutlined, ApiOutlined,
} from "@ant-design/icons";
import { extractApiError } from "../utils";
import {
  getImageProviderConfigs, saveImageProviderConfig, activateImageProviderConfig,
  getImageUsageStats, getImageTasks, resolveImageTaskSrc, ImageGenTask,
  fetchModels as fetchImageModels, fetchModelInfo, testImageConnection,
} from "../api";
import type { ImageProviderConfig } from "../types";

// Provider options mapping（厂商元信息：中文名 + 默认地址 + 默认模型）
const PROVIDER_META: Record<string, { label: string; base_url: string; model: string }> = {
  agnes: { label: "Agnes AI（新加坡）", base_url: "https://api.agnes-ai.cn/v1", model: "agnes-image-2.1-flash" },
  dashscope: { label: "阿里云百炼", base_url: "https://dashscope.aliyuncs.com/compatible-mode/v1", model: "dashscope-image-studio-v1" },
  volcengine: { label: "火山引擎", base_url: "https://ark.cn-beijing.volces.com/api/v3", model: "doubao-edition-v1" },
  hunyuan: { label: "腾讯混元", base_url: "https://api.hunyuan.cloud.tencent.com/v1", model: "hunyuan-image" },
  zhipu: { label: "智谱AI", base_url: "https://open.bigmodel.cn/api/paas/v5/image/image-generation", model: "cogview-4" },
  stepfun: { label: "阶跃星辰", base_url: "https://api.stepfun.com/v1", model: "stepfun-art" },
  baidu: { label: "百度文心一格", base_url: "https://qianfan.baidubce.com/v2", model: "wanx-v1" },
  xingchen_maas: { label: "讯飞星辰 MaaS (Token Plan)", base_url: "https://maas-token-api.cn-huabei-1.xf-yun.com/v2", model: "xsparkx2flash" },
  nvidia: { label: "NVIDIA NIM API", base_url: "https://integrate.api.nvidia.com/v1", model: "nvidia/nemotron-mini-4b-instruct" },
  openai_compatible: { label: "自定义兼容 OpenAI", base_url: "", model: "" },
};

const QUALITY_OPTIONS = [
  { value: "standard", label: "标准" },
  { value: "hd", label: "高清" },
  { value: "4k", label: "超清 4K" },
];

const SIZE_OPTIONS = [
  { value: "512x512", label: "512×512" },
  { value: "768x768", label: "768×768" },
  { value: "1024x1024", label: "1024×1024" },
  { value: "1024x1792", label: "1024×1792" },
  { value: "1792x1024", label: "1792×1024" },
];

const RATIO_OPTIONS = [
  { value: "1:1", label: "1:1" },
  { value: "3:4", label: "3:4" },
  { value: "4:3", label: "4:3" },
  { value: "16:9", label: "16:9" },
];

// ── 单厂商卡片（含独立 Form）───────────────────────────────────────────────
function ProviderCard({
  config,
  onSaved,
}: {
  config: ImageProviderConfig;
  onSaved: () => void;
}) {
  const [form] = Form.useForm();
  const [expanded, setExpanded] = useState(false);
  const [saving, setSaving] = useState(false);
  const [activating, setActivating] = useState(false);
  const [testResult, setTestResult] = useState<{ status: string; message: string } | null>(null);
  const [showKey, setShowKey] = useState(false);
  const [modelOptions, setModelOptions] = useState<DefaultOptionType[]>([]);
  const [fetchingModels, setFetchingModels] = useState(false);
  const currentFormRef = useRef(form);
  currentFormRef.current = form;

  useEffect(() => {
    // 关键修复：仅在 Form 展开（挂载）后才调用 setFieldsValue，避免 useForm 警告
    if (!expanded) return;
    // 切换厂商时清空已缓存的模型选项
    setModelOptions([]);
    // BUG 修复：使用 queueMicrotask 延迟一帧，确保 Form 已完全挂载
    queueMicrotask(() => {
      try {
        form.setFieldsValue({
          api_key: config.api_key || "",
          base_url: config.base_url || "",
          model: config.model || "",
          max_concurrency: config.max_concurrency || 2,
          max_per_project: config.max_per_project || 10,
          quality: config.quality || "standard",
          style_injection: config.style_injection || "",
          enabled: config.enabled !== undefined ? config.enabled : 1,
          default_size: config.default_size || "1024x1024",
          default_ratio: config.default_ratio || "1:1",
          price_per_image: config.price_per_image || 0,
        });
      } catch {
        // Form 未挂载时可能抛异常，忽略即可
      }
    });
  }, [config.provider_name, expanded]);

  const handleSave = useCallback(async () => {
    try {
      const values = await currentFormRef.current.validateFields();
      setSaving(true);
      await saveImageProviderConfig({
        provider_name: config.provider_name,
        api_key: values.api_key || "",
        base_url: values.base_url || "",
        model: values.model || "",
        max_concurrency: Number(values.max_concurrency ?? config.max_concurrency) || 2,
        // 修复：enabled / max_per_project / style_injection 此前没有对应表单控件，
        // 未注册字段不会出现在 validateFields() 结果里，每次保存都会把
        // enabled 写成 false（等于关停该厂商）、每项目上限重置、风格词清空。
        // 表单缺省时回落到服务端已保存的值，而不是硬编码默认值。
        max_per_project: Number(values.max_per_project ?? config.max_per_project) || 10,
        quality: values.quality || config.quality || "standard",
        style_injection: values.style_injection ?? config.style_injection ?? "",
        enabled: values.enabled === undefined ? config.enabled !== false : Boolean(values.enabled),
        default_size: values.default_size || "1024x1024",
        default_ratio: values.default_ratio || "1:1",
        price_per_image: Number(values.price_per_image) || 0,
      } as ImageProviderConfig);
      const msg = values.api_key ? `${config.provider_name} 配置已保存并已自动设为当前使用` : `${config.provider_name} 配置已保存`;
      message.success(msg);
      onSaved();
    } catch (err: any) {
      if (err.errorFields) return;
      message.error(`保存失败: ${extractApiError(err)}`);
    } finally {
      setSaving(false);
    }
  }, [config.provider_name, config.is_active, config.api_key, form, onSaved]);

  const handleActivate = useCallback(async () => {
    // 修复：表单仅在展开时初始化，未展开时 getFieldValue 为 undefined，
    // 服务端明明已配置好密钥却被拦截。以服务端返回的 config 为准。
    const apiKey = config.api_key || form.getFieldValue("api_key");
    if (!apiKey) { message.warning("请先填写 API Key 再设为当前使用"); return; }
    setActivating(true);
    try {
      await activateImageProviderConfig(config.provider_name);
      message.success(`已激活「${config.provider_name}」`);
      onSaved();
    } catch (err: any) {
      message.error(`激活失败: ${extractApiError(err)}`);
    } finally {
      setActivating(false);
    }
  }, [config.provider_name, form, onSaved]);

  const handleTest = useCallback(async () => {
    setTestResult({ status: "loading", message: "测试中..." });
    try {
      const values = await form.validateFields(["api_key", "model", "base_url"]);
      const result = await testImageConnection({
        provider: config.provider_name,
        api_key: values.api_key,
        model: values.model,
        base_url: values.base_url,
        max_concurrency: form.getFieldValue("max_concurrency") || 2,
        max_per_project: form.getFieldValue("max_per_project") || 10,
        quality: form.getFieldValue("quality") || "standard",
        style_injection: form.getFieldValue("style_injection") || "",
        enabled: true,
        default_size: form.getFieldValue("default_size") || "1024x1024",
        default_ratio: form.getFieldValue("default_ratio") || "1:1",
        price_per_image: form.getFieldValue("price_per_image") || 0,
      });
      setTestResult(result);
      if (result.status === "ok") message.success(`${config.provider_name} 连接测试成功`);
    } catch (err: any) {
      const msg = extractApiError(err);
      setTestResult({ status: "error", message: msg });
      message.error(msg);
    }
  }, [config.provider_name, form]);

  const handleFetchModels = useCallback(async () => {
    const apiKey = form.getFieldValue("api_key");
    const baseUrl = form.getFieldValue("base_url");
    if (!apiKey || !baseUrl) { message.warning("请先填写 API Key 和 Base URL"); return; }
    setFetchingModels(true);
    try {
      // 跨模块复用：这里走的是文本路由的 /ai/models。传入 provider 名让后端按
      // image_provider_configs 解析本厂商自己的密钥 —— 否则占位符会回落成主配置
      // 密钥，而图像厂商密钥与主配置不同本是常态，导致已配置厂商恒 401。
      const result = await fetchImageModels(apiKey, baseUrl, config.provider_name);
      const modelList: string[] = Array.isArray(result) ? result : (result.models || []);
      if (modelList.length > 0) {
        const options: DefaultOptionType[] = modelList.map((m) => ({ label: m, value: m }));
        setModelOptions(options);
        const current = form.getFieldValue("model");
        const selected = modelList.includes(current) ? current : modelList[0];
        form.setFieldsValue({ model: selected });
        try {
          const info = await fetchModelInfo({ api_key: apiKey, base_url: baseUrl, model: selected, provider: config.provider_name });
          if (info) form.setFieldsValue({ max_concurrency: info.concurrency });
        } catch { /* 忽略 */ }
        message.success(`获取到 ${modelList.length} 个模型，请从下拉菜单选择`);
      } else {
        message.info("未获取到模型列表，可手动输入模型名称");
      }
    } catch (err: any) {
      message.error(extractApiError(err));
    } finally {
      setFetchingModels(false);
    }
  }, [config.provider_name, form]);

  const handleModelChange = useCallback((model: string) => {
    form.setFieldsValue({ model });
    const apiKey = form.getFieldValue("api_key");
    const baseUrl = form.getFieldValue("base_url");
    if (model && apiKey && baseUrl) {
      fetchModelInfo({ api_key: apiKey, base_url: baseUrl, model, provider: config.provider_name })
        .then(info => { if (info) form.setFieldsValue({ max_concurrency: info.concurrency }); })
        .catch(() => {});
    }
  }, [config.provider_name, form]);

  return (
    <Col xs={24} sm={12} lg={8} key={config.provider_name}>
      <Card
        size="small"
        style={{
          height: "100%",
          minHeight: 160,
          border: config.is_active ? "1.5px solid #52c41a" : undefined,
          borderRadius: 8,
          boxShadow: config.is_active ? "0 2px 8px rgba(82,196,26,0.15)" : undefined,
        }}
        title={
          <Space>
            <span style={{ fontSize: 14, fontWeight: 600 }}>
              {PROVIDER_META[config.provider_name]?.label || config.provider_name}
            </span>
            {config.is_active && <Tag color="green" style={{ marginInlineEnd: 0 }}>当前使用</Tag>}
            {config.model && !config.is_active && <Tag color="blue" style={{ marginInlineEnd: 0 }}>已配置</Tag>}
            {!config.model && !config.is_active && <Tag color="default" style={{ marginInlineEnd: 0 }}>未配置</Tag>}
          </Space>
        }
        extra={
          <Button type="text" size="small" icon={expanded ? <UpOutlined /> : <DownOutlined />} onClick={() => setExpanded(p => !p)}>
            {expanded ? "收起" : "编辑"}
          </Button>
        }
      >
        {!expanded && (
          <div style={{ color: "#666", fontSize: 12, lineHeight: "20px" }}>
            <div>地址：{config.base_url ? `${config.base_url.slice(0, 30)}${config.base_url.length > 30 ? "…" : ""}` : "未填写"}</div>
            <div>模型：{config.model || "未填写"}</div>
            <div>并发：{config.max_concurrency} · 质量：{config.quality || "standard"}</div>
          </div>
        )}

        {/* 始终挂载 Form，用 display 隐藏避免 useForm 未连接警告 */}
        <div style={{ display: expanded ? undefined : "none" }}>
          <Form form={form} layout="vertical" style={{ marginTop: 8 }}>
            <Form.Item name="api_key" label="API Key" required>
              <Space.Compact style={{ width: "100%" }}>
                {showKey
                  ? <Input value={form.getFieldValue("api_key") || ""} onChange={(e) => form.setFieldsValue({ api_key: e.target.value })} placeholder="sk-..." autoComplete="new-password" />
                  : <Input.Password placeholder="sk-..." autoComplete="new-password" />
                }
                <Tooltip title={showKey ? "隐藏密钥" : "显示密钥"}>
                  <Button icon={showKey ? <EyeInvisibleOutlined /> : <EyeOutlined />} onClick={() => setShowKey(p => !p)} />
                </Tooltip>
              </Space.Compact>
            </Form.Item>

            <Form.Item name="base_url" label="API Base URL" required>
              <Input placeholder="https://api.example.com/v1" autoComplete="off" />
            </Form.Item>

            <Form.Item name="model" label="模型名称" required>
              <Space.Compact style={{ width: "100%" }}>
                <Select
                  value={form.getFieldValue("model") || undefined}
                  onChange={(value) => handleModelChange(value as string)}
                  onSearch={(keyword) => {
                    if (keyword && keyword.trim()) {
                      setModelOptions((prev) => {
                        const exists = prev.some((o) => o?.value === keyword);
                        if (exists) return prev;
                        return [{ label: keyword, value: keyword }, ...prev];
                      });
                    }
                  }}
                  options={modelOptions}
                  showSearch
                  allowClear
                  filterOption={(input, option) =>
                    (option?.label?.toString() ?? "").toLowerCase().includes(input.toLowerCase())
                  }
                  placeholder={
                    modelOptions.length > 0
                      ? `从 ${modelOptions.length} 个模型中选择，或手动输入`
                      : "点击右侧「获取」从 API 拉取，或直接输入模型名"
                  }
                  notFoundContent={modelOptions.length === 0 ? "请先填写 API Key 和 Base URL，点击右侧「获取」拉取模型列表" : undefined}
                  style={{ minWidth: 260 }}
                />
                <Button
                  icon={<ReloadOutlined />}
                  loading={fetchingModels}
                  onClick={handleFetchModels}
                  title={modelOptions.length > 0 ? `重新获取（当前 ${modelOptions.length} 个模型）` : "从 API 获取模型列表"}
                >
                  {modelOptions.length > 0 ? `${modelOptions.length}` : "获取"}
                </Button>
              </Space.Compact>
            </Form.Item>

            <Row gutter={[8, 8]}>
              <Col span={12}>
                <Form.Item name="max_concurrency" label="最大并发">
                  <InputNumber min={1} max={100} style={{ width: "100%" }} />
                </Form.Item>
              </Col>
              <Col span={12}>
                <Form.Item name="quality" label="质量">
                  <Select options={QUALITY_OPTIONS} style={{ width: "100%" }} />
                </Form.Item>
              </Col>
              <Col span={12}>
                <Form.Item name="default_size" label="默认尺寸">
                  <Select options={SIZE_OPTIONS} style={{ width: "100%" }} />
                </Form.Item>
              </Col>
              <Col span={12}>
                <Form.Item name="default_ratio" label="默认比例">
                  <Select options={RATIO_OPTIONS} style={{ width: "100%" }} />
                </Form.Item>
              </Col>
              <Col span={24}>
                <Form.Item name="price_per_image" label="每张价格（元）">
                  <InputNumber min={0} precision={4} style={{ width: "100%" }} />
                </Form.Item>
              </Col>
            </Row>

            <Row gutter={[8, 8]}>
              <Col span={12}>
                <Form.Item name="max_per_project" label="每项目上限">
                  <InputNumber min={1} max={1000} style={{ width: "100%" }} />
                </Form.Item>
              </Col>
              <Col span={12}>
                <Form.Item name="enabled" label="启用该厂商" valuePropName="checked">
                  <Switch />
                </Form.Item>
              </Col>
              <Col span={24}>
                <Form.Item name="style_injection" label="风格注入词">
                  <Input.TextArea
                    rows={2}
                    placeholder="追加到每次生图提示词前的风格描述，可留空"
                  />
                </Form.Item>
              </Col>
            </Row>

            <Space wrap size={8}>
              <Button type="primary" icon={<SaveOutlined />} loading={saving} onClick={handleSave}>
                保存本厂商
              </Button>
              <Button icon={<CheckCircleOutlined />} loading={activating} onClick={handleActivate} disabled={config.is_active}>
                {config.is_active ? "当前使用中" : "设为当前使用"}
              </Button>
              <Button icon={<ApiOutlined />} onClick={handleTest} disabled={!config.api_key} loading={testResult?.status === "loading"}>
                测试连接
              </Button>
            </Space>

            {testResult && (
              <Alert
                type={testResult.status === "ok" ? "success" : "error"}
                message={testResult.status === "ok" ? "连接成功" : "连接失败"}
                description={testResult.message}
                style={{ marginTop: 8 }}
                closable
                onClose={() => setTestResult(null)}
              />
            )}
          </Form>
        </div>
      </Card>
    </Col>
  );
}

// ── 任务缩略图 ─────────────────────────────────────────────────────────────
function TaskThumb({ task }: { task: ImageGenTask }) {
  const [failed, setFailed] = useState(false);
  const src = failed ? "" : resolveImageTaskSrc(task);
  if (!src) {
    return (
      <div style={{
        width: 64, height: 64, borderRadius: 6, border: "1px dashed #d9d9d9",
        display: "flex", alignItems: "center", justifyContent: "center",
        color: "#bfbfbf", fontSize: 12, background: "#fafafa",
      }}>
        无图
      </div>
    );
  }
  return (
    <img
      src={src}
      alt={task.prompt?.slice(0, 20) || "生成图"}
      style={{ width: 64, height: 64, objectFit: "cover", borderRadius: 6 }}
      onError={() => setFailed(true)}
    />
  );
}

function renderTaskStatus(status: string, errorMessage?: string | null) {
  switch (status) {
    case "success": return <Tag color="success">成功</Tag>;
    case "failed": return <Tooltip title={errorMessage || "生成失败"}><Tag color="error">失败</Tag></Tooltip>;
    case "generating": return <Tag color="processing">生成中</Tag>;
    case "pending": return <Tag color="warning">排队中</Tag>;
    case "cancelled": return <Tag>已取消</Tag>;
    default: return <Tag>{status}</Tag>;
  }
}

// ── 主页面 ────────────────────────────────────────────────────────────────
export default function ImageConfigPage() {
  const mountedRef = useRef(false);
  const [loading, setLoading] = useState(true);
  const [configs, setConfigs] = useState<ImageProviderConfig[]>([]);
  const [usageStats, setUsageStats] = useState<any>(null);
  const [usageLoading, setUsageLoading] = useState(false);
  const [imageTasks, setImageTasks] = useState<ImageGenTask[]>([]);
  const [imageTasksLoading, setImageTasksLoading] = useState(false);
  const [quickVisible, setQuickVisible] = useState(false);
  const [quickLoading, setQuickLoading] = useState(false);
  const [quickError, setQuickError] = useState("");
  const quickKeyRef = useRef("");

  useEffect(() => {
    mountedRef.current = true;
    loadAll();
    return () => { mountedRef.current = false; };
  }, []);

  const loadAll = useCallback(async () => {
    try {
      const [cfgs, stats] = await Promise.all([
        getImageProviderConfigs(),
        getImageUsageStats(),
      ]);
      if (!mountedRef.current) return;
      setConfigs(cfgs.configs);
      setUsageStats(stats);
    } catch {
      if (mountedRef.current) message.error("加载图像配置失败");
    } finally {
      if (mountedRef.current) setLoading(false);
    }
  }, []);

  const loadImageTasks = useCallback(async () => {
    try {
      setImageTasksLoading(true);
      const result = await getImageTasks({ limit: 20 });
      if (mountedRef.current) setImageTasks(result.tasks);
    } catch {
      // ignore
    } finally {
      if (mountedRef.current) setImageTasksLoading(false);
    }
  }, []);

  useEffect(() => {
    loadImageTasks();
  }, [loadImageTasks]);

  const handleQuickSetup = useCallback(async () => {
    const key = quickKeyRef.current;
    if (!key) { message.warning("请输入 API Key"); return; }
    setQuickLoading(true);
    setQuickError("");
    try {
      // 先调文本 quickSetup 作为占位，实际图像有独立的
      message.info("请使用上方卡片填写 API Key 后点「设为当前使用」");
      setQuickVisible(false);
    } catch (err: any) {
      setQuickError(extractApiError(err));
    } finally {
      setQuickLoading(false);
    }
  }, []);

  if (loading) {
    return (
      <div style={{ display: "flex", justifyContent: "center", alignItems: "center", height: "300px" }}>
        <Spin size="large" />
      </div>
    );
  }

  return (
    <div style={{ maxWidth: 1200, margin: "0 auto", padding: "0 8px" }}>
      <Typography.Title level={4} style={{ marginBottom: 4, display: "flex", alignItems: "center", gap: 8 }}>
        <PictureOutlined /> 图像模型配置
      </Typography.Title>
      <Typography.Paragraph type="secondary" style={{ marginTop: 0, marginBottom: 20 }}>
        每个厂商的配置独立保存、互不干扰。点击「编辑」展开表单填写后保存，点「设为当前使用」切换活跃厂商。
      </Typography.Paragraph>

      <Row gutter={[16, 16]}>
        {configs.map(c => (
          <ProviderCard key={c.provider_name} config={c} onSaved={loadAll} />
        ))}
      </Row>

      {/* 用量统计 */}
      {usageStats && (
        <Card
          title={<Space><BarChartOutlined /><span>用量统计</span></Space>}
          size="small"
          style={{ marginBottom: 16, marginTop: 16 }}
          loading={usageLoading}
        >
          <Row gutter={[16, 16]}>
            {[
              // 修复：字段名与后端 `/ai/image-model/usage` 对齐。
              // 旧代码读 total_tasks / total_time_seconds，后端从未返回这两个键，
              // 导致「总任务数」「总耗时」恒为 0 / —。
              { label: "总任务数", value: usageStats.total ?? 0, color: "#1677ff" },
              { label: "本月", value: usageStats.monthly ?? 0, color: "#1677ff" },
              { label: "成功", value: usageStats.success_count ?? 0, color: "#52c41a" },
              { label: "失败", value: usageStats.failed_count ?? 0, color: "#ff4d4f" },
              { label: "成功率", value: `${usageStats.success_rate ?? 0}%`, color: "#722ed1" },
              { label: "总费用", value: usageStats.total_cost ? `¥${Number(usageStats.total_cost).toFixed(4)}` : "—", color: "#eb2f96" },
            ].map(item => (
              <Col key={item.label} span={4}>
                <div style={{ textAlign: "center", padding: "8px 0" }}>
                  <div style={{ fontSize: 24, fontWeight: 600, color: item.color }}>{item.value}</div>
                  <div style={{ fontSize: 12, color: "#999", marginTop: 4 }}>{item.label}</div>
                </div>
              </Col>
            ))}
          </Row>
        </Card>
      )}

      {/* 最近生成任务 */}
      <Card
        title={
          <Space>
            <HistoryOutlined />
            <span>最近生成任务</span>
            <Tag color="default">{imageTasks.length} 条记录</Tag>
          </Space>
        }
        size="small"
        style={{ marginBottom: 16 }}
        loading={imageTasksLoading}
        extra={
          <Button size="small" icon={<ReloadOutlined />} onClick={loadImageTasks} loading={imageTasksLoading}>
            刷新
          </Button>
        }
      >
        {imageTasks.length === 0 ? (
          <div style={{ textAlign: "center", color: "#999", padding: "24px 0" }}>
            <HistoryOutlined style={{ fontSize: 32, marginBottom: 8 }} />
            <div>暂无生成记录</div>
          </div>
        ) : (
          <Row gutter={[8, 8]}>
            {imageTasks.map(task => (
              <Col key={task.id} span={6}>
                <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                  <TaskThumb task={task} />
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div style={{ fontSize: 12, color: "#333", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                      {task.prompt?.slice(0, 30) || "无描述"}
                    </div>
                    <div style={{ fontSize: 11, color: "#999", marginTop: 2 }}>
                      {task.provider} · {task.model}
                    </div>
                    <div style={{ marginTop: 4 }}>{renderTaskStatus(task.status, task.error_message)}</div>
                  </div>
                </div>
              </Col>
            ))}
          </Row>
        )}
      </Card>

      {/* 快速提示 */}
      <Card style={{ marginTop: 8 }} size="small">
        <Space wrap>
          <CheckOutlined style={{ color: "#52c41a" }} />
          <span style={{ fontSize: 13, color: "#666" }}>
            提示：在上方卡片中填写 API Key 后点击「设为当前使用」即可切换活跃图像生成厂商。各厂商 KEY 独立保存，互不共享。
          </span>
        </Space>
      </Card>
    </div>
  );
}
