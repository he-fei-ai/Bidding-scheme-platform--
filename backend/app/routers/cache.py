"""Redis缓存管理路由

提供缓存监控、清除和统计功能。

V3.0 增强：统一收集所有缓存实例的统计信息（RedisCache、内存缓存、导出缓存、图表缓存等）。
"""
import logging
import time
from fastapi import APIRouter, Depends, HTTPException
from ..security.auth import get_current_user
from ..services.redis_cache import CacheService, _memory_cache, cache_stats, get_redis_client
logger = logging.getLogger(__name__)
router = APIRouter(prefix='/cache', tags=['cache'], dependencies=[Depends(get_current_user)])

@router.get('/stats')
async def get_cache_stats():
    """获取所有缓存实例的统计信息"""
    try:
        redis_stats = CacheService.get_stats()
        redis_stats['name'] = 'redis_cache'
        redis_stats['type'] = 'redis'
        mem_stats = _memory_cache.stats
        mem_stats['name'] = 'memory_fallback'
        mem_stats['type'] = 'memory'
        from ..services.export import get_export_cache_stats
        export_stats = get_export_cache_stats()
        client = get_redis_client()
        all_caches = [redis_stats, mem_stats] + export_stats.get('caches', [])
        return {'status': 'ok', 'caches': all_caches, 'redis_connected': client is not None}
    except Exception as e:
        logger.error('获取缓存统计失败: %s', e, exc_info=True)
        raise HTTPException(status_code=500, detail=f'获取统计失败: {e!s}') from e

@router.post('/clear')
async def clear_cache(pattern: str='all'):
    """清除缓存

    Args:
        pattern: 清除模式 ("all" 清除所有, "project_list", "outline", "section", "files", "templates")
    """
    try:
        from ..services.redis_cache import RedisCache
        if pattern == 'all':
            count = CacheService.clear_all()
            message = f'已清除所有缓存，共 {count} 条'
        else:
            prefix_map = {'project_list': CacheService.PROJECT_LIST, 'project_detail': CacheService.PROJECT_DETAIL, 'outline': CacheService.OUTLINE_CONTENT, 'section': CacheService.SECTION_CONTENT, 'files': CacheService.FILE_LIST, 'templates': CacheService.TEMPLATE_LIST, 'api_keys': CacheService.API_KEYS_LIST, 'knowledge': CacheService.KNOWLEDGE_BASE}
            prefix = prefix_map.get(pattern)
            if not prefix:
                raise HTTPException(status_code=400, detail=f"不支持的清除模式: {pattern}，支持: {', '.join(prefix_map.keys())}, all")
            pattern_full = f'{RedisCache.KEY_PREFIX}{prefix}:*'
            count = RedisCache.clear_pattern(pattern_full)
            message = f'已清除缓存: {pattern}，共 {count} 条'
        return {'status': 'ok', 'message': message, 'cleared_count': count}
    except HTTPException:
        raise
    except Exception as e:
        logger.error('清除缓存失败: %s', e, exc_info=True)
        raise HTTPException(status_code=500, detail=f'清除缓存失败: {e!s}') from e

@router.get('/health')
async def check_cache_health():
    """检查缓存服务健康状态"""
    try:
        client = get_redis_client()
        if not client:
            return {'status': 'degraded', 'redis_available': False, 'message': 'Redis未连接，使用降级方案', 'fallback': '内存缓存'}
        start = time.time()
        client.ping()
        latency_ms = (time.time() - start) * 1000
        return {'status': 'healthy', 'redis_available': True, 'latency_ms': round(latency_ms, 2), 'stats': cache_stats.to_dict()}
    except Exception as e:
        logger.error('缓存健康检查失败: %s', e, exc_info=True)
        return {'status': 'error', 'redis_available': False, 'message': str(e)}
