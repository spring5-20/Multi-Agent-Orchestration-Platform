"""商品检索抽象层：定义统一接口，屏蔽商品库与 RAG 后端差异。

业务代码只依赖 ``ProductSource`` 协议，切换商品来源只需修改
``ECOMMERCE_PRODUCT_SOURCE`` 配置：``builtin``（内置目录）、``pgvector`` /
``milvus``（向量 RAG）、``hybrid``（RAG + 目录兜底）。
"""

import logging
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from ....config import settings

logger = logging.getLogger(__name__)


@dataclass
class Product:
    """一件商品。"""

    id: str
    name: str
    category: str
    description: str
    price: float
    score: float = 0.0  # 相关性得分，目录检索为关键词得分，RAG 为相似度

    def to_dict(self) -> dict[str, Any]:
        """转为 JSON 安全的字典。"""
        return {
            "id": self.id,
            "name": self.name,
            "category": self.category,
            "description": self.description,
            "price": self.price,
            "score": self.score,
        }


@runtime_checkable
class ProductSource(Protocol):
    """商品检索的统一接口。

    新来源只需实现本协议并在 :func:`build_product_source` 注册，即可无缝替换。
    """

    async def search(
        self,
        query: str,
        *,
        category: str | None = None,
        budget_min: float | None = None,
        budget_max: float | None = None,
        top_k: int = 5,
    ) -> list[Product]:
        """检索与 ``query`` 相关的商品，支持品类与价格区间过滤。"""
        ...


def build_product_source(source: str | None = None) -> ProductSource:
    """根据配置创建商品检索源，实现商品库与 RAG 的平滑切换。"""
    source = source or settings.ECOMMERCE_PRODUCT_SOURCE
    if source == "pgvector":
        from .pgvector import PgvectorProductSource

        logger.info("商品检索源：pgvector（向量 RAG）")
        return PgvectorProductSource()
    if source == "milvus":
        from .milvus import MilvusProductSource

        logger.info("商品检索源：milvus（向量 RAG）")
        return MilvusProductSource()
    if source == "hybrid":
        from .hybrid import HybridProductSource

        logger.info("商品检索源：hybrid（RAG + 内置目录兜底）")
        return HybridProductSource()

    from .builtin import BuiltinProductSource

    logger.info("商品检索源：builtin（内置目录）")
    return BuiltinProductSource()
