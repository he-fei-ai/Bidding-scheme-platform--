/** RecoveryBanner — 后台任务恢复提示横幅

当检测到有可恢复的后台任务（目录生成中断、正文生成暂停等）时，
在页面顶部显示一条可关闭的提示横幅，点击可跳转到对应页面继续任务。
*/
import React, { useEffect, useState } from "react";
import { Alert, Button, Space } from "antd";
import {
  ReloadOutlined,
  ClockCircleOutlined,
  CloseOutlined,
  FileTextOutlined,
  FolderOutlined,
} from "@ant-design/icons";
import { useNavigate } from "react-router-dom";
import { fetchTaskRecoveryStatus, type TaskRecoveryStatus } from "../api";

interface RecoveryBannerProps {
  projectId: number;
  /** 是否显示，默认 true */
  visible?: boolean;
  /** 用户点击"不显示"后的回调 */
  onDismiss?: () => void;
}

function RecoveryBanner({ projectId, visible: propVisible, onDismiss }: RecoveryBannerProps) {
  const navigate = useNavigate();
  const [visible, setVisible] = useState(propVisible ?? true);
  const [status, setStatus] = useState<TaskRecoveryStatus | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!visible || !projectId || projectId <= 0) return;
    setLoading(true);
    fetchTaskRecoveryStatus(projectId)
      .then((data) => {
        if (data.recovery_options && data.recovery_options.length > 0) {
          setStatus(data);
        }
      })
      .catch(() => {})
      .finally(() => setLoading(false));
  }, [projectId, visible]);

  const handleDismiss = () => {
    setVisible(false);
    onDismiss?.();
  };

  // ENHANCE 2026-09-15（Task 9）：`*_interrupted` 是后台任务快照对账出来的
  // 「上次未正常结束」条目（进程被强杀、心跳已停），与 v51_outline 这类
  // 「真能续跑」的恢复项语义不同 —— 按钮要写「重新生成」而不是「恢复」，
  // 否则用户会以为进度还在。
  const isInterrupted = (type: string) => type.endsWith("_interrupted");

  const handleResume = (type: string) => {
    if (type === "v51_outline" || type === "outline_interrupted") {
      navigate(`/projects/${projectId}/outline`);
    } else if (type === "content_generation" || type === "content_interrupted") {
      navigate(`/projects/${projectId}/sections`);
    }
    setVisible(false);
  };

  if (!visible || loading || !status || status.recovery_options.length === 0) {
    return null;
  }

  // 中断类条目（进度已丢失）不能复用「上次编辑进度尚未丢失」这句文案
  const interruptedCount = status.recovery_options.filter((opt) =>
    isInterrupted(opt.type),
  ).length;

  return (
    <Alert
      type="warning"
      showIcon
      closable
      closeIcon={<CloseOutlined style={{ fontSize: 11 }} />}
      message={
        <div style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 12, lineHeight: "20px" }}>
          <ClockCircleOutlined />
          <span>
            {interruptedCount > 0
              ? `发现 ${interruptedCount} 个上次未正常结束的任务`
              : `发现 ${status.recovery_options.length} 个可恢复的任务`}
          </span>
          {status.recovery_options.map((opt) => (
            <span key={opt.type} style={{ color: "#666" }}>
              · {opt.description}
            </span>
          ))}
          {status.recovery_options.map((opt) => (
            <Button
              key={`btn-${opt.type}`}
              size="small"
              type="primary"
              icon={<ReloadOutlined />}
              onClick={() => handleResume(opt.type)}
              style={{ minWidth: 54, fontSize: 11, height: 20, padding: "0 4px", marginLeft: 4 }}
            >
              {isInterrupted(opt.type) ? "重生成" : "恢复"}
            </Button>
          ))}
        </div>
      }
      onClose={handleDismiss}
      style={{ margin: 0, borderRadius: 0, padding: "0 8px", fontSize: 12 }}
    />
  );
}

export default React.memo(RecoveryBanner);
