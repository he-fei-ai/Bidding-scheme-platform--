"""workflows 子模块：workflows_quality（由 scripts/split_workflows.py 自动生成，勿手改）"""

import asyncio
import json
import logging
import re
from typing import Any

from ...utils import count_words as _count_words
from ...utils.json_parser import extract_json_from_text
from .prompts import get_prompt
from .providers.base import AIMessage


logger = logging.getLogger(__name__)


def _strip_chart_payloads(content: str) -> str:
    """剔除图表围栏载荷（```mermaid / ```chart-json 代码体）后的正文。

    FIX 2026-10-08（遗留7）：质量统计口径必须排除图表代码——mermaid 节点定义、
    chart-json 的任务表往往几百字符，计入 `char_count` 会让大图表章节的
    「字数达成」虚高（权重 0.35 的维度失真）。
    """
    if not content or "```" not in content:
        return content
    try:
        from ..content_cleaning import CHART_FENCE_SCAN_PATTERN

        # 整块移除（含 ```mermaid / ```chart-json 的标记行），不留任何残渣
        return CHART_FENCE_SCAN_PATTERN.sub("", content)
    except Exception:  # 解析不可用：退化为原文（向后兼容）
        return content


def _count_chart_fences(content: str) -> int:
    """统计正文里的图表围栏块数量（用于「标记使用」维度）。"""
    if not content or "```" not in content:
        return 0
    try:
        from ..content_cleaning import iter_chart_fences

        return sum(1 for _ in iter_chart_fences(content))
    except Exception:
        return 0


from .workflows_base import _fill_template, collect_json_response, with_timeout
from .workflows_outline_utils import _title_similarity


class ContentQualityEvaluator:
    """内容质量评估器：AI驱动的内容质量评分机制

    评估维度：
    1. 字数达标率：实际字数 vs 目标字数
    2. 结构完整性：是否有清晰的段落和层次
    3. 专业性：是否使用行业术语和专业表达
    4. 重复度：是否存在内容重复
    5. 相关性：是否与章节主题相关
    """

    # 质量等级
    QUALITY_EXCELLENT = "excellent"  # 90-100分
    QUALITY_GOOD = "good"  # 70-89分
    QUALITY_FAIR = "fair"  # 50-69分
    QUALITY_POOR = "poor"  # <50分

    def __init__(self, provider: Any):
        self.provider = provider

    async def evaluate_content(
        self,
        content: str,
        section_title: str,
        target_words: int | None = None,
        section_level: int = 3,
        cancel_event: "asyncio.Event | None" = None,
    ) -> dict:
        """评估单章节内容质量

        Args:
            content: 章节正文内容
            section_title: 章节标题
            target_words: 目标字数
            section_level: 章节层级（1/2/3）
            cancel_event: 取消事件（P1 修复：透传给 AI 评估调用，
                用户点停止后不再继续执行最长 60s 的兜底评估）

        Returns:
            dict: {
                "score": 总分(0-100),
                "grade": 质量等级,
                "dimensions": {
                    "word_count_score": 字数得分,
                    "structure_score": 结构得分,
                    "professional_score": 专业度得分,
                    "repetition_score": 重复度得分,
                    "relevance_score": 相关性得分
                },
                "issues": ["问题列表"],
                "suggestions": ["改进建议"]
            }
        """
        if not content:
            return {
                "score": 0,
                "grade": self.QUALITY_POOR,
                "dimensions": {
                    "word_count_score": 0,
                    "structure_score": 0,
                    "professional_score": 0,
                    "repetition_score": 0,
                    "relevance_score": 0,
                },
                "issues": ["内容为空"],
                "suggestions": ["需要重新生成内容"],
            }

        # 1. 字数达标率评估（快速检查，不调用AI）
        # BUG #3 修复：使用 _count_words 而非 len()，后者统计的是字符数（含标点空格换行），
        # 对中文文本会严重虚高，导致 word_score 始终满分、质量检查形同虚设
        actual_words = _count_words(content)
        if target_words and target_words > 0:
            word_ratio = actual_words / target_words
            word_score = min(100, word_ratio * 100)
        else:
            # 无目标字数时，基于章节层级给默认期望
            expected_words = {1: 2000, 2: 3500, 3: 1500}
            expected = expected_words.get(section_level, 1500)
            word_ratio = actual_words / expected
            word_score = min(100, word_ratio * 100)

        # 2. 重复度评估（快速检查）
        repetition_score = self._calculate_repetition_score(content)

        # 3. 规则评估结构、专业性和相关性（避免额外AI调用，提升性能）
        # 仅当规则评估总分过低（<40）时，才触发AI评估作为兜底
        rule_structure = self._rule_structure_score(content)
        rule_professional = self._rule_professional_score(content)
        rule_relevance = 70.0  # 默认中等分数

        rule_total = (
            word_score * 0.20
            + rule_structure * 0.25
            + rule_professional * 0.25
            + repetition_score * 0.15
            + rule_relevance * 0.15
        )

        if rule_total < 40.0:
            # 规则评估得分过低，触发AI评估兜底
            try:
                ai_evaluation = await self._ai_evaluate_dimensions(
                    content, section_title, word_score, repetition_score,
                    cancel_event=cancel_event,
                )
            except Exception as e:
                logger.warning("AI质量评估失败 [%s]: %s", section_title, e)
                ai_evaluation = {
                    "structure_score": rule_structure,
                    "professional_score": rule_professional,
                    "relevance_score": rule_relevance,
                    "issues": [],
                    "suggestions": [],
                }
        else:
            ai_evaluation = {
                "structure_score": rule_structure,
                "professional_score": rule_professional,
                "relevance_score": rule_relevance,
                "issues": [],
                "suggestions": [],
            }

        # 4. 计算综合得分（加权平均）
        dimensions = {
            "word_count_score": word_score,
            "structure_score": ai_evaluation["structure_score"],
            "professional_score": ai_evaluation["professional_score"],
            "repetition_score": repetition_score,
            "relevance_score": ai_evaluation["relevance_score"],
        }

        # 权重配置
        weights = {
            "word_count_score": 0.20,  # 字数占20%
            "structure_score": 0.25,  # 结构占25%
            "professional_score": 0.25,  # 专业度占25%
            "repetition_score": 0.15,  # 重复度占15%
            "relevance_score": 0.15,  # 相关性占15%
        }

        total_score = sum(dimensions[dim] * weights[dim] for dim in dimensions)
        total_score = round(min(100, max(0, total_score)), 1)

        # 确定质量等级
        if total_score >= 90:
            grade = self.QUALITY_EXCELLENT
        elif total_score >= 70:
            grade = self.QUALITY_GOOD
        elif total_score >= 50:
            grade = self.QUALITY_FAIR
        else:
            grade = self.QUALITY_POOR

        # 合并问题和改进建议
        all_issues = list(ai_evaluation.get("issues", []))
        all_suggestions = list(ai_evaluation.get("suggestions", []))

        # 添加字数相关问题
        if word_score < 60:
            all_issues.append(f"字数不足（当前{actual_words}字，目标约{target_words or '未知'}字）")
            all_suggestions.append("需要扩充内容细节，补充具体技术参数和管理措施")

        # 添加重复度问题
        if repetition_score < 60:
            all_issues.append("存在较多内容重复")
            all_suggestions.append("删除重复段落，使用不同角度的表述")

        return {
            "score": total_score,
            "grade": grade,
            "dimensions": dimensions,
            "issues": all_issues[:5],  # 最多返回5个问题
            "suggestions": all_suggestions[:5],  # 最多返回5条建议
        }

    async def _ai_evaluate_dimensions(
        self,
        content: str,
        section_title: str,
        word_score: float,
        repetition_score: float,
        cancel_event: "asyncio.Event | None" = None,
    ) -> dict:
        """使用AI评估内容维度（结构、专业性、相关性）"""

        # 截取内容前5000字进行评估（避免token超限）
        content_sample = content[:5000] if len(content) > 5000 else content

        # ENHANCE 2026-10-18（审计遗留 D2 批次 2 收编）：user 侧收编为
        # QUALITY_EVAL_USER（system 侧早已于 BUG-FIX-PROMPT-E4 收编）。
        # 正文为 AI 生成的标书内容（下游数据，非原始外部输入），做元标记中和。
        prompt = get_prompt(
            "QUALITY_EVAL_USER",
            section_title=section_title,
            content_sample=content_sample,
            sanitize=True,
        ) or f"""请评估以下标书章节的内容质量，从以下几个维度打分（0-100分）：

【章节标题】
{section_title}

【评估维度】
1. 结构完整性（structure_score）：是否有清晰的段落划分、逻辑层次是否分明
2. 专业性（professional_score）：是否使用建筑行业术语、是否符合标书专业风格
3. 相关性（relevance_score）：内容是否紧扣章节主题、是否有跑题或泛泛而谈

【评估要求】
- 结构完整性：检查是否有引言、主体、结论；段落之间是否有逻辑过渡；是否使用了合适的小标题
- 专业性：检查是否使用专业术语（如"混凝土浇筑"、"土方开挖"等）；是否符合工程技术规范
- 相关性：检查内容是否围绕章节主题展开；是否有无关内容

【输出格式】
请直接返回JSON对象，不要包含其他文字：
{{
    "structure_score": 85.5,
    "professional_score": 78.0,
    "relevance_score": 90.0,
    "issues": ["问题1", "问题2"],
    "suggestions": ["建议1", "建议2"]
}}

【正文内容】
{content_sample}
"""

        messages = [
            # BUG-FIX-PROMPT-E4（inline 系统提示词未注册）：原以字面量形式
            # inline 存在，管理页无法查看/编辑/覆盖。现统一走 get_prompt()
            # （DB 优先 → 注册表 → 硬编码回退）。
            AIMessage(
                role="system",
                content=(
                    get_prompt("QUALITY_EVAL_SYSTEM")
                    or "你是一个标书内容质量评估专家，擅长客观评估标书章节的质量。"
                ),
            ),
            AIMessage(role="user", content=prompt),
        ]

        response = await with_timeout(
            self.provider.chat(messages, temperature=0.3, cancel_event=cancel_event),
            timeout=60,
            error_msg="质量评估AI调用超时",
        )

        # BUG 修复：response 可能为 None（AI 返回空），需安全处理
        if not response:
            logger.warning("AI质量评估返回空响应，使用默认分数")
            return {
                "structure_score": 70.0,
                "professional_score": 70.0,
                "relevance_score": 70.0,
                "issues": [],
                "suggestions": [],
            }

        parsed = extract_json_from_text(response)

        if isinstance(parsed, dict):
            # BUG 修复：float() 转换需容错，AI 可能返回非数字字符串
            def _safe_float(val, default=70.0):
                try:
                    return float(val)
                except (ValueError, TypeError):
                    return default

            return {
                "structure_score": _safe_float(parsed.get("structure_score", 70)),
                "professional_score": _safe_float(parsed.get("professional_score", 70)),
                "relevance_score": _safe_float(parsed.get("relevance_score", 70)),
                "issues": parsed.get("issues", []) or [],
                "suggestions": parsed.get("suggestions", []) or [],
            }

        # 解析失败时返回默认值
        return {
            "structure_score": 70.0,
            "professional_score": 70.0,
            "relevance_score": 70.0,
            "issues": [],
            "suggestions": [],
        }

    def _calculate_repetition_score(self, content: str) -> float:
        """计算重复度得分（基于段落重复检测）

        Returns:
            float: 重复度得分（越高越好，100表示无重复）
        """
        if not content:
            return 0.0

        # 按段落分割
        paragraphs = [p.strip() for p in re.split(r"\n\s*\n", content) if len(p.strip()) > 50]

        if len(paragraphs) <= 1:
            return 80.0  # 段落太少无法判断，给中等分数

        # 检测重复段落
        seen = {}
        duplicate_count = 0

        for i, para in enumerate(paragraphs):
            # 简化比较：取前100字符作为指纹
            fingerprint = para[:100].lower().strip()
            if fingerprint in seen:
                duplicate_count += 1
            else:
                seen[fingerprint] = i

        # 计算重复率
        duplication_rate = duplicate_count / len(paragraphs) if paragraphs else 0
        score = max(0, 100 - duplication_rate * 200)  # 重复率50%得0分

        return round(score, 1)

    def _rule_structure_score(self, content: str) -> float:
        """基于规则的結構评分（降级方案）"""
        if not content:
            return 0.0

        score = 50.0  # 基础分

        # 检查段落数量
        paragraphs = [p for p in content.split("\n") if p.strip()]
        if len(paragraphs) >= 5:
            score += 10
        if len(paragraphs) >= 10:
            score += 10

        # 检查是否有小标题特征（数字编号、顿号等）
        has_headings = bool(re.search(r"\d+[、.\s]", content))
        if has_headings:
            score += 10

        # 检查长度
        if len(content) > 1000:
            score += 10
        if len(content) > 3000:
            score += 10

        return min(100, score)

    def _rule_professional_score(self, content: str) -> float:
        """基于规则的专业性评分（降级方案）"""
        if not content:
            return 0.0

        # 建筑行业关键词密度
        professional_keywords = [
            "施工",
            "工程",
            "技术",
            "质量",
            "安全",
            "管理",
            "标准",
            "规范",
            "验收",
            "监理",
            "图纸",
            "材料",
            "设备",
            "工艺",
        ]

        keyword_count = sum(1 for kw in professional_keywords if kw in content)
        score = min(100, 50 + keyword_count * 5)

        return score


async def evaluate_sections_quality(
    provider: Any,
    results: list[dict],
) -> list[dict]:
    """批量评估章节内容质量

    Args:
        provider: AI提供商实例
        results: 生成结果列表 [{id, title, content, status, ...}]

    Returns:
        list[dict]: 带质量评估的结果列表
    """
    evaluator = ContentQualityEvaluator(provider)
    evaluated_results = []

    for result in results:
        if result.get("status") != "completed" or not result.get("content"):
            evaluated_results.append(
                {
                    **result,
                    "quality": {
                        "score": 0,
                        "grade": "skipped",
                        "dimensions": {},
                        "issues": [],
                        "suggestions": [],
                    },
                }
            )
            continue

        quality = await evaluator.evaluate_content(
            content=result.get("content", ""),
            section_title=result.get("title", ""),
            target_words=result.get("target_words"),
            section_level=result.get("level", 3),
        )

        evaluated_results.append(
            {
                **result,
                "quality": quality,
            }
        )

    return evaluated_results


def score_section_quality_local(
    content: str,
    title: str = "",
    target_words: int | None = None,
) -> dict:
    """基于可复现规则的章节质量打分（**不调用 AI**）。

    ENHANCE 2026-09-18（消除幽灵功能）：
    `evaluate_sections_quality` / `ContentQualityEvaluator` 是 AI 版评估器，
    但全仓**零调用方**；而 `routers/sse/sse_content.py` 的 `quality_summary` 事件
    依赖生成结果里的 `quality.score`，该字段恒为 None（`generate_sections` 写死
    `"quality": None`）→ 事件永不发出，「质量检查」成了前端看不到的幽灵功能。

    接线 AI 版会给每个章节额外增加一次 AI 调用（N 倍成本与耗时），对已经跑几十章
    的正文生成不划算。这里提供**确定性本地打分**：零成本、可复现、口径透明，
    足以支撑前端展示质量分布并提示明显问题（字数严重不足、大段重复、通篇无数据）。

    维度与权重：字数达成 0.35 / 段落结构 0.20 / 具体性 0.15 / 重复度 0.20 / 标记使用 0.10
    """
    text = content or ""
    # FIX 2026-10-08（遗留7）：先剔除图表围栏载荷，再进入后续度量。
    # 否则 mermaid/chart-json 代码体会计入字数/段落/重复句/具体性四大维度。
    _text_no_charts = _strip_chart_payloads(text)
    # 去 HTML 标签与 Markdown 修饰符，保留正文语义
    plain = re.sub(r"<[^>]+>", "", _text_no_charts)
    plain = re.sub(r"[#*`>|]", " ", plain)
    char_count = len(re.sub(r"\s", "", plain))

    # 1) 字数达成度
    if target_words and target_words > 0:
        ratio = char_count / float(target_words)
        if ratio < 0.5:
            wc_score = ratio * 140.0
        elif ratio < 0.85:
            wc_score = 70.0 + (ratio - 0.5) * 85.7
        elif ratio <= 1.3:
            wc_score = 100.0
        else:
            wc_score = max(60.0, 100.0 - (ratio - 1.3) * 40.0)
    else:
        wc_score = 100.0 if char_count >= 200 else char_count / 2.0
    wc_score = min(100.0, max(0.0, wc_score))

    # 2) 段落结构：段落数（8 段以上视为结构完整）
    paragraphs = [p for p in re.split(r"\n\s*\n|\n", plain) if p.strip()]
    struct_score = min(100.0, len(paragraphs) / 8.0 * 100.0)

    # 3) 具体性：带单位的具体数值密度
    spec_hits = len(
        re.findall(
            r"\d+(?:\.\d+)?\s*(?:%|mm|cm|km|kg|㎡|m2|m3|万元|元|天|年|月|日|小时|分钟|人|台|套)",
            plain,
        )
    )
    spec_score = min(100.0, spec_hits / 12.0 * 100.0)

    # 4) 重复度惩罚：重复句子占比
    sentences = [s.strip() for s in re.split(r"[。；;\n]", plain) if len(s.strip()) >= 12]
    dup_ratio = 0.0
    if sentences:
        dup_ratio = 1.0 - (len(set(sentences)) / float(len(sentences)))
    repeat_score = max(0.0, 100.0 - dup_ratio * 200.0)

    # 5) 标记使用：图表标记 + 图表围栏 + 列表项
    # FIX 2026-10-08（遗留7）：提示词已改为 mermaid/chart-json 围栏产出，
    # 旧实现只数 `<<CHART:` → 围栏式章节本维度恒为 0 分。
    marker_hits = (
        len(re.findall(r"<<CHART:", text))
        + _count_chart_fences(text)
        + len(re.findall(r"^\s*(?:[-*]|\d+[.、])", plain, re.M))
    )
    marker_score = min(100.0, marker_hits / 6.0 * 100.0)

    total = (
        wc_score * 0.35
        + struct_score * 0.20
        + spec_score * 0.15
        + repeat_score * 0.20
        + marker_score * 0.10
    )
    score = round(total, 1)
    if score >= 85:
        grade = "A"
    elif score >= 70:
        grade = "B"
    elif score >= 55:
        grade = "C"
    else:
        grade = "D"

    issues: list[str] = []
    if wc_score < 70:
        issues.append(
            f"字数未达标（{char_count} 字 / 目标 {target_words if target_words else '未设置'} 字）"
        )
    if dup_ratio > 0.15:
        issues.append(f"存在重复表述（重复句占比 {dup_ratio:.0%}）")
    if spec_score < 40:
        issues.append("缺少带单位的具体数值，建议补充可核查的规格/参数")

    return {
        "score": score,
        "grade": grade,
        "dimensions": {
            "word_count": round(wc_score, 1),
            "structure": round(struct_score, 1),
            "specificity": round(spec_score, 1),
            "repetition": round(repeat_score, 1),
            "markers": round(marker_score, 1),
        },
        "issues": issues,
        "suggestions": [],
        "char_count": char_count,
        "evaluator": "local",
    }


_OUTLINE_REVIEW_LABELS = {
    "__SCORING_POINTS__": "评分点列表",
    "__OUTLINE__": "当前目录",
    "__SUMMARY__": "项目概述",
    "__INDUSTRY_PROFILE__": "行业背景（如房建、市政、修缮改造等）",
}


def _build_outline_review_texts(
    scoring_points: str,
    outline: str,
    summary: str = "",
    industry_profile: str = "",
) -> tuple[str, str]:
    """拼装目录审核的 system / user 提示词。

    BUG-FIX 2026-09-14（中）留存：模板里的 __SUMMARY__ / __INDUSTRY_PROFILE__
    两个占位符若既不在 mapping 也不在 labels 里，_fill_template 会兜底保留原样，
    发给模型的提示词出现字面量占位符，模型无法执行项目契合度 / 行业特征校验。
    这里统一在 labels 里登记，供「初次审核」与「修复后复检」两条路径共用。
    """
    review_sys = get_prompt("OUTLINE_REVIEW_SYSTEM")
    review_user = get_prompt("OUTLINE_REVIEW_USER")
    # SECURITY 2026-10-06：审核链路注入的评分点/目录/概述均为外部衍生数据，开启
    # sanitize=True 与全平台高注入风险链路（workflows_analysis）口径对齐，做
    # 元标记中和纵深防御；默认脱敏保持关闭以免误伤结构化文本（如需再开 desensitize）。
    review_sys_text = _fill_template(review_sys, {}, labels=_OUTLINE_REVIEW_LABELS, sanitize=True)
    review_user_text = _fill_template(
        review_user,
        {
            "__SCORING_POINTS__": scoring_points,
            "__OUTLINE__": outline,
            "__SUMMARY__": (summary or "")[:4000],
            "__INDUSTRY_PROFILE__": industry_profile or "通用工程",
        },
        labels=_OUTLINE_REVIEW_LABELS,
        sanitize=True,
    )
    return review_sys_text, review_user_text


async def review_outline(
    provider: Any,
    outline: Any,
    scoring_points: str,
    summary: str = "",
    industry_profile: str = "",
) -> dict:
    """单轮目录审核（不自动修复）。

    ENHANCE 2026-09-15：修复 `POST /projects/{id}/sections/review` 的必崩缺陷。
    路由里写的是 `from ..services.ai.workflows import review_outline`，但
    services 层从来没有定义过这个函数、workflows facade 也没有 re-export：
    请求进来必抛 ImportError → HTTP 500，前端「AI 审核目录」按钮从未真正可用
    （同类缺陷：/review-with-autofix 因 facade 未导出而同样 500）。

    Args:
        provider: AI 提供商实例
        outline: 目录树（list / dict）或其 JSON 字符串
        scoring_points: 评分点 JSON 字符串
        summary: 项目概述
        industry_profile: 行业背景

    Returns:
        dict: 至少含 passed / issues / suggestions 的审核结果。AI 未返回有效
        JSON 时返回 passed=False 的兜底结构而不是 None，调用方可以直接读
        `result["passed"]`，无需再判空判类型。
    """
    outline_text = outline if isinstance(outline, str) else json.dumps(outline, ensure_ascii=False, indent=2)
    review_sys_text, review_user_text = _build_outline_review_texts(
        scoring_points, outline_text, summary, industry_profile
    )
    messages = [
        AIMessage(role="system", content=review_sys_text),
        AIMessage(role="user", content=review_user_text),
    ]
    try:
        result = await collect_json_response(provider, messages, max_retries=1)
    except Exception as e:
        logger.warning("目录审核 AI 调用失败: %s", e)
        result = None

    if not isinstance(result, dict) or not result:
        logger.warning("目录审核未得到有效的 JSON 结果，返回未通过")
        return {
            "passed": False,
            "confidence": 0.0,
            "issues": ["AI 审核未返回有效的 JSON 结果，请重试"],
            "suggestions": [],
            "fixed_outline": None,
            "repaired": False,
            "repair_rounds": 0,
        }

    result.setdefault("issues", [])
    result.setdefault("suggestions", [])
    result.setdefault("fixed_outline", None)
    result.setdefault("repaired", False)
    result.setdefault("repair_rounds", 0)
    # 关键兜底：模型漏写 passed 时按「未通过」处理（历史实现默认 True），
    # 否则外层会误判审核通过、跳过自修复。
    result.setdefault("passed", False)
    return result


async def review_outline_with_autofix(
    provider: Any,
    scoring_points: str,
    outline: str,
    max_repair_rounds: int = 3,
    summary: str = "",
    industry_profile: str = "",
) -> dict:
    """目录审核 + 自修复机制（参考易标模式）。

    工作流程：
    1. 正常审核目录
    2. 若审核未通过（passed=false），尝试 AI 自修复
    3. 最多重试 max_repair_rounds 轮
    4. 若最终仍无法通过，返回当前最佳结果及 issue 列表

    Args:
        provider: AI 提供商实例
        scoring_points: 评分点 JSON
        outline: 当前目录 JSON 字符串
        max_repair_rounds: 最大修复轮次
    Returns:
        dict: 含 passed, confidence, issues, suggestions, fixed_outline
    """
    outline_text = outline if isinstance(outline, str) else json.dumps(outline, ensure_ascii=False, indent=2)
    review_sys_text, _ = _build_outline_review_texts(scoring_points, outline_text, summary, industry_profile)

    # 首轮审核复用 review_outline（单轮审核），避免两份重复实现各自漂移
    result = await review_outline(provider, outline_text, scoring_points, summary, industry_profile)
    current_outline = outline_text

    # 自修复机制：若审核未通过，尝试自动修复
    if isinstance(result, dict) and not result.get("passed", True):
        for round_idx in range(max_repair_rounds):
            logger.info("目录审核未通过，尝试第 %d/%d 轮自修复", round_idx + 1, max_repair_rounds)

            repair_prompt = (
                f"你是一个标书目录修复专家。以下目录审核未通过，请根据审核意见修复目录。\n\n"
                f"【审核结果】\n"
                f"issues: {json.dumps(result.get('issues', []), ensure_ascii=False)}\n"
                f"suggestions: {json.dumps(result.get('suggestions', []), ensure_ascii=False)}\n\n"
                f"【当前目录】\n{current_outline}\n\n"
                f"【评分点列表】\n{scoring_points}\n\n"
                f"请直接输出修复后的完整目录 JSON 数组，保持三级结构不变，"
                f"仅针对审核意见中的问题进行修正。严禁附加解释文字。"
            )

            try:
                repair_messages = [
                    AIMessage(
                        role="system",
                        content="你是一个标书目录修复专家，擅长根据审核意见修复目录结构。请直接输出修复后的 JSON 数组。",
                    ),
                    AIMessage(role="user", content=repair_prompt),
                ]
                repaired_content = await with_timeout(
                    provider.chat(repair_messages, temperature=0.3), timeout=120
                )
                parsed = extract_json_from_text(repaired_content)
                if parsed and isinstance(parsed, list):
                    fixed_outline = json.dumps(parsed, ensure_ascii=False, indent=2)

                    # BUG-FIX 2026-09-15（中）：复检提示词原先手工拼模板，labels
                    # 只登记了 2 个占位符，__SUMMARY__ / __INDUSTRY_PROFILE__ 会以
                    # 字面量形式发给模型；统一走 _build_outline_review_texts。
                    _, review_user_text = _build_outline_review_texts(
                        scoring_points, fixed_outline, summary, industry_profile
                    )
                    review_messages = [
                        AIMessage(role="system", content=review_sys_text),
                        AIMessage(role="user", content=review_user_text),
                    ]
                    recheck = await collect_json_response(provider, review_messages, max_retries=2)

                    if isinstance(recheck, dict) and recheck.get("passed", False):
                        recheck["fixed_outline"] = parsed
                        recheck["repaired"] = True
                        recheck["repair_rounds"] = round_idx + 1
                        logger.info("目录自修复成功，共 %d 轮", round_idx + 1)
                        return recheck

                    result = recheck or result
                    current_outline = fixed_outline
            except Exception as e:
                logger.warning("目录自修复第 %d 轮失败: %s", round_idx + 1, e)

        logger.warning("目录自修复 %d 轮后仍未通过，返回当前最佳结果", max_repair_rounds)

    if isinstance(result, dict):
        result.setdefault("fixed_outline", None)
        result.setdefault("repaired", False)
        result.setdefault("repair_rounds", 0)

    return result


def snapshot_section_content(sections: list[dict]) -> dict[str, dict]:
    """快照已有正文内容，用于目录编辑后恢复。

    对每个有内容的章节，以 title + level + parent_title 为键保存快照，
    用于后续 ID 映射追踪节点变化。

    Args:
        sections: 章节列表，每项含 id, title, level, parent_id, content, status
    Returns:
        dict: {key: {content, status, section_type, target_words}}
    """
    snapshot = {}
    for sec in sections:
        content = sec.get("content") or ""
        if not content.strip():
            continue
        title = sec.get("title", "")
        level = sec.get("level", 1)
        parent_title = ""
        parent_id = sec.get("parent_id")
        if parent_id:
            for s in sections:
                if s.get("id") == parent_id:
                    parent_title = s.get("title", "")
                    break
        key = f"{title}|{level}|{parent_title}"
        snapshot[key] = {
            "content": content,
            "status": sec.get("status", "completed"),
            "section_type": sec.get("section_type", "text"),
            "target_words": sec.get("target_words"),
        }
    return snapshot


def build_content_mapping(
    old_sections: list[dict],
    new_sections: list[dict],
) -> dict[int, dict]:
    """构建新旧章节 ID 映射，追踪节点变化。

    通过标题相似度匹配新旧章节，将旧章节的内容映射到新章节。

    Args:
        old_sections: 旧章节列表
        new_sections: 新章节列表
    Returns:
        dict: {new_id: {content, status, section_type, target_words}}
    """
    old_snapshot = snapshot_section_content(old_sections)
    mapping = {}

    for new_sec in new_sections:
        new_id = new_sec.get("id")
        new_title = new_sec.get("title", "")
        new_level = new_sec.get("level", 1)
        parent_id = new_sec.get("parent_id")
        new_parent_title = ""
        if parent_id:
            for s in new_sections:
                if s.get("id") == parent_id:
                    new_parent_title = s.get("title", "")
                    break

        exact_key = f"{new_title}|{new_level}|{new_parent_title}"
        if exact_key in old_snapshot:
            mapping[new_id] = old_snapshot[exact_key]
            continue

        best_match = None
        best_score = 0.0
        for key, data in old_snapshot.items():
            parts = key.split("|")
            old_title = parts[0] if len(parts) > 0 else ""
            try:
                old_level = int(parts[1]) if len(parts) > 1 else 0
            except (ValueError, IndexError):
                continue
            if old_level != new_level:
                continue
            score = _title_similarity(old_title, new_title)
            if score > best_score and score >= 0.6:
                best_score = score
                best_match = (key, data)

        if best_match:
            mapping[new_id] = best_match[1]

    return mapping


def restore_mapped_content(
    new_sections: list[dict],
    content_mapping: dict[int, dict],
) -> list[dict]:
    """根据映射恢复新章节的正文内容。

    Args:
        new_sections: 新章节列表
        content_mapping: build_content_mapping 返回的映射
    Returns:
        list[dict]: 恢复内容后的章节列表
    """
    restored = []
    for sec in new_sections:
        new_id = sec.get("id")
        if new_id in content_mapping:
            sec["content"] = content_mapping[new_id]["content"]
            sec["status"] = content_mapping[new_id]["status"]
            if sec.get("section_type") == "text":
                sec["section_type"] = content_mapping[new_id].get("section_type", "text")
        restored.append(sec)
    return restored
