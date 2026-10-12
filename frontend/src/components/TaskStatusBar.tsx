import { useEffect, useState } from "react";
import { Progress, Tag, Tooltip } from "antd";
import {
  SyncOutlined,
  PauseCircleOutlined,
  CheckCircleOutlined,
} from "@ant-design/icons";
import { useNavigate } from "react-router-dom";
import { taskStore, type ActiveTask } from "../services/taskStore";

/** 任务类型 → 中文名（与 taskStore.TaskType 对齐） */
const TASK_LABEL: Record<string, string> = {
  outline: "目录生成",
  content: "正文生成",
  export: "文档导出",
  file_parse: "文件解析",
};

/** 任务类型 → 工作流路由段（点击跳转到对应页面查看进度） */
const TASK_ROUTE: Record<string, string> = {
  outline: "outline",
  content: "sections",
  export: "export",
  file_parse: "files",
};

/**
 * 顶栏「后台任务实时显示栏」（2026-10-12 新增）。
 *
 * 订阅全局 taskStore，实时展示当前进行中的后台任务（目录生成 / 正文生成等）：
 * 每个任务一行「类型标签 + 进度条 + 文案」，点击跳到对应工作流页。taskStore 是
 * 跨页面单例，故切到其他页面时任务仍在栏中显示，返回原页面继续更新。空闲时显示
 * 轻量占位，保证「栏」常驻、位置稳定。
 */
export default function TaskStatusBar() {
  const [tasks, setTasks] = useState<ActiveTask[]>([]);
  const navigate = useNavigate();

  // subscribe 会立即回推一次当前快照，卸载时取消订阅
  useEffect(() => taskStore.subscribe(setTasks), []);

  if (tasks.length === 0) {
    return (
      <span
        style={{
          color: "#a6adbb",
          fontSize: 12.5,
          display: "inline-flex",
          alignItems: "center",
          gap: 6,
        }}
      >
        <CheckCircleOutlined />
        后台空闲，暂无进行中的任务
      </span>
    );
  }

  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        gap: 18,
        flexWrap: "wrap",
        justifyContent: "center",
        minWidth: 0,
      }}
    >
      {tasks.map((t) => {
        const label = TASK_LABEL[t.taskType] ?? t.taskType;
        const paused = t.status === "paused";
        return (
          <Tooltip key={t.taskId} title={`${t.message}（点击查看）`}>
            <div
              role="button"
              tabIndex={0}
              onClick={() =>
                navigate(`/projects/${t.projectId}/${TASK_ROUTE[t.taskType] ?? "sections"}`)
              }
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  navigate(`/projects/${t.projectId}/${TASK_ROUTE[t.taskType] ?? "sections"}`);
                }
              }}
              style={{
                display: "flex",
                alignItems: "center",
                gap: 8,
                cursor: "pointer",
                minWidth: 0,
              }}
            >
              <Tag
                color={paused ? "orange" : "processing"}
                icon={paused ? <PauseCircleOutlined /> : <SyncOutlined spin />}
                style={{ margin: 0, flexShrink: 0 }}
              >
                {label}
              </Tag>
              <Progress
                percent={t.progress}
                size="small"
                status={paused ? "normal" : "active"}
                style={{ width: 120, margin: 0, flexShrink: 0 }}
              />
              <span
                style={{
                  fontSize: 12,
                  color: "#5b6675",
                  maxWidth: 200,
                  overflow: "hidden",
                  textOverflow: "ellipsis",
                  whiteSpace: "nowrap",
                }}
              >
                {t.message}
              </span>
            </div>
          </Tooltip>
        );
      })}
    </div>
  );
}
