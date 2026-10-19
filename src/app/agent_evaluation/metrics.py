"""多 Agent 核心指标的纯函数实现：完成率、工具调用准确率、冲突率、延迟与成本。

判定口径（都写进 docstring，避免玄学）：

- 任务完成率：运行无异常、回答非空，且（若配置了关键词）关键词全部命中，
  且（若指定了期望子智能体）这些子智能体都成功返回；
- 工具调用准确率：期望子智能体集合与实际调用集合的 Dice 系数（0~1）；
- 冲突率：同一轮里出现失败的子智能体调用，或同一个子智能体被重复派单。
"""

from __future__ import annotations

from collections import Counter
from statistics import fmean
from typing import Any

from app.core.observability import estimate_cost

from .schemas import AgentEvalCase, AgentEvalResult, AgentEvaluationSummary, AgentStep, AgentTrajectory, JudgeScore


def evaluate_trajectory(
    case: AgentEvalCase,
    trajectory: AgentTrajectory,
    judge_score: JudgeScore | None = None,
) -> AgentEvalResult:
    """把一条用例 + 一条轨迹折算成单条评估结果。"""
    calls = [step.agent for step in trajectory.steps]
    ok_calls = [step.agent for step in trajectory.steps if step.ok]
    expected = list(dict.fromkeys(case.expected_agents))
    actual = list(dict.fromkeys(calls))

    keywords_hit = _keywords_hit(case.expected_keywords, trajectory.answer)
    status = "failed" if trajectory.error else "success"
    expected_ready = all(name in ok_calls for name in expected)
    completed = (
        status == "success"
        and bool(trajectory.answer.strip())
        and (keywords_hit is None or keywords_hit >= 1.0)
        and expected_ready
    )

    return AgentEvalResult(
        question=case.question,
        answer=trajectory.answer,
        status=status,
        error=trajectory.error,
        completed=completed,
        tool_calls=calls,
        tool_accuracy=_tool_accuracy(expected, actual),
        conflict=_conflict(trajectory.steps),
        latency=trajectory.latency,
        input_tokens=trajectory.input_tokens,
        output_tokens=trajectory.output_tokens,
        cost=estimate_cost(trajectory.input_tokens, trajectory.output_tokens),
        keywords_hit=keywords_hit,
        judge=judge_score,
    )


def summarize(results: list[AgentEvalResult], *, targets: dict[str, float]) -> AgentEvaluationSummary:
    """汇总一批结果，并按阈值给出各项是否达标。"""
    total = len(results)
    if total == 0:
        return AgentEvaluationSummary()

    completion_rate = round(sum(1 for item in results if item.completed) / total, 4)
    conflict_rate = round(sum(1 for item in results if item.conflict) / total, 4)
    avg_latency = round(fmean(item.latency for item in results), 4)
    avg_cost = round(fmean(item.cost for item in results), 6)

    tool_values = [item.tool_accuracy for item in results if item.tool_accuracy is not None]
    tool_call_accuracy = round(fmean(tool_values), 4) if tool_values else None

    judge_values = [item.judge.score for item in results if item.judge is not None and item.judge.score is not None]
    judge_score = round(fmean(judge_values), 4) if judge_values else None

    passed = {
        "completion_rate": completion_rate >= targets.get("completion_rate", 0.9),
        # 没做工具对照时不计为不达标（该用例本来就不考察路由）。
        "tool_call_accuracy": tool_call_accuracy is None
        or tool_call_accuracy >= targets.get("tool_call_accuracy", 0.95),
        "conflict_rate": conflict_rate <= targets.get("conflict_rate", 0.05),
        "avg_latency": avg_latency <= targets.get("avg_latency", 5.0),
    }

    return AgentEvaluationSummary(
        total=total,
        completion_rate=completion_rate,
        tool_call_accuracy=tool_call_accuracy,
        conflict_rate=conflict_rate,
        avg_latency=avg_latency,
        avg_cost=avg_cost,
        judge_score=judge_score,
        passed=passed,
    )


def summary_deltas(baseline: AgentEvaluationSummary, candidate: AgentEvaluationSummary) -> dict[str, float]:
    """计算候选变体相对基线变体的指标差值（候选 - 基线）。"""
    deltas: dict[str, float] = {}
    for key in ("completion_rate", "tool_call_accuracy", "conflict_rate", "avg_latency", "avg_cost", "judge_score"):
        base_value: Any = getattr(baseline, key)
        candidate_value: Any = getattr(candidate, key)
        if base_value is None or candidate_value is None:
            continue
        deltas[key] = round(float(candidate_value) - float(base_value), 6)
    return deltas


def _tool_accuracy(expected: list[str], actual: list[str]) -> float | None:
    """Dice 系数：2|交集| / (|期望| + |实际|)；两者都为空时返回 None。"""
    expected_set = set(expected)
    actual_set = set(actual)
    if not expected_set and not actual_set:
        return None
    matched = len(expected_set & actual_set)
    denominator = len(expected_set) + len(actual_set)
    return round(2 * matched / denominator, 4) if denominator else 0.0


def _keywords_hit(keywords: list[str], answer: str) -> float | None:
    """关键词命中率；未配置关键词时返回 None。"""
    if not keywords:
        return None
    hits = sum(1 for keyword in keywords if keyword and keyword in answer)
    return round(hits / len(keywords), 4)


def _conflict(steps: list[AgentStep]) -> bool:
    """冲突判定：任一子智能体调用失败，或同一子智能体被重复派单。"""
    if any(not step.ok for step in steps):
        return True
    return any(count > 1 for count in Counter(step.agent for step in steps).values())
