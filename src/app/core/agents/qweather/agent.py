from collections.abc import AsyncGenerator
from typing import Any

from langchain.agents import create_agent
from langchain_openai import ChatOpenAI
from pydantic import SecretStr

from ...config import settings
from .tools import qweather_tools

_SYSTEM_PROMPT = (
    "你是一个友好的 AI 助手，可以陪用户闲聊，也能在用户询问天气时提供帮助。"
    "当用户询问某地天气、几号适合出行、出行计划或穿衣建议时，"
    "请直接调用天气查询工具获取实时天气或未来几天的预报，不要输出思考过程或英文说明，"
    "然后基于查询结果用简洁实用的中文给出出行建议。"
    "如果用户询问未来几天，请使用预报工具；如果询问当前天气，请使用实时天气工具。"
    "如果用户的问题与天气无关，请直接以友好、自然的方式回答，不要调用任何工具。"
)


def _build_agent() -> Any:
    model = settings.DEEPSEEK_MODEL
    api_key = settings.DEEPSEEK_API_KEY
    if not model or not api_key:
        raise RuntimeError("请先在 src/.env 中配置 DEEPSEEK_MODEL 与 DEEPSEEK_API_KEY")

    llm = ChatOpenAI(
        model=model,
        api_key=SecretStr(api_key),
        base_url=settings.DEEPSEEK_BASE_URL,
        temperature=0.7,
        streaming=True,
        timeout=60,
    )
    return create_agent(llm, qweather_tools, system_prompt=_SYSTEM_PROMPT)


_agent: Any | None = None


def get_agent() -> Any:
    """惰性创建并缓存 Agent 实例。"""
    global _agent
    if _agent is None:
        _agent = _build_agent()
    return _agent


async def stream_events(question: str) -> AsyncGenerator[dict[str, Any]]:
    """流式产出 Agent 事件。

    产出事件：
    - ``{"type": "tool", "name": str, "args": dict}``：模型决定调用天气工具。
    - ``{"type": "content", "content": str}``：最终回答的文本片段。

    工具调用前模型可能输出的思考文本会被丢弃，只保留最终回答。
    """
    agent = get_agent()
    tool_seen = False
    buffer: list[str] = []

    async for event in agent.astream_events(
        {"messages": [{"role": "user", "content": question}]},
        version="v2",
    ):
        kind = event["event"]

        if kind == "on_tool_start":
            tool_seen = True
            buffer.clear()
            yield {
                "type": "tool",
                "name": event["name"],
                "args": event["data"].get("input") or {},
            }
            continue

        if kind == "on_chat_model_stream":
            content = event["data"]["chunk"].content
            if not isinstance(content, str) or not content:
                continue
            if tool_seen:
                yield {"type": "content", "content": content}
            else:
                buffer.append(content)

    if not tool_seen:
        for text in buffer:
            yield {"type": "content", "content": text}


async def stream_answer(question: str) -> AsyncGenerator[str]:
    """流式输出纯文本回答（仅保留 ``content`` 事件）。"""
    async for event in stream_events(question):
        if event["type"] == "content":
            yield event["content"]
