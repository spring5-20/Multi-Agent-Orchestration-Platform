"use strict";

/*
 * 智能问数（Text-to-SQL）前端逻辑。
 *
 * 覆盖：
 * 1. 问数：POST /api/v1/bi/chat，渲染中文结论、SQL、结果表格与图表；
 * 2. 多轮：conversation_id 存内存，追问「那平均值呢」会带上同一会话；
 * 3. 表结构：GET /api/v1/bi/schema 显示可问数表数量，POST /api/v1/bi/schema/refresh 刷新缓存；
 * 4. 审计：GET /api/v1/bi/logs 与 /logs/summary 展示总览、每日趋势与最近记录。
 */

const API_BASE = "/api/v1";
const CHAT_PATH = API_BASE + "/bi/chat";
const SCHEMA_PATH = API_BASE + "/bi/schema";
const SCHEMA_REFRESH_PATH = API_BASE + "/bi/schema/refresh";
const LOGS_PATH = API_BASE + "/bi/logs";
const LOGS_SUMMARY_PATH = API_BASE + "/bi/logs/summary";
const HEALTH_PATH = API_BASE + "/health";

let toastTimer = null;

const state = {
  conversationId: "",
  userId: 0,
  backendOk: false,
  busy: false,
};

function el(id) {
  return document.getElementById(id);
}

function toast(message, kind) {
  const box = el("toast");
  box.textContent = message;
  box.className = "toast" + (kind ? " " + kind : "");
  box.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(function () {
    box.hidden = true;
  }, 3200);
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

function esc(value) {
  return String(value === null || value === undefined ? "" : value).replace(/[&<>"']/g, function (ch) {
    if (ch === "&") {
      return "&amp;";
    }
    if (ch === "<") {
      return "&lt;";
    }
    if (ch === ">") {
      return "&gt;";
    }
    if (ch === '"') {
      return "&quot;";
    }
    return "&#39;";
  });
}

function shorten(text, limit) {
  const value = String(text || "").replace(/\s+/g, " ").trim();
  return value.length > limit ? value.slice(0, limit) + "…" : value;
}

async function readError(response) {
  try {
    const data = await response.json();
    return typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail || data);
  } catch (error) {
    return "HTTP " + response.status;
  }
}

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

async function loadSchema() {
  try {
    const response = await fetch(SCHEMA_PATH, { headers: { Accept: "application/json" } });
    if (!response.ok) {
      throw new Error(await readError(response));
    }
    const data = await response.json();
    const tables = Object.keys(data.schema || {});
    setChip("tables", String(tables.length), tables.length ? "ok" : "warn");
  } catch (error) {
    setChip("tables", "读取失败", "bad");
  }
}

function setBusy(busy) {
  state.busy = busy;
  const button = el("askButton");
  button.disabled = busy;
  button.textContent = busy ? "查询中…" : "问数";
}

async function askQuestion(question) {
  const value = String(question || "").trim();
  if (!value || state.busy) {
    return;
  }
  setBusy(true);
  el("errorBox").hidden = true;
  el("answerBox").hidden = true;
  try {
    const response = await fetch(CHAT_PATH, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify({
        question: value,
        conversation_id: state.conversationId || null,
        user_id: state.userId,
      }),
    });
    if (!response.ok) {
      throw new Error(await readError(response));
    }
    render(await response.json());
  } catch (error) {
    showError("问数失败：" + shorten(error.message, 160));
  } finally {
    setBusy(false);
  }
}

function showError(message) {
  const box = el("errorBox");
  box.textContent = message;
  box.hidden = false;
}

function render(data) {
  if (data.conversation_id) {
    state.conversationId = data.conversation_id;
    setChip("conv", data.conversation_id.slice(0, 8), "ok");
  }

  const answerBox = el("answerBox");
  el("answerText").textContent = data.answer || "没有生成结论。";
  answerBox.hidden = false;

  el("sqlBox").textContent = data.sql || "（本轮没有生成可执行的 SQL）";

  const columns = data.columns || [];
  const rows = data.rows || [];
  el("rowHint").textContent = "共 " + rows.length + " 行";
  el("tableBox").innerHTML = renderTable(columns, rows);
  el("chartBox").innerHTML = renderChart(data.chart);

  if (data.error) {
    showError("执行过程中出现问题：" + data.error);
  }
}

function renderTable(columns, rows) {
  if (!columns.length) {
    return '<div class="empty-state">没有查询到数据。</div>';
  }
  let html = "<table><thead><tr>";
  columns.forEach(function (column) {
    html += "<th>" + esc(column) + "</th>";
  });
  html += "</tr></thead><tbody>";
  if (!rows.length) {
    html += '<tr><td colspan="' + columns.length + '">没有符合条件的数据</td></tr>';
  }
  rows.forEach(function (row) {
    html += "<tr>";
    columns.forEach(function (column) {
      html += "<td>" + esc(row[column]) + "</td>";
    });
    html += "</tr>";
  });
  return html + "</tbody></table>";
}

function renderChart(chart) {
  if (!chart || !chart.series || !chart.series.length) {
    return '<div class="empty-state">本次结果没有可自动生成的图表。</div>';
  }
  const values = (chart.series[0].data || []).map(Number);
  const labels = chart.x_axis || [];
  const max = Math.max.apply(null, values.concat([1]));
  let html = '<div class="chart-row">';
  values.forEach(function (value, index) {
    const height = Math.max(Math.round((value / max) * 150), 2);
    html +=
      '<div class="chart-item"><div class="bar" style="height:' +
      height +
      'px"></div><div class="chart-value">' +
      esc(value) +
      '</div><div class="chart-label">' +
      esc(labels[index] === undefined ? index + 1 : labels[index]) +
      "</div></div>";
  });
  html += "</div>";
  return html;
}

function newConversation() {
  state.conversationId = "";
  setChip("conv", "新会话", "ok");
  el("answerBox").hidden = true;
  el("errorBox").hidden = true;
  el("sqlBox").textContent = "还没有执行过问数。";
  el("rowHint").textContent = "—";
  el("tableBox").innerHTML = '<div class="empty-state">问一次数据，结果会显示在这里。</div>';
  el("chartBox").innerHTML = '<div class="empty-state">暂无图表。</div>';
  toast("已开启新会话，上下文已清空", "ok");
}

async function refreshSchema() {
  try {
    const response = await fetch(SCHEMA_REFRESH_PATH, { method: "POST" });
    if (!response.ok) {
      throw new Error(await readError(response));
    }
    await loadSchema();
    toast("表结构缓存已刷新，新建表现在可被问数", "ok");
  } catch (error) {
    toast("刷新失败：" + shorten(error.message, 80), "error");
  }
}

async function loadLogs() {
  const card = el("logsCard");
  card.hidden = false;
  const statBox = el("logStats");
  statBox.innerHTML = '<div class="empty-state">加载中…</div>';
  try {
    const responses = await Promise.all([
      fetch(LOGS_SUMMARY_PATH, { headers: { Accept: "application/json" } }),
      fetch(LOGS_PATH + "?limit=20", { headers: { Accept: "application/json" } }),
    ]);
    const summary = await responses[0].json();
    const logs = await responses[1].json();
    statBox.innerHTML = renderStats(summary);
    el("dailyChart").innerHTML = renderDaily(summary.daily || []);
    el("logTable").innerHTML = renderLogTable(logs.items || []);
  } catch (error) {
    statBox.innerHTML = '<div class="empty-state">日志加载失败</div>';
  }
}

function statCard(label, value) {
  return '<div class="stat"><div class="stat-value">' + esc(value) + '</div><div class="stat-label">' + esc(label) + "</div></div>";
}

function renderStats(summary) {
  return (
    '<div class="stats-grid">' +
    statCard("总问数", summary.total) +
    statCard("成功率", summary.success_rate + "%") +
    statCard("平均耗时", summary.avg_elapsed_ms + " ms") +
    statCard("平均行数", summary.avg_row_count) +
    statCard("成功", summary.success) +
    statCard("失败", summary.error) +
    "</div>"
  );
}

function renderDaily(daily) {
  if (!daily.length) {
    return '<div class="empty-state">暂无每日趋势数据。</div>';
  }
  const max = Math.max.apply(null, daily.map(function (item) { return item.count; }).concat([1]));
  let html = '<div class="chart-row">';
  daily.forEach(function (item) {
    const height = Math.max(Math.round((item.count / max) * 130), 4);
    html +=
      '<div class="chart-item"><div class="bar" style="height:' +
      height +
      'px"></div><div class="chart-value">' +
      esc(item.count) +
      '</div><div class="chart-label">' +
      esc(String(item.day).slice(5)) +
      "</div></div>";
  });
  return html + "</div>";
}

function renderLogTable(items) {
  if (!items.length) {
    return '<div class="empty-state">暂无问数记录。</div>';
  }
  let html = "<table><thead><tr><th>时间</th><th>用户</th><th>问题</th><th>状态</th><th>行数</th><th>耗时(ms)</th></tr></thead><tbody>";
  items.forEach(function (item) {
    html +=
      "<tr><td>" +
      esc(String(item.created_at || "").replace("T", " ").slice(0, 19)) +
      "</td><td>" +
      esc(item.user_id) +
      "</td><td>" +
      esc(shorten(item.question, 36)) +
      "</td><td>" +
      (item.status === "success" ? "成功" : '<span class="bad">失败</span>') +
      "</td><td>" +
      esc(item.row_count) +
      "</td><td>" +
      esc(item.elapsed_ms) +
      "</td></tr>";
  });
  return html + "</tbody></table>";
}

function bind() {
  el("askForm").addEventListener("submit", function (event) {
    event.preventDefault();
    askQuestion(el("questionInput").value);
  });

  el("questionInput").addEventListener("keydown", function (event) {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      askQuestion(el("questionInput").value);
    }
  });

  el("userIdInput").addEventListener("change", function (event) {
    const value = Number(event.target.value || "0");
    state.userId = Number.isFinite(value) && value >= 0 ? Math.floor(value) : 0;
    setChip("user", String(state.userId));
  });

  el("suggestions").addEventListener("click", function (event) {
    const text = event.target.getAttribute("data-suggest");
    if (text) {
      el("questionInput").value = text;
      askQuestion(text);
    }
  });

  document.querySelector("[data-refresh-schema]").addEventListener("click", refreshSchema);
  document.querySelector("[data-new-conversation]").addEventListener("click", newConversation);
  document.querySelector("[data-toggle-logs]").addEventListener("click", function () {
    const card = el("logsCard");
    if (card.hidden) {
      loadLogs();
    } else {
      card.hidden = true;
    }
  });
}

function init() {
  bind();
  setChip("user", String(state.userId));
  setChip("conv", "新会话", "ok");
  checkBackend();
  loadSchema();
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", init);
} else {
  init();
}
