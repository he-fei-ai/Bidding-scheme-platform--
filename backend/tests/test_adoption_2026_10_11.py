"""易标方法论引入本软件——接入点回归测试（2026-10-11）。

对应《报告_易标引入本软件_方案与代码修改_2026-10-11.md》§4 各接入项。
每个接入点两条断言：门禁 OFF（默认）保持既有行为；门禁 ON 走新分支。

运行：
    cd backend
    python -m pytest tests/test_adoption_2026_10_11.py -v
"""

from __future__ import annotations

import asyncio

from app.config import CONSISTENCY_UNIQUE_EDIT_STRICT
from app.services.text_patch import PatchApplyError, apply_exact_patch, apply_patches


def _build_messages_with_layer_gate(monkeypatch, gate: bool):
    """调用真实正文装配函数，检查 SPLIT_PROMPT_LAYERS 门禁的字节影响。"""
    import app.config as config_module
    from app.services.ai import workflows_sections as ws

    monkeypatch.setattr(config_module, "SPLIT_PROMPT_LAYERS", gate, raising=False)
    chapter_info = {
        "title": "施工部署",
        "summary": "总体部署要点   ",  # 末尾留空白，用于观察 normalize 是否生效
        "level": 3,
        "is_l3_own": True,
        "id": "3.1",
    }
    messages = ws.build_chapter_content_messages(
        chapter_info, facts=None, summary="总体部署要点   ", writing_length="standard"
    )
    return messages



# ============================================================
# #13 一致性唯一命中修复接入（CONSISTENCY_UNIQUE_EDIT_STRICT）
# ============================================================


class TestUniqueEditStrictConsumption:
    """验证 config.CONSISTENCY_UNIQUE_EDIT_STRICT 被补丁引擎真正消费。"""

    def test_config_default_true(self):
        # 默认收紧（更安全），且属"行为收紧"例外允许默认 true
        assert CONSISTENCY_UNIQUE_EDIT_STRICT is True

    def test_single_match_ok(self):
        result = apply_exact_patch(
            "本项目工期30天完工。",
            {"old_text": "工期30天", "new_text": "工期45天"},
        )
        assert result == "本项目工期45天完工。"

    def test_multi_match_rejected_when_strict_default(self):
        # 默认读 config = True：多命中必须拒绝（与既有行为逐字一致）
        text = "工期30天，另附工期30天说明。"
        patch = {"old_text": "工期30天", "new_text": "工期45天"}
        assert CONSISTENCY_UNIQUE_EDIT_STRICT is True
        try:
            apply_exact_patch(text, patch)
            assert False, "strict=True 时多命中应抛 PatchApplyError"
        except PatchApplyError as exc:
            assert "2 次" in str(exc)

    def test_multi_match_rejected_when_strict_explicit_true(self):
        text = "工期30天，另附工期30天说明。"
        patch = {"old_text": "工期30天", "new_text": "工期45天"}
        try:
            apply_exact_patch(text, patch, unique_strict=True)
            assert False, "unique_strict=True 多命中应拒绝"
        except PatchApplyError:
            pass

    def test_multi_match_replaced_all_when_strict_false(self):
        # 显式关闭收紧：回退旧的"多处也替换"宽松行为，替换全部出现位置
        text = "工期30天，另附工期30天说明。"
        patch = {"old_text": "工期30天", "new_text": "工期45天"}
        result = apply_exact_patch(text, patch, unique_strict=False)
        assert result == "工期45天，另附工期45天说明。"
        assert "30" not in result

    def test_zero_match_always_rejected(self):
        # 无论开关，未命中一律拒绝（不能凭空追加）
        for strict in (True, False):
            try:
                apply_exact_patch(
                    "没有相关内容。", {"old_text": "工期30天", "new_text": "工期45天"},
                    unique_strict=strict,
                )
                assert False, "未命中应抛 PatchApplyError"
            except PatchApplyError as exc:
                assert "未在当前小节正文中找到" in str(exc)

    def test_apply_patches_threads_unique_strict(self):
        text = "工期30天，另附工期30天说明。"
        patches = [{"old_text": "工期30天", "new_text": "工期45天"}]
        # strict（默认）：整批拒绝该条
        res_strict = apply_patches(text, patches, unique_strict=True)
        assert res_strict["applied_count"] == 0
        assert res_strict["changed"] is False
        # 关闭收紧：全部替换
        res_loose = apply_patches(text, patches, unique_strict=False)
        assert res_loose["applied_count"] == 1
        assert res_loose["content"] == "工期45天，另附工期45天说明。"

    def test_line_range_branch_unaffected_by_flag(self):
        # 行号区间命中路径与唯一命中收紧无关，两种取值都应成功且行为一致
        text = "第一行\n工期30天\n第三行"
        patch = {
            "old_text": "工期30天",
            "new_text": "工期45天",
            "start_line": 2,
            "end_line": 2,
        }
        for strict in (True, False):
            assert apply_exact_patch(text, patch, unique_strict=strict) == "第一行\n工期45天\n第三行"


# ============================================================
# #8 正文分层消息栈「格式稳定」门禁消费（SPLIT_PROMPT_LAYERS）
# ============================================================


class TestSplitPromptLayersConsumption:
    """验证 SPLIT_PROMPT_LAYERS 被正文装配函数消费，且默认零变更。"""

    def test_gate_off_default_keeps_bytes_identical(self, monkeypatch):
        # 默认 false：末层/稳定层的原始空白应原样保留（与既有行为逐字一致）
        assert getattr(__import__("app.config", fromlist=["SPLIT_PROMPT_LAYERS"]),
                      "SPLIT_PROMPT_LAYERS", False) is False
        messages = _build_messages_with_layer_gate(monkeypatch, gate=False)
        joined = "\n".join(m.content for m in messages)
        # 关闭门禁时保留「总体部署要点」后的行尾空白
        assert "总体部署要点   " in joined

    def test_gate_on_normalizes_trailing_whitespace(self, monkeypatch):
        # 开启门禁时行尾空白被 normalize_readable_text 统一剥离，但可见文本不变
        messages_off = _build_messages_with_layer_gate(monkeypatch, gate=False)
        messages_on = _build_messages_with_layer_gate(monkeypatch, gate=True)
        text_off = "\n".join(m.content for m in messages_off)
        text_on = "\n".join(m.content for m in messages_on)
        assert "总体部署要点   " in text_off
        assert "总体部署要点   " not in text_on
        # 语义等价：strip 后内容一致，仅字节稳定性差异
        assert "\n".join(ln.rstrip() for ln in text_off.split("\n")).strip() == \
               "\n".join(ln.rstrip() for ln in text_on.split("\n")).strip()


# ============================================================
# #16 知识库两轮补漏「仅预览、不落库」接入（KNOWLEDGE_REFILL_TWO_ROUND）
# ============================================================


class TestKnowledgeRefillPreview:
    """验证补漏模块被主链路安全消费：默认门禁关闭、bid_text 真正注入、预览形态正确。"""

    def test_config_default_false(self):
        # 默认关闭 → sse_outline 整段跳过，目录生成端到端逐字不变
        from app.config import KNOWLEDGE_REFILL_TWO_ROUND

        assert KNOWLEDGE_REFILL_TWO_ROUND is False

    async def test_refill_injects_bid_text_into_prompt(self):
        # 关键修复：此前 bid_text 形参从未注入提示词（凭空编造）；现应作为取材来源出现
        from app.services.ai.knowledge_refill import refill_knowledge_gaps

        captured: dict = {}
        marker = "本标段采用逆作法施工工期540天"

        async def _ai_call(messages):
            captured["user_prompt"] = messages[-1]["content"]
            return "[]"  # 合法空数组，聚焦断言提示词内容

        outline = [{"title": "逆作法施工方案", "children": [{"title": "工期保障"}]}]
        result = await refill_knowledge_gaps(outline, [], marker, _ai_call)
        assert result.success is True
        assert marker in captured["user_prompt"]

    async def test_refill_truncates_overlong_bid_text(self):
        # 超长正文应被有界摘录截断并标注，避免撑爆上下文
        from app.services.ai.knowledge_refill import refill_knowledge_gaps

        captured: dict = {}

        async def _ai_call(messages):
            captured["user_prompt"] = messages[-1]["content"]
            return "[]"

        long_text = "甲" * 20000
        outline = [{"title": "任意章节"}]
        await refill_knowledge_gaps(outline, [], long_text, _ai_call, max_bid_excerpt_chars=8000)
        assert "已截断" in captured["user_prompt"]
        assert len(captured["user_prompt"]) < len(long_text)

    async def test_refill_empty_bid_text_returns_error(self):
        # 空正文 → 明确失败，不产出臆造条目
        from app.services.ai.knowledge_refill import refill_knowledge_gaps

        async def _ai_call(messages):  # pragma: no cover - 不应被调用
            raise AssertionError("空正文不应调用 AI")

        outline = [{"title": "某章节"}]
        result = await refill_knowledge_gaps(outline, [], "", _ai_call)
        assert result.success is False
        assert "为空" in (result.error or "")

    def test_row_to_knowledge_item_parses_json_keywords(self):
        # DB 行 keywords 为 JSON 字符串，映射层需还原为 list 并与 schema 解耦
        import json

        from app.services.ai.knowledge_refill import row_to_knowledge_item

        row = {
            "id": 7,
            "title": "评分办法",
            "content": "技术标权重40%",
            "category": "评分标准",
            "keywords": json.dumps(["评分", "权重"], ensure_ascii=False),
            "source_ref": "doc#10",
        }
        item = row_to_knowledge_item(row)
        assert item.title == "评分办法"
        assert item.keywords == ["评分", "权重"]
        assert item.source == "doc#10"

    async def test_build_refill_preview_shape(self):
        # 预览形态 {success,count,items,error}，provider.chat 经适配层被消费
        from app.services.ai.knowledge_refill import build_refill_preview

        class _FakeProvider:
            def __init__(self):
                self.calls = 0

            async def chat(self, messages, *, max_tokens, temperature, **kwargs):
                self.calls += 1
                return '[{"title":"危大工程清单","content":"深基坑超过5米需专家论证",' \
                       '"category":"技术要求","keywords":["深基坑","专家论证"]}]'

        provider = _FakeProvider()
        outline = [{"title": "危大工程管理"}]
        preview = await build_refill_preview(provider, outline, [], "深基坑超过5米需专家论证")
        assert preview["success"] is True
        assert preview["count"] == 1
        assert preview["items"][0]["title"] == "危大工程清单"
        assert "error" in preview
        assert provider.calls == 1

    async def test_build_refill_preview_dedup_against_existing(self):
        # 已有条目命中的补漏应被去重，count=0（不污染知识库）
        from app.services.ai.knowledge_refill import build_refill_preview

        class _FakeProvider:
            async def chat(self, messages, *, max_tokens, temperature, **kwargs):
                return '[{"title":"深基坑支护","content":"排桩+内支撑","category":"技术要求",' \
                       '"keywords":["排桩"]}]'

        existing = [{"id": 1, "title": "深基坑支护", "content": "排桩+内支撑", "keywords": "[]"}]
        preview = await build_refill_preview(
            _FakeProvider(), [{"title": "深基坑支护"}], existing, "排桩加内支撑方案"
        )
        assert preview["success"] is True
        assert preview["count"] == 0


# ============================================================
# #14 Agent 修复持久工作区（AGENT_REPAIR_PERSIST_WORKSPACE）
#     —— 同时回归 build_agent_workspace 丢失 return 导致 run_agent_repair 崩溃的缺陷
# ============================================================


def _agent_seed_project(name: str) -> int:
    from app.db import get_db, init_db

    init_db()
    with get_db() as conn:
        conn.execute(
            "INSERT INTO projects (name, industry, created_by) VALUES (?, ?, ?)",
            (name, "通用", 990001),
        )
        return conn.execute("SELECT id FROM projects ORDER BY id DESC LIMIT 1").fetchone()["id"]


def _agent_seed_section(project_id: int, *, title: str, content: str) -> int:
    from app.db import get_db

    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO sections (project_id, title, level, sort_order, content, status, section_type) "
            "VALUES (?, ?, 1, 0, ?, 'completed', 'normal')",
            (project_id, title, content),
        )
        return cur.lastrowid


def _agent_seed_conflict(project_id: int, conflict_id: str) -> None:
    from app.db import get_db

    with get_db() as conn:
        conn.execute(
            "INSERT INTO consistency_conflicts "
            "(project_id, conflict_id, fact_name, values_json, sources_json, severity, description, resolved) "
            "VALUES (?, ?, ?, ?, '[\"section_1\"]', 'medium', '工期口径不一致', 0)",
            (project_id, conflict_id, "工期", '["90","120"]'),
        )


class TestAgentRepairWorkspace:
    """验证持久工作区门禁，并锁定 build_agent_workspace 必须返回 dict 的缺陷修复。"""

    def test_config_default_false(self):
        from app.config import AGENT_REPAIR_PERSIST_WORKSPACE

        assert AGENT_REPAIR_PERSIST_WORKSPACE is False

    def test_build_workspace_returns_dict_not_none(self, tmp_path):
        # 关键回归：此前尾部代码被误并入 _exec_action，本函数返回 None
        # → run_agent_repair 在 ctx["sections"] 处 TypeError 崩溃，端点从未跑通。
        from app.services import agent_repair

        pid = _agent_seed_project("adoption-agent-ws-off")
        _agent_seed_section(pid, title="施工进度", content="工期90天完成主体结构")
        _agent_seed_conflict(pid, f"c_off_{pid}")

        import app.config as config_module

        monkeypatch_gate = getattr(config_module, "AGENT_REPAIR_PERSIST_WORKSPACE", False)
        assert monkeypatch_gate is False  # 默认关闭

        ctx = agent_repair.build_agent_workspace(pid, base=tmp_path)
        # 必须返回 dict（而非 None），且带 run_agent_repair 依赖的键
        assert isinstance(ctx, dict)
        assert ctx["persisted"] is False
        assert ctx["dir"] is None
        assert ctx["global_facts_md"] is None
        assert [s["content"] for s in ctx["sections"]] == ["工期90天完成主体结构"]
        assert len(ctx["conflicts"]) == 1
        # 关闭时不得在 base 下留下任何工作区文件
        assert not (tmp_path / str(pid) / "technical-plan.md").exists()
        assert not (tmp_path / str(pid) / "global-facts.md").exists()

    def test_build_workspace_persists_when_gate_on(self, tmp_path, monkeypatch):
        # 门禁 ON：写出带章节标记的正文快照 + 事实底稿，dir/路径非空
        import app.config as config_module
        from app.services import agent_repair

        monkeypatch.setattr(config_module, "AGENT_REPAIR_PERSIST_WORKSPACE", True, raising=False)

        pid = _agent_seed_project("adoption-agent-ws-on")
        _agent_seed_section(pid, title="质量管理体系", content="本工程实行三检制")
        _agent_seed_conflict(pid, f"c_on_{pid}")

        ctx = agent_repair.build_agent_workspace(pid, base=tmp_path)
        assert ctx["persisted"] is True
        assert ctx["dir"] == str(tmp_path / str(pid))
        plan_md = tmp_path / str(pid) / "technical-plan.md"
        facts_md = tmp_path / str(pid) / "global-facts.md"
        assert plan_md.exists() and facts_md.exists()
        text = plan_md.read_text(encoding="utf-8")
        assert "[[SECTION id=" in text and "本工程实行三检制" in text
        assert "全局事实" in facts_md.read_text(encoding="utf-8")

    def test_run_agent_repair_no_crash_without_sections(self, tmp_path):
        # 端到端最小回归：空正文项目应安全返回报告，而非因 ctx=None 抛 TypeError
        from app.services.agent_repair import run_agent_repair

        pid = _agent_seed_project("adoption-agent-empty")
        report = asyncio.run(run_agent_repair(pid, dry_run=True, base=tmp_path))
        assert report["success"] is False
        assert "没有可供修复的正文" in report["message"]



