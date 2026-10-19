"""多 Agent 评估模块单元测试：注入假运行器与假判官，不调用真实模型。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from app.agent_evaluation.golden import DEFAULT_GOLDEN_CASES
from app.agent_evaluation.judge import _parse_json
from app.agent_evaluation.metrics import evaluate_trajectory, summarize
from app.agent_evaluation.runner import MockAgentRunner, OrchestratorAgentRunner
from app.agent_evaluation.schemas import (
    AgentEvalCase,
    AgentStep,
    AgentTrajectory,
    JudgeScore,
)
from app.agent_evaluation.service import AgentEvaluationService

TARGETS = {"completion_rate": 0.9, "tool_call_accuracy": 0.95, "conflict_rate": 0.05, "avg_latency": 5.0}


class _FakeJudge:
    """固定打分的假裁判，并记录调用次数。"""

    def __init__(self, score: float = 0.8) -> None:
        self.value = score
        self.calls = 0

    async def score(self, *, question: str, answer: str, ground_truth: str | None = None) -> JudgeScore:
        self.calls += 1
        return JudgeScore(score=self.value, reason="测试裁判", model="fake")


class _StubRunner:
    """按内置黄金集返回稳定轨迹的假运行器；可通过参数制造失败样本。"""

    async def run(self, question: str, **params: Any) -> AgentTrajectory:
        case = next((item for item in DEFAULT_GOLDEN_CASES if item.question == question), None)
        agent = case.expected_agents[0] if case and case.expected_agents else "mock"
        if params.get("fail"):
            return AgentTrajectory(
                question=question,
                answer="子智能体失败",
                steps=[AgentStep(agent=agent, ok=False, latency=0.1)],
                latency=0.2,
                input_tokens=100,
                output_tokens=20,
            )
        answer = "北京今天晴，适合跑步。" if "北京" in question else f"已处理：{question}"
        return AgentTrajectory(
            question=question,
            answer=answer,
            steps=[AgentStep(agent=agent, ok=True, latency=0.2)],
            latency=0.5,
            input_tokens=1000,
            output_tokens=500,
        )


def test_routes_registered() -> None:
    """批量评估与回归测试端点应挂载到 /api/v1/agent/evaluation 下。"""
    from app.main import app

    paths = app.openapi().get("paths", {})
    assert "post" in paths.get("/api/v1/agent/evaluation/batch", {})
    assert "post" in paths.get("/api/v1/agent/evaluation/regression", {})


def test_evaluate_trajectory_happy_path() -> None:
    """路由正确、关键词命中、无异常时应判定完成且无冲突。"""
    case = AgentEvalCase(question="北京今天适合跑步吗？", expected_agents=["qweather"], expected_keywords=["北京"])
    trajectory = AgentTrajectory(
        question=case.question,
        answer="北京今天晴，适合跑步。",
        steps=[AgentStep(agent="qweather", ok=True, latency=0.3)],
        latency=0.8,
        input_tokens=1000,
        output_tokens=500,
    )

    result = evaluate_trajectory(case, trajectory)

    assert result.completed is True
    assert result.status == "success"
    assert result.tool_accuracy == 1.0
    assert result.conflict is False
    assert result.keywords_hit == 1.0
    assert result.cost > 0


def test_conflict_detection_and_tool_miss() -> None:
    """子智能体失败与重复派单都应计入冲突，路由不符会拉低工具调用准确率。"""
    case = AgentEvalCase(question="北京今天适合跑步吗？", expected_agents=["qweather"])

    failed = evaluate_trajectory(
        case,
        AgentTrajectory(
            question=case.question,
            answer="暂时无法回答",
            steps=[AgentStep(agent="qweather", ok=False, latency=0.2)],
            latency=0.4,
        ),
    )
    repeated = evaluate_trajectory(
        case,
        AgentTrajectory(
            question=case.question,
            answer="北京今天晴",
            steps=[
                AgentStep(agent="qweather", ok=True, latency=0.1),
                AgentStep(agent="qweather", ok=True, latency=0.1),
            ],
            latency=0.4,
        ),
    )
    wrong_route = evaluate_trajectory(
        case,
        AgentTrajectory(
            question=case.question,
            answer="已推荐跑鞋",
            steps=[AgentStep(agent="ecommerce", ok=True, latency=0.1)],
            latency=0.3,
        ),
    )

    assert failed.completed is False and failed.conflict is True
    assert repeated.conflict is True
    assert wrong_route.tool_accuracy == 0.0
    assert wrong_route.completed is False


def test_summarize_applies_thresholds() -> None:
    """汇总应按阈值给出 passed 判定；无工具对照时不应因该项判负。"""
    good = evaluate_trajectory(
        AgentEvalCase(question="推荐 500 元以内的跑鞋"),
        AgentTrajectory(question="推荐 500 元以内的跑鞋", answer="推荐 Nike 跑鞋", steps=[], latency=0.2),
    )
    bad = evaluate_trajectory(
        AgentEvalCase(question="北京今天适合跑步吗？", expected_agents=["qweather"]),
        AgentTrajectory(
            question="北京今天适合跑步吗？",
            answer="",
            steps=[AgentStep(agent="qweather", ok=False, latency=0.1)],
            latency=9.0,
            error="运行失败",
        ),
    )

    summary = summarize([good], targets=TARGETS)
    assert summary.completion_rate == 1.0
    assert summary.tool_call_accuracy is None
    assert summary.passed["tool_call_accuracy"] is True

    failed_summary = summarize([bad], targets=TARGETS)
    assert failed_summary.completion_rate == 0.0
    assert failed_summary.conflict_rate == 1.0
    assert failed_summary.passed["completion_rate"] is False
    assert failed_summary.passed["avg_latency"] is False


@pytest.mark.anyio
async def test_service_batch_with_judge() -> None:
    """服务批量评估应汇总核心指标，并带上 LLM-as-Judge 平均分。"""
    judge = _FakeJudge(0.8)
    service = AgentEvaluationService(_StubRunner(), judge=judge)

    response = await service.evaluate_batch(use_judge=True)

    assert response.summary.total == len(DEFAULT_GOLDEN_CASES)
    assert response.summary.completion_rate == 1.0
    assert response.summary.tool_call_accuracy == 1.0
    assert response.summary.conflict_rate == 0.0
    assert response.summary.judge_score == 0.8
    assert judge.calls == len(DEFAULT_GOLDEN_CASES)
    assert all(response.summary.passed.values())


@pytest.mark.anyio
async def test_service_regression_deltas() -> None:
    """回归测试应输出候选变体相对基线的指标差值。"""
    service = AgentEvaluationService(_StubRunner(), judge=_FakeJudge(0.9))

    response = await service.compare(
        None,
        variants=[{"label": "baseline"}, {"label": "candidate", "fail": True}],
        use_judge=False,
    )

    assert [variant.label for variant in response.variants] == ["baseline", "candidate"]
    assert response.variants[0].summary.completion_rate == 1.0
    assert response.variants[1].summary.completion_rate == 0.0
    assert response.deltas["candidate.completion_rate"] == -1.0
    assert response.deltas["candidate.conflict_rate"] == 1.0


@pytest.mark.anyio
async def test_orchestrator_runner_rebuilds_trajectory(monkeypatch: Any) -> None:
    """生产运行器应把 SSE 事件还原成带耗时与成败的轨迹（用假事件序列，离线跑）。"""
    from app.core.agents.orchestrator import engine

    async def fake_stream_events(
        question: str, *, context: dict[str, Any] | None = None
    ) -> AsyncIterator[dict[str, Any]]:
        yield {"type": "agent_call", "agent": "qweather", "message": "北京天气"}
        yield {"type": "agent_result", "agent": "qweather", "ok": True}
        yield {"type": "text", "text": "北京今天晴"}

    monkeypatch.setattr(engine, "stream_events", fake_stream_events)

    trajectory = await OrchestratorAgentRunner().run("北京今天天气怎么样")

    assert trajectory.answer == "北京今天晴"
    assert len(trajectory.steps) == 1
    assert trajectory.steps[0].agent == "qweather"
    assert trajectory.steps[0].ok is True
    assert trajectory.steps[0].latency is not None
    assert trajectory.error is None


@pytest.mark.anyio
async def test_mock_runner_default() -> None:
    """MockAgentRunner 未预置用例时应返回可评估的模拟轨迹。"""
    trajectory = await MockAgentRunner().run("随便问一句")

    assert trajectory.question == "随便问一句"
    assert trajectory.answer
    assert trajectory.steps


@pytest.mark.anyio
async def test_batch_endpoint_with_mock_service(monkeypatch: pytest.MonkeyPatch) -> None:
    """批量评估接口应透传请求并返回汇总指标（用假服务，离线跑）。"""
    from app.api.v1 import agent_evaluation as api
    from app.main import app

    monkeypatch.setattr(
        api,
        "_build_service",
        lambda: AgentEvaluationService(_StubRunner(), judge=_FakeJudge(0.9)),
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/agent/evaluation/batch",
            json={
                "use_judge": True,
                "cases": [
                    {
                        "question": "北京今天适合跑步吗？",
                        "expected_agents": ["qweather"],
                        "expected_keywords": ["北京"],
                    }
                ],
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["summary"]["total"] == 1
    assert body["summary"]["completion_rate"] == 1.0
    assert body["summary"]["judge_score"] == 0.9


def test_judge_parse_json_and_fallback() -> None:
    """裁判输出解析：优先 JSON，退化时抓取 0~1 的数字。"""
    assert _parse_json('{"score": 0.7, "reason": "ok"}')["score"] == 0.7
    assert _parse_json("我觉得可以给 0.4 分")["score"] == 0.4
    assert _parse_json("完全跑偏了")["score"] is None
