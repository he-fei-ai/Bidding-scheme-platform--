"""两模块「只读端点事件循环阻塞 / 写锁争用」修复的组件级交互测试（ENHANCE 2026-10-06）。

背景（PERF 2026-10-05 批次遗漏的两个端点对）
------------------------------------------------
本项目自 2026-09-18 起把重操作移出事件循环，2026-10-05 又统一把「只读端点」
改成 `get_db(readonly=True) + asyncio.to_thread`（见 routers/consistency.py 的
`get_conflicts` / `get_audit_summary`、routers/export.py 的 `download_export` /
`export_pdf`）。本轮补齐该批次**漏掉**的 4 个端点：

| 模块 | 端点 | 修复前 | 阻塞源 |
|---|---|---|---|
| 审核与预检 | `GET /word-count/overview/{id}` | 同步直调 `list_sections()` | 写锁 `get_db()` + 对每章正文跑 `_count_words` |
| 审核与预检 | `GET /word-count/sections/{id}` | 同上 | 同上 |
| 导出文档 | `GET /export/history` | 同步直调 `list_exports()` | 写锁 `get_db()` + 每行 `Path.resolve()` |
| 导出文档 | `GET /export/latest` | 同步直调 `get_latest_export()` + `resolve()/exists()` | 写锁 `get_db()` + 文件 I/O |

这些是「预检/历史/下载」类 GET，纯读却会把整个 loop 卡住，导致 SSE 进度流与
其它并发请求一并饿死（前端表现为预检转圈时全站无响应）。

测试策略（三层，只测行为不测实现）
---------------------------------
1. **行为层（决定性）**：把底层服务替换成「耗时 0.25s 的同步函数」，同时在
   事件循环里跑一个 5ms 心跳协程；若端点确实让出控制权，端点执行期间心跳必须
   明显推进；若仍是同步直调，事件循环被卡死，心跳增量为 0。这是**真的**并发
   证明，不依赖源码文本匹配。
2. **只读连接层**：spy 捕获 `readonly` 实参，确保纯读端点真的走读连接
   （服务层新增关键字参数，默认值保持旧口径 → 向后兼容）。
3. **契约层**：走 TestClient/直接调协程，断言 4 个端点的响应契约与错误分支
   （200/400/403/404）逐分支等价 —— 证明重构没有改变对外行为。
"""
from __future__ import annotations

import asyncio
import time

import pytest

from app.db import get_db, init_db
from app.security.auth import User


OWNER = 4100
STRANGER = 4200
API = "/api/v1"

#: 模拟「重」的同步服务耗时（秒）。0.25s 足以让 5ms 心跳跑 ~50 次，
#: 而同步直调时心跳一次也跑不动，区分度非常干净。
_BLOCK_SECONDS = 0.25
#: 心跳间隔（秒）
_TICK = 0.005
#: 判定「确实让出了控制权」的心跳次数下限。
#: 同步直调 → 0~2；asyncio.to_thread → 约 40~60。取 5 留足抗抖动余量。
_MIN_TICKS = 5


# --------------------------------------------------------------------------
# 公共夹具
# --------------------------------------------------------------------------
def _new_project(name: str, owner: int = OWNER) -> int:
    init_db()
    with get_db() as conn:
        conn.execute(
            "INSERT INTO projects (name, industry, created_by) VALUES (?, ?, ?)",
            (name, "通用", owner),
        )
        return conn.execute("SELECT id FROM projects ORDER BY id DESC LIMIT 1").fetchone()["id"]


def _seed_section(project_id: int, *, title: str, content: str, target_words=None,
                  level: int = 1, sort_order: int = 0, status: str = "completed") -> int:
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO sections (project_id, title, level, sort_order, content, status, target_words) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (project_id, title, level, sort_order, content, status, target_words),
        )
        return int(cur.lastrowid)


async def _loop_ticks_while(awaitable) -> int:
    """执行 `awaitable` 期间，事件循环里的心跳协程能推进多少次。

    返回心跳增量。这是判断「端点是否把控制权还给事件循环」的决定性指标：
    心跳只在 loop 空闲时才能被调度，若端点同步阻塞 loop，增量为 0。
    """
    ticks = 0
    stop = False

    async def _heartbeat():
        nonlocal ticks
        while not stop:
            ticks += 1
            await asyncio.sleep(_TICK)

    hb = asyncio.create_task(_heartbeat())
    await asyncio.sleep(0)  # 确保心跳已启动并至少自增一次
    try:
        await awaitable
    finally:
        stop = True
        await hb
    return ticks


def _run(coro):
    return asyncio.run(coro)


# ==========================================================================
# 一、审核与预检模块：字数统计预检（word_count）
# ==========================================================================
class TestWordCountOffEventLoop:
    """GET /word-count/{overview,sections}/{project_id} 不得阻塞事件循环。"""

    def test_overview_yields_control_to_event_loop(self, monkeypatch):
        """overview：底层服务被替换为耗时同步函数时，心跳必须仍在推进。"""
        from app.routers import word_count as wc

        def _slow(project_id, *, readonly=False):
            time.sleep(_BLOCK_SECONDS)
            return []

        monkeypatch.setattr(wc, "list_sections", _slow)
        pid = _new_project("预检-overview-不阻塞")
        ticks = _run(_loop_ticks_while(
            wc.overview(pid, user=User(OWNER, "o", "user"))
        ))
        assert ticks >= _MIN_TICKS, (
            f"overview 在执行期间事件循环只推进了 {ticks} 次心跳，"
            f"说明 list_sections 仍被同步直调、阻塞了整个 loop"
        )

    def test_sections_yields_control_to_event_loop(self, monkeypatch):
        """sections：同上。"""
        from app.routers import word_count as wc

        def _slow(project_id, *, readonly=False):
            time.sleep(_BLOCK_SECONDS)
            return []

        monkeypatch.setattr(wc, "list_sections", _slow)
        pid = _new_project("预检-sections-不阻塞")
        ticks = _run(_loop_ticks_while(
            wc.section_stats(pid, user=User(OWNER, "o", "user"))
        ))
        assert ticks >= _MIN_TICKS, (
            f"section_stats 执行期间心跳仅 {ticks} 次，仍是同步阻塞"
        )

    def test_both_endpoints_request_readonly_connection(self, monkeypatch):
        """纯读预检端点必须走 readonly 连接（避免与生成/导出写事务争用全局写锁）。"""
        from app.routers import word_count as wc

        seen: list[bool] = []

        def _spy(project_id, *, readonly=False):
            seen.append(bool(readonly))
            return []

        monkeypatch.setattr(wc, "list_sections", _spy)
        pid = _new_project("预检-readonly")
        _run(wc.overview(pid, user=User(OWNER, "o", "user")))
        _run(wc.section_stats(pid, user=User(OWNER, "o", "user")))
        assert len(seen) == 2, "两个端点都应调用 list_sections 恰好一次"
        assert all(seen), f"两个端点均须传 readonly=True，实际捕获 {seen}"

    def test_list_sections_readonly_default_is_backward_compatible(self):
        """服务层新增的 readonly 关键字默认 False，既有调用方行为不变（向后兼容）。"""
        import inspect

        from app.services.sections import list_sections

        sig = inspect.signature(list_sections)
        assert "readonly" in sig.parameters
        assert sig.parameters["readonly"].default is False
        # 仍是位置参数只接受 project_id，老调用 list_sections(pid) 合法
        assert list_sections.__doc__


class TestWordCountContractPreserved:
    """重构不得改变对外响应契约。"""

    def test_overview_contract(self):
        from app.routers.word_count import overview

        pid = _new_project("预检-overview-契约")
        # 2026-10-08 契约：目标字数仅对 L3+ 生效；达标判定为 ±10% 容差。
        # B 300/300 在容差内 → 达标；A 100/300 缺字；C（L1 父节点）无目标。
        _seed_section(pid, title="A", level=3, content="一" * 100, target_words=300)
        _seed_section(pid, title="B", level=3, content="二" * 300, target_words=300, sort_order=1)
        _seed_section(pid, title="C", content="", sort_order=2)

        result = _run(overview(pid, user=User(OWNER, "o", "user")))
        assert result["status"] == "ok"
        assert result["project_id"] == pid
        assert result["total_sections"] == 3
        assert result["on_target_sections"] == 1      # B 达标（300/300 ±10% 内）
        assert result["below_target_sections"] == 1   # A 缺字
        assert result["empty_sections"] == 1          # C 空
        assert result["stats"]["per_level"]["level_3"] == 400
        assert "per_status" in result["stats"]

    def test_sections_contract_and_sort_layering(self):
        from app.routers.word_count import section_stats

        pid = _new_project("预检-sections-契约")
        # 2026-10-08 契约：L1/L2 父节点无目标（排最后）；目标字数仅对 L3+ 生效。
        _seed_section(pid, title="无目标", content="字" * 10)
        _seed_section(pid, title="已达标", level=3, content="字" * 300, target_words=200, sort_order=1)
        _seed_section(pid, title="大缺口", level=3, content="字" * 10, target_words=300, sort_order=2)
        _seed_section(pid, title="小缺口", level=3, content="字" * 150, target_words=200, sort_order=3)

        result = _run(section_stats(pid, user=User(OWNER, "o", "user")))
        assert result["status"] == "ok"
        assert result["total"] == 4
        order = [i["title"] for i in result["items"]]
        # 分层排序：缺字(缺口大→小) → 已达标 → 无目标
        assert order == ["大缺口", "小缺口", "已达标", "无目标"], order
        for item in result["items"]:
            assert 0 <= item["progress"] <= 100
            assert item["word_count_text"]

    def test_empty_project_is_safe(self):
        from app.routers.word_count import overview, section_stats

        pid = _new_project("预检-空项目")
        assert _run(overview(pid, user=User(OWNER, "o", "user")))["total_words"] == 0
        assert _run(section_stats(pid, user=User(OWNER, "o", "user")))["items"] == []

    def test_idor_guard_still_enforced(self):
        """重构不得削弱属权校验：他人项目仍应被拒。"""
        from fastapi import HTTPException

        from app.routers.word_count import overview

        pid = _new_project("预检-越权")
        _seed_section(pid, title="A", content="内容")
        with pytest.raises(HTTPException) as ei:
            _run(overview(pid, user=User(STRANGER, "x", "user")))
        assert ei.value.status_code in (401, 403, 404)


# ==========================================================================
# 二、导出文档模块：导出历史 / 最新下载
# ==========================================================================
class TestExportReadEndpointsOffEventLoop:
    """GET /export/{history,latest} 不得阻塞事件循环、须走只读连接。"""

    def test_history_yields_control_to_event_loop(self, monkeypatch):
        from app.routers import export as ex

        def _slow(project_id, *, readonly=False):
            time.sleep(_BLOCK_SECONDS)
            return []

        monkeypatch.setattr(ex, "list_exports", _slow)
        pid = _new_project("导出-history-不阻塞")
        ticks = _run(_loop_ticks_while(
            ex.export_history(pid, identity={"user_id": OWNER, "username": "o"})
        ))
        assert ticks >= _MIN_TICKS, (
            f"export_history 执行期间心跳仅 {ticks} 次，仍是同步阻塞"
        )

    def test_latest_yields_control_to_event_loop(self, monkeypatch):
        from app.routers import export as ex

        def _slow(project_id, *, readonly=False):
            time.sleep(_BLOCK_SECONDS)
            return None  # → 404 分支，同样证明控制权已让出

        monkeypatch.setattr(ex, "get_latest_export", _slow)
        pid = _new_project("导出-latest-不阻塞")
        with pytest.raises(Exception):
            _run(_loop_ticks_while(
                ex.download_latest(pid, identity={"user_id": OWNER, "username": "o"})
            ))

    def test_both_endpoints_request_readonly_connection(self, monkeypatch):
        from app.routers import export as ex

        seen: list[bool] = []

        def _spy_list(project_id, *, readonly=False):
            seen.append(bool(readonly))
            return []

        def _spy_latest(project_id, *, readonly=False):
            seen.append(bool(readonly))
            return None

        monkeypatch.setattr(ex, "list_exports", _spy_list)
        monkeypatch.setattr(ex, "get_latest_export", _spy_latest)
        pid = _new_project("导出-readonly")

        _run(ex.export_history(pid, identity={"user_id": OWNER, "username": "o"}))
        with pytest.raises(Exception):
            _run(ex.download_latest(pid, identity={"user_id": OWNER, "username": "o"}))

        assert len(seen) == 2
        assert all(seen), f"两个端点均须传 readonly=True，实际捕获 {seen}"

    def test_service_readonly_params_default_backward_compatible(self):
        """服务层新增关键字参数默认值保持旧口径，既有调用方零改动。"""
        import inspect

        from app.services import export as svc

        for fn in (svc.get_latest_export, svc.get_latest_export_info, svc.list_exports):
            sig = inspect.signature(fn)
            assert "readonly" in sig.parameters, f"{fn.__name__} 缺少 readonly 开关"
            assert sig.parameters["readonly"].default is False, (
                f"{fn.__name__} 的 readonly 默认值必须是 False 以保持向后兼容"
            )


class TestExportEndpointContractPreserved:
    """重构后 /latest 与 /history 的响应/错误分支逐条等价。"""

    def _insert_export(self, pid: int, filename: str, status: str = "completed",
                       file_path: str | None = None) -> int:
        with get_db() as conn:
            cur = conn.execute(
                "INSERT INTO exports (project_id, filename, file_path, file_size, status, created_at) "
                "VALUES (?, ?, ?, ?, ?, datetime('now'))",
                (pid, filename, file_path if file_path is not None else filename, 1, status),
            )
            return int(cur.lastrowid)

    def test_history_lists_records(self):
        from app.routers.export import export_history

        pid = _new_project("导出-history-契约")
        self._insert_export(pid, "a.docx")
        self._insert_export(pid, "b.docx", status="failed", file_path="")
        result = _run(export_history(pid, identity={"user_id": OWNER, "username": "o"}))
        assert isinstance(result, list)
        assert len(result) == 2
        assert {r["status"] for r in result} == {"completed", "failed"}

    def test_latest_404_when_no_record(self):
        from fastapi import HTTPException

        from app.routers.export import download_latest

        pid = _new_project("导出-latest-无记录")
        with pytest.raises(HTTPException) as ei:
            _run(download_latest(pid, identity={"user_id": OWNER, "username": "o"}))
        assert ei.value.status_code == 404

    def test_latest_ignores_failed_record(self):
        """失败记录（file_path='')不得被 latest 选中（T2 口径保持）。"""
        from fastapi import HTTPException

        from app.routers.export import download_latest

        pid = _new_project("导出-latest-仅失败记录")
        self._insert_export(pid, "bad.docx", status="failed", file_path="")
        with pytest.raises(HTTPException) as ei:
            _run(download_latest(pid, identity={"user_id": OWNER, "username": "o"}))
        assert ei.value.status_code == 404

    def test_latest_403_on_path_escape(self):
        """历史脏行指向数据目录之外 → 403 拦截（越界守卫未被重构削弱）。"""
        from fastapi import HTTPException

        from app.routers.export import download_latest

        pid = _new_project("导出-latest-越界")
        self._insert_export(pid, "evil.docx", file_path="C:/Windows/System32/evil.docx")
        with pytest.raises(HTTPException) as ei:
            _run(download_latest(pid, identity={"user_id": OWNER, "username": "o"}))
        assert ei.value.status_code == 403

    def test_latest_400_on_bad_suffix(self):
        """后缀白名单 .docx/.dotx 口径保持。"""
        from fastapi import HTTPException

        from app.config import PROJECTS_DATA_DIR
        from app.routers.export import download_latest

        pid = _new_project("导出-latest-后缀")
        junk = PROJECTS_DATA_DIR / str(pid) / "exports" / "note.txt"
        junk.parent.mkdir(parents=True, exist_ok=True)
        junk.write_text("x", encoding="utf-8")
        self._insert_export(pid, "note.txt", file_path=str(junk))
        with pytest.raises(HTTPException) as ei:
            _run(download_latest(pid, identity={"user_id": OWNER, "username": "o"}))
        assert ei.value.status_code == 400


class TestRoutesRegisteredUnderApiV1:
    """路由挂载与前端调用链（/api/v1 前缀）保持不变。"""

    def test_endpoints_present(self):
        from app.main import app

        paths = app.openapi()["paths"]
        for p in (
            f"{API}/word-count/overview/{{project_id}}",
            f"{API}/word-count/sections/{{project_id}}",
            f"{API}/projects/{{project_id}}/export/history",
            f"{API}/projects/{{project_id}}/export/latest",
            f"{API}/rejection/status/{{task_id}}",
            f"{API}/rejection/tasks",
        ):
            assert p in paths, f"路由未挂载: {p}"


# ==========================================================================
# 三、审核与预检模块：标书查重 / 废标检查（同一批遗漏端点）
# ==========================================================================
class TestDuplicateAndRejectionOffEventLoop:
    """`/duplicate/tasks`、`/rejection/{status,tasks,...}` 同样不得阻塞 loop。"""

    def test_duplicate_list_tasks_yields_control(self, monkeypatch):
        from app.routers import duplicate as dup

        def _slow(limit=20, project_id=None, *, readonly=False):
            time.sleep(_BLOCK_SECONDS)
            return []

        monkeypatch.setattr(dup.duplicate, "list_tasks", _slow)
        pid = _new_project("查重-不阻塞")
        ticks = _run(_loop_ticks_while(
            dup.list_tasks(project_id=pid, user=User(OWNER, "o", "user"))
        ))
        assert ticks >= _MIN_TICKS, f"查重任务列表阻塞了 loop（心跳 {ticks} 次）"

    def test_duplicate_list_tasks_requests_readonly(self, monkeypatch):
        from app.routers import duplicate as dup

        seen: list[bool] = []

        def _spy(limit=20, project_id=None, *, readonly=False):
            seen.append(bool(readonly))
            return []

        monkeypatch.setattr(dup.duplicate, "list_tasks", _spy)
        pid = _new_project("查重-readonly")
        _run(dup.list_tasks(project_id=pid, user=User(OWNER, "o", "user")))
        assert seen == [True], f"查重任务列表应走只读连接，实际 {seen}"

    def test_duplicate_service_readonly_default_backward_compatible(self):
        import inspect

        from app.services.duplicate import list_tasks as svc_list_tasks

        sig = inspect.signature(svc_list_tasks)
        assert sig.parameters["readonly"].default is False

    def test_rejection_status_yields_control(self, monkeypatch):
        """废标检查状态查询（前端高频轮询）不得阻塞 loop。"""
        from app.routers import rejection as rj

        async def _noop_access(*_a, **_k):
            return None

        def _slow_get_task(task_id):
            time.sleep(_BLOCK_SECONDS)
            return {"id": task_id, "status": "completed", "progress": 100,
                    "progress_message": "done", "error": ""}

        monkeypatch.setattr(rj, "enforce_task_access", _noop_access)
        monkeypatch.setattr(rj.rejection, "get_task", _slow_get_task)
        ticks = _run(_loop_ticks_while(
            rj.get_status(1, user=User(OWNER, "o", "user"))
        ))
        assert ticks >= _MIN_TICKS, f"废标状态查询阻塞了 loop（心跳 {ticks} 次）"

    def test_rejection_task_lists_yield_control(self, monkeypatch):
        """废标任务列表（项目视图 / 全局视图）不得阻塞 loop。"""
        from app.routers import rejection as rj

        def _slow_list(project_id=None, limit=20):
            time.sleep(_BLOCK_SECONDS)
            return []

        def _slow_by_project(project_id, limit=100):
            time.sleep(_BLOCK_SECONDS)
            return []

        monkeypatch.setattr(rj.rejection, "list_tasks", _slow_list)
        pid = _new_project("废标-列表不阻塞")
        t1 = _run(_loop_ticks_while(
            rj.list_tasks(project_id=pid, user=User(OWNER, "o", "user"))
        ))
        assert t1 >= _MIN_TICKS, f"废标任务列表阻塞了 loop（心跳 {t1} 次）"

        monkeypatch.setattr(rj.rejection, "get_tasks_by_project", _slow_by_project)
        t2 = _run(_loop_ticks_while(
            rj.get_project_tasks(pid, user=User(OWNER, "o", "user"))
        ))
        assert t2 >= _MIN_TICKS, f"废标项目任务列表阻塞了 loop（心跳 {t2} 次）"

    def test_rejection_404_still_raised_when_task_missing(self, monkeypatch):
        """移入线程池不得改变 404 语义。"""
        from fastapi import HTTPException

        from app.routers import rejection as rj

        async def _noop_access(*_a, **_k):
            return None

        monkeypatch.setattr(rj, "enforce_task_access", _noop_access)
        monkeypatch.setattr(rj.rejection, "get_task", lambda _tid: None)
        with pytest.raises(HTTPException) as ei:
            _run(rj.get_status(999999, user=User(OWNER, "o", "user")))
        assert ei.value.status_code == 404

