"""提示词模块增强回归测试（2026-10-06）。

覆盖：
1. 编排渲染器 _render_orch_user 统一到安全渲染器 render_prompt（兼容 v2 __OUTLINE__
   风格与历史 {leaf_list_json} 风格，且保留"项目概述缺失时补一段"逻辑）。
2. _fill_template 解析 {shared:NAME} 引用，与统一渲染器口径一致。
3. 占位符覆盖完整性：每个注册提示词的模板变量都必须被至少一个注入点实际提供
   （get_prompt / render_prompt / _fill_template / _render_orch_user / _render_arrange_sys），
   杜绝"占位符以字面量发给模型"的回归。这是"上下文完整注入"要求的静态护栏。
"""
from __future__ import annotations

import ast
import os

from app.services.ai.prompts import _ALL_PROMPTS
from app.services.ai import workflows_orchestration as orch
from app.services.ai import workflows_base as base


APP = os.path.join(os.path.dirname(__file__), "..", "app")


def test_render_orch_user_v2_style_fills_outline_and_overview():
    tpl = orch.get_prompt("CONTENT_ORCHESTRATION_USER")
    leaf = '[{"id":"1","title":"施工总体部署"}]'
    out = orch._render_orch_user(tpl, "某医院改扩建工程", leaf, "评分标准A")
    # v2 模板的 __OUTLINE__ / __SCORING_STANDARDS__ 必须被替换
    assert "__OUTLINE__" not in out
    assert "__SCORING_STANDARDS__" not in out
    assert "施工总体部署" in out          # leaf_json 注入
    assert "某医院改扩建工程" in out      # 项目概述注入
    # 模板未声明项目概述占位符 → 兜底补一段
    assert "项目概述" in out


def test_render_orch_user_legacy_style_fills():
    # 历史回落模板使用 {leaf_list_json} / {scoring_summary} / {project_overview}
    legacy = (
        "### 项目概述\n{project_overview}\n### 叶子节点列表（JSON）\n"
        "{leaf_list_json}\n### 评分标准（摘要）\n{scoring_summary}"
    )
    out = orch._render_orch_user(legacy, "道路工程", '[{"id":"2"}]', "权重评分")
    assert "{leaf_list_json}" not in out
    assert "{scoring_summary}" not in out
    assert "{project_overview}" not in out
    assert "道路工程" in out
    assert "权重评分" in out


def test_render_orch_user_single_pass_prevents_cross_var_injection():
    # 概述文本里若含有占位符形态，不应被二次替换（单遍渲染护栏）
    tpl = "概述:{project_overview}\n叶子:{leaf_list_json}"
    out = orch._render_orch_user(tpl, "正常概述__OUTLINE__", "LEAF", "SC")
    assert "__OUTLINE__" in out.split("叶子:")[0]  # 概述里的 __OUTLINE__ 原样保留，未被当变量
    assert "LEAF" in out


def test_fill_template_resolves_shared_ref():
    from app.services.ai.prompts._registry import _resolve_shared

    tpl = "规则开始 {shared:SHARED_OUTPUT_SPEC} 规则结束"
    out = base._fill_template(tpl, {}, {})
    # 必须消解 {shared:...}，不得残留字面量
    assert "{shared:" not in out
    # 且解出的共享规则内容确实被嵌入
    assert _resolve_shared("{shared:SHARED_OUTPUT_SPEC}") in out


def test_fill_template_keeps_unknown_placeholder_when_no_label():
    tpl = "数据:__FOO__ 引用:__FOO__"
    out = base._fill_template(tpl, {"__FOO__": "V"}, {})
    # 首次=数据，后续=标签（无标签则保留标签形态）；此处无 labels，后续应保留 __FOO__
    assert out.count("V") == 1
    assert "__FOO__" in out


# ---------------------------------------------------------------------------
# 占位符覆盖完整性：AST 静态护栏
# ---------------------------------------------------------------------------

def _collect_provided() -> dict[str, set[str]]:
    provided: dict[str, set[str]] = {k: set() for k in _ALL_PROMPTS}
    assigned: dict[str, str] = {}  # var -> prompt key
    dict_literals: dict[str, set[str]] = {}  # var -> 字典字面量的键集合

    def _dict_keys(d) -> set[str]:
        keys = set()
        if isinstance(d, ast.Dict):
            for k in d.keys:
                if isinstance(k, ast.Constant) and isinstance(k.value, str):
                    keys.add(k.value.strip("_").upper())
        return keys

    class V(ast.NodeVisitor):
        def visit_Assign(self, node):
            if len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                tgt = node.targets[0].id
                val = node.value
                if isinstance(val, ast.Call):
                    f = val.func
                    if (isinstance(f, ast.Attribute) and f.attr == "get_prompt") or (
                        isinstance(f, ast.Name) and f.id == "get_prompt"
                    ):
                        ka = val.args[0] if val.args else None
                        if isinstance(ka, ast.Constant) and isinstance(ka.value, str):
                            assigned[tgt] = ka.value
                elif isinstance(val, ast.Dict):
                    dict_literals[tgt] = _dict_keys(val)
            self.generic_visit(node)

        def visit_AnnAssign(self, node):
            if isinstance(node.target, ast.Name) and isinstance(node.value, ast.Call):
                val = node.value
                f = val.func
                if (isinstance(f, ast.Attribute) and f.attr == "get_prompt") or (
                    isinstance(f, ast.Name) and f.id == "get_prompt"
                ):
                    ka = val.args[0] if val.args else None
                    if isinstance(ka, ast.Constant) and isinstance(ka.value, str):
                        assigned[node.target.id] = ka.value
            self.generic_visit(node)

        def _key_of(self, node):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                return node.value
            if isinstance(node, ast.Name) and node.id in assigned:
                return assigned[node.id]
            if isinstance(node, ast.Call):
                f = node.func
                if (isinstance(f, ast.Attribute) and f.attr == "get_prompt") or (
                    isinstance(f, ast.Name) and f.id == "get_prompt"
                ):
                    ka = node.args[0] if node.args else None
                    if isinstance(ka, ast.Constant) and isinstance(ka.value, str):
                        return ka.value
            return None

        def visit_Call(self, node):
            f = node.func
            fname = f.attr if isinstance(f, ast.Attribute) else (f.id if isinstance(f, ast.Name) else None)
            if fname in ("get_prompt", "get_prompt_with_validation"):
                key = self._key_of(node.args[0]) if node.args else None
                if key:
                    kw = {kw.arg for kw in node.keywords if kw.arg}
                    if kw:
                        provided[key] |= {k.upper() for k in kw}
            elif fname == "render_prompt":
                key = self._key_of(node.args[0]) if node.args else None
                if key:
                    provided[key] |= {kw.arg.upper() for kw in node.keywords if kw.arg}
            elif fname == "_fill_template":
                key = self._key_of(node.args[0]) if node.args else None
                if key:
                    # mapping / labels 既可是位置参数也可是关键字参数
                    kwmap = {kw.arg: kw.value for kw in node.keywords if kw.arg}
                    mapping = node.args[1] if len(node.args) > 1 else kwmap.get("mapping")
                    labels = node.args[2] if len(node.args) > 2 else kwmap.get("labels")
                    for d in (mapping, labels):
                        if isinstance(d, ast.Dict):
                            provided[key] |= _dict_keys(d)
                        elif isinstance(d, ast.Name) and d.id in dict_literals:
                            provided[key] |= dict_literals[d.id]
            elif fname == "_render_orch_user":
                key = self._key_of(node.args[0]) if node.args else None
                if key:
                    provided[key] |= {
                        "OUTLINE", "SCORING_STANDARDS", "PROJECT_OVERVIEW", "SUMMARY",
                        "LEAF_LIST_JSON", "SCORING_SUMMARY", "KNOWLEDGE_CATALOG",
                    }
            elif fname == "_render_arrange_sys":
                key = self._key_of(node.args[0]) if node.args else None
                if key:
                    provided[key] |= {"SECTION_TITLE", "SUMMARY", "WRITING_LENGTH"}
            self.generic_visit(node)

    v = V()
    for root, _d, files in os.walk(APP):
        for fn in files:
            if not fn.endswith(".py"):
                continue
            p = os.path.join(root, fn)
            try:
                tree = ast.parse(open(p, encoding="utf-8").read(), filename=p)
            except Exception:
                continue
            v.visit(tree)
    # ENHANCE 2026-10-12：已注册的 legacy 编排模板 CONTENT_ORCHESTRATION_LEGACY_USER
    # 也走 _render_orch_user 渲染（三元表达式链 AST 无法静态推断 user_template 的值），
    # 这里手动标记其注入覆盖范围，防止误报 missing variables。
    _orch_full_vars = {
        "OUTLINE", "SCORING_STANDARDS", "PROJECT_OVERVIEW", "SUMMARY",
        "LEAF_LIST_JSON", "SCORING_SUMMARY", "KNOWLEDGE_CATALOG",
    }
    provided.setdefault("CONTENT_ORCHESTRATION_LEGACY_USER", set())
    provided["CONTENT_ORCHESTRATION_LEGACY_USER"] |= _orch_full_vars
    return provided


def test_prompt_placeholder_coverage_complete():
    provided = _collect_provided()
    gaps = []
    for key, meta in _ALL_PROMPTS.items():
        tvars = set(meta.get("variables", []))
        if not tvars:
            continue
        prov = provided.get(key, set())
        missing = [v for v in tvars if v.upper() not in prov]
        if missing:
            gaps.append((key, sorted(tvars), sorted(missing)))
    assert not gaps, f"存在模板占位符从未被注入点提供（会以字面量发给模型）: {gaps}"


def test_shared_refs_in_templates_resolve():
    import re

    pat = re.compile(r"\{shared:([A-Za-z_]\w{1,})\}")
    bad = []
    for key, meta in _ALL_PROMPTS.items():
        refs = pat.findall(meta["content"])
        for r in refs:
            cand = None
            for c in (r, "SHARED_" + r, r.upper(), "SHARED_" + r.upper()):
                if c in _ALL_PROMPTS:
                    cand = c
                    break
            if cand is None:
                bad.append((key, r))
    assert not bad, f"模板引用了未注册的共享规则（渲染时残留字面量）: {bad}"


def test_fill_template_sanitize_neutralizes_meta_markers_in_data():
    # sanitize=True 时，注入的数据值里的**模块自有元标记**应被中和
    # （与 render_prompt 对变量值口径一致）。
    #
    # BUG-FIX-PROMPT-S1（2026-10-09 语义修正）：本用例原先还断言裸 `{fake}`
    # 也被中和。该断言编码的是「防御性过度」行为，且会破坏真实内容：
    # 本项目两条渲染链路（_registry.render_prompt 与 base._fill_template）
    # 均为单遍 re.sub，插入的变量值不会被二次扫描，裸 `{var}` 在值里本就
    # 不会被重新解释，因此它不是注入面；而中和它会毁掉 Mermaid 判定节点
    # `A{开始}` 与图表数据。现改为：模块元语法照常中和，裸 `{var}` 默认保留
    # （可用 inline_vars=True 显式开启中和）。
    malicious = "正常数据 {shared:SHARED_OUTPUT_SPEC} 和 __SECRET__ 与 {fake}"
    out = base._fill_template("数据:__X__", {"__X__": malicious}, {}, sanitize=True)
    assert "{shared:" not in out
    assert "__SECRET__" not in out
    assert "正常数据" in out
    # 裸 {var} 默认保留（单遍渲染下无害，且中和会破坏 Mermaid/图表数据）
    assert "{fake}" in out
    # 显式开启 inline_vars 后仍可中和
    from app.services.ai.prompts._security import sanitize_prompt_value

    assert "{fake}" not in sanitize_prompt_value(malicious, inline_vars=True)


def test_sanitize_preserves_mermaid_and_chart_payloads():
    # BUG-FIX-PROMPT-S1 回归守卫：sanitize 不得破坏图表/流程图管线依赖的语法。
    # 修复前 neutralize_meta_markers 无条件中和裸 {var}，实测会把
    #   flowchart TD / A{开始} --> B[结束]
    # 改写为 A[占位符已屏蔽]，导致 Mermaid 渲染管线拿到坏数据。
    from app.services.ai.prompts._security import sanitize_prompt_value

    mermaid = "flowchart TD\n    A{开始} --> B[结束]\n    B --> C{是否通过}"
    assert sanitize_prompt_value(mermaid) == mermaid

    chart_json = '{"type": "bar", "series": [{"name": "a", "data": [1, 2]}]}'
    assert sanitize_prompt_value(chart_json) == chart_json

    # 真正需要中和的仍然生效
    assert "共享规则引用已屏蔽" in sanitize_prompt_value("注入 {shared:SHARED_OUTPUT_SPEC}")
    assert "元占位符已屏蔽" in sanitize_prompt_value("注入 __GLOBAL_FACTS__")


def test_fill_template_desensitize_masks_sensitive_info():
    data = "联系人 a@b.com 手机 13800138000"
    out = base._fill_template("数据:__X__", {"__X__": data}, {}, desensitize=True)
    assert "a@b.com" not in out
    assert "13800138000" not in out


def test_fill_template_sanitize_default_off_is_backward_compatible():
    # 默认关闭，不改变历史行为（不破坏现有调用方 / 不误伤 JSON 数据）
    data = '[{"score": 10, "item": "质量"}]'
    out = base._fill_template("数据:__X__ 引用:__X__", {"__X__": data}, {"__X__": "评分点列表"})
    # 首次=数据，后续=标签
    assert out.count(data) == 1
    assert "评分点列表" in out
    assert "{shared:" not in out  # 无元标记故原样保留数据


def test_fill_template_review_chain_sanitize_enabled():
    # 审核链路调用形态：开启 sanitize 时数据值被中和但不破坏占位符替换
    from app.services.ai.workflows_quality import _build_outline_review_texts

    sys_t, user_t = _build_outline_review_texts(
        scoring_points='[{"score":10}]',
        outline="第一章",
        summary="概述含 {shared:SHARED_OUTPUT_SPEC} 注入尝试",
        industry_profile="房建",
    )
    # 占位符全部被替换（无残留字面量）
    for marker in ("__SCORING_POINTS__", "__OUTLINE__", "__SUMMARY__", "__INDUSTRY_PROFILE__"):
        assert marker not in sys_t
        assert marker not in user_t
    # 注入尝试被中和
    assert "{shared:" not in user_t

