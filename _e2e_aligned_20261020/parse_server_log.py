# -*- coding: utf-8 -*-
"""解析 sandbox 服务端 stderr 里的 logging error 转储，提取带参数的日志记录。

背景：sandbox 实例与常驻后端共用 logs/backend.log 轮转句柄冲突，文件 handler 失败后
记录以 "--- Logging error --- / Message / Arguments" 形式转储到 stderr。
用法：python parse_server_log.py <server.log> [关键字]
"""
import re
import sys
from pathlib import Path

path = Path(sys.argv[1])
kw = sys.argv[2] if len(sys.argv) > 2 else "Aligned"
raw = path.read_bytes()
for enc in ("utf-8", "gbk", "utf-16"):
    try:
        text = raw.decode(enc)
        break
    except UnicodeDecodeError:
        continue
text = raw.decode("utf-8", errors="replace")
lines = text.splitlines()
out = []
hits = 0
for i, ln in enumerate(lines):
    if kw in ln:
        ctx = lines[i : i + 4]
        out.append(" >> ".join(c.strip()[:160] for c in ctx))
        hits += 1
        if hits > 30:
            break
out.append(f"total_hits≈{hits}")
Path(str(path) + "." + kw + ".parsed.txt").write_text("\n".join(out), encoding="utf-8")
print(f"解析完成 hits={hits} → {path}.{kw}.parsed.txt")
