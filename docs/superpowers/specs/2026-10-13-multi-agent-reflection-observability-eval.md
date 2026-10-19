# 多 Agent 反思、可观测性与评估：断层补齐

- 日期：2026-10-13
- 状态：已实现
- 范围：补全课件《多 Agent 系统》三大讲在项目中缺失的环节，不改动既有前端契约

## 1. 断层盘点

课件三讲与项目原有实现的对照：

| 课件内容 | 原有实现 | 断层 |
| --- | --- | --- |
| 1.3 Reflection（生成 → 批评 → 修订） | `core/agents/reflection/graph.py` + `POST /api/v1/reflection/write` | 无 |
| 1.4 Replanning（失败后重新规划） | `build_replanning_graph` + `POST /api/v1/reflection/execute` | 无 |
| 1.2 Reflexion（失败后文字反思，注入后续尝试） | 无 | **缺** |
| 2.2 Metric 层（延迟 / 成本 / 错误率） | 无 | **缺** |
| 2.2 Log 层（结构化日志） | 无 | **缺** |
| 2.3 LangSmith Trace | `core/observability.apply_langsmith_settings` 已接入启动流程 | 无（仅补了本地 Trace 兜底） |
| 3.1 核心评估指标（完成率 / 工具准确率 / 冲突率 / 延迟 / 成本） | 无 | **缺** |
| 3.2 评估方法（手动审查 / LLM-as-Judge / 回归测试） | 无 | **缺** |

## 2. 补齐内容

### 2.1 Reflexion

`src/app/core/agents/reflection/reflexion.py`

- 图结构：`plan → execute →（失败且未超上限）reflect → replan → execute …`；
- `reflect` 节点让 LLM 复盘失败原因，反思累积在 `reflections` 列表；
- `replan` 提示词带上全部历史反思，避免重复犯错；
- 复用 Replanning 的 `MAX_RETRIES = 3` 与 `should_replan` 判定；
- 新增接口 `POST /api/v1/reflection/reflexion`，响应比 `/execute` 多一个 `reflections` 字段。

### 2.2 可观测性三层

`src/app/core/observability.py`

- **Trace**：保留 LangSmith 环境变量注入；新增进程内 `TraceContext` / `Span` / 最近轨迹环形缓冲，离线可查；
- **Metric**：`MetricsCollector` 统计轮次、错误率、延迟分位数、各子智能体调用与失败次数、Token 与估算成本，并导出 Prometheus 文本；
- **Log**：`log_event` 输出单行 JSON 结构化日志。

`src/app/core/agents/orchestrator/engine.py`

- `stream_events` 拆为「公共入口 + `_iter_events`」，公共入口负责开启 / 结束轨迹；
- 埋点记录 `agent_call` / `agent_result` 跨度、`usage_metadata` 里的 Token 用量；
- **对外事件契约不变**（`tests/test_agents_ui.py` 的精确断言保持通过）。

接口：`GET /api/v1/observability/status | /metrics | /prometheus | /traces`。

### 2.3 多 Agent 评估

`src/app/agent_evaluation/`

| 文件 | 职责 |
| --- | --- |
| `schemas.py` | 用例、轨迹、指标结果、批量 / 回归请求响应 |
| `golden.py` | 内置黄金测试集（天气 / 电商 / 钉钉三条主链路） |
| `metrics.py` | 完成率、工具调用准确率（Dice）、冲突率、延迟、成本的纯函数实现 |
| `judge.py` | LLM-as-Judge（DeepSeek 裁判，输出 score + reason，失败降级为 None） |
| `runner.py` | `AgentRunner` 协议 + 生产 `OrchestratorAgentRunner` + 测试 `MockAgentRunner` |
| `service.py` | 批量评估、回归对比（候选相对基线的差值） |

接口：`POST /api/v1/agent/evaluation/batch | /regression`。

判定口径写在 `metrics.py` 的 docstring 里：完成 = 无异常 + 回答非空 + 关键词全中 + 期望子智能体都成功返回；冲突 = 有子智能体失败或同一子智能体被重复派单。

### 2.4 控制台

`src/app/static/agents/` 侧栏新增两块：

- **运行指标**：轮次、错误率、平均 / P95 延迟、Token、估算成本（每轮结束自动刷新）；
- **质量评估**：一键跑内置黄金集，展示五项核心指标与逐条用例结果，可选 LLM 判官。

## 3. 配置

`src/.env.example` 新增（均有默认值，可不填）：

```
OBSERVABILITY_TRACE_WINDOW=200
OBSERVABILITY_COST_PER_1K_INPUT=0.002
OBSERVABILITY_COST_PER_1K_OUTPUT=0.008
OBSERVABILITY_STRUCTURED_LOG=true
AGENT_JUDGE_MODEL=deepseek-chat
AGENT_JUDGE_ENABLED=true
AGENT_TARGET_COMPLETION_RATE=0.9
AGENT_TARGET_TOOL_ACCURACY=0.95
AGENT_TARGET_CONFLICT_RATE=0.05
AGENT_TARGET_AVG_LATENCY=5.0
```

成本单价按 deepseek-chat 公开价位量级设置，仅用于估算，请按实际账单调整。

## 4. 验证

```sh
uv run python -m pytest tests/test_reflection.py tests/test_reflexion.py tests/test_observability.py tests/test_agent_evaluation.py -q
uv run ruff check src tests
uv run mypy src
```

全量 `uv run python -m pytest` 应保持全绿。
