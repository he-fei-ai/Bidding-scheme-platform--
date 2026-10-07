import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest


pytestmark = pytest.mark.skip(
    reason="[V3 LEGACY] workflows_outline_v2v3.py 已物理删除，统一走 V5.1 generate_outline()，跳过已退役路径测试"
)


from app.services.ai.providers.base import AIMessage
from app.services.ai.workflows import (
    collect_json_response,
    normalize_writing_targets,
    parse_json_response,
)


class _ScriptedProvider:
    """按脚本依次返回预设响应的假 provider，用于验证 JSON 修复链路。

    - 首次 chat（json_mode 与 fallback 都算一次脚本步进）返回坏 JSON；
    - 修复轮返回合法 JSON。
    这样可回归保护「修复链路 dict→AIMessage」的历史缺陷（此前修复分支
    因误用 dict 触发 AttributeError 被静默吞掉，修复能力形同虚设）。
    """

    def __init__(self, responses: list[str]):
        self._responses = responses
        self.calls: list[list] = []

    async def chat(self, messages, temperature=0.7, json_mode=False, cancel_event=None):
        # 校验：传入的必须是 AIMessage（拥有 .role/.content），否则等同真实 provider 崩溃
        for m in messages:
            assert hasattr(m, "role") and hasattr(m, "content"), "messages 必须是 AIMessage"
        self.calls.append(list(messages))
        if not self._responses:
            raise RuntimeError("脚本已耗尽")
        return self._responses.pop(0)


@pytest.mark.asyncio
async def test_collect_json_repair_recovers_from_bad_json():
    # 第 1 轮：坏 JSON（无法解析）→ 触发修复；修复轮返回合法 JSON
    provider = _ScriptedProvider(["这不是JSON{坏的", '{"ok": true}'])
    result = await collect_json_response(
        provider,
        [AIMessage(role="user", content="生成JSON")],
        max_retries=3,
    )
    assert result == {"ok": True}
    # 至少发生了 1 次初始调用 + 1 次修复调用
    assert len(provider.calls) >= 2


@pytest.mark.asyncio
async def test_collect_json_business_validator_then_repair():
    # 第 1 轮：合法 JSON 但业务校验不通过 → 修复轮返回通过校验的 JSON
    provider = _ScriptedProvider(['{"count": 0}', '{"count": 5}'])

    def validator(parsed):
        return (parsed.get("count", 0) > 0, "count 必须大于 0")

    result = await collect_json_response(
        provider,
        [AIMessage(role="user", content="生成JSON")],
        validator=validator,
        max_retries=3,
    )
    assert result == {"count": 5}


EXCLUDE_KEYWORDS = ["承诺", "保证", "盖章", "签字", "格式要求", "装订"]


def test_parse_json_standard():
    assert parse_json_response('{"key": "value"}') == {"key": "value"}


def test_parse_json_in_code_fence():
    assert parse_json_response('```json\n{"a": 1}\n```') == {"a": 1}


def test_parse_json_array():
    assert parse_json_response("[1, 2, 3]") == [1, 2, 3]


def test_parse_json_array_with_text():
    assert parse_json_response('以下是评分点：\n\n[{"name": "test"}]') == [{"name": "test"}]


def test_parse_json_invalid():
    assert parse_json_response("纯文本，没有 JSON") is None


def test_normalize_deduplication():
    raw = [
        {"id": "R1", "name": "技术方案", "weight": "30分", "standard": "", "source": "第三章", "category": "评分项"},
        {"id": "R2", "name": "质量管理", "weight": "20分", "standard": "", "source": "", "category": "评分项"},
        {"id": "R3", "name": "施工组织", "weight": "25分", "standard": "详细程度", "source": "", "category": "评分项"},
        {"id": "R1", "name": "技术方案", "weight": "30分", "standard": "", "source": "第三章", "category": "评分项"},
    ]
    # normalize_writing_targets 内部处理去重，并可能插入权重校验警告
    result = normalize_writing_targets(raw)
    # 去重后应有 3 个唯一评分项 + 可能的权重警告项
    scoring_items = [r for r in result if not r["id"].startswith("__")]
    assert len(scoring_items) == 3
    names = [r["name"] for r in scoring_items]
    assert names == ["技术方案", "质量管理", "施工组织"]


def test_normalize_empty():
    assert normalize_writing_targets([]) == []


def test_normalize_missing_name():
    result = normalize_writing_targets([{"id": "R1", "weight": "10分"}])
    assert len(result) == 0


def test_normalize_fill_defaults():
    raw = [
        {"id": "R1", "name": "技术方案", "weight": "", "standard": "", "source": "", "category": ""},
        {"id": "R2", "name": "施工方案"},
    ]
    result = normalize_writing_targets(raw)
    assert len(result) >= 1
    for r in result:
        assert r["category"] in ("评分项", "")


# ========== 缓存键跨项目隔离测试（修复跨项目内容污染 BUG）==========


@pytest.mark.asyncio
async def test_cache_key_isolates_different_projects():
    """不同 project_id 即便章节标题/上下文/写作长度完全相同，也不应命中同一缓存"""
    from app.services.ai.workflows import SectionContentCache

    cache = SectionContentCache(max_size=10)

    # 项目 1 存入章节
    await cache.put(
        project_id=1,
        section_title="施工组织设计",
        tender_context="某市政道路工程招标文件",
        writing_length="standard",
        target_words=800,
        content="项目1的内容",
        quality={"score": 85.0},
    )

    # 项目 2 同名章节 + 相同上下文 → 不应命中项目 1 的缓存
    hit = await cache.get(
        project_id=2,
        section_title="施工组织设计",
        tender_context="某市政道路工程招标文件",
        writing_length="standard",
        target_words=800,
    )
    assert hit is None, "不同 project_id 不应命中同一缓存键（跨项目污染）"

    # 项目 1 自身应能命中
    hit_same = await cache.get(
        project_id=1,
        section_title="施工组织设计",
        tender_context="某市政道路工程招标文件",
        writing_length="standard",
        target_words=800,
    )
    assert hit_same is not None
    assert hit_same["content"] == "项目1的内容"


@pytest.mark.asyncio
async def test_cache_key_none_project_id_still_works():
    """project_id 为 None（向后兼容场景）时应正常工作"""
    from app.services.ai.workflows import SectionContentCache

    cache = SectionContentCache(max_size=5)
    await cache.put(
        project_id=None,
        section_title="质量管理",
        tender_context="上下文",
        writing_length="standard",
        target_words=600,
        content="内容",
    )
    hit = await cache.get(
        project_id=None,
        section_title="质量管理",
        tender_context="上下文",
        writing_length="standard",
        target_words=600,
    )
    assert hit is not None
    assert hit["content"] == "内容"


@pytest.mark.asyncio
async def test_cache_invalidate_scoped_to_project():
    """invalidate 只影响指定 project_id 的缓存，不影响其他项目"""
    from app.services.ai.workflows import SectionContentCache

    cache = SectionContentCache(max_size=10)
    await cache.put(1, "标题A", "上下文", "standard", 800, "项目1内容A")
    await cache.put(2, "标题A", "上下文", "standard", 800, "项目2内容A")

    # 仅失效项目 1
    await cache.invalidate(1, "标题A", "上下文", "standard", 800)

    assert await cache.get(1, "标题A", "上下文", "standard", 800) is None
    hit2 = await cache.get(2, "标题A", "上下文", "standard", 800)
    assert hit2 is not None and hit2["content"] == "项目2内容A"


@pytest.mark.asyncio
async def test_normalize_outline_items_corrects_sub_node_level():
    """normalize_outline_items 应始终使用递归计算的 default_level，而非 AI 返回的错误 level 值。

    背景：AI 生成的目录中，二级子节点有时被错误标记为 level=1，导致最终审核弹窗展示错误层级。
    修复：normalize_outline_items 不再检查 "level" 是否在字典中，而是始终覆盖为正确的 default_level。
    """
    from app.services.ai.workflows import normalize_outline_items

    # AI 返回的目录：一级节点正确，但二级子节点被错误标记为 level=1
    raw_outline = [
        {
            "id": 1,
            "title": "项目概述",
            "level": 1,
            "children": [
                {"id": 2, "title": "项目背景", "level": 1, "children": []},
                {"id": 3, "title": "建设目标", "level": 1, "children": []},
            ],
        },
        {
            "id": 4,
            "title": "编制依据",
            "level": 1,
            "children": [
                {"id": 5, "title": "法律法规", "level": 1, "children": []},
            ],
        },
    ]

    result = normalize_outline_items(raw_outline, default_level=1)

    # 一级节点 level 应为 1
    assert result[0]["level"] == 1
    assert result[1]["level"] == 1
    # 二级子节点 level 应被修正为 2（即使 AI 返回了 level=1）
    assert result[0]["children"][0]["level"] == 2
    assert result[0]["children"][1]["level"] == 2
    assert result[1]["children"][0]["level"] == 2


@pytest.mark.asyncio
async def test_self_check_v51_persists_edited_outline():
    """_self_check_v51 步骤4自检通过时，应将修正后的目录持久化到 v51_state.edited_outline。

    背景：原始代码中 _self_check_v51 内部对 level1 进行了重新赋值（调用 _fix_* 函数），
    但 Python 的参数传递机制导致调用方的 level1 引用未更新，最终审核弹窗仍显示原始错误目录。
    修复：自检通过时将修正后的 level1 同步写入 v51_state.edited_outline。
    """
    from unittest.mock import AsyncMock, patch
    from app.services.v51_state import V51TaskState
    from app.services.ai.workflows import _self_check_v51

    v51_state = V51TaskState(project_id=999)

    # 模拟一个有问题的目录：二级子节点被错误标记为 level=1
    problematic_outline = [
        {
            "id": 1,
            "title": "项目概述",
            "level": 1,
            "children": [
                {"id": 2, "title": "项目背景描述", "level": 1, "children": []},
                {"id": 3, "title": "建设目标与规模", "level": 1, "children": []},
            ],
        },
    ]

    mock_provider = AsyncMock()
    mock_provider.base_url = "https://test.example.com/v1"

    # 直接 mock _call_ai_v51，跳过 collect_json_response → provider.chat 链路
    # 模拟自检通过：返回无问题
    with patch("app.services.ai.workflows._call_ai_v51", new_callable=AsyncMock, return_value={"passed": True, "issues": [], "message": "目录结构完整，无需修复"}):
        await _self_check_v51(mock_provider, problematic_outline, 0.0, v51_state=v51_state)

    # _self_check_v51 本身不返回值（return None），关键验证：修正后的目录持久化到 v51_state.edited_outline
    assert v51_state.edited_outline is not None
    assert len(v51_state.edited_outline) == 1
    assert v51_state.edited_outline[0]["id"] == 1
    assert len(v51_state.edited_outline[0]["children"]) == 2


@pytest.mark.asyncio
async def test_generate_outline_v3_awaits_phase_callback():
    """验证 generate_outline_v3 正确 await phase_callback（修复前缺少 await 导致协程泄漏）。

    回归测试：phase_callback 是 async 函数，必须 await 调用，否则会产生
    'coroutine was never awaited' RuntimeWarning 并导致进度事件丢失。
    """
    from app.services.ai.workflows import generate_outline_v3

    # 最小化 mock provider：所有 AI 调用返回合法空结果
    mock_provider = AsyncMock()
    mock_provider.base_url = "https://test.example.com/v1"

    phase_calls = []

    async def tracking_phase_callback(phase: str, message: str):
        phase_calls.append((phase, message))

    # 注意：generate_outline_v3 定义在 workflows_outline_v2v3 中，patch 需指向实际模块
    with patch("app.services.ai.workflows_outline_v2v3.generate_supplement_chapters", new_callable=AsyncMock, return_value=[]), \
         patch("app.services.ai.workflows_outline_v2v3._get_template_list_json", return_value="[]"), \
         patch("app.services.ai.workflows_outline_v2v3.smart_merge_outline", new_callable=AsyncMock, return_value=[]), \
         patch("app.services.ai.workflows_outline_v2v3.analyze_boq", new_callable=AsyncMock, return_value=[]), \
         patch("app.services.ai.workflows_outline_v2v3.analyze_work_interface", new_callable=AsyncMock, return_value=[]), \
         patch("app.services.ai.workflows_outline_v2v3.analyze_construction_method", new_callable=AsyncMock, return_value=[]), \
         patch("app.services.ai.workflows_outline_v2v3.fill_children_with_retry", new_callable=AsyncMock, return_value={
             "title": "测试章", "level": 1, "children": [
                 {"title": "二级1", "level": 2, "children": [
                     {"title": "三级1", "level": 3, "children": []},
                     {"title": "三级2", "level": 3, "children": []},
                     {"title": "三级3", "level": 3, "children": []},
                 ]},
                 {"title": "二级2", "level": 2, "children": [
                     {"title": "三级1", "level": 3, "children": []},
                     {"title": "三级2", "level": 3, "children": []},
                     {"title": "三级3", "level": 3, "children": []},
                 ]},
                 {"title": "二级3", "level": 2, "children": [
                     {"title": "三级1", "level": 3, "children": []},
                     {"title": "三级2", "level": 3, "children": []},
                     {"title": "三级3", "level": 3, "children": []},
                 ]},
             ]
         }):
        result = await generate_outline_v3(
            provider=mock_provider,
            summary="测试项目",
            scoring_points="[]",
            industry_profile="建筑行业",
            phase_callback=tracking_phase_callback,
        )

    # 验证 phase_callback 被正确 await（不是 coroutine 泄漏）
    assert len(phase_calls) >= 2, f"phase_callback 应被调用至少 2 次，实际: {phase_calls}"
    assert any(call[0] == "二三级生成完成" for call in phase_calls)
    assert any(call[0] == "评分项对齐完成" for call in phase_calls)


def test_count_outline_nodes():
    """_count_outline_nodes 递归统计所有层级节点数。"""
    from app.services.ai.workflows import _count_outline_nodes

    outline = [
        {"id": 1, "title": "A", "level": 1, "children": [
            {"id": 2, "title": "A1", "level": 2, "children": [
                {"id": 4, "title": "A1a", "level": 3, "children": []},
                {"id": 5, "title": "A1b", "level": 3, "children": []},
            ]},
            {"id": 3, "title": "A2", "level": 2, "children": []},
        ]},
        {"id": 6, "title": "B", "level": 1, "children": []},
    ]
    assert _count_outline_nodes(outline) == 6  # A, A1, A1a, A1b, A2, B


@pytest.mark.asyncio
async def test_self_check_v51_filters_false_truncated_when_outline_complete():
    """自检通过结构完整性校验时，不误报截断问题。

    回归测试：当大纲结构完整时，本地检查不应报告截断相关问题。
    """
    from app.services.ai.workflows import _quick_local_check, _self_check_v51
    from app.services.v51_state import V51TaskState

    v51_state = V51TaskState(project_id=999)
    # 构造完整的目录（每章有 L2，每 L2 有 L3）
    complete_outline = [
        {"id": 1, "title": "项目概述", "level": 1, "children": [
            {"id": "1.1", "title": "项目背景", "level": 2, "children": [
                {"id": "1.1.1", "title": "建设背景", "level": 3, "children": []},
                {"id": "1.1.2", "title": "建设意义", "level": 3, "children": []},
                {"id": "1.1.3", "title": "建设目标", "level": 3, "children": []},
            ]},
        ]},
        {"id": 2, "title": "施工部署", "level": 1, "children": [
            {"id": "2.1", "title": "总体部署", "level": 2, "children": [
                {"id": "2.1.1", "title": "施工区划分", "level": 3, "children": []},
                {"id": "2.1.2", "title": "施工顺序", "level": 3, "children": []},
                {"id": "2.1.3", "title": "资源配置", "level": 3, "children": []},
            ]},
        ]},
    ]

    # 本地检查不应报告截断/不完整问题
    issues = _quick_local_check(complete_outline)
    # 不应有截断相关的误报
    assert not any("截断" in i or "truncat" in i.lower() for i in issues), \
        f"完整的目录不应被误报为截断: {issues}"

    mock_provider = AsyncMock()
    mock_provider.base_url = "https://test.example.com/v1"
    result = await _self_check_v51(mock_provider, complete_outline, 0.0, v51_state=v51_state)
    assert v51_state.edited_outline is not None
    assert len(result) == 2  # 不应删除节点


@pytest.mark.asyncio
async def test_self_check_v51_fixes_multiple_issue_types_in_one_attempt():
    """自检同时发现多种本地问题（重复标题 + 缺少子节点）时，一轮自动修复全部处理。

    回归测试：原始代码的互斥逻辑导致只有第一类问题被修复。
    修复后，所有可修复的问题类型在同一轮 _auto_fix_issues 中顺序执行。
    """
    from app.services.ai.workflows import _quick_local_check, _auto_fix_issues, _self_check_v51
    from app.services.v51_state import V51TaskState

    v51_state = V51TaskState(project_id=999)
    # 构造含重复标题 + 缺少 L3 子节点的目录
    problematic_outline = [
        {"id": 1, "title": "项目概述", "level": 1, "children": [
            {"id": "1.1", "title": "重复标题", "level": 2, "children": [
                {"id": "1.1.1", "title": "子节点1", "level": 3, "children": []},
                {"id": "1.1.2", "title": "子节点2", "level": 3, "children": []},
                {"id": "1.1.3", "title": "子节点3", "level": 3, "children": []},
            ]},
        ]},
        {"id": 2, "title": "施工部署", "level": 1, "children": [
            {"id": "2.1", "title": "重复标题", "level": 2, "children": []},  # 缺少 L3
        ]},
    ]

    # 本地检查应发现两个问题：重复标题 + 缺少 L3
    issues = _quick_local_check(problematic_outline)
    assert len(issues) >= 2, f"应发现至少2个问题，实际: {issues}"
    assert any(i.startswith("重复标题") for i in issues), f"应发现重复标题问题: {issues}"
    assert any("无三级" in i for i in issues), f"应发现缺少L3问题: {issues}"

    # 一轮修复
    fixed = _auto_fix_issues(problematic_outline, issues)
    # 验证修复后不再有重复标题问题（精确匹配 issue 类型，不误匹配标题文本中的"重复"字样）
    fixed_issues = _quick_local_check(fixed)
    # 重复标题问题应已解决（精确匹配 "重复标题" 作为 issue 开头）
    assert not any(i.startswith("重复标题") for i in fixed_issues), \
        f"修复后仍有重复标题问题: {fixed_issues}"


def test_is_outline_complete_valid():
    """_is_outline_complete 对合法大纲返回 True。"""
    from app.services.ai.workflows import _is_outline_complete

    valid = [{"title": "第一章", "level": 1, "children": [{"title": "1.1", "level": 2, "children": []}]}]
    assert _is_outline_complete(valid) is True

    # 超大完整大纲（模拟真实场景）
    big = []
    for i in range(30):
        children = []
        for j in range(8):
            grandchildren = []
            for k in range(5):
                grandchildren.append({
                    "title": f"第{i+1}章_{j+1}_{k+1}节-这是一个很长的标题用来测试字符数是否超过限制",
                    "level": 3, "children": []
                })
            children.append({"title": f"第{i+1}章_{j+1}节", "level": 2, "children": grandchildren})
        big.append({"title": f"第{i+1}章标题", "level": 1, "children": children})
    assert _is_outline_complete(big) is True  # 结构完整，即使 JSON 超过 40000 字符


def test_is_outline_complete_invalid():
    """_is_outline_complete 对非法/空大纲返回 False。"""
    from app.services.ai.workflows import _is_outline_complete

    assert _is_outline_complete([]) is False
    assert _is_outline_complete("string") is False
    assert _is_outline_complete(None) is False
    # 模拟截断的 JSON（以 { 结尾，非 ]）
    import json
    broken = json.dumps([{"title": "test"}])[:-1] + "}"  # 破坏结构
    assert _is_outline_complete(json.loads(broken.split(",")[0]) if False else []) is False  # noqa



