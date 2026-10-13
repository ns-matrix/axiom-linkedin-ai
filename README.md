# Axiom LinkedIn AI

A Chrome Manifest V3 extension with a local Python bridge, Ollama planner, supervised task recording, learned skill library, and an MCP-style endpoint.

## v0.3.0 — record → learn → plan → execute → verify

- **Record**: opt-in recorder captures structured events (click / input / change / submit / scroll / navigate) with locator candidates (`data-testid`, role, label, placeholder, name, text, CSS).
- **Learn**: the bridge turns a recording into a versioned **skill** — inputs, steps, verification specs, risk level, approval requirements, warnings (brittle locators, consent text).
- **Plan**: `POST /tasks/plan` retrieves the matching skill, asks Ollama to extract inputs / draft content (never code), and returns a validated plan. Steps only come from the skill template; the model never invents actions.
- **Execute**: the background service worker claims steps from the bridge and runs them in the tab. Consequential steps (Post, Publish, Send, Connect, Delete, …) stop for **approval** with a content preview; plan-level approval happens before anything runs.
- **Verify**: each step has a verification method (element state / URL match / text present / toast) with timeouts and bounded retries for safe steps only. Clicks abort rather than retry. Results end as `completed`, `partially_completed`, `failed`, `verification_inconclusive` — ambiguous outcomes are reported, never hidden.

Runs are paused/cancellable at any time, logged step-by-step, and kept in execution history.

## Install on Windows
1. Install Python 3.10+ and Ollama.
2. In `local-server` run:
   ```bat
   py -m venv .venv
   .venv\Scripts\activate
   pip install -r requirements.txt
   ```
3. Run `start-local.bat` from the project root and keep the terminal open.
4. Open `chrome://extensions`, enable Developer mode, click **Load unpacked**, and choose `extension/`.
5. Open the extension side panel.

### Practice safely
Open `http://127.0.0.1:8765/demo` — a local fake feed with a composer (`data-testid` targets) so you can record, learn, and run the whole loop without touching a real account.

## Workflow in the panel
1. **Train / Record** → *Start recording on current tab* (Chrome asks for site permission for that origin only) → perform the workflow → *Stop* → *Save recording as skill*.
2. **Skill library** → view steps/inputs/warnings, **Dry run** (no browser touched), Rename, History, Delete.
3. **Run a task** → describe the goal (optionally pick a skill) → *Plan task* → review plan + draft (editable) → **Approve & start supervised run** → watch live step status, approve gated actions, read the final result.

## Local endpoints
- Health: `http://127.0.0.1:8765/health`
- Models: `http://127.0.0.1:8765/models`
- Chat: `POST /chat`
- Skill learning: `POST /recordings`, `POST /skills`, `POST /skills/{id}/dry-run`
- Planning: `POST /tasks/plan`
- Sessions: `POST /sessions`, `/approve`, `/start`, `/pause`, `/resume`, `/cancel`, `/claim`, `/approvals/{step_id}`, `/steps/{step_id}/result`
- Demo page: `http://127.0.0.1:8765/demo`
- MCP-style JSON-RPC: `http://127.0.0.1:8765/mcp` · tools: `http://127.0.0.1:8765/mcp/tools`

## Tests
```bat
cd local-server
.venv\Scripts\python.exe -m pytest tests -q
```
Covers skill learning, action allowlist/validation, planning, session state machine, approvals, retries, verification, and the HTTP API.

## Privacy behavior
When recording is explicitly enabled, Axiom records event types (click/input/scroll), safe element metadata (tag, role, accessible label, placeholder, type), scroll position, and a URL without query parameters or fragments. Typed text is captured **only** if you explicitly enable text capture; passwords are never recorded. A button/link's accessible label can still contain private text, so inspect recordings before saving or sharing them. Events are stored in `chrome.storage.local` on this browser profile; recordings/skills/sessions live as JSON under `local-server/data/` (gitignored).

Chat prompts and plan drafts are sent to your configured local Ollama server. The local Python bridge listens on loopback only.

## Skill bundle
The upstream README supplied by the user documents LinkedIn marketing skills, but does not contain the individual `SKILL.md` files. To use them, clone `https://github.com/sergebulaev/linkedin-skills` and set `SKILLS_DIR` to that directory.

## Limits
This is a supervised MVP. Skills run only from validated action templates (`navigate, click, fill, select, scroll, wait_for, inspect_visible_state, request_approval`) — no model-generated JavaScript, no execution without approval. The MCP endpoint is a small JSON-RPC subset and should be tested with the specific MCP client before relying on it.
