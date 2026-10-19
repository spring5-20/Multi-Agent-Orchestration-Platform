"""Milvus 向量数据库状态接口。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from ...core.utils import milvus

router = APIRouter(prefix="/milvus", tags=["milvus"])


@router.get("/status")
async def milvus_status() -> dict[str, Any]:
    """返回 Milvus 启用与连接状态。"""
    return milvus.status()
