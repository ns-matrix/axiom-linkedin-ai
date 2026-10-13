"""Supervised session state machine: approvals, retries, verification, verdicts."""

from __future__ import annotations

import time

import pytest

from axiom import sessions as eng
from axiom.planner import build_plan_from_skill
from axiom.schemas import (
    Locator,
    Plan,
    PlanStep,
    Session,
    VerificationSpec,
)
from axiom import store

FEED = "https://www.linkedin.com/feed/"


def make_plan(sample_skill, inputs=None):
    return build_plan_from_skill(
        sample_skill, "Create a LinkedIn post about testing",
        inputs or {"post_text": "Hello world draft"}, source="deterministic",
    )


def claim(sid):
    return eng.claim_next_step(sid)


def submit(sid, payload, status="succeeded", observed=None, **kw):
    return eng.submit_step_result(
        sid, payload["step"]["step_id"], payload["token"], status,
        observed=observed, **kw,
    )


def run_until_gate(sample_skill, sid=None):
    """Drive the happy path up to (and including) the publish approval gate."""
    plan = make_plan(sample_skill)
    session = eng.create_session(plan)
    sid = session.session_id
    eng.approve_plan(sid)

    observations = [
        {"url": FEED},          # navigate
        {"visible": True},      # open composer
        {"value_present": True, "value_length": 18},  # fill
        {"visible": True},      # review
    ]
    while True:
        payload = claim(sid)
        if payload["status"] == "wait":
            return sid, payload
        if payload["status"] == "terminal":
            raise AssertionError(f"reached terminal state {payload} before the gate")
        step = payload["step"]
        if step["requires_approval"]:
            # executor must not run it; this claim should not have happened
            raise AssertionError("claimed a step that requires approval")
        obs = observations.pop(0) if observations else {"ok": True}
        session = submit(sid, payload, observed=obs)


def test_create_starts_gated(sample_skill):
    session = eng.create_session(make_plan(sample_skill))
    assert session.state == "awaiting_approval"
    assert session.pending_approval["type"] == "plan"
    assert len(session.pending_approval["steps"]) == len(session.plan.steps)
    payload = claim(session.session_id)
    assert payload == {"status": "wait", "state": "awaiting_approval",
                       "reason": "awaiting_plan_approval"}


def test_cannot_create_session_from_unready_plan():
    plan = Plan(goal="g", status="needs_inputs", missing_inputs=["x"],
                steps=[PlanStep(step_id="a", action="scroll", parameters={"y": 1})])
    with pytest.raises(eng.SessionError, match="not executable"):
        eng.create_session(plan)


def test_non_allowlisted_plan_rejected():
    bad_step = PlanStep.model_construct(step_id="a", action="execute_javascript",
                                        parameters={"code": "alert(1)"}, timeout=10)
    plan = Plan.model_construct(goal="g", steps=[bad_step])
    with pytest.raises(eng.SessionError, match="allowlist"):
        eng.create_session(plan)


def test_full_happy_path_completes(sample_skill):
    plan = make_plan(sample_skill)
    session = eng.create_session(plan)
    sid = session.session_id
    eng.approve_plan(sid)

    observations = [
        {"url": FEED},
        {"visible": True},
        {"value_present": True, "value_length": 18},
        {"visible": True},
    ]
    for obs in observations:
        payload = claim(sid)
        assert payload["status"] == "claimed", payload
        session = submit(sid, payload, observed=obs)

    # the publish click stops for its own approval
    gate = claim(sid)
    assert gate["reason"] == "awaiting_step_approval"
    session = eng.approve_step(sid, gate["step_id"])
    payload = claim(sid)
    session = submit(sid, payload, observed={"ok": True})

    assert session.state == "completed"
    assert "Result: completed" in session.summary
    assert all(s.status == "succeeded" for s in session.steps)
    terminal = claim(sid)
    assert terminal["status"] == "terminal"
    assert terminal["state"] == "completed"


def test_publish_step_requires_fresh_approval(sample_skill):
    sid, payload = run_until_gate(sample_skill)
    assert payload["reason"] == "awaiting_step_approval"
    session = eng.get_session(sid)
    assert session.state == "awaiting_approval"
    gate_id = payload["step_id"]
    assert gate_id in session.pending_approval["step_id"]

    # content preview is available for review before approving
    assert session.pending_approval.get("content")

    # executor cannot report a result while approval is pending
    fake = {"step": {"step_id": gate_id}, "token": "tok"}
    with pytest.raises(eng.SessionError):
        eng.submit_step_result(sid, gate_id, "tok", "succeeded", observed={"ok": True})

    # claiming again just waits
    assert claim(sid)["reason"] == "awaiting_step_approval"

    session = eng.approve_step(sid, gate_id)
    assert session.state == "running"
    payload = claim(sid)
    assert payload["status"] == "claimed"
    session = submit(sid, payload, observed={"ok": True})
    assert session.state == "completed"


def test_rejecting_gated_step_cancels_session(sample_skill):
    sid, payload = run_until_gate(sample_skill)
    session = eng.reject_plan(sid)
    assert session.state == "cancelled"
    assert session.plan.status == "rejected"
    assert claim(sid)["status"] == "terminal"


def test_rejecting_plan_before_start(sample_skill):
    session = eng.create_session(make_plan(sample_skill))
    session = eng.reject_plan(session.session_id)
    assert session.state == "cancelled"
    with pytest.raises(eng.SessionError):
        eng.approve_plan(session.session_id)


def test_stale_token_blocked(sample_skill):
    plan = make_plan(sample_skill)
    session = eng.create_session(plan)
    sid = session.session_id
    eng.approve_plan(sid)
    payload = claim(sid)
    with pytest.raises(eng.SessionError, match="token"):
        eng.submit_step_result(sid, payload["step"]["step_id"], "wrong-token", "succeeded")
    # second claim while fresh is refused -> no duplicate execution
    assert claim(sid)["reason"] == "step_in_progress"


def test_stale_claim_is_reissued(sample_skill):
    plan = make_plan(sample_skill)
    session = eng.create_session(plan)
    sid = session.session_id
    eng.approve_plan(sid)
    first = claim(sid)

    stored = eng.get_session(sid)
    stored.steps[0].claim_issued_at = time.time() - 600
    store.save_model("sessions", stored)

    second = claim(sid)
    assert second["status"] == "claimed"
    assert second["token"] != first["token"]
    # original token can no longer post a result
    with pytest.raises(eng.SessionError, match="token"):
        eng.submit_step_result(sid, first["step"]["step_id"], first["token"], "succeeded")


def test_failed_verification_downgrades_and_retries(sample_skill):
    plan = make_plan(sample_skill)
    session = eng.create_session(plan)
    sid = session.session_id
    eng.approve_plan(sid)

    payload = claim(sid)
    # executor claims success, but the observed url does not match
    session = submit(sid, payload, status="succeeded", observed={"url": "https://evil.example/"},
                     error=None, log="navigated")
    assert session.steps[0].status == "pending"          # safe action retried
    assert session.steps[0].attempts == 1

    payload = claim(sid)
    assert payload["attempt"] == 2
    session = submit(sid, payload, status="succeeded", observed={"url": "https://evil.example/"})
    assert session.state == "failed"                     # retries exhausted -> abort
    assert session.steps[0].status == "failed"
    assert "url mismatch" in (session.steps[0].error or "")


def test_verification_inconclusive_reported():
    step = PlanStep(step_id="a", action="inspect_visible_state", title="Check indicator",
                    parameters={}, verification=VerificationSpec(method="element_present"),
                    failure_policy="abort", timeout=5)
    plan = Plan(goal="inconclusive test", steps=[step])
    session = eng.create_session(plan)
    sid = session.session_id
    eng.approve_plan(sid)
    payload = claim(sid)
    session = submit(sid, payload, status="verification_inconclusive",
                     error="no success indicator appeared")
    assert session.state == "verification_inconclusive"
    assert session.steps[0].status == "verification_inconclusive"
    assert "inconclusive" in session.summary


def test_failed_review_aborts(sample_skill):
    plan = make_plan(sample_skill)
    session = eng.create_session(plan)
    sid = session.session_id
    eng.approve_plan(sid)

    submit(sid, claim(sid), observed={"url": FEED})
    submit(sid, claim(sid), observed={"visible": True})
    submit(sid, claim(sid), observed={"value_present": True, "value_length": 3})
    payload = claim(sid)
    assert payload["step"]["action"] == "inspect_visible_state"
    session = submit(sid, payload, status="succeeded", observed={"visible": False})
    assert session.state == "failed"
    assert "visible" in (session.steps[3].error or "")


def test_failed_step_with_continue_yields_partial():
    step_a = PlanStep(step_id="a", action="navigate", title="Go",
                       parameters={"url": "https://example.com/"},
                       verification=VerificationSpec(method="url_match", expect="https://example.com/"),
                       failure_policy="continue", timeout=10)
    step_b = PlanStep(step_id="b", action="scroll", title="Scroll",
                       parameters={"y": 300}, verification=VerificationSpec(method="none"),
                       failure_policy="abort", timeout=10)
    plan = Plan(goal="partial test", steps=[step_a, step_b])
    session = eng.create_session(plan)
    sid = session.session_id
    eng.approve_plan(sid)

    payload = claim(sid)
    session = submit(sid, payload, status="failed", error="net::ERR_CONNECTION_REFUSED")
    assert session.state == "running"
    payload = claim(sid)
    assert payload["step"]["step_id"] == "b"
    session = submit(sid, payload, status="succeeded", observed={"ok": True})
    assert session.state == "partially_completed"
    assert session.steps[0].status == "failed"
    assert session.steps[1].status == "succeeded"


def test_pause_resume_cancel(sample_skill):
    plan = make_plan(sample_skill)
    session = eng.create_session(plan)
    sid = session.session_id
    eng.approve_plan(sid)
    payload = claim(sid)

    session = eng.pause_session(sid)
    assert session.state == "paused"
    assert claim(sid)["reason"] == "paused"
    # result of the in-flight step is still accepted while paused
    session = eng.submit_step_result(sid, payload["step"]["step_id"], payload["token"],
                                     "succeeded", observed={"url": FEED})
    assert session.state == "paused"

    session = eng.resume_session(sid)
    assert session.state == "running"
    session = eng.cancel_session(sid)
    assert session.state == "cancelled"
    assert claim(sid)["status"] == "terminal"


def test_cancel_is_terminal(sample_skill):
    session = eng.create_session(make_plan(sample_skill))
    sid = session.session_id
    eng.approve_plan(sid)
    eng.cancel_session(sid)
    assert eng.get_session(sid).state == "cancelled"
    with pytest.raises(eng.SessionError):
        eng.approve_plan(sid)  # terminal sessions accept no further approvals


def test_invalid_transitions_raise(sample_skill):
    session = eng.create_session(make_plan(sample_skill))
    sid = session.session_id
    with pytest.raises(eng.SessionError, match="cannot start"):
        eng.start_session(sid)          # created/awaiting -> ready only via approve
    with pytest.raises(eng.SessionError, match="cannot resume"):
        eng.resume_session(sid)         # not paused
    eng.approve_plan(sid)
    with pytest.raises(eng.SessionError, match="no plan approval"):
        eng.approve_plan(sid)           # already approved


def test_unknown_session_lookup():
    assert eng.get_session("ses_missing") is None
    with pytest.raises(LookupError):
        eng.claim_next_step("ses_missing")


def test_history_summaries(sample_skill):
    plan = make_plan(sample_skill)
    session = eng.create_session(plan)
    history = eng.list_history()
    assert history and history[0]["session_id"] == session.session_id
    assert history[0]["state"] == "awaiting_approval"
    assert history[0]["progress"]["total"] == len(plan.steps)


def test_approval_preview_contains_draft(sample_skill):
    plan = make_plan(sample_skill)
    session = eng.create_session(plan)
    sid = session.session_id
    eng.approve_plan(sid)
    while True:
        payload = claim(sid)
        if payload["status"] == "wait":
            break
        submit(sid, payload, observed={"url": FEED, "visible": True,
                                       "value_present": True, "value_length": 18, "ok": True})
    preview = eng.get_session(sid).pending_approval
    assert preview["type"] == "step"
    assert "Hello world draft" in preview["content"]
    assert preview["action"] == "click"
