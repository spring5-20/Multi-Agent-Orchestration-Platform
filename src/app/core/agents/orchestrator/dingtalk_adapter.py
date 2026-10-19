"""钉协助手子智能体的进程内适配实现。"""

from collections.abc import AsyncIterator
from typing import Any

from ..dingtalk import agent as dingtalk_agent
from .protocol import BaseSubAgent


class DingTalkSubAgent(BaseSubAgent):
    """把钉协助手智能体包装成编排器可调用的子智能体。"""

    name = "dingtalk"
    description = "钉钉办公助手：通讯录搜索、日历日程、待办、消息与 DING、日志、考勤等办公操作。"
    confirmation_required = True  # 钉钉写操作（发消息/日程/待办等）需先确认

    async def stream(self, message: str, *, context: dict[str, Any] | None = None) -> AsyncIterator[str]:
        """透传钉协助手智能体的流式回答，并把「已确认」标记传给写操作工具。

        编排器的确认闭环会在用户点击确认后把 context["confirmed"] 置为 True；
        只有这条路径能让钉钉写操作真实执行，聊天路径始终只做预演。
        """
        confirmed = bool((context or {}).get("confirmed"))
        async for chunk in dingtalk_agent.stream_answer(message, confirmed=confirmed):
            yield chunk
