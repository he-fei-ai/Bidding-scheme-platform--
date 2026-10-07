"""静态门禁：禁止在 `async def` 内直调阻塞调用（PERF 2026-10-06，遗留项 TD-1 根治）。

## 为什么需要门禁

本项目自 2026-09-18 起要求「重操作移出事件循环」，2026-10-05 起要求「只读端点
用 `readonly + asyncio.to_thread`」。但这套规则**只靠人工逐端点迁移**，已连续
两批出现漏改（2026-10-06 在审核与预检 / 导出文档两模块共发现 6 个端点遗漏）。
本门禁把规则变成 CI 可执行断言。

## 规则设计（刻意保守：零假阳性优先）

朴素做法「async def 里出现任何 `app.services.*` 调用就报错」会产生**大量假
阳性**——`parse_stored_items` / `get_ai_config` / `is_encrypted` / `mask_key_for_audit`
这类纯 CPU 函数是微秒级，包一层 `to_thread` 只会徒增线程调度开销。
因此本门禁只抓**两类确凿的阻塞源**：

**规则 A（精确、零假阳性）**：`get_db()` / `get_db_with_retry()` 直调。
两者是全局唯一写锁 `_db_write_lock` 的入口（8s 超时），一旦在事件循环里调用，
整个服务的并发请求都会被冻结。判定方式：出现在 `AsyncFunctionDef` **自身函数体**
（不下钻嵌套 `def`）中即违规——因为本项目的合规写法正是「把 DB 访问包进嵌套
同步闭包，再 `await asyncio.to_thread(闭包)`」。

**规则 B（策展登记表）**：`_HEAVY_REGISTRY` 中登记的**重**服务函数。
这些函数会全表扫描 / 逐行文件 I/O / 对每章正文做文本统计（例如
`sections.list_sections` 对**每个章节完整正文**跑 `_count_words`，大项目单次数十
MB 级扫描）。规则 B 是**人工策展的登记表**而非自动推导——自动推导需要做跨模块
的传递性 DB 调用分析，误判率反而高。登记表的维护要求：新增重服务函数时同步登记。

**规则 C**：`async def` 内定义的嵌套同步闭包，若在该 async def 内从未作为
`asyncio.to_thread(...)` 的实参出现，则它会同步执行 → 同样是阻塞点。

合规示例（本项目标准写法）：
```python
async def endpoint():
    def _read():                      # 嵌套同步闭包
        with get_db(readonly=True) as c: ...
    return await asyncio.to_thread(_read)
```
"""
from __future__ import annotations

import ast
import importlib
import inspect
from pathlib import Path

import pytest


BACKEND = Path(__file__).resolve().parent.parent
ROUTERS_DIR = BACKEND / "app" / "routers"

#: 会真正阻塞事件循环的 DB 入口（规则 A）
_DB_ENTRIES = {"get_db", "get_db_with_retry"}

#: 重服务函数登记表（规则 B）：全表扫描 / 批量文件 I/O / 逐章正文统计
_HEAVY_REGISTRY: set[tuple[str, str]] = {
    # 审核与预检
    ("app.services.sections", "list_sections"),      # 逐章正文 _count_words
    ("app.services.sections", "build_tree"),         # 目录树 + 60s 缓存
    ("app.services.duplicate", "list_tasks"),
    ("app.services.duplicate", "get_task"),           # 前端每 2s 轮询，写锁连接
    ("app.services.rejection", "get_task"),
    ("app.services.rejection", "list_tasks"),
    ("app.services.rejection", "get_tasks_by_project"),
    ("app.services.rejection", "get_all_report_schedules"),
    ("app.services.rejection", "get_report_schedules_by_task"),
    # 导出文档
    ("app.services.export", "list_exports"),         # 逐行 Path.resolve()
    ("app.services.export", "get_latest_export"),
    ("app.services.export", "get_latest_export_info"),
    ("app.services.export", "check_document"),
    ("app.services.export", "build_docx"),
}

_NESTED = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)


# --------------------------------------------------------------------------
# AST 辅助
# --------------------------------------------------------------------------
def _resolve_aliases(tree: ast.AST) -> dict[str, str]:
    """把 `from ..services.x import y` / `from ..services import x` 解析成 {本地名: 绝对模块}。"""
    out: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or not node.module:
            continue
        if node.level:
            base = "app" + "." * (node.level - 1) + node.module
        elif node.module.startswith("app."):
            base = node.module
        else:
            continue
        for alias in node.names:
            out[alias.asname or alias.name] = base
    return out


def _dotted(node: ast.AST) -> str | None:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def _own_body_calls(fn: ast.AsyncFunctionDef) -> list[ast.Call]:
    """函数体**直接**的调用；不下钻嵌套 def/async def/lambda/class。"""
    out: list[ast.Call] = []
    for stmt in fn.body:
        if isinstance(stmt, _NESTED):
            continue
        stack: list[ast.AST] = [stmt]
        while stack:
            node = stack.pop()
            for child in ast.iter_child_nodes(node):
                if isinstance(child, _NESTED):
                    continue
                if isinstance(child, ast.Call):
                    out.append(child)
                stack.append(child)
    return out


def _has_thread_dispatch(fn: ast.AsyncFunctionDef) -> bool:
    return any(
        isinstance(n, ast.Call) and (_dotted(n.func) or "").endswith("to_thread")
        for n in ast.walk(fn)
    )


def _resolve_target(call: ast.Call, aliases: dict[str, str]) -> tuple[str, str] | None:
    """返回 (绝对模块, 属性路径)；无法解析到 app.* 时返回 None。"""
    dotted = _dotted(call.func)
    if not dotted:
        return None
    head, _, rest = dotted.partition(".")
    if head in _DB_ENTRIES and not rest:
        return "app.db", head
    if head not in aliases:
        return None
    mod = aliases[head]
    # `from ..services import export as x` → x.build_docx ⇒ rest 已是属性路径
    # `from ..services.sections import list_sections` ⇒ 无 rest，用本地名本身
    return mod, rest or head


def _violations_in_file(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    aliases = _resolve_aliases(tree)
    found: list[str] = []
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.AsyncFunctionDef):
            continue
        dispatched = _has_thread_dispatch(fn)

        for call in _own_body_calls(fn):
            target = _resolve_target(call, aliases)
            if not target:
                continue
            mod, attr = target
            dotted = _dotted(call.func)
            if mod == "app.db" and attr in _DB_ENTRIES:
                found.append(
                    f"[A] {path.name}:{call.lineno} {fn.name}() 直调 {attr}()"
                    f"（写锁入口，必须包进 asyncio.to_thread）"
                )
                continue
            if (mod, attr) in _HEAVY_REGISTRY:
                found.append(
                    f"[B] {path.name}:{call.lineno} {fn.name}() 直调重服务 "
                    f"{mod}.{attr}()（全表/逐章扫描，必须包进 asyncio.to_thread）"
                )

        # 规则 C：未被 to_thread 调用的**同步**闭包
        # （只判 ast.FunctionDef；AsyncFunctionDef 是异步生成器/协程，
        #  用于 StreamingResponse 等场景，本身不阻塞循环）
        if not dispatched:
            for stmt in fn.body:
                if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                    nodes = [stmt]
                else:
                    nodes = [n for n in ast.walk(stmt) if n is not stmt and isinstance(n, ast.FunctionDef)]
                for node in nodes:
                    if isinstance(node, ast.FunctionDef):
                        found.append(
                            f"[C] {path.name}:{node.lineno} {fn.name}() 内的同步闭包 "
                            f"`{node.name}` 未见 asyncio.to_thread 调度"
                        )
    return found


def _router_files() -> list[Path]:
    return [p for p in sorted(ROUTERS_DIR.rglob("*.py")) if "__pycache__" not in str(p)]


# --------------------------------------------------------------------------
# 门禁断言
# --------------------------------------------------------------------------
#: 本次治理**范围内**的路由（审核与预检 + 导出文档 + 章节评审链）——零容忍。
_IN_SCOPE_ROUTERS = {
    "consistency.py",   # 全文一致性审计
    "rejection.py",     # 废标检查
    "duplicate.py",     # 标书查重
    "word_count.py",    # 字数统计预检
    "export.py",        # 导出文档
    "sections.py",      # 评审大纲（审核链）+ 章节树
}

#: 范围外**已登记存量**（AI 配置管理端点，非轮询热路径）。
#: 采用棘轮（ratchet）冻结：数量只许持平或下降，新增即失败。
#: 待办：把这 7 处按同口径收敛后，即可把本常量降为 0 并删除 ratchet 用例。
_RATCHET_BASELINE = 7
_RATCHET_BACKLOG_MODULE = "ai_config.py"


def _collect_violations() -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for path in _router_files():
        try:
            found = _violations_in_file(path)
        except SyntaxError as exc:  # pragma: no cover - 语法错误应由其它用例拦截
            pytest.fail(f"{path.name} 语法错误，无法静态分析: {exc}")
        if found:
            out[path.name] = found
    return out


def test_no_blocking_calls_in_scope_routers():
    """【零容忍·治理范围内】审核与预检 / 导出文档 / 评审链的路由。

    新增端点若违反规则 A/B/C，本用例立即失败 —— 这正是「人工迁移连续两批漏改」的根治点。
    """
    violations = _collect_violations()
    scoped = {f: v for f, v in violations.items() if f in _IN_SCOPE_ROUTERS}
    if scoped:
        detail = "\n".join(f"  - {x}" for items in scoped.values() for x in items)
        pytest.fail(
            f"治理范围内检测到 {sum(len(v) for v in scoped.values())} 处 async def 内的阻塞调用"
            f"（事件循环会被冻结 / 抢占全局写锁）：\n{detail}\n"
            f"修复方式：把阻塞调用包进嵌套同步闭包并 `await asyncio.to_thread(闭包)`，"
            f"纯读查询再加 readonly=True。"
        )


def test_blocking_calls_ratchet_not_increasing():
    """【棘轮·全项目】范围外存量阻塞调用只许持平或下降，禁止新增。

    治理范围外仍有登记存量（见 `_RATCHET_BACKLOG_MODULE`），它们不在本次两个模块的
    范围内，但必须冻结：任何人在这些文件里新增 async def 同步 DB 直调都会让本用例失败。
    """
    violations = _collect_violations()
    total = sum(len(v) for v in violations.values())
    assert total <= _RATCHET_BASELINE, (
        f"全项目 async def 阻塞调用共 {total} 处，超过棘轮基线 {_RATCHET_BASELINE} 处"
        f"（禁止新增）。新增位置：\n"
        + "\n".join(f"  - {x}" for items in violations.values() for x in items)
    )


def test_backlog_is_confined_to_declared_module():
    """登记存量只允许出现在声明的待办模块里，避免棘轮被『挪个文件』绕过。"""
    violations = _collect_violations()
    outside = {
        f: v for f, v in violations.items()
        if f not in _IN_SCOPE_ROUTERS and f != _RATCHET_BACKLOG_MODULE
    }
    assert not outside, (
        "棘轮范围外出现了未登记的阻塞调用文件："
        + ", ".join(f"{f}({len(v)})" for f, v in outside.items())
        + "；请先修到零，或显式登记进 _RATCHET_BACKLOG_MODULE 并上调基线。"
    )


def test_heavy_registry_entries_are_importable():
    """登记表本身的可维护性守卫：登记项必须真实存在，否则门禁会静默失效。"""
    missing = []
    for mod_name, attr in sorted(_HEAVY_REGISTRY):
        try:
            mod = importlib.import_module(mod_name)
        except Exception as exc:  # pragma: no cover
            missing.append(f"{mod_name}: 导入失败 {exc}")
            continue
        obj = getattr(mod, attr, None)
        if obj is None:
            missing.append(f"{mod_name}.{attr} 不存在")
        elif inspect.iscoroutinefunction(obj):
            missing.append(f"{mod_name}.{attr} 是异步函数，无需登记（请从登记表移除）")
    assert not missing, "重服务登记表与实际代码不符：\n" + "\n".join(f"  - {m}" for m in missing)


def test_probe_detects_planted_violation():
    """自检：门禁本身必须能抓到违规（防止规则写坏后变成永绿）。"""
    source = (
        "from ..db import get_db\n"
        "from ..services.sections import list_sections\n"
        "async def bad_endpoint():\n"
        "    with get_db() as conn:\n"
        "        pass\n"
        "    return list_sections(1)\n"
    )
    tmp = BACKEND / "tests" / "_tmp_gate_probe_router.py"
    tmp.write_text(source, encoding="utf-8")
    try:
        found = _violations_in_file(tmp)
    finally:
        tmp.unlink(missing_ok=True)
    assert any(v.startswith("[A]") for v in found), f"规则 A 自检失败: {found}"
    assert any(v.startswith("[B]") for v in found), f"规则 B 自检失败: {found}"


def test_probe_accepts_compliant_pattern():
    """反向自检：合规写法（嵌套闭包 + to_thread）不得被误报。"""
    source = (
        "from ..db import get_db\n"
        "from ..services.sections import list_sections\n"
        "async def good_endpoint(pid):\n"
        "    def _read():\n"
        "        with get_db(readonly=True) as conn:\n"
        "            return conn.execute('SELECT 1').fetchone()\n"
        "    row = await asyncio.to_thread(_read)\n"
        "    return await asyncio.to_thread(list_sections, pid, readonly=True)\n"
    )
    tmp = BACKEND / "tests" / "_tmp_gate_probe_ok_router.py"
    tmp.write_text(source, encoding="utf-8")
    try:
        found = _violations_in_file(tmp)
    finally:
        tmp.unlink(missing_ok=True)
    assert not found, f"合规写法被误报: {found}"
