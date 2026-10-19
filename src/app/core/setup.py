import logging
import sys
from collections.abc import AsyncGenerator, Callable
from contextlib import _AsyncGeneratorContextManager, asynccontextmanager
from pathlib import Path
from typing import Any

import anyio
import fastapi
import redis.asyncio as redis
from arq import create_pool
from arq.connections import RedisSettings
from fastapi import APIRouter, FastAPI
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.openapi.utils import get_openapi
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from ..middleware.client_cache_middleware import ClientCacheMiddleware
from .config import (
    AppSettings,
    ClientSideCacheSettings,
    DatabaseSettings,
    EnvironmentOption,
    EnvironmentSettings,
    RedisCacheSettings,
    RedisQueueSettings,
    RedisRateLimiterSettings,
    Settings,
    SkipInfraSettings,
    settings,
)
from .db import models  # noqa: F401  确保连接验证表等模型注册到 Base.metadata
from .db.database import Base
from .db.database import async_engine as engine
from .observability import apply_langsmith_settings, configure_observability
from .utils import cache, milvus, queue, rag_collections, rate_limit

logger = logging.getLogger(__name__)

# 前端单页应用目录（旅游建议智能体页面），由 StaticFiles 挂载到 /ui 下
WEB_DIR = Path(__file__).resolve().parents[1] / "static"
# 钉协助手智能体前端目录（信息查询 / 快捷办理两个页面），挂载到 /dingtalk 下
DINGTALK_WEB_DIR = WEB_DIR / "dingtalk"
# 电商导购智能体前端目录（带记忆的对话测试台），挂载到 /ecommerce 下
ECOMMERCE_WEB_DIR = WEB_DIR / "ecommerce"
# 多智能体编排控制台前端目录（单入口对话 + 路由轨迹），挂载到 /agents 下
AGENTS_WEB_DIR = WEB_DIR / "agents"
# 智能问数前端目录（自然语言问数 + SQL + 表格 + 审计日志），挂载到 /bi 下
BI_WEB_DIR = WEB_DIR / "bi"


# -------------- database --------------
async def create_tables() -> None:
    logger.info("正在创建/校验数据库表结构（Base.metadata.create_all）")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("数据库表结构创建/校验完成，连接验证表 connection_test 已就绪")


# -------------- cache --------------
async def create_redis_cache_pool() -> None:
    if settings.SKIP_INFRA_ON_START:
        logger.info("已跳过 Redis 缓存连接（SKIP_INFRA_ON_START=true）")
        return
    logger.info("正在连接 Redis 缓存: %s", settings.REDIS_CACHE_URL)
    cache.pool = redis.ConnectionPool.from_url(settings.REDIS_CACHE_URL)
    cache.client = redis.Redis.from_pool(cache.pool)  # type: ignore
    await cache.client.ping()
    logger.info("Redis 缓存连接成功: %s", settings.REDIS_CACHE_URL)


async def close_redis_cache_pool() -> None:
    if cache.client is not None:
        logger.info("正在关闭 Redis 缓存连接")
        await cache.client.aclose()


# -------------- queue --------------
async def create_redis_queue_pool() -> None:
    if settings.SKIP_INFRA_ON_START:
        logger.info("已跳过 Redis 队列连接（SKIP_INFRA_ON_START=true）")
        return
    logger.info("正在连接 Redis 队列: %s:%s", settings.REDIS_QUEUE_HOST, settings.REDIS_QUEUE_PORT)
    queue.pool = await create_pool(RedisSettings(host=settings.REDIS_QUEUE_HOST, port=settings.REDIS_QUEUE_PORT))
    logger.info("Redis 队列连接成功: %s:%s", settings.REDIS_QUEUE_HOST, settings.REDIS_QUEUE_PORT)


async def close_redis_queue_pool() -> None:
    if queue.pool is not None:
        logger.info("正在关闭 Redis 队列连接")
        await queue.pool.aclose()


# -------------- rate limit --------------
async def create_redis_rate_limit_pool() -> None:
    if settings.SKIP_INFRA_ON_START:
        logger.info("已跳过 Redis 限流连接（SKIP_INFRA_ON_START=true）")
        return
    logger.info("正在连接 Redis 限流: %s", settings.REDIS_RATE_LIMIT_URL)
    rate_limit.pool = redis.ConnectionPool.from_url(settings.REDIS_RATE_LIMIT_URL)
    rate_limit.client = redis.Redis.from_pool(rate_limit.pool)  # type: ignore
    await rate_limit.client.ping()
    logger.info("Redis 限流连接成功: %s", settings.REDIS_RATE_LIMIT_URL)


async def close_redis_rate_limit_pool() -> None:
    if rate_limit.client is not None:
        logger.info("正在关闭 Redis 限流连接")
        await rate_limit.client.aclose()


# -------------- application --------------
async def set_threadpool_tokens(number_of_tokens: int = 100) -> None:
    limiter = anyio.to_thread.current_default_thread_limiter()
    limiter.total_tokens = number_of_tokens


def lifespan_factory(
    settings: Settings,
    create_tables_on_start: bool = True,
) -> Callable[[FastAPI], _AsyncGeneratorContextManager[Any]]:
    """Factory to create a lifespan async context manager for a FastAPI app."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator:
        await set_threadpool_tokens()
        logging.getLogger().setLevel(settings.LOG_LEVEL.upper())
        logger.info("应用启动，环境=%s，日志级别=%s", settings.ENVIRONMENT, settings.LOG_LEVEL)

        if (
            isinstance(settings, DatabaseSettings)
            and create_tables_on_start
            and not (isinstance(settings, SkipInfraSettings) and settings.SKIP_INFRA_ON_START)
        ):
            await create_tables()

        if isinstance(settings, RedisCacheSettings):
            await create_redis_cache_pool()

        if isinstance(settings, RedisQueueSettings):
            await create_redis_queue_pool()

        if isinstance(settings, RedisRateLimiterSettings):
            await create_redis_rate_limit_pool()

        if settings.ENABLE_MILVUS:
            await anyio.to_thread.run_sync(milvus.connect)
            if settings.AUTO_CREATE_RAG_COLLECTIONS:
                client = milvus.get_client()
                if client is not None:
                    try:
                        created = await anyio.to_thread.run_sync(rag_collections.ensure_collections, client)
                        if created:
                            logger.info("本次新建 Milvus 集合: %s", created)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("Milvus RAG 集合创建失败: %s", exc)
            else:
                logger.info("已跳过 Milvus RAG 集合自动创建（AUTO_CREATE_RAG_COLLECTIONS=false）")

        yield

        if isinstance(settings, RedisCacheSettings):
            await close_redis_cache_pool()

        if isinstance(settings, RedisQueueSettings):
            await close_redis_queue_pool()

        if isinstance(settings, RedisRateLimiterSettings):
            await close_redis_rate_limit_pool()

        if settings.ENABLE_MILVUS:
            await anyio.to_thread.run_sync(milvus.close)

        # 实时语音线程池是懒加载的：只有本进程真的建过语音会话才需要关闭。
        # 这里按当前包名拼出模块名（项目同时存在 src.app.* 与 app.* 两套导入
        # 路径，写死绝对名会查不到），并只在模块已加载时才关闭，避免退出时
        # 无谓地导入识别 SDK。
        voice_realtime = sys.modules.get(f"{__package__}.agents.ecommerce.voice.realtime")
        if voice_realtime is not None:
            await voice_realtime.close_voice_pools()

        # 短期记忆使用 Postgres Checkpointer 时会常驻一个连接池，退出时一并释放；
        # 同样按当前包名查找，未使用过电商智能体时不触发导入。
        ecommerce_runtime = sys.modules.get(f"{__package__}.agents.ecommerce.runtime")
        if ecommerce_runtime is not None:
            await ecommerce_runtime.close_checkpointer()

        # 智能问数的多轮检查点同样常驻连接池，退出时释放。
        bi_runtime = sys.modules.get(f"{__package__}.agents.bi.runtime")
        if bi_runtime is not None:
            await bi_runtime.close_checkpointer()

    return lifespan


# -------------- application --------------
def create_application(
    router: APIRouter,
    settings: Settings,
    create_tables_on_start: bool = True,
    **kwargs: Any,
) -> FastAPI:
    """Creates and configures a FastAPI application based on the provided settings.

    This function initializes a FastAPI application and configures it with various settings
    and handlers based on the type of the `settings` object provided.

    Parameters
    ----------
    router : APIRouter
        The APIRouter object containing the routes to be included in the FastAPI application.

    settings
        An instance representing the settings for configuring the FastAPI application.
        It determines the configuration applied:

        - AppSettings: Configures basic app metadata like name, description, contact, and license info.
        - DatabaseSettings: Adds event handlers for initializing database tables during startup.
        - RedisCacheSettings: Sets up event handlers for creating and closing a Redis cache pool.
        - ClientSideCacheSettings: Integrates middleware for client-side caching.
        - RedisQueueSettings: Sets up event handlers for creating and closing a Redis queue pool.
        - RedisRateLimiterSettings: Sets up event handlers for creating and closing a Redis rate limiter pool.
        - EnvironmentSettings: Conditionally sets documentation URLs and integrates custom routes for API documentation
          based on the environment type.

    create_tables_on_start : bool
        A flag to indicate whether to create database tables on application startup.
        Defaults to True.

    **kwargs
        Additional keyword arguments passed directly to the FastAPI constructor.

    Returns
    -------
    FastAPI
        A fully configured FastAPI application instance.

    The function configures the FastAPI application with different features and behaviors
    based on the provided settings. It includes setting up database connections, Redis pools
    for caching, queue, and rate limiting, client-side caching, and customizing the API documentation
    based on the environment settings.
    """
    # --- before creating application ---
    # 可观测性：尽早写入 LANGSMITH_* 环境变量，之后所有 LangGraph 调用自动上报追踪
    apply_langsmith_settings(settings)
    # Metric / Log 层：写入指标窗口与成本单价（自定义 settings 对象缺失字段时跳过）
    if hasattr(settings, "OBSERVABILITY_TRACE_WINDOW"):
        configure_observability(settings)

    if isinstance(settings, AppSettings):
        to_update = {
            "title": settings.APP_NAME,
            "description": settings.APP_DESCRIPTION,
            "contact": {"name": settings.CONTACT_NAME, "email": settings.CONTACT_EMAIL},
            "license_info": {"name": settings.LICENSE_NAME},
        }
        kwargs.update(to_update)

    if isinstance(settings, EnvironmentSettings):
        kwargs.update({"docs_url": None, "redoc_url": None, "openapi_url": None})

    lifespan = lifespan_factory(settings, create_tables_on_start=create_tables_on_start)

    application = FastAPI(lifespan=lifespan, **kwargs)
    application.include_router(router)

    # 挂载前端页面：/ui/ 提供旅游建议智能体单页，根路径自动跳转过去
    if WEB_DIR.is_dir():
        application.mount("/ui", StaticFiles(directory=WEB_DIR, html=True), name="ui")

        @application.get("/", include_in_schema=False)
        async def redirect_to_web_ui() -> RedirectResponse:
            """把根路径重定向到旅游建议智能体前端页面。"""
            return RedirectResponse(url="/ui/")

    # 挂载钉协助手智能体前端：/dingtalk/ 提供「信息查询」「快捷办理」两个页面
    if DINGTALK_WEB_DIR.is_dir():
        application.mount("/dingtalk", StaticFiles(directory=DINGTALK_WEB_DIR, html=True), name="dingtalk-ui")

    # 挂载电商导购智能体前端：/ecommerce/ 提供带记忆的对话测试台
    if ECOMMERCE_WEB_DIR.is_dir():
        application.mount("/ecommerce", StaticFiles(directory=ECOMMERCE_WEB_DIR, html=True), name="ecommerce-ui")

    # 挂载多智能体编排控制台：/agents/ 提供单入口对话、路由轨迹与确认闭环
    if AGENTS_WEB_DIR.is_dir():
        application.mount("/agents", StaticFiles(directory=AGENTS_WEB_DIR, html=True), name="agents-ui")

    # 挂载智能问数：/bi/ 提供自然语言问数、SQL、结果表格与审计日志面板
    if BI_WEB_DIR.is_dir():
        application.mount("/bi", StaticFiles(directory=BI_WEB_DIR, html=True), name="bi-ui")

    if isinstance(settings, ClientSideCacheSettings):
        application.add_middleware(ClientCacheMiddleware, max_age=settings.CLIENT_CACHE_MAX_AGE)  # type: ignore[arg-type]

    if isinstance(settings, EnvironmentSettings):
        if settings.ENVIRONMENT != EnvironmentOption.PRODUCTION:
            docs_router = APIRouter()

            @docs_router.get("/docs", include_in_schema=False)
            async def get_swagger_documentation() -> fastapi.responses.HTMLResponse:
                return get_swagger_ui_html(openapi_url="/openapi.json", title="docs")

            @docs_router.get("/redoc", include_in_schema=False)
            async def get_redoc_documentation() -> fastapi.responses.HTMLResponse:
                return get_redoc_html(openapi_url="/openapi.json", title="docs")

            @docs_router.get("/openapi.json", include_in_schema=False)
            async def openapi() -> dict[str, Any]:
                out: dict = get_openapi(title=application.title, version=application.version, routes=application.routes)
                return out

            application.include_router(docs_router)

        return application

    return application
