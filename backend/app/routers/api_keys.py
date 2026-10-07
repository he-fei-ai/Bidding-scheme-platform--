"""API Key 管理路由

提供 API Key 的创建、列表、撤销和使用统计功能。
"""
import logging
from fastapi import APIRouter, Depends, HTTPException, Request
from ..security.auth import User, get_current_user
from ..services.api_key_auth import ApiKeyService
logger = logging.getLogger(__name__)
router = APIRouter(prefix='/keys', tags=['api_keys'], dependencies=[Depends(get_current_user)])

#: 允许签发的权限白名单。此前 `permissions` 由调用方任意指定，
#: 任何登录用户都能给自己签发一把名义上 `export:write` 的高权限 Key。
ALLOWED_PERMISSIONS = frozenset(
    {
        'projects:read',
        'projects:write',
        'sections:read',
        'sections:write',
        'export:read',
        'export:write',
        'knowledge:read',
        'knowledge:write',
    }
)
#: 有效期上限（天）。默认 365 天过长，收敛到 90 天。
MAX_EXPIRES_DAYS = 90


def _require_admin(request: Request) -> User:
    """API Key 属于凭据签发能力，必须管理员才能操作。"""
    user = get_current_user(request)
    if user is None or getattr(user, "role", "") != "admin":
        raise HTTPException(status_code=403, detail='仅管理员可管理 API Key')
    return user


@router.post('')
async def create_new_api_key(
    request: Request,
    name: str = '默认密钥',
    permissions: str = 'projects:read,sections:read,export:read',
    expires_days: int = 365,
):
    """创建新的 API Key

    Args:
        name: Key 名称
        permissions: 权限列表，逗号分隔（如 'projects:read,projects:write'）
        expires_days: 有效期（天）

    Returns:
        包含完整 API Key 的信息（仅显示一次！）
    """
    _require_admin(request)
    try:
        perm_list = [p.strip() for p in permissions.split(',') if p.strip()]
    except Exception:
        perm_list = ['projects:read', 'sections:read', 'export:read']
    rejected = [p for p in perm_list if p not in ALLOWED_PERMISSIONS]
    if rejected:
        raise HTTPException(
            status_code=400,
            detail=f'不支持的权限: {", ".join(rejected)}',
        )
    expires_days = max(1, min(int(expires_days or 1), MAX_EXPIRES_DAYS))
    try:
        result = ApiKeyService.create_api_key(name=name, permissions=perm_list, expires_days=expires_days)
        return result
    except Exception as e:
        logger.error('创建 API Key 失败: %s', e, exc_info=True)
        raise HTTPException(status_code=500, detail=f'创建 API Key 失败: {e!s}') from e

@router.get('')
async def list_api_keys(request: Request):
    """列出所有 API Key（隐藏完整密钥）

    BUG-FIX 2026-09-16（安全 P1）：只读端点此前没有角色校验，仅靠 router 级
    `get_current_user`（任何登录用户都通过），普通用户即可枚举全部 API Key 的
    name/prefix/permissions/rate_limit 与各 Key 的用量统计，等于把签发面透露出去。
    """
    _require_admin(request)
    try:
        keys = ApiKeyService.list_api_keys(user_id=1)
        return {'status': 'ok', 'count': len(keys), 'keys': keys}
    except Exception as e:
        logger.error('获取 API Key 列表失败: %s', e, exc_info=True)
        raise HTTPException(status_code=500, detail=f'获取列表失败: {e!s}') from e

@router.delete('/{key_id}')
async def revoke_api_key(key_id: int, request: Request):
    """撤销（禁用）指定的 API Key"""
    _require_admin(request)
    try:
        success = ApiKeyService.revoke_api_key(key_id)
        if success:
            return {'status': 'ok', 'message': f'API Key {key_id} 已撤销'}
        else:
            raise HTTPException(status_code=404, detail=f'API Key {key_id} 不存在')
    except HTTPException:
        raise
    except Exception as e:
        logger.error('撤销 API Key 失败: %s', e, exc_info=True)
        raise HTTPException(status_code=500, detail=f'撤销失败: {e!s}') from e

@router.get('/{key_id}/usage')
async def get_api_key_usage(key_id: int, request: Request, days: int=7):
    """获取指定 API Key 的使用统计

    Args:
        key_id: API Key ID
        days: 统计最近 N 天的使用数据

    BUG-FIX 2026-09-16（安全）：同 list_api_keys，补管理员校验。
    """
    _require_admin(request)
    try:
        stats = ApiKeyService.get_usage_stats(key_id=key_id, days=days)
        return {'status': 'ok', 'usage': stats}
    except Exception as e:
        logger.error('获取使用统计失败: %s', e, exc_info=True)
        raise HTTPException(status_code=500, detail=f'获取统计失败: {e!s}') from e
