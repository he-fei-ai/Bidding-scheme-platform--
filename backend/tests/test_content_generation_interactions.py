"""正文生成模块（content generation）组件级交互测试

覆盖 2026-09-29 修复项与正文生成链路（路由 ↔ SSE 主循环 ↔ AI 工作流 ↔ DB ↔ 全局事实）：

1. **P0 心跳风暴 / 队列饿死回归**（sse_content.py 主循环）
   原缺陷：last_heartbeat_time 写后从未被读，心跳分支带 continue。AI 单章耗时
   超过 8 秒（并发场景常态）后，主循环每轮都走心跳分支并 continue，永不执行
   queue.get —— 队列被饿死，进度/完成事件滞留队列（前端进度条卡死、日志延迟
   错位），且 `while not finished or not queue.empty()` 因队列永不排空而无法
   退出，SSE 流只能等 4 小时硬熔断。
   验证方式备注：回退该修复后本文件的集成测试会因“流永不结束”而挂住
   （需人工终止进程），因此另留 TestHeartbeatThrottleLogic 纯函数单测作快速定位。
2. 心跳节流：空闲期只发 1 条 keep_alive，而非每轮一条。
3. 数据链贯通：analysis.global_facts_json → _fetch_project_data_async →
   generate_sections 的 global_facts 形参（跨模块事实注入）。
4. 正文落库：section_complete_callback → _save_section_to_db 真实写 DB。
5. 章节筛选与排序：叶子节点判定、按 path 前序排序（编号链路）。
"""
import json
import sys
import time as _real_time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.db import get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.routers.sse import sse_content  # noqa: E402
from app.security.auth import get_current_user  # noqa: E402

API = "/api/v1/projects"

# 伪造时钟：主循环与 run_generation 只用到 time.time()，其余属性透传真模块
class FakeClock:
    def __init__(self, start: float):
        self.t = float(start)

    def time(self) -> float:
        return self.t

    def __getattr__(self, name):
        return getattr(_real_time, name)


def _gm_mock():
    """generation_manager 桩：内存任务表置空，持久化分支自然跳过"""
    gm = MagicMock(name="generation_manager")
    gm.create_task = AsyncMock(return_value="gen-test-task")
    gm.update_section_progress = AsyncMock()
    gm.finalize_task = AsyncMock()
    gm.is_paused = AsyncMock(return_value=False)
    gm._lock = __import__("asyncio").Lock()
    gm._tasks = {}
    gm._persist_locks = {}
    return gm


@pytest.fixture()
def client():
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=1)
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture()
def seeded_project(client):
    """真实建项目 + 三级目录 + analysis 行（含全局事实），走完整数据链"""
    resp = client.post(
        API,
        json={"name": "正文生成交互测试项目", "industry": "公共建筑", "bidder_name": "测试投标方"},
    )
    assert resp.status_code in (200, 201), resp.text
    pid = resp.json()["id"]

    def mk(payload):
        r = client.post(f"{API}/{pid}/sections", json=payload)
        assert r.status_code == 201, r.text
        return r.json()

    root = mk({"title": "第一章 施工组织"})
    child = mk({"title": "1.1 工期保证措施", "parent_id": root["id"]})
    leaf1 = mk({"title": "1.1.1 工期承诺", "parent_id": child["id"]})
    leaf2 = mk({"title": "1.1.2 人员配置", "parent_id": child["id"]})

    facts = [
        {"key": "duration", "label": "总工期", "value": "180天", "category": "schedule"},
        {"key": "pm", "label": "项目经理", "value": "张工", "category": "personnel"},
        # placeholder 档产出的**带说明**占位项：必须在注入正文前被过滤掉
        {"key": "chief", "label": "技术负责人", "value": "【待填写：技术负责人姓名】",
         "category": "personnel", "needs_manual_input": True},
    ]
    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO analysis (project_id, summary_json, scoring_points_json,"
            " global_facts_json) VALUES (?, ?, ?, ?)",
            (pid, json.dumps({"project_overview": "公共建筑施工招标"}, ensure_ascii=False),
             json.dumps([{"point": "工期保证", "score": 10}], ensure_ascii=False),
             json.dumps(facts, ensure_ascii=False)),
        )
        conn.commit()

    return {
        "pid": pid,
        "leaf_ids": [leaf1["id"], leaf2["id"]],
        "all_ids": [root["id"], child["id"], leaf1["id"], leaf2["id"]],
    }


def _consume_sse(client, url, max_lines=400, wall_timeout=8.0):
    """读 SSE 行并解析 data: 事件。

    两个硬上限（行数 / 真实时间）是必要的防护：P0 饿死缺陷下 SSE 流永不结束
    （生产上只能等 4 小时硬熔断），没有上限的测试会直接挂死。

    Returns:
        (events, ended_cleanly): ended_cleanly=True 表示服务端自行收尾，
        而不是测试因超限主动断开。
    """
    events = []
    deadline = _real_time.monotonic() + wall_timeout
    ended_cleanly = False
    with client.stream("GET", url) as r:
        assert r.status_code == 200, r.text
        for line in r.iter_lines():
            if _real_time.monotonic() > deadline or len(events) >= max_lines:
                break
            if not line or not line.startswith("data:"):
                continue
            try:
                events.append(json.loads(line[len("data:"):].strip()))
            except json.JSONDecodeError:
                continue
        else:
            ended_cleanly = True
    return events, ended_cleanly


class TestSSEHeartbeatQueueStarvation:
    """P0 回归：AI 静默期心跳不得饿死队列，且必须受最小间隔节流"""

    def test_progress_events_survive_ai_idle_gap(self, client, seeded_project):
        import asyncio

        pid = seeded_project["pid"]
        clock = FakeClock(_real_time.time())
        idle_seconds = sse_content.SSE_HEARTBEAT_INTERVAL_SECS + 12  # 跨过阈值并保持静默

        async def fake_run_sections(provider, leaves, **kwargs):
            progress_cb = kwargs["progress_callback"]
            complete_cb = kwargs["section_complete_callback"]
            results = []
            for idx, ld in enumerate(leaves):
                await progress_cb(ld["id"], ld["title"], "generating", 20 + idx * 10, "生成中")
                if idx == 0:
                    # 先给主循环一个真实轮询周期把上一章事件消费并转发，
                    # 使 last_activity_time 停在推进前的时刻，再模拟 AI 静默
                    await asyncio.sleep(0.3)
                    clock.t += idle_seconds
                    await asyncio.sleep(1.3)
                content = f"《{ld['title']}》正文：工期 180 天，项目经理张工，" \
                          f"本章围绕工期保证措施展开论述，共三条具体做法。"
                res = {"id": ld["id"], "title": ld["title"], "status": "completed",
                       "content": content}
                await complete_cb(res)
                results.append(res)
            return results

        with patch.object(sse_content, "time", clock), \
             patch.object(sse_content, "_try_get_provider",
                          AsyncMock(return_value=(MagicMock(), None, 2))), \
             patch.object(sse_content, "_run_sections", fake_run_sections), \
             patch.object(sse_content, "generation_manager", _gm_mock()), \
             patch("app.routers.sections.require_project_permission", AsyncMock()):
            events, ended_cleanly = _consume_sse(
                client, f"{API}/{pid}/sections/generate-content"
            )

        assert ended_cleanly, "心跳风暴下队列永不排空，SSE 流无法自行结束"
        kinds = [e.get("status") for e in events]
        keep_alives = [e for e in events if e.get("event") == "keep_alive"]

        # ① 队列未被饿死：每一章的进度事件都必须真正转发给客户端
        progressed_ids = {e.get("section_id") for e in events if e.get("section_id")}
        assert set(seeded_project["leaf_ids"]) <= progressed_ids, (
            f"AI 静默期后的进度事件被心跳 continue 吞掉，仅收到 {kinds}"
        )

        # ② 心跳受节流：整段静默只允许 1 条 keep_alive（缺陷版本会刷屏数百条）
        assert len(keep_alives) <= 2, f"心跳风暴：收到 {len(keep_alives)} 条 keep_alive"

        # ③ 终态事件可达且流正常结束（缺陷版本队列永不排空，流不会结束）
        terminal = [e for e in events
                    if e.get("status") in ("completed", "error", "stopped")
                    and e.get("section_id") is None]
        assert terminal, f"未收到终止事件，事件序列={kinds}"
        assert terminal[-1]["status"] == "completed", terminal[-1]
        assert terminal[-1]["success"] == len(seeded_project["leaf_ids"])

    def test_sections_persisted_after_generation(self, client, seeded_project):
        """跨模块数据链：生成结果经 _save_section_to_db 真实落库，状态不再卡 generating"""
        import asyncio

        pid = seeded_project["pid"]
        clock = FakeClock(_real_time.time())

        async def fake_run_sections(provider, leaves, **kwargs):
            complete_cb = kwargs["section_complete_callback"]
            results = []
            for ld in leaves:
                content = f"《{ld['title']}》正文内容，工期 180 天，项目经理张工，具体措施如下所述。"
                res = {"id": ld["id"], "title": ld["title"], "status": "completed",
                       "content": content}
                await complete_cb(res)
                results.append(res)
            return results

        with patch.object(sse_content, "time", clock), \
             patch.object(sse_content, "_try_get_provider",
                          AsyncMock(return_value=(MagicMock(), None, 2))), \
             patch.object(sse_content, "_run_sections", fake_run_sections), \
             patch.object(sse_content, "generation_manager", _gm_mock()), \
             patch("app.routers.sections.require_project_permission", AsyncMock()):
            events, ended_cleanly = _consume_sse(
                client, f"{API}/{pid}/sections/generate-content", max_lines=200
            )

        assert ended_cleanly, "SSE 流未自行结束"

        assert any(e.get("status") == "completed" and e.get("section_id") is None
                   for e in events), events

        with get_db() as conn:
            rows = conn.execute(
                "SELECT id, status, content FROM sections WHERE project_id = ? ORDER BY id",
                (pid,),
            ).fetchall()
        by_id = {r["id"]: r for r in rows}
        for lid in seeded_project["leaf_ids"]:
            assert by_id[lid]["status"] == "completed", by_id[lid]["status"]
            assert len(by_id[lid]["content"]) > 20

    def test_global_facts_flow_into_section_workflow(self, client, seeded_project):
        """全局事实 → 正文生成 的跨模块传递：generate_sections 收到 facts 列表"""
        import asyncio

        pid = seeded_project["pid"]
        clock = FakeClock(_real_time.time())
        captured = {}

        async def fake_run_sections(provider, leaves, **kwargs):
            captured["global_facts"] = kwargs.get("global_facts")
            complete_cb = kwargs["section_complete_callback"]
            results = []
            for ld in leaves:
                res = {"id": ld["id"], "title": ld["title"], "status": "completed",
                       "content": f"《{ld['title']}》正文，工期 180 天，内容足够长以便通过空壳判定校验。"}
                await complete_cb(res)
                results.append(res)
            return results

        with patch.object(sse_content, "time", clock), \
             patch.object(sse_content, "_try_get_provider",
                          AsyncMock(return_value=(MagicMock(), None, 2))), \
             patch.object(sse_content, "_run_sections", fake_run_sections), \
             patch.object(sse_content, "generation_manager", _gm_mock()), \
             patch("app.routers.sections.require_project_permission", AsyncMock()):
            _consume_sse(client, f"{API}/{pid}/sections/generate-content", max_lines=200)

        facts = captured["global_facts"]
        assert isinstance(facts, list) and facts, f"全局事实未传入正文生成: {facts!r}"
        values = {f.get("value") for f in facts}
        assert "180天" in values and "张工" in values, facts
        # P1 回归：带说明的【待填写：xxx】占位项不得泄漏进正文 prompt
        assert not any("【待填写" in str(v) for v in values), facts


class TestHeartbeatThrottleLogic:
    """心跳判据纯函数单测（缺陷定位无需依赖 SSE 连接，秒级失败）"""

    def test_idle_beyond_threshold_triggers_first_heartbeat(self):
        assert sse_content._should_emit_heartbeat(108.0, 100.0, 0.0, 8.0) is True

    def test_still_active_does_not_trigger(self):
        assert sse_content._should_emit_heartbeat(107.0, 100.0, 0.0, 8.0) is False

    def test_recent_heartbeat_throttles_next(self):
        """P0 回归：空闲充分但刚发过心跳 → 必须不发（原实现 last_heartbeat_time 从未被读）"""
        assert sse_content._should_emit_heartbeat(120.0, 100.0, 118.0, 8.0) is False

    def test_non_positive_interval_disables(self):
        assert sse_content._should_emit_heartbeat(1e9, 0.0, 0.0, 0.0) is False


class TestGlobalFactsParsing:
    """global_facts_json 解析契约（写入方 facts.py ↔ 读取方 sse/tasks）"""

    def test_flat_list_and_placeholder_prefix(self):
        from app.routers.sse.sse_utils import _parse_global_facts_items

        raw = json.dumps([
            {"key": "k1", "value": "180天"},
            {"key": "k2", "value": "【待填写】"},
            {"key": "k3", "value": "【待填写：项目经理姓名】"},
        ], ensure_ascii=False)
        assert [f["key"] for f in _parse_global_facts_items(raw)] == ["k1"]

    def test_wrapped_items_form_accepted(self):
        """docstring 声称支持分组，实现仅认顶层 list → {"items": [...]} 曾静默丢事实"""
        from app.routers.sse.sse_utils import _parse_global_facts_items

        raw = json.dumps({"items": [{"key": "k1", "value": "180天"}]}, ensure_ascii=False)
        assert _parse_global_facts_items(raw) == [{"key": "k1", "value": "180天"}]

    def test_bad_payloads_degrade_to_empty(self):
        from app.routers.sse.sse_utils import _parse_global_facts_items

        assert _parse_global_facts_items(None) == []
        assert _parse_global_facts_items("not json") == []
        assert _parse_global_facts_items(json.dumps({"foo": 1})) == []
        assert _parse_global_facts_items(json.dumps("纯字符串")) == []


class TestSSELeafSelection:
    """章节筛选与编号链路（叶子判定 / path 排序 / 已完成跳过）"""

    def test_leaf_only_and_path_order(self, client, seeded_project):
        import asyncio

        pid = seeded_project["pid"]
        clock = FakeClock(_real_time.time())
        seen = {}

        async def fake_run_sections(provider, leaves, **kwargs):
            seen["leaves"] = list(leaves)
            complete_cb = kwargs["section_complete_callback"]
            results = []
            for ld in leaves:
                res = {"id": ld["id"], "title": ld["title"], "status": "completed",
                       "content": f"《{ld['title']}》正文，路径 {ld['path']}，内容长度足够通过校验。"}
                await complete_cb(res)
                results.append(res)
            return results

        with patch.object(sse_content, "time", clock), \
             patch.object(sse_content, "_try_get_provider",
                          AsyncMock(return_value=(MagicMock(), None, 2))), \
             patch.object(sse_content, "_run_sections", fake_run_sections), \
             patch.object(sse_content, "generation_manager", _gm_mock()), \
             patch("app.routers.sections.require_project_permission", AsyncMock()):
            _consume_sse(client, f"{API}/{pid}/sections/generate-content", max_lines=200)

        leaves = seen["leaves"]
        assert [s["id"] for s in leaves] == seeded_project["leaf_ids"]
        # 非叶子（root/child）不得进入生成列表
        assert not set(seeded_project["all_ids"][:2]) & {s["id"] for s in leaves}
        # 编号链路：path 为位置派生编号，且按前序自然排序
        assert [s["path"] for s in leaves] == sorted(
            [s["path"] for s in leaves], key=lambda p: tuple(int(x) for x in p.split("."))
        )
        assert leaves[0]["path"].count(".") >= 2

    def test_completed_sections_skipped_when_all_valid(self, client, seeded_project):
        """全部叶子已有有效正文且状态 completed → SSE 直接返回提示，不再重复计费"""
        pid = seeded_project["pid"]
        clock = FakeClock(_real_time.time())
        long_text = (
            "这是一段足够长的正文内容，用于通过空壳章节的最小字数有效性判定："
            "只有 status=completed 且正文长度超过阈值（50 字）的章节才会被默认模式跳过，"
            "避免对已完成章节重复计费。这里刻意写得足够长。"
        )
        with get_db() as conn:
            conn.execute(
                "UPDATE sections SET status='completed', content=? WHERE project_id=? AND level=3",
                (long_text, pid),
            )
            conn.commit()

        with patch.object(sse_content, "time", clock), \
             patch.object(sse_content, "_try_get_provider",
                          AsyncMock(return_value=(MagicMock(), None, 2))), \
             patch("app.routers.sections.require_project_permission", AsyncMock()):
            events, ended_cleanly = _consume_sse(
                client, f"{API}/{pid}/sections/generate-content", max_lines=50
            )

        assert ended_cleanly
        assert any(e.get("status") == "info" and "没有需要重新生成" in e.get("message", "")
                   for e in events), events
