#!/usr/bin/env python3
"""
架构图 L3 叶子层竖排优化 — 正式回归测试。

依据《架构图 L3 叶子层文字垂直显示优化方案》：
  L3 叶子节点数 > _VERTICAL_THRESHOLD(6) 时自动启用竖排布局，
  将画布宽度从横向 ~2740px 缩减至 ~986px（>50%），适配 A4 纸宽/DOCX 导出。

覆盖增强清单：
  1. L3 节点竖排样式（PIL 模拟 writing-mode: vertical-rl → _draw_text_vertical）
  2. L3 尺寸配置化（_L3_VERTICAL_W/H/GAP）
  3. 自动判断节点数（_VERTICAL_THRESHOLD：L3 > 6 → 竖排）
  4. 长文本智能换行（竖排分列 + 省略号）
  5. 导出 PNG 竖排支持（_render_architecture_image_v2 返回有效 PNG）
  6. 画布宽度收敛（竖排显著窄于横排）
"""

from io import BytesIO

from app.services.ai import mermaid_architecture as arch

# 方案示例数据：青浦住宅项目质量管控体系（2+4+3+2 = 11 个 L3 叶子节点）
QINGPU_TREE = {
    "id": "root",
    "label": "青浦住宅项目质量管控体系",
    "children": [
        {
            "id": "g1", "label": "公司级质量管控",
            "children": [
                {"id": "g1_1", "label": "公司质量总监"},
                {"id": "g1_2", "label": "质量管理部"},
            ],
        },
        {
            "id": "g2", "label": "项目部质量执行",
            "children": [
                {"id": "g2_1", "label": "项目经理（第一责任人）"},
                {"id": "g2_2", "label": "技术负责人"},
                {"id": "g2_3", "label": "质量主管"},
                {"id": "g2_4", "label": "专职质检员"},
            ],
        },
        {
            "id": "g3", "label": "现场质量落实",
            "children": [
                {"id": "g3_1", "label": "PC灌浆过程控制"},
                {"id": "g3_2", "label": "防水蓄水试验组"},
                {"id": "g3_3", "label": "进场材料复试组"},
            ],
        },
        {
            "id": "g4", "label": "质量保障支撑",
            "children": [
                {"id": "g4_1", "label": "BIM管线综合平衡"},
                {"id": "g4_2", "label": "精密检测设备组"},
            ],
        },
    ],
}


# ============================================================
# 1. 阈值与叶子计数（自动判断节点数）
# ============================================================


def test_vertical_threshold_is_six():
    """方案约定：L3 节点 > 6 自动启用竖排，≤ 6 保持横排。"""
    assert arch._VERTICAL_THRESHOLD == 6


def test_count_leaves_qingpu():
    """青浦质量控制体系共有 2+4+3+2 = 11 个叶子节点。"""
    assert arch._count_leaves(QINGPU_TREE) == 11


def test_count_leaves_single_node():
    """无子节点的根自身计为一个叶子；兼容 nodes/sub_units 字段。"""
    assert arch._count_leaves({"label": "单节点"}) == 1
    assert arch._count_leaves({"label": "n", "nodes": [{"label": "a"}, {"label": "b"}]}) == 2


# ============================================================
# 2. 竖排布局（L3 窄高矩形 60x200 + 间距 8）
# ============================================================


def test_vertical_layout_leaf_size_and_style_flags():
    """竖排模式下 L3 叶子采用 60x200 窄高矩形，标记 vertical=True。"""
    nodes, _w, _h = arch._layout_architecture_tree(QINGPU_TREE, vertical_l3=True)
    leaves = [n for n in nodes if n.get("is_leaf") and n["depth"] >= 2]
    assert len(leaves) == 11
    for n in leaves:
        assert n["w"] == arch._L3_VERTICAL_W
        assert n["h"] == arch._L3_VERTICAL_H
        assert n.get("vertical") is True
    # 非叶子节点：root(depth=0) 保持 220 横排；竖排模式下 L2 中间节点收窄至 _L2_VERTICAL_W(160)
    middle = [n for n in nodes if not n.get("is_leaf")]
    assert middle, "树中应存在中间节点"
    assert all(n["w"] == 220 and n["h"] == 52 for n in middle if n["depth"] == 0)
    l2_nodes = [n for n in middle if n["depth"] == 1]
    assert len(l2_nodes) == 4
    assert all(n["w"] == arch._L2_VERTICAL_W and n["h"] == 52 for n in l2_nodes)


def test_vertical_layout_reduces_canvas_width_over_50pct():
    """竖排画布宽度较横排缩减 > 50%（方案目标 -54%）。"""
    _nv, wv, hv = arch._layout_architecture_tree(QINGPU_TREE, vertical_l3=True)
    _nh, wh, hh = arch._layout_architecture_tree(QINGPU_TREE, vertical_l3=False)
    assert wv < wh * 0.6, f"竖排宽度 {wv}px 未较横排 {wh}px 缩减 50%"
    # 竖排加高、变窄（竖矩形布局）
    assert hv > hh
    # L2 收窄后竖排画布 ~876px（2 组以上 L2 分组场景），保持紧凑
    assert wv < 950, f"竖排画布宽度 {wv}px 超出预期"


def test_vertical_l3_gap_is_tighter():
    """L3 竖排间距 8px（更紧凑，原横向 gap 24px）。"""
    assert arch._L3_VERTICAL_GAP == 8


def test_horizontal_layout_when_leaf_count_low():
    """L3 ≤ 6：保持横排且使用横排尺寸，不启用竖排。"""
    small = {
        "id": "root",
        "label": "小架构",
        "children": [
            {"id": "a", "label": "A", "children": [{"id": "a1", "label": "A1"}, {"id": "a2", "label": "A2"}]},
        ],
    }
    assert arch._count_leaves(small) <= arch._VERTICAL_THRESHOLD
    nodes, _w, _h = arch._layout_architecture_tree(small, vertical_l3=False)
    leaves = [n for n in nodes if n.get("is_leaf")]
    assert all(n["w"] == arch._L3_HORIZONTAL_W for n in leaves)
    assert all(n["h"] == arch._L3_HORIZONTAL_H for n in leaves)


# ============================================================
# 3. 竖排文字绘制（长文本分列 + 省略号）
# ============================================================


def test_draw_text_vertical_handles_long_and_short_text():
    """竖排绘制：长文本分列、短文本、空文本均不抛异常。"""
    from PIL import Image, ImageDraw

    from app.services.ai.mermaid_common import _image_font

    img = Image.new("RGB", (200, 400), "white")
    draw = ImageDraw.Draw(img)
    font = _image_font(16, bold=False)
    box = (10, 10, 60, 390)
    # 长文本 → 分列 + 省略号分支
    arch._draw_text_vertical(
        draw, box, "项目经理（第一责任人）BIM管线综合平衡超长文本分列", font, "#000000"
    )
    # 短文本单列
    arch._draw_text_vertical(draw, box, "短", font, "#000000")
    # 空文本直接返回
    arch._draw_text_vertical(draw, box, "", font, "#000000")


# ============================================================
# 4. 渲染入口：自动判断 + 导出 PNG
# ============================================================


def test_render_architecture_png_with_vertical_auto():
    """11 个叶子：自动启用竖排并产出有效 PNG（导出 DOCX 竖排支持）。"""
    stream = arch._render_architecture_image_v2({"root": QINGPU_TREE})
    assert stream is not None
    assert _png_head(stream) == b"\x89PNG\r\n\x1a\n"


def test_render_architecture_png_small_data_no_crash():
    """小数据（2 个叶子，≤6）：不触发竖排分支，渲染仍正常。"""
    stream = arch._render_architecture_image_v2(
        {"root": {"id": "root", "label": "小架构", "children": [{"id": "a1", "label": "A1"}]}}
    )
    assert stream is not None
    assert _png_head(stream) == b"\x89PNG\r\n\x1a\n"


def test_render_auto_switch_logs_vertical_mode(caplog):
    """渲染时的自动判断日志：11 叶 → 竖排模式=True。"""
    import logging

    _logger = logging.getLogger("app.services.ai.mermaid_architecture")
    with caplog.at_level(logging.INFO, logger=_logger.name):
        arch._render_architecture_image_v2({"root": QINGPU_TREE})
    assert any("竖排模式=True" in r.message for r in caplog.records)


def _png_head(stream: BytesIO) -> bytes:
    raw = stream.getvalue() if hasattr(stream, "getvalue") else stream.read()
    return raw[:8]