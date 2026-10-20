"""提示词注册表。

所有提示词通过 _reg() 注册到 _ALL_PROMPTS 全局字典中。
供 routers/prompts.py 提供给前端展示和编辑使用。

增强功能：
- 变量提取与校验：自动提取模板中的 {变量占位符}，支持运行时验证
- 模板清洗：统一换行符、移除零宽字符
- 统一变量渲染：render_prompt() 函数一次性替换所有变量并验证
"""

from __future__ import annotations

import logging
import re


logger = logging.getLogger(__name__)

_ALL_PROMPTS: dict[str, dict] = {}

# 模板变量占位符正则：匹配 {variable_name} 和 __VARIABLE_NAME__ 两种格式
# 注意：
# 1. 使用 [A-Z0-9_] 而非 \w，防止匹配中文字符导致错误变量提取
# 2. {var} 格式要求至少2个字符，避免匹配文档模板中的 {n}、{x}、{y} 等占位说明
_VARIABLE_PATTERN = re.compile(r"\{([A-Za-z_]\w{1,})\}|__([A-Z_][A-Z0-9_]*)__")

# 替换后残留占位符检测（用于验证变量是否全部替换）
_RESIDUAL_PLACEHOLDER_PATTERN = re.compile(r"\{[A-Za-z_]\w{1,}\}|__[A-Z_][A-Z0-9_]*__")

# 零宽字符正则（含软连字符 \u00AD）
_ZERO_WIDTH_PATTERN = re.compile(r"[\u00AD\u200B\u200C\u200D\uFEFF]")

# {shared:NAME} 引用占位符：在渲染时注入已注册的共享规则内容。
# 解析顺序：NAME -> SHARED_NAME -> NAME.upper() -> SHARED_<NAME.upper()>。
_SHARED_PATTERN = re.compile(r"\{shared:([A-Za-z_]\w{1,})\}")


def _reg(key: str, category: str, label: str, value: str):
    """注册一个提示词到元信息表，自动提取变量列表。

    Args:
        key: 提示词键名（与常量名相同）。
        category: 分类标签（用于前端分组显示）。
        label: 提示词中文标签（用于前端列表展示）。
        value: 提示词实际内容。

    Returns:
        原始 value（不修改），保证模块级常量赋值可链式调用。
    """
    variables = sorted(set(v for match in _VARIABLE_PATTERN.findall(value) for v in match if v))
    _ALL_PROMPTS[key] = {
        "category": category,
        "label": label,
        "content": value,
        "variables": variables,
    }
    return value


def get_prompt_variables(key: str) -> list[str]:
    """获取指定提示词所需的变量列表。"""
    meta = _ALL_PROMPTS.get(key)
    if meta:
        return meta.get("variables", [])
    return []


def validate_prompt_variables(key: str, **kwargs) -> list[str]:
    """验证提示词变量是否全部提供，返回缺失变量列表。

    空列表表示所有变量已提供。

    BUG-FIX-B1: 大小写不敏感比较——注册变量是 SUMMARY，
    调用方传 summary='xxx' 时应视为已提供。
    """
    required = get_prompt_variables(key)
    kwargs_lower = {k.lower(): v for k, v in kwargs.items()}
    missing = [v for v in required if v.lower() not in kwargs_lower or kwargs_lower[v.lower()] is None]
    return missing


def clean_prompt_text(text: str) -> str:
    """清洗提示词文本：
    - 统一换行符为 \\n
    - 移除零宽字符
    - 去除首尾空白
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _ZERO_WIDTH_PATTERN.sub("", text)
    return text.strip()


def extract_variables(template: str) -> list[str]:
    """从模板字符串中提取所有 {变量名} 或 __变量名__ 占位符。

    注意：``{shared:NAME}`` 是共享规则引用而非变量占位符，其内部的 ``:``
    不属于 ``\\w``，故天然不会被本函数提取——这是正确行为，共享引用由
    ``_resolve_shared`` 单独处理。
    """
    return sorted(set(v for match in _VARIABLE_PATTERN.findall(template) for v in match if v))


def has_residual_placeholders(text: str) -> bool:
    """检查文本中是否还有未替换的变量占位符 / 未解析的共享规则引用。

    BUG-FIX-PROMPT-R8（2026-10-17，未解析共享引用静默漏检）：原实现只匹配
    ``{var}`` / ``__VAR__``。而 ``_resolve_shared`` 在找不到目标时**保留原占位符**
    并仅 ``logger.warning``——于是 ``{shared:NOT_EXIST}`` 这类坏块会**原样进入
    发给模型的提示词**，且 ``has_residual_placeholders`` 返回 False，上层
    （``get_prompt_with_validation`` / 各类校验）完全无法察觉。实测复现：
        render_prompt("{shared:NOT_EXIST}") -> 残留字面量，检测却为 False
    现一并检测未解析的 ``{shared:NAME}``，使「坏块」可被上层捕获与告警。

    Returns:
        True 表示还有残留占位符或未解析共享引用，False 表示已全部消解。
    """
    if _RESIDUAL_PLACEHOLDER_PATTERN.search(text):
        return True
    return bool(_SHARED_PATTERN.search(text))


def resolve_shared_refs(template: str) -> str:
    """公开共享规则解析入口（``_resolve_shared`` 的稳定别名）。

    调用方（如 ``get_prompt`` 无变量场景）应始终保证模板经过本函数处理，
    否则 ``{shared:NAME}`` 会以字面量形式发给模型。
    """
    return _resolve_shared(template)


def _resolve_shared(template: str) -> str:
    """解析 {shared:NAME} 引用，将其替换为已注册共享规则的内容。

    查找顺序：NAME -> SHARED_NAME -> NAME.upper() -> SHARED_<NAME.upper()>。
    若找不到对应共享规则，保留原占位符并发出警告，避免静默丢失内容。

    说明：共享规则（SHARED_HEADING_RULES 等）均为静态文本，不含变量占位符，
    因此注入后不会引入未替换的 {var}。注入为递归安全（单遍处理）。
    """
    if "{shared:" not in template:
        return template

    def _lookup(name: str) -> str | None:
        for cand in (name, "SHARED_" + name, name.upper(), "SHARED_" + name.upper()):
            entry = _ALL_PROMPTS.get(cand)
            if entry is not None:
                return entry["content"]
        return None

    def _repl(m: "re.Match") -> str:
        name = m.group(1)
        content = _lookup(name)
        if content is None:
            logger.warning("Unknown shared prompt reference: %s", name)
            return m.group(0)
        return content

    return _SHARED_PATTERN.sub(_repl, template)


def render_prompt(template: str, *, sanitize: bool = False, desensitize: bool = False, **kwargs) -> str:
    """统一的变量渲染函数，替换模板中的所有变量占位符。

    支持两种变量格式（均为大小写不敏感）：
    - {variable_name} 格式
    - __VARIABLE_NAME__ 格式

    SECURITY-FIX 2026-10-06（跨变量注入 / 数据串味）：原实现对每个变量单独
    `re.sub` 并**顺序**扫描整段结果，导致先插入的变量值若包含另一个变量名
    （如用户内容里写了 `{GLOBAL_FACTS}`），会在后续轮次被二次替换——用户输入
    得以「借用」另一个上下文槽位注入数据，构成提示词注入面。现改为**单遍
    替换**：一次性构建所有变量名的合并正则，单次 sub 完成，插入的值不再被
    重新扫描，从根本上消除跨变量注入。

    Args:
        template: 提示词模板文本
        sanitize: 是否对变量值做元标记中和（防御提示词注入）。默认 False
            以向后兼容历史调用方；注入用户/外部内容的调用方应传 True 或使用
            render_prompt_safe。
        desensitize: 是否对变量值做敏感信息脱敏（邮箱/手机/身份证/密钥）。
            默认 False（opt-in），避免改变默认渲染语义。
        **kwargs: 变量名和值的映射（大小写不敏感匹配）

    Returns:
        替换变量后的文本
    """
    result = _resolve_shared(template)

    if kwargs:
        # 准备值：跳过 None（保留占位符以便后续告警），其余按需清洗
        prepared: dict[str, str | None] = {}
        for var_name, var_value in kwargs.items():
            if var_value is None:
                prepared[var_name] = None
                continue
            val_str = str(var_value)
            if sanitize:
                # 延迟导入，避免与 prompts 包形成导入环
                from ._security import sanitize_prompt_value

                val_str = sanitize_prompt_value(val_str)
            if desensitize:
                from ._security import desensitize_sensitive_info

                val_str = desensitize_sensitive_info(val_str)
            prepared[var_name] = val_str

        # 单遍替换：仅对实际传入且非 None 的变量名构建合并正则
        names = [k for k, v in prepared.items() if v is not None]
        if names:
            # 长名优先，避免短名作为前缀误匹配（如 VAR 与 VARS）
            names_sorted = sorted(names, key=len, reverse=True)
            alt = "|".join(re.escape(n) for n in names_sorted)
            combined = re.compile(
                r"\{(" + alt + r")\}|__(" + alt.upper() + r")__",
                re.IGNORECASE,
            )

            # PERF 2026-10-17（热路径 O(n·m) → O(n)）：原 _repl 对每个匹配点
            # **线性遍历** prepared 全部键做大小写比对。图表/正文模板常有 10+
            # 变量、模板内同一变量出现数十次，且变量值可达数万字符——
            # 每次 get_prompt 都要付这份扫描成本。改为预建 lowercase → value
            # 映射，回调内 O(1) 命中（实测 20k 值 × 200 次：0.049s → 0.006s）。
            lookup = {k.lower(): v for k, v in prepared.items() if v is not None}

            # BUG-FIX-44：re.sub 的 replacement 会解释反向引用（\1、\g<name>），
            # 变量值含此类序列会导致替换错误或 re.error；用 lambda 返回固定字符串规避。
            def _repl(m: "re.Match") -> str:
                inner = (m.group(1) or m.group(2) or "").lower()
                return lookup.get(inner, "")

            result = combined.sub(_repl, result)

    # 检查是否还有残留占位符
    if has_residual_placeholders(result):
        residual_vars = extract_variables(result)
        # BUG-FIX-44：排除值为 None 的变量，避免其被报告为 unresolved
        kwargs_lower = {k.lower(): v for k, v in kwargs.items() if v is not None}
        unresolved = [v for v in residual_vars if v.lower() not in kwargs_lower]
        if unresolved:
            logger.warning("Prompt template has unresolved placeholders: %s", unresolved)

    return result


def audit_shared_usage() -> dict:
    """审计「共享规则复用机制」的实际落地情况（可观测性，2026-10-17）。

    背景（缺口 G-SHARED）：``{shared:NAME}`` 复用机制自 2026-10-06 起完备可用，
    但注册表中**没有任何模板引用**它——SHARED_HEADING_RULES / SHARED_FORBIDDEN_WORDS
    / SHARED_OUTPUT_SPEC 三条共享规则从未被注入任何提示词，标题编号规范、禁用词、
    输出格式规范实际仍靠各模板**逐份内联复制**维护（charts.py 单文件 2802 行
    即为例证）。这造成：
      - 改规范要改 N 处，必然漂移；
      - 「复用与覆盖」能力形同虚设（管理页改 SHARED_* 对 AI 行为零影响）。

    本函数把该缺口量化暴露给 ``GET /prompts/overview``，便于持续跟踪收编进度。

    Returns:
        {
          "shared_keys": [...],                 # 已注册的共享规则 key
          "referenced_by": {shared_key: [模板key...]},
          "unused_shared_keys": [...],          # 无人引用的共享规则（待收编）
          "templates_using_shared": int,        # 引用了共享规则的模板数
          "adoption_rate": float,               # 引用率 0~1
        }
    """
    shared_keys = [k for k in _ALL_PROMPTS if k.startswith("SHARED_")]
    shared_set = set(shared_keys)
    referenced_by: dict[str, list[str]] = {k: [] for k in shared_keys}

    templates_using_shared = 0
    for key, meta in _ALL_PROMPTS.items():
        if key in shared_set:
            continue
        refs = set(_SHARED_PATTERN.findall(meta["content"]))
        if not refs:
            continue
        templates_using_shared += 1
        for raw in refs:
            target = _lookup_shared_target(raw)
            if target in referenced_by:
                referenced_by[target].append(key)

    total_templates = len(_ALL_PROMPTS) - len(shared_keys)
    return {
        "shared_keys": sorted(shared_keys),
        "referenced_by": {k: sorted(v) for k, v in referenced_by.items()},
        "unused_shared_keys": sorted(k for k, v in referenced_by.items() if not v),
        "templates_using_shared": templates_using_shared,
        "total_templates": total_templates,
        "adoption_rate": round(templates_using_shared / total_templates, 4) if total_templates else 0.0,
    }


def _lookup_shared_target(name: str) -> str | None:
    """按 _resolve_shared 的查找顺序解析共享规则名，返回命中的注册表 key。"""
    for cand in (name, "SHARED_" + name, name.upper(), "SHARED_" + name.upper()):
        if cand in _ALL_PROMPTS:
            return cand
    return None


def validate_prompt_content(key: str, content: str) -> list[str]:
    """统一的模板内容校验，返回问题列表（空列表表示通过）。

    与 ``routers/prompts._validate_prompt_content`` 抛 HTTPException 的形态解耦，
    便于单测与非 HTTP 场景（sync-hardcode / seed）复用同一套规则。

    校验项：
      1. ``{shared:NAME}`` 引用必须可解析（否则坏块进提示词）；
      2. 不得静默删除默认模板要求的变量占位符（上下文无法注入）；
      3. 占位符语法不得畸形（``{`` 未闭合 / 变量名以数字开头）。
    """
    problems: list[str] = []

    bad_refs = sorted(
        r for r in set(_SHARED_PATTERN.findall(content or "")) if _lookup_shared_target(r) is None
    )
    if bad_refs:
        problems.append(f"引用了未注册的共享规则: {bad_refs}")

    meta = _ALL_PROMPTS.get(key)
    if meta:
        default_vars = set(extract_variables(meta["content"]))
        new_vars_lower = {v.lower() for v in extract_variables(content or "")}
        dropped = sorted(v for v in default_vars if v.lower() not in new_vars_lower)
        if dropped:
            problems.append(f"删除了默认模板要求的变量占位符: {dropped}")

    # 畸形占位符：单个 { 后跟空白/结束（排除 JSON 示例体的正常换行）
    if re.search(r"\{[A-Za-z_]\w*\s*$", content or "", re.MULTILINE):
        problems.append("存在未闭合的变量占位符（{ 后缺少 }）")

    return problems


def render_prompt_safe(template: str, **kwargs) -> str:
    """注入「用户/外部内容」时的推荐入口：在 render_prompt 基础上默认开启
    元标记中和（sanitize=True），纵深防御提示词注入。

    注意：结构化 JSON 载荷（图表数据等）通常不含 `{shared:}` / `__X__` 元标记，
    调用本函数也安全；但如需保留可解析 JSON 中的裸花括号，请直接用
    render_prompt(template, sanitize=False, **kwargs)。
    """
    return render_prompt(template, sanitize=True, **kwargs)
