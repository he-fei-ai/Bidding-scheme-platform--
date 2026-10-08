"""写作长度（简短 / 标准 / 详细 / 超长）-> 三级目录（L3）目标字数配置。

字数控制策略（2026-10-09 方案三）：
- **L3 自身正文**：目标 300 字左右，±20%（240-360）——每个 L3 目录（无论是否
  有下级）都生成自身正文，且不得为空。见本文件 L3_OWN_* 常量与 get_l3_own_*()。
- **四档（brief/standard/detailed/long）**：800/1200/2000/2500 ±10%，语义变为
  「L3 子树总字数（含下级目录章节）」，仅当 L3 存在下级目录章节（L4+）时参与
  校验；无下级的 L3（当前系统全部 L3 均为叶子）以 300±20% 为唯一口径。
- **控制层级**：三级目录（L3）。正文挂在叶子节点与 L3 自身；L1/L2 是父节点，
  不直接生成正文。
- **L1/L2 字数**：作为统计展示（累加其下 L3 字数），不单独控制。
- **DB 优先**：get_tier_config() / get_default_tier_config() 优先从 word_count_tiers
  表读取（可运行时配置），DB 缺失时回落下方常量。
- **兼容**：保留 WRITING_LENGTH_LEVEL_TARGETS 与 level_target 旧接口；
  旧的 `short/standard/detailed/extra_long` 仍被 `normalize_writing_length` 映射到
  `brief/standard/detailed/long`，历史调用无需改动。
"""

from typing import Optional

# === 三级目录（L3）自身正文配置（方案三 2026-10-09）===
# 需求：L3 级目录下（除下级目录章节外）需生成 300 字左右的正文文字。
# - L3 自身正文目标 300 字，允许浮动 ±20%（240-360 字）。
# - 四档（brief/standard/detailed/long）语义在方案三下变为「L3 子树总字数」
#   （含下级目录章节），仅当 L3 存在下级目录章节（L4+）时才参与校验；
#   无下级的 L3（当前系统全部 L3 均为叶子）以 300±20% 为唯一口径。
# - 可配置：环境变量 L3_OWN_TARGET_WORDS / L3_OWN_TOLERANCE_RATIO 覆盖默认值。
import os

L3_OWN_TARGET_WORDS: int = int(os.environ.get("L3_OWN_TARGET_WORDS", "300"))
L3_OWN_TOLERANCE_RATIO: float = float(os.environ.get("L3_OWN_TOLERANCE_RATIO", "0.20"))


def get_l3_own_target() -> int:
    """L3 自身正文目标字数（默认 300，可用环境变量覆盖）。"""
    return L3_OWN_TARGET_WORDS


def get_l3_own_tolerance() -> float:
    """L3 自身正文允许浮动比例（默认 0.20 即 ±20%）。"""
    return L3_OWN_TOLERANCE_RATIO


def get_l3_own_range() -> tuple[int, int]:
    """L3 自身正文达标区间（默认 240-360）。"""
    target = L3_OWN_TARGET_WORDS
    tol = L3_OWN_TOLERANCE_RATIO
    return (int(target * (1 - tol)), int(target * (1 + tol)))


def l3_own_within(word_count: int) -> bool:
    """判断 L3 自身正文字数是否在 300±20% 区间（达标判定）。"""
    if word_count <= 0:
        return False
    lo, hi = get_l3_own_range()
    return lo <= word_count <= hi


# === 档位常量（三级目录 L3 目标字数；DB 表缺失时的兜底）===
# 每档结构：
#   target_words    目标字数
#   tolerance_ratio 允许的浮动比例（±）
#   label           中文档位名（简短/标准/详细/超长）
#   is_default      是否默认档位
#   sort_order      档位顺序（前端下拉展示顺序）
WRITING_LENGTH_TIER_CONFIG: dict[str, dict] = {
    "brief": {
        "label": "简短",
        "target_words": 800,
        "tolerance_ratio": 0.10,
        "is_default": False,
        "sort_order": 1,
    },
    "standard": {
        "label": "标准",
        "target_words": 1200,
        "tolerance_ratio": 0.10,
        "is_default": True,
        "sort_order": 2,
    },
    "detailed": {
        "label": "详细",
        "target_words": 2000,
        "tolerance_ratio": 0.10,
        "is_default": False,
        "sort_order": 3,
    },
    "long": {
        "label": "超长",
        "target_words": 2500,
        "tolerance_ratio": 0.10,
        "is_default": False,
        "sort_order": 4,
    },
}

# 默认档位：用户未选或选空时使用
DEFAULT_WORD_COUNT_TIER = "standard"

# === 兼容旧的 mode -> {level: target} 结构 ===
# L3 为控制核心；L2 保留 0（不直接控制，仅为兼容旧代码 level_target(mode, 2) 不抛错）；
# L4 保留递减值，兼容深层叶子（若目录深度 >3）的分档回落。
WRITING_LENGTH_LEVEL_TARGETS: dict[str, dict[int, int]] = {
    "brief":      {2: 0, 3: 800,  4: 600},
    "standard":   {2: 0, 3: 1200, 4: 900},
    "detailed":   {2: 0, 3: 2000, 4: 1500},
    "long":       {2: 0, 3: 2500, 4: 2000},
}

# 旧 key 到新 key 的映射（历史代码 level_target("short", 3) 兼容）
_WRITING_LENGTH_LEVEL_TARGETS_LEGACY_ALIASES: dict[str, str] = {
    "short": "brief",
    "extra_long": "long",
}

# 模式 -> 人类可读区间（用于提示词备注 / 日志）
# 与 WRITING_LENGTH_TIER_CONFIG 目标字数一致：中值 ±10%
WRITING_LENGTH_RANGE: dict[str, tuple[int, int]] = {
    "brief":      (720, 880),
    "standard":   (1080, 1320),
    "detailed":   (1800, 2200),
    "long":       (2250, 2750),
    # 兼容旧 key
    "short":      (720, 880),
    "extra_long": (2250, 2750),
}

WRITING_LENGTH_LABEL: dict[str, str] = {
    "brief": "简短",
    "standard": "标准",
    "detailed": "详细",
    "long": "超长",
    # 兼容旧 key
    "short": "简短",
    "extra_long": "超长",
}

# 兼容前端 / 旧调用可能使用的写法；将旧 key 归一化到新 key
_WRITING_LENGTH_ALIASES: dict[str, str] = {
    "short": "brief", "concise": "brief",
    "extra_long": "long", "super_long": "long",
    "brief": "brief", "standard": "standard", "detail": "detailed",
    "normal": "standard", "std": "standard",
    "long": "long",
}


def normalize_writing_length(value: Optional[str]) -> str:
    """规范化写作长度取值，非法 / 缺失回落到 default。

    返回 **档位 key**（brief/standard/detailed/long）；旧调用方（如 short/extra_long）
    会被映射到新 key，但对外接口保持兼容。
    """
    if not value:
        return DEFAULT_WORD_COUNT_TIER
    v = str(value).strip().lower()
    if v in WRITING_LENGTH_TIER_CONFIG:
        return v
    return _WRITING_LENGTH_ALIASES.get(v, DEFAULT_WORD_COUNT_TIER)


def _resolve_tier_key_from_legacy(writing_length: Optional[str]) -> str:
    """把 mode（可能含 legacy key）解析为档位 key。"""
    mode = normalize_writing_length(writing_length)
    return _WRITING_LENGTH_LEVEL_TARGETS_LEGACY_ALIASES.get(mode, mode)


def level3_target(writing_length: Optional[str]) -> int:
    """取某档位下的三级目录目标字数（核心 API）。

    Args:
        writing_length: 档位 key（brief/standard/detailed/long）或旧 key（short/extra_long）

    Returns:
        int: 三级目录目标字数（brief=800, standard=1200, detailed=2000, long=2500）
    """
    tier = _resolve_tier_key_from_legacy(writing_length)
    cfg = WRITING_LENGTH_TIER_CONFIG.get(tier)
    if cfg:
        return cfg["target_words"]
    return WRITING_LENGTH_LEVEL_TARGETS.get(tier, {}).get(3, 1200)


def level2_target(writing_length: Optional[str]) -> int:
    """取某模式下的二级目录目标字数。

    **重要**：字数控制已从 L2 下沉到 L3，此函数仅为兼容旧调用保留。
    返回 0 表示"不参与正文字数控制"（L2 是父节点，不生成正文）。
    """
    mode = normalize_writing_length(writing_length)
    return WRITING_LENGTH_LEVEL_TARGETS.get(mode, {}).get(2, 0)


def level_target(writing_length: Optional[str], level: int, default: int = 300) -> int:
    """取某模式下指定层级的目标字数（历史 API）。

    - L3：按档位返回（brief=800, standard=1200, detailed=2000, long=2500）
    - L2：返回 0（不直接控制，仅 L3 是控制单位）
    - 其他层级：走 WRITING_LENGTH_LEVEL_TARGETS 兜底，再回落 default
    """
    mode = normalize_writing_length(writing_length)
    if level == 3:
        return level3_target(writing_length)
    val = WRITING_LENGTH_LEVEL_TARGETS.get(mode, {}).get(level)
    if val and val > 0:
        return val
    return default


def get_tier_config(tier_key: Optional[str]) -> dict | None:
    """按档位 key 获取配置。优先查 DB，回落常量。

    Args:
        tier_key: brief/standard/detailed/long，或 None/空串表示取默认档位

    Returns:
        dict: {key, label, target_words, tolerance_ratio, is_default, sort_order}
        None: tier_key 非法或未找到
    """
    key = _resolve_tier_key_from_legacy(tier_key) if tier_key else DEFAULT_WORD_COUNT_TIER
    try:
        from ...db import get_db
        with get_db(readonly=True) as conn:
            row = conn.execute(
                "SELECT key, label, target_words, tolerance_ratio, is_default, sort_order "
                "FROM word_count_tiers WHERE key = ? AND enabled = 1",
                (key,),
            ).fetchone()
        if row:
            return dict(row)
    except Exception:
        # 表不存在或数据库不可用 → 静默回落常量
        pass
    return dict(WRITING_LENGTH_TIER_CONFIG.get(key, {})) or None


def get_default_tier_config() -> dict:
    """取默认档位配置（DB 中 is_default=1 的第一行，回落 standard 常量）。"""
    try:
        from ...db import get_db
        with get_db(readonly=True) as conn:
            row = conn.execute(
                "SELECT key, label, target_words, tolerance_ratio, is_default, sort_order "
                "FROM word_count_tiers WHERE is_default = 1 AND enabled = 1 "
                "ORDER BY sort_order LIMIT 1"
            ).fetchone()
        if row:
            return dict(row)
    except Exception:
        pass
    return dict(WRITING_LENGTH_TIER_CONFIG.get(DEFAULT_WORD_COUNT_TIER, {}))


def list_tier_configs() -> list[dict]:
    """列出所有档位配置（按 sort_order 排序）。DB 优先，回落常量。"""
    try:
        from ...db import get_db
        with get_db(readonly=True) as conn:
            rows = conn.execute(
                "SELECT key, label, target_words, tolerance_ratio, is_default, sort_order "
                "FROM word_count_tiers WHERE enabled = 1 ORDER BY sort_order"
            ).fetchall()
        if rows:
            return [dict(r) for r in rows]
    except Exception:
        pass
    return [
        dict({"key": k, **cfg})
        for k, cfg in sorted(
            WRITING_LENGTH_TIER_CONFIG.items(),
            key=lambda x: x[1].get("sort_order", 0),
        )
    ]
