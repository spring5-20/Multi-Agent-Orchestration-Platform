"""短期记忆窗口管理：控制注入大模型的对话历史长度。

短期记忆（``AgentState.messages``）会跨请求无限累积，若不约束会撑爆上下文
窗口并推高 token 成本。本模块通过 ``WindowStrategy`` 协议解耦，支持四种策略，
由 ``ECOMMERCE_WINDOW_STRATEGY`` 配置切换：

- ``truncate``：只保留最近 ``ECOMMERCE_WINDOW_MAX_MESSAGES`` 条消息，最旧的消息直接丢弃。
- ``token``：按 token 预算 ``ECOMMERCE_WINDOW_MAX_TOKENS`` 从尾部向前保留，超出预算的消息丢弃。
- ``summary``：保留最近 ``ECOMMERCE_WINDOW_SUMMARY_KEEP`` 条消息，被挤出的旧消息交给大模型
  总结，摘要写入长期画像 ``UserProfile.history_summary``，随后删除原消息。
- ``hybrid``：生产级组合策略——工具输出截断 + 旧消息摘要 + 最近几轮原文 + token 预算兜底。

四种策略统一提供两个动作：

- ``trim``：喂给大模型前裁剪（纯读取，无副作用），在 ``answer`` 节点使用。
- ``compact``：对话结束后压缩（可能总结并返回待删除消息），在 ``memory_write`` 节点使用，
  返回值经 ``add_messages`` reducer 真正从短期记忆中移除旧消息。
"""

import logging
from typing import Protocol, runtime_checkable

from langchain_core.messages import BaseMessage, RemoveMessage
from langchain_openai import ChatOpenAI
from pydantic import SecretStr

from ...config import settings
from .memory.base import UserProfile

logger = logging.getLogger(__name__)

_ROLE_LABELS = {"human": "用户", "ai": "助手", "tool": "工具", "system": "系统"}

_SUMMARY_PROMPT = """你是对话摘要器，把历史对话压缩成简短摘要，供后续对话复用。

要求：
1. 只保留对后续对话有用的关键信息：用户身份、需求、偏好、预算、讨论或购买过的商品等。
2. 摘要精炼，控制在 200 字以内。
3. 直接输出摘要正文，不要输出任何其他说明或格式标记。

{history}"""


def _message_text(message: BaseMessage) -> str:
    """提取消息的纯文本内容，兼容字符串与内容块列表两种形式。"""
    content = message.content
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                text = block.get("text")
                parts.append(str(text) if isinstance(text, str) else str(text or ""))
            else:
                parts.append(str(block))
        return "".join(parts)
    return str(content or "")


def _role_label(message: BaseMessage) -> str:
    """把消息类型映射为中文角色名，用于摘要输入。"""
    return _ROLE_LABELS.get(getattr(message, "type", ""), "消息")


def _is_cjk(char: str) -> bool:
    """判断字符是否属于中日韩统一表意文字（汉字）。"""
    code = ord(char)
    return 0x3400 <= code <= 0x4DBF or 0x4E00 <= code <= 0x9FFF


def _estimate_tokens(message: BaseMessage) -> int:
    """估算单条消息的 token 数（含少量固定开销），用于 token 预算裁剪。

    对话模型为 DeepSeek：中文约 1 字 1 token、英文约 4 字符 1 token。
    按该比例做字符启发式估算，比 GPT 的 ``cl100k_base`` 编码器更贴合实际模型；
    估算值略偏大（保守），确保 token 预算裁剪不会超出模型上下文窗口。
    """
    text = _message_text(message)
    if not text:
        return 0
    cjk = sum(1 for char in text if _is_cjk(char))
    other = len(text) - cjk
    return cjk + (other + 3) // 4 + 4


def _slice_by_tokens(messages: list[BaseMessage], max_tokens: int) -> list[BaseMessage]:
    """从最新消息向前保留，累计 token 不超过预算（至少保留一条）。"""
    kept: list[BaseMessage] = []
    used = 0
    for message in reversed(messages):
        cost = _estimate_tokens(message)
        if kept and used + cost > max_tokens:
            break
        kept.append(message)
        used += cost
    kept.reverse()
    return kept


def _truncate_tool_outputs(messages: list[BaseMessage], max_chars: int) -> list[BaseMessage]:
    """截断工具输出消息的文本，避免大段工具结果撑爆上下文。"""
    if max_chars <= 0:
        return messages
    result: list[BaseMessage] = []
    for message in messages:
        if getattr(message, "type", "") != "tool":
            result.append(message)
            continue
        text = _message_text(message)
        if len(text) <= max_chars:
            result.append(message)
            continue
        truncated = text[:max_chars] + f"…（工具输出过长，已截断，原 {len(text)} 字符）"
        result.append(message.model_copy(update={"content": truncated}))
    return result


def _remove_messages(messages: list[BaseMessage]) -> list[RemoveMessage]:
    """把待移除消息转为 ``RemoveMessage``，跳过缺少 id 的消息。"""
    return [RemoveMessage(id=message.id) for message in messages if message.id is not None]


@runtime_checkable
class WindowStrategy(Protocol):
    """短期记忆窗口管理策略的统一接口。"""

    def trim(self, messages: list[BaseMessage]) -> list[BaseMessage]:
        """裁剪：返回应注入大模型的消息序列，不产生副作用。"""
        ...

    async def compact(self, messages: list[BaseMessage], user_id: int) -> list[RemoveMessage]:
        """压缩：对话结束后处理超窗旧消息，返回需从短期记忆删除的消息。"""
        ...


class TruncateWindowStrategy:
    """截断策略：只保留最近 ``max_messages`` 条消息，其余直接丢弃。"""

    def __init__(self, max_messages: int) -> None:
        self.max_messages = max_messages

    def trim(self, messages: list[BaseMessage]) -> list[BaseMessage]:
        if len(messages) <= self.max_messages:
            return messages
        return messages[-self.max_messages :]

    async def compact(self, messages: list[BaseMessage], user_id: int) -> list[RemoveMessage]:
        if len(messages) <= self.max_messages:
            return []
        return _remove_messages(messages[: -self.max_messages])


class TokenBudgetWindowStrategy:
    """token 预算策略：从尾部向前保留，直到 token 预算耗尽。"""

    def __init__(self, max_tokens: int) -> None:
        self.max_tokens = max_tokens

    def trim(self, messages: list[BaseMessage]) -> list[BaseMessage]:
        return _slice_by_tokens(messages, self.max_tokens)

    async def compact(self, messages: list[BaseMessage], user_id: int) -> list[RemoveMessage]:
        kept = _slice_by_tokens(messages, self.max_tokens)
        if len(kept) == len(messages):
            return []
        kept_ids = {message.id for message in kept}
        return _remove_messages([message for message in messages if message.id not in kept_ids])


def _build_summary_llm() -> ChatOpenAI:
    """创建用于窗口摘要的大模型客户端（温度 0，追求稳定输出）。"""
    if not settings.DEEPSEEK_API_KEY or not settings.DEEPSEEK_MODEL or not settings.DEEPSEEK_BASE_URL:
        raise RuntimeError("DeepSeek 配置不完整，无法执行窗口摘要")
    return ChatOpenAI(
        model=settings.DEEPSEEK_MODEL,
        api_key=SecretStr(settings.DEEPSEEK_API_KEY),
        base_url=settings.DEEPSEEK_BASE_URL,
        temperature=0,
        timeout=60,
    )


class SummaryWindowStrategy:
    """总结压缩策略：旧消息总结进长期画像 ``history_summary`` 后删除。"""

    def __init__(self, keep_messages: int, hard_limit: int, tool_max_chars: int = 0) -> None:
        self.keep_messages = keep_messages
        self.hard_limit = max(hard_limit, keep_messages + 1)
        self.tool_max_chars = tool_max_chars

    def trim(self, messages: list[BaseMessage]) -> list[BaseMessage]:
        messages = _truncate_tool_outputs(messages, self.tool_max_chars)
        if len(messages) <= self.keep_messages:
            return messages
        return messages[-self.keep_messages :]

    async def compact(self, messages: list[BaseMessage], user_id: int) -> list[RemoveMessage]:
        if len(messages) <= self.keep_messages:
            return []
        overflow = messages[: -self.keep_messages]

        # 惰性导入避免 window 与 runtime 之间的循环依赖
        from .runtime import get_memory_store

        store = get_memory_store()
        summarized = False
        try:
            profile = await store.get_profile(user_id)
            summary = await self._summarize(overflow, profile.history_summary if profile else None)
            if summary:
                profile = profile or UserProfile(user_id=user_id)
                profile.history_summary = summary
                await store.upsert_profile(profile, touch=False)
                summarized = True
        except Exception as exc:  # noqa: BLE001
            logger.warning("窗口摘要生成或写入失败: %s", exc)

        if not summarized and len(messages) <= self.hard_limit:
            # 总结失败但未达硬上限：保留旧消息待下一轮重试，避免信息永久丢失
            logger.warning("总结失败且未达硬上限，本轮保留旧消息待重试（%d/%d）", len(messages), self.hard_limit)
            return []

        return _remove_messages(overflow)

    async def _summarize(self, overflow: list[BaseMessage], previous: str | None) -> str:
        """把旧消息与既有摘要一起压缩成新的滚动摘要。"""
        overflow = _truncate_tool_outputs(overflow, self.tool_max_chars)
        llm = _build_summary_llm()
        parts: list[str] = []
        if previous:
            parts.append(f"此前对话摘要：{previous}")
        parts.append("新增历史对话：")
        for message in overflow:
            text = _message_text(message)
            if text:
                parts.append(f"{_role_label(message)}: {text}")
        prompt = _SUMMARY_PROMPT.format(history="\n".join(parts))
        response = await llm.ainvoke(prompt)
        return _message_text(response).strip()


class HybridWindowStrategy(SummaryWindowStrategy):
    """生产级组合策略：工具输出截断 + 旧消息摘要 + 最近几轮原文 + token 预算兜底。

    - ``trim``：先截断工具输出，保留最近 ``keep_messages`` 条原文，再按
      ``max_tokens`` 从尾部向前兜底裁剪。
    - ``compact``：继承摘要压缩逻辑，把超出保留窗口的旧消息滚动摘要进
      长期画像 ``history_summary`` 后删除，摘要失败时同样受硬上限保护。
    """

    def __init__(
        self,
        keep_messages: int,
        max_tokens: int,
        tool_max_chars: int,
        hard_limit: int,
    ) -> None:
        super().__init__(keep_messages, hard_limit, tool_max_chars)
        self.max_tokens = max_tokens

    def trim(self, messages: list[BaseMessage]) -> list[BaseMessage]:
        messages = _truncate_tool_outputs(messages, self.tool_max_chars)
        if len(messages) > self.keep_messages:
            messages = messages[-self.keep_messages :]
        return _slice_by_tokens(messages, self.max_tokens)


def build_window_strategy(strategy: str | None = None) -> WindowStrategy:
    """根据配置创建窗口管理策略。"""
    strategy = strategy or settings.ECOMMERCE_WINDOW_STRATEGY
    if strategy == "token":
        return TokenBudgetWindowStrategy(settings.ECOMMERCE_WINDOW_MAX_TOKENS)
    if strategy == "summary":
        return SummaryWindowStrategy(
            settings.ECOMMERCE_WINDOW_SUMMARY_KEEP,
            settings.ECOMMERCE_WINDOW_SUMMARY_HARD_LIMIT,
        )
    if strategy == "hybrid":
        return HybridWindowStrategy(
            settings.ECOMMERCE_WINDOW_HYBRID_KEEP,
            settings.ECOMMERCE_WINDOW_HYBRID_MAX_TOKENS,
            settings.ECOMMERCE_WINDOW_TOOL_MAX_CHARS,
            settings.ECOMMERCE_WINDOW_HYBRID_HARD_LIMIT,
        )
    return TruncateWindowStrategy(settings.ECOMMERCE_WINDOW_MAX_MESSAGES)
