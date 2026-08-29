import { renderMarkdown } from "/markdown.js";
import { t, applyI18n, mountLangToggle, getLang } from "/i18n.js";

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
const activityNotice = document.querySelector("#activityNotice");
const activityNoticeTitle = document.querySelector("#activityNoticeTitle");
const activityNoticeMeta = document.querySelector("#activityNoticeMeta");
const activityNoticeDismiss = document.querySelector("#activityNoticeDismiss");
const activityNoticeInterpret = document.querySelector("#activityNoticeInterpret");

const ACTIVE_SESSION_KEY = "coros-running-agent-active-session";
const CONVERSATIONS_KEY = "coros-running-agent-conversations-v1";
const SEEN_ACTIVITY_NOTICE_KEY = "coros-running-agent-seen-activity-notices-v1";
const FALLBACK_ACTIONS = [
  { title: "List my last 90 days", prompt: "List my COROS activities from the last 90 days" },
  { title: "Show saved race photos", prompt: "Show all race photos I have saved" },
  { title: "Review my latest workout", prompt: "How was my latest workout, and what should I do next?" },
  { title: "Show my PBs", prompt: "Show my personal bests" },
  { title: "Find my marathon bottleneck", prompt: "My half marathon is 1:40 and my marathon is 4:30. What should I improve?" },
  { title: "Search running knowledge", prompt: "Use my imported running books to explain my current performance bottleneck" },
  { title: "Build a training plan", prompt: "Use my current fitness and knowledge base to build a marathon training plan" },
];

let busy = false;
let thinkingEl = null;
let activeSessionId = "";
let defaultActions = FALLBACK_ACTIONS;
let contextualActions = null;
let flowQueue = [];
let flowPlaying = false;
let lastFlowModule = null;
let pendingActivityNotice = null;
let activeStreamBubble = null;
let activeStreamText = "";
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
    title: t("chat.currentChat"),
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
    // 标题的默认值随语言变，所以两种语言的默认标题都要认，
    // 否则切一次语言之后旧对话的标题就不会再被首句覆盖了。
    const defaults = ["新对话", "当前对话", "New chat", "Current chat"];
    if (role === "user" && defaults.includes(conversation.title)) {
      conversation.title = text.slice(0, 26) || t("chat.currentChat");
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

function seenActivityNotices() {
  try {
    const value = JSON.parse(localStorage.getItem(SEEN_ACTIVITY_NOTICE_KEY) || "[]");
    return Array.isArray(value) ? value.filter(Boolean).slice(-30) : [];
  } catch {
    return [];
  }
}

function markActivityNoticeSeen(key) {
  if (!key) return;
  const next = [...new Set([...seenActivityNotices(), key])].slice(-30);
  localStorage.setItem(SEEN_ACTIVITY_NOTICE_KEY, JSON.stringify(next));
}

function hideActivityNotice() {
  if (!activityNotice) return;
  activityNotice.hidden = true;
  pendingActivityNotice = null;
}

function showActivityNotice(activity) {
  if (!activityNotice || !activity) return;
  pendingActivityNotice = activity;
  activityNoticeTitle.textContent = activity.title || "Completed workout";
  activityNoticeMeta.textContent = activity.meta || "Ready for AI review";
  activityNotice.hidden = false;
}

async function checkActivityNotice() {
  if (!activityNotice || document.hidden) return;
  try {
    const response = await fetch(`/api/auto-report/latest?lang=${getLang()}`);
    if (!response.ok) return;
    const payload = await response.json();
    const activity = payload.activity;
    if (!payload.pending || !activity?.key || seenActivityNotices().includes(activity.key)) {
      return;
    }
    showActivityNotice(activity);
  } catch {
    // The notice is opportunistic. Chat should keep working even if COROS is unavailable.
  }
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
    const title = ["新对话", "当前对话", "New chat"].includes(conversation.title)
      ? t("chat.currentChat")
      : conversation.title;
    button.querySelector(".conversation-title").textContent = title || t("chat.currentChat");
    const count = Array.isArray(conversation.messages) ? conversation.messages.length : 0;
    button.querySelector(".conversation-meta").textContent = count
      ? `${count} ${t("chat.messageCount")}`
      : t("chat.emptyChat");
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
    title.textContent = `${caption} · ${urls.length} images`;
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
    img.alt = caption || "Race photo";
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
  if (flowHint) flowHint.textContent = t("flow.hint");
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
      enqueueFlowStep("llm", "Tool results are passed to the LLM");
    }
    enqueueFlowStep("reflection", "Check whether the answer needs more evidence");
  }
  enqueueFlowStep(module, hint);
  if (OBSERVATION_SOURCES.has(module)) {
    enqueueFlowStep("observation", "Tool result is fed back as an observation");
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
  return /^(正在[^\n]{0,36}|[A-Z][^\n]{0,48}\.\.\.)$/.test(text.trim());
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

function renderActiveStream() {
  if (!activeStreamBubble) return;
  const stick = atBottom();
  activeStreamBubble.innerHTML = renderMarkdown(activeStreamText);
  const cursor = document.createElement("span");
  cursor.className = "cursor";
  (activeStreamBubble.lastElementChild || activeStreamBubble).appendChild(cursor);
  if (stick) scrollToBottom();
}

function startAgentStream() {
  hideThinking();
  if (activeStreamBubble) return;
  activeStreamText = "";
  activeStreamBubble = appendMessage("agent", "", { persist: false });
  renderActiveStream();
}

function appendAgentStream(delta) {
  if (!delta) return;
  if (!activeStreamBubble) startAgentStream();
  activeStreamText += delta;
  renderActiveStream();
}

function finishAgentStream(message = "") {
  if (!activeStreamBubble) return;
  const finalText = message || activeStreamText;
  activeStreamBubble.innerHTML = renderMarkdown(finalText);
  appendStoredMessage("agent", finalText);
  updateContextualSuggestions("", finalText);
  activeStreamBubble = null;
  activeStreamText = "";
  scrollToBottom();
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
    // 语言随请求发给后端：它决定 Agent 用哪种语言回答
    body: JSON.stringify({ message, session_id: conversationId(), lang: getLang() }),
  });

  if (!response.ok || !response.body) {
    let errorMessage = t("chat.requestFailed");
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
        finishAgentStream();
        await reader.cancel().catch(() => {});
        return;
      }
      if (event.type === "message_start") {
        startAgentStream();
      } else if (event.type === "message_delta") {
        appendAgentStream(event.delta || "");
      } else if (event.type === "message_end") {
        finishAgentStream(event.message || "");
      } else if (event.type === "message") {
        const text = event.message || "";
        if (!text.trim()) continue;
        if (isProgressNotice(text)) {
          showThinking(text);
          continue;
        }
        finishAgentStream();
        hideThinking();
        await streamText(appendMessage("agent", "", { persist: false }), text);
        appendStoredMessage("agent", text);
        updateContextualSuggestions("", text);
      } else if (event.type === "images") {
        finishAgentStream();
        hideThinking();
        appendImages(event.urls, event.caption || "");
      } else if (event.type === "trace_step") {
        flowStep(event.module, event.why || event.label || "");
      } else if (event.type === "status") {
        showThinking(event.message || t("chat.thinking"));
      } else if (event.type === "error") {
        finishAgentStream();
        hideThinking();
        appendMessage("error", `${t("chat.failed")}: ${event.error || t("chat.unknownError")}`);
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
  showThinking(t("chat.thinking"));
  flowReset();
  flowStep("entry", "Received the question and sent it to the main agent");

  try {
    await streamChat(message);
  } catch (error) {
    appendMessage("error", `Error: ${error.message}`);
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
  return match ? match[1].trim() : "this race";
}

function contextualActionsFor(userText, agentText = "") {
  const combined = `${userText}\n${agentText}`;
  const text = normalized(combined);

  if (/(照片|相册|图片|photo)|找到\s*\d+\s*组照片/.test(combined)) {
    const eventName = eventNameFromText(combined);
    return [
      action("Generate a race report", `Find the workout record that matches ${eventName} and generate a report`),
      action("Find the matching workout", `Use the race date for ${eventName} to find the matching COROS activity`),
      action("Show photo metadata", `Show what metadata is saved for ${eventName}`),
      action("List all photos", "Show all race photos I have saved"),
    ];
  }

  if (/运动记录|历史运动|记录列表|coros|activity|查到\s*最近/.test(text)) {
    return [
      action("Analyze item 1", "Analyze activity 1"),
      action("Check the second half", "Analyze activity 1, focusing on second-half heart rate and pace"),
      action("Suggest next workout", "Based on the latest activity, tell me what I should do next"),
      action("Show my PBs", "Show my personal bests"),
    ];
  }

  // 跑鞋：知识库里现在有测评内容，问完一双自然会想比较、想结合自己水平
  if (/跑鞋|碳板|缓震|中底|竞速鞋|训练鞋|穿什么鞋|选鞋|测评/.test(combined)) {
    return [
      action("Match shoes to my level", "Based on my pace and weekly mileage, is this shoe a good fit? What else should I consider?"),
      action("Compare similar shoes", "Compare a few carbon-plated shoes from the knowledge base in the same price range"),
      action("Race-day choice", "Which shoe should I wear for my next race, considering the race distance and target time?"),
      action("Show shoe reviews", "What shoe reviews are currently in the knowledge base?"),
    ];
  }

  // 订阅：加完一个来源，接着会想确认状态和进度
  if (/订阅|up主|知识来源|space\.bilibili|导入.*视频|知识库.*添加/.test(combined)) {
    return [
      action("Show subscriptions", "What knowledge sources am I subscribed to?"),
      action("Inspect knowledge base", "What is currently in my running knowledge base?"),
      action("Find daily trainer shoes", "Pick a daily trainer from the shoe reviews in my knowledge base"),
      action("Explain threshold runs", "Use the training theory knowledge to explain how to train threshold runs"),
    ];
  }

  if (/\bpb\b|个人最好|最好成绩|最好记录|半马|全马|成绩瓶颈/.test(text)) {
    return [
      action("Build marathon plan", "Use my half-marathon and marathon level to build a marathon training plan"),
      action("Analyze performance gap", "My half marathon is 1:40 and my marathon is 4:30. What should I improve?"),
      action("Use running knowledge", "Use my imported running books to explain my current performance bottleneck"),
      action("List recent activities", "List my COROS activities from the last 90 days"),
    ];
  }

  if (/rag|知识库|书籍|视频|训练计划|丹尼尔斯|跑步书/.test(text)) {
    return [
      action("Answer with citations", "Use the running knowledge base and cite original passages"),
      action("Build a training plan", "Use my current fitness and knowledge base to build a training plan"),
      action("Explain training principles", "Use the imported running books to explain how to schedule long runs"),
      action("Inspect RAG data", "Explain what is inside my RAG knowledge base"),
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
    const response = await fetch(`/api/capabilities?lang=${getLang()}`);
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

activityNoticeDismiss?.addEventListener("click", () => {
  markActivityNoticeSeen(pendingActivityNotice?.key);
  hideActivityNotice();
});

activityNoticeInterpret?.addEventListener("click", () => {
  const activity = pendingActivityNotice;
  if (!activity) return;
  markActivityNoticeSeen(activity.key);
  hideActivityNotice();
  startNewConversation();
  submitMessage(activity.prompt || "Generate a detailed report for my latest COROS workout");
});

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
  // 先刷文案再渲染内容：反过来的话会先闪一下默认语言
  applyI18n();
  mountLangToggle();
  renderConversationList();
  renderConversation();
  loadSuggestions();
  updateSuggestionsFromConversation();
  const prompt = new URLSearchParams(window.location.search).get("prompt");
  if (prompt) fillComposer(prompt);
  window.setTimeout(checkActivityNotice, 1200);
  window.setInterval(checkActivityNotice, 5 * 60 * 1000);
  input.focus();
}

boot();
