"""BUG-FIX 2026-10-28 回归测试：

- BUG-1（P1）：/sections/generate-content 与 /sections/expand-content 的
  writing_length 白名单用旧 key（short/extra_long），前端档位 key 为
  brief/standard/detailed/long → 选「简短/超长」被 422 拒绝。
  修复后白名单 = 权威 key（brief/standard/detailed/long），旧别名仍可经
  normalize_writing_length 归一化（向后兼容）。
- BUG-2（P2）：sse_utils 正文生成取数 SQL 漏选 word_count_tier 列 →
  单节档位覆盖在生成侧被静默丢弃（展示与生成预算分叉）。
  修复后 SELECT 含该列，缓存命中路径对缺列行做 None 补齐防御。
"""
import inspect

from app.routers import sections as sections_router


def test_generate_content_writing_length_validates_authoritative_keys():
    """路由 valid_lengths 必须接受权威 key brief/standard/detailed/long。"""
    src = inspect.getsource(sections_router.generate_outline_sse) if hasattr(
        sections_router, "generate_outline_sse"
    ) else ""
    # 生成正文路由使用 normalize_writing_length 归一化 + 权威白名单
    body = inspect.getsource(
        sections_router.generate_content
    ) if hasattr(sections_router, "generate_content") else ""
    assert "normalize_writing_length" in body, "generate-content 应归一化 writing_length"
    assert "('brief', 'standard', 'detailed', 'long')" in body, (
        "generate-content 白名单应为权威 key"
    )


def test_expand_content_writing_length_uses_authoritative_keys():
    """expand-content 路由白名单同样收敛到权威 key。"""
    body = inspect.getsource(sections_router.expand_content)
    assert "('brief', 'standard', 'detailed', 'long')" in body, (
        "expand-content 白名单应为权威 key"
    )
    assert "normalize_writing_length" in body


def test_legacy_keys_still_accepted_by_normalizer():
    """旧别名 short/extra_long 经 normalize_writing_length 归一化不 422。"""
    from app.services.ai.writing_length_config import normalize_writing_length

    assert normalize_writing_length("short") == "brief"
    assert normalize_writing_length("extra_long") == "long"
    assert normalize_writing_length("brief") == "brief"
    assert normalize_writing_length("long") == "long"
    assert normalize_writing_length("standard") == "standard"


def test_build_leaf_dicts_sql_selects_word_count_tier():
    """正文生成取数 SQL 必须选 word_count_tier 列（BUG-2 断点修复）。"""
    from app.routers.sse import sse_utils

    src = inspect.getsource(sse_utils._load_sections_data) if hasattr(
        sse_utils, "_load_sections_data"
    ) else ""
    if not src:
        # 回退：全模块源码中搜索该 SELECT
        import app.routers.sse.sse_utils as _m

        mod_src = inspect.getsource(_m)
        assert "word_count_tier" in mod_src, "sse_utils 应包含 word_count_tier 列"
    else:
        assert "word_count_tier" in src, "取数 SQL 应包含 word_count_tier 列"


def test_cache_hit_normalizes_word_count_tier_missing():
    """缓存命中路径对缺 word_count_tier 列的行做 None 补齐（旧缓存防御）。"""
    from app.routers.sse import sse_utils

    mod_src = inspect.getsource(sse_utils)
    # 缓存路径含列补齐防御代码
    assert 'if "word_count_tier" not in _r' in mod_src
