"""行业树路由"""
from fastapi import APIRouter, Depends, HTTPException
from ..models import BatchImportRequest, GenerateChildrenRequest, IndustryNodeCreate, IndustryNodeUpdate, MatchIndustryRequest, ReorderRequest
from ..security.auth import get_current_user
from ..services.industry_tree import batch_import, create_node, create_version, delete_node, get_audit_logs, get_children, get_default_scoring_items, get_full_tree, get_industry_knowledge_map, get_industry_references, get_node_by_id, get_path, get_root_nodes, get_stats, get_versions, link_knowledge_to_industry, match_industry_by_ai, match_industry_by_text, reorder_nodes, search_nodes, update_node
from ..services.industry_tree import generate_children as ai_generate_children
router = APIRouter(prefix='/industry-tree', tags=['行业树'])

@router.get('/tree', dependencies=[Depends(get_current_user)])
async def api_get_tree(active_only: bool=True):
    """获取完整行业树（嵌套）"""
    return get_full_tree(active_only)

@router.get('/roots', dependencies=[Depends(get_current_user)])
async def api_get_roots(active_only: bool=True):
    """获取根节点列表"""
    return get_root_nodes(active_only)

@router.get('/nodes/{node_id}/children', dependencies=[Depends(get_current_user)])
async def api_get_children(node_id: str, active_only: bool=True):
    """获取子节点列表"""
    return get_children(node_id, active_only)

@router.get('/nodes/{node_id}', dependencies=[Depends(get_current_user)])
async def api_get_node(node_id: str):
    """获取节点详情"""
    node = get_node_by_id(node_id)
    if not node:
        raise HTTPException(status_code=404, detail='节点不存在')
    return node

@router.get('/nodes/{node_id}/path', dependencies=[Depends(get_current_user)])
async def api_get_path(node_id: str):
    """获取节点路径（面包屑）"""
    return get_path(node_id)

@router.get('/search', dependencies=[Depends(get_current_user)])
async def api_search(keyword: str='', level: int | None=None, limit: int=50):
    """搜索节点"""
    if not keyword:
        return []
    return search_nodes(keyword, level, limit)

@router.post('/match', dependencies=[Depends(get_current_user)])
async def api_match_industry(req: MatchIndustryRequest):
    """根据文本匹配行业"""
    result = match_industry_by_text(req.text)
    if result is None:
        return []
    return result

@router.post('/nodes', dependencies=[Depends(get_current_user)])
async def api_create_node(req: IndustryNodeCreate):
    """创建节点"""
    try:
        node = create_node(name=req.name, parent_id=req.parent_id, level=req.level, description=req.description or '', name_en=req.name_en or '', prompt_hint=req.prompt_hint or '', icon=req.icon or '', knowledge_base_ids=req.knowledge_base_ids or [], default_scoring_items=req.default_scoring_items or [], order_index=req.order_index or 0)
        return node
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

@router.put('/nodes/{node_id}', dependencies=[Depends(get_current_user)])
async def api_update_node(node_id: str, req: IndustryNodeUpdate):
    """更新节点"""
    data = req.model_dump(exclude_unset=True)
    node = update_node(node_id, data)
    if not node:
        raise HTTPException(status_code=404, detail='节点不存在')
    return node

@router.delete('/nodes/{node_id}', dependencies=[Depends(get_current_user)])
async def api_delete_node(node_id: str, force: bool=False):
    """删除节点"""
    result = delete_node(node_id, force)
    if not result['success']:
        raise HTTPException(status_code=400, detail=result['error'])
    return {'message': '删除成功'}

@router.post('/nodes/{node_id}/generate-children', dependencies=[Depends(get_current_user)])
async def api_generate_children(node_id: str, req: GenerateChildrenRequest):
    """AI 生成子节点"""
    try:
        nodes = await ai_generate_children(node_id, count=req.count)
        return {'nodes': nodes, 'count': len(nodes)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e

@router.post('/batch-import', dependencies=[Depends(get_current_user)])
async def api_batch_import(req: BatchImportRequest):
    """批量导入"""
    count = batch_import([n.model_dump() for n in req.nodes])
    return {'imported': count}

@router.post('/reorder', dependencies=[Depends(get_current_user)])
async def api_reorder(req: ReorderRequest):
    """重新排序"""
    reorder_nodes(None, req.ordered_ids)
    return {'message': '排序更新成功'}

@router.get('/stats', dependencies=[Depends(get_current_user)])
async def api_get_stats():
    """统计信息"""
    return get_stats()

@router.post('/link-knowledge', dependencies=[Depends(get_current_user)])
async def api_link_knowledge(doc_id: int, industry_id: str):
    """将知识库文档关联到行业树节点"""
    try:
        link_knowledge_to_industry(doc_id, industry_id)
        return {'message': '关联成功'}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

@router.get('/knowledge-map', dependencies=[Depends(get_current_user)])
async def api_get_knowledge_map():
    """获取行业树 → 知识库映射"""
    return get_industry_knowledge_map()

@router.get('/references/{node_id}', dependencies=[Depends(get_current_user)])
async def api_get_industry_references(node_id: str):
    """获取引用该行业节点的项目列表"""
    return get_industry_references(node_id)

@router.get('/scoring-items/{node_id}', dependencies=[Depends(get_current_user)])
async def api_get_scoring_items(node_id: str):
    """获取行业的默认评分项"""
    return {'scoring_items': get_default_scoring_items(node_id)}

@router.post('/match-ai', dependencies=[Depends(get_current_user)])
async def api_match_industry_ai(req: MatchIndustryRequest):
    """AI 语义匹配行业"""
    result = await match_industry_by_ai(req.text, top_k=5)
    return result

@router.get('/audit-logs', dependencies=[Depends(get_current_user)])
async def api_get_audit_logs(node_id: str | None=None, limit: int=50):
    """获取审计日志"""
    return get_audit_logs(node_id, limit)

@router.post('/create-version', dependencies=[Depends(get_current_user)])
async def api_create_version(changelog: str=''):
    """创建版本快照"""
    version_num = create_version(changelog)
    return {'version_number': version_num, 'message': f'已创建版本 v{version_num}'}

@router.post('/rollback/{version_num}', dependencies=[Depends(get_current_user)])
async def api_rollback_to_version(version_num: int):
    """回滚到指定版本"""
    result = rollback_to_version(version_num)
    if result:
        return {'message': f'已成功回滚到版本 v{version_num}'}
    else:
        raise HTTPException(status_code=404, detail=f'版本 v{version_num} 不存在')

@router.get('/versions', dependencies=[Depends(get_current_user)])
async def api_get_versions():
    """获取所有版本列表"""
    return get_versions()
