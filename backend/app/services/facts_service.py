"""全局事实变量（Global Facts）统一服务层 —— 命名空间与字段契约的唯一正本

ENHANCE 2026-09-29（facts-ns 统一命名空间与去重重构）：

在重构前，「事实」的字段契约与规范化规则散落在 5 处、各自漂移：

| 位置 | 职责 | 漂移点 |
| --- | --- | --- |
| `routers/facts.py::_normalize_facts` | 手动提取端点 | 三档策略 + needs_manual_input |
| `services/ai/workflows_analysis.py::_normalize_fact_items` | 评分流程自动提取 | **只认 fabricate 一档**，兜底文案硬编码 |
| `services/ai/workflows_analysis.py::_dict_to_fact_items` | AI dict → items | label 直接用英文 key，分类只看 key |
| `routers/sse/sse_utils.py::_parse_global_facts_items` | 正文生成读取 | 占位过滤只在这里做 |
| `tasks/__init__.py` / `consistency_repair.py` | 后台生成 / 修复 | 直接 `json.loads`，**不过滤占位** |

由此产生的真实缺陷：
1. **分类口径不一致**：同一事实两条链路 category 不同（facts.py 副本曾把「技术」
   判成 personnel），而 category 决定正文注入分组与审计口径；
2. **占位符泄漏**：placeholder 策略下「【待填写】」被 Celery 路径原样注入正文；
3. **字段丢失**：自动链路不产 `needs_manual_input`，前端「待人工填写」角标时隐时现；
4. **策略漂移**：用户选了 omit，评分流程仍按 fabricate 编一个工期出来。

本模块把这些规则收敛为一份实现。旧位置保留 **thin re-export**（带来源注释），
既有 `from app.routers.facts import _normalize_facts` 之类的 import 路径全部向后兼容。

命名说明：`app/services/global_facts.py` 曾因「全库无 import」被物理删除
（见 tests/test_bugfix_2026_09_14.py::test_global_facts_dead_module_removed），
故正本改名 `facts_service.py`，且本模块被 facts / workflows_analysis / sse / tasks /
consistency_repair 真实 import，不会再退化为死模块。
"""

import json
import logging
import os
import re
from typing import Any


logger = logging.getLogger(__name__)


# ============================================================
# 一、缺失值策略（三档）
# ============================================================

#: 全局事实缺失值策略：
#:   fabricate   —— 资料没给值时可模拟生成合理值（历史默认行为，向后兼容）
#:   omit        —— 不编造，改为不含具体值的笼统承诺
#:   placeholder —— 不编造，留「【待填写】」占位交人工补齐
FACTS_POLICIES: tuple[str, ...] = ("fabricate", "omit", "placeholder")
DEFAULT_FACTS_POLICY = "fabricate"

#: 三档策略对应的「值规范化规则第 3 条」原文，插入提取提示词
POLICY_RULE_TEXTS: dict[str, str] = {
    "fabricate": (
        "3. **无值可补**：资料未给出具体值但对全文一致性重要且允许时，可模拟生成合理、稳定、"
        "不冲突的值（如人名、工期天数），并保持全文统一。"
    ),
    "omit": (
        "3. **缺值不编造**：资料未给出具体值时，**严禁**编造具体数字、人名、日期或金额，"
        "改为不含具体值的、且能满足招标要求的笼统承诺。\n"
        '   - ❌ "项目经理：张三" → [OK] "项目经理：由具备一级建造师执业资格的人员担任，开工前报建设单位备案"\n'
        '   - ❌ "建设工期：420 日历天" → [OK] "建设工期：按招标文件要求及合同约定执行\n'
        "   宁可笼统，也不得虚构；全文口径必须一致。"
    ),
    "placeholder": (
        "3. **缺值留占位**：资料未给出具体值时，**严禁**编造，value 统一写成 "
        "`【待填写】`，必要时写成 `【待填写：<这里该填什么>】`，交由人工补齐。\n"
        '   - ❌ "项目经理：张三" → [OK] "项目经理：【待填写：项目经理姓名】"\n'
        '   - ❌ "建设工期：420 日历天" → [OK] "建设工期：【待填写】"'
    ),
}

#: 各策略下追加在「必须有工期」之后的补充约束
POLICY_TAIL_NOTES: dict[str, str] = {
    "fabricate": "",
    "omit": (
        "\n6. **一致性优先于具体性**：同一事实在全文中只能有一种笼统表述，"
        "不得在不同章节给出不同说法。"
    ),
    "placeholder": (
        "\n6. **占位要集中**：只在确实必要的事实上留【待填写】，"
        "不要把每个 key 都写成占位；无法判断时宁可不输出该 key。"
    ),
}

#: 缺值兜底工期文案（AI/启发式都没拿到工期时用）
SCHEDULE_FALLBACK_VALUES: dict[str, str] = {
    "fabricate": "满足招标文件工期要求，按合同约定执行",
    "omit": "按招标文件要求及合同约定执行",
    "placeholder": "【待填写】",
}


def normalize_facts_policy(value: Any) -> str:
    """把外部传入的策略值归一化到三档之一；非法值回退默认（不抛错）。"""
    policy = str(value or "").strip().lower()
    return policy if policy in FACTS_POLICIES else DEFAULT_FACTS_POLICY


def policy_rule_text(policy: str) -> str:
    return POLICY_RULE_TEXTS.get(
        normalize_facts_policy(policy), POLICY_RULE_TEXTS[DEFAULT_FACTS_POLICY]
    )


def policy_tail_note(policy: str) -> str:
    return POLICY_TAIL_NOTES.get(
        normalize_facts_policy(policy), POLICY_TAIL_NOTES[DEFAULT_FACTS_POLICY]
    )


def schedule_fallback_value(policy: str) -> str:
    return SCHEDULE_FALLBACK_VALUES.get(
        normalize_facts_policy(policy), SCHEDULE_FALLBACK_VALUES[DEFAULT_FACTS_POLICY]
    )


# ============================================================
# 二、分类命名空间（前端展示 + 审计分组共用）
# ============================================================

#: 事实分类与中文标签。ENHANCE 2026-09-29：此前该表在 facts.py 里定义却从未被
#: 使用（死代码），中文标签只在前端 `SummaryReviewPage.tsx` 的 catMap 里另写一份，
#: 导致后端新增 implementation 分类时前端漏映射、直接显示英文。
#: 现在它是唯一正本：`GET /facts` 通过 `categories` 字段下发给前端渲染。
FACT_CATEGORIES: tuple[tuple[str, str], ...] = (
    ("basic", "基本信息"),
    ("project", "项目概况"),
    ("bidder", "投标人信息"),
    ("commitment", "承诺事项"),
    ("schedule", "工期计划"),
    ("quality", "质量标准"),
    ("safety", "安全目标"),
    ("personnel", "人员配置"),
    ("technical", "技术参数"),
    ("implementation", "实施做法"),
)
FACT_CATEGORY_LABELS: dict[str, str] = dict(FACT_CATEGORIES)

#: 分类别名：AI 偶尔把 category 写成中文/同义词，归一化后再入库，
#: 避免同一分类出现 "quality"/"质量标准" 两种取值把前端分组打散。
_CATEGORY_ALIASES: dict[str, str] = {
    "基础": "basic",
    "base": "basic",
    "基本信息": "basic",
    "项目": "project",
    "项目概况": "project",
    "投标人": "bidder",
    "承诺": "commitment",
    "服务承诺": "commitment",
    "工期": "schedule",
    "进度": "schedule",
    "时间": "schedule",
    "质量": "quality",
    "质量标准": "quality",
    "安全": "safety",
    "安全目标": "safety",
    "人员": "personnel",
    "团队": "personnel",
    "人员配置": "personnel",
    "技术": "technical",
    "技术参数": "technical",
    "技术路线": "technical",
    "实施": "implementation",
    "实施做法": "implementation",
    "施工": "implementation",
}


def normalize_category(raw: Any) -> str:
    """把 category 取值归一化到 `FACT_CATEGORIES` 之内；未知值回退 basic。"""
    text = str(raw or "").strip().lower()
    if not text:
        return "basic"
    if text in FACT_CATEGORY_LABELS:
        return text
    if text in _CATEGORY_ALIASES:
        return _CATEGORY_ALIASES[text]
    for zh, en in _CATEGORY_ALIASES.items():
        if zh in text:
            return en
    return "basic"


def category_label(raw: Any) -> str:
    """分类的中文标签（前端直接展示，不再自己维护映射表）。"""
    return FACT_CATEGORY_LABELS.get(normalize_category(raw), str(raw or ""))


# ============================================================
# 三、占位（placeholder）契约
# ============================================================

#: 占位值前缀。事实占位串有两种形态——无说明的【待填写】与带字段名的
#: 【待填写：xxx】，统一用前缀匹配，避免带说明的占位项绕过过滤进入正文 prompt。
PLACEHOLDER_VALUE_PREFIX = "【待填写"


def is_placeholder_value(value: Any) -> bool:
    """判断事实 value 是否为「待人工填写」占位串。"""
    return PLACEHOLDER_VALUE_PREFIX in str(value or "")


def mark_placeholders(items: list) -> int:
    """给占位事实逐条打 `needs_manual_input` 标；返回打标条数。

    placeholder 策略下前端据此渲染「待人工填写」角标，避免用户把【待填写】
    直接确认并写进全篇标书。
    """
    count = 0
    for item in items or []:
        if isinstance(item, dict) and is_placeholder_value(item.get("value")):
            item["needs_manual_input"] = True
            count += 1
    return count


def count_placeholders(items: list) -> int:
    """统计仍留占位的事实条数（GET /facts 与 /facts/generate 共用口径）。"""
    return sum(
        1 for item in (items or []) if isinstance(item, dict) and is_placeholder_value(item.get("value"))
    )


# ============================================================
# 四、存储读写口径
# ============================================================


def parse_stored_items(raw: Any) -> list[dict]:
    """把 `analysis.global_facts_json` 解析为扁平事实列表；任何异常返回 []。

    BUG-FIX 2026-09-29（契约脆弱）：正本写入格式是**扁平 list**
    （`json.dumps([f.model_dump() ...])`），但历史上出现过 {"items": [...]} 包装
    形态（Step02 自动链路与手工导入都可能产生）。原实现只认裸 list，遇到包装
    整体丢弃 → 正文生成就「静默不带事实」，日志里也看不出来。
    现同时接受 list 与带 list 型 items 键的 dict，其余形态降级为空并留痕。
    """
    if not raw:
        return []
    parsed: Any
    if isinstance(raw, (list, dict)):
        parsed = raw
    elif isinstance(raw, (str, bytes)):
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, TypeError, UnicodeDecodeError):
            # BUG-FIX 2026-09-29：原先只捕 JSONDecodeError，坏数据（None/字节流/
            # 超长非法串）抛出的其它异常会直接 500 掉正文生成接口。
            logger.warning("global_facts_json 解析失败，正文将不带全局事实生成: 前50字=%r", str(raw)[:50])
            return []
    else:
        logger.warning("global_facts_json 类型不支持（类型=%s），已忽略", type(raw).__name__)
        return []

    if isinstance(parsed, dict):
        # 分组/包装形态：只认 items 键，避免把无关字典当成事实
        inner = parsed.get("items")
        if isinstance(inner, list):
            parsed = inner
        else:
            logger.warning(
                "global_facts_json 为字典但缺少 list 型 items 键（keys=%s），已忽略",
                list(parsed.keys())[:5],
            )
            return []

    if not isinstance(parsed, list):
        logger.warning("global_facts_json 结构非列表（类型=%s），已忽略", type(parsed).__name__)
        return []

    return [item for item in parsed if isinstance(item, dict)]


def facts_for_generation(raw: Any) -> list[dict]:
    """正文生成/修复链路读取事实：解析 + **剔除占位项**。

    这是 SSE 与 Celery 两条生成路径共同的事实入口。此前 Celery 侧直接
    `json.loads` 不过滤占位，placeholder 策略的【待填写】会被原样注入正文。
    """
    return [item for item in parse_stored_items(raw) if not is_placeholder_value(item.get("value"))]


def dump_items(items: list) -> str:
    """事实列表 → 入库 JSON（统一 ensure_ascii=False，保留中文可读性）。"""
    return json.dumps(items or [], ensure_ascii=False)


# ============================================================
# 五、时间类事实（「必须有工期」规则）
# ============================================================

SCHEDULE_CATEGORIES: frozenset[str] = frozenset({"schedule"})
SCHEDULE_KEY_TAGS: tuple[str, ...] = (
    "duration",
    "schedule",
    "工期",
    "运维",
    "保修",
    "质保",
    "交货",
    "delivery",
    "warranty",
    "service_period",
    "warranty_period",
)


def has_schedule_fact(items: list) -> bool:
    """是否已存在时间类变量（工期/运维期/交货时间）。"""
    return any(
        (isinstance(f, dict))
        and (
            f.get("category") in SCHEDULE_CATEGORIES
            or any(tag in (f.get("key") or "").lower() + (f.get("label") or "") for tag in SCHEDULE_KEY_TAGS)
        )
        for f in (items or [])
    )


def extract_schedule_value(text: str) -> str:
    """从文本中启发式提取工期/运维期/交货时间值。"""
    if not text:
        return ""
    # 工期：如 "工期 450 日历天"、"建设工期：450天"
    m = re.search(r"(?:工期|建设工期|总工期|计划工期)[^，。；\n]{0,10}?([\d.]+)\s*(日历)?天", text)
    if m:
        return f"{m.group(1)} 日历天"
    # 运维期/保修期：如 "运维期 2 年"、"保修期 2 年"
    m = re.search(r"(?:运维期|保修期|质保期|服务期)[^，。；\n]{0,8}?([\d.]+)\s*年", text)
    if m:
        return f"{m.group(1)} 年"
    return ""


# ============================================================
# 六、去重与值规范化
# ============================================================


def dedup_key(fact: dict) -> str:
    """去重键：优先 key，其次 label；两者都为空返回 ""（该条应被丢弃）。"""
    if not isinstance(fact, dict):
        return ""
    return (str(fact.get("key") or "")).strip() or (str(fact.get("label") or "")).strip()


def dedup_facts(items: list) -> list:
    """按 key（其次 label）去重，保留首个出现的条目。

    ENHANCE 2026-09-29：同一 key 重复出现时，除了丢弃后者，还把「已有条目为空值/
    占位、后来那条有真实值」的情况做一次补强 —— 原实现纯「保留首个」，
    于是【待填写】排在前面时会永久压住后面的真实值（AI 输出顺序不稳定，
    这条会随机丢失事实）。
    """
    seen: dict[str, int] = {}
    dedup: list = []
    for f in items or []:
        if not isinstance(f, dict):
            continue
        fk = dedup_key(f)
        if not fk:
            continue
        if fk not in seen:
            seen[fk] = len(dedup)
            dedup.append(f)
            continue
        first = dedup[seen[fk]]
        old_empty = is_placeholder_value(first.get("value")) or not str(first.get("value") or "").strip()
        new_ok = not is_placeholder_value(f.get("value")) and bool(str(f.get("value") or "").strip())
        if old_empty and new_ok:
            # 用真实值替换占位/空值，但保留原条目的 key/label 命名
            merged = {**first, **f}
            merged["key"] = first.get("key") or f.get("key")
            merged["label"] = first.get("label") or f.get("label")
            dedup[seen[fk]] = merged
    return dedup


# ============================================================
# 七、AI dict → 事实条目（分类推断 + 中文标签）
# ============================================================

#: 常见英文 key 的中文标签。ENHANCE 2026-09-29（分类与展示缺口）：AI 按提示词
#: 输出驼峰 key，原实现把 key 原样当 label（`"label": k`），前端「标签」列于是
#: 显示 `scheduleDuration` 这类英文串，用户无法核对事实含义。
KEY_LABEL_ZH: dict[str, str] = {
    "projectName": "项目名称",
    "projectLocation": "项目地点",
    "constructionLocation": "建设地点",
    "projectType": "工程类型",
    "projectScale": "项目规模",
    "constructionUnit": "建设单位",
    "bidSection": "标段",
    "scheduleDuration": "建设工期",
    "totalDuration": "总工期",
    "startDate": "开工日期",
    "endDate": "竣工日期",
    "completeDate": "竣工日期",
    "warrantyPeriod": "质保期",
    "servicePeriod": "运维服务期",
    "deliveryTime": "交货时间",
    "projectManager": "项目经理",
    "techManager": "技术负责人",
    "technicalManager": "技术负责人",
    "safetyManager": "安全负责人",
    "qualityManager": "质量负责人",
    "techRoute": "技术路线",
    "technicalRoute": "技术路线",
    "qualityRequirement": "质量要求",
    "qualityStandard": "质量标准",
    "qualityTarget": "质量目标",
    "safetyTarget": "安全目标",
    "safetyGoal": "安全目标",
    "commitment": "服务承诺",
    "serviceResponseTime": "故障响应时间",
    "trainingCommitment": "培训承诺",
    "methodStatement": "实施做法",
    "buildingArea": "建筑面积",
    "workScope": "承包范围",
}


def infer_category(key: str, label: str = "") -> str:
    """按 key + label 推断事实分类。

    BUG-FIX 2026-09-29（分类只看 key）：原实现只在 key 上找标签，而中文 key
    命中率高、英文 key 常与中文 label 成对出现（{"scheduleDuration": ...} 配
    "建设工期"），只看 key 会让部分事实落到 basic。判定顺序保持与历史正本一致，
    仅把匹配文本扩展为 key + label。
    """
    hay = f"{(key or '').lower()} {label or ''}"
    if any(tag in hay for tag in ("duration", "schedule", "工期", "运维", "保修", "质保", "交货")):
        return "schedule"
    if any(tag in hay for tag in ("person", "manager", "负责人", "人员", "经理")):
        return "personnel"
    if any(tag in hay for tag in ("tech", "技术", "设备", "材料", "工艺", "参数", "规格")):
        return "technical"
    if any(tag in hay for tag in ("commit", "承诺", "service", "响应", "售后")):
        return "commitment"
    if any(tag in hay for tag in ("implement", "施工", "方法", "组织", "实施")):
        return "implementation"
    # safety 必须在 quality 之前：否则"安全"被并进 quality，safety 分类永为空
    if any(tag in hay for tag in ("safety", "安全")):
        return "safety"
    if any(tag in hay for tag in ("quality", "质量")):
        return "quality"
    return "basic"


def resolve_category(raw: Any, key: str = "", label: str = "") -> str:
    """归一化事实分类；无法识别（落到 basic）时按 key + label 兜底推断。

    BUG-FIX 2026-09-29（分类缺失会拖垮整条数据链）：事实条目进入库里后，
    `category` 是三处下游的唯一依据：前端分组展示、审计送审分组、以及
    `consistency_auditor.FACT_BASELINE_CATEGORIES`（按 category 匹配已确认基准）。
    而历史实现只做别名映射，空值/未知值直接塌成 basic：调用方（含
    `GlobalFact` 模型的默认值）不传 category 时，同一事实会从审计基准里静默消失。
    现在对识别失败的条目补一次推断。显式合法分类保持原值，不行为改变。
    """
    normalized = normalize_category(raw)
    if normalized != "basic":
        return normalized
    return infer_category(key, label)


def dict_to_fact_items(d: dict) -> list:
    """将 AI 返回的 JSON dict（{key: value}）转换为扁平事实列表。

    这是两份历史副本（routers/facts.py 与 workflows_analysis.py）的正本合一：
      - routers 副本把「技术」放进 personnel 标签，且 personnel 缺「经理」
      - workflows 副本 label 直接用英文 key，分类只看 key
    """
    items: list = []
    if not isinstance(d, dict):
        return items
    for k, v in d.items():
        if v is None:
            continue
        val_str = v.strip() if isinstance(v, str) else str(v).strip()
        if not val_str:
            continue
        label = KEY_LABEL_ZH.get(k, str(k))
        items.append(
            {
                "key": k,
                "label": label,
                "value": val_str,
                "category": infer_category(k, label),
                "source": "ai",
                "verified": False,
            }
        )
    return items


def normalize_fact_items(
    items: list,
    fallback_text: str = "",
    policy: str = DEFAULT_FACTS_POLICY,
) -> list:
    """事实条目的值规范化后处理：去重 + 必须有工期（缺值兜底按策略生成）。

    依据《全局事实变量分析与提取.md》4.2 值规范化规则。
    统一取代原 `_normalize_facts`（facts.py，三档策略）与
    `_normalize_fact_items`（workflows_analysis，只认 fabricate）两份实现。
    """
    if not isinstance(items, list):
        items = []

    dedup = dedup_facts(items)

    # 分类归一化：把 AI/启发式给的中文分类、同义词收敛到命名空间内；
    # 识别不出的条目再按 key + label 推断（否则整批塌成 basic）
    for f in dedup:
        f["category"] = resolve_category(f.get("category"), f.get("key", ""), f.get("label", ""))

    # 必须有工期：先正则提取，再按策略兜底
    if not has_schedule_fact(dedup):
        schedule_val = extract_schedule_value(fallback_text)
        if schedule_val:
            dedup.append(
                {
                    "key": "scheduleDuration",
                    "label": "建设工期",
                    "value": schedule_val,
                    "category": "schedule",
                    "source": "heuristic",
                    "verified": False,
                }
            )
        else:
            normalized_policy = normalize_facts_policy(policy)
            dedup.append(
                {
                    "key": "scheduleDuration",
                    "label": "建设工期",
                    "value": schedule_fallback_value(normalized_policy),
                    "category": "schedule",
                    "source": "default",
                    "verified": False,
                    "needs_manual_input": normalized_policy == "placeholder",
                }
            )

    # BUG-FIX 2026-10-06（口径分叉）：placeholder 打标此前只在
    # `routers/facts.py` 的 /facts/generate 端点显式调 mark_placeholders，
    # 而评分链路 `workflows_analysis.extract_global_facts` 只走本函数、
    # 不打标 —— placeholder 策略下评分自动产出的【待填写】条目
    # needs_manual_input=False，前端逐行「待人工填写」角标缺失，
    # 与手动提取链路行为不一致。打标是幂等的（只对占位项置 True），
    # 上收正本后所有调用方（手动提取/评分自动提取/启发式兜底）统一口径。
    mark_placeholders(dedup)

    return dedup


# ============================================================
# 八、一致性审计结果解析
# ============================================================


def coerce_audit_list(result: Any) -> list[dict]:
    """把 AI 返回的事实审计结果归一化为 list[dict]。

    BUG-FIX 2026-09-18（中）：`collect_json_response` 会原样返回模型给出的顶层结构，
    模型经常用 `{"items": [...]}` / `{"results": [...]}` / `{"data": [...]}` 包装。
    原实现只接受裸 list，遇到包装即整体丢弃 → 接口返回空数组 → 前端把「0 条问题」
    误读为「全部一致」。此处解包常见包装键，并做最基础的元素类型过滤。
    """
    if result is None:
        return []
    if isinstance(result, list):
        return [x for x in result if isinstance(x, dict)]
    if isinstance(result, dict):
        for key in ("items", "results", "data", "audit_results", "findings", "list"):
            v = result.get(key)
            if isinstance(v, list):
                return [x for x in v if isinstance(x, dict)]
        # 单条结果（dict）也接受，避免模型只返回一条时被丢弃
        if any(k in result for k in ("fact_key", "fact_label", "status")):
            return [result]
    return []


# ============================================================
# 九、事实值的可比归一化（一致性检测用）
# ============================================================

_NUM_RE = re.compile(r"\d+(?:\.\d+)?")
_UNIT_STRIP_RE = re.compile(r"[\s，,。.、；;：:（）()【】\[\]\"'“”‘’\-—_/／]")


def normalize_fact_value(value: Any) -> str:
    """把事实值归一化为「可比较形态」，用于冲突判定。

    BUG-FIX 2026-09-29（误报）：原实现用 `set(原文)` 判冲突，于是
    "450日历天" / "450 日历天" / "450天" 三种同义写法被判成三个冲突值，
    审计报告里挤满假阳性，用户很快就不相信这个功能。
    归一化规则：全角转半角 → 剥空白与标点 → 单位同义归一（日历天→天）；
    含数字的值再拆成「数字序列 + 剩余文字」。
    边界（有意保守，不要当成 bug）：**只消除排版差异，不做语义推断**。
    带着额外标签词的写法（"工期450天" 与 "450天"）归一化后仍不同，
    会被判为冲突 —— 宁多报也不漏报真冲突。调用方若需精确比较，
    应先把两端置于同一抽取口径（参考 `consistency_auditor` 用
    `extract_facts_from_text` 同时处理基准值与正文）。
    """
    text = str(value or "")
    if not text:
        return ""
    # 全角字符 → 半角
    out = []
    for ch in text:
        code = ord(ch)
        if code == 0x3000:
            code = 32
        elif 0xFF01 <= code <= 0xFF5E:
            code -= 0xFEE0
        out.append(chr(code))
    text = "".join(out).lower()
    text = _UNIT_STRIP_RE.sub("", text)
    # 单位口径统一：日历天/自然日/日 → 天；万元 → 万
    text = text.replace("自然日", "天").replace("日历天", "天").replace("工作日", "天")
    text = text.replace("万元", "万").replace("个月", "月")
    nums = _NUM_RE.findall(text)
    if nums:
        # 含数字的值：只保留「数字序列 + 其余字符」，忽略顺序差异以外的一切装饰
        return f"{','.join(nums)}|{_NUM_RE.sub('', text)}"
    return text


def values_conflict(values: list) -> bool:
    """一组值里是否存在**归一化后仍不同**的取值（≥2 种）。"""
    return len({normalize_fact_value(v) for v in (values or []) if str(v or "").strip()}) > 1


def group_fact_conflicts(items: list) -> list[dict]:
    """检测事实集**内部**的互斥取值（同一事实出现不同值的矛盾）。

    BUG / 缺口（全局事实模块内部一致性）：模块此前只做「跨章节正文一致性审计」
    （/facts/audit、consistency_auditor），却**从不在自身事实集内部查矛盾**——
    于是「项目经理：张伟」与手工补录的「项目经理：李强」（不同 key、同 label）
    能同时落库，正文注入时两值并存，制造出本模块本应避免的前后矛盾。

    判定：按「归一化 label」归组（同 label 视为同一事实），组内出现 ≥2 个归一化
    后不同的 value 即记为冲突。占位值与空值不参与比较（它们本就是待定/待补，
    不应与真实值互比）。key 不同的同 label 事实也会被归并——这正是 `dedup_key`
    只按 key 去重所漏掉的那类矛盾。

    Returns:
        list[dict]: 每项 {label, key, values:[{key,label,value,category}...]}
        仅含真正冲突的组，供前端高亮、供接口返回 `fact_conflicts`（非阻断）。
    """
    from collections import defaultdict

    groups: dict[str, list[dict]] = defaultdict(list)
    for f in (items or []):
        if not isinstance(f, dict):
            continue
        val = str(f.get("value") or "")
        if not val.strip() or is_placeholder_value(val):
            continue
        fk = dedup_key(f)
        label = (f.get("label") or fk or "").strip()
        gk = normalize_fact_value(label) if label else fk
        if not gk:
            continue
        groups[gk].append(
            {
                "key": f.get("key", ""),
                "label": label,
                "value": val,
                "category": f.get("category", ""),
                "norm_value": normalize_fact_value(val),
            }
        )
    conflicts: list[dict] = []
    for _gk, members in groups.items():
        norms = {m["norm_value"] for m in members}
        if len(norms) > 1:
            conflicts.append(
                {
                    "label": members[0]["label"],
                    "key": members[0]["key"],
                    "values": members,
                }
            )
    return conflicts


# ============================================================
# 十、提取提示词正本（消除 facts 路由与 Step02 链路的双份维护）
# ============================================================

#: 值规范化规则里除「第 3 条（策略相关）」之外的固定条目。
#: ENHANCE 2026-09-29：此前 `routers/facts.py::extract_sys` 与
#: `workflows_analysis.extract_global_facts` 的 `sys_prompt` 各写一份完整提示词，
#: 改一处忘另一处 —— 结果是「同一平台，手动提取按三档策略走，评分流程自动提取
#: 永远按 fabricate 走」。现在两条链路共用本正本。
_RULE_USER_FIRST = (
    "1. **用户资料优先**：资料已给出明确事实值时，直接用资料中的值，不得改动。"
)
_RULE_TO_FACT = (
    "2. **要求转事实**：资料只给出约束/要求时，转写为\"本方案已采用/已具备/统一承诺\"的"
    "确定性事实表达，而非摘录要求。\n"
    '   - ❌ "工期不能超过450天" → [OK] "建设工期：450 日历天"\n'
    '   - ❌ "需提供2年质保" → [OK] "质保期：2 年（自竣工验收合格之日起）"\n'
    '   - ❌ "建议采用BIM管理" → [OK] "已采用 BIM 全过程管理平台"\n'
    '   - ❌ "项目经理要求一级建造师" → [OK] "项目经理：具备一级建造师执业资格"'
)
_RULE_TECH_ONLY = "4. **仅技术方案**：只关注技术方案部分，不涉及商务报价或资格材料。"
_RULE_MUST_SCHEDULE = (
    "5. **必须有工期**：输出必须包含工期、运维期或交货时间中的**至少一个**时间类变量，"
    "否则视为不完整。"
)


def value_normalization_rules(policy: str = DEFAULT_FACTS_POLICY) -> str:
    """「值规范化规则」全文（1~5 条 + 策略附加约束），按缺失值策略拼装。"""
    return (
        f"{_RULE_USER_FIRST}\n"
        f"{_RULE_TO_FACT}\n"
        f"{policy_rule_text(policy)}\n"
        f"{_RULE_TECH_ONLY}\n"
        f"{_RULE_MUST_SCHEDULE}" + policy_tail_note(policy)
    )


def fact_extract_system_prompt(policy: str = DEFAULT_FACTS_POLICY) -> str:
    """全局事实提取的 system 提示词正本。"""
    return f"""# 全局事实变量提取专家

## 角色
你是招投标方案编制专家，从项目文档中提取**确定性事实变量**，用于保障后续各章节 AI 生成内容的前后一致性。

## 核心目标
全局事实变量是技术方案正文中必须保持前后一致的确定性事实，任何章节不得与之矛盾。

## 必须提取的六大类事实
1. **项目人员**：项目经理、技术负责人、安全负责人等关键岗位姓名、角色、职责
2. **工期与时间节点**：总工期、开工/竣工日期、运维期/保修期时长、交货时间、关键里程碑
3. **技术路线与设备参数**：技术路线/工艺、主要设备型号规格、统一配置标准、关键材料品牌型号
4. **服务承诺与响应**：服务质量承诺、故障响应时间、售后服务期限、培训承诺
5. **实施做法与执行安排**：施工/实施方法、组织架构与分工、进度保障措施、质量控制措施
6. **基础项目事实**：项目名称、建设单位、建设地点、规模、建筑面积、工程量、工程类型、承包范围、质量要求、安全目标

## 提取规则
- 每个事实独立、可验证、不可随意发挥
- 优先提取数字、日期、名称、等级等确定性信息
- key 用英文驼峰命名（如 projectManager, totalDuration, serviceResponseTime）
- value 写具体值，不含主观推测
- **不要输出 category**：分类由后端按 key/label 统一推断，避免同一事实在不同链路被归到不同类别
- **不要输出 label**：中文标签由后端字典统一维护

## 值规范化规则（关键，必须严格遵守）
{value_normalization_rules(policy)}

## 输出格式
```json
{{"projectName":"...","constructionLocation":"...","scheduleDuration":"..."}}
```
直接输出 JSON 对象（dict），key 使用驼峰命名，value 为字符串，严禁附加任何解释。"""


# ============================================================
# 九、按章节筛选事实（ENHANCE 2026-10-05 / P-5）
# ============================================================

#: 打分时忽略的标点与空白（只比较实义字符）
_FACT_IGNORED_CHARS = set(
    " \t\r\n，。；：、（）()《》“”\"'’‘·-—_/\\|!！?？,.;:+*#[]{}<>%％&"
)

#: 无论匹配度多低都强制注入的事实分类。
#: 项目名称、建设地点、总工期这类事实是**每一章都要用**的基线，
#: 若按「章节标题重合度」筛选会被全部剔除（章节标题里几乎不会出现
#: 「工期」「项目名称」这种词），导致正文里出现「工期 90 天」和「工期 120 天」
#: 并存——正是易标规格三.1 反复强调要避免的前后不一致。
_ALWAYS_INCLUDE_CATEGORIES: tuple[str, ...] = ("basic", "schedule")

#: 章节事实注入的条数上限（超过则按相关性截断）
FACTS_SELECT_MAX = 60


def _fact_signature_chars(text: Any) -> set:
    """取出实义字符集合（去标点/空白/去重）。"""
    out: dict[str, None] = {}
    for ch in str(text or ""):
        if ch in _FACT_IGNORED_CHARS:
            continue
        out.setdefault(ch, None)
    return set(out)


def _fact_bigrams(text: Any) -> set:
    """取出实义字符二元组（滑窗），用于给「连续命中」加权。"""
    chars = [ch for ch in str(text or "") if ch not in _FACT_IGNORED_CHARS]
    return {chars[i] + chars[i + 1] for i in range(len(chars) - 1)}


def score_fact_relevance(fact: dict, query_chars: set, query_bigrams: set) -> int:
    """单条事实与章节标题/概要的相关度打分。

    打分口径（与 feasibility_knowledge 的字符重合度打分同一思路）：
      - 每个**命中**的实义字符 +1
      - 每个**命中**的连续二元组 +2（连续命中信息量远高于零散命中，
        避免「混凝土施工方案」被「水」「方」「案」三个单字分散命中抬高分数）

    Returns:
        int: 相关度分数，0 表示完全不相关。
    """
    fact_text = " ".join(
        str(fact.get(k) or "") for k in ("key", "label", "value", "category")
    )
    f_chars = _fact_signature_chars(fact_text)
    f_bigrams = _fact_bigrams(fact_text)
    return len(f_chars & query_chars) + 2 * len(f_bigrams & query_bigrams)


def select_facts_for_section(
    facts: list,
    section_title: str = "",
    section_summary: str = "",
    max_items: int = FACTS_SELECT_MAX,
) -> list:
    """按章节标题/概要筛选最相关的事实，替代「全量注入 + 截断」。

    易标规格三.4 第二步：「根据章节标题选择最相关事实并注入」。
    旧实现把全部事实逐章注入、超 2000 字符就硬截断，事实条目多时
    早期章节占满预算、尾部章节拿不到 —— 且被截断掉的恰好是排在后面的事实。

    排序策略（相关性优先，基线事实兜底）：
      1. 相关度分数 > 0 的事实，按分数降序
      2. `_ALWAYS_INCLUDE_CATEGORIES` 里的基线事实（无论分数）
      3. 其余事实，保持原顺序

    这样即便 max_items 截断，被保留的一定是「本章最相关的 + 全局基线的」，
    而不是「排在最前面的」。

    Args:
        facts: 事实条目列表（每项含 key/label/value/category）
        section_title: 章节标题
        section_summary: 章节概要（可选，提高匹配精度）
        max_items: 返回条数上限

    Returns:
        list: 筛选并重排后的事实列表（可能短于原列表）
    """
    if not facts:
        return []
    # 非 list（如已格式化的 str / dict）无法逐条打分，原样返回 ——
    # 否则遍历字符串会拿到单个字符、items 为空，静默丢掉全部事实。
    if not isinstance(facts, (list, tuple)):
        return facts
    items = [f for f in facts if isinstance(f, dict)]
    if not items:
        return []

    query_chars = _fact_signature_chars(section_title) | _fact_signature_chars(section_summary)
    query_bigrams = _fact_bigrams(section_title) | _fact_bigrams(section_summary)

    if not query_chars:
        # 标题/概要为空时无法打分，退化为「基线优先 + 原顺序」
        scored = [(0, i, f) for i, f in enumerate(items)]
    else:
        scored = [(score_fact_relevance(f, query_chars, query_bigrams), i, f)
                  for i, f in enumerate(items)]

    matched = [(s, i, f) for s, i, f in scored
               if s > 0 and f.get("category") not in _ALWAYS_INCLUDE_CATEGORIES]
    baseline = [(s, i, f) for s, i, f in scored
                if f.get("category") in _ALWAYS_INCLUDE_CATEGORIES]
    rest = [(s, i, f) for s, i, f in scored
            if s <= 0 and f.get("category") not in _ALWAYS_INCLUDE_CATEGORIES]

    matched.sort(key=lambda x: (-x[0], x[1]))
    baseline.sort(key=lambda x: (-x[0], x[1]))

    result: list[dict] = []
    seen: set[int] = set()
    for _s, i, f in matched + baseline + rest:
        if len(result) >= max_items:
            break
        if i in seen:
            continue
        seen.add(i)
        result.append(f)
    return result


# ============================================================
# 九、全局事实增强开关与两阶段消费工具（2026-10-14 facts-ns P0/P1）
# ============================================================
# 对齐易标（OpenBidKit）全局事实全流程：本节提供
#   1. 环境变量开关（默认全部保持现有行为，与既有 AUTO_FALLBACK_CHARTS
#      等开关同一读法；调用时读取而非 import 时固化，便于测试覆盖）；
#   2. 编排阶段「AI 只看标题选事实」的标题清单与按标题取全文的解析正本；
#   3. omit/placeholder 模式向正文传播的「完整性指令」正本。


def _env_flag(name: str, default: str = "0") -> bool:
    """布尔环境变量开关：默认关（保持现有行为）；设为 1/true/on/yes 开启。"""
    raw = str(os.getenv(name, default) or default).strip().lower()
    return raw not in {"", "0", "false", "off", "no"}


def orchestrated_select_enabled() -> bool:
    """P0：编排阶段 AI 选事实标题通道（FACTS_ORCHESTRATED_SELECT_ENABLED，默认关）。

    开启后编排 plan 增加 facts.titles，正文生成优先按选定标题取全文注入，
    未选定/未命中时回退 `select_facts_for_section` 确定性打分筛选。
    """
    return _env_flag("FACTS_ORCHESTRATED_SELECT_ENABLED")


def segmented_extraction_enabled() -> bool:
    """P1-1：超长招标文件分段提取（FACTS_SEGMENTED_EXTRACTION_ENABLED，默认关）。

    开启后源文本超过 `facts_segment_token_cap()` 时按段提取→归并，
    不再被 truncate_to_tokens 静默截断丢内容；单段文件行为不变。
    """
    return _env_flag("FACTS_SEGMENTED_EXTRACTION_ENABLED")


def facts_segment_token_cap() -> int:
    """P1-1：分段提取的单段 token 预算（FACTS_SEGMENT_TOKEN_CAP，默认 20000）。"""
    raw = os.getenv("FACTS_SEGMENT_TOKEN_CAP", "20000")
    try:
        cap = int(raw)
    except (TypeError, ValueError):
        cap = 20000
    return cap if cap > 0 else 20000


def outline_input_enabled() -> bool:
    """P1-2：目录标题树作为事实提取输入（FACTS_OUTLINE_INPUT_ENABLED，默认关）。

    对齐易标「先目录后事实」：目录只约束事实的大项范围，不反向消费事实。
    """
    return _env_flag("FACTS_OUTLINE_INPUT_ENABLED")


def completeness_instruction_enabled() -> bool:
    """P1-3：omit/placeholder 正文完整性指令（FACTS_COMPLETENESS_INSTRUCTION_ENABLED，默认关）。"""
    return _env_flag("FACTS_COMPLETENESS_INSTRUCTION_ENABLED")


def content_repair_max_attempts() -> int:
    """P1-4：AI 补丁修复失败回喂重试上限（CONSISTENCY_REPAIR_MAX_ATTEMPTS，默认 1=现行为）。"""
    raw = os.getenv("CONSISTENCY_REPAIR_MAX_ATTEMPTS", "1")
    try:
        n = int(raw)
    except (TypeError, ValueError):
        n = 1
    return max(1, min(n, 5))


#: 编排标题清单的条数上限（易标：编排只看标题，token 成本≈事实全文 1/10）
FACTS_TITLES_CATALOG_MAX = 120
#: 编排单节可选事实标题上限（防止 AI 整单全选把注入预算打爆）
FACTS_TITLES_PER_SECTION_MAX = 12


def fact_title_of(fact: Any) -> str:
    """取一条事实的「标题」（与正文注入格式同一口径：label 优先，兜底 key/title）。"""
    if not isinstance(fact, dict):
        return ""
    return str(fact.get("label") or fact.get("title") or fact.get("key") or "").strip()


def fact_titles_for_orchestration(facts: Any, max_items: int = FACTS_TITLES_CATALOG_MAX) -> list[str]:
    """事实列表 → 去重保序的标题清单（供编排阶段 AI 选择，不含 value）。

    易标对照：contentGenerationTask.cjs 的 globalFactTitlesText（编排时只能选
    标题，不输出具体变量内容）。
    """
    titles: list[str] = []
    seen: set[str] = set()
    for f in facts or []:
        t = fact_title_of(f)
        if not t or t in seen:
            continue
        seen.add(t)
        titles.append(t)
        if len(titles) >= max_items:
            break
    return titles


def _normalize_title_key(value: str) -> str:
    """标题匹配用的归一化：去空白（含全角空格）与尾部标点，大小写不敏感。"""
    return re.sub(r"[\s\u3000]+", "", str(value or "")).strip("：:、,.").lower()


def resolve_facts_by_titles(
    titles: Any,
    facts: Any,
    max_items: int = FACTS_SELECT_MAX,
) -> list:
    """按编排选定的事实标题取出完整事实条目（两阶段消费的程序侧）。

    易标对照：contentGenerationTask.cjs 的 resolveGlobalFactsByTitles。
    匹配策略：归一化精确匹配（label/key 任一命中）优先，其次互包含模糊匹配；
    AI 幻觉出的清单外标题一律丢弃（与 knowledge.item_ids 白名单同一安全思路）。
    无命中返回空列表，由调用方回退确定性打分筛选。
    """
    if not isinstance(titles, list) or not titles or not isinstance(facts, list) or not facts:
        return []
    wanted = [str(t).strip() for t in titles if str(t).strip()]
    if not wanted:
        return []
    # 预建归一化索引：title_key -> facts 下标（同一标题只认首条，与标题唯一性约定一致）
    exact: dict[str, int] = {}
    norm_facts: list[tuple[str, int]] = []  # (归一化标题, 下标)
    for i, f in enumerate(facts):
        t = fact_title_of(f)
        if not t:
            continue
        k = _normalize_title_key(t)
        if k and k not in exact:
            exact[k] = i
        norm_facts.append((k, i))

    picked: list[dict] = []
    used: set[int] = set()
    for w in wanted:
        if len(picked) >= max_items:
            break
        kw = _normalize_title_key(w)
        idx = exact.get(kw)
        if idx is None and len(kw) >= 2:
            # 模糊兜底：标题被 AI 抛写成前/后缀变体（如多/少「（日历天）」）时互包含命中
            cands = [i for k, i in norm_facts if k and (kw in k or k in kw)]
            idx = cands[0] if cands else None
        if idx is None or idx in used:
            continue
        used.add(idx)
        picked.append(facts[idx])
    return picked


#: omit/placeholder 模式下追加到正文 system 的「完整性指令」正本
#: （易标对照：buildContentFactCompletenessInstruction(globalFactsMode)）
CONTENT_COMPLETENESS_INSTRUCTIONS: dict[str, str] = {
    "fabricate": "",
    "omit": (
        "【缺值口径铁律】本项目采用「不编造」事实策略：事实变量中含“按招标文件要求/按合同约定”"
        "等笼统承诺表述的条目，正文引用时必须沿用同样笼统口径，严禁自行补出具体工期天数、"
        "人员姓名、品牌型号、金额日期等具体值；也不得在不同章节给出不同的笼统说法。"
    ),
    "placeholder": (
        "【缺值口径铁律】本项目采用「留占位」事实策略：事实变量中以「【待填写】」开头的条目，"
        "正文涉及时必须原样写「【待填写：事项名】」占位，严禁编造具体值顶替；"
        "未涉及的事项不要主动铺占位符。"
    ),
}


def content_completeness_instruction(policy: Any) -> str:
    """按缺失值策略返回正文「完整性指令」；fabricate/非法值返回空串（行为不变）。"""
    return CONTENT_COMPLETENESS_INSTRUCTIONS.get(normalize_facts_policy(policy), "")
