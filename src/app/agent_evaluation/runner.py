"""多 Agent 运行器：评估模块只依赖 AgentRunner 协议，不侵入编排器实现。

生产环境用 OrchestratorAgentRunner 跑真实 supervisor 图（消费现有 SSE 事件契约），
测试用 MockAgentRunner 返回固定轨迹，不依赖大模型与外部服务。
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from typing import Any, Protocol, runtime_checkable

from .schemas import AgentStep, AgentTrajectory


@runtime_checkable
class AgentRunner(Protocol):
    """运行器协议：输入问题与参数，产出一条多 Agent 轨迹。"""

    async def run(self, question: str, **params: Any) -> AgentTrajectory: ...


class OrchestratorAgentRunner:
    """调用现有 supervisor 编排器，把 SSE 事件还原成一条可评估的轨迹。"""

    async def run(self, question: str, **params: Any) -> AgentTrajectory:
        from app.core.agents.orchestrator.engine import stream_events

        context = params.get("context")
        pending: dict[str, deque[float]] = defaultdict(deque)
        steps: list[AgentStep] = []
        texts: list[str] = []
        error: str | None = None
        started = time.perf_counter()

        try:
            async for event in stream_events(question, context=context):
                kind = event.get("type")
                if kind == "agent_call":
                    name = str(event.get("agent") or "")
                    pending[name].append(time.perf_counter())
                    steps.append(AgentStep(agent=name, ok=True, message=str(event.get("message") or "")))
                elif kind == "agent_result":
                    name = str(event.get("agent") or "")
                    begin = pending[name].popleft() if pending[name] else None
                    latency = round(time.perf_counter() - begin, 4) if begin else None
                    # 按先入先出给最早一条尚未闭合的同名步骤补上耗时与成败。
                    for step in steps:
                        if step.agent == name and step.latency is None and step.ok:
                            step.latency = latency
                            step.ok = bool(event.get("ok", True))
                            break
                elif kind == "text":
                    texts.append(str(event.get("text") or ""))
        except Exception as exc:  # noqa: BLE001 - 单条用例失败不中断整批评估。
            error = f"{type(exc).__name__}: {exc}"

        return AgentTrajectory(
            question=question,
            answer="".join(texts),
            steps=steps,
            latency=round(time.perf_counter() - started, 4),
            error=error,
        )


class MockAgentRunner:
    """测试用运行器：按预置映射返回固定轨迹，未预置时生成模拟轨迹。"""

    def __init__(
        self,
        trajectories: dict[str, AgentTrajectory] | None = None,
        default: AgentTrajectory | None = None,
    ) -> None:
        self._trajectories = trajectories or {}
        self._default = default

    async def run(self, question: str, **params: Any) -> AgentTrajectory:
        """优先返回预置轨迹；否则返回默认轨迹（问题替换为当前问题）或模拟回答。"""
        if question in self._trajectories:
            return self._trajectories[question]
        if self._default is not None:
            return self._default.model_copy(update={"question": question})
        return AgentTrajectory(
            question=question,
            answer=f"这是对「{question}」的模拟回答。",
            steps=[AgentStep(agent="mock", ok=True, latency=0.01)],
            latency=0.05,
            input_tokens=100,
            output_tokens=50,
        )
