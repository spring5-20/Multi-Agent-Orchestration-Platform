# 智能问数（Text-to-SQL）落地设计

- 日期：2026-10-14
- 状态：已实现
- 范围：新增一条「自然语言 → SQL → 只读校验 → 执行 → 解读」链路，并接入编排器与控制台

## 1. 它解决什么问题

数据在数据库里，但「问数据」的门槛是 SQL + 表结构 + 口径。智能问数把这条链路自动化：
业务同学用中文提问，系统生成 SQL、只读执行、再用中文把结果讲清楚，全过程留痕可审计。

链路上的四个价值：降低取数门槛、秒级响应、口径统一可回溯、安全可控（只读 + 黑名单 + 敏感列拒绝）。

## 2. 组件与文件

| 组件 | 文件 | 职责 |
| --- | --- | --- |
| 配置 | `src/app/core/config.py` 的 `BISettings` | 黑名单、敏感列、行数上限、超时、重试次数、模型配置 |
| 审计表 | `src/app/core/db/models.py` 的 `BiQueryLog` | 记录 question / sql / status / row_count / elapsed_ms |
| 表结构内省 | `src/app/core/agents/bi/schema.py` | 运行时读 PostgreSQL catalog 并缓存，新表自动可见 |
| 只读守卫 | `src/app/core/agents/bi/guard_sql.py` | 拒绝写操作 / 多语句 / 敏感列 / 系统 catalog，夹逼 LIMIT |
| 只读执行器 | `src/app/core/agents/bi/executor.py` | 只读事务 + statement_timeout + asyncio 超时，结果转 JSON 安全类型 |
| 图状态 | `src/app/core/agents/bi/state.py` | `messages`（add_messages）承载多轮指代，其余字段每轮重置 |
| 提示词 | `src/app/core/agents/bi/prompts.py` | SQL 生成 / 结果解读提示词，以及图表自动推导 |
| 图编排 | `src/app/core/agents/bi/graph.py` | schema → generate → guard → execute → answer，失败自修正 |
| 多轮记忆 | `src/app/core/agents/bi/checkpointer.py`、`runtime.py` | PostgresSaver 检查点，thread_id = user_id:conversation_id |
| 服务层 | `src/app/core/agents/bi/service.py` | 会话锁、审计写入、日志查询与统计 |
| API | `src/app/api/v1/bi.py` | chat / chat-stream / schema / schema-refresh / logs / logs-summary |
| 子智能体 | `src/app/core/agents/orchestrator/bi_adapter.py` | 让 supervisor 也能把「问数类」问题路由过来 |
| 前端 | `src/app/static/bi/` | 提问、SQL、结果表格、图表、审计日志面板 |

## 3. 安全设计（三道防线）

1. **提示词层**：只把非敏感表结构给模型，敏感列根本不出现；
2. **守卫层**：`guard()` 保守拒绝写操作、多语句、敏感列、系统 catalog，并强制 `LIMIT`；
3. **执行层**：`SET TRANSACTION READ ONLY` + `statement_timeout` + `asyncio.wait_for`。

任何一层拒绝或执行报错，都会带着错误原因回到 generate 节点自修正，最多 `BI_SQL_MAX_RETRIES` 次。

## 4. 配置（src/.env）

```
BI_TABLE_BLOCKLIST=
BI_SENSITIVE_COLUMNS=password_hash,embedding
BI_MAX_ROWS=100
BI_QUERY_TIMEOUT_SECONDS=10
BI_SCHEMA_CACHE_TTL_SECONDS=300
BI_SQL_MAX_RETRIES=2
BI_TEMPERATURE=0
BI_CHART_ENABLED=true
BI_HISTORY_MESSAGES=6
# 可选：换用其他 OpenAI 兼容模型，留空则复用 DEEPSEEK_*
# BI_LLM_MODEL= / BI_LLM_BASE_URL= / BI_LLM_API_KEY=
```

## 5. 跑起来

```sh
uv run python src/scripts/seed_demo_products.py   # 幂等写入 20 条演示商品
uv run python src/run.py --port 8000              # 启动服务
```

- 页面：http://127.0.0.1:8000/bi/
- 接口：POST /api/v1/bi/chat、POST /api/v1/bi/chat/stream、GET /api/v1/bi/schema、GET /api/v1/bi/logs

## 6. 验证

```sh
uv run python -m pytest tests/test_bi.py -q   # 14 项离线用例：守卫 / 图 / API / 页面
uv run ruff check src tests
uv run mypy src
```

真实环境已实测：内省到 12 张表（users 的 password_hash 被剔除）→ 生成 SQL 并夹逼 LIMIT →
只读执行返回真实数据 → 自动生成图表 → 写入 bi_query_logs。
模型侧依赖 `DEEPSEEK_API_KEY`；若该 Key 失效，接口会返回「模型鉴权失败」的中文提示。
