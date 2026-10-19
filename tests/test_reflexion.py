"""Reflexion（反射）单元测试：注入假 LLM、自定义反思器与执行器，不依赖外部模型。"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from app.core.agents.reflection import (
    MAX_RETRIES,
    ReflexionState,
    run_task_with_reflexion,
)


class _FakeLLM:
    """按调用顺序返回内容的假 LLM，同时记录每次提示词便于断言反思被注入。"""

    def __init__(self, contents: list[str]) -> None:
        self._contents = contents
        self.calls = 0
        self.prompts: list[str] = []

    def invoke(self, prompt: str) -> Any:
        self.prompts.append(prompt)
        content = self._contents[min(self.calls, len(self._contents) - 1)]
        self.calls += 1
        return SimpleNamespace(content=content)


def test_route_registered() -> None:
    """Reflexion 端点应挂载到 /api/v1/reflection/reflexion。"""
    from src.app.main import app

    paths = app.openapi().get("paths", {})
    assert "post" in paths.get("/api/v1/reflection/reflexion", {})


def test_reflexion_recovers_and_injects_history() -> None:
    """失败后应记录文字反思、带着反思重新规划，并在成功时返回完整状态。"""
    attempts: list[int] = []

    def flaky_executor(state: ReflexionState) -> dict[str, Any]:
        attempts.append(state["retry_count"])
        if state["retry_count"] < 2:
            return {"status": "failed", "result": "", "error": "工具超时"}
        return {"status": "success", "result": "任务完成", "error": ""}

    reflections = iter(["反思1：应先确认参数", "反思2：应改用备用工具"])
    llm = _FakeLLM(["初始计划", "重规划计划1", "重规划计划2"])

    state = run_task_with_reflexion(
        "查询明天的天气并生成出行建议",
        executor=flaky_executor,
        llm=llm,
        reflector=lambda _state: next(reflections),
    )

    assert state["status"] == "success"
    assert state["result"] == "任务完成"
    assert state["retry_count"] == 2
    assert state["plan"] == "重规划计划2"
    assert state["reflections"] == ["反思1：应先确认参数", "反思2：应改用备用工具"]
    assert attempts == [0, 1, 2]
    # 第二次重规划的提示词里必须同时带上两条历史反思（这就是 Reflexion 与 Replanning 的差别）
    assert "反思1：应先确认参数" in llm.prompts[2]
    assert "反思2：应改用备用工具" in llm.prompts[2]


def test_reflexion_stops_at_max_retries() -> None:
    """持续失败时应留下 MAX_RETRIES 条反思，状态保持 failed。"""
    calls: list[int] = []
    reflections: list[str] = []

    def always_fails(state: ReflexionState) -> dict[str, Any]:
        calls.append(state["retry_count"])
        return {"status": "failed", "result": "", "error": "永久故障"}

    def reflect(state: ReflexionState) -> str:
        text = f"第{state['retry_count'] + 1}次反思"
        reflections.append(text)
        return text

    state = run_task_with_reflexion(
        "不可能完成的任务",
        executor=always_fails,
        llm=_FakeLLM(["计划A", "计划B", "计划C", "计划D"]),
        reflector=reflect,
    )

    assert state["status"] == "failed"
    assert state["retry_count"] == MAX_RETRIES
    assert state["error"] == "永久故障"
    assert len(state["reflections"]) == MAX_RETRIES
    assert state["reflections"] == reflections
    # 初次执行 + 3 次重试 = 4 次执行，3 次反思
    assert len(calls) == MAX_RETRIES + 1
