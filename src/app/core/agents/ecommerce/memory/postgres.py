"""PostgreSQL + pgvector 长期记忆实现。

依赖：

- ``user_profiles`` 与 ``user_memories`` 表（见 ``core.db.models``）。
- dashscope embedding（``core.embeddings.get_text_embedding``）。
- 数据库需启用 pgvector 扩展（docker-compose 建议使用 ``pgvector/pgvector`` 镜像）。
"""

import logging
from datetime import UTC, datetime
from typing import Any

import anyio
from sqlalchemy import cast, delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import func

from ....config import settings
from ....db.database import local_session
from ....db.models import UserMemory as UserMemoryModel
from ....db.models import UserProfile as UserProfileModel
from ....db.models import Vector
from ....embeddings import get_text_embedding, get_text_embeddings
from .base import MemoryItem, MemoryRecord, MemoryStore, UserProfile, combine_score, compute_base_score

logger = logging.getLogger(__name__)


def _embedding_sync(content: str) -> list[float]:
    """同步调用 embedding 模型，由线程池包装避免阻塞事件循环。"""
    return get_text_embedding(content, dimension=1536)


def _embeddings_sync(contents: list[str]) -> list[list[float]]:
    """同步批量调用 embedding 模型，由线程池包装避免阻塞事件循环。"""
    return get_text_embeddings(contents, dimension=1536)


class PostgresMemoryStore(MemoryStore):
    """基于 pgvector 的长期记忆存储。"""

    async def recall(self, user_id: int, query: str, top_k: int = 3) -> list[MemoryRecord]:
        query_embedding = await anyio.to_thread.run_sync(_embedding_sync, query)
        query_vector = cast(query_embedding, Vector(1536))
        similarity = 1 - func.cosine_distance(UserMemoryModel.embedding, query_vector)
        stmt = select(
            UserMemoryModel.id,
            UserMemoryModel.content,
            UserMemoryModel.memory_type,
            UserMemoryModel.importance,
            UserMemoryModel.recall_count,
            UserMemoryModel.created_at,
            similarity.label("similarity"),
        ).where(UserMemoryModel.user_id == user_id)
        async with local_session() as session:
            result = await session.execute(stmt)
            rows = result.all()

        # 取该用户全部记忆，在 Python 侧用与 inmemory 相同的公式统一打分排序。
        # 用户长期记忆量级有限（后续会加条数上限），全量精排开销可控，且能保证
        # 重要性 / 召回频率 / 时间衰减与语义相关度在两个后端完全一致。
        scored: list[tuple[float, Any]] = []
        min_relevance = settings.ECOMMERCE_MEMORY_MIN_RELEVANCE
        for row in rows:
            base_score = compute_base_score(
                row.created_at,
                float(row.importance),
                int(row.recall_count or 0),
                settings.ECOMMERCE_MEMORY_DECAY_DAYS,
            )
            # cosine 相似度范围为 [-1, 1]，归一化到 [0, 1] 后作为语义相关度，
            # 与 inmemory 的关键词重叠率（[0, 1]）保持同一尺度。
            relevance = (float(row.similarity) + 1.0) / 2.0
            if relevance < min_relevance:
                continue
            score = combine_score(base_score, relevance)
            scored.append((score, row))
        scored.sort(key=lambda item: item[0], reverse=True)

        return [
            MemoryRecord(
                id=row.id,
                content=row.content,
                memory_type=row.memory_type,
                importance=row.importance,
                score=round(score, 4),
                created_at=row.created_at,
            )
            for score, row in scored[:top_k]
        ]

    async def write(self, user_id: int, content: str, memory_type: str, importance: float = 0.5) -> None:
        await self.write_many(user_id, [MemoryItem(content=content, memory_type=memory_type, importance=importance)])

    async def write_many(self, user_id: int, items: list[MemoryItem]) -> None:
        if not items:
            return
        contents = [item.content for item in items]

        # 先查已存在内容，避免对重复记忆浪费 embedding 调用。
        async with local_session() as session:
            result = await session.execute(
                select(UserMemoryModel.content).where(
                    UserMemoryModel.user_id == user_id,
                    UserMemoryModel.content.in_(contents),
                )
            )
            existing = set(result.scalars())
        pending = [item for item in items if item.content not in existing]
        if not pending:
            return

        # 一次批量 embedding 往返，随后单事务插入并统一做容量淘汰。
        embeddings = await anyio.to_thread.run_sync(_embeddings_sync, [item.content for item in pending])
        async with local_session() as session:
            for item, embedding in zip(pending, embeddings, strict=True):
                # 依赖 (user_id, content) 唯一约束做原子去重，避免并发下重复插入。
                stmt = (
                    pg_insert(UserMemoryModel)
                    .values(
                        user_id=user_id,
                        content=item.content,
                        embedding=embedding,
                        memory_type=item.memory_type,
                        importance=item.importance,
                    )
                    .on_conflict_do_nothing(index_elements=["user_id", "content"])
                )
                await session.execute(stmt)
            await self._enforce_capacity(session, user_id)
            await session.commit()

    async def _enforce_capacity(self, session: AsyncSession, user_id: int) -> None:
        """容量控制：超过单用户条数上限时，淘汰基础分最低的记忆。"""
        max_per_user = settings.ECOMMERCE_MEMORY_MAX_PER_USER
        if max_per_user <= 0:
            return
        result = await session.execute(
            select(
                UserMemoryModel.id,
                UserMemoryModel.created_at,
                UserMemoryModel.importance,
                UserMemoryModel.recall_count,
            ).where(UserMemoryModel.user_id == user_id)
        )
        rows = result.all()
        if len(rows) > max_per_user:
            ordered = sorted(
                rows,
                key=lambda row: compute_base_score(
                    row.created_at,
                    float(row.importance),
                    int(row.recall_count or 0),
                    settings.ECOMMERCE_MEMORY_DECAY_DAYS,
                ),
            )
            to_delete = [row.id for row in ordered[: len(ordered) - max_per_user]]
            await session.execute(delete(UserMemoryModel).where(UserMemoryModel.id.in_(to_delete)))

    async def get_profile(self, user_id: int) -> UserProfile | None:
        async with local_session() as session:
            row = await session.scalar(select(UserProfileModel).where(UserProfileModel.user_id == user_id))
        if row is None:
            return None
        return UserProfile(
            user_id=row.user_id,
            name=row.name,
            preferences=row.preferences or {},
            history_summary=row.history_summary,
            last_interaction=row.last_interaction,
        )

    async def upsert_profile(self, profile: UserProfile, touch: bool = True) -> None:
        now = datetime.now(UTC).replace(tzinfo=None) if touch else None
        async with local_session() as session:
            row = await session.scalar(select(UserProfileModel).where(UserProfileModel.user_id == profile.user_id))
            if row is None:
                session.add(
                    UserProfileModel(
                        user_id=profile.user_id,
                        name=profile.name,
                        preferences=profile.preferences,
                        history_summary=profile.history_summary,
                        last_interaction=now,
                    )
                )
            else:
                row.name = profile.name or row.name
                # 调用方（graph / window）传入的 preferences 已是完整合并结果，
                # 整体替换以支持删除语义（否则旧键无法被清空）。
                row.preferences = profile.preferences or {}
                row.history_summary = profile.history_summary or row.history_summary
                if touch:
                    row.last_interaction = now
            await session.commit()

    async def mark_recalled(self, user_id: int, ids: list[int]) -> None:
        if not ids:
            return
        now = datetime.now(UTC).replace(tzinfo=None)
        async with local_session() as session:
            result = await session.execute(
                select(UserMemoryModel).where(
                    UserMemoryModel.user_id == user_id,
                    UserMemoryModel.id.in_(ids),
                )
            )
            for row in result.scalars().all():
                row.recall_count = (row.recall_count or 0) + 1
                row.last_recalled_at = now
            await session.commit()

    async def list_all(self, user_id: int) -> list[MemoryRecord]:
        async with local_session() as session:
            result = await session.execute(
                select(UserMemoryModel)
                .where(UserMemoryModel.user_id == user_id)
                .order_by(UserMemoryModel.created_at.desc())
            )
            rows = result.scalars().all()
        return [
            MemoryRecord(
                id=row.id,
                content=row.content,
                memory_type=row.memory_type,
                importance=row.importance,
                score=round(
                    compute_base_score(
                        row.created_at,
                        float(row.importance),
                        int(row.recall_count or 0),
                        settings.ECOMMERCE_MEMORY_DECAY_DAYS,
                    ),
                    4,
                ),
                created_at=row.created_at,
            )
            for row in rows
        ]

    async def close(self) -> None:
        # 连接池由全局 engine 管理，无需显式关闭
        return None
