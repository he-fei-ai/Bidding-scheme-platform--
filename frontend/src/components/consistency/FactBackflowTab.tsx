import { useCallback, useEffect, useRef, useState } from "react";
import {
  Alert, Button, Card, Empty, InputNumber, Space, Table, Tag, Typography, message,
} from "antd";
import {
  CheckOutlined, ReloadOutlined, ShareAltOutlined, WarningOutlined,
} from "@ant-design/icons";
import { proposeFactsFromConflicts, acceptFactProposals } from "../../api";
import type { FactProposal, FactProposalSkipped } from "../../api";
import { extractApiError } from "../../utils";

const { Text } = Typography;

export interface FactBackflowTabProps {
  projectId: number;
}

/**
 * 「事实回流」Tab —— 一致性审计 → 全局事实库的反向沉淀。
 *
 * 后端机制：
 *   扫描未解决冲突，把「正文里占多数的取值」沉淀成候选事实（占位值剔除、
 *   与既有事实去重）。用户确认后走 accept 端点落库到 global_facts。
 *
 * 两步流：
 *   1. 拉候选（GET /facts/proposals/{id}）→ Table + rowSelection
 *   2. 勾选 → 采纳（POST /facts/proposals/{id}/accept）→ 刷新
 */
export default function FactBackflowTab({ projectId }: FactBackflowTabProps) {
  const [loading, setLoading] = useState(false);
  const [proposals, setProposals] = useState<FactProposal[]>([]);
  const [selectedKeys, setSelectedKeys] = useState<React.Key[]>([]);
  const [minOccurrences, setMinOccurrences] = useState(3);
  const [scannedInfo, setScannedInfo] = useState<{ scanned: number; skipped: number } | null>(null);
  /** 采纳时被「基准类别互斥」挡下的候选（后端回传，带可读原因） */
  const [skipped, setSkipped] = useState<FactProposalSkipped[]>([]);

  // 响应竞态保护
  const loadSeqRef = useRef(0);

  const isValidProject = Number.isFinite(projectId) && projectId > 0;

  const load = useCallback(async () => {
    if (!isValidProject) return;
    const seq = ++loadSeqRef.current;
    setLoading(true);
    setSelectedKeys([]);
    try {
      const res = await proposeFactsFromConflicts(projectId, {
        minOccurrences,
      });
      if (seq !== loadSeqRef.current) return;
      setProposals(res.proposals || []);
      setScannedInfo({
        scanned: res.conflicts_scanned || 0,
        skipped: res.skipped || 0,
      });
    } catch (e) {
      if (seq === loadSeqRef.current) {
        message.error(extractApiError(e));
      }
    } finally {
      if (seq === loadSeqRef.current) setLoading(false);
    }
  }, [projectId, minOccurrences, isValidProject]);

  useEffect(() => {
    load();
  }, [load]);

  const handleAccept = async () => {
    if (selectedKeys.length === 0) {
      message.warning("请先勾选要采纳的候选事实");
      return;
    }
    const picked = proposals.filter(
      (p) => selectedKeys.includes(p.conflict_id),
    );
    try {
      const res = await acceptFactProposals(projectId, picked);
      if (res.status === "ok") {
        // ENHANCE 2026-10-13（第六轮·R-14 闭环）：被基准类别互斥挡下的候选
        // 必须显式告知 —— 否则用户看到「采纳 0 条」无从得知是被冲突规则拦了。
        setSkipped(res.skipped ?? []);
        if (res.skipped?.length) {
          message.warning(
            `已采纳 ${res.added} 条；另有 ${res.skipped.length} 条未采纳（与既有事实冲突）`,
          );
        } else {
          message.success(
            `已采纳 ${res.added} 条候选（合并进入 ${res.merged_total} 个全局事实）`,
          );
        }
        await load();
      } else {
        message.warning(`采纳完成但返回非 ok 状态：${res.status}`);
      }
    } catch (e) {
      message.error(extractApiError(e));
    }
  };

  const columns = [
    {
      title: "事实项",
      dataIndex: "fact_name",
      key: "fact_name",
      width: 160,
      render: (v: string) => <Text strong>{v}</Text>,
    },
    {
      title: "候选值",
      dataIndex: "value",
      key: "value",
      width: 140,
      render: (v: string) => <Tag color="blue">{v}</Tag>,
    },
    {
      title: "出现次数",
      dataIndex: "occurrences",
      key: "occurrences",
      width: 100,
      render: (v: number) => (
        <Text type={v >= 5 ? "success" : v >= 3 ? undefined : "secondary"}>
          {v} 次
        </Text>
      ),
    },
    {
      title: "分类",
      dataIndex: "category",
      key: "category",
      width: 100,
      render: (v: string) => <Tag>{v || "未分类"}</Tag>,
    },
    {
      title: "建议键名",
      dataIndex: "suggested_key",
      key: "suggested_key",
      width: 140,
      render: (v: string) => <Text code>{v || "—"}</Text>,
    },
    {
      title: "来源分布",
      dataIndex: "occurrences_map",
      key: "occurrences_map",
      render: (items: { value: string; count: number }[]) => (
        <Space size={[4, 4]} wrap>
          {(items || []).map((it, i) => (
            <Tag key={i}>{it.value} ×{it.count}</Tag>
          ))}
        </Space>
      ),
    },
  ];

  if (!isValidProject) {
    return <Alert type="error" showIcon message="项目 ID 无效" />;
  }

  return (
    <Space direction="vertical" style={{ width: "100%" }} size="middle">
      <Alert
        type="info"
        showIcon
        icon={<ShareAltOutlined />}
        message="事实回流（审计 → 全局事实库）"
        description="扫描未解决冲突，把正文中出现多次、占多数的取值沉淀为候选事实。勾选你确认正确的值，一次性采纳进入全局事实库——后续正文生成 / 一致性审计都会以这些值为基准。"
      />

      <Card
        size="small"
        title={<Space><ReloadOutlined />候选列表</Space>}
        extra={
          <Space>
            <Space>
              <Text>最少出现次数：</Text>
              <InputNumber
                min={1}
                max={20}
                value={minOccurrences}
                onChange={(v) => setMinOccurrences(Number(v) || 3)}
                disabled={loading}
              />
            </Space>
            <Button
              icon={<ReloadOutlined />}
              loading={loading}
              onClick={load}
            >
              重新扫描
            </Button>
            <Button
              type="primary"
              icon={<CheckOutlined />}
              disabled={selectedKeys.length === 0}
              loading={loading}
              onClick={handleAccept}
            >
              采纳选中 ({selectedKeys.length})
            </Button>
          </Space>
        }
      >
        {skipped.length > 0 && (
          <Alert
            type="warning"
            showIcon
            icon={<WarningOutlined />}
            style={{ marginBottom: 12 }}
            message={`${skipped.length} 条候选未采纳（与既有事实冲突）`}
            description={
              <Space direction="vertical" size={2}>
                {skipped.map((it, i) => (
                  <Text key={i} type="secondary" style={{ fontSize: 12 }}>
                    · {it.fact_name}={it.value} —— {it.reason}
                  </Text>
                ))}
              </Space>
            }
            closable
            onClose={() => setSkipped([])}
          />
        )}

        {scannedInfo && scannedInfo.scanned === 0 && proposals.length === 0 && (
          <Alert
            type="warning"
            showIcon
            icon={<WarningOutlined />}
            style={{ marginBottom: 12 }}
            message="没有可扫描的未解决一致性冲突。请先在「一致性冲突」Tab 运行审计并解决冲突。"
          />
        )}
        {scannedInfo && scannedInfo.scanned > 0 && (
          <Space style={{ marginBottom: 12 }}>
            <Tag color="blue">已扫描 {scannedInfo.scanned} 处冲突</Tag>
            <Tag color="orange">跳过占位值 / 与既有事实重复 {scannedInfo.skipped} 条</Tag>
            <Tag color="green">产生候选 {proposals.length} 条</Tag>
          </Space>
        )}

        <Table
          rowKey="conflict_id"
          loading={loading}
          dataSource={proposals}
          columns={columns}
          size="middle"
          pagination={{ pageSize: 10, showSizeChanger: true }}
          rowSelection={{
            selectedRowKeys: selectedKeys,
            onChange: (keys) => setSelectedKeys(keys),
          }}
          locale={{ emptyText: <Empty description={loading ? "正在扫描..." : "暂无候选"} /> }}
        />
      </Card>
    </Space>
  );
}
