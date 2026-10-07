import asyncio
import logging
import os
from typing import Annotated
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field
from ..security.auth import get_current_user
from ..services import files as file_service
from ..services.projects import update_step
from .project_access import enforce_project_access
logger = logging.getLogger(__name__)
# ENHANCE 2026-09-29（P1 IDOR 收口·补齐链首）：sections/facts/审核四路由/导出均已接入
# enforce_project_access，但链首的 files / ai / projects 三个路由仍只验「已登录」不验
# 「项目归属」——任意登录用户可向他人项目上传/删除文件、覆写分析结果。
# 现逐端点接入与 sections 完全同口径的属权校验（router 级依赖改为参数注入，
# 以便把身份传给助手；AUTH_ENABLED=False 时 get_current_user 返回 mock admin，
# 历史项目 created_by 为 NULL 时放行 —— 默认行为与单用户部署完全兼容）。
router = APIRouter(prefix='/projects/{project_id}/files', tags=['files'])
# BUG-FIX 2026-09-29：白名单与 services/files._do_extract_text 实际支持的格式对齐——
# 服务层早已支持 .wps 与图片 OCR（png/jpg/jpeg/bmp/tiff/webp），但路由层一直不放行，
# 导致图片扫描件/WPS 文档根本无法进入解析链路（功能入口封死）。
# 图片在 OCR 引擎不可用时由服务层优雅降级（空文本 + 提示），不会中断上传。
# ENHANCE 2026-10-05（TD3 技术债）：此前这里是一份**手工维护的副本**，与服务层
# _do_extract_text 的分支各写一份，历史上正因如此出现了上面的入口封死。现改为从
# 服务层导出的唯一真相 SUPPORTED_EXTENSIONS 派生；两个集合必须相等的约束由
# tests/test_import_enhancements_2026_10_05.py 钉住。
ALLOWED_EXTENSIONS = set(file_service.SUPPORTED_EXTENSIONS)
MAX_FILE_SIZE = 50 * 1024 * 1024


class FileRoleUpdate(BaseModel):
    """手动切换文件角色的请求体（main=主招标文件 / supporting=辅助附件）"""

    file_role: str = Field(..., description='文件角色：main 或 supporting')

def allowed_extension(filename: str) -> bool:
    """检查文件扩展名是否允许"""
    if not filename or '.' not in filename:
        return False
    ext = os.path.splitext(filename)[1].lower()
    return ext in ALLOWED_EXTENSIONS

def get_file_extension(filename: str) -> str:
    """获取文件扩展名"""
    if not filename or '.' not in filename:
        return ''
    return os.path.splitext(filename)[1].lower()

@router.post('')
async def upload_file(project_id: int, file: UploadFile=File(...), file_role: str=Form(''), current_user: Annotated[object, Depends(get_current_user)]=None):
    """上传文件（带安全校验）"""
    # 属权校验兼项目存在性校验（无权 403、不存在 404，与其余模块同口径）
    await enforce_project_access(project_id, current_user)
    if not file.filename:
        raise HTTPException(status_code=400, detail='文件名为空')
    if not allowed_extension(file.filename):
        ext = get_file_extension(file.filename)
        raise HTTPException(status_code=400, detail=f"不支持的文件类型 '{ext}'，允许的类型：{', '.join(sorted(ALLOWED_EXTENSIONS))}")
    # BUG-FIX 2026-09-18（中）：原实现 `content = await file.read()` 先把整个文件
    # 读进内存，之后才比较 MAX_FILE_SIZE —— 大小限制形同虚设：上传一个数 GB 的
    # 文件会直接把进程内存打爆（OOM），而不是被拒绝。改为分块累积、超限立即中断。
    _CHUNK = 1024 * 1024
    _chunks: list[bytes] = []
    _total = 0
    while True:
        _chunk = await file.read(_CHUNK)
        if not _chunk:
            break
        _total += len(_chunk)
        if _total > MAX_FILE_SIZE:
            raise HTTPException(status_code=400, detail=f'文件大小超过 {MAX_FILE_SIZE // (1024 * 1024)}MB 限制')
        _chunks.append(_chunk)
    content = b''.join(_chunks)
    if not content:
        raise HTTPException(status_code=400, detail='文件内容为空')
    try:
        result = await asyncio.to_thread(file_service.save_upload, project_id, file.filename, content, file_role)
        if result.get('status') == 'success':
            # PERF 2026-09-18（P2）：update_step 是同步 DB 写（拿全局写锁），
            # 直调会阻塞事件循环——上传高峰期卡住所有并发 SSE/请求。
            await asyncio.to_thread(update_step, project_id, 2)
        return result
    except Exception as e:
        logger.error('文件上传失败 [%s]: %s', file.filename, e, exc_info=True)
        raise HTTPException(status_code=500, detail='文件上传失败，请检查日志') from e

@router.get('')
async def list_files(project_id: int, current_user: Annotated[object, Depends(get_current_user)]=None):
    """获取项目文件列表"""
    await enforce_project_access(project_id, current_user)
    # PERF（2026-09-16）：同步 DB 查询移入线程池，避免正文生成高峰期
    # 写锁争用时阻塞事件循环
    return await asyncio.to_thread(file_service.get_project_files, project_id)

@router.get('/text')
async def get_latest_text(project_id: int, current_user: Annotated[object, Depends(get_current_user)]=None):
    """获取最新合并文本"""
    await enforce_project_access(project_id, current_user)
    text = await file_service.latest_extracted_text(project_id)
    return {'text': text}

@router.get('/{file_id}/text')
async def get_file_text(project_id: int, file_id: int, current_user: Annotated[object, Depends(get_current_user)]=None):
    """获取指定文件的提取文本"""
    await enforce_project_access(project_id, current_user)
    # PERF（2026-09-16）：文件读取（可能数 MB）+ DB 查询移入线程池
    text = await asyncio.to_thread(file_service.get_file_text, project_id, file_id)
    return {'text': text}

@router.delete('/{file_id}')
async def delete_uploaded_file(project_id: int, file_id: int, current_user: Annotated[object, Depends(get_current_user)]=None):
    """删除上传文件"""
    await enforce_project_access(project_id, current_user)
    try:
        # PERF（2026-09-16）：DB 删除 + 物理文件 unlink 移入线程池
        result = await asyncio.to_thread(file_service.delete_file, project_id, file_id)
        return result
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e

@router.post('/{file_id}/re-extract')
async def re_extract_file_text(project_id: int, file_id: int, current_user: Annotated[object, Depends(get_current_user)]=None):
    """重新提取指定文件的文本（用于 pypdf 等依赖补装后的重试）"""
    await enforce_project_access(project_id, current_user)
    try:
        result = await asyncio.to_thread(file_service.re_extract_text, project_id, file_id)
        return result
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e

@router.post('/{file_id}/async-reparse')
async def async_reparse_file(project_id: int, file_id: int, current_user: Annotated[object, Depends(get_current_user)]=None):
    """后台异步重新解析指定文件（遗留修复 2026-10-06 L2）

    Celery 任务 ``tasks.file.parse_and_extract`` 此前**只被注册、从不被派发**
    （全库没有任何调用点），是一条死链路：注册了却永远不执行，状态端点上也不会出现
    它的痕迹。这里给补上正式入口，用于耗时的解析场景（扫描件 OCR、大体积 PDF）：
      - 经 ``dispatch_task`` 派发：有 broker → 入队；无 broker → 进程内直调
        （返回体里的 ``dispatched_via`` 如实说明走了哪条路）。

    与同步端点 ``/{file_id}/re-extract`` 的区别：本端点立即返回（不等待解析完成），
    解析进度可通过 ``GET /projects/{id}/tasks/files/status`` 观察。
    """
    await enforce_project_access(project_id, current_user)
    try:
        from pathlib import Path

        from ..celery_app import dispatch_task
        from ..db import get_db
        from ..services.files import _create_parse_task

        def _read_uploaded_file():
            with get_db(readonly=True) as conn:
                return conn.execute(
                    "SELECT filename, path FROM uploaded_files WHERE id = ? AND project_id = ?",
                    (file_id, project_id),
                ).fetchone()

        # PERF（2026-10-06）：DB 往返移出事件循环（async def 内直调 DB 入口会冻结
        # 并发请求；此处是 PERF-2026-10-05 批次的漏网之鱼）。
        row = await asyncio.to_thread(_read_uploaded_file)
        if not row:
            raise HTTPException(status_code=404, detail='文件不存在')
        file_path = row["path"]
        filename = row["filename"]
        if not file_path or not Path(file_path).exists():
            raise HTTPException(status_code=400, detail='原始文件已不存在，请重新上传')

        # 任务行由本端点创建（任务内部已改为「有则更新」，这里先建是为了让
        # 状态端点立刻可见 processing，而不是等 worker 接单后才出现）。
        # BUG-FIX 2026-10-06（R3）：派发时显式 create_task_row=False，
        # 否则任务内再建一行，同一次解析会在任务表留下两行（重复计数 + 孤儿行）
        _create_parse_task(project_id, filename, Path(filename).suffix.lower())

        dispatched = await asyncio.to_thread(
            dispatch_task,
            "tasks.file.parse_and_extract",
            (file_path, project_id, file_id),
            {"create_task_row": False},
        )
        return {
            "file_id": file_id,
            "filename": filename,
            "dispatched": dispatched.get("status"),
            "dispatched_via": dispatched.get("dispatched_via", "none"),
            "celery_task_id": dispatched.get("celery_task_id"),
            "detail": dispatched,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e

@router.put('/{file_id}/role')
async def update_file_role(project_id: int, file_id: int, payload: FileRoleUpdate, current_user: Annotated[object, Depends(get_current_user)]=None):
    """手动设置文件的主/辅助角色（影响下游主文件选择）"""
    await enforce_project_access(project_id, current_user)
    role = (payload.file_role or '').strip()
    if role not in ('main', 'supporting'):
        raise HTTPException(status_code=400, detail='非法的文件角色，仅支持 main / supporting')
    try:
        # PERF：同步 DB 写（拿全局写锁）+ 缓存失效移入线程池，避免阻塞事件循环
        return await asyncio.to_thread(file_service.set_file_role, project_id, file_id, role)
    except file_service.MainFileRequiredError as e:
        # REVIEW-FIX 2026-10-04（M2）：拒绝把项目唯一 main 降为 0 main（可转回，非 404）
        raise HTTPException(status_code=422, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except Exception as e:
        logger.error('更新文件角色失败 [project=%d file=%d]: %s', project_id, file_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail='更新文件角色失败，请检查日志') from e
