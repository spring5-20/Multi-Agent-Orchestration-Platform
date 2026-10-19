"""钉协助手智能体前端（/dingtalk）与路由注册测试。

覆盖点：
1. 两个页面（信息查询 / 快捷办理）与静态资源可正常访问；
2. 顶部菜单可以在两个页面之间互相切换；
3. 页面里的表单声明覆盖了 ``/api/v1/dingtalk`` 下的全部路由与智能体对话；
4. 后端路由全部注册到 ``/api/v1/dingtalk`` 下。
"""

import pytest
from httpx import ASGITransport, AsyncClient, Response

from src.app.main import app

# /api/v1/dingtalk 下的全部路由：(相对路径, 方法)
READ_ROUTES = (
    ("/health", "GET"),
    ("/auth/status", "GET"),
    ("/auth/login", "POST"),
    ("/contact/search", "GET"),
    ("/calendar/list", "GET"),
    ("/todo/list", "GET"),
    ("/log/list", "GET"),
    ("/attendance", "GET"),
    ("/group/search", "GET"),
)

WRITE_ROUTES = (
    ("/calendar", "POST"),
    ("/todo", "POST"),
    ("/todo/assign", "POST"),
    ("/message/send", "POST"),
    ("/group-message/send", "POST"),
    ("/ding/send", "POST"),
    ("/sheet", "POST"),
    ("/log", "POST"),
    ("/notice", "POST"),
    ("/robot-message/send", "POST"),
)

# 前端表单覆盖的路径：/health 与 /auth/status 由顶部状态徽标调用（common.js 内）
QUERY_PAGE_PATHS = tuple(path for path, _ in READ_ROUTES if path not in ("/health", "/auth/status"))
OPERATION_PAGE_PATHS = tuple(path for path, _ in WRITE_ROUTES)


async def fetch(*paths: str) -> list[Response]:
    """一次性请求多个路径，返回响应列表（顺序与入参一致）。"""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return [await client.get(path) for path in paths]


@pytest.mark.anyio
async def test_pages_and_assets_available() -> None:
    """两个页面与 CSS/JS 资源都应返回 200，且页面标题为「钉协助手智能体」。"""
    query_page, operations_page, style, chat_style, common, query_script, operations_script = await fetch(
        "/dingtalk/",
        "/dingtalk/operations.html",
        "/dingtalk/styles.css",
        "/dingtalk/chat.css",
        "/dingtalk/common.js",
        "/dingtalk/query.js",
        "/dingtalk/operations.js",
    )

    assert query_page.status_code == 200
    assert operations_page.status_code == 200
    assert "钉协助手智能体" in query_page.text
    assert "钉协助手智能体" in operations_page.text

    for style_response in (style, chat_style):
        assert style_response.status_code == 200
        assert "text/css" in style_response.headers["content-type"]
    for script in (common, query_script, operations_script):
        assert script.status_code == 200
        assert "javascript" in script.headers["content-type"]


@pytest.mark.anyio
async def test_menu_switches_between_pages() -> None:
    """两个页面都要提供指向对方的菜单入口，实现菜单切换。"""
    query_page, operations_page = await fetch("/dingtalk/", "/dingtalk/operations.html")

    assert "./operations.html" in query_page.text
    assert "./index.html" in operations_page.text
    assert 'data-page="query"' in query_page.text
    assert 'data-page="operations"' in operations_page.text


@pytest.mark.anyio
async def test_pages_cover_all_dingtalk_routes() -> None:
    """钉钉路由必须全部落到两个页面上，避免新增路由后漏做界面。"""
    query_page, operations_page, query_script, common = await fetch(
        "/dingtalk/",
        "/dingtalk/operations.html",
        "/dingtalk/query.js",
        "/dingtalk/common.js",
    )

    for path in QUERY_PAGE_PATHS:
        assert f'data-path="{path}"' in query_page.text, path
    for path in OPERATION_PAGE_PATHS:
        assert f'data-path="{path}"' in operations_page.text, path

    # 状态徽标调用健康检查与登录状态，智能体对话由 query.js 调用
    assert '"/health"' in common.text
    assert '"/auth/status"' in common.text
    assert "${API_BASE}/chat" in query_script.text
    assert "和钉协助手对话" in query_page.text


def test_dingtalk_routes_registered() -> None:
    """后端路由必须完整注册到 /api/v1/dingtalk 下（router 重复定义会静默丢路由）。"""
    schema_paths = app.openapi().get("paths", {})

    for path, method in READ_ROUTES + WRITE_ROUTES:
        operations = schema_paths.get(f"/api/v1/dingtalk{path}", {})
        assert method.lower() in operations, path
    assert "post" in schema_paths.get("/api/v1/dingtalk/chat", {})
