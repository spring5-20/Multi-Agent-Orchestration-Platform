"""进程内长期记忆实现：零外部依赖，用于开发与无 pgvector 环境。

召回不使用向量，而是用关键词重叠近似语义相关度，再与共享的基础分
（时间衰减 + 重要性 + 召回频率）加权，保证未配置 embedding / pgvector
时也能完整演示记忆闭环，且与 postgres 后端使用同一套打分公式。
"""

from datetime import UTC, datetime

from ....config import settings
from .base import (
    MemoryItem,
    MemoryRecord,
    MemoryStore,
    UserProfile,
    combine_score,
    compute_base_score,
    tokenize,
)


class InMemoryMemoryStore(MemoryStore):
    """基于进程内字典的长期记忆存储。"""

    def __init__(self) -> None:
        self._memories: dict[int, list[dict]] = {}
        self._profiles: dict[int, UserProfile] = {}
        self._next_id: int = 1

    @staticmethod
    def _base_score(memory: dict) -> float:
        """计算单条记忆的基础分，复用 base 层的统一公式。"""
        return compute_base_score(
            memory.get("created_at"),
            float(memory.get("importance", 0.5)),
            int(memory.get("recall_count", 0)),
            settings.ECOMMERCE_MEMORY_DECAY_DAYS,
        )

    async def recall(self, user_id: int, query: str, top_k: int = 3) -> list[MemoryRecord]:
        memories = self._memories.get(user_id, [])
        if not memories:
            return []

        query_tokens = tokenize(query)
        min_relevance = settings.ECOMMERCE_MEMORY_MIN_RELEVANCE
        scored: list[tuple[float, dict]] = []
        for mem in memories:
            overlap = len(query_tokens & tokenize(mem["content"]))
            relevance = min(overlap / max(len(query_tokens), 1), 1.0)
            if relevance < min_relevance:
                continue
            score = combine_score(self._base_score(mem), relevance)
            scored.append((score, mem))
        scored.sort(key=lambda item: item[0], reverse=True)

        return [
            MemoryRecord(
                id=mem["id"],
                content=mem["content"],
                memory_type=mem["memory_type"],
                importance=mem["importance"],
                score=round(score, 4),
                created_at=mem["created_at"],
            )
            for score, mem in scored[:top_k]
        ]

    async def write(self, user_id: int, content: str, memory_type: str, importance: float = 0.5) -> None:
        await self.write_many(user_id, [MemoryItem(content=content, memory_type=memory_type, importance=importance)])

    async def write_many(self, user_id: int, items: list[MemoryItem]) -> None:
        memories = self._memories.setdefault(user_id, [])
        for item in items:
            # 去重：相同内容不重复写入
            if any(mem["content"] == item.content for mem in memories):
                continue
            memories.append(
                {
                    "id": self._next_id,
                    "content": item.content,
                    "memory_type": item.memory_type,
                    "importance": item.importance,
                    "recall_count": 0,
                    "created_at": datetime.now(UTC),
                }
            )
            self._next_id += 1
        self._enforce_capacity(user_id)

    def _enforce_capacity(self, user_id: int) -> None:
        """超过单用户条数上限时，按基础分升序淘汰最低分的记忆。"""
        max_per_user = settings.ECOMMERCE_MEMORY_MAX_PER_USER
        if max_per_user <= 0:
            return
        memories = self._memories.get(user_id, [])
        if len(memories) <= max_per_user:
            return
        memories.sort(key=self._base_score)
        del memories[: len(memories) - max_per_user]

    async def get_profile(self, user_id: int) -> UserProfile | None:
        return self._profiles.get(user_id)

    async def upsert_profile(self, profile: UserProfile, touch: bool = True) -> None:
        if touch:
            profile.last_interaction = datetime.now(UTC)
        self._profiles[profile.user_id] = profile

    async def mark_recalled(self, user_id: int, ids: list[int]) -> None:
        if not ids:
            return
        id_set = set(ids)
        for mem in self._memories.get(user_id, []):
            if mem["id"] in id_set:
                mem["recall_count"] = mem.get("recall_count", 0) + 1

    async def list_all(self, user_id: int) -> list[MemoryRecord]:
        records = [
            MemoryRecord(
                id=mem["id"],
                content=mem["content"],
                memory_type=mem["memory_type"],
                importance=mem["importance"],
                score=self._base_score(mem),
                created_at=mem["created_at"],
            )
            for mem in self._memories.get(user_id, [])
        ]
        records.sort(key=lambda item: item.created_at or datetime.min.replace(tzinfo=UTC), reverse=True)
        return records

    async def close(self) -> None:
        self._memories.clear()
        self._profiles.clear()
