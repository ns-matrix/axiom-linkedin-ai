const $ = selector => document.querySelector(selector);
const $$ = selector => Array.from(document.querySelectorAll(selector));
const state = {
  bridge: "http://127.0.0.1:8765",
  ollama: "http://127.0.0.1:11434",
  history: [],
  recording: false,
  activeTab: null,
  skills: [],
  lastPlan: null,
  sessionId: null,
  session: null,
  pollTimer: null,
};
const bg = message => new Promise(resolve => chrome.runtime.sendMessage(message, resolve));

function toast(message) {
  const node = $("#toast");
  node.textContent = message;
  node.style.display = "block";
  setTimeout(() => node.style.display = "none", 3200);
}

async function api(path, options = {}) {
  const response = await fetch(state.bridge + path, {
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : `HTTP ${response.status}`);
  return data;
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char]));
}

const PAGE_TITLES = {
  dashboard: "Dashboard",
  chat: "AI Chat",
  run: "Run a task",
  skills: "Skill library",
  tasks: "Tasks",
  recorder: "Train / Record",
  settings: "Settings & MCP",
};

function navigate(page) {
  $$(".page").forEach(node => node.classList.toggle("active", node.id === page));
  $$(".nav").forEach(node => node.classList.toggle("active", node.dataset.page === page));
  $("#page-title").textContent = PAGE_TITLES[page] || page;
  if (page === "tasks") renderTasks();
  if (page === "recorder") renderEvents();
  if (page === "skills") loadSkills();
}
$$(".nav").forEach(button => button.addEventListener("click", () => navigate(button.dataset.page)));

// ------------------------------ connection --------------------------------- //
async function connect() {
  $("#diagnostics").textContent = "Checking local bridge and Ollama…";
  try {
    const data = await api("/health");
    $("#status-pill").textContent = data.ollama_connected ? "Ollama connected" : "Bridge online · Ollama offline";
    $("#status-pill").className = "pill " + (data.ollama_connected ? "online" : "offline");
    $("#model-stat").textContent = data.models?.[0] || "No model";
    $("#diagnostics").textContent = `Bridge: online · Ollama: ${data.ollama_connected ? "online" : "offline"} · Models: ${data.models?.join(", ") || "none"} · Skills: ${data.skills ?? "?"} · MCP: ${data.mcp_url}`;
    $("#mcp-url").textContent = data.mcp_url;
    $("#model-select").innerHTML = '<option value="">Auto-select model</option>' + data.models.map(model => `<option value="${escapeHtml(model)}">${escapeHtml(model)}</option>`).join("");
    loadSkills();
    return data;
  } catch (error) {
    $("#status-pill").textContent = "Disconnected";
    $("#status-pill").className = "pill offline";
    $("#diagnostics").textContent = `Cannot reach ${state.bridge}. Start start-local.bat, then retry. Details: ${error.message}`;
    return null;
  }
}
$("#start-btn").addEventListener("click", connect);
$("#refresh-btn").addEventListener("click", connect);

// --------------------------------- chat ------------------------------------ //
function addBubble(role, text) {
  const node = document.createElement("div");
  node.className = "bubble " + role;
  node.textContent = text;
  $("#messages").append(node);
  $("#messages").scrollTop = $("#messages").scrollHeight;
}
$("#chat-form").addEventListener("submit", async event => {
  event.preventDefault();
  const message = $("#chat-input").value.trim();
  if (!message) return;
  $("#chat-input").value = "";
  addBubble("user", message);
  state.history.push({ role: "user", content: message });
  const button = event.submitter;
  button.disabled = true;
  try {
    const result = await api("/chat", { method: "POST", body: JSON.stringify({ message, model: $("#model-select").value || null, history: state.history.slice(-12) }) });
    addBubble("assistant", result.reply);
    state.history.push({ role: "assistant", content: result.reply });
  } catch (error) {
    addBubble("assistant", "Connection error: " + error.message);
  } finally {
    button.disabled = false;
  }
});

// ------------------------------ legacy tasks ------------------------------- //
async function getTasks() { return new Promise(resolve => chrome.storage.local.get({ axiomTasks: [] }, data => resolve(data.axiomTasks))); }
async function saveTasks(tasks) { return new Promise(resolve => chrome.storage.local.set({ axiomTasks: tasks }, resolve)); }
async function renderTasks() {
  const tasks = await getTasks();
  $("#task-count").textContent = tasks.length;
  const list = $("#task-list");
  list.innerHTML = "";
  if (!tasks.length) { list.innerHTML = '<p class="muted">No saved tasks yet. Create a task goal or record a demonstration.</p>'; return; }
  tasks.forEach((task, index) => {
    const node = document.createElement("div");
    node.className = "task-item";
    node.innerHTML = `<strong>${escapeHtml(task.name)}</strong><p>${escapeHtml(task.goal)}</p><small>${(task.steps || []).length} recorded events · ${escapeHtml(task.created || "")}</small><div class="actions"><button class="ghost" data-plan="${index}">Generate plan</button><button class="ghost" data-delete="${index}">Delete</button></div><div class="plan-output" id="plan-${index}"></div>`;
    list.append(node);
  });
  list.querySelectorAll("[data-delete]").forEach(button => button.addEventListener("click", async () => {
    const all = await getTasks();
    all.splice(Number(button.dataset.delete), 1);
    await saveTasks(all);
    renderTasks();
  }));
  list.querySelectorAll("[data-plan]").forEach(button => button.addEventListener("click", async () => {
    const task = (await getTasks())[Number(button.dataset.plan)];
    const output = $("#plan-" + button.dataset.plan);
    output.textContent = "Generating review-only plan…";
    try {
      const plan = await api("/plan", { method: "POST", body: JSON.stringify({ goal: task.goal }) });
      output.textContent = plan.reply || JSON.stringify(plan.plan, null, 2);
    } catch (error) { output.textContent = error.message; }
  }));
}
$("#new-task").addEventListener("click", () => $("#task-form").classList.remove("hidden"));
$("#cancel-task").addEventListener("click", () => $("#task-form").classList.add("hidden"));
$("#task-form").addEventListener("submit", async event => {
  event.preventDefault();
  const tasks = await getTasks();
  tasks.push({ name: $("#task-name").value.trim(), goal: $("#task-goal").value.trim(), steps: [], created: new Date().toLocaleString() });
  await saveTasks(tasks);
  event.target.reset();
  event.target.classList.add("hidden");
  renderTasks();
  toast("Task saved");
});

// ------------------------------- recorder ---------------------------------- //
async function currentTab() { const tabs = await chrome.tabs.query({ active: true, currentWindow: true }); return tabs[0]; }

async function setRecording(enabled) {
  const tab = await currentTab();
  if (!tab?.id || !tab.url || !/^https?:\/\//i.test(tab.url)) { toast("Open a normal HTTP/HTTPS website tab first."); return; }
  const url = new URL(tab.url);
  const originPattern = `${url.origin}/*`;
  try {
    if (enabled) {
      const alreadyAllowed = await chrome.permissions.contains({ origins: [originPattern] });
      if (!alreadyAllowed) {
        const granted = await chrome.permissions.request({ origins: [originPattern] });
        if (!granted) {
          $("#record-status").textContent = `Permission declined for ${url.origin}. No recording started.`;
          toast("Site permission was not granted.");
          return;
        }
      }
      const name = $("#rec-name").value.trim() || `Recording ${new Date().toLocaleString()}`;
      const recordText = $("#rec-text-consent").checked;
      const started = await bg({ type: "AXIOM_REC_START", name, origin: url.origin, tabId: tab.id, recordText });
      if (!started?.ok) throw new Error(started?.error || "recorder start failed");
      await chrome.scripting.executeScript({ target: { tabId: tab.id }, files: ["content.js"] });
      await chrome.tabs.sendMessage(tab.id, { type: "AXIOM_SET_RECORDING", enabled: true, sessionId: started.recordingId, recordText });
      state.recording = true;
      state.activeTab = tab.id;
      $("#record-status").textContent = recordText
        ? `Recording on ${url.origin} WITH typed-text capture (your explicit choice). Review before saving.`
        : `Recording on ${url.origin}. Metadata only: field names and lengths, not typed values. Passwords are never recorded.`;
    } else {
      await chrome.tabs.sendMessage(tab.id, { type: "AXIOM_SET_RECORDING", enabled: false }).catch(() => {});
      const stopped = await bg({ type: "AXIOM_REC_STOP" });
      state.recording = false;
      $("#record-status").textContent = `Recording stopped. ${stopped?.events || 0} event(s) captured.`;
    }
    $("#record-start").disabled = enabled;
    $("#record-stop").disabled = !enabled;
    $("#record-stat").textContent = enabled ? "Recording" : "Stopped";
    toast(enabled ? "Recording started" : "Recording stopped");
    renderEvents();
  } catch (error) {
    const message = error?.message || String(error);
    $("#record-status").textContent = `Could not ${enabled ? "start" : "stop"} recording: ${message}. Check Chrome site permission and reload this tab.`;
    toast("Recorder failed: " + message);
  }
}
$("#record-start").addEventListener("click", () => setRecording(true));
$("#record-stop").addEventListener("click", () => setRecording(false));
$("#record-refresh").addEventListener("click", renderEvents);

async function latestRecording() {
  const { recordings } = await bg({ type: "AXIOM_REC_LIST" });
  return recordings?.[0] || null;
}

async function renderEvents() {
  const list = $("#record-events");
  list.innerHTML = "";
  const rec = await latestRecording();
  if (!rec || !rec.events?.length) {
    list.innerHTML = '<p class="muted">No recorded events yet.</p>';
    return;
  }
  const label = document.createElement("p");
  label.className = "muted";
  label.textContent = `${rec.name} · ${rec.origin || ""} · ${rec.events.length} events`;
  list.append(label);
  rec.events.slice(-80).reverse().forEach(item => {
    const node = document.createElement("div");
    node.className = "task-item";
    const target = item.element || {};
    const locators = (target.locators || []).slice(0, 2).map(l => `${l.strategy}${l.alt_value ? "=" + l.alt_value : "=" + l.value}`).join(" · ");
    node.innerHTML = `<strong>${escapeHtml(item.action_type)} · ${escapeHtml(target.tag || "page")}${target.name ? " · " + escapeHtml(target.name) : ""}</strong>
      <p>${escapeHtml(locators || "(no locators)")}</p>
      <small>${escapeHtml(item.url || "")}${item.value_length != null ? ` · typed ${item.value_length} chars${item.value ? " (text saved)" : ""}` : ""}${item.consent_text_recorded ? " · consented text" : ""}</small>`;
    list.append(node);
  });
}

$("#record-save-skill").addEventListener("click", async () => {
  const rec = await latestRecording();
  if (!rec || !rec.events?.length) { toast("Nothing to save yet — record some steps first."); return; }
  const name = $("#rec-name").value.trim() || rec.name;
  try {
    const stored = await api("/recordings", { method: "POST", body: JSON.stringify({ name, origin: rec.origin || "", events: rec.events }) });
    const skill = await api("/skills", { method: "POST", body: JSON.stringify({ name, recording_id: stored.recording_id, description: `Learned from recording on ${rec.origin || "site"}` }) });
    await bg({ type: "AXIOM_REC_DELETE", recordingId: rec.recording_id });
    toast(`Saved skill "${skill.name}" v${skill.version}`);
    loadSkills();
    navigate("skills");
  } catch (error) {
    toast("Save failed: " + error.message);
  }
});

// -------------------------------- skills ----------------------------------- //
async function loadSkills() {
  try {
    const data = await api("/skills");
    state.skills = data.skills || [];
    $("#skill-count").textContent = state.skills.length;
    fillSkillSelect();
    renderSkillList();
  } catch (error) {
    $("#skill-list").innerHTML = `<p class="muted">Bridge unreachable: ${escapeHtml(error.message)}</p>`;
  }
}
$("#skills-refresh").addEventListener("click", loadSkills);

function fillSkillSelect() {
  const select = $("#run-skill");
  const current = select.value;
  select.innerHTML = '<option value="">Auto-select from request</option>' +
    state.skills.map(s => `<option value="${escapeHtml(s.skill_id)}">${escapeHtml(s.name)} v${s.version}</option>`).join("");
  if (current) select.value = current;
}

function renderSkillList() {
  const list = $("#skill-list");
  list.innerHTML = "";
  if (!state.skills.length) {
    list.innerHTML = '<p class="muted">No skills yet. Record a workflow in <b>Train / Record</b>, then click "Save recording as skill".</p>';
    return;
  }
  state.skills.forEach(skill => {
    const node = document.createElement("div");
    node.className = "task-item";
    node.innerHTML = `<strong>${escapeHtml(skill.name)} <span class="badge">v${skill.version}</span> <span class="badge risk-${escapeHtml(skill.risk_level)}">${escapeHtml(skill.risk_level)} risk</span></strong>
      <p>${escapeHtml(skill.description || "")}</p>
      <small>${(skill.steps || []).length} steps · inputs: ${escapeHtml((skill.input_schema || []).map(i => i.name).join(", ") || "none")}</small>
      <div class="actions">
        <button class="ghost" data-act="view">View</button>
        <button class="ghost" data-act="dry">Dry run</button>
        <button class="primary" data-act="run">Run</button>
        <button class="ghost" data-act="rename">Rename</button>
        <button class="ghost" data-act="history">History</button>
        <button class="ghost" data-act="delete">Delete</button>
      </div>`;
    node.querySelectorAll("[data-act]").forEach(btn => btn.addEventListener("click", () => skillAction(btn.dataset.act, skill)));
    list.append(node);
  });
}

function renderSkillDetailHtml(skill) {
  const steps = (skill.steps || []).map((s, i) =>
    `<div class="logline"><b>${i + 1}.</b> [${escapeHtml(s.action)}] ${escapeHtml(s.title)} ${s.requires_approval ? '<span class="badge risk-high">approval</span>' : ""}<br><small>verify: ${escapeHtml(s.verification?.method || "none")} · risk: ${escapeHtml(s.risk_level)} · timeout ${s.timeout}s</small></div>`).join("");
  const inputs = (skill.input_schema || []).map(i =>
    `<div class="logline"><code>${escapeHtml(i.name)}</code> (${escapeHtml(i.type)}${i.required ? ", required" : ""}) ${escapeHtml(i.description || "")}${i.example ? ` · e.g. ${escapeHtml(String(i.example).slice(0, 60))}` : ""}</div>`).join("") || "<p class='muted'>No inputs.</p>";
  const warnings = (skill.warnings || []).map(w => `<p class="hint">${escapeHtml(w)}</p>`).join("");
  return `<div class="panel-sub"><h3>${escapeHtml(skill.name)} <span class="badge">v${skill.version}</span></h3>
    <p class="muted">${escapeHtml(skill.summary || skill.description || "")}</p>
    <b>Inputs</b><div class="trace">${inputs}</div>
    <b>Steps</b><div class="trace">${steps}</div>
    ${warnings ? `<b>Warnings</b>${warnings}` : ""}
    <p class="hint">Source recording: ${escapeHtml(skill.source_recording_id || "n/a")} · created ${escapeHtml(skill.created_at)}</p></div>`;
}

function renderTrace(result) {
  const rows = (result.trace || []).map(t =>
    `<div class="logline"><b>${t.would_execute ? "✓" : "✗"}</b> [${escapeHtml(t.action)}] ${escapeHtml(t.title)} ${t.requires_approval ? '<span class="badge risk-high">approval</span>' : ""} <small>verify: ${escapeHtml(t.verify)}</small></div>`).join("");
  const missing = (result.missing_inputs || []).length
    ? `<p class="hint">Missing inputs: ${escapeHtml(result.missing_inputs.join(", "))} — use Run and provide them at plan time.</p>` : "";
  return `<div class="panel-sub"><b>Dry run (no browser touched)</b>${missing}<div class="trace">${rows}</div></div>`;
}

async function skillAction(act, skill) {
  const detail = $("#skill-detail");
  detail.classList.remove("hidden");
  if (act === "view") {
    detail.innerHTML = renderSkillDetailHtml(skill);
  } else if (act === "dry") {
    detail.innerHTML = "<p class='muted'>Running dry-run…</p>";
    try {
      const result = await api(`/skills/${skill.skill_id}/dry-run`, { method: "POST", body: JSON.stringify({ inputs: {}, goal: "Dry run" }) });
      detail.innerHTML = renderSkillDetailHtml(skill) + renderTrace(result);
    } catch (error) {
      detail.innerHTML = `<p class="hint">Dry run failed: ${escapeHtml(error.message)}</p>`;
    }
  } else if (act === "run") {
    fillSkillSelect();
    $("#run-skill").value = skill.skill_id;
    navigate("run");
    $("#run-goal").focus();
    toast(`Skill "${skill.name}" selected — describe the task.`);
  } else if (act === "rename") {
    const name = prompt("Rename skill", skill.name);
    if (!name || name.trim() === skill.name) return;
    try {
      await api(`/skills/${skill.skill_id}`, { method: "PATCH", body: JSON.stringify({ name: name.trim() }) });
      loadSkills();
      toast("Renamed (new version saved)");
    } catch (error) { toast(error.message); }
  } else if (act === "delete") {
    if (!confirm(`Delete skill "${skill.name}"? Execution history is kept.`)) return;
    try {
      await api(`/skills/${skill.skill_id}`, { method: "DELETE" });
      detail.classList.add("hidden");
      loadSkills();
      toast("Skill deleted");
    } catch (error) { toast(error.message); }
  } else if (act === "history") {
    try {
      const data = await api("/sessions?limit=50");
      const rows = (data.history || []).filter(h => h.skill_id === skill.skill_id);
      const html = rows.length
        ? rows.map(r => `<div class="logline"><span class="badge ${r.state === "completed" ? "risk-low" : "risk-medium"}">${escapeHtml(r.state)}</span> ${escapeHtml(r.goal)}<br><small>${escapeHtml(r.created_at)} · ${escapeHtml(r.summary || "")}</small></div>`).join("")
        : "<p class='muted'>No runs yet for this skill.</p>";
      detail.innerHTML = renderSkillDetailHtml(skill) + `<div class="panel-sub"><b>Execution history</b><div class="trace">${html}</div></div>`;
    } catch (error) { toast(error.message); }
  }
}

// ---------------------------------- run ------------------------------------ //
$("#run-plan").addEventListener("click", () => planTask());

async function planTask(extraInputs = null) {
  const goal = $("#run-goal").value.trim();
  if (!goal) { toast("Describe the task first."); return; }
  const card = $("#plan-card");
  card.classList.remove("hidden");
  $("#run-live").classList.add("hidden");
  card.innerHTML = "<p class='muted'>Planning locally — selecting skill and drafting content with Ollama…</p>";
  try {
    const body = { goal, skill_id: $("#run-skill").value || null, inputs: extraInputs || {} };
    const result = await api("/tasks/plan", { method: "POST", body: JSON.stringify(body) });
    state.lastPlan = result.plan;
    renderPlan(result);
  } catch (error) {
    card.innerHTML = `<p class="hint">Planning failed: ${escapeHtml(error.message)}</p>`;
  }
}

function renderPlan(result) {
  const plan = result.plan;
  const card = $("#plan-card");
  if (!plan || result.status === "no_skill") {
    card.innerHTML = `<p class="hint">${escapeHtml(result.message || "No skill matched this request.")}</p>
      <p class="muted">Record the workflow in <b>Train / Record</b> and save it as a skill, then try again.</p>`;
    return;
  }
  const missing = plan.missing_inputs || [];
  const inputsHtml = missing.length
    ? `<div class="approval"><b>Missing inputs</b><p class="muted">The skill needs these values:</p>` +
      missing.map(name => `<label>${escapeHtml(name)}<input data-input="${escapeHtml(name)}" placeholder="${escapeHtml(name)}"></label>`).join("") +
      `<button class="primary" id="plan-retry">Re-plan with these inputs</button></div>`
    : "";
  const draftHtml = plan.draft
    ? `<label>Draft — edit freely; nothing runs until you approve<textarea id="plan-draft" rows="6">${escapeHtml(plan.draft)}</textarea></label>`
    : "";
  const stepsHtml = `<div class="trace">${(plan.steps || []).map((s, i) =>
    `<div class="logline"><b>${i + 1}.</b> [${escapeHtml(s.action)}] ${escapeHtml(s.title || s.step_id)}
      ${s.requires_approval ? '<span class="badge risk-high">approval required</span>' : ""}
      <span class="badge risk-${escapeHtml(s.risk_level)}">${escapeHtml(s.risk_level)}</span>
      <small>verify: ${escapeHtml(s.verification_method)}</small></div>`).join("")}</div>`;
  const warnings = (plan.warnings || []).map(w => `<p class="hint">${escapeHtml(w)}</p>`).join("");
  const ready = result.status === "ready";
  card.innerHTML = `<div class="panel-sub">
    <p><b>Skill:</b> ${escapeHtml(plan.skill_name || "-")} v${plan.skill_version || 1}
      · <span class="badge">${escapeHtml(plan.source)}</span>
      · <span class="badge ${ready ? "risk-low" : "risk-high"}">${escapeHtml(result.status)}</span></p>
    ${result.message ? `<p class="hint">${escapeHtml(result.message)}</p>` : ""}
    ${warnings}${inputsHtml}${draftHtml}
    <b>Proposed plan</b>${stepsHtml}
    <div class="actions">
      ${ready ? '<button class="primary" id="plan-approve">Approve &amp; start supervised run</button>' : ""}
      <button class="ghost" id="plan-discard">Discard</button>
    </div></div>`;

  $("#plan-retry")?.addEventListener("click", () => {
    const inputs = {};
    card.querySelectorAll("[data-input]").forEach(el => { if (el.value.trim()) inputs[el.dataset.input] = el.value.trim(); });
    planTask(inputs);
  });
  $("#plan-discard")?.addEventListener("click", () => {
    card.classList.add("hidden");
    state.lastPlan = null;
  });
  $("#plan-approve")?.addEventListener("click", approveAndStart);
}

async function approveAndStart() {
  const plan = state.lastPlan;
  if (!plan) return;
  const draftEl = $("#plan-draft");
  if (draftEl) {
    const oldDraft = plan.draft || "";
    const edited = draftEl.value;
    if (edited !== oldDraft) {
      for (const [key, value] of Object.entries(plan.inputs || {})) {
        if (typeof value === "string" && value === oldDraft) plan.inputs[key] = edited;
      }
      plan.draft = edited;
    }
  }
  const tab = await currentTab();
  if (!tab?.id) { toast("Open the target website tab first."); return; }
  const navStep = (plan.steps || []).find(s => s.action === "navigate");
  const targetUrl = navStep?.parameters?.url || tab.url;
  try {
    const origin = new URL(targetUrl).origin;
    const pattern = `${origin}/*`;
    const already = await chrome.permissions.contains({ origins: [pattern] });
    if (!already) {
      const granted = await chrome.permissions.request({ origins: [pattern] });
      if (!granted) { toast("Site permission is required to execute this plan."); return; }
    }
  } catch { /* tab may be a special page; execution uses the session tab */ }
  try {
    const session = await api("/sessions", { method: "POST", body: JSON.stringify({ plan, tab_id: tab.id }) });
    state.sessionId = session.session_id;
    await api(`/sessions/${session.session_id}/approve`, { method: "POST" });
    await bg({ type: "AXIOM_SESSION_START", sessionId: session.session_id, tabId: tab.id });
    $("#plan-card").classList.add("hidden");
    $("#run-live").classList.remove("hidden");
    startPolling();
    toast("Plan approved. Gated actions will still ask right before they run.");
  } catch (error) {
    toast("Could not start: " + error.message);
  }
}

function startPolling() {
  if (state.pollTimer) clearInterval(state.pollTimer);
  state.pollTimer = setInterval(refreshSession, 1500);
  refreshSession();
}

const TERMINAL_STATES = ["completed", "partially_completed", "failed", "cancelled", "verification_inconclusive"];

async function refreshSession() {
  if (!state.sessionId) return;
  let session;
  try {
    session = await api(`/sessions/${state.sessionId}`);
  } catch { return; }
  state.session = session;
  renderRunLive(session);
  if (TERMINAL_STATES.includes(session.state)) {
    clearInterval(state.pollTimer);
    state.pollTimer = null;
  }
}

function statusBadge(st) {
  const map = {
    succeeded: "risk-low", failed: "risk-high", verification_inconclusive: "risk-medium",
    skipped: "risk-medium", pending: "", running: "risk-medium", awaiting_approval: "risk-high",
  };
  return `<span class="badge ${map[st.status] || ""}">${escapeHtml(st.status)}${st.attempts > 1 ? ` · try ${st.attempts}` : ""}</span>`;
}

function approvalCard(a) {
  return `<div class="approval"><b>Approval required: ${escapeHtml(a.title || a.step_id)}</b>
    <p class="muted">Action <code>${escapeHtml(a.action)}</code> · risk ${escapeHtml(a.risk_level || "high")}<br>${escapeHtml(a.expected_outcome || "")}</p>
    ${a.target ? `<p class="hint">Target: ${escapeHtml(a.target)}</p>` : ""}
    ${a.content ? `<label>Content that will be submitted<textarea id="appr-content" rows="6" readonly>${escapeHtml(a.content)}</textarea></label>` : `<p class="hint">No text content attached to this action.</p>`}
    <div class="actions"><button class="primary" id="appr-approve">Approve this action</button><button class="ghost" id="appr-reject">Reject &amp; stop</button></div></div>`;
}

function renderRunLive(session) {
  const node = $("#run-live");
  const terminal = TERMINAL_STATES.includes(session.state);
  const current = session.steps[session.current_index];
  const approval = session.pending_approval?.type === "step" ? session.pending_approval : null;
  node.innerHTML = `<div class="panel-sub">
    <p><span class="pill ${session.state === "completed" ? "online" : "offline"}">${escapeHtml(session.state)}</span>
      &nbsp; step ${Math.min(session.current_index + 1, session.steps.length)} / ${session.steps.length}
      ${current && !terminal ? ` · current: <b>${escapeHtml(current.step.title || current.step.step_id)}</b>` : ""}</p>
    <div class="trace">${session.steps.map((st, i) =>
      `<div class="logline"><b>${i + 1}.</b> ${escapeHtml(st.step.title || st.step.step_id)} — ${statusBadge(st)}${st.error ? ` <small>${escapeHtml(st.error)}</small>` : ""}</div>`).join("")}</div>
    ${approval ? approvalCard(approval) : ""}
    ${!terminal ? `<div class="actions"><button class="ghost" id="run-pause">${session.state === "paused" ? "Resume" : "Pause"}</button><button class="ghost" id="run-cancel">Cancel run</button></div>` : ""}
    ${terminal ? `<div class="approval"><b>Final result: ${escapeHtml(session.state)}</b><p>${escapeHtml(session.summary || "")}</p>
      <p class="hint">Check the browser tab to confirm the outcome; ambiguous verifications are reported, never hidden.</p>
      <button class="primary" id="run-done">Done</button></div>` : ""}
    <details><summary>Execution log (${(session.logs || []).length})</summary><div class="trace">${(session.logs || []).slice(-15).map(l =>
      `<div class="logline"><small>${escapeHtml(l.ts)}</small> ${escapeHtml(l.message)}</div>`).join("")}</div></details>
  </div>`;

  $("#appr-approve")?.addEventListener("click", () => decideStep(approval.step_id, "approve"));
  $("#appr-reject")?.addEventListener("click", () => decideStep(approval.step_id, "reject"));
  $("#run-pause")?.addEventListener("click", async () => {
    const action = session.state === "paused" ? "resume" : "pause";
    try {
      await api(`/sessions/${session.session_id}/${action}`, { method: "POST" });
      await bg({ type: "AXIOM_SESSION_CONTROL", sessionId: session.session_id, control: action });
      refreshSession();
    } catch (error) { toast(error.message); }
  });
  $("#run-cancel")?.addEventListener("click", async () => {
    if (!confirm("Cancel this supervised run?")) return;
    try {
      await api(`/sessions/${session.session_id}/cancel`, { method: "POST" });
      await bg({ type: "AXIOM_SESSION_CONTROL", sessionId: session.session_id, control: "cancel" });
      refreshSession();
    } catch (error) { toast(error.message); }
  });
  $("#run-done")?.addEventListener("click", () => {
    $("#run-live").classList.add("hidden");
    state.sessionId = null;
  });
}

async function decideStep(stepId, decision) {
  try {
    await api(`/sessions/${state.sessionId}/approvals/${stepId}`, { method: "POST", body: JSON.stringify({decision}) });
    if (decision === "reject") await bg({ type: "AXIOM_SESSION_CONTROL", sessionId: state.sessionId, control: "stop" });
    refreshSession();
  } catch (error) { toast(error.message); }
}

// ------------------------------- settings ---------------------------------- //
$("#save-settings").addEventListener("click", () => {
  state.bridge = $("#bridge-url").value.replace(/\/$/, "");
  state.ollama = $("#ollama-url").value.replace(/\/$/, "");
  chrome.storage.local.set({ axiomSettings: { bridge: state.bridge, ollama: state.ollama } }, () => toast("Settings saved"));
  connect();
});
$("#copy-mcp").addEventListener("click", () => navigator.clipboard.writeText($("#mcp-url").textContent).then(() => toast("MCP URL copied")).catch(() => toast("Copy failed")));
$("#copy-demo").addEventListener("click", () => navigator.clipboard.writeText($("#demo-url").textContent).then(() => toast("Demo URL copied")).catch(() => toast("Copy failed")));

// -------------------------------- start ------------------------------------ //
chrome.storage.local.get({ axiomSettings: null }, data => {
  if (data.axiomSettings) {
    state.bridge = data.axiomSettings.bridge || state.bridge;
    state.ollama = data.axiomSettings.ollama || state.ollama;
    $("#bridge-url").value = state.bridge;
    $("#ollama-url").value = state.ollama;
  }
});
renderTasks();
connect();
