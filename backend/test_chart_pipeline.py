# -*- coding: utf-8 -*-
"""直接测试导出管线的图表部分 —— 模拟 DB 数据，检查图表渲染质量"""
import sys, os, json, io, traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ============ 模拟数据 ============
fake_sections = [
    {
        "id": 1, "project_id": 1946,
        "section_number": "3.1", "section_title": "施工总进度计划",
        "content": "<h2>3.1 施工总进度计划</h2><p>根据施工部署...</p>",
        "gantt_json": json.dumps({
            "title": "施工总进度计划横道图",
            "phases": [
                {"name": "基础工程", "start": "2026-03-01", "end": "2026-05-30", "progress": 0},
                {"name": "主体结构", "start": "2026-06-01", "end": "2026-12-31", "progress": 0},
                {"name": "装饰工程", "start": "2027-01-01", "end": "2027-06-30", "progress": 0},
            ]
        }, ensure_ascii=False),
        "flowchart_json": None,
        "architecture_json": None,
        "labor_json": None,
        "comparison_json": None,
        "layout_json": None,
        "timeline_json": None,
    },
    {
        "id": 2, "project_id": 1946,
        "section_number": "3.2", "section_title": "施工流程",
        "content": "<h2>3.2 施工流程</h2><p>标准施工工艺流程如下...</p>",
        "gantt_json": None,
        "flowchart_json": json.dumps({
            "title": "标准施工工艺流程图",
            "nodes": [
                {"id": "A", "label": "施工准备", "type": "start"},
                {"id": "B", "label": "测量放线", "type": "process"},
                {"id": "C", "label": "土方开挖", "type": "process"},
                {"id": "D", "label": "基础浇筑", "type": "process"},
                {"id": "E", "label": "回填土", "type": "end"},
            ],
            "edges": [
                {"from": "A", "to": "B"}, {"from": "B", "to": "C"},
                {"from": "C", "to": "D"}, {"from": "D", "to": "E"},
            ]
        }, ensure_ascii=False),
        "architecture_json": None,
        "labor_json": None,
        "comparison_json": None,
        "layout_json": None,
        "timeline_json": None,
    },
]

fake_project = {
    "id": 1946, "name": "测试项目",
    "build_area": "10000", "building_height": "50", "floors": "15",
    "structure_type": "框架结构", "duration_days": "365",
    "location": "上海市青浦区", "bid_deadline": "2026-12-01",
}

# ============ 单元测试：mermaid_renderer ============
print("=" * 60)
print("[TEST 1] mermaid_renderer DPI / 字体嵌入")
try:
    from app.services.ai.mermaid_renderer import render_mermaid_to_png
    # 测试 Gantt 渲染
    gantt_code = """gantt
    title 施工总进度计划
    dateFormat  YYYY-MM-DD
    section 基础工程
    土方开挖 :a1, 2026-03-01, 30d
    基础浇筑 :a2, after a1, 60d
    section 主体结构
    主体施工 :b1, after a2, 180d"""
    result = render_mermaid_to_png(gantt_code)
    if result:
        print(f"  ✅ 渲染成功: {len(result)} bytes")
        # 检查 PNG 文件头（IHDR 包含宽高）
        import struct
        if result[:8] == b'\x89PNG\r\n\x1a\n':
            w = struct.unpack('>I', result[16:20])[0]
            h = struct.unpack('>I', result[20:24])[0]
            print(f"  尺寸: {w}x{h}")
            # 检查 pHYs chunk 中的 DPI
            idx = result.find(b'pHYs')
            if idx > 0:
                ppux = struct.unpack('>I', result[idx+4:idx+8])[0]
                ppuy = struct.unpack('>I', result[idx+8:idx+12])[0]
                print(f"  DPI: x={ppux}, y={ppuy}, dpi={'≈96' if ppux < 400 else '≥300'}")
            else:
                print("  ⚠️ 无 pHYs chunk（Mermaid 默认 96 DPI）")
        else:
            print("  ⚠️ 非 PNG 格式")
    else:
        print("  ❌ 渲染返回 None")
except Exception as e:
    print(f"  ❌ 导入/执行错误: {e}")
    traceback.print_exc()

# ============ 单元测试：export 的 _add_diagram_picture ============
print("\n" + "=" * 60)
print("[TEST 2] export._add_diagram_picture 图片缩放逻辑")
try:
    from docx import Document
    from docx.shared import Cm, Emu
    from app.services.export import _add_diagram_picture, _FigureNumberer
    
    doc = Document()
    numberer = _FigureNumberer()
    
    # 创建一张模拟 PNG
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new('RGB', (1200, 600), 'white')
    draw = ImageDraw.Draw(img)
    draw.rectangle([20, 20, 1180, 580], outline='black', width=2)
    draw.text((40, 40), "测试图表标题", fill='black')
    buf = io.BytesIO()
    img.save(buf, format='PNG', dpi=(300, 300))
    png_data = buf.getvalue()
    
    # 调用
    n = _add_diagram_picture(doc, png_data, numberer, "3.1")
    print(f"  返回序号: {n}")
    if n:
        print(f"  ✅ 图表插入成功")
    
    # 检查文档中的图片尺寸
    for rel in doc.part.rels.values():
        if 'image' in rel.reltype:
            target = rel.target_ref
            print(f"  图片关系: {target}")
    
    # 保存检查
    out_path = os.path.join(os.path.dirname(__file__), 'test_chart_export.docx')
    doc.save(out_path)
    print(f"  保存到: {out_path} ({os.path.getsize(out_path)} bytes)")
except Exception as e:
    print(f"  ❌ 错误: {e}")
    traceback.print_exc()

# ============ 单元测试：标题提取 ============
print("\n" + "=" * 60)
print("[TEST 3] _extract_title_from_json 标题提取")
try:
    from app.services.export import _extract_title_from_json
    
    tests = [
        (fake_sections[0]["gantt_json"], "gantt_json"),
        (fake_sections[1]["flowchart_json"], "flowchart_json"),
        ('{}', 'gantt_json'),
        ('{"nodes":[]}', 'flowchart_json'),
    ]
    for data, ftype in tests:
        t = _extract_title_from_json(data, ftype)
        print(f"  [{ftype}] → {repr(t)}")
    print("  ✅ 标题提取测试完成")
except Exception as e:
    print(f"  ❌ 错误: {e}")
    traceback.print_exc()

# ============ 单元测试：logger 是否在 content_cleaning.py 里 ============
print("\n" + "=" * 60)
print("[TEST 4] content_cleaning logger 检查")
try:
    from app.services import content_cleaning
    src = open(content_cleaning.__file__, 'r', encoding='utf-8').read()
    has_import = 'import logging' in src
    has_getlogger = 'logging.getLogger' in src or 'logger = ' in src
    print(f"  import logging: {has_import}, has logger: {has_getlogger}")
    if not has_import or not has_getlogger:
        print("  ⚠️ content_cleaning.py 缺少 logger 定义 — 需要修复！")
except Exception as e:
    print(f"  ❌ 错误: {e}")

print("\n" + "=" * 60)
print("全部测试完成")
