import React from "react";
import { Progress, List, Tag, Typography, Space } from "antd";
import { CloseCircleFilled, LoadingOutlined, SaveOutlined, StopOutlined, EditOutlined, AuditOutlined, ExpandOutlined, FileSearchOutlined } from "@ant-design/icons";

const { Text } = Typography;

export interface ProgressItem {
  section_id: number;
  title: string;
  status: string;
  progress: number;
  message: string;
  word_count?: number;
  saved?: boolean;
  /** 有效目标字数（后端唯一权威口径 word_target_resolver 解析，CONVERGE 2026-10-12） */
  word_count_target?: number | null;
  /** 达标区间下界 / 上界（强控为快照派生的 ±20% 硬区间） */
  word_count_target_lo?: number | null;
  word_count_target_hi?: number | null;
  /** 目标来源：strict（强控快照）/ l3_own / section_tier / writing_length_tier / default_tier / section_target_words */
  word_count_target_source?: string | null;
}

interface ProgressPanelProps {
  items: ProgressItem[];
  total: number;
  success: number;
  failed: number;
  isRunning: boolean;
  cancelled?: number;
}

const PHASE_CONFIG: Record<string, { icon: React.ReactNode; color: string; text: string }> = {
  planning: { icon: <FileSearchOutlined style={{ color: "#722ed1" }} />, color: "purple", text: "编排中" },
  generating: { icon: <LoadingOutlined style={{ color: "#1677ff" }} />, color: "processing", text: "生成中" },
  rewriting: { icon: <LoadingOutlined style={{ color: "#fa8c16" }} />, color: "orange", text: "重写中" },
  cached: { icon: <SaveOutlined style={{ color: "#13c2c2" }} />, color: "cyan", text: "缓存命中" },
  expanding: { icon: <ExpandOutlined style={{ color: "#fa8c16" }} />, color: "orange", text: "扩写中" },
  auditing: { icon: <AuditOutlined style={{ color: "#13c2c2" }} />, color: "cyan", text: "审计中" },
  completed: { icon: <SaveOutlined style={{ color: "#52c41a" }} />, color: "green", text: "已保存" },
  failed: { icon: <CloseCircleFilled style={{ color: "#ff4d4f" }} />, color: "red", text: "失败" },
  cancelled: { icon: <StopOutlined style={{ color: "#999" }} />, color: "default", text: "已取消" },
};

/** 偏差百分比文案（如 +4% / -12%）；缺目标或实际字数为空时返回空串。 */
function deviationText(item: ProgressItem): string {
  const target = item.word_count_target;
  const actual = item.word_count;
  if (!target || target <= 0 || actual == null || actual <= 0) return "";
  const rate = Math.round(((actual - target) / target) * 100);
  if (rate === 0) return "";
  return `${rate > 0 ? "+" : ""}${rate}%`;
}

/** 是否越界（强控区间优先；缺区间时按 ±20% 兜底判定）。 */
function isWordTargetOutOfRange(item: ProgressItem): boolean {
  const actual = item.word_count;
  const lo = item.word_count_target_lo;
  const hi = item.word_count_target_hi;
  if (actual == null || actual <= 0) return false;
  if (lo != null && hi != null && hi > 0) return actual < lo || actual > hi;
  const target = item.word_count_target;
  if (!target || target <= 0) return false;
  return actual < target * 0.8 || actual > target * 1.2;
}

/** 目标来源短标签（便于排障：同一页可能混用强控 / 档位 / L3 自身三种口径）。 */
const WORD_TARGET_SOURCE_LABEL: Record<string, string> = {
  strict: "强控",
  l3_own: "L3自身",
  section_tier: "单节档位",
  writing_length_tier: "全局档位",
  default_tier: "默认档位",
  section_target_words: "单节目标",
};

/** 「目标300字（240~360）· +4%」；无目标时返回空串（不渲染）。 */
function describeWordTarget(item: ProgressItem): string {
  const target = item.word_count_target;
  if (!target || target <= 0) return "";
  const lo = item.word_count_target_lo;
  const hi = item.word_count_target_hi;
  const range = lo != null && hi != null && hi > 0 ? `（${lo}~${hi}）` : "";
  const dev = deviationText(item);
  const src = item.word_count_target_source
    ? WORD_TARGET_SOURCE_LABEL[item.word_count_target_source]
    : undefined;
  const srcTag = src ? `[${src}]` : "";
  return `${srcTag}目标${target}字${range}${dev ? ` · ${dev}` : ""}`;
}

export default React.memo(function ProgressPanel({ items, total, success, failed, isRunning, cancelled = 0 }: ProgressPanelProps) {
  const done = success + failed + cancelled;
  // BUG-FIX 2026-10-08（P0 进度 >100%）：原实现 `Math.round(done/total*100)` 未钳位，
  // 而 done 在「重试章节先 failed 后 completed」时会 > total（后端会为同一 section_id
  // 重发两类事件）。当全部章节进入终态（generatingProgress===0）时又恰好走到未钳位的
  // else 分支 → antd Progress 直接渲染 133%。这里无条件钳到 [0,100]。
  const percent = total > 0 ? Math.max(0, Math.min(100, Math.round((done / total) * 100))) : 0;

  // BUG-F-05 修复：计算整体进度时纳入生成中的章节进度
  // 原代码仅基于完成数/总数，生成中的章节被忽略导致进度条长时间不动
  const generatingItems = items.filter((i) => i.status === "generating" || i.status === "rewriting");
  // BUG-FIX 2026-10-08（P1 除数错误）：in-flight 进度是「均值」而非「占总量的份额」，
  // 除以 total 会把它压到几乎不可见（8 并发时最大只贡献 8/total）。
  // 改为除以生成中章节数，再按「生成中章节数/总数」折算成对总进度的贡献。
  const generatingProgress = total > 0 && generatingItems.length > 0
    ? Math.round(
        (generatingItems.reduce((acc, i) => acc + (i.progress || 0), 0) / generatingItems.length)
        * (generatingItems.length / total),
      )
    : 0;
  const effectivePercent = total > 0 && generatingProgress > 0
    ? Math.min(100, percent + generatingProgress)
    : percent;

  const statusIcon = (status: string) => {
    return PHASE_CONFIG[status]?.icon || null;
  };

  const statusColor = (status: string) => {
    return PHASE_CONFIG[status]?.color || "default";
  };

  const statusText = (status: string) => {
    return PHASE_CONFIG[status]?.text || status;
  };

  return (
    <div>
      <div style={{ display: "flex", alignItems: "center", gap: 4, marginBottom: 4, flexWrap: "wrap" }}>
        <Tag color="blue" style={{ fontSize: 11, lineHeight: "16px", padding: "0 4px", marginRight: 2 }}>{total}总</Tag>
        <Tag color="green" style={{ fontSize: 11, lineHeight: "16px", padding: "0 4px", marginRight: 2 }}>{success}存</Tag>
        {failed > 0 && <Tag color="red" style={{ fontSize: 11, lineHeight: "16px", padding: "0 4px", marginRight: 2 }}>{failed}败</Tag>}
        {cancelled > 0 && <Tag style={{ fontSize: 11, lineHeight: "16px", padding: "0 4px", marginRight: 2 }}>{cancelled}取</Tag>}
        {isRunning && <Tag color="processing" style={{ fontSize: 11, lineHeight: "16px", padding: "0 4px" }}>生成中</Tag>}
      </div>
      <Progress
        percent={effectivePercent}
        size="small"
        strokeWidth={6}
        status={isRunning ? "active" : (failed > 0 ? "exception" : "success")}
      />
      {items.length > 0 && (
      <div style={{ maxHeight: 100, overflow: "auto", marginTop: 4 }}>
        <List
          size="small"
          dataSource={items}
          renderItem={(item) => (
            <List.Item style={{ padding: "1px 0" }}>
              <Space size={4} style={{ width: "100%", justifyContent: "space-between" }}>
                <Space size={4}>
                  {statusIcon(item.status)}
                  <Tag color={statusColor(item.status)} style={{ minWidth: 40, textAlign: "center", fontSize: 11, lineHeight: "14px", padding: "0 3px", margin: 0 }}>
                    {statusText(item.status)}
                  </Tag>
                  <Text
                    ellipsis
                    style={{
                      maxWidth: 180,
                      fontSize: 11,
                      textDecoration: item.status === "failed" ? "line-through" : "none",
                      color: item.status === "failed" ? "#ff4d4f" : item.status === "cancelled" ? "#999" : undefined,
                    }}
                  >
                    {item.title || `章节 #${item.section_id}`}
                  </Text>
                </Space>
                <Space size={4}>
                  {(item.status === "generating" || item.status === "rewriting") && item.progress > 0 && (
                    <Text type="secondary" style={{ fontSize: 11 }}>{item.progress}%</Text>
                  )}
                  {item.word_count != null && item.word_count > 0 && (
                    <Text type="secondary" style={{ fontSize: 11 }}>{item.word_count}字</Text>
                  )}
                  {/* CONVERGE 2026-10-12：字数目标可视化。
                      此前本面板只显示「实际 N 字」，用户无法判断是否达标，
                      必须切到「字数统计」页比对；现在单章节 SSE 事件自带
                      目标/区间，生成过程中即可看到「目标 300（240~360）· 实测 312 (+4%)」，
                      越界一目了然（与 R12 警告同源事实）。 */}
                  {describeWordTarget(item) && (
                    <Text
                      type={isWordTargetOutOfRange(item) ? "danger" : "secondary"}
                      style={{ fontSize: 11 }}
                    >
                      {describeWordTarget(item)}
                    </Text>
                  )}
                </Space>
              </Space>
            </List.Item>
          )}
          locale={{ emptyText: null }}
        />
      </div>
      )}
    </div>
  );
});