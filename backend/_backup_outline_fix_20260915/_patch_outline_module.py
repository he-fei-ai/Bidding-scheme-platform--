# -*- coding: utf-8 -*-
"""目录生成模块修复补丁（BUG-FIX 2026-09-15）。

覆盖四项修复：
  1. outline_gate.parse_word_budget —— 区间单位混用 + 噪声误匹配
  2. workflows_outline_v51 —— reference_outline 死参数生效 / 死变量 / 失实 docstring
  3. prompts.outline —— 生成侧与审核侧的一级目录数量判据统一
  4. sse_outline —— 静态清理（未使用导入 / 死语句 / 冗余 nonlocal）

本仓库目标文件全为 CRLF，故统一「读入归一化为 LF → 匹配替换 → 按原换行符写回」，
并对每处替换断言命中次数恰为 1，避免静默改错位置。
"""

import io
import sys

FAILED = []


def _load(path):
    with io.open(path, encoding="utf-8", newline="") as f:
        raw = f.read()
    nl = "\r\n" if "\r\n" in raw else "\n"
    return raw.replace("\r\n", "\n"), nl


def _save(path, text_lf, nl):
    with io.open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text_lf.replace("\n", nl))


def apply_blocks(path, edits):
    """按文本块替换。edits: [(old, new, label), ...]"""
    src, nl = _load(path)
    for i, (old, new, label) in enumerate(edits, 1):
        cnt = src.count(old)
        if cnt != 1:
            print("FAIL %s #%d [%s]: 命中 %d 次（期望 1）" % (path, i, label, cnt))
            FAILED.append("%s#%d" % (path, i))
            return
        src = src.replace(old, new)
    _save(path, src, nl)
    print("OK   %s  (%d 处文本块替换)" % (path, len(edits)))


def apply_statement_deletes(path, targets):
    """删除「整行内容匹配 + 紧随其后一个空行」的死语句。

    用内容定位而非行号：同一次运行里前面的块替换会移动行号。
    targets: [(判定函数, 期望命中行数, 标签), ...]
    """
    src, nl = _load(path)
    lines = src.split("\n")
    doomed = []
    for pred, expect, label in targets:
        hits = [i for i, l in enumerate(lines) if pred(l)]
        if len(hits) != expect:
            print("FAIL %s [%s]: 命中 %d 行（期望 %d）: %s" % (path, label, len(hits), expect, hits))
            FAILED.append("%s[%s]" % (path, label))
            return
        doomed.extend(hits)
    for idx in sorted(set(doomed), reverse=True):
        end = idx + 1
        if end < len(lines) and lines[end].strip() == "":
            end += 1
        del lines[idx:end]
    _save(path, "\n".join(lines), nl)
    print("OK   %s  (删除 %d 行死语句)" % (path, len(set(doomed))))


# ===========================================================================
# 1. outline_gate.py —— 字数预算解析
# ===========================================================================
GATE = "app/services/ai/outline_gate.py"

apply_blocks(GATE, [
    (
        """#   总量：约 30000 字 / 总计 3 万字 / 2 万字以上
_WORD_TOTAL_RE = re.compile(
    r"(?:约|大约|总计|共计|合计|全文|总字数)?\\s*(\\d{1,7})\\s*(万?)\\s*字(?:以上|左右|以内|以下)?"
)

# 占位符 / 未渲染残留规则：(标记名, 正则)""",
        """#   总量：约 30000 字 / 总计 3 万字 / 2 万字以上
_WORD_TOTAL_RE = re.compile(
    r"(?:约|大约|总计|共计|合计|全文|总字数)?\\s*(\\d{1,7})\\s*(万?)\\s*字(?:以上|左右|以内|以下)?"
)

# 字数预算的最小可信值（BUG-FIX 2026-09-15）。
# 低于该值基本是"每页 N 字""限 N 字"这类排版格式要求，不是标书正文的总字数
# 预算。原实现没有这道护栏，招标全文里随便一句"每页 500 字"都会被当成全文
# 预算，`derive_target_leaf_count` 据此推出 1 个目标叶子，leaf-count 检查随即
# 对一份几百节点的正常目录报出"叶子数严重不足"的误报。
MIN_PLAUSIBLE_WORD_BUDGET = 1000

# 占位符 / 未渲染残留规则：(标记名, 正则)""",
        "常量",
    ),
    (
        """    def _num(raw: str, unit: str) -> int | None:
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return None
        if value <= 0:
            return None
        return value * 10000 if unit else value

    # 1. 区间优先（区间信息量最大，且能同时给出上下限）
    match = _WORD_RANGE_RE.search(text)
    if match:
        low_unit, high_unit = match.group(2), match.group(4)
        # 一侧写了「万」，另一侧通常省略，按同一量级处理（1 万~2 万字）
        unit = low_unit or high_unit
        low = _num(match.group(1), unit)
        high = _num(match.group(3), unit)
        if low and high:
            result["min_words"], result["max_words"] = min(low, high), max(low, high)""",
        """    def _num(raw: str, has_unit: bool) -> int | None:
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return None
        if value <= 0:
            return None
        total = value * 10000 if has_unit else value
        # 低于可信下限的一律当噪声丢弃（见 MIN_PLAUSIBLE_WORD_BUDGET）
        return total if total >= MIN_PLAUSIBLE_WORD_BUDGET else None

    def _omits_unit(raw: str) -> bool:
        \"\"\"该数值是否属于「省略了万」的写法（如「1~2万字」的前侧）。

        只有三位以内的数才可能省略量级词；`10000~2万字` 的前侧已是绝对值。
        \"\"\"
        try:
            return int(raw) < 1000
        except (TypeError, ValueError):
            return False

    # 1. 区间优先（区间信息量最大，且能同时给出上下限）
    match = _WORD_RANGE_RE.search(text)
    if match:
        low_raw, high_raw = match.group(1), match.group(3)
        low_unit, high_unit = bool(match.group(2)), bool(match.group(4))
        # BUG-FIX 2026-09-15（中）：原实现无条件取 `unit = low_unit or high_unit`，
        # 把两侧按同一量级处理。这对「1 万~2 万字」是对的，但「10000~2 万字」的
        # 前侧本来就是绝对值，再乘 10000 会得到 1 亿字，目标叶子数被推导成
        # 16670，leaf-count 检查随即误报。改为只在**小数值一侧**补量级词。
        if low_unit and not high_unit and _omits_unit(high_raw):
            high_unit = True
        elif high_unit and not low_unit and _omits_unit(low_raw):
            low_unit = True
        low = _num(low_raw, low_unit)
        high = _num(high_raw, high_unit)
        if low and high:
            result["min_words"], result["max_words"] = min(low, high), max(low, high)""",
        "区间解析",
    ),
    (
        """    # 2. 下限
    if result["min_words"] is None:
        match = _WORD_MIN_RE.search(text)
        if match:
            result["min_words"] = _num(match.group(1), match.group(2))

    # 3. 上限
    if result["max_words"] is None:
        match = _WORD_MAX_RE.search(text)
        if match:
            result["max_words"] = _num(match.group(1), match.group(2))

    # 4. 总量（「约 3 万字」这类只给单值的写法）
    if result["min_words"] is None and result["max_words"] is None:
        match = _WORD_TOTAL_RE.search(text)
        if match:
            result["total_words"] = _num(match.group(1), match.group(2))""",
        """    # 2. 下限
    if result["min_words"] is None:
        match = _WORD_MIN_RE.search(text)
        if match:
            result["min_words"] = _num(match.group(1), bool(match.group(2)))

    # 3. 上限
    if result["max_words"] is None:
        match = _WORD_MAX_RE.search(text)
        if match:
            result["max_words"] = _num(match.group(1), bool(match.group(2)))

    # 4. 总量（「约 3 万字」这类只给单值的写法）
    if result["min_words"] is None and result["max_words"] is None:
        match = _WORD_TOTAL_RE.search(text)
        if match:
            result["total_words"] = _num(match.group(1), bool(match.group(2)))""",
        "上下限/总量单位传参",
    ),
])


# ===========================================================================
# 2. workflows_outline_v51.py
# ===========================================================================
V51 = "app/services/ai/workflows_outline_v51.py"

apply_blocks(V51, [
    (
        """# ========== 参考模板（可根据项目特点增删章节） ==========

REFERENCE_TEMPLATE_V51 = [""",
        """# ========== 参考模板（可根据项目特点增删章节） ==========
#
# BUG-FIX 2026-09-15（中）：本软件曾有三份互不同步的「固定参考目录」——
#   - `outline_sort_optimizer.CORE_28_CHAPTERS`（28 章，带 zone，供排序优化器用）
#   - `outline_common.FIXED_28_CHAPTERS`（28 章，`_get_template_list_json()` 的输出；
#     路由层把它读出来经 `reference_outline` 形参传进本工作流）
#   - 本列表（23 章）
# 前两份内容一致，本列表少了 5 章：文明施工措施计划、拟配备本工程的试验和检测
# 仪器设备表、任何可能的紧急情况的处理措施预案以及抵抗风险的措施、与发包人
# （项目管理单位、监理及设计人）的配合、其他。
# 而本工作流此前**直接忽略**调用方传入的 `reference_outline`，永远用这份 23 章
# 列表，于是「改参考目录」只改一处、另一处不动，两套骨架长期漂移且无人察觉。
# 现由 `_resolve_reference_json()` 让调用方传入的参考目录优先生效，本列表仅作兜底。
REFERENCE_TEMPLATE_V51 = [""",
        "参考模板来源注释",
    ),
    (
        """    {"id": "ref_23", "title": "附图、附件"},
]


# ========== AI 调用统一入口 ==========""",
        """    {"id": "ref_23", "title": "附图、附件"},
]


def _resolve_reference_json(reference_outline: Any) -> str:
    \"\"\"把调用方传入的参考目录解析为提示词可用的 JSON 字符串。

    BUG-FIX 2026-09-15（中）：`generate_outline_v51(reference_outline=...)` 这个形参
    此前从未被读取过 —— 路由层从 `outline_common._get_template_list_json()` 取出现成
    的模板 JSON 一路传进来，却被静默丢弃，实际用的是硬编码的 `REFERENCE_TEMPLATE_V51`。
    这里让它真正生效。

    接受 JSON 字符串（路由层产物）、已解析的 list，或空值；非法/空值一律回退内置
    模板，保证 `<reference>` 段永远有内容、且永远是合法 JSON。

    Returns:
        str: 形如 `[{"id": "1", "title": "项目概述"}, ...]` 的合法 JSON 字符串。
    \"\"\"
    candidate: Any = None
    if isinstance(reference_outline, str) and reference_outline.strip():
        try:
            candidate = json.loads(reference_outline)
        except (ValueError, TypeError) as _ref_e:
            logger.warning("V5.1 参考目录不是合法 JSON，回退内置模板: %s", _ref_e)
    elif isinstance(reference_outline, list):
        candidate = reference_outline

    if isinstance(candidate, list) and candidate:
        # 只保留 id/title：调用方可能带上 zone 等额外字段，白占提示词预算
        slim = [
            {"id": str(ch.get("id", "")), "title": str(ch.get("title", "")).strip()}
            for ch in candidate
            if isinstance(ch, dict) and str(ch.get("title", "")).strip()
        ]
        if slim:
            return json.dumps(slim, ensure_ascii=False)

    return json.dumps(REFERENCE_TEMPLATE_V51, ensure_ascii=False)


# ========== AI 调用统一入口 ==========""",
        "新增 _resolve_reference_json",
    ),
    (
        """        f"3. 按项目逻辑排序（总纲→组织→方案→管理→收尾）\\n"
        f"4. 直接返回 JSON 数组，不要任何额外说明\\n"
    )""",
        """        f"3. 按项目逻辑排序（总纲→组织→方案→管理→收尾）\\n"
        f"4. <reference> 仅作章节体系与命名风格参考，不得据此改变章节数量\\n"
        f"5. 直接返回 JSON 数组，不要任何额外说明\\n"
    )""",
        "L1 user prompt 参考模板约束",
    ),
    (
        """    # 构建参考模板
    reference_json = json.dumps(REFERENCE_TEMPLATE_V51, ensure_ascii=False)""",
        """    # 构建参考模板：调用方传入的参考目录优先，缺省才回退内置模板。
    reference_json = _resolve_reference_json(reference_outline)""",
        "reference_outline 生效",
    ),
    (
        """        word_budget_text="\\n".join(
            part for part in (scoring_points, tender_context, project_scope) if part
        ),""",
        """        # 字数预算只从评分要点与承包范围解析：招标文件全文（tender_context）动辄
        # 数万字，其中"每页 N 字""限 N 字"这类排版要求会被 _WORD_TOTAL_RE 命中，
        # 把目标叶子数推导到个位数。护栏见 outline_gate.parse_word_budget。
        word_budget_text="\\n".join(
            part for part in (scoring_points, project_scope) if part
        ),""",
        "word_budget_text 去全文噪声",
    ),
    (
        """    _tech_verbs = ("施工", "管理", "控制", "检查", "组织", "实施", "协调")
    # 已含技术关键词的三级标题特征""",
        """    # 已含技术关键词的三级标题特征""",
        "删 _tech_verbs 死变量",
    ),
    (
        """    \"\"\"L3：流水线生成三级目录。

    ⚠️ 说明（2026-09-14 排查）：本函数**完全不调用 AI**，`provider` 参数保留仅为
    接口兼容。三级标题为空时由 `_generate_diverse_l3_title()` 按规则兜底生成，
    因此 L3 阶段**不存在可注入全局事实的提示词入口**——要注入就必须把 L3 重新
    改成 AI 驱动，代价是当前"零耗时、零成本"的 L3 会变成 N 次 AI 调用。
    事实约束目前覆盖 L1（_generate_level1_v51）与 L2（_fill_level2_with_ai），
    如需 L3 也受约束，应改走"事实校验/后处理"而非"提示词注入"。

    在 L2 阶段已经预构建了包含三级节点的骨架，此处 AI 仅需填充三级标题。
    流水线模式：L2 完成一个章节，立即开始该章节的 L3 生成。
    三级默认标题根据二级目录名称多样化生成。

    BUG 修复：添加完整的类型检查，防止非 dict 元素导致 AttributeError。
    \"\"\"""",
        """    \"\"\"L3：补全三级目录（全量串行后处理，**不是**流水线）。

    ⚠️ 说明（2026-09-14 排查，2026-09-15 修正描述）：本函数**完全不调用 AI**。
    `provider` / `summary` / `sp_text` / `l2_done_queue` 四个形参目前都只为接口
    兼容保留，函数体内一概未使用。三级标题非空则原样保留，为空才由
    `_generate_diverse_l3_title()` 按规则兜底生成。因此 L3 阶段**不存在可注入
    全局事实的提示词入口**——要注入就必须把 L3 改成 AI 驱动，代价是当前
    "零耗时、零成本"的 L3 会变成 N 次 AI 调用。事实约束目前覆盖 L1
    （_generate_level1_v51）与 L2（_fill_level2_with_ai），如需 L3 也受约束，
    应改走"事实校验/后处理"而非"提示词注入"。

    原 docstring 自称"流水线模式：L2 完成一个章节，立即开始该章节的 L3 生成"，
    与实现不符：这里是在 L2 全部结束后对整棵目录做一次串行遍历，`l2_done_queue`
    从未被消费。若确实需要流水线语义，应重写本函数而非只改描述。

    三级默认标题根据二级目录名称多样化生成。

    BUG 修复：添加完整的类型检查，防止非 dict 元素导致 AttributeError。
    \"\"\"""",
        "L3 docstring 如实化",
    ),
])


# ===========================================================================
# 3. prompts/outline.py —— 统一一级目录数量判据
# ===========================================================================
PROMPTS = "app/services/ai/prompts/outline.py"

apply_blocks(PROMPTS, [
    (
        """### ⚠️ 重要：目录生成范式说明

当前目录以【参考一级目录】为强制骨架生成，一级目录数量由参考目录决定（通常远多于评分点数量），**不要求一级目录与评分点一一对应**。评分点内容归入最相关的章节即可。审核时**不需要**检查"一级目录数量 = 评分点数量"。""",
        """### ⚠️ 重要：目录生成范式说明

本软件的目录由 V5.1 工作流生成：**一级目录按评分项一一映射**——每个评分项对应一个一级章节，允许 ±1 的合并/拆分浮动。审核时须核对"一级目录数量与评分点数量是否基本一致（差异 ≤1）"；差异过大（例如 8 个评分点却只有 3 章）意味着有评分项未被映射成章节，应作为问题报告。""",
        "SYSTEM 范式说明",
    ),
    (
        """1. **评分点覆盖检查（一票否决）**：检查每个评分点的 `standard` 中关键要求是否在目录的子目录中有对应体现。若某个评分点（权重≥5分）的核心要求完全未被任何章节覆盖，`passed` 为 `false`。
2. **目录层级结构**""",
        """1. **评分点覆盖检查（一票否决）**：检查每个评分点的 `standard` 中关键要求是否在目录的子目录中有对应体现。若某个评分点（权重≥5分）的核心要求完全未被任何章节覆盖，`passed` 为 `false`。**同时核对数量**：一级目录数量与评分点数量的差异若超过 1，须在 `issues` 中以 `[评分点覆盖]` 前缀报告（此项为警告，不单独触发 `passed=false`，供人工判断是漏映射还是有意合并）。
2. **目录层级结构**""",
        "SYSTEM 维度1",
    ),
    (
        """对当前目录进行质量审核。**注意**：当前目录以【参考一级目录】为强制骨架生成，一级目录数量由参考目录决定，**不要求与评分点一一对应**。审核时聚焦于评分点覆盖质量、层级结构完整性和标题专业性。

### 审核维度

1. **评分点覆盖（一票否决）**：检查每个评分点（尤其是权重≥5分的）的 `standard` 中关键要求是否在子目录中有对应体现。若某评分点核心要求完全未被任何章节覆盖，`passed` 为 `false`。**不检查**一级目录数量是否等于评分点数量。""",
        """对当前目录进行质量审核。**注意**：本软件的目录按"一级目录 ↔ 评分项"一一映射生成（允许 ±1 的合并/拆分浮动）。审核时聚焦于评分点覆盖质量、层级结构完整性和标题专业性，并核对一级目录数量与评分点数量是否基本一致。

### 审核维度

1. **评分点覆盖（一票否决）**：检查每个评分点（尤其是权重≥5分的）`standard` 中关键要求是否在子目录中有对应体现。若某评分点核心要求完全未被任何章节覆盖，`passed` 为 `false`。**同时核对数量**：一级目录数量与评分点数量的差异若超过 1，须在 `issues` 中以 `[评分点覆盖]` 前缀报告（警告级，不单独触发 `passed=false`）。""",
        "USER 范式说明+维度1",
    ),
    (
        """4. **参考模板**：参考模板中的章节可作为框架参考，但以评分要求为核心依据""",
        """4. **参考模板**：参考模板中的章节仅作章节体系与命名风格的参考，**不得据此改变章节数量**——章节数量始终等于评分项数量""",
        "L1 系统提示词规则4",
    ),
])


# ===========================================================================
# 4. sse_outline.py —— 静态清理
# ===========================================================================
SSE = "app/routers/sse/sse_outline.py"

apply_blocks(SSE, [
    (
        """import asyncio
import functools
import hashlib
import json
import logging
import time
from typing import Any
from fastapi import HTTPException, Query
from starlette.responses import StreamingResponse
from ...db import get_db
from ...services.ai.provider_factory import get_ai_config
from ...models import SectionUpdate
from ...services.ai.industry_profiles import get_industry_profile
from ...services.ai.workflows import generate_outline as _run_outline_workflow
from ...services.ai.workflows import generate_sections as _run_sections
from ...services.files import latest_extracted_text
from ...services.generation_manager import generation_manager
from ...services.projects import update_step
from ...services.redis_cache import CacheService
from ...services.sections import (

    _count_words,

    _invalidate_build_tree_cache,

    build_tree,

    get_subtree_leaf_ids,

    update_section,

)
from ...services import task_lock as _task_lock
from ...services import task_snapshot
from ...services.task_registry import TaskStatus, task_registry
from ...services.tender_context import extract_tender_context_async
from ...services.v51_state import (

    clear_v51_state_from_db,

    create_v51_state,

    load_v51_state_from_db,

    remove_v51_state,

    save_v51_state_to_db,

    set_v51_active_run,

)
from ..task_control import _cancel_events, _cancel_events_lock, _cancel_tasks

from .sse_db_ops import _clear_and_save_outline, _extract_project_context, _extract_project_context_with_retry, _save_and_finalize_outline
from .sse_shared import SSE_TOTAL_TIMEOUT_DEFAULT, _NotifyQueue, logger
from .sse_utils import _try_get_provider, _try_get_provider_with_retry""",
        """import asyncio
import functools
import json
import time
from fastapi import HTTPException
from starlette.responses import StreamingResponse
from ...services.ai.workflows import generate_outline as _run_outline_workflow
from ...services import task_lock as _task_lock
from ...services import task_snapshot
from ...services.task_registry import TaskStatus, task_registry
from ...services.v51_state import (

    clear_v51_state_from_db,

    create_v51_state,

    load_v51_state_from_db,

    remove_v51_state,

    save_v51_state_to_db,

    set_v51_active_run,

)
from ..task_control import _cancel_events_lock, _cancel_tasks

from .sse_db_ops import _extract_project_context_with_retry, _save_and_finalize_outline
from .sse_shared import SSE_TOTAL_TIMEOUT_DEFAULT, _NotifyQueue, logger
from .sse_utils import _try_get_provider, _try_get_provider_with_retry""",
        "导入块精简",
    ),
])

# 孤立 time.time() 死语句 + 冗余 nonlocal（内容定位，不依赖行号）
apply_statement_deletes(SSE, [
    (lambda l: l.strip() == "time.time()", 3, "孤立 time.time()"),
    (lambda l: l.strip() == "nonlocal outline_finished", 1, "冗余 nonlocal"),
])


print()
if FAILED:
    print("存在失败项: %s" % ", ".join(FAILED))
    sys.exit(1)
print("全部补丁应用成功")
