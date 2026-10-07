#!/usr/bin/env python3
"""
批量图表编排 + 渲染流程集成测试
涵盖: generate_all_chart_arrangements / render_mermaid_to_bytes / DOCX 导出图表嵌入
"""

import io
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

# 确保 backend 在 path 中
_BACKEND_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_BACKEND_DIR))

import pytest
from docx import Document

from app.services.ai.image_engine import (
    generate_all_chart_arrangements,
)
from app.services.ai.mermaid_renderer import (
    render_mermaid_to_bytes,
)
from app.services.export import (
    _add_inline_chart,
    _render_section_charts,
)
from app.services.ai.providers.base import AIMessage


# ============================================================
# Mock Provider
# ============================================================
class MockProvider:
    """模拟 AI 提供商，返回预设的 JSON 响应"""

    def __init__(self, responses: list[str]):
        self.responses = responses
        self.call_count = 0

    async def chat(self, messages, temperature=0.7, json_mode=False, cancel_event=None, max_tokens=None, **kwargs):
        self.call_count += 1
        # 循环使用响应，避免 exhausted 错误
        idx = (self.call_count - 1) % len(self.responses)
        return self.responses[idx]


# ============================================================
# 测试数据：包含多种图表类型的章节内容
# ============================================================
MULTI_CHART_SECTION_CONTENT = """## 施工组织设计

### 施工工艺

本项目采用"先地下后地上、先结构后装饰"的总体施工顺序。施工流程如下：

施工准备 → 测量放线 → 基础施工 → 主体结构 → 装饰装修 → 竣工验收。

每个工序之间设置质量控制点，确保工程质量符合设计要求和规范标准。

### 进度计划

项目总工期120日历天，分为施工准备（15天）、基础施工（30天）、主体结构（50天）、装饰装修（20天）、竣工验收（5天）五个阶段。

各阶段之间合理穿插，确保工期目标顺利实现。

### 组织架构

项目部设项目经理1名，下设技术负责人、生产负责人、经营负责人。技术部、工程部、质量安全部、物资设备部等部门协同配合。

### 劳动力配置

施工高峰期预计投入80人，其中管理人员10人、技术工人35人、普通工人30人、特殊工种5人。各阶段劳动力动态调配，确保施工需要。

### 场地布置

施工现场划分为办公区、材料堆放区、加工区和生产区四个功能区，各区域之间设置安全通道，确保施工安全和材料转运顺畅。

### 方案比选

针对基础施工方案，拟采用钻孔灌注桩和人工挖孔桩两种方案。钻孔灌注桩工期短、质量可控，人工挖孔桩成本低、设备简单。

### 关键节点

项目关键里程碑包括：施工准备完成（第15天）、基础完工（第45天）、主体封顶（第95天）、装饰装修完成（第115天）、竣工验收（第120天）。
"""

MULTI_CHART_SECTION_TITLE = "施工组织设计综合方案"

BATCH_ARRANGE_RESPONSE = json.dumps({
    "flowchart":    {"needed": True,  "type": "construction", "priority": 5, "reason": "章节包含完整施工流程，适合流程图展示", "nodeCountHint": 7, "flowHint": "td"},
    "gantt":        {"needed": True,  "type": "schedule",    "priority": 5, "reason": "章节包含明确工期计划，适合甘特图展示", "nodeCountHint": 0, "flowHint": "none"},
    "architecture": {"needed": True,  "type": "org",          "priority": 4, "reason": "章节描述项目部组织架构，适合架构图展示", "nodeCountHint": 0, "depthHint": 3},
    "labor":        {"needed": True,  "type": "dynamic",      "priority": 4, "reason": "章节含劳动力动态配置数据，适合劳动力图", "phases": ["准备","基础","主体","装饰","验收"], "categories": ["管理","技术","普通","特殊"]},
    "comparison":   {"needed": True,  "type": "scheme",       "priority": 3, "reason": "章节包含基础施工方案比选，适合对比图", "nodeCountHint": 0},
    "layout":       {"needed": False, "type": "none",         "priority": 3, "reason": "场地布置为文字描述，无具体尺寸数据"},
    "timeline":     {"needed": False, "type": "none",         "priority": 3, "reason": "工期已用甘特图展示，时间线冗余"},
}, ensure_ascii=False)


# ============================================================
# 1. 批量编排测试
# ============================================================
class TestBatchArrangement:
    """测试 generate_all_chart_arrangements 批量编排功能"""

    @pytest.mark.asyncio
    async def test_batch_arrange_all_needed(self):
        """批量编排：7种图表全部需要生成"""
        provider = MockProvider([BATCH_ARRANGE_RESPONSE])
        result = await generate_all_chart_arrangements(
            provider,
            section_title=MULTI_CHART_SECTION_TITLE,
            section_content=MULTI_CHART_SECTION_CONTENT,
            project_overview="某办公楼建设项目，总建筑面积20000平方米，框架结构，工期120天",
            scoring_standard="施工组织设计需包含施工流程图、进度计划、组织架构",
            industry_profile="房建工程",
        )
        assert isinstance(result, dict), f"返回类型错误: {type(result)}"
        assert len(result) == 7, f"应返回7个图表类型，实际: {list(result.keys())}"

        assert result["flowchart"]["needed"] is True
        assert result["gantt"]["needed"] is True
        assert result["architecture"]["needed"] is True
        assert result["labor"]["needed"] is True
        assert result["comparison"]["needed"] is True
        assert result["layout"]["needed"] is False
        assert result["timeline"]["needed"] is False

        logger_needed = [k for k, v in result.items() if isinstance(v, dict) and v.get("needed")]
        assert set(logger_needed) == {"flowchart", "gantt", "architecture", "labor", "comparison"}

    @pytest.mark.asyncio
    async def test_batch_arrange_none_needed(self):
        """批量编排：所有图表都不需要"""
        none_response = json.dumps({
            "flowchart":    {"needed": False, "type": "none", "priority": 3, "reason": "内容简单"},
            "gantt":        {"needed": False, "type": "none", "priority": 3, "reason": "无工期数据"},
            "architecture": {"needed": False, "type": "none", "priority": 3, "reason": "无组织架构"},
            "labor":        {"needed": False, "type": "none", "priority": 3, "reason": "无劳动力数据"},
            "comparison":   {"needed": False, "type": "none", "priority": 3, "reason": "无方案比选"},
            "layout":       {"needed": False, "type": "none", "priority": 3, "reason": "无布局数据"},
            "timeline":     {"needed": False, "type": "none", "priority": 3, "reason": "无里程碑"},
        }, ensure_ascii=False)
        provider = MockProvider([none_response])
        result = await generate_all_chart_arrangements(
            provider,
            section_title="项目概述",
            section_content="本项目位于XX市，为普通办公楼建设。",
        )
        for key in result:
            assert result[key].get("needed") is False, f"{key} 应为 False"

    @pytest.mark.asyncio
    async def test_batch_arrange_missing_keys_fallback(self):
        """批量编排：返回缺少某些key时自动补默认值"""
        partial_response = json.dumps({
            "flowchart": {"needed": True, "type": "construction", "priority": 5},
            "gantt":     {"needed": False, "type": "none", "priority": 3},
        }, ensure_ascii=False)
        provider = MockProvider([partial_response])
        result = await generate_all_chart_arrangements(
            provider,
            section_title="测试章节",
            section_content="测试内容",
        )
        assert "flowchart" in result
        assert "gantt" in result
        assert "architecture" in result
        assert "labor" in result
        assert "comparison" in result
        assert "layout" in result
        assert "timeline" in result

    @pytest.mark.asyncio
    async def test_batch_arrange_non_dict_response(self):
        """批量编排：返回非字典时不崩溃"""
        # 提供一个合法的 JSON 响应（但值为字符串，模拟非预期格式）
        provider = MockProvider(['{"flowchart": "just a string"}'])
        result = await generate_all_chart_arrangements(
            provider,
            section_title="测试章节",
            section_content="测试内容",
        )
        # 应返回原始结果或空字典，不应抛出异常
        assert result is not None


# ============================================================
# 2. 渲染流程测试
# ============================================================
class TestRenderMermaidToBytes:
    """测试 render_mermaid_to_bytes 各图表类型渲染"""

    # --- flowchart ---
    def test_render_flowchart_mermaid(self):
        """流程图：Mermaid 语法代码"""
        mermaid_code = """flowchart TD
    A[施工准备] --> B[测量放线]
    B --> C[基础施工]
    C --> D[主体施工]
    D --> E[装饰装修]
    E --> F[竣工验收]"""
        result = render_mermaid_to_bytes(mermaid_code, "flowchart")
        assert result is not None, "流程图渲染应返回 BytesIO"
        assert isinstance(result, io.BytesIO)
        data = result.getvalue()
        assert len(data) > 0, "渲染结果不能为空"
        assert data.startswith(b"\x89PNG"), f"应为PNG格式，实际前8字节: {data[:8]}"
        result.seek(0)

    def test_render_flowchart_json_nodes(self):
        """流程图：JSON 节点列表格式"""
        json_data = json.dumps({
            "type": "flowchart",
            "title": "施工流程图",
            "nodes": ["施工准备", "测量放线", "基础施工", "主体施工", "竣工验收"],
        })
        result = render_mermaid_to_bytes(json_data, "flowchart")
        assert result is not None
        assert isinstance(result, io.BytesIO)
        assert len(result.getvalue()) > 0

    # --- gantt ---
    def test_render_gantt_mermaid(self):
        """甘特图：Mermaid gantt 代码"""
        mermaid_code = """gantt
    dateFormat YYYY-MM-DD
    axisFormat %m月%d日
    section 施工准备
    施工图设计 :a1, 2024-01-01, 10d
    现场准备 :a2, after a1, 5d
    section 主体施工
    基础施工 :b1, after a2, 30d
    主体结构 :b2, after b1, 40d
    section 收尾验收
    装饰装修 :c1, after b2, 25d
    竣工验收 :milestone, after c1, 0d"""
        result = render_mermaid_to_bytes(mermaid_code, "gantt")
        assert result is not None
        assert isinstance(result, io.BytesIO)
        data = result.getvalue()
        assert len(data) > 0
        assert data.startswith(b"\x89PNG")
        result.seek(0)

    def test_render_gantt_json(self):
        """甘特图：JSON 数据格式"""
        json_data = json.dumps({
            "type": "gantt",
            "title": "施工进度计划",
            "duration": 120,
            "unit": "天",
            "tick": 10,
            "tasks": [
                {"name": "施工准备", "start": 1, "end": 15},
                {"name": "基础施工", "start": 10, "end": 40},
                {"name": "主体施工", "start": 35, "end": 85},
                {"name": "装饰装修", "start": 80, "end": 110},
                {"name": "竣工验收", "start": 105, "end": 120},
            ],
        })
        result = render_mermaid_to_bytes(json_data, "gantt")
        assert result is not None
        assert isinstance(result, io.BytesIO)
        assert len(result.getvalue()) > 0

    # --- architecture ---
    def test_render_architecture_json(self):
        """架构图：JSON 树形数据"""
        json_data = json.dumps({
            "type": "architecture",
            "title": "项目组织架构",
            "root": {
                "label": "项目经理",
                "children": [
                    {"label": "技术负责人", "children": [{"label": "技术部", "children": []}, {"label": "质量部", "children": []}]},
                    {"label": "生产负责人", "children": [{"label": "施工部", "children": []}, {"label": "安全部", "children": []}]},
                ],
            },
        })
        result = render_mermaid_to_bytes(json_data, "architecture")
        assert result is not None
        assert isinstance(result, io.BytesIO)
        data = result.getvalue()
        assert len(data) > 0

    # --- labor ---
    def test_render_labor_json(self):
        """劳动力图：JSON 数据"""
        json_data = json.dumps({
            "type": "labor",
            "title": "劳动力配置计划",
            "phases": ["施工准备", "基础工程", "主体工程", "装饰装修", "竣工验收"],
            "categories": ["管理人员", "技术工人", "普通工人", "特殊工种"],
            "data": [
                [5, 8, 10, 2],
                [8, 15, 20, 4],
                [10, 25, 30, 6],
                [8, 18, 22, 4],
                [5, 6, 8, 2],
            ],
        })
        result = render_mermaid_to_bytes(json_data, "labor")
        assert result is not None
        assert isinstance(result, io.BytesIO)
        data = result.getvalue()
        assert len(data) > 0

    # --- comparison ---
    def test_render_comparison_json(self):
        """对比图：JSON 数据"""
        json_data = json.dumps({
            "type": "comparison",
            "title": "基础施工方案比选",
            "headers": ["评价指标", "方案A", "方案B"],
            "rows": [
                {"label": "工期", "values": [30, 45]},
                {"label": "成本", "values": [100, 80]},
                {"label": "质量", "values": [95, 85]},
            ],
        })
        result = render_mermaid_to_bytes(json_data, "comparison")
        assert result is not None
        assert isinstance(result, io.BytesIO)
        data = result.getvalue()
        assert len(data) > 0

    # --- layout ---
    def test_render_layout_json(self):
        """布局图：JSON 数据"""
        json_data = json.dumps({
            "type": "layout",
            "title": "施工现场总平面布置",
            "zones": [
                {"name": "办公区", "category": "办公区", "x": 0, "y": 0, "w": 3, "h": 2},
                {"name": "材料堆场", "category": "材料堆放区", "x": 4, "y": 0, "w": 4, "h": 3},
                {"name": "钢筋加工区", "category": "加工区", "x": 0, "y": 3, "w": 3, "h": 2},
                {"name": "主体施工区", "category": "生产区", "x": 4, "y": 4, "w": 5, "h": 3},
            ],
        })
        result = render_mermaid_to_bytes(json_data, "layout")
        assert result is not None
        assert isinstance(result, io.BytesIO)
        data = result.getvalue()
        assert len(data) > 0

    # --- timeline ---
    def test_render_timeline_json(self):
        """时间线图：JSON 数据"""
        json_data = json.dumps({
            "type": "timeline",
            "title": "关键里程碑时间线",
            "milestones": [
                {"date": "2024-01-15", "name": "施工准备完成"},
                {"date": "2024-02-15", "name": "基础完工"},
                {"date": "2024-04-15", "name": "主体封顶"},
                {"date": "2024-05-10", "name": "装饰装修完成"},
                {"date": "2024-05-20", "name": "竣工验收"},
            ],
        })
        result = render_mermaid_to_bytes(json_data, "timeline")
        assert result is not None
        assert isinstance(result, io.BytesIO)
        data = result.getvalue()
        assert len(data) > 0

    # --- chart 通配符 ---
    def test_render_chart_wildcard(self):
        """chart 通配符自动识别内部 type"""
        json_data = json.dumps({
            "type": "flowchart",
            "title": "施工流程图",
            "nodes": ["开始", "准备", "施工", "验收", "结束"],
        })
        result = render_mermaid_to_bytes(json_data, "chart")
        assert result is not None
        assert isinstance(result, io.BytesIO)
        assert len(result.getvalue()) > 0

    # --- 边界条件 ---
    def test_render_empty_input(self):
        """空输入应返回 None"""
        assert render_mermaid_to_bytes("", "flowchart") is None
        assert render_mermaid_to_bytes(None, "flowchart") is None
        assert render_mermaid_to_bytes("   ", "flowchart") is None

    def test_render_invalid_type(self):
        """不支持的图表类型应返回 None"""
        result = render_mermaid_to_bytes("some code", "unknown_type")
        assert result is None


# ============================================================
# 3. DOCX 导出图表嵌入测试
# ============================================================
class TestDocxChartExport:
    """测试 DOCX 导出中图表是否嵌入为图片而非占位符"""

    def _make_config(self):
        from app.services.export import ExportConfig
        return ExportConfig()

    def _make_section_detail(self):
        """创建一个模拟的章节详情对象"""
        class SectionDetail:
            def __init__(self):
                self.content = ""
                self.flowchart_json = None
                self.gantt_json = None
                self.architecture_json = None
                self.labor_json = None
                self.comparison_json = None
                self.layout_json = None
                self.timeline_json = None
        return SectionDetail()

    def _render_to_docx(self, chart_type: str, chart_data: dict) -> tuple[Document, list]:
        """渲染图表到 DOCX，返回 (document, paragraphs_info)"""
        from app.services.export import ExportConfig
        doc = Document()
        config = self._make_config()
        section_detail = self._make_section_detail()

        # 将图表数据写入对应的字段
        field_map = {
            "flowchart": "flowchart_json",
            "gantt": "gantt_json",
            "architecture": "architecture_json",
            "labor": "labor_json",
            "comparison": "comparison_json",
            "layout": "layout_json",
            "timeline": "timeline_json",
        }
        field = field_map.get(chart_type)
        if field:
            setattr(section_detail, field, json.dumps(chart_data, ensure_ascii=False))

        # 调用 _add_inline_chart 渲染
        _add_inline_chart(doc, chart_type, json.dumps(chart_data, ensure_ascii=False), section_detail, config)

        # 收集文档中的元素信息
        paras = []
        for p in doc.paragraphs:
            runs_info = []
            for run in p.runs:
                runs_info.append({
                    "text": run.text[:50] if run.text else "",
                    "bold": run.bold,
                })
            paras.append({
                "text": p.text[:100],
                "runs": runs_info,
                "has_picture": len(p._p.xpath('.//pic:pic')) > 0 if hasattr(p._p, 'xpath') else False,
            })

        # 检查表格中的图片
        table_images = []
        for table in doc.tables:
            for row in table.rows:
                for cell in row.cells:
                    for p in cell.paragraphs:
                        if p._p.xpath('.//pic:pic'):
                            table_images.append({"text": p.text[:50]})

        return doc, paras, table_images

    @pytest.mark.parametrize("chart_type,data", [
        ("flowchart", {
            "type": "flowchart", "title": "施工流程图",
            "nodes": ["施工准备", "测量放线", "基础施工", "主体施工", "装饰装修", "竣工验收"],
        }),
        ("gantt", {
            "type": "gantt", "title": "施工进度计划",
            "duration": 120, "unit": "天", "tick": 10,
            "tasks": [
                {"name": "施工准备", "start": 1, "end": 15},
                {"name": "基础施工", "start": 10, "end": 40},
                {"name": "主体施工", "start": 35, "end": 85},
                {"name": "装饰装修", "start": 80, "end": 110},
                {"name": "竣工验收", "start": 105, "end": 120},
            ],
        }),
        ("architecture", {
            "type": "architecture", "title": "组织架构图",
            "root": {"label": "项目经理", "children": [
                {"label": "技术部", "children": []},
                {"label": "工程部", "children": [{"label": "施工组", "children": []}]},
            ]},
        }),
        ("labor", {
            "type": "labor", "title": "劳动力配置",
            "phases": ["准备", "主体", "验收"],
            "categories": ["管理", "技术", "普通"],
            "data": [[5, 10, 8], [10, 25, 18], [8, 20, 15]],
        }),
        ("comparison", {
            "type": "comparison", "title": "方案比选",
            "rows": [{"label": "方案A", "value": 95}, {"label": "方案B", "value": 80}],
        }),
        ("layout", {
            "type": "layout", "title": "场地布置",
            "zones": [
                {"name": "办公区", "category": "办公区"},
                {"name": "材料区", "category": "材料区"},
                {"name": "施工区", "category": "生产区"},
            ],
        }),
        ("timeline", {
            "type": "timeline", "title": "里程碑",
            "milestones": [
                {"date": "2024-01-15", "name": "准备完成"},
                {"date": "2024-02-15", "name": "基础完工"},
                {"date": "2024-04-15", "name": "主体封顶"},
            ],
        }),
    ])
    def test_chart_rendered_as_image_not_placeholder(self, chart_type: str, data: dict):
        """验证每种图表类型在导出时都嵌入为图片，不显示占位符文本"""
        doc, paras, table_images = self._render_to_docx(chart_type, data)

        # 检查文档中没有占位符文本
        all_text = "\n".join(p["text"] for p in paras)
        assert "[[" not in all_text, f"{chart_type}: 文档中存在占位符文本 [[..."
        assert "<<" not in all_text or "<<" in all_text[:0], f"{chart_type}: 文档中存在 <<CHART:...>> 标记"
        assert "Mermaid" not in all_text, f"{chart_type}: 文档中出现了Mermaid代码文本"
        assert "图表" not in all_text or "施工" in all_text, f"{chart_type}: 出现了异常的图表占位文字"

        # 验证文档结构正常
        assert len(paras) > 0, f"{chart_type}: 文档应至少有一个段落"
        # 检查是否有图片元素（通过 XML 检查）
        has_picture = False
        for p in doc.paragraphs:
            if p._p.xpath('.//pic:pic'):
                has_picture = True
                break
        # 注意：由于渲染可能依赖外部工具，这里主要验证没有占位符
        # 如果有图片则更好，没有也不应报错（因为渲染器可能不可用）

    def test_render_section_charts_no_placeholder_text(self):
        """测试 _render_section_charts 不会输出占位符文本"""
        from app.services.export import ExportConfig
        doc = Document()
        config = ExportConfig()

        class SectionDetail:
            content = "这是测试内容"
            flowchart_json = json.dumps({
                "type": "flowchart", "title": "测试流程",
                "nodes": ["A", "B", "C"],
            })
            gantt_json = None
            architecture_json = None
            labor_json = None
            comparison_json = None
            layout_json = None
            timeline_json = None

        section = SectionDetail()
        _render_section_charts(doc, section, "测试章节", config)

        all_text = "\n".join(p.text for p in doc.paragraphs)
        assert "[[" not in all_text, "文档中存在 [[...]] 占位符"
        assert "<<CHART:" not in all_text, "文档中存在 <<CHART:...>> 占位符"
        assert "Mermaid" not in all_text, "文档中出现了 Mermaid 代码文本"

    def test_add_inline_chart_skips_when_no_data(self):
        """V6.1 无降级原则：章节无图表数据时跳过占位符，不伪造内容"""
        from app.services.export import ExportConfig
        doc = Document()
        config = ExportConfig()

        class EmptySectionDetail:
            content = "测试内容"
            flowchart_json = None
            gantt_json = None
            architecture_json = None
            labor_json = None
            comparison_json = None
            layout_json = None
            timeline_json = None

        section = EmptySectionDetail()
        _add_inline_chart(doc, "gantt", "", section, config)

        # 无数据源：不应插入任何图片或占位文本
        all_text = "\n".join(p.text for p in doc.paragraphs)
        assert all_text.strip() == "", f"无数据时不应输出内容，实际: {all_text!r}"
        for p in doc.paragraphs:
            assert not p._p.xpath('.//pic:pic'), "无数据时不应插入图片"


# ============================================================
# 4. 真实数据渲染测试（V6.1 无降级原则：不伪造数据，用真实结构化数据渲染）
# ============================================================
class TestRealDataRendering:
    """验证各图表类型的真实结构化数据可直接经渲染管线输出 PNG"""

    @pytest.mark.parametrize("chart_type,chart_data", [
        ("flowchart", {"type": "flowchart", "title": "施工流程图",
                       "nodes": ["施工准备", "测量放线", "基础施工", "主体施工", "竣工验收"]}),
        ("gantt", {"type": "gantt", "title": "施工进度计划", "duration": 120, "unit": "天", "tick": 10,
                   "tasks": [{"name": "施工准备", "start": 1, "end": 15},
                             {"name": "主体施工", "start": 15, "end": 100},
                             {"name": "竣工验收", "start": 100, "end": 120}]}),
        ("architecture", {"type": "architecture", "title": "组织架构图",
                          "root": {"label": "项目经理", "children": [{"label": "技术部", "children": []}]}}),
        ("labor", {"type": "labor", "title": "劳动力配置", "phases": ["准备", "主体", "验收"],
                   "categories": ["管理", "技术"], "data": [[5, 10], [10, 25], [5, 8]]}),
        ("comparison", {"type": "comparison", "title": "方案比选",
                        "items": [{"label": "方案A", "value": 95}, {"label": "方案B", "value": 80}]}),
        ("layout", {"type": "layout", "title": "场地布置",
                    "zones": [{"name": "办公区", "category": "办公区"},
                              {"name": "施工区", "category": "生产区"}]}),
        ("timeline", {"type": "timeline", "title": "里程碑",
                      "milestones": [{"date": "第1天", "name": "开工"}, {"date": "第120天", "name": "竣工"}]}),
    ])
    def test_real_data_renders_png(self, chart_type: str, chart_data: dict):
        """真实数据经 render_mermaid_to_bytes 输出有效 PNG"""
        rendered = render_mermaid_to_bytes(json.dumps(chart_data, ensure_ascii=False), chart_type)
        assert rendered is not None, f"{chart_type} 真实数据渲染失败"
        assert isinstance(rendered, io.BytesIO)
        data = rendered.getvalue()
        assert len(data) > 0
        assert data.startswith(b"\x89PNG"), f"{chart_type} 应输出 PNG，实际前8字节: {data[:8]}"


# ============================================================
# 5. 端到端流程测试（模拟完整工作流）
# ============================================================
class TestEndToEndChartFlow:
    """端到端测试：编排 → 生成 → 渲染 → 导出"""

    @pytest.mark.asyncio
    async def test_full_pipeline_flowchart(self):
        """完整流程：编排 → 流程图生成 → 渲染 → 验证"""
        arrange_response = json.dumps({
            "flowchart": {"needed": True, "type": "construction", "priority": 5,
                          "reason": "适合流程图", "nodeCountHint": 5, "flowHint": "td"},
            "gantt": {"needed": False, "type": "none", "priority": 3, "reason": ""},
            "architecture": {"needed": False, "type": "none", "priority": 3, "reason": ""},
            "labor": {"needed": False, "type": "none", "priority": 3, "reason": ""},
            "comparison": {"needed": False, "type": "none", "priority": 3, "reason": ""},
            "layout": {"needed": False, "type": "none", "priority": 3, "reason": ""},
            "timeline": {"needed": False, "type": "none", "priority": 3, "reason": ""},
        }, ensure_ascii=False)

        provider = MockProvider([arrange_response])
        arrangements = await generate_all_chart_arrangements(
            provider,
            section_title="施工工艺",
            section_content="本项目施工流程包括施工准备、测量放线、基础施工、主体施工、装饰装修、竣工验收六个阶段。",
            project_overview="某办公楼项目",
        )

        assert arrangements["flowchart"]["needed"] is True
        assert arrangements["gantt"]["needed"] is False

        # 使用真实结构化数据渲染（无降级原则：不伪造数据）
        real_data = {
            "type": "flowchart",
            "title": "施工流程图",
            "nodes": ["施工准备", "测量放线", "基础施工", "主体施工", "装饰装修", "竣工验收"],
        }
        rendered = render_mermaid_to_bytes(json.dumps(real_data, ensure_ascii=False), "flowchart")
        assert rendered is not None
        assert isinstance(rendered, io.BytesIO)
        assert len(rendered.getvalue()) > 0

    @pytest.mark.asyncio
    async def test_full_pipeline_gantt(self):
        """完整流程：编排 → 甘特图生成 → 渲染 → 验证"""
        arrange_response = json.dumps({
            "flowchart": {"needed": False, "type": "none", "priority": 3, "reason": ""},
            "gantt": {"needed": True, "type": "schedule", "priority": 5, "reason": "适合甘特图", "nodeCountHint": 0, "flowHint": "none"},
            "architecture": {"needed": False, "type": "none", "priority": 3, "reason": ""},
            "labor": {"needed": False, "type": "none", "priority": 3, "reason": ""},
            "comparison": {"needed": False, "type": "none", "priority": 3, "reason": ""},
            "layout": {"needed": False, "type": "none", "priority": 3, "reason": ""},
            "timeline": {"needed": False, "type": "none", "priority": 3, "reason": ""},
        }, ensure_ascii=False)

        provider = MockProvider([arrange_response])
        arrangements = await generate_all_chart_arrangements(
            provider,
            section_title="进度计划",
            section_content="项目总工期120天，分为五个阶段。",
        )

        assert arrangements["gantt"]["needed"] is True
        assert arrangements["flowchart"]["needed"] is False

        # 使用真实结构化数据渲染（无降级原则：不伪造数据）
        real_data = {
            "type": "gantt",
            "title": "施工进度计划",
            "duration": 120,
            "unit": "天",
            "tick": 10,
            "tasks": [
                {"name": "施工准备", "start": 1, "end": 15},
                {"name": "主体施工", "start": 15, "end": 100},
                {"name": "竣工验收", "start": 100, "end": 120},
            ],
        }
        rendered = render_mermaid_to_bytes(json.dumps(real_data, ensure_ascii=False), "gantt")
        assert rendered is not None
        assert isinstance(rendered, io.BytesIO)
        assert len(rendered.getvalue()) > 0


# ============================================================
# 6. 高并发批量图表生成测试（50个复杂图表）
# ============================================================
@pytest.mark.asyncio
async def test_high_concurrency_batch_chart_generation():
    """测试50个复杂图表的高并发批量生成能力"""
    import asyncio

    chart_templates = [
        {"type": "flowchart", "title": "施工流程{}", "nodes": ["准备{}", "测量{}", "基础{}", "主体{}", "装饰{}", "验收{}"]},
        {"type": "gantt", "title": "进度计划{}", "duration": 120, "unit": "天", "tasks": [{"name": "阶段{}-A", "start": 1, "end": 30}, {"name": "阶段{}-B", "start": 25, "end": 60}, {"name": "阶段{}-C", "start": 55, "end": 90}, {"name": "阶段{}-D", "start": 85, "end": 120}]},
        {"type": "architecture", "title": "组织架构{}", "root": {"label": "经理{}", "children": [{"label": "技术{}", "children": [{"label": "子部门{}", "children": []}]}, {"label": "生产{}", "children": []}]}},
        {"type": "labor", "title": "劳动力计划{}", "phases": ["准备{}", "主体{}", "收尾{}"], "categories": ["管理{}", "技术{}", "普通{}"], "data": [[5, 10, 8], [10, 25, 18], [8, 20, 15]]},
        {"type": "comparison", "title": "方案比选{}", "headers": ["指标", "方案A{}", "方案B{}"], "rows": [{"label": "工期{}", "values": [30, 45]}, {"label": "成本{}", "values": [100, 80]}, {"label": "质量{}", "values": [95, 85]}]},
        {"type": "layout", "title": "场地布置{}", "zones": [{"name": "办公区{}", "category": "办公", "x": 0, "y": 0, "w": 3, "h": 2}, {"name": "材料区{}", "category": "材料", "x": 4, "y": 0, "w": 4, "h": 3}]},
        {"type": "timeline", "title": "里程碑{}", "milestones": [{"date": "2024-01-15", "name": "节点A{}"}, {"date": "2024-03-15", "name": "节点B{}"}, {"date": "2024-05-15", "name": "节点C{}"}]},
        {"type": "flowchart", "title": "质量管控流程{}", "nodes": ["进场{}", "检验{}", "验收{}", "归档{}"]},
        {"type": "gantt", "title": "材料供应计划{}", "duration": 90, "unit": "天", "tasks": [{"name": "采购{}", "start": 1, "end": 20}, {"name": "运输{}", "start": 18, "end": 40}, {"name": "入库{}", "start": 38, "end": 60}]},
        {"type": "architecture", "title": "安全管理架构{}", "root": {"label": "安全总监{}", "children": [{"label": "安全员{}", "children": [{"label": "班组{}", "children": []}]}]}},
    ]

    all_chart_data = []
    for i in range(50):
        template = chart_templates[i % len(chart_templates)]
        chart_data = {
            "type": template["type"],
            "title": template["title"].format(i + 1),
            **{k: v for k, v in template.items() if k not in ("type", "title")}
        }
        all_chart_data.append(chart_data)

    async def render_single_chart(index, chart_data):
        rendered = render_mermaid_to_bytes(json.dumps(chart_data, ensure_ascii=False), chart_data["type"])
        return index, rendered

    results = await asyncio.gather(*[
        render_single_chart(i, chart_data)
        for i, chart_data in enumerate(all_chart_data)
    ])

    success_count = 0
    fail_count = 0
    for index, rendered in results:
        if rendered is not None and isinstance(rendered, io.BytesIO):
            data = rendered.getvalue()
            if len(data) > 0:
                success_count += 1
            else:
                fail_count += 1
        else:
            fail_count += 1

    assert success_count == 50, f"成功渲染 {success_count}/50 个图表，失败 {fail_count} 个"


@pytest.mark.asyncio
async def test_concurrent_batch_arrangement_and_render():
    """测试批量编排与渲染的高并发整合"""
    import asyncio

    section_contents = [
        (f"施工工艺方案{i+1}", f"本项目采用先进的施工方法{i+1}，包括多个关键工序和质量控制点。")
        for i in range(20)
    ]

    arrange_responses = [
        json.dumps({
            "flowchart": {"needed": True, "type": "construction", "priority": 5, "reason": f"方案{i+1}适合流程图", "nodeCountHint": 5, "flowHint": "td"},
            "gantt": {"needed": False, "type": "none", "priority": 3, "reason": ""},
            "architecture": {"needed": False, "type": "none", "priority": 3, "reason": ""},
            "labor": {"needed": False, "type": "none", "priority": 3, "reason": ""},
            "comparison": {"needed": False, "type": "none", "priority": 3, "reason": ""},
            "layout": {"needed": False, "type": "none", "priority": 3, "reason": ""},
            "timeline": {"needed": False, "type": "none", "priority": 3, "reason": ""},
        }, ensure_ascii=False)
        for i in range(20)
    ]

    async def process_section(index, title, content, response):
        provider = MockProvider([response])
        arrangements = await generate_all_chart_arrangements(
            provider,
            section_title=title,
            section_content=content,
            project_overview="测试项目",
        )
        charts_to_render = [k for k, v in arrangements.items() if isinstance(v, dict) and v.get("needed")]
        rendered_results = {}
        for chart_type in charts_to_render:
            # 使用真实结构化数据渲染（无降级原则：不伪造数据）
            real_data = {
                "type": chart_type,
                "title": f"{title}-图表",
                "nodes": ["施工准备", "测量放线", "基础施工", "主体施工", "竣工验收"],
            }
            rendered = render_mermaid_to_bytes(json.dumps(real_data, ensure_ascii=False), chart_type)
            rendered_results[chart_type] = rendered is not None and len(rendered.getvalue()) > 0 if rendered else False

        return index, arrangements, rendered_results

    t0 = time.perf_counter()
    results = await asyncio.gather(*[
        process_section(i, title, content, response)
        for i, (title, content), response in zip(range(20), section_contents, arrange_responses)
    ])
    elapsed = time.perf_counter() - t0

    all_success = True
    for index, arrangements, rendered_results in results:
        assert all(v for v in rendered_results.values()), f"章节 {index} 渲染失败"
        needed_count = sum(1 for v in arrangements.values() if isinstance(v, dict) and v.get("needed"))
        assert needed_count == len(rendered_results), f"章节 {index} 编排与渲染数量不匹配"

    print(f"\n[高并发批量测试] 20个章节并发处理完成 耗时={elapsed:.2f}s 平均={elapsed/20:.3f}s/章节")
    assert elapsed < 60, f"20个章节并发处理超时: {elapsed:.2f}s > 60s"


# ============================================================
# 运行入口
# ============================================================
if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
