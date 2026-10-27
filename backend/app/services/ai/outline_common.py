"""outline_common.py — 目录生成共享函数库（V5.1 唯一路径核心）

从已退役的 V2/V3 模块抽离的跨策略共享函数：
- 同级标题去重（_uniquify_outline_titles / _dedupe_outline_titles）
- 标题归一化（_title_core）— 全系统唯一入口
- 冗余后缀剥离（_strip_legacy_redundant_suffix / _clean_legacy_suffix_tree）
- 重复标题消歧（_disambiguate_title）
- 最小子节点保障（_ensure_min_children）
- 节点分布生成（generate_nodes_distribution）
- JSON 骨架构建（generate_one_outline_json_by_level1）
- JSON 结构校验（check_json）
- 承包范围解析（_parse_scope_items）
- 节点计数（_count_outline_nodes）
- 路径式子编号（child_number / tree_path_numbers）— 编号命名空间唯一事实源

所有新代码应直接 import 本模块。

## 路径式编号（2026-10-13 统一，编号命名空间单一事实源）

「1 / 1.2 / 1.2.3」式路径编号曾有三处各自实现：
  1. `outline_gate.renumber_outline_ids`（内存树位置 id，门禁修正用）；
  2. `sse_db_ops._save_nodes`（落库时随节点透出的 `path_number`）；
  3. `services.sections.get_section_path`（DB 动态路径号，导出/正文消费）。
三处算法相同但各自维护，任何一处口径漂移都会让「同一节点三个编号」。
现收敛为：
  - `child_number(parent_number, index)`：**拼接算法唯一实现**（三处共用）；
  - `tree_path_numbers(nodes)`：内存树版批量计算（DFS 顺序，与节点一一对应）；
  - DB 版（`get_section_path`）消费同一 `child_number` 自顶向下拼接。
一致性由 `tests/test_outline_l3_hardening_2026_10_13.py::TestPathNumberNamespace`
锁定（同结构四出口一致）。


## 标题去重策略（2026-10-11 修正，**勿回退**）

作用域是**同一父节点下**，不是全树。标书里「3.1 施工组织设计」「4.1 施工组织设计」
是两节独立内容（Word 自动目录会渲染成 3.1 / 4.1），跨父同名完全合法。

历史上这里执行的是全树去重，把跨父的同名子标题**整段删除**（章节内容消失），
同时 `_disambiguate_title` 又为这类「重复」追加「（其2）（其3）…（其7）」式后缀，
形成目录污染。两条路径叠加后既有删除又有污染。

因此：
1. 去重只剔除同一父节点下归一化后同名的真·重复（保留第一个）。
2. 消歧需要给同父重复命名时，禁止用「（其N）」——该括号序数形态会被
   `_ORDINAL_SUFFIX_RE` 反复剥离，触发无意义后缀递进。
   优先用维度词/父节点关键词/中文序数「之N」（不匹配剥离规则，无死循环）。
3. 已污染的遗留数据在去重与导出两处剥离「（其N）」后缀。

全系统统一走 `_title_core()` 做归一化，不要各处手写比较逻辑。
"""

import json
import logging
import random
import re
import copy


logger = logging.getLogger(__name__)

# ========== 固定 28 章参考目录 ==========

FIXED_28_CHAPTERS = [
    {"id": "1", "title": "项目概述", "fixed": True},
    {"id": "2", "title": "项目重难点分析", "fixed": True},
    {"id": "3", "title": "工程概况", "fixed": True},
    {"id": "4", "title": "编制依据", "fixed": True},
    {"id": "5", "title": "施工方案及技术措施", "fixed": True},
    {"id": "6", "title": "施工现场总平面布置", "fixed": True},
    {"id": "7", "title": "主要分部工程施工方法", "fixed": True, "dynamic": True},
    {
        "id": "8",
        "title": "装配式混凝土结构（PC）工程专项方案",
        "fixed": True,
        "conditional": "has_pc",
    },
    {"id": "9", "title": "质量保证措施和创优计划", "fixed": True},
    {"id": "10", "title": "施工安全措施计划", "fixed": True},
    {"id": "11", "title": "文明施工措施计划", "fixed": True},
    {"id": "12", "title": "施工环保措施计划", "fixed": True},
    {"id": "13", "title": "施工总进度计划及保证措施", "fixed": True},
    {"id": "14", "title": "资源配备计划", "fixed": True},
    {"id": "15", "title": "拟投入本工程的主要施工设备表", "fixed": True},
    {"id": "16", "title": "拟配备本工程的试验和检测仪器设备表", "fixed": True},
    {"id": "17", "title": "主要材料及劳动力计划表", "fixed": True},
    {"id": "18", "title": "特殊气候季节性施工方案", "fixed": True},
    {"id": "19", "title": "成品保护和工程保修工作的管理措施和承诺", "fixed": True},
    {"id": "20", "title": "危大工程及专项施工方案、紧急情况预案与风险控制措施", "fixed": True},
    {"id": "21", "title": "任何可能的紧急情况的处理措施、预案以及抵抗风险的措施", "fixed": True},
    {"id": "22", "title": "对专业分包工程的配合、协调、管理、服务方案", "fixed": True},
    {"id": "23", "title": "与发包人、项目管理单位、监理及设计人的配合", "fixed": True},
    {"id": "24", "title": "对总包管理及专业分包配合方案／与各参建方配合", "fixed": True},
    {"id": "25", "title": "工期优化", "fixed": True},
    {"id": "26", "title": "工程资料及竣工验收管理", "fixed": True},
    {"id": "27", "title": "附图、附件", "fixed": True},
    {"id": "28", "title": "其他", "fixed": True},
]

# 中文数字映射
_CN_NUMS = [
    "一",
    "二",
    "三",
    "四",
    "五",
    "六",
    "七",
    "八",
    "九",
    "十",
    "十一",
    "十二",
    "十三",
    "十四",
    "十五",
    "十六",
    "十七",
    "十八",
    "十九",
    "二十",
]

# ========== 模板辅助 ==========


def _get_template_titles() -> str:
    """获取模板标题列表文字（结果固定，缓存避免重复拼接）"""
    return "\n".join(f"第{ch['id']}章 {ch['title']}" for ch in FIXED_28_CHAPTERS)


def _get_template_list_json() -> str:
    """获取模板列表 JSON 字符串（结果固定，缓存避免重复序列化）"""
    return json.dumps(
        [{"id": ch["id"], "title": ch["title"]} for ch in FIXED_28_CHAPTERS],
        ensure_ascii=False,
    )


# ========== 节点分布生成 ==========


def generate_nodes_distribution(
    level1_count: int,
    min_level2: int = 3,
    min_level3: int = 5,
    max_level3_variation: int = 5,
) -> list[dict]:
    """为每个一级节点生成二级数量和每个二级下的三级数量。

    保证最低数量，并增加随机增量使结构更自然。
    一级目录下不得空置无二级目录，二级目录下也不得空置无三级目录。

    数量范围（用户定制 2026-09-04 更新）：
      每个 L2 下 L3 数量: 5 ~ 10 个（由 AI 结合项目实际判定；此处给骨架下限 5，
        上限 5 + max_level3_variation 浮动，使结构可自然长至 10）

    Args:
        level1_count: 一级目录数量
        min_level2: 每个一级最少二级数（默认 3，浮动 0~2）
        min_level3: 每个二级最少三级数（默认 5，浮动 0~max_level3_variation）
        max_level3_variation: L3 数量浮动上限（默认 5，使 5+5=10 达范围上限）
    Returns:
        list[dict]: 每个元素含 level2_count 和 leaf_per_level2
    """
    distribution = []
    for _ in range(level1_count):
        level2_count = min_level2 + random.randint(0, 2)
        leaf_distribution = []
        for _ in range(level2_count):
            # L3 数量: min_level3 + random(0, max_level3_variation) → 默认 5~10
            leaf_count = min_level3 + random.randint(0, max_level3_variation)
            leaf_distribution.append(leaf_count)
        distribution.append(
            {
                "level2_count": level2_count,
                "leaf_per_level2": leaf_distribution,
            }
        )
    return distribution


# ========== JSON 结构校验 ==========


def check_json(json_str: str, schema: str | dict | list) -> tuple[bool, str]:
    """根据模板 JSON 校验目标字符串的格式是否符合要求。

    Returns:
        tuple[bool, str]: (是否验证通过, 错误信息)
    """
    try:
        try:
            data = json.loads(json_str)
        except json.JSONDecodeError as e:
            return False, f"JSON 解析错误: {e!s}"

        try:
            if isinstance(schema, str):
                schema = json.loads(schema)
            elif not isinstance(schema, (dict, list)):
                return False, "schema 必须是 JSON 字符串、字典对象或列表对象"
        except json.JSONDecodeError as e:
            return False, f"schema 解析错误: {e!s}"

        def check_structure(target, template, path="", structural_only=False, _depth=0):
            # BUG-FIX 2026-09-14（中）：深度嵌套的 AI 输出会触发 RecursionError，
            # 该异常发生在 check_json 的 try 内会被兜成 "校验异常"，
            # 但在其它直接调用点会直接炸穿调用栈。这里加显式深度上限。
            if _depth > 30:
                return False, f"路径 '{path}' 结构嵌套过深（>30 层），拒绝继续校验"
            _next_depth = _depth + 1
            if isinstance(template, (int, float)) and isinstance(target, (int, float)):
                return True, ""
            if type(template) != type(target) and not (
                isinstance(template, (int, float)) and isinstance(target, (int, float))
            ):
                return (
                    False,
                    f"路径 '{path}' 的类型不匹配: 期望 {type(template).__name__}, 实际 {type(target).__name__}",
                )
            if isinstance(template, list):
                # BUG 修复: 空模板列表应检查 target 也是列表类型，且允许空列表匹配空模板
                if not template:
                    if not isinstance(target, list):
                        return False, f"路径 '{path}' 期望列表，实际 {type(target).__name__}"
                    return True, ""
                if not target:
                    return False, f"路径 '{path}' 的列表为空，但期望有内容"
                # BUG-OUTLINE-L2 修复：等长列表必须逐位置对应比较。
                # 旧逻辑对所有 target 项都用 template[0] 做"精确值"比较，
                # 导致骨架自检时 children[1].id（如 "3.1.2"）与 template[0].id
                # （如 "3.1.1"）必然不相等 → L2 填充结构校验 100% 失败 →
                # 全部章节回退默认目录（历史日志 L2 45/45 全部失败即此问题）。
                if len(template) == len(target):
                    for i, (t_item, item) in enumerate(zip(template, target)):
                        ok, err = check_structure(
                            item, t_item, f"{path}[{i}]", structural_only, _next_depth
                        )
                        if not ok:
                            return False, err
                else:
                    # 不等长：结构模板模式——每个 target 项对照 template[0] 的
                    # "结构"（键/类型/嵌套形状），叶子节点仅校验类型不比较值
                    #（模板中的空 title/占位值允许被 AI 填充值替换）
                    for i, item in enumerate(target):
                        ok, err = check_structure(
                            item, template[0], f"{path}[{i}]", True, _next_depth
                        )
                        if not ok:
                            return False, err
                return True, ""
            if isinstance(template, dict):
                if not isinstance(target, dict):
                    return False, f"路径 '{path}' 期望字典，实际 {type(target).__name__}"
                for key in template:
                    if key not in target:
                        return False, f"路径 '{path}' 缺少必需键 '{key}'"
                    # BUG-OUTLINE-L2 修复：title 和 description 是预期被 AI 填充的字段，
                    # 骨架里留空/占位，AI 填充后值会不同 → 只做类型校验，不比较值
                    # id、level、parent_id 等结构化字段必须严格匹配
                    _skip_value_check = key in ("title", "description", "content", "message")
                    ok, err = check_structure(target[key], template[key], f"{path}.{key}",
                            structural_only or _skip_value_check, _next_depth,
                        )
                    if not ok:
                        return False, err
                return True, ""
            if structural_only:
                # 结构模板模式：叶子仅做类型校验（类型已在上方统一检查），不比较值
                return True, ""
            if template != target:
                return False, f"路径 '{path}' 值不匹配: 期望 {template}, 实际 {target}"
            return True, ""

        ok, err = check_structure(data, schema)
        return ok, err
    except Exception as e:
        return False, f"校验异常: {e!s}"


# ========== JSON 骨架构建 ==========


def generate_one_outline_json_by_level1(
    level1_title: str,
    level1_index: int,
    nodes_distribution: list[dict],
) -> dict:
    """根据一级标题和节点分布生成 JSON 框架（AI 只填充标题，不修改结构）。

    Args:
        level1_title: 一级目录标题
        level1_index: 一级目录索引（从 1 开始）
        nodes_distribution: 节点分布，由 generate_nodes_distribution 生成
    Returns:
        dict: JSON 框架，title 和 description 留空等待 AI 填充
    """
    if level1_index < 1 or level1_index > len(nodes_distribution):
        raise ValueError(
            f"level1_index {level1_index} 超出节点分布范围 (1~{len(nodes_distribution)})"
        )

    dist = nodes_distribution[level1_index - 1]
    level2_count = dist["level2_count"]
    leaf_distribution = dist["leaf_per_level2"]

    level1_node = {
        "id": str(level1_index),
        "title": level1_title,
        "description": "",
        "children": [],
    }

    for j in range(level2_count):
        level2_node = {
            "id": f"{level1_index}.{j + 1}",
            "title": "",
            "description": "",
            "children": [],
        }
        leaf_count = leaf_distribution[j] if j < len(leaf_distribution) else 3
        for k in range(leaf_count):
            level2_node["children"].append(
                {
                    "id": f"{level1_index}.{j + 1}.{k + 1}",
                    "title": "",
                    "description": "",
                }
            )
        level1_node["children"].append(level2_node)

    return level1_node


# ========== 节点计数 ==========


def _count_outline_nodes(outline: list[dict]) -> int:
    """递归统计目录树总节点数（含所有层级）。"""
    total = 0
    for node in outline:
        if not isinstance(node, dict):
            continue
        total += 1
        children = node.get("children", [])
        if isinstance(children, list):
            total += _count_outline_nodes(children)
    return total


# ========== 路径式子编号（编号命名空间唯一事实源，2026-10-13） ==========


def child_number(parent_number: str, index: int) -> str:
    """路径式子编号拼接算法（唯一实现）。

    `parent_number=""` + index=1 → `"1"`；`"1"` + index=2 → `"1.2"`；
    `"1.2"` + index=3 → `"1.2.3"`。

    对「父号是否带尾点」两种调用约定都兼容（`"1"` 与 `"1."` 都给 `"1.2"`）——
    这正是一线口径分裂的实证：`_save_nodes` 的递归约定是父号带尾点
    （传 `f"{node_number}."`），而 `renumber_outline_ids` 的约定是不带点、
    拼接时加点。两者输出恰好一致，所以漂移长期不可见；统一到本函数后
    两种约定都归一为同一输出，新增消费方无需关心尾点归属。

    三个消费方共用本函数，保证同节点同编号：
      - `outline_gate.renumber_outline_ids`（内存树位置 id，不带点约定）
      - `sse_db_ops._save_nodes`（落库透出的 `path_number`，带尾点约定）
      - `sections.get_section_path`（DB 动态路径号，不带点约定）

    边界说明（2026-10-13 · 与前端编号体系的关系）：本函数是**后端位置式
    路径号**的唯一拼接算法，服务于生成编排 / 落库 / 门禁等**内部**场景。
    前端目录编辑 / 章节编辑展示的章节号是另一套**层级式文档标题号**
    （`frontend/src/utils.ts::annotateOutlineWithNumbers` →
    `deriveHeadingPath` + `formatV21Heading`，按 L1~L8 规则逐层重置），
    二者**用途不同、本就不必逐位相同**：正文内嵌编号在生成/保存时即被剥离
    （`content_cleaning.strip_heading_numbering_from_content`），文档编号在
    导出/展示时按当前树结构现算。故此处**不**与前端数字对齐——强行对齐会把
    前端层级展示号改成位置路径号，属展示回归。两套体系自此各自单一事实源，
    仅在此交叉注明，防止后续误「修复」本非缺陷的差异。
    """
    if not parent_number:
        return str(index)
    if parent_number.endswith("."):
        return f"{parent_number}{index}"
    return f"{parent_number}.{index}"


def tree_path_numbers(nodes: list[dict]) -> list[str]:
    """按 DFS（深度优先、兄弟按序）顺序产出路径式子编号列表。

    返回列表与 `_flatten_dfs(nodes)` 的节点一一对应（同序遍历口径），
    供落库/门禁侧做「编号 ↔ 节点」配对，避免各处重算口径漂移。
    非 dict 节点跳过（与 `_count_outline_nodes` 的容错口径一致）。
    """
    out: list[str] = []

    def _walk(level_nodes: Any, prefix: str) -> None:
        if not isinstance(level_nodes, list):
            return
        for index, node in enumerate(level_nodes, 1):
            if not isinstance(node, dict):
                continue
            number = child_number(prefix, index)
            out.append(number)
            _walk(node.get("children"), number)

    _walk(nodes, "")
    return out


# ========== 承包范围解析 ==========


def _parse_scope_items(scope: str) -> list[str]:
    """解析承包范围字符串为列表。"""
    if not scope:
        return ["土方工程", "主体结构", "砌体工程", "装饰装修", "机电安装"]
    for sep in ["、", "，", ",", ";", "；", "\n"]:
        if sep in scope:
            items = [s.strip() for s in scope.split(sep) if s.strip()]
            if len(items) >= 2:
                return items
    return (
        [scope.strip()]
        if scope.strip()
        else ["土方工程", "主体结构", "砌体工程", "装饰装修", "机电安装"]
    )


# ========== 兜底标题生成（结合上一级目录对应性） ==========

# 通用标题后缀（用于从标题剥离冗余词，提取核心语义）
_GENERIC_TITLE_SUFFIXES = sorted(
    [
        "专项施工方案",
        "施工专项方案",
        "施工组织方案",
        "施工方案",
        "施工方法",
        "施工工艺",
        "专项方案",
        "实施方案",
        "保障方案",
        "管理方案",
        "创优计划",
        "管理体系",
        "管理系统",
        "管理机制",
        "管理制度",
        "管理组织",
        "管理架构",
        "管理措施",
        "保障措施",
        "控制措施",
        "技术措施",
        "保护措施",
        "防护措施",
        "计划",
        "方案",
        "措施",
        "体系",
        "机制",
        "组织",
        "制度",
        "流程",
        "工程",
        "施工",
        "管理",
        "控制",
        "保障",
        "保护",
        "防护",
    ],
    key=len,
    reverse=True,
)

_GENERIC_TITLE_PREFIXES = ["工程", "项目", "主要", "施工", "单位", "本项"]

# 语义域识别关键词（顺序即优先级：工程部位 > 管理维度）
_DOMAIN_KEYWORDS: dict[str, tuple[str, ...]] = {
    "work": (
        "混凝土",
        "钢筋",
        "模板",
        "土方",
        "基坑",
        "桩基",
        "砌筑",
        "防水",
        "保温",
        "幕墙",
        "钢结构",
        "装配式",
        "屋面",
        "装饰",
        "装修",
        "机电",
        "电气",
        "给排水",
        "暖通",
        "消防",
        "智能化",
        "弱电",
        "道路",
        "桥梁",
        "隧道",
        "吊顶",
        "抹灰",
        "涂料",
        "楼地面",
        "门窗",
        "脚手架",
    ),
    "quality": ("质量", "创优", "样板", "优质"),
    "safety": ("安全", "应急", "消防", "文明", "环保", "环境"),
    "schedule": ("进度", "工期", "里程碑"),
    "cost": ("成本", "造价", "经济", "资金"),
    "org": ("组织", "机构", "架构", "人员", "岗位", "制度"),
    "tech": ("施工", "工艺", "技术"),
}

# L3 后缀池（按语义域；拼接后标题 8~14 字；不含"基本要求"等禁用词）
_L3_SUFFIX_POOL: dict[str, tuple[str, ...]] = {
    "work": (
        "施工工艺与操作要点",
        "质量检查与验收标准",
        "材料与机具配置",
        "安全防护与技术交底",
        "常见通病与防治措施",
        # 2026-10-27 修复（BUG-OUTLINE-2026-10-27-L2POOL）：旧值「进度计划与资源保障」
        # 与 _L2_SUFFIX_POOL["work"] 完全相同，L3 直接吐出 L2 章名。
        "进度安排与资源协调",
        # 2026-10-27 修复：旧值「成品保护与现场管理」与 _L2_SUFFIX_POOL["work"] 完全相同。
        "成品保护与文明施工",
        "检验试验与记录管理",
        "测量放线与工序交接",
        "季节性施工与应急措施",
        # 2026-10-27 第三轮扩容：增加动作型条目，缓解跨 L2 重复问题。
        "工序穿插与界面协调",
        "施工准备与作业面移交",
    ),
    "quality": (
        "过程检查与验收标准",
        "通病防治措施",
        "检测试验与记录管理",
        "样板引路与首件验收",
        "质量追溯与资料归档",
        "问题整改闭环机制",
        "关键工序旁站监督",
        "分项工程质量验收",
        # 2026-10-27 第三轮扩容
        "隐蔽工程验收与资料移交",
        "实测实量与数据复核",
        # 2026-10-27 第四轮（遗留加固 P1）：扩容至 12+ 项。
        "原材料进场与复检",
        "成品保护与养护管理",
    ),
    "safety": (
        "教育培训与技术交底",
        "危险源辨识与防控",
        "防护设施与标识管理",
        "应急预案与演练",
        "隐患排查与整改闭环",
        "职业健康与劳动保护",
        "现场安全巡查制度",
        "安全技术措施交底",
        # 2026-10-27 第三轮扩容
        "临时用电与消防管理",
        "高处作业与临边防护",
        # 2026-10-27 第四轮（遗留加固 P1）：扩容至 12+ 项。
        "机械设备安全防护装置",
        "有限空间作业管理",
    ),
    "schedule": (
        "里程碑节点控制",
        "关键线路分析与保障",
        "资源投入与进度匹配",
        "偏差监测与纠偏措施",
        "周月计划分解与考核",
        "工期风险预警与应对",
        # 2026-10-27 第三轮扩容
        "工序穿插与流水段划分",
        "劳动力与机械动态调配",
        # 2026-10-27 第四轮（遗留加固 P1）：扩容至 12+ 项。
        "里程碑节点跟踪管理",
        "进度计划调整与优化",
        "工期索赔与延误处理",
        "资源调配与保障",
    ),
    "cost": (
        # 2026-10-27 修复（BUG-OUTLINE-2026-10-27-L2POOL）：旧值「目标成本分解与控制」
        # 与 _L2_SUFFIX_POOL["cost"] 完全相同，L3 直接吐出 L2 章名。
        "成本目标分解与动态控制",
        "变更签证与索赔管理",
        "资金计划与支付管理",
        # 2026-10-27 修复：旧值「成本核算与偏差分析」与 _L2_SUFFIX_POOL["cost"] 完全相同。
        "成本核算与偏差分析管理",
        "材料量价双控措施",
        # 2026-10-27 第三轮扩容
        "分包结算与资料归档",
        "人工与机械成本分析",
        # 2026-10-27 第四轮（遗留加固 P1）：扩容至 12+ 项。
        "成本偏差分析与预警",
        "材料采购与价格管控",
        "分包成本控制措施",
        "成本台账与归集管理",
        "结算资料与审核管理",
    ),
    "org": (
        # 2026-10-27 修复（BUG-OUTLINE-2026-10-27-L2POOL）：旧值全部 5 个条目
        # 与 _L2_SUFFIX_POOL["org"] 高度相似或完全相同（「组织架构与职责分工」
        # 「管理制度与考核机制」完全相同，「沟通协调与例会制度」vs「沟通协调机制」，
        # 「信息化管理与报表」vs「信息化管理平台建设」），导致 L3 直接吐出 L2 章名。
        # 现替换为动作型条目，与 generic 域的修复口径一致。
        "组织架构设置与职责明确",
        "管理制度建立与考核实施",
        "例会组织与纪要归档",
        "人员培训与技能认证",
        "信息化平台建设与数据管理",
        "绩效考核与奖惩实施",
        "岗位配置与职责落实",
        "沟通协调与问题反馈",
        # 2026-10-27 第三轮扩容
        "分包管理与协同机制",
        "制度宣贯与培训落实",
        # 2026-10-27 第四轮（遗留加固 P1）：扩容至 12+ 项。
        "组织架构调整与优化",
        "制度执行与监督检查",
    ),
    "tech": (
        "施工工艺流程与要点",
        "关键工序质量控制",
        "专项施工技术措施",
        "施工测量与放线要求",
        "机械设备配置要求",
        "成品保护与验收要求",
        "常见问题与防治措施",
        "技术交底与作业指导",
        # 2026-10-27 第三轮扩容
        "深化设计与方案优化",
        "BIM 模型应用与协调",
        # 2026-10-27 第四轮（遗留加固 P1）：扩容至 12+ 项。
        "工艺参数试验与确定",
        "新技术应用与效益评估",
    ),
    # generic 域专供 L3：必须是「动作型条目」，不得是 L2 级章名。
    # 2026-10-10 遗留建议 2：旧值（「总体实施策划」「分阶段推进计划」
    # 「资源配置与协调保障」「过程控制与考核评价」「总结评估与持续改进」）
    # 全部是二级章名 —— L3 的 prefix_core 为空（见 _derive_child_title 规则 3），
    # 池内条目会被原样吐出，于是 L2 章名直接落到三级，既读起来像章名，
    # 又与其它章节的 L2 撞名。现替换为动作型条目并纳入 _L3_L2LIKE_TITLES 拦截。
    "generic": (
        "施工方案编制与交底",
        "进度计划编制与考核",
        "资源配置与动态调配",
        "现场协调与接口管理",
        "过程检查与验收记录",
        "技术措施落实与核查",
        "责任分工与考核评价",
        "总结复盘与改进闭环",
        # 2026-10-27 第三轮扩容
        "任务分解与节点跟踪",
        "风险清单与应对方案",
        # 2026-10-27 第四轮（遗留加固 P1）：扩容至 12+ 项。
        "计划执行与动态调整",
        "验收评估与成果移交",
    ),
}

# L2 后缀池（从 L1 语义域派生二级目录；管理域用完整管理标题，工程域前置核心词）
_L2_SUFFIX_POOL: dict[str, tuple[str, ...]] = {
    "work": (
        "总体施工部署与区段划分",
        "主要分项工程施工方案",
        "专项施工方案与技术措施",
        "质量控制与验收要求",
        "安全防护与文明施工措施",
        "进度计划与资源保障",
        "成品保护与现场管理",
        "检验试验与资料管理",
        # 2026-10-27 第三轮扩容：缓解跨 L1 L2 重复问题。
        "施工准备与临建工程",
        "施工界面与专业协调",
        "竣工资料与验收移交",
    ),
    "quality": (
        "质量保证组织体系",
        "质量控制与验收要求",
        "创优目标分解与实施",
        "质量通病防治措施",
        "质量检查与考核机制",
        # 2026-10-27 第三轮扩容
        "质量目标与奖惩制度",
        "质量培训与技术交底",
    ),
    "safety": (
        "安全管理组织体系",
        "安全防护与技术措施",
        "应急预案与演练计划",
        "隐患排查与治理机制",
        "文明施工与环保措施",
        # 2026-10-27 第三轮扩容
        "安全教育与培训制度",
        "劳动保护与职业卫生",
    ),
    "schedule": (
        "进度计划编制与平衡",
        "关键线路分析与控制",
        "资源投入与保障措施",
        "进度动态监测与纠偏",
        # 2026-10-27 第三轮扩容
        "施工进度考核与奖惩",
        "进度计划执行与跟踪",
    ),
    "cost": (
        "成本管理组织体系",
        "目标成本分解与控制",
        "合同与索赔管理",
        "成本核算与偏差分析",
        # 2026-10-27 第三轮扩容
        "资金计划与动态调配",
        "变更签证与索赔处理",
    ),
    "org": (
        "组织架构与职责分工",
        "管理制度与考核机制",
        "沟通协调机制",
        "信息化管理平台建设",
        # 2026-10-27 第三轮扩容
        "人员配置与培训计划",
        "绩效考核与奖惩制度",
    ),
    "tech": (
        "主要分部工程施工方案",
        "关键工序施工技术措施",
        "专项施工方案与工艺",
        "施工测量与试验检测",
        "季节性施工措施",
        "施工机械与设备保障",
        # 2026-10-27 第三轮扩容
        "BIM 应用与三维协调",
        "施工工艺与作业指导",
    ),
    "generic": (
        "总体方案与实施策划",
        "分阶段推进计划",
        "资源配置与协调保障",
        "过程控制与考核机制",
        "总结评估与持续改进",
        # 2026-10-27 第三轮扩容
        "实施风险识别与应对",
        "责任落实与节点跟踪",
    ),
}


def _extract_title_core(title: str) -> str:
    """提取标题核心语义词（剥离通用前/后缀，保留专业词）。

    例："混凝土工程施工方案" → "混凝土"
        "土方开挖与回填工程" → "土方开挖回填"
        "质量管理体系" → "质量"
        "安全文明施工管理" → "安全文明施工"
        "项目概况与实施背景" → "概况与实施背景"（无通用词尾时保留原文）
    """
    t = (title or "").strip()
    if not t:
        return ""
    core = t
    changed = True
    while changed:
        changed = False
        for sfx in _GENERIC_TITLE_SUFFIXES:
            if core.endswith(sfx) and len(core) > len(sfx):
                core = core[: -len(sfx)].strip()
                # 剥掉"与/和/及"连接词残留，避免"质量与"这类尾巴
                while core and core[-1] in "与和及、":
                    core = core[:-1].strip()
                changed = True
                break
    for pfx in _GENERIC_TITLE_PREFIXES:
        if core.startswith(pfx) and len(core) > len(pfx):
            core = core[len(pfx) :].strip()
            break
    return core if len(core) >= 2 else t


def _detect_domain(parent_title: str) -> str:
    """按关键词判定标题所属语义域（优先级：部位 > 质量 > 安全 > 进度 > 成本 > 组织 > 技术）。"""
    t = parent_title or ""
    for dom, kws in _DOMAIN_KEYWORDS.items():
        for kw in kws:
            if kw in t:
                return dom
    return "generic"


def _core_for_derive(parent_title: str) -> str:
    """提取用于前缀的简明核心词（≤6 字），保证子标题与父标题的显性关联。

    对"X与Y"型复合词（如"土方与基坑支护"）倾向保留完整段，而非只取第一段，
    以维持语义完整性；再剥离冗余收尾词。
    """
    core = _extract_title_core(parent_title)
    core = (core or "").strip()
    if not core or len(core) > 8 or core == parent_title:
        # 提取失败/过长：按连接词取首段，或截取前 4 字
        core = ""
        for conn in ("与", "和", "及"):
            if conn in parent_title:
                seg = parent_title.split(conn)[0].strip()
                if 2 <= len(seg) <= 6:
                    core = seg
                    break
        if not core:
            core = parent_title[:4] if len(parent_title) >= 4 else parent_title
    return core if len(core) >= 2 else "工程"


# L2 前缀可用性上限：前缀必须是「修饰词」而非「重复小句」。
# 4 字是中文里定语长度的舒适上限（"基坑""环境保护""混凝土"）；
# 再长就变成两个并列小句硬拼（"安全文明施工"+"应急预案与演练计划" = 14 字），
# 读起来像把两句话挤在一起，而不是一个标题。
_L2_PREFIX_MAX_LEN = 4


def _ordered_unique(items: list) -> list:
    """保序去重。

    BUG-FIX 2026-10-12（二级轮转退化）：`dom == "generic"` 时
    `pool = _L2_SUFFIX_POOL[dom]` 与 `generic_pool = _L2_SUFFIX_POOL["generic"]`
    **是同一个元组**，于是 `full_pool = pool + generic_pool` 把每个候选都放了两遍。
    轮转 `idx % len(full_pool)` 因此只有一半的槽位是新的：generic 域本就只有 5 个
    候选，前 5 次取完后就只能反复命中已用过的条目并最终落到
    `_semantic_fallback_title` —— 池容量被白白砍半。
    L3 分支的 `pool_filtered + entity + generic_filtered` 同样会重复。
    """
    return list(dict.fromkeys(items))


def _usable_l2_prefix_core(core: str, dom: str) -> str:
    """判定 L2 派生标题是否可以拼上父级核心词前缀；不可用时返回 ""。

    BUG-FIX 2026-10-12（二级标题可读性 · 父级前缀硬拼）

    旧实现对 level=2 无条件 `prefix_core = core`，只要后缀与核心词无 ≥2 字
    重叠就硬拼。实测产出了一批**零信息量或语义矛盾**的标题：

      父级「总体实施策划」 → 总体实施分阶段推进计划 / 总体实施资源配置与协调保障
      父级「项目风险管理」 → 风险总体方案与实施策划
      父级「其他」「概述」 → 其他总体方案与实施策划 / 概述分阶段推进计划
      父级「安全文明施工措施」 → 安全文明应急预案与演练计划（14 字，两个小句硬拼）
      父级「环境保护」     → 环境安全管理组织体系（环境 ≠ 安全，语义矛盾）

    成因有二，且都与「前缀的作用」错配：
      1. **前缀是组织性空词时不承载任何主题信息**。「总体实施」「其他」「概述」
         「风险」「项目管理」这类核心词拼上去只增加噪音，不建立父子语义关联 ——
         恰恰抵消了 `_core_for_derive` docstring 声明的「保证子标题与父标题的
         显性关联」初衷。
      2. **后缀池本来就按父级语义域选过了**。`dom != "generic"` 时
         `_L2_SUFFIX_POOL[dom]` 里的每个短语本身已带该域信息，此时再拼父级
         核心词要么同义重复（质量 + 质量保证组织体系），要么跨主题矛盾
         （环境 + 安全管理组织体系）。

    因此前缀只在**同时**满足以下条件时启用 —— 也就是它真正有增量的那个狭窄区间：
      · 父级判为 generic 域，即后缀只能取到 generic 池、不带任何域信息；
      · 核心词本身**有主题性**（`_detect_domain(core) != "generic"`），
        排除「总体实施」「其他」「概述」这类纯组织词；
      · 核心词长度在 2~4 字，读起来是修饰词而不是重复小句。

    其余情况一律返回 ""，后缀短语本身就是完整可读的二级章节名
    （`分阶段推进计划` / `质量保证组织体系` / `关键线路分析与控制`），
    直接使用即可。

    跨章节去重不依赖此前缀：`_ensure_min_children` 传的是**全局** used 集合，
    `_default_l2_children_for_skeleton` 的同名标题由 `_uniquify_outline_titles`
    的同级消歧兜底；跨父同名本就合法（2026-10-11 起「同级唯一」为唯一口径）。
    """
    if not core:
        return ""
    # 池已按域选过 → 后缀自带域信息，前缀只增噪不增量
    if dom != "generic":
        return ""
    if not (2 <= len(core) <= _L2_PREFIX_MAX_LEN):
        return ""
    # 核心词自身也无主题信息 → 纯组织性空词，拼上去零增量
    if _detect_domain(core) == "generic":
        return ""
    return core


def _has_bigram_overlap(core: str, text: str) -> bool:
    """检查 core 的核心内容（≥2字连续子串）是否出现在 text 中（叠词防护）。

    用归一化后完全包含判定（_uniquify_outline_titles 同款），
    避免单字字符（如"总"在"总体"中）被误判为语义重叠。

    例：core='安全文明' text='现场安全巡查制度' → True（'安全文明'完整包含）
        core='总分包与总承包' text='总分包与总承包总体方案' → True（'总承包'是核心串）
        core='质量保证措施' text='质量保证组织体系' → True（'质量保证'是核心串）
        core='混凝土'      text='混凝土施工工艺'      → True（'混凝土'完整包含）
        core='售后服务'    text='质量检查与验收标准'  → False（无连续子串包含）
    """
    if len(core) < 2:
        return False
    # 归一化：去掉空白和标点（全大写转小写）
    _norm = lambda s: re.sub(r"[\s\u3000《》（）、，。：；！？]+", "", (s or "").lower())
    nc = _norm(core)
    nt = _norm(text)
    if not nc or not nt:
        return False
    # 检查核心串是否作为完整子串出现在 text 中
    if nc in nt:
        return True
    # 额外检查：core 任何 ≥2 字后缀是否出现在 text 中
    # （如"总分包与总承包" → 检查"总承包""分包""总分包"等）
    for start in range(len(nc)):
        for end in range(start + 2, len(nc) + 1):
            sub = nc[start:end]
            if sub in nt and len(sub) >= 2:
                return True
    return False


# ---------------------------------------------------------------------------
# 2026-10-09 增强：语义化兜底 / 消歧 / 占位清洗
#
# 背景：此前 L3 兜底标题与全局去重分别产出「专项6」「总体实施策划（2）」
# 这类无意义或裸编号后缀标题（正是《招投标项目》样例目录里大量出现的
# 「专项6/专项7」「总体实施策划（2）/（3）」的来源）。本次统一改为：
#   - 兜底标题：父级简短指代词 + 专业执行维度，保证有意义且唯一；
#   - 重名消歧：优先追加专业执行维度词，杜绝「（2）/（二）」式裸编号；
#   - 占位清洗：兜底清除任何遗留「专项N」「实施要点」脏标题。
# 与缺陷修复方案 BUG-2026-1008-02（三级标题重复/占位）的「标题编号统一管理」
# 思想一致——标题必须唯一、可懂、与父级对应。
# ---------------------------------------------------------------------------

# 专业执行维度词：用于语义化兜底与重名消歧（自包含，不依赖父级前缀拼接）
_L3_FALLBACK_DIMENSIONS: tuple[str, ...] = (
    "施工工艺", "质量管控", "安全保障", "进度安排", "资源配置",
    "技术措施", "部署方案", "管理措施", "组织保障", "防治对策",
    "验收标准", "过程控制", "实施要点", "操作细则", "专项技术",
)

# 重名消歧维度词（追加在重名标题后，使其仍可读、可懂）
_DISAMBIG_DIMENSIONS: tuple[str, ...] = (
    "专项方案", "补充措施", "实施方案", "保障措施", "管控要点",
    "深化部署", "实施细则", "配套保障", "专项对策", "落地措施",
)

# 旧版占位标题识别（需被清洗）
#
# BUG-FIX 2026-10-13（G1 · 中文数字占位漏判）：旧正则只认阿拉伯数字，
# 「专项六」「专项一」「地下室专项六」这类中文序数占位完全穿透判定与清洗
# （`_PLACEHOLDER_ANYWHERE_RE` 同理）——用户报告的「专项6」家族里，
# AI 与规则兜底都会产中文数字变体。现两种数字体系都纳入。
_PLACEHOLDER_TITLE_RE = re.compile(r"^专项\s*[0-9一二三四五六七八九十百]+\s*$")

# ---------------------------------------------------------------------------
# 2026-10-10 增强：占位泛化标题识别器扩展
#
# 复现（2026-10-10，54 个三级节点的构造用例；回归见
# tests/test_l3_title_quality_2026_10_10.py::TestL3GenerationEndToEnd）：
#   1) 旧正则 `^专项\d+$` 只能命中「专项6」，完全漏掉「分阶段推进专项13」这类
#      带前缀变体 → `_cleanup_placeholder_titles` 形同虚设；
#   2) 「总体实施策划（2）」「总体实施策划（3）」「总体方案质量管控（第6项）」
#      这类裸编号冲突后缀没有任何拦截点；
#   3) `_uniquify_outline_titles._core()` 归一化不剥离裸序数后缀，
#      「总体方案质量管控」与「总体方案质量管控（第6项）」被判为不重复而双双保留，
#      语义重复无限堆叠；
#   4) L3 规则禁止继承父级核心词，且 generic 域池仅 5 项，池耗尽后只能靠
#      `_semantic_fallback_title` 产出「父级token + 通用维度（第N项）」——
#      这正是「总体实施策划（2）（3）」的直接来源。
# 修复：扩充识别模式 + 引入「工程实体 × 专业维度」组合池，把三级标题落到
# 具体工程实体上；裸编号后缀在归一化阶段剥离，从源头切断堆叠。
# ---------------------------------------------------------------------------

# 任意位置的「专项N」（覆盖「分阶段推进专项13」「总体方案专项6」）
#
# BUG-FIX 2026-10-13（G1 · 中文数字占位漏判）：旧正则只认阿拉伯数字，
# 「专项六」「专项一」「地下室专项三」这类中文序数占位完全穿透判定与清洗
# （`_PLACEHOLDER_TITLE_RE` 同理）——用户报告的「专项6」家族里，AI 与规则
# 兜底都会产中文数字变体，且含实体词的「地下室专项六」连
# `_is_l3_title_project_specific` 的实体词白名单都能穿透，直接存活到落库。
# 现两种数字体系都纳入。
#
# BUG-FIX 2026-10-17（第六轮 · 括号式专项占位漏判）：旧正则要求数字紧邻
# 「专项」，「专项（6）」「专项(6)」这类括号式编号完全穿透判定与清洗
# （对抗探针实证：作为唯一兄弟时原样存活到落库）。现允许括号间隔。
_PLACEHOLDER_ANYWHERE_RE = re.compile(r"专项\s*[（(]?\s*[0-9一二三四五六七八九十百]+")

# BUG-FIX 2026-10-17（第六轮 · 倒序专项占位漏判）：「第六专项」「6专项」
# 「第三章专项」——编号在「专项」**之前**的镜像形态，上面两个正则都只查后置。
# 允许可选「第」前缀（「第六专项」是更常见的 AI 产出形态）。锚定标题开头
# 收敛误判面（L3 层级的「2026专项规划」同样无语义，判脏后由清洗链改写）。
_PLACEHOLDER_PREFIXED_RE = re.compile(r"^第?\s*[0-9一二三四五六七八九十百]+\s*专项")

# BUG-FIX 2026-10-17（第六轮 · 专项+泛化词+尾编号漏判）：「专项措施6」
# 「专项方案6」「专项施工3」——主体是泛化短语、结尾是裸编号。
# `_BARE_NUM_TAIL_RE` 的收窄口径（去数字后须恰为泛化词表成员）放行它们，
# 因为「专项措施」不在 `_GENERIC_ONLY_TITLES`/`_L3_L2LIKE_TITLES` 词表。
# 泛化词显式列举（不放开到任意 CJK），避免误伤「专项施工方案2026」这类
# 带年份的合法标题。
_PLACEHOLDER_TAIL_NUM_RE = re.compile(
    r"专项(?:措施|方案|施工|工程|技术|管理)?\s*[0-9一二三四五六七八九十百]+\s*$"
)

# 纯序号占位标题：「内容3」「子章节2」「章节1」「小节4」…
#
# BUG-FIX 2026-10-12（P1）：`_auto_fix_issues`（workflows_outline_v51.py）在修复
# 「空标题」issue 时的兜底文案就是 `f"内容{k+1}"` / `f"子章节{j+1}"` / `f"章节{i+1}"`。
# 这些标题零语义、且逐章递增重复，本质上与「专项6」是同一类占位垃圾——用户看到的
# 「三级标题大量是占位式泛化名称」里就有它们。此前脏标题判定器完全不认识这形态，
# 于是它们可以绕过 `_cleanup_placeholder_titles` 直接进入最终审核与落库。
# 现纳入判定，让清洗链把它们改写为「工程实体 × 专业维度」的语义化标题。
#
# 正则刻意收紧为「通用结构词 + 纯数字」，避免误伤「第一内容」「内容管理」
# 「第三章施工部署」这类合法标题。
#
# BUG-FIX 2026-10-17（第六轮 · 序号占位家族漏判）：
#   1) 中文数字变体「内容三」「项目六」穿透（旧正则只认阿拉伯数字）；
#   2) 「项目6」未纳入结构词表；
#   3) 「第6项」「第六项」整标题即纯序号——旧 `_ORDINAL_SUFFIX_RE` 要求
#      编号之外还有主体，这类**整体就是编号**的形态无人拦截。
_PLACEHOLDER_ORDINAL_ONLY_RE = re.compile(
    r"^(?:内容|项目|子章节|章节|小节|条|项|节)\s*[0-9一二三四五六七八九十百]{1,3}$"
)

# BUG-FIX 2026-10-17（第六轮）：整标题为「第N项/第N条/N项」式纯序号占位。
# 「第6项」「第六项」「6项」——不带任何主体，语义为零。
_ORDINAL_ITEM_ONLY_RE = re.compile(
    r"^第?\s*[0-9一二三四五六七八九十百]+\s*[项条]$"
)

# BUG-FIX 2026-10-17（第六轮 · 清洗器碎片主体护栏）：`_cleanup_placeholder_titles`
# 的「先剥后缀保主体」分支对 bare fragment 主体会保留出不可读标题——
# 「专项（6）」剥后缀得「专项」、「内容（3）」剥后得「内容」、「项目（2）」
# 剥后得「项目」。这类 1~2 字结构词不是标题，只是被剥空了壳的占位。
# 现对过短主体（< 4 字）或碎片词表成员改走整体语义化重写，
# 与 `_semantic_fallback_title` 的候选口径一致。
_MIN_CLEAN_BODY_LEN = 4
_L3_TITLE_FRAGMENTS = frozenset({
    "专项", "内容", "项目", "章节", "小节", "子章节", "条", "项", "节",
    "措施", "方案", "要点", "要求", "计划", "部署", "工程", "技术",
})
# 裸编号冲突后缀（括号式）：（2）/（3）/（第2项）/（二）/半角 (2)
#
# 2026-10-11 修复：旧正则只认全角括号，导致「总体实施策划(2)」这类半角括号
# 裸编号穿透判定与归一化剥离 —— AI 输出与前端拼接常产半角，于是
# 「总体实施策划」与「总体实施策划(2)」被判为不重复而双双保留，
# 正是用户报告「编号冲突后缀（2）（3）」的漏网形态之一。
#
# 半角括号收紧：要求括号内容全为编号（可带 第/项），故「深基坑(超5m)支护方案」
# 「混凝土(28天)配合比」这类含文字说明的半角括号不会被误剥离。
#
# 「（其N）」不在本正则内：它是**旧版兜底产出的残渣**（不是裸编号），
# 由 `_QI_ORDINAL_SUFFIX_RE` 单独识别并在归一化时一并剥离，
# 从而让「砌体成品保护（其2）」与「砌体成品保护」被判为同名（真正应消歧的形态）。
#
# ⚠️ 本正则**故意不匹配裸数字结尾**（如「总体实施策划2」）。
# 裸数字若纳入剥离，会让 `_uniquify_outline_titles` 的裸数字回退
# `while _core(f"{new_title}{_cnt}") in seen` 对任意 _cnt 都得到同一个 core
# （因为刚加的数字又被剥掉）→ 必然死循环。裸数字结尾的判定改由
# `_BARE_NUM_TAIL_RE` 单独负责（只判定、不剥离），产出方也已改为语义化标题。
_ORDINAL_SUFFIX_RE = re.compile(
    r"[（(]\s*第?\s*[0-9一二三四五六七八九十百ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩ]+\s*项?\s*[）)]\s*$"
)

# BUG-FIX 2026-10-27（L1/L2/L3/L4 · 占位泛化漏检）：以下形态此前穿过
# `_is_dirty_l3_title_deep`，是用户复报「占位式泛化名称 + 编号冲突后缀」的漏网之鱼。
#
# 形态 1：裸中文/罗马序数后缀（无括号、无「之」前缀）——「总体实施策划二」
#   「总体实施策划三」—— 与合法的「总体实施策划之二」（消歧产物）只差一个「之」。
#   旧 `_ORDINAL_SUFFIX_RE` 要求 `[（(]` 括号包裹，裸序数完全穿透；`_BARE_NUM_TAIL_RE`
#   只认阿拉伯数字，中文/罗马序数完全穿透。
# 形态 2：「第N」无括号后缀——「总体实施策划第二」—— 语义上等同「（第二）」/「（二）」，
#   但少了括号让旧正则不匹配。
#
# 口径：
#   - 括号式序号由 `_ORDINAL_SUFFIX_RE` 处理（已扩展到罗马数字 Ⅰ-Ⅹ）；
#   - 「之N」由 `_is_generic_stem_only_title` 刻意豁免（本函数负向后视 `(?!之)` 与
#     剥离步骤保持一致，双保险）；
#   - 剥离后剩余的 base 必须**非空**且属于 `_GENERIC_ONLY_TITLES`/`_L3_L2LIKE_TITLES`
#     才判脏，避免过度拦截「质量管理二」「混凝土三」这类含具体实体的合法标题。
_BARE_ORDINAL_SUFFIX_RE = re.compile(
    r"(?<![之第])"
    r"(?:第[一二三四五六七八九十百ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩ]|"
    r"[一二三四五六七八九十百ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩ]+)\s*$"
)

# 单字符标题整段就是序号（「1」「一」「Ⅱ」）—— 剥离后 base 为空。
# 与 `_PLACEHOLDER_ORDINAL_ONLY_RE` 的区别：后者要求主体词（内容/项目等）+ 数字；
# 本形态整段就是序号、无任何主体，语义为零。
_SINGLE_ORDINAL_TITLE_RE = re.compile(
    r"^(?:第[一二三四五六七八九十百ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩ]|[一二三四五六七八九十百ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩ]+|[0-9]+)$"
)


def _is_generic_stem_ordinal_suffix(title: str) -> bool:
    """检测标题是否以「泛化词干 × 裸序号」形态结尾（无括号、无「之」消歧）。

    BUG-FIX 2026-10-27：用户复报「总体实施策划二」「总体实施策划（Ⅱ）」
    「总体实施策划第二」等形态穿过脏判定，是「编号冲突后缀（3）（2）」的
    镜像漏网。本函数补齐裸中文/罗马序数与「第N」无括号两种漏网形态。

    判定逻辑（幂等，从末尾反复剥离）：
      1. 匹配裸序号（无「之」/「第」前缀，或「第+序数」组合），若剥后剩余
         base 与「之」相连（如「总体实施策划之二」），说明是合法消歧，返回 False；
      2. 反复剥离直至无裸序号匹配；
      3. 最终 base 为空 或 属于泛化词表 → 判脏；否则合法。

    样例（判脏）：
        「总体实施策划二」→ base=「总体实施策划」∈ 泛化词表
        「总体实施策划（Ⅱ）」→ 括号式，由 _ORDINAL_SUFFIX_RE 直接判脏（不进本函数）
        「总体实施策划第二」→ 剥离「第二」得「总体实施策划」
        「1」/「一」→ base 空

    样例（不判脏）：
        「总体实施策划之二」→ 剥离「二」后 base 以「之」结尾 → 合法消歧豁免
        「质量管理二」→ base=「质量管理」不在泛化词表
        「混凝土施工三」→ base 不在泛化词表
    """
    t = (title or "").strip()
    if not t:
        return False
    matched_any = False
    changed = True
    while changed:
        changed = False
        m = _BARE_ORDINAL_SUFFIX_RE.search(t)
        if m and m.group(0).strip():
            matched_any = True
            base = t[: m.start()].strip()
            # 「之+序数」是合法消歧：剥离后 base 以「之」结尾说明原本是「之+序数」形态
            while base.endswith("之"):
                base = base[:-1].strip()
                if not base:
                    break
            t = base
            changed = True
        else:
            break
    if not matched_any:
        # 标题本身不含裸序号后缀 —— 是否泛化由其它规则（如 `_GENERIC_ONLY_TITLES`
        # 直查）判断，本函数只负责「泛化词干 + 裸序号」这一特定形态。
        return False
    # 剥离完毕后：空标题 / 泛化词表成员 → 判脏
    if not t:
        return True
    return t in (_GENERIC_ONLY_TITLES | _L3_L2LIKE_TITLES)

# ========== 2026-10-17 第六轮：编号前缀形态（BUG-OUTLINE-2026-10-17-01） ==========
# 用户复报症状「编号冲突后缀（2）（3）」的**镜像形态**：AI 输出与人工编辑会把
# 编号写到标题**开头**——「（2）总体实施策划」「(3)总体实施策划」
# 「（二）质量保证措施」「2、总体实施策划」「2. 总体实施策划」。
# `_ORDINAL_SUFFIX_RE` 只锚定结尾（`[）)]\s*$`），`_PLACEHOLDER_TITLE_RE` 又要求
# 整标题恰为「专项N」，于是此前缀形态完整穿过清洗链/去重/自检/安全网/落库
# （对抗探针实证：3 处存活且 `_is_dirty_l3_title_deep` 全判非脏）。
#
# 口径与后缀侧严格对称：
#   - 括号式前缀：（2）/（3）/（二）/（第2项）/半角 (2)，括号内容必须全为编号
#     （沿用 `_ORDINAL_SUFFIX_RE` 的收紧规则，不误伤「深基坑（超5m）支护方案」）；
#   - 列表式前缀：「2、」「二、」「2，」与「2. 」（点号要求后跟空白，
#     避免误伤「1.5 目标」这类小数编号）；
#   - 堆叠前缀「（2）（3）X」循环剥离。
# 本命名空间中 DB title 不携带编号（编号按位置现算，见 `renumber_outline_ids`），
# 故任何前导编号都是应剥离的污染：`_is_dirty_l3_title` 判脏，
# `_cleanup_placeholder_titles` 按「先剥前缀保主体，主体仍泛化才重写」处理
# （与后缀侧同口径，见 L1943 附近的对称实现）。
_ORDINAL_PREFIX_BRACKET_RE = re.compile(
    r"^[（(]\s*第?\s*[0-9一二三四五六七八九十百]+\s*项?\s*[）)]"
)
_ORDINAL_PREFIX_LIST_RE = re.compile(
    r"^(?:[0-9一二三四五六七八九十百]{1,4}\s*[、．,，]|[0-9]{1,4}\.\s+)"
)
_ORDINAL_PREFIX_RE = re.compile(
    r"^(?:[（(]\s*第?\s*[0-9一二三四五六七八九十百]+\s*项?\s*[）)]"
    r"|[0-9一二三四五六七八九十百]{1,4}\s*[、．,，]"
    r"|[0-9]{1,4}\.\s+)\s*"
)


def _strip_ordinal_prefix(title: str) -> str:
    """剥离标题开头的裸编号前缀（（2）/（第2项）/2、/2. ），循环处理堆叠形态。

    与 `_strip_ordinal_suffix` 对称：只剥编号、不动主体；无命中原样返回。
    整标题即编号时返回空串（由调用方按空标题处理）。
    """
    t = (title or "").strip()
    while True:
        new = _ORDINAL_PREFIX_RE.sub("", t, count=1).strip()
        if new == t:
            return t
        t = new

# 裸数字结尾「总体实施策划2」/「总体实施策划20」—— 仅用于脏标题判定，不参与归一化剥离。
# 负向断言排除：数字/字母（单位如 mm、m、C30）、百分比、以及任意 CJK 量词
# （天/米/层/根/年/月，如「工期365天」「2026年施工组织计划」）。
# 即：仅当标题以纯数字结尾且数字后无任何字符时才判定为裸编号后缀。
_BARE_NUM_TAIL_RE = re.compile(r"[0-9]{1,4}(?![0-9A-Za-z%％\u4e00-\u9fff])\s*$")


def _is_placeholder_numbered_title(title: str) -> bool:
    """层级无关的「编号式占位」判定（只认编号/序数，不含泛化词规则）。

    BUG-FIX 2026-10-26（L2 占位标题无人拦截）：`_cleanup_placeholder_titles`
    与 `_is_dirty_l3_title` 只作用于 **level>=3**，理由是「其他/概述/说明」
    这类词在 L1/L2 合法。但「专项方案2」「内容3」「第6项」「总体实施策划（2）」
    这类**带裸编号/序数后缀**的标题在 L2 同样无语义，此前完全穿过
    `_post_process_title_quality`（其 L2 判定只有 8 条锚定正则 + 「（第N项）」
    结尾）→ 二级审核界面直接展示，落库链路也不清洗。
    本函数刻意只覆盖编号类形态（不碰泛化词），避免误伤 L1/L2 合法词。
    """
    t = (title or "").strip()
    if not t:
        return True
    if (
        _PLACEHOLDER_ANYWHERE_RE.search(t)
        or _PLACEHOLDER_PREFIXED_RE.search(t)
        or _PLACEHOLDER_TAIL_NUM_RE.search(t)
        or _PLACEHOLDER_ORDINAL_ONLY_RE.match(t)
        or _ORDINAL_ITEM_ONLY_RE.match(t)
    ):
        return True
    # 括号式 / 列表式编号后缀与前缀（（2）/（第3项）/2、/（二））
    if _ORDINAL_SUFFIX_RE.search(t) or _ORDINAL_PREFIX_RE.match(t):
        return True
    # 裸数字结尾（「总体实施策划2」）：剥数字后若主体是泛化词才是占位，
    # 避免误伤「工期365天」（CJK 量词结尾不匹配）与「C30 混凝土」这类合法标题。
    if _BARE_NUM_TAIL_RE.search(t):
        _rest = _BARE_NUM_TAIL_RE.sub("", t).strip()
        if not _rest or _rest in (_GENERIC_ONLY_TITLES | _L3_L2LIKE_TITLES):
            return True
    # 泛化词干 + 裸序号（无括号、无「之」）—— 与 L3 脏判定保持同口径，
    # 确保 L2 审核界面也会拦截「总体实施策划二」「总体实施策划第三」这类
    # 编号冲突形态，而不是只在三级被清。
    if _is_generic_stem_ordinal_suffix(t):
        return True
    # 整段就是序号的标题（「1」「一」「Ⅱ」）
    if _SINGLE_ORDINAL_TITLE_RE.match(t):
        return True
    return False


# 纯泛化标题词表：标题恰好等于其中之一即判定为占位泛化
_GENERIC_ONLY_TITLES = frozenset({
    "总体实施策划", "总体部署", "总体安排", "总体方案", "总体思路", "总体规划",
    "总体实施", "总体管理", "总体工作", "总体计划",
    "实施要点", "基本要求", "主要内容", "主要措施", "主要工作", "主要内容安排",
    "工作要点", "关键内容", "核心内容", "实施内容", "工作内容",
    "专项方案", "通用方案", "整体方案", "基本方案", "实施方案", "基本措施",
    "其他", "补充", "相关", "若干", "说明", "概述",
})

# L2 形态短语：语义完整、粒度等同于二级章名，落到三级会读起来像章名而非条目，
# 且极易与其它章节的 L2 标题撞名（如「资源配置与协调保障」既是 L2 池短语，
# 也曾以 L3 身份出现在「质量管理措施」章下）。
# 单独成表而非并入 _GENERIC_ONLY_TITLES：后者含「其他」「说明」「概述」这类
# 在 L1/L2 完全合法的词，语义不同，混在一起会让维护者误判清洗作用域。
#
# 2026-10-10 收窄：原表含「质量保证组织体系」「质量管理组织体系」，但这两个
# 短语语义具体（描述明确的质量组织体系/管理组织架构），作为三级条目完全可读、
# 合法，属过度拦截（over-filter）——回归测试
# test_does_not_false_positive_on_concrete_titles 明确断言其为具体标题。
# 即便其与某些章节 L2 同名，跨层撞名也由 _uniquify_outline_titles 全局去重兜底，
# 不应在脏标题判定阶段预先判死（否则会误删合法内容）。
# 本表只保留「总体方案与实施策划」这类纯泛化、读起来像章名而非条目的短语。
_L3_L2LIKE_TITLES = frozenset({
    # generic 域
    "总体方案与实施策划", "总体实施策划", "分阶段推进计划",
    "资源配置与协调保障", "过程控制与考核评价", "过程控制与考核机制",
    "总结评估与持续改进",
    # 2026-10-27 新增（BUG-OUTLINE-2026-10-27-L2POOL）：以下条目曾出现在
    # _L3_SUFFIX_POOL 中，但与 _L2_SUFFIX_POOL 完全相同，导致 L3 直接吐出 L2 章名。
    # 现从 L3 池替换为动作型条目，同时在此拦截旧数据中残留的 L2 级 L3 标题。
    "组织架构与职责分工", "管理制度与考核机制",
    "进度计划与资源保障", "成品保护与现场管理",
    "目标成本分解与控制", "成本核算与偏差分析",
    # 2026-10-27 第三轮新增（BUG-OUTLINE-2026-10-27-CROSS-L1）：
    # 以下条目是 _L2_SUFFIX_POOL 中的 L2 级章名，在 docx 实测中以 L3 身份出现
    # （由 _l3_entity_candidates 的实体×维度组合产出，或 L3 池残留）。
    # 这些标题读起来像章名而非条目，应拦截。
    # 注：「质量保证组织体系」刻意不拦截——回归测试 test_does_not_false_positive_on_concrete_titles
    # 明确断言其为具体标题（语义可读、合法），属过度拦截。
    "沟通协调机制", "信息化管理平台建设",
    "总体施工部署与区段划分", "主要分项工程施工方案",
    "主要分部工程施工方案", "关键工序施工技术措施",
    "专项施工方案与技术措施", "专项施工方案与工艺",
    "质量控制与验收要求",
    "安全管理组织体系", "安全防护与技术措施",
    "应急预案与演练计划", "隐患排查与治理机制",
    "文明施工与环保措施", "创优目标分解与实施",
    "质量通病防治措施", "进度计划编制与平衡",
    "关键线路分析与控制", "资源投入与保障措施",
    "合同与索赔管理", "质量检查与考核机制",
    "施工测量与试验检测", "季节性施工措施",
    "施工机械与设备保障",
    # 2026-10-27 第三轮扩容新增
    "安全防护与文明施工措施", "检验试验与资料管理",
    "施工准备与临建工程", "施工界面与专业协调",
    "竣工资料与验收移交", "质量目标与奖惩制度",
    "质量培训与技术交底", "安全教育与培训制度",
    "劳动保护与职业卫生", "进度动态监测与纠偏",
    "施工进度考核与奖惩", "进度计划执行与跟踪",
    "成本管理组织体系", "资金计划与动态调配",
    "变更签证与索赔处理", "人员配置与培训计划",
    "绩效考核与奖惩制度", "BIM 应用与三维协调",
    "施工工艺与作业指导", "实施风险识别与应对",
    "责任落实与节点跟踪",
})


def _strip_ordinal_suffix(title: str) -> str:
    """剥离标题末尾的裸编号后缀（（2）/（第3项）/半角 (2)），用于语义去重归一化。

    不含「（其N）」—— 那是旧版兜底留下的残渣，由 `_strip_legacy_redundant_suffix`
    单独剥离（且只在 `_title_core` 归一化时生效，不改变 `_is_dirty_l3_title` 的
    判定语义）；也不含裸数字结尾（「总体实施策划2」），否则会导致
    `_uniquify_outline_titles` 的裸数字回退死循环。裸数字结尾由
    `_is_dirty_l3_title` 单独判定。
    """
    if not title:
        return title
    return _ORDINAL_SUFFIX_RE.sub("", title).strip()


# 「（其N）」/「（其 2）」/半角「(其2)」—— 旧版去重兜底产出的**无意义序数后缀**。
# 形如「砌体成品保护（其2）」「机电管线材料管理实施细则（其2）」，
# 对用户完全无信息量（用户 2026-10-11 反馈的目录污染即此形态）。
# 刻意不并入 _ORDINAL_SUFFIX_RE：那条正则服务于「（2）/（第3项）」这类**裸**编号，
# 两者语义不同，合并会让 _is_dirty_l3_title 把「（其N）」误判为泛化占位。
_QI_ORDINAL_SUFFIX_RE = re.compile(r"[（(]\s*其\s*[0-9一二三四五六七八九十百]+\s*[）)]\s*$")

# 中文序数用字
_CN_DIGITS = ("零", "一", "二", "三", "四", "五", "六", "七", "八", "九")


def _cn_two_digits(n: int) -> str:
    """10~99 → 中文十位段（供百/千位组合复用，形如「一十」「二十三」「五十」）。

    百/千位组合里保留「一十」而非「十」：「一百一十」读起来明确，且与个位
    「十一」的省略写法不冲突（后者由 `_cn_ordinal` 的独立分支处理）。
    """
    tens, ones = divmod(n, 10)
    return _CN_DIGITS[tens] + "十" + (_CN_DIGITS[ones] if ones else "")


def _cn_ordinal(n: int) -> str:
    """阿拉伯序数 → 中文序数（覆盖 1~9999，全区间输出纯中文）。

    加固 2026-10-15（D6 · 阿拉伯数字泄漏）：旧实现 `n >= 100` 直接
    `return str(n)`，而三处消歧兜底的起点恰好都落在 100 以上 ——
    `_disambiguate_title` 的有界扫描收口于 `之{_cn_ordinal(101)}`，
    `_uniquify_outline_titles` 的防御式循环从 `_n = 101` 起步，
    `build_chapter7_framework` 的兜底从 `_guard = 101` 起步。于是同级重名
    条目较多时会产出「总体实施策划之101」这类**阿拉伯数字混入中文标题**的
    形态：既不可读，也让「之N」的脏判定豁免名存实亡（形态与「（2）」裸编号
    同类，等于兜底函数产出了自家定义的脏格式）。现补齐十/百/千位与「零」
    衔接规则（GB/T 15835 形式：101→一百零一、1010→一千零一十、2005→两千零五），
    全区间不再返回阿拉伯数字。> 9999 仍回落 `str(n)`：同一父级下不可能有
    如此多兄弟节点，仅作最后防线保留可见性。
    """
    try:
        n = int(n)
    except (TypeError, ValueError):
        return "一"
    if n < 1:
        n = 1
    if n > 9999:
        return str(n)
    if n <= 10:
        # n=10 需特判：_CN_DIGITS 只有下标 0~9
        return "十" if n == 10 else _CN_DIGITS[n]
    if n < 20:
        return "十" + _CN_DIGITS[n % 10]
    if n < 100:
        return _cn_two_digits(n)
    out = ""
    if n >= 1000:
        out = _CN_DIGITS[n // 1000] + "千"
    rest = n % 1000
    r2 = rest % 100
    if 0 < rest < 100:  # 千位后直接是个位/十位 → 需「零」衔接
        out += "零"
    hundreds = rest // 100
    if hundreds:
        out += _CN_DIGITS[hundreds] + "百"
        if 0 < r2 < 10:  # 百位后直接是个位 → 需「零」衔接
            out += "零"
    if r2 >= 10:
        out += _cn_two_digits(r2)
    elif r2:
        out += _CN_DIGITS[r2]
    return out


def _strip_legacy_redundant_suffix(title: str) -> str:
    """剥离历史遗留的冗余序数后缀（「（其N）」等），保留标题主体。

    用于清洗**已生成/已落库**的目录：旧版兜底产出的「（其N）」既不是
    「（2）/（第3项）」式裸编号（_ORDINAL_SUFFIX_RE 不识别），也不是有意义
    的语义表述，纯粹是去重失败留下的残渣。剥掉后交由消歧逻辑重新分配，
    标题主体（业务语义）完整保留。

    幂等：无命中则原样返回；命中则循环剥离，覆盖「（其2）（其3）」这类堆叠。
    """
    t = (title or "").strip()
    while True:
        new = _QI_ORDINAL_SUFFIX_RE.sub("", t).strip()
        if new == t:
            return t
        t = new


def _title_core(title: str) -> str:
    """标题归一化**唯一正本**：去重与消歧判断「两个标题是否同名」时必须共用。

    步骤：剥离编号**前缀**（（2）/2、）→ 剥离裸编号后缀（（2）/（第3项））→
    剥离冗余序数后缀（（其N））→ 去空白 → 小写 → 剔除非词字符。

    ⚠️ 历史缺陷（2026-10-11 修复）：`_disambiguate_title` 曾用**原始字符串**
    去比对**归一化 core 集合**，「X（其2）」永远不等于集合里的「X其2」，
    于是它的序数计数器 `_n` 恒停在 2，上层再包一层后滚成「（其3）（其4）…」
    的递增污染。现在两侧统一走本函数，比对口径一致。

    BUG-FIX 2026-10-17（L-4 · 前缀不参与归一化）：前缀剥离此前缺失，
    「（2）X」与「X」被判为不同标题。生产链路里 `_cleanup_placeholder_titles`
    先于去重执行、会把前缀剥掉，故此缺口只在**绕过清洗的路径**
    （`_uniquify_outline_titles` 被单独调用、遗留数据、人工编辑回灌）
    暴露：前缀污染标题与干净同名标题双双保留，语义重复。现与后缀侧对称
    纳入，保证「判重口径」与「清洗口径」严格一致。
    """
    tt = _strip_ordinal_prefix(title or "")
    tt = _strip_ordinal_suffix(tt)
    tt = _strip_legacy_redundant_suffix(tt)
    tt = tt.strip().lower()
    tt = re.sub(r"[\s\u3000]+", "", tt)
    tt = re.sub(r"[^\w\u4e00-\u9fff]", "", tt)
    return tt


def _clean_legacy_suffix_tree(level1: list[dict]) -> int:
    """就地清洗目录树中所有节点的遗留冗余后缀，返回被清洗的节点数。

    只处理三级及以下：一/二级章节标题是用户的正式章节名，
    且「（其N）」几乎只会出现在三级（去重兜底只作用于此）。
    """
    fixed = 0

    def _walk(nodes: list, level: int) -> None:
        nonlocal fixed
        for node in nodes:
            if not isinstance(node, dict):
                continue
            if level >= 3:
                title = node.get("title") or ""
                cleaned = _strip_legacy_redundant_suffix(title)
                if cleaned != title:
                    if cleaned:
                        node["title"] = cleaned
                        fixed += 1
                    else:
                        logger.warning("清洗冗余后缀后标题为空，保留原标题: %s", title)
            children = node.get("children")
            if isinstance(children, list):
                _walk(children, level + 1)

    if isinstance(level1, list):
        _walk(level1, 1)
    return fixed


def _is_dirty_l3_title(title: str) -> bool:
    """判定标题是否为占位/泛化脏标题（三级标题质量收口统一入口）。

    判定标准（任一命中即视为脏标题）：
      1. 空标题
      2. 精确占位（「专项6」）
      3. 任意位置含「专项N」（「分阶段推进专项13」）
      4. 结尾带裸编号后缀（「总体实施策划（2）」「总体实施策划(2)」）
      5. 结尾带裸数字且去掉数字后仍为泛化词（「总体实施策划2」）
         —— 只判定不剥离；且必须收窄，见下方说明
      6. 标题恰好等于纯泛化词（「总体实施策划」）
      7. 纯序号占位（「内容3」「子章节2」「章节1」）
      8. 「泛化词干 + 泛化维度词」组合（「总体实施策划专项方案」
         「总体实施策划补充措施」「总体方案施工工艺」「实施策划质量控制」）
         —— 消歧分支与本章实体抽取的共同产物，语义含量为零；
            由 `_is_generic_stem_only_title` 判定，
            config.OUTLINE_L3_GENERIC_STEM_GUARD_ENABLED=false 时关闭。
      9. 编号前缀形态（「（2）总体实施策划」「2、总体实施策划」）
         —— 用户复报的「编号冲突（2）（3）」的镜像形态：编号在标题**开头**。
            本命名空间 DB title 不携带编号（编号按位置现算），任何前导编号
            都是污染；且前缀 + 泛化主体正是零语义占位。判脏后由
            `_cleanup_placeholder_titles` 按「先剥前缀保主体」处理。
      10. 专项占位家族扩展（「专项（6）」「第六专项」「专项措施6」）
         —— 括号式/倒序/泛化词+尾编号三种变体，旧正则只查「专项+紧邻数字」。
      11. 纯序号占位家族扩展（「项目6」「第六项」「第6项」）
         —— 中文数字、「项目」结构词、整标题即序号三种漏判形态。
      """
    t = (title or "").strip()
    if not t:
        return True
    if _PLACEHOLDER_TITLE_RE.match(t):
        return True
    if _PLACEHOLDER_ANYWHERE_RE.search(t):
        return True
    if _PLACEHOLDER_PREFIXED_RE.search(t):
        return True
    if _PLACEHOLDER_TAIL_NUM_RE.search(t):
        return True
    if _PLACEHOLDER_ORDINAL_ONLY_RE.match(t):
        return True
    if _ORDINAL_ITEM_ONLY_RE.match(t):
        return True
    if _ORDINAL_SUFFIX_RE.search(t):
        return True
    if _BARE_NUM_TAIL_RE.search(t):
        # 2026-10-11 收窄：裸数字结尾**不能一律判脏**，否则会把 AI 生成的合法标题
        # 「工序交接流程5」（5 是流程序号，有实际语义）误判为占位并拒收，
        # 导致被拒后原始脏标题反而被保留（见 test_concurrency_is_bounded_by_config）。
        # 仅当去掉尾部数字后的主体本身是泛化词时才判脏 —— 那才是真正的
        # 「泛化词 + 消歧残留数字」形态，而非具有业务语义的编号。
        _rest = _BARE_NUM_TAIL_RE.sub("", t).strip()
        if _rest and _rest in (_GENERIC_ONLY_TITLES | _L3_L2LIKE_TITLES):
            return True
    if t in _GENERIC_ONLY_TITLES:
        return True
    if t in _L3_L2LIKE_TITLES:
        return True
    # 8. 「泛化词干 + 泛化维度词」组合（2026-10-15 第五轮）：
    #    「总体实施策划专项方案」「总体方案施工工艺」「实施策划质量控制」。
    #    这类标题是消歧分支（`_disambiguate_title` 优先级 1）与本章实体抽取
    #    （`_title_entity_fragments` 未过滤泛化段）共同产出的第三副占位面孔，
    #    语义含量为零，且**不含任何项目实体**。规则 6/7 的「恰好等于」口径放行
    #    它们，导致清洗链与消歧产物脏复检双双失效（见 config.py 同名开关注释）。
    if _is_generic_stem_only_title(t):
        return True
    # 9. 编号前缀形态（2026-10-17 第六轮 · BUG-OUTLINE-2026-10-17-01）：
    #    「（2）总体实施策划」「2、总体实施策划」「(3)总体实施策划」。
    #    与后缀侧（规则 4）对称：本命名空间 DB title 不携带编号，前导编号
    #    一律是污染；且前缀 + 泛化主体正是用户复报的「编号冲突（2）（3）」
    #    镜像症状。判脏后 `_cleanup_placeholder_titles` 先剥前缀保主体，
    #    主体仍泛化才整体重写（与后缀侧同口径）。
    if _strip_ordinal_prefix(t) != t:
        return True
    # 12. 「泛化词干 + 裸序号」形态（2026-10-27 · BUG-OUTLINE-2026-10-27-L1~L4）：
    #     「总体实施策划二」「总体实施策划第三」—— 无括号、无「之」的裸序数后缀，
    #     与合法的「总体实施策划之二」消歧只差一个「之」字。这是用户复报的
    #     「总体实施策划（2）」「总体实施策划（3）」去掉括号后的镜像漏网形态：
    #     `_ORDINAL_SUFFIX_RE` 要求 `[（(]` 包裹，裸序数完全穿透旧判定。
    #     「（Ⅱ）/（Ⅲ）」罗马数字括号由本规则前一步 `_ORDINAL_SUFFIX_RE` 兜住
    #     （已在字符类扩展到 Ⅰ-Ⅹ），「第N」无括号由本函数一并处理。
    #     「之N」合法消歧由 `_is_generic_stem_only_title` 与本函数各自豁免，双保险。
    if _is_generic_stem_ordinal_suffix(t):
        return True
    # 13. 单字符/整段序号标题（2026-10-27 · BUG-OUTLINE-2026-10-27-L4）：
    #     「1」「一」「Ⅱ」等整段就是序号、无任何主体的标题，语义为零。
    #     与 `_PLACEHOLDER_ORDINAL_ONLY_RE`（要求主体词+数字）与
    #     `_ORDINAL_ITEM_ONLY_RE`（要求「项/条」量词）都不同——纯序号单段
    #     是第三种形态，旧判定完全放行。
    if _SINGLE_ORDINAL_TITLE_RE.match(t):
        return True
    return False


def _is_dirty_l3_title_deep(title: str) -> bool:
    """深度脏判定：原文 + 剥离冗余后缀后的形态都过一遍 `_is_dirty_l3_title`。

    BUG-FIX 2026-10-13（G2 · 「（其N）」残渣漏判）：`_QI_ORDINAL_SUFFIX_RE`
    此前只参与 `_title_core` 归一化（去重/消歧口径），`_is_dirty_l3_title`
    本身不查它。后果：唯一存在的「总体实施策划（其三）」既不会被
    `_cleanup_placeholder_titles` 改写（原文不判脏），也不会有同名兄弟触发
    `_uniquify_outline_titles` 的消歧 → 占位泛化标题原样流出到审核与落库。

    设计取舍：**不把「（其N）」本身判脏**。「砌体成品保护（其2）」这类
    「合法标题 + 历史残渣」的正确修法是剥后缀保留主体（`_clean_legacy_suffix_tree`
    的职责），整体替换反而丢业务语义。因此深度判定只对**剥离后**的形态生效：
      - 「砌体成品保护（其2）」→ 剥离后非脏 → 不清洗（后续统一剥后缀）；
      - 「总体实施策划（其三）」→ 剥离后命中泛化词表 → 清洗为语义化标题。
    """
    if _is_dirty_l3_title(title):
        return True
    _t = (title or "").strip()
    if not _t:
        return False
    _stripped = _strip_legacy_redundant_suffix(_t)
    if _stripped != _t and _is_dirty_l3_title(_stripped):
        return True
    return False


# 三级标题工程实体词池（按语义域）：标题必须落到具体工程实体，
# 而非只写「质量管控」这类抽象管理词。
_L3_ENTITY_WORDS: dict[str, tuple[str, ...]] = {
    "work": (
        "地下室", "主体结构", "屋面", "外墙", "基坑", "砌体", "门窗", "楼地面",
        "脚手架", "幕墙", "机电管线", "混凝土", "钢筋", "模板", "土方", "桩基",
        "防水", "保温", "装饰", "钢结构", "装配式", "吊顶", "抹灰", "涂料",
        "电气", "给排水", "暖通", "消防", "施工缝", "后浇带",
    ),
    "quality": (
        "样板区", "关键工序", "隐蔽工程", "首件工程", "分项工程", "见证取样",
        "旁站监理", "实测实量", "成品", "观感质量", "工序交接", "工序检验",
        "试验室", "标准养护", "检测见证",
    ),
    "safety": (
        "临边洞口", "高处作业", "临时用电", "动火作业", "塔吊", "有限空间",
        "危化品", "应急物资", "消防栓", "脚手架", "防护棚", "安全通道",
        "安全警示", "消防设施",
    ),
    "schedule": (
        "关键线路", "里程碑节点", "总控计划", "月进度计划", "周进度计划",
        "交叉作业", "流水施工", "劳动力", "机械台班", "材料供应", "节点验收",
        "工序穿插",
    ),
    "cost": (
        "目标成本", "变更签证", "索赔事项", "资金计划", "量价分离", "分包结算",
        "材料采购", "机械租赁", "人工单价", "成本台账",
    ),
    "org": (
        "项目管理部", "专业分包", "劳务班组", "岗位职责", "例会制度", "技术交底",
        "绩效考核", "人员培训", "组织机构", "管理体系",
    ),
    "tech": (
        "施工工艺", "技术交底", "样板引路", "首件验收", "试验检测", "深化设计",
        "BIM模型", "智慧工地", "新技术", "工艺评定", "作业指导", "工序验收",
    ),
    "generic": (
        "阶段目标", "资源投入", "协调接口", "过程管控", "考核评价", "总结改进",
        "任务分解", "责任落实", "计划编制", "动态跟踪", "风险清单", "验收交付",
    ),
}


# 三级标题短动作词池（按语义域）：与实体词拼接后必须读得通，
# 因此限定为 2~4 字的管理/执行动作，而非「施工工艺流程与要点」这类完整短语。
_L3_SHORT_ACTIONS: dict[str, tuple[str, ...]] = {
    "work": (
        "施工工艺", "质量控制", "安全防护", "技术交底", "验收标准", "操作要点",
        "施工准备", "成品保护", "材料管理", "过程监控", "检测试验", "记录归档",
        "环境保护", "工序交接",
    ),
    "quality": (
        "质量控制", "检测试验", "验收标准", "记录归档", "整改闭环", "样板引路",
        "旁站监督", "见证取样", "实测实量", "成品保护",
    ),
    "safety": (
        "安全防护", "技术交底", "隐患排查", "整改闭环", "教育培训", "应急演练",
        "消防管理", "职业健康", "危险源辨识",
    ),
    "schedule": (
        "进度计划", "节点控制", "资源配置", "交叉作业", "流水施工", "动态跟踪",
        "偏差纠正", "考核评价",
    ),
    "cost": (
        "成本核算", "变更签证", "索赔管理", "资金计划", "分包结算", "材料采购",
        "量价管控",
    ),
    "org": (
        "组织架构", "岗位职责", "例会制度", "绩效考核", "人员培训", "管理体系",
        "制度落实",
    ),
    "tech": (
        "施工工艺", "技术交底", "样板引路", "首件验收", "试验检测", "深化设计",
        "工艺评定", "作业指导",
    ),
    "generic": (
        "总体实施", "阶段推进", "资源配置", "过程控制", "考核评价",
    ),
}


# 过程 / 制度类名词：本身已是完整语义单元，与另一个动作词直接拼接会产生
# 「作业指导样板引路」「工序交接实测实量」「见证取样检测试验」这类生硬标题
# ——两个过程名被粘在一起，读者看不出谁修饰谁。
# 这类词改用 _L3_PROCEDURE_DIMS 拼接（要求 / 要点 / 流程 / 标准…），
# 产出「作业指导要点」「工序交接流程」「见证取样要求」这类自然标题。
_L3_PROCEDURE_WORDS = frozenset({
    "技术交底", "工艺评定", "作业指导", "工序验收", "样板引路", "工序交接",
    "工序检验", "首件验收", "见证取样", "检测见证", "旁站监理", "旁站监督",
    "实测实量", "观感质量", "试验检测", "检测试验", "深化设计",
    "例会制度", "岗位职责", "绩效考核", "人员培训", "组织机构", "管理体系",
    "制度落实", "任务分解", "责任落实", "计划编制", "动态跟踪", "风险清单",
    "验收交付", "阶段目标", "资源投入", "协调接口", "过程管控", "考核评价",
    "总结改进", "量价分离", "分包结算",
})

# 过程/制度类名词的维度后缀：全部为「名词性维度词」，不与过程词重复语义。
_L3_PROCEDURE_DIMS: tuple[str, ...] = (
    "要求", "要点", "流程", "标准", "措施", "细则", "方案", "实施",
    "检查", "记录", "控制", "交底", "验收", "培训", "部署",
)
# ========== 泛化词干 × 维度词组合识别（2026-10-15 第五轮） ==========
# 「总体实施策划专项方案」「总体方案施工工艺」「实施策划质量控制」这类标题由两部分
# 构成：泛化词干（本身即章名级泛化短语，不携带工程实体）+ 泛化维度词（消歧/兜底
# 维度池里的通用执行维度）。两者相加语义含量为零，与「专项6」「总体实施策划（2）」
# 同属占位式泛化名称，但既有判定的「恰好等于泛化词」口径完全放行它们 —— 于是
# 2026-10-15 的「消歧产物脏复检」和清洗链都形同虚设（详见 config.py 同名开关注释
# 与 backend/scripts/diag_l3_leaks.py 的 D1/D2）。
_GENERIC_STEM_WORDS: frozenset[str] = frozenset(
    _GENERIC_ONLY_TITLES | _L3_L2LIKE_TITLES | {
        # 泛化父级指代词中未出现在上面两个集合里的条目（≥3 字，
        # 「总体」「方案」「措施」这类 ≤2 字短词刻意不纳入：
        # 会把「总体技术路线」误判为泛化词干 + 泛化维度词）。
        "管理措施", "总体施工部署", "施工部署", "总体策划", "工程实施",
        "项目管理", "施工组织", "分阶段推进", "推进计划", "阶段推进",
        "实施策划", "工程质量保证",
    }
)

_GENERIC_STEM_DIMENSIONS: frozenset[str] = frozenset(
    tuple(_DISAMBIG_DIMENSIONS)
    + tuple(_L3_FALLBACK_DIMENSIONS)
    + tuple(_L3_PROCEDURE_DIMS)
    + tuple(_L3_SUFFIX_POOL["generic"])
    + tuple(_L3_SHORT_ACTIONS["generic"])
    + tuple(_L3_SHORT_ACTIONS["work"])
    + tuple(_L3_SHORT_ACTIONS["quality"])
    + tuple(_L3_SHORT_ACTIONS["safety"])
    + tuple(_L3_SHORT_ACTIONS["schedule"])
    + tuple(_L3_SHORT_ACTIONS["cost"])
    + tuple(_L3_SHORT_ACTIONS["org"])
    + tuple(_L3_SHORT_ACTIONS["tech"])
)


def _generic_stem_guard_enabled() -> bool:
    """读 config.OUTLINE_L3_GENERIC_STEM_GUARD_ENABLED（惰性读取以支持测试打桩）。"""
    from app.config import OUTLINE_L3_GENERIC_STEM_GUARD_ENABLED  # noqa: PLC0415

    return OUTLINE_L3_GENERIC_STEM_GUARD_ENABLED


def _is_generic_stem_only_title(title: str) -> bool:
    """判定标题是否为「泛化词干 × 泛化维度词」组合（不含任何项目实体）。

    命中样例（形态 A · 词干 + 单个维度词）：
        「总体实施策划专项方案」「总体方案施工工艺」「实施策划质量控制」
        「总体方案检测试验」「实施策划记录归档」「工程质量保证检测试验」
    命中样例（形态 B · 连续堆叠多个泛化词干）：
        「总体方案总体实施策划专项方案」「总体方案与实施策划质量控制」

    不命中样例：「混凝土施工工艺」「样板区质量控制」「见证取样要点」
              「专项方案编制与评审」「施工工艺流程与要点」
              「总体实施策划之一」（中文序数消歧产物，刻意豁免：
              `_uniquify_outline_titles` 的消歧兜底分支必然产出「X之N」，
              若判脏会与其构成互斥循环）。

    形态 B 的判定口径刻意收敛为「**连续**泛化词干」，不使用维度词参与堆叠：
    否则「见证取样要点」（维度 + 维度，语义完整的制度类标题）会被误判。

    由 config.OUTLINE_L3_GENERIC_STEM_GUARD_ENABLED 控制，设为 false 时恒为 False。
    """
    if not _generic_stem_guard_enabled():
        return False
    t = (title or "").strip()
    if not t:
        return False
    if "之" in t:  # 中文序数消歧产物豁免（见 docstring）
        return False

    stems_desc = sorted(_GENERIC_STEM_WORDS, key=len, reverse=True)

    # 形态 B：连续剥除泛化词干，堆叠 ≥2 个即判脏
    rest = t
    n_stems = 0
    while True:
        for stem in stems_desc:
            if rest.startswith(stem):
                rest = rest[len(stem):].strip()
                n_stems += 1
                break
        else:
            break
    if n_stems >= 2:
        return True

    # 形态 A：泛化词干 + 恰好一个泛化维度词（整段被完全分解）
    for stem in stems_desc:
        if not t.startswith(stem):
            continue
        tail = t[len(stem):].strip()
        if tail and tail in _GENERIC_STEM_DIMENSIONS:
            return True
    return False




def _l3_dims_for(entity: str, dimensions: tuple[str, ...]) -> tuple[str, ...]:
    """按实体词类型选择维度池：过程/制度类名词走 _L3_PROCEDURE_DIMS。

    返回空元组表示该实体词不应参与组合（调用方跳过）。
    """
    if entity in _L3_PROCEDURE_WORDS:
        return _L3_PROCEDURE_DIMS
    return dimensions


# 三级标题兜底时从祖先标题切实体词需剥掉的「章名级」虚词尾巴：
# 「盘扣支撑体系方案」的实体是「盘扣支撑体系」，整段当实体会拼出
# 「盘扣支撑体系方案质量控制」这类不通顺标题。按长度降序匹配。
_TITLE_ENTITY_NOISE_SUFFIXES = tuple(
    sorted(
        (
            "专项施工方案", "专项方案", "施工方案", "施工方法", "技术措施",
            "管理计划", "管理体系", "实施策划", "推进计划", "协调保障", "考核机制",
            "持续改进", "方案", "计划", "措施", "管理", "要求", "标准",
            "制度", "预案", "部署", "策划", "体系", "机制", "保障", "设计",
            "交付", "要点",
        ),
        key=len,
        reverse=True,
    )
)


# 章节片段判为「泛化词干（非工程实体）」的**宽口径**词表 —— 仅供
# `_title_entity_fragments` 过滤使用，不参与脏标题判定（后者必须严格收窄以避免
# 误伤「混凝土施工工艺」这类合法标题）。
#
# 这里刻意比 `_GENERIC_STEM_WORDS` 更宽：把 L2 维度名（资源配置/协调保障/
# 过程控制/考核评价…）与章名级组织词（项目管理/施工组织/管理体系…）都算进去。
# 过滤掉的后果只是回落到语义域词池（`_L3_ENTITY_WORDS`，产出「样板区质量控制」
# 这类具体实体标题），不会产生任何标题质量问题；而不过滤则会把
# 「总体方案」「实施策划」这类泛化片段当成实体拼出「总体方案施工工艺」。
_GENERIC_FRAGMENT_WORDS: frozenset[str] = frozenset(
    _GENERIC_STEM_WORDS
    | {
        "资源配置", "协调保障", "过程控制", "考核评价", "总结改进",
        "阶段目标", "资源投入", "协调接口", "任务分解", "责任落实",
        "计划编制", "动态跟踪", "风险清单", "验收交付", "制度落实",
        "管理体系", "组织架构", "岗位职责", "例会制度", "绩效考核",
        "人员培训", "教育培训", "危险源辨识", "项目管理机构",
        "总体技术", "工程概况", "工程特点", "施工部署", "总体安排",
        "工程质量保证", "工程质量", "工程总平面", "工程总控",
    }
)


def _is_generic_entity_fragment(seg: str) -> bool:
    """判定章名片段是否为泛化词干（非工程实体，不得参与三级标题拼接）。

    例：「总体方案」「实施策划」「资源配置」「协调保障」「工程质量保证」→ True；
        「悬挑花篮架」「盘扣支撑体系」「试验检验」「计量管理」→ False。

    判断口径：片段本身是泛化词干，或剥掉**一层**章名级虚词后是泛化词干
    （「工程质量保证」剥「保证」得「工程质量」——两者都登记为泛化词干）。
    只剥一层与 `_title_entity_fragments` 的剥离口径保持一致。
    """
    if not _generic_stem_guard_enabled():
        return False
    s = (seg or "").strip()
    if not s or not re.search(r"[一-鿿]", s):
        return False
    if s in _GENERIC_FRAGMENT_WORDS:
        return True
    for suf in _TITLE_ENTITY_NOISE_SUFFIXES:
        if s.endswith(suf) and len(s) - len(suf) >= 3:
            if s[: -len(suf)] in _GENERIC_FRAGMENT_WORDS:
                return True
            break
    return False


def _title_entity_fragments(*titles: str) -> tuple[str, ...]:
    """从祖先标题切出**本章专属**的工程实体词，供三级标题兜底优先使用。

    2026-10-15 新增，修复实体错配：`_l3_entity_candidates` 此前只按语义域从
    全局词池取实体，而 `_DOMAIN_KEYWORDS` 缺一大批专业词（悬挑花篮架、盘扣
    支撑体系、试验计量、资料与 BIM 交付…），这些章被判 generic 后落回 work 池，
    于是在「悬挑花篮架与盘扣支撑体系方案」章下产出「地下室施工工艺」。
    实测 21 章 × 5 维度骨架上错配 70/525 个三级标题。
    本函数直接从祖先标题切实体，使兜底标题与所在章主题对应。

    切分：按连接符（与/及/、/，/／/空格）切段 → 剥首部编号与「第N章」前缀 →
    剥**一层**尾部章名级虚词 → 保留 ≥3 字且含 CJK 的片段（2 字片段拼维度词后
    易退化成「质量控制」这类无指代短语）。最多返回 4 个，按出现顺序。

    只剥一层：虚词本身常是复合实体的一部分，剥到不动点会把实体掏空 ——
    「盘扣支撑体系方案」剥「方案」得「盘扣支撑体系」后再剥「体系」会得到
    「盘扣支撑」，拼出的「盘扣支撑技术交底」丢了本体指代。
    """
    frags: list[str] = []
    for raw in titles:
        title = (raw or "").strip()
        if not title:
            continue
        for seg in re.split(r"[与及、，,/／\s　]+", title):
            seg = re.sub(r"^[0-9一二三四五六七八九十百零]+[、.．)）]", "", seg).strip()
            seg = re.sub(r"^第[0-9一二三四五六七八九十百零]+[章节部分]", "", seg).strip()
            for suf in _TITLE_ENTITY_NOISE_SUFFIXES:
                if seg.endswith(suf) and len(seg) - len(suf) >= 3:
                    seg = seg[: -len(suf)]
                    break
            seg = seg.strip()
            if len(seg) >= 3 and re.search(r"[一-鿿]", seg) and seg not in frags:
                # 2026-10-15 第五轮（D1 核心根因）：章名本身是泛化短语时
                # （「总体方案与实施策划」「资源配置与协调保障」「工程质量保证
                # 措施」），切出的「总体方案」「实施策划」「资源配置」「协调保障」
                # 「工程质量保证」会被当成"本章专属实体"，与维度词拼出
                # 「总体方案施工工艺」「实施策划质量控制」这类语义含量为零的
                # 占位式泛化标题 —— 实测在生产形态的 6 章 × 5 节点骨架上，
                # L3 全部是这种形态（backend/scripts/diag_l3_leaks.py 的 D4/D5）。
                # 过滤后回落语义域词池，产出「样板区质量控制」这类具体标题。
                if _is_generic_entity_fragment(seg):
                    continue
                frags.append(seg)
    return tuple(frags[:4])


def _l3_entity_candidates(
    parent_title: str,
    idx: int,
    used: set[str],
    extra_entities: tuple[str, ...] | None = None,
    context_title: str = "",
) -> list[str]:
    """生成「工程实体 × 专业维度」候选三级标题列表。

    与 `_derive_child_title(level=3)` 的规则 3（禁止继承父级核心短语）不冲突：
    实体词取自语义域词池，而非父标题字符串本身，因此不会把 L2 标题整段复制到 L3。

    Args:
        parent_title: 父级（L2）标题，用于判定语义域
        idx: 当前节点序号（用于轮转）
        used: 已使用标题集合（跨章节全局）
        extra_entities: 调用方额外提供的实体词（如从 L1/L2 提取的域关键词）
        context_title: 祖级标题（如 L1），L2 为通用型时用于兜底判定语义域

    Returns:
        按优先级排序、尚未使用的候选标题列表
    """
    # 语义域判定：L2 常为通用型（「总体方案与实施策划」），此时把 L1 一起纳入
    # 判定上下文，使 L3 继承所在一级章的专业领域（L1=质量管理措施 → quality 域）。
    ctx = f"{context_title or ''} {parent_title or ''}".strip()
    dom = _detect_domain(ctx)
    # 仍无法判定时落到具体施工实体域。
    # 「generic」域的实体与维度都是完整短语（阶段目标 / 总体实施策划），
    # 互相拼接会产出「阶段目标总体实施策划」这类不通顺的伪标题。
    if dom == "generic":
        dom = "work"
    # 调用方未显式提供实体词时，以**祖级（L1）标题**切出的本章实体兜底。
    # 只取 L1 不取 L2：2026-09-04 用户定制规则 3 禁止三级继承二级核心短语。
    if extra_entities is None and context_title:
        extra_entities = _title_entity_fragments(context_title)
    dom_entities = _L3_ENTITY_WORDS.get(dom) or _L3_ENTITY_WORDS["work"]
    # 调用方提供的实体词（所在章的专属实体，见 `_title_entity_fragments`）
    # 排在域池之前：先用章内实体组合，耗尽后才落到通用域池。旧实现把两者
    # 直接拼接成同一个轮转序列，章内实体只有 1/30 的概率被选中，导致
    # 「悬挑花篮架」章批量产出「地下室施工工艺」这类与本章无关的三级标题。
    chapter_entities = tuple(
        e for e in (extra_entities or ()) if e and e not in dom_entities
    )
    # 2026-10-27 第四轮（P2 · L3 与 L2 主题对应）：提取 **L2 专属实体**，
    # 使 L3 标题与父级 L2 主题语义相关（修复 docx 实测 66% 的 L2-L3 语义脱节）。
    #
    # 规则 3 禁止 L3 继承 L2 **核心短语**（`_core_for_derive` 的结果），但允许
    # 使用 L2 中的其他具体实体。例：L2「土方与基坑支护工程」→ 核心词「土方」，
    # 实体「基坑支护」非核心词，可作为 L3 实体 →「基坑支护施工工艺」。
    # 单实体 L2（如「脚手架工程」→ 实体=核心词「脚手架」）会被过滤，
    # 回落到域池，不违反规则 3。
    #
    # 过滤链：
    #   1. `_title_entity_fragments` 已过滤泛化词段（总体方案/实施策划等）；
    #   2. 过滤已在域池的实体（避免与域池重复）；
    #   3. 过滤含 L2 核心词的实体（规则 3 严格口径）。
    _l2_core = _core_for_derive(parent_title)
    l2_entities = tuple(
        e
        for e in _title_entity_fragments(parent_title)
        if e
        and e not in dom_entities
        and not (_l2_core and _l2_core in e)
        and not (context_title and _l2_core and _l2_core in context_title)
    )
    # 维度使用短动作词池（2~4 字），与实体词拼接后读得通；
    # 不用 _L3_SUFFIX_POOL（那里是「施工工艺流程与要点」这类完整短语，
    # 与实体词拼接会产生「样板引路施工工艺流程与要点」伪标题）。
    dimensions = tuple(_L3_SHORT_ACTIONS.get(dom, ()) or _L3_SHORT_ACTIONS["work"])
    if not dimensions:
        dimensions = tuple(_L3_SHORT_ACTIONS["work"])

    out: list[str] = []
    # 过程/制度类实体的维度池（_L3_PROCEDURE_DIMS）与动作池长度不同，
    # 轮转步长取两者上限，保证组合空间不因池切换而缩水。
    dim_cap = max(len(dimensions), len(_L3_PROCEDURE_DIMS))
    entity_stages: list[tuple[str, ...]] = []
    if chapter_entities:
        entity_stages.append(chapter_entities)
    # P2: L2 专属实体排在域池之前，保证 L3 与 L2 主题对应。
    if l2_entities:
        entity_stages.append(l2_entities)
    entity_stages.append(dom_entities)
    for entities in entity_stages:
        ent_len = len(entities)
        if not ent_len:
            continue
        for offset in range(ent_len * dim_cap):
            # 实体索引步进 (idx*5) 与维度索引步进 (idx) 互质，使同一章的兄弟
            # 三级标题落在不同实体上；维度索引步进 (offset) 保证同实体的动作也分散。
            e = entities[(idx * 5 + offset) % ent_len]
            dims = _l3_dims_for(e, dimensions)
            if not dims:
                continue
            d = dims[(idx + offset) % len(dims)]
            if e in d or d in e:  # 完全叠词
                continue
            # 二元组重叠过滤：拦掉「旁站监理+旁站监督」「工序验收+首件验收」
            # 这类共享核心词、读起来像在说同一件事的伪标题
            if _has_bigram_overlap(e, d):
                continue
            # 双过程词过滤：拦掉「作业指导样板引路」「工序交接实测实量」这类
            # 两个过程名直接粘在一起、读不出修饰关系的生硬标题。
            # 过程类实体改由 _l3_dims_for 提供名词性维度，此处为双保险。
            if e in _L3_PROCEDURE_WORDS and d in _L3_PROCEDURE_WORDS:
                continue
            cand = f"{e}{d}"
            if cand in used or cand in out or _is_dirty_l3_title(cand):
                continue
            out.append(cand)
            if len(out) >= 40:
                return out
    return out


def _short_parent_token(parent_title: str) -> str:
    """提取父级标题的简短指代词（≤6 字），用于语义化兜底前缀。

    优先取「域关键词」（如 脚手架/用电/防水…）保持与父级主题对应；
    否则回落核心词（_core_for_derive 已含长度裁剪），并剥除首部编号
    （「一、」「1、」「（1）」等）与顿号，最后兜底为「工程」。
    """
    if not parent_title:
        return "工程"
    for _dom, _kws in _DOMAIN_KEYWORDS.items():
        for _kw in _kws:
            if _kw in parent_title and len(_kw) >= 2:
                return _kw
    _core = _core_for_derive(parent_title)
    # 剥除首部中文/阿拉伯编号与顿号（如「一、概述…」→「概述…」）
    _core = re.sub(r"^[一二三四五六七八九十百零\d]+[、.．)]", "", _core or "").strip("、 ")
    if _core and _core != "工程" and len(_core) <= 6:
        return _core
    return "工程"


# 父级指代词本身即泛化短语时，禁止再拼维度词（旧实现此处必然产出
# 「总体方案质量管控（第N项）」这类占位泛化标题，是本次修复的核心症状）
_GENERIC_PARENT_TOKENS = frozenset({
    "工程", "总体", "方案", "措施", "管理", "部署", "组织", "计划", "安排",
    "实施", "技术", "管理措施", "总体方案", "总体部署", "总体安排", "总体实施",
    "总体管理", "总体计划", "总体安排", "总体实施策划", "总体施工部署",
    "实施策划", "施工部署", "分阶段推进", "推进计划", "阶段推进", "总体策划",
    "工程实施", "项目管理", "施工组织",
})


def _semantic_fallback_title(
    parent_title: str, idx: int, used: set[str] | None = None, context_title: str = ""
) -> str:
    """语义化兜底标题：永不产出「专项N」/「（第N项）」等占位脏标题。

    优先级：
      - 父级指代词是专业实体词（脚手架/防水/混凝土…）→ 指代词 + 专业执行维度
      - 父级指代词本身是泛化短语（总体方案/工程/部署…）→ 工程实体 × 短动作词
      两种路径都耗尽后，用序位词收口（仍不产出裸编号后缀）。
    返回前会把所选标题登记进 used（自洽，避免调用方漏登记导致重复）。
    """
    if used is None:
        used = set()
    _token = _short_parent_token(parent_title)
    _token_generic = _token in _GENERIC_PARENT_TOKENS or len(_token) <= 2
    if _token_generic:
        # 2026-10-10 主路径：父级泛化时落到具体工程实体
        # （组合空间约 30 实体 × 14 维度 = 420，足够支撑数十个具体标题）
        for _cand in _l3_entity_candidates(
            parent_title, idx, used, context_title=context_title
        ):
            used.add(_cand)
            return _cand
    else:
        _pool = _L3_FALLBACK_DIMENSIONS
        _start = idx % len(_pool)
        for _offset in range(len(_pool)):
            _dim = _pool[(_start + _offset) % len(_pool)]
            _cand = f"{_token}{_dim}"
            if _cand not in used and not _is_dirty_l3_title(_cand):
                used.add(_cand)
                return _cand
    # 上一路径耗尽 → 另一路径
    if _token_generic:
        _pool = _L3_FALLBACK_DIMENSIONS
        for _offset in range(len(_pool)):
            _dim = _pool[(idx + _offset) % len(_pool)]
            _cand = f"{_token}{_dim}"
            if _cand not in used and not _is_dirty_l3_title(_cand):
                used.add(_cand)
                return _cand
    else:
        for _cand in _l3_entity_candidates(
            parent_title, idx, used, context_title=context_title
        ):
            used.add(_cand)
            return _cand
    # 极端兜底 —— 2026-10-10 起不再产出「（第N项）」裸编号冲突后缀
    # （旧实现此处必然产出脏标题，是「总体实施策划（2）/（3）」的直接来源）
    _ordinals = ("之一", "之二", "之三", "之四", "之五", "之六", "之七", "之八")
    _dim = _L3_FALLBACK_DIMENSIONS[idx % len(_L3_FALLBACK_DIMENSIONS)]
    for _o in _ordinals:
        _cand = f"{_token}{_dim}{_o}"
        if _cand not in used and not _is_dirty_l3_title(_cand):
            used.add(_cand)
            return _cand
    # Step 4：绝对兜底（理论上不可达：Step 1 组合空间远大于单章节点数）
    # 2026-10-11 修复：旧实现 `f"{_token}{_pool[_]}{_n}"` 追加阿拉伯数字，
    # 产出「总体实施策划专项方案2」这类裸数字结尾标题 —— 恰好被
    # `_BARE_NUM_TAIL_RE` 判为脏标题，即兜底函数产出了自家定义的脏格式。
    # 改为「维度词 × 中文序数」全组合，穷尽后退回「第N条」表述
    # （结尾是 CJK 量词，不触发脏判定），保证唯一且始终可读。
    _ordinals_all = (
        "之一", "之二", "之三", "之四", "之五",
        "之六", "之七", "之八", "之九", "之十",
    )
    for _dim in _L3_FALLBACK_DIMENSIONS:
        for _o in _ordinals_all:
            _cand = f"{_token}{_dim}{_o}"
            if _cand not in used and not _is_dirty_l3_title(_cand):
                used.add(_cand)
                return _cand
    _n = 1
    while True:
        _cand = f"{_token}{_pool[idx % len(_pool)]}第{_n}条"
        if _cand not in used:
            used.add(_cand)
            return _cand
        _n += 1


def _parent_tokens(parent_title: str, title: str) -> list[str]:
    """提取父级标题的候选指代词（优先级降序），用于重名消歧前缀。

    与 `_short_parent_token` 的区别：它刻意避开 `title` 已有的词面，
    专挑父级**独有**的语义片段 —— 这样「地下施工工艺」在不同二级章下
    重复时，靠「资源配置 / 过程控制 / 总结评估」这类父级差异就能自然
    区分，而不是靠追加「（其N）」式无意义后缀。

    候选来源（按优先级）：
      1. 父级简短指代词（域关键词优先，如「脚手架」）；
      2. 父级命中的全部域关键词；
      3. 剥掉与 `title` 公共前缀后剩下的父级语义片段
         （「专业分包资源配置与协调保障」vs「专业分包地下施工工艺」→「资源配置」）。
    """
    if not parent_title or not title:
        return []
    _norm = re.compile(r"[（()）,，、。：；！？/\s\u3000]+")
    base = _norm.sub("", title or "")
    if not base:
        return []
    out: list[str] = []

    def _push(tok: str) -> None:
        tok = _norm.sub("", tok or "")
        if len(tok) < 2 or len(tok) > 4 or tok in base or tok in out:
            return
        out.append(tok)

    _push(_short_parent_token(parent_title))
    for _kws in _DOMAIN_KEYWORDS.values():
        for _kw in _kws:
            if len(_kw) >= 2 and _kw in parent_title:
                _push(_kw)
    # 剩余语义片段：先剥掉「第1章/一、」这类编号，再剥掉与 base 的公共前缀
    _core = _extract_title_core(parent_title)
    _core = re.sub(r"^第?\s*[一二三四五六七八九十百零\d]+\s*[章节部分编期]?", "", _core or "").strip()
    _core = re.sub(r"^[一二三四五六七八九十百零\d]+[、.．)]", "", _core).strip("、 ")
    _core = _norm.sub("", _core)
    if _core:
        _i = 0
        while _i < len(_core) and _i < len(base) and _core[_i] == base[_i]:
            _i += 1
        _rem = re.sub(r"^[与和及、,，:：/\s\u3000]+", "", _core[_i:]).strip()
        if _rem:
            _push(_rem[:4])
    return out


def _disambiguate_title(
    title: str, used: set[str] | None = None, parent_title: str = ""
) -> str:
    """对重名标题做语义化消歧：永不产出「（其N）」/「（2）」式冗余后缀。

    ⚠️ `used` 的比对口径：候选先经 `_title_core` 归一化再比对，因此调用方
    无论传「归一化 core 集合」还是「原始标题集合」都能正确判定。
    旧实现用**原始字符串**去比对**归一化 core 集合**，「X（其2）」永远不等于
    集合里的「X其2」，序数计数器恒停在 2，上层再包一层后滚成
    「（其3）（其4）（其5）」的递增污染（用户 2026-10-11 反馈的目录形态）。

    优先级（组合空间由小到大，前序产物可读性最好）：
      1) 追加专业执行维度词：总体实施策划 → 总体实施策划专项方案；
      2) 父级差异词前缀 + 维度词 / 仅父级差异词前缀
         （脚手架总体实施策划）—— 组合空间 = 父级词 × 维度词，远大于任一
         单侧池，专门应对《招投标项目》样例里通用标题被反复复用的情况，
         且天然与父级章节对应；
      3) 中文序数兜底：总体实施策划之三。不带括号，`_ORDINAL_SUFFIX_RE` 与
         `_BARE_NUM_TAIL_RE` 都不会识别它，故每个序数的 core 都不同，
         必然收敛，不会重演归一化剥离导致的死循环。
    注意：本函数不负责把结果登记进 used，由调用方登记。

    2026-10-15 第五轮（D2）：分支 1/2 的候选必须过一道脏判定（`_ok`），
    否则泛化主体「总体实施策划」会拿到「总体实施策划专项方案/补充措施/实施
    方案/保障措施」—— 语义含量为零，且此前逃过 `_is_dirty_l3_title`（规则
    6/7 只按"恰好等于"比对），导致 `_uniquify_outline_titles` 的消歧产物脏
    复检形同虚设。分支 3 的「X之N」不查脏判定：`_is_generic_stem_only_title`
    对含「之」的标题刻意豁免，查了也不影响收敛性。
    """
    if used is None:
        used = set()
    # 加固：输入本身可能带裸编号或历史冗余后缀（「总体实施策划（2）」
    # 「砌体成品保护（其2）」）。先全部剥离再消歧，避免残留后缀被挪到标题中间
    # 而让 `_clean_legacy_suffix_tree` 的末尾匹配失效。仅剥离不登记。
    #
    # 2026-10-17 第六轮（BUG-OUTLINE-2026-10-17-01）前缀侧对称：输入还可能带
    # **开头**编号（「（2）总体实施策划」「2、总体实施策划」）。旧实现只剥后缀，
    # 前缀残留会参与消歧拼接，产出「（2）总体实施策划专项方案」这类把编号挪到
    # 中间的形态——既逃过 `_clean_legacy_suffix_tree` 的末尾匹配，又让
    # `_title_core` 判重失真。入口统一剥前缀后再消歧。
    title = _strip_ordinal_prefix(title or "")
    title = _strip_ordinal_suffix(title)
    title = _strip_legacy_redundant_suffix(title)
    if _BARE_NUM_TAIL_RE.search(title):
        _rest = _BARE_NUM_TAIL_RE.sub("", title).strip()
        if _rest and _rest in (_GENERIC_ONLY_TITLES | _L3_L2LIKE_TITLES):
            title = _rest
    if not title:
        return title

    def _free(cand: str) -> bool:
        return _title_core(cand) not in used

    def _ok(cand: str) -> bool:
        """候选可用：不重名 **且不是** 泛化词干+泛化维度词的空壳标题。"""
        return _free(cand) and not _is_dirty_l3_title_deep(cand)

    # 1) 专业执行维度词（泛化主体跳过：追加维度词只会得到另一副空壳面孔）
    #    「总体实施策划」→ 若直接追加维度词会得到「总体实施策划专项方案」，
    #    语义含量为零；改用中文序数兜底，再由 `_uniquify_outline_titles` 的
    #    消歧产物脏复检改写成语义化标题。
    _generic_base = title in (_GENERIC_STEM_WORDS | _GENERIC_ONLY_TITLES | _L3_L2LIKE_TITLES)
    # BUG-FIX 2026-10-15（D1）：旧的「恰好等于」口径漏掉了『泛化词干+泛化维度词』
    # 拼接出来的泛化标题，因此追加维度词会产出「总体实施策划专项方案」这类
    # 两头都泛化的新脏标题。此处改用 `_is_generic_stem_only_title` 的深判定。
    if (
        not _generic_base
        and _generic_stem_guard_enabled()
        and _is_generic_stem_only_title(title)
    ):
        _generic_base = True

    if not _generic_base:
        for _d in _DISAMBIG_DIMENSIONS:
            if title.endswith(_d):
                continue
            _cand = f"{title}{_d}"
            if _ok(_cand):
                return _cand

        # 2) 父级差异词前缀（组合空间 = 父级词 × 维度词）
        for _tok in _parent_tokens(parent_title, title):
            # 2026-10-15 第五轮（D2）：`_parent_tokens` 的「剩余语义片段」分支
            # 会把「总体方案与实施策划」裁成「方案与实」这类无意义 2 字碎片，
            # 拼出「方案与实总体实施策划专项方案」。要求前缀是 ≥3 字的完整词，
            # 且不含连接虚字；碎片前缀直接跳过。
            _tk_core = re.sub(r"[与和及、,，的\s　]+", "", _tok)
            if len(_tk_core) < 3 or re.search(r"[与和及、,，的\s　]", _tok):
                continue
            for _d in _DISAMBIG_DIMENSIONS:
                if _d in title:
                    continue
                _cand = f"{_tok}{title}{_d}"
                if _ok(_cand):
                    return _cand
            if _ok(f"{_tok}{title}"):
                return f"{_tok}{title}"

    # BUG-FIX 2026-10-15（D1）：泛化主体跳过维度词与父级碎片，改走主体专属
    # 语义池。旧实现两条退路都会产出空壳标题：
    #   - 「总体实施策划」+ 维度词  →「总体实施策划专项方案」（两头都泛化）
    #   - 「总体实施策划」+ 中文序数 →「总体实施策划之四」（只有序数无语义）
    # 语义池产出「总体施工技术交底 / 总体施工样板引路」这类真正可读的标题，
    # 组合空间（30 实体 × 14 维度）远大于单章节点数，不会退化。
    if _generic_base:
        # 注意两点：
        #  1) 必须传 `used` 的**副本** —— `_semantic_fallback_title` 会把选中的
        #     标题写回其入参集合，而本函数的 `_free()` 以 `used` 判重，直传会
        #     让候选被自己刚写入的条目判为重名，静默退回到纯序数兜底；
        #  2) 选中后**不要**把 core 写回 `used` —— 调用方
        #     （`_uniquify_outline_titles`）自己负责登记，这里抢先登记会被
        #     其防御式去重循环误判为撞名，反而退回「X之101」。
        _cand = _semantic_fallback_title(
            parent_title or "", len(used), set(used), context_title=title
        )
        if _ok(_cand):
            return _cand

    # 3) 中文序数兜底（有界扫描，永不产出「（其N）」）
    #    2026-10-15 加固：旧实现扫描 2..100、收口写死 `_cn_ordinal(101)`，而旧
    #    `_cn_ordinal` 在 n≥100 直接 `return str(n)` —— 于是「扫描用尽」这个正常
    #    可达的分支会产出「X之101」，这是该无义形态的唯一上游（同级重名条目
    #    超过 100 即必然触发，与 `_DISAMBIG_DIMENSIONS` 组合空间无关）。
    #    现扫描与收口统一区间，且 `_cn_ordinal` 已补齐百/千位输出纯中文。
    for _n in range(2, 1000):
        _cand = f"{title}之{_cn_ordinal(_n)}"
        if _free(_cand):
            return _cand
    return f"{title}之{_cn_ordinal(1000)}"


def _cleanup_placeholder_titles(
    level1: list[dict], global_used: set[str] | None = None
) -> int:
    """兜底清洗：将任何遗留的占位标题（专项N / 裸「实施要点」）改写为语义化标题。

    遍历整棵树，对命中占位模式的节点，依据其父级标题用 `_semantic_fallback_title`
    重新生成唯一标题。返回被清洗的节点数。
    """
    _fixed = 0
    _used = global_used if global_used is not None else set()
    # BUG-FIX 2026-10-13（G10 · 清洗序号恒 0）：旧实现 `_idx = len(children)`，
    # 而三级被洗节点几乎都是叶子（children 恒空）→ 所有被洗节点都以 idx=0 调
    # `_semantic_fallback_title`，候选轮转起点完全相同，仅靠 used 累加保证不重，
    # 多样性严重受损（同章多个脏标题会拿到同一实体词的不同维度）。
    # 改为按「本棵树内已清洗计数」轮转，跨父级也连续错开。
    _clean_seq = 0

    def _collect(nodes: list) -> None:
        for _n in nodes:
            if not isinstance(_n, dict):
                continue
            if _n.get("title"):
                _used.add(_n["title"].strip())
            # 2026-10-15 修复（B1 遗留 · 播种不完整）：旧实现只收集入参这一层
            # （即 L2），三级及以下的既有标题从未进入 `_used`。于是
            # 「悬挑架安装」+「悬挑架安装（2）」这对兄弟被清洗时，剥后缀得到的
            # 主体不在 used 里 → 直接落成一个与新兄弟同名的标题；同父撞名虽然
            # 后续 `_uniquify_outline_titles` 能兜住，但清洗链在落库侧位于
            # 去重**之后**，重复标题会漏到终态。改为整子树递归播种。
            _collect(_n.get("children") or [])

    def _walk(
        nodes: list, parent_title: str = "", grandparent_title: str = "", level: int = 1
    ) -> None:
        nonlocal _fixed, _clean_seq
        for _node in nodes:
            if not isinstance(_node, dict):
                continue
            _title = (_node.get("title") or "").strip()
            # 2026-10-10 修复：脏标题清洗只作用于三级及以下。
            # 旧实现无条件套用占位判定，但「其他」「概述」「说明」这类词
            # 同为 28 章模板的合法一级/二级标题（FIXED_28_CHAPTERS[27]="其他"），
            # 若一并清洗会误伤合法章节标题。
            # 2026-10-13 修复（G2）：判定升级为深度判定（含「（其N）」剥离后
            # 仍为泛化词的形态），见 `_is_dirty_l3_title_deep`。
            _is_placeholder = level >= 3 and _is_dirty_l3_title_deep(_title)
            if _is_placeholder:
                # 2026-10-15 修复（B1 · 清洗器过度剥离）：
                # 「悬挑架安装（2）」这类标题的主体**完全具体**，只是带了个
                # 冲突后缀；旧实现命中脏判定后整段替换为词池标题（实测变成
                # 「技术交底要点」），把 AI 生成的项目针对性丢掉了 —— 清洗器
                # 自己成了泛化标题的来源之一。
                # 现按「先剥后缀保主体，主体仍泛化才重写」处理，
                # 与 `_clean_legacy_suffix_tree` 对「（其N）」的处理口径一致。
                #
                # 2026-10-17 第六轮（BUG-OUTLINE-2026-10-17-01）前缀侧对称：
                # 「（2）总体实施策划」「2、总体实施策划」的编号在**开头**，
                # 同样先剥前缀保主体。另加碎片主体护栏：「专项（6）」剥后缀
                # 得「专项」、「内容（3）」剥后得「内容」—— bare fragment
                # 不是可读标题，旧实现直接保留等于把一种占位垃圾换成另一种，
                # 现改走整体语义化重写。
                _body = _strip_legacy_redundant_suffix(_strip_ordinal_suffix(_title)).strip()
                _body = _strip_ordinal_prefix(_body).strip()
                if (
                    _body
                    and _body != _title
                    and len(_body) >= _MIN_CLEAN_BODY_LEN
                    and _body not in _L3_TITLE_FRAGMENTS
                    and not _is_dirty_l3_title_deep(_body)
                    and _body not in _used
                ):
                    _node["title"] = _body
                    _used.add(_body)
                    _clean_seq += 1
                    _fixed += 1
                else:
                    _new = _semantic_fallback_title(
                        parent_title or "", _clean_seq, _used, context_title=grandparent_title
                    )
                    _node["title"] = _new
                    _used.add(_new)
                    _clean_seq += 1
                    _fixed += 1
            _sub = _node.get("children")
            if isinstance(_sub, list):
                _walk(_sub, _title, parent_title, level + 1)

    # 2026-10-11 修复（O-9）：旧实现只收集 L1 的 children（L2+），遗漏 L1 自身。
    # 清洗后的 L3 标题可能与同项目 L1 标题撞名。现先收集 L1 自身标题。
    for _ch in level1:
        if isinstance(_ch, dict) and _ch.get("title"):
            _used.add(_ch["title"].strip())
            _collect(_ch.get("children", []) or [])
    _walk(level1)
    return _fixed


def _derive_child_title(
    parent_title: str, idx: int, used: set[str], level: int, grandparent_title: str = ""
) -> str:
    """从上一级目录标题语义派生子标题（保证对应性 + 多样性 + 不重名）。

    **2026-09-04 用户定制规则 3 适配**：
      - level=3：**完全独立生成，不继承父级二级的任何核心短语**
        - 禁用前缀拼合（prefix_core = ""）
        - 域池剔除所有包含父级核心短语的条目（如父级 core="质量保证"
          → "质量追溯与资料归档""质量通病防治"等域内条目全部淘汰，
          只保留"过程检查与验收标准""样板引路与首件验收"这类真独立条目）
      - level=2：保留父级一级的核心词拼合（规则 3 只约束 L3+）

    Args:
        parent_title: 上一级目录标题（level=3 时仅用于识别语义域 + 提取屏蔽短语）
        idx: 子节点序号（轮转用）
        used: 已使用标题集合（本章内）
        level: 2 或 3
        grandparent_title: 祖父级标题（level=3 时为 L1，用于兜底判定语义域）
    """
    parent_title = (parent_title or "").strip() or "工程实施"
    dom = _detect_domain(parent_title)
    pool = _L3_SUFFIX_POOL[dom] if level == 3 else _L2_SUFFIX_POOL[dom]
    generic_pool = _L3_SUFFIX_POOL["generic"] if level == 3 else _L2_SUFFIX_POOL["generic"]
    core = _core_for_derive(parent_title)

    # === 规则 3 核心改动：L3 完全独立 ===
    if level == 3:
        # 2026-10-10 增强：仅当语义域判为 generic（父级 L2 是通用型标题）时，
        # 优先走「工程实体 × 短动作词」组合 —— 此时规则池只有 5 项 generic 短语，
        # 耗尽后旧实现必然退回到「总体实施策划（2）/（3）」这类占位脏标题。
        # 父级本身专业（work/quality/safety…）时保留原有过滤池路径，
        # 那里的标题已是完整可读的独立条目，实体拼接反而冗余。
        if dom == "generic":
            for _cand in _l3_entity_candidates(
                parent_title, idx, used, context_title=grandparent_title
            ):
                return _cand
        # 禁用父级词汇的前缀拼合
        prefix_core = ""
        # 域池剔除含父级核心短语的条目（严格语义隔离）
        def _filter_phrases(src: tuple[str, ...]) -> list[str]:
            if not core:
                return list(src)
            return [s for s in src if core not in s]

        pool_filtered = _filter_phrases(pool)
        generic_filtered = _filter_phrases(generic_pool)
        # 2026-10-10 增强：域池耗尽时优先用「工程实体 × 短动作词」组合，
        # 而不是直接吐出 generic 池里的完整短语（「总体实施策划」
        # 「分阶段推进计划」「资源配置与协调保障」）。那 5 个短语本质是
        # L2 级章名，落到 L3 会与别的章节 L2 撞名，且读起来像章名而非条目。
        _entity_cands = _l3_entity_candidates(
            parent_title, idx, used, context_title=grandparent_title
        )
        # 主域池耗尽时再用 generic 池补充（先过滤再补充）
        if not pool_filtered and not generic_filtered:
            # 极端兜底：核心短语屏蔽后两池全空 → 退回未过滤池，
            # 避免节点生成失败；此时仍无前缀拼合，语义解耦基本成立
            full_pool = _ordered_unique(list(pool) + list(_entity_cands) + list(generic_pool))
        else:
            full_pool = _ordered_unique(
                list(pool_filtered) + list(_entity_cands) + list(generic_filtered)
            )
    else:
        prefix_core = _usable_l2_prefix_core(core, dom)
        full_pool = _ordered_unique(list(pool) + list(generic_pool))

    def _candidate(sfx: str) -> str:
        if prefix_core and not _has_bigram_overlap(prefix_core, sfx) and prefix_core not in sfx:
            return f"{prefix_core}{sfx}"
        return sfx

    start = idx % len(full_pool)
    for offset in range(len(full_pool)):
        cand = _candidate(full_pool[(start + offset) % len(full_pool)])
        if cand != parent_title and cand not in used:
            # level=3 最后一道保险：候选仍不得含父级核心短语
            if level == 3 and core and core in cand:
                continue
            # BUG-FIX 2026-10-13（G4 · 主循环无脏判定复检）：旧实现只查
            # parent/used/core 三重条件。各池条目当前虽均不命中脏词表，但
            # 这是一处「缺最后防线」的设计缺口——任何新增池条目若恰好等于
            # `_GENERIC_ONLY_TITLES`/`_L3_L2LIKE_TITLES` 成员（如把
            # 「总体实施策划」加回 generic 池），脏标题会从这里直接漏出，
            # 且绕过 `_cleanup_placeholder_titles` 之前的所有检查。
            # 补一道统一脏判定，与 `_semantic_fallback_title` 的候选口径一致。
            if level == 3 and _is_dirty_l3_title(cand):
                continue
            return cand
    # 极端兜底：所有池都耗尽（level=3 不再追加父级前缀）
    # 2026-10-09 增强：改用语义化兜底（父级指代词 + 专业执行维度），
    # 永不产出「专项{n}」占位，且仍保证全局唯一。
    return _semantic_fallback_title(parent_title, idx, used)


def _generate_fallback_l3_for_ensure(l2_title: str, idx: int, l2_index: int = 0) -> str:
    """为 _ensure_min_children 生成专业的兜底三级标题。

    从 L2 标题按语义域派生：不同 L2 核心词不同 → 标题不重名。

    BUG-FIX 2026-10-12（修正陈旧描述）：原 docstring 称「前缀核心词注入 →
    三级与父级二级目录语义关联」，但 2026-09-04 用户定制规则 3 已在
    `_derive_child_title` 的 level==3 分支把 `prefix_core` 置空 —— 三级标题
    **不再**注入父级前缀，改由域池过滤 + 「工程实体 × 专业维度」组合保证
    语义具体性。保留的错误描述会让后来者以为规则 3 未生效。

    注：本函数目前**无生产调用方**（`_ensure_min_children` 走的是
    `_derive_child_title(..., level=3)` 直调），属历史遗留包装。
    """
    return _derive_child_title(l2_title or "工程实施", idx, set(), level=3)


# ========== 全局标题去重 ==========

# `_uniquify_outline_titles` 语义化重命名的有界重试次数。
# 步长 7 与 `_l3_entity_candidates` 内部「实体步进 idx*5」互质，使相邻重试落在
# 不同实体×维度组合上，而不是在同一个候选上反复空转。
_DEDUPE_SEED_TRIES = 12


def _uniquify_outline_titles(level1: list[dict]) -> list[dict]:
    """同级标题去重安全网（V5.1 共用）。

    **2026-10-11 政策修正：由「全树全局唯一」改为「同级唯一」。**

    原因：标书目录的合法形态就是同一个三级条目在不同二级章下重复出现
    （如「地下施工工艺」同时出现在「资源配置与协调保障」「过程控制与考核机制」
    「总结评估与持续改进」下）—— 靠父级章节编号区分即可，Word 自动目录里
    会呈现为 3.1 / 4.1 / 5.1，完全正常。旧的全局唯一策略为消除这类**合法**
    重复，不断追加「专项方案/补充措施」等无意义维度词，最终退化为
    「（其3）（其4）（其5）」式冗余后缀（用户 2026-10-11 反馈的目录污染）。

    现在的规则：
      1. 只保证**同一父节点下的兄弟标题**互不重名 —— 同级重名才是真缺陷；
      2. 不同父节点下的同名标题原样保留；
      3. 先清洗历史遗留的「（其N）」等冗余后缀，使**已落库**的目录同样被修复；
      4. 只重命名「重复」的标题，唯一标题保持不动；
      5. 幂等：重复调用不产生二次改名。

    注：2026-09-04 用户定制规则 3（L3-L5 不得继承父级词汇）仍由
    `_derive_child_title` 在**生成**阶段执行；本函数只在**兜底消歧**时才
    使用父级差异词前缀，且已先尝试专业维度词。

    2026-10-15 加固（D6）追加一条不变式：**被改名的产物既非泛化脏标题、
    也不含阿拉伯数字序数**。旧实现在语义池耗尽时产出「总体实施策划之101」
    （见 `_dedupe_siblings` 内的加固注释），该形态既无信息量又逃过了脏判定
    豁免规则。现由 `_cn_ordinal` 全区间纯中文 + 有界 seed 重试共同保证。
    """
    _clean_legacy_suffix_tree(level1)

    def _dedupe_siblings(nodes: list[dict], parent_title: str = "") -> None:
        seen: set[str] = set()
        # 兄弟原始标题：语义化兜底需要按原始字符串判重（`used` 口径），
        # 而 seen 存的是归一化 core，两者不能混用。
        sibling_raw = {
            str(n.get("title") or "").strip() for n in nodes if isinstance(n, dict)
        } - {""}
        for node in nodes:
            if not isinstance(node, dict):
                continue
            title = node.get("title", "")
            core = _title_core(title) if title else ""
            if core:
                if core in seen:
                    new_title = _disambiguate_title(title, seen, parent_title=parent_title)
                    new_core = _title_core(new_title)
                    # 2026-10-15 加固（D6 · 消歧产物无脏复检 + 重名 fallthrough）：
                    # 旧实现两处兜底各自只走一步，留下两条「X之101」式无义形态的
                    # 产出路径：
                    #   ① 防御式循环从 `_n = 101` 起步直接拼「X之N」—— 跳过了
                    #      本可用的语义化候选，且旧 `_cn_ordinal` 在 n≥100 返回
                    #      阿拉伯数字，实测产出「总体实施策划之101」；
                    #   ② B4 脏复检只试**一次**语义化兜底且用固定 `idx=len(seen)`，
                    #      一旦该候选恰好与兄弟撞名（`_rebuilt_core in seen`）条件
                    #      就不满足，脏标题被原样保留落库。
                    # 现合并为一个有界重试：每次换 seed（步长 7）重跑语义化兜底，
                    # 同时要求「未与兄弟撞名 且 非泛化脏标题」两条件都满足才采纳；
                    # 语义池也耗尽时才退回中文序数收口。
                    _guard = 0
                    while (
                        new_core in seen or _is_dirty_l3_title_deep(new_title)
                    ) and _guard < _DEDUPE_SEED_TRIES:
                        _rb_used = set(sibling_raw) | set(seen)
                        _rebuilt = _semantic_fallback_title(
                            parent_title,
                            len(seen) + _guard * 7,
                            _rb_used,
                            context_title="",
                        )
                        _rebuilt_core = _title_core(_rebuilt)
                        if (
                            _rebuilt_core
                            and _rebuilt_core not in seen
                            and not _is_dirty_l3_title_deep(_rebuilt)
                        ):
                            logger.info(
                                "同级去重: 消歧产物「%s」仍为泛化/重名标题，改用语义化兜底「%s」（父级=%s）",
                                new_title,
                                _rebuilt,
                                parent_title or "(根)",
                            )
                            new_title = _rebuilt
                            new_core = _rebuilt_core
                            break
                        _guard += 1
                    if new_core in seen or _is_dirty_l3_title_deep(new_title):
                        # 语义化兜底也耗尽：退回中文序数收口。「之N」刻意豁免脏
                        # 判定（见 `_is_generic_stem_only_title` docstring），故每个
                        # 序数的 core 都不同、必然收敛；`_cn_ordinal` 已补齐百/千位
                        # 输出纯中文，不再产出「X之101」，也不回退到「（其N）」式
                        # 冗余后缀（那会被 `_clean_legacy_suffix_tree` 剥掉造成抖动）。
                        _n = 2
                        while _n < 10000 and _title_core(f"{title}之{_cn_ordinal(_n)}") in seen:
                            _n += 1
                        new_title = f"{title}之{_cn_ordinal(_n)}"
                        new_core = _title_core(new_title)
                        logger.warning(
                            "同级去重: 「%s」语义化兜底耗尽，退回中文序数「%s」（父级=%s）",
                            title,
                            new_title,
                            parent_title or "(根)",
                        )
                    logger.info(
                        "同级去重: 「%s」->「%s」（父级=%s）",
                        title,
                        new_title,
                        parent_title or "(根)",
                    )
                    node["title"] = new_title
                    core = new_core
                seen.add(core)
            children = node.get("children")
            if isinstance(children, list):
                _dedupe_siblings(children, node.get("title") or "")

    if isinstance(level1, list):
        _dedupe_siblings(level1)
    return level1


# ========== 跨章成套复用护栏 ==========


def _cap_cross_chapter_repetition(
    level1: list[dict], max_reuse: int = 3
) -> tuple[list[dict], int]:
    """限制同一个三级标题在**不同父级**下的复用广度，超出部分按所在章重新生成。

    2026-10-15 新增，修复用户反馈的「三级标题大量是占位式泛化名称」。

    与 2026-10-11「同级唯一」政策的关系（不冲突、不覆盖）：
      该政策取消的是**全树去重**（旧实现会整段删除跨父同名节点 → 章节内容消失，
      并追加「（其N）」后缀 → 目录污染），因为「3.1 施工组织设计」「4.1 施工组织设计」
      是两节独立内容，跨父同名合法。但政策落地后**没有任何机制约束复用广度**，
      于是 AI 按同一套骨架提示词逐章填充时，同一批泛化标题被成套复制到几十章 ——
      生产库实测单个标题（「智能建造地下室施工工艺」）出现在 25 个互不相关的
      一级章下，全库三级标题跨章重复率 97%。合法复用的形态是 2~3 个相关章，
      成套复制的形态是「十几个章共用同一组标题」，分界正是复用广度。

    规则：
      - 只作用于**三级及以下**；一级/二级是用户的正式章节名，一律不动；
      - 按 `_title_core` 归一化口径统计出现的**不同父级个数**，保留前 `max_reuse`
        个出现（先到先得，保证幂等：第二次运行统计已 ≤ 上限，不做任何改名）；
      - 超出部分用所在章主题重新生成（L1 标题切实体 → `_semantic_fallback_title`），
        因此新标题既与本章对应，又不会与已有标题撞名；
      - 永不产出「（N）」/「（其N）」式裸编号后缀（生成侧由 `_is_dirty_l3_title`
        把关，登记侧由 `used` 集合把关）。

    Args:
        level1: 一级目录树（就地修改）
        max_reuse: 同一标题允许出现的不同父级个数上限；<=0 表示关闭护栏（向后兼容）

    Returns:
        (目录树, 被改写的节点数)
    """
    if not isinstance(level1, list) or max_reuse <= 0:
        return level1, 0

    # 收集三级及以下节点：core -> [(node, parent_key, parent_title, l1_title), ...]
    occurrences: dict[str, list[tuple[dict, int, str, str]]] = {}
    all_titles: set[str] = set()
    # 父级身份必须用**节点实例**而非标题字符串：真实目录里 L2 是通用维度名
    # （「总体方案与实施策划」在每一章都出现），用标题判「不同父级」会把
    # 25 个章的同名父级塌缩成 1 个 → 护栏永不触发（2026-10-15 实测回归抓到）。
    _parent_seq = 0

    def _walk(nodes: list, level: int, parent_key: int, parent_title: str, l1_title: str) -> None:
        nonlocal _parent_seq
        for node in nodes:
            if not isinstance(node, dict):
                continue
            title = str(node.get("title") or "").strip()
            cur_l1 = title if level == 1 else l1_title
            if title:
                all_titles.add(title)
                if level >= 3:
                    core = _title_core(title)
                    if core:
                        occurrences.setdefault(core, []).append(
                            (node, parent_key, parent_title, cur_l1)
                        )
            children = node.get("children")
            if isinstance(children, list):
                _parent_seq += 1
                _walk(children, level + 1, _parent_seq, title or parent_title, cur_l1)

    _walk(level1, 1, 0, "", "")

    used = set(all_titles)
    fixed = 0
    for core, items in occurrences.items():
        if len(items) <= max_reuse:
            continue
        # 统计口径是「不同父级实例」：同一父级下的重复由 `_uniquify_outline_titles`
        # 负责，这里不越界处理，避免两处判据打架。
        parent_keys = list(dict.fromkeys(k for _, k, _, _ in items))
        if len(parent_keys) <= max_reuse:
            continue
        kept_keys = set(parent_keys[:max_reuse])
        seq = 0
        rewritten = 0
        sample_title = str(items[0][0].get("title") or "").strip()
        for node, parent_key, parent_title, l1_title in items:
            if parent_key in kept_keys:
                continue
            old_title = str(node.get("title") or "").strip()
            new_title = _semantic_fallback_title(
                parent_title, seq, used, context_title=l1_title
            )
            # 语义化兜底自会登记 used；此处仅做「确实变更且不再泛化」的收口校验
            if not new_title or new_title == old_title or _is_dirty_l3_title_deep(new_title):
                logger.warning(
                    "跨章复用护栏: 「%s」重生成失败（父级=%s），保留原标题",
                    old_title,
                    parent_title or "(根)",
                )
                continue
            node["title"] = new_title
            rewritten += 1
            seq += 1
        if rewritten:
            logger.info(
                "跨章复用护栏: 「%s」出现在 %d 个不同父级（上限 %d），改写超出的 %d 个",
                sample_title,
                len(parent_keys),
                max_reuse,
                rewritten,
            )
            fixed += rewritten

    if fixed:
        logger.info("跨章复用护栏: 共改写 %d 个跨章成套复用的三级标题", fixed)
    return level1, fixed


def _cap_intra_chapter_repetition(
    level1: list[dict], max_parents_per_chapter: int = 2
) -> tuple[list[dict], int]:
    """限制同一个三级标题在**同一个一级章内**不同二级父级下的复用个数。

    2026-10-19 新增，修复《岳阳路园区科研南楼室内装修工程》事故中
    「一、总体方案与实施策划（1-7）/ 二、分阶段推进计划（1-7 完全相同）/
    三、资源配置与协调保障（前 7 个照抄）」的成套照抄形态。

    与既有两道护栏的关系（互补、不重叠）：
      - `_uniquify_outline_titles`：同一 L2 父级下的兄弟去重（同级唯一政策）；
      - `_cap_cross_chapter_repetition`：全树按不同父级**实例**计数，上限 3；
        「同一章内 3 个 L2 各挂同一批 L3」恰好 = 3 个父级实例，不会触发；
      - 本函数：按「每个一级章内」计数，上限更严（默认 2）——同章内跨 L2
        复用读者同屏对照即穿帮，是最刺眼的死循环形态。

    规则（与 `_cap_cross_chapter_repetition` 同口径）：
      - 只作用于三级及以下节点；
      - 按 `_title_core` 归一化统计「本章内不同 L2 父级实例」个数；
      - 保留前 `max_parents_per_chapter` 个出现（先到先得，幂等）；
      - 超出部分用所在 L2 主题经 `_semantic_fallback_title` 重新生成，
        永不产出「（N）」式裸编号后缀。

    Args:
        level1: 一级目录树（就地修改）
        max_parents_per_chapter: 同一 L3 标题在单个一级章内允许出现的
            不同 L2 父级个数上限；<=0 表示关闭护栏（向后兼容）

    Returns:
        (目录树, 被改写的节点数)
    """
    if not isinstance(level1, list) or max_parents_per_chapter <= 0:
        return level1, 0

    fixed_total = 0
    for chapter in level1:
        if not isinstance(chapter, dict):
            continue
        l1_title = str(chapter.get("title") or "").strip()
        l2_children = chapter.get("children")
        if not isinstance(l2_children, list):
            continue

        # 收集本章 L3：core -> [(node, l2_parent_key, l2_title), ...]
        occurrences: dict[str, list[tuple[dict, int, str]]] = {}
        _parent_seq = 0

        def _walk_l3(nodes: list, parent_key: int, parent_title: str) -> None:
            nonlocal _parent_seq
            for node in nodes:
                if not isinstance(node, dict):
                    continue
                title = str(node.get("title") or "").strip()
                sub = node.get("children")
                # 只统计挂在 L2 下的 L3 节点（parent_key>0 即处于某个 L2 之下）
                if title and parent_key > 0:
                    core = _title_core(title)
                    if core:
                        occurrences.setdefault(core, []).append(
                            (node, parent_key, parent_title)
                        )
                if isinstance(sub, list):
                    _parent_seq += 1
                    _walk_l3(sub, _parent_seq, title or parent_title)

        _walk_l3(l2_children, 0, l1_title)

        if not occurrences:
            continue

        all_titles: set[str] = set()

        def _collect_titles(nodes: list) -> None:
            for n in nodes:
                if isinstance(n, dict):
                    t = str(n.get("title") or "").strip()
                    if t:
                        all_titles.add(t)
                    _collect_titles(n.get("children") or [])

        _collect_titles(chapter.get("children") or [])

        for core, items in occurrences.items():
            if len(items) <= max_parents_per_chapter:
                continue
            parent_keys = list(dict.fromkeys(k for _, k, _ in items))
            if len(parent_keys) <= max_parents_per_chapter:
                continue
            kept_keys = set(parent_keys[:max_parents_per_chapter])
            seq = 0
            rewritten = 0
            sample_title = str(items[0][0].get("title") or "").strip()
            for node, parent_key, parent_title in items:
                if parent_key in kept_keys:
                    continue
                old_title = str(node.get("title") or "").strip()
                new_title = _semantic_fallback_title(
                    parent_title, seq, all_titles, context_title=l1_title
                )
                if not new_title or new_title == old_title or _is_dirty_l3_title_deep(new_title):
                    logger.warning(
                        "同章复用护栏: 「%s」重生成失败（L2=%s，章=%s），保留原标题",
                        old_title,
                        parent_title or "(根)",
                        l1_title,
                    )
                    continue
                node["title"] = new_title
                all_titles.add(new_title)
                rewritten += 1
                seq += 1
            if rewritten:
                logger.info(
                    "同章复用护栏: 「%s」在章「%s」内出现于 %d 个不同 L2（上限 %d），改写超出的 %d 个",
                    sample_title,
                    l1_title,
                    len(parent_keys),
                    max_parents_per_chapter,
                    rewritten,
                )
                fixed_total += rewritten

    if fixed_total:
        logger.info("同章复用护栏: 共改写 %d 个同章成套照抄的三级标题", fixed_total)
    return level1, fixed_total


def _cap_cross_chapter_l2_repetition(
    level1: list[dict], max_reuse: int = 2
) -> tuple[list[dict], int]:
    """限制同一个二级标题在**不同一级章**下的复用广度，超出部分按所在章重新生成。

    2026-10-27 第三轮新增，修复《岳阳路园区科研南楼室内装修工程》docx 中
    16 个 L2 标题跨 L1 重复的问题（如「组织架构与职责分工」出现在 3 个 L1 中，
    「主要分部工程施工方案」出现在 5 个 L1 中）。

    根因：`_L2_SUFFIX_POOL` 按语义域共享，多个 L1 属于同一域时复用相同 L2 池，
    产出完全相同的 L2 标题。

    与既有护栏的关系（互补、不重叠）：
      - `_cap_cross_chapter_repetition`：只作用于三级及以下节点；
      - `_cap_intra_chapter_repetition`：只作用于同一 L1 内的 L3 节点；
      - 本函数：只作用于二级节点，跨 L1 计数。

    规则：
      - 只作用于二级节点；
      - 按 `_title_core` 归一化统计「不同 L1 父级实例」个数；
      - 保留前 `max_reuse` 个出现（先到先得，幂等）；
      - 超出部分从 _L2_SUFFIX_POOL 中选取未使用的池条目，或基于 L1 标题生成
        唯一 L2 标题；永不产出「（N）」式裸编号后缀。

    Args:
        level1: 一级目录树（就地修改）
        max_reuse: 同一 L2 标题允许出现的不同 L1 个数上限；<=0 表示关闭护栏

    Returns:
        (目录树, 被改写的节点数)
    """
    if not isinstance(level1, list) or max_reuse <= 0:
        return level1, 0

    # 收集 L2 节点：core -> [(node, l1_title, l2_index), ...]
    # 同时收集所有 L2 标题的归一化核心词（不含 L3），用于生成唯一标题时排除
    occurrences: dict[str, list[tuple[dict, str, int]]] = {}
    all_l2_cores: set[str] = set()

    for l1 in level1:
        if isinstance(l1, dict):
            l2_children = l1.get("children")
            if isinstance(l2_children, list):
                for l2 in l2_children:
                    if isinstance(l2, dict):
                        t = str(l2.get("title") or "").strip()
                        if t:
                            core = _title_core(t)
                            if core:
                                all_l2_cores.add(core)

    _l1_seq = 0
    for l1 in level1:
        if not isinstance(l1, dict):
            continue
        l1_title = str(l1.get("title") or "").strip()
        l2_children = l1.get("children")
        if not isinstance(l2_children, list):
            continue
        for j, l2 in enumerate(l2_children):
            if not isinstance(l2, dict):
                continue
            title = str(l2.get("title") or "").strip()
            if title:
                core = _title_core(title)
                if core:
                    occurrences.setdefault(core, []).append(
                        (l2, l1_title, j)
                    )

    # used 集合含归一化核心词：_derive_child_title 返回的池条目（无编号）
    # 会被判为已占用，从而跳过旧标题、选取不同池条目
    used = set(all_l2_cores)
    fixed = 0
    for core, items in occurrences.items():
        if len(items) <= max_reuse:
            continue
        # 统计不同 L1 父级个数
        l1_titles = list(dict.fromkeys(item[1] for item in items))
        if len(l1_titles) <= max_reuse:
            continue
        kept_l1s = set(l1_titles[:max_reuse])
        seq = 0
        rewritten = 0
        sample_title = str(items[0][0].get("title") or "").strip()
        for l2, l1_title, j in items:
            if l1_title in kept_l1s:
                continue
            old_title = str(l2.get("title") or "").strip()
            # 用 L1 标题作为 context，生成与本章对应的唯一 L2 标题
            new_title = _derive_child_title(
                l1_title, j + seq * 7, used, level=2, grandparent_title=""
            )
            if (
                not new_title
                or new_title == old_title
            ):
                logger.warning(
                    "跨章 L2 复用护栏: 「%s」重生成失败（L1=%s），保留原标题",
                    old_title,
                    l1_title,
                )
                continue
            l2["title"] = new_title
            used.add(_title_core(new_title))
            rewritten += 1
            seq += 1
        if rewritten:
            logger.info(
                "跨章 L2 复用护栏: 「%s」出现在 %d 个 L1（上限 %d），改写超出的 %d 个",
                sample_title,
                len(l1_titles),
                max_reuse,
                rewritten,
            )
            fixed += rewritten

    if fixed:
        logger.info("跨章 L2 复用护栏: 共改写 %d 个跨章重复的二级标题", fixed)
    return level1, fixed


# ========== 专业隔离（跨领域术语串台修复，2026-10-19） ==========

#: 项目类型判定关键词（按扫描优先级排序；命中即停）
_PROJECT_TYPE_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("装饰装修", "装饰装修"),
    ("室内装修", "装饰装修"),
    ("装修工程", "装饰装修"),
    ("装饰工程", "装饰装修"),
    ("精装", "装饰装修"),
    ("幕墙", "装饰装修"),
    ("改造工程", "装饰装修"),
    ("市政", "市政"),
    ("公路", "市政"),
    ("道路", "市政"),
    ("管道", "市政"),
    ("园林", "园林景观"),
    ("绿化", "园林景观"),
    ("水利", "水利"),
    ("电力", "电力"),
    ("机电安装", "机电安装"),
    ("建筑", "房建"),
    ("土建", "房建"),
)

#: 各项目类型严禁出现的「域外术语 → 语义转化词」映射。
#: 装饰装修项目出现地勘/房建结构术语即串台（生产事故：装修项目目录出现
#: 「勘察技术路线与地质难点应对方案」）。
_CROSS_DOMAIN_TERM_MAP: dict[str, dict[str, str]] = {
    "装饰装修": {
        "地质勘察": "现场调查",
        "工程地质": "既有建筑",
        "地质难点": "既有设施难点",
        "地质条件": "既有建筑条件",
        "地质": "既有建筑",
        "深基坑": "既有地下设施",
        "基坑支护": "既有地下设施防护",
        "基坑": "既有地下设施",
        "桩基": "基础加固",
        "灌注桩": "基础加固",
        "塔吊基础": "垂直运输设施",
        "塔吊": "垂直运输",
        "土方开挖": "基层处理",
        "降水": "渗漏治理",
    },
}

#: 提示词用的各域禁用词表（与 _CROSS_DOMAIN_TERM_MAP 键对应，取键原文）
_PROMPT_FORBIDDEN_TERMS: dict[str, str] = {
    "装饰装修": "地质勘察、深基坑、基坑支护、桩基、塔吊、土方开挖、降水",
}


def _detect_project_type(summary: str, global_facts: list | None = None) -> str:
    """从全局事实与项目摘要中检测项目类型（专业域）。

    优先级：事实变量 projectType/工程类型 > 摘要关键词扫描 > 空串。
    返回归一化类型名（如「装饰装修」「市政」「房建」），无法判定返回 ""。
    """
    if isinstance(global_facts, list):
        for item in global_facts:
            if not isinstance(item, dict):
                continue
            key = str(item.get("key") or item.get("label") or "")
            val = str(item.get("value") or "")
            if not val:
                continue
            if key in ("projectType", "工程类型", "project_type"):
                return val.strip()
    text = summary or ""
    for kw, ptype in _PROJECT_TYPE_KEYWORDS:
        if kw in text:
            return ptype
    return ""


def _sanitize_cross_domain_titles(level1: list[dict], project_type: str) -> tuple[list[dict], int]:
    """将目录树中与项目类型冲突的域外术语改写为本域语义（就地修改）。

    2026-10-19 新增：装修项目目录出现「勘察技术路线与地质难点应对方案」
    这类地勘术语时，正文会被标题误导强行往地质上扯（文不对题）。
    在目录收口阶段按 `_CROSS_DOMAIN_TERM_MAP` 做确定性术语转化，例如
    「地质」→「既有建筑」——「勘察技术路线与地质难点应对方案」改写后
    语义自动贴合「原建筑踏勘与既有设施保护」。

    仅当项目类型命中映射表（当前支持装饰装修）时生效；无法判定类型时
    不做任何改写（避免误伤其他行业的合法术语）。

    Returns:
        (目录树, 改写命中的节点数)
    """
    term_map = _CROSS_DOMAIN_TERM_MAP.get((project_type or "").strip())
    if not term_map or not isinstance(level1, list):
        return level1, 0

    fixed = 0

    def _walk(nodes: list) -> None:
        nonlocal fixed
        for node in nodes:
            if not isinstance(node, dict):
                continue
            title = str(node.get("title") or "").strip()
            if title:
                new_title = title
                for bad, good in term_map.items():
                    if bad in new_title:
                        new_title = new_title.replace(bad, good)
                if new_title != title:
                    node["title"] = new_title
                    fixed += 1
            sub = node.get("children")
            if isinstance(sub, list):
                _walk(sub)

    _walk(level1)
    if fixed:
        logger.info(
            "专业隔离护栏: 项目类型「%s」，共改写 %d 个含域外术语的目录标题",
            project_type,
            fixed,
        )
    return level1, fixed


def _build_professional_isolation_block(project_type: str) -> str:
    """构建注入生成提示词的「专业隔离约束」文本块。

    对齐质量报告建议 2：正文/目录生成的 System Prompt 必须锁定项目类型并
    严禁域外术语。project_type 未命中禁用词表时返回空串（不注入）。
    """
    ptype = (project_type or "").strip()
    if not ptype:
        return ""
    forbidden = _PROMPT_FORBIDDEN_TERMS.get(ptype, "")
    if not forbidden:
        return ""
    return (
        f"\n【专业隔离约束】本工程为【{ptype}】项目，严禁出现「{forbidden}」"
        f"等不属于本专业的其他领域术语。若目录标题或评分要点中出现此类词汇，"
        f"必须按{ptype}的业务逻辑进行语义转化后再使用"
        f"（如「勘察」→「现场调查与既有设施排查」、「地质难点」→「既有设施难点」），"
        f"不得死板硬套其他行业的内容框架。\n"
    )


# ========== 最小子节点保障 ==========


def _ensure_min_children(
    outline: list[dict],
    min_level2: int = 3,
    min_level3: int = 5,
) -> list[dict]:
    """强制确保每个一级目录下有至少 min_level2 个二级目录，
    每个二级目录下有至少 min_level3 个三级目录。

    当 AI 填充失败或 normalize 过滤导致子目录不足时，用「从上一级目录
    语义派生」的方式补全标题，不使用固定模板池，保证：
    - 补全的 L2 与所属 L1 章节标题语义关联
    - 补全的 L3 与所属 L2 标题语义关联
    - 全局唯一：补全标题不得与任何层级已有标题重复（含跨章节）
    """
    # 全局已用标题集合：跨所有章节，防止补全标题与任何位置已存在标题重名
    global_used: set[str] = set()
    for _ch in outline:
        if not isinstance(_ch, dict):
            continue

        def _collect(nodes: list) -> None:
            for n in nodes:
                if isinstance(n, dict):
                    if n.get("title"):
                        global_used.add(n["title"].strip())
                    _collect(n.get("children", []))

        _collect(_ch.get("children", []))

    for chapter in outline:
        if not isinstance(chapter, dict):
            continue
        chapter_title = chapter.get("title", "")
        children = chapter.get("children", [])
        if not isinstance(children, list):
            chapter["children"] = []
            children = chapter["children"]

        existing_titles = {
            c.get("title", "") for c in children if isinstance(c, dict) and c.get("title")
        }

        # 第一步：仅填充已有占位节点（不追加），用 _l2_filled 跟踪已填充数量
        _l2_filled = 0
        for child in children:
            if isinstance(child, dict) and not str(child.get("title") or "").strip():
                t = _derive_child_title(
                    chapter_title or "工程实施", _l2_filled, existing_titles | global_used, level=2
                )
                child["title"] = t
                existing_titles.add(t)
                global_used.add(t)
            _l2_filled += 1

        level2_count = len(children)
        if level2_count < min_level2:
            _before = level2_count
            while len(children) < min_level2:
                # L2 从 L1 章节标题派生，保持语义关联且不重名（used 含全局）
                l2_title = _derive_child_title(
                    chapter_title, _l2_filled, existing_titles | global_used, level=2
                )
                _l2_filled += 1
                existing_titles.add(l2_title)
                global_used.add(l2_title)
                grandchildren = []
                for j in range(min_level3):
                    # L3 从 L2 标题派生（used 含全局，避免与兄弟/其他章节重名）
                    # 2026-10-15 修复（B5）：旧实现传 `set()` —— 注释写「used 含全局」
                    # 但代码是空集，本章新建 L2 下的整批 L3 完全不看全局标题集合，
                    # 于是同一批派生标题在不同章成套复制（生产库跨章重复的形态）。
                    # 同时补 `grandparent_title=chapter_title`：L2 恒为通用维度名
                    # （「总体方案与实施策划」等），不传 L1 就无法取得本章实体词。
                    l3_title = _derive_child_title(
                        l2_title,
                        j,
                        global_used,
                        level=3,
                        grandparent_title=chapter_title,
                    )
                    global_used.add(l3_title)
                    grandchildren.append(
                        {
                            "title": l3_title,
                            "level": 3,
                            "children": [],
                        }
                    )
                children.append(
                    {
                        "title": l2_title,
                        "level": 2,
                        "children": grandchildren,
                    }
                )
            logger.info("补全二级目录: 章=%s, 从 %d 补到 %d", chapter_title, _before, len(children))

        for child in children:
            if not isinstance(child, dict):
                continue
            grandchildren = child.get("children", [])
            if not isinstance(grandchildren, list):
                child["children"] = []
                grandchildren = child["children"]

            # 第二步：仅填充已有占位 L3 节点（不追加新 L3）
            _l3_filled = 0
            for gc in grandchildren:
                if isinstance(gc, dict) and not str(gc.get("title") or "").strip():
                    t = _derive_child_title(
                        str(child.get("title") or ""),
                        _l3_filled,
                        global_used,
                        level=3,
                        grandparent_title=chapter_title,
                    )
                    gc["title"] = t
                    global_used.add(t)
                _l3_filled += 1

            gc_count = len(grandchildren)
            if gc_count < min_level3:
                used_l3 = {
                    g.get("title", "")
                    for g in grandchildren
                    if isinstance(g, dict) and g.get("title")
                }
                _before_gc = gc_count
                while len(grandchildren) < min_level3:
                    l3_title = _derive_child_title(
                        child.get("title", ""),
                        _l3_filled,
                        used_l3 | global_used,
                        level=3,
                        grandparent_title=chapter_title,
                    )
                    _l3_filled += 1
                    used_l3.add(l3_title)
                    global_used.add(l3_title)
                    grandchildren.append(
                        {
                            "title": l3_title,
                            "level": 3,
                            "children": [],
                        }
                    )
                logger.info(
                    "补全三级目录: 二级=%s, 从 %d 补到 %d",
                    str(child.get("title", ""))[:20],
                    _before_gc,
                    len(grandchildren),
                )

    return _uniquify_outline_titles(outline)


# ============================================================
# Legacy V3 framework utilities (migrated from retired v2v3 module)
# ============================================================

def _classify_chapter_type(chapter_title: str) -> str:
    """根据章节标题分类章节类型，用于生成多样化的兜底标题。

    优先级：更具体的关键词（施工方法/专项施工/附图/方案）放在前面，
    通用词（方案/措施）放在后面，确保"专项施工方案"等归为 construction 而非 measure。
    """
    if not chapter_title:
        logger.debug("[分类] 空标题 -> other")
        return "other"

    # 优先级检查顺序 = 具体度降序：最具体的类型先匹配
    checks: list[tuple[str, str]] = [
        (("质量", "创优", "质量管控"), "quality", "质量相关"),
        (("安全", "文明", "环保", "消防"), "safety", "安全相关"),
        (("进度", "工期", "施工计划"), "schedule", "进度相关"),
        (("资源", "设备", "材料", "劳动力", "配备"), "resource", "资源相关"),
        (("平面", "布置", "总图"), "layout", "平面布置"),
        (("应急", "风险", "预案", "紧急情况"), "emergency", "应急管理"),
        (("施工方法", "分部工程", "专项施工"), "construction", "施工方法"),
        (("概述", "概况", "编制", "项目概况", "工程概况"), "overview", "概况说明"),
        (("配合", "协调", "沟通"), "coordination", "协调管理"),
        (("重难点", "分析"), "analysis", "重难点分析"),
        (("施工",), "construction", "工程施工（含方案）"),
        (("方案",), "measure", "通用方案"),
        (("措施",), "measure", "通用措施"),
        (("附件", "附图"), "attachment", "附件附图"),
    ]

    for keywords, result_type, label in checks:
        matched = [kw for kw in keywords if kw in chapter_title]
        if matched:
            logger.debug(
                "[分类] '%s' -> %s (匹配关键词: %s)",
                chapter_title,
                result_type,
                ",".join(matched),
            )
            return result_type

    logger.debug("[分类] '%s' -> other (无匹配关键词)", chapter_title)
    return "other"


_L2_TITLE_PATTERNS: dict[str, list[str]] = {
    "quality": [
        "质量目标与创优规划",
        "质量保证体系与组织机构",
        "施工质量过程控制",
        "质量通病防治措施",
        "隐蔽工程质量验收",
        "成品保护与交付维护",
        "质量检验与试验管理",
        "质量回访与保修承诺",
        "质量文档与档案管理",
        "质量奖惩与考核制度",
        "材料设备质量管控",
        "分项工程验收标准",
        "质量例会与沟通机制",
        "质量监督与整改闭环",
        "质量风险预警管理",
        "质量持续改进机制",
    ],
    "safety": [
        "安全目标与管理体系",
        "安全生产责任制与组织架构",
        "施工现场安全管控措施",
        "特种作业与危险源管理",
        "临时用电安全管理",
        "机械设备安全防护",
        "高处作业与临边防护",
        "消防安全与应急管理",
        "安全培训与交底制度",
        "安全检查与隐患排查",
        "文明施工与环境保护",
        "职业病防治与健康管理",
        "安全教育与宣传机制",
        "安全事故报告与处理",
        "安全防护用品管理",
        "安全投入与经费保障",
    ],
    "schedule": [
        "施工总进度计划编制",
        "关键线路分析与里程碑节点",
        "进度保障措施与资源配置",
        "进度动态监控与调整机制",
        "工期延误应急预案",
        "进度协调与会议制度",
        "交叉作业进度统筹",
        "进度报表与报告体系",
        "进度预警与纠偏措施",
        "节点工期考核方案",
        "赶工措施与资源增配",
        "进度优化与网络计划",
        "总进度计划与分项计划",
        "进度资金保障机制",
        "进度风险识别与防控",
        "进度总结与经验沉淀",
    ],
    "resource": [
        "劳动力配置与进场计划",
        "施工机械设备投入计划",
        "主要材料采购与供应方案",
        "试验检测设备配置",
        "资金使用与现金流计划",
        "资源动态调配机制",
        "材料进场检验方案",
        "设备维护保养计划",
        "劳动力培训与安全教育",
        "资源需求预测与调整",
        "外包资源管理方案",
        "仓储与物流管理",
        "资源平衡与优化配置",
        "主要材料品牌与规格",
        "机械设备进退场计划",
        "资源成本管控措施",
    ],
    "construction": [
        "施工测量方案与精度控制",
        "土方开挖与基坑支护施工",
        "主体结构施工方法",
        "装饰装修工程施工工艺",
        "机电安装施工方案",
        "脚手架与临时设施方案",
        "屋面防水施工方法",
        "砌体工程施工方案",
        "钢结构安装工艺",
        "地面工程施工方案",
        "门窗安装施工工艺",
        "防水与保温施工方法",
        "施工降排水方案",
        "模板工程专项方案",
        "钢筋工程专项方案",
        "混凝土工程专项方案",
    ],
    "overview": [
        "工程概况与建设规模",
        "项目建设地点与周边环境",
        "工程承包范围与合同工期",
        "项目质量目标与安全目标",
        "施工条件与现场环境",
        "项目组织架构与人员配置",
        "项目特点与重难点分析",
        "主要技术参数与指标",
        "主要工程量汇总",
        "项目主要材料设备概况",
        "项目设计依据与技术标准",
        "项目主要分部分项工程",
        "项目工期目标与节点安排",
        "项目主要风险点识别",
        "项目组织协调原则",
        "项目实施总体思路",
    ],
    "layout": [
        "施工总平面布置原则与依据",
        "施工总平面布置方案",
        "临时道路与交通组织",
        "材料堆场与加工区布置",
        "临时办公与生活设施",
        "临时水电设施布置",
        "消防布置与安全通道",
        "施工分区与围挡方案",
        "排水系统与环保措施",
        "垂直运输设备布置",
        "现场标牌与CI形象布置",
        "季节性施工平面调整",
        "临建拆除与场地恢复",
        "总平面动态管理方案",
        "施工总平面布置图",
        "平面布置说明与依据",
    ],
    "emergency": [
        "应急组织体系与职责分工",
        "风险识别与预警机制",
        "应急响应分级与处置流程",
        "应急资源与物资保障",
        "突发事件专项处置方案",
        "应急演练计划与评估",
        "灾后恢复与总结改进",
        "应急信息报告与舆论应对",
        "应急培训与教育计划",
        "应急联动与外部协调",
        "自然灾害应急处置",
        "安全事故应急处理",
        "公共卫生事件应急响应",
        "应急资金与后勤保障",
        "应急预案管理体系",
        "应急总结与持续改进",
    ],
    "coordination": [
        "组织协调组织架构与职责",
        "沟通协调机制与流程设计",
        "与建设单位配合方案",
        "与监理单位协调措施",
        "与设计单位技术对接方案",
        "专业分包协调管理",
        "外部关系协调与公共关系",
        "联席会议制度与决策流程",
        "信息传递与报告机制",
        "冲突处理与争议解决程序",
        "与政府主管部门协调",
        "社区关系与居民沟通",
        "协调会议管理制度",
        "协调档案与记录管理",
        "协调效果评估与改进",
        "跨专业协同工作机制",
    ],
    "analysis": [
        "项目主要特点与优势分析",
        "施工重难点识别与对策",
        "技术难点分析与解决方案",
        "管理难点分析与应对策略",
        "进度难点分析与保障措施",
        "安全难点分析与防控方案",
        "质量难点分析与控制措施",
        "外部环境风险分析",
        "资源供应风险分析",
        "组织协调难点分析",
        "季节性施工难点分析",
        "交叉作业难点分析",
        "项目风险总体评估",
        "难点应对效果预测",
        "重难点动态跟踪机制",
        "分析结论与建议汇总",
    ],
    "measure": [
        "施工总体部署与规划",
        "主要施工方案与技术措施",
        "专项施工方案",
        "施工关键工序控制",
        "施工资源配置方案",
        "施工节点进度计划",
        "季节性施工措施",
        "成品保护措施",
        "施工技术方案优化",
        "施工质量控制要点",
        "施工安全管理措施",
        "施工进度保障措施",
        "施工成本控制措施",
        "施工环保保障措施",
        "施工技术管理措施",
        "施工协调管理措施",
    ],
    "attachment": [
        "施工总平面布置图",
        "施工进度计划网络图",
        "项目管理组织架构图",
        "专项施工方法流程图",
        "主要机械设备配置表",
        "主要材料进场计划表",
        "劳动力动态曲线图",
        "质量检验试验计划表",
        "安全文明施工检查表",
        "进度计划甘特图",
        "施工总进度计划横道图",
        "项目管理流程图",
        "主要分部分项工程进度计划",
        "材料设备采购计划表",
        "安全应急预案流程图",
        "项目组织机构职责表",
    ],
    "other": [
        "基本组织方案",
        "主要实施措施",
        "质量控制要点",
        "安全管理办法",
        "进度管控方案",
        "资源配置计划",
        "技术保障措施",
        "协调管理方案",
        "现场管理制度",
        "档案资料管理",
        "成本管控措施",
        "环境保护措施",
        "综合管理方案",
        "技术质量控制",
        "进度安全保障",
        "资源协调管理",
    ],
}

_L3_TITLE_PATTERNS: dict[str, list[str]] = {
    "quality": [
        "质量管理组织架构搭建",
        "质量方针目标与指标分解",
        "质量管理责任制与签字制度",
        "质量管理制度文件汇编",
        "材料进场质量检验流程",
        "工序交接质量检查制度",
        "隐蔽工程验收程序",
        "分项分部工程质量评定",
        "质量通病防治技术方案",
        "质量缺陷处理与返工程序",
        "质量检验试验计划",
        "质量数据分析与改进",
        "质量奖惩考核实施细则",
        "质量例会与沟通机制",
        "质量回访保修制度",
        "质量持续改进流程",
        "质量策划与事前控制措施",
        "过程质量控制要点",
        "质量资料归档与验收准备",
        "质量通病专项治理方案",
        "质量控制节点与停检点设置",
        "质量追溯与终身责任制",
        "质量缺陷预防与纠偏机制",
        "质量评审与持续改进循环",
        "首件鉴定与样板引路制度",
        "质量数据信息化管理",
        "质量目标分解与考核体系",
        "质量检验批划分方案",
        "质量通病防治交底卡",
        "质量事故分析与整改闭环",
        "质量改进PDCA循环机制",
        "材料复试与见证取样管理",
        "质量目标分解与考核指标",
        "质量检验批划分与验收标准",
        "质量事故责任追溯机制",
        "质量数据可视化分析报告",
        "质量审核内审流程与整改",
        "质量回访保修服务方案",
        "质量创优策划与申报流程",
        "质量追溯码与数字化台账",
    ],
    "safety": [
        "安全生产领导小组设立",
        "各级人员安全生产责任书",
        "安全管理组织网络搭建",
        "安全生产管理制度汇编",
        "危险源辨识与风险分级",
        "专项安全施工方案编制",
        "安全教育培训与交底",
        "安全检查与隐患排查制度",
        "安全事故报告与处理流程",
        "安全防护用品管理",
        "特种作业人员管理",
        "安全经费投入与使用",
        "安全例会与协调机制",
        "安全应急预案演练",
        "安全奖惩考核制度",
        "安全文明施工创建方案",
        "安全技术创新与工艺改进",
        "安全风险分级管控措施",
        "安全隐患排查治理台账",
        "安全应急演练评估与改进",
        "安全文明施工标准化建设",
        "特种作业持证上岗核查",
        "安全费用专款专用审计",
        "安全风险动态预警机制",
        "安全技术交底签字存档",
        "交叉作业安全协调机制",
        "安全警示标识与防护设施",
        "安全事故责任追究制度",
        "安全教育培训效果评估",
        "安全隐患闭环整改流程",
        "安全例会会议纪要归档",
        "安全应急预案动态更新",
        "安全风险分级管控清单",
        "安全文明施工检查结果通报",
        "安全费用投入统计分析",
        "安全应急预案响应时间考核",
        "安全培训效果跟踪机制",
        "安全隐患排查标准台账",
        "安全责任考核奖惩兑现",
        "安全标准化示范工地创建",
    ],
    "schedule": [
        "施工总进度计划编制方法",
        "关键线路识别与工期目标",
        "进度计划分级管理体系",
        "进度资源配置与平衡",
        "进度偏差分析与纠偏措施",
        "进度协调会议与报告制度",
        "交叉作业进度统筹方案",
        "进度预警与应急响应",
        "赶工措施与资源增配",
        "进度计划优化调整",
        "节点工期考核与奖惩",
        "进度数据收集与分析",
        "进度风险识别与防控",
        "进度总结与经验归档",
        "总进度计划与施工网络图",
        "进度计划动态调整机制",
        "进度计划滚动修订制度",
        "工期延误责任划分机制",
        "多专业交叉进度协调表",
        "进度偏差根本原因分析",
        "进度资源动态调配方案",
        "进度考核与激励机制",
        "进度风险预留与缓冲安排",
        "进度数据可视化看板建设",
        "进度计划里程碑管理",
        "进度协调联席会议制度",
        "进度偏差预警与纠偏流程",
        "进度总结与复盘机制",
        "进度计划版本控制管理",
        "进度数据分析报表体系",
        "进度风险应急预案编制",
        "进度考核指标分解落地",
        "进度计划里程碑分解图",
        "进度协调联席会议制度汇编",
        "进度偏差预警响应流程图",
        "进度数据可视化看板建设方案",
        "进度计划三级预警机制",
        "进度风险应急赶工预案",
        "进度计划数字化协同平台",
        "进度偏差根因追溯报告",
    ],
    "resource": [
        "劳动力需求预测与配置",
        "各阶段用工计划与进场安排",
        "施工机械设备选型与配置",
        "主要材料采购与供应计划",
        "试验检测设备配置方案",
        "资金使用计划与现金流管理",
        "材料进场检验与保管",
        "设备维护保养计划",
        "劳动力培训与安全教育",
        "资源动态调配与平衡",
        "外包资源管理方案",
        "材料仓储与物流管理",
        "资源成本分析与控制",
        "主要设备进退场计划",
        "资源需求预测调整机制",
        "资源总结与经验归档",
        "资源需求弹性预测模型",
        "供应商资质审查与评价",
        "材料损耗率控制与节约措施",
        "大型设备租赁比选方案",
        "资源调配应急预案",
        "资源成本动态监控机制",
        "劳动力技能矩阵与培训体系",
        "资源利用率评估指标",
        "资源需求预测偏差分析",
        "供应商备选库建设方案",
        "材料采购价格监测机制",
        "设备维护保养周期表",
        "劳动力技能等级考核标准",
        "资源成本偏差纠偏措施",
        "资源需求预测方法总结",
        "供应商评价打分表模板",
        "材料损耗率指标分解表",
        "设备租赁比选决策流程",
        "资源需求预测偏差分析报告",
        "供应商备选库动态更新机制",
        "劳动力技能等级考核评估报告",
        "资源成本偏差预警与应对方案",
        "资源需求预测偏差修正方案",
        "供应商动态绩效评价机制",
    ],
    "construction": [
        "施工测量定位与精度控制",
        "土方开挖与基坑支护工艺",
        "基础工程施工方法",
        "主体结构施工技术方案",
        "钢筋工程专项工艺",
        "模板工程专项方案",
        "混凝土工程专项工艺",
        "砌体工程施工方法",
        "钢结构安装工艺",
        "屋面防水施工方法",
        "外墙保温施工要点",
        "装饰装修工程施工工艺",
        "机电安装施工方案",
        "脚手架搭设与拆除方案",
        "施工降排水方案",
        "季节性施工措施",
        "预制构件吊装与连接工艺",
        "大体积混凝土温控方案",
        "深基坑监测与信息化施工",
        "高空作业安全技术措施",
        "施工临时设施搭设规范",
        "特种工序质量检验标准",
        "新材料新工艺应用方案",
        "复杂节点施工方案优化",
        "预应力张拉施工工艺",
        "焊接工艺评定与检测",
        "防水工程细部构造做法",
        "幕墙施工技术方案",
        "桩基施工专项方案",
        "地下室防水施工工艺",
        "高空吊装安全验算",
        "大体积混凝土测温方案",
        "测量定位复测与验线",
        "施工缝与后浇带处理工艺",
        "预制构件吊装专项方案",
        "深基坑监测自动化系统",
        "高空作业安全应急预案",
        "特种工序质量追溯制度",
        "施工测量复核与精度验算",
        "地下管线保护专项方案",
    ],
    "overview": [
        "工程名称与建设地点",
        "项目规模与结构类型",
        "承包范围与合同工期",
        "质量标准与安全目标",
        "施工条件与现场环境",
        "主要工程量概况",
        "项目组织架构与人员",
        "项目主要技术特点",
        "项目建设背景与意义",
        "项目设计依据与标准",
        "项目主要分部分项工程",
        "项目重难点初步识别",
        "项目投资与资金来源",
        "项目合同结构与分包",
        "项目实施总体思路",
        "项目主要风险点概述",
        "项目地理位置与交通条件",
        "周边环境和制约因素分析",
        "项目组织架构与职责分工",
        "项目管理体系与运行机制",
        "项目投资构成与资金筹措",
        "项目主要技术参数清单",
        "项目相关方职责与协作机制",
        "项目实施总体路线图",
        "项目审批手续与合规性审查",
        "项目建设条件踏勘报告",
        "项目主要技术经济指标",
        "项目组织架构人员编制表",
        "项目管理体系框架图",
        "项目实施阶段划分与里程碑",
        "项目相关方沟通计划",
        "项目风险矩阵与应对策略",
        "项目审批流程与进度计划",
        "项目建设条件踏勘记录表",
        "项目主要技术参数清单模板",
        "项目组织架构人员编制方案",
        "项目投资估算与经济评价",
        "项目主要工程量计算书",
        "项目参建单位职责矩阵",
        "项目全周期管理流程图",
    ],
    "coordination": [
        "协调组织体系与职责分工",
        "沟通协调流程与机制",
        "例会制度与决策程序",
        "信息传递与报告渠道",
        "冲突处理与争议解决",
        "外包协调管理方案",
        "政府主管部门协调",
        "社区与公众关系管理",
        "协调会议记录与档案管理",
        "协调效果评估机制",
        "跨专业协同工作方案",
        "外部接口管理方案",
        "协调风险预警机制",
        "协调经验总结与改进",
        "协调制度文件汇编",
        "协调培训计划",
        "多方协调联席会议制度",
        "协调问题升级处理机制",
        "协调信息数字化管理平台",
        "协调效果量化评估指标",
        "外部关系维护与公共关系",
        "协调风险预案与快速响应",
        "协调成果文档标准化模板",
        "协调培训与能力提升计划",
        "协调问题跟踪闭环管理",
        "跨部门协调责任矩阵",
        "外部关系维护年度计划",
        "协调会议纪要标准化模板",
        "协调效果满意度调查机制",
        "协调问题升级处理流程图",
        "协调信息管理系统建设方案",
        "协调培训课件与考核体系",
        "协调问题跟踪闭环管理制度",
        "跨部门协调责任矩阵模板",
        "外部关系维护年度计划方案",
        "协调会议纪要标准化模板汇编",
        "协调机制运转效能审计",
        "多方联席会议议事规则",
        "外部关系公共关系手册",
        "协调问题升级决策流程",
    ],
    "emergency": [
        "应急组织架构与职责",
        "风险识别与预警标准",
        "应急响应分级与流程",
        "应急处置技术方案",
        "应急资源与物资保障",
        "应急演练计划与评估",
        "灾后恢复与总结改进",
        "信息报告与舆论应对",
        "应急培训与教育计划",
        "应急联动与外部协调",
        "自然灾害应急处置",
        "安全事故应急处理",
        "公共卫生事件应急响应",
        "应急资金与后勤保障",
        "应急预案管理体系",
        "应急总结与持续改进",
        "应急指挥体系与决策机制",
        "预警信息发布与响应触发",
        "应急资源储备与调配方案",
        "灾后恢复与业务连续性计划",
        "应急演练效果评估与改进",
        "应急联动外部机构协作机制",
        "应急物资动态盘点与更新",
        "应急响应时间指标与考核",
        "应急值班与报告制度",
        "应急指挥平台运维保障",
        "应急资源跨区域调配预案",
        "应急培训效果评估标准",
        "应急响应时效考核机制",
        "应急演练脚本与评估表",
        "应急物资储备清单与盘点表",
        "应急指挥决策流程图",
        "应急值班与报告制度汇编",
        "应急指挥平台运维保障方案",
        "应急资源跨区域调配预案汇编",
        "应急响应时效考核指标体系",
        "应急资源动态盘点机制",
        "应急联动协议签订方案",
        "应急演练脚本编写规范",
        "应急响应时效考核细则",
    ],
    "layout": [
        "施工总平面布置原则",
        "分区与功能划分方案",
        "临时道路与交通组织",
        "材料堆场与加工区布置",
        "临时办公与生活设施",
        "临时水电设施布置",
        "消防布置与安全通道",
        "排水系统与环保措施",
        "垂直运输设备布置",
        "施工分区与围挡方案",
        "现场标牌与CI形象",
        "季节性平面调整方案",
        "临建拆除与场地恢复",
        "总平面动态管理",
        "施工总平面布置图绘制",
        "平面布置说明与依据",
        "平面布置动态优化机制",
        "临时设施标准化建造规范",
        "施工通道与物流组织优化",
        "临建容量规划与分期安排",
        "平面布置BIM可视化方案",
        "场地复用与二次转运规划",
        "季节性平面调整应急预案",
        "总平面管理责任分区划分",
        "施工总平面分层布置方案",
        "临建设施抗震与消防验算",
        "总平面交通流线优化设计",
        "施工分区与封闭管理方案",
        "临建设施标准化图集",
        "总平面动态调整审批流程",
        "施工道路排水与沉降监测",
        "总平面布置效果评估指标",
        "施工总平面分层布置方案汇编",
        "临建设施抗震消防验算报告",
        "总平面交通流线优化设计方案",
        "施工分区封闭管理实施方案",
        "总平面动态调整审批表",
        "临建设施抗震验算报告",
        "施工道路沉降监测方案",
        "平面布置BIM仿真验证",
    ],
    "analysis": [
        "项目特点与优势分析",
        "施工重难点识别与对策",
        "技术难点分析与方案",
        "管理难点分析与措施",
        "进度难点分析与保障",
        "安全难点分析与防控",
        "质量难点分析与控制",
        "外部环境风险分析",
        "资源供应风险分析",
        "组织协调难点分析",
        "季节性施工难点分析",
        "交叉作业难点分析",
        "项目风险总体评估",
        "难点应对效果预测",
        "重难点动态跟踪机制",
        "分析结论与建议汇总",
        "难点识别方法论与评分模型",
        "重难点分级分类管控策略",
        "技术难点专项论证方案",
        "管理难点组织优化建议",
        "难点应对资源保障分析",
        "重难点动态监测与预警机制",
        "难点应对效果量化评估",
        "难点分析与解决方案库建设",
        "难点识别与分级标准",
        "重难点管控责任矩阵",
        "技术难点专家论证流程",
        "管理难点专项整改方案",
        "难点应对资源平衡分析",
        "重难点动态监测报告模板",
        "难点应对效果评估指标",
        "难点知识库建设方案",
        "难点识别与分级标准汇编",
        "重难点管控责任矩阵模板",
        "技术难点专家论证流程标准化",
        "管理难点专项整改方案汇编",
        "重难点识别评分模型说明",
        "难点应对方案库建设方案",
        "技术难点专家论证纪要",
        "难点知识库建设与维护方案",
    ],
    "measure": [
        "施工总体部署与规划",
        "主要施工方案与技术措施",
        "专项施工方案编制",
        "施工关键工序控制要点",
        "施工资源配置方案",
        "施工节点进度计划",
        "季节性施工保障措施",
        "成品保护具体措施",
        "施工技术方案优化",
        "施工质量控制要点",
        "施工安全管理措施",
        "施工进度保障措施",
        "施工成本控制措施",
        "施工环保保障措施",
        "施工技术管理措施",
        "施工协调管理措施",
        "施工总体部署动态调整方案",
        "重大施工方案专家论证机制",
        "专项施工方案编制深度标准",
        "施工关键工序质量控制卡",
        "施工资源配置弹性调整方案",
        "施工节点里程碑考核办法",
        "季节性施工专项保障方案",
        "成品保护分级管理制度",
        "施工组织设计动态修订流程",
        "重大施工方案专家库建设",
        "专项施工方案编制审查流程",
        "关键工序质量控制卡模板",
        "资源配置弹性调整审批流程",
        "里程碑节点考核奖惩办法",
        "季节性施工专项技术交底",
        "成品保护分级管理制度汇编",
        "施工组织设计动态修订制度",
        "重大施工方案专家库管理办法",
        "专项施工方案编制审查标准",
        "关键工序质量控制卡实施细则",
        "施工组织设计动态修订手册",
        "专项施工方案编制审查指南",
        "里程碑节点考核奖惩办法汇编",
        "施工组织设计动态修订流程标准化",
    ],
    "attachment": [
        "施工总平面布置图",
        "施工进度计划网络图",
        "项目管理组织架构图",
        "专项施工方法流程图",
        "主要机械设备配置表",
        "主要材料进场计划表",
        "劳动力动态曲线图",
        "质量检验试验计划表",
        "安全文明施工检查表",
        "进度计划甘特图",
        "施工总进度计划横道图",
        "项目管理流程图",
        "主要分部分项工程进度计划",
        "材料设备采购计划表",
        "安全应急预案流程图",
        "项目组织机构职责表",
        "施工总进度计划双代号网络图",
        "主要分部分项工程流水段划分图",
        "项目管理岗位职责矩阵表",
        "专项施工方案审批流程图",
        "机械设备维修保养记录表",
        "材料进场验收合格台账",
        "劳动力动态分布热力图",
        "质量检验批次计划汇总表",
        "施工进度计划WBS分解图",
        "安全管理组织机构网络图",
        "质量保证措施落实责任表",
        "安全文明施工达标评价表",
        "主要分部分项工程质量控制表",
        "施工进度偏差分析报告表",
        "材料设备进场验收登记表",
        "劳动力技能等级分布表",
        "施工进度计划里程碑分解图",
        "安全管理组织机构职责网络图",
        "质量保证措施落实责任清单",
        "安全文明施工达标评价细则",
        "施工总进度计划里程碑分解图",
        "质量保证措施落实责任清单模板",
        "安全文明施工达标评价细则汇编",
        "施工进度偏差分析报表体系",
    ],
    "other": [
        "基本组织方案",
        "主要实施措施",
        "质量控制要点",
        "安全管理办法",
        "进度管控方案",
        "资源配置计划",
        "技术保障措施",
        "协调管理方案",
        "现场管理制度",
        "档案资料管理",
        "成本管控措施",
        "环境保护措施",
        "综合管理方案",
        "技术质量控制",
        "进度安全保障",
        "资源协调管理",
        "基础保障体系建设方案",
        "实施过程风险防控措施",
        "质量控制关键节点清单",
        "安全管理红线与底线要求",
        "进度管控节点考核办法",
        "资源统筹调配实施方案",
        "技术保障措施清单",
        "协调管理机制运转方案",
        "基础保障体系组织架构图",
        "风险防控措施责任矩阵",
        "质量控制关键节点检查表",
        "安全管理红线条款清单",
        "进度管控节点考核细则",
        "资源统筹调配决策流程",
        "技术保障措施执行清单",
        "协调管理机制运转流程图",
        "基础保障体系组织架构图模板",
        "风险防控措施责任矩阵模板",
        "质量控制关键节点检查表汇编",
        "安全管理红线条款清单模板",
        "综合保障体系建设方案",
        "风险识别与评估方法论",
        "质量控制闭环管理机制",
        "安全管理标准化建设方案",
    ],
}

def _generate_diverse_l2_title(
    chapter_title: str,
    chapter_type: str,
    child_idx: int,
    chapter_idx: int,
) -> str:
    """生成多样化的二级标题，结合章节类型和索引确保跨章节不重复。"""
    patterns = _L2_TITLE_PATTERNS.get(chapter_type, _L2_TITLE_PATTERNS["other"])
    # 使用 (chapter_idx + child_idx) 取模，跨章节错开
    pattern_idx = (chapter_idx + child_idx) % len(patterns)
    result = patterns[pattern_idx]
    logger.debug(
        "[L2生成] type=%s chapter_idx=%d child_idx=%d pattern_idx=%d/%d -> '%s'",
        chapter_type,
        chapter_idx,
        child_idx,
        pattern_idx,
        len(patterns) - 1,
        result,
    )
    return result

def build_chapter7_framework(scope_list: list[str], chapter_number: int = 7) -> dict:
    """根据承包范围列表生成第七章的二级目录框架（V3.0 商业级）。

    关键设计（依据《标书智能体（二）——生成标书提纲代码+提示词（最终增强版 V3.0）》）：
      - 标题纯文本，不带"第X章"前缀（编号由前端/导出器统一加注）
      - 插入补充章节后，7 章可能不再是第 7 章，因此 id 暂占位，由 normalize_outline_items 重新编号
      - 强制每个承包范围都生成二级子节点，二级标题"X工程"格式

    Args:
        scope_list: 承包范围列表，如 ["土方", "主体结构", "机电安装"]
        chapter_number: 章节编号（占位用）
    Returns:
        dict: 一级节点框架，含二级子节点
    """
    children = []
    for idx, item in enumerate(scope_list, start=1):
        # 标题纯文本：去掉原 chapter_number 硬编码 id，由 normalize 统一编号。
        # V3.6 修复：承包范围术语原样保留（如"主体结构"不应被加成"主体结构工程"）。
        # 仅当项本身既不含"工程"也非已知标准分部名时，才追加"工程"后缀，
        # 避免破坏招投标标准术语。
        _std_names = {
            "主体结构",
            "砌体",
            "装饰装修",
            "机电安装",
            "给排水",
            "采暖",
            "通风",
            "空调",
            "电气",
            "智能建筑",
            "电梯",
            "节能",
            "屋面",
        }
        if item.endswith("工程") or item in _std_names or "结构" in item or "装修" in item:
            title = item
        else:
            title = f"{item}工程"
        children.append(
            {
                "id": f"X.{idx}",  # 占位 ID，由 normalize_outline_items 重新分配
                "title": title,
                "description": f"{title}施工方案",
                "level": 2,
                "children": [],
            }
        )
    return {
        "id": "X",  # 占位 ID（最终由 normalize 重新编号）
        "title": "主要分部工程施工方法",  # 纯文本：与 28 章模板格式完全一致
        "level": 1,
        "children": children,
    }


_CN_NUMS = ["一", "二", "三", "四", "五", "六", "七", "八", "九", "十"]

def build_framework_for_chapter(
    chapter: dict,
    dist: dict,
    chapter_number: int,
) -> dict:
    """为普通章节构建二三级目录框架（V3.3 商业级）。

    关键设计：
      - 标题纯文本，不带"第X章"前缀（编号由前端/导出器统一加注）
      - id 使用"X"占位，最终由 normalize_outline_items 重新编号
      - 保证最小节点数：每个一级 ≥3 二级，每个二级 ≥3 三级
      - 默认标题不可为空（防止 normalize_outline_items 过滤掉空节点，
        确保 AI 填充失败回退时仍保留完整的目录结构）
      - V3.3 优化：框架使用通用占位标题（如"【待填充】二级标题1"），
        强制 AI 根据项目实际内容生成针对性标题，不再使用模板中的具体标题。
        模板标题仅作为参考建议写入提示词。

    Args:
        chapter: 一级章节信息
        dist: 节点分布信息（含 level2_count 和 leaf_per_level2）
        chapter_number: 章节编号（占位用）
    Returns:
        dict: 含固定数量子节点的 JSON 框架（标题为通用占位，等待 AI 填充）
    """
    chapter_title = chapter.get("title", "")

    children = []
    level2_count = dist["level2_count"]
    leaf_per_level2 = dist["leaf_per_level2"]
    for i in range(level2_count):
        leaf_count = leaf_per_level2[i] if i < len(leaf_per_level2) else 3
        # V3.3 使用通用占位标题，强制 AI 替换
        l2_placeholder = f"【请根据项目实际替换为具体标题】二级第{i+1}项"
        grandchildren = []
        for j in range(leaf_count):
            l3_placeholder = f"【请替换】三级第{j+1}项"
            grandchildren.append(
                {
                    "id": f"X.X.{j+1}",  # 占位 ID
                    "title": l3_placeholder,
                    "description": "",
                    "level": 3,
                    "children": [],
                }
            )
        children.append(
            {
                "id": f"X.{i+1}",  # 占位 ID
                "title": l2_placeholder,
                "description": "",
                "level": 2,
                "children": grandchildren,
            }
        )
    return {
        "id": "X",  # 占位 ID（最终由 normalize 重新编号）
        "title": chapter_title,  # 纯文本标题
        "level": 1,
        "children": children,
    }

def sanitize_framework_titles(
    framework: dict,
    chapter_title: str = "",
    chapter_idx: int = 0,
    global_used_titles: set | None = None,
) -> dict:
    """替换框架中的占位标题为项目相关标题，避免占位文本存入数据库。

    ⚠️ 【已废弃 · 不在生产链路】BUG-FIX 2026-10-12（死代码治理）

    本函数**没有任何生产调用方**，且其行为被 2026-10-09/10/11 三轮三级标题
    占位治理"绕过"了还不自知 —— 这正是最危险的地方：

      · docstring 原文声称「当 AI 填充失败时，``fill_children_with_retry`` 会
        返回原始框架」。**该说法自 2026-09 起就已失效**：`fill_children_with_retry`
        是 `workflows.py::_legacy_stub` 里的退役桩，直接抛 NotImplementedError。
        陈旧的依赖描述会让后来者以为本函数仍在生效。
      · V5.1 主链路的真实兜底是 `workflows_outline_v51._default_l2_children_for_skeleton`
        （由 `_generate_level2_v51` 在 AI 填充失败时调用），它走
        `_derive_child_title(level=2/3)` 按父级语义派生，**与本函数是两套完全
        不同的标题生成策略**。
      · 因此本函数里那 15 个单测（test_diversity / test_e2e_diversity）验证的是
        一条**不会运行**的链路，提供的是虚假信心。

    之所以保留函数体而不是直接删除：它仍是本仓唯一一处「跨章节全局去重 +
    语义化消歧」的完整示例，且 2026-10-12 已把其最后那个裸编号后缀生产点
    `f"{new_title}（{chapter_idx + 1}）"` 换成 `_disambiguate_title` —— 万一将来
    要复活这条链路，代码已经是正确的。

    保留的同时由 `tests/test_outline_module_audit_2026_10_12.py` 钉住两条不变式：
      1) 本模块（除 tests 外）不得再 import 它，防止它悄悄变成影子链路；
      2) 真实兜底 `_default_l2_children_for_skeleton` 产出的标题零占位。

    历史行为（供复活时参考）：
    替换「【请根据项目实际替换为具体标题】二级第1项」这类占位标题；
    V5.2 起支持跨章节全局去重（``global_used_titles``），未传入时降级为章节内去重。

    Args:
        framework: AI 生成的目录框架（可能含占位标题）
        chapter_title: 一级章节标题（用于生成项目相关的二级/三级标题）
        chapter_idx: 章节序号（用于跨章节去重，确保不同章节的兜底标题不同）
        global_used_titles: 跨章节共享的已用标题池；不传则退化为章节内去重。

    Returns:
        dict: 占位标题已被替换的框架
    """
    # 延迟导入：_generate_diverse_l3_title 定义在 workflows_outline_v51，
    # 该模块依赖本文件（outline_common），故不能模块级导入。
    from .workflows_outline_v51 import _generate_diverse_l3_title  # noqa: F401

    # 初始化全局去重池（caller 若提供则共用同一个 set，实现跨章节去重）
    _reuse_global = global_used_titles is not None
    pool = global_used_titles if _reuse_global else set()

    result = copy.deepcopy(framework)
    children = result.get("children", [])

    # 章节内去重集合 = 全局池快照 + 本章节已确认标题
    chapter_used_titles: set[str] = set(pool)

    # 章节类型分类，用于生成多样化的兜底标题
    chapter_type = _classify_chapter_type(chapter_title)
    logger.info(
        "[兜底标题] 章节[%s] type=%s idx=%d children=%d global_pool=%s",
        chapter_title,
        chapter_type,
        chapter_idx,
        len(children),
        len(pool) if _reuse_global else "disabled",
    )

    l2_replaced = 0
    l3_replaced = 0
    l3_retry_count = 0

    for i, child in enumerate(children):
        title = child.get("title", "")
        if "【请根据项目" in title or "【待填充】" in title or "二级第" in title:
            # L2 也走全局去重：若生成的 L2 已在池中则加章节前缀
            new_title = _generate_diverse_l2_title(chapter_title, chapter_type, i, chapter_idx)
            if new_title in pool:
                new_title = f"{chapter_title}—{new_title}"
            child["title"] = new_title
            pool.add(new_title)
            chapter_used_titles.add(new_title)
            l2_replaced += 1
            logger.info("[兜底标题] L2[%d] placeholder='%s' -> '%s'", i, title[:30], new_title)

        grandchildren = child.get("children", [])
        for j, gc in enumerate(grandchildren):
            gc_title = gc.get("title", "")
            if gc_title:
                pool.add(gc_title)
                chapter_used_titles.add(gc_title)
            if "【请替换】" in gc_title or "三级第" in gc_title:
                # 适配 V5.1 _generate_diverse_l3_title 签名:
                # (l2_title, idx, l1_title='', used_titles=None, l2_index=0)
                l2_title = child.get("title", "")
                new_title = _generate_diverse_l3_title(
                    l2_title,                 # l2_title
                    j,                        # idx
                    chapter_title,            # l1_title
                    chapter_used_titles,      # used_titles（章节内）
                    i,                        # l2_index
                )
                # 跨章节全局去重：冲突时最多重试 5 次
                _max_retries = 5
                _attempt = 0
                while new_title in pool and _attempt < _max_retries:
                    _attempt += 1
                    l3_retry_count += 1
                    # 换一个 idx 偏移重新生成（让候选空间偏移）
                    new_title = _generate_diverse_l3_title(
                        l2_title,
                        j + _attempt * 7,  # idx 偏移打散候选
                        chapter_title,
                        chapter_used_titles,
                        i + _attempt,
                    )
                # 最终兜底：保证全局唯一。
                #
                # BUG-FIX 2026-10-12（P1）：原写作 `f"{new_title}（{chapter_idx + 1}）"`，
                # 即本仓库**最后一个仍在生产「（N）」裸编号后缀**的代码点 —— 正是用户
                # 报告的「总体实施策划（2）/（3）」形态之一（此处为「第 N 章」变体）。
                # 该后缀既无语义、又会被 `_is_dirty_l3_title` 判脏、被
                # `_title_core` 归一化剥离，等于在目录里埋了一颗定时炸弹：落库前的
                # 占位清洗会把它改写掉，产生一次无谓的重命名抖动。
                # 改用与全局其余消歧路径同一份语义化实现 `_disambiguate_title`
                # （专业维度词 → 父级差异词前缀 → 「之N」中文序数），既保证唯一，
                # 又不产出裸编号。父级标题传 l2，使前缀消歧与该节点语义相关。
                if new_title in pool:
                    new_title = _disambiguate_title(
                        new_title,
                        {_title_core(_t) for _t in pool},
                        parent_title=l2_title,
                    )
                    # 理论上 `_disambiguate_title` 必然返回未占用的候选（其内部
                    # 「之N」分支已扫描 2..999）；极端情况下若仍冲突，继续向上
                    # 探。2026-10-15 加固：旧实现 `_guard = 101` 起步、`< 200` 收口，
                    # 跳过了 2..100 整段可用区间，且旧 `_cn_ordinal` 在 n≥100 返回
                    # 阿拉伯数字 —— 一旦触发必然产出「X之101」。现从 2 起步。
                    _pool_core = {_title_core(_t) for _t in pool}
                    _guard = 2
                    while (
                        _title_core(new_title) in _pool_core
                        and _guard < 10000
                    ):
                        new_title = f"{_strip_ordinal_suffix(new_title)}之{_cn_ordinal(_guard)}"
                        _guard += 1

                logger.debug(
                    "[兜底标题] L3[%d][%d] placeholder='%s' -> '%s' (retries=%d)",
                    i,
                    j,
                    gc_title[:30],
                    new_title,
                    _attempt,
                )
                gc["title"] = new_title
                pool.add(new_title)
                chapter_used_titles.add(new_title)
                l3_replaced += 1

    logger.info(
        "[兜底标题] 章节[%s] 完成: L2替换%d, L3替换%d, L3重试%d, 全局池=%d",
        chapter_title,
        l2_replaced,
        l3_replaced,
        l3_retry_count,
        len(pool),
    )
    return result

