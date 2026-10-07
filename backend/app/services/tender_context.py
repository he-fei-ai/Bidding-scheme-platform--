"""技术标上下文锚定提取模块

从完整招标文件文本中精准提取与"技术标"相关的片段，
避免全量投喂导致成本高、聚焦差。
"""

import hashlib
import logging
import threading
import time


logger = logging.getLogger(__name__)

# 锚定关键词：定位技术标相关区域
# 滑动窗口常量
_ANCHOR_WINDOW = 100
_SCORING_EXTRA_OFFSET = 50
_MAX_CHARS_DEFAULT = 40000

TECHNICAL_ANCHORS = [
    "技术标",
    "技术部分",
    "技术方案",
    "施工组织",
    "施工方案",
    "技术评分",
    "评分标准",
    "评分项",
    "评分办法",
    "评标方法",
    "评审要素",
    "评审标准",
    "综合评分",
    "技术部分评分",
    "技术要求",
    "技术参数",
    "技术响应",
    "实施方案",
    "项目实施方案",
    "质量管理",
    "质量保证",
    "工期",
    "进度计划",
    "安全施工",
    "安全生产",
    "文明施工",
    "环境保护",
    "项目部",
    "项目管理",
    "人员配置",
    "组织机构",
    "售后服务",
    "服务承诺",
    "培训方案",
    "技术规范",
    "验收标准",
    "验收方案",
    "重点难点",
    "合理化建议",
    "优化方案",
    "设计方案",
    "深化设计",
    "施工图",
    "评审方法",
    "评分细则",
    "技术评审",
]

# 排除关键词：排除商业/报价相关内容
STOP_ANCHORS = [
    "报价",
    "投标总价",
    "投标报价",
    "商务标",
    "商务部分",
    "资格审查",
    "投标函",
    "法定代表人",
    "授权委托",
    "企业业绩",
    "履约能力",
    "财务",
    "财务状况",
    "信誉",
    "企业信誉",
    "资信",
    "资质证书",
    "供应商实力",
    "企业实力",
    "营业执照",
    "投标保证金",
    "履约保证金",
    "招标代理",
    "开标",
    "评标办法（商务）",
]


# ===== 评分相关常量（BUG-TC-3: 集中到文件顶部，避免"先用后定义"困扰）=====
_SCORING_KEYWORDS = [
    "评分标准",
    "技术评分",
    "综合评分",
    "评分项",
    "评分细则",
    "评审方法",
    "评标办法",
]

# 评分区域专用窗口大小
_SCORING_WINDOW = 200

# ===== 噪声压缩（ENHANCE 2026-09-18）=====
# 入选窗口里「既不含技术锚点、也不含评分关键词」的行，按本模块自己的相关性判据
# 就是与"技术标"无关的内容（页眉页脚、投标人须知、通用格式条款…）。
# 实测（锚点密集的真实形态，约 66 个锚点/370 行）：窗口并集覆盖 46% 全文，
# 其中噪声行占 53%~58%，把真正有价值的评分细则与技术条款挤出 max_chars 预算
# —— 与模块 docstring 宣称的「精准提取…避免全量投喂导致成本高、聚焦差」相悖。
# 压缩策略：连续无锚点区段达到 _NOISE_RUN_MIN 行才动手，首尾各保留
# _NOISE_EDGE_KEEP 行并插入显式省略标记 —— 信息不会静默消失，AI 仍能看到边界
# 与「此处省略 N 行」。实测噪声占比 53% → 10%，评分与技术条款 100% 保留。
_NOISE_RUN_MIN = 30
_NOISE_EDGE_KEEP = 5

# 模块级缓存：key -> (result, timestamp)
_cache: dict[str, tuple[str, float]] = {}
_CACHE_TTL = 3600  # 秒，1小时（与文档内容稳定性匹配，避免频繁重算）
_CACHE_MAX = 100  # 最多缓存 100 个 key
_CACHE_LOCK = threading.Lock()


def _cache_key(text: str, *params: object) -> str:
    """生成缓存键（全文哈希，避免短前缀碰撞）

    BUG 修复：原实现仅对 text[:8192] 哈希，两个前 8192 字符相同但正文不同
    的招标文件（如同模板的封面+目录）会命中同一缓存键，返回错误上下文，
    导致后续 AI 摘要/评分点/全局事实基于错误 tender_context 生成。
    改为对全文长度 + 全文 sha256 取摘要，碰撞概率可忽略，性能开销 <1ms（1MB）。
    """
    raw = f"{len(text)}:{hashlib.sha256(text.encode('utf-8', 'ignore')).hexdigest()}:{','.join(str(p) for p in params)}"
    return hashlib.sha256(raw.encode()).hexdigest()[:20]


def extract_tender_context(full_text: str, max_chars: int = _MAX_CHARS_DEFAULT) -> str:
    """从完整招标文件文本中提取技术标相关片段

    策略：
    1. 锚定关键词定位技术标相关区域
    2. 排除商业/报价相关内容
    3. 滑动窗口提取（锚定行前后 100 行）
    4. 优先保留评分相关区域
    5. 限制最大字符数

    Args:
        full_text: 完整招标文件文本
        max_chars: 最大字符数（默认 40000）

    Returns:
        技术标相关片段
    """
    if not full_text:
        return ""

    # BUG 修复：原代码只 _cache_set 不 _cache_get，缓存形同虚设，
    # 每次 AI 分析请求都重新执行锚定+窗口提取。此处补上缓存读取。
    # BUG-FIX-67: 缓存键只计算一次，避免 SHA256 重复计算
    cache_key = _cache_key(full_text, max_chars)
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    lines = full_text.splitlines()
    logger.info(
        "[上下文] 开始提取技术标上下文: text_len=%d, max_chars=%d", len(full_text), max_chars
    )

    # 1. 锚定关键词定位
    anchor_indices = _find_anchors(lines, TECHNICAL_ANCHORS)

    # 2. 排除关键词定位
    stop_indices = _find_anchors(lines, STOP_ANCHORS)

    # 识别高优先级锚点（评分相关）
    # ENHANCE 2026-09-18：这份评分关键词此前与模块级 `_SCORING_KEYWORDS`
    # 各自硬编码一份（本文件里共三份，内容互不一致），改一处漏一处。
    # 改为以模块级常量为唯一来源派生，只补上更宽泛的「评分」与「评审标准」
    # —— 这两词是超集关系，不会缩小命中面。
    scoring_keywords = ["评分", "评审标准", *_SCORING_KEYWORDS]
    scoring_anchor_indices = []
    for idx in anchor_indices:
        line = lines[idx]
        if any(kw in line for kw in scoring_keywords):
            scoring_anchor_indices.append(idx)

    if not anchor_indices:
        # 没有锚定命中：头尾双取（BUG-FIX 2026-10-06，原为 full_text[:max_chars]
        # 纯头截断，会把尾部评分细则/技术要求整段丢掉且静默无告警）
        # BUG-FIX-68: 无锚定命中分支也写入缓存，避免重复计算
        result = _head_tail_excerpt(full_text, max_chars)
        _cache_set(cache_key, result)
        return result

    # 3. 滑动窗口提取
    window = _ANCHOR_WINDOW
    selected = set()

    # 优先处理评分相关锚点，使用更大窗口
    for idx in scoring_anchor_indices:
        start = max(0, idx - window)
        end = min(len(lines), idx + window + _SCORING_EXTRA_OFFSET)

        # 检查窗口内是否包含排除关键词
        has_stop = any(start <= si <= end for si in stop_indices)
        if has_stop:
            stop_in_window = [si for si in stop_indices if start <= si <= end]
            for si in stop_in_window:
                if si < idx:
                    start = max(start, si + 1)
                elif si > idx:
                    end = min(end, si)

        for i in range(start, end):
            selected.add(i)

    # 然后处理其他锚点
    for idx in anchor_indices:
        if idx in scoring_anchor_indices:
            continue
        start = max(0, idx - window)
        end = min(len(lines), idx + window)

        # 检查窗口内是否包含排除关键词
        has_stop = any(start <= si <= end for si in stop_indices)
        if has_stop:
            stop_in_window = [si for si in stop_indices if start <= si <= end]
            for si in stop_in_window:
                if si < idx:
                    start = max(start, si + 1)
                elif si > idx:
                    end = min(end, si)

        for i in range(start, end):
            selected.add(i)

    # 4. 构建结果
    selected_lines = [lines[i] for i in sorted(selected) if lines[i].strip()]
    # ENHANCE 2026-09-18：压缩窗口捎带进来的无关长区段（页眉页脚/须知条款等），
    # 让 max_chars 预算真正花在评分与技术条款上。判据即本模块自己的关键词表。
    selected_lines = _compress_irrelevant_runs(
        selected_lines, TECHNICAL_ANCHORS + _SCORING_KEYWORDS
    )

    if not selected_lines:
        # BUG-FIX-68: 无选中行分支也写入缓存，避免重复计算
        result = full_text[:max_chars]
        _cache_set(cache_key, result)
        return result

    result = "\n".join(selected_lines)

    # BUG-TC-1: 原代码将评分关键词嵌入 HTML 注释 `<!-- ... -->`，
    # 下游投喂给 AI 时可能被误判为指令或噪声。改为日志记录，
    # 不再污染返回给 AI 的上下文文本。
    scoring_found = [kw for kw in _SCORING_KEYWORDS if kw in result]
    if scoring_found:
        logger.info(
            "[上下文] 检测到评分关键词: %s", "、".join(scoring_found)
        )

    # 5. 限制最大字符数
    if len(result) > max_chars:
        # 优先保留包含评分关键词的部分
        scoring_pos = -1
        # ENHANCE 2026-09-18：原为硬编码 4 词，漏了「评分细则 / 评审方法 /
        # 评标办法」—— 而「评标办法」正是中文招标文件评分章节最常见的标题。
        # 改为复用模块级全集；_SCORING_KEYWORDS 的前 4 项与原列表**完全一致**，
        # 故本次改动只增不减，不会改变任何既有命中结果。
        for kw in _SCORING_KEYWORDS:
            pos = result.find(kw)
            if pos > 0:
                scoring_pos = pos
                break

        if scoring_pos > max_chars // 2:
            # 评分部分在后半段，从评分部分往前截取
            start = max(0, scoring_pos - max_chars // 3)
            end = min(len(result), start + max_chars - 3)
            result = "..." + result[start:end] if end > start else result[:max_chars]
        else:
            # 尝试从最后出现的锚定关键词附近截取（优先保留技术相关内容）
            last_anchor_pos = -1
            for anchor in TECHNICAL_ANCHORS:
                pos = result.rfind(anchor)
                if pos > 0 and pos > last_anchor_pos:
                    last_anchor_pos = pos
            if last_anchor_pos > max_chars // 2:
                # 从锚点往前截取
                start = max(0, last_anchor_pos - max_chars // 3)
                end = min(len(result), start + max_chars - 3)
                result = "..." + result[start:end] if end > start else result[:max_chars]
            else:
                # 默认保留开头
                result = result[: max_chars - 3] + "..."

    _cache_set(cache_key, result)  # BUG-FIX-67: 复用已计算的 cache_key
    logger.info(
        "[上下文] 提取完成: 结果长度=%d 字符, 锚点数=%d, 排除锚点=%d",
        len(result),
        len(anchor_indices),
        len(stop_indices),
    )
    return result


def _find_anchors(lines: list[str], keywords: list[str]) -> list[int]:
    """在行列表中查找关键词的所有匹配位置"""
    indices = []
    for i, line in enumerate(lines):
        for kw in keywords:
            if kw in line:
                indices.append(i)
                break
    return sorted(set(indices))


def _is_relevant_line(line: str, keywords: list[str]) -> bool:
    """按调用方给出的关键词表判断该行是否与"技术标"相关。"""
    return any(kw in line for kw in keywords)


def _head_tail_excerpt(full_text: str, max_chars: int) -> str:
    """无锚点命中时的降级截取：头尾双取，而非纯头部截断。

    BUG-FIX 2026-10-06（数据丢失）：原实现在无锚点命中时直接
    `full_text[:max_chars]` —— 只保留**头部**。但招标文件的评分细则、
    技术要求、附件条款通常位于**尾部**，于是「非标结构、锚点词一个都没
    命中」的招标文件（正是走这条分支的典型形态）会恰好丢掉最该喂给
    AI 的评分/技术部分，且截断是**静默**的，下游无从察觉。

    改为头尾各取一半预算，中间插入显式省略标记（与 _compress_irrelevant_runs
    的「不静默丢信息」原则一致）。本模块的截断本来就不承诺全文（超预算
    必然要丢），丢「中间」比丢「尾部」信息价值高得多。

    Args:
        full_text: 完整文本
        max_chars: 字符预算
    """
    if len(full_text) <= max_chars:
        return full_text
    marker_budget = 40
    if max_chars <= marker_budget:
        # 预算小到放不下省略标记时，退回原行为（不构造无意义的残缺文本）
        return full_text[:max_chars]
    body = max_chars - marker_budget
    head = body // 2
    tail = body - head
    omitted = len(full_text) - head - tail
    return (
        full_text[:head]
        + f"……（此处省略中间 {omitted} 个字符）……"
        + full_text[-tail:]
    )


def _compress_irrelevant_runs(
    lines: list[str],
    keywords: list[str],
    run_min: int = _NOISE_RUN_MIN,
    edge_keep: int = _NOISE_EDGE_KEEP,
) -> list[str]:
    """压缩窗口中与"技术标"无关的连续长区段（ENHANCE 2026-09-18）。

    锚点窗口按「行」取上下文，锚点密集时并集会整段吞进页眉页脚、投标人须知等
    格式性条款。这里用**本模块自己的锚点/评分关键词表**作相关性判据
    （不是新引入的启发式）：连续 `run_min` 行以上既无技术锚点也无评分关键词的
    区段，只保留首尾各 `edge_keep` 行，并插入一行省略标记。

    保留首尾的原因：让 AI 仍能看到区段边界（例如表格表头、章节结尾），
    且省略是**显式**的 —— 不会静默丢信息。

    Args:
        lines: 已按原文顺序排列的入选行
        keywords: 相关性关键词表（技术锚点 + 评分关键词）
        run_min: 触发压缩的最短连续无关行数
        edge_keep: 压缩区段首尾各保留的行数

    Returns:
        压缩后的行列表（保持原顺序）
    """
    if not lines:
        return lines
    flags = [_is_relevant_line(ln, keywords) for ln in lines]

    out: list[str] = []
    i, n = 0, len(lines)
    while i < n:
        if flags[i]:
            out.append(lines[i])
            i += 1
            continue
        j = i
        while j < n and not flags[j]:
            j += 1
        run_len = j - i
        if run_len >= run_min and run_len > 2 * edge_keep:
            out.extend(lines[i : i + edge_keep])
            out.append(f"……（此处省略 {run_len - 2 * edge_keep} 行与技术和评分无关的内容）")
            out.extend(lines[j - edge_keep : j])
        else:
            out.extend(lines[i:j])
        i = j
    return out


def _cache_get(key: str) -> str | None:
    with _CACHE_LOCK:
        entry = _cache.get(key)
        if entry is None:
            return None
        result, ts = entry
        if time.time() - ts > _CACHE_TTL:
            _cache.pop(key, None)
            return None
    return result


def _cache_set(key: str, result: str) -> None:
    with _CACHE_LOCK:
        # 达到上限时淘汰最旧的一半
        if len(_cache) >= _CACHE_MAX:
            sorted_items = sorted(_cache.items(), key=lambda x: x[1][1])
            for k, _ in sorted_items[: len(sorted_items) // 2]:
                _cache.pop(k, None)
        _cache[key] = (result, time.time())


def has_scoring_content(text: str) -> bool:
    """快速判断文本是否包含评分相关关键词"""
    if not text:
        return False
    return any(kw in text for kw in _SCORING_KEYWORDS)


def extract_scoring_region(full_text: str) -> str:
    """从文本中提取评分相关区域，使用更大的窗口（200 行）

    Args:
        full_text: 完整招标文件文本

    Returns:
        评分相关片段
    """
    # BUG-TC-2: 原代码在 L349 先做 has_scoring_content 检查就直接 return ""，
    # 缓存检查在后面——导致不含评分关键词的文本每次都重复计算 has_scoring_content
    # + 缓存查找 + 锚定查找。缓存检查必须是第一行（空文本除外），所有 early return
    # 分支都写入缓存，避免重复计算。
    if not full_text:
        return ""

    cache_key = _cache_key(full_text)
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    if not has_scoring_content(full_text):
        _cache_set(cache_key, "")
        return ""

    lines = full_text.splitlines()
    scoring_indices = _find_anchors(lines, _SCORING_KEYWORDS)

    if not scoring_indices:
        _cache_set(cache_key, "")
        return ""

    stop_indices = _find_anchors(lines, STOP_ANCHORS)
    selected = set()

    for idx in scoring_indices:
        start = max(0, idx - _SCORING_WINDOW)
        end = min(len(lines), idx + _SCORING_WINDOW)

        # 排除商业/报价相关内容
        has_stop = any(start <= si <= end for si in stop_indices)
        if has_stop:
            stop_in_window = [si for si in stop_indices if start <= si <= end]
            for si in stop_in_window:
                if si < idx:
                    start = max(start, si + 1)
                elif si > idx:
                    end = min(end, si)

        for i in range(start, end):
            selected.add(i)

    selected_lines = [lines[i] for i in sorted(selected) if lines[i].strip()]
    # 同上：评分区域窗口（±200 行）同样会捎带大量格式性条款
    selected_lines = _compress_irrelevant_runs(selected_lines, _SCORING_KEYWORDS)
    result = "\n".join(selected_lines) if selected_lines else ""
    _cache_set(cache_key, result)
    return result


async def extract_tender_context_async(full_text: str, max_chars: int = _MAX_CHARS_DEFAULT) -> str:
    """异步版本：避免阻塞事件循环"""
    import asyncio

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, extract_tender_context, full_text, max_chars)


async def extract_scoring_region_async(full_text: str) -> str:
    """异步版本：避免阻塞事件循环"""
    import asyncio

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, extract_scoring_region, full_text)
