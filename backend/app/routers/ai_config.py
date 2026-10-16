import asyncio
import json
import logging
import os
import re
import sqlite3
import threading
import time
from datetime import datetime
from urllib.parse import urlparse
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
import httpx
from pydantic import BaseModel

from ..config import AGNES_API_KEY, AGNES_BASE_URL, AGNES_MODEL
from ..db import get_db, get_db_with_retry
from ..security.auth import get_current_user
from ..services.ai import provider_catalog
from ..services.ai.cache_metrics import snapshot
from ..services.ai.image_providers.registry import ProviderRegistry
from ..services.ai.image_styles import list_image_styles
from ..services.ai.provider_factory import (
    _clear_ai_config_cache,
    bump_ai_config_version,
    detect_provider_type,
    get_ai_config_version,
    get_key_rotation_status,
    get_provider_priority_order,
    mask_key_for_audit,
    record_config_audit,
    record_provider_usage,
    set_key_rotated_at,
)
from ..services.ai.provider_factory import create_provider as _create_shared_provider
from ..services.crypto import decrypt_api_key, encrypt_api_key, is_encrypted
from ..utils.proxy_config import build_httpx_proxy_settings
logger = logging.getLogger(__name__)
router = APIRouter(prefix='/ai', tags=['ai_config'], dependencies=[Depends(get_current_user)])
API_KEY_MASK = '***已配置***'

def _is_masked_api_key(api_key: str) -> bool:
    """判断 api_key 是否为脱敏占位符。

    使用长度+字符集比较，避免编码问题导致的中文字符匹配失败。
    """
    _MASK_CHARS = set(API_KEY_MASK)
    return bool(api_key and (api_key == API_KEY_MASK or (len(api_key) == len(API_KEY_MASK) and api_key.startswith('***') and api_key.endswith('***') and all((c in _MASK_CHARS for c in api_key)))))

def _plain_api_key(key: str) -> str:
    """将 ENC: 密文转换为明文（仅供服务端内部调用，禁止用于 HTTP 响应）。"""
    if not key:
        return key
    return decrypt_api_key(key) if is_encrypted(key) else key


def _display_api_key(key: str) -> str:
    """HTTP 读取路径统一脱敏：有值返回占位符，空值返回空串。

    BUG-FIX（安全 P0）：此前 `/ai/config`、`/ai/provider-configs` 等读接口会把
    解密后的**明文** API Key 直接回传，使数据库侧的 `ENC:` 加密存储完全失效 ——
    任何能调用这些接口的人都能拿到平台主密钥与各厂商密钥。
    """
    if not key:
        return ''
    return API_KEY_MASK


def _current_user_of(request: Request | None) -> dict | None:
    """鑾峰彇褰撳鐢ㄦ埛鍙傛暟鍙?JwtAuthMiddleware 鍐呭彨(request.state.current_user) 鍚?get_current_user 鍙傚墠绯诲垪锛屾墍鏈夶粠鍙傛暟鑷?"""
    if request is None:
        return None
    cu = getattr(request.state, 'current_user', None)
    if isinstance(cu, dict):
        return cu
    u = get_current_user(request)
    if u is None:
        return None
    return {'id': getattr(u, 'id', None), 'username': getattr(u, 'username', None), 'role': getattr(u, 'role', None)}


def _require_admin(request: Request) -> dict:
    """RBAC: 鏄?admin浠ュ悄杩斿洗鐢ㄦ埛鍙傛暟鍙?涓嶅彲鍦?admin鍐欏叆 HTTP 403"""
    user = _current_user_of(request)
    if not user or user.get('role') != 'admin':
        raise HTTPException(status_code=403, detail='P0-RBAC: 鍙傚墠绌€')
    return user


def _existing_provider_api_key(provider_name: str) -> str:
    """读取某厂商已保存的 api_key（库内原值，可能是 ENC: 密文），不存在返回空串。"""
    try:
        with get_db_with_retry() as conn:
            row = conn.execute(
                'SELECT api_key FROM text_provider_configs WHERE provider_name = ?',
                [provider_name],
            ).fetchone()
        if row and row['api_key']:
            return row['api_key']
    except Exception as _e:
        logger.debug("读取已保存密钥失败 provider=%s: %s", provider_name, _e)
    return ''


def _existing_image_provider_api_key(provider_name: str) -> str:
    """读取图像厂商已保存的 api_key（库内原值），不存在返回空串。"""
    try:
        with get_db_with_retry() as conn:
            row = conn.execute(
                'SELECT api_key FROM image_provider_configs WHERE provider_name = ?',
                [provider_name],
            ).fetchone()
        if row and row['api_key']:
            return row['api_key']
    except Exception as _e:
        logger.debug("读取已保存图像密钥失败 provider=%s: %s", provider_name, _e)
    return ''


def _encrypt_fresh_key(key: str) -> str:
    """F-1 安全修复（2026-10-04）：用户新提交的明文密钥统一 ENC: 加密后落库。

    此前 `encrypt_api_key` 全仓**零调用点** —— 读取侧按密文解密、注释宣称
    「ENC: 加密存储」，但所有写路径实际明文入库，密钥保护仅为纸面承诺。
    现在是真正的单点加密入口：
      - 空串 / 脱敏占位符（回填的库内原值，已是密文形态）→ 原样返回；
      - 已是 ENC: 密文 → 原样返回（幂等，不二次加密）；
      - 其余视为新明文 → 加密。
    向后兼容：历史明文行经 decrypt_api_key 原样透传，读取链路无需迁移。
    """
    if not key or _is_masked_api_key(key) or is_encrypted(key):
        return key
    return encrypt_api_key(key)


def _actor_of(request) -> str:
    """L-5 审计主体提取：优先 JWT 解析后的 request.state.user；
    测试/内部调用直接传函数参数时 request=None，退化为 'unknown'。"""
    if request is None:
        return 'unknown'
    return getattr(getattr(request.state, 'user', None), 'username', None) or 'unknown'


def _audit(action: str, target: str, detail: str = '', request=None) -> None:
    """L-5 调用侧防御：审计无论如何不得阻断配置主流程。

    record_config_audit 自身的 try/except 只覆盖库内写入；本包装兼顾
    参数拼装/未来实现变更等一切异常（测试用 monkeypatch 替换本体时
    同样验证了这一层）。
    """
    try:
        record_config_audit(_actor_of(request), action, target, detail)
    except Exception as e:
        logger.warning('配置审计写入失败（不阻断主流程）: %s', e)


def _resolve_api_key(api_key: str) -> str:
    """传入脱敏占位符时，从数据库解析真实密钥；否则原样返回。

    L-6 泄露面收敛（2026-10-04）：此前库内无密钥时回退返回 AGNES_API_KEY ——
    而 `/ai/models`、`/ai/model-info` 的 base_url 是调用方任意指定的，
    等于「任何人传占位符+自己的 URL 就能把平台主密钥发到该 URL」（域名可被
    DNS 重绑定/内网开关关闭时指向元数据地址）。现不再回退：解析不到就返回
    空串（无 Authorization 头，厂商侧 401，行为可理解不难排查）。
    正常使用路径不受影响：前端配置页的占位符对应库内必有已保存密钥。
    """
    if not _is_masked_api_key(api_key):
        return api_key
    try:
        with get_db_with_retry() as conn:
            row = conn.execute('SELECT api_key FROM ai_config WHERE id = 1').fetchone()
        if row and row['api_key']:
            stored = row['api_key'] or ''
            return decrypt_api_key(stored) if is_encrypted(stored) else stored
    except Exception as _e:
        logger.debug('解析占位符密钥失败: %s', _e)
    return ''


def _resolve_image_api_key(provider_name: str, api_key: str) -> str:
    """图像厂商密钥解析：传入脱敏占位符时，从该厂商已保存配置取真实密钥。

    BUG-FIX 2026-09-17：`GET /ai/image-provider-configs` 返回的 api_key 已是
    `***已配置***`，而 `POST /ai/image-model/test` 原先只处理 `ENC:` 密文，
    于是「已配置好的厂商点测试连接」必然 401 —— 用户会误判密钥失效并重置。
    """
    if not _is_masked_api_key(api_key):
        return _plain_api_key(api_key)
    return _plain_api_key(_existing_image_provider_api_key(provider_name))


def _validate_base_url(base_url: str) -> str | None:
    """校验 AI base_url，防止 file:// 等危险协议（SSRF 防护）。

    返回错误信息字符串（非法）或 None（合法）。
    本地优先部署允许 http/https（含 localhost 与内网地址，便于接入本地模型）。
    """
    if not base_url:
        return 'base_url 不能为空'
    try:
        parsed = urlparse(base_url)
    except Exception:
        return '非法的 base_url'
    if parsed.scheme not in ('http', 'https'):
        return f"不支持的协议: {parsed.scheme or '空'}，仅允许 http/https"
    if not parsed.hostname:
        return 'base_url 缺少主机名'
    # 增强 2026-09-29（SSRF 防护）：AI_ALLOW_INTERNAL_URL=false 时，禁止 base_url
    # 指向内网/环回/链路本地/云元数据等保留地址（复用 is_safe_http_url）。
    # 默认（true）保持「本地优先部署」行为，不做 DNS 解析，零性能/行为变化。
    from ..config import AI_ALLOW_INTERNAL_URL
    from ..utils.ssrf import is_safe_http_url
    if not AI_ALLOW_INTERNAL_URL and not is_safe_http_url(base_url, allow_private=False):
        return '不允许将 AI 服务指向内网/保留地址（如需本地或内网模型，请设置 AI_ALLOW_INTERNAL_URL=true）'
    return None

def _normalize_image_base_url(base_url: str) -> str:
    """归一化图像生成 base_url：去掉误填的生成端点后缀（如 /images/generations），
    避免与适配器内部拼接重复（如 /v1/images/generations/models）。"""
    if not base_url:
        return base_url
    return re.sub('/(?:dashscope/)?images/generations$', '', base_url.rstrip('/'))
# D-1（2026-10-06）：厂商元数据收敛到 provider_catalog 单一正本，本模块保留
# 同名别名以保持既有调用方/测试零改动。
BUILTIN_DEFAULT_PROVIDER = provider_catalog.DEFAULT_TEXT_PROVIDER
# 逐项浅拷贝：既保持 `KNOWN_PROVIDERS[i] = ...` 与就地改写的既有可变性
# （既有测试会改写首项做安全断言），又不会把正本改坏。
KNOWN_PROVIDERS = [dict(p) for p in provider_catalog.KNOWN_PROVIDERS]
PROVIDER_MODEL_METADATA = provider_catalog.PROVIDER_MODEL_METADATA

class AIConfigData(BaseModel):
    provider_name: str = ''
    provider: str = 'openai_compatible'
    api_key: str = ''
    model: str = ''
    base_url: str = 'https://api.openai.com/v1'
    document_profile: str | None = None
    concurrency: int = 4
    temperature: float = 0.7
    max_tokens: int = 4096
    # P1-CFG（2026-10-14）：timeout 此前**存在于 ai_config 表、也被 provider 工厂
    # 读取（create_provider 的 config.get('timeout', 120)），但请求模型里压根没有这个
    # 字段、SQL 也不写它 → 一个永远等于默认 120s 的「死列」。本字段补齐后主配置
    # 的超时才真正可调；范围与 Profile 的 timeout 口径对齐 [5, 600]。
    timeout: int = 120
    is_builtin_default: bool = False

@router.get('/providers')
async def list_known_providers():
    """返回已知厂商平台列表（D-1：正本在 provider_catalog，此处仅做脱敏投影）"""
    return provider_catalog.known_providers_public()

@router.get('/config')
async def get_ai_config():
    from ..services.ai.provider_factory import _load_env_config
    from ..services.ai.provider_factory import get_ai_config as _get_config
    # PERF（2026-10-14）：get_ai_config 内部持全局写锁做同步 SQLite 读，本端点是
    # 「AI 配置」/「文本模型配置」两页的加载必经路径，移出事件循环。
    cfg = await asyncio.to_thread(_get_config)
    # L-4（2026-10-04 遗留闭环）：回显配置来源，便于运维判断环境变量是否
    # 覆盖了库内配置（此前「环境变量优先级最高」对前端完全黑盒）。
    # 新增字段向后兼容：旧前端忽略未知字段即可。
    if _load_env_config():
        config_source = 'env'
    else:
        try:
            def _probe_ai_config_row():
                with get_db_with_retry(readonly=True) as conn:
                    return conn.execute('SELECT 1 FROM ai_config WHERE id = 1').fetchone()

            _row = await asyncio.to_thread(_probe_ai_config_row)
            config_source = 'database' if _row else 'default'
        except Exception:
            config_source = 'default'
    # BUG-FIX（安全 P0）：读接口一律返回脱敏占位符，不再解密回传明文。
    # 前端回显掩码并在保存时原样提交，`_resolve_api_key` 会把它解析为库内真实密钥。
    # 注意：env 来源时 cfg.api_key 是环境变量明文，同样必须脱敏。
    current_version = get_ai_config_version()
    return {**cfg, 'api_key': _display_api_key(cfg.get('api_key', '')), 'config_source': config_source, 'version': current_version, 'etag': str(current_version)}

@router.put('/config')
async def save_ai_config(data: AIConfigData, request: Request = None):
    provider = data.provider or detect_provider_type(data.base_url)
    is_mask = _is_masked_api_key(data.api_key)
    is_empty = not data.api_key
    if is_mask or is_empty:
        # BUG-FIX 2026-09-16（安全 P1 加密降级）：原实现把库里的 `ENC:` 密文
        # **解密成明文**再 UPSERT 回去 —— 用户只要「不改密钥、随便改个模型/温度保存一次」，
        # 库里的主密钥就从加密永久降级为明文，此前安全加固的成果被一次保存抹掉。
        # 读取侧（provider_factory.get_ai_config）本就会按 is_encrypted 解密，
        # 因此这里保持库内**原始形态**回写即可，无需解密。
        # PERF（2026-10-05）：纯读走 readonly + to_thread，不在事件循环内做同步 SQLite I/O
        def _read_stored_api_key():
            with get_db_with_retry(readonly=True) as conn:
                row = conn.execute('SELECT api_key FROM ai_config WHERE id = 1').fetchone()
            return dict(row) if row else None
        try:
            row = await asyncio.to_thread(_read_stored_api_key)
            if row:
                data.api_key = row['api_key'] or ''
            else:
                raise HTTPException(status_code=400, detail='首次配置请提供真实的 API Key，不能使用占位符')
        except HTTPException:
            raise
        except Exception as _e:
            logger.warning('读取已保存的 API Key 失败: %s', _e)
        # BUG-FIX（连带）：读取失败时不能让占位符原样落库，否则 ai_config.api_key
        # 会变成字符串 "***已配置***"，所有 AI 调用拿假密钥去鉴权（401 且难排查）。
        if is_mask and _is_masked_api_key(data.api_key):
            raise HTTPException(status_code=400, detail='无法解析已保存的 API Key，请重新填写真实密钥')
    else:
        pass
    # 乐观锁（If-Match / etag）：客户端需携带上次 GET /ai/config 返回的 etag，服务端写前校验是否与当前版本一致；
    # 缺失/不合法 → 400；与当前版本不符 → 409（配置已被他处修改，拒绝覆盖写，避免丢失更新）。
    if request is not None:
        # 乐观锁仅在 HTTP 层面强制；进程内直接调用(request=None)不受影响。
        if_match = request.headers.get('if-match')
        if if_match is None:
            raise HTTPException(status_code=400, detail='缺失 If-Match 请求头(etag)，请先 GET /ai/config 获取当前版本后再保存')
        try:
            expected_version = int(if_match.strip().strip('"'))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail='If-Match 请求头值非法')
        current_version = get_ai_config_version()
        if expected_version != current_version:
            raise HTTPException(
                status_code=409,
                detail='配置版本已冲突(etag=%s，当前=%s)，请刷新后重试' % (expected_version, current_version),
            )
    # 增强 2026-09-29（配置校验·范围/安全）：此前仅 max_tokens 做范围钳制，
    # temperature / concurrency 原样落库 —— 传入 -5 / 999 / 0 会导致模型产出行
    # 为乱码或直接报错，concurrency=0 更可能拖垮并发生成。现统一钳制到合法区间。
    # base_url 此前只在 /models 探测时校验，保存路径缺失，允许 file:// 等危险
    # 协议落库并被 provider_factory 健康检查/AI 调用使用。现补存盘前 SSRF 校验
    #（空 base_url 允许，兼容“自定义兼容 OpenAI 接口”厂商）。
    if data.base_url:
        url_err = _validate_base_url(data.base_url)
        if url_err:
            raise HTTPException(status_code=400, detail=url_err)
    try:
        data.temperature = min(max(float(data.temperature if data.temperature is not None else 0.7), 0.0), 2.0)
    except (TypeError, ValueError):
        data.temperature = 0.7
    try:
        data.concurrency = min(max(int(data.concurrency or 4), 1), 64)
    except (TypeError, ValueError):
        data.concurrency = 4
    data.max_tokens = min(max(int(data.max_tokens or 4096), 256), 65536)
    try:
        data.timeout = min(max(int(data.timeout or 120), 5), 600)
    except (TypeError, ValueError):
        data.timeout = 120
    # F-1：仅加密「用户新输入的明文」；占位符/空值分支已在上方回填为库内原值。
    data.api_key = _encrypt_fresh_key(data.api_key)
    # PERF（2026-10-05）：写入整段派发 to_thread，全局写锁在 worker 线程阻塞，loop 不受影响
    key_changed = False

    def _write_ai_config():
        nonlocal key_changed
        with get_db_with_retry() as conn:
            # TD-7（2026-10-06）：写前先读旧值，供审计记录 before→after，
            # 排障时能追溯「从哪家厂商切到了哪家」。
            before_row = conn.execute(
                "SELECT provider_name, model, base_url, api_key FROM ai_config WHERE id = 1"
            ).fetchone()
            before = dict(before_row) if before_row else {}
            # 仅在「真实明文密钥」且「与库内已解密值不同」时判定为轮转（避免占位符回写/相同密钥重复保存触发）
            _before_api_key = before.get('api_key') or ''
            _before_plain = decrypt_api_key(_before_api_key) if is_encrypted(_before_api_key) else _before_api_key
            _new_plain = decrypt_api_key(data.api_key) if is_encrypted(data.api_key) else (data.api_key or '')
            if _new_plain and _new_plain != _before_plain:
                key_changed = True
            # document_profile 的「未提供即保留」（P1-DOC）：该字段被 sections.py 的
            # 配图编排消费（industry_profile），但请求模型此前默认 ''，任何一次「只改
            # 温度」的保存都会静默清空用户已填的行业画像。语义：传 None=保留原值，
            # 传 ""=显式清空。由于列是 NOT NULL，不能靠 SQL COALESCE（INSERT 路径会
            # 触发 NOT NULL 约束失败），改为在同一事务内先回读旧值再决定写入值。
            _dp_row = conn.execute("SELECT document_profile FROM ai_config WHERE id = 1").fetchone()
            _dp_current = (_dp_row[0] or '') if _dp_row else ''
            effective_dp = _dp_current if data.document_profile is None else data.document_profile
            conn.execute("INSERT INTO ai_config (id, provider_name, provider, api_key, model, base_url,\n                document_profile, concurrency, temperature, max_tokens, timeout, updated_at)\n               VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now', 'localtime'))\n               ON CONFLICT(id) DO UPDATE SET\n                provider_name=excluded.provider_name, provider=excluded.provider,\n                api_key=excluded.api_key, model=excluded.model,\n                base_url=excluded.base_url,\n                document_profile=excluded.document_profile,\n                concurrency=excluded.concurrency,\n                temperature=excluded.temperature,\n                max_tokens=excluded.max_tokens,\n                timeout=excluded.timeout,\n                updated_at=datetime('now', 'localtime')", (data.provider_name, provider, data.api_key, data.model, data.base_url, effective_dp, data.concurrency, data.temperature, data.max_tokens, data.timeout))
            return before
    before = await asyncio.to_thread(_write_ai_config)
    # L-3：版本号递增（含本进程缓存清除），跨进程 ≤5s 收敛
    bump_ai_config_version()
    new_version = get_ai_config_version()
    # API Key 轮转：写入审计事件 + 记录轮转时间戳(用于 /ai/config/rotation-status 到期提醒)
    if key_changed:
        def _rotation_events():
            try:
                _audit('key_rotation', data.provider_name or provider, 'API key rotated', request)
            except Exception as _e:
                logger.debug('record key_rotation audit 失败: %s', _e)
            try:
                set_key_rotated_at(datetime.now().isoformat())
            except Exception as _e:
                logger.debug('set key_rotated_at 失败: %s', _e)
        await asyncio.to_thread(_rotation_events)
    # F-NEW-4（2026-10-06）：审计写库 + provider_history 写库均为 best-effort
    # 同步 SQLite 操作，此前在事件循环内直接执行会阻塞请求处理；与主配置写
    # 一样统一派发到 worker 线程。两者各自内部已 try/except，不阻断主流程。
    def _post_save_bookkeeping():
        before_desc = (
            f"{before.get('provider_name') or '-'}/{before.get('model') or '-'}"
            if before else "(none)"
        )
        _audit('save_config', data.provider_name or provider,
               f"before={before_desc} "
               f"after={data.provider_name or '-'}/{data.model or '-'} "
               f"key={mask_key_for_audit(data.api_key)}", request)
        record_provider_usage(data.provider_name, data.provider, data.base_url, success=True)
    await asyncio.to_thread(_post_save_bookkeeping)
    return JSONResponse(
        content={'status': 'ok', 'version': new_version},
        headers={'ETag': str(new_version), 'Version': str(new_version)},
    )


@router.get('/config/export')
async def export_ai_config(include_secret: bool = False, request: Request = None):
    """閰嶇疆鍙戣疆鍙峰紩(JSON)。

    include_secret=true 鍒欐垨鍔犲瘑鐨?api_key 鍐欏叆(鍐欑幇鍚庡彲鍦ㄥぇ灏?admin)，
    鍐欏叆鏃?涓嶈祫婧愯幇鍦?403。鏋勯€犳湰寮忎繑鍥炲彲浠?璇锋眰(include_secret=false)鍏ㄥ悗鑷?03。
    """
    from ..services.ai.provider_factory import get_ai_config as _get_config

    cfg = await asyncio.to_thread(_get_config)
    version = get_ai_config_version()
    api_key_raw = cfg.get('api_key') or ''
    export = {
        'format': 'ai-config-v1',
        'exported_at': datetime.now().isoformat(),
        'config_version': version,
        'config': {
            'provider_name': cfg.get('provider_name', ''),
            'provider': cfg.get('provider', ''),
            'model': cfg.get('model', ''),
            'base_url': cfg.get('base_url', ''),
            'document_profile': cfg.get('document_profile', ''),
            'concurrency': cfg.get('concurrency', 4),
            'temperature': cfg.get('temperature', 0.7),
            'max_tokens': cfg.get('max_tokens', 16384),
            'timeout': cfg.get('timeout', 120),
        },
        'api_key_set': bool(api_key_raw),
        'keys_included': False,
    }
    if include_secret:
        _require_admin(request)
        export['config']['api_key'] = api_key_raw  # provider_factory.get_ai_config 已解密(库内)或回退 env
        export['keys_included'] = True
    _audit('export_config', 'ai_config', 'include_secret=%s keys_included=%s' % (include_secret, export['keys_included']), request)
    return export


@router.get('/config/rotation-status')
async def config_rotation_status(request: Request = None):
    """API Key 鏃犵骇鏃ユ湰鏁?澶勭悊(鍙栧嵄绀猴紝 鏃犵骇缁欎簨/鏄?鍙栧嵄)。

    鍐欏叆缁欎簨锛?days_since_rotation(鍒?缁欎簨涓嶅悓鍒?null)、rotation_due(鏄?鍙栧嵄)、
    rotation_interval_days(鍦?AI_KEY_ROTATION_DAYS, 榛樿 90 鏃?)。
    """
    from ..services.ai.provider_factory import get_ai_config as _get_config

    cfg = await asyncio.to_thread(_get_config)
    status = get_key_rotation_status()
    status['api_key_set'] = bool((cfg.get('api_key') or ''))
    return status


@router.get('/config/stream')
async def stream_ai_config(request: Request):
    """SSE: 閰嶇疆鍐欏叆鍚庨€掑鐗堟湰鍙?。

    EventSource 涓嶆敮鎸佽嚜瀹氫箍, 鍏ㄩ€夋嫨鍙傛暟 `?token=` 鍐欏叆(鍚庣灞?Vite 浠ｇ悊),
    灞℃牸 /api/v1/ 鍙傛暟浠?Authorization 鍙傛暟鍐欏叆。
    鍐欑粰鍙?event: config-changed (data: {event, version, ts}) 鍙栧嵄鍐欏叆(2s 蹇?娉?)。
    """

    async def _event_generator():
        last_version = get_ai_config_version()
        while True:
            if await request.is_disconnected():
                break
            version = get_ai_config_version()
            if version != last_version:
                yield 'data: %s\n\n' % json.dumps({'event': 'config-changed', 'version': version, 'ts': time.time()})
                last_version = version
            await asyncio.sleep(2.0)

    return StreamingResponse(
        _event_generator(),
        media_type='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'},
    )


@router.post('/config/clear-api-key')
async def clear_ai_config_api_key(request: Request = None):
    """G-6（2026-10-11）：显式清空主配置 API Key。

    动机：此前若用户想"重置密钥"，唯一入口是 PUT /config 传占位符；
    但占位符回填会把库内**原密文**保留（F-1 加密修复），前端无法区分
    "保持原密钥" 与 "清空密钥"。新增独立端点，前端「重置密钥」按钮直连。

    语义：
      - 只改 api_key（清空为空串），其他字段（provider/model/base_url/…）保留；
      - 版本号递增，跨进程 ≤5s 收敛（与 save 同口径）；
      - 写审计行 action=clear_api_key，方便追溯谁在什么时候清空。

    幂等：主配置行不存在时也不报错（新建库首次调用安全）。
    """
    def _clear_api_key():
        with get_db_with_retry() as conn:
            conn.execute("UPDATE ai_config SET api_key = '' WHERE id = 1")
            conn.commit()
    await asyncio.to_thread(_clear_api_key)
    bump_ai_config_version()
    await asyncio.to_thread(
        lambda: _audit('clear_api_key', 'ai_config', '(main config)', request)
    )
    return {'status': 'ok', 'cleared': True}


class ModelFetchRequest(BaseModel):
    api_key: str
    base_url: str

@router.post('/models')
async def fetch_available_models(data: ModelFetchRequest):
    """调用模型列表端点获取可用模型列表，支持多种 API 格式"""
    url_err = _validate_base_url(data.base_url)
    if url_err:
        raise HTTPException(status_code=400, detail=url_err)
    base = data.base_url.rstrip('/')
    _matched_ver = re.search('/(v\\d+)$', base)
    _original_base = base
    if _matched_ver and _matched_ver.group(1) in ('v1', 'v2', 'v3'):
        base = base[:_matched_ver.start()].rstrip('/')
    _stored_ver = _matched_ver.group(1) if _matched_ver else None
    api_key = _resolve_api_key(data.api_key)
    headers = {'User-Agent': 'BidAssistant/1.0 (AI Config Service; +https://github.com/bid-assistant)'}
    if api_key:
        headers['Authorization'] = f'Bearer {api_key}'
    is_dashscope = 'dashscope' in base.lower()
    is_xfyun_maas = 'xf-yun.com' in base.lower() or 'spark-api' in base.lower()
    _XFYUN_BUILTIN_MODELS = ['spark-5.5-pro', 'spark-5.5-lite', 'spark-5.0-pro', 'spark-5.0-lite', 'spark-4.0-turbo', 'spark-3.5', 'spark-3.0', 'spark-2.0']
    try:
        async with httpx.AsyncClient(timeout=15, **build_httpx_proxy_settings()) as client:
            if is_xfyun_maas:
                models_url = f'{base}/v1/models' if not re.search('/v\\d+', base) else f'{base}/models'
                resp = await client.get(models_url, headers=headers)
                if resp.status_code == 200:
                    body = resp.json()
                    models = _extract_openai_models(body)
                    if models:
                        return {'models': models}
                return {'models': _XFYUN_BUILTIN_MODELS}
            if is_dashscope:
                if '/compatible-mode' in base:
                    models_url = f'{base}/models'
                else:
                    models_url = f'{base}/compatible-mode/v1/models'
                resp = await client.get(models_url, headers=headers)
                if resp.status_code == 200:
                    body = resp.json()
                    models = _extract_openai_models(body)
                    return {'models': models}
                resp = await client.post(f'{base}/api/v1/services/dashscope/dashscope/ListModels', headers={**headers, 'Content-Type': 'application/json'}, params={'Action': 'ListModels'}, json={'page_size': 100, 'action': 'ListModels'})
                if resp.status_code == 200:
                    body = resp.json()
                    models = []
                    for m in body.get('data', {}).get('models', []):
                        mid = (m.get('model_id') or '').strip()
                        if mid:
                            models.append(mid)
                    models.sort()
                    return {'models': models}
                raise HTTPException(status_code=400, detail=f'获取模型列表失败 (HTTP {resp.status_code}): {resp.text[:200]}')
            if '/images/' in base:
                from urllib.parse import urlparse as _urlparse
                _parsed = _urlparse(base)
                models_url = f'{_parsed.scheme}://{_parsed.netloc}/v1/models'
            else:
                models_url = f'{base}/models'
                if not re.search('/v\\d+', base):
                    models_url = f'{base}/v1/models'
            resp = await client.get(models_url, headers=headers)
            if resp.status_code != 200 and '/images/' not in base:
                alt_url = f'{base}/models'
                if alt_url != models_url:
                    resp = await client.get(alt_url, headers=headers)
            if resp.status_code != 200:
                if '<!DOCTYPE' in resp.text or '<html' in resp.text.lower():
                    raise HTTPException(status_code=400, detail={'message': f'API 地址被服务商 WAF 拦截（HTTP {resp.status_code}）。请检查 API Key 是否已正确配置，或尝试切换为国内可用提供商。', 'suggest_alternatives': _get_china_accessible_providers()})
                raise HTTPException(status_code=400, detail=f'获取模型列表失败 (HTTP {resp.status_code}): {resp.text[:200]}')
            body = resp.json()
            models = _extract_openai_models(body)
            return {'models': models}
    except httpx.TimeoutException:
        raise HTTPException(status_code=400, detail={'message': '请求超时，请检查 API 地址是否正确。国内用户建议改用 DeepSeek 或 SiliconFlow。', 'suggest_alternatives': _get_china_accessible_providers()})
    except HTTPException:
        raise
    except Exception as e:
        err_msg = str(e)
        if 'getaddrinfo' in err_msg.lower() or 'name or service not known' in err_msg.lower():
            raise HTTPException(status_code=400, detail={'message': f'无法解析域名（{err_msg}）。请检查网络或 base_url 是否正确。', 'suggest_alternatives': _get_china_accessible_providers()}) from e
        elif 'connect' in err_msg.lower() or 'timeout' in err_msg.lower():
            raise HTTPException(status_code=400, detail={'message': f'连接失败: {err_msg[:120]}。该地址可能被网络拦截，建议改用国内直接可达的提供商。', 'suggest_alternatives': _get_china_accessible_providers()}) from e
        else:
            raise HTTPException(status_code=400, detail={'message': f'连接失败: {err_msg[:200]}', 'suggest_alternatives': _get_china_accessible_providers()}) from e

def _extract_openai_models(body: dict) -> list[str]:
    """从 OpenAI 兼容的 /models 响应中提取模型 ID 列表"""
    models = body.get('data', [])
    result = []
    seen = set()
    for m in models:
        mid = (m.get('id') or '').strip()
        if mid and mid not in seen:
            seen.add(mid)
            result.append(mid)
    result.sort()
    return result

class AIConfigTest(BaseModel):
    provider: str
    api_key: str
    model: str
    base_url: str
    max_tokens: int = 4096
    temperature: float = 0.7

def _get_china_accessible_providers() -> list[dict]:
    """返回国内可直接访问的推荐提供商列表（D-1：正本在 provider_catalog）"""
    return provider_catalog.china_accessible_providers()


def _clamp_test_ai_params(max_tokens, temperature) -> tuple:
    """G-2（2026-10-11）：`POST /test` 的连通性探针参数钳制。

    现状：AIConfigTest 直接落 chat()，max_tokens=1_000_000 / temperature=999
    会打挂厂商侧（400/500）并让「测试连接」返回失败——用户会误判为密钥问题。
    钳制口径与 save_ai_config 对齐（[256, 65536] / [0, 2]），默认探针行为
    （4096 / 0.7）不变。
    """
    try:
        t = min(max(float(temperature if temperature is not None else 0.7), 0.0), 2.0)
    except (TypeError, ValueError):
        t = 0.7
    try:
        m = min(max(int(max_tokens or 4096), 256), 65536)
    except (TypeError, ValueError):
        m = 4096
    return m, t

_DEFAULT_MAX_TOKENS = 4096
_DEFAULT_CONCURRENCY = 4

# HARDENING 2026-10-11（遗留·并发 test 无 rate limit）：
# 进程级信号量限制 `POST /test` 同时在飞探针数。环境变量 AI_TEST_CONCURRENCY_LIMIT
# 可配（默认 5）；`asyncio.Semaphore` 不能跨事件循环复用，但模块级单例 + 锁保证
# 同一进程内只建一次，测试中可 monkeypatch 该函数换新信号量。
_TEST_CONCURRENCY_LIMIT = max(1, int(os.environ.get("AI_TEST_CONCURRENCY_LIMIT", "5")))
_TEST_CONNECTION_SEMAPHORE: asyncio.Semaphore | None = None
_TEST_CONNECTION_SEMAPHORE_LOCK = threading.Lock()


def _get_test_connection_semaphore() -> asyncio.Semaphore:
    """返回进程级 test 并发信号量（惰性初始化，线程安全）。"""
    global _TEST_CONNECTION_SEMAPHORE
    if _TEST_CONNECTION_SEMAPHORE is None:
        with _TEST_CONNECTION_SEMAPHORE_LOCK:
            if _TEST_CONNECTION_SEMAPHORE is None:
                _TEST_CONNECTION_SEMAPHORE = asyncio.Semaphore(_TEST_CONCURRENCY_LIMIT)
    return _TEST_CONNECTION_SEMAPHORE

@router.get('/health')
async def check_ai_health():
    """快速检查当前配置的 AI 服务是否可用

    返回:
        - status: ok | error
        - latency_ms: 响应延迟（毫秒）
        - message: 错误信息（失败时）
        - config: 当前配置（脱敏）
        - suggest_alternatives: 当当前提供商不可用时，推荐国内可用的替代方案
    """
    import time
    from ..services.ai.provider_factory import _clear_ai_config_cache, get_ai_config
    # PERF（2026-10-14）：强制清缓存 + 重新读库都是同步 SQLite 操作，整段落线程，
    # 避免「点一次测试连通性」把整个事件循环冻住。
    def _reload():
        _clear_ai_config_cache()
        return get_ai_config()

    cfg = await asyncio.to_thread(_reload)
    api_key = cfg.get('api_key', '')
    if not api_key:
        return {'status': 'error', 'code': 'NO_API_KEY', 'message': '未配置 AI API Key，请先在设置页面配置', 'config': {'provider': cfg.get('provider'), 'model': cfg.get('model')}, 'suggest_alternatives': _get_china_accessible_providers()}
    if is_encrypted(api_key):
        try:
            api_key = decrypt_api_key(api_key)
        except Exception as e:
            return {'status': 'error', 'code': 'DECRYPT_FAILED', 'message': f'API Key 解密失败: {e}', 'config': {'provider': cfg.get('provider'), 'model': cfg.get('model')}, 'suggest_alternatives': _get_china_accessible_providers()}
    base_url = cfg.get('base_url', '')
    model = cfg.get('model', '')
    if not base_url or not model:
        return {'status': 'error', 'code': 'INCOMPLETE_CONFIG', 'message': 'AI 配置不完整（缺少 base_url 或 model）', 'config': {'provider': cfg.get('provider'), 'model': model}, 'suggest_alternatives': _get_china_accessible_providers()}
    url_err = _validate_base_url(base_url)
    if url_err:
        return {'status': 'error', 'code': 'INVALID_URL', 'message': url_err, 'config': {'provider': cfg.get('provider'), 'model': model, 'base_url': base_url}, 'suggest_alternatives': _get_china_accessible_providers()}
    import httpx
    start = time.time()
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(15, connect=10, read=15), **build_httpx_proxy_settings()) as client:
            resp = await client.post(base_url.rstrip('/') + '/chat/completions', headers={'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'}, json={'model': model, 'messages': [{'role': 'user', 'content': 'hi'}], 'max_tokens': 8, 'temperature': 0.1})
            latency_ms = int((time.time() - start) * 1000)
            if resp.status_code == 200:
                return {'status': 'ok', 'latency_ms': latency_ms, 'message': f'AI 服务可用，延迟 {latency_ms}ms', 'config': {'provider': cfg.get('provider'), 'model': model, 'base_url': base_url}}
            if resp.status_code == 401:
                return {'status': 'error', 'code': 'AUTH_FAILED', 'message': 'API Key 无效（HTTP 401）', 'latency_ms': latency_ms, 'config': {'provider': cfg.get('provider'), 'model': model}}
            if resp.status_code == 429:
                return {'status': 'error', 'code': 'RATE_LIMITED', 'message': 'API 配额已耗尽（HTTP 429）', 'latency_ms': latency_ms, 'config': {'provider': cfg.get('provider'), 'model': model}}
            return {'status': 'error', 'code': f'HTTP_{resp.status_code}', 'message': f'AI 返回 HTTP {resp.status_code}: {resp.text[:200]}', 'latency_ms': latency_ms, 'config': {'provider': cfg.get('provider'), 'model': model}}
    except (httpx.ConnectTimeout, httpx.RemoteProtocolError):
        elapsed = int((time.time() - start) * 1000)
        return {'status': 'error', 'code': 'CONNECT_TIMEOUT', 'message': f'无法连接到 AI 服务（{base_url}），该地址可能被网络防火墙拦截。\n建议切换到国内可直接访问的提供商，点击下方「推荐配置」按钮自动切换。', 'latency_ms': elapsed, 'config': {'provider': cfg.get('provider'), 'model': model, 'base_url': base_url}, 'suggest_alternatives': _get_china_accessible_providers()}
    except httpx.ConnectError as e:
        return {'status': 'error', 'code': 'CONNECT_ERROR', 'message': f'连接 AI 服务失败: {str(e)[:200]}', 'latency_ms': int((time.time() - start) * 1000), 'config': {'provider': cfg.get('provider'), 'model': model, 'base_url': base_url}, 'suggest_alternatives': _get_china_accessible_providers()}
    except httpx.TimeoutException:
        return {'status': 'error', 'code': 'TIMEOUT', 'message': 'AI 服务响应超时（15秒）', 'latency_ms': int((time.time() - start) * 1000), 'config': {'provider': cfg.get('provider'), 'model': model}, 'suggest_alternatives': _get_china_accessible_providers()}
    except Exception as e:
        return {'status': 'error', 'code': 'UNKNOWN', 'message': f'检查失败: {type(e).__name__}: {str(e)[:200]}', 'latency_ms': int((time.time() - start) * 1000), 'config': {'provider': cfg.get('provider'), 'model': model}, 'suggest_alternatives': _get_china_accessible_providers()}

@router.post('/test')
async def test_ai_connection(data: AIConfigTest):
    # HARDENING 2026-10-11（遗留·并发 test 无 rate limit）：多 provider 并发压测
    # 可能触发厂商速率限制。进程级 Semaphore 限制同时在飞探针数（默认 5，
    # 环境变量 AI_TEST_CONCURRENCY_LIMIT 可配），超出排队等待，30s 拿不到槽位返回 429。
    _sem = _get_test_connection_semaphore()
    try:
        await asyncio.wait_for(_sem.acquire(), timeout=30)
    except asyncio.TimeoutError:
        raise HTTPException(
            status_code=429,
            detail=f'测试连接请求过载，请稍后重试（并发上限 {_TEST_CONCURRENCY_LIMIT}）',
        ) from None
    try:
        try:
            from ..services.ai.providers.base import AIMessage
            if not data.model:
                from ..services.ai.provider_factory import _AI_CONFIG_DEFAULTS
                data.model = _AI_CONFIG_DEFAULTS.get('model', 'gpt-4o')
            # G-2（2026-10-11）：探针参数钳制 + base_url SSRF 校验前置。
            # 此前：AIConfigTest 直接落 chat()，max_tokens=1_000_000 会打挂厂商侧
            # 让"测试连接"报失败（用户误判为密钥问题）；base_url 传 file:// 之类
            # 危险协议可被上游 httpx 意外跟随。
            data.max_tokens, data.temperature = _clamp_test_ai_params(data.max_tokens, data.temperature)
            url_err = _validate_base_url(data.base_url)
            if url_err:
                raise HTTPException(status_code=400, detail=url_err)
            # 空 base_url 兜底（兼容"仅填模型/密钥"）——回落到厂商默认 base_url，
            # 否则 create_provider 会用空 base_url 直接 400。
            if not data.base_url:
                from ..services.ai.provider_catalog import find_default_base_url_for_provider
                default_base = find_default_base_url_for_provider(data.provider) or ''
                if not default_base:
                    raise HTTPException(status_code=400, detail='base_url 不能为空')
                data.base_url = default_base
            config = {'provider': data.provider, 'api_key': _resolve_api_key(data.api_key), 'model': data.model, 'base_url': data.base_url, 'max_tokens': data.max_tokens, 'temperature': data.temperature}
            provider = _create_shared_provider(config)
            result = await provider.chat(messages=[AIMessage(role='user', content='回复OK即可')], max_tokens=256, temperature=0.1)
            record_provider_usage(data.provider, data.provider, data.base_url, success=True)
            return {'status': 'ok', 'message': f'连接成功: {result[:50]}'}
        except Exception as e:
            record_provider_usage(data.provider, data.provider, data.base_url, success=False)
            raise HTTPException(status_code=400, detail=f'连接失败: {e!s}') from e
    finally:
        _sem.release()

class QuickSetupRequest(BaseModel):
    """快速配置请求：提供 API Key，自动检测可用提供商"""
    api_key: str
    provider_name: str = ''
    model: str = ''

class QuickSetupResult(BaseModel):
    """快速配置结果"""
    success: bool
    message: str
    applied_config: dict | None = None
    tested_providers: list[dict] = []

@router.post('/quick-setup', response_model=QuickSetupResult)
async def quick_setup(data: QuickSetupRequest, request: Request = None):
    """快速配置：测试指定 API Key 在国内可访问的提供商，自动选择最优配置

    工作流程：
    1. 测试列出的国内可用提供商
    2. 返回可达且认证通过的提供商
    3. 如果指定了 provider_name，优先测试该提供商
    """
    import time
    providers_to_test = _get_china_accessible_providers()
    if data.provider_name:
        providers_to_test = [p for p in providers_to_test if p['name'] == data.provider_name]
        if not providers_to_test:
            # BUG-FIX 2026-09-17：此处原本在**已被清空**的列表上再次过滤，
            # 结果恒为空 —— 只要 provider_name 不是精确展示名，就一律 400
            # 「未知的提供商」，base_url 兜底分支从未生效过。
            providers_to_test = [p for p in _get_china_accessible_providers() if data.provider_name.lower() in p['base_url'].lower()]
        if not providers_to_test:
            raise HTTPException(status_code=400, detail=f"未知的提供商: {data.provider_name}，支持的国内提供商: {[p['name'] for p in _get_china_accessible_providers()]}")
    results = []
    for provider in providers_to_test:
        start = time.time()
        base_url = provider['base_url']
        provider_type = provider['type']  # F-5：下方落库改用 best 自身的 type/model，此变量仅为循环内可读性
        model = data.model or (provider['models'][0] if provider.get('models') else 'default')
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(10, connect=5, read=10), **build_httpx_proxy_settings()) as client:
                resp = await client.post(base_url.rstrip('/') + '/chat/completions', headers={'Authorization': f'Bearer {data.api_key}', 'Content-Type': 'application/json'}, json={'model': model, 'messages': [{'role': 'user', 'content': 'hi'}], 'max_tokens': 4, 'temperature': 0.1})
                elapsed_ms = int((time.time() - start) * 1000)
                if resp.status_code == 200:
                    results.append({**provider, 'test_result': 'ok', 'latency_ms': elapsed_ms, 'tested_model': model})
                elif resp.status_code == 401:
                    results.append({**provider, 'test_result': 'auth_failed', 'latency_ms': elapsed_ms, 'tested_model': model})
                else:
                    results.append({**provider, 'test_result': f'http_{resp.status_code}', 'latency_ms': elapsed_ms, 'tested_model': model})
        except Exception as e:
            elapsed_ms = int((time.time() - start) * 1000)
            results.append({**provider, 'test_result': f'error: {type(e).__name__}', 'latency_ms': elapsed_ms, 'tested_model': model})
    working = [r for r in results if r['test_result'] == 'ok']
    working.sort(key=lambda x: x['latency_ms'])
    if working:
        best = working[0]
        # F-5 BUG修复：provider_type/model 此前取的是**循环残留变量**（最后一个被
        # 测试的厂商），当最优结果不是最后一个时会写错路由类型（如 best=DeepSeek
        # 但 provider_type=openai_compatible），导致 provider 实例选型错误。
        # F-1：明文密钥统一加密落库。
        # PERF（2026-10-05）：写入整段派发 to_thread，全局写锁在 worker 线程阻塞，loop 不受影响
        def _write_quick_setup():
            with get_db_with_retry() as conn:
                conn.execute("INSERT INTO ai_config (id, provider_name, provider, api_key, model, base_url,\n                    document_profile, concurrency, temperature, max_tokens, updated_at)\n                   VALUES (1, ?, ?, ?, ?, ?, '', 4, 0.7, 4096, datetime('now', 'localtime'))\n                   ON CONFLICT(id) DO UPDATE SET\n                    provider_name=excluded.provider_name, provider=excluded.provider,\n                    api_key=excluded.api_key, model=excluded.model,\n                    base_url=excluded.base_url,\n                    updated_at=datetime('now', 'localtime')", (best['name'], best['type'], _encrypt_fresh_key(data.api_key), best['tested_model'], best['base_url']))
        await asyncio.to_thread(_write_quick_setup)
        bump_ai_config_version()
        # F-NEW-4：审计 + 用量记录派发到线程，避免阻塞事件循环
        def _post_quick_bookkeeping():
            _audit('quick_setup', best['name'],
                   f"model={best['tested_model']} key={mask_key_for_audit(data.api_key)}", request)
            record_provider_usage(best['name'], best['type'], best['base_url'], success=True)
        await asyncio.to_thread(_post_quick_bookkeeping)
        # BUG-FIX 2026-09-17（安全 P0）：旧实现把用户刚输入的**明文 API Key**
        # 原样放进 applied_config 随 HTTP 响应回传（可被引擎/代理/日志留存）。
        # 只回传「是否已设置」，不回传密钥本身。
        # L-1b（2026-10-04）：applied_config.provider 此前取的是**循环残留变量**
        # provider_type（F-5 只修了落库侧，响应侧漏了）—— 前端展示的路由类型
        # 可能与实际落库不符。改用 best['type'] 与库内一致。
        applied = {'provider_name': best['name'], 'provider': best['type'], 'base_url': best['base_url'], 'model': best['tested_model'], 'api_key_set': bool(data.api_key)}
        return QuickSetupResult(success=True, message=f"检测到 {len(working)} 个可用提供商，已推荐延迟最低的「{best['name']}」", applied_config=applied, tested_providers=results)
    auth_passed = [r for r in results if r['test_result'] == 'auth_failed']
    if auth_passed:
        return QuickSetupResult(success=False, message='API Key 无效或未授权，请检查 Key 是否正确', tested_providers=results)
    return QuickSetupResult(success=False, message=f'所有 {len(results)} 个国内提供商均无法连接，请检查网络或代理设置', tested_providers=results)

class ImageModelConfigRequest(BaseModel):
    """图像模型配置请求体"""
    provider: str = 'agnes'
    model: str = 'agnes-image-2.1-flash'
    base_url: str = ''
    api_key: str = ''
    max_concurrency: int = 2
    max_per_project: int = 10
    quality: str = 'standard'
    style_injection: str = ''
    enabled: bool = True
    default_size: str = '1024x1024'
    default_ratio: str = '1:1'
    price_per_image: float = 0.0

class ImageModelConfigResponse(BaseModel):
    """图像模型配置响应体"""
    provider: str
    model: str
    base_url: str
    api_key: str
    max_concurrency: int
    max_per_project: int
    quality: str
    style_injection: str
    enabled: bool
    default_size: str
    default_ratio: str
    price_per_image: float
    is_default: bool

@router.get('/image-model/providers')
async def list_image_providers():
    """获取支持的图像生成平台列表"""
    providers = []
    for pid in ProviderRegistry.list_providers():
        if pid == 'agnes':
            display_name = 'Agnes AI（新加坡）'
            price = '免费'
            is_default = True
        elif pid == 'dashscope':
            display_name = '阿里云百炼（国内）'
            price = '¥0.50/张'
            is_default = False
        elif pid == 'volcengine':
            display_name = '火山引擎（国内）'
            price = '¥0.22/张'
            is_default = False
        elif pid == 'hunyuan':
            display_name = '腾讯混元（国内）'
            price = '¥0.20/张'
            is_default = False
        elif pid == 'zhipu':
            display_name = '智谱AI（国内）'
            price = '¥0.15~0.30/张'
            is_default = False
        elif pid == 'stepfun':
            display_name = '阶跃星辰（国内）'
            price = '¥0.02~0.10/张'
            is_default = False
        elif pid == 'baidu':
            display_name = '百度文心一格（国内）'
            price = '¥0.06~0.50/张'
            is_default = False
        else:
            display_name = pid.capitalize()
            price = '待定'
            is_default = False
        providers.append({'id': pid, 'name': display_name, 'price': price, 'is_default': is_default})
    return providers

@router.get('/image-model/styles')
async def list_image_styles():
    """获取可用的 AI 生图风格列表

    ENHANCE 2026-10-05（P-16）：风格文案此前硬编码在 3 处（image_providers/service.py、
    image_engine.py 两处），现已收敛到 services/ai/image_styles.py 作为唯一正本。
    支持通过 IMAGE_STYLE_OVERRIDE 环境变量追加/覆盖风格，本接口会一并返回。
    """
    return list_image_styles()


@router.get('/cache-metrics')
async def get_cache_metrics():
    """获取缓存命中率监控（ENHANCE 2026-10-05 / P-23）

    对齐易标规格九「缓存命中监控」——分层提示词的价值要靠命中率验证：

      - ``prompt_cache``：Anthropic prompt cache 的 token 级统计
        （``cache_read_input_tokens`` / ``cache_creation_input_tokens``），
        命中率低说明稳定前缀被反复重写、分层没生效。
      - ``stores``：各缓存组件（section_content / image / export）的
        命中/未命中次数与命中率。

    计数为进程内统计，重启归零。
    """
    return snapshot()


@router.get('/image-model')
async def get_image_model_config(user_id: int=Depends(lambda: 1)):
    """获取用户的图像模型配置"""
    try:
        with get_db_with_retry(readonly=True) as conn:
            row = conn.execute('\n                SELECT image_provider, image_model, image_base_url, image_api_key_enc,\n                       image_max_concurrency, image_max_per_project, image_quality,\n                       image_style_injection, image_enabled, image_default_size, image_default_ratio, image_price_per_image\n                FROM model_configs WHERE user_id = ?\n            ', [user_id]).fetchone()
            if row:
                # BUG-FIX（安全 P0）：图像模型密钥同样只回传脱敏占位符；
                # `PUT /ai/image-model` 已支持 API_KEY_MASK 回填原值。
                display_key = _display_api_key(row[3] or '')
                return ImageModelConfigResponse(provider=row[0] or 'agnes', model=row[1] or 'agnes-image-2.1-flash', base_url=_normalize_image_base_url(row[2] or 'https://api.agnes-ai.cn/v1'), api_key=display_key, max_concurrency=row[4] or 2, max_per_project=row[5] or 10, quality=row[6] or 'standard', style_injection=row[7] or '', enabled=bool(row[8]) if row[8] is not None else True, default_size=row[9] or '1024x1024', default_ratio=row[10] or '1:1', price_per_image=row[11] or 0.0, is_default=False)
            else:
                return ImageModelConfigResponse(provider='agnes', model='agnes-image-2.1-flash', base_url='https://api.agnes-ai.cn/v1', api_key='', max_concurrency=2, max_per_project=10, quality='standard', style_injection='', enabled=True, default_size='1024x1024', default_ratio='1:1', price_per_image=0.0, is_default=True)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f'获取配置失败: {e!s}') from e

@router.put('/image-model')
async def save_image_model_config(config: ImageModelConfigRequest, user_id: int=Depends(lambda: 1)):
    """保存用户图像模型配置"""
    try:
        api_key_to_save = config.api_key
        # BUG-FIX 2026-09-17：两点修复
        # 1) 只认 `== API_KEY_MASK` 精确相等，前端回显的其他掩码形态漏网；
        # 2) 读取已保存密钥失败时旧实现 `pass` 静默放过，
        #    于是占位符 `***已配置***` 被当作真实密钥落库 → 图像生成全部 401。
        if _is_masked_api_key(config.api_key):
            api_key_to_save = ''
            # PERF（2026-10-05）：纯读走 readonly + to_thread，不在事件循环内做同步 SQLite I/O
            def _read_stored_image_key():
                with get_db_with_retry(readonly=True) as conn:
                    row = conn.execute('SELECT image_api_key_enc FROM model_configs WHERE user_id = ?', [user_id]).fetchone()
                return row[0] if row else None
            try:
                stored = await asyncio.to_thread(_read_stored_image_key)
                if stored:
                    # S-3（2026-10-06）：回填库内**原值**（ENC: 密文/历史明文），
                    # 不再解密为明文；下方统一经 _encrypt_fresh_key 收口后落库。
                    api_key_to_save = stored
            except Exception as _e:
                logger.warning('读取已保存的图像 API Key 失败: %s', _e)
            if not api_key_to_save:
                raise HTTPException(status_code=400, detail='首次配置请提供真实的 API Key，不能使用占位符')
        config.base_url = _normalize_image_base_url(config.base_url)
        # S-3（2026-10-06）：新明文密钥 ENC: 加密落库；占位符回填的库内原值
        # （密文/历史明文）幂等透传，与文本配置写路径口径一致。
        api_key_to_save = _encrypt_fresh_key(api_key_to_save)
        # PERF（2026-10-05）：写入整段派发 to_thread，全局写锁在 worker 线程阻塞，loop 不受影响
        def _write_image_model_config():
            with get_db_with_retry() as conn:
                current = conn.execute('\n                SELECT 1 FROM model_configs WHERE user_id = ?\n            ', [user_id]).fetchone()
                if current:
                    conn.execute('\n                    UPDATE model_configs SET\n                        image_provider = ?,\n                        image_model = ?,\n                        image_base_url = ?,\n                        image_api_key_enc = ?,\n                        image_max_concurrency = ?,\n                        image_max_per_project = ?,\n                        image_quality = ?,\n                        image_style_injection = ?,\n                        image_enabled = ?,\n                        image_default_size = ?,\n                        image_default_ratio = ?,\n                        image_price_per_image = ?\n                    WHERE user_id = ?\n                ', (config.provider, config.model, config.base_url, api_key_to_save, config.max_concurrency, config.max_per_project, config.quality, config.style_injection, config.enabled, config.default_size, config.default_ratio, config.price_per_image, user_id))
                else:
                    conn.execute('\n                    INSERT INTO model_configs (\n                        user_id, image_provider, image_model, image_base_url, image_api_key_enc,\n                        image_max_concurrency, image_max_per_project, image_quality,\n                        image_style_injection, image_enabled, image_default_size, image_default_ratio, image_price_per_image\n                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)\n                ', (user_id, config.provider, config.model, config.base_url, api_key_to_save, config.max_concurrency, config.max_per_project, config.quality, config.style_injection, config.enabled, config.default_size, config.default_ratio, config.price_per_image))
                conn.commit()
        await asyncio.to_thread(_write_image_model_config)
        return {'status': 'ok'}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f'保存配置失败: {e!s}') from e

@router.post('/image-model/test')
async def test_image_connection(config: ImageModelConfigRequest):
    """测试图像模型连通性"""
    try:
        adapter_class = ProviderRegistry._adapters.get(config.provider)
        if not adapter_class:
            raise HTTPException(status_code=400, detail=f'未知的图像平台: {config.provider}')
        api_key = _resolve_image_api_key(config.provider, config.api_key)
        if config.api_key and not api_key:
            raise HTTPException(status_code=400, detail='未找到该厂商已保存的 API Key，请先填写并保存后再测试')
        config.base_url = _normalize_image_base_url(config.base_url)
        temp_adapter = adapter_class({'base_url': config.base_url or 'https://api.agnes-ai.cn/v1', 'api_key': api_key or ''})
        connected = await temp_adapter.test_connection(api_key=api_key or '', base_url=config.base_url or temp_adapter.base_url)
        if connected:
            return {'status': 'ok', 'message': f'连接到 {config.provider} 成功'}
        else:
            raise HTTPException(status_code=400, detail=f'连接到 {config.provider} 失败')
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f'连接测试失败: {e!s}') from e

@router.get('/image-model/usage')
async def get_image_usage_stats(user_id: int=Depends(lambda: 1)):
    """获取图像生成用量统计"""
    try:
        with get_db(readonly=True) as conn:
            # BUG-FIX 2026-09-17：total 限定 `project_id IS NOT NULL`，
            # 而 success 统计全表 —— 分母小于分子，成功率会算出 >100% 的荒谬值。
            # 统一为全表口径，并补 failed_count 供前端展示。
            total = conn.execute('SELECT COUNT(*) FROM image_generation_tasks').fetchone()[0]
            monthly = conn.execute("\n                SELECT COUNT(*) FROM image_generation_tasks \n                WHERE created_at >= datetime('now', 'start of month')\n            ").fetchone()[0]
            success = conn.execute("\n                SELECT COUNT(*) FROM image_generation_tasks \n                WHERE status = 'success'\n            ").fetchone()[0]
            failed = conn.execute("\n                SELECT COUNT(*) FROM image_generation_tasks \n                WHERE status = 'failed'\n            ").fetchone()[0]
            total_cost = conn.execute('SELECT COALESCE(SUM(cost), 0) FROM image_generation_tasks').fetchone()[0]
            platform_dist = {}
            for row in conn.execute('\n                SELECT provider, COUNT(*) FROM image_generation_tasks \n                GROUP BY provider\n            '):
                platform_dist[row[0]] = row[1]
            return {'total': total, 'monthly': monthly, 'success_count': success, 'failed_count': failed, 'success_rate': round(success / total * 100, 1) if total > 0 else 0, 'total_cost': round(total_cost, 2), 'platform_distribution': platform_dist}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f'获取统计数据失败: {e!s}') from e

class ImageQuickSetupRequest(BaseModel):
    api_key: str
    provider_name: str = ''

class ImageQuickSetupResult(BaseModel):
    success: bool
    message: str
    applied_config: dict | None = None
    tested_providers: list[dict] = []
_IMAGE_FALLBACK_PROVIDERS = [{'name': '百度文心一格', 'provider': 'baidu', 'base_url': 'https://aip.baidubce.com/rpc/2.0/ai_custom', 'models': ['wanx-v1']}, {'name': '字节火山引擎', 'provider': 'volcengine', 'base_url': 'https://open.volcengineapi.com', 'models': ['doubao-image']}, {'name': '阿里云通义', 'provider': 'dashscope', 'base_url': 'https://dashscope.aliyuncs.com/compatible-mode/v1', 'models': ['wanx-v1']}, {'name': '腾讯混元', 'provider': 'hunyuan', 'base_url': 'https://api.hunyuan.cloud.tencent.com/v1', 'models': ['hunyuan-image']}, {'name': '阶跃星辰', 'provider': 'stepfun', 'base_url': 'https://api.stepfun.com/v1', 'models': ['step-image']}, {'name': '智谱AI', 'provider': 'zhipu', 'base_url': 'https://open.bigmodel.cn/api/paas/v4', 'models': ['cogview-4']}, {'name': 'Agnes 内置', 'provider': 'agnes', 'base_url': 'https://api.agnes-ai.cn/v1', 'models': ['agnes-image-2.1-flash']}]

@router.post('/image-model/quick-setup', response_model=ImageQuickSetupResult)
async def quick_setup_image(data: ImageQuickSetupRequest):
    """快速配置图像模型：测试 API Key 并自动选择国内可用的最佳提供商"""
    import time
    results = []
    providers_to_test = _IMAGE_FALLBACK_PROVIDERS
    if data.provider_name:
        providers_to_test = [p for p in _IMAGE_FALLBACK_PROVIDERS if p['name'] == data.provider_name]
        if not providers_to_test:
            providers_to_test = [p for p in _IMAGE_FALLBACK_PROVIDERS if data.provider_name.lower() in p['provider'].lower()]
    for provider in providers_to_test:
        start = time.time()
        try:
            from ..services.ai.image_providers.registry import ProviderRegistry
            adapter_class = ProviderRegistry._adapters.get(provider['provider'])
            if not adapter_class:
                results.append({**provider, 'test_result': 'not_registered'})
                continue
            adapter = adapter_class({'base_url': provider['base_url'], 'api_key': data.api_key})
            connected = await adapter.test_connection(api_key=data.api_key, base_url=provider['base_url'])
            elapsed = int((time.time() - start) * 1000)
            if connected:
                results.append({**provider, 'test_result': 'ok', 'latency_ms': elapsed})
            else:
                results.append({**provider, 'test_result': 'connect_failed', 'latency_ms': elapsed})
        except Exception as e:
            elapsed = int((time.time() - start) * 1000)
            results.append({**provider, 'test_result': f'error: {type(e).__name__}', 'latency_ms': elapsed})
    working = [r for r in results if r['test_result'] == 'ok']
    working.sort(key=lambda x: x['latency_ms'])
    if working:
        best = working[0]
        return ImageQuickSetupResult(success=True, message=f"检测到 {len(working)} 个可用图像提供商，已推荐延迟最低的「{best['name']}」", applied_config={'provider': best['provider'], 'base_url': best['base_url'], 'model': best['models'][0]}, tested_providers=results)
    auth_failed = [r for r in results if r['test_result'] == 'auth_failed']
    if auth_failed:
        return ImageQuickSetupResult(success=False, message='API Key 无效，请检查 Key 是否正确', tested_providers=results)
    return ImageQuickSetupResult(success=False, message=f'所有 {len(results)} 个图像提供商均无法连接，请检查网络', tested_providers=results)

class ProviderHistoryItem(BaseModel):
    provider_name: str
    provider_type: str
    base_url: str
    success: int
    failed: int
    last_used_at: str
    success_rate: float

class ProviderHistoryList(BaseModel):
    providers: list[ProviderHistoryItem]
    total: int

@router.get('/provider-history', response_model=ProviderHistoryList)
async def get_provider_history():
    """获取 Provider 降级链（主配置优先 → 历史记录 → 内置兜底）

    返回按优先级排序的 provider 列表：
      1. 当前 ai_config 中保存的主配置（第1优先）
      2. provider_history 中记录的最近配置厂商（第2优先）
      3. 内置兜底提供商（第3优先）
    """
    # PERF（2026-10-14）：get_provider_priority_order 内部走 get_ai_config()，
    # 会做同步 SQLite 读；整段移出事件循环。
    priority_list = await asyncio.to_thread(get_provider_priority_order)
    items = []
    for entry in priority_list:
        is_builtin = entry.get('is_builtin_default', False)
        # L-2 配套（2026-10-04）：降级链条目的 success/failed 本质是由 success_rate
        # 派生的**展示占位**（100/0 或按比率），与历史表里 record_usage 的真实
        # 计数是两套口径（既有设计）。flag 修正后（仅 agnes 为内置默认），
        # 非 flag 条目改用自身 success_rate 派生，与旧副本展示语义一致。
        _sr = float(entry.get('success_rate', 1.0) or 0.0)
        success_val = 100 if is_builtin else int(_sr * 100)
        failed_val = 0 if is_builtin else int((1 - _sr) * 100)
        items.append(ProviderHistoryItem(provider_name=entry['name'], provider_type=entry.get('provider', 'openai_compatible'), base_url=entry['base_url'], success=success_val, failed=failed_val, last_used_at=entry.get('last_used_at', ''), success_rate=float(entry.get('success_rate', 1.0 if is_builtin else 0)) if is_builtin else float(entry.get('success_rate', 0))))
    return ProviderHistoryList(providers=items, total=len(items))

@router.delete('/provider-history')
async def clear_provider_history():
    """清除 Provider 使用历史记录（重置降级优先级）"""
    try:
        # PERF（2026-10-05）：删除整段派发 to_thread，全局写锁在 worker 线程阻塞，loop 不受影响
        def _clear_history():
            with get_db_with_retry() as conn:
                conn.execute('DELETE FROM provider_history')
                conn.commit()
        await asyncio.to_thread(_clear_history)
        # L-3：降级链组成变化，同样递增版本号
        bump_ai_config_version()
        return {'status': 'ok', 'message': '历史记录已清除'}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e

@router.get('/config-audit')
async def get_config_audit(limit: int = 50, action: str | None = None, target: str | None = None):
    """L-5（2026-10-04 遗留闭环）：配置变更审计日志查询。

    仅返回 who/action/target/脱敏摘要；detail 写入时已不含密钥原文，
    此处再防御一层（任何尾号信息仅对已认证用户可见，路由级
    Depends(get_current_user) 已覆盖本 router）。

    G-5（2026-10-11）：新增 ``action`` / ``target`` 过滤参数。
    此前只能按时间倒序翻，前端要区分「保存配置 / 激活 / 应用 Profile」
    必须拉全量再本地过滤；运维在 500 条截断前无法看到历史事件。
    过滤语义：``action`` 精确匹配；``target`` 前缀匹配（LIKE 'target%'），
    便于按厂商/Profile 名过滤。

    PERF（2026-10-14）：整段读移出事件循环（本文件既有 PERF 口径的延续）。
    """
    limit = min(max(int(limit or 50), 1), 500)
    clauses = []
    params: list = []
    if action:
        clauses.append('action = ?')
        params.append(action)
    if target:
        clauses.append('target LIKE ?')
        params.append(target + '%')
    where = (' WHERE ' + ' AND '.join(clauses)) if clauses else ''
    params.append(limit)

    def _read_audit():
        with get_db_with_retry(readonly=True) as conn:
            return conn.execute(
                f' SELECT actor, action, target, detail, created_at FROM ai_config_audit{where} ORDER BY id DESC LIMIT ? ',
                params,
            ).fetchall()

    rows = await asyncio.to_thread(_read_audit)
    items = []
    for r in rows:
        d = dict(r) if hasattr(r, 'keys') else dict(zip(['actor', 'action', 'target', 'detail', 'created_at'], r, strict=False))
        items.append(d)
    # P0-CONTRACT（2026-10-14）：返回体固定为 {items, total}（文档化契约）。
    # 前端 `getConfigAudit` 曾按 `Array<...>` 声明并直接 `res.data` 塞进 antd Table
    # 的 dataSource —— 形状不匹配导致审计表格页运行时崩溃（对象无 .map）。此处补
    # `items` 为空时也保证是 list，调用方 `res.data.items ?? []` 即可安全消费。
    return {'items': items, 'total': len(items)}

class ProfileCreateRequest(BaseModel):
    name: str
    display_name: str = ''
    description: str = ''
    provider_name: str = 'agnes'
    provider: str = 'openai_compatible'
    api_key: str = ''
    model: str = 'gpt-4o'
    base_url: str = ''
    max_tokens: int = 16384
    temperature: float = 0.7
    concurrency: int = 4
    timeout: int = 120
    json_mode: bool = False
    task_type: str = ''
    rate_limit_per_min: int = 0
    token_budget_daily: int = 0
    is_default: bool = False

class ProfileUpdateRequest(BaseModel):
    display_name: str | None = None
    description: str | None = None
    provider_name: str | None = None
    provider: str | None = None
    api_key: str | None = None
    model: str | None = None
    base_url: str | None = None
    max_tokens: int | None = None
    temperature: float | None = None
    concurrency: int | None = None
    timeout: int | None = None
    json_mode: bool | None = None
    task_type: str | None = None
    rate_limit_per_min: int | None = None
    token_budget_daily: int | None = None
    is_default: bool | None = None

class ProfileResponse(BaseModel):
    id: int
    name: str
    display_name: str
    description: str
    provider_name: str
    provider: str
    api_key: str
    model: str
    base_url: str
    max_tokens: int
    temperature: float
    concurrency: int
    timeout: int
    json_mode: bool
    task_type: str
    rate_limit_per_min: int
    token_budget_daily: int
    is_active: bool
    is_default: bool
    created_at: str
    updated_at: str

@router.get('/profiles')
async def list_profiles():
    """获取所有 AI Profile 列表（不含 api_key）"""
    from ..services.ai.profile_service import list_profiles as _list
    # PERF（2026-10-14）：同步 SQLite 读移出事件循环
    profiles = await asyncio.to_thread(_list, active_only=True)
    config_version = get_ai_config_version()
    result = []
    for p in profiles:
        result.append({'id': p['id'], 'name': p['name'], 'display_name': p.get('display_name', p['name']), 'description': p.get('description', ''), 'provider_name': p.get('provider_name', 'agnes'), 'provider': p.get('provider', 'openai_compatible'), 'model': p.get('model', 'gpt-4o'), 'base_url': p.get('base_url', ''), 'max_tokens': p.get('max_tokens', 16384), 'temperature': p.get('temperature', 0.7), 'concurrency': p.get('concurrency', 4), 'timeout': p.get('timeout', 120), 'json_mode': bool(p.get('json_mode', 0)), 'task_type': p.get('task_type', ''), 'rate_limit_per_min': p.get('rate_limit_per_min', 0), 'token_budget_daily': p.get('token_budget_daily', 0), 'is_active': bool(p.get('is_active', 1)), 'is_default': bool(p.get('is_default', 0)), 'created_at': p.get('created_at', ''), 'updated_at': p.get('updated_at', ''), 'version': config_version, 'etag': str(config_version)})
    return result

@router.get('/profiles/default')
async def get_default_profile():
    """获取当前默认/激活的 Profile；不存在时返回空占位，不抛 404"""
    from ..services.ai.profile_service import get_default_profile as _get_default
    profile = await asyncio.to_thread(_get_default)
    config_version = get_ai_config_version()
    if not profile:
        return {'name': 'default', 'display_name': '默认配置', 'description': '', 'provider_name': None, 'provider': None, 'api_key': '', 'model': None, 'base_url': None, 'max_tokens': None, 'temperature': None, 'concurrency': None, 'timeout': None, 'is_active': False, 'is_default': True, 'json_mode': False, 'version': config_version, 'etag': str(config_version)}
    return {**profile, 'is_default': True, 'is_active': bool(profile.get('is_active', 1)), 'json_mode': bool(profile.get('json_mode', 0)), 'version': config_version, 'etag': str(config_version)}

@router.get('/profiles/export')
async def export_profiles():
    """导出所有 Profile（JSON 格式，不含密钥，仅标注 api_key_set）"""
    from ..services.ai.profile_service import export_profiles as _export
    return await asyncio.to_thread(_export)

@router.get('/profiles/{profile_name}')
async def get_profile(profile_name: str):
    """获取单个 Profile 详情（含解密后的 api_key）"""
    from ..services.ai.profile_service import get_profile as _get
    profile = await asyncio.to_thread(_get, profile_name)
    if not profile:
        raise HTTPException(status_code=404, detail=f"Profile '{profile_name}' 不存在")
    config_version = get_ai_config_version()
    return {**profile, 'is_default': bool(profile.get('is_default', 0)), 'is_active': bool(profile.get('is_active', 1)), 'json_mode': bool(profile.get('json_mode', 0)), 'version': config_version, 'etag': str(config_version)}

@router.post('/profiles', response_model=ProfileResponse)
async def create_profile(data: ProfileCreateRequest):
    """创建新 AI Profile"""
    from ..services.ai.profile_service import create_profile as _create
    try:
        profile = _create(data.model_dump())
        return {**profile, 'is_default': bool(profile.get('is_default', 0)), 'is_active': True, 'json_mode': bool(profile.get('json_mode', 0))}
    except ValueError as e:
        # HARDENING 2026-10-11（遗留·命名冲突未映射 409）：同名 Profile 冲突
        # 原实现一律 400；语义上属资源已存在，应返回 409 Conflict。
        if "已存在" in str(e):
            raise HTTPException(status_code=409, detail=str(e)) from e
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e

@router.put('/profiles/{profile_name}')
async def update_profile(profile_name: str, data: ProfileUpdateRequest, request: Request = None):
    """更新 AI Profile（可选 If-Match/etag 乐观锁；与 ai_config 共享版本号，跨模块并发写互斥）"""
    from ..services.ai.profile_service import update_profile as _update, ConflictError
    if_match = request.headers.get('if-match') if request else None
    if_match_version = None
    if if_match is not None:
        try:
            if_match_version = int(if_match.strip().strip('"'))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail='If-Match 请求头值非法')
    try:
        update_data = data.model_dump(exclude_none=True)
        profile = _update(profile_name, update_data, if_match=if_match_version)
        new_version = get_ai_config_version()
        return {**profile, 'version': new_version, 'etag': str(new_version), 'is_default': bool(profile.get('is_default', 0)), 'is_active': bool(profile.get('is_active', 1)), 'json_mode': bool(profile.get('json_mode', 0))}
    except ConflictError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e

@router.delete('/profiles/{profile_name}')
async def delete_profile(profile_name: str):
    """删除 AI Profile"""
    from ..services.ai.profile_service import delete_profile as _delete
    if not _delete(profile_name):
        raise HTTPException(status_code=404, detail=f"Profile '{profile_name}' 不存在")
    return {'status': 'ok'}

@router.post('/profiles/{profile_name}/set-default')
async def set_default_profile(profile_name: str):
    """将指定 Profile 设为默认"""
    from ..services.ai.profile_service import set_default_profile as _set_default
    try:
        profile = _set_default(profile_name)
        if not profile:
            raise HTTPException(status_code=404, detail=f"Profile '{profile_name}' 不存在")
        return {'status': 'ok', 'profile': profile['name']}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

@router.post('/profiles/{profile_name}/apply')
async def apply_profile(profile_name: str, request: Request = None):
    """将 Profile 配置应用到当前 AI 主配置"""
    from ..services.ai.profile_service import apply_profile_to_config
    from ..services.ai.profile_service import get_profile as _get
    if not _get(profile_name):
        raise HTTPException(status_code=404, detail=f"Profile '{profile_name}' 不存在")
    try:
        # 传 name 而非 dict：由服务层自行读取真实密钥（详见 profile_service 注释）
        result = apply_profile_to_config(profile_name)
        bump_ai_config_version()
        _audit('apply_profile', profile_name, request=request)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e

@router.post('/profiles/import')
async def import_profiles(data: dict):
    """导入 Profile（JSON 格式）"""
    from ..services.ai.profile_service import import_profiles as _import
    try:
        result = _import(data)
        return {'status': 'ok', **result}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e

@router.get('/usage/summary')
async def get_usage_summary(days: int=30, task_type: str | None=None, profile: str | None=None):
    """获取 AI 调用用量汇总"""
    from ..services.ai.usage_stats import get_usage_summary as _get
    return await asyncio.to_thread(_get, days=days, task_type=task_type, profile_name=profile)

@router.get('/usage/dashboard')
async def get_usage_dashboard():
    """获取用量仪表盘：汇总 + 趋势 + 预算状态"""
    from ..services.ai.profile_service import get_active_profile
    from ..services.ai.usage_stats import get_daily_trend, get_token_budget_status, get_usage_summary
    active = get_active_profile()
    profile_name = active['name'] if active else 'default'
    return {'summary': get_usage_summary(days=30), 'daily_trend': get_daily_trend(days=7), 'budget': get_token_budget_status(profile_name), 'active_profile': profile_name}

@router.get('/health/dashboard')
async def get_health_dashboard():
    """获取 Provider 健康度面板"""
    from ..services.ai.usage_stats import get_health_dashboard as _get
    return await asyncio.to_thread(_get)

@router.get('/usage/trend')
async def get_usage_trend(days: int=7):
    """获取每日调用趋势"""
    from ..services.ai.usage_stats import get_daily_trend as _get
    return await asyncio.to_thread(_get, days=days)

@router.delete('/usage/truncate')
async def truncate_usage_stats(days: int=90):
    """清理超过指定天数的统计数据"""
    from ..services.ai.usage_stats import truncate_stats as _truncate
    deleted = _truncate(keep_days=days)
    return {'status': 'ok', 'deleted_records': deleted}

class TextProviderConfigData(BaseModel):
    """单个厂商的文本模型配置"""
    provider_name: str
    provider_type: str = 'openai_compatible'
    api_key: str = ''
    base_url: str = ''
    model: str = ''
    max_tokens: int | None = None
    concurrency: int | None = None
    temperature: float = 0.7
    # P1-DOC（2026-10-14）：None = 未提供 → 保留库内原值（此前默认 ''，每次保存都
    # 静默清空）；"" = 显式清空。与 Profile 的 COALESCE 语义对齐。
    document_profile: str | None = None
    # P1-CFG：与主配置对齐的可配置超时（迁移 #37 新增列；旧库无该列时见 save 内的
    # 兼容分支，读侧统一以 120 兜底）。
    timeout: int = 120
    # TD-5（2026-10-06）：保存后是否自动激活为主配置。默认 True 保持向后兼容；
    # 前端"仅编辑参数不切换主用"场景可显式传 False。
    activate_after_save: bool = True


def _row_to_dict(row, col_names: list[str]) -> dict:
    """sqlite3.Row / tuple → dict（兼容两种 row_factory）。"""
    if hasattr(row, 'keys'):
        return dict(row)
    return dict(zip(col_names, row, strict=False))


def _provider_view(row, col_names: list[str], provider_name: str) -> dict:
    """text_provider_configs 行 → HTTP 响应字典（脱敏 + 补稳定 id）。

    结构化防御：此处**永不**回传库内原始密钥列（只出 `api_key` 占位符 +
    `api_key_set` 布尔）。内部需要原始值的路径（如 activate 回写主表）走
    `_row_to_dict`，不复用本函数。
    """
    d = _row_to_dict(row, col_names)
    return {
        'provider_name': d.get('provider_name', provider_name),
        # P1-ID（2026-10-14）：新增稳定机器 id。前端此前只能靠中文 provider_name 去
        # 猜厂商（本地 snake_case 键表 11 项仅 2 项对得上），导致 base_url/model
        # 默认值回填大面积失效。新增字段，旧前端忽略即可。
        'provider_id': provider_catalog.provider_id_of(d.get('provider_name', provider_name)),
        'provider_type': d.get('provider_type') or 'openai_compatible',
        'api_key': _display_api_key(d.get('api_key') or ''),
        'api_key_set': bool(d.get('api_key')),
        'base_url': d.get('base_url') or '',
        'model': d.get('model') or '',
        'max_tokens': d.get('max_tokens') or 4096,
        'concurrency': d.get('concurrency') or 4,
        'temperature': d.get('temperature') if d.get('temperature') is not None else 0.7,
        'document_profile': d.get('document_profile') or '',
        'timeout': d.get('timeout') or 120,
        'is_active': bool(d.get('is_active', 1)),
        'updated_at': d.get('updated_at', ''),
    }


_TEXT_PROVIDER_COLS = [
    'provider_name', 'provider_type', 'api_key', 'base_url', 'model',
    'max_tokens', 'concurrency', 'temperature', 'document_profile', 'timeout',
    'is_active', 'updated_at',
]
_TEXT_PROVIDER_SELECT = (
    'SELECT provider_name, provider_type, api_key, base_url, model,\n'
    '       max_tokens, concurrency, temperature, document_profile, timeout,\n'
    '       is_active, updated_at\n'
    'FROM text_provider_configs\n'
)

@router.get('/provider-configs')
async def list_text_provider_configs():
    """获取所有厂商的文本模型配置（每个厂商独立保存，互不共享）"""
    configs = []
    # PERF（2026-10-14）：整段读移出事件循环（get_db_with_retry 持全局写锁 RLock，
    # 在 loop 内调用会冻结并发请求）。与本文件其它读端点口径统一。
    def _read_all():
        with get_db_with_retry(readonly=True) as conn:
            return conn.execute(
                _TEXT_PROVIDER_SELECT + 'ORDER BY is_active DESC, provider_name'
            ).fetchall()

    rows = await asyncio.to_thread(_read_all)
    for row in rows:
        configs.append(_provider_view(row, _TEXT_PROVIDER_COLS, ''))
    configured_names = {c['provider_name'] for c in configs}
    for p in KNOWN_PROVIDERS:
        pname = p['name']
        if pname not in configured_names:
            configs.append({
                'provider_name': pname,
                # P1-ID：与「已配置」行同构，保证前端两种卡片字段一致
                'provider_id': p.get('id', ''),
                'provider_type': p.get('type', 'openai_compatible'),
                'api_key': '',
                'api_key_set': False,
                'base_url': p.get('base_url', ''),
                # P1-ID（2026-10-14）：默认模型取自厂商目录正本
                # （DEFAULT_TEXT_PROVIDER.model 由 AGNES_MODEL 派生，可被环境变量
                # 覆盖），前端据此预填；目录未收录时留空，由前端本地键表/手填兜底。
                'model': p.get('model', ''),
                'max_tokens': 4096,
                'concurrency': 4,
                'temperature': 0.7,
                'document_profile': '',
                'timeout': 120,
                'is_active': False,
                'updated_at': '',
            })
    return {'configs': configs}

@router.put('/provider-configs/{provider_name}')
async def save_text_provider_config(provider_name: str, data: TextProviderConfigData, request: Request = None):
    """保存单个厂商的文本模型配置（厂商间 KEY 完全隔离，不共享）"""
    if data.provider_name != provider_name:
        raise HTTPException(status_code=400, detail='provider_name 不匹配')
    # F-2 BUG修复（校验对齐）：本路由是文本模型配置的**主写路径**，但此前唯一的
    # 钳制只有 max_tokens —— 不校验 base_url（file:// 等危险协议可经 activate
    # 同步进主表并被 AI 调用使用）、temperature/concurrency 无上限（0/负数/999
    # 直接拖垮生成并发）。口径与 save_ai_config 完全对齐，默认行为不变。
    if data.base_url:
        url_err = _validate_base_url(data.base_url)
        if url_err:
            raise HTTPException(status_code=400, detail=url_err)
    elif data.model:
        # TD-6（2026-10-06）：model 已填但 base_url 留空，激活后 AI 调用会因
        # 空 URL 直接报错。不阻断保存（兼容历史空记录），但落 warning 便于排查。
        logger.warning(
            "文本厂商 %s 配置了 model=%r 但 base_url 为空；激活后 AI 调用会失败",
            provider_name, data.model,
        )
    data.max_tokens = min(max(int(data.max_tokens or 4096), 256), 65536)
    try:
        data.concurrency = min(max(int(data.concurrency or 4), 1), 64)
    except (TypeError, ValueError):
        data.concurrency = 4
    try:
        data.temperature = min(max(float(data.temperature if data.temperature is not None else 0.7), 0.0), 2.0)
    except (TypeError, ValueError):
        data.temperature = 0.7
    try:
        data.timeout = min(max(int(data.timeout or 120), 5), 600)
    except (TypeError, ValueError):
        data.timeout = 120
    # BUG-FIX：读接口改为脱敏后，前端「只改模型/URL、不动密钥」时会把占位符或空串
    # 原样提交回来。此前直接写入会把已保存的密钥清空。这里与 `/ai/config` 保持
    # 同一语义：占位符 / 空串 = 保持原值；只有真正的非空新密钥才覆盖。
    if _is_masked_api_key(data.api_key) or not (data.api_key or '').strip():
        data.api_key = _existing_provider_api_key(provider_name)
    # F-1：新提交的明文密钥加密落库；回填的库内原值（已是 ENC:/历史明文）幂等透传。
    data.api_key = _encrypt_fresh_key(data.api_key)
    # PERF（2026-10-05）：写入整段派发 to_thread，全局写锁在 worker 线程阻塞，loop 不受影响
    def _write_text_provider_config():
        with get_db_with_retry() as conn:
            # document_profile：None=未提供→保留原值 / ""=显式清空（P1-DOC）。
            # 列是 NOT NULL，INSERT 路径不能用 SQL COALESCE（NULL 会触发约束失败），
            # 故在同一事务内先回读旧值再决定写入值（与 save_ai_config 同口径）。
            _dp_row = conn.execute(
                'SELECT document_profile FROM text_provider_configs WHERE provider_name = ?',
                [provider_name],
            ).fetchone()
            _dp_current = (_dp_row[0] or '') if _dp_row else ''
            effective_dp = _dp_current if data.document_profile is None else data.document_profile
            conn.execute("\n            INSERT INTO text_provider_configs\n                (provider_name, provider_type, api_key, base_url, model,\n                 max_tokens, concurrency, temperature, document_profile, timeout, is_active)\n            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)\n            ON CONFLICT(provider_name) DO UPDATE SET\n                provider_type=excluded.provider_type,\n                api_key=excluded.api_key,\n                base_url=excluded.base_url,\n                model=excluded.model,\n                max_tokens=excluded.max_tokens,\n                concurrency=excluded.concurrency,\n                temperature=excluded.temperature,\n                document_profile=excluded.document_profile,\n                timeout=excluded.timeout,\n                is_active=text_provider_configs.is_active,\n                updated_at=datetime('now', 'localtime')\n        ", (data.provider_name, data.provider_type, data.api_key, data.base_url, data.model, data.max_tokens, data.concurrency, data.temperature, effective_dp, data.timeout))
            conn.commit()
    await asyncio.to_thread(_write_text_provider_config)
    # L-3/L-5：降级链第二层来源变化也要跨进程失效 + 审计
    bump_ai_config_version()
    # F-NEW-4：审计写库派发到线程，避免阻塞事件循环
    await asyncio.to_thread(
        lambda: _audit('save_provider_config', provider_name,
                       f"model={data.model} key={mask_key_for_audit(data.api_key)}", request)
    )
    if data.api_key and data.api_key.strip() and data.activate_after_save:
        await activate_text_provider_config(provider_name)
    return {'status': 'ok', 'provider_name': provider_name}

@router.get('/provider-configs/{provider_name}')
async def get_text_provider_config(provider_name: str):
    """获取单个厂商的文本模型配置"""
    def _read_one():
        with get_db_with_retry(readonly=True) as conn:
            return conn.execute(
                _TEXT_PROVIDER_SELECT + 'WHERE provider_name = ?', [provider_name]
            ).fetchone()

    row = await asyncio.to_thread(_read_one)
    if not row:
        known = next((p for p in KNOWN_PROVIDERS if p['name'] == provider_name), None)
        # G-3（2026-10-11）：补 api_key_set 布尔提示，与 /image-provider-configs 单查口径一致
        # P1-ID：未收录于目录的厂商 provider_id 为空串（前端据此回退按名查找）
        return {
            'provider_name': provider_name,
            'provider_id': known.get('id', '') if known else '',
            'provider_type': known.get('type', 'openai_compatible') if known else 'openai_compatible',
            'api_key': '', 'api_key_set': False,
            'base_url': known.get('base_url', '') if known else '',
            'model': known.get('model', '') if known else '',
            'max_tokens': 4096, 'concurrency': 4, 'temperature': 0.7,
            'document_profile': '', 'timeout': 120,
            'is_active': False, 'updated_at': '',
        }
    # G-3：api_key_set 前端可据它显示「已配置/未配置」，无需再判字符串
    return _provider_view(row, _TEXT_PROVIDER_COLS, provider_name)

# F-13：单厂商 GET 与列表接口口径不一致（列表有 updated_at，单查无），
# 前端刷新单卡时拿不到更新时间。补列保持两接口同构，新增字段向后兼容。

# F-7 并发修复：激活=「全表置 0 → 目标置 1 → 同步主表」三步跨多个连接执行，
# 并发激活两个厂商时可能交错出「双双 inactive」或「flag 与主表不一致」。
# SQLite 不支持 UPDATE FROM CTE，在事件循环内用 asyncio.Lock 串行化激活写路径；
# （不用 threading.Lock：本临界区含 await，同步锁会在单事件循环下自死锁）
#
# D-2 修复（2026-10-06）：锁改为**按运行中事件循环取**。模块级单例 asyncio.Lock
# 在多loop 场景（Celery 任务各自 _safe_asyncio_run 建 loop + FastAPI 主 loop，
# 测试里更是每线程一个 loop）会把等待者挂到「首次使用它的那个 loop」上，
# 另一个 loop 里的 acquire 永久挂起（实测表现为测试超时/请求卡死）。
# 现每个 loop 各自一把锁：本loop 内仍严格串行（这正是 F-7 需要的语义），
# 跨 loop/跨进程一致性由唯一索引 idx_tpc_unique_active + 409 语义化冲突保证。
_text_activate_locks: dict[int, asyncio.Lock] = {}
_text_activate_locks_guard = threading.Lock()


def _get_text_activate_lock() -> asyncio.Lock:
    """取当前事件循环专属的激活锁（并顺手清理已关闭 loop 的残留条目）。"""
    loop = asyncio.get_running_loop()
    key = id(loop)
    with _text_activate_locks_guard:
        if len(_text_activate_locks) > 32:  # 兜底防泄漏
            for k in [k for k in _text_activate_locks if k != key and k in _closed_loop_ids()]:
                _text_activate_locks.pop(k, None)
        lock = _text_activate_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _text_activate_locks[key] = lock
        return lock


def _closed_loop_ids() -> set[int]:
    import gc

    ids: set[int] = set()
    for obj in gc.get_objects():
        if isinstance(obj, asyncio.AbstractEventLoop) and obj.is_closed():
            ids.add(id(obj))
    return ids


@router.put('/provider-configs/{provider_name}/activate')
async def activate_text_provider_config(provider_name: str, request: Request = None):
    """将指定厂商的配置设为当前激活的文本模型配置（同步到 ai_config 主表）"""
    async with _get_text_activate_lock():
        # PERF（2026-10-05）：整段写（含 SELECT 回读 + 全表置 0/目标置 1 + 主表 UPSERT）
        # 派发 to_thread，写锁在 worker 线程阻塞，loop 不受影响；外层激活锁
        # 是 asyncio.Lock（非 threading.Lock），跨 await 持有安全；回读的 d 是物化 dict。
        def _activate_text_write():
            with get_db_with_retry() as conn:
                row = conn.execute(_TEXT_PROVIDER_SELECT + 'WHERE provider_name = ?', [provider_name]).fetchone()
                if not row:
                    raise HTTPException(status_code=404, detail=f"厂商 '{provider_name}' 的配置不存在，请先保存")
                d = _row_to_dict(row, _TEXT_PROVIDER_COLS)
                conn.execute('UPDATE text_provider_configs SET is_active = 0')
                conn.execute('UPDATE text_provider_configs SET is_active = 1 WHERE provider_name = ?', [provider_name])
                # P1-CFG（2026-10-14）：激活时把厂商级 timeout 同步到主配置 —— 否则
                # 「某厂商专门调了超时」在激活后不生效（主配置保持上一次的值/默认 120s）。
                # P1-DOC：document_profile 用 COALESCE 保留语义（激活不覆盖「未提供」）。
                conn.execute("INSERT INTO ai_config (id, provider_name, provider, api_key, model, base_url,\n                document_profile, concurrency, temperature, max_tokens, timeout, updated_at)\n               VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now', 'localtime'))\n               ON CONFLICT(id) DO UPDATE SET\n                provider_name=excluded.provider_name, provider=excluded.provider,\n                api_key=excluded.api_key, model=excluded.model,\n                base_url=excluded.base_url,\n                document_profile=excluded.document_profile,\n                concurrency=excluded.concurrency,\n                temperature=excluded.temperature,\n                max_tokens=excluded.max_tokens,\n                timeout=excluded.timeout,\n                updated_at=datetime('now', 'localtime')", (d.get('provider_name', provider_name), d.get('provider_type', 'openai_compatible'), d.get('api_key') or '', d.get('model') or '', d.get('base_url') or '', d.get('document_profile') or '', d.get('concurrency') or 4, d.get('temperature') or 0.7, d.get('max_tokens') or 4096, d.get('timeout') or 120))
                conn.commit()
            return d
        try:
            d = await asyncio.to_thread(_activate_text_write)
        except sqlite3.IntegrityError as e:
            # D-2（2026-10-06）：激活写路径受唯一索引 idx_tpc_unique_active 保护
            # （迁移 #33，保证全库至多一个 is_active=1）。跨进程并发激活时，
            # 后到者会撞唯一索引 —— 此前直接冒泡成500，调用方无法区分
            # 「并发冲突」与「真故障」。现返回 409 语义化错误，客户端重试即可。
            # 注：进程内已由 _text_activate_lock 串行化，此分支只在多实例下触发。
            if 'unique' in str(e).lower():
                raise HTTPException(
                    status_code=409,
                    detail='配置已被其他进程同时修改，请重试',
                ) from e
            raise
        bump_ai_config_version()
        # F-NEW-4：审计 + 用量记录派发到线程（激活锁是 asyncio.Lock，跨 await 持有安全）
        def _post_activate_bookkeeping():
            _audit('activate_provider_config', provider_name, request=request)
            record_provider_usage(d.get('provider_name', provider_name), d.get('provider_type', 'openai_compatible'), d.get('base_url', ''), success=True)
        await asyncio.to_thread(_post_activate_bookkeeping)
        return {'status': 'ok', 'provider_name': provider_name}

class ImageProviderConfigData(BaseModel):
    """单个厂商的图像模型配置"""
    provider_name: str
    api_key: str = ''
    base_url: str = ''
    model: str = ''
    max_concurrency: int = 2
    max_per_project: int = 10
    quality: str = 'standard'
    style_injection: str = ''
    enabled: bool = True
    default_size: str = '1024x1024'
    default_ratio: str = '1:1'
    price_per_image: float = 0.0

@router.get('/image-provider-configs')
async def list_image_provider_configs():
    """获取所有厂商的图像模型配置（每个厂商独立保存，互不共享）"""
    configs = []
    with get_db_with_retry(readonly=True) as conn:
        rows = conn.execute('\n            SELECT provider_name, api_key, base_url, model, max_concurrency,\n                   max_per_project, quality, style_injection, enabled,\n                   default_size, default_ratio, price_per_image, is_active, updated_at\n            FROM image_provider_configs\n            ORDER BY is_active DESC, provider_name\n        ').fetchall()
        col_names = ['provider_name', 'api_key', 'base_url', 'model', 'max_concurrency', 'max_per_project', 'quality', 'style_injection', 'enabled', 'default_size', 'default_ratio', 'price_per_image', 'is_active', 'updated_at']
        for row in rows:
            d = dict(row) if hasattr(row, 'keys') else dict(zip(col_names, row, strict=False))
            configs.append({'provider_name': d.get('provider_name', ''), 'api_key': _display_api_key(d.get('api_key') or ''), 'base_url': d.get('base_url') or '', 'model': d.get('model') or '', 'max_concurrency': d.get('max_concurrency') or 2, 'max_per_project': d.get('max_per_project') or 10, 'quality': d.get('quality') or 'standard', 'style_injection': d.get('style_injection') or '', 'enabled': bool(d.get('enabled', 1)), 'default_size': d.get('default_size') or '1024x1024', 'default_ratio': d.get('default_ratio') or '1:1', 'price_per_image': d.get('price_per_image') or 0.0, 'is_active': bool(d.get('is_active', 0)), 'updated_at': d.get('updated_at', '')})
    configured_names = {c['provider_name'] for c in configs}
    image_providers = ['agnes', 'dashscope', 'volcengine', 'hunyuan', 'zhipu', 'stepfun', 'baidu', 'openai_compatible']
    for pid in image_providers:
        if pid not in configured_names:
            configs.append({'provider_name': pid, 'api_key': '', 'base_url': '', 'model': '', 'max_concurrency': 2, 'max_per_project': 10, 'quality': 'standard', 'style_injection': '', 'enabled': False, 'default_size': '1024x1024', 'default_ratio': '1:1', 'price_per_image': 0.0, 'is_active': False, 'updated_at': ''})
    return {'configs': configs}

@router.put('/image-provider-configs/{provider_name}')
async def save_image_provider_config(provider_name: str, data: ImageProviderConfigData, request: Request = None):
    """保存单个厂商的图像模型配置（厂商间 KEY 完全隔离）"""
    if data.provider_name != provider_name:
        raise HTTPException(status_code=400, detail='provider_name 不匹配')
    data.base_url = _normalize_image_base_url(data.base_url)
    # 与文本厂商一致：占位符 / 空串 = 保持原值，避免「只改模型」误清空密钥
    if _is_masked_api_key(data.api_key) or not (data.api_key or '').strip():
        data.api_key = _existing_image_provider_api_key(provider_name)
    # S-3 加密收口（2026-10-06）：与文本厂商口径对齐 —— 新明文密钥 ENC: 加密落库，
    # 回填的库内原值（密文/历史明文）幂等透传，不二次加密、不降级。
    data.api_key = _encrypt_fresh_key(data.api_key)
    # PERF（2026-10-05）：写入整段派发 to_thread，全局写锁在 worker 线程阻塞，loop 不受影响
    def _write_image_provider_config():
        with get_db_with_retry() as conn:
            conn.execute("\n            INSERT INTO image_provider_configs\n                (provider_name, api_key, base_url, model, max_concurrency,\n                 max_per_project, quality, style_injection, enabled,\n                 default_size, default_ratio, price_per_image, updated_at)\n            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now', 'localtime'))\n            ON CONFLICT(provider_name) DO UPDATE SET\n                api_key=excluded.api_key,\n                base_url=excluded.base_url,\n                model=excluded.model,\n                max_concurrency=excluded.max_concurrency,\n                max_per_project=excluded.max_per_project,\n                quality=excluded.quality,\n                style_injection=excluded.style_injection,\n                enabled=excluded.enabled,\n                default_size=excluded.default_size,\n                default_ratio=excluded.default_ratio,\n                price_per_image=excluded.price_per_image,\n                updated_at=datetime('now', 'localtime')\n        ", (data.provider_name, data.api_key, data.base_url, data.model, data.max_concurrency, data.max_per_project, data.quality, data.style_injection, 1 if data.enabled else 0, data.default_size, data.default_ratio, data.price_per_image))
            conn.commit()
    await asyncio.to_thread(_write_image_provider_config)
    # A-1 审计补齐（2026-10-06）：与文本厂商保存路径同口径（密钥只记脱敏摘要）
    # F-NEW-4：审计写库派发到线程，避免阻塞事件循环
    await asyncio.to_thread(
        lambda: _audit('save_image_provider_config', provider_name,
                       f"model={data.model} key={mask_key_for_audit(data.api_key)}", request)
    )
    if data.api_key and data.api_key.strip():
        # BUG-FIX：此处此前漏了 await，产生的协程从未被调度 ——
        # 「保存图像厂商配置后自动同步到主表」实际从未生效。
        await activate_image_provider_config(provider_name, request=request)
    return {'status': 'ok', 'provider_name': provider_name}

@router.get('/image-provider-configs/{provider_name}')
async def get_image_provider_config(provider_name: str):
    """获取单个厂商的图像模型配置"""
    with get_db_with_retry(readonly=True) as conn:
        row = conn.execute('\n            SELECT provider_name, api_key, base_url, model, max_concurrency,\n                   max_per_project, quality, style_injection, enabled,\n                   default_size, default_ratio, price_per_image, updated_at\n            FROM image_provider_configs WHERE provider_name = ?\n        ', [provider_name]).fetchone()
    if not row:
        # G-4（2026-10-11）：补齐 updated_at（列表接口 F-13 口径同构）+ api_key_set 提示
        return {'provider_name': provider_name, 'api_key': '', 'api_key_set': False, 'base_url': '', 'model': '', 'max_concurrency': 2, 'max_per_project': 10, 'quality': 'standard', 'style_injection': '', 'enabled': False, 'default_size': '1024x1024', 'default_ratio': '1:1', 'price_per_image': 0.0, 'updated_at': ''}
    col_names = ['provider_name', 'api_key', 'base_url', 'model', 'max_concurrency', 'max_per_project', 'quality', 'style_injection', 'enabled', 'default_size', 'default_ratio', 'price_per_image', 'updated_at']
    d = dict(row) if hasattr(row, 'keys') else dict(zip(col_names, row, strict=False))
    return {'provider_name': d.get('provider_name', ''), 'api_key': _display_api_key(d.get('api_key') or ''), 'api_key_set': bool(d.get('api_key')), 'base_url': d.get('base_url') or '', 'model': d.get('model') or '', 'max_concurrency': d.get('max_concurrency') or 2, 'max_per_project': d.get('max_per_project') or 10, 'quality': d.get('quality') or 'standard', 'style_injection': d.get('style_injection') or '', 'enabled': bool(d.get('enabled', 1)), 'default_size': d.get('default_size') or '1024x1024', 'default_ratio': d.get('default_ratio') or '1:1', 'price_per_image': d.get('price_per_image') or 0.0, 'updated_at': d.get('updated_at', '')}

@router.put('/image-provider-configs/{provider_name}/activate')
async def activate_image_provider_config(provider_name: str, request: Request = None):
    """将指定厂商的配置同步到 model_configs 主表（供现有图像生成读取）"""
    # PERF（2026-10-05）：整段写（回读 + 全表置 0/目标置 1 + 主表 UPSERT）派发 to_thread，
    # 全局写锁在 worker 线程阻塞，loop 不受影响
    def _activate_image_write():
        with get_db_with_retry() as conn:
            row = conn.execute('\n            SELECT provider_name, api_key, base_url, model, max_concurrency,\n                   max_per_project, quality, style_injection, enabled,\n                   default_size, default_ratio, price_per_image\n            FROM image_provider_configs WHERE provider_name = ?\n        ', [provider_name]).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail=f"厂商 '{provider_name}' 的配置不存在")
            col_names = ['provider_name', 'api_key', 'base_url', 'model', 'max_concurrency', 'max_per_project', 'quality', 'style_injection', 'enabled', 'default_size', 'default_ratio', 'price_per_image']
            d = dict(row) if hasattr(row, 'keys') else dict(zip(col_names, row, strict=False))
            conn.execute('UPDATE image_provider_configs SET is_active = 0')
            conn.execute('UPDATE image_provider_configs SET is_active = 1 WHERE provider_name = ?', [provider_name])
            existing = conn.execute('SELECT 1 FROM model_configs WHERE user_id = 1').fetchone()
            if existing:
                conn.execute('\n                UPDATE model_configs SET\n                    image_provider = ?, image_model = ?, image_base_url = ?,\n                    image_api_key_enc = ?, image_max_concurrency = ?,\n                    image_max_per_project = ?, image_quality = ?,\n                    image_style_injection = ?, image_enabled = ?,\n                    image_default_size = ?, image_default_ratio = ?,\n                    image_price_per_image = ?\n                WHERE user_id = 1\n            ', (d.get('provider_name'), d.get('model'), d.get('base_url'), d.get('api_key') or '', d.get('max_concurrency', 2), d.get('max_per_project', 10), d.get('quality', 'standard'), d.get('style_injection', ''), 1 if d.get('enabled', 1) else 0, d.get('default_size', '1024x1024'), d.get('default_ratio', '1:1'), d.get('price_per_image', 0.0)))
            else:
                conn.execute('\n                INSERT INTO model_configs\n                    (user_id, image_provider, image_model, image_base_url, image_api_key_enc,\n                     image_max_concurrency, image_max_per_project, image_quality,\n                     image_style_injection, image_enabled, image_default_size,\n                     image_default_ratio, image_price_per_image)\n                VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)\n            ', (d.get('provider_name'), d.get('model'), d.get('base_url'), d.get('api_key') or '', d.get('max_concurrency', 2), d.get('max_per_project', 10), d.get('quality', 'standard'), d.get('style_injection', ''), 1 if d.get('enabled', 1) else 0, d.get('default_size', '1024x1024'), d.get('default_ratio', '1:1'), d.get('price_per_image', 0.0)))
            conn.commit()
    await asyncio.to_thread(_activate_image_write)
    # A-1 审计补齐（2026-10-06）：与文本厂商激活路径同口径
    # F-NEW-4：审计写库派发到线程
    await asyncio.to_thread(lambda: _audit('activate_image_provider_config', provider_name, request=request))
    return {'status': 'ok', 'provider_name': provider_name}

class ModelInfoRequest(BaseModel):
    api_key: str = ''
    base_url: str = ''
    model: str = ''
    provider: str = ''
    # P1-ID（2026-10-14）：新增可选稳定机器 id（对应 /ai/providers 的 id 字段）。
    # 前端传 id 时按 id 查元数据表，避免中文展示名匹配失败；不传则回退 provider。
    provider_id: str = ''

@router.post('/model-info')
async def fetch_model_info(data: ModelInfoRequest):
    """获取模型元数据：max_tokens（最大输出 Token）、concurrency（并发数）

    优先级：
    1. 从内置 PROVIDER_MODEL_METADATA 表查询
    2. 尝试从厂商 API 的 /models 端点获取（部分厂商返回 context_window 等字段）
    3. 无法获取时返回默认值，前端提示用户手动填写
    """
    result = {'model': data.model, 'max_tokens': _DEFAULT_MAX_TOKENS, 'concurrency': _DEFAULT_CONCURRENCY, 'source': 'default'}
    # D-1：元数据匹配逻辑收敛到 provider_catalog.find_model_metadata（含 F-8 的
    # 厂商名/模型名大小写容错 + P1-META 的中文展示名/稳定 id 解析），本路由只负责
    # 「命中 builtin 还是回落默认值」。
    # 前端传 provider_id（稳定机器 id）时优先，避免中文名 → 名称完全匹配
    # （即便能匹配也不受大小写/空格影响）。
    lookup_provider = data.provider_id or data.provider
    meta = provider_catalog.find_model_metadata(lookup_provider, data.model)
    if not meta and data.base_url:
        # 厂商名匹配不到时，退化为按 base_url 模糊匹配（F-8 口径不变）
        url_lower = data.base_url.lower()
        for key in PROVIDER_MODEL_METADATA:
            if key.replace('_', '') in url_lower:
                table = PROVIDER_MODEL_METADATA[key]
                meta = table.get(data.model) or {k.lower(): v for k, v in table.items()}.get(data.model.lower())
                break
    if meta:
        result['max_tokens'] = meta.get('max_tokens', _DEFAULT_MAX_TOKENS)
        result['concurrency'] = meta.get('concurrency', _DEFAULT_CONCURRENCY)
        result['source'] = 'builtin'
        return result
    if data.api_key and data.base_url:
        try:
            api_key = _resolve_api_key(data.api_key)
            url_err = _validate_base_url(data.base_url)
            if url_err:
                return result
            base = data.base_url.rstrip('/')
            _matched_ver = re.search('/(v\\d+)$', base)
            if _matched_ver and _matched_ver.group(1) in ('v1', 'v2', 'v3'):
                base = base[:_matched_ver.start()].rstrip('/')
            if '/images/' in base:
                from urllib.parse import urlparse as _urlparse
                _parsed = _urlparse(base)
                models_url = f'{_parsed.scheme}://{_parsed.netloc}/v1/models'
            else:
                models_url = f'{base}/models'
                if not re.search('/v\\d+', base):
                    models_url = f'{base}/v1/models'
            headers = {'Authorization': f'Bearer {api_key}'}
            async with httpx.AsyncClient(timeout=10, **build_httpx_proxy_settings()) as client:
                resp = await client.get(models_url, headers=headers)
                if resp.status_code == 200:
                    body = resp.json()
                    models_list = body.get('data', [])
                    for m in models_list:
                        mid = (m.get('id') or '').strip()
                        if mid == data.model:
                            ctx = m.get('context_length') or m.get('context_window') or m.get('max_context_length')
                            if ctx and isinstance(ctx, (int, float)):
                                result['max_tokens'] = min(int(ctx), 65536)
                                result['source'] = 'api'
                            break
        except Exception as _e:
            # B-1 修复（2026-10-06）：此前元数据探测失败被完全静默（吞错无日志），
            # 排障时无法区分「厂商没有返回 context_window」与「请求根本没发出去」。
            # 本端点语义是尽力而为（失败退回默认值），故仅记 warning 不抛错。
            logger.warning('model-info 元数据探测失败 provider=%s model=%s: %s',
                           data.provider, data.model, type(_e).__name__)
    return result
