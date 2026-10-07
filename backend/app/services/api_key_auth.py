"""API开放接口 - API Key认证服务

提供RESTful API供第三方系统集成。
支持API Key的创建、验证、权限管理和使用统计。
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
import hashlib
import json
import logging
import secrets

from ..db import get_db


logger = logging.getLogger(__name__)


@dataclass
class ApiKey:
    """API Key信息"""

    key_id: int
    user_id: int  # 预留字段，当前为1表示系统级
    name: str
    key_hash: str  # 存储hash值，不存明文
    prefix: str  # 显示部分前缀（如 sk-xxxx）
    permissions: list[str] = field(default_factory=list)
    rate_limit: int = 100  # 每小时请求限制
    usage_count: int = 0
    last_used_at: str | None = None
    expires_at: str | None = None
    is_active: bool = True
    created_at: str = ""


class ApiKeyService:
    """API Key管理服务

    职责：
    1. 生成和存储API Key
    2. 验证API Key有效性
    3. 检查权限和速率限制
    4. 记录使用统计
    """

    @staticmethod
    def generate_api_key() -> tuple[str, str]:
        """生成新的API Key

        Returns:
            (full_key, prefix) - 完整密钥和前缀
        """
        # 生成32字节随机密钥
        raw_key = secrets.token_urlsafe(32)
        # 格式: sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
        full_key = f"sk-{raw_key}"
        prefix = full_key[:8]  # 显示前8个字符
        return full_key, prefix

    @staticmethod
    def create_api_key(
        name: str, permissions: list[str] | None = None, expires_days: int = 365
    ) -> dict:
        """创建新的API Key

        Args:
            name: Key名称（用于识别）
            permissions: 权限列表（如 ['projects:read', 'projects:write']）
            expires_days: 有效期（天）

        Returns:
            包含完整Key的信息字典
        """
        if permissions is None:
            permissions = ["projects:read", "sections:read", "export:read"]

        full_key, prefix = ApiKeyService.generate_api_key()
        key_hash = hashlib.sha256(full_key.encode()).hexdigest()

        # 计算过期时间
        expires_at = (datetime.now() + timedelta(days=expires_days)).strftime("%Y-%m-%d %H:%M:%S")

        with get_db() as conn:
            cursor = conn.execute(
                """INSERT INTO api_keys 
                   (name, key_hash, prefix, permissions, rate_limit, expires_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    name,
                    key_hash,
                    prefix,
                    json.dumps(permissions, ensure_ascii=False),
                    100,  # 默认100次/小时
                    expires_at,
                ),
            )
            key_id = cursor.lastrowid

        logger.info("创建API Key: id=%d, name=%s, prefix=%s", key_id, name, prefix)

        return {
            "key_id": key_id,
            "name": name,
            "api_key": full_key,  # 只返回一次！
            "prefix": prefix,
            "permissions": permissions,
            "expires_at": expires_at,
            "warning": "请妥善保存此API Key，后续无法再次获取！",
        }

    @staticmethod
    def validate_api_key(api_key: str) -> dict | None:
        """验证API Key

        Args:
            api_key: 用户提供的API Key

        Returns:
            Key信息字典或None（无效）
        """
        key_hash = hashlib.sha256(api_key.encode()).hexdigest()

        with get_db() as conn:
            row = conn.execute(
                """SELECT * FROM api_keys 
                   WHERE key_hash = ? AND is_active = 1""",
                (key_hash,),
            ).fetchone()

        if not row:
            return None

        key_info = dict(row)

        # 检查是否过期
        if key_info["expires_at"]:
            expires_dt = datetime.strptime(key_info["expires_at"], "%Y-%m-%d %H:%M:%S")
            if datetime.now() > expires_dt:
                logger.warning("API Key已过期: id=%d", key_info["id"])
                return None

        # 检查速率限制
        if not ApiKeyService._check_rate_limit(key_info["id"]):
            logger.warning("API Key触发速率限制: id=%d", key_info["id"])
            return None

        # 更新使用统计
        ApiKeyService._record_usage(key_info["id"])

        return {
            "key_id": key_info["id"],
            "name": key_info["name"],
            "permissions": json.loads(key_info["permissions"]) if key_info["permissions"] else [],
            "rate_limit": key_info["rate_limit"],
        }

    @staticmethod
    def _check_rate_limit(key_id: int) -> bool:
        """检查速率限制

        Args:
            key_id: Key ID

        Returns:
            是否未超限
        """
        one_hour_ago = (datetime.now() - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")

        with get_db() as conn:
            # 查询该 API Key 的速率限制
            key_row = conn.execute(
                "SELECT rate_limit FROM api_keys WHERE id = ?", (key_id,)
            ).fetchone()
            rate_limit = key_row["rate_limit"] if key_row else 60

            count = conn.execute(
                """SELECT COUNT(*) as cnt FROM api_key_usage 
                   WHERE key_id = ? AND used_at > ?""",
                (key_id, one_hour_ago),
            ).fetchone()["cnt"]

        return count < rate_limit

    @staticmethod
    def _record_usage(key_id: int):
        """记录API Key使用情况

        Args:
            key_id: Key ID
        """
        with get_db() as conn:
            conn.execute(
                """INSERT INTO api_key_usage (key_id, used_at) VALUES (?, ?)""",
                (key_id, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
            )

            # 更新最后使用时间
            conn.execute(
                """UPDATE api_keys SET last_used_at = ?, usage_count = usage_count + 1 WHERE id = ?""",
                (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), key_id),
            )

    @staticmethod
    def list_api_keys(user_id: int = 1) -> list[dict]:
        """列出所有API Key（隐藏完整密钥）

        Args:
            user_id: 用户ID（预留）

        Returns:
            Key列表
        """
        with get_db() as conn:
            rows = conn.execute(
                """SELECT id, name, prefix, permissions, rate_limit,
                          usage_count, last_used_at, expires_at, is_active, created_at
                   FROM api_keys 
                   WHERE user_id = ?
                   ORDER BY created_at DESC""",
                (user_id,),
            ).fetchall()

        result = []
        for row in rows:
            d = dict(row)
            # permissions 在数据库中是 JSON 字符串，需解析为数组
            if isinstance(d.get("permissions"), str):
                try:
                    d["permissions"] = json.loads(d["permissions"])
                except (json.JSONDecodeError, TypeError):
                    d["permissions"] = []
            result.append(d)
        return result

    @staticmethod
    def revoke_api_key(key_id: int) -> bool:
        """撤销API Key

        Args:
            key_id: Key ID

        Returns:
            是否成功
        """
        with get_db() as conn:
            conn.execute(
                "UPDATE api_keys SET is_active = 0 WHERE id = ?",
                (key_id,),
            )

        logger.info("撤销API Key: id=%d", key_id)
        return True

    @staticmethod
    def get_usage_stats(key_id: int, days: int = 7) -> dict:
        """获取API Key使用统计

        Args:
            key_id: Key ID
            days: 统计最近N天

        Returns:
            统计数据
        """
        since_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

        with get_db() as conn:
            total = conn.execute(
                """SELECT COUNT(*) as cnt FROM api_key_usage 
                   WHERE key_id = ? AND DATE(used_at) >= ?""",
                (key_id, since_date),
            ).fetchone()["cnt"]

            daily = conn.execute(
                """SELECT DATE(used_at) as day, COUNT(*) as cnt 
                   FROM api_key_usage 
                   WHERE key_id = ? AND DATE(used_at) >= ?
                   GROUP BY day
                   ORDER BY day""",
                (key_id, since_date),
            ).fetchall()

        return {
            "key_id": key_id,
            "days": days,
            "total_usage": total,
            "daily_breakdown": [dict(row) for row in daily],
        }


# 全局实例
