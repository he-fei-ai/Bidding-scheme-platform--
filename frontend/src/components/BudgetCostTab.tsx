import { useCallback, useEffect, useState } from "react";
import {
  Alert, Button, Card, Col, Descriptions, Divider, Form, InputNumber, List,
  Progress, Row, Space, Spin, Statistic, Table, Tag, Typography, message, Modal,
} from "antd";
import {
  AlertOutlined, DollarOutlined, ReloadOutlined, SaveOutlined,
  ThunderboltOutlined, WarningOutlined, DollarCircleOutlined,
} from "@ant-design/icons";
import {
  getPromptBudget, updatePromptBudget, getBudgetPrediction, getBudgetSuggestions,
  getCostSuggestions, compareModelCosts, getCostOptimizationSummary,
  getAlertThresholds, updateAlertThresholds,
} from "../api";
import type { PromptBudget } from "../api";

const { Text, Paragraph, Title } = Typography;

export default function BudgetCostTab() {
  const [budget, setBudget] = useState<PromptBudget | null>(null);
  const [prediction, setPrediction] = useState<any>(null);
  const [suggestions, setSuggestions] = useState<any[]>([]);
  const [costSuggestions, setCostSuggestions] = useState<any[]>([]);
  const [modelCompare, setModelCompare] = useState<any[]>([]);
  const [optimization, setOptimization] = useState<any>(null);
  const [thresholds, setThresholds] = useState<any>(null);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [editingThreshold, setEditingThreshold] = useState(false);
  const [thresholdForm, setThresholdForm] = useState<Record<string, number>>({});

  const fetchData = useCallback(async () => {
    setLoading(true);
    try {
      const [b, pred, sugg, costSugg, cmp, opt, thr] = await Promise.all([
        getPromptBudget(),
        getBudgetPrediction(),
        getBudgetSuggestions(),
        getCostSuggestions(),
        compareModelCosts(),
        getCostOptimizationSummary(),
        getAlertThresholds(),
      ]);
      setBudget(b);
      setPrediction(pred);
      setSuggestions(Array.isArray(sugg) ? sugg : sugg.suggestions || []);
      setCostSuggestions(Array.isArray(costSugg) ? costSugg : costSugg.suggestions || []);
      setModelCompare(Array.isArray(cmp) ? cmp : cmp.comparison || []);
      setOptimization(opt);
      setThresholds(thr);
      setThresholdForm({
        monthly_usage_ratio: thr.monthly_usage_ratio ?? 0.8,
        daily_usage_ratio: thr.daily_usage_ratio ?? 0.8,
        token_spike: thr.token_spike ?? 3,
      });
    } catch {
      // handled by interceptor
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { fetchData(); }, [fetchData]);

  const handleSaveBudget = async () => {
    if (!budget) return;
    setSaving(true);
    try {
      await updatePromptBudget({ monthly_limit: budget.monthly_limit, daily_limit: budget.daily_limit });
      message.success("预算已更新");
      fetchData();
    } catch {
      message.error("保存失败");
    } finally {
      setSaving(false);
    }
  };

  const handleSaveThresholds = async () => {
    setSaving(true);
    try {
      await updateAlertThresholds(thresholdForm);
      message.success("告警阈值已更新");
      setEditingThreshold(false);
      fetchData();
    } catch {
      message.error("保存失败");
    } finally {
      setSaving(false);
    }
  };

  if (loading) return <Spin size="large" style={{ display: "block", marginTop: 80 }} />;

  const monthlyRatio = budget ? Math.round(budget.usage_ratio * 100) : 0;
  const dailyRatio = budget ? Math.round(budget.daily_ratio * 100) : 0;

  return (
    <div>
      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 16 }}
        message="预算与成本管理"
        description="管理 AI 提示词调用的月度/日度预算上限，查看成本预测和优化建议。"
      />

      <Row gutter={[16, 16]} style={{ marginBottom: 16 }}>
        <Col xs={24} md={8}>
          <Card title={<Space><DollarCircleOutlined /> 月度预算</Space>} size="small"
            extra={
              <Space>
                <Button size="small" icon={<ReloadOutlined />} onClick={fetchData} />
                <Button size="small" type="primary" icon={<SaveOutlined />} loading={saving} onClick={handleSaveBudget}>保存</Button>
              </Space>
            }>
            <Row gutter={12}>
              <Col span={8}><Statistic title="月上限" value={budget?.monthly_limit || 0} prefix={<DollarOutlined />} valueStyle={{ fontSize: 16 }} /></Col>
              <Col span={8}><Statistic title="本月已用" value={budget?.current_month_usage || 0} valueStyle={{ fontSize: 16, color: monthlyRatio > 80 ? "#ff4d4f" : "#1890ff" }} /></Col>
              <Col span={8}><Statistic title="使用率" value={monthlyRatio} suffix="%" valueStyle={{ fontSize: 16, color: monthlyRatio > 80 ? "#ff4d4f" : monthlyRatio > 50 ? "#faad14" : "#52c41a" }} /></Col>
            </Row>
            <div style={{ marginTop: 12 }}>
              <Progress percent={monthlyRatio} strokeColor={monthlyRatio > 80 ? "#ff4d4f" : monthlyRatio > 50 ? "#faad14" : "#52c41a"} />
            </div>
            <div style={{ marginTop: 12 }}>
              <Text type="secondary">月度上限：</Text>
              <InputNumber
                value={budget?.monthly_limit}
                onChange={(v) => setBudget(b => b ? { ...b, monthly_limit: v || 0 } : b)}
                min={0}
                style={{ width: 160, marginLeft: 8 }}
                size="small"
              />
            </div>
          </Card>
        </Col>
        <Col xs={24} md={8}>
          <Card title={<Space><DollarCircleOutlined /> 日度预算</Space>} size="small">
            <Row gutter={12}>
              <Col span={8}><Statistic title="日上限" value={budget?.daily_limit || 0} prefix={<DollarOutlined />} valueStyle={{ fontSize: 16 }} /></Col>
              <Col span={8}><Statistic title="今日已用" value={budget?.current_day_usage || 0} valueStyle={{ fontSize: 16, color: dailyRatio > 80 ? "#ff4d4f" : "#1890ff" }} /></Col>
              <Col span={8}><Statistic title="使用率" value={dailyRatio} suffix="%" valueStyle={{ fontSize: 16, color: dailyRatio > 80 ? "#ff4d4f" : dailyRatio > 50 ? "#faad14" : "#52c41a" }} /></Col>
            </Row>
            <div style={{ marginTop: 12 }}>
              <Progress percent={dailyRatio} strokeColor={dailyRatio > 80 ? "#ff4d4f" : dailyRatio > 50 ? "#faad14" : "#52c41a"} />
            </div>
            <div style={{ marginTop: 12 }}>
              <Text type="secondary">日度上限：</Text>
              <InputNumber
                value={budget?.daily_limit}
                onChange={(v) => setBudget(b => b ? { ...b, daily_limit: v || 0 } : b)}
                min={0}
                style={{ width: 160, marginLeft: 8 }}
                size="small"
              />
            </div>
          </Card>
        </Col>
        <Col xs={24} md={8}>
          <Card title={<Space><AlertOutlined /> 告警阈值</Space>} size="small"
            extra={
              <Button size="small" type={editingThreshold ? "default" : "primary"}
                onClick={() => editingThreshold ? handleSaveThresholds() : setEditingThreshold(true)}
                loading={saving && editingThreshold}>
                {editingThreshold ? "保存阈值" : "编辑阈值"}
              </Button>
            }>
            {editingThreshold ? (
              <Space direction="vertical" style={{ width: "100%" }}>
                <div>
                  <Text type="secondary" style={{ fontSize: 12 }}>月度使用率告警：</Text>
                  <InputNumber value={thresholdForm.monthly_usage_ratio} onChange={(v) => setThresholdForm(t => ({ ...t, monthly_usage_ratio: (v || 0) / 100 }))} min={0} max={1} step={0.05} formatter={(v) => `${(v || 0) * 100}%`} parser={(v) => parseFloat(String(v).replace("%", "")) / 100} size="small" />
                </div>
                <div>
                  <Text type="secondary" style={{ fontSize: 12 }}>日度使用率告警：</Text>
                  <InputNumber value={thresholdForm.daily_usage_ratio} onChange={(v) => setThresholdForm(t => ({ ...t, daily_usage_ratio: (v || 0) / 100 }))} min={0} max={1} step={0.05} formatter={(v) => `${(v || 0) * 100}%`} parser={(v) => parseFloat(String(v).replace("%", "")) / 100} size="small" />
                </div>
                <div>
                  <Text type="secondary" style={{ fontSize: 12 }}>Token 突增倍数：</Text>
                  <InputNumber value={thresholdForm.token_spike} onChange={(v) => setThresholdForm(t => ({ ...t, token_spike: v || 3 }))} min={1} max={10} step={1} size="small" style={{ width: 100 }} />
                </div>
              </Space>
            ) : (
              <Descriptions column={1} size="small">
                <Descriptions.Item label="月度使用率告警">{thresholds?.monthly_usage_ratio ? `${Math.round(thresholds.monthly_usage_ratio * 100)}%` : "未设置"}</Descriptions.Item>
                <Descriptions.Item label="日度使用率告警">{thresholds?.daily_usage_ratio ? `${Math.round(thresholds.daily_usage_ratio * 100)}%` : "未设置"}</Descriptions.Item>
                <Descriptions.Item label="Token 突增倍数">{thresholds?.token_spike ?? "未设置"}x</Descriptions.Item>
              </Descriptions>
            )}
          </Card>
        </Col>
      </Row>

      {prediction && (
        <Card title={<Space><ThunderboltOutlined /> 预算预测</Space>} size="small" style={{ marginBottom: 16 }}>
          <Row gutter={[16, 16]}>
            {prediction.months_to_exhaust !== undefined && (
              <Col xs={12} sm={8}>
                <Statistic title="预计耗尽时间" value={prediction.months_to_exhaust} suffix="个月" valueStyle={{ fontSize: 18, color: prediction.months_to_exhaust < 1 ? "#ff4d4f" : prediction.months_to_exhaust < 3 ? "#faad14" : "#52c41a" }} />
              </Col>
            )}
            {prediction.daily_average !== undefined && (
              <Col xs={12} sm={8}>
                <Statistic title="日均消耗" value={prediction.daily_average.toFixed(2)} prefix="¥" valueStyle={{ fontSize: 18 }} />
              </Col>
            )}
            {prediction.monthly_forecast !== undefined && (
              <Col xs={12} sm={8}>
                <Statistic title="本月预测消耗" value={prediction.monthly_forecast.toFixed(2)} prefix="¥" valueStyle={{ fontSize: 18, color: prediction.monthly_forecast > (budget?.monthly_limit || 0) ? "#ff4d4f" : "#1890ff" }} />
              </Col>
            )}
          </Row>
          {prediction.warning && (
            <Alert type="warning" message={prediction.warning} showIcon style={{ marginTop: 12 }} />
          )}
        </Card>
      )}

      <Row gutter={[16, 16]} style={{ marginBottom: 16 }}>
        <Col xs={24} md={12}>
          <Card title={<Space><WarningOutlined /> 预算优化建议</Space>} size="small">
            {suggestions.length === 0 ? (
              <Text type="secondary">暂无预算优化建议</Text>
            ) : (
              <List
                size="small"
                dataSource={suggestions}
                renderItem={(item: any) => (
                  <List.Item>
                    <List.Item.Meta
                      title={<Text strong style={{ fontSize: 13 }}>{item.title || item.suggestion}</Text>}
                      description={<Text type="secondary" style={{ fontSize: 12 }}>{item.detail || item.description}</Text>}
                    />
                  </List.Item>
                )}
              />
            )}
          </Card>
        </Col>
        <Col xs={24} md={12}>
          <Card title={<Space><DollarOutlined /> 成本优化建议</Space>} size="small">
            {costSuggestions.length === 0 ? (
              <Text type="secondary">暂无成本优化建议</Text>
            ) : (
              <List
                size="small"
                dataSource={costSuggestions}
                renderItem={(item: any) => (
                  <List.Item>
                    <List.Item.Meta
                      title={<Text strong style={{ fontSize: 13 }}>{item.title || item.suggestion}</Text>}
                      description={<Text type="secondary" style={{ fontSize: 12 }}>{item.detail || item.description || item.estimated_saving}</Text>}
                    />
                  </List.Item>
                )}
              />
            )}
          </Card>
        </Col>
      </Row>

      {modelCompare.length > 0 && (
        <Card title={<Space><DollarOutlined /> 模型成本对比</Space>} size="small" style={{ marginBottom: 16 }}>
          <Table
            dataSource={modelCompare}
            rowKey={(r: any) => r.model || r.provider || String(Math.random())}
            pagination={false}
            size="small"
            columns={[
              { title: "模型", dataIndex: "model", key: "model", render: (m: string) => <Text code style={{ fontSize: 12 }}>{m}</Text> },
              { title: "提供商", dataIndex: "provider", key: "provider", width: 100 },
              { title: "输入价格", dataIndex: "input_price", key: "input_price", width: 100, render: (v: number) => <Text>{v} ¥/1K tokens</Text> },
              { title: "输出价格", dataIndex: "output_price", key: "output_price", width: 100, render: (v: number) => <Text>{v} ¥/1K tokens</Text> },
              { title: "推荐场景", dataIndex: "use_case", key: "use_case", render: (v: string) => <Tag color="blue">{v}</Tag> },
            ]}
          />
        </Card>
      )}

      {optimization && (
        <Card title={<Space><ThunderboltOutlined /> 成本优化汇总</Space>} size="small">
          <Row gutter={[16, 16]}>
            {optimization.total_savings !== undefined && (
              <Col xs={12} sm={8}>
                <Statistic title="预计可节省" value={optimization.total_savings.toFixed(2)} prefix="¥" valueStyle={{ fontSize: 18, color: "#52c41a" }} />
              </Col>
            )}
            {optimization.savings_ratio !== undefined && (
              <Col xs={12} sm={8}>
                <Statistic title="节省比例" value={optimization.savings_ratio} suffix="%" valueStyle={{ fontSize: 18, color: "#52c41a" }} />
              </Col>
            )}
            {optimization.recalled_calls !== undefined && (
              <Col xs={12} sm={8}>
                <Statistic title="可复用调用" value={optimization.recalled_calls} valueStyle={{ fontSize: 18 }} />
              </Col>
            )}
          </Row>
          {optimization.recommendations && optimization.recommendations.length > 0 && (
            <div style={{ marginTop: 12 }}>
              <Divider style={{ margin: "8px 0" }} />
              <Text strong style={{ fontSize: 13 }}>优化建议：</Text>
              <List size="small" dataSource={optimization.recommendations} renderItem={(item: any) => (
                <List.Item><Text style={{ fontSize: 12 }}>{typeof item === "string" ? item : item.description || JSON.stringify(item)}</Text></List.Item>
              )} />
            </div>
          )}
        </Card>
      )}
    </div>
  );
}