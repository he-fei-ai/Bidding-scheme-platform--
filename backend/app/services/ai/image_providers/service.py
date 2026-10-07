"""图像生成统一服务层

提供统一的图像生成接口，集成多个平台适配器，支持：
- 风格约束自动注入
- 并发控制（semaphore）
- 成本追踪与用量统计
- 失败降级/切换
- 任务记录
"""

import asyncio
from datetime import datetime
import logging
import threading
from typing import Any

from ....utils.proxy_config import aiohttp_session_kwargs
from ..image_styles import get_image_style
from .base import ImageProviderAdapter
from .registry import ProviderRegistry


logger = logging.getLogger(__name__)

# 保护 _semaphores 字典的跨线程锁（_get_semaphore 是同步方法，用 threading.Lock 而不是 asyncio.Lock）
_semaphore_guard = threading.Lock()

# 图像生成平台兜底列表（按优先级排列，最后一个是 Agnes 内置兜底）
IMAGE_FALLBACK_PROVIDERS: list[str] = [
    "baidu",  # 百度文心一格
    "volcengine",  # 字节火山引擎
    "dashscope",  # 阿里云通义
    "hunyuan",  # 腾讯混元
    "stepfun",  # 阶跃星辰
    "zhipu",  # 智谱AI
    "agnes",  # Agnes 内置兜底
]


def load_image_config(user_id: int = 1) -> dict[str, Any]:
    """从 model_configs 表加载图像模型配置（供路由/引擎直接复用）

    返回的 key 全部带 image_ 前缀，与 _get_adapter() 读取的字段保持一致，
    否则 api_key / base_url / model 等配置会被静默丢弃。
    无配置时返回启用状态为 True 的 Agnes 默认配置（兼容自由生图场景）。
    """
    try:
        from ....db import get_db_with_retry

        row = None
        # PERF（2026-10-05）：纯读走 readonly，不抢全局写锁（此前无条件抢锁，
        # 与 generate_image 长事务叠加会放大写锁争用）
        with get_db_with_retry(readonly=True) as conn:
            row = conn.execute(
                """
                SELECT image_provider, image_model, image_base_url, image_api_key_enc,
                       image_max_concurrency, image_max_per_project, image_quality,
                       image_style_injection, image_enabled, image_default_size, image_default_ratio, image_price_per_image
                FROM model_configs WHERE user_id = ?
                """,
                [user_id],
            ).fetchone()
    except Exception as e:
        logger.error("Failed to load image config for user {user_id}: %s", user_id)
        row = None

    if not row:
        # 返回默认 Agnes 配置（enabled=True，密钥为空由引擎处理）
        return {
            "image_provider": "agnes",
            "image_model": "agnes-image-2.1-flash",
            "image_base_url": "https://api.agnes-ai.cn/v1",
            "image_api_key_enc": "",
            "image_max_concurrency": 2,
            "image_max_per_project": 10,
            "image_quality": "standard",
            "image_style_injection": None,
            "image_enabled": True,
            "image_default_size": "2K",
            "image_default_ratio": "1:1",
            "image_price_per_image": 0.0,
        }

    return {
        "image_provider": row[0] or "agnes",
        "image_model": row[1] or "agnes-image-2.1-flash",
        "image_base_url": row[2] or "https://api.agnes-ai.cn/v1",
        "image_api_key_enc": row[3],
        "image_max_concurrency": row[4] or 2,
        "image_max_per_project": row[5] or 10,
        "image_quality": row[6] or "standard",
        "image_style_injection": row[7],
        "image_enabled": bool(row[8]) if row[8] is not None else True,
        "image_default_size": row[9] or "2K",
        "image_default_ratio": row[10] or "1:1",
        "image_price_per_image": row[11] or 0.0,
    }


class CostTracker:
    """用量统计与成本追踪器"""

    def __init__(self):
        self.usage: dict[str, dict[str, Any]] = {}  # {provider: {project_id: count}}
        self.costs: dict[str, float] = {}  # {provider: total_cost}
        self.monthly_budget: float = 50.0  # 默认月度预算 50 元

    def record_usage(self, provider: str, project_id: str, cost: float) -> None:
        """记录一次生成用量和成本"""
        if provider not in self.usage:
            self.usage[provider] = {}
        if project_id not in self.usage[provider]:
            self.usage[provider][project_id] = 0
        self.usage[provider][project_id] += 1

        if provider not in self.costs:
            self.costs[provider] = 0.0
        self.costs[provider] += cost

        # 检查成本预警
        total_cost = sum(self.costs.values())
        if total_cost > self.monthly_budget:
            logger.warning(
                "月度成本预警：总成本 ¥%s exceeds budget ¥%s", total_cost, self.monthly_budget
            )

    def get_usage_by_provider(self, provider: str) -> int:
        """获取某平台的总使用量"""
        return sum(self.usage.get(provider, {}).values())

    def get_total_cost(self) -> float:
        """获取总成本"""
        return sum(self.costs.values())


class ImageGenerationService:
    """统一图像生成服务

    作为所有图像生成请求的入口，负责：
    1. 获取用户配置
    2. 选择平台（按优先级：用户配置 > 默认 Agnes）
    3. 注入风格约束
    4. 调用平台 API
    5. 记录用量与成本
    6. 返回结果
    """

    def __init__(self, db_conn=None, config_service=None):
        # PERF/结构性修复（2026-10-05）：db_conn 改为**可选**。
        # 旧用法由路由传一个 `with get_db()` 长连接，导致外部 HTTP 生成
        # （秒~分钟级）全程持有全局写锁 → 冻结所有写请求。新默认（db_conn=None）
        # 由服务内部每个写点自管短事务；仅单测可注入 FakeDB 走直接写路径。
        self.db = db_conn
        self.config_service = config_service
        self._semaphores: dict[str, asyncio.Semaphore] = {}
        self._provider_cache: dict[str, ImageProviderAdapter] = {}
        self.cost_tracker = CostTracker()
        self._lock = asyncio.Lock()  # 保护共享状态

    async def _execute_write(self, sql: str, params, *, commit_on_injected: bool = True) -> None:
        """统一的图像任务写入入口。

        - 生产路径（self.db is None）：在 worker 线程内开一个**短事务**，
          写锁获取/释放完整发生在 to_thread 内部，不跨 await、不阻塞事件循环。
          执行完立即退出 with（最外层自动 commit）。
        - 注入路径（单测 FakeDB）：直接同步写，按 commit_on_injected 控制是否 commit
          （保持旧行为：_record_task 不 commit，其余 commit）。
        """
        if self.db is not None:
            self.db.execute(sql, params)
            if commit_on_injected:
                self.db.commit()
            return

        def _do():
            from ....db import get_db
            with get_db() as conn:
                conn.execute(sql, params)

        await asyncio.to_thread(_do)

    async def generate(
        self,
        request: dict[str, Any],
        user_id: str,
    ) -> dict[str, Any]:
        """
        统一图像生成入口

        Args:
            request: 包含 prompt, model, size, ratio, style, reference_images, project_id, chapter_id, provider 等
            user_id: 用户标识

        Returns:
            {success: bool, imageUrl?: string, localPath?: string, cost?: number, provider: string, model: string, taskId: string, error?: string}
        """
        try:
            # 1. 获取用户图像模型配置
            config = await self._get_user_image_config(user_id)
            if not config or not config.get("image_enabled", True):
                return self._error_response(
                    "图像生成未启用，请检查配置", provider=None, task_id=self._create_task_id()
                )

            # 2. 注入风格约束
            final_prompt = self._inject_style(
                request.get("prompt", ""),
                request.get("style", "engineering_diagram"),
                config.get("image_style_injection"),
            )

            # 3. 确定要使用的平台
            provider = request.get("provider") or config.get("image_provider", "agnes")

            # 4. 获取适配器实例
            adapter = await self._get_adapter(provider, config)

            # 5. 并发控制
            semaphore = self._get_semaphore(provider, config.get("image_max_concurrency", 2))
            async with semaphore:
                # 6. 调用生成 API
                result = await self._call_generate(adapter, final_prompt, request, config)

            # 7. 记录任务（先记录再处理结果，确保任务ID可追踪）
            task_id = self._create_task_id()
            await self._record_task(
                task_id=task_id,
                project_id=request.get("project_id"),
                chapter_id=request.get("chapterId"),
                provider=provider,
                model=request.get("model") or config.get("image_model", "agnes-image-2.1-flash"),
                original_prompt=request.get("prompt", ""),
                injected_prompt=final_prompt,
                size=request.get("size") or config.get("image_default_size", "2K"),
                ratio=request.get("ratio") or config.get("image_default_ratio", "1:1"),
                result=result,
            )

            # 8. 更新成本追踪（仅在成功时记录）
            if result.get("success"):
                self.cost_tracker.record_usage(
                    provider=provider,
                    project_id=request.get("project_id") or "global",
                    cost=result.get("cost", 0.0),
                )

            # 9. 返回结果（成功时直接返回；失败时走降级链）
            if result.get("success"):
                return await self._build_success_response(result, provider, task_id, request)

            # 失败时按优先级链降级
            if provider != "agnes":
                fallback_chain = IMAGE_FALLBACK_PROVIDERS
                if provider in fallback_chain:
                    idx = fallback_chain.index(provider)
                    fallbacks = fallback_chain[idx + 1 :]
                else:
                    fallbacks = IMAGE_FALLBACK_PROVIDERS
                for fb_provider in fallbacks:
                    logger.warning("平台 %s 生成失败，尝试降级到 %s", provider, fb_provider)
                    fb_result = await self._try_fallback_provider(
                        fb_provider, config, final_prompt, request, user_id
                    )
                    if fb_result.get("success"):
                        # 降级成功：更新任务记录为最终成功 + 返回成功响应
                        fb_result.setdefault("cost", 0.0)
                        await self._update_task_success(task_id, fb_provider, fb_result)
                        return await self._build_success_response(
                            fb_result, fb_provider, task_id, request
                        )

            return self._format_error_response(result, task_id)

        except Exception as e:
            task_id = self._create_task_id()
            logger.error("Image generation failed: %s", e)
            await self._record_task(
                task_id=task_id,
                project_id=request.get("project_id"),
                chapter_id=request.get("chapterId"),
                provider="unknown",
                model="unknown",
                original_prompt=request.get("prompt", ""),
                injected_prompt="",
                result={"success": False, "error": str(e)},
            )
            return self._error_response(f"图像生成发生内部错误: {e!s}", task_id=task_id)

    async def _build_success_response(
        self,
        result: dict[str, Any],
        provider: str,
        task_id: str,
        request: dict[str, Any],
    ) -> dict[str, Any]:
        """构造成功响应（含本地图片下载）"""
        final_provider = result.get("provider", provider)
        local_path = None
        if result.get("image_url"):
            local_path = await self._download_image(
                result["image_url"], request.get("project_id"), task_id
            )
        return {
            "success": True,
            "imageUrl": result.get("image_url"),
            "imageBase64": result.get("image_base64"),
            "localPath": local_path,
            "cost": result.get("cost", 0.0),
            "provider": final_provider,
            "model": result.get("model", ""),
            "taskId": task_id,
            "error": None,
        }

    async def _update_task_success(
        self,
        task_id: str,
        provider: str,
        result: dict[str, Any],
    ) -> None:
        """降级成功后，将任务记录更新为最终成功状态"""
        try:
            await self._execute_write(
                """
                UPDATE image_generation_tasks SET
                    status = 'success', provider = ?, model = ?,
                    image_url = ?, cost = ?, completed_at = ?
                WHERE id = ?
                """,
                (
                    provider,
                    result.get("model", ""),
                    result.get("image_url"),
                    result.get("cost", 0.0),
                    datetime.now().isoformat(),
                    task_id,
                ),
            )
        except Exception as e:
            logger.debug("更新降级任务状态失败: %s", e)

    async def _call_generate(
        self,
        adapter: ImageProviderAdapter,
        prompt: str,
        request: dict[str, Any],
        config: dict[str, Any],
    ) -> dict[str, Any]:
        """调用适配器的生成方法"""
        model = request.get("model") or config.get("image_model", adapter.default_model)
        size = request.get("size") or config.get("image_default_size", "2K")
        ratio = request.get("ratio") or config.get("image_default_ratio", "1:1")
        ref_images = request.get("reference_images", [])

        return await adapter.generate(
            prompt=prompt,
            model=model,
            size=size,
            ratio=ratio,
            reference_images=ref_images,
        )

    async def _try_fallback_provider(
        self,
        provider: str,
        config: dict[str, Any],
        prompt: str,
        request: dict[str, Any],
        user_id: str,
    ) -> dict[str, Any]:
        """尝试使用指定提供商生成图像"""
        try:
            fb_config = await self._get_user_image_config(user_id)
            # 如果 fallback 提供商有独立配置，使用 fallback 配置
            if fb_config.get("image_provider") == provider and fb_config.get("image_api_key_enc"):
                adapter = await self._get_adapter(provider, fb_config)
            else:
                adapter = await self._get_adapter(provider, config)
            semaphore = self._get_semaphore(provider, fb_config.get("image_max_concurrency", 2))
            async with semaphore:
                result = await adapter.generate(
                    prompt=prompt,
                    model=request.get("model")
                    or fb_config.get("image_model", adapter.default_model),
                    size=request.get("size") or config.get("image_default_size", "2K"),
                    ratio=request.get("ratio") or config.get("image_default_ratio", "1:1"),
                    reference_images=request.get("reference_images", []),
                )
            result["provider"] = provider
            return result
        except Exception as e:
            logger.debug("降级提供商 %s 失败: %s", provider, type(e).__name__)
            return {"success": False, "error": str(e), "provider": provider}

    def _inject_style(self, prompt: str, style: str, custom_style: str | None) -> str:
        """注入风格约束

        ENHANCE 2026-10-05（P-16）：风格文案收敛到 `services/ai/image_styles.py`
        作为唯一正本（此前本处与 image_engine.py 两处逐字复制了同一套文案，
        改一处不同步另一处）。支持 IMAGE_STYLE_OVERRIDE 环境变量按风格覆盖。
        """
        if custom_style:
            return f"{custom_style}\n{prompt}"

        style_prefix = get_image_style(style)
        return f"{style_prefix}\n{prompt}"

    async def _get_adapter(self, provider_id: str, config: dict) -> ImageProviderAdapter:
        """获取或创建适配器实例"""
        # 从缓存中查找
        if provider_id in self._provider_cache:
            return self._provider_cache[provider_id]

        # 注册中心获取适配器类
        adapter_class = ProviderRegistry._adapters.get(provider_id)
        if not adapter_class:
            available = list(ProviderRegistry._adapters.keys())
            raise ValueError(f"Unknown provider: {provider_id}. Available: {available}")

        # 创建配置（从数据库加密密钥解密）
        api_key = (
            self._decrypt_api_key(config.get("image_api_key_enc", ""))
            if config.get("image_api_key_enc")
            else ""
        )
        adapter_config = {
            "api_key": api_key,
            "base_url": config.get("image_base_url", "https://api.agnes-ai.cn/v1"),
            "model": config.get("image_model", "agnes-image-2.1-flash"),
            "default_size": config.get("image_default_size", "2K"),
            "default_ratio": config.get("image_default_ratio", "1:1"),
            "timeout": config.get("image_timeout", 60),
            "price_per_image": config.get("image_price_per_image", 0.0),
        }

        adapter = adapter_class(adapter_config)
        self._provider_cache[provider_id] = adapter
        return adapter

    def _get_semaphore(self, provider_id: str, max_concurrency: int) -> asyncio.Semaphore:
        """获取或创建指定平台的信号量"""
        if provider_id not in self._semaphores:
            with _semaphore_guard:
                if provider_id not in self._semaphores:
                    self._semaphores[provider_id] = asyncio.Semaphore(max_concurrency)
        return self._semaphores[provider_id]

    async def _get_user_image_config(self, user_id: str) -> dict[str, Any]:
        """获取用户图像模型配置（从数据库）"""
        try:
            return load_image_config(int(user_id or 1) if str(user_id).isdigit() else 1)
        except Exception as e:
            logger.error("Failed to get image config for user {user_id}: %s", user_id)
            # 返回默认配置但标记为未启用（因为缺少密钥）
            return {"image_enabled": False}

    def _decrypt_api_key(self, encrypted_key: str) -> str:
        """解密加密的 API Key（使用现有 crypto 模块）"""
        try:
            from ...crypto import decrypt_api_key

            if encrypted_key and self._is_encrypted(encrypted_key):
                return decrypt_api_key(encrypted_key)
            return encrypted_key or ""
        except Exception as e:
            logger.debug("API Key decryption failed: %s", e)
            return ""

    def _is_encrypted(self, key: str) -> bool:
        """检查密钥是否已加密（以 ENC: 开头）"""
        return isinstance(key, str) and key.startswith("ENC:")

    async def _record_task(
        self,
        task_id: str,
        project_id: str | None,
        chapter_id: str | None,
        provider: str,
        model: str,
        original_prompt: str,
        injected_prompt: str,
        result: dict[str, Any],
        size: str | None = None,
        ratio: str | None = None,
    ) -> None:
        """记录图像生成任务到数据库"""
        status = "success" if result.get("success") else "failed"
        completed_at = datetime.now().isoformat() if status == "success" else None

        try:
            # _record_task 旧行为不显式 commit（依赖外层会话）；注入路径保持
            # commit_on_injected=False，自管短事务则由 get_db 退出时提交。
            await self._execute_write(
                """
                INSERT OR REPLACE INTO image_generation_tasks (
                    id, project_id, chapter_id, provider, model, prompt,
                    injected_prompt, size, ratio, status, image_url, image_path,
                    cost, retry_count, error_message, created_at, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    task_id,
                    project_id,
                    chapter_id,
                    provider,
                    model,
                    original_prompt,
                    injected_prompt,
                    size or "2K",  # 本次生成实际尺寸
                    ratio or "1:1",  # 本次生成实际比例
                    status,
                    result.get("image_url"),
                    None,  # 本地路径 - 由下载任务设置
                    result.get("cost", 0.0),
                    result.get("retry_count", 0),
                    result.get("error"),
                    datetime.now().isoformat(),
                    completed_at,
                ),
                commit_on_injected=False,
            )
        except Exception as e:
            logger.error("Failed to record image generation task: %s", e)

    async def _download_image(self, url: str, project_id: str | None, task_id: str) -> str | None:
        """下载图片到本地存储（data/images/<project_id>/<task_id>.<ext>）

        远程图片 URL 通常有时效，落盘保存一份，供导出/二次使用。
        """
        from ....config import DATA_DIR

        # BUG-FIX 2026-09-15（高，SSRF）：拒绝拉取内网/保留地址
        from app.utils.ssrf import is_safe_http_url
        if not is_safe_http_url(url):
            logger.warning("拒绝下载非安全图片 URL（疑似内网地址，存在 SSRF 风险）: %s", url)
            return None

        try:
            import aiohttp

            image_dir = DATA_DIR / "images" / (str(project_id) if project_id else "global")
            image_dir.mkdir(parents=True, exist_ok=True)

            # 根据 URL 推断扩展名（默认 png）
            path_part = url.split("?")[0].lower()
            ext = ".png"
            if path_part.endswith(".jpg") or path_part.endswith(".jpeg"):
                ext = ".jpg"
            elif path_part.endswith(".webp"):
                ext = ".webp"
            elif path_part.endswith(".gif"):
                ext = ".gif"
            dest = image_dir / f"{task_id}{ext}"

            async with aiohttp.ClientSession(**aiohttp_session_kwargs()) as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=60)) as resp:
                    if resp.status != 200:
                        logger.debug("下载图片失败: HTTP %s", resp.status)
                        return None
                    data = await resp.content.read()
                    if not data:
                        logger.debug("下载图片失败: 空内容")
                        return None
                    dest.write_bytes(data)
                    logger.info("图片已保存到本地: %s (%d bytes)", dest, len(data))

            # 回写 image_path 到任务记录
            try:
                await self._execute_write(
                    "UPDATE image_generation_tasks SET image_path = ? WHERE id = ?",
                    (str(dest), task_id),
                )
            except Exception as e:
                logger.debug("更新 image_path 失败: %s", e)
            return str(dest)
        except Exception as e:
            logger.debug("Failed to download image: %s", e)
        return None

    def _create_task_id(self) -> str:
        """创建唯一任务 ID"""
        import uuid

        return f"img_{uuid.uuid4().hex[:8]}"

    def _error_response(self, error_msg: str, task_id: str | None = None) -> dict[str, Any]:
        """生成错误响应"""
        if task_id is None:
            task_id = self._create_task_id()
        return {
            "success": False,
            "imageUrl": None,
            "imageBase64": None,
            "localPath": None,
            "cost": 0.0,
            "provider": None,
            "model": None,
            "taskId": task_id,
            "error": error_msg,
        }

    def _format_error_response(self, result: dict[str, Any], task_id: str) -> dict[str, Any]:
        """格式化适配器返回的错误为统一响应格式"""
        return {
            "success": False,
            "imageUrl": None,
            "imageBase64": None,
            "localPath": None,
            "cost": 0.0,
            "provider": result.get("provider"),
            "model": result.get("model"),
            "taskId": task_id,
            "error": result.get("error", "未知错误"),
        }

