"""Agent 模式全文一致性修复（E-1，对标易标 D1「Agent修复」）。

## 与普通修复的分工（consistency_repair.py，AI 补丁路径）

| | AI 补丁路径（既有） | Agent 路径（本模块） |
|---|---|---|
| 交互形态 | 一次性把「事实+冲突+正文」塞进一次请求 | 多轮往返：Agent 先看冲突/正文索引，再按需 `search` / `read` / `replace` |
| 上下文压力 | 单请求内受 batch/token 限制，>24K 字符需分批 | 正文不进一次请求，Agent 只读需要的小块 → 天然支持长文 |
| 修改粒度 | 每批一组 {old_text,new_text} 补丁 | 同一宿主安全模型：`replace` 仍然要求 old_text **唯一命中**，幻觉 0 落库 |

## 安全设计（沿用 consistency_repair 的核心理念）

- **AI 只提修改意图，宿主执行改写。** Agent 永远碰不到数据库，只对自己工作区
  内的正文副本做 `replace`，且每次 `replace` 都走 `find_exact_occurrences`
  唯一命中校验（找不到 / 找到多处 → 拒绝并反馈给 Agent）。
- **章节结构锁死。** 下标（section_id -> 正文）在会话开始前冻结；Agent 不能新增、
  删除、重排章节，只能修改已有章节的正文 → 结构校验内置。
- **越界编号丢弃。** `replace.section_id` 不在白名单内直接拒绝。
- 落文沿用 `refresh_conflict_states` 的可验证判据（正文里仍能找到 ≥2 个取值才
  保持未解决），杜绝「Agent 说修好了就标已解决」的谎报。

工作区文件（`{data}/agent_repair/{project_id}/`）：
- ``global-facts.md``：全局事实 + 招标文件关键解析结果（供 Agent 核对口径）
- ``technical-plan.md``：带章节标记的完整正文快照（Agent 多轮修改后用于回读校验）
"""

from __future__ import annotations

import asyncio
from datetime import datetime
import json
import logging
from pathlib import Path
import re
from typing import Any

from ..db import get_db
from . import text_patch
from .ai.prompts import get_prompt
from .ai.providers.base import AIMessage
from .consistency_repair import (
    _load_all_project_content,
    _load_project_summary_and_baseline,
    build_conflicts_text,
    build_facts_text,
    refresh_conflict_states,
)


logger = logging.getLogger(__name__)

#: 章节开始在全文中的标记（工作区文件 / 回读校验用）
_SECTION_START = "[[SECTION id={sid}|title={title}]]"
_SECTION_END = "[[/SECTION id={sid}]]"

AGENT_SYSTEM_PROMPT_DEFAULT = """你是投标技术方案的全一致性审查 Agent。正文是**分章节并发生成**的，不同章节可能对同一事实（工期、人员、设备型号、质保期、响应时间等）写了不同口径。你的任务：找出这些冲突，并把全文统一到「全局事实」的口径上。

工作方式（每轮）：
1. 先用 search 定位含可疑数值/术语的章节；
2. 用 read 读取相关章节正文片断，比对全局事实（global-facts.md）；
3. 发现冲突后用 replace 提交精确修改（old_text 必须与正文逐字一致，且在该章节内只出现一次）；
4. 全部处理完或你觉得无法继续时，置 done: true。

铁律：
- 只修改与全局事实冲突的表述；不重写章节、不润色文笔、不新增内容；
- 每章 replace 之间互相独立，一次 1 条；
- 不知道确切值时不要猜，标注 reason 让宿主登记为 unresolved；
- 严格 JSON 输出。"""

_AGENT_USER_HDR = """## 全局事实（事实底稿，冲突以它为准）
{global_facts}

## 未解决冲突清单
{conflicts_text}

## 章节索引（section_id<TAB>标题<TAB>字数）
{chapter_index}

## 上一轮你的操作反馈
{prev_feedback}

请继续。输出严格 JSON：
{{"done": bool, "actions": [
  {{"action": "search", "keyword": "..."}},
  {{"action": "read", "section_id": "5", "start_line": 1, "end_line": 20}},
  {{"action": "replace", "section_id": "5", "old_text": "...", "new_text": "...", "reason": "..."}}
], "unresolved": [{{"section_id": "5", "reason": "..."}}]}}
- search：在全部正文里检索关键词，宿主返回命中的 section_id + 命中次数
- read：读取某章节指定行范围（行号从 1 开始），宿主返回该片断文本
- replace：精确替换。old_text 必须在目标章节唯一命中，否则宿主拒绝并反馈
- done: true 时停止；如某处必须改写但无法给出唯一 old_text，请放进 unresolved"""

#: 单轮最多 action 条数（防止单轮输出过长）
MAX_ACTIONS_PER_TURN = 12


def _workspace_dir(project_id: int, base: Path | None = None) -> Path:
    """工作区目录：默认落到 {backend}/data/agent_repair/{project_id}/。"""
    root = base or Path(__file__).resolve().parents[2] / "data" / "agent_repair"
    return root / str(project_id)


def _load_sections(conn, project_id: int) -> list[dict]:
    """载入有正文的小节（id/title/content），id 即唯一的章节键。"""
    rows = conn.execute(
        "SELECT id, title, content FROM sections "
        "WHERE project_id = ? AND content IS NOT NULL AND content != '' "
        "ORDER BY sort_order, id",
        (project_id,),
    ).fetchall()
    return [
        {"id": int(r["id"]), "title": r["title"] or "未命名章节", "content": r["content"] or ""}
        for r in rows
    ]


def build_agent_workspace(project_id: int, base: Path | None = None) -> dict:
    """准备 Agent 工作区：读取事实/冲突/正文，写出两个工作区文件。

    Returns dict：{dir, facts, conflicts, sections, global_facts_md, technical_plan_md}
    只读操作，不落库。

    BUG-FIX 2026-10-27（P3·纯读抢写锁）：本函数对 DB 只读（文件写入仅在
    AGENT_REPAIR_PERSIST_WORKSPACE 门禁下发生，且不碰库），此前用无参
    `get_db()` 抢全局写锁 —— 与 backflow / auditor 已修的「纯读却抢写锁」
    同类问题，会把并发导出/生成事务无谓拖慢。现改 `get_db(readonly=True)`。
    """
    with get_db(readonly=True) as conn:
        facts_row = conn.execute(
            "SELECT global_facts_json FROM analysis WHERE project_id = ?", (project_id,)
        ).fetchone()
        conflict_rows = conn.execute(
            "SELECT conflict_id, fact_name, values_json, description "
            "FROM consistency_conflicts WHERE project_id = ? AND resolved = 0 ORDER BY id",
            (project_id,),
        ).fetchall()
        conflicts: list[dict] = []
        for row in conflict_rows:
            try:
                values = json.loads(row["values_json"] or "[]")
            except (json.JSONDecodeError, TypeError):
                values = []
            conflicts.append({
                "conflict_id": row["conflict_id"],
                "fact_name": row["fact_name"] or "",
                "values": values if isinstance(values, list) else [],
                "description": row["description"] or "",
            })
        sections = _load_sections(conn, project_id)

    from .facts_service import parse_stored_items
    facts = parse_stored_items((facts_row["global_facts_json"] if facts_row else "") or "")

    # ENHANCE 2026-10-11（引入项 #14）：AGENT_REPAIR_PERSIST_WORKSPACE 门禁。
    #   默认 false：Agent 工作区只在内存（facts/conflicts/sections 已足够驱动多轮），
    #     **不落盘**——保持"运行即无痕、仅操作事件被上层日志记录"的既有行为，
    #     避免在 data/ 下长期堆积含正文的敏感快照。
    #   =true：把 global-facts.md / technical-plan.md 写入
    #     {data}/agent_repair/{project_id}/，便于人工复核 Agent 改前/改后正文快照。
    # 修复说明：本函数此前因尾部代码被误并入 `_exec_action`（成为其 return 之后的
    # 死代码）而**丢失 return**，导致 `run_agent_repair` 拿到 None 后在
    # `ctx["sections"]` 处 TypeError 崩溃——即 Agent 修复端点从未真正跑通过。
    # 现恢复返回值，并把"是否落盘"收敛到门禁之下。
    try:
        from ..config import AGENT_REPAIR_PERSIST_WORKSPACE
    except Exception:  # pragma: no cover - 配置读取兜底
        AGENT_REPAIR_PERSIST_WORKSPACE = False

    workdir = _workspace_dir(project_id, base)
    facts_md_path: str | None = None
    plan_md_path: str | None = None

    if AGENT_REPAIR_PERSIST_WORKSPACE:
        workdir.mkdir(parents=True, exist_ok=True)

        # 1) global-facts.md（事实底稿）
        facts_md = workdir / "global-facts.md"
        facts_md.write_text(
            "# 全局事实\n\n" + (build_facts_text(facts) if facts else "（无全局事实）")
            + "\n\n# 未解决冲突\n\n" + build_conflicts_text(conflicts),
            encoding="utf-8",
        )
        facts_md_path = str(facts_md)

        # 2) technical-plan.md（带章节标记的正文快照）
        plan_md = workdir / "technical-plan.md"
        blocks = []
        for sec in sections:
            blocks.append(_SECTION_START.format(sid=sec["id"], title=sec["title"]))
            blocks.append(sec["content"] or "")
            blocks.append(_SECTION_END.format(sid=sec["id"]))
        plan_md.write_text("\n\n".join(blocks), encoding="utf-8")
        plan_md_path = str(plan_md)

    return {
        "dir": str(workdir) if AGENT_REPAIR_PERSIST_WORKSPACE else None,
        "persisted": bool(AGENT_REPAIR_PERSIST_WORKSPACE),
        "facts": facts,
        "conflicts": conflicts,
        "sections": sections,
        "global_facts_md": facts_md_path,
        "technical_plan_md": plan_md_path,
    }


def _parse_agent_json(text: str) -> dict | None:
    """从 Agent 返回中提取 JSON 对象（容忍 ```json 围栏/前后说明文字）。"""
    if not text:
        return None
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return None
    try:
        data = json.loads(m.group())
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _format_feedback(events: list[dict]) -> str:
    """把上一轮宿主对 action 的反馈渲染成提示文本。"""
    lines = []
    for ev in events[-8:]:
        action = ev.get("action", "").upper()
        ok = ev.get("ok")
        detail = ev.get("detail", "")
        if isinstance(ok, bool):
            lines.append(f"- [{action}] {'成功' if ok else '拒绝'}: {detail}")
        else:
            lines.append(f"- [{action}]: {detail}")
    return "\n".join(lines) if lines else "（本轮暂无操作记录）"


async def _agent_turn(
    provider: Any,
    ctx: dict,
    prev_feedback: str,
    turn: int,
) -> dict:
    """调用一次 Agent，返回 {actions, unresolved} 或 None（调用失败/格式非法）。"""
    sections = ctx["sections"]
    chapter_index = "\n".join(
        f"{sec['id']}\t{sec['title']}\t{len(sec['content'])}字" for sec in sections
    )
    # ENHANCE 2026-10-12：CONSISTENCY_AGENT_USER 已注册进 prompts 注册表（_integrate.py），
    # 统一从注册表读取，支持管理页覆盖/重置；保留 _AGENT_USER_HDR.format 作为 DB 不可用时的 fallback。
    user_prompt = (
        get_prompt(
            "CONSISTENCY_AGENT_USER",
            global_facts=build_facts_text(ctx["facts"]) or "（无全局事实）",
            conflicts_text=build_conflicts_text(ctx["conflicts"]) or "（无未解决冲突）",
            chapter_index=chapter_index or "（无正文章节）",
            prev_feedback=prev_feedback,
        )
        or _AGENT_USER_HDR.format(
            global_facts=build_facts_text(ctx["facts"]) or "（无全局事实）",
            conflicts_text=build_conflicts_text(ctx["conflicts"]) or "（无未解决冲突）",
            chapter_index=chapter_index or "（无正文章节）",
            prev_feedback=prev_feedback,
        )
    )
    # ENHANCE 2026-10-12：CONSISTENCY_AGENT_SYSTEM 已注册进 prompts 注册表（checks.py），
    # DB 优先+硬编码回退的 get_prompt 已覆盖全部场景，去掉与注册表完全相同（407 字符）的
    # 冗余 fallback，使 agent_repair 提示词可通过管理页覆盖/重置。
    sys_prompt = get_prompt("CONSISTENCY_AGENT_SYSTEM") or AGENT_SYSTEM_PROMPT_DEFAULT
    try:
        raw = await provider.chat(
            [
                AIMessage(role="system", content=sys_prompt),
                AIMessage(role="user", content=user_prompt),
            ],
            max_tokens=1500,
            temperature=0.2,
            max_retries=1,
        )
        text = raw if isinstance(raw, str) else (raw or {}).get("content", str(raw))
    except Exception as e:
        logger.warning("[Agent修复] 第 %d 轮调用失败: %s", turn + 1, e)
        return None
    data = _parse_agent_json(text)
    if data is None:
        logger.warning("[Agent修复] 第 %d 轮 JSON 解析失败: %.200s", turn + 1, text)
        return None
    actions = data.get("actions")
    unresolved = data.get("unresolved")
    return {
        "done": bool(data.get("done")),
        "actions": actions if isinstance(actions, list) else [],
        "unresolved": unresolved if isinstance(unresolved, list) else [],
    }


def _exec_action(action: dict, ctx: dict) -> dict:
    """宿主执行单条 action（search/read/replace）。

    关键安全点：replace 走 find_exact_occurrences 唯一命中校验；
    section_id 越界 / 老文本多处命中 / 命不中 → 拒绝并反馈原因。
    """
    kind = str(action.get("action") or "").strip().lower()
    sections = ctx["sections"]
    by_id = {str(s["id"]): s for s in sections}
    if kind == "search":
        keyword = str(action.get("keyword") or "").strip()
        if not keyword:
            return {"action": "search", "ok": False, "detail": "keyword 为空"}
        hits = []
        for sec in sections:
            n = sec["content"].count(keyword)
            if n > 0:
                hits.append(f"{sec['id']}({sec['title']})×{n}")
        if not hits:
            return {"action": "search", "ok": True, "detail": f"无章节命中「{keyword}」"}
        return {"action": "search", "ok": True, "detail": f"「{keyword}」命中: {'; '.join(hits[:20])}"}

    if kind == "read":
        sid = str(action.get("section_id") or "").strip()
        sec = by_id.get(sid)
        if sec is None:
            return {"action": "read", "ok": False, "detail": f"section_id={sid} 不存在（越界）"}

        def _to_int(v) -> int | None:
            try:
                return max(1, int(v))
            except (TypeError, ValueError):
                return None

        start = _to_int(action.get("start_line"))
        end = _to_int(action.get("end_line"))
        lines = (sec["content"] or "").split("\n")
        if start is None:
            start = 1
        if end is None or end < start:
            end = start
        end = min(end, len(lines))
        snippet = "\n".join(f"{i + 1}: {lines[i]}" for i in range(start - 1, end))
        if not snippet:
            return {"action": "read", "ok": True, "detail": "（空片断）"}
        return {"action": "read", "ok": True, "detail": snippet}

    if kind == "replace":
        sid = str(action.get("section_id") or "").strip()
        sec = by_id.get(sid)
        if sec is None:
            return {"action": "replace", "ok": False, "detail": f"section_id={sid} 不存在（越界）"}
        old_text = text_patch.normalize_patch_text(action.get("old_text"))
        new_text = text_patch.normalize_patch_text(action.get("new_text"))
        reason = str(action.get("reason") or "")[:120]
        if not old_text:
            return {"action": "replace", "ok": False, "detail": "old_text 为空"}
        if new_text == old_text:
            return {"action": "replace", "ok": False, "detail": "old_text 与 new_text 相同，跳过无效替换"}
        occ = text_patch.find_exact_occurrences(sec["content"], old_text)
        if len(occ) == 0:
            return {"action": "replace", "ok": False, "detail": "old_text 未找到（建议先 read 该章节核对原文）"}
        if len(occ) > 1:
            return {"action": "replace", "ok": False, "detail": f"old_text 在该章节命中 {len(occ)} 处，不唯一，拒绝替换（请缩小范围）"}
        # 安全的宿主侧唯一性替换
        sec["content"] = sec["content"].replace(old_text, new_text, 1)
        sec.setdefault("changed", True)
        return {
            "action": "replace", "ok": True,
            "detail": f"已替换（{reason}）" + (f"：{old_text[:24]}…→{new_text[:24]}…" if reason else ""),
        }

    return {"action": str(kind)[:20], "ok": False, "detail": f"未知 action={kind}"}


async def run_agent_repair(
    project_id: int,
    *,
    dry_run: bool = False,
    provider: Any = None,
    max_turns: int = 8,
    base: Path | None = None,
) -> dict:
    """Agent 模式全文一致性修复主流程（E-1）。

    Args:
        project_id: 项目ID
        dry_run: 只跑 Agent 会话与预览，不写库
        provider: 复用调用方已建的 provider；为空则自行创建
        max_turns: 最大往返轮数（默认 8，防止失控烧 token）

    Returns:
        修复报告：turns / max_turns / applied / rejected / unresolved / errors /
        changed_sections / conflict_states / message / success。

    BUG-FIX 2026-10-13（**P0 · 报告缺字段导致前端整页崩溃**）：本报告 dict 与
    姊妹链路 ``consistency_repair.repair_with_ai`` 的报告 dict（后者**有**
    ``errors``）不一致——缺 ``errors``，也缺 ``max_turns``。而前端
    ``AgentRepairTab.tsx`` 按「与 repair 报告同构」消费这些字段，其中
    ``report.errors.length`` **无保护**：

        TypeError: Cannot read properties of undefined (reading 'length')
          at AgentRepairTab.tsx:156:28

    React 18 无错误边界的默认行为是**卸载整棵组件树**，而
    ``ConsistencyPage.tsx`` 没挂 ErrorBoundary —— 于是「执行修复」只要拿到
    任何一份真实响应（**包括成功响应**），整个「一致性校核」页直接白屏，用户
    连 Tab 都切不过去。

    为什么一直没被发现：本模块的组件级测试
    ``AgentRepairTab.dom.test.tsx`` 的 mock 响应里**手写了** ``errors: []`` 与
    ``max_turns`` —— 桩数据比真实契约更完整，于是测试永远绿、线上永远崩
    （典型「测试替身固化了契约」反模式）。已同步修正该测试的桩数据，改为
    照抄本 dict 的键集，另加一条**真实契约**（缺字段）的回归用例。

    修法取「补齐后端」而非「只改前端可选链」：两条链路报告同构是既有约定，
    缺的那一侧才是异常；两者都补则前端对历史响应/第三方调用方也保持健壮。
    """
    report: dict = {
        "project_id": project_id,
        "dry_run": dry_run,
        "success": False,
        "turns": 0,
        "max_turns": max_turns,
        "applied": [],
        "rejected": [],
        "unresolved": [],
        "errors": [],
        "changed_sections": [],
        "conflict_states": None,
        "message": "",
    }
    try:
        # BUG-FIX 2026-10-27（P1·事件循环阻塞）：本函数是 async，但工作区准备
        # （只读连接 + 全量正文读取 + 可选落盘写文件）是同步重操作，此前直接
        # 跑在事件循环里 —— 正是 audit 路由 asyncio.to_thread 修复注释里的
        # 同一病灶（prompts.py 铁律：async 端点的同步 DB 调用必须进线程池）。
        ctx = await asyncio.to_thread(build_agent_workspace, project_id, base)
    except Exception as e:
        report["message"] = f"工作区准备失败：{e!s}"
        # 与 repair_with_ai 报告同构：异常既进 message（给人读）也进 errors（给 UI 计数）
        report["errors"].append(f"工作区准备失败：{e!s}")
        logger.error("[Agent修复] 工作区准备失败 project=%s: %s", project_id, e, exc_info=True)
        return report

    sections = ctx["sections"]
    if not sections:
        report["message"] = "没有可供修复的正文内容"
        return report
    if not ctx["conflicts"]:
        report["message"] = "没有未解决的一致性冲突，无需修复"
        return report

    if provider is None:
        from .ai.provider_factory import create_provider, get_ai_config
        try:
            provider = create_provider(get_ai_config())
        except Exception as e:
            report["message"] = f"AI Provider 创建失败：{e!s}"
            report["errors"].append(f"AI Provider 创建失败：{e!s}")
            return report

    events: list[dict] = []
    turn = 0
    for turn in range(max_turns):
        result = await _agent_turn(provider, ctx, _format_feedback(events), turn)
        if result is None:
            # BUG-FIX 2026-10-27（P3·单次解析失败终结全会话）：原实现 break，
            # 一次非法 JSON（max_tokens 截断 / 模型夹带说明文字）就烧掉剩余轮数。
            # 现把「上轮返回不可解析」作为反馈喂回下一轮（_format_feedback 取
            # 最近 8 条事件），要求模型严格只输出 JSON 后继续；轮数仍由
            # max_turns 硬性封顶，不会失控。
            events.append({
                "action": "turn",
                "ok": None,
                "detail": "上一轮返回不可解析（非合法 JSON）。请严格只输出一个 JSON 对象：不要附加解释文字、不要 markdown 围栏、不要截断。",
            })
            report["errors"].append(f"第 {turn + 1} 轮模型返回不可解析，已反馈并重试")
            continue
        if result.get("unresolved"):
            for item in result["unresolved"]:
                if isinstance(item, dict) and (item.get("section_id") or item.get("reason")):
                    report["unresolved"].append({
                        "section_id": str(item.get("section_id") or ""),
                        "reason": str(item.get("reason") or ""),
                    })
        for action in (result.get("actions") or [])[:MAX_ACTIONS_PER_TURN]:
            if not isinstance(action, dict):
                continue
            ev = _exec_action(action, ctx)
            events.append(ev)
            if ev["action"] == "replace":
                if ev.get("ok"):
                    report["applied"].append(action)
                else:
                    # BUG-FIX 2026-10-27（P2·被拒补丁契约错位）：本报告的
                    # `rejected` 消费方是前端 AgentRepairTab（渲染
                    # {section_id, title, error}）与姊妹链路 repair_with_ai
                    # 的同名字段（rejected[i]["error"]，见
                    # test_enhancement_2026_09_14 的契约用例）。原实现把 AI 的
                    # 原始 action dict（{action, section_id, old_text, ...}）
                    # 直接塞进来，宿主拒绝原因 ev["detail"]（未找到/不唯一/越界）
                    # 被丢弃 → 前端每条都显示兜底文案「匹配失败」。现按契约
                    # 归一化，把真实拒绝原因带给 UI。
                    _sid = str(action.get("section_id") or "")
                    _title = next(
                        (s["title"] for s in ctx["sections"] if str(s["id"]) == _sid),
                        "",
                    )
                    report["rejected"].append({
                        "section_id": _sid,
                        "title": _title,
                        "error": str(ev.get("detail") or "匹配失败"),
                    })
        if result.get("done"):
            break

    report["turns"] = turn + 1
    changed = [s for s in sections if s.get("changed")]
    report["changed_sections"] = [
        {"id": s["id"], "title": s["title"]} for s in changed
    ]

    # —— 结构校验：章节集合只能增改、不许增减删重排 ——
    # sections 就是 ctx["sections"] 原地修改，id 集合天然不变；额外校验每个
    # 改动章节正文非空（防 AI 整章清空，宁缺毋假）。
    invalid = [s["id"] for s in changed if not (s["content"] or "").strip()]
    if invalid:
        report["message"] = f"存在被清空的章节 {invalid}，拒绝落库（无降级原则）"
        return report

    if not changed:
        report["success"] = True
        report["message"] = (
            f"Agent 执行 {report['turns']} 轮，未提交任何可落库修改"
            + (f"，{len(report['rejected'])} 条被宿主拒绝" if report["rejected"] else "")
        )
        return report

    if dry_run:
        report["success"] = True
        report["message"] = (
            f"预览：Agent 执行 {report['turns']} 轮，修改 {len(changed)} 个小节，"
            f"{len(report['rejected'])} 条替换被拒绝"
        )
        return report

    # —— 落文 + 收敛冲突状态（复用 AI 补丁路径同一判据）——
    # BUG-FIX 2026-10-27（P1·事件循环阻塞）：写锁内的「全量正文扫描 +
    # 冲突状态收敛」是同步重操作，此前直接跑在事件循环里（写锁争用时事件
    # 循环最长冻结 _db_write_lock_timeout=8s）。现整段移入线程池，事件循环
    # 不再被阻塞；异常语义与原实现一致。
    def _persist_changed_sections() -> dict:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with get_db() as conn:
            for s in changed:
                conn.execute(
                    "UPDATE sections SET content = ?, updated_at = ? "
                    "WHERE id = ? AND project_id = ?",
                    (s["content"], now, s["id"], project_id),
                )
            # 全量正文 + 摘要/已确认事实基准做收敛判定，避免「局部范围扫描
            # 把真实旧取值洗白」。BUG-FIX 2026-10-11：审计器的事实来源除章节
            # 正文外还有摘要与事实基准，判据「全文还剩几个取值」必须扫同一宇宙，
            # 否则只在摘要/基准里的旧取值会让冲突被误标已解决。
            verify_scope = _load_all_project_content(conn, project_id)
            for s in changed:
                verify_scope[int(s["id"])] = s["content"]
            return refresh_conflict_states(
                conn,
                project_id,
                verify_scope,
                extra_texts=_load_project_summary_and_baseline(conn, project_id),
            )

    try:
        report["conflict_states"] = await asyncio.to_thread(_persist_changed_sections)
    except Exception as e:
        report["message"] = f"落库失败：{e!s}"
        report["errors"].append(f"落库失败：{e!s}")
        logger.error("[Agent修复] 落库失败 project=%s: %s", project_id, e, exc_info=True)
        return report

    # 旁路直写正文 → 失效三层缓存
    try:
        from .sections import invalidate_content_caches
        await asyncio.to_thread(
            invalidate_content_caches, project_id, [s["id"] for s in changed]
        )
    except Exception as e:
        logger.warning("[Agent修复] 缓存失效失败（忽略）: %s", e)

    states = report["conflict_states"] or {}
    report["success"] = True
    report["message"] = (
        f"Agent 执行 {report['turns']} 轮，修改 {len(changed)} 个小节"
        f"（拒绝 {len(report['rejected'])} 条，登记 unresolved {len(report['unresolved'])} 条）；"
        f"冲突已解决 {states.get('resolved', 0)} 条，仍待处理 {states.get('still_open', 0)} 条"
    )
    logger.info(
        "[Agent修复] 完成 project=%s, turns=%d, 小节=%d, 冲突解决=%d",
        project_id, report["turns"], len(changed), states.get("resolved", 0),
    )
    return report
