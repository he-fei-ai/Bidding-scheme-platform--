"""成本核算模块（2026-10-06）。

基于Token消耗计算实际费用，支持不同模型定价配置。
"""

from __future__ import annotations

import os
from typing import Dict


# 默认模型定价（每1K Token，单位：元）
# 可通过环境变量覆盖
DEFAULT_MODEL_PRICING: Dict[str, Dict[str, float]] = {
    # 输入: 每1K tokens输入价格
    # 输出: 每1K tokens输出价格
    "agnes-default": {
        "input": float(os.getenv("AGNES_INPUT_PRICE_PER_1K", "0.002")),
        "output": float(os.getenv("AGNES_OUTPUT_PRICE_PER_1K", "0.006")),
    },
    "gpt-4": {
        "input": 0.03,
        "output": 0.06,
    },
    "gpt-3.5-turbo": {
        "input": 0.0015,
        "output": 0.002,
    },
    "claude-3": {
        "input": 0.003,
        "output": 0.015,
    },
}


def calculate_cost(
    total_tokens: int,
    *,
    model: str = "agnes-default",
    input_ratio: float = 0.7,
) -> dict:
    """计算Token消耗的成本。

    Args:
        total_tokens: 总Token数（估算值）
        model: 模型名称
        input_ratio: 输入Token占比（默认70%）

    Returns:
        成本明细字典
    """
    pricing = DEFAULT_MODEL_PRICING.get(model, DEFAULT_MODEL_PRICING["agnes-default"])

    input_tokens = int(total_tokens * input_ratio)
    output_tokens = total_tokens - input_tokens

    input_cost = input_tokens / 1000 * pricing["input"]
    output_cost = output_tokens / 1000 * pricing["output"]
    total_cost = input_cost + output_cost

    return {
        "model": model,
        "total_tokens": total_tokens,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "input_cost": round(input_cost, 4),
        "output_cost": round(output_cost, 4),
        "total_cost": round(total_cost, 4),
        "currency": "CNY",
    }


def get_cost_summary(metrics: dict, *, model: str = "agnes-default") -> dict:
    """从指标数据计算成本汇总。

    Args:
        metrics: get_prompt_metrics() 返回的数据
        model: 模型名称

    Returns:
        成本汇总
    """
    total_tokens = sum(m.get("total_tokens", 0) for m in metrics.values())
    total_calls = sum(m.get("calls", 0) for m in metrics.values())

    cost = calculate_cost(total_tokens, model=model)

    return {
        **cost,
        "total_calls": total_calls,
        "cost_per_call": round(cost["total_cost"] / total_calls, 4) if total_calls > 0 else 0,
    }


def compare_models(metrics: dict, *, models: list[str] | None = None) -> list[dict]:
    """多模型成本对比。

    Args:
        metrics: get_prompt_metrics() 返回的数据
        models: 要对比的模型列表，默认所有已配置模型

    Returns:
        各模型成本对比列表
    """
    if models is None:
        models = list(DEFAULT_MODEL_PRICING.keys())

    results = []
    for model in models:
        summary = get_cost_summary(metrics, model=model)
        results.append({
            "model": model,
            "total_cost": summary["total_cost"],
            "cost_per_call": summary["cost_per_call"],
        })

    # 按成本升序排列
    results.sort(key=lambda x: x["total_cost"])
    return results


def generate_cost_optimization_suggestions(metrics: dict) -> list[dict]:
    """生成成本优化建议。

    分析哪些提示词最值得优化（高Token消耗 + 高频调用）。

    Args:
        metrics: get_prompt_metrics() 返回的数据

    Returns:
        优化建议列表
    """
    suggestions = []

    # 按总Token消耗排序
    sorted_prompts = sorted(
        metrics.items(),
        key=lambda x: x[1].get("total_tokens", 0),
        reverse=True,
    )

    total_tokens = sum(m.get("total_tokens", 0) for m in metrics.values())
    if total_tokens == 0:
        return []

    # Top 5 高消耗提示词
    for key, data in sorted_prompts[:5]:
        calls = data.get("calls", 0)
        prompt_tokens = data.get("total_tokens", 0)
        ratio = prompt_tokens / total_tokens * 100

        if calls < 10:  # 调用太少不建议优化
            continue

        avg_tokens_per_call = prompt_tokens / calls if calls > 0 else 0

        suggestion = {
            "key": key,
            "calls": calls,
            "total_tokens": prompt_tokens,
            "tokens_ratio": round(ratio, 1),
            "avg_tokens_per_call": int(avg_tokens_per_call),
            "priority": "high" if ratio > 20 else ("medium" if ratio > 10 else "low"),
        }

        # 生成具体建议
        if avg_tokens_per_call > 2000:
            suggestion["suggestion"] = f"提示词 {key} 平均每次消耗 {int(avg_tokens_per_call)} tokens，建议精简模板内容或优化变量注入"
        elif calls > 1000:
            suggestion["suggestion"] = f"提示词 {key} 调用量高达 {calls} 次，每次优化100 tokens可节省约 ¥{calls * 100 / 1000 * 0.004:.2f}"
        else:
            suggestion["suggestion"] = f"提示词 {key} 占总消耗 {ratio:.1f}%，可评估是否需要保留全部内容"

        suggestions.append(suggestion)

    return suggestions


# ──────────────────────────────────────────────────────────────────────
# 成本优化跟踪
# ──────────────────────────────────────────────────────────────────────
# 记录优化前后的成本快照
_optimization_baselines: dict[str, dict] = {}


def record_optimization_baseline(name: str, metrics: dict):
    """记录优化前的成本基线快照。

    Args:
        name: 优化项目名称
        metrics: get_prompt_metrics() 返回的数据
    """
    from datetime import datetime

    summary = get_cost_summary(metrics)
    _optimization_baselines[name] = {
        "recorded_at": datetime.now().isoformat(),
        "total_calls": summary["total_calls"],
        "total_tokens": summary["total_tokens"],
        "total_cost": summary["total_cost"],
        "cost_per_call": summary["cost_per_call"],
    }


def compare_optimization_result(name: str, current_metrics: dict) -> dict | None:
    """对比优化前后的成本变化。

    Args:
        name: 优化项目名称
        current_metrics: 当前 get_prompt_metrics() 返回的数据

    Returns:
        优化对比结果，如果没有基线返回None
    """
    if name not in _optimization_baselines:
        return None

    baseline = _optimization_baselines[name]
    current = get_cost_summary(current_metrics)

    cost_change = current["total_cost"] - baseline["total_cost"]
    cost_change_ratio = (cost_change / baseline["total_cost"] * 100) if baseline["total_cost"] > 0 else 0

    return {
        "name": name,
        "baseline": baseline,
        "current": {
            "total_calls": current["total_calls"],
            "total_tokens": current["total_tokens"],
            "total_cost": current["total_cost"],
            "cost_per_call": current["cost_per_call"],
        },
        "improvement": {
            "cost_saved": round(-cost_change, 2),
            "cost_reduction_ratio": round(-cost_change_ratio, 1),
            "per_call_saved": round(baseline["cost_per_call"] - current["cost_per_call"], 4),
        },
    }


__all__ = [
    "calculate_cost",
    "get_cost_summary",
    "compare_models",
    "generate_cost_optimization_suggestions",
    "record_optimization_baseline",
    "compare_optimization_result",
    "DEFAULT_MODEL_PRICING",
]
