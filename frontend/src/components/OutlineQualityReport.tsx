import React from "react";
import { Card, Progress, Tag, Space, Typography, Tooltip, Alert } from "antd";
import { CheckCircleOutlined, WarningOutlined, CloseCircleOutlined } from "@ant-design/icons";

const { Text } = Typography;

/** 单维评分明细（与后端 outline_quality.py 的 detail 结构对齐） */
export interface OutlineQualityDetail {
  score: number;
  label: string;
  note?: string;
}

/** 质量评分报告（后端 `_self_check_v51` → verdict["quality_score"] → completed 事件 quality 字段） */
export interface OutlineQuality {
  total_score: number;
  grade: string;
  detail: Record<string, OutlineQualityDetail>;
  node_count?: number;
  chapter_count?: number;
  leaf_count?: number;
}

export interface OutlineQualityReportProps {
  quality: OutlineQuality | null | undefined;
  /** 门禁裁决待关注问题数（completed 事件 gate_issue_count） */
  gateIssueCount?: number;
  /** 门禁问题摘要（completed 事件 gate_summary，如 "duplicate-title×2"） */
  gateSummary?: string;
  /** 已自动修复项（completed 事件 gate_auto_fixed） */
  gateAutoFixed?: string[];
}

/** 等级首字母 → 徽章色（A 绿 / B 蓝 / C 橙 / D 红） */
function gradeTagColor(grade: string): "success" | "processing" | "warning" | "error" {
  const g = (grade || "").trim().charAt(0).toUpperCase();
  if (g === "A") return "success";
  if (g === "B") return "processing";
  if (g === "C") return "warning";
  return "error";
}

/** 等级首字母 → 分数主色 */
function gradeColor(grade: string): string {
  const g = (grade || "").trim().charAt(0).toUpperCase();
  if (g === "A") return "#52c41a";
  if (g === "B") return "#1677ff";
  if (g === "C") return "#fa8c16";
  return "#f5222d";
}

/** 单维分数 → 图标与色（≥90 绿 / ≥75 蓝 / ≥60 橙 / <60 红） */
function dimensionStatus(score: number): { icon: React.ReactNode; color: string } {
  if (score >= 90) {
    return { icon: <CheckCircleOutlined style={{ color: "#52c41a" }} />, color: "#52c41a" };
  }
  if (score >= 75) {
    return { icon: <CheckCircleOutlined style={{ color: "#1677ff" }} />, color: "#1677ff" };
  }
  if (score >= 60) {
    return { icon: <WarningOutlined style={{ color: "#fa8c16" }} />, color: "#fa8c16" };
  }
  return { icon: <CloseCircleOutlined style={{ color: "#f5222d" }} />, color: "#f5222d" };
}

/**
 * 目录生成质量报告卡片（《目录生成工作流程优化方案》优化 6 / 第五节前端展示）。
 *
 * 数据链：后端 compute_outline_facts → outline_quality.score_outline_quality
 * （五维加权评分 + A/B/C/D 等级）→ _self_check_v51 写入门禁裁决 →
 * SSE completed 事件 quality 字段 → 本组件渲染。
 *
 * quality 为 null（直出路径 / 评分关闭 / 重连早于完成事件）时返回 null，
 * 由调用方决定整体不展示，避免出现空卡片。
 */
export default function OutlineQualityReport({
  quality,
  gateIssueCount,
  gateSummary,
  gateAutoFixed,
}: OutlineQualityReportProps) {
  if (!quality || typeof quality.total_score !== "number") return null;

  const total = Math.round(quality.total_score * 10) / 10;
  const grade = quality.grade || "D（需整改）";
  const color = gradeColor(grade);
  const details = Object.values(quality.detail || {}).filter(
    (d): d is OutlineQualityDetail => !!d && typeof d.score === "number",
  );

  return (
    <Card
      size="small"
      title={
        <Space>
          <Text strong>目录生成质量报告</Text>
          <Tag color={gradeTagColor(grade)}>{grade}</Tag>
        </Space>
      }
      style={{ marginTop: 12 }}
      data-testid="outline-quality-report"
    >
      {/* 综合评分 */}
      <div
        style={{
          display: "flex",
          alignItems: "baseline",
          gap: 8,
          marginBottom: 12,
          padding: "8px 12px",
          backgroundColor: "#f6f8fa",
          borderRadius: 6,
          border: "1px solid #e8e8e8",
        }}
      >
        <span style={{ color: "#8c8c8c", fontSize: 12 }}>综合评分：</span>
        <Text strong style={{ fontSize: 24, color }}>
          {total}
        </Text>
        <span style={{ color: "#8c8c8c", fontSize: 12 }}>分</span>
        <span style={{ color: "#8c8c8c", fontSize: 12 }}>
          （{quality.chapter_count ?? 0} 章 / {quality.leaf_count ?? 0} 个三级节点，共{" "}
          {quality.node_count ?? 0} 节点）
        </span>
      </div>

      {/* 五维明细 */}
      <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
        {details.map((d) => {
          const st = dimensionStatus(d.score);
          return (
            <div key={d.label} style={{ display: "flex", alignItems: "center", gap: 8 }}>
              {st.icon}
              <Text style={{ fontSize: 12, width: 72, color: "#595959" }}>{d.label}</Text>
              <Progress
                percent={Math.round(d.score)}
                strokeColor={st.color}
                size="small"
                style={{ flex: 1, margin: 0, minWidth: 120 }}
              />
              <Text
                strong
                style={{ fontSize: 12, width: 32, textAlign: "right", color: st.color }}
              >
                {Math.round(d.score)}
              </Text>
              {d.note ? (
                <Tooltip title={d.note}>
                  <Text type="secondary" style={{ fontSize: 11, maxWidth: 220 }} ellipsis>
                    {d.note}
                  </Text>
                </Tooltip>
              ) : null}
            </div>
          );
        })}
      </div>

      {/* 门禁问题与自动修复摘要 */}
      {typeof gateIssueCount === "number" && gateIssueCount > 0 ? (
        <Alert
          type="warning"
          showIcon
          style={{ marginTop: 12 }}
          message={
            <span style={{ fontSize: 12 }}>
              门禁发现 {gateIssueCount} 项待关注问题
              {gateSummary ? `（${gateSummary}）` : ""}，已在目录编辑器中标注，可人工复核后微调
            </span>
          }
        />
      ) : (
        <Alert
          type="success"
          showIcon
          style={{ marginTop: 12 }}
          message={<span style={{ fontSize: 12 }}>门禁校验通过，未发现待关注问题</span>}
        />
      )}

      {Array.isArray(gateAutoFixed) && gateAutoFixed.length > 0 ? (
        <div style={{ marginTop: 8 }}>
          <Text type="secondary" style={{ fontSize: 11 }}>
            已自动修复：
          </Text>
          {gateAutoFixed.map((item) => (
            <Tag key={item} color="success" style={{ fontSize: 10, margin: "2px 4px 2px 0" }}>
              {item}
            </Tag>
          ))}
        </div>
      ) : null}
    </Card>
  );
}
