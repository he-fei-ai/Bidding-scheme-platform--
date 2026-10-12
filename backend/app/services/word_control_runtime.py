"""易标字数控制体系 —— 生成链运行时装配（R5/R8/R12/R14 的实际接线层）。

与 ``app/services/word_control.py`` 的分工：

- ``word_control.py``：**纯函数**（零 DB、零 AI、零副作用），负责全部公式口径；
- 本模块：把纯函数接到真实运行链上——读取项目配置与快照、生成入口的
  R14 硬门槛、强控生效时的逐节覆盖参数（R5 区间 / R8 初稿倒推目标）、
  生成收尾的 R12 结构化警告与落库回写。

设计约束（承自移植报告第 4 节）：

1. **不重复实现任何公式**——本模块只做「装配」，所有数值一律来自
   ``word_control.py``，避免本平台历史上「生成侧/校验侧口径分叉」的故障复现。
2. **向后兼容**——项目未配置绝对字数目标（全 0）时，本模块所有入口都返回
   「不控制」的等价结构，既有档位（tier）体系行为一字不变；配置了绝对目标
   却缺生效快照时才触发 R14 硬门槛，与易标「无快照禁止生成正文」对齐。
3. **失败不阻断主流程**——除门槛判定外，装配/回写环节的任何异常只记日志，
   降级为「本环节不生效」，绝不把整个生成任务带崩。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional

from . import word_control as wc

logger = logging.getLogger(__name__)

#: R14 硬门槛提示语（对应易标「当前目录没有字数控制生效快照，请重新生成目录」）。
#: 本平台的快照由「固化」动作显式产出（POST /word-count/word-control-snapshot/{id}），
#: 因此提示语指向字数统计页的固化入口。
SNAPSHOT_GATE_MESSAGE = (
    "当前目录缺少字数控制生效快照：已配置全文字数目标，请先在「字数统计」页"
    "固化字数控制配置（生成目录/调整目录后需重新固化），再生成正文"
)


@dataclass(frozen=True)
class ProjectWordControl:
    """项目级字数控制状态（配置 + 生效快照）。"""

    project_id: int
    options: wc.WordControlOptions
    snapshot: Optional[dict]
    snapshot_at: Optional[str]

    @property
    def configured(self) -> bool:
        """是否配置了任何绝对字数目标（全 0 = 未启用，走既有档位体系）。"""
        return bool(self.options)

    @property
    def effective_options(self) -> wc.WordControlOptions:
        """正文控制只认快照（R14）；快照缺失时回落当前配置。

        快照与目录版本配对存在：用户改了配置但未重新固化时，正文链必须仍按
        快照跑，否则就会出现「改了目录字数失控」的静默脱节。
        """
        if wc.has_valid_snapshot(self.snapshot):
            return wc.normalize_outline_word_control_options(self.snapshot)
        return self.options

    @property
    def strict_active(self) -> bool:
        """强控是否生效：快照里 section_words > 0 且开关为真。"""
        opts = self.effective_options
        return bool(opts.section_words and opts.strict_section_words)

    def gate_error(self) -> Optional[str]:
        """R14 启动门槛：返回错误消息表示应拒绝生成；None 表示放行。"""
        if not self.configured:
            return None
        if not wc.has_valid_snapshot(self.snapshot):
            return SNAPSHOT_GATE_MESSAGE
        return None


def load_project_word_control(project_id: int) -> ProjectWordControl:
    """读取项目的字数控制配置与快照；表不存在/无行/读取异常均视为未配置。"""
    row: Optional[dict] = None
    try:
        from ..db import get_db

        with get_db(readonly=True) as conn:
            ready = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name='word_control_settings'"
            ).fetchone()
            if ready:
                r = conn.execute(
                    "SELECT * FROM word_control_settings WHERE project_id = ?",
                    (project_id,),
                ).fetchone()
                row = dict(r) if r else None
    except Exception as e:  # 迁移未跑 / DB 抖动 → 按未配置处理，不阻断生成
        logger.warning("[字数控制] 读取项目配置失败（按未配置处理）: %s", e)
        row = None

    if not row:
        return ProjectWordControl(project_id, wc.WordControlOptions(), None, None)

    options = wc.normalize_word_control_options(row)
    snapshot: Optional[dict] = None
    raw = row.get("outline_word_control_snapshot_json")
    if raw:
        try:
            parsed = json.loads(raw)
            snapshot = parsed if isinstance(parsed, Mapping) else None
        except (TypeError, ValueError):
            snapshot = None
    return ProjectWordControl(
        project_id, options, snapshot, row.get("snapshot_at")
    )


def build_section_override(
    state: ProjectWordControl, sections: Iterable[Any]
) -> dict[Any, dict]:
    """强控生效时为每个参与生成的 L3+ 章节装配覆盖参数（R5 / R8）。

    返回 ``{section_id: {"section_words", "lo", "hi", "generation_target", "strict"}}``；
    未配置 / 未强控 / 快照无效时返回空 dict（调用方回落既有档位逻辑）。

    - ``lo`` / ``hi``：R5 强控区间 ``[ceil(0.8S), floor(1.2S)]``；
    - ``generation_target``：R8 初稿倒推目标（强控且设了全文上限时非 0）；
    - ``leaf_count``：以本次参与生成的 L3+ 章节数为基准（与易标「叶子数」
      同口径；子集重生成时按子集计，保证同一批章节内的预算一致）。
    """
    if not state.strict_active:
        return {}
    snap = wc.derive_snapshot_bounds(state.snapshot)
    section_words = int(snap["section_words"] or 0)
    if section_words <= 0 or snap["section_minimum_words"] <= 0:
        return {}

    pool = [
        s for s in sections
        if int((s.get("level") if isinstance(s, Mapping) else getattr(s, "level", 3)) or 3) >= 3
    ]
    gen_target = wc.compute_generation_word_target(
        section_words,
        int(snap["maximum_words"] or 0),
        len(pool),
        True,
    )
    base = {
        "section_words": section_words,
        "lo": int(snap["section_minimum_words"]),
        "hi": int(snap["section_maximum_words"]),
        "generation_target": gen_target,
        "strict": True,
    }
    result: dict[Any, dict] = {}
    for s in pool:
        sid = s.get("id") if isinstance(s, Mapping) else getattr(s, "id", None)
        if sid is not None:
            result[sid] = dict(base)
    return result


def resolve_override(override: Optional[Mapping[Any, dict]], section: Any) -> Optional[dict]:
    """从覆盖表里取某章节的参数（无覆盖 / 非强控时返回 None）。"""
    if not override:
        return None
    sid = section.get("id") if isinstance(section, Mapping) else getattr(section, "id", None)
    if sid is None:
        return None
    return override.get(sid)


def build_strict_section_requirement(override: Mapping[str, Any]) -> str:
    """R5/R8/R13 小节字数指令（对应易标 ``buildSectionWordRequirement`` 强控话术）。

    - 强控话术：「目标约 X 字，硬性上限 Y 字，绝对不得超过上限；超出上限属于
      不合格输出。……宁可略短也不要为凑字数扩写、堆砌或重复表达。」
    - 倒推出初稿目标（R8）时以它为建议目标，硬上限仍是 1.2S；
    - 附加 R13 内容保真条款：字数服从内容保真。
    """
    target = int(override.get("generation_target") or 0) or int(override["section_words"])
    hi = int(override["hi"])
    lo = int(override["lo"])
    return (
        f"本小节目标字数约 {target} 字，硬性上限 {hi} 字（允许区间 {lo}~{hi} 字），"
        "绝对不得超过上限；超出上限属于不合格输出。"
        "宁可略短也不要为凑字数扩写、堆砌或重复表达。"
        f"{wc.SECTION_WORD_CONTENT_PRESERVATION_CLAUSE}"
    )


def finalize_word_control_warning(
    state: ProjectWordControl,
    results: Iterable[Any],
    *,
    single_section: bool = False,
) -> Optional[dict]:
    """生成收尾（R12）：构造结构化警告并回写 DB。

    步骤（对应易标「重建全文字数 → 逐节有效性检查 → word_control_warning」）：

    1. 逐节按 **R1 可读字数**重算实际字数（剔除 Markdown/图片/代码块/URL）；
    2. 调纯函数 ``build_word_control_warning`` 判定小节越界（强控）与全文偏离；
    3. 强控项目回写 ``sections.word_count_target / word_count_deviation`` 列；
    4. 警告写入 ``word_control_settings.word_control_warning``（None 也写，
       清除上一轮残留），供前端弹窗展示「期望区间 vs 实际 + 逐节明细」。
    """
    opts = state.effective_options
    if not opts:
        return None

    views: list[dict] = []
    for r in results or []:
        if not isinstance(r, Mapping):
            continue
        if r.get("status") != "completed":
            continue
        content = r.get("content") or ""
        if not content:
            continue
        views.append(
            {
                "id": r.get("id"),
                "title": r.get("title") or "",
                "word_count": int(wc.count_readable_words(content)),
            }
        )

    total_words = sum(v["word_count"] for v in views)
    warning = wc.build_word_control_warning(
        single_section=bool(single_section),
        total_words=total_words,
        options=opts,
        sections=views,
    )

    _persist_warning(state.project_id, warning)
    if opts.section_words and opts.strict_section_words and views:
        _persist_section_deviations(views, int(opts.section_words))
    return warning


def _persist_warning(project_id: int, warning: Optional[dict]) -> None:
    """警告回写（None 亦写，清除上一轮残留）；失败只记日志。"""
    try:
        from ..db import get_db

        with get_db() as conn:
            ready = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name='word_control_settings'"
            ).fetchone()
            if not ready:
                return
            conn.execute(
                "UPDATE word_control_settings SET word_control_warning = ?, "
                "updated_at = datetime('now', 'localtime') WHERE project_id = ?",
                (
                    json.dumps(warning, ensure_ascii=False) if warning else None,
                    project_id,
                ),
            )
    except Exception as e:
        logger.warning("[字数控制] 警告回写失败（忽略）: %s", e)


def _persist_section_deviations(views: list[dict], target: int) -> None:
    """强控项目回写 sections.word_count_target / word_count_deviation。

    实际字数用 R1 可读口径（与警告判定同一事实源）；target 来自快照派生。
    """
    rows = [
        (target, int(v["word_count"]) - target, v["id"])
        for v in views
        if v.get("id") is not None
    ]
    if not rows:
        return
    try:
        from ..db import get_db

        with get_db() as conn:
            conn.executemany(
                "UPDATE sections SET word_count_target = ?, word_count_deviation = ? "
                "WHERE id = ?",
                rows,
            )
    except Exception as e:
        logger.warning("[字数控制] 小节偏差回写失败（忽略）: %s", e)


__all__ = [
    "SNAPSHOT_GATE_MESSAGE",
    "ProjectWordControl",
    "load_project_word_control",
    "build_section_override",
    "resolve_override",
    "build_strict_section_requirement",
    "finalize_word_control_warning",
]
