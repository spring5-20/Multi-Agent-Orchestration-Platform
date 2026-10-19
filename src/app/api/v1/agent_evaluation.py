"""多 Agent 评估 API：批量评估核心指标、LLM-as-Judge 与回归测试。"""

from __future__ import annotations

from fastapi import APIRouter

from app.agent_evaluation.runner import OrchestratorAgentRunner
from app.agent_evaluation.schemas import (
    BatchEvalRequest,
    BatchEvalResponse,
    RegressionRequest,
    RegressionResponse,
)
from app.agent_evaluation.service import AgentEvaluationService

router = APIRouter(prefix="/agent/evaluation", tags=["多 Agent 评估"])


def _build_service() -> AgentEvaluationService:
    """构造评估服务，注入生产环境的多 Agent 运行器。"""
    return AgentEvaluationService(OrchestratorAgentRunner())


@router.post("/batch", response_model=BatchEvalResponse, summary="批量评估黄金测试集")
async def evaluate_batch(payload: BatchEvalRequest) -> BatchEvalResponse:
    """批量评估：完成率、工具调用准确率、冲突率、平均延迟、成本与 LLM-as-Judge。"""
    return await _build_service().evaluate_batch(
        payload.cases,
        use_judge=payload.use_judge,
        context=payload.context or None,
    )


@router.post("/regression", response_model=RegressionResponse, summary="回归测试：多组参数对比")
async def evaluate_regression(payload: RegressionRequest) -> RegressionResponse:
    """保留真实案例，变更后批量复跑并对比指标（回归测试）。"""
    return await _build_service().compare(payload.cases, payload.variants, use_judge=payload.use_judge)
