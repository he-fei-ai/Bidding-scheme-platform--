"""增强功能编排器

统一管理所有新增增强功能的初始化和依赖关系。
在应用启动时自动执行数据库迁移和服务初始化。
"""

from datetime import datetime
import logging

from ..db import ensure_enhancement_tables, get_db
from .api_key_auth import ApiKeyService
from .generation_manager import generation_manager
from .template_expansion import TemplateExpansionService


logger = logging.getLogger(__name__)


class EnhancementOrchestrator:
    """增强功能编排器

    职责：
    1. 初始化所有增强服务
    2. 执行数据库迁移
    3. 验证依赖项
    4. 提供统一的健康检查接口
    """

    def __init__(self):
        self.template_service: TemplateExpansionService | None = None
        self.generation_mgr = generation_manager
        self.api_key_service = ApiKeyService
        self._initialized = False

    async def initialize(self):
        """初始化所有增强功能"""
        if self._initialized:
            logger.info("增强功能已初始化，跳过")
            return

        logger.info("开始初始化增强功能...")

        try:
            # 1. 执行数据库迁移
            logger.info("步骤 1/4: 执行数据库迁移...")
            ensure_enhancement_tables()
            logger.info("[OK] 数据库迁移完成")

            # 2. 初始化模板扩写服务
            logger.info("步骤 2/4: 初始化模板扩写服务...")
            self.template_service = TemplateExpansionService()
            logger.info("[OK] 模板扩写服务就绪")

            # 3. 清理过期数据
            logger.info("步骤 3/4: 清理过期数据...")
            await self.generation_mgr.cleanup_completed_tasks(max_hours=24)
            logger.info("[OK] 数据清理完成")

            # 4. 验证API Key服务
            logger.info("步骤 4/4: 验证API Key服务...")
            # 测试数据库连接
            with get_db() as conn:
                conn.execute("SELECT 1")
            logger.info("[OK] API Key服务就绪")

            self._initialized = True
            logger.info("[OK] 所有增强功能初始化完成")

        except Exception as e:
            logger.error("增强功能初始化失败: %s", e, exc_info=True)
            raise

    async def health_check(self) -> dict:
        """健康检查

        Returns:
            健康状态字典
        """
        status = {
            "status": "healthy",
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "services": {},
        }

        # 检查数据库连接
        try:
            with get_db() as conn:
                conn.execute("SELECT 1")
            status["services"]["database"] = "ok"
        except Exception as e:
            status["services"]["database"] = f"error: {e!s}"
            status["status"] = "degraded"

        # 检查模板服务
        if self.template_service:
            status["services"]["template_expansion"] = "ok"
        else:
            status["services"]["template_expansion"] = "not_initialized"
            status["status"] = "degraded"

        # 检查生成管理器
        status["services"]["generation_manager"] = (
            "ok" if self.generation_mgr else "not_initialized"
        )

        # 检查API Key服务
        status["services"]["api_key_auth"] = "ok" if self.api_key_service else "not_initialized"

        return status

    def get_template_service(self) -> TemplateExpansionService:
        """获取模板扩写服务实例"""
        if not self.template_service:
            raise RuntimeError("模板扩写服务未初始化")
        return self.template_service


# 全局实例
enhancement_orchestrator = EnhancementOrchestrator()


async def initialize_all():
    """应用启动时调用"""
    await enhancement_orchestrator.initialize()


def check_health():
    """健康检查端点"""
    from .ai.workflows import _safe_asyncio_run

    logger.info("[check_health] 开始执行异步健康检查")
    try:
        result = _safe_asyncio_run(enhancement_orchestrator.health_check())
        logger.info("[check_health] 健康检查完成，结果=%s", result)
        return result
    except Exception as e:
        logger.error("[check_health] 健康检查失败：%s", e, exc_info=True)
        raise
