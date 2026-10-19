"""RAG 向量检索所需的 Milvus Collection 定义与自动创建模块（单集合版）。

将原 ``rag_documents`` 与 ``rag_chunks`` 合并为一个 ``rag_chunks`` 集合：

- 每个 chunk 是一条 Milvus 实体，主键为 chunk 的 ``id``。
- 文档级元信息（title/source_type/source_path/author/doc_created_at/doc_updated_at）
  冗余到每个 chunk 上，便于向量检索后直接返回与过滤。
- 不再单独存储“没有 chunk 的文档”元信息。

兼容 Milvus 2.5.14：
- 该版本不支持 ``TIMESTAMPTZ``（DataType 26），因此所有时间字段使用
  ``VARCHAR(64)`` 存储 ISO 8601 字符串。
- 该版本 ``STL_SORT`` 仅支持数值字段，因此 VARCHAR 时间字段统一使用
  ``INVERTED`` 倒排索引。

集合在应用启动、Milvus 可用时自动创建（幂等：已存在则跳过），
并同时建立向量检索索引（HNSW + COSINE）与标量过滤索引。
"""

from __future__ import annotations

import logging

from pymilvus import CollectionSchema, DataType, FieldSchema, MilvusClient
from pymilvus.milvus_client.index import IndexParams

logger = logging.getLogger(__name__)

# 向量维度，需与 embedding 模型的输出维度保持一致
EMBEDDING_DIM = 1536

# 合并后的集合名称。
# 注意：若旧的 rag_chunks 已存在（含 TIMESTAMPTZ 字段），本模块会因幂等跳过而不会
# 自动迁移 schema。请先删除旧集合，或将 RAG_COLLECTION 改为新名称（如 rag_embeddings）。
RAG_COLLECTION = "rag_chunks"

# HNSW 向量索引参数
HNSW_M = 16
HNSW_EF_CONSTRUCTION = 64

# 时间字段统一使用的 ISO 8601 字符串最大长度
DATETIME_MAX_LENGTH = 64


def _rag_schema() -> CollectionSchema:
    """构建合并后的 ``rag_chunks`` 集合字段模式。

    主键为 chunk 的 ``id``；``document_id`` 指向原文档主键。
    文档级字段直接冗余在 chunk 实体中，避免二次查询。

    时间字段使用 ``VARCHAR(64)`` 存储 ISO 8601 字符串，以兼容 Milvus 2.5.14。
    """
    return CollectionSchema(
        fields=[
            # chunk 主键，对应 PostgreSQL 的 rag_chunks.id
            FieldSchema(
                "id",
                DataType.INT64,
                is_primary=True,
                description="切块主键，对应 PostgreSQL 的 rag_chunks.id",
            ),
            # 所属文档
            FieldSchema(
                "document_id",
                DataType.INT64,
                description="所属文档主键，对应 rag_documents.id",
            ),
            FieldSchema(
                "chunk_index",
                DataType.INT32,
                description="块在文档中的顺序",
            ),
            FieldSchema(
                "content",
                DataType.VARCHAR,
                max_length=65535,
                description="块的原始文本",
            ),
            FieldSchema(
                "embedding",
                DataType.FLOAT_VECTOR,
                dim=EMBEDDING_DIM,
                description="文本的向量表示",
            ),
            FieldSchema(
                "token_count",
                DataType.INT32,
                description="粗略的 token 数量",
            ),
            FieldSchema(
                "section_title",
                DataType.VARCHAR,
                max_length=1024,
                description="所属章节标题",
            ),
            FieldSchema(
                "content_type",
                DataType.VARCHAR,
                max_length=50,
                description="内容类型，如 paragraph/code/table",
            ),
            # 以下为原 rag_documents 的文档级元信息，冗余到每个 chunk
            FieldSchema(
                "title",
                DataType.VARCHAR,
                max_length=1024,
                description="文档标题",
            ),
            FieldSchema(
                "source_type",
                DataType.VARCHAR,
                max_length=50,
                description="来源类型，如 pdf/docx/url/txt",
            ),
            FieldSchema(
                "source_path",
                DataType.VARCHAR,
                max_length=2048,
                description="文件路径或 URL",
            ),
            FieldSchema(
                "author",
                DataType.VARCHAR,
                max_length=256,
                description="作者",
            ),
            # 时间字段：Milvus 2.5.14 不支持 TIMESTAMPTZ，使用 ISO 8601 字符串
            FieldSchema(
                "doc_created_at",
                DataType.VARCHAR,
                max_length=DATETIME_MAX_LENGTH,
                description="文档创建时间（ISO 8601 字符串）",
            ),
            FieldSchema(
                "doc_updated_at",
                DataType.VARCHAR,
                max_length=DATETIME_MAX_LENGTH,
                description="文档更新时间（ISO 8601 字符串）",
            ),
            FieldSchema(
                "created_at",
                DataType.VARCHAR,
                max_length=DATETIME_MAX_LENGTH,
                description="切块创建时间（ISO 8601 字符串）",
            ),
            FieldSchema(
                "metadata",
                DataType.JSON,
                description="扩展元数据，写入时可合并文档与切块的 metadata",
            ),
        ],
        description="RAG 文档切块与向量（合并原 rag_documents 与 rag_chunks）",
        enable_dynamic_field=True,
    )


def _rag_index_params() -> IndexParams:
    """构建合并后集合的索引参数。

    - embedding：HNSW + COSINE，用于向量相似度检索。
    - document_id/source_type/content_type/section_title/created_at/doc_updated_at：
      INVERTED 倒排索引。Milvus 2.5.14 中 STL_SORT 仅支持数值字段，
      VARCHAR 字段（含 ISO 8601 时间字符串）统一使用 INVERTED。
    """
    index_params = IndexParams()

    # 向量索引：HNSW + COSINE
    index_params.add_index(
        field_name="embedding",
        index_type="HNSW",
        metric_type="COSINE",
        params={"M": HNSW_M, "efConstruction": HNSW_EF_CONSTRUCTION},
    )

    # 标量过滤字段：INVERTED 倒排索引（VARCHAR 类型不支持 STL_SORT）
    for field_name in (
        "document_id",
        "source_type",
        "content_type",
        "section_title",
        "created_at",
        "doc_updated_at",
    ):
        index_params.add_index(
            field_name=field_name,
            index_type="INVERTED",
        )

    return index_params


def ensure_collections(client: MilvusClient) -> list[str]:
    """确保 RAG 所需的合并集合与索引已存在，返回本次实际创建的集合名列表。

    幂等实现：集合已存在时直接跳过，不会重复创建或覆盖数据。

    Args:
        client: 已建立连接的 Milvus 客户端。

    Returns:
        本次调用中新建的集合名称列表，用于日志输出。
    """
    created: list[str] = []

    if client.has_collection(RAG_COLLECTION):
        logger.info("Milvus 集合 %s 已存在，跳过创建", RAG_COLLECTION)
        return created

    client.create_collection(
        collection_name=RAG_COLLECTION,
        schema=_rag_schema(),
        index_params=_rag_index_params(),
    )
    created.append(RAG_COLLECTION)
    logger.info("Milvus 集合 %s 创建完成（含索引）", RAG_COLLECTION)

    return created
