"""电商导购智能体前端页面与对话接口的访问测试。"""

import pytest
from httpx import ASGITransport, AsyncClient

from src.app.main import app

CHAT_STREAM_PATH = "/api/v1/ecommerce/chat/stream"


@pytest.mark.anyio
async def test_ecommerce_page_and_assets_available() -> None:
    """页面与静态资源（CSS/JS）都应可正常访问。"""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        page = await client.get("/ecommerce/")
        style = await client.get("/ecommerce/styles.css")
        script = await client.get("/ecommerce/app.js")

    assert page.status_code == 200
    assert "电商导购智能体" in page.text
    assert style.status_code == 200
    assert "text/css" in style.headers["content-type"]
    assert script.status_code == 200
    assert "javascript" in script.headers["content-type"]


@pytest.mark.anyio
async def test_ecommerce_chat_stream_requires_token() -> None:
    """未携带令牌时对话接口应返回 401，而不是 500 或 404。"""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(CHAT_STREAM_PATH, json={"conversation_id": "", "message": "你好"})

    assert response.status_code == 401
    assert response.json()["detail"] == "未提供认证令牌"
