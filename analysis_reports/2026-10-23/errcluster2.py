#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""WARNING/ERROR 聚类 + 测试噪声识别 + production/real 分离。"""
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

# 测试噪声指纹
NOISE_PAT = [
    re.compile(r"MagicMock|Mock|mock|monkeypatch|patch", re.I),
    re.compile(r"模拟|mock 崩溃|fail-fast|故意|intentional|test_|NO_SUCH_KEY|pytest|unittest", re.I),
    re.compile(r"tests[/\\]"),
]

PROJ_TEST = re.compile(r"\b(9\d{5}|999)\b")


def norm(s):
    s = HEX.sub("<hex>", s)
    s = NUM.sub("<n>", s)
    return re.sub(r"\s+", " ", s).strip()


def is_noise(msg, ctx, sample):
    blob = msg + " | " + ctx + " | " + sample
    for p in NOISE_PAT:
        if p.search(blob):
            return True
    m = re.search(r"pid=([^\s\]]+)", ctx)
    if m and m.group(1) in ("999", "-"):
        pass
    return False


ap = argparse.ArgumentParser()
ap.add_argument("--levels", default="WARNING,ERROR")
ap.add_argument("--top", type=int, default=80)
ap.add_argument("--noise", action="store_true", help="只看噪声")
a = ap.parse_args()
WANT = set(a.levels.split(","))

clusters = collections.OrderedDict()
n_noise = 0
n_real = 0

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
            noise = is_noise(m.group("msg"), m.group("ctx"), line)
            if noise:
                n_noise += 1
            else:
                n_real += 1
            if a.noise != noise:
                continue
            key = (m.group("level"), m.group("logger"), m.group("loc"), norm(m.group("msg")))
            c = clusters.setdefault(key, {"n": 0, "first": m.group("ts"), "last": m.group("ts"),
                                          "sample": line, "pids": collections.Counter(),
                                          "hours": collections.Counter()})
            c["n"] += 1
            c["hours"][m.group("ts")[:13]] += 1
            pm = re.search(r"pid=([^\s\]]+)", m.group("ctx"))
            if pm:
                c["pids"][pm.group(1)] += 1
            if m.group("ts") < c["first"]:
                c["first"] = m.group("ts")
            if m.group("ts") > c["last"]:
                c["last"] = m.group("ts")

print("噪声=%d 真实=%d  聚类(当前视角)=%d" % (n_noise, n_real, len(clusters)))
print("=" * 100)
for (lv, lg, loc, tmpl), c in sorted(clusters.items(), key=lambda kv: -kv[1]["n"])[: a.top]:
    print("[%s] x%-5d %s (%s)" % (lv, c["n"], lg, loc))
    print("    %s" % tmpl[:240])
    print("    首末 %s ~ %s  pid=%s" % (c["first"], c["last"], dict(list(c["pids"].most_common(4)))))
    print("    样例: %s" % c["sample"][:280])
    print("-" * 96)
