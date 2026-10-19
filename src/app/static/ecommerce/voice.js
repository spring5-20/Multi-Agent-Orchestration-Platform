/**
 * 实时语音对话页面逻辑。
 *
 * 与后端 src/app/api/v1/ecommerce_voice.py + voice/realtime.py 的协议约定：
 *
 * 客户端 → 服务端
 *   - 二进制帧：16kHz 单声道 16bit 小端 PCM 音频
 *   - 文本帧：{"type":"start_turn"} 本句开始（后端忽略，用于语义清晰）
 *             {"type":"end_turn"}   本句说完：后端提交音频并开始回答
 *             {"type":"close"}      主动结束会话
 *
 * 服务端 → 客户端
 *   - {"type":"ready","silence_duration_ms":N}  识别会话就绪，可以送音频
 *   - {"type":"transcript","text":...}          实时转写文本（边说边出）
 *   - {"type":"reply_start","user_text":...}    本句定稿，开始生成回答
 *   - {"type":"audio","audio_base64":...,"audio_url":...,"audio_format":...}
 *   - {"type":"reply","user_text":...,"text":...}  本轮回答文本，也是本轮结束标志
 *   - {"type":"idle"}                           没有识别到内容
 *   - {"type":"error","message":...}
 *
 * 断句：manual 模式下由前端决定句子边界。「按住说话」按下开始、松手 end_turn；
 * 「连续对话」用能量 VAD，说话后静音超过 silence_duration_ms 就 end_turn。
 */
"use strict";

const API_BASE = "/api/v1";
const VOICE_WS_PATH = API_BASE + "/ecommerce/voice/realtime";
const TOKEN_KEY = "ecommerce.token";
const USER_KEY = "ecommerce.username";

const TARGET_SAMPLE_RATE = 16000;
const FRAME_SIZE = 1024;
const FRAME_MS = (FRAME_SIZE / TARGET_SAMPLE_RATE) * 1000;
const MIN_SPEECH_MS = 120;
const TAIL_GUARD_MS = 260;
// 滑块 1..10 对应由「不容易触发」到「很容易触发」的能量阈值
const THRESHOLDS = [0.06, 0.045, 0.035, 0.028, 0.022, 0.017, 0.013, 0.01, 0.007, 0.005];

const state = {
  token: localStorage.getItem(TOKEN_KEY) || "",
  username: localStorage.getItem(USER_KEY) || "",
  socket: null,
  asrReady: false,
  busy: false,
  mode: "hold",
  holding: false,
  capture: false,
  audioContext: null,
  mediaStream: null,
  workletNode: null,
  resample: null,
  turnOpen: false,
  speechMs: 0,
  lastVoiceMs: 0,
  silenceMs: 800,
  threshold: THRESHOLDS[3],
  playQueue: [],
  playing: false,
  liveBubble: null,
  replyBubble: null,
  turnHadAudio: false,
};

/* ---------------- 基础工具 ---------------- */

let toastTimer = null;
function toast(message, kind) {
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

function setChip(name, value, kind) {
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

function setMicState(text) {
  const node = document.getElementById("micState");
  if (node) {
    node.textContent = text;
  }
}

function setTurnState(text) {
  const node = document.getElementById("turnState");
  if (node) {
    node.textContent = "当前轮次：" + text;
  }
}

function updateLevel(rms) {
  const bar = document.getElementById("levelBar");
  if (!bar) {
    return;
  }
  const percent = Math.max(0, Math.min(100, Math.round((rms / 0.12) * 100)));
  bar.style.width = percent + "%";
}

/* ---------------- 登录 ---------------- */

async function readError(response) {
  const raw = await response.text();
  try {
    const data = JSON.parse(raw);
    if (data && data.detail) {
      return typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail);
    }
  } catch (error) {
    /* 非 JSON，按原文返回 */
  }
  return raw || "HTTP " + response.status;
}

async function apiPost(path, body) {
  const response = await fetch(API_BASE + path, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    throw new Error(await readError(response));
  }
  return response.json();
}

function showLoginResult(text, kind) {
  const box = document.getElementById("loginResult");
  if (!box) {
    return;
  }
  box.textContent = text;
  box.className = "login-result" + (kind ? " " + kind : "");
  box.hidden = !text;
}

function refreshChips() {
  if (state.token) {
    setChip("auth", state.username ? "已登录 · " + state.username : "已登录", "ok");
  } else {
    setChip("auth", "未登录", "warn");
  }
}

async function checkBackend() {
  try {
    const response = await fetch(API_BASE + "/health", { headers: { Accept: "application/json" } });
    setChip("api", response.ok ? "可用" : "异常 " + response.status, response.ok ? "ok" : "bad");
  } catch (error) {
    setChip("api", "连不上", "bad");
  }
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
  return { username: username, password: password };
}

/** 一键登录：账号不存在时自动注册，避免第一次使用的人卡住。 */
async function loginOrRegister() {
  const creds = readCredentials();
  if (!creds) {
    return;
  }
  showLoginResult("正在登录…");
  try {
    const data = await apiPost("/auth/login", creds);
    saveSession(data, creds.username);
    showLoginResult("已登录：" + creds.username + "（user_id=" + data.user_id + "）", "ok");
    toast("登录成功", "ok");
    connect();
    return;
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    if (!/用户名或密码错误/.test(message)) {
      showLoginResult("登录失败：" + message, "bad");
      toast(message, "error");
      return;
    }
  }

  try {
    const data = await apiPost("/auth/register", creds);
    saveSession(data, creds.username);
    showLoginResult("账号不存在，已自动注册并登录：" + creds.username, "ok");
    toast("已自动注册并登录", "ok");
    connect();
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    showLoginResult("注册失败：" + message, "bad");
    toast(message, "error");
  }
}

function saveSession(data, username) {
  state.token = data.access_token;
  state.username = username;
  localStorage.setItem(TOKEN_KEY, state.token);
  localStorage.setItem(USER_KEY, state.username);
  refreshChips();
}

function logout() {
  state.token = "";
  state.username = "";
  localStorage.removeItem(TOKEN_KEY);
  localStorage.removeItem(USER_KEY);
  refreshChips();
  disconnect("已退出登录");
  toast("已退出登录", "ok");
}

/* ---------------- WebSocket ---------------- */

function wsUrl() {
  const scheme = window.location.protocol === "https:" ? "wss:" : "ws:";
  return scheme + "//" + window.location.host + VOICE_WS_PATH + "?token=" + encodeURIComponent(state.token);
}

function connect() {
  if (!state.token) {
    toast("请先登录获取令牌", "error");
    return;
  }
  if (state.socket && (state.socket.readyState === WebSocket.OPEN || state.socket.readyState === WebSocket.CONNECTING)) {
    return;
  }
  setChip("ws", "连接中…", "warn");
  setChip("asr", "未就绪");
  const socket = new WebSocket(wsUrl());
  socket.binaryType = "arraybuffer";
  state.socket = socket;

  socket.addEventListener("open", () => {
    setChip("ws", "已连接", "ok");
    setMicState("正在建立识别会话…稍等一两秒，就绪后即可说话。");
  });
  socket.addEventListener("message", onSocketMessage);
  socket.addEventListener("error", () => setChip("ws", "连接出错", "bad"));
  socket.addEventListener("close", (event) => {
    state.socket = null;
    state.asrReady = false;
    state.busy = false;
    state.turnOpen = false;
    state.holding = false;
    setChip("asr", "未就绪");
    if (event.code === 4401 || event.code === 1008) {
      setChip("ws", "未认证(" + event.code + ")", "bad");
      setMicState("连接被拒绝：令牌缺失或已过期，请重新登录后再连接。");
    } else {
      setChip("ws", "未连接");
      setMicState("语音连接已断开，点「连接语音」重新建立会话。");
    }
    stopCapture();
    updateMicUi();
  });
}

function disconnect(reason) {
  const socket = state.socket;
  if (socket && socket.readyState === WebSocket.OPEN) {
    try {
      socket.send(JSON.stringify({ type: "close" }));
    } catch (error) {
      /* 连接可能刚好断开，忽略 */
    }
    socket.close();
  }
  state.socket = null;
  state.asrReady = false;
  state.busy = false;
  stopCapture();
  setChip("ws", "未连接");
  setChip("asr", "未就绪");
  setMicState(reason || "语音连接已断开。");
  updateMicUi();
}

function sendControl(payload) {
  const socket = state.socket;
  if (!socket || socket.readyState !== WebSocket.OPEN) {
    return false;
  }
  socket.send(JSON.stringify(payload));
  return true;
}

function onSocketMessage(event) {
  if (typeof event.data !== "string") {
    return;
  }
  let data;
  try {
    data = JSON.parse(event.data);
  } catch (error) {
    return;
  }

  switch (data.type) {
    case "ready":
      state.asrReady = true;
      state.silenceMs = data.silence_duration_ms || state.silenceMs;
      setChip("asr", "已就绪", "ok");
      setMicState("识别会话已就绪，" + (state.mode === "auto" ? "点麦克风开始连续对话。" : "按住麦克风开始说话。"));
      break;
    case "transcript":
      showTranscript(data.text);
      break;
    case "reply_start":
      finalizeUserBubble(data.user_text);
      startAssistantBubble();
      setTurnState("生成回答中…");
      break;
    case "audio":
      state.turnHadAudio = true;
      enqueueAudio(data);
      break;
    case "reply":
      finishReply(data.text);
      break;
    case "idle":
      state.busy = false;
      setTurnState("空闲（没有识别到内容）");
      setMicState("没有识别到内容，再说一次试试。");
      break;
    case "error":
      state.busy = false;
      appendSystem(data.message);
      setMicState("后端返回错误：" + data.message);
      toast(data.message, "error");
      break;
    default:
      break;
  }
  updateMicUi();
}

/* ---------------- 对话记录 ---------------- */

function logBox() {
  const box = document.getElementById("chatLog");
  const empty = box.querySelector(".empty-state");
  if (empty) {
    empty.remove();
  }
  return box;
}

function appendBubble(role, text, avatar, typing) {
  const box = logBox();
  const wrap = document.createElement("article");
  wrap.className = "msg " + role;

  const avatarNode = document.createElement("span");
  avatarNode.className = "avatar";
  avatarNode.textContent = avatar;

  const content = document.createElement("div");
  const bubble = document.createElement("div");
  bubble.className = "bubble" + (typing ? " typing" : "");
  bubble.textContent = text;
  content.appendChild(bubble);

  wrap.appendChild(avatarNode);
  wrap.appendChild(content);
  box.appendChild(wrap);
  box.scrollTop = box.scrollHeight;
  return bubble;
}

function appendSystem(text) {
  appendBubble("system", text, "⚙️", false);
}

function showTranscript(text) {
  const value = String(text || "").trim();
  if (!value) {
    return;
  }
  if (!state.liveBubble) {
    state.liveBubble = appendBubble("user", value, "🙋", false);
  } else {
    state.liveBubble.textContent = value;
  }
  const box = logBox();
  box.scrollTop = box.scrollHeight;
}

function finalizeUserBubble(text) {
  const value = String(text || "").trim();
  if (state.liveBubble) {
    state.liveBubble.textContent = value || "（未识别到内容）";
    state.liveBubble = null;
    return;
  }
  if (value) {
    appendBubble("user", value, "🙋", false);
  }
}

function startAssistantBubble() {
  state.turnHadAudio = false;
  state.replyBubble = appendBubble("assistant", "思考中…", "🛒", true);
}

function finishReply(text) {
  const value = String(text || "").trim();
  if (state.replyBubble) {
    state.replyBubble.classList.remove("typing");
    state.replyBubble.textContent = value || "（本轮没有返回文本，请查看后端日志）";
    if (state.turnHadAudio) {
      state.replyBubble.classList.add("audio-mark");
    }
    state.replyBubble = null;
  }
  state.busy = false;
  setTurnState("空闲");
  setMicState(state.mode === "auto" ? "可以继续说下一句。" : "按住麦克风说下一句。");
}

/* ---------------- 音频回放 ---------------- */

function mimeOf(fmt) {
  if (fmt === "wav") {
    return "audio/wav";
  }
  if (fmt === "pcm") {
    return "audio/L16";
  }
  return "audio/mpeg";
}

function enqueueAudio(data) {
  let src = "";
  if (data.audio_base64) {
    src = "data:" + mimeOf(data.audio_format) + ";base64," + data.audio_base64;
  } else if (data.audio_url) {
    src = data.audio_url;
  }
  if (!src) {
    return;
  }
  state.playQueue.push(src);
  void playNextAudio();
}

async function playNextAudio() {
  if (state.playing) {
    return;
  }
  const src = state.playQueue.shift();
  if (!src) {
    return;
  }
  state.playing = true;
  const audio = new Audio(src);
  const done = () => {
    state.playing = false;
    void playNextAudio();
  };
  audio.addEventListener("ended", done, { once: true });
  audio.addEventListener("error", () => {
    toast("语音播放失败，可查看对话记录里的文字回复", "error");
    done();
  }, { once: true });
  try {
    await audio.play();
  } catch (error) {
    // 浏览器自动播放限制：需要用户先与页面交互（点麦克风即可解除）
    done();
  }
}

/* ---------------- 麦克风采集 ---------------- */

/** 线性插值重采样：把设备采样率的数据流换成 16kHz，避免浏览器忽略 sampleRate。 */
function createResampler(ratio) {
  let tail = null;
  let phase = 0;
  return function resample(input) {
    if (input.length === 0) {
      return input;
    }
    const buffer = new Float32Array(input.length + 1);
    buffer[0] = tail === null ? input[0] : tail;
    buffer.set(input, 1);
    const out = [];
    let x = phase;
    while (x < buffer.length - 1) {
      const index = Math.floor(x);
      const frac = x - index;
      out.push(buffer[index] * (1 - frac) + buffer[index + 1] * frac);
      x += ratio;
    }
    phase = x - (buffer.length - 1);
    tail = buffer[buffer.length - 1];
    return Float32Array.from(out);
  };
}

function encodePcm16(samples) {
  const view = new DataView(new ArrayBuffer(samples.length * 2));
  for (let i = 0; i < samples.length; i += 1) {
    const value = Math.max(-1, Math.min(1, samples[i]));
    view.setInt16(i * 2, value < 0 ? value * 0x8000 : value * 0x7fff, true);
  }
  return view.buffer;
}

async function startCapture() {
  if (state.capture) {
    return true;
  }
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    toast("当前浏览器不支持麦克风采集，请改用 Chrome / Edge", "error");
    return false;
  }
  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    });
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    toast("无法访问麦克风：" + message, "error");
    setMicState("无法访问麦克风。请确认已授权，并用 http://127.0.0.1 或 HTTPS 打开本页。");
    return false;
  }

  try {
    const context = new AudioContext({ sampleRate: TARGET_SAMPLE_RATE });
    await context.audioWorklet.addModule("./voice-worklet.js");
    const source = context.createMediaStreamSource(stream);
    const node = new AudioWorkletNode(context, "pcm-capture", {
      processorOptions: { frameSize: FRAME_SIZE },
    });
    const mute = context.createGain();
    mute.gain.value = 0; // 只把节点接进音频图，不把麦克风声音放出来避免回声
    source.connect(node).connect(mute).connect(context.destination);

    node.port.onmessage = (event) => onPcm(event.data);
    state.audioContext = context;
    state.mediaStream = stream;
    state.workletNode = node;
    state.resample =
      context.sampleRate === TARGET_SAMPLE_RATE ? null : createResampler(context.sampleRate / TARGET_SAMPLE_RATE);
    state.capture = true;
    setMicState("麦克风已开启（设备采样率 " + context.sampleRate + "Hz → 16kHz）。");
    return true;
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    toast("初始化麦克风失败：" + message, "error");
    if (stream) {
      stream.getTracks().forEach((track) => track.stop());
    }
    return false;
  }
}

async function stopCapture() {
  state.capture = false;
  if (state.workletNode) {
    state.workletNode.port.onmessage = null;
    state.workletNode.disconnect();
    state.workletNode = null;
  }
  if (state.audioContext) {
    try {
      await state.audioContext.close();
    } catch (error) {
      /* 关闭失败无需处理 */
    }
    state.audioContext = null;
  }
  if (state.mediaStream) {
    state.mediaStream.getTracks().forEach((track) => track.stop());
    state.mediaStream = null;
  }
  state.resample = null;
  updateLevel(0);
}

function computeRms(samples) {
  let sum = 0;
  for (let i = 0; i < samples.length; i += 1) {
    sum += samples[i] * samples[i];
  }
  return Math.sqrt(sum / samples.length);
}

function onPcm(rawSamples) {
  const socket = state.socket;
  if (!socket || socket.readyState !== WebSocket.OPEN || !state.asrReady) {
    return;
  }
  const samples = state.resample ? state.resample(rawSamples) : rawSamples;
  if (samples.length === 0) {
    return;
  }
  const rms = computeRms(samples);
  updateLevel(rms);

  if (rms >= state.threshold) {
    state.speechMs += FRAME_MS;
    state.lastVoiceMs = performance.now();
    // 连续对话模式下，检测到开口就自动开启新一轮
    if (!state.turnOpen && !state.busy) {
      beginTurn();
    }
  } else {
    state.speechMs = Math.max(0, state.speechMs - FRAME_MS);
  }

  socket.send(encodePcm16(samples));

  if (state.mode !== "auto" || !state.turnOpen) {
    return;
  }
  const quietFor = performance.now() - state.lastVoiceMs;
  if (state.speechMs >= MIN_SPEECH_MS && quietFor >= state.silenceMs + TAIL_GUARD_MS) {
    endTurn();
    setTurnState("已断句，等待回答…");
  }
}

/* ---------------- 轮次控制 ---------------- */

function beginTurn() {
  if (state.turnOpen) {
    return;
  }
  state.turnOpen = true;
  state.speechMs = 0;
  state.liveBubble = null;
  sendControl({ type: "start_turn" });
  setTurnState("说话中…");
}

function endTurn() {
  if (!state.turnOpen) {
    return;
  }
  state.turnOpen = false;
  state.speechMs = 0;
  state.busy = true;
  sendControl({ type: "end_turn" });
  setTurnState("等待回答…");
}

function updateMicUi() {
  const btn = document.getElementById("micBtn");
  const label = document.getElementById("micLabel");
  const connected = Boolean(state.socket) && state.socket.readyState === WebSocket.OPEN;
  const ready = Boolean(state.token) && connected && state.asrReady;

  btn.disabled = !ready || state.busy;
  btn.classList.toggle("listening", state.capture && (state.mode === "auto" || state.holding));
  btn.classList.toggle("thinking", state.busy);

  if (state.busy) {
    label.textContent = "思考中";
  } else if (state.mode === "auto") {
    label.textContent = state.capture ? "正在听" : "开始监听";
  } else {
    label.textContent = state.holding ? "松手结束" : "按住说话";
  }
}

async function onMicDown(event) {
  event.preventDefault();
  if (!state.asrReady) {
    toast("识别会话还没就绪，稍等一下", "warn");
    return;
  }
  if (state.mode === "auto") {
    await toggleContinuous();
    return;
  }
  if (state.busy) {
    toast("上一轮还在回答，稍等一下", "warn");
    return;
  }
  if (!(await startCapture())) {
    return;
  }
  state.holding = true;
  beginTurn();
  updateMicUi();
}

async function onMicUp() {
  if (!state.holding) {
    return;
  }
  state.holding = false;
  endTurn();
  await stopCapture();
  updateMicUi();
}

async function toggleContinuous() {
  if (state.capture) {
    endTurn();
    await stopCapture();
    setTurnState("空闲");
    setMicState("已停止监听，点麦克风可继续。");
  } else {
    if (state.busy) {
      toast("上一轮还在回答，稍等一下", "warn");
      return;
    }
    if (!(await startCapture())) {
      return;
    }
    setTurnState("空闲");
    setMicState("正在监听，直接说话即可，静音 " + state.silenceMs + " 毫秒后自动断句。");
  }
  updateMicUi();
}

async function setMode(mode) {
  state.mode = mode;
  state.holding = false;
  if (state.turnOpen) {
    endTurn();
  }
  await stopCapture();
  setTurnState("空闲");
  setMicState(mode === "auto" ? "连续对话模式：点麦克风开始监听。" : "按住说话模式：按住麦克风说话，松手结束。");
  updateMicUi();
}

/* ---------------- 事件绑定 ---------------- */

function bind() {
  document.getElementById("loginForm").addEventListener("submit", (event) => {
    event.preventDefault();
    void loginOrRegister();
  });
  document.querySelector("[data-logout]").addEventListener("click", logout);
  document.querySelector("[data-connect]").addEventListener("click", connect);
  document.querySelector("[data-disconnect]").addEventListener("click", () => disconnect("已主动断开语音连接。"));

  const micBtn = document.getElementById("micBtn");
  micBtn.addEventListener("pointerdown", (event) => void onMicDown(event));
  ["pointerup", "pointercancel", "pointerleave"].forEach((name) => {
    micBtn.addEventListener(name, () => void onMicUp());
  });

  document.querySelectorAll('input[name="turnMode"]').forEach((radio) => {
    radio.addEventListener("change", (event) => void setMode(event.target.value));
  });

  const sensitivity = document.getElementById("sensitivity");
  sensitivity.addEventListener("input", () => {
    const value = Number(sensitivity.value) || 4;
    state.threshold = THRESHOLDS[Math.max(0, Math.min(THRESHOLDS.length - 1, value - 1))];
  });
  state.threshold = THRESHOLDS[Number(sensitivity.value) - 1] || THRESHOLDS[3];

  if (window.location.protocol === "file:") {
    toast("请通过 http://127.0.0.1:8000/ecommerce/voice.html 打开本页", "error");
  }

  if (state.username) {
    document.getElementById("username").value = state.username;
  }
  refreshChips();
  updateMicUi();
  checkBackend();
  if (state.token) {
    connect();
  } else {
    setMicState("先登录，再点「连接语音」建立识别会话。");
  }
}

document.addEventListener("DOMContentLoaded", bind);
