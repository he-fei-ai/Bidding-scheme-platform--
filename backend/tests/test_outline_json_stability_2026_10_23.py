"""目录 JSON 稳定性增强回归（2026-10-23，对齐《标书智能体（5）》）。

覆盖本次引入的四类加固：
1. 业务校验 validator（make_full_outline_validator）：合法 JSON ≠ 可用 JSON——
   深度三级 / 二级 3~5 / 三级 ≥5 / 标题纯文本；模型原始输出在 collect_json_response
   内过业务校验，失败即带具体问题进入定向修复链。
2. 校验问题格式化（json_issues）：JSONDecodeError 带行列号、ValueError 原样、
   pydantic 错误带 loc 路径；供修复消息使用。
3. JSON 修复三要素（json_repair）：修复助手拿到「目标类型 + 校验问题 + 原始内容」。
4. P0 回归：直出路径不再回填「第X章」前缀（编号由导出端现算），且一级章数与评分项
   覆盖差异 >1 时确定性回退分步路径。
"""

import asyncio
import json

import pytest

from app.services.ai.json_issues import format_issues_text, format_json_issues
from app.services.ai.json_repair import repair_json_with_ai
from app.services.ai.workflows_base import collect_json_response
from app.services.ai.workflows_outline_utils import (
    make_full_outline_validator,
    validate_outline_business,
)


def _valid_outline(n_l1=1, n_l2=3, n_l3=5):
    """构造结构合规的目录（深度 3、二级 3、三级 5）。"""
    return [
        {
            "title": f"章{i}",
            "children": [
                {
                    "title": f"章{i}-二级{j}",
                    "children": [{"title": f"章{i}-{j}-{k}"} for k in range(n_l3)],
                }
                for j in range(n_l2)
            ],
        }
        for i in range(n_l1)
    ]


# ─────────────────────────────────────────────────────────────────────────────
# 1. 业务校验 validator
# ─────────────────────────────────────────────────────────────────────────────


def test_validator_accepts_compliant_outline():
    v = make_full_outline_validator()
    ok, msg = v(_valid_outline())
    assert ok is True, msg


def test_validator_rejects_shallow_depth():
    v = make_full_outline_validator()
    # 只有一级、无子节点 → 深度 1 < 3
    ok, msg = v([{"title": "章1"}])
    assert ok is False
    assert "深度" in msg


def test_validator_rejects_too_few_l2():
    v = make_full_outline_validator()
    # 一级下仅 1 个二级（< 3）
    ok, msg = v(
        [{"title": "章1", "children": [{"title": "a", "children": [{"title": "x"} for _ in range(5)]}]}]
    )
    assert ok is False
    assert "二级目录不足" in msg


def test_validator_rejects_too_few_l3():
    v = make_full_outline_validator()
    # 二级下仅 2 个三级（< 5）
    ok, msg = v(
        [{"title": "章1", "children": [
            {"title": "a", "children": [{"title": "x1"}, {"title": "x2"}]},
            {"title": "b", "children": [{"title": "y1"}, {"title": "y2"}]},
            {"title": "c", "children": [{"title": "z1"}, {"title": "z2"}]},
        ]}]
    )
    assert ok is False
    assert "三级目录不足" in msg


def test_validator_unwraps_dict_wrapper():
    v = make_full_outline_validator()
    ok, msg = v({"outline": _valid_outline()})
    assert ok is True, msg
    ok, msg = v({"children": _valid_outline()})
    assert ok is True, msg


def test_validator_strips_title_prefix_deterministically():
    """编号前缀由程序保证：弱模型带回「第一章 / 1.1」应确定性剥离，
    不作为业务校验失败（避免为此烧一次修复调用）。"""
    v = make_full_outline_validator()
    payload = [
        {
            "title": "第一章 总体实施方案",
            "children": [
                {
                    "title": f"1.{idx} {name}",
                    "children": [{"title": f"一、措施{k}"} for k in range(5)],
                }
                for idx, name in enumerate(
                    ["安全管理机构", "质量保证措施", "进度控制计划"], start=1
                )
            ],
        }
    ]
    ok, msg = v(payload)
    assert ok is True, msg
    # 剥离后标题不含编号前缀
    assert payload[0]["title"] == "总体实施方案"
    assert payload[0]["children"][0]["title"] == "安全管理机构"


def test_validator_flags_duplicate_sibling_title():
    v = make_full_outline_validator()
    ok, msg = v(
        [{"title": "章1", "children": [
            {"title": "a", "children": [{"title": "x"} for _ in range(5)]},
            {"title": "a", "children": [{"title": "y"} for _ in range(5)]},
            {"title": "b", "children": [{"title": "z"} for _ in range(5)]},
        ]}]
    )
    assert ok is False
    assert "重复" in msg


# ─────────────────────────────────────────────────────────────────────────────
# 2. 校验问题格式化
# ─────────────────────────────────────────────────────────────────────────────


def test_format_json_decode_error_has_position():
    try:
        json.loads('{\n  "a": 1')  # 缺闭合，触发 DecodeError
    except json.JSONDecodeError as e:
        issues = format_json_issues(e)
        assert len(issues) == 1
        assert "行" in issues[0] and "列" in issues[0]


def test_format_value_error_passthrough():
    err = ValueError("第 1 项二级目录不足 3 个（实际 1 个）")
    assert format_json_issues(err) == ["第 1 项二级目录不足 3 个（实际 1 个）"]


def test_format_issues_text_single_and_list():
    """单条 ValueError 原样带前缀；多错误列表逐条独立成行（供修复消息分段）。"""
    text = format_issues_text(ValueError("第 1 项标题为空"))
    assert text == "- 第 1 项标题为空"
    from app.services.ai.json_issues import format_issues_from_list

    assert format_issues_from_list(["A", "B", "", None]) == "- A\n- B"


# ─────────────────────────────────────────────────────────────────────────────
# 3. JSON 修复三要素
# ─────────────────────────────────────────────────────────────────────────────


def test_repair_message_carries_three_parts(monkeypatch):
    captured = {}

    async def _ai(messages):
        captured["messages"] = messages
        return '{"ok": true}'

    asyncio.run(
        repair_json_with_ai(
            "RAW_OUTPUT",
            _ai,
            target_description="完整目录",
            issues=["outline.0.title: Field required", "outline.0.children: 非空"],
        )
    )
    user_msg = next(m["content"] for m in captured["messages"] if m["role"] == "user")
    assert "目标类型: 完整目录" in user_msg
    assert "校验问题:" in user_msg
    assert "outline.0.title: Field required" in user_msg
    assert "原始输出:" in user_msg and "RAW_OUTPUT" in user_msg


# ─────────────────────────────────────────────────────────────────────────────
# 4. collect_json_response 接入 validator + 定向修复
# ─────────────────────────────────────────────────────────────────────────────


class _FakeProvider:
    json_mode_supported = False

    def __init__(self, responses):
        self._responses = list(responses)

    async def chat(self, messages, **kwargs):
        assert self._responses, "fake provider 用尽响应"
        return self._responses.pop(0)


def test_collect_validator_passes_on_valid():
    valid = json.dumps(_valid_outline())
    prov = _FakeProvider([valid])
    out = asyncio.run(
        collect_json_response(
            prov,
            [{"role": "user", "content": "生成目录"}],
            max_retries=1,
            validator=make_full_outline_validator("完整目录"),
        )
    )
    assert isinstance(out, list) and out[0]["title"] == "章0"


def test_collect_validator_triggers_repair_then_passes():
    invalid = json.dumps([{"title": "章1"}])  # 深度不足
    valid = json.dumps(_valid_outline())
    prov = _FakeProvider([invalid, valid])
    out = asyncio.run(
        collect_json_response(
            prov,
            [{"role": "user", "content": "生成目录"}],
            max_retries=1,
            validator=make_full_outline_validator("完整目录"),
        )
    )
    # 第一次校验失败 → 修复调用（携带格式化问题）→ 第二次通过
    assert isinstance(out, list) and out[0]["title"] == "章0"


def test_collect_validator_fails_after_repair_exhausted():
    invalid = json.dumps([{"title": "章1"}])
    prov = _FakeProvider([invalid, invalid])
    with pytest.raises(RuntimeError):
        asyncio.run(
            collect_json_response(
                prov,
                [{"role": "user", "content": "生成目录"}],
                max_retries=1,
                validator=make_full_outline_validator("完整目录"),
            )
        )


# ─────────────────────────────────────────────────────────────────────────────
# 5. P0 回归：直出路径前缀契约 + 覆盖回退
# ─────────────────────────────────────────────────────────────────────────────


def test_normalize_direct_strips_prefix_on_all_levels(monkeypatch):
    from app.services.ai.workflows_outline_v51 import _normalize_direct_outline

    raw = [
        {
            "title": "第一章 总体实施方案",
            "children": [
                {
                    "title": "1.1 安全管理机构",
                    "children": [{"title": "一、安全技术措施", "children": []}],
                }
            ],
        }
    ]
    out = _normalize_direct_outline(raw)
    assert out[0]["title"] == "总体实施方案"
    assert out[0]["children"][0]["title"] == "安全管理机构"
    assert out[0]["children"][0]["children"][0]["title"] == "安全技术措施"


def test_direct_path_no_prefix_added(monkeypatch):
    from app.services.ai import workflows_outline_v51 as wo

    async def _fake(provider, messages, **kw):
        return _valid_outline()

    monkeypatch.setattr(wo, "collect_json_response", _fake)
    out = asyncio.run(wo.generate_outline_direct(object(), "概述", "[]", "工程"))
    assert out, "直出路径未产出目录"
    # 契约：一级标题不得回填「第X章」前缀
    assert not any(o["title"].startswith("第") for o in out), [o["title"] for o in out]


def test_direct_path_coverage_fallback_when_l1_mismatch(monkeypatch):
    """一级章数与评分项差异 >1 时确定性回退分步（与分步路径同尺 ±1）。"""
    from app.services.ai import workflows_outline_v51 as wo

    async def _fake(provider, messages, **kw):
        return _valid_outline(n_l1=3)  # 3 章

    monkeypatch.setattr(wo, "collect_json_response", _fake)
    # 评分项 6 个 → 期望 6 章，实际 3 章，差异 3 > 1 → 回退（返回空）
    scoring = json.dumps([{"item": f"评分{k}"} for k in range(6)])
    out = asyncio.run(wo.generate_outline_direct(object(), "概述", scoring, "工程"))
    assert out == [], "覆盖差异过大应回退分步路径"


def test_direct_path_coverage_ok_within_tolerance(monkeypatch):
    from app.services.ai import workflows_outline_v51 as wo

    async def _fake(provider, messages, **kw):
        return _valid_outline(n_l1=4)  # 4 章

    monkeypatch.setattr(wo, "collect_json_response", _fake)
    scoring = json.dumps([{"item": f"评分{k}"} for k in range(5)])  # 期望 5，差异 1 ≤ 1
    out = asyncio.run(wo.generate_outline_direct(object(), "概述", scoring, "工程"))
    assert len(out) == 4


def test_business_rules_toggle(monkeypatch):
    """OUTLINE_BUSINESS_VALIDATION_ENABLED=false 时退化为历史宽松口径（仅 ≥2/≥2）。"""
    import app.services.ai.workflows_outline_utils as utils

    monkeypatch.setattr(utils, "OUTLINE_BUSINESS_VALIDATION_ENABLED", False)
    # 历史口径下，2 二级 / 2 三级 应通过
    ok, _ = utils.validate_outline_business(
        [{"title": "章1", "children": [
            {"title": "a", "children": [{"title": "x1"}, {"title": "x2"}]},
            {"title": "b", "children": [{"title": "y1"}, {"title": "y2"}]},
        ]}]
    )
    assert ok is True
