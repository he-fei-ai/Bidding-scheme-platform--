"""前后端路由对齐检查脚本（一次性）"""
import re
import os

# 1. 提取后端路由
backend = set()
for root, _, files in os.walk('backend/app/routers'):
    for f in files:
        if not f.endswith('.py'):
            continue
        p = os.path.join(root, f)
        with open(p, encoding='utf-8') as fh:
            src = fh.read()
        m = re.search(r'router\s*=\s*APIRouter\(prefix=["\']([^"\']+)["\']', src)
        pref = m.group(1) if m else ''
        for mm in re.finditer(r'@router\.(get|post|put|delete|patch)\(["\']([^"\']+)["\']', src):
            backend.add(pref + mm.group(2))

# 2. 提取前端 axios 调用
fe = set()
for root, _, files in os.walk('frontend/src'):
    if 'node_modules' in root:
        continue
    for f in files:
        if not (f.endswith('.ts') or f.endswith('.tsx')):
            continue
        p = os.path.join(root, f)
        with open(p, encoding='utf-8') as fh:
            src = fh.read()
        # 形式 1: http.get("/api/v1/...")
        for mm in re.finditer(r'http\.[a-z]+[<\(]?[^>]*?\(\s*[`"\'](/api/v1/[^"\'`]+)[`"\'\)]', src):
            fe.add(mm.group(1))
        # 形式 2: 模板字符串拼接 /api/v1/...
        for mm in re.finditer(r'[`"\'](/api/v1/[^"\'`]+)[`"\'\)]', src):
            fe.add(mm.group(1))

# 3. 归一：把后端路由中的 {xxx} 转成通配
def normalize(p):
    p = p.replace('${projectId}', 'X').replace('${fileId}', 'X')
    p = re.sub(r'\$\{[^}]+\}', 'X', p)
    p = p.replace('X', 'X')  # 通用占位
    return p

unmatched = []
fe_clean = set(p for p in fe if not p.startswith('/api/v1/_debug'))
for f_path in fe_clean:
    f_path_n = normalize(f_path)
    matched = False
    for b in backend:
        b_norm = normalize(b)
        b_pat = re.sub(r'\{[^}]+\}', '[^/]+', b_norm)
        if re.match('^' + b_pat + r'(?:/.*)?$', f_path_n):
            matched = True
            break
    if not matched:
        unmatched.append(f_path)

print(f'后端路由数: {len(backend)}')
print(f'前端调用样本数(去重): {len(fe_clean)}')
print(f'潜在未匹配前端调用: {len(unmatched)}')
if unmatched:
    for u in sorted(unmatched)[:20]:
        print(' ', u)
