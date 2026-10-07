#!/usr/bin/env python3
"""测试 DiagramPreview.tsx 中的 jsonToMermaid 转换逻辑"""
import json

def jsonToMermaid(code):
    """模拟前端 jsonToMermaid 函数"""
    try:
        data = json.loads(code.strip())
        chartType = data.get("type", "")
        title = data.get("title", "")
        nodes = data.get("nodes", [])

        if chartType == "flowchart":
            lines = ["flowchart TD"]
            if nodes:
                for i in range(len(nodes)):
                    nid = nodes[i] if isinstance(nodes[i], str) else (nodes[i].get("label") or f"节点{i+1}")
                    lines.append(f'    N{i}["{nid}"]')
                    if i > 0:
                        lines.append(f"    N{i-1} --> N{i}")
            return "\n".join(lines)

        if chartType == "gantt":
            tasks = data.get("tasks", [])
            if tasks:
                lines = ["gantt", f"    title {title or '进度计划'}", "    dateFormat  YYYY-MM-DD", "    axisFormat  %m月%d日"]
                baseDate = "2025-01-01"
                for i, task in enumerate(tasks):
                    if isinstance(task, dict):
                        taskName = task.get("name") or task.get("label") or f"任务{i+1}"
                        start = task.get("start", i + 1)
                        end = task.get("end", start + 5)
                    else:
                        taskName = str(task)
                        start = i * 5 + 1
                        end = start + 30
                    duration = max(1, end - start + 1)
                    lines.append(f"    {taskName} :a{i+1}, {baseDate}, {duration}d")
                return "\n".join(lines)

        if chartType == "comparison" and data.get("rows"):
            headers = data.get("headers") or ["项目", "数值"]
            lines = ["graph TD"]
            lines.append(f'    Title["{title or "对比图"}"]')
            for i, row in enumerate(data["rows"]):
                label = row.get(headers[0]) if isinstance(row, dict) else (row[0] if isinstance(row, list) else f"项{i+1}")
                value = row.get(headers[1] if len(headers) > 1 else "数值") if isinstance(row, dict) else (row[1] if len(row) > 1 else 0)
                lines.append(f'    Node{i}["{label}: {value}"]')
            for i in range(len(data["rows"])):
                if i > 0:
                    lines.append(f"    Node{i-1} --> Node{i}")
            return "\n".join(lines)

        if chartType == "labor" and data.get("phases") and data.get("categories"):
            lines = ["gantt", f"    title {title or '劳动力投入计划'}", "    dateFormat  X"]
            for p, phase in enumerate(data["phases"]):
                rowData = data.get("data", [])[p] if isinstance(data.get("data"), list) else []
                values = rowData if isinstance(rowData, list) else (rowData.get("values") or []) if isinstance(rowData, dict) else []
                total = sum(values) if values else 0
                lines.append(f"    {phase} :a{p+1}, {p+1}, {max(total, 1)}d")
            return "\n".join(lines)

        if chartType == "architecture" and (data.get("children") or data.get("label")):
            lines = ["flowchart TD"]
            nodeId = [0]
            def traverse(node, parentId=None):
                label = node.get("label") or node.get("name") or node.get("title") or "未命名"
                currentId = f"N{nodeId[0]}"
                lines.append(f'    {currentId}["{label}"]')
                if parentId:
                    lines.append(f"    {parentId} --> {currentId}")
                if node.get("children") and isinstance(node["children"], list):
                    for child in node["children"]:
                        traverse(child, currentId)
                nodeId[0] += 1
            traverse(data)
            return "\n".join(lines)

        if chartType == "layout" and data.get("nodes"):
            lines = ["flowchart LR"]
            for i, node in enumerate(data["nodes"]):
                label = node if isinstance(node, str) else (node.get("label") or node.get("name") or f"节点{i+1}")
                lines.append(f'    N{i}["{label}"]')
            if data.get("edges") and isinstance(data["edges"], list):
                for edge in data["edges"]:
                    from_idx = edge.get("from", edge.get("source", 0))
                    to_idx = edge.get("to", edge.get("target", 1))
                    label = edge.get("label", "")
                    arrow = f'-->|{label}|' if label else "-->"
                    lines.append(f"    N{from_idx} {arrow} N{to_idx}")
            return "\n".join(lines)

        if chartType == "timeline" and data.get("events"):
            lines = ["journey", f"    title {title or '时间线'}"]
            for i, event in enumerate(data["events"]):
                taskName = event.get("title") or event.get("label") or f"事件{i+1}"
                date = event.get("date") or event.get("time") or ""
                lines.append(f"    {taskName} :{i+1}: {date}")
            return "\n".join(lines)

    except json.JSONDecodeError:
        pass
    except Exception as e:
        print(f"  ERROR in jsonToMermaid: {e}")
    return None


print("=" * 60)
print("jsonToMermaid 测试")
print("=" * 60)

tests = [
    ('{"type":"flowchart","nodes":["开始","处理","结束"]}', "flowchart基本"),
    ('{"type":"gantt","title":"进度计划","tasks":[{"name":"设计","start":1,"end":10},{"name":"开发","start":11,"end":30}]}', "gantt双任务"),
    ('{"type":"architecture","label":"根节点","children":[{"label":"子A","children":[{"label":"孙A1"}]},{"label":"子B"}]}', "architecture嵌套"),
    ('{"type":"labor","phases":["基础","主体","装饰"],"categories":["钢筋工","木工","混凝土工"],"data":[[10,5,8],[15,12,10],[5,3,2]]}', "labor矩阵"),
    ('{"type":"comparison","rows":[{"项目":"A","数值":30},{"项目":"B","数值":70}],"headers":["项目","数值"]}', "comparison.rows"),
    ('{"type":"layout","nodes":["生产区","生活区","办公区"],"edges":[{"from":0,"to":1,"label":"通道"}]}', "layout带边"),
    ('{"type":"timeline","title":"里程碑","events":[{"title":"开工","date":"2025-01-01"},{"title":"竣工","date":"2025-12-31"}]}', "timeline双事件"),
    ('invalid json', "无效JSON"),
    ('{"type":"unknown","data":{}}', "未知类型"),
]

for raw, desc in tests:
    result = jsonToMermaid(raw)
    if result:
        first_line = result.split('\n')[0][:60]
        print(f"  [OK]   {desc} -> {first_line}...")
    else:
        print(f"  [NULL] {desc}")

# 关键测试：含特殊字符的 JSON
print()
print("=" * 60)
print("边缘测试：含特殊字符")
print("=" * 60)

special_tests = [
    ('{"type":"flowchart","nodes":["含「书名号」的节点","普通节点"]}', "节点含书名号"),
    ('{"type":"flowchart","nodes":["含\"引号\"的节点"]}', '节点含引号'),
    ('{"type":"flowchart","nodes":["含\\n换行符的节点"]}', "节点含换行符"),
    ('{"type":"gantt","tasks":[{"name":"任务1（备注）","start":1,"end":5}]}', "任务名含括号"),
    ('{"type":"architecture","label":"项目总览","children":[{"label":"部门A","children":[]},{"label":"部门B","children":[{"label":""}]}]}', "空标签节点"),
]

for raw, desc in special_tests:
    result = jsonToMermaid(raw)
    if result:
        print(f"  [OK]   {desc} -> OK")
        # Check for mermaid syntax issues
        if '\n' in raw or '"' in raw:
            # Look for potential issues
            if '\\n' in result or '""' in result or '    N' not in result:
                print(f"         WARNING: potential mermaid syntax issue")
    else:
        print(f"  [FAIL] {desc} -> NULL")
