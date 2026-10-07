"""可开关的 JWT 认证服务。

- 纯标准库实现：hmac + hashlib + base64url（HS256）
- 密码哈希：hashlib.pbkdf2_hmac（salt 随机，迭代 120_000）
- AUTH_ENABLED 默认 False；关闭时 get_current_user_dependency 兜底 mock admin
- 存在任何无效 Bearer token（无论开关）一律抛 401，避免前端静默降级
"""
import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from fastapi import HTTPException, Request
from ..config import AUTH_ENABLED, DATA_DIR
logger = logging.getLogger(__name__)
HASH_ITERATIONS = 120000
JWT_HEADER = {'alg': 'HS256', 'typ': 'JWT'}

def hash_password(password: str, salt: bytes | None=None) -> tuple[str, str]:
    """返回 (salt_hex, hash_hex)。"""
    if salt is None:
        salt = secrets.token_bytes(32)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode(), salt, HASH_ITERATIONS)
    return (salt.hex(), digest.hex())

def verify_password(password: str, salt_hex: str, hash_hex: str) -> bool:
    salt = bytes.fromhex(salt_hex)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode(), salt, HASH_ITERATIONS)
    return hmac.compare_digest(digest.hex(), hash_hex)

def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode('ascii')

def _b64url_decode(s: str) -> bytes:
    pad = s + '=' * (-len(s) % 4)
    return base64.urlsafe_b64decode(pad)

def _load_jwt_secret() -> str:
    """从环境变量或持久化文件加载 JWT secret。文件不存在则一次性生成写入。"""
    raw = os.environ.get('AUTH_JWT_SECRET', '')
    if raw:
        return raw
    secret_file = DATA_DIR / '.jwt_secret'
    try:
        secret_file.parent.mkdir(parents=True, exist_ok=True)
        if secret_file.exists():
            return secret_file.read_text(encoding='utf-8').strip()
        secret = secrets.token_hex(32)
        secret_file.write_text(secret, encoding='utf-8')
        # BUG-FIX（P1）：JWT 签名密钥等同于「任意身份签发权」，默认文件权限
        # 对同主机其他用户可读。写入后立即收紧为 0600。
        try:
            os.chmod(secret_file, 0o600)
        except Exception as _e:
            logger.warning('收紧 %s 权限失败，请手动设置为 0600: %s', secret_file, _e)
        logger.warning('已生成一次性 JWT secret 写入 %s，重启后不变', secret_file)
        return secret
    except Exception as e:
        logger.error('无法持久化 JWT secret，使用运行时随机值（重启失效）: %s', e)
        return secrets.token_hex(32)

def create_access_token(user_id: int, username: str, role: str, expires_minutes: int | None=None) -> str:
    now = int(time.time())
    exp = now + (expires_minutes or 720) * 60
    payload = {'sub': str(user_id), 'user_id': user_id, 'username': username, 'role': role, 'iat': now, 'exp': exp}
    header_b64 = _b64url_encode(json.dumps(JWT_HEADER, separators=(',', ':')).encode())
    payload_b64 = _b64url_encode(json.dumps(payload, separators=(',', ':')).encode())
    signing_input = f'{header_b64}.{payload_b64}'
    secret = _load_jwt_secret()
    sig = hmac.new(secret.encode(), signing_input.encode(), hashlib.sha256).digest()
    return f'{signing_input}.{_b64url_encode(sig)}'

def decode_access_token(token: str) -> dict:
    """校验签名与过期；失败抛 PermissionError（中文消息）。"""
    try:
        header_b64, payload_b64, sig_b64 = token.split('.')
    except ValueError:
        raise PermissionError('无效的登录凭证')
    try:
        header = json.loads(_b64url_decode(header_b64))
        payload = json.loads(_b64url_decode(payload_b64))
    except Exception:
        raise PermissionError('无效的登录凭证')
    if header.get('alg') != 'HS256':
        raise PermissionError('无效的登录凭证')
    secret = _load_jwt_secret()
    signing_input = f'{header_b64}.{payload_b64}'
    expected = _b64url_encode(hmac.new(secret.encode(), signing_input.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(expected, sig_b64):
        raise PermissionError('无效的登录凭证')
    if payload.get('exp', 0) < int(time.time()):
        raise PermissionError('登录已过期，请重新登录')
    return payload

def _get_users_cursor():
    """懒导入 db，避免循环依赖。"""
    import app.db as _db_mod
    return _db_mod.get_db()

def get_user_by_username(username: str) -> dict | None:
    with _get_users_cursor() as conn:
        row = conn.execute('SELECT id, username, password_hash, salt, role, is_active, created_at, last_login_at FROM users WHERE username = ?', (username,)).fetchone()
        return dict(row) if row else None

def get_user_by_id(user_id: int) -> dict | None:
    with _get_users_cursor() as conn:
        row = conn.execute('SELECT id, username, password_hash, salt, role, is_active, created_at, last_login_at FROM users WHERE id = ?', (user_id,)).fetchone()
        return dict(row) if row else None

def authenticate_user(username: str, password: str) -> dict | None:
    row = get_user_by_username(username)
    if row is None or not row['is_active']:
        return None
    if not verify_password(password, row['salt'], row['password_hash']):
        return None
    with _get_users_cursor() as conn:
        conn.execute("UPDATE users SET last_login_at = datetime('now', 'localtime') WHERE id = ?", (row['id'],))
    return row

def change_password(user_id: int, old_password: str, new_password: str) -> bool:
    row = get_user_by_id(user_id)
    if row is None or not row['is_active']:
        return False
    if not verify_password(old_password, row['salt'], row['password_hash']):
        return False
    salt_hex, hash_hex = hash_password(new_password)
    with _get_users_cursor() as conn:
        conn.execute('UPDATE users SET password_hash = ?, salt = ? WHERE id = ?', (hash_hex, salt_hex, user_id))
    return True

def get_current_user_dependency(request: Request) -> dict:
    """从 Authorization: Bearer <token> 或 ?token= 查询参数解出当前用户。

    - AUTH_ENABLED=False 且无 token：返回 mock admin dict，兼容现有路由
    - 有 token 但无效/过期：一律抛 401 HTTPException
    - AUTH_ENABLED=True：必须携带有效 token

    BUG-FIX 2026-09-18（P1）：main.py 认证中间件允许 query ?token= 通过，
    但本依赖只读 Authorization 头 → EventSource(SSE)、导出/报告下载等
    “浏览器直接导航”类请求在 AUTH_ENABLED=True 时全部 401（与中间件
    口径不一致的单腿断链）。现补齐：Header 优先，无 Header 时回退 query。
    """
    auth_header = request.headers.get('Authorization', '')
    token: str | None = None
    if auth_header.startswith('Bearer '):
        token = auth_header[7:].strip()
    if token is None:
        token = request.query_params.get('token', '') or None
    if token is None:
        if not AUTH_ENABLED:
            return {'id': 1, 'username': 'admin', 'role': 'admin'}
        raise HTTPException(status_code=401, detail='缺少授权令牌')
    try:
        payload = decode_access_token(token)
    except PermissionError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    user_id = int(payload['user_id'])
    row = get_user_by_id(user_id)
    if row is None or not row['is_active']:
        raise HTTPException(status_code=401, detail='账户已被禁用')
    return {'id': row['id'], 'username': row['username'], 'role': row['role']}
