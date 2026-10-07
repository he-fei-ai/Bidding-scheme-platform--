"""tech_knowledge.py — 建筑业技术知识库

为目录生成提供「新技术/最成熟方案」导向的技术词条，
注入 L1/L3 提示词与后处理流程，确保 L3 标题含具体技术含量。

来源：《建筑业10项新技术（最新版）》、四新技术名录、国家级/省级工法名录
"""

TECH_KNOWLEDGE = {
    "new_tech": [
        {
            "term": "智能建造",
            "keywords": ["BIM", "智慧工地", "物联网监测", "建筑信息模型"],
            "apply_to": ["施工部署", "施工方案", "项目管理"],
        },
        {
            "term": "装配式建筑",
            "keywords": ["预制构件吊装", "灌浆套筒连接", "套筒灌浆饱满度检测", "装配式混凝土"],
            "apply_to": ["专项施工方案"],
        },
        {
            "term": "绿色施工",
            "keywords": ["四节一环保", "建筑垃圾减量化", "扬尘在线监测", "绿色建筑"],
            "apply_to": ["绿色施工措施", "环保方案"],
        },
        {
            "term": "数字孪生监测",
            "keywords": ["深基坑自动化监测", "高支模变形实时预警", "结构健康监测"],
            "apply_to": ["监测方案", "安全管理"],
        },
        {
            "term": "智能张拉压浆",
            "keywords": ["预应力智能张拉", "循环智能压浆", "预应力施工"],
            "apply_to": ["预应力专项施工方案"],
        },
        {
            "term": "无人机巡检",
            "keywords": ["土方量测算", "进度航拍比对", "无人机航测"],
            "apply_to": ["进度管理", "测量方案"],
        },
        {
            "term": "建造机器人",
            "keywords": ["测量机器人", "地面整平机器人", "砌筑机器人"],
            "apply_to": ["施工装备", "施工工艺"],
        },
        {
            "term": "BIM 深化设计",
            "keywords": ["管线综合排布", "碰撞检测", "深化设计", "4D 模拟"],
            "apply_to": ["施工方案", "项目管理"],
        },
        {
            "term": "智慧工地",
            "keywords": ["AI 安全监控", "人员定位", "设备管理", "智能调度"],
            "apply_to": ["项目管理", "安全管理"],
        },
        {
            "term": "低碳建造",
            "keywords": ["碳排放计算", "低碳材料", "能耗监测", "碳中和"],
            "apply_to": ["绿色施工", "环保方案"],
        },
    ],
    "mature_method": [
        {"term": "逆作法", "keywords": ["地下结构逆作", "一桩一柱", "逆作施工"]},
        {"term": "全护筒全回转钻进", "keywords": ["临近既有建筑桩基", "全护筒钻进", "桩基保护"]},
        {"term": "液压爬模", "keywords": ["核心筒结构施工", "液压爬模", "超高层施工"]},
        {"term": "盘扣式脚手架", "keywords": ["高支模支撑体系", "盘扣架", "模板支撑"]},
        {"term": "TRD/CSM工法", "keywords": ["深基坑止水帷幕", "TRD工法", "CSM工法", "水泥土搅拌"]},
        {"term": "盾构法施工", "keywords": ["盾构隧道", "管片拼装", "盾构掘进"]},
        {"term": "暗挖法", "keywords": ["暗挖隧道", "新奥法", "浅埋暗挖"]},
        {"term": "预应力管桩", "keywords": ["PHC管桩", "静压管桩", "锤击沉桩"]},
        {"term": "大体积混凝土温控", "keywords": ["大体积混凝土", "温控方案", "混凝土养护"]},
        {"term": "钢箱梁顶推", "keywords": ["钢箱梁", "顶推施工", "桥梁架设"]},
        {"term": "移动模架", "keywords": ["移动模架", "桥梁施工", "现浇箱梁"]},
        {"term": "反拱底板", "keywords": ["反拱底板", "抗浮设计"]},
    ],
}

# 通用标题（无技术含量）判定模式
_GENERIC_TITLE_PATTERNS = (
    "基本要求",
    "实施要点",
    "主要内容",
    "关键措施",
    "施工准备",
    "施工工艺",
    "质量控制",
    "通用要求",
    "基本方案",
    "实施流程",
    "质量检查",
    "验收标准",
    "项目概述",
    "项目概况",
    "项目简介",
    "项目背景",
    "安全管理",
    "质量管理",
    "进度管理",
    "成本管理",
    "施工管理",
    "现场管理",
    "组织管理",
    "资源管理",
)


def get_tech_candidates(keywords: list[str] | None = None) -> list[str]:
    """获取技术候选词条（用于注入 L3 提示词的 tech_candidates）"""
    candidates = []
    for category in TECH_KNOWLEDGE.values():
        for item in category:
            candidates.append(item["term"])
            if keywords:
                candidates.extend(item["keywords"])
    return list(dict.fromkeys(candidates))  # 去重保序


def is_generic_title(title: str) -> bool:
    """判断三级标题是否为无技术含量的通用占位标题"""
    if not title:
        return True
    for pattern in _GENERIC_TITLE_PATTERNS:
        if pattern in title:
            # 匹配到通用模式，但标题有实质内容时不过滤
            # 例如 "混凝土质量控制" 有技术含量，"质量控制" 则无
            if len(title) <= len(pattern) + 4:
                return True
    return False


def get_tech_term_for_domain(domain: str) -> str | None:
    """根据领域关键词匹配对应技术词条。

    匹配优先级：
    1. 精确匹配 apply_to 字段
    2. 关键词在 domain 中出现
    3. 技术术语在 domain 中出现
    4. 通用兜底：返回第一个新基建词条
    """
    for category in TECH_KNOWLEDGE.values():
        for item in category:
            # 匹配 apply_to 字段
            for app in item.get("apply_to", []):
                if app in domain or domain in app:
                    return item["term"]
            # 匹配关键词
            if item["term"] in domain:
                return item["term"]
            for kw in item["keywords"]:
                if kw in domain:
                    return item["term"]
    # 兜底：通用智能建造
    return "智能建造" if domain else None


def get_fallback_tech(title: str) -> str:
    """为通用标题提供兜底技术词条（基于语义相似度）"""
    # 按标题关键词匹配最相关的技术
    keyword_map = {
        "质量": "智能质量",
        "安全": "AI 智能安全监控",
        "进度": "BIM 4D 进度模拟",
        "成本": "5D 成本管控",
        "材料": "智慧物料管理",
        "组织": "智能建造",
        "施工": "装配式建筑",
        "管理": "智慧工地",
        "控制": "数字孪生",
        "检查": "智能质量检测",
        "验收": "智能质量验收",
        "监测": "自动化监测",
        "数据": "建筑信息模型",
        "环境": "绿色施工",
        "绿色": "低碳建造",
        "环保": "低碳建造",
        "计量": "无人机测绘",
        "测量": "无人机航测",
        "设计": "BIM 深化设计",
        "部署": "智能建造",
    }
    for key, tech in keyword_map.items():
        if key in title:
            return tech
    return "智能建造"
