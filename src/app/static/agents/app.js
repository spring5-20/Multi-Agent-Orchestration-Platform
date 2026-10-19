"use strict";

/*
 * 多智能体编排控制台前端逻辑。
 *
 * 覆盖：
 * 1. 子智能体清单：GET /api/v1/orchestrator/agents，侧栏卡片由接口驱动，不在前端写死；
 * 2. 对话：POST /api/v1/orchestrator/chat（SSE），渲染文本流 + 路由轨迹 + 事件时间线；
 * 3. 确认闭环：收到 confirm_required 时插确认卡，POST /api/v1/orchestrator/confirm 执行或取消；
 * 4. 会话：conversation_id 与 user_id 存 localStorage，「新会话」换新 id。
 */

const API_BASE = "/api/v1";
const CHAT_PATH = API_BASE + "/orchestrator/chat";
const CONFIRM_PATH = API_BASE + "/orchestrator/confirm";
const AGENTS_PATH = API_BASE + "/orchestrator/agents";
const HEALTH_PATH = API_BASE + "/health";
const METRICS_PATH = API_BASE + "/observability/metrics";
const EVAL_PATH = API_BASE + "/agent/evaluation/batch";

const CONV_KEY = "orchestrator.conversationId";
const USER_KEY = "orchestrator.userId";

let toastTimer = null;

const state = {
  conversationId: localStorage.getItem(CONV_KEY) || "",
  userId: Number(localStorage.getItem(USER_KEY) || "0") || 0,
  agents: [],
  backendOk: false,
  controller: null,
  busy: false,
};

/* ---------------- 通用小工具 ---------------- */

function el(id) {
  return document.getElementById(id);
}

function logBox() {
  return el("chatLog");
}

function toast(message, kind) {
  const box = el("toast");
  box.textContent = message;
  box.className = "toast" + (kind ? " " + kind : "");
  box.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(function () {
    box.hidden = true;
  }, 3600);
}

function setChip(name, value, cls) {
  const chip = document.querySelector('[data-chip="' + name + '"]');
  if (!chip) {
    return;
  }
  const valueBox = chip.querySelector("[data-value]");
  if (valueBox) {
    valueBox.textContent = value;
  }
  chip.classList.remove("ok", "warn", "bad");
  if (cls) {
    chip.classList.add(cls);
  }
}

function nowText() {
  return new Date().toLocaleTimeString("zh-CN", { hour12: false });
}

function shorten(text, limit) {
  const value = String(text || "").replace(/\s+/g, " ").trim();
  return value.length > limit ? value.slice(0, limit) + "…" : value;
}

function scrollLog() {
  const box = logBox();
  box.scrollTop = box.scrollHeight;
}

async function readError(response) {
  let detail = "";
  try {
    const data = await response.json();
    detail = typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail || data);
  } catch (error) {
    detail = "";
  }
  return detail || "HTTP " + response.status;
}

/* ---------------- 会话 ---------------- */

function newConversationId() {
  if (window.crypto && window.crypto.randomUUID) {
    return window.crypto.randomUUID();
  }
  return "conv-" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 8);
}

function ensureConversation(force) {
  if (force || !state.conversationId) {
    state.conversationId = newConversationId();
    localStorage.setItem(CONV_KEY, state.conversationId);
  }
  return state.conversationId;
}

function refreshSessionChips() {
  setChip("conv", ensureConversation(false).slice(0, 8), "ok");
  setChip("user", String(state.userId));
  const input = el("userIdInput");
  if (input && document.activeElement !== input) {
    input.value = String(state.userId);
  }
}

/* ---------------- 后端与子智能体 ---------------- */

async function checkBackend() {
  try {
    const response = await fetch(HEALTH_PATH, { headers: { Accept: "application/json" } });
    state.backendOk = response.ok;
    setChip("api", response.ok ? "可用" : "HTTP " + response.status, response.ok ? "ok" : "bad");
  } catch (error) {
    state.backendOk = false;
    setChip("api", "不可用", "bad");
  }
}

async function loadAgents() {
  const box = el("agentList");
  try {
    const response = await fetch(AGENTS_PATH, { headers: { Accept: "application/json" } });
    if (!response.ok) {
      throw new Error(await readError(response));
    }
    const data = await response.json();
    state.agents = data.agents || [];
    renderAgents();
    setChip("agents", String(state.agents.length) + " 个", "ok");
  } catch (error) {
    setChip("agents", "读取失败", "bad");
    box.textContent = "";
    const tip = document.createElement("div");
    tip.className = "empty-state";
    tip.textContent = "读取子智能体清单失败：" + (error && error.message ? error.message : error);
    box.appendChild(tip);
  }
}

function renderAgents() {
  const box = el("agentList");
  box.textContent = "";
  state.agents.forEach(function (agent) {
    const card = document.createElement("div");
    card.className = "agent-card";
    card.setAttribute("data-agent", agent.name);

    const head = document.createElement("div");
    head.className = "agent-head";

    const name = document.createElement("span");
    name.className = "agent-name";
    name.textContent = agent.name;
    head.appendChild(name);

    if (agent.confirmation_required) {
      const badge = document.createElement("span");
      badge.className = "agent-badge";
      badge.textContent = "需确认";
      head.appendChild(badge);
    }

    const count = document.createElement("span");
    count.className = "agent-count";
    count.textContent = "调用 0 次";
    head.appendChild(count);

    const desc = document.createElement("p");
    desc.className = "agent-desc";
    desc.textContent = agent.description || "（无描述）";

    card.appendChild(head);
    card.appendChild(desc);
    box.appendChild(card);
  });
}

function markAgent(name, cls, countIt) {
  const card = document.querySelector('.agent-card[data-agent="' + name + '"]');
  if (!card) {
    return;
  }
  const count = card.querySelector(".agent-count");
  if (cls === "active" && countIt && count) {
    const current = Number((count.textContent.match(/\d+/) || ["0"])[0]);
    count.textContent = "调用 " + (current + 1) + " 次";
  }
  card.classList.remove("active", "fail");
  if (cls === "fail") {
    card.classList.add("fail");
  } else if (cls === "active") {
    card.classList.add("active");
  }
}

/* ---------------- 事件时间线 ---------------- */

function addEvent(kind, text, cls) {
  const box = el("timeline");
  const empty = box.querySelector(".timeline-empty");
  if (empty) {
    empty.remove();
  }
  const item = document.createElement("li");
  const time = document.createElement("time");
  time.textContent = nowText();
  const body = document.createElement("span");
  body.textContent = text;
  if (cls === "ok") {
    body.className = "event-ok";
  } else if (cls === "fail") {
    body.className = "event-fail";
  } else if (cls === "warn") {
    body.className = "event-warn";
  }
  item.setAttribute("data-kind", kind);
  item.appendChild(time);
  item.appendChild(body);
  box.appendChild(item);
  box.scrollTop = box.scrollHeight;
}

/* ---------------- 消息渲染 ---------------- */

function appendUserMessage(text) {
  const log = logBox();
  const wrap = document.createElement("div");
  wrap.className = "msg user";
  const avatar = document.createElement("div");
  avatar.className = "avatar";
  avatar.textContent = "🙋";
  const body = document.createElement("div");
  body.className = "msg-body";
  const bubble = document.createElement("div");
  bubble.className = "bubble";
  bubble.textContent = text;
  const meta = document.createElement("div");
  meta.className = "meta";
  meta.textContent = nowText();
  body.appendChild(bubble);
  body.appendChild(meta);
  wrap.appendChild(avatar);
  wrap.appendChild(body);
  log.appendChild(wrap);
  scrollLog();
}

function beginTurn() {
  const log = logBox();
  const wrap = document.createElement("div");
  wrap.className = "msg assistant";

  const avatar = document.createElement("div");
  avatar.className = "avatar";
  avatar.textContent = "🧠";

  const body = document.createElement("div");
  body.className = "msg-body";

  const track = document.createElement("div");
  track.className = "route-track";
  track.hidden = true;

  const bubble = document.createElement("div");
  bubble.className = "bubble typing";
  bubble.textContent = "思考中…";

  body.appendChild(track);
  body.appendChild(bubble);
  wrap.appendChild(avatar);
  wrap.appendChild(body);
  log.appendChild(wrap);
  scrollLog();

  return { wrap: wrap, body: body, track: track, bubble: bubble, text: "", calls: {}, start: Date.now(), finished: false };
}

function routeStart(turn, agent, message) {
  turn.track.hidden = false;
  let chip = turn.track.querySelector('[data-agent="' + agent + '"]');
  if (!chip) {
    chip = document.createElement("span");
    chip.className = "route-chip running";
    chip.setAttribute("data-agent", agent);
    const dot = document.createElement("span");
    dot.className = "dot";
    const name = document.createElement("span");
    name.className = "name";
    name.textContent = agent;
    const elapsed = document.createElement("span");
    elapsed.className = "elapsed";
    elapsed.textContent = "进行中";
    chip.appendChild(dot);
    chip.appendChild(name);
    chip.appendChild(elapsed);
    turn.track.appendChild(chip);
  }
  const firstDispatch = !turn.calls[agent];
  if (!firstDispatch) {
    // 同一子智能体二次出现（例如确认后重新执行）：重置为进行中
    chip.className = "route-chip running";
    const running = chip.querySelector(".elapsed");
    if (running) {
      running.textContent = "进行中";
    }
  }
  turn.calls[agent] = { start: Date.now(), chip: chip };
  // 只按「本轮首次派给该子智能体」计数，避免确认后重复执行被算成两次调用
  markAgent(agent, "active", firstDispatch);
  addEvent("agent_call", "派给 " + agent + (message ? "：" + shorten(message, 40) : ""), "");
}

function routeFinish(turn, agent, ok) {
  const call = turn.calls[agent];
  const cost = call ? (Date.now() - call.start) / 1000 : 0;
  if (call) {
    call.chip.className = "route-chip " + (ok ? "done" : "fail");
    const elapsed = call.chip.querySelector(".elapsed");
    elapsed.textContent = (ok ? "完成 " : "失败 ") + cost.toFixed(1) + "s";
  }
  markAgent(agent, ok ? "" : "fail");
  addEvent("agent_result", agent + (ok ? " 返回结果" : " 执行失败"), ok ? "ok" : "fail");
}

function appendText(turn, text) {
  if (turn.bubble.classList.contains("typing")) {
    turn.bubble.classList.remove("typing");
    turn.bubble.textContent = "";
  }
  turn.text += text;
  turn.bubble.textContent = turn.text;
  scrollLog();
}

function markDone(turn, label, start) {
  const cost = ((Date.now() - (start || turn.start)) / 1000).toFixed(1);
  setChip("turn", cost + "s", "ok");
  addEvent("done", label + "，耗时 " + cost + "s", "");
  turn.finished = true;
  // 每轮结束刷新一次指标，指标面板与后端进程内采集保持一致
  loadMetrics();
}


function finishTurn(turn, sawConfirm) {
  if (turn.finished) {
    return;
  }
  if (turn.bubble.classList.contains("typing")) {
    turn.bubble.classList.remove("typing");
    turn.bubble.textContent = sawConfirm
      ? "（等待你确认上面的操作）"
      : "（本轮没有返回文本：可能只调用了工具，具体看后端日志）";
  }
  markDone(turn, "本轮结束");
}

function appendConfirmCard(turn, event) {
  // 轨迹上的该子智能体标记为「待确认」：此刻它还没有真正执行
  const chip = turn.track.querySelector('[data-agent="' + event.agent + '"]');
  if (chip) {
    chip.className = "route-chip pending";
    const elapsed = chip.querySelector(".elapsed");
    if (elapsed) {
      elapsed.textContent = "待确认";
    }
  }
  turn.bubble.classList.remove("typing");
  if (!turn.text) {
    turn.bubble.textContent = "这个操作需要你确认后才会执行。";
  }

  const card = document.createElement("div");
  card.className = "confirm-card";
  // 确认信息存在卡片自身上：多张待确认卡片并存时互不干扰
  card.dataset.confirmId = String(event.confirm_id || "");
  card.dataset.agent = String(event.agent || "");

  const title = document.createElement("span");
  title.className = "confirm-title";
  title.textContent = "⚠️ 需要确认 · 子智能体：" + event.agent;

  const prompt = document.createElement("p");
  prompt.className = "confirm-prompt";
  prompt.textContent = event.prompt || "该操作需要确认后才能执行。";

  const actions = document.createElement("div");
  actions.className = "confirm-actions";

  const okBtn = document.createElement("button");
  okBtn.type = "button";
  okBtn.className = "primary-btn";
  okBtn.textContent = "确认执行";

  const cancelBtn = document.createElement("button");
  cancelBtn.type = "button";
  cancelBtn.className = "ghost-btn";
  cancelBtn.textContent = "取消";

  okBtn.addEventListener("click", function () {
    decide(turn, card, okBtn, cancelBtn, "confirm");
  });
  cancelBtn.addEventListener("click", function () {
    decide(turn, card, okBtn, cancelBtn, "cancel");
  });

  actions.appendChild(okBtn);
  actions.appendChild(cancelBtn);
  card.appendChild(title);
  card.appendChild(prompt);
  card.appendChild(actions);
  turn.body.appendChild(card);

  addEvent("confirm_required", event.agent + " 需要确认后才能执行", "warn");
  scrollLog();
}

async function decide(turn, card, okBtn, cancelBtn, decision) {
  const confirmId = card.dataset.confirmId;
  if (state.busy || !confirmId || card.dataset.decided === "1") {
    return;
  }
  card.dataset.decided = "1";
  const pending = { confirmId: confirmId, agent: card.dataset.agent || "" };
  okBtn.disabled = true;
  cancelBtn.disabled = true;

  state.busy = true;
  state.controller = new AbortController();
  setButtons(true);
  const startedAt = Date.now();

  try {
    await streamEvents(CONFIRM_PATH, { confirm_id: pending.confirmId, decision: decision }, function (event) {
      if (event.type === "cancelled") {
        card.className = "confirm-card cancelled";
        card.querySelector(".confirm-title").textContent = "🚫 已取消";
        addEvent("cancelled", "你取消了该操作", "warn");
        return;
      }
      if (event.type === "agent_call") {
        routeStart(turn, event.agent, event.message);
        return;
      }
      if (event.type === "agent_result") {
        routeFinish(turn, event.agent, event.ok);
        return;
      }
      if (event.type === "text") {
        appendText(turn, event.text);
        return;
      }
      if (event.type === "error") {
        turn.wrap.classList.add("error");
        turn.bubble.classList.remove("typing");
        turn.bubble.textContent = event.message;
        addEvent("error", event.message, "fail");
        return;
      }
      if (event.type === "done") {
        finishTurn(turn, false);
      }
    });
    if (card.className.indexOf("cancelled") < 0) {
      card.className = "confirm-card confirmed";
      card.querySelector(".confirm-title").textContent = "✅ 已确认执行 · " + pending.agent;
    }
  } catch (error) {
    const message = error && error.message ? error.message : String(error);
    turn.wrap.classList.add("error");
    turn.bubble.classList.remove("typing");
    turn.bubble.textContent = "确认请求失败：" + message;
    toast(message, "error");
    addEvent("error", message, "fail");
  } finally {
    state.busy = false;
    state.controller = null;
    setButtons(false);
    // 确认执行是独立的一段：即使对话轮已经结束，也要补记本次的结束事件与耗时
    markDone(turn, "确认执行结束", startedAt);
  }
}

/* ---------------- SSE ---------------- */

function parseFrame(frame) {
  const line = frame.split("\n").find(function (item) {
    return item.indexOf("data:") === 0;
  });
  if (!line) {
    return null;
  }
  try {
    return JSON.parse(line.slice(5).trim());
  } catch (error) {
    return null;
  }
}

async function streamEvents(path, body, onEvent) {
  const response = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
    body: JSON.stringify(body),
    signal: state.controller ? state.controller.signal : undefined,
  });
  if (!response.ok) {
    throw new Error(await readError(response));
  }
  if (!response.body) {
    throw new Error("当前浏览器不支持流式响应");
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder("utf-8");
  let buffer = "";
  while (true) {
    const chunk = await reader.read();
    if (chunk.done) {
      return;
    }
    buffer += decoder.decode(chunk.value, { stream: true });
    let index = buffer.indexOf("\n\n");
    while (index >= 0) {
      const event = parseFrame(buffer.slice(0, index));
      buffer = buffer.slice(index + 2);
      if (event) {
        onEvent(event);
        if (event.type === "done") {
          return;
        }
      }
      index = buffer.indexOf("\n\n");
    }
  }
}

/* ---------------- 主流程 ---------------- */

async function sendMessage(raw) {
  if (state.busy) {
    return;
  }
  const message = String(raw || "").trim();
  if (!message) {
    return;
  }
  if (!state.backendOk) {
    toast("后端接口不可用，请先点「刷新状态」", "error");
    return;
  }

  const empty = logBox().querySelector(".empty-state");
  if (empty) {
    empty.remove();
  }

  appendUserMessage(message);
  el("chatInput").value = "";
  resizeInput();

  const turn = beginTurn();
  state.busy = true;
  state.controller = new AbortController();
  setButtons(true);

  let sawConfirm = false;
  try {
    await streamEvents(
      CHAT_PATH,
      { question: message, user_id: state.userId, conversation_id: ensureConversation(false) },
      function (event) {
        if (event.type === "text") {
          appendText(turn, event.text);
          return;
        }
        if (event.type === "agent_call") {
          routeStart(turn, event.agent, event.message);
          return;
        }
        if (event.type === "agent_result") {
          routeFinish(turn, event.agent, event.ok);
          return;
        }
        if (event.type === "confirm_required") {
          sawConfirm = true;
          addEvent("confirm_required", event.agent + " 需要确认后才能执行", "warn");
          appendConfirmCard(turn, event);
          return;
        }
        if (event.type === "error") {
          turn.wrap.classList.add("error");
          turn.bubble.classList.remove("typing");
          turn.bubble.textContent = event.message;
          addEvent("error", event.message, "fail");
          return;
        }
        if (event.type === "done") {
          finishTurn(turn, sawConfirm);
        }
      }
    );
  } catch (error) {
    turn.bubble.classList.remove("typing");
    if (error && error.name === "AbortError") {
      turn.bubble.textContent = (turn.text || "") + "\n\n[已停止]";
      addEvent("aborted", "你停止了本轮请求", "warn");
    } else {
      const message2 = error && error.message ? error.message : String(error);
      turn.wrap.classList.add("error");
      turn.bubble.textContent = turn.text ? turn.text + "\n\n[出错] " + message2 : "请求失败：" + message2;
      toast(message2, "error");
      addEvent("error", message2, "fail");
    }
  } finally {
    state.busy = false;
    state.controller = null;
    setButtons(false);
    finishTurn(turn, sawConfirm);
  }
}

/* ---------------- 可观测性（Metric 层） ---------------- */

function percentText(value) {
  return (Number(value || 0) * 100).toFixed(1) + "%";
}

function secondsText(value) {
  return Number(value || 0).toFixed(2) + "s";
}

function moneyText(value) {
  return Number(value || 0).toFixed(4);
}

async function loadMetrics() {
  const box = el("metricGrid");
  if (!box) {
    return;
  }
  try {
    const response = await fetch(METRICS_PATH, { headers: { Accept: "application/json" } });
    if (!response.ok) {
      throw new Error(await readError(response));
    }
    renderMetrics(await response.json());
  } catch (error) {
    box.innerHTML = '<div class="empty-state">指标读取失败</div>';
  }
}

function renderMetrics(snapshot) {
  const box = el("metricGrid");
  if (!box) {
    return;
  }
  const errorRate = Number(snapshot.error_rate || 0);
  const avgLatency = Number(snapshot.avg_latency || 0);
  const items = [
    { label: "轮次", value: String(snapshot.turns_total || 0), cls: "" },
    { label: "错误率", value: percentText(errorRate), cls: errorRate > 0 ? "warn" : "ok" },
    { label: "平均延迟", value: secondsText(avgLatency), cls: avgLatency <= 5 ? "ok" : "warn" },
    { label: "P95 延迟", value: secondsText(snapshot.p95_latency), cls: "" },
    {
      label: "Token 入/出",
      value: String(snapshot.input_tokens_total || 0) + " / " + String(snapshot.output_tokens_total || 0),
      cls: "",
    },
    { label: "估算成本(元)", value: moneyText(snapshot.cost_total), cls: "" },
  ];

  const html = items
    .map(function (item) {
      return (
        '<div class="metric-item ' +
        item.cls +
        '"><span class="metric-label">' +
        item.label +
        '</span><span class="metric-value">' +
        item.value +
        "</span></div>"
      );
    })
    .join("");
  box.innerHTML = html;
}

/* ---------------- 质量评估（多 Agent 核心指标 + LLM-as-Judge） ---------------- */

async function runEvaluation(useJudge) {
  const box = el("evalResult");
  if (!box) {
    return;
  }
  box.innerHTML =
    '<div class="empty-state">正在跑内置黄金集' + (useJudge ? "（含 LLM 判官，较慢）" : "") + "…</div>";
  try {
    const response = await fetch(EVAL_PATH, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify({ use_judge: useJudge }),
    });
    if (!response.ok) {
      throw new Error(await readError(response));
    }
    renderEvaluation(await response.json());
  } catch (error) {
    box.innerHTML = '<div class="empty-state">评估失败：' + shorten(error.message, 100) + "</div>";
  }
}

function renderEvaluation(data) {
  const box = el("evalResult");
  if (!box) {
    return;
  }
  const summary = data.summary || {};
  const passed = summary.passed || {};
  const rows = [
    { label: "完成率", value: percentText(summary.completion_rate), target: "> 90%", ok: passed.completion_rate },
    {
      label: "工具准确率",
      value: summary.tool_call_accuracy == null ? "—" : percentText(summary.tool_call_accuracy),
      target: "> 95%",
      ok: passed.tool_call_accuracy,
    },
    { label: "冲突率", value: percentText(summary.conflict_rate), target: "< 5%", ok: passed.conflict_rate },
    { label: "平均延迟", value: secondsText(summary.avg_latency), target: "< 5s", ok: passed.avg_latency },
    { label: "平均成本", value: moneyText(summary.avg_cost), target: "按预算", ok: true },
  ];
  if (summary.judge_score != null) {
    rows.push({ label: "判官均分", value: percentText(summary.judge_score), target: "0~1", ok: true });
  }

  const metricHtml = rows
    .map(function (row) {
      const cls = row.ok === false ? "bad" : row.ok === true ? "ok" : "";
      return (
        '<div class="eval-metric ' +
        cls +
        '"><span class="metric-label">' +
        row.label +
        '</span><span class="metric-value">' +
        row.value +
        '</span><span class="eval-target">' +
        row.target +
        "</span></div>"
      );
    })
    .join("");

  const caseHtml = (data.results || [])
    .map(function (item) {
      const judge = item.judge && item.judge.score != null ? percentText(item.judge.score) : "—";
      return (
        '<div class="eval-case ' +
        (item.completed ? "ok" : "bad") +
        '"><div class="eval-question">' +
        shorten(item.question, 34) +
        '</div><div class="eval-meta">' +
        (item.completed ? "✅ 完成" : "⚠️ 未完成") +
        " · 路由：" +
        (item.tool_calls.length ? item.tool_calls.join(" / ") : "未派单") +
        " · 判官：" +
        judge +
        " · " +
        secondsText(item.latency) +
        "</div></div>"
      );
    })
    .join("");

  box.innerHTML =
    '<div class="metric-grid">' +
    metricHtml +
    '</div><div class="eval-cases">' +
    caseHtml +
    "</div>";
}

function setButtons(busy) {
  const sendBtn = el("chatSend");
  const stopBtn = el("chatStop");
  sendBtn.disabled = busy;
  stopBtn.hidden = !busy;
}

function resizeInput() {
  const input = el("chatInput");
  input.style.height = "auto";
  input.style.height = Math.min(input.scrollHeight, 160) + "px";
}

function bind() {
  el("chatForm").addEventListener("submit", function (event) {
    event.preventDefault();
    sendMessage(el("chatInput").value);
  });

  el("chatInput").addEventListener("keydown", function (event) {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      sendMessage(el("chatInput").value);
    }
  });
  el("chatInput").addEventListener("input", resizeInput);

  el("chatStop").addEventListener("click", function () {
    if (state.controller) {
      state.controller.abort();
    }
  });

  el("userIdInput").addEventListener("change", function (event) {
    const value = Number(event.target.value || "0");
    state.userId = Number.isFinite(value) && value >= 0 ? Math.floor(value) : 0;
    localStorage.setItem(USER_KEY, String(state.userId));
    refreshSessionChips();
    addEvent("session", "user_id 已切到 " + state.userId, "");
  });

  el("suggestions").addEventListener("click", function (event) {
    const text = event.target.getAttribute("data-suggest");
    if (text) {
      sendMessage(text);
    }
  });

  document.querySelector("[data-new-conversation]").addEventListener("click", function () {
    ensureConversation(true);
    refreshSessionChips();
    addEvent("session", "已开启新会话 " + state.conversationId.slice(0, 8), "ok");
    toast("已开启新会话", "ok");
  });

  document.querySelector("[data-refresh-status]").addEventListener("click", function () {
    checkBackend().then(loadAgents);
    loadMetrics();
  });

  document.querySelectorAll("[data-eval]").forEach(function (button) {
    button.addEventListener("click", function () {
      runEvaluation(button.getAttribute("data-eval") === "judge");
    });
  });
}

function init() {
  bind();
  refreshSessionChips();
  resizeInput();
  checkBackend();
  loadAgents();
  loadMetrics();
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", init);
} else {
  init();
}
