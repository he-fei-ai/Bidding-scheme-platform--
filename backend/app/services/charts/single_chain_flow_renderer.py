"""Single-chain flowchart renderer (vertical / horizontal dual mode).

Implements the unified visual spec:
    - node background:      #f0edff (light purple)
    - node border:          #a89aff (purple)
    - node text:            #333333 (dark gray)
    - arrow line / head:    #333333 (dark gray)

Rendering is done via Playwright (Chromium) by generating a small self-contained
HTML page and screenshotting the flow container.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from string import Template
from typing import Any


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Visual constants (single source of truth for the unified style spec)
# ---------------------------------------------------------------------------
NODE_BG = "#f0edff"
NODE_BORDER = "#a89aff"
NODE_TEXT = "#333333"
ARROW_COLOR = "#333333"

DEFAULT_NODE_WIDTH_VERTICAL = 280
DEFAULT_NODE_WIDTH_HORIZONTAL = 200
DEFAULT_NODE_HEIGHT = 80
DEFAULT_GAP = 40


def auto_select_direction(step_count: int) -> str:
    """Pick vertical/horizontal direction based on node count.

    - step_count <= 8 -> horizontal (avoid too-tall vertical layout)
    - step_count >  8 -> vertical   (long flow, avoid too-wide layout)
    """
    if step_count < 0:
        raise ValueError(f"step_count must be >= 0, got {step_count}")
    return "horizontal" if step_count <= 8 else "vertical"


def _html_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


# ---------------------------------------------------------------------------
# HTML template (uses string.Template to avoid CSS/JS brace escaping)
# ---------------------------------------------------------------------------
_HTML_TEMPLATE = Template(
    """\
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>$title</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  html, body { background: #ffffff; }
  body {
    font-family: "Microsoft YaHei", "PingFang SC", Arial, sans-serif;
    color: $node_text;
    padding: 20px;
  }
  .flow {
    display: inline-flex;
    flex-direction: $direction;
    align-items: center;
    padding: 20px;
  }
  .node {
    box-sizing: border-box;
    width: $node_width_px;
    min-height: $node_height_px;
    background: $node_bg;
    border: 1px solid $node_border;
    border-radius: 0;
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 16px 24px;
    font-size: $node_font_size;
    color: $node_text;
    letter-spacing: 0.5px;
    text-align: center;
    line-height: 1.4;
    white-space: pre-wrap;
    word-break: break-word;
    flex-shrink: 0;
    transition: box-shadow 0.2s;
  }
  .arrow {
    display: flex;
    align-items: center;
    justify-content: center;
    flex-shrink: 0;
    $arrow_container_style
  }
  .arrow svg { overflow: visible; }
  .arrow line {
    stroke: $arrow_color;
    stroke-width: 1.5;
    stroke-linecap: round;
  }
  .arrow polygon { fill: $arrow_color; }
</style>
</head>
<body>
<div class="flow" id="flow"></div>
<script>
const DATA = $data_json;
const DIRECTION = "$direction";
const GAP = $gap;

function renderArrow() {
  if (DIRECTION === "column") {
    return '<svg xmlns="http://www.w3.org/2000/svg">' +
           '<line x1="0" y1="0" x2="0" y2="' + (GAP - 10) + '" />' +
           '<polygon points="0,' + GAP + ' -5,' + (GAP - 10) + ' 5,' + (GAP - 10) + '" />' +
           '</svg>';
  }
  return '<svg xmlns="http://www.w3.org/2000/svg">' +
         '<line x1="0" y1="0" x2="' + (GAP - 10) + '" y2="0" />' +
         '<polygon points="' + GAP + ',0 ' + (GAP - 10) + ',-5 ' + (GAP - 10) + ',5" />' +
         '</svg>';
}

function render() {
  const container = document.getElementById("flow");
  container.innerHTML = "";
  DATA.steps.forEach((text, i) => {
    const node = document.createElement("div");
    node.className = "node";
    node.textContent = text;
    container.appendChild(node);

    if (i < DATA.steps.length - 1) {
      const arrow = document.createElement("div");
      arrow.className = "arrow";
      arrow.innerHTML = renderArrow();
      container.appendChild(arrow);
    }
  });
}

render();
</script>
</body>
</html>
"""
)


# ---------------------------------------------------------------------------
# Public render API (async + sync wrapper)
# ---------------------------------------------------------------------------
async def render_single_chain_flow_to_png(
    data: dict[str, Any],
    output_path: str | Path,
    direction: str = "vertical",  # "vertical" | "horizontal"
    node_width: int | None = None,
    node_height: int = DEFAULT_NODE_HEIGHT,
    gap: int = DEFAULT_GAP,
    node_font_size: int = 15,
    scale: float = 2.0,
) -> Path:
    """Render a single-chain flowchart to a PNG file.

    Args:
        data: Dict with at least a ``steps`` list of strings. May contain a
              ``title`` field (used only as the HTML <title>).
        output_path: Output PNG path. Parent directories are created.
        direction: "vertical" (top-to-bottom) or "horizontal" (left-to-right).
        node_width: Node width in px. Defaults to 280 (vertical) or
                    200 (horizontal).
        node_height: Minimum node height in px.
        gap: Space between nodes (also used as arrow length).
        node_font_size: Font size in px.
        scale: Browser device scale factor (higher = sharper PNG).

    Returns:
        The output ``Path``.
    """
    from playwright.async_api import async_playwright

    if direction not in ("vertical", "horizontal"):
        raise ValueError(f"direction must be 'vertical' or 'horizontal', got {direction!r}")
    if not isinstance(data, dict) or not data.get("steps"):
        raise ValueError("data.steps must be a non-empty list of strings")

    steps: list[str] = [str(s) for s in data["steps"]]
    title = str(data.get("title") or "SingleChainFlow")

    is_vertical = direction == "vertical"
    if node_width is None:
        node_width = DEFAULT_NODE_WIDTH_VERTICAL if is_vertical else DEFAULT_NODE_WIDTH_HORIZONTAL

    css_direction = "column" if is_vertical else "row"
    if is_vertical:
        arrow_container_style = f"width: 100%; height: {gap}px;"
    else:
        arrow_container_style = f"width: {gap}px; height: 100%;"

    html = _HTML_TEMPLATE.safe_substitute(
        title=_html_escape(title),
        direction=css_direction,
        node_width_px=f"{node_width}px",
        node_height_px=f"{node_height}px",
        node_font_size=f"{node_font_size}px",
        gap=gap,
        arrow_container_style=arrow_container_style,
        node_bg=NODE_BG,
        node_border=NODE_BORDER,
        node_text=NODE_TEXT,
        arrow_color=ARROW_COLOR,
        data_json=json.dumps({"title": title, "steps": steps}, ensure_ascii=False),
    )

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            try:
                page = await browser.new_page(device_scale_factor=scale)
                await page.set_content(html, wait_until="domcontentloaded")
                await page.wait_for_selector(".node", timeout=10_000)

                el = await page.query_selector(".flow")
                if el is None:
                    raise RuntimeError("Flow container '.flow' not found after render")

                box = await el.bounding_box()
                if not box:
                    raise RuntimeError("Bounding box for '.flow' is empty")

                await page.set_viewport_size(
                    {
                        "width": max(1, int(box["width"]) + 40),
                        "height": max(1, int(box["height"]) + 40),
                    }
                )
                await el.screenshot(path=str(output_path), omit_background=False)
            finally:
                await browser.close()
    except ImportError as exc:
        raise RuntimeError(
            "Playwright is not installed. Run `pip install playwright` and "
            "`playwright install chromium`."
        ) from exc

    logger.info(
        "Rendered single-chain flowchart: %d steps, direction=%s -> %s",
        len(steps),
        direction,
        output_path,
    )
    return output_path


def render_single_chain_flow_sync(
    data: dict[str, Any],
    output_path: str | Path,
    **kwargs: Any,
) -> Path:
    """Synchronous wrapper around :func:`render_single_chain_flow_to_png`."""
    return asyncio.run(render_single_chain_flow_to_png(data, output_path, **kwargs))
