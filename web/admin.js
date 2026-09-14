const tokenInput = document.querySelector("#adminToken");
const content = document.querySelector("#adminContent");
const actions = document.querySelector("#adminActions");
const statusText = document.querySelector("#statusText");
let state = null;

function token() { return sessionStorage.getItem("coros-admin-token") || ""; }
function setStatus(message, error = false) {
  statusText.textContent = message;
  statusText.classList.toggle("error", error);
}
function number(value) { return new Intl.NumberFormat("zh-CN").format(Number(value || 0)); }
function money(value, currency = "USD") {
  if (value === null || value === undefined) return "未计价";
  return new Intl.NumberFormat("zh-CN", { style: "currency", currency, maximumFractionDigits: 4 }).format(value);
}
function node(tag, className = "", text = "") {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text) element.textContent = text;
  return element;
}
function statusBadge(text, good = false, warning = false) {
  const badge = node("span", `admin-status${good ? " good" : ""}${warning ? " warning" : ""}`, text);
  return badge;
}
async function request(method = "GET", body = null) {
  const response = await fetch("/api/admin", {
    method,
    headers: { Authorization: `Bearer ${token()}`, ...(body ? {"Content-Type": "application/json"} : {}) },
    body: body ? JSON.stringify(body) : null,
  });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
  return payload;
}
async function load() {
  try {
    state = await request();
    render();
    content.hidden = false;
    actions.hidden = false;
    setStatus("后台数据已更新");
  } catch (error) {
    content.hidden = true;
    actions.hidden = true;
    setStatus(error.message, true);
  }
}
async function mutate(body) {
  setStatus("正在保存...");
  try {
    state = await request("POST", body);
    render();
    setStatus("已保存");
  } catch (error) {
    setStatus(error.message, true);
    throw error;
  }
}

function renderOverview() {
  const overview = state.overview;
  const billing = state.global_usage_30d;
  document.querySelector("#tenantCount").textContent = number(overview.tenants);
  document.querySelector("#activeTenantCount").textContent = `${number(overview.active_tenants)} 个可用`;
  document.querySelector("#subscriptionCount").textContent = number(overview.active_subscriptions);
  document.querySelector("#trialCount").textContent = `${number(overview.trial_subscriptions)} 个试用`;
  document.querySelector("#callCount").textContent = number(overview.calls);
  document.querySelector("#tokenCount").textContent = `${number(overview.total_tokens)} tokens`;
  document.querySelector("#estimatedCost").textContent = money(billing.estimated_cost, billing.currency);
  document.querySelector("#costNote").textContent = billing.unpriced_models.length
    ? `${billing.unpriced_models.length} 个模型未配置单价`
    : "按已配置单价估算";

  const enabled = state.multi_tenant_enabled;
  document.querySelector("#modeDot").classList.toggle("active", enabled);
  document.querySelector("#modeTitle").textContent = enabled ? "多人路由已启用" : "兼容模式运行中";
  document.querySelector("#modeDescription").textContent = enabled
    ? "机器人会按 Discord 或飞书用户身份隔离数据与会话。"
    : "管理后台已经可用；当前机器人仍沿用原单用户行为，绑定主账号后再开启多人路由。";
}

function identityList(tenant) {
  const wrap = node("div", "identity-list");
  if (!tenant.identities.length) wrap.append(node("span", "muted-text", "尚未绑定"));
  for (const identity of tenant.identities) {
    const item = node("div", "identity-item");
    const label = node("span", "");
    label.append(node("strong", "", identity.provider === "feishu" ? "飞书" : "Discord"));
    label.append(document.createTextNode(` · ${identity.label || identity.external_user_id}`));
    const remove = node("button", "identity-remove", "移除");
    remove.type = "button";
    remove.addEventListener("click", async () => {
      if (!window.confirm("确定移除这个平台身份吗？")) return;
      await mutate({ action: "remove_identity", identity_id: identity.id });
    });
    item.append(label, remove);
    wrap.append(item);
  }
  return wrap;
}

function openEdit(tenant) {
  const form = document.querySelector("#editTenantForm");
  form.elements.tenant_id.value = tenant.id;
  form.elements.name.value = tenant.name;
  form.elements.plan_code.value = tenant.plan_code;
  form.elements.subscription_status.value = tenant.subscription_status;
  form.elements.status.value = tenant.status;
  form.elements.subscription_expires_at.value = (tenant.subscription_expires_at || "").slice(0, 10);
  const owner = tenant.id === "default";
  for (const name of ["plan_code", "subscription_status", "status", "subscription_expires_at"]) {
    form.elements[name].disabled = owner;
  }
  document.querySelector("#ownerEditNote").hidden = !owner;
  document.querySelector("#editTenantDialog").showModal();
}

function openBind(tenant) {
  const form = document.querySelector("#bindIdentityForm");
  form.reset();
  form.elements.tenant_id.value = tenant.id;
  document.querySelector("#bindIdentityDialog").showModal();
}

function renderTenants() {
  const rows = document.querySelector("#tenantRows");
  rows.replaceChildren();
  for (const tenant of state.tenants) {
    const row = document.createElement("tr");
    const nameCell = document.createElement("td");
    nameCell.append(node("strong", "tenant-name", tenant.name));
    nameCell.append(node("small", "tenant-id", tenant.id));
    const identityCell = document.createElement("td");
    identityCell.append(identityList(tenant));
    const corosCell = document.createElement("td");
    corosCell.append(statusBadge(tenant.coros_authorized ? "已授权" : "待授权", tenant.coros_authorized, !tenant.coros_authorized));
    const subscriptionCell = document.createElement("td");
    subscriptionCell.append(node("strong", "", tenant.plan_code));
    subscriptionCell.append(node("small", "tenant-id", tenant.subscription_status));
    const accountCell = document.createElement("td");
    accountCell.append(statusBadge(tenant.status === "active" ? "可用" : tenant.status, tenant.status === "active"));
    const usageCell = document.createElement("td");
    usageCell.append(node("strong", "", `${number(tenant.usage_30d.calls)} 次`));
    usageCell.append(node("small", "tenant-id", `${number(tenant.usage_30d.total_tokens)} tokens`));
    const controls = document.createElement("td");
    const controlWrap = node("div", "row-actions");
    const bind = node("button", "table-button", "绑定");
    bind.type = "button";
    bind.addEventListener("click", () => openBind(tenant));
    const edit = node("button", "table-button", "编辑");
    edit.type = "button";
    edit.addEventListener("click", () => openEdit(tenant));
    controlWrap.append(bind, edit);
    controls.append(controlWrap);
    row.append(nameCell, identityCell, corosCell, subscriptionCell, accountCell, usageCell, controls);
    rows.append(row);
  }
}

function renderIntegrations() {
  const rows = document.querySelector("#integrationRows");
  rows.replaceChildren();
  for (const integration of state.integrations) {
    const row = document.createElement("tr");
    const name = document.createElement("td");
    name.append(node("strong", "", integration.name));
    name.append(node("small", "tenant-id", integration.endpoint));
    const status = document.createElement("td");
    status.append(statusBadge(integration.configured ? "已配置" : "未配置", integration.configured, !integration.configured));
    const detail = node("td", "", integration.detail);
    row.append(name, status, detail);
    rows.append(row);
  }
}

function renderUsage() {
  const rows = document.querySelector("#usageRows");
  rows.replaceChildren();
  const usage = state.global_usage_30d;
  const entries = Object.entries(usage.by_model || {});
  if (!entries.length) {
    const row = document.createElement("tr");
    const cell = node("td", "empty-table", "还没有近 30 天模型调用记录");
    cell.colSpan = 4;
    row.append(cell);
    rows.append(row);
    return;
  }
  for (const [model, item] of entries) {
    const row = document.createElement("tr");
    row.append(
      node("td", "", model),
      node("td", "", number(item.calls)),
      node("td", "", number(item.total_tokens)),
      node("td", "", money(item.estimated_cost, usage.currency)),
    );
    rows.append(row);
  }
}

function renderAudit() {
  const list = document.querySelector("#auditList");
  list.replaceChildren();
  const labels = {
    "tenant.created": "创建用户",
    "tenant.updated": "更新用户",
    "identity.bound": "绑定平台身份",
    "identity.removed": "移除平台身份",
  };
  for (const event of state.audit) {
    const item = document.createElement("li");
    const main = node("div", "");
    main.append(node("strong", "", labels[event.action] || event.action));
    main.append(node("span", "", event.tenant_id || "system"));
    item.append(main, node("time", "", new Date(event.created_at).toLocaleString("zh-CN")));
    list.append(item);
  }
}

function render() {
  renderOverview();
  renderTenants();
  renderIntegrations();
  renderUsage();
  renderAudit();
}

document.querySelector("#authPanel").addEventListener("submit", (event) => {
  event.preventDefault();
  sessionStorage.setItem("coros-admin-token", tokenInput.value.trim());
  load();
});
document.querySelector("#refreshButton").addEventListener("click", load);
document.querySelector("#createTenantButton").addEventListener("click", () => {
  document.querySelector("#createTenantForm").reset();
  document.querySelector("#createTenantDialog").showModal();
});
document.querySelectorAll("[data-close]").forEach((button) => button.addEventListener("click", () => {
  document.querySelector(`#${button.dataset.close}`).close();
}));
document.querySelector("#createTenantForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const data = new FormData(event.currentTarget);
  await mutate({ action: "create_tenant", name: data.get("name"), plan_code: data.get("plan_code") });
  document.querySelector("#createTenantDialog").close();
});
document.querySelector("#editTenantForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const data = new FormData(form);
  const body = {
    action: "update_tenant",
    tenant_id: data.get("tenant_id"),
    name: data.get("name"),
  };
  if (data.get("tenant_id") !== "default") {
    Object.assign(body, {
      plan_code: data.get("plan_code"),
      subscription_status: data.get("subscription_status"),
      status: data.get("status"),
      subscription_expires_at: data.get("subscription_expires_at"),
    });
  }
  await mutate(body);
  document.querySelector("#editTenantDialog").close();
});
document.querySelector("#bindIdentityForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const data = new FormData(event.currentTarget);
  await mutate({
    action: "bind_identity",
    tenant_id: data.get("tenant_id"),
    provider: data.get("provider"),
    external_user_id: data.get("external_user_id"),
    workspace_id: data.get("workspace_id"),
    label: data.get("label"),
  });
  document.querySelector("#bindIdentityDialog").close();
});

tokenInput.value = token();
if (token()) load();
