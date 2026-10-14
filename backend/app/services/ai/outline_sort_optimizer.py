"""V6.0 智能排序优化器（商业级）

对应设计文档：
  《标书智能体（二）——生成标书提纲代码+提示词（商业级 V6.0）》
  —— "三、智能排序优化器（V6.0 核心新增）" 章节

核心目标：
  - 在保持核心 28 章相对顺序绝对不变的前提下
  - 对补充章节按"工程逻辑分区"重新分布
  - 消除"智慧工地插在质量保证之后"等突兀叙事
  - 形成"宏观→技术→管理→资源→收尾"的专业叙事流

设计要点：
  1. 优先级权重：核心章 > 补充章
  2. 分区聚类：同分区章节连续出现（≥85% 紧凑度）
  3. AI 全局排序 + 关键词推断分区（降级方案）
  4. 排序后强制验证核心章顺序（最多 3 次重试）
  5. 完整降级：失败时按分区权重稳定排序

STATUS 2026-10-13（L6 · 死代码评估结论，**有意保留不删**）：
  - 本模块的**常量**（`CORE_28_CHAPTERS` / `CORE_ZONE_MAP` / `KEYWORD_ZONE_MAP` /
    `ZONE_ORDER` / `CORE_TITLE_SET`）是**生产依赖**：`outline_gate.reorder_by_zone`
    等工程逻辑重排直接导入它们（`from .outline_sort_optimizer import CORE_ZONE_MAP,
    KEYWORD_ZONE_MAP, ZONE_ORDER`），模块不可删除；
  - `OutlineSortOptimizer` / `OutlineSortOptimizerV2` **两个类目前无生产调用方**
    （V5.1 分步工作流不调用 AI 全局排序），但 `test_bugfix_2026_09_15_outline_module`
    等测试对它们的降级路径（`_enrich_zones` / `_composite_sort` / 同区稳定性）有
    成体系的回归覆盖，且这些路径修复过真实缺陷。删除类会连带销毁这些回归保护，
    收益（减维护面）小于风险，故**保留并在此标注为「已实现未接线」**。
  - 接线前置条件（将来若要启用）：AI 全局排序必须接入 V5.1 审核链，且候选标题
    须过 `outline_common._is_dirty_l3_title` 脏判定（2026-10-13 加固口径）。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from .providers.base import AIMessage
from .prompts import get_prompt


logger = logging.getLogger(__name__)


# ============================================================
# 核心 28 章固定骨架（与 V6.0 规范完全一致）
# ============================================================
CORE_28_CHAPTERS: list[dict[str, str]] = [
    {"id": "1", "title": "项目概述", "zone": "宏观总览"},
    {"id": "2", "title": "项目重难点分析", "zone": "宏观总览"},
    {"id": "3", "title": "工程概况", "zone": "宏观总览"},
    {"id": "4", "title": "编制依据", "zone": "前期准备"},
    {"id": "5", "title": "施工方案及技术措施", "zone": "核心技术"},
    {"id": "6", "title": "施工现场总平面布置", "zone": "核心技术"},
    {"id": "7", "title": "主要分部工程施工方法", "zone": "核心技术"},
    {"id": "8", "title": "装配式混凝土结构（PC）工程专项方案", "zone": "核心技术"},
    {"id": "9", "title": "质量保证措施和创优计划", "zone": "管理保障"},
    {"id": "10", "title": "施工安全措施计划", "zone": "管理保障"},
    {"id": "11", "title": "文明施工措施计划", "zone": "管理保障"},
    {"id": "12", "title": "施工环保措施计划", "zone": "管理保障"},
    {"id": "13", "title": "施工总进度计划及保证措施", "zone": "进度管控"},
    {"id": "14", "title": "资源配备计划", "zone": "资源保障"},
    {"id": "15", "title": "拟投入本工程的主要施工设备表", "zone": "资源保障"},
    {"id": "16", "title": "拟配备本工程的试验和检测仪器设备表", "zone": "资源保障"},
    {"id": "17", "title": "主要材料及劳动力计划表", "zone": "资源保障"},
    {"id": "18", "title": "特殊气候季节性施工方案", "zone": "专项预案"},
    {"id": "19", "title": "成品保护和工程保修工作的管理措施和承诺", "zone": "专项预案"},
    {"id": "20", "title": "危大工程及专项施工方案、紧急情况预案与风险控制措施", "zone": "专项预案"},
    {"id": "21", "title": "任何可能的紧急情况的处理措施、预案以及抵抗风险的措施", "zone": "专项预案"},
    {"id": "22", "title": "对专业分包工程的配合、协调、管理、服务方案", "zone": "协调配合"},
    {"id": "23", "title": "与发包人、项目管理单位、监理（包括投资监理）及设计人的配合", "zone": "协调配合"},
    {"id": "24", "title": "对总包管理及专业分包配合方案／与各参建方配合", "zone": "协调配合"},
    {"id": "25", "title": "工期优化", "zone": "收尾优化"},
    {"id": "26", "title": "工程资料及竣工验收管理", "zone": "收尾优化"},
    {"id": "27", "title": "附图、附件", "zone": "收尾优化"},
    {"id": "28", "title": "其他", "zone": "收尾优化"},
]

CORE_TITLE_SET: set[str] = {ch["title"] for ch in CORE_28_CHAPTERS}
CORE_ZONE_MAP: dict[str, str] = {ch["title"]: ch["zone"] for ch in CORE_28_CHAPTERS}


# ============================================================
# 工程逻辑分区（按叙事流排序）
# ============================================================
ZONE_ORDER: dict[str, int] = {
    "宏观总览": 1,
    "前期准备": 2,
    "核心技术": 3,
    "管理保障": 4,
    "进度管控": 5,
    "资源保障": 6,
    "专项预案": 7,
    "协调配合": 8,
    "收尾优化": 9,
    "未知分区": 5,  # 默认置于中间
}

# 关键词 → 分区映射（用于自动推断补充章节分区）
KEYWORD_ZONE_MAP: list[tuple[list[str], str]] = [
    (["概述", "重难点", "概况", "背景", "项目背景"], "宏观总览"),
    (["依据", "编制", "规范", "图集", "标准依据"], "前期准备"),
    (["施工方案", "技术措施", "技术路线", "工艺", "部署", "技术"], "核心技术"),
    (["平面布置", "现场布置", "总平面", "临设", "临建", "场地"], "核心技术"),
    (
        [
            "施工方法",
            "分部工程",
            "主体结构",
            "基础工程",
            "pc",
            "装配式",
            "pc构件",
            "混凝土",
            "钢结构",
            "砌体",
        ],
        "核心技术",
    ),
    (["质量", "创优", "qc", "保证措施", "质量管控"], "管理保障"),
    (["安全", "危大", "重大危险", "消防", "用电安全", "智慧工地", "ci"], "管理保障"),
    (["文明施工", "ci", "职业健康", "ci形象"], "管理保障"),
    (["环保", "扬尘", "噪声", "废", "生态", "绿色", "碳中和", "绿色建造"], "管理保障"),
    (["bim", "bim技术", "建筑信息模型"], "核心技术"),
    (["智慧", "数字化", "信息化", "智能"], "管理保障"),
    (["装配式", "pc构件", "预制"], "核心技术"),
    (["进度", "工期", "里程碑", "节点", "总进度", "进度计划"], "进度管控"),
    (["资源", "设备", "机械", "仪器", "检测", "机具"], "资源保障"),
    (["材料", "劳动力", "劳动力计划", "用工", "材料供应"], "资源保障"),
    (["特殊气候", "雨季", "台风", "冬季", "高温", "季节性", "恶劣天气"], "专项预案"),
    (["成品保护", "保修", "样板", "工程保修"], "专项预案"),
    (["风险", "应急", "预案", "紧急", "抵抗风险", "风险管理"], "专项预案"),
    (
        ["分包", "配合", "协调", "发包人", "监理", "总包", "设计人", "项目管理", "总承包"],
        "协调配合",
    ),
    (["工期优化", "竣工", "验收", "附图", "附件", "结算", "竣工验收"], "收尾优化"),
    (["新工艺", "新技术", "新材料", "四新"], "核心技术"),
    (["档案", "资料", "技术资料"], "收尾优化"),
    (["其他", "补充", "增项"], "收尾优化"),
]


# ============================================================
# 排序器提示词（System Prompt）
# ============================================================
SORT_SYSTEM_PROMPT = """### 角色
你是标书编制专家，擅长根据工程逻辑对目录进行全局排序优化。

### 核心铁律（违反即为严重错误）
1. **核心28章顺序绝对不变**：标题在【核心28章标题集合】中的章节，其相对顺序必须严格保持升序（按"宏观→技术→管理→进度→资源→预案→协调→收尾"叙事流）。你可以将补充章节插入它们之间的任何位置（包括最前面和最后面），但绝不能改变核心28章的先后次序。
2. **逻辑分区聚类**：请参考【工程逻辑分区】标签，尽量将同一分区的章节（如"核心技术"、"管理保障"）排列在一起，形成清晰的叙事段落。
3. **从宏观到微观**：总体顺序应遵循"宏观总览 → 前期准备 → 核心技术 → 管理保障 → 进度管控 → 资源保障 → 专项预案 → 协调配合 → 收尾优化"的递进逻辑。
4. **事实关联度**：与同一事实（如同一风险或工艺）相关的章节应尽量靠近，减少读者跳跃。

### 输入格式
你会收到一个包含"核心28章+补充章节"的列表。每个条目包含：
- `title`：章节标题
- `is_core`：是否为固定核心章（true/false）
- `zone`：工程逻辑分区（若补充章节无分区，已自动推断）

### 输出格式
返回一个重新排序后的 JSON 数组，元素仅包含 `title` 字段（按新顺序排列）。
核心章必须保持原始顺序。
补充章根据逻辑优化重新分配到合适的间隙位置。
**不要**改变、合并、删除任何标题，只调整顺序。

直接返回 JSON 数组，不包含任何解释。"""


# ============================================================
# 主类：OutlineSortOptimizer
# ============================================================
class OutlineSortOptimizer:
    """V6.0 智能排序优化器

    使用示例：
        optimizer = OutlineSortOptimizer(provider)
        sorted_outline = optimizer.optimize(
            merged_outline=outline_list,
            facts={"project_name": "..."},
            core_titles=CORE_TITLE_SET,
        )
    """

    def __init__(self, provider: Any, max_retries: int = 3):
        """Args:
        provider: AI 提供商实例（需有 chat/messages 接口）
        max_retries: AI 排序调用最大重试次数
        """
        self.provider = provider
        self.max_retries = max_retries

    # ================ 对外主方法 ================
    def optimize(
        self,
        merged_outline: list[dict],
        facts: dict | None = None,
        core_titles: set[str] | None = None,
    ) -> list[dict]:
        """对合并后的目录进行智能排序

        Args:
            merged_outline: 合并后的目录（每项含 id, title 等字段）
            facts: 全局事实变量（供 AI 参考排序）
            core_titles: 核心 28 章标题集合
        Returns:
            排序后的目录（保持原有字段，仅调整顺序）
        """
        core_titles = core_titles or CORE_TITLE_SET
        facts = facts or {}

        logger.info("[V6.0] 开始智能排序优化（共 %d 章）", len(merged_outline))

        # 1. 为每个章节标注分区
        enriched = self._enrich_zones(merged_outline, core_titles)

        # 2. 尝试 AI 排序（带重试）
        sorted_titles = self._try_ai_sort(enriched, facts)
        if sorted_titles:
            reordered = self._reorder_by_titles(merged_outline, sorted_titles)
            # 验证核心章顺序
            if self._validate_core_order(reordered, core_titles):
                logger.info("[V6.0] AI 排序成功，应用新顺序")
                return reordered
            logger.warning("[V6.0] AI 排序破坏了核心章顺序，降级为分区权重排序")

        # 3. 降级：按分区权重稳定排序
        return self._fallback_sort_by_zone(enriched, core_titles)

    # ================ 分区标注 ================
    def _enrich_zones(
        self,
        outline: list[dict],
        core_titles: set[str],
    ) -> list[dict]:
        """为每个章节补充分区标签"""
        enriched = []
        for idx, ch in enumerate(outline):
            title = ch.get("title", "").strip()
            is_core = title in core_titles
            if is_core:
                zone = CORE_ZONE_MAP.get(title, "未知分区")
            else:
                zone = ch.get("zone") or self._infer_zone_by_keywords(title)
            enriched.append(
                {
                    "id": ch.get("id", ""),
                    "title": title,
                    "is_core": is_core,
                    "zone": zone,
                    # 原始下标：供降级排序做**稳定** tie-breaker（见 _fallback_sort_by_zone）
                    "index": idx,
                    "original": ch,
                }
            )
        return enriched

    @staticmethod
    def _infer_zone_by_keywords(title: str) -> str:
        """基于关键词推断补充章节的分区（优化版本）"""
        if not title:
            return "未知分区"
        title_lower = title.lower()

        # 优化：先检查长关键词，避免短关键词误匹配
        for keywords, zone in KEYWORD_ZONE_MAP:
            # 按关键词长度降序排序，优先匹配长关键词
            sorted_keywords = sorted(keywords, key=len, reverse=True)
            for kw in sorted_keywords:
                if kw in title_lower:
                    return zone
        return "未知分区"

    # ================ AI 排序 ================
    def _try_ai_sort(
        self,
        enriched: list[dict],
        facts: dict,
    ) -> list[str] | None:
        """调用 AI 进行全局排序"""
        # 构建简洁的输入
        sort_input_items = [
            {"title": it["title"], "is_core": it["is_core"], "zone": it["zone"]} for it in enriched
        ]
        user_prompt = (
            f"【核心28章标题集合】\n{json.dumps(sorted(CORE_TITLE_SET), ensure_ascii=False, indent=2)}\n\n"
            f"【工程逻辑分区（按叙事流排序）】\n"
            f"宏观总览 < 前期准备 < 核心技术 < 管理保障 < 进度管控 < 资源保障 < 专项预案 < 协调配合 < 收尾优化\n\n"
            f"【全局事实（辅助排序判断）】\n{json.dumps(facts, ensure_ascii=False, indent=2)[:2000]}\n\n"
            f"【当前目录列表】\n{json.dumps(sort_input_items, ensure_ascii=False, indent=2)}\n\n"
            f"请返回按新顺序排列的 JSON 数组（仅含 title 字段），核心28章必须保持原始相对顺序。"
        )

        # BUG-FIX-PROMPT-E7（目录排序提示词未注册）：原直接引用模块级
        # SORT_SYSTEM_PROMPT 硬编码，管理页无法查看/编辑/覆盖。现统一走
        # get_prompt()（DB 优先 → 注册表 → 硬编码回退）。
        _sort_sys = get_prompt("OUTLINE_SORT_SYSTEM") or SORT_SYSTEM_PROMPT
        messages = [
            AIMessage(role="system", content=_sort_sys),
            AIMessage(role="user", content=user_prompt),
        ]

        for attempt in range(1, self.max_retries + 1):
            try:
                # 兼容 sync/async provider
                response = self._call_provider(messages)
                if not response:
                    continue
                parsed = self._parse_titles(response)
                if parsed:
                    return parsed
                logger.warning("[V6.0] AI 排序解析失败（尝试 %d/%d）", attempt, self.max_retries)
            except Exception as e:
                logger.warning("[V6.0] AI 排序失败（尝试 %d/%d）：%s", attempt, self.max_retries, e)
        return None

    def _call_provider(self, messages: list[AIMessage]) -> str | None:
        """调用 provider（兼容 sync/async）"""
        import inspect

        chat = getattr(self.provider, "chat", None)
        if chat is None:
            logger.info("[_call_provider] provider.chat 为空，跳过排序")
            return None

        # Python 3.16 废弃 asyncio.iscoroutinefunction，改用 inspect
        if not inspect.iscoroutinefunction(chat):
            # 同步调用；但部分 provider 的同步方法内部可能返回协程对象
            # （如 def chat(...): return async_chat(...) 的转发写法），
            # 此时 iscoroutinefunction 误判为同步，返回协程对象导致下游
            # _parse_titles 崩溃（历史日志：'coroutine' object has no attribute 'startswith'）。
            # 防御：对返回值做 isawaitable 检查，协程则走 _safe_asyncio_run 执行。
            logger.info("[_call_provider] 同步调用 provider.chat（%d 条消息）", len(messages))
            result = chat(messages)
            if inspect.isawaitable(result):
                logger.info("[_call_provider] chat 同步方法返回协程，改用 _safe_asyncio_run 执行")
                from .workflows import _safe_asyncio_run

                try:
                    result = _safe_asyncio_run(result)
                except Exception as e:
                    logger.warning("[_call_provider] 协程执行异常：%s", e, exc_info=True)
                    return None
            return result

        # 异步调用：使用 _safe_asyncio_run 正确处理 running loop 场景
        # Python 3.10+ asyncio.get_event_loop() 会创建新 loop 而非返回 running loop，
        # 旧代码用 get_event_loop() + is_running() 判断存在逻辑缺陷，已统一改用 _safe_asyncio_run
        from .workflows import _safe_asyncio_run

        logger.info(
            "[_call_provider] 异步调用 provider.chat，使用 _safe_asyncio_run（%d 条消息）",
            len(messages),
        )
        try:
            result = _safe_asyncio_run(chat(messages))
            logger.info("[_call_provider] 调用成功，响应长度=%d", len(result) if result else 0)
            return result
        except Exception as e:
            logger.warning("[_call_provider] provider 调用异常：%s", e, exc_info=True)
            return None

    @staticmethod
    def _parse_titles(response: str) -> list[str] | None:
        """从 AI 响应中解析标题列表"""
        if not response:
            return None
        text = response.strip()

        # 移除可能的 markdown 围栏
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text)
            text = re.sub(r"\s*```$", "", text)
        text = text.strip()

        # 尝试解析为 JSON 数组
        try:
            data = json.loads(text)
            if isinstance(data, list):
                titles: list[str] = []
                for x in data:
                    if isinstance(x, dict):
                        # BUG 修复：AI 可能返回 [{"title": "..."}, ...] 结构，
                        # 原实现把整个 dict str() 成 "{'title': ...}" 导致排序匹配失败
                        t = x.get("title") or x.get("name") or ""
                        if t:
                            titles.append(str(t).strip())
                    elif x:
                        titles.append(str(x).strip())
                return titles if titles else None
            # 兼容 {"titles": [...]} 形式
            if isinstance(data, dict):
                for key in ("titles", "result", "data", "items"):
                    if key in data and isinstance(data[key], list):
                        titles = []
                        for x in data[key]:
                            if isinstance(x, dict):
                                t = x.get("title") or x.get("name") or ""
                                if t:
                                    titles.append(str(t).strip())
                            elif x:
                                titles.append(str(x).strip())
                        return titles if titles else None
        except json.JSONDecodeError:
            pass

        # 兜底：提取所有 "第X章..." 或 "X、..." 行
        titles: list[str] = []
        for line in text.split("\n"):
            line = line.strip().lstrip("- ").lstrip("0123456789.、").strip()
            if line and len(line) >= 2 and len(line) <= 60:
                # 移除引号
                line = line.strip("\"'" "''")
                if line:
                    titles.append(line)
        return titles if titles else None

    # ================ 验证 ================
    @staticmethod
    def _validate_core_order(
        reordered: list[dict],
        core_titles: set[str],
    ) -> bool:
        """验证核心章相对顺序是否保持"""
        # 提取重排结果中的核心章
        sorted_core = [
            ch.get("title", "") for ch in reordered if ch.get("title", "") in core_titles
        ]
        # 按 CORE_28_CHAPTERS 的原始顺序
        core_order_map = {ch["title"]: idx for idx, ch in enumerate(CORE_28_CHAPTERS)}

        last_idx = -1
        for title in sorted_core:
            current_idx = core_order_map.get(title, -1)
            if current_idx == -1:
                continue
            if current_idx < last_idx:
                logger.debug("[V6.0] 核心章顺序违规：%s 后出现更早的核心章", title)
                return False
            last_idx = current_idx
        return True

    # ================ 重排 ================
    @staticmethod
    def _reorder_by_titles(
        original: list[dict],
        sorted_titles: list[str],
    ) -> list[dict]:
        """根据 AI 返回的标题顺序重组目录（保留原始字段）"""
        title_to_obj = {ch.get("title", ""): ch for ch in original}
        reordered = []
        seen: set[str] = set()
        for title in sorted_titles:
            if title in title_to_obj and title not in seen:
                reordered.append(title_to_obj[title])
                seen.add(title)
        # 补全可能遗漏的章节
        for ch in original:
            if ch.get("title", "") not in seen:
                reordered.append(ch)
        return reordered

    # ================ 降级方案 ================
    @staticmethod
    def _fallback_sort_by_zone(
        enriched: list[dict],
        core_titles: set[str],
    ) -> list[dict]:
        """按分区权重稳定排序（保持核心章相对顺序）"""
        core_order_map = {ch["title"]: idx for idx, ch in enumerate(CORE_28_CHAPTERS)}

        def sort_key(item: dict) -> tuple:
            title = item["title"]
            if item["is_core"]:
                # 核心章：分区权重优先，同分区按原始顺序
                zone_w = ZONE_ORDER.get(item["zone"], 5)
                core_idx = core_order_map.get(title, 9999)
                return (0, zone_w, core_idx)
            else:
                # 补充章：分区权重优先，同分区**保持原始相对顺序**。
                # BUG-FIX 2026-09-15（中）：原 tie-breaker 是 title，导致同分区章节
                # 按标题字典序乱排。V5.1 的 L1 输出是「评分项专业化改写」标题，
                # 实测 8/8 都命中不了 CORE_TITLE_SET（`is_core` 全为 False），于是
                # 全部落到本分支 —— AI 按「总纲→组织→方案→管理→收尾」排好的顺序
                # 会被字典序彻底打乱（实测「施工组织部署」被排到「主要分部分项」之后）。
                # 改用原始下标，使本降级路径成为**稳定**排序。
                zone_w = ZONE_ORDER.get(item["zone"], 5)
                return (1, zone_w, item.get("index", 0))

        sorted_items = sorted(enriched, key=sort_key)
        return [item["original"] for item in sorted_items]


# ============================================================
# 自测
# ============================================================


# ============================================================
# V6.0 增强：OutlineSortOptimizerV2（工程逻辑流 + 评分权重）
# ============================================================
class OutlineSortOptimizerV2:
    """V6.0 增强版排序优化器 - 结合工程逻辑流 + 评分权重

    与 OutlineSortOptimizer（V1）的区别：
      - V1：仅基于分区权重排序，AI 调用为可选项
      - V2：分区权重 + 评分权重复合排序，评分权重高的章节适当前移
      - 自动从招标文件评分标准中提取权重
    """

    # 标准工程逻辑分区顺序（权重值）
    ZONE_ORDER = {
        "宏观总览": 1,
        "前期准备": 2,
        "核心技术": 3,
        "管理保障": 4,
        "进度管控": 5,
        "资源保障": 6,
        "专项预案": 7,
        "协调配合": 8,
        "收尾优化": 9,
    }

    def __init__(self, provider: Any | None = None, max_retries: int = 1):
        # BUG O3 修复：默认 max_retries 从 3 降到 1
        # 排序优化是非核心路径，重试 1 次已足够，最坏情况由 SSE_TOTAL_TIMEOUT 兜底
        """Args:
        provider: AI 提供商实例（可选，用于 AI 排序）
        max_retries: AI 排序调用最大重试次数
        """
        self.provider = provider
        self.max_retries = max_retries
        self._v1_optimizer = OutlineSortOptimizer(provider, max_retries) if provider else None

    def optimize(
        self,
        merged_outline: list[dict],
        facts: dict | None = None,
        score_weights: dict[str, float] | None = None,
        core_titles: set[str] | None = None,
    ) -> list[dict]:
        """智能排序：结合分区权重 + 评分点权重

        Args:
            merged_outline: 合并后的目录（每项含 title 等字段）
            facts: 全局事实变量
            score_weights: 章节标题 -> 评分权重（从招标文件评分标准中提取）
            core_titles: 核心28章标题集合

        Returns:
            排序后的目录
        """
        core_titles = core_titles or CORE_TITLE_SET
        facts = facts or {}
        score_weights = score_weights or {}

        logger.info(
            "[V6.0 Enhanced] 开始智能排序优化（共 %d 章，评分权重 %d 项）",
            len(merged_outline),
            len(score_weights),
        )

        # 1. 为每个章节标注分区
        enriched = (
            self._v1_optimizer._enrich_zones(merged_outline, core_titles)
            if self._v1_optimizer
            else self._enrich_zones(merged_outline, core_titles)
        )

        # 2. 尝试 AI 排序（如果 provider 可用）
        if self._v1_optimizer:
            sorted_titles = self._v1_optimizer._try_ai_sort(enriched, facts)
            if sorted_titles:
                reordered = self._v1_optimizer._reorder_by_titles(merged_outline, sorted_titles)
                if self._v1_optimizer._validate_core_order(reordered, core_titles):
                    logger.info("[V6.0 Enhanced] AI 排序成功，应用新顺序")
                    return reordered

        # 3. 降级：按复合分数排序（分区权重 + 评分权重）
        return self._composite_sort(enriched, core_titles, score_weights)

    def _enrich_zones(self, outline: list[dict], core_titles: set[str]) -> list[dict]:
        """为每个章节补充分区标签（独立实现，不依赖 v1）"""
        enriched = []
        for idx, ch in enumerate(outline):
            title = ch.get("title", "").strip()
            is_core = title in core_titles
            if is_core:
                zone = CORE_ZONE_MAP.get(title, "未知分区")
            else:
                zone = ch.get("zone") or self._infer_zone_by_keywords(title)
            enriched.append(
                {
                    "id": ch.get("id", ""),
                    "title": title,
                    "is_core": is_core,
                    "zone": zone,
                    # 原始下标：供复合排序做**稳定** tie-breaker
                    "index": idx,
                    "original": ch,
                }
            )
        return enriched

    @staticmethod
    def _infer_zone_by_keywords(title: str) -> str:
        """基于关键词推断补充章节的分区"""
        return OutlineSortOptimizer._infer_zone_by_keywords(title)

    def _composite_sort(
        self,
        enriched: list[dict],
        core_titles: set[str],
        score_weights: dict[str, float],
    ) -> list[dict]:
        """按复合分数排序（分区权重 + 评分权重）

        核心逻辑：
        - 核心章：分区权重优先，同分区按原始顺序
        - 补充章：分区权重优先，同分区内评分权重高的适当前移
        """
        core_order_map = {ch["title"]: idx for idx, ch in enumerate(CORE_28_CHAPTERS)}

        def sort_key(item: dict) -> tuple:
            title = item["title"]
            if item["is_core"]:
                zone_w = self.ZONE_ORDER.get(item["zone"], 5)
                core_idx = core_order_map.get(title, 9999)
                return (0, zone_w, core_idx)
            else:
                zone_w = self.ZONE_ORDER.get(item["zone"], 5)
                # 评分权重高的章节适当前移（同分区内）
                weight_score = score_weights.get(title, 1.0)
                # 复合分数：分区权重 * 10 - 评分权重 * 0.5
                # 权重高的章节（score 大）→ 复合分数更小 → 排在前面
                composite = zone_w * 10 - weight_score * 0.5
                # tie-breaker 用原始下标而非标题：评分权重相同时（`extract_score_weights`
                # 对未提及章节统一返回默认值）保持原顺序，避免按字典序乱排。
                # 同 V1 `_fallback_sort_by_zone` 的 BUG-FIX 2026-09-15。
                return (1, composite, item.get("index", 0))

        sorted_items = sorted(enriched, key=sort_key)
        return [item["original"] for item in sorted_items]


def extract_score_weights(requirements: str) -> dict[str, float]:
    """从技术评分要求中提取各章节的评分权重

    示例输入: "施工方案（30分）、质量保证措施（20分）..."
    输出: {"施工方案": 30.0, "质量保证措施": 20.0, ...}

    支持的格式：
      - "施工方案（30分）"
      - "施工方案(30分)"
      - "施工方案 30分"
      - "施工方案：30分"
    """
    weights: dict[str, float] = {}
    if not requirements:
        return weights

    # 模式1: "标题（N分）" 或 "标题(N分)"
    pattern1 = r"([^（(]+)[（(](\d+(?:\.\d+)?)\s*分[）)]"
    for match in re.findall(pattern1, requirements):
        title = match[0].strip()
        if title:
            try:
                weights[title] = float(match[1])
            except ValueError:
                pass

    # 模式2: "标题 N分" 或 "标题：N分"
    pattern2 = r"([^\d\n]+?)[：:]\s*(\d+(?:\.\d+)?)\s*分"
    for match in re.findall(pattern2, requirements):
        title = match[0].strip()
        if title and title not in weights:
            try:
                weights[title] = float(match[1])
            except ValueError:
                pass

    # 模式3: "标题  N分"（无冒号）
    pattern3 = r"([^\d\n]{2,20})\s+(\d+(?:\.\d+)?)\s*分"
    for match in re.findall(pattern3, requirements):
        title = match[0].strip()
        if title and title not in weights:
            try:
                weights[title] = float(match[1])
            except ValueError:
                pass

    return weights


# ============================================================
# 自测（V2）
# ============================================================
