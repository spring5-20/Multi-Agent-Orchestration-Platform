"""智能问数 API：自然语言问数、流式对话、表结构与审计日志。"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ...core.agents.bi import ask, get_schema, invalidate, list_logs, log_summary

router = APIRouter(prefix="/bi", tags=["智能问数"])


def _sse(payload: dict[str, Any]) -> str:
    """把事件字典编码为一行 SSE data。"""
    return f"data: {json.dumps(payload, ensure_ascii=False, default=str)}\n\n"


class BiChatRequest(BaseModel):
    """问数请求体。"""

    question: str = Field(..., min_length=1, description="用户的自然语言数据问题")
    conversation_id: str | None = Field(None, description="会话 id；为空则新建会话")
    user_id: int = Field(0, description="用户 id（会话隔离与审计，缺省 0 表示匿名）")


class BiChatResponse(BaseModel):
    """问数响应体。"""

    answer: str = Field(..., description="自然语言结论")
    sql: str | None = Field(None, description="实际执行的只读 SQL")
    columns: list[str] = Field(default_factory=list, description="结果列名")
    rows: list[dict[str, Any]] = Field(default_factory=list, description="结果行")
    chart: dict[str, Any] | None = Field(None, description="自动推导的图表配置")
    error: str | None = Field(None, description="失败原因")
    conversation_id: str = Field(..., description="会话 id（多轮追问带上它）")


@router.post("/chat", response_model=BiChatResponse, summary="自然语言问数（非流式）")
async def chat(request: BiChatRequest) -> BiChatResponse:
    """执行一次完整问数，返回结论、SQL、结果行与图表配置。"""
    result = await ask(request.question, request.conversation_id, request.user_id)
    return BiChatResponse(
        answer=result.answer,
        sql=result.sql,
        columns=result.columns,
        rows=result.rows,
        chart=result.chart,
        error=result.error,
        conversation_id=result.conversation_id,
    )


@router.post("/chat/stream", summary="自然语言问数（流式 SSE）")
async def chat_stream(request: BiChatRequest) -> StreamingResponse:
    """流式问数：先分段返回结论，再返回会话 id、SQL、表格与图表事件。"""
    result = await ask(request.question, request.conversation_id, request.user_id)
    answer = result.answer
    step = 24  # 每次推送的字符数

    async def event_stream() -> Any:
        yield _sse({"type": "conversation_id", "conversation_id": result.conversation_id})
        for index in range(0, len(answer), step):
            yield _sse({"type": "text", "text": answer[index : index + step]})
        yield _sse({"type": "sql", "sql": result.sql})
        yield _sse({"type": "table", "columns": result.columns, "rows": result.rows})
        yield _sse({"type": "chart", "chart": result.chart})
        if result.error:
            yield _sse({"type": "error", "message": result.error})
        yield _sse({"type": "done"})

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@router.get("/schema", summary="查看可问数的表结构")
async def schema() -> dict[str, Any]:
    """返回当前内省到的表结构（已排除黑名单表与敏感列）。"""
    return {"schema": await get_schema()}


@router.post("/schema/refresh", summary="刷新表结构缓存")
async def schema_refresh() -> dict[str, str]:
    """立即失效表结构缓存，使新建表马上进入问数范围。"""
    await invalidate()
    return {"status": "ok"}


@router.get("/logs", summary="查询问数审计日志")
async def logs(
    user_id: int | None = Query(None, description="按用户过滤，缺省返回全部"),
    limit: int = Query(50, ge=1, le=200, description="返回条数"),
    offset: int = Query(0, ge=0, description="偏移量"),
) -> dict[str, Any]:
    """分页查询问数审计日志，按时间倒序。"""
    items, total = await list_logs(user_id, limit=limit, offset=offset)
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@router.get("/logs/summary", summary="问数审计日志统计总览")
async def logs_summary(
    user_id: int | None = Query(None, description="按用户过滤，缺省返回全部"),
) -> dict[str, Any]:
    """返回问数总览统计、每日趋势与高频用户（供图表化展示）。"""
    return await log_summary(user_id)
