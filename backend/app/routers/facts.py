"""全局事实变量（Global Facts）路由

功能：
- 从项目摘要/评分点中提取关键事实变量
- 人工编辑、确认事实变量
- 正文生成时自动注入事实变量提示词
- 正文生成后一致性审计（检测事实一致性）
"""

import asyncio
import json
import logging
import re
import time
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException

from pathlib import Path

from ..db import get_db
from ..config import PROJECTS_DATA_DIR
from ..models import FactsGenerateRequest, GlobalFactList
from ..security.auth import get_current_user
from ..services.facts_service import (
    DEFAULT_FACTS_POLICY,
    FACT_CATEGORIES,
    chapter_scope_enabled,
    chapter_subject_scope_block,
    coerce_audit_list,
    count_placeholders,
    dedup_key,
    dict_to_fact_items,
    extract_schedule_value,
    fact_extract_system_prompt,
    facts_segment_token_cap,
    group_fact_conflicts,
    is_placeholder_value,
    kb_patch_enabled,
    mark_placeholders,
    merge_kb_patch_items,
    merge_template_patch_items,
    normalize_fact_items,
    normalize_facts_policy,
    outline_input_enabled,
    parse_stored_items,
    PLACEHOLDER_VALUE_PREFIX,
    policy_rule_text,
    resolve_category,
    schedule_fallback_value,
    segmented_extraction_enabled,
    template_patch_enabled,
)
from ..services.content_field_contract import (
    compute_affected_section_ids,
    diff_key_field_changes,
    extraction_fields_record,
)
from ..services.files import get_facts_file_text, latest_extracted_text
from ..services.sections import invalidate_content_caches
from ..services.task_lock import get_active_lock, is_lock_live
from ..services.facts_metrics import inc, set_value


logger = logging.getLogger(__name__)


# ============================================================
# 命名空间统一（ENHANCE 2026-09-29 facts-ns）
# ------------------------------------------------------------
# 以下名称历史上在本文件内各自维护一份实现，与
# `services/ai/workflows_analysis.py`、`routers/sse/sse_utils.py`、`tasks/__init__.py`
# 的副本已经漂移（分类口径不一致 / 占位符泄漏 / 策略只认 fabricate）。
# 现在正本收敛到 `services/facts_service.py`，本文件只做 thin re-export，
# 既有 `from app.routers.facts import _normalize_facts` 等 import 路径全部保持可用。
#
# BUG-FIX 2026-10-15（TD-D 死 alias）：本块曾含 `_normalize_facts =
# normalize_fact_items`，但它被下方同名 `def _normalize_facts` 遮蔽成**永不可达
# 的死代码** —— 所有调用方实际都解析到 def，那一行只是误导读者的假 re-export。
# 删除后语义完全不变（模块级名字解析到 def），静态守卫测试
# tests/test_import_module_hardening_2026_10_15c.py::TestStaticGuards 钉住
# 「alias 不得被同名 def 遮蔽」这条约定，防同类死代码再次引入。
# ============================================================
_DEFAULT_FACTS_POLICY = DEFAULT_FACTS_POLICY
_DEFAULT_FACT_CATEGORIES = FACT_CATEGORIES
_policy_rule_text = policy_rule_text
_schedule_fallback_value = schedule_fallback_value
_coerce_fact_audit_list = coerce_audit_list
_dict_to_fact_items = dict_to_fact_items


def _generate_default_facts(
    summary: str,
    scoring_points: str,
    policy: str = _DEFAULT_FACTS_POLICY,
) -> list:
    """当 AI 提取失败时,使用启发式规则从摘要和评分点生成默认事实变量

    通过正则表达式匹配常见模式来提取关键信息

    BUG-FIX 2026-10-15（F-5 兜底绕过缺失值策略）：旧实现无任何事实时硬编码
    两条 ``source="default"`` 的兜底项 —— ``projectName="待确认"`` 与
    ``projectType="建筑工程"``，两者都绕过本模块的三档策略契约：

      1. ``is_placeholder_value`` 只认 ``PLACEHOLDER_VALUE_PREFIX``（【待填写）
         前缀，所以 ``待确认`` **不被识别为占位**：不计入 ``placeholder_count``、
         不打 ``needs_manual_input``、更不被正文生成的占位过滤剔除 —— 它会被当成
         真实项目名原样注入全篇提示词，成为一处稳定的虚构来源；
      2. ``projectType="建筑工程"`` 是**未经资料佐证的断言**，用户选 ``omit``
         （不编造）/``placeholder``（留占位）时仍会拿到，等于策略选择形同虚设。

    现按策略产出：projectName 在**所有**策略下都是占位（项目名无从得知，
    写成占位会被下游过滤掉，比注入一个假名字安全）；projectType 在 ``omit``
    下不产出、``placeholder`` 下写成占位、``fabricate`` 下保持历史默认值
    （向后兼容，历史部署不改变行为）。
    """
    facts = []
    text = summary + " " + scoring_points

    # 1. 项目名称 (支持多种格式)
    name_match = re.search(r"(?:工程名称|项目名称)\s*[:：*]*\s*[:：]?\s*([^\n*]+)", text)
    if not name_match:
        name_match = re.search(r"(?:项目)?(?:名称|工程)\s*[:：*]*\s*[:：]?\s*([^\n*]+)", text)
    if not name_match:
        # 尝试从标题行提取：如 "**1. 项目名称和基本信息**" 后面的内容
        title_match = re.search(r"项目名称.*?[：:]\s*(.+)", text)
        if title_match:
            name_match = title_match
    if name_match:
        val = name_match.group(1).strip().replace("**", "").strip("*").strip()
        facts.append(
            {
                "key": "projectName",
                "label": "项目名称",
                "value": val,
                "category": "basic",
                "source": "heuristic",
            }
        )

    # 2. 项目地点
    location_matches = re.findall(r"(?:建设地点|项目地点|位于)[：:]\s*([^\n*]+)", text)
    if location_matches:
        facts.append(
            {
                "key": "projectLocation",
                "label": "建设地点",
                "value": location_matches[0].strip(),
                "category": "basic",
                "source": "heuristic",
            }
        )

    # 3. 项目规模 - 提取混凝土、钢筋等关键工程量
    concrete_matches = re.findall(r"C(\d+).*?([\d.]+)立方米", text)
    if concrete_matches:
        concrete_info = "; ".join([f"C{m[0]}: {m[1]}m³" for m in concrete_matches[:5]])
        facts.append(
            {
                "key": "concreteVolume",
                "label": "混凝土工程量",
                "value": concrete_info,
                "category": "technical",
                "source": "heuristic",
            }
        )

    steel_matches = re.findall(r"钢筋.*?([\d.]+)吨", text)
    if steel_matches:
        facts.append(
            {
                "key": "steelWeight",
                "label": "钢筋工程量",
                "value": f"{steel_matches[0]}吨",
                "category": "technical",
                "source": "heuristic",
            }
        )

    # 4. 工程类型
    if "钢结构" in text:
        facts.append(
            {
                "key": "projectType",
                "label": "工程类型",
                "value": "包含钢结构工程",
                "category": "basic",
                "source": "heuristic",
            }
        )

    if "防水" in text:
        facts.append(
            {
                "key": "waterproofRequired",
                "label": "防水要求",
                "value": "包含地下防水、屋面防水",
                "category": "technical",
                "source": "heuristic",
            }
        )

    # 5. 从评分点提取质量、工期要求
    quality_matches = re.findall(r"质量.*?(?:要求|标准|等级)[：:]\s*([^\n*]{5,50})", text)
    if quality_matches:
        facts.append(
            {
                "key": "qualityRequirement",
                "label": "质量要求",
                "value": quality_matches[0].strip(),
                "category": "quality",
                "source": "heuristic",
            }
        )

    schedule_matches = re.findall(r"工期.*?(?:要求|计划|天数)[：:]\s*([^\n*]{5,50})", text)
    if schedule_matches:
        # key/label 对齐正本（normalize_fact_items 兜底与全库一致用 scheduleDuration/建设工期）：
        # 此前启发式独用 scheduleRequirement，跨来源（AI↔启发式↔人工）同义工期因 key 不同
        # 无法被 dedup_key 合并，一致性审计会在同 schedule 类内比对两种写法而误报冲突。
        facts.append(
            {
                "key": "scheduleDuration",
                "label": "建设工期",
                "value": schedule_matches[0].strip(),
                "category": "schedule",
                "source": "heuristic",
            }
        )

    # 6. 添加通用基础事实(如果都没有提取到)
    if not facts:
        # BUG-FIX 2026-10-15（F-5）：兜底值必须落在本模块的占位契约内（见函数
        # docstring）。projectName 一律写成可被 is_placeholder_value 识别的占位串，
        # 使其被计入 placeholder_count、并在正文生成的占位过滤中被剔除；
        # projectType 按策略决定「不产出 / 占位 / 历史默认值」。
        facts.append(
            {
                "key": "projectName",
                "label": "项目名称",
                "value": f"{PLACEHOLDER_VALUE_PREFIX}：项目名称】",
                "category": "basic",
                "source": "default",
            }
        )
        if policy != "omit":
            # omit = 不编造：不产出未经资料佐证的工程类型断言。
            facts.append(
                {
                    "key": "projectType",
                    "label": "工程类型",
                    "value": (
                        f"{PLACEHOLDER_VALUE_PREFIX}：工程类型】"
                        if policy == "placeholder"
                        else "建筑工程"
                    ),
                    "category": "basic",
                    "source": "default",
                }
            )

    return facts


# 时间类事实判定、「必须有工期」兜底、分类推断的正本：
# `app/services/facts_service.py`（_dict_to_fact_items 已在文件头做 thin re-export）


# 缺失值策略（三档 fabricate / omit / placeholder）的正本常量与工具同样位于
# `app/services/facts_service.py`，本文件通过文件头的 import 暴露
# `normalize_facts_policy` / `_policy_rule_text` / `_schedule_fallback_value`。


def _normalize_facts(items: list, fallback_text: str = "", policy: str = _DEFAULT_FACTS_POLICY) -> list:
    """值规范化（去重 + 必须有工期）—— 委派正本 `facts_service.normalize_fact_items`。

    ENHANCE 2026-09-29（facts-ns）：实现已上收到服务层，与 Step02 自动提取链路
    （`workflows_analysis.extract_global_facts`）共用同一规则，不再两份副本漂移。
    保留本函数名是为了兼容既有调用方与测试的 import 路径。
    """
    return normalize_fact_items(items, fallback_text=fallback_text, policy=policy)


_extract_schedule_value = extract_schedule_value


_coerce_fact_audit_list = coerce_audit_list
"""审计结果包装解包（正本在 facts_service）。原 BUG-FIX 2026-09-18（中）：
模型常用 {"items":[...]} 包装，只接受裸 list 会整体丢弃→前端把「0 条问题」
误读为「全部一致」。"""


def _safe_json_obj(raw: Any) -> Any:
    """把列里的 JSON 文本安全解析成对象；坏数据返回 None（不抛错）。"""
    if not raw:
        return None
    if isinstance(raw, (list, dict)):
        return raw
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        logger.warning("facts 列内 JSON 不可解析，已降级为 None: 前50字=%r", str(raw)[:50])
        return None


def _persist_audit(project_id: int, payload: dict) -> bool:
    """审计结果落库（upsert）；返回是否真正写入。

    BUG-FIX 2026-10-04（旧库兼容）：GET /facts 对 facts_audit_* 缺列已按列名
    集合降级，但写入侧此前是裸 INSERT —— 旧库未跑迁移时每次审计都抛
    no-such-column，审计结果永久不可回看。先 PRAGMA 判列，缺列时跳过落库
    （不失败：本次报告仍正常返回，只失去可回看性）。提出为模块级函数
    以便「读写两侧缺列容忍对称」可被单独测试钉住。
    """
    with get_db() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(analysis)").fetchall()}
        if "facts_audit_json" not in cols or "facts_audit_at" not in cols:
            logger.warning("analysis 表缺 facts_audit_* 列，本次审计结果不落库（仅随响应返回）")
            return False
        conn.execute(
            "INSERT INTO analysis (project_id, facts_audit_json, facts_audit_at) "
            "VALUES (?, ?, datetime('now', 'localtime')) "
            "ON CONFLICT(project_id) DO UPDATE SET "
            "facts_audit_json = excluded.facts_audit_json, "
            "facts_audit_at = excluded.facts_audit_at",
            (project_id, json.dumps(payload, ensure_ascii=False)),
        )
        return True


async def _ensure_project_access(project_id: int, current_user: Any) -> None:
    """校验当前用户是否有权访问指定项目。

    BUG-FIX 2026-09-18（高·横向越权）：facts 路由此前只在 router 级挂了
    `Depends(get_current_user)`（仅验证「已登录」），**所有端点都不做项目归属校验**；
    而 sections 路由的每个写端点都会调用 `require_project_permission`。
    后果：任意登录用户都能读取、覆盖、删除他人项目的事实变量，并触发他人项目的
    AI 审计（消耗其配额）。这里复用 sections 路由已有的实现，保证两处口径一致
    （延迟导入以避免路由模块之间的循环依赖）。
    """
    from .sections import require_project_permission

    await require_project_permission(project_id, current_user)


router = APIRouter(prefix="/projects/{project_id}/facts", tags=["facts"], dependencies=[Depends(get_current_user)])


# 分类命名空间 `_DEFAULT_FACT_CATEGORIES` 的正本是
# `facts_service.FACT_CATEGORIES`（已在文件头 alias）。这里不再重复定义。


async def _ensure_no_ai_task_running(project_id: int) -> None:
    """事实写入的任务运行中防护（409）。

    BUG-FIX 2026-09-29（并发竞态 / 缓存未失效）：sections 路由每个写端点都会
    调用 `_reject_if_generation_running`，而 facts 路由一直是裸的：
      ① 正文生成（content-generation）进行中时重新提取/保存事实，生成循环
         已把旧事实读进内存并逐章注入 prompt，新事实只在后半部分章节生效
         → 同一篇标书前半用旧工期、后半用新工期，一致性审计反而报矛盾；
      ② 目录生成会整表重建 sections，此时改事实等于在未定稿的结构上改基准。
    与 sections 同口径：锁行 sink 到 sqlite，跨进程可见；只读查询投线程，
    避免在 async 路由里阻塞事件循环。
    """

    def _check() -> str:
        lock = get_active_lock("content-generation", project_id=project_id)
        if not lock or not is_lock_live(lock.get("lock_key", "")):
            return ""
        from ..services.task_lock import TASK_LABELS

        return TASK_LABELS.get(lock.get("task_type", ""), lock.get("task_type") or "AI 生成任务")

    label = await asyncio.to_thread(_check)
    if label:
        raise HTTPException(
            status_code=409,
            detail=f"当前正在执行「{label}」，事实变量已锁定，请任务结束后再修改或重新提取。",
        )


@router.get("")
async def get_facts(project_id: int, current_user: Annotated[Any, Depends(get_current_user)] = None):
    """获取项目的事实变量列表（含上次使用的缺失值策略）"""
    await _ensure_project_access(project_id, current_user)

    # PERF-FIX 2026-09-18：本文件所有 async 路由内直连同步 sqlite 会阻塞
    # 事件循环（同项目其余 SSE/请求全部卡顿），统一改投线程执行。
    def _read_row():
        with get_db() as conn:
            return conn.execute(
                "SELECT global_facts_json, confirmed_facts, "
                "COALESCE(facts_policy, 'fabricate') AS facts_policy, "
                "facts_audit_json, facts_audit_at "
                "FROM analysis WHERE project_id = ?",
                (project_id,),
            ).fetchone()

    row = await asyncio.to_thread(_read_row)

    if not row:
        return {"items": [], "confirmed": False, "missing_value_policy": _DEFAULT_FACTS_POLICY}

    # 旧库可能还没跑过 facts_audit_* 迁移（SELECT 里用了 COALESCE 兜底列存在的情况），
    # 所以取列名集合判存在性，缺列时按 None 处理而不是抛 IndexError。
    cols = set(row.keys())
    # BUG-FIX 2026-09-29（读取口径不统一）：此处以前自己 `json.loads` + 只捕
    # JSONDecodeError，与正文生成链路（`facts_service.parse_stored_items`）不同口径。
    # 包装形态/坏数据下两边会看到不一样的事实集，表达为「前端有事实但正文没注入」。
    items = parse_stored_items(row["global_facts_json"])
    confirmed = bool(row["confirmed_facts"])

    return {
        "items": items,
        "confirmed": confirmed,
        # ENHANCE 2026-09-15：让前端回显上次用的策略，避免用户以为一直是默认档
        "missing_value_policy": normalize_facts_policy(row["facts_policy"]),
        "placeholder_count": count_placeholders(items),
        # ENHANCE 2026-09-29：上一次一致性审计结果随列表回传，刷新页面不再丢失审计记录
        # （旧实现只在前端内存里，刷新即没；用户重复点审计反复烧 AI 配额）
        "last_audit": _safe_json_obj(row["facts_audit_json"]) if "facts_audit_json" in cols else None,
        "audited_at": row["facts_audit_at"] if "facts_audit_at" in cols else None,
        # ENHANCE 2026-09-29：下发分类中文名，前端不再自己维护第二份映射表
        # （旧 catMap 缺 implementation，直接显示英文；现在以后端 FACT_CATEGORIES 为基准）
        "categories": dict(FACT_CATEGORIES),
        # ENHANCE 2026-10-06（模块内部一致性）：同步回传事实集内部的互斥取值矛盾
        # （同 label 不同 key/不同值），前端可据此高亮，避免「张伟/李强并存」流入正文。
        "fact_conflicts": group_fact_conflicts(items),
    }


@router.post("")
async def save_facts(project_id: int, body: GlobalFactList, current_user: Annotated[Any, Depends(get_current_user)] = None):
    """保存事实变量（人工编辑后的全量覆盖）"""
    await _ensure_project_access(project_id, current_user)
    # BUG-FIX 2026-09-29（并发竞态）：生成进行中不得覆盖事实基准
    await _ensure_no_ai_task_running(project_id)

    items = [f.model_dump() for f in body.items]
    # BUG-FIX 2026-10-04（静默丢行）：无 key 的行在下游 `dedup_key` 返回空串，
    # 「重新提取」时被 dedup_facts 整体丢弃；而手工保存直接入库，两条链路行为
    # 分叉。前端「新增事实」入口上线后空标识成为现实路径，写入侧显式拒绝，
    # 不靠用户碰运气发现丢行（库里现存数据不受影响，仅新保存需满足）。
    _no_key = [i for i in items if not str(i.get("key") or "").strip()]
    if _no_key:
        raise HTTPException(
            status_code=400,
            detail=f"有 {len(_no_key)} 条事实缺少标识（key），请为每条事实填写唯一英文标识后保存",
        )
    # BUG-FIX 2026-10-04（重复事实 / 跨模块一致性）：提取链路有 `dedup_facts`
    # 兜底，手工保存链路此前没有 —— 同一 key 两行不同值能直接落库：
    #   ① `parse_stored_items` 原样返回两行，SSE 按序注入两条互斥事实；
    #   ② `consistency_auditor` 把两值互比，「重新提取」又静默合并成一个，
    #      用户看到的行为随入口不同而不同。手工编辑界面里重复是明确错误，
    #      显式 400 好过静默丢行（与「不报假成功」的模块口径一致）。
    _seen_keys: set[str] = set()
    _dups: list[str] = []
    for item in items:
        fk = dedup_key(item)
        if fk and fk in _seen_keys and fk not in _dups:
            _dups.append(fk)
        _seen_keys.add(fk)
    if _dups:
        raise HTTPException(
            status_code=400,
            detail="存在标识重复的事实条目：" + "、".join(_dups[:5]) + "，请合并或删除后再保存",
        )
    # 前后端契约修复：前端在用户改掉【待填写】后不一定会清 needs_manual_input，
    # 服务端以 value 为准：不是占位串就清掉标记，避免旧角标永久赖在 UI 上。
    for item in items:
        if is_placeholder_value(item.get("value")):
            item["needs_manual_input"] = True
        else:
            item.pop("needs_manual_input", None)
        # BUG-FIX 2026-09-29（分类缺失）：GlobalFact.category 有默认值 basic，
        # 调用方不传分类时会直接写进库里 —— 于是前端分组丢、审计不送该事实、
        # `consistency_auditor` 也拿不到它作为已确认基准。按 key + label 补推断。
        item["category"] = resolve_category(
            item.get("category"), item.get("key", ""), item.get("label", "")
        )
    facts_json = json.dumps(items, ensure_ascii=False)

    # ENHANCE 2026-10-07（结构化提取项目字段表落库）：随事实同步派生 13 项
    # 结构化字段表（含溯源），正文生成取数时经 extraction_fields 显式直供
    # 通道消费 —— 运行时不再重复跑启发式派生，字段表可审计、可回显。
    _ef_record = extraction_fields_record(items)
    _ef_json = json.dumps(_ef_record, ensure_ascii=False)

    # ENHANCE 2026-10-07（字段级定向重生成，需求四.4）：
    # 保存前后比对关键一致性字段（工期/质保期/人员数量/设备型号等），
    # 圈定引用了被变更字段的章节，随响应回传 —— 前端可按既有
    # generate_content_impl(regenerate_ids=...) 对受影响章节精确重生成，
    # 不必整册重跑；全量缓存失效（下方）保持不变作为安全基线。
    _field_propagation: dict = {"changed": {}, "affected": {"section_ids": [], "chapters": [], "global_change": False}}

    def _save():
        with get_db() as conn:
            _old_row = conn.execute(
                "SELECT global_facts_json FROM analysis WHERE project_id = ?",
                (project_id,),
            ).fetchone()
            _old_items = parse_stored_items(_old_row["global_facts_json"]) if _old_row else []
            # BUG-FIX 2026-09-14（高）：analysis 行只在 ai.py 的"保存项目概述/评分点"
            # 流程里创建。用户若跳过概述直接编辑/提取事实变量，这里 UPDATE 影响 0 行，
            # 接口仍返回 {"ok": true}，前端提示"已保存"，刷新后数据全没了
            # （且后续正文生成拿不到任何事实）。改为 upsert。
            conn.execute(
                "INSERT INTO analysis (project_id, global_facts_json, extraction_fields_json, "
                "confirmed_facts, facts_audit_json, facts_audit_at) VALUES (?, ?, ?, 0, '', NULL) "
                "ON CONFLICT(project_id) DO UPDATE SET "
                "global_facts_json = excluded.global_facts_json, "
                "extraction_fields_json = excluded.extraction_fields_json, "
                "confirmed_facts = 0, "
                # BUG-FIX 2026-09-29（缓存未失效）：人工保存会全量覆写事实集，
                # 旧审计是针对另一批值的结论，继续留在库里会被 GET /facts 回看成
                # 「本次事实的一致性结果」，直接误导确认决策。一并抹除。
                #
                # 约定「事实基准 global_facts_json 变更 → confirmed_facts 回退 +
                # facts_audit_* 抹除」共 4 处强制点，改口径须四处同改：
                #  ① 本处 POST /facts 手工保存（本节）
                #  ② routers/facts.py 重新提取端点（下方 _persist，同一口径）
                #  ③ services/consistency_backflow.py accept_fact_proposals（审计回流采纳）
                #  ④ routers/ai.py _sync_reset_facts_audit（清除分析 / 重跑评分）
                "facts_audit_json = '', facts_audit_at = NULL",
                (project_id, facts_json, _ef_json),
            )
            # ENHANCE 2026-10-19（岳阳路事故·页眉修复）：项目名占位回填。
            # 前端快速建项目时默认命名为「招投标项目 <时间戳>」，分析提取出
            # projectName 事实后从不回写 → DOCX 页眉/封面长期显示占位名。
            # 现在保存事实时检测：项目名仍是占位模式且事实里有真实 projectName
            # 则自动回写 projects.name（用户改过名则尊重用户命名，不覆盖）。
            try:
                _pname = ""
                for _it in items:
                    if isinstance(_it, dict) and str(_it.get("key") or "") in (
                        "projectName",
                        "project_name",
                        "项目名称",
                    ):
                        _pname = str(_it.get("value") or "").strip()
                        break
                if _pname:
                    _DEFAULT_NAME_RE = re.compile(
                        r"^(招投标项目|未命名项目|新建项目)\s*\d{4}[-/年]"
                    )
                    _row_name = conn.execute(
                        "SELECT name FROM projects WHERE id = ?", (project_id,)
                    ).fetchone()
                    _cur_name = str((_row_name["name"] if _row_name else "") or "").strip()
                    if _cur_name != _pname and (
                        not _cur_name or _DEFAULT_NAME_RE.match(_cur_name)
                    ):
                        conn.execute(
                            "UPDATE projects SET name = ?, updated_at = datetime('now','localtime') "
                            "WHERE id = ?",
                            (_pname, project_id),
                        )
                        logger.info(
                            "[事实保存] 项目名占位回填: %r → %r (project=%d)",
                            _cur_name, _pname, project_id,
                        )
            except Exception as _pn_e:
                logger.warning("[事实保存] 项目名回填失败（不影响保存本身）: %s", _pn_e)
            # ENHANCE 2026-10-06（设计文档 8.1「保存后清空旧正文生成缓存」）：
            # 事实基准已全量覆写，若旧正文缓存不失效，用户改完事实重生成时仍命中
            # 旧章节内容，造成「改了事实但正文前后仍不一致」的隐性回归。失效三层
            # 正文缓存，使重新生成以新事实为唯一基准（函数内部已逐层 try/except，
            # 单层失效失败不影响其余层与本次保存本身）。
            invalidate_content_caches(project_id)
            # 字段级定向重生成：差异比对 + 受影响章节圈定（失败不影响保存本身）
            try:
                _field_propagation["changed"] = diff_key_field_changes(_old_items, items)
                if _field_propagation["changed"]:
                    _section_rows = conn.execute(
                        "SELECT id, title FROM sections WHERE project_id = ?",
                        (project_id,),
                    ).fetchall()
                    _field_propagation["affected"] = compute_affected_section_ids(
                        list(_field_propagation["changed"].keys()), _section_rows
                    )
            except Exception as _fp_e:
                logger.warning("[事实保存] 字段变更传播计算失败（不影响保存）: %s", _fp_e)
                _field_propagation["changed"] = {}

    await asyncio.to_thread(_save)
    # ENHANCE 2026-10-06（保存链路冲突反馈）：手工保存此前只判 key 重复/空 key，
    # 同 label 不同 key 的内部矛盾（如 projectManager=张伟 与 pm=李强）能静默落库，
    # 用户要等到 GET /facts 回读或正文生成后才发现。保存响应即时回传冲突分组，
    # 前端据此弹告警，不必等刷新。
    return {
        "ok": True,
        "fact_conflicts": group_fact_conflicts(items),
        # ENHANCE 2026-10-07：字段级定向重生成 —— 关键字段变更清单与受影响章节，
        # 前端可据 affected.section_ids 走 regenerate_ids 精确重生成受影响章节。
        "field_changes": _field_propagation["changed"],
        "affected_sections": _field_propagation["affected"],
        # ENHANCE 2026-10-07（结构化字段表）：随保存回显，前端可渲染字段溯源
        "extraction_fields": _ef_record["fields"],
        "extraction_fields_provenance": _ef_record["provenance"],
    }


@router.post("/generate")
async def generate_facts(project_id: int, body: FactsGenerateRequest | None = None, current_user: Annotated[Any, Depends(get_current_user)] = None):
    """AI 提取全局事实变量（优先使用事实变量文件，未找到时降级使用招标文件全文）

    事实变量涵盖六大类：
    1. 项目人员角色、姓名、职责
    2. 工期、运维期、交货时间
    3. 技术路线、设备型号、统一配置
    4. 服务承诺、响应口径
    5. 实施做法、执行安排
    6. 其他确定性项目事实（名称、地点、规模等）

    ENHANCE 2026-09-15：新增 `missing_value_policy`（fabricate / omit / placeholder）。
    历史实现只有一档语义（资料没给值就由 AI 编一个"合理值"），而虚构的人名、
    工期、金额会作为确定性事实被注入全篇正文 —— 对投标这种要担责的文书风险很大。
    现在可由用户显式选择"不编造"的两档。

    ENHANCE 2026-10-16（P2-4）：`FACTS_TEMPLATE_PATCH_ENABLED` 开启时，基线提取后
    再从项目已上传的模板/既有方案文件做一次「只补不增」的补充提取（对齐易标
    `runOriginalPlanGlobalFactPatches`），结果计入 `template_patches_applied`。
    """
    policy = normalize_facts_policy(body.missing_value_policy if body else None)
    _t0 = time.monotonic()  # PERF 2026-10-17: 提取耗时指标
    await _ensure_project_access(project_id, current_user)
    # BUG-FIX 2026-09-29（并发竞态）：提取会全量覆写 global_facts_json 并把
    # confirmed_facts 打回 0，生成进行中执行它等于中途换掉正文的事实基准。
    await _ensure_no_ai_task_running(project_id)
    # 优先查找上传的事实变量文件（含"事实"/"global fact"/"fact"关键词）
    facts_text = await asyncio.to_thread(get_facts_file_text, project_id)
    if facts_text and facts_text.strip():
        logger.info("[AI事实] 使用事实变量文件进行分析，长度: %d 字符", len(facts_text))
        source_text = facts_text
    else:
        # 降级：使用招标文件全文
        full_text = await latest_extracted_text(project_id)
        if not full_text:
            raise HTTPException(status_code=400, detail="请先上传文件")
        logger.info(
            "[AI事实] 未找到事实变量文件，降级使用招标文件全文进行分析，长度: %d 字符",
            len(full_text),
        )
        source_text = full_text

    def _read_ctx():
        with get_db() as conn:
            return conn.execute(
                "SELECT summary_json, scoring_points_json, work_interface_json, construction_method_json "
                "FROM analysis WHERE project_id = ?",
                (project_id,),
            ).fetchone()

    row = await asyncio.to_thread(_read_ctx)

    summary = row["summary_json"] if row else ""
    scoring_points = row["scoring_points_json"] if row else ""
    work_interface = row["work_interface_json"] if row else ""
    construction_method = row["construction_method_json"] if row else ""

    # R-2 2026-10-12：统一「取 provider → 调 collect_json_response」链路，
    # 消除本文件与 routers/templates.py 各自内联该组合的重复。
    # provider **仍在下方 try 之外构造**：原语义是「AI 配置故障直接 500」，
    # 若把构造挪进 try，配置异常会被启发式兜底静默吞掉、向用户谎报成功。
    from ..services.ai.extraction_runner import (
        create_provider,
        get_ai_config,
        run_json_extraction,
        AIMessage,
    )

    ai_config = get_ai_config()
    provider = create_provider(ai_config)

    # 事实提取提示词正本：`facts_service.fact_extract_system_prompt`
    # ENHANCE 2026-09-29（facts-ns 去重）：本文件与 workflows_analysis.extract_global_facts
    # 曾各自维护一份完整提示词，导致「手动提取按三档策略，评分流程自动提取永远按
    # fabricate」。现在两处共用同一函数，策略作为唯一参数驱动行为差异。
    extract_sys = fact_extract_system_prompt(policy)

    # 多源融合：核心来源(事实文件/招标全文) + Step02 结构化解析结果 + 摘要/评分点
    # 设计文档：招标文件(核心) → Step02关键解析结果 → 目录 → 知识库，后序来源作为补充
    step02_parts = []
    if summary:
        step02_parts.append(f"【项目信息摘要】\n{summary[:1200]}")
    if work_interface and work_interface != "[]":
        try:
            wi = json.loads(work_interface)
            if isinstance(wi, list) and wi:
                step02_parts.append(f"【工作界面/分工】\n{json.dumps(wi[:8], ensure_ascii=False)}")
        except json.JSONDecodeError:
            pass
    if construction_method and construction_method != "[]":
        try:
            cm = json.loads(construction_method)
            if isinstance(cm, list) and cm:
                step02_parts.append(
                    f"【实施做法/施工方法】\n{json.dumps(cm[:8], ensure_ascii=False)}"
                )
        except json.JSONDecodeError:
            pass

    # P1-2（facts-ns 2026-10-14，对齐易标「先目录后事实」）：目录标题树作为提取
    # 输入——目录只约束事实的大项覆盖范围，不是取值来源（取值仍以上述文档为准）。
    # 开关 FACTS_OUTLINE_INPUT_ENABLED 默认关=不读目录，行为不变。
    outline_block = ""
    if outline_input_enabled():
        def _read_outline_titles():
            # BUG-FIX 2026-10-28（P3 性能）：纯 SELECT 用只读连接，不抢全局写锁
            # （批量上传/保存高峰期不再与 save_upload 排队）。
            with get_db(readonly=True) as conn:
                return conn.execute(
                    "SELECT title, level FROM sections WHERE project_id = ? "
                    "ORDER BY sort_order, id",
                    (project_id,),
                ).fetchall()
        try:
            _outline_rows = await asyncio.to_thread(_read_outline_titles)
            _o_lines: list[str] = []
            _o_budget = 1200  # 字符预算：目录行信息密度低，超预算截断
            for _or in _outline_rows or []:
                _title = str(_or["title"] or "").strip()
                if not _title:
                    continue
                try:
                    _lvl = max(1, int(_or["level"] or 3))
                except (TypeError, ValueError):
                    _lvl = 3
                _line = f"{'  ' * (_lvl - 1)}- {_title[:60]}"
                if _o_budget - len(_line) < 0:
                    break
                _o_budget -= len(_line)
                _o_lines.append(_line)
            if _o_lines:
                outline_block = (
                    "\n\n### 技术方案目录（事实大项覆盖范围参考，不是取值来源）\n"
                    + "\n".join(_o_lines)
                    + "\n（事实的分组与覆盖应能支撑目录各大章的正文写作；"
                    "目录中需要事实支撑的大项（如工期/人员/质保）应输出对应事实；"
                    "事实取值仍以上述文档为唯一依据，不得从目录标题反推具体值）"
                )
        except Exception as _ol_e:
            logger.warning("[AI事实] 目录输入加载失败（跳过，不影响提取）: %s", _ol_e)

    # P2-1（facts-ns 2026-10-15）：九大章节选题标准随「目录输入」开关一同启用，
    # 也可用 FACTS_CHAPTER_SCOPE_ENABLED 单独开启。目录用于确定大项覆盖范围，
    # 该块再按九大章节给出「每章必备事实类别」的选题提示（只决定选哪些项，
    # 不决定值）。开关默认关=不追加，提示词与历史逐字一致。
    chapter_scope_block = chapter_subject_scope_block() if chapter_scope_enabled() else ""

    extract_user = f"""请从以下项目文档中提取全局事实变量：

{source_text}
{outline_block}
{chapter_scope_block}
"""
    if step02_parts:
        extract_user += (
            "\n\n以下是 Step02 关键解析结果（作为补充事实来源，与文档冲突时以文档为准）：\n"
            + "\n\n".join(step02_parts)
        )
    if summary:
        extract_user += f"\n\n参考摘要：{summary[:800]}"
    if scoring_points:
        extract_user += f"\n参考评分点：{scoring_points[:800]}"

    extract_user += """

请提取所有可确定的事实变量，输出 JSON 对象（dict）。"""
    # 尾部补充来源（Step02/摘要/评分点+输出指令）单独抽出：分段提取时
    # 每个分段复用同一段尾部，与单段路径的提示词尾部完全一致。
    # BUG-FIX（2026-10-28 跨模块深度探索 F10）：`str.index` 在标记未命中时抛
    # ValueError → 直接 500。改用 `find` 带回退（标记恒在用户提示词末尾，未命中
    # 属异常态，回退到「取整个用户提示词」仍可安全降级，绝不中断提取）。
    _tail_marker = "\n\n以下是 Step02" if step02_parts else "\n\n请提取所有可确定的事实变量"
    _tail_start = extract_user.find(_tail_marker)
    if _tail_start < 0:
        _tail_start = extract_user.find("\n\n请提取所有可确定的事实变量")
    if _tail_start < 0:
        logger.warning("[AI事实] 提示词尾部标记未找到，回退整串作为尾部（提取继续）")
        _tail_start = 0
    extract_tail = extract_user[_tail_start:]

    # BUG-FIX 2026-09-14（中·可信度）：原先无论 AI 成功还是异常降级为正则启发式，
    # 接口都返回同一形态的结果，前端一律提示"事实变量已提取"——用户以为这些
    # 数值来自 AI 精准抽取，实际只是几条正则匹配，极易把错误事实带进正文。
    # 这里显式记录来源，并在返回值里带上 mode / warning 供前端区分展示。
    extraction_mode = "ai"
    extraction_note = ""

    # P1-1（facts-ns 2026-10-14，对齐易标分段提取管线）：超预算的招标全文按 token
    # 上限分段逐段提取、跨段归并，消除单次调用被静默截断导致后文事实丢失。
    # 开关 FACTS_SEGMENTED_EXTRACTION_ENABLED 默认关 → segments 恒为单元素 →
    # 走下方原有单次调用路径，提示词与行为逐字不变。
    segments = [source_text]
    if segmented_extraction_enabled():
        from ..services.ai.prompts import split_into_token_capped_chunks
        segments = split_into_token_capped_chunks(source_text, facts_segment_token_cap())

    try:
        if len(segments) > 1:
            # 分段路径：每段复用与单段路径完全一致的尾部补充来源（extract_tail），
            # 并显式告知「本分段没有提及不代表整份没有提及」，防止模型因段内
            # 未见某项而放弃提取。失败段跳过，全段失败则抛出走既有启发式兜底。
            items = []
            seen_keys: dict[str, int] = {}
            _failed_segments = 0
            for _idx, _seg in enumerate(segments, 1):
                _seg_user = (
                    f"请从以下项目文档中提取全局事实变量（这是第 {_idx}/{len(segments)} 个分段，"
                    "只提取本分段有明确依据的事实；本分段没有提及不代表整份文档没有提及，"
                    f"不要因某项信息未在本段出现而放弃提取）：\n\n{_seg}\n{outline_block}\n{chapter_scope_block}{extract_tail}"
                )
                try:
                    _seg_result, _ = await run_json_extraction(
                        [
                            AIMessage(role="system", content=extract_sys),
                            AIMessage(role="user", content=_seg_user),
                        ],
                        provider=provider,
                        temperature=0.3,
                        max_retries=2,
                    )
                    if isinstance(_seg_result, dict):
                        _seg_items = _dict_to_fact_items(_seg_result)
                    else:
                        _seg_items = _seg_result if isinstance(_seg_result, list) else []
                except Exception as _seg_e:
                    _failed_segments += 1
                    logger.warning(
                        "[AI事实] 第 %d/%d 分段提取失败（跳过该段，继续其余分段）: %s",
                        _idx, len(segments), _seg_e,
                    )
                    continue
                # 跨段归并：同 key 首次出现保留；已存在但值为占位符、新段给出
                # 实值时替换（占位符让位于有依据的确定值，与后处理去重口径一致）。
                for _it in _seg_items:
                    if not isinstance(_it, dict):
                        continue
                    _k = dedup_key(_it)
                    if _k in seen_keys:
                        _pos = seen_keys[_k]
                        _old = items[_pos]
                        if is_placeholder_value(_old.get("value", "")) and not is_placeholder_value(_it.get("value", "")):
                            items[_pos] = _it
                    else:
                        seen_keys[_k] = len(items)
                        items.append(_it)
            if _failed_segments == len(segments):
                raise RuntimeError(f"全部 {len(segments)} 个分段提取均失败")
            if _failed_segments:
                extraction_note = (
                    f"共 {len(segments)} 个分段，其中 {_failed_segments} 个分段提取失败已跳过，"
                    "结果可能不完整，请人工核对。"
                )
            if not items:
                logger.info("[AI事实] 分段提取归并后为空，使用启发式规则兜底")
                items = _generate_default_facts(summary, scoring_points, policy)
                extraction_mode = "heuristic"
                extraction_note = "AI 未提取到事实，以下为规则启发式结果，请逐条人工确认后再使用。"
        else:
            # R-2 2026-10-12：改走统一 runner（provider 由调用方预构造传入）。
            result, _ = await run_json_extraction(
                [
                    AIMessage(role="system", content=extract_sys),
                    AIMessage(role="user", content=extract_user),
                ],
                provider=provider,
                temperature=0.3,
                max_retries=2,
            )
            if isinstance(result, dict):
                items = _dict_to_fact_items(result)
            else:
                items = result if isinstance(result, list) else []

            if not items:
                logger.info("[AI事实] AI 提取返回空结果，使用启发式规则兜底")
                items = _generate_default_facts(summary, scoring_points, policy)
                extraction_mode = "heuristic"
                extraction_note = "AI 未提取到事实，以下为规则启发式结果，请逐条人工确认后再使用。"

    except Exception as e:
        logger.warning("[AI事实] 提取异常，使用启发式规则兜底: %s", e)
        inc("fact_extract_failed")
        items = _generate_default_facts(summary, scoring_points, policy)
        extraction_mode = "heuristic"
        extraction_note = f"AI 提取失败（{str(e)[:80]}），以下为规则启发式结果，请逐条人工确认后再使用。"

    # P2-2（facts-ns 2026-10-15，对齐易标 runKnowledgeGlobalFactPatches）：基线提取
    # 完成后从参考知识库做一次「只补不增」的补充提取。开关 FACTS_KNOWLEDGE_PATCH_ENABLED
    # 默认关 = 不读知识库，行为与历史一致；开启且基线 items 非空时：读知识库轻量条目 →
    # 二次提取 → merge_kb_patch_items 拼到基线之后 → 由下方 _normalize_facts 统一去重
    # （基线优先、基线占位/空值让位知识库实值）。任何异常都降级跳过，不阻断基线提取。
    kb_patch_tried = False
    if kb_patch_enabled() and items:
        try:
            from ..services import knowledge as _kb_svc

            _kb_items = _kb_svc.get_relevant_items(limit=30) or []

            def _render_kb_material(rows):
                _lines: list[str] = []
                _budget = 6000
                for _i, _it in enumerate(rows or [], 1):
                    _title = str(_it.get("title") or f"知识条目{_i}").strip()
                    _summary = str(_it.get("summary") or "").strip()
                    _content = str(_it.get("content") or "").strip()[:600]
                    _block = f"[条目{_i}] 标题：{_title}"
                    if _summary:
                        _block += f"\n摘要：{_summary[:200]}"
                    if _content:
                        _block += f"\n正文：{_content}"
                    if len(_block) > _budget:
                        break
                    _budget -= len(_block)
                    _lines.append(_block)
                return "\n\n".join(_lines)

            _kb_material = _render_kb_material(_kb_items)
            if _kb_material:
                _kb_sys = (
                    "你是招投标方案全局事实变量补充助手。请从参考知识库素材中提取"
                    "『技术方案正文需要统一采用』的确定性事实（人员、工期、设备型号、"
                    "质保期、响应时间、服务承诺、技术参数等）。"
                    "只提取素材中有明确依据的事实；素材未提到的严禁编造；"
                    "只返回 JSON 对象（dict），key 用英文驼峰，value 为字符串。"
                )
                _kb_user = f"""以下是参考知识库素材（历史项目文档/企业级规范，仅作事实补源）：

{_kb_material}

请从上述知识库素材中补充提取全局事实变量：
1. 只提取素材中有明确依据的确定性事实；
2. 与招标文件/Step02 解析结果冲突时，不要覆盖已确认的值（基线以招标为准）；
3. 素材没有明确值时严禁编造；没有可补充内容时返回空 JSON 对象 {{}}；
4. 输出 JSON 对象（dict），严禁附加解释。"""
                _kb_result, _ = await run_json_extraction(
                    [
                        AIMessage(role="system", content=_kb_sys),
                        AIMessage(role="user", content=_kb_user),
                    ],
                    provider=provider,
                    temperature=0.3,
                    max_retries=2,
                )
                if isinstance(_kb_result, dict):
                    _kb_patch = _dict_to_fact_items(_kb_result)
                elif isinstance(_kb_result, list):
                    _kb_patch = _kb_result
                else:
                    _kb_patch = []
                if _kb_patch:
                    items = merge_kb_patch_items(items, _kb_patch)
                    logger.info(
                        "[AI事实] 知识库补充候选 %d 条已并入基线（去重后以最终清单为准）",
                        len(_kb_patch),
                    )
                kb_patch_tried = True
        except Exception as _kb_e:
            logger.warning("[AI事实] 知识库补充失败（跳过，不影响基线提取）: %s", _kb_e)

    # P2-4（facts-ns 2026-10-16，对齐易标 runOriginalPlanGlobalFactPatches）：
    # 基线提取后从项目已上传的模板/既有方案文件再做一次「只补不增」的补充提取。
    # 开关 FACTS_TEMPLATE_PATCH_ENABLED 默认关 = 不读模板，行为与历史一致；
    # 开启且基线 items 非空时：读项目模板文件正文（预算截断）→ 二次提取 →
    # merge_template_patch_items 拼到基线之后 → 由下方 _normalize_facts 统一去重
    # （基线优先、基线占位/空值让位模板实值，来源标记 source=template）。
    # 任何异常都降级跳过，不阻断基线提取。
    template_patch_tried = False
    if template_patch_enabled() and items:
        try:
            from ..services.files import extract_text_from_path as _extract_tpl_text

            def _read_ready_templates():
                # BUG-FIX 2026-10-28（P3 性能）：纯 SELECT 用只读连接，不抢全局写锁。
                with get_db(readonly=True) as conn:
                    return conn.execute(
                        "SELECT id, filename, file_path FROM project_templates "
                        "WHERE project_id = ? AND status = 'ready' AND file_path != '' "
                        "ORDER BY id",
                        (project_id,),
                    ).fetchall()

            _tpl_rows = await asyncio.to_thread(_read_ready_templates)

            def _render_template_material(rows):
                _lines: list[str] = []
                _budget = 6000
                _tpl_root = PROJECTS_DATA_DIR / str(project_id) / "templates"
                for _i, _tr in enumerate(rows or [], 1):
                    _fp = str(_tr["file_path"] or "").strip()
                    if not _fp:
                        continue
                    # G1（2026-10-28 遗留加固）：模板补丁读取的 file_path 虽由
                    # template_expansion 写入（受控位置），但 DB 可能被手工改写或
                    # 历史数据可能存任意路径（用户绝对路径/UNC）。读取前做
                    # resolve + 包含校验，必须落在本项目 templates 目录内，
                    # 否则跳过并告警（fail-closed），杜绝项目间越权/任意文件读取。
                    try:
                        _fp_resolved = Path(_fp).resolve()
                        _root_resolved = _tpl_root.resolve()
                        if not _fp_resolved.is_relative_to(_root_resolved):
                            logger.warning(
                                "[AI事实] 模板 %s 路径不在本项目模板目录 %s 内，跳过（SSRF 路径守卫）",
                                _fp, _root_resolved,
                            )
                            continue
                    except Exception as _pe:
                        logger.warning(
                            "[AI事实] 模板 %s 路径解析失败，跳过（SSRF 路径守卫）: %s",
                            _fp, _pe,
                        )
                        continue
                    try:
                        _text = _extract_tpl_text(_fp)
                    except Exception as _tx_e:
                        logger.warning(
                            "[AI事实] 模板 %s 正文提取失败（跳过）: %s", _fp, _tx_e
                        )
                        continue
                    _text = str(_text or "").strip()
                    if not _text:
                        continue
                    _name = str(_tr["filename"] or f"模板{_i}").strip()
                    _block = f"[模板{_i}] 文件名：{_name}\n正文：{_text[:4000]}"
                    if len(_block) > _budget:
                        _block = _block[:_budget]
                    _budget -= len(_block)
                    _lines.append(_block)
                    if _budget <= 0:
                        break
                return "\n\n".join(_lines)

            _tpl_material = _render_template_material(_tpl_rows)
            if _tpl_material:
                _tpl_sys = (
                    "你是招投标方案全局事实变量补充助手。请从『既有方案/历史标书模板』"
                    "素材中提取『技术方案正文需要统一采用』的确定性事实（人员、工期、"
                    "设备型号、质保期、响应时间、服务承诺、技术参数、统一配置口径等）。"
                    "既有方案中已经落地的安排、既有承诺、统一配置优先沉淀为事实；"
                    "只提取素材中有明确依据的事实；素材未提到的严禁编造；"
                    "只返回 JSON 对象（dict），key 用英文驼峰，value 为字符串。"
                )
                _tpl_user = f"""以下是项目已上传的既有方案/模板素材（仅作事实补源）：

{_tpl_material}

请从上述既有方案素材中补充提取全局事实变量：
1. 只提取素材中有明确依据的确定性事实；原方案已写成的实际安排、既有承诺优先；
2. 与招标文件/Step02 解析结果冲突时，不要覆盖已确认的值（基线以招标为准）；
3. 素材没有明确值时严禁编造；没有可补充内容时返回空 JSON 对象 {{}}；
4. 输出 JSON 对象（dict），严禁附加解释。"""
                _tpl_result, _ = await run_json_extraction(
                    [
                        AIMessage(role="system", content=_tpl_sys),
                        AIMessage(role="user", content=_tpl_user),
                    ],
                    provider=provider,
                    temperature=0.3,
                    max_retries=2,
                )
                if isinstance(_tpl_result, dict):
                    _tpl_patch = _dict_to_fact_items(_tpl_result)
                elif isinstance(_tpl_result, list):
                    _tpl_patch = _tpl_result
                else:
                    _tpl_patch = []
                if _tpl_patch:
                    items = merge_template_patch_items(items, _tpl_patch)
                    logger.info(
                        "[AI事实] 既有方案补充候选 %d 条已并入基线（去重后以最终清单为准）",
                        len(_tpl_patch),
                    )
                template_patch_tried = True
        except Exception as _tpl_e:
            logger.warning("[AI事实] 既有方案补充失败（跳过，不影响基线提取）: %s", _tpl_e)

    # 值规范化后处理：去重 + 必须有工期规则（缺值兜底文案按策略生成）
    items = _normalize_facts(
        items, fallback_text=(summary or "") + (scoring_points or ""), policy=policy
    )

    # P2-2：知识库补充实际生效条数（规范化去重后 source=knowledge 的条目数）
    knowledge_patches_applied = 0
    if kb_patch_tried:
        knowledge_patches_applied = sum(
            1
            for _it in items
            if isinstance(_it, dict)
            and str(_it.get("source") or "").strip() == "knowledge"
        )

    # P2-4：既有方案（模板）补充实际生效条数（规范化去重后 source=template 的条目数）
    template_patches_applied = 0
    if template_patch_tried:
        template_patches_applied = sum(
            1
            for _it in items
            if isinstance(_it, dict)
            and str(_it.get("source") or "").strip() == "template"
        )

    # placeholder 策略下逐条打标，前端可高亮「这条还等着人工填」。
    # omit 策略不逐条打标 —— 它的产物是完整的笼统承诺，不需要逐条补填。
    # ENHANCE 2026-09-29：判定与打标统一走 `facts_service`，与 GET /facts、
    # 正文生成过滤同一口径（前缀匹配【待填写）。
    if policy == "placeholder":
        mark_placeholders(items)

    # 启发式来源：逐条打标，前端可据此给出"需确认"角标，
    # 避免用户把正则猜出来的数值当成已核实事实。
    if extraction_mode == "heuristic":
        for _it in items:
            if isinstance(_it, dict):
                _it["source"] = "heuristic"
                _it["verified"] = False

    facts_json = json.dumps(items, ensure_ascii=False)

    # ENHANCE 2026-10-07（结构化提取项目字段表落库）：重新提取链路与人工保存
    # 同一口径，随事实全量覆写结构化字段表。
    _ef_json = json.dumps(extraction_fields_record(items), ensure_ascii=False)

    def _persist():
        with get_db() as conn:
            # BUG-FIX 2026-09-14（高）：同上，analysis 行可能尚未创建，
            # 纯 UPDATE 会静默丢掉整批 AI 提取结果。改为 upsert。
            conn.execute(
                "INSERT INTO analysis (project_id, global_facts_json, extraction_fields_json, "
                "facts_policy, confirmed_facts, facts_audit_json, facts_audit_at) "
                "VALUES (?, ?, ?, ?, 0, '', NULL) "
                "ON CONFLICT(project_id) DO UPDATE SET "
                "global_facts_json = excluded.global_facts_json, "
                "extraction_fields_json = excluded.extraction_fields_json, "
                "facts_policy = excluded.facts_policy, "
                # 重新提取同样作废审计结果（与 POST /facts 同一口径）。
                # 约定共 4 处强制点（本处 + POST /facts + consistency_backflow.
                # accept_fact_proposals + routers/ai.py _sync_reset_facts_audit），
                # 改口径须四处同改，见 POST /facts 处的完整注释。
                "confirmed_facts = 0, facts_audit_json = '', facts_audit_at = NULL",
                (project_id, facts_json, _ef_json, policy),
            )
            # ENHANCE 2026-10-06（设计文档 8.1）：重新提取同样是事实基准的全量覆写，
            # 必须让旧正文缓存失效，否则已生成的章节会在新事实下继续被复用，出现
            # 「重新提取了工期，但旧正文仍写旧工期」的前后矛盾。
            invalidate_content_caches(project_id)

    await asyncio.to_thread(_persist)

    # PERF 2026-10-17: 记录提取耗时与条目数指标
    _elapsed_ms = int((time.monotonic() - _t0) * 1000)
    inc("fact_extract_runs")
    set_value("fact_extract_elapsed_ms", _elapsed_ms)
    inc("fact_extract_items_total", len(items))
    if knowledge_patches_applied:
        inc("fact_knowledge_patch_hits", knowledge_patches_applied)
    if template_patches_applied:
        inc("fact_template_patch_hits", template_patches_applied)

    return {
        "items": items,
        "confirmed": False,
        # ENHANCE 2026-10-06：返回事实集内部矛盾，前端可在提取后立即提示
        # （如 AI 抽出的工期与启发式兜底工期不一致），而非等正文生成后才暴露。
        "fact_conflicts": group_fact_conflicts(items),
        # BUG-FIX 2026-09-14：前端据此区分"AI 精准提取"与"规则启发式兜底"
        "extraction_mode": extraction_mode,
        "warning": extraction_note,
        # ENHANCE 2026-10-15（P2-2）：知识库补充实际生效条数（0=未启用或未补充）
        "knowledge_patches_applied": knowledge_patches_applied,
        # ENHANCE 2026-10-16（P2-4）：既有方案（模板）补充实际生效条数（0=未启用或未补充）
        "template_patches_applied": template_patches_applied,
        # ENHANCE 2026-09-15：回传本次采用的缺失值策略
        "missing_value_policy": policy,
        "placeholder_count": count_placeholders(items),
        # ENHANCE 2026-09-29：分类命名空间随结果回传，前端不再自己维护 catMap
        "categories": dict(FACT_CATEGORIES),
    }


@router.post("/confirm")
async def confirm_facts(project_id: int, current_user: Annotated[Any, Depends(get_current_user)] = None):
    """确认事实变量"""
    await _ensure_project_access(project_id, current_user)

    # BUG-FIX 2026-10-04（数据链断裂）：本端点此前是无条件置 1，不校验库里
    # 是否真有可用事实：空事实集/全占位也能被置为「已确认」，而下游
    # （Step02 评分流程的「已确认则跳过覆盖」保护、审计基准）都以此为开关，
    # 等于用一张空基准锁住了整条事实链。仅拦「无任何非占位事实」这一明确
    # 不可用形态；含占位仍允许确认（placeholder 策略下补值前确认是现有 UI
    # 流程，GET /facts 的 placeholder_count 已负责提醒，行为向后兼容）。
    def _read_facts_state():
        with get_db() as conn:
            row = conn.execute(
                "SELECT global_facts_json FROM analysis WHERE project_id = ?",
                (project_id,),
            ).fetchone()
            return row["global_facts_json"] if row else ""

    stored = await asyncio.to_thread(_read_facts_state)
    usable = [it for it in parse_stored_items(stored) if not is_placeholder_value(it.get("value"))]
    if not usable:
        raise HTTPException(
            status_code=400,
            detail="没有可确认的事实变量（为空或全部为【待填写】占位），请先提取并补齐后再确认",
        )

    def _confirm():
        with get_db() as conn:
            # BUG-FIX 2026-09-14（高）：同上，改为 upsert，避免"确认"动作静默失效
            conn.execute(
                "INSERT INTO analysis (project_id, confirmed_facts) VALUES (?, 1) "
                "ON CONFLICT(project_id) DO UPDATE SET confirmed_facts = 1",
                (project_id,),
            )

    await asyncio.to_thread(_confirm)
    return {"ok": True}


@router.post("/audit")
async def audit_facts_consistency(project_id: int, current_user: Annotated[Any, Depends(get_current_user)] = None):
    """一致性审计：检查正文中是否与事实变量一致"""
    await _ensure_project_access(project_id, current_user)

    def _read_audit_data():
        with get_db() as conn:
            row = conn.execute(
                "SELECT global_facts_json FROM analysis WHERE project_id = ?",
                (project_id,),
            ).fetchone()
            if not row:
                return None, None
            sections = conn.execute(
                "SELECT id, title, content FROM sections WHERE project_id = ? AND status = 'completed' AND content != ''",
                (project_id,),
            ).fetchall()
            return row, sections

    row, sections = await asyncio.to_thread(_read_audit_data)
    if row is None:
        raise HTTPException(status_code=400, detail="未找到事实变量")

    _stored_facts = parse_stored_items(row["global_facts_json"])
    facts = [f for f in _stored_facts if not is_placeholder_value(f.get("value"))]
    skipped_placeholder = len(_stored_facts) - len(facts)

    if not facts:
        raise HTTPException(status_code=400, detail="事实变量为空（或全部为【待填写】占位），请先生成并人工补齐")

    if not sections:
        raise HTTPException(status_code=400, detail="没有已完成的正文内容可供审计")

    # ENHANCE 2026-09-29（审计覆盖率）：原实现只取前 5 章 × 每章 500 字送审，
    # 一份 10 万字的标书被检查不到 3%。现复用「章节一致性审计」已有的
    # 字符装箱分组器 `_split_audit_groups`（单节 5000 字、每组 30000 字符），
    # 对所有已完成章节**全覆盖**，组间按上限并发。
    # 成本可控位：`FACTS_AUDIT_MAX_GROUPS`（默认 0 = 不限组数）。超出时取前 N 组
    # 并如实报 truncated，不再把「只查了一部分」当成「全部一致」。
    from ..config import FACTS_AUDIT_MAX_GROUPS
    from ..services.ai.workflows_expand import (
        AUDIT_GROUP_CONCURRENCY,
        AUDIT_SECTION_CHAR_LIMIT,
        _split_audit_groups,
    )

    groups = _split_audit_groups(
        [{"id": s["id"], "title": s["title"], "content": s["content"] or ""} for s in sections]
    )
    _total_groups = len(groups)
    if FACTS_AUDIT_MAX_GROUPS > 0 and _total_groups > FACTS_AUDIT_MAX_GROUPS:
        groups = groups[:FACTS_AUDIT_MAX_GROUPS]
    audited_sections = sum(len(g) for g in groups)
    _truncated = audited_sections < len(sections) or any(
        len(s["content"] or "") > AUDIT_SECTION_CHAR_LIMIT for s in sections
    )

    # 逐事实列表（只送非占位事实，避免把【待填写】当成「正文缺失」报假阳）
    facts_text = "\n".join(
        [f"- {f.get('label') or f.get('key', '')}：{f.get('value', '')}" for f in facts]
    )

    # R-2 2026-10-12：同上一处，统一 provider + collect_json_response 链路。
    from ..services.ai.extraction_runner import (
        create_provider,
        get_ai_config,
        run_json_extraction,
        AIMessage,
    )

    ai_config = get_ai_config()
    provider = create_provider(ai_config)

    audit_sys = """# 事实一致性审计

## 角色
你是标书内容审计专家，检查正文内容是否与事实变量一致。

## 审计规则
逐条检查事实变量在正文中是否出现矛盾或不一致的情况：
1. 如果事实变量在正文中有对应描述且一致 → 标记为 `consistent`
2. 如果事实变量在正文中有对应描述但矛盾 → 标记为 `inconsistent`，并说明矛盾处
3. 如果事实变量在正文中没有对应描述 → 标记为 `missing`

## 输出格式
```json
[{"fact_key":"...","fact_label":"...","fact_value":"...","status":"consistent|inconsistent|missing","detail":"..."}]
```

直接输出 JSON 数组。"""

    _sem = asyncio.Semaphore(max(1, AUDIT_GROUP_CONCURRENCY))

    async def _audit_group(group_index: int, group: list) -> tuple[list, bool]:
        """审计一组章节，返回 (结果列表, 是否失败)。

        失败不再整体退回空数组：单组异常只影响该组，其余结果照常合入，
        并通过 `audit_status=partial` 告知前端「本次未覆盖全部」。
        """
        section_texts = "\n\n".join(
            [f"章节《{s['title']}》内容：\n{(s['content'] or '')[:AUDIT_SECTION_CHAR_LIMIT]}" for s in group]
        )
        audit_user = f"""事实变量列表：
{facts_text}

正文内容（本次送审 {len(group)} 章，第 {group_index + 1} 批）：
{section_texts}

请逐条检查以上事实变量在送审正文中的一致性。"""
        async with _sem:
            try:
                # R-2 2026-10-12：改走统一 runner（provider 由调用方预构造传入）。
                result, _ = await run_json_extraction(
                    [
                        AIMessage(role="system", content=audit_sys),
                        AIMessage(role="user", content=audit_user),
                    ],
                    provider=provider,
                    temperature=0.3,
                    max_retries=2,
                )
                return coerce_audit_list(result), False
            except Exception as e:
                logger.warning("[事实审计] 第 %d/%d 组审计失败: %s", group_index + 1, _total_groups, e)
                return [], True

    group_results = await asyncio.gather(
        *(_audit_group(i, g) for i, g in enumerate(groups)), return_exceptions=True
    )

    # 合并：同一 fact_key 取「最严重」状态（inconsistent > missing > consistent），
    # 不同组重复报出的 detail 拼接，保留定位信息。
    _STATUS_RANK = {"inconsistent": 3, "missing": 2, "consistent": 1}
    merged: dict[str, dict] = {}
    failed_groups = 0
    for res in group_results:
        if isinstance(res, BaseException):
            failed_groups += 1
            continue
        items_g, failed = res
        if failed:
            failed_groups += 1
        for row_item in items_g:
            fk = str(row_item.get("fact_key") or row_item.get("fact_label") or "")
            if not fk:
                continue
            prev = merged.get(fk)
            if prev is None:
                merged[fk] = dict(row_item)
                continue
            if _STATUS_RANK.get(str(row_item.get("status")), 0) > _STATUS_RANK.get(
                str(prev.get("status")), 0
            ):
                merged[fk] = dict(row_item)
            elif row_item.get("detail"):
                prev["detail"] = f"{prev.get('detail') or ''} | {row_item['detail']}".strip(" |")

    audit_results = list(merged.values())
    if failed_groups and failed_groups >= len(groups):
        audit_status = "failed"
    elif failed_groups or _truncated:
        audit_status = "partial"
    else:
        audit_status = "ok"
    audit_payload = {
        "status": audit_status,
        # BUG-FIX 2026-10-04（前后端接口不一致）：落库载荷此前只有 `status`，
        # 而 HTTP 返回体叫 `audit_status`，前端 loadData 回看 last_audit 读的
        # 是 `audit_status` —— 会话内能看到「未覆盖全部/审计未执行」标签，
        # 刷新后同一标签静默消失（旧数据行无此键时前端回退 status，兼容）。
        "audit_status": audit_status,
        "items": audit_results,
        "total_facts": len(facts),
        "audited_sections": audited_sections,
        "total_sections": len(sections),
        "truncated": _truncated,
        "skipped_placeholder": skipped_placeholder,
        # 与 HTTP 返回体保持同一字段集，否则刷新后回看的 last_audit 少了诊断信息
        "audit_groups": len(groups),
        "failed_groups": failed_groups,
    }

    # ENHANCE 2026-09-29：审计结果落库，刷新页面不丢历史，也便于跨次对比与监控。
    try:
        await asyncio.to_thread(_persist_audit, project_id, audit_payload)
    except Exception as e:
        # 落库失败不影响审计结果本身（前端仍能拿到本次报告），仅失去可回看性
        logger.warning("[事实审计] 审计结果落库失败（不影响本次返回）: %s", e)

    return {
        "items": audit_results,
        "total_facts": len(facts),
        "audited_sections": audited_sections,
        "total_sections": len(sections),
        "truncated": _truncated,
        # ENHANCE 2026-09-29：前端据此区分「真的全部一致」与「审计没跑起来」。
        # 旧实现下 AI 异常也返回空 items，前端绿字「一致性检查通过」——假的通过。
        "audit_status": audit_status,
        "audit_groups": len(groups),
        "failed_groups": failed_groups,
        "skipped_placeholder": skipped_placeholder,
    }


@router.get("/metrics")
async def facts_metrics_endpoint():
    """全局事实模块运行时指标快照（ENHANCE 2026-10-17 / 加固）。

    与 ``GET /consistency/metrics`` 同构：纯内存计数器，进程重启归零，
    仅用于实时观测事实提取/编排选择/标题解析的耗时与命中情况。
    """
    from ..services.facts_metrics import snapshot_with_defaults

    return snapshot_with_defaults()
