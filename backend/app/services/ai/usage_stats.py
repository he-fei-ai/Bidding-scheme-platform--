"""AI 调用用量统计服务

职责：
  1. 记录每次 AI 调用的 token 用量、响应时间、成本、状态
  2. 计算每日/每月用量汇总
  3. 提供健康度评分（响应时间、成功率）
  4. 预算追踪（日/月 token 预算）

使用方式：
  from app.services.ai.usage_stats import record_call, get_usage_summary, get_health_dashboard

  record_call(
      provider="openai_compatible", model="gpt-4o",
      prompt_tokens=120, completion_tokens=340, cost_usd=0.0012,
      response_time_ms=1800, task_type="outline",
  )
"""

from datetime import datetime, timedelta
import logging
from typing import Any
import uuid

from ...db import get_db


logger = logging.getLogger(__name__)

# 各模型的 approximate 单价（USD per 1M tokens），用于成本估算
_PRICING: dict[str, tuple[float, float]] = {
    # (input_price_per_1m, output_price_per_1m)
    "gpt-4o": (2.50, 10.00),
    "gpt-4o-mini": (0.15, 0.60),
    "deepseek-chat": (0.14, 0.28),
    "deepseek-coder": (0.14, 0.28),
    "qwen-max": (0.80, 2.00),
    "qwen-plus": (0.40, 1.20),
    "glm-4-plus": (0.10, 0.10),
    "glm-4": (0.05, 0.05),
    "glm-4-flash": (0.002, 0.002),
    "claude-3-5-sonnet": (3.00, 15.00),
    "claude-3-haiku": (0.25, 1.25),
    "claude-3-opus": (15.00, 75.00),
    "gemini-1.5-pro": (1.25, 5.00),
    "gemini-1.5-flash": (0.075, 0.30),
}

# agnes 内置模型（免费）
_PRICING["agnes-2.0-flash"] = (0.0, 0.0)


def _get_pricing(model: str) -> tuple[float, float]:
    """获取模型的 token 单价，模糊匹配"""
    model_lower = model.lower()
    for key, prices in _PRICING.items():
        if key in model_lower or model_lower in key:
            return prices
    return (0.0, 0.0)  # 未知模型默认为免费


def record_call(
    provider: str,
    model: str,
    base_url: str,
    *,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    total_tokens: int | None = None,
    cost_usd: float = 0.0,
    response_time_ms: int = 0,
    task_type: str = "unknown",
    success: bool = True,
    error_message: str = "",
    profile_name: str = "default",
) -> str:
    """记录一次 AI 调用，返回 call_id"""
    if total_tokens is None:
        total_tokens = prompt_tokens + completion_tokens

    call_id = str(uuid.uuid4())
    try:
        with get_db() as conn:
            conn.execute(
                """INSERT INTO ai_usage_stats
                   (call_id, provider, model, base_url, task_type,
                    prompt_tokens, completion_tokens, total_tokens,
                    cost_usd, response_time_ms, status, error_message, profile_name)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    call_id,
                    provider,
                    model,
                    base_url,
                    task_type,
                    prompt_tokens,
                    completion_tokens,
                    total_tokens,
                    cost_usd,
                    response_time_ms,
                    "success" if success else "error",
                    error_message[:500] if error_message else "",
                    profile_name,
                ),
            )
            conn.commit()
    except Exception as e:
        logger.debug("记录 AI 调用统计失败: %s", e)

    return call_id


def record_from_response(
    provider: str,
    model: str,
    base_url: str,
    response_data: dict,
    *,
    response_time_ms: int = 0,
    task_type: str = "unknown",
    success: bool = True,
    error_message: str = "",
    profile_name: str = "default",
) -> str:
    """从 OpenAI 兼容响应中提取 token 用量并记录"""
    usage = response_data.get("usage", {})
    prompt_tokens = usage.get("prompt_tokens", 0)
    completion_tokens = usage.get("completion_tokens", 0)
    total_tokens = usage.get("total_tokens", prompt_tokens + completion_tokens)

    # 自动估算成本
    if not usage.get("cost_usd"):
        input_price, output_price = _get_pricing(model)
        cost_usd = (prompt_tokens * input_price + completion_tokens * output_price) / 1_000_000
    else:
        cost_usd = usage.get("cost_usd", 0.0)

    return record_call(
        provider=provider,
        model=model,
        base_url=base_url,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
        cost_usd=cost_usd,
        response_time_ms=response_time_ms,
        task_type=task_type,
        success=success,
        error_message=error_message,
        profile_name=profile_name,
    )


def get_usage_summary(
    days: int = 30,
    task_type: str | None = None,
    profile_name: str | None = None,
) -> dict[str, Any]:
    """获取用量汇总"""
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    where = "WHERE created_at >= ?"
    params: list[Any] = [cutoff]
    if task_type:
        where += " AND task_type = ?"
        params.append(task_type)
    if profile_name:
        where += " AND profile_name = ?"
        params.append(profile_name)

    try:
        with get_db(readonly=True) as conn:
            row = conn.execute(
                f"""SELECT
                    COUNT(*) as total_calls,
                    SUM(prompt_tokens) as total_prompt_tokens,
                    SUM(completion_tokens) as total_completion_tokens,
                    SUM(total_tokens) as total_tokens,
                    SUM(cost_usd) as total_cost_usd,
                    AVG(response_time_ms) as avg_response_time_ms,
                    MIN(response_time_ms) as min_response_time_ms,
                    MAX(response_time_ms) as max_response_time_ms,
                    SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END) as success_count,
                    SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END) as error_count
                FROM ai_usage_stats {where}
            """,
                params,
            ).fetchone()
            if not row:
                return _empty_summary(days, task_type, profile_name)

            total = row["total_calls"] or 1
            return {
                "days": days,
                "total_calls": row["total_calls"],
                "total_prompt_tokens": row["total_prompt_tokens"] or 0,
                "total_completion_tokens": row["total_completion_tokens"] or 0,
                "total_tokens": row["total_tokens"] or 0,
                "total_cost_usd": round(row["total_cost_usd"] or 0.0, 4),
                "total_cost_cny": round((row["total_cost_usd"] or 0.0) * 7.2, 2),
                "avg_response_time_ms": round(row["avg_response_time_ms"] or 0, 0),
                "min_response_time_ms": row["min_response_time_ms"] or 0,
                "max_response_time_ms": row["max_response_time_ms"] or 0,
                "success_count": row["success_count"] or 0,
                "error_count": row["error_count"] or 0,
                "success_rate": round((row["success_count"] or 0) / total * 100, 1),
                "avg_tokens_per_call": round((row["total_tokens"] or 0) / total),
                "period": f"近{days}天",
            }
    except Exception as e:
        logger.debug("获取用量统计失败: %s", e)
        return _empty_summary(days, task_type, profile_name)


def _empty_summary(days: int, task_type: str | None, profile_name: str | None) -> dict:
    return {
        "days": days,
        "total_calls": 0,
        "total_prompt_tokens": 0,
        "total_completion_tokens": 0,
        "total_tokens": 0,
        "total_cost_usd": 0.0,
        "total_cost_cny": 0.0,
        "avg_response_time_ms": 0,
        "min_response_time_ms": 0,
        "max_response_time_ms": 0,
        "success_count": 0,
        "error_count": 0,
        "success_rate": 100.0,
        "avg_tokens_per_call": 0,
        "period": f"近{days}天"
        + (f" [{task_type}]" if task_type else "")
        + (f" [{profile_name}]" if profile_name else ""),
    }


def get_health_dashboard() -> dict[str, Any]:
    """获取各 Provider 的健康度面板"""
    try:
        with get_db(readonly=True) as conn:
            rows = conn.execute("""
                SELECT
                    provider, model, base_url,
                    COUNT(*) as total_calls,
                    SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END) as success,
                    SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END) as failed,
                    AVG(response_time_ms) as avg_ms,
                    MAX(created_at) as last_used,
                    SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END) * 1.0
                        / NULLIF(COUNT(*), 0) as health_score
                FROM ai_usage_stats
                WHERE created_at >= datetime('now', '-7 days')
                GROUP BY provider, model, base_url
                ORDER BY health_score DESC
            """).fetchall()

            providers = []
            for r in rows:
                score = r["health_score"] or 0.0
                total = r["total_calls"] or 0
                # 置信度加权：调用次数越少，可信度越低
                confidence = min(total / 10.0, 1.0)
                weighted_score = score * confidence

                providers.append(
                    {
                        "provider": r["provider"],
                        "model": r["model"],
                        "base_url": r["base_url"],
                        "total_calls": total,
                        "success": r["success"] or 0,
                        "failed": r["failed"] or 0,
                        "success_rate": round(score * 100, 1),
                        "avg_response_time_ms": round(r["avg_ms"] or 0),
                        "last_used": r["last_used"] or "",
                        "health_score": round(weighted_score * 100, 1),
                        "confidence": round(confidence * 100, 0),
                        "status": "healthy"
                        if weighted_score > 0.8
                        else ("degraded" if weighted_score > 0.5 else "unhealthy"),
                    }
                )

            return {"providers": providers, "total_records": len(providers)}
    except Exception as e:
        logger.debug("获取健康面板失败: %s", e)
        return {"providers": [], "total_records": 0}


def get_daily_trend(days: int = 7) -> list[dict]:
    """获取每日调用趋势"""
    try:
        with get_db(readonly=True) as conn:
            rows = conn.execute(
                """
                SELECT
                    date(created_at) as day,
                    task_type,
                    COUNT(*) as calls,
                    SUM(total_tokens) as tokens,
                    SUM(cost_usd) as cost
                FROM ai_usage_stats
                WHERE created_at >= date('now', ? || ' days')
                GROUP BY day, task_type
                ORDER BY day DESC
            """,
                (f"-{days}",),
            ).fetchall()

            return [
                {
                    "day": r["day"],
                    "task_type": r["task_type"],
                    "calls": r["calls"],
                    "tokens": r["tokens"] or 0,
                    "cost_usd": round(r["cost"] or 0.0, 4),
                }
                for r in rows
            ]
    except Exception as e:
        logger.debug("获取趋势数据失败: %s", e)
        return []


def get_token_budget_status(profile_name: str = "default") -> dict[str, Any]:
    """检查 token 预算状态"""
    try:
        with get_db(readonly=True) as conn:
            profile = conn.execute(
                "SELECT token_budget_daily FROM ai_profiles WHERE name = ?", (profile_name,)
            ).fetchone()
            if not profile or not profile["token_budget_daily"]:
                return {
                    "has_budget": False,
                    "daily_limit": 0,
                    "used_today": 0,
                    "remaining": 0,
                    "usage_pct": 0,
                }

            daily_limit = profile["token_budget_daily"]
            today = datetime.now().strftime("%Y-%m-%d")
            row = conn.execute(
                "SELECT COALESCE(SUM(total_tokens), 0) as used FROM ai_usage_stats "
                "WHERE profile_name = ? AND date(created_at) = date(?)",
                (profile_name, today),
            ).fetchone()
            used = row["used"] or 0
            remaining = max(0, daily_limit - used)
            return {
                "has_budget": True,
                "daily_limit": daily_limit,
                "used_today": used,
                "remaining": remaining,
                "usage_pct": round(used / daily_limit * 100, 1) if daily_limit > 0 else 0,
                "budget_exceeded": used >= daily_limit,
            }
    except Exception as e:
        logger.debug("检查预算状态失败: %s", e)
        return {
            "has_budget": False,
            "daily_limit": 0,
            "used_today": 0,
            "remaining": 0,
            "usage_pct": 0,
        }


def truncate_stats(keep_days: int = 90) -> int:
    """清理过期的统计数据"""
    try:
        cutoff = (datetime.now() - timedelta(days=keep_days)).strftime("%Y-%m-%d %H:%M:%S")
        with get_db() as conn:
            # BUG-FIX 2026-09-16（中）：`Connection.total_changes` 是**连接自打开以来**
            # 累计变更的行数（含此前所有 INSERT/UPDATE），不是本次 DELETE 的行数 ——
            # 清理接口返回的数字虚高数万倍，运维根本无法据此判断清理是否生效。
            _cur = conn.execute("DELETE FROM ai_usage_stats WHERE created_at < ?", (cutoff,))
            deleted = _cur.rowcount if _cur is not None else 0
            conn.commit()
            return max(0, deleted)
    except Exception as e:
        logger.debug("清理统计失败: %s", e)
        return 0


def get_stats_summary(days: int = 30) -> dict:
    """向后兼容别名"""
    return get_usage_summary(days=days)
