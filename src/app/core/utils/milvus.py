"""Milvus 向量数据库连接管理模块。

通过 ``ENABLE_MILVUS`` 开关控制是否启用，默认关闭。
未启用时不会发起任何连接，避免未部署 Milvus 时启动或调用报错。
"""

from __future__ import annotations

import logging
from typing import Any

from pymilvus import MilvusClient

from ..config import settings

logger = logging.getLogger(__name__)

_client: MilvusClient | None = None


def _build_uri() -> str:
    """构造 Milvus 连接地址，优先使用显式 URI，否则用 host + port。"""
    if settings.MILVUS_URI:
        return settings.MILVUS_URI
    return f"http://{settings.MILVUS_HOST}:{settings.MILVUS_PORT}"


def is_enabled() -> bool:
    """返回 Milvus 是否已启用。"""
    return settings.ENABLE_MILVUS


def connect() -> bool:
    """连接 Milvus。

    未启用时直接返回 False；连接失败时记录告警并返回 False，不抛出异常。
    """
    global _client
    if not settings.ENABLE_MILVUS:
        logger.info("Milvus 未启用（ENABLE_MILVUS=false），跳过连接")
        return False
    if _client is not None:
        return True

    uri = _build_uri()
    try:
        _client = MilvusClient(
            uri=uri,
            token=settings.MILVUS_TOKEN,
            user=settings.MILVUS_USER,
            password=settings.MILVUS_PASSWORD,
            db_name=settings.MILVUS_DB_NAME,
        )
        _client.list_collections()  # 触发一次真实请求，确认连接可用
        logger.info("Milvus 连接成功: %s", uri)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("Milvus 连接失败: %s", exc)
        _client = None
        return False


def close() -> None:
    """关闭 Milvus 连接。"""
    global _client
    if _client is not None:
        _client.close()
        _client = None
        logger.info("Milvus 连接已关闭")


def get_client() -> MilvusClient | None:
    """获取 Milvus 客户端；未启用或连接失败时返回 None。"""
    if not settings.ENABLE_MILVUS:
        return None
    if _client is None:
        connect()
    return _client


def status() -> dict[str, Any]:
    """返回 Milvus 启用与连接状态，供接口或日志使用。"""
    return {
        "enabled": settings.ENABLE_MILVUS,
        "connected": _client is not None,
        "uri": _build_uri(),
        "db_name": settings.MILVUS_DB_NAME,
    }
