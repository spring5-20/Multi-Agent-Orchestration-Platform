"""电商导购子智能体的进程内适配实现。"""

from collections.abc import AsyncIterator
from typing import Any

from ..ecommerce.stream import stream_chat as ecommerce_stream_chat
from .protocol import BaseSubAgent


class ECommerceSubAgent(BaseSubAgent):
    """把电商导购智能体包装成编排器可调用的子智能体（带用户会话隔离）。"""

    name = "ecommerce"
    description = "电商导购助手：商品推荐、选品与购物问题解答，并记住用户偏好。"

    async def stream(self, message: str, *, context: dict[str, Any] | None = None) -> AsyncIterator[str]:
        """透传电商导购智能体的流式回答，并透传用户会话上下文。"""
        ctx = context or {}
        user_id = int(ctx.get("user_id") or 0)
        conversation_id = ctx.get("conversation_id")
        async for chunk in ecommerce_stream_chat(user_id, message, conversation_id):
            yield chunk
