import { useEffect, useState, useCallback, useMemo } from "react";
import { useParams, useNavigate } from "react-router-dom";
import {
  Button, Card, Skeleton, message, Space, Typography, Tag, Input, Empty, Divider, Switch, Table, Tooltip,
  Progress, Select, Alert, Modal,
} from "antd";
const { Text } = Typography;
import { ClearOutlined, ArrowLeftOutlined, CheckCircleOutlined, ThunderboltOutlined, EditOutlined, CodeOutlined, DatabaseOutlined, TeamOutlined, ToolOutlined, FileTextOutlined, LoadingOutlined, DeleteOutlined, PlusOutlined, SaveOutlined } from "@ant-design/icons";
import { getProject, getAnalysis, generateSummary, generateScoring, confirmSummary, confirmScoring, clearAnalysis, analyzeBoq, confirmBoq, analyzeWorkInterface, confirmWorkInterface, analyzeConstructionMethod, confirmConstructionMethod, getFacts, saveFacts, generateFacts, confirmFacts, auditFactsConsistency } from "../api";
import { extractApiError, isValidSummary, parseScoringPoints, parseArrayData } from "../utils";
import type { ScoringPoint } from "../utils";
import type { FactsMissingValuePolicy, FactConflict } from "../api";
import type { AnalysisResult } from "../types";

const { TextArea } = Input;

export default function SummaryReviewPage() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const projectId = Number(id);
  const isProjectIdValid = !isNaN(projectId) && projectId > 0;

  const [projectName, setProjectName] = useState("");
  const [loading, setLoading] = useState(false);
  const [generatingSummary, setGeneratingSummary] = useState(false);
  const [generatingScoring, setGeneratingScoring] = useState(false);

  const [summaryText, setSummaryText] = useState("");
  const [summaryConfirmed, setSummaryConfirmed] = useState(false);

  const [scoringText, setScoringText] = useState("");
  const [scoringConfirmed, setScoringConfirmed] = useState(false);
  const [scoringPoints, setScoringPoints] = useState<ScoringPoint[]>([]);
  const [showRawJson, setShowRawJson] = useState(false);

  const [boqData, setBoqData] = useState<any[]>([]);
  const [boqText, setBoqText] = useState("");
  const [boqConfirmed, setBoqConfirmed] = useState(false);
  const [generatingBoq, setGeneratingBoq] = useState(false);

  const [workInterfaceData, setWorkInterfaceData] = useState<any[]>([]);
  const [workInterfaceText, setWorkInterfaceText] = useState("");
  const [workInterfaceConfirmed, setWorkInterfaceConfirmed] = useState(false);
  const [generatingWorkInterface, setGeneratingWorkInterface] = useState(false);

  const [constructionMethodData, setConstructionMethodData] = useState<any[]>([]);
  const [constructionMethodText, setConstructionMethodText] = useState("");
  const [constructionMethodConfirmed, setConstructionMethodConfirmed] = useState(false);

  const [factsData, setFactsData] = useState<any[]>([]);
  const [factsConfirmed, setFactsConfirmed] = useState(false);
  const [generatingFacts, setGeneratingFacts] = useState(false);
  // ENHANCE 2026-09-15：全局事实缺失值策略（fabricate / omit / placeholder）。
  // 默认 fabricate 保持历史行为；选 omit/placeholder 时不再编造缺值。
  const [factsPolicy, setFactsPolicy] = useState<FactsMissingValuePolicy>("fabricate");
  const [auditingFacts, setAuditingFacts] = useState(false);
  // ENHANCE 2026-10-04（事实人工管理缺口）：此前编辑只能靠「确认事实变量」
  // 一并落库（保存+确认绑死），改完不想立即确认为止没有出口，且切页即丢。
  // 现拆出独立的「保存修改」（POST /facts，confirmed 保持 0）。
  const [savingFacts, setSavingFacts] = useState(false);
  const [auditResult, setAuditResult] = useState<any[] | null>(null);
  // ENHANCE 2026-09-29（前后端契约）：审计的覆盖率与降级状态。旧实现只看
  // items.length，AI 异常返回空数组也能拿到绿字「一致性检查通过」——假的通过。
  const [auditMeta, setAuditMeta] = useState<{
    audit_status?: string;
    audited_sections?: number;
    total_sections?: number;
    truncated?: boolean;
    failed_groups?: number;
    skipped_placeholder?: number;
    audited_at?: string | null;
  } | null>(null);
  // 分类中文名以后端 FACT_CATEGORIES 为基准（旧 catMap 缺 implementation，直接显示英文）
  const [factCategories, setFactCategories] = useState<Record<string, string>>({});
  // BUG-FIX 2026-10-06（后端算了前端零消费）：GET /facts、/facts/generate、
  // /facts 保存响应都已下发 fact_conflicts（同 label 不同 key/值的内部矛盾，
  // 如「项目经理：张伟」与「pm：李强」并存）。此前前端类型有定义但页面
  // 不渲染——用户确认前看不到矛盾，两个值会同时注入正文。
  const [factConflicts, setFactConflicts] = useState<FactConflict[]>([]);
  // ENHANCE 2026-10-04：类别列改为可编辑下拉。选项正本仍是后端下发的
  // categories；无 analysis 行时 GET /facts 不返该键，用内置表兜底，
  // 保证首次提取前/接口异常时也能纠正分类。
  const factCategoryOptions = useMemo(() => {
    const map = Object.keys(factCategories).length > 0
      ? factCategories
      : { basic: "基本信息", project: "项目概况", bidder: "投标人", commitment: "承诺事项", schedule: "工期计划", quality: "质量标准", safety: "安全目标", personnel: "人员配置", technical: "技术参数", implementation: "实施做法" };
    return Object.entries(map).map(([value, label]) => ({ value, label }));
  }, [factCategories]);
  const [generatingConstructionMethod, setGeneratingConstructionMethod] = useState(false);
  const [generatingPhase, setGeneratingPhase] = useState<string>("");

  const GENERATING_PHASES: Record<string, { message: string; percent: number }> = {
    summary: { message: "正在生成招标摘要，AI 分析招标文件...", percent: 30 },
    scoring: { message: "正在识别评分点，AI 提取技术要求...", percent: 50 },
    boq: { message: "正在分析清单工程量，AI 提取项目特征...", percent: 40 },
    work_interface: { message: "正在分析工作界面，AI 识别接口关系...", percent: 40 },
    construction_method: { message: "正在分析工程作法，AI 提取技术要求...", percent: 40 },
    facts: { message: "正在提取事实变量，AI 分析项目关键信息...", percent: 50 },
    audit_facts: { message: "正在进行事实一致性审计...", percent: 70 },
  };

  const hasValidScoringPoints = scoringPoints.length > 0;
  const hasScoringText = !!scoringText && scoringText !== "[]" && scoringText.trim().length > 5;

  // parseScoringPoints / isValidSummary / parseArrayData 已抽为 utils 纯函数（组件级可测化 2026-10-05）

  const syncScoringPoints = useCallback((text: string) => {
    setScoringText(text);
    setScoringPoints(parseScoringPoints(text));
  }, []);

  const syncFromPoints = useCallback((points: ScoringPoint[]) => {
    setScoringPoints(points);
    const json = JSON.stringify(points, null, 2);
    setScoringText(json);
  }, []);

  const handlePointFieldChange = (index: number, field: keyof ScoringPoint, value: string) => {
    const updated = [...scoringPoints];
    updated[index] = { ...updated[index], [field]: value };
    syncFromPoints(updated);
    setScoringConfirmed(false);
  };

  const loadData = useCallback(async () => {
    if (!isProjectIdValid) return;
    setLoading(true);
    try {
      const p = await getProject(projectId, { silent: true });
      setProjectName(p.name);
    } catch (e: any) {
      if (e?.response?.status === 404) {
        localStorage.removeItem("tzb_active_project_id");
        message.warning("项目不存在，已返回首页");
        navigate("/");
        return;
      } else {
        message.error("加载项目信息失败");
      }
    }
    try {
      const a = await getAnalysis(projectId);
      setSummaryText(a.summary_json || "");
      syncScoringPoints(a.scoring_points_json || "[]");
      setSummaryConfirmed(a.confirmed_summary || false);
      setScoringConfirmed(a.confirmed_scoring || false);
      setBoqData(parseArrayData(a.boq_json));
      setBoqText(a.boq_json || "[]");
      setBoqConfirmed(a.confirmed_boq || false);
      setWorkInterfaceData(parseArrayData(a.work_interface_json));
      setWorkInterfaceText(a.work_interface_json || "[]");
      setWorkInterfaceConfirmed(a.confirmed_work_interface || false);
      setConstructionMethodData(parseArrayData(a.construction_method_json));
      setConstructionMethodText(a.construction_method_json || "[]");
      setConstructionMethodConfirmed(a.confirmed_construction_method || false);
      const f = await getFacts(projectId);
      setFactsData(f.items || []);
      setFactsConfirmed(f.confirmed);
      setFactConflicts(f.fact_conflicts || []);
      // 回显上次使用的缺失值策略，避免用户以为一直是默认档
      if (f.missing_value_policy) setFactsPolicy(f.missing_value_policy);
      // ENHANCE 2026-09-29：分类中文名与上次审计结果从后端取，刷新页面不再丢失
      if (f.categories) setFactCategories(f.categories);
      if (f.last_audit) {
        setAuditResult(f.last_audit.items || []);
        setAuditMeta({
          // BUG-FIX 2026-10-04：旧数据行里只有 `status`（新后端双写两键），
          // 回退读它，免得历史审计的降级标签在刷新后静默消失。
          audit_status: f.last_audit.audit_status ?? f.last_audit.status,
          audited_sections: f.last_audit.audited_sections,
          total_sections: f.last_audit.total_sections,
          truncated: f.last_audit.truncated,
          failed_groups: f.last_audit.failed_groups,
          skipped_placeholder: f.last_audit.skipped_placeholder,
          audited_at: f.audited_at ?? null,
        });
      }
    } catch (e) {
      console.warn("加载分析结果失败:", e);
    } finally {
      setLoading(false);
    }
  }, [projectId, isProjectIdValid]);

  useEffect(() => {
    if (!isProjectIdValid) {
      message.error("无效的项目ID");
      return;
    }
    loadData();
  }, [loadData, isProjectIdValid]);

  const handleGenerateSummary = async () => {
    setGeneratingSummary(true);
    setGeneratingPhase("summary");
    try {
      const result = await generateSummary(projectId);
      setSummaryText(result.summary_json);
      message.success("招标摘要已生成");
    } catch (e: any) {
      message.error(extractApiError(e));
    } finally {
      setGeneratingSummary(false);
      setGeneratingPhase("");
    }
  };

  const handleGenerateScoring = async () => {
    setGeneratingScoring(true);
    setGeneratingPhase("scoring");
    try {
      const result = await generateScoring(projectId);
      if (result.summary_json) {
        setSummaryText(result.summary_json);
        setSummaryConfirmed(false);
      }
      syncScoringPoints(result.scoring_points_json);
      setShowRawJson(false);
      // 评分模块配套提取的全局事实变量：自动刷新事实区 + 从 DB 刷新确认态
      if (result.global_facts && result.global_facts.length > 0) {
        setFactsData(result.global_facts);
        setFactsConfirmed(false);
      }
      // 从 DB 刷新 facts 确认态（后端已保存，确保前后端一致）
      const fAfterScoring = await getFacts(projectId);
      setFactsData(fAfterScoring.items ?? []);
      setFactsConfirmed(fAfterScoring.confirmed ?? false);
      message.success("评分点已识别");
    } catch (e: any) {
      message.error(extractApiError(e));
    } finally {
      setGeneratingScoring(false);
      setGeneratingPhase("");
    }
  };

  const handleConfirmSummary = async () => {
    try {
      await confirmSummary(projectId, summaryText);
      setSummaryConfirmed(true);
      message.success("摘要已确认");
    } catch {
      message.error("确认失败");
    }
  };

  const handleConfirmScoring = async () => {
    try {
      const jsonToSave = showRawJson ? scoringText : JSON.stringify(scoringPoints, null, 2);
      await confirmScoring(projectId, jsonToSave);
      if (!showRawJson) {
        setScoringText(jsonToSave);
      }
      setScoringConfirmed(true);
      message.success("评分点已确认");
    } catch {
      message.error("确认失败");
    }
  };

  const handleClearSummary = async () => {
    try {
      await clearAnalysis(projectId);
      setSummaryText("");
      setSummaryConfirmed(false);
      message.success("招标摘要已清除");
    } catch {
      message.error("清除失败");
    }
  };

  const handleClearScoring = async () => {
    try {
      await clearAnalysis(projectId);
      setScoringText("");
      setScoringPoints([]);
      setScoringConfirmed(false);
      message.success("评分要求已清除");
    } catch {
      message.error("清除失败");
    }
  };

  const handleAnalyzeBoq = async () => {
    setGeneratingBoq(true);
    setGeneratingPhase("boq");
    try {
      const result = await analyzeBoq(projectId);
      const data = parseArrayData(result.boq_json);
      setBoqData(data);
      setBoqText(result.boq_json);
      setBoqConfirmed(false);
      if (data.length === 0) {
        message.warning("AI 未提取到有效的清单工程量数据，请检查招标文件内容后重试");
      } else {
        message.success(`清单工程量分析完成，共提取 ${data.length} 项`);
      }
    } catch (e: any) {
      message.error(extractApiError(e));
    } finally {
      setGeneratingBoq(false);
      setGeneratingPhase("");
    }
  };

  const handleConfirmBoq = async () => {
    try {
      await confirmBoq(projectId);
      setBoqConfirmed(true);
      message.success("清单工程量分析已确认");
    } catch {
      message.error("确认失败");
    }
  };

  const handleAnalyzeWorkInterface = async () => {
    setGeneratingWorkInterface(true);
    setGeneratingPhase("work_interface");
    try {
      const result = await analyzeWorkInterface(projectId);
      const data = parseArrayData(result.work_interface_json);
      setWorkInterfaceData(data);
      setWorkInterfaceText(result.work_interface_json);
      setWorkInterfaceConfirmed(false);
      if (data.length === 0) {
        message.warning("AI 未提取到有效的工作界面数据，请检查招标文件内容后重试");
      } else {
        message.success(`工作界面分析完成，共提取 ${data.length} 项`);
      }
    } catch (e: any) {
      message.error(extractApiError(e));
    } finally {
      setGeneratingWorkInterface(false);
      setGeneratingPhase("");
    }
  };

  const handleConfirmWorkInterface = async () => {
    try {
      await confirmWorkInterface(projectId);
      setWorkInterfaceConfirmed(true);
      message.success("工作界面分析已确认");
    } catch {
      message.error("确认失败");
    }
  };

  const handleAnalyzeConstructionMethod = async () => {
    setGeneratingConstructionMethod(true);
    try {
      const result = await analyzeConstructionMethod(projectId);
      const data = parseArrayData(result.construction_method_json);
      setConstructionMethodData(data);
      setConstructionMethodText(result.construction_method_json);
      setConstructionMethodConfirmed(false);
      if (data.length === 0) {
        message.warning("AI 未提取到有效的工程作法数据，请检查招标文件内容后重试");
      } else {
        message.success(`工程作法分析完成，共提取 ${data.length} 项`);
      }
    } catch (e: any) {
      message.error(extractApiError(e));
    } finally {
      setGeneratingConstructionMethod(false);
    }
  };

  const handleConfirmConstructionMethod = async () => {
    try {
      await confirmConstructionMethod(projectId);
      setConstructionMethodConfirmed(true);
      message.success("工程作法分析已确认");
    } catch {
      message.error("确认失败");
    }
  };

  const scoringColumns = [
    {
      title: "评分项",
      dataIndex: "name",
      key: "name",
      render: (_: string, __: ScoringPoint, index: number) => (
        <Input
          value={scoringPoints[index]?.name || ""}
          onChange={(e) => handlePointFieldChange(index, "name", e.target.value)}
          variant="borderless"
          style={{ fontWeight: 500, padding: 0 }}
        />
      ),
    },
    {
      title: "权重",
      dataIndex: "weight",
      key: "weight",
      width: 100,
      render: (_: string, __: ScoringPoint, index: number) => (
        <Input
          value={scoringPoints[index]?.weight || ""}
          onChange={(e) => handlePointFieldChange(index, "weight", e.target.value)}
          variant="borderless"
          style={{ padding: 0 }}
        />
      ),
    },
    {
      title: "评分标准",
      dataIndex: "standard",
      key: "standard",
      render: (_: string, __: ScoringPoint, index: number) => (
        <Input.TextArea
          value={scoringPoints[index]?.standard || ""}
          onChange={(e) => handlePointFieldChange(index, "standard", e.target.value)}
          variant="borderless"
          autoSize={{ minRows: 1, maxRows: 3 }}
          style={{ padding: 0 }}
        />
      ),
    },
    {
      title: "来源",
      dataIndex: "source",
      key: "source",
      width: 160,
      render: (_: string, __: ScoringPoint, index: number) => (
        <Input
          value={scoringPoints[index]?.source || ""}
          onChange={(e) => handlePointFieldChange(index, "source", e.target.value)}
          variant="borderless"
          style={{ padding: 0, fontSize: 12 }}
        />
      ),
    },
  ];

  const renderAnalysisCard = (
    title: string,
    icon: React.ReactNode,
    data: any[],
    text: string,
    confirmed: boolean,
    generating: boolean,
    onGenerate: () => void,
    onConfirm: () => void,
    fields: { key: string; label: string }[],
  ) => (
    <Card
      title={
        <Space>
          {icon}
          <span>{title}</span>
          {confirmed && <Tag color="green" icon={<CheckCircleOutlined />}>已确认</Tag>}
        </Space>
      }
      style={{ marginBottom: 24 }}
      extra={
        <Space>
          {data.length > 0 && (
            <Button icon={<ClearOutlined />} onClick={async () => {
              try {
                await clearAnalysis(projectId);
                if (title.includes("清单")) { setBoqData([]); setBoqText(""); setBoqConfirmed(false); }
                else if (title.includes("界面")) { setWorkInterfaceData([]); setWorkInterfaceText(""); setWorkInterfaceConfirmed(false); }
                else { setConstructionMethodData([]); setConstructionMethodText(""); setConstructionMethodConfirmed(false); }
                message.success(`${title}已清除`);
              } catch { message.error("清除失败"); }
            }} size="small" danger>清除</Button>
          )}
          <Button type="primary" icon={<ThunderboltOutlined />} onClick={onGenerate} loading={generating}>
            {data.length > 0 ? "重新分析" : "开始分析"}
          </Button>
        </Space>
      }
    >
      {data.length > 0 ? (
        <Table
          dataSource={data}
          columns={fields.map((f) => ({
            title: f.label,
            dataIndex: f.key,
            key: f.key,
            render: (val: string) => val || "-",
          }))}
          rowKey={(record: any) => record.name || record.key || record.id}
          pagination={false}
          size="small"
          bordered
          expandable={{
            expandedRowRender: (record: any) => (
              <div style={{ margin: 0, whiteSpace: "pre-wrap", fontSize: 13 }}>
                {record.description || record.detail || record.technical_requirements || record.quality_standard || "-"}
              </div>
            ),
            rowExpandable: (record: any) => !!(record.description || record.detail || record.technical_requirements || record.quality_standard),
          }}
        />
      ) : text && text !== "[]" ? (
        <div style={{ padding: "16px 0", color: "#fa8c16", fontSize: 13 }}>
          <Empty description="数据格式异常，请尝试重新分析" />
        </div>
      ) : (
        <Empty description={`尚未分析，请先上传文件并点击"开始分析"`} />
      )}
      {data.length > 0 && (
        <Button
          type={confirmed ? "default" : "primary"}
          onClick={onConfirm}
          style={{ marginTop: 12 }}
          icon={confirmed ? <CheckCircleOutlined /> : undefined}
        >
          {confirmed ? "已确认（点击可重新确认）" : "确认分析结果"}
        </Button>
      )}
    </Card>
  );

  if (loading) {
    return <Skeleton active paragraph={{ rows: 8 }} />;
  }

  return (
    <div>
      <Space style={{ marginBottom: 16 }}>
        <Button icon={<ArrowLeftOutlined />} onClick={() => navigate(`/projects/${projectId}/files`)}>
          返回文件管理
        </Button>
        <Typography.Title level={4} style={{ margin: 0 }}>
          {projectName} — 分析与评分确认
        </Typography.Title>
      </Space>

      {generatingPhase && (
        <Card
          size="small"
          style={{ marginBottom: 16 }}
          styles={{ body: { padding: "12px 16px" } }}
        >
          <Space direction="vertical" style={{ width: "100%" }} size={8}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
              <Space>
                <LoadingOutlined style={{ color: "#1677ff" }} spin />
                <span style={{ fontSize: 13, color: "#595959" }}>AI 正在处理：</span>
                <Text strong style={{ fontSize: 13, color: "#1677ff" }}>
                  {GENERATING_PHASES[generatingPhase]?.message || "分析中..."}
                </Text>
              </Space>
              <Tag color="processing" icon={<LoadingOutlined />}>执行中</Tag>
            </div>
            <Progress
              percent={GENERATING_PHASES[generatingPhase]?.percent || 50}
              status="active"
              showInfo
              size="small"
            />
            <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
              {(["summary", "scoring", "boq", "work_interface", "construction_method", "facts", "audit_facts"] as const).map((phase) => {
                const cfg = GENERATING_PHASES[phase];
                const isActive = phase === generatingPhase;
                return (
                  <Tag
                    key={phase}
                    color={isActive ? "processing" : "default"}
                    style={{ fontSize: 11, margin: 0, opacity: isActive ? 1 : 0.5 }}
                  >
                    {isActive ? <LoadingOutlined style={{ marginRight: 4 }} /> : <CheckCircleOutlined style={{ color: "#52c41a", marginRight: 4 }} />}
                    {cfg?.message.replace("正在", "").replace("AI ", "").replace("，AI ...", "").slice(0, 12)}
                  </Tag>
                );
              })}
            </div>
          </Space>
        </Card>
      )}

      <Card
        title={
          <Space>
            <span>招标摘要</span>
            {summaryConfirmed && <Tag color="green" icon={<CheckCircleOutlined />}>已确认</Tag>}
          </Space>
        }
        style={{ marginBottom: 24 }}
        extra={
          <Space>
            {isValidSummary(summaryText) && (
              <Button icon={<ClearOutlined />} onClick={handleClearSummary} size="small" danger>
                清除
              </Button>
            )}
            <Button
              type="primary"
              icon={<ThunderboltOutlined />}
              onClick={handleGenerateSummary}
              loading={generatingSummary}
            >
              {isValidSummary(summaryText) ? "重新生成" : "生成摘要"}
            </Button>
          </Space>
        }
      >
        {isValidSummary(summaryText) ? (
          <TextArea
            value={summaryText}
            onChange={(e) => { setSummaryText(e.target.value); setSummaryConfirmed(false); }}
            rows={8}
            placeholder="编辑招标摘要内容..."
            style={{ fontSize: 14 }}
          />
        ) : (
          <Empty description={'尚未生成摘要，请先上传文件并点击"生成摘要"'} />
        )}
        {isValidSummary(summaryText) && (
          <Button
            type={summaryConfirmed ? "default" : "primary"}
            onClick={handleConfirmSummary}
            style={{ marginTop: 12 }}
            icon={summaryConfirmed ? <CheckCircleOutlined /> : undefined}
          >
            {summaryConfirmed ? "已确认（点击可重新确认）" : "确认摘要"}
          </Button>
        )}
      </Card>

      <Card
        title={
          <Space>
            <span>技术评分要求</span>
            {scoringConfirmed && <Tag color="green" icon={<CheckCircleOutlined />}>已确认</Tag>}
          </Space>
        }
        style={{ marginBottom: 24 }}
        extra={
          <Space>
            <Tooltip title={showRawJson ? "切换到表格视图" : "切换到 JSON 编辑"}>
              <Switch
                checkedChildren={<CodeOutlined />}
                unCheckedChildren={<EditOutlined />}
                checked={showRawJson}
                onChange={setShowRawJson}
              />
            </Tooltip>
            {(hasValidScoringPoints || (showRawJson && hasScoringText)) && (
              <Button icon={<ClearOutlined />} onClick={handleClearScoring} size="small" danger>
                清除
              </Button>
            )}
            <Button
              type="primary"
              icon={<ThunderboltOutlined />}
              onClick={handleGenerateScoring}
              loading={generatingScoring}
            >
              {hasValidScoringPoints ? "重新识别" : "识别评分点"}
            </Button>
          </Space>
        }
      >
        {hasValidScoringPoints || (showRawJson && hasScoringText) ? (
          showRawJson ? (
            <TextArea
              value={scoringText}
              onChange={(e) => { syncScoringPoints(e.target.value); setScoringConfirmed(false); }}
              rows={10}
              placeholder="编辑评分点列表（JSON 格式）..."
              style={{ fontSize: 14, fontFamily: "monospace" }}
            />
          ) : (
            <Table
              dataSource={scoringPoints}
              columns={scoringColumns}
              rowKey="id"
              pagination={false}
              size="small"
              bordered
              style={{ fontSize: 13 }}
            />
          )
        ) : (
          <Empty description={generatingScoring ? '正在识别评分点，请稍候...' : '尚未识别评分点，请先上传文件并点击"识别评分点"'} />
        )}
        {hasValidScoringPoints && (
          <Button
            type={scoringConfirmed ? "default" : "primary"}
            onClick={handleConfirmScoring}
            style={{ marginTop: 12 }}
            icon={scoringConfirmed ? <CheckCircleOutlined /> : undefined}
          >
            {scoringConfirmed ? "已确认（点击可重新确认）" : "确认评分点"}
          </Button>
        )}
      </Card>

      {renderAnalysisCard(
        "清单工程量分析",
        <DatabaseOutlined />,
        boqData, boqText, boqConfirmed, generatingBoq,
        handleAnalyzeBoq, handleConfirmBoq,
        [
          { key: "name", label: "项目名称" },
          { key: "description", label: "描述" },
          { key: "detail", label: "详细内容" },
        ],
      )}

      {renderAnalysisCard(
        "工作界面分析",
        <TeamOutlined />,
        workInterfaceData, workInterfaceText, workInterfaceConfirmed, generatingWorkInterface,
        handleAnalyzeWorkInterface, handleConfirmWorkInterface,
        [
          { key: "name", label: "界面名称" },
          { key: "description", label: "描述" },
          { key: "responsible_party", label: "责任方" },
          { key: "coordination_requirements", label: "协调要求" },
        ],
      )}

      {renderAnalysisCard(
        "工程作法分析",
        <ToolOutlined />,
        constructionMethodData, constructionMethodText, constructionMethodConfirmed, generatingConstructionMethod,
        handleAnalyzeConstructionMethod, handleConfirmConstructionMethod,
        [
          { key: "name", label: "作法名称" },
          { key: "description", label: "描述" },
          { key: "technical_requirements", label: "技术要求" },
          { key: "quality_standard", label: "质量标准" },
        ],
      )}

      <Card
        title={
          <Space>
            <FileTextOutlined />
            <span>全局事实变量</span>
            {factsConfirmed && <Tag color="green" icon={<CheckCircleOutlined />}>已确认</Tag>}
          </Space>
        }
        style={{ marginBottom: 24 }}
        extra={
          <Space>
            {factsData.length > 0 && (
              <Button
                type="primary"
                icon={<ThunderboltOutlined />}
                onClick={async () => {
                setAuditingFacts(true);
                setGeneratingPhase("audit_facts");
                try {
                  const r = await auditFactsConsistency(projectId);
                  const items = r.items || [];
                  setAuditResult(items);
                  setAuditMeta({
                    audit_status: r.audit_status,
                    audited_sections: r.audited_sections,
                    total_sections: r.total_sections,
                    truncated: r.truncated,
                    failed_groups: r.failed_groups,
                    skipped_placeholder: r.skipped_placeholder,
                  });
                  const inconsistent = items.filter((i: any) => i.status === "inconsistent").length;
                  const missing = items.filter((i: any) => i.status === "missing").length;
                  // ENHANCE 2026-09-29：先区分「审计没跑起来」与「真的一致」，
                  // 再才谈通过/不通过；旧实现把两者混为一谈。
                  if (r.audit_status === "failed") {
                    message.error("一致性审计未能执行（AI 调用全部失败），本次结果不代表正文无矛盾", 8);
                  } else if (inconsistent > 0) {
                    message.warning(`发现 ${inconsistent} 处事实不一致`, 6);
                  } else if (r.audit_status === "partial") {
                    message.warning(
                      `审计未覆盖全部正文（${r.audited_sections}/${r.total_sections} 章），未发现不一致不等于全部一致`,
                      8,
                    );
                  } else if (missing > 0) {
                    message.warning(`正文未发现矛盾，但有 ${missing} 项事实在正文中没有对应表述`, 6);
                  } else {
                    message.success("一致性检查通过");
                  }
                } catch (e: any) {
                  message.error(e.response?.data?.detail || "审计失败");
                } finally {
                  setAuditingFacts(false);
                  setGeneratingPhase("");
                }
              }}
                loading={auditingFacts}
              >
                一致性审计
              </Button>
            )}
            {/* ENHANCE 2026-09-15：缺失值策略（移植自易标 globalFactsMode 三档） */}
            <Select
              size="small"
              value={factsPolicy}
              disabled={generatingFacts}
              onChange={(v) => setFactsPolicy(v)}
              style={{ width: 190 }}
              options={[
                { value: "fabricate", label: "缺失值：AI 补合理值（默认）" },
                { value: "omit", label: "缺失值：不编造，写笼统承诺" },
                { value: "placeholder", label: "缺失值：留【待填写】占位" },
              ]}
            />
            <Button
              type="primary"
              icon={<ThunderboltOutlined />}
              onClick={async () => {
                setGeneratingFacts(true);
                setGeneratingPhase("facts");
                try {
                  const r = await generateFacts(projectId, factsPolicy);
                  setFactsData(r.items || []);
                  setFactsConfirmed(false);
                  setFactConflicts(r.fact_conflicts || []);
                  setAuditResult(null);
                  // BUG-FIX 2026-09-29（缓存未失效）：重新提取后上一轮审计针对的
                  // 已是旧事实集，必须连覆盖率条一起抹掉，不然页面把旧结论当新结论展示。
                  setAuditMeta(null);
                  if (r?.categories) setFactCategories(r.categories);
                  // 2026-09-14：后端区分了 AI 精准提取与规则启发式兜底，
                  // 前者提示成功即可，后者必须明确告知"需人工确认"，
                  // 否则用户会把正则猜出来的数值当成已核实事实写进标书。
                  if (r?.extraction_mode === "heuristic") {
                    message.warning(
                      `已生成 ${r.items?.length || 0} 条事实，但为规则启发式结果，请逐条人工确认后再使用`,
                    );
                  } else if (factsPolicy === "placeholder" && (r?.placeholder_count || 0) > 0) {
                    // 占位策略：明确告知还有多少条等着人工填，别让占位符流进正文
                    message.warning(
                      `已生成 ${r.items?.length || 0} 条事实，其中 ${r?.placeholder_count} 条为【待填写】占位，请补齐后再确认`,
                      8,
                    );
                  } else if (factsPolicy === "omit") {
                    message.success(
                      `已生成 ${r.items?.length || 0} 条事实（缺值一律未编造，改为笼统承诺口径）`,
                    );
                  } else {
                    message.success("事实变量已提取");
                  }
                } catch (e: any) {
                  message.error(extractApiError(e));
                } finally {
                  setGeneratingFacts(false);
                  setGeneratingPhase("");
                }
              }}
              loading={generatingFacts}
            >
              {factsData.length > 0 ? "重新提取" : "提取事实"}
            </Button>
          </Space>
        }
      >
        {factsData.length > 0 ? (
          <Table
            dataSource={factsData}
            columns={[
              { title: "类别", dataIndex: "category", key: "category", width: 130, render: (v: string, _: any, i: number) => (
                // ENHANCE 2026-10-04（人工管理缺口）：旧实现类别列只读。而
                // category 是下游三处唯一路由依据（前端分组/审计送审/
                // consistency_auditor 基准匹配），推断错时用户无法就地纠正。
                // 缺 category 的历史条目不回传该字段，让后端 resolve_category
                // 按 key+label 兜底推断（与修复前行为一致）。
                <Select
                  size="small"
                  style={{ width: "100%" }}
                  value={v || undefined}
                  placeholder="自动推断"
                  options={factCategoryOptions}
                  onChange={(nv) => {
                    const updated = [...factsData];
                    updated[i] = { ...updated[i], category: nv };
                    setFactsData(updated);
                    setFactsConfirmed(false);
                  }}
                />
              )},
              { title: "标识", dataIndex: "key", key: "key", width: 150, render: (v: string, _: any, i: number) => (
                // ENHANCE 2026-10-04：旧实现只读，而「新增事实」入口上线后
                // 用户必须能设唯一标识（后端 POST /facts 已拒空 key/重复 key，
                // 且表格 rowKey="key" 依赖非空唯一值）。
                <Input
                  size="small"
                  value={v}
                  placeholder="如 projectManager"
                  onChange={(e) => {
                    const updated = [...factsData];
                    updated[i] = { ...updated[i], key: e.target.value };
                    setFactsData(updated);
                    setFactsConfirmed(false);
                  }}
                />
              )},
              { title: "标签", dataIndex: "label", key: "label", width: 120, render: (v: string, _: any, i: number) => (
                <Input size="small" value={v} onChange={(e) => {
                  const updated = [...factsData];
                  updated[i] = { ...updated[i], label: e.target.value };
                  setFactsData(updated);
                  setFactsConfirmed(false);
                }} />
              )},
              { title: "值", dataIndex: "value", key: "value", render: (v: string, record: any, i: number) => (
                <Space direction="vertical" size={0} style={{ width: "100%" }}>
                  <Input size="small" value={v} status={record?.needs_manual_input ? "warning" : undefined} onChange={(e) => {
                    const updated = [...factsData];
                    // BUG-FIX 2026-09-29（更新机制）：人工把【待填写】改成真值后，
                    // needs_manual_input 必须跟着消失。旧实现只换 value，角标永远挂在
                    // 已填好的事实上，后续确认时仍提醒“待人工填写”。
                    // 前缀与后端 facts_service.PLACEHOLDER_VALUE_PREFIX 同源。
                    const nextValue = e.target.value;
                    updated[i] = {
                      ...updated[i],
                      value: nextValue,
                      needs_manual_input: nextValue.includes("【待填写") ? updated[i]?.needs_manual_input : false,
                    };
                    setFactsData(updated);
                    setFactsConfirmed(false);
                  }} />
                  {/* ENHANCE 2026-09-15：placeholder 策略留下的占位必须一眼看得见，
                      否则【待填写】会直接被确认并写进全篇标书 */}
                  {record?.needs_manual_input && (
                    <Text type="warning" style={{ fontSize: 12 }}>待人工填写</Text>
                  )}
                </Space>
              )},
              { title: "来源", dataIndex: "source", key: "source", width: 90, render: (v: string) => {
                // BUG-FIX 2026-09-29（前后端契约）：旧实现 `v === "manual" ? "手动" : "AI"`，
                // 而启发式兜底（heuristic）与内置默认（default）都不是 AI 产出，
                // 被统一标成“AI”会让用户误信这些值是模型从招标文件读出来的。
                const srcMap: Record<string, string> = { ai: "AI", heuristic: "规则兜底", default: "默认值", manual: "手动" };
                return srcMap[v] || v || "手动";
              }},
              {
                title: "操作", key: "action", width: 60,
                render: (_: any, __: any, i: number) => (
                  // ENHANCE 2026-10-04：旧表格无删行入口，抽错的/重复的事实
                  // 只能整批重新提取碰运气；落库仍是 POST /facts 全量覆盖，
                  // 无需新增后端端点。
                  <Button
                    size="small"
                    type="text"
                    danger
                    icon={<DeleteOutlined />}
                    onClick={() => {
                      setFactsData(factsData.filter((_it, idx) => idx !== i));
                      setFactsConfirmed(false);
                    }}
                  />
                ),
              },
            ]}
            rowKey="key"
            pagination={false}
            size="small"
            bordered
          />
        ) : (
          <Empty description='尚未提取事实变量，请点击"提取事实"' />
        )}

        {/* BUG-FIX 2026-10-06：事实集内部矛盾告警（后端 group_fact_conflicts 下发）。
            同 label 不同 key/值的条目（如「项目经理：张伟」与「pm：李强」）若直接确认，
            两个值会同时注入正文造成自相矛盾——必须在确认前让用户看到并合并。 */}
        {factConflicts.length > 0 && (
          <div style={{ marginTop: 12 }}>
            <Alert
              type="warning"
              showIcon
              style={{ marginBottom: 8 }}
              message={`检测到 ${factConflicts.length} 组事实内部矛盾，确认前请合并或删除冲突条目`}
            />
            <Table
              size="small"
              bordered
              pagination={false}
              rowKey={(r: FactConflict, i?: number) => `${r.label}-${i ?? 0}`}
              dataSource={factConflicts}
              columns={[
                { title: "冲突事实", dataIndex: "label", key: "label", width: 160, render: (v: string) => <Text strong>{v}</Text> },
                {
                  title: "并存取值", key: "values",
                  render: (_: any, record: FactConflict) => (
                    <Space direction="vertical" size={2}>
                      {record.values.map((v, i) => (
                        <Text key={i} style={{ fontSize: 12 }}>
                          <Tag color="red">{v.value}</Tag>
                          <Text type="secondary" style={{ fontSize: 11 }}>标识 {v.key || "(空)"}</Text>
                        </Text>
                      ))}
                    </Space>
                  ),
                },
              ]}
            />
          </div>
        )}

        {/* ENHANCE 2026-09-29（覆盖率假象）：把本次送审覆盖面显式展出来。
            旧实现只取前 5 章×每章 500 字，一份十万字标书检查不到 3%，
            却只说“通过”；现在无论有没有查出问题，都能看到“查了多少”。 */}
        {auditMeta && (
          <div style={{ marginTop: 12 }}>
            <Space size={4} wrap>
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                审计范围：{auditMeta.audited_sections ?? "-"}/{auditMeta.total_sections ?? "-"} 章已完成正文
              </Typography.Text>
              {auditMeta.audit_status === "partial" && <Tag color="orange">未覆盖全部</Tag>}
              {auditMeta.audit_status === "failed" && <Tag color="red">审计未执行</Tag>}
              {!!auditMeta.failed_groups && <Tag color="orange">{auditMeta.failed_groups} 组失败</Tag>}
              {!!auditMeta.skipped_placeholder && (
                <Tag color="orange">{auditMeta.skipped_placeholder} 项占位未审</Tag>
              )}
              {auditMeta.audited_at && (
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                  于 {auditMeta.audited_at}
                </Typography.Text>
              )}
            </Space>
          </div>
        )}

        {auditResult && auditResult.length > 0 && (
          <div style={{ marginTop: 12 }}>
            <Typography.Text strong style={{ display: "block", marginBottom: 8 }}>一致性审计结果：</Typography.Text>
            <Table
              dataSource={auditResult}
              columns={[
                { title: "事实", dataIndex: "fact_label", key: "fact_label", width: 120 },
                { title: "值", dataIndex: "fact_value", key: "fact_value", width: 150 },
                { title: "状态", dataIndex: "status", key: "status", width: 80, render: (v: string) => {
                  const statusMap: Record<string, { color: string; text: string }> = { consistent: { color: "green", text: "一致" }, inconsistent: { color: "red", text: "矛盾" }, missing: { color: "orange", text: "缺失" } };
                  const s = statusMap[v] || { color: "default", text: v };
                  return <Tag color={s.color}>{s.text}</Tag>;
                }},
                { title: "详情", dataIndex: "detail", key: "detail" },
              ]}
              // 后端合并时按 fact_key || fact_label 去重，模型偶尔不返 fact_key；
              // 固定 rowKey="fact_key" 会退化成 undefined 造成重复 key 告警。
              rowKey={(r: any) => String(r.fact_key || r.fact_label || r.fact_value || "")}
              pagination={false}
              size="small"
            />
          </div>
        )}

        {factsData.length > 0 && (
          <Space style={{ marginTop: 12 }} wrap>
            <Button
              icon={<PlusOutlined />}
              onClick={() => {
                // ENHANCE 2026-10-04：人工补录此前没有入口（提示词里要求的
                // 项目事实 AI 没抽到时，只能重跑提取或改数据库）。source=manual
                // 与后端来源口径对齐，前端来源列已能区分展示。
                setFactsData([...factsData, { key: "", label: "", value: "", source: "manual", verified: false }]);
                setFactsConfirmed(false);
              }}
            >
              新增事实
            </Button>
            <Button
              icon={<SaveOutlined />}
              loading={savingFacts}
              onClick={async () => {
                // 只落库不确认：POST /facts 本身会把 confirmed 打回 0，
                // 保存后从 DB 回读同步各列（后端可能规范化过分类/占位标记）。
                try {
                  setSavingFacts(true);
                  const saved = await saveFacts(projectId, factsData);
                  const fSaved = await getFacts(projectId);
                  setFactsData(fSaved.items ?? []);
                  setFactsConfirmed(fSaved.confirmed ?? false);
                  // 保存响应即时回传的冲突分组优先于回读值（同一口径）
                  setFactConflicts(saved?.fact_conflicts ?? fSaved.fact_conflicts ?? []);
                  if ((saved?.fact_conflicts?.length ?? 0) > 0) {
                    message.warning(`已保存，但检测到 ${saved!.fact_conflicts!.length} 组事实内部矛盾，请合并后再确认`);
                  } else {
                    message.success("事实变量已保存（未确认，正文生成前仍需确认）");
                  }
                } catch (e: any) {
                  message.error(extractApiError(e) || "保存失败");
                } finally {
                  setSavingFacts(false);
                }
              }}
            >
              保存修改
            </Button>
            <Button
              type={factsConfirmed ? "default" : "primary"}
              onClick={async () => {
                // BUG-FIX 2026-10-06：存在内部矛盾时不允许静默确认——
                // 两个互斥值会同时注入正文。先弹二次确认，用户明知风险仍要确认才放行。
                const doConfirm = async () => {
                  try {
                    await saveFacts(projectId, factsData);
                    await confirmFacts(projectId);
                    setFactsConfirmed(true);
                    // 保存会全量覆写事实集，旧审计结果同样作废（与重新提取同一口径）
                    setAuditResult(null);
                    setAuditMeta(null);
                    // 刷新 facts 确认态（从 DB 同步）
                    const fRef = await getFacts(projectId);
                    setFactsData(fRef.items ?? []);
                    setFactsConfirmed(fRef.confirmed ?? false);
                    setFactConflicts(fRef.fact_conflicts ?? []);
                    message.success("事实变量已确认，正文生成时将自动引用");
                  } catch (e: any) {
                    // BUG-FIX 2026-10-04（前后端同步）：旧实现 catch 里写死
                    // 「确认失败」，而后端新增的同 key 重复/空事实拦截有明确原因，
                    // 透传 detail 让用户知道改什么而不是盲探。
                    message.error(extractApiError(e) || "确认失败");
                  }
                };
                if (factConflicts.length > 0) {
                  Modal.confirm({
                    title: "事实存在内部矛盾，仍要确认吗？",
                    content: `当前有 ${factConflicts.length} 组事实存在互斥取值（如同一事实两个不同的值）。若直接确认，这些值会同时注入正文，造成前后不一致。建议先合并或删除冲突条目。`,
                    okText: "仍要确认",
                    okButtonProps: { danger: true },
                    cancelText: "返回合并",
                    onOk: doConfirm,
                  });
                } else {
                  await doConfirm();
                }
              }}
              icon={factsConfirmed ? <CheckCircleOutlined /> : undefined}
            >
              {factsConfirmed ? "已确认（点击可重新确认）" : "确认事实变量"}
            </Button>
          </Space>
        )}
      </Card>

      <Divider />

      <div style={{ textAlign: "center", marginTop: 24 }}>
        <Button
          type="primary"
          size="large"
          disabled={!summaryConfirmed || !scoringConfirmed}
          onClick={() => navigate(`/projects/${projectId}/outline`)}
        >
          下一步：生成目录
        </Button>
        {(!summaryConfirmed || !scoringConfirmed) && (
          <div style={{ color: "#999", marginTop: 8, fontSize: 13 }}>
            请先确认摘要和评分点后继续
          </div>
        )}
      </div>
    </div>
  );
}