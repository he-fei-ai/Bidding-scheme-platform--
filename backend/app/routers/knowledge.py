"""知识库路由

提供 7 个 API 端点：
- POST   /api/knowledge/documents                上传文档，启动处理流水线
- GET    /api/knowledge/documents                列出文档
- GET    /api/knowledge/documents/{id}/status    获取文档处理状态
- GET    /api/knowledge/items                    检索知识条目
- GET    /api/knowledge/items/{id}               获取单条详情
- PUT    /api/knowledge/items/{id}               编辑知识条目
- DELETE /api/knowledge/items/{id}               删除知识条目
- POST   /api/knowledge/migrate-from-v2          从旧版 JSON 迁移
"""

import asyncio
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Query, UploadFile

from ..db import get_db
from ..models import KnowledgeItemUpdate
from ..security.auth import get_current_user  # BUG-FIX-63: 添加认证依赖
from ..services import knowledge


router = APIRouter(prefix="/knowledge", tags=["knowledge"])

# BUG-FIX-64: 知识库上传文件大小限制（100MB），防止超大文件耗尽内存/磁盘
MAX_KNOWLEDGE_FILE_SIZE = 100 * 1024 * 1024


@router.post("/documents", dependencies=[Depends(get_current_user)])
async def upload_document(
    bg: BackgroundTasks,
    file: UploadFile = File(...),
    industry: str = Form(""),
):
    """上传文档，启动处理流水线"""
    if not file.filename:
        raise HTTPException(status_code=400, detail="文件名不能为空")

    # 保存到临时位置
    content = await file.read()
    ext = Path(file.filename).suffix.lower()
    if ext not in (".docx", ".pdf", ".doc", ".txt"):
        raise HTTPException(status_code=400, detail="仅支持 .docx/.pdf/.doc/.txt 格式")

    # BUG-FIX-64: 校验文件大小，防止超大文件耗尽内存/磁盘
    if len(content) > MAX_KNOWLEDGE_FILE_SIZE:
        raise HTTPException(
            status_code=400,
            detail=f"文件大小超过 {MAX_KNOWLEDGE_FILE_SIZE // (1024*1024)}MB 限制",
        )

    # 保存到知识库源文件目录（仅取文件名，防止路径遍历）
    from ..config import KNOWLEDGE_SOURCE_DIR

    # PERF（2026-10-05）：建目录 + 写盘（最大 100MB）属同步文件 I/O，
    # 派发至工作线程，避免大文件上传时冻结事件循环
    def _write_temp() -> Path:
        KNOWLEDGE_SOURCE_DIR.mkdir(parents=True, exist_ok=True)
        safe_name = Path(file.filename).name  # 只取文件名，剥离路径部分
        temp_path = KNOWLEDGE_SOURCE_DIR / f"temp_{safe_name}"
        temp_path.write_bytes(content)
        return temp_path

    temp_path = await asyncio.to_thread(_write_temp)

    # 创建文档记录（同步 DB 写，同样派发）
    doc_id = await asyncio.to_thread(
        knowledge.create_document,
        filename=file.filename,
        file_path=str(temp_path),
        file_type=ext.lstrip("."),
        industry=industry,
    )

    # 后台运行流水线
    bg.add_task(_run_pipeline_safely, doc_id)

    return {"doc_id": doc_id, "status": "pending", "filename": file.filename}


async def _run_pipeline_safely(doc_id: int):
    try:
        await knowledge.run_pipeline(doc_id)
    except Exception:
        import traceback

        traceback.print_exc()
        knowledge.update_document(doc_id, status="error")


@router.get("/documents", dependencies=[Depends(get_current_user)])
async def list_documents(limit: int = 50):
    """列出文档"""
    # PERF（2026-10-05）：同步 sqlite 读取派发至工作线程（下同，本文件全部端点同口径）
    return await asyncio.to_thread(knowledge.list_documents, limit)


@router.get("/documents/{doc_id}/status", dependencies=[Depends(get_current_user)])
async def get_document_status(doc_id: int):
    """获取文档处理状态"""
    doc = await asyncio.to_thread(knowledge.get_document, doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="文档不存在")
    return {
        "id": doc["id"],
        "filename": doc["filename"],
        "status": doc["status"],
        "coverage": doc.get("coverage", 0),
        "step_log": doc.get("step_log", []),
        "error": doc.get("error", ""),
    }


@router.delete("/documents/{doc_id}", dependencies=[Depends(get_current_user)])
async def delete_document(doc_id: int):
    """删除文档及其知识条目"""
    await asyncio.to_thread(knowledge.delete_document, doc_id)
    return {"status": "deleted", "doc_id": doc_id}


@router.get("/items", dependencies=[Depends(get_current_user)])
async def search_items(
    keyword: str = Query(""),
    industry: str = Query(""),
    tag: str = Query(""),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """检索知识条目"""
    return await asyncio.to_thread(knowledge.search_items, keyword, industry, tag, limit, offset)


@router.get("/items/{item_id}", dependencies=[Depends(get_current_user)])
async def get_item(item_id: int):
    """获取单条详情"""
    item = await asyncio.to_thread(knowledge.get_item, item_id)
    if not item:
        raise HTTPException(status_code=404, detail="条目不存在")
    return item


@router.put("/items/{item_id}", dependencies=[Depends(get_current_user)])
async def update_item(item_id: int, data: KnowledgeItemUpdate):
    """编辑知识条目"""
    updates = data.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(status_code=400, detail="无更新内容")
    item = await asyncio.to_thread(lambda: knowledge.update_item(item_id, **updates))
    if not item:
        raise HTTPException(status_code=404, detail="条目不存在")
    return item


@router.delete("/items/{item_id}", dependencies=[Depends(get_current_user)])
async def delete_item(item_id: int):
    """删除知识条目"""
    await asyncio.to_thread(knowledge.delete_item, item_id)
    return {"status": "deleted", "item_id": item_id}


@router.get("/industries", dependencies=[Depends(get_current_user)])
async def list_industries():
    """列出所有行业分类"""
    return {"industries": await asyncio.to_thread(knowledge.list_industries)}


@router.get("/tags", dependencies=[Depends(get_current_user)])
async def list_tags():
    """列出所有标签"""
    return {"tags": await asyncio.to_thread(knowledge.list_tags)}


@router.post("/migrate-from-v2", dependencies=[Depends(get_current_user)])
async def migrate_from_v2():
    """从旧版 JSON 迁移数据"""
    return await asyncio.to_thread(knowledge.migrate_from_v2)


@router.get("/stats", dependencies=[Depends(get_current_user)])
async def get_stats():
    """获取知识库统计信息"""
    # PERF（2026-10-05）：纯读改 readonly + to_thread，不在事件循环内抢全局写锁
    def _read_stats():
        with get_db(readonly=True) as conn:
            total_docs = conn.execute("SELECT COUNT(*) as cnt FROM knowledge_documents").fetchone()[
                "cnt"
            ]
            success_docs = conn.execute(
                "SELECT COUNT(*) as cnt FROM knowledge_documents WHERE status = 'success'",
            ).fetchone()["cnt"]
            total_items = conn.execute("SELECT COUNT(*) as cnt FROM knowledge_items").fetchone()["cnt"]
            total_usage = conn.execute("SELECT COUNT(*) as cnt FROM knowledge_usage").fetchone()["cnt"]
        return {
            "total_documents": total_docs,
            "success_documents": success_docs,
            "total_items": total_items,
            "total_usage": total_usage,
        }

    return await asyncio.to_thread(_read_stats)
