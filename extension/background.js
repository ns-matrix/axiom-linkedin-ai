chrome.runtime.onInstalled.addListener(() => {
  chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true }).catch(() => {});
});

const BRIDGE_FALLBACK = "http://127.0.0.1:8765";
const TICK_MS = 1200;
const TICK_MS_WAITING = 2500;
const ALARM_NAME = "axiom-session-tick";

const storageGet = keys => new Promise(resolve => chrome.storage.local.get(keys, resolve));
const storageSet = items => new Promise(resolve => chrome.storage.local.set(items, resolve));

function stripUrl(url) {
  try {
    const parsed = new URL(url);
    return `${parsed.origin}${parsed.pathname}`;
  } catch {
    return "";
  }
}

async function bridgeBase() {
  const data = await storageGet({ axiomSettings: null });
  return (data.axiomSettings?.bridge || BRIDGE_FALLBACK).replace(/\/$/, "");
}

async function bridgeCall(path, options = {}) {
  const base = await bridgeBase();
  const response = await fetch(base + path, {
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = typeof body.detail === "string" ? body.detail : `HTTP ${response.status}`;
    const error = new Error(detail);
    error.status = response.status;
    throw error;
  }
  return body;
}

// ------------------------------- recording -------------------------------- //

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message?.type === "AXIOM_REC_START") {
    handleRecStart(message, sender, sendResponse);
    return true;
  }
  if (message?.type === "AXIOM_RECORD_EVENT") {
    handleRecordEvent(message, sender, sendResponse);
    return true;
  }
  if (message?.type === "AXIOM_REC_STOP") {
    handleRecStop(sendResponse);
    return true;
  }
  if (message?.type === "AXIOM_REC_LIST") {
    storageGet({ axiomRecordings: [] }).then(({ axiomRecordings }) => sendResponse({ recordings: axiomRecordings }));
    return true;
  }
  if (message?.type === "AXIOM_REC_DELETE") {
    handleRecDelete(message.recordingId, sendResponse);
    return true;
  }
  if (message?.type === "AXIOM_SESSION_START") {
    handleSessionStart(message, sendResponse);
    return true;
  }
  if (message?.type === "AXIOM_SESSION_CONTROL") {
    handleSessionControl(message, sendResponse);
    return true;
  }
  if (message?.type === "AXIOM_EXEC_STATUS") {
    storageGet({ axiomActiveSession: null, axiomExecLog: [] })
      .then(data => sendResponse({ active: data.axiomActiveSession, log: data.axiomExecLog || [] }));
    return true;
  }
  return false;
});

async function handleRecStart(message, sender, sendResponse) {
  const tabId = message.tabId ?? sender.tab?.id;
  const recordingId = message.recordingId || `rec_${Date.now().toString(36)}${Math.random().toString(36).slice(2, 8)}`;
  const active = {
    recordingId,
    name: message.name || `Recording ${new Date().toLocaleString()}`,
    origin: message.origin || "",
    tabId,
    startedAt: new Date().toISOString(),
    recordText: Boolean(message.recordText),
  };
  const { axiomRecordings = [] } = await storageGet({ axiomRecordings: [] });
  axiomRecordings.unshift({
    recording_id: recordingId,
    name: active.name,
    origin: active.origin,
    created_at: active.startedAt,
    status: "open",
    events: [],
  });
  await storageSet({
    axiomRecording: active,
    axiomRecordings: axiomRecordings.slice(0, 50),
  });
  sendResponse({ ok: true, recordingId });
}

async function handleRecordEvent(message, sender, sendResponse) {
  const { axiomRecording, axiomRecordings = [] } = await storageGet({ axiomRecording: null, axiomRecordings: [] });
  if (!axiomRecording) return sendResponse({ ok: false, error: "not_recording" });
  if (message.sessionId && axiomRecording.recordingId !== message.sessionId) {
    return sendResponse({ ok: false, error: "session_mismatch" });
  }
  const target = axiomRecordings.find(r => r.recording_id === axiomRecording.recordingId);
  if (!target) return sendResponse({ ok: false, error: "recording_not_found" });

  const event = {
    ...message.event,
    session_id: axiomRecording.recordingId,
    origin: axiomRecording.origin || message.event?.origin || "",
    url: stripUrl(message.event?.url || ""),
  };
  if (!event.origin && event.url) {
    try { event.origin = new URL(event.url).origin; } catch { /* keep empty */ }
  }
  target.events.push(event);
  if (target.events.length > 1000) target.events = target.events.slice(-1000);
  target.status = "open";
  await storageSet({ axiomRecordings });
  sendResponse({ ok: true, count: target.events.length });
}

async function handleRecStop(sendResponse) {
  const { axiomRecording, axiomRecordings = [] } = await storageGet({ axiomRecording: null, axiomRecordings: [] });
  if (!axiomRecording) return sendResponse({ ok: false });
  const target = axiomRecordings.find(r => r.recording_id === axiomRecording.recordingId);
  if (target) target.status = "stopped";
  await storageSet({ axiomRecordings, axiomRecording: null });
  sendResponse({ ok: true, recordingId: axiomRecording.recordingId, events: target?.events?.length || 0 });
}

async function handleRecDelete(recordingId, sendResponse) {
  const { axiomRecordings = [] } = await storageGet({ axiomRecordings: [] });
  const next = axiomRecordings.filter(r => r.recording_id !== recordingId);
  await storageSet({ axiomRecordings: next });
  sendResponse({ ok: true, remaining: next.length });
}

// ------------------------------ execution --------------------------------- //

let loopTimer = null;
let looping = false;

async function execLog(entry) {
  const { axiomExecLog = [] } = await storageGet({ axiomExecLog: [] });
  axiomExecLog.push({ ts: new Date().toISOString(), ...entry });
  await storageSet({ axiomExecLog: axiomExecLog.slice(-200) });
}

async function handleSessionStart(message, sendResponse) {
  const ctx = {
    sessionId: message.sessionId,
    tabId: message.tabId ?? null,
    startedAt: new Date().toISOString(),
  };
  await storageSet({ axiomActiveSession: ctx, axiomExecLog: [] });
  try { chrome.alarms.create(ALARM_NAME, { periodInMinutes: 1 }); } catch { /* best effort */ }
  await execLog({ level: "info", message: `Executor attached to session ${ctx.sessionId}` });
  startLoop();
  sendResponse({ ok: true, ctx });
}

async function handleSessionControl(message, sendResponse) {
  const { axiomActiveSession } = await storageGet({ axiomActiveSession: null });
  const sessionId = message.sessionId || axiomActiveSession?.sessionId;
  if (!sessionId) return sendResponse({ ok: false, error: "no active session" });
  try {
    if (message.control === "stop") {
      stopLoop();
      await storageSet({ axiomActiveSession: null });
      await execLog({ level: "warn", message: "Executor stopped locally" });
      return sendResponse({ ok: true });
    }
    const map = { pause: "pause", resume: "resume", cancel: "cancel" };
    const suffix = map[message.control];
    if (!suffix) return sendResponse({ ok: false, error: "unknown control" });
    const session = await bridgeCall(`/sessions/${sessionId}/${suffix}`, { method: "POST" });
    await execLog({ level: "info", message: `Session ${message.control} -> ${session.state}` });
    if (message.control === "cancel") {
      stopLoop();
      await storageSet({ axiomActiveSession: null });
    }
    sendResponse({ ok: true, state: session.state });
  } catch (error) {
    sendResponse({ ok: false, error: String(error.message || error) });
  }
}

function startLoop() {
  if (looping) return;
  looping = true;
  schedule(0);
}

function stopLoop() {
  looping = false;
  if (loopTimer) clearTimeout(loopTimer);
  loopTimer = null;
  try { chrome.alarms.clear(ALARM_NAME); } catch { /* ignore */ }
}

function schedule(delay) {
  if (!looping) return;
  loopTimer = setTimeout(tick, delay);
}

async function tick() {
  if (!looping) return;
  const { axiomActiveSession } = await storageGet({ axiomActiveSession: null });
  if (!axiomActiveSession) {
    stopLoop();
    return;
  }
  let delay = TICK_MS;
  try {
    const claim = await bridgeCall(`/sessions/${axiomActiveSession.sessionId}/claim`, { method: "POST" });
    await storageSet({ axiomSessionMirror: { ...claim, mirroredAt: Date.now() } });
    if (claim.status === "claimed") {
      delay = TICK_MS;
      await executeStep(axiomActiveSession, claim);
    } else if (claim.status === "terminal") {
      await execLog({ level: "info", message: `Session finished: ${claim.state} — ${claim.summary || ""}` });
      stopLoop();
      await storageSet({ axiomActiveSession: null });
      return;
    } else if (claim.reason && String(claim.reason).includes("approval")) {
      delay = TICK_MS_WAITING;
    }
  } catch (error) {
    await execLog({ level: "error", message: `Executor error: ${error.message || error}` });
    delay = TICK_MS_WAITING;
  }
  schedule(delay);
}

async function ensureContentScript(tabId) {
  try {
    const pong = await chrome.tabs.sendMessage(tabId, { type: "AXIOM_PING" });
    if (pong?.ok) return true;
  } catch { /* not injected yet */ }
  await chrome.scripting.executeScript({ target: { tabId }, files: ["content.js"] });
  return true;
}

function withTimeout(promise, ms, label) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error(`${label} timed out after ${ms}ms`)), ms);
    promise.then(
      value => { clearTimeout(timer); resolve(value); },
      error => { clearTimeout(timer); reject(error); },
    );
  });
}

async function tabUrl(tabId) {
  try {
    const tab = await chrome.tabs.get(tabId);
    return stripUrl(tab.url || "");
  } catch {
    return "";
  }
}

async function executeStep(ctx, claim) {
  const step = claim.step;
  const token = claim.token;
  const timeoutMs = Math.min(Number(step.timeout || 20) * 1000, 150000);
  let result;

  try {
    if (step.action === "navigate") {
      const url = String(step.parameters?.url || "");
      if (!/^https?:\/\//i.test(url)) throw new Error("navigate url must be http(s)");
      await chrome.tabs.update(ctx.tabId, { url });
      await withTimeout(new Promise(resolve => {
        const listener = (tabId, info) => {
          if (tabId === ctx.tabId && info.status === "complete") {
            chrome.tabs.onUpdated.removeListener(listener);
            resolve();
          }
        };
        chrome.tabs.onUpdated.addListener(listener);
        chrome.tabs.get(ctx.tabId, tab => {
          if (chrome.runtime.lastError || tab.status === "complete") {
            chrome.tabs.onUpdated.removeListener(listener);
            resolve();
          }
        });
      }), timeoutMs, "navigate");
      await sleep(400);
      result = { status: "succeeded", observed: { ok: true, url: await tabUrl(ctx.tabId) } };
    } else {
      await ensureContentScript(ctx.tabId);
      const observed = await withTimeout(
        chrome.tabs.sendMessage(ctx.tabId, { type: "AXIOM_EXECUTE_ACTION", payload: step }),
        timeoutMs,
        step.action,
      );
      if (!observed || typeof observed !== "object") {
        result = { status: "failed", observed: { ok: false }, error: "empty response from content script" };
      } else if (observed.ok) {
        result = { status: "succeeded", observed };
      } else {
        result = { status: "failed", observed, error: observed.error || "action reported failure" };
      }
    }
  } catch (error) {
    result = { status: "failed", observed: { ok: false }, error: String(error?.message || error) };
  }

  await execLog({
    level: result.status === "succeeded" ? "info" : "error",
    stepId: step.step_id,
    message: `[${step.action}] ${step.title || step.step_id}: ${result.status}${result.error ? ` (${result.error})` : ""}`,
  });

  try {
    await bridgeCall(`/sessions/${ctx.sessionId}/steps/${step.step_id}/result`, {
      method: "POST",
      body: JSON.stringify({ token, status: result.status, observed: result.observed, error: result.error || null }),
    });
  } catch (error) {
    await execLog({ level: "error", message: `Failed to report result for ${step.step_id}: ${error.message || error}` });
  }
}

const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

chrome.alarms.onAlarm.addListener(alarm => {
  if (alarm.name === ALARM_NAME) tick();
});
