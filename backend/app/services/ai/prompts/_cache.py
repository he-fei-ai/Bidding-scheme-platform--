"""提示词运行时缓存。

实现 DB 优先、回退硬编码的读取策略。
- 启动时从数据库 prompt_templates 表加载所有提示词到 _prompt_cache
- get_prompt() 优先返回 DB 内容，缺失时回退到 _ALL_PROMPTS
- reload_prompt_cache() 用于前端编辑保存后强制刷新
- 增强：自动清洗模板文本、缺失变量警告
- 增强：线程安全的缓存操作
- 增强：render_prompt() 统一变量渲染接口
"""

from __future__ import annotations

import logging
import os
import threading
import time

from ._registry import (
    _ALL_PROMPTS,
    clean_prompt_text,
    extract_variables,
    has_residual_placeholders,
    render_prompt,
    validate_prompt_variables,
)
from ._context import assemble_prompt_context, truncate_to_tokens


logger = logging.getLogger(__name__)

# 模块级缓存，使用锁保护
_prompt_cache: dict[str, str] | None = None
_cache_lock = threading.Lock()

# ──────────────────────────────────────────────────────────────────────
# 监控指标（2026-10-06 增强）
# ──────────────────────────────────────────────────────────────────────
# 记录每个提示词的调用次数、DB命中次数、总Token消耗
_prompt_metrics: dict[str, dict] = {}
_metrics_lock = threading.Lock()

# 历史数据快照（用于趋势图）
_metrics_history: list[dict] = []
MAX_HISTORY_POINTS = 288  # 保留最近24小时（每5分钟一个点）

# 告警阈值配置（可通过环境变量或API动态覆盖）
DB_HIT_RATIO_WARN_THRESHOLD = float(os.getenv("PROMPT_METRICS_DB_HIT_THRESHOLD", "0.8"))  # DB命中率低于80%告警
CALL_COUNT_SPIKE_MULTIPLIER = float(os.getenv("PROMPT_METRICS_SPIKE_MULTIPLIER", "5.0"))   # 调用量较基线飙升5倍告警
CALL_COUNT_SPIKE_MIN_BASELINE = int(os.getenv("PROMPT_METRICS_SPIKE_MIN_BASELINE", "10"))  # 基线至少10次才触发飙升告警


def set_alert_thresholds(
    db_hit_threshold: float | None = None,
    spike_multiplier: float | None = None,
    spike_min_baseline: int | None = None,
):
    """动态更新告警阈值配置。

    Args:
        db_hit_threshold: DB命中率告警阈值
        spike_multiplier: 调用量飙升倍数
        spike_min_baseline: 飙升告警最小基线
    """
    global DB_HIT_RATIO_WARN_THRESHOLD, CALL_COUNT_SPIKE_MULTIPLIER, CALL_COUNT_SPIKE_MIN_BASELINE

    if db_hit_threshold is not None:
        DB_HIT_RATIO_WARN_THRESHOLD = db_hit_threshold
    if spike_multiplier is not None:
        CALL_COUNT_SPIKE_MULTIPLIER = spike_multiplier
    if spike_min_baseline is not None:
        CALL_COUNT_SPIKE_MIN_BASELINE = spike_min_baseline

    _save_config_to_file()  # 自动保存到文件
    logger.info("告警阈值已更新: DB命中率=%.2f, 飙升倍数=%.1f, 最小基线=%d",
                DB_HIT_RATIO_WARN_THRESHOLD, CALL_COUNT_SPIKE_MULTIPLIER, CALL_COUNT_SPIKE_MIN_BASELINE)


def get_alert_thresholds() -> dict:
    """获取当前告警阈值配置。"""
    return {
        "db_hit_threshold": DB_HIT_RATIO_WARN_THRESHOLD,
        "spike_multiplier": CALL_COUNT_SPIKE_MULTIPLIER,
        "spike_min_baseline": CALL_COUNT_SPIKE_MIN_BASELINE,
    }

# 上一轮基线（用于检测飙升）
_last_metrics_snapshot: dict[str, int] = {}

# 告警抑制（2026-10-06 增强）
# 记录已推送的告警，避免短时间内重复推送
_sent_alerts: dict[str, float] = {}
ALERT_SUPPRESSION_WINDOW = int(os.getenv("PROMPT_ALERT_SUPPRESSION_WINDOW", "3600"))  # 默认1小时抑制窗口

# 成本预算告警（2026-10-06 增强）
MONTHLY_BUDGET_CNY = float(os.getenv("PROMPT_MONTHLY_BUDGET", "100.0"))  # 默认月度预算100元

# 配置持久化文件路径
_CONFIG_FILE = os.path.join("logs", "prompt_monitoring_config.json")


def _load_saved_config():
    """从文件加载已保存的配置（启动时调用）。"""
    global MONTHLY_BUDGET_CNY, DB_HIT_RATIO_WARN_THRESHOLD, CALL_COUNT_SPIKE_MULTIPLIER, CALL_COUNT_SPIKE_MIN_BASELINE

    if not os.path.exists(_CONFIG_FILE):
        return

    try:
        import json
        with open(_CONFIG_FILE, "r", encoding="utf-8") as f:
            config = json.load(f)

        if "monthly_budget" in config:
            MONTHLY_BUDGET_CNY = config["monthly_budget"]
        if "db_hit_threshold" in config:
            DB_HIT_RATIO_WARN_THRESHOLD = config["db_hit_threshold"]
        if "spike_multiplier" in config:
            CALL_COUNT_SPIKE_MULTIPLIER = config["spike_multiplier"]
        if "spike_min_baseline" in config:
            CALL_COUNT_SPIKE_MIN_BASELINE = config["spike_min_baseline"]

        logger.info("已从文件加载监控配置: %s", _CONFIG_FILE)
    except Exception as e:
        logger.warning("加载监控配置失败: %s", e)


def _save_config_to_file():
    """将当前配置保存到文件。"""
    import json
    os.makedirs(os.path.dirname(_CONFIG_FILE), exist_ok=True)

    config = {
        "monthly_budget": MONTHLY_BUDGET_CNY,
        "db_hit_threshold": DB_HIT_RATIO_WARN_THRESHOLD,
        "spike_multiplier": CALL_COUNT_SPIKE_MULTIPLIER,
        "spike_min_baseline": CALL_COUNT_SPIKE_MIN_BASELINE,
    }

    try:
        with open(_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)
    except Exception as e:
        logger.warning("保存监控配置失败: %s", e)


# 启动时自动加载配置
_load_saved_config()


def set_monthly_budget(amount: float):
    """动态设置月度预算（覆盖环境变量）。

    Args:
        amount: 月度预算金额（元）
    """
    global MONTHLY_BUDGET_CNY
    MONTHLY_BUDGET_CNY = amount
    _save_config_to_file()  # 自动保存到文件
    logger.info("月度预算已更新为 ¥%.2f", amount)


def get_monthly_budget() -> float:
    """获取当前月度预算。"""
    return MONTHLY_BUDGET_CNY


def predict_month_end_cost() -> dict:
    """预测月底成本是否会超预算。

    基于本月已消耗的成本和当前日期，按当前消耗速度推算月底总成本。

    Returns:
        预测结果，含预计月底成本、是否超预算、超预算金额等
    """
    import calendar
    from datetime import datetime

    today = datetime.now()
    day_of_month = today.day
    days_in_month = calendar.monthrange(today.year, today.month)[1]

    # 获取当前已消耗成本
    from ._cost import get_cost_summary
    metrics = get_prompt_metrics()
    current_cost = get_cost_summary(metrics)["total_cost"]

    # 按比例推算月底总成本
    if day_of_month > 0:
        daily_avg = current_cost / day_of_month
        predicted_month_end = daily_avg * days_in_month
    else:
        predicted_month_end = current_cost

    budget = get_monthly_budget()
    will_exceed = predicted_month_end > budget
    over_amount = max(0, predicted_month_end - budget)

    # 置信度：如果当月已过一半以上，置信度高
    progress_ratio = day_of_month / days_in_month
    confidence = "high" if progress_ratio > 0.5 else ("medium" if progress_ratio > 0.2 else "low")

    return {
        "current_day": day_of_month,
        "days_in_month": days_in_month,
        "month_progress_ratio": round(progress_ratio * 100, 1),
        "current_cost": round(current_cost, 2),
        "daily_avg_cost": round(daily_avg, 2),
        "predicted_month_end_cost": round(predicted_month_end, 2),
        "monthly_budget": budget,
        "will_exceed_budget": will_exceed,
        "predicted_over_amount": round(over_amount, 2),
        "confidence": confidence,
    }


def suggest_next_month_budget() -> dict:
    """基于历史消耗建议下月预算。

    Returns:
        建议预算及依据
    """
    prediction = predict_month_end_cost()
    predicted_end = prediction["predicted_month_end_cost"]

    # 建议预算 = 预测月底成本 × 1.2（留20%缓冲）
    suggested = predicted_end * 1.2

    # 如果历史数据不足，建议保守值
    if prediction["confidence"] == "low":
        suggested = max(suggested, prediction["monthly_budget"])

    return {
        "current_month_predicted_cost": predicted_end,
        "current_budget": prediction["monthly_budget"],
        "suggested_next_month_budget": round(suggested, 2),
        "increase_ratio": round((suggested / prediction["monthly_budget"] - 1) * 100, 1),
        "buffer_ratio": 20,
        "confidence": prediction["confidence"],
        "reason": f"基于本月已消耗¥{prediction['current_cost']:.2f}（{prediction['month_progress_ratio']}%进度）推算，留20%缓冲",
    }


def get_prompt_metrics() -> dict[str, dict]:
    """获取提示词使用监控指标。

    Returns:
        {key: {calls, db_hits, fallback_hits, total_chars, total_tokens}}
    """
    with _metrics_lock:
        # 返回深拷贝，避免外部修改影响内部状态
        return {k: dict(v) for k, v in _prompt_metrics.items()}


def snapshot_metrics_history() -> dict:
    """记录一次历史快照（用于趋势图）。

    Returns:
        本次快照数据
    """
    import time
    snapshot = {
        "timestamp": time.time(),
        "total_calls": sum(m.get("calls", 0) for m in _prompt_metrics.values()),
        "total_tokens": sum(m.get("total_tokens", 0) for m in _prompt_metrics.values()),
        "total_db_hits": sum(m.get("db_hits", 0) for m in _prompt_metrics.values()),
    }
    with _metrics_lock:
        _metrics_history.append(snapshot)
        # 只保留最近 N 个点
        if len(_metrics_history) > MAX_HISTORY_POINTS:
            _metrics_history.pop(0)
    return snapshot


def get_metrics_history(hours: int = 24) -> list[dict]:
    """获取历史指标数据。

    Args:
        hours: 最近多少小时的数据

    Returns:
        历史快照列表
    """
    cutoff = time.time() - hours * 3600
    with _metrics_lock:
        return [h for h in _metrics_history if h["timestamp"] >= cutoff]


def reset_prompt_metrics() -> None:
    """重置监控指标（测试用）。"""
    global _last_metrics_snapshot, _metrics_history
    with _metrics_lock:
        _prompt_metrics.clear()
        _last_metrics_snapshot = {}
        _metrics_history = []


def check_prompt_alerts() -> list[dict]:
    """检查提示词监控告警。

    告警规则：
    1. DB命中率低于阈值（默认80%）
    2. 调用量较上次快照飙升超过倍数（默认5倍）

    Returns:
        告警列表，每条含 level/key/message
    """
    alerts = []
    global _last_metrics_snapshot, _sent_alerts

    with _metrics_lock:
        current_calls = {k: v.get("calls", 0) for k, v in _prompt_metrics.items()}

        # 检查1：DB命中率
        for key, data in _prompt_metrics.items():
            calls = data.get("calls", 0)
            if calls < 5:  # 调用太少不告警
                continue
            db_hits = data.get("db_hits", 0)
            ratio = db_hits / calls if calls > 0 else 0
            if ratio < DB_HIT_RATIO_WARN_THRESHOLD:
                alerts.append({
                    "level": "warning",
                    "type": "db_hit_ratio_low",
                    "key": key,
                    "message": f"提示词 {key} DB命中率仅 {ratio:.1%}（{db_hits}/{calls}），低于阈值 {DB_HIT_RATIO_WARN_THRESHOLD:.0%}",
                })

        # 检查2：调用量飙升
        if _last_metrics_snapshot:
            for key, calls in current_calls.items():
                baseline = _last_metrics_snapshot.get(key, 0)
                if baseline >= CALL_COUNT_SPIKE_MIN_BASELINE:
                    spike_ratio = calls / baseline
                    if spike_ratio > CALL_COUNT_SPIKE_MULTIPLIER:
                        alerts.append({
                            "level": "warning",
                            "type": "call_count_spike",
                            "key": key,
                            "message": f"提示词 {key} 调用量飙升：{baseline} → {calls}（{spike_ratio:.1f}倍）",
                        })

        # 检查3：月度成本预算超支
        from ._cost import get_cost_summary
        cost = get_cost_summary(_prompt_metrics)
        monthly_cost = cost.get("total_cost", 0)
        if monthly_cost > MONTHLY_BUDGET_CNY:
            alerts.append({
                "level": "critical",
                "type": "budget_exceeded",
                "key": "_system",
                "message": f"月度成本已达 ¥{monthly_cost:.2f}，超过预算 ¥{MONTHLY_BUDGET_CNY:.2f}（{monthly_cost/MONTHLY_BUDGET_CNY*100:.1f}%）",
            })

        # 检查4：预测月底将超预算（提前预警）
        if monthly_cost < MONTHLY_BUDGET_CNY:  # 还没超才需要预测
            prediction = predict_month_end_cost()
            if prediction["will_exceed_budget"]:
                alerts.append({
                    "level": "warning",
                    "type": "budget_predicted_exceed",
                    "key": "_system",
                    "message": f"按当前消耗速度预测月底将达 ¥{prediction['predicted_month_end_cost']:.2f}，预计超预算 ¥{prediction['predicted_over_amount']:.2f}（置信度：{prediction['confidence']}）",
                })

        # 更新快照
        _last_metrics_snapshot = current_calls.copy()

    # 触发通知钩子（如果已注册）
    # 告警抑制：相同告警在时间窗口内不重复推送
    now = time.time()
    new_alerts = []
    for alert in alerts:
        alert_key = f"{alert.get('type')}:{alert.get('key')}"
        last_sent = _sent_alerts.get(alert_key, 0)
        if now - last_sent > ALERT_SUPPRESSION_WINDOW:
            new_alerts.append(alert)
            _sent_alerts[alert_key] = now

    # 清理过期的告警记录
    _sent_alerts = {k: v for k, v in _sent_alerts.items() if now - v < ALERT_SUPPRESSION_WINDOW * 2}

    if new_alerts and _alert_notification_hook:
        try:
            _alert_notification_hook(new_alerts)
        except Exception as e:
            logger.warning("告警通知钩子执行失败: %s", e)

    return alerts


# ──────────────────────────────────────────────────────────────────────
# 告警通知钩子（2026-10-06 增强）
# ──────────────────────────────────────────────────────────────────────
# 用户可注册自定义通知函数，用于接入企业微信/邮件/钉钉等
_alert_notification_hook = None


def register_alert_notification_hook(func):
    """注册告警通知钩子函数。

    Args:
        func: 通知函数，接收 alerts 列表作为参数。
              例如：
              def send_wechat_notification(alerts):
                  # 调用企业微信webhook
                  ...
    """
    global _alert_notification_hook
    _alert_notification_hook = func
    logger.info("告警通知钩子已注册")


def unregister_alert_notification_hook():
    """注销告警通知钩子。"""
    global _alert_notification_hook
    _alert_notification_hook = None
    logger.info("告警通知钩子已注销")


# ──────────────────────────────────────────────────────────────────────
# 自动快照调度（2026-10-06 增强）
# ──────────────────────────────────────────────────────────────────────
_snapshot_timer = None
_snapshot_interval = int(os.getenv("PROMPT_METRICS_SNAPSHOT_INTERVAL", "300"))  # 默认5分钟


def start_auto_snapshot(interval: int | None = None):
    """启动自动快照定时任务。

    Args:
        interval: 快照间隔（秒），默认从环境变量读取，否则300秒
    """
    global _snapshot_timer, _snapshot_interval
    if interval is not None:
        _snapshot_interval = interval

    if _snapshot_timer is not None:
        return  # 已启动

    def _tick():
        try:
            snapshot_metrics_history()
        except Exception as e:
            logger.warning("自动快照失败: %s", e)
        finally:
            # 调度下一次
            global _snapshot_timer
            _snapshot_timer = threading.Timer(_snapshot_interval, _tick)
            _snapshot_timer.daemon = True
            _snapshot_timer.start()

    _snapshot_timer = threading.Timer(_snapshot_interval, _tick)
    _snapshot_timer.daemon = True
    _snapshot_timer.start()
    logger.info("自动快照已启动，间隔 %d 秒", _snapshot_interval)


def stop_auto_snapshot():
    """停止自动快照定时任务。"""
    global _snapshot_timer
    if _snapshot_timer is not None:
        _snapshot_timer.cancel()
        _snapshot_timer = None
        logger.info("自动快照已停止")


def predict_token_usage(hours_ahead: int = 24) -> dict:
    """基于历史数据预测未来Token消耗（移动平均法）。

    使用简单移动平均（SMA）替代简单速率外推，更平滑、更稳定。

    Args:
        hours_ahead: 预测未来多少小时

    Returns:
        预测结果，含每日预计消耗、置信度等
    """
    history = get_metrics_history(24)

    if len(history) < 3:
        return {
            "available": False,
            "message": "历史数据不足，无法预测（至少需要3个数据点）",
            "hours_ahead": hours_ahead,
        }

    # 计算每个区间的Token消耗速率（每小时）
    rates_per_hour = []
    for i in range(1, len(history)):
        dt = history[i]["timestamp"] - history[i-1]["timestamp"]
        dtokens = history[i]["total_tokens"] - history[i-1]["total_tokens"]
        if dt > 0 and dtokens >= 0:
            rates_per_hour.append(dtokens / dt * 3600)  # 转换为每小时

    if not rates_per_hour:
        return {
            "available": False,
            "message": "无法计算消耗速率",
            "hours_ahead": hours_ahead,
        }

    # 移动平均：最近N个点的平均
    window = min(5, len(rates_per_hour))
    recent_rates = rates_per_hour[-window:]
    avg_rate = sum(recent_rates) / len(recent_rates)

    predicted_tokens = avg_rate * hours_ahead

    # 计算趋势（最近12小时 vs 之前12小时）
    mid_idx = len(rates_per_hour) // 2
    first_half_avg = sum(rates_per_hour[:mid_idx]) / max(1, mid_idx)
    second_half_avg = sum(rates_per_hour[mid_idx:]) / max(1, len(rates_per_hour) - mid_idx)

    trend = "stable"
    if first_half_avg > 0:
        change_ratio = (second_half_avg - first_half_avg) / first_half_avg
        if change_ratio > 0.2:
            trend = "increasing"
        elif change_ratio < -0.2:
            trend = "decreasing"

    # 计算波动率（标准差/均值），用于置信度
    if len(recent_rates) > 1:
        mean_rate = sum(recent_rates) / len(recent_rates)
        variance = sum((r - mean_rate) ** 2 for r in recent_rates) / len(recent_rates)
        std_dev = variance ** 0.5
        cv = std_dev / mean_rate if mean_rate > 0 else 1
        confidence = "high" if cv < 0.1 else ("medium" if cv < 0.3 else "low")
    else:
        confidence = "low"

    return {
        "available": True,
        "hours_ahead": hours_ahead,
        "avg_tokens_per_hour": int(avg_rate),
        "predicted_total_tokens": int(predicted_tokens),
        "trend": trend,
        "data_points_used": len(history),
        "confidence": confidence,
        "method": "moving_average",
        "window_size": window,
    }


def generate_monthly_report(output_dir: str = "logs/prompt_reports", period: str = "monthly") -> str:
    """生成监控报表并保存到文件。

    Args:
        output_dir: 报表输出目录
        period: 报表周期 - monthly/quarterly/yearly

    Returns:
        生成的报表文件路径
    """
    import json
    import os
    from datetime import datetime

    # 确保目录存在
    os.makedirs(output_dir, exist_ok=True)

    # 按周期确定历史数据范围
    period_days = {
        "monthly": 30,
        "quarterly": 90,
        "yearly": 365,
    }
    days = period_days.get(period, 30)

    # 收集数据
    metrics = get_prompt_metrics()
    history = get_metrics_history(24 * days)
    alerts = check_prompt_alerts()

    # 计算成本
    from ._cost import get_cost_summary
    cost = get_cost_summary(metrics)

    # 获取费用分摊数据
    cost_allocation = get_cost_allocation()

    # 生成报表
    report = {
        "report_type": f"{period}_prompt_monitoring",
        "period": period,
        "period_days": days,
        "generated_at": datetime.now().isoformat(),
        "summary": {
            "total_prompts": len(metrics),
            "total_calls": sum(m.get("calls", 0) for m in metrics.values()),
            "total_tokens": sum(m.get("total_tokens", 0) for m in metrics.values()),
            "alerts_count": len(alerts),
        },
        "cost": cost,
        "cost_allocation": cost_allocation,
        "top_prompts": sorted(
            [{"key": k, **v} for k, v in metrics.items()],
            key=lambda x: x.get("calls", 0),
            reverse=True,
        )[:20],
        "alerts": alerts,
        "history_points": len(history),
    }

    # 保存到文件
    period_suffix = {
        "monthly": datetime.now().strftime('%Y%m'),
        "quarterly": f"{datetime.now().year}Q{(datetime.now().month - 1) // 3 + 1}",
        "yearly": str(datetime.now().year),
    }
    filename = f"prompt_report_{period_suffix.get(period, datetime.now().strftime('%Y%m'))}.json"
    filepath = os.path.join(output_dir, filename)
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    logger.info("月度报表已生成: %s", filepath)

    # 自动发送邮件（如果配置了邮件通知）
    try:
        from ._notifications import send_email_report
        send_email_report(report, filepath)
    except Exception as e:
        logger.warning("月度报表邮件发送失败: %s", e)

    return filepath


# ──────────────────────────────────────────────────────────────────────
# 费用分摊（2026-10-06 增强）
# ──────────────────────────────────────────────────────────────────────
# 按标签/项目维度的指标统计（预留接口，后续可扩展）
_tagged_metrics: dict[str, dict] = {}
_tag_lock = threading.Lock()


def record_tagged_call(tag: str, tokens: int = 0):
    """记录带标签的调用（用于费用分摊）。

    Args:
        tag: 标签（项目ID/部门ID等）
        tokens: 本次消耗的Token数
    """
    with _tag_lock:
        if tag not in _tagged_metrics:
            _tagged_metrics[tag] = {"calls": 0, "total_tokens": 0}
        _tagged_metrics[tag]["calls"] += 1
        _tagged_metrics[tag]["total_tokens"] += tokens


def get_tagged_metrics() -> dict:
    """获取按标签维度的指标统计（用于费用分摊）。"""
    with _tag_lock:
        return {k: dict(v) for k, v in _tagged_metrics.items()}


def get_cost_allocation() -> list[dict]:
    """按标签计算费用分摊。

    Returns:
        各标签的成本占比列表
    """
    from ._cost import calculate_cost

    tagged = get_tagged_metrics()
    total_tokens = sum(v["total_tokens"] for v in tagged.values())

    if total_tokens == 0:
        return []

    result = []
    for tag, data in tagged.items():
        cost = calculate_cost(data["total_tokens"])
        result.append({
            "tag": tag,
            "calls": data["calls"],
            "total_tokens": data["total_tokens"],
            "tokens_ratio": round(data["total_tokens"] / total_tokens * 100, 1),
            "estimated_cost": cost["total_cost"],
        })

    # 按成本降序
    result.sort(key=lambda x: x["estimated_cost"], reverse=True)
    return result


def _new_metrics_entry() -> dict:
    """创建新的指标条目。"""
    return {
        "calls": 0,
        "db_hits": 0,
        "fallback_hits": 0,
        "not_found": 0,
        "total_chars": 0,
        "total_tokens": 0,
    }


def _check_hardcode_drift() -> None:
    """比对硬编码默认值与DB内容的差异，检测漂移。

    场景：开发者修改了硬编码提示词，但没有同步执行 init_prompt_templates.py
    初始化到 DB。此时 DB 中仍是旧版本，get_prompt 优先返回旧内容，
    导致修改不生效。

    本函数在缓存加载完成后自动执行，输出漂移告警日志，提示管理员同步。
    """
    if not _prompt_cache:
        return

    drifted_keys = []
    for key, meta in _ALL_PROMPTS.items():
        if key not in _prompt_cache:
            continue  # DB中不存在，使用硬编码，无漂移
        db_content = _prompt_cache[key]
        default_content = meta["content"]
        # 清洗后比对（DB加载时已清洗，这里对硬编码也清洗一次）
        default_cleaned = clean_prompt_text(default_content)
        if db_content != default_cleaned:
            drifted_keys.append(key)

    if drifted_keys:
        logger.warning(
            "提示词硬编码与DB内容存在差异（共 %d 个）：%s\n"
            "如需同步硬编码新版本到DB，请执行 init_prompt_templates.py 或在管理页重置对应提示词。",
            len(drifted_keys),
            ", ".join(drifted_keys[:10]) + ("..." if len(drifted_keys) > 10 else ""),
        )

# BUG-FIX 2026-09-14（中·性能）：DB 加载失败时原实现把 _prompt_cache 置回 None，
# 于是**每一次 get_prompt 调用都会重新查一次库**。get_prompt 位于 AI 热路径
# （每个章节、每次 JSON 修复、每次图表编排都会调用），DB 异常时会退化成
# "100 章节 = 100 次失败查询 + 100 次异常构造"，且这些查询还要抢全局写锁、
# 阻塞整个事件循环。修复：失败后进入冷却期，冷却期内直接走硬编码回退。
_PROMPT_CACHE_RETRY_INTERVAL = 60.0
_load_failed_at: float | None = None


def _load_prompt_cache():
    """从数据库加载所有提示词到缓存，并自动清洗。

    线程安全版本。
    """
    global _prompt_cache, _load_failed_at
    with _cache_lock:
        try:
            from ....db import get_db  # app.services.ai.prompts._cache -> app.db

            # BUG-FIX 2026-09-14：纯读查询走 readonly=True，跳过全局写锁，
            # 避免与正文/目录的并发写入互相串行化。
            with get_db(readonly=True) as conn:
                rows = conn.execute("SELECT key, content FROM prompt_templates").fetchall()
                _prompt_cache = {}
                for row in rows:
                    cleaned = clean_prompt_text(row["content"])
                    _prompt_cache[row["key"]] = cleaned
            _load_failed_at = None
            logger.info("Prompt cache loaded: %d templates from DB", len(_prompt_cache))
            # 自动比对硬编码与DB内容差异（2026-10-06 增强）
            _check_hardcode_drift()
        except Exception as e:
            logger.warning("Failed to load prompt cache from DB: %s", e)
            # BUG-FIX-45 修复：原将 _prompt_cache 设为 {}（空字典），导致后续
            # get_prompt 中 `if _prompt_cache is None` 永远为 False，永远不会
            # 重试 DB 加载——即使 DB 后来恢复也一直走硬编码回退。改为保持 None，
            # 下次 get_prompt 调用时会重试加载。
            _prompt_cache = None
            _load_failed_at = time.time()


def get_prompt(key: str, **kwargs) -> str:
    """获取提示词，优先读取数据库，回退到硬编码。

    当提供 kwargs 时，自动使用 render_prompt 替换变量。

    BUG-FIX (并发安全): 原实现用 if _prompt_cache is None 无锁检查，
    高并发下可能多个线程同时看到 None → 并发查 DB → 无谓 I/O。
    改为在锁内做一次性检查，同时保证 _prompt_cache 的可见性。
    """
    global _prompt_cache, _load_failed_at
    # 锁内检查 + 加载，保证并发安全
    with _cache_lock:
        if _prompt_cache is None:
            # BUG-FIX 2026-09-14（性能）：上次加载失败且在冷却期内 → 不再查库，
            # 直接落到下面的硬编码回退分支，避免 AI 热路径被失败的 DB 查询拖垮。
            if _load_failed_at is not None and (
                time.time() - _load_failed_at < _PROMPT_CACHE_RETRY_INTERVAL
            ):
                logger.debug("Prompt cache 处于失败冷却期，本次直接走硬编码回退")
            else:
                _prompt_cache = {}  # 先占位，避免其它线程再触发加载
                try:
                    from ....db import get_db  # app.services.ai.prompts._cache -> app.db

                    # 纯读查询：readonly=True 跳过全局写锁
                    with get_db(readonly=True) as conn:
                        rows = conn.execute(
                            "SELECT key, content FROM prompt_templates"
                        ).fetchall()
                        local_cache: dict[str, str] = {}
                        for row in rows:
                            cleaned = clean_prompt_text(row["content"])
                            local_cache[row["key"]] = cleaned
                        _prompt_cache = local_cache
                        _load_failed_at = None
                        logger.info("Prompt cache loaded: %d templates from DB", len(local_cache))
                        # 自动比对硬编码与DB内容差异（2026-10-06 增强）
                        _check_hardcode_drift()
                except Exception as e:
                    logger.warning("Failed to load prompt cache from DB: %s", e)
                    _prompt_cache = None  # 保持 None，冷却期后重试
                    _load_failed_at = time.time()

    if _prompt_cache and key in _prompt_cache:
        prompt = _prompt_cache[key]
        source = "db"
    else:
        meta = _ALL_PROMPTS.get(key)
        if meta:
            prompt = meta["content"]
            source = "fallback"
        else:
            logger.warning("Prompt key '%s' not found in registry or cache", key)
            # 记录未命中指标
            with _metrics_lock:
                m = _prompt_metrics.setdefault(key, _new_metrics_entry())
                m["calls"] += 1
                m["not_found"] = m.get("not_found", 0) + 1
            return ""

    # 记录监控指标
    with _metrics_lock:
        m = _prompt_metrics.setdefault(key, _new_metrics_entry())
        m["calls"] += 1
        m[f"{source}_hits"] = m.get(f"{source}_hits", 0) + 1
        m["total_chars"] = m.get("total_chars", 0) + len(prompt)
        # 粗略 token 估算：中文按字符计，其余按4字符≈1token
        cjk_count = sum(1 for c in prompt if '\u4e00' <= c <= '\u9fff')
        other_count = len(prompt) - cjk_count
        est_tokens = cjk_count + (other_count + 3) // 4
        m["total_tokens"] = m.get("total_tokens", 0) + est_tokens

    # BUG-FIX-PROMPT-D3 2026-10-04：原用 validate_prompt_variables(key, **kwargs)
    # 始终校验**注册表默认模板**的变量集合。BUG-FIX-28 注释声称"统一校验"，但 DB
    # 编辑后实际生效的是缓存内容：用户在 DB 里新增的占位符不会被校验（漏检），
    # 删除的占位符每次调用都误报告警（噪声）。改为对**实际使用的模板内容**做变量校验，
    # 且仅在需要渲染（有 kwargs）时执行，避免抓取原始模板的调用方（如编排/审核）产生噪声。
    if kwargs:
        required_vars = extract_variables(prompt)
        passed_lower = {k.lower() for k, v in kwargs.items() if v is not None}
        missing = [v for v in required_vars if v.lower() not in passed_lower]
        if missing:
            logger.warning(
                "Prompt '%s' has missing variables (not passed in call): %s", key, missing
            )
        # 变量替换（统一走 render_prompt：兼容 {var} 与 __VAR__，大小写不敏感）
        prompt = render_prompt(prompt, **kwargs)

    return prompt


def get_prompt_with_validation(key: str, **kwargs) -> tuple[str, list[str]]:
    """获取提示词并校验变量，返回 (提示词文本, 缺失变量列表)。

    Args:
        key: 提示词键名
        **kwargs: 变量名和值的映射

    Returns:
        tuple: (渲染后的提示词文本, 缺失的变量名列表)
    """
    prompt = get_prompt(key, **kwargs)
    missing = validate_prompt_variables(key, **kwargs)

    # 额外检查：渲染后是否有残留占位符
    if not missing and has_residual_placeholders(prompt):
        residual = extract_variables(prompt)
        logger.warning("Prompt '%s' has residual placeholders after rendering: %s", key, residual)

    return prompt, missing


def reload_prompt_cache():
    """强制重新加载提示词缓存（保存后调用）。

    线程安全版本。
    """
    global _prompt_cache, _load_failed_at
    with _cache_lock:
        _prompt_cache = None
        _load_failed_at = None  # 主动 reload 应立刻重试，不受冷却期限制
    logger.info("Prompt cache invalidated, will reload on next access")


def build_prompt_with_context(key, parts, *, max_tokens=None, sanitize=False, desensitize=False):
    """Unified context injection entry for single-block templates (e.g. __CONTEXT__)."""
    block = assemble_prompt_context(parts, max_tokens=max_tokens)
    return get_prompt(key, CONTEXT=block, sanitize=sanitize, desensitize=desensitize)
