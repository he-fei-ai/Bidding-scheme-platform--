"""API Key 加密/解密工具

使用 Fernet (AES-128-CBC + HMAC-SHA256) 对数据库中存储的 API Key 进行对称加密。
- 密钥来源优先级（D-3 加固）：
    1. 环境变量 ``BID_ASSISTANT_KEY``（base64 编码的 32 字节密钥）—— 生产推荐
    2. 环境变量 ``BID_ASSISTANT_KEY_FILE`` 指向的密钥文件 —— 多实例共享同一
       密钥文件的部署形态（各实例自建 .secret_key 会导致彼此无法解密，见下）
    3. 默认路径 ``backend/.secret_key``（历史兼容）
    4. 以上都没有 → 自动生成并写入默认路径，且**告警升级**
- 解密失败时返回原文 (向后兼容未加密的历史数据)
- ``rotate_master_key()`` 支持主密钥轮换（配合 scripts/rotate_api_key_encryption.py）
"""

import base64
import logging
import os
import stat
from pathlib import Path


logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
KEY_FILE_PATH = PROJECT_ROOT / ".secret_key"

# BUG-FIX（安全 P1）：旧实现的 PROJECT_ROOT 少退一层，密钥实际落在
# `backend/backend/.secret_key` —— 该路径不被 .gitignore（只写了 `backend/.secret_key`）
# 覆盖，随时可能随 `git add .` 入库，一旦入库则库内所有 ENC: 密文都可被解密。
# 这里保留旧路径作为**只读兜底**，确保历史部署升级后仍能解密已有数据。
LEGACY_KEY_FILE_PATH = PROJECT_ROOT / "backend" / ".secret_key"

_fernet_instance = None
#: 主密钥来源标记：env / key_file / legacy_file / generated（供运维观测与告警）
_key_source: str | None = None


def _key_source_name() -> str:
    return _key_source or "unresolved"


def _warn_if_world_readable(path: Path) -> None:
    """POSIX 下校验密钥文件权限；组/其他可读即告警（D-3 加固）。

    Windows 的 os.chmod 语义与 POSIX 不同（只切换只读位），因此仅在非 Windows
    平台做该校验，避免误报。
    """
    if os.name == "nt":
        return
    try:
        mode = path.stat().st_mode
    except Exception as e:  # pragma: no cover - 极端文件系统异常
        logger.debug("读取密钥文件权限失败 %s: %s", path, e)
        return
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        logger.warning(
            "密钥文件 %s 权限过宽（当前 %o），建议 chmod 600：%s",
            path,
            stat.S_IMODE(mode),
            path,
        )


def _get_or_create_fernet():
    """获取或创建 Fernet 实例(单例)"""
    global _fernet_instance, _key_source
    if _fernet_instance is not None:
        return _fernet_instance

    from cryptography.fernet import Fernet

    key: bytes | None = None
    env_key = os.environ.get("BID_ASSISTANT_KEY")
    if env_key:
        try:
            key = base64.urlsafe_b64decode(env_key.encode("utf-8"))
            if len(key) != 32:
                logger.warning("BID_ASSISTANT_KEY 长度不是 32 字节,将重新生成")
                key = None
            else:
                _key_source = "env"
        except Exception as e:
            logger.warning("BID_ASSISTANT_KEY 解析失败: %s,将重新生成", e)
            key = None

    if key is None:
        # D-3：显式密钥文件（多实例共享同一密钥文件的部署形态）
        shared_file = os.environ.get("BID_ASSISTANT_KEY_FILE")
        candidates: list[tuple[Path, str]] = []
        if shared_file:
            candidates.append((Path(shared_file), "key_file"))
        else:
            candidates.append((KEY_FILE_PATH, "default_file"))
            candidates.append((LEGACY_KEY_FILE_PATH, "legacy_file"))
        # 依次尝试，保证历史部署平滑升级。
        for candidate, source in candidates:
            if not candidate.exists():
                continue
            try:
                raw = candidate.read_bytes().strip()
                # Fernet.generate_key() 返回的已经是 url-safe base64 字符串
                # 直接作为 Fernet key 使用,不需要再次 base64 decode
                Fernet(raw)  # 验证格式
                key = raw
                _key_source = source
                _warn_if_world_readable(candidate)
                break
            except Exception as e:
                logger.warning("从 %s 读取密钥失败: %s", candidate, e)
                key = None

    if key is None:
        # 生成新密钥
        key = Fernet.generate_key()
        _key_source = "generated"
        try:
            KEY_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)
            # 仅保存原始 key(已经是 url-safe base64),不要二次编码
            KEY_FILE_PATH.write_bytes(key)
            # 设置文件权限为仅当前用户可读
            try:
                os.chmod(KEY_FILE_PATH, 0o600)
            except Exception as _e:
                logger.debug("设置密钥文件权限失败: %s", _e)
            _warn_if_world_readable(KEY_FILE_PATH)
            logger.info("已生成新的 API Key 加密密钥并保存到 %s", KEY_FILE_PATH)
        except Exception as e:
            logger.warning("保存密钥文件失败 (生产环境请使用环境变量 BID_ASSISTANT_KEY): %s", e)
        # D-3：自动生成 = 各实例密钥互不相同，跨实例解密必然失败（表现为 401）。
        # 这是需要运维介入的部署风险，告警升级为 warning 并给出两种修复方式。
        logger.warning(
            "已自动生成 API Key 加密主密钥（来源=generated）。多实例/多进程部署下各实例"
            "密钥不同，彼此无法解密对方写入的 ENC: 密文（AI 调用将 401）。生产环境请："
            "① 设置环境变量 BID_ASSISTANT_KEY；或 ② 设置 BID_ASSISTANT_KEY_FILE 指向"
            "各实例共享的同一密钥文件。当前文件：%s",
            KEY_FILE_PATH,
        )

    _fernet_instance = Fernet(key)
    return _fernet_instance


def rotate_master_key(new_key: str, tables: tuple[tuple[str, str], ...] | None = None) -> dict:
    """用新主密钥重新加密全部落库密钥（Fernet 主密钥轮换）。

    - 必须在**当前进程仍持有旧密钥**时调用（即尚未有任何解密/加密触发过
      _get_or_create_fernet 之外的换钥），本函数会先把旧 fernet 实例固定下来；
    - 明文行（无 ENC: 前缀）会先加密再纳入统计，不会被二次处理；
    - 解密失败的密文（历史用别的密钥加密）计入 failed，需人工处理，不静默丢弃；
    - 返回统计 {表名: 转换行数} 与 'failed'，供脚本/运维核对。

    新密钥格式与 BID_ASSISTANT_KEY 一致：url-safe base64 的 32 字节（Fernet key）。
    """
    from cryptography.fernet import Fernet

    if not new_key:
        raise ValueError("新主密钥不能为空")
    old_fernet = _get_or_create_fernet()  # 固定旧密钥（幂等：已初始化则直接复用）
    new_fernet = Fernet(new_key)

    from ..db import get_db
    from ..db_migrations import _API_KEY_STORAGE_COLUMNS

    stats: dict[str, int] = {}
    failed = 0
    with get_db() as conn:
        for table, col in (tables or _API_KEY_STORAGE_COLUMNS):
            converted = 0
            rows = conn.execute(f"SELECT rowid AS _rid, {col} AS k FROM {table}").fetchall()
            for r in rows:
                old_val = r["k"] or ""
                if not old_val:
                    continue
                if old_val.startswith("ENC:"):
                    try:
                        plain = old_fernet.decrypt(
                            base64.urlsafe_b64decode(old_val[4:].encode("utf-8"))
                        ).decode("utf-8")
                    except Exception:
                        failed += 1
                        continue
                else:
                    plain = old_val
                conn.execute(
                    f"UPDATE {table} SET {col} = ? WHERE rowid = ?",
                    ("ENC:" + base64.urlsafe_b64encode(new_fernet.encrypt(plain.encode("utf-8"))).decode("utf-8"), r["_rid"]),
                )
                converted += 1
            stats[table] = converted
        conn.commit()
    stats["failed"] = failed
    return stats


def encrypt_api_key(plaintext: str) -> str:
    """加密 API Key,返回 base64 字符串 (带 ENC: 前缀)"""
    if not plaintext:
        return plaintext
    if plaintext.startswith("ENC:"):
        # 已经是加密格式
        return plaintext
    try:
        fernet = _get_or_create_fernet()
        encrypted = fernet.encrypt(plaintext.encode("utf-8"))
        return "ENC:" + base64.urlsafe_b64encode(encrypted).decode("utf-8")
    except Exception as e:
        logger.warning("API Key 加密失败,使用明文存储: %s", e)
        return plaintext


def decrypt_api_key(ciphertext: str) -> str:
    """解密 API Key,返回明文

    - 遇到 ENC: 前缀: 尝试解密,失败时回退到原文
    - 无前缀: 当作明文直接返回 (兼容未加密的历史数据)
    """
    if not ciphertext:
        return ciphertext
    if not ciphertext.startswith("ENC:"):
        return ciphertext
    try:
        fernet = _get_or_create_fernet()
        raw = base64.urlsafe_b64decode(ciphertext[4:].encode("utf-8"))
        plaintext = fernet.decrypt(raw).decode("utf-8")
        return plaintext
    except Exception as e:
        logger.warning("API Key 解密失败,返回原文: %s", e)
        return ciphertext


def is_encrypted(value: str) -> bool:
    """判断字符串是否已加密"""
    return bool(value) and value.startswith("ENC:")


def mask_api_key(api_key: str) -> str:
    """对 API Key 进行脱敏处理,仅用于日志或前端展示

    - 加密的 Key: 掩码中间部分
    - 明文 Key: 仅显示前4位和后4位
    """
    if not api_key:
        return ""
    plain = decrypt_api_key(api_key)
    if len(plain) <= 8:
        return "****"
    return plain[:4] + "****" + plain[-4:]
