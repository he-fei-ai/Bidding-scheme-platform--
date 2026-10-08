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

        return False



# 模块级单例——渲染管道直接复用，避免重复实例化
SANITIZER = MermaidSanitizer()


def sanitize_mermaid_code(code: str) -> str:
    """模块级便捷入口：使用全局单例清洗 Mermaid 代码。

    等价于 `SANITIZER.sanitize(code)`，供渲染管道一行调用。
    """
    return SANITIZER.sanitize(code)

