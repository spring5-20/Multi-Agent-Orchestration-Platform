"""前端页面（/ui）访问测试。"""

import pytest
from httpx import ASGITransport, AsyncClient

from src.app.main import app


@pytest.mark.anyio
async def test_root_redirects_to_ui() -> None:
    """访问根路径应 307 重定向到 /ui/。"""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/", follow_redirects=False)

    assert response.status_code in (307, 308)
    assert response.headers["location"] == "/ui/"


@pytest.mark.anyio
async def test_ui_page_and_assets_available() -> None:
    """页面与静态资源（CSS/JS）都应可正常访问。"""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        page = await client.get("/ui/")
        style = await client.get("/ui/styles.css")
        script = await client.get("/ui/app.js")

    assert page.status_code == 200
    assert "旅游建议智能体" in page.text
    assert style.status_code == 200
    assert "text/css" in style.headers["content-type"]
    assert script.status_code == 200
    assert "javascript" in script.headers["content-type"]
