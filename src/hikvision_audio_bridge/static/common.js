export const $ = (id) => document.getElementById(id);
export let state = null;
let token = "";
let render = () => {};
let busy = false;
let refreshing = false;
let revision = 0;
let refreshError = false;

export function message(id, text) {
  $(id).textContent = text || "";
  $(id).hidden = !text;
}

function session(active) {
  $("app").hidden = !active;
  $("login-panel").hidden = active;
  $("session-tools").hidden = !active;
}

function signOut() {
  revision += 1;
  token = "";
  state = null;
  sessionStorage.removeItem("hikvision-access");
  session(false);
  $("login-token").value = "";
  document.querySelectorAll("#app form").forEach((form) => form.reset());
  document.querySelectorAll("[data-edit-id]").forEach((input) => { input.readOnly = false; });
  document.querySelectorAll("[data-secret]").forEach((input) => { input.value = ""; });
  document.dispatchEvent(new Event("audio-signout"));
}

async function request(command) {
  const usedToken = token;
  const options = {
    headers: { Authorization: `Bearer ${usedToken}` },
    cache: "no-store",
    signal: AbortSignal.timeout(25000),
  };
  if (command) {
    options.method = "POST";
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(command);
  }
  const response = await fetch(command ? "/api/command" : "/api/status", options);
  const data = await response.json();
  if (usedToken !== token) throw new Error("The browser session changed.");
  if (response.status === 401) {
    signOut();
    const error = new Error("Access token rejected. Open a fresh login URL or enter the correct token.");
    message("login-error", error.message);
    throw error;
  }
  if (!response.ok || data.ok !== true) throw new Error(data.error || "Request failed.");
  return data.status;
}

function accept(next) {
  state = { ...next };
  delete state.audio_access_token;
  delete state.queued_job;
  session(true);
  render(state);
  const lan = !["localhost", "127.0.0.1", "[::1]"].includes(location.hostname);
  message("live-warning", location.protocol === "http:" && lan
    ? "Plaintext LAN access is enabled. Passwords, access tokens and audio are not encrypted in transit."
    : "");
}

export async function refresh() {
  if (!token || busy || refreshing) return;
  refreshing = true;
  const started = revision;
  try {
    const next = await request();
    if (started !== revision || busy) return;
    accept(next);
    message("login-error", "");
    if (refreshError) message("app-error", "");
    refreshError = false;
  } catch (error) {
    if (started !== revision) return;
    refreshError = true;
    message(state ? "app-error" : "login-error", error.message);
  } finally {
    refreshing = false;
  }
}

export async function command(value) {
  if (busy) throw new Error("Wait for the current change to finish.");
  busy = true;
  revision += 1;
  refreshError = false;
  const controls = Array.from(document.querySelectorAll("#app button, #app input, #app select, #app textarea"))
    .map((control) => [control, control.disabled]);
  controls.forEach(([control]) => { control.disabled = true; });
  message("app-error", "");
  message("app-notice", "");
  try {
    const next = await request(value);
    accept(next);
    message("app-notice", "Change applied.");
    return next;
  } finally {
    busy = false;
    controls.forEach(([control, disabled]) => { control.disabled = disabled; });
  }
}

export function action(callback) {
  return async (event) => {
    event?.preventDefault();
    try { await callback(event); }
    catch (error) { refreshError = false; message("app-error", error.message); }
  };
}

export function form(id, callback) {
  $(id).addEventListener("submit", action(async () => {
    if ($(id).reportValidity()) await callback();
  }));
}

export function selected(id) {
  return Array.from($(id).selectedOptions, (option) => option.value);
}

export function options(id, entries, values = null) {
  const select = $(id);
  const chosen = new Set(values ?? selected(id));
  const available = new Map(entries);
  for (const value of chosen) {
    if (!available.has(value)) available.set(value, `${value} (unavailable)`);
  }
  const signature = JSON.stringify([...available]);
  if (select.dataset.options !== signature) {
    select.replaceChildren(...Array.from(available, ([value, label]) => new Option(label, value)));
    select.dataset.options = signature;
  }
  for (const option of select.options) option.selected = chosen.has(option.value);
}

export function element(tag, className, text = "") {
  const node = document.createElement(tag);
  if (className) node.className = className;
  node.textContent = text;
  return node;
}

export function button(label, callback, danger = false) {
  const node = element("button", `button ${danger ? "button-danger" : "button-secondary"}`, label);
  node.type = "button";
  node.addEventListener("click", action(callback));
  return node;
}

export function card(actions = []) {
  const node = element("article", "entity");
  const header = element("div", "entity-header");
  const title = element("h3", "entity-title");
  const badge = element("span", "badge badge-idle");
  const subtitle = element("p", "entity-subtitle");
  const detail = element("p", "hint");
  const error = element("p", "field-error");
  error.hidden = true;
  header.append(title, badge);
  const tools = element("div", "entity-actions");
  tools.append(...actions);
  node.append(header, subtitle, detail, error, tools);
  node.parts = { title, badge, subtitle, detail, error, tools };
  return node;
}

export function badge(node, value, label = value) {
  const known = ["idle", "connecting", "connected", "error", "synthesizing", "playing", "speaker", "microphone"];
  node.className = `badge badge-${known.includes(value) ? value : "unknown"}`;
  node.textContent = label;
}

export function errorText(node, text) {
  node.textContent = text || "";
  node.hidden = !text;
}

export function list(id, entries, create, update, empty = "Nothing configured yet.") {
  const root = $(id);
  const retained = new Map(Array.from(root.children, (node) => [node.dataset.key, node]));
  if (!entries.length) {
    if (root.children.length !== 1 || !root.firstElementChild.classList.contains("empty-state")) {
      root.replaceChildren(element("p", "empty-state", empty));
    }
    return;
  }
  const keys = new Set(entries.map(([key]) => key));
  for (const node of Array.from(root.children)) if (!keys.has(node.dataset.key)) node.remove();
  entries.forEach(([key, value], index) => {
    const node = retained.get(key) || create(key);
    node.dataset.key = key;
    update(node, value, key);
    if (root.children[index] !== node) root.insertBefore(node, root.children[index] || null);
  });
}

export function start(renderer) {
  render = renderer;
  const fragment = new URLSearchParams(location.hash.slice(1));
  token = fragment.get("token") || sessionStorage.getItem("hikvision-access") || "";
  if (location.hash) history.replaceState(null, "", location.pathname + location.search);
  if (token) sessionStorage.setItem("hikvision-access", token);
  $("login-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    revision += 1;
    token = $("login-token").value.trim();
    if (!token) return;
    sessionStorage.setItem("hikvision-access", token);
    $("login-token").value = "";
    await refresh();
  });
  $("sign-out").addEventListener("click", signOut);
  refresh();
  setInterval(() => { if (!document.hidden) refresh(); }, 2000);
}
