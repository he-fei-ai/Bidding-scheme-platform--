# -*- coding: utf-8 -*-
"""扫描 workflows.py.bak 中未归组的顶层常量（供拆分脚本补齐 CONST_GROUP）"""
import ast
import re
import sys

txt = open('scripts/split_workflows.py', encoding='utf-8').read()

const_m = re.search(r'CONST_GROUP: dict\[str, str\] = \{(.*?)\n\}', txt, re.S)
const_group = dict(re.findall(r'"([A-Za-z_][A-Za-z0-9_]*)": "([a-z_]+)"', const_m.group(1)))

# GROUPS: name -> list
groups = {}
g_m = re.search(r'GROUPS: dict\[str, list\[str\]\] = \{(.*?)\n\}', txt, re.S)
for m in re.finditer(r'"([a-z_]+)": \[(.*?)\]', g_m.group(1), re.S):
    names = [x.strip().strip('"').strip("'") for x in m.group(2).split(',')]
    groups[m.group(1)] = [n for n in names if n]

grouped = set(const_group) | {n for names in groups.values() for n in names}

src = open('app/services/ai/workflows.py.bak', encoding='utf-8').read()
tree = ast.parse(src)

defs = {n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}

consts = []
for n in tree.body:
    if isinstance(n, ast.Assign):
        for t in n.targets:
            if isinstance(t, ast.Name) and t.id not in defs:
                consts.append((t.id, n.lineno))
    elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name) and n.target.id not in defs:
        consts.append((n.target.id, n.lineno))

missing = [(n, ln) for n, ln in consts if n not in grouped]
print('未归组的顶层常量数:', len(missing))
for name, ln in missing:
    print(f'  line {ln}: {name}')
