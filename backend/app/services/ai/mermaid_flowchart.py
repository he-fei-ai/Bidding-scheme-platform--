"""Flowchart 图表渲染器。

由 scripts/_split_mermaid.py 从原 mermaid_renderer.py 自动拆分而来。
所有公共工具从 .mermaid_common 导入。
"""
from __future__ import annotations

import logging
from io import BytesIO

from PIL import Image, ImageDraw

from .mermaid_common import (
    _build_gantt_marks,
    _draw_text_center,
    _fit_text_with_ellipsis,
    _image_font,
    _image_to_stream,
    _render_hq,
    _text_height,
    _text_width,
    _wrap_text,
)


import re

logger = logging.getLogger(__name__)



# 决策节点关键词（中英双语启发式判定）
_DECISION_KEYWORDS = (
    "?",
    "？",
    "是否",
    "判",
    "审核",
    "审查",
    "审批",
    "是否通过",
    "否",
    "通过",
    "成功",
    "失败",
    "满足",
    "达成",
    "符合",
    "合格",
    "不合格",
    "正确",
    "错误",
    "Check",
    "Pass",
    "Fail",
    "Yes",
    "No",
    "Approve",
    "Verify",
    "Confirm",
)


def _parse_mermaid_flowchart(mermaid_code: str) -> list[str] | None:
    """解析 Mermaid 流程图代码，提取节点标签列表"""
    nodes = []
    lines = mermaid_code.strip().split("\n")
    for line in lines:
        line = line.strip()
        if not line or line.startswith("graph") or line.startswith("flowchart"):
            continue
        node_match = re.match(r"^\w+\s*\[([^\]]*)\]", line)
        if node_match:
            label = node_match.group(1).strip()
            if label:
                nodes.append(label)
            continue
        node_match = re.match(r"^\w+\s*\{([^}]*)\}", line)
        if node_match:
            label = node_match.group(1).strip()
            if label:
                nodes.append(label)
            continue
        node_match = re.match(r"^\w+\s*\(\(([^)]*)\)\)", line)
        if node_match:
            label = node_match.group(1).strip()
            if label:
                nodes.append(label)
            continue
    return nodes if len(nodes) >= 2 else None

def _is_decision_label(label: str) -> bool:
    """根据标签内容启发式判断是否为决策节点（菱形）。"""
    if not label:
        return False
    label = label.strip()
    if "?" in label or "？" in label:
        return True
    for kw in _DECISION_KEYWORDS:
        if kw in label:
            return True
    return False

def _extract_mermaid_node_label(nid: str) -> str | None:
    """从 Mermaid 节点定义中提取可读标签。

    支持以下语法：
      - A1["测量放线"]      -> "测量放线"
      - A1["水压试验"]      -> "水压试验"
      - A1(测量放线)        -> "测量放线"
      - A1{判定}            -> "判定"
      - A1(("圆形节点"))    -> "圆形节点"
      - A1>"非对称"]        -> "非对称"

    Args:
        nid: Mermaid 节点 ID 字符串（可能含标签）

    Returns:
        提取后的标签文本；若无法提取则返回 None
    """
    if not nid:
        return None

    # 1) 圆角节点 A1(["label"]) - 商业级优先
    m = re.match(r"^[A-Za-z_]\w*\s*\(\s*\[(.*)\]\s*\)$", nid)
    if m:
        return m.group(1).strip().strip('"').strip("'").strip()

    # 2) 圆形节点 A1(("label"))
    m = re.match(r"^[A-Za-z_]\w*\s*\(\((.*)\)\)$", nid)
    if m:
        return m.group(1).strip().strip('"').strip("'").strip()

    # 3) 矩形节点 A1["label"] / A1[label]
    m = re.match(r"^[A-Za-z_]\w*\s*\[(.*)\]$", nid)
    if m:
        return m.group(1).strip().strip('"').strip("'").strip()

    # 4) 菱形节点 A1{"label"}
    m = re.match(r"^[A-Za-z_]\w*\s*\{(.*)\}$", nid)
    if m:
        return m.group(1).strip().strip('"').strip("'").strip()

    # 5) 圆角矩形 A1("label")
    m = re.match(r"^[A-Za-z_]\w*\s*\((.*)\)$", nid)
    if m:
        return m.group(1).strip().strip('"').strip("'").strip()

    # 6) 非对称 A1>"label"]
    m = re.match(r"^[A-Za-z_]\w*\s*>\s*\"(.*)\"\s*\]$", nid)
    if m:
        return m.group(1).strip().strip('"').strip("'").strip()

    # 7) 并行/平行 A1[/"label"/]
    m = re.match(r"^[A-Za-z_]\w*\s*/\s*\"(.*)\"\s*/\]$", nid)
    if m:
        return m.group(1).strip().strip('"').strip("'").strip()

    return None

def _register_node_if_match(line: str, nodes_map: dict) -> None:
    """尝试将一行 Mermaid 代码识别为独立节点定义并注册到 nodes_map。

    支持的节点定义语法（Mermaid flowchart 标准）：
      N1["矩形标签"]    N1("圆角标签")    N1{{"六边形"}}
      N1{"菱形标签"}    N1(("圆形"))      N1>"非对称"]
      N1[/"平行左"/]    N1(["子图"])

    Args:
        line: 已 strip 的 Mermaid 行
        nodes_map: 待填充的节点字典（会被原地修改）
    """
    if not line or "-->" in line:
        return
    # 快速过滤：不含括号/方括号/大括号的肯定不是节点定义
    if not any(c in line for c in "[{("):
        return

    # 尝试提取标签（如果没有标签，说明不是节点定义行）
    label = _extract_mermaid_node_label(line)
    if not label:
        return
    # 连线语法残留（如 `N1["CheckBIM -- \"通过\""]`）不是节点定义，跳过注册，
    # 否则会渲染出 `CheckBIM -- "通过"` 这样的垃圾节点。
    if _looks_like_edge_artifact(label):
        return

    plain_id = _strip_mermaid_node_brackets(line)
    if not plain_id:
        return
    # ID 必须是合法 Mermaid 节点 ID（字母/下划线开头）
    if not re.match(r"^[A-Za-z_]\w*$", plain_id):
        return

    # 注册：用带标签的完整字符串作为 key（与连线行里的 _resolve_id 兼容）
    nodes_map[line] = {
        "id": plain_id,
        "label": label,
        "type": "decision" if ("{" in line) or _is_decision_label(label) else "process",
        "shape": "diamond" if ("{" in line) else "rect",
    }

def _strip_mermaid_node_brackets(nid: str) -> str:
    """从 Mermaid 节点定义中提取纯 ID 名称（去除方括号/圆括号/大括号包裹的标签部分）。

    示例：
      - A1["测量放线"]   -> A1
      - A1(测量放线)     -> A1
      - A1{"判定"}      -> A1
      - A1(("圆形"))    -> A1
      - A1              -> A1

    Args:
        nid: Mermaid 节点 ID 字符串

    Returns:
        纯 ID 名称（不含标签包裹符号）
    """
    if not nid:
        return nid
    # 匹配 ID 起始：字母/下划线 + 字母数字下划线
    m = re.match(r"^([A-Za-z_]\w*)\s*[\[\(\{]", nid)
    if m:
        return m.group(1)
    return nid

def _brackets_balanced(text: str) -> bool:
    """判断字符串中的 `[`/`{`/`(` 是否与 `]`/`}`/`)` 一一配对且顺序合法。

    用于区分「真正的节点定义」与「被括号包裹的边文本残留」。
    例：`A[工期 2026-09-22` 不平衡（左侧被切断的节点标签），
    `A[工期 2026-09-22 -- 2026-12-15]` 平衡（完整节点）。
    """
    depth = 0
    for ch in text:
        if ch in "[({":
            depth += 1
        elif ch in "])}":
            depth -= 1
            if depth < 0:
                return False
    return depth == 0


def _clean_edge_label(label: str | None) -> str | None:
    """清理边标签文本：修正 JSON 风格转义、去除成对的包裹引号。

    AI 常把边标签写成 `-->|"通过"|` 或 `-- \"通过\" -->`，
    直接渲染会把引号/反斜杠一并画出来（图中显示为 `"通过"`）。
    空白结果返回 None（表示无边标签）。
    """
    if not label:
        return None
    text = label.strip().replace('\\"', '"')
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ('"', "'"):
        text = text[1:-1].strip()
    return text or None


# 明显的连线语法残留（不是节点标签）：
#   - `CheckBIM -- "通过"`   （legacy 边文本写法被误当节点）
#   - `D1 -- 通过`           （同上，无引号）
#   - `A ==> 通过` / `A -.- 通过`
# 注意：`工期 2026-09-22 -- 2026-12-15` 这类以「中文/日期」开头的
# 合法节点标签不会被命中（可选前缀仅允许 ASCII/中文单词，其后必须紧跟连接符）。
_EDGE_ARTIFACT_RE = re.compile(
    r"^(?:[A-Za-z0-9_\u4e00-\u9fff]+\s+)?(?:-->|===?->|==+>|-\.->)\s+\S"
)


def _looks_like_edge_artifact(label: str | None) -> bool:
    """判断标签文本是否是连线语法残留（不应被当作节点渲染）。"""
    if not label:
        return True
    return bool(_EDGE_ARTIFACT_RE.match(label.strip()))


def _split_mermaid_edge(line: str) -> tuple[str, str | None, str] | None:
    """解析 Mermaid 边定义：from --[|label|]--> to（支持带中英文标签和方括号节点 ID）

    Returns:
        (from_id, edge_label or None, to_id)，解析失败返回 None
    """
    line = line.strip()
    if not line or "-->" not in line:
        return None

    # 拆分箭头位置（第一个 -->）
    arrow_idx = line.find("-->")
    if arrow_idx < 0:
        return None

    left = line[:arrow_idx].strip()
    right = line[arrow_idx + 3 :].strip()

    edge_label: str | None = None

    # 标签在左侧尾部:  A -->|label|  ->  左侧可能是 "A -->|label|" 但已经被分割
    # 标签在右侧首部:  -->|label| B
    label_m = re.match(r"^\s*\|([^|]*)\|\s*(.*)$", right)
    if label_m:
        edge_label = _clean_edge_label(label_m.group(1))
        right = label_m.group(2).strip()
    else:
        # 兼容 A -->|label| 之后直接接节点（label 在右半部分首部）
        label_m = re.match(r"^\s*\|([^|]*)\|\s*$", right)
        if label_m:
            edge_label = _clean_edge_label(label_m.group(1))
            right = ""
        # 清理左侧可能存在的连线样式后缀
        left = re.sub(r"\s+---\s*$", "", left)

    # 兼容 legacy 边文本残留：`A -- "通过" --> B`（未先经过 Sanitizer 时的兜底）。
    # 要求 src 括号平衡，避免把 `A[工期 2026-09-22 -- 2026-12-15]` 这类
    # 完整节点标签误拆成「节点 + 边标签」。
    left_legacy = re.match(r"^(?P<src>.+?)\s+(?:--|==>|-\.-)\s+(?P<label>\S.*)$", left)
    if left_legacy and _brackets_balanced(left_legacy.group("src")):
        legacy_label = _clean_edge_label(left_legacy.group("label"))
        if legacy_label and not edge_label:
            edge_label = legacy_label
        left = left_legacy.group("src").strip()

    # 清理左侧可能存在的 |...| 形式的标签（少数语法）
    label_m_left = re.match(r"^(.*?)\s*\|([^|]*)\|\s*$", left)
    if label_m_left and not edge_label:
        left = label_m_left.group(1).strip()
        edge_label = _clean_edge_label(label_m_left.group(2))

    from_id = left
    to_id = right

    if not from_id or not to_id:
        return None

    # 兜底：端点如果仍是连线语法残留（如 `CheckBIM -- "通过"`），说明该行无法
    # 安全解析——放弃该条连线，避免注册出垃圾节点把整张图的拓扑带偏。
    if _looks_like_edge_artifact(from_id) or _looks_like_edge_artifact(to_id):
        return None

    return from_id, edge_label, to_id

def _parse_flowchart_structure(mermaid_code: str) -> dict:
    """解析 Mermaid 流程图，提取节点、连线、决策分支、并行/汇合结构。

    Returns:
        dict: {
            "direction": "TD" | "LR",
            "nodes": [{"id": str, "label": str, "type": "process"|"decision"|"start"|"end"}],
            "edges": [{"from": str, "to": str, "label": str|None}],
        }
    """
    direction = "TD"
    nodes_map: dict[str, dict] = {}
    edges: list[dict] = []

    for raw in mermaid_code.split("\n"):
        line = raw.strip()
        if not line:
            continue
        if line.startswith("flowchart") or line.startswith("graph"):
            tokens = line.split()
            if len(tokens) >= 2 and tokens[1].upper() in ("TD", "TB", "LR", "RL", "BT"):
                direction = tokens[1].upper()
                if direction == "TB":
                    direction = "TD"
            continue
        # BUG 修复：end 仅在精确匹配（subgraph 结束关键字）时跳过。
        # 旧逻辑 line.startswith("end") 会吞掉独立节点定义行 end(["结束"])，
        # 导致 end 节点标签丢失、渲染时显示英文 "end"。
        if line in ("end", "end;"):
            continue
        if "-->" not in line:
            # 非连线行：跳过纯关键字行（subgraph/title/classDef/class/linkStyle/style）。
            # 注意 title/class/style 等过滤必须带后缀（空格/冒号），
            # 避免误吞同名节点定义行（如 title["标题"]、classNode["分类"]）。
            if (
                line.startswith("subgraph")
                or line.startswith("title ")
                or line.startswith("title:")
                or line.startswith("classDef")
                or line.startswith("class ")
                or line.startswith("linkStyle")
                or line.startswith("style ")
            ):
                continue
            # === 修复：独立节点定义行注册 ===
            # Mermaid 标准格式中节点定义（如 N1["施工准备"]）和连线（N1 --> N2）是分开的。
            # 原先代码只处理含 "-->" 的连线行，导致节点标签从未被注册进 nodes_map，
            # 后续纯 ID 连线无法关联到带标签定义，label 就回退成了纯 ID（N1, N2...）。
            # 现在在跳过 --> 之前先识别独立节点定义行并注册。
            _register_node_if_match(line, nodes_map)
            continue

        # 处理连线（支持带标签 -->|label| 与不带标签 -->；支持节点 ID 中带方括号标签）
        edge_info = _split_mermaid_edge(line)
        if not edge_info:
            continue
        from_id_raw, edge_label, to_id_raw = edge_info
        if not from_id_raw or not to_id_raw:
            continue

        # 规范化节点 ID：纯 ID 名称（如 B1）合并到带标签节点（B1["xxx"]）
        from_label = _extract_mermaid_node_label(from_id_raw)
        to_label = _extract_mermaid_node_label(to_id_raw)
        _strip_mermaid_node_brackets(from_id_raw)
        _strip_mermaid_node_brackets(to_id_raw)

        # 查找已存在节点：纯 ID 形式 vs 带标签形式
        def _resolve_id(nid_raw: str, label: str | None) -> str:
            if label:
                return nid_raw  # 带标签的就是它的 ID
            # 纯 ID，查找已存在的带标签节点
            plain = _strip_mermaid_node_brackets(nid_raw)
            for existing in nodes_map:
                if _strip_mermaid_node_brackets(existing) == plain and (
                    existing != plain or "[" in existing or "{" in existing or "(" in existing
                ):
                    return existing
            return nid_raw

        from_id = _resolve_id(from_id_raw, from_label)
        to_id = _resolve_id(to_id_raw, to_label)

        for nid in (from_id, to_id):
            if nid not in nodes_map:
                label = _extract_mermaid_node_label(nid)
                if label and _looks_like_edge_artifact(label):
                    continue  # 连线语法残留（如 `A -- "通过"`）不注册为节点
                if label:
                    nodes_map[nid] = {
                        "id": nid,
                        "label": label,
                        "type": "decision"
                        if ("{" in nid) or _is_decision_label(label)
                        else "process",
                        "shape": "diamond" if ("{" in nid) else "rect",
                    }
                else:
                    nodes_map[nid] = {"id": nid, "label": nid, "type": "process", "shape": "rect"}

        edges.append({"from": from_id, "to": to_id, "label": edge_label})

    # ENHANCE 2026-10-11: 纯 ID 规范化（chart_validators 兼容）
    #
    # _resolve_id / _register_node_if_match 用带方括号的完整 Mermaid 字符串
    # （如 "A[施工准备]"）作为 nodes_map 的 key——这是渲染链路的设计：渲染器
    # 需要用完整 Mermaid 语句来匹配和查找节点。但 chart_validators
    # validate_flowchart_structure 期望：
    #   - nodes[].id = 纯 ASCII（"A"）
    #   - nodes[].label = 中文标签（"施工准备"）
    #   - edges[].from/to = 纯 ASCII，与 nodes[].id 对齐
    #
    # 渲染链路（_render_flowchart_image_v2）和校验链路（chart_validators）
    # 在返回格式上各有偏好，所以在函数末尾统一做一次规范化：
    # nodes[].id / edges[].from / edges[].to 全部 strip 成纯 ID，
    # nodes[].label 保持中文不变。
    plain_nodes: list[dict] = []
    for n in nodes_map.values():
        plain_nodes.append({
            **n,
            "id": _strip_mermaid_node_brackets(n["id"]),
        })
    plain_edges = []
    for e in edges:
        plain_edges.append({
            **e,
            "from": _strip_mermaid_node_brackets(e["from"]),
            "to": _strip_mermaid_node_brackets(e["to"]),
        })

    return {
        "direction": direction,
        "nodes": plain_nodes,
        "edges": plain_edges,
    }

@_render_hq
def _render_flowchart_image_v2(mermaid_code: str) -> BytesIO | None:
    """商业级流程图渲染器 v2

    特性：
    - 严格区分决策菱形与流程矩形
    - 自动判断 TD / LR 布局
    - 带标签的分支连线（-->|label|）
    - 扁平化配色：所有节点统一使用淡紫填充 #EDE9FE、丁香紫边框 #9B8BE0、深炭灰文字 #2D2D3A，直角矩形无阴影
    - 自动避让连线，箭头清晰
    - 增强 v3.0: 使用 @_render_hq 实现 2.5x 超采样抗锯齿，300 DPI 输出
    """
    if not mermaid_code:
        return None

    parsed = _parse_flowchart_structure(mermaid_code)
    nodes = parsed["nodes"]
    edges = parsed["edges"]
    if len(nodes) < 2 or not edges:
        return None
    logger.info(
        "[PIL v2渲染] 流程图渲染启动: 节点数=%d 连线数=%d 方向=%s",
        len(nodes),
        len(edges),
        parsed["direction"],
    )

    direction = parsed["direction"]
    decision_ids = {n["id"] for n in nodes if n["type"] == "decision"}

    # ---- 尺寸参数 ----
    padding = 40
    node_w = 180
    node_h = 56
    diamond_w = 200
    diamond_h = 200          # 菱形取正方形：文字可用区 = min(w, h)/2
    row_gap = 32
    col_gap = 60
    title_h = 60
    caption_h = 32

    # ---- 决策菱形自适应尺寸（BUG-2026-1011-02）----
    # 菱形是正方形旋转 45°，文字实际可用区域只有内接矩形（宽 = 高 = min(w, h)/2）。
    # 原来 diamond_h=80 远小于 diamond_w=200，"BIM管线综合碰撞检查" 这类标签
    # 必然溢出斜边被裁切。这里按最长决策标签反推所需边长；col_w/row_h 由
    # max(node_w, diamond_w) 推导，边长增大时布局会自动让位，不会压到相邻节点。
    scale = 2.5  # 与下方 _render_hq 超采样倍率保持一致
    decision_labels = [str(n.get("label", "")) for n in nodes if n["id"] in decision_ids]
    if decision_labels:
        probe_draw = ImageDraw.Draw(Image.new("RGB", (8, 8), "white"))
        probe_font = _image_font(int(15 * scale))          # 与 font_node 同字号
        cjk_w = max(1.0, _text_width(probe_draw, "国", probe_font))
        line_h = max(16.0, _text_height(probe_draw, "国", probe_font) + 4)
        pad = 20.0 * scale                                  # 文字与斜边的留白
        longest = max(len(l) for l in decision_labels)
        best_side = None
        for n_lines in range(1, 5):
            chars = max(1, -(-longest // n_lines))          # 向上取整
            side = 2 * max(chars * cjk_w, n_lines * line_h) + 2 * pad
            if best_side is None or side < best_side:
                best_side = side
        if best_side is not None:
            need = best_side / scale
            if need > diamond_w or need > diamond_h:
                diamond_w = diamond_h = max(diamond_w, diamond_h, need)

    # ---- 自动布局（基于 BFS 层级）----
    # 构建邻接表
    adj: dict[str, list[str]] = {n["id"]: [] for n in nodes}
    in_degree: dict[str, int] = {n["id"]: 0 for n in nodes}
    for e in edges:
        if e["from"] in adj and e["to"] in adj:
            adj[e["from"]].append(e["to"])
            in_degree[e["to"]] = in_degree.get(e["to"], 0) + 1

    # BFS 计算层级（处理简单图，支持多个起点；带环路保护）
    levels: dict[str, int] = {}
    queue = [nid for nid, d in in_degree.items() if d == 0]
    if not queue:
        queue = [nodes[0]["id"]]
    for nid in queue:
        levels[nid] = 0
    head = 0
    max_iterations = len(nodes) * len(nodes) + 100  # 环路保护：避免无限循环
    iterations = 0
    while head < len(queue):
        cur = queue[head]
        head += 1
        iterations += 1
        if iterations > max_iterations:
            # 环路保护：剩余节点直接分配层级
            for n in nodes:
                if n["id"] not in levels:
                    levels[n["id"]] = max(levels.values()) + 1 if levels else 0
            break
        for nb in adj.get(cur, []):
            if nb not in levels:
                levels[nb] = levels[cur] + 1
                queue.append(nb)
            # 不再更新已存在节点的层级，避免循环导致无限传播

    # 同层级归组
    if not levels:
        levels = {n["id"]: 0 for n in nodes}
    max_level = max(levels.values()) if levels else 0
    level_groups: dict[int, list[str]] = {lv: [] for lv in range(max_level + 1)}
    for nid, lv in levels.items():
        level_groups[lv].append(nid)
    for lv in level_groups:
        level_groups[lv].sort()

    if direction == "LR":
        # 横向布局：层级 = 列
        max_cols = max_level + 1
        max_rows = max(len(g) for g in level_groups.values())
    else:
        # 纵向布局：层级 = 行
        max_rows = max_level + 1
        max_cols = max(len(g) for g in level_groups.values())
    # 无决策节点时不占位，保持原有行/列步距
    col_w = max(node_w, diamond_w if decision_ids else node_w) + col_gap
    row_h = max(node_h, diamond_h if decision_ids else node_h) + row_gap
    total_w = padding * 2 + max_cols * col_w - col_gap
    total_h = title_h + padding * 2 + max_rows * row_h + caption_h

    # 2.5x 缩放渲染 + @_render_hq 保持原分辨率并添加 300 DPI 元数据
    # 2.5x 渲染 + 不降采样 = 真实超采样抗锯齿，最终 DOCX 插入时 DPI 感知缩放到合适尺寸

    img = Image.new("RGB", (int(total_w * scale), int(total_h * scale)), "white")
    draw = ImageDraw.Draw(img)
    font_title = _image_font(int(20 * scale), bold=True)
    font_node = _image_font(int(15 * scale))
    font_edge = _image_font(int(13 * scale))
    font_caption = _image_font(int(13 * scale))

    # ---- 标题 ----
    # 从 Mermaid 中提取 title（如果有）
    title = "流程图"
    for raw in mermaid_code.split("\n"):
        if raw.strip().startswith("title"):
            title = raw.strip()[5:].strip()
            break

    draw.text((int(padding * scale), int(18 * scale)), title, fill="#2D2D3A", font=font_title)

    # ---- 计算节点位置 ----
    positions: dict[str, tuple[float, float, float, float]] = {}
    for lv, ids in level_groups.items():
        n_in_level = len(ids)
        if direction == "LR":
            # BUG 修复：LR（横向）布局中列 x 必须固定按层级推进
            # （padding + lv * col_w），与画布 total_w 的计算方式一致。
            # 旧公式额外叠加 (max_cols-1)*col_w/2 - n_in_level*col_w/2 的
            # "水平居中"项，会让列间距小于 col_w 甚至超出画布（数值验证：
            # 层级规模 [1,2,1] 时相邻列水平重叠 50px）。居中只应作用于 y 轴。
            start_x = padding * scale + lv * col_w * scale
            for i, nid in enumerate(ids):
                x = start_x
                y = (
                    title_h * scale
                    + padding * scale
                    + (max_rows - n_in_level) * row_h * scale / 2
                    + i * row_h * scale
                )
                w = diamond_w * scale if nid in decision_ids else node_w * scale
                h = diamond_h * scale if nid in decision_ids else node_h * scale
                positions[nid] = (x, y, w, h)
        else:
            start_y = title_h * scale + padding * scale + lv * row_h * scale
            start_x = padding * scale + (max_cols - n_in_level) * col_w * scale / 2
            for i, nid in enumerate(ids):
                x = start_x + i * col_w * scale
                y = start_y
                w = diamond_w * scale if nid in decision_ids else node_w * scale
                h = diamond_h * scale if nid in decision_ids else node_h * scale
                positions[nid] = (x, y, w, h)

    node_lookup = {n["id"]: n for n in nodes}

    # ---- 绘制连线（先画线，再画节点，确保节点覆盖在连线上方）----
    arrow_color = "#333333"
    edge_label_bg = "#FFFFFF"
    for e in edges:
        if e["from"] not in positions or e["to"] not in positions:
            continue
        fx, fy, fw, fh = positions[e["from"]]
        tx, ty, tw, th = positions[e["to"]]
        # 计算源点和目标点
        if direction == "LR":
            sx = fx + fw
            sy = fy + fh / 2
            ex = tx
            ey = ty + th / 2
        else:
            sx = fx + fw / 2
            sy = fy + fh
            ex = tx + tw / 2
            ey = ty

        # 绘制折线（带拐点）
        if direction == "LR":
            mid_x = (sx + ex) / 2
            path = [(sx, sy), (mid_x, sy), (mid_x, ey), (ex, ey)]
        else:
            mid_y = (sy + ey) / 2
            path = [(sx, sy), (sx, mid_y), (ex, mid_y), (ex, ey)]

        # 调整终点（在节点边缘前停住，留箭头空间）
        if direction == "LR":
            path[-1] = (ex - 8, ey)
        else:
            path[-1] = (ex, ey - 8)

        # 扁平化直线连线（无阴影）
        draw.line(path, fill=arrow_color, width=int(1.5 * scale))

        # 绘制箭头（使用实心三角形）
        if direction == "LR":
            arrow_tip = (ex - 2, ey)
            arrow_base1 = (ex - 14, ey - 7)
            arrow_base2 = (ex - 14, ey + 7)
        else:
            arrow_tip = (ex, ey - 2)
            arrow_base1 = (ex - 7, ey - 14)
            arrow_base2 = (ex + 7, ey - 14)
        draw.polygon([arrow_tip, arrow_base1, arrow_base2], fill=arrow_color)

        # 绘制边标签
        if e["label"]:
            if direction == "LR":
                label_x = (sx + ex) / 2 - 14
                label_y = (sy + ey) / 2 - 16
            else:
                label_x = (sx + ex) / 2 - 14
                label_y = sy + (ey - sy) / 2 - 10
            bbox = draw.textbbox((0, 0), e["label"], font=font_edge)
            tw_l = bbox[2] - bbox[0] + 8
            th_l = bbox[3] - bbox[1] + 4
            draw.rectangle(
                [label_x - 2, label_y - 2, label_x + tw_l, label_y + th_l],
                fill=edge_label_bg,
                outline="#9B8BE0",
                width=1,
            )
            draw.text((label_x + 2, label_y), e["label"], fill="#2D2D3A", font=font_edge)

    # ---- 绘制节点（最后画，置于最上层）----
    for nid, (x, y, w, h) in positions.items():
        node = node_lookup[nid]
        label = node["label"]
        if nid in decision_ids:
            # 菱形（决策）扁平化，无阴影
            cx = x + w / 2
            cy = y + h / 2
            d = min(w, h) / 2 - 2
            # 主体
            draw.polygon(
                [(cx, cy - d), (cx + d, cy), (cx, cy + d), (cx - d, cy)],
                fill="#EDE9FE",
                outline="#9B8BE0",
                width=int(1.5 * scale),
            )
            # 文字必须约束在菱形内接矩形内（宽 = 高 = min(w, h)），
            # 否则会被菱形斜边裁切（BUG-2026-1011-02）。
            inscribed = min(w, h)
            inner_w = max(int(24 * scale), int(inscribed - 24 * scale))
            _draw_text_center(
                draw,
                (cx - inner_w / 2, y, cx + inner_w / 2, y + h),
                label,
                font_node,
                "#2D2D3A",
                max_lines=4,
            )
        else:
            # 扁平化直角矩形（无圆角、无阴影）
            draw.rectangle(
                [x, y, x + w, y + h],
                fill="#EDE9FE",
                outline="#9B8BE0",
                width=int(1.5 * scale),
            )
            _draw_text_center(
                draw, (x + 4, y + 4, x + w - 4, y + h - 4), label, font_node, "#2D2D3A", max_lines=2
            )

    # ---- 图例（扁平化淡紫色系）----
    # 注意：图例几何必须与 scale 同步缩放——字体已按 scale 放大，
    # 但旧的色块/间距仍是原始像素，2.5x 渲染下两个图例项会挤在一起
    # （图上显示成 "流程节点决策判定" 连成一片），且贴近底边被裁切。
    label1, label2 = "流程节点", "决策判定"
    box_w = int(24 * scale)
    box_h = int(16 * scale)
    text_off = int(8 * scale)      # 色块与文字之间的间距
    item_gap = int(28 * scale)     # 两个图例项之间的间距
    bbox1 = draw.textbbox((0, 0), label1, font=font_caption)
    bbox2 = draw.textbbox((0, 0), label2, font=font_caption)
    text_w1 = bbox1[2] - bbox1[0]
    text_h = max(bbox1[3] - bbox1[1], bbox2[3] - bbox2[1])
    legend_bottom = int(total_h * scale) - int(caption_h * scale / 2)
    legend_top = legend_bottom - max(box_h, text_h)
    legend_x = int(padding * scale)
    # 流程节点（矩形）
    draw.rectangle(
        [legend_x, legend_top, legend_x + box_w, legend_top + box_h],
        fill="#EDE9FE",
        outline="#9B8BE0",
        width=int(1 * scale),
    )
    draw.text((legend_x + box_w + text_off, legend_top), label1, fill="#2D2D3A", font=font_caption)
    legend_x += box_w + text_off + text_w1 + item_gap
    # 决策节点（菱形）
    cx = legend_x + box_w / 2
    cy = legend_top + box_h / 2
    d = min(box_w, box_h) / 2
    draw.polygon(
        [(cx, cy - d), (cx + d, cy), (cx, cy + d), (cx - d, cy)],
        fill="#EDE9FE",
        outline="#9B8BE0",
        width=int(1 * scale),
    )
    draw.text((legend_x + box_w + text_off, legend_top), label2, fill="#2D2D3A", font=font_caption)

    result_stream = _image_to_stream(img)
    size_kb = len(result_stream.getvalue()) // 1024
    logger.info(
        "[PIL v2渲染] 流程图渲染完成: 节点=%d 尺寸=%dKB DPI=300",
        len(nodes),
        size_kb,
    )
    return result_stream

def flowchart_json_to_mermaid(data: dict) -> str:
    """将 nodes + edges 结构转换为 Mermaid 流程图代码字符串

    用于在前端 Mermaid.js 预览和 mmdc 渲染中使用。
    支持简化格式：nodes 可以是字符串列表（["步骤1", "步骤2"]），
    此时自动按顺序生成连线；edges 缺失时同样生成顺序连线。

    Args:
        data: {"nodes": [...], "edges": [...], "title": "..."}
              nodes 支持两种格式：
                - 对象列表: [{"id": "A", "label": "开始", "type": "start"}, ...]
                - 字符串列表: ["开始", "处理", "结束"]

    Returns:
        Mermaid 流程图代码字符串
    """
    try:
        nodes_raw = data.get("nodes", [])
        edges_raw = data.get("edges", [])
        if not nodes_raw:
            return ""

        # === 规范化 nodes：统一为对象列表 ===
        norm_nodes: list[dict] = []
        for i, node in enumerate(nodes_raw):
            if isinstance(node, str):
                # 字符串节点：自动分配 ID
                nid = f"N{i+1}"
                norm_nodes.append({"id": nid, "label": node, "type": "process"})
            elif isinstance(node, dict):
                nid = node.get("id") or f"N{i+1}"
                label = node.get("label") or nid
                ntype = node.get("type", "process")
                norm_nodes.append({"id": nid, "label": label, "type": ntype})
            else:
                nid = f"N{i+1}"
                norm_nodes.append({"id": nid, "label": str(node), "type": "process"})

        if len(norm_nodes) < 2:
            return ""

        # === 规范化 edges ===
        # 构建"数字索引 → 节点 ID"映射，因为 AI 经常输出 edges=[[0,1],[1,2]...] 形式
        # （用节点在 nodes 数组中的索引），需要自动映射为实际节点 ID（N1/N2/...）
        # BUG 修复：仅当值是纯数字且【不是】已有节点 ID 时才做索引映射。
        # AI 也常用数字字符串作节点 ID（如 "1"、"2"），旧逻辑会把真实 ID
        # 误当数组下标改写，产生错误拓扑（自环/错边）。
        node_ids = {n["id"] for n in norm_nodes}
        idx_to_id: dict[str, str] = {
            str(i): n["id"] for i, n in enumerate(norm_nodes)
        }

        def _resolve_endpoint(v: Any) -> str:
            s = str(v)
            if s in node_ids:
                return s  # 精确匹配已有节点 ID，优先于索引映射
            if s in idx_to_id:
                return idx_to_id[s]
            return s

        norm_edges: list[dict] = []
        edges_valid = bool(edges_raw) and isinstance(edges_raw, list)
        if edges_valid:
            for edge in edges_raw:
                if isinstance(edge, dict):
                    f = edge.get("from", "")
                    t = edge.get("to", "")
                    elabel = edge.get("label", "")
                    if f and t:
                        # 自动把数字索引 from/to 转成节点 ID（优先精确 ID 匹配）
                        f = _resolve_endpoint(f)
                        t = _resolve_endpoint(t)
                        norm_edges.append({"from": f, "to": t, "label": elabel})
                elif isinstance(edge, (list, tuple)) and len(edge) >= 2:
                    f = _resolve_endpoint(edge[0])
                    t = _resolve_endpoint(edge[1])
                    norm_edges.append({"from": f, "to": t, "label": ""})
                elif isinstance(edge, str) and "-->" in edge:
                    # Mermaid 格式边
                    parts = edge.split("-->", 1)
                    f = parts[0].strip()
                    t_raw = parts[1].strip()
                    elabel = ""
                    if t_raw.startswith("|") and "|" in t_raw:
                        elabel = t_raw[1:t_raw.index("|")]
                        t_raw = t_raw[t_raw.index("|") + 1:]
                    norm_edges.append({"from": f, "to": t_raw, "label": elabel})

        # === 无有效 edges 时自动生成顺序连线 ===
        if not norm_edges:
            for i in range(len(norm_nodes) - 1):
                norm_edges.append({
                    "from": norm_nodes[i]["id"],
                    "to": norm_nodes[i + 1]["id"],
                    "label": ""
                })

        # === 生成 Mermaid 代码 ===
        # BUG-FIX-34 修复：原硬编码 "flowchart TD"，丢弃 data 中的 direction 字段，
        # 导致 AI 输出的 LR 流程图被强制转为 TD。改为读取 direction 并归一化。
        direction = str(data.get("direction", "TD")).upper()
        if direction == "TB":
            direction = "TD"
        if direction not in ("TD", "LR", "RL", "BT"):
            direction = "TD"
        lines = [f"flowchart {direction}"]
        title = data.get("title")
        if title:
            lines.append(f"    title {title}")

        # 节点定义
        for node in norm_nodes:
            nid = node["id"]
            label = node["label"].replace('"', "'")
            ntype = node["type"]
            if ntype in ("start", "end"):
                lines.append(f'    {nid}(["{label}"])')
            elif ntype == "decision":
                lines.append(f"    {nid}{{{label}}}")
            else:
                lines.append(f'    {nid}["{label}"]')

        # 边定义
        for edge in norm_edges:
            f = edge["from"]
            t = edge["to"]
            label = edge.get("label", "")
            if label:
                lines.append(f"    {f} -->|{label}| {t}")
            else:
                lines.append(f"    {f} --> {t}")

        logger.info(
            "[flowchart_json_to_mermaid] 转换成功: nodes=%d edges=%d (edges_source=%s)",
            len(norm_nodes), len(norm_edges),
            "auto_generated" if not edges_valid else "user_provided"
        )
        return "\n".join(lines)
    except Exception as e:
        logger.warning("flowchart_json_to_mermaid 转换失败: %s", e)
        return ""

@_render_hq
def _render_flowchart_with_edges(data: dict) -> BytesIO | None:
    """使用 PIL 绘制带箭头和标签的完整流程图（nodes + edges 结构）

    输入：{"nodes": [{"id":"A","label":"施工准备","type":"start"},...],
           "edges": [{"from":"A","to":"B","label":"合格"},...]}
    支持简化格式：nodes 可以是字符串列表，edges 缺失时自动生成顺序连线。

    支持3种节点类型（统一扁平化淡紫色样式）:
    - start: 直角矩形, 淡紫 (#EDE9FE)
    - process: 直角矩形, 淡紫 (#EDE9FE)
    - end: 直角矩形, 淡紫 (#EDE9FE)

    自动布局：按 edges 拓扑排序确定节点位置
    箭头绘制：使用 PIL.ImageDraw 的 line + polygon 绘制方向箭头
    BUG-FIX-33 修复：添加 @_render_hq + 2.5x 超采样，与其他渲染函数保持一致。
    边标签：在箭头中间位置绘制 label 文本
    """
    try:
        nodes_raw = data.get("nodes", [])
        edges_raw = data.get("edges", [])
        if not nodes_raw:
            return None

        # === 规范化 nodes ===
        nodes: list[dict] = []
        for i, node in enumerate(nodes_raw):
            if isinstance(node, str):
                nodes.append({"id": f"N{i+1}", "label": node, "type": "process"})
            elif isinstance(node, dict):
                nid = node.get("id") or f"N{i+1}"
                nodes.append({
                    "id": nid,
                    "label": node.get("label") or nid,
                    "type": node.get("type", "process"),
                })
            else:
                nodes.append({"id": f"N{i+1}", "label": str(node), "type": "process"})

        if len(nodes) < 2:
            return None

        # === 规范化 edges ===
        edges: list[dict] = []
        if isinstance(edges_raw, list) and edges_raw:
            # BUG 修复（与 flowchart_json_to_mermaid 同源）：仅当端点值不是
            # 已有节点 ID 时才做"数字索引 → 节点 ID"映射，避免把数字字符串
            # 节点 ID 误当数组下标改写，产生自环/错边。
            _node_ids = {n["id"] for n in nodes}
            idx_to_id = {str(i): n["id"] for i, n in enumerate(nodes)}

            def _resolve_ep(v) -> str:
                s = str(v)
                if s in _node_ids:
                    return s
                if s in idx_to_id:
                    return idx_to_id[s]
                return s

            for edge in edges_raw:
                if isinstance(edge, dict):
                    f = _resolve_ep(edge.get("from", ""))
                    t = _resolve_ep(edge.get("to", ""))
                    if f and t:
                        edges.append({"from": f, "to": t, "label": edge.get("label", "")})
                elif isinstance(edge, (list, tuple)) and len(edge) >= 2:
                    f = _resolve_ep(edge[0])
                    t = _resolve_ep(edge[1])
                    edges.append({"from": f, "to": t, "label": ""})

        # === 无 edges 时自动生成顺序连线 ===
        if not edges:
            for i in range(len(nodes) - 1):
                edges.append({"from": nodes[i]["id"], "to": nodes[i + 1]["id"], "label": ""})

        # 尺寸参数（2.5x 超采样抗锯齿）
        scale = 2.5
        padding = int(40 * scale)
        node_w = int(180 * scale)
        node_h = int(56 * scale)
        col_gap = int(60 * scale)
        row_gap = int(32 * scale)
        title_h = int(60 * scale)

        # 节点类型配色
        type_styles = {
            "start": {"fill": "#EDE9FE", "outline": "#9B8BE0", "shape": "rect"},
            "process": {"fill": "#EDE9FE", "outline": "#9B8BE0", "shape": "rect"},
            "end": {"fill": "#EDE9FE", "outline": "#9B8BE0", "shape": "rect"},
        }

        # 构建邻接表 & 入度
        adj: dict[str, list[str]] = {n["id"]: [] for n in nodes}
        in_degree: dict[str, int] = {n["id"]: 0 for n in nodes}
        for e in edges:
            f, t = e.get("from", ""), e.get("to", "")
            if f in adj and t in adj:
                adj[f].append(t)
                in_degree[t] = in_degree.get(t, 0) + 1

        # 拓扑排序（Kahn 算法）确定层级
        levels: dict[str, int] = {}
        queue = [nid for nid, d in in_degree.items() if d == 0]
        if not queue:
            queue = [nodes[0]["id"]]
        for nid in queue:
            levels[nid] = 0
        head = 0
        max_iterations = len(nodes) * len(nodes) + 100
        iterations = 0
        while head < len(queue):
            cur = queue[head]
            head += 1
            iterations += 1
            if iterations > max_iterations:
                for n in nodes:
                    if n["id"] not in levels:
                        levels[n["id"]] = max(levels.values()) + 1 if levels else 0
                break
            for nb in adj.get(cur, []):
                if nb not in levels:
                    levels[nb] = levels[cur] + 1
                    queue.append(nb)

        # 未分配层级的节点
        for n in nodes:
            if n["id"] not in levels:
                levels[n["id"]] = max(levels.values()) + 1 if levels else 0

        if not levels:
            levels = {n["id"]: 0 for n in nodes}

        max_level = max(levels.values()) if levels else 0
        level_groups: dict[int, list[str]] = {lv: [] for lv in range(max_level + 1)}
        for nid, lv in levels.items():
            level_groups[lv].append(nid)
        for lv in level_groups:
            level_groups[lv].sort()

        max_rows = max_level + 1
        max_cols = max(len(g) for g in level_groups.values())
        col_w = node_w + col_gap
        row_h = node_h + row_gap

        total_w = padding * 2 + max_cols * col_w - col_gap
        total_h = title_h + padding * 2 + max_rows * row_h

        img = Image.new("RGB", (int(total_w), int(total_h)), "white")
        draw = ImageDraw.Draw(img)
        font_title = _image_font(int(20 * scale), bold=True)
        font_node = _image_font(int(15 * scale))
        font_edge = _image_font(int(13 * scale))
        font_caption = _image_font(int(13 * scale))

        # 标题
        title = str(data.get("title", "流程图"))
        draw.text((padding, int(18 * scale)), title, fill="#2D2D3A", font=font_title)

        # 计算节点位置
        node_lookup = {n["id"]: n for n in nodes}
        positions: dict[str, tuple[float, float, float, float]] = {}

        for lv, ids in level_groups.items():
            n_in_level = len(ids)
            start_y = title_h + padding + lv * row_h
            start_x = padding + (max_cols - n_in_level) * col_w / 2
            for i, nid in enumerate(ids):
                x = start_x + i * col_w
                y = start_y
                positions[nid] = (x, y, node_w, node_h)

        # ---- 先画连线（确保节点覆盖在连线上方）----
        arrow_color = "#333333"
        edge_label_bg = "#FFFFFF"
        for e in edges:
            f, t = e.get("from", ""), e.get("to", "")
            if f not in positions or t not in positions:
                continue
            fx, fy, fw, fh = positions[f]
            tx, ty, tw, th = positions[t]

            # 计算源点和目标点（从上到下布局）
            sx = fx + fw / 2
            sy = fy + fh
            ex = tx + tw / 2
            ey = ty

            # 折线路径（带拐点）
            mid_y = (sy + ey) / 2
            path = [(sx, sy), (sx, mid_y), (ex, mid_y), (ex, ey - int(8 * scale))]

            draw.line(path, fill=arrow_color, width=int(2 * scale))

            # 绘制箭头（使用 polygon 绘制三角箭头）
            arrow_tip = (ex, ey - int(2 * scale))
            arrow_base1 = (ex - int(5 * scale), ey - int(12 * scale))
            arrow_base2 = (ex + int(5 * scale), ey - int(12 * scale))
            draw.polygon([arrow_tip, arrow_base1, arrow_base2], fill=arrow_color)

            # 在箭头中间位置绘制 label 文本
            label = e.get("label", "")
            if label:
                label_x = (sx + ex) / 2 - 14
                label_y = sy + (ey - sy) / 2 - 10
                bbox = draw.textbbox((0, 0), label, font=font_edge)
                tw_l = bbox[2] - bbox[0] + 8
                th_l = bbox[3] - bbox[1] + 4
                draw.rectangle(
                    [label_x - 2, label_y - 2, label_x + tw_l, label_y + th_l],
                    fill=edge_label_bg,
                    outline="#9B8BE0",
                    width=1,
                )
                draw.text((label_x + 2, label_y), label, fill="#2D2D3A", font=font_edge)

        # ---- 再画节点（置于最上层）----
        for nid, (x, y, w, h) in positions.items():
            node = node_lookup.get(nid, {})
            label = node.get("label", nid)
            ntype = node.get("type", "process")
            style = type_styles.get(ntype, type_styles["process"])

            # 扁平化直角矩形（无圆角、无阴影）
            draw.rectangle(
                [x, y, x + w, y + h],
                fill=style["fill"],
                outline=style["outline"],
                width=int(1.5 * scale),
            )
            _draw_text_center(
                draw, (x + 4, y + 4, x + w - 4, y + h - 4), label, font_node, "#2D2D3A", max_lines=2
            )

        # ---- 图例（扁平化淡紫色系）----
        legend_y = total_h - 24
        legend_x = padding
        legend_items = [
            ("#EDE9FE", "#9B8BE0", "流程节点"),
        ]
        for fill, outline, ltext in legend_items:
            draw.rectangle(
                [
                    legend_x,
                    legend_y - int(14 * scale),
                    legend_x + int(24 * scale),
                    legend_y + int(2 * scale),
                ],
                fill=fill,
                outline=outline,
                width=int(1 * scale),
            )
            draw.text(
                (legend_x + int(30 * scale), legend_y - int(13 * scale)),
                ltext,
                fill="#2D2D3A",
                font=font_caption,
            )
            legend_x += int(90 * scale)

        return _image_to_stream(img)
    except Exception as e:
        logger.warning("_render_flowchart_with_edges 渲染失败: %s", e)
        return None

