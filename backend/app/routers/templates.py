"""已有方案扩写工作流（Project Templates）

功能：
- 上传历史标书（.docx/.pdf）作为模板
- AI 提取模板的目录结构
- 列表展示已有模板
- 覆盖审计：对比当前目录与模板目录，分析覆盖情况
- 正文生成时注入模板上下文
"""
import asyncio
import json
import logging
from pathlib import Path
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from ..config import PROJECTS_DATA_DIR
from ..db import get_db
from ..services.ai.extraction_runner import (
    create_provider,
    get_ai_config,
    run_json_extraction,
    AIMessage,
)
from ..services.files import extract_text_from_path
from ..security.auth import get_current_user
logger = logging.getLogger(__name__)
router = APIRouter(
    prefix='/projects/{project_id}/templates',
    tags=['templates'],
    dependencies=[Depends(get_current_user)],
)

@router.post('/upload')
async def upload_template(project_id: int, file: UploadFile=File(...)):
    """上传历史标书文件作为模板"""
    if not file.filename:
        raise HTTPException(status_code=400, detail='文件名不能为空')
    ext = Path(file.filename).suffix.lower()
    if ext not in ('.docx', '.pdf', '.doc', '.txt'):
        raise HTTPException(status_code=400, detail='仅支持 .docx/.pdf/.doc/.txt 格式')
    templates_dir = PROJECTS_DATA_DIR / str(project_id) / 'templates'
    safe_name = Path(file.filename).name
    file_path = templates_dir / safe_name
    content = await file.read()

    # PERF（2026-10-05）：建目录/写盘/DB 插入等同步 I/O 派发至工作线程
    def _persist():
        templates_dir.mkdir(parents=True, exist_ok=True)
        file_path.write_bytes(content)
        with get_db() as conn:
            cursor = conn.execute("INSERT INTO project_templates (project_id, filename, file_path, status) VALUES (?, ?, ?, 'pending')", (project_id, safe_name, str(file_path)))
            return cursor.lastrowid

    template_id = await asyncio.to_thread(_persist)
    return {'template_id': template_id, 'filename': safe_name, 'status': 'pending'}

@router.post('/{template_id}/extract')
async def extract_template_outline(project_id: int, template_id: int):
    """AI 提取模板的目录结构"""
    # PERF（2026-10-05）：纯读 readonly + to_thread，不在事件循环内抢写锁
    def _read_tpl():
        with get_db(readonly=True) as conn:
            row = conn.execute('SELECT * FROM project_templates WHERE id = ? AND project_id = ?', (template_id, project_id)).fetchone()
            return dict(row) if row else None

    row = await asyncio.to_thread(_read_tpl)
    if not row:
        raise HTTPException(status_code=404, detail='模板不存在')
    file_path = Path(row['file_path'])
    if not await asyncio.to_thread(file_path.exists):
        raise HTTPException(status_code=400, detail='模板文件已不存在')
    try:
        text = await asyncio.to_thread(extract_text_from_path, str(file_path))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f'文本提取失败: {e}') from e
    if not text.strip():
        raise HTTPException(status_code=400, detail='模板文件为空或无法提取文本')
    try:
        ai_config = get_ai_config()
        provider = create_provider(ai_config)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f'AI 配置异常: {e}') from e
    extract_sys = '# 已有方案目录结构提取\n\n## 角色\n你是标书结构分析专家，从历史标书文本中提取目录结构。\n\n## 任务\n提取文档中的完整目录结构，输出为三级嵌套 JSON 格式。\n\n## 规则\n1. 识别文档中的标题层级（如"第一节"、"一、"、"1、"、"1.1"等）\n2. 一级标题对应 level=1（如"第一节 项目概述"）\n3. 二级标题对应 level=2（如"一、项目概况"）\n4. 三级标题对应 level=3（如"1、项目概况"）\n5. 三级以下合并到三级内容中\n6. 只提取有实际内容的标题，跳过目录页、封面、签字页\n7. 每个节点包含 title、level、children（三级为空数组）\n\n## 输出格式\n```json\n[{"title":"项目概述","level":1,"children":[{"title":"项目概况","level":2,"children":[{"title":"工程名称","level":3,"children":[]}]}]}]\n```\n\n直接输出 JSON 数组，严禁附加解释。'
    truncated = text[:30000]
    extract_user = f'请从以下标书文本中提取目录结构：\n\n{truncated}'
    try:
        # R-2 2026-10-12：改走统一 runner（provider 由上方 try 预构造后传入，
        # 保留「AI 配置异常 → 500」的原始语义，与 facts.py 口径一致）。
        result, _ = await run_json_extraction([AIMessage(role='system', content=extract_sys), AIMessage(role='user', content=extract_user)], provider=provider, temperature=0.3, max_retries=2)
        outline = result if isinstance(result, list) else []
    except Exception as e:
        logger.warning('模板目录提取失败: %s', e)
        raise HTTPException(status_code=500, detail=f'AI 提取目录失败: {e}') from e
    outline_json = json.dumps(outline, ensure_ascii=False)

    def _save_outline():
        with get_db() as conn:
            conn.execute("UPDATE project_templates SET outline_json = ?, status = 'ready', updated_at = datetime('now', 'localtime') WHERE id = ?", (outline_json, template_id))

    await asyncio.to_thread(_save_outline)
    return {'template_id': template_id, 'outline': outline, 'title_count': len(outline)}

@router.get('')
async def list_templates(project_id: int):
    """列出项目的已有模板"""
    # PERF（2026-10-05）：纯读 readonly + to_thread，不在事件循环内抢写锁
    def _read_templates():
        with get_db(readonly=True) as conn:
            return [dict(r) for r in conn.execute('SELECT id, filename, status, outline_json, created_at FROM project_templates WHERE project_id = ? ORDER BY id DESC', (project_id,)).fetchall()]

    rows = await asyncio.to_thread(_read_templates)
    items = []
    for r in rows:
        item = dict(r)
        try:
            outline = json.loads(item['outline_json']) if item['outline_json'] else []
        except json.JSONDecodeError:
            outline = []
        item['title_count'] = len(outline)
        item['outline_json'] = ''
        items.append(item)
    return {'items': items}

@router.delete('/{template_id}')
async def delete_template(project_id: int, template_id: int):
    """删除模板"""
    # PERF（2026-10-05）：读+ unlink 文件 + 删除行的整段同步 I/O 派发至工作线程
    def _delete():
        with get_db() as conn:
            row = conn.execute('SELECT file_path FROM project_templates WHERE id = ? AND project_id = ?', (template_id, project_id)).fetchone()
            if not row:
                return False
            if row['file_path']:
                p = Path(row['file_path'])
                if p.exists():
                    p.unlink()
            conn.execute('DELETE FROM project_templates WHERE id = ?', (template_id,))
            return True

    if not await asyncio.to_thread(_delete):
        raise HTTPException(status_code=404, detail='模板不存在')
    return {'ok': True}

@router.post('/{template_id}/audit')
async def audit_template_coverage(project_id: int, template_id: int):
    """覆盖审计：对比当前目录与模板目录"""
    # PERF（2026-10-05）：两段读合并为一次 readonly 会话并派发至工作线程
    def _read_audit_source():
        with get_db(readonly=True) as conn:
            tpl = conn.execute('SELECT outline_json FROM project_templates WHERE id = ? AND project_id = ?', (template_id, project_id)).fetchone()
            if not tpl:
                return None, None
            sections = conn.execute('SELECT id, title, level, parent_id, sort_order, status FROM sections WHERE project_id = ? ORDER BY sort_order, id', (project_id,)).fetchall()
            return dict(tpl), [dict(s) for s in sections]

    tpl, sections = await asyncio.to_thread(_read_audit_source)
    if tpl is None:
        raise HTTPException(status_code=404, detail='模板不存在')
    try:
        template_outline = json.loads(tpl['outline_json']) if tpl['outline_json'] else []
    except json.JSONDecodeError:
        template_outline = []
    if not template_outline:
        raise HTTPException(status_code=400, detail='模板目录为空，请先提取目录')
    current_titles = {s['title'] for s in sections if s['title']}
    audit_items = []
    covered = 0
    missing = 0

    def _walk(node: dict, path: str=''):
        nonlocal covered, missing
        title = node.get('title', '')
        full_path = f'{path}/{title}' if path else title
        if title in current_titles:
            covered += 1
            audit_items.append({'title': title, 'path': full_path, 'status': 'covered'})
        else:
            missing += 1
            audit_items.append({'title': title, 'path': full_path, 'status': 'missing'})
        for child in node.get('children', []):
            _walk(child, full_path)
    for node in template_outline:
        _walk(node)
    audit_json = json.dumps(audit_items, ensure_ascii=False)

    def _save_audit():
        with get_db() as conn:
            conn.execute("UPDATE project_templates SET coverage_audit_json = ?, updated_at = datetime('now', 'localtime') WHERE id = ?", (audit_json, template_id))

    await asyncio.to_thread(_save_audit)
    return {'template_id': template_id, 'total': len(audit_items), 'covered': covered, 'missing': missing, 'coverage_rate': round(covered / len(audit_items) * 100, 1) if audit_items else 0, 'items': audit_items[:100]}
