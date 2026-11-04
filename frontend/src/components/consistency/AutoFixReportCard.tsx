import { Alert, Button, Card, Space, Tag, Typography } from "antd";
import { CheckOutlined } from "@ant-design/icons";
import type { AutoFixReport } from "../../api";

const { Text, Paragraph } = Typography;

/**
 * autofix 真实报告卡片（常驻展示）。
 *
 * BUG-FIX 2026-10-13（工作流缺口·关键结果不可见）：autofix 的真实报告会返回
 * `filtered`（被「就近标签词校验」判为其他事实、按设计跳过的取值计数）与
 * `scoped_section_ids` / `scope_fallback_reason`（替换范围是否被限定在冲突
 * 来源章节内）。这些**决定了替换结果**的信息此前只出现在 message 的一句话里
 * 甚至完全不出现，用户看到「改了 3 处」却不知道另有 2 处没改、为什么没改 ——
 * 这正是 2026-10-09 引入两道闸门后最需要向用户解释的事。
 *
 * L2(2026-11-03 遗留项加固)：从 `ConflictListTab.tsx` 迁出为独立子组件。
 * 纯移动：集合字段在此组件内归一（原主文件的同名变量随之删除），
 * 渲染输出不变。
 */
export default function AutoFixReportCard({
  report,
  onClose,
}: {
  report: AutoFixReport;
  onClose: () => void;
}) {
  // BUG-FIX 2026-10-13：`filtered` / `scoped_section_ids` / `scope_missing_section_ids`
  // 是后端**一直在返回、类型里刚补上**的可选字段。这里归一一次，JSX 里即可
  // 按必填数组处理（同时过 TS 收窄）。
  const filtered = report?.filtered ?? [];
  const skipped = report?.skipped ?? [];
  const scopeMissing = report?.scope_missing_section_ids ?? [];

  return (
    <Card
      size="small"
      style={{ marginBottom: 16 }}
      title={
        <Space>
          <CheckOutlined /> 自动修复结果
          <Tag color={report.status === "ok" ? "green" : "orange"}>
            {report.status}
          </Tag>
          <Text type="secondary" style={{ fontWeight: 400 }}>
            统一为：{report.unified_value}
          </Text>
        </Space>
      }
      extra={<Button size="small" type="link" onClick={onClose}>关闭</Button>}
    >
      <Paragraph style={{ marginBottom: 8 }}>{report.message}</Paragraph>
      <Space size={[8, 8]} wrap style={{ marginBottom: 8 }}>
        {/* F-1(2026-11-02)：有 written_sections 时按「实际落库」口径展示，
            老后端回退 changed_sections（两者在新契约下恒等）。 */}
        <Tag>修改章节 {report.written_sections ?? report.changed_sections} 个</Tag>
        <Tag color="green">替换 {report.total_replacements} 处</Tag>
        {!!report.replaced_values?.length && (
          <Tag color="volcano">统一掉旧值 {report.replaced_values.join("、")}</Tag>
        )}
        {skipped.length > 0 && (
          <Tag color="red">未能安全替换 {skipped.length} 章</Tag>
        )}
        {filtered.length > 0 && (
          <Tag color="orange">
            就近校验拦下 {filtered.length} 章
          </Tag>
        )}
      </Space>
      {report.scope_fallback_reason && (
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 8 }}
          message={report.scope_fallback_reason}
          description="本次替换未限定在冲突来源章节内，请核对下方明细以免误改无关数字。"
        />
      )}
      {!!report.scoped_section_ids?.length && (
        <Text type="secondary" style={{ fontSize: 12, display: "block", marginBottom: 4 }}>
          替换范围已限定在冲突来源章节：{report.scoped_section_ids.join("、")}
        </Text>
      )}
      {/* F-1(2026-11-02·可解释性)：来源声明了但本项目查不到的章节，此前静默
          丢失 —— 用户看到「只改了一半」却不知是被项目过滤还是本就无内容。 */}
      {scopeMissing.length > 0 && (
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 8 }}
          message={`冲突来源声明的 ${scopeMissing.length} 个章节在本项目未找到：${scopeMissing.join("、")}`}
          description="这些章节可能被项目过滤拦下、已被删除或正文为空，未参与本次替换；若来源信息陈旧，请重新审计。"
        />
      )}
      {skipped.map((s, i) => (
        <div key={`sk${i}`}>
          <Text type="danger" style={{ fontSize: 12 }}>
            [{s.section_id} {s.title}] {s.reason}
          </Text>
        </div>
      ))}
      {filtered.map((f, i) => (
        <div key={`ft${i}`}>
          <Text type="warning" style={{ fontSize: 12 }}>
            [{f.section_id} {f.title}] {f.reason}
          </Text>
        </div>
      ))}
    </Card>
  );
}
