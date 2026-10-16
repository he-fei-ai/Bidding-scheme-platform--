"""AI Profile 多配置管理系统

职责：
  1. Profile CRUD（创建/读取/更新/删除）
  2. Profile 切换（设置默认 Profile）
  3. Profile 导入/导出（JSON 格式）
  4. Profile 与当前 AI 配置的联动

使用方式：
  from app.services.ai.profile_service import (
      list_profiles, get_profile, create_profile,
      set_default_profile, import_profiles, export_profiles
  )
"""

from datetime import datetime
import logging

from ...db import get_db
from ..crypto import decrypt_api_key


logger = logging.getLogger(__name__)

# 读接口统一使用的脱敏占位符（与 routers/ai_config.API_KEY_MASK 保持一致）
KEY_MASK = "***已配置***"

# ──────────────────────────────────────────────────────────────────────
# G-1（2026-10-11）：Profile 数值参数范围钳制
# ──────────────────────────────────────────────────────────────────────
# 现状：ProfileCreateRequest/ProfileUpdateRequest 直接落库，未对 temperature /
# max_tokens / concurrency / timeout / rate_limit_per_min / token_budget_daily
# 做任何范围校验 —— 与 save_ai_config / save_text_provider_config 的口径不一致。
# 传入 -1（temperature）或 0（concurrency）会在 apply_profile_to_config 覆盖
# 主配置后导致模型产出乱码/并发挂死；负数 token_budget_daily 会被
# get_token_budget_status 直接判为超限。
# 钳制口径与 save_ai_config 对齐：temperature [0, 2.0]、max_tokens [256, 65536]、
# concurrency [1, 64]；Profile 特有字段的边界见常量。

_PROFILE_RANGE = {
    "temperature": (0.0, 2.0),
    "max_tokens": (256, 65536),
    "concurrency": (1, 64),
    "timeout": (5, 600),
    "rate_limit_per_min": (0, 10000),
    "token_budget_daily": (0, 10_000_000),
}

_PROFILE_DEFAULTS = {
    "temperature": 0.7,
    "max_tokens": 16384,
    "concurrency": 4,
    "timeout": 120,
    "rate_limit_per_min": 0,
    "token_budget_daily": 0,
}


class ConflictError(Exception):
    """閰嶇疆鍐欏叆鍚庝紶鍏冩牸鍙?鍚戒负 409 Conflict(ETag / If-Match 鍙傚墠绯诲垪涓嶈祫鍙?。"""


def _clamp_profile_params(data: dict) -> dict:
    """把 Profile 数值字段钳制到合法区间；非法/缺失字段回填默认值。

    幂等：调用方传入 int/float/None/str，都会输出可写库的 int/float。
    未出现的键不动，供 update_profile 与 COALESCE 语义共存。
    """
    out = dict(data) if data else {}
    for key, (lo, hi) in _PROFILE_RANGE.items():
        raw = out.get(key, _PROFILE_DEFAULTS[key])
        try:
            if raw is None:
                out[key] = _PROFILE_DEFAULTS[key]
                continue
            if isinstance(raw, str):
                if raw.strip() == "":
                    out[key] = _PROFILE_DEFAULTS[key]
                    continue
                raw = float(raw) if "." in raw else int(raw)
            value = float(raw) if key == "temperature" else int(raw)
            out[key] = min(max(value, lo), hi)
        except (TypeError, ValueError):
            out[key] = _PROFILE_DEFAULTS[key]
    return out


def _profile_row_to_dict(row, include_key: bool = False) -> dict:
    """把 ai_profiles 的一行转换为对外字典。

    BUG-FIX 2026-09-17（安全 P0 + 一致性）：
    旧实现只在 `api_key_enc` 是 `ENC:` 密文时才生成 `api_key` 并删除 `api_key_enc`；
    迁移后新写入的 Profile 存的是**明文**，于是：
      1. `api_key` 字段缺失 → `POST /profiles`（response_model 要求 api_key）直接 500；
      2. `api_key_enc` 原样保留 → 明文 API Key 通过 `GET /profiles/{name}`、
         `/profiles/default` 原封不动吐给前端；
      3. `apply_profile_to_config` / `profile_to_provider_config` 只读
         `api_key_enc`，在加密分支下拿到空串 → 应用 Profile 把主配置密钥清空。

    统一在这里收敛：任何情况下都产出 `api_key`，且**绝不**保留 `api_key_enc`。
    `include_key=False`（默认，面向 HTTP）返回占位符；`include_key=True`
    （仅供服务端内部调用）返回可用于真实调用的密钥。
    """
    d = dict(row)
    raw = d.pop("api_key_enc", "") or ""
    if include_key:
        d["api_key"] = decrypt_api_key(raw)  # 明文原样返回，ENC: 密文解密
    else:
        d["api_key"] = KEY_MASK if raw else ""
    return d


# ──────────────────────────────────────────────────────────────────────
# Profile CRUD
# ──────────────────────────────────────────────────────────────────────
def list_profiles(active_only: bool = True) -> list[dict]:
    """列出所有 Profile（不含任何密钥原文）"""
    try:
        with get_db(readonly=True) as conn:
            sql = "SELECT * FROM ai_profiles"
            if active_only:
                sql += " WHERE is_active = 1"
            sql += " ORDER BY is_default DESC, created_at ASC"
            rows = conn.execute(sql).fetchall()
            return [_profile_row_to_dict(r) for r in rows]
    except Exception as e:
        logger.debug("列出 Profile 失败: %s", e)
        return []


def get_profile(profile_name: str, include_key: bool = False) -> dict | None:
    """获取单个 Profile

    include_key=True 时返回真实密钥（仅供服务端内部使用，禁止直接进 HTTP 响应）。
    """
    try:
        with get_db(readonly=True) as conn:
            row = conn.execute(
                "SELECT * FROM ai_profiles WHERE name = ? AND is_active = 1",
                (profile_name,),
            ).fetchone()
            if not row:
                return None
            return _profile_row_to_dict(row, include_key=include_key)
    except Exception as e:
        logger.debug("获取 Profile '%s' 失败: %s", profile_name, e)
        return None


def get_default_profile(include_key: bool = False) -> dict | None:
    """获取当前默认 Profile"""
    try:
        with get_db(readonly=True) as conn:
            row = conn.execute(
                "SELECT * FROM ai_profiles WHERE is_default = 1 AND is_active = 1"
            ).fetchone()
            if not row:
                return None
            return _profile_row_to_dict(row, include_key=include_key)
    except Exception as e:
        logger.debug("获取默认 Profile 失败: %s", e)
        return None


def create_profile(data: dict) -> dict:
    """创建新 Profile"""
    name = data.get("name", "").strip()
    if not name:
        raise ValueError("Profile 名称不能为空")
    if not all(c.isalnum() or c in ("_", "-") for c in name):
        raise ValueError("Profile 名称只能包含字母、数字、下划线和连字符")

    # G-1（2026-10-11）：数值参数钳制到合法区间（口径与 save_ai_config 一致）
    data = _clamp_profile_params(data)

    try:
        with get_db() as conn:
            # 检查是否已存在
            existing = conn.execute("SELECT id FROM ai_profiles WHERE name = ?", (name,)).fetchone()
            if existing:
                raise ValueError(f"Profile '{name}' 已存在")

            api_key = data.get("api_key", "")
            # L-2（2026-10-04 遗留闭环）：新写入统一 ENC: 加密落库。
            # 此前「迁移阶段已统一解密」的前提并无迁移脚本支撑，新密钥实为
            # 明文入库 —— 与 ai_config/text_provider_configs 的加密口径不一致。
            # 库内实测无 ENC: 残留行，加密后读取侧（decrypt_api_key 对明文
            # 透传）对历史数据完全向后兼容。懒导入避免 router→service 循环依赖。
            from ...routers.ai_config import _is_masked_api_key, _encrypt_fresh_key
            # 占位符/空值 = 未提供密钥（新建即空，语义与原 `or ""` 一致）
            if api_key and _is_masked_api_key(api_key):
                api_key = ""
            api_key_enc = _encrypt_fresh_key(api_key)

            is_default = bool(data.get("is_default", False))

            conn.execute(
                """INSERT INTO ai_profiles
                   (name, display_name, description, provider_name, provider, api_key_enc,
                    model, base_url, max_tokens, temperature, concurrency, timeout,
                    json_mode, task_type, rate_limit_per_min, token_budget_daily,
                    is_active, is_default)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                """,
                (
                    name,
                    data.get("display_name", name),
                    data.get("description", ""),
                    data.get("provider_name", "agnes"),
                    data.get("provider", "openai_compatible"),
                    api_key_enc,
                    data.get("model", "gpt-4o"),
                    data.get("base_url", ""),
                    data.get("max_tokens", 16384),
                    data.get("temperature", 0.7),
                    data.get("concurrency", 4),
                    data.get("timeout", 120),
                    1 if data.get("json_mode", False) else 0,
                    data.get("task_type", ""),
                    data.get("rate_limit_per_min", 0),
                    data.get("token_budget_daily", 0),
                    1 if is_default else 0,
                ),
            )

            # 如果设为默认，取消其他默认
            if is_default:
                conn.execute("UPDATE ai_profiles SET is_default = 0 WHERE name != ?", (name,))
                # 同步 ai_config 表
                conn.execute("UPDATE ai_config SET current_profile_name = ? WHERE id = 1", (name,))

            conn.commit()
            # L-3：Profile 是降级链第三层来源，写入后同样递增版本号跨进程失效
            from .provider_factory import bump_ai_config_version
            bump_ai_config_version()
            logger.info("创建 Profile: %s", name)
            return get_profile(name) or {}
    except ValueError:
        raise
    except Exception as e:
        logger.error("创建 Profile 失败: %s", e)
        raise


def update_profile(name: str, data: dict, if_match: int | None = None) -> dict:
    """更新 Profile

    if_match 钳制负褰撳嶇疆鐗堟湰鍙?(ETag)。鍐欏叆涔熷悗鍐欏叆, 鍐欑幇鍚庯祫婧愮幇鍦ㄥ彲鍦?409 Conflict。
    """
    # 鍙傚墠绯诲垪(If-Match / etag): 鍐欏叆涔熷悗鍐欏叆, 鍐欑幇鍚庯紝祫婧愮幇鍦ㄥ彲鍦?409
    if if_match is not None:
        from .provider_factory import get_ai_config_version
        try:
            if int(if_match) != get_ai_config_version():
                raise ConflictError(
                    "閰嶇疆巳鏈夋牸鍐欏叆(etag=%s)锛屼笉鍙堟暟鎹勫垎鍐欏叆 Profile '%s'" % (if_match, name)
                )
        except (TypeError, ValueError):
            raise ConflictError("If-Match 鍙傚墠绯诲垪閿欒")
    # G-1（2026-10-11）：只钳制 data 中实际提供的数值字段；未提供的保持
    # None（COALESCE 会保留库内原值），不覆盖用户「仅改一个字段」的意图。
    clamped = _clamp_profile_params(data)
    out: dict = {}
    for key, (lo, hi) in _PROFILE_RANGE.items():
        if data.get(key) is None:
            # 未提供 → 保持 COALESCE 原值（不写死默认值）
            continue
        out[key] = clamped[key]
    data = {**data, **out}
    try:
        with get_db() as conn:
            existing = conn.execute("SELECT id FROM ai_profiles WHERE name = ?", (name,)).fetchone()
            if not existing:
                raise ValueError(f"Profile '{name}' 不存在")

            api_key = data.get("api_key", "")
            # BUG-FIX 2026-09-17：空字符串此前会被 `COALESCE('', api_key_enc)`
            # 判定为非 NULL 而写入空值 —— 前端「留空则保持原值」的语义下，
            # 用户只改模型/温度就会把已保存的密钥清空。
            # 现在：显式 `_keep_key`、空值、或未提供 → 传 None 让 COALESCE 保持原值。
            # L-2 配套修复（2026-10-04）：读接口改脱敏后，前端「只改模型不动密钥」
            # 回传的是占位符 —— 旧代码会把字面量 "***已配置***" 写进库，
            # 所有 AI 调用拿占位符去鉴权（401 且难排查）。占位符同样归入保持原值。
            from ...routers.ai_config import _is_masked_api_key
            if (
                data.get("_keep_key")
                or not (api_key or "").strip()
                or _is_masked_api_key(api_key)
            ):
                api_key_enc = None
            else:
                # L-2（2026-10-04 遗留闭环）：新密钥 ENC: 加密后落库，与主配置/
                # 厂商配置写路径口径一致；占位符回填的库内原值（密文/历史明文）
                # 由 _encrypt_fresh_key 幂等透传，不会二次加密也不会降级为明文。
                from ...routers.ai_config import _encrypt_fresh_key
                api_key_enc = _encrypt_fresh_key(api_key)

            is_default = bool(data.get("is_default", False))

            conn.execute(
                """UPDATE ai_profiles SET
                    display_name = COALESCE(?, display_name),
                    description = COALESCE(?, description),
                    provider_name = COALESCE(?, provider_name),
                    provider = COALESCE(?, provider),
                    api_key_enc = COALESCE(?, api_key_enc),
                    model = COALESCE(?, model),
                    base_url = COALESCE(?, base_url),
                    max_tokens = COALESCE(?, max_tokens),
                    temperature = COALESCE(?, temperature),
                    concurrency = COALESCE(?, concurrency),
                    timeout = COALESCE(?, timeout),
                    json_mode = COALESCE(?, json_mode),
                    task_type = COALESCE(?, task_type),
                    rate_limit_per_min = COALESCE(?, rate_limit_per_min),
                    token_budget_daily = COALESCE(?, token_budget_daily),
                    updated_at = datetime('now', 'localtime')
                WHERE name = ?
                """,
                (
                    data.get("display_name"),
                    data.get("description"),
                    data.get("provider_name"),
                    data.get("provider"),
                    api_key_enc,
                    data.get("model"),
                    data.get("base_url"),
                    data.get("max_tokens"),
                    data.get("temperature"),
                    data.get("concurrency"),
                    data.get("timeout"),
                    # BUG-FIX 2026-09-17：json_mode 未传时旧实现固定写 0，
                    # 配合 COALESCE 会导致「只改温度」把 json_mode 悄悄关掉。
                    None if data.get("json_mode") is None else (1 if data.get("json_mode") else 0),
                    data.get("task_type"),
                    data.get("rate_limit_per_min"),
                    data.get("token_budget_daily"),
                    name,
                ),
            )

            if is_default:
                conn.execute("UPDATE ai_profiles SET is_default = 0 WHERE name != ?", (name,))
                conn.execute("UPDATE ai_config SET current_profile_name = ? WHERE id = 1", (name,))

            conn.commit()
            from .provider_factory import bump_ai_config_version
            bump_ai_config_version()
            logger.info("更新 Profile: %s", name)
            return get_profile(name) or {}
    except ValueError:
        raise
    except Exception as e:
        logger.error("更新 Profile 失败: %s", e)
        raise


def delete_profile(name: str) -> bool:
    """删除 Profile"""
    try:
        with get_db() as conn:
            existing = conn.execute(
                "SELECT id, is_default FROM ai_profiles WHERE name = ?", (name,)
            ).fetchone()
            if not existing:
                return False

            conn.execute("UPDATE ai_profiles SET is_active = 0 WHERE name = ?", (name,))
            # 如果被删除的是默认 Profile，把默认位让给其余仍激活的 Profile。
            # BUG-FIX 2026-09-17：旧实现硬编码回退到名为 'default' 的记录，
            # 而该记录可能不存在或已被软删 —— 结果平台进入「无任何默认 Profile」状态，
            # provider 降级链因此丢失第 2 层。
            if existing["is_default"]:
                fallback_row = conn.execute(
                    "SELECT name FROM ai_profiles WHERE is_active = 1 AND name != ?"
                    " ORDER BY id LIMIT 1",
                    (name,),
                ).fetchone()
                fallback = fallback_row["name"] if fallback_row else None
                conn.execute("UPDATE ai_profiles SET is_default = 0")
                if fallback:
                    conn.execute(
                        "UPDATE ai_profiles SET is_default = 1 WHERE name = ?", (fallback,)
                    )
                conn.execute(
                    "UPDATE ai_config SET current_profile_name = ? WHERE id = 1",
                    (fallback or "default",),
                )
            conn.commit()
            logger.info("删除 Profile: %s", name)
            return True
    except Exception as e:
        logger.error("删除 Profile 失败: %s", e)
        return False


def set_default_profile(name: str) -> dict | None:
    """设置指定 Profile 为默认"""
    try:
        with get_db() as conn:
            existing = conn.execute(
                "SELECT id FROM ai_profiles WHERE name = ? AND is_active = 1", (name,)
            ).fetchone()
            if not existing:
                raise ValueError(f"Profile '{name}' 不存在或未激活")

            conn.execute("UPDATE ai_profiles SET is_default = 0")
            conn.execute("UPDATE ai_profiles SET is_default = 1 WHERE name = ?", (name,))
            conn.execute("UPDATE ai_config SET current_profile_name = ? WHERE id = 1", (name,))
            conn.commit()
            logger.info("设置默认 Profile: %s", name)
            return get_profile(name)
    except ValueError:
        raise
    except Exception as e:
        logger.error("设置默认 Profile 失败: %s", e)
        raise


def get_active_profile(include_key: bool = False) -> dict | None:
    """获取当前激活的 Profile（对应 ai_config.current_profile_name）"""
    try:
        with get_db(readonly=True) as conn:
            row = conn.execute("SELECT current_profile_name FROM ai_config WHERE id = 1").fetchone()
            profile_name = (row["current_profile_name"] or "default") if row else "default"
            return get_profile(profile_name, include_key=include_key)
    except Exception as e:
        logger.debug("获取当前激活 Profile 失败: %s", e)
        return None


def apply_profile_to_config(profile_name: str) -> dict:
    """将指定 Profile 的配置应用到 ai_config 主配置。

    BUG-FIX 2026-09-17（P0）：旧实现接收一个 dict 并读 `profile["api_key_enc"]`，
    而 `get_profile()` 在密钥为 ENC: 密文时会把该键删掉、只留 `api_key`。
    于是「应用 Profile」实际执行的是 `UPDATE ai_config SET api_key=''` ——
    点一次应用就把平台主密钥清空，之后所有 AI 调用 401。
    改为按 name 重新读取（拿真实密钥），从根上消除字段口径不一致。
    """
    profile = get_profile(profile_name, include_key=True)
    if not profile:
        raise ValueError(f"Profile '{profile_name}' 不存在")
    try:
        with get_db() as conn:
            # S-2 修复（2026-10-06，安全 P0）：include_key=True 拿到的是**解密后的
            # 明文**，旧实现直接写进 ai_config.api_key —— 应用一次 Profile 就把
            # 主配置密钥从 ENC: 降级为明文落库。现经 _encrypt_fresh_key 收口
            # （空串/ENC: 幂等透传，新明文加密），与各写路径口径一致。
            from ...routers.ai_config import _encrypt_fresh_key
            enc_key = _encrypt_fresh_key(profile.get("api_key", ""))
            conn.execute(
                """UPDATE ai_config SET
                    provider_name = ?,
                    provider = ?,
                    api_key = ?,
                    model = ?,
                    base_url = ?,
                    max_tokens = ?,
                    temperature = ?,
                    concurrency = ?,
                    timeout = ?,
                    current_profile_name = ?,
                    updated_at = datetime('now', 'localtime')
                WHERE id = 1
                """,
                (
                    profile["provider_name"],
                    profile["provider"],
                    enc_key,
                    profile["model"],
                    profile["base_url"],
                    profile["max_tokens"],
                    profile["temperature"],
                    profile["concurrency"],
                    profile["timeout"],
                    profile["name"],
                ),
            )
            conn.commit()
            # L-3（2026-10-04）：本函数也可能被非 router 路径调用，在写入侧
            # 递增版本号确保跨进程缓存 ≤5s 收敛（best-effort，不阻断主流程）。
            from .provider_factory import bump_ai_config_version
            bump_ai_config_version()
            return {"status": "ok", "applied_profile": profile["name"]}
    except Exception as e:
        logger.error("应用 Profile 到配置失败: %s", e)
        raise


# ──────────────────────────────────────────────────────────────────────
# 导入/导出
# ──────────────────────────────────────────────────────────────────────
def export_profiles() -> dict:
    """导出所有 Profile 为 JSON。

    BUG-FIX 2026-09-17（安全 P0）：旧实现 `SELECT *` 原样返回 `api_key_enc`，
    而该字段在当前部署下存的是**明文**密钥。前端 `GET /ai/profiles/export`
    会把整包 JSON 直接下载到浏览器磁盘 —— 一次导出即泄露全部厂商密钥。
    现在只导出 `api_key_set` 布尔位，密钥本身一律不落导出文件。
    """
    try:
        with get_db(readonly=True) as conn:
            rows = conn.execute(
                "SELECT * FROM ai_profiles WHERE is_active = 1 ORDER BY is_default DESC, created_at ASC"
            ).fetchall()
            profiles = []
            for r in rows:
                d = _profile_row_to_dict(r)
                raw = bool(d.get("api_key"))
                d.pop("api_key", None)
                d["api_key_set"] = raw
                profiles.append(d)
            return {
                "version": "2.0",
                "exported_at": datetime.now().isoformat(),
                # 明确告知消费方：导出文件不含密钥，导入后需重新填写。
                "keys_included": False,
                "profiles": profiles,
            }
    except Exception as e:
        logger.error("导出 Profile 失败: %s", e)
        raise


def import_profiles(data: dict) -> dict:
    """导入 Profile（JSON 格式，覆盖同名 Profile）"""
    if not isinstance(data, dict) or "profiles" not in data:
        raise ValueError("导入数据格式错误，需要包含 profiles 数组")
    if not isinstance(data["profiles"], list):
        raise ValueError("导入数据格式错误，profiles 必须是数组")

    imported = 0
    updated = 0
    skipped = 0
    for p in data["profiles"]:
        if not isinstance(p, dict):
            skipped += 1
            continue
        name = p.get("name")
        if not name:
            skipped += 1
            continue
        payload = dict(p)
        # BUG-FIX 2026-09-17：v1.0 导出文件把密钥放在 `api_key_enc` 里，
        # 而 create/update 读的是 `api_key` —— 导入后所有密钥静默丢失。
        if not payload.get("api_key") and payload.get("api_key_enc"):
            payload["api_key"] = payload["api_key_enc"]
        try:
            existing = get_profile(name)
            if existing:
                update_profile(name, payload)
                updated += 1
            else:
                create_profile(payload)
                imported += 1
        except Exception as e:
            logger.warning("导入 Profile '%s' 失败: %s", name, e)
            skipped += 1

    return {
        "imported": imported,
        "updated": updated,
        "skipped": skipped,
        "total": len(data["profiles"]),
    }


# ──────────────────────────────────────────────────────────────────────
# 从 Profile 创建 Provider 配置
# ──────────────────────────────────────────────────────────────────────
def profile_to_provider_config(profile_name: str | None = None) -> dict:
    """将 Profile 转换为 Provider 工厂所需的配置字典"""
    if profile_name:
        profile = get_profile(profile_name, include_key=True)
    else:
        active = get_active_profile(include_key=True)
        profile = active or get_default_profile(include_key=True)

    if not profile:
        from .provider_factory import _AI_CONFIG_DEFAULTS

        return dict(_AI_CONFIG_DEFAULTS)

    return {
        "provider_name": profile["provider_name"],
        "provider": profile["provider"],
        # BUG-FIX 2026-09-17：与 apply_profile_to_config 同源的问题 ——
        # 只读 `api_key_enc` 会拿到空串（密文分支下该键已被移除）。
        "api_key": profile.get("api_key") or decrypt_api_key(profile.get("api_key_enc", "")),
        "model": profile["model"],
        "base_url": profile["base_url"],
        "max_tokens": profile["max_tokens"],
        "temperature": profile["temperature"],
        "concurrency": profile["concurrency"],
        "timeout": profile["timeout"],
        "json_mode": bool(profile.get("json_mode", 0)),
        "task_type": profile.get("task_type", ""),
        "profile_name": profile["name"],
        "is_builtin_default": profile["name"] == "default",
    }
