# sections.py 模块化拆分手动指南

## 目标
将庞大的 `sections.py` 拆分为三个职责清晰的模块：
1. `sse_handlers.py` — SSE 事件生成逻辑（generate_outline_sse, generate_content, expand_content）
2. `task_control.py` — 任务取消与状态管理（全局变量 + stop_generation）
3. `sections.py` — 精简的路由入口（只保留路由装饰器和对 handler 的调用）

## 准备工作

1. **备份当前文件**（已自动完成）：
   - `sections.py.backup_...` 位于同一目录
   
2. **打开终端**并确保 Python 环境可用

## 步骤 1: 完善 sse_handlers.py

之前脚本已生成该文件，但存在缺陷。请手动修正：

### a) 修复导入语句（确保每行独立）
错误示例：
```python
import asyncioimport json...
```
应改为：
```python
import asyncio
import json
import logging
import time
from typing import Any, AsyncGenerator, Dict, Optional
from fastapi import HTTPException, Query
from starlette.responses import StreamingResponse
```

### b) 补全函数体
目前 `generate_outline_sse` 只有签名没有实现。**正确做法：**

1. 打开原始的 `sections.py`（未修改前）或从备份恢复
2. 找到完整的 `async def generate_outline_sse(...):` 函数（包含从 `@router.get` 到 `return StreamingResponse(...)` 的全部内容）
3. **复制整个函数体（不包括 @router.get 装饰器）**
4. 粘贴到 `sse_handlers.py` 中，并将函数名改为 `generate_outline_sse_impl`
5. 同样处理 `generate_content_impl` 和 `expand_content_impl`

### c) 添加必要的导入
确保每个 impl 函数内部需要的 import 都在 sse_handlers.py 顶部已有（例如 `get_ai_config`, `create_provider`, `CacheService` 等）。如果缺少，添加相对导入：
```python
from ..services.ai.provider_factory import get_ai_config, create_provider
from ..services.redis_cache import CacheService
```

## 步骤 2: 完善 task_control.py

### a) 声明全局变量
在文件顶部添加：
```python
import asyncio
import logging
from typing import Dict, List

_cancel_events: Dict[int, List[asyncio.Event]] = {}
_cancel_tasks: Dict[int, List[asyncio.Task]] = {}
_cancel_events_lock = asyncio.Lock()
```

### b) 提取 stop_generation 函数
1. 从原始 sections.py 中找到 `async def stop_generation(...):` 及其全部内容
2. 复制到 `task_control.py` 中
3. 确保其内部使用的导入（如 `_cancel_events_lock`、`logger` 等）可用或添加

### c) （可选）添加辅助函数
根据需要可添加：
```python
def get_or_create_cancel_event(project_id: int) -> asyncio.Event:
    """获取或创建项目的取消事件"""
    async with _cancel_events_lock:
        if project_id not in _cancel_events:
            _cancel_events[project_id] = []
        event = asyncio.Event()
        _cancel_events[project_id].append(event)
        return event

def cleanup_project_tasks(project_id: int) -> None:
    """清理项目相关记录"""
    async with _cancel_events_lock:
        _cancel_events.pop(project_id, None)
        _cancel_tasks.pop(project_id, None)
```

## 步骤 3: 更新 sections.py（精简版）

### a) 删除原函数
打开当前的 `sections.py`（可能是尚未完全拆分的版本），删除以下函数的完整定义：
- `generate_outline_sse`
- `generate_content`  
- `expand_content`
- `stop_generation`

⚠️ **注意**：删除时要小心，确保只删除完整函数，不要误删其他内容。建议使用编辑器"折叠函数"功能确认范围。

### b) 添加新的简化 wrapper 函数
在删除的位置（通常在文件中部，路由装饰器附近），插入以下代理代码：

#### generate_outline_sse 代理
```python
@router.get("/generate-sse", dependencies=[Depends(limit_per_endpoint)])
async def generate_outline_sse(
    request: Request,
    project_id: int,
    strategy: str = Query(default="v3", pattern="^(step_by_step|one_shot|v2|v3)$"),
):
    """SSE 流式生成目录（EventSource 只支持 GET）"""
    from .sse_handlers import generate_outline_sse_impl
    return StreamingResponse(
        generate_outline_sse_impl(project_id, strategy),
        media_type="text/event-stream"
    )
```

#### generate_content 代理
```python
@router.get("/generate-content", dependencies=[Depends(limit_per_endpoint)])
async def generate_content(
    request: Request,
    project_id:
    writing_length: str = Query("standard"),
    # ... 其余参数与原签名相同
):
    """SSE 并发生成正文（EventSource 只支持 GET）"""
    from .sse_handlers import generate_content_impl
    return StreamingResponse(
        generate_content_impl(project_id, ...),  # 传递所有必要参数
        media_type="text/event-stream"
    )
```

*注：需仔细复制所有参数名及默认值，保持与原函数一致。*

#### expand_content 代理
```python
@router.post("/expand-content", dependencies=[Depends(limit_per_endpoint)])
async def expand_content(
    request: Request,
    project_id: int,
    writing_length: str = Query("standard"),
    level2_default: int = Query(3500, ge=1000, le=10000),
):
    """兜底扩写：对字数最少的章节进行扩写，确保内容充实"""
    from .sse_handlers import expand_content_impl
    return StreamingResponse(
        expand_content_impl(project_id, writing_length, level2_default),
        media_type="text/event-stream"
    )
```

#### stop_generation 代理（如需保留原端点）
```python
@router.post("/stop-generation")
async def stop_generation(project_id: int):
    """停止指定项目的生成任务"""
    from .task_control import stop_generation as task_stop
    # 或直接执行取消逻辑（视具体实现而定）
    task_stop(project_id)
    return {"status": "cancelled"}
```

### c) 添加顶部导入
确保在 sections.py 的 imports 部分加入：
```python
from .sse_handlers import generate_outline_sse_impl, generate_content_impl, expand_content_impl
from .task_control import stop_generation, _cancel_events, _cancel_tasks, _cancel_events_lock
```

## 步骤 4: 语法验证

保存所有文件后，在每个目录下运行编译检查：

```bash
cd backend
python -m py_compile app/routers/sections.py
python -m py_compile app/routers/sse_handlers.py
python -m py_compile app/routers/task_control.py
```

如无报错则成功。

## 步骤 5: 功能测试

启动开发服务器并访问以下端点验证是否正常：
- GET `/projects/{id}/sections/generate-sse?strategy=v3` → 应返回 SSE 流
- GET `/projects/{id}/sections/generate-content` → 同上
- POST `/projects/{id}/sections/stop-generation` → 应触发取消逻辑
- POST `/projects/{id}/sections/expand-content` → 应执行扩写

同时检查日志中是否有错误或异常输出。

## ⚠️ 注意事项

1. **循环依赖**：确保 sse_handlers/task_control 不反向导入 sections.py。
2. **导入路径**：所有相对导入应基于 `app/routers/` 作为包根（即前面加点数）。
3. **全局状态**：_cancel_events 等全局变量在多进程场景下不适用（如 Gunicorn 多 worker），生产环境应考虑使用 Redis 共享状态。
4. **调试方便**：拆分初期可先让原 sections.py 导入新模块的 _impl 但不删除原函数，对比行为一致后再移除。

## 🛠 推荐工具

- **PyCharm**: 使用 Refactor → Extract Method / Move Class，智能处理导入和引用
- **VS Code + Python 扩展**: 右键函数 → Extract to new file
- **IDEA**: 强大的重构支持

这些工具能自动处理大部分繁琐细节，避免手动编辑失误。完成后请用上述编译检查验证。

--- 

✅ 完成此指南后，你将得到一个结构清晰、易于维护的代码base，并为后续单元测试和集成测试奠定基础。