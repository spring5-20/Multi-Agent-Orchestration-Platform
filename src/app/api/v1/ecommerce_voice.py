"""电商导购智能体的实时语音 WebSocket 路由。

浏览器无法为 WebSocket 设置自定义请求头，因此访问令牌通过查询参数传入：
``/api/v1/ecommerce/voice/realtime?token=<access_token>``。握手后先鉴权，
鉴权失败以 4401 关闭连接；通过后交给 ``handle_realtime``，由它完成
「识别 → 对话 → 语音合成回推」的完整链路。
"""

import logging

from fastapi import APIRouter, WebSocket
from starlette.websockets import WebSocketDisconnect

from ...core.deps import resolve_user_id

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ecommerce/voice", tags=["电商导购智能体"])

# 自定义关闭码：与 HTTP 401 对齐，前端据此提示「重新登录」
_AUTH_FAILED_CODE = 4401


@router.websocket("/realtime")
async def voice_realtime_ws(websocket: WebSocket) -> None:
    """实时语音对话：持续接收 PCM 音频流，实时转写并按句子回复。"""
    await websocket.accept()

    user_id = resolve_user_id(websocket.query_params.get("token"))
    if user_id is None:
        await websocket.close(code=_AUTH_FAILED_CODE, reason="未认证：令牌缺失或已失效")
        return

    # 延迟导入：识别 SDK 只在真正建立语音会话时才加载，避免拖慢应用启动
    from ...core.agents.ecommerce.voice.realtime import handle_realtime

    try:
        await handle_realtime(websocket, user_id)
    except WebSocketDisconnect:
        logger.info("实时语音客户端已断开")
    except Exception as exc:  # noqa: BLE001
        logger.warning("实时语音连接异常结束: %s", exc)
