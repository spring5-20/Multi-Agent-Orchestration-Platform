"""智能问数（Text-to-SQL）智能体。

把用户的中文自然语言问题转换为 PostgreSQL 只读查询并解读结果，
表结构在运行时从数据库 catalog 动态内省，新建表自动进入问数范围。
"""

from .runtime import get_graph as get_graph
from .runtime import reset_graph as reset_graph
from .schema import get_schema as get_schema
from .schema import invalidate as invalidate
from .service import BiResult as BiResult
from .service import ask as ask
from .service import list_logs as list_logs
from .service import log_summary as log_summary
from .service import new_conversation_id as new_conversation_id

__all__ = [
    "BiResult",
    "ask",
    "get_graph",
    "get_schema",
    "invalidate",
    "list_logs",
    "log_summary",
    "new_conversation_id",
    "reset_graph",
]
