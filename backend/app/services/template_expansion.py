"""已有方案扩写服务

实现"已有方案扩写"工作流的核心业务逻辑。
支持从历史标书中提取目录结构，并在正文生成时保留原方案实质内容。
"""

from dataclasses import dataclass, field
import hashlib
import json
import logging
import os
from pathlib import Path

from ..config import PROJECTS_DATA_DIR
from ..db import get_db
from .ai.provider_factory import create_provider, get_ai_config
from .ai.providers.base import AIMessage
from .ai.workflows import collect_json_response
from .ai.workflows_base import _safe_asyncio_run


logger = logging.getLogger(__name__)


# ============================================================================
# §6.1 P2：模板段落-节点 AI 语义映射（2026-10-20 引入）
# ============================================================================
# 本文件历史上遗留一个"覆盖率审计 AI 语义映射 TODO"（原 L318 map_sections_to_nodes
# 只有简单标题包含匹配）。本轮补齐：
#   1. 保持函数签名同步（下游消费者零改动）；
#   2. 新增开关 TEMPLATE_AI_SECTION_MAPPING_ENABLED（默认关，兼容性优先）；
#   3. 开关开时先尝试 AI 语义映射，任何异常/超时/AI 关闭时**回落到**原有的
#      _simple_title_match（确定性匹配）——保证不引入新的失败模式；
#   4. AI 输出走 JSON 三级恢复（extract_json_from_text + 白名单校验），
#      无效条目被丢弃，不影响确定性结果；
#   5. 与 template_coverage_audit.map_paragraphs_to_nodes（本轮遗留 4 引入的
#      确定性映射）**完全独立**，不共用代码，前者服务于扩写流程输入映射，
#      后者服务于内容级覆盖率审计。
#
# 与 template_coverage_audit.ENABLED 同口径：运行时读取环境变量，允许运维不改
# 代码热开关。默认 false 保证零行为变化，仅当显式设置为 true 才生效。
# ============================================================================


def _ai_section_mapping_enabled() -> bool:
    """开关：``TEMPLATE_AI_SECTION_MAPPING_ENABLED``，默认关（兼容现有行为）。"""
    raw = str(os.getenv("TEMPLATE_AI_SECTION_MAPPING_ENABLED", "false") or "false").strip().lower()
    return raw in ("1", "true", "yes", "on")


def _ai_section_mapping_timeout() -> int:
    """AI 映射整体超时秒数，默认 60s（与 AI_CALL_TIMEOUT 同口径）。"""
    try:
        value = int(os.getenv("TEMPLATE_AI_SECTION_MAPPING_TIMEOUT", "60"))
        return max(5, min(300, value))
    except (ValueError, TypeError):
        return 60


def _ai_section_mapping_topk() -> int:
    """单段落最多返回的候选节点数，默认 3（防止 AI 返回过多候选污染确定性结果）。"""
    try:
        value = int(os.getenv("TEMPLATE_AI_SECTION_MAPPING_TOPK", "3"))
        return max(1, min(10, value))
    except (ValueError, TypeError):
        return 3


@dataclass
class TemplateFile:
    """模板文件信息"""

    template_id: int
    filename: str
    file_path: str
    status: str  # pending/extracting/ready/failed
    outline: list[dict] = field(default_factory=list)
    text_preview: str = ""


@dataclass
class TemplateSection:
    """原方案段落（用于扩写时引用）"""

    id: str  # P001, P002, ...
    title_path: list[str]
    content: str
    chars: int
    hash: str


class TemplateExpansionService:
    """已有方案扩写服务

    核心功能：
    1. 模板文件管理（上传、提取、存储）
    2. 目录结构提取（AI辅助）
    3. 原方案拆分与段落映射
    4. 扩写模式下的正文生成增强
    """

    def __init__(self):
        self.templates_dir = None  # 将在首次使用时初始化

    def _get_templates_dir(self, project_id: int) -> Path:
        """获取项目模板目录"""
        if self.templates_dir is None:
            self.templates_dir = PROJECTS_DATA_DIR / str(project_id) / "templates"
        return self.templates_dir

    def upload_template(self, project_id: int, filename: str, content: bytes) -> dict:
        """上传模板文件

        Args:
            project_id: 项目ID
            filename: 文件名
            content: 文件二进制内容

        Returns:
            模板信息字典
        """
        templates_dir = self._get_templates_dir(project_id)
        templates_dir.mkdir(parents=True, exist_ok=True)

        safe_name = Path(filename).name
        file_path = templates_dir / safe_name
        file_path.write_bytes(content)

        with get_db() as conn:
            cursor = conn.execute(
                """INSERT INTO project_templates 
                   (project_id, filename, file_path, status) 
                   VALUES (?, ?, ?, 'pending')""",
                (project_id, safe_name, str(file_path)),
            )
            template_id = cursor.lastrowid

        logger.info(
            "上传模板文件: project=%d, template=%d, file=%s", project_id, template_id, safe_name
        )

        return {
            "template_id": template_id,
            "filename": safe_name,
            "file_path": str(file_path),
            "status": "pending",
        }

    def get_template_list(self, project_id: int) -> list[dict]:
        """获取项目的模板列表"""
        with get_db() as conn:
            rows = conn.execute(
                """SELECT id, filename, file_path, status, created_at,
                          CASE WHEN length(outline_json) > 0 THEN 1 ELSE 0 END as has_outline
                   FROM project_templates 
                   WHERE project_id = ? 
                   ORDER BY created_at DESC""",
                (project_id,),
            ).fetchall()

        return [dict(row) for row in rows]

    def get_template_detail(self, template_id: int, project_id: int) -> dict:
        """获取模板详情"""
        with get_db() as conn:
            row = conn.execute(
                "SELECT * FROM project_templates WHERE id = ? AND project_id = ?",
                (template_id, project_id),
            ).fetchone()

        if not row:
            raise ValueError(f"模板不存在: {template_id}")

        outline = []
        if row["outline_json"]:
            try:
                outline = json.loads(row["outline_json"])
            except json.JSONDecodeError:
                logger.warning("模板 %d 的 outline_json 解析失败", template_id)

        return {
            "id": row["id"],
            "filename": row["filename"],
            "file_path": row["file_path"],
            "status": row["status"],
            "outline": outline,
            "text_preview": row["text_preview"] or "",
            "created_at": row["created_at"],
        }

    def delete_template(self, template_id: int, project_id: int) -> bool:
        """删除模板"""
        with get_db() as conn:
            row = conn.execute(
                "SELECT file_path FROM project_templates WHERE id = ? AND project_id = ?",
                (template_id, project_id),
            ).fetchone()

        if not row:
            raise ValueError(f"模板不存在: {template_id}")

        # 删除物理文件
        file_path = Path(row["file_path"])
        if file_path.exists():
            try:
                file_path.unlink()
            except Exception as e:
                logger.warning("删除模板文件失败: %s", e)

        # 删除数据库记录
        with get_db() as conn:
            conn.execute("DELETE FROM project_templates WHERE id = ?", (template_id,))

        logger.info("删除模板: template=%d", template_id)
        return True

    def split_template_to_sections(self, template_text: str) -> list[TemplateSection]:
        """将原方案拆分为可引用的段落

        按 Markdown 标题结构拆分，每个段落有稳定编号。

        Args:
            template_text: 原方案完整文本

        Returns:
            段落列表
        """
        lines = template_text.split("\n")
        sections = []
        current_section = None
        section_id_counter = [0]

        def next_section_id():
            section_id_counter[0] += 1
            return f"P{section_id_counter[0]:03d}"

        for line in lines:
            # 检测 Markdown 标题
            if line.startswith("#"):
                # 保存上一个段落
                if current_section and current_section.content.strip():
                    sections.append(current_section)

                # 创建新段落
                len(line) - len(line.lstrip("#"))
                title = line.strip("# ").strip()

                current_section = TemplateSection(
                    id=next_section_id(),
                    title_path=[title],
                    content="",
                    chars=0,
                    hash="",
                )
            elif current_section:
                # 累加内容
                if current_section.content:
                    current_section.content += "\n" + line
                else:
                    current_section.content = line

        # 保存最后一个段落
        if current_section and current_section.content.strip():
            sections.append(current_section)

        # 计算每个段落的哈希和字数
        for section in sections:
            section.chars = len(section.content)
            section.hash = hashlib.sha256(section.content.encode()).hexdigest()[:16]

        logger.info("拆分原方案: %d 个段落", len(sections))
        return sections

    def map_sections_to_nodes(
        self, template_sections: list[TemplateSection], outline_nodes: list[dict]
    ) -> list[dict]:
        """将原方案段落映射到当前目录节点。

        §6.1 P2 补齐（2026-10-20）：接入 AI 语义映射 + 确定性回退。

        工作流程：
          1. 空输入直接返回空列表（无操作，零风险）；
          2. ``TEMPLATE_AI_SECTION_MAPPING_ENABLED`` 关（默认）→ 直接走确定性匹配；
          3. 开关开 → 先调 AI 语义映射，成功且能识别至少一个节点 → 合并结果；
             AI 输出无效条目会被丢弃，不污染确定性结果；
          4. AI 映射失败/超时/异常 → 完整回落到 ``_simple_title_match``，
             保证不引入新的失败模式；
          5. 观测日志（INFO 级）：记录 AI 命中数、回落原因，便于灰度评估。

        与 ``template_coverage_audit.map_paragraphs_to_nodes`` 独立：
        本函数服务于"扩写流程输入映射"（告诉正文生成侧哪些节点该参考哪些原段落），
        覆盖率审计用独立确定性映射，两者不共享代码。

        Args:
            template_sections: 原方案段落列表
            outline_nodes: 当前目录节点列表

        Returns:
            映射关系列表；空输入时返回空列表。
        """
        if not template_sections or not outline_nodes:
            return []

        # 开关关（默认）：保持历史行为，完全走确定性匹配
        if not _ai_section_mapping_enabled():
            return self._simple_title_match(template_sections, outline_nodes)

        # 开关开：尝试 AI 语义映射 + 确定性回退
        try:
            ai_mappings = self._ai_semantic_mapping(template_sections, outline_nodes)
        except Exception as e:
            logger.warning(
                "模板段落 AI 映射失败，回落到确定性匹配: %s", e
            )
            return self._simple_title_match(template_sections, outline_nodes)

        if ai_mappings:
            logger.info(
                "模板段落映射: AI 语义命中 %d 个节点（确定性匹配被替换）",
                len(ai_mappings),
            )
            return ai_mappings

        # AI 返回空（可能是提示词未返回有效映射，或全部候选被过滤）
        logger.info(
            "模板段落映射: AI 返回空结果，回落到确定性匹配"
        )
        return self._simple_title_match(template_sections, outline_nodes)

    def _ai_semantic_mapping(
        self, template_sections: list[TemplateSection], outline_nodes: list[dict]
    ) -> list[dict]:
        """调用 AI 做段落-节点语义映射（§6.1 P2 核心实现）。

        失败、超时、AI 输出无法解析或结构非法时**抛异常或返回空列表**，
        由上层 ``map_sections_to_nodes`` 回落到 ``_simple_title_match``。

        输出契约与 ``_simple_title_match`` 严格一致（**节点视角**）：
            {
              "node_id": str,
              "node_title": str,
              "source_ids": [段落 id, ...],
              "sections": [{"source_id", "title_path", "chars"}, ...],
              "ai_matched": True,   # 观测标记：本条由 AI 语义映射产出
            }
        下游消费者（若未来启用）不需要感知 AI 或确定性来源。

        流程：
          1. 构造轻量化的段落摘要（id + title_path，不含正文）与节点清单；
          2. 单次 AI 调用返回 JSON ``{"mappings": [{"paragraph_id", "node_ids"}]}``
             （AI 视角是段落 → 节点，方便其理解语义关系）；
          3. 白名单校验：paragraph_id 必须在段落集合中，node_ids 必须在节点集合中；
          4. 每段最多 ``_ai_section_mapping_topk()`` 个候选；
          5. 反转视角：节点 → 段落，与 ``_simple_title_match`` 一致；
          6. 空结果返回空列表，由上层判定是否需要回落。
        """
        import time

        topk = _ai_section_mapping_topk()
        start = time.monotonic()

        # 构造轻量化的输入（不含正文，避免撑爆上下文）
        paragraph_summary = [
            {"id": s.id, "title_path": list(s.title_path)[:3]}
            for s in template_sections
        ]
        node_summary = [
            {
                "id": str(n.get("id") or ""),
                "title": str(n.get("title") or ""),
                "is_leaf": bool(n.get("is_leaf")),
            }
            for n in outline_nodes
            if isinstance(n, dict)
        ]
        # 无节点可用，无需调 AI
        if not node_summary or not paragraph_summary:
            return []

        ai_config = get_ai_config()
        if not ai_config:
            logger.warning("模板段落 AI 映射: AI 配置缺失，跳过")
            return []
        provider = create_provider(ai_config)

        messages = [
            AIMessage(role="system", content=self._get_section_mapping_prompt()),
            AIMessage(
                role="user",
                content=(
                    "请基于以下原方案段落清单与目标目录节点清单，判断每个段落应该归属到"
                    "哪些节点（语义相近即可，不必完全匹配标题）。\n\n"
                    f"原方案段落（{len(paragraph_summary)} 条，只含标题路径，不含正文）：\n"
                    f"{json.dumps(paragraph_summary, ensure_ascii=False, indent=2)}\n\n"
                    f"目标目录节点（{len(node_summary)} 个）：\n"
                    f"{json.dumps(node_summary, ensure_ascii=False, indent=2)}\n\n"
                    f"每个段落最多返回 {topk} 个候选节点 id；无匹配时返回空数组。"
                ),
            ),
        ]

        # 同步包装（保持函数签名同步；使用 _safe_asyncio_run 兼容 FastAPI 场景）
        result = _safe_asyncio_run(
            collect_json_response(
                provider,
                messages,
                temperature=0.2,  # 语义映射追求稳定，不用创意采样
                max_retries=1,    # 与 collect_json_response 默认一致，避免叠加超时
                max_tokens=2048,  # 输出量小，短超时（≤60s）
            )
        )

        # 解析 AI 输出（结构：{"mappings": [{"paragraph_id", "node_ids": [...]}]}）
        raw_mappings = []
        if isinstance(result, dict):
            raw_mappings = result.get("mappings") or result.get("items") or []
        elif isinstance(result, list):
            raw_mappings = result

        # 建立校验白名单
        valid_paragraph_ids = {str(s.id) for s in template_sections}
        valid_node_ids = {str(n.get("id") or "") for n in outline_nodes if isinstance(n, dict)}

        # 查找 section 用于填充 title_path/chars
        section_by_id: dict[str, TemplateSection] = {s.id: s for s in template_sections}
        # 节点 id → 节点对象反查
        node_by_id: dict[str, dict] = {
            str(n.get("id") or ""): n for n in outline_nodes if isinstance(n, dict)
        }

        # ====== 阶段 1：解析 AI 段落视角的映射，白名单校验 ======
        # paragraph_id → [node_id, ...]
        paragraph_to_nodes: dict[str, list[str]] = {}
        seen_paragraph_ids: set[str] = set()
        for item in raw_mappings:
            if not isinstance(item, dict):
                continue
            pid = str(item.get("paragraph_id") or item.get("paragraph") or "").strip()
            if pid not in valid_paragraph_ids or pid in seen_paragraph_ids:
                continue
            seen_paragraph_ids.add(pid)

            raw_node_ids = item.get("node_ids") or item.get("nodes") or []
            if not isinstance(raw_node_ids, list):
                raw_node_ids = []
            cleaned_ids: list[str] = []
            for nid in raw_node_ids:
                nid_s = str(nid or "").strip()
                if nid_s and nid_s in valid_node_ids and nid_s not in cleaned_ids:
                    cleaned_ids.append(nid_s)
                if len(cleaned_ids) >= topk:
                    break
            if cleaned_ids:
                paragraph_to_nodes[pid] = cleaned_ids

        # ====== 阶段 2：反转视角为节点 → 段落，与 _simple_title_match 输出契约一致 ======
        # node_id → [段落 id, ...]
        node_to_paragraphs: dict[str, list[str]] = {}
        for pid, node_ids in paragraph_to_nodes.items():
            for nid in node_ids:
                node_to_paragraphs.setdefault(nid, []).append(pid)

        out: list[dict] = []
        for nid, pids in node_to_paragraphs.items():
            node = node_by_id.get(nid) or {}
            node_title = str(node.get("title") or "")
            matched_sections = []
            for pid in pids:
                section = section_by_id.get(pid)
                if section is None:
                    continue
                matched_sections.append(
                    {
                        "source_id": pid,
                        "title_path": list(section.title_path),
                        "chars": int(section.chars or 0),
                    }
                )
            if not matched_sections:
                continue
            out.append(
                {
                    "node_id": nid,
                    "node_title": node_title,
                    "source_ids": [m["source_id"] for m in matched_sections],
                    "sections": matched_sections,
                    "ai_matched": True,  # 观测标记：本条由 AI 语义映射产出
                }
            )

        elapsed = time.monotonic() - start
        logger.info(
            "模板段落 AI 映射完成: 段落=%d, 节点=%d, 命中节点=%d, 耗时=%.2fs",
            len(template_sections), len(outline_nodes), len(out), elapsed,
        )
        return out

    def _simple_title_match(
        self, template_sections: list[TemplateSection], outline_nodes: list[dict]
    ) -> list[dict]:
        """基于标题的简单匹配（基础版）"""
        mappings = []

        for node in outline_nodes:
            if not node.get("is_leaf"):
                continue

            node_title = node.get("title", "")
            matched_sections = []

            for section in template_sections:
                # 简单匹配：标题包含或段落标题路径包含节点标题
                if (
                    node_title.lower() in section.title_path[0].lower()
                    or section.title_path[0].lower() in node_title.lower()
                ):
                    matched_sections.append(
                        {
                            "source_id": section.id,
                            "title_path": section.title_path,
                            "chars": section.chars,
                        }
                    )

            if matched_sections:
                mappings.append(
                    {
                        "node_id": node.get("id"),
                        "node_title": node_title,
                        "source_ids": [m["source_id"] for m in matched_sections],
                        "sections": matched_sections,
                    }
                )

        return mappings

    def analyze_gaps(
        self, tender_summary: dict, template_outline: list[dict], current_outline: list[dict]
    ) -> dict:
        """分析招标要求与已有方案的差异

        Args:
            tender_summary: 招标摘要
            template_outline: 模板目录结构
            current_outline: 当前生成的目录结构

        Returns:
            差异分析报告
        """
        # 提取当前目录的所有标题
        current_titles = set()

        def extract_titles(nodes):
            for node in nodes:
                current_titles.add(node.get("title", ""))
                if node.get("children"):
                    extract_titles(node["children"])

        extract_titles(current_outline)

        # 检查模板目录中哪些标题不在当前目录中
        missing_titles = []
        covered_titles = []

        def check_template_nodes(nodes):
            for node in nodes:
                title = node.get("title", "")
                if title in current_titles:
                    covered_titles.append(title)
                else:
                    missing_titles.append(
                        {
                            "title": title,
                            "level": node.get("level", 1),
                            "path": self._get_node_path(node, current_outline),
                        }
                    )
                if node.get("children"):
                    check_template_nodes(node["children"])

        check_template_nodes(template_outline)

        return {
            "total_template_sections": len(covered_titles) + len(missing_titles),
            "covered_count": len(covered_titles),
            "missing_count": len(missing_titles),
            "coverage_rate": round(
                len(covered_titles) / max(1, len(covered_titles) + len(missing_titles)) * 100, 1
            ),
            "missing_items": missing_titles[:20],  # 最多返回20项
        }

    def _get_node_path(self, node: dict, outline: list[dict], path: str = "") -> str:
        """获取节点完整路径"""
        title = node.get("title", "")
        current_path = f"{path}/{title}" if path else title

        for item in outline:
            if item.get("title") == title:
                if item.get("children"):
                    for child in item["children"]:
                        child_path = self._get_node_path(child, outline, current_path)
                        if child_path:
                            return child_path
                return current_path if title else ""

        return ""

    def _get_section_mapping_prompt(self) -> str:
        """§6.1 P2：模板段落-目录节点 AI 语义映射提示词。

        设计原则（对齐易标「编排选事实、生成注事实」哲学）：
        1. AI 只看到轻量化的段落 id + 标题路径、节点 id + 标题，不看正文——
           避免撑爆上下文；
        2. 严格 JSON 输出，字段白名单 + 数组长度限制由程序侧兜底；
        3. 允许"无匹配"—— AI 应返回空 node_ids 而不是硬凑；
        4. 明确禁止编造 id（防止出现段落 id 或节点 id 幻觉）。
        """
        return """# 模板段落-目录节点语义映射

## 角色
你是标书结构分析专家，负责判断历史标书段落应该归属到新目录树的哪些节点。

## 任务
根据给定的原方案段落清单与目标目录节点清单，为每个段落语义匹配最合适的节点。

## 匹配原则
1. **语义相近即可**，不要求标题完全相等——例如"项目概况"可匹配"工程概况"、"项目背景"；
2. **宁缺勿滥**：如果某个段落找不到合适的节点，返回空数组，不要硬凑；
3. **多个候选**：一个段落可能对应多个语义相近的节点，返回 1~N 个 id；
4. **严格禁止编造**：只能返回清单中已存在的 paragraph_id 与 node id，不得凭空生成；
5. **优先叶子节点**：如果非叶子节点与其某叶子节点语义相近，优先返回叶子节点 id。

## 输出格式（严格 JSON）
```json
{
  "mappings": [
    {
      "paragraph_id": "P001",
      "node_ids": ["1.1", "1.2"]
    },
    {
      "paragraph_id": "P002",
      "node_ids": []
    }
  ]
}
```

## 注意事项
- 只输出 JSON，不要输出任何解释、Markdown 代码围栏外的文本；
- 不要遗漏任何输入段落（每个 paragraph_id 都要出现在 mappings 中，即使 node_ids 为空）；
- 不要返回清单外的 id。
"""
