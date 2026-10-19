"""智能问数的只读 SQL 守卫。

不依赖第三方 SQL 解析器，采用「保守拒绝」策略：宁可误拒复杂 SQL，也不放过任何
写操作或越权读取。守卫是执行前的最后一道应用层防线，真正的兜底由执行层的
「只读事务 + 超时 + 行数上限」承担。

校验维度：

1. 去掉注释后折叠空白，要求语句以 SELECT 或 WITH 开头；
2. 拒绝多语句（末尾分号之外再出现分号）；
3. 对全部标识符做黑名单匹配，拒绝 DML / DDL / DCL 关键字；
4. 拒绝引用配置中的敏感列、黑名单表与系统 catalog（pg_catalog / information_schema）；
5. 对 LIMIT 做夹逼：缺失则补 LIMIT max_rows，超限则降为 max_rows。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ...config import settings
from .schema import sensitive_columns, table_blocklist

# 拒绝出现在任何位置的写操作 / DDL / DCL 关键字（小写）。
_FORBIDDEN_KEYWORDS = {
    "insert",
    "update",
    "delete",
    "drop",
    "alter",
    "truncate",
    "create",
    "replace",
    "merge",
    "copy",
    "vacuum",
    "reindex",
    "cluster",
    "grant",
    "revoke",
    "into",
    "begin",
    "commit",
    "rollback",
    "savepoint",
    "lock",
    "execute",
    "call",
    "do",
    "prepare",
    "listen",
    "unlisten",
    "notify",
    "load",
    "import",
    "export",
    "attach",
    "detach",
    "set",
    "reset",
}

# 系统 catalog：不允许通过问数探测数据库内部结构。
_SYSTEM_SCHEMAS = {
    "pg_catalog",
    "information_schema",
    "pg_shadow",
    "pg_authid",
    "pg_user",
    "pg_roles",
    "pg_stat_activity",
}

# 仅允许以这些关键字开头的只读语句（小写）。
_READ_ONLY_STARTS = {"select", "with"}

_IDENTIFIER = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]*")
_LIMIT = re.compile(r"\blimit\s+\d+", re.IGNORECASE)


@dataclass
class GuardResult:
    """一次 SQL 守卫校验的结果。"""

    ok: bool
    sql: str  # 夹逼 LIMIT 后的可执行 SQL（ok=True 时有效）
    error: str | None


def _tokens(sql: str) -> set[str]:
    """提取 SQL 中的全部标识符（小写）。列名带表前缀时会被拆分为两个 token。"""
    return {token.lower() for token in _IDENTIFIER.findall(sql)}


def _strip_comments(sql: str) -> str:
    """去掉行注释（--）与块注释（/* */）后折叠空白。"""
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    sql = re.sub(r"--[^\n]*", " ", sql)
    return re.sub(r"\s+", " ", sql).strip()


def _clamp_limit(sql: str, max_rows: int) -> str:
    """夹逼 LIMIT：缺失则补 LIMIT max_rows，超限则降为 max_rows。"""
    match = _LIMIT.search(sql)
    if match is None:
        return f"{sql} LIMIT {max_rows}"
    current = int(re.search(r"\d+", match.group(0)).group(0))  # type: ignore[union-attr]
    if current > max_rows:
        return _LIMIT.sub(f" LIMIT {max_rows}", sql, count=1)
    return sql


def _build_error(tokens: set[str], blocklist: set[str], sensitive: set[str]) -> str | None:
    """按优先级返回第一条违规原因；全部合规时返回 None。"""
    for token in sorted(tokens & sensitive):
        return f"查询涉及敏感列：{token}"
    for token in sorted(tokens & blocklist):
        return f"查询涉及黑名单表：{token}"
    for token in sorted(tokens & _SYSTEM_SCHEMAS):
        return f"禁止查询系统 catalog：{token}"
    for token in sorted(tokens & _FORBIDDEN_KEYWORDS):
        return f"SQL 含被禁止的关键字：{token}"
    return None


def guard(
    sql: str,
    *,
    max_rows: int | None = None,
    blocklist: set[str] | None = None,
    sensitive: set[str] | None = None,
) -> GuardResult:
    """校验并净化一条待执行的 SQL，返回夹逼后的只读 SQL 或错误原因。"""
    max_rows = settings.BI_MAX_ROWS if max_rows is None else max_rows
    blocked = table_blocklist() if blocklist is None else blocklist
    sensitive_set = sensitive_columns() if sensitive is None else sensitive

    normalized = _strip_comments(sql)
    if not normalized:
        return GuardResult(ok=False, sql="", error="SQL 为空")

    stripped = normalized.rstrip(";").rstrip()
    if ";" in stripped:
        return GuardResult(ok=False, sql="", error="禁止多语句查询")

    first_word = stripped.split(" ", 1)[0].lower()
    if first_word not in _READ_ONLY_STARTS:
        return GuardResult(ok=False, sql="", error=f"仅允许 SELECT / WITH 开头的只读查询，当前以 {first_word} 开头")

    tokens = _tokens(stripped)
    error = _build_error(tokens, blocked, sensitive_set)
    if error:
        return GuardResult(ok=False, sql="", error=error)

    return GuardResult(ok=True, sql=_clamp_limit(stripped, max_rows), error=None)
