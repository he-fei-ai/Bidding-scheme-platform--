# -*- coding: utf-8 -*-
"""降级可观测性回归用例（2026-11-04）。

覆盖本轮把「静默兜底」改为「可观测降级」的 7 处分支：
- 统一埋点 helper 在结构化 sink 故障时仍输出 WARNING；
- 上传解析：图片超出 OCR 大小上限跳过；
- HTML 导出：chart_marker JSON 解析失败；
- 章节生成：content_field_contract 导入失败（事实清单 / 九章要素）；
- 一致性：审计日志清理失败；
- 文本模型配置：配置版本号 / Key 轮转时间读取失败；
- 图表：结构化数据解析失败。
"""
from __future__ import annotations

import logging
import sys
from contextlib import contextmanager

import pytest


def test_log_degradation_warning_survives_sink_failure(monkeypatch, caplog):
    """结构化 sink 抛错时，统一埋点仍必须输出标准 WARNING（埋点不反噬主流程）。"""
    import app.observability as obs
    from app.services.degradation import log_degradation

    def _boom(**_kwargs):
        raise RuntimeError("observability sink down")

    monkeypatch.setattr(obs, "log_degrade", _boom)
    with caplog.at_level(logging.WARNING, logger="app.services.degradation"):
        log_degradation(
            "parse",
            trigger="测试触发",
            action="测试动作",
            reason="测试原因",
            file_name="demo.pdf",
        )

    text = caplog.text
    assert "[降级] 测试触发 -> 测试动作" in text
    assert "测试原因" in text
    assert "file_name=demo.pdf" in text


def test_ocr_size_limit_skip_is_observable(monkeypatch, caplog, tmp_path):
    """图片超 OCR 大小上限时跳过，但必须留 [降级] 日志（原实现静默 return ""）。"""
    import app.services.extractors as extractors

    img = tmp_path / "huge.png"
    img.write_bytes(b"\x00")

    def _fake_late(name: str):
        if name == "OCR_ENGINE":
            return "tesseract"
        if name == "_ocr_image_size_exceeded":
            return lambda _p: True
        raise AssertionError(f"unexpected late name: {name}")

    monkeypatch.setattr(extractors, "_late", _fake_late)
    with caplog.at_level(logging.WARNING):
        assert extractors._extract_image_ocr(str(img)) == ""

    assert "[降级] 图片超出 OCR 大小上限" in caplog.text
    assert "huge.png" in caplog.text


def test_html_chart_marker_json_failure_is_observable(monkeypatch, caplog):
    """chart_marker 非法 JSON 时仍返回占位符，但必须记录降级原因。"""
    import app.services.html_export as html_export

    monkeypatch.setattr(html_export, "_resolve_chart_source", lambda *a, **k: None)
    with caplog.at_level(logging.WARNING):
        out = html_export._render_chart_marker_html(
            '{"chart_type": "bar", oops',
            {},
            None,
            project_id=1,
            chapter_number="1",
        )

    assert "图表占位符" in out
    assert "[降级] chart_marker JSON 解析失败" in caplog.text


def test_content_field_contract_import_failure_is_observable(monkeypatch, caplog):
    """事实清单与九章要素两个导入降级分支都必须留痕，且返回值语义不变。"""
    import app.services.ai.ordered_generation as ordered

    monkeypatch.setitem(sys.modules, "app.services.content_field_contract", None)
    with caplog.at_level(logging.WARNING):
        facts = ordered.select_facts_by_chapter_map("施工组织设计", [])
        elements = ordered.build_nine_chapter_elements("施工组织设计")

    assert facts == []
    assert elements == ""
    assert "[降级] content_field_contract 导入失败" in caplog.text
    assert "跳过章节事实清单注入" in caplog.text
    assert "跳过九章要素提示注入" in caplog.text


def test_consistency_cleanup_failure_is_observable(monkeypatch, caplog):
    """审计日志清理失败返回 0（不阻断），但必须留痕。"""
    import app.services.consistency_auditor as auditor

    @contextmanager
    def _broken_db(*_a, **_k):
        raise RuntimeError("db locked")
        yield  # pragma: no cover

    monkeypatch.setattr(auditor, "get_db", _broken_db)
    with caplog.at_level(logging.WARNING):
        assert auditor.cleanup_old_consistency_audit(90) == 0

    assert "[降级] 一致性审计日志清理失败" in caplog.text
    assert "db locked" in caplog.text


def test_ai_config_version_read_failure_is_observable(monkeypatch, caplog):
    """配置版本号读取失败仍返回 0，但必须留痕（否则 ETag 悄悄退化）。"""
    import app.services.ai.provider_factory as factory

    monkeypatch.setattr(factory, "_read_config_version", lambda: (_ for _ in ()).throw(RuntimeError("no table")))
    with caplog.at_level(logging.WARNING):
        assert factory.get_ai_config_version() == 0

    assert "[降级] AI 配置版本号读取失败" in caplog.text


def test_key_rotated_at_read_failure_is_observable(monkeypatch, caplog):
    """Key 轮转时间读取失败仍返回 None，但必须留痕。"""
    import app.services.ai.provider_factory as factory

    @contextmanager
    def _broken_db(*_a, **_k):
        raise RuntimeError("db down")
        yield  # pragma: no cover

    monkeypatch.setattr(factory, "get_db", _broken_db)
    with caplog.at_level(logging.WARNING):
        assert factory.get_key_rotated_at() is None

    assert "[降级] API Key 轮转时间读取失败" in caplog.text


def test_chart_data_parse_failure_is_observable(caplog):
    """图表结构化数据解析失败不抛错，但必须留痕（子类型可能退化）。"""
    from app.services.chart_validators import detect_architecture_subtype

    with caplog.at_level(logging.WARNING):
        detect_architecture_subtype("{not-json", "组织架构")

    assert "[降级] 图表数据 JSON 解析/展开失败" in caplog.text


def test_docx_chart_render_failure_is_observable(monkeypatch, caplog):
    """默认 CHART_FAIL_PLACEHOLDER=false 时图表被静默跳过；必须落结构化降级事件。"""
    import app.services.ai.image_engine as engine
    import app.services.export as export

    monkeypatch.setattr(engine, "repair_mermaid", lambda code: code)
    monkeypatch.setattr(engine, "validate_mermaid", lambda code: (False, "syntax error"))
    with caplog.at_level(logging.WARNING):
        ok = export._render_commercial(
            "mermaid",
            {"code": "graph TD; A-->", "title": "测试图表"},
            "图1 测试图表",
            doc=None,
            config=None,
            preload_only=False,
        )

    assert ok is False
    assert "[降级] DOCX 图表渲染失败" in caplog.text
    assert "syntax error" in caplog.text
    assert "静默跳过该图表" in caplog.text
