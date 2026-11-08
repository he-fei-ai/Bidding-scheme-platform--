"""提示词模板包（拆分重构版）。

原 prompts.py（5588 行 / 148KB）已拆分到以下子模块：
  - json_repair.py : JSON 修复
  - analysis.py    : 文件解析（14 个）
  - outline.py     : 目录生成（8 个）
  - content.py     : 正文生成（7 个）
  - mermaid.py     : Mermaid 系列（6 个）
  - charts.py      : 图表类（架构/劳动力/对比/布局/时间线/修复，11 个）
  - illustration.py: 配图方案（2 个）
  - checks.py      : 废标检查/错别字/逻辑（10 个）
  - knowledge.py   : 知识库（3 个）
  - industry.py    : 行业树（1 个）
  - scheme_extraction.py : 五段式专项方案解析提取（10 个）
  - _integrate*.py : 调用方存量硬编码收编（4 批次共 63 个）
  - _shared.py     : 共享规则（3 个，注册到管理界面只读展示）

公共 API：
  - get_prompt(key, **kwargs) -> str  读取并渲染提示词（DB 优先，硬编码回退）
  - get_prompt_with_validation(key, **kwargs) -> tuple  读取并校验变量
  - render_prompt(template, **kwargs) -> str  统一变量渲染函数
  - reload_prompt_cache()    重新加载缓存
  - _ALL_PROMPTS             全部提示词元信息注册表

所有提示词通过 from app.services.ai.prompts import get_prompt 访问。

R6（2026-11-04，零生产调用导出的定位说明）：以下导出为**公共 API 面或预留
统一入口**，当前无生产调用方（两轮调用方审计核实），由测试锁定行为、管理
接口或后续接入方使用——请勿据此判定为死代码而删除：
  - get_prompt_with_validation / render_prompt_safe : 显式校验/安全渲染入口
  - build_prompt_with_context / build_chapter_context : 单块/章节统一上下文
    组装入口（正文链路 workflows_sections 目前自组装，见其 sanitize+截断设计）
  - repair_json_output : 独立场景 JSON 修复（生产主路径为
    workflows_base.collect_json_response 的重修复链，已复用本包
    extract_json_body/try_parse_json 原语）
  - validate_output_format / strip_code_fences : 输出格式门/围栏剥离工具
  - detect_injection_risk / sanitize_for_injection : 注入风险分级处置
    （sanitize_for_injection 的包裹阈值默认读 config.PROMPT_INJECTION_WRAP_THRESHOLD）
"""

from __future__ import annotations

# 导入子模块以触发提示词注册
# 注册共享规则到管理界面（只读展示，内容锁定于 _shared.py）
from . import (
    _context,
    _integrate,  # ENHANCE 2026-10-12：整合调用方散落的存量硬编码提示词
    # ENHANCE 2026-10-18（审计遗留 D2 批次 2）：收编 image_engine / workflows_*
    # 的 user 侧硬编码提示词（system 侧此前已注册表化）
    _integrate_batch2,
    # BUG-FIX-PROMPT-E10（2026-11-03，孤儿注册文件激活）：_integrate_batch3.py
    # 在第八轮审计中创建（收编 facts/qa/模板提取/修复器代际隔离共 13 个 key），
    # 但从未加入本导入链——文件内所有 _reg() 不执行、13 个 key 全部未注册、
    # 注释引用的等价测试也不存在，形成「半途而废的收编批次」。现接入导入链
    # 使注册生效；生产调用方（qa.py / facts_service.py / routers/facts.py /
    # routers/templates.py / json_repair.py / mermaid_repair.py）在本轮
    # BUG-FIX-PROMPT-E10 系列中逐一接线。
    # BUG-FIX-PROMPT-M8（2026-11-04，过期注释）：原名单中的 template_expansion.py
    # 经全仓核查并无任何 prompts 引用（services/template_expansion.py 未导入
    # get_prompt/_gp），属笔误遗留，现从名单移除。
    _integrate_batch3,
    # BUG-FIX-PROMPT-E10（2026-11-03，收编批次 4）：知识补漏 user 侧 2 处 +
    # 可研模块 14 个 build_* 提示词函数，收编进注册表（调用方同步接线，
    # 保留逐字一致的硬编码 fallback，DB 无行时行为零变化）。
    _integrate_batch4,
    # ENHANCE 2026-11-08（收编批次 6）：全局事实提取 user 侧提示词（单段/分段）
    # 收编进注册表，与 FACTS_EXTRACT_SYSTEM 对称；调用方 routers/facts.py 同步接线。
    _integrate_batch6,
    # ENHANCE 2026-11-08（收编批次 7）：目录自修复 system+user 与 Mermaid 直修
    # user 侧收编进注册表；调用方 workflows_quality.py / mermaid_repair.py 接线。
    _integrate_batch7,
    _security,
    _shared,
    analysis,
    charts,
    checks,
    content,
    industry,
    json_repair,
    knowledge,
    mermaid,
    outline,
    scheme_extraction,  # 标书智能体（一）引入：五段式专项方案解析提取提示词
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
    warm_prompt_cache,
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
    PART_ALIASES,
    assemble_prompt_context,
    build_chapter_context,
    estimate_tokens,
    normalize_part_name,
    truncate_to_tokens,
    split_into_token_capped_chunks,
)
from ._registry import (
    _ALL_PROMPTS,
    _reg,
    audit_shared_usage,
    clean_prompt_text,
    extract_variables,
    get_prompt_variables,
    has_residual_placeholders,
    render_prompt,
    render_prompt_safe,
    resolve_shared_refs,
    validate_prompt_content,
    validate_prompt_variables,
)
from ._security import (
    desensitize_sensitive_info,
    detect_injection_risk,
    neutralize_meta_markers,
    sanitize_for_injection,
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


# R1（2026-11-04，参考规范标注）：三条共享规则当前无任何模板以 {shared:NAME}
# 引用（audit_shared_usage adoption_rate=0）。经审查**不应强行收编**——大纲/
# 图表模板内的标题质量规则是「指令语气」写作要求（如"编号由系统按树位置派生，
# 只填语义本体"），与本库的「规范表语气」（逐级编号模板描述）语义角色不同，
# 文本替换会改变提示词行为。故保留为管理页可查看/可引用的参考资产，标签
# 显式标注身份，避免运维误解为"已在生效的注入配置"。
_reg("SHARED_HEADING_RULES", "共享规则", "标题编号规范（V3.1 七级，参考规范·暂无模板引用）", _shared.SHARED_HEADING_RULES)
_reg("SHARED_FORBIDDEN_WORDS", "共享规则", "标题禁用词列表（参考规范·暂无模板引用）", _shared.SHARED_FORBIDDEN_WORDS)
_reg("SHARED_OUTPUT_SPEC", "共享规则", "输出格式规范（参考规范·暂无模板引用）", _shared.SHARED_OUTPUT_SPEC)

__all__ = [
    "get_prompt",
    "get_prompt_with_validation",
    "reload_prompt_cache",
    "warm_prompt_cache",
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
    "split_into_token_capped_chunks",
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
    # ENHANCE 2026-10-17
    "resolve_shared_refs",
    "audit_shared_usage",
    "validate_prompt_content",
    "detect_injection_risk",
    "sanitize_for_injection",
    "normalize_part_name",
    "PART_ALIASES",
]
