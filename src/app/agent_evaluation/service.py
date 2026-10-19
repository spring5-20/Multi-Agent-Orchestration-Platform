"""多 Agent 评估编排服务：采集轨迹 → 规则指标 + LLM-as-Judge → 汇总 / 回归对比。

对应课件的三种评估方法：

- 手动审查：返回逐条轨迹与裁判理由，便于人工抽查 20~50 条真实轨迹；
- LLM-as-Judge：可选调用裁判模型给答案打分；
- 回归测试：同一批黄金用例在多组参数下复跑，输出相对基线的指标差值。
"""

from __future__ import annotations

from typing import Any

from app.core.config import settings
from app.core.observability import log_event

from .golden import DEFAULT_GOLDEN_CASES
from .judge import Judge, LLMJudge
from .metrics import evaluate_trajectory, summarize, summary_deltas
from .runner import AgentRunner
from .schemas import (
    AgentEvalCase,
    AgentEvalResult,
    BatchEvalResponse,
    RegressionResponse,
    RegressionVariant,
)


class AgentEvaluationService:
    """评估服务：依赖 AgentRunner 协议，与具体编排实现解耦。"""

    def __init__(self, runner: AgentRunner, judge: Judge | None = None) -> None:
        self._runner = runner
        self._judge = judge

    def _get_judge(self) -> Judge:
        """惰性创建裁判，避免未启用判官时导入 / 初始化模型。"""
        if self._judge is None:
            self._judge = LLMJudge()
        return self._judge

    async def evaluate_batch(
        self,
        cases: list[AgentEvalCase] | None = None,
        *,
        use_judge: bool | None = None,
        context: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> BatchEvalResponse:
        """批量评估黄金测试集，返回逐条结果与汇总指标。"""
        selected = list(cases) if cases else list(DEFAULT_GOLDEN_CASES)
        judge_on = settings.AGENT_JUDGE_ENABLED if use_judge is None else bool(use_judge)

        results: list[AgentEvalResult] = []
        for case in selected:
            trajectory = await self._runner.run(case.question, context=context, **(params or {}))
            judge_score = None
            if judge_on:
                judge_score = await self._get_judge().score(
                    question=case.question,
                    answer=trajectory.answer,
                    ground_truth=case.ground_truth,
                )
            results.append(evaluate_trajectory(case, trajectory, judge_score))

        summary = summarize(results, targets=_targets())
        log_event(
            "agent_evaluation.batch_done",
            total=summary.total,
            completion_rate=summary.completion_rate,
            conflict_rate=summary.conflict_rate,
            tool_call_accuracy=summary.tool_call_accuracy,
        )
        return BatchEvalResponse(results=results, summary=summary)

    async def compare(
        self,
        cases: list[AgentEvalCase] | None,
        variants: list[dict[str, Any]],
        *,
        use_judge: bool | None = None,
    ) -> RegressionResponse:
        """回归测试：同一批用例在多组参数下复跑，输出各变体相对基线的差值。"""
        variant_results: list[RegressionVariant] = []
        for index, variant in enumerate(variants):
            label = str(variant.get("label") or f"variant-{index + 1}")
            params = {key: value for key, value in variant.items() if key != "label"}
            context = params.pop("context", None)
            batch = await self.evaluate_batch(cases, use_judge=use_judge, context=context, params=params)
            variant_results.append(
                RegressionVariant(label=label, params=params, summary=batch.summary, results=batch.results)
            )

        deltas: dict[str, float] = {}
        if variant_results:
            baseline = variant_results[0].summary
            for candidate in variant_results[1:]:
                for metric, value in summary_deltas(baseline, candidate.summary).items():
                    deltas[f"{candidate.label}.{metric}"] = value

        return RegressionResponse(variants=variant_results, deltas=deltas)


def _targets() -> dict[str, float]:
    """从配置读取各指标的验收阈值。"""
    return {
        "completion_rate": settings.AGENT_TARGET_COMPLETION_RATE,
        "tool_call_accuracy": settings.AGENT_TARGET_TOOL_ACCURACY,
        "conflict_rate": settings.AGENT_TARGET_CONFLICT_RATE,
        "avg_latency": settings.AGENT_TARGET_AVG_LATENCY,
    }
