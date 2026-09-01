import { applyI18n, mountLangToggle, t } from "/i18n.js";

const tokenInput = document.querySelector("#adminToken");
const content = document.querySelector("#settingsContent");
const statusText = document.querySelector("#statusText");
const skillSelect = document.querySelector("#skillSelect");
const skillName = document.querySelector("#skillName");
const skillContent = document.querySelector("#skillContent");
let state = null;
let activeKind = "coach";

function token() { return sessionStorage.getItem("coros-settings-token") || ""; }
function status(message, error = false) {
  statusText.textContent = message;
  statusText.classList.toggle("error", error);
}
async function request(method = "GET", body = null) {
  const response = await fetch("/api/settings", {
    method,
    headers: { Authorization: `Bearer ${token()}`, ...(body ? {"Content-Type": "application/json"} : {}) },
    body: body ? JSON.stringify(body) : null,
  });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
  return payload;
}
function currentSkills() { return state?.skills?.[activeKind] || []; }
function renderSkills(preferredId = "") {
  skillSelect.replaceChildren();
  for (const skill of currentSkills()) {
    const option = document.createElement("option");
    option.value = skill.id;
    option.textContent = `${skill.active ? "● " : ""}${skill.name} · v${skill.version}`;
    skillSelect.appendChild(option);
  }
  const selected = currentSkills().find((item) => item.id === preferredId)
    || currentSkills().find((item) => item.active) || currentSkills()[0];
  if (!selected) return;
  skillSelect.value = selected.id;
  skillName.value = selected.source === "built-in" ? "" : selected.name;
  skillContent.value = selected.content || "";
}
function render() {
  document.querySelector("#autoReportToggle").checked = !!state.automations.auto_report;
  document.querySelector("#sleepReportToggle").checked = !!state.automations.sleep_report;
  renderSkills();
  content.hidden = false;
}
async function unlock() {
  sessionStorage.setItem("coros-settings-token", tokenInput.value.trim());
  try { state = await request(); render(); status(t("settings.ready")); }
  catch (error) { content.hidden = true; status(error.message, true); }
}
async function mutate(body) {
  try { state = await request("POST", body); render(); status(t("settings.saved")); }
  catch (error) { status(error.message, true); }
}
document.querySelector("#unlockButton").addEventListener("click", unlock);
for (const [id, name] of [["autoReportToggle", "auto_report"], ["sleepReportToggle", "sleep_report"]]) {
  document.querySelector(`#${id}`).addEventListener("change", (event) =>
    mutate({ action: "set_automation", name, enabled: event.target.checked }));
}
document.querySelectorAll("[data-kind]").forEach((button) => button.addEventListener("click", () => {
  activeKind = button.dataset.kind;
  document.querySelectorAll("[data-kind]").forEach((item) => item.classList.toggle("active", item === button));
  renderSkills();
}));
skillSelect.addEventListener("change", () => renderSkills(skillSelect.value));
document.querySelector("#activateSkill").addEventListener("click", () =>
  mutate({ action: "activate_skill", kind: activeKind, skill_id: skillSelect.value }));
document.querySelector("#resetSkill").addEventListener("click", () =>
  mutate({ action: "reset_skill", kind: activeKind }));
document.querySelector("#saveSkill").addEventListener("click", () =>
  mutate({ action: "save_skill", kind: activeKind, name: skillName.value, content: skillContent.value, activate: true }));
document.querySelector("#skillFile").addEventListener("change", async (event) => {
  const file = event.target.files?.[0];
  if (!file) return;
  if (file.size > 64 * 1024) return status("Skill file must be 64 KB or smaller.", true);
  skillContent.value = await file.text();
  if (!skillName.value) skillName.value = file.name.replace(/\.md$/i, "");
});
applyI18n();
mountLangToggle();
tokenInput.value = token();
if (token()) unlock();
