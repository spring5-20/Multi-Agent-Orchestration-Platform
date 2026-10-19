"""规划反思 API 路由：Reflection 写作与失败自动重规划的任务执行。"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.core.agents.reflection import run_reflection, run_task_with_reflexion, run_task_with_replanning

router = APIRouter(prefix="/reflection", tags=["规划与反思"])


class WriteRequest(BaseModel):
    """Reflection 写作请求。"""

    task: str = Field(..., min_length=1, description="写作任务，例如：写一篇介绍春天的短文")


class WriteResponse(BaseModel):
    """Reflection 写作结果。"""

    final: str = Field(..., description="生成 → 批评 → 修订后的最终文章")


class ExecuteRequest(BaseModel):
    """任务执行请求（失败自动重规划）。"""

    task: str = Field(..., min_length=1, description="要执行的任务")


class ExecuteResponse(BaseModel):
    """任务执行结果。"""

    status: str = Field(..., description="success 或 failed")
    plan: str = Field("", description="最终使用的计划")
    result: str = Field("", description="执行结果")
    error: str = Field("", description="最后一次失败原因")
    retry_count: int = Field(0, description="重规划（重试）次数")


class ReflexionResponse(ExecuteResponse):
    """带反思的任务执行结果。"""

    reflections: list[str] = Field(default_factory=list, description="每次失败后生成的文字反思（按时间顺序）")


@router.post("/write", response_model=WriteResponse)
async def write_with_reflection(payload: WriteRequest) -> WriteResponse:
    """Reflection 写作：生成初稿 → 批评 → 修订，返回最终稿。"""
    final = await asyncio.to_thread(run_reflection, payload.task)
    return WriteResponse(final=final)


@router.post("/execute", response_model=ExecuteResponse)
async def execute_with_replanning(payload: ExecuteRequest) -> ExecuteResponse:
    """执行任务：失败后自动重新规划并重试，最多重试 3 次。"""
    state = await asyncio.to_thread(run_task_with_replanning, payload.task)
    return ExecuteResponse(
        status=state.get("status", "failed"),
        plan=state.get("plan", ""),
        result=state.get("result", ""),
        error=state.get("error", ""),
        retry_count=state.get("retry_count", 0),
    )


@router.post("/reflexion", response_model=ReflexionResponse)
async def execute_with_reflexion(payload: ExecuteRequest) -> ReflexionResponse:
    """执行任务：失败后先生成文字反思，再带着反思重规划，最多重试 3 次。"""
    state = await asyncio.to_thread(run_task_with_reflexion, payload.task)
    return ReflexionResponse(
        status=state.get("status", "failed"),
        plan=state.get("plan", ""),
        result=state.get("result", ""),
        error=state.get("error", ""),
        retry_count=state.get("retry_count", 0),
        reflections=list(state.get("reflections", [])),
    )
