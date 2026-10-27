"""prompt_order.py — 提示词顺序优化（缓存命中）通用消息装配器。

外部引用标准（冲突时以其工作机制/工作流程/标准为准）：
《标书智能体（4）——提示词顺序优化，让缓存命中，输入成本直降10倍》
（FB208/yibiao-simple，2026-10-23 引入本平台）。

核心结论：节约成本最好的办法不是精简提示词，而是调整提示词顺序——服务商的
Prompt Cache 看的是**请求前缀**，只有「每条消息的内容与顺序都稳定」，第二次
请求才能命中同一前缀。参考软件实测（OpenRouter + gemini-2.5-flash）：第二次
请求 cached_tokens=19442，输入成本从 0.0058 降到 0.00067，约为第一次的 1/9。

四条核心原则：
  1. system 只放稳定规则：通用角色、通用写作规范、通用输出要求；
     不放本次请求特有的差异（当前章节 ID、字数标准、用户额外要求…）。
  2. 最大、最稳定的上下文尽量前置：项目概述、招标文件全文、目录树、
     上级章节链、九大章节标准、适用规范清单、全局事实标题清单…
  3. 任务差异尽量放最后：提取项目概述 / 生成某章正文 / 审核某章节…
  4. 同一份数据的组织格式保持稳定：标题写法一致、换行数量一致、列表
     顺序一致、strip 规则一致；不把随机信息、时间戳、request_id 塞进
     共享上下文。

本模块提供与业务无关的装配原语。各高频 AI 任务（解析提取 / 目录生成 /
全局事实提取 / 正文生成 / 正文编排 / 图表生成 / 审核预检）统一经此处装配
消息，使「稳定前缀一致、任务差异后置」成为可测试的结构性保证，而不是依赖
各调用方自觉。消息内容本身不增删，只改组织顺序与格式稳定化。
"""

from __future__ import annotations

from collections.abc import Iterable
import hashlib
from typing import Any

from .prompt_layering import normalize_readable_text
from .providers.base import AIMessage


def normalize_stable_text(text: str | None) -> str:
    """格式稳定化（原则 4）：统一换行符、去行尾空白、去首尾空白。

    与 prompt_layering.normalize_readable_text 同一口径：同一份数据每次
    渲染结果字节级一致，避免「看起来差不多但字节不同」导致缓存前缀失效。
    """
    return normalize_readable_text(text)


def _clean_blocks(context_blocks: Iterable[str] | None) -> list[str]:
    """过滤空块（空串/纯空白不产生消息，避免无意义的消息边界）。"""
    out: list[str] = []
    for blk in context_blocks or ():
        if blk is None:
            continue
        text = blk if isinstance(blk, str) else str(blk)
        if text.strip():
            out.append(text)
    return out


def build_cache_friendly_messages(
    system_rules: str = "",
    context_blocks: Iterable[str] | None = None,
    task_block: str = "",
    *,
    normalize: bool = True,
) -> list[AIMessage]:
    """按四条原则装配 AIMessage 列表（稳定前置、任务差异后置）。

    结构：
        [0] system         稳定规则（跨请求完全一致 → 缓存命中区间起点）
        [1..n-1] user      稳定上下文，按「稳定度降序」排列（调用方负责排序），
                           每条独立成消息，跨请求一致 → 缓存命中区间
        [n] user           任务差异（唯一随请求变化的部分，放最后）

    Args:
        system_rules: 稳定规则（通用角色/写作规范/输出要求）。为空且无其它
            消息时返回单条空 system（部分 provider 要求首条为 system）。
        context_blocks: 稳定上下文块列表，顺序即消息顺序（最稳最长的放前面）。
        task_block: 任务差异块；为空时不产生最后一条消息。
        normalize: True 时对每条消息做格式稳定化（原则 4，默认开）。
    """
    messages: list[AIMessage] = []
    if system_rules and str(system_rules).strip():
        content = normalize_stable_text(system_rules) if normalize else system_rules
        messages.append(AIMessage(role="system", content=content))
    for blk in _clean_blocks(context_blocks):
        content = normalize_stable_text(blk) if normalize else blk
        messages.append(AIMessage(role="user", content=content))
    if task_block and str(task_block).strip():
        content = normalize_stable_text(task_block) if normalize else task_block
        messages.append(AIMessage(role="user", content=content))
    if not messages:
        # 兜底：至少给一条 system，满足 provider 的最小消息数要求
        messages.append(AIMessage(role="system", content=""))
    elif messages[0].role != "system":
        messages.insert(0, AIMessage(role="system", content="You are a helpful assistant."))
    return messages


def build_cache_friendly_dict_messages(
    system_rules: str = "",
    context_blocks: Iterable[str] | None = None,
    task_block: str = "",
    *,
    normalize: bool = True,
) -> list[dict[str, str]]:
    """同 build_cache_friendly_messages，但返回原始 dict（OpenAI SDK 形态）。"""
    return [
        {"role": m.role, "content": m.content}
        for m in build_cache_friendly_messages(
            system_rules, context_blocks, task_block, normalize=normalize
        )
    ]


def stable_prefix_text(messages: list, *, exclude_last: bool = True) -> str:
    """稳定前缀文本：除任务差异消息（最后一条 user）外的全部消息内容拼接。

    两次请求该值一致 ⇒ 服务商可见的请求前缀逐字节一致 ⇒ 可命中同一缓存。
    供测试与排障使用（正文/编排等同父章节多叶子的前缀一致性断言）。
    """
    parts: list[str] = []
    seq = messages[:-1] if (exclude_last and len(messages) > 1) else messages
    for m in seq:
        content = getattr(m, "content", None)
        if content is None and isinstance(m, dict):
            content = m.get("content", "")
        parts.append(str(content or ""))
    return "\n".join(parts)


def stable_prefix_fingerprint(messages: list, *, exclude_last: bool = True) -> str:
    """稳定前缀指纹（sha1 短 hex），用于日志/监控比对，避免落盘全文。"""
    return hashlib.sha1(
        stable_prefix_text(messages, exclude_last=exclude_last).encode("utf-8")
    ).hexdigest()[:12]


def split_rendered_template(rendered: str, marker: str) -> tuple[str, str]:
    """把渲染后的模板按 marker 切成（稳定前段, 稳定后段）。

    用于 DB 可编辑模板（占位符位于模板中段）的缓存友好拆分：把易变占位符
    （当前章节/待审核内容）从稳定模板文本中摘出，使稳定段可独立成消息、
    跨请求复用前缀。marker 不存在时返回 (rendered, "")（整体视为稳定段）。
    """
    text = rendered or ""
    if not marker or marker not in text:
        return text, ""
    before, _, after = text.partition(marker)
    return before, after


def merge_single_user(blocks: Iterable[str]) -> str:
    """把多块合并为单条 user 消息（兼容开关关闭时的旧行为）。

    与分层装配的内容完全一致，仅消息条数不同；分隔符统一为单个换行，
    不引入随机空白。
    """
    return "\n".join(_clean_blocks(blocks))


def task_message_label(task_block: str, *, max_len: int = 40) -> str:
    """任务差异消息的短标签（日志用，避免把整段任务文本写进日志）。"""
    text = (task_block or "").strip().replace("\n", " ")
    return text[:max_len]


def describe_message_stack(messages: list) -> dict[str, Any]:
    """消息栈结构摘要（条数/各消息角色与长度），供调试日志与测试断言。"""
    return {
        "count": len(messages),
        "roles": [getattr(m, "role", "?") for m in messages],
        "lengths": [len(str(getattr(m, "content", "") or "")) for m in messages],
    }
