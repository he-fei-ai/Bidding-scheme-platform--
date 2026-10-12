"""AI 调用监控 API 路由

提供实时监控面板数据和管理接口"""
import logging
import time
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from ..db import get_pool
from ..security.auth import get_current_user
from ..services.ai.workflows import _adaptive_controller, _section_content_cache
from ..services.ai.image_engine import (
    get_repair_stats, flush_repair_stats, get_repair_stats_history,
    cleanup_repair_stats_snapshots,
)
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

# ──────────────────────────────────────────────────────────────────────
# repair_mermaid 统计（ENHANCE 2026-10-11）
# ──────────────────────────────────────────────────────────────────────
@router.get('/repair-stats')
async def get_repair_mermaid_stats():
    """获取 repair_mermaid 步骤统计快照

    返回：
    - total_validations: 累计校验次数
    - per_chart_type: 各 chart_type 触发的修复步骤及次数（按次数降序）
    - type_mismatches: original_chart_type ≠ 外层 chart_type 的次数 + 明细
    """
    try:
        stats = get_repair_stats()
        return {'status': 'success', 'data': stats}
    except Exception as e:
        logger.error('获取 repair_mermaid 统计失败: %s', e)
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post('/repair-stats/flush')
async def flush_repair_mermaid_stats():
    """手动触发 repair_mermaid 统计 flush（打印汇总 + reset）

    正常由 celery beat 每小时自动触发；本端点供运维调试。
    """
    try:
        snapshot = flush_repair_stats()
        if snapshot['total'] == 0:
            return {'status': 'success', 'message': '暂无数据（静默跳过）', 'data': snapshot}
        return {'status': 'success', 'message': '已 flush 并 reset', 'data': snapshot}
    except Exception as e:
        logger.error('flush repair_mermaid 统计失败: %s', e)
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get('/repair-stats/history')
async def get_repair_stats_history_endpoint(limit: int = 72):
    """repair_stats_snapshots 历史趋势（返回最近 N 行快照）

    Args:
        limit: 返回最近多少行。默认 72 = 3 天 × 24 小时；最大 168（7 天）
    """
    try:
        limit = max(1, min(limit, 168))  # clamp
        history = get_repair_stats_history(limit=limit)
        return {'status': 'success', 'count': len(history), 'data': history}
    except Exception as e:
        logger.error('repair-stats/history 失败: %s', e)
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post('/repair-stats/cleanup')
async def cleanup_repair_stats_snapshots_endpoint(keep_rows: int = 720):
    """手动触发 repair_stats_snapshots 清理（保留最近 keep_rows 行）"""
    try:
        deleted = cleanup_repair_stats_snapshots(keep_rows=keep_rows)
        return {'status': 'success', 'deleted': deleted, 'keep_rows': keep_rows}
    except Exception as e:
        logger.error('repair-stats/cleanup 失败: %s', e)
        raise HTTPException(status_code=500, detail=str(e)) from e

# ──────────────────────────────────────────────────────────────────────
# repair_mermaid ECharts Dashboard（ENHANCE 2026-10-11）
# ──────────────────────────────────────────────────────────────────────
@router.get('/repair-stats/dashboard', response_class=HTMLResponse)
async def repair_stats_dashboard():
    """repair_mermaid 统计 ECharts dashboard（前端运维页面）"""
    return """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>repair_mermaid 统计 Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/echarts@5.4.3/dist/echarts.min.js"></script>
<style>
  body { font-family: -apple-system, "Microsoft YaHei", sans-serif; background:#0f172a; color:#e2e8f0; padding:20px; }
  h1 { font-size:20px; margin-bottom:4px; }
  .sub { color:#94a3b8; font-size:13px; margin-bottom:16px; }
  .grid { display:grid; grid-template-columns:1fr 1fr; gap:16px; }
  .card { background:#1e293b; border-radius:8px; padding:16px; }
  .card-full { background:#1e293b; border-radius:8px; padding:16px; margin-bottom:16px; }
  .chart { width:100%; height:340px; }
  .metric { display:inline-block; margin-right:24px; font-size:13px; }
  .metric b { color:#fbbf24; font-size:18px; }
  table { width:100%; border-collapse:collapse; font-size:13px; }
  th, td { text-align:left; padding:6px 8px; border-bottom:1px solid #334155; }
  th { color:#94a3b8; font-weight:normal; }
  .warn { color:#f87171; }
</style>
</head>
<body>
<div class="card-full">
  <h1>🔧 repair_mermaid 步骤统计 Dashboard</h1>
  <div class="sub">每 30s 自动刷新 · <span id="ts">-</span>
    <button id="flushBtn" style="margin-left:12px;background:#f59e0b;color:#0f172a;border:none;padding:6px 14px;border-radius:4px;cursor:pointer;font-weight:600;">
      🔄 手动 Flush（写入 DB + reset）
    </button>
    <span id="flushStatus" style="margin-left:8px;font-size:12px;color:#94a3b8"></span>
  </div>
  <span class="metric">累计校验 <b id="mtotal">0</b></span>
  <span class="metric">类型错位 <b id="mmism">0</b></span>
  <span class="metric">高频规则 <b id="mhigh">0</b></span>
</div>
<div class="grid">
  <div class="card"><h3>按 Chart Type 的修复步骤 Top-5</h3><div id="barChart" class="chart"></div></div>
  <div class="card"><h3>Type Mismatch 热区</h3><div id="sankeyChart" class="chart"></div></div>
</div>
<div class="card-full" style="margin-top:16px">
  <h3>高频 Mismatch 规则（≥3次）</h3>
  <table id="highFreqTable"><thead><tr><th>外层类型</th><th>AI误输出</th><th>次数</th><th>建议</th></tr></thead><tbody></tbody></table>
</div>
<!-- ENHANCE 2026-10-11: 历史趋势 Tab（DB 持久化的 snapshots） -->
<div class="card-full" style="margin-top:16px">
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
    <h3 style="margin:0">📈 历史趋势（最近 24 小时 DB snapshots）</h3>
    <div>
      <span id="histStatus" style="font-size:12px;color:#94a3b8;margin-right:8px"></span>
      <button id="histBtn" style="background:#3b82f6;color:#fff;border:none;padding:4px 12px;border-radius:4px;cursor:pointer;font-size:12px">加载历史</button>
    </div>
  </div>
  <div id="histChart" style="width:100%;height:260px"></div>
</div>
<script>
const bar = echarts.init(document.getElementById('barChart'), 'dark');
const sankey = echarts.init(document.getElementById('sankeyChart'), 'dark');
const hist = echarts.init(document.getElementById('histChart'), 'dark');
async function refresh() {
  try {
    const r = await fetch(window.location.pathname.replace('/dashboard', ''));
    const d = await r.json();
    const data = d.data || {};
    document.getElementById('mtotal').textContent = data.total_validations || 0;
    document.getElementById('ts').textContent = '更新于 ' + new Date().toLocaleTimeString();
    const types = Object.keys(data.per_chart_type || {});
    if (types.length) {
      const allSteps = new Set();
      types.forEach(t => Object.keys(data.per_chart_type[t]).forEach(s => allSteps.add(s)));
      const topSteps = Array.from(allSteps).sort().slice(0, 8);
      bar.setOption({ tooltip: {}, legend: { data: topSteps, bottom: 0 },
        grid: { left: 60, right: 20, top: 20, bottom: 50 },
        xAxis: { type: 'category', data: types }, yAxis: { type: 'value' },
        series: topSteps.map(s => ({ name: s, type: 'bar', stack: 'total',
          data: types.map(t => (data.per_chart_type[t] || {})[s] || 0) })) }, true);
    }
    const mism = data.type_mismatches || {};
    const nodes = [], links = [], seen = new Set();
    Object.entries(mism).forEach(([outer, origins]) => {
      Object.entries(origins).forEach(([orig, cnt]) => {
        ['外层:' + outer, 'AI:' + orig].forEach(n => { if (!seen.has(n)) { nodes.push({name: n}); seen.add(n); } });
        links.push({source: '外层:' + outer, target: 'AI:' + orig, value: cnt});
      });
    });
    sankey.setOption({ tooltip: {}, series: [{ type: 'sankey', emphasis: { focus: 'adjacency' }, data: nodes, links: links }] }, true);
    const hf = data.high_freq_mismatches || [];
    document.getElementById('mhigh').textContent = hf.length;
    const tbody = document.querySelector('#highFreqTable tbody');
    tbody.innerHTML = hf.length ? hf.map(h =>
      '<tr><td>' + h.outer_type + '</td><td class="warn">' + h.original_type + '</td><td>' + h.count + '</td><td>' + h.suggestion + '</td></tr>'
    ).join('') : '<tr><td colspan="4" style="color:#64748b;text-align:center">暂无高频 mismatch</td></tr>';
    let totalMism = 0;
    Object.values(mism).forEach(o => Object.values(o).forEach(c => totalMism += c));
    document.getElementById('mmism').textContent = totalMism;
  } catch(e) { console.error(e); }
}
refresh(); setInterval(refresh, 30000);
window.addEventListener('resize', () => { bar.resize(); sankey.resize(); hist.resize(); });
// ── 历史趋势加载（ENHANCE 2026-10-11）──
async function loadHistory() {
  const btn = document.getElementById('histBtn');
  const status = document.getElementById('histStatus');
  btn.disabled = true; btn.textContent = '⏳';
  status.textContent = '加载中...'; status.style.color = '#94a3b8';
  try {
    const base = window.location.pathname.replace('/dashboard', '');
    const r = await fetch(base + '/history?limit=24');
    const d = await r.json();
    const rows = (d.data || []).reverse();  // 时间升序（前端画图要从小到大）
    if (!rows.length) {
      status.textContent = '暂无 DB 快照（等待首次 flush）';
      btn.disabled = false; btn.textContent = '加载历史';
      hist.setOption({title:{text:'暂无历史数据',left:'center',top:'center',textStyle:{color:'#64748b',fontSize:14}}}, true);
      return;
    }
    const times = rows.map(r => (r.created_at || '').split(' ')[1] || r.created_at);
    const totals = rows.map(r => r.total_validations || 0);
    // 按 chart_type 聚合 step_counts（简化：只画 total 和 mismatch 总数两条线）
    let mismTotals = rows.map(r => {
      const m = r.mismatches || {}; let sum = 0;
      Object.values(m).forEach(o => Object.values(o).forEach(c => sum += c));
      return sum;
    });
    hist.setOption({
      tooltip: { trigger: 'axis' },
      legend: { data: ['校验次数', '类型错位总数'], bottom: 0 },
      grid: { left: 60, right: 30, top: 20, bottom: 50 },
      xAxis: { type: 'category', data: times, axisLabel: { fontSize: 10, rotate: 30 } },
      yAxis: [
        { type: 'value', name: '校验数', nameTextStyle: { color: '#94a3b8' } },
        { type: 'value', name: '错位数', nameTextStyle: { color: '#94a3b8' } },
      ],
      series: [
        { name: '校验次数', type: 'line', smooth: true, data: totals, itemStyle: { color: '#fbbf24' } },
        { name: '类型错位总数', type: 'line', smooth: true, yAxisIndex: 1, data: mismTotals, itemStyle: { color: '#f87171' } },
      ],
    }, true);
    status.textContent = `已加载 ${rows.length} 条快照`; status.style.color = '#22c55e';
  } catch(e) {
    status.textContent = '加载失败'; status.style.color = '#f87171';
  } finally {
    btn.disabled = false; btn.textContent = '加载历史';
  }
}
document.getElementById('histBtn').addEventListener('click', loadHistory);
loadHistory();  // 页面打开时自动加载一次
// ── 手动 flush 按钮（ENHANCE 2026-10-11）──
document.getElementById('flushBtn').addEventListener('click', async () => {
  const btn = document.getElementById('flushBtn');
  const status = document.getElementById('flushStatus');
  btn.disabled = true; btn.textContent = '⏳ Flushing...';
  status.textContent = ''; status.style.color = '#94a3b8';
  try {
    const r = await fetch(window.location.pathname.replace('/dashboard', '') + '/flush', {method: 'POST'});
    const d = await r.json();
    if (d.status === 'success') {
      status.textContent = d.message || 'Flush 成功';
      status.style.color = '#22c55e';
    } else {
      status.textContent = 'Flush 失败';
      status.style.color = '#f87171';
    }
    refresh();  // flush 后立即刷新页面
  } catch(e) {
    status.textContent = '网络错误'; status.style.color = '#f87171';
  } finally {
    btn.disabled = false; btn.textContent = '🔄 手动 Flush（写入 DB + reset）';
    setTimeout(() => { status.textContent = ''; }, 3000);
  }
});
</script>
</body>
</html>"""
