#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""基线指标：AI 调用耗时分位、错误码分布、重试次数、429 分布、traceId 覆盖。"""
import io, os, re, collections

LOG_DIR = r"j:/编程/招投标方案平台/logs"
FILES = ["backend.log"] + [f"backend.log.{i}" for i in range(1, 6)]
LINE_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s+\[(?P<level>[A-Z]+)\]\s+"
    r"(?P<logger>\S+)\s+\((?P<loc>[^)]*)\)\s+\[(?P<ctx>[^\]]*)\]:\s?(?P<msg>.*)$")

dur = []          # with_timeout 完成耗时
timeout_n = 0
err_codes = collections.Counter()
retry_429 = collections.Counter()
cooldown = collections.Counter()
rid_ok = rid_tot = 0
rid_warn_ok = rid_warn_tot = 0
per_module_warn = collections.Counter()
ai_fail_reason = collections.Counter()
l2_phase = collections.Counter()

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
            lv, msg, ctx = m.group("level"), m.group("msg"), m.group("ctx")
            rid_tot += 1
            if not re.search(r"rid=-(\s|$)", ctx) and "rid=-" not in ctx:
                rid_ok += 1
            if lv in ("WARNING", "ERROR"):
                rid_warn_tot += 1
                if "rid=-" not in ctx:
                    rid_warn_ok += 1
                per_module_warn[m.group("logger")] += 1
            mm = re.search(r"\[with_timeout\]\[[^\]]+\] 完成，耗时 ([\d.]+)s", msg)
            if mm:
                dur.append(float(mm.group(1)))
            if "任务被中止" in msg and "with_timeout" in msg:
                timeout_n += 1
            for code in re.findall(r"error_code=([A-Z_]+)", msg):
                err_codes[code] += 1
            mm = re.search(r"HTTP 429 限流.*限流重试 (\d+)/(\d+)", msg)
            if mm:
                retry_429[int(mm.group(1))] += 1
            mm = re.search(r"\[限流闸门\] 冷却 ([\d.]+)s", msg)
            if mm:
                cooldown[float(mm.group(1))] += 1
            mm = re.search(r"\[AI监控\] 调用失败 \[[^\]]+\]: (.+)$", msg)
            if mm:
                ai_fail_reason[mm.group(1)[:60]] += 1
            if "V5.1 L2" in msg and ("失败" in msg or "完成" in msg):
                l2_phase["L2"] += 1


def pct(a, q):
    if not a:
        return 0.0
    s = sorted(a)
    i = min(len(s) - 1, int(round(q * (len(s) - 1))))
    return s[i]


dur.sort()
print("=== AI 调用耗时（with_timeout 完成样本 n=%d）===" % len(dur))
if dur:
    print("  avg=%.2fs  P50=%.2fs  P90=%.2fs  P95=%.2fs  P99=%.2fs  max=%.2fs"
          % (sum(dur) / len(dur), pct(dur, .5), pct(dur, .9), pct(dur, .95),
             pct(dur, .99), dur[-1]))
print("  超时中止次数: %d  超时占比(相对完成样本): %.2f%%"
      % (timeout_n, 100.0 * timeout_n / max(1, len(dur) + timeout_n)))
print()
print("=== 错误码分布 ===")
for k, v in err_codes.most_common(30):
    print("  %-32s %d" % (k, v))
print()
print("=== 429 重试次数分布 (第几次重试) ===")
for k, v in sorted(retry_429.items()):
    print("  第 %d 次: %d" % (k, v))
print("  429 总次数: %d" % sum(retry_429.values()))
print()
print("=== 限流闸门冷却时长分布 ===")
for k, v in sorted(cooldown.items()):
    print("  %.1fs: %d" % (k, v))
print()
print("=== AI 监控失败原因 TOP10 ===")
for k, v in ai_fail_reason.most_common(10):
    print("  %-60s %d" % (k, v))
print()
print("=== traceId(rid) 覆盖 ===")
print("  全量: %d/%d = %.2f%%" % (rid_ok, rid_tot, 100.0 * rid_ok / max(1, rid_tot)))
print("  WARNING/ERROR: %d/%d = %.2f%%"
      % (rid_warn_ok, rid_warn_tot, 100.0 * rid_warn_ok / max(1, rid_warn_tot)))
print()
print("=== WARNING/ERROR 按模块 TOP20 ===")
for k, v in per_module_warn.most_common(20):
    print("  %-45s %d" % (k, v))
