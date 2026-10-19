"""和风天气子智能体的进程内适配实现。"""

from collections.abc import AsyncIterator
from typing import Any

from ..qweather import agent as qweather_agent
from .protocol import BaseSubAgent


class QWeatherSubAgent(BaseSubAgent):
    """把和风天气出行助手包装成编排器可调用的子智能体。"""

    name = "qweather"
    description = "和风天气出行助手：查询某地实时天气、未来多日预报，并给出出行与穿衣建议。"

    async def stream(self, message: str, *, context: dict[str, Any] | None = None) -> AsyncIterator[str]:
        """直接透传和风天气智能体的流式回答。"""
        async for chunk in qweather_agent.stream_answer(message):
            yield chunk
