"""LEFTOVER-2 2026-11-02：SectionContentCache 可选 Redis L2 共享层回归测试。

覆盖：
  A. 默认关 = 行为逐字不变（不触碰后端，向后兼容红线）
  B. 开启后跨实例（模拟多 worker）共享命中 + L1 回填
  C. 各级失效路径同步清理 L2（async by_ids / sync by ids / clear scan）
  D. 后端异常全降级，不穿透主链路
  E. get_stats 运维可见性新键

测试缝隙：monkeypatch workflows_sections._shared_backend 注入 fake，
不需要真实 Redis；开关用 monkeypatch.setenv（conftest 逐用例清理基线为关）。
"""

import asyncio

import pytest

from app.services.ai import workflows_sections as ws


class _FakeBackend:
    """内存版共享层后端：与 _RedisSharedBackend 同接口，记录全部调用。"""

    def __init__(self):
        self.store: dict[str, tuple[str, int]] = {}
        self.gets = 0
        self.sets = 0
        self.deletes = 0
        self.scan_deletes = 0

    def get(self, full_key):
        self.gets += 1
        rec = self.store.get(full_key)
        return rec[0] if rec else None

    def set(self, full_key, value, ttl):
        self.sets += 1
        self.store[full_key] = (value, ttl)
        return True

    def delete(self, full_key):
        self.deletes += 1
        self.store.pop(full_key, None)

    def scan_delete(self, key_prefix):
        self.scan_deletes += 1
        keys = [k for k in self.store if k.startswith(key_prefix)]
        for k in keys:
            self.store.pop(k)
        return len(keys)


@pytest.fixture()
def fake_backend(monkeypatch):
    """注入 fake 后端并开启 L2 开关。"""
    backend = _FakeBackend()
    monkeypatch.setattr(ws, "_shared_backend", lambda: backend)
    monkeypatch.setenv("SECTION_CONTENT_REDIS_SHARED_ENABLED", "1")
    return backend


@pytest.fixture()
def off(monkeypatch):
    """显式关闭开关（回归默认态），注入 fake 仅用于计数断言零触碰。"""
    backend = _FakeBackend()
    monkeypatch.setattr(ws, "_shared_backend", lambda: backend)
    monkeypatch.delenv("SECTION_CONTENT_REDIS_SHARED_ENABLED", raising=False)
    return backend


def _key(pid, sid):
    return f"section_content:{pid}:{sid}"


class TestDefaultOffBackwardCompat:
    def test_no_backend_touch_when_disabled(self, off):
        """默认关：get/set 不得触碰任何后端（旧单级行为逐字保持）。"""

        async def _flow():
            c = ws.SectionContentCache()
            await c.set(_key(1, 10), "正文A")
            assert await c.get(_key(1, 10)) == "正文A"
            assert await c.get(_key(1, 99)) is None

        asyncio.run(_flow())
        assert (off.gets, off.sets, off.deletes, off.scan_deletes) == (0, 0, 0, 0)

    def test_stats_reports_disabled(self, off):
        assert ws.SectionContentCache().get_stats()["redis_shared_enabled"] is False


class TestSharedLayerEnabled:
    def test_cross_instance_hit(self, fake_backend):
        """worker A 写入 → worker B（另一实例）L1 miss 回源 L2 命中并回填。"""

        async def _flow():
            a = ws.SectionContentCache()
            b = ws.SectionContentCache()
            await a.set(_key(7, 100), "共享正文")
            assert fake_backend.store == {"ssc:" + _key(7, 100): ("共享正文", a._ttl)}
            # B 本地无 → 回源命中
            assert await b.get(_key(7, 100)) == "共享正文"
            # 命中后回填 B 的 L1：再读不再打后端
            calls = fake_backend.gets
            assert await b.get(_key(7, 100)) == "共享正文"
            assert fake_backend.gets == calls

        asyncio.run(_flow())

    def test_ttl_aligned_with_l1(self, fake_backend):
        """L2 键 TTL 与 L1 配置一致（向后兼容：不改默认 3600 口径）。"""

        async def _flow():
            c = ws.SectionContentCache(ttl=123)
            await c.set(_key(1, 2), "x")

        asyncio.run(_flow())
        assert fake_backend.store["ssc:" + _key(1, 2)][1] == 123

    def test_invalidate_by_ids_syncs_l2(self, fake_backend):
        """async 按 id 失效：精确删除对应共享键，不误伤同项目其它章节。"""

        async def _flow():
            c = ws.SectionContentCache()
            await c.set(_key(9, 1), "正文1")
            await c.set(_key(9, 2), "正文2")
            n = await c.invalidate_by_ids(9, [1])
            assert n == 1
            assert ("ssc:" + _key(9, 1)) not in fake_backend.store
            assert ("ssc:" + _key(9, 2)) in fake_backend.store

        asyncio.run(_flow())

    def test_invalidate_ids_sync_cross_process(self, fake_backend):
        """同步失效（invalidate_content_caches 入口）：本进程 L1 未命中也要清 L2，
        否则 worker A 旁路直写后 worker B 的旧共享副本会回灌旧正文。"""
        a = ws.SectionContentCache()

        async def _seed():
            await a.set(_key(5, 11), "旧正文")

        asyncio.run(_seed())
        b = ws.SectionContentCache()  # 模拟另一 worker：L1 为空
        n = b.invalidate_ids_sync(5, [11])
        assert n == 0  # 本地无命中
        assert ("ssc:" + _key(5, 11)) not in fake_backend.store  # 但 L2 已被清
        # L2 清后任何实例回源都是 miss
        assert asyncio.run(b.get(_key(5, 11))) is None

    def test_invalidate_ids_sync_project_wide_scan(self, fake_backend):
        """同步整项目失效（ids=None）：走 scan 前缀删除。"""
        c = ws.SectionContentCache()
        asyncio.run(c.set(_key(3, 1), "a"))
        asyncio.run(c.set(_key(3, 2), "b"))
        asyncio.run(c.set(_key(4, 1), "c"))
        c.invalidate_ids_sync(3, None)
        assert [k for k in fake_backend.store if k.startswith("ssc:section_content:3:")] == []
        assert any(k.startswith("ssc:section_content:4:") for k in fake_backend.store)

    def test_clear_removes_all_shared(self, fake_backend):
        c = ws.SectionContentCache()
        asyncio.run(c.set(_key(1, 1), "a"))
        asyncio.run(c.clear())
        assert fake_backend.store == {}

    def test_stats_reports_enabled(self, fake_backend):
        assert ws.SectionContentCache().get_stats()["redis_shared_enabled"] is True


class TestDegradation:
    def test_backend_exceptions_never_leak(self, monkeypatch):
        """后端任意原语抛异常 → L1 语义不受影响、不穿透主链路。"""

        class _Boom:
            def get(self, k):
                raise RuntimeError("redis down")

            def set(self, k, v, t):
                raise RuntimeError("redis down")

            def delete(self, k):
                raise RuntimeError("redis down")

            def scan_delete(self, p):
                raise RuntimeError("redis down")

        monkeypatch.setattr(ws, "_shared_backend", lambda: _Boom())
        monkeypatch.setenv("SECTION_CONTENT_REDIS_SHARED_ENABLED", "1")

        async def _flow():
            c = ws.SectionContentCache()
            await c.set(_key(1, 1), "正文")  # L2 炸，L1 写入照常
            assert await c.get(_key(1, 1)) == "正文"  # L1 命中，不读 L2
            c2 = ws.SectionContentCache()
            assert await c2.get(_key(1, 1)) is None  # L2 miss（异常降级）不抛
            await c.invalidate_by_ids(1, [1])  # 不抛
            assert await c.get(_key(1, 1)) is None  # L1 已失效

        asyncio.run(_flow())
