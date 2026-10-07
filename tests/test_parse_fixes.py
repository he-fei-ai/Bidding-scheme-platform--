#!/usr/bin/env python3
"""验证前端解析模块修复后的正确性"""
import json
import re
import sys

# 修复编码问题
if sys.stdout.encoding != 'utf-8':
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

# ============================================================
# 模拟修复后的 parseChartPayload
# ============================================================
def parseChartPayload(raw):
    text = raw.strip()
    if text.startswith("<<") and text.endswith(">>"):
        text = text[2:-2].strip()
    elif text.startswith("[[") and text.endswith("]]"):
        text = text[2:-2].strip()

    colonIdx = text.find(":")
    if colonIdx > 0:
        prefix = text[:colonIdx].strip().lower()
        valid = ["chart", "flowchart", "gantt", "architecture", "labor", "comparison", "layout", "timeline"]
        if prefix in valid:
            rest = text[colonIdx + 1:].strip()
            # 修复：处理双层包裹格式
            innerPrefix = prefix
            parseRest = rest
            innerColon = rest.find(":")
            if innerColon > 0:
                innerP = rest[:innerColon].strip().lower()
                if innerP in valid:
                    innerPrefix = innerP
                    parseRest = rest[innerColon + 1:].strip()
            try:
                data = json.loads(parseRest)
                innerType = (data.get("type") or innerPrefix).lower()
                return {"type": innerType, "data": data}
            except json.JSONDecodeError:
                return {"type": innerPrefix, "data": {"raw": parseRest}}
    return None

# ============================================================
# 模拟修复后的 jsonToMermaid
# ============================================================
def esc(s):
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r")

def jsonToMermaid(code):
    try:
        data = json.loads(code.strip())
        chartType = data.get("type", "")
        nodes = data.get("nodes", [])
        if chartType == "flowchart" and nodes:
            lines = ["flowchart TD"]
            for i, nid in enumerate(nodes):
                nid_str = nid if isinstance(nid, str) else nid.get("label") or f"节点{i+1}"
                lines.append(f'    N{i}["{esc(nid_str)}"]')
            return "\n".join(lines)
    except Exception as e:
        return f"ERROR: {e}"
    return None

# ============================================================
# 运行测试
# ============================================================
all_pass = True

print("=" * 60)
print("Bug 1 修复验证: parseChartPayload 类型优先级")
print("=" * 60)

tests = [
    ('<<CHART:flowchart:{"nodes":["A"]}>}', 'CHART包裹flowchart', 'flowchart'),
    ('<<flowchart:{"nodes":["A"]}>}', '直接flowchart包裹', 'flowchart'),
    ('<<CHART:gantt:{"tasks":[]}>}', 'CHART包裹gantt', 'gantt'),
    ('<<gantt:{"tasks":[]}>}', '直接gantt包裹', 'gantt'),
    ('<<CHART:{"type":"architecture"}>>', 'CHART包裹带内type', 'architecture'),
    ('{"type":"layout","nodes":["A"]}', '纯JSON无包裹', 'layout'),
]

for raw, desc, expected in tests:
    result = parseChartPayload(raw)
    got = result["type"] if result else None
    status = "PASS" if got == expected else f"FAIL(got={got})"
    if got != expected:
        all_pass = False
    print(f"  [{status}] {desc} -> type={got!r}")

print()
print("=" * 60)
print("Bug 2 修复验证: jsonToMermaid 引号转义")
print("=" * 60)

# 正确构造含引号的JSON测试用例
quote_json = '{"type":"flowchart","nodes":["含\\"引号\\"的节点"]}'
special_tests = [
    (quote_json, '含引号的节点'),
    ('{"type":"flowchart","nodes":["普通节点"]}', '普通节点'),
    ('{"type":"flowchart","nodes":["含\\n换行符的节点"]}', '含换行符的节点'),
    ('{"type":"gantt","tasks":[{"name":"任务1（备注）","start":1,"end":5}]}', '任务名含括号'),
]

for raw, desc in special_tests:
    result = jsonToMermaid(raw)
    if result and "ERROR" not in result:
        lines = result.split("\n")
        # 检查生成的 Mermaid 代码中的引号节点行
        node_lines = [l for l in lines if 'N[' in l]
        has_unescaped = any(l.count('"') % 2 != 0 for l in node_lines)
        status = "PASS" if not has_unescaped else "FAIL(未转义引号)"
        if has_unescaped:
            all_pass = False
        print(f"  [{status}] {desc}")
        for l in node_lines:
            print(f"           -> {l}")
    else:
        print(f"  [FAIL] {desc} -> {result}")
        all_pass = False

print()
print("=" * 60)
if all_pass:
    print("All tests PASSED")
else:
    print("Some tests FAILED")
print("=" * 60)
