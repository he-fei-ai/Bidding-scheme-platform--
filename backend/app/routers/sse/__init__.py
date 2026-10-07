"""sse 子包 — sse_handlers 拆分模块"""

from .sse_shared import SSE_TOTAL_TIMEOUT_DEFAULT
from .sse_shared import logger
from .sse_shared import _background_tasks
from .sse_shared import _NotifyQueue
from .sse_utils import _parse_global_facts_items
from .sse_utils import _get_path_and_ancestors
from .sse_utils import _get_effective_target_words
from .sse_utils import _resolve_chart_field
from .sse_utils import _try_get_provider
from .sse_utils import _try_get_provider_with_retry
from .sse_utils import _fetch_project_data_async
from .sse_utils import _build_leaf_dicts
from .sse_db_ops import _save_section_to_db
from .sse_db_ops import _mark_sections_generating
from .sse_db_ops import _rollback_dirty_sections
from .sse_db_ops import _extract_project_context
from .sse_db_ops import _extract_project_context_with_retry
from .sse_db_ops import _count_outline_nodes
from .sse_db_ops import _clear_and_save_outline
from .sse_db_ops import _save_and_finalize_outline
from .sse_charts import _auto_generate_charts_for_section
from .sse_outline import _outline_main_loop
from .sse_outline import generate_outline_sse_impl
from .sse_content import generate_content_impl
from .sse_content import expand_content_impl

__all__ = [
    "SSE_TOTAL_TIMEOUT_DEFAULT",
    "_NotifyQueue",
    "_auto_generate_charts_for_section",
    "_background_tasks",
    "_build_leaf_dicts",
    "_clear_and_save_outline",
    "_count_outline_nodes",
    "_extract_project_context",
    "_extract_project_context_with_retry",
    "_fetch_project_data_async",
    "_get_effective_target_words",
    "_get_path_and_ancestors",
    "_mark_sections_generating",
    "_outline_main_loop",
    "_parse_global_facts_items",
    "_resolve_chart_field",
    "_rollback_dirty_sections",
    "_save_and_finalize_outline",
    "_save_section_to_db",
    "_try_get_provider",
    "_try_get_provider_with_retry",
    "expand_content_impl",
    "generate_content_impl",
    "generate_outline_sse_impl",
    "logger",
]