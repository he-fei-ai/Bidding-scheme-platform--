"""AI 服务模块（V6.0 商业级）

包含：
  - 标题编号规范（heading_standard.py）
  - 智能排序优化器（outline_sort_optimizer.py）
  - Mermaid 图表渲染引擎（mermaid_renderer.py）
  - 图像引擎（image_engine.py）
  - 工作流引擎（workflows.py）
  - 提示词模板（prompts.py）
  - 行业知识库（industry_profiles.py）
  - AI 提供商工厂（provider_factory.py）
"""

# V3.1 新增：图表校验模块 - from parent package
from ..chart_validators import (
    SENTENCE_PATTERNS,
    TRIGGER_KEYWORDS,
    detect_architecture_subtype,
    detect_chart_type_from_text,
    detect_chart_with_metadata,
    extract_matched_keywords,
    trigger_match,
    validate_architecture_tree,
    validate_comparison_pie,
    validate_flowchart_structure,
    validate_gantt_plan,
    validate_labor_data,
    validate_layout_data,
)
from .heading_standard import (
    HEADING_STYLE_CONFIG,
    HeadingNumberingGenerator,
    HeadingNumberingGeneratorV2,
    build_heading_spec_prompt,
)
from .image_engine import (
    # V6.0 新增
    AIMageGenerator,
    InlineChartDetector,
    build_illustration_url,
    chart_data_fix,
    download_and_embed_image,
    generate_architecture_arrange,
    generate_architecture_tree,
    generate_comparison_arrange,
    generate_comparison_mermaid,
    generate_flowchart_arrange,
    generate_illustration_prompt,
    generate_labor_arrange,
    generate_labor_data,
    generate_layout_arrange,
    generate_layout_mermaid,
    generate_mermaid_flowchart,
    generate_mermaid_gantt,
    repair_mermaid,
    # 图表编排
    validate_mermaid,
)
from .mermaid_renderer import (
    ChartCache,
    _render_architecture_image_v2,
    _render_labor_image_v2,
    render_mermaid_to_bytes,
)
from .outline_sort_optimizer import (
    CORE_28_CHAPTERS,
    CORE_TITLE_SET,
    CORE_ZONE_MAP,
    ZONE_ORDER,
    OutlineSortOptimizer,
    OutlineSortOptimizerV2,
    extract_score_weights,
)
from .providers.anthropic_compatible import AnthropicCompatibleProvider
from .providers.base import AIMessage
from .providers.deepseek import DeepSeekProvider
from .providers.openai_compatible import OpenAICompatibleProvider


__all__ = [
    # heading_standard
    "HeadingNumberingGenerator",
    "HeadingNumberingGeneratorV2",
    "HEADING_STYLE_CONFIG",
    "build_heading_spec_prompt",
    # outline_sort_optimizer
    "OutlineSortOptimizer",
    "OutlineSortOptimizerV2",
    "CORE_28_CHAPTERS",
    "CORE_TITLE_SET",
    "CORE_ZONE_MAP",
    "ZONE_ORDER",
    "extract_score_weights",
    # mermaid_renderer
    "render_mermaid_to_bytes",
    "ChartCache",
    # image_engine
    "validate_mermaid",
    "repair_mermaid",
    "generate_flowchart_arrange",
    "chart_data_fix",
    "generate_layout_arrange",
    "generate_layout_mermaid",
    "generate_comparison_arrange",
    "generate_comparison_mermaid",
    "generate_mermaid_flowchart",
    "generate_mermaid_gantt",
    "generate_illustration_prompt",
    "generate_labor_arrange",
    "generate_labor_data",
    "generate_architecture_arrange",
    "generate_architecture_tree",
    "build_illustration_url",
    # V6.0 新增
    "AIMageGenerator",
    "download_and_embed_image",
    "InlineChartDetector",
    # providers
    "AIMessage",
    "AnthropicCompatibleProvider",
    "OpenAICompatibleProvider",
    "DeepSeekProvider",
    # V3.1 图表校验（新增）
    "validate_architecture_tree",
    "validate_gantt_plan",
    "validate_flowchart_structure",
    "validate_labor_data",
    "validate_comparison_pie",
    "validate_layout_data",
    "detect_architecture_subtype",
    "detect_chart_with_metadata",
    "extract_matched_keywords",
    "trigger_match",
    "detect_chart_type_from_text",
    "TRIGGER_KEYWORDS",
    "SENTENCE_PATTERNS",
]
