"""多智能体编排控制台（/agents）前端与编排器接口测试。

覆盖点：
1. 页面与静态资源（CSS/JS）可正常访问；
2. 四套前端页面互相都有导航入口；
3. ``GET /api/v1/orchestrator/agents`` 暴露子智能体清单（含「需确认」标记）；
4. ``engine.stream_events`` 把 supervisor 图事件映射为 ``agent_call`` /
   ``agent_result`` / ``text``（用假图，离线跑，不调大模型）；
5. 确认闭环：``/chat`` 能返回 ``confirm_required``，``/confirm`` 能确认执行 / 取消 / 报错。
"""

from collections.abc import AsyncIterator, Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from src.app.core.agents.orchestrator import engine
from src.app.core.agents.orchestrator.confirmation import ConfirmationRequired, store_pending
from src.app.core.agents.orchestrator.protocol import BaseSubAgent
from src.app.main import app

# 页面与静态资源
AGENTS_PAGE_PATH = "/agents/"
# 四套前端页面：互相都应该能看到「多智能体」入口
NAV_PAGES = ("/ui/", "/dingtalk/", "/ecommerce/", "/ecommerce/voice.html", "/agents/")


class _StubAgent(BaseSubAgent):
    """假子智能体：离线验证确认闭环，不调用任何大模型或外部服务。"""

    name = "stub-agent"
    description = "测试用假子智能体"
    confirmation_required = True

    async def stream(self, message: str, *, context: dict[str, Any] | None = None) -> AsyncIterator[str]:
        yield "已执行：" + message


class _FakeGraph:
    """假 supervisor 图：按预设事件序列复现「先派单给子智能体，再给出最终回答」。"""

    def __init__(self, events: list[dict[str, Any]]) -> None:
        self._events = events

    async def astream_events(self, *args: Any, **kwargs: Any) -> AsyncIterator[dict[str, Any]]:
        for event in self._events:
            yield event


def _supervisor_stream(content: str) -> dict[str, Any]:
    """构造 supervisor 的一次流式输出事件。"""
    return {
        "event": "on_chat_model_stream",
        "metadata": {"langgraph_node": "supervisor"},
        "data": {"chunk": SimpleNamespace(content=content)},
    }


def _supervisor_end(tool_calls: list[dict[str, Any]] | None) -> dict[str, Any]:
    """构造 supervisor 的一次模型输出结束事件（是否带工具调用决定它是过渡输出还是最终回答）。"""
    return {
        "event": "on_chat_model_end",
        "metadata": {"langgraph_node": "supervisor"},
        "data": {"output": SimpleNamespace(tool_calls=tool_calls or [])},
    }


def _tools_end(*messages: Any) -> dict[str, Any]:
    """构造工具节点结束事件：输出里的 ToolMessage 就是各子智能体的返回。"""
    return {
        "event": "on_chain_end",
        "name": "tools",
        "metadata": {"langgraph_node": "tools"},
        "data": {"output": {"messages": list(messages)}},
    }


def _agent_message(name: str, content: str) -> Any:
    """构造一条子智能体返回的 ToolMessage（只用到 name / content 两个字段）。"""
    return SimpleNamespace(name=name, content=content)


@pytest.fixture
def stub_agent() -> Iterator[BaseSubAgent]:
    """临时注册假子智能体，测试结束后恢复原注册表（避免影响其它用例）。"""
    agent = _StubAgent()
    snapshot = dict(engine._registry)
    engine.register_agent(agent)
    yield agent
    engine._registry.clear()
    engine._registry.update(snapshot)
    engine._graph = None


async def _get(client: AsyncClient, path: str) -> Any:
    return await client.get(path)


@pytest.mark.anyio
async def test_agents_page_and_assets_available() -> None:
    """控制台页面与 CSS / JS 都应可访问，标题为「多智能体编排控制台」。"""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        page = await _get(client, AGENTS_PAGE_PATH)
        style = await _get(client, "/agents/styles.css")
        script = await _get(client, "/agents/app.js")

    assert page.status_code == 200
    assert "多智能体编排控制台" in page.text
    assert style.status_code == 200 and "text/css" in style.headers["content-type"]
    assert script.status_code == 200 and "javascript" in script.headers["content-type"]


@pytest.mark.anyio
async def test_agents_page_has_observability_and_eval_panels() -> None:
    """控制台应包含运行指标与质量评估面板，并指向对应接口。"""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        page = (await _get(client, AGENTS_PAGE_PATH)).text
        script = (await _get(client, "/agents/app.js")).text

    assert 'id="metricGrid"' in page
    assert 'id="evalResult"' in page
    assert "运行指标" in page and "质量评估" in page
    assert "/api/v1/observability/metrics" in page
    assert "/api/v1/agent/evaluation/batch" in page
    assert "loadMetrics" in script
    assert "runEvaluation" in script


@pytest.mark.anyio
async def test_navigation_links_between_pages() -> None:
    """四套前端页面互相都要有导航入口，避免新页面成为孤岛。"""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        pages = {path: (await _get(client, path)).text for path in NAV_PAGES}

    agents_page = pages[AGENTS_PAGE_PATH]
    for path in ("/ui/", "/dingtalk/", "/ecommerce/", "/ecommerce/voice.html"):
        assert path in agents_page, path

    for path, html in pages.items():
        if path == AGENTS_PAGE_PATH:
            continue
        assert "/agents/" in html, path


@pytest.mark.anyio
async def test_agents_endpoint_lists_subagents() -> None:
    """子智能体清单接口应返回默认注册的三个子智能体，钉钉标记为需确认。"""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/orchestrator/agents")

    assert response.status_code == 200
    agents = response.json()["agents"]
    names = {item["name"] for item in agents}
    assert {"qweather", "dingtalk", "ecommerce"} <= names

    dingtalk = next(item for item in agents if item["name"] == "dingtalk")
    assert dingtalk["confirmation_required"] is True
    assert dingtalk["description"]


@pytest.mark.anyio
async def test_stream_events_maps_tool_calls_to_agent_events(monkeypatch: pytest.MonkeyPatch) -> None:
    """工具调用应映射成 agent_call / agent_result，且过渡文本不会混进最终回答。"""
    events = [
        _supervisor_stream("我先查一下天气"),
        _supervisor_end([{"name": "qweather", "args": {"message": "北京天气"}, "id": "call-1"}]),
        _tools_end(_agent_message("qweather", "北京：晴，20℃")),
        _supervisor_stream("北京"),
        _supervisor_stream("今天晴"),
        _supervisor_end([]),
    ]
    monkeypatch.setattr(engine, "get_graph", lambda: _FakeGraph(events))

    collected = [event async for event in engine.stream_events("北京今天天气怎么样")]

    assert collected == [
        {"type": "agent_call", "agent": "qweather", "message": "北京天气"},
        {"type": "agent_result", "agent": "qweather", "ok": True},
        {"type": "text", "text": "北京"},
        {"type": "text", "text": "今天晴"},
    ]


@pytest.mark.anyio
async def test_stream_events_marks_failed_subagent(monkeypatch: pytest.MonkeyPatch) -> None:
    """子智能体执行失败时，agent_result 的 ok 应为 False。"""
    events = [
        _supervisor_end([{"name": "qweather", "args": {"message": "北京天气"}, "id": "call-1"}]),
        _tools_end(_agent_message("qweather", "[qweather 暂时无法回答]")),
        _supervisor_end([]),
    ]
    monkeypatch.setattr(engine, "get_graph", lambda: _FakeGraph(events))

    collected = [event async for event in engine.stream_events("北京今天天气怎么样")]

    assert {"type": "agent_result", "agent": "qweather", "ok": False} in collected
    assert all(event["type"] != "text" for event in collected)


@pytest.mark.anyio
async def test_stream_events_ignores_subagent_internal_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    """子智能体「内部工具」的事件不能被当成派单（真实链路里它们也带 node=tools）。"""
    events = [
        _supervisor_end([{"name": "qweather", "args": {"message": "北京天气"}, "id": "call-1"}]),
        _tools_end(_agent_message("query_qweather_now", "北京：晴")),
        _tools_end(_agent_message("qweather", "北京：晴，20℃")),
        _supervisor_stream("今天适合跑步"),
        _supervisor_end([]),
    ]
    monkeypatch.setattr(engine, "get_graph", lambda: _FakeGraph(events))

    collected = [event async for event in engine.stream_events("北京今天天气怎么样")]

    assert collected == [
        {"type": "agent_call", "agent": "qweather", "message": "北京天气"},
        {"type": "agent_result", "agent": "qweather", "ok": True},
        {"type": "text", "text": "今天适合跑步"},
    ]


@pytest.mark.anyio
async def test_stream_events_reports_repeated_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    """同一轮里同一个子智能体被派两次时，两次结果都要上报，否则前端轨迹会卡在「进行中」。"""
    events = [
        _supervisor_end(
            [
                {"name": "qweather", "args": {"message": "北京天气"}, "id": "call-1"},
                {"name": "qweather", "args": {"message": "上海天气"}, "id": "call-2"},
            ]
        ),
        _tools_end(_agent_message("qweather", "北京：晴"), _agent_message("qweather", "上海：多云")),
        _supervisor_stream("两地天气如下"),
        _supervisor_end([]),
    ]
    monkeypatch.setattr(engine, "get_graph", lambda: _FakeGraph(events))

    collected = [event async for event in engine.stream_events("北京和上海天气怎么样")]

    assert [event for event in collected if event["type"] == "agent_call"] == [
        {"type": "agent_call", "agent": "qweather", "message": "北京天气"},
        {"type": "agent_call", "agent": "qweather", "message": "上海天气"},
    ]
    assert [event for event in collected if event["type"] == "agent_result"] == [
        {"type": "agent_result", "agent": "qweather", "ok": True},
        {"type": "agent_result", "agent": "qweather", "ok": True},
    ]


@pytest.mark.anyio
async def test_chat_sse_emits_agent_events(monkeypatch: pytest.MonkeyPatch) -> None:
    """对话接口应把 agent_call / text / done 事件按契约透传给前端。"""
    events = [
        _supervisor_end([{"name": "qweather", "args": {"message": "北京天气"}, "id": "call-1"}]),
        _tools_end(_agent_message("qweather", "北京：晴")),
        _supervisor_stream("北京今天晴"),
        _supervisor_end([]),
    ]
    monkeypatch.setattr(engine, "get_graph", lambda: _FakeGraph(events))

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/orchestrator/chat",
            json={"question": "北京今天天气怎么样", "user_id": 1},
        )

    assert response.status_code == 200
    body = response.text
    assert '"type": "agent_call"' in body
    assert '"agent": "qweather"' in body
    assert '"type": "agent_result"' in body
    assert '"type": "text"' in body
    assert '"type": "done"' in body


@pytest.mark.anyio
async def test_chat_sse_emits_confirm_required(monkeypatch: pytest.MonkeyPatch) -> None:
    """需要确认的子智能体应触发 confirm_required 事件，并带上 confirm_id。"""
    import src.app.api.v1.orchestrator as orchestrator_api

    async def fake_stream_events(
        question: str, *, context: dict[str, Any] | None = None
    ) -> AsyncIterator[dict[str, Any]]:
        raise ConfirmationRequired("dingtalk", "给张三发消息", context, "即将执行操作：给张三发消息")
        yield {"type": "text", "text": ""}  # 使本函数成为异步生成器（不会执行）

    monkeypatch.setattr(orchestrator_api, "stream_events", fake_stream_events)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/api/v1/orchestrator/chat", json={"question": "给张三发消息"})

    assert response.status_code == 200
    body = response.text
    assert '"type": "confirm_required"' in body
    assert '"agent": "dingtalk"' in body
    assert "confirm_id" in body


@pytest.mark.anyio
async def test_confirm_executes_pending_action(stub_agent: BaseSubAgent) -> None:
    """确认后应真正调用子智能体，并把结果作为 text 流式返回。"""
    confirm_id = store_pending(stub_agent.name, "给张三发消息", {"user_id": 1})

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/orchestrator/confirm",
            json={"confirm_id": confirm_id, "decision": "confirm"},
        )

    assert response.status_code == 200
    body = response.text
    assert '"agent": "stub-agent"' in body
    assert "已执行：给张三发消息" in body
    assert '"ok": true' in body
    assert '"type": "done"' in body


@pytest.mark.anyio
async def test_confirm_cancel_and_unknown_id() -> None:
    """取消应返回 cancelled；未知 confirm_id 应返回 error 而不是 500。"""
    cancel_id = store_pending("stub-agent", "给张三发消息", None)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        cancelled = await client.post(
            "/api/v1/orchestrator/confirm",
            json={"confirm_id": cancel_id, "decision": "cancel"},
        )
        unknown = await client.post(
            "/api/v1/orchestrator/confirm",
            json={"confirm_id": "not-exist", "decision": "confirm"},
        )

    assert cancelled.status_code == 200
    assert '"type": "cancelled"' in cancelled.text

    assert unknown.status_code == 200
    assert '"type": "error"' in unknown.text
    assert "确认请求不存在或已过期" in unknown.text
