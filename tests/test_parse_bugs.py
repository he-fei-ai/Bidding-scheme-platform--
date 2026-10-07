#!/usr/bin/env python3
"""验证前端解析模块的 BUG"""
import json
import re

# ============================================================
# Bug 1: parseChartPayload 类型优先级
# ============================================================
print("=" * 60)
print("Bug 1: parseChartPayload 类型优先级")
print("=" * 60)

def simulate_parse_chart_payload(raw):
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
            try:
                data = json.loads(rest)
                innerType = (data.get("type") or prefix).lower()
                return {"type": innerType, "data": data}
            except json.JSONDecodeError:
                return {"type": prefix, "data": {"raw": rest}}
    return None

tests = [
    ('<<CHART:flowchart:{"nodes":["A"]}>}', 'CHART包裹flowchart'),
    ('<<flowchart:{"nodes":["A"]}>}', '直接flowchart包裹'),
    ('<<CHART:gantt:{"tasks":[]}>}', 'CHART包裹gantt'),
    ('<<gantt:{"tasks":[]}>}', '直接gantt包裹'),
]

for raw, desc in tests:
    result = simulate_parse_chart_payload(raw)
    if result:
        print(f"  [{desc}] type={result['type']!r}")
    else:
        print(f"  [{desc}] -> None")

# ============================================================
# Bug 2: jsonToMermaid 引号未转义
# ============================================================
print()
print("=" * 60)
print("Bug 2: jsonToMermaid 引号未转义")
print("=" * 60)

def simulate_json_to_mermaid(code):
    try:
        data = json.loads(code.strip())
        chartType = data.get("type", "")
        nodes = data.get("nodes", [])
        if chartType == "flowchart" and nodes:
            lines = ["flowchart TD"]
            for i, nid in enumerate(nodes):
                # 模拟前端: lines.push(`    N${i}["${nid}"]`)
                line = f'    N{i}["{nid}"]'
                lines.append(line)
            return "\n".join(lines)
    except Exception as e:
        return f"ERROR: {e}"
    return None

special_tests = [
    ('{"type":"flowchart","nodes":["含\"引号\"的节点"]}', '含引号的节点'),
    ('{"type":"flowchart","nodes":["普通节点"]}', '普通节点'),
    ('{"type":"flowchart","nodes":["含\\n换行的节点"]}', '含换行符的节点'),
]

for raw, desc in special_tests:
    result = simulate_json_to_mermaid(raw)
    print(f"  [{desc}] -> {result!r}")
    if result and '\\n' in result and '"\\n"' in result:
        print(f"         WARNING: 换行符未转义，Mermaid渲染会失败!")
    if result and '""' in result:
        print(f"         WARNING: 存在连续双引号，Mermaid可能解析失败!")

# ============================================================
# Bug 3: parseTableMarkup 多行尾部 >> 处理
# ============================================================
print()
print("=" * 60)
print("Bug 3: parseTableMarkup 多行尾部 >> 处理")
print("=" * 60)

def simulate_parse_table_markup(text):
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

table_tests = [
    ('<<表格：标题\n|列1|列2|\n|a|b|\n>>', '多行表格带尾部换行>>'),
    ('<<表格：标题\n|列1|列2|\n|a|b|>>', '多行表格无换行>>'),
    ('<<表格：标题\n|列1|列2|\n|a|b|', '无尾部>>'),
]

for raw, desc in table_tests:
    r = simulate_parse_table_markup(raw)
    if r:
        print(f"  [OK] {desc} -> title={r['title']!r}, headers={r['headers']}, rows={len(r['rows'])}")
    else:
        print(f"  [None] {desc}")

# ============================================================
# Bug 4: parseContent 单行图表检测逻辑
# ============================================================
print()
print("=" * 60)
print("Bug 4: parseContent 单行/多行图表检测")
print("=" * 60)

def simulate_parse_content(content):
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

content_tests = [
    ("段落一\n<<CHART:flowchart:{\"nodes\":[\"A\"]}>>\n段落二", "单行图表"),
    ("<<CHART:flowchart:\n{\"nodes\":[\"A\"]}\n>>", "多行图表"),
]

for raw, desc in content_tests:
    blocks = simulate_parse_content(raw)
    types = [b["type"] for b in blocks]
    print(f"  [{desc}] -> {types}")
    for b in blocks:
        if b["type"] == "chart":
            print(f"           value: {b['value'][:80]}...")

print()
print("=" * 60)
print("总结")
print("=" * 60)
print("Bug 1: <<CHART:flowchart:...>> 格式解析后 type='chart' 而非 'flowchart'")
print("       原因: prefix='chart', rest='flowchart:{...}' JSON解析失败, fallback到prefix='chart'")
print("       影响: 使用CHART包裹时，内层类型丢失，图表无法正确渲染")
print()
print("Bug 2: jsonToMermaid 中节点文本含引号时未转义")
print("       原因: Mermaid模板直接使用f-string插入文本，未对引号转义")
print("       影响: 含引号的节点会导致Mermaid语法错误")
print()
print("Bug 3: parseTableMarkup 多行表格尾部换行>>处理")
print("       原因: 先用replace('>>$','')再判断endswith('>>')，逻辑冗余但不影响结果")
print("       影响: 低优先级，功能正常")
