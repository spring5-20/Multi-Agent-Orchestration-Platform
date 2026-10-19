"""实时语音对话路由与语音页面的访问测试。"""

import pytest
from httpx import ASGITransport, AsyncClient
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from src.app.core.security import create_access_token
from src.app.main import app

VOICE_WS_PATH = "/api/v1/ecommerce/voice/realtime"


@pytest.mark.anyio
async def test_voice_page_and_assets_available() -> None:
    """语音页与它的 CSS / JS / AudioWorklet 都应可正常访问。"""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        page = await client.get("/ecommerce/voice.html")
        style = await client.get("/ecommerce/voice.css")
        script = await client.get("/ecommerce/voice.js")
        worklet = await client.get("/ecommerce/voice-worklet.js")

    assert page.status_code == 200
    assert "实时语音对话" in page.text
    assert "voice.js" in page.text
    assert style.status_code == 200 and "text/css" in style.headers["content-type"]
    assert script.status_code == 200 and "javascript" in script.headers["content-type"]
    assert worklet.status_code == 200 and "javascript" in worklet.headers["content-type"]


@pytest.mark.anyio
async def test_ecommerce_page_links_voice_page() -> None:
    """测试台导航里应能看到语音页入口。"""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        page = await client.get("/ecommerce/")

    assert "./voice.html" in page.text


def _expect_ws_close(token_suffix: str) -> int:
    """连接后在首次收数据时观察服务端关闭码（鉴权失败走 accept 后再关闭）。"""
    client = TestClient(app)
    url = VOICE_WS_PATH + token_suffix
    with pytest.raises(WebSocketDisconnect) as excinfo, client.websocket_connect(url) as socket:
        socket.receive_text()

    return excinfo.value.code


def test_voice_ws_rejects_missing_token() -> None:
    """未提供令牌时应以 4401 关闭连接，而不是放行到识别流程。"""
    assert _expect_ws_close("") == 4401


def test_voice_ws_rejects_invalid_token() -> None:
    """令牌无效（被篡改）时同样应以 4401 拒绝连接。"""
    assert _expect_ws_close("?token=not-a-token") == 4401


def test_voice_ws_delegates_to_realtime_handler(monkeypatch: pytest.MonkeyPatch) -> None:
    """令牌有效时应接受连接，并把连接交给 handle_realtime 处理。"""
    # 项目同时存在 src.app.* 与 app.* 两套导入路径，路由（src.app.api.v1.*）实际
    # 使用的是 src.app.core.* 这棵树，因此必须打桩到同一个模块。
    import src.app.core.agents.ecommerce.voice.realtime as realtime_module

    seen: dict[str, int] = {}

    async def fake_handle(websocket, user_id: int) -> None:
        seen["user_id"] = user_id
        await websocket.send_json({"type": "ready", "silence_duration_ms": 800})

    monkeypatch.setattr(realtime_module, "handle_realtime", fake_handle)
    token = create_access_token("123")

    client = TestClient(app)
    with client.websocket_connect(VOICE_WS_PATH + "?token=" + token) as socket:
        assert socket.receive_json() == {"type": "ready", "silence_duration_ms": 800}

    assert seen["user_id"] == 123
