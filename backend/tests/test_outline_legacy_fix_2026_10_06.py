"""目录生成模块「遗留问题」收口测试（2026-10-06）

覆盖上一轮探查报告中 5 项遗留问题的修复：

- A 活跃 V5.1 路径集成测试：此前 104 条 `[V3 LEGACY]` 测试永久跳过，活跃 V5.1
  路径的真实覆盖率被高估。这里以「mock 一次 AI 调用 + 走真实直出链路」的方式
  直接驱动 `generate_outline_direct`，覆盖 提示词 → AI → JSON 规整 → 后处理 →
  编号 的完整活动链路。
- B `_flatten_outline_tree`：level 改由递归深度计算，不再盲信节点自带 level。
- C 门禁重复编号检测：原先只判「缺失/层级不匹配」，检测不到重复编号（破坏
  「编号即位置」契约）；现在重复编号归入 invalid_ids，复用既有自动重排链路。
- D `_call_ai_v51` temperature：去掉无解释硬编码，集中到
  `_resolve_outline_temperature()`（默认 0.1，可用 OUTLINE_AI_TEMPERATURE 覆盖）。
- E 评分点容错解析：非标准 JSON（代码围栏 / 内嵌 JSON / 尾随逗号）不再整批丢弃。
"""

import asyncio
import os

import pytest

from app.services.ai import outline_gate
from app.services.ai import workflows_outline_v51 as wo
from app.services.ai.workflows_outline_utils import _flatten_outline_tree


# ─────────────────────────────────────────────────────────────────────────────
# A：活跃 V5.1 路径集成（直出链路）
# ─────────────────────────────────────────────────────────────────────────────


def _ai_payload() -> list:
    """模拟模型返回的标准三层级目录 JSON（children 嵌套）。"""
    return [
        {
            "id": "1",
            "title": "总体实施方案",
            "description": "总体部署说明",
            "children": [
                {
                    "id": "1.1",
                    "title": "施工总体部署",
                    "children": [
                        {"id": "1.1.1", "title": "部署原则"},
                        {"id": "1.1.2", "title": "施工总平面布置"},
                        {"id": "1.1.3", "title": "资源配置计划"},
                    ],
                },
                {
                    "id": "1.2",
                    "title": "质量管理体系",
                    "children": [
                        {"id": "1.2.1", "title": "质量目标"},
                        {"id": "1.2.2", "title": "质量保证措施"},
                        {"id": "1.2.3", "title": "质量检验制度"},
                    ],
                },
                {
                    "id": "1.3",
                    "title": "安全文明施工",
                    "children": [
                        {"id": "1.3.1", "title": "安全管理机构"},
                        {"id": "1.3.2", "title": "安全技术措施"},
                        {"id": "1.3.3", "title": "文明施工措施"},
                    ],
                },
            ],
        }
    ]


class TestV51DirectPathIntegration:
    def test_direct_path_builds_three_level_tree(self, monkeypatch):
        """活跃路径：一次 AI 调用产出标准三级目录，且一级标题带第X章编号"""

        async def _fake_collect(provider, messages, **kwargs):
            assert messages and len(messages) >= 2, "提示词链路未构建 system/user 消息"
            return _ai_payload()

        monkeypatch.setattr(wo, "collect_json_response", _fake_collect)

        outline = asyncio.run(
            wo.generate_outline_direct(object(), "某市政道路工程概述", "[]", "市政工程")
        )
        assert outline, "直出路径未产出目录"
        assert len(outline) == 1
        # 一级标题补齐第X章编号（_normalize_direct_outline 契约）
        assert outline[0]["title"].startswith("第一章")

        l2 = outline[0]["children"]
        assert len(l2) >= 3, "二级目录数量不足"
        assert all(c.get("children") for c in l2), "三级目录缺失"

    def test_direct_path_then_numbering_is_consistent(self, monkeypatch):
        """活跃路径产出 → 编号赋值 → 门禁事实：编号连续、层级完整、无非法编号"""

        async def _fake_collect(provider, messages, **kwargs):
            return _ai_payload()

        monkeypatch.setattr(wo, "collect_json_response", _fake_collect)

        outline = asyncio.run(
            wo.generate_outline_direct(object(), "某市政道路工程概述", "[]", "市政工程")
        )
        numbered = wo._assign_ids(outline)

        # 编号命名空间：L1=1、L2=1.1、L3=1.1.1
        assert numbered[0]["id"] == "1"
        assert [c["id"] for c in numbered[0]["children"][:3]] == ["1.1", "1.2", "1.3"]
        assert numbered[0]["children"][0]["children"][0]["id"] == "1.1.1"

        facts = outline_gate.compute_outline_facts(numbered)
        assert not facts["invalid_id_nodes"], "编号缺失/重复/层级不匹配"
        assert facts["max_depth"] == 3
        assert facts["node_counts_by_level"].get("1") == 1
        assert facts["node_counts_by_level"].get("3", 0) >= 9


# ─────────────────────────────────────────────────────────────────────────────
# B：_flatten_outline_tree level 由递归深度决定
# ─────────────────────────────────────────────────────────────────────────────


class TestFlattenLevelFromDepth:
    def test_level_ignores_node_declared_level(self):
        """节点自带的错误 level（9）不得入库，层级按实际递归深度计算"""
        tree = [
            {
                "title": "A",
                "level": 9,
                "children": [
                    {
                        "title": "A1",
                        "level": 9,
                        "children": [{"title": "A1a", "level": 9}],
                    }
                ],
            }
        ]
        flat = _flatten_outline_tree(tree)
        assert [f["level"] for f in flat] == [1, 2, 3]

    def test_backward_compatible_signature(self):
        """既有两参数调用方式保持不变（level 参数带默认值）"""
        flat = _flatten_outline_tree([{"title": "A", "children": []}], None)
        assert flat[0]["level"] == 1


# ─────────────────────────────────────────────────────────────────────────────
# C：门禁重复编号检测 + 自动重排
# ─────────────────────────────────────────────────────────────────────────────


class TestGateDuplicateIdDetection:
    def test_duplicate_ids_flagged_as_invalid(self):
        """两个子节点都是 1.1：格式合法但破坏「编号即位置」契约，必须被判定非法"""
        outline = [
            {
                "id": "1",
                "title": "第一章 总体实施方案",
                "children": [
                    {"id": "1.1", "title": "安全管理机构", "children": []},
                    {"id": "1.1", "title": "安全技术措施", "children": []},
                ],
            }
        ]
        facts = outline_gate.compute_outline_facts(outline)
        assert facts["invalid_id_nodes"], "重复编号未被门禁识别"

    def test_renumber_clears_duplicates(self):
        """自动重排后重复编号消失，且同级编号连续"""
        outline = [
            {
                "id": "1",
                "title": "第一章 总体实施方案",
                "children": [
                    {"id": "1.1", "title": "安全管理机构", "children": []},
                    {"id": "1.1", "title": "安全技术措施", "children": []},
                ],
            }
        ]
        fixed, changed = outline_gate.renumber_outline_ids(outline)
        assert changed > 0
        assert [c["id"] for c in fixed[0]["children"]] == ["1.1", "1.2"]
        assert not outline_gate.compute_outline_facts(fixed)["invalid_id_nodes"]

    def test_unique_ids_not_flagged(self):
        """正常唯一编号不得被误判"""
        outline = [
            {
                "id": "1",
                "title": "第一章 总体实施方案",
                "children": [
                    {"id": "1.1", "title": "甲", "children": []},
                    {"id": "1.2", "title": "乙", "children": []},
                ],
            }
        ]
        assert not outline_gate.compute_outline_facts(outline)["invalid_id_nodes"]


# ─────────────────────────────────────────────────────────────────────────────
# D：temperature 集中化（默认 0.1，向后兼容）
# ─────────────────────────────────────────────────────────────────────────────


class TestOutlineTemperature:
    @staticmethod
    def _run_call(monkeypatch, **kwargs):
        captured = {}

        async def _fake_collect(provider, messages, **kw):
            captured.update(kw)
            return {}

        monkeypatch.setattr(wo, "collect_json_response", _fake_collect)
        asyncio.run(wo._call_ai_v51(object(), "sys", "usr", **kwargs))
        return captured

    def test_default_is_0_1(self, monkeypatch):
        monkeypatch.delenv("OUTLINE_AI_TEMPERATURE", raising=False)
        assert self._run_call(monkeypatch)["temperature"] == 0.1

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("OUTLINE_AI_TEMPERATURE", "0.35")
        assert self._run_call(monkeypatch)["temperature"] == 0.35

    def test_invalid_env_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("OUTLINE_AI_TEMPERATURE", "not-a-number")
        assert self._run_call(monkeypatch)["temperature"] == 0.1

    def test_explicit_param_wins(self, monkeypatch):
        monkeypatch.setenv("OUTLINE_AI_TEMPERATURE", "0.35")
        assert self._run_call(monkeypatch, temperature=0.9)["temperature"] == 0.9


# ─────────────────────────────────────────────────────────────────────────────
# E：评分点容错解析
# ─────────────────────────────────────────────────────────────────────────────


class TestScoringPointsTolerantParse:
    def test_plain_json(self):
        parsed, mode = wo._parse_scoring_points_json('[{"item": "质量目标"}]')
        assert mode == "json"
        assert parsed[0]["item"] == "质量目标"

    def test_fenced_json_is_salvaged(self):
        """```json 代码围栏包裹：原裸 json.loads 会整体失败丢信息"""
        raw = '```json\n[{"item": "工期保证"}]\n```'
        parsed, mode = wo._parse_scoring_points_json(raw)
        assert mode == "salvage"
        assert parsed[0]["item"] == "工期保证"

    def test_embedded_json_is_salvaged(self):
        """AI 在 JSON 前后附了说明文字：原实现会整批丢弃评分点"""
        raw = '以下是评分点：\n[{"item": "安全措施"}]\n以上供参考。'
        parsed, mode = wo._parse_scoring_points_json(raw)
        assert mode == "salvage"
        assert parsed[0]["item"] == "安全措施"

    def test_garbage_returns_failed_not_raise(self):
        parsed, mode = wo._parse_scoring_points_json("完全不是JSON的一段话")
        assert parsed is None
        assert mode == "failed"

    def test_empty_input(self):
        parsed, mode = wo._parse_scoring_points_json("")
        assert parsed is None
        assert mode == "empty"
