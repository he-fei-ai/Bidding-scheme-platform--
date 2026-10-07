#!/usr/bin/env python3
"""最终验证: 前端解析模块所有关键路径"""
import json
import re
import sys
sys.stdout.reconfigure(encoding='utf-8')

passed = 0
failed = 0

def check(desc, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
        print(f"  [PASS] {desc}")
    else:
        failed += 1
        print(f"  [FAIL] {desc} {detail}")

# ============================================================
# 1. parseChartPayload - 各种格式
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
        valid = ["chart","flowchart","gantt","architecture","labor","comparison","layout","timeline"]
        if prefix in valid:
            rest = text[colonIdx+1:].strip()
            innerPrefix = prefix
            parseRest = rest
            innerColon = rest.find(":")
            if innerColon > 0:
                innerP = rest[:innerColon].strip().lower()
                if innerP in valid:
                    innerPrefix = innerP
                    parseRest = rest[innerColon+1:].strip()
            try:
                data = json.loads(parseRest)
                innerType = (data.get("type") or innerPrefix).lower()
                return {"type": innerType, "data": data}
            except json.JSONDecodeError:
                return {"type": innerPrefix, "data": {"raw": parseRest}}
    return None

print("=" * 60)
print("1. parseChartPayload 测试")
print("=" * 60)

# Bug 1 修复验证
json_nodes = json.dumps({"nodes": ["A"]})
r = parseChartPayload("<<CHART:flowchart:" + json_nodes + ">>")
check("CHART包裹flowchart → type=flowchart", r and r["type"] == "flowchart", f"got={r['type'] if r else None}")

r = parseChartPayload("<<flowchart:" + json_nodes + ">>")
check("直接flowchart包裹 → type=flowchart", r and r["type"] == "flowchart", f"got={r['type'] if r else None}")

json_gantt = json.dumps({"tasks": []})
r = parseChartPayload("<<CHART:gantt:" + json_gantt + ">>")
check("CHART包裹gantt → type=gantt", r and r["type"] == "gantt", f"got={r['type'] if r else None}")

r = parseChartPayload("<<CHART:{\"type\":\"architecture\"}>>")
check("CHART包裹带内type → type=architecture", r and r["type"] == "architecture", f"got={r['type'] if r else None}")

r = parseChartPayload("{\"type\":\"layout\",\"nodes\":[\"A\"]}")
check("纯JSON无包裹 → type=layout", r and r["type"] == "layout", f"got={r['type'] if r else None}")

# [[ 包裹
r = parseChartPayload("[[chart:flowchart:" + json_nodes + "]]")
check("双中括号包裹 → type=flowchart", r and r["type"] == "flowchart", f"got={r['type'] if r else None}")

# ============================================================
# 2. jsonToMermaid - 特殊字符
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

print()
print("=" * 60)
print("2. jsonToMermaid 特殊字符测试")
print("=" * 60)

# 引号
node_with_quote = '含"引号"的节点'
test_json = json.dumps({"type": "flowchart", "nodes": [node_with_quote]})
result = jsonToMermaid(test_json)
check("含引号节点 → Mermaid正确转义", result and '\\"' in result and "ERROR" not in result, f"got={result!r}")

# 换行
node_with_newline = "行1\n行2"
test_json2 = json.dumps({"type": "flowchart", "nodes": [node_with_newline]})
result2 = jsonToMermaid(test_json2)
check("含换行节点 → Mermaid正确转义", result2 and "\\n" in result2 and "ERROR" not in result2, f"got={result2!r}")

# 反斜杠
node_with_backslash = r"路径\to\file"
test_json3 = json.dumps({"type": "flowchart", "nodes": [node_with_backslash]})
result3 = jsonToMermaid(test_json3)
check("含反斜杠节点 → Mermaid正确转义", result3 and "\\\\" in result3 and "ERROR" not in result3, f"got={result3!r}")

# 正常节点
test_json4 = json.dumps({"type": "flowchart", "nodes": ["开始", "处理", "结束"]})
result4 = jsonToMermaid(test_json4)
check("普通节点 → 正常渲染", result4 and "flowchart TD" in result4, f"got={result4!r}")

# ============================================================
# 3. parseTableMarkup
# ============================================================
def parseTableMarkup(text):
    cleaned = text.replace("^<<表格[:：]", "").replace(">>$", "").strip()
    cleaned = re.sub(r"^<<表格[:：]", "", cleaned)
    if cleaned.endswith(">>"):
        cleaned = cleaned[:-2]
    cleaned = cleaned.strip()
    lines = [l.strip() for l in cleaned.split("\n") if l.strip()]
    if len(lines) < 2:
        return None
    title = lines[0] if "|" not in lines[0] else ""
    dataStart = 1 if title else 0
    if dataStart >= len(lines):
        return None
    headers = [h.strip() for h in lines[dataStart].split("|") if h.strip()]
    rows = []
    for line in lines[dataStart + 1:]:
        cells = [c.strip() for c in line.split("|") if c.strip()]
        if cells:
            rows.append(cells)
    if not headers:
        return None
    return {"title": title, "headers": headers, "rows": rows}

print()
print("=" * 60)
print("3. parseTableMarkup 测试")
print("=" * 60)

r = parseTableMarkup("<<表格：标题\n|列1|列2|\n|a|b|>>")
check("单行>>包裹表格", r and r["headers"] == ["列1", "列2"] and len(r["rows"]) == 1, f"got={r}")

r = parseTableMarkup("<<表格：标题\n|列1|列2|\n|a|b|\n>>")
check("多行表格带换行>>", r and r["headers"] == ["列1", "列2"] and len(r["rows"]) == 1, f"got={r}")

r = parseTableMarkup("<<表格：标题\n|列1|列2|\n|a|b|")
check("无尾部>>的表格", r and r["headers"] == ["列1", "列2"], f"got={r}")

r = parseTableMarkup("<<表格：单行>>")
check("单行应返回None", r is None, f"got={r}")

# ============================================================
# 4. parseContent
# ============================================================
def parseContent(content):
    blocks = []
    lines = content.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        if re.match(r"^<<表格[:：]", line):
            table_lines = [line]
            i += 1
            while i < len(lines):
                tline = lines[i].strip()
                if tline.endswith(">>"):
                    table_lines.append(tline)
                    i += 1
                    break
                if tline:
                    table_lines.append(tline)
                i += 1
            blocks.append({"type": "table", "value": "\n".join(table_lines)})
            continue
        if re.match(r"^<<(CHART|FLOWCHART|GANTT|ARCHITECTURE|LABOR|COMPARISON|LAYOUT|TIMELINE)\s*:", line, re.IGNORECASE) or \
           re.match(r"^\[\[(CHART|FLOWCHART|GANTT|ARCHITECTURE|LABOR|COMPARISON|LAYOUT|TIMELINE)\s*:", line, re.IGNORECASE):
            if (line.strip().endswith(">>") or line.strip().endswith("]]")) and "\n" not in line:
                blocks.append({"type": "chart", "value": line})
                i += 1
                continue
            chart_lines = [line]
            i += 1
            is_double_bracket = line.strip().startswith("[[")
            end_marker = "]]" if is_double_bracket else ">>"
            while i < len(lines):
                chart_lines.append(lines[i])
                if lines[i].strip().endswith(end_marker):
                    i += 1
                    break
                i += 1
            blocks.append({"type": "chart", "value": "\n".join(chart_lines)})
            continue
        if blocks and blocks[-1]["type"] == "text":
            blocks[-1] = {"type": "text", "value": blocks[-1]["value"] + "\n" + line}
        else:
            blocks.append({"type": "text", "value": line})
        i += 1
    return blocks

print()
print("=" * 60)
print("4. parseContent 测试")
print("=" * 60)

blocks = parseContent("段落一\n<<CHART:flowchart:" + json.dumps({"nodes":["A"]}) + ">>\n段落二")
types = [b["type"] for b in blocks]
check("文本+图表+文本", types == ["text", "chart", "text"], f"got={types}")

blocks = parseContent("<<CHART:flowchart:\n" + json.dumps({"nodes":["A"]}) + "\n>>")
types = [b["type"] for b in blocks]
check("多行图表块", types == ["chart"], f"got={types}")

blocks = parseContent("<<表格：A\n|H1|H2|\n|R1|R2|>>\n<<CHART:gantt:{}>>")
types = [b["type"] for b in blocks]
check("表格后接图表", types == ["table", "chart"], f"got={types}")

# ============================================================
# 总结
# ============================================================
print()
print("=" * 60)
print(f"测试结果: {passed} PASS, {failed} FAIL")
print("=" * 60)
