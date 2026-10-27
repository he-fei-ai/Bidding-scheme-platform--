#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""日志采集与基线统计：解析 backend.log* 全量日志。"""
import os, re, sys, glob, collections, io

LOG_DIR = r"j:/编程/招投标方案平台/logs"
FILES = ["backend.log"] + [f"backend.log.{i}" for i in range(1, 6)]

LINE_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s+"
    r"\[(?P<level>[A-Z]+)\]\s+"
    r"(?P<logger>\S+)\s+"
    r"\((?P<loc>[^)]*)\)\s+"
    r"\[(?P<ctx>[^\]]*)\]:\s?"
    r"(?P<msg>.*)$"
)
CTX_RE = re.compile(r"(rid|pid|sid|tid|project|task|chapter)=([^\s\]]*)")

total = 0
levels = collections.Counter()
loggers = collections.Counter()
locs = collections.Counter()
hours = collections.Counter()
rid_present = 0
unparsed = []
ts_min, ts_max = None, None
per_file = {}

for name in FILES:
    p = os.path.join(LOG_DIR, name)
    if not os.path.exists(p):
        continue
    n = 0
    with io.open(p, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.rstrip("\r\n")
            if not line.strip():
                continue
            n += 1
            total += 1
            m = LINE_RE.match(line)
            if not m:
                if len(unparsed) < 30:
                    unparsed.append(line[:200])
                continue
            lv = m.group("level")
            levels[lv] += 1
            loggers[m.group("logger")] += 1
            locs[m.group("loc")] += 1
            ts = m.group("ts")
            hours[ts[:13]] += 1
            if ts_min is None or ts < ts_min:
                ts_min = ts
            if ts_max is None or ts > ts_max:
                ts_max = ts
            ctx = dict(CTX_RE.findall(m.group("ctx")))
            if ctx.get("rid", "-") not in ("-", "", "None"):
                rid_present += 1
    per_file[name] = n

print("=" * 70)
print("日志采集基线")
print("=" * 70)
print("文件行数:", per_file)
print("总行数(非空):", total)
print("时间范围:", ts_min, "->", ts_max)
print()
print("级别分布:")
for k, v in levels.most_common():
    print("  %-8s %8d  %5.2f%%" % (k, v, 100.0 * v / max(total, 1)))
print()
print("rid 有值行数:", rid_present, " (%.2f%%)" % (100.0 * rid_present / max(total, 1)))
print()
print("TOP 40 logger:")
for k, v in loggers.most_common(40):
    print("  %-45s %8d" % (k, v))
print()
print("TOP 40 代码位置:")
for k, v in locs.most_common(40):
    print("  %-45s %8d" % (k, v))
print()
print("未解析样例:", len(unparsed))
for u in unparsed[:15]:
    print("   ", u)
