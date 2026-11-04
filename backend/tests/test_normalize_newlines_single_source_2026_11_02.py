"""LEFTOVER-3 收敛 2026-11-02：normalize_newlines 单一权威回归测试。

背景：normalize_newlines 曾有三份定义（app.utils 权威 / text_patch 局部副本 /
workflows_sections ImportError 兜底），存在口径漂移风险。本轮收敛为：
  - app.utils.normalize_newlines = 唯一权威实现（含 Any 容错语义）；
  - text_patch 从权威模块引用（本测试断言同一对象）；
  - workflows_sections 兜底仅在 ImportError 路径生效，本测试用 AST 提取
    兜底函数体逐样本比对权威实现，任何一侧改动不同步即红。
"""

import ast
import pathlib

from app.services import text_patch
from app.utils import normalize_newlines as authoritative


SAMPLES = [
    "",
    None,
    "plain text\nno cr",
    "a\r\nb\r\nc",
    "a\rb\rc",
    "mixed\r\nonly\rone",
    "\r\n",
    "\r",
    0,          # falsy 非 str → ""
    5,          # truthy 非 str → "5"
    "tail\r",   # 行尾 CR（围栏锚点事故形态）
]


def _extract_fallback_func() -> callable:
    """从 workflows_sections.py 源码 AST 提取 ImportError 兜底版 normalize_newlines。"""
    src = (
        pathlib.Path(__file__).resolve().parents[1]
        / "app" / "services" / "ai" / "workflows_sections.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        for handler in node.handlers:
            for sub in ast.walk(handler):
                if (
                    isinstance(sub, ast.FunctionDef)
                    and sub.name == "normalize_newlines"
                ):
                    module = ast.Module(body=[sub], type_ignores=[])
                    ns: dict = {}
                    exec(compile(module, "<fallback>", "exec"), ns)
                    return ns["normalize_newlines"]
    raise AssertionError("未找到 workflows_sections 的 normalize_newlines 兜底定义")


class TestNormalizeNewlinesSingleSource:
    def test_utils_authoritative_semantics(self):
        """权威实现：CRLF/CR→LF、falsy→""、非 str 真值按 str() 归一。"""
        assert authoritative("") == ""
        assert authoritative(None) == ""
        assert authoritative("a\r\nb") == "a\nb"
        assert authoritative("a\rb") == "a\nb"
        assert authoritative(5) == "5"
        assert authoritative("plain\n") == "plain\n"

    def test_text_patch_reexports_authoritative_object(self):
        """text_patch 不再持有副本，必须是权威模块同一对象。"""
        assert text_patch.normalize_newlines is authoritative

    def test_workflows_sections_fallback_matches_authoritative(self):
        """ImportError 兜底与权威实现逐样本等价（口径漂移守卫）。"""
        fallback = _extract_fallback_func()
        for s in SAMPLES:
            assert fallback(s) == authoritative(s), (
                f"兜底实现与权威漂移: input={s!r} "
                f"fallback={fallback(s)!r} authoritative={authoritative(s)!r}"
            )
