"""写作长度（简短 / 标准 / 详细 / 超长）-> 各级目录目标字数配置。

控制范围按「中间值」生效：当选中某一模式时，二级目录及以下章节生成的实际目标字数
取该模式区间的中间值（如 简短 1000~1800 -> 1400），并给出 ±10% 的弹性区间交给 AI。

区间说明（沿用既有正文生成字数分档的中间值，简短 1000~1800 为用户指定）：
    short       简要   1000~1800   -> 1400
    standard    标准   2000~5000   -> 3500
    detailed    详细   4000~8000   -> 6000
    extra_long  超长   6000~10000  -> 8000
L3 / L4 在下限层级上按层级递减，确保「二级目录及以下」由同一模式统一控制。
"""

from typing import Optional

# 模式 -> 各级目录目标字数（中间值）；二级目录为控制核心，三级 / 四级按层级递减
WRITING_LENGTH_LEVEL_TARGETS: dict[str, dict[int, int]] = {
    "short":      {2: 1400, 3: 800,  4: 600},
    "standard":   {2: 2800, 3: 1500, 4: 1100},
    "detailed":   {2: 4200, 3: 2400, 4: 1700},
    "extra_long": {2: 5600, 3: 3200, 4: 2300},
}

# 模式 -> 人类可读区间（用于提示词备注 / 日志）；中间值 == 上方对应层级目标
# 各档区间宽度统一为 800（与简短档一致），中值等距递进：1400 / 2800 / 4200 / 5600
WRITING_LENGTH_RANGE: dict[str, tuple[int, int]] = {
    "short":      (1000, 1800),
    "standard":   (2400, 3200),
    "detailed":   (3800, 4600),
    "extra_long": (5200, 6000),
}

WRITING_LENGTH_LABEL: dict[str, str] = {
    "short": "简短",
    "standard": "标准",
    "detailed": "详细",
    "extra_long": "超长",
}

DEFAULT_WRITING_LENGTH = "standard"

# 兼容前端 / 旧调用可能使用的写法
_WRITING_LENGTH_ALIASES = {
    "brief": "short", "concise": "short",
    "normal": "standard", "std": "standard",
    "detail": "detailed",
    "long": "extra_long", "super_long": "extra_long",
}


def normalize_writing_length(value: Optional[str]) -> str:
    """规范化写作长度取值，非法 / 缺失回落到 default。"""
    if not value:
        return DEFAULT_WRITING_LENGTH
    v = str(value).strip().lower()
    if v in WRITING_LENGTH_LEVEL_TARGETS:
        return v
    return _WRITING_LENGTH_ALIASES.get(v, DEFAULT_WRITING_LENGTH)


def level2_target(writing_length: Optional[str]) -> int:
    """取某模式下的二级目录目标字数（中间值）。"""
    mode = normalize_writing_length(writing_length)
    return WRITING_LENGTH_LEVEL_TARGETS[mode][2]


def level_target(writing_length: Optional[str], level: int, default: int = 300) -> int:
    """取某模式下指定层级的目标字数（L2/L3/L4 由模式控制，其它层级回落 default）。"""
    mode = normalize_writing_length(writing_length)
    return WRITING_LENGTH_LEVEL_TARGETS.get(mode, {}).get(level, default)
