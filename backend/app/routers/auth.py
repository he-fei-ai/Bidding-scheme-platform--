"""认证路由：登录 / 当前用户 / 改密。

前缀：/api/v1/auth（由 app/main.py 挂载到 v1_router 时统一处理）
本文件 router.prefix = ""，由 include_router 时传入 prefix="/api/v1/auth"。
"""

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ..services.auth_service import (
    authenticate_user,
    change_password,
    create_access_token,
    get_current_user_dependency,
)


logger = logging.getLogger(__name__)

router = APIRouter()


class LoginRequest(BaseModel):
    username: str
    password: str


class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str = Field(..., min_length=8)


@router.post("/login")
async def login(body: LoginRequest):
    # BUG-FIX 2026-09-16（中）：authenticate_user 内含 bcrypt 校验（约 100-300ms）
    # 与同步 DB 查询，直接在 async 路由里调用会阻塞事件循环；
    # 并发登录/被暴力刷接口时全服务卡顿。移到线程池执行。
    row = await asyncio.to_thread(authenticate_user, body.username, body.password)
    if row is None:
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    expires_minutes: int = int(__import__("os").environ.get("AUTH_TOKEN_EXPIRE_MINUTES", "720"))
    # 从路由模块导入时 config 还未加载到 sys.modules，直接读环境变量
    token = create_access_token(row["id"], row["username"], row["role"])
    return {
        "access_token": token,
        "token_type": "bearer",
        "expires_in": expires_minutes * 60,
        "user": {
            "id": row["id"],
            "username": row["username"],
            "role": row["role"],
        },
    }


@router.get("/me")
async def me(request: Request, user: dict = Depends(get_current_user_dependency)):
    return user


@router.post("/change-password")
async def change_password_endpoint(
    body: ChangePasswordRequest,
    request: Request,
    user: dict = Depends(get_current_user_dependency),
):
    # 同 login：bcrypt 哈希校验移到线程池，避免阻塞事件循环
    ok = await asyncio.to_thread(change_password, user["id"], body.old_password, body.new_password)
    if not ok:
        raise HTTPException(status_code=400, detail="旧密码错误")
    return {"ok": True}
