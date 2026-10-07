# -*- coding: utf-8 -*-
"""同步端口配置到 backend-ports.json（供启动 BAT 调用，Vite 插件读取）。

修复说明：原先三个 BAT 中的内嵌 python 代码块（多行 python -c / heredoc）
在 cmd 下均无法执行，现统一收敛为本脚本，单点维护。

用法：
    python scripts/sync_backend_ports.py [后端端口] [前端端口]

端口参数可省略（默认后端 8000 / 前端 5175）。
项目根目录自动从脚本自身位置（<根>/scripts/）推导，不要从 BAT 传入带
尾反斜杠的路径参数（cmd 会把 `\"` 当作转义引号，吞掉后续所有参数）。
"""
import json
import sys
from datetime import datetime
from pathlib import Path


def main() -> int:
    # 项目根目录 = scripts/ 的上一级（单点来源，避免 BAT 传参转义问题）
    default_root = Path(__file__).resolve().parents[1]
    args = sys.argv[1:]
    if len(args) >= 3:
        root, backend_port, frontend_port = Path(args[0]), int(args[1]), int(args[2])
    elif len(args) == 2:
        root, backend_port, frontend_port = default_root, int(args[0]), int(args[1])
    elif len(args) == 1:
        root, backend_port, frontend_port = default_root, int(args[0]), 5175
    else:
        root, backend_port, frontend_port = default_root, 8000, 5175

    cfg = {
        "version": 1,
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "backend": {
            "host": "127.0.0.1",
            "port": backend_port,
            "url": f"http://127.0.0.1:{backend_port}",
            "docs_url": f"http://127.0.0.1:{backend_port}/docs",
            "health_url": f"http://127.0.0.1:{backend_port}/api/v1/health",
        },
        "frontend": {
            "host": "0.0.0.0",
            "port": frontend_port,
            "url": f"http://127.0.0.1:{frontend_port}",
        },
    }
    target = root / "backend-ports.json"
    target.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[启动] backend-ports.json 已同步 → 后端 {backend_port}，前端 {frontend_port}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
