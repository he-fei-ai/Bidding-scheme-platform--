"""outline_common.py — 目录生成共享函数库（V5.1 唯一路径核心）

从已退役的 V2/V3 模块抽离的跨策略共享函数：
- 全局标题去重（_uniquify_outline_titles）
- 最小子节点保障（_ensure_min_children）
- 节点分布生成（generate_nodes_distribution）
- JSON 骨架构建（generate_one_outline_json_by_level1）
- JSON 结构校验（check_json）
- 承包范围解析（_parse_scope_items）
- 节点计数（_count_outline_nodes）

所有新代码应直接 import 本模块。
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
        "进度安排与资源保障",
        "成品保护与现场管理",
        "检验试验与记录管理",
        "测量放线与工序交接",
        "季节性施工与应急措施",
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
    ),
    "schedule": (
        "里程碑节点控制",
        "关键线路分析与保障",
        "资源投入与进度匹配",
        "偏差监测与纠偏措施",
        "周月计划分解与考核",
        "工期风险预警与应对",
    ),
    "cost": (
        "目标成本分解与控制",
        "变更签证与索赔管理",
        "资金计划与支付管理",
        "成本核算与偏差分析",
        "材料量价双控措施",
    ),
    "org": (
        "组织架构与职责分工",
        "管理制度与考核机制",
        "沟通协调与例会制度",
        "人才配置与梯队建设",
        "信息化管理与报表",
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
    ),
    "generic": (
        "总体实施策划",
        "分阶段推进计划",
        "资源配置与协调保障",
        "过程控制与考核评价",
        "总结评估与持续改进",
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
    ),
    "quality": (
        "质量保证组织体系",
        "质量控制与验收要求",
        "创优目标分解与实施",
        "质量通病防治措施",
        "质量检查与考核机制",
    ),
    "safety": (
        "安全管理组织体系",
        "安全防护与技术措施",
        "应急预案与演练计划",
        "隐患排查与治理机制",
        "文明施工与环保措施",
    ),
    "schedule": (
        "进度计划编制与平衡",
        "关键线路分析与控制",
        "资源投入与保障措施",
        "进度动态监测与纠偏",
    ),
    "cost": ("成本管理组织体系", "目标成本分解与控制", "合同与索赔管理", "成本核算与偏差分析"),
    "org": ("组织架构与职责分工", "管理制度与考核机制", "沟通协调机制", "信息化管理平台建设"),
    "tech": (
        "主要分部工程施工方案",
        "关键工序施工技术措施",
        "专项施工方案与工艺",
        "施工测量与试验检测",
        "季节性施工措施",
        "施工机械与设备保障",
    ),
    "generic": (
        "总体方案与实施策划",
        "分阶段推进计划",
        "资源配置与协调保障",
        "过程控制与考核机制",
        "总结评估与持续改进",
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


def _derive_child_title(parent_title: str, idx: int, used: set[str], level: int) -> str:
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
    """
    parent_title = (parent_title or "").strip() or "工程实施"
    dom = _detect_domain(parent_title)
    pool = _L3_SUFFIX_POOL[dom] if level == 3 else _L2_SUFFIX_POOL[dom]
    generic_pool = _L3_SUFFIX_POOL["generic"] if level == 3 else _L2_SUFFIX_POOL["generic"]
    core = _core_for_derive(parent_title)

    # === 规则 3 核心改动：L3 完全独立 ===
    if level == 3:
        # 禁用父级词汇的前缀拼合
        prefix_core = ""
        # 域池剔除含父级核心短语的条目（严格语义隔离）
        def _filter_phrases(src: tuple[str, ...]) -> list[str]:
            if not core:
                return list(src)
            return [s for s in src if core not in s]

        pool_filtered = _filter_phrases(pool)
        generic_filtered = _filter_phrases(generic_pool)
        # 主域池耗尽时再用 generic 池补充（先过滤再补充）
        if not pool_filtered and not generic_filtered:
            # 极端兜底：核心短语屏蔽后两池全空 → 退回未过滤池，
            # 避免节点生成失败；此时仍无前缀拼合，语义解耦基本成立
            full_pool = list(pool) + list(generic_pool)
        else:
            full_pool = list(pool_filtered) + list(generic_filtered)
    else:
        prefix_core = core
        full_pool = list(pool) + list(generic_pool)

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
            return cand
    # 极端兜底：所有池都耗尽（level=3 不再追加父级前缀）
    # BUG-OUTLINE-INFLOOP 修复：原 while True 内 n 未自增，若 "专项{n}" 恰好在
    # used 集合中会导致真·无限循环。改为每轮 n += 1 直至找到未占用标题。
    n = idx + 1
    while True:
        cand = f"专项{n}"
        if cand not in used:
            return cand
        n += 1


def _generate_fallback_l3_for_ensure(l2_title: str, idx: int, l2_index: int = 0) -> str:
    """为 _ensure_min_children 生成专业的兜底三级标题。

    从 L2 标题按语义域派生：不同 L2 核心词不同 → 标题不重名；
    前缀核心词注入 → 三级与父级二级目录语义关联。
    """
    return _derive_child_title(l2_title or "工程实施", idx, set(), level=3)


# ========== 全局标题去重 ==========


def _uniquify_outline_titles(level1: list[dict]) -> list[dict]:
    """全局标题去重安全网（目录重名修复，V5.1 共用）。

    遍历整个目录树，维护全局归一化标题集合：
    - **2026-09-04 用户定制规则 3 适配**：禁止用父级标题做防重名前缀
      （L3-L5 标题不得继承父级词汇）。改为直接追加序号 "（{n}）" 保证唯一。
    - 只重命名「重复」的标题，唯一标题保持不动。
    """

    def _core(t: str) -> str:
        tt = (t or "").strip().lower()
        tt = re.sub(r"[\s\u3000]+", "", tt)
        tt = re.sub(r"[^\w\u4e00-\u9fff]", "", tt)
        return tt

    seen: set[str] = set()

    def _walk(nodes: list[dict]) -> None:
        for node in nodes:
            if not isinstance(node, dict):
                continue
            title = node.get("title", "")
            core = _core(title) if title else ""
            if core:
                if core in seen:
                    ncore = core
                    _cnt = 2
                    while ncore in seen:
                        new_title = f"{title}（{_cnt}）"
                        ncore = _core(new_title)
                        _cnt += 1
                    logger.info("V5.1 全局去重: 「%s」->「%s」（避免重名，仅追加序号）", title, new_title)
                    node["title"] = new_title
                    core = ncore
                seen.add(core)
            children = node.get("children")
            if isinstance(children, list):
                _walk(children)

    _walk(level1)
    return level1


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
                    l3_title = _derive_child_title(
                        l2_title,
                        j,
                        set(),  # 本 L2 的 L3 列表初始为空
                        level=3,
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
                        str(child.get("title") or ""), _l3_filled, global_used, level=3
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
                        child.get("title", ""), _l3_filled, used_l3 | global_used, level=3
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

    当 AI 填充失败时，fill_children_with_retry 会返回原始框架，
    其中包含如"【请根据项目实际替换为具体标题】二级第1项"的占位标题。
    此函数将这些占位标题替换为基于项目章节名的实际标题，
    并使用章节类型感知策略生成多样化标题，避免不同章节使用相同模式。

    V5.2 增强：支持跨章节全局去重（global_used_titles），
    防止多个章节各自独立兜底时生成相同的 L2/L3 标题。
    未传入时自动降级为章节内去重（向后兼容）。

    Args:
        framework: AI 生成的目录框架（可能含占位标题）
        chapter_title: 一级章节标题（用于生成项目相关的二级/三级标题）
        chapter_idx: 章节序号（用于跨章节去重，确保不同章节的兜底标题不同）
        global_used_titles: 跨章节共享的已用标题池（所有章节共用同一个 set）。
                            传入时函数会从中读取 + 写入，实现跨章节去重；
                            不传入则退化为章节内去重（V5.1 行为）。

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
                # 最终兜底：加章节索引后缀保证唯一
                if new_title in pool:
                    new_title = f"{new_title}（{chapter_idx + 1}）"

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

