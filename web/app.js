import { renderMarkdown } from "/markdown.js";

const stage = document.querySelector(".stage");
const chatLog = document.querySelector("#chatLog");
const welcome = document.querySelector("#welcome");
const suggestionsEl = document.querySelector("#suggestions");
const conversationListEl = document.querySelector("#conversationList");
const form = document.querySelector("#chatForm");
const input = document.querySelector("#messageInput");
const sendButton = document.querySelector("#sendButton");
const newChatButton = document.querySelector("#newChat");
const flowNodes = document.querySelectorAll("#flowMap .flow-node");
const flowEdges = document.querySelectorAll("#flowMap .flow-edge");
const flowHint = document.querySelector("#flowHint");

const ACTIVE_SESSION_KEY = "coros-running-agent-active-session";
const CONVERSATIONS_KEY = "coros-running-agent-conversations-v1";
const FALLBACK_ACTIONS = [
  { title: "查最近 90 天运动记录", prompt: "列出我最近 90 天的运动记录" },
  { title: "列出全部比赛照片", prompt: "查看我保存过的所有比赛照片" },
  { title: "查最近一次训练报告", prompt: "我今天这次训练怎么样？下一次应该怎么练？" },
  { title: "查个人 PB", prompt: "查我的个人 PB" },
  { title: "查成绩瓶颈", prompt: "我现在半马 1:40，全马 4:30，想提高全马成绩，应该加强哪部分训练？" },
  { title: "查跑步知识库", prompt: "根据已导入的跑步书籍，解释我当前成绩瓶颈" },
  { title: "制定训练计划", prompt: "根据我的当前能力和知识库，制定一份全马训练计划" },
];

let busy = false;
let thinkingEl = null;
let activeSessionId = "";
let defaultActions = FALLBACK_ACTIONS;
let contextualActions = null;
let flowQueue = [];
let flowPlaying = false;
let lastFlowModule = null;
const OBSERVATION_SOURCES = new Set(["coros", "profile", "knowledge", "search"]);

function newSessionId() {
  return crypto.randomUUID
    ? crypto.randomUUID()
    : `s-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

function emptyConversation() {
  const now = new Date().toISOString();
  return {
    id: newSessionId(),
    title: "当前对话",
    createdAt: now,
    updatedAt: now,
    messages: [],
  };
}

function loadConversations() {
  try {
    const value = JSON.parse(localStorage.getItem(CONVERSATIONS_KEY) || "[]");
    if (Array.isArray(value)) return value.filter((item) => item && item.id);
  } catch {
    return [];
  }
  return [];
}

function isEmptyConversation(conversation) {
  const messages = Array.isArray(conversation?.messages) ? conversation.messages : [];
  return messages.length === 0;
}

function normalizeConversations(conversations) {
  const valid = conversations.filter((item) => item && item.id);
  let empty = null;
  const filled = [];

  for (const conversation of valid) {
    if (isEmptyConversation(conversation)) {
      if (!empty || conversation.id === activeSessionId) empty = conversation;
      continue;
    }
    filled.push(conversation);
  }

  return [...(empty ? [empty] : []), ...filled].slice(0, 24);
}

function saveConversations(conversations) {
  localStorage.setItem(CONVERSATIONS_KEY, JSON.stringify(normalizeConversations(conversations)));
}

function activeConversation() {
  let conversations = loadConversations();
  let active = conversations.find((item) => item.id === activeSessionId);
  if (!active) {
    active = emptyConversation();
    activeSessionId = active.id;
    conversations = [active, ...conversations];
    saveConversations(conversations);
    localStorage.setItem(ACTIVE_SESSION_KEY, activeSessionId);
  }
  return active;
}

function updateActiveConversation(updater) {
  const conversations = loadConversations();
  const index = conversations.findIndex((item) => item.id === activeSessionId);
  if (index === -1) return;
  const next = { ...conversations[index] };
  updater(next);
  next.updatedAt = new Date().toISOString();
  conversations[index] = next;
  conversations.sort((a, b) => String(b.updatedAt).localeCompare(String(a.updatedAt)));
  saveConversations(conversations);
  renderConversationList();
}

function appendStoredMessage(role, text) {
  updateActiveConversation((conversation) => {
    const messages = Array.isArray(conversation.messages) ? conversation.messages : [];
    conversation.messages = [...messages, { role, text }];
    if (role === "user" && ["新对话", "当前对话"].includes(conversation.title)) {
      conversation.title = text.slice(0, 26) || "当前对话";
    }
  });
}

function conversationId() {
  return activeSessionId;
}

function startNewConversation() {
  if (busy) return;
  const conversations = loadConversations();
  let empty = conversations.find(isEmptyConversation);
  if (!empty) {
    empty = emptyConversation();
    saveConversations([empty, ...conversations]);
  }
  activeSessionId = empty.id;
  localStorage.setItem(ACTIVE_SESSION_KEY, activeSessionId);
  renderConversationList();
  renderConversation();
  updateSuggestionsFromConversation();
  input.focus();
}

function switchConversation(id) {
  if (busy || id === activeSessionId) return;
  activeSessionId = id;
  localStorage.setItem(ACTIVE_SESSION_KEY, activeSessionId);
  renderConversationList();
  renderConversation();
  updateSuggestionsFromConversation();
  input.focus();
}

function renderConversationList() {
  const conversations = loadConversations();
  conversationListEl.replaceChildren();
  for (const conversation of conversations.filter((item) => !isEmptyConversation(item))) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "conversation-item";
    if (conversation.id === activeSessionId) button.classList.add("active");
    button.innerHTML = `
      <span class="conversation-title"></span>
      <span class="conversation-meta"></span>
    `;
    const title = conversation.title === "新对话" ? "当前对话" : conversation.title;
    button.querySelector(".conversation-title").textContent = title || "当前对话";
    const count = Array.isArray(conversation.messages) ? conversation.messages.length : 0;
    button.querySelector(".conversation-meta").textContent = count ? `${count} 条消息` : "空白对话";
    button.addEventListener("click", () => switchConversation(conversation.id));
    conversationListEl.appendChild(button);
  }
}

function renderConversation() {
  const conversation = activeConversation();
  chatLog.replaceChildren(welcome);
  const messages = Array.isArray(conversation.messages) ? conversation.messages : [];
  welcome.hidden = messages.length > 0;
  for (const message of messages) {
    appendMessage(message.role, message.text, { persist: false });
  }
  scrollToBottom();
}

function atBottom() {
  return stage.scrollHeight - stage.scrollTop - stage.clientHeight < 120;
}

function scrollToBottom() {
  stage.scrollTop = stage.scrollHeight;
}

function hideWelcome() {
  welcome.hidden = true;
}

function appendMessage(kind, text, options = {}) {
  const { persist = true } = options;
  hideWelcome();
  const article = document.createElement("article");
  article.className = `message ${kind}-message`;
  const bubble = document.createElement("div");
  bubble.className = "bubble";
  if (kind === "user") {
    bubble.textContent = text;
  } else {
    bubble.innerHTML = renderMarkdown(text);
  }
  article.appendChild(bubble);
  chatLog.appendChild(article);
  if (persist) appendStoredMessage(kind, text);
  scrollToBottom();
  return bubble;
}

// 图片走独立的 images 事件，不经过模型也不进 markdown 渲染。
// 直接建 <img>，src 来自后端给的 /media/photo-memory/ 路径。
function appendImages(urls, caption) {
  if (!Array.isArray(urls) || !urls.length) return;
  hideWelcome();
  const article = document.createElement("article");
  article.className = "message agent-message";

  const bubble = document.createElement("div");
  bubble.className = "bubble photo-bubble";
  if (caption) {
    const title = document.createElement("p");
    title.className = "photo-caption";
    title.textContent = `${caption} · ${urls.length} 张`;
    bubble.appendChild(title);
  }

  const grid = document.createElement("div");
  grid.className = "photo-grid";
  for (const url of urls) {
    const link = document.createElement("a");
    link.href = url;
    link.target = "_blank";
    link.rel = "noopener";
    const img = document.createElement("img");
    img.src = url;
    img.loading = "lazy";
    img.alt = caption || "比赛照片";
    link.appendChild(img);
    grid.appendChild(link);
  }
  bubble.appendChild(grid);
  article.appendChild(bubble);
  chatLog.appendChild(article);
  scrollToBottom();
}

function flowReset() {
  for (const node of flowNodes) node.classList.remove("is-active", "is-done");
  for (const edge of flowEdges) edge.classList.remove("is-active", "is-done");
  flowQueue = [];
  flowPlaying = false;
  lastFlowModule = null;
  if (flowHint) flowHint.textContent = "提问后节点会在图上亮起";
}

// 只有「当前步」是高亮的，之前走过的降级成 is-done。
// 这样一眼看得出走到哪了，同时保留了整条路径。
function flowStepNow(module, hint) {
  if (!flowNodes.length) return;
  for (const node of flowNodes) {
    const name = node.dataset.module;
    if (name === module) {
      node.classList.remove("is-done");
      node.classList.add("is-active");
    } else if (node.classList.contains("is-active")) {
      node.classList.remove("is-active");
      node.classList.add("is-done");
    }
  }

  if (lastFlowModule && lastFlowModule !== module) {
    const directEdge = document.querySelector(
      `#flowMap .flow-edge[data-from="${lastFlowModule}"][data-to="${module}"]`,
    );
    const incomingEdges = directEdge
      ? [directEdge]
      : Array.from(document.querySelectorAll(`#flowMap .flow-edge[data-to="${module}"]`));
    if (incomingEdges.length) {
      for (const item of flowEdges) {
        if (item.classList.contains("is-active")) {
          item.classList.remove("is-active");
          item.classList.add("is-done");
        }
      }
      for (const edge of incomingEdges) {
        edge.classList.remove("is-done");
        edge.classList.add("is-active");
      }
    }
  }

  lastFlowModule = module;
  if (flowHint && hint) flowHint.textContent = hint;
}

async function playFlowQueue() {
  if (flowPlaying) return;
  flowPlaying = true;
  while (flowQueue.length) {
    const step = flowQueue.shift();
    flowStepNow(step.module, step.hint);
    await new Promise((resolve) => setTimeout(resolve, 240));
  }
  flowPlaying = false;
}

function enqueueFlowStep(module, hint) {
  const queuedLast = flowQueue.at(-1)?.module;
  if (queuedLast === module) return;
  if (!queuedLast && lastFlowModule === module) return;
  flowQueue.push({ module, hint });
}

function flowStep(module, hint) {
  if (!module) return;
  const queuedLast = flowQueue.at(-1)?.module || lastFlowModule;
  if (module === "answer" && queuedLast && queuedLast !== "reflection") {
    if (queuedLast !== "llm") {
      enqueueFlowStep("llm", "工具返回结果交给 LLM 生成回答");
    }
    enqueueFlowStep("reflection", "检查回答是否还需要补充检索");
  }
  enqueueFlowStep(module, hint);
  if (OBSERVATION_SOURCES.has(module)) {
    enqueueFlowStep("observation", "工具返回结果作为 observation 回灌");
  }
  playFlowQueue();
}

// 回答落地后不再有「当前步」，全部转成走过的状态。
function flowSettle() {
  if (!flowNodes.length) return;
  for (const node of flowNodes) {
    if (node.classList.contains("is-active")) {
      node.classList.remove("is-active");
      node.classList.add("is-done");
    }
  }
  for (const edge of flowEdges) {
    if (edge.classList.contains("is-active")) {
      edge.classList.remove("is-active");
      edge.classList.add("is-done");
    }
  }
}

function showThinking(label) {
  hideWelcome();
  if (!thinkingEl) {
    thinkingEl = document.createElement("article");
    thinkingEl.className = "message thinking";
    thinkingEl.innerHTML =
      '<span class="text"></span><span class="dots"><span></span><span></span><span></span></span>';
    chatLog.appendChild(thinkingEl);
  }
  thinkingEl.querySelector(".text").textContent = label;
  scrollToBottom();
}

function hideThinking() {
  thinkingEl?.remove();
  thinkingEl = null;
}

function isProgressNotice(text) {
  return /^正在[^\n]{0,36}$/.test(text.trim());
}

const tick = () => new Promise((resolve) => setTimeout(resolve, 16));

async function streamText(bubble, text) {
  const total = text.length;
  const durationMs = Math.min(1400, Math.max(320, total * 3));
  const startedAt = performance.now();
  let shown = 0;

  while (shown < total) {
    await tick();
    const progress = (performance.now() - startedAt) / durationMs;
    shown = Math.min(total, Math.max(shown + 1, Math.ceil(total * progress)));
    const stick = atBottom();
    bubble.innerHTML = renderMarkdown(text.slice(0, shown));
    const cursor = document.createElement("span");
    cursor.className = "cursor";
    (bubble.lastElementChild || bubble).appendChild(cursor);
    if (stick) scrollToBottom();
  }

  bubble.innerHTML = renderMarkdown(text);
}

function parseSseEvents(buffer) {
  const events = [];
  let remaining = buffer;
  let separatorIndex = remaining.indexOf("\n\n");

  while (separatorIndex !== -1) {
    const rawEvent = remaining.slice(0, separatorIndex);
    remaining = remaining.slice(separatorIndex + 2);
    const data = rawEvent
      .split("\n")
      .filter((line) => line.startsWith("data:"))
      .map((line) => line.slice(5).trimStart())
      .join("\n");
    if (data) events.push(JSON.parse(data));
    separatorIndex = remaining.indexOf("\n\n");
  }

  return { events, remaining };
}

async function streamChat(message) {
  const response = await fetch("/api/chat/stream", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message, session_id: conversationId() }),
  });

  if (!response.ok || !response.body) {
    let errorMessage = "请求失败";
    try {
      const result = await response.json();
      errorMessage = result.error || errorMessage;
    } catch {
      errorMessage = response.statusText || errorMessage;
    }
    throw new Error(errorMessage);
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;

    buffer += decoder.decode(value, { stream: true });
    const parsed = parseSseEvents(buffer);
    buffer = parsed.remaining;

    for (const event of parsed.events) {
      if (event.type === "done") {
        await reader.cancel().catch(() => {});
        return;
      }
      if (event.type === "message") {
        const text = event.message || "";
        if (!text.trim()) continue;
        if (isProgressNotice(text)) {
          showThinking(text);
          continue;
        }
        hideThinking();
        await streamText(appendMessage("agent", "", { persist: false }), text);
        appendStoredMessage("agent", text);
        updateContextualSuggestions("", text);
      } else if (event.type === "images") {
        hideThinking();
        appendImages(event.urls, event.caption || "");
      } else if (event.type === "trace_step") {
        flowStep(event.module, event.why || event.label || "");
      } else if (event.type === "status") {
        showThinking(event.message || "思考中");
      } else if (event.type === "error") {
        hideThinking();
        appendMessage("error", `出错了：${event.error || "未知错误"}`);
      }
    }
  }
}

function setBusy(value) {
  busy = value;
  sendButton.disabled = value;
}

function autoGrow() {
  if (!input.value) {
    input.style.height = "";
    return;
  }
  input.style.height = "auto";
  input.style.height = `${Math.min(input.scrollHeight, 180)}px`;
}

async function submitMessage(message) {
  if (busy || !message) return;

  appendMessage("user", message);
  updateContextualSuggestions(message, "");
  input.value = "";
  autoGrow();
  setBusy(true);
  showThinking("思考中");
  flowReset();
  flowStep("entry", "收到问题，交给主 Agent");

  try {
    await streamChat(message);
  } catch (error) {
    appendMessage("error", `出错了：${error.message}`);
  } finally {
    hideThinking();
    flowSettle();
    setBusy(false);
    input.focus();
  }
}

function fillComposer(message) {
  input.value = message;
  autoGrow();
  input.focus();
}

function renderSuggestions(actions) {
  suggestionsEl.replaceChildren();
  const loopActions = [...actions, ...actions];
  for (const action of loopActions) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "suggestion-row";
    button.innerHTML = `
      <span class="suggestion-title"></span>
      <span class="suggestion-arrow" aria-hidden="true">→</span>
    `;
    button.querySelector(".suggestion-title").textContent = action.title;
    button.addEventListener("click", () => fillComposer(action.prompt));
    suggestionsEl.appendChild(button);
  }
}

function applySuggestions() {
  renderSuggestions(contextualActions || defaultActions);
}

function action(title, prompt) {
  return { title, prompt };
}

function normalized(text) {
  return String(text || "").toLowerCase();
}

function eventNameFromText(text) {
  const match = text.match(
    /([\u4e00-\u9fa5A-Za-z0-9\s]{2,18}(?:马拉松|半马|全马|越野跑|越野赛|路跑))/
  );
  return match ? match[1].trim() : "这场比赛";
}

function contextualActionsFor(userText, agentText = "") {
  const combined = `${userText}\n${agentText}`;
  const text = normalized(combined);

  if (/(照片|相册|图片|photo)|找到\s*\d+\s*组照片/.test(combined)) {
    const eventName = eventNameFromText(combined);
    return [
      action("根据这场比赛生成报告", `根据${eventName}照片对应的运动记录生成一下报告`),
      action("查对应运动记录", `根据${eventName}的比赛日期，查一下对应的运动记录`),
      action("查看照片数据", `查看${eventName}照片保存了哪些信息`),
      action("列出全部照片", "查看我保存过的所有比赛照片"),
    ];
  }

  if (/运动记录|历史运动|记录列表|coros|activity|查到\s*最近/.test(text)) {
    return [
      action("分析第 1 条", "分析第 1 条运动记录"),
      action("重点看后半程", "分析第 1 条运动记录，重点看后半程心率和配速"),
      action("生成训练建议", "根据最近这条运动记录，告诉我下一次应该怎么练"),
      action("查个人 PB", "查我的个人 PB"),
    ];
  }

  // 跑鞋：知识库里现在有测评内容，问完一双自然会想比较、想结合自己水平
  if (/跑鞋|碳板|缓震|中底|竞速鞋|训练鞋|穿什么鞋|选鞋|测评/.test(combined)) {
    return [
      action("结合我的水平推荐", "根据我的实际配速和周跑量，这双鞋适合我吗？还有更合适的吗"),
      action("对比另一双", "把知识库里几双同价位的碳板鞋对比一下"),
      action("比赛日怎么选", "我下一场比赛该穿哪双？考虑距离和我的完赛时间"),
      action("看有哪些测评", "知识库里一共有哪些跑鞋测评？"),
    ];
  }

  // 订阅：加完一个来源，接着会想确认状态和进度
  if (/订阅|up主|知识来源|space\.bilibili|导入.*视频|知识库.*添加/.test(combined)) {
    return [
      action("查看当前订阅", "我订阅了哪些知识来源？"),
      action("看知识库有什么", "我的跑步知识库里现在有哪些内容？"),
      action("按分类查跑鞋", "从跑鞋测评里挑一双适合我日常训练的"),
      action("按分类查训练", "从训练理论里讲讲阈值跑该怎么练"),
    ];
  }

  if (/\bpb\b|个人最好|最好成绩|最好记录|半马|全马|成绩瓶颈/.test(text)) {
    return [
      action("制定全马训练计划", "根据我的半马和全马水平，制定一份全马训练计划"),
      action("分析成绩短板", "我现在半马 1:40，全马 4:30，应该加强哪部分训练？"),
      action("查跑步知识库", "根据已导入的跑步书籍，解释我当前成绩瓶颈"),
      action("列出最近运动", "列出我最近 90 天的运动记录"),
    ];
  }

  if (/rag|知识库|书籍|视频|训练计划|丹尼尔斯|跑步书/.test(text)) {
    return [
      action("引用原文回答", "根据跑步知识库，引用原文回答我的训练问题"),
      action("制定训练计划", "根据我的当前能力和知识库，制定一份训练计划"),
      action("解释训练原则", "根据已导入的跑步书籍，解释长距离训练怎么安排"),
      action("查看知识库数据", "解释一下我的 RAG 知识库里有什么"),
    ];
  }

  return null;
}

function updateContextualSuggestions(userText, agentText) {
  contextualActions = contextualActionsFor(userText, agentText);
  applySuggestions();
}

function updateSuggestionsFromConversation() {
  const conversation = activeConversation();
  const messages = Array.isArray(conversation.messages) ? conversation.messages : [];
  const recent = messages.slice(-4);
  const userText = recent
    .filter((message) => message.role === "user")
    .map((message) => message.text)
    .join("\n");
  const agentText = recent
    .filter((message) => message.role !== "user")
    .map((message) => message.text)
    .join("\n");
  contextualActions = contextualActionsFor(userText, agentText);
  applySuggestions();
}

async function loadSuggestions() {
  try {
    const response = await fetch("/api/capabilities");
    if (!response.ok) throw new Error();
    const payload = await response.json();
    const actions = payload.sample_actions || FALLBACK_ACTIONS;
    defaultActions = actions.length ? actions : FALLBACK_ACTIONS;
    applySuggestions();
  } catch {
    defaultActions = FALLBACK_ACTIONS;
    applySuggestions();
  }
}

form.addEventListener("submit", (event) => {
  event.preventDefault();
  submitMessage(input.value.trim());
});

input.addEventListener("input", autoGrow);

input.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    submitMessage(input.value.trim());
  }
});

newChatButton.addEventListener("click", startNewConversation);

function boot() {
  let conversations = loadConversations();
  if (conversations.length) {
    saveConversations(conversations);
    conversations = loadConversations();
  }
  activeSessionId = localStorage.getItem(ACTIVE_SESSION_KEY) || conversations[0]?.id || "";
  if (!conversations.length || !activeSessionId) {
    const conversation = emptyConversation();
    activeSessionId = conversation.id;
    saveConversations([conversation]);
    localStorage.setItem(ACTIVE_SESSION_KEY, activeSessionId);
  }
  renderConversationList();
  renderConversation();
  loadSuggestions();
  updateSuggestionsFromConversation();
  const prompt = new URLSearchParams(window.location.search).get("prompt");
  if (prompt) fillComposer(prompt);
  input.focus();
}

boot();
