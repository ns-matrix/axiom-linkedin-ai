"""Stage E + F: the supervised execution state machine (server side).

The extension is a thin executor: it claims one allowlisted step at a time,
runs it, and reports the observed state back. This module owns every state
transition, approval gate, retry decision and the final verification verdict,
so the UI, the extension and MCP all enforce identical rules.
"""

from __future__ import annotations

import threading
import time
from typing import Any

from .actions import is_consequential, step_action_payload, validate_steps
from .schemas import (
    TERMINAL_STATES,
    Locator,
    Plan,
    Session,
    StepRuntime,
    VerificationSpec,
    now_iso,
    uid,
)
from . import store

_LOCK = threading.RLock()
MAX_LOGS = 500
MAX_ATTEMPTS = 2
CLAIM_STALE_SECONDS = 60
MAX_SESSIONS_KEPT = 100

ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "created": {"awaiting_approval", "cancelled"},
    "awaiting_approval": {"ready", "running", "cancelled", "failed"},
    "ready": {"running", "paused", "cancelled", "completed", "partially_completed",
              "failed", "verification_inconclusive"},
    "running": {"awaiting_approval", "paused", "cancelled", "ready", "completed",
                "partially_completed", "failed", "verification_inconclusive"},
    "paused": {"running", "cancelled", "failed", "completed", "partially_completed", "verification_inconclusive"},
    "completed": set(),
    "partially_completed": set(),
    "failed": set(),
    "cancelled": set(),
    "verification_inconclusive": set(),
}


class SessionError(Exception):
    pass


def _transition(session: Session, target: str, reason: str = "") -> None:
    allowed = ALLOWED_TRANSITIONS.get(session.state, set())
    if target not in allowed:
        raise SessionError(f"invalid state transition {session.state} -> {target}" + (f": {reason}" if reason else ""))
    session.state = target  # type: ignore[assignment]
    session.updated_at = now_iso()


def _log(session: Session, level: str, message: str, step_id: str | None = None) -> None:
    entry = {"ts": now_iso(), "level": level, "message": _redact(message)}
    if step_id:
        entry["step_id"] = step_id
    session.logs.append(entry)
    if len(session.logs) > MAX_LOGS:
        session.logs = session.logs[-MAX_LOGS:]


def _redact(message: str) -> str:
    """Never let long text payloads or token-shaped strings land in logs."""
    message = str(message)
    if len(message) > 200:
        message = message[:200] + "…[truncated]"
    for sep in ("://",):
        if sep in message:
            parts = message.split(sep)
            if len(parts) == 2 and "@" in parts[1].split("/")[0]:
                host = parts[1].split("/")[0]
                message = message.replace(host, host.split("@")[-1])
    return message


def _save(session: Session) -> None:
    store.save_model("sessions", session)


def _fresh_claim(step: StepRuntime) -> bool:
    return bool(step.claim_token) and (time.time() - step.claim_issued_at) < CLAIM_STALE_SECONDS


def _approval_preview(session: Session, index: int) -> dict[str, Any]:
    """What the user must inspect right before approving a consequential step."""
    step = session.steps[index]
    preview: dict[str, Any] = {
        "step_id": step.step.step_id,
        "title": step.step.title,
        "action": step.step.action,
        "risk_level": step.step.risk_level,
        "expected_outcome": step.step.expected_outcome,
        "target": _target_label(step.step.target),
    }
    if session.plan.draft:
        preview["content"] = session.plan.draft[:4000]
    else:
        for prior in session.steps[:index][::-1]:
            if prior.step.action == "fill":
                value = str(prior.step.parameters.get("value", ""))
                if value and not value.startswith("{{"):
                    preview["content"] = value[:4000]
                    break
                variable = prior.step.variable or ""
                if variable and variable in session.plan.inputs:
                    preview["content"] = str(session.plan.inputs[variable])[:4000]
                    break
    return preview


def _target_label(target: list[Locator] | None) -> str:
    if not target:
        return ""
    return "; ".join(f"{loc.strategy}={loc.value}" for loc in target)


def create_session(plan: Plan, tab_id: int | None = None) -> Session:
    with _LOCK:
        # Status first: a plan missing inputs should say so, not surface a
        # cryptic validation error about the empty values it caused.
        if plan.status not in ("ready", "approved"):
            raise SessionError(f"plan is not executable (status '{plan.status}')"
                               + (f": {plan.message}" if plan.message else ""))
        errors = validate_steps(plan.steps)
        if errors:
            raise SessionError("plan contains non-allowlisted steps: " + "; ".join(errors[:3]))
        if not plan.steps:
            raise SessionError("plan has no steps")
        session = Session(plan=plan, execution_tab_id=tab_id)
        session.steps = [StepRuntime(step=step, index=i) for i, step in enumerate(plan.steps)]
        # A plan always starts gated: a generic "start task" is not enough
        # authorization for the consequential actions inside it.
        _transition(session, "awaiting_approval")
        session.pending_approval = {
            "type": "plan",
            "prompt": "Review and approve this plan before any browser action runs.",
            "steps": [
                {"step_id": s.step.step_id, "title": s.step.title, "action": s.step.action,
                 "requires_approval": s.step.requires_approval, "risk_level": s.step.risk_level}
                for s in session.steps
            ],
        }
        _log(session, "info", f"Session created for plan {plan.plan_id} ({len(plan.steps)} steps)")
        _save(session)
        _prune_sessions()
        return session


def get_session(session_id: str) -> Session | None:
    return store.load_model("sessions", session_id, Session)


def _require(session_id: str) -> Session:
    session = get_session(session_id)
    if session is None:
        raise LookupError(f"session '{session_id}' not found")
    return session


def approve_plan(session_id: str) -> Session:
    with _LOCK:
        session = _require(session_id)
        if session.state != "awaiting_approval" or not session.pending_approval or session.pending_approval.get("type") != "plan":
            raise SessionError("no plan approval is pending")
        session.plan.status = "approved"
        session.pending_approval = None
        _transition(session, "ready")
        _log(session, "info", "Plan approved by user")
        _save(session)
        return session


def reject_plan(session_id: str) -> Session:
    with _LOCK:
        session = _require(session_id)
        if session.state != "awaiting_approval" or not session.pending_approval:
            raise SessionError("no approval is pending")
        kind = session.pending_approval.get("type")
        if kind == "step":
            step = session.steps[session.current_index]
            step.status = "skipped"
            step.error = "rejected by user"
            session.pending_approval = None
            session.plan.status = "rejected"
            _transition(session, "cancelled")
            _log(session, "warn", f"Step '{step.step.title}' rejected by user; session cancelled", step.step.step_id)
        else:
            session.plan.status = "rejected"
            session.pending_approval = None
            _transition(session, "cancelled")
            _log(session, "warn", "Plan rejected by user")
        _save(session)
        return session


def start_session(session_id: str) -> Session:
    with _LOCK:
        session = _require(session_id)
        if session.state == "running":
            return session
        if session.state != "ready":
            raise SessionError(f"session cannot start from state '{session.state}'")
        _transition(session, "running")
        _log(session, "info", "Execution started")
        _save(session)
        return session


def pause_session(session_id: str) -> Session:
    with _LOCK:
        session = _require(session_id)
        if session.state == "paused":
            return session
        if session.state not in ("running", "ready"):
            raise SessionError(f"session cannot pause from state '{session.state}'")
        _transition(session, "paused")
        _log(session, "warn", "Execution paused by user")
        _save(session)
        return session


def resume_session(session_id: str) -> Session:
    with _LOCK:
        session = _require(session_id)
        if session.state == "running":
            return session
        if session.state != "paused":
            raise SessionError(f"session cannot resume from state '{session.state}'")
        _transition(session, "running")
        _log(session, "info", "Execution resumed")
        _save(session)
        return session


def cancel_session(session_id: str) -> Session:
    with _LOCK:
        session = _require(session_id)
        if session.state in TERMINAL_STATES:
            return session
        if session.state == "running":
            _transition(session, "cancelled")
        elif session.state == "paused" and session.steps and any(s.status == "running" for s in session.steps):
            _transition(session, "cancelled")
        else:
            _transition(session, "cancelled")
        for step in session.steps:
            if step.status in ("pending", "running", "awaiting_approval"):
                step.status = "skipped"
        session.pending_approval = None
        _log(session, "warn", "Session cancelled")
        _save(session)
        return session


def approve_step(session_id: str, step_id: str) -> Session:
    with _LOCK:
        session = _require(session_id)
        if session.state != "awaiting_approval" or not session.pending_approval or session.pending_approval.get("type") != "step":
            raise SessionError("no step approval is pending")
        if session.pending_approval.get("step_id") != step_id:
            raise SessionError("pending approval is for a different step")
        step = session.steps[session.current_index]
        step.approved = True
        step.status = "pending"
        session.pending_approval = None
        _transition(session, "running")
        _log(session, "info", f"User approved step '{step.step.title}'", step_id)
        _save(session)
        return session


def claim_next_step(session_id: str) -> dict[str, Any]:
    """Executor heartbeat: returns the next step to run, or why it must wait."""
    with _LOCK:
        session = _require(session_id)

        if session.state in TERMINAL_STATES:
            return {"status": "terminal", "state": session.state, "summary": session.summary}

        if session.state == "awaiting_approval":
            pending = session.pending_approval or {}
            if pending.get("type") == "step":
                step = session.steps[session.current_index]
                if step.approved:
                    _transition(session, "running")
                    _save(session)
                else:
                    return {"status": "wait", "state": session.state, "reason": "awaiting_step_approval",
                            "step_id": pending.get("step_id")}
            else:
                return {"status": "wait", "state": session.state, "reason": "awaiting_plan_approval"}

        if session.state in ("created", "paused"):
            return {"status": "wait", "state": session.state, "reason": session.state}

        if session.state == "ready":
            _transition(session, "running")
            _log(session, "info", "Execution started")
            _save(session)

        # Advance over already-finished steps.
        while session.current_index < len(session.steps):
            step = session.steps[session.current_index]
            if step.status in ("succeeded", "skipped", "failed", "verification_inconclusive"):
                session.current_index += 1
                continue
            break

        if session.current_index >= len(session.steps):
            _finalize(session)
            _save(session)
            return {"status": "terminal", "state": session.state, "summary": session.summary}

        step = session.steps[session.current_index]

        if step.status == "running" and _fresh_claim(step):
            return {"status": "wait", "state": session.state, "reason": "step_in_progress", "step_id": step.step.step_id}

        if step.step.requires_approval and not step.approved:
            step.status = "awaiting_approval"
            session.pending_approval = {
                "type": "step",
                "step_id": step.step.step_id,
                **_approval_preview(session, session.current_index),
            }
            _transition(session, "awaiting_approval")
            _log(session, "warn", f"Approval required before '{step.step.title}'", step.step.step_id)
            _save(session)
            return {"status": "wait", "state": session.state, "reason": "awaiting_step_approval",
                    "step_id": step.step.step_id}

        token = uid("tok")
        step.status = "running"
        step.attempts += 1
        step.claim_token = token
        step.claim_issued_at = time.time()
        step.started_at = step.started_at or now_iso()
        session.updated_at = now_iso()
        _log(session, "info", f"Claimed step '{step.step.title}' (attempt {step.attempts})", step.step.step_id)
        _save(session)
        return {
            "status": "claimed",
            "state": session.state,
            "step": step_action_payload(step.step),
            "token": token,
            "attempt": step.attempts,
        }


def submit_step_result(
    session_id: str,
    step_id: str,
    token: str,
    status: str,
    observed: dict[str, Any] | None = None,
    error: str | None = None,
    log: str | None = None,
) -> Session:
    with _LOCK:
        session = _require(session_id)
        if session.state in TERMINAL_STATES:
            raise SessionError(f"session already finished ({session.state})")
        if session.state not in ("running", "paused"):
            raise SessionError(f"cannot accept a result while state is '{session.state}'")

        index = next((i for i, s in enumerate(session.steps) if s.step.step_id == step_id), None)
        if index is None:
            raise LookupError(f"step '{step_id}' not part of this session")
        step = session.steps[index]
        if step.claim_token != token:
            raise SessionError("stale or unknown claim token (duplicate execution blocked)")
        if status not in ("succeeded", "failed", "verification_inconclusive"):
            raise SessionError(f"invalid result status '{status}'")

        observed = observed or {}
        step.result = dict(observed)
        step.claim_token = None
        step.finished_at = now_iso()

        if log:
            _log(session, "info", log, step_id)

        # Server-side verification: a dispatched click is never a success by itself.
        if status == "succeeded":
            verdict, reason = _verify(step.step.verification, observed)
            if verdict == "fail":
                status = "failed"
                error = reason
            elif verdict == "inconclusive":
                status = "verification_inconclusive"
                error = reason

        if status == "succeeded":
            step.status = "succeeded"
            _log(session, "info", f"Verified: {step.step.expected_outcome or 'step complete'}", step_id)
            session.current_index = index + 1
        elif status == "verification_inconclusive":
            step.status = "verification_inconclusive"
            step.error = error or "verification inconclusive"
            _log(session, "warn", f"Verification inconclusive for '{step.step.title}': {step.error}", step_id)
            if step.step.failure_policy == "abort":
                session.current_index = index + 1
                _finalize(session)
                _save(session)
                return session
            session.current_index = index + 1
        else:  # failed
            step.error = error or "step failed"
            _log(session, "error", f"Step '{step.step.title}' failed: {step.error}", step_id)
            policy = step.step.failure_policy
            if policy == "retry_safe" and step.attempts < MAX_ATTEMPTS and step.step.action in (
                "navigate", "fill", "select", "scroll", "wait_for", "inspect_visible_state"
            ):
                step.status = "pending"
                _log(session, "warn", f"Retrying safe step '{step.step.title}' ({step.attempts}/{MAX_ATTEMPTS})", step_id)
                session.updated_at = now_iso()
                _save(session)
                return session
            if policy == "continue":
                step.status = "failed"
                session.current_index = index + 1
            else:
                step.status = "failed"
                session.current_index = index + 1
                _finalize(session, aborted=True)
                _save(session)
                return session

        if session.current_index >= len(session.steps):
            _finalize(session)
        session.updated_at = now_iso()
        _save(session)
        return session


def _verify(spec: VerificationSpec, observed: dict[str, Any]) -> tuple[str, str]:
    """Compare executor observations with the expected outcome.

    Returns (pass|fail|inconclusive, reason).
    """
    method = spec.method
    if method == "none":
        if observed.get("ok") is False:
            return "fail", str(observed.get("error") or "action reported failure")
        return "pass", ""

    if method == "url_match":
        got = str(observed.get("url") or "")
        expect = str(spec.expect or "")
        if not got:
            return "inconclusive", "no observed url reported"
        if expect and not got.startswith(expect.rstrip("/")):
            return "fail", f"url mismatch (expected {expect}, got {got})"
        return "pass", ""

    if method in ("element_present", "element_visible", "value_present", "text_present"):
        key = {"element_present": "present", "element_visible": "visible",
               "value_present": "value_present", "text_present": "text_found"}[method]
        if key not in observed:
            return "inconclusive", f"executor did not report '{key}'"
        return ("pass", "") if observed.get(key) else ("fail", f"expected {key} to be true")

    return "pass", ""


def _finalize(session: Session, aborted: bool = False) -> None:
    if session.state in TERMINAL_STATES:
        return
    statuses = [s.status for s in session.steps]
    failed = statuses.count("failed")
    inconclusive = statuses.count("verification_inconclusive")
    succeeded = statuses.count("succeeded")
    skipped = statuses.count("skipped")

    if aborted or (failed and any(s.step.failure_policy == "abort" for s in session.steps if s.status == "failed")):
        target = "failed"
    elif failed and succeeded == 0:
        target = "failed"
    elif failed:
        target = "partially_completed"
    elif skipped and succeeded == 0:
        target = "cancelled"
    elif skipped:
        target = "partially_completed"
    elif inconclusive:
        target = "verification_inconclusive"
    else:
        target = "completed"

    _transition(session, target)
    session.plan.status = "approved"
    counts = {"succeeded": succeeded, "failed": failed, "inconclusive": inconclusive, "skipped": skipped}
    session.summary = f"Result: {target} ({counts['succeeded']} ok, {counts['failed']} failed, {counts['inconclusive']} inconclusive, {counts['skipped']} skipped)"
    _log(session, "info" if target == "completed" else "warn", session.summary)


def session_summary(session: Session) -> dict[str, Any]:
    done = sum(1 for s in session.steps if s.status in ("succeeded", "skipped"))
    return {
        "session_id": session.session_id,
        "goal": session.plan.goal,
        "skill_id": session.plan.skill_id,
        "skill_name": session.plan.skill_name,
        "state": session.state,
        "progress": {"done": done, "total": len(session.steps), "current_index": session.current_index},
        "created_at": session.created_at,
        "updated_at": session.updated_at,
        "summary": session.summary,
    }


def list_history(limit: int = 50) -> list[dict[str, Any]]:
    sessions = store.list_models("sessions", Session)
    sessions.sort(key=lambda s: s.created_at, reverse=True)
    return [session_summary(s) for s in sessions[:limit]]


def _prune_sessions() -> None:
    sessions = store.list_models("sessions", Session)
    if len(sessions) <= MAX_SESSIONS_KEPT:
        return
    sessions.sort(key=lambda s: s.created_at)
    for old in sessions[: len(sessions) - MAX_SESSIONS_KEPT]:
        if old.state in TERMINAL_STATES:
            store.delete_model("sessions", old.session_id)
