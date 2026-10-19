"""评估模块的数据模型。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class EvalSample(BaseModel):
    """一条黄金测试用例：问题 + 可选标准答案。"""

    question: str = Field(..., min_length=1, description="评估问题")
    ground_truth: str | None = Field(default=None, description="标准答案，Context Recall 等指标需要")


class EvalTrace(BaseModel):
    """一次 RAG 运行的产物。"""

    question: str = Field(..., description="原始问题")
    answer: str = Field("", description="模型回答")
    contexts: list[str] = Field(default_factory=list, description="检索到的上下文文本")
    sources: list[dict[str, Any]] = Field(default_factory=list, description="来源元信息，便于追溯")


class MetricScore(BaseModel):
    """单项指标的得分。"""

    name: str = Field(..., description="指标名称")
    score: float | None = Field(None, description="得分，范围约 0~1；None 表示因缺少输入无法计算")


class EvalResult(BaseModel):
    """单条样本的评估结果。"""

    question: str = Field(..., description="评估问题")
    answer: str = Field("", description="模型回答")
    scores: list[MetricScore] = Field(default_factory=list, description="各指标得分")
    status: str = Field("success", description="success 或 failed")
    error: str | None = Field(default=None, description="失败原因")


class BatchEvalRequest(BaseModel):
    """批量评估请求。"""

    samples: list[EvalSample] = Field(..., min_length=1, description="黄金测试集")
    params: dict[str, Any] = Field(default_factory=dict, description="RAG 运行参数，如 top_k / rerank_top_n")
    metrics: list[str] | None = Field(None, description="指标名称列表，缺省使用默认四项")


class BatchEvalResponse(BaseModel):
    """批量评估结果。"""

    results: list[EvalResult] = Field(default_factory=list, description="逐条结果")
    aggregate: dict[str, float] = Field(default_factory=dict, description="各指标的平均分")


class CompareEvalRequest(BaseModel):
    """对比实验请求：同一批问题在不同参数下评估。"""

    samples: list[EvalSample] = Field(..., min_length=1, description="黄金测试集")
    variants: list[dict[str, Any]] = Field(
        ..., min_length=1, description="多组 RAG 参数，如 [{'top_k':3},{'top_k':5}]"
    )
    metrics: list[str] | None = Field(None, description="指标名称列表")


class CompareVariantResult(BaseModel):
    """单组参数的评估结果。"""

    params: dict[str, Any] = Field(..., description="该组使用的 RAG 参数")
    aggregate: dict[str, float] = Field(default_factory=dict, description="各指标平均分")
    results: list[EvalResult] = Field(default_factory=list, description="逐条结果")


class CompareEvalResponse(BaseModel):
    """对比实验结果。"""

    variants: list[CompareVariantResult] = Field(default_factory=list, description="各组结果")
