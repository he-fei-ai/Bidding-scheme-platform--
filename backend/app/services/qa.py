"""智能问答服务层

基于当前项目的招标文件文本与已生成章节内容构建上下文，
调用 AI 大模型回答用户问题，并返回答案与信息来源。

核心能力：
- 上下文构建：项目信息 + 招标文件 + 章节正文（按字数预算裁剪）
- 多轮对话：携带历史消息，支持追问与上下文延续
- 引用溯源：记录每个章节作为来源，回答中给出定位
- 来源安全：回答同时返回被引用的章节标题，供前端展示
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from ..db import get_db
from .ai.providers.base import AIMessage


logger = logging.getLogger(__name__)


# 上下文预算（字符），避免超出模型上下文窗口
# 招标文件文本与章节正文共用该预算
CONTEXT_CHAR_BUDGET = 12000
# 单个章节内容的最大截取长度
MAX_SECTION_CHARS = 600
# 历史消息条数上限（用户+助手消息总对数）
MAX_HISTORY_TURNS = 6
# 每项目历史记录上限（内存存储）
MAX_HISTORY_PER_PROJECT = 20


# ========== 历史记录（内存存储，进程级） ==========
# 结构: {project_id: [{"role": "user"/"assistant", "content": str}, ...]}
_qa_history: dict[int, list[dict]] = {}
_qa_lock = threading.Lock()


def get_history(project_id: int) -> list[dict]:
    """获取指定项目的问答历史"""
    with _qa_lock:
        return list(_qa_history.get(project_id, []))


def clear_history(project_id: int) -> None:
    """清空指定项目的问答历史"""
    with _qa_lock:
        _qa_history.pop(project_id, None)


def _append_history(project_id: int, role: str, content: str) -> None:
    with _qa_lock:
        hist = _qa_history.setdefault(project_id, [])
        hist.append({"role": role, "content": content})
        # 裁剪到上限，保留最近记录
        if len(hist) > MAX_HISTORY_PER_PROJECT:
            del hist[: len(hist) - MAX_HISTORY_PER_PROJECT]


# ========== 上下文构建 ==========


def _get_project_info(project_id: int) -> dict:
    """获取项目基本信息"""
    try:
        with get_db() as conn:
            row = conn.execute(
                "SELECT id, name, industry, bidder_name, notes FROM projects WHERE id = ?",
                (project_id,),
            ).fetchone()
            return dict(row) if row else {}
    except Exception as e:
        logger.warning("获取项目信息失败: %s", e)
        return {}


def _build_sections_context(project_id: int) -> tuple[str, list[str]]:
    """构建章节正文上下文

    Returns:
        (context_text, sources): 章节正文拼接文本 + 来源章节标题列表
    """
    from .sections import list_sections

    try:
        sections = list_sections(project_id)
    except Exception as e:
        logger.warning("加载章节失败: %s", e)
        return "", []

    lines: list[str] = []
    sources: list[str] = []
    budget_used = 0

    for s in sections:
        title = (s.title or "").strip()
        content = (s.content or "").strip()
        if not title or not content:
            continue
        text = content[:MAX_SECTION_CHARS]
        # 剪裁到预算内
        if budget_used + len(text) > CONTEXT_CHAR_BUDGET:
            remain = CONTEXT_CHAR_BUDGET - budget_used
            if remain <= 200:
                break
            text = text[:remain]
        lines.append(f"【{title}】\n{text}")
        sources.append(title)
        budget_used += len(text)
        if budget_used >= CONTEXT_CHAR_BUDGET:
            break

    return "\n\n".join(lines), sources


async def _get_tender_text(project_id: int) -> str:
    """获取招标文件提取文本"""
    try:
        from .files import latest_extracted_text

        text = await latest_extracted_text(project_id)
        if not text:
            return ""
        # 剪裁到上下文预算（与章节共用预算，各占一半）
        half = CONTEXT_CHAR_BUDGET // 2
        return text[:half]
    except Exception as e:
        logger.warning("获取招标文件文本失败: %s", e)
        return ""


async def build_context(project_id: int) -> dict[str, Any]:
    """构建问答上下文

    Returns:
        {
            "project": 项目信息,
            "tender_text": 招标文件文本（可能为空）,
            "sections_text": 章节正文拼接,
            "sources": 来源章节标题列表,
        }
    """
    project = _get_project_info(project_id)
    tender_text = await _get_tender_text(project_id)
    sections_text, sources = _build_sections_context(project_id)
    return {
        "project": project,
        "tender_text": tender_text,
        "sections_text": sections_text,
        "sources": sources,
    }


# ========== 问答主逻辑 ==========


async def qa_chat(
    project_id: int, question: str, history: list[dict] | None = None
) -> dict[str, Any]:
    """执行一次问答

    Args:
        project_id: 项目ID
        question: 用户问题
        history: 可选的历史消息（仅用于本轮构造，最终以服务端存储为准）

    Returns:
        {
            "answer": AI 回答,
            "sources": 来源章节标题列表,
            "project_name": 项目名称,
        }
    """
    question = (question or "").strip()
    if not question:
        raise ValueError("问题不能为空")

    # 构建上下文
    ctx = await build_context(project_id)
    project_name = ctx["project"].get("name", f"项目{project_id}")

    # 构造系统提示词
    system_prompt = _build_system_prompt(project_name, ctx)

    # 消息序列：系统 + 历史 + 当前问题
    messages = [AIMessage(role="system", content=system_prompt)]

    # 客户端传来的历史（去重，避免与存储重复；仅取最近若干轮）
    recent_history = (history or [])[-MAX_HISTORY_TURNS * 2 :]
    for msg in recent_history:
        role = msg.get("role", "")
        content = str(msg.get("content", "")).strip()
        if role in ("user", "assistant") and content:
            messages.append(AIMessage(role=role, content=content))

    messages.append(AIMessage(role="user", content=question))

    # 调用 AI
    answer = await _call_ai(messages)

    # 记录历史
    _append_history(project_id, "user", question)
    _append_history(project_id, "assistant", answer)

    return {
        "answer": answer,
        "sources": ctx["sources"],
        "project_name": project_name,
    }


def _build_system_prompt(project_name: str, ctx: dict[str, Any]) -> str:
    """构造问答系统提示词"""
    parts = [
        "你是一名资深的招投标方案编写专家。请基于以下提供的项目信息、招标文件与已生成章节内容，"
        "回答用户关于本项目的问题。",
        f"【项目名称】{project_name}",
    ]

    if ctx.get("tender_text"):
        parts.append("【招标文件要点】\n" + ctx["tender_text"])

    if ctx.get("sections_text"):
        parts.append("【已生成章节内容】\n" + ctx["sections_text"])

    parts.append(
        "回答要求：\n"
        "1. 优先基于上述上下文作答，引用具体章节或招标条款时说明来源；\n"
        "2. 上下文未覆盖的内容，结合招投标专业经验补充，但需明确说明是建议；\n"
        "3. 回答使用中文，结构清晰，可直接用于标书编写参考。"
    )
    return "\n\n".join(parts)


async def _call_ai(messages: list[AIMessage]) -> str:
    """调用 AI Provider 获取回答"""
    from .ai.provider_factory import create_provider

    provider = create_provider()
    raw = await provider.chat(
        messages,
        temperature=0.4,
        max_tokens=2048,
        max_retries=2,
    )
    answer = (raw or "").strip()
    if not answer:
        raise RuntimeError("AI 返回为空，请稍后重试")
    return answer


def format_sources(sources: list[str], max_items: int = 8) -> list[str]:
    """格式化来源列表（去重 + 截断）"""
    seen: set[str] = set()
    result: list[str] = []
    for s in sources:
        s = (s or "").strip()
        if s and s not in seen:
            seen.add(s)
            result.append(s)
        if len(result) >= max_items:
            break
    return result
