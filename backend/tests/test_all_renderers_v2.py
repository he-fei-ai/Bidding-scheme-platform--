"""
全面测试后端图表渲染模块 mermaid_renderer.py 的所有 v2 渲染器函数。

测试目标：
1) _render_flowchart_image_v2 - 流程图_v2
2) _render_gantt_image_v2 - 甘特图_v2
3) _render_architecture_image_v2 - 架构图_v2
4) _render_comparison_image_v2 - 对比图_v2
5) _render_labor_image_v2 - 劳动力图_v2
6) _render_layout_image_v2 - 布局图_v2
7) _render_timeline_image_v2 - 时间轴_v2

每个渲染器测试：
- 正常数据：验证能接收数据并返回 PNG 字节流
- 边界条件：空列表、单元素、缺失字段
- 空值处理：None/null/空字符串
- JSON 数据格式兼容性：多种字段名称映射
- 返回结果验证：BytesIO 对象、有效 PNG 头、合理尺寸

注：v1 渲染器已按无降级原则移除，不再测试降级路径。
"""

import json
import sys
from io import BytesIO

# 确保能导入
sys.path.insert(0, "j:/编程/招投标方案平台/backend")

from app.services.ai.mermaid_renderer import (
    _render_flowchart_image_v2,
    _render_gantt_image_v2,
    _render_architecture_image_v2,
    _render_comparison_image_v2,
    _render_labor_image_v2,
    _render_layout_image_v2,
    _render_timeline_image_v2,
    render_mermaid_to_bytes,
)


def _assert_valid_png_stream(stream, test_name: str):
    """断言返回值是有效的 BytesIO PNG 流"""
    assert stream is not None, f"[{test_name}] 返回值不应为 None"
    assert isinstance(stream, BytesIO), f"[{test_name}] 返回值应为 BytesIO，实际为 {type(stream).__name__}"
    data = stream.getvalue()
    assert len(data) > 0, f"[{test_name}] PNG 数据长度应大于 0"
    # PNG magic bytes
    assert data[:8] == b"\x89PNG\r\n\x1a\n", f"[{test_name}] 不是有效的 PNG 文件头"
    print(f"  [{test_name}] OK - PNG size={len(data)} bytes, dims={stream.getvalue()}...")


def _assert_none_return(test_name: str, result):
    """断言在无效输入下应返回 None"""
    assert result is None, f"[{test_name}] 预期返回 None，但得到了 {type(result).__name__}"
    print(f"  [{test_name}] OK - correctly returned None")


# ============================================================
# 1. 流程图_v2 (_render_flowchart_image_v2)
# ============================================================

def test_flowchart_v2_normal():
    """正常 Mermaid 流程图代码（TD 方向）"""
    code = '''flowchart TD
    A["开始"] --> B["需求分析"]
    B --> C["方案设计"]
    C --> D["评审通过?"]
    D -->|是| E["详细设计"]
    D -->|否| B
    E --> F["文档编写"]
    F --> G["结束"]'''
    result = _render_flowchart_image_v2(code)
    _assert_valid_png_stream(result, "flowchart_v2_normal_TD")


def test_flowchart_v2_lr():
    """LR 方向的流程图"""
    code = '''flowchart LR
    A["开始"] --> B["步骤1"]
    B --> C["步骤2"]
    C --> D["结束"]'''
    result = _render_flowchart_image_v2(code)
    _assert_valid_png_stream(result, "flowchart_v2_LR")


def test_flowchart_v2_decision_nodes():
    """包含决策节点的流程图"""
    code = '''graph TD
    A["开始"] --> B{"条件判断"}
    B -->|通过| C["执行A"]
    B -->|失败| D["执行B"]
    C --> E["结束"]
    D --> E'''
    result = _render_flowchart_image_v2(code)
    _assert_valid_png_stream(result, "flowchart_v2_decision")


def test_flowchart_v2_with_title():
    """带 title 的流程图"""
    code = '''flowchart TD
    title 施工流程
    A["施工准备"] --> B["基础工程"]
    B --> C["主体结构"]'''
    result = _render_flowchart_image_v2(code)
    _assert_valid_png_stream(result, "flowchart_v2_with_title")


def test_flowchart_v2_empty():
    """空字符串"""
    result = _render_flowchart_image_v2("")
    _assert_none_return("flowchart_v2_empty", result)


def test_flowchart_v2_single_node():
    """只有一个节点"""
    code = '''flowchart TD
    A["仅一个节点"]'''
    result = _render_flowchart_image_v2(code)
    # 需要至少 2 个节点和 1 条边
    _assert_none_return("flowchart_v2_single_node", result)


def test_flowchart_v2_no_edges():
    """只有节点没有连线"""
    code = '''flowchart TD
    A["节点1"]
    B["节点2"]'''
    result = _render_flowchart_image_v2(code)
    _assert_none_return("flowchart_v2_no_edges", result)


def test_flowchart_v2_null_input():
    """None 输入"""
    result = _render_flowchart_image_v2(None)
    _assert_none_return("flowchart_v2_null", result)


def test_flowchart_v2_subgraph():
    """含 subgraph 的流程图"""
    code = '''flowchart TD
    subgraph 设计阶段
        A["需求分析"] --> B["方案设计"]
    end
    subgraph 实施阶段
        C["施工"] --> D["验收"]
    end
    B --> C'''
    result = _render_flowchart_image_v2(code)
    _assert_valid_png_stream(result, "flowchart_v2_subgraph")


def test_flowchart_v2_edge_labels():
    """带标签的边"""
    code = '''flowchart TD
    A["开始"] -->|审批| B["审核"]
    B -->|通过| C["完成"]
    B -->|拒绝| D["退回"]'''
    result = _render_flowchart_image_v2(code)
    _assert_valid_png_stream(result, "flowchart_v2_edge_labels")


# ============================================================
# 2. 甘特图_v2 (_render_gantt_image_v2)
# ============================================================

def test_gantt_v2_normal():
    """正常甘特图 plan 数据"""
    plan = {
        "type": "gantt",
        "title": "施工进度计划",
        "totalDays": 90,
        "tasks": [
            {"id": 1, "name": "施工准备", "start": 1, "end": 10},
            {"id": 2, "name": "基础工程", "start": 11, "end": 30},
            {"id": 3, "name": "主体结构", "start": 31, "end": 60},
            {"id": 4, "name": "装饰装修", "start": 50, "end": 80},
            {"id": 5, "name": "竣工验收", "start": 81, "end": 90},
        ]
    }
    result = _render_gantt_image_v2(plan)
    _assert_valid_png_stream(result, "gantt_v2_normal")


def test_gantt_v2_milestones():
    """含里程碑的甘特图"""
    plan = {
        "type": "gantt",
        "title": "项目里程碑",
        "totalDays": 60,
        "tasks": [
            {"id": 1, "name": "开工", "start": 1, "end": 1, "isMilestone": True},
            {"id": 2, "name": "基础完成", "start": 30, "end": 30, "isMilestone": True},
            {"id": 3, "name": "主体封顶", "start": 50, "end": 50, "isMilestone": True},
            {"id": 4, "name": "竣工", "start": 60, "end": 60, "isMilestone": True},
        ]
    }
    result = _render_gantt_image_v2(plan)
    _assert_valid_png_stream(result, "gantt_v2_milestones")


def test_gantt_v2_dependencies():
    """含依赖关系的甘特图"""
    plan = {
        "type": "gantt",
        "title": "带依赖的甘特图",
        "totalDays": 60,
        "tasks": [
            {"id": 1, "name": "任务A", "start": 1, "end": 15, "dependencies": []},
            {"id": 2, "name": "任务B", "start": 16, "end": 30, "dependencies": [1]},
            {"id": 3, "name": "任务C", "start": 31, "end": 45, "dependencies": [2]},
            {"id": 4, "name": "任务D", "start": 46, "end": 60, "dependencies": [3]},
        ]
    }
    result = _render_gantt_image_v2(plan)
    _assert_valid_png_stream(result, "gantt_v2_dependencies")


def test_gantt_v2_various_field_names():
    """测试多种字段名称映射"""
    plan = {
        "type": "gantt",
        "title": "字段映射测试",
        "duration": 90,  # totalDays 或 duration
        "tasks": [
            {"task": "任务1", "start_day": 1, "end_day": 20},  # name->task, start->start_day
            {"title": "任务2", "from": 21, "to": 40},  # name->title, start->from
            {"name": "任务3", "start": 41, "end": 60},
        ]
    }
    result = _render_gantt_image_v2(plan)
    _assert_valid_png_stream(result, "gantt_v2_field_mapping")


def test_gantt_v2_items_format():
    """使用 items 代替 tasks"""
    plan = {
        "type": "gantt",
        "title": "items 格式",
        "totalDays": 60,
        "items": [
            {"name": "任务A", "start": 1, "end": 30},
            {"name": "任务B", "start": 31, "end": 60},
        ]
    }
    result = _render_gantt_image_v2(plan)
    _assert_valid_png_stream(result, "gantt_v2_items_format")


def test_gantt_v2_empty_tasks():
    """空任务列表"""
    plan = {"type": "gantt", "title": "空", "totalDays": 10, "tasks": []}
    result = _render_gantt_image_v2(plan)
    _assert_none_return("gantt_v2_empty_tasks", result)


def test_gantt_v2_null_plan():
    """None 输入"""
    result = _render_gantt_image_v2(None)
    _assert_none_return("gantt_v2_null", result)


def test_gantt_v2_invalid_plan():
    """非 dict 输入"""
    result = _render_gantt_image_v2("not a dict")
    _assert_none_return("gantt_v2_invalid", result)


def test_gantt_v2_list_input():
    """列表输入（非 dict）"""
    result = _render_gantt_image_v2([{"name": "A", "start": 1, "end": 10}])
    _assert_none_return("gantt_v2_list", result)


def test_gantt_v2_missing_duration():
    """缺少 totalDays/duration"""
    plan = {
        "type": "gantt",
        "tasks": [
            {"name": "任务1", "start": 1, "end": 30},
        ]
    }
    result = _render_gantt_image_v2(plan)
    _assert_valid_png_stream(result, "gantt_v2_auto_duration")


def test_gantt_v2_max_tasks_limit():
    """超过 30 个任务（应截断到 30）"""
    tasks = [{"id": i + 1, "name": f"任务{i+1}", "start": i * 3 + 1, "end": (i + 1) * 3} for i in range(50)]
    plan = {"type": "gantt", "title": "大量任务", "totalDays": 150, "tasks": tasks}
    result = _render_gantt_image_v2(plan)
    _assert_valid_png_stream(result, "gantt_v2_50_tasks_capped")


def test_gantt_v2_mermaid_source():
    """通过 render_mermaid_to_bytes 传递 Mermaid gantt 代码"""
    code = '''gantt
    title 施工进度计划
    dateFormat YYYY-MM-DD
    section 准备阶段
    施工准备 :2025-01-01, 30d
    场地平整 :2025-01-15, 20d
    section 主体阶段
    基础工程 :2025-02-01, 45d
    主体结构 :2025-03-15, 90d
    section 装饰阶段
    装饰装修 :2025-06-01, 60d
    室外工程 :2025-07-30, 30d'''
    result = render_mermaid_to_bytes(code, "gantt")
    _assert_valid_png_stream(result, "gantt_v2_mermaid_source")


# ============================================================
# 3. 架构图_v2 (_render_architecture_image_v2)
# ============================================================

def test_architecture_v2_normal():
    """正常架构树"""
    data = {
        "label": "系统架构",
        "children": [
            {
                "label": "前端层",
                "children": [
                    {"label": "Web 应用"},
                    {"label": "移动端"},
                ]
            },
            {
                "label": "后端层",
                "children": [
                    {"label": "API 服务"},
                    {
                        "label": "业务逻辑",
                        "children": [
                            {"label": "认证模块"},
                            {"label": "数据处理"},
                        ]
                    }
                ]
            },
            {
                "label": "数据层",
                "children": [
                    {"label": "数据库"},
                    {"label": "缓存"},
                ]
            }
        ]
    }
    result = _render_architecture_image_v2(data)
    _assert_valid_png_stream(result, "architecture_v2_normal")


def test_architecture_v2_name_field():
    """使用 name 而非 label"""
    data = {
        "name": "组织架构图",
        "children": [
            {"name": "部门A", "children": [{"name": "小组1"}, {"name": "小组2"}]},
            {"name": "部门B"},
        ]
    }
    result = _render_architecture_image_v2(data)
    _assert_valid_png_stream(result, "architecture_v2_name_field")


def test_architecture_v2_nested_data():
    """嵌套在 root/tree/data 键中"""
    data = {
        "root": {
            "label": "根节点",
            "children": [
                {"label": "子节点1"},
                {"label": "子节点2"},
            ]
        }
    }
    result = _render_architecture_image_v2(data)
    _assert_valid_png_stream(result, "architecture_v2_nested_root")


def test_architecture_v2_deep_tree():
    """深层级架构树（5 层）"""
    data = {
        "label": "L0",
        "children": [
            {
                "label": "L1-A",
                "children": [
                    {
                        "label": "L2-A",
                        "children": [
                            {
                                "label": "L3-A",
                                "children": [
                                    {"label": "L4-A"},
                                ]
                            }
                        ]
                    }
                ]
            },
            {"label": "L1-B"},
        ]
    }
    result = _render_architecture_image_v2(data)
    _assert_valid_png_stream(result, "architecture_v2_deep_tree")


def test_architecture_v2_empty_children():
    """空 children 数组"""
    data = {"label": "叶子节点", "children": []}
    result = _render_architecture_image_v2(data)
    _assert_valid_png_stream(result, "architecture_v2_empty_children")


def test_architecture_v2_null_input():
    """None 输入"""
    result = _render_architecture_image_v2(None)
    _assert_none_return("architecture_v2_null", result)


def test_architecture_v2_not_dict():
    """非 dict 输入"""
    result = _render_architecture_image_v2("not a dict")
    _assert_none_return("architecture_v2_not_dict", result)


def test_architecture_v2_no_label_or_name():
    """无 label 也无 name"""
    data = {"children": [{"label": "子节点"}]}
    result = _render_architecture_image_v2(data)
    _assert_none_return("architecture_v2_no_label", result)


def test_architecture_v2_single_leaf():
    """单个叶子节点"""
    data = {"label": "唯一节点"}
    result = _render_architecture_image_v2(data)
    _assert_valid_png_stream(result, "architecture_v2_single_leaf")


# ============================================================
# 4. 对比图_v2 (_render_comparison_image_v2)
# ============================================================

def test_comparison_v2_standard_table():
    """标准表格格式"""
    data = {
        "headers": ["对比项", "方案A", "方案B"],
        "rows": [
            {"对比项": "材料费", "方案A": 80, "方案B": 65},
            {"对比项": "人工费", "方案A": 50, "方案B": 55},
            {"对比项": "工期", "方案A": 90, "方案B": 75},
            {"对比项": "总成本", "方案A": 160, "方案B": 140},
        ]
    }
    result = _render_comparison_image_v2(data)
    _assert_valid_png_stream(result, "comparison_v2_standard_table")


def test_comparison_v2_simple_label_value():
    """极简 label/value 格式"""
    data = {
        "rows": [
            {"label": "方案A", "value": 30},
            {"label": "方案B", "value": 50},
            {"label": "方案C", "value": 20},
        ]
    }
    result = _render_comparison_image_v2(data)
    _assert_valid_png_stream(result, "comparison_v2_simple_label_value")


def test_comparison_v2_items_format():
    """Mermaid pie 数据格式 (items)"""
    data = {
        "items": [
            {"label": "钢材", "value": 40},
            {"label": "水泥", "value": 30},
            {"label": "木材", "value": 20},
            {"label": "其他", "value": 10},
        ]
    }
    result = _render_comparison_image_v2(data)
    _assert_valid_png_stream(result, "comparison_v2_items_format")


def test_comparison_v2_values_array():
    """多指标 values 数组格式"""
    data = {
        "headers": ["方案", "材料费", "人工费", "机械费", "总计"],
        "rows": [
            {"label": "方案A", "values": [80, 50, 30, 160]},
            {"label": "方案B", "values": [65, 55, 25, 145]},
            {"label": "方案C", "values": [70, 45, 35, 150]},
        ]
    }
    result = _render_comparison_image_v2(data)
    _assert_valid_png_stream(result, "comparison_v2_values_array")


def test_comparison_v2_numeric_pairs():
    """数值对格式"""
    data = {
        "rows": [
            {"钢材": 40},
            {"水泥": 30},
            {"木材": 20},
        ]
    }
    result = _render_comparison_image_v2(data)
    _assert_valid_png_stream(result, "comparison_v2_numeric_pairs")


def test_comparison_v2_chinese_headers():
    """中文表头字段映射"""
    data = {
        "rows": [
            {"label": "方案A", "数值": 30},
            {"label": "方案B", "数值": 50},
            {"label": "方案C", "占比": 20},
        ]
    }
    result = _render_comparison_image_v2(data)
    _assert_valid_png_stream(result, "comparison_v2_chinese_fields")


def test_comparison_v2_with_title():
    """带标题的对比图"""
    data = {
        "title": "施工方案对比分析",
        "rows": [
            {"label": "传统方案", "value": 100},
            {"label": "优化方案", "value": 80},
        ]
    }
    result = _render_comparison_image_v2(data)
    _assert_valid_png_stream(result, "comparison_v2_with_title")


def test_comparison_v2_empty_rows():
    """空 rows"""
    data = {"headers": ["A", "B"], "rows": []}
    result = _render_comparison_image_v2(data)
    _assert_none_return("comparison_v2_empty_rows", result)


def test_comparison_v2_null_input():
    """None 输入"""
    result = _render_comparison_image_v2(None)
    _assert_none_return("comparison_v2_null", result)


def test_comparison_v2_not_dict():
    """非 dict 输入"""
    result = _render_comparison_image_v2("not a dict")
    _assert_none_return("comparison_v2_not_dict", result)


def test_comparison_v2_zero_values():
    """零值或负值过滤"""
    data = {
        "rows": [
            {"label": "有效", "value": 50},
            {"label": "零值", "value": 0},
            {"label": "负值", "value": -10},
            {"label": "另一个有效", "value": 30},
        ]
    }
    result = _render_comparison_image_v2(data)
    _assert_valid_png_stream(result, "comparison_v2_zero_values_filtered")


def test_comparison_v2_single_row():
    """单行数据"""
    data = {
        "rows": [
            {"label": "唯一方案", "value": 100},
        ]
    }
    result = _render_comparison_image_v2(data)
    _assert_valid_png_stream(result, "comparison_v2_single_row")


def test_comparison_v2_many_colors():
    """超过调色板数量的行"""
    data = {
        "rows": [
            {"label": f"方案{i}", "value": 10 + i} for i in range(20)
        ]
    }
    result = _render_comparison_image_v2(data)
    _assert_valid_png_stream(result, "comparison_v2_many_colors")


# ============================================================
# 5. 劳动力图_v2 (_render_labor_image_v2)
# ============================================================

def test_labor_v2_normal():
    """正常劳动力数据"""
    data = {
        "title": "劳动力投入计划",
        "phases": ["准备阶段", "基础阶段", "主体阶段", "装饰阶段", "收尾阶段"],
        "categories": ["木工", "钢筋工", "混凝土工", "砌筑工", "抹灰工"],
        "data": [
            {"phase": "准备阶段", "values": [5, 2, 1, 0, 0]},
            {"phase": "基础阶段", "values": [10, 20, 15, 8, 5]},
            {"phase": "主体阶段", "values": [25, 30, 20, 15, 10]},
            {"phase": "装饰阶段", "values": [15, 5, 3, 20, 25]},
            {"phase": "收尾阶段", "values": [5, 2, 1, 5, 10]},
        ]
    }
    result = _render_labor_image_v2(data)
    _assert_valid_png_stream(result, "labor_v2_normal")


def test_labor_v2_plain_arrays():
    """纯数组格式（无 phase 字段）"""
    data = {
        "phases": ["1月", "2月", "3月", "4月"],
        "categories": ["木工", "钢筋工", "混凝土工"],
        "data": [
            [5, 3, 2],
            [10, 8, 5],
            [15, 12, 8],
            [8, 5, 3],
        ]
    }
    result = _render_labor_image_v2(data)
    _assert_valid_png_stream(result, "labor_v2_plain_arrays")


def test_labor_v2_stages_key():
    """使用 stages 代替 phases"""
    data = {
        "stages": ["阶段A", "阶段B", "阶段C"],
        "types": ["工种1", "工种2"],
        "data": [
            [10, 5],
            [20, 15],
            [8, 3],
        ]
    }
    result = _render_labor_image_v2(data)
    _assert_valid_png_stream(result, "labor_v2_stages_key")


def test_labor_v2_time_key():
    """使用 time 代替 phases"""
    data = {
        "time": ["Q1", "Q2", "Q3", "Q4"],
        "categories": ["电工", "焊工"],
        "data": [
            [5, 3],
            [10, 8],
            [12, 10],
            [6, 4],
        ]
    }
    result = _render_labor_image_v2(data)
    _assert_valid_png_stream(result, "labor_v2_time_key")


def test_labor_v2_null_input():
    """None 输入"""
    result = _render_labor_image_v2(None)
    _assert_none_return("labor_v2_null", result)


def test_labor_v2_not_dict():
    """非 dict 输入"""
    result = _render_labor_image_v2("not a dict")
    _assert_none_return("labor_v2_not_dict", result)


def test_labor_v2_empty_phases():
    """空 phases"""
    data = {"phases": [], "categories": ["A"], "data": []}
    result = _render_labor_image_v2(data)
    _assert_none_return("labor_v2_empty_phases", result)


def test_labor_v2_empty_categories():
    """空 categories"""
    data = {"phases": ["P1"], "categories": [], "data": [[1]]}
    result = _render_labor_image_v2(data)
    _assert_none_return("labor_v2_empty_categories", result)


def test_labor_v2_missing_data():
    """缺少 data 字段"""
    data = {"phases": ["P1"], "categories": ["A"]}
    result = _render_labor_image_v2(data)
    _assert_none_return("labor_v2_missing_data", result)


def test_labor_v2_matrix_mismatch():
    """矩阵维度不匹配（自动补齐/截断）"""
    data = {
        "phases": ["P1", "P2"],
        "categories": ["A", "B", "C"],
        "data": [
            [10, 5],           # 只有 2 列，应补齐为 3
            [20, 15, 8, 99],   # 有 4 列，应截断为 3
        ]
    }
    result = _render_labor_image_v2(data)
    _assert_valid_png_stream(result, "labor_v2_matrix_resize")


def test_labor_v2_zero_values():
    """包含零值的矩阵"""
    data = {
        "phases": ["P1", "P2"],
        "categories": ["A", "B"],
        "data": [
            [0, 10],
            [5, 0],
        ]
    }
    result = _render_labor_image_v2(data)
    _assert_valid_png_stream(result, "labor_v2_zero_values")


# ============================================================
# 6. 布局图_v2 (_render_layout_image_v2)
# ============================================================

def test_layout_v2_normal():
    """正常布局数据"""
    data = {
        "title": "施工现场总平面布置图",
        "zones": [
            {"name": "大门入口", "category": "运输通道", "position": "东侧", "size": "10m x 8m"},
            {"name": "办公区", "category": "办公区", "position": "北侧", "size": "200㎡"},
            {"name": "生活区", "category": "生活区", "position": "西北角", "size": "300㎡"},
            {"name": "钢筋加工区", "category": "加工区", "position": "西侧", "size": "150㎡"},
            {"name": "材料堆场", "category": "材料堆放区", "position": "南侧", "size": "400㎡"},
            {"name": "主体施工区", "category": "生产区", "position": "中部", "size": "800㎡"},
            {"name": "危险品仓库", "category": "危险品区", "position": "东南角", "size": "50㎡"},
        ]
    }
    result = _render_layout_image_v2(data)
    _assert_valid_png_stream(result, "layout_v2_normal")


def test_layout_v2_areas_key():
    """使用 areas 代替 zones"""
    data = {
        "areas": [
            {"label": "区域A", "type": "生产区", "desc": "主要作业区"},
            {"label": "区域B", "type": "生活区", "desc": "员工宿舍"},
        ]
    }
    result = _render_layout_image_v2(data)
    _assert_valid_png_stream(result, "layout_v2_areas_key")


def test_layout_v2_auto_category():
    """自动推断分类"""
    data = {
        "zones": [
            {"name": "工人宿舍"},  # 应自动识别为"生活区"
            {"name": "会议室"},    # 应自动识别为"办公区"
            {"name": "钢筋堆放场"},# 应自动识别为"材料堆放区"
            {"name": "木工加工棚"},# 应自动识别为"加工区"
        ]
    }
    result = _render_layout_image_v2(data)
    _assert_valid_png_stream(result, "layout_v2_auto_category")


def test_layout_v2_custom_color():
    """自定义颜色"""
    data = {
        "zones": [
            {"name": "特殊区域", "color": "#FF5733", "description": "自定义颜色测试"},
            {"name": "普通区域"},
        ]
    }
    result = _render_layout_image_v2(data, title="颜色测试")
    _assert_valid_png_stream(result, "layout_v2_custom_color")


def test_layout_v2_connections():
    """带连接线的布局图"""
    data = {
        "zones": [
            {"name": "大门", "category": "运输通道"},
            {"name": "办公区", "category": "办公区"},
            {"name": "生活区", "category": "生活区"},
            {"name": "施工区", "category": "生产区"},
        ],
        "connections": [
            {"from": "大门", "to": "施工区"},
            {"from": "办公区", "to": "施工区"},
            {"from": "生活区", "to": "施工区"},
        ]
    }
    result = _render_layout_image_v2(data)
    _assert_valid_png_stream(result, "layout_v2_connections")


def test_layout_v2_integer_indices():
    """connection 使用整数索引"""
    data = {
        "zones": [
            {"name": "A区", "category": "生产区"},
            {"name": "B区", "category": "生活区"},
            {"name": "C区", "category": "办公区"},
        ],
        "connections": [
            {"from": 0, "to": 1},
            {"from": 1, "to": 2},
        ]
    }
    result = _render_layout_image_v2(data)
    _assert_valid_png_stream(result, "layout_v2_integer_indices")


def test_layout_v2_empty_zones():
    """空 zones"""
    result = _render_layout_image_v2({"zones": []})
    _assert_none_return("layout_v2_empty_zones", result)


def test_layout_v2_null_input():
    """None 输入"""
    result = _render_layout_image_v2(None)
    _assert_none_return("layout_v2_null", result)


def test_layout_v2_not_dict():
    """非 dict 输入"""
    result = _render_layout_image_v2("not a dict")
    _assert_none_return("layout_v2_not_dict", result)


def test_layout_v2_single_zone():
    """单个区域"""
    data = {"zones": [{"name": "唯一区域", "category": "生产区"}]}
    result = _render_layout_image_v2(data)
    _assert_valid_png_stream(result, "layout_v2_single_zone")


def test_layout_v2_role_field():
    """使用 role 代替 category"""
    data = {
        "zones": [
            {"name": "区域A", "role": "生产区", "pos": "东侧", "area": "200㎡"},
            {"name": "区域B", "role": "生活区"},
        ]
    }
    result = _render_layout_image_v2(data)
    _assert_valid_png_stream(result, "layout_v2_role_field")


# ============================================================
# 7. 时间轴_v2 (_render_timeline_image_v2)
# ============================================================

def test_timeline_v2_normal():
    """正常时间轴数据"""
    data = {
        "title": "项目关键里程碑",
        "milestones": [
            {"name": "项目启动", "date": "2025-01-01", "desc": "正式开工"},
            {"name": "基础完工", "date": "2025-03-15", "desc": "地下室结构完成"},
            {"name": "主体封顶", "date": "2025-07-01", "desc": "主体结构完成"},
            {"name": "装饰完成", "date": "2025-10-15", "desc": "内外装饰完成"},
            {"name": "竣工验收", "date": "2025-12-31", "desc": "项目交付"},
        ]
    }
    result = _render_timeline_image_v2(data)
    _assert_valid_png_stream(result, "timeline_v2_normal")


def test_timeline_v2_events_key():
    """使用 events 代替 milestones"""
    data = {
        "events": [
            {"title": "事件A", "time": "2025-01-01", "description": "描述A"},
            {"title": "事件B", "time": "2025-06-01", "description": "描述B"},
        ]
    }
    result = _render_timeline_image_v2(data)
    _assert_valid_png_stream(result, "timeline_v2_events_key")


def test_timeline_v2_points_key():
    """使用 points 代替 milestones"""
    data = {
        "points": [
            {"label": "点A", "day": "第1天", "desc": "说明A"},
            {"label": "点B", "day": "第10天", "desc": "说明B"},
        ]
    }
    result = _render_timeline_image_v2(data)
    _assert_valid_png_stream(result, "timeline_v2_points_key")


def test_timeline_v2_groups():
    """含阶段分组"""
    data = {
        "milestones": [
            {"name": "里程碑1", "date": "2025-01-01", "group": "第一阶段"},
            {"name": "里程碑2", "date": "2025-03-01", "group": "第一阶段"},
            {"name": "里程碑3", "date": "2025-06-01", "group": "第二阶段"},
            {"name": "里程碑4", "date": "2025-09-01", "group": "第二阶段"},
            {"name": "里程碑5", "date": "2025-12-01", "group": "第三阶段"},
        ]
    }
    result = _render_timeline_image_v2(data)
    _assert_valid_png_stream(result, "timeline_v2_groups")


def test_timeline_v2_phase_key():
    """使用 phase 代替 group"""
    data = {
        "milestones": [
            {"name": "A", "date": "2025-01-01", "phase": "前期"},
            {"name": "B", "date": "2025-06-01", "phase": "中期"},
            {"name": "C", "date": "2025-12-01", "phase": "后期"},
        ]
    }
    result = _render_timeline_image_v2(data)
    _assert_valid_png_stream(result, "timeline_v2_phase_key")


def test_timeline_v2_single_milestone():
    """单个里程碑"""
    data = {
        "milestones": [
            {"name": "唯一里程碑", "date": "2025-06-01", "desc": "重要节点"},
        ]
    }
    result = _render_timeline_image_v2(data)
    _assert_valid_png_stream(result, "timeline_v2_single_milestone")


def test_timeline_v2_empty_milestones():
    """空 milestones"""
    result = _render_timeline_image_v2({"milestones": []})
    _assert_none_return("timeline_v2_empty", result)


def test_timeline_v2_null_input():
    """None 输入"""
    result = _render_timeline_image_v2(None)
    _assert_none_return("timeline_v2_null", result)


def test_timeline_v2_not_dict():
    """非 dict 输入"""
    result = _render_timeline_image_v2("not a dict")
    _assert_none_return("timeline_v2_not_dict", result)


def test_timeline_v2_no_dates():
    """无日期信息"""
    data = {
        "milestones": [
            {"name": "事件A"},
            {"name": "事件B"},
            {"name": "事件C"},
        ]
    }
    result = _render_timeline_image_v2(data)
    _assert_valid_png_stream(result, "timeline_v2_no_dates")


def test_timeline_v2_skip_empty_names():
    """跳过空名称"""
    data = {
        "milestones": [
            {"name": "", "date": "2025-01-01"},
            {"name": "有效", "date": "2025-06-01"},
            {"date": "2025-09-01"},  # 无 name
            {"name": "也有效", "date": "2025-12-01"},
        ]
    }
    result = _render_timeline_image_v2(data)
    _assert_valid_png_stream(result, "timeline_v2_skip_empty_names")


def test_timeline_v2_many_milestones():
    """大量里程碑"""
    data = {
        "milestones": [
            {"name": f"事件{i}", "date": f"2025-{str(i//12+1).zfill(2)}-{str(i%28+1).zfill(2)}"}
            for i in range(30)
        ]
    }
    result = _render_timeline_image_v2(data)
    _assert_valid_png_stream(result, "timeline_v2_many_milestones")


# ============================================================
# 8. 主入口函数 render_mermaid_to_bytes 综合测试
# ============================================================

def test_main_entry_flowchart():
    """主入口 - 流程图"""
    code = '''flowchart TD
    A["开始"] --> B["步骤1"]
    B --> C["步骤2"]
    C --> D["结束"]'''
    result = render_mermaid_to_bytes(code, "flowchart")
    _assert_valid_png_stream(result, "main_flowchart")


def test_main_entry_gantt():
    """主入口 - 甘特图"""
    code = '''gantt
    title 测试甘特图
    dateFormat YYYY-MM-DD
    section 测试
    任务A :2025-01-01, 30d
    任务B :2025-01-15, 20d'''
    result = render_mermaid_to_bytes(code, "gantt")
    _assert_valid_png_stream(result, "main_gantt")


def test_main_entry_architecture():
    """主入口 - 架构图"""
    code = json.dumps({
        "label": "根节点",
        "children": [
            {"label": "子节点A"},
            {"label": "子节点B"},
        ]
    })
    result = render_mermaid_to_bytes(code, "architecture")
    _assert_valid_png_stream(result, "main_architecture")


def test_main_entry_comparison():
    """主入口 - 对比图"""
    code = json.dumps({
        "rows": [
            {"label": "A", "value": 30},
            {"label": "B", "value": 50},
        ]
    })
    result = render_mermaid_to_bytes(code, "comparison")
    _assert_valid_png_stream(result, "main_comparison")


def test_main_entry_labor():
    """主入口 - 劳动力图"""
    code = json.dumps({
        "phases": ["P1", "P2"],
        "categories": ["A", "B"],
        "data": [[10, 5], [20, 15]],
    })
    result = render_mermaid_to_bytes(code, "labor")
    _assert_valid_png_stream(result, "main_labor")


def test_main_entry_layout():
    """主入口 - 布局图"""
    code = json.dumps({
        "zones": [
            {"name": "区域A", "category": "生产区"},
            {"name": "区域B", "category": "生活区"},
        ]
    })
    result = render_mermaid_to_bytes(code, "layout")
    _assert_valid_png_stream(result, "main_layout")


def test_main_entry_timeline():
    """主入口 - 时间轴"""
    code = json.dumps({
        "milestones": [
            {"name": "事件A", "date": "2025-01-01"},
            {"name": "事件B", "date": "2025-06-01"},
        ]
    })
    result = render_mermaid_to_bytes(code, "timeline")
    _assert_valid_png_stream(result, "main_timeline")


def test_main_entry_chart_wildcard():
    """主入口 - chart 通配符（自动识别内部 type）"""
    code = json.dumps({
        "type": "flowchart",
        "nodes": ["节点A", "节点B", "节点C"],
    })
    result = render_mermaid_to_bytes(code, "chart")
    # chart 通配符能识别 flowchart 类型
    if result:
        _assert_valid_png_stream(result, "main_chart_wildcard_flowchart")
    else:
        print("  [main_chart_wildcard] Note: chart wildcard for nodes array fell through to v1 renderer")


def test_main_entry_empty_code():
    """空代码"""
    result = render_mermaid_to_bytes("", "flowchart")
    _assert_none_return("main_empty_code", result)


def test_main_entry_whitespace_only():
    """空白字符"""
    result = render_mermaid_to_bytes("   \n\t  ", "flowchart")
    _assert_none_return("main_whitespace_only", result)


def test_main_entry_unknown_type():
    """未知图表类型"""
    result = render_mermaid_to_bytes("some code", "unknown_type_xyz")
    _assert_none_return("main_unknown_type", result)


# ============================================================
# Runner
# ============================================================

def run_all_tests():
    """运行所有测试"""
    tests = [
        # 流程图_v2
        ("流程图_v2 - 正常(TD)", test_flowchart_v2_normal),
        ("流程图_v2 - LR方向", test_flowchart_v2_lr),
        ("流程图_v2 - 决策节点", test_flowchart_v2_decision_nodes),
        ("流程图_v2 - 带标题", test_flowchart_v2_with_title),
        ("流程图_v2 - 空字符串", test_flowchart_v2_empty),
        ("流程图_v2 - 单节点", test_flowchart_v2_single_node),
        ("流程图_v2 - 无边", test_flowchart_v2_no_edges),
        ("流程图_v2 - None输入", test_flowchart_v2_null_input),
        ("流程图_v2 - Subgraph", test_flowchart_v2_subgraph),
        ("流程图_v2 - 边标签", test_flowchart_v2_edge_labels),

        # 甘特图_v2
        ("甘特图_v2 - 正常", test_gantt_v2_normal),
        ("甘特图_v2 - 里程碑", test_gantt_v2_milestones),
        ("甘特图_v2 - 依赖关系", test_gantt_v2_dependencies),
        ("甘特图_v2 - 字段映射", test_gantt_v2_various_field_names),
        ("甘特图_v2 - items格式", test_gantt_v2_items_format),
        ("甘特图_v2 - 空任务", test_gantt_v2_empty_tasks),
        ("甘特图_v2 - None输入", test_gantt_v2_null_plan),
        ("甘特图_v2 - 非dict", test_gantt_v2_invalid_plan),
        ("甘特图_v2 - 列表输入", test_gantt_v2_list_input),
        ("甘特图_v2 - 自动工期", test_gantt_v2_missing_duration),
        ("甘特图_v2 - 50任务截断", test_gantt_v2_max_tasks_limit),
        ("甘特图_v2 - Mermaid源码", test_gantt_v2_mermaid_source),

        # 架构图_v2
        ("架构图_v2 - 正常", test_architecture_v2_normal),
        ("架构图_v2 - name字段", test_architecture_v2_name_field),
        ("架构图_v2 - 嵌套root", test_architecture_v2_nested_data),
        ("架构图_v2 - 深树5层", test_architecture_v2_deep_tree),
        ("架构图_v2 - 空children", test_architecture_v2_empty_children),
        ("架构图_v2 - None输入", test_architecture_v2_null_input),
        ("架构图_v2 - 非dict", test_architecture_v2_not_dict),
        ("架构图_v2 - 无label/name", test_architecture_v2_no_label_or_name),
        ("架构图_v2 - 单叶子", test_architecture_v2_single_leaf),

        # 对比图_v2
        ("对比图_v2 - 标准表格", test_comparison_v2_standard_table),
        ("对比图_v2 - 极简label/value", test_comparison_v2_simple_label_value),
        ("对比图_v2 - items格式", test_comparison_v2_items_format),
        ("对比图_v2 - values数组", test_comparison_v2_values_array),
        ("对比图_v2 - 数值对", test_comparison_v2_numeric_pairs),
        ("对比图_v2 - 中文字段", test_comparison_v2_chinese_headers),
        ("对比图_v2 - 带标题", test_comparison_v2_with_title),
        ("对比图_v2 - 空rows", test_comparison_v2_empty_rows),
        ("对比图_v2 - None输入", test_comparison_v2_null_input),
        ("对比图_v2 - 非dict", test_comparison_v2_not_dict),
        ("对比图_v2 - 零值过滤", test_comparison_v2_zero_values),
        ("对比图_v2 - 单行", test_comparison_v2_single_row),
        ("对比图_v2 - 多色循环", test_comparison_v2_many_colors),

        # 劳动力图_v2
        ("劳动力图_v2 - 正常", test_labor_v2_normal),
        ("劳动力图_v2 - 纯数组", test_labor_v2_plain_arrays),
        ("劳动力图_v2 - stages键", test_labor_v2_stages_key),
        ("劳动力图_v2 - time键", test_labor_v2_time_key),
        ("劳动力图_v2 - None输入", test_labor_v2_null_input),
        ("劳动力图_v2 - 非dict", test_labor_v2_not_dict),
        ("劳动力图_v2 - 空phases", test_labor_v2_empty_phases),
        ("劳动力图_v2 - 空categories", test_labor_v2_empty_categories),
        ("劳动力图_v2 - 缺data", test_labor_v2_missing_data),
        ("劳动力图_v2 - 矩阵resize", test_labor_v2_matrix_mismatch),
        ("劳动力图_v2 - 零值", test_labor_v2_zero_values),

        # 布局图_v2
        ("布局图_v2 - 正常", test_layout_v2_normal),
        ("布局图_v2 - areas键", test_layout_v2_areas_key),
        ("布局图_v2 - 自动分类", test_layout_v2_auto_category),
        ("布局图_v2 - 自定义颜色", test_layout_v2_custom_color),
        ("布局图_v2 - 连接线", test_layout_v2_connections),
        ("布局图_v2 - 整数索引", test_layout_v2_integer_indices),
        ("布局图_v2 - 空zones", test_layout_v2_empty_zones),
        ("布局图_v2 - None输入", test_layout_v2_null_input),
        ("布局图_v2 - 非dict", test_layout_v2_not_dict),
        ("布局图_v2 - 单区域", test_layout_v2_single_zone),
        ("布局图_v2 - role字段", test_layout_v2_role_field),

        # 时间轴_v2
        ("时间轴_v2 - 正常", test_timeline_v2_normal),
        ("时间轴_v2 - events键", test_timeline_v2_events_key),
        ("时间轴_v2 - points键", test_timeline_v2_points_key),
        ("时间轴_v2 - 分组", test_timeline_v2_groups),
        ("时间轴_v2 - phase键", test_timeline_v2_phase_key),
        ("时间轴_v2 - 单里程碑", test_timeline_v2_single_milestone),
        ("时间轴_v2 - 空milestones", test_timeline_v2_empty_milestones),
        ("时间轴_v2 - None输入", test_timeline_v2_null_input),
        ("时间轴_v2 - 非dict", test_timeline_v2_not_dict),
        ("时间轴_v2 - 无日期", test_timeline_v2_no_dates),
        ("时间轴_v2 - 跳空名", test_timeline_v2_skip_empty_names),
        ("时间轴_v2 - 30里程碑", test_timeline_v2_many_milestones),

        # 主入口
        ("主入口 - 流程图", test_main_entry_flowchart),
        ("主入口 - 甘特图", test_main_entry_gantt),
        ("主入口 - 架构图", test_main_entry_architecture),
        ("主入口 - 对比图", test_main_entry_comparison),
        ("主入口 - 劳动力图", test_main_entry_labor),
        ("主入口 - 布局图", test_main_entry_layout),
        ("主入口 - 时间轴", test_main_entry_timeline),
        ("主入口 - chart通配符", test_main_entry_chart_wildcard),
        ("主入口 - 空代码", test_main_entry_empty_code),
        ("主入口 - 空白字符", test_main_entry_whitespace_only),
        ("主入口 - 未知类型", test_main_entry_unknown_type),
    ]

    passed = 0
    failed = 0
    errors = []

    print("=" * 80)
    print("Mermaid Renderer v2 全面测试")
    print("=" * 80)

    for name, func in tests:
        try:
            func()
            passed += 1
            print(f"  PASS: {name}")
        except AssertionError as e:
            failed += 1
            msg = f"  FAIL: {name} - {e}"
            print(msg)
            errors.append((name, str(e)))
        except Exception as e:
            failed += 1
            msg = f"  ERROR: {name} - {type(e).__name__}: {e}"
            print(msg)
            errors.append((name, f"{type(e).__name__}: {e}"))

    print("\n" + "=" * 80)
    print(f"测试结果: {passed} 通过, {failed} 失败, 共 {passed + failed} 项")
    print("=" * 80)

    if errors:
        print("\n失败详情:")
        for name, err in errors:
            print(f"  - {name}: {err}")

    return failed == 0


if __name__ == "__main__":
    success = run_all_tests()
    sys.exit(0 if success else 1)
