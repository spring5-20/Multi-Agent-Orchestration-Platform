/**
 * 旅游建议智能体前端逻辑。
 *
 * 数据来源均为后端 ``/api/v1/qweather`` 下的接口：
 * - ``GET  /weather/10d``     ：城市解析 + 未来逐日预报（同时拿到经纬度）
 * - ``GET  /weather/now``     ：经纬度实时天气
 * - ``GET  /travel-advice``   ：实时天气 + 大模型出行建议
 * - ``POST /chat``            ：智能体流式问答（SSE）
 */

"use strict";

const API_BASE = "/api/v1/qweather";
const DEFAULT_CITY = "北京";
const COORD_RE = /^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$/;
const BEIJING_OFFSET_MS = 8 * 60 * 60 * 1000;

/** 页面没有通过后端服务打开时的统一提示（接口会返回 404）。 */
const SERVE_HINT =
  "页面没有连上后端接口（请求返回 404）。请先启动后端服务，再用浏览器打开 http://127.0.0.1:8000/ui/ 访问本页面；" +
  "直接双击 index.html 或用 IDE 的静态预览打开，接口是请求不到的。";

/** 全局状态：当前城市信息与请求占用标记。 */
const state = {
  city: null,
  loading: false,
  chatting: false,
};

const dom = {
  searchForm: document.getElementById("searchForm"),
  cityInput: document.getElementById("cityInput"),
  searchButton: document.getElementById("searchButton"),
  quickCities: document.getElementById("quickCities"),
  currentHint: document.getElementById("currentHint"),
  currentBody: document.getElementById("currentBody"),
  adviceBody: document.getElementById("adviceBody"),
  forecastTrack: document.getElementById("forecastTrack"),
  chatStatus: document.getElementById("chatStatus"),
  chatLog: document.getElementById("chatLog"),
  chatSuggestions: document.getElementById("chatSuggestions"),
  chatForm: document.getElementById("chatForm"),
  chatInput: document.getElementById("chatInput"),
  chatSend: document.getElementById("chatSend"),
  toast: document.getElementById("toast"),
};

/* ---------------- 通用工具 ---------------- */

/** 转义 HTML，避免大模型或接口返回内容注入页面。 */
function escapeHtml(text) {
  return String(text ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

/** 读取形如 ``{value, unit}`` 的数值对象，缺失时返回占位符。 */
function valueOf(holder, digits = 0) {
  const value = holder && holder.value;
  if (value === null || value === undefined || Number.isNaN(Number(value))) {
    return "—";
  }
  return `${Number(value).toFixed(digits)}${(holder && holder.unit) || ""}`;
}

/** 把 [0, 1] 的小数（湿度、云量、降水概率）格式化为百分比。 */
function percentOf(ratio) {
  if (ratio === null || ratio === undefined || Number.isNaN(Number(ratio))) {
    return "—";
  }
  const value = Number(ratio);
  return `${Math.round(value <= 1 ? value * 100 : value)}%`;
}

/** 和风天气返回的风向英文缩写转中文描述。 */
const COMPASS_CN = {
  n: "北风",
  nne: "北东北风",
  ne: "东北风",
  ene: "东东北风",
  e: "东风",
  ese: "东东南风",
  se: "东南风",
  sse: "南东南风",
  s: "南风",
  ssw: "南西南风",
  sw: "西南风",
  wsw: "西西南风",
  w: "西风",
  wnw: "西西北风",
  nw: "西北风",
  nnw: "北西北风",
};

/** 由风向英文缩写得到中文描述。 */
function compassCN(direction) {
  if (!direction) {
    return "—";
  }
  const key = String(direction.compass || "").toLowerCase();
  return COMPASS_CN[key] || (direction.degree !== undefined ? `${direction.degree}°` : "—");
}

/** 夜间天气代码的专属图标。 */
const NIGHT_EMOJI = { 150: "🌙", 151: "☁️", 152: "🌙", 153: "🌙", 154: "☁️" };

/** 根据天气现象文字（和天气代码）挑选一个 emoji 图标。 */
function weatherEmoji(condition) {
  const code = String((condition && condition.code) || "");
  if (NIGHT_EMOJI[code]) {
    return NIGHT_EMOJI[code];
  }
  const text = String((condition && condition.text) || "");
  const rules = [
    [/雷/, "⛈️"],
    [/暴雨|大雨|强降雨/, "🌧️"],
    [/雪|冰雹/, "❄️"],
    [/雾|霾|浮尘/, "🌫️"],
    [/沙|尘暴|扬沙/, "🌪️"],
    [/雨/, "🌦️"],
    [/晴/, "☀️"],
    [/云|阴/, "⛅"],
    [/风/, "🌬️"],
  ];
  for (const [pattern, emoji] of rules) {
    if (pattern.test(text)) {
      return emoji;
    }
  }
  return "🌡️";
}

/** 紫外线指数对应的强度描述。 */
function uvText(index) {
  if (index === null || index === undefined) {
    return "—";
  }
  const level = Number(index);
  if (level <= 2) return `${level}（弱）`;
  if (level <= 5) return `${level}（中等）`;
  if (level <= 7) return `${level}（强）`;
  if (level <= 10) return `${level}（很强）`;
  return `${level}（极强）`;
}

/** 能见度：米转千米展示。 */
function visibilityText(holder) {
  if (!holder || holder.value === undefined || holder.value === null) {
    return "—";
  }
  const meters = Number(holder.value);
  return meters >= 1000 ? `${(meters / 1000).toFixed(1)} km` : `${meters} m`;
}

/** 把 UTC 时间串换算成北京时间的各部分，避免受浏览器时区影响。 */
function beijingParts(iso) {
  if (!iso) {
    return null;
  }
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) {
    return null;
  }
  const shifted = new Date(date.getTime() + BEIJING_OFFSET_MS);
  return {
    year: shifted.getUTCFullYear(),
    month: shifted.getUTCMonth() + 1,
    day: shifted.getUTCDate(),
    weekday: shifted.getUTCDay(),
    hour: shifted.getUTCHours(),
    minute: shifted.getUTCMinutes(),
  };
}

const WEEKDAY_CN = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"];

/** 生成「今天 / 明天 / 周三」这类日期标签。 */
function dayLabel(parts, index) {
  if (!parts) {
    return "—";
  }
  if (index === 0) return "今天";
  if (index === 1) return "明天";
  return WEEKDAY_CN[parts.weekday];
}

/** 生成「09月20日」这类日期文本。 */
function dateText(parts) {
  if (!parts) {
    return "";
  }
  return `${String(parts.month).padStart(2, "0")}月${String(parts.day).padStart(2, "0")}日`;
}

/** 生成「05:47」这类时刻文本。 */
function clockText(parts) {
  if (!parts) {
    return "—";
  }
  return `${String(parts.hour).padStart(2, "0")}:${String(parts.minute).padStart(2, "0")}`;
}

/** 简单的 Markdown 渲染：支持段落、有序/无序列表、加粗与行内代码。 */
function renderRichText(text) {
  const lines = escapeHtml(String(text ?? "").replace(/\r\n/g, "\n")).split("\n");
  const blocks = [];
  let list = null;

  const flushList = () => {
    if (list) {
      blocks.push(`<${list.tag}>${list.items.join("")}</${list.tag}>`);
      list = null;
    }
  };

  const inline = (segment) =>
    segment.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>").replace(/`([^`]+)`/g, "<code>$1</code>");

  for (const rawLine of lines) {
    const line = rawLine.trim();
    if (!line) {
      flushList();
      continue;
    }
    const bullet = /^[-*•]\s+/.exec(line);
    const ordered = /^\d+[.、)]\s+/.exec(line);
    if (bullet || ordered) {
      const tag = bullet ? "ul" : "ol";
      if (!list || list.tag !== tag) {
        flushList();
        list = { tag, items: [] };
      }
      list.items.push(`<li>${inline(line.slice((bullet || ordered)[0].length))}</li>`);
      continue;
    }
    flushList();
    blocks.push(`<p>${inline(line)}</p>`);
  }
  flushList();
  return blocks.join("");
}

/** 弹出底部提示条。 */
let toastTimer = null;
function toast(message) {
  dom.toast.textContent = message;
  dom.toast.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    dom.toast.hidden = true;
  }, 4200);
}

/** 在容器里渲染一条错误提示，可选带「重试」按钮。 */
function renderError(container, message, onRetry) {
  container.innerHTML = "";
  const box = document.createElement("div");
  box.className = "empty-state";
  box.textContent = `😥 ${message}`;
  if (onRetry) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "retry-btn";
    button.textContent = "重试";
    button.addEventListener("click", onRetry);
    box.appendChild(document.createElement("br"));
    box.appendChild(button);
  }
  container.appendChild(box);
}

/** 把后端返回的错误转成更容易理解的中文提示。 */
function friendlyError(message) {
  const text = String(message ?? "").trim();
  // 静态服务器/IDE 预览返回的 HTML 404 页面，说明页面不是由后端托管的
  if (/<!doctype html|404 Not Found|HTTP 404/i.test(text)) {
    return SERVE_HINT;
  }
  if (!text || /Internal Server Error|Bad Gateway|Service Unavailable/i.test(text)) {
    return "服务端暂时不可用（大模型接口可能繁忙或超时），请稍后重试。";
  }
  return text;
}

/** 请求 JSON 接口，非 2xx 时抛出后端 detail 文案。 */
async function requestJSON(path, params) {
  const url = new URL(API_BASE + path, window.location.origin);
  Object.entries(params || {}).forEach(([key, value]) => url.searchParams.set(key, value));
  const response = await fetch(url.toString(), { headers: { Accept: "application/json" } });
  const raw = await response.text();
  let data = null;
  try {
    data = raw ? JSON.parse(raw) : null;
  } catch {
    data = null;
  }
  if (!response.ok) {
    const detail = data && data.detail ? data.detail : `请求失败（HTTP ${response.status}）`;
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return data;
}

/* ---------------- 天气数据渲染 ---------------- */

/** 查询入口：解析城市或经纬度，并刷新三块天气内容。 */
async function loadCity(rawInput) {
  const raw = String(rawInput ?? "").trim();
  if (!raw) {
    toast("请输入城市名或经纬度");
    return;
  }
  if (state.loading) {
    return;
  }

  state.loading = true;
  dom.searchButton.disabled = true;
  dom.searchButton.textContent = "查询中";
  dom.currentBody.innerHTML = '<div class="empty-state">正在获取实时天气…</div>';
  dom.adviceBody.innerHTML = '<div class="empty-state">正在生成出行建议…</div>';

  try {
    const coordMatch = COORD_RE.exec(raw);
    let city;
    let days = [];

    if (coordMatch) {
      const latitude = Number(coordMatch[1]);
      const longitude = Number(coordMatch[2]);
      if (latitude < -90 || latitude > 90 || longitude < -180 || longitude > 180) {
        throw new Error("经纬度范围：纬度 -90~90，经度 -180~180");
      }
      city = {
        name: `${latitude.toFixed(2)},${longitude.toFixed(2)}`,
        adm1: "坐标模式",
        latitude,
        longitude,
      };
      dom.forecastTrack.innerHTML =
        '<div class="empty-state">坐标模式无法反查城市名，输入城市名即可查看未来 10 天预报。</div>';
    } else {
      const forecast = await requestJSON("/weather/10d", { city: raw, days: 10 });
      city = forecast.city || {};
      days = forecast.days || [];
      renderForecast(city, days);
    }

    state.city = city;
    const location = `${city.latitude},${city.longitude}`;
    const nowTask = renderCurrentCard(city, location);
    const adviceTask = renderAdviceCard(location);
    await Promise.allSettled([nowTask, adviceTask]);
  } catch (error) {
    const message = friendlyError(error instanceof Error ? error.message : String(error));
    renderError(dom.currentBody, message, () => loadCity(raw));
    renderError(dom.adviceBody, message);
    if (!dom.forecastTrack.querySelector(".day-card")) {
      renderError(dom.forecastTrack, message, () => loadCity(raw));
    }
    toast(message);
  } finally {
    state.loading = false;
    dom.searchButton.disabled = false;
    dom.searchButton.textContent = "查询";
  }
}

/** 渲染实时天气卡片。 */
async function renderCurrentCard(city, location) {
  try {
    const payload = await requestJSON("/weather/now", { location });
    const weather = payload.weather || {};
    const condition = weather.condition || {};
    const wind = weather.wind || {};
    const precipitation = weather.precipitation || {};
    const temperatureValue = weather.temperature?.value;
    const temperatureText =
      temperatureValue === undefined || temperatureValue === null || Number.isNaN(Number(temperatureValue))
        ? "—"
        : Math.round(Number(temperatureValue));

    const metrics = [
      ["体感温度", valueOf(weather.feelsLike, 1)],
      ["相对湿度", percentOf(weather.humidity)],
      ["风向风力", `${compassCN(wind.direction)} ${wind.scale ?? "—"} 级`],
      ["风速", valueOf(wind.speed, 1)],
      ["阵风", valueOf(weather.windGust, 1)],
      ["降水量", valueOf(precipitation.amount, 1)],
      ["气压", valueOf(weather.pressure, 0)],
      ["能见度", visibilityText(weather.visibility)],
      ["紫外线", uvText(weather.uvIndex)],
      ["云量", percentOf(weather.cloudCover)],
      ["露点温度", valueOf(weather.dewPoint, 1)],
    ];

    const cityName = city.name || "未知地点";
    const cityMeta = [city.adm1, city.adm2, city.country].filter((item) => item && item !== cityName).join(" · ");
    const updatedAt = new Date().toLocaleTimeString("zh-CN", { hour12: false });

    dom.currentHint.textContent = `更新于 ${updatedAt}`;
    dom.currentBody.innerHTML = `
      <div class="current-hero">
        <div class="current-emoji">${weatherEmoji(condition)}</div>
        <div class="current-temp">${temperatureText}<small>${weather.temperature?.unit || "°C"}</small></div>
        <div class="current-desc">
          <div class="city-name">${escapeHtml(cityName)}</div>
          <div class="city-meta">${escapeHtml(cityMeta || "实时观测")}</div>
          <div class="condition">${escapeHtml(condition.text || "未知天气")} · 体感 ${valueOf(weather.feelsLike, 1)}</div>
        </div>
      </div>
      <div class="metric-grid">
        ${metrics
          .map(
            ([label, value]) =>
              `<div class="metric"><span class="label">${escapeHtml(label)}</span><span class="value">${escapeHtml(value)}</span></div>`,
          )
          .join("")}
      </div>
    `;
  } catch (error) {
    const message = friendlyError(error instanceof Error ? error.message : String(error));
    renderError(dom.currentBody, message, () => renderCurrentCard(city, location));
  }
}

/** 渲染大模型出行建议卡片。 */
async function renderAdviceCard(location) {
  try {
    const payload = await requestJSON("/travel-advice", { location });
    dom.adviceBody.innerHTML = renderRichText(payload.advice || "暂时没有生成建议。");
  } catch (error) {
    const message = friendlyError(error instanceof Error ? error.message : String(error));
    renderError(dom.adviceBody, message, () => renderAdviceCard(location));
  }
}

/** 渲染未来逐日预报，点击某天可向智能体追问。 */
function renderForecast(city, days) {
  if (!days.length) {
    dom.forecastTrack.innerHTML = '<div class="empty-state">暂未获取到逐日预报数据。</div>';
    return;
  }

  dom.forecastTrack.innerHTML = days
    .map((day, index) => {
      const parts = beijingParts(day.forecastStartTime);
      const daytime = day.daytime || {};
      const nighttime = day.nighttime || {};
      const rain = percentOf(daytime.precipitation?.probability);
      const windScale = daytime.wind?.scale ?? "—";
      const sunrise = clockText(beijingParts(day.astro?.sunrise));
      const sunset = clockText(beijingParts(day.astro?.sunset));
      const label = `${dayLabel(parts, index)} ${dateText(parts)}`;
      const max = day.temperatureMax?.value !== undefined ? Math.round(Number(day.temperatureMax.value)) : "—";
      const min = day.temperatureMin?.value !== undefined ? Math.round(Number(day.temperatureMin.value)) : "—";

      return `
        <button type="button" class="day-card" data-label="${escapeHtml(label)}">
          <div class="day-label">${escapeHtml(dayLabel(parts, index))}</div>
          <div class="day-date">${escapeHtml(dateText(parts))}</div>
          <div class="day-emoji">${weatherEmoji(daytime.condition)}</div>
          <div class="day-row"><span>白天</span><span>${escapeHtml(daytime.condition?.text || "未知")}</span></div>
          <div class="day-row"><span>夜间</span><span>${escapeHtml(nighttime.condition?.text || "未知")}</span></div>
          <div class="day-temp">${min}° ~ ${max}°</div>
          <div class="day-extra">💧 ${escapeHtml(rain)} · 🌬 ${escapeHtml(String(windScale))} 级</div>
          <div class="day-extra">🌅 ${escapeHtml(sunrise)} 🌇 ${escapeHtml(sunset)}</div>
        </button>
      `;
    })
    .join("");

  dom.forecastTrack.querySelectorAll(".day-card").forEach((card) => {
    card.addEventListener("click", () => {
      const cityName = state.city?.name || city.name || "";
      sendChat(`${card.dataset.label}（${cityName}）的天气怎么样？适合安排出游吗？`);
    });
  });
}

/* ---------------- 智能体对话（SSE） ---------------- */

/** 在对话区追加一条消息气泡，返回气泡元素。 */
function appendMessage(role, text) {
  const bubble = document.createElement("div");
  bubble.className = `msg ${role}`;
  const content = document.createElement("div");
  content.className = "richtext";
  if (role === "assistant" && state.city?.name) {
    const tag = document.createElement("span");
    tag.className = "city-tag";
    tag.textContent = `📍 ${state.city.name}`;
    bubble.appendChild(tag);
  }
  content.innerHTML = role === "user" ? escapeHtml(text) : renderRichText(text);
  bubble.appendChild(content);
  dom.chatLog.appendChild(bubble);
  dom.chatLog.scrollTop = dom.chatLog.scrollHeight;
  return bubble;
}

/** 把工具调用事件翻译成中文提示。 */
function toolTraceText(event) {
  const args = event.args || {};
  const city = args.city ? `「${args.city}」` : "";
  if (event.name === "query_qweather_forecast") {
    return `🔧 正在查询${city}未来 ${args.days || 10} 天预报…`;
  }
  if (event.name === "query_qweather_now") {
    return `🔧 正在查询${city}实时天气…`;
  }
  return `🔧 正在调用工具 ${event.name || ""}…`;
}

/** 发送一轮对话，并流式渲染 SSE 事件。 */
async function sendChat(rawQuestion) {
  const question = String(rawQuestion ?? "").trim();
  if (!question || state.chatting) {
    return;
  }

  state.chatting = true;
  dom.chatSend.disabled = true;
  dom.chatStatus.classList.add("busy");
  dom.chatStatus.textContent = "思考中";
  appendMessage("user", question);

  const bubble = appendMessage("assistant", "");
  const content = bubble.querySelector(".richtext");
  let trace = null;
  let answer = "";
  let finished = false;

  // 追问时带上当前城市上下文，让智能体的天气工具知道该查哪里
  const payload = state.city?.name ? `（当前城市：${state.city.name}）${question}` : question;

  const handleEvent = (event) => {
    if (event.type === "done") {
      finished = true;
      return;
    }
    if (event.type === "tool") {
      if (!trace) {
        trace = document.createElement("div");
        trace.className = "tool-trace";
        bubble.insertBefore(trace, content);
      }
      trace.textContent = toolTraceText(event);
      dom.chatLog.scrollTop = dom.chatLog.scrollHeight;
      return;
    }
    if (event.type === "content" && typeof event.content === "string") {
      if (trace) {
        trace.textContent = `${trace.textContent.replace(/…$/, "")} ✓`;
      }
      answer += event.content;
      content.innerHTML = renderRichText(answer);
      bubble.classList.add("typing");
      dom.chatLog.scrollTop = dom.chatLog.scrollHeight;
    }
  };

  try {
    const response = await fetch(`${API_BASE}/chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question: payload }),
    });

    if (!response.ok || !response.body) {
      const detail = await response.text();
      throw new Error(detail || `请求失败（HTTP ${response.status}）`);
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder("utf-8");
    let buffer = "";

    for (;;) {
      const { value, done } = await reader.read();
      if (done) {
        break;
      }
      buffer += decoder.decode(value, { stream: true });
      let boundary = buffer.indexOf("\n\n");
      while (boundary !== -1) {
        const chunk = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + 2);
        for (const line of chunk.split("\n")) {
          if (!line.startsWith("data:")) {
            continue;
          }
          const raw = line.slice(5).trim();
          if (!raw) {
            continue;
          }
          try {
            handleEvent(JSON.parse(raw));
          } catch {
            // 忽略无法解析的分片，避免中断本次回答
          }
        }
        boundary = buffer.indexOf("\n\n");
      }
    }

    if (!answer && finished) {
      content.innerHTML = renderRichText("（智能体没有返回内容，换个问法再试试？）");
    } else if (!answer) {
      // 流被中断（通常是后端调用大模型失败）时给出明确提示
      bubble.classList.add("error");
      content.innerHTML = renderRichText("😥 回答中断了：大模型服务可能暂时不可用，请稍后重试。");
    }
  } catch (error) {
    const message = friendlyError(error instanceof Error ? error.message : String(error));
    bubble.classList.add("error");
    content.innerHTML = renderRichText(`😥 出错了：${message}`);
  } finally {
    bubble.classList.remove("typing");
    state.chatting = false;
    dom.chatSend.disabled = false;
    dom.chatStatus.classList.remove("busy");
    dom.chatStatus.textContent = "在线";
    dom.chatLog.scrollTop = dom.chatLog.scrollHeight;
  }
}

/* ---------------- 事件绑定 ---------------- */

/** 根据输入框内容自适应高度。 */
function autoGrow(textarea) {
  textarea.style.height = "auto";
  textarea.style.height = `${Math.min(textarea.scrollHeight, 120)}px`;
}

function bindEvents() {
  dom.searchForm.addEventListener("submit", (event) => {
    event.preventDefault();
    loadCity(dom.cityInput.value);
  });

  dom.quickCities.addEventListener("click", (event) => {
    const button = event.target.closest("button[data-city]");
    if (!button) {
      return;
    }
    dom.cityInput.value = button.dataset.city;
    loadCity(button.dataset.city);
  });

  dom.chatSuggestions.addEventListener("click", (event) => {
    const button = event.target.closest("button");
    if (button) {
      sendChat(button.textContent);
    }
  });

  dom.chatForm.addEventListener("submit", (event) => {
    event.preventDefault();
    const question = dom.chatInput.value;
    dom.chatInput.value = "";
    autoGrow(dom.chatInput);
    sendChat(question);
  });

  dom.chatInput.addEventListener("input", () => autoGrow(dom.chatInput));
  dom.chatInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      dom.chatForm.requestSubmit();
    }
  });
}

/** 页面初始化：欢迎语 + 默认城市。 */
function init() {
  bindEvents();
  appendMessage(
    "assistant",
    "你好，我是旅游建议智能体 🌤️\n- 告诉我城市，我帮你查实时天气和未来 10 天预报\n- 也可以直接问「明天需要带伞吗」「周末适合去爬山吗」",
  );

  // 以文件方式（file://）打开时接口必然请求不到，直接给出提示，避免误导
  if (window.location.protocol === "file:") {
    renderError(dom.currentBody, SERVE_HINT);
    renderError(dom.adviceBody, SERVE_HINT);
    renderError(dom.forecastTrack, SERVE_HINT);
    toast("请通过 http://127.0.0.1:8000/ui/ 打开本页面");
    return;
  }

  dom.cityInput.value = DEFAULT_CITY;
  loadCity(DEFAULT_CITY);
}

init();
