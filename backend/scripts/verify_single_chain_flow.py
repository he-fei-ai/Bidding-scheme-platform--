"""Verify single-chain flowchart renderer against the reference spec."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.charts import (  # noqa: E402
    render_single_chain_flow_sync,
    auto_select_direction,
)

OUT_DIR = Path(r"J:\编程\招投标方案平台\data\_exports\charts")
OUT_DIR.mkdir(parents=True, exist_ok=True)

vertical_steps = [
    "拆除技术交底与保护标记",
    "吊顶拆除",
    "隔墙拆除",
    "墙面面层铲除",
    "地面面层拆除",
    "垃圾分类与密闭收集",
    "临时贮存与封闭锁闭",
    "装车与洒水降尘",
    "车辆冲洗",
    "密闭运输至消纳点",
    "台账记录与闭环管理",
]

horizontal_steps = [
    "基层处理",
    "防水施工",
    "蓄水试验",
    "保护层施工",
    "验收交付",
]

# Sanity: auto-direction
assert auto_select_direction(11) == "vertical"
assert auto_select_direction(5) == "horizontal"

# Render both directions
v = render_single_chain_flow_sync(
    data={"title": "拆除工艺流程", "steps": vertical_steps},
    output_path=OUT_DIR / "vertical_demo.png",
    direction="vertical",
)
h = render_single_chain_flow_sync(
    data={"title": "防水施工流程", "steps": horizontal_steps},
    output_path=OUT_DIR / "horizontal_demo.png",
    direction="horizontal",
)

from PIL import Image  # noqa: E402

for p in (v, h):
    im = Image.open(p)
    print(f"{p.name}: exists={p.exists()} size={im.size} bytes={p.stat().st_size}")

print("OK")
