"""评估服务：按指标名称逐个调用 RAGAS collections 指标的 ``ascore``。

注意 ragas 0.4.x 的 collections 指标不是旧 Metric 基类子类，不能直接
``ragas.evaluate(...)``，所以要按名称分别调用 ``ascore``：

- ``Faithfulness(question, response, contexts)``
- ``AnswerRelevancy(question, response)``
- ``ContextPrecision / ContextRecall(question, reference, contexts)``

其中 ``reference`` 就是标准答案 ``ground_truth``。
"""

from __future__ import annotations

from typing import Any

from .metrics import DEFAULT_METRIC_NAMES, build_ragas_embeddings, build_ragas_llm, resolve_metrics
from .runner import RagRunner
from .schemas import (
    BatchEvalResponse,
    CompareEvalResponse,
    CompareVariantResult,
    EvalResult,
    EvalSample,
    EvalTrace,
    MetricScore,
)


class EvaluationService:
    """评估服务：依赖 ``RagRunner`` 协议，与具体 RAG 实现解耦。"""

    def __init__(self, runner: RagRunner) -> None:
        self._runner = runner
        self._llm: Any = None
        self._embeddings: Any = None

    def _get_llm(self) -> Any:
        if self._llm is None:
            self._llm = build_ragas_llm()
        return self._llm

    def _get_embeddings(self) -> Any:
        if self._embeddings is None:
            self._embeddings = build_ragas_embeddings()
        return self._embeddings

    async def evaluate_single(
        self,
        sample: EvalSample,
        params: dict[str, Any] | None = None,
        metric_names: list[str] | None = None,
    ) -> EvalResult:
        """评估单条样本，返回四项指标分数。"""
        response = await self.evaluate_batch([sample], params=params, metric_names=metric_names)
        return response.results[0]

    async def evaluate_batch(
        self,
        samples: list[EvalSample],
        params: dict[str, Any] | None = None,
        metric_names: list[str] | None = None,
    ) -> BatchEvalResponse:
        """批量评估：逐条运行 RAG 收集产物，再统一打分。"""
        params = params or {}
        names = list(metric_names) if metric_names else list(DEFAULT_METRIC_NAMES)

        pairs: list[tuple[EvalSample, EvalTrace]] = []
        failed: list[EvalResult] = []

        for sample in samples:
            try:
                trace = await self._runner.run(sample.question, **params)
            except Exception as exc:  # noqa: BLE001 - 单条失败不中断整批。
                failed.append(
                    EvalResult(
                        question=sample.question,
                        answer="",
                        status="failed",
                        error=f"{type(exc).__name__}: {exc}",
                    )
                )
                continue
            pairs.append((sample, trace))

        results = failed
        if pairs:
            results = results + await self._score(pairs, names)

        return BatchEvalResponse(results=results, aggregate=_aggregate(results))

    async def compare(
        self,
        samples: list[EvalSample],
        variants: list[dict[str, Any]],
        metric_names: list[str] | None = None,
    ) -> CompareEvalResponse:
        """对比实验：同一批样本在不同参数下评估。"""
        variant_results: list[CompareVariantResult] = []
        for variant in variants:
            batch = await self.evaluate_batch(samples, params=variant, metric_names=metric_names)
            variant_results.append(
                CompareVariantResult(params=variant, aggregate=batch.aggregate, results=batch.results)
            )
        return CompareEvalResponse(variants=variant_results)

    async def _score(
        self,
        pairs: list[tuple[EvalSample, EvalTrace]],
        metric_names: list[str],
    ) -> list[EvalResult]:
        """对 (样本, 产物) 对列表逐指标打分。"""
        metrics = resolve_metrics(metric_names, self._get_llm(), self._get_embeddings())

        evaluated: list[EvalResult] = []
        for sample, trace in pairs:
            scores: list[MetricScore] = []
            for name, metric in metrics:
                scores.append(
                    MetricScore(name=name, score=await _score_metric(metric, name, sample, trace))
                )
            evaluated.append(
                EvalResult(question=sample.question, answer=trace.answer, scores=scores, status="success")
            )
        return evaluated


async def _score_metric(metric: Any, name: str, sample: EvalSample, trace: EvalTrace) -> float | None:
    """按指标名称调用对应 ``ascore``，返回分数；缺少输入或失败时返回 None。"""
    try:
        if name == "faithfulness":
            result = await metric.ascore(
                user_input=sample.question,
                response=trace.answer,
                retrieved_contexts=trace.contexts,
            )
        elif name == "answer_relevancy":
            result = await metric.ascore(user_input=sample.question, response=trace.answer)
        elif name in ("context_precision", "context_recall"):
            if not sample.ground_truth or not trace.contexts:
                return None
            result = await metric.ascore(
                user_input=sample.question,
                reference=sample.ground_truth,
                retrieved_contexts=trace.contexts,
            )
        else:
            return None
    except Exception:  # noqa: BLE001 - 单指标失败不影响其他指标。
        return None

    value = result.value
    if value is None:
        return None
    # value != value 是判断 NaN 的惯用写法（NaN 不等于自身）。
    if value != value:  # noqa: PLR0124
        return None
    return float(value)


def _aggregate(results: list[EvalResult]) -> dict[str, float]:
    """按指标求平均分，只统计实际可计算（score 非 None）的样本。"""
    buckets: dict[str, list[float]] = {}
    for result in results:
        for score in result.scores:
            if score.score is not None:
                buckets.setdefault(score.name, []).append(score.score)
    return {name: sum(values) / len(values) for name, values in buckets.items()}
