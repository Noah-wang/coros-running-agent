// 中英文切换。
//
// 三层文案要一起切，缺一层就会露馅：
//   1. 界面外壳（这里）—— 导航、按钮、占位符、空状态
//   2. 后端返回的文案 —— 数据页分区标题、快捷提问，见 web_server.py 的 _text()
//   3. Agent 的回答 —— 语言偏好随请求发给后端，注入系统提示词
//
// 只做前两层的话，界面是英文而 Agent 用中文回答，比全中文还怪。
//
// 默认英文：这是个开源项目，陌生访客占多数。选择存 localStorage，
// 切一次之后就记住了。

const STORAGE_KEY = "coros-agent-lang";
export const LANGS = ["en", "zh"];
const DEFAULT_LANG = "en";

const DICT = {
  // ── 通用外壳 ──────────────────────────────────────────
  "nav.chat": { en: "Chat", zh: "对话" },
  "nav.data": { en: "Data", zh: "数据" },
  "nav.tech": { en: "Tech", zh: "技术" },
  "lang.toggle": { en: "中文", zh: "EN" },
  "lang.toggleAria": { en: "Switch to Chinese", zh: "Switch to English" },

  // ── 对话页 ───────────────────────────────────────────
  "chat.newChat": { en: "New chat", zh: "新建对话" },
  "chat.conversationList": { en: "Conversation list", zh: "对话列表" },
  "chat.welcomeBody": {
    en: "This web console can read workouts, personal bests, running knowledge, and COROS archives. Write actions stay in Discord.",
    zh: "这个网页可以查询运动记录、PB、跑步知识库和 COROS 归档数据；写入操作只在 Discord 里开放。",
  },
  "chat.suggestions": { en: "Suggested questions", zh: "快捷问题" },
  "chat.message": { en: "Message", zh: "消息" },
  "chat.placeholder": {
    en: "Ask a question, or click a suggested prompt above...",
    zh: "输入问题，或点击上方快捷问题填入……",
  },
  "chat.send": { en: "Send", zh: "发送" },
  "chat.thinking": { en: "Thinking", zh: "思考中" },
  "chat.currentChat": { en: "Current chat", zh: "当前对话" },
  "chat.emptyChat": { en: "Empty chat", zh: "空白对话" },
  "chat.messageCount": { en: "messages", zh: "条消息" },
  "chat.failed": { en: "Something went wrong", zh: "出错了" },
  "chat.requestFailed": { en: "Request failed", zh: "请求失败" },
  "chat.unknownError": { en: "Unknown error", zh: "未知错误" },

  // ── 调用链路图 ───────────────────────────────────────
  "flow.title": { en: "Execution trace", zh: "调用链路" },
  "flow.hint": { en: "Nodes light up after you ask", zh: "提问后节点会在图上亮起" },
  "flow.expand": { en: "Show execution trace", zh: "查看调用链路" },
  "flow.collapse": { en: "Hide execution trace", zh: "收起调用链路" },
  "flow.entry": { en: "Input", zh: "入口" },
  "flow.router": { en: "Router", zh: "语义路由" },
  "flow.loop": { en: "Agent loop", zh: "主循环" },
  "flow.capability": { en: "Capability", zh: "能力层" },
  "flow.langgraph": { en: "LangGraph", zh: "LangGraph" },
  "flow.coros": { en: "COROS MCP", zh: "COROS MCP" },
  "flow.profile": { en: "Profile", zh: "长期档案" },
  "flow.knowledge": { en: "RAG", zh: "RAG" },
  "flow.search": { en: "Search", zh: "搜索" },
  "flow.observation": { en: "Observation", zh: "工具返回" },
  "flow.llm": { en: "LLM", zh: "LLM 生成" },
  "flow.reflection": { en: "Reflect", zh: "反思检查" },
  "flow.answer": { en: "Answer", zh: "生成回答" },

  // ── 新运动提醒 ───────────────────────────────────────
  "notice.eyebrow": { en: "New activity detected", zh: "检测到新运动" },
  "notice.title": { en: "Completed workout", zh: "已完成的训练" },
  "notice.meta": { en: "Ready for AI review", zh: "可以让 AI 复盘了" },
  "notice.interpret": { en: "Interpret", zh: "解读一下" },
  "notice.dismiss": { en: "Dismiss workout notice", zh: "关闭运动提醒" },

  // ── 数据页 ───────────────────────────────────────────
  "data.eyebrow": { en: "Personal Agent Data", zh: "个人 Agent 数据" },
  "data.title": { en: "Your Agent Memory", zh: "你的 Agent 记忆库" },
  "data.back": { en: "Back to chat", zh: "回到对话" },
  "data.overview": { en: "Data overview", zh: "数据总览" },
  "data.categories": { en: "Data categories", zh: "数据分类" },
  "data.loading": { en: "Loading...", zh: "加载中……" },
  "data.loadFailed": { en: "Failed to load data", zh: "数据加载失败" },
  "data.subtitle": {
    en: "This page shows data that the public web console can read: PBs, running knowledge, COROS archives, and route assets. The public web surface is read-only; writes still happen through Discord.",
    zh: "这里集中展示网页可读的数据：PB、跑步知识库、COROS 原始记录和路线图素材。公开网页只读，写入和修改仍通过 Discord 完成。",
  },
  "data.readOnly": { en: "Read only", zh: "只读" },
  "data.askWith": { en: "Ask with this", zh: "用它提问" },
  "data.empty": { en: "Nothing here yet", zh: "这里还没有内容" },

  // ── 技术页 ───────────────────────────────────────────
  "tech.sections": { en: "Sections", zh: "章节" },
  "tech.loadFailed": { en: "Failed to load", zh: "加载失败" },
};

export function getLang() {
  const saved = localStorage.getItem(STORAGE_KEY);
  return LANGS.includes(saved) ? saved : DEFAULT_LANG;
}

export function setLang(lang) {
  if (!LANGS.includes(lang)) return;
  localStorage.setItem(STORAGE_KEY, lang);
  document.documentElement.lang = lang === "zh" ? "zh-CN" : "en";
}

export function t(key) {
  const entry = DICT[key];
  if (!entry) return key; // 缺 key 时显示 key 本身，比显示空字符串好定位
  return entry[getLang()] ?? entry[DEFAULT_LANG] ?? key;
}

/** 把页面上带 data-i18n* 标记的地方按当前语言刷一遍。 */
export function applyI18n(root = document) {
  document.documentElement.lang = getLang() === "zh" ? "zh-CN" : "en";
  for (const el of root.querySelectorAll("[data-i18n]")) {
    el.textContent = t(el.dataset.i18n);
  }
  for (const el of root.querySelectorAll("[data-i18n-placeholder]")) {
    el.placeholder = t(el.dataset.i18nPlaceholder);
  }
  for (const el of root.querySelectorAll("[data-i18n-aria]")) {
    el.setAttribute("aria-label", t(el.dataset.i18nAria));
  }
  for (const el of root.querySelectorAll("[data-i18n-title]")) {
    el.setAttribute("title", t(el.dataset.i18nTitle));
  }
}

/**
 * 在顶栏插一个切换按钮。
 *
 * 切换后整页重载，而不是逐个刷新组件。看起来粗暴，但这里是对的：
 * 后端返回的文案（数据页分区、快捷提问）也要跟着换，逐个刷新等于把
 * 「哪些东西依赖语言」这件事散落到每个渲染函数里，漏一个就是半中半英。
 * 重载一次全部重取，语言状态只有一个来源。
 */
export function mountLangToggle() {
  const nav = document.querySelector(".topnav");
  if (!nav || nav.querySelector(".lang-toggle")) return;

  const button = document.createElement("button");
  button.type = "button";
  button.className = "lang-toggle";
  button.textContent = t("lang.toggle");
  button.setAttribute("aria-label", t("lang.toggleAria"));
  button.addEventListener("click", () => {
    setLang(getLang() === "zh" ? "en" : "zh");
    window.location.reload();
  });
  nav.appendChild(button);
}

/** 语言偏好要随请求发给后端：数据页文案和 Agent 的回答语言都由它决定。 */
export function langParam() {
  return `lang=${getLang()}`;
}
