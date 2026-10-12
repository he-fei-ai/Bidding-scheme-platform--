"""Chart rendering services.

This package hosts chart renderers that produce PNG images from structured data.

Currently exposed renderers:
    - single_chain_flow_renderer: vertical / horizontal single-chain flowcharts
      following the unified visual spec (light purple bg + purple border +
      dark gray text + dark gray arrows).
"""

from .single_chain_flow_renderer import (
    auto_select_direction,
    render_single_chain_flow_sync,
    render_single_chain_flow_to_png,
)


__all__ = [
    "auto_select_direction",
    "render_single_chain_flow_sync",
    "render_single_chain_flow_to_png",
]
