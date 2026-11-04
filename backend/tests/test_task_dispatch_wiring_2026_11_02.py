"""LEFTOVER-4 2026-11-02：Celery 任务分发接线守卫测试。

背景（判定结论，详见 celery_app.WEB_DISPATCHED_TASKS 注释）：
正文批量任务 tasks.content.generate_sections **刻意不做 Web 分发**——SSE 主管线
（generation_tasks + task_lock + 断线重连 + 进度上报）已是完整实时路径，再加
Celery 分发 API 会形成双写管线（进度/版本链双份维护、正文互相覆盖风险）。
Celery 任务本体保留为运维手动入口，其与 SSE 的互斥和写库后三层缓存失效必须
长期在位。

本测试把上述决策"守则化"：
  A. 全库扫描 dispatch_task("tasks.*") 真实接线点，必须与 WEB_DISPATCHED_TASKS
     登记一致（新增/移除接线不同步登记即红）；
  B. 正文任务必须保持在 INLINE_BLOCKED_TASKS（防误解禁导致 Web 进程就地跑 1h）；
  C. 正文 Celery 任务源码必须保留 task_lock 互斥与 invalidate_content_caches
     失效调用（防回归，二者分别对应 2026-09-16 并发修复与 2026-11-02 FIX-2）。
"""

from pathlib import Path
import re

from app.celery_app import INLINE_BLOCKED_TASKS, WEB_DISPATCHED_TASKS


APP_DIR = Path(__file__).resolve().parents[1] / "app"

# dispatch_task( 后紧跟 tasks.* 字面量（允许跨行）。限定 tasks. 前缀以自然排除
# celery_app.py / report_schedule_runner.py docstring 里的 app.celery_app.* 示例；
# celery_app.py 本体排除（其 dispatch_task 定义与帮助文案不属于业务接线）。
_DISPATCH_RE = re.compile(r"""dispatch_task\(\s*["'](tasks\.[^"']+)["']""")


def _scan_wired_task_names() -> set[str]:
    names: set[str] = set()
    for py in APP_DIR.rglob("*.py"):
        if py.name == "celery_app.py":
            continue
        text = py.read_text(encoding="utf-8", errors="replace")
        names.update(_DISPATCH_RE.findall(text))
    return names


class TestDispatchWiring:
    def test_wired_tasks_match_registry(self):
        """A：真实接线点 == WEB_DISPATCHED_TASKS（漂移守卫）。"""
        wired = _scan_wired_task_names()
        assert wired == set(WEB_DISPATCHED_TASKS), (
            f"dispatch_task 接线与登记漂移：代码接线={sorted(wired)} "
            f"登记={sorted(WEB_DISPATCHED_TASKS)}；"
            "新增分发点须同步登记 WEB_DISPATCHED_TASKS 并复核双管线风险"
        )

    def test_content_task_stays_inline_blocked(self):
        """B：正文任务禁止 Web 进程内就地执行（1h 级耗时）。"""
        assert "tasks.content.generate_sections" in INLINE_BLOCKED_TASKS

    def test_content_task_keeps_lock_and_cache_invalidation(self):
        """C：Celery 正文任务源码保留并发互斥 + 三层缓存失效（防回归）。"""
        src = (APP_DIR / "tasks" / "__init__.py").read_text(
            encoding="utf-8", errors="replace"
        )
        start = src.index("def generate_section_content")
        # 函数体到下一个模块级 def 为止
        rest = src[start:]
        nxt = rest.find("\n@celery_app.task", 1)
        body = rest[: nxt if nxt > 0 else len(rest)]
        assert "task_lock" in body and "acquire" in body, (
            "Celery 正文任务丢失 task_lock 互斥：将与 SSE 主管线并发双写同一批章节"
        )
        assert "invalidate_content_caches" in body, (
            "Celery 正文任务丢失缓存失效（FIX-2 2026-11-02）：旁路直写后 "
            "build_tree/Redis 整包/内存 LRU 三层旧正文会回灌生成链路"
        )
