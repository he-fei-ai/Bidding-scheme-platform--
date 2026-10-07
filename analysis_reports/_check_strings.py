"""查找字符串拼接热点"""
import re
import os

for p in [
    'services/ai/workflows.py',
    'services/ai/workflows_sections.py',
    'services/ai/workflows_outline_v51.py',
    'routers/sections.py',
    'routers/sse_handlers.py',
    'services/export.py',
    'services/ai/mermaid_renderer.py',
]:
    p_abs = os.path.join('.', p.replace('/', os.sep))
    if not os.path.exists(p_abs):
        continue
    with open(p_abs, encoding='utf-8') as f:
        src = f.read()
    # 找 "...." + ... 模式（粗略）
    plus_concat = len(re.findall(r"'[^\n']*'\s*\+\s*", src)) + len(re.findall(r'"\S*?"\s*\+\s*', src))
    fstring = len(re.findall(r"f['\"]", src))
    print(f"{p}: +串接~{plus_concat}, f-string~{fstring}")
