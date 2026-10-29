"""可观测性规范定义（ENHANCE 2026-10-29）。

集中定义结构化日志的：统一字段名、级别枚举、错误层级枚举、语义化事件名、
模块名、模块前缀错误码注册表、任务生命周期状态与合法迁移。

设计约束：本模块只依赖标准库，不得反向 import 业务模块，避免循环导入。
"""
from __future__ import annotations

from typing import Final

# ──────────────────────────────────────────────────────────────────
# 一、统一结构化日志字段（共 18 个核心字段，见需求「日志结构化」）
# ──────────────────────────────────────────────────────────────────
LOG_FIELDS: Final[tuple[str, ...]] = (
    "timestamp", "level", "module", "event",
    "trace_id", "span_id", "parent_span_id",
    "request_id", "task_id", "project_id", "chapter_id",
    "duration_ms", "status",
    "error_code", "error_layer", "retryable",
    "message", "detail",
)

# 级别枚举
LOG_LEVELS: Final[tuple[str, ...]] = ("DEBUG", "INFO", "WARN", "ERROR", "FATAL")

# 错误层级枚举（error_layer）
ERROR_LAYERS: Final[tuple[str, ...]] = (
    "api", "db", "llm", "tool", "validation", "io", "network", "unspecified",
)

# ──────────────────────────────────────────────────────────────────
# 二、模块名（module）
# ──────────────────────────────────────────────────────────────────
MODULES: Final[dict[str, str]] = {
    "parse": "上传解析", "extract": "要点提取", "outline": "目录生成",
    "facts": "全局事实", "content": "正文生成", "chart": "图表生成",
    "audit": "审核预检", "export": "导出DOCX", "prompt": "提示词",
    "model_config": "文本模型配置", "consistency": "一致性校核",
    "api": "接口", "db": "数据库", "task": "任务调度",
    "observability": "可观测性", "system": "系统",
}

# 需求指定的模块前缀（error_code 前缀）
MODULE_PREFIXES: Final[dict[str, str]] = {
    "parse": "PARSE", "extract": "EXTRACT", "outline": "OUTLINE",
    "facts": "FACTS", "content": "CONTENT", "chart": "CHART",
    "audit": "AUDIT", "export": "EXPORT", "prompt": "PROMPT",
    "model_config": "MODELCFG", "consistency": "CONSIST",
    "api": "API", "db": "DB", "task": "TASK",
    "observability": "OBS", "system": "SYS",
}


# ──────────────────────────────────────────────────────────────────
# 三、语义化事件名（event）
# 约定：<module>.<sub>.action，动作用 start/ok/fail/retry/degrade/correction/hit/miss 等。
# ──────────────────────────────────────────────────────────────────
EVENTS: Final[dict[str, str]] = {
    # 解析提取
    "parse.start": "解析开始", "parse.end": "解析结束", "parse.fail": "解析失败",
    "extract.start": "提取开始", "extract.end": "提取结束",
    "extract.field_completeness": "提取字段完整率",
    # 目录生成
    "outline.generate.start": "一级目录生成开始",
    "outline.generate.end": "目录生成结束",
    "outline.generate.retry": "目录生成重试",
    "outline.check_json": "目录 JSON 校验", "outline.fallback": "分步兜底",
    "outline.renumber": "重编号", "outline.audit_backfill": "审核回灌",
    "outline.ai_token": "目录 AI Token",
    # 全局事实
    "facts.extract.start": "事实提取开始", "facts.merge_dedup": "事实合并去重",
    "facts.user_edit": "事实用户编辑", "facts.conflict": "事实冲突",
    "facts.version_change": "事实版本变更",
    # 正文生成
    "content.generate.start": "正文生成开始", "content.generate.end": "正文生成结束",
    "content.generate.retry": "正文生成重试", "content.orchestrate": "正文编排",
    "content.concurrent": "并发生成", "content.word_count_check": "字数校验",
    "content.self_check": "正文自检", "content.repair.normal": "普通修复",
    "content.repair.agent": "Agent修复", "content.structure_check": "结构校验",
    "content.text_edit": "old_text/new_text 定位", "content.writeback": "回写小节",
    # 图表生成
    "chart.trigger": "图表触发", "chart.generate": "图表生成", "chart.render": "图表渲染",
    "chart.validate": "图表校验", "chart.repair": "图表修复", "chart.degrade": "图表降级",
    "chart.fig_no": "图号生成",
    # 审核预检
    "audit.rule_hit": "规则命中", "audit.precheck": "预检结果",
    "audit.issue_locate": "问题定位", "audit.repair_suggest": "修复建议",
    "audit.manual_recheck": "人工复核",
    # 导出 DOCX
    "export.docx.start": "导出DOCX开始", "export.docx.assemble": "组装",
    "export.docx.render": "渲染", "export.docx.cache_fingerprint": "缓存指纹",
    "export.docx.concurrent": "导出并发", "export.docx.readback": "读回校验",
    "export.docx.expand_check": "扩展检查", "export.docx.fail": "导出DOCX失败",
    # 提示词
    "prompt.template_call": "模板调用", "prompt.variable_inject": "变量注入",
    "prompt.version": "提示词版本", "prompt.cache_hit": "提示词缓存命中",
    # 文本模型配置
    "model_config.route": "模型路由", "model_config.param": "模型参数",
    "model_config.hot_reload": "热更新", "model_config.connectivity": "连通性",
    # 通用动作
    "tool.call": "工具调用", "db.write": "数据库写入", "file.op": "文件操作",
    "cache.hit": "缓存命中", "cache.miss": "缓存失效",
    "retry": "重试", "degrade": "降级",
    "correction.start": "修正开始", "correction.end": "修正结束",
    "state.change": "状态变化", "trace.break": "链路断链",
    "log.write_fail": "日志写入失败", "alert.fire": "告警触发",
}


# ──────────────────────────────────────────────────────────────────
# 四、模块前缀错误码注册表（error_code）
# 格式：<MODULE_PREFIX>_<三位序号>，集中注册以便查询/校验/测试断言。
# ──────────────────────────────────────────────────────────────────
ERROR_CODES: Final[dict[str, str]] = {
    # 解析（PARSE_0xx）
    "PARSE_001": "解析文件不存在", "PARSE_002": "解析超时",
    "PARSE_003": "解析文件损坏", "PARSE_004": "解析无可用文本",
    # 要点提取（EXTRACT_0xx）
    "EXTRACT_001": "字段提取缺失", "EXTRACT_002": "分类判定失败",
    "EXTRACT_003": "校验失败", "EXTRACT_004": "回填失败",
    # 目录生成（OUTLINE_0xx）
    "OUTLINE_001": "目录 AI 调用失败", "OUTLINE_002": "目录 JSON 解析失败",
    "OUTLINE_003": "目录结构校验失败", "OUTLINE_004": "目录重编号失败",
    # 全局事实（FACTS_0xx）
    "FACTS_001": "事实提取失败", "FACTS_002": "事实合并冲突",
    "FACTS_003": "事实版本变更冲突",
    # 正文生成（CONTENT_0xx）
    "CONTENT_001": "正文生成失败", "CONTENT_002": "正文字数校验失败",
    "CONTENT_003": "正文自检失败", "CONTENT_004": "正文修复失败",
    # 图表生成（CHART_0xx）
    "CHART_001": "图表生成失败", "CHART_002": "图表渲染失败",
    "CHART_003": "图表校验失败", "CHART_004": "图表修复失败",
    # 审核预检（AUDIT_0xx）
    "AUDIT_001": "规则命中异常", "AUDIT_002": "预检失败", "AUDIT_003": "问题定位失败",
    # 导出（EXPORT_0xx）
    "EXPORT_001": "导出组装失败", "EXPORT_002": "导出渲染失败",
    "EXPORT_003": "导出读回校验失败", "EXPORT_004": "导出缓存指纹冲突",
    # 提示词（PROMPT_0xx）
    "PROMPT_001": "提示词模板缺失", "PROMPT_002": "提示词变量注入失败",
    "PROMPT_003": "提示词缓存加载失败",
    # 文本模型配置（MODELCFG_0xx）
    "MODELCFG_001": "模型路由失败", "MODELCFG_002": "模型参数无效",
    "MODELCFG_003": "模型连通性失败",
    # 通用
    "API_001": "接口参数无效", "API_002": "接口鉴权失败",
    "DB_001": "数据库写入失败", "DB_002": "数据库锁定超时", "DB_003": "数据库连接耗尽",
    "LLM_001": "大模型不可用", "LLM_002": "大模型超时", "LLM_003": "大模型限流",
    "TOOL_001": "工具调用失败", "IO_001": "文件读写失败", "NETWORK_001": "网络异常",
    "TASK_001": "任务状态非法迁移",
    "OBS_001": "日志写入失败", "OBS_002": "日志存储降级",
}

# ──────────────────────────────────────────────────────────────────
# 五、任务生命周期状态与合法迁移
# ──────────────────────────────────────────────────────────────────
TRAJECTORY_STATES: Final[tuple[str, ...]] = (
    "created", "queued", "started", "running", "paused",
    "resumed", "stopping", "stopped", "completed", "failed",
)

# 合法状态迁移表（from -> allowed to）
TRAJECTORY_TRANSITIONS: Final[dict[str, tuple[str, ...]]] = {
    "created": ("queued", "started", "failed"),
    "queued": ("started", "running", "stopped", "failed"),
    "started": ("running", "paused", "completed", "failed"),
    "running": ("paused", "resumed", "stopping", "completed", "failed"),
    "paused": ("resumed", "running", "stopped", "failed"),
    "resumed": ("running", "paused", "stopping", "completed", "failed"),
    "stopping": ("stopped", "completed", "failed"),
    "stopped": ("completed", "failed"),
    "completed": (),
    "failed": (),
}

# 任务类型（task_type）
TASK_TYPES: Final[tuple[str, ...]] = (
    "parse", "extract", "outline", "facts", "content",
    "chart", "audit", "export", "rejection", "consistency",
)

# 可记录到「关键业务日志（写库）」的事件（高频 DEBUG 事件写文件即可）
DB_WRITE_EVENTS: Final[frozenset[str]] = frozenset({
    "parse.start", "parse.end", "parse.fail",
    "outline.generate.start", "outline.generate.end", "outline.generate.retry",
    "content.generate.start", "content.generate.end", "content.generate.retry",
    "content.repair.agent", "chart.generate", "chart.repair", "chart.degrade",
    "export.docx.start", "export.docx.fail",
    "tool.call", "db.write", "state.change", "trace.break",
    "alert.fire", "log.write_fail", "retry", "degrade",
})
