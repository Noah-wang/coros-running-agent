const metricStrip = document.querySelector("#metricStrip");
const dataTabs = document.querySelector("#dataTabs");
const dataBoard = document.querySelector("#dataBoard");

let sections = [];
let activeKey = "";

function askUrl(prompt) {
  return `/?prompt=${encodeURIComponent(prompt)}`;
}

function renderMetrics(summary) {
  metricStrip.replaceChildren();
  for (const metric of summary || []) {
    const item = document.createElement("article");
    item.className = "metric-item";
    item.innerHTML = `
      <span></span>
      <strong></strong>
      <small></small>
    `;
    item.querySelector("span").textContent = metric.label || "";
    item.querySelector("strong").textContent = metric.value || "0";
    item.querySelector("small").textContent = metric.detail || "";
    metricStrip.appendChild(item);
  }
}

function renderTabs() {
  dataTabs.replaceChildren();
  for (const section of sections) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = section.key === activeKey ? "active" : "";
    button.textContent = section.title;
    button.addEventListener("click", () => {
      activeKey = section.key;
      renderTabs();
      renderActiveSection();
    });
    dataTabs.appendChild(button);
  }
}

function renderActiveSection() {
  const section = sections.find((item) => item.key === activeKey);
  dataBoard.replaceChildren();
  if (!section) return;

  const header = document.createElement("header");
  header.className = "data-board-head";
  header.innerHTML = `
    <div>
      <p class="eyebrow"></p>
      <h3></h3>
      <p></p>
    </div>
  `;
  header.querySelector(".eyebrow").textContent = "Read only";
  header.querySelector("h3").textContent = section.title;
  header.querySelector("p:last-child").textContent = section.description || "";
  dataBoard.appendChild(header);

  if (Array.isArray(section.tree) && section.tree.length) {
    dataBoard.appendChild(renderKnowledgeTree(section.tree));
  }

  const items = Array.isArray(section.items) ? section.items : [];
  if (!items.length) {
    const empty = document.createElement("section");
    empty.className = "empty-state";
    empty.innerHTML = `
      <h4>No data yet</h4>
      <p>This module does not have saved data yet. After you upload, import, or sync from Discord, it will appear here automatically.</p>
    `;
    dataBoard.appendChild(empty);
    return;
  }

  const grid = document.createElement("div");
  grid.className = section.key === "personal-bests" ? "record-grid pb-grid" : "record-grid";
  for (const item of items) {
    grid.appendChild(renderRecord(item, section.key));
  }
  dataBoard.appendChild(grid);
}

// 知识库按 内容方向 → UP主 → 单条资料 逐级展开。
// 平铺成卡片时二十多条视频堆在一起看不出结构，而分类和 UP 主本来就在数据里。
let treeSelection = { category: 0, group: 0 };

function renderKnowledgeTree(tree) {
  const wrap = document.createElement("section");
  wrap.className = "knowledge-tree";

  const aside = document.createElement("aside");
  aside.className = "tree-rail";
  const detail = document.createElement("div");
  detail.className = "tree-detail";

  function paint() {
    aside.replaceChildren();
    detail.replaceChildren();

    const category = tree[treeSelection.category] || tree[0];
    if (!category) return;

    for (const [ci, cat] of tree.entries()) {
      const catBtn = document.createElement("button");
      catBtn.type = "button";
      catBtn.className = `tree-category ${ci === treeSelection.category ? "active" : ""}`;
      catBtn.innerHTML = `<span class="tree-label"></span><span class="tree-count"></span>`;
      catBtn.querySelector(".tree-label").textContent = cat.label;
      catBtn.querySelector(".tree-count").textContent = String(cat.count);
      catBtn.addEventListener("click", () => {
        treeSelection = { category: ci, group: 0 };
        paint();
      });
      aside.appendChild(catBtn);

      if (ci !== treeSelection.category) continue;
      for (const [gi, group] of (cat.groups || []).entries()) {
        const groupBtn = document.createElement("button");
        groupBtn.type = "button";
        groupBtn.className = [
          "tree-group",
          gi === treeSelection.group ? "active" : "",
          group.count ? "" : "empty",
        ].filter(Boolean).join(" ");
        groupBtn.innerHTML = `<span class="tree-label"></span><span class="tree-count"></span>`;
        groupBtn.querySelector(".tree-label").textContent = group.name;
        // 显示回填进度（已导入/总数），让「订阅了但还没导入」的来源也有反馈
        groupBtn.querySelector(".tree-count").textContent = group.progress || String(group.count);
        groupBtn.addEventListener("click", () => {
          treeSelection = { category: ci, group: gi };
          paint();
        });
        aside.appendChild(groupBtn);
      }
    }

    const group = (category.groups || [])[treeSelection.group] || (category.groups || [])[0];
    if (!group) return;

    const head = document.createElement("p");
    head.className = "tree-detail-head";
    const uidText = group.uid ? ` · UID ${group.uid}` : "";
    head.textContent = `${category.label} · ${group.name}${uidText} · ${group.count} imported`;
    detail.appendChild(head);

    if (!(group.items || []).length) {
      const empty = document.createElement("p");
      empty.className = "tree-empty";
      empty.textContent = group.pending
        ? `This source has not started importing yet. ${group.pending} items are waiting for scheduled sync.`
        : "This source has not imported any content yet.";
      detail.appendChild(empty);
      return;
    }

    for (const item of group.items || []) {
      const row = document.createElement("article");
      row.className = "tree-item";
      row.innerHTML = `<div class="tree-item-main"><h4></h4><p></p></div>`;
      row.querySelector("h4").textContent = item.title || "";
      row.querySelector("p").textContent = item.meta || "";
      if (item.prompt) {
        const ask = document.createElement("a");
        ask.className = "tree-ask";
        ask.href = askUrl(item.prompt);
        ask.textContent = "Ask";
        row.appendChild(ask);
      }
      detail.appendChild(row);
    }
  }

  paint();
  wrap.append(aside, detail);
  return wrap;
}

function renderRecord(item, sectionKey) {
  const article = document.createElement("article");
  article.className = `record-card ${item.state === "empty" ? "muted-card" : ""}`;
  article.innerHTML = `
    <div class="record-main">
      <span class="record-type"></span>
      <h4></h4>
      <p class="record-meta"></p>
      <p class="record-desc"></p>
      <div class="fact-row"></div>
    </div>
    <div class="image-grid"></div>
    <a class="record-action"></a>
  `;
  article.querySelector(".record-type").textContent = labelFor(sectionKey);
  article.querySelector("h4").textContent = item.title || "Untitled data";
  article.querySelector(".record-meta").textContent = item.meta || "";
  article.querySelector(".record-desc").textContent = item.description || "";

  const factRow = article.querySelector(".fact-row");
  const facts = Array.isArray(item.facts) ? item.facts : [];
  if (!facts.length) factRow.remove();
  for (const fact of facts) {
    const factEl = document.createElement("span");
    factEl.innerHTML = `<small></small><strong></strong>`;
    factEl.querySelector("small").textContent = fact.label || "";
    factEl.querySelector("strong").textContent = fact.value || "-";
    factRow.appendChild(factEl);
  }

  const imageGrid = article.querySelector(".image-grid");
  const images = Array.isArray(item.images) ? item.images : [];
  if (!images.length) {
    imageGrid.remove();
  } else {
    for (const src of images.slice(0, 6)) {
      const img = document.createElement("img");
      img.src = src;
      img.alt = item.title || "Data image";
      img.loading = "lazy";
      imageGrid.appendChild(img);
    }
  }

  const action = article.querySelector(".record-action");
  action.textContent = "Ask with this";
  action.href = askUrl(item.prompt || item.title || "");
  return article;
}

function labelFor(key) {
  return {
    profile: "Profile",
    "personal-bests": "PB",
    rag: "Knowledge",
    coros: "COROS",
    photos: "Photos",
  }[key] || "Data";
}

async function boot() {
  try {
    const response = await fetch("/api/data");
    if (!response.ok) throw new Error("Failed to load data");
    const payload = await response.json();
    sections = Array.isArray(payload.sections) ? payload.sections : [];
    activeKey = sections[0]?.key || "";
    renderMetrics(payload.summary || []);
    renderTabs();
    renderActiveSection();
  } catch {
    dataBoard.innerHTML = `
      <section class="empty-state">
        <h4>Data is temporarily unavailable</h4>
        <p>The backend did not return data. Check that COROS Running Agent is running.</p>
      </section>
    `;
  }
}

boot();
