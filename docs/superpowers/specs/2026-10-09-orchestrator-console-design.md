# 多智能体编排控制台（/agents/）设计

- 日期：2026-10-09
- 状态：待评审
- 范围：新增一个前端页面 + 两处向后兼容的后端增量

## 1. 背景与目标

项目已有三个前端：`/ui/`（和风天气出行）、`/dingtalk/`（钉钉助手，双页）、`/ecommerce/`（电商导购 + 实时语音）。
唯一没有界面的是**多智能体编排器**——它已经暴露了 `POST /api/v1/orchestrator/chat`（SSE 流式）和
`POST /api/v1/orchestrator/confirm`（确认闭环），却只能靠 curl 调用。

目标：做一个 `/agents/` 控制台，作为多智能体的统一入口，把编排器独有的能力暴露出来：

1. 单入口对话，由 supervisor 自动路由到 qweather / dingtalk / ecommerce 三个子智能体；
2. 把"路由过程"可视化（本轮派给了谁、耗时、成败）；
3. 把**确认闭环**做成可点击的交互（钉钉子智能体的写操作必须先确认）。

## 2. 范围

**做：**

- 新页面 `/agents/`（原生 HTML/CSS/JS，无构建步骤，沿用现有页面视觉语言）；
- 现有三个前端的 5 个页面都加「🧠 多智能体」入口（细节见 §4.4），新页面带全套导航；
- 后端增量一：`GET /api/v1/orchestrator/agents` 暴露子智能体清单；
- 后端增量二：编排器 SSE 增加 `agent_call` / `agent_result` 事件；
- `tests/test_agents_ui.py`：静态资源、导航互链、agents 接口、SSE 事件契约（离线可跑）。

**不做（YAGNI）：**

- 多会话历史列表、消息持久化；
- 子智能体开关 / 动态注册界面；
- 登录鉴权（编排器接口本身不校验令牌，页面保持即开即用）；
- 语音输入输出（`/ecommerce/voice.html` 已覆盖）。

## 3. 现状与缺口

| 项 | 现状 |
| --- | --- |
| 编排器对话 | `POST /api/v1/orchestrator/chat`，SSE 事件：`text` / `done` / `confirm_required` |
| 确认闭环 | `POST /api/v1/orchestrator/confirm`，SSE 事件：`text` / `done` / `cancelled` / `error` |
| 子智能体清单 | 仅存在于 `engine._registry`，无 HTTP 接口 |
| 路由可见性 | `engine.stream_answer()` 只产出最终文本，中间的工具调用被丢弃，前端无法知道派给了谁 |
| 前端模式 | `StaticFiles` 挂载 + 原生 JS + `fetch` 读取 SSE（见 `static/ecommerce/app.js`） |

## 4. 页面设计

单页三区，桌面两栏、窄屏上下堆叠。

**顶部**：品牌「🧠 多智能体编排控制台」+ 四页导航 + 状态条（后端可用性 / 会话 id / user_id / 本轮耗时 / 「新会话」「刷新状态」）。

**左栏（对话主区）**

- 用户消息、助手消息气泡；
- 助手气泡上方渲染一条**路由轨迹**：本轮依次调用的子智能体、每个耗时、成功或失败；未调用子智能体时显示"本轮由 supervisor 直接回答"；
- 输入框（Enter 发送、Shift+Enter 换行）+ 发送 / 停止按钮；示例问题按钮（"北京今天适合跑步吗"、"给张三发条消息"、"推荐 500 元以内的跑鞋"）。

**右栏（侧栏）**

- **子智能体卡片**：由 `GET /api/v1/orchestrator/agents` 渲染，展示名字、描述、"需确认"标记；本轮被调用时高亮，并累加调用次数；
- **本轮事件时间线**：按时间列出 `agent_call` / `agent_result` / `confirm_required` / 文本开始等事件，便于调试。

**4.4 导航改造细节**（实测现状，避免实现时踩坑）

| 页面 | 现状 | 改法 |
| --- | --- | --- |
| `/ui/`（`static/index.html`） | **没有任何导航条**，`static/styles.css` 里也没有 `.menu` 样式 | 新增一个 `nav.menu`（4 项：旅游建议 / 钉协助手 / 电商导购 / 多智能体），并在 `static/styles.css` 补 `.menu` 样式，视觉对齐 `static/ecommerce/styles.css` 那份 |
| `/ecommerce/`、`/ecommerce/voice.html` | 已有 4 项 `nav.menu`（旅游建议 / 钉协助手 / 电商导购 / 语音对话） | 追加第 5 项「🧠 多智能体」，`active` 归属不变 |
| `/dingtalk/`、`/dingtalk/operations.html` | `nav.menu` 只有本模块 2 项（信息查询 / 快捷办理），且 `common.js` 会按 `data-page` 切换 `.active` | 追加「🧠 多智能体」时**不带** `data-page`（否则会被 active 逻辑当成模块内页签）；顺带确认窄屏媒体查询下 3 项不溢出 |

**状态管理**（单个 JS 模块内的 `state`）：

```js
{
  conversationId,      // localStorage 持久化，可重置
  userId,              // 可选输入，默认 0，localStorage 记住
  agents: [],          // 来自 GET /orchestrator/agents
  pendingConfirm: null,// { confirmId, agent, prompt } —— 防重复提交
  controller: null,    // AbortController
  busy: false
}
```

## 5. 后端增量

### 5.1 `GET /api/v1/orchestrator/agents`

```json
{ "agents": [ { "name": "qweather", "description": "...", "confirmation_required": false } ] }
```

数据来自 `engine.get_agents()`（该函数已经会惰性注册默认子智能体）。

### 5.2 SSE 新增事件

`engine` 新增 `stream_events(question, *, context)`，产出结构化事件；
`stream_answer()` 保留为"只取 `text`"的薄封装，现有调用方与语义不变。

事件类型（触发点已按真实事件流校准，见下方「实现要点」）：

| type | 字段 | 触发点 |
| --- | --- | --- |
| `agent_call` | `agent`, `message` | supervisor 模型输出里带 `tool_calls`（`on_chat_model_end`，`langgraph_node == "supervisor"`），此时派单尚未执行 |
| `agent_result` | `agent`, `ok` | 工具节点结束（`on_chain_end`，`langgraph_node == "tools"`），输出里的 `ToolMessage.name` 即子智能体名；返回内容含「暂时无法回答」或「未知子智能体」时 `ok=false` |
| `text` | `text` | 最终回答的增量文本（保持现状语义） |
| `confirm_required` | `confirm_id`, `agent`, `prompt` | 子智能体需要确认（保持现状语义） |
| `done` | — | 本轮结束（保持现状语义） |

耗时由前端依据 `agent_call` / `agent_result` 自行计算，后端不传时长，减少耦合。
``/confirm`` 确认执行同一条链路时也会发 `agent_call` / `agent_result`，前端因此可以复用同一套轨迹渲染。

**实现要点（踩过的坑，务必保留）**

1. **不能用 `on_tool_start` / `on_tool_end` 判断派单**：supervisor 的子智能体工具（`_make_agent_tool`）只是给模型看的 schema，真正的执行发生在工具节点里直接调 `agent.run()`，并不会触发工具 runnable 的事件。
2. **子智能体「内部工具」的事件也带 `langgraph_node == "tools"`**：例如 qweather 子智能体内部的 `query_qweather_now`。若不做过滤，前端会把它们当成派单，出现"派给 query_qweather_now"这种错误轨迹。
3. 因此实现里维护一个「已派单名字集合」`pending`：只有登记过的名字才认结果，其余事件一律忽略。

## 6. 数据流

**普通问答**：发送 → `POST /chat` → 依次收到 `agent_call`（高亮卡片、记开始时间）→ `agent_result`（记结束时间）→ `text` 增量渲染 → `done`。

**需确认的写操作**：发送 → `agent_call`("`dingtalk`") → 收到 `confirm_required`（插确认卡，记录 `confirm_id`）→ 用户点「确认执行」→ `POST /confirm {decision:"confirm"}` → `text` 增量追加到卡片下方 → `done`；点「取消」→ `{decision:"cancel"}` → `cancelled`，卡片标记"已取消"。

## 7. 错误处理

- 非 2xx：读取响应体里的 `detail` 显示为错误气泡（复用 `static/ecommerce/app.js` 的 `readError` 思路）；
- 流式中断 / 用户点停止：`AbortController` 中止，保留已收到文本并标注「已停止」；
- 一轮结束但没有任何 `text`：提示"本轮可能只调用了工具，查看后端日志"；
- `error` 事件（例如"确认请求不存在或已过期"）：显示为错误提示，并清除本地待确认状态；
- 页面加载与「刷新状态」：探测 `GET /api/v1/health`，状态条显示后端可用性；后端不可用时禁用发送。

## 8. 测试策略（`tests/test_agents_ui.py`）

1. `/agents/`、`/agents/styles.css`、`/agents/app.js` 返回 200，页面标题包含「多智能体编排控制台」；
2. 导航互链：`/agents/` 含另外三个页面入口，且 `/ui/`、`/dingtalk/`、`/ecommerce/` 都含 `/agents/` 链接；
3. `GET /api/v1/orchestrator/agents` 返回 qweather / dingtalk / ecommerce，且 dingtalk 的 `confirmation_required` 为 `true`；
4. SSE 事件契约（离线）：用 monkeypatch 把 `engine.get_graph` 换成返回假事件序列的假图，断言（a）supervisor 的 `tool_calls` 映射为 `agent_call`、（b）工具节点输出映射为 `agent_result`、（c）子智能体内部工具的事件被忽略、（d）失败时 `ok=false`、（e）`text` 只来自最终回答；
5. 确认闭环（离线）：注册一个 `confirmation_required=True` 的假子智能体，验证 `/chat` 返回 `confirm_required` 且带 `confirm_id`，随后 `/confirm` 用该 id 能返回 `text` + `done`；未知 id 返回 `error`。

不依赖真实 LLM：supervisor 的 LLM 调用在测试里被假图替换。

## 9. 文件清单

**新增**

- `src/app/static/agents/index.html`
- `src/app/static/agents/styles.css`
- `src/app/static/agents/app.js`
- `tests/test_agents_ui.py`

**修改**

- `src/app/core/setup.py`：新增 `AGENTS_WEB_DIR` 并挂载 `/agents`
- `src/app/core/agents/orchestrator/engine.py`：新增 `stream_events()`，`stream_answer()` 改为薄封装
- `src/app/api/v1/orchestrator.py`：透传新事件 + 新增 `GET /agents`
- `src/app/static/index.html`、`src/app/static/ecommerce/index.html`、`src/app/static/ecommerce/voice.html`、`src/app/static/dingtalk/index.html`、`src/app/static/dingtalk/operations.html`：顶部导航加入口（见 §4.4）
- `src/app/static/styles.css`：为 `/ui/` 新增的导航条补 `.menu` 样式

## 10. 风险与回滚

| 风险 | 处理 |
| --- | --- |
| 改 `engine` 影响现有调用 | `stream_answer()` 语义与签名不变，只新增函数；现有事件类型语义不变，只新增两种 |
| 前端硬编码子智能体名 | 卡片全部来自 `/agents` 接口，增删子智能体无需改前端 |
| 测试依赖 LLM 导致不稳定 | 全部测试用假图 / 假子智能体，离线可跑 |
| 回滚 | 页面与后端增量彼此独立：删掉 `/agents` 挂载即恢复原状，后端新增接口/事件不影响旧调用方 |

## 11. 验收标准

1. 浏览器打开 `/agents/`，能看到导航、状态条、对话区、子智能体卡片；
2. 提问天气类问题，日志/轨迹显示调用了 `qweather` 并流式回答；
3. 提问"给张三发条消息"，派单轨迹上该子智能体先变「待确认」，页面出现确认卡；点「确认执行」后结果流式返回，点「取消」显示已取消；
4. `/agents` 接口返回三个子智能体，前端卡片与之一致；
5. `uv run python -m pytest` 全绿，`ruff check`、`mypy src` 无新增问题。
