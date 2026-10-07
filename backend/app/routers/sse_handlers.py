"""sse_handlers 兼容层 — 从 sse/ 子包 re-export 所有名称

原始 6613 行单文件已按功能拆分到 sse/ 子包:
  sse_shared.py    基础设施 (logger / 队列 / 超时常量)
  sse_utils.py     工具函数 (provider 获取 / 数据提取 / leaf 字典)
  sse_db_ops.py    DB 操作 (章节保存 / context 提取 / outline 持久化)
  sse_charts.py    图表自动生成
  sse_outline.py   目录生成 SSE 主循环
  sse_content.py   正文生成 + 扩写 SSE 主循环

本文件仅做向后兼容 re-export，不应再向此文件添加新逻辑。
"""

from .sse import *  # noqa: F401,F403
from .sse import __all__  # noqa: F401
