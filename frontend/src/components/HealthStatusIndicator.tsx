import React, { useEffect, useState } from "react";
import { Tooltip } from "antd";
import { healthCheck } from "../api";

type HealthState = "checking" | "healthy" | "unhealthy";

const HEALTH_LABEL: Record<HealthState, string> = {
  checking: "正在检测服务…",
  healthy: "后端服务正常",
  unhealthy: "后端服务不可用",
};

const HealthStatusIndicator: React.FC = () => {
  const [status, setStatus] = useState<HealthState>("checking");

  useEffect(() => {
    let mounted = true;
    const checkHealth = async () => {
      try {
        await healthCheck();
        if (mounted) setStatus("healthy");
      } catch {
        if (mounted) setStatus("unhealthy");
      }
    };
    checkHealth();
    const interval = setInterval(checkHealth, 30000);
    return () => {
      mounted = false;
      clearInterval(interval);
    };
  }, []);

  return (
    <Tooltip title={HEALTH_LABEL[status]}>
      <span className="health-indicator" role="status" aria-live="polite">
        <span className={`health-indicator__dot is-${status === "healthy" ? "ok" : status === "unhealthy" ? "error" : "checking"}`} />
        <span>{status === "healthy" ? "服务正常" : status === "unhealthy" ? "服务异常" : "检测中"}</span>
      </span>
    </Tooltip>
  );
};

export default HealthStatusIndicator;
