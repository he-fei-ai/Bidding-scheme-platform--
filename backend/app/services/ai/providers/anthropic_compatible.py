import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
import logging
import random
from typing import Any

import httpx

from ....utils.proxy_config import build_httpx_proxy_settings
from .base import AIMessage


logger = logging.getLogger(__name__)


class AnthropicCompatibleProvider:
    """Anthropic 兼容的 AI Provider（支持 Claude 及中转站）"""

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.anthropic.com/v1",
        model: str = "claude-3-5-sonnet-20241022",
        max_tokens: int = 4096,
        temperature: float = 0.7,
        timeout: int = 300,
        max_retries: int = 3,
        rate_limit_per_min: int = 0,
        concurrency: int = 0,
    ):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.timeout = timeout
        self._max_retries = max_retries
        # BUG-FIX 2026-10-07：与 openai_compatible 对齐，使限流配置能贯通到
        # 所有 provider 构造路径（此前 _make_provider 会传这两个参数导致 TypeError）。
        self._rate_limit_per_min = int(rate_limit_per_min or 0)
        self._concurrency = int(concurrency or 0)
        self._client: httpx.AsyncClient | None = None
        self._client_lock = asyncio.Lock()

    @asynccontextmanager
    async def _get_client(self, timeout: int) -> AsyncIterator[httpx.AsyncClient]:
        if self._client is None:
            async with self._client_lock:
                if self._client is None:
                    self._client = httpx.AsyncClient(
                        timeout=httpx.Timeout(timeout, read=timeout * 2),
                        limits=httpx.Limits(max_connections=50, max_keepalive_connections=10),
                        **build_httpx_proxy_settings(),
                    )
        try:
            yield self._client
        except (httpx.ConnectError, httpx.RemoteProtocolError):
            # 连接级别异常时关闭客户端，下次重建
            if self._client is not None:
                await self._client.aclose()
                self._client = None
            raise

    @property
    def supports_json_mode(self) -> bool:
        return False

    async def chat(
        self,
        messages: list[AIMessage],
        *,
        max_retries: int | None = None,
        return_meta: bool = False,
        **options: Any,
    ) -> str | dict:
        """调用 Anthropic API 生成文本"""
        effective_timeout = self.timeout
        if options.get("max_tokens", 0) >= 16000:
            effective_timeout = 240

        if max_retries is None:
            max_retries = self._max_retries

        cancel_event = options.get("cancel_event")

        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }

        system_content = ""
        user_messages = []
        for msg in messages:
            if msg.role == "system":
                system_content = msg.content
            else:
                user_messages.append({"role": msg.role, "content": msg.content})

        payload = {
            "model": options.get("model", self.model),
            "max_tokens": options.get("max_tokens", self.max_tokens),
            "temperature": options.get("temperature", self.temperature),
            "messages": user_messages,
        }

        # Prompt Caching（提示词缓存，命中后输入成本大幅下降）：
        # 将稳定的 SYSTEM 提示词标记为可缓存块。仅对官方 anthropic.com 端点启用，
        # 避免部分「中转站」不接受 block 结构的 system 而报 400。
        # 参考设计文档（四）：稳定内容前置 + cache_control 命中缓存。
        use_prompt_cache = self.base_url.startswith("https://api.anthropic.com") and bool(
            system_content
        )
        if system_content:
            if use_prompt_cache:
                headers["anthropic-beta"] = "prompt-caching-2024-07-31"
                payload["system"] = [
                    {
                        "type": "text",
                        "text": system_content,
                        "cache_control": {"type": "ephemeral"},
                    },
                ]
            else:
                payload["system"] = system_content

        url = self.base_url
        if not url.endswith("/messages"):
            url = f"{url}/messages"

        for attempt in range(max_retries):
            if cancel_event and cancel_event.is_set():
                raise asyncio.CancelledError("操作已被用户取消")

            try:
                async with self._get_client(effective_timeout) as client:
                    response = await client.post(url, headers=headers, json=payload)

                    if response.status_code == 429:
                        body_text = response.text
                        if (
                            "quota" in body_text.lower()
                            or "limit" in body_text.lower()
                            or "exhausted" in body_text.lower()
                            or "insufficient" in body_text.lower()
                        ):
                            raise RuntimeError(
                                f"API 配额不足或已耗尽 (HTTP 429): {body_text[:200]}"
                            )
                        retry_after = response.headers.get("retry-after")
                        wait_time = (
                            int(retry_after)
                            if retry_after and retry_after.isdigit()
                            else (2**attempt)
                        )
                        if attempt == max_retries - 1:
                            raise RuntimeError(
                                f"请求频率过高，重试失败 (HTTP 429): {body_text[:200]}"
                            )
                        logger.warning(
                            "HTTP 429 限流，等待 %ss 后重试 (%s/%s)", wait_time, attempt + 1, max_retries
                        )
                        raise RetryableError(f"rate_limited:{wait_time}")

                    if response.status_code in (500, 502, 503, 504):
                        raise RetryableError(
                            f"可重试错误 (HTTP {response.status_code}): {response.text[:200]}"
                        )

                    if response.status_code == 400:
                        raise ValueError(f"请求参数错误 (HTTP 400): {response.text[:300]}")
                    if response.status_code == 401:
                        raise RuntimeError("API Key 无效或未授权 (HTTP 401)")
                    if response.status_code == 402:
                        raise RuntimeError(
                            f"API 配额已耗尽，请检查账户余额和套餐 (HTTP 402): {response.text[:300]}"
                        )
                    if response.status_code == 403:
                        raise RuntimeError(f"访问被拒绝 (HTTP 403): {response.text[:300]}")
                    if response.status_code == 404:
                        raise RuntimeError(f"模型不存在 (HTTP 404): {response.text[:300]}")

                    response.raise_for_status()
                    data = response.json()

                    if not data.get("content"):
                        raise ValueError("API 返回空 content")

                    if isinstance(data["content"], list):
                        content = "".join(
                            block.get("text", "")
                            for block in data["content"]
                            if block.get("type") == "text"
                        )
                    else:
                        content = str(data["content"])

                    # Anthropic stop_reason (end_turn/max_tokens/stop_sequence/tool_use)
                    # -> OpenAI finish_reason 统一格式
                    raw_stop = data.get("stop_reason", "end_turn")
                    if raw_stop == "max_tokens":
                        finish_reason = "length"
                    elif raw_stop in ("end_turn", "stop_sequence"):
                        finish_reason = "stop"
                    else:
                        finish_reason = raw_stop

                    if return_meta:
                        usage = data.get("usage", {})
                        return {
                            "content": content,
                            "finish_reason": finish_reason,
                            "usage": usage,
                        }
                    return content

            except httpx.TimeoutException as e:
                if attempt == max_retries - 1:
                    raise RuntimeError(f"请求超时（{effective_timeout}秒）: {e}") from e
                # 添加 jitter 防止 thundering herd
                wait_time = (2**attempt) + random.uniform(0, 1)
                await asyncio.sleep(wait_time)

            except httpx.NetworkError as e:
                if attempt == max_retries - 1:
                    raise RuntimeError(f"网络错误: {e}") from e
                # 添加 jitter 防止 thundering herd
                wait_time = (2**attempt) + random.uniform(0, 1)
                await asyncio.sleep(wait_time)

            except RetryableError as e:
                if attempt == max_retries - 1:
                    raise RuntimeError(f"多次重试失败: {e}") from e
                wait_time = 2**attempt
                msg = str(e)
                if msg.startswith("rate_limited:"):
                    try:
                        wait_time = int(msg.split(":", 1)[1])
                    except (ValueError, IndexError):
                        pass
                logger.warning("重试 {attempt + 1}/{max_retries} (等待 {wait_time}s): %s", max_retries)
                await asyncio.sleep(wait_time)

            except Exception as e:
                raise RuntimeError(f"API 调用失败: {e}") from e

        raise RuntimeError("超过最大重试次数")


class RetryableError(Exception):
    """可重试错误"""

    pass

