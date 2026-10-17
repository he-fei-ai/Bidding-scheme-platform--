import { useEffect, useState, useCallback, useRef } from "react";
import {
  Button, Card, Col, Form, Input, InputNumber, Row, message, Spin, Select,
  Typography, Space, Tag, Tooltip, Modal, Alert, Steps,
} from "antd";
import type { DefaultOptionType } from "antd/es/select";
import {
  SaveOutlined, ApiOutlined, ReloadOutlined,
  ThunderboltOutlined,
  RocketOutlined, ExperimentOutlined, EyeOutlined, EyeInvisibleOutlined,
  ThunderboltFilled, UpOutlined, DownOutlined, QuestionCircleOutlined,
} from "@ant-design/icons";
import { extractApiError, findProviderMeta } from "../utils";
import {
  getTextProviderConfigs, saveTextProviderConfig, activateTextProviderConfig,
  fetchModels, quickSetup, fetchModelInfo, testAIConnection, getAIConfig,
} from "../api";
import type { TextProviderConfig, TextProviderSavePayload } from "../types";

// PROVIDER_META 已抽为 utils 单一事实源（TEXT_PROVIDER_META/findProviderMeta，组件级可测化 2026-10-05）

// ── 单厂商卡片（含独立 Form）───────────────────────────────────────────────
function ProviderCard({
  config,
  onSaved,
}: {
  config: TextProviderConfig;
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

  // 初始化表单
  // BUG 修复：使用 queueMicrotask 延迟一帧，确保 Form 组件已挂载到 DOM
  useEffect(() => {
    // P1-ID（2026-10-14）：优先用后端下发的稳定 id 查本地元数据兜底表。
    // 此前只传中文 provider_name，而兜底表是 snake_case 键（11 项仅 2 项能对上），
    // 导致 base_url/model 默认值对绝大多数厂商静默失效。
    const meta = findProviderMeta(config.provider_id || config.provider_name);
    // 切换厂商时清空已缓存的模型选项
    setModelOptions([]);
    queueMicrotask(() => {
      try {
        form.setFieldsValue({
          api_key: config.api_key || "",
          base_url: config.base_url || meta?.base_url || "",
          model: config.model || meta?.model || "",
          max_tokens: config.max_tokens ?? undefined,
          concurrency: config.concurrency ?? undefined,
          temperature: config.temperature ?? 0.7,
          timeout: config.timeout ?? 120,
          document_profile: config.document_profile || "",
        });
      } catch {
        // Form 未挂载时可能抛异常，忽略即可
      }
    });
  }, [config.provider_name, config.provider_id]);

  /** 组装提交载荷：统一做范围钳制，避免各按钮逻辑漂移 */
  const buildPayload = (values: any, activate: boolean): TextProviderSavePayload => ({
    provider_name: config.provider_name,
    api_key: values.api_key || "",
    base_url: values.base_url || "",
    model: values.model || "",
    max_tokens: Math.min(Math.max(Number(values.max_tokens) || 4096, 256), 65536),
    // F-12 对齐后端（save_ai_config）：temperature 允许 0（确定性输出），
    // 此前 `|| 0.7` 会把用户显式设置的 0 静默替换为 0.7；并发补充上限 64。
    concurrency: Math.min(Number(values.concurrency) || 4, 64),
    temperature: values.temperature ?? 0.7,
    // P1-CFG（2026-10-14）：超时此前在库里有列、provider 也读，但既无请求字段
    // 也无 UI，等于永远 120s。补齐输入 + 提交（后端钳制到 [5, 600]）。
    timeout: Math.min(Math.max(Number(values.timeout) || 120, 5), 600),
    // P1-DOC（2026-10-14）：行业画像被后端配图编排消费（sections.py）。原请求模型
    // 默认 ''，每次保存都会静默清空。现显式提交；未填写时传 ""（=显式清空），
    // 由表单本身承载真值，不再出现「保存一次丢一次」。
    document_profile: values.document_profile ?? "",
    // TD-5：显式声明语义 ——「保存本厂商」不切换主用，「设为当前使用」才切换。
    // 此前前端从不传该字段，后端默认 True，于是「只改温度」也会把主配置切走。
    activate_after_save: activate,
  });

  const handleSave = useCallback(async () => {
    try {
      const values = await currentFormRef.current.validateFields();
      setSaving(true);
      await saveTextProviderConfig(buildPayload(values, false));
      message.success(`${config.provider_name} 配置已保存`);
      onSaved();
    } catch (err: any) {
      if (err.errorFields) return; // 表单验证失败
      message.error(`保存失败: ${extractApiError(err)}`);
    } finally {
      setSaving(false);
    }
  }, [config.provider_name, onSaved, buildPayload]);

  const handleActivate = useCallback(async () => {
    try {
      const values = await currentFormRef.current.validateFields();
      if (!values.api_key && !config.api_key_set) { message.warning("请先填写 API Key 再设为当前使用"); return; }
      setActivating(true);
      // 如果配置未保存或表单有修改，先自动保存再激活（保存阶段不做自动激活）
      await saveTextProviderConfig(buildPayload(values, false));
      await activateTextProviderConfig(config.provider_name);
      message.success(`已保存并激活「${config.provider_name}」`);
      onSaved();
    } catch (err: any) {
      if (err.errorFields) return; // 表单验证失败
      message.error(`激活失败: ${extractApiError(err)}`);
    } finally {
      setActivating(false);
    }
  }, [config.provider_name, config.api_key_set, onSaved, buildPayload]);

  const handleTest = useCallback(async () => {
    setTestResult({ status: "loading", message: "测试中..." });
    try {
      const values = await form.validateFields(["api_key", "model", "base_url"]);
      const result = await testAIConnection({
        provider: config.provider_name,
        api_key: values.api_key,
        model: values.model,
        base_url: values.base_url,
        max_tokens: values.max_tokens ?? 4096,
        temperature: values.temperature ?? 0.7,
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
      const result = await fetchModels(apiKey, baseUrl, config.provider_name);
      const modelList: string[] = Array.isArray(result) ? result : (result.models || []);
      if (modelList.length > 0) {
        const options: DefaultOptionType[] = modelList.map((m) => ({ label: m, value: m }));
        setModelOptions(options);
        // 保留已选中的模型（若已在列表中），否则自动选第一个
        const current = form.getFieldValue("model");
        const selected = modelList.includes(current) ? current : modelList[0];
        form.setFieldsValue({ model: selected });
        // 自动填充元数据
        try {
          const info = await fetchModelInfo({ api_key: apiKey, base_url: baseUrl, model: selected, provider: config.provider_name, provider_id: config.provider_id || "" });
          if (info) form.setFieldsValue({ max_tokens: info.max_tokens, concurrency: info.concurrency });
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
    // 防抖自动获取元数据
    const apiKey = form.getFieldValue("api_key");
    const baseUrl = form.getFieldValue("base_url");
    if (model && apiKey && baseUrl) {
      fetchModelInfo({ api_key: apiKey, base_url: baseUrl, model, provider: config.provider_name, provider_id: config.provider_id || "" })
        .then(info => { if (info) form.setFieldsValue({ max_tokens: info.max_tokens, concurrency: info.concurrency }); })
        .catch(() => {});
    }
  }, [config.provider_name, config.provider_id, form]);

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
            <span style={{ fontSize: 14, fontWeight: 600 }}>{config.provider_name}</span>
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
        {/* 收起时概览 */}
        {!expanded && (
          <div style={{ color: "#666", fontSize: 12, lineHeight: "20px" }}>
            <div>地址：{config.base_url ? `${config.base_url.slice(0, 30)}${config.base_url.length > 30 ? "…" : ""}` : "未填写"}</div>
            <div>模型：{config.model || "未填写"}</div>
            <div>Token：{config.max_tokens} · 并发：{config.concurrency} · 超时：{config.timeout ?? 120}s</div>
          </div>
        )}

        {/* 展开时表单：始终挂载 Form，收起时用样式隐藏，避免 antd useForm 未连接警告 */}
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
                    // 允许手动输入：若输入值不在下拉选项中，临时加入选项列表
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

            <Row gutter={8}>
              <Col span={12}>
                <Form.Item name="max_tokens" label="Token 数（生成上限）">
                  <InputNumber
                    min={256}
                    max={65536}
                    step={256}
                    placeholder="留空用默认 4096"
                    style={{ width: "100%" }}
                    suffix="tokens"
                  />
                </Form.Item>
              </Col>
              <Col span={12}>
                <Form.Item name="concurrency" label="并发数">
                  <InputNumber
                    min={1}
                    max={64}
                    step={1}
                    placeholder="留空用默认 4"
                    style={{ width: "100%" }}
                    suffix="路"
                  />
                </Form.Item>
              </Col>
            </Row>

            <Row gutter={8}>
              <Col span={12}>
                <Form.Item name="temperature" label="Temperature">
                  <InputNumber min={0} max={2} step={0.1} precision={1} style={{ width: "100%" }} />
                </Form.Item>
              </Col>
              <Col span={12}>
                {/* P1-CFG（2026-10-14）：超时此前既无请求字段也无 UI，恒为 120s */}
                <Form.Item
                  name="timeout"
                  label="请求超时（秒）"
                  tooltip="单次 AI 请求的超时上限。厂商在配置页单独调大/调小后，激活该厂商时会同步到主配置。"
                >
                  <InputNumber min={5} max={600} step={10} style={{ width: "100%" }} suffix="s" />
                </Form.Item>
              </Col>
            </Row>

            {/* P1-DOC（2026-10-14）：行业画像被后端配图编排消费（sections.py
                generate_illustration_*），此前后端有列、前端无入口，且每次保存
                厂商配置都会被默认空串清空 */}
            <Form.Item
              name="document_profile"
              label={
                <Space size={4}>
                  <span>行业画像</span>
                  <Tooltip title="本项目所属行业/领域描述，用于提升章节配图的相关性。留空表示不限定行业。">
                    <QuestionCircleOutlined style={{ color: "#999" }} />
                  </Tooltip>
                </Space>
              }
            >
              <Input placeholder="例如：市政公用工程总承包 / 智慧城市信息化" allowClear />
            </Form.Item>

            <Space wrap size={8}>
              <Button type="primary" icon={<SaveOutlined />} loading={saving} onClick={handleSave}>
                保存本厂商
              </Button>
              <Button icon={<ThunderboltFilled />} loading={activating} onClick={handleActivate} disabled={config.is_active}>
                {config.is_active ? "当前使用中" : "设为当前使用"}
              </Button>
              <Button icon={<ApiOutlined />} onClick={handleTest} loading={testResult?.status === "loading"}>
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

// ── 主页面 ────────────────────────────────────────────────────────────────
export default function TextConfigPage() {
  const mountedRef = useRef(false);
  const [loading, setLoading] = useState(true);
  const [configs, setConfigs] = useState<TextProviderConfig[]>([]);
  const [quickVisible, setQuickVisible] = useState(false);
  const [quickLoading, setQuickLoading] = useState(false);
  const [quickError, setQuickError] = useState("");
  const [quickErrorDetail, setQuickErrorDetail] = useState<{ message?: string; suggest_alternatives?: Array<{ name: string; base_url: string; type: string; models: string[]; reason: string }> } | null>(null);
  // L-4（2026-10-04）：配置来源回显——环境变量（AI_API_KEY 等）优先级高于库内配置，
  // 此前对页面完全黑盒；env 覆盖时提示“页面保存不生效”，避免用户反复保存无效莫名。
  const [configSource, setConfigSource] = useState<string>("");
  const quickKeyRef = useRef("");

  useEffect(() => {
    mountedRef.current = true;
    loadAll();
    return () => { mountedRef.current = false; };
  }, []);

  const loadAll = useCallback(async () => {
    try {
      const { configs: c } = await getTextProviderConfigs();
      if (mountedRef.current) setConfigs(c);
    } catch {
      if (mountedRef.current) message.error("加载文本模型配置失败");
    } finally {
      if (mountedRef.current) setLoading(false);
    }
    // 来源回显为辅助信息，失败静默忽略，不影响主流程
    try {
      const cfg = await getAIConfig();
      if (mountedRef.current) setConfigSource(cfg.config_source || "");
    } catch { /* ignore */ }
  }, []);

  const handleQuickSetup = useCallback(async () => {
    const key = quickKeyRef.current;
    if (!key) { message.warning("请输入 API Key"); return; }
    setQuickLoading(true);
    setQuickError("");
    setQuickErrorDetail(null);
    try {
      const result = await quickSetup({ api_key: key });
      if (result.success) {
        const provider = result.tested_providers?.[0];
        message.success(`自动配置成功：${provider?.name || result.message}`);
        setQuickVisible(false);
        loadAll();
      } else {
        setQuickError(result.message || "配置失败");
      }
    } catch (err: any) {
      const detail = err?.response?.data?.detail;
      if (detail && typeof detail === "object" && (detail as any).suggest_alternatives) {
        setQuickError((detail as any).message || "配置失败");
        setQuickErrorDetail(detail as any);
      } else {
        setQuickError(extractApiError(err));
      }
    } finally {
      setQuickLoading(false);
    }
  }, [loadAll]);

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
        <ThunderboltOutlined /> 文本模型配置
      </Typography.Title>
      <Typography.Paragraph type="secondary" style={{ marginTop: 0, marginBottom: 20 }}>
        每个厂商的配置独立保存、互不干扰。点击「编辑」展开表单填写后保存，点「设为当前使用」切换活跃厂商。
      </Typography.Paragraph>

      {configSource === "env" && (
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 16 }}
          message="当前生效配置来自环境变量（AI_API_KEY / DEEPSEEK_API_KEY / OPENAI_API_KEY）"
          description="环境变量优先级高于本页保存的配置：本页保存/切换厂商不会改变实际调用参数。如需使用页面配置，请移除对应环境变量后重启服务。"
        />
      )}

      <Row gutter={[16, 16]}>
        {configs.map(c => (
          <ProviderCard key={c.provider_name} config={c} onSaved={loadAll} />
        ))}
      </Row>

      <Card style={{ marginTop: 16 }} size="small">
        <Space wrap>
          <Button icon={<RocketOutlined />} onClick={() => setQuickVisible(true)}>
            一键自动配置
          </Button>
          <Button icon={<ExperimentOutlined />} onClick={() => setQuickVisible(true)}>
            尝试 Agnes AI
          </Button>
          <span style={{ color: "#999", fontSize: 12 }}>提示：在卡片中填写 API Key 后点击「设为当前使用」即可切换活跃厂商</span>
        </Space>
      </Card>

      <Modal
        title="一键自动配置"
        open={quickVisible}
        onCancel={() => { setQuickVisible(false); setQuickError(""); }}
        footer={null}
        width={520}
      >
        <Steps
          current={quickError ? 2 : 1}
          items={[
            { title: "输入 API Key", description: "填写你的 Agnes AI API Key" },
            { title: "自动测试", description: "系统自动测试并选择最佳模型" },
          ]}
          style={{ marginBottom: 16 }}
        />
        <Form layout="vertical">
          <Form.Item label="API Key" required>
            <Input.Password
              placeholder="sk-..."
              autoComplete="new-password"
              onChange={e => { quickKeyRef.current = e.target.value; }}
            />
          </Form.Item>
          <Button type="primary" block loading={quickLoading} onClick={handleQuickSetup}>
            开始配置
          </Button>
          {quickError && (
            <>
              <Alert type="error" message={quickError} style={{ marginTop: 8 }} />
              {quickErrorDetail?.suggest_alternatives && quickErrorDetail.suggest_alternatives.length > 0 && (
                <div style={{ marginTop: 12 }}>
                  <div style={{ fontSize: 12, color: "#999", marginBottom: 8 }}>推荐国内可直接访问的替代方案：</div>
                  <Space wrap>
                    {quickErrorDetail.suggest_alternatives.map((p: { name: string; reason: string }) => (
                      <Button
                        key={p.name}
                        size="small"
                        type="default"
                        onClick={() => {
                          setQuickVisible(false);
                          message.info(`请在左侧卡片中为 ${p.name} 填写 API Key 并保存`);
                        }}
                      >
                        {p.name} <span style={{ color: "#aaa", fontSize: 11 }}>{p.reason}</span>
                      </Button>
                    ))}
                  </Space>
                </div>
              )}
            </>
          )}
        </Form>
      </Modal>
    </div>
  );
}
