# -*- coding: utf-8 -*-
"""方案类型接入目录选型 + docx_extractor 资源释放 测试（标书智能体（一）引入）。

验证：
- recommend_outline_reference：已知方案类型在通用模板基础上追加类型特有关卡；
  未知/空类型回退通用模板（行为不变）。
- recommend_outline_reference(dangerous_params=...)：危大参数已识别时，为「计算书与图纸」
  「应急处置」增补配套章节，实现《引入方案》第六节2「危大工程判定结果影响这两类章节的深度」。
- _read_scheme_extraction_for_outline：从 analysis.scheme_extraction_json 解析方案类型与
  危大参数；读取失败/缺列/未识别均安全降级为 ("", [])（不中断目录生成）。
- docx_extractor.extract_docx_plain 的 finally 释放资源（del doc + gc.collect）。
"""

from unittest.mock import patch

import json

import app.routers.sse.sse_outline as sse_outline_mod
from app.services.ai.workflows_outline_v51 import (
    REFERENCE_TEMPLATE_V51,
    recommend_outline_reference,
    _apply_reference_source,
    _resolve_reference_json,
)


def _base_titles():
    return [ch["title"] for ch in REFERENCE_TEMPLATE_V51]


def test_recommend_known_type_appends_scheme_chapters():
    ref = recommend_outline_reference("基坑工程")
    base = _base_titles()
    # 通用骨架完整保留
    assert ref[: len(REFERENCE_TEMPLATE_V51)] == REFERENCE_TEMPLATE_V51
    # 追加了基坑工程特有章节
    extra_titles = [ch["title"] for ch in ref[len(REFERENCE_TEMPLATE_V51):]]
    assert "基坑监测方案" in extra_titles
    assert "降水与排水施工" in extra_titles
    # 返回的 id 唯一
    ids = [ch["id"] for ch in ref]
    assert len(ids) == len(set(ids))
    # 不修改通用模板原对象
    assert _base_titles() == base


def test_recommend_other_type_falls_back_to_base():
    ref = recommend_outline_reference("其他")
    assert [ch["title"] for ch in ref] == _base_titles()


def test_recommend_empty_and_unknown_fall_back():
    assert [ch["title"] for ch in recommend_outline_reference("")] == _base_titles()
    assert [ch["title"] for ch in recommend_outline_reference("不存在的类型")] == _base_titles()


class _FakeConn:
    def __init__(self, row):
        self._row = row

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, *a, **k):
        class _Cur:
            def __init__(self, row):
                self._row = row

            def fetchone(self):
                return self._row

        return _Cur(self._row)


def test_recommend_deepens_calc_and_emergency_by_dangerous_params():
    # 危大参数已识别 → 为「计算书与图纸」「应急处置」增补配套章节（第六节2）
    dp = [
        {"param": "开挖深度", "value": "6.5", "unit": "m",
         "threshold": "超过一定规模（≥5m）", "source": "第3.2节"},
        {"param": "脚手架搭设高度", "value": "48", "unit": "m",
         "threshold": "一般危大", "source": "第4.1节"},
    ]
    ref = recommend_outline_reference("", dp)
    base = _base_titles()
    # 通用骨架完整保留
    assert ref[: len(REFERENCE_TEMPLATE_V51)] == REFERENCE_TEMPLATE_V51
    extra_titles = [ch["title"] for ch in ref[len(REFERENCE_TEMPLATE_V51):]]
    assert "基坑工程计算书（支护稳定性与降水验算）" in extra_titles
    assert "基坑坍塌及涌水涌砂应急预案" in extra_titles
    assert "脚手架稳定性计算书" in extra_titles
    assert "脚手架坍塌及高处坠落应急预案" in extra_titles
    # 无方案类型时不追加方案特有关卡，但追加危大深化章节
    assert "基坑监测方案" not in extra_titles
    # 来源标注：危大深化章节标注触发参数名
    extra = {ch["title"]: ch.get("source") for ch in ref[len(REFERENCE_TEMPLATE_V51):]}
    assert extra["基坑工程计算书（支护稳定性与降水验算）"] == "危大参数：开挖深度"
    assert extra["基坑坍塌及涌水涌砂应急预案"] == "危大参数：开挖深度"
    assert extra["脚手架稳定性计算书"] == "危大参数：脚手架搭设高度"
    # 通用 23 章基础章节不带 source 字段
    for ch in ref[: len(REFERENCE_TEMPLATE_V51)]:
        assert "source" not in ch
    assert _base_titles() == base  # 不修改通用模板原对象


def test_recommend_skips_unidentified_dangerous_params():
    # value 为「未提及」的危大参数不触发深化
    dp = [{"param": "开挖深度", "value": "未提及", "threshold": "", "source": ""}]
    assert [ch["title"] for ch in recommend_outline_reference("", dp)] == _base_titles()


def test_recommend_combines_scheme_type_and_dangerous_params():
    dp = [{"param": "起吊重量", "value": "120", "unit": "t",
           "threshold": "超规模危大", "source": "表2"}]
    ref = recommend_outline_reference("起重吊装及安装拆卸工程", dp)
    titles = [ch["title"] for ch in ref]
    # 方案类型特有关卡 + 危大深化章节同时存在
    assert "起重机械选型与布置" in titles
    assert "起重吊装受力计算书" in titles
    assert "起重伤害及物体打击应急预案" in titles
    # 来源标注：方案类型章节与危大深化章节各标其来源
    src = {ch["title"]: ch.get("source") for ch in ref}
    assert src["起重机械选型与布置"] == "方案类型：起重吊装及安装拆卸工程"
    assert src["起重吊装受力计算书"] == "危大参数：起吊重量"


def test_detect_scheme_type_parses_json():
    payload = '{"scheme_type":[{"scheme_type":"模板工程及支撑体系","confidence":"高"}]}'
    with patch.object(
        sse_outline_mod, "get_db", return_value=_FakeConn((payload,))
    ):
        scheme, dp = sse_outline_mod._read_scheme_extraction_for_outline(1)
    assert scheme == "模板工程及支撑体系"
    assert dp == []


def test_detect_scheme_extraction_parses_dangerous_params():
    payload = ('{"scheme_type":[{"scheme_type":"基坑工程"}],'
               '"dangerous_work_params":[{"param":"开挖深度","value":"6.5"}]}')
    with patch.object(
        sse_outline_mod, "get_db", return_value=_FakeConn((payload,))
    ):
        scheme, dp = sse_outline_mod._read_scheme_extraction_for_outline(1)
    assert scheme == "基坑工程"
    assert dp == [{"param": "开挖深度", "value": "6.5"}]


def test_detect_scheme_type_empty_when_no_row():
    with patch.object(sse_outline_mod, "get_db", return_value=_FakeConn(None)):
        scheme, dp = sse_outline_mod._read_scheme_extraction_for_outline(1)
    assert scheme == ""
    assert dp == []


def test_detect_scheme_type_silent_on_read_error():
    with patch.object(
        sse_outline_mod, "get_db", side_effect=RuntimeError("db down")
    ):
        scheme, dp = sse_outline_mod._read_scheme_extraction_for_outline(1)
    assert scheme == ""
    assert dp == []


def test_detect_scheme_type_handles_bad_json():
    with patch.object(
        sse_outline_mod, "get_db", return_value=_FakeConn(("not-json",))
    ):
        scheme, dp = sse_outline_mod._read_scheme_extraction_for_outline(1)
    assert scheme == ""
    assert dp == []


def test_resolve_reference_json_keeps_source():
    # 解析器应把参考目录的 source 标注一并保留进提示词参考段
    ref = recommend_outline_reference(
        "基坑工程", [{"param": "开挖深度", "value": "6.5", "unit": "m",
                      "threshold": "超规模", "source": "3.2"}]
    )
    resolved = json.loads(_resolve_reference_json(json.dumps(ref, ensure_ascii=False)))
    src = {ch["title"]: ch.get("source") for ch in resolved}
    assert src.get("基坑监测方案") == "方案类型：基坑工程"
    assert src.get("基坑工程计算书（支护稳定性与降水验算）") == "危大参数：开挖深度"
    # 基座章节无 source 字段
    base = [c for c in resolved if c["title"] == "项目概述"][0]
    assert "source" not in base


def test_apply_reference_source_attaches_to_matching_l1():
    # 把参考目录来源透传到生成后的一级节点：标题匹配即标注，未匹配/基座不变
    ref = recommend_outline_reference(
        "基坑工程", [{"param": "开挖深度", "value": "6.5", "unit": "m",
                      "threshold": "超规模", "source": "3.2"}]
    )
    ref_json = json.dumps(ref, ensure_ascii=False)
    level1 = [
        {"id": "1", "title": "项目概述", "children": []},
        {"id": "2", "title": "基坑监测方案及报警制度", "children": []},
        {"id": "3", "title": "基坑工程计算书", "children": []},  # 匹配危大深化（参考标题更长）
        {"id": "4", "title": "完全无关的章节", "children": []},
    ]
    _apply_reference_source(level1, ref_json)
    src = {n["title"]: n.get("source") for n in level1}
    # 方案类型特有关卡（种子被生成标题包含）→ 标来源
    assert src["基坑监测方案及报警制度"] == "方案类型：基坑工程"
    # 危大深化章节（生成标题被参考标题包含）→ 标来源
    assert src["基坑工程计算书"] == "危大参数：开挖深度"
    # 基座章节、无关章节不变（无 source 字段 → 取 None）
    assert src.get("项目概述") is None
    assert src.get("完全无关的章节") is None


def test_apply_reference_source_does_not_overwrite_existing():
    ref = recommend_outline_reference("基坑工程")
    ref_json = json.dumps(ref, ensure_ascii=False)
    level1 = [{"id": "1", "title": "基坑监测方案", "source": "已有来源"}]
    _apply_reference_source(level1, ref_json)
    # 已有 source 不被覆盖
    assert level1[0]["source"] == "已有来源"


def test_apply_reference_source_noop_without_source_map():
    # 无 source 的参考目录（通用模板）透传时不写入任何 source
    level1 = [{"id": "1", "title": "项目概述"}, {"id": "2", "title": "基坑监测方案"}]
    _apply_reference_source(level1, json.dumps(REFERENCE_TEMPLATE_V51, ensure_ascii=False))
    for n in level1:
        assert "source" not in n
