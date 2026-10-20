# -*- coding: utf-8 -*-
"""E2E 验证 · sandbox 搭建：把主库快照复制到 <root>/<mode>/data/。

用 sqlite3 backup API 保证一致性快照（主库 WAL 模式下直接 copy 文件可能撕裂）。
AI Provider 配置随快照一并带入 sandbox，密钥解密依赖 backend/.secret_key（同仓同机）。

用法：python setup_sandbox.py free|aligned
"""
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC_DB = Path(r"j:\编程\招投标方案平台\data\bid_assistant.sqlite3")


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "").strip().lower()
    if mode not in ("free", "aligned"):
        print("用法: python setup_sandbox.py free|aligned")
        return 2
    data_dir = ROOT / mode / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    dst_db = data_dir / "bid_assistant.sqlite3"
    src = sqlite3.connect(str(SRC_DB))
    dst = sqlite3.connect(str(dst_db))
    with dst:
        src.backup(dst)
    src.close()
    dst.close()
    print(f"[setup] sandbox DB 就绪: {dst_db} ({dst_db.stat().st_size} bytes)")
    # 清理上次运行的证据文件（保留 server.log 由启动脚本自行覆盖）
    return 0


if __name__ == "__main__":
    sys.exit(main())
