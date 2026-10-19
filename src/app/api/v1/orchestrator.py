"""编排器 API：单入口对话 + 待确认操作的确认闭环。"""

import json
from typing import Any, Literal

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ...core.agents.orchestrator.confirmation import ConfirmationRequired, pop_pending, store_pending
from ...core.agents.orchestrator.engine import get_agent, get_agents, stream_events

router = APIRouter(prefix="/orchestrator", tags=["orchestrator"])


def _sse(payload: dict[str, Any]) -> str:
    """把事件字典编码为一行 SSE data。"""
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


class ChatRequest(BaseModel):
    question: str = Field(..., min_length=1, description="用户本轮消息")
    user_id: int = Field(0, description="用户 id（电商导购等有状态子智能体使用）")
    conversation_id: str | None = Field(None, description="会话 id；为空则新建会话")


class ConfirmRequest(BaseModel):
    confirm_id: str = Field(..., description="待确认请求的 id（来自 confirm_required 事件）")
    decision: Literal["confirm", "cancel"] = Field(..., description="确认执行或取消")


@router.get("/agents", summary="列出编排器已注册的子智能体")
async def list_agents() -> dict[str, Any]:
    """返回子智能体清单，供前端渲染侧栏卡片（避免在页面里写死子智能体名字）。"""
    return {
        "agents": [
            {
                "name": agent.name,
                "description": agent.description,
                "confirmation_required": agent.confirmation_required,
            }
            for agent in get_agents()
        ]
    }


@router.post("/chat", summary="单入口对话：自动路由到子智能体")
async def chat(req: ChatRequest) -> StreamingResponse:
    """流式对话：以 SSE 透传编排器事件（agent_call / agent_result / text / done / confirm_required）。"""

    async def event_stream() -> Any:
        context: dict[str, Any] = {"user_id": req.user_id, "conversation_id": req.conversation_id}
        try:
            async for event in stream_events(req.question, context=context):
                yield _sse(event)
            yield _sse({"type": "done"})
        except ConfirmationRequired as cr:
            confirm_id = store_pending(cr.agent_name, cr.message, cr.context)
            yield _sse(
                {"type": "confirm_required", "confirm_id": confirm_id, "agent": cr.agent_name, "prompt": cr.prompt}
            )

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@router.post("/confirm", summary="处理待确认操作：确认执行或取消")
async def confirm(req: ConfirmRequest) -> StreamingResponse:
    async def event_stream() -> Any:
        pending = pop_pending(req.confirm_id)
        if pending is None:
            yield _sse({"type": "error", "message": "确认请求不存在或已过期"})
            return

        if req.decision == "cancel":
            yield _sse({"type": "cancelled", "agent": pending.agent_name})
            return

        agent = get_agent(pending.agent_name)
        if agent is None:
            yield _sse({"type": "error", "message": f"未知子智能体：{pending.agent_name}"})
            return

        context: dict[str, Any] = {**(pending.context or {}), "confirmed": True}
        # 确认后的执行同样走 agent_call / agent_result，前端可以复用同一套轨迹与耗时统计
        yield _sse({"type": "agent_call", "agent": pending.agent_name, "message": pending.message})
        ok = True
        try:
            async for text in agent.stream(pending.message, context=context):
                yield _sse({"type": "text", "text": text})
        except Exception as exc:  # noqa: BLE001
            # 子智能体执行失败不能让 SSE 静默中断：转成错误事件，前端提示后结束本轮
            ok = False
            yield _sse({"type": "error", "message": f"执行失败：{exc}"})
        yield _sse({"type": "agent_result", "agent": pending.agent_name, "ok": ok})
        yield _sse({"type": "done"})

    return StreamingResponse(event_stream(), media_type="text/event-stream")
