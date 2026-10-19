"""个人任务管理 MCP Server（fastmcp + psycopg v3）。

提供任务增删改查工具、任务概览资源与每日复盘提示。
仅作为被挂载模块使用，由 FastAPI 主应用在 /task-mcp 路径挂载。
"""

from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from threading import Lock
from typing import Any

from fastmcp import FastMCP
from fastmcp.dependencies import Depends
from psycopg import Connection
from psycopg.conninfo import make_conninfo
from psycopg.rows import DictRow, dict_row
from psycopg_pool import ConnectionPool

from src.app.core.config import settings

# 连接串：从项目配置（src/.env）读取，使用 make_conninfo 自动转义特殊字符
CONNINFO = make_conninfo(
    host=settings.POSTGRES_SERVER,
    port=settings.POSTGRES_PORT,
    dbname=settings.POSTGRES_DB,
    user=settings.POSTGRES_USER,
    password=settings.POSTGRES_PASSWORD,
    connect_timeout=5,
    application_name="mcp-task-server",
)

# 连接池：延迟打开，首次调用工具时才真正连接数据库
pool: ConnectionPool[Connection[DictRow]] = ConnectionPool(
    conninfo=CONNINFO,
    min_size=1,
    max_size=5,
    open=False,
    kwargs={"row_factory": dict_row},
)


class ConnectionPoolProxy:
    """连接池代理：屏蔽 ``ConnectionPool`` 的上下文管理协议。

    psycopg_pool 的 ``ConnectionPool`` 实现了 ``__enter__``/``__exit__``，
    若直接作为 fastmcp ``Depends`` 的返回值，会被 uncalled_for 依赖引擎当作
    上下文管理器进入/退出，导致首次解析后连接池被 ``close()``、后续解析再
    次 ``open()`` 时抛出 ``PoolClosed``。代理对象不实现上下文管理协议，仅转发
    ``connection()`` 方法。
    """

    def __init__(self, connection_pool: ConnectionPool[Connection[DictRow]]) -> None:
        """保存被代理的连接池实例。"""
        self._pool = connection_pool

    def connection(self, timeout: float | None = None) -> AbstractContextManager[Connection[DictRow]]:
        """从底层连接池获取一条连接，返回上下文管理器。"""
        return self._pool.connection(timeout=timeout)


# 供依赖注入使用的代理单例；底层连接池仍由 get_conn 惰性打开
_pool_proxy = ConnectionPoolProxy(pool)

_initialized = False
_lock = Lock()


def _init_db() -> None:
    """建表（幂等）。"""
    with pool.connection() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id         SERIAL PRIMARY KEY,
                title      TEXT NOT NULL,
                done       BOOLEAN NOT NULL DEFAULT FALSE,
                priority   TEXT NOT NULL DEFAULT 'normal',
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )


def _ensure_db() -> None:
    """首次调用时打开连接池并建表（线程安全）。"""
    global _initialized
    if _initialized:
        return
    with _lock:
        if not _initialized:
            pool.open(wait=True, timeout=5)
            _init_db()
            _initialized = True


def get_pool() -> ConnectionPoolProxy:
    """依赖工厂：返回连接池代理（无阻塞操作，避免触发上下文管理协议）。"""
    return _pool_proxy


@contextmanager
def get_conn(db: ConnectionPoolProxy) -> Iterator[Connection[DictRow]]:
    """获取一条数据库连接；首次调用时惰性打开连接池并建表。"""
    _ensure_db()
    with db.connection() as conn:
        yield conn


mcp = FastMCP("task-manager")

_PRIORITIES = ("low", "normal", "high")


# ---------------- Tools ----------------
@mcp.tool
def add_task(
    title: str,
    priority: str = "normal",
    db: ConnectionPoolProxy = Depends(get_pool),
) -> Any | None:
    """添加一条任务。priority 可选 low / normal / high，默认 normal。"""
    if priority not in _PRIORITIES:
        priority = "normal"
    with get_conn(db) as conn:
        row = conn.execute(
            "INSERT INTO tasks (title, priority) VALUES (%s, %s) RETURNING id, title, priority",
            (title, priority),
        ).fetchone()
    assert row is not None  # INSERT ... RETURNING 必然返回新插入行
    return row


@mcp.tool
def list_tasks(
    include_done: bool = False,
    db: ConnectionPoolProxy = Depends(get_pool),
) -> list[DictRow]:
    """列出任务，默认只列未完成的任务；include_done=True 时包含已完成。"""
    sql = "SELECT id, title, done, priority FROM tasks"
    if not include_done:
        sql += " WHERE done = FALSE"
    sql += " ORDER BY CASE priority WHEN 'high' THEN 0 WHEN 'normal' THEN 1 ELSE 2 END, id"
    with get_conn(db) as conn:
        return conn.execute(sql).fetchall()


@mcp.tool
def complete_task(task_id: int, db: ConnectionPoolProxy = Depends(get_pool)) -> dict:
    """把指定 id 的任务标记为完成。"""
    with get_conn(db) as conn:
        cur = conn.execute("UPDATE tasks SET done = TRUE WHERE id = %s", (task_id,))
        return {"updated": cur.rowcount}


@mcp.tool
def delete_task(task_id: int, db: ConnectionPoolProxy = Depends(get_pool)) -> dict:
    """删除指定 id 的任务。"""
    with get_conn(db) as conn:
        cur = conn.execute("DELETE FROM tasks WHERE id = %s", (task_id,))
        return {"deleted": cur.rowcount}


# ---------------- Resource ----------------


@mcp.resource("tasks://summary")
def tasks_summary(db: ConnectionPoolProxy = Depends(get_pool)) -> str:
    """当前任务概览，可被客户端直接作为上下文注入。"""
    with get_conn(db) as conn:
        rows = conn.execute("SELECT id, title, done, priority FROM tasks ORDER BY id").fetchall()
    if not rows:
        return "（暂无任务）"
    lines = [f"[{r['id']}] {'✓' if r['done'] else '○'} ({r['priority']}) {r['title']}" for r in rows]
    done = sum(1 for r in rows if r["done"])
    return f"共 {len(rows)} 条，已完成 {done} 条\n" + "\n".join(lines)


@mcp.resource("tasks://usage")
def tasks_usage() -> str:
    """任务管理 MCP 的能力与使用说明，模型可在用户询问相关用法时读取。"""
    return (
        "个人任务管理能力说明：\n"
        "1) add_task(title, priority)：添加待办，priority 可选 low / normal / high，默认 normal；\n"
        "2) list_tasks(include_done)：列出任务，默认只返回未完成，include_done=True 时包含已完成；\n"
        "3) complete_task(task_id)：把指定任务标记为完成；\n"
        "4) delete_task(task_id)：删除指定任务；\n"
        "5) 资源 tasks://summary：当前任务概览；\n"
        "6) 提示 daily_review：生成今日复盘提示。\n"
        "说明：任务保存在 PostgreSQL 的 tasks 表，所有对话共享同一份清单；"
        "完成/删除需要任务 id，可先用 list_tasks 查询。"
    )


# ---------------- Prompt ----------------


@mcp.prompt
def daily_review(focus: str = "整体进度") -> str:
    """生成一次今日复盘提示。"""
    return (
        f"请读取 tasks://summary，围绕「{focus}」帮我做今日复盘："
        "1) 列出最该先做的三件事；2) 指出被拖延的任务；"
        "3) 给出一个可执行的下一步。"
    )
