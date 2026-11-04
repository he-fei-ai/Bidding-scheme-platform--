"""提示词治理项持久化与热更新（2026-11-04）。

背景（本轮收尾 P1）：``PROMPT_DESENSITIZE_DEFAULT`` / ``PROMPT_SANITIZE_DEFAULT``
/ ``PROMPT_INJECTION_WRAP_THRESHOLD`` / ``PROMPT_CONTEXT_MAX_TOKENS`` 四项治理
配置此前只在进程导入时从环境变量求值（``config.py`` 模块级常量），运行期
无法调整、无审计、改值必须重启。本模块把它们迁到 DB：

  - ``ai_runtime_settings``       当前生效值（键值 + 类型 + 更新人/时间）
  - ``ai_runtime_settings_audit`` 变更审计（旧值/新值/操作人/时间/动作）

设计约束（与需求一一对应）：

1. **默认行为完全不变**：DB 无记录时回落 ``config.py`` 中的环境变量默认值
   （脱敏/清洗默认关、注入包裹阈值 3、上下文预算 0=不限）。因此不写 DB 的
   部署与改造前逐字节一致。
2. **旧库自动升级**：``ensure_schema`` 使用 ``CREATE TABLE IF NOT EXISTS`` +
   ``_ensure_column`` 语义，老库启动时自动建表/补列，幂等可重复执行。
3. **改后立即生效**：读侧带进程级缓存（2 秒 TTL），任何写/回滚在**同一进程**
   内立即清空缓存；其它进程最迟 TTL 后收敛。缓存读取点已接到真实渲染路径
   （``_cache.get_prompt`` / ``_security.sanitize_for_injection`` /
   ``_context._default_context_max_tokens``）。
4. **审计与回滚**：每次写入记录 old/new/changed_by/changed_at；回滚按审计行
   把值恢复为 ``old_value``（``old_value`` 为 NULL 表示「删除 DB 行、回到环境
   变量默认」），并为回滚本身追加一条审计。

注意：本模块只依赖标准库 + 惰性导入 ``app.db``，避免与提示词包其它模块形成
导入环（``_cache`` / ``_security`` / ``_context`` 都会导入本模块）。
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any


logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────
# 配置项注册表
# ──────────────────────────────────────────────────────────────────────
# env_default 为「config 模块中的同名属性」——DB 无记录时即回落该值。
# 键名与既有环境变量保持一致，运维无需记忆第二套命名。


@dataclass(frozen=True)
class SettingDef:
    key: str
    value_type: str  # bool | int
    description: str
    minimum: int | None = None
    maximum: int | None = None


SETTINGS: dict[str, SettingDef] = {
    "PROMPT_DESENSITIZE_DEFAULT": SettingDef(
        key="PROMPT_DESENSITIZE_DEFAULT",
        value_type="bool",
        description=(
            "提示词变量值默认脱敏（邮箱/手机/身份证/银行卡/JWT/口令/IP）。"
            "默认 false（opt-in），与改造前一致。"
        ),
    ),
    "PROMPT_SANITIZE_DEFAULT": SettingDef(
        key="PROMPT_SANITIZE_DEFAULT",
        value_type="bool",
        description=(
            "提示词变量值默认元标记中和（{shared:NAME} / __VAR__）。"
            "默认 false，与改造前一致。"
        ),
    ),
    "PROMPT_INJECTION_WRAP_THRESHOLD": SettingDef(
        key="PROMPT_INJECTION_WRAP_THRESHOLD",
        value_type="int",
        description=(
            "注入风险自动包裹阈值：detect_injection_risk 得分达到该值即用 "
            "<<<USER_DATA_START/END>>> 包裹变量值。0=始终包裹，很大值=关闭。"
        ),
        minimum=0,
        maximum=999,
    ),
    "PROMPT_CONTEXT_MAX_TOKENS": SettingDef(
        key="PROMPT_CONTEXT_MAX_TOKENS",
        value_type="int",
        description="上下文组装默认 Token 预算上限，0 表示不限（保持既有行为）。",
        minimum=0,
        maximum=1_000_000,
    ),
}

_BOOL_TRUE = {"1", "true", "yes", "on"}
_BOOL_FALSE = {"0", "false", "no", "off", ""}

# 缓存 TTL：写操作在本进程内直接失效缓存，跨进程最迟 TTL 后收敛。
# 取值很短（2 秒），兼顾 AI 热路径（每次渲染都要读）与多进程部署一致性。
CACHE_TTL_SECS = 2.0

_cache: dict[str, tuple[Any, float]] = {}
_cache_lock = threading.RLock()


# ──────────────────────────────────────────────────────────────────────
# Schema（幂等）
# ──────────────────────────────────────────────────────────────────────

_CREATE_SETTINGS = """
CREATE TABLE IF NOT EXISTS ai_runtime_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT '',
    value_type TEXT NOT NULL DEFAULT 'string',
    description TEXT NOT NULL DEFAULT '',
    updated_by TEXT NOT NULL DEFAULT 'system',
    updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
)
"""

_CREATE_AUDIT = """
CREATE TABLE IF NOT EXISTS ai_runtime_settings_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL,
    old_value TEXT DEFAULT NULL,
    new_value TEXT DEFAULT NULL,
    action TEXT NOT NULL DEFAULT 'set',
    changed_by TEXT NOT NULL DEFAULT 'system',
    changed_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
)
"""


def ensure_schema(conn) -> None:
    """建表/补列（幂等）。由 ``db.init_db`` 在启动时调用，覆盖旧库升级。"""
    conn.execute(_CREATE_SETTINGS)
    conn.execute(_CREATE_AUDIT)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_ai_runtime_settings_audit_key "
        "ON ai_runtime_settings_audit(key, id)"
    )


# ──────────────────────────────────────────────────────────────────────
# 默认值（环境变量回落）
# ──────────────────────────────────────────────────────────────────────


def _env_default(name: str) -> Any:
    """读取 config 中的同名属性作为 DB 无记录时的默认值。

    config 不可用（极简环境/单测隔离）时按类型给硬编码安全默认：
    bool -> False，int -> 0（但注入阈值历史默认是 3，见下）。
    """
    definition = SETTINGS[name]
    try:
        from .... import config

        raw = getattr(config, name, None)
        if raw is not None:
            return bool(raw) if definition.value_type == "bool" else int(raw)
    except Exception:  # pragma: no cover - 仅在极端隔离环境触发
        pass
    if definition.value_type == "bool":
        return False
    # 注入包裹阈值的历史默认值 3；上下文预算默认 0（不限）
    return 3 if name == "PROMPT_INJECTION_WRAP_THRESHOLD" else 0


# ──────────────────────────────────────────────────────────────────────
# 值转换
# ──────────────────────────────────────────────────────────────────────


def normalize_value(name: str, raw: Any) -> str:
    """把任意输入规范化为可落库的字符串，越界/非法类型抛 ``ValueError``。"""
    definition = SETTINGS.get(name)
    if definition is None:
        raise ValueError(f"未知的治理配置项: {name}")
    if definition.value_type == "bool":
        if isinstance(raw, bool):
            return "true" if raw else "false"
        text = str(raw).strip().lower()
        if text in _BOOL_TRUE:
            return "true"
        if text in _BOOL_FALSE:
            return "false"
        raise ValueError(f"{name} 需要布尔值（true/false），收到 {raw!r}")
    # int
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        raise ValueError(f"{name} 需要整数，收到 {raw!r}") from None
    if definition.minimum is not None and value < definition.minimum:
        raise ValueError(f"{name} 不能小于 {definition.minimum}，收到 {value}")
    if definition.maximum is not None and value > definition.maximum:
        raise ValueError(f"{name} 不能大于 {definition.maximum}，收到 {value}")
    return str(value)


def _typed(name: str, raw: str) -> Any:
    definition = SETTINGS[name]
    if definition.value_type == "bool":
        return str(raw).strip().lower() in _BOOL_TRUE
    return int(raw)


# ──────────────────────────────────────────────────────────────────────
# 读（带缓存）
# ──────────────────────────────────────────────────────────────────────


def invalidate_cache() -> None:
    """清空进程级缓存。写/回滚后调用，保证「改完立即生效」。"""
    with _cache_lock:
        _cache.clear()


def _read_db(name: str) -> str | None:
    """返回 DB 中的原始值；无记录返回 None。DB 异常向上抛出由调用方兜底。"""
    from ....db import get_db

    with get_db(readonly=True) as conn:
        row = conn.execute(
            "SELECT value FROM ai_runtime_settings WHERE key = ?", (name,)
        ).fetchone()
    return None if row is None else row["value"]


def get_setting(name: str) -> Any:
    """读取治理项**当前生效值**（DB 优先，回退环境变量默认）。

    热路径（每次提示词渲染）调用，成功读取后 2 秒内走内存缓存；DB 不可用等
    异常一律回退默认值，绝不让治理配置读取把 AI 链路打挂。
    """
    if name not in SETTINGS:
        raise ValueError(f"未知的治理配置项: {name}")
    now = time.time()
    with _cache_lock:
        cached = _cache.get(name)
        if cached is not None and (now - cached[1]) < CACHE_TTL_SECS:
            return cached[0]
    value: Any
    try:
        raw = _read_db(name)
        value = _env_default(name) if raw is None else _typed(name, raw)
    except Exception as exc:
        # 读失败不缓存，避免把「DB 挂了」的判断固化成 2 秒行为；直接回落默认。
        logger.debug("治理配置读取失败，回落默认值: %s (%s)", name, exc)
        return _env_default(name)
    with _cache_lock:
        _cache[name] = (value, time.time())
    return value


def get_bool(name: str) -> bool:
    return bool(get_setting(name))


def get_int(name: str) -> int:
    return int(get_setting(name))


# ──────────────────────────────────────────────────────────────────────
# 写 + 审计
# ──────────────────────────────────────────────────────────────────────


def _audit(conn, *, key: str, old_value: str | None, new_value: str | None,
           action: str, changed_by: str) -> None:
    conn.execute(
        """INSERT INTO ai_runtime_settings_audit
           (key, old_value, new_value, action, changed_by)
           VALUES (?, ?, ?, ?, ?)""",
        (key, old_value, new_value, action, changed_by or "system"),
    )


def set_setting(name: str, raw: Any, *, changed_by: str = "system") -> dict[str, Any]:
    """写入治理项并记审计，返回写入后的视图。非法值抛 ``ValueError``。"""
    if name not in SETTINGS:
        raise ValueError(f"未知的治理配置项: {name}")
    definition = SETTINGS[name]
    new_value = normalize_value(name, raw)
    from ....db import get_db

    with get_db() as conn:
        row = conn.execute(
            "SELECT value FROM ai_runtime_settings WHERE key = ?", (name,)
        ).fetchone()
        old_value = None if row is None else row["value"]
        conn.execute(
            """INSERT INTO ai_runtime_settings (key, value, value_type, description, updated_by, updated_at)
               VALUES (?, ?, ?, ?, ?, datetime('now', 'localtime'))
               ON CONFLICT(key) DO UPDATE SET
                   value = excluded.value,
                   value_type = excluded.value_type,
                   description = excluded.description,
                   updated_by = excluded.updated_by,
                   updated_at = datetime('now', 'localtime')""",
            (name, new_value, definition.value_type, definition.description, changed_by or "system"),
        )
        _audit(conn, key=name, old_value=old_value, new_value=new_value,
               action="set", changed_by=changed_by)
    invalidate_cache()
    logger.info("治理配置已更新: %s = %s（操作人 %s）", name, new_value, changed_by)
    return get_setting_view(name)


def get_setting_view(name: str) -> dict[str, Any]:
    """单个治理项的完整视图（当前值/来源/更新时间/操作人）。"""
    if name not in SETTINGS:
        raise ValueError(f"未知的治理配置项: {name}")
    definition = SETTINGS[name]
    db_row = None
    try:
        from ....db import get_db

        with get_db(readonly=True) as conn:
            db_row = conn.execute(
                "SELECT value, updated_by, updated_at FROM ai_runtime_settings WHERE key = ?",
                (name,),
            ).fetchone()
    except Exception as exc:
        logger.debug("治理配置视图读取失败: %s (%s)", name, exc)
    value = get_setting(name)
    return {
        "key": name,
        "value": value,
        "raw_value": None if db_row is None else db_row["value"],
        "value_type": definition.value_type,
        "description": definition.description,
        "source": "db" if db_row is not None else "env_default",
        "env_default": _env_default(name),
        "updated_by": None if db_row is None else db_row["updated_by"],
        "updated_at": None if db_row is None else db_row["updated_at"],
    }


def list_settings() -> list[dict[str, Any]]:
    """全部治理项视图（含 DB 未落库、当前走环境变量默认的项）。"""
    return [get_setting_view(name) for name in SETTINGS]


def list_audit(name: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    """变更审计列表（最新在前）。"""
    from ....db import get_db

    limit = max(1, min(int(limit), 500))
    with get_db(readonly=True) as conn:
        if name:
            rows = conn.execute(
                """SELECT id, key, old_value, new_value, action, changed_by, changed_at
                   FROM ai_runtime_settings_audit WHERE key = ?
                   ORDER BY id DESC LIMIT ?""",
                (name, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT id, key, old_value, new_value, action, changed_by, changed_at
                   FROM ai_runtime_settings_audit ORDER BY id DESC LIMIT ?""",
                (limit,),
            ).fetchall()
    return [dict(row) for row in rows]


def rollback(audit_id: int, *, changed_by: str = "system") -> dict[str, Any]:
    """按审计行回滚到变更前的值，并追加一条 ``action='rollback'`` 审计。

    ``old_value`` 为 NULL 表示该次变更是「首次落库」，回滚即删除 DB 行、
    回到环境变量默认值（不写入伪造值）。
    """
    from ....db import get_db

    with get_db() as conn:
        audit_row = conn.execute(
            "SELECT id, key, old_value, new_value FROM ai_runtime_settings_audit WHERE id = ?",
            (int(audit_id),),
        ).fetchone()
        if audit_row is None:
            raise ValueError(f"审计记录不存在: {audit_id}")
        key = audit_row["key"]
        if key not in SETTINGS:
            raise ValueError(f"审计记录对应的配置项已废弃: {key}")
        old_value = audit_row["old_value"]
        current = conn.execute(
            "SELECT value FROM ai_runtime_settings WHERE key = ?", (key,)
        ).fetchone()
        current_value = None if current is None else current["value"]
        if old_value is None:
            conn.execute("DELETE FROM ai_runtime_settings WHERE key = ?", (key,))
        else:
            definition = SETTINGS[key]
            conn.execute(
                """INSERT INTO ai_runtime_settings (key, value, value_type, description, updated_by, updated_at)
                   VALUES (?, ?, ?, ?, ?, datetime('now', 'localtime'))
                   ON CONFLICT(key) DO UPDATE SET
                       value = excluded.value, updated_by = excluded.updated_by,
                       updated_at = datetime('now', 'localtime')""",
                (key, old_value, definition.value_type, definition.description,
                 changed_by or "system"),
            )
        _audit(conn, key=key, old_value=current_value, new_value=old_value,
               action="rollback", changed_by=changed_by)
    invalidate_cache()
    logger.info("治理配置已回滚: %s -> %s（操作人 %s）", key, old_value, changed_by)
    return get_setting_view(key)


__all__ = [
    "SETTINGS",
    "SettingDef",
    "ensure_schema",
    "normalize_value",
    "invalidate_cache",
    "get_setting",
    "get_bool",
    "get_int",
    "set_setting",
    "get_setting_view",
    "list_settings",
    "list_audit",
    "rollback",
]
