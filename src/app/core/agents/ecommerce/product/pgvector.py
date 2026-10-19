"""pgvector 商品 RAG 检索：对 ``products`` 表做向量相似度搜索。

依赖 ``products`` 表已灌入商品与 embedding（可运行 ``seed_products`` 脚本完成），
数据库需启用 pgvector 扩展。
"""

import logging

import anyio
from sqlalchemy import cast, select
from sqlalchemy.sql import func

from ....config import settings
from ....db.database import local_session
from ....db.models import Product as ProductModel
from ....db.models import Vector
from ....embeddings import get_text_embedding
from .base import Product, ProductSource

logger = logging.getLogger(__name__)


class PgvectorProductSource(ProductSource):
    """基于 pgvector 的商品向量检索。"""

    async def search(
        self,
        query: str,
        *,
        category: str | None = None,
        budget_min: float | None = None,
        budget_max: float | None = None,
        top_k: int = 5,
    ) -> list[Product]:
        query_embedding = await anyio.to_thread.run_sync(get_text_embedding, query, 1536)
        query_vector = cast(query_embedding, Vector(1536))
        similarity = 1 - func.cosine_distance(ProductModel.embedding, query_vector)
        stmt = (
            select(
                ProductModel.id,
                ProductModel.name,
                ProductModel.category,
                ProductModel.description,
                ProductModel.price,
                similarity.label("similarity"),
            )
            .where(ProductModel.embedding.is_not(None))
            .where(similarity >= settings.ECOMMERCE_PRODUCT_MIN_SIMILARITY)
            .order_by(similarity.desc())
            .limit(top_k)
        )
        if category:
            stmt = stmt.where(ProductModel.category == category)
        if budget_min is not None:
            stmt = stmt.where(ProductModel.price >= budget_min)
        if budget_max is not None:
            stmt = stmt.where(ProductModel.price <= budget_max)

        async with local_session() as session:
            result = await session.execute(stmt)
            rows = result.all()

        return [
            Product(
                id=str(row.id),
                name=row.name,
                category=row.category,
                description=row.description,
                price=float(row.price),
                score=round(float(row.similarity), 4),
            )
            for row in rows
        ]
