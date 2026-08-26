import { renderMarkdown } from "/markdown.js";

const tabsEl = document.querySelector("#tabs");
const navEl = document.querySelector("#docNav");
const bodyEl = document.querySelector("#docBody");

const state = { tabs: [], activeTab: 0, activeItem: 0 };

function renderTabs() {
  tabsEl.replaceChildren();
  state.tabs.forEach((tab, index) => {
    const button = document.createElement("button");
    button.type = "button";
    button.role = "tab";
    button.textContent = `${tab.title}（${tab.items.length}）`;
    button.className = index === state.activeTab ? "active" : "";
    button.setAttribute("aria-selected", String(index === state.activeTab));
    button.addEventListener("click", () => {
      state.activeTab = index;
      state.activeItem = 0;
      render();
    });
    tabsEl.appendChild(button);
  });
}

function renderNav() {
  const tab = state.tabs[state.activeTab];
  navEl.replaceChildren();
  if (!tab) return;

  tab.items.forEach((item, index) => {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = item.title;
    button.className = index === state.activeItem ? "active" : "";
    button.addEventListener("click", () => {
      state.activeItem = index;
      render();
      bodyEl.scrollIntoView({ behavior: "smooth", block: "start" });
    });
    navEl.appendChild(button);
  });
}

function renderBody() {
  const tab = state.tabs[state.activeTab];
  const item = tab?.items[state.activeItem];
  if (!item) {
    bodyEl.innerHTML = "<p class='empty'>这一部分暂时没有内容。</p>";
    return;
  }
  bodyEl.innerHTML = `<h2>${item.title}</h2>${renderMarkdown(item.body)}`;
}

function render() {
  renderTabs();
  renderNav();
  renderBody();
}

async function load() {
  try {
    const response = await fetch("/api/tech");
    if (!response.ok) throw new Error("加载失败");
    const payload = await response.json();
    state.tabs = (payload.tabs || []).filter((tab) => tab.items?.length);
    if (!state.tabs.length) {
      bodyEl.innerHTML = "<p class='empty'>暂时没有可展示的内容。</p>";
      return;
    }
    render();
  } catch (error) {
    bodyEl.innerHTML = `<p class='empty'>加载失败：${error.message}</p>`;
  }
}

load();
