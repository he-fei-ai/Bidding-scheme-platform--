#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""按时间窗 + pid 抽取日志（用于定位真实生产活动）。"""
import os, re, io, sys, collections

LOG_DIR = r"j:/编程/招投标方案平台/logs"
FILES = ["backend.log"] + [f"backend.log.{i}" for i in range(1, 6)]
LINE_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s+\[(?P<level>[A-Z]+)\]\s+"
    r"(?P<logger>\S+)\s+\((?P<loc>[^)]*)\)\s+\[(?P<ctx>[^\]]*)\]:\s?(?P<msg>.*)$")

t0 = sys.argv[1] if len(sys.argv) > 1 else "2026-10-22 20:00"
t1 = sys.argv[2] if len(sys.argv) > 2 else "2026-10-22 23:59"
lv = set((sys.argv[3] if len(sys.argv) > 3 else "INFO,WARNING,ERROR").split(","))
pid_filter = sys.argv[4] if len(sys.argv) > 4 else None

out = []
for name in FILES:
    p = os.path.join(LOG_DIR, name)
    if not os.path.exists(p):
        continue
    with io.open(p, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.rstrip("\r\n")
            m = LINE_RE.match(line)
            if not m:
                continue
            if not (t0 <= m.group("ts") <= t1):
                continue
            if m.group("level") not in lv:
                continue
            if pid_filter and ("pid=%s" % pid_filter) not in m.group("ctx"):
                continue
            out.append((m.group("ts"), line))
out.sort()
print("命中 %d 条" % len(out))
for ts, l in out:
    print(l[:400])
