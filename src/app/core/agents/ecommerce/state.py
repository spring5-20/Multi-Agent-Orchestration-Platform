"""电商导购智能体的图状态定义。

``messages`` 是短期记忆的载体：使用 ``add_messages`` reducer，
会话内的对话历史由 LangGraph Checkpointer 自动累积，而不是覆盖。
"""

from typing import Annotated, Any, TypedDict

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages


class AgentState(TypedDict, total=False):
    """图节点间共享的状态（短期记忆载体 + 长期记忆召回结果）。"""

    messages: Annotated[list[BaseMessage], add_messages]
    user_id: int
    # 召回的长期记忆，dict 列表，保证 PostgresSaver 可 JSON 序列化
    recalled_memories: list[dict[str, Any]]
    # 用户结构化画像
    user_profile: dict[str, Any] | None
