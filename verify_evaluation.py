# verify_evaluation.py
"""真实端到端验证：用 MockRagRunner 提供样本，跑真实 RAGAS 打分。"""

import asyncio

from app.rag_evaluation.runner import MockRagRunner
from app.rag_evaluation.schemas import EvalSample, EvalTrace
from app.rag_evaluation.service import EvaluationService


async def main() -> None:
    runner = MockRagRunner(
        traces={
            "什么是RAG？": EvalTrace(
                question="什么是RAG？",
                answer="RAG是检索增强生成，让大模型在回答前先检索外部知识。",
                contexts=["RAG是检索增强生成（Retrieval-Augmented Generation），用于让大模型访问外部知识。"],
            )
        }
    )
    service = EvaluationService(runner)
    result = await service.evaluate_single(
        EvalSample(
            question="什么是RAG？",
            ground_truth="RAG是检索增强生成，让大模型在回答前先检索外部知识。",
        )
    )
    print("status:", result.status)
    print("error:", result.error)
    for score in result.scores:
        print(f"{score.name}: {score.score}")


if __name__ == "__main__":
    asyncio.run(main())
