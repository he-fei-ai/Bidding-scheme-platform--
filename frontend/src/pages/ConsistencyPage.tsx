import { useEffect, useState } from "react";
import { useParams, useNavigate } from "react-router-dom";
import { Alert, Button, Space, Tabs, Typography } from "antd";
import { ArrowLeftOutlined, AuditOutlined } from "@ant-design/icons";
import { getConsistencyRules } from "../api";
import type { ConsistencyRules } from "../api";
import { applyConsistencySeverityMeta } from "../utils";
import ConflictListTab from "../components/consistency/ConflictListTab";
import ScoringComplianceTab from "../components/consistency/ScoringComplianceTab";

const { Text } = Typography;

/**
 * 一致性校核页（工作流第 5 步「一致性校验」）。
 *
 * ENHANCE 2026-10-08：由 484 行单文件拆成「页面壳 + 两个 Tab 组件」：
 *   - `components/consistency/ConflictListTab`      —— 冲突审计与修复
 *   - `components/consistency/ScoringComplianceTab` —— 评分标准达标对齐（此前无 UI）
 *   - `components/consistency/AuditSummaryCards`    —— 统计卡片
 *
 * 拆分的直接收益：Tab 级 / 组件级交互测试可以只挂载被测的那一块，不必把页面壳、
 * 另一个 Tab 的请求、三个弹窗一起拉起来 —— 既快又稳定（原先单测一个断言要 15s，
 * 且与其它测试文件并发时会因 CPU 争用超时）。
 *
 * 同时新增 Tab 级审核规则对齐：拉 `GET /consistency/rules` 把「严重/中等/轻微」
 * 的取值与配色下发到 `utils.ts`，使前后端判定标准同源（失败静默退回静态兜底）。
 */
export default function ConsistencyPage() {
  const { id } = useParams<{ id: string }>();
  const projectId = Number(id);
  const navigate = useNavigate();

  const [activeTab, setActiveTab] = useState("conflicts");
  // 规则快照：仅用于一次性对齐严重度映射，不驱动渲染，避免多一次重排
  const [, setRules] = useState<ConsistencyRules | null>(null);

  // 审核规则命名空间对齐（ENHANCE 2026-10-08）。
  // 失败**不提示**：静态兜底（utils.ts 内置表）与后端默认值一致，
  // 拉取失败不应阻塞用户审计流程。
  useEffect(() => {
    let cancelled = false;
    getConsistencyRules()
      .then((rules) => {
        if (cancelled) return;
        applyConsistencySeverityMeta(rules?.severity?.meta);
        setRules(rules);
      })
      .catch(() => {
        /* 静默：退回静态兜底 */
      });
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <div style={{ maxWidth: 1200, margin: "0 auto" }}>
      <div style={{ marginBottom: 16, display: "flex", alignItems: "center", justifyContent: "space-between" }}>
        <Space>
          <Button icon={<ArrowLeftOutlined />} onClick={() => navigate(`/projects/${projectId}/sections`)}>
            返回正文
          </Button>
          <Typography.Title level={4} style={{ margin: 0 }}>
            <AuditOutlined /> 全文一致性审计
          </Typography.Title>
        </Space>
      </div>

      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 16 }}
        message="对全文关键事实（如工期、金额、人员配置、技术参数等）进行交叉一致性审计，发现同一事实在不同章节中表述不一致的冲突。"
        description="两种修复方式：表格里的「自动修复」把冲突取值统一成同一个值（适合「30 天 / 45 天」这类）；「AI 补丁修复」由 AI 读懂语义后产出局部替换补丁（适合「2026 年 5 月竣工」vs「工期 90 天」这类不同措辞的冲突）。补丁只替换 AI 逐字引用的原文片段，定位不到或有多处命中都会被拒绝，不会误改正文。"
      />

      <Tabs
        activeKey={activeTab}
        onChange={setActiveTab}
        items={[
          {
            key: "conflicts",
            label: "一致性冲突",
            children: <ConflictListTab projectId={projectId} />,
          },
          {
            key: "scoring",
            label: "评分标准达标",
            children: <ScoringComplianceTab projectId={projectId} />,
          },
        ]}
      />

      <Text type="secondary" style={{ fontSize: 12 }}>
        提示：点击冲突的「涉及章节」标签可直接跳转到对应章节定位问题。
      </Text>
    </div>
  );
}