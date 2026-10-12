from pathlib import Path
from app.services.ai import workflows_outline_v51 as v51
src = Path(v51.__file__).read_text(encoding="utf-8")
# _block() 的定位方式
start = src.index("L3 标题质量收口")
end = src.index("# ===== AI 自检 =====")
block = src[start:end]
print("anchor start at docstring of helper?", "_apply_l3_quality_closeout" in src[:start])
for needle in ["validate_l3_specificity(level1)", "repair_l3_generic_titles(", "OUTLINE_L3_QUALITY_ENHANCE_ENABLED", "elif not _passed:", "except Exception"]:
    print(needle, "->", needle in block)
