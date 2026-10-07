"""AI 调用监控 API 路由

提供实时监控面板数据和管理接口"""
import logging
import time
from fastapi import APIRouter, Depends, HTTPException
from ..db import get_pool
from ..security.auth import get_current_user
from ..services.ai.workflows import _adaptive_controller, _section_content_cache
from ..services.ai_monitor import AlertConfig, configure_ai_monitor, get_ai_monitor, get_ai_monitor_stats
from ..services.redis_cache import CacheService, get_redis_client
logger = logging.getLogger(__name__)
router = APIRouter(prefix='/monitor', tags=['monitor'], dependencies=[Depends(get_current_user)])

@router.get('/ai-stats')
async def get_ai_monitoring_stats():
    """获取 AI 调用监控统计信息

    返回：
    - 总调用次数、成功/失败次数
    - 失败率、平均响应时间
    - 各 provider 的详细统计
    - 最近告警信息
    """
    try:
        stats = get_ai_monitor_stats()
        return {'status': 'success', 'data': stats}
    except Exception as e:
        logger.error('获取监控统计失败: %s', e)
        raise HTTPException(status_code=500, detail=str(e)) from e

@router.get('/ai-stats/{provider}')
async def get_provider_stats(provider: str):
    """获取指定 provider 的监控统计

    Args:
        provider: AI 提供商名称（如 openai_compatible, deepseek, anthropic）
    """
    try:
        monitor = get_ai_monitor()
        stats = monitor.get_provider_stats(provider)
        if not stats:
            raise HTTPException(status_code=404, detail=f'未找到 provider [{provider}] 的监控数据')
        return {'status': 'success', 'provider': provider, 'data': stats}
    except HTTPException:
        raise
    except Exception as e:
        logger.error('获取 provider 统计失败: %s', e)
        raise HTTPException(status_code=500, detail=str(e)) from e

@router.post('/clear-alerts')
async def clear_active_alerts():
    """清除所有活跃告警"""
    try:
        monitor = get_ai_monitor()
        count = monitor.clear_alerts()
        return {'status': 'success', 'message': f'已清除 {count} 条活跃告警'}
    except Exception as e:
        logger.error('清除告警失败: %s', e)
        raise HTTPException(status_code=500, detail=str(e)) from e

@router.put('/configure')
async def update_monitor_config(config: AlertConfig):
    """更新监控配置

    Args:
        config: 新的告警配置
            - failure_rate_threshold: 失败率阈值（百分比）
            - response_time_threshold: 响应时间阈值（秒）
            - window_size: 监控窗口大小
            - cooldown_seconds: 告警冷却时间
            - enabled: 是否启用告警
    """
    try:
        configure_ai_monitor(config)
        return {'status': 'success', 'message': '监控配置已更新', 'config': {'failure_rate_threshold': config.failure_rate_threshold, 'response_time_threshold': config.response_time_threshold, 'window_size': config.window_size, 'cooldown_seconds': config.cooldown_seconds, 'enabled': config.enabled}}
    except Exception as e:
        logger.error('更新监控配置失败: %s', e)
        raise HTTPException(status_code=500, detail=str(e)) from e

@router.get('/health')
async def monitor_health_check():
    """监控服务健康检查

    返回监控服务自身状态和数据库连接池状态
    """
    try:
        stats = get_ai_monitor_stats()
        summary = stats.get('summary', {})
        failure_rate = summary.get('failure_rate', 0)
        if failure_rate > 50:
            health_status = 'critical'
        elif failure_rate > 30:
            health_status = 'degraded'
        else:
            health_status = 'healthy'
        db_pool = get_pool()
        db_stats = db_pool.get_stats()
        return {'status': 'success', 'health': health_status, 'timestamp': time.time(), 'data': {'total_calls': summary.get('total_calls', 0), 'failure_rate': failure_rate, 'avg_response_time': summary.get('avg_response_time', 0)}, 'database': {'pool_stats': db_stats}}
    except Exception as e:
        logger.error('健康检查失败: %s', e)
        return {'status': 'error', 'health': 'unhealthy', 'timestamp': time.time(), 'error': str(e)}

@router.get('/cache-stats')
async def get_cache_statistics():
    """获取章节内容缓存统计信息

    返回：
    - 缓存命中率、当前缓存大小
    - 缓存命中/未命中计数
    - 淘汰次数
    """
    try:
        cache_stats = _section_content_cache.get_stats()
        redis_stats = CacheService.get_stats()
        return {'status': 'success', 'data': {'section_content_cache': {'size': cache_stats['cache_size'], 'max_size': cache_stats['max_size'], 'hits': cache_stats['hits'], 'misses': cache_stats['misses'], 'hit_rate': cache_stats['hit_rate'], 'evictions': cache_stats['evictions']}, 'redis_cache': {'hits': redis_stats.get('hits', 0), 'misses': redis_stats.get('misses', 0), 'hit_rate': redis_stats.get('hit_rate', '0.0%'), 'total_requests': redis_stats.get('total_requests', 0)}}}
    except Exception as e:
        logger.error('获取缓存统计失败: %s', e)
        raise HTTPException(status_code=500, detail=str(e)) from e

@router.get('/concurrency-stats')
async def get_concurrency_statistics():
    """获取智能并发控制器统计信息

    返回：
    - 当前并发数、总请求数、失败请求数
    - 平均响应时间
    """
    try:
        if _adaptive_controller:
            stats = {'current_concurrency': _adaptive_controller.get_current_concurrency(), 'total_requests': _adaptive_controller.total_requests, 'failed_requests': _adaptive_controller.failed_requests, 'timeout_count': _adaptive_controller.timeout_count}
            times = [e[0] for e in _adaptive_controller.response_times] if _adaptive_controller.response_times else []
            if times:
                stats['avg_response_time'] = sum(times) / len(times)
                stats['min_response_time'] = min(times)
                stats['max_response_time'] = max(times)
            else:
                stats['avg_response_time'] = 0
                stats['min_response_time'] = 0
                stats['max_response_time'] = 0
            return {'status': 'success', 'data': stats}
        else:
            return {'status': 'success', 'data': {'message': '并发控制器未初始化'}}
    except Exception as e:
        logger.error('获取并发统计失败: %s', e)
        raise HTTPException(status_code=500, detail=str(e)) from e
_CACHE_KEY_PATTERNS = {'项目列表': 'project_list', '项目详情': 'project_detail', '大纲内容': 'outline_content', '章节内容': 'section_content', '文件列表': 'file_list', 'API Keys': 'api_keys_list', '模板列表': 'template_list', '知识库': 'knowledge_base'}

@router.get('/cache-info')
async def get_cache_info():
    """获取 Redis 服务端指标与键分布统计"""
    try:
        client = get_redis_client()
        redis_connected = client is not None
        redis_server: dict = {}
        key_distribution: list[dict] = []
        total_keys = 0
        if redis_connected:
            try:
                info = client.info() or {}
                redis_server = {'memory_used_bytes': int(info.get('used_memory', 0)), 'memory_used_human': info.get('used_memory_human', '-'), 'connected_clients': int(info.get('connected_clients', 0)), 'uptime_seconds': int(info.get('uptime_in_seconds', 0)), 'evicted_keys': int(info.get('evicted_keys', 0)), 'keyspace_hits': int(info.get('keyspace_hits', 0)), 'keyspace_misses': int(info.get('keyspace_misses', 0))}
            except Exception:
                redis_server = {'error': '无法读取 Redis INFO'}
            try:
                for label, pattern in _CACHE_KEY_PATTERNS.items():
                    count = sum((1 for _ in client.scan_iter(match=f'*{pattern}*')))
                    total_keys += count
                    key_distribution.append({'type': label, 'pattern': pattern, 'count': count})
            except Exception:
                key_distribution = [{'type': p, 'pattern': p, 'count': 0} for p in _CACHE_KEY_PATTERNS]
                total_keys = 0
        app_stats = CacheService.get_stats()
        section_cache_stats = _section_content_cache.get_stats() if _section_content_cache else {}
        return {'status': 'success', 'redis_connected': redis_connected, 'redis_server': redis_server, 'key_distribution': key_distribution, 'total_keys': total_keys, 'app_stats': app_stats, 'section_content_cache': section_cache_stats}
    except Exception as e:
        logger.error('获取缓存详情失败: %s', e)
        raise HTTPException(status_code=500, detail=str(e)) from e
