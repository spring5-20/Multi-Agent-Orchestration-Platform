"""多 Agent 评估的数据模型：用例、轨迹、指标结果与批量 / 回归响应。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class AgentEvalCase(BaseModel):
    """一条黄金测试用例（问题 + 期望的子智能体 + 可选标准答案）。"""

    question: str = Field(..., min_length=1, description="评估问题")
    expected_agents: list[str] = Field(
        default_factory=list,
        description="期望被调用的子智能体；为空表示该用例不做工具调用对照",
    )
    ground_truth: str | None = Field(default=None, description="标准答案，供 LLM-as-Judge 对照")
    expected_keywords: list[str] = Field(
        default_factory=list, description="答案里必须出现的关键词，用于确定性完成度判定"
    )


class AgentStep(BaseModel):
    """轨迹里的一步：一次子智能体调用。"""

    agent: str = Field(..., description="子智能体名称")
    ok: bool = Field(default=True, description="该步是否成功")
    latency: float | None = Field(default=None, description="该步耗时（秒）；不可得时为 None")
    message: str = Field(default="", description="转发给该子智能体的消息")


class AgentTrajectory(BaseModel):
    """一次多 Agent 运行的完整产物。"""

    question: str = Field(..., description="原始问题")
    answer: str = Field(default="", description="最终回答")
    steps: list[AgentStep] = Field(default_factory=list, description="子智能体调用轨迹")
    latency: float = Field(default=0.0, description="端到端耗时（秒）")
    input_tokens: int = Field(default=0, description="输入 Token 数")
    output_tokens: int = Field(default=0, description="输出 Token 数")
    error: str | None = Field(default=None, description="运行失败原因")


class JudgeScore(BaseModel):
    """LLM-as-Judge 的评分结果。"""

    score: float | None = Field(default=None, description="0~1 的分数；None 表示未能评分")
    reason: str = Field(default="", description="评分理由")
    model: str = Field(default="", description="裁判模型名")


class AgentEvalResult(BaseModel):
    """单条用例的评估结果。"""

    question: str = Field(..., description="评估问题")
    answer: str = Field(default="", description="模型回答")
    status: str = Field(default="success", description="success 或 failed")
    error: str | None = Field(default=None, description="失败原因")
    completed: bool = Field(default=False, description="该用例是否判定为完成")
    tool_calls: list[str] = Field(default_factory=list, description="实际调用过的子智能体")
    tool_accuracy: float | None = Field(default=None, description="工具调用准确率；无期望子智能体时为 None")
    conflict: bool = Field(default=False, description="是否发生冲突（子智能体失败或重复派单）")
    latency: float = Field(default=0.0, description="端到端耗时（秒）")
    input_tokens: int = Field(default=0, description="输入 Token 数")
    output_tokens: int = Field(default=0, description="输出 Token 数")
    cost: float = Field(default=0.0, description="估算成本（元）")
    keywords_hit: float | None = Field(default=None, description="关键词命中率；未配置关键词时为 None")
    judge: JudgeScore | None = Field(default=None, description="LLM-as-Judge 评分")


class AgentEvaluationSummary(BaseModel):
    """一批用例的汇总指标（课件里的多 Agent 核心评估指标）。"""

    total: int = Field(default=0, description="用例数")
    completion_rate: float = Field(default=0.0, description="任务完成率")
    tool_call_accuracy: float | None = Field(default=None, description="工具调用准确率；全部用例都无期望时为 None")
    conflict_rate: float = Field(default=0.0, description="冲突率")
    avg_latency: float = Field(default=0.0, description="平均延迟（秒）")
    avg_cost: float = Field(default=0.0, description="平均成本（元）")
    judge_score: float | None = Field(default=None, description="LLM-as-Judge 平均分")
    passed: dict[str, bool] = Field(default_factory=dict, description="各指标是否达标")


class BatchEvalRequest(BaseModel):
    """批量评估请求。"""

    cases: list[AgentEvalCase] | None = Field(default=None, description="黄金测试集；缺省使用内置用例")
    use_judge: bool | None = Field(default=None, description="是否启用 LLM-as-Judge；缺省读配置")
    context: dict[str, Any] = Field(default_factory=dict, description="透传给编排器的上下文")


class BatchEvalResponse(BaseModel):
    """批量评估响应。"""

    results: list[AgentEvalResult] = Field(default_factory=list, description="逐条结果")
    summary: AgentEvaluationSummary = Field(default_factory=AgentEvaluationSummary, description="汇总指标")


class RegressionRequest(BaseModel):
    """回归测试请求：同一批用例在多组参数下复跑并对比指标。"""

    cases: list[AgentEvalCase] | None = Field(default=None, description="黄金测试集；缺省使用内置用例")
    variants: list[dict[str, Any]] = Field(
        ...,
        min_length=1,
        description="多组参数，例如 [{'label':'baseline'},{'label':'candidate','context':{...}}]",
    )
    use_judge: bool | None = Field(default=None, description="是否启用 LLM-as-Judge；缺省读配置")


class RegressionVariant(BaseModel):
    """一组参数的回归结果。"""

    label: str = Field(..., description="变体名称")
    params: dict[str, Any] = Field(default_factory=dict, description="该组使用的参数")
    summary: AgentEvaluationSummary = Field(default_factory=AgentEvaluationSummary, description="汇总指标")
    results: list[AgentEvalResult] = Field(default_factory=list, description="逐条结果")


class RegressionResponse(BaseModel):
    """回归测试响应：各变体指标 + 相对基线的差值。"""

    variants: list[RegressionVariant] = Field(default_factory=list, description="各变体结果")
    deltas: dict[str, float] = Field(default_factory=dict, description="各变体相对第一组（基线）的指标差值")
