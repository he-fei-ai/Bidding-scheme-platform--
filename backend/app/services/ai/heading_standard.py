"""七级标题编号规范（商业级）

对应设计文档：
  《标书智能体（三）——生成标书正文代码+提示词（增强版 V3.1）》
  —— "二、标题编号规范" 章节

核心能力：
  1. 根据章节层级 + 路径计数器生成标准编号（第X节 / 一 / 1 / 1.1 / 1.1.1 / 1） / a）
  2. 提供完整的标题样式配置（字号/字体/加粗/无倾斜）
  3. 支持从已有文本反向检测层级（用于 DOCX 导出）
  4. 提供 7 种层级的批量格式化能力

STATUS 2026-10-13（L6 · 死代码评估结论，**有意保留不删**）：
  - `HeadingNumberingGenerator.normalize_content` /
    `workflows_base._fix_level4_heading_numbering` 当前**无生产调用方**，
    由 `tests/test_heading_numbering_fix_2026_10_06.py` 覆盖（下级计数器重置
    范围、deep 层级归一化等真实缺陷的回归保护）。
  - `services/sections.py::_strip_body_heading_numbering`（正文编号净化）已评估
    **不宜**在此接入：该链路的契约是「剥离 DB 编号语义变化后残留的内嵌编号」，
    与 normalize_content 的「按层级规范重编号」目标不同，接入会把用户手写的
    L5+ 编号一并改写（`HEADING_MANAGED_LEVELS` 只管 L1~L4 但归一化会动结构）。
    保留为导出侧可选的规范步骤，不在正文净化链调用。
"""

from __future__ import annotations

import re


# ============================================================
# 受控编号层级：仅 L1~L4 强制应用标准编号，L5~L7 保留原文格式
# ============================================================
HEADING_MANAGED_LEVELS: frozenset[int] = frozenset({1, 2, 3, 4})


# ============================================================
# 中文数字 / 字母映射
# ============================================================
# 支持 1-30 的中文数字（含"零"占位以保持索引一致）
CHINESE_NUMBERS: list[str] = [
    "",
    "一",
    "二",
    "三",
    "四",
    "五",
    "六",
    "七",
    "八",
    "九",
    "十",
    "十一",
    "十二",
    "十三",
    "十四",
    "十五",
    "十六",
    "十七",
    "十八",
    "十九",
    "二十",
    "二十一",
    "二十二",
    "二十三",
    "二十四",
    "二十五",
    "二十六",
    "二十七",
    "二十八",
    "二十九",
    "三十",
]

# 8 级英文小写字母（a-z 之后回到 aa）
ALPHABET: list[str] = [
    chr(ord("a") + i) if i < 26 else f"a{chr(ord('a') + i - 26)}" for i in range(52)
]


# ============================================================
# V3.1 七级标题样式配置（与用户定义标准完全一致）
# ============================================================
HEADING_STYLE_CONFIG: dict[int, dict] = {
    1: {  # 一级
        "prefix": "第",
        "suffix": "节",
        "number_format": "chinese",  # 一、二、三...
        "punctuation": "、",
        "font_size": 14,  # 四号
        "font_name": "宋体",
        "bold": True,
        "italic": False,
    },
    2: {  # 二级
        "prefix": "",
        "suffix": "",
        "number_format": "chinese",  # 一、二、三...
        "punctuation": "、",
        "font_size": 14,
        "font_name": "宋体",
        "bold": True,
        "italic": False,
    },
    3: {  # 三级
        "prefix": "",
        "suffix": "",
        "number_format": "digit",  # 1, 2, 3...
        "punctuation": "、",
        "font_size": 14,
        "font_name": "宋体",
        "bold": True,
        "italic": False,
    },
    4: {  # 四级
        "prefix": "",
        "suffix": "",
        "number_format": "digit_dot",  # 1.1, 1.2...
        "punctuation": "、",
        "font_size": 14,
        "font_name": "宋体",
        "bold": True,
        "italic": False,
    },
    5: {  # 五级 —— 正文样式：不加粗、字号/字体同正文（由 export.py 注入 size_body/font_body）
        "prefix": "",
        "suffix": "",
        "number_format": "digit_dot_dot",
        "punctuation": "、",
        "font_size": 12,  # 小四（正文字号）
        "font_name": "宋体",
        "bold": False,
        "italic": False,
        "body_style": True,  # 标记：导出时按正文样式渲染（字号/字体由 ExportConfig 注入）
    },
    6: {  # 六级 —— 正文样式
        "prefix": "",
        "suffix": "）",
        "number_format": "digit_parenthesis",
        "punctuation": "、",
        "font_size": 12,
        "font_name": "宋体",
        "bold": False,
        "italic": False,
        "body_style": True,
    },
    7: {  # 七级 —— 正文样式
        "prefix": "",
        "suffix": "",
        "number_format": "alpha",
        "punctuation": "、",
        "font_size": 12,
        "font_name": "宋体",
        "bold": False,
        "italic": False,
        "body_style": True,
    },
    8: {  # 八级 —— 正文样式（最底层细分）
        "prefix": "",
        "suffix": "",
        "number_format": "alpha_alpha",
        "punctuation": "、",
        "font_size": 12,
        "font_name": "宋体",
        "bold": False,
        "italic": False,
        "body_style": True,
    },
}


# ============================================================
# 七级标题正则（用于从文本反向检测层级）
# 注意：每个层级都包含 1-2 种正则以兼容 AI 偶尔输出的非标准格式
# ============================================================
HEADING_REGEX_PATTERNS: list[tuple[str, int, int]] = [
    # pattern, level, 纯标题所在组
    (r"^第([一二三四五六七八九十百千万零\d]+)[章节][、\s]+(.+)$", 1, 2),  # 第一节/第一章、标题
    (r"^([一二三四五六七八九十百千零]+)[、\s]+(.+)$", 2, 2),  # 一、标题
    (r"^(\d+)[、\s]+(.+)$", 3, 2),  # 1、标题
    (r"^(\d+\.\d+)[、\s]+(.+)$", 4, 2),  # 1.1、标题
    (r"^(\d+\.\d+\.\d+)[、\s]+(.+)$", 5, 2),  # 1.1.1、标题
    (r"^(\d+)[）)]\s*(.+)$", 6, 2),  # 1）/1) 标题（中英文右括号都识别）
    (r"^([a-z]{1,2})[、\s]+(.+)$", 7, 2),  # a、标题
]


# ============================================================
# 主类：HeadingNumberingGenerator
# ============================================================
class HeadingNumberingGenerator:
    """V3.1 七级标题编号生成器（商业级）

    使用示例：
        # 方式1：根据路径生成（推荐）
        counters = [0, 0, 0, 0, 0, 0, 0]
        counters, number, formatted = HeadingNumberingGenerator.next_heading(3, counters, "编制依据与适用标准")
        # → ("1", "1、编制依据与适用标准", updated_counters)

        # 方式2：纯格式化（不更新计数器）
        formatted = HeadingNumberingGenerator.format_heading(3, [1, 0, 0, 0, 0, 0, 0], "标题")
        # → "1、标题"

        # 方式3：反向检测
        level, pure_title = HeadingNumberingGenerator.detect_level("1.1、设计依据")
        # → (4, "设计依据")
    """

    @classmethod
    def generate_number(cls, level: int, counters: list[int]) -> str:
        """根据层级和计数器生成编号（V3.1 七级新标准）

        计数器布局（7级）：
          counters[0] = L1 计数
          counters[1] = L2 计数
          counters[2] = L3 计数（L4-L5 的根）
          counters[3] = L4 计数（L4 的第二位 / L5 的第二位）
          counters[4] = L5 计数（L5 的第三位）
          counters[5] = L6 计数
          counters[6] = L7 计数

        编号规范：
        L1: 第X节  L2: X(中文)  L3: X(阿拉伯)  L4: X.X  L5+: 保留原文（不强制编号）
        """
        if not 1 <= level <= 8:
            return ""

        # 受控层级仅 L1~L4；L5~L7 不强制编号，返回空串由调用方决定保留原文
        if level not in HEADING_MANAGED_LEVELS:
            return ""

        if level == 1:
            n = max(1, min(30, counters[0] or 1))
            return f"第{CHINESE_NUMBERS[n]}节"
        if level == 2:
            n = max(1, min(30, counters[1] or 1))
            return f"{CHINESE_NUMBERS[n]}"
        if level == 3:
            return f"{counters[2] or 1}"
        if level == 4:
            return f"{counters[2] or 1}.{counters[3] or 1}"
        return ""

    @classmethod
    def format_heading(cls, level: int, counters: list[int], title: str) -> str:
        """生成完整的标题文本（含编号）- V3.1 七级新标准"""
        if not title or not title.strip():
            return title or ""
        if not 1 <= level <= 8:
            return title

        # L5+ 不强制编号，直接返回原标题（由调用方决定是否保留原文编号）
        if level not in HEADING_MANAGED_LEVELS:
            return title

        number = cls.generate_number(level, counters)
        config = HEADING_STYLE_CONFIG[level]
        punct = config.get("punctuation", "")

        # L1-L4 统一使用：编号 + 顿号 + 标题
        return f"{number}{punct}{title}"

    @classmethod
    def next_heading(
        cls,
        level: int,
        counters: list[int],
        title: str,
    ) -> tuple[list[int], str, str]:
        """更新计数器并生成下一条标题（V3.1 七级新标准）

        计数器递增规则（L4 使用层级式编号）：
          L1: counters[0]+=1, reset [1..6]
          L2: counters[1]+=1, reset [2..6]
          L3: counters[2]+=1, reset [3..6]
          L4: counters[3]+=1, reset [4..6]  (counters[2] 保持不变作为父级)
          L5+: 不强制编号，仅更新计数器用于后续上级上下文

        受控层级：仅 L1~L4 强制生成标准编号；L5~L7 保留原文格式。
        """
        if not 1 <= level <= 8:
            return counters, "", title or ""

        new_counters = list(counters)

        # 计数器递增（所有层级都更新，保持上下文一致）
        level_idx_map = {1: 0, 2: 1, 3: 2, 4: 3, 5: 4, 6: 5, 7: 6, 8: 7}
        increment_idx = level_idx_map[level]

        # 自身层级 +1
        new_counters[increment_idx] = (new_counters[increment_idx] or 0) + 1

        # 重置下级计数器
        # FIX-2026-10-06：range 上界由 7 改为 8，与 level_idx_map 最大 idx=7（level=8）对齐；
        # 原写法在 level=7(idx=6) 时漏重置 counters[7]，level=8 时直接越界。
        reset_start = increment_idx + 1
        for i in range(reset_start, 8):
            new_counters[i] = 0

        # 自动补齐上级
        for i in range(level_idx_map[level]):
            if not new_counters[i]:
                new_counters[i] = 1

        # L5+ 不强制编号，保留原标题
        if level not in HEADING_MANAGED_LEVELS:
            return new_counters, "", title or ""

        number = cls.generate_number(level, new_counters)
        formatted = cls.format_heading(level, new_counters, title)
        return new_counters, number, formatted

    @classmethod
    def detect_level(cls, text: str) -> tuple[int, str]:
        """从文本反向检测标题层级（用于 DOCX 导出）

        Args:
            text: 完整文本行
        Returns:
            Tuple[level, pure_title]
            - level=0 表示非标题行
        """
        if not text or not text.strip():
            return 0, text or ""

        stripped = text.strip()
        for pattern, level, group_idx in HEADING_REGEX_PATTERNS:
            m = re.match(pattern, stripped)
            if m:
                # L6 (原 L7): 标题里可能没有顿号，需要去掉前导分隔符
                if level in (6,) and m.lastindex and m.lastindex >= group_idx:
                    pure = m.group(group_idx).strip()
                    # 去掉可能的前导 顿号 / 空格
                    pure = re.sub(r"^[、\s]+", "", pure)
                elif m.lastindex and m.lastindex >= group_idx:
                    pure = m.group(group_idx).strip()
                else:
                    pure = stripped
                return level, pure
        return 0, text

    @classmethod
    def get_style(cls, level: int) -> dict:
        """获取指定层级的完整样式配置"""
        return HEADING_STYLE_CONFIG.get(level, HEADING_STYLE_CONFIG[3])

    @classmethod
    def get_all_levels(cls) -> list[int]:
        """返回所有支持的层级 [1, 2, ..., 7]"""
        return [1, 2, 3, 4, 5, 6, 7]

    @classmethod
    def normalize_content(cls, content: str, *, normalize_deep_levels: bool = False) -> str:
        """V3.1 内容规范化：对原始正文重新应用七级编号（商业级）

        应用场景：
          - AI 生成的章节正文可能使用了错误的编号（一/二/三 vs 1/2/3）
          - 需要在文本生成后、导出 DOCX 前统一规范

        受控层级策略（2026-09-04 变更）：
          - L1~L4：强制应用标准编号（第X节、一、1、1.1）
          - L5~L7：默认仅识别为标题（保留 Heading 样式），但不强制重新编号，保留 AI 原文格式

        Args:
            content: 原始正文
            normalize_deep_levels: FIX-2026-10-06 新增。默认 False（向后兼容，
                L5-L8 保留 AI 原文）。设为 True 时，导出前对 L5-L8 也按 V4.0 模板
                重新编号（L5=1.1.1、L6=1）、L7=a、L8=aa），与目录树/导出编号一致。
                **不要**在正文生成入库路径启用——会破坏 AI 正文中的"见前述（1）"等
                内部引用；只在导出前的最后一道归一化里启用。

        Returns:
            规范化后的正文
        """
        if not content:
            return content

        # 不修改非文本行（表格/图表/代码围栏/引用块）
        SKIP_PREFIXES = ("<<", "[[", "```", "    ", "\t", "> ")
        out_lines: list[str] = []
        counters: list[int] = [0] * 8
        last_level = 0

        for raw_line in content.split("\n"):
            line = raw_line.rstrip()

            # 跳过空行
            if not line.strip():
                out_lines.append(line)
                continue
            # 跳过代码围栏/表格/图表标记
            stripped = line.lstrip()
            if any(stripped.startswith(p) for p in SKIP_PREFIXES):
                out_lines.append(line)
                continue
            # 跳过引用行/列表项
            if stripped.startswith(("-", "·", "•", "*")) and not cls._looks_like_heading(stripped):
                out_lines.append(line)
                continue
            # BUG 修复：增加中文顿号(、)到编号列表跳过模式
            # 防止 "1、施工前应..." 等正文列表项被误判为 L3 标题
            if re.match(r"^\d+[.\)）、]\s+", stripped) and not cls._looks_like_heading(stripped):
                out_lines.append(line)
                continue

            level, pure_title = cls.detect_level(stripped)
            if level == 0 or not pure_title:
                # 非标题：保持原样
                out_lines.append(line)
                continue

            # 上下文级联修正：AI 可能将 L6 标题误输出为 L3 格式（如 "5、" 而非 "1）"）
            # 当上一条标题为 L5 时，紧接的 "digit、" 格式应视为 L6
            if last_level >= 5 and level == 3:
                level = 6

            # 更新计数器（所有层级都更新以保持上下文一致）
            level_idx_map = {1: 0, 2: 1, 3: 2, 4: 3, 5: 4, 6: 5, 7: 6, 8: 7}
            increment_idx = level_idx_map[level]
            # FIX-2026-10-06：range 上界由 7 改为 8，覆盖 level=8(idx=7) 与 level=7 时的 L8 残留
            for i in range(increment_idx + 1, 8):
                counters[i] = 0
            counters[increment_idx] = (counters[increment_idx] or 0) + 1
            for i in range(increment_idx):
                if not counters[i]:
                    counters[i] = 1
            last_level = level

            # L5+ 处理
            if level not in HEADING_MANAGED_LEVELS:
                if not normalize_deep_levels:
                    # 默认行为：保留原文（向后兼容）
                    out_lines.append(line)
                    continue
                # FIX-2026-10-06：导出前归一化 L5-L8，与目录树/导出编号一致。
                # L5 = X.X.X、  L6 = X）、  L7 = a、  L8 = aa、
                indent = line[: len(line) - len(line.lstrip())]
                c = counters
                if level == 5:
                    num = f"{c[2] or 1}.{c[3] or 1}.{c[4] or 1}"
                    formatted = f"{num}、{pure_title}"
                elif level == 6:
                    num = f"{c[5] or 1}）"
                    formatted = f"{num}、{pure_title}"
                elif level == 7:
                    _letter_idx = max(1, min(52, c[6] or 1))
                    num = ALPHABET[_letter_idx - 1]
                    formatted = f"{num}、{pure_title}"
                elif level == 8:
                    _letter_idx = max(1, min(52, c[7] or 1))
                    num = ALPHABET[_letter_idx - 1]
                    formatted = f"{num}、{pure_title}"
                else:
                    formatted = pure_title
                out_lines.append(f"{indent}{formatted}")
                continue

            # L1~L4：重新生成标准编号
            formatted = cls.format_heading(level, counters, pure_title)
            # 保留原缩进
            indent = line[: len(line) - len(line.lstrip())]
            out_lines.append(f"{indent}{formatted}")

        return "\n".join(out_lines)

    @staticmethod
    def _looks_like_heading(text: str) -> bool:
        """快速判断文本是否像标题（V3.1 七级新标准）

        关键改进：通过句末标点检测区分标题与正文列表项。
        含句号/感叹号/问号的行通常是完整句子（正文），而非标题。
        """
        t = text.strip()
        if not t:
            return False
        # 标题不应以句末标点结尾（防止正文列表项被误判）
        if re.search(r"[。！？.!?]\s*$", t):
            return False
        # 有"第X节/第X章"明显编号 → 一级标题
        if re.match(r"^第[一二三四五六七八九十百千零\d]+[章节]", t):
            return True
        # 多级小数点编号（1.1.1）→ 五级（1.1.1.1 不再支持）
        if re.match(r"^\d+\.\d+\.\d+", t):
            return True
        if re.match(r"^\d+\.\d+\s*[、．.]", t):
            return True
        # 中文数字 + 顿号（一、二）→ 二级
        if re.match(r"^[一二三四五六七八九十百千]+\s*[、．.]", t):
            return len(t) <= 60
        # BUG 修复：阿拉伯数字 + 顿号（1、2）→ 三级，增加句末标点检查
        if re.match(r"^\d+\s*[、．.]", t):
            # 包含句末标点则不是标题（如 "1、施工前应进行技术交底。"）
            if re.search(r"[。！？.!?]", t):
                return False
            return len(t) <= 60
        # 数字 + 右括号（1）、2））→ 六级
        if re.match(r"^\d+[）)]\s*", t):
            # 包含句末标点则不是标题
            if re.search(r"[。！？.!?]", t):
                return False
            return len(t) <= 60
        # 字母 + 顿号（a、b）→ 七级
        if re.match(r"^[a-z]\s*[、．.]", t):
            # 包含句末标点则不是标题
            if re.search(r"[。！？.!?]", t):
                return False
            return len(t) <= 60
        return False


# ============================================================
# 便捷函数（与设计文档示例保持一致）
# ============================================================
def build_heading_spec_prompt() -> str:
    """生成 V3.1 标题编号规范说明（用于注入到 System Prompt）

    受控层级策略（2026-09-04 变更）：
      L1~L4 强制标准编号 + 标题样式；L5~L7 仅识别为标题，编号/字体格式同正文。
    """
    return """【标题编号规范 - 必须严格遵守】

所有章节标题必须按以下格式编号，不得自行更改：

=== 强制管理层级（L1~L4）===

一级标题（四号字，宋体，加粗）：第X节、标题
  示例：第一节、图文表达与编制格式规范
  示例：第一章、施工组织设计

二级标题（四号字，宋体，加粗）：X、标题（中文数字）
  示例：一、编制依据与适用标准

三级标题（四号字，宋体，加粗）：X、标题（阿拉伯数字）
  示例：1、设计与施工规范依据

四级标题（四号字，宋体，加粗）：X.X、标题
  示例：1.1、工程质量管控

=== 正文样式层级（L5~L8，不强制编号格式，字体同正文）===

五级标题：不加粗，字号/字体同正文，编号格式可自由选择（如 1.1.1、X、（一）等）。
六级标题：不加粗，字号/字体同正文，编号格式可自由选择。
七级标题：不加粗，字号/字体同正文，编号格式可自由选择。
八级标题：不加粗，字号/字体同正文，编号格式可自由选择。

重要规则：
1. 所有标题禁止使用倾斜（斜体）
2. L1~L4 的标点符号必须严格按示例（顿号、括号）
3. L1~L4 必须使用对应的编号格式，不得混用
4. L5~L7 编号格式不限，但必须能从内容上区分层级
5. L1~L4 标题加粗、四号字；L5~L7 不加粗、字号/字体同正文
6. 七级为最深层级，不再支持更深的小数点编号（如 1.1.1.1）
"""


# ============================================================
# V6.0 增强：HeadingNumberingGeneratorV2（父级ID追踪 + 自动重置）
# ============================================================
class HeadingNumberingGeneratorV2:
    """多级标题编号生成器（V6.0 增强版） - 支持八级标题，每级在父级变更时自动重置

    与 HeadingNumberingGenerator（静态方法版）的区别：
      - 实例化后维护状态（计数器 + 父级ID）
      - 通过 update_counter(level, parent_id) 检测父级变更并自动重置
      - 适用于流式生成场景：DOCX 导出、实时正文生成

    使用示例：
        gen = HeadingNumberingGeneratorV2()
        # 一级标题
        num = gen.update_counter(1, "root")
        # → "第一章"
        # 二级标题（父级=当前一级ID）
        num = gen.update_counter(2, "ch1")
        # → "第一节"
        # 新的一级标题 → 二级自动重置
        num = gen.update_counter(1, "root")
        # → "第二章"
        num = gen.update_counter(2, "ch2")
        # → "第一节"（已重置）
    """

    CHINESE_NUMBERS = [
        "",
        "一",
        "二",
        "三",
        "四",
        "五",
        "六",
        "七",
        "八",
        "九",
        "十",
        "十一",
        "十二",
        "十三",
        "十四",
        "十五",
        "十六",
        "十七",
        "十八",
        "十九",
        "二十",
        "二十一",
        "二十二",
        "二十三",
        "二十四",
        "二十五",
        "二十六",
        "二十七",
        "二十八",
        "二十九",
        "三十",
    ]
    ALPHABET = [
        "",
        "a",
        "b",
        "c",
        "d",
        "e",
        "f",
        "g",
        "h",
        "i",
        "j",
        "k",
        "l",
        "m",
        "n",
        "o",
        "p",
        "q",
        "r",
        "s",
        "t",
        "u",
        "v",
        "w",
        "x",
        "y",
        "z",
        "aa",
        "ab",
        "ac",
        "ad",
        "ae",
        "af",
        "ag",
        "ah",
        "ai",
        "aj",
        "ak",
        "al",
        "am",
        "an",
        "ao",
        "ap",
        "aq",
        "ar",
        "as",
        "at",
        "au",
        "av",
        "aw",
        "ax",
        "ay",
        "az",
    ]

    def __init__(self):
        # 各级别当前计数器（V3.1 八级，索引 0-7 对应 level 1-8）
        # 布局：[L1, L2, L3, L4, L5, L6, L7, L8]
        # L4-L5 采用层级式编号：
        #   L4 = counters[2].counters[3]  (L3.L4)
        #   L5 = counters[2].counters[3].counters[4]
        # FIX-2026-10-06：原数组只有 7 个元素，level=8(idx=7) 时
        # self.parent_ids[7] / self.counters[7] 越界 IndexError；
        # 导出遍历用户手工创建的 8 级目录时直接崩溃。
        self.counters = [0, 0, 0, 0, 0, 0, 0, 0]
        # 各级别父级ID（用于检测父级变更）
        self.parent_ids: list[str | None] = [None, None, None, None, None, None, None, None]

    def update_counter(self, level: int, parent_id: str) -> str:
        """更新计数器并返回当前级别的编号（V3.1 七级）

        受控层级：仅 L1~L4 强制生成标准编号；L5~L7 返回空串，调用方应保留原文。
        """
        if not 1 <= level <= 8:
            return ""

        # 先更新计数器（所有层级都更新以维持父级上下文）
        level_idx_map = {1: 0, 2: 1, 3: 2, 4: 3, 5: 4, 6: 5, 7: 6, 8: 7}
        idx = level_idx_map[level]

        # 检测父级是否变更
        if self.parent_ids[idx] != parent_id:
            self.counters[idx] = 0
            self.parent_ids[idx] = parent_id
            # 重置所有下级计数器
            # FIX-2026-10-06：上界 7→8，与 8 元素数组对齐
            for i in range(idx + 1, 8):
                self.counters[i] = 0
                self.parent_ids[i] = None

        self.counters[idx] += 1

        # L5+ 不强制编号
        if level not in HEADING_MANAGED_LEVELS:
            return ""

        return self._format_number(level)

    def _format_number(self, level: int) -> str:
        """根据层级格式化编号（V3.1 七级新标准）"""
        # 受控层级检查
        if level not in HEADING_MANAGED_LEVELS:
            return ""

        c = self.counters
        if level == 1:
            n = max(1, min(30, c[0]))
            return f"第{self.CHINESE_NUMBERS[n]}节"
        elif level == 2:
            n = max(1, min(30, c[1]))
            return f"{self.CHINESE_NUMBERS[n]}"
        elif level == 3:
            return f"{c[2] or 1}"
        elif level == 4:
            return f"{c[2] or 1}.{c[3] or 1}"
        return ""

    def format_heading(self, level: int, title: str, parent_id: str) -> str:
        """生成完整标题（含编号和标点）

        Args:
            level: 1-7
            title: 纯标题文本
            parent_id: 父级标识

        Returns:
            str: 完整标题（如 "第一章、施工组织设计"）
                 L5+ 返回原 title（不强制加编号）
        """
        number = self.update_counter(level, parent_id)
        # L5+ 不强制编号，update_counter 已返回 ""
        if not number:
            return title
        config = HEADING_STYLE_CONFIG.get(level, HEADING_STYLE_CONFIG[3])
        punct = config.get("punctuation", "")
        return f"{number}{punct}{title}"

    def reset(self):
        """重置所有计数器"""
        # FIX-2026-10-06：与 __init__ 保持 8 元素一致
        self.counters = [0, 0, 0, 0, 0, 0, 0, 0]
        self.parent_ids = [None, None, None, None, None, None, None, None]


# ============================================================
# V4.0 增强：基于 ID 路径的模板化编号系统（对齐设计文档）
# ============================================================

# 默认编号模板配置（对齐 V3.1 七级标题标准）
# 对应设计文档中的 DEFAULT_EXPORT_FORMAT，调整为 V3.1 规范：
#   L1: 第X节、  L2: X、(中文数字)  L3: X、(阿拉伯数字)  L4: X.X、
#   L5: X.X.X、  L6: X）、  L7: a、
#
# ID 约定（与设计文档不同，更简洁）：
#   L1/L2/L3/L6/L7: id="N"  (仅当前级序号)
#   L4: id="N.N"  (L3序号.L4序号)
#   L5: id="N.N.N"  (L3.L4.L5)
#
# 模板占位符选择：
#   L3 使用 {num} (仅显示最后一级数字)
#   L4-L5 使用 {full} (显示完整点分路径)
#   注意：模板不含标点，由 format_heading_by_id 统一添加顿号"、"
DEFAULT_NUMBERING_TEMPLATES: dict[int, dict[str, str]] = {
    1: {"numbering_format": "custom", "numbering_template": "第{zh}节"},
    2: {"numbering_format": "custom", "numbering_template": "{zh}"},
    3: {"numbering_format": "custom", "numbering_template": "{num}"},
    4: {"numbering_format": "custom", "numbering_template": "{full}"},
    5: {"numbering_format": "custom", "numbering_template": "{full}"},
    6: {"numbering_format": "custom", "numbering_template": "{num}）"},
    7: {"numbering_format": "custom", "numbering_template": "{alpha}"},
}

# 圈码数字映射（①-⑳）
CIRCLED_NUMBERS: dict[int, str] = {
    1: "①",
    2: "②",
    3: "③",
    4: "④",
    5: "⑤",
    6: "⑥",
    7: "⑦",
    8: "⑧",
    9: "⑨",
    10: "⑩",
    11: "⑪",
    12: "⑫",
    13: "⑬",
    14: "⑭",
    15: "⑮",
    16: "⑯",
    17: "⑰",
    18: "⑱",
    19: "⑲",
    20: "⑳",
}

# 罗马数字映射
ROMAN_NUMBERS: list[str] = [
    "",
    "i",
    "ii",
    "iii",
    "iv",
    "v",
    "vi",
    "vii",
    "viii",
    "ix",
    "x",
    "xi",
    "xii",
    "xiii",
    "xiv",
    "xv",
    "xvi",
    "xvii",
    "xviii",
    "xix",
    "xx",
]
ROMAN_NUMBERS_UPPER: list[str] = [
    "",
    "I",
    "II",
    "III",
    "IV",
    "V",
    "VI",
    "VII",
    "VIII",
    "IX",
    "X",
    "XI",
    "XII",
    "XIII",
    "XIV",
    "XV",
    "XVI",
    "XVII",
    "XVIII",
    "XIX",
    "XX",
]


def outline_number_parts(id_str: str) -> list[int]:
    """将点分 ID 拆分为数字数组（对应设计文档 outlineNumbering.ts:73-78）

    Args:
        id_str: 点分 ID，如 "1", "1.1", "1.1.1"

    Returns:
        数字数组，如 [1], [1, 1], [1, 1, 1]

    Examples:
        >>> outline_number_parts("1")
        [1]
        >>> outline_number_parts("1.1")
        [1, 1]
        >>> outline_number_parts("1.1.1.1")
        [1, 1, 1, 1]
    """
    return [int(p) for p in str(id_str or "").split(".") if p.isdigit() and int(p) > 0]


def format_outline_number_by_template(id_str: str, template: str) -> str:
    """基于模板格式化编号（对应设计文档 outlineNumbering.ts:85-114）

    支持的占位符：
    - {tail}: 第三级及以后的编号（parts.length>=3 时取 parts[2:]，否则取最后一级）
    - {tailN}: 第 N 级及以后的编号（如 {tail2} 表示从第 2 级起）
    - {zh}: 最后一级的中文数字
    - {num}: 最后一级的阿拉伯数字
    - {full}: 完整点分 ID
    - {circled}: 圈码数字（①②③）
    - {alpha}: 小写英文字母（a,b,c）
    - {ALPHA}: 大写英文字母（A,B,C）
    - {roman}: 小写罗马数字（i,ii,iii）
    - {ROMAN}: 大写罗马数字（I,II,III）

    Args:
        id_str: 点分 ID，如 "1.2.3"
        template: 模板字符串，如 "{tail}", "第{zh}章"

    Returns:
        格式化后的编号字符串

    Examples:
        >>> format_outline_number_by_template("1.1.1", "{tail}")
        "1"
        >>> format_outline_number_by_template("1.1.1.1", "{tail}")
        "1.1"
        >>> format_outline_number_by_template("1", "第{zh}章")
        "第一章"
    """
    parts = outline_number_parts(id_str)
    if not parts:
        return ""

    last_part = parts[-1]
    full_str = ".".join(str(p) for p in parts)

    # {zh}: 中文数字（支持 1-30）
    cn = CHINESE_NUMBERS[last_part] if 0 < last_part < len(CHINESE_NUMBERS) else str(last_part)

    # {alpha}: 小写英文字母
    alpha = ALPHABET[last_part - 1] if 1 <= last_part <= len(ALPHABET) else "a"

    # {ALPHA}: 大写英文字母
    alpha_upper = alpha.upper()

    # {circled}: 圈码数字
    circled = CIRCLED_NUMBERS.get(last_part, str(last_part))

    # {roman}: 小写罗马数字
    roman = ROMAN_NUMBERS[last_part] if 0 < last_part < len(ROMAN_NUMBERS) else str(last_part)

    # {ROMAN}: 大写罗马数字
    roman_upper = (
        ROMAN_NUMBERS_UPPER[last_part]
        if 0 < last_part < len(ROMAN_NUMBERS_UPPER)
        else str(last_part)
    )

    # {tail} 特殊语义：当 parts.length >= 3 时取第三级起，否则取最后一级
    if len(parts) >= 3:
        tail = ".".join(str(p) for p in parts[2:])
    else:
        tail = str(last_part)

    # 模板替换
    result = template
    result = re.sub(r"\{tail(\d+)\}", lambda m: _replace_tail_n(m, parts), result)
    result = result.replace("{tail}", tail)
    result = result.replace("{zh}", cn)
    result = result.replace("{num}", str(last_part))
    result = result.replace("{full}", full_str)
    result = result.replace("{circled}", circled)
    result = result.replace("{alpha}", alpha)
    result = result.replace("{ALPHA}", alpha_upper)
    result = result.replace("{roman}", roman)
    result = result.replace("{ROMAN}", roman_upper)

    return result.strip()


def _replace_tail_n(m: re.Match, parts: list[int]) -> str:
    """{tailN} 占位符替换：从第 N 级起

    Args:
        m: 正则匹配对象，group(1) 为 N
        parts: ID 数字数组

    Returns:
        从第 N 级起的点分字符串
    """
    n = int(m.group(1))
    if len(parts) >= n:
        return ".".join(str(p) for p in parts[n - 1 :])
    # 如果 parts 长度不够，返回最后一级
    return str(parts[-1] if parts else 1)


def get_heading_template(level: int) -> dict[str, str]:
    """获取指定层级的默认编号模板配置

    Args:
        level: 层级 (1-8)

    Returns:
        包含 numbering_format 和 numbering_template 的字典
    """
    return DEFAULT_NUMBERING_TEMPLATES.get(
        level,
        DEFAULT_NUMBERING_TEMPLATES.get(
            3, {"numbering_format": "custom", "numbering_template": "{tail}"}
        ),
    )


def format_heading_by_id(
    id_str: str, level: int, title: str, custom_template: str | None = None
) -> str:
    """基于 ID 路径生成完整标题（含编号）

    Args:
        id_str: 点分 ID，如 "1.1.1"
        level: 层级 (1-8)
        title: 标题文本
        custom_template: 自定义模板（可选），如果提供则覆盖默认模板

    Returns:
        完整标题字符串，如 "1.1、标题内容"

    Examples:
        >>> format_heading_by_id("1.1", 4, "工程质量管控")
        "1.1、工程质量管控"
        >>> format_heading_by_id("1", 1, "总体设计")
        "第一章、总体设计"
    """
    if not title or not title.strip():
        return title or ""

    # 获取模板
    if custom_template:
        template = custom_template
    else:
        config = get_heading_template(level)
        template = config.get("numbering_template", "{tail}")

    # 生成编号
    number = format_outline_number_by_template(id_str, template)
    if not number:
        return title

    # 获取标点符号
    style_config = HEADING_STYLE_CONFIG.get(level, HEADING_STYLE_CONFIG[3])
    punct = style_config.get("punctuation", "、")

    # 拼接完整标题
    return f"{number}{punct}{title.strip()}"


def should_insert_space_after_number(prefix: str) -> bool:
    """判断编号后是否需要加空格（对应设计文档 outlineNumbering.ts:119-121）

    如果前缀以中文标点结尾，则不加空格；否则加空格。

    Args:
        prefix: 编号前缀

    Returns:
        是否需要加空格
    """
    return not bool(re.search(r"[、，。；：）)】\]》〉]$", prefix))


def format_outline_title(
    id_str: str, title: str, level: int, custom_template: str | None = None
) -> str:
    """格式化目录标题（对齐 V3.0 八级标准，使用顿号分隔）

    与设计文档的区别：设计文档使用空格分隔，本实现使用顿号"、"分隔，
    以符合 V3.0 八级标题标准（第X节、X、X.X、...）。

    Args:
        id_str: 点分 ID
        title: 标题文本
        level: 层级
        custom_template: 自定义模板（可选）

    Returns:
        格式化后的标题（使用顿号分隔）
    """
    # 复用 format_heading_by_id 的逻辑，保持一致性
    return format_heading_by_id(id_str, level, title, custom_template)


# 将新功能集成到 HeadingNumberingGenerator 类中
def _generate_number_by_id(cls, id_str: str, level: int) -> str:
    """基于 ID 路径生成编号（新增方法）

    Args:
        id_str: 点分 ID，如 "1.1.1"
        level: 层级 (1-8)

    Returns:
        编号字符串
    """
    config = get_heading_template(level)
    template = config.get("numbering_template", "{tail}")
    return format_outline_number_by_template(id_str, template)


def _format_heading_by_id(cls, id_str: str, level: int, title: str) -> str:
    """基于 ID 路径生成完整标题（新增方法）

    Args:
        id_str: 点分 ID
        level: 层级
        title: 标题文本

    Returns:
        完整标题（含编号和标点）
    """
    return format_heading_by_id(id_str, level, title)


# 绑定新方法到 HeadingNumberingGenerator 类
HeadingNumberingGenerator.generate_number_by_id = classmethod(_generate_number_by_id)
HeadingNumberingGenerator.format_heading_by_id = classmethod(_format_heading_by_id)


# ============================================================
# 测试（开发期）
# ============================================================
