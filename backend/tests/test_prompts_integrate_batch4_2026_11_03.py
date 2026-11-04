"""批次 3/4 收编收尾测试（BUG-FIX-PROMPT-E10，2026-11-03）。

覆盖 _integrate_batch4.py docstring 预留的双文本断言：
- batch3（13 key）/ batch4（16 新增 key）注册存在性；
- 调用方 fallback 常量与注册表正本逐字一致（DB 覆盖语义成立的前提）；
- 收编前后渲染等价（qa 组装 / facts 提取 / kb·tpl 补源 / knowledge_refill）；
- D1 共享规则库无内联副本（单一事实源不漂移）。

零行为变更原则：注册表可用时 get_prompt 返回注册文本（= fallback 常量），
渲染输出与收编前 f-string 逐字节一致；本文件逐条断言该等价性。
若有意变更注册文本，必须三处同步（注册表 / fallback 常量 / 本文件的
legacy 语义重建），并说明行为变更理由。
"""

import re

import pytest

from app.services.ai.prompts import get_prompt, render_prompt
from app.services.ai.prompts._registry import _ALL_PROMPTS

# ────────────────────────────────────────────────────────────────
# 注册存在性
# ────────────────────────────────────────────────────────────────

BATCH3_KEYS = [
    "FACTS_EXTRACT_SYSTEM",
    "FACTS_KB_SUPPLEMENT_SYSTEM",
    "FACTS_KB_SUPPLEMENT_USER",
    "FACTS_TPL_SUPPLEMENT_SYSTEM",
    "FACTS_TPL_SUPPLEMENT_USER",
    "FACTS_AUDIT_SYSTEM",
    "QA_SYSTEM_ROLE",
    "QA_CONTEXT_TENDER_BLOCK",
    "QA_CONTEXT_SECTIONS_BLOCK",
    "QA_ANSWER_REQUIREMENTS",
    "TEMPLATE_EXTRACT_OUTLINE_OBJECT_SYSTEM",
    "JSON_REPAIR_SYSTEM_LEGACY",
    "MERMAID_REPAIR_SYSTEM_ANY",
]

# batch4 新增 16 key（KB_REFILL_EXTRACT_SYSTEM 为批次 1 存量，不在断言范围）
BATCH4_NEW_KEYS = [
    "KB_REFILL_EXTRACT_USER",
    "KB_REFILL_GAP_USER",
    "FEASIBILITY_ANALYSIS_SYSTEM",
    "FEASIBILITY_ANALYSIS_USER",
    "FEASIBILITY_ANALYSIS_MERGE_SYSTEM",
    "FEASIBILITY_ANALYSIS_MERGE_USER",
    "FEASIBILITY_OUTLINE_SYSTEM",
    "FEASIBILITY_OUTLINE_USER",
    "FEASIBILITY_PARAMETERS_SYSTEM",
    "FEASIBILITY_PARAMETERS_USER",
    "FEASIBILITY_CONTENT_SYSTEM",
    "FEASIBILITY_CONTENT_WRITING_RULES",
    "FEASIBILITY_KB_BACKGROUND_NOTE",
    "FEASIBILITY_KB_CATALOG_NOTE",
    "FEASIBILITY_KB_MATERIAL_NOTE",
    "FEASIBILITY_HUMAN_WRITING_SYSTEM",
]


@pytest.mark.parametrize("key", BATCH3_KEYS + BATCH4_NEW_KEYS)
def test_batch34_keys_registered(key):
    assert key in _ALL_PROMPTS, f"批次 key 未注册: {key}"


# ────────────────────────────────────────────────────────────────
# qa.py 四块拼装（E10 接线）
# ────────────────────────────────────────────────────────────────


def test_qa_fallbacks_match_registry():
    from app.services import qa as qa_mod

    assert qa_mod._QA_FALLBACK_ROLE == get_prompt("QA_SYSTEM_ROLE")
    assert qa_mod._QA_FALLBACK_TENDER_BLOCK == get_prompt("QA_CONTEXT_TENDER_BLOCK")
    assert qa_mod._QA_FALLBACK_SECTIONS_BLOCK == get_prompt("QA_CONTEXT_SECTIONS_BLOCK")
    assert qa_mod._QA_FALLBACK_REQUIREMENTS == get_prompt("QA_ANSWER_REQUIREMENTS")


def test_qa_system_prompt_assembly_equivalence():
    """注册路径拼装 == 收编前 legacy 拼装（五种 ctx 组合逐字节一致）。"""
    from app.services.qa import _build_system_prompt, _legacy_system_prompt

    pn = "智慧园区项目"
    cases = [
        {},
        {"tender_text": "招标要点T"},
        {"sections_text": "章节S"},
        {"tender_text": "招标要点T", "sections_text": "章节S"},
        {"tender_text": "", "sections_text": None},
    ]
    for ctx in cases:
        assert _build_system_prompt(pn, ctx) == _legacy_system_prompt(pn, ctx), ctx


# ────────────────────────────────────────────────────────────────
# feasibility_prompts 静态 fallback ↔ 注册表（12 个）
# ────────────────────────────────────────────────────────────────

import app.services.feasibility_prompts as fb_mod  # noqa: E402

_FEAS_FB_MAP = {
    "_FB_ANALYSIS_SYSTEM": "FEASIBILITY_ANALYSIS_SYSTEM",
    "_FB_ANALYSIS_MERGE_SYSTEM": "FEASIBILITY_ANALYSIS_MERGE_SYSTEM",
    "_FB_ANALYSIS_MERGE_USER": "FEASIBILITY_ANALYSIS_MERGE_USER",
    "_FB_OUTLINE_SYSTEM": "FEASIBILITY_OUTLINE_SYSTEM",
    "_FB_PARAMETERS_SYSTEM": "FEASIBILITY_PARAMETERS_SYSTEM",
    "_FB_PARAMETERS_USER": "FEASIBILITY_PARAMETERS_USER",
    "_FB_CONTENT_SYSTEM": "FEASIBILITY_CONTENT_SYSTEM",
    "_FB_CONTENT_WRITING_RULES": "FEASIBILITY_CONTENT_WRITING_RULES",
    "_FB_KB_BACKGROUND_NOTE": "FEASIBILITY_KB_BACKGROUND_NOTE",
    "_FB_KB_CATALOG_NOTE": "FEASIBILITY_KB_CATALOG_NOTE",
    "_FB_KB_MATERIAL_NOTE": "FEASIBILITY_KB_MATERIAL_NOTE",
    "_FB_HUMAN_WRITING_SYSTEM": "FEASIBILITY_HUMAN_WRITING_SYSTEM",
}


@pytest.mark.parametrize("fb_name,key", sorted(_FEAS_FB_MAP.items()))
def test_feasibility_fallbacks_match_registry(fb_name, key):
    assert getattr(fb_mod, fb_name) == get_prompt(key)


def test_feasibility_dynamic_users_fully_rendered():
    """动态 build_*（注册路径）输出不得残留未解析占位符。"""
    out1 = fb_mod.build_analysis_user_instruction(1, 3)
    out2 = fb_mod.build_outline_user_instruction("tpl-1", 5000, has_knowledge=True)
    out3 = fb_mod.build_outline_user_instruction("tpl-1", 5000, has_knowledge=False)
    for name, out in [("analysis_user", out1), ("outline_user(has_kb)", out2), ("outline_user", out3)]:
        assert not re.search(r"\{[A-Za-z_]\w{1,}\}", out), f"{name} 残留占位符: {out[:200]}"
    assert "knowledge_item_ids" in out2 and "knowledge_item_ids" not in out3


# ────────────────────────────────────────────────────────────────
# knowledge_refill user 侧（E10 收编）渲染等价
# ────────────────────────────────────────────────────────────────


def test_knowledge_refill_extract_user_equivalence():
    from app.services.ai.knowledge_refill import _extract_user_prompt

    assert _extract_user_prompt("分批文本B1") == "请提取以下招标文件内容中的关键知识条目：\n\n分批文本B1"


def test_knowledge_refill_gap_user_equivalence():
    from app.services.ai.knowledge_refill import _gap_user_prompt

    want = """请检查招标文件中是否有以下目录章节涉及但未提取的关键信息。

已有知识条目：
条目A

目录章节：
章节X

招标文件正文（取材来源，只允许从中提取、禁止编造）：
正文C

请从上述招标文件正文中提取与这些章节相关、但尚未包含在已有知识条目中的关键信息。

输出格式：JSON 数组，每个元素包含：
- title: 知识条目标题
- content: 知识条目详细内容
- category: 分类
- keywords: 关键词数组

只返回 JSON 数组，不要包含任何解释或 markdown 围栏。"""
    assert _gap_user_prompt("条目A", "章节X", "正文C") == want


# ────────────────────────────────────────────────────────────────
# 事实链路（E10 f5 接线）：fallback ↔ 注册表 + 渲染等价
# ────────────────────────────────────────────────────────────────


def test_facts_router_fallbacks_match_registry():
    from app.routers import facts as facts_mod

    assert facts_mod._FACTS_KB_SUPPLEMENT_SYS_FB == get_prompt("FACTS_KB_SUPPLEMENT_SYSTEM")
    assert facts_mod._FACTS_KB_SUPPLEMENT_USER_FB == get_prompt("FACTS_KB_SUPPLEMENT_USER")
    assert facts_mod._FACTS_TPL_SUPPLEMENT_SYS_FB == get_prompt("FACTS_TPL_SUPPLEMENT_SYSTEM")
    assert facts_mod._FACTS_TPL_SUPPLEMENT_USER_FB == get_prompt("FACTS_TPL_SUPPLEMENT_USER")
    assert facts_mod._FACTS_AUDIT_SYS_FB == get_prompt("FACTS_AUDIT_SYSTEM")


def test_facts_service_extract_fallback_matches_registry():
    from app.services.facts_service import _FACTS_EXTRACT_SYSTEM_FB

    assert _FACTS_EXTRACT_SYSTEM_FB == get_prompt("FACTS_EXTRACT_SYSTEM")


def test_templates_extract_fallbacks_match_registry():
    from app.routers.templates import (
        _TEMPLATE_EXTRACT_SYSTEM_FB,
        _TEMPLATE_EXTRACT_USER_FB,
    )

    assert _TEMPLATE_EXTRACT_SYSTEM_FB == get_prompt("TEMPLATE_EXTRACT_SYSTEM")
    # E10 遗留加固：user 前缀注册化
    assert _TEMPLATE_EXTRACT_USER_FB == get_prompt("TEMPLATE_EXTRACT_USER")


def test_templates_extract_user_render_equivalence():
    """user 前缀注册路径渲染 == 收编前 f-string 输出（逐字节一致）。"""
    from app.routers.templates import _TEMPLATE_EXTRACT_USER_FB

    assert (
        render_prompt(_TEMPLATE_EXTRACT_USER_FB, truncated="正文TXT")
        == "请从以下标书文本中提取目录结构：\n\n正文TXT"
    )
    # replace 兜底路径（注册表异常）同样一致
    assert (
        _TEMPLATE_EXTRACT_USER_FB.replace("{truncated}", "正文TXT")
        == "请从以下标书文本中提取目录结构：\n\n正文TXT"
    )


def test_fact_extract_prompt_renders_like_legacy():
    """fact_extract_system_prompt == 收编前 f-string 原实现（四种缺失值策略逐字节一致）。"""
    from app.services.facts_service import (
        DEFAULT_FACTS_POLICY,
        fact_extract_system_prompt,
        value_normalization_rules,
    )

    def legacy_extract(policy):
        """收编前 facts_service.py 的 f-string 原实现（逐字重建）。"""
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

    for policy in [DEFAULT_FACTS_POLICY, "fill", "ask", "omit"]:
        assert fact_extract_system_prompt(policy) == legacy_extract(policy), policy


def test_kb_tpl_supplement_render_equivalence():
    """kb/tpl 补源 user 渲染 == 收编前 f-string 输出（含空 JSON 对象 {} 与中文分号）。"""
    from app.routers.facts import (
        _FACTS_KB_SUPPLEMENT_USER_FB,
        _FACTS_TPL_SUPPLEMENT_USER_FB,
    )

    mat = "素材A\n素材B"
    want_kb = f"""以下是参考知识库素材（历史项目文档/企业级规范，仅作事实补源）：

{mat}

请从上述知识库素材中补充提取全局事实变量：
1. 只提取素材中有明确依据的确定性事实；
2. 与招标文件/Step02 解析结果冲突时，不要覆盖已确认的值（基线以招标为准）；
3. 素材没有明确值时严禁编造；没有可补充内容时返回空 JSON 对象 {{}}；
4. 输出 JSON 对象（dict），严禁附加解释。"""
    assert render_prompt(_FACTS_KB_SUPPLEMENT_USER_FB, kb_material=mat) == want_kb
    # fallback 路径（注册表异常时的 .replace 兜底）同样一致
    assert _FACTS_KB_SUPPLEMENT_USER_FB.replace("{kb_material}", mat) == want_kb

    tpl_mat = "模板素材X"
    want_tpl = f"""以下是项目已上传的既有方案/模板素材（仅作事实补源）：

{tpl_mat}

请从上述既有方案素材中补充提取全局事实变量：
1. 只提取素材中有明确依据的确定性事实；原方案已写成的实际安排、既有承诺优先；
2. 与招标文件/Step02 解析结果冲突时，不要覆盖已确认的值（基线以招标为准）；
3. 素材没有明确值时严禁编造；没有可补充内容时返回空 JSON 对象 {{}}；
4. 输出 JSON 对象（dict），严禁附加解释。"""
    assert render_prompt(_FACTS_TPL_SUPPLEMENT_USER_FB, tpl_material=tpl_mat) == want_tpl
    assert _FACTS_TPL_SUPPLEMENT_USER_FB.replace("{tpl_material}", tpl_mat) == want_tpl


# ────────────────────────────────────────────────────────────────
# D1 共享规则库：单一事实源不漂移（无内联副本）
# ────────────────────────────────────────────────────────────────

_SHARED_MARKERS = {
    "SHARED_HEADING_RULES": ["标题编号规范（V3.1", "title 字段只填纯文本标题"],
    "SHARED_FORBIDDEN_WORDS": ["**二级标题绝对禁用**", "**三级标题绝对禁用**"],
    "SHARED_OUTPUT_SPEC": ["### 输出规范（严格执行）"],
}


def _entry_value(entry):
    if isinstance(entry, dict) and "value" in entry:
        return entry["value"]
    return entry


def test_shared_rules_no_inline_duplicates():
    """任何注册模板不得内联复制共享规则文本（应改用 {shared:NAME} 引用）。"""
    for name, marks in _SHARED_MARKERS.items():
        for key, entry in _ALL_PROMPTS.items():
            if key.startswith("SHARED_"):
                continue
            val = _entry_value(entry)
            if isinstance(val, str) and all(m in val for m in marks):
                pytest.fail(f"{key} 内联复制了 {name} 的规则文本，应改为 {{{name}}} 引用")


def test_shared_rules_registered():
    for name in _SHARED_MARKERS:
        assert name in _ALL_PROMPTS
