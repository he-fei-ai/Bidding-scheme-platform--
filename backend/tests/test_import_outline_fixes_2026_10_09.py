"""上传解析（import）与生成大纲模块 2026-10-09 修复批次回归测试。

本轮探查确认并修复的缺陷（每项均为「可复现 → 修复后有断言」）：

I-1  格式误判 / 字段错位（根因）：_do_extract_text 的 OLE 降级链入口条件
     `ext in (declared_ext, X if not declared_ext else "")` 的 `if/else` 只作用于
     元组第二个元素 —— declared_ext 非空时退化为 `(declared_ext, "")`。
     于是声明 .xls 的 OLE 文件被 _sniff_extension 改写为 ".doc" 后
     （`".doc" in (".xls","")` 为 False）直接落到 `elif ext == ".doc"` 单分支，
     用 Word 提取器解析 Excel 二进制 → 工程量清单表格/数字全错位成乱码，
     且 status 仍为 success（静默）。
I-2  缓存未失效：Celery 异步重解析入口不先清解析缓存，补装依赖/开启 OCR 后
     重解析在 TTL 内命中旧的空串或降级提示串，永远拿不到新结果。
I-3  缓存未失效：clear_project_texts 只删 texts/ 与合并文本缓存，
     _parse_result_cache 仍按原路径保留旧结果，「清空后重提取」在 TTL 内复活旧文本。
I-4  跨项目串改：uploaded_files 的 UPDATE 只按 id 定位，未按 project_id 作用域收敛。
O-1  超长内容崩溃：_int_to_chinese_num(n>=100) 抛 IndexError；
     _normalize_direct_outline 在规模护栏之前补「第X章」前缀 → 整次直出崩溃，
     异常被上层吞掉后静默回退分步路径，白白浪费一次完整 AI 调用。
O-2  编号命名空间统一：_renumber_items 是死代码，落库目录树从不携带 number，
     前端/导出/正文/API 消费方各算一遍。

前端（frontend/src）因本环境未安装 node，无法执行 vitest；跨模块数据传递契约
（SSE outline_complete 的节点字段集）由下方 TestSavedOutlineCarriesNumber 钉住，
与前端 outlineDictToTreeNodes / annotateOutlineWithNumbers 的期望字段对齐。
"""

import asyncio
import inspect
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.main import app  # noqa: E402
from app.services import files as files_mod  # noqa: E402

OLE2_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


@pytest.fixture()
def client():
    return TestClient(app)


@pytest.fixture()
def project_id(client):
    resp = client.post(
        "/api/v1/projects",
        json={"name": "修复专项20261009项目", "industry": "公共建筑", "bidder_name": "测试投标方"},
    )
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["id"]


@pytest.fixture(autouse=True)
def _clean_parse_cache():
    """解析结果缓存是模块级全局态，隔离测试之间的相互污染。"""
    files_mod._clear_parse_cache()
    yield
    files_mod._clear_parse_cache()


# ============================================================================
# I-1：OLE 降级链入口按「声明扩展名」判定向
# ============================================================================
class TestOleChainEntryByDeclaredExt:
    """声明为 .xls/.ppt/.wps 的 OLE2 文件必须进入降级链，不得只用 .doc 单分支解析。"""

    @staticmethod
    def _mk(calls, label, ret):
        def _h(fp):
            calls.append(label)
            return ret
        return _h

    def test_declared_xls_enters_chain_and_tries_xls(self, monkeypatch, tmp_path):
        p = tmp_path / "工程量清单.xls"
        p.write_bytes(OLE2_MAGIC + b"\x00" * 64)
        calls: list = []
        monkeypatch.setattr(files_mod, "_extract_doc", self._mk(calls, "doc", ""))
        monkeypatch.setattr(files_mod, "_extract_xls", self._mk(calls, "xls", "工作表: 单价1|合价2"))
        monkeypatch.setattr(files_mod, "_extract_ppt", self._mk(calls, "ppt", ""))
        monkeypatch.setattr(files_mod, "_extract_wps", self._mk(calls, "wps", ""))
        # 与生产同链路：走完整 extract_text_from_path（含魔数嗅探改写 ext 的步骤）
        out = files_mod.extract_text_from_path(str(p))
        assert out == "工作表: 单价1|合价2"
        # 关键断言：xls 分支被触发（修复前只有 doc，xls 永不可达）
        assert calls == ["doc", "xls"], calls

    def test_declared_wps_enters_chain_via_full_pipeline(self, monkeypatch, tmp_path):
        p = tmp_path / "施工组织设计.wps"
        p.write_bytes(OLE2_MAGIC + b"\x00" * 64)
        calls: list = []
        monkeypatch.setattr(files_mod, "_extract_doc", self._mk(calls, "doc", ""))
        monkeypatch.setattr(files_mod, "_extract_xls", self._mk(calls, "xls", ""))
        monkeypatch.setattr(files_mod, "_extract_ppt", self._mk(calls, "ppt", ""))
        monkeypatch.setattr(files_mod, "_extract_wps", self._mk(calls, "wps", "WPS正文"))
        out = files_mod.extract_text_from_path(str(p))
        assert out == "WPS正文"
        assert "wps" in calls, calls

    def test_ooxml_declared_still_uses_single_branch(self, monkeypatch, tmp_path):
        """伪装护栏保持：声明 .docx 但实为 OLE2 的文件不进链，避免白等 PowerPoint COM。"""
        p = tmp_path / "伪装.docx"
        p.write_bytes(OLE2_MAGIC + b"\x00" * 64)
        calls: list = []
        monkeypatch.setattr(files_mod, "_extract_doc", self._mk(calls, "doc", "正文"))
        monkeypatch.setattr(files_mod, "_extract_xls", self._mk(calls, "xls", ""))
        monkeypatch.setattr(files_mod, "_extract_ppt", self._mk(calls, "ppt", ""))
        monkeypatch.setattr(files_mod, "_extract_wps", self._mk(calls, "wps", ""))
        out = files_mod.extract_text_from_path(str(p))
        assert out == "正文"
        assert calls == ["doc"], calls

    def test_non_ole_file_never_enters_chain(self, monkeypatch, tmp_path):
        p = tmp_path / "探测.ppt"
        p.write_bytes(b"probe")
        calls: list = []
        monkeypatch.setattr(files_mod, "_extract_doc", self._mk(calls, "doc", ""))
        monkeypatch.setattr(files_mod, "_extract_xls", self._mk(calls, "xls", ""))
        monkeypatch.setattr(
            files_mod, "_extract_ppt", lambda fp: files_mod.EXTRACT_PPT_NOTICE
        )
        out = files_mod._do_extract_text(str(p), ".ppt", ".ppt")
        assert calls == [], calls
        assert out == files_mod.EXTRACT_PPT_NOTICE


# ============================================================================
# I-2 / I-3：解析缓存失效
# ============================================================================
class TestParseCacheInvalidation:
    TASKS_FILE = BACKEND_DIR / "app" / "tasks" / "__init__.py"

    def _task_source(self) -> str:
        """直接读文件源码：Celery task 代理对象无法稳定用于 inspect.getsource。

        并去掉整行注释，避免「修复注释里引用旧写法」造成假阳性。
        """
        text = self.TASKS_FILE.read_text(encoding="utf-8")
        return "\n".join(
            line for line in text.splitlines() if not line.lstrip().startswith("#")
        )

    def test_async_parse_task_clears_cache_before_extract(self):
        """源码断言：清缓存必须先于解析调用（防后续重构破坏时序）。"""
        src = self._task_source()
        assert "_clear_parse_cache(file_path)" in src
        assert src.index("_clear_parse_cache(file_path)") < src.index(
            "extract_text_from_path(file_path)"
        )

    def test_async_parse_task_actually_invokes_cache_clear(self, monkeypatch, tmp_path):
        """行为断言：任务内真实调用 _clear_parse_cache，且带的是待解析文件路径。"""
        from app.services import files as fmod
        from app.tasks import parse_and_extract_file

        p = tmp_path / "清单.docx"
        p.write_bytes(b"\x00" * 32)
        hits: list = []
        _orig_clear = fmod._clear_parse_cache

        def _spy(fp=""):
            hits.append(str(fp))
            return _orig_clear(fp)

        _fn = parse_and_extract_file.__wrapped__ if hasattr(
            parse_and_extract_file, "__wrapped__"
        ) else parse_and_extract_file
        # 隔离副作用：真实项目/文件不存在，只验证缓存时序
        monkeypatch.setattr(fmod, "_clear_parse_cache", _spy)
        monkeypatch.setattr(fmod, "extract_text_from_path", lambda fp: "新正文")
        monkeypatch.setattr(fmod, "_update_parse_task", lambda *a, **k: None)
        monkeypatch.setattr(fmod, "_create_parse_task", lambda *a, **k: None)
        monkeypatch.setattr(fmod, "_text_cache_clear", lambda pid: None)

        try:
            _fn(str(p), 999999, 1, create_task_row=False)
        except Exception:
            pass  # 任务后续写库/落盘失败不影响本用例的断言目标

        assert hits == [str(p)], hits

    def test_clear_project_texts_invalidates_parse_cache(self, project_id):
        """清空项目文本后，解析结果缓存必须一并失效（不得在 TTL 内复活旧结果）。"""
        from app.db import get_db

        p = Path(files_mod.PROJECTS_DATA_DIR / str(project_id) / "uploads") / "旧文件.docx"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"old content")

        with get_db() as conn:
            conn.execute(
                "INSERT INTO uploaded_files "
                "(project_id, filename, path, file_type, file_role, status, extracted_text_path) "
                "VALUES (?, ?, ?, ?, 'main', 'success', ?)",
                (project_id, p.name, str(p), ".docx", str(p.parent / "旧文件.docx.txt")),
            )

        key = str(p)
        files_mod._parse_result_cache[key] = (
            files_mod._get_file_hash(key), time.time(), "旧的脏文本"
        )
        assert key in files_mod._parse_result_cache, "前置条件未成立"

        files_mod.clear_project_texts(project_id)

        assert key not in files_mod._parse_result_cache, (
            "clear_project_texts 未失效解析缓存：清空后 TTL 内会复活旧解析结果"
        )


# ============================================================================
# I-4：uploaded_files 写操作按 project_id 收敛
# ============================================================================
class TestUploadedFilesScopedByProject:
    def test_parse_task_updates_scoped_by_project(self):
        text = (BACKEND_DIR / "app" / "tasks" / "__init__.py").read_text(encoding="utf-8")
        # 去掉整行注释，防止修复注释里引用旧写法造成假阳性
        src = "\n".join(
            line for line in text.splitlines() if not line.lstrip().startswith("#")
        )
        assert "SET status = 'processing' WHERE id = ? AND project_id = ?" in src, (
            "processing 更新未按 project_id 收敛"
        )
        assert "SET status = ?, extracted_text_path = ?, message = ?" in src
        assert "SET status = 'failed', message = ?" in src
        assert src.count("AND project_id = ?") >= 3, (
            "三处 uploaded_files 更新必须都带 project_id 作用域"
        )

    def test_re_extract_update_scoped_by_project(self):
        from app.services.files import re_extract_text
        src = inspect.getsource(re_extract_text)
        assert "WHERE id = ? AND project_id = ?" in src, "re_extract_text 的 UPDATE 未按项目收敛"


# ============================================================================
# O-1：中文数字转换与直出规模护栏
# ============================================================================
class TestIntToChineseNum:
    @pytest.mark.parametrize(
        "n,expected",
        [
            (1, "一"), (2, "二"), (9, "九"),
            (10, "十"), (11, "十一"), (20, "二十"), (21, "二十一"), (99, "九十九"),
            (100, "一百"), (101, "一百零一"), (123, "一百二十三"), (999, "九百九十九"),
        ],
    )
    def test_valid_range(self, n, expected):
        from app.services.ai.workflows_outline_v51 import _int_to_chinese_num
        assert _int_to_chinese_num(n) == expected

    @pytest.mark.parametrize("n", [0, -1, -100, "abc", None])
    def test_non_positive_and_invalid_are_safe(self, n):
        """n<=0 或非数字不得抛异常，也不得产出「第零章」这类脏标题。"""
        from app.services.ai.workflows_outline_v51 import _int_to_chinese_num
        assert _int_to_chinese_num(n) == ""

    def test_large_n_does_not_raise(self):
        """修复前：n>=100 时 `tens = n // 10 >= 10` 越界访问 _CN_NUMS → IndexError。"""
        from app.services.ai.workflows_outline_v51 import _int_to_chinese_num
        assert _int_to_chinese_num(100) == "一百"
        assert _int_to_chinese_num(999) == "九百九十九"


class TestUnwrapOutlinePayload:
    def test_plain_list(self):
        from app.services.ai.workflows_outline_v51 import _unwrap_outline_payload
        payload = [{"title": "A"}]
        assert _unwrap_outline_payload(payload) is payload

    def test_wrapped_by_known_keys(self):
        from app.services.ai.workflows_outline_v51 import _unwrap_outline_payload
        inner = [{"title": "A"}]
        for key in ("outline", "children", "data", "items"):
            assert _unwrap_outline_payload({key: inner}) is inner, key

    def test_unwrapping_is_side_effect_free(self):
        from app.services.ai.workflows_outline_v51 import _unwrap_outline_payload
        data = {"outline": [{"title": "A"}]}
        _unwrap_outline_payload(data)
        assert data == {"outline": [{"title": "A"}]}, "解包不得原地修改入参"

    @pytest.mark.parametrize("bad", [None, {}, "str", 42, {"outline": "not-a-list"}])
    def test_non_array_returns_none(self, bad):
        from app.services.ai.workflows_outline_v51 import _unwrap_outline_payload
        assert _unwrap_outline_payload(bad) is None


class TestDirectOutlineScaleGuard:
    @staticmethod
    def _payload(n_l1: int):
        return [{"title": f"章节{i}"} for i in range(1, n_l1 + 1)]

    def test_huge_l1_count_returns_empty_without_raising(self, monkeypatch):
        """200 个一级章节：护栏须先于编号阶段触发，不抛 IndexError。"""
        from app.services.ai import workflows_outline_v51 as wo

        async def _fake(provider, messages, **kw):
            return self._payload(200)

        monkeypatch.setattr(wo, "collect_json_response", _fake)
        out = asyncio.run(wo.generate_outline_direct(object(), "概述", "[]", "工程"))
        assert out == [], "超规模直出应回退分步（返回空列表），而不是崩溃或产出残缺目录"

    def test_guard_triggers_before_numbering(self, monkeypatch):
        """护栏前移的直接证据：编号函数不应被调用到超出上限的索引。"""
        from app.services.ai import workflows_outline_v51 as wo

        hits: list = []
        _orig = wo._int_to_chinese_num

        def _spy(n):
            hits.append(n)
            return _orig(n)

        monkeypatch.setattr(wo, "_int_to_chinese_num", _spy)
        monkeypatch.setattr(wo, "OUTLINE_DIRECT_MAX_LEVEL1", 12)

        async def _fake(provider, messages, **kw):
            return self._payload(60)

        monkeypatch.setattr(wo, "collect_json_response", _fake)
        out = asyncio.run(wo.generate_outline_direct(object(), "概述", "[]", "工程"))
        assert out == []
        assert not any(h and h > 12 for h in hits), f"护栏后仍触发大编号: {hits}"

    def test_under_limit_numbers_start_from_chapter_one(self, monkeypatch):
        from app.services.ai import workflows_outline_v51 as wo

        async def _fake(provider, messages, **kw):
            return self._payload(3)

        monkeypatch.setattr(wo, "collect_json_response", _fake)
        out = asyncio.run(wo.generate_outline_direct(object(), "概述", "[]", "工程"))
        assert len(out) == 3
        assert out[0]["title"].startswith("第一章"), out[0]["title"]
        assert out[2]["title"].startswith("第三章"), out[2]["title"]

    def test_normalize_handles_hundreds_without_indexerror(self):
        """_normalize_direct_outline 自身也不能因大编号崩溃（防御纵深）。"""
        from app.services.ai.workflows_outline_v51 import _normalize_direct_outline
        payload = [{"title": f"T{i}"} for i in range(1, 130)]
        outline = _normalize_direct_outline(payload)
        assert len(outline) == 129
        assert outline[0]["title"].startswith("第一章")
        assert "章" in outline[110]["title"]


# ============================================================================
# O-2：落库目录树携带稳定路径编号（跨模块数据传递）
# ============================================================================
class TestSavedOutlineCarriesNumber:
    """SSE outline_complete 交付给前端的节点必须自带稳定编号。

    修复前 _renumber_items 是死代码（零生产调用方），落库树从不带 number，
    导致前端 outlineDictToTreeNodes / annotateOutlineWithNumbers、DOCX 导出、
    正文模块、v51-confirm 回显各自重算一遍编号 —— 编号命名空间不统一，
    人工编辑后前后端编号还可能对不上。
    """

    @staticmethod
    def _save(project_id: int, outline: list) -> list:
        from app.routers.sse.sse_db_ops import _clear_and_save_outline
        return _clear_and_save_outline(project_id, outline)

    def test_saved_outline_carries_number(self, project_id):
        outline = [
            {
                "title": "总体实施",
                "children": [
                    {"title": "部署", "children": [{"title": "原则"}, {"title": "布置"}]},
                    {"title": "质量", "children": [{"title": "目标"}]},
                ],
            },
            {"title": "资源配置", "children": [{"title": "计划"}]},
        ]
        saved = self._save(project_id, outline)

        assert len(saved) == 2
        assert [n["number"] for n in saved] == ["1", "2"], [n["number"] for n in saved]
        l2 = saved[0]["children"]
        assert [c["number"] for c in l2] == ["1.1", "1.2"], [c["number"] for c in l2]
        assert [c["number"] for c in l2[0]["children"]] == ["1.1.1", "1.1.2"]
        assert [c["number"] for c in l2[1]["children"]] == ["1.2.1"]

    def test_number_field_present_on_every_node(self, project_id):
        """任何 API 消费方（v51-confirm 回显 / 前端刷新重建）都能直接读到编号。"""
        saved = self._save(project_id, [{"title": "唯一章节"}])

        def _walk(nodes):
            for n in nodes:
                assert "number" in n, f"节点缺少 number 字段: {n}"
                _walk(n.get("children") or [])

        _walk(saved)

    def test_title_stays_plain_text(self, project_id):
        """编号只写在 number 字段，title 保持纯文本（不得写入「1.1 」前缀）。"""
        saved = self._save(project_id, [
            {"title": "总体实施", "children": [{"title": "部署"}]},
        ])
        assert saved[0]["title"] == "总体实施"
        assert saved[0]["children"][0]["title"] == "部署"

    def test_number_matches_db_section_path(self, project_id):
        """落库编号与 services.sections.get_section_path 的 DB 动态路径号同口径。"""
        from app.services.sections import get_section_path

        saved = self._save(project_id, [
            {"title": "总体实施", "children": [
                {"title": "部署", "children": [{"title": "原则"}, {"title": "布置"}]},
            ]},
        ])

        def _check(nodes):
            for n in nodes:
                assert n["number"] == get_section_path(n["id"]), (
                    f"内存树编号 {n['number']!r} 与 DB 路径号 {get_section_path(n['id'])!r} "
                    f"不一致（section_id={n['id']}, title={n['title']!r}）"
                )
                _check(n.get("children") or [])

        _check(saved)

    def test_rebuild_renumbers_from_scratch(self, project_id):
        """重跑目录重建：编号按新结构重新计算，不得沿用旧行残留。"""
        first = self._save(project_id, [
            {"title": "甲", "children": [{"title": "甲1"}, {"title": "甲2"}]},
            {"title": "乙", "children": [{"title": "乙1"}]},
        ])
        assert [n["number"] for n in first] == ["1", "2"]
        assert [c["number"] for c in first[0]["children"]] == ["1.1", "1.2"]

        second = self._save(project_id, [{"title": "丙"}])
        assert [n["number"] for n in second] == ["1"]
        assert second[0]["children"] == []