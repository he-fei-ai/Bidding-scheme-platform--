import sys
sys.path.insert(0, r"J:/编程/招投标方案平台/backend")
from app.utils.json_parser import extract_json_from_text, safe_json_parse

results = []

# Test 1: Plain JSON
t1 = '{"type": "flowchart", "nodes": ["A", "B", "C"]}'
r1 = extract_json_from_text(t1)
results.append(("plain json", r1 is not None and r1.get("type") == "flowchart"))

# Test 2: Markdown fenced
t2 = '```json\n{"type": "gantt", "tasks": [{"name": "T1", "start": 1, "end": 5}]}\n```'
r2 = extract_json_from_text(t2)
results.append(("markdown fenced", r2 is not None and r2.get("type") == "gantt"))

# Test 3: Text with embedded JSON
t3 = 'Some intro.\n\n{"type": "labor", "phases": ["P1"], "categories": ["C1"], "data": [[10]]}\n\nMore.'
r3 = extract_json_from_text(t3)
results.append(("embedded json", r3 is not None and r3.get("type") == "labor"))

# Test 4: Nested braces
t4 = '{"a": {"b": {"c": 1}}}'
r4 = extract_json_from_text(t4)
results.append(("nested braces", r4 is not None and r4.get("a", {}).get("b", {}).get("c") == 1))

# Test 5: Invalid
r5 = extract_json_from_text("not json at all")
results.append(("invalid -> None", r5 is None))

# Test 6: Empty
r6 = extract_json_from_text("")
results.append(("empty -> None", r6 is None))

# Test 7: String with escaped quotes inside JSON
t7 = '{"desc": "a \\"quoted\\" string", "value": 42}'
r7 = extract_json_from_text(t7)
results.append(("escaped quotes", r7 is not None and r7.get("value") == 42))

# Test 8: JSON array
t8 = '[{"a": 1}, {"b": 2}]'
r8 = extract_json_from_text(t8)
results.append(("json array", r8 is not None and isinstance(r8, list) and len(r8) == 2))

# Test 9: JSON with trailing text after closing brace
t9 = '{"x": 1} some trailing text'
r9 = extract_json_from_text(t9)
results.append(("trailing text", r9 is not None and r9.get("x") == 1))

# Test 10: safe_json_parse direct
r10 = safe_json_parse('{"ok": true}', default={})
results.append(("safe_json_parse", r10.get("ok") == True))

# Test 11: safe_json_parse invalid
r11 = safe_json_parse("not json", default="fallback")
results.append(("safe_json_parse invalid", r11 == "fallback"))

print("=== JSON Parser Tests ===")
all_pass = True
for name, passed in results:
    status = "PASS" if passed else "FAIL"
    if not passed:
        all_pass = False
    print(f"  [{status}] {name}")

print()
print("OVERALL:", "ALL PASSED" if all_pass else "SOME FAILED")
