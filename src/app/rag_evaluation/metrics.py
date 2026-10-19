"""RAGAS 裁判模型与指标工厂。

本文件负责三件事：

1. 导入 ragas 前先装兼容补丁；
2. 用 AsyncOpenAI 造裁判 LLM（重点：必须异步客户端，否则报
   ``Cannot use agenerate() with a synchronous client``）；
3. 把项目已有的 DashScope 嵌入适配成 RAGAS 的 ``BaseRagasEmbedding``。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any, cast

from app.core.config import settings
from app.core.embeddings import get_text_embedding

from ._compat import install_compat_stub

# 必须最先执行，否则 ragas 导入会因缺失 vertexai 模块而失败。
install_compat_stub()

from ragas.embeddings.base import BaseRagasEmbedding  # noqa: E402
from ragas.llms import llm_factory  # noqa: E402
from ragas.metrics.collections import (  # noqa: E402
    AnswerRelevancy,
    ContextPrecision,
    ContextRecall,
    Faithfulness,
)


def build_ragas_llm() -> Any:
    """构建 RAGAS 裁判 LLM。

    DeepSeek 提供 OpenAI 兼容接口，这里用 ``llm_factory`` 的 openai provider
    指向 DeepSeek 的 ``base_url``，返回 ``InstructorBaseRagasLLM`` 实例。

    注意：RAGAS 指标在异步上下文中调用 ``agenerate``，因此必须使用
    ``AsyncOpenAI`` 客户端，否则会报 “Cannot use agenerate() with a synchronous client”。
    """
    from openai import AsyncOpenAI

    client = AsyncOpenAI(
        api_key=settings.DEEPSEEK_API_KEY,
        base_url=settings.DEEPSEEK_BASE_URL or "https://api.deepseek.com",
    )
    model = settings.RAGAS_LLM_MODEL or settings.DEEPSEEK_MODEL or "deepseek-chat"
    return llm_factory(model, provider="openai", client=client)


class DashScopeRagasEmbedding(BaseRagasEmbedding):
    """把项目已有的 DashScope 文本嵌入适配为 RAGAS Embedding 接口。"""

    def __init__(self, dimension: int = 1536) -> None:
        super().__init__()
        self._dimension = dimension

    def embed_text(self, text: str, **kwargs: Any) -> list[float]:
        """同步嵌入单条文本。"""
        return cast(list[float], get_text_embedding(text, self._dimension))

    async def aembed_text(self, text: str, **kwargs: Any) -> list[float]:
        """异步嵌入单条文本，在线程池中执行以避免阻塞事件循环。"""
        return cast(list[float], await asyncio.to_thread(get_text_embedding, text, self._dimension))


def build_ragas_embeddings() -> BaseRagasEmbedding:
    """构建 RAGAS 使用的 DashScope 嵌入实例。"""
    return DashScopeRagasEmbedding()


def _build_metric(
    name: str,
    llm: Any,
    embeddings: BaseRagasEmbedding,
) -> Any:
    """按名称构造单个 RAGAS 指标实例。"""
    factories: dict[str, Callable[[], Any]] = {
        "faithfulness": lambda: Faithfulness(llm=llm),
        "answer_relevancy": lambda: AnswerRelevancy(llm=llm, embeddings=embeddings),
        "context_precision": lambda: ContextPrecision(llm=llm),
        "context_recall": lambda: ContextRecall(llm=llm),
    }
    factory = factories.get(name)
    if factory is None:
        supported = ", ".join(sorted(factories))
        raise ValueError(f"不支持的评估指标: {name}，可选指标: {supported}")
    return factory()


DEFAULT_METRIC_NAMES = ("faithfulness", "answer_relevancy", "context_precision", "context_recall")


def resolve_metrics(
    names: list[str] | tuple[str, ...] | None,
    llm: Any,
    embeddings: BaseRagasEmbedding,
) -> list[tuple[str, Any]]:
    """把指标名称列表解析为 ``(名称, 指标实例)`` 列表。"""
    resolved_names = list(names) if names else list(DEFAULT_METRIC_NAMES)
    return [(name, _build_metric(name, llm, embeddings)) for name in resolved_names]
