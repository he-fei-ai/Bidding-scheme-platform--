"""Provider 工厂 — 优先级链

优先级策略（严格顺序，不可交换）：
  1. 当前保存的配置（ai_config 表）—— 优先使用用户当前选定的厂商
  2. 最近配置的厂商历史（provider_history 表，按 last_used_at 降序）—— 主配置不可用时降级
  3. 内置兜底提供商（DeepSeek / SiliconFlow / 通义 / 智谱 / OpenAI）—— 全部失败时最后防线
  4. 全部不可用 → 返回主配置实例（让上层处理错误，不静默失败）
"""

import hashlib
import logging
import os
import threading
import time
from typing import Any

from fastapi import HTTPException

from ...config import AGNES_API_KEY, AGNES_BASE_URL, AGNES_MODEL
from ...db import get_db, get_db_with_retry
from ...utils.proxy_config import build_httpx_proxy_settings
from ..crypto import decrypt_api_key, is_encrypted
from . import provider_catalog


logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────
# 缓存
# ──────────────────────────────────────────────────────────────────────
_ai_config_cache: dict | None = None
_ai_config_cache_time: float = 0
_AI_CONFIG_TTL = 3600  # 1 小时，减少数据库查询
_ai_config_lock = threading.Lock()

# L-3（2026-10-04 遗留闭环）：跨进程缓存失效 —— 进程内 _clear_ai_config_cache()
# 无法通知其他 worker 进程，此前多进程部署下另一进程最长 1 小时读到旧配置。
# 方案：库内版本计数器 ai_config_state.ai_config_version，写路径 +1；
# 读路径以 5 秒为粒度比对版本号（防抖，避免每次调用都多一次 SQL），
# 版本变化即丢弃缓存重建 —— 跨进程收敛时间从 TTL(1h) 降到 ≤5s。
# 表不存在/查询失败时静默退化为纯 TTL 行为（向后兼容，不阻断业务）。
_CONFIG_VERSION_CHECK_INTERVAL = 5.0
_seen_config_version: int = -1
_version_checked_at: float = 0.0


def _read_config_version() -> int:
    """读取库内配置版本号；表缺失/异常返回 -1（视为「无版本信息」，退化为 TTL）。"""
    try:
        with get_db(readonly=True) as conn:
            row = conn.execute(
                "SELECT value FROM ai_config_state WHERE key = 'ai_config_version'"
            ).fetchone()
        return int(row[0]) if row else 0
    except Exception:
        return -1


def bump_ai_config_version() -> None:
    """配置写入后递增版本计数器，并清除本进程缓存。

    与写入 SQL 分开执行（写路径各自 commit 风格不同），best-effort：
    失败仅影响其他进程的失效时效，不影响本次写入正确性。
    """
    _clear_ai_config_cache()
    # 配置刚变（换厂商/换密钥/换模型）→ 旧的探活结论可能已失效，一并清空
    _clear_health_cache()
    try:
        with get_db() as conn:
            conn.execute(
                "INSERT INTO ai_config_state (key, value) VALUES ('ai_config_version', '1')\n"
                "ON CONFLICT(key) DO UPDATE SET value = CAST(value AS INTEGER) + 1"
            )
            conn.commit()
    except Exception as e:
        logger.debug("递增 ai_config 版本号失败（退化为 TTL 失效）: %s", e)


# ──────────────────────────────────────────────────────────────────────
# 降级链探活结果缓存（性能）
# ──────────────────────────────────────────────────────────────────────
# 背景：create_provider_with_fallback 每次调用都会对降级链上**每个**厂商发一次
# 同步 POST /chat/completions 探活（单次 5s 超时）。链长 5+ 时最坏一次调用要
# 20~30 秒，且同一次业务动作往往反复创建 provider，探活请求被成倍放大。
# 这里给探活结论加一个很短的进程内缓存：命中即跳过 HTTP。
# 语义保证：
#   - 成功结论缓存 60s、失败结论 15s（失败缓存更短，保证厂商恢复后能尽快被选中）；
#   - 键含 URL + 模型 + **密钥指纹**（sha256 前 12 位，不把可读密钥放进缓存键）；
#   - 配置变更（bump_ai_config_version）立即清空，不等TTL；
#   - 只影响「是否跳过重复探活」，不改变降级顺序与最终选中的厂商语义。
_HEALTH_CACHE: dict[str, tuple[float, bool]] = {}
_HEALTH_CACHE_LOCK = threading.Lock()
_HEALTH_CACHE_TTL_OK = 60.0
_HEALTH_CACHE_TTL_FAIL = 15.0
_HEALTH_CACHE_MAX_ENTRIES = 64


def _health_cache_key(base_url: str, model: str, api_key: str) -> str:
    fingerprint = hashlib.sha256((api_key or "").encode("utf-8")).hexdigest()[:12]
    return f"{(base_url or '').rstrip('/')}|{model or ''}|{fingerprint}"


def _health_cache_get(key: str) -> bool | None:
    """返回缓存的探活结论；无有效缓存返回 None（表示需真实探活）。"""
    now = time.time()
    with _HEALTH_CACHE_LOCK:
        item = _HEALTH_CACHE.get(key)
        if not item:
            return None
        ts, ok = item
        ttl = _HEALTH_CACHE_TTL_OK if ok else _HEALTH_CACHE_TTL_FAIL
        if now - ts > ttl:
            _HEALTH_CACHE.pop(key, None)
            return None
        return ok


def _health_cache_put(key: str, ok: bool) -> None:
    with _HEALTH_CACHE_LOCK:
        if len(_HEALTH_CACHE) >= _HEALTH_CACHE_MAX_ENTRIES:
            oldest = min(_HEALTH_CACHE.items(), key=lambda kv: kv[1][0])[0]
            _HEALTH_CACHE.pop(oldest, None)
        _HEALTH_CACHE[key] = (time.time(), ok)


def _clear_health_cache() -> None:
    with _HEALTH_CACHE_LOCK:
        _HEALTH_CACHE.clear()


def _external_version_changed(now: float) -> bool:
    """缓存命中前的跨进程失效检查：5 秒防抖比对库内版本号。"""
    global _seen_config_version, _version_checked_at
    if _version_checked_at and (now - _version_checked_at) < _CONFIG_VERSION_CHECK_INTERVAL:
        return False
    _version_checked_at = now
    ver = _read_config_version()
    if ver < 0:
        return False
    if _seen_config_version < 0:
        _seen_config_version = ver
        return False
    if ver != _seen_config_version:
        _seen_config_version = ver
        return True
    return False


# ──────────────────────────────────────────────────────────────────────
# 配置变更审计（L-5 遗留闭环）：who / action / 脱敏字段摘要
# 写入 best-effort，绝不因审计失败阻断配置主流程；密钥只记录脱敏形态。
# ──────────────────────────────────────────────────────────────────────
def record_config_audit(actor: str, action: str, target: str, detail: str = "") -> None:
    try:
        with get_db() as conn:
            conn.execute(
                "INSERT INTO ai_config_audit (actor, action, target, detail, created_at)\n"
                " VALUES (?, ?, ?, ?, datetime('now', 'localtime'))",
                (actor, action, target, detail),
            )
            conn.commit()
    except Exception as e:
        logger.debug("写入 ai_config_audit 失败（不阻断主流程）: %s", e)


def mask_key_for_audit(key: str) -> str:
    """审计用的密钥摘要：脱敏占位符/空值原样标注，真实密钥只留后 4 位。"""
    if not key:
        return "(empty)"
    if key.startswith("***"):
        return "(masked)"
    plain = decrypt_api_key(key) if is_encrypted(key) else key
    if len(plain) <= 4:
        return "(short)"
    return f"(tail:{plain[-4:]})"


# HARDENING 2026-10-11（遗留·审计日志无 retention）：配置审计表 ai_config_audit
# 会长期累积。新增按天清理函数：删除 retention_days 天之前的记录（默认 90）。
# 返回删除的行数；任何异常都不阻断主流程（与 record_config_audit 同容错口径）。
def cleanup_old_config_audit(retention_days: int = 90) -> int:
    try:
        retention_days = max(1, int(retention_days))
        with get_db() as conn:
            cur = conn.execute(
                "DELETE FROM ai_config_audit"
                " WHERE created_at < datetime('now', 'localtime', ?)",
                (f"-{retention_days} days",),
            )
            conn.commit()
            return cur.rowcount or 0
    except Exception as e:
        logger.warning("清理 ai_config_audit 失败（不阻断主流程）: %s", e)
        return 0


# ──────────────────────────────────────────────────────────────────────
# 内置兜底提供商（永不消失的最后防线）
# D-1（2026-10-06）：清单收敛到 provider_catalog 单一正本，此处保留同名别名
# （既有测试与调用方按 _FALLBACK_PROVIDERS 引用，零改动）。
# ──────────────────────────────────────────────────────────────────────
_FALLBACK_PROVIDERS: list[dict] = provider_catalog.FALLBACK_PROVIDERS

# ──────────────────────────────────────────────────────────────────────
# 默认配置
# ──────────────────────────────────────────────────────────────────────
_AI_CONFIG_DEFAULTS = {
    "provider_name": "agnes",
    "provider": "openai_compatible",
    "api_key": AGNES_API_KEY,
    "model": AGNES_MODEL,
    "base_url": AGNES_BASE_URL,
    "max_tokens": 32768,
    "temperature": 0.7,
    "concurrency": 10,
    "timeout": 120,
    "document_profile": "",
    "is_builtin_default": True,
}


# ──────────────────────────────────────────────────────────────────────
# 工具函数
# ──────────────────────────────────────────────────────────────────────
def detect_provider_type(base_url: str) -> str:
    url_lower = base_url.lower()
    if "deepseek" in url_lower:
        return "deepseek"
    if "anthropic" in url_lower:
        return "anthropic_compatible"
    return "openai_compatible"


def _clear_ai_config_cache():
    """清除 AI 配置缓存（配置变更后由 ai_config.py 调用）"""
    global _ai_config_cache, _ai_config_cache_time, _seen_config_version, _version_checked_at
    with _ai_config_lock:
        _ai_config_cache = None
        _ai_config_cache_time = 0
        # 本进程主动写入后，把已知版本刷新为库内最新值，避免防抖期内误判。
        _seen_config_version = _read_config_version()
        _version_checked_at = time.time()


def _make_provider(
    api_key: str,
    base_url: str,
    model: str,
    max_tokens: int,
    temperature: float,
    timeout: int = 120,
    max_retries: int = 2,
    rate_limit_per_min: int = 0,
    concurrency: int = 0,
) -> Any:
    """根据配置创建对应的 Provider 实例"""
    # 终极防御：model 为空时用厂商默认值兜底，避免 Agnes 等 API 返回
    # "Model name not specified (HTTP 400)"
    if not model:
        model = _AI_CONFIG_DEFAULTS.get("model", "gpt-4o")
        logger.warning("_make_provider: model 为空，使用默认值 '%s'", model)

    p = detect_provider_type(base_url)

    if p == "deepseek":
        from .providers.deepseek import DeepSeekProvider

        return DeepSeekProvider(
            api_key=api_key,
            base_url=base_url,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            timeout=timeout,
            max_retries=max_retries,
            rate_limit_per_min=rate_limit_per_min,
            concurrency=concurrency,
        )
    elif p == "anthropic_compatible":
        from .providers.anthropic_compatible import AnthropicCompatibleProvider

        return AnthropicCompatibleProvider(
            api_key=api_key,
            base_url=base_url,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            timeout=timeout,
            max_retries=max_retries,
            rate_limit_per_min=rate_limit_per_min,
            concurrency=concurrency,
        )
    else:
        from .providers.openai_compatible import OpenAICompatibleProvider

        return OpenAICompatibleProvider(
            api_key=api_key,
            base_url=base_url,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            timeout=timeout,
            max_retries=max_retries,
            rate_limit_per_min=rate_limit_per_min,
            concurrency=concurrency,
        )


# ──────────────────────────────────────────────────────────────────────
# Provider 历史记录管理
# ──────────────────────────────────────────────────────────────────────
def record_provider_usage(
    provider_name: str,
    provider_type: str,
    base_url: str,
    success: bool = True,
) -> None:
    """记录一次 provider 使用情况，用于降级链优先级排序"""
    try:
        with get_db_with_retry() as conn:
            existing = conn.execute(
                "SELECT id, success, failed, last_used_at FROM provider_history "
                "WHERE provider_name = ? AND provider_type = ? AND base_url = ?",
                (provider_name, provider_type, base_url),
            ).fetchone()

            now = time.strftime("%Y-%m-%d %H:%M:%S")
            if existing:
                sid, s, f, _ = existing
                conn.execute(
                    "UPDATE provider_history SET "
                    "success = success + ? , failed = failed + ?, last_used_at = ? "
                    "WHERE id = ?",
                    (1 if success else 0, 0 if success else 1, now, sid),
                )
            else:
                conn.execute(
                    "INSERT INTO provider_history "
                    "(provider_name, provider_type, base_url, success, failed, last_used_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        provider_name,
                        provider_type,
                        base_url,
                        1 if success else 0,
                        1 if not success else 0,
                        now,
                    ),
                )
            conn.commit()
    except Exception as e:
        logger.debug("记录 provider 使用历史失败: %s", e)


def _normalize_url(url: str) -> str:
    """规范化 URL 用于去重（去除末尾斜杠，统一 /v1 路径）"""
    url = url.rstrip("/")
    if url == "https://api.deepseek.com":
        url = url + "/v1"
    return url


# L-1（2026-10-04 遗留闭环）：provider_history 的读/清收敛到本模块，
# 与 record_provider_usage 同属一套降级链实现。此前这两个操作只存在于
# ai_provider_service 的副本里，调用方需要同时依赖两个模块才能完成
# 「记录→查看→清除」闭环，且双实现口径容易漂移（如返回字段名）。
def get_provider_history() -> list[dict]:
    """获取全部 provider 历史记录（含 success_rate 字段，按最近使用降序）"""
    try:
        with get_db_with_retry(readonly=True) as conn:
            rows = conn.execute(
                "SELECT provider_name, provider_type, base_url, success, failed, last_used_at "
                "FROM provider_history ORDER BY last_used_at DESC"
            ).fetchall()
            items = []
            for r in rows:
                success, failed = r[3], r[4]
                total = (success or 0) + (failed or 0)
                items.append(
                    {
                        "provider_name": r[0],
                        "provider_type": r[1],
                        "base_url": r[2],
                        "success": success,
                        "failed": failed,
                        "last_used_at": r[5],
                        "success_rate": (success / total) if total > 0 else 1.0,
                    }
                )
            return items
    except Exception as e:
        logger.debug("查询 provider 历史失败: %s", e)
        return []


def clear_provider_history() -> int:
    """清除全部 provider 历史，返回剩余行数（异常返回 -1）"""
    try:
        with get_db_with_retry() as conn:
            conn.execute("DELETE FROM provider_history")
            conn.commit()
            return conn.execute("SELECT COUNT(*) FROM provider_history").fetchone()[0]
    except Exception as e:
        logger.debug("清除 provider 历史失败: %s", e)
        return -1


# ──────────────────────────────────────────────────────────────────────
# 获取历史记录中的厂商（不含主配置，供降级链使用）
# ──────────────────────────────────────────────────────────────────────
def get_history_providers() -> list[dict]:
    """从 provider_history 表获取已配置的厂商列表（按 last_used_at 降序）

    返回的列表中不包含 api_key（由主配置提供），仅包含连接信息。
    """
    result: list[dict] = []
    seen_urls: set[str] = set()

    try:
        with get_db_with_retry() as conn:
            rows = conn.execute(
                "SELECT provider_name, provider_type, base_url, success, failed, last_used_at "
                "FROM provider_history ORDER BY last_used_at DESC"
            ).fetchall()
            for row in rows:
                name, ptype, url, success, failed, used_at = row
                norm_url = _normalize_url(url)
                if norm_url and norm_url not in seen_urls:
                    seen_urls.add(norm_url)
                    result.append(
                        {
                            "name": name,
                            "provider": ptype,
                            "base_url": url,
                            "api_key": "",
                            "success_rate": success / (success + failed)
                            if (success + failed) > 0
                            else 1.0,
                            "is_builtin_default": False,
                            "last_used_at": used_at,
                        }
                    )
    except Exception as e:
        logger.debug("查询 provider 历史记录失败: %s", e)

    return result


def get_configured_providers() -> list[dict]:
    """从 text_provider_configs 读取用户已配置的、带有有效 API Key 的提供商

    这些是用户明确配置好的、可以真正使用的 provider，
    优先级应高于 history（无 key）和 _FALLBACK_PROVIDERS（大多无 key）。
    """
    result: list[dict] = []
    seen_urls: set[str] = set()

    try:
        with get_db_with_retry() as conn:
            rows = conn.execute(
                "SELECT provider_name, provider_type, base_url, api_key, model, "
                "max_tokens, temperature, concurrency, is_active "
                "FROM text_provider_configs "
                "WHERE api_key IS NOT NULL AND api_key != '' "
                "ORDER BY is_active DESC, updated_at DESC"
            ).fetchall()
            for row in rows:
                name, ptype, url, key, model, max_tokens, temp, concurrency, is_active = row
                norm_url = _normalize_url(url)
                if not norm_url or norm_url in seen_urls:
                    continue
                seen_urls.add(norm_url)
                # F-3 BUG修复（热更新未生效/降级链断裂）：写入侧已统一 ENC: 加密落库，
                # 但本函数把库内**密文串**直接塞进降级链，get_working_provider 拿去当
                # Bearer 请求必然 401 —— 此前仅因写路径存明文而未暴露，加密补齐后不修
                # 则备选厂商全部静失效。主配置路径（get_ai_config）本就解密，口径对齐。
                if key and is_encrypted(key):
                    key = decrypt_api_key(key)
                result.append(
                    {
                        "name": name,
                        "provider": ptype or "openai_compatible",
                        "base_url": url,
                        "api_key": key,
                        "model": model or "gpt-4o",
                        "max_tokens": max_tokens or 16384,
                        "temperature": temp if temp is not None else 0.7,
                        "concurrency": concurrency or 4,
                        "success_rate": 1.0,
                        "is_builtin_default": False,
                        "is_primary": False,
                        "is_user_configured": True,
                        "is_active": bool(is_active),
                    }
                )
    except Exception as e:
        logger.debug("查询 text_provider_configs 失败: %s", e)

    return result


def get_provider_priority_order() -> list[dict]:
    """获取完整降级链（主配置 + 已配置provider + AI Profile + 历史记录 + 内置兜底）

    强制调用顺序：
      1. 文本模型配置中的当前厂商平台（ai_config 表，保存时自动激活）
      2. text_provider_configs 中所有已配置的提供商（带真实 API Key，按 active 优先）
      3. AI Profile 下的默认配置厂商平台
      4. provider_history 中记录的最近配置厂商（无 key）
      5. 内置兜底提供商（大多无 key）
    """
    # 第1层：主配置 = 文本模型配置中当前激活的厂商
    primary = get_ai_config()
    primary_url = _normalize_url(primary.get("base_url", ""))
    primary_key = primary.get("api_key", "")
    primary_name = primary.get("provider_name", "主配置")

    result: list[dict] = [
        {
            "name": primary_name,
            "provider": primary.get("provider", "openai_compatible"),
            "base_url": primary.get("base_url", ""),
            "api_key": primary_key,
            "model": primary.get("model", "gpt-4o"),
            "success_rate": 1.0,
            "is_builtin_default": False,
            "last_used_at": "",
            "is_primary": True,  # 标记为主配置
        }
    ]

    seen_urls = {primary_url} if primary_url else set()


    # 第1.5层：用户已配置的提供商（带真实 API Key，优先于 AI Profile 和 history）
    # 这些是用户明确配置好的备选，可靠性高于无 key 的 history/fallback
    configured_providers = get_configured_providers()
    for cp in configured_providers:
        # Skip the primary one (already added in layer 1)
        norm_cp_url = _normalize_url(cp["base_url"])
        if norm_cp_url in seen_urls:
            continue
        seen_urls.add(norm_cp_url)
        # Put active ones before inactive ones
        if cp.get("is_active"):
            # Insert right after primary
            result.insert(1, cp)
        else:
            result.append(cp)
    if configured_providers:
        logger.info("已加载 %d 个用户配置的备用 AI 提供商", len(configured_providers))

    # 第2层：AI Profile 下的默认配置厂商平台（主配置不可用时的首选降级）
    from .profile_service import get_default_profile

    # 降级链需要真实可用的密钥，因此这里必须取未脱敏的 Profile
    profile = get_default_profile(include_key=True)
    if profile:
        norm_pf_url = _normalize_url(profile.get("base_url", ""))
        if norm_pf_url and norm_pf_url not in seen_urls:
            seen_urls.add(norm_pf_url)
            result.append(
                {
                    "name": profile.get("provider_name") or "AI Profile 默认配置",
                    "provider": profile.get("provider", "openai_compatible"),
                    "base_url": profile.get("base_url", ""),
                    "api_key": profile.get("api_key", ""),
                    "model": profile.get("model") or "gpt-4o",
                    "success_rate": 1.0,
                    "is_builtin_default": False,
                    "is_primary": False,
                    "last_used_at": "",
                }
            )

    # 第3层：历史记录中的厂商（排除已出现过的 URL）
    for hp in get_history_providers():
        norm_hp_url = _normalize_url(hp["base_url"])
        if norm_hp_url not in seen_urls:
            seen_urls.add(norm_hp_url)
            hp["is_primary"] = False
            result.append(hp)

    # 第4层：内置兜底
    for fb in _FALLBACK_PROVIDERS:
        norm_fb_url = _normalize_url(fb["base_url"])
        if norm_fb_url not in seen_urls:
            seen_urls.add(norm_fb_url)
            result.append(
                {
                    **fb,
                    "success_rate": 1.0,
                    # L-2（2026-10-04 遗留闭环发现）：此前无条件写 True，覆盖了
                    # _FALLBACK_PROVIDERS 自身的 is_builtin_default 标记（仅 agnes
                    # 为 True）。下游（/provider-history 的 success/failed 展示、
                    # get_working_provider 的无 key 跳过逻辑）语义应为「是否内置
                    # 默认厂商」，按条目自身标记透传才正确。
                    "is_builtin_default": fb.get("is_builtin_default", False),
                    "is_primary": False,
                }
            )

    return result


# ──────────────────────────────────────────────────────────────────────
# 配置加载
# ──────────────────────────────────────────────────────────────────────
def _load_env_config() -> dict | None:
    """从环境变量加载 AI 配置（优先级最高）"""
    if os.getenv("AI_API_KEY"):
        return {
            "api_key": os.getenv("AI_API_KEY", ""),
            "base_url": os.getenv("AI_BASE_URL", "https://api.openai.com/v1"),
            "model": os.getenv("AI_MODEL", "gpt-4o"),
            "provider": detect_provider_type(os.getenv("AI_BASE_URL", "https://api.openai.com/v1")),
            "is_builtin_default": False,
        }
    if os.getenv("DEEPSEEK_API_KEY"):
        return {
            "provider": "deepseek",
            "api_key": os.getenv("DEEPSEEK_API_KEY"),
            "base_url": os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
            "model": os.getenv("DEEPSEEK_MODEL", "deepseek-chat"),
            "is_builtin_default": False,
        }
    if os.getenv("OPENAI_API_KEY"):
        return {
            "provider": "openai_compatible",
            "api_key": os.getenv("OPENAI_API_KEY"),
            "base_url": os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"),
            "model": os.getenv("OPENAI_MODEL", "gpt-4o"),
            "is_builtin_default": False,
        }
    return None


def _load_active_profile_limits() -> dict:
    """读取当前激活的 AI Profile 中的限流相关配置。

    BUG-FIX 2026-10-07：`ai_profiles` 表此前**只写不读** ——
    `profile_service.save_profile` 写入、`routers/ai_config.py` 对外暴露给前端，
    但生成链路从未消费过它。结果是用户在界面上配置的
    `rate_limit_per_min` / `token_budget_daily` 完全不生效，
    与 `logs/backend.log` 中持续 7 分钟、累计 528 次的 HTTP 429 直接相关。

    这里只取限流语义明确的字段；`concurrency` 仅在主配置未显式设置时才作为兜底，
    避免覆盖用户在「AI 配置」页的选择。

    表可能不存在于历史数据库，故所有异常都降级为空结果（缺省即不限流）。
    """
    try:
        with get_db_with_retry() as conn:
            row = conn.execute(
                "SELECT rate_limit_per_min, concurrency FROM ai_profiles "
                "WHERE is_active = 1 ORDER BY id LIMIT 1"
            ).fetchone()
        if not row:
            return {}
        return {
            "rate_limit_per_min": int(row["rate_limit_per_min"] or 0),
            "concurrency": int(row["concurrency"] or 0),
        }
    except Exception as e:
        logger.debug("[AI配置] 读取 ai_profiles 限流配置失败（忽略）: %s", e)
        return {}


def get_ai_config() -> dict:
    global _ai_config_cache, _ai_config_cache_time

    now = time.time()
    with _ai_config_lock:
        if _ai_config_cache and (now - _ai_config_cache_time) < _AI_CONFIG_TTL:
            # L-3：TTL 内命中也要比对库内版本号（5 秒防抖），
            # 否则多进程部署下另一进程写入的新配置最长 1 小时不可见。
            if not _external_version_changed(now):
                # F-9 防御：返回拷贝而非共享引用 —— 调用方（SSE 链、健康检查）历史上
                # 会对返回 dict 写回临时字段，污染缓存后影响其他请求。
                return dict(_ai_config_cache)
            _ai_config_cache = None

        config = dict(_AI_CONFIG_DEFAULTS)

        with get_db_with_retry() as conn:
            row = conn.execute("SELECT * FROM ai_config WHERE id = 1").fetchone()
            if row:
                db_config = dict(row)
                # 兜底：model 为空时用旧字段 model_name 迁移过来（兼容历史数据库）
                if not db_config.get("model") and db_config.get("model_name"):
                    db_config["model"] = db_config["model_name"]
                    logger.info(
                        "[兼容] ai_config.model 为空，已从旧字段 model_name='%s' 迁移",
                        db_config["model_name"],
                    )
                if db_config.get("api_key") and is_encrypted(db_config["api_key"]):
                    try:
                        db_config["api_key"] = decrypt_api_key(db_config["api_key"])
                    except Exception as e:
                        # 修复：解密失败不应静默，否则加密串会被直接发给 AI API 导致 401 难排查
                        logger.warning(
                            "AI配置主配置的 API Key 解密失败（Fernet 密钥可能变更）：%s", e
                        )
                is_still_default = (
                    db_config.get("provider_name") == _AI_CONFIG_DEFAULTS["provider_name"]
                    and db_config.get("base_url") == _AI_CONFIG_DEFAULTS["base_url"]
                    and db_config.get("api_key") == _AI_CONFIG_DEFAULTS["api_key"]
                    and db_config.get("model") == _AI_CONFIG_DEFAULTS["model"]
                )
                if not is_still_default:
                    db_config["is_builtin_default"] = False
                    config.update(db_config)
                elif is_still_default:
                    for k in (
                        "max_tokens",
                        "temperature",
                        "concurrency",
                        "timeout",
                        "document_profile",
                    ):
                        if k in db_config and db_config[k] is not None:
                            config[k] = db_config[k]

        env_config = _load_env_config()
        if env_config:
            config.update(env_config)

        # BUG-FIX 2026-10-07：把 ai_profiles 中配置的每分钟请求上限接入生成链路。
        # 优先级低于 ai_config / 环境变量，仅填补缺口。
        _profile_limits = _load_active_profile_limits()
        if _profile_limits.get("rate_limit_per_min") and not config.get("rate_limit_per_min"):
            config["rate_limit_per_min"] = _profile_limits["rate_limit_per_min"]
            logger.info(
                "[AI配置] 已启用 Profile 限流: rate_limit_per_min=%d",
                _profile_limits["rate_limit_per_min"],
            )
        if _profile_limits.get("concurrency") and not config.get("concurrency"):
            config["concurrency"] = _profile_limits["concurrency"]

        if not config.get("provider"):
            config["provider"] = detect_provider_type(config.get("base_url", ""))

        # ai_config 的 api_key 为空时，回退到 text_provider_configs（is_active=1）
        if not config.get("api_key"):
            with get_db() as conn:
                tpc = conn.execute(
                    "SELECT * FROM text_provider_configs WHERE is_active = 1 LIMIT 1"
                ).fetchone()
                if tpc:
                    tpc_dict = dict(tpc)
                    if tpc_dict.get("api_key") and is_encrypted(tpc_dict["api_key"]):
                        try:
                            tpc_dict["api_key"] = decrypt_api_key(tpc_dict["api_key"])
                        except Exception as e:
                            # 修复：解密失败不应静默，否则加密串会被直接发给 AI API 导致 401 难排查
                            logger.warning(
                                "AI配置厂商 %s 的 API Key 解密失败：%s",
                                tpc_dict.get("provider_name", "?"),
                                e,
                            )
                    if tpc_dict.get("api_key"):
                        config.update(
                            {
                                k: tpc_dict[k]
                                for k in (
                                    "provider_name",
                                    "api_key",
                                    "base_url",
                                    "model",
                                    "concurrency",
                                    "max_tokens",
                                    "document_profile",
                                )
                                if k in tpc_dict and tpc_dict[k] is not None
                            }
                        )
                        logger.info(
                            "从 text_provider_configs 回退加载 ai_config api_key: %s",
                            tpc_dict.get("provider_name"),
                        )

        # 前端不再接收 is_builtin_default，始终允许自由编辑配置
        config.pop("is_builtin_default", None)

        _ai_config_cache = config
        _ai_config_cache_time = time.time()
        return dict(config)


# ──────────────────────────────────────────────────────────────────────
# Provider 创建（核心降级逻辑）
# ──────────────────────────────────────────────────────────────────────
def create_provider(config: dict | None = None) -> Any:
    """创建 AI Provider 实例（不使用降级，仅用于已知健康的场景）"""
    if config is None:
        config = get_ai_config()

    api_key = config.get("api_key", "")
    if not api_key:
        raise HTTPException(
            status_code=400,
            detail="未配置 AI 服务，请先在设置页面配置 API Key",
        )

    # 防御：model 为空时回退到厂商默认值
    model = config.get("model") or _AI_CONFIG_DEFAULTS.get("model", "gpt-4o")

    return _make_provider(
        api_key=api_key,
        base_url=config.get("base_url", ""),
        model=model,
        max_tokens=config.get("max_tokens", 16384),
        temperature=config.get("temperature", 0.7),
        timeout=config.get("timeout", 120),
        max_retries=config.get("max_retries", 2),
        rate_limit_per_min=config.get("rate_limit_per_min", 0),
        concurrency=config.get("concurrency", 0),
    )


def record_usage_from_provider(
    provider: Any,
    task_type: str = "unknown",
    profile_name: str = "default",
) -> None:
    """从 Provider 实例提取最近一次调用的 token 用量并记录统计"""
    try:
        usage = getattr(provider, "last_usage", {})
        if not usage:
            return
        response_time_ms = getattr(provider, "last_response_time_ms", 0)
        from .usage_stats import record_from_response

        record_from_response(
            provider=getattr(provider, "api_key", "")[:8] or "unknown",
            model=getattr(provider, "model", "unknown"),
            base_url=getattr(provider, "base_url", ""),
            response_data={"usage": usage},
            response_time_ms=response_time_ms,
            task_type=task_type,
            success=True,
            profile_name=profile_name,
        )
    except Exception as e:
        logger.debug("记录 usage 统计失败: %s", e)


def create_provider_with_fallback(config: dict | None = None) -> tuple[Any, str]:
    """创建 AI Provider 实例，不可用时自动降级

    优先级策略：
      1. 当前 ai_config 中保存的主配置（最后输入的厂商）—— 优先尝试
      2. provider_history 中记录的最近配置厂商
      3. 内置兜底提供商

    返回:
        (provider, used_source): provider 为可用的 Provider 实例，
        used_source 为来源名称（空字符串 = 主配置，否则为降级来源名称）
    """
    if config is None:
        config = get_ai_config()

    api_key = config.get("api_key", "")
    if not api_key:
        raise HTTPException(
            status_code=400,
            detail="未配置 AI 服务，请先在设置页面配置 API Key",
        )

    primary_url = config.get("base_url", "")
    timeout = config.get("timeout", 120)
    model = (config.get("model") or _AI_CONFIG_DEFAULTS.get("model", "gpt-4o"))

    # 构建完整降级链
    priority_chain = get_provider_priority_order()

    # 依次尝试
    for entry in priority_chain:
        url = entry["base_url"]
        if not url:
            continue

        # F-4 BUG修复：此前所有降级条目统一用主配置的 key（注释自曝「历史记录中
        # 没有密钥」），但第1.5层 text_provider_configs 备选厂商各有自己的密钥 ——
        # 把 A 厂商的 key 发到 B 厂商 URL 必然 401，降级到带 key 的备选时健检也永远
        # 失败。现按厂商取自身密钥，无自身密钥（history/兜底）才沿用主配置 key。
        key = entry.get("api_key") or api_key
        # TD-3（2026-10-06）：与异步 get_working_provider 语义对齐 —— 条目既无
        # 自身密钥、又不是内置默认（agnes，自带 AGNES_API_KEY）时，拿主配置 key
        # 去请求别的厂商 URL 必然 401，白等一次 5s 超时。直接跳过。
        if not key and not entry.get("is_builtin_default"):
            logger.debug(
                "Provider %s 无自身密钥且非内置默认，跳过（与异步链对齐）",
                entry.get("name", url),
            )
            continue
        model_to_use = entry.get("model", model)

        try:
            # 尝试健康检查（短 TTL 缓存：命中则跳过重复 HTTP 探活）
            cache_key = _health_cache_key(url, model_to_use, key)
            cached_ok = _health_cache_get(cache_key)
            if cached_ok is None:
                healthy = _quick_health_check(url, key, model_to_use, timeout)
                _health_cache_put(cache_key, healthy)
            else:
                healthy = cached_ok
            if healthy:
                logger.info("使用 provider: %s (url=%s)", entry.get("name", "主配置"), url)
                provider = _make_provider(
                    api_key=key,
                    base_url=url,
                    model=model_to_use,
                    max_tokens=config.get("max_tokens", 32768),
                    temperature=config.get("temperature", 0.7),
                    timeout=timeout,
                    max_retries=2,
                )
                is_primary = (url == primary_url) or entry.get("is_primary", False)
                source = "" if is_primary else entry["name"]
                return provider, source
            else:
                logger.debug("Provider %s 健康检查失败", entry.get("name", url))
        except Exception as e:
            logger.debug("Provider %s 初始化失败: %s", entry.get("name", url), type(e).__name__)

    # 全部失败，返回主配置
    logger.warning("所有 AI 提供商均不可用，回退到主配置 %s", primary_url)
    return _make_provider(
        api_key=api_key,
        base_url=primary_url,
        model=model,
        max_tokens=config.get("max_tokens", 32768),
        temperature=config.get("temperature", 0.7),
        timeout=timeout,
        max_retries=2,
    ), ""


def _quick_health_check(base_url: str, api_key: str, model: str, timeout: int) -> bool:
    """快速 HTTP 健康检查（同步版）"""
    import httpx

    if not model:
        model = _AI_CONFIG_DEFAULTS.get("model", "gpt-4o")

    try:
        # F-NEW-1（2026-10-06）：此前同步探活直接构造 httpx.Client 未走
        # build_httpx_proxy_settings，而异步版 get_working_provider/quick-setup
        # 全部走代理 —— 配置 HTTPS_PROXY 后，Celery 任务侧（本函数经
        # asyncio.to_thread 调用）的降级链探活会全部连不通，与异步链行为
        # 漂移。这里对齐：无代理配置时返回 {}，**展开为空，行为不变。
        with httpx.Client(
            timeout=httpx.Timeout(5, connect=3, read=5),
            **build_httpx_proxy_settings(),
        ) as client:
            resp = client.post(
                base_url.rstrip("/") + "/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": "请回复'OK'"}],
                    "max_tokens": 4,
                },
            )
            return resp.status_code == 200
            # TD-3（2026-10-06）：429（限流）此前被视为健康，会选中该厂商后在真正
            # 调用时被拒；与异步 get_working_provider 的 429=跳过 语义对齐。
            # 失败结论走 _health_cache 15s 短 TTL，厂商恢复后能尽快重选。
    except Exception:
        return False


async def get_working_provider(primary_config: dict) -> tuple[Any, str]:
    """异步版降级链（供 FastAPI 路由使用）

    优先级：
      1. 当前保存的配置（primary_config）
      2. provider_history 中记录的最近配置厂商
      3. 内置兜底提供商

    返回:
        (provider, used_fallback_name): provider 为可用的 Provider 实例，
        used_fallback_name 为降级到的 provider 名称（空字符串表示使用主配置）
    """
    import httpx

    api_key = primary_config.get("api_key", "")
    if not api_key:
        raise HTTPException(
            status_code=400,
            detail="未配置 AI 服务，请先在设置页面配置 API Key",
        )

    primary_url = primary_config.get("base_url", "")
    timeout = primary_config.get("timeout", 120)

    # 构建完整降级链
    priority_chain = get_provider_priority_order()

    # 依次尝试
    for entry in priority_chain:
        url = entry["base_url"]
        if not url:
            continue
        key = entry.get("api_key", api_key)
        if not key:
            if not entry.get("is_builtin_default"):
                continue

        model = entry.get("model") or primary_config.get("model") or _AI_CONFIG_DEFAULTS.get("model", "gpt-4o")

        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(8, connect=5, read=8), **build_httpx_proxy_settings()
            ) as client:
                resp = await client.post(
                    url.rstrip("/") + "/chat/completions",
                    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                    json={
                        "model": model,
                        "messages": [{"role": "user", "content": "hi"}],
                        "max_tokens": 4,
                    },
                )
                if resp.status_code == 200:
                    is_primary = (url == primary_url) or entry.get("is_primary", False)
                    source = "" if is_primary else entry["name"]
                    logger.info("使用 provider: %s (url=%s)", source or "主配置", url)
                    provider = _make_provider(
                        api_key=key,
                        base_url=url,
                        model=model,
                        max_tokens=primary_config.get("max_tokens", 32768),
                        temperature=primary_config.get("temperature", 0.7),
                        timeout=timeout,
                        max_retries=2,
                    )
                    return provider, source
                elif resp.status_code == 429:
                    # 429 = 限流，这个 provider 当前不可用，跳过尝试下一个
                    retry_after = resp.headers.get("Retry-After", "?")
                    logger.warning("Provider %s 返回 429 限流 (Retry-After=%ss)，跳过", entry.get("name", url), retry_after)
                    continue
                elif resp.status_code == 401:
                    logger.info("Provider %s 返回 401，跳过", entry.get("name", url))
        except Exception as e:
            logger.debug("Provider %s 不可用: %s", entry.get("name", url), type(e).__name__)
            continue

    # 全部失败，返回主配置
    logger.warning("所有 AI 提供商均不可用，回退到主配置 %s", primary_url)
    return _make_provider(
        api_key=api_key,
        base_url=primary_url,
        model=primary_config.get("model") or _AI_CONFIG_DEFAULTS.get("model", "gpt-4o"),
        max_tokens=primary_config.get("max_tokens", 32768),
        temperature=primary_config.get("temperature", 0.7),
        timeout=timeout,
        max_retries=2,
    ), ""


async def try_provider_health_check(config: dict) -> bool:
    """快速探测 Provider 是否可达（非阻塞，超时极短）"""
    import httpx

    api_key = config.get("api_key", "")
    base_url = config.get("base_url", "")
    model = config.get("model") or _AI_CONFIG_DEFAULTS.get("model", "gpt-4o")

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(5, connect=3, read=5), **build_httpx_proxy_settings()
        ) as client:
            resp = await client.post(
                base_url.rstrip("/") + "/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": "hi"}],
                    "max_tokens": 4,
                },
            )
            return resp.status_code in (200, 429)
    except Exception:
        return False
