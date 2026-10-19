"""智能问数子智能体的进程内适配实现。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from ..bi import ask
from .protocol import BaseSubAgent


class BISubAgent(BaseSubAgent):
    """把智能问数包装成编排器可调用的子智能体。"""

    name = "bi"
    description = "智能问数助手：把用户的自然语言数据问题转换为数据库只读查询并解读结果（如销量、均价、排行）。"

    async def stream(self, message: str, *, context: dict[str, Any] | None = None) -> AsyncIterator[str]:
        """执行问数并按固定块流式产出结论文本（表格与图表仅在独立接口返回）。"""
        ctx = context or {}
        user_id = int(ctx.get("user_id") or 0)
        conversation_id = ctx.get("conversation_id")
        result = await ask(message, conversation_id=conversation_id, user_id=user_id)
        step = 24
        for index in range(0, len(result.answer), step):
            yield result.answer[index : index + step]
