"""智能问数服务层：会话隔离、图调用、审计日志与日志统计。

- 会话隔离：thread_id = "{user_id}:{conversation_id}"，既按用户又按会话隔离多轮上下文；
- 并发保护：同一会话的并发问数用 asyncio.Lock 串行化，避免检查点的读-改-写竞态；
- 审计留痕：每次问数写入 bi_query_logs，写日志失败只告警，绝不影响问数主链路。
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from langchain_core.messages import HumanMessage
from sqlalchemy import Date, case, cast, func, select

from ...db.database import local_session
from ...db.models import BiQueryLog
from .runtime import get_graph

logger = logging.getLogger(__name__)

#: 同一会话的串行锁：避免并发问数把检查点状态写乱。
_thread_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)


@dataclass
class BiResult:
    """一次问数的结果。"""

    answer: str
    sql: str | None
    columns: list[str] = field(default_factory=list)
    rows: list[dict[str, Any]] = field(default_factory=list)
    chart: dict[str, Any] | None = None
    error: str | None = None
    conversation_id: str = ""


def new_conversation_id() -> str:
    """生成一个新的会话 id。"""
    return uuid.uuid4().hex


def _initial_state(question: str) -> dict[str, Any]:
    """构造本轮输入状态：消息累加，其余字段显式重置，避免残留上一轮结果。"""
    return {
        "messages": [HumanMessage(content=question)],
        "question": question,
        "sql": None,
        "feedback": None,
        "attempt": 0,
        "columns": [],
        "rows": [],
        "error": None,
        "answer": "",
        "chart": None,
    }


async def ask(question: str, conversation_id: str | None = None, user_id: int = 0) -> BiResult:
    """执行一次完整问数：自然语言 → SQL → 只读校验 → 执行 → 解读 → 审计留痕。"""
    question = question.strip()
    conversation_id = conversation_id or new_conversation_id()
    thread_id = f"{user_id}:{conversation_id}"

    started = time.perf_counter()
    status = "success"
    error_message: str | None = None
    sql: str | None = None
    row_count = 0
    answer = ""

    try:
        graph = await get_graph()
        async with _thread_locks[thread_id]:
            result = await graph.ainvoke(
                _initial_state(question),
                config={"configurable": {"thread_id": thread_id}},
            )
        answer = str(result.get("answer") or "").strip()
        sql = result.get("sql")
        rows = list(result.get("rows") or [])
        columns = list(result.get("columns") or [])
        chart = result.get("chart")
        error = result.get("error")
        row_count = len(rows)
        if error:
            status = "error"
            error_message = str(error)
        return BiResult(
            answer=answer or "没有生成结论。",
            sql=sql,
            columns=columns,
            rows=rows,
            chart=chart,
            error=error_message,
            conversation_id=conversation_id,
        )
    except Exception as exc:  # noqa: BLE001 - 任何异常都要留痕并给前端可读信息
        status = "error"
        error_message = f"{type(exc).__name__}: {exc}"
        answer = f"问数失败：{error_message}"
        logger.exception("智能问数执行失败")
        return BiResult(answer=answer, sql=sql, error=error_message, conversation_id=conversation_id)
    finally:
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        await _write_log(
            question=question,
            user_id=user_id,
            sql=sql,
            status=status,
            error_message=error_message,
            row_count=row_count,
            elapsed_ms=elapsed_ms,
        )


async def _write_log(
    *,
    question: str,
    user_id: int,
    sql: str | None,
    status: str,
    error_message: str | None,
    row_count: int,
    elapsed_ms: int,
) -> None:
    """写入审计日志；失败只告警，不影响问数结果返回。"""
    try:
        async with local_session() as session:
            session.add(
                BiQueryLog(
                    question=question,
                    user_id=user_id,
                    sql=sql,
                    status=status,
                    error_message=error_message,
                    row_count=row_count,
                    elapsed_ms=elapsed_ms,
                )
            )
            await session.commit()
    except Exception as exc:  # noqa: BLE001 - 无数据库时问数仍应可用
        logger.warning("问数审计日志写入失败: %s", exc)


def _log_to_dict(item: BiQueryLog) -> dict[str, Any]:
    """把日志 ORM 对象转成接口友好的字典。"""
    return {
        "id": item.id,
        "question": item.question,
        "user_id": item.user_id,
        "sql": item.sql,
        "status": item.status,
        "error_message": item.error_message,
        "row_count": item.row_count,
        "elapsed_ms": item.elapsed_ms,
        "created_at": item.created_at.isoformat() if item.created_at else None,
    }


async def list_logs(
    user_id: int | None = None,
    *,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    """分页查询问数审计日志，按时间倒序，返回 (items, total)。"""
    async with local_session() as session:
        conditions = [] if user_id is None else [BiQueryLog.user_id == user_id]
        total = await session.scalar(select(func.count()).select_from(BiQueryLog).where(*conditions))
        stmt = (
            select(BiQueryLog)
            .where(*conditions)
            .order_by(BiQueryLog.created_at.desc(), BiQueryLog.id.desc())
            .offset(offset)
            .limit(limit)
        )
        items = (await session.scalars(stmt)).all()
        return [_log_to_dict(item) for item in items], int(total or 0)


async def log_summary(user_id: int | None = None) -> dict[str, Any]:
    """问数日志总览：总量、成功率、平均耗时与行数、每日趋势、高频用户。"""
    conditions = [] if user_id is None else [BiQueryLog.user_id == user_id]
    day_expr = cast(BiQueryLog.created_at, Date)

    async with local_session() as session:
        overview = (
            await session.execute(
                select(
                    func.count().label("total"),
                    func.coalesce(func.sum(case((BiQueryLog.status == "success", 1), else_=0)), 0).label("success"),
                    func.coalesce(func.sum(case((BiQueryLog.status == "error", 1), else_=0)), 0).label("error"),
                    func.coalesce(func.avg(BiQueryLog.elapsed_ms), 0).label("avg_elapsed"),
                    func.coalesce(func.avg(BiQueryLog.row_count), 0).label("avg_rows"),
                ).where(*conditions)
            )
        ).one()

        daily = (
            await session.execute(
                select(
                    day_expr.label("day"),
                    func.count().label("count"),
                    func.coalesce(func.sum(case((BiQueryLog.status == "success", 1), else_=0)), 0).label("success"),
                    func.coalesce(func.sum(case((BiQueryLog.status == "error", 1), else_=0)), 0).label("error"),
                )
                .where(*conditions)
                .group_by(day_expr)
                .order_by(day_expr)
            )
        ).all()

        top_users = (
            await session.execute(
                select(BiQueryLog.user_id, func.count().label("count"))
                .where(*conditions)
                .group_by(BiQueryLog.user_id)
                .order_by(func.count().desc())
                .limit(10)
            )
        ).all()

    total = int(overview.total or 0)
    success = int(overview.success or 0)
    error = int(overview.error or 0)
    return {
        "total": total,
        "success": success,
        "error": error,
        "success_rate": round(success / total * 100, 1) if total else 0.0,
        "avg_elapsed_ms": round(float(overview.avg_elapsed or 0), 1),
        "avg_row_count": round(float(overview.avg_rows or 0), 1),
        "daily": [
            {
                "day": str(row.day),
                "count": int(row.count),
                "success": int(row.success),
                "error": int(row.error),
            }
            for row in daily
        ],
        "top_users": [{"user_id": int(row.user_id), "count": int(row.count)} for row in top_users],
    }
