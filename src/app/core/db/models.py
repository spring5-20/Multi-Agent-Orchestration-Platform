from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from pgvector.sqlalchemy import Vector as PgVectorType
from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Identity,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base


def utcnow() -> datetime:
    """返回无时区信息的 UTC 当前时间，与 ``DateTime``（无时区）列匹配。"""
    return datetime.now(UTC).replace(tzinfo=None)


class Vector(PgVectorType):
    """适配 asyncpg 驱动的 pgvector 向量列类型。

    asyncpg 没有内置 pgvector 类型编解码器，因此把 Python list 转成
    pgvector 文本格式（如 ``[1.0,2.0]``）后再绑定，由 PostgreSQL 解析为向量。
    """

    cache_ok = True

    def bind_processor(self, dialect: Dialect) -> Callable[[Any], Any] | None:
        def process(value: Any) -> Any:
            if value is None:
                return value
            return "[" + ",".join(str(item) for item in value) + "]"

        return process


class IDMixin:
    """自增主键：BIGINT IDENTITY，符合单库业务系统的建表规范。"""

    id: Mapped[int] = mapped_column(
        BigInteger,
        Identity(),
        primary_key=True,
        kw_only=True,
        init=False,
    )


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default_factory=utcnow,
        server_default=text("current_timestamp(0)"),
        kw_only=True,
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True,
        default_factory=utcnow,
        onupdate=utcnow,
        server_default=text("current_timestamp(0)"),
        kw_only=True,
    )


class SoftDeleteMixin:
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True,
        default=None,
        kw_only=True,
    )
    is_deleted: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default=text("false"),
        kw_only=True,
    )


class ConnectionTest(Base):
    """连接验证表，用于在 PostgreSQL 连接成功时自动创建并验证连接可用。"""

    __tablename__ = "connection_test"

    id: Mapped[int] = mapped_column(
        BigInteger,
        Identity(),
        primary_key=True,
        init=False,
    )
    note: Mapped[str] = mapped_column(String(255), default="connection-ok")
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default_factory=utcnow,
        server_default=text("current_timestamp(0)"),
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        server_default=text("true"),
    )


class User(Base, IDMixin, TimestampMixin):
    """平台用户：身份认证主体，电商导购的记忆均归属到该用户。"""

    __tablename__ = "users"

    username: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        server_default=text("true"),
    )


class Product(Base, IDMixin, TimestampMixin):
    """商品库：供电商智能体推荐，支持向量检索（RAG 商品源）。"""

    __tablename__ = "products"

    name: Mapped[str] = mapped_column(String(255))
    category: Mapped[str] = mapped_column(String(100), index=True)
    description: Mapped[str] = mapped_column(Text)
    price: Mapped[float] = mapped_column(Float)
    embedding: Mapped[list[float] | None] = mapped_column(
        Vector(1536),
        nullable=True,
        default=None,
    )


class UserProfile(Base, IDMixin, TimestampMixin):
    """用户画像：跨会话的长期结构化记忆。"""

    __tablename__ = "user_profiles"

    user_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    name: Mapped[str | None] = mapped_column(String(100), nullable=True, default=None)
    preferences: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        default_factory=dict,
        server_default=text("'{}'::jsonb"),
    )
    history_summary: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    last_interaction: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True,
        default=None,
    )


class UserMemory(Base, IDMixin, TimestampMixin):
    """用户长期记忆：向量化存储，支持 pgvector 语义召回。"""

    __tablename__ = "user_memories"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "content",
            name="uq_user_memories_user_content",
        ),
    )

    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    content: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list[float]] = mapped_column(Vector(1536))
    memory_type: Mapped[str] = mapped_column(
        String(50),
        default="fact",
        server_default=text("'fact'"),
    )
    importance: Mapped[float] = mapped_column(
        Float,
        default=0.5,
        server_default=text("0.5"),
    )
    recall_count: Mapped[int] = mapped_column(
        Integer,
        default=0,
        server_default=text("0"),
    )
    last_recalled_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True,
        default=None,
    )


class Announcement(Base, IDMixin, TimestampMixin):
    """对外公告：发布前按风险等级决定是否需要人工审批。"""

    __tablename__ = "announcements"

    thread_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(255))
    content: Mapped[str] = mapped_column(Text)
    risk_level: Mapped[str] = mapped_column(
        String(20),
        default="high",
        server_default=text("'high'"),
    )
    status: Mapped[str] = mapped_column(
        String(30),
        default="submitted",
        server_default=text("'submitted'"),
    )
    submitter: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
        default=None,
    )
    approver: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
        default=None,
    )
    approval_note: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        default=None,
    )
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True,
        default=None,
    )


class BiQueryLog(Base, IDMixin, TimestampMixin):
    """智能问数审计日志：记录每次问数的问题、生成的 SQL 与执行结果。"""

    __tablename__ = "bi_query_logs"

    # 注意：无默认值的字段必须排在有默认值的字段之前，否则 MappedAsDataclass 报错。
    question: Mapped[str] = mapped_column(Text)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True, default=0, server_default=text("0"))
    sql: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    status: Mapped[str] = mapped_column(String(20), default="success", server_default=text("'success'"))
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    row_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    elapsed_ms: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))


class ApprovalRecord(Base, IDMixin, TimestampMixin):
    """审批审计记录：记录每次人工决策（批准/编辑/拒绝/取消）。"""

    __tablename__ = "approval_records"

    announcement_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("announcements.id"),
        index=True,
    )
    approver: Mapped[str] = mapped_column(String(100))
    action: Mapped[str] = mapped_column(String(20))
    note: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    decided_at: Mapped[datetime] = mapped_column(
        DateTime,
        default_factory=utcnow,
        server_default=text("current_timestamp(0)"),
    )