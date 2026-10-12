"""全局事实 ← 审计结果 反向回流（E-5，A1「闭环补全」）。

## 解决的问题

既有闭环是链式的：事实提取 → 编排注入 → 事后审计 → 修冲突。但审计发现的
「事实清单之外的稳定取值」（正文里反复出现、且从未冲突的高频值）没有回流
通道 —— 下次重生成时这些口径仍然游离在清单外，依赖 AI 自觉保持一致。

本模块提供：
1. ``propose_facts``：扫描未解决冲突 + 全项目正文，把「冲突中占多数的取值」
   （以及正文高频稳定值）沉淀成候选事实（占位值剔除、与既有事实去重）；
2. ``accept_fact_proposals``：把确认的候选合并进 ``analysis.global_facts_json``
   （走与手工保存完全一致的 upsert + 三层缓存失效），合并前按
   ``facts_service.dedup_key`` 去重兜底。

判定口径：候选值是否「已被正文普遍采纳」用 ``text_patch.find_value_occurrences``
（带数字边界保护）统计出现次数，与确定性修复链路同一套定位口径，避免把
「工程 90 天」里的 90 误计成 90 这个孤立值。

## 并发与临界区约定（HARDEN 2026-10-11·R-11）

服务层函数默认可被任意模块 ``import`` 直接调用，**路由层的锁不构成服务层自保护**
（``routers/consistency.py`` 的 ``_LockManager`` 是 asyncio.Lock，只覆盖 HTTP 入口；
`db.get_db` 的全局写锁只串行化**单个事务块**、不覆盖块与块之间的间隙）。因此本模块
的写函数遵循一条约定：

1. **读-改-写必须落在同一个 ``get_db()`` 临界区内**，不得拆成两个事务块
   （否则产生 lost update，见 ``accept_fact_proposals`` 的 HARDEN 注释）。
2. **临界区内禁止 I/O**。合并、校验、序列化等纯计算一律抽成独立纯函数
   （如 ``merge_fact_proposals``），先算好再进块；临界区受
   ``db._db_write_lock_timeout``（当前 8.0s）约束，锁内外部调用会把争用请求
   拖到超时失败。
3. **缓存在临界区外失效**。``invalidate_content_caches`` 等收尾动作放在
   ``with get_db()`` 块之后，不延长持锁时间。

新增写函数请照此三条实现；破坏任意一条都会重新打开并发窗口。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from ..db import get_db
from . import text_patch
from .consistency_repair import _load_all_project_content
from .facts_service import (
    dedup_key,
    dump_items,
    infer_category,
    is_placeholder_value,
    normalize_fact_value,
    parse_stored_items,
)
from .sections import invalidate_content_caches


logger = logging.getLogger(__name__)

#: 回流的候选键前缀（避免与用户真实 key 语义混淆）
_PROPOSAL_KEY_PREFIX = "audit_"


def _open_conflicts(conn, project_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT conflict_id, fact_name, values_json FROM consistency_conflicts "
        "WHERE project_id = ? AND resolved = 0 ORDER BY id",
        (project_id,),
    ).fetchall()
    out = []
    for row in rows:
        try:
            values = json.loads(row["values_json"] or "[]")
        except (json.JSONDecodeError, TypeError):
            values = []
        if not isinstance(values, list):
            values = []
        out.append({
            "conflict_id": row["conflict_id"],
            "fact_name": row["fact_name"] or "",
            "values": [str(v) for v in values if str(v).strip()],
        })
    return out


def _existing_fact_markers(project_id: int) -> set:
    """既有事实的 label + 归一化 value 集合，用于候选去重。"""
    markers: set = set()
    with get_db() as conn:
        row = conn.execute(
            "SELECT global_facts_json FROM analysis WHERE project_id = ?", (project_id,)
        ).fetchone()
        if row is None:
            return markers
    for item in parse_stored_items(row["global_facts_json"]):
        label = str(item.get("label") or item.get("key") or "").strip()
        if label:
            markers.add(label)
        value = normalize_fact_value(item.get("value"))
        if value:
            markers.add(f"v:{value}")
    return markers


def propose_facts(project_id: int, min_occurrences: int = 3) -> dict:
    """生成「沉淀为全局事实」的候选清单（E-5）。

    Args:
        project_id: 项目ID
        min_occurrences: 候选值在全项目正文里至少出现次数（防单章噪声）

    Returns:
        {"proposals": [...], "skipped": n, "conflicts_scanned": n}
        每项候选：{conflict_id, fact_name, value, occurrences, category,
        suggested_key, occurrences_map}
    """
    with get_db() as conn:
        content_by_id = _load_all_project_content(conn, project_id)
        conflicts = _open_conflicts(conn, project_id)
    existing = _existing_fact_markers(project_id)

    proposals: list[dict] = []
    seen: set = set()
    skipped_missing_conflict = 0
    for conflict in conflicts:
        fact_name = conflict["fact_name"]
        if not conflict["values"]:
            continue
        # 统计每个取值在全项目正文中的出现次数（数字边界保护）
        tally: dict[str, int] = {}
        for value in conflict["values"]:
            if is_placeholder_value(value):
                continue
            count = 0
            for content in content_by_id.values():
                if not content:
                    continue
                count += len(text_patch.find_value_occurrences(content, value))
            if count:
                tally[value] = count
        if not tally:
            skipped_missing_conflict += 1
            continue
        # 冲突里占多数的取值 = 候选值
        winner = max(tally, key=lambda v: (tally[v], len(v)))
        if winner in existing or f"v:{normalize_fact_value(winner)}" in existing:
            continue
        markers_k = (fact_name, winner)
        if markers_k in seen:
            continue
        seen.add(markers_k)
        proposals.append({
            "conflict_id": conflict["conflict_id"],
            "fact_name": fact_name,
            "value": winner,
            "occurrences": tally[winner],
            "category": infer_category(fact_name),
            "suggested_key": f"{_PROPOSAL_KEY_PREFIX}{conflict['conflict_id']}",
            "occurrences_map": [
                {"value": v, "count": c} for v, c in sorted(tally.items(), key=lambda kv: -kv[1])
            ],
        })

    # 太低频的候选由调用方决定是否采用；这里只做「达标数并不代表必须采纳」提示
    kept = [p for p in proposals if p["occurrences"] >= min_occurrences]
    return {
        "proposals": kept,
        "skipped": len(proposals) - len(kept) + skipped_missing_conflict,
        "conflicts_scanned": len(conflicts),
    }


def merge_fact_proposals(
    existing_items: list[dict], proposals: list[dict]
) -> tuple[list[dict], int]:
    """把候选合并进既有事实清单（**纯函数**）。

    HARDEN 2026-10-11（R-8·临界区 I/O-free 可证）：此前合并循环内联在
    ``accept_fact_proposals`` 的 ``get_db()`` 块里，「临界区内无 I/O」只是一句
    注释承诺，无法验证，也无法在不动数据库的情况下单独测试。现提纯为独立函数：

    - 输入输出都是纯内存结构（``parse_stored_items`` 的输出 / 同构列表 + 计数）；
    - 函数体**不做也不允许做任何 I/O**（查库、调 AI、网络、缓存失效）；
    - 因此 ``accept_fact_proposals`` 的临界区 = 一次 SELECT + 本函数 + 一次
      INSERT，持锁时长有上界。

    约束背景：临界区受 ``db._db_write_lock_timeout``（当前 **8.0s**，见
    ``db.py`` 顶部说明）约束。在锁内引入任何外部调用都会拉长持锁时间，争用时把
    后续请求拖到超时失败（``DatabaseWriteLockTimeout``）。**新增逻辑请先算好、
    再进临界区**，不要往本函数里加 I/O。

    合并规则（与既有实现逐条等价，未改语义）：
    - 非 dict 候选、value/fact_name 为空、占位值 → 跳过；
    - ``category`` 缺省用 ``infer_category(fact_name)`` 推导；
    - ``key`` 优先用 ``suggested_key``，否则退回 ``audit_<conflict_id>``；
    - ``dedup_key`` 与既有项碰撞 → 跳过。``dedup_key`` 只比较
      ``key``（无 key 时退化为 ``label``），**不含 value**——即同 key/label
      不同 value 只留一条（见下方 known limitation）；
    - 顺序稳定：先既有项、后新增项。

    Known limitation（HARDEN 2026-10-11·R-14，有意不改语义）：候选的 key 恒为
    ``audit_<conflict_id>``（或显式 ``suggested_key``），而 ``label`` 是
    ``fact_name``，所以「同 label 不同 value」不会被上面的 dedup 拦下。触发链路
    是**跨轮**的，一轮内不会重复产出同名候选——``propose_facts`` 每个 open 冲突
    只取一个 winner，而 ``consistency_auditor._save_conflicts`` 先清掉全部未解决行、
    再对同名已解决历史复用同一行，故任一时刻每个 fact_name 最多一行 open 冲突。
    但上一轮确认的候选不拦下一轮：正文改了口径后，新一轮 winner 与已确认值不同时，
    第二条同名候选同样会通过 dedup。后果比「冗余」更重：下游按
    ``consistency_auditor.FACT_BASELINE_CATEGORIES`` 用 **category**（不是 label）
    收基准，且把该事实名下**所有**值都收进 ``all_facts`` 互比（没有「首条胜出」），
    于是用户自己确认的两条事实会互相判成冲突，且该冲突来自 global_facts 本身、
    无法靠改正文消解。判定不改的原因：label 去重会静默丢弃用户明确确认的候选
    （界面回报「0 条新增」却查不出原因），比留下一条冗余事实更糟；且 ``basic``
    等非基准分类本就允许多条同 label 事实并存。

    Args:
        existing_items: 既有事实项（不会被就地修改，函数内做浅拷贝）
        proposals: 待采纳候选（``propose_facts`` 的条目）

    Returns:
        ``(合并后的清单, 新增条数)``
    """
    items = list(existing_items)
    added = 0
    for p in proposals:
        if not isinstance(p, dict):
            continue
        value = str(p.get("value") or "").strip()
        fact_name = str(p.get("fact_name") or "").strip()
        if not value or not fact_name:
            continue
        if is_placeholder_value(value):
            continue
        new_item = {
            "key": str(p.get("suggested_key") or f"{_PROPOSAL_KEY_PREFIX}{p.get('conflict_id', '')}"),
            "label": fact_name,
            "value": value,
            "category": p.get("category") or infer_category(fact_name),
            "source": "audit_backflow",
        }
        # dedup 兜底：`dedup_key` 优先比较 key、无 key 时退化为 label（不含 value）。
        # 由于 key 恒为 `audit_<conflict_id>`，这条主要兜住「同一冲突被重复采纳」
        # 的幂等场景；suggested_key 恰好撞上既有 key 时也会在此被拦下。
        if any(dedup_key(new_item) == dedup_key(it) for it in items):
            continue
        items.append(new_item)
        added += 1
    return items, added


def accept_fact_proposals(project_id: int, proposals: list[dict]) -> dict:
    """把确认的候选合并进全局事实清单（E-5）。

    与手工保存同一条 upsert 链路：全量覆写 global_facts_json + 失效三层缓存
    （旧正文缓存若不清，重生成仍会命中旧口径）。合并前用 dedup_key 防重。

    并发语义：读快照 + 合并 + upsert 在**同一个** ``get_db()`` 临界区内完成，
    合并本身走纯函数 ``merge_fact_proposals``（见其 docstring 的 I/O-free 约定），
    因此持锁时长有上界，不会因锁内外部调用而放大。

    Args:
        project_id: 项目ID
        proposals: 要采纳的候选（来自 propose_facts 的条目）

    Returns:
        {"added": n, "merged_total": m}
    """
    if not proposals:
        return {"added": 0, "merged_total": 0}

    try:
        # HARDEN 2026-10-11（并发丢失更新）：旧实现把「读旧事实清单」和「写回」拆在
        # **两个独立事务**里。db.get_db 的全局写锁（threading.RLock）只串行化
        # **单个事务块**、不覆盖块与块之间的间隙，于是并发两次 accept 可以在
        # 「A 读到空快照 → B 也读到空快照 → A 写 {a} → B 写 {b}」的交错下丢掉 A
        # 刚合并的那条事实（lost update）——路由层虽有 `_LockManager(project_id)`
        # 挡住 HTTP 入口，但服务层一旦被第二个调用方绕过路由调用，窗口立即重现。
        # 现把读快照移进写事务同一块：读 + 去重合并 + upsert 成为同一临界区；
        # 锁内只做内存处理（proposals 已随请求体到达），无耗时 I/O。
        #
        # 约定（勿删 SET 子句里的这两列）：事实基准 global_facts_json 变更后，
        # `confirmed_facts` 必须回退、`facts_audit_json/facts_audit_at` 必须抹除——
        # 旧审计是针对另一批事实值的结论，留库会被 GET /facts 回看成「本次事实的
        # 一致性结果」从而误导确认决策。此处与 routers/facts.py 的 POST /facts
        # （2026-09-29 BUG-FIX）、重新提取端点、以及 routers/ai.py 的
        # _sync_reset_facts_audit 属**同一口径**，改口径请四处同改。
        with get_db() as conn:
            old_row = conn.execute(
                "SELECT global_facts_json FROM analysis WHERE project_id = ?", (project_id,)
            ).fetchone()
            items = parse_stored_items(old_row["global_facts_json"]) if old_row else []

            # 合并走纯函数：临界区内不含任何 I/O（见 merge_fact_proposals docstring）
            items, added = merge_fact_proposals(items, proposals)

            if added == 0:
                return {"added": 0, "merged_total": len(items)}

            facts_json = dump_items(items)
            conn.execute(
                "INSERT INTO analysis (project_id, global_facts_json, confirmed_facts, "
                "facts_audit_json, facts_audit_at) "
                "VALUES (?, ?, 0, '', NULL) "
                "ON CONFLICT(project_id) DO UPDATE SET "
                "global_facts_json = excluded.global_facts_json, "
                "confirmed_facts = 0, facts_audit_json = '', facts_audit_at = NULL",
                (project_id, facts_json),
            )
        invalidate_content_caches(project_id)
    except Exception as e:
        logger.error("[事实回流] 写入失败 project=%s: %s", project_id, e, exc_info=True)
        raise
    return {"added": added, "merged_total": len(items)}