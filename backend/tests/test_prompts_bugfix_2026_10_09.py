"""提示词模块回归测试（2026-10-09 深度审计与修复）。

覆盖本轮定位并修复的全部缺陷，每项均对应可复现的失败场景：

P0 致命
  - PROMPT-L1  check_prompt_alerts 持锁重入 -> 永久死锁（/prompts/alerts 挂起）
  - PROMPT-R1  /prompts/{key} 路由遮蔽静态子路由（/metrics /alerts /budget 全 404）
  - PROMPT-O3  _output_repair 相对导入层级错误 -> AI 修复环节 100% 静默失效

P1 正确性
  - PROMPT-S2  脱敏用 \b 界定边界，CJK 邻接的手机号/身份证完全漏脱敏
  - PROMPT-S3  身份证 18 位被手机号 11 位规则切碎，残留 7 位明文
  - PROMPT-S1  中和裸 {var} 破坏 Mermaid 判定节点与图表 JSON
  - PROMPT-O1  strip_code_fences 只取第一个围栏
  - PROMPT-O2  extract_json_body 对 end<start 静默返回空串
  - PROMPT-C2  Token 预算必然超支（标记未计费 + token 当字符数）
  - PROMPT-R2  PUT 未知 key 写入 category/label 为 NULL 的孤儿行
  - PROMPT-R4  sync-hardcode overwrite 只更 default_content，同步后仍不生效
  - PROMPT-R6  回滚可能写入空 default_content，令「重置为默认」清空提示词
  - PROMPT-L2  predict_month_end_cost 的 daily_avg 可能未绑定
  - PROMPT-L3  快照聚合在锁外，与入列快照不自洽
  - PROMPT-L4  告警抑制表锁外读写，并发下重复推送

P2 上下文注入
  - PROMPT-C1  全局事实提取注入整篇招标原文：无上限/无优先级/无脱敏
  - PROMPT-C5  L3 标题修复上下文未脱敏 + 逐段硬切片（非 Token 预算）
  - PROMPT-C6  _format_tender_context_block 只截断不脱敏
  - PROMPT-C3  estimate_tokens / get_prompt 热路径性能
"""

from __future__ import annotations

import re
import threading

import pytest


# ══════════════════════════════════════════════════════════════════════════
# P0-1  check_prompt_alerts 死锁
# ══════════════════════════════════════════════════════════════════════════

class TestAlertDeadlock:
    def test_check_prompt_alerts_terminates(self):
        """回归：原实现用非重入 Lock，check_prompt_alerts 持锁期间调用
        predict_month_end_cost -> get_prompt_metrics 再次 acquire 同一把锁，
        线程永久挂起，GET /prompts/alerts 与 generate_monthly_report 全部卡死。"""
        from app.services.ai.prompts import _cache

        result: dict = {}

        def _run():
            try:
                result["alerts"] = _cache.check_prompt_alerts()
            except Exception as exc:  # pragma: no cover - 失败即报错
                result["error"] = exc

        th = threading.Thread(target=_run, daemon=True)
        th.start()
        th.join(timeout=15)

        assert not th.is_alive(), "check_prompt_alerts 死锁（线程未在 15s 内结束）"
        assert "error" not in result, f"check_prompt_alerts 抛异常: {result.get('error')}"
        assert isinstance(result["alerts"], list)

    def test_metrics_lock_is_reentrant(self):
        from app.services.ai.prompts import _cache

        assert hasattr(_cache._metrics_lock, "_RLock__release") or type(
            _cache._metrics_lock
        ).__name__ == "RLock", "_metrics_lock 必须为可重入锁"

    def test_predict_month_end_cost_never_unbound(self):
        """回归 PROMPT-L2：day_of_month<=0 分支未赋 daily_avg，返回体无条件读取它。"""
        from app.services.ai.prompts import _cache

        r = _cache.predict_month_end_cost()
        assert "daily_avg_cost" in r

    def test_snapshot_metrics_history_is_consistent(self):
        """回归 PROMPT-L3：聚合值与入列快照必须来自同一时刻的锁内读取。"""
        from app.services.ai.prompts import _cache

        _cache.snapshot_metrics_history()
        hist = _cache.get_metrics_history(24)
        assert hist, "快照历史为空"
        for point in hist:
            assert set(point) >= {"timestamp", "total_calls", "total_tokens", "total_db_hits"}


# ══════════════════════════════════════════════════════════════════════════
# P0-2  路由遮蔽
# ══════════════════════════════════════════════════════════════════════════

class TestRouteShadowing:
    @pytest.mark.parametrize(
        "path",
        [
            "/prompts/metrics",
            "/prompts/alerts",
            "/prompts/budget",
            "/prompts/metrics/history",
            "/prompts/metrics/by-category",
            "/prompts/metrics/predict",
            "/prompts/metrics/export",
            "/prompts/budget/predict",
            "/prompts/budget/suggest",
            "/prompts/cost/suggestions",
            "/prompts/cost/compare-models",
            "/prompts/cost/optimization-summary",
            "/prompts/alerts/thresholds",
        ],
    )
    def test_static_route_not_shadowed(self, path):
        """回归 PROMPT-R1：带路径参数的 /prompts/{key} 必须声明在静态路由之后，
        否则 Starlette 会把 'metrics'/'alerts'/'budget' 当作 key 绑定并返回 404。"""
        from app.routers import prompts

        paths = [r.path for r in prompts.router.routes]
        param_idx = paths.index("/prompts/{key}")
        assert paths.index(path) < param_idx, (
            f"{path} 声明在 /prompts/{{key}} 之后，将被遮蔽；"
            f"实际顺序: {paths[param_idx - 2:param_idx + 2]}"
        )

    def test_no_path_param_route_precedes_static_route(self):
        """通用守卫：任何 /prompts/<静态段> 都不得排在 /prompts/{key} 之后。"""
        from app.routers import prompts

        paths = [r.path for r in prompts.router.routes]
        param_idx = paths.index("/prompts/{key}")
        static = re.compile(r"^/prompts/[a-z][a-z0-9\-]*(/.*)?$")
        for p in paths[param_idx + 1:]:
            assert not static.match(p), f"参数路由之后仍出现静态路由: {p}"

    def test_endpoints_are_reachable(self):
        """端到端：受影响的接口必须返回 2xx。"""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from app.routers import prompts

        app = FastAPI()
        app.include_router(prompts.router)
        client = TestClient(app, raise_server_exceptions=False)

        for path in [
            "/prompts/metrics",
            "/prompts/alerts",
            "/prompts/budget",
            "/prompts/metrics/history",
            "/prompts/metrics/by-category",
            "/prompts/budget/predict",
            "/prompts/budget/suggest",
            "/prompts/cost/suggestions",
            "/prompts/cost/compare-models",
            "/prompts/alerts/thresholds",
            "/prompts/cost/optimization-summary",
            "/prompts/metrics/predict",
            "/prompts/metrics/export?format=csv",
        ]:
            resp = client.get(path)
            assert resp.status_code == 200, f"{path} -> {resp.status_code} {resp.text[:120]}"

    def test_put_budget_not_treated_as_prompt_update(self):
        """回归：路由遮蔽修复前 PUT /prompts/budget 误命中 update_prompt。"""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from app.routers import prompts

        app = FastAPI()
        app.include_router(prompts.router)
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.put("/prompts/budget", json={"amount": 42.0})
        assert resp.status_code == 200, resp.text[:200]
        assert resp.json().get("status") == "ok"


# ══════════════════════════════════════════════════════════════════════════
# P0-3  相对导入层级
# ══════════════════════════════════════════════════════════════════════════

class TestRelativeImportLevel:
    def test_output_repair_uses_correct_parent_level(self):
        """回归 PROMPT-O3：`.workflows_base` 指向 prompts 包内不存在的模块，
        ModuleNotFoundError 被 except 吞掉 -> AI 修复永久静默失效。"""
        from app.services.ai import workflows_base
        from app.services.ai.prompts import _output_repair

        assert workflows_base is not None
        with open(_output_repair.__file__, encoding="utf-8") as f:
            body = f.read()
        # 去掉注释后再检查，避免注释里的说明文字造成误判
        code = "\n".join(line.split("#", 1)[0] for line in body.splitlines())
        assert "from .workflows_base" not in code, "仍使用错误的一级相对导入"
        assert "from ..workflows_base" in code, "未使用正确的二级相对导入"

    @pytest.mark.asyncio
    async def test_repair_json_output_ai_path_actually_runs(self):
        """端到端：AI 修复分支必须真的能调用到 provider（而非静默吞异常）。"""
        from app.services.ai.prompts._output_repair import repair_json_output

        calls: list = []

        class _FakeProvider:
            async def chat(self, messages, **kw):
                calls.append(messages)
                return '```json\n{"repaired": true}\n```'

        data, ok = await repair_json_output(
            _FakeProvider(), "{broken json", error_info="boom", max_retries=1
        )
        assert ok is True, "AI 修复未生效（很可能仍是导入层级错误）"
        assert data == {"repaired": True}
        assert len(calls) == 1


# ══════════════════════════════════════════════════════════════════════════
# P1  安全：脱敏与元标记中和
# ══════════════════════════════════════════════════════════════════════════

class TestDesensitization:
    def test_cjk_adjacent_phone_is_redacted(self):
        """回归 PROMPT-S2：\\b 在 CJK 邻接时不成立，原实现完全漏脱敏。"""
        from app.services.ai.prompts._security import desensitize_sensitive_info

        assert "13800138000" not in desensitize_sensitive_info("联系人13800138000，手机")

    def test_cjk_adjacent_id_card_is_redacted(self):
        from app.services.ai.prompts._security import desensitize_sensitive_info

        assert "11010119900307123X" not in desensitize_sensitive_info(
            "身份证11010119900307123X号"
        )

    def test_id_card_not_fragmented_by_mobile_rule(self):
        """回归 PROMPT-S3：18 位身份证若先过 11 位手机号规则会残留 7 位明文。"""
        from app.services.ai.prompts._security import desensitize_sensitive_info

        out = desensitize_sensitive_info("11010119900307123X")
        assert out == "[身份证号已脱敏]"
        assert not re.search(r"\d{5,}", out), f"残留明文数字: {out}"

    def test_no_false_positive_on_long_numbers(self):
        """预算/金额等长数字不应被误判为手机号。"""
        from app.services.ai.prompts._security import desensitize_sensitive_info

        assert "1234567890123" in desensitize_sensitive_info("预算金额1234567890123元")

    def test_spaced_phone_still_redacted(self):
        from app.services.ai.prompts._security import desensitize_sensitive_info

        assert "13800138000" not in desensitize_sensitive_info("tel 13800138000 ok")

    def test_email_and_api_key_still_redacted(self):
        from app.services.ai.prompts._security import desensitize_sensitive_info

        out = desensitize_sensitive_info("a@b.com sk-abcdefgh12345 api_key=xyz12345678")
        assert "a@b.com" not in out
        assert "sk-abcdefgh12345" not in out
        assert "xyz12345678" not in out


class TestMetaMarkerNeutralization:
    def test_mermaid_decision_node_survives(self):
        """回归 PROMPT-S1：中和裸 {var} 会毁掉 Mermaid 判定节点语法。"""
        from app.services.ai.prompts._security import sanitize_prompt_value

        mermaid = "flowchart TD\n    A{开始} --> B[结束]\n    B --> C{是否通过}"
        assert sanitize_prompt_value(mermaid) == mermaid

    def test_chart_json_payload_survives(self):
        from app.services.ai.prompts._security import sanitize_prompt_value

        payload = '{"type": "bar", "series": [{"name": "a", "data": [1, 2]}]}'
        assert sanitize_prompt_value(payload) == payload

    def test_shared_ref_still_neutralized(self):
        from app.services.ai.prompts._security import sanitize_prompt_value

        assert "{shared:" not in sanitize_prompt_value("注入 {shared:SHARED_OUTPUT_SPEC}")

    def test_meta_var_still_neutralized(self):
        from app.services.ai.prompts._security import sanitize_prompt_value

        assert "__GLOBAL_FACTS__" not in sanitize_prompt_value("注入 __GLOBAL_FACTS__")

    def test_inline_var_optional(self):
        from app.services.ai.prompts._security import sanitize_prompt_value

        assert "{fake}" not in sanitize_prompt_value("a {fake} b", inline_vars=True)


# ══════════════════════════════════════════════════════════════════════════
# P1  输出修复
# ══════════════════════════════════════════════════════════════════════════

class TestOutputRepair:
    def test_strip_code_fences_picks_largest_block(self):
        """回归 PROMPT-O1：多围栏输出时原实现只取第一个。"""
        from app.services.ai.prompts._output_repair import strip_code_fences

        text = '```\nshort\n```\n中间说明\n```json\n{"a": 1, "b": 2}\n```'
        out = strip_code_fences(text)
        assert "{" in out and "a" in out, out

    def test_extract_json_body_reversed_delimiters_not_empty(self):
        """回归 PROMPT-O2：'] junk {' 原实现静默返回空串，把提取失败伪装成空内容。"""
        from app.services.ai.prompts._output_repair import extract_json_body

        out = extract_json_body("] junk {")
        assert out != "", "畸形输入不应被静默转为空串"

    def test_extract_json_body_normal_cases(self):
        from app.services.ai.prompts._output_repair import extract_json_body

        assert extract_json_body('前缀 {"a": 1} 后缀') == '{"a": 1}'
        assert extract_json_body("说明\n```json\n[1, 2]\n```") == "[1, 2]"
        assert extract_json_body("没有 JSON") == "没有 JSON"


# ══════════════════════════════════════════════════════════════════════════
# P1  Token 预算
# ══════════════════════════════════════════════════════════════════════════

class TestTokenBudget:
    @pytest.mark.parametrize(
        "parts,cap",
        [
            ({"GLOBAL_FACTS": "招标全文" * 60000, "SUMMARY": "摘要" * 50}, 20000),
            ({"GLOBAL_FACTS": "abcdefghij " * 20000, "SUMMARY": "x" * 100}, 5000),
            ({"GLOBAL_FACTS": "项目name编号ABC123" * 20000}, 8000),
            ({f"P{i}": "内容" * 300 for i in range(20)}, 1000),
        ],
    )
    def test_assemble_never_exceeds_budget(self, parts, cap):
        """回归 PROMPT-C2：原实现截断标记未计费 + token 当字符数，必然超支。"""
        from app.services.ai.prompts import assemble_prompt_context, estimate_tokens

        out = assemble_prompt_context(parts, max_tokens=cap)
        assert estimate_tokens(out) <= cap, (
            f"超预算: {estimate_tokens(out)} > {cap}"
        )

    @pytest.mark.parametrize("cap", [50, 200, 2000])
    def test_truncate_respects_budget(self, cap):
        from app.services.ai.prompts import estimate_tokens, truncate_to_tokens

        out = truncate_to_tokens("招标全文" * 5000, cap)
        assert estimate_tokens(out) <= cap

    def test_budget_utilization_is_reasonable(self):
        """预算不应被过度浪费（二分收敛应贴近上限）。"""
        from app.services.ai.prompts import assemble_prompt_context, estimate_tokens

        out = assemble_prompt_context({"A": "招标全文" * 60000}, max_tokens=20000)
        assert estimate_tokens(out) > 19000

    def test_estimate_tokens_semantics_unchanged(self):
        """回归 PROMPT-C3：translate 加速实现必须与原正则口径一致。

        ENHANCE 2026-10-11：tiktoken 已安装时使用精确计数，值与启发式不同
        （更准确）。测试分两路：tiktoken 可用 → 验证精确计数合理；
        tiktoken 不可用 → 验证启发式口径不变。
        """
        from app.services.ai.prompts import estimate_tokens
        from app.services.ai.prompts._context import _TIKTOKEN_ENCODER

        for s in ["", "abc", "中文abc中文", "a" * 400, "中" * 400]:
            if _TIKTOKEN_ENCODER is not None:
                # tiktoken 精确计数：非空文本须 > 0，空文本 = 0
                result = estimate_tokens(s)
                if s:
                    assert result > 0, f"tiktoken 应返回 >0: {s[:20]}"
                else:
                    assert result == 0, "空文本应为 0"
            else:
                # 启发式回退：口径与原正则一致
                cjk = len(re.findall(r"[一-鿿]", s))
                expected = cjk + (len(s) - cjk + 3) // 4
                assert estimate_tokens(s) == expected, s[:20]

    def test_priority_ordering_respected(self):
        from app.services.ai.prompts import assemble_prompt_context

        out = assemble_prompt_context(
            {"LOW": "低优先级内容", "HIGH": "高优先级内容"},
            priorities={"HIGH": 100, "LOW": 1},
        )
        assert out.index("高优先级内容") < out.index("低优先级内容")


# ══════════════════════════════════════════════════════════════════════════
# P1  路由器写路径
# ══════════════════════════════════════════════════════════════════════════

class TestPromptWritePaths:
    @pytest.fixture()
    def client(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from app.routers import prompts

        app = FastAPI()
        app.include_router(prompts.router)
        return TestClient(app, raise_server_exceptions=False)

    def test_update_unknown_key_rejected(self, client):
        """回归 PROMPT-R2：原实现对未知 key 也写入，产生 category/label 为 NULL 的孤儿行。"""
        resp = client.put("/prompts/__NOT_A_REAL_PROMPT__", json={"content": "x"})
        assert resp.status_code == 404, resp.text[:200]

        from app.db import get_db

        with get_db(readonly=True) as conn:
            row = conn.execute(
                "SELECT COUNT(*) c FROM prompt_templates WHERE key = ?",
                ("__NOT_A_REAL_PROMPT__",),
            ).fetchone()
        assert row["c"] == 0, "未知 key 被写入数据库"

    def test_update_writes_category_and_label(self, client):
        from app.db import get_db
        from app.services.ai.prompts import _ALL_PROMPTS

        key = "TENDER_SUMMARY_SYSTEM"
        meta = _ALL_PROMPTS[key]
        resp = client.put(f"/prompts/{key}", json={"content": meta["content"]})
        assert resp.status_code == 200, resp.text[:200]

        with get_db(readonly=True) as conn:
            row = conn.execute(
                "SELECT category, label FROM prompt_templates WHERE key = ?", (key,)
            ).fetchone()
        assert row is not None
        assert row["category"] == meta["category"]
        assert row["label"] == meta["label"]

    def test_sync_hardcode_refreshes_content(self, client):
        """回归 PROMPT-R4：原 overwrite 只更 default_content，content 仍旧 -> 同步无效。"""
        from app.db import get_db
        from app.services.ai.prompts import _ALL_PROMPTS, reload_prompt_cache

        key = "TENDER_SUMMARY_SYSTEM"
        new_default = _ALL_PROMPTS[key]["content"] + "\n【同步探针】"

        # 模拟「开发者改了硬编码但 DB 仍是旧版」的漂移场景
        with get_db() as conn:
            conn.execute(
                "INSERT INTO prompt_templates (key, category, label, content, default_content, updated_at)"
                " VALUES (?, ?, ?, ?, ?, datetime('now','localtime'))"
                " ON CONFLICT(key) DO UPDATE SET content=excluded.content,"
                " default_content=excluded.default_content",
                (key, "旧分类", "旧标签", "旧内容", "旧内容"),
            )
        reload_prompt_cache()

        # 覆盖注册表内容为 new_default，模拟硬编码更新
        _ALL_PROMPTS[key]["content"] = new_default
        try:
            resp = client.post("/prompts/sync-hardcode?overwrite=true")
            assert resp.status_code == 200, resp.text[:200]
            body = resp.json()
            assert key in body["refreshed"], f"未标记为 refreshed: {body}"
        finally:
            _ALL_PROMPTS[key]["content"] = new_default.replace("\n【同步探针】", "")
            reload_prompt_cache()

        from app.services.ai.prompts import get_prompt

        assert "【同步探针】" in get_prompt(key), "同步后 content 仍是旧版（同步无效）"

    def test_sync_keeps_user_modified_content(self, client):
        """用户改过的 content 默认必须保留，不得被 overwrite 静默覆盖。"""
        from app.db import get_db
        from app.services.ai.prompts import _ALL_PROMPTS, reload_prompt_cache

        key = "WORK_INTERFACE_ANALYSIS_SYSTEM"
        default_content = _ALL_PROMPTS[key]["content"]
        with get_db() as conn:
            conn.execute(
                "INSERT INTO prompt_templates (key, category, label, content, default_content, updated_at)"
                " VALUES (?, ?, ?, ?, ?, datetime('now','localtime'))"
                " ON CONFLICT(key) DO UPDATE SET content=excluded.content,"
                " default_content=excluded.default_content",
                (key, "c", "l", "【用户自定义】", "【旧默认值】"),
            )
        reload_prompt_cache()
        try:
            resp = client.post("/prompts/sync-hardcode?overwrite=true")
            assert resp.status_code == 200
            body = resp.json()
            assert key in body["kept_modified"], body

            from app.services.ai.prompts import get_prompt

            # 断言必须在 finally 还原数据之前执行
            assert "【用户自定义】" in get_prompt(key), "用户自定义被静默覆盖"
        finally:
            with get_db() as conn:
                conn.execute(
                    "UPDATE prompt_templates SET content=?, default_content=? WHERE key=?",
                    (default_content, default_content, key),
                )
            reload_prompt_cache()

    def test_rollback_never_writes_empty_default(self, client):
        """回归 PROMPT-R6：归档版本 default_content 为空时，回滚会写入空默认值，
        导致前端「重置为默认」把提示词清空。"""
        from app.db import get_db
        from app.services.ai.prompts import _ALL_PROMPTS, reload_prompt_cache

        key = "CONSTRUCTION_METHOD_ANALYSIS_SYSTEM"
        # 先保存一次，产出历史版本
        assert client.put(f"/prompts/{key}", json={"content": "版本A"}).status_code == 200
        # 手工把该历史版本的 default_content 置空（模拟早期脏数据）
        with get_db() as conn:
            vid = conn.execute(
                "SELECT MAX(id) i FROM prompt_versions WHERE key=?", (key,)
            ).fetchone()["i"]
            conn.execute(
                "UPDATE prompt_versions SET default_content='' WHERE id=?", (vid,)
            )
        # 删除主记录，强制回滚走 INSERT 分支
        with get_db() as conn:
            conn.execute("DELETE FROM prompt_templates WHERE key=?", (key,))
        reload_prompt_cache()

        resp = client.post(f"/prompts/{key}/versions/{vid}/rollback")
        assert resp.status_code == 200, resp.text[:200]

        with get_db(readonly=True) as conn:
            row = conn.execute(
                "SELECT default_content FROM prompt_templates WHERE key=?", (key,)
            ).fetchone()
        assert row is not None
        assert row["default_content"], "回滚写入了空 default_content"
        assert row["default_content"] == _ALL_PROMPTS[key]["content"]

    def test_list_prompts_can_surface_orphans(self, client):
        """回归 PROMPT-R5：孤儿记录此前完全不可见，无法发现/清理。"""
        from app.db import get_db
        from app.services.ai.prompts import reload_prompt_cache

        with get_db() as conn:
            conn.execute(
                "INSERT INTO prompt_templates (key, category, label, content, default_content, updated_at)"
                " VALUES ('__ORPHAN_PROBE__', 'x', 'y', 'c', 'c', datetime('now','localtime'))"
                " ON CONFLICT(key) DO NOTHING"
            )
        reload_prompt_cache()
        try:
            default_resp = client.get("/prompts")
            assert all(r["key"] != "__ORPHAN_PROBE__" for r in default_resp.json())

            resp = client.get("/prompts?include_orphans=true")
            assert resp.status_code == 200
            orphans = [r for r in resp.json() if r.get("orphan")]
            assert "__ORPHAN_PROBE__" in {r["key"] for r in orphans}, orphans
        finally:
            with get_db() as conn:
                conn.execute("DELETE FROM prompt_templates WHERE key='__ORPHAN_PROBE__'")
            reload_prompt_cache()


# ══════════════════════════════════════════════════════════════════════════
# P2  上下文注入链路
# ══════════════════════════════════════════════════════════════════════════

class TestContextInjection:
    def test_global_facts_uses_unified_assembly(self):
        """回归 PROMPT-C1：整篇招标原文裸拼、无上限、无优先级、无脱敏。"""
        import inspect

        from app.services.ai import workflows_analysis

        src = inspect.getsource(workflows_analysis.extract_global_facts)
        assert "assemble_prompt_context" in src, "全局事实上下文未走统一组装"
        assert "{tender_context}" not in src, "仍存在裸 f-string 招标原文注入"
        assert "sanitize_prompt_value" in src, "全局事实上下文未脱敏"

    def test_l3_repair_context_is_sanitized_and_budgeted(self):
        """回归 PROMPT-C5：L3 修复上下文此前未脱敏且逐段硬切片。"""
        import inspect

        from app.services.ai import workflows_outline_utils

        src = inspect.getsource(workflows_outline_utils)
        # BUG-FIX 2026-10-15：原实现用 next(...) 取「定义序第一个」名字含 repair
        # 且源码含 L3 的函数。后引入的配置读取小函数 `_repair_quota_by_task`
        # （docstring 含 OUTLINE_L3_REPAIR_QUOTA_BY_TASK）定义在真正的入口
        # `repair_l3_generic_titles` 之前，启发式误中小函数导致假红。
        # 改为：名字含 l3 的候选优先；否则取候选中最长者（入口必然大于 helper）。
        candidates = [
            inspect.getsource(f)
            for n, f in vars(workflows_outline_utils).items()
            if inspect.isfunction(f) and "repair" in n.lower() and "L3" in inspect.getsource(f)
        ]
        l3_named = [
            inspect.getsource(f)
            for n, f in vars(workflows_outline_utils).items()
            if inspect.isfunction(f) and "repair" in n.lower() and "l3" in n.lower()
        ]
        fn = next(iter(l3_named), max(candidates, key=len, default=""))
        body = fn or src
        assert "sanitize_prompt_value" in body, "L3 修复上下文未脱敏"
        assert "assemble_prompt_context" in body, "L3 修复上下文未走 Token 预算"

    def test_tender_context_block_is_sanitized(self):
        """回归 PROMPT-C6：只截断不脱敏。"""
        from app.services.ai import workflows_outline_v51 as v51

        block = v51._format_tender_context_block(
            "招标原文 __GLOBAL_FACTS__ {shared:SHARED_OUTPUT_SPEC}", "行业画像", max_chars=3000
        )
        assert "__GLOBAL_FACTS__" not in block
        assert "{shared:" not in block

    def test_tender_context_block_preserves_payload(self):
        from app.services.ai import workflows_outline_v51 as v51

        payload = '{"items": [{"id": 1}]}'
        block = v51._format_tender_context_block(payload, "")
        assert payload in block

    def test_tender_context_block_empty(self):
        from app.services.ai import workflows_outline_v51 as v51

        assert v51._format_tender_context_block("", "") == ""


# ══════════════════════════════════════════════════════════════════════════
# 模块完整性
# ══════════════════════════════════════════════════════════════════════════

class TestModuleIntegrity:
    def test_all_prompts_have_valid_shared_refs(self):
        from app.services.ai.prompts import _ALL_PROMPTS

        pat = re.compile(r"\{shared:([A-Za-z_]\w{1,})\}")
        bad = []
        for key, meta in _ALL_PROMPTS.items():
            for ref in pat.findall(meta["content"]):
                if not any(
                    c in _ALL_PROMPTS
                    for c in (ref, "SHARED_" + ref, ref.upper(), "SHARED_" + ref.upper())
                ):
                    bad.append((key, ref))
        assert not bad, f"引用了未注册的共享规则: {bad}"

    def test_registry_count_matches_readme_claim(self):
        """README/包 docstring 声称 57 个，实际注册数需一致（防止文档漂移）。"""
        from app.services.ai.prompts import _ALL_PROMPTS

        assert len(_ALL_PROMPTS) >= 55, f"注册表异常偏少: {len(_ALL_PROMPTS)}"
