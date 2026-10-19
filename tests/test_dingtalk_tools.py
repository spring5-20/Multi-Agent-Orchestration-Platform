"""钉钉写操作确认语义测试。

核心约束：写类工具不再接受模型传入的 ``confirm`` 参数，是否真实执行只取决于调用方
注入的确认标记（编排器确认闭环 / 显式确认的接口），避免模型"自作主张"把消息真发出去。
"""

from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from src.app.core.agents.dingtalk import tools
from src.app.core.agents.orchestrator import dingtalk_adapter
from src.app.main import app

WRITE_TOOLS = (
    tools.create_calendar,
    tools.create_todo,
    tools.assign_todo,
    tools.send_message,
    tools.send_group_message,
    tools.send_robot_message,
    tools.publish_notice,
    tools.send_ding,
    tools.create_sheet,
    tools.create_log,
)


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[tuple[str, dict[str, Any]]]]:
    """替换 run_dws，记录每次调用的动作与参数，避免真的执行 dws。"""
    calls: list[tuple[str, dict[str, Any]]] = []

    def fake_run_dws(action: str, **params: Any) -> str:
        calls.append((action, params))
        return "ok"

    monkeypatch.setattr(tools, "run_dws", fake_run_dws)
    yield calls


def test_write_tools_do_not_expose_confirm_argument() -> None:
    """写类工具的 schema 里不能出现 confirm / config，模型无法伪造「已确认」。"""
    for tool in WRITE_TOOLS:
        args = set(tool.args.keys())
        assert "confirm" not in args, tool.name
        assert "config" not in args, tool.name


def test_write_tool_previews_without_confirmation(captured: list[tuple[str, dict[str, Any]]]) -> None:
    """没有确认标记时（含未传 config）写操作走预演。"""
    tools.send_message.invoke({"to": "张三", "content": "会议改期"})
    tools.send_message.invoke(
        {"to": "张三", "content": "会议改期"},
        config={"configurable": {"dingtalk_confirmed": False}},
    )

    assert [call[1]["confirm"] for call in captured] == [False, False]


def test_write_tool_executes_only_when_confirmed(captured: list[tuple[str, dict[str, Any]]]) -> None:
    """只有调用方注入确认标记时，写操作才带 confirm=True 真实执行。"""
    tools.send_message.invoke(
        {"to": "张三", "content": "会议改期"},
        config={"configurable": {"dingtalk_confirmed": True}},
    )
    tools.create_todo.invoke({"task": "交周报"}, config={"configurable": {"dingtalk_confirmed": True}})

    assert captured == [
        ("message_send", {"to": "张三", "content": "会议改期", "confirm": True}),
        ("todo_remind", {"task": "交周报", "at": None, "confirm": True}),
    ]


def test_read_tool_never_uses_confirmation(captured: list[tuple[str, dict[str, Any]]]) -> None:
    """读操作不受确认标记影响，也不带 confirm 参数。"""
    tools.list_calendar.invoke({})
    tools.search_contact.invoke({"query": "张三"})

    assert captured == [("calendar_list", {}), ("contact_search", {"query": "张三"})]


def test_robot_message_requires_webhook_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """未配置机器人 token 时直接报错，不会调用 dws。"""
    monkeypatch.setattr(tools.settings, "DINGTALK_ROBOT_WEBHOOK_TOKEN", None)
    with pytest.raises(RuntimeError, match="DINGTALK_ROBOT_WEBHOOK_TOKEN"):
        tools.send_robot_message.invoke({"content": "通知"})


@pytest.mark.anyio
async def test_orchestrator_adapter_passes_confirmation(monkeypatch: pytest.MonkeyPatch) -> None:
    """编排器适配器要把 context["confirmed"] 透传给钉钉智能体。"""
    seen: list[bool] = []

    async def fake_stream_answer(question: str, *, confirmed: bool = False) -> AsyncIterator[str]:
        seen.append(confirmed)
        yield "已执行"

    monkeypatch.setattr(dingtalk_adapter.dingtalk_agent, "stream_answer", fake_stream_answer)
    agent = dingtalk_adapter.DingTalkSubAgent()

    assert await agent.run("给张三发消息", context={"confirmed": True}) == "已执行"
    assert await agent.run("给张三发消息") == "已执行"

    assert seen == [True, False]


@pytest.mark.anyio
async def test_dingtalk_chat_route_never_confirms(monkeypatch: pytest.MonkeyPatch) -> None:
    """REST 对话入口必须按「未确认」调用，避免纯聊天触发真实写操作。"""
    import src.app.api.v1.dingtalk as dingtalk_api

    seen: list[bool] = []

    async def fake_stream_events(question: str, *, confirmed: bool = False) -> AsyncIterator[dict[str, Any]]:
        seen.append(confirmed)
        yield {"type": "content", "content": "ok"}

    monkeypatch.setattr(dingtalk_api.dingtalk_agent, "stream_events", fake_stream_events)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/api/v1/dingtalk/chat", json={"question": "给张三发消息"})

    assert response.status_code == 200
    assert seen == [False]
    assert '"type": "done"' in response.text
