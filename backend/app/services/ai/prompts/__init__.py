"""提示词模板包（拆分重构版）。

原 prompts.py（5588 行 / 148KB）已拆分到以下子模块：
  - json_repair.py : JSON 修复
  - analysis.py    : 文件解析（14 个）
  - outline.py     : 目录生成（8 个）
  - content.py     : 正文生成（5 个）
  - mermaid.py     : Mermaid 系列（6 个）
  - charts.py      : 图表类（架构/劳动力/对比/布局/时间线/修复，11 个）
  - illustration.py: 配图方案（2 个）
  - checks.py      : 废标检查/错别字/逻辑（5 个）
  - knowledge.py   : 知识库（3 个）
  - industry.py    : 行业树（1 个）
  - _shared.py     : 共享规则（3 个，注册到管理界面只读展示）

公共 API：
  - get_prompt(key, **kwargs) -> str  读取并渲染提示词（DB 优先，硬编码回退）
  - get_prompt_with_validation(key, **kwargs) -> tuple  读取并校验变量
  - render_prompt(template, **kwargs) -> str  统一变量渲染函数
  - reload_prompt_cache()    重新加载缓存
  - _ALL_PROMPTS             全部提示词元信息注册表

所有提示词通过 from app.services.ai.prompts import get_prompt 访问。
"""

from __future__ import annotations

# 导入子模块以触发提示词注册
# 注册共享规则到管理界面（只读展示，内容锁定于 _shared.py）
from . import (
    _context,
    _security,
    _shared,
    analysis,
    charts,
    checks,
    content,
    illustration,
    industry,
    json_repair,
    knowledge,
    mermaid,
    outline,
)
from ._cache import (
    build_prompt_with_context,
    check_prompt_alerts,
    get_metrics_history,
    get_prompt,
    get_prompt_metrics,
    get_prompt_with_validation,
    register_alert_notification_hook,
    reload_prompt_cache,
    reset_prompt_metrics,
    snapshot_metrics_history,
    start_auto_snapshot,
    stop_auto_snapshot,
    predict_token_usage,
    generate_monthly_report,
    record_tagged_call,
    get_tagged_metrics,
    get_cost_allocation,
    set_monthly_budget,
    get_monthly_budget,
    predict_month_end_cost,
    suggest_next_month_budget,
    set_alert_thresholds,
    get_alert_thresholds,
    unregister_alert_notification_hook,
)
from ._context import (
    DEFAULT_PRIORITIES,
    assemble_prompt_context,
    build_chapter_context,
    estimate_tokens,
    truncate_to_tokens,
)
from ._registry import (
    _ALL_PROMPTS,
    _reg,
    clean_prompt_text,
    extract_variables,
    get_prompt_variables,
    has_residual_placeholders,
    render_prompt,
    render_prompt_safe,
    validate_prompt_variables,
)
from ._security import (
    desensitize_sensitive_info,
    neutralize_meta_markers,
    sanitize_prompt_value,
)
from ._notifications import (
    send_email_alert,
    send_wechat_work_alert,
    setup_email_notification,
    setup_wechat_notification,
    send_dingtalk_alert,
    setup_dingtalk_notification,
)
from ._cost import (
    calculate_cost,
    compare_models,
    generate_cost_optimization_suggestions,
    get_cost_summary,
    record_optimization_baseline,
    compare_optimization_result,
)
from ._output_repair import (
    extract_json_body,
    repair_json_output,
    strip_code_fences,
    try_parse_json,
    validate_output_format,
)


_reg("SHARED_HEADING_RULES", "共享规则", "标题编号规范（V3.1 七级）", _shared.SHARED_HEADING_RULES)
_reg("SHARED_FORBIDDEN_WORDS", "共享规则", "标题禁用词列表", _shared.SHARED_FORBIDDEN_WORDS)
_reg("SHARED_OUTPUT_SPEC", "共享规则", "输出格式规范", _shared.SHARED_OUTPUT_SPEC)

__all__ = [
    "get_prompt",
    "get_prompt_with_validation",
    "reload_prompt_cache",
    "render_prompt",
    "render_prompt_safe",
    "has_residual_placeholders",
    "neutralize_meta_markers",
    "sanitize_prompt_value",
    "desensitize_sensitive_info",
    "assemble_prompt_context",
    "build_chapter_context",
    "estimate_tokens",
    "truncate_to_tokens",
    "build_prompt_with_context",
    "extract_json_body",
    "repair_json_output",
    "strip_code_fences",
    "try_parse_json",
    "validate_output_format",
    "get_prompt_metrics",
    "reset_prompt_metrics",
    "check_prompt_alerts",
    "get_metrics_history",
    "snapshot_metrics_history",
    "register_alert_notification_hook",
    "unregister_alert_notification_hook",
    "start_auto_snapshot",
    "stop_auto_snapshot",
    "predict_token_usage",
    "generate_monthly_report",
    "record_tagged_call",
    "get_tagged_metrics",
    "get_cost_allocation",
    "set_monthly_budget",
    "get_monthly_budget",
    "predict_month_end_cost",
    "suggest_next_month_budget",
    "set_alert_thresholds",
    "get_alert_thresholds",
    "send_wechat_work_alert",
    "setup_wechat_notification",
    "send_email_alert",
    "setup_email_notification",
    "send_dingtalk_alert",
    "setup_dingtalk_notification",
    "calculate_cost",
    "compare_models",
    "generate_cost_optimization_suggestions",
    "record_optimization_baseline",
    "compare_optimization_result",
    "get_cost_summary",
    "DEFAULT_PRIORITIES",
    "_ALL_PROMPTS",
    "_reg",
    "get_prompt_variables",
    "validate_prompt_variables",
    "clean_prompt_text",
    "extract_variables",
]
