"""2026-10-19 生成大纲模块第七轮 BUG 修复回归。

对应「报告_生成大纲模块第七轮BUG修复_2026-10-19」§5 修复项：

  BUG-OUTLINE-07a（P1）：`_fill_level2_with_ai` 在 AI 返回非规范键
       （如 "subsections"）时无法识别 → 走 3 次重试仍失败，整个 L2 填充崩溃。
       修复：抽出模块级常量 `_OUTLINE_LIST_KEYS` 收敛 8 个键别名。

  BUG-OUTLINE-07b（P2）：`_unwrap_outline_payload` 同样问题（直出路径），
       导致直出模式静默降级为分步、无任何告警。
       修复：同一常量收敛，两个入口共用同一事实源。

  BUG-OUTLINE-07c（P3）：`v51-confirm` 端点在 `current_phase='level3'` 时
       落入 else 返回 `next_phase='unknown'`，前端无法推进 UI。
       修复：新增 level3 → final 分支。

  BUG-OUTLINE-07d（P2）：`/review-with-autofix` 与 `/expand-content`
       两个路由缺失 `task_guard` 前置守卫；目录生成进行中执行会触发
       长写事务 `_clear_and_save_outline` 或 SSE 内的 `content-generation`
       任务组锁竞争。前者导致两份 DELETE+INSERT 互相覆盖（后写者胜），
       后者前端只收到 SSE error 事件、体验不佳。
       修复：两个路由前置 `_reject_if_generation_running(project_id)`。

运行：cd backend && python -m pytest tests/test_outline_bugfix_2026_10_19.py -q
"""

from __future__ import annotations

from pathlib import Path
import re

from app.services.ai import workflows_outline_v51 as wf


BACKEND_DIR = Path(__file__).resolve().parent.parent


def _read_module_source(mod_path: str) -> str:
    """按相对路径读取源码（零元数据依赖，避免 inspect.getsource 定位漂移）。"""
    return (BACKEND_DIR / mod_path).read_text(encoding="utf-8")


def _func_src(func_src: str, func_name: str) -> str:
    """从模块源码里按 def/async def + 缩进边界截取函数源码。

    与 2026-10-17 遗留项 L-2 修复同款工具：不使用 `inspect.getsource`
    避免 `__code__.co_firstlineno` 元数据在全量套件跑完后漂移。
    支持 async def 与 def 两种形式；def 头可跨行（多参数场景）。
    """
    # 找到 def 关键字的位置（跳过行首换行）
    pat = re.compile(
        rf"(?:^|\n)(?:async\s+)?def\s+{re.escape(func_name)}\s*\(",
        re.MULTILINE,
    )
    m = pat.search(func_src)
    if not m:
        return ""
    # def 行起点
    start = m.start()
    if start > 0 and func_src[start - 1] == "\n":
        start += 1  # 跳过换行
    # 找 def 行的缩进
    header = func_src[start:func_src.find("\n", start)]
    indent = len(header) - len(header.lstrip(" "))
    # 扫描函数签名直到 ":" 结尾（可跨行，需处理括号）
    sig_pos = m.end()
    paren_depth = 1  # 已经跨过 def 后的第一个 (
    while sig_pos < len(func_src) and paren_depth > 0:
        ch = func_src[sig_pos]
        if ch == "(":
            paren_depth += 1
        elif ch == ")":
            paren_depth -= 1
        sig_pos += 1
    # 括号闭合后再找行首 ":"（同一段签名）
    while sig_pos < len(func_src) and func_src[sig_pos] != ":":
        sig_pos += 1
    if sig_pos >= len(func_src):
        return ""
    # 跳到 ":" 后换行
    body_start = sig_pos + 1
    if body_start < len(func_src) and func_src[body_start] == "\n":
        body_start += 1
    # 从函数体开始向后扫描，遇到缩进<=def 缩进 且非空的行则结束
    pos = body_start
    end = pos
    while pos < len(func_src):
        nl = func_src.find("\n", pos)
        if nl < 0:
            end = len(func_src)
            break
        line = func_src[pos:nl]
        if line.strip():
            cur_indent = len(line) - len(line.lstrip(" "))
            if cur_indent <= indent:
                end = pos
                break
        pos = nl + 1
        end = pos
    return func_src[start:end]


# ===========================================================================
# BUG-07a/07b：outline 响应键别名收敛到单一事实源
# ===========================================================================


class TestOutlineKeyAliases:
    def test_module_constant_exists(self):
        assert hasattr(wf, "_OUTLINE_LIST_KEYS"), (
            "workflows_outline_v51 应有模块级常量 _OUTLINE_LIST_KEYS，"
            "作为两个入口（_unwrap_outline_payload / _fill_level2_with_ai）的共享事实源"
        )
        keys = wf._OUTLINE_LIST_KEYS
        assert isinstance(keys, tuple)
        # 核心 4 键必须存在（历史兼容）
        for k in ("outline", "children", "data", "items"):
            assert k in keys
        # 本轮扩展的 4 个新键（GPT/Claude 长 JSON 常见别名）
        for k in ("subsections", "sections", "chapters", "nodes"):
            assert k in keys, f"_OUTLINE_LIST_KEYS 应包含常见别名 {k}"

    def test_unwrap_handles_all_key_aliases(self):
        """标准键 + 新支持键 + 无 key + 非 list 情况全覆盖。"""
        for k in ("outline", "children", "data", "items",
                  "subsections", "sections", "chapters", "nodes"):
            assert wf._unwrap_outline_payload({k: [1, 2]}) == [1, 2], f"键 {k} 应被识别"

    def test_unwrap_direct_list(self):
        assert wf._unwrap_outline_payload([1, 2]) == [1, 2]

    def test_unwrap_no_valid_key(self):
        # 未识别：无有效键
        assert wf._unwrap_outline_payload({}) is None
        # 非 list 值
        assert wf._unwrap_outline_payload({"unknown_key": "not a list"}) is None
        assert wf._unwrap_outline_payload({"outline": "not a list"}) is None
        # 非 dict 非 list 输入
        assert wf._unwrap_outline_payload("string") is None
        assert wf._unwrap_outline_payload(42) is None
        assert wf._unwrap_outline_payload(None) is None

    def test_fill_level2_uses_shared_alias_table(self):
        """L2 填充也应通过共享键别名表识别非规范 AI 输出。"""
        src = _read_module_source("app/services/ai/workflows_outline_v51.py")
        body = _func_src(src, "_fill_level2_with_ai")
        assert body, "应能定位 _fill_level2_with_ai 函数"
        # 修复后必须引用 _OUTLINE_LIST_KEYS
        assert "_OUTLINE_LIST_KEYS" in body, (
            "_fill_level2_with_ai 应使用 _OUTLINE_LIST_KEYS 常量，"
            "而非硬编码 (\"children\", \"items\", \"chapters\", \"outline\")"
        )
        # 旧硬编码 4 键已收敛
        assert not re.search(
            r'for\s+key\s+in\s+\(\s*["\']children["\']\s*,\s*["\']items["\']\s*,'
            r"\s*[\"']chapters[\"']\s*,\s*[\"']outline[\"']",
            body,
        ), "旧 4 键硬编码应已移除"


# ===========================================================================
# BUG-07c：v51-confirm 在 level3 → next_phase='final'
# ===========================================================================


class TestV51ConfirmNextPhase:
    def _route_body(self) -> str:
        src = _read_module_source("app/routers/sections.py")
        return _func_src(src, "v51_confirm_route")

    def test_all_four_phase_transitions_present(self):
        """验证四条相位转移全部存在（level1→2, level2→3, level3→final, final→done）。"""
        body = self._route_body()
        assert body, "应能定位 v51_confirm_route"
        expected = [
            ("level1", "level2"),
            ("level2", "level3"),
            ("level3", "final"),
            ("final", "done"),
        ]
        for cur, nxt in expected:
            pattern = re.compile(
                rf"state\.current_phase\s*==\s*['\"]{re.escape(cur)}['\"]"
                rf"\s*:\s*\n\s+next_phase\s*=\s*['\"]{re.escape(nxt)}['\"]",
            )
            assert pattern.search(body), (
                f"应存在 {cur} → {nxt} 分支（修复前 level3 落 else 返回 'unknown'）"
            )

    def test_no_unknown_leak_for_known_phases(self):
        """四条已知相位都不能落入 next_phase='unknown' 兜底。"""
        body = self._route_body()
        # 找到 next_phase = 'unknown' 所在行
        assert "next_phase = 'unknown'" in body, "保留 unknown 兜底"
        # 检查 unknown 之前是否有 else 分支（合法位置）
        unknown_pos = body.rfind("next_phase = 'unknown'")
        # 前 200 字符内应有 "else:" 关键字（合法 else 兜底）
        preceding = body[max(0, unknown_pos - 200):unknown_pos]
        assert "else:" in preceding, "next_phase='unknown' 应仅在 else 分支"


# ===========================================================================
# BUG-07d：review-with-autofix 与 expand-content 缺少 task_guard
# ===========================================================================


class TestGenerationGuardOnContentRoutes:
    def test_review_with_autofix_has_guard(self):
        src = _read_module_source("app/routers/sections.py")
        body = _func_src(src, "review_outline_with_autofix_route")
        assert body, "应能定位 review_outline_with_autofix_route"
        # 必须调用 _reject_if_generation_running(project_id)
        assert "_reject_if_generation_running(project_id)" in body, (
            "review-with-autofix 前置应调用 _reject_if_generation_running(project_id) "
            "阻止与目录生成并发（否则 _clear_and_save_outline 会与目录生成的"
            " DELETE+INSERT 长事务互相覆盖）"
        )
        # 守卫应在 AI 调用（review_outline_with_autofix）之前
        guard_pos = body.find("_reject_if_generation_running(project_id)")
        ai_pos = body.find("review_outline_with_autofix(provider")
        assert guard_pos < ai_pos, "task_guard 应在 AI 调用之前"

    def test_expand_content_has_guard(self):
        src = _read_module_source("app/routers/sections.py")
        body = _func_src(src, "expand_content")
        assert body, "应能定位 expand_content 路由"
        assert "_reject_if_generation_running(project_id)" in body, (
            "expand-content 前置应调用 _reject_if_generation_running(project_id) "
            "让目录生成进行中直接 409，而非 SSE 内 TaskLockConflict 事件"
        )
        guard_pos = body.find("_reject_if_generation_running(project_id)")
        impl_pos = body.find("expand_content_impl(")
        assert guard_pos < impl_pos, "task_guard 应在 expand_content_impl 调用之前"

    def test_expand_content_guard_not_allowed_content_edit(self):
        """扩写不是内容类编辑（会抢任务组锁），必须是**严格** 409，不带 allow_content_edit=True。"""
        src = _read_module_source("app/routers/sections.py")
        body = _func_src(src, "expand_content")
        assert body
        # 检查守卫调用不带 allow_content_edit=True
        guard_call_pattern = r"_reject_if_generation_running\(project_id\)"
        guard_call = re.search(guard_call_pattern, body)
        assert guard_call is not None
        # 检查该调用的下一行没有 allow_content_edit=True（严格模式）
        call_end = guard_call.end()
        next_chunk = body[call_end:call_end + 100]
        assert "allow_content_edit=True" not in next_chunk, (
            "expand-content 抢 content-generation 任务组锁，"
            "不应带 allow_content_edit=True（目录生成中一律 409）"
        )
