import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
import json
import logging
import random
import time
from typing import Any
import httpx
from ....utils.proxy_config import build_httpx_proxy_settings
from ..rate_gate import _parse_retry_after, rate_gate as _rate_gate
from .base import AIMessage
logger = logging.getLogger(__name__)
_EMPTY_LENGTH_MAX_RETRIES = 3
import os as _os_agn
_AGNES_MAX_CONCURRENCY = int(_os_agn.environ.get('AGNES_MAX_CONCURRENCY', '5'))
_GLOBAL_REQUEST_SEMAPHORE = asyncio.Semaphore(_AGNES_MAX_CONCURRENCY)

# ── 429 限流专用重试预算（BUG-FIX 2026-10-07）────────────────────────────
# 生产日志实证：22:34~22:41 持续 7 分钟、每分钟 70~97 次 HTTP 429，累计 528 条。
# 根因之一是 429 复用了通用 max_retries（provider_factory 传 2）：
# `if attempt >= max_retries - 1` 使 429 实际只剩 **1 次重试 + 3~6s 等待**，
# 面对以分钟计的限流窗口必然失败。限流需要独立且显著更宽的预算。
_RATE_LIMIT_MAX_RETRIES = int(_os_agn.environ.get('AI_RATE_LIMIT_RETRIES', '6') or 6)
_RATE_LIMIT_BASE_WAIT = float(_os_agn.environ.get('AI_RATE_LIMIT_BASE_WAIT', '3') or 3)
_RATE_LIMIT_MAX_WAIT = float(_os_agn.environ.get('AI_RATE_LIMIT_MAX_WAIT', '60') or 60)

async def _cancellable_sleep(cancel_event: asyncio.Event | None, wait_time: float, jitter: bool=True) -> None:
    """可被取消事件中断的退避等待，带 jitter 抖动。

    - jitter=True 时实际等待时间为 wait_time 的 50%~100%，避免 thundering herd。
    - 等待期间若 cancel_event 被 set，立即抛出 CancelledError。
    - cancel_event 为 None 时退化为普通 asyncio.sleep。
    """
    if jitter:
        wait_time = wait_time * (0.5 + 0.5 * random.random())
    if cancel_event is None:
        await asyncio.sleep(wait_time)
        return
    try:
        await asyncio.wait_for(cancel_event.wait(), timeout=wait_time)
        raise asyncio.CancelledError('操作已被用户取消')
    except TimeoutError:
        pass

class OpenAICompatibleProvider:
    """OpenAI 兼容的 AI Provider（支持 OpenAI、通义千问、MiniMax、Kimi、智谱 GLM 等）"""

    def __init__(self, api_key: str, base_url: str='https://api.openai.com/v1', model: str='gpt-3.5-turbo', max_tokens: int=4096, temperature: float=0.7, timeout: int=120, max_retries: int=3, rate_limit_per_min: int=0, concurrency: int=0):
        self.api_key = api_key
        self.base_url = base_url.rstrip('/')
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.timeout = timeout
        self._max_retries = max_retries
        # BUG-FIX 2026-10-07：限流闸门参数。此前 rate_limit_per_min 只存在于
        # DB/UI（ai_config.py、profile_service.py）却从未被读取，属死配置，
        # 是"配了限流却不生效"的直接原因。
        self._rate_limit_per_min = int(rate_limit_per_min or 0)
        # 自适应并发上限不得超过全局信号量的硬上限：否则闸门比信号量更宽松，
        # AIMD 首次收缩会落在信号量的限制之上，变成一轮无效收缩。
        self._concurrency = (
            max(1, min(int(concurrency), _AGNES_MAX_CONCURRENCY)) if concurrency else 0
        )
        self._client: httpx.AsyncClient | None = None
        self._client_lock = asyncio.Lock()
        self._last_usage: dict[str, int] = {}
        self._last_response_time_ms: int = 0

    @asynccontextmanager
    async def _get_client(self, timeout: int, headers: dict) -> AsyncIterator[httpx.AsyncClient]:
        """复用 httpx.AsyncClient 实例避免重复建连"""
        if self._client is None:
            async with self._client_lock:
                if self._client is None:
                    self._client = httpx.AsyncClient(timeout=httpx.Timeout(timeout, read=int(timeout * 1.5), write=timeout, connect=30.0), headers=headers, limits=httpx.Limits(max_connections=50, max_keepalive_connections=10, keepalive_expiry=30), **build_httpx_proxy_settings())
        try:
            yield self._client
        except (httpx.ConnectError, httpx.RemoteProtocolError):
            # BUG-FIX 2026-10-08（P1 并发竞态）：client 被多任务并发复用，此处
            # aclose 会关掉其它在飞请求正在使用的连接池，制造级联失败。
            # 改为仅置空引用（下次调用重建新 client），旧实例由在飞请求
            # 结束后交由 GC 回收；连接类错误本身无需主动销毁连接池。
            self._client = None
            raise

    @property
    def supports_json_mode(self) -> bool:
        return True

    async def chat(self, messages: list[AIMessage], *, max_retries: int | None=None, return_meta: bool=False, **options: Any) -> str | dict:
        """调用 OpenAI 兼容 API 生成文本"""
        async with _GLOBAL_REQUEST_SEMAPHORE:
            start_time = time.time()
            effective_timeout = self.timeout
            requested_max_tokens = options.get('max_tokens', self.max_tokens)
            try:
                requested_max_tokens = min(max(int(requested_max_tokens or 4096), 256), 65536)
            except (TypeError, ValueError):
                requested_max_tokens = 4096
            if requested_max_tokens >= 16000:
                effective_timeout = max(effective_timeout, 180)
            elif requested_max_tokens >= 8000:
                effective_timeout = max(effective_timeout, 120)
            if max_retries is None:
                max_retries = self._max_retries
            headers = {'Authorization': f'Bearer {self.api_key}', 'Content-Type': 'application/json', 'User-Agent': 'BidAssistant/1.0 (AI Service; +https://github.com/bid-assistant)'}
            payload = {'model': options.get('model', self.model), 'messages': [{'role': m.role, 'content': m.content} for m in messages], 'max_tokens': requested_max_tokens, 'temperature': options.get('temperature', self.temperature)}
            if options.get('json_mode'):
                payload['response_format'] = {'type': 'json_object'}
            extra_body = options.get('extra_body')
            if extra_body and isinstance(extra_body, dict):
                for k, v in extra_body.items():
                    payload[k] = v
            last_error = None
            current_max_tokens = requested_max_tokens
            empty_retries = 0
            # BUG-FIX 2026-10-07：429 独立计数，不再与通用 max_retries 共用预算。
            rate_limit_hits = 0
            # BUG-FIX 2026-10-08（P1 预算挤压）：429 分支 continue 同样会推进 for
            # 的 attempt，原先 429 重试会把 5xx/超时/空内容的通用预算吃光——
            # max_retries=2 时只要先发生 ≥2 次 429，后续第一个通用错误就零重试
            # 直接抛错。通用错误改用独立计数 generic_retries。
            generic_retries = 0
            # 循环上限需容纳三类重试：通用 max_retries、空内容截断、429 限流。
            # BUG-FIX 2026-10-07：原先只有 max_retries + _EMPTY_LENGTH_MAX_RETRIES，
            # 429 拿到独立预算后若不同步放宽上限，会在预算用完前被外层 range 截断，
            # 抛出误导性的「超过最大重试次数」。
            total_attempts = max_retries + _EMPTY_LENGTH_MAX_RETRIES + _RATE_LIMIT_MAX_RETRIES
            for attempt in range(total_attempts):
                cancel_event = options.get('cancel_event')
                if cancel_event and cancel_event.is_set():
                    raise asyncio.CancelledError('操作已被用户取消')
                try:
                    async with self._get_client(effective_timeout, headers) as client:
                        cancel_event_check = options.get('cancel_event')
                        if cancel_event_check and cancel_event_check.is_set():
                            raise asyncio.CancelledError('操作已被用户取消')
                        # 限流闸门：每一次 HTTP 请求单独取许可（BUG-FIX 2026-10-07）。
                        # 令牌桶按 RPM 节流、自适应并发在 429 时即时收缩；
                        # 许可在此粒度获取/释放，使退避等待期间不占用并发额度。
                        async with _rate_gate.slot(
                            self.base_url,
                            rpm=self._rate_limit_per_min or None,
                            concurrency=self._concurrency or None,
                            cancel_event=cancel_event_check,
                        ):
                            # BUG-FIX 2026-10-08（P2 超时失效）：client 复用后其建连时
                            # 的固定 timeout 不再跟随本次 effective_timeout（长文
                            # max_tokens>=16000 → 180s 会被旧读超时误杀）。httpx 支持
                            # 每请求覆盖超时，这里显式传入本次计算结果。
                            response = await client.post(
                                f'{self.base_url}/chat/completions',
                                json=payload,
                                timeout=httpx.Timeout(effective_timeout, read=int(effective_timeout * 1.5), write=effective_timeout, connect=30.0),
                            )
                        cancel_event_after = options.get('cancel_event')
                        if cancel_event_after and cancel_event_after.is_set():
                            raise asyncio.CancelledError('操作已被用户取消')
                        if response.status_code == 429:
                            body_text = response.text
                            lower_body = body_text.lower()
                            if any((word in lower_body for word in ['quota', 'exhausted', 'insufficient_quota'])):
                                raise RuntimeError(f'API 配额已耗尽 (HTTP 429): {body_text[:300]}')
                            rate_limit_hits += 1
                            if rate_limit_hits > _RATE_LIMIT_MAX_RETRIES:
                                raise RuntimeError(
                                    f'请求频率过高，重试失败 (HTTP 429, 已重试 {rate_limit_hits - 1} 次): {body_text[:300]}'
                                )
                            # 等待时长三方取大：
                            #   ① 服务端 Retry-After（秒数 / HTTP-date 两种格式都支持）
                            #   ② 限流闸门按收缩后并发给出的相称冷却窗口
                            #   ③ 本地指数退避 base * 2^(n-1)
                            # 最后由 _cancellable_sleep 施加 jitter，避免多章节同步重试。
                            _retry_after = _parse_retry_after(response.headers.get('retry-after'))
                            _gate_wait = _rate_gate.on_rate_limited(self.base_url, _retry_after)
                            _exp = min(_RATE_LIMIT_MAX_WAIT, _RATE_LIMIT_BASE_WAIT * (2 ** (rate_limit_hits - 1)))
                            wait_time = max(_retry_after or 0.0, _gate_wait, _exp)
                            logger.warning(
                                'HTTP 429 限流，等待 %.1fs 后重试 (限流重试 %d/%d, retry_after=%s)',
                                wait_time, rate_limit_hits, _RATE_LIMIT_MAX_RETRIES,
                                response.headers.get('retry-after'),
                            )
                            if cancel_event_check and cancel_event_check.is_set():
                                raise asyncio.CancelledError('操作已被用户取消')
                            await _cancellable_sleep(cancel_event_check, wait_time)
                            continue
                        if response.status_code in (500, 502, 503, 504, 520):
                            last_error = f'可重试错误 (HTTP {response.status_code}): {response.text[:200]}'
                            logger.warning('HTTP %d 错误，准备重试 (%d/%d)', response.status_code, generic_retries + 1, max_retries)
                            # BUG-FIX 2026-10-08：以 generic_retries（而非被 429 推进的
                            # attempt）判定通用重试预算是否耗尽。
                            if generic_retries >= max_retries - 1:
                                raise RuntimeError(f'可重试错误 (HTTP {response.status_code}): {response.text[:200]}')
                            generic_retries += 1
                            wait_time = 2 ** (generic_retries - 1) * (0.5 + 0.5 * random.random())
                            await _cancellable_sleep(cancel_event, wait_time)
                            continue
                        if response.status_code == 400:
                            raise ValueError(f'请求参数错误 (HTTP 400): {response.text[:300]}')
                        if response.status_code == 401:
                            raise RuntimeError('API Key 无效或未授权 (HTTP 401)')
                        if response.status_code == 402:
                            raise RuntimeError(f'API 配额已耗尽，请检查账户余额和套餐 (HTTP 402): {response.text[:300]}')
                        if response.status_code == 403:
                            raise RuntimeError(f'访问被拒绝 (HTTP 403): {response.text[:300]}')
                        if response.status_code == 404:
                            raise RuntimeError(f'模型不存在 (HTTP 404): {response.text[:300]}')
                        response.raise_for_status()
                        data = response.json()
                        choice = {}
                        if not data.get('choices') and data.get('result') is not None:
                            agnes_result = data['result']
                            _agr_stripped = agnes_result.strip() if isinstance(agnes_result, str) else str(agnes_result)
                            if not _agr_stripped:
                                logger.warning('Agnes result 字段为空/空白 (type=%s), 原始长度=%d, 跳过 Agnes 解包直接报错', type(agnes_result).__name__, len(agnes_result) if hasattr(agnes_result, '__len__') else '?')
                                raise ValueError(f'Agnes API 返回空 result (type={type(agnes_result).__name__})')
                            if isinstance(agnes_result, str):
                                content = agnes_result
                                logger.info('检测到 Agnes 包裹格式响应，已解包 result 字段 (length=%d)', len(content))
                            elif isinstance(agnes_result, list) or isinstance(agnes_result, dict):
                                content = json.dumps(agnes_result, ensure_ascii=False)
                            else:
                                content = str(agnes_result)
                        elif not data.get('choices'):
                            raise ValueError('API 返回空 choices，请检查请求参数')
                        else:
                            choice = data['choices'][0]
                            content = choice.get('message', {}).get('content', '')
                        if not content:
                            finish_reason = choice.get('finish_reason', 'unknown')
                            try:
                                _preview = json.dumps(data, ensure_ascii=False)[:500]
                                logger.warning('API 返回空内容, finish_reason=%s, response_keys=%s, response_preview=%s', finish_reason, list(data.keys())[:8], _preview)
                            except Exception:
                                logger.warning('API 返回空内容, finish_reason=%s (response 序列化失败)', finish_reason)
                            if finish_reason == 'length':
                                empty_retries += 1
                                if empty_retries <= _EMPTY_LENGTH_MAX_RETRIES:
                                    logger.warning('输出被截断且内容为空，max_tokens=%d 重试 (%d/%d)', current_max_tokens, empty_retries, _EMPTY_LENGTH_MAX_RETRIES)
                                    await _cancellable_sleep(options.get('cancel_event'), 1)
                                    continue
                                raise ValueError(f'API 返回空内容 (finish_reason: {finish_reason})')
                            if generic_retries < max_retries - 1:
                                generic_retries += 1
                                cancel_event_retry = options.get('cancel_event')
                                if cancel_event_retry and cancel_event_retry.is_set():
                                    raise asyncio.CancelledError('操作已被用户取消')
                                await _cancellable_sleep(cancel_event_retry, 1)
                                continue
                            raise ValueError(f'API 返回空内容 (finish_reason: {finish_reason})')
                        elapsed = time.time() - start_time
                        self._last_response_time_ms = int(elapsed * 1000)
                        # AIMD 的"加性增"信号：一次非限流成功。连续成功达阈值后
                        # 限流闸门把并发 +1 逐步恢复到初始上限。
                        _rate_gate.on_success(self.base_url)
                        try:
                            from ...ai_monitor import record_ai_call
                            task_type = options.get('task_type', '')
                            record_ai_call(provider='openai_compatible', model=self.model, success=True, response_time=elapsed, task_type=task_type, error='')
                        except Exception as monitor_error:
                            logger.debug('监控记录失败: %s', monitor_error)
                        finish_reason = choice.get('finish_reason', 'stop') if choice else 'stop'
                        # BUG-FIX 2026-10-08（低）：本次响应缺 usage 时不得返回
                        # 上一请求的陈旧 token 数（record_prompt_cache 会记错），
                        # 返回全零占位更真实。
                        _usage_meta = {'prompt_tokens': 0, 'completion_tokens': 0, 'total_tokens': 0}
                        if data.get('usage'):
                            u = data['usage']
                            self._last_usage = {'prompt_tokens': u.get('prompt_tokens', 0), 'completion_tokens': u.get('completion_tokens', 0), 'total_tokens': u.get('total_tokens', 0)}
                            _usage_meta = dict(self._last_usage)
                        if return_meta:
                            return {'content': content, 'finish_reason': finish_reason, 'usage': _usage_meta}
                        return content
                except httpx.TimeoutException as e:
                    last_error = f'请求超时（{effective_timeout}秒）: {e}'
                    logger.warning('请求超时，准备重试 (%d/%d)', generic_retries + 1, max_retries)
                    if cancel_event and cancel_event.is_set():
                        raise asyncio.CancelledError('操作已被用户取消') from e
                    if generic_retries < max_retries - 1:
                        cancel_event_timeout = options.get('cancel_event')
                        if cancel_event_timeout and cancel_event_timeout.is_set():
                            raise asyncio.CancelledError('操作已被用户取消') from e
                        await _cancellable_sleep(cancel_event_timeout, 2 ** generic_retries)
                        generic_retries += 1
                    else:
                        raise RuntimeError(f'请求超时（{effective_timeout}秒）: {e}') from e
                except httpx.NetworkError as e:
                    last_error = f'网络错误: {e}'
                    logger.warning('网络错误，准备重试 (%d/%d)', generic_retries + 1, max_retries)
                    if cancel_event and cancel_event.is_set():
                        raise asyncio.CancelledError('操作已被用户取消') from e
                    if generic_retries < max_retries - 1:
                        cancel_event_network = options.get('cancel_event')
                        if cancel_event_network and cancel_event_network.is_set():
                            raise asyncio.CancelledError('操作已被用户取消') from e
                        await _cancellable_sleep(cancel_event_network, 2 ** generic_retries)
                        generic_retries += 1
                    else:
                        raise RuntimeError(f'网络错误: {e}') from e
                except httpx.HTTPStatusError as e:
                    last_error = f'HTTP 错误: {e}'
                    logger.warning('HTTP 状态错误，准备重试 (%d/%d)', generic_retries + 1, max_retries)
                    if generic_retries < max_retries - 1:
                        await _cancellable_sleep(cancel_event, 2 ** generic_retries)
                        generic_retries += 1
                    else:
                        raise RuntimeError(f'HTTP 错误: {e}') from e
                except asyncio.CancelledError:
                    elapsed = time.time() - start_time
                    try:
                        from ...ai_monitor import record_ai_call
                        task_type = options.get('task_type', '')
                        # BUG-FIX 2026-10-08（低）：用户取消不应记为成功调用（成功率虚高）
                        record_ai_call(provider='openai_compatible', model=self.model, success=False, response_time=elapsed, task_type=task_type, error='操作已被用户取消')
                    except Exception as monitor_error:
                        logger.debug('监控记录失败: %s', monitor_error)
                    raise
                except RuntimeError:
                    raise
                except ValueError:
                    raise
                except Exception as e:
                    last_error = f'API 调用失败: {type(e).__name__}: {e}'
                    logger.error(last_error)
                    if attempt < max_retries - 1:
                        await _cancellable_sleep(cancel_event, 1)
                    else:
                        try:
                            elapsed = time.time() - start_time
                            from ...ai_monitor import record_ai_call
                            task_type = options.get('task_type', '')
                            record_ai_call(provider='openai_compatible', model=self.model, success=False, response_time=elapsed, tokens_used=0, error=str(e), task_type=task_type)
                        except Exception as monitor_error:
                            logger.debug('监控记录失败时调用异常（忽略）: %s', monitor_error)
                        raise RuntimeError(f'API 调用最终失败 ({max_retries} 次重试后): {e}') from e
            raise RuntimeError(f'超过最大重试次数 ({max_retries}): {last_error}')

    async def stream_chat(self, messages: list[AIMessage], **options: Any) -> AsyncIterator[str]:
        """流式调用 OpenAI 兼容 API

        FIX 2026-10-08（遗留2）：本方法当前零调用方，但一旦启用就会**绕过** chat()
        的三层保护（全局信号量 / 限流闸门 / 每请求超时），一个长流式请求即可占满
        并发额度并打穿 RPM 限制。这里补齐同等保护：入 slots 前后的凭据/超时与 chat()
        完全同口径（并发许可以整条流为单位持有，直到流结束才释放）。
        """
        import json as _json
        headers = {'Authorization': f'Bearer {self.api_key}', 'Content-Type': 'application/json'}
        payload = {'model': options.get('model', self.model), 'messages': [{'role': m.role, 'content': m.content} for m in messages], 'max_tokens': options.get('max_tokens', self.max_tokens), 'temperature': options.get('temperature', self.temperature), 'stream': True}
        cancel_event = options.get('cancel_event')
        requested_max_tokens = options.get('max_tokens', self.max_tokens)
        try:
            requested_max_tokens = min(max(int(requested_max_tokens or 4096), 256), 65536)
        except (TypeError, ValueError):
            requested_max_tokens = 4096
        effective_timeout = self.timeout
        if requested_max_tokens >= 16000:
            effective_timeout = max(effective_timeout, 180)
        elif requested_max_tokens >= 8000:
            effective_timeout = max(effective_timeout, 120)
        _req_timeout = httpx.Timeout(
            effective_timeout, read=int(effective_timeout * 1.5),
            write=effective_timeout, connect=30.0,
        )
        async with _GLOBAL_REQUEST_SEMAPHORE:
            async with _rate_gate.slot(
                self.base_url,
                rpm=self._rate_limit_per_min or None,
                concurrency=self._concurrency or None,
                cancel_event=cancel_event,
            ):
                if cancel_event is not None and cancel_event.is_set():
                    raise asyncio.CancelledError('操作已被用户取消')
                async with self._get_client(effective_timeout, headers) as client:
                    async with client.stream(
                        'POST', f'{self.base_url}/chat/completions', json=payload, timeout=_req_timeout,
                    ) as response:
                        response.raise_for_status()
                        async for line in response.aiter_lines():
                            if cancel_event is not None and cancel_event.is_set():
                                raise asyncio.CancelledError('操作已被用户取消')
                            if not line or not line.startswith('data: '):
                                continue
                            data = line[6:]
                            if data == '[DONE]':
                                break
                            try:
                                chunk = _json.loads(data)
                            except _json.JSONDecodeError:
                                continue
                            # BUG-FIX 2026-10-08（低）：choices 为空数组或 delta 为 null 时
                            # 原实现直接 IndexError/AttributeError 中断流。加固为空值安全。
                            _choices = chunk.get('choices') or [{}]
                            _choice0 = _choices[0] if isinstance(_choices[0], dict) else {}
                            delta = _choice0.get('delta') or {}
                            content = delta.get('content')
                            if content:
                                yield content

    async def close(self):
        """关闭底层 HTTP 客户端"""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def last_usage(self) -> dict[str, int]:
        """最近一次调用的 token 用量"""
        return self._last_usage.copy()

    @property
    def last_response_time_ms(self) -> int:
        """最近一次调用的响应时间（毫秒）"""
        return self._last_response_time_ms
