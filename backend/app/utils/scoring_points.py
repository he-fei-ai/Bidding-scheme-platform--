"""评分项（scoring points）通用工具 —— 全仓库单一事实来源。

背景（BUG-FIX 2026-09-18）：`extract_scoring_points` 在权重之和偏离 100 时，
会往评分项列表头部插入一条**系统提示项**：

    {"id": "__weight_warning__", "name": "[权重异常] 评分项权重之和为 88，…"}

它是给用户看的校验提醒，**不是评分项**。凡是「按评分项数量/内容做事」的下游
（目录生成的一级标题数、正文提示词、预检的覆盖率检查）都必须先剔除它，否则：

  - 目录生成多凑一个名为「[权重异常]…」的一级章节；
  - 正文提示词里多一条无法展开的"评分要点"；
  - 预检永远报「有 1 个评分点未被目录覆盖」，且用户无论如何都消不掉。

修复前该判定在 5 个模块里各写一份字符串字面量（`== "__weight_warning__"`），
新增占位类型时要改 5 处、漏一处就复现。这里收敛为唯一实现，各模块一律导入。
"""

from __future__ import annotations

from typing import Any

#: 系统提示型「伪评分项」的 id 集合。新增占位类型只需登记这里。
SCORING_PLACEHOLDER_IDS: frozenset[str] = frozenset({"__weight_warning__"})


def is_scoring_placeholder(item: Any) -> bool:
    """判断一条评分项是否为系统提示伪项（不应参与任何业务计数/渲染）。

    同时兼容 dict 与裸 id 字符串两种入参形态。
    """
    if isinstance(item, str):
        return item in SCORING_PLACEHOLDER_IDS
    if isinstance(item, dict):
        return str(item.get("id") or "") in SCORING_PLACEHOLDER_IDS
    return False


def strip_scoring_placeholders(parsed: Any) -> Any:
    """剔除评分项容器里的系统提示伪项，保持入参形态不变。

    支持三种形态（与 `extract_json_from_text` 的常见返回一致）：
      - list[dict]                      → list[dict]
      - {"scoring_points": [...]} 等包装 → 同结构 dict
      - 其它 / 解析失败的原值            → 原样返回
    """
    if isinstance(parsed, list):
        return [it for it in parsed if not is_scoring_placeholder(it)]
    if isinstance(parsed, dict):
        cleaned = {
            k: ([it for it in v if not is_scoring_placeholder(it)] if isinstance(v, list) else v)
            for k, v in parsed.items()
        }
        return cleaned
    return parsed
