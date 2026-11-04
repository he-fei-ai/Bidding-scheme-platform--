import { useEffect, useState } from "react";
import { Alert, Button, Descriptions, Drawer, Space, Spin, Typography } from "antd";
import { ReloadOutlined } from "@ant-design/icons";
import { getConsistencyMetrics } from "../../api";
import type { ConsistencyMetrics } from "../../api";
// BUG-FIX 2026-11-04（第 11 轮·P1·生产构建炸弹）：`extractApiError` 导出自
// `utils.ts`，本文件此前从 `api` 模块导入 —— 该命名导出不存在，TypeScript
// 编译报 TS2305、vite 生产构建直接失败；即便以宽容模式构建，运行时调用也是
// undefined 调用 TypeError（拉取失败分支必触发）。改从 utils 导入。
import { extractApiError } from "../../utils";

const { Text, Paragraph } = Typography;

/** 指标键 → 中文标签（后端 consistency_metrics.py 的计数器名）。 */
const METRIC_LABELS: Record<string, string> = {
  audit_runs: "审计执行次数",
  audit_failed: "审计失败次数",
  ai_audit_runs: "AI 审计执行次数",
  ai_findings: "AI 语义发现数",
  ai_findings_repaired: "AI 发现已修复数",
  ai_patch_rejected: "AI 补丁被拒绝数",
  guard_interceptions: "生成守卫拦截数",
  // ENHANCE 2026-10-17（第八轮·T7-3）新增的运维维度键此前无标签，
  // 抽屉里渲染成裸英文键名（用户无法解读）。补齐与 KNOWN_KEYS 一一对应。
  audit_duration_ms_total: "审计累计耗时（毫秒）",
  conflicts_detected_total: "累计检出规则冲突数",
  autofix_applied_total: "自动修复成功次数",
  autofix_no_change_total: "自动修复未改动次数",
  autofix_failed_total: "自动修复异常次数",
  autofix_edits_total: "自动修复累计改写处数",
};

/**
 * 一致性校核「运维指标」抽屉（遗留项 L3，2026-11-03）。
 *
 * 后端 `GET /consistency/metrics`（ENHANCE 2026-10-16 / T5 + G-3）返回进程内
 * 累计计数器与 `since` / `notes`，此前前端零消费 —— 运维观测能力存在但用户
 * 够不着。本抽屉提供只读入口：点开才拉取（不影响首屏请求数），展示当前
 * API 进程的累计计数、计数起点与口径说明。纯展示，不作为审计结论来源。
 *
 * 老后端（无 since/notes）回退为不展示对应区块，指标仍按值渲染。
 */
export default function ConsistencyMetricsDrawer({
  open,
  onClose,
}: {
  open: boolean;
  onClose: () => void;
}) {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [metrics, setMetrics] = useState<ConsistencyMetrics | null>(null);
  const [since, setSince] = useState<string | undefined>(undefined);
  const [notes, setNotes] = useState<string | undefined>(undefined);

  const load = async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await getConsistencyMetrics();
      setMetrics(res.metrics || null);
      setSince(res.since);
      setNotes(res.notes);
    } catch (e) {
      setError(extractApiError(e));
    } finally {
      setLoading(false);
    }
  };

  // 首次打开时拉取；之后再打开沿用上次数据（可手动刷新），不反复打请求。
  useEffect(() => {
    if (open && !metrics && !error) {
      load();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  const items = metrics
    ? Object.entries(metrics).map(([key, value]) => ({
        key,
        label: METRIC_LABELS[key] ?? key,
        children: <Text code>{value}</Text>,
      }))
    : [];

  return (
    <Drawer
      title="一致性校核运维指标"
      open={open}
      onClose={onClose}
      width={480}
      extra={
        <Button
          size="small"
          icon={<ReloadOutlined />}
          loading={loading}
          onClick={load}
        >
          刷新
        </Button>
      }
    >
      {error && <Alert type="error" showIcon message={error} style={{ marginBottom: 12 }} />}
      {since && (
        <Paragraph type="secondary" style={{ marginBottom: 4 }}>
          计数起点（本进程启动时刻）：{since}
        </Paragraph>
      )}
      {notes && (
        <Paragraph type="secondary" style={{ marginBottom: 12, fontSize: 12 }}>
          {notes}
        </Paragraph>
      )}
      {loading && !metrics ? (
        <Space style={{ width: "100%", justifyContent: "center", padding: 24 }}>
          <Spin />
        </Space>
      ) : metrics ? (
        <Descriptions column={1} size="small" bordered items={items} />
      ) : null}
      <Paragraph type="secondary" style={{ marginTop: 12, fontSize: 12 }}>
        指标为当前 API 进程内存累计、重启归零，仅用于实时观测，不作为审计结论来源；
        多进程部署下各进程独立计数。
      </Paragraph>
    </Drawer>
  );
}
