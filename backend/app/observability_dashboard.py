"""可观测性前端查看界面（ENHANCE 2026-10-29）。

自包含单页 HTML（无外部依赖），由 /observability/dashboard 返回。
含：日志列表（排序/筛选）、日志详情、链路视图（调用树）、时间线视图
（任务生命周期）、搜索过滤（关键词/级别/模块/时间/错误码）、实时刷新、
导出 CSV/JSON、指标看板、告警列表。数据来自 /observability/* API。
"""
from __future__ import annotations

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>可观测性看板</title>
<style>
:root{--bg:#0f172a;--panel:#1e293b;--panel2:#273449;--line:#334155;--fg:#e2e8f0;--dim:#94a3b8;
--info:#38bdf8;--warn:#fbbf24;--err:#f87171;--ok:#34d399;--dbg:#64748b}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:13px/1.5 "Segoe UI",system-ui,sans-serif}
header{display:flex;align-items:center;gap:12px;padding:10px 16px;background:var(--panel);
border-bottom:1px solid var(--line);flex-wrap:wrap;position:sticky;top:0;z-index:20}
header h1{font-size:15px;margin:0;font-weight:600}
.tabs{display:flex;gap:4px;margin-left:8px}
.tab{padding:6px 14px;border:1px solid var(--line);border-radius:6px;cursor:pointer;color:var(--dim);background:transparent}
.tab.active{background:var(--panel2);color:var(--fg);border-color:var(--info)}
.spacer{flex:1}
.btn{padding:6px 12px;border:1px solid var(--line);border-radius:6px;cursor:pointer;background:var(--panel2);color:var(--fg)}
.btn:hover{border-color:var(--info)}
input,select{background:var(--panel2);border:1px solid var(--line);color:var(--fg);border-radius:6px;padding:6px 8px}
main{padding:14px 16px}
.toolbar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:12px}
table{width:100%;border-collapse:collapse;background:var(--panel);border-radius:8px;overflow:hidden}
th,td{padding:6px 8px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
th{background:var(--panel2);color:var(--dim);font-weight:600;position:sticky;top:52px;cursor:pointer;user-select:none}
tr:hover{background:var(--panel2)}
.lv{display:inline-block;padding:1px 6px;border-radius:4px;font-size:11px;font-weight:600}
.lv-INFO{background:#0c4a6e;color:var(--info)}.lv-WARN{background:#78350f;color:var(--warn)}
.lv-ERROR{background:#7f1d1d;color:var(--err)}.lv-FATAL{background:#450a0a;color:#fecaca}
.lv-DEBUG{background:#334155;color:var(--dbg)}
.mono{font-family:Consolas,monospace;font-size:12px}
.badge{display:inline-block;padding:1px 6px;border-radius:4px;background:var(--panel2);border:1px solid var(--line);font-size:11px}
.muted{color:var(--dim)}
.detail{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:12px;margin-top:10px}
pre{background:#0b1120;padding:10px;border-radius:6px;overflow:auto;font-size:12px;max-height:320px}
.tree{font-family:Consolas,monospace;font-size:12px}
.tree .node{border-left:2px solid var(--line);margin-left:10px;padding-left:10px}
.tree .node.root{border-left:0;margin-left:0;padding-left:0}
.timeline{list-style:none;padding:0;margin:0}
.timeline li{display:flex;gap:10px;padding:6px 0;border-bottom:1px dashed var(--line)}
.tl-dot{width:10px;height:10px;border-radius:50%;margin-top:4px;flex:none}
.dot-ok{background:var(--ok)}.dot-warn{background:var(--warn)}.dot-err{background:var(--err)}
.stat-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:10px}
.stat{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:10px}
.stat .k{color:var(--dim);font-size:11px}.stat .v{font-size:18px;font-weight:700;margin-top:2px}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:12px;margin-bottom:12px}
.panel h3{margin:0 0 10px;font-size:13px;color:var(--info)}
.alert-item{padding:6px;border-bottom:1px dashed var(--line)}
#toast{position:fixed;bottom:16px;right:16px;background:var(--panel2);border:1px solid var(--info);
padding:10px 14px;border-radius:8px;display:none;z-index:99}
@media(max-width:900px){th,td{white-space:nowrap}}
</style>
</head>
<body>
<header>
  <h1>🔍 可观测性看板</h1>
  <div class="tabs">
    <button class="tab active" data-tab="logs">日志</button>
    <button class="tab" data-tab="trace">链路</button>
    <button class="tab" data-tab="trajectory">运行轨迹</button>
    <button class="tab" data-tab="metrics">指标</button>
    <button class="tab" data-tab="alerts">告警</button>
  </div>
  <div class="spacer"></div>
  <label><input type="checkbox" id="auto" checked> 实时刷新</label>
  <button class="btn" id="refresh">刷新</button>
</header>
<main>
  <!-- 日志视图 -->
  <section id="view-logs">
    <div class="toolbar">
      <input id="fKeyword" placeholder="关键词" style="width:140px">
      <select id="fLevel"><option value="">级别</option>
        <option>DEBUG</option><option>INFO</option><option>WARN</option>
        <option>ERROR</option><option>FATAL</option></select>
      <input id="fModule" placeholder="模块" style="width:90px">
      <input id="fEvent" placeholder="事件" style="width:150px">
      <input id="fErrorCode" placeholder="错误码" style="width:90px">
      <input id="fTrace" placeholder="trace_id" style="width:180px">
      <input id="fTask" placeholder="task_id" style="width:120px">
      <button class="btn" id="search">搜索</button>
      <span class="muted" id="logTotal"></span>
      <div class="spacer"></div>
      <button class="btn" id="exportJson">导出JSON</button>
      <button class="btn" id="exportCsv">导出CSV</button>
    </div>
    <table>
      <thead><tr><th>时间</th><th>级别</th><th>模块</th><th>事件</th><th>摘要</th>
        <th>耗时ms</th><th>状态</th><th>trace</th></tr></thead>
      <tbody id="logBody"></tbody>
    </table>
    <div class="detail" id="logDetail" style="display:none"></div>
  </section>
  <!-- 链路视图 -->
  <section id="view-trace" style="display:none">
    <div class="toolbar">
      <input id="tTrace" placeholder="trace_id" style="width:320px">
      <button class="btn" id="tLoad">还原调用链</button>
      <span class="muted" id="tInfo"></span>
    </div>
    <div class="panel"><h3>调用树（span 层级与耗时）</h3><div id="tTree" class="tree"></div></div>
    <div class="panel"><h3>时间线（span 按时间排序）</h3><div id="tTimeline"></div></div>
  </section>
  <!-- 运行轨迹视图 -->
  <section id="view-trajectory" style="display:none">
    <div class="toolbar">
      <input id="jTask" placeholder="task_id" style="width:200px">
      <select id="jStatus"><option value="">状态</option>
        <option>created</option><option>queued</option><option>started</option><option>running</option>
        <option>paused</option><option>resumed</option><option>stopping</option><option>stopped</option>
        <option>completed</option><option>failed</option></select>
      <button class="btn" id="jSearch">查询轨迹</button>
    </div>
    <div class="panel"><h3>生命周期时间线</h3><ul class="timeline" id="jTimeline"></ul></div>
    <div class="panel"><h3>任务列表</h3><table><thead><tr><th>task_id</th><th>类型</th>
      <th>状态</th><th>原因</th><th>操作者</th><th>时间</th></tr></thead>
      <tbody id="jList"></tbody></table></div>
  </section>
  <!-- 指标视图 -->
  <section id="view-metrics" style="display:none">
    <div class="stat-grid" id="mStats"></div>
    <div class="panel"><h3>指标明细（P50/P95/P99）</h3><div id="mDetail"></div></div>
    <div class="panel"><h3>错误码 TOP10</h3><div id="mErrors"></div></div>
  </section>
  <!-- 告警视图 -->
  <section id="view-alerts" style="display:none">
    <div class="panel"><h3>告警历史</h3><div id="aList"></div></div>
  </section>
</main>
<div id="toast"></div>

<script>
"use strict";
const API = window.location.pathname.replace(/\/dashboard$/, '');
const $ = id => document.getElementById(id);
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const lvClass = l => 'lv lv-' + String(l || 'INFO').toUpperCase();
let currentSort = 'desc';
let timer = null;

function toast(msg, ok = true) {
  const t = $('toast');
  t.textContent = msg;
  t.style.borderColor = ok ? 'var(--ok)' : 'var(--err)';
  t.style.display = 'block';
  clearTimeout(toast._h);
  toast._h = setTimeout(() => { t.style.display = 'none'; }, 2500);
}

async function jget(url) {
  const r = await fetch(API + url);
  if (!r.ok) throw new Error('HTTP ' + r.status);
  return r.json();
}

function buildLogQuery() {
  const p = new URLSearchParams();
  const kv = [['keyword','fKeyword'],['level','fLevel'],['module','fModule'],['event','fEvent'],
              ['error_code','fErrorCode'],['trace_id','fTrace'],['task_id','fTask']];
  for (const [k, id] of kv) { const v = $(id).value.trim(); if (v) p.set(k, v); }
  p.set('sort', currentSort);
  p.set('limit', '200');
  return p.toString();
}

async function loadLogs() {
  try {
    const d = await jget('/logs?' + buildLogQuery());
    $('logTotal').textContent = '共 ' + d.total + ' 条';
    const rows = d.items.map(it =>
      `<tr data-id="${it.id}"><td class="mono">${esc(it.timestamp)}</td>
       <td><span class="${lvClass(it.level)}">${esc(it.level)}</span></td>
       <td>${esc(it.module)}</td><td>${esc(it.event)}</td>
       <td title="${esc(it.message)}">${esc((it.message||'').slice(0,60))}</td>
       <td>${it.duration_ms ?? ''}</td><td>${esc(it.status)}</td>
       <td class="mono" title="${esc(it.trace_id)}">${esc((it.trace_id||'-').slice(0,12))}</td></tr>`).join('');
    $('logBody').innerHTML = rows;
    $('logBody').querySelectorAll('tr').forEach(tr => tr.onclick = () => showDetail(d.items[tr.rowIndex - 1]));
  } catch (e) { $('logBody').innerHTML = `<tr><td colspan="8" class="muted">加载失败: ${esc(e.message)}</td></tr>`; }
}

function showDetail(it) {
  const d = $('logDetail');
  d.style.display = 'block';
  d.innerHTML = `<h3>日志详情</h3>
    <div><b>时间:</b> ${esc(it.timestamp)} &nbsp; <b>级别:</b> <span class="${lvClass(it.level)}">${esc(it.level)}</span>
      &nbsp; <b>模块:</b> ${esc(it.module)} &nbsp; <b>事件:</b> ${esc(it.event)}</div>
    <div><b>trace:</b> <span class="mono">${esc(it.trace_id)}</span> &nbsp; <b>span:</b> <span class="mono">${esc(it.span_id)}</span>
      &nbsp; <b>parent:</b> <span class="mono">${esc(it.parent_span_id)}</span></div>
    <div><b>task:</b> ${esc(it.task_id)} &nbsp; <b>project:</b> ${esc(it.project_id)} &nbsp; <b>耗时:</b> ${it.duration_ms ?? '-'}ms
      &nbsp; <b>状态:</b> ${esc(it.status)}</div>
    <div><b>错误码:</b> ${esc(it.error_code || '-')} &nbsp; <b>错误层:</b> ${esc(it.error_layer || '-')}
      &nbsp; <b>可重试:</b> ${esc(it.retryable ?? '-')}</div>
    <div style="margin-top:6px"><b>消息:</b> ${esc(it.message)}</div>
    <div style="margin-top:6px"><b>详情:</b></div><pre>${esc(JSON.stringify(it.detail ?? {}, null, 2))}</pre>`;
  d.scrollIntoView({ behavior: 'smooth' });
}

async function doExport(fmt) {
  const p = buildLogQuery().replace(/sort=.*?&/, '');
  try {
    const r = await fetch(`${API}/logs/export?fmt=${fmt}&` + p);
    const blob = await r.blob();
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = 'observability_logs.' + fmt;
    a.click();
    URL.revokeObjectURL(a.href);
    toast('导出 ' + fmt.toUpperCase() + ' 成功');
  } catch (e) { toast('导出失败: ' + e.message, false); }
}

// 链路视图
async function loadTrace(traceId) {
  const tid = traceId || $('tTrace').value.trim();
  if (!tid) { toast('请输入 trace_id', false); return; }
  try {
    const d = await jget('/trace/' + encodeURIComponent(tid));
    $('tInfo').textContent = `span ${d.chain_count} 条 / 日志 ${d.log_count} 条` + (d.broken ? ' ⚠️ 断链' : '');
    $('tInfo').style.color = d.broken ? 'var(--err)' : 'var(--dim)';
    $('tTree').innerHTML = renderTree(d.spans.filter(s => !s.parent_span_id || s.parent_span_id === '-'), d.spans);
    $('tTimeline').innerHTML = d.spans.map(s =>
      `<div style="padding:4px 0;border-bottom:1px dashed var(--line)">
        ${'　'.repeat(s.depth || 0)}<span class="badge">${esc(s.event || s.module)}</span>
        <span class="mono">${esc(s.span_id)}</span>
        <span class="muted">${s.duration_ms}ms</span> <span class="badge">${esc(s.status)}</span>
        <div class="muted">${esc((s.detail && (s.detail.message || s.detail.model)) || '')}</div></div>`).join('');
    if (d.broken) toast('⚠️ 检测到链路断链（父 span 缺失）', false);
  } catch (e) { toast('调用链查询失败: ' + e.message, false); }
}

function renderTree(nodes, all) {
  if (!nodes.length) return '<div class="muted">无 span</div>';
  return nodes.map(n => {
    const kids = all.filter(c => c.parent_span_id === n.span_id);
    return `<div class="node ${n.depth === 0 ? 'root' : ''}">
      <span class="badge">${esc(n.event || n.module)}</span>
      <span class="mono">${esc(n.span_id)}</span>
      <span class="muted">${n.duration_ms}ms</span>
      <span class="badge">${esc(n.status)}</span>
      <div class="muted" style="margin-left:16px">${esc((n.detail && (n.detail.message || n.detail.model)) || '')}</div>
      ${kids.length ? renderTree(kids, all) : ''}
    </div>`;
  }).join('');
}


// 运行轨迹视图
async function loadTrajectory(taskId) {
  const tid = taskId || $('jTask').value.trim();
  try {
    let d;
    if (tid) d = await jget('/trajectory/' + encodeURIComponent(tid));
    else d = await jget('/trajectories?limit=200');
    const items = d.items || [];
    const dot = s => s === 'failed' || s === 'stopped' ? 'dot-err' : (s === 'completed' || s === 'running' ? 'dot-ok' : 'dot-warn');
    $('jTimeline').innerHTML = items.map(it =>
      `<li><span class="tl-dot ${dot(it.to_status)}"></span>
        <div><span class="badge">${esc(it.to_status)}</span>
          <span class="muted">${esc(it.from_status)} → ${esc(it.to_status)}</span>
          <span class="muted">${esc(it.created_at)}</span>
          <div class="muted">${esc(it.reason)} / ${esc(it.operator)}</div></div></li>`).join('');
    const rows = items.map(it =>
      `<tr><td class="mono">${esc(it.task_id)}</td><td>${esc(it.task_type)}</td>
       <td><span class="badge">${esc(it.to_status)}</span></td><td>${esc(it.reason)}</td>
       <td>${esc(it.operator)}</td><td class="mono">${esc(it.created_at)}</td></tr>`).join('');
    $('jList').innerHTML = rows;
  } catch (e) { toast('轨迹查询失败: ' + e.message, false); }
}

// 指标视图
async function loadMetrics() {
  try {
    const d = await jget('/metrics');
    const m = d.metrics || {};
    const names = Object.keys(m);
    const cards = [
      ['日志写入', d.storage.written], ['队列积压', d.storage.queue_size],
      ['丢弃', d.storage.dropped], ['写盘错误', d.storage.flush_errors],
      ['降级', d.storage.degraded ? '是' : '否'], ['成功率', d.success_rate],
      ['缓存命中率', d.cache_hit_rate], ['指标项', names.length],
    ];
    $('mStats').innerHTML = cards.map(([k, v]) =>
      `<div class="stat"><div class="k">${esc(k)}</div><div class="v">${esc(v)}</div></div>`).join('');
    $('mDetail').innerHTML = '<table><thead><tr><th>指标</th><th>次数</th><th>avg</th><th>p50</th><th>p95</th><th>p99</th><th>max</th></tr></thead><tbody>' +
      names.map(n => { const s = m[n];
        return `<tr><td>${esc(n)}</td><td>${s.count}</td><td>${s.avg}</td><td>${s.p50}</td>
          <td>${s.p95}</td><td>${s.p99}</td><td>${s.max}</td></tr>`; }).join('') + '</tbody></table>';
    $('mErrors').innerHTML = '<table><thead><tr><th>错误码</th><th>次数</th></tr></thead><tbody>' +
      (d.error_top10 || []).map(e => `<tr><td class="mono">${esc(e.code)}</td><td>${e.count}</td></tr>`).join('')
      + '</tbody></table>';
  } catch (e) { toast('指标查询失败: ' + e.message, false); }
}

// 告警视图
async function loadAlerts() {
  try {
    const d = await jget('/alerts');
    $('aList').innerHTML = (d.items || []).map(a =>
      `<div class="alert-item"><b>[${esc(a.type)}]</b> ${esc(a.message)}
        <div class="muted">trace: ${esc(a.trace_id || '-')} @ ${esc(a.fired_at)}</div>
        <div class="muted">建议: ${esc(a.suggestion)}</div></div>`).join('') || '<div class="muted">暂无告警</div>';
  } catch (e) { toast('告警查询失败: ' + e.message, false); }
}

// 标签页切换 + 数据刷新
const TAB_LOAD = { logs: loadLogs, trace: () => {}, trajectory: loadTrajectory, metrics: loadMetrics, alerts: loadAlerts };
document.querySelectorAll('.tab').forEach(t => t.onclick = () => {
  document.querySelectorAll('.tab').forEach(x => x.classList.remove('active'));
  t.classList.add('active');
  const name = t.dataset.tab;
  ['logs', 'trace', 'trajectory', 'metrics', 'alerts'].forEach(v =>
    $('view-' + v).style.display = (v === name) ? 'block' : 'none');
  history.replaceState(null, '', '?tab=' + name);
  (TAB_LOAD[name] || (() => {}))();
});

function refresh() {
  const tab = document.querySelector('.tab.active').dataset.tab;
  (TAB_LOAD[tab] || (() => {}))();
}

// 事件绑定
$('search').onclick = loadLogs;
$('refresh').onclick = refresh;
$('exportJson').onclick = () => doExport('json');
$('exportCsv').onclick = () => doExport('csv');
$('tLoad').onclick = () => loadTrace();
$('jSearch').onclick = () => loadTrajectory();
document.querySelectorAll('#logBody th').forEach((th, i) => th.onclick = () => {
  currentSort = currentSort === 'desc' ? 'asc' : 'desc';
  loadLogs();
});
// 回车触发搜索
['fKeyword','fLevel','fModule','fEvent','fErrorCode','fTrace','fTask'].forEach(id =>
  $(id).addEventListener('keydown', e => { if (e.key === 'Enter') loadLogs(); }));

// URL 参数定位：?tab=metrics 或 ?trace=xxx（打开链路视图并加载）
const params = new URLSearchParams(location.search);
if (params.get('trace')) {
  document.querySelector('[data-tab="trace"]').click();
  $('tTrace').value = params.get('trace');
  loadTrace(params.get('trace'));
} else if (params.get('task')) {
  document.querySelector('[data-tab="trajectory"]').click();
  $('jTask').value = params.get('task');
  loadTrajectory(params.get('task'));
} else {
  const tab = params.get('tab');
  if (tab) document.querySelector(`[data-tab="${tab}"]`).click();
  else loadLogs();
}

// 实时刷新（自动/暂停）
function startAuto() { stopAuto(); if ($('auto').checked) timer = setInterval(refresh, 5000); }
function stopAuto() { if (timer) clearInterval(timer); }
$('auto').onchange = () => { startAuto(); };
startAuto();
</script>
</body>
</html>
"""
