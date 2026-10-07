# useSSEGeneration Hook - 设计规范文档

## 概述

当前前端 SSE 生成逻辑（目录生成、正文生成）直接内联在各页面组件中，存在代码复用困难、状态管理分散、错误处理不一致等问题。此设计文档规范提取为一个可复用的 React Hook `useSSEGeneration`，统一封装后端 SSE 连接管理、进度追踪、错误处理和取消功能。

---

## API 设计

### Hook 签名

```ts
const {
  isGenerating,
  progress,
  status,
  data,
  error,
  start,
  cancel,
  abort
} = useSSEGeneration({
  type: 'outline' | 'content',
  projectId: number,
  // 其他参数按需传递
  params?: Record<string, any>
});
```

### 返回值说明

| 属性 | 类型 | 描述 |
|------|------|------|
| `isGenerating` | boolean | 是否正在生成中 |
| `progress` | number \| undefined | 生成进度百分比 (0-100)，仅在流式推进时更新 |
| `status` | 'idle' \| 'running' \| 'paused' \| 'completed' \| 'cancelled' \| 'error' | 当前生成状态 |
| `data` | object \| null | 最终生成的数据（目录结构/正文内容），生成完成后填充 |
| `error` | string \| null | 错误信息，发生失败时设置 |
| `start` | (params?: Object) => void | 启动生成任务的函数 |
| `cancel` | () => void | 发起取消请求（调用后端 stop_generation） |
| `abort` | () => void | 断开 SSE 连接并清理本地状态（硬中止） |

---

## 使用场景示例

### 1. 生成目录（Outline Generation）

```tsx
const { isGenerating, progress, status, data, start, cancel, abort } = useSSEGeneration({
  type: 'outline',
  projectId: 123,
});

return (
  <div>
    {isGenerating && <Progress percent={progress} />}
    <button onClick={() => start({ strategy: 'v3' })} disabled={isGenerating}>
      {isGenerating ? '生成中...' : '生成目录'}
    </button>
    {status === 'cancelled' && <p>已取消</p>}
    {status === 'error' && <p>错误: {error}</p>}
    {data && <OutlineTree tree={data} />}
  </div>
);
```

### 2. 生成正文（Content Generation）

```tsx
const { isGenerating, status, data, start, cancel, abort } = useSSEGeneration({
  type: 'content',
  projectId: 123,
  params: { writingLength: 'detailed', level2Default: 5000 },
});
```

### 3. 手动控制取消

```tsx
// 可在侧边栏或全局操作中提供取消按钮
<button onClick={cancel} disabled={!isGenerating || status !== 'running'}>
  停止生成
</button>
```

---

## 内部实现逻辑

### 1. SSE 连接管理

- 使用 `EventSource` API 建立与服务器的 `/projects/{projectId}/sections/generate-sse` 或 `/generate-content` 连接。
- 连接建立时设置 `onopen`、`onmessage`、`onerror`、`onclose` 回调。
- 每次启动新任务若前一个连接仍存在，先调用 `abort()` 关闭旧连接。

### 2. 消息解析

后端 SSE 事件格式（已统一）：
```
event: update
data: {"status":"progress","percent":45,"message":"正在处理第3章节"}

event: chunk
data: {"sectionId":2,"partialText":"..."}

event: done
data: {"status":"completed","finalData":{...}}

event: error
data: {"status":"error","message":"AI配置异常，请检查设置"}
```

Hook 根据 event type 更新对应状态：
- `update` → 更新 `progress` 和 `status`（可能为 running/paused）
- `chunk` → 逐步累加到 `data`（视具体业务可能需要合并）
- `done` → 设置 `status='completed'`、`isGenerating=false`、填充 `data`
- `error` → 设置 `status='error'`、`isGenerating=false`、填充 `error`

### 3. 取消流程

- 用户点击 `cancel` → Hook 发送 POST 请求到 `/projects/{projectId}/sections/stop-generation`。
- 同时设置本地标志 `shouldCancelRequested = true`，并在收到 SSE 的后续 `update` 事件（含 cancelled 状态）时清理连接。
- 若用户点击 `abort`（例如网络中断），则直接关闭 EventSource 并清除定时器，不等待后端响应。

### 4. 重连策略（可选）

对于不稳定连接，可在 `onerror` 时指数退避重试最多 3 次，但仅适用于非致命错误（如临时网络波动），遇到明确 error 事件后不再重连。

---

## 与现有代码的整合步骤

1. **创建 Hook 文件**  
   `frontend/src/hooks/useSSEGeneration.tsx` （基于 React + TypeScript）

2. **替换原有内联代码**  
   在所有调用 `/generate-sse` 和 `/generate-content` 的页面组件中：
   - 删除原有的 EventSource 手动编写代码
   - 导入并使用 `useSSEGeneration` hook
   - 将原组件中的状态变量（如 `isGenerating`, `progress`, `error`）替换为 hook 返回值

3. **统一后端返回格式**（如尚未统一）  
   确保后端 SSE 事件结构一致，包含 `event` 字段和标准的数据格式。参考之前的错误消息 sanitization 修复，确保不泄露敏感信息。

4. **测试**  
   编写单元测试模拟 SSE 事件流，验证状态转换是否正确；集成测试验证真实连接和数据渲染。

---

## 注意事项

- **跨浏览器兼容性**：`EventSource` 在现代浏览器中广泛支持，但 IE 不支持。如需兼容 IE，可使用 polyfill（如 `eventsource-polyfill`）或轮询替代方案。
- **多实例安全**：同一项目同时多个 hook 实例应共享同一个取消状态，避免冲突。可在 Hook 内部使用 Map 缓存按 projectId 连接的引用。
- **内存泄漏防护**：组件卸载时应调用 `abort()` 关闭连接，防止 EventSource 持续占用资源。
- **性能优化**：对于大文件生成，`onmessage` 可能频繁触发，建议对 `progress` 更新进行防抖（debounce），避免不必要的重渲染。

---

## 依赖需求

- React >= 18（推荐使用 useState, useEffect, useCallback, useRef）
- TypeScript（强类型有助于减少错误）
- Axios 或 fetch 用于发送取消请求（hook 内部可自行封装）

---

## 收益总结

- ✅ 代码复用性大幅提升，消除多处重复的 SSE 连接逻辑
- ✅ 状态统一管理，便于调试和扩展（如增加暂停/续传功能）
- ✅ 错误处理标准化，用户提示一致
- ✅ 取消逻辑集中，避免残留任务
- ✅ 更易添加新功能（如进度持久化、离线队列等）
