/**
 * 信息查询页（index.html）的页面级逻辑。
 *
 * 通用能力（表单绑定、状态徽标、结果渲染）都在 common.js 里，本文件负责两件本页特有的事：
 * 1. 触发扫码登录后轮询登录状态；
 * 2. 「和钉协助手对话」面板：POST /chat 的 SSE 流式问答。
 */

"use strict";

(function () {
  const DWS = window.DWS;
  if (!DWS) {
    return;
  }

  /* ---------------- 触发登录后轮询状态 ---------------- */

  const authCard = document.getElementById("authCard");
  if (authCard) {
    authCard.addEventListener("dws:success", () => {
      DWS.toast("已触发扫码登录，请在浏览器完成授权，正在轮询登录状态…");
      DWS.pollAuthStatus();
    });
  }

  /* ---------------- 对话面板 ---------------- */

  /** 工具名到中文提示的映射，与 core/agents/dingtalk/tools.py 中的工具保持一致。 */
  const TOOL_HINTS = {
    search_contact: "🔧 正在搜索通讯录…",
    list_calendar: "🔧 正在查询今日日程…",
    list_todos: "🔧 正在查询待办…",
    list_logs: "🔧 正在查询收到的日志…",
    get_attendance: "🔧 正在查询今日考勤…",
    search_group: "🔧 正在搜索群聊…",
    create_calendar: "🔧 正在创建日程…",
    create_todo: "🔧 正在创建待办…",
    assign_todo: "🔧 正在指派待办…",
    send_message: "🔧 正在发送单聊消息…",
    send_group_message: "🔧 正在发送群消息…",
    send_robot_message: "🔧 正在推送机器人消息…",
    publish_notice: "🔧 正在发布群公告…",
    send_ding: "🔧 正在发送 DING 强提醒…",
    create_sheet: "🔧 正在创建在线表格…",
    create_log: "🔧 正在提交日报日志…",
  };

  const chatDom = {
    form: document.getElementById("chatForm"),
    input: document.getElementById("chatInput"),
    send: document.getElementById("chatSend"),
    log: document.getElementById("chatLog"),
    status: document.getElementById("chatStatus"),
    suggestions: document.getElementById("chatSuggestions"),
  };

  const chatState = { busy: false };

  /** 极简 Markdown 渲染：段落、列表、加粗与行内代码（先转义再渲染）。 */
  function renderRichText(text) {
    const lines = DWS.escapeHtml(String(text ?? "").replace(/\r\n/g, "\n")).split("\n");
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

  /** 在对话区追加一条消息气泡，返回气泡元素。 */
  function appendMessage(role, text) {
    const bubble = document.createElement("div");
    bubble.className = `msg ${role}`;
    if (role === "user") {
      bubble.textContent = text;
    } else {
      const content = document.createElement("div");
      content.className = "richtext";
      content.innerHTML = renderRichText(text);
      bubble.appendChild(content);
    }
    chatDom.log.appendChild(bubble);
    chatDom.log.scrollTop = chatDom.log.scrollHeight;
    return bubble;
  }

  /** 把工具调用事件翻译成中文提示。 */
  function toolTraceText(event) {
    const known = TOOL_HINTS[event.name];
    if (known) {
      return known;
    }
    return `🔧 正在调用工具 ${event.name || ""}…`;
  }

  /** 逐个解析 SSE 分片里的 ``data:`` 行。 */
  function handleChunk(chunk, onEvent) {
    for (const line of chunk.split("\n")) {
      if (!line.startsWith("data:")) {
        continue;
      }
      const raw = line.slice(5).trim();
      if (!raw) {
        continue;
      }
      try {
        onEvent(JSON.parse(raw));
      } catch {
        // 忽略无法解析的分片，避免中断本次回答
      }
    }
  }

  /** 发送一轮对话，并流式渲染 SSE 事件。 */
  async function sendChat(rawQuestion) {
    const question = String(rawQuestion ?? "").trim();
    if (!question || chatState.busy) {
      return;
    }

    chatState.busy = true;
    chatDom.send.disabled = true;
    chatDom.status.classList.add("busy");
    chatDom.status.textContent = "思考中";
    appendMessage("user", question);

    const bubble = appendMessage("assistant", "");
    const content = bubble.querySelector(".richtext");
    let trace = null;
    let answer = "";
    let finished = false;

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
        chatDom.log.scrollTop = chatDom.log.scrollHeight;
        return;
      }
      if (event.type === "content" && typeof event.content === "string") {
        if (trace) {
          trace.textContent = `${trace.textContent.replace(/…$/, "")} ✓`;
        }
        answer += event.content;
        content.innerHTML = renderRichText(answer);
        bubble.classList.add("typing");
        chatDom.log.scrollTop = chatDom.log.scrollHeight;
      }
    };

    try {
      // API_BASE 由 common.js 声明，两个脚本共享全局作用域
      const response = await fetch(`${API_BASE}/chat`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question }),
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
          handleChunk(buffer.slice(0, boundary), handleEvent);
          buffer = buffer.slice(boundary + 2);
          boundary = buffer.indexOf("\n\n");
        }
      }

      if (!answer) {
        content.innerHTML = renderRichText(
          finished ? "（智能体没有返回内容，换个问法再试试？）" : "😥 回答中断了：模型服务可能暂时不可用，请稍后重试。",
        );
        if (!finished) {
          bubble.classList.add("error");
        }
      }
    } catch (error) {
      const message = DWS.friendlyError(error instanceof Error ? error.message : String(error));
      bubble.classList.add("error");
      content.innerHTML = `<p class="error-text">😥 出错了：${DWS.escapeHtml(message)}</p>`;
    } finally {
      bubble.classList.remove("typing");
      chatState.busy = false;
      chatDom.send.disabled = false;
      chatDom.status.classList.remove("busy");
      chatDom.status.textContent = "在线";
      chatDom.log.scrollTop = chatDom.log.scrollHeight;
    }
  }

  /** 根据输入框内容自适应高度。 */
  function autoGrow(textarea) {
    textarea.style.height = "auto";
    textarea.style.height = `${Math.min(textarea.scrollHeight, 132)}px`;
  }

  function bindChat() {
    if (!chatDom.form || !chatDom.log) {
      return;
    }

    appendMessage(
      "assistant",
      "你好，我是钉协助手智能体 📇\n- 可以直接问「今天有哪些日程」「我有哪些未完成待办」\n" +
        "- 也可以让我建日程、发消息、提交日报；写操作执行前我会先跟你确认",
    );

    chatDom.suggestions.addEventListener("click", (event) => {
      const button = event.target.closest("button");
      if (button) {
        sendChat(button.textContent);
      }
    });

    chatDom.form.addEventListener("submit", (event) => {
      event.preventDefault();
      const question = chatDom.input.value;
      chatDom.input.value = "";
      autoGrow(chatDom.input);
      sendChat(question);
    });

    chatDom.input.addEventListener("input", () => autoGrow(chatDom.input));
    chatDom.input.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        chatDom.form.requestSubmit();
      }
    });
  }

  bindChat();
  DWS.initShell({ page: "query" });
})();
