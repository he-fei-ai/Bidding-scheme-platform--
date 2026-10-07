"""已有方案扩写服务

实现"已有方案扩写"工作流的核心业务逻辑。
支持从历史标书中提取目录结构，并在正文生成时保留原方案实质内容。
"""

from dataclasses import dataclass, field
import hashlib
import json
import logging
from pathlib import Path

from ..config import PROJECTS_DATA_DIR
from ..db import get_db
from .ai.provider_factory import create_provider, get_ai_config
from .ai.providers.base import AIMessage
from .ai.workflows import collect_json_response
from .files import extract_text_from_path


logger = logging.getLogger(__name__)


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

    def extract_template_outline(self, template_id: int, project_id: int) -> dict:
        """AI提取模板目录结构

        Args:
            template_id: 模板ID
            project_id: 项目ID

        Returns:
            提取的目录结构
        """
        with get_db() as conn:
            row = conn.execute(
                "SELECT * FROM project_templates WHERE id = ? AND project_id = ?",
                (template_id, project_id),
            ).fetchone()

        if not row:
            raise ValueError(f"模板不存在: {template_id}")

        file_path = Path(row["file_path"])
        if not file_path.exists():
            raise ValueError("模板文件已不存在")

        # 更新状态为 extracting
        with get_db() as conn:
            conn.execute(
                "UPDATE project_templates SET status = 'extracting' WHERE id = ?",
                (template_id,),
            )

        try:
            # 提取文本
            text = extract_text_from_path(str(file_path))
            if not text.strip():
                raise ValueError("模板文件为空或无法提取文本")

            # 预览前500字符
            text_preview = text[:500]

            # AI提取目录结构
            ai_config = get_ai_config()
            provider = create_provider(ai_config)

            messages = [
                AIMessage(role="system", content=self._get_extract_outline_prompt()),
                AIMessage(
                    role="user",
                    content=f"请从以下历史标书技术方案中提取完整的目录结构：\n\n{text[:8000]}",  # 限制输入长度
                ),
            ]

            result = collect_json_response(provider, messages, temperature=0.3, max_retries=2)

            # 解析结果
            outline = []
            if isinstance(result, dict) and "outline" in result:
                outline = result["outline"]
            elif isinstance(result, list):
                outline = result

            # 更新模板记录
            with get_db() as conn:
                conn.execute(
                    """UPDATE project_templates 
                       SET status = 'ready', outline_json = ?, text_preview = ?
                       WHERE id = ?""",
                    (json.dumps(outline, ensure_ascii=False), text_preview, template_id),
                )

            logger.info("提取模板目录成功: template=%d, sections=%d", template_id, len(outline))

            return {
                "template_id": template_id,
                "status": "ready",
                "outline": outline,
                "text_preview": text_preview,
            }

        except Exception as e:
            logger.error("提取模板目录失败: %s", e, exc_info=True)

            with get_db() as conn:
                conn.execute(
                    "UPDATE project_templates SET status = 'failed', message = ? WHERE id = ?",
                    (str(e), template_id),
                )

            raise ValueError(f"提取失败: {e}") from e

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
        """将原方案段落映射到当前目录节点

        使用AI辅助的智能映射算法，提高匹配准确率。

        Args:
            template_sections: 原方案段落列表
            outline_nodes: 当前目录节点列表

        Returns:
            映射关系列表
        """
        # TODO: 实现AI辅助的段落-节点映射
        # 这里先用简单的标题匹配，后续升级为AI语义匹配
        return self._simple_title_match(template_sections, outline_nodes)

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

    def _get_extract_outline_prompt(self) -> str:
        """获取提取目录结构的提示词"""
        return """# 已有方案目录结构提取

## 角色
你是标书结构分析专家，从历史标书文本中提取目录结构。

## 任务
提取文档中的完整目录结构，输出为三级嵌套 JSON 格式。

## 规则
1. 识别文档中的标题层级（如"第一节"、"一、"、"1、"、"1.1"等）
2. 一级标题对应 level=1（如"第一节 项目概述"）
3. 二级标题对应 level=2（如"一、项目概况"）
4. 三级标题对应 level=3（如"1、项目概况"）
5. 只提取目录结构，不提取正文内容

## 输出格式
```json
{
  "outline": [
    {
      "id": "1",
      "title": "项目概述",
      "level": 1,
      "children": [
        {
          "id": "1.1",
          "title": "项目概况",
          "level": 2,
          "children": []
        }
      ]
    }
  ]
}
```

## 注意事项
- 确保目录结构完整，不要遗漏任何章节
- 保持原有的层级关系
- 只输出JSON，不要输出其他内容"""
