"""可观测性（Trace / Metric / Log）单元测试：离线跑，不依赖大模型与外部服务。"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.agents.orchestrator import engine
from app.core.observability import (
    finish_trace,
    metrics_snapshot,
    recent_traces,
    render_prometheus,
    reset_observability,
    start_trace,
)
from app.main import app


@pytest.fixture(autouse=True)
def _clean_metrics() -> Iterator[None]:
    """每个用例前后都清空指标，避免用例之间互相污染。"""
    reset_observability()
    yield
    reset_observability()


class _FakeGraph:
    """假 supervisor 图：按预设事件序列复现一次「派单 + 最终回答」。"""

    def __init__(self, events: list[dict[str, Any]]) -> None:
        self._events = events

    async def astream_events(self, *args: Any, **kwargs: Any) -> AsyncIterator[dict[str, Any]]:
        for event in self._events:
            yield event


def _supervisor_end(tool_calls: list[dict[str, Any]] | None, usage: dict[str, int] | None = None) -> dict[str, Any]:
    """构造 supervisor 模型输出结束事件（可带工具调用与 Token 用量）。"""
    output = SimpleNamespace(tool_calls=tool_calls or [], usage_metadata=usage)
    return {
        "event": "on_chat_model_end",
        "metadata": {"langgraph_node": "supervisor"},
        "data": {"output": output},
    }


def _supervisor_stream(content: str) -> dict[str, Any]:
    """构造 supervisor 流式文本事件。"""
    return {
        "event": "on_chat_model_stream",
        "metadata": {"langgraph_node": "supervisor"},
        "data": {"chunk": SimpleNamespace(content=content)},
    }


def _tools_end(*messages: Any) -> dict[str, Any]:
    """构造工具节点结束事件。"""
    return {
        "event": "on_chain_end",
        "metadata": {"langgraph_node": "tools"},
        "data": {"output": {"messages": list(messages)}},
    }


def _agent_message(name: str, content: str) -> Any:
    """构造子智能体返回的 ToolMessage（只用到 name / content）。"""
    return SimpleNamespace(name=name, content=content)


def test_trace_records_spans_and_tokens() -> None:
    """本地轨迹应记录 supervisor 与子智能体跨度、Token 与最终状态。"""
    trace = start_trace("北京今天天气怎么样")
    trace.span("supervisor", node="supervisor")
    trace.close_span("supervisor", status="success")
    trace.span("qweather", node="agent", message="北京天气")
    trace.close_span("qweather", status="success")
    trace.add_tokens(input_tokens=1000, output_tokens=500)

    finished = finish_trace(trace, answer="北京今天晴", ok=True)

    assert finished.status == "success"
    assert finished.input_tokens == 1000
    assert finished.output_tokens == 500
    assert [span.name for span in finished.spans] == ["supervisor", "qweather"]
    assert all(span.status == "success" for span in finished.spans)

    traces = recent_traces(5)
    assert len(traces) == 1
    assert traces[0]["trace_id"] == finished.trace_id
    assert traces[0]["duration"] >= 0


def test_metrics_snapshot_and_prometheus() -> None:
    """指标快照应统计错误率、延迟、子智能体调用与成本，并能导出 Prometheus 文本。"""
    ok_trace = start_trace("天气")
    ok_trace.span("qweather", node="agent")
    ok_trace.close_span("qweather", status="success")
    ok_trace.add_tokens(input_tokens=1000, output_tokens=1000)
    finish_trace(ok_trace, answer="晴", ok=True)

    bad_trace = start_trace("钉钉")
    bad_trace.span("dingtalk", node="agent")
    bad_trace.close_span("dingtalk", status="failed")
    finish_trace(bad_trace, answer="", ok=False)

    snapshot = metrics_snapshot()
    assert snapshot["turns_total"] == 2
    assert snapshot["turns_failed_total"] == 1
    assert snapshot["error_rate"] == 0.5
    assert snapshot["agent_calls"] == {"qweather": 1, "dingtalk": 1}
    assert snapshot["agent_failures"] == {"dingtalk": 1}
    assert snapshot["input_tokens_total"] == 1000
    assert snapshot["output_tokens_total"] == 1000
    assert snapshot["cost_total"] > 0

    text = render_prometheus()
    assert "agent_turns_total 2" in text
    assert 'agent_calls_total{agent="qweather"} 1' in text
    assert 'agent_failures_total{agent="dingtalk"} 1' in text


@pytest.mark.anyio
async def test_stream_events_records_metrics_without_changing_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """编排器埋点不应改变事件契约，同时应把这一轮计入指标。"""
    events = [
        _supervisor_stream("我先查一下"),
        _supervisor_end(
            [{"name": "qweather", "args": {"message": "北京天气"}, "id": "call-1"}],
            usage={"input_tokens": 100, "output_tokens": 20},
        ),
        _tools_end(_agent_message("qweather", "北京：晴")),
        _supervisor_stream("北京今天晴"),
        _supervisor_end([], usage={"input_tokens": 50, "output_tokens": 10}),
    ]
    monkeypatch.setattr(engine, "get_graph", lambda: _FakeGraph(events))

    collected = [event async for event in engine.stream_events("北京今天天气怎么样")]

    assert collected == [
        {"type": "agent_call", "agent": "qweather", "message": "北京天气"},
        {"type": "agent_result", "agent": "qweather", "ok": True},
        {"type": "text", "text": "北京今天晴"},
    ]

    snapshot = metrics_snapshot()
    assert snapshot["turns_total"] == 1
    assert snapshot["turns_failed_total"] == 0
    assert snapshot["agent_calls"] == {"qweather": 1}
    assert snapshot["input_tokens_total"] == 150
    assert snapshot["output_tokens_total"] == 30
    traces = recent_traces(5)
    assert traces[0]["answer"] == "北京今天晴"


@pytest.mark.anyio
async def test_observability_endpoints() -> None:
    """可观测性接口应返回状态、指标快照、Prometheus 文本与最近轨迹。"""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        status = await client.get("/api/v1/observability/status")
        metrics = await client.get("/api/v1/observability/metrics")
        prometheus = await client.get("/api/v1/observability/prometheus")
        traces = await client.get("/api/v1/observability/traces")

    assert status.status_code == 200
    assert "langsmith_enabled" in status.json()
    assert metrics.status_code == 200 and "turns_total" in metrics.json()
    assert prometheus.status_code == 200
    assert "text/plain" in prometheus.headers["content-type"]
    assert "agent_turns_total" in prometheus.text
    assert traces.status_code == 200 and "traces" in traces.json()
