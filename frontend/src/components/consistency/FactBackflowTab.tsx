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
  /** 采纳请求进行中（独立于扫描 loading，防重复提交） */
  const [accepting, setAccepting] = useState(false);
  /** 同步防重入守卫：React 批处理下同 tick 双击读不到最新 state，用 ref 兜底 */
  const acceptingRef = useRef(false);
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
    // BUG-FIX 2026-10-27（P3·skipped 告警残留）：skipped 详情由「采纳」动作产生，
    // 但重新扫描/调整参数后旧告警仍挂着，展示的是过期信息。扫描即代表开始
    // 新一轮决策，先清掉；采纳后由 handleAccept 在刷新完成时重新回填。
    setSkipped([]);
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
    // BUG-FIX 2026-10-27（P3·逐键触发全量扫描）：minOccurrences 在 load 依赖里，
    // antd InputNumber 每次敲键都 onChange → 每键一次全项目正文扫描（无防抖）。
    // 现对 load 触发做 350ms 防抖：连续输入只发最后一次；projectId 切换的
    // 首扫同样走此路径（延迟一个防抖窗口，可接受）。
    const t = window.setTimeout(() => {
      load();
    }, 350);
    return () => window.clearTimeout(t);
  }, [load]);

  const handleAccept = async () => {
    // BUG-FIX 2026-10-27（P3·采纳无独立 loading）：原实现按钮 loading 复用扫描态，
    // 采纳请求期间按钮可再次点击 → 重复 POST（第二次被后端锁串行化后回
    // added=0 再弹一次 success）。现用独立 accepting 态 + ref 同步守卫防重入。
    if (acceptingRef.current) return;
    if (selectedKeys.length === 0) {
      message.warning("请先勾选要采纳的候选事实");
      return;
    }
    const picked = proposals.filter(
      (p) => selectedKeys.includes(p.conflict_id),
    );
    acceptingRef.current = true;
    setAccepting(true);
    try {
      const res = await acceptFactProposals(projectId, picked);
      if (res.status === "ok") {
        // ENHANCE 2026-10-13（第六轮·R-14 闭环）：被基准类别互斥挡下的候选
        // 必须显式告知 —— 否则用户看到「采纳 0 条」无从得知是被冲突规则拦了。
        // 注意：load() 现在会先清空 skipped（BUG-FIX 2026-10-27），所以这里
        // 必须在刷新完成后再回填本次结果，否则告警会被自己的刷新吞掉。
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
        setSkipped(res.skipped ?? []);
      } else {
        message.warning(`采纳完成但返回非 ok 状态：${res.status}`);
      }
    } catch (e) {
      message.error(extractApiError(e));
    } finally {
      acceptingRef.current = false;
      setAccepting(false);
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
              loading={accepting}
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
