"""编排器引擎：基于 LangGraph 的 supervisor（监督者）模式。"""

import logging
from collections.abc import AsyncIterator
from typing import Annotated, Any, TypedDict

from langchain_core.messages import AIMessage, BaseMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from pydantic import SecretStr

from ...config import settings
from ...observability import TraceContext, finish_trace, log_event, start_trace
from .confirmation import ConfirmationRequired
from .protocol import BaseSubAgent

logger = logging.getLogger(__name__)

# 子智能体执行失败时的工具返回标记：stream_events 据此判断本次派单是否成功
_AGENT_FAILURE_MARK = "暂时无法回答"
# 工具节点在子智能体名字不存在时返回的前缀，同样视为失败
_FAILURE_MARKS = (_AGENT_FAILURE_MARK, "未知子智能体")

_SUPERVISOR_SYSTEM_PREFIX = (
    "你是智能助手编排者（supervisor）。你可以直接回答用户，也可以把任务交给以下子智能体处理。"
    "你可以按需调用一个或多个子智能体，也可以不调用。子智能体的回答会返回给你，"
    "你可以据此继续调用其他子智能体，或直接给出最终回答。最终回答请用简洁的中文。\n"
    "子智能体列表：\n"
)


class SupervisorState(TypedDict, total=False):
    """supervisor 图状态：对话消息与透传给子智能体的上下文。"""

    messages: Annotated[list[BaseMessage], add_messages]
    context: dict[str, Any] | None


_registry: dict[str, BaseSubAgent] = {}
_graph: Any | None = None
_llm: ChatOpenAI | None = None
# 默认子智能体是否已注册（init 模块导入即注册）
_defaults_loaded = False


def _ensure_default_agents() -> None:
    """惰性注册默认子智能体。

    注册逻辑放在 init 模块中（导入即执行），这里统一兜底触发，
    避免调用方漏掉导入时监管者（supervisor）看不到任何子智能体。
    """
    global _defaults_loaded
    if _defaults_loaded:
        return
    _defaults_loaded = True
    from . import init as _default_agents  # noqa: F401  导入即注册默认子智能体


def register_agent(agent: BaseSubAgent) -> None:
    """向编排器注册一个子智能体（同名覆盖），并使缓存图失效。"""
    global _graph
    _registry[agent.name] = agent
    _graph = None


def clear_agents() -> None:
    """清空已注册的子智能体（主要用于测试隔离），并使缓存图失效。

    清空后不再自动注册默认子智能体，便于测试自行装配注册表。
    """
    global _defaults_loaded, _graph
    _defaults_loaded = True
    _registry.clear()
    _graph = None


def get_agents() -> list[BaseSubAgent]:
    """返回当前已注册的子智能体，按注册顺序。"""
    _ensure_default_agents()
    return list(_registry.values())


def get_agent(name: str) -> BaseSubAgent | None:
    """按名称取子智能体，未注册时返回 None。"""
    _ensure_default_agents()
    return _registry.get(name)


def build_supervisor_system_prompt() -> str:
    """构造 supervisor 系统提示词，附带各子智能体的能力描述。"""
    lines = [_SUPERVISOR_SYSTEM_PREFIX]
    for agent in get_agents():
        lines.append(f"- {agent.name}：{agent.description}")
    return "\n".join(lines)


def _get_llm() -> ChatOpenAI:
    """获取大模型客户端单例（流式）。"""
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


def _make_agent_tool(agent: BaseSubAgent) -> Any:
    """为子智能体生成工具 schema（仅供大模型识别，实际执行在工具节点）。"""

    @tool(agent.name, description=agent.description)
    async def call(message: str) -> str:
        """调用子智能体处理消息，返回其回答文本。"""
        return ""

    return call


def _build_agent_tools() -> list[Any]:
    """按当前注册表生成 supervisor 可见的子智能体工具列表。"""
    return [_make_agent_tool(agent) for agent in get_agents()]


def _route_after_supervisor(state: SupervisorState) -> str:
    """supervisor 节点之后：有工具调用则执行工具，否则结束。"""
    messages = state.get("messages", [])
    last = messages[-1] if messages else None
    if getattr(last, "tool_calls", None):
        return "tools"
    return "end"


def build_graph() -> Any:
    """构建 supervisor 图：supervisor 节点与工具节点循环。"""
    tools = _build_agent_tools()

    async def supervisor_node(state: SupervisorState) -> dict[str, Any]:
        """supervisor 节点：大模型决定调用子智能体或直接回答。"""
        system = SystemMessage(content=build_supervisor_system_prompt())
        response = await _get_llm().bind_tools(tools).ainvoke([system, *state["messages"]])
        return {"messages": [response]}

    async def tools_node(state: SupervisorState) -> dict[str, Any]:
        """工具节点：按大模型的调用名执行对应子智能体。"""
        messages = state.get("messages", [])
        last = messages[-1] if messages else None
        tool_messages: list[ToolMessage] = []
        for call in getattr(last, "tool_calls", None) or []:
            name = call["name"]
            agent = get_agent(name)
            message = str((call.get("args") or {}).get("message", ""))
            if agent is None:
                result = f"未知子智能体：{name}"
            else:
                # 需要确认的子智能体：不在这里执行，而是向上抛出信号，交给 API 转成前端交互
                if agent.needs_confirmation(state.get("context")):
                    raise ConfirmationRequired(
                        agent_name=name,
                        message=message,
                        context=state.get("context"),
                        prompt=agent.confirmation_prompt(message),
                    )
                try:
                    result = await agent.run(message, context=state.get("context"))
                except Exception as exc:  # noqa: BLE001
                    logger.warning("子智能体 %s 执行失败: %s", name, exc)
                    result = f"[{name} {_AGENT_FAILURE_MARK}]"
            tool_messages.append(ToolMessage(content=result, name=name, tool_call_id=call["id"]))
        return {"messages": tool_messages}

    builder = StateGraph(SupervisorState)
    builder.add_node("supervisor", supervisor_node)
    builder.add_node("tools", tools_node)
    builder.add_edge(START, "supervisor")
    builder.add_conditional_edges("supervisor", _route_after_supervisor, {"tools": "tools", "end": END})
    builder.add_edge("tools", "supervisor")
    return builder.compile()


def get_graph() -> Any:
    """获取编译好的 supervisor 图单例。"""
    global _graph
    if _graph is None:
        _graph = build_graph()
    return _graph


def _extract_final_answer(messages: list[Any]) -> str:
    """从消息列表逆序取最后一条无工具调用的 AI 回答文本。"""
    for message in reversed(messages):
        if isinstance(message, AIMessage) and not getattr(message, "tool_calls", None):
            content = message.content
            if isinstance(content, str) and content:
                return content
    return ""


async def invoke(question: str, *, context: dict[str, Any] | None = None) -> str:
    """非流式执行 supervisor 图，返回最终回答文本（供测试与内部使用）。"""
    graph = get_graph()
    result = await graph.ainvoke({"messages": [{"role": "user", "content": question}], "context": context})
    return _extract_final_answer(result.get("messages", []))


def _record_usage(trace: TraceContext, output: Any) -> None:
    """从 LangChain 消息的 usage_metadata 里提取 Token 用量（缺失时忽略）。"""
    usage = getattr(output, "usage_metadata", None)
    if not isinstance(usage, dict):
        return
    trace.add_tokens(
        input_tokens=int(usage.get("input_tokens") or 0),
        output_tokens=int(usage.get("output_tokens") or 0),
    )


def _tool_messages(event: dict[str, Any]) -> list[Any]:
    """从工具节点的 on_chain_end 事件里取出 ToolMessage 列表（取不到时返回空列表）。"""
    output = (event.get("data") or {}).get("output")
    if isinstance(output, dict):
        messages = output.get("messages")
        if isinstance(messages, list):
            return messages
    return []


async def stream_events(
    question: str,
    *,
    context: dict[str, Any] | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """编排入口：运行 supervisor 图，产出结构化事件。

    事件类型：

    - agent_call：supervisor 决定调用某个子智能体，带 agent 名与转发给它的消息；
    - agent_result：该子智能体返回，ok 为 False 表示执行失败；
    - text：最终回答的增量文本。

    中间的工具调用与过渡文本不会被当作 text 产出，只保留大模型最终给出的回答，
    这样前端既能看到派单过程，也不会把过渡文本混进最终回答。
    """
    trace = start_trace(question)
    answer_parts: list[str] = []
    turn_ok = True
    try:
        async for event in _iter_events(question, context=context, trace=trace):
            if event["type"] == "text":
                answer_parts.append(event["text"])
            yield event
    except BaseException:
        # 任何异常（含确认中断）都要落一条失败轨迹，再原样抛给调用方处理。
        turn_ok = False
        raise
    finally:
        finished = finish_trace(trace, answer="".join(answer_parts), ok=turn_ok and bool(answer_parts))
        log_event(
            "orchestrator.turn_done",
            trace_id=finished.trace_id,
            status=finished.status,
            duration=finished.duration,
            agents=[span.name for span in finished.spans if span.node == "agent"],
        )


async def _iter_events(
    question: str,
    *,
    context: dict[str, Any] | None,
    trace: TraceContext,
) -> AsyncIterator[dict[str, Any]]:
    """运行 supervisor 图，把原始事件翻译为前端契约事件，并记录本地轨迹与指标。"""
    graph = get_graph()
    buffer: list[str] = []
    # 本轮派过单的子智能体名字：只有登记过的名字才认结果，
    # 从而把子智能体「内部工具」的同名事件排除在外。
    # 这里刻意不「消费」名字（同一轮里同一个子智能体可能被派两次，
    # 两次结果都要上报，否则前端轨迹会永远停在「进行中」）。
    dispatched: set[str] = set()

    async for event in graph.astream_events(
        {"messages": [{"role": "user", "content": question}], "context": context},
        version="v2",
    ):
        kind = event["event"]
        node = (event.get("metadata") or {}).get("langgraph_node")

        if kind == "on_chat_model_end" and node == "supervisor":
            output = event["data"]["output"]
            _record_usage(trace, output)
            tool_calls = getattr(output, "tool_calls", None) or []
            if tool_calls:
                # 这次输出是「决定派单」的过渡输出：先上报派给了谁，再丢弃过渡文本
                buffer.clear()
                for call in tool_calls:
                    name = str(call.get("name") or "")
                    args = call.get("args") or {}
                    forwarded = str(args.get("message", "")) if isinstance(args, dict) else ""
                    dispatched.add(name)
                    trace.span(name, node="agent", message=forwarded)
                    log_event("orchestrator.agent_call", trace_id=trace.trace_id, agent=name, message=forwarded)
                    yield {"type": "agent_call", "agent": name, "message": forwarded}
            else:
                # 最终回答，逐段产出
                for text in buffer:
                    yield {"type": "text", "text": text}
                buffer.clear()
        elif kind == "on_chain_end" and node == "tools":
            # 工具节点输出里的 ToolMessage，其 name 就是被派单的子智能体名
            for message in _tool_messages(event):
                name = str(getattr(message, "name", "") or "")
                if name not in dispatched:
                    continue
                content = str(getattr(message, "content", ""))
                ok = not any(mark in content for mark in _FAILURE_MARKS)
                trace.close_span(name, status="success" if ok else "failed", content=content[:200])
                log_event("orchestrator.agent_result", trace_id=trace.trace_id, agent=name, ok=ok)
                yield {"type": "agent_result", "agent": name, "ok": ok}
        elif kind == "on_chat_model_stream" and node == "supervisor":
            content = event["data"]["chunk"].content
            if isinstance(content, str) and content:
                buffer.append(content)

    for text in buffer:
        yield {"type": "text", "text": text}


async def stream_answer(
    question: str,
    *,
    context: dict[str, Any] | None = None,
) -> AsyncIterator[str]:
    """编排入口（纯文本版）：只产出最终回答文本，供不需要中间事件的调用方使用。"""
    async for event in stream_events(question, context=context):
        if event["type"] == "text":
            yield event["text"]
