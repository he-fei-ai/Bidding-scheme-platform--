# -*- coding: utf-8 -*-
"""图表模块端到端验证（2026-11-04 深度加固轮·需求 13）

真实服务层全链路：建项目/章节 → 正文内嵌图表围栏（```mermaid / ```chart-json）
+ DB 字段绑定（<<GANTT:>> 标记）→ build_docx 导出 → 解析 DOCX 逐项断言：

  E1. 合法图表全部渲染为 PNG 并插入（inline_shapes 计数）
  E2. 图题严格「图 {章}-{序} {图名}」，章内序号连续（渲染失败/坏块不虚跳占号）
  E3. 未闭合坏围栏被删除（正文零代码残留）
  E4. 渲染失败默认跳过且不占号（CHART_FAIL_PLACEHOLDER=false 无占位说明）
  E5. 正文段落完整保留（图表管线不吞段）
  E6. 渲染耗时 profiling 已接线（P3-3，get_render_profile 有数据）
"""

from __future__ import annotations

import io
import json

import pytest

from app.models import ProjectCreate, SectionCreate, SectionUpdate
from app.services.export import build_docx, get_render_profile_summary
from app.services.projects import create_project
from app.services.sections import create_section, update_section


def _mk_project(title: str = "图表E2E项目") -> int:
    proj = create_project(ProjectCreate(name=title, description="端到端验证"))
    return proj.id


CH1_FLOWCHART = """flowchart TD
    A["开工准备"] --> B["基础施工"]
    B --> C["主体结构"]
    C --> D["竣工验收"]"""

CH1_LAYOUT = {
    "type": "layout",
    "title": "施工现场平面布置图",
    "zones": [
        {"id": "Z1", "name": "办公区", "x": 5, "y": 5, "w": 25, "h": 25},
        {"id": "Z2", "name": "加工区", "x": 40, "y": 5, "w": 25, "h": 25},
        {"id": "Z3", "name": "材料堆场", "x": 5, "y": 40, "w": 25, "h": 25},
    ],
    "connections": [],
}

CH2_GANTT = {
    "type": "gantt",
    "title": "施工总进度计划",
    "totalDays": 180,
    "tasks": [
        {"id": 1, "name": "施工准备", "start": "2026-01-01", "end": "2026-01-20"},
        {"id": 2, "name": "主体结构", "start": "2026-01-21", "end": "2026-06-30"},
        {"id": 3, "name": "装饰装修", "start": "2026-07-01", "end": "2026-11-30"},
    ],
}

CH2_LABOR_EMPTY = {
    "type": "labor",
    "title": "劳动力配置计划",
    "phases": ["准备阶段"],
    "categories": ["木工"],
    "data": [],  # 数据行缺失 → 渲染必然失败（E4 降级验证）
}

CHAPTER1_CONTENT = f"""本章阐述施工组织部署的总体安排。

下图为施工流程：
```mermaid
{CH1_FLOWCHART}
```

现场布置说明：施工场地划分为办公区、加工区与材料堆场三大功能区。

```chart-json
{json.dumps(CH1_LAYOUT, ensure_ascii=False)}
```

```mermaid
flowchart TD
    A["坏块示例：本围栏未闭合，应被整块删除
"""

CHAPTER2_CONTENT = (
    "进度与资源保障措施如下。\n\n"
    "<<GANTT:施工总进度计划>>\n\n"
    "劳动力配置说明：各阶段劳动力需求详见下表（数据行缺失场景，验证降级）。\n\n"
    "```chart-json\n" + json.dumps(CH2_LABOR_EMPTY, ensure_ascii=False) + "\n```"
)


def _docx_texts(doc) -> list[str]:
    return [p.text for p in doc.paragraphs if p.text and p.text.strip()]


def _fig_captions(texts: list[str]) -> list[str]:
    import re

    return [t for t in texts if re.match(r"^图\d+-\d+", t.strip())]


@pytest.fixture()
def e2e_project():
    pid = _mk_project()
    ch1 = create_section(pid, SectionCreate(title="施工组织部署", level=1))
    update_section(ch1.id, SectionUpdate(
        content=CHAPTER1_CONTENT, status="completed",
    ))
    ch2 = create_section(pid, SectionCreate(title="进度与资源计划", level=1))
    update_section(ch2.id, SectionUpdate(
        content=CHAPTER2_CONTENT, status="completed",
        gantt_json=json.dumps(CH2_GANTT, ensure_ascii=False),
    ))
    return pid


class TestChartEndToEnd:
    def test_full_export_charts_numbering_and_degradation(self, e2e_project):
        pid = e2e_project
        path, _export_id = build_docx(pid)

        from docx import Document

        doc = Document(path)

        # ---- E1：合法图表（flowchart + layout + gantt）全部渲染插入 ----
        assert len(doc.inline_shapes) == 3, (
            f"应插入 3 张图（流程图/布局图/甘特图），实际 {len(doc.inline_shapes)}"
        )

        texts = _docx_texts(doc)
        caps = _fig_captions(texts)

        # ---- E2：图题「图{章}-{序} {图名}」且章内连续，不虚跳 ----
        assert any(c.startswith("图1-1") for c in caps), f"缺图1-1: {caps}"
        assert any(c.startswith("图1-2") for c in caps), f"缺图1-2: {caps}"
        assert any(c.startswith("图2-1") for c in caps), f"缺图2-1: {caps}"
        # labor 渲染失败 + 坏围栏删除 → 不产生 图1-3 / 图2-2
        assert not any(c.startswith("图1-3") for c in caps), f"图1-3 不应存在（虚跳）: {caps}"
        assert not any(c.startswith("图2-2") for c in caps), f"图2-2 不应存在（失败占号）: {caps}"
        # 图名优先级：layout 载荷 title / gantt 载荷 title
        assert any("施工现场平面布置图" in c for c in caps), f"layout 载荷图名缺失: {caps}"
        assert any("施工总进度计划" in c for c in caps), f"gantt 载荷图名缺失: {caps}"

        # ---- E3：坏围栏零残留 ----
        joined = "\n".join(texts)
        assert "```" not in joined, "正文残留围栏标记"
        assert "坏块示例" not in joined, "未闭合坏围栏未被删除"
        assert "<<GANTT" not in joined and "<<CHART" not in joined, "标记未转换为图表"
        assert "chart-json" not in joined, "围栏语言标签残留"

        # ---- E4：渲染失败默认无占位说明（CHART_FAIL_PLACEHOLDER=false）----
        assert "图表渲染失败" not in joined

        # ---- E5：正文段落完整保留 ----
        assert "本章阐述施工组织部署的总体安排。" in joined
        assert "现场布置说明" in joined
        assert "进度与资源保障措施如下。" in joined
        assert "劳动力配置说明" in joined

        # ---- E6：profiling 接线（P3-3）----
        summary = get_render_profile_summary()
        assert summary["count"] >= 3, f"渲染耗时统计未接线: {summary}"

    def test_fail_placeholder_enabled_shows_note(self, e2e_project, monkeypatch):
        """CHART_FAIL_PLACEHOLDER=true 时渲染失败显示灰色说明（不占图号）。"""
        from app.services import export as export_mod

        monkeypatch.setattr(export_mod, "CHART_FAIL_PLACEHOLDER", True)
        pid = e2e_project
        path, _ = build_docx(pid)

        from docx import Document

        doc = Document(path)
        texts = _docx_texts(doc)
        joined = "\n".join(texts)
        # 失败说明出现（labor 数据为空），但不携带图号前缀
        assert "图表渲染失败" in joined
        caps = _fig_captions(texts)
        assert not any(c.startswith("图2-2") for c in caps), (
            f"失败说明不得占用图号: {caps}"
        )
