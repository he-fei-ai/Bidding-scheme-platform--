import os
import shutil
import sys
import tempfile
import atexit
from pathlib import Path


# 将 backend/ 目录加入 Python 路径，使 from app.xxx 导入可用
BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


# 测试强制使用独立临时数据目录：
# 1) 避免污染/依赖真实数据目录（真实目录可能因安全软件对 python 进程只读）
# 2) 保证测试可重复、可并行
_TEST_DATA_DIR = tempfile.mkdtemp(prefix="bid_assistant_test_data_")
os.environ["BID_ASSISTANT_DATA_DIR"] = _TEST_DATA_DIR


def _cleanup_test_data_dir() -> None:
    shutil.rmtree(_TEST_DATA_DIR, ignore_errors=True)


atexit.register(_cleanup_test_data_dir)

# 在任意测试模块导入前，于临时数据目录中完成建库建表，
# 使 TestClient 集成测试与直接调用服务的单测都拿到完整 schema
from app.db import init_db  # noqa: E402

init_db()
