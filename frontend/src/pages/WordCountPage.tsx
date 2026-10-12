import { useEffect, useRef, useState } from "react";
import { useParams, useNavigate } from "react-router-dom";
import {
  Alert, Button, Card, Col, Empty, InputNumber, Progress, Row, Space, Statistic, Switch, Table, Tag, Tooltip, Typography, message,
} from "antd";
import {
  ArrowLeftOutlined, FileTextOutlined, CheckCircleOutlined, AimOutlined, WarningOutlined, BarChartOutlined, ControlOutlined, SafetyCertificateOutlined,
} from "@ant-design/icons";
import {
  computeWordControlDerived,
  getWordControlSettings,
  getWordCountOverview,
  getWordCountSections,
  setSectionTargetWords,
  snapshotWordControlSettings,
  updateWordControlSettings,
} from "../api";
import type {
  WordControlDerived,
  WordControlSettings,
  WordCountOverview,
  WordCountItem,
} from "../api";
import { extractApiError, getLevelColor, getLevelLabel, resolveProgressLowerBound } from "../utils";

const { Text } = Typography;

const statusLabel: Record<string, string> = {
  pending: "待生成",
  generating: "生成中",
  completed: "已完成",
  failed: "失败",
};

const statusColor: Record<string, string> = {
  pending: "default",
  generating: "processing",
  completed: "success",
  failed: "error",
};

/** 后端 422 校验错误的 errors 数组（保存/固化时的「配置不合理」明细）。 */
function extractWordControlErrors(e: unknown): string[] {
  const detail = (e as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
  if (detail && typeof detail === "object" && Array.isArray((detail as { errors?: unknown }).errors)) {
    return (detail as { errors: string[] }).errors;
  }
  return [];
}

/** R12 收尾警告的展示形态（后端 word_control_warning JSON 的解析结果）。 */
interface WordControlWarningView {
  message: string;
  total?: {
    expected: [number, number] | null;
    actual: number;
    status: string | null;
  };
  sections?: {
    target: number | null;
    range: [number, number] | null;
    out_of_range_count: number;
    items: Array<{ id: number; title: string; words: number; status: string }>;
  };
}

function parseWarning(raw: string | null | undefined): WordControlWarningView | null {
  if (!raw) return null;
  try {
    const w = JSON.parse(raw);
    if (!w || typeof w !== "object" || !w.message) return null;
    return w as WordControlWarningView;
  } catch {
    return null;
  }
}

/** 字数控制草稿（R2：输入单位为「万字」，最多 4 位小数；null = 未填）。 */
interface WordControlDraft {
  min: number | null;
  max: number | null;
  section: number | null;
  strict: boolean;
}

const toWan = (words: number): number => (words ? Number((words / 10000).toFixed(4)) : 0);
const toAbsWords = (wan: number | null): number =>
  wan && wan > 0 ? Math.round(wan * 10000) : 0;

const draftFromSettings = (s: WordControlSettings): WordControlDraft => ({
  min: toWan(s.minimum_words),
  max: toWan(s.maximum_words),
  section: toWan(s.section_words),
  strict: Boolean(s.strict_section_words),
});

const draftToSettings = (d: WordControlDraft): WordControlSettings => {
  const section = toAbsWords(d.section);
  return {
    minimum_words: toAbsWords(d.min),
    maximum_words: toAbsWords(d.max),
    section_words: section,
    // 与后端归一化同口径：无每节字数时强控无从定义，直接清零
    strict_section_words: section > 0 ? Boolean(d.strict) : false,
  };
};

export default function WordCountPage() {
  const { id } = useParams<{ id: string }>();
  const projectId = Number(id);
  const navigate = useNavigate();

  const [overview, setOverview] = useState<WordCountOverview | null>(null);
  const [items, setItems] = useState<WordCountItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [drafts, setDrafts] = useState<Record<number, number | null>>({});

  // === 易标字数控制（R2/R4/R5/R6/R8/R12/R14，2026-10-12 移植）===
  const [wcDraft, setWcDraft] = useState<WordControlDraft | null>(null);
  const [wcDerived, setWcDerived] = useState<WordControlDerived | null>(null);
  const [wcErrors, setWcErrors] = useState<string[]>([]);
  const [wcSaving, setWcSaving] = useState(false);
  const [wcSnapshotAt, setWcSnapshotAt] = useState<string | null>(null);
  const [wcSnapshotCount, setWcSnapshotCount] = useState<number | null>(null);
  const [wcWarning, setWcWarning] = useState<WordControlWarningView | null>(null);
  const previewTimerRef = useRef<number | null>(null);

  const load = async () => {
    setLoading(true);
    try {
      const [ov, list, wc] = await Promise.all([
        getWordCountOverview(projectId),
        getWordCountSections(projectId),
        getWordControlSettings(projectId).catch(() => null),
      ]);
      setOverview(ov);
      setItems(list.items || []);
      if (wc) {
        setWcDraft(draftFromSettings(wc.settings));
        setWcDerived(wc.derived);
        setWcErrors(wc.derived?.errors || []);
        setWcSnapshotAt(wc.snapshot_at);
        setWcSnapshotCount(wc.snapshot?.section_count ?? null);
        setWcWarning(parseWarning(wc.last_warning));
      }
    } catch (e) {
      message.error(extractApiError(e));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    load();
  }, [projectId]);

  // R4/R5/R6/R8 派生指标「即改即算」：草稿变化后 400ms 防抖预览（不落库）。
  useEffect(() => {
    if (!wcDraft) return;
    if (previewTimerRef.current) window.clearTimeout(previewTimerRef.current);
    previewTimerRef.current = window.setTimeout(() => {
      computeWordControlDerived({ ...draftToSettings(wcDraft), project_id: projectId })
        .then((d) => {
          setWcDerived(d);
          setWcErrors(d.errors || []);
        })
        .catch(() => {});
    }, 400);
    return () => {
      if (previewTimerRef.current) window.clearTimeout(previewTimerRef.current);
    };
  }, [wcDraft, projectId]);

  const handleSetTarget = async (sectionId: number, value: number | null) => {
    try {
      await setSectionTargetWords(projectId, sectionId, value ?? 0);
      message.success("目标字数已更新");
      load();
    } catch (e) {
      message.error(extractApiError(e));
    }
  };

  const handleSaveWordControl = async () => {
    if (!wcDraft) return;
    setWcSaving(true);
    try {
      const settings = draftToSettings(wcDraft);
      const res = await updateWordControlSettings(projectId, settings);
      setWcDerived(res.derived);
      setWcErrors(res.derived?.errors || []);
      // PUT 会主动置空生效快照（R14：改了配置必须重新固化），快照状态随之复位
      setWcSnapshotAt(null);
      setWcSnapshotCount(null);
      message.success("字数控制配置已保存；生效快照已失效，生成正文前请重新固化");
    } catch (e) {
      const errs = extractWordControlErrors(e);
      if (errs.length) {
        setWcErrors(errs);
        message.error("字数控制配置不合理，已拒绝保存");
      } else {
        message.error(extractApiError(e));
      }
    } finally {
      setWcSaving(false);
    }
  };

  const handleSnapshot = async () => {
    setWcSaving(true);
    try {
      const res = await snapshotWordControlSettings(projectId);
      setWcSnapshotAt(res.snapshot?.snapshot_at || null);
      setWcSnapshotCount(res.snapshot?.section_count ?? null);
      message.success(
        `字数控制生效快照已固化（${res.snapshot?.section_count ?? 0} 个章节），生成正文将按本快照控制`,
      );
    } catch (e) {
      const errs = extractWordControlErrors(e);
      if (errs.length) {
        setWcErrors(errs);
        message.error("配置不合理，无法固化快照");
      } else {
        message.error(extractApiError(e));
      }
    } finally {
      setWcSaving(false);
    }
  };

  const columns = [
    {
      title: "标题",
      dataIndex: "title",
      key: "title",
      ellipsis: true,
      render: (title: string, record: WordCountItem) => (
        <Space>
          <Tag color={getLevelColor(record.level)} style={{ marginRight: 0 }}>
            {getLevelLabel(record.level)}
          </Tag>
          <Text style={{ paddingLeft: (record.level - 1) * 12 }}>{title}</Text>
        </Space>
      ),
    },
    {
      title: "状态",
      dataIndex: "status",
      key: "status",
      width: 100,
      render: (status: string) => (
        <Tag color={statusColor[status] || "default"}>{statusLabel[status] || status}</Tag>
      ),
    },
    {
      title: "当前字数",
      dataIndex: "word_count",
      key: "word_count",
      width: 120,
      sorter: (a: WordCountItem, b: WordCountItem) => a.word_count - b.word_count,
      render: (v: number) => <Text strong>{v.toLocaleString()}</Text>,
    },
    {
      title: "目标字数",
      dataIndex: "target_words",
      key: "target_words",
      width: 180,
      render: (v: number | null, record: WordCountItem) => (
        <InputNumber
          value={drafts[record.id] !== undefined ? drafts[record.id] : v}
          min={0}
          placeholder="未设置"
          style={{ width: 140 }}
          onChange={(value) => setDrafts((prev) => ({ ...prev, [record.id]: value ?? null }))}
          onBlur={() => {
            const draft = drafts[record.id];
            if (draft !== undefined && draft !== v) {
              handleSetTarget(record.id, draft);
            }
          }}
          // BUG-FIX 2026-09-16（中）：表格提示是「回车保存」，但此前只实现了失焦保存 ——
          // 按提示敲 Enter 什么都不会发生，切换页面/重新查询后草稿静默丢失。
          onPressEnter={(e) => {
            const draft = drafts[record.id];
            if (draft !== undefined && draft !== v) {
              handleSetTarget(record.id, draft);
            }
            (e.target as unknown as HTMLInputElement).blur();
          }}
        />
      ),
    },
    {
      title: "达成进度",
      dataIndex: "progress",
      key: "progress",
      width: 180,
      render: (progress: number, record: WordCountItem) => {
        // BUG-FIX 2026-10-09（前后端判定不一致）：本列此前用
        // `word_count < target_words` / `>= target_words` 判定，即**零容差**，
        // 完全忽略后端 `GET /word-count/sections` 已经算好的容差区间。
        // 于是同一个 300 字目标、实际 280 字的 L3 章节，后端 `on_target`
        // 判定为「在 240~360 内 = 达标」，本列却渲染成「缺 20 字」+ 红色进度条
        // —— 用户看到的画面与导出预检（services/export.py 走同一套区间）
        // 直接矛盾，且无法自查原因。
        // 现统一以服务端给出的 `on_target` 为准（它是唯一事实来源，
        // 已包含 L3 自身 300±20% 与四档档位各自 tolerance_ratio 的判定），
        // 「缺多少字」也改为相对**区间下界**计算，与红色进度条语义对齐。
        const onTarget = record.on_target === true;
        const unknown = record.on_target === "unknown";
        // 缺字数以达标区间下界为基准；后端未直接给区间时用 target - tolerance
        // 估算，tolerance 取自档位配置（tier_target 与 target_words 的关系由
        // 后端口径保证，前端仅在缺少 tier 信息时按 ±10% 保守估算）。
        const lowerBound = resolveProgressLowerBound(record);
        const hasTarget = record.target_words != null && record.target_words > 0;
        const below = !onTarget && !unknown && hasTarget && record.word_count < lowerBound;
        const done = onTarget;
        return (
          <Space direction="vertical" size={2} style={{ width: "100%" }}>
            <Progress
              percent={progress}
              size="small"
              status={done ? "success" : below ? "exception" : "active"}
            />
            {below && (
              <Text type="warning" style={{ fontSize: 12 }}>
                缺 {(lowerBound - record.word_count).toLocaleString()} 字
              </Text>
            )}
            {done && <Text type="success" style={{ fontSize: 12 }}>已达标</Text>}
            {!done && !below && unknown && (
              <Text type="secondary" style={{ fontSize: 12 }}>待生成</Text>
            )}
          </Space>
        );
      },
    },
  ];

  return (
    <div style={{ maxWidth: 1200, margin: "0 auto" }}>
      <div style={{ marginBottom: 16, display: "flex", alignItems: "center", justifyContent: "space-between" }}>
        <Space>
          <Button icon={<ArrowLeftOutlined />} onClick={() => navigate(`/projects/${projectId}/sections`)}>
            返回正文
          </Button>
          <Typography.Title level={4} style={{ margin: 0 }}>
            <BarChartOutlined /> 字数统计
          </Typography.Title>
        </Space>
        <Button onClick={load} loading={loading}>刷新</Button>
      </div>

      {overview && (
        <Row gutter={16} style={{ marginBottom: 16 }}>
          <Col xs={12} md={6}>
            <Card>
              <Statistic
                title="总字数"
                value={overview.total_words}
                prefix={<FileTextOutlined />}
                suffix={<Text type="secondary" style={{ fontSize: 13 }}>字</Text>}
              />
            </Card>
          </Col>
          <Col xs={12} md={6}>
            <Card>
              <Statistic
                title="目标总字数"
                value={overview.total_target}
                prefix={<AimOutlined />}
                suffix={<Text type="secondary" style={{ fontSize: 13 }}>字</Text>}
              />
            </Card>
          </Col>
          <Col xs={12} md={6}>
            <Card>
              <Statistic
                title="目标覆盖率"
                value={overview.target_coverage}
                suffix="%"
                valueStyle={{ color: overview.target_coverage >= 100 ? "#52c41a" : "#faad14" }}
              />
            </Card>
          </Col>
          <Col xs={12} md={6}>
            <Card>
              <Statistic
                title="达标章节"
                value={overview.on_target_sections}
                prefix={<CheckCircleOutlined />}
                suffix={
                  <Text type="secondary" style={{ fontSize: 13 }}>
                    / {overview.total_sections} 章节，缺字 {overview.below_target_sections}
                  </Text>
                }
              />
            </Card>
          </Col>
        </Row>
      )}

      {wcDraft && (
        <Card
          style={{ marginBottom: 16 }}
          title={<Space><ControlOutlined /> 字数控制（生成正文的绝对字数目标）</Space>}
          extra={
            <Space>
              <Button type="primary" size="small" loading={wcSaving} onClick={handleSaveWordControl}>
                保存配置
              </Button>
              <Tooltip title="生成正文前必须固化：快照与当前目录版本配对，正文链只认快照">
                <Button
                  size="small"
                  icon={<SafetyCertificateOutlined />}
                  loading={wcSaving}
                  disabled={wcErrors.length > 0}
                  onClick={handleSnapshot}
                >
                  固化快照
                </Button>
              </Tooltip>
            </Space>
          }
        >
          <Space direction="vertical" size={12} style={{ width: "100%" }}>
            <Space size={24} wrap align="start">
              <Space direction="vertical" size={4}>
                <Text type="secondary">最少字数</Text>
                <InputNumber
                  min={0}
                  step={0.1}
                  precision={4}
                  style={{ width: 150 }}
                  placeholder="0 = 不控制"
                  addonAfter="万字"
                  value={wcDraft.min}
                  onChange={(v) => setWcDraft({ ...wcDraft, min: v ?? null })}
                />
                <Text type="secondary" style={{ fontSize: 12 }}>
                  ≈ {toAbsWords(wcDraft.min).toLocaleString()} 字
                </Text>
              </Space>
              <Space direction="vertical" size={4}>
                <Text type="secondary">最多字数</Text>
                <InputNumber
                  min={0}
                  step={0.1}
                  precision={4}
                  style={{ width: 150 }}
                  placeholder="0 = 不控制"
                  addonAfter="万字"
                  value={wcDraft.max}
                  onChange={(v) => setWcDraft({ ...wcDraft, max: v ?? null })}
                />
                <Text type="secondary" style={{ fontSize: 12 }}>
                  ≈ {toAbsWords(wcDraft.max).toLocaleString()} 字
                </Text>
              </Space>
              <Space direction="vertical" size={4}>
                <Text type="secondary">每小节字数</Text>
                <InputNumber
                  min={0}
                  step={0.05}
                  precision={4}
                  style={{ width: 150 }}
                  placeholder="0 = 不控制"
                  addonAfter="万字"
                  value={wcDraft.section}
                  onChange={(v) => setWcDraft({ ...wcDraft, section: v ?? null })}
                />
                <Text type="secondary" style={{ fontSize: 12 }}>
                  ≈ {toAbsWords(wcDraft.section).toLocaleString()} 字
                </Text>
              </Space>
              <Space direction="vertical" size={4}>
                <Text type="secondary">强控（每小节 ±20%）</Text>
                <Space>
                  <Switch
                    checked={wcDraft.strict}
                    disabled={toAbsWords(wcDraft.section) <= 0}
                    onChange={(checked) => setWcDraft({ ...wcDraft, strict: checked })}
                  />
                  <Text type="secondary" style={{ fontSize: 12 }}>
                    {toAbsWords(wcDraft.section) > 0
                      ? "超出区间将自动压缩并提示"
                      : "需先填写每小节字数"}
                  </Text>
                </Space>
              </Space>
            </Space>

            <Space wrap>
              {wcSnapshotAt ? (
                <Tag color="success">生效快照已固化：{wcSnapshotAt}</Tag>
              ) : (
                <Tag color="warning">未固化：生成正文将被拒绝</Tag>
              )}
              {wcSnapshotCount != null && (
                <Text type="secondary" style={{ fontSize: 12 }}>
                  固化时目录 {wcSnapshotCount} 个章节
                </Text>
              )}
              <Text type="secondary" style={{ fontSize: 12 }}>
                提示：修改本配置会置空快照，需重新固化后生成（生成目录/调整目录后同理）
              </Text>
            </Space>

            {wcErrors.length > 0 && (
              <Alert
                type="error"
                showIcon
                message="配置不合理（将拒绝保存/固化）"
                description={
                  <ul style={{ margin: 0, paddingLeft: 18 }}>
                    {wcErrors.map((err) => (
                      <li key={err}>{err}</li>
                    ))}
                  </ul>
                }
              />
            )}

            {wcDerived && wcErrors.length === 0 && (
              <Space direction="vertical" size={4}>
                {wcDerived.section_bounds?.[1] > 0 && (
                  <Text>
                    每小节达标区间（±20%）：
                    <Text strong>
                      {wcDerived.section_bounds[0].toLocaleString()} ~ {wcDerived.section_bounds[1].toLocaleString()}
                    </Text>{" "}
                    字
                  </Text>
                )}
                {wcDerived.target_leaf_count ? (
                  <Text>
                    目标小节数：<Text strong>{wcDerived.target_leaf_count}</Text> 节
                    {wcDerived.leaf_count_range?.[1] != null && (
                      <Text type="secondary">
                        （允许 {wcDerived.leaf_count_range[0]}~{wcDerived.leaf_count_range[1]} 节）
                      </Text>
                    )}
                    {typeof wcDerived.current_leaf_count === "number" && (
                      <Text type={wcDerived.leaf_outside_range ? "warning" : "secondary"}>
                        ，当前目录 {wcDerived.current_leaf_count} 节
                        {wcDerived.leaf_outside_range ? "（超出允许区间，生成正文字数可能不符合预期）" : ""}
                      </Text>
                    )}
                  </Text>
                ) : null}
                {wcDerived.generation_word_target ? (
                  <Text>
                    初稿目标（单节）：<Text strong>{wcDerived.generation_word_target.toLocaleString()}</Text> 字
                    <Text type="secondary">（按全文上限倒推，为扩写预留空间）</Text>
                  </Text>
                ) : null}
                {wcDerived.estimated_pages ? (
                  <Text>
                    页数预估：约 <Text strong>{wcDerived.estimated_pages}</Text> 页
                    <Text type="secondary">（页数与排版有关，无法精确预估，不作为验收标准）</Text>
                  </Text>
                ) : null}
                {wcDerived.leaf_count_warning?.message && (
                  <Text type="warning">{wcDerived.leaf_count_warning.message}</Text>
                )}
                {wcDerived.word_capacity && (
                  <Text type="warning">{wcDerived.word_capacity}</Text>
                )}
              </Space>
            )}
          </Space>
        </Card>
      )}

      {wcWarning && (
        <Card style={{ marginBottom: 16 }} size="small">
          <Space direction="vertical" size={4} style={{ width: "100%" }}>
            <Space>
              <WarningOutlined style={{ color: "#faad14" }} />
              <Text strong>上次生成字数提醒：{wcWarning.message}</Text>
            </Space>
            {wcWarning.total?.expected && (
              <Text type="secondary">
                全文期望 {wcWarning.total.expected[0]?.toLocaleString()}~{wcWarning.total.expected[1]?.toLocaleString()} 字，
                实际 {wcWarning.total.actual?.toLocaleString()} 字
                {wcWarning.total.status === "below" ? "（偏低）" : wcWarning.total.status === "above" ? "（偏高）" : ""}
              </Text>
            )}
            {wcWarning.sections?.range && (
              <Text type="secondary">
                强控每节 {wcWarning.sections.target?.toLocaleString()} 字
                （达标区间 {wcWarning.sections.range[0]?.toLocaleString()}~{wcWarning.sections.range[1]?.toLocaleString()} 字），
                越界 {wcWarning.sections.out_of_range_count} 节
              </Text>
            )}
            {(wcWarning.sections?.items || []).slice(0, 8).map((it) => (
              <Text key={`${it.id}`} type="warning" style={{ fontSize: 12 }}>
                · {it.title || `#${it.id}`}：{it.words?.toLocaleString()} 字
                {it.status === "above" ? "（超出上限）" : "（低于下限）"}
              </Text>
            ))}
          </Space>
        </Card>
      )}

      {overview && (overview.empty_sections > 0 || overview.below_target_sections > 0) && (
        <Card style={{ marginBottom: 16 }}>
          <Space direction="vertical" style={{ width: "100%" }}>
            {overview.empty_sections > 0 && (
              <Space>
                <WarningOutlined style={{ color: "#faad14" }} />
                <Text>
                  有 <Text strong type="warning">{overview.empty_sections}</Text> 个章节尚未生成内容
                </Text>
              </Space>
            )}
            {overview.below_target_sections > 0 && (
              <Space>
                <WarningOutlined style={{ color: "#faad14" }} />
                <Text>
                  有 <Text strong type="warning">{overview.below_target_sections}</Text> 个章节未达目标字数
                </Text>
              </Space>
            )}
          </Space>
        </Card>
      )}

      <Card
        title={<Space><FileTextOutlined /> 章节字数明细</Space>}
        extra={<Text type="secondary">点击目标字数输入框回车保存</Text>}
      >
        <Table
          rowKey="id"
          loading={loading}
          dataSource={items}
          columns={columns}
          pagination={{ pageSize: 20, showSizeChanger: true }}
          locale={{ emptyText: <Empty description="暂无章节数据" /> }}
          size="middle"
        />
      </Card>
    </div>
  );
}
