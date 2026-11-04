import { Space, Typography } from "antd";
import type { ConflictEvidence } from "../../api";

const { Text } = Typography;

/**
 * G-1(2026-11-02)：「判定依据」浮层内容。
 *
 * 审计此前只下发 `severity`，误报排查只能读代码。后端 `rules.severity_rationale`
 * （与 `assess_severity` 同一实现体）随审计响应给出命中分支解释与关键入参，
 * 这里原样展示，不另起任何判定口径。
 *
 * L2(2026-11-03 遗留项加固)：从 `ConflictListTab.tsx` 迁出为独立子组件
 * （原主文件 840+ 行，拆分后按渲染块聚焦）。纯移动，渲染输出不变。
 */
export default function EvidenceDetail({ ev }: { ev: ConflictEvidence }) {
  return (
    <Space direction="vertical" size={2} style={{ maxWidth: 320 }}>
      <Text style={{ fontSize: 12 }}>{ev.reason}</Text>
      <Text type="secondary" style={{ fontSize: 12 }}>
        命中实例 {ev.instance_count} 条 · 覆盖 {ev.section_count ?? "—"} 章 · 去重取值 {ev.distinct_count} 个
      </Text>
      <Text type="secondary" style={{ fontSize: 12 }}>
        取值：{ev.distinct_values.join("、") || "—"}
      </Text>
      <Text type="secondary" style={{ fontSize: 12 }}>
        阈值：取值数 &gt; {ev.thresholds.high_distinct_gt} 判严重；章节数 ≤ {ev.thresholds.low_max_sections} 且非关键事实判轻微
      </Text>
      {ev.section_ids.length > 0 && (
        <Text type="secondary" style={{ fontSize: 12 }}>
          来源章节 id：{ev.section_ids.slice(0, 8).join("、")}
          {ev.section_ids.length > 8 ? ` …共 ${ev.section_ids.length} 章` : ""}
        </Text>
      )}
    </Space>
  );
}
