# -*- coding: utf-8 -*-
"""
图表渲染管线质量检查 — 7 类图表逐个渲染并验证输出质量
检查项：DPI、像素尺寸、字体、是否有效 PNG、DOCX 插入尺寸
"""
import sys, os, json, io, struct, traceback
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.ai.mermaid_renderer import render_mermaid_to_bytes

# ============ 7 类图表的测试数据 ============
TEST_DATA = {
    "gantt": {
        "title": "施工总进度计划",
        "tasks": [
            {"name": "土方开挖", "start_day": 1, "duration": 30, "resource": "土方组"},
            {"name": "基础浇筑", "start_day": 31, "duration": 60, "resource": "混凝土组", "dependencies": ["土方开挖"]},
            {"name": "主体施工", "start_day": 91, "duration": 180, "resource": "土建组", "dependencies": ["基础浇筑"]},
            {"name": "装饰工程", "start_day": 271, "duration": 90, "resource": "装饰组", "dependencies": ["主体施工"]},
        ],
        "highlight_critical_path": True,
    },
    "flowchart": {
        "title": "施工工艺流程图",
        "nodes": [
            {"id": "A", "label": "施工准备", "type": "start"},
            {"id": "B", "label": "测量放线", "type": "process"},
            {"id": "C", "label": "土方开挖", "type": "process"},
            {"id": "D", "label": "基础浇筑", "type": "process"},
            {"id": "E", "label": "验收合格?", "type": "decision"},
            {"id": "F", "label": "返工处理", "type": "process"},
            {"id": "G", "label": "主体施工", "type": "process"},
            {"id": "H", "label": "竣工验收", "type": "end"},
        ],
        "edges": [
            {"from": "A", "to": "B"}, {"from": "B", "to": "C"}, {"from": "C", "to": "D"},
            {"from": "D", "to": "E"}, {"from": "E", "to": "G", "label": "是"},
            {"from": "E", "to": "F", "label": "否"}, {"from": "F", "to": "D"}, {"from": "G", "to": "H"},
        ],
    },
    "architecture": {
        "title": "项目组织架构图",
        "name": "项目经理", "type": "manager",
        "children": [
            {"name": "技术负责人", "type": "tech", "children": [
                {"name": "施工员", "type": "staff"},
                {"name": "质检员", "type": "staff"},
            ]},
            {"name": "商务负责人", "type": "business", "children": [
                {"name": "造价员", "type": "staff"},
                {"name": "材料员", "type": "staff"},
            ]},
            {"name": "安全负责人", "type": "safety", "children": [
                {"name": "安全员", "type": "staff"},
            ]},
        ],
    },
    "labor": {
        "title": "劳动力投入计划",
        "phases": ["基础阶段", "主体阶段", "装饰阶段"],
        "categories": ["土建工", "钢筋工", "木工", "混凝土工", "普工"],
        "data": [
            [30, 20, 10],   # 土建工
            [15, 40, 15],   # 钢筋工
            [20, 35, 25],   # 木工
            [25, 25, 15],   # 混凝土工
            [40, 30, 20],   # 普工
        ],
    },
    "comparison": {
        "title": "施工方案对比",
        "headers": ["方案", "工期(天)", "成本(万)", "质量评级"],
        "rows": [
            ["方案A", "365", "5000", "优"],
            ["方案B", "400", "4800", "良"],
            ["方案C", "330", "5500", "优"],
        ],
    },
    "layout": {
        "title": "施工现场平面布置图",
        "zones": [
            {"name": "办公区", "x": 10, "y": 10, "w": 20, "h": 15},
            {"name": "生活区", "x": 10, "y": 30, "w": 20, "h": 15},
            {"name": "加工区", "x": 35, "y": 10, "w": 25, "h": 20},
            {"name": "材料堆场", "x": 35, "y": 35, "w": 25, "h": 15},
            {"name": "施工区", "x": 65, "y": 10, "w": 30, "h": 40},
        ],
        "width": 100, "height": 60,
    },
    "timeline": {
        "title": "里程碑计划",
        "events": [
            {"date": "2026-03-01", "title": "开工典礼", "description": "项目正式启动"},
            {"date": "2026-06-30", "title": "基础完工", "description": "±0.000以下完成"},
            {"date": "2026-12-31", "title": "主体封顶", "description": "结构封顶完成"},
            {"date": "2027-06-30", "title": "竣工交付", "description": "整体验收通过"},
        ],
    },
}

def check_png_quality(png_bytes: bytes, chart_name: str):
    """检查 PNG 图片质量"""
    from PIL import Image
    issues = []
    
    # 1. 校验 PNG 头
    if png_bytes[:8] != b'\x89PNG\r\n\x1a\n':
        print(f"  ❌ 非 PNG 格式!")
        return None
    
    # 2. 用 PIL 完整检查
    try:
        with Image.open(io.BytesIO(png_bytes)) as img:
            w, h = img.size
            dpi = img.info.get("dpi", (0, 0))
            color_mode = img.mode
            
            # 计算在 DOCX 中的物理尺寸（cm）
            dpi_x = dpi[0] if dpi and dpi[0] > 0 else 96
            phys_w_cm = (w / dpi_x) * 2.54
            phys_h_cm = (h / dpi_x) * 2.54
            
            # DPI 检查
            dpi_ok = dpi[0] >= 280 if dpi else False
            dpi_status = "✅" if dpi_ok else "⚠️"
            
            # 分辨率检查（目标：2.5x scale 在 300 DPI 下 ≈ 物理尺寸 * 300/2.54 * 2.5）
            # 在 A4 可用宽度 16cm 下，理想像素 ≈ 16/2.54 * 300 ≈ 1890
            res_ok = 1000 <= w <= 4000 and 500 <= h <= 4000
            res_status = "✅" if res_ok else "⚠️"
            
            # 文件大小检查
            size_kb = len(png_bytes) / 1024
            size_ok = size_kb >= 50
            size_status = "✅" if size_ok else "⚠️"
            
            print(f"  {dpi_status} DPI: {dpi[0]}x{dpi[1]}")
            print(f"  {res_status} 像素: {w}x{h} ({phys_w_cm:.1f}x{phys_h_cm:.1f}cm @{dpi[0]}dpi)")
            print(f"  {size_status} 文件大小: {size_kb:.1f}KB")
            print(f"     色彩模式: {color_mode}")
            
            if not dpi_ok: issues.append(f"DPI {dpi[0]} < 280")
            if not res_ok: issues.append(f"像素尺寸 {w}x{h} 异常")
            if not size_ok: issues.append(f"文件过小 {size_kb:.0f}KB")
            
            return {"w": w, "h": h, "dpi": dpi, "phys_w_cm": phys_w_cm, "phys_h_cm": phys_h_cm,
                    "size_kb": size_kb, "issues": issues}
    except Exception as e:
        print(f"  ❌ PIL 验证失败: {e}")
        return None

# ============ 渲染测试 ============
print("=" * 70)
print("图表渲染管线质量检查 — render_mermaid_to_bytes")
print("=" * 70)

results = {}
for chart_type, payload in TEST_DATA.items():
    print(f"\n{'─' * 50}")
    print(f"▶ 渲染 {chart_type:14s} ... ", end="", flush=True)
    
    try:
        payload_str = json.dumps(payload, ensure_ascii=False)
        stream = render_mermaid_to_bytes(payload_str, chart_type, skip_http=True)
        
        if stream is None:
            print("❌ 返回 None")
            results[chart_type] = None
            continue
        
        png_bytes = stream.getvalue()
        print(f"✅ ({len(png_bytes)} bytes)")
        q = check_png_quality(png_bytes, chart_type)
        results[chart_type] = q
        
    except Exception as e:
        print(f"❌ 异常: {e}")
        traceback.print_exc()
        results[chart_type] = None

# ============ 汇总 ============
print(f"\n{'=' * 70}")
print("质量汇总")
print("=" * 70)

passed = 0
total = len(TEST_DATA)
for chart_type, q in results.items():
    if q and not q.get("issues"):
        print(f"  ✅ {chart_type:14s} — DPI={q['dpi'][0]}, {q['w']}x{q['h']}px, {q['phys_w_cm']:.1f}cm, {q['size_kb']:.0f}KB")
        passed += 1
    elif q:
        print(f"  ⚠️  {chart_type:14s} — {', '.join(q['issues'])}")
    else:
        print(f"  ❌ {chart_type:14s} — 渲染失败")

print(f"\n通过率: {passed}/{total}")

# ============ 用成功的图生成 DOCX ============
print(f"\n{'=' * 70}")
print("DOCX 集成测试 — 用成功渲染的图表生成 Word 文档")
print("=" * 70)

try:
    from docx import Document
    from docx.shared import Cm, Pt
    from app.services.export import _add_diagram_picture
    
    doc = Document()
    
    # 页面设置
    section = doc.sections[0]
    section.page_width = Cm(21)
    section.page_height = Cm(29.7)
    section.left_margin = Cm(2.5)
    section.right_margin = Cm(2.5)
    section.top_margin = Cm(2.5)
    section.bottom_margin = Cm(2.5)
    
    # Mock config
    class MockConfig:
        image_max_width_cm = 16.0
        image_dpi = 300
        font_body = "宋体"
        size_body = 10.5
    
    config = MockConfig()
    
    doc.add_heading("图表渲染质量检查报告", level=1)
    doc.add_paragraph("本文档由自动化脚本生成，用于验证 7 类图表的 DOCX 导出效果。")
    
    fig_counter = 0
    for chart_type, payload in TEST_DATA.items():
        q = results.get(chart_type)
        if not q:
            continue
        
        payload_str = json.dumps(payload, ensure_ascii=False)
        stream = render_mermaid_to_bytes(payload_str, chart_type, skip_http=True)
        if not stream:
            continue
        
        fig_counter += 1
        doc.add_heading(f"图表 {fig_counter}: {chart_type.upper()} — {payload.get('title', '')}", level=2)
        
        caption = f"图{fig_counter}-{fig_counter} {payload.get('title', '')}"
        _add_diagram_picture(doc, stream, config, caption=caption)
        
        # 添加质量数据说明
        p = doc.add_paragraph()
        p.add_run(f"  [质量数据] ").bold = True
        p.add_run(f"DPI={q['dpi'][0]}, {q['w']}x{q['h']}px, 物理尺寸≈{q['phys_w_cm']:.1f}cm, 文件大小={q['size_kb']:.0f}KB")
    
    out_path = os.path.join(os.path.dirname(__file__), "chart_quality_report.docx")
    doc.save(out_path)
    size_mb = os.path.getsize(out_path) / 1024 / 1024
    print(f"\n✅ DOCX 已生成: {out_path} ({size_mb:.2f} MB)")
    print(f"   包含 {fig_counter} 张成功渲染的图表")
    
except Exception as e:
    print(f"❌ DOCX 生成失败: {e}")
    traceback.print_exc()
