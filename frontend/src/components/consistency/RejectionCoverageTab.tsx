import { useCallback, useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  Alert, Button, Card, Empty, Progress, Space, Table, Tag, Tooltip, Typography, message,
} from "antd";
import { ReloadOutlined, SafetyOutlined } from "@ant-design/icons";
import { getRejectionCoverage } from "../../api";
import type { ConsistencyRules, RejectionCoverageItem } from "../../api";
import { extractApiError, mapConsistencyRejectionStatus } from "../../utils";

const { Text, Paragraph } = Typography;

export interface RejectionCoverageTabProps {
  projectId: number;
  /**
   * 审核规则快照（页面壳从 GET /consistency/rules 拉取后下发）。
   * ENHANCE 2026-10-13（第六轮·统一命名空间）：三档状态文案与弱覆盖阈值改从
   * 这里取，不再本地硬编码（statusLabel/statusColor 两张写死表 + 0.25 魔法数）。
   */
  rules?: ConsistencyRules | null;
}

/**
 * 「废标条款应答覆盖率」Tab —— 统计项目正文对招标废标条款的应答覆盖率。
 *
 * 后端机制：
 *   扫描已有的大数据废标检查结果，判断每个废标条款是否在正文中有足够
 *   字数的实质性应答。返回 covered / weak / empty 三种状态。
 *
 * 端点性质：**确定性**（不调 AI、不查网络），只查已有废标任务结果。
 * 如果项目从未跑过废标检查，返回 status='no_checks'（不是错误）。
 *
 * ── BUG-FIX 2026-10-13（P1 · 与后端契约三处不一致，本组件此前全错）：
 *   1. `rowKey="clause_id"` —— 后端返回的键是 `id`（`rejection_coverage.py`
 *      `_extract_clauses` 归一后的 `clause["id"]`），故**每一行**的 rowKey 都是
 *      undefined，行标识完全失效；
 *   2. `section_hits` 列按 `string[]` 声明并 `{h}` 直出，而后端给的是
 *      `[{section_id, matched_bigrams}]` → 页面渲染出 `[object Object]`；
 *   3. `threshold` 被当成「字数」展示（「默认 200 字」「单条阈值 0.25 字」），
 *      而它其实是**字符二元组覆盖率比值**（默认 0.25），单位完全讲错。
 * 现按后端真实结构渲染，并顺手把「命中最强章节」接成可点击深链（与冲突 Tab 同款
 * 定位能力）——这一列此前即使修好也只能显示乱码，等于废列。
 */
export default function RejectionCoverageTab({ projectId, rules }: RejectionCoverageTabProps) {
  const navigate = useNavigate();

  const [loading, setLoading] = useState(false);
  const [items, setItems] = useState<RejectionCoverageItem[]>([]);
  const [summary, setSummary] = useState<{
    status: string;
    scannedClauses: number;
    fullChars: number;
    /** 后端响应下发的阈值；缺省时为 null，展示层回退 rules 快照值 */
    threshold: number | null;
    weakCount: number;
    emptyCount: number;
  } | null>(null);

  // 响应竞态保护
  const loadSeqRef = useRef(0);

  const isValidProject = Number.isFinite(projectId) && projectId > 0;

  // 弱覆盖阈值兜底：优先规则快照生效值，快照未就绪时回退静态默认（与后端
  // rejection_coverage.COVERAGE_WEAK 初始标定值一致）。响应体里的 threshold
  // 仍是第一来源（load 时按实际返回值展示），这里只兜「响应缺字段」。
  const fallbackThreshold = rules?.rejection_coverage?.coverage_weak ?? 0.25;

  const load = useCallback(async () => {
    if (!isValidProject) return;
    const seq = ++loadSeqRef.current;
    setLoading(true);
    try {
      const res = await getRejectionCoverage(projectId);
      if (seq !== loadSeqRef.current) return;
      setItems(res.items || []);
      setSummary({
        status: res.status,
        scannedClauses: res.scanned_clauses || 0,
        fullChars: res.full_chars || 0,
        // BUG-FIX 2026-10-13（第六轮）：旧写法 `res.threshold || 0` 会把
        // 「响应缺 threshold」压成 0，而展示层用 `?? fallback` 兜底时
        // **0 不是 nullish**——兜底永远不生效，页面显示「阈值 0.00（0%）」。
        // 这里缺省即存 null，把兜底决策完整交给展示层。
        threshold: typeof res.threshold === "number" ? res.threshold : null,
        weakCount: res.weak_count || 0,
        emptyCount: res.empty_count || 0,
      });
    } catch (e) {
      if (seq === loadSeqRef.current) {
        message.error(extractApiError(e));
      }
    } finally {
      if (seq === loadSeqRef.current) setLoading(false);
    }
  }, [projectId, isValidProject]);

  useEffect(() => {
    load();
  }, [load]);

  const statusMeta = (status: string) => mapConsistencyRejectionStatus(status);

  const columns = [
    {
      title: "废标条款",
      dataIndex: "name",
      key: "name",
      width: 200,
      render: (v: string) => <Text strong>{v}</Text>,
    },
    {
      title: "废标原文",
      dataIndex: "text",
      key: "text",
      ellipsis: true,
      render: (v: string) => <Text type="secondary">{v}</Text>,
    },
    {
      title: "应答状态",
      dataIndex: "status",
      key: "status",
      width: 110,
      render: (v: string) => {
        const meta = statusMeta(v);
        return <Tag color={meta.color}>{meta.label}</Tag>;
      },
    },
    {
      title: "应答强度",
      dataIndex: "coverage",
      key: "coverage",
      width: 140,
      render: (v: number) => (
        <Progress
          percent={Math.round(v * 100)}
          size="small"
          strokeColor={
            v >= 0.8 ? "#52c41a" : v >= 0.4 ? "#faad14" : "#ff4d4f"
          }
        />
      ),
    },
    {
      title: "正文章节命中",
      dataIndex: "section_hits",
      key: "section_hits",
      render: (hits: RejectionCoverageItem["section_hits"]) => (
        <Space size={[4, 4]} wrap>
          {(hits || []).length > 0 ? (
            hits.map((h, i) => (
              <Tag
                key={`${h.section_id}_${i}`}
                color="blue"
                style={{ cursor: "pointer" }}
                onClick={() =>
                  navigate(`/projects/${projectId}/sections?section=${h.section_id}`)
                }
              >
                <Tooltip title={`字符二元组命中 ${h.matched_bigrams} 个`}>
                  章节 {h.section_id}
                </Tooltip>
              </Tag>
            ))
          ) : (
            <Text type="secondary">—</Text>
          )}
        </Space>
      ),
    },
  ];

  if (!isValidProject) {
    return <Alert type="error" showIcon message="项目 ID 无效" />;
  }

  const coveredCount = summary
    ? summary.scannedClauses - summary.weakCount - summary.emptyCount
    : 0;

  return (
    <Space direction="vertical" style={{ width: "100%" }} size="middle">
      <Alert
        type="info"
        showIcon
        icon={<SafetyOutlined />}
        message="废标条款应答覆盖率"
        description={
          <>
            统计项目正文对招标废标条款的应答覆盖率：每个条款返回「已覆盖 / 覆盖较弱 / 未应答」三档状态。判定口径为条款字符二元组与全文正文字符二元组的重合率，低于阈值（默认{" "}
            {fallbackThreshold.toFixed(2)}，即{" "}
            {Math.round(fallbackThreshold * 100)}%）视为覆盖较弱。此端点
            <Text strong>确定性</Text>（不调 AI），仅依赖已有的大数据废标检查结果。
          </>
        }
      />

      {/* 还没跑过废标检查 —— 优雅提示 */}
      {summary?.status === "no_checks" && (
        <Alert
          type="warning"
          showIcon
          icon={<SafetyOutlined />}
          message="项目尚未运行过废标检查"
          description={
            <Paragraph style={{ marginBottom: 0 }}>
              废标覆盖率依赖已有的大数据废标检查结果。请先在「废标检查」模块运行一次完整检查，然后回到此页查看应答覆盖率。
            </Paragraph>
          }
        />
      )}

      <Card
        title={<Space><SafetyOutlined />覆盖率统计</Space>}
        extra={
          <Button icon={<ReloadOutlined />} loading={loading} onClick={load}>
            刷新
          </Button>
        }
      >
        {summary && summary.scannedClauses > 0 ? (
          <>
            <Space size={[12, 12]} wrap style={{ marginBottom: 16 }}>
              <Tag>废标条款总数 {summary.scannedClauses}</Tag>
              <Tag color="green">已覆盖 {coveredCount}</Tag>
              <Tag color="orange">覆盖较弱 {summary.weakCount}</Tag>
              <Tag color="red">未应答 {summary.emptyCount}</Tag>
              <Tag>应答正文总字数 {summary.fullChars}</Tag>
              <Tag>
                覆盖率阈值 {(summary.threshold ?? fallbackThreshold).toFixed(2)}（
                {Math.round((summary.threshold ?? fallbackThreshold) * 100)}%）
              </Tag>
            </Space>

            {/* 总览进度 */}
            <Space style={{ marginBottom: 12 }}>
              <Text>整体覆盖率：</Text>
              <Progress
                percent={Math.round(
                  (coveredCount / summary.scannedClauses) * 100,
                )}
                strokeColor={
                  coveredCount / summary.scannedClauses >= 0.8
                    ? "#52c41a"
                    : coveredCount / summary.scannedClauses >= 0.5
                    ? "#faad14"
                    : "#ff4d4f"
                }
                style={{ minWidth: 300 }}
              />
            </Space>
          </>
        ) : summary?.status !== "no_checks" ? (
          <Empty description={loading ? "正在加载..." : "暂无废标数据"} />
        ) : null}
      </Card>

      <Card title="废标条款明细" size="small">
        <Table
          rowKey="id"
          loading={loading}
          dataSource={items}
          columns={columns}
          size="middle"
          pagination={{ pageSize: 10, showSizeChanger: true }}
          locale={{
            emptyText:
              loading ? (
                "正在加载..."
              ) : summary?.status === "no_checks" ? (
                <Empty description="请先在废标检查模块运行一次完整检查" />
              ) : (
                <Empty description="暂无废标条款" />
              ),
          }}
        />
      </Card>
    </Space>
  );
}
