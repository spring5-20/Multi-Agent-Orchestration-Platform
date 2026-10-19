"""智能问数（Text-to-SQL）测试：守卫、图编排、API 与前端页面。

全部用例离线运行：图测试注入假模型与假执行器，并把表结构内省替换成固定字典，
不需要真实数据库或大模型。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from app.api.v1 import bi as bi_api
from app.core.agents.bi.graph import build_bi_graph, clean_sql
from app.core.agents.bi.guard_sql import guard
from app.core.agents.bi.service import BiResult, _initial_state
from app.main import app

FAKE_SCHEMA = {
    "products": {
        "comment": "商品库",
        "columns": [
            {"name": "id", "type": "BIGINT", "comment": "主键"},
            {"name": "name", "type": "VARCHAR", "comment": "商品名称"},
            {"name": "category", "type": "VARCHAR", "comment": "品类"},
            {"name": "price", "type": "FLOAT", "comment": "价格"},
        ],
    }
}


async def _fake_get_schema(force: bool = False) -> dict[str, Any]:
    """返回固定表结构，避免测试依赖数据库。"""
    return FAKE_SCHEMA


class _FakeLLM:
    """按调用顺序返回内容的假模型：第一次返回 SQL，第二次返回结论文本。"""

    def __init__(self, contents: list[str]) -> None:
        self._contents = contents
        self.calls = 0
        self.prompts: list[str] = []

    async def ainvoke(self, messages: list[Any]) -> Any:
        self.prompts.append(str(messages[-1].content))
        content = self._contents[min(self.calls, len(self._contents) - 1)]
        self.calls += 1
        return SimpleNamespace(content=content)


async def _fake_executor(sql: str) -> tuple[list[str], list[dict[str, Any]]]:
    """假执行器：返回固定两行结果。"""
    return ["category", "product_count"], [
        {"category": "运动鞋", "product_count": 6},
        {"category": "配件", "product_count": 7},
    ]


# ---------------- 只读守卫 ----------------


def test_guard_allows_select_and_clamps_limit() -> None:
    """只读 SELECT 应通过，并自动补 / 夹逼 LIMIT。"""
    plain = guard("select * from products")
    over = guard("select * from products limit 9999")

    assert plain.ok is True
    assert plain.sql.endswith("LIMIT 100")
    assert over.ok is True
    assert over.sql.endswith("LIMIT 100")


def test_guard_rejects_write_multi_statement_and_sensitive_column() -> None:
    """写操作、多语句、敏感列都必须被拒绝。"""
    write = guard("delete from products")
    multi = guard("select 1; drop table users")
    sensitive = guard("select password_hash from users")
    catalog = guard("select * from information_schema.tables")

    assert write.ok is False and "delete" in (write.error or "")
    assert multi.ok is False and "多语句" in (multi.error or "")
    assert sensitive.ok is False and "敏感列" in (sensitive.error or "")
    assert catalog.ok is False


def test_guard_strips_comments_and_rejects_keyword_inside_comment() -> None:
    """注释被剥离后再校验：注释里的写操作不生效，也不应误伤正常 SQL。"""
    result = guard("select /* delete from products */ name from products -- drop table users")
    assert result.ok is True
    assert "delete" not in result.sql
    assert result.sql.startswith("select name from products")


def test_clean_sql_removes_fences_and_semicolon() -> None:
    """模型输出常带 markdown 围栏与分号，需要清理成可执行 SQL。"""
    fence = chr(96) * 3
    raw = fence + "sql\nSELECT COUNT(*) FROM products;\n" + fence
    assert clean_sql(raw) == "SELECT COUNT(*) FROM products"
    assert clean_sql("SELECT 1;") == "SELECT 1"


# ---------------- 图编排（离线） ----------------


@pytest.mark.anyio
async def test_graph_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """schema → generate → guard → execute → answer 全链路应产出结论与图表。"""
    monkeypatch.setattr("app.core.agents.bi.graph.get_schema", _fake_get_schema)
    llm = _FakeLLM(
        [
            "SELECT category, COUNT(*) AS product_count FROM products GROUP BY category",
            "运动鞋 6 件，是数量最多的品类。",
        ]
    )
    graph = build_bi_graph(llm=llm, executor=_fake_executor)

    result = await graph.ainvoke(_initial_state("各品类商品数量是多少？"))

    assert result["sql"].endswith("LIMIT 100")
    assert result["columns"] == ["category", "product_count"]
    assert len(result["rows"]) == 2
    assert result["answer"] == "运动鞋 6 件，是数量最多的品类。"
    assert result["error"] is None
    assert result["chart"]["x_axis"] == ["运动鞋", "配件"]
    assert result["chart"]["series"][0]["data"] == [6, 7]
    # 表结构进提示词，多轮历史也在
    assert "products" in llm.prompts[0]
    assert "各品类商品数量是多少？" in llm.prompts[0]


@pytest.mark.anyio
async def test_graph_retries_after_guard_rejection(monkeypatch: pytest.MonkeyPatch) -> None:
    """第一版 SQL 被守卫拒绝时，应带着失败原因回炉重写，最终执行修正后的 SQL。"""
    monkeypatch.setattr("app.core.agents.bi.graph.get_schema", _fake_get_schema)
    executed: list[str] = []

    async def _executor(sql: str) -> tuple[list[str], list[dict[str, Any]]]:
        executed.append(sql)
        return ["category", "product_count"], [{"category": "运动鞋", "product_count": 6}]

    llm = _FakeLLM(
        [
            "DELETE FROM products",
            "SELECT category, COUNT(*) AS product_count FROM products GROUP BY category",
            "运动鞋 6 件。",
        ]
    )
    graph = build_bi_graph(llm=llm, executor=_executor)

    result = await graph.ainvoke(_initial_state("删掉所有商品"))

    assert result["error"] is None
    assert len(executed) == 1 and "DELETE" not in executed[0]
    assert result["answer"] == "运动鞋 6 件。"
    # 第二次生成时提示词里带上了守卫拒绝原因
    assert "被安全守卫拒绝" in llm.prompts[1]


@pytest.mark.anyio
async def test_graph_gives_up_after_max_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    """持续生成非法 SQL 时应重试到上限后放弃，不再执行任何 SQL。"""
    monkeypatch.setattr("app.core.agents.bi.graph.get_schema", _fake_get_schema)
    executed: list[str] = []

    async def _executor(sql: str) -> tuple[list[str], list[dict[str, Any]]]:
        executed.append(sql)
        return [], []

    llm = _FakeLLM(["DROP TABLE users"])
    graph = build_bi_graph(llm=llm, executor=_executor)

    result = await graph.ainvoke(_initial_state("删除用户表"))

    assert executed == []
    assert result["error"]
    assert "没能完成这次问数" in result["answer"]
    # 初次生成 + 2 次重试 = 3 次
    assert llm.calls == 3


@pytest.mark.anyio
async def test_graph_falls_back_when_answer_llm_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """解读节点失败时仍要用兜底文案返回结果，不能把查询结果丢掉。"""
    monkeypatch.setattr("app.core.agents.bi.graph.get_schema", _fake_get_schema)

    class _FlakyLLM(_FakeLLM):
        async def ainvoke(self, messages: list[Any]) -> Any:
            if "请给出结论" in str(messages[-1].content):
                raise RuntimeError("模型超时")
            return await super().ainvoke(messages)

    llm = _FlakyLLM(["SELECT category, COUNT(*) AS product_count FROM products GROUP BY category"])
    graph = build_bi_graph(llm=llm, executor=_fake_executor)

    result = await graph.ainvoke(_initial_state("各品类商品数量是多少？"))

    assert "共查询到 2 行结果" in result["answer"]


# ---------------- API 与前端 ----------------


def test_bi_routes_registered() -> None:
    """6 个问数端点都应注册到 /api/v1/bi 下。"""
    paths = app.openapi().get("paths", {})
    for path, method in (
        ("/api/v1/bi/chat", "post"),
        ("/api/v1/bi/chat/stream", "post"),
        ("/api/v1/bi/schema", "get"),
        ("/api/v1/bi/schema/refresh", "post"),
        ("/api/v1/bi/logs", "get"),
        ("/api/v1/bi/logs/summary", "get"),
    ):
        assert method in paths.get(path, {}), path


@pytest.mark.anyio
async def test_bi_chat_endpoint_returns_result(monkeypatch: pytest.MonkeyPatch) -> None:
    """/chat 应把服务层结果透传给前端（用假服务，离线跑）。"""

    async def _fake_ask(question: str, conversation_id: str | None = None, user_id: int = 0) -> BiResult:
        return BiResult(
            answer="运动鞋最多，共 6 件。",
            sql="SELECT category FROM products LIMIT 100",
            columns=["category"],
            rows=[{"category": "运动鞋"}],
            chart={"x_axis": ["运动鞋"], "series": [{"name": "count", "data": [6]}]},
            conversation_id="conv-1",
        )

    monkeypatch.setattr(bi_api, "ask", _fake_ask)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/api/v1/bi/chat", json={"question": "哪个品类商品最多？", "user_id": 3})

    assert response.status_code == 200
    body = response.json()
    assert body["answer"] == "运动鞋最多，共 6 件。"
    assert body["conversation_id"] == "conv-1"
    assert body["columns"] == ["category"]
    assert body["chart"]["series"][0]["data"] == [6]


@pytest.mark.anyio
async def test_bi_chat_stream_emits_events(monkeypatch: pytest.MonkeyPatch) -> None:
    """/chat/stream 应按契约产出 conversation_id / text / sql / table / chart / done。"""

    async def _fake_ask(question: str, conversation_id: str | None = None, user_id: int = 0) -> BiResult:
        return BiResult(
            answer="共 20 个商品。",
            sql="SELECT COUNT(*) FROM products LIMIT 100",
            columns=["count"],
            rows=[{"count": 20}],
            conversation_id="conv-2",
        )

    monkeypatch.setattr(bi_api, "ask", _fake_ask)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/api/v1/bi/chat/stream", json={"question": "一共有多少个商品？"})

    assert response.status_code == 200
    body = response.text
    for fragment in (
        '"type": "conversation_id"',
        '"type": "text"',
        '"type": "sql"',
        '"type": "table"',
        '"type": "done"',
    ):
        assert fragment in body


@pytest.mark.anyio
async def test_bi_schema_logs_and_refresh(monkeypatch: pytest.MonkeyPatch) -> None:
    """表结构、刷新与审计日志端点应正常返回（服务层替换为假实现）。"""

    async def _fake_schema() -> dict[str, Any]:
        return FAKE_SCHEMA

    async def _fake_invalidate() -> None:
        return None

    async def _fake_list_logs(user_id: int | None = None, *, limit: int = 50, offset: int = 0) -> Any:
        return [{"question": "一共有多少个商品？", "status": "success"}], 1

    async def _fake_summary(user_id: int | None = None) -> dict[str, Any]:
        return {"total": 1, "success": 1, "error": 0, "success_rate": 100.0, "daily": [], "top_users": []}

    monkeypatch.setattr(bi_api, "get_schema", _fake_schema)
    monkeypatch.setattr(bi_api, "invalidate", _fake_invalidate)
    monkeypatch.setattr(bi_api, "list_logs", _fake_list_logs)
    monkeypatch.setattr(bi_api, "log_summary", _fake_summary)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        schema = await client.get("/api/v1/bi/schema")
        refresh = await client.post("/api/v1/bi/schema/refresh")
        logs = await client.get("/api/v1/bi/logs")
        summary = await client.get("/api/v1/bi/logs/summary")

    assert schema.json()["schema"]["products"]["columns"]
    assert refresh.json() == {"status": "ok"}
    assert logs.json()["total"] == 1
    assert summary.json()["success_rate"] == 100.0


@pytest.mark.anyio
async def test_bi_page_and_assets_available() -> None:
    """/bi/ 页面、样式与脚本可访问，且各前端页面都有入口。"""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        page = await client.get("/bi/")
        style = await client.get("/bi/styles.css")
        script = await client.get("/bi/app.js")
        pages = {path: (await client.get(path)).text for path in ("/ui/", "/dingtalk/", "/ecommerce/", "/agents/")}

    assert page.status_code == 200
    assert "智能问数" in page.text
    assert "/api/v1/bi/chat" in page.text
    assert style.status_code == 200 and "text/css" in style.headers["content-type"]
    assert script.status_code == 200 and "javascript" in script.headers["content-type"]
    for path, html in pages.items():
        assert "/bi/" in html, path


def test_bi_registered_as_orchestrator_subagent() -> None:
    """智能问数应作为子智能体注册到编排器，supervisor 才能路由过来。"""
    from app.core.agents.orchestrator.engine import get_agents

    names = {agent.name for agent in get_agents()}
    assert "bi" in names
