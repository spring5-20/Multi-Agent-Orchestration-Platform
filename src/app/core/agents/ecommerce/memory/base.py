import math
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from ....config import settings


@dataclass
class MemoryRecord:
    """一条长期记忆。
    - fact：事实类记忆。比如「用户住在杭州」「用户是男性」这类客观信息。
    - preference：偏好类记忆。比如「用户喜欢蓝色」「用户偏好无线耳机」这类主观喜好。
    - event：事件类记忆。比如「用户上周三下过一单」「用户退过一次货」这类发生过的事情。
    作用是让系统在召回（recall）记忆时，能区分记忆的性质，从而做更精准的筛选、排序或做不同的处理策略。
    """

    content: str
    id: int | None = None  # 后端主键，用于精确定位记忆（更新 / 删除 / 召回计数）
    memory_type: str = "fact"  # preference / fact / event
    importance: float = 0.5
    score: float = 0.0  # 召回时计算的综合得分（含时间衰减）
    created_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        """转为 JSON 安全的字典。"""
        return {
            "id": self.id,
            "content": self.content,
            "memory_type": self.memory_type,
            "importance": self.importance,
            "score": self.score,
            "created_at": _as_utc(self.created_at).isoformat() if self.created_at else None,
        }


@dataclass
class MemoryItem:
    """待写入的一条长期记忆（不含用户维度，供批量写入使用）。"""

    content: str
    memory_type: str = "fact"
    importance: float = 0.5


@dataclass
class UserProfile:
    """用户结构化画像。"""

    user_id: int
    name: str | None = None
    preferences: dict[str, Any] = field(default_factory=dict)
    history_summary: str | None = None
    last_interaction: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        """转为 JSON 安全的字典。"""
        return {
            "user_id": self.user_id,
            "name": self.name,
            "preferences": self.preferences,
            "history_summary": self.history_summary,
            "last_interaction": _as_utc(self.last_interaction).isoformat() if self.last_interaction else None,
        }


def _as_utc(dt: datetime) -> datetime:
    """把 naive 时间视为 UTC，aware 时间统一转 UTC，保证时间差计算健壮。"""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


@runtime_checkable
class MemoryStore(Protocol):
    """长期记忆存储的统一接口。

    新后端只需实现本协议并在 :func:`build_memory_store` 注册，即可无缝替换。
    """

    async def recall(self, user_id: int, query: str, top_k: int = 3) -> list[MemoryRecord]:
        """按语义召回与 ``query`` 相关的用户记忆。"""
        ...

    async def write(self, user_id: int, content: str, memory_type: str, importance: float = 0.5) -> None:
        """写入一条长期记忆。"""
        ...

    async def write_many(self, user_id: int, items: list[MemoryItem]) -> None:
        """批量写入多条长期记忆，实现可减少 embedding 往返与事务次数。"""
        ...

    async def get_profile(self, user_id: int) -> UserProfile | None:
        """获取用户画像。"""
        ...

    async def upsert_profile(self, profile: UserProfile, touch: bool = True) -> None:
        """写入或更新用户画像。

        ``touch=True`` 表示本次调用由用户交互触发，刷新 ``last_interaction``；
        后台任务（如窗口摘要）写入 ``history_summary`` 时应传 ``touch=False``，
        避免把「系统整理记忆」误判为「用户最新交互」。
        """
        ...

    async def mark_recalled(self, user_id: int, ids: list[int]) -> None:
        """按主键标记记忆被召回，用于更新召回频率与时间（供衰减策略使用）。"""
        ...

    async def list_all(self, user_id: int) -> list[MemoryRecord]:
        """列出某用户的全部长期记忆，供调试与查看接口使用。"""
        ...

    async def close(self) -> None:
        """释放后端资源。"""
        ...


def tokenize(text: str) -> set[str]:
    """简化分词：抽取连续中文单字、英文单词与数字（小写）。

    供召回相关度与画像裁剪复用，保证同一套文本相似度口径。
    """
    return set(re.findall(r"[\u4e00-\u9fff]|[a-zA-Z0-9]+", str(text).lower()))


# 画像字段删除哨兵：合并时值为 CLEAR 的键会被删除，用于表达「取消 / 清空」。
CLEAR = "__clear__"


def merge_profile_fields(existing: dict[str, Any] | None, updates: dict[str, Any] | None) -> dict[str, Any]:
    """合并画像字段：值为哨兵 ``CLEAR`` 的键被删除，其余非空值覆盖。"""
    merged = dict(existing or {})
    for key, value in (updates or {}).items():
        if value == CLEAR:
            merged.pop(key, None)
        elif value not in (None, "", [], {}):
            merged[key] = value
    return merged


# 记忆基础分的构成权重：时间衰减 + 重要性 + 召回频率。
_TIME_WEIGHT = 0.4
_IMPORTANCE_WEIGHT = 0.4
_RECALL_WEIGHT = 0.2


def compute_base_score(
    created_at: datetime | None,
    importance: float,
    recall_count: int,
    decay_days: int,
    now: datetime | None = None,
) -> float:
    """计算记忆的基础得分：时间衰减 + 重要性 + 召回频率。

    与存储后端无关，供 inmemory 与 postgres 复用，保证两种后端召回排序
    使用同一套公式。时间衰减以记忆创建时间为基准，符合遗忘曲线；召回频率
    反映被频繁使用的记忆应更容易被再次召回（封顶 1.0）。
    """
    now_utc = _as_utc(now or datetime.now(UTC))
    days_old = 0.0
    if created_at is not None:
        # 遗忘天数
        days_old = (now_utc - _as_utc(created_at)).total_seconds() / 86400.0
    time_decay = math.exp(-max(days_old, 0.0) / max(decay_days, 1))
    recall_freq = min(float(recall_count) / 10.0, 1.0)
    return time_decay * _TIME_WEIGHT + float(importance) * _IMPORTANCE_WEIGHT + recall_freq * _RECALL_WEIGHT


# 最终召回得分 = 基础分 * _BASE_WEIGHT + 语义相关度 * _RELEVANCE_WEIGHT。
_BASE_WEIGHT = 0.6
_RELEVANCE_WEIGHT = 0.4


def combine_score(base_score: float, relevance: float) -> float:
    """基础分与语义相关度加权求和，得到最终召回得分。"""
    return base_score * _BASE_WEIGHT + relevance * _RELEVANCE_WEIGHT


def build_memory_store(backend: str | None = None) -> MemoryStore:
    """根据配置创建长期记忆存储，实现记忆后端的平滑切换。"""
    backend = backend or settings.ECOMMERCE_MEMORY_BACKEND
    if backend == "postgres":
        from .postgres import PostgresMemoryStore

        return PostgresMemoryStore()

    from .inmemory import InMemoryMemoryStore

    return InMemoryMemoryStore()
