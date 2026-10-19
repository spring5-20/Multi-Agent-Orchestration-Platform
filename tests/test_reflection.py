"""规划与反思模块单元测试：注入假 LLM / 自定义执行器，不依赖外部模型。"""

from __future__ import annotations

from typing import Any

from app.core.agents.reflection import (
    MAX_RETRIES,
    build_reflection_graph,
    run_reflection,
    run_task_with_replanning,
    should_replan,
)


class _FakeLLM:
    """按调用顺序返回内容的假 LLM，invoke 返回带 .content 的消息对象。"""

    def __init__(self, contents: list[str]) -> None:
        self._contents = contents
        self.calls = 0

    def invoke(self, prompt: str) -> Any:
        content = self._contents[min(self.calls, len(self._contents) - 1)]
        self.calls += 1

        class _Msg:
            def __init__(self, content: str) -> None:
                self.content = content

        return _Msg(content)


def test_routes_registered() -> None:
    """Reflection 与 Replanning 端点应挂载到 /api/v1/reflection 前缀下。"""
    from src.app.main import app

    paths = app.openapi().get("paths", {})
    assert "post" in paths.get("/api/v1/reflection/write", {})
    assert "post" in paths.get("/api/v1/reflection/execute", {})


def test_reflection_graph_generate_critique_revise() -> None:
    """Reflection 图应依次经过生成、批评、修订三个节点，产出最终稿。"""
    llm = _FakeLLM(["初稿", "批评意见", "最终稿"])
    graph = build_reflection_graph(llm)

    result = graph.invoke({"task": "写一篇文章", "draft": "", "critique": "", "final": ""})

    assert result["draft"] == "初稿"
    assert result["critique"] == "批评意见"
    assert result["final"] == "最终稿"
    assert llm.calls == 3


def test_run_reflection_returns_final() -> None:
    """run_reflection 便捷函数应直接返回修订后的最终结果。"""
    final = run_reflection("写一篇文章", llm=_FakeLLM(["初稿", "批评意见", "最终稿"]))
    assert final == "最终稿"


def test_should_replan_rules() -> None:
    """失败且未超过重试上限才重规划；成功或达到上限都结束。"""
    failed = {"task": "t", "plan": "", "status": "failed", "result": "", "error": "e"}
    success = {"task": "t", "plan": "", "status": "success", "result": "ok", "error": ""}

    assert should_replan({**failed, "retry_count": 0}) == "replan"
    assert should_replan({**failed, "retry_count": 2}) == "replan"
    assert should_replan({**failed, "retry_count": 3}) == "end"
    assert should_replan({**success, "retry_count": 0}) == "end"


def test_replanning_recovers_after_failures() -> None:
    """执行失败后应自动重规划重试，成功后携带最终计划与结果。"""
    attempts: list[int] = []

    def flaky_executor(state: Any) -> dict[str, Any]:
        attempts.append(state["retry_count"])
        if state["retry_count"] < 2:
            return {"status": "failed", "result": "", "error": "工具超时"}
        return {"status": "success", "result": "任务完成", "error": ""}

    state = run_task_with_replanning(
        "查询明天的天气并生成出行建议",
        executor=flaky_executor,
        llm=_FakeLLM(["初始计划", "重规划计划1", "重规划计划2"]),
    )

    assert state["status"] == "success"
    assert state["result"] == "任务完成"
    assert state["retry_count"] == 2
    assert state["plan"] == "重规划计划2"
    assert attempts == [0, 1, 2]


def test_replanning_stops_at_max_retries() -> None:
    """持续失败时应最多重规划 3 次后结束，状态为 failed。"""
    calls: list[int] = []

    def always_fails(state: Any) -> dict[str, Any]:
        calls.append(state["retry_count"])
        return {"status": "failed", "result": "", "error": "永久故障"}

    state = run_task_with_replanning(
        "不可能完成的任务",
        executor=always_fails,
        llm=_FakeLLM(["计划A", "计划B", "计划C", "计划D"]),
    )

    assert state["status"] == "failed"
    assert state["retry_count"] == MAX_RETRIES
    assert state["error"] == "永久故障"
    # 初次执行 + 3 次重试 = 4 次执行
    assert len(calls) == MAX_RETRIES + 1
