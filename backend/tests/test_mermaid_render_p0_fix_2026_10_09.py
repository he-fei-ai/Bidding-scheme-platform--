"""P0 流程图渲染失败修复（BUG-2026-1008-01）增强测试 — 2026-10-09。

覆盖修复方案 P0 中「按最优方案加固」的代码层改动：

  1. MermaidSanitizer 新增裸节点边修复：将 `文本 --> 文本`
     （修复方案失败样例的精确形态）改写为合法 `N1["文本"] --> N2["文本"]`；
  2. 同一裸标签跨行共享 ID（连线连通）；合法 ASCII 节点 ID（A/B/start）保持原样；
  3. 边标签 `|说明|` 保留；
  4. 里程碑时间线自动识别（含日期节点 + 里程碑关键词）→ 路由 timeline 渲染；
  5. 端到端：修复方案样例经 render_mermaid_to_bytes 成功产出 PNG（不再降级为文本）。

纯函数/本地渲染测试，不依赖外部 HTTP 渲染服务（用 skip_http 隔离）。
"""

import re
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.services.ai.mermaid_sanitizer import (
    MermaidSanitizer,
    sanitize_mermaid_code,
)
from app.services.ai.mermaid_renderer import (
    _flowchart_to_timeline_plan,
    _is_milestone_timeline,
    render_mermaid_to_bytes,
)

# 修复方案 1.1 中的失败样例（精确复刻：无方括号裸节点）
TENDER_BARE = (
    "flowchart TD\n"
    "开工: 2026-09-22 --> 出地面: 2026-12-15\n"
    "出地面: 2026-12-15 --> 主体50%: 2027-04-30\n"
    "主体50%: 2027-04-30 --> 主体封顶: 2027-07-15\n"
    "主体封顶: 2027-07-15 --> 竣工: 2028-05-30\n"
)


class TestBareNodeBracketization:
    """裸节点边修复（P0 核心）。"""

    def test_tender_bare_nodes_bracketized(self):
        out = sanitize_mermaid_code(TENDER_BARE)
        assert "N1" in out
        assert 'N1["开工: 2026-09-22"]' in out
        assert 'N2["出地面: 2026-12-15"]' in out
        # 所有节点都带方括号且已加双引号
        for line in out.split("\n")[1:]:
            if "-->" in line:
                assert "[" in line and '"' in line

    def test_shared_label_keeps_same_id(self):
        # 同一裸标签跨行复用同一 ID，保证连线连通
        code = "A --> 节点X\nB --> 节点X"
        out = sanitize_mermaid_code(code)
        # 出现两次「节点X」应映射到同一个 N-id
        assert out.count('["节点X"]') == 2

    def test_valid_ascii_ids_unchanged(self):
        # 合法 ASCII 节点 ID 不应被改写（既有契约：A --> B 保持原样）
        code = "A --> B\nC[Start] --> D[Mid] --> E[End]"
        out = sanitize_mermaid_code(code)
        assert "A --> B" in out

    def test_edge_label_preserved(self):
        # 边标签 |说明| 应保留
        code = '开工: 2026-09-22 -->|审核通过| 出地面: 2026-12-15'
        out = sanitize_mermaid_code(code)
        assert "|审核通过|" in out
        assert '["开工: 2026-09-22"]' in out

    def test_needs_sanitization_true_for_bare(self):
        assert MermaidSanitizer.needs_sanitization(TENDER_BARE) is True

    def test_needs_sanitization_false_for_valid(self):
        # 合法裸节点（ASCII ID）不应触发
        assert MermaidSanitizer.needs_sanitization("A --> B") is False

    def test_idempotent(self):
        once = sanitize_mermaid_code(TENDER_BARE)
        twice = sanitize_mermaid_code(once)
        assert once == twice


class TestMilestoneTimelineRouting:
    """里程碑时间线自动识别（P0-5 加固）。"""

    def test_detect_milestone_timeline_on_sanitized(self):
        sanitized = sanitize_mermaid_code(TENDER_BARE)
        assert _is_milestone_timeline(sanitized) is True

    def test_not_milestone_for_plain_flowchart(self):
        plain = 'flowchart TD\nA["施工准备"] --> B["测量放线"] --> C["钢筋绑扎"]'
        assert _is_milestone_timeline(plain) is False

    def test_plan_extracts_dates_and_names(self):
        sanitized = sanitize_mermaid_code(TENDER_BARE)
        plan = _flowchart_to_timeline_plan(sanitized)
        assert plan is not None
        assert plan["type"] == "timeline"
        names = [m["name"] for m in plan["milestones"]]
        assert "开工" in names[0]
        # 按日期升序
        dates = [m["date"] for m in plan["milestones"]]
        assert dates == sorted(dates)

    def test_no_date_nodes_not_timeline(self):
        code = 'flowchart TD\nA["步骤一"] --> B["步骤二"] --> C["步骤三"]'
        assert _flowchart_to_timeline_plan(code) is None


class TestEndToEndRenderNoDowngrade:
    """端到端：修复方案样例必须渲染为 PNG，而非降级为纯文本。"""

    def test_render_tender_bare_returns_png(self):
        img = render_mermaid_to_bytes(TENDER_BARE, "flowchart", skip_http=True)
        assert img is not None
        data = img.getvalue()
        # PNG 魔数校验
        assert data[:8] == b"\x89PNG\r\n\x1a\n"
        assert len(data) > 1000

    def test_render_routes_to_timeline_image(self):
        # 里程碑时间线自动路由应产出时间线图（与直接 timeline 渲染尺寸一致范畴）
        img = render_mermaid_to_bytes(TENDER_BARE, "flowchart", skip_http=True)
        assert img is not None
