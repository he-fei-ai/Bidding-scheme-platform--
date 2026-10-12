"""BUG-FIX 2026-10-06（H1）老格式降级提示串污染主文件 → 污染整条 AI 链 的回归测试

缺陷复现链（修复前）：
  1. 上传 .doc/.ppt/.wps，系统缺 olefile / antiword / catdoc / COM 自动化
  2. _extract_doc(files.py:1018) / _extract_ppt(:1192) / _extract_wps(:1550)
     return 一句**非空**的「[提示] 当前系统无法自动提取此 .doc 文件文本…」
  3. save_upload 此前只按「有没有抛异常」判 success → 提示串被写为 status='success'
  4. guess_file_role 对「技术要求.doc」判 main；_pick_main_file 只优先挑
     status=='success' 的 main → 这句提示被选为**项目主文本**
  5. 全局事实提取 / 目录生成 / 正文生成三个模块的 AI 输入全是这句提示，
     且前端 message 为空、看不到任何失败迹象

本文件的用例按「可复现缺陷 → 修复后有断言」组织，并含**自证用例**
（合规样本断言不误报），防止判定规则被写成过宽的误报源。
"""
import asyncio
import sys
import uuid
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.services import files as files_mod  # noqa: E402


DOC_NOTICE = "[提示] 当前系统无法自动提取此 .doc 文件文本，建议转换为 .docx 后重新上传"
PPT_NOTICE = "[提示] 当前系统无法自动提取此 .ppt 文件文本，建议转换为 .pptx 后重新上传"
WPS_NOTICE = "[提示] 当前系统无法自动提取此 .wps 文件文本，建议使用WPS打开后另存为 .docx 格式再上传。"


def _mk_project(name: str) -> int:
    from app.db import get_db

    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO projects (name, bidder_name) VALUES (?, ?)", (name, "测试投标方")
        )
        return int(cur.lastrowid)


# ---------- 1. is_extraction_notice 自证用例（正例 + 零假阳性反例） ----------

class TestIsExtractionNotice:
    def test_catches_all_three_real_notices(self):
        """三条真实降级提示串必须全部被识别（否则修复漏网）"""
        for notice in (DOC_NOTICE, PPT_NOTICE, WPS_NOTICE):
            assert files_mod.is_extraction_notice(notice) is True, notice

    def test_tolerates_surrounding_whitespace(self):
        """提取结果常带首尾空白，strip 后仍应命中"""
        assert files_mod.is_extraction_notice(f"\n  {DOC_NOTICE}  \n") is True

    @pytest.mark.parametrize(
        "text",
        [
            None,
            "",
            "   ",
            "本项目招标工期为 450 日历天，招标人：某某市住建局。",
            # 真实标书正文完全可能以「[提示]」起头（如引用响应表说明行），
            # 长度护栏必须保证这类内容不被误判为降级提示。
            "[提示] 投标人须知：本表所列全部数据以中文填写，英文大写，涂改无效。",
        ],
    )
    def test_does_not_false_positive(self, text):
        """零假阳性：真实正文 / 空值一律不判为降级提示"""
        assert files_mod.is_extraction_notice(text) is False, repr(text)

    def test_prefix_only_helper_is_importable(self):
        """登记表与 helper 必须从模块公共面导出，供 tasks/__init__.py 异步路径复用"""
        assert files_mod.EXTRACTION_NOTICES == frozenset({DOC_NOTICE, PPT_NOTICE, WPS_NOTICE})

    def test_registry_has_no_drift_with_code(self):
        """防漂移守卫：代码里所有 "[提示]" 字面量都必须已登记进登记表。

        防止后续有人在 _extract_doc/_extract_ppt/_extract_wps 里新增第四句
        降级提示却忘记登记 —— 那样它会重新退化成「被标 success 的假正文」。

        注释里出现的提示串（文档说明本缺陷的注释）不是字面量，须排除，
        否则本守卫会对着自己的说明文字报警。
        """
        import re

        src = (BACKEND_DIR / "app" / "services" / "files.py").read_text(encoding="utf-8")
        code_lines = []
        for line in src.splitlines():
            if line.strip().startswith("#"):
                continue
            idx = line.find('"[提示]')
            if idx == -1:
                continue
            if "#" in line[:idx]:  # 行内注释
                continue
            code_lines.append(line)
        literals = set(re.findall(r'"(\[提示\][^"]*)"', "\n".join(code_lines)))
        unregistered = literals - files_mod.EXTRACTION_NOTICES
        assert not unregistered, f"以下降级提示串未登记进 EXTRACTION_NOTICES：{unregistered}"
        assert len(files_mod.EXTRACTION_NOTICES) == 3, "登记表条数变化时需同步本守卫与自证用例"

    def test_extract_branches_return_registered_constants(self, monkeypatch, tmp_path):
        """三条提取分支的降级返回值必须就是登记表里的常量（防止代码与登记表脱节）"""
        p = tmp_path / "x.doc"
        p.write_bytes(b"\xd0\xcf\x11\xe0fake")
        # 屏蔽所有可用引擎，强制走到最后的降级 return
        for mod in ("olefile",):
            monkeypatch.setitem(sys.modules, mod, None)
        out = files_mod._extract_doc(str(p))
        assert out in files_mod.EXTRACTION_NOTICES, out
        assert "无法自动提取" in out


# ---------- 2. save_upload：提示串不得再被标为 success ----------

class TestSaveUploadNoticeNotSuccess:
    def test_notice_marked_failed_with_guidance_kept(self, monkeypatch):
        """提示串 → status='failed'，但 message 保留转换建议（用户仍可自助修复）"""
        pid = _mk_project("H1-提示串判失败")
        monkeypatch.setattr(files_mod, "extract_text_from_path", lambda _p: DOC_NOTICE)
        try:
            res = files_mod.save_upload(pid, f"技术要求_{uuid.uuid4().hex[:6]}.doc", b"\xd0\xcf\x11\xe0fake")
        finally:
            monkeypatch.undo()
        assert res["status"] == "failed", "降级提示串不得再被标为解析成功"
        assert res["message"] == DOC_NOTICE, "message 必须保留原文提示"
        assert res["file_role"] == "main", "文件名命中「技术要求」，角色仍应判 main（不改变角色语义）"

    def test_real_text_still_success(self, monkeypatch):
        """向后兼容：真实正文仍为 success，message 为空"""
        pid = _mk_project("H1-真实正文仍成功")
        monkeypatch.setattr(files_mod, "extract_text_from_path", lambda _p: "本项目工期450日历天。")
        try:
            res = files_mod.save_upload(pid, f"招标文件_{uuid.uuid4().hex[:6]}.doc", b"\xd0\xcf\x11\xe0fake")
        finally:
            monkeypatch.undo()
        assert res["status"] == "success"
        assert res["message"] == ""

    def test_empty_text_still_success_with_empty_message(self, monkeypatch):
        """向后兼容：空文本既有语义不变（success + EMPTY_TEXT_MESSAGE）"""
        pid = _mk_project("H1-空文本语义不变")
        monkeypatch.setattr(files_mod, "extract_text_from_path", lambda _p: "   ")
        try:
            res = files_mod.save_upload(pid, f"扫描件_{uuid.uuid4().hex[:6]}.doc", b"\xd0\xcf\x11\xe0fake")
        finally:
            monkeypatch.undo()
        assert res["status"] == "success"
        assert res["message"] == files_mod.EMPTY_TEXT_MESSAGE

    def test_raising_extractor_still_failed(self, monkeypatch):
        """既有异常路径不变"""
        pid = _mk_project("H1-异常路径不变")

        def _boom(_p):
            raise RuntimeError("解析器崩溃")

        monkeypatch.setattr(files_mod, "extract_text_from_path", _boom)
        try:
            res = files_mod.save_upload(pid, f"坏文件_{uuid.uuid4().hex[:6]}.doc", b"\xd0\xcf\x11\xe0fake")
        finally:
            monkeypatch.undo()
        assert res["status"] == "failed"
        assert "解析器崩溃" in res["message"]


# ---------- 3. 端到端：提示串不得成为主文本 / 不得进入合并文本 ----------

class _FakeExtractor:
    """按文件名后缀返回不同提取结果，模拟混合上传场景"""

    def __init__(self, mapping: dict[str, str]):
        self.mapping = mapping

    def __call__(self, path: str) -> str:
        return self.mapping.get(Path(path).suffix.lower(), "")


class TestNoticeNeverReachesAiContext:
    def test_failed_notice_not_picked_over_real_main(self, monkeypatch):
        """核心回归：真实 docx 主文件在时，提示串文件不得被 _pick_main_file 选中"""
        pid = _mk_project("H1-主文件选择")
        good = f"招标文件_{uuid.uuid4().hex[:6]}.docx"
        bad = f"技术要求_{uuid.uuid4().hex[:6]}.doc"
        fake = _FakeExtractor({".docx": "本项目工期450日历天，招标人某某市住建局。", ".doc": DOC_NOTICE})
        monkeypatch.setattr(files_mod, "extract_text_from_path", fake)
        try:
            files_mod.save_upload(pid, bad, b"\xd0\xcf\x11\xe0fake")
            files_mod.save_upload(pid, good, b"PK\x03\x04fake")
        finally:
            monkeypatch.undo()

        rows = files_mod.get_project_files(pid)
        main = files_mod._pick_main_file(rows)
        assert main is not None
        assert Path(main["filename"]).name == good, (
            f"主文件必须是解析成功的 docx，实际选中 {main['filename']}"
        )

    def test_notice_text_excluded_from_merged_text(self, monkeypatch):
        """全部文件都只有提示串时，合并文本不得把提示串喂给下游 AI"""
        pid = _mk_project("H1-合并文本剔除提示")
        name = f"技术要求_{uuid.uuid4().hex[:6]}.doc"
        monkeypatch.setattr(files_mod, "extract_text_from_path", lambda _p: DOC_NOTICE)
        try:
            files_mod.save_upload(pid, name, b"\xd0\xcf\x11\xe0fake")
        finally:
            monkeypatch.undo()

        merged = asyncio.run(files_mod.latest_extracted_text(pid))
        assert "无法自动提取" not in merged, "提示串不得进入 AI 上下文"

    def test_real_text_still_merged(self, monkeypatch):
        """向后兼容：真实正文照常进入合并文本"""
        pid = _mk_project("H1-真实正文仍合并")
        monkeypatch.setattr(files_mod, "extract_text_from_path", lambda _p: "本项目工期450日历天。")
        try:
            files_mod.save_upload(pid, f"招标文件_{uuid.uuid4().hex[:6]}.doc", b"\xd0\xcf\x11\xe0fake")
        finally:
            monkeypatch.undo()
        merged = asyncio.run(files_mod.latest_extracted_text(pid))
        assert "450" in merged


# ---------- 4. Celery 异步路径口径一致（防双路径漂移） ----------

class TestCeleryPathAligned:
    """Celery 异步路径必须与同步路径**共用同一份**「降级提示 → failed」判定。

    BUG-FIX 2026-10-12：判定逻辑原先内联在 tasks/__init__.py 里，与 save_upload
    口径漂移过一次（Celery 路径无条件写 status='success'，把降级提示串当正文
    喂给下游 AI）。现已下沉到 services.files._persist_file_extraction 单点实现，
    任务模块只做异常兜底。因此本组断言的落点随实现**上移**为「任务模块必须复用
    helper，且判定逻辑只存在于 helper 一处」——把断言留在原位置只会钉死过时的
    实现细节，反而在真正的口径漂移回归时仍保持绿色（假绿）。
    """

    TASK_SRC = (BACKEND_DIR / "app" / "tasks" / "__init__.py").read_text(encoding="utf-8")
    FILES_SRC = (BACKEND_DIR / "app" / "services" / "files.py").read_text(encoding="utf-8")

    def test_task_module_delegates_to_shared_helper(self):
        """任务模块不得再内联判定，必须调用共用 helper。"""
        assert "_persist_file_extraction" in self.TASK_SRC, (
            "Celery 解析路径未接入共用的落盘/判定 helper"
        )
        assert "is_extraction_notice" not in self.TASK_SRC, (
            "Celery 路径又内联了一份降级提示判定，会与 save_upload 口径再次漂移"
        )
        assert "'success', extracted_text_path" not in self.TASK_SRC, (
            "Celery 路径仍在无条件写 status='success'，会与 save_upload 口径漂移"
        )

    def test_notice_branch_lives_only_in_helper(self):
        """判定逻辑的唯一落点必须在 helper 内（防止被删掉后静默回归）。"""
        assert "is_extraction_notice(extracted_text)" in self.FILES_SRC, (
            "helper 内的降级提示判定被移除，降级串会被当正文写库"
        )
        assert "_status, _msg" in self.FILES_SRC

    def test_task_module_status_follows_helper(self):
        """任务返回的 status 必须取自 helper 返回值，不得自行硬编码成功。"""
        assert "'status': status" in self.TASK_SRC, (
            "任务未把 helper 的判定结果原样返回，前端会看到与列表相反的结论"
        )


# ---------- 5. I-6（2026-10-11）Excel/PPT 动态错误串必须被识别为降级 ----------

class TestExcelPptxErrorNoticeRecognition:
    """I-6：Excel/PPTX 分支的 f-string 错误串此前不在 EXTRACTION_NOTICES 里，
    导致 `is_extraction_notice` 返回 False，损坏文件被写成 status='success'、
    错误文本被缓存并喂给下游 AI。现改为「前缀精确匹配 + 长度上界」判定。"""

    EXCEL_PREFIXES = (
        "[Excel 解析错误]",
        "[Excel 文件损坏]",
        "[Excel 文件不存在]",
        "[Excel 权限不足]",
        "[PPTX 解析错误]",
    )

    def test_prefix_constants_are_exported(self):
        assert files_mod.EXTRACT_ERROR_PREFIXES == frozenset(self.EXCEL_PREFIXES)
        assert files_mod.EXTRACT_ERROR_MAX_LEN == 120

    def test_each_prefix_is_registered_with_distinct_body(self):
        """前缀必须两两不互相前缀，避免 "[Excel 文件]" 这类过宽前缀吞掉兄弟分支"""
        for p in self.EXCEL_PREFIXES:
            for q in self.EXCEL_PREFIXES:
                if p != q:
                    assert not p.startswith(q), f"前缀 {p!r} 被 {q!r} 前缀吞并"

    @pytest.mark.parametrize("msg", [
        "[Excel 解析错误] bad magic number",
        "[Excel 文件损坏] Bad magic number for central directory",
        "[Excel 文件不存在] [Errno 2] No such file or directory: 'x.xlsx'",
        "[Excel 权限不足] [Errno 13] Permission denied",
        "[PPTX 解析错误] missing package member",
    ])
    def test_dynamic_excel_pptx_errors_are_recognized(self, msg):
        """真实错误串（前缀 + 一个 str(e)）必须被判定为降级"""
        assert files_mod.is_extraction_notice(msg) is True, msg

    def test_long_real_content_is_not_misidentified(self):
        """长度护栏：真实标书正文即便偶发以 "[Excel 解析错误] " 起头也不得误判"""
        long_text = "[Excel 解析错误] " + "这是一段真实正文" * 40
        assert len(long_text) > files_mod.EXTRACT_ERROR_MAX_LEN
        assert files_mod.is_extraction_notice(long_text) is False

    def test_prefix_must_be_followed_by_whitespace(self):
        """前缀后必须紧跟空白/冒号/字符串结尾，禁止吃下伪造前缀如 '[Excel 解析错误xxx真实正文]'"""
        assert files_mod.is_extraction_notice("[Excel 解析错误xxx真实正文]") is False
        assert files_mod.is_extraction_notice("[Excel 解析错误]") is True

    def test_registry_no_drift_with_error_prefix_literals(self):
        """防漂移守卫：files.py 里所有以 `[Excel`/`[PPTX` 起头的 f-string 错误前缀
        必须已登记进 EXTRACT_ERROR_PREFIXES。新增前缀而未登记会让该分支
        重新退化成「被标 success 的假正文」。"""
        import re

        src = (BACKEND_DIR / "app" / "services" / "files.py").read_text(encoding="utf-8")
        code_lines = [
            line for line in src.splitlines()
            if not line.strip().startswith("#") and 'return f"' in line
        ]
        # 抓形如 return f"[前缀] {e}" 的字面量前缀（不含 f-string 内插）
        literals = set(re.findall(
            r'return f"\[(?:Excel|PPTX)[^"]*?\]\s*\{e\}"', "\n".join(code_lines)
        ))
        prefixes = {lit[len('return f"'):-len('"{e}"')] for lit in literals}
        unregistered = prefixes - files_mod.EXTRACT_ERROR_PREFIXES
        assert not unregistered, f"以下错误前缀未登记：{unregistered}"

    def test_save_upload_marks_excel_error_as_failed(self, monkeypatch):
        """save_upload 对 Excel 错误串必须写 status='failed'，且 message 保留原文"""
        pid = _mk_project("I6-Excel错误判失败")
        err_msg = "[Excel 解析错误] Bad magic number for central directory"
        monkeypatch.setattr(files_mod, "extract_text_from_path", lambda _p: err_msg)
        try:
            res = files_mod.save_upload(pid, f"工程量清单_{uuid.uuid4().hex[:6]}.xlsx", b"PK\x03\x04fake")
        finally:
            monkeypatch.undo()
        assert res["status"] == "failed", "Excel 错误串不得再被标为解析成功"
        assert res["message"] == err_msg, "message 必须保留原文错误（用户可自助修复）"
