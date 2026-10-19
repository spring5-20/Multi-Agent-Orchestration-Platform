"""电商导购智能体的 LangGraph 工作流。

流程：START → recall（召回长期记忆）→ answer（LLM，可调用商品工具）
     → tools（执行工具，可选）→ … → memory_write（写入长期记忆）→ END

商品检索以工具形式交给 LLM 按需调用，而非固定节点执行。
短期记忆由 Checkpointer 管理：``messages`` 使用 ``add_messages`` reducer，
同一 ``thread_id``（按 ``conversation_id`` + ``user_id`` 隔离）跨请求累积对话历史。
"""

import asyncio
import logging
from typing import Any

from langchain_core.messages import AIMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode
from pydantic import SecretStr

from ...config import settings
from ..guard import build_injection_guard
from .extractor import extract_memory_items, worth_extracting
from .memory.base import MemoryItem, UserProfile, merge_profile_fields
from .prompts import SYSTEM_PROMPT, build_memory_context
from .runtime import get_checkpointer, get_memory_store, get_window_strategy
from .state import AgentState
from .tools import search_products

logger = logging.getLogger(__name__)

TOOLS = [search_products]

_graph: Any | None = None
_lock = asyncio.Lock()

_llm: ChatOpenAI | None = None


def _get_llm() -> ChatOpenAI:
    """获取大模型客户端单例（非流式）。"""
    global _llm
    if _llm is None:
        if not settings.DEEPSEEK_API_KEY or not settings.DEEPSEEK_MODEL or not settings.DEEPSEEK_BASE_URL:
            raise RuntimeError("DeepSeek 配置不完整，请检查 DEEPSEEK_API_KEY / DEEPSEEK_MODEL / DEEPSEEK_BASE_URL")
        _llm = ChatOpenAI(
            model=settings.DEEPSEEK_MODEL,
            api_key=SecretStr(settings.DEEPSEEK_API_KEY),
            base_url=settings.DEEPSEEK_BASE_URL,
            temperature=0.7,
            streaming=True,
            timeout=60,
        )
    return _llm


def _last_user_message(state: AgentState) -> str:
    """取最后一条用户消息文本。"""
    for message in reversed(state["messages"]):
        if getattr(message, "type", None) == "human":
            return str(message.content)
    return ""


async def _recall_node(state: AgentState) -> dict[str, Any]:
    """召回与当前问题相关的长期记忆与用户画像。"""
    user_id = state["user_id"]
    query = _last_user_message(state)
    store = get_memory_store()
    memories = await store.recall(user_id, query, top_k=settings.ECOMMERCE_MEMORY_TOP_K)
    profile = await store.get_profile(user_id)
    if memories:
        ids = [mem.id for mem in memories if mem.id is not None]
        if ids:
            await store.mark_recalled(user_id, ids)
    return {
        "recalled_memories": [mem.to_dict() for mem in memories],
        "user_profile": profile.to_dict() if profile else None,
    }


async def _answer_node(state: AgentState) -> dict[str, Any]:
    """让 LLM 决定直接回答，还是先调用商品检索工具。"""
    llm = _get_llm()
    query = _last_user_message(state)
    guard = build_injection_guard(query)
    context = build_memory_context(state.get("user_profile"), state.get("recalled_memories", []), query)
    system_parts = [part for part in (SYSTEM_PROMPT, guard, context) if part]
    system = "\n\n".join(system_parts)
    history = get_window_strategy().trim(state["messages"])
    messages = [SystemMessage(content=system), *history]

    try:
        response = await llm.bind_tools(TOOLS).ainvoke(messages)
    except Exception as exc:  # noqa: BLE001
        # 回答生成是核心路径：失败时返回兜底文案，避免整轮对话 500
        logger.warning("回答生成失败，返回兜底文案: %s", exc)
        return {"messages": [AIMessage(content="抱歉，我暂时无法回答，请稍后再试。")]}
    return {"messages": [response]}


def _final_answer(state: AgentState) -> str:
    """取最后一条不含工具调用的 AI 回复文本。"""
    for message in reversed(state["messages"]):
        if isinstance(message, AIMessage) and not getattr(message, "tool_calls", None):
            return str(message.content)
    return ""


async def _memory_write_node(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
    """对话结束后提取关键信息，写入长期记忆并更新画像。

    语音链路可通过 ``configurable.skip_memory_write`` 跳过这里的记忆提取，
    省掉一次额外的 LLM 调用；窗口压缩仍会执行，避免短期记忆无界增长。
    """
    user_id = state["user_id"]
    answer = _final_answer(state)
    skip_memory_write = bool(config.get("configurable", {}).get("skip_memory_write"))

    if answer and not skip_memory_write:
        user_message = _last_user_message(state)
        store = get_memory_store()

        if worth_extracting(user_message):
            try:
                extracted = await extract_memory_items(user_message, answer)
                memories_to_write = [
                    MemoryItem(content=content, memory_type="preference", importance=0.8)
                    for content in extracted["preferences"]
                ]
                memories_to_write += [
                    MemoryItem(content=content, memory_type="fact", importance=0.6) for content in extracted["facts"]
                ]
                memories_to_write += [
                    MemoryItem(content=content, memory_type="event", importance=0.7) for content in extracted["events"]
                ]
                if memories_to_write:
                    await store.write_many(user_id, memories_to_write)

                profile_data = extracted.get("profile") or {}
                if profile_data:
                    existing = await store.get_profile(user_id)
                    profile = existing or UserProfile(user_id=user_id)
                    if profile_data.get("name"):
                        profile.name = str(profile_data["name"])
                    updates = {key: value for key, value in profile_data.items() if key != "name"}
                    profile.preferences = merge_profile_fields(profile.preferences, updates)
                    await store.upsert_profile(profile)
            except Exception as exc:  # noqa: BLE001
                # 长期记忆是增强能力：提取/写入失败不阻塞本轮对话返回
                logger.warning("长期记忆提取或写入失败，本轮跳过记忆更新: %s", exc)

    # 窗口压缩：无论本轮是否产出回答都执行，避免异常场景下短期记忆无界增长
    removals = await get_window_strategy().compact(state["messages"], user_id)
    return {"messages": removals} if removals else {}


def _route_after_answer(state: AgentState) -> str:
    """answer 节点之后：有工具调用则执行工具，否则写入记忆。"""
    messages = state.get("messages", [])
    last = messages[-1] if messages else None
    if getattr(last, "tool_calls", None):
        return "tools"
    return "memory_write"


def build_graph(checkpointer: Any = None) -> Any:
    """构建并编译工作流图。"""
    builder = StateGraph(AgentState)
    builder.add_node("recall", _recall_node)
    builder.add_node("answer", _answer_node)
    builder.add_node("tools", ToolNode(TOOLS))
    builder.add_node("memory_write", _memory_write_node)
    builder.add_edge(START, "recall")
    builder.add_edge("recall", "answer")
    builder.add_conditional_edges(
        "answer",
        _route_after_answer,
        {"tools": "tools", "memory_write": "memory_write"},
    )
    builder.add_edge("tools", "answer")
    builder.add_edge("memory_write", END)
    return builder.compile(checkpointer=checkpointer)


async def get_graph() -> Any:
    """获取编译好的图单例，首次调用时异步初始化 Checkpointer。"""
    global _graph
    if _graph is not None:
        return _graph
    async with _lock:
        if _graph is not None:
            return _graph
        checkpointer = await get_checkpointer()
        _graph = build_graph(checkpointer=checkpointer)
        return _graph
