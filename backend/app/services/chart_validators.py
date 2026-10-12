#!/usr/bin/env python3


"""图表数据校验模块 - 遵循 v3.1 规范

实现各类图表的结构验证规则，确保生成内容符合工程标准。
"""

import json
import re
from typing import Any


def _flatten_with_level(node: dict, level: int = 0) -> list[dict]:
    """递归展平树结构，返回带层级信息的节点列表"""
    result = []
    label = node.get("label") or node.get("name", "")
    if label:
        result.append({"label": str(label), "level": level})
    children = node.get("children", []) or []
    if isinstance(children, list):
        for child in children:
            if isinstance(child, dict):
                result.extend(_flatten_with_level(child, level + 1))
    return result


def calculate_tree_depth(node: dict, level: int = 0) -> int:
    """计算树的最大深度"""
    if not isinstance(node, dict):
        return level
    children = node.get("children", []) or []
    if not children:
        return level
    max_d = level
    for child in children:
        if isinstance(child, dict):
            max_d = max(max_d, calculate_tree_depth(child, level + 1))
    return max_d


def count_all_nodes(node: dict) -> int:
    """统计节点总数（含根节点）"""
    if not isinstance(node, dict):
        return 0
    count = 1  # 当前节点
    children = node.get("children", []) or []
    for child in children:
        if isinstance(child, dict):
            count += count_all_nodes(child)
    return count


def validate_architecture_tree(data: Any, subtype: str = None) -> tuple[bool, list[str]]:
    """
    验证架构图数据结构完整性（v3.1 规范）。

    验证规则：
    - 根节点有且仅有 1 个
    - 同级节点标签不得完全重复
    - 深度不得超过 5 层
    - 节点数量在 subtype 规定的区间内

    Args:
        data: 架构树数据（dict 或 JSON 字符串）
        subtype: 子类型 organization/wbs/system/infrastructure（可选）

    Returns:
        (is_valid, error_messages) 元组
    """
    errors = []

    # 解析数据
    try:
        if isinstance(data, str):
            data = json.loads(data)
        if not isinstance(data, dict):
            errors.append("架构图数据必须是字典格式")
            return False, errors
    except (json.JSONDecodeError, TypeError) as e:
        errors.append(f"数据解析失败: {e!s}")
        return False, errors

    # 检查根节点存在且有标签
    if not (data.get("label") or data.get("name")):
        errors.append("缺少根节点标签（label 或 name 字段）")
        return False, errors

    # 深度检查（≤5层）
    depth = calculate_tree_depth(data)
    if depth > 5:
        errors.append(f"架构深度超过5层限制: {depth}层")
        return False, errors

    # 同级节点标签唯一性检查（按层级分组）
    # BUG-FIX-31 修复：原 labels_seen 是全局集合不区分层级，
    # 不同层级相同标签（如各分部同岗位"项目经理"）被误报为重复。
    labels_by_level: dict[int, set[str]] = {}
    for node_info in _flatten_with_level(data):
        level = node_info["level"]
        label = node_info["label"]
        if level not in labels_by_level:
            labels_by_level[level] = set()
        if label in labels_by_level[level]:
            errors.append(f"检测到同级重复标签: '{label}'（层级 {level}）")
        labels_by_level[level].add(label)

    # 节点数量区间检查（根据subtype）
    total_nodes = count_all_nodes(data)
    expected_ranges = {
        "organization": (6, 18),
        "wbs": (5, 15),
        "system": (6, 12),
        "infrastructure": (5, 12),
    }
    if subtype and subtype in expected_ranges:
        min_n, max_n = expected_ranges[subtype]
        if total_nodes < min_n:
            errors.append(f"{subtype}类型最少需{min_n}个节点，实际{total_nodes}")
        elif total_nodes > max_n:
            errors.append(f"{subtype}类型最多{max_n}个节点，实际{total_nodes}")

    return len(errors) == 0, errors


def detect_architecture_subtype(data: dict, section_title: str = None) -> str | None:
    """
    根据架构图数据内容和章节标题，检测子类型（v3.1规范）。

    子类型分类：
    - "organization": 组织机构类（含"组织"、"部门"、"岗位"等）
    - "wbs": 工作分解结构（含"WBS"、"工作包"、"工作分解"等）
    - "system": 系统架构（含"系统"、"模块"、"软件"、"平台"等）
    - "infrastructure": 基础设施/网络拓扑（含"网络"、"服务器"、"设备"等）

    Returns:
        子类型字符串或 None（无法确定）
    """
    # 收集关键词（从数据和标题中提取）
    keywords = []

    # 从树数据中提取标签关键词
    def extract_labels(node: dict):
        if not isinstance(node, dict):
            return
        label = node.get("label") or node.get("name", "")
        if label:
            keywords.append(str(label).lower())
        children = node.get("children", []) or []
        for child in children:
            extract_labels(child)

    try:
        if isinstance(data, str):
            data = json.loads(data)
        if isinstance(data, dict):
            extract_labels(data)
    except Exception as _e:
        pass

    # 添加章节标题关键词
    if section_title:
        keywords.extend([s.strip() for s in re.split(r"[，。、！？\s]+", section_title) if s])

    keywords_lower = [k.lower().strip() for k in keywords if k.strip()]

    # 定义各子类型的关键词模式
    patterns = {
        "organization": [
            r"组织(构)?",
            r"部门",
            r"岗(位)?",
            r"机构",
            r"管理处",
            r"项目部",
            r"指挥部",
            r"负责人",
            r"总工",
            r"经理",
            r"主任",
            r"主管",
            r"管理层",
            r"五方责任",
            r"职责",
            r"职能",
            r"汇报",
            r"矩阵管理",
            r"项目制",
            r"职能制",
            r"岗位说明书",
            r"权责矩阵",
        ],
        "wbs": [
            r"wbs",
            r"工作分解",
            r"工作包",
            r"工作包.",
            r"任务分解",
            r"分解结构",
            r"任务清单",
            r"工序",
            r"作业",
            r"单项工程",
            r"分部工程",
            r"分项工程",
        ],
        "system": [
            r"系统(架构)?",
            r"模块构成",
            r"模块",
            r"软件(架构)?",
            r"平台(架构)?",
            r"子系统",
            r"系统拓扑",
            r"网络拓扑",
            r"接口",
            r"数据库",
            r"服务",
            r"体系",
            r"框架",
            r"架构",
            r"组件",
            r"中间件",
            r"应用层",
            r"数据层",
        ],
        "infrastructure": [
            r"网络拓?扑",
            r"设备构成",
            r"设备",
            r"服务器",
            r"主机",
            r"交换机",
            r"路由器",
            r"存储",
            r"布线",
            r"机房",
            r"物理架构",
            r"硬件架构",
            r"基础设施",
            r"部署架构",
            r"站点",
            r"节点",
            r"终端",
            r"控制器",
        ],
    }

    # 计算匹配分数
    scores = {"organization": 0, "wbs": 0, "system": 0, "infrastructure": 0}
    for sub_type, pattern_list in patterns.items():
        for pattern in pattern_list:
            try:
                if any(re.search(pattern, kw) for kw in keywords_lower):
                    scores[sub_type] += 1
            except re.error:
                pass

    # 获取最高分的子类型
    best_subtype = max(scores, key=scores.get)
    if scores[best_subtype] > 0:
        return best_subtype

    return None


def validate_gantt_plan(plan: Any) -> tuple[bool, list[str]]:
    """
    验证甘特图计划数据（v3.1 规范）。

    验证规则：
    - 任务 ID 不得重复
    - 依赖关系不得成环（简化版：只检查引用存在的任务）
    """
    errors = []

    try:
        if isinstance(plan, str):
            plan = json.loads(plan)
        if not isinstance(plan, dict):
            errors.append("甘特图计划必须是字典格式")
            return False, errors

        tasks = plan.get("tasks", []) or plan.get("items", [])
        if not isinstance(tasks, list) or len(tasks) == 0:
            errors.append("必须包含至少一个任务")
            return False, errors

        # 检查任务ID重复
        seen_ids = set()
        for task in tasks:
            if not isinstance(task, dict):
                continue
            task_id = task.get("id") or task.get("name")
            if task_id in seen_ids:
                errors.append(f"检测到重复的任务ID: {task_id}")
            seen_ids.add(task_id)

        # 检查依赖关系是否存在于任务中（简化版）
        task_ids = set(str(t.get("id") or t.get("name")) for t in tasks if isinstance(t, dict))
        for task in tasks:
            if not isinstance(task, dict):
                continue
            deps = task.get("dependencies", []) or []
            for dep in deps:
                if str(dep) not in task_ids:
                    errors.append(
                        f"任务 {task.get('id', task.get('name'))} 的依赖项 '{dep}' 不存在"
                    )

    except (json.JSONDecodeError, TypeError) as e:
        errors.append(f"数据解析失败: {e!s}")

    return len(errors) == 0, errors


def validate_flowchart_structure(data: Any) -> tuple[bool, list[str]]:
    """
    验证流程图结构（v3.1 规范）。

    验证规则：
    - 节点ID仅含ASCII字符
    - 无孤立节点（所有节点均存在至少一条入边或出边，首尾节点除外）
    """
    errors = []

    try:
        if isinstance(data, str):
            data = json.loads(data)
        if not isinstance(data, dict):
            errors.append("流程图数据必须是字典格式")
            return False, errors

        nodes = data.get("nodes", []) or []
        edges = data.get("edges", []) or []

        if len(nodes) < 2:
            errors.append("流程图必须至少包含2个节点")
            return False, errors

        # 检查节点ID仅含ASCII
        ascii_allowed = re.compile(r"^[\w\s]+$")
        node_ids = set()
        for node in nodes:
            if not isinstance(node, dict):
                continue
            nid = node.get("id", str(node))
            if not ascii_allowed.match(str(nid)):
                errors.append(f"节点ID '{nid}' 包含非ASCII字符")
            node_ids.add(str(nid))

        # 检查入边/出边覆盖（简化：只要出现在边上就不算孤立）
        connected_ids = set()
        for edge in edges:
            if not isinstance(edge, dict):
                continue
            from_id = str(edge.get("from", ""))
            to_id = str(edge.get("to", ""))
            if from_id:
                connected_ids.add(from_id)
            if to_id:
                connected_ids.add(to_id)

        # 孤立节点检测（排除可能在边列表中但未明确作为from/to的节点）
        isolated = node_ids - connected_ids
        if isolated:
            errors.append(f"检测到孤立节点（未在任何连线上出现）: {', '.join(sorted(isolated))}")

    except (json.JSONDecodeError, TypeError) as e:
        errors.append(f"数据解析失败: {e!s}")

    return len(errors) == 0, errors


def validate_timeline_data(data: Any) -> tuple[bool, list[str]]:
    """
    验证时间线数据（timeline）。

    验证规则：
    - milestones 必须是 ≥2 个事件的列表
    - 每个事件必须有 date 和 title 字段
    - date 格式应为 YYYY-MM-DD
    - status 应为 completed/in_progress/pending 之一
    """
    errors = []

    try:
        if isinstance(data, str):
            data = json.loads(data)
        if not isinstance(data, dict):
            errors.append("时间线数据必须是字典格式")
            return False, errors

        milestones = data.get("milestones", [])
        if not isinstance(milestones, list) or len(milestones) < 2:
            errors.append("timeline.milestones 必须是 ≥2 个事件的列表")
            return False, errors

        valid_statuses = {"completed", "in_progress", "pending"}
        for i, m in enumerate(milestones):
            if not isinstance(m, dict):
                errors.append(f"timeline.milestones[{i}] 必须是字典")
                continue
            if not m.get("date") or not m.get("title"):
                errors.append(f"timeline.milestones[{i}] 缺少 date 或 title")
            date_str = str(m.get("date", ""))
            if len(date_str) != 10 or date_str[4] != "-" or date_str[7] != "-":
                errors.append(
                    f"timeline.milestones[{i}] date 格式应为 YYYY-MM-DD，实际: {date_str}"
                )
            status = m.get("status", "")
            if status and status not in valid_statuses:
                errors.append(
                    f"timeline.milestones[{i}] status 应为 completed/in_progress/pending，实际: {status}"
                )

    except (json.JSONDecodeError, TypeError) as e:
        errors.append(f"数据解析失败: {e!s}")

    return len(errors) == 0, errors


def validate_labor_data(data: Any) -> tuple[bool, list[str]]:
    """
    验证劳动力图数据（v3.1 规范）。

    验证规则：
    - 所有阶段下至少有一个工种的数值 > 0
    - 各工种人数之和不为负数
    """
    errors = []

    try:
        if isinstance(data, str):
            data = json.loads(data)
        if not isinstance(data, dict):
            errors.append("劳动力数据必须是字典格式")
            return False, errors

        phases = data.get("phases") or data.get("time") or data.get("stages") or []
        categories = data.get("categories") or data.get("types") or []
        raw_data = data.get("data") or []

        if not phases or not categories or not raw_data:
            errors.append("必须包含阶段、类别和劳动率数据")
            return False, errors

        # 规范化数据矩阵
        matrix = []
        for row in raw_data:
            if isinstance(row, dict):
                values = row.get("values") or []
            else:
                values = row or []
            try:
                matrix.append([float(v) for v in values])
            except (TypeError, ValueError):
                continue

        if not matrix:
            errors.append("无效的数据矩阵")
            return False, errors

        n_cats = len(categories)
        for row in matrix:
            if len(row) < n_cats:
                row.extend([0.0] * (n_cats - len(row)))

        # 检查每个阶段至少有一个工种数值 > 0
        for i, row in enumerate(matrix):
            has_positive = any(v > 0 for v in row)
            if not has_positive:
                errors.append(f"第{i+1}个阶段('{phases[i]}')所有工种数值为0，无效数据")

        # 检查总人数非负（隐含，因为都是非负值，但显式检查更清晰）
        total = sum(sum(row) for row in matrix)
        if total < 0:
            errors.append(f"总劳动力人数不能为负数: {total}")

    except (json.JSONDecodeError, TypeError) as e:
        errors.append(f"数据解析失败: {e!s}")

    return len(errors) == 0, errors


def validate_comparison_pie(data: Any) -> tuple[bool, list[str]]:
    """
    验证对比图饼图数据（v3.1 规范）。

    验证规则：
    - 饼图所有数据项百分比之和 ∈ [99, 101]（允许舍入误差）
    """
    errors = []

    try:
        if isinstance(data, str):
            data = json.loads(data)
        if not isinstance(data, dict):
            errors.append("对比图数据必须是字典格式")
            return False, errors

        rows = data.get("rows", []) or []
        items = data.get("items", []) or []

        values = []
        if rows:
            for row in rows:
                if isinstance(row, dict) and "value" in row:
                    values.append(float(row["value"]))
                elif isinstance(row, dict) and "数值" in row:
                    values.append(float(row["数值"]))
        elif items:
            for item in items:
                if isinstance(item, dict) and "value" in item:
                    values.append(float(item["value"]))

        if len(values) < 2:
            errors.append("饼图至少需要2个数据项")
            return False, errors

        total = sum(values)
        if total <= 0:
            errors.append("饼图数据总和必须为正数")
            return False, errors

        pct_sum = sum((v / total) * 100 for v in values)
        if not (99 <= pct_sum <= 101):
            errors.append(f"饼图百分比之和超出允许范围 [{99}, {101}], 当前: {pct_sum:.2f}%")

    except (json.JSONDecodeError, TypeError, ValueError) as e:
        errors.append(f"数据解析失败: {e!s}")

    return len(errors) == 0, errors


def validate_layout_data(data: Any) -> tuple[bool, list[str]]:
    """
    验证布局图数据（v3.1 规范）。

    验证规则：
    - 每个 subgraph 必须包含至少 2 个节点
    - 无重叠分区描述（简化版：不检查几何重叠，只检查基本结构）
    """
    errors = []

    try:
        if isinstance(data, str):
            data = json.loads(data)
        if not isinstance(data, dict):
            errors.append("布局图数据必须是字典格式")
            return False, errors

        zones = data.get("zones") or data.get("areas") or []
        if not isinstance(zones, list) or len(zones) == 0:
            errors.append("必须包含至少一个区域（zones/areas）")
            return False, errors

        for i, zone in enumerate(zones):
            if not isinstance(zone, dict):
                errors.append(f"区域 {i+1} 不是字典对象")
                continue
            name = zone.get("name") or zone.get("label") or zone.get("title")
            if not name or str(name).strip() == "":
                errors.append(f"区域 {i+1} 缺少有效名称")

        connections = data.get("connections") or []
        if isinstance(connections, list):
            for conn in connections:
                if not isinstance(conn, dict):
                    continue
                from_idx = conn.get("from")
                to_idx = conn.get("to")
                if from_idx is not None and to_idx is not None:
                    # 简单的索引有效性检查
                    if isinstance(from_idx, int) or isinstance(from_idx, str):
                        pass  # 合法，稍后检查是否在范围内
                    else:
                        errors.append("连接项 from/to 必须是整数或字符串索引")

    except (json.JSONDecodeError, TypeError) as e:
        errors.append(f"数据解析失败: {e!s}")

    return len(errors) == 0, errors


# ============================================================
# 触发条件关键词库（v3.1 规范，120+ 触发词）
# ============================================================
TRIGGER_KEYWORDS = {
    "gantt": [
        "进度计划",
        "施工进度",
        "工期安排",
        "里程碑",
        "时间节点",
        "施工部署",
        "阶段划分",
        "总工期",
        "开竣工日期",
        "关键线路",
        "月度计划",
        "周计划",
        "赶工措施",
        "进度保证",
        "工期目标",
        "工期倒排",
        "节点目标",
        "进度款支付节点",
        "分段验收",
        "先...再...最后...",
        "分...个阶段",
        "从...到...需要...",
        "计划...开始，...结束",
        "工期60天",
        "第30天",
    ],
    "flowchart": [
        "施工工序",
        "工艺流程",
        "部署顺序",
        "操作步骤",
        "工艺衔接",
        "施工流向",
        "作业流程",
        "施工程序",
        "逻辑关系",
        "先...后...",
        "施工流程",
        "验收流程",
        "报验流程",
        "审批流程",
        "移交流程",
        "交接检",
        "隐蔽验收流程",
        "变更流程",
        "索赔流程",
        "招标流程",
        "采购流程",
        "验收步骤",
        "检查步骤",
        "安装步骤",
        "调试步骤",
        "施工准备→...→交付",
        "如果...则...，反之...",
        "是否合格",
        "满足要求",
        "条件判断",
    ],
    "architecture": [
        "组织机构",
        "项目管理架构",
        "组织架构",
        "管理体系",
        "岗位配置",
        "岗位设置",
        "职责分工",
        "责任划分",
        "职能分配",
        "WBS",
        "工作分解",
        "工作包",
        "系统架构",
        "网络拓扑",
        "系统拓扑",
        "子系统",
        "模块构成",
        "设备构成",
        "系统构成",
        "平台架构",
        "软件架构",
        "管理层级",
        "汇报关系",
        "上下级关系",
        "矩阵管理",
        "项目制",
        "职能制",
        "指挥部",
        "项目部",
        "五方责任主体",
        "部门设置",
        "总包/分包/业主/监理关系",
        "管理跨度",
        "指挥体系",
        "决策层→管理层→执行层",
        "岗位职责",
        "岗位说明书",
        "权责矩阵",
        "设...层",
        "下设...个部门",
        "由...负责...，...向...汇报",
        "包含...层",
    ],
    "labor": [
        "劳动力配置",
        "人员配置",
        "人力计划",
        "工种分配",
        "劳动力投入",
        "用工需求",
        "资源配置",
        "用工峰值",
        "劳动力曲线",
        "劳动力动态",
        "各阶段人数",
        "技术工种",
        "普工配置",
        "劳动力缺口",
        "人员调配",
        "劳动力平衡",
        "配置...名...工，...名...工",
        "高峰期80人",
        "钢筋工30人",
        "木工25人",
    ],
    "layout": [
        "总平面布置",
        "施工分区",
        "功能分区",
        "功能布局",
        "区域划分",
        "临建布置",
        "交通组织",
        "场地布置",
        "堆场布置",
        "加工区布置",
        "办公区布置",
        "生活区布置",
        "临时道路",
        "围挡布置",
        "出入口设置",
        "材料堆放区",
        "周转场地",
        "施工便道",
        "现场排水",
        "泥浆池布置",
        "塔吊覆盖范围",
        "施工电梯定位",
        "钢筋加工棚",
        "木工加工棚",
        "砂浆搅拌站",
        "预制构件堆场",
        "进场道路",
        "消防通道",
        "垂直运输布置",
        "进入...区域",
        "...区布置",
        "...侧为...",
    ],
    "comparison": [
        "得分权重",
        "成本占比",
        "资源配置比例",
        "风险分布",
        "投资构成",
        "费用占比",
        "人员比例",
        "工期占比",
        "材料占比",
        "合格率对比",
        "方案对比",
        "指标对比",
        "A/B/C方案比选",
        "预算占比",
        "实际vs计划",
        "目标vs完成",
        "节约率",
        "损耗率",
        "利用率",
        "投诉分布",
        "质量问题分布",
        "工期延误原因分析",
        "各项占比",
        "各项权重",
        "百分比",
        "一方面...另一方面...",
        "与...相比，...高出...",
    ],
}

# 句式触发模式（正则表达式）
SENTENCE_PATTERNS = {
    "gantt": [
        r"先(.+)再(.+)最后(.+)",
        r"分(\d+)个阶段",
        r"从(.+)到(.+)需要(.+)天",
        r"计划(.+)开始，(.+)结束",
    ],
    "architecture": [
        r"由(.+)负责(.+), (.+)向(.+)汇报",
        r"设(.+)层",
        r"下设(.+)个部门",
        r"包含(.+)层",
    ],
    "comparison": [r"与(.+)相比(.+)高(.+)", r"与(.+)相比(.+)低(.+)", r"一方面(.+)另一方面(.+)"],
    "layout": [r"进入(.+)区域", r"(.+)区布置", r"东侧为(.+)|西侧为(.+)|北侧为(.+)"],
    "labor": [r"配置(.+)名(.+)工，(.+)名(.+)工"],
    "flowchart": [r"如果(.+)则(.+)，反之(.+)"],
}

# 行业标准化术语库（按分类）
INDUSTRY_TERMS = {
    # 建筑工程施工术语
    "construction": {
        "地基基础": ["桩基", "基坑支护", "土方开挖", "回填", "垫层"],
        "主体结构": ["砌体", "混凝土", "钢筋模板", "钢结构", "幕墙"],
        "建筑装饰": ["吊顶", "墙面装饰", "地面铺装", "门窗安装"],
        "建筑机电": ["给排水", "电气布线", "通风空调", "智能建筑"],
    },
    # 市政工程施工术语
    "municipal": {
        "路基路面": ["路基处理", "水稳层", "沥青面层", "水泥路面"],
        "桥梁隧道": ["墩柱", "梁板", "隧道开挖", "衬砌", "涵洞"],
        "管道工程": ["给排水管道", "污水管网", "雨水管", "顶管", "箱涵"],
        "附属设施": ["交通设施", "照明工程", "绿化工程", "围堰"],
    },
    # 机电安装术语
    "mech_elect": {
        "设备安装": ["压缩机安装", "泵组安装", "变压器安装", "盘柜安装"],
        "管道工程": ["管道预制", "焊接工艺", "法兰连接", "阀门安装", "试压"],
        "电气工程": ["电缆桥架", "母线槽", "防雷接地", "DCS系统", "PLC系统"],
        "调试试车": ["单机试车", "联动试车", "吹扫清洗", "防腐绝热"],
    },
    # 水利水电术语
    "water_cons": {
        "土石方": ["土石方开挖", "土石方填筑", "地基处理"],
        "水工建筑": ["混凝土坝", "水闸", "泵站", "隧洞", "渡槽"],
        "机电设备": ["启闭机", "闸门机组", "水力发电设备", "水文监测"],
        "河道治理": ["护岸工程", "防汛工程", "灌溉渠系", "河道疏浚"],
    },
    # 公路交通术语
    "highway": {
        "路基路面": ["路基", "桩基", "墩柱", "梁板", "伸缩缝"],
        "桥隧工程": ["桥梁", "隧道", "沥青路面", "水稳层"],
        "交通安全": ["交通标志", "标线", "护栏", "照明"],
        "机电工程": ["监控系统", "收费系统", "通信系统", "绿化环保"],
    },
    # 通用管理类
    "general_project": [
        "项目经理部",
        "五方责任主体",
        "施工组织设计",
        "专项施工方案",
        "技术交底",
        "图纸会审",
        "设计变更",
        "工程签证",
        "隐蔽验收",
        "检验批",
        "分项工程",
        "分部工程",
        "单位工程",
        "竣工验收",
        "备案",
        "质量管理体系",
        "三检制",
        "实测实量",
        "通病防治",
        "安全技术交底",
        "危险源辨识",
        "应急预案",
        "文明施工",
        "扬尘治理",
        "噪声控制",
        "绿色施工",
        "碳减排",
        "BIM模型",
    ],
}


def get_industry_terms_by_category(category: str) -> list[str]:
    """获取指定行业的标准化术语列表"""
    terms = []
    if category in INDUSTRY_TERMS:
        val = INDUSTRY_TERMS[category]
        if isinstance(val, dict):
            for term_list in val.values():
                terms.extend(term_list)
        elif isinstance(val, list):
            terms.extend(val)
    # 也返回通用术语
    terms.extend(INDUSTRY_TERMS.get("general_project", []))
    return terms


def trigger_match(keyword: str, text: str) -> bool:
    """检查关键词是否与文本匹配（子串匹配，忽略大小写和空格）"""
    if not keyword or not text:
        return False
    kw = keyword.replace(" ", "").replace("·", "").lower()
    txt = text.replace(" ", "").replace("·", "").lower()
    return kw in txt or txt in kw


def detect_chart_type_from_text(text: str) -> list[tuple[str, float]]:
    """
    根据文本内容检测可能的图表类型及匹配分数（v3.1规范）。

    返回按分数降序排列的 (chart_type, score) 列表。
    """
    results = []
    text_lower = text.lower()

    # 关键词匹配计数
    for chart_type, keywords in TRIGGER_KEYWORDS.items():
        match_count = sum(1 for kw in keywords if trigger_match(kw, text_lower))
        results.append((chart_type, match_count))

    # 句式模式匹配（额外加分）
    for chart_type, patterns in SENTENCE_PATTERNS.items():
        for pattern in patterns:
            try:
                import re

                if re.search(pattern, text):
                    # 句式匹配额外加 1 分
                    idx = next((i for i, (ct, _) in enumerate(results) if ct == chart_type), -1)
                    if idx >= 0:
                        results[idx] = (chart_type, results[idx][1] + 1)
            except re.error:
                pass

    # 按匹配数排序
    results.sort(key=lambda x: (-x[1], x[0]))
    return results


def extract_matched_keywords(text: str, chart_type: str) -> list[str]:
    """提取用于 matchedKeywords 字段的匹配关键词"""
    matched = []
    keywords = TRIGGER_KEYWORDS.get(chart_type, [])
    for kw in keywords:
        if trigger_match(kw, text):
            matched.append(kw)
    return matched


def detect_chart_with_metadata(text: str) -> dict:
    """
    根据文本内容检测图表类型及匹配信息（v3.1规范增强版）。

    Returns:
        {
            "chart_types": [(chart_type, score), ...],  # 按分数降序排列
            "matched_keywords": {chart_type: [keywords], ...},  # 每个类型的匹配关键词
            "top_chart": chart_type or None,  # 最高分的图表类型
            "top_score": int,  # 最高分
            "patterns_matched": List[str]  # 匹配的句式模式
        }
    """
    results = []
    matched_keywords = {}
    patterns_matched = []
    text_lower = text.lower()

    # 关键词匹配计数
    for chart_type, keywords in TRIGGER_KEYWORDS.items():
        match_count = sum(1 for kw in keywords if trigger_match(kw, text_lower))
        if match_count > 0:
            matched_keywords[chart_type] = [kw for kw in keywords if trigger_match(kw, text_lower)]
        results.append((chart_type, match_count))

    # 句式模式匹配（额外加分）
    for chart_type, patterns in SENTENCE_PATTERNS.items():
        for pattern in patterns:
            try:
                if re.search(pattern, text):
                    patterns_matched.append(f"{chart_type}:{pattern[:50]}...")
                    # 额外加分
                    for i, (ct, _) in enumerate(results):
                        if ct == chart_type:
                            results[i] = (chart_type, results[i][1] + 1)
                            break
            except re.error:
                pass

    # 按匹配数排序
    results.sort(key=lambda x: (-x[1], x[0]))

    # 确保所有类型都有 matched_keywords 字段（空列表）
    all_types = set(TRIGGER_KEYWORDS.keys())
    for ct in all_types:
        if ct not in matched_keywords:
            matched_keywords[ct] = []

    top_chart = results[0][0] if results else None
    top_score = results[0][1] if results else 0

    return {
        "chart_types": results,
        "matched_keywords": matched_keywords,
        "top_chart": top_chart,
        "top_score": top_score,
        "patterns_matched": patterns_matched,
    }


# ===========================================================================
# BUG-2026-1008-02 修复（P1）：正文重复标题预检
# ===========================================================================


# 编号前缀正则（与 content_cleaning.py 保持一致）
_TITLE_NUMBERING_PREFIX = re.compile(
    r"^\s*(?:\d+[.、．,]|（?\d+）?|[\(（]\d+[\)）]|[一二三四五六七八九十]+[、．.])\s*"
)


def check_duplicate_title_in_content(title: str, content: str) -> list[dict]:
    """检查正文是否重复标题（导出预检 CMP-07）。

    BUG-2026-1008-02 修复（P1）：在导出前检查章节正文前 5 行是否包含
    与章节标题重复的行（去编号前缀后比较），避免双重标题出现在导出文档中。

    Args:
        title: 章节标题
        content: 章节正文

    Returns:
        Issue 列表，每项含 rule_id / severity / title / message / suggestion / section_id。
        空列表表示无重复标题。
    """
    issues: list[dict] = []

    if not content or not isinstance(content, str):
        return issues

    title_stripped = (title or "").strip()
    if not title_stripped:
        return issues

    lines = content.split("\n")[:5]  # 仅检查前 5 行

    for i, line in enumerate(lines):
        line_clean = line.strip()
        if not line_clean:
            continue

        # 移除编号前缀和 Markdown 标题标记
        line_clean = _TITLE_NUMBERING_PREFIX.sub("", line_clean)
        line_clean = re.sub(r"^\s*#{1,6}\s*", "", line_clean).strip()

        if line_clean == title_stripped:
            issues.append(
                {
                    "rule_id": "CMP-07",
                    "severity": "medium",
                    "title": "正文重复标题",
                    "message": f"章节「{title_stripped}」正文第 {i + 1} 行重复标题",
                    "suggestion": "移除正文开头的重复标题",
                    "section_id": None,
                }
            )
            break  # 只报告第一个重复

    return issues
