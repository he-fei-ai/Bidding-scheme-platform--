#!/usr/bin/env python3
"""测试前端 ContentPreview.tsx 解析逻辑的边界情况"""
import re
import json

# ============================================================
# 模拟 parseChartPayload
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
            try:
                data = json.loads(rest)
                innerType = (data.get("type") or prefix).lower()
                return {"type": innerType, "data": data}
            except json.JSONDecodeError:
                return {"type": prefix, "data": {"raw": rest}}

    try:
        data = json.loads(text)
        innerType = (data.get("type") or "flowchart").lower()
        return {"type": innerType, "data": data}
    except json.JSONDecodeError:
        return {"type": "flowchart", "data": {"nodes": [s.strip() for s in re.split(r'[，,、\n]', raw) if s.strip()]}}


print("=" * 60)
print("parseChartPayload 测试")
print("=" * 60)

tests = [
    # (input, description, expected_type)
    ('<<CHART:flowchart:{"nodes":["A"]}>>', '双尖括号+COLON前缀', 'flowchart'),
    ('[[chart:flowchart:{"nodes":["A"]}]]', '双中括号+COLON前缀', 'flowchart'),
    ('<<CHART:{"type":"gantt"}>>', '双尖括号无内type', 'gantt'),
    ('{"type":"architecture"}', '纯JSON无包裹', 'architecture'),
    ('flowchart:json', '前缀但JSON无效', 'flowchart'),  # fallback to flowchart
    ('plain text no chart', '纯文本无图表标记', 'flowchart'),  # fallback
    ('<<表格:test>>', '表格标记被当图表处理', 'flowchart'),  # BUG候选: 表格标记不应进入parseChartPayload
]

for raw, desc, expected in tests:
    try:
        r = parseChartPayload(raw)
        result = "PASS" if r["type"] == expected else f"UNEXPECTED(got={r['type']})"
    except Exception as e:
        result = f"ERROR: {e}"
    print(f"  [{result}] {desc}")
    print(f"           input={raw[:50]}... type={r['type'] if 'r' in dir() else '?'}")


# ============================================================
# 模拟 parseTableMarkup
# ============================================================
def parseTableMarkup(text):
    cleaned = text.replace("^<<表格[:：]", "").replace(">>$", "").strip()
    # Fix: use actual regex
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
print("parseTableMarkup 测试")
print("=" * 60)

table_tests = [
    ('<<表格：标题\n|列1|列2|\n|a|b|', '正常表格'),
    ('<<表格：标题\n|列1|列2|\n|a|b|\n>>', '带结尾>>'),
    ('标题\n|列1|列2|\n|a|b|', '无包裹符'),
    ('<<表格：单行>>', '单行（应返回None）'),
    ('纯文本内容', '无表格标记'),
    ('<<表格：\n|||\n>>', '空表头'),
    ('<<表格：标题\n|列1|列2|\n>>', '只有表头无数据行'),
]

for raw, desc in table_tests:
    r = parseTableMarkup(raw)
    if r:
        print(f"  [OK] {desc} -> title={r['title']!r}, headers={r['headers']}, rows={len(r['rows'])}")
    else:
        print(f"  [None] {desc}")


# ============================================================
# 模拟 parseContent (block 分割逻辑)
# ============================================================
def parseContent(content):
    blocks = []
    lines = content.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        # 表格检测
        if re.match(r"^<<表格[:：]", line):
            tableLines = [line]
            i += 1
            while i < len(lines):
                tline = lines[i].strip()
                if tline.endswith(">>"):
                    tableLines.append(tline)
                    i += 1
                    break
                if tline:
                    tableLines.append(tline)
                i += 1
            blocks.append({"type": "table", "value": "\n".join(tableLines)})
            continue

        # 图表检测
        if re.match(r"^<<(CHART|FLOWCHART|GANTT|ARCHITECTURE|LABOR|COMPARISON|LAYOUT|TIMELINE)\s*:", line, re.IGNORECASE) or \
           re.match(r"^\[\[(CHART|FLOWCHART|GANTT|ARCHITECTURE|LABOR|COMPARISON|LAYOUT|TIMELINE)\s*:", line, re.IGNORECASE):
            chartLines = [line]
            i += 1
            isDoubleBracket = line.strip().startswith("[[")
            endMarker = "]]" if isDoubleBracket else ">>"
            while i < len(lines):
                chartLines.append(lines[i])
                if lines[i].strip().endswith(endMarker):
                    i += 1
                    break
                i += 1
            blocks.append({"type": "chart", "value": "\n".join(chartLines)})
            continue

        if blocks and blocks[-1]["type"] == "text":
            blocks[-1] = {"type": "text", "value": blocks[-1]["value"] + "\n" + line}
        else:
            blocks.append({"type": "text", "value": line})
        i += 1
    return blocks


print()
print("=" * 60)
print("parseContent 测试")
print("=" * 60)

content_tests = [
    ("段落一\n段落二", "纯文本"),
    ("段落一\n<<表格：标题\n|列1|列2|\n|a|b|>>\n段落二", "文本+表格+文本"),
    ("段落一\n<<CHART:flowchart:{\"nodes\":[\"A\",\"B\"]}>>\n段落二", "文本+图表+文本"),
    ("<<CHART:flowchart:\n{\"nodes\":[\"A\",\"B\"]}\n>>", "多行图表块"),
    ("<<CHART:flowchart:{\"nodes\":[\"A\",\">>\"]}>>", "JSON内含>>字符"),
    ("第一段\n第二段\n第三段", "多段文本合并"),
    ("<<表格：A\n|H1|H2|\n|R1|R2|>>\n<<CHART:gantt:{\"tasks\":[]}>>", "表格后接图表"),
]

for raw, desc in content_tests:
    blocks = parseContent(raw)
    types = [b["type"] for b in blocks]
    print(f"  [{desc}] -> {types}")
    for b in blocks:
        if b["type"] == "chart":
            val = b["value"]
            print(f"           chart value: {val[:80]}{'...' if len(val)>80 else ''}")
