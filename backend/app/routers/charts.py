"""Chart rendering API routes.

Exposes endpoints for generating chart PNGs from structured data. Currently
supports the unified single-chain flowchart style (vertical / horizontal).

Endpoints:
    POST /api/v1/charts/single-chain-flow/render        Render a flowchart to PNG
    POST /api/v1/charts/single-chain-flow/auto-direction  Pick direction from count
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..services.charts import auto_select_direction, render_single_chain_flow_sync


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/charts", tags=["charts"])


# ---------------------------------------------------------------------------
# Defaults (mirrors the renderer's DEFAULT_* constants but exposed here so
# Pydantic Field defaults don't depend on import order).
# ---------------------------------------------------------------------------
DEFAULT_NODE_HEIGHT = 80
DEFAULT_GAP = 40
DEFAULT_FONT_SIZE = 15


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------
class SingleChainFlowRequest(BaseModel):
    """Request body for rendering a single-chain flowchart PNG."""

    scheme_id: str = Field(
        ..., min_length=1, description="Owning scheme id (used for output folder)"
    )
    section_id: str = Field(..., min_length=1, description="Owning section id (used for filename)")
    title: str | None = None
    steps: list[str] = Field(..., min_length=2, description="Ordered list of node labels")
    direction: str = Field(
        "vertical",
        description=(
            "Layout direction: 'vertical' (top-to-bottom) or 'horizontal' "
            "(left-to-right). Pass 'auto' to auto-pick based on step count."
        ),
    )
    node_width: int | None = Field(
        None, ge=60, le=1200, description="Node width in px. Defaults by direction."
    )
    node_height: int = Field(DEFAULT_NODE_HEIGHT, ge=40, le=400)
    gap: int = Field(DEFAULT_GAP, ge=10, le=120)
    node_font_size: int = Field(DEFAULT_FONT_SIZE, ge=10, le=32)
    scale: float = Field(2.0, ge=1.0, le=4.0)


class SingleChainFlowResponse(BaseModel):
    status: str = "ok"
    path: str
    direction: str
    step_count: int
    title: str | None = None


class AutoDirectionRequest(BaseModel):
    step_count: int = Field(..., ge=0)


class AutoDirectionResponse(BaseModel):
    step_count: int
    direction: str


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@router.post("/single-chain-flow/render", response_model=SingleChainFlowResponse)
def render_single_chain_flow(req: SingleChainFlowRequest) -> SingleChainFlowResponse:
    """Render a single-chain flowchart to PNG.

    Output file layout: data/_exports/charts/{scheme_id}/{section_id}_flow.png
    """
    if not req.steps:
        raise HTTPException(status_code=400, detail="steps must not be empty")

    direction = req.direction
    if direction == "auto":
        direction = auto_select_direction(len(req.steps))
    if direction not in ("vertical", "horizontal"):
        raise HTTPException(
            status_code=400,
            detail="direction must be 'vertical', 'horizontal', or 'auto'",
        )

    out_dir = Path("data/_exports/charts") / req.scheme_id
    out_path = out_dir / f"{req.section_id}_flow.png"

    try:
        render_single_chain_flow_sync(
            data={"title": req.title, "steps": req.steps},
            output_path=out_path,
            direction=direction,
            node_width=req.node_width,
            node_height=req.node_height,
            gap=req.gap,
            node_font_size=req.node_font_size,
            scale=req.scale,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid request: {exc}") from exc
    except Exception as exc:  # render layer may raise many types
        logger.exception("Single-chain flowchart render failed")
        raise HTTPException(status_code=500, detail=f"Render failed: {exc}") from exc

    return SingleChainFlowResponse(
        path=str(out_path),
        direction=direction,
        step_count=len(req.steps),
        title=req.title,
    )


@router.post("/single-chain-flow/auto-direction", response_model=AutoDirectionResponse)
def auto_direction(req: AutoDirectionRequest) -> AutoDirectionResponse:
    """Return the direction that should be used for a given step count."""
    try:
        direction = auto_select_direction(req.step_count)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return AutoDirectionResponse(step_count=req.step_count, direction=direction)
