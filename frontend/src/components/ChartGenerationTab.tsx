import { useCallback, useEffect, useRef, useState } from "react";
import {
  Alert, Card, Descriptions, Spin, Tag, Typography,
} from "antd";
import {
  ApartmentOutlined, BarChartOutlined, BranchesOutlined, CheckCircleOutlined,
  FundOutlined, InfoCircleOutlined, StopOutlined,
} from "@ant-design/icons";
import { autoOrchestrateSection, type AutoOrchestrateSectionResponse } from "../api";
import { extractApiError } from "../utils";

const { Text, Paragraph } = Typography;

export interface ChartGenerationTabProps {
  projectId: number;
  sectionId: number;
  /** 当前章节标题 */
  sectionTitle: string;
  /** 当前写作长度档位（保留参数兼容父组件；自动编排不再依赖人工档位选择） */
  writingLength: string;
  /** 自动插入图表后通知父组件刷新章节详情 */
  onGenerated: () => void;
}

const CHART_TYPE_LABELS: Record<string, string> = {
  flowchart: "流程图",
  gantt: "甘特图",
  architecture: "架构图",
  labor: "劳动力配置图",
  comparison: "对比图",
  layout: "平面布置图",
  timeline: "里程碑时间线",
};

const SKIPPED_REASON_LABELS: Record<string, string> = {
  not_needed: "根据章节标题与内容判断，本章节无需配图",
  chart_fence_exists: "正文已包含图表，已自动去重跳过（防止双图与图号虚跳）",
  validation_failed: "图表载荷校验未通过，已容错跳过（不写入坏块、不占图号）",
  payload_generation_failed: "图表载荷生成失败，已跳过",
  content_empty_fallback_chart_field: "正文为空，图表数据已降级保存到章节图表字段",
  unsupported_chart_type: "暂不支持该图表类型",
  section_not_found: "章节不存在",
};

interface TabState {
  loading: boolean;
  result: AutoOrchestrateSectionResponse | null;
  error: string | null;
}

/**
 * 图表自动编排面板（2026-10-17 重构，需求 1/3/4）
 *
 * 图表全部自动生成：挂载时自动执行「分析 → 生成载荷 → 校验修复 →
 * 正文最佳位置插入 → 落库」闭环，不提供任何人工配图/生图按钮。
 * 面板只读展示编排结论与载荷预览。
 */
export default function ChartGenerationTab({
  projectId, sectionId, onGenerated,
}: ChartGenerationTabProps) {
  const [state, setState] = useState<TabState>({ loading: false, result: null, error: null });
  const lastOrchestratedRef = useRef<number | null>(null);

  const runOrchestration = useCallback(async (pid: number, sid: number) => {
    setState({ loading: true, result: null, error: null });
    try {
      const res = await autoOrchestrateSection(pid, sid);
      setState({ loading: false, result: res, error: null });
      if (res.inserted) onGenerated();
    } catch (err) {
      setState({ loading: false, result: null, error: extractApiError(err) || "自动编排失败" });
    }
  }, [onGenerated]);

  useEffect(() => {
    // 同一章节只自动编排一次，避免父组件刷新（onGenerated）造成死循环
    if (lastOrchestratedRef.current === sectionId) return;
    lastOrchestratedRef.current = sectionId;
    runOrchestration(projectId, sectionId);
  }, [projectId, sectionId, runOrchestration]);

  const analysis = state.result?.analysis ?? null;
  const payload = state.result?.payload ?? null;

  return (
    <div style={{ padding: "12px 0" }}>
      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 16 }}
        message="图表自动编排"
        description="系统根据章节标题与内容自动判断是否需要配图，自动生成图表并插入正文最佳位置（图文逻辑连贯）。全过程自动完成，无需人工操作。"
      />

      {state.loading && (
        <div style={{ textAlign: "center", padding: "32px 0" }}>
          <Spin tip="正在自动分析章节图表需求并生成配图..." />
        </div>
      )}

      {state.error && (
        <Alert type="error" showIcon message="自动编排失败" description={state.error} />
      )}

      {state.result && analysis && (
        <Card size="small" title={<span><BarChartOutlined /> 编排结论</span>}>
          {analysis.needed ? (
            <>
              <Descriptions size="small" column={1} style={{ marginBottom: 12 }}>
                <Descriptions.Item label="判定结果">
                  <Tag color="green" icon={<CheckCircleOutlined />}>需要配图</Tag>
                  <Tag color="blue" icon={
                    analysis.chart_type === "architecture" ? <ApartmentOutlined />
                      : analysis.chart_type === "gantt" ? <FundOutlined />
                        : <BranchesOutlined />
                  }>
                    {CHART_TYPE_LABELS[analysis.chart_type] || analysis.chart_type}
                  </Tag>
                </Descriptions.Item>
                <Descriptions.Item label="判定依据">{analysis.reason}</Descriptions.Item>
                <Descriptions.Item label="数据来源">
                  {analysis.source === "ai" ? "AI 智能分析" : "本地规则推断"}
                </Descriptions.Item>
                {state.result.inserted && (
                  <Descriptions.Item label="插入位置">
                    已自动插入正文第 {analysis.insert_paragraph_index + 1} 个段落后（图文逻辑连贯位置）
                  </Descriptions.Item>
                )}
                {state.result.persisted_chart_field && (
                  <Descriptions.Item label="保存方式">
                    正文为空，图表数据已保存到章节图表字段（<Text code>{state.result.persisted_chart_field}</Text>）
                  </Descriptions.Item>
                )}
              </Descriptions>

              {state.result.inserted && (
                <Alert
                  type="success"
                  showIcon
                  style={{ marginBottom: 12 }}
                  message="图表已自动插入正文"
                  description="在「正文」TAB 的预览模式下可查看渲染效果；导出 DOCX 时将按「图 {章号}-{序号}」规范自动编号。"
                />
              )}

              {!state.result.inserted && !state.result.persisted_chart_field && (
                <Alert
                  type="warning"
                  showIcon
                  style={{ marginBottom: 12 }}
                  message="已跳过"
                  description={SKIPPED_REASON_LABELS[state.result.skipped_reason] || state.result.skipped_reason}
                />
              )}

              {state.result.validation_errors.length > 0 && (
                <Alert
                  type="error"
                  showIcon
                  style={{ marginBottom: 12 }}
                  message="校验错误详情"
                  description={
                    <ul style={{ margin: 0, paddingLeft: 18 }}>
                      {state.result.validation_errors.map((e, i) => <li key={i}>{e}</li>)}
                    </ul>
                  }
                />
              )}

              {payload && (
                <>
                  <Text type="secondary" style={{ fontSize: 12 }}>图表载荷预览（只读）：</Text>
                  <pre style={{
                    background: "#f5f5f5", padding: 8, borderRadius: 4,
                    fontSize: 12, maxHeight: 240, overflow: "auto", whiteSpace: "pre-wrap",
                    margin: "4px 0 0 0",
                  }}>
                    {typeof payload.code === "string" && payload.code
                      ? String(payload.code)
                      : JSON.stringify(payload, null, 2)}
                  </pre>
                </>
              )}
            </>
          ) : (
            <Alert
              type="success"
              showIcon
              icon={<StopOutlined />}
              message="本章节无需配图"
              description={SKIPPED_REASON_LABELS[state.result.skipped_reason] || analysis.reason || "纯文本论述章节保持正文为主"}
            />
          )}
        </Card>
      )}

      {state.result && !analysis && (
        <Alert
          type="warning"
          showIcon
          icon={<InfoCircleOutlined />}
          message={SKIPPED_REASON_LABELS[state.result.skipped_reason] || state.result.skipped_reason}
        />
      )}

      <Paragraph type="secondary" style={{ fontSize: 12, marginTop: 12 }}>
        说明：图表在正文生成完成后由系统自动编排生成；导出 DOCX 时按需实时渲染
        （Mermaid.js 用于流程图/甘特图等，PIL 用于数据型图表），渲染失败自动跳过且不占图号。
      </Paragraph>
    </div>
  );
}
