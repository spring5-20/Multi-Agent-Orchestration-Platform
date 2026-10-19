"""应用入口：创建 FastAPI 应用，并挂载和风天气 / 个人任务两个 MCP 服务。"""

from fastmcp.utilities.lifespan import combine_lifespans

from src.mcp_serves.qweather_mcp.server import mcp as qweather_mcp
from src.mcp_serves.task_mcp.server import mcp as task_mcp

from .api import router
from .core.config import settings
from .core.setup import create_application

app = create_application(router=router, settings=settings)

# 和风天气 MCP：端点 http://<host>:<port>/qweather_mcp/（结尾斜杠不可省略）
qweather_app = qweather_mcp.http_app(path="/", transport="streamable-http")
# 个人任务 MCP：端点 http://<host>:<port>/task-mcp/
task_app = task_mcp.http_app(path="/", transport="streamable-http")

# 合并三套 lifespan，否则 Streamable HTTP 的会话管理器不会启动
app.router.lifespan_context = combine_lifespans(
    app.router.lifespan_context,
    qweather_app.lifespan,
    task_app.lifespan,
)
app.mount("/qweather_mcp", qweather_app)
app.mount("/task-mcp", task_app)
