"""RAG 评估模块单元测试：MockRagRunner + mock 打分，不调真实模型。"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.rag_evaluation.runner import MockRagRunner
from app.rag_evaluation.schemas import EvalSample, EvalTrace
from app.rag_evaluation.service import EvaluationService

METRIC_NAMES = ["faithfulness", "answer_relevancy", "context_precision", "context_recall"]


@pytest.fixture
def anyio_backend() -> str:
    """使用 asyncio 作为 anyio 后端。"""
    return "asyncio"


@pytest.fixture
def score_mock(mocker: Any) -> AsyncMock:
    """替换指标打分函数，并伪造 LLM/Embedding/指标解析，避免外部 API 调用。"""
    mocker.patch("app.rag_evaluation.service.build_ragas_llm", return_value=object())
    mocker.patch("app.rag_evaluation.service.build_ragas_embeddings", return_value=object())
    mocker.patch(
        "app.rag_evaluation.service.resolve_metrics",
        return_value=[(name, object()) for name in METRIC_NAMES],
    )
    return mocker.patch(
        "app.rag_evaluation.service._score_metric",
        new=AsyncMock(return_value=0.9),
    )


@pytest.mark.anyio
async def test_evaluate_single(score_mock: AsyncMock) -> None:
    """单条评估应返回四项指标分数。"""
    service = EvaluationService(MockRagRunner())
    result = await service.evaluate_single(EvalSample(question="什么是RAG？", ground_truth="检索增强生成"))

    assert result.status == "success"
    assert {score.name for score in result.scores} == set(METRIC_NAMES)
    assert all(score.score == 0.9 for score in result.scores)


@pytest.mark.anyio
async def test_evaluate_batch_aggregate(score_mock: AsyncMock) -> None:
    """批量评估应返回逐条结果与平均分。"""
    samples = [EvalSample(question=f"问题{i}") for i in range(3)]

    response = await EvaluationService(MockRagRunner()).evaluate_batch(samples)

    assert len(response.results) == 3
    assert response.aggregate["faithfulness"] == pytest.approx(0.9)


@pytest.mark.anyio
async def test_compare_variants(score_mock: AsyncMock) -> None:
    """对比实验应为每组参数分别产出结果。"""
    samples = [EvalSample(question="什么是RAG？")]

    response = await EvaluationService(MockRagRunner()).compare(
        samples,
        variants=[{"top_k": 3}, {"top_k": 5}],
    )

    assert len(response.variants) == 2
    assert response.variants[0].params == {"top_k": 3}
    assert response.variants[1].params == {"top_k": 5}


@pytest.mark.anyio
async def test_single_failure_does_not_abort_batch(score_mock: AsyncMock) -> None:
    """单条运行失败不应中断整批，失败样本应标记 failed。"""

    class _FlakyRunner(MockRagRunner):
        async def run(self, question: str, **params: Any) -> Any:
            if question == "坏问题":
                raise RuntimeError("检索失败")
            return await super().run(question, **params)

    samples = [EvalSample(question="好问题"), EvalSample(question="坏问题")]

    response = await EvaluationService(_FlakyRunner()).evaluate_batch(samples)

    failed = [result for result in response.results if result.status == "failed"]
    succeeded = [result for result in response.results if result.status == "success"]
    assert len(failed) == 1
    assert len(succeeded) == 1


def test_routes_registered() -> None:
    """三个评估端点应挂载到 /api/v1/rag/evaluation 前缀下。"""
    from src.app.main import app

    paths = app.openapi().get("paths", {})
    assert "post" in paths.get("/api/v1/rag/evaluation/single", {})
    assert "post" in paths.get("/api/v1/rag/evaluation/batch", {})
    assert "post" in paths.get("/api/v1/rag/evaluation/compare", {})


@pytest.mark.anyio
async def test_mock_runner_returns_preset_trace() -> None:
    """MockRagRunner 应优先返回预置产物。"""
    trace = EvalTrace(question="什么是RAG？", answer="检索增强生成", contexts=["RAG 定义"])
    runner = MockRagRunner(traces={"什么是RAG？": trace})

    result = await runner.run("什么是RAG？")

    assert result is trace
