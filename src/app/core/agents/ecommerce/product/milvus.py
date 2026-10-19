"""Milvus 商品 RAG 检索：向量语义搜索 + 标量过滤。

使用专用集合 ``ecommerce_products``，提供幂等的集合创建与灌库函数。
Milvus 未启用或集合不存在时返回空列表，由调用方（或 hybrid 模式）决定是否回退。
"""

import logging
from typing import Any

import anyio
from pymilvus import CollectionSchema, DataType, FieldSchema, MilvusClient
from pymilvus.milvus_client.index import IndexParams

from ....embeddings import get_text_embedding
from ....utils import milvus as milvus_client
from .base import Product, ProductSource

logger = logging.getLogger(__name__)

COLLECTION = "ecommerce_products"
EMBEDDING_DIM = 1536
HNSW_M = 16
HNSW_EF_CONSTRUCTION = 64


def _product_schema() -> CollectionSchema:
    """构建商品集合的字段模式。"""
    return CollectionSchema(
        fields=[
            FieldSchema("id", DataType.VARCHAR, max_length=64, is_primary=True, description="商品编号"),
            FieldSchema("name", DataType.VARCHAR, max_length=255, description="商品名称"),
            FieldSchema("category", DataType.VARCHAR, max_length=100, description="品类"),
            FieldSchema("description", DataType.VARCHAR, max_length=2048, description="商品描述"),
            FieldSchema("price", DataType.FLOAT, description="价格（元）"),
            FieldSchema("embedding", DataType.FLOAT_VECTOR, dim=EMBEDDING_DIM, description="名称+品类+描述的向量"),
        ],
        description="电商导购商品向量库",
        enable_dynamic_field=True,
    )


def _product_index_params() -> IndexParams:
    """构建商品集合的索引：embedding 用 HNSW+COSINE，category 用倒排索引。"""
    index_params = IndexParams()
    index_params.add_index(
        field_name="embedding",
        index_type="HNSW",
        metric_type="COSINE",
        params={"M": HNSW_M, "efConstruction": HNSW_EF_CONSTRUCTION},
    )
    index_params.add_index(field_name="category", index_type="INVERTED")
    return index_params


def ensure_product_collection(client: MilvusClient) -> bool:
    """确保商品集合与索引存在，幂等。返回本次是否新建。"""
    if client.has_collection(COLLECTION):
        logger.info("Milvus 集合 %s 已存在，跳过创建", COLLECTION)
        return False
    client.create_collection(
        collection_name=COLLECTION,
        schema=_product_schema(),
        index_params=_product_index_params(),
    )
    logger.info("Milvus 集合 %s 创建完成（含索引）", COLLECTION)
    return True


def seed_products(client: MilvusClient, catalog: list[tuple[str, str, str, str, float]]) -> int:
    """把商品目录向量化后插入 Milvus，返回插入条数。"""
    data: list[dict[str, Any]] = []
    for product_id, name, category, description, price in catalog:
        embedding = get_text_embedding(f"{name} {category} {description}", dimension=EMBEDDING_DIM)
        data.append(
            {
                "id": product_id,
                "name": name,
                "category": category,
                "description": description,
                "price": price,
                "embedding": embedding,
            }
        )
    client.insert(collection_name=COLLECTION, data=data)
    return len(data)


class MilvusProductSource(ProductSource):
    """基于 Milvus 的商品向量检索。"""

    async def search(
        self,
        query: str,
        *,
        category: str | None = None,
        budget_min: float | None = None,
        budget_max: float | None = None,
        top_k: int = 5,
    ) -> list[Product]:
        client = milvus_client.get_client()
        if client is None:
            logger.warning("Milvus 未启用或连接失败，商品 RAG 检索返回空")
            return []
        if not client.has_collection(COLLECTION):
            logger.warning("Milvus 集合 %s 不存在，商品 RAG 检索返回空", COLLECTION)
            return []

        query_embedding = await anyio.to_thread.run_sync(get_text_embedding, query, EMBEDDING_DIM)

        conditions: list[str] = []
        if category:
            conditions.append(f'category == "{category}"')
        if budget_min is not None:
            conditions.append(f"price >= {budget_min}")
        if budget_max is not None:
            conditions.append(f"price <= {budget_max}")
        filter_expr = " and ".join(conditions) if conditions else None

        resp = client.search(
            collection_name=COLLECTION,
            data=[query_embedding],
            limit=top_k,
            filter=filter_expr,
            output_fields=["id", "name", "category", "description", "price"],
        )

        products: list[Product] = []
        for hit in resp[0] if resp else []:
            entity = hit.get("entity", {})
            products.append(
                Product(
                    id=str(entity.get("id", "")),
                    name=str(entity.get("name", "")),
                    category=str(entity.get("category", "")),
                    description=str(entity.get("description", "")),
                    price=float(entity.get("price", 0.0)),
                    score=round(float(hit.get("distance", 0.0)), 4),
                )
            )
        return products
