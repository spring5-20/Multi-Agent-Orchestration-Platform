"""多 Agent 评估模块：规则指标 + LLM-as-Judge + 回归测试。

评估层只依赖 AgentRunner 协议，不侵入编排器实现；生产环境用
OrchestratorAgentRunner 跑真实 supervisor 图，测试用 MockAgentRunner。
"""

from .golden import DEFAULT_GOLDEN_CASES
from .metrics import evaluate_trajectory, summarize, summary_deltas
from .runner import AgentRunner, MockAgentRunner, OrchestratorAgentRunner
from .schemas import (
    AgentEvalCase,
    AgentEvalResult,
    AgentEvaluationSummary,
    AgentStep,
    AgentTrajectory,
    BatchEvalResponse,
    RegressionResponse,
    RegressionVariant,
)
from .service import AgentEvaluationService

__all__ = [
    "DEFAULT_GOLDEN_CASES",
    "AgentEvalCase",
    "AgentEvalResult",
    "AgentEvaluationService",
    "AgentEvaluationSummary",
    "AgentRunner",
    "AgentStep",
    "AgentTrajectory",
    "BatchEvalResponse",
    "MockAgentRunner",
    "OrchestratorAgentRunner",
    "RegressionResponse",
    "RegressionVariant",
    "evaluate_trajectory",
    "summarize",
    "summary_deltas",
]
