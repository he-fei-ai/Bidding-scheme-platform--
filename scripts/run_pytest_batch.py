"""按文件跑 pytest，单文件超时则终止并标记 TIMEOUT（避免挂起测试拖垮整轮）。"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent / "backend"
PY = BACKEND / ".venv" / "Scripts" / "python.exe"
TIMEOUT = int(sys.argv[1]) if len(sys.argv) > 1 else 60

files = sorted(p.name for p in (BACKEND / "tests").glob("test*.py") if any(k in p.name for k in sys.argv[2:]))
if not files:
    print("no files matched", sys.argv[2:])
    sys.exit(1)

summary: list[str] = []
for f in files:
    t0 = time.time()
    proc = subprocess.Popen(
        [str(PY), "-m", "pytest", f"tests\\{f}", "-q", "--no-header", "-p", "no:randomly"],
        cwd=str(BACKEND),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    try:
        out, _ = proc.communicate(timeout=TIMEOUT)
        tail = [ln for ln in out.splitlines() if ln.strip()][-3:]
        status = "PASS " if proc.returncode == 0 else "FAIL "
        print(f"{status} {f}  ({time.time()-t0:.1f}s)  " + " | ".join(tail))
    except subprocess.TimeoutExpired:
        proc.kill()
        out, _ = proc.communicate()
        print(f"TIMEOUT {f}  (>{TIMEOUT}s)  " + " | ".join([ln for ln in out.splitlines() if ln.strip()][-3:]))
    summary.append(f)

print("\n汇总:", ", ".join(summary))
