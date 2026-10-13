(() => {
  if (window.__axiomInstalled) return;
  window.__axiomInstalled = true;

  const state = {
    recording: false,
    sessionId: null,
    seq: 0,
    recordText: false,
  };

  const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
  const normalize = text => (text || "").trim().replace(/\s+/g, " ").toLowerCase();
  const cssEscape = value => (window.CSS && CSS.escape) ? CSS.escape(value) : String(value).replace(/["\\]/g, "\\$&");

  function safeUrl() {
    try {
      const url = new URL(location.href);
      return `${url.origin}${url.pathname}`;
    } catch {
      return location.origin;
    }
  }

  function isVisible(el) {
    if (!el || !el.getClientRects) return false;
    const rects = el.getClientRects();
    if (!rects.length) return false;
    const style = getComputedStyle(el);
    if (style.visibility === "hidden" || style.display === "none" || parseFloat(style.opacity || "1") === 0) return false;
    if (el.closest('[aria-hidden="true"], [hidden]')) return false;
    return true;
  }

  function implicitRoleSelectors(role) {
    const map = {
      link: ['a[href]'],
      button: ['button', '[role="button"]', 'input[type="button"]', 'input[type="submit"]', 'input[type="reset"]'],
      textbox: ['textarea', 'input[type="text"]', 'input[type="search"]', 'input[type="email"]', 'input[type="url"]', 'input:not([type])', '[contenteditable="true"][role="textbox"]', '[contenteditable=""]'],
      combobox: ['select', 'input[list]'],
      checkbox: ['input[type="checkbox"]'],
      radio: ['input[type="radio"]'],
      img: ['img'],
      navigation: ['nav'],
      dialog: ['dialog', '[role="dialog"]'],
      option: ['option'],
      listbox: ['select', '[role="listbox"]'],
      searchbox: ['input[type="search"]', '[role="searchbox"]'],
    };
    return map[role] || [];
  }

  function accessibleName(el) {
    if (!el || !el.getAttribute) return "";
    const aria = el.getAttribute("aria-label");
    if (aria && aria.trim()) return aria.trim();
    const labelledBy = el.getAttribute("aria-labelledby");
    if (labelledBy) {
      for (const id of labelledBy.split(/\s+/)) {
        const ref = id && document.getElementById(id);
        if (ref) {
          const text = (ref.innerText || ref.textContent || "").trim();
          if (text) return text;
        }
      }
    }
    if (el.id) {
      const label = document.querySelector(`label[for="${cssEscape(el.id)}"]`);
      if (label) {
        const text = (label.innerText || label.textContent || "").trim();
        if (text) return text;
      }
    }
    const wrapping = el.closest && el.closest("label");
    if (wrapping && wrapping !== el) {
      const text = (wrapping.innerText || wrapping.textContent || "").trim();
      if (text) return text;
    }
    const tag = el.tagName.toLowerCase();
    if (tag === "button" || (tag === "a" && el.hasAttribute("href")) || el.getAttribute("role") === "button") {
      const label = (el.innerText || el.textContent || "").trim().replace(/\s+/g, " ");
      if (label) return label.slice(0, 80);
    }
    return (el.getAttribute("title") || el.getAttribute("placeholder") || el.getAttribute("name") || "").trim();
  }

  function implicitRole(el) {
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute("type") || "").toLowerCase();
    if (el.getAttribute("role")) return el.getAttribute("role");
    if (tag === "textarea") return "textbox";
    if (tag === "select") return "combobox";
    if (tag === "button") return "button";
    if (tag === "a" && el.hasAttribute("href")) return "link";
    if (tag === "img") return "img";
    if (tag === "nav") return "navigation";
    if (tag === "dialog") return "dialog";
    if (tag === "input") {
      if (type === "checkbox") return "checkbox";
      if (type === "radio") return "radio";
      if (type === "submit" || type === "button" || type === "reset") return "button";
      if (type === "search") return "searchbox";
      if (type === "password") return null;
      return "textbox";
    }
    if (el.isContentEditable) return "textbox";
    return null;
  }

  const GENERATED_ID = /ember\d|react-aria|:r[0-9a-z]{4,}|radix|chakra|headlessui|svelte|aria-uidescendant|\d{5,}/i;

  function stableId(el) {
    const id = el && el.id;
    return id && id.length <= 60 && !GENERATED_ID.test(id) ? id : null;
  }

  function structuralPath(el) {
    const parts = [];
    let node = el;
    for (let depth = 0; node && node !== document.body && depth < 4; depth++) {
      const parent = node.parentElement;
      if (!parent) break;
      const index = Array.prototype.indexOf.call(parent.children, node) + 1;
      parts.unshift(`${node.tagName.toLowerCase()}:nth-of-type(${index})`);
      node = parent;
    }
    return parts.length ? `body > ${parts.join(" > ")}` : null;
  }

  function buildLocators(el) {
    if (!el || el.nodeType !== 1) return [];
    const found = [];
    const add = (strategy, value, alt, priority, brittle = false, note = null) => {
      if (!value) return;
      if (found.some(l => l.strategy === strategy && l.value === value && (l.alt_value || "") === (alt || ""))) return;
      found.push({ strategy, value, alt_value: alt || undefined, priority, brittle, note: note || undefined });
    };

    for (const attr of ["data-testid", "data-test", "data-axiom-id"]) {
      const val = el.getAttribute(attr);
      if (val) add("testid", val, null, 10, false, attr);
    }

    const role = implicitRole(el);
    const name = accessibleName(el);
    if (role && name) add("role", role, name, 20);

    const tag = el.tagName.toLowerCase();
    if (["input", "textarea", "select"].includes(tag) || el.isContentEditable) {
      if (name) add("label", name, null, 30);
      const placeholder = el.getAttribute("placeholder");
      if (placeholder) add("placeholder", placeholder, null, 35);
      const fieldName = el.getAttribute("name");
      if (fieldName) add("name", fieldName, null, 40);
    }

    if (role === "button" || role === "link" || tag === "button" || (tag === "a" && el.hasAttribute("href"))) {
      const text = (el.innerText || el.textContent || "").trim().replace(/\s+/g, " ").slice(0, 80);
      if (text) add("text", text, null, 45);
    }

    const id = stableId(el);
    if (id) add("css", `#${cssEscape(id)}`, null, 50);

    const path = structuralPath(el);
    if (path) add("css", path, null, 70, true, "structural fallback");

    return found.sort((a, b) => a.priority - b.priority).slice(0, 5);
  }

  function targetMetadata(el) {
    if (!el || el.nodeType !== 1) return { tag: "unknown", locators: [] };
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute("type") || "").toLowerCase();
    if (type === "password" || el.closest('input[type="password"]')) {
      return { tag, sensitive: true, locators: [] };
    }
    const attrs = {};
    for (const key of ["placeholder", "aria-label", "title", "name", "type", "role"]) {
      const val = el.getAttribute(key);
      if (val) attrs[key] = val.slice(0, 120);
    }
    const id = stableId(el);
    if (id) attrs.id = id;
    return {
      tag,
      role: implicitRole(el) || undefined,
      name: accessibleName(el).slice(0, 120) || undefined,
      attrs,
      locators: buildLocators(el),
      sensitive: false,
    };
  }

  function proposedVariable(el) {
    const meta = targetMetadata(el);
    const source = meta.attrs.placeholder || meta.attrs["aria-label"] || meta.attrs.name || meta.name || meta.attrs.id || "";
    const slug = normalize(source).replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "").slice(0, 40);
    return slug || "text_value";
  }

  // ---------------------------- recorder ---------------------------------- //
  function emit(actionType, el, extra = {}) {
    if (!state.recording) return;
    const target = el ? targetMetadata(el) : null;
    if (target && target.sensitive) return;
    state.seq += 1;
    chrome.runtime.sendMessage({
      type: "AXIOM_RECORD_EVENT",
      sessionId: state.sessionId,
      event: {
        seq: state.seq,
        timestamp: Date.now() / 1000,
        action_type: actionType,
        url: safeUrl(),
        origin: location.origin,
        element: target || undefined,
        ...extra,
      },
    });
  }

  let lastScrollAt = 0;
  document.addEventListener("click", event => {
    if (!state.recording) return;
    const el = event.target instanceof Element ? event.target : null;
    emit("click", el);
  }, true);

  document.addEventListener("input", event => {
    if (!state.recording) return;
    const el = event.target;
    if (!(el instanceof Element)) return;
    const type = (el.getAttribute("type") || "").toLowerCase();
    if (type === "password") return;
    const value = el.value !== undefined ? String(el.value) : (el.textContent || "");
    const extra = {
      value_length: value.length,
      valueRecorded: false,
      variable: proposedVariable(el),
    };
    if (state.recordText) {
      extra.value = value.slice(0, 20000);
      extra.consent_text_recorded = true;
    }
    emit("fill", el, extra);
  }, true);

  document.addEventListener("change", event => {
    if (!state.recording) return;
    const el = event.target;
    if (!(el instanceof Element) || el.tagName.toLowerCase() !== "select") return;
    const value = String(el.value || "");
    const extra = { value_length: value.length, variable: proposedVariable(el) };
    if (state.recordText) {
      extra.value = value.slice(0, 2000);
      extra.consent_text_recorded = true;
    }
    emit("select", el, extra);
  }, true);

  document.addEventListener("submit", () => {
    if (!state.recording) return;
    emit("submit", document.activeElement || null);
  }, true);

  document.addEventListener("scroll", () => {
    if (!state.recording) return;
    const now = Date.now();
    if (now - lastScrollAt < 600) return;
    lastScrollAt = now;
    emit("scroll", null, { meta: { scrollY: window.scrollY } });
  }, { passive: true });

  const originalPush = history.pushState.bind(history);
  const originalReplace = history.replaceState.bind(history);
  history.pushState = function (...args) {
    const result = originalPush(...args);
    emit("navigate", null, { meta: { spa: true } });
    return result;
  };
  history.replaceState = function (...args) {
    const result = originalReplace(...args);
    emit("navigate", null, { meta: { spa: true } });
    return result;
  };
  window.addEventListener("popstate", () => emit("navigate", null, { meta: { spa: true, popstate: true } }));

  // ---------------------------- executor ---------------------------------- //
  function resolveLocator(loc) {
    if (!loc || !loc.strategy) return null;
    const value = String(loc.value || "");
    let candidates = [];
    try {
      if (loc.strategy === "testid") {
        candidates = Array.from(document.querySelectorAll(
          `[data-testid="${cssEscape(value)}"], [data-test="${cssEscape(value)}"], [data-axiom-id="${cssEscape(value)}"]`
        ));
      } else if (loc.strategy === "role") {
        const selectors = [`[role="${cssEscape(value)}"]`, ...implicitRoleSelectors(value)];
        const seen = new Set();
        candidates = [];
        for (const sel of selectors) {
          document.querySelectorAll(sel).forEach(node => {
            if (!seen.has(node)) { seen.add(node); candidates.push(node); }
          });
        }
        const alt = normalize(loc.alt_value);
        if (alt) {
          const named = candidates.filter(node => normalize(accessibleName(node)) === alt);
          if (named.length) candidates = named;
        }
      } else if (loc.strategy === "placeholder") {
        candidates = Array.from(document.querySelectorAll(`[placeholder="${cssEscape(value)}"]`));
      } else if (loc.strategy === "name") {
        candidates = Array.from(document.querySelectorAll(`[name="${cssEscape(value)}"]`));
      } else if (loc.strategy === "label") {
        const want = normalize(value);
        candidates = Array.from(document.querySelectorAll("input, textarea, select, button, [contenteditable='true']"))
          .filter(node => normalize(accessibleName(node)) === want);
      } else if (loc.strategy === "text") {
        const want = normalize(value);
        candidates = Array.from(document.querySelectorAll('button, a[href], [role="button"], [role="link"]'))
          .filter(node => normalize(node.innerText || node.textContent) === want);
      } else if (loc.strategy === "css") {
        candidates = Array.from(document.querySelectorAll(value)).slice(0, 20);
      }
    } catch {
      return null;
    }
    return candidates.find(node => isVisible(node)) || null;
  }

  function resolveTarget(locators) {
    if (!locators || !locators.length) return null;
    const ordered = [...locators].sort((a, b) => (a.priority ?? 50) - (b.priority ?? 50));
    for (const loc of ordered) {
      const el = resolveLocator(loc);
      if (el) return el;
    }
    return null;
  }

  function setNativeValue(el, value) {
    const proto = el.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype
      : el.tagName === "INPUT" ? HTMLInputElement.prototype : null;
    if (proto) {
      const setter = Object.getOwnPropertyDescriptor(proto, "value")?.set;
      if (setter) setter.call(el, value);
      else el.value = value;
    } else {
      el.value = value;
    }
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
  }

  async function actionClick(payload) {
    const el = resolveTarget(payload.target);
    if (!el) return { ok: false, present: false, visible: false, error: "element_not_found" };
    el.scrollIntoView({ block: "center", inline: "center" });
    await sleep(120);
    el.click();
    await sleep(250);
    return { ok: true, present: true, visible: isVisible(el) };
  }

  async function actionFill(payload) {
    const el = resolveTarget(payload.target);
    if (!el) return { ok: false, present: false, error: "element_not_found" };
    const value = String(payload.parameters?.value ?? "");
    el.scrollIntoView({ block: "center" });
    el.focus();
    await sleep(80);
    if (el.isContentEditable) {
      document.execCommand("selectAll", false, null);
      document.execCommand("insertText", false, value);
      await sleep(120);
      const actual = (el.innerText || "").replace(/\n$/, "");
      const ok = actual.length > 0 && Math.abs(actual.length - value.length) <= 2;
      return { ok, value_present: ok, value_length: actual.length, present: true, visible: isVisible(el),
               error: ok ? undefined : "value_mismatch" };
    }
    if (typeof el.value !== "string") return { ok: false, error: "not_a_text_field", present: true };
    setNativeValue(el, value);
    await sleep(150);
    const ok = el.value.length === value.length && (value.length === 0 ? true : el.value.length > 0);
    return {
      ok, value_present: ok, value_length: el.value.length,
      present: true, visible: isVisible(el),
      error: ok ? undefined : "value_mismatch",
    };
  }

  async function actionSelect(payload) {
    const el = resolveTarget(payload.target);
    if (!el) return { ok: false, present: false, error: "element_not_found" };
    const value = String(payload.parameters?.value ?? "");
    if (el.tagName === "SELECT") {
      const option = Array.from(el.options).find(o => o.value === value || normalize(o.text) === normalize(value));
      if (!option) return { ok: false, present: true, error: "option_not_found" };
      el.value = option.value;
      el.dispatchEvent(new Event("change", { bubbles: true }));
      return { ok: true, value_present: true, present: true, visible: isVisible(el) };
    }
    const option = Array.from(document.querySelectorAll('[role="option"], option'))
      .find(o => normalize(o.innerText || o.textContent || o.value) === normalize(value));
    if (option) {
      option.click();
      return { ok: true, value_present: true, present: true, visible: true };
    }
    return { ok: false, present: true, error: "option_not_found" };
  }

  async function actionScroll(payload) {
    const target = payload.parameters?.target ? resolveTarget(payload.parameters.target) : null;
    const y = Number(payload.parameters?.y || 600);
    if (target) target.scrollIntoView({ block: "center" });
    else window.scrollBy({ top: y, behavior: "smooth" });
    await sleep(300);
    return { ok: true, scrollY: window.scrollY };
  }

  async function actionWaitFor(payload) {
    const deadline = Date.now() + Math.min(Number(payload.parameters?.timeout || 10) * 1000, 30000);
    const target = payload.parameters?.target || payload.target;
    while (Date.now() < deadline) {
      if (payload.parameters?.url) {
        const want = String(payload.parameters.url).split("?")[0].split("#")[0];
        if (safeUrl().startsWith(want)) return { ok: true, url: safeUrl(), present: true, visible: true };
      } else if (!target || resolveTarget(target)) {
        const el = target ? resolveTarget(target) : null;
        return { ok: true, present: true, visible: el ? isVisible(el) : true, url: safeUrl() };
      }
      await sleep(250);
    }
    return { ok: false, present: false, visible: false, url: safeUrl(), error: "wait_timeout" };
  }

  async function actionInspect(payload) {
    const checks = Array.isArray(payload.parameters?.checks) ? payload.parameters.checks : [];
    const results = checks.map(check => {
      const el = resolveTarget(check.target);
      return { present: !!el, visible: el ? isVisible(el) : false };
    });
    const primary = payload.verification?.target ? resolveTarget(payload.verification.target) : null;
    return {
      ok: true,
      present: primary ? true : (results.length ? results.every(r => r.present) : true),
      visible: primary ? isVisible(primary) : (results.length ? results.every(r => r.visible) : true),
      url: safeUrl(),
      checks: results,
    };
  }

  function collectVerification(payload, base) {
    const observed = { ...base };
    const verification = payload.verification || {};
    if (verification.method === "url_match") {
      observed.url = safeUrl();
    }
    if (verification.target && ["element_present", "element_visible"].includes(verification.method) && observed.present === undefined) {
      const el = resolveTarget(verification.target);
      observed.present = !!el;
      observed.visible = el ? isVisible(el) : false;
    }
    if (payload.action === "click" && payload.target && observed.present === undefined) {
      const el = resolveTarget(payload.target);
      observed.present = !!el;
      observed.visible = el ? isVisible(el) : false;
    }
    return observed;
  }

  const ACTION_TIMEOUT_CAP = 120000;

  async function runAction(payload) {
    const timeoutMs = Math.min(Number(payload.timeout || 20) * 1000, ACTION_TIMEOUT_CAP);
    const started = Date.now();
    try {
      let result;
      switch (payload.action) {
        case "click": result = await actionClick(payload); break;
        case "fill": result = await actionFill(payload); break;
        case "select": result = await actionSelect(payload); break;
        case "scroll": result = await actionScroll(payload); break;
        case "wait_for": result = await actionWaitFor(payload); break;
        case "inspect_visible_state": result = await actionInspect(payload); break;
        case "request_approval":
          result = { ok: true, note: "approval handled by the session engine" };
          break;
        case "navigate":
          result = { ok: false, error: "navigate_is_executed_by_background" };
          break;
        default:
          result = { ok: false, error: `unsupported_action:${payload.action}` };
      }
      result.elapsed_ms = Date.now() - started;
      return collectVerification(payload, result);
    } catch (error) {
      return { ok: false, error: String(error?.message || error), elapsed_ms: Date.now() - started };
    }
  }

  // ---------------------------- messages ---------------------------------- //
  chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
    if (!message || typeof message.type !== "string") return false;

    if (message.type === "AXIOM_SET_RECORDING") {
      const wasRecording = state.recording;
      state.recording = Boolean(message.enabled);
      state.sessionId = message.sessionId || state.sessionId;
      state.recordText = Boolean(message.recordText);
      if (state.recording && !wasRecording) {
        state.seq = 0;
        emit("navigate", null, { meta: { initial: true } });
      }
      sendResponse({ ok: true, recording: state.recording });
      return false;
    }

    if (message.type === "AXIOM_PING") {
      sendResponse({ ok: true, url: safeUrl(), recording: state.recording });
      return false;
    }

    if (message.type === "AXIOM_EXECUTE_ACTION") {
      runAction(message.payload || {}).then(sendResponse);
      return true;
    }

    if (message.type === "AXIOM_BUILD_LOCATORS") {
      const el = document.activeElement;
      sendResponse({ locators: el ? buildLocators(el) : [], name: el ? accessibleName(el) : "" });
      return false;
    }

    return false;
  });
})();
