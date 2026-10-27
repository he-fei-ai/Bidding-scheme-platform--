#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""WARNING/ERROR 模板聚类（数字归一化），按 logger/级别 分组。"""
import os, re, io, collections, argparse

LOG_DIR = r"j:/编程/招投标方案平台/logs"
FILES = ["backend.log"] + [f"backend.log.{i}" for i in range(1, 6)]

LINE_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s+"
    r"\[(?P<level>[A-Z]+)\]\s+(?P<logger>\S+)\s+\((?P<loc>[^)]*)\)\s+"
    r"\[(?P<ctx>[^\]]*)\]:\s?(?P<msg>.*)$"
)

NUM = re.compile(r"\d+")
HEX = re.compile(r"\b[0-9a-f]{8,}\b")


def norm(s):
    s = HEX.sub("<hex>", s)
    s = NUM.sub("<n>", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


ap = argparse.ArgumentParser()
ap.add_argument("--levels", default="WARNING,ERROR")
ap.add_argument("--top", type=int, default=60)
ap.add_argument("--sample", type=int, default=1)
a = ap.parse_args()
WANT = set(a.levels.split(","))

clusters = collections.OrderedDict()
hourly = collections.Counter()
noc = 0

for name in FILES:
    p = os.path.join(LOG_DIR, name)
    if not os.path.exists(p):
        continue
    with io.open(p, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.rstrip("\r\n")
            m = LINE_RE.match(line)
            if not m or m.group("level") not in WANT:
                continue
            noc += 1
            key = (m.group("level"), m.group("logger"), m.group("loc"), norm(m.group("msg")))
            if key not in clusters:
                clusters[key] = {"n": 0, "first": m.group("ts"), "last": m.group("ts"),
                                 "sample": line, "hours": collections.Counter(),
                                 "ctx": collections.Counter()}
            c = clusters[key]
            c["n"] += 1
            c["hours"][m.group("ts")[:13]] += 1
            if m.group("ts") < c["first"]:
                c["first"] = m.group("ts")
            if m.group("ts") > c["last"]:
                c["last"] = m.group("ts")
            rid = re.search(r"rid=([^\s\]]+)", m.group("ctx"))
            if rid and rid.group(1) != "-":
                c["ctx"]["rid"] += 1

print("匹配行:", noc, " 聚类数:", len(clusters))
print("=" * 100)
for (lv, lg, loc, tmpl), c in sorted(clusters.items(), key=lambda kv: -kv[1]["n"])[: a.top]:
    print("[%s] x%-6d %s (%s)" % (lv, c["n"], lg, loc))
    print("    模板: %s" % tmpl[:260])
    print("    首末: %s ~ %s   rid覆盖=%d" % (c["first"], c["last"], c["ctx"].get("rid", 0)))
    print("    样例: %s" % c["sample"][:300])
    print("-" * 100)
