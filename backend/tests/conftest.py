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


# =====================================================================
# HARDENING 2026-10-11：inspect.getsource 全局脆弱性兜底
# ---------------------------------------------------------------------
# 背景：大量测试用 `assert '<text>' in inspect.getsource(func)` 做源码断言。
#   inspect.getsource 依赖 func.__code__.co_firstlineno + 源文件当前内容；
#   全量套件跑完后（大量 importlib 动态加载 / monkeypatch 副作用），
#   模块对象的 __code__ 元数据可能被短暂覆盖，导致 getsource 定位到
#   错误偏移、返回其他函数的源码（不抛异常），测试偶发失败。
# 方案：在 conftest 打**一处全局补丁**——优先按「源文件 + 函数名 + def 边界」
#   直读截取；仅当直读路径不可用时回退标准 inspect.getsource。
#   这样无需迁移既有 ~130 处测试断言，即可根除该脆弱性。
# =====================================================================
import inspect as _inspect  # noqa: E402
import re as _re  # noqa: E402

_ORIG_GETSOURCE = _inspect.getsource


def _robust_getsource(obj):
    """稳定的 getsource：按源文件 + 函数名 + 缩进边界截取函数体。"""
    try:
        mod = _inspect.getmodule(obj)
        if mod is not None:
            mod_file = getattr(mod, "__file__", None)
            if mod_file and Path(mod_file).exists():
                src_text = Path(mod_file).read_text(encoding="utf-8")
                target = getattr(obj, "__func__", obj)
                if _inspect.isfunction(target):
                    name = target.__name__
                    # 匹配模块级或嵌套 def（允许 async def）
                    pattern = _re.compile(
                        rf"^(?:async\s+)?def\s+{_re.escape(name)}\s*\(",
                        _re.MULTILINE,
                    )
                    m = pattern.search(src_text)
                    if m:
                        start = m.start()
                        lines = src_text[start:].splitlines()
                        head = lines[0]
                        indent = len(head) - len(head.lstrip())
                        result = [head]
                        for line in lines[1:]:
                            stripped = line.strip()
                            if stripped and (
                                stripped.startswith("def ")
                                or stripped.startswith("async def ")
                                or stripped.startswith("class ")
                            ):
                                this_indent = len(line) - len(line.lstrip())
                                if this_indent <= indent:
                                    break
                            result.append(line)
                        return "\n".join(result)
                # 非函数（模块/类等）或函数名匹配失败：回退原实现
                return _ORIG_GETSOURCE(obj)
    except Exception:  # noqa: BLE001 —— 兜底失败时绝不掩盖原实现
        pass
    return _ORIG_GETSOURCE(obj)


_inspect.getsource = _robust_getsource
