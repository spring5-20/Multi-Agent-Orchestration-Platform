import logging
from collections.abc import AsyncGenerator
from typing import Any

from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.ext.asyncio.session import AsyncSession
from sqlalchemy.orm import DeclarativeBase, MappedAsDataclass, sessionmaker

from ..config import settings

logger = logging.getLogger(__name__)


class Base(DeclarativeBase, MappedAsDataclass):
    pass


DATABASE_URI = settings.POSTGRES_URI
DATABASE_PREFIX = settings.POSTGRES_ASYNC_PREFIX
DATABASE_URL = f"{DATABASE_PREFIX}{DATABASE_URI}"

logger.info("初始化异步数据库引擎: %s:%s/%s", settings.POSTGRES_SERVER, settings.POSTGRES_PORT, settings.POSTGRES_DB)

async_engine = create_async_engine(
    DATABASE_URL,
    echo=settings.DB_ECHO,
    future=True,
    # 每次从连接池取连接前先探测连接是否存活，避免复用到已被服务端回收的空闲连接
    pool_pre_ping=True,
    # 连接最多存活 1 小时后主动回收，降低被远程主机/防火墙中途断开连接的概率
    pool_recycle=3600,
)

local_session = sessionmaker(bind=async_engine, class_=AsyncSession, expire_on_commit=False)  # type: ignore[call-overload]


async def async_get_db() -> AsyncGenerator[Any, Any]:
    """创建并管理异步数据库会话的依赖，请求结束后自动关闭会话。"""
    logger.debug("创建数据库会话")
    async with local_session() as db:
        yield db
    logger.debug("关闭数据库会话")
