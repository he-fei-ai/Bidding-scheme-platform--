import { useCallback, useState } from "react";
import {
  Alert, Button, Card, Col, Divider, Empty, Row, Space, Spin, Tag, Tooltip, Typography, message,
} from "antd";
import {
  ApartmentOutlined, BarChartOutlined, BranchesOutlined, FileTextOutlined,
  FundOutlined, LoadingOutlined, ReloadOutlined,
} from "@ant-design/icons";
import {
  arrangeFlowchart, generateFlowchart,
  generateGantt,
  arrangeArchitecture, generateArchitecture,
  arrangeLabor, generateLabor,
  arrangeComparison, generateComparison,
} from "../api";
import { extractApiError } from "../utils";

const { Text, Paragraph } = Typography;

export interface ChartGenerationTabProps {
  projectId: number;
  sectionId: number;
  /** 当前章节标题 */
  sectionTitle: string;
  /** 当前写作长度档位（影响劳动力表生成） */
  writingLength: string;
  /** 生成完成后通知父组件刷新章节详情 */
  onGenerated: () => void;
}

interface ChartCardState {
  loading: boolean;
  result: any | null;
  arrangeResult: { need_arrange: boolean; reason: string } | null;
  error: string | null;
}

const CHART_TYPES = [
  {
    key: "flowchart",
    label: "流程图",
    icon: <BranchesOutlined />,
    color: "#2B579A",
    arrangeApi: arrangeFlowchart,
    generateApi: generateFlowchart,
    resultKey: "mermaid_code",
    needsWritingLength: false,
  },
  {
    key: "gantt",
    label: "甘特图",
    icon: <FundOutlined />,
    color: "#E8A33D",
    arrangeApi: null,
    generateApi: generateGantt,
    resultKey: "mermaid_code",
    needsWritingLength: false,
  },
  {
    key: "architecture",
    label: "架构图",
    icon: <ApartmentOutlined />,
    color: "#70AD47",
    arrangeApi: arrangeArchitecture,
    generateApi: generateArchitecture,
    resultKey: "mermaid",
    needsWritingLength: false,
  },
  {
    key: "labor",
    label: "劳动力表",
    icon: <BarChartOutlined />,
    color: "#C00000",
    arrangeApi: arrangeLabor,
    generateApi: generateLabor,
    resultKey: "labor_data",
    needsWritingLength: true,
  },
  {
    key: "comparison",
    label: "对比表",
    icon: <FileTextOutlined />,
    color: "#7030A0",
    arrangeApi: arrangeComparison,
    generateApi: generateComparison,
    resultKey: "mermaid_code",
    needsWritingLength: false,
  },
] as const;

type ChartKey = typeof CHART_TYPES[number]["key"];

export default function ChartGenerationTab({
  projectId, sectionId, sectionTitle, writingLength, onGenerated,
}: ChartGenerationTabProps) {
  const [states, setStates] = useState<Record<ChartKey, ChartCardState>>(() =>
    Object.fromEntries(CHART_TYPES.map(t => [t.key, { loading: false, result: null, arrangeResult: null, error: null }])) as Record<ChartKey, ChartCardState>
  );

  const updateState = useCallback((key: ChartKey, patch: Partial<ChartCardState>) => {
    setStates(prev => ({ ...prev, [key]: { ...prev[key], ...patch } }));
  }, []);

  const handleArrange = useCallback(async (type: (typeof CHART_TYPES)[number]) => {
    if (!type.arrangeApi) return;
    const key = type.key;
    updateState(key, { loading: true, error: null });
    try {
      const res = await type.arrangeApi(projectId, sectionId);
      updateState(key, { loading: false, arrangeResult: res });
    } catch (err) {
      updateState(key, { loading: false, error: extractApiError(err) || "检查失败" });
    }
  }, [projectId, sectionId, updateState]);

  const handleGenerate = useCallback(async (type: (typeof CHART_TYPES)[number]) => {
    const key = type.key;
    updateState(key, { loading: true, error: null, result: null });
    try {
      let res: any;
      if (type.needsWritingLength) {
        res = await (type.generateApi as typeof generateLabor)(projectId, sectionId, writingLength);
      } else {
        res = await (type.generateApi as typeof generateFlowchart)(projectId, sectionId);
      }
      updateState(key, { loading: false, result: res });
      // 通知父组件刷新章节详情（后端已将图表数据落库）
      onGenerated();
      message.success(`${type.label}生成成功`);
    } catch (err) {
      updateState(key, { loading: false, error: extractApiError(err) || `${type.label}生成失败` });
    }
  }, [projectId, sectionId, writingLength, onGenerated, updateState]);

  return (
    <div style={{ padding: "12px 0" }}>
      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 16 }}
        message="图表生成"
        description="AI 根据章节内容自动生成流程图、甘特图、架构图等可视化图表。生成后图表数据自动保存到章节记录，可在正文预览中查看渲染效果。"
      />

      <Row gutter={[16, 16]}>
        {CHART_TYPES.map(type => {
          const state = states[type.key];
          return (
            <Col span={12} key={type.key}>
              <Card
                size="small"
                title={
                  <Space>
                    <span style={{ color: type.color }}>{type.icon}</span>
                    <Text strong>{type.label}</Text>
                    {state.arrangeResult?.need_arrange && <Tag color="orange">建议生成</Tag>}
                    {state.result && <Tag color="green">已生成</Tag>}
                  </Space>
                }
                extra={
                  <Tooltip title="重新生成">
                    <Button
                      size="small"
                      type="text"
                      icon={<ReloadOutlined />}
                      loading={state.loading}
                      onClick={() => {
                        updateState(type.key, { result: null, arrangeResult: null, error: null });
                        handleGenerate(type);
                      }}
                    />
                  </Tooltip>
                }
                style={{ height: "100%" }}
                styles={{ body: { padding: "12px" } }}
              >
                <div style={{ marginBottom: 12 }}>
                  <Space size="middle">
                    {type.arrangeApi && (
                      <Button
                        size="small"
                        onClick={() => handleArrange(type)}
                        loading={state.loading && !state.arrangeResult}
                        disabled={state.loading}
                      >
                        检查是否需要
                      </Button>
                    )}
                    <Button
                      size="small"
                      type="primary"
                      loading={state.loading && state.result === null}
                      disabled={state.loading}
                      onClick={() => handleGenerate(type)}
                    >
                      {state.loading ? <LoadingOutlined /> : null} 生成{type.label}
                    </Button>
                  </Space>
                </div>

                {state.arrangeResult && (
                  <Alert
                    type={state.arrangeResult.need_arrange ? "warning" : "success"}
                    showIcon
                    message={state.arrangeResult.need_arrange ? "建议生成" : "无需生成"}
                    description={state.arrangeResult.reason}
                    style={{ marginBottom: 8, fontSize: 12 }}
                  />
                )}

                {state.error && (
                  <Alert type="error" showIcon style={{ marginBottom: 8 }} message={state.error} />
                )}

                {state.result && (
                  <div style={{ marginTop: 8 }}>
                    <Divider style={{ margin: "8px 0" }} />
                    <Text type="secondary" style={{ fontSize: 12 }}>生成结果：</Text>
                    {type.resultKey === "mermaid_code" || type.resultKey === "mermaid" ? (
                      <pre style={{
                        background: "#f5f5f5", padding: 8, borderRadius: 4,
                        fontSize: 12, maxHeight: 200, overflow: "auto", whiteSpace: "pre-wrap",
                        margin: "4px 0 0 0",
                      }}>
                        {state.result[type.resultKey]}
                      </pre>
                    ) : (
                      <pre style={{
                        background: "#f5f5f5", padding: 8, borderRadius: 4,
                        fontSize: 12, maxHeight: 200, overflow: "auto", whiteSpace: "pre-wrap",
                        margin: "4px 0 0 0",
                      }}>
                        {JSON.stringify(state.result[type.resultKey], null, 2)}
                      </pre>
                    )}
                    <Text type="secondary" style={{ fontSize: 11, display: "block", marginTop: 4 }}>
                      图表已保存到章节记录，在「正文」TAB 的预览模式下可查看渲染效果。
                    </Text>
                  </div>
                )}
              </Card>
            </Col>
          );
        })}
      </Row>
    </div>
  );
}