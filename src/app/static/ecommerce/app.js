/**
 * 电商导购智能体测试台前端逻辑。
 *
 * 覆盖：
 * 1. 认证：调用 /api/v1/auth/login 与 /api/v1/auth/register 换取 Bearer 令牌，
 *    令牌存 localStorage，对话请求带上 Authorization 头。
 * 2. 会话：会话 id 由前端生成并持久化，作为后端短期记忆的 thread_id；
 *    「新会话」换 id，用于验证长期记忆是否跨会话生效。
 * 3. 对话：POST /api/v1/ecommerce/chat/stream 返回 SSE，前端逐段渲染，
 *    支持中途停止。
 */

"use strict";

const API_BASE = "/api/v1";
const CHAT_PATH = API_BASE + "/ecommerce/chat/stream";
const TOKEN_KEY = "ecommerce.token";
const USER_KEY = "ecommerce.username";
const CONV_KEY = "ecommerce.conversationId";

const state = {
  token: localStorage.getItem(TOKEN_KEY) || "",
  username: localStorage.getItem(USER_KEY) || "",
  conversationId: localStorage.getItem(CONV_KEY) || "",
  controller: null,
};

/* ---------------- 基础工具 ---------------- */

function escapeHtml(text) {
  return String(text ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

let toastTimer = null;
function toast(message, kind = "") {
  const box = document.getElementById("toast");
  if (!box) {
    return;
  }
  box.textContent = message;
  box.className = "toast" + (kind ? " " + kind : "");
  box.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    box.hidden = true;
  }, 4500);
}

function setChip(name, value, kind = "") {
  const chip = document.querySelector('[data-chip="' + name + '"]');
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

/* ---------------- 接口调用 ---------------- */

async function readError(response) {
  const raw = await response.text();
  try {
    const data = JSON.parse(raw);
    if (data && data.detail) {
      return typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail);
    }
  } catch {
    /* 非 JSON，按原文返回 */
  }
  return raw || "HTTP " + response.status;
}

async function apiPost(path, body, withToken = false) {
  const headers = { "Content-Type": "application/json", Accept: "application/json" };
  if (withToken && state.token) {
    headers.Authorization = "Bearer " + state.token;
  }
  let response;
  try {
    response = await fetch(API_BASE + path, { method: "POST", headers, body: JSON.stringify(body) });
  } catch (error) {
    throw new Error("无法连接后端服务：" + (error instanceof Error ? error.message : String(error)));
  }
  if (!response.ok) {
    throw new Error(await readError(response));
  }
  return response.json();
}

/* ---------------- 登录状态 ---------------- */

function refreshChips() {
  if (state.token) {
    setChip("auth", state.username ? "已登录 · " + state.username : "已登录", "ok");
  } else {
    setChip("auth", "未登录", "warn");
  }
  setChip("conv", state.conversationId ? state.conversationId.slice(0, 8) : "—", state.conversationId ? "ok" : "");
  const chip = document.querySelector('[data-chip="conv"]');
  if (chip) {
    chip.title = state.conversationId || "尚未创建会话";
  }
}

async function checkBackend() {
  try {
    const response = await fetch(API_BASE + "/health", { headers: { Accept: "application/json" } });
    if (!response.ok) {
      setChip("api", "异常 " + response.status, "bad");
      return false;
    }
    setChip("api", "可用", "ok");
    return true;
  } catch {
    setChip("api", "连不上", "bad");
    return false;
  }
}

function saveSession(data) {
  state.token = data.access_token;
  state.username = document.getElementById("username").value.trim();
  localStorage.setItem(TOKEN_KEY, state.token);
  localStorage.setItem(USER_KEY, state.username);
  refreshChips();
}

function clearSession() {
  state.token = "";
  localStorage.removeItem(TOKEN_KEY);
  refreshChips();
}

/** 在登录卡片里显示一行结果，方便用户看到到底发生了什么。 */
function showLoginResult(text, kind = "") {
  const box = document.getElementById("loginResult");
  if (!box) {
    return;
  }
  box.textContent = text;
  box.className = "login-result" + (kind ? " " + kind : "");
  box.hidden = !text;
}

function readCredentials() {
  const username = document.getElementById("username").value.trim();
  const password = document.getElementById("password").value;
  if (username.length < 3) {
    toast("用户名至少 3 个字符", "error");
    return null;
  }
  if (password.length < 6) {
    toast("密码至少 6 个字符", "error");
    return null;
  }
  return { username, password };
}

/**
 * 一键登录：先用账号密码登录；如果账号还不存在（后端返回「用户名或密码错误」），
 * 自动注册一次再登录，避免第一次使用的人卡在「登不上」。
 */
async function loginOrRegister() {
  const creds = readCredentials();
  if (!creds) {
    return;
  }
  showLoginResult("正在登录…");
  try {
    const data = await apiPost("/auth/login", creds);
    saveSession(data);
    showLoginResult("已登录：" + creds.username + "（user_id=" + data.user_id + "）", "ok");
    toast("登录成功", "ok");
    return;
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    // 账号不存在和密码错误后端返回同一句文案，这里统一尝试注册一次
    if (!/用户名或密码错误/.test(message)) {
      showLoginResult("登录失败：" + message, "bad");
      toast(message, "error");
      return;
    }
  }

  try {
    const data = await apiPost("/auth/register", creds);
    saveSession(data);
    showLoginResult("账号不存在，已自动注册并登录：" + creds.username + "（user_id=" + data.user_id + "）", "ok");
    toast("已自动注册并登录", "ok");
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    if (/用户名已存在/.test(message)) {
      const hint = "用户名「" + creds.username + "」已存在，但密码不对：请检查密码，或换个用户名。";
      showLoginResult(hint, "bad");
      toast("该用户名已存在，密码不对", "error");
      return;
    }
    showLoginResult("注册失败：" + message, "bad");
    toast(message, "error");
  }
}

/* ---------------- 会话与消息 ---------------- */

function ensureConversation(forceNew = false) {
  if (forceNew || !state.conversationId) {
    state.conversationId = crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random();
    localStorage.setItem(CONV_KEY, state.conversationId);
  }
  refreshChips();
  return state.conversationId;
}

function logBox() {
  return document.getElementById("chatLog");
}

function clearLogIfEmpty() {
  const box = logBox();
  const empty = box.querySelector(".empty-state");
  if (empty) {
    empty.remove();
  }
  return box;
}

function appendMessage(role, text, options = {}) {
  const box = clearLogIfEmpty();
  const wrap = document.createElement("article");
  wrap.className = "msg " + role + (options.error ? " error" : "");

  const avatar = document.createElement("span");
  avatar.className = "avatar";
  avatar.textContent = role === "user" ? "🙋" : "🛒";

  const content = document.createElement("div");
  const bubble = document.createElement("div");
  bubble.className = "bubble" + (options.typing ? " typing" : "");
  bubble.textContent = text;
  content.appendChild(bubble);

  if (options.meta) {
    const meta = document.createElement("div");
    meta.className = "meta";
    meta.textContent = options.meta;
    content.appendChild(meta);
  }

  wrap.appendChild(avatar);
  wrap.appendChild(content);
  box.appendChild(wrap);
  box.scrollTop = box.scrollHeight;
  return bubble;
}

/** 解析一段 SSE 帧（形如 data: {...}），交给 onEvent 处理。 */
function handleFrame(frame, onEvent) {
  const lines = frame.split("\n");
  const chunks = [];
  lines.forEach((line) => {
    if (line.startsWith("data:")) {
      chunks.push(line.slice(5).trim());
    }
  });
  const payload = chunks.join("");
  if (!payload) {
    return;
  }
  try {
    onEvent(JSON.parse(payload));
  } catch {
    /* 忽略无法解析的心跳帧 */
  }
}

async function sendMessage(text) {
  const message = String(text ?? "").trim();
  if (!message) {
    return;
  }
  if (!state.token) {
    toast("请先登录获取令牌", "error");
    return;
  }

  const conversationId = ensureConversation(false);
  appendMessage("user", message, { meta: new Date().toLocaleTimeString("zh-CN", { hour12: false }) });
  const bubble = appendMessage("assistant", "思考中…", { typing: true });

  const sendBtn = document.getElementById("chatSend");
  const stopBtn = document.getElementById("chatStop");
  sendBtn.disabled = true;
  stopBtn.hidden = false;
  state.controller = new AbortController();

  let answer = "";
  let finished = false;

  try {
    const response = await fetch(CHAT_PATH, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Accept: "text/event-stream",
        Authorization: "Bearer " + state.token,
      },
      body: JSON.stringify({ conversation_id: conversationId, message }),
      signal: state.controller.signal,
    });

    if (!response.ok) {
      throw new Error(await readError(response));
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder("utf-8");
    let buffer = "";

    while (!finished) {
      const { value, done } = await reader.read();
      if (done) {
        break;
      }
      buffer += decoder.decode(value, { stream: true });
      let index = buffer.indexOf("\n\n");
      while (index >= 0) {
        const frame = buffer.slice(0, index);
        buffer = buffer.slice(index + 2);
        handleFrame(frame, (event) => {
          if (event.conversation_id) {
            state.conversationId = event.conversation_id;
            localStorage.setItem(CONV_KEY, state.conversationId);
            refreshChips();
          }
          if (event.text) {
            answer += event.text;
            bubble.classList.remove("typing");
            bubble.textContent = answer;
            logBox().scrollTop = logBox().scrollHeight;
          }
          if (event.done) {
            finished = true;
          }
        });
        index = buffer.indexOf("\n\n");
      }
    }

    if (!answer) {
      bubble.classList.remove("typing");
      bubble.textContent = "（本轮没有返回文本，请查看后端控制台日志）";
    }
  } catch (error) {
    bubble.classList.remove("typing");
    if (error instanceof DOMException && error.name === "AbortError") {
      bubble.textContent = answer || "（已停止）";
    } else {
      const message = error instanceof Error ? error.message : String(error);
      bubble.closest(".msg").classList.add("error");
      bubble.textContent = answer ? answer + "\n\n[出错] " + message : "请求失败：" + message;
      toast(message, "error");
    }
  } finally {
    bubble.classList.remove("typing");
    sendBtn.disabled = false;
    stopBtn.hidden = true;
    state.controller = null;
  }
}

/* ---------------- 事件绑定 ---------------- */

function bind() {
  document.getElementById("loginForm").addEventListener("submit", (event) => {
    event.preventDefault();
    loginOrRegister();
  });
  document.querySelector("[data-logout]").addEventListener("click", () => {
    clearSession();
    toast("已退出登录", "ok");
  });

  document.querySelector("[data-refresh-status]").addEventListener("click", async () => {
    await checkBackend();
    refreshChips();
  });

  document.querySelector("[data-new-conversation]").addEventListener("click", () => {
    ensureConversation(true);
    logBox().innerHTML = '<div class="empty-state">已开启新会话，先前的对话仍留在短期记忆里，但当前会话不会带上它们。</div>';
    toast("已新建会话：" + state.conversationId.slice(0, 8), "ok");
  });

  const form = document.getElementById("chatForm");
  const input = document.getElementById("chatInput");
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const text = input.value;
    input.value = "";
    sendMessage(text);
  });
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      form.requestSubmit();
    }
  });
  input.addEventListener("input", () => {
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, 160) + "px";
  });

  document.getElementById("chatStop").addEventListener("click", () => {
    if (state.controller) {
      state.controller.abort();
    }
  });

  document.getElementById("suggestions").addEventListener("click", (event) => {
    const button = event.target.closest("button");
    if (!button) {
      return;
    }
    sendMessage(button.textContent);
  });

  if (window.location.protocol === "file:") {
    toast("请通过 http://127.0.0.1:8000/ecommerce/ 打开本页面，直接双击文件请求不到接口。", "error");
  }

  if (state.username) {
    document.getElementById("username").value = state.username;
  }
  ensureConversation(false);
  refreshChips();
  checkBackend();
}

document.addEventListener("DOMContentLoaded", bind);
