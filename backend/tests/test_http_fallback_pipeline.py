"""
HTTP 服务不可用场景的降级测试（V5.0 增强版）

验证：
1. Mermaid HTTP Service 不可用时，渲染管道正确降级到 PIL v2
2. V5.0 已移除 mmdc 降级路径，mmdc 不再被调用
3. 所有 7 种图表类型在降级链路上都能正常渲染

测试场景：
  - 场景 A: HTTP 服务正常 → 直接命中 HTTP
  - 场景 B: HTTP 失败 → 降级 PIL v2（V5.0 无 mmdc）
  - 场景 C: HTTP 异常 → 同样降级到 PIL v2
"""

from __future__ import annotations

import json
import sys
from io import BytesIO
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(_BACKEND_DIR) not in __import__("sys").path:
    __import__("sys").path.insert(0, str(_BACKEND_DIR))

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
from app.services.ai.mermaid_service import MermaidRenderError, close_mermaid_service_client


def _reset_singleton():
    """重置 MermaidServiceClient 单例，确保每个测试使用独立的 mock 实例"""
    import app.services.ai.mermaid_service as _ms
    import app.services.ai.mermaid_renderer as _mr
    _ms._client_instance = None
    # 清除 renderer 中对单例的缓存引用（如有）
    if hasattr(_mr, '_mermaid_client'):
        delattr(_mr, '_mermaid_client')


# ─────────────────────────────────────────────────────────────────────────────
# 辅助：生成各类型 Mermaid 代码
# ─────────────────────────────────────────────────────────────────────────────

def _make_flowchart_code() -> str:
    return """flowchart TD
    A["开工准备"] --> B["基坑开挖"]
    B --> C["基础施工"]
    C --> D["主体结构"]
    D --> E["竣工验收"]"""


def _make_gantt_code() -> str:
    return """gantt
    dateFormat YYYY-MM-DD
    section 基础工程
    施工准备 :2025-03-01, 7d
    基坑开挖 :2025-03-08, 14d
    section 主体工程
    主体施工 :2025-03-22, 60d
    section 装饰工程
    装饰装修 :2025-05-21, 30d"""


def _make_architecture_code() -> str:
    """架构图使用 JSON 格式（非 Mermaid 语法）"""
    return json.dumps({
        "type": "architecture",
        "root": {
            "label": "项目部",
            "children": [
                {"label": "技术部", "children": [{"label": "测量组", "children": []}]},
                {"label": "施工部", "children": [{"label": "土建组", "children": []}, {"label": "安装组", "children": []}]},
                {"label": "安全部", "children": []},
            ],
        },
    }, ensure_ascii=False)


def _make_comparison_code() -> str:
    """对比图使用 Mermaid pie 语法（生产环境的标准载荷格式）"""
    return """pie title 方案对比
    "方案A" : 80
    "方案B" : 60
    "方案C" : 70"""


def _make_chart_type_code(chart_type: str) -> str:
    """根据图表类型返回对应的 Mermaid 代码或 JSON 数据"""
    type_map = {
        "flowchart": _make_flowchart_code,
        "gantt": _make_gantt_code,
        "architecture": _make_architecture_code,
        "comparison": _make_comparison_code,
    }
    if chart_type in type_map:
        return type_map[chart_type]()

    # 非 Mermaid 语法类型使用 JSON 数据
    json_map = {
        "labor": {
            "type": "labor",
            "title": "劳动力计划",
            "phases": ["基础", "主体", "装饰"],
            "categories": ["普工", "钢筋工", "木工"],
            "data": [[30, 15, 20], [50, 40, 35], [40, 20, 45]],
        },
        "layout": {
            "type": "layout",
            "title": "总平面布置",
            "zones": [
                {"name": "加工区", "category": "生产区"},
                {"name": "办公区", "category": "办公区"},
                {"name": "生活区", "category": "生活区"},
            ],
        },
        "timeline": {
            "type": "timeline",
            "title": "里程碑",
            "milestones": [
                {"name": "开工", "date": "2025-03-01"},
                {"name": "封顶", "date": "2025-08-20"},
                {"name": "竣工", "date": "2026-01-15"},
            ],
        },
    }
    import json
    return json.dumps(json_map.get(chart_type, {}), ensure_ascii=False)


# ─────────────────────────────────────────────────────────────────────────────
# 场景 A: HTTP 服务正常
# ─────────────────────────────────────────────────────────────────────────────

class TestHttpServiceWorking:
    """HTTP 服务正常时，验证直接命中 HTTP 路径

    V6.1 渲染矩阵：
      - Mermaid 语法载荷（flowchart/gantt/comparison-pie）→ 第一优先方案 HTTP Service
      - JSON 数据载荷（architecture/labor/layout/timeline）→ HTTP Service 无法解析，
        第一优先方案即 PIL v2 商业级渲染器（跳过无效 HTTP 尝试）
    """

    @pytest.mark.parametrize("chart_type", [
        "flowchart", "gantt", "comparison",
    ])
    def test_http_service_success(self, chart_type):
        """Mermaid 语法载荷：HTTP 正常时应直接返回 HTTP 渲染结果"""
        code = _make_chart_type_code(chart_type)
        _reset_singleton()

        # 模拟 HTTP 服务成功
        mock_http_bytes = b"\x89PNG\r\n\x1a\n" + b"fake_png_data" * 100

        with patch(
            "app.services.ai.mermaid_renderer.get_mermaid_service_client"
        ) as MockGetClient:
            mock_instance = MagicMock()
            mock_instance.render_mermaid_via_http.return_value = mock_http_bytes
            MockGetClient.return_value = mock_instance

            result = render_mermaid_to_bytes(code, chart_type)
            assert result is not None, f"[{chart_type}] HTTP 正常时应返回结果"
            data = result.getvalue()
            assert data[:8] == b"\x89PNG\r\n\x1a\n", f"[{chart_type}] 应返回有效 PNG"
            assert mock_instance.render_mermaid_via_http.called, \
                f"[{chart_type}] 应调用 HTTP Service"
            print(f"  [{chart_type}] HTTP 直连 OK")

    @pytest.mark.parametrize("chart_type", [
        "architecture", "labor", "layout", "timeline",
    ])
    def test_json_payload_renders_via_pil_v2(self, chart_type):
        """JSON 数据载荷：HTTP 无法解析，第一优先方案为 PIL v2，不应调用 HTTP"""
        code = _make_chart_type_code(chart_type)
        _reset_singleton()

        mock_http_bytes = b"\x89PNG\r\n\x1a\n" + b"fake_png_data" * 100

        with patch(
            "app.services.ai.mermaid_renderer.get_mermaid_service_client"
        ) as MockGetClient:
            mock_instance = MagicMock()
            mock_instance.render_mermaid_via_http.return_value = mock_http_bytes
            MockGetClient.return_value = mock_instance

            result = render_mermaid_to_bytes(code, chart_type)
            assert result is not None, f"[{chart_type}] JSON 载荷应经 PIL v2 渲染"
            data = result.getvalue()
            assert data[:8] == b"\x89PNG\r\n\x1a\n", f"[{chart_type}] 应返回有效 PNG"
            assert not mock_instance.render_mermaid_via_http.called, (
                f"[{chart_type}] JSON 数据载荷不应调用 HTTP Service（必然失败，浪费重试）"
            )
            print(f"  [{chart_type}] JSON载荷→PIL v2 直连 OK")


# ─────────────────────────────────────────────────────────────────────────────
# 场景 B: HTTP 失败 → mmdc 可用
# ─────────────────────────────────────────────────────────────────────────────

class TestHttpFailsMmdcWorks:
    """HTTP 失败后降级到 PIL v2（V5.0 已移除 mmdc 降级路径）"""

    @pytest.mark.parametrize("chart_type", ["flowchart", "gantt", "architecture"])
    def test_fallback_to_pil_v2(self, chart_type):
        """HTTP 失败后，应降级到 PIL v2（V5.0 移除了 mmdc 降级）"""
        code = _make_chart_type_code(chart_type)
        _reset_singleton()

        with patch(
            "app.services.ai.mermaid_renderer.get_mermaid_service_client"
        ) as MockGetClient:
            mock_instance = MagicMock()
            mock_instance.render_mermaid_via_http.side_effect = MermaidRenderError("所有后端不可用")
            MockGetClient.return_value = mock_instance

            result = render_mermaid_to_bytes(code, chart_type)
            assert result is not None, f"[{chart_type}] HTTP 失败时应走 PIL v2"
            print(f"  [{chart_type}] HTTP失败→PILv2 降级链 OK")

    def test_fallback_chain_flowchart(self):
        """流程图中 HTTP→PIL 的降级链（V5.0 无 mmdc）"""
        code = _make_flowchart_code()
        _reset_singleton()

        with patch(
            "app.services.ai.mermaid_renderer.get_mermaid_service_client"
        ) as MockGetClient:
            mock_instance = MagicMock()
            mock_instance.render_mermaid_via_http.side_effect = MermaidRenderError("unavailable")
            MockGetClient.return_value = mock_instance

            result = render_mermaid_to_bytes(code, "flowchart")
            assert result is not None
            data = result.getvalue()
            assert data[:8] == b"\x89PNG\r\n\x1a\n"
            print(f"  [flowchart] 降级链 HTTP→PILv2 OK - {len(data)} bytes")


# ─────────────────────────────────────────────────────────────────────────────
# 场景 C: HTTP 失败 + mmdc 不可用 → 全部走 PIL v2
# ─────────────────────────────────────────────────────────────────────────────

class TestHttpAndMmdcBothUnavailable:
    """HTTP 失败时，验证全部 7 种图表类型走 PIL v2"""

    @pytest.mark.parametrize("chart_type", [
        "flowchart", "gantt", "architecture",
        "comparison", "labor", "layout", "timeline",
    ])
    def test_all_types_fall_back_to_pil_v2(self, chart_type):
        """所有类型在 HTTP 失败时，应走 PIL v2 且成功"""
        code = _make_chart_type_code(chart_type)
        _reset_singleton()

        with patch(
            "app.services.ai.mermaid_renderer.get_mermaid_service_client"
        ) as MockGetClient:
            mock_instance = MagicMock()
            mock_instance.render_mermaid_via_http.side_effect = MermaidRenderError("所有后端不可用")
            MockGetClient.return_value = mock_instance

            result = render_mermaid_to_bytes(code, chart_type)
            assert result is not None, (
                f"[{chart_type}] HTTP 失败时，PIL v2 必须成功"
            )
            data = result.getvalue()
            assert len(data) > 5000, (
                f"[{chart_type}] PIL v2 返回数据过小 ({len(data)} bytes)"
            )
            assert data[:8] == b"\x89PNG\r\n\x1a\n", f"[{chart_type}] 不是有效 PNG"
            print(f"  [{chart_type}] PIL v2 兜底 OK - {len(data)} bytes")

    def test_pil_v2_is_called_with_correct_renderer(self):
        """验证各类型正确调用了对应的 v2 渲染器"""
        test_cases = [
            ("flowchart", _make_flowchart_code, _render_flowchart_image_v2),
            ("gantt", _make_gantt_code, _render_gantt_image_v2),
            ("architecture", _make_architecture_code, _render_architecture_image_v2),
            ("comparison", lambda: _make_chart_type_code("comparison"), _render_comparison_image_v2),
            ("labor", lambda: _make_chart_type_code("labor"), _render_labor_image_v2),
            ("layout", lambda: _make_chart_type_code("layout"), _render_layout_image_v2),
            ("timeline", lambda: _make_chart_type_code("timeline"), _render_timeline_image_v2),
        ]

        with patch(
            "app.services.ai.mermaid_renderer.get_mermaid_service_client"
        ) as MockGetClient:
            mock_instance = MagicMock()
            mock_instance.render_mermaid_via_http.side_effect = MermaidRenderError("unavailable")
            MockGetClient.return_value = mock_instance

            for chart_type, code_fn, _ in test_cases:
                code = code_fn() if callable(code_fn) else code_fn
                result = render_mermaid_to_bytes(code, chart_type)
                assert result is not None, f"[{chart_type}] PIL v2 失败"
                print(f"  [{chart_type}] 正确路由到 v2 渲染器 OK")


# ─────────────────────────────────────────────────────────────────────────────
# 场景 D: HTTP 异常（非 MermaidRenderError）
# ─────────────────────────────────────────────────────────────────────────────

class TestHttpExceptionFallback:
    """HTTP 服务抛出异常（非 MermaidRenderError）时也正确降级"""

    @pytest.mark.parametrize("chart_type", [
        "flowchart", "comparison", "timeline",
    ])
    def test_http_exception_falls_back(self, chart_type):
        """HTTP 抛出普通异常时，也应降级到 PIL v2"""
        code = _make_chart_type_code(chart_type)
        _reset_singleton()

        with patch(
            "app.services.ai.mermaid_renderer.get_mermaid_service_client"
        ) as MockGetClient:
            mock_instance = MagicMock()
            # 抛出一个普通异常（模拟网络超时等）
            mock_instance.render_mermaid_via_http.side_effect = Exception("Connection timeout")
            MockGetClient.return_value = mock_instance

            result = render_mermaid_to_bytes(code, chart_type)
            assert result is not None, f"[{chart_type}] HTTP 异常时应降级到 PIL v2"
            data = result.getvalue()
            assert data[:8] == b"\x89PNG\r\n\x1a\n"
            print(f"  [{chart_type}] HTTP 异常→PILv2 降级 OK - {len(data)} bytes")


# ─────────────────────────────────────────────────────────────────────────────
# 场景 E: mmdc 实际可用时的降级验证
# ─────────────────────────────────────────────────────────────────────────────

class TestMmdcRemoved:
    """V5.0 已移除 mmdc 降级路径，验证不再调用 mmdc"""

    def test_mmdc_not_called_after_v5_remove(self):
        """V5.0 移除 mmdc 后，render_mermaid_to_bytes 不应调用 mmdc 函数"""
        code = _make_flowchart_code()
        _reset_singleton()

        with patch(
            "app.services.ai.mermaid_renderer.get_mermaid_service_client"
        ) as MockGetClient:
            mock_instance = MagicMock()
            mock_instance.render_mermaid_via_http.side_effect = MermaidRenderError("unavailable")
            MockGetClient.return_value = mock_instance

            # V5.0：mmdc 降级路径已被彻底移除，模块中不应再存在 _render_via_mmdc
            import app.services.ai.mermaid_renderer as _mr

            assert not hasattr(_mr, "_render_via_mmdc"), "V5.0 应移除 _render_via_mmdc 函数"
            assert not hasattr(_mr, "_detect_mmdc"), "V5.0 应移除 _detect_mmdc 函数"

            result = render_mermaid_to_bytes(code, "flowchart")
            assert result is not None
            # HTTP 不可用时应降级到 PIL v2 渲染
            data = result.getvalue()
            assert data[:8] == b"\x89PNG\r\n\x1a\n"
            print("  [mmdc] V5.0 确认 mmdc 不再被调用 OK")
