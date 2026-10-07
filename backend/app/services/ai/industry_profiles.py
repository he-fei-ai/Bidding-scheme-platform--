"""行业知识库模块

内置 12 个行业的专业知识库，指导 AI 生成更贴合行业特点的标书内容。

V2.0 新增：缓存机制
- 使用 functools.lru_cache 缓存行业资料查询结果
- TTL: 5 分钟，支持手动失效
- 避免重复的字符串匹配计算
"""

import functools
import time


# ========== 行业知识库数据（不变）==========

INDUSTRY_PROFILES = {
    "通用技术服务": {
        "keywords": ["技术服务", "项目实施", "运维"],
        "profile": "本行业为通用技术服务行业，标书应包括：公司技术实力与服务团队介绍、"
        "项目实施方法论与流程、质量保障与服务体系、应急预案与响应机制。"
        "重点突出标准化流程管理和专业服务能力。",
    },
    "消防工程": {
        "keywords": ["消防", "喷淋", "消火栓", "报警", "联动"],
        "profile": "本行业为消防工程行业，标书应包括：消防系统设计与施工方案、"
        "火灾自动报警系统、自动喷水灭火系统、消火栓系统、防排烟系统。"
        "需重点描述与国家消防规范的符合性、系统联动调试方案、"
        "消防验收配合措施。",
    },
    "弱电智能化": {
        "keywords": ["弱电", "智能化", "综合布线", "监控", "安防"],
        "profile": "本行业为弱电智能化行业，标书应包括：智能化系统总体架构设计、"
        "综合布线系统、安防监控系统、门禁一卡通系统、楼宇自控系统。"
        "重点突出系统集成能力、设备选型合理性、各子系统联动方案。",
    },
    "电力施工": {
        "keywords": ["电力", "配电", "电缆", "变配电", "高低压"],
        "profile": "本行业为电力施工行业，标书应包括：电力工程施工组织设计、"
        "变配电系统安装方案、电缆敷设与接线方案、电气试验与调试方案。"
        "需重点描述施工安全措施、停电过渡方案、与供电部门协调配合。",
    },
    "广告施工": {
        "keywords": ["广告", "标识", "标牌", "灯箱", "展示"],
        "profile": "本行业为广告制作与施工行业，标书应包括：广告设计与制作流程、"
        "标识系统制作安装方案、灯箱制作工艺、现场安装与安全措施。"
        "重点突出设计能力、制作工艺水平、安装质量和安全保证。",
    },
    "装饰装修": {
        "keywords": ["装修", "装饰", "吊顶", "墙面", "精装"],
        "profile": "本行业为装饰装修行业，标书应包括：装饰装修施工组织设计、"
        "各工种施工工艺（泥水、木工、油漆、水电）、材料选用与质量控制。"
        "重点突出精装修细节处理能力、环保材料使用、成品保护措施。",
    },
    "市政/园林": {
        "keywords": ["市政", "园林", "绿化", "景观", "道路"],
        "profile": "本行业为市政与园林行业，标书应包括：市政工程施工组织设计、"
        "园林绿化施工方案、景观小品制作安装、绿化养护方案。"
        "重点突出与市政管线的协调施工、苗木选型与成活率保证、"
        "文明施工与交通疏导方案。",
    },
    "设备采购安装": {
        "keywords": ["设备", "采购", "安装", "调试", "供货"],
        "profile": "本行业为设备采购安装行业，标书应包括：设备供货方案、"
        "设备安装调试方案、设备技术参数响应、质量控制与检验计划。"
        "重点突出设备选型合理性、供货周期保证、安装调试能力、"
        "售后服务和备品备件供应。",
    },
    "物业/运维服务": {
        "keywords": ["物业", "运维", "巡检", "维修", "保养"],
        "profile": "本行业为物业与运维服务行业，标书应包括：服务组织架构与人员配置、"
        "日常运维巡检方案、维修响应机制、应急预案与值班制度。"
        "重点突出服务团队专业能力、响应时效性、质量管理体系。",
    },
    "公路水运试验检测": {
        "keywords": ["试验检测", "桥梁检测", "取样", "公路检测", "水运检测"],
        "profile": "本行业为公路水运试验检测行业，标书应包括：试验检测方案、"
        "检测方法与标准规范、检测设备与人员配置、数据管理与报告制度。"
        "需重点描述与行业规范（公路/水运工程试验检测规范）的符合性、"
        "检测数据真实性和可追溯性。",
    },
    "电梯以旧换新/改造": {
        "keywords": ["电梯", "更新改造", "曳引机", "电梯改造", "旧梯换新"],
        "profile": "本行业为电梯以旧换新/改造行业，标书应包括：旧电梯拆除方案、"
        "新电梯安装调试方案、电梯改造技术方案、安全防护与应急预案。"
        "需重点描述电梯性能参数响应、施工期间楼内垂直交通保障方案、"
        "与特种设备检验机构的配合。",
    },
    "石油化工": {
        "keywords": ["石油化工", "原油", "天然气", "防爆", "化工"],
        "profile": "本行业为石油化工行业，标书应包括：石油化工装置施工方案、"
        "防爆区域施工安全措施、特种设备安装方案、焊接与无损检测方案。"
        "需重点突出 HSE（健康安全环保）管理体系、防爆施工规范符合性、"
        "化工介质管道试压与吹扫方案。",
    },
}


# ========== 缓存机制 ==========


class IndustryProfileCache:
    """行业资料缓存器：缓存不常变化的行业知识数据

    功能：
    1. TTL 缓存：5 分钟过期时间
    2. 手动失效：支持清除特定或全部缓存
    3. 统计信息：记录命中率
    4. 线程安全：使用锁保护
    """

    def __init__(self, ttl_seconds: int = 300, max_size: int = 50):
        self.ttl = ttl_seconds  # 默认 5 分钟
        self.max_size = max_size  # 最多缓存 50 个行业
        self._cache: dict[str, tuple[float, str]] = {}  # key -> (timestamp, value)
        self._lock = None  # 延迟初始化
        self._hits = 0
        self._misses = 0

    def _get_lock(self):
        """延迟导入 threading.Lock"""
        if self._lock is None:
            import threading

            self._lock = threading.Lock()
        return self._lock

    def get(self, industry: str) -> str | None:
        """获取缓存的行业资料

        Returns:
            str: 行业资料内容，如果缓存命中则返回
            None: 如果缓存未命中或已过期
        """
        with self._get_lock():
            if industry in self._cache:
                timestamp, content = self._cache[industry]
                # 检查是否过期
                if time.time() - timestamp < self.ttl:
                    self._hits += 1
                    return content
                else:
                    # 过期，删除
                    del self._cache[industry]

            self._misses += 1
            return None

    def set(self, industry: str, content: str) -> None:
        """设置缓存"""
        with self._get_lock():
            # 达到上限时淘汰最旧的一半
            if len(self._cache) >= self.max_size:
                sorted_items = sorted(self._cache.items(), key=lambda x: x[1][0])
                for k, _ in sorted_items[: len(sorted_items) // 2]:
                    self._cache.pop(k, None)
            self._cache[industry] = (time.time(), content)

    def invalidate(self, industry: str) -> bool:
        """使特定行业的缓存失效"""
        with self._get_lock():
            if industry in self._cache:
                del self._cache[industry]
                return True
            return False

    def clear(self) -> int:
        """清空所有缓存"""
        with self._get_lock():
            count = len(self._cache)
            self._cache.clear()
            return count

    def get_stats(self) -> dict:
        """获取缓存统计信息"""
        with self._get_lock():
            total = self._hits + self._misses
            return {
                "cache_size": len(self._cache),
                "hits": self._hits,
                "misses": self._misses,
                "hit_rate": f"{self._hits / total * 100:.1f}%" if total > 0 else "0.0%",
                "ttl_seconds": self.ttl,
            }


# 全局行业资料缓存实例
_industry_profile_cache = IndustryProfileCache(ttl_seconds=300)


# ========== 带缓存的查询函数 ==========


@functools.lru_cache(maxsize=64)
def _get_industry_profile_cached(industry: str) -> str:
    """带 lru_cache 的行业资料查询（不可变参数，线程安全）

    注意：此函数用于静态数据，lru_cache 在进程生命周期内有效
    """
    # 调用原始逻辑
    return _lookup_industry_profile(industry)


def _lookup_industry_profile(industry: str) -> str:
    """查找行业资料的底层实现"""
    # 1. 精确匹配
    if industry in INDUSTRY_PROFILES:
        return INDUSTRY_PROFILES[industry]["profile"]

    # 2. 关键词子串匹配
    for _name, profile in INDUSTRY_PROFILES.items():
        for kw in profile["keywords"]:
            if kw in industry or industry in kw:
                return profile["profile"]

    # 3. 回退到通用模板
    return INDUSTRY_PROFILES["通用技术服务"]["profile"]


def get_industry_profile(industry: str) -> str:
    """根据行业名称获取对应的知识库内容（带缓存）

    匹配逻辑：
    1. 先查缓存（TTL 5 分钟）
    2. 再查 lru_cache（进程级缓存）
    3. 最后执行底层查找并回填缓存

    Args:
        industry: 行业名称

    Returns:
        str: 行业知识库内容
    """
    # 1. 尝试 TTL 缓存
    cached = _industry_profile_cache.get(industry)
    if cached:
        return cached

    # 2. 尝试 lru_cache
    try:
        result = _get_industry_profile_cached(industry)
        # 回填 TTL 缓存
        _industry_profile_cache.set(industry, result)
        return result
    except Exception:
        # lru_cache 失败时降级到直接查找
        pass

    # 3. 底层查找
    result = _lookup_industry_profile(industry)

    # 4. 回填缓存
    _industry_profile_cache.set(industry, result)

    return result


def clear_industry_profile_cache(industry: str | None = None) -> dict:
    """清除行业资料缓存

    Args:
        industry: 指定行业名称，如果为 None 则清除全部缓存

    Returns:
        dict: 操作结果
    """
    if industry:
        success = _industry_profile_cache.invalidate(industry)
        # 同时清除 lru_cache 中的条目
        try:
            _get_industry_profile_cached.cache_clear()
        except Exception as _e:
            pass
        return {
            "status": "success" if success else "not_found",
            "message": f"已清除 [{industry}] 的行业资料缓存"
            if success
            else f"未找到 [{industry}] 的缓存",
        }
    else:
        count = _industry_profile_cache.clear()
        try:
            _get_industry_profile_cached.cache_clear()
        except Exception as _e:
            pass
        return {
            "status": "success",
            "message": f"已清空所有行业资料缓存（共 {count} 条）",
        }


def get_industry_profile_cache_stats() -> dict:
    """获取行业资料缓存统计信息"""
    stats = _industry_profile_cache.get_stats()
    # 添加 lru_cache 信息
    try:
        lru_info = _get_industry_profile_cached.cache_info()
        stats["lru_cache"] = {
            "hits": lru_info.hits,
            "misses": lru_info.misses,
            "maxsize": lru_info.maxsize,
            "currsize": lru_info.currsize,
        }
    except Exception as _e:
        pass
    return stats
