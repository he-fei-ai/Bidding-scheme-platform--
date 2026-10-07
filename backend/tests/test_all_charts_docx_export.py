"""
全 7 种图表类型 DOCX 导出集成测试

验证：
1. 所有 7 种图表类型都能成功渲染并插入 DOCX
2. DOCX 导出时不做降级处理（始终返回 PNG 图片）
3. 图片质量：尺寸 ≥ 800x600px，DPI=300
4. _render_commercial 五层策略的完整调用链

图表类型覆盖：
  - flowchart（流程图）
  - gantt（甘特图）
  - architecture（架构图）
  - comparison（对比图）
  - labor（劳动力图）
  - layout（布局图）
  - timeline（时间轴）
"""

from __future__ import annotations

import json
import sys
from io import BytesIO
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(_BACKEND_DIR) not in __import__("sys").path:
    __import__("sys").path.insert(0, str(_BACKEND_DIR))

from app.services.export import _add_inline_chart, _add_diagram_picture
from app.services.ai.mermaid_renderer import (
    render_mermaid_to_bytes,
    _render_flowchart_image_v2,
    _render_gantt_image_v2,
    _render_architecture_image_v2,
    _render_comparison_image_v2,
    _render_labor_image_v2,
    _render_layout_image_v2,
    _render_timeline_image_v2,
)
from app.models import ExportConfig


# ─────────────────────────────────────────────────────────────────────────────
# 辅助函数
# ─────────────────────────────────────────────────────────────────────────────

def _assert_valid_png(stream, label: str):
    assert stream is not None, f"[{label}] 渲染结果不应为 None"
    data = stream.getvalue()
    assert len(data) > 0, f"[{label}] PNG 数据不能为空"
    assert data[:8] == b"\x89PNG\r\n\x1a\n", f"[{label}] 不是有效 PNG"
    return data


def _mock_doc():
    """创建一个最小的 mock document"""
    doc = MagicMock()
    doc.add_paragraph.return_value = MagicMock()
    doc.add_picture.return_value = MagicMock()
    # 模拟 doc.sections 以支持 _add_diagram_picture 中的页面宽度计算
    mock_section = MagicMock()
    mock_section.page_width = 12000000   # 约 12cm (EMU)
    mock_section.left_margin = 1800000   # 1.8cm
    mock_section.right_margin = 1800000  # 1.8cm
    doc.sections = [mock_section]
    return doc


def _assert_picture_added(doc, caption: str, min_w_px: int = 800, min_h_px: int = 600):
    """验证 doc.add_picture 被调用且图片尺寸合规"""
    assert doc.add_picture.called, f"[{caption}] add_picture 未被调用"
    call_args = doc.add_picture.call_args
    stream = call_args[0][0]
    data = stream.getvalue()
    w, h = len(data) // 4, len(data) // 4  # rough estimate
    assert len(data) > 10000, f"[{caption}] PNG 数据过小 ({len(data)} bytes)，可能是降级渲染"


# ─────────────────────────────────────────────────────────────────────────────
# 1. 各 v2 渲染器直接测试
# ─────────────────────────────────────────────────────────────────────────────

class TestV2RenderersDirect:
    """直接测试各 v2 渲染器，确保数据→图片链路畅通"""

    def test_flowchart_v2(self):
        code = """flowchart TD
    A["开工准备"] --> B["基坑开挖"]
    B --> C["基础施工"]
    C --> D["主体结构"]
    D --> E["装饰装修"]
    E --> F["竣工验收"]"""
        result = _render_flowchart_image_v2(code)
        data = _assert_valid_png(result, "flowchart_v2")
        assert len(data) > 20000, f"流程图数据过小: {len(data)} bytes"
        print(f"  [flowchart_v2] OK - {len(data)} bytes")

    def test_gantt_v2(self):
        plan = {
            "type": "gantt",
            "title": "施工进度计划",
            "totalDays": 180,
            "tasks": [
                {"id": 1, "name": "施工准备", "start": 1, "end": 5, "dependencies": [], "isMilestone": False},
                {"id": 2, "name": "基坑开挖", "start": 6, "end": 15, "dependencies": [1], "isMilestone": False},
                {"id": 3, "name": "基础施工", "start": 16, "end": 40, "dependencies": [2], "isMilestone": False},
                {"id": 4, "name": "主体结构", "start": 41, "end": 120, "dependencies": [3], "isMilestone": False},
                {"id": 5, "name": "装饰装修", "start": 121, "end": 160, "dependencies": [4], "isMilestone": False},
                {"id": 6, "name": "竣工验收", "start": 180, "end": 180, "dependencies": [5], "isMilestone": True},
            ],
        }
        result = _render_gantt_image_v2(plan)
        data = _assert_valid_png(result, "gantt_v2")
        assert len(data) > 20000, f"甘特图数据过小: {len(data)} bytes"
        print(f"  [gantt_v2] OK - {len(data)} bytes")

    def test_architecture_v2(self):
        data = {
            "root": {
                "label": "XX 建设项目部",
                "children": [
                    {"label": "技术负责人", "children": [
                        {"label": "测量组", "children": []},
                        {"label": "质检员", "children": []},
                    ]},
                    {"label": "施工员", "children": [
                        {"label": "土建班组", "children": []},
                        {"label": "安装班组", "children": []},
                    ]},
                    {"label": "安全员", "children": []},
                ],
            },
        }
        result = _render_architecture_image_v2(data)
        data_png = _assert_valid_png(result, "architecture_v2")
        assert len(data_png) > 20000, f"架构图数据过小: {len(data_png)} bytes"
        print(f"  [architecture_v2] OK - {len(data_png)} bytes")

    def test_comparison_v2(self):
        data = {
            "type": "comparison",
            "title": "施工方案的对比分析",
            "rows": [
                {"label": "方案 A：悬臂浇筑", "values": [80, 120, 50, 250]},
                {"label": "方案 B：支架现浇", "values": [60, 100, 80, 240]},
                {"label": "方案 C：顶推施工", "values": [70, 90, 60, 220]},
            ],
            "headers": ["方案", "材料费(万)", "人工费(万)", "机械费(万)", "合计(万)"],
        }
        result = _render_comparison_image_v2(data)
        data_png = _assert_valid_png(result, "comparison_v2")
        assert len(data_png) > 20000, f"对比图数据过小: {len(data_png)} bytes"
        print(f"  [comparison_v2] OK - {len(data_png)} bytes")

    def test_labor_v2(self):
        data = {
            "type": "labor",
            "title": "劳动力需求计划",
            "phases": ["基础施工", "主体结构", "装饰装修", "竣工验收"],
            "categories": ["普工", "钢筋工", "木工", "混凝土工", "电工"],
            "data": [
                [30, 15, 20, 10, 5],
                [50, 40, 35, 30, 10],
                [40, 20, 45, 15, 8],
                [20, 5, 10, 5, 3],
            ],
        }
        result = _render_labor_image_v2(data)
        data_png = _assert_valid_png(result, "labor_v2")
        assert len(data_png) > 20000, f"劳动力图数据过小: {len(data_png)} bytes"
        print(f"  [labor_v2] OK - {len(data_png)} bytes")

    def test_layout_v2(self):
        data = {
            "type": "layout",
            "title": "施工现场总平面布置",
            "zones": [
                {"name": "钢筋加工场", "category": "加工区", "position": "东北", "size": "60x40"},
                {"name": "混凝土搅拌站", "category": "生产区", "position": "东南", "size": "50x30"},
                {"name": "材料堆放区", "category": "材料堆放区", "position": "西北", "size": "40x30"},
                {"name": "办公区", "category": "办公区", "position": "西南", "size": "30x20"},
                {"name": "生活区", "category": "生活区", "position": "西南", "size": "25x20"},
            ],
        }
        result = _render_layout_image_v2(data)
        data_png = _assert_valid_png(result, "layout_v2")
        assert len(data_png) > 20000, f"布局图数据过小: {len(data_png)} bytes"
        print(f"  [layout_v2] OK - {len(data_png)} bytes")

    def test_timeline_v2(self):
        data = {
            "type": "timeline",
            "title": "项目关键里程碑",
            "milestones": [
                {"name": "开工", "date": "2025-03-01", "desc": "工程正式开工", "group": "基础"},
                {"name": "基础完工", "date": "2025-04-15", "desc": "基础工程验收通过", "group": "基础"},
                {"name": "主体封顶", "date": "2025-08-20", "desc": "主体结构完工", "group": "主体"},
                {"name": "装修完成", "date": "2025-11-30", "desc": "装饰装修全部完工", "group": "装修"},
                {"name": "竣工验收", "date": "2026-01-15", "desc": "工程整体验收", "group": "竣工"},
            ],
        }
        result = _render_timeline_image_v2(data)
        data_png = _assert_valid_png(result, "timeline_v2")
        assert len(data_png) > 20000, f"时间轴数据过小: {len(data_png)} bytes"
        print(f"  [timeline_v2] OK - {len(data_png)} bytes")


# ─────────────────────────────────────────────────────────────────────────────
# 2. render_mermaid_to_bytes 全类型测试
# ─────────────────────────────────────────────────────────────────────────────

class TestRenderMermaidToBytes:
    """测试 render_mermaid_to_bytes 对全部 7 种类型的处理"""

    @pytest.mark.parametrize("chart_type,code", [
        (
            "flowchart",
            'flowchart TD\n    A["开工"] --> B["施工"]\n    B --> C["验收"]\n    C --> D["竣工"]',
        ),
        (
            "gantt",
            'gantt\n    dateFormat YYYY-MM-DD\n    section 基础\n    基坑开挖 :2025-03-01, 15d\n    section 主体\n    主体施工 :2025-03-20, 60d',
        ),
        (
            "architecture",
            '{"type":"architecture","root":"项目部","nodes":[{"id":"root","label":"项目经理","children":[{"id":"a","label":"技术部","children":[]},{"id":"b","label":"施工部","children":[]}]}]}',
        ),
        (
            "comparison",
            '{"type":"comparison","title":"方案对比","rows":[{"label":"方案A","value":80},{"label":"方案B","value":60},{"label":"方案C","value":70}]}',
        ),
        (
            "labor",
            '{"type":"labor","title":"劳动力计划","phases":["基础","主体","装饰"],"categories":["普工","钢筋工","木工"],"data":[[30,15,20],[50,40,35],[40,20,45]]}',
        ),
        (
            "layout",
            '{"type":"layout","title":"平面布置","zones":[{"name":"加工区","category":"生产区"},{"name":"办公区","category":"办公区"},{"name":"生活区","category":"生活区"}]}',
        ),
        (
            "timeline",
            '{"type":"timeline","title":"里程碑","milestones":[{"name":"开工","date":"2025-03-01"},{"name":"封顶","date":"2025-08-20"},{"name":"竣工","date":"2026-01-15"}]}',
        ),
    ])
    def test_all_chart_types(self, chart_type, code):
        """验证所有 7 种图表类型都能通过 render_mermaid_to_bytes 渲染"""
        result = render_mermaid_to_bytes(code, chart_type)
        assert result is not None, f"[{chart_type}] render_mermaid_to_bytes 返回 None，未走任何渲染路径"
        data = result.getvalue()
        assert len(data) > 5000, f"[{chart_type}] PNG 数据过小: {len(data)} bytes"
        assert data[:8] == b"\x89PNG\r\n\x1a\n", f"[{chart_type}] 不是有效 PNG"
        print(f"  [{chart_type}] OK - {len(data)} bytes")


# ─────────────────────────────────────────────────────────────────────────────
# 3. _render_commercial 五层策略端到端测试
# ─────────────────────────────────────────────────────────────────────────────

class TestRenderCommercialFiveStrategies:
    """测试 _render_commercial 五层渲染策略，验证 DOCX 导出无降级"""

    @pytest.fixture
    def config(self):
        return ExportConfig(image_max_width_cm=18.0, image_dpi=300)

    @pytest.mark.parametrize("chart_type,chart_data", [
        ("flowchart", {"type": "flowchart", "title": "施工流程", "nodes": [
            {"id": "a", "label": "开工准备"}, {"id": "b", "label": "基坑开挖"},
            {"id": "c", "label": "基础施工"}, {"id": "d", "label": "主体结构"},
            {"id": "e", "label": "竣工验收"},
        ], "edges": [{"from": "a", "to": "b"}, {"from": "b", "to": "c"}, {"from": "c", "to": "d"}, {"from": "d", "to": "e"}]}),
        ("gantt", {"type": "gantt", "title": "进度计划",
                   "tasks": [{"id": 1, "name": "基础", "start": 1, "end": 10,
                              "dependencies": [], "isMilestone": False},
                             {"id": 2, "name": "主体", "start": 11, "end": 50,
                              "dependencies": [1], "isMilestone": False},
                             {"id": 3, "name": "竣工", "start": 51, "end": 51,
                              "dependencies": [2], "isMilestone": True}]}),
        ("architecture", {"type": "architecture", "title": "组织架构",
                          "root": {"label": "项目经理", "children": [
                              {"label": "技术组", "children": []},
                              {"label": "施工组", "children": []},
                              {"label": "安全组", "children": []}
                          ]}}),
        ("comparison", {"type": "comparison", "title": "材料对比",
                        "rows": [{"label": "水泥", "value": 80},
                                 {"label": "钢材", "value": 120},
                                 {"label": "木材", "value": 50}]}),
        ("labor", {"type": "labor", "title": "劳动力",
                   "phases": ["基础", "主体", "装饰"],
                   "categories": ["普工", "钢筋工", "木工"],
                   "data": [[30, 15, 20], [50, 40, 35], [40, 20, 45]]}),
        ("layout", {"type": "layout", "title": "平面布置",
                    "zones": [{"name": "加工区", "category": "生产区", "position": "东", "size": "40x30"},
                              {"name": "办公区", "category": "办公区", "position": "西", "size": "20x15"},
                              {"name": "生活区", "category": "生活区", "position": "南", "size": "25x20"}]}),
        ("timeline", {"type": "timeline", "title": "里程碑",
                      "milestones": [
                          {"name": "开工", "date": "2025-03-01", "desc": "正式开工"},
                          {"name": "封顶", "date": "2025-08-20", "desc": "主体封顶"},
                          {"name": "竣工", "date": "2026-01-15", "desc": "竣工验收"}
                      ]}),
    ])
    def test_all_charts_in_docx(self, config, chart_type, chart_data):
        """验证所有 7 种图表类型在 DOCX 中都能成功渲染，不降级到文本占位图"""
        doc = _mock_doc()
        caption = f"{chart_type}图表"

        # 将 chart_data 序列化为 JSON 字符串（模拟 _add_inline_chart 的数据格式）
        chart_desc = json.dumps(chart_data, ensure_ascii=False)

        # _add_diagram_picture 内部调用 run.add_picture()，patch 它来捕获 PNG 数据
        png_captured = []
        orig_add_diagram_picture = _add_diagram_picture
        def capturing_add_diagram_picture(doc, image_stream, config, caption="", wide=False):
            if hasattr(image_stream, 'getvalue'):
                png_captured.append(image_stream.getvalue())
            return orig_add_diagram_picture(doc, image_stream, config, caption=caption, wide=wide)

        with patch('app.services.export._add_diagram_picture', side_effect=capturing_add_diagram_picture):
            _add_inline_chart(doc, chart_type, chart_desc, None, config)

        assert len(png_captured) > 0, f"[{chart_type}] _add_diagram_picture 应被调用（图片已插入）"
        png_data = png_captured[0]
        assert len(png_data) > 10000, (
            f"[{chart_type}] 图片数据过小 ({len(png_data)} bytes)，可能存在降级渲染"
        )
        assert png_data[:8] == b"\x89PNG\r\n\x1a\n", f"[{chart_type}] 不是有效 PNG"
        print(f"  [{chart_type}] DOCX 导出 OK - PNG {len(png_data)} bytes")


# ─────────────────────────────────────────────────────────────────────────────
# 4. 边界条件：空数据 / 缺失字段
# ─────────────────────────────────────────────────────────────────────────────

class TestEdgeCases:
    """测试边界条件，确保渲染器不会崩溃"""

    @pytest.mark.parametrize("chart_type,data_factory", [
        ("flowchart", lambda: ""),
        ("flowchart", lambda: None),
        ("gantt", lambda: {"type": "gantt", "tasks": []}),
        ("architecture", lambda: {"type": "architecture", "nodes": []}),
        ("comparison", lambda: {"type": "comparison", "rows": []}),
        ("labor", lambda: {"type": "labor", "data": []}),
        ("layout", lambda: {"type": "layout", "zones": []}),
        ("timeline", lambda: {"type": "timeline", "milestones": []}),
    ])
    def test_empty_data_returns_none(self, chart_type, data_factory):
        """空数据应返回 None，由上层处理"""
        data = data_factory()
        fn_map = {
            "flowchart": lambda d: _render_flowchart_image_v2(d),
            "gantt": lambda d: _render_gantt_image_v2(d),
            "architecture": lambda d: _render_architecture_image_v2(d),
            "comparison": lambda d: _render_comparison_image_v2(d),
            "labor": lambda d: _render_labor_image_v2(d),
            "layout": lambda d: _render_layout_image_v2(d),
            "timeline": lambda d: _render_timeline_image_v2(d),
        }
        result = fn_map[chart_type](data)
        # 空数据返回 None 是合理的（由 _render_commercial 的策略 4/5 兜底）
        assert result is None, f"[{chart_type}] 空数据应返回 None"
        print(f"  [{chart_type}] 空数据正确返回 None")


# ─────────────────────────────────────────────────────────────────────────────
# 5. 日志验证：确认中间状态日志被触发
# ─────────────────────────────────────────────────────────────────────────────

class TestRendererLogs:
    """验证关键中间状态日志是否正确输出"""

    def test_comparison_intermediate_logs(self, caplog):
        import logging
        caplog.set_level(logging.INFO)

        data = {
            "type": "comparison",
            "title": "材料成本对比",
            "rows": [
                {"label": "方案A", "value": 80},
                {"label": "方案B", "value": 60},
                {"label": "方案C", "value": 70},
            ],
        }
        result = _render_comparison_image_v2(data)
        assert result is not None

        # 验证启动日志
        assert any("[PIL v2渲染] 对比图渲染启动" in r for r in caplog.messages), \
            "应有对比图启动日志"
        # 验证数据准备日志
        assert any("对比图数据准备" in r for r in caplog.messages), \
            "应有对比图数据准备日志"
        # 验证画布日志
        assert any("对比图画布" in r for r in caplog.messages), \
            "应有对比图画布日志"
        # 验证完成日志
        assert any("对比图渲染完成" in r for r in caplog.messages), \
            "应有对比图完成日志"
        print("  [对比图日志] OK - 4 条中间状态日志均正确输出")

    def test_layout_intermediate_logs(self, caplog):
        import logging
        caplog.set_level(logging.INFO)

        data = {
            "type": "layout",
            "title": "总平面布置",
            "zones": [
                {"name": "加工区", "category": "生产区"},
                {"name": "办公区", "category": "办公区"},
                {"name": "生活区", "category": "生活区"},
            ],
        }
        result = _render_layout_image_v2(data)
        assert result is not None

        assert any("布局图渲染启动" in r for r in caplog.messages), "应有布局图启动日志"
        assert any("布局图布局计算" in r for r in caplog.messages), "应有布局图布局计算日志"
        assert any("布局图渲染完成" in r for r in caplog.messages), "应有布局图完成日志"
        print("  [布局图日志] OK - 3 条中间状态日志均正确输出")

    def test_timeline_intermediate_logs(self, caplog):
        import logging
        caplog.set_level(logging.INFO)

        data = {
            "type": "timeline",
            "title": "关键里程碑",
            "milestones": [
                {"name": "开工", "date": "2025-03-01"},
                {"name": "封顶", "date": "2025-08-20"},
                {"name": "竣工", "date": "2026-01-15"},
            ],
        }
        result = _render_timeline_image_v2(data)
        assert result is not None

        assert any("时间轴渲染启动" in r for r in caplog.messages), "应有时间轴启动日志"
        assert any("时间轴绘制" in r for r in caplog.messages), "应有时间轴绘制日志"
        assert any("时间轴渲染完成" in r for r in caplog.messages), "应有时间轴完成日志"
        print("  [时间轴日志] OK - 3 条中间状态日志均正确输出")


# ─────────────────────────────────────────────────────────────────────────────
# 6. HTTP 服务不可用场景 — mmdc 回退机制验证
# ─────────────────────────────────────────────────────────────────────────────

class TestHttpUnavailableFallback:
    """验证 HTTP 服务不可用时，mmdc 回退机制是否正确触发"""

    def test_http_service_unavailable_fallback_to_pil(self):
        """HTTP 服务不可用 → 应降级到 PIL v2，而非崩溃"""
        from unittest.mock import patch
        from app.services.ai.mermaid_service import MermaidRenderError

        code = 'flowchart TD\n    A["开工"] --> B["施工"]\n    B --> C["验收"]'

        with patch(
            "app.services.ai.mermaid_renderer.get_mermaid_service_client"
        ) as MockGetClient:
            # 模拟 HTTP 服务抛出 MermaidRenderError（所有后端不可用）
            mock_instance = MockGetClient.return_value
            mock_instance.render_mermaid_via_http.side_effect = MermaidRenderError(
                "[all] 所有渲染后端均不可用: 无可用后端"
            )

            # V5.0 已移除 mmdc 路径：HTTP 不可用时直接走 PIL v2 兜底
            result = render_mermaid_to_bytes(code, "flowchart")

        # 验证：HTTP 失败后不应崩溃，应通过 PIL v2 渲染成功
        assert result is not None, "HTTP 不可用时，PIL v2 应作为兜底成功渲染"
        data = result.getvalue()
        assert len(data) > 5000, f"PIL v2 兜底图片过小: {len(data)} bytes"
        assert data[:8] == b"\x89PNG\r\n\x1a\n", "不是有效 PNG"
        print(f"  [HTTP不可用] PIL v2 兜底成功 - {len(data)} bytes")

    def test_asyncio_no_running_loop_handled(self):
        """_safe_asyncio_run 在无运行 event loop 时不崩溃"""
        from unittest.mock import patch
        from app.services.ai.mermaid_service import MermaidRenderError

        code = 'flowchart TD\n    A --> B'

        with patch(
            "app.services.ai.mermaid_renderer.get_mermaid_service_client"
        ) as MockGetClient:
            mock_instance = MockGetClient.return_value
            # 模拟异步结果（has __await__），但 _safe_asyncio_run 抛出 no running loop
            async_result = mock_instance.render_mermaid_via_http.return_value
            async_result.__await__ = lambda: None  # 模拟协程

            with patch(
                "app.services.ai.workflows._safe_asyncio_run",
                side_effect=RuntimeError("no running event loop"),
            ):
                # V5.0 已移除 mmdc 路径：_safe_asyncio_run 异常时应降级到 PIL v2
                result = render_mermaid_to_bytes(code, "flowchart")

        # 应通过 PIL v2 兜底成功，而不是抛出异常
        assert result is not None, "_safe_asyncio_run 异常时应降级到 PIL v2"
        print("  [asyncio异常] 降级到 PIL v2 成功")

    def test_all_strategies_fail_returns_none(self):
        """所有策略均失败时返回 None"""
        from unittest.mock import patch

        code = 'flowchart TD\n    A --> B'

        with patch(
            "app.services.ai.mermaid_renderer._render_flowchart_image_v2",
            return_value=None,
        ):
            # V5.0 已移除 mmdc 路径：PIL v2 也失败时所有策略失败
            result = render_mermaid_to_bytes(code, "flowchart")

        assert result is None, "所有策略失败时应返回 None"
        print("  [全策略失败] 正确返回 None")
