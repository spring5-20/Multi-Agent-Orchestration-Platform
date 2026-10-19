"""RAG 评估 API 路由：单条评估、批量评估、对比实验。"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db.database import async_get_db
from app.rag_evaluation.runner import LangGraphRagRunner
from app.rag_evaluation.schemas import (
    BatchEvalRequest,
    BatchEvalResponse,
    CompareEvalRequest,
    CompareEvalResponse,
    EvalResult,
    EvalSample,
)
from app.rag_evaluation.service import EvaluationService

router = APIRouter(prefix="/rag/evaluation", tags=["RAG 评估"])

DbSession = Annotated[AsyncSession, Depends(async_get_db)]


class SingleEvalRequest(BaseModel):
    """单条评估请求。"""

    question: str = Field(..., min_length=1, description="评估问题")
    ground_truth: str | None = Field(None, description="标准答案")
    params: dict[str, Any] = Field(default_factory=dict, description="RAG 运行参数")
    metrics: list[str] | None = Field(None, description="指标名称列表")


def _build_service(db: AsyncSession) -> EvaluationService:
    """构造评估服务，注入生产环境 RAG 运行器。"""
    return EvaluationService(LangGraphRagRunner(db))


@router.post("/single", response_model=EvalResult)
async def evaluate_single(payload: SingleEvalRequest, db: DbSession) -> EvalResult:
    """评估单条问答，输出四项指标分数。"""
    sample = EvalSample(question=payload.question, ground_truth=payload.ground_truth)
    return await _build_service(db).evaluate_single(sample, params=payload.params, metric_names=payload.metrics)


@router.post("/batch", response_model=BatchEvalResponse)
async def evaluate_batch(payload: BatchEvalRequest, db: DbSession) -> BatchEvalResponse:
    """批量评估黄金测试集。"""
    return await _build_service(db).evaluate_batch(payload.samples, params=payload.params, metric_names=payload.metrics)


@router.post("/compare", response_model=CompareEvalResponse)
async def evaluate_compare(payload: CompareEvalRequest, db: DbSession) -> CompareEvalResponse:
    """多组参数对比实验。"""
    return await _build_service(db).compare(payload.samples, payload.variants, metric_names=payload.metrics)
