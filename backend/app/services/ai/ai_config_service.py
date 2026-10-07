"""AI 配置服务层 — 启动初始化 + 定时清理

职责：
  1. 启动时自动初始化 AI 配置缓存
  2. 后台定时清理 provider_history 中超过 30 天的旧记录
  3. 提供统一的 API 调用入口

使用方式：
  from app.services.ai.ai_config_service import startup, cleanup_old_history, get_service

  # 启动时自动初始化（无需手动调用）
  startup()

  # 手动清理历史记录
  cleanup_old_history(days=30)

  # 获取单例服务
  svc = get_service()
"""

import asyncio
from datetime import datetime, timedelta
import logging
import threading
from typing import Any

from .provider_factory import (
    get_ai_config,
    record_provider_usage,
)


logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────
# AIProviderService 单例
# ──────────────────────────────────────────────────────────────────────
_instance: Any | None = None
_instance_lock = threading.Lock()


def get_service() -> Any:
    """返回 AIProviderService 单例（线程安全）"""
    global _instance
    if _instance is not None:
        return _instance
    with _instance_lock:
        # 双重检查：并发首次调用时只构造一次，避免重复实例化
        if _instance is None:
            from .ai_provider_service import AIProviderService

            _instance = AIProviderService()
    return _instance


# ──────────────────────────────────────────────────────────────────────
# 全局状态
# ──────────────────────────────────────────────────────────────────────
_cleanup_task: asyncio.Task | None = None
_HISTORY_RETENTION_DAYS = 30  # 历史记录保留天数


# ──────────────────────────────────────────────────────────────────────
# 启动初始化
# ──────────────────────────────────────────────────────────────────────
def startup() -> None:
    """应用启动时调用：预热配置缓存，注册清理定时任务"""
    logger.info("正在初始化 AI 配置服务...")

    # 1. 预热配置缓存（首次调用 get_ai_config 加载到内存）
    try:
        cfg = get_ai_config()
        logger.info(
            "AI 配置已加载: provider=%s, model=%s",
            cfg.get("provider_name", "N/A"),
            cfg.get("model", "N/A"),
        )
    except Exception as e:
        logger.warning("AI 配置加载失败（不影响服务启动）: %s", e)

    # 2. 注册后台清理定时任务
    _schedule_cleanup()

    logger.info("AI 配置服务初始化完成")


def _schedule_cleanup() -> None:
    """注册 provider_history 清理定时任务（每 6 小时执行一次）

    BUG-FIX 2026-09-17：
    1. 重复调用（如 lifespan 重入、测试内多次 startup）会直接覆盖
       `_cleanup_task`，旧协程失去强引用成为孤儿任务并持续跑下去；
    2. 清理是同步 sqlite 调用，直接在事件循环里执行会阻塞所有请求。
    """
    global _cleanup_task

    if _cleanup_task is not None and not _cleanup_task.done():
        logger.debug("provider_history 清理任务已在运行，跳过重复注册")
        return

    async def _cleanup_loop():
        """每 6 小时清理一次超过保留天数的历史记录"""
        while True:
            await asyncio.sleep(6 * 3600)  # 6 小时
            try:
                # sqlite 是同步阻塞调用，放到线程里执行，避免卡住事件循环
                count = await asyncio.to_thread(
                    cleanup_old_history, _HISTORY_RETENTION_DAYS
                )
                if count > 0:
                    logger.info(
                        "已清理 %d 条超过 %d 天的 provider 历史记录",
                        count,
                        _HISTORY_RETENTION_DAYS,
                    )
                else:
                    logger.debug("provider_history 无需清理")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("provider_history 清理任务失败: %s", e)

    _cleanup_task = asyncio.create_task(_cleanup_loop())
    logger.info(
        "provider_history 清理定时任务已注册（每 6 小时执行，保留 %d 天）", _HISTORY_RETENTION_DAYS
    )


def shutdown() -> None:
    """应用关闭时调用：取消后台清理任务，避免事件循环关闭时抛出未回收任务警告"""
    global _cleanup_task
    if _cleanup_task is not None and not _cleanup_task.done():
        _cleanup_task.cancel()
    _cleanup_task = None


def cleanup_old_history(days: int = _HISTORY_RETENTION_DAYS) -> int:
    """清理超过指定天数的 provider_history 记录

    Returns:
        删除的记录数量
    """
    try:
        from ...db import get_db

        cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
        with get_db() as conn:
            # BUG-FIX 2026-09-17：`conn.total_changes` 是**该连接自打开以来的
            # 累计变更行数**（连接池复用下可达数万），并非本次 DELETE 的行数。
            # 返回值被用于日志与调用方判断，虚高会让人误以为清理了大量数据。
            # 必须用游标的 rowcount。
            cursor = conn.execute(
                "DELETE FROM provider_history WHERE last_used_at < ?",
                (cutoff,),
            )
            deleted = cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0
            conn.commit()
        logger.info(
            "已清理 %d 条超过 %d 天的 provider_history 记录 (cutoff=%s)", deleted, days, cutoff[:10]
        )
        return deleted
    except Exception as e:
        logger.warning("清理 provider_history 失败: %s", e)
        return 0


# ──────────────────────────────────────────────────────────────────────
# 对外暴露的便捷函数
# ──────────────────────────────────────────────────────────────────────
def record_usage(
    provider_name: str, provider_type: str, base_url: str, success: bool = True
) -> None:
    """记录 provider 使用情况（别名，兼容旧调用方式）"""
    record_provider_usage(provider_name, provider_type, base_url, success)


def get_priority_chain() -> list[dict]:
    """获取优先级链（主配置 → 历史记录 → 内置兜底）"""
    from .provider_factory import get_provider_priority_order

    return get_provider_priority_order()


def get_config() -> dict:
    """获取 AI 配置（别名）"""
    return get_ai_config()
