"""智能问数的图状态定义。"""

from __future__ import annotations

from typing import Annotated, Any, TypedDict

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages


class BiState(TypedDict, total=False):
    """图节点间共享的状态：单轮「问题 → SQL → 结果 → 结论」全链路数据。

    messages 使用 add_messages reducer，由 Checkpointer 跨请求累积，
    是多轮指代（「那上周呢」）的基础；其余字段每轮由本次输入显式重置。
    """

    messages: Annotated[list[BaseMessage], add_messages]
    question: str
    # 注入大模型的表结构文本（由 schema 节点内省生成）
    schema_text: str
    # 当前轮生成的 SQL；失败自修正时会被覆盖
    sql: str | None
    # 上一次生成 / 执行失败的原因，回传给 generate 节点用于自修正
    feedback: str | None
    # 已尝试生成 SQL 的次数（用于限制重试）
    attempt: int
    # 执行成功后的列名与结果行
    columns: list[str]
    rows: list[dict[str, Any]]
    # 不可恢复的错误（守卫失败或执行失败且重试耗尽）
    error: str | None
    # 最终自然语言结论
    answer: str
    # 可选图表配置（前端按 x_axis / series 渲染）
    chart: dict[str, Any] | None
