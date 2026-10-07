/**
 * 全局任务存储 - 跨页面追踪后台任务
 *
 * 用于在用户导航到其他页面时保持任务状态，返回时恢复。
 * 采用极简实现：单例类 + 事件订阅，无需 Redux/Zustand。
 *
 * 修复历史：
 * - 2026-08-21: 修复completeTask的setTimeout无清理机制，添加版本号防止竞态删除
 * - 2026-09-04: 契约BUG5 修复：syncFromBackend 状态枚举归一化——后端 active/processing/
 *   queued/pending 未被识别，跨页面后台任务指示器恒为空；同时修正 taskKey→TaskType
 *   映射（v51_outline 曾被误标为"正文生成"）并按 completed_count/total_sections
 *   换算正文生成进度
 * - 2026-09-04: 清理无调用死代码——syncFromBackend/startPeriodicSync/periodicSyncAll/
 *   cleanupExpiredTasks 及配套字段（syncTimer/syncedProjects/TASK_EXPIRY_MS/
 *   SYNC_INTERVAL_MS）和 import fetchTaskRecoveryStatus。这些 2026-08-21 新增的
 *   "后端同步链路"从未被任何页面接线，RecoveryBanner 独立消费后端 API、
 *   OutlineEditorPage/Layout 直接 startTask/completeTask，跨页面恢复依赖
 *   taskStore 内存态 + 手动重进页面触发 fetchPendingV51Task / RecoveryBanner，
 *   此链路与前端 state 同步是冗余的。如未来需要整页刷新后自动恢复，应在 App.tsx
 *   路由层用新 API 接线，而非复活这段悬空的定期同步框架。
 */

export type TaskType = 'outline' | 'content' | 'export' | 'file_parse';

export interface ActiveTask {
  taskId: string;
  projectId: number;
  taskType: TaskType;
  status: 'running' | 'paused' | 'completed' | 'failed' | 'cancelled';
  progress: number; // 0-100
  message: string;
  startedAt: number;
  lastUpdated: number;
}

type TaskListener = (tasks: ActiveTask[]) => void;

class TaskStoreImpl {
  private tasks: Map<string, ActiveTask> = new Map();
  private listeners: Set<TaskListener> = new Set();
  // 每个任务的清理计时器ID映射，用于竞态防护
  private cleanupTimers: Map<string, { timerId: ReturnType<typeof setTimeout>; version: number }> = new Map();
  private static instance: TaskStoreImpl | null = null;

  static getInstance(): TaskStoreImpl {
    if (!TaskStoreImpl.instance) {
      TaskStoreImpl.instance = new TaskStoreImpl();
    }
    return TaskStoreImpl.instance;
  }

  /** 订阅任务变更 */
  subscribe(listener: TaskListener): () => void {
    this.listeners.add(listener);
    // 立即发送当前状态
    listener(this.getActiveTasks());
    return () => {
      this.listeners.delete(listener);
    };
  }

  /** 通知所有订阅者 */
  private notify() {
    const tasks = this.getActiveTasks();
    for (const listener of this.listeners) {
      try {
        listener(tasks);
      } catch {
        // 忽略订阅者错误
      }
    }
  }

  /** 开始追踪任务 */
  startTask(task: ActiveTask) {
    this.tasks.set(task.taskId, task);
    this.notify();
  }

  /** 更新任务进度 */
  updateTask(taskId: string, updates: Partial<Omit<ActiveTask, 'taskId' | 'projectId' | 'taskType' | 'startedAt'>>) {
    const task = this.tasks.get(taskId);
    if (task) {
      Object.assign(task, updates, { lastUpdated: Date.now() });
      this.notify();
    }
  }

  /** 完成任务
   * 
   * 添加版本号机制防止setTimeout竞态删除
   * 如果任务在30秒内被重新启动（status变为running），清理计时器版本号不匹配，
   * 将自动放弃删除操作。
   */
  completeTask(taskId: string, status: ActiveTask['status'], message?: string) {
    const task = this.tasks.get(taskId);
    if (task) {
      task.status = status;
      if (message) task.message = message;
      task.lastUpdated = Date.now();
      this.notify();
      
      // 清除旧的清理计时器（如果存在）
      const oldCleanup = this.cleanupTimers.get(taskId);
      if (oldCleanup) {
        clearTimeout(oldCleanup.timerId);
      }
      
      // 创建新的清理计时器，带版本号
      const version = Date.now();
      const timerId = setTimeout(() => {
        // 检查版本号是否匹配（防止任务被重新启动后旧计时器误删）
        const current = this.cleanupTimers.get(taskId);
        if (current && current.version === version) {
          const task = this.tasks.get(taskId);
          // 只有在任务仍为完成/失败/取消状态时才删除
          if (task && (task.status === 'completed' || task.status === 'failed' || task.status === 'cancelled')) {
            this.tasks.delete(taskId);
            this.notify();
          }
          this.cleanupTimers.delete(taskId);
        }
      }, 30000);
      
      this.cleanupTimers.set(taskId, { timerId, version });
    }
  }

  /** 取消任务追踪（页面不再需要追踪时调用，不影响后端任务） */
  stopTracking(taskId: string) {
    const cleanup = this.cleanupTimers.get(taskId);
    if (cleanup) {
      clearTimeout(cleanup.timerId);
      this.cleanupTimers.delete(taskId);
    }
    this.tasks.delete(taskId);
    this.notify();
  }

  /** 获取所有活跃任务（返回深拷贝，防止外部修改影响内部状态） */
  getActiveTasks(): ActiveTask[] {
    return Array.from(this.tasks.values())
      .filter((t) => t.status === 'running' || t.status === 'paused')
      .map((t) => ({ ...t }));
  }

  /** 获取指定项目的活跃任务 */
  getProjectTasks(projectId: number): ActiveTask[] {
    return this.getActiveTasks().filter((t) => t.projectId === projectId);
  }

  /** 获取指定项目+类型的任务 */
  getTask(projectId: number, taskType: TaskType): ActiveTask | undefined {
    return this.getActiveTasks().find(
      (t) => t.projectId === projectId && t.taskType === taskType
    );
  }

  /** 检查是否有指定项目的活跃任务 */
  hasActiveTask(projectId: number, taskType?: TaskType): boolean {
    if (taskType) {
      return this.getTask(projectId, taskType) !== undefined;
    }
    return this.getProjectTasks(projectId).length > 0;
  }

  /** 清空所有任务（登出或重置时调用） */
  clear() {
    // 清除所有清理计时器
    for (const [, cleanup] of this.cleanupTimers) {
      clearTimeout(cleanup.timerId);
    }
    this.cleanupTimers.clear();
    this.tasks.clear();
    this.notify();
  }
}

export const taskStore = TaskStoreImpl.getInstance();
