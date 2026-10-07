"""工具模块，提供通用工具函数、性能监控和 JSON 解析工具。"""

import functools
import re

from .json_parser import extract_json_from_text, safe_json_parse


# 预编译正则表达式，避免每次调用时重复编译（性能优化）
_COUNT_WORDS_RE = re.compile(r"[\u4e00-\u9fff]|[a-zA-Z0-9]+")


@functools.lru_cache(maxsize=256)
def count_words(text: str) -> int:
    """统计中英文混合文本的字数（中文字符逐个计数，英文/数字按词计数）

    性能优化：
    - 使用预编译正则表达式（全局常量）
    - LRU 缓存：重复内容直接返回缓存结果。
      性能修复：maxsize 从 2048 降至 256——旧值最长可持有 2048 份完整正文
      （数十 MB 内存），而正则扫描 5KB 文本耗时不足 1ms，缓存miss代价低，
      缩小上限可控制内存占用且几乎不影响命中率（同一内容的重复计数是突发性的）
    """
    if not text:
        return 0
    return len(_COUNT_WORDS_RE.findall(text))


__all__ = [
    "extract_json_from_text",
    "safe_json_parse",
    "count_words",
]
