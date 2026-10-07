"""workflows 子模块：workflows_analysis（由 scripts/split_workflows.py 自动生成，勿手改）"""

import json
import logging
import re
from typing import Any

from ...utils.json_parser import extract_json_from_text
from ...utils.scoring_points import is_scoring_placeholder
from ..facts_service import (
    DEFAULT_FACTS_POLICY,
    dict_to_fact_items,
    fact_extract_system_prompt,
    normalize_fact_items,
    normalize_facts_policy,
)
from .prompts import get_prompt, render_prompt, truncate_to_tokens

# 安全增强 2026-10-06（G5+G3）：招标原文（用户/外部内容）注入统一经
# 元标记中和（sanitize）+ 敏感信息脱敏（desensitize）+ Token 软上限保护。
# 默认 20000 token ≈ 2 万中文字；仅极端长标书被裁切，正常标书不降级。
ANALYSIS_CONTEXT_TOKEN_CAP = 20000


def _safe_tender_context(tender_context: str) -> str:
    """招标原文注入前的安全处理：Token 软上限截断。

    元标记中和与 PII 脱敏在 get_prompt/render_prompt 调用处通过
    sanitize=True / desensitize=True 统一施加，确保注入变量值不含
    {shared:...}/__VAR__ 元标记，且邮箱/手机/身份证/密钥被掩码。
    """
    return truncate_to_tokens(tender_context, ANALYSIS_CONTEXT_TOKEN_CAP)
from .providers.base import AIMessage


logger = logging.getLogger(__name__)


from .workflows_base import call_ai_text, collect_json_response, with_timeout


async def generate_tender_summary(provider: Any, tender_context: str) -> str:
    """生成招标摘要"""
    logger.info("[AI摘要] 开始生成招标摘要，上下文长度: %d 字符", len(tender_context))
    sys_prompt = get_prompt("TENDER_SUMMARY_SYSTEM")
    user_prompt = get_prompt(
        "TENDER_SUMMARY_USER",
        CONTEXT=_safe_tender_context(tender_context),
        sanitize=True,
        desensitize=True,
    )

    messages = [
        AIMessage(role="system", content=sys_prompt),
        AIMessage(role="user", content=user_prompt),
    ]

    # F-P0-2（2026-10-07）：改用 call_ai_text，空响应显式告警+重试+抛错，
    # 避免空摘要被静默落库（日志曾出现"响应长度: 0 字符"）。
    raw_response = await call_ai_text(
        provider, messages,
        timeout=60, max_tokens=4096,
        require_nonempty=True, retries=1, context="[AI摘要]",
    )
    logger.info("[AI摘要] 生成完成，响应长度: %d 字符", len(raw_response or ""))
    return raw_response


def _extract_array_from_response(data: Any) -> list:
    """从 AI 响应中提取数组，支持多种格式：
    - 直接数组：[...]
    - 对象包裹：{"scoring_points": [...]}、{"points": [...]}、{"items": [...]}
    - 嵌套：{"data": {"scoring_points": [...]}}
    """
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("scoring_points", "points", "items", "data", "result", "results", "list"):
            value = data.get(key)
            if isinstance(value, list):
                return value
            if isinstance(value, dict):
                nested = _extract_array_from_response(value)
                if nested:
                    return nested
    return []


async def extract_scoring_points(provider: Any, tender_context: str, summary: str) -> list[dict]:
    """两阶段识别评分点

    第一阶段：初次识别
    第二阶段：二次审查（查漏补缺）

    Returns:
        list[dict]: 标准化后的评分点列表
    """
    scoring_first_sys = get_prompt("SCORING_FIRST_ROUND_SYSTEM")
    scoring_first_user = get_prompt(
        "SCORING_FIRST_ROUND_USER",
        CONTEXT=_safe_tender_context(tender_context),
        SUMMARY=summary,
        sanitize=True,
    )
    # BUG-FIX 2026-09-14（中）：模板里的 __CONTEXT__ / __SUMMARY__ / __EXISTING_POINTS__
    # 未在 system 侧渲染 → 模型收到字面量占位符（框架日志 missing variables）。
    # system 提示词只做规则描述，这里替换成**语义标签**而非整段数据，
    # 避免把 4 万字招标上下文在 system/user 里重复注入一遍（token 翻倍）。
    scoring_review_sys = render_prompt(
        get_prompt("SCORING_REVIEW_SYSTEM"),
        CONTEXT="招标文件上下文",
        SUMMARY="项目概述",
        EXISTING_POINTS="已识别评分项",
    )
    scoring_review_user_template = get_prompt("SCORING_REVIEW_USER")
    logger.info(
        "[评分点] 提示词加载: SYSTEM=%d字, USER=%d字, REVIEW_SYS=%d字, REVIEW_USER=%d字",
        len(scoring_first_sys),
        len(scoring_first_user),
        len(scoring_review_sys),
        len(scoring_review_user_template),
    )

    logger.info(
        "[评分点] 第一阶段开始: tender_context_len=%d, summary_len=%d",
        len(tender_context),
        len(summary),
    )

    # BUG-FIX-57: summary 为空时填充占位，避免提示词出现空字段导致 AI 输出退化
    if not summary or not summary.strip():
        summary = "（无摘要，请直接依据招标上下文识别评分点）"
        logger.warning("[评分点] summary 为空，已填充占位文本")

    # 第一阶段：初次识别
    first_messages = [
        AIMessage(role="system", content=scoring_first_sys),
        AIMessage(role="user", content=scoring_first_user),
    ]

    # BUG-FIX-58: 第一阶段添加异常降级，AI 调用失败时返回空列表而非抛异常中断整个流程
    # BUG-FIX 2026-09-16（中）：with_timeout 抛 TimeoutError（OSError 子类）、
    # provider 抛网络异常（httpx.ConnectError 等），均不在 (ValueError, RuntimeError)
    # 内 → 最常见的超时场景降级完全失效。改用 Exception 兜底
    # （asyncio.CancelledError 属 BaseException，不会被误吞，取消语义不受影响）。
    try:
        first_raw = await with_timeout(provider.chat(first_messages), timeout=60)
        first_points = _extract_array_from_response(extract_json_from_text(first_raw))
    except Exception as e:
        logger.warning("[评分点] 第一阶段失败（返回空列表降级）: %s", e)
        return []

    existing_points_json = json.dumps(first_points, ensure_ascii=False, indent=2)
    review_user = render_prompt(
        scoring_review_user_template,
        CONTEXT=_safe_tender_context(tender_context),
        SUMMARY=summary,
        EXISTING_POINTS=existing_points_json,
        sanitize=True,
    )

    # 第二阶段：二次审查（查漏补缺）
    logger.info("[评分点] 第二阶段开始：二次审查，已有评分点=%d个", len(first_points))
    review_messages = [
        AIMessage(role="system", content=scoring_review_sys),
        AIMessage(role="user", content=review_user),
    ]

    try:
        review_content = await with_timeout(provider.chat(review_messages), timeout=120)
        review_points = _extract_array_from_response(extract_json_from_text(review_content))
        logger.info(
            "[评分点] 第二阶段完成：补充 %d 个评分点，总计 %d 个",
            len(review_points),
            len(first_points) + len(review_points),
        )
    except Exception as e:
        # 同上：超时/网络异常也必须降级回第一阶段结果，不能让整次评分识别失败
        logger.warning("[评分点] 二次审查失败（降级使用第一阶段结果）: %s", e)
        review_points = []

    # 合并两阶段结果
    all_points = first_points + review_points
    return normalize_writing_targets(all_points)


async def extract_global_facts(
    provider: Any,
    tender_context: str,
    summary: str,
    work_interface: str = "",
    construction_method: str = "",
    policy: str = DEFAULT_FACTS_POLICY,
) -> list[dict]:
    """从招标文件上下文 + 摘要中提取全局事实变量（评分模块配套提取）

    依据《全局事实变量分析与提取.md》：
    - 六大类确定性事实：项目人员 / 工期时间 / 技术路线设备 / 服务承诺 / 实施做法 / 基础项目事实
    - 值规范化规则：用户资料优先、要求转事实、无值可补、仅技术方案、必须有工期

    Args:
        policy: 缺值处理三档（fabricate / omit / placeholder）。ENHANCE 2026-09-29
            （facts-ns）：本函数以前不接策略、提示词写死为「可模拟生成」一档，
            而 Step03 手动提取端点按用户所选策略执行 —— 同一项目重跑评分会把
            用户要的 omit/placeholder 语义覆盖回 fabricate，兜底缺值也被编造。
            现在策略从 `analysis.facts_policy` 透传，与手动链路共用提示词正本。
            默认值保持 fabricate，与旧调用方（不传 policy）行为完全一致。

    Returns:
        list[dict]: 扁平结构的全局事实列表（key/label/value/category/source/verified）
    """
    policy = normalize_facts_policy(policy)
    # 提示词正本：`facts_service.fact_extract_system_prompt`
    sys_prompt = fact_extract_system_prompt(policy)

    # 多源融合：与 /facts/generate 端点保持一致的 step02_parts 逻辑
    step02_parts: list[str] = []
    if work_interface and work_interface != "[]":
        try:
            wi = json.loads(work_interface)
            if isinstance(wi, list) and wi:
                step02_parts.append(f"【工作界面/分工】\n{json.dumps(wi[:8], ensure_ascii=False)}")
        except (json.JSONDecodeError, TypeError):
            pass
    if construction_method and construction_method != "[]":
        try:
            cm = json.loads(construction_method)
            if isinstance(cm, list) and cm:
                step02_parts.append(
                    f"【实施做法/施工方法】\n{json.dumps(cm[:8], ensure_ascii=False)}"
                )
        except (json.JSONDecodeError, TypeError):
            pass

    multi_source_context = f"""请从以下多源项目文档中提取全局事实变量（后序来源作为补充，与核心来源冲突时以核心来源为准）：\n\n【核心来源：招标文件全文】\n{tender_context}\n"""
    if step02_parts:
        multi_source_context += "\n\n【补充来源：Step02 关键解析结果】\n" + "\n\n".join(
            step02_parts
        )
    if summary:
        multi_source_context += f"\n\n【辅助来源：项目摘要】\n{summary}"

    try:
        result = await collect_json_response(
            provider,
            [
                AIMessage(role="system", content=sys_prompt),
                AIMessage(role="user", content=multi_source_context),
            ],
            temperature=0.3,
            max_retries=1,
        )
        if isinstance(result, dict):
            items = _dict_to_fact_items(result)
        else:
            items = result if isinstance(result, list) else []
    except Exception as e:
        logger.warning("[全局事实] 提取异常，返回空列表: %s", e)
        items = []

    return _normalize_fact_items(items, fallback_text=summary, policy=policy)


def _dict_to_fact_items(d: dict) -> list:
    """AI 返回的 JSON dict（{key: value}）→ 扁平事实列表。

    ENHANCE 2026-09-29（facts-ns）：实现已上收到
    `facts_service.dict_to_fact_items`，与 Step03 手动提取链路共用同一
    中文 label 表与分类推断顺序（以前本副本 label 直接用英文 key，前端
    展示为一串 `projectName` / `scheduleDuration`，人工核对成本很高）。
    保留本函数名兼容既有调用方与 `test_safety_category_is_reachable`。
    """
    return dict_to_fact_items(d)


def _normalize_fact_items(items: list, fallback_text: str = "", policy: str = DEFAULT_FACTS_POLICY) -> list:
    """值规范化后处理（去重 + 必须有工期）—— 委派正本 `normalize_fact_items`。

    ENHANCE 2026-09-29（facts-ns）：本副本此前只认 fabricate 一档（兜底文案
    硬编码），与 `routers/facts.py::_normalize_facts` 两份规则会各自漂移。
    现在两条链路同一实现，兜底文案随策略变化。
    """
    return normalize_fact_items(items, fallback_text=fallback_text, policy=policy)


def normalize_writing_targets(raw_points: list[dict]) -> list[dict]:
    """标准化评分点：去重、合并、附加证据上下文、过滤非技术项

    排除关键词：
        报价、投标总价、商务标、商务响应、资格、投标函、
        业绩、履约能力、供应商实力、财务、信誉、证书
    """
    EXCLUDE_KEYWORDS = [
        "报价",
        "投标总价",
        "商务标",
        "商务响应",
        "资格",
        "投标函",
        "业绩",
        "履约能力",
        "供应商实力",
        "财务",
        "信誉",
        "证书",
        "营业执照",
        "资质",
        "注册资金",
    ]

    seen = set()
    normalized = []

    for point in raw_points:
        if not isinstance(point, dict):
            continue

        name = (point.get("name") or point.get("title") or "").strip()
        if not name:
            continue

        # 过滤非技术项
        should_exclude = False
        for kw in EXCLUDE_KEYWORDS:
            if kw in name:
                should_exclude = True
                break
        if should_exclude:
            continue

        # 去重（基于评分项名称）
        if name in seen:
            continue
        seen.add(name)

        normalized.append(
            {
                "id": f"R{len(normalized) + 1}",
                "name": name,
                "weight": str(point.get("weight") or point.get("score") or ""),
                "standard": point.get("standard") or point.get("criteria") or "",
                "source": point.get("source") or "",
                "category": point.get("category") or "评分项",
                # 增强 4.2: 结构化字段溯源
                "source_ref": point.get("source_ref") or point.get("original_text") or "",
                "source_page": point.get("source_page"),
                "confidence": point.get("confidence", 1.0),
            }
        )

    # 增强 4.1: 评分项权重自动校验
    weights = []
    for p in normalized:
        w = p.get("weight", "")
        if w and w != "未提及":
            # 尝试提取数字
            digits = re.findall(r"\d+\.?\d*", str(w))
            if digits:
                try:
                    weights.append(float(digits[0]))
                except ValueError:
                    pass
    if weights:
        total = sum(weights)
        # 权重总和偏差超过1个百分点，且有正数权重时才发出警告
        if total > 0 and abs(total - 100) > 1:
            logger.warning("评分项权重之和为 %s，可能遗漏评分项", total)  # BUG-FIX-59: 改用延迟格式化避免无谓字符串构造
            # 检查是否已有警告项（避免重复插入）
            # ENHANCE 2026-09-18：判据收敛到 app.utils.scoring_points（与
            # outline_gate / workflows_sections / workflows_outline_v51 / export 同源）。
            has_warning = len(normalized) > 0 and is_scoring_placeholder(normalized[0])
            if not has_warning:
                # 在第一个元素中附加警告信息（不改变返回结构）
                normalized.insert(
                    0,
                    {
                        "id": "__weight_warning__",
                        "name": f"[权重异常] 评分项权重之和为 {total}，请检查是否遗漏评分项",
                        "weight": "",
                        "standard": "",
                        "source": "系统自动校验",
                        "category": "系统提示",
                        "source_ref": "",
                        "source_page": None,
                        "confidence": 1.0,
                    },
                )

    return normalized


def _sanitize_scoring_points(scoring_points: str) -> str:
    """预处理评分点 JSON 字符串，确保字段完整

    - 解析 JSON 字符串
    - 为缺失 category 的项补充默认值 "评分项"
    - 为空 weight 的项补充默认值 "未明确"
    - 过滤掉 name 为空的项
    - 返回处理后的 JSON 字符串（解析失败则原样返回）
    """
    if not scoring_points or not scoring_points.strip():
        return scoring_points
    try:
        points = json.loads(scoring_points)
    except (json.JSONDecodeError, TypeError):
        return scoring_points

    if not isinstance(points, list):
        return scoring_points

    sanitized = []
    for p in points:
        if not isinstance(p, dict):
            continue
        name = (p.get("name") or p.get("title") or "").strip()
        if not name:
            continue
        item = dict(p)
        item["name"] = name
        if not item.get("category"):
            item["category"] = "评分项"
        weight = str(item.get("weight") or item.get("score") or "").strip()
        item["weight"] = weight if weight else "未明确"
        sanitized.append(item)

    return json.dumps(sanitized, ensure_ascii=False)


async def analyze_boq(provider: Any, tender_context: str) -> list[dict]:
    """分析清单工程量"""
    logger.info("[AI清单] 开始分析清单工程量，上下文长度: %d 字符", len(tender_context))
    sys_prompt = get_prompt("BOQ_ANALYSIS_SYSTEM")
    user_prompt = get_prompt(
        "BOQ_ANALYSIS_USER",
        CONTEXT=_safe_tender_context(tender_context),
        sanitize=True,
        desensitize=True,
    )

    messages = [
        AIMessage(role="system", content=sys_prompt),
        AIMessage(role="user", content=user_prompt),
    ]

    # BUG-FIX 2026-09-18（高）：`collect_json_response` 在 AI 不可用 / 返回非 JSON 时
    # 抛 `RuntimeError`（workflows_base.py 末尾的 `raise RuntimeError(...)`），而本函数
    # 原先**没有 try 包裹** → 异常直接冒泡到路由层（`routers/ai.py:288`）被转成
    # HTTP 502「AI服务调用失败」，下面精心准备的 BOQ1~BOQ3 默认清单**永远不可达**
    # （纯死代码）。同型的 `analyze_construction_method` 早已修过（BUG-X-02），
    # 这里与 `analyze_work_interface` 是漏修的两处，导致「工程作法」能降级、
    # 「清单工程量」却整条失败，行为不一致。
    result = None
    try:
        result = await collect_json_response(provider, messages, max_retries=1)
    except Exception as e:
        logger.warning("[AI清单] collect_json_response 抛异常: %s，使用默认清单项目", e)
    if isinstance(result, list) and result:
        logger.info("[AI清单] 分析完成，共 %d 项", len(result))
        return result

    # AI 返回错误或空结果时，使用默认清单项目
    logger.warning("BOQ 分析 AI 返回非列表或空结果，使用默认清单项目")
    return [
        {
            "id": "BOQ1",
            "name": "清单编制依据",
            "description": "参照《建设工程工程量清单计价规范》GB50500及相关定额标准",
            "detail": "采用国家标准清单编制规范，结合项目实际情况编制",
        },
        {
            "id": "BOQ2",
            "name": "土建工程",
            "description": "包含基础、主体结构、二次结构等土建工程内容",
            "detail": "涵盖混凝土结构、砌体结构、钢结构等所有土建分部分项工程",
        },
        {
            "id": "BOQ3",
            "name": "防水工程",
            "description": "地下防水、屋面防水及室内有浸水房间部位的防水工程",
            "detail": "包含防水卷材、防水涂料等材料施工及验收",
        },
    ]


async def analyze_work_interface(provider: Any, tender_context: str) -> list[dict]:
    """分析工作界面"""
    logger.info("[AI工界] 开始分析工作界面，上下文长度: %d 字符", len(tender_context))
    sys_prompt = get_prompt("WORK_INTERFACE_ANALYSIS_SYSTEM")
    user_prompt = get_prompt(
        "WORK_INTERFACE_ANALYSIS_USER",
        CONTEXT=_safe_tender_context(tender_context),
        sanitize=True,
        desensitize=True,
    )

    messages = [
        AIMessage(role="system", content=sys_prompt),
        AIMessage(role="user", content=user_prompt),
    ]

    # BUG-FIX 2026-09-18（高）：同 `analyze_boq` —— 原先无 try 包裹，
    # `collect_json_response` 抛 RuntimeError 时下面的 WI1~WI3 默认工作界面
    # 永远不可达（死代码），且整条「工作界面分析」直接 502 失败。
    result = None
    try:
        result = await collect_json_response(provider, messages, max_retries=1)
    except Exception as e:
        logger.warning("[AI工界] collect_json_response 抛异常: %s，使用默认工作界面", e)
    if isinstance(result, list) and result:
        logger.info("[AI工界] 分析完成，共 %d 项", len(result))
        return result

    # AI 返回错误或空结果时，使用默认工作界面
    logger.warning("工作界面分析 AI 返回非列表或空结果，使用默认工作界面")
    return [
        {
            "id": "WI1",
            "name": "总包与分包界面",
            "description": "总承包单位与各专业分包单位之间的工作界面划分",
            "responsible_party": "总承包单位统筹管理",
            "coordination_requirements": "明确交接条件、验收标准和时间节点",
        },
        {
            "id": "WI2",
            "name": "土建与安装界面",
            "description": "土建工程与机电安装工程之间的工作界面",
            "responsible_party": "总承包单位协调",
            "coordination_requirements": "预埋件预留孔洞配合、管线敷设接口管理",
        },
        {
            "id": "WI3",
            "name": "室内外界面",
            "description": "室内装修与室外装饰、景观工程的工作界面",
            "responsible_party": "按合同约定划分",
            "coordination_requirements": "收口处理、防水衔接、标高控制",
        },
    ]


async def analyze_construction_method(provider: Any, tender_context: str) -> list[dict]:
    """分析工程作法"""
    logger.info("[AI工法] 开始分析工程作法，上下文长度: %d 字符", len(tender_context))
    sys_prompt = get_prompt("CONSTRUCTION_METHOD_ANALYSIS_SYSTEM")
    user_prompt = get_prompt(
        "CONSTRUCTION_METHOD_ANALYSIS_USER",
        CONTEXT=_safe_tender_context(tender_context),
        sanitize=True,
        desensitize=True,
    )

    messages = [
        AIMessage(role="system", content=sys_prompt),
        AIMessage(role="user", content=user_prompt),
    ]

    # BUG-X-02 修复：max_retries 从 1 改为 2，AI 弱模型常见"加 markdown 包裹"的非 JSON 输出，
    # 给 JSON 自修复一次额外机会；并在调用外层 catch RuntimeError 触发兜底默认值（CM1~CM5）。
    # 原实现 collect_json_response 直接 raise RuntimeError → fallback 在 result 非空检查里
    # 永远不触发（因为函数异常退出了）。
    result = None
    try:
        result = await collect_json_response(provider, messages, max_retries=2)
    except Exception as e:
        logger.warning("[AI工法] collect_json_response 抛异常: %s，使用默认工程作法", e)
    if isinstance(result, list) and result:
        logger.info("[AI工法] 分析完成，共 %d 项", len(result))
        return result

    # AI 返回错误或空结果时，使用默认工程作法
    logger.warning("工程作法分析 AI 返回非列表或空结果，使用默认工程作法")
    return [
        {
            "id": "CM1",
            "name": "土方工程",
            "description": "土方开挖、运输、回填及压实",
            "technical_requirements": "按设计要求放坡，做好降水排水措施",
            "quality_standard": "符合《建筑地基基础工程施工质量验收规范》",
        },
        {
            "id": "CM2",
            "name": "基础工程",
            "description": "桩基施工、基础垫层、基础钢筋绑扎及混凝土浇筑",
            "technical_requirements": "桩头处理、清孔插筋、防水加强层施工",
            "quality_standard": "基础工程验收合格，无渗漏",
        },
        {
            "id": "CM3",
            "name": "主体结构",
            "description": "钢筋混凝土主体结构施工，含模板、钢筋、混凝土工序",
            "technical_requirements": "泵送商品砼，严格控制标号及抗渗等级",
            "quality_standard": "结构安全，符合设计及规范要求",
        },
        {
            "id": "CM4",
            "name": "防水工程",
            "description": "地下防水、屋面防水及室内有浸水房间部位的防水施工",
            "technical_requirements": "止水钢板安装、防水层铺设、闭水试验",
            "quality_standard": "无渗漏，闭水试验合格",
        },
        {
            "id": "CM5",
            "name": "装饰装修",
            "description": "内外墙抹灰、涂料、饰面砖等装饰装修工程",
            "technical_requirements": "基层处理、腻子批嵌、涂料涂刷遍数控制",
            "quality_standard": "表面平整，色泽均匀，无开裂脱落",
        },
    ]
