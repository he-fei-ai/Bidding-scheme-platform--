import { useState } from "react";
import {
  Alert, Button, Card, Empty, InputNumber, Space, Tag, Typography, message,
} from "antd";
import {
  BugOutlined, CheckCircleOutlined, CloseCircleOutlined,
  ThunderboltOutlined, WarningOutlined,
} from "@ant-design/icons";
import { repairConsistencyWithAgent } from "../../api";
import type { AgentRepairReport } from "../../api";
import { extractApiError } from "../../utils";

const { Text, Paragraph } = Typography;

export interface AgentRepairTabProps {
  projectId: number;
}

/**
 * 「Agent 多轮修复」Tab —— 全文一致性的多轮往返修复。
 *
 * 差异 vs ConflictListTab 的「AI 补丁修复」：
 *   - 补丁修复：一次性给 AI 全部冲突，AI 产 patch → 宿主验证 → 应用；
 *     适合冲突少、语义差异小的场景，耗时短、token 可控。
 *   - Agent 修复：多轮往返（search / read / replace），AI 自主选择修复顺序；
 *     适合长文（几十万字）、冲突多、语义复杂的场景，更贵但覆盖更全。
 *
 * 安全模型与补丁路径一致：AI 只提修改意图，宿主唯一命中校验后执行，
 * Agent 编造的 old_text / 越界 section_id 一律拒绝，幻觉 0 落库。
 *
 * ── BUG-FIX 2026-10-13（P0 · 真实后端响应导致整页白屏）：本组件原先对
 * `report.rejected / unresolved / errors` **无保护地**取 `.length`。而后端
 * `services/agent_repair.py` 的报告 dict 当时**没有 `errors` 键**（与姊妹链路
 * `consistency_repair.repair_with_ai` 不对称 —— 后者有）。于是
 *   TypeError: Cannot read properties of undefined (reading 'length')
 * 一旦抛出，React 18 在无错误边界时**卸载整棵组件树**，而 `ConsistencyPage`
 * 未挂 ErrorBoundary —— 「执行修复」拿到任何一份真实响应（**含成功响应**）
 * 都会让整个一致性校核页白屏，Tab 全切不动。
 * 现两处同改：① 后端补齐 `errors` / `max_turns`；② 前端全部改可选链，
 * 对历史响应与第三方调用方同样健壮。另见 `AgentRepairTab.dom.test.tsx`
 * 「真实契约」回归用例（桩数据此前手写了契约里不存在的字段，把 P0 永久掩盖）。
 */
export default function AgentRepairTab({ projectId }: AgentRepairTabProps) {
  const [repairing, setRepairing] = useState(false);
  const [report, setReport] = useState<AgentRepairReport | null>(null);
  const [maxTurns, setMaxTurns] = useState(8);

  const isValidProject = Number.isFinite(projectId) && projectId > 0;

  const handleRepair = async (dryRun: boolean) => {
    setRepairing(true);
    setReport(null);
    try {
      const result = await repairConsistencyWithAgent(projectId, {
        dryRun,
        maxTurns,
      });
      setReport(result);
      if (result.status === "ok" && result.success) {
        message.success(result.message || "Agent 修复完成");
      } else if (result.success === false) {
        // BUG-FIX 2026-10-27（P3·硬失败被当「无需修复」）：success=false 有两类
        // 截然不同的语义 —— ①短路返回（空项目/无冲突，无需修复）；②真实执行
        // 失败（工作区准备失败 / Provider 失败 / 章节被清空拒绝落库 / 落库异常，
        // 后端同时填充 errors 数组）。原实现一律弹蓝色 info「没有需要修复的
        // 一致性冲突」，把真失败包装成了正常情况。现按 errors 是否非空分支。
        if (result.errors?.length) {
          message.error(result.errors[0] || result.message || "Agent 修复执行失败");
        } else {
          message.info(result.message || "没有需要修复的一致性冲突");
        }
      } else {
        message.warning(result.message || "Agent 未完成全部修复");
      }
    } catch (e) {
      message.error(extractApiError(e));
    } finally {
      setRepairing(false);
    }
  };

  if (!isValidProject) {
    return <Alert type="error" showIcon message="项目 ID 无效，无法启动 Agent 修复" />;
  }

  // BUG-FIX 2026-10-13（P0）：把「可能缺失」的集合字段在这里一次性归一为数组。
  // 之前散落在 JSX 里写 `report.rejected?.length > 0 && ...{report.rejected.length}`，
  // 既脆（漏一处就是白屏）又过不了 TS 收窄（`?.length > 0` 的结果不会把
  // `report.rejected` 收窄进 JSX 分支）。归一后全部按必填数组处理。
  const rejected = report?.rejected ?? [];
  const unresolved = report?.unresolved ?? [];
  const errors = report?.errors ?? [];
  const changedSections = report?.changed_sections ?? [];

  return (
    <Space direction="vertical" style={{ width: "100%" }} size="middle">
      {/* 顶部操作区 */}
      <Alert
        type="info"
        showIcon
        icon={<BugOutlined />}
        message="Agent 多轮修复"
        description="AI 以多轮往返方式（查找 → 读取 → 修改）逐次解决一致性冲突，适合长文、冲突密集、语义复杂的场景。每次请求最多 600 秒，由「最大往返轮数」控制成本（默认 8 轮，可增到 20 轮）。Agent 只提修改意图，宿主唯一命中校验后才会写正文——幻觉 0 落库。"
      />

      <Card size="small" title={<Space><ThunderboltOutlined />启动修复</Space>}>
        <Space>
          <Text>最大往返轮数：</Text>
          <InputNumber
            min={1}
            max={20}
            value={maxTurns}
            onChange={(v) => setMaxTurns(Number(v) || 8)}
            disabled={repairing}
          />
          <Button
            icon={<ThunderboltOutlined />}
            loading={repairing}
            onClick={() => handleRepair(true)}
          >
            预览（dry run）
          </Button>
          <Button
            type="primary"
            icon={<ThunderboltOutlined />}
            loading={repairing}
            onClick={() => handleRepair(false)}
          >
            执行修复
          </Button>
        </Space>
        <Text type="secondary" style={{ fontSize: 12, marginTop: 8, display: "block" }}>
          预览模式只跑 Agent 会话与报告，不写正文；执行模式会将 Agent 判定的补丁落库。
        </Text>
      </Card>

      {/* 修复结果卡片 */}
      {report ? (
        <Card
          title={
            <Space>
              {report.status === "ok" && report.success
                ? <CheckCircleOutlined style={{ color: "#52c41a" }} />
                : <WarningOutlined style={{ color: "#faad14" }} />}
              Agent 修复{report.dry_run ? "（预览）" : ""}结果
              <Tag color={report.status === "ok" && report.success ? "green" : "orange"}>
                {report.status}
              </Tag>
              {report.turns > 0 && (
                <Tag color="blue">
                  {report.turns}/{report.max_turns ?? maxTurns} 轮
                </Tag>
              )}
            </Space>
          }
          extra={
            <Button size="small" type="link" onClick={() => setReport(null)}>
              关闭
            </Button>
          }
        >
          <Paragraph style={{ marginBottom: 12 }}>
            {report.message}
          </Paragraph>

          {/* 统计指标 */}
          <Space size={[8, 8]} wrap style={{ marginBottom: 16 }}>
            {changedSections.length > 0 && (
              <Tag color="geekblue">修改章节 {changedSections.length} 个</Tag>
            )}
            {report.conflict_states && (
              <>
                <Tag color="green">
                  冲突已解决 {report.conflict_states.resolved}
                </Tag>
                {report.conflict_states.still_open > 0 && (
                  <Tag color="orange">仍待处理 {report.conflict_states.still_open}</Tag>
                )}
              </>
            )}
            {rejected.length > 0 && (
              <Tag color="red">被拒绝 {rejected.length} 条</Tag>
            )}
            {unresolved.length > 0 && (
              <Tag color="orange">无法局部修复 {unresolved.length} 处</Tag>
            )}
            {errors.length > 0 && (
              <Tag color="red">错误 {errors.length} 条</Tag>
            )}
          </Space>

          {/* 修改章节列表 */}
          {changedSections.length > 0 && (
            <div style={{ marginBottom: 12 }}>
              <Text strong>涉及修改的章节：</Text>
              <Paragraph style={{ marginBottom: 0, marginTop: 4 }}>
                {changedSections
                  .map((s) => `[${s.id}] ${s.title}`)
                  .join("；")}
              </Paragraph>
            </div>
          )}

          {/* 被拒绝的补丁 */}
          {rejected.length > 0 && (
            <div style={{ marginBottom: 12 }}>
              <Space>
                <CloseCircleOutlined style={{ color: "#ff4d4f" }} />
                <Text strong>被拒绝的补丁：</Text>
              </Space>
              <div style={{ marginTop: 4 }}>
                {rejected.slice(0, 10).map((r: unknown, i: number) => {
                  const item = r as { section_id?: string; title?: string; error?: string };
                  return (
                    <Text type="warning" key={i} style={{ display: "block", fontSize: 12 }}>
                      [{item.section_id ?? "?"}] {item.title ?? ""} — {item.error ?? "匹配失败"}
                    </Text>
                  );
                })}
                {rejected.length > 10 && (
                  <Text type="secondary" style={{ fontSize: 12 }}>
                    ... 及其他 {rejected.length - 10} 条
                  </Text>
                )}
              </div>
            </div>
          )}

          {/* 无法局部修复 */}
          {unresolved.length > 0 && (
            <div style={{ marginBottom: 12 }}>
              <Space>
                <WarningOutlined style={{ color: "#faad14" }} />
                <Text strong>Agent 判定无法局部修复：</Text>
              </Space>
              <div style={{ marginTop: 4 }}>
                {unresolved.map((u, i) => (
                  <Text type="secondary" key={i} style={{ display: "block", fontSize: 12 }}>
                    [{u.section_id}] {u.reason}
                  </Text>
                ))}
              </div>
            </div>
          )}

          {/* 错误 */}
          {errors.length > 0 && (
            <div>
              {errors.slice(0, 5).map((e, i) => (
                <Text type="danger" key={i} style={{ display: "block", fontSize: 12 }}>
                  {e}
                </Text>
              ))}
              {errors.length > 5 && (
                <Text type="secondary" style={{ fontSize: 12 }}>
                  ... 及其他 {errors.length - 5} 条错误
                </Text>
              )}
            </div>
          )}
        </Card>
      ) : !repairing ? (
        <Card>
          <Empty description="尚未启动 Agent 修复。点击上方「预览」或「执行」按钮开始。" />
        </Card>
      ) : null}
    </Space>
  );
}
