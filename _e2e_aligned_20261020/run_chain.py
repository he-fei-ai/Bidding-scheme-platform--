# -*- coding: utf-8 -*-
"""E2E 验证 · 链路驱动：上传解析 → 要点提取 → 目录生成 → 全局事实 → 正文生成 → 导出。

针对已启动的 sandbox 后端（BID_ASSISTANT_DATA_DIR 隔离 + OUTLINE_ALIGNED_MODE 指定模式），
用真实招标文件《岳阳路办公园区南楼办公室装修EPC项目招标文件.pdf》跑通完整链路，
并把每一步的原始证据落盘到 <root>/<mode>/ 下，供 collect_evidence.py 汇总。

用法：python run_chain.py free|aligned http://127.0.0.1:8791
      （aligned 用 8792）
"""
import json
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent
PDF = Path(
    r"J:\编程\招投标方案平台\data\projects\1871130\uploads"
    r"\岳阳路办公园区南楼办公室装修EPC项目招标文件.pdf"
)

MODE = (sys.argv[1] if len(sys.argv) > 1 else "").strip().lower()
BASE = (sys.argv[2] if len(sys.argv) > 2 else "http://127.0.0.1:8791").rstrip("/")
if MODE not in ("free", "aligned"):
    print("用法: python run_chain.py free|aligned http://127.0.0.1:<port>")
    sys.exit(2)

WORK = ROOT / MODE
WORK.mkdir(parents=True, exist_ok=True)
B = BASE + "/api/v1"
STEPS: list[dict] = []


def log(step: str, detail: str = "") -> None:
    line = f"[{time.strftime('%H:%M:%S')}][{MODE}][{step}] {detail}"
    print(line, flush=True)
    STEPS.append({"step": step, "detail": detail, "ts": time.strftime("%F %T")})


def save(name: str, obj) -> None:
    (WORK / name).write_text(
        json.dumps(obj, ensure_ascii=False, indent=2) if not isinstance(obj, str) else obj,
        encoding="utf-8",
    )


def consume_sse(client: httpx.Client, url: str, params: dict, jsonl_name: str, budget_s: float) -> list[dict]:
    """流式消费 SSE 事件直到服务端关闭流；全量事件落盘 jsonl，返回事件列表。

    终止条件：流自然结束（生成完成后生成器退出）；或收到 status=error；或超时。
    """
    events: list[dict] = []
    t0 = time.monotonic()
    with client.stream("GET", url, params=params, timeout=httpx.Timeout(10.0, read=900.0)) as resp:
        resp.raise_for_status()
        buf = ""
        for chunk in resp.iter_text():
            buf += chunk
            while "\n\n" in buf:
                raw, buf = buf.split("\n\n", 1)
                for line in raw.splitlines():
                    if not line.startswith("data: "):
                        continue
                    try:
                        ev = json.loads(line[6:])
                    except json.JSONDecodeError:
                        continue
                    events.append(ev)
                    st = str(ev.get("status") or ev.get("type") or "")
                    ph = ev.get("phase") or ev.get("message") or ""
                    if len(events) % 40 == 0:
                        log("SSE", f"事件#{len(events)} status={st} {str(ph)[:60]}")
                    if st == "error":
                        raise RuntimeError(f"SSE 报错: {json.dumps(ev, ensure_ascii=False)[:400]}")
            if time.monotonic() - t0 > budget_s:
                raise TimeoutError(f"SSE 超过预算 {budget_s}s，事件数={len(events)}")
    (WORK / jsonl_name).write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in events), encoding="utf-8"
    )
    log("SSE", f"流结束，共 {len(events)} 个事件，耗时 {time.monotonic() - t0:.0f}s → {jsonl_name}")
    return events


def main() -> int:
    client = httpx.Client(base_url="", timeout=httpx.Timeout(30.0, read=900.0))
    # ---- 1. 创建项目 ----
    r = client.post(B + "/projects", json={"name": f"[E2E-{MODE}] 岳阳路办公园区南楼办公室装修EPC", "industry": "装饰装修"})
    r.raise_for_status()
    project = r.json()
    pid = project.get("id") or project.get("project", {}).get("id")
    log("项目创建", f"project_id={pid}")
    save("01_project.json", project)

    # ---- 2. 上传并解析真实招标文件 ----
    data = PDF.read_bytes()
    r = client.post(
        B + f"/projects/{pid}/files",
        files={"file": (PDF.name, data, "application/pdf")},
        data={"file_role": "main"},
        timeout=httpx.Timeout(30.0, read=1800.0),
    )
    r.raise_for_status()
    save("02_upload.json", r.json())
    log("上传解析", f"status={r.json().get('status')}")

    # 合并文本长度验证（解析链路是否真的产出了文本）
    r = client.get(B + f"/projects/{pid}/files/text")
    r.raise_for_status()
    txt = r.json()
    merged = txt.get("text") or txt.get("merged_text") or ""
    save("02_text_meta.json", {k: (f"<len {len(str(v))}>" if isinstance(v, str) and len(str(v)) > 200 else v) for k, v in txt.items()})
    log("文本提取", f"合并文本长度={len(merged)}")
    if len(merged) < 10000:
        raise RuntimeError(f"解析文本过短（{len(merged)}），链路终止")

    # ---- 3. 要点提取：项目概述 ----
    t0 = time.monotonic()
    r = client.post(B + f"/projects/{pid}/ai/summary")
    r.raise_for_status()
    summary = r.json().get("summary_json", "")
    save("03_summary.json", summary)
    log("要点提取-概述", f"len={len(summary)} 耗时={time.monotonic() - t0:.0f}s")

    # ---- 4. 要点提取：评分要点 ----
    t0 = time.monotonic()
    r = client.post(B + f"/projects/{pid}/ai/scoring")
    r.raise_for_status()
    scoring = r.json()
    save("04_scoring.json", scoring)
    sp_json = scoring.get("scoring_json") or scoring.get("scoring_points") or ""
    log("要点提取-评分项", f"keys={list(scoring)} len={len(str(sp_json))} 耗时={time.monotonic() - t0:.0f}s")

    # ---- 5. 目录生成（SSE 全流程）----
    events = consume_sse(
        client, B + f"/projects/{pid}/sections/generate-sse",
        {"outline_mode": "auto"}, "05_outline_sse.jsonl", budget_s=60 * 60,
    )
    tree = client.get(B + f"/projects/{pid}/sections").json()
    save("06_sections_tree.json", tree)
    flat = client.get(B + f"/projects/{pid}/sections/flat").json()
    save("06_sections_flat.json", flat)
    l1 = [s for s in flat if s.get("level") == 1]
    leaves = [s for s in flat if not any(x.get("parent_id") == s.get("id") for x in flat)]
    log("目录生成", f"L1={len(l1)} 总节点={len(flat)} 叶子={len(leaves)}")
    for s in l1:
        log("L1", f"{s.get('sort_order')}. {s.get('title')} scoring_point_id={s.get('scoring_point_id')}")

    # ---- 6. 全局事实 ----
    t0 = time.monotonic()
    r = client.post(B + f"/projects/{pid}/facts/generate", json={})
    r.raise_for_status()
    save("07_facts_generate.json", r.json())
    facts = client.get(B + f"/projects/{pid}/facts").json()
    save("07_facts.json", facts)
    facts_list = facts.get("facts") or facts.get("items") or facts
    log("全局事实", f"条数={len(facts_list) if isinstance(facts_list, list) else '见07_facts.json'} 耗时={time.monotonic() - t0:.0f}s")

    # ---- 7. 正文生成（子树模式：第一个一级章节，控制真实 AI 成本）----
    first_l1_id = l1[0]["id"]
    log("正文生成", f"子树模式 root_section_id={first_l1_id}（{l1[0]['title']}）")
    consume_sse(
        client, B + f"/projects/{pid}/sections/generate-content",
        {"root_section_id": first_l1_id, "writing_length": "standard", "concurrency": 4},
        "08_content_sse.jsonl", budget_s=60 * 60,
    )
    flat2 = client.get(B + f"/projects/{pid}/sections/flat").json()
    save("08_sections_after_content.json", flat2)
    gen = [s for s in flat2 if s.get("status") in ("completed", "done") or (s.get("content") and len(str(s["content"])) > 100)]
    log("正文生成", f"已生成节点={len(gen)}")

    # ---- 8. 导出 DOCX ----
    t0 = time.monotonic()
    r = client.post(B + f"/projects/{pid}/export/docx", json={}, timeout=httpx.Timeout(30.0, read=1800.0))
    r.raise_for_status()
    out = WORK / "09_export.docx"
    out.write_bytes(r.content)
    log("导出", f"{out.name} size={out.stat().st_size} 耗时={time.monotonic() - t0:.0f}s")

    save("run_meta.json", {"mode": MODE, "project_id": pid, "base": BASE, "pdf": str(PDF), "steps": STEPS})
    log("完成", "链路全部跑通")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # noqa: BLE001
        log("失败", repr(e))
        save("run_meta.json", {"mode": MODE, "base": BASE, "error": repr(e), "steps": STEPS})
        sys.exit(1)
