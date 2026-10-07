"""端到端验证：拆分后 7 个图表渲染仍输出 PNG"""
import sys
import os
import json

sys.path.insert(0, r"J:\编程\招投标方案平台\backend")

from app.services.ai.mermaid_renderer import render_mermaid_to_bytes, ChartCache

results = []

# 1. 流程图
fc = '''flowchart TD
    A["施工准备"] --> B{是否审批通过?}
    B -- 是 --> C["基础施工"]
    B -- 否 --> D["整改"]
    C --> E["竣工验收"]
'''
out = render_mermaid_to_bytes(fc, 'flowchart', skip_http=True)
results.append(('flowchart', out))

# 2. 甘特图
plan = {'type':'gantt','title':'测试','totalDays':60,'tasks':[
    {'id':1,'name':'施工准备','start':1,'end':10,'dependencies':[]},
    {'id':2,'name':'基础工程','start':8,'end':30,'dependencies':[1]},
    {'id':3,'name':'主体施工','start':28,'end':55,'dependencies':[2]},
    {'id':4,'name':'竣工','start':56,'end':60,'dependencies':[3],'isMilestone':False},
]}
out = render_mermaid_to_bytes(json.dumps(plan, ensure_ascii=False), 'gantt', skip_http=True)
results.append(('gantt', out))

# 3. 架构图
arch = {'label':'项目经理','children':[
    {'label':'技术负责人','children':[{'label':'技术部','children':[]}]},
    {'label':'生产负责人','children':[{'label':'施工部','children':[]}]}
]}
out = render_mermaid_to_bytes(json.dumps(arch, ensure_ascii=False), 'architecture', skip_http=True)
results.append(('architecture', out))

# 4. 时间轴
tl = {'milestones':[
    {'date':'2026-01','title':'开工','status':'completed'},
    {'date':'2026-06','title':'主体封顶','status':'in_progress'},
    {'date':'2026-12','title':'竣工','status':'pending'}
]}
out = render_mermaid_to_bytes(json.dumps(tl, ensure_ascii=False), 'timeline', skip_http=True)
results.append(('timeline', out))

# 5. 布局图
lo = {'zones':[
    {'id':'Z1','name':'办公区','x':10,'y':10,'w':20,'h':15,'color':'#FFE4B5'},
    {'id':'Z2','name':'材料堆场','x':40,'y':10,'w':25,'h':15,'color':'#87CEEB'},
    {'id':'Z3','name':'加工区','x':10,'y':35,'w':20,'h':20,'color':'#90EE90'},
    {'id':'Z4','name':'主体施工区','x':40,'y':35,'w':30,'h':25,'color':'#FFB6C1'},
]}
out = render_mermaid_to_bytes(json.dumps(lo, ensure_ascii=False), 'layout', skip_http=True)
results.append(('layout', out))

# 6. 劳动力
lb = {'phases':['施工准备','基础','主体','装修','验收'],
      'categories':['管理','技工','普工','特殊'],
      'data':[[5,8,10,2],[8,15,20,4],[10,25,30,6],[8,18,22,4],[5,6,8,2]]}
out = render_mermaid_to_bytes(json.dumps(lb, ensure_ascii=False), 'labor', skip_http=True)
results.append(('labor', out))

# 7. 对比图
cmp = {'headers':['指标','方案A','方案B'],'rows':[{'label':'工期','values':[30,45]},{'label':'成本','values':[100,80]}]}
out = render_mermaid_to_bytes(json.dumps(cmp, ensure_ascii=False), 'comparison', skip_http=True)
results.append(('comparison', out))

# 输出
ok = 0
for name, out in results:
    if out is None:
        print(f'  {name:12s} FAIL (None)')
    else:
        b = len(out.getvalue())
        marker = 'OK' if b > 1000 else 'WARN'
        print(f'  {name:12s} {marker:4s} {b:>7d} bytes')
        if b > 1000:
            ok += 1

# ChartCache
cache = ChartCache(cache_dir='./_test_cache')
print(f'  ChartCache    OK   {cache.__class__.__name__}')

print(f'\nResult: {ok}/7 图表渲染通过 (>1KB)')
