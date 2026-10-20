"""BUG-FIX 2026-10-19：评分点识别两阶段 AI 调用瞬时超时要有限重试的回归测试。

背景（生产日志 2026-10-19 21:30，项目 1871130）：
  /ai/scoring 第一阶段 provider.chat 恰好越过 60s 阈值被 with_timeout 取消 →
  extract_scoring_points 直接降级 return [] → 路由抛 502「识别失败」；
  21:32 用户重试时同一提供商 59s 即成功。一次瞬时抖动被"零重试"放大成
  确定性失败，且第一阶段失败后连第二阶段（独立于第一阶段结果、本可产出
  评分点）也被整体跳过。

同模块对照：摘要 generate_tender_summary 走 call_ai_text(retries=1)，
超时自动重试一次（F-P0-2）；评分点两阶段仍是裸 with_timeout(provider.chat)
——同模块两套口径。修复：两阶段统一改走 call_ai_text，瞬时超时重试一次，
重试耗尽仍失败则保持原有降级语义（第一阶段→[]，第二阶段→仅第一阶段结果）。
空响应不判失败（require_nonempty=False），维持"第一阶段解析为空仍继续
第二阶段审查"的旧语义。
"""

import json

from fastapi.testclient import TestClient
import pytest

from app.db import init_db
from app.services.ai import workflows_analysis
from app.services.ai.workflows_analysis import extract_scoring_points


# 两条评分点权重和为 100，避开 normalize_writing_targets 的 [权重异常] 伪项，
# 让断言只反映"重试是否发生"，不受权重校验分支干扰。
_FIRST_JSON = json.dumps(
    [{"name": "施工组织设计", "weight": "60", "standard": "内容完整、针对性强"}],
    ensure_ascii=False,
)
_REVIEW_JSON = json.dumps(
    [{"name": "进度计划保证措施", "weight": "40", "standard": "工期保障措施合理"}],
    ensure_ascii=False,
)


class _ScriptedProvider:
    """按调用次序返回脚本内容：条目为 Exception 则抛出，否则作为响应文本返回。

    timeout 模拟厂商配置的单请求超时（provider_factory 透传的 provider.timeout）；
    默认 0 表示不携带配置，业务层走自身下限。
    """

    def __init__(self, script, timeout: int = 0):
        self._script = list(script)
        self.calls = 0
        self.timeout = timeout

    async def chat(self, messages, **kwargs):
        self.calls += 1
        item = self._script[min(self.calls - 1, len(self._script) - 1)]
        if isinstance(item, BaseException):
            raise item
        return item


class TestScoringStageRetryOnTransientTimeout:
    async def test_first_stage_transient_timeout_recovers_on_retry(self):
        """第一阶段瞬时超时 → 第二次调用成功：应拿到两阶段合并结果（旧行为：直接 []）。"""
        provider = _ScriptedProvider(
            [TimeoutError("AI调用超时（60秒）"), _FIRST_JSON, _REVIEW_JSON]
        )
        points = await extract_scoring_points(provider, "招标文件上下文", "项目摘要")
        names = [p["name"] for p in points]
        assert "施工组织设计" in names, f"重试成功后应含第一阶段评分点，实际: {names}"
        assert "进度计划保证措施" in names, f"应继续执行第二阶段，实际: {names}"
        assert provider.calls == 3, f"应为 失败1次+重试1次+第二阶段1次，实际 calls={provider.calls}"

    async def test_second_stage_transient_timeout_recovers_on_retry(self):
        """第二阶段瞬时超时 → 重试成功：应保留两阶段合并结果（旧行为：仅第一阶段+1次调用）。"""
        provider = _ScriptedProvider(
            [_FIRST_JSON, TimeoutError("AI调用超时（120秒）"), _REVIEW_JSON]
        )
        points = await extract_scoring_points(provider, "招标文件上下文", "项目摘要")
        names = [p["name"] for p in points]
        assert names == ["施工组织设计", "进度计划保证措施"], names
        assert provider.calls == 3, f"第二阶段应有 失败1次+重试1次，实际 calls={provider.calls}"

    async def test_persistent_timeout_still_degrades_without_hanging(self):
        """持续超时（重试耗尽）：保持原降级语义——第一阶段失败返回 []，不无限重试。"""
        provider = _ScriptedProvider([TimeoutError("AI调用超时（60秒）")])
        points = await extract_scoring_points(provider, "招标文件上下文", "项目摘要")
        assert points == []
        # 有限重试：第一阶段 attempts=2 后降级返回，第二阶段不再发起
        assert provider.calls == 2, f"应重试一次后降级，实际 calls={provider.calls}"

    async def test_second_stage_exhausted_falls_back_to_first_stage(self):
        """第二阶段重试耗尽：降级为仅第一阶段结果（原语义不变）。"""
        provider = _ScriptedProvider([_FIRST_JSON, TimeoutError(), TimeoutError()])
        points = await extract_scoring_points(provider, "招标文件上下文", "项目摘要")
        names = [p["name"] for p in points]
        # 仅剩第一阶段（权重和 60≠100）时 normalize 会插入 [权重异常] 伪项，
        # 属既有语义，不在本用例断言范围——只钉"第一阶段结果保留、第二阶段无产出"。
        assert "施工组织设计" in names, names
        assert "进度计划保证措施" not in names, names
        assert provider.calls == 3, f"第二阶段应有 首次+重试 共2次，实际 calls={provider.calls}"

    async def test_empty_first_stage_response_still_runs_second_stage(self):
        """空响应不是超时：不判失败、不重试，仍继续第二阶段审查（旧语义钉桩）。"""
        provider = _ScriptedProvider(["", _REVIEW_JSON])
        points = await extract_scoring_points(provider, "招标文件上下文", "项目摘要")
        names = [p["name"] for p in points]
        # 第一阶段为空时仅剩权重 40，normalize 会插入 [权重异常] 伪项（既有语义），
        # 本用例只钉"第二阶段照常执行且有产出"。
        assert "进度计划保证措施" in names, names
        assert provider.calls == 2, f"空响应不应触发重试，实际 calls={provider.calls}"


class TestScoringStageTimeoutFollowsConfig:
    """P1-CFG 接线（2026-10-19）：评分点各阶段单次超时预算跟随厂商配置 timeout，
    下限为原硬编码口径（第一阶段 60s / 第二阶段 120s）。

    背景：提供商 HTTP 层按配置 timeout（生产实测 120s，read=1.5x）本就愿意等待，
    业务层裸 60s 会把 59~90s 的慢而成功的响应提前误杀（logs/backend.log 2026-10-19
    21:30 取消在 60s 整、21:32 同请求 59s 成功）。
    """

    def _spy_timeouts(self, monkeypatch):
        captured: list[int] = []
        orig = workflows_analysis.call_ai_text

        async def _spy(provider, messages, **kwargs):
            # 经 kwargs 捕获 timeout，避免形参名触发 ASYNC109 lint
            captured.append(kwargs.get("timeout"))
            return await orig(provider, messages, **kwargs)

        monkeypatch.setattr(workflows_analysis, "call_ai_text", _spy)
        return captured

    async def test_configured_timeout_above_floor_is_used(self, monkeypatch):
        captured = self._spy_timeouts(monkeypatch)
        provider = _ScriptedProvider([_FIRST_JSON, _REVIEW_JSON], timeout=180)
        await extract_scoring_points(provider, "ctx", "summary")
        assert captured == [180, 180], f"两阶段应跟随厂商配置 timeout=180，实际: {captured}"

    async def test_small_configured_timeout_never_breaks_floors(self, monkeypatch):
        captured = self._spy_timeouts(monkeypatch)
        provider = _ScriptedProvider([_FIRST_JSON, _REVIEW_JSON], timeout=30)
        await extract_scoring_points(provider, "ctx", "summary")
        assert captured == [60, 120], f"配置过小时应回退阶段下限 60/120，实际: {captured}"

    async def test_provider_without_timeout_attribute_uses_floors(self, monkeypatch):
        captured = self._spy_timeouts(monkeypatch)
        provider = _ScriptedProvider([_FIRST_JSON, _REVIEW_JSON])  # timeout=0 → 走下限
        await extract_scoring_points(provider, "ctx", "summary")
        assert captured == [60, 120], f"无配置超时下维持原硬编码口径，实际: {captured}"


TENDER_TEXT = (
    "第一章 招标公告\n本项目为市政道路工程，工期：450 天。\n"
    "第二章 评标办法\n评分标准：施工组织设计 60 分，进度计划保证措施 40 分。\n"
)


@pytest.fixture()
def client():
    init_db()
    from app.main import app

    return TestClient(app)


class TestScoringRouteTransientTimeout:
    """路由级契约：/ai/scoring 遇第一阶段瞬时超时应在重试后拿到 200+评分点，
    而不是旧行为的一次超时→降级 []→502「识别失败」。"""

    def test_route_recovers_from_first_stage_timeout(self, client, monkeypatch):
        import app.routers.ai as ai_mod

        # 建项目 + 上传招标文件（走真实文件链，latest_extracted_text 有内容）
        r = client.post("/api/v1/projects", json={"name": "评分重试-路由级", "industry": "市政工程"})
        assert r.status_code == 201, r.text
        pid = r.json()["id"]
        r = client.post(
            f"/api/v1/projects/{pid}/files",
            files={"file": ("招标文件.txt", TENDER_TEXT.encode("utf-8"), "text/plain")},
            data={"file_role": "main"},
        )
        assert r.status_code == 200 and r.json()["status"] == "success", r.text

        provider = _ScriptedProvider(
            [TimeoutError("AI调用超时（60秒）"), _FIRST_JSON, _REVIEW_JSON]
        )

        async def _fake_ctx(text):
            return text

        async def _fake_provider(_cfg):
            return provider, None

        async def _fake_summary(_p, _ctx):
            return "项目概述：市政道路工程，工期450天"

        async def _fake_facts(*_a, **_kw):
            return []

        monkeypatch.setattr(ai_mod, "extract_tender_context_async", _fake_ctx)
        monkeypatch.setattr(ai_mod, "get_working_provider", _fake_provider)
        monkeypatch.setattr(ai_mod, "generate_tender_summary", _fake_summary)
        monkeypatch.setattr(ai_mod, "extract_global_facts", _fake_facts)
        # 注意：**不** patch extract_scoring_points——被测对象正是它的重试行为

        r = client.post(f"/api/v1/projects/{pid}/ai/scoring")
        assert r.status_code == 200, f"瞬时超时经重试应成功，实际: {r.status_code} {r.text}"
        points = json.loads(r.json()["scoring_points_json"])
        names = [p["name"] for p in points]
        assert "施工组织设计" in names and "进度计划保证措施" in names, names
        assert provider.calls == 3, f"应为 失败1次+重试1次+第二阶段1次，实际 calls={provider.calls}"
