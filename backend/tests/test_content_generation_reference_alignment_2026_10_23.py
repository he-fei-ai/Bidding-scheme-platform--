"""正文生成模块对齐外部引用软件《标书智能体（3）——生成标书正文代码+提示词》

2026-10-23 引入回归测试。

外部引用标准（以其工作机制 / 工作流程 / 标准为准；多份文档冲突时以最新一份为准）：
  1. 请求参数：chapter（当前章节）/ parent_chapters（上级，保持连贯）/
     sibling_chapters（同级，避免重复）/ project_overview（项目概述）；
  2. SystemPrompt 六条要求（专业准确 / 朴实不假大空 / 正式不像 AI /
     详细不空泛 / 避免同级重复 / 只返回内容不生成标题）；
  3. 上下文拼接关键细节：
     · 字段访问用安全取值，缺字段不抛异常；
     · 上下文过长时**按优先级截断，优先保留上级章节与当前章节信息**。

本文件覆盖本轮落地的三项改动：
  A. SystemPrompt 对齐参考六条 + 专项施工方案适配；
  B. 上下文按语义优先级截断（原按长度降序 → 当前章节任务最先被裁，与标准相反）；
  C. 字段安全取值 + 同级章节缓存友好口径。

同级章节口径演进（以外部引用软件《标书智能体（4）——提示词顺序优化》为准）：
  文档（3）要求「同级章节排除当前章节，否则 AI 自我参考」；文档（4）进一步
  指出该做法导致同一父章节下每个叶子的 sibling_context 都不同、前缀缓存只能
  部分命中，改进为：**传入完整兄弟列表（含当前章节）**，在任务说明中写
  「请避免与除当前章节外的其他章节内容重复」。本文件的同级章节守卫已按
  文档（4）口径更新（见 TestSiblingInclusionAndSafeAccess）。
"""

import pytest

import app.config as app_config
from app.services.ai.prompts.content import CONTENT_SECTION_SYSTEM
from app.services.ai.providers.base import AIMessage
import app.services.ai.workflows_sections as sections_mod
from app.services.ai.workflows_sections import (
    _context_message_priority,
    build_chapter_content_messages,
)


def _text(messages) -> str:
    out = []
    for m in messages:
        if isinstance(m, dict):
            out.append(str(m.get("content") or ""))
        else:
            out.append(str(getattr(m, "content", "") or ""))
    return "\n".join(out)


# ---------------------------------------------------------------------------
# A. SystemPrompt 对齐参考六条
# ---------------------------------------------------------------------------
class TestSectionSystemPromptAlignment:

    def test_reference_six_requirements_present(self):
        """参考文档 SystemPrompt 六条要求逐条可检出。"""
        required = [
            "内容要专业、准确，与章节标题和描述保持一致",   # 第 1 条
            "朴实无华，不假大空",                            # 第 2 条
            "不让人觉得内容像是AI生成的",                    # 第 3 条
            "内容要详细具体，避免空泛的描述",                # 第 4 条
            "注意避免与同级章节内容重复",                    # 第 5 条
            "直接返回章节内容，不生成标题",                  # 第 6 条
        ]
        for kw in required:
            assert kw in CONTENT_SECTION_SYSTEM, f"缺少参考要求：{kw}"

    def test_special_plan_standard_block(self):
        """专项施工方案适配 + 九大章节标准（只展开本章要素，不得越界）。"""
        assert "【专项方案标准】" in CONTENT_SECTION_SYSTEM
        assert "九大章节标准" in CONTENT_SECTION_SYSTEM
        assert "不得越界撰写其他章节的内容" in CONTENT_SECTION_SYSTEM

    def test_fact_iron_rule_line_preserved(self):
        """【事实铁律】必须整行保留（既有事实补丁修复模式测试按行首匹配）。"""
        rule = next(
            line for line in CONTENT_SECTION_SYSTEM.splitlines()
            if line.startswith("【事实铁律】")
        )
        for kw in (
            "必须优先使用变量值",
            "不得前后矛盾",
            "仅使用本章节提供的事实变量",
            "不要主动编造具体人员、周期、质保、品牌、型号",
            "一字不差",
            "改用定性描述",
        ):
            assert kw in rule

    def test_existing_enhancement_blocks_kept(self):
        """本软件增强块不得因对齐参考而丢失。"""
        for kw in (
            "【正文纯净性】",
            "【表格规范】",
            "【智能配图规范】",
            "【配图铁律】",
        ):
            assert kw in CONTENT_SECTION_SYSTEM


# ---------------------------------------------------------------------------
# C. 同级章节含当前章节（缓存友好口径）+ 字段安全取值
#    依据外部引用软件《标书智能体（4）——提示词顺序优化》关键改进：
#    完整兄弟列表（含当前章节）+ 任务说明显式排除当前章节。
# ---------------------------------------------------------------------------
class TestSiblingInclusionAndSafeAccess:

    def test_current_chapter_included_in_siblings_with_exclusion_instruction(self):
        """同级列表包含当前章节，且消息头显式声明「避免与除当前章节外重复」。"""
        msgs = build_chapter_content_messages(
            {"id": "2.2", "title": "当前章", "level": 3},
            sibling_chapters=[
                {"id": "2.1", "title": "同级甲", "description": "描述甲"},
                {"id": "2.2", "title": "当前章", "description": "自我参考描述"},
                {"id": "2.3", "title": "同级乙", "description": "描述乙"},
            ],
        )
        sibling_msg = next(
            m for m in msgs
            if str(getattr(m, "content", "")).startswith("同级章节")
        )
        text = sibling_msg.content
        # 文档（4）：完整兄弟列表（含当前章节）——缓存前缀跨叶子一致的前提
        assert "同级甲" in text and "同级乙" in text and "当前章" in text
        # 自我引用由显式指令约束，而非数据层过滤
        assert "请避免与除当前章节外的其他章节内容重复" in text

    def test_current_chapter_included_when_chapter_info_carries_siblings(self):
        """chapter_info 内嵌 sibling_chapters 时同样保留当前章节。"""
        msgs = build_chapter_content_messages(
            {
                "id": "3.1",
                "title": "第三章内容",
                "level": 3,
                "sibling_chapters": [
                    {"id": "3.1", "title": "第三章内容", "description": "自我描述"},
                    {"id": "3.2", "title": "兄弟章", "description": "兄弟描述"},
                ],
            },
        )
        text = _text(msgs)
        assert "兄弟章" in text
        assert "自我描述" in text  # 含当前章节（不再数据层排除）

    def test_sibling_context_identical_across_siblings(self):
        """缓存核心保证：同一父章节下不同叶子的 sibling_context 字节级一致。

        旧实现按叶子排除当前章节 → 每个叶子的同级列表都不同 → 前缀缓存只能
        部分命中（文档（4）指出的缺陷）。现在 build 层不做任何按叶子过滤，
        同父章节共用同一列表时渲染结果必须逐字节相同。
        """
        shared_siblings = [
            {"id": "2.1", "title": "同级甲", "description": "描述甲"},
            {"id": "2.2", "title": "当前章", "description": "自我参考描述"},
            {"id": "2.3", "title": "同级乙", "description": "描述乙"},
        ]

        def _sibling_msg(section_id: str) -> str:
            msgs = build_chapter_content_messages(
                {"id": section_id, "title": f"章节{section_id}", "level": 3},
                sibling_chapters=shared_siblings,
            )
            return next(
                m.content for m in msgs
                if str(getattr(m, "content", "")).startswith("同级章节")
            )

        assert _sibling_msg("2.1") == _sibling_msg("2.2") == _sibling_msg("2.3")

    def test_missing_or_none_fields_do_not_raise(self):
        """安全取值：None 标题 / 缺字段 / 非 dict 项均不抛异常、不渲染 'None'。"""
        msgs = build_chapter_content_messages(
            {"id": "1.1", "title": "首章", "level": 3},
            parent_chapters=[{"title": None}, {"description": "仅描述"}, "脏数据"],
            sibling_chapters=[{"title": None}, {}, {"id": "x", "title": "有效同级"}],
        )
        text = _text(msgs)
        assert "有效同级" in text
        assert "None" not in text

    def test_sibling_without_current_id_kept(self):
        """当前章节无 id 时不做误删（无法判定即保留）。"""
        msgs = build_chapter_content_messages(
            {"title": "无 id 章", "level": 3},
            sibling_chapters=[{"id": "a", "title": "同级甲", "description": "d"}],
        )
        assert "同级甲" in _text(msgs)


# ---------------------------------------------------------------------------
# B. 上下文按优先级截断
# ---------------------------------------------------------------------------
class TestContextPriorityTruncation:

    def test_priority_tiers(self):
        assert _context_message_priority("# 章节: 某章\n内容") == 9
        assert _context_message_priority("上级章节（承接关系）:\n  - 父") == 8
        assert _context_message_priority("同级章节（请避免内容重复）:\n  - 同") == 0
        assert _context_message_priority("事实变量:\n某事实") == 1
        assert _context_message_priority("全篇叙事骨架（跨章承接约束）:\n骨架") == 3
        # 未知段落落在中等优先级，绝不落入「永不裁剪」级
        assert _context_message_priority("某未识别段") < 9

    def test_current_chapter_and_parents_kept_when_referencing_overflow(self, monkeypatch):
        """同级/事实段足以吸收溢出时，上级章节与当前章节任务原文保留。"""
        monkeypatch.setattr(app_config, "CONTENT_CONTEXT_LIMIT_CHARS", 12000)
        monkeypatch.setattr(app_config, "CONTENT_CONTEXT_DEGRADE_RATIO", 0.7)
        msgs = [
            AIMessage(role="system", content="铁律" * 50),
            AIMessage(role="user", content="# 章节: 当前章\n" + "内" * 600),
            AIMessage(role="user", content="上级章节（承接关系，请与之呼应而非重复）:\n  - 父章\n" + "父" * 600),
            AIMessage(role="user", content="同级章节（请避免内容重复，突出本章节独特性）:\n  - 同级甲\n" + "同" * 6000),
            AIMessage(role="user", content="事实变量:\n" + "实" * 6000),
        ]
        out = sections_mod._degrade_long_context_messages(msgs, {"id": "1"})
        assert out[1].content == msgs[1].content          # 当前章节任务：绝不裁剪
        assert "已截断" not in out[2].content             # 上级章节：最后才裁
        assert "已截断" in out[3].content                 # 同级章节：最先裁
        assert "已截断" in out[4].content                 # 事实变量：次先裁

    def test_longest_current_chapter_not_truncated_first(self, monkeypatch):
        """回归：旧实现按长度降序裁剪 → 最长的当前章节任务最先被裁。

        参考标准要求「优先保留上级章节与当前章节信息」，本用例钉死该行为。
        """
        monkeypatch.setattr(app_config, "CONTENT_CONTEXT_LIMIT_CHARS", 2000)
        monkeypatch.setattr(app_config, "CONTENT_CONTEXT_DEGRADE_RATIO", 0.7)
        msgs = [
            AIMessage(role="system", content="铁律" * 50),
            AIMessage(role="user", content="# 章节: 当前章\n" + "内" * 5000),
            AIMessage(role="user", content="同级章节（请避免内容重复，突出本章节独特性）:\n  - 同级甲\n" + "同" * 1000),
        ]
        out = sections_mod._degrade_long_context_messages(msgs, {"id": "1"})
        assert out[1].content == msgs[1].content          # 当前章节任务完整保留
        assert "已截断" in out[2].content

    def test_system_message_never_touched(self, monkeypatch):
        monkeypatch.setattr(app_config, "CONTENT_CONTEXT_LIMIT_CHARS", 2000)
        monkeypatch.setattr(app_config, "CONTENT_CONTEXT_DEGRADE_RATIO", 0.7)
        sys_before = "铁律" * 50
        msgs = [
            AIMessage(role="system", content=sys_before),
            AIMessage(role="user", content="资料" * 3000),
        ]
        out = sections_mod._degrade_long_context_messages(msgs, {"id": "1"})
        assert out[0].content == sys_before

    def test_degrade_flag_default_off(self):
        """降级总开关默认保持关闭（灰度口径不变）。"""
        assert app_config.CONTENT_CONTEXT_DEGRADE_ENABLED is False

    def test_real_built_messages_classified_correctly(self):
        """集成护栏：真实构建出的消息段 → 优先级分类必须正确。

        防止未来改动消息段前缀（如「# 章节:」）后，「当前章节永不裁剪」的
        保证静默失效。
        """
        msgs = build_chapter_content_messages(
            {"id": "1.1", "title": "某章节", "level": 3},
            scoring_context="评分要点原文",
            sibling_chapters=[{"id": "1.2", "title": "兄弟章", "description": "d"}],
            parent_chapters=[{"id": "1", "title": "父章", "description": "d"}],
        )
        by_prefix = {}
        for m in msgs:
            c = str(getattr(m, "content", "") or "")
            for head in ("# 章节: ", "上级章节", "同级章节", "事实变量:"):
                if c.startswith(head):
                    by_prefix[head] = c
                    break
        assert "# 章节: " in by_prefix, "未找到当前章节任务段"
        assert by_prefix["# 章节: "].startswith("# 章节: 某章节")
        assert _context_message_priority(by_prefix["# 章节: "]) == 9
        assert "同级章节" in by_prefix
        assert _context_message_priority(by_prefix["同级章节"]) == 0
        assert "上级章节" in by_prefix
        assert _context_message_priority(by_prefix["上级章节"]) == 8


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
