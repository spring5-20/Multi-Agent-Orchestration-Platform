from http import HTTPStatus
from typing import Any, TypedDict, cast

import dashscope

from .config import settings

# 初始化 API Key（只需执行一次）
dashscope.api_key = settings.DASHSCOPE_API_KEY  # type: ignore[assignment]


class ReRankResult(TypedDict):
    """DashScope 重排序结果。"""

    index: int
    relevance_score: float
    document: Any


def get_text_embedding(text: str, dimension: int = 1536) -> list[float]:
    """
    调用通义千问文本嵌入模型，返回文本的密集向量和稀疏向量。

    Args:
        text (str): 输入文本
        dimension (int): 向量维度，默认 1536（需与 documents 表的 vector 列维度一致）
    """
    resp = dashscope.TextEmbedding.call(
        model=settings.DASHSCOPE_MODEL,  # type: ignore[arg-type]
        input=text,
        dimension=dimension,
        output_type="dense&sparse",
    )

    if resp.status_code == HTTPStatus.OK:
        emb = resp.output["embeddings"][0]
        return cast(list[float], emb["embedding"])  # 密集向量列表

    else:
        raise RuntimeError(f"Embedding 请求失败: {resp.code} - {resp.message}")


def rerank_documents(query: str, documents: list[str], top_n: int | None = None) -> list[ReRankResult]:
    """调用 qwen3-rerank 对候选文本重新排序。

    Args:
        query: 检索问题。
        documents: 待重排序的候选文本列表。
        top_n: 返回的最大条数，``None`` 表示返回全部。

    Returns:
        按相关性降序排列的结果列表，每个结果包含 ``index`` 与 ``relevance_score``。
    """
    if not documents:
        return []

    response = dashscope.TextReRank.call(
        model=settings.DASHSCOPE_RERANK_MODEL,
        query=query,
        documents=documents,
        top_n=top_n,
    )
    if response.status_code != HTTPStatus.OK:
        raise RuntimeError(f"Rerank 请求失败: {response.code} - {response.message}")
    return [cast(ReRankResult, item) for item in response.output.results]


def get_text_embeddings(texts: list[str], dimension: int = 1536) -> list[list[float]]:
    """批量调用文本嵌入模型，返回与输入顺序一致的密集向量列表。

    Args:
        texts (list[str]): 待嵌入的文本列表，一次请求批量处理，减少网络往返。
        dimension (int): 向量维度，默认 1536（需与向量列维度一致）。

    Returns:
        与 ``texts`` 顺序一致的密集向量列表。
    """
    if not texts:
        return []

    resp = dashscope.TextEmbedding.call(
        model=settings.DASHSCOPE_MODEL,  # type: ignore[arg-type]
        input=texts,
        dimension=dimension,
        output_type="dense&sparse",
    )

    if resp.status_code == HTTPStatus.OK:
        return [cast(list[float], emb["embedding"]) for emb in resp.output["embeddings"]]
    raise RuntimeError(f"Embedding 请求失败: {resp.code} - {resp.message}")
