import { Button, Card, Space, Tag, Typography } from "antd";
import { ThunderboltOutlined } from "@ant-design/icons";
import type { AiRepairReport } from "../../api";

const { Text, Paragraph } = Typography;

/**
 * AI 补丁修复报告卡片（常驻展示）。
 *
 * TD-8(2026-11-02)：集合字段按 AgentRepairTab 同款一次归一 —— 后端
 * `repair_with_ai` 现在恒初始化全键，但契约只由服务内部 dict 保证——
 * 历史响应/第三方调用一旦缺键，裸 `report.rejected.length` 会抛
 * TypeError 整树卸载（2026-10-13 Agent 链路白屏 P0 的同类隐患）。
 *
 * L2(2026-11-03 遗留项加固)：从 `ConflictListTab.tsx` 迁出为独立子组件。
 * 纯移动：集合字段在此组件内归一（原主文件的同名变量随之删除），
 * 渲染输出不变。
 */
export default function AiRepairReportCard({
  report,
  onClose,
}: {
  report: AiRepairReport;
  onClose: () => void;
}) {
  const patches = report?.patches ?? [];
  const applied = report?.applied ?? [];
  const rejected = report?.rejected ?? [];
  const unresolved = report?.unresolved ?? [];
  const unwritten = report?.unwritten ?? [];
  const sections = report?.sections ?? [];
  const errors = report?.errors ?? [];

  return (
    <Card
      size="small"
      style={{ marginBottom: 16 }}
      title={
        <Space>
          <ThunderboltOutlined /> AI 补丁修复{report.dry_run ? "（预览）" : ""}
          <Tag color={report.status === "ok" ? "green" : "orange"}>{report.status}</Tag>
        </Space>
      }
      extra={<Button size="small" type="link" onClick={onClose}>关闭</Button>}
    >
      <Paragraph style={{ marginBottom: 8 }}>{report.message}</Paragraph>
      {/* TD-8(2026-11-02)：以下全部走归一后的数组变量，不再裸取 report.*.length */}
      <Space size={[8, 8]} wrap style={{ marginBottom: rejected.length ? 8 : 0 }}>
        <Tag>补丁 {patches.length} 条</Tag>
        <Tag color="green">可应用 {applied.length}</Tag>
        {rejected.length > 0 && <Tag color="orange">被拒绝 {rejected.length}</Tag>}
        <Tag>改动小节 {sections.length}</Tag>
        {/* F-1 同类(2026-11-02)：落文 rowcount 校验后的「实际落库」口径 */}
        {!report.dry_run && typeof report.written_sections === "number" && (
          <Tag color={report.written_sections > 0 ? "green" : "red"}>
            实际落库 {report.written_sections} 章
          </Tag>
        )}
        {unwritten.length > 0 && (
          <Tag color="red">落库未命中 {unwritten.length} 章</Tag>
        )}
        {/* F-2(2026-11-02)：AI 语义发现单列参考，不再虚高「待修复冲突数」 */}
        {!!report.ai_findings_used && (
          <Tag color="purple">语义发现 {report.ai_findings_used} 条（仅参考）</Tag>
        )}
        {report.conflict_states && (
          <Tag color="blue">
            冲突已解决 {report.conflict_states.resolved} / 仍待处理 {report.conflict_states.still_open}
            {!!report.conflict_states.not_verifiable &&
              ` / 需人工核对 ${report.conflict_states.not_verifiable}`}
          </Tag>
        )}
      </Space>
      {unwritten.length > 0 && (
        <div style={{ marginBottom: 8 }}>
          {unwritten.slice(0, 8).map((u, i) => (
            <div key={`uw${i}`}>
              <Text type="danger" style={{ fontSize: 12 }}>
                [{u.section_id}] {u.reason}
              </Text>
            </div>
          ))}
        </div>
      )}
      {rejected.length > 0 && (
        <div style={{ marginBottom: unresolved.length ? 8 : 0 }}>
          {rejected.slice(0, 8).map((r, i) => (
            <div key={i}>
              <Text type="warning">
                [{r.section_id} {r.title}] {r.error}
              </Text>
            </div>
          ))}
        </div>
      )}
      {unresolved.length > 0 && (
        <div>
          <Text type="secondary">AI 判定无法局部修复：</Text>
          {unresolved.slice(0, 8).map((u, i) => (
            <div key={i}>
              <Text type="secondary">
                [{u.section_id}] {u.reason}
              </Text>
            </div>
          ))}
        </div>
      )}
      {errors.length > 0 && (
        <div>
          {errors.slice(0, 5).map((e, i) => (
            <div key={i}>
              <Text type="danger">{e}</Text>
            </div>
          ))}
        </div>
      )}
    </Card>
  );
}
