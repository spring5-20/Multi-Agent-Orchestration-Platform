"""钉协助手智能体：大模型 + 钉钉工具，流式产出回答事件。

本模块只负责 Agent 的构建与事件流；HTTP 路由（``POST /api/v1/dingtalk/chat``）
定义在 :mod:`app.api.v1.dingtalk` 中，避免模块之间互相导入形成循环依赖。
"""

from collections.abc import AsyncGenerator
from typing import Any

from langchain.agents import create_agent
from langchain_openai import ChatOpenAI
from pydantic import SecretStr

from ...config import settings
from .tools import dingtalk_tools

_SYSTEM_PROMPT = (
    "你是「钉协助手智能体」，一个钉钉办公助手，可以帮用户完成通讯录搜索、日历日程、日志、待办、"
    "机器人/消息发送、DING 强提醒、AI 表格、考勤查询等办公操作。"
    "当用户请求与钉钉相关的操作时，请确认必要信息后调用对应工具；"
    "写操作（发消息、发 DING、创建日程/待办/日志/表格）执行前应确认内容无误，谨慎发送强提醒。"
    "返回结果用简洁的中文，不要输出思考过程或英文说明。"
    "如果用户的问题与钉钉无关，请直接友好回答，不要调用任何工具。"
)


def _build_agent() -> Any:
    """创建钉钉 Agent 实例（DeepSeek 大模型 + 钉钉工具）。"""
    model = settings.DEEPSEEK_MODEL
    api_key = settings.DEEPSEEK_API_KEY
    base_url = settings.DEEPSEEK_BASE_URL
    if not model or not api_key or not base_url:
        raise RuntimeError("DeepSeek 配置不完整，请检查 DEEPSEEK_API_KEY / DEEPSEEK_MODEL / DEEPSEEK_BASE_URL")

    llm = ChatOpenAI(
        model=model,
        api_key=SecretStr(api_key),
        base_url=base_url,
        temperature=0.7,
        streaming=True,
        timeout=60,
    )
    return create_agent(llm, dingtalk_tools, system_prompt=_SYSTEM_PROMPT)


_agent: Any | None = None


def get_agent() -> Any:
    """惰性创建并缓存 Agent 实例。"""
    global _agent
    if _agent is None:
        _agent = _build_agent()
    return _agent


async def stream_events(question: str, *, confirmed: bool = False) -> AsyncGenerator[dict[str, Any]]:
    """流式产出 Agent 事件。

    产出事件：
    - ``{"type": "tool", "name": str, "args": dict}``：模型决定调用钉钉工具。
    - ``{"type": "content", "content": str}``：最终回答的文本片段。

    工具调用前模型可能输出的思考文本会被丢弃，只保留最终回答。

    ``confirmed`` 是「用户已确认写操作」的标记，由调用方（编排器确认闭环、显式确认的接口）
    注入到运行配置里传给工具；模型自己无法伪造，未确认时写操作只做预演。
    """
    agent = get_agent()
    tool_seen = False
    buffer: list[str] = []

    async for event in agent.astream_events(
        {"messages": [{"role": "user", "content": question}]},
        config={"configurable": {"dingtalk_confirmed": confirmed}},
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


async def stream_answer(question: str, *, confirmed: bool = False) -> AsyncGenerator[str]:
    """流式输出纯文本回答（仅保留 ``content`` 事件）。"""
    async for event in stream_events(question, confirmed=confirmed):
        if event["type"] == "content":
            yield event["content"]
