/**
 * 钉协助手智能体 —— 两个页面共用的前端逻辑。
 *
 * 覆盖的能力：
 * 1. 接口封装：统一前缀 ``/api/v1/dingtalk``，GET 走查询串、POST 走 JSON。
 * 2. 声明式表单绑定：页面里任何带 ``data-action`` 的 form 都会被自动接管，
 *    ``data-action="get"`` 触发查询，``data-action="post"`` 触发写操作。
 * 3. 写操作的确认语义：表单里的 ``confirm`` 复选框对应后端 ``confirm`` 字段，
 *    未勾选时不追加 ``--yes``，钉钉侧不会真正落库。
 * 4. 结果输出：把 dws CLI 的原始文本渲染到等宽结果区，并支持一键复制。
 * 5. 顶部状态徽标：dws CLI 可用性（GET /health）与登录状态（GET /auth/status）。
 */

"use strict";

const API_BASE = "/api/v1/dingtalk";

/** 页面不是通过后端服务打开时的统一提示（此时接口必然 404）。 */
const SERVE_HINT =
  "页面没有连上后端接口（请求返回 404）。请先启动后端服务，再用浏览器打开 " +
  "http://127.0.0.1:8000/dingtalk/ 访问本页面；直接双击 HTML 文件或在 IDE 里静态预览是请求不到接口的。";

/** 后端 500 的统一解释（真实原因在后端控制台日志里）。 */
const SERVER_HINT =
  "服务端执行失败。常见原因：dws 未登录、未开启写操作（DINGTALK_ENABLE_WRITE）、" +
  "命令超时或参数不合法，详情请看后端控制台日志。";

/* ---------------- 基础工具 ---------------- */

/** 转义 HTML，避免接口或大模型返回的内容注入页面。 */
function escapeHtml(text) {
  return String(text ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

/** 把后端错误转成更容易理解的中文提示。 */
function friendlyError(message) {
  const text = String(message ?? "").trim();
  if (/<!doctype html|404 Not Found/i.test(text)) {
    return SERVE_HINT;
  }
  if (!text || /Internal Server Error|Bad Gateway|Service Unavailable/i.test(text)) {
    return SERVER_HINT;
  }
  return text;
}

/** 当前时间的 HH:MM:SS，用于结果区的时间戳。 */
function clockNow() {
  return new Date().toLocaleTimeString("zh-CN", { hour12: false });
}

/** 底部提示条。 */
let toastTimer = null;
function toast(message, kind = "") {
  const box = document.getElementById("toast");
  if (!box) {
    return;
  }
  box.textContent = message;
  box.className = `toast${kind ? ` ${kind}` : ""}`;
  box.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    box.hidden = true;
  }, 5000);
}

/** 复制文本到剪贴板，失败时降级为选中文本提示。 */
async function copyText(text, button) {
  const value = String(text ?? "");
  if (!value) {
    toast("没有可复制的内容");
    return;
  }
  try {
    await navigator.clipboard.writeText(value);
  } catch {
    const area = document.createElement("textarea");
    area.value = value;
    area.style.position = "fixed";
    area.style.opacity = "0";
    document.body.appendChild(area);
    area.select();
    document.execCommand("copy");
    area.remove();
  }
  if (button) {
    const original = button.textContent;
    button.textContent = "已复制";
    setTimeout(() => {
      button.textContent = original;
    }, 1500);
  }
}

/* ---------------- 接口封装 ---------------- */

/** 发起一次请求；非 2xx 时抛出后端 detail 文案。 */
async function request(path, options = {}) {
  const method = options.method || "GET";
  const url = new URL(API_BASE + path, window.location.origin);
  Object.entries(options.params || {}).forEach(([key, value]) => {
    url.searchParams.set(key, String(value));
  });

  const init = { method, headers: { Accept: "application/json" } };
  if (method !== "GET") {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(options.body || {});
  }

  let response;
  try {
    response = await fetch(url.toString(), init);
  } catch (error) {
    throw new Error(`无法连接后端服务：${error instanceof Error ? error.message : String(error)}`);
  }

  const raw = await response.text();
  let data = null;
  try {
    data = raw ? JSON.parse(raw) : null;
  } catch {
    data = null;
  }

  if (!response.ok) {
    const detail = data && data.detail ? data.detail : raw;
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return data;
}

/** GET 查询。 */
function apiGet(path, params) {
  return request(path, { method: "GET", params });
}

/** POST 提交。 */
function apiPost(path, body) {
  return request(path, { method: "POST", body });
}

/* ---------------- 结果区渲染 ---------------- */

/** 把接口返回值压成用于展示的文本。 */
function payloadText(data) {
  if (data === null || data === undefined) {
    return "";
  }
  if (typeof data === "string") {
    return data;
  }
  if (typeof data === "object") {
    if ("result" in data) {
      const result = data.result;
      return typeof result === "string" ? result : JSON.stringify(result, null, 2);
    }
    return JSON.stringify(data, null, 2);
  }
  return String(data);
}

/** 找到表单/卡片内的结果容器。 */
function resultBox(scope) {
  return scope.querySelector("[data-result]");
}

/** 结果区进入「执行中」状态。 */
function showPending(box, text) {
  box.hidden = false;
  box.innerHTML = `<div class="result-pending">${escapeHtml(text)}</div>`;
}

/**
 * 渲染一次执行结果。
 *
 * @param {HTMLElement} box 结果容器。
 * @param {{state: string, text: string, hint?: string}} payload
 *   state 取 ok（成功）/ preview（已提交但未确认）/ bad（失败）。
 */
function showResult(box, payload) {
  const labels = { ok: "执行成功", preview: "预演 · 未落库", bad: "执行失败" };
  const state = labels[payload.state] ? payload.state : "ok";
  const text = String(payload.text ?? "").trim() || "（命令执行完毕，但没有返回内容）";

  box.hidden = false;
  box.innerHTML = "";

  const head = document.createElement("div");
  head.className = "result-head";
  head.innerHTML = `
    <span class="result-state ${state}">${labels[state]}</span>
    <span class="result-time">${clockNow()}</span>
    <span class="flex-spacer"></span>
  `;
  const copyBtn = document.createElement("button");
  copyBtn.type = "button";
  copyBtn.className = "ghost-btn";
  copyBtn.textContent = "复制";
  copyBtn.addEventListener("click", () => copyText(text, copyBtn));
  head.appendChild(copyBtn);

  const body = document.createElement("pre");
  body.className = "result-body";
  body.textContent = payload.hint ? `${payload.hint}\n\n${text}` : text;

  box.appendChild(head);
  box.appendChild(body);
}

/* ---------------- 声明式表单绑定 ---------------- */

/**
 * 收集表单字段。
 *
 * - 复选框统一转成布尔值（对应后端的 bool 参数，如 confirm / sticky）；
 * - 文本空值不下发，让后端使用默认值；
 * - 其余按字符串提交。
 */
function collectFields(form) {
  const data = {};
  form.querySelectorAll("[name]").forEach((element) => {
    const name = element.name;
    if (!name) {
      return;
    }
    if (element.type === "checkbox") {
      data[name] = element.checked;
      return;
    }
    const value = String(element.value ?? "").trim();
    if (value === "") {
      return;
    }
    data[name] = value;
  });
  return data;
}

/** 为单个 form 绑定提交行为。 */
function bindForm(form) {
  if (form.dataset.bound === "1") {
    return;
  }
  form.dataset.bound = "1";

  const action = form.dataset.action;
  const path = form.dataset.path;
  const box = resultBox(form);
  const button = form.querySelector('button[type="submit"]');

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!path || !box || form.dataset.pending === "1") {
      return;
    }

    const fields = collectFields(form);
    const isWrite = action === "post";

    form.dataset.pending = "1";
    if (button) {
      button.disabled = true;
      button.dataset.label = button.textContent;
      button.textContent = "执行中…";
    }
    showPending(box, `正在调用 ${isWrite ? "POST" : "GET"} ${path} …`);

    try {
      const data = isWrite ? await apiPost(path, fields) : await apiGet(path, fields);
      const confirmed = fields.confirm === true;
      showResult(box, {
        state: isWrite && !confirmed ? "preview" : "ok",
        text: payloadText(data),
        hint:
          isWrite && !confirmed
            ? "提示：未勾选「已确认执行」，后端本次以 --dry-run 预演，上面是即将执行的内容，钉钉侧没有产生任何改动。"
            : "",
      });
      form.dispatchEvent(new CustomEvent("dws:success", { bubbles: true, detail: { fields, data } }));
    } catch (error) {
      const message = friendlyError(error instanceof Error ? error.message : String(error));
      showResult(box, { state: "bad", text: message });
      toast(message, "error");
    } finally {
      form.dataset.pending = "0";
      if (button) {
        button.disabled = false;
        button.textContent = button.dataset.label || "提交";
      }
    }
  });
}

/** 接管页面上所有声明式表单。 */
function bindForms(root = document) {
  root.querySelectorAll("form[data-action]").forEach(bindForm);
}

/* ---------------- 顶部状态徽标 ---------------- */

/** 更新一个状态徽标。 */
function setChip(name, value, kind = "") {
  const chip = document.querySelector(`[data-chip="${name}"]`);
  if (!chip) {
    return;
  }
  chip.classList.remove("ok", "warn", "bad");
  if (kind) {
    chip.classList.add(kind);
  }
  const holder = chip.querySelector("[data-value]");
  if (holder) {
    holder.textContent = value;
  }
}

/** 查询 dws CLI 可用性。 */
async function refreshDwsStatus() {
  try {
    const data = await apiGet("/health");
    if (data && data.dws_available) {
      setChip("dws", "可用", "ok");
    } else {
      setChip("dws", "未安装", "bad");
    }
    return Boolean(data && data.dws_available);
  } catch {
    setChip("dws", "无法检测", "bad");
    return false;
  }
}

/** 查询钉钉登录状态。 */
async function refreshAuthStatus() {
  try {
    const data = await apiGet("/auth/status");
    const status = data || {};
    const authenticated = Boolean(
      status.authenticated ?? status.logged_in ?? status.is_authenticated ?? status.isLogin,
    );
    const name = status.user_name || status.name || status.username || status.nick_name || "";

    if (authenticated) {
      setChip("auth", name ? `已登录 · ${name}` : "已登录", "ok");
    } else {
      setChip("auth", "未登录", "warn");
    }
    const chip = document.querySelector('[data-chip="auth"]');
    if (chip) {
      chip.title = JSON.stringify(status);
    }
    return authenticated;
  } catch (error) {
    setChip("auth", "状态未知", "bad");
    const chip = document.querySelector('[data-chip="auth"]');
    if (chip) {
      chip.title = error instanceof Error ? error.message : String(error);
    }
    return false;
  }
}

/** 触发扫码登录后轮询登录状态，登录成功即停止。 */
async function pollAuthStatus(rounds = 12, intervalMs = 5000) {
  for (let index = 0; index < rounds; index += 1) {
    await new Promise((resolve) => setTimeout(resolve, intervalMs));
    if (await refreshAuthStatus()) {
      toast("钉钉登录成功", "ok");
      return true;
    }
  }
  return false;
}

/* ---------------- 页面初始化 ---------------- */

/** 高亮当前菜单项。 */
function highlightMenu(page) {
  document.querySelectorAll(".menu a[data-page]").forEach((link) => {
    link.classList.toggle("active", link.dataset.page === page);
    if (link.dataset.page === page) {
      link.setAttribute("aria-current", "page");
    }
  });
}

/**
 * 初始化页面外壳：菜单高亮、状态徽标、声明式表单。
 *
 * @param {{page: string}} options page 取值 query / operations。
 */
function initShell(options = {}) {
  highlightMenu(options.page || "");
  bindForms(document);

  const refreshBtn = document.querySelector("[data-refresh-status]");
  if (refreshBtn) {
    refreshBtn.addEventListener("click", async () => {
      refreshBtn.disabled = true;
      await Promise.all([refreshDwsStatus(), refreshAuthStatus()]);
      refreshBtn.disabled = false;
    });
  }

  if (window.location.protocol === "file:") {
    toast(SERVE_HINT, "error");
  }

  refreshDwsStatus();
  refreshAuthStatus();
}

window.DWS = {
  apiGet,
  apiPost,
  copyText,
  escapeHtml,
  friendlyError,
  highlightMenu,
  initShell,
  payloadText,
  pollAuthStatus,
  refreshAuthStatus,
  refreshDwsStatus,
  showPending,
  showResult,
  toast,
};
