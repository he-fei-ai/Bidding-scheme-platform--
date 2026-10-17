import { useCallback, useEffect, useState } from "react";
import {
  Alert, Button, Card, Col, Descriptions, List, Radio, Row, Select,
  Space, Spin, Statistic, Table, Tag, Typography, message,
} from "antd";
import {
  GlobalOutlined, ReloadOutlined, SaveOutlined, TranslationOutlined,
} from "@ant-design/icons";
import {
  getLanguages, setLanguage, getPromptTemplates, translateText,
} from "../api";
import type { LanguageInfo } from "../api";
import PageHeader from "../components/PageHeader";

const { Text, Title, Paragraph } = Typography;

export default function I18nSettingsPage() {
  const [languages, setLanguages] = useState<LanguageInfo[]>([]);
  const [defaultLanguage, setDefaultLanguage] = useState("");
  const [currentLanguage, setCurrentLanguage] = useState<string | null>(null);
  const [promptTemplates, setPromptTemplates] = useState<Record<string, string>>({});
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [translateKey, setTranslateKey] = useState("");
  const [translateResult, setTranslateResult] = useState("");

  const fetchData = useCallback(async () => {
    setLoading(true);
    try {
      const langRes = await getLanguages();
      setLanguages(langRes.languages || []);
      setDefaultLanguage(langRes.default_language || "zh-CN");
      const current = localStorage.getItem("i18n_language") || langRes.default_language || "zh-CN";
      setCurrentLanguage(current);

      const promptRes = await getPromptTemplates(current);
      setPromptTemplates(promptRes.prompts || {});
    } catch {
      // handled by interceptor
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchData();
  }, [fetchData]);

  const handleLanguageChange = async (lang: string) => {
    setSaving(true);
    try {
      await setLanguage(lang);
      setCurrentLanguage(lang);
      localStorage.setItem("i18n_language", lang);
      message.success(`语言已切换为 ${lang}`);
      // 刷新 prompt templates
      const promptRes = await getPromptTemplates(lang);
      setPromptTemplates(promptRes.prompts || {});
    } catch {
      message.error("切换语言失败");
    } finally {
      setSaving(false);
    }
  };

  const handleTranslate = async () => {
    if (!translateKey.trim() || !currentLanguage) return;
    try {
      const res = await translateText(translateKey.trim(), currentLanguage);
      setTranslateResult(res.text || "无翻译结果");
    } catch {
      message.error("翻译失败");
    }
  };

  if (loading) return <Spin size="large" style={{ display: "block", marginTop: 80 }} />;

  const templateEntries = Object.entries(promptTemplates);

  return (
    <div>
      <PageHeader
        icon={<GlobalOutlined />}
        title="国际化设置"
        description="管理系统的多语言支持：切换界面语言、查看翻译提示词模板、测试翻译功能。"
        extra={
          <Button icon={<ReloadOutlined />} onClick={fetchData} loading={loading}>
            刷新
          </Button>
        }
      />

      <Row gutter={[16, 16]} style={{ marginBottom: 16 }}>
        <Col xs={24} md={12}>
          <Card title={<Space><GlobalOutlined /> 语言设置</Space>} size="small">
            <Space direction="vertical" style={{ width: "100%" }}>
              <div>
                <Text type="secondary" style={{ fontSize: 12 }}>默认语言：</Text>
                <Tag color="blue">{defaultLanguage}</Tag>
              </div>
              <div>
                <Text type="secondary" style={{ fontSize: 12 }}>当前语言：</Text>
                {currentLanguage && (
                  <Tag color="green" icon={<GlobalOutlined />}>
                    {languages.find(l => l.code === currentLanguage)?.nativeName || currentLanguage}
                  </Tag>
                )}
              </div>
              <div style={{ marginTop: 8 }}>
                <Text strong style={{ fontSize: 13 }}>切换语言：</Text>
                <Radio.Group
                  value={currentLanguage}
                  onChange={(e) => handleLanguageChange(e.target.value)}
                  optionType="button"
                  buttonStyle="solid"
                  size="small"
                  style={{ display: "block", marginTop: 8 }}
                >
                  <Row gutter={[8, 8]}>
                    {languages.map((lang) => (
                      <Col key={lang.code} span={8}>
                        <Radio.Button value={lang.code} style={{ width: "100%" }}>
                          {lang.nativeName}
                        </Radio.Button>
                      </Col>
                    ))}
                  </Row>
                </Radio.Group>
              </div>
              <div style={{ marginTop: 8, display: "flex", justifyContent: "space-between" }}>
                <Text type="secondary" style={{ fontSize: 12 }}>可用语言数</Text>
                <Statistic value={languages.length} valueStyle={{ fontSize: 20, color: "#1890ff" }} />
              </div>
            </Space>
          </Card>
        </Col>

        <Col xs={24} md={12}>
          <Card title={<Space><TranslationOutlined /> 翻译测试</Space>} size="small">
            <Space direction="vertical" style={{ width: "100%" }}>
              <div>
                <Text type="secondary" style={{ fontSize: 12 }}>输入翻译键（如 welcome_message）：</Text>
                <Space.Compact style={{ width: "100%", marginTop: 4 }}>
                  <input
                    type="text"
                    value={translateKey}
                    onChange={(e) => setTranslateKey(e.target.value)}
                    placeholder="输入翻译键..."
                    style={{
                      flex: 1,
                      padding: "4px 12px",
                      border: "1px solid #d9d9d9",
                      borderRadius: "4px 0 0 4px",
                      fontSize: 13,
                    }}
                    onKeyDown={(e) => e.key === "Enter" && handleTranslate()}
                  />
                  <Button
                    type="primary"
                    onClick={handleTranslate}
                    style={{ borderRadius: "0 4px 4px 0" }}
                  >
                    翻译
                  </Button>
                </Space.Compact>
              </div>
              {translateResult && (
                <Alert
                  type="success"
                  message={`翻译结果（${currentLanguage}）：`}
                  description={translateResult}
                  showIcon
                />
              )}
            </Space>
          </Card>
        </Col>
      </Row>

      <Card
        title={<Space><TranslationOutlined /> 当前语言提示词模板</Space>}
        size="small"
        extra={<Text type="secondary" style={{ fontSize: 12 }}>共 {templateEntries.length} 个模板</Text>}
      >
        {templateEntries.length === 0 ? (
          <Alert type="info" message="当前语言无自定义提示词模板" showIcon />
        ) : (
          <Table
            dataSource={templateEntries.map(([key, value]) => ({ key, value }))}
            rowKey="key"
            pagination={false}
            size="small"
            columns={[
              {
                title: "键名",
                dataIndex: "key",
                width: 240,
                render: (k: string) => <Text code style={{ fontSize: 12 }}>{k}</Text>
              },
              {
                title: "内容",
                dataIndex: "value",
                render: (v: string) => (
                  <Paragraph style={{ margin: 0, fontSize: 12, wordBreak: "break-word", whiteSpace: "pre-wrap" }}>
                    {v.length > 200 ? v.slice(0, 200) + "..." : v}
                  </Paragraph>
                )
              },
            ]}
          />
        )}
      </Card>
    </div>
  );
}