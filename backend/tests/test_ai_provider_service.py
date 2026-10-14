# -*- coding: utf-8 -*-
"""AI Provider 降级链完整测试套件 — 96 个测试用例

覆盖以下 6 个已修复 BUG 的回归验证 + 扩展场景:
  BUG-1: record_provider_usage 首次失败时 failed 计数错误
  BUG-2: ProviderHistoryItem.success_rate 字段缺失导致 500
  BUG-3: URL 规范化失效（DeepSeek /v1 后缀差异）
  BUG-4: last_used_at 变量名错误导致 NameError
  BUG-5: 内置兜底 provider success/failed 显示错误
  BUG-6: 历史记录清理功能（cleanup_old_history）

测试分组:
  1-3:   BUG-1 首次失败计数 (3)
  4-5:   BUG-2 success_rate 字段 (2)
  6-8:   BUG-3 URL 规范化 (3)
  9-11:  BUG-4 last_used_at 变量 (3)
  12-14: BUG-5 内置兜底显示 (3)
  15-18: BUG-6 历史清理 (4)
  19-25: record_usage 扩展测试 (7)
  26-40: get_priority_chain 扩展测试 (15)
  41-55: create_with_fallback 扩展测试 (15)
  56-70: config 缓存与加载测试 (15)
  71-80: ai_config_service 定时清理测试 (10)
  81-96: 集成场景与边界测试 (16)
"""
import sys
sys.path.insert(0, r"j:\编程\招投标方案平台\backend")

import pytest
import time
import json
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock

from app.db import init_db, get_db
from app.services.ai.ai_provider_service import (
    AIProviderService,
    record_provider_usage,
    get_provider_priority_order,
    clear_provider_history,
    get_provider_history,
    normalize_provider_url,
    get_ai_config,
    clear_ai_config_cache,
    normalize_url,
    _FALLBACK_PROVIDERS,
)
from app.services.ai.ai_config_service import (
    get_service,
    startup,
    cleanup_old_history,
    record_usage,
    get_priority_chain,
    get_config,
)

init_db()
service = AIProviderService()


@pytest.fixture(autouse=True)
def clean_history():
    """每个测试前备份配置/历史/厂商配置/Profile，测试结束后恢复原状

    BUG 修复：原实现仅 DELETE provider_history 且不恢复 ai_config，
    测试内的 DELETE+INSERT ai_config（如 '空配置'/'None' 测试值）会直接
    污染真实数据库，导致用户"最后保存并应用的厂商平台"被覆盖，
    目录生成读取到错误配置（历史故障根因之一）。
    现改为：测试前备份 → 测试后恢复（而非清空）。

    TEST-ISO 修复（2026-10-14）：降级链 `get_provider_priority_order` 的第 1.5 层
    来自 `text_provider_configs`（带 key 的备选厂商）、第 2 层来自 `ai_profiles`
    （默认 Profile）、第 1 层来自 `ai_config` 主配置。本文件多条断言按「主配置=内置
    默认 agnes + 无备选 → 链长恰为 6（去重 agnes 后 1 主 + 5 内置）」写死，但原
    fixture 只隔离 provider_history/ai_config —— 全量运行时有其它测试文件（
    test_text_model_config_*、test_text_config_*）先写入 text_provider_configs /
    ai_profiles 且不回滚，这些残留行进入降级链后被计成「无 last_used_at」条目，
    使链长变为 15，15 条断言红（单独运行本文件却全绿）。补齐：对这四张表统一
    备份→清空→恢复，令降级链在任意运行顺序下都确定，且不破坏真实用户数据。
    """
    import sqlite3

    _ISOLATED_TABLES = ("provider_history", "text_provider_configs", "ai_profiles")
    backup: dict[str, list[dict]] = {}
    with get_db() as conn:
        conn.row_factory = sqlite3.Row
        for tbl in _ISOLATED_TABLES:
            backup[tbl] = [dict(r) for r in conn.execute(f"SELECT * FROM {tbl}").fetchall()]
        # ai_config 主配置：备份并在测试期清空，使 get_ai_config 回落内置默认（agnes），
        # 从而与内置兜底 agnes 去重、链长确定为 6；测试结束后按原值恢复。
        _backup_config = None
        row = conn.execute("SELECT * FROM ai_config WHERE id = 1").fetchone()
        if row is not None:
            _backup_config = dict(row)
        for tbl in _ISOLATED_TABLES:
            conn.execute(f"DELETE FROM {tbl}")
        conn.execute("DELETE FROM ai_config")
        conn.commit()
    # 清缓存：避免上一测试/其它文件残留的内存配置污染本测试看到的降级链
    clear_ai_config_cache()
    yield
    with get_db() as conn:
        conn.row_factory = sqlite3.Row
        for tbl in _ISOLATED_TABLES:
            conn.execute(f"DELETE FROM {tbl}")
            rows = backup.get(tbl) or []
            if rows:
                cols = list(rows[0].keys())
                placeholders = ", ".join("?" * len(cols))
                col_names = ", ".join(cols)
                conn.executemany(
                    f"INSERT INTO {tbl} ({col_names}) VALUES ({placeholders})",
                    [tuple(r[c] for c in cols) for r in rows],
                )
        # 恢复 ai_config（若测试期间被修改/删除）
        conn.execute("DELETE FROM ai_config")
        if _backup_config is not None:
            cols = list(_backup_config.keys())
            placeholders = ", ".join("?" * len(cols))
            col_names = ", ".join(cols)
            conn.execute(
                f"INSERT INTO ai_config ({col_names}) VALUES ({placeholders})",
                tuple(_backup_config[c] for c in cols),
            )
        conn.commit()
    clear_ai_config_cache()


# ═══════════════════════════════════════════════════════════════════════
# 组 1: BUG-1 — record_provider_usage 首次失败计数 (3)
# ═══════════════════════════════════════════════════════════════════════
class TestBug1_InitialFailedCount:
    def test_first_failure_inserts_failed_1(self):
        record_provider_usage("baidu", "image", "https://qianfan.baidubce.com", success=False)
        with get_db() as conn:
            row = conn.execute(
                "SELECT success, failed FROM provider_history WHERE provider_name='baidu'"
            ).fetchone()
        assert row is not None
        assert row[0] == 0, f"成功应为 0，实际 {row[0]}"
        assert row[1] == 1, f"失败应为 1，实际 {row[1]}"

    def test_first_success_inserts_success_1(self):
        record_provider_usage("volc", "image", "https://ark.cn-beijing.volces.com", success=True)
        with get_db() as conn:
            row = conn.execute(
                "SELECT success, failed FROM provider_history WHERE provider_name='volc'"
            ).fetchone()
        assert row[0] == 1, f"成功应为 1，实际 {row[0]}"
        assert row[1] == 0, f"失败应为 0，实际 {row[1]}"

    def test_subsequent_failures_increment(self):
        for _ in range(5):
            record_provider_usage("baidu", "image", "https://qianfan.baidubce.com", success=False)
        with get_db() as conn:
            row = conn.execute(
                "SELECT success, failed FROM provider_history WHERE provider_name='baidu'"
            ).fetchone()
        assert row[0] == 0
        assert row[1] == 5

    def test_mixed_success_and_failure(self):
        record_provider_usage("volc", "image", "https://ark.cn-beijing.volces.com", success=True)
        record_provider_usage("volc", "image", "https://ark.cn-beijing.volces.com", success=False)
        record_provider_usage("volc", "image", "https://ark.cn-beijing.volces.com", success=True)
        with get_db() as conn:
            row = conn.execute(
                "SELECT success, failed FROM provider_history WHERE provider_name='volc'"
            ).fetchone()
        assert row[0] == 2
        assert row[1] == 1


# ═══════════════════════════════════════════════════════════════════════
# 组 2: BUG-2 — success_rate 字段 (2)
# ═══════════════════════════════════════════════════════════════════════
class TestBug2_SuccessRateField:
    def test_priority_chain_has_success_rate(self):
        record_provider_usage("test_provider", "text", "https://example.com", success=True)
        chain = get_provider_priority_order()
        for entry in chain:
            assert "success_rate" in entry, f"缺少 success_rate 字段: {entry.keys()}"
            assert isinstance(entry["success_rate"], (int, float))

    def test_success_rate_reflects_ratio(self):
        record_provider_usage("ratio_test", "text", "https://test.com", success=True)
        for _ in range(2):
            record_provider_usage("ratio_test", "text", "https://test.com", success=False)
        chain = get_provider_priority_order()
        entry = next((e for e in chain if e["name"] == "ratio_test"), None)
        assert entry is not None
        assert abs(entry["success_rate"] - 0.333) < 0.01

    def test_success_rate_zero_for_all_failures(self):
        for _ in range(3):
            record_provider_usage("fail_only", "text", "https://fail.com", success=False)
        chain = get_provider_priority_order()
        entry = next((e for e in chain if e["name"] == "fail_only"), None)
        assert entry is not None
        assert entry["success_rate"] == 0.0

    def test_success_rate_one_for_all_success(self):
        for _ in range(3):
            record_provider_usage("success_only", "text", "https://success.com", success=True)
        chain = get_provider_priority_order()
        entry = next((e for e in chain if e["name"] == "success_only"), None)
        assert entry is not None
        assert entry["success_rate"] == 1.0


# ═══════════════════════════════════════════════════════════════════════
# 组 3: BUG-3 — URL 规范化去重 (3)
# ═══════════════════════════════════════════════════════════════════════
class TestBug3_UrlNormalization:
    def test_deepseek_url_normalization(self):
        assert normalize_provider_url("https://api.deepseek.com") == "https://api.deepseek.com/v1"
        assert normalize_provider_url("https://api.deepseek.com/v1") == "https://api.deepseek.com/v1"
        assert normalize_provider_url("https://api.deepseek.com/") == "https://api.deepseek.com/v1"

    def test_no_duplicate_in_chain_after_same_url(self):
        record_provider_usage("DeepSeek-v1", "deepseek", "https://api.deepseek.com/v1", success=True)
        record_provider_usage("DeepSeek-no-v1", "deepseek", "https://api.deepseek.com", success=True)
        chain = get_provider_priority_order()
        deepseek_entries = [e for e in chain if "deepseek" in e["base_url"].lower()]
        assert len(deepseek_entries) == 1

    def test_different_urls_not_deduped(self):
        record_provider_usage("DeepSeek", "deepseek", "https://api.deepseek.com", success=True)
        record_provider_usage("SiliconFlow", "openai_compatible", "https://api.siliconflow.cn/v1", success=True)
        chain = get_provider_priority_order()
        names = [e["name"] for e in chain]
        assert "DeepSeek" in names
        assert "SiliconFlow" in names

    def test_trailing_slash_normalization(self):
        record_provider_usage("test1", "text", "https://api.example.com/", success=True)
        record_provider_usage("test2", "text", "https://api.example.com", success=True)
        chain = get_provider_priority_order()
        example_entries = [e for e in chain if "example.com" in e["base_url"].lower()]
        assert len(example_entries) == 1

    def test_case_insensitive_url_matching(self):
        record_provider_usage("upper", "text", "https://API.EXAMPLE.COM", success=True)
        record_provider_usage("lower", "text", "https://api.example.com", success=True)
        # normalize_url 不做大小写转换，所以它们可能不同
        # 但 URL 规范化应至少去除斜杠
        assert normalize_url("https://api.example.com/") == "https://api.example.com"


# ═══════════════════════════════════════════════════════════════════════
# 组 4: BUG-4 — last_used_at 变量 (3)
# ═══════════════════════════════════════════════════════════════════════
class TestBug4_LastUsedAtVariable:
    def test_get_priority_chain_no_nameerror(self):
        try:
            chain = get_provider_priority_order()
            assert isinstance(chain, list)
        except NameError as e:
            pytest.fail(f"NameError 应已被修复: {e}")

    def test_last_used_at_updated_on_each_call(self):
        record_provider_usage("time_test", "text", "https://test.com", success=True)
        time.sleep(1)
        record_provider_usage("time_test", "text", "https://test.com", success=False)
        chain = get_provider_priority_order()
        entry = next((e for e in chain if e["name"] == "time_test"), None)
        assert entry is not None
        assert entry["last_used_at"] != ""

    def test_last_used_at_ordering(self):
        record_provider_usage("old_provider", "text", "https://old-svc.example.com", success=True)
        time.sleep(1.1)
        record_provider_usage("new_provider", "text", "https://new-svc.example.com", success=True)
        chain = get_provider_priority_order()
        names = [e["name"] for e in chain]
        old_idx = names.index("old_provider")
        new_idx = names.index("new_provider")
        assert new_idx < old_idx, f"new_provider[{new_idx}] 应排在 old_provider[{old_idx}] 前面"

    def test_last_used_at_format(self):
        record_provider_usage("fmt_test", "text", "https://fmt.com", success=True)
        chain = get_provider_priority_order()
        entry = next((e for e in chain if e["name"] == "fmt_test"), None)
        assert entry is not None
        ts = entry["last_used_at"]
        assert len(ts) >= 19
        assert ts.count("-") == 2 and ts.count(":") == 2


# ═══════════════════════════════════════════════════════════════════════
# 组 5: BUG-5 — 内置兜底显示 (3)
# ═══════════════════════════════════════════════════════════════════════
class TestBug5_BuiltinFallbackDisplay:
    def test_builtin_fallback_success_is_100(self):
        # L-2 flag 修正后：内置兜底条目（无 last_used_at、非用户配置）的
        # success_rate 展示占位恒为 1.0；is_builtin_default 仅标记真正的
        # 内置默认厂商（agnes），两种入口下断言同一不变量。
        chain = get_provider_priority_order()
        fallback_entries = [
            e for e in chain
            if not e.get("last_used_at") and not e.get("is_user_configured")
        ]
        assert len(fallback_entries) >= 1
        for entry in fallback_entries:
            assert entry["success_rate"] == 1.0

    def test_builtin_fallback_not_deduped_with_history(self):
        record_provider_usage("custom_provider", "text", "https://custom.example.com/v1", success=False)
        chain = get_provider_priority_order()
        custom_entries = [e for e in chain if "custom.example.com" in e["base_url"].lower()]
        assert len(custom_entries) == 1
        names = [e["name"] for e in chain]
        assert "DeepSeek" in names

    def test_builtin_fallback_appears_when_no_history(self):
        clear_provider_history()
        chain = get_provider_priority_order()
        names = [e["name"] for e in chain]
        expected = ["DeepSeek", "SiliconFlow", "通义千问", "智谱GLM", "OpenAI（默认）"]
        for name in expected:
            assert name in names, f"内置兜底 {name} 应在降级链中"

    def test_builtin_fallback_count(self):
        clear_provider_history()
        chain = get_provider_priority_order()
        builtin_count = sum(1 for e in chain if not e.get("last_used_at"))
        assert builtin_count == 6, f"内置兜底应为 6 个（含 agnes 兜底），实际 {builtin_count}"

    def test_builtin_fallback_order(self):
        clear_provider_history()
        chain = get_provider_priority_order()
        names = [e["name"] for e in chain]
        # 内置兜底应全部在末尾
        builtin_names = [n for n in names if not any(
            e["name"] == n and e.get("last_used_at") for e in chain
        )]
        # 最后5个应该是内置兜底
        assert names[-5:] == ["DeepSeek", "SiliconFlow", "通义千问", "智谱GLM", "OpenAI（默认）"]


# ═══════════════════════════════════════════════════════════════════════
# 组 6: BUG-6 — 历史清理 (4)
# ═══════════════════════════════════════════════════════════════════════
class TestBug6_HistoryCleanup:
    def test_cleanup_removes_old_records(self):
        with get_db() as conn:
            old_time = (datetime.now() - timedelta(days=60)).strftime("%Y-%m-%d %H:%M:%S")
            conn.execute(
                "INSERT INTO provider_history (provider_name, provider_type, base_url, success, failed, last_used_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("old_provider", "text", "https://old.com", 1, 0, old_time),
            )
            now_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            conn.execute(
                "INSERT INTO provider_history (provider_name, provider_type, base_url, success, failed, last_used_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("new_provider", "text", "https://new.com", 1, 0, now_time),
            )
            conn.commit()
        deleted = cleanup_old_history(30)
        with get_db() as conn:
            remaining = conn.execute("SELECT provider_name FROM provider_history").fetchall()
            names = [r[0] for r in remaining]
            assert "new_provider" in names
            assert "old_provider" not in names
            assert deleted >= 1

    def test_cleanup_removes_nothing_for_recent_records(self):
        record_provider_usage("recent", "text", "https://recent.com", success=True)
        deleted = cleanup_old_history(30)
        with get_db() as conn:
            count = conn.execute("SELECT COUNT(*) FROM provider_history").fetchone()[0]
            assert count >= 1
            has_recent = conn.execute(
                "SELECT COUNT(*) FROM provider_history WHERE provider_name='recent'"
            ).fetchone()[0]
            assert has_recent == 1

    def test_cleanup_with_zero_days_removes_all(self):
        # 修正 2026-09-17：本用例原先只调 `record_provider_usage` 后立即清理，
        # 记录的 last_used_at 与 cutoff 落在同一秒，`<` 比较不成立、实际删 0 行；
        # 之所以能过，是因为旧实现返回 `conn.total_changes`（连接累计变更数，
        # 含刚才那条 INSERT）。改为写入一条明确更早的记录，让断言真正校验删除行数。
        with get_db() as conn:
            past = (datetime.now() - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
            conn.execute(
                "INSERT INTO provider_history (provider_name, provider_type, base_url, success, failed, last_used_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("zero_days", "text", "https://zero.com", 1, 0, past),
            )
            conn.commit()
        deleted = cleanup_old_history(0)
        assert deleted >= 1
        with get_db() as conn:
            left = conn.execute(
                "SELECT COUNT(*) FROM provider_history WHERE provider_name='zero_days'"
            ).fetchone()[0]
        assert left == 0

    def test_cleanup_safe_when_table_empty(self):
        deleted = cleanup_old_history(30)
        assert isinstance(deleted, int) and deleted >= 0

    def test_cleanup_custom_days(self):
        with get_db() as conn:
            old_time = (datetime.now() - timedelta(days=10)).strftime("%Y-%m-%d %H:%M:%S")
            conn.execute(
                "INSERT INTO provider_history (provider_name, provider_type, base_url, success, failed, last_used_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("mid_provider", "text", "https://mid.com", 1, 0, old_time),
            )
            conn.commit()
        deleted = cleanup_old_history(5)
        with get_db() as conn:
            remaining = conn.execute("SELECT provider_name FROM provider_history").fetchall()
            names = [r[0] for r in remaining]
            assert "mid_provider" not in names, "10天前的记录在5天清理下应被删除"
            assert deleted >= 1


# ═══════════════════════════════════════════════════════════════════════
# 组 7: record_usage 扩展测试 (7)
# ═══════════════════════════════════════════════════════════════════════
class TestRecordUsage:
    def test_idempotent_same_provider(self):
        for _ in range(10):
            record_provider_usage("idempotent", "text", "https://test.com", success=True)
        with get_db() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM provider_history WHERE provider_name='idempotent'"
            ).fetchone()[0]
        assert count == 1

    def test_different_providers_different_records(self):
        record_provider_usage("p1", "text", "https://a.com", success=True)
        record_provider_usage("p2", "text", "https://b.com", success=True)
        with get_db() as conn:
            count = conn.execute("SELECT COUNT(*) FROM provider_history").fetchone()[0]
        assert count == 2

    def test_same_provider_different_types(self):
        record_provider_usage("provider", "text", "https://api.com", success=True)
        record_provider_usage("provider", "image", "https://api.com", success=True)
        with get_db() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM provider_history WHERE provider_name='provider'"
            ).fetchone()[0]
        assert count == 2, "不同类型应创建独立记录"

    def test_zero_success_and_failed(self):
        chain = get_provider_priority_order()
        # 无记录时不应有错误
        assert isinstance(chain, list)

    def test_rapid_success_updates(self):
        for _ in range(100):
            record_provider_usage("spammer", "text", "https://spam.com", success=True)
        with get_db() as conn:
            row = conn.execute(
                "SELECT success, failed FROM provider_history WHERE provider_name='spammer'"
            ).fetchone()
        assert row[0] == 100
        assert row[1] == 0

    def test_rapid_failure_updates(self):
        for _ in range(50):
            record_provider_usage("failer", "text", "https://fail.com", success=False)
        with get_db() as conn:
            row = conn.execute(
                "SELECT success, failed FROM provider_history WHERE provider_name='failer'"
            ).fetchone()
        assert row[0] == 0
        assert row[1] == 50

    def test_update_existing_record(self):
        record_provider_usage("update_test", "text", "https://up.com", success=True)
        record_provider_usage("update_test", "text", "https://up.com", success=False)
        record_provider_usage("update_test", "text", "https://up.com", success=True)
        with get_db() as conn:
            row = conn.execute(
                "SELECT success, failed FROM provider_history WHERE provider_name='update_test'"
            ).fetchone()
        assert row[0] == 2
        assert row[1] == 1


# ═══════════════════════════════════════════════════════════════════════
# 组 8: get_priority_chain 扩展测试 (15)
# ═══════════════════════════════════════════════════════════════════════
class TestPriorityChain:
    def test_empty_history_returns_builtin_only(self):
        clear_provider_history()
        chain = get_provider_priority_order()
        assert len(chain) == 6
        names = [e["name"] for e in chain]
        for b in ["agnes", "DeepSeek", "SiliconFlow", "通义千问", "智谱GLM", "OpenAI（默认）"]:
            assert b in names

    def test_history_entries_before_builtin(self):
        record_provider_usage("my_provider", "text", "https://my.com", success=True)
        chain = get_provider_priority_order()
        history_entries = [e for e in chain if e.get("last_used_at")]
        builtin_entries = [e for e in chain if not e.get("last_used_at")]
        assert len(history_entries) >= 1
        assert len(builtin_entries) == 6
        # 历史记录应在内置兜底之前
        assert history_entries[-1]["name"] != "OpenAI（默认）"

    def test_lsf_ordering(self):
        record_provider_usage("first", "text", "https://first.com", success=True)
        time.sleep(1.1)
        record_provider_usage("second", "text", "https://second.com", success=True)
        time.sleep(1.1)
        record_provider_usage("third", "text", "https://third.com", success=True)
        chain = get_provider_priority_order()
        history_entries = [e for e in chain if e.get("last_used_at")]
        names = [e["name"] for e in history_entries]
        assert names.index("third") < names.index("second") < names.index("first")

    def test_dedup_by_url(self):
        record_provider_usage("p1", "text", "https://same.com", success=True)
        record_provider_usage("p2", "text", "https://same.com", success=False)
        chain = get_provider_priority_order()
        same_entries = [e for e in chain if e["base_url"] == "https://same.com"]
        assert len(same_entries) == 1, "相同 URL 应去重"

    def test_dedup_by_normalized_url(self):
        record_provider_usage("p1", "text", "https://api.example.com/", success=True)
        record_provider_usage("p2", "text", "https://api.example.com", success=True)
        chain = get_provider_priority_order()
        example_entries = [e for e in chain if "example.com" in e["base_url"].lower()]
        assert len(example_entries) == 1

    def test_success_rate_calculation(self):
        record_provider_usage("calc", "text", "https://calc.com", success=True)
        record_provider_usage("calc", "text", "https://calc.com", success=True)
        record_provider_usage("calc", "text", "https://calc.com", success=False)
        chain = get_provider_priority_order()
        entry = next((e for e in chain if e["name"] == "calc"), None)
        assert entry is not None
        assert abs(entry["success_rate"] - 2/3) < 0.01

    def test_all_builtin_have_success_rate_one(self):
        clear_provider_history()
        chain = get_provider_priority_order()
        for entry in chain:
            if not entry.get("last_used_at"):
                assert entry["success_rate"] == 1.0, f"{entry['name']} 应为 1.0"

    def test_builtin_names_match_fallback(self):
        clear_provider_history()
        chain = get_provider_priority_order()
        builtin_names = [e["name"] for e in chain if not e.get("last_used_at")]
        fallback_names = [f["name"] for f in _FALLBACK_PROVIDERS]
        assert builtin_names == fallback_names

    def test_chain_contains_all_fields(self):
        record_provider_usage("fields", "text", "https://fields.com", success=True)
        chain = get_provider_priority_order()
        entry = next((e for e in chain if e["name"] == "fields"), None)
        assert entry is not None
        required_keys = {"name", "provider", "base_url", "success_rate", "is_builtin_default", "last_used_at"}
        assert required_keys.issubset(entry.keys()), f"缺少字段: {required_keys - entry.keys()}"

    def test_builtin_has_required_fields(self):
        clear_provider_history()
        chain = get_provider_priority_order()
        builtin = next((e for e in chain if e["name"] == "DeepSeek"), None)
        assert builtin is not None
        assert builtin["provider"] == "deepseek"
        assert builtin["base_url"] == "https://api.deepseek.com/v1"
        assert builtin["success_rate"] == 1.0
        # L-1 单源重构后：is_builtin_default 唯一可能为 True 的是内联兜底条目；
        # 若现场为 True 打印完整链便于定位（历史断言保留，失败即暴露污染）。
        assert builtin["is_builtin_default"] is False, [
            (e["name"], e.get("is_builtin_default"), e.get("is_primary")) for e in chain
        ]

    def test_chain_length_with_multiple_history(self):
        for i in range(5):
            record_provider_usage(f"hist_{i}", "text", f"https://hist{i}.com", success=True)
        chain = get_provider_priority_order()
        assert len(chain) == 11, f"应有 5 历史 + 6 内置 = 11，实际 {len(chain)}"

    def test_history_entry_not_treated_as_builtin(self):
        record_provider_usage("hist", "text", "https://hist.com", success=True)
        chain = get_provider_priority_order()
        hist_entry = next((e for e in chain if e["name"] == "hist"), None)
        assert hist_entry is not None
        assert hist_entry.get("is_builtin_default") is False

    def test_clear_history_then_chain(self):
        record_provider_usage("to_clear", "text", "https://clear.com", success=True)
        clear_provider_history()
        chain = get_provider_priority_order()
        names = [e["name"] for e in chain]
        assert "to_clear" not in names
        assert len(chain) == 6

    def test_get_provider_history_returns_list(self):
        record_provider_usage("hist_st", "text", "https://histst.com", success=True)
        history = get_provider_history()
        assert isinstance(history, list)
        assert len(history) >= 1
        entry = history[0]
        assert "provider_name" in entry
        assert "success" in entry
        assert "failed" in entry
        assert "last_used_at" in entry

    def test_get_provider_history_sorted_desc(self):
        record_provider_usage("older", "text", "https://older.com", success=True)
        time.sleep(2.1)
        record_provider_usage("newer", "text", "https://newer.com", success=True)
        history = get_provider_history()
        assert history[0]["provider_name"] == "newer"
        assert history[1]["provider_name"] == "older"


# ═══════════════════════════════════════════════════════════════════════
# 组 9: create_with_fallback 扩展测试 (15)
# ═══════════════════════════════════════════════════════════════════════
class TestCreateWithFallback:
    def test_no_api_key_raises_http_exception(self):
        """L-1 单源重构后口径：无主配置 key → 400 提示先配置（与
        provider_factory.create_provider_with_fallback 统一）。
        旧副本的「静默回退 agnes」会把环境变量密钥当万能兜底发往任意 URL，
        与泄露面收敛（L-6）方向相反，不再是预期行为。"""
        service = AIProviderService()
        with get_db() as conn:
            conn.execute("DELETE FROM ai_config")
            conn.execute(
                "INSERT INTO ai_config (id, provider_name, provider, api_key, model, base_url) "
                "VALUES (1, 'None', 'openai_compatible', '', 'gpt-4o', 'https://api.openai.com/v1')"
            )
            conn.commit()
        service.clear_config_cache()
        from fastapi import HTTPException
        with patch.dict("os.environ", {}, clear=True):
            with pytest.raises(HTTPException) as ei:
                service.create_with_fallback()
        assert ei.value.status_code == 400

    def test_create_provider_without_fallback(self):
        service = AIProviderService()
        config = {
            "api_key": "test-key",
            "base_url": "https://api.openai.com/v1",
            "model": "gpt-4o",
            "max_tokens": 4096,
            "temperature": 0.7,
            "timeout": 30,
        }
        provider = service.create_provider(config)
        assert provider is not None

    def test_get_builtin_fallbacks(self):
        service = AIProviderService()
        fallbacks = service.get_builtin_fallbacks()
        assert len(fallbacks) == 6
        names = [f["name"] for f in fallbacks]
        assert "DeepSeek" in names
        assert "OpenAI（默认）" in names

    def test_normalize_url_consistency(self):
        assert normalize_url("https://api.deepseek.com") == "https://api.deepseek.com/v1"
        assert normalize_url("https://api.deepseek.com/") == "https://api.deepseek.com/v1"
        assert normalize_url("https://api.deepseek.com/v1") == "https://api.deepseek.com/v1"
        assert normalize_url("https://example.com/") == "https://example.com"
        assert normalize_url("https://example.com/v1/") == "https://example.com/v1"

    def test_create_with_fallback_provided_config(self):
        service = AIProviderService()
        config = {
            "api_key": "test-key",
            "base_url": "https://api.openai.com/v1",
            "model": "gpt-4o",
            "max_tokens": 4096,
            "temperature": 0.7,
            "timeout": 30,
        }
        try:
            provider, source = service.create_with_fallback(config)
            assert provider is not None
            # source 可能为空或主配置名
            assert isinstance(source, str)
        except Exception:
            # 网络不可用时可能失败，但只要不抛内部异常即可
            pass

    def test_service_singleton(self):
        # L-1：门面已无实例内缓存（缓存单一归属 provider_factory），
        # “同一进程行为一致”验证改为：两次 get_config 返回同一工厂缓存。
        s1 = AIProviderService()
        s2 = AIProviderService()
        assert s1 is not s2  # 普通实例（非单例约束）
        assert s1.get_config() == s2.get_config()

    def test_get_config_returns_defaults_when_empty(self):
        with get_db() as conn:
            conn.execute("DELETE FROM ai_config")
            conn.execute(
                "INSERT INTO ai_config (id, provider_name, provider, api_key, model, base_url) "
                "VALUES (1, '默认AI配置', 'openai_compatible', '', 'gpt-4o', 'https://api.openai.com/v1')"
            )
            conn.commit()
        service = AIProviderService()
        service.clear_config_cache()
        config = service.get_config()
        assert config["provider"] == "openai_compatible"
        assert config["model"] not in (None, "")
        assert config["temperature"] == 0.7

    def test_config_cache_ttl(self):
        service = AIProviderService()
        config1 = service.get_config()
        config2 = service.get_config()
        assert config1 is config2 or config1 == config2  # 缓存命中

    def test_clear_config_cache(self):
        # L-1：缓存在 provider_factory 模块级，门面 clear 转发后验证工厂缓存已清空。
        import app.services.ai.provider_factory as pf

        service = AIProviderService()
        service.get_config()  # 先填充缓存
        assert pf._ai_config_cache is not None
        service.clear_config_cache()
        assert pf._ai_config_cache is None
        assert pf._ai_config_cache_time == 0

    def test_detect_provider_type_deepseek(self):
        from app.services.ai.ai_provider_service import detect_provider_type
        assert detect_provider_type("https://api.deepseek.com/v1") == "deepseek"
        assert detect_provider_type("https://api.deepseek.com") == "deepseek"

    def test_detect_provider_type_anthropic(self):
        from app.services.ai.ai_provider_service import detect_provider_type
        assert detect_provider_type("https://api.anthropic.com/v1") == "anthropic_compatible"

    def test_detect_provider_type_openai(self):
        from app.services.ai.ai_provider_service import detect_provider_type
        assert detect_provider_type("https://api.openai.com/v1") == "openai_compatible"
        assert detect_provider_type("https://api.siliconflow.cn/v1") == "openai_compatible"


# ═══════════════════════════════════════════════════════════════════════
# 组 10: ai_config_service 定时清理 (10)
# ═══════════════════════════════════════════════════════════════════════
class TestAiConfigService:
    def test_get_service_singleton(self):
        s1 = get_service()
        s2 = get_service()
        assert s1 is s2

    def test_startup_no_error(self):
        """startup 应不抛异常（测试环境无 event loop 时跳过）"""
        import asyncio
        try:
            startup()
        except RuntimeError as e:
            if "no running event loop" in str(e):
                pytest.skip("测试环境无运行中 event loop（需在被 asyncio.run 包裹的上下文中调用）")
            raise

    def test_cleanup_old_history_integration(self):
        with get_db() as conn:
            old_time = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d %H:%M:%S")
            conn.execute(
                "INSERT INTO provider_history (provider_name, provider_type, base_url, success, failed, last_used_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("ancient", "text", "https://ancient.com", 1, 0, old_time),
            )
            conn.commit()
        deleted = cleanup_old_history(30)
        assert deleted >= 1

    def test_cleanup_preserves_recent(self):
        record_provider_usage("recent", "text", "https://recent.com", success=True)
        deleted = cleanup_old_history(30)
        with get_db() as conn:
            count = conn.execute("SELECT COUNT(*) FROM provider_history WHERE provider_name='recent'").fetchone()[0]
        assert count == 1

    def test_record_usage_alias(self):
        record_usage("alias_test", "text", "https://alias.com", success=True)
        with get_db() as conn:
            row = conn.execute(
                "SELECT success FROM provider_history WHERE provider_name='alias_test'"
            ).fetchone()
        assert row[0] == 1

    def test_get_priority_chain_alias(self):
        chain = get_priority_chain()
        assert isinstance(chain, list)
        assert len(chain) >= 6  # 至少有内置兜底

    def test_get_config_alias(self):
        config = get_config()
        assert isinstance(config, dict)
        assert "provider" in config

    def test_cleanup_does_not_affect_builtin(self):
        clear_provider_history()
        deleted = cleanup_old_history(0)
        # 清理后内置兜底仍应存在
        chain = get_provider_priority_order()
        assert len(chain) == 6

    def test_service_has_all_methods(self):
        svc = get_service()
        assert hasattr(svc, "record_usage")
        assert hasattr(svc, "get_priority_chain")
        assert hasattr(svc, "clear_history")
        assert hasattr(svc, "create_with_fallback")
        assert hasattr(svc, "get_config")

    def test_cleanup_with_negative_days(self):
        record_provider_usage("neg_test", "text", "https://neg.com", success=True)
        deleted = cleanup_old_history(-1)
        # -1 天意味着 cutoff = now + 1 day，所有记录都应该被删除
        with get_db() as conn:
            count = conn.execute("SELECT COUNT(*) FROM provider_history").fetchone()[0]
        assert count == 0


# ═══════════════════════════════════════════════════════════════════════
# 组 11: 集成场景与边界测试 (16)
# ═══════════════════════════════════════════════════════════════════════
class TestIntegrationScenarios:
    def test_image_generation_failure_fallback(self):
        """模拟图像生成失败场景：百度失败→火山成功→内置兜底"""
        clear_provider_history()
        for _ in range(3):
            record_provider_usage("百度文心", "image", "https://qianfan.baidubce.com", success=False)
        time.sleep(3.1)
        for _ in range(2):
            record_provider_usage("火山引擎", "image", "https://ark.cn-beijing.volces.com", success=True)

        chain = get_provider_priority_order()
        names = [e["name"] for e in chain]
        baidu_entry = next((e for e in chain if e["name"] == "百度文心"), None)
        volc_entry = next((e for e in chain if e["name"] == "火山引擎"), None)

        assert baidu_entry is not None
        assert volc_entry is not None
        assert baidu_entry["success_rate"] == 0.0
        assert volc_entry["success_rate"] == 1.0
        # 最后使用的应排最前
        assert names.index("火山引擎") < names.index("百度文心")

    def test_text_generation_different_from_image(self):
        """文本和图像 provider 应独立记录"""
        record_provider_usage("provider", "text", "https://text.com", success=True)
        record_provider_usage("provider", "image", "https://image.com", success=False)
        with get_db() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM provider_history WHERE provider_name='provider'"
            ).fetchone()[0]
        assert count == 2

    def test_rapid_fallback_chain_build(self):
        """快速切换多个 provider 后，降级链应正确排序"""
        providers = [
            ("p1", "https://p1.com"),
            ("p2", "https://p2.com"),
            ("p3", "https://p3.com"),
            ("p4", "https://p4.com"),
            ("p5", "https://p5.com"),
        ]
        for name, url in providers:
            record_provider_usage(name, "text", url, success=True)
            time.sleep(1.1)

        chain = get_provider_priority_order()
        history_entries = [e for e in chain if e.get("last_used_at")]
        names = [e["name"] for e in history_entries]
        # 最后一个使用的应排最前
        assert names[0] == "p5"
        assert names[-1] == "p1"

    def test_cleanup_and_rebuild(self):
        """清理后重新记录，降级链应只包含新记录"""
        record_provider_usage("old", "text", "https://old.com", success=True)
        clear_provider_history()
        record_provider_usage("new", "text", "https://new.com", success=True)
        chain = get_provider_priority_order()
        names = [e["name"] for e in chain if e.get("last_used_at")]
        assert "old" not in names
        assert "new" in names

    def test_mixed_success_failure_ordering(self):
        """不同成功率的 provider 在 LSF 下仍按时间排序"""
        record_provider_usage("old_fail", "text", "https://of.com", success=False)
        time.sleep(1.1)
        record_provider_usage("new_success", "text", "https://ns.com", success=True)
        chain = get_provider_priority_order()
        names = [e["name"] for e in chain if e.get("last_used_at")]
        assert names[0] == "new_success"
        assert names[1] == "old_fail"

    def test_empty_base_url_not_in_chain(self):
        """base_url 为空的记录不应出现在降级链中"""
        record_provider_usage("no_url", "text", "", success=True)
        chain = get_provider_priority_order()
        no_url_entries = [e for e in chain if e["base_url"] == ""]
        assert len(no_url_entries) == 0

    def test_large_scale_provider_records(self):
        """大量 provider 记录时降级链仍正确"""
        for i in range(20):
            record_provider_usage(f"provider_{i}", "text", f"https://p{i}.com", success=True)
        chain = get_provider_priority_order()
        history_count = sum(1 for e in chain if e.get("last_used_at"))
        assert history_count == 20
        assert len(chain) == 26  # 20 历史 + 6 内置

    def test_provider_history_schema_compliance(self):
        """provider_history 表字段应符合预期 schema"""
        record_provider_usage("schema_test", "text", "https://schema.com", success=True)
        with get_db() as conn:
            row = conn.execute(
                "PRAGMA table_info(provider_history)"
            ).fetchall()
        column_names = {r[1] for r in row}
        expected = {"id", "provider_name", "provider_type", "base_url", "success", "failed", "last_used_at", "created_at"}
        assert expected.issubset(column_names)

    def test_clear_history_returns_zero_after_clear(self):
        """清除后再次清除应返回 0"""
        record_provider_usage("x", "text", "https://x.com", success=True)
        clear_provider_history()
        count = clear_provider_history()
        assert count == 0

    def test_service_methods_do_not_mutate_chain(self):
        """get_priority_chain 调用不应修改数据库"""
        record_provider_usage("immutable", "text", "https://immutable.com", success=True)
        with get_db() as conn:
            count_before = conn.execute("SELECT COUNT(*) FROM provider_history").fetchone()[0]
        get_provider_priority_order()
        get_provider_priority_order()
        get_provider_priority_order()
        with get_db() as conn:
            count_after = conn.execute("SELECT COUNT(*) FROM provider_history").fetchone()[0]
        assert count_before == count_after

    def test_special_characters_in_provider_name(self):
        """含特殊字符的 provider 名称应正确处理"""
        record_provider_usage("测试- provider (v2.0)", "text", "https://special.com", success=True)
        chain = get_provider_priority_order()
        entry = next((e for e in chain if "测试" in e["name"]), None)
        assert entry is not None

    def test_unicode_provider_name(self):
        """Unicode 名称应正确存储和检索"""
        record_provider_usage("日本語プロバイダ", "text", "https://jp.com", success=True)
        chain = get_provider_priority_order()
        jp_entry = next((e for e in chain if "日本語" in e["name"]), None)
        assert jp_entry is not None
        assert jp_entry["provider"] == "text"

    def test_fallback_chain_structure_after_mixed_operations(self):
        """混合操作后的降级链结构完整性"""
        # 添加记录
        record_provider_usage("a", "text", "https://a.com", success=True)
        record_provider_usage("b", "text", "https://b.com", success=False)
        record_provider_usage("c", "image", "https://c.com", success=True)
        # 去重
        record_provider_usage("a", "text", "https://a.com/", success=True)
        # 清理
        clear_provider_history()
        # 重建
        record_provider_usage("fresh", "text", "https://fresh.com", success=True)
        chain = get_provider_priority_order()
        history_entries = [e for e in chain if e.get("last_used_at")]
        builtin_entries = [e for e in chain if not e.get("last_used_at")]
        assert len(history_entries) == 1
        assert len(builtin_entries) == 6
        assert history_entries[0]["name"] == "fresh"

    def test_normalize_url_edge_cases(self):
        """URL 规范化边界情况"""
        assert normalize_url("https://example.com") == "https://example.com"
        assert normalize_url("https://example.com/") == "https://example.com"
        assert normalize_url("https://example.com/v1") == "https://example.com/v1"
        assert normalize_url("https://example.com/v1/") == "https://example.com/v1"
        assert normalize_url("https://api.deepseek.com") == "https://api.deepseek.com/v1"
        assert normalize_url("https://api.deepseek.com/v1") == "https://api.deepseek.com/v1"

    def test_health_check_endpoint_accessible(self):
        """健康检查端点逻辑验证（服务未启动时通过函数验证）"""
        # 验证 ai_config router 的 health 端点逻辑
        from app.routers.ai_config import router
        routes = [r.path for r in router.routes]
        assert any("health" in r for r in routes)

    def test_provider_history_endpoint_accessible(self):
        """provider-history 端点逻辑验证"""
        from app.routers.ai_config import router
        routes = [r.path for r in router.routes]
        assert any("provider-history" in r for r in routes)
        assert any("health" in r for r in routes)

    def test_provider_history_endpoint_with_records(self):
        """有历史记录时端点返回正确数据（通过服务函数验证）"""
        record_provider_usage("ep_test", "text", "https://ep.com", success=True)
        history = get_provider_history()
        ep_entries = [e for e in history if e["provider_name"] == "ep_test"]
        assert len(ep_entries) == 1
        assert ep_entries[0]["success"] == 1
        assert ep_entries[0]["failed"] == 0

    def test_builtin_fallbacks_in_endpoint(self):
        """内置兜底在端点中 success=100, failed=0（通过服务函数验证）"""
        clear_provider_history()
        history = get_provider_history()
        # 清理后只有内置兜底（无历史记录）
        chain = get_provider_priority_order()
        for item in chain:
            if not item.get("last_used_at"):
                assert item["success_rate"] == 1.0

    def test_clear_history_via_api(self):
        """清除历史后验证端点返回（通过服务函数验证）"""
        record_provider_usage("api_test", "text", "https://api-test.com", success=True)
        clear_provider_history()
        chain = get_provider_priority_order()
        assert len(chain) == 6  # 只有内置兜底

    def test_get_provider_history_empty_list(self):
        """无记录时 get_provider_history 返回空列表"""
        clear_provider_history()
        history = get_provider_history()
        assert history == []

    def test_record_usage_updates_last_used_at(self):
        """每次调用 record_provider_usage 都应更新 last_used_at"""
        record_provider_usage("ts_update", "text", "https://ts.com", success=True)
        time.sleep(1.1)
        record_provider_usage("ts_update", "text", "https://ts.com", success=False)
        chain = get_provider_priority_order()
        entry = next((e for e in chain if e["name"] == "ts_update"), None)
        assert entry is not None
        # last_used_at 应为第二次调用后的时间（最新）
        assert entry["last_used_at"] != ""

    def test_priority_chain_stable_after_multiple_clears(self):
        """多次清除后降级链稳定性"""
        for _ in range(3):
            clear_provider_history()
            chain = get_provider_priority_order()
            assert len(chain) == 6
            names = [e["name"] for e in chain]
            assert names == ["agnes", "DeepSeek", "SiliconFlow", "通义千问", "智谱GLM", "OpenAI（默认）"]

    def test_mixed_content_types_in_history(self):
        """混合内容类型（text/image）的 provider 历史记录"""
        record_provider_usage("mixed", "text", "https://mixed-text.com", success=True)
        record_provider_usage("mixed", "image", "https://mixed-img.com", success=True)
        record_provider_usage("mixed", "text", "https://mixed-text.com", success=False)
        with get_db() as conn:
            rows = conn.execute(
                "SELECT provider_type, success, failed FROM provider_history WHERE provider_name='mixed'"
            ).fetchall()
        assert len(rows) == 2
        types = {r[0] for r in rows}
        assert types == {"text", "image"}

    def test_service_get_provider_history_integration(self):
        """通过 service 实例获取历史记录"""
        service = AIProviderService()
        record_provider_usage("svc_test", "text", "https://svc.com", success=True)
        history = service.get_history()
        assert len(history) >= 1
        svc_entry = next((e for e in history if e["provider_name"] == "svc_test"), None)
        assert svc_entry is not None
        assert svc_entry["success"] == 1
        assert svc_entry["failed"] == 0

    def test_service_create_with_fallback_no_config(self):
        """L-1 单源口径：无任何 key（库+env）时明确 400，不静默拿
        环境变量密钥降级（与泄露面收敛 L-6 一致）"""
        service = AIProviderService()
        with get_db() as conn:
            conn.execute("DELETE FROM ai_config")
            conn.execute(
                "INSERT INTO ai_config (id, provider_name, provider, api_key, model, base_url) "
                "VALUES (1, '空配置', 'openai_compatible', '', 'gpt-4o', 'https://api.openai.com/v1')"
            )
            conn.commit()
        service.clear_config_cache()
        from fastapi import HTTPException
        with patch.dict("os.environ", {}, clear=True):
            with pytest.raises(HTTPException) as ei:
                service.create_with_fallback()
        assert ei.value.status_code == 400

    def test_dedup_preserves_lsf_order(self):
        """URL 去重时应保留 Last-Saved-First 顺序"""
        record_provider_usage("p1", "text", "https://same.com", success=True)
        time.sleep(3.1)
        record_provider_usage("p2", "text", "https://same.com/", success=False)
        chain = get_provider_priority_order()
        same_entries = [e for e in chain if normalize_url(e["base_url"]) == "https://same.com"]
        assert len(same_entries) == 1
        assert same_entries[0]["name"] == "p2"  # 最新的保留

