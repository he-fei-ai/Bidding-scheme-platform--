"""Mermaid 语法预处理器（Sanitizer）。

BUG-2026-1008-01 修复（P0）：AI 生成 Mermaid 代码时经常违反语法规范——
节点标签未加双引号（含中文冒号/日期/百分号等特殊字符）、连写多节点连接
（A --> B --> C）、行末分号、非 ASCII 节点 ID——导致 HTTP 渲染服务
（Docker mmdc）与 PIL v2 解析器都解析失败，图表被静默降级为纯文本。

本模块作为渲染管道入口的「语法预处理」层：
  - 幂等（idempotent）：对已合规代码无副作用，sanitize 两次结果一致
  - 保守（conservative）：无法确定时保持原样，不破坏可读性
  - 仅处理 Mermaid 语法代码（flowchart/graph/gantt/timeline），
    JSON 数据载荷（architecture/labor 等）不进入本层

典型修复示例：
  输入:
    flowchart TD
    A[开工: 2026-09-22] --> B[出地面: 2026-12-15]
  输出:
    flowchart TD
    A["开工: 2026-09-22"] --> B["出地面: 2026-12-15"]
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)


# 触发「加双引号」的字符集：冒号/逗号/百分号/斜杠/括号/连字符（用于日期）。
# 中文也单独判一次（\u4e00-\u9fff），因为中文在 Mermaid 中常需引号包裹。
_SPECIAL_LABEL_CHARS = re.compile(r"[:：,，/\\%（）()\[\]{}\-]")
_CHINESE_CHARS = re.compile(r"[\u4e00-\u9fff]")

# 匹配「ID[标签]」形式（label 内不允许出现方括号，避免嵌套误匹配）。
# \w 在 Python 中匹配 [A-Za-z0-9_] 加 Unicode 字母（含中文），
# 因此本正则同时覆盖 ASCII ID 与中文 ID 两种情况。
_NODE_LABEL_PATTERN = re.compile(r"(\w+)\[([^\[\]]+)\]")

# ------------------------------------------------------------------
# 裸节点边修复（BUG-2026-1008-01 P0 核心修复）
# ------------------------------------------------------------------
# AI 经常直接输出「无方括号」的裸节点边，例如修复方案中的失败样例：
#   flowchart TD
#   开工: 2026-09-22 --> 出地面: 2026-12-15
#   出地面: 2026-12-15 --> 主体50%: 2027-04-30
# 这类代码既无节点 ID 也无方括号，Mermaid 解析失败、PIL 解析器也无法
# 识别，导致图表被静默降级（丢失或占位）。本模块在语法预处理阶段将其
# 改写为合法的 `N1["开工: 2026-09-22"] --> N2["出地面: 2026-12-15"]`。
# 箭头切分正则：长模式优先，避免 `--` 被 `--x` / `-->` 误切。
_ARROW_SPLIT_RE = re.compile(r"(<-->|-\.->|==>|--x|--o|-\.-|-->|---|==)")
# 不参与裸节点处理的声明/结构行前缀
_SKIP_LINE_PREFIXES = (
    "flowchart", "graph", "subgraph", "end", "classDef", "class",
    "style", "click", "linkStyle", "%%",
)
# 边标签 `A -->|说明| B` 中的 `|...|`
_EDGE_LABEL_RE = re.compile(r"^\|(.*?)\|\s*(.*)$")

# ------------------------------------------------------------------
# 边文本（legacy edge text）修复（BUG-2026-1011-01 P0）
# ------------------------------------------------------------------
# Mermaid 允许用「连接符 + 文本 + 箭头」给边加标签：
#   A -- "通过" --> B     /     A -- 通过 --> B     /     A ==> 通过 ==> B
# 这类行里的 `A -- 通过` 既不是节点，也不是「裸节点标签」。但
# `_ARROW_SPLIT_RE` 只识别 `-->` 这类完整箭头，于是 `A -- 通过` 会被
# `_bracketize_bare_nodes` 当成裸节点，括号化成 `N1["A -- \"通过\""]`：
#   - 渲染出 `A -- "通过"` 这种垃圾节点（且含「通过/合格」等词会被判成菱形）
#   - 真实边标签丢失，A 与 B 之间的连线变成 N1 -> B（拓扑错乱、ID 被顶替）
# 因此必须在裸节点括号化【之前】把边文本改写为规范形式 `A -->|通过| B`。
#
# 匹配约束（避免误伤合规代码）：
#   - 连接符只取 `--` / `==>`（后面必须紧跟「文本 + 箭头」）；`-->` 本身
#     因箭头后紧跟 `>` 而非「文本」，不会命中；
#   - src 允许 `ID` 或 `ID["标签"]` 形式的完整节点，括号内即使含 `--`
#     也已作为整体消费，不会被切断。
# 例：N1["工期 2026-09-22 -- 2026-12-15"] --> N2 不会命中（src 消费完
# 整个方括号后，剩余 ` --> N2` 无法匹配「文本 + 箭头」）。
_LEGACY_EDGE_TEXT_RE = re.compile(
    r"^\s*"
    r"(?P<src>[^\s|>]+(?:\[[^\]]*\]|\{[^}]*\}|\([^()]*\))*)"  # 源节点（ID 或带括号标签）
    r"\s*"
    r"(?P<conn>(?:--|==)(?!>))"                             # 连接符 -- 或 ==，但【不能】后接 >（否则会把 --> 拆成 "--" + ">" 误判为边文本）
    r"\s*"
    r"(?P<label>[^|\n>]+?)"                                 # 边文本：非空、不含 > 和 |（不能跨越箭头或另一个标签）
    r"\s*"
    r"(?P<arrow>-->|==>)"                                    # 目标箭头
    r"\s*"
    r"(?P<dst>.*)$"
)

# 匹配节点 ID 行首（后跟括号形式的标签）—— 用于识别非 ASCII ID。
_NODE_ID_PREFIX_PATTERN = re.compile(r"^(\w+)\s*[\[\(\{]", re.MULTILINE)
class MermaidSanitizer:
    """Mermaid 代码语法预处理器。

    调用方式::

        sanitizer = MermaidSanitizer()
        cleaned = sanitizer.sanitize(raw_mermaid_code)

    或模块级单例::

        from app.services.ai.mermaid_sanitizer import SANITIZER
        cleaned = SANITIZER.sanitize(raw_mermaid_code)
    """

    def sanitize(self, code: str) -> str:
        """按顺序执行四步语法清洗，返回规范化后的 Mermaid 代码。

        Args:
            code: 原始 Mermaid 代码字符串（可能已被 AI 生成、未经校验）

        Returns:
            规范化后的 Mermaid 代码。空/None 输入原样返回。
        """
        if not code or not isinstance(code, str):
            return code

        original = code
        # Step 0a: 边文本规范化（必须最先执行）
        # 把 Mermaid 的 legacy 边文本写法 `A -- "通过" --> B` 改写为
        # 规范的 `A -->|"通过"| B`。若不先处理，下一步 `_bracketize_bare_nodes`
        # 会把 `A -- "通过"` 当成裸节点标签括号化，产生垃圾节点并破坏拓扑。
        code = self._normalize_edge_text(code)
        # Step 0: 裸节点边修复（核心 P0 修复，必须最先执行——
        # 把 `文本 --> 文本` 改写为 `N1["文本"] --> N2["文本"]`，
        # 否则后续步骤无法识别节点，图表仍会降级）
        code = self._bracketize_bare_nodes(code)
        # Step 1: 为未加引号且含特殊字符/中文的节点标签添加双引号
        code = self._add_quotes_to_labels(code)
        # Step 2: 展开 A --> B --> C 形式的多节点连写
        code = self._expand_multi_connections(code)
        # Step 3: 移除行末分号
        code = self._remove_trailing_semicolons(code)
        # Step 4: 规范化非 ASCII 节点 ID（中文 ID → N1/N2/...）
        code = self._normalize_node_ids(code)

        if code != original:
            logger.info(
                "[MermaidSanitizer] 语法预处理生效: 原长=%d 新长=%d",
                len(original),
                len(code),
            )
        return code

    # ------------------------------------------------------------------
    # Step 0a: 边文本规范化（BUG-2026-1011-01 P0）
    # ------------------------------------------------------------------
    def _normalize_edge_text(self, code: str) -> str:
        """把 legacy 边文本写法改写为规范的 `|label|` 形式。

        Mermaid 支持两种给边加标签的语法，AI 两种都会输出：
          规范:  A -->|"通过"| B          A ==>|通过| B
          legacy: A -- "通过" --> B       A ==> 通过 ==> B
        本方法只负责把后者改写为前者，且对已是规范形式的代码零改动（幂等）。

        示例：
          D1 -- "通过" --> N3["竣工验收"]   →   D1 -->|"通过"| N3["竣工验收"]
          A[开工] -- 是 --> B[竣工]         →   A[开工] -->|是| B[竣工]
          A ==> 通过 ==> B                  →   A ==>|通过| B
          A -->|"通过"| B                   →   原样（已规范）
          A[工期 2026-09-22 -- 2026-12-15] --> B → 原样（括号内文本不算边文本）
        """
        if not code:
            return code
        if "--" not in code and "==>" not in code:
            return code

        out_lines: list[str] = []
        for line in code.split("\n"):
            stripped = line.strip()
            # 声明/结构行跳过（与裸节点修复保持一致）
            if not stripped:
                out_lines.append(line)
                continue
            _first = stripped.split()[0].rstrip(":")
            if _first in _SKIP_LINE_PREFIXES or stripped.startswith(_SKIP_LINE_PREFIXES):
                out_lines.append(line)
                continue
            # 最多迭代 3 次，覆盖 `A -- 是 --> B -- 否 --> C` 这类多次改写
            current = line
            for _ in range(3):
                m = _LEGACY_EDGE_TEXT_RE.match(current)
                if not m:
                    break
                src = m.group("src").strip()
                label = m.group("label").strip().replace('\\"', '"')
                arrow = m.group("arrow")
                dst = m.group("dst").strip()
                if not src or not label:
                    break
                current = f"{src} {arrow}|{label}| {dst}" if dst else f"{src} {arrow}|{label}|"
            out_lines.append(current)
        return "\n".join(out_lines)

    # ------------------------------------------------------------------
    # Step 0: 裸节点边修复（核心 P0 修复）
    # ------------------------------------------------------------------
    def _bracketize_bare_nodes(self, code: str) -> str:
        """将「无方括号」的裸节点边改写为合法 Mermaid 节点声明。

        例：
          flowchart TD
          开工: 2026-09-22 --> 出地面: 2026-12-15
            ↓
          flowchart TD
          N1["开工: 2026-09-22"] --> N2["出地面: 2026-12-15"]

        规则：
          - 仅处理含箭头的边行；已带 `[`/`{`/`(` 的节点保持原样；
          - 同一裸标签跨行共享同一 ID（保证连线连通、方向正确）；
          - 边标签 `|说明|` 原样保留并附加到后续节点；
          - 声明行（flowchart/graph/subgraph/end/样式/注释）跳过。
        """
        if not code:
            return code
        if not ("-->" in code or "---" in code or "==>" in code):
            return code

        label_to_id: dict[str, str] = {}
        counter = [0]

        def _wrap(seg_text: str) -> str:
            seg = seg_text.strip()
            if not seg:
                return ""
            # 已带形状括号（方/花/圆）→ 保持原样
            if "[" in seg or "{" in seg or "(" in seg:
                return seg
            # 悬挂的边文本（legacy 写法残留）：`B -- 否` / `B ==> 通过`。
            # 这既不是节点也不是裸节点标签，若直接括号化会渲染出
            # `B -- 否` 这样的垃圾节点。只保留节点部分，避免污染拓扑。
            m_edge = re.match(
                r"^(?P<node>[^\s|>\[\]{()}]+(?:\[[^\]]*\]|\{[^}]*\}|\([^()]*\))*)"
                r"\s+(?:--|-\.->|==>)\s*\S",
                seg,
            )
            if m_edge:
                return m_edge.group("node")
            # 边标签 |...|
            m = _EDGE_LABEL_RE.match(seg)
            if m:
                label = m.group(1)
                rest = m.group(2).strip()
                if rest:
                    return f"|{label}|{_wrap(rest)}"
                return seg
            # 合法 ASCII 节点 ID（字母开头、仅字母/数字/下划线）→ Mermaid 原生支持，
            # 保持原样（如 `A --> B`、`start --> end`），避免过度改写。
            if re.match(r"^[A-Za-z][A-Za-z0-9_]*$", seg):
                return seg
            nid = label_to_id.get(seg)
            if nid is None:
                counter[0] += 1
                nid = f"N{counter[0]}"
                label_to_id[seg] = nid
            escaped = seg.replace('"', '\\"')
            return f'{nid}["{escaped}"]'

        out_lines: list[str] = []
        for line in code.split("\n"):
            stripped = line.strip()
            if not stripped:
                out_lines.append(line)
                continue
            _first = stripped.split()[0].rstrip(":")
            if _first in _SKIP_LINE_PREFIXES or stripped.startswith(_SKIP_LINE_PREFIXES):
                out_lines.append(line)
                continue
            if not _ARROW_SPLIT_RE.search(line):
                out_lines.append(line)
                continue
            parts = _ARROW_SPLIT_RE.split(line)
            rebuilt = ""
            for i, p in enumerate(parts):
                if i % 2 == 1:
                    rebuilt += f" {p} "  # 箭头，带空格保持可读
                else:
                    rebuilt += _wrap(p)
            out_lines.append(rebuilt)
        return "\n".join(out_lines)

    # ------------------------------------------------------------------
    # Step 1: 节点标签加引号
    # ------------------------------------------------------------------
    def _add_quotes_to_labels(self, code: str) -> str:
        """为未加引号的节点标签添加双引号。

        触发条件（label 满足任一即加引号）：
          - 含中文（\u4e00-\u9fff）
          - 含特殊字符：冒号、逗号、百分号、斜杠、括号、连字符

        已加引号（首字符为 `"` 或 `'`）的标签保持不变。
        纯 ASCII 且无特殊字符的标签（如 `[Start]`）保持不变——
        避免把大量合规代码误加引号（不影响渲染但增加视觉噪音）。

        内部双引号会转义为 \\"，防止破坏外层引号。
        """
        if not code:
            return code

        def _replacer(match: re.Match[str]) -> str:
            node_id = match.group(1)
            label = match.group(2)
            # 已有引号 → 保持不变
            if label.startswith('"') or label.startswith("'"):
                return match.group(0)
            # 需要加引号的条件
            if _SPECIAL_LABEL_CHARS.search(label) or _CHINESE_CHARS.search(label):
                escaped = label.replace('"', '\\"')
                return f'{node_id}["{escaped}"]'
            # 纯 ASCII 无特殊字符 → 保持不变
            return match.group(0)

        return _NODE_LABEL_PATTERN.sub(_replacer, code)

    # ------------------------------------------------------------------
    # Step 2: 展开多节点连写
    # ------------------------------------------------------------------
    def _expand_multi_connections(self, code: str) -> str:
        """展开 `A --> B --> C` 形式的多节点连写为多行。

        仅处理 `-->` 类型的箭头链式（Mermaid 中最常见的有向边）。
        `===` / `-.-` / `<->` 等其他箭头形态不在此处理范围内——
        它们的语义与语法不同，误展开可能改变图拓扑。

        示例：
          A --> B --> C
            ↓
          A --> B
          B --> C
        """
        if "-->" not in code:
            return code

        lines = code.split("\n")
        result: list[str] = []

        for line in lines:
            stripped = line.strip()
            arrow_count = stripped.count("-->")
            if arrow_count > 1:
                parts = [p.strip() for p in stripped.split("-->")]
                # 过滤掉空段（末尾多余箭头产生的空 tail）
                parts = [p for p in parts if p]
                if len(parts) < 2:
                    result.append(line)
                    continue
                for i in range(len(parts) - 1):
                    result.append(f"{parts[i]} --> {parts[i + 1]}")
            else:
                result.append(line)

        return "\n".join(result)

    # ------------------------------------------------------------------
    # Step 3: 移除行末分号
    # ------------------------------------------------------------------
    def _remove_trailing_semicolons(self, code: str) -> str:
        """移除每行末尾的分号（Mermaid 不支持行末分号作为分隔符）。

        仅移除行末的 `;`（可含尾部空白），不影响标签内容中的分号。
        示例：
          A --> B;           →   A --> B
          A[label; text]     →   不变（分号在方括号内部）
        """
        if ";" not in code:
            return code
        return re.sub(r";\s*$", "", code, flags=re.MULTILINE)

    # ------------------------------------------------------------------
    # Step 4: 规范化节点 ID
    # ------------------------------------------------------------------
    def _normalize_node_ids(self, code: str) -> str:
        """把非 ASCII 节点 ID 替换为 N1/N2/... 形式的合法 ID。

        Mermaid 节点 ID 必须匹配 `^[A-Za-z][A-Za-z0-9_]*$`：
        字母开头，后续仅允许字母/数字/下划线。
        中文 ID（如 `开工[...]`、`节点1[...]`）会导致解析失败。

        检测位置（ID 只在这些位置合法）：
          - 行首（多行模式，允许前导空白）
          - `-->` 箭头之后（链式定义时下一段）

        替换策略：使用回调式重构，只在 ID 位置替换，避免误伤
        标签内容中同名文本（例如 `A[开工]` 中的 `开工` 是标签不是 ID）。
        """
        if not code:
            return code

        # 定位所有 ID 出现位置：行首 或 `-->` 之后（允许前导空白）
        # 捕获 anchor 以便替换时保留完整上下文
        id_anchor_pattern = re.compile(
            r"((?:^|-->))(\s*)(\w+)(\s*)([\[\(\{])",
            re.MULTILINE,
        )

        # 收集需要重命名的 ID（保序、去重）
        id_map: dict[str, str] = {}
        for m in id_anchor_pattern.finditer(code):
            old_id = m.group(3)
            if old_id in id_map:
                continue
            # 合法 ID：字母开头 + 字母/数字/下划线
            if re.match(r"^[A-Za-z][A-Za-z0-9_]*$", old_id):
                continue
            id_map[old_id] = f"N{len(id_map) + 1}"

        if not id_map:
            return code

        # 使用 sub + 回调：只在 ID 位置替换，保留 anchor/空白/括号上下文
        def _replacer(m: re.Match[str]) -> str:
            old_id = m.group(3)
            new_id = id_map.get(old_id)
            if not new_id:
                return m.group(0)
            anchor = m.group(1)   # `""`（行首）或 `"-->"`
            prefix = m.group(2)   # 前导空白
            suffix = m.group(4)   # ID 与括号之间的空白
            bracket = m.group(5)  # 括号本身
            return f"{anchor}{prefix}{new_id}{suffix}{bracket}"

        return id_anchor_pattern.sub(_replacer, code)



    # ------------------------------------------------------------------
    # 诊断接口
    # ------------------------------------------------------------------
    @staticmethod
    def needs_sanitization(code: str) -> bool:
        """快速判定代码是否需要清洗（避免对已合规代码做无用功）。

        仅用于性能优化——调用方可以据此决定是否调用 sanitize。
        检测项：
          - 单行含多个 `-->`（多连写）
          - 行末分号
          - 未加引号但含特殊字符/中文的节点标签
          - 非 ASCII 节点 ID
        """
        if not code or not isinstance(code, str):
            return False

        # 多连写
        for line in code.split("\n"):
            if line.count("-->") > 1:
                return True

        # legacy 边文本：`A -- 说明 --> B`（应改写为 `A -->|说明| B`）
        for line in code.split("\n"):
            if _LEGACY_EDGE_TEXT_RE.match(line):
                return True

        # 行末分号
        if re.search(r";\s*$", code, re.MULTILINE):
            return True

        # 未加引号但含特殊字符的标签
        for m in _NODE_LABEL_PATTERN.finditer(code):
            label = m.group(2)
            if not (label.startswith('"') or label.startswith("'")):
                if _SPECIAL_LABEL_CHARS.search(label) or _CHINESE_CHARS.search(label):
                    return True

        # 非 ASCII 节点 ID（含 --> 后的 ID）
        id_anchor_pattern = re.compile(
            r"((?:^|-->))(\s*)(\w+)(\s*)([\[\(\{])",
            re.MULTILINE,
        )
        for m in id_anchor_pattern.finditer(code):
            old_id = m.group(3)
            if not re.match(r"^[A-Za-z][A-Za-z0-9_]*$", old_id):
                return True

        # 裸节点边：`文本 --> 文本` 其中某侧无方括号/花括号/圆括号
        for line in code.split("\n"):
            if not _ARROW_SPLIT_RE.search(line):
                continue
            _first = line.strip().split()
            if _first:
                _fw = _first[0].rstrip(":")
                if _fw in _SKIP_LINE_PREFIXES:
                    continue
            parts = _ARROW_SPLIT_RE.split(line)
            for i in range(0, len(parts), 2):  # 仅文本段
                seg = parts[i].strip()
                if not seg:
                    continue
                if "[" in seg or "{" in seg or "(" in seg:
                    continue
                if _EDGE_LABEL_RE.match(seg):
                    continue
                # 合法 ASCII 节点 ID 无需处理；仅非法裸节点（含中文/特殊字符/空格）才需修复
                if re.match(r"^[A-Za-z][A-Za-z0-9_]*$", seg):
                    continue
                return True  # 发现裸节点

        return False



# 模块级单例——渲染管道直接复用，避免重复实例化
SANITIZER = MermaidSanitizer()


def sanitize_mermaid_code(code: str) -> str:
    """模块级便捷入口：使用全局单例清洗 Mermaid 代码。

    等价于 `SANITIZER.sanitize(code)`，供渲染管道一行调用。
    """
    return SANITIZER.sanitize(code)

