# -*- coding: utf-8 -*-
"""上传解析模块 · 第十二轮回归（2026-11-04）：编码异常 B-12-1 与 AI 端点文案 B-12-2。

对应缺陷（均有代码行号/实测证据）：

  B-12-1 ``services/files._read_text_auto``：无 BOM 的 UTF-16LE/BE 文件在
       「BOM 嗅探落空 → utf-8 严格解码抛 UnicodeDecodeError → 奇偶零字节启发式
       不触发（纯中文为主时 NUL 占比 <= 30%）→ gb18030 严格解码同样失败」时，
       旧逻辑直接落到 ``errors="replace"`` 兜底，产出整页 U+FFFD 乱码入库并被
       判 success —— 下游摘要/评分点/目录全部基于乱码上下文生成且用户无感。
       修复前实测：475 字符中文文本 -> 475 字符替换符乱码。
       修复：gb18030 失败后、兜底前插入 utf-16-le / utf-16-be 严格解码最后尝试。

  B-12-2 ``routers/ai.py`` 6 处「请先上传文件」400 文案：项目已上传文件但全部
       解析失败 / 全为空文本时 ``latest_extracted_text`` 返回空串，端点报
       「请先上传文件」—— 与真实状态（已上传、解析未产出文本）矛盾，用户无从
       排查。新文案包含旧文案作为子串（既有断言
       ``"请先上传文件" in detail`` 兼容），并显式给出排查方向。

测试策略：``_read_text_auto`` 为纯函数（tmp 文件），确定性复现；B-12-2 用
静态源码守卫钉住「6 处端点都必须使用含排查方向的新文案」，防止文案回漂。
"""
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.services.files import _read_text_auto  # noqa: E402

# 纯中文为主的标书样例（ASCII 占比极低 -> UTF-16LE 编码下 NUL 占比远低于 30%，
# 触发不到旧启发式；且 gb18030 严格解码失败 —— 修复前必走 replace 兜底）。
_CJK_TEXT = (
    "第一章 项目概况\n"
    "本项目为市政道路工程施工招标，包含道路、桥梁、排水工程。\n"
    "投标人须知前附表。\n"
    "评标办法：综合评估法。\n"
) * 5


class TestB121Utf16NoBomDecode:
    """B-12-1：无 BOM UTF-16 文本在全部常见编码失败后可被严格尝试救回。"""

    def test_utf16le_no_bom_cjk_decoded(self, tmp_path):
        """修复前：utf-8 抛异常、NUL 启发式不触发、gb18030 失败 -> replace 乱码。"""
        p = tmp_path / "tender.txt"
        p.write_bytes(_CJK_TEXT.encode("utf-16-le"))  # 无 BOM
        got = _read_text_auto(str(p))
        assert got.strip() == _CJK_TEXT.strip()
        assert "\ufffd" not in got

    def test_utf16be_no_bom_cjk_decoded(self, tmp_path):
        p = tmp_path / "tender.txt"
        p.write_bytes(_CJK_TEXT.encode("utf-16-be"))  # 无 BOM
        got = _read_text_auto(str(p))
        assert got.strip() == _CJK_TEXT.strip()
        assert "\ufffd" not in got

    def test_utf16be_gb18030_collision_decoded(self, tmp_path):
        """B-12-1c：gb18030 能“成功”解码的 UTF-16BE 中文不得被抢走。

        修复前实测（2026-11-04，本机）：
        "本项目为岳阳路市政道路改造工程，主要包括道路、排水、照明及绿化施工内容。"
        以 utf-16-be 无 BOM 写入后，``_read_text_auto`` 返回
        "Ⱨ禘\uee76㩎덜㎖…"（gb18030 无异常解码），且后续 utf-16 严格尝试不可达。
        修复：gb18030 之前先做 UTF-16 字节对结构判定 + 解码质量校验。
        """
        text = "本项目为岳阳路市政道路改造工程，主要包括道路、排水、照明及绿化施工内容。"
        p = tmp_path / "tender_be.txt"
        p.write_bytes(text.encode("utf-16-be"))  # 无 BOM
        got = _read_text_auto(str(p))
        assert got.strip() == text, f"UTF-16BE 被误判为 gb18030：{got[:30]!r}"
        assert "\ufffd" not in got

    def test_gb18030_cjk_still_preferred(self, tmp_path):
        """回归：真实 GBK/GB18030 中文文件不得被 UTF-16 结构判定误抢。"""
        text = "本项目为市政道路工程施工招标，包含道路、桥梁、排水工程。"
        p = tmp_path / "tender_gbk.txt"
        p.write_bytes(text.encode("gb18030"))
        got = _read_text_auto(str(p))
        assert got.strip() == text, f"GB18030 被误判为 UTF-16：{got[:30]!r}"

    def test_bom_utf16le_still_works(self, tmp_path):
        """回归：带 BOM 的 UTF-16LE 走 BOM 嗅探分支，不留 \\ufeff。"""
        p = tmp_path / "bom.txt"
        p.write_bytes(b"\xff\xfe" + "投标人须知：资质要求".encode("utf-16-le"))
        got = _read_text_auto(str(p))
        assert "投标人须知" in got
        assert "\ufeff" not in got

    def test_utf8_and_gb18030_unaffected(self, tmp_path):
        """回归：UTF-8 / GB18030 在更早分支返回，不受新分支影响。"""
        p8 = tmp_path / "u8.txt"
        p8.write_bytes("招标文件正文内容".encode("utf-8"))
        assert _read_text_auto(str(p8)) == "招标文件正文内容"

        pg = tmp_path / "gb.txt"
        pg.write_bytes("工程量清单：土方开挖".encode("gb18030"))
        assert "土方开挖" in _read_text_auto(str(pg))

    def test_binary_fallback_still_no_crash(self, tmp_path):
        """回归：彻底损坏的二进制仍走 replace 兜底（不抛异常、不中断上传）。"""
        p = tmp_path / "bin.txt"
        p.write_bytes(bytes(range(256)) * 4)
        got = _read_text_auto(str(p))
        assert isinstance(got, str)

    def test_short_utf16le_ascii_heavy(self, tmp_path):
        """中英混合（NUL 占比高）仍应优先命中既有奇偶零字节启发式，结果正确。"""
        mixed = "Section 1 总体要求\n施工方案：道路工程 100m。\n" * 8
        p = tmp_path / "mixed.txt"
        p.write_bytes(mixed.encode("utf-16-le"))
        got = _read_text_auto(str(p))
        assert got.strip() == mixed.strip()
        assert "\ufffd" not in got


class TestB122AiEmptyTextMessage:
    """B-12-2：AI 端点空文本 400 文案须含排查方向（且保持旧子串兼容）。"""

    def test_all_empty_text_endpoints_use_new_message(self):
        """静态守卫：ai.py 中全部「请先上传文件」400 都应升级为新文案。

        新文案包含旧文案子串 —— 既有断言（test_import_round10_fg_fh_metrics_smoke_
        2026_10_26.py::407 `"请先上传文件" in detail`）零回归。
        """
        src = (BACKEND_DIR / "app" / "routers" / "ai.py").read_text(encoding="utf-8")
        # 旧裸文案不得再单独出现（必须带排查方向）
        bare = src.count("detail='请先上传文件'")
        assert bare == 0, (
            f"ai.py 仍有 {bare} 处裸「请先上传文件」400 文案（应升级为含排查方向的新文案）"
        )
        upgraded = src.count("未提取到可分析的文本内容，请先上传文件并确保至少一个文件解析成功")
        assert upgraded >= 6, (
            f"ai.py 新文案仅 {upgraded} 处（预期 >= 6 处空文本 400 端点全部覆盖）"
        )
