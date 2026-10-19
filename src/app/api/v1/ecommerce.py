"""电商导购智能体接口：带记忆的文本流式对话（SSE）。"""

import json
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from starlette.responses import StreamingResponse

from ...core.agents.ecommerce.session import new_conversation_id
from ...core.agents.ecommerce.stream import stream_chat
from ...core.deps import get_current_user_id

router = APIRouter(
    prefix="/ecommerce",
    tags=["电商导购智能体"],
)


class ChatRequest(BaseModel):
    """电商导购智能体问答请求体。"""

    conversation_id: str
    message: str


@router.post("/chat/stream", summary="带记忆的电商导购流式对话")
async def chat_stream(
    request: ChatRequest,
    user_id: int = Depends(get_current_user_id),
) -> StreamingResponse:
    """流式对话：以 SSE 逐段返回回答文本，其他流程与非流式一致。"""
    conversation_id = request.conversation_id or new_conversation_id()

    async def event_stream() -> Any:
        yield f"data: {json.dumps({'conversation_id': conversation_id}, ensure_ascii=False)}\n\n"
        async for text in stream_chat(user_id, request.message, conversation_id):
            yield f"data: {json.dumps({'text': text}, ensure_ascii=False)}\n\n"
        yield 'data: {"done": true}\n\n'

    return StreamingResponse(event_stream(), media_type="text/event-stream")
