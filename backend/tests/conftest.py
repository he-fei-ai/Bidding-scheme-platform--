import os
import shutil
import sys
import tempfile
import atexit
from pathlib import Path


# BUG-FIX 2026-10-17（TD-1 根因修复）：在任何 app 模块被导入之前（包括 conftest
# 自身的 `from app.db import init_db`），设置 PYTEST_RUNNING=1。app/main.py 检测
# 到此标记后改用 load_dotenv(override=False)，从根源杜绝 .env 功能开关污染
# os.environ。必须在模块级（而非 fixture）设置，确保早于任何 test 文件的
# `from app.main import app` 触发的 load_dotenv。
os.environ["PYTEST_RUNNING"] = "1"

# 将 backend/ 目录加入 Python 路径，使 from app.xxx 导入可用
BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


# HARDEN 2026-10-13（间歇性收集失败根因）：禁写字节码，规避中文路径
# __pycache__/*.pyc 写入竞态。背景：全量收集期多模块导入时，__pycache__
# 的 .pyc 写入在中文路径下偶发 WinError 5（安全软件锁文件 / 瞬时竞态），
# conftest 早前注释即记载「间歇性 Killing 进程」，实证表现为 90 个测试文件
# 在收集期报 ImportError（单文件复跑均正常、--co 亦正常，纯间歇）。
# 根 conftest 于收集前加载，故在此设 sys.dont_write_bytecode=True 可阻止
# 本进程在收集期为测试模块落 .pyc；同步写环境变量以覆盖测试衍生的子进程。
# 代价仅为每次运行重新编译（无 .pyc 缓存），对正确性无影响。
sys.dont_write_bytecode = True
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"


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


# =====================================================================
# 测试隔离加固（2026-10-17）：清除 app.main 导入时 load_dotenv(override=True)
# 污染到 os.environ 的功能开关
# ---------------------------------------------------------------------
# 根因：app/main.py:34 在模块导入时执行 load_dotenv(override=True)，把 .env 里的
# CONSISTENCY_PATCH_REPAIR_ENABLED=1 / FACTS_*_ENABLED=1 等功能开关强制载入
# os.environ。任何测试一旦 from app.main import app（e2e 测试、TestClient 等）
# 就永久污染进程级 os.environ，使后续所有测试的 _env_flag() 从默认 False 变为 True。
#
# 后果：workflows_expand._audit_one_group 的冲突修复从「整节重写」切换为
# 「JSON 精确补丁」路径，使基于文本返回的假 provider 返回非 JSON 内容 →
# consistency_repair.generate_patches 解析失败 → 8 个测试在全量运行时失败
# （单独运行时因未触发 app.main 导入而通过，造成"间歇性失败"假象）。
#
# 修复策略：在 pytest 收集完成后（所有模块级代码已执行完毕）、测试执行前，
# 清除这些功能开关环境变量，让测试环境回归「默认关闭」的干净状态。
# 需要这些开关为 True 的测试（如 test_global_facts_integration）应自行用
# monkeypatch.setenv 显式设置，不依赖全局污染。
_FUNCTIONAL_SWITCH_PREFIXES = (
    "CONSISTENCY_PATCH_REPAIR_ENABLED",
    "CONSISTENCY_AUDIT_EVIDENCE_ENABLED",
    "FACTS_ORCHESTRATED_SELECT_ENABLED",
    "FACTS_SEGMENTED_EXTRACTION_ENABLED",
    "FACTS_OUTLINE_INPUT_ENABLED",
    "FACTS_CHAPTER_SCOPE_ENABLED",
    "FACTS_COMPLETENESS_INSTRUCTION_ENABLED",
    "FACTS_CONTENT_GATE_ENABLED",
    "FACTS_KNOWLEDGE_PATCH_ENABLED",
    "FACTS_TEMPLATE_PATCH_ENABLED",
)


def pytest_collection_modifyitems(session, config, items):
    """收集完成后、测试执行前：清除功能开关环境变量污染。"""
    for _key in _FUNCTIONAL_SWITCH_PREFIXES:
        os.environ.pop(_key, None)
